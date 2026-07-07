"""Read-only dashboard: API endpoints against a temp DB + status file."""

import json

import pytest

pytest.importorskip("flask")

from statarb.database import DataLogger                     # noqa: E402
from statarb.models import OrderSide, Position, SignalType, Trade  # noqa: E402
from statarb.webapp import create_app                       # noqa: E402


@pytest.fixture
def dashboard(tmp_path):
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

    app = create_app(db_path=str(tmp_path / "dash.db"),
                     status_path=str(status_path))
    app.config['TESTING'] = True
    return app.test_client()


def test_index_serves_page(dashboard):
    response = dashboard.get('/')
    assert response.status_code == 200
    assert b'StatArb Dashboard' in response.data


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
