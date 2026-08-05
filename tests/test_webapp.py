"""Read-only dashboard: API endpoints against a temp DB + status file."""

import json

import pytest

pytest.importorskip("flask")

from statarb.database import DataLogger                     # noqa: E402
from statarb.models import OrderSide, Position, SignalType, Trade  # noqa: E402
from statarb.webapp import create_app                       # noqa: E402


@pytest.fixture
def paths(tmp_path):
    return {'config': str(tmp_path / "config.json"),
            'control': str(tmp_path / "control.json")}


@pytest.fixture
def dashboard(tmp_path, paths):
    db = DataLogger(db_path=str(tmp_path / "dash.db"))

    spot = Trade('XAUUSD', OrderSide.BUY, 50.0)
    spot.executed_price = 3300.0
    fut = Trade('GC1225', OrderSide.SELL, 50.0)
    fut.executed_price = 3320.0
    position = Position('POS_0001', 'GOLD', SignalType.SELL_BASIS, spot, fut)
    position.realized_pnl = 500.0
    position.close_reason = 'TAKE_PROFIT'
    position.exit_plan = {'entry_z': 3.0, 'entry_sigma': 2.0,
                          'tp_usd': 15000, 'stop_usd': 1500,
                          'rt_cost_usd': 3000, 'gate_floor_usd': 0,
                          'max_hold_sec': 2400}
    from datetime import datetime
    position.close_time = datetime.now()
    db.log_trade_review(position, exit_z=0.3)
    db.log_untracked_close('account_a', 'XAUUSD', 999, 10.0, 3301.0,
                           'orphan auto-close')

    status_path = tmp_path / "status.json"
    status_path.write_text(json.dumps({
        'mode': 'PAPER', 'updated': '12:00:00', 'halted': False,
        'halt_reason': None, 'daily_pnl': 500.0,
        'assets': [{'asset': 'GOLD', 'z': 1.2, 'basis': 20.0,
                    'lots_today': 100, 'lot_target': 500}],
        'positions': [{'position_id': 'POS_0002', 'asset': 'GOLD',
                       'signal_type': 'SELL_BASIS', 'lots': 50.0,
                       'entry_premium': 25.0, 'unrealized_pnl': 250.0,
                       'age': '1.5h'}],
    }))

    import json as json_mod
    with open(paths['config'], 'w') as f:
        json_mod.dump({'accounts': {'account_a': {'login': 1},
                                    'account_b': {'login': 2}},
                       'leg_accounts': {'spot': 'account_a',
                                        'futures': 'account_b'},
                       'trading': {'HEDGE_RATIO': 1.0, 'CLIP_LOTS': 50.0},
                       'signals': {'ENTRY_Z': 3.0}}, f)

    app = create_app(db_path=str(tmp_path / "dash.db"),
                     status_path=str(status_path),
                     config_path=paths['config'],
                     control_path=paths['control'])
    app.config['TESTING'] = True
    return app.test_client()


def test_pages_serve(dashboard):
    for route in ('/', '/settings', '/analysis'):
        response = dashboard.get(route)
        assert response.status_code == 200, route
    assert b'Brokers & legs (MT5)' in dashboard.get('/settings').data


def test_summary_reflects_runtime_status(dashboard):
    data = dashboard.get('/api/summary').get_json()
    assert data['mode'] == 'PAPER'
    assert data['assets'][0]['z'] == 1.2
    assert data['daily_pnl'] == 500.0


def test_positions_prefer_live_snapshot(dashboard):
    rows = dashboard.get('/api/positions').get_json()
    assert rows[0]['position_id'] == 'POS_0002'
    assert rows[0]['unrealized_pnl'] == 250.0


def test_reviews_and_untracked_from_db(dashboard):
    reviews = dashboard.get('/api/reviews').get_json()
    assert reviews[0]['position_id'] == 'POS_0001'
    assert reviews[0]['realized_pnl'] == 500.0
    assert reviews[0]['exit_reason'] == 'TAKE_PROFIT'

    untracked = dashboard.get('/api/untracked').get_json()
    assert untracked[0]['ticket'] == 999


def test_market_endpoint_empty_ok(dashboard):
    assert dashboard.get('/api/market?asset=GOLD').get_json() == []


def test_engine_toggle_and_close_write_control(dashboard, paths):
    import json as json_mod
    response = dashboard.post('/api/engine/toggle')
    assert response.status_code == 200
    control = json_mod.load(open(paths['control']))
    assert control['algo_enabled'] is False    # status said True

    response = dashboard.post('/api/engine/close',
                              json={'position_id': 'POS_0002'})
    assert response.status_code == 200
    control = json_mod.load(open(paths['control']))
    assert control['close']['position_id'] == 'POS_0002'
    assert control['algo_enabled'] is False    # toggle preserved


def test_config_save_merges_and_backfills(dashboard, paths):
    import json as json_mod
    response = dashboard.post('/api/config', json={
        'sections': {'SIGNALS': {'ENTRY_Z': 2.5},
                     'EXITS': {'HARD_MAX_HOLD_MIN': 90.0}},
    })
    assert response.status_code == 200
    raw = json_mod.load(open(paths['config']))
    assert raw['signals']['ENTRY_Z'] == 2.5
    assert raw['exits']['HARD_MAX_HOLD_MIN'] == 90.0
    # Untouched sections/keys survive (back-fill rule)
    assert raw['trading']['CLIP_LOTS'] == 50.0
    assert raw['accounts']['account_a']['login'] == 1


def test_config_rejects_beta_change_while_in_trade(dashboard, paths):
    # runtime status fixture shows an open position -> 409, W3 rule
    response = dashboard.post('/api/config', json={
        'sections': {'TRADING': {'HEDGE_RATIO': 2.0}},
    })
    assert response.status_code == 409
    import json as json_mod
    raw = json_mod.load(open(paths['config']))
    assert raw['trading']['HEDGE_RATIO'] == 1.0    # unchanged


def test_analysis_endpoint_math(dashboard):
    stats = dashboard.get('/api/analysis').get_json()
    # One closed trade in the fixture: +$500
    assert stats['total'] == 1 and stats['winners'] == 1
    assert stats['total_pnl'] == 500.0
    assert stats['win_rate'] == 100.0
