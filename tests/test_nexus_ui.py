"""Nexus UI: every page renders and every endpoint the templates call
responds. This is the regression net for the vendored W3 templates —
if a template starts asking for a field the backend doesn't serve,
these tests fail before the operator ever sees a broken page."""

import json
import os
import re

import pytest

pytest.importorskip("flask")

from statarb.database import DataLogger                      # noqa: E402
from statarb.models import OrderSide, Position, SignalType, Trade  # noqa: E402
from statarb.webapp import create_app                        # noqa: E402

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TEMPLATES = os.path.join(BASE_DIR, 'templates')


def seed_db(db):
    spot = Trade('XAUUSD', OrderSide.BUY, 50.0)
    spot.executed_price = 3300.0
    fut = Trade('GC1225', OrderSide.SELL, 50.0)
    fut.executed_price = 3320.0
    for i in range(6):
        position = Position(f'POS_{i:04d}', 'GOLD', SignalType.SELL_BASIS,
                            spot, fut)
        position.realized_pnl = 500.0 if i % 2 else -200.0
        position.close_reason = 'TAKE_PROFIT' if i % 2 else 'DOLLAR_STOP'
        position.peak_pnl, position.peak_min = 900.0, 12.0
        position.trough_pnl, position.trough_min = -300.0, 40.0
        position.exit_plan = {'entry_z': 3.0, 'entry_sigma': 2.0,
                              'tp_usd': 15000.0, 'stop_usd': 1500.0,
                              'rt_cost_usd': 3000.0, 'max_hold_sec': 2400,
                              'gate_floor_usd': 0.0, 'entry_spread': 20.0,
                              'levels': {'entry_spread': 20.0, 'be': 19.4,
                                         'ex': 19.4, 'tp': 16.4,
                                         'sl': 20.3, 'favorable': 'down'}}
        from datetime import datetime
        position.close_time = datetime.now()
        db.log_trade_review(position, exit_z=0.3, outcome='TARGET_HIT',
                            exit_spread=17.0, notional=16500000.0)
        db.log_shadow({'position_id': f'POS_{i:04d}', 'asset': 'GOLD',
                       'exit_reason': 'DOLLAR_STOP', 'exit_pnl': -200.0,
                       'net': 400.0, 'peak': 900.0, 'trough': -300.0,
                       'hit_be_min': 10.0, 'hit_tp_min': None,
                       'horizon_sec': 7200,
                       'verdict': 'REVERTED_TO_BREAK_EVEN'})
    db.log_sd_touch('GOLD', 2, 'UP', 2.1, 20.5)
    db.log_untracked_close('account_a', 'XAUUSD', 999, 10.0, 3301.0, 'test')


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    # UI secret-saving writes into os.environ; keep it out of other tests
    monkeypatch.setattr(os, 'environ', dict(os.environ))
    db = DataLogger(db_path=str(tmp_path / "algo.db"))
    seed_db(db)

    (tmp_path / "config.json").write_text(json.dumps({
        'trading_mode': 'paper',
        'accounts': {'account_a': {'terminal_path': 'C:/A/terminal64.exe',
                                   'login': 111, 'server': 'FxPro-Demo',
                                   'password_env': 'MT5_PASSWORD_A',
                                   'endpoint': '127.0.0.1:9101'},
                     'account_b': {'terminal_path': 'C:/B/terminal64.exe',
                                   'login': 222, 'server': 'CFI-Demo',
                                   'password_env': 'MT5_PASSWORD_B',
                                   'endpoint': '127.0.0.1:9102'}},
        'leg_accounts': {'spot': 'account_a', 'futures': 'account_b'},
        'trading': {'HEDGE_RATIO': 1.0, 'CLIP_LOTS': 50.0},
        'signals': {'ENTRY_Z': 3.0},
        'assets': {'GOLD': {'name': 'GOLD', 'enabled': True,
                            'spot_symbols': ['XAUUSD'],
                            'futures_symbols': ['GC1225'],
                            'futures_expiry': '2026-11-25',
                            'lot_size': 100, 'swap_charge': 45.0,
                            'risk_free_rate': 0.0425, 'multiplier': 1.0}},
    }))
    (tmp_path / "runtime_status.json").write_text(json.dumps({
        'mode': 'PAPER', 'algo_enabled': True, 'updated': '12:00:00',
        'halted': False, 'halt_reason': None, 'daily_pnl': 500.0,
        'assets': [{'asset': 'GOLD', 'z': 1.2, 'basis': 20.0,
                    'swap_diff': 1.5, 'spot_price': 3300.0,
                    'spot_bid': 3299.9, 'spot_ask': 3300.1,
                    'futures_price': 3320.0, 'fut_bid': 3319.9,
                    'fut_ask': 3320.1, 'lots_today': 100,
                    'lot_target': 500}],
        'positions': [{'position_id': 'POS_0009', 'asset': 'GOLD',
                       'signal_type': 'SELL_BASIS', 'lots': 50.0,
                       'entry_premium': 25.0, 'unrealized_pnl': 250.0,
                       'net_pnl': -2750.0, 'age': '1.5h', 'age_sec': 5400,
                       'entry_spot': 3300.0, 'entry_fut': 3320.0,
                       'entry_z': 3.1, 'tp_usd': 15000.0,
                       'stop_usd': 1500.0, 'rt_cost_usd': 3000.0,
                       'max_hold_sec': 2400, 'peak_pnl': 900.0,
                       'trough_pnl': -300.0,
                       'levels': {'entry_spread': 20.0, 'be': 19.4,
                                  'ex': 19.4, 'tp': 16.4, 'sl': 20.3,
                                  'favorable': 'down'}}],
        'shadow': {'active': 1, 'tracking': [
            {'position_id': 'POS_0008', 'asset': 'GOLD',
             'exit_reason': 'DOLLAR_STOP', 'exit_pnl': -200.0,
             'net': 100.0, 'peak': 300.0, 'trough': -50.0,
             'minutes': 30.0, 'horizon_min': 120.0}]},
        'test_results': None,
    }))

    app = create_app(db_path=str(tmp_path / "algo.db"),
                     status_path=str(tmp_path / "runtime_status.json"),
                     config_path=str(tmp_path / "config.json"),
                     control_path=str(tmp_path / "control.json"),
                     env_path=str(tmp_path / ".env"))
    app.config['TESTING'] = True
    client = app.test_client()
    client.tmp_path = tmp_path
    return client


# ---------------------------------------------------------------------------
# Pages
# ---------------------------------------------------------------------------

def nav_links():
    """Every href the vendored navbar exposes — each must resolve."""
    base = open(os.path.join(TEMPLATES, 'base.html'), encoding='utf-8').read()
    return sorted(set(re.findall(r'href="(/[a-z-]*)"', base)))


@pytest.mark.parametrize('route', ['/', '/dashboard', '/analysis',
                                   '/settings', '/setup'])
def test_every_page_renders(client, route):
    response = client.get(route)
    assert response.status_code == 200, route
    body = response.data.decode()
    assert 'Nexus' in body                      # the vendored W3 chrome
    assert 'jinja2' not in body.lower()


@pytest.mark.parametrize('href', nav_links())
def test_every_navbar_link_resolves(client, href):
    """No dead links in the nav — this is what /dashboard 404'd on."""
    assert client.get(href).status_code == 200, href


def test_dashboard_is_the_nexus_template(client):
    body = client.get('/').data.decode()
    # Markers unique to the W3 dashboard, not to a hand-rolled page
    assert 'position-details' in body
    assert 'socket.io' in body
    assert 'logo-nexus.svg' in body


def test_settings_page_is_mt5_native_not_crypto(client):
    """The Nexus chrome is W3's, but every field must be MT5's."""
    body = client.get('/settings').data.decode()
    for crypto in ('OKX', 'BTC-USDT', 'passphrase', 'perpetual swap'):
        assert crypto.lower() not in body.lower(), crypto
    # MT5 broker management replaces the exchange API-key card
    assert 'MT5 Brokers' in body
    assert 'broker_terminal_path' in body
    assert 'leg_spot_account' in body and 'leg_futures_account' in body
    # All three topologies are explained on the page
    assert 'Both legs' in body
    # Sizing is in lots, not USD notional
    assert 'clip_lots' in body
    assert 'position_size_usd' not in body


def test_all_pages_are_free_of_crypto_wording(client):
    for route in ('/', '/analysis', '/settings'):
        body = client.get(route).data.decode()
        assert 'okx' not in body.lower(), route


def test_account_info_exposes_both_accounts_and_margins(client, tmp_path):
    """Two brokers means two margin pools — M2M is managed per account,
    so both must be visible, never only the combined figure."""
    status = json.loads((tmp_path / "runtime_status.json").read_text())
    status['accounts'] = {
        'account_a': {'account': 'account_a', 'login': 591805111,
                      'server': 'FxPro-MT5 Demo', 'currency': 'USD',
                      'roles': ['spot'], 'balance': 100000.0,
                      'equity': 98000.0, 'margin': 20000.0,
                      'margin_free': 78000.0, 'margin_level': 490.0,
                      'profit': -2000.0},
        'account_b': {'account': 'account_b', 'login': 887766,
                      'server': 'CFI-MT5 Demo', 'currency': 'USD',
                      'roles': ['futures'], 'balance': 50000.0,
                      'equity': 51500.0, 'margin': 40000.0,
                      'margin_free': 11500.0, 'margin_level': 128.75,
                      'profit': 1500.0},
    }
    (tmp_path / "runtime_status.json").write_text(json.dumps(status))

    data = client.get('/api/account-info').get_json()
    accounts = {a['account']: a for a in data['accounts']}
    assert set(accounts) == {'account_a', 'account_b'}
    # Both logins and balances surface
    assert accounts['account_a']['login'] == 591805111
    assert accounts['account_b']['login'] == 887766
    assert accounts['account_a']['balance'] == 100000.0
    assert accounts['account_b']['balance'] == 50000.0
    # Per-account margin detail
    assert accounts['account_a']['margin'] == 20000.0
    assert accounts['account_b']['margin_free'] == 11500.0
    assert accounts['account_a']['roles'] == ['spot']
    # Combined roll-up
    assert data['totals']['equity'] == pytest.approx(149500.0)
    assert data['totals']['margin'] == pytest.approx(60000.0)
    assert data['totals']['margin_level'] == pytest.approx(249.17, rel=1e-3)
    # The WEAKEST account drives the risk view — a healthy combined
    # 249% must not mask account_b sitting at 129%
    assert data['weakest']['account'] == 'account_b'
    assert data['weakest']['margin_level'] == pytest.approx(128.75)
    assert '591805111' in data['uid'] and '887766' in data['uid']


def test_account_info_lists_accounts_before_connection(client):
    """Before MT5 connects, both configured accounts still appear so
    the operator can see what the engine will connect to."""
    data = client.get('/api/account-info').get_json()
    accounts = {a['account'] for a in data['accounts']}
    assert accounts == {'account_a', 'account_b'}
    assert all(a['connected'] is False for a in data['accounts'])
    # Config values show through even unconnected
    by_name = {a['account']: a for a in data['accounts']}
    assert by_name['account_a']['login'] == 111
    assert by_name['account_a']['roles'] == ['spot']


def test_every_configured_account_is_listed_even_if_unused(client, tmp_path):
    """A configured account no leg points at must still be visible —
    otherwise it silently disappears from the dashboard (this is what
    left the operator seeing a single row)."""
    raw = json.loads((tmp_path / "config.json").read_text())
    raw['leg_accounts'] = {'spot': 'account_a', 'futures': 'account_a'}
    (tmp_path / "config.json").write_text(json.dumps(raw))

    data = client.get('/api/account-info').get_json()
    by_name = {a['account']: a for a in data['accounts']}
    assert set(by_name) == {'account_a', 'account_b'}
    assert by_name['account_a']['roles'] == ['spot', 'futures']
    assert by_name['account_b']['roles'] == []      # configured, unused


def test_dashboard_layout_order(client):
    """Per-account margin sits AFTER the liquidation rows (it was
    landing above them, so the Leg A/B Liq lines read as if they were
    the per-account breakdown), and Manual Spread Trade is a compact
    card directly above Reset."""
    body = client.get('/').data.decode()
    liq = body.index('id="leg-b-liq-row"')
    per_account = body.index('PER-ACCOUNT MARGIN')
    assert liq < per_account, 'per-account block must follow the liq rows'

    manual = body.index('Manual Spread Trade')
    reset = body.index('Reset Controls')
    assert manual < reset, 'manual trade card belongs above Reset'
    # ...and inside the right-hand column, not the full-width top area
    assert body.index('accounts-strip') < manual


def test_dashboard_renders_per_account_margin(client):
    body = client.get('/').data.decode()
    assert 'accounts-strip' in body            # per-account cards
    assert 'accounts-margin-table' in body     # per-account margin rows
    assert 'PER-ACCOUNT MARGIN' in body
    assert 'renderAccountsStrip' in body


def test_margin_breaker_toggle_round_trips_through_ui(client):
    """The operator must be able to arm and disarm it from Settings."""
    body = client.get('/settings').data.decode()
    assert 'margin_breaker_enabled' in body
    assert 'Margin Breaker' in body
    assert 'margin_halt_level' in body and 'margin_reduce_level' in body

    assert client.get('/api/config').get_json()[
        'margin_breaker_enabled'] is False        # off by default

    response = client.post('/api/config', json={
        'margin_breaker_enabled': True, 'margin_halt_level': 250,
        'margin_reduce_enabled': True, 'margin_reduce_level': 500,
        'margin_min_size_fraction': 0.3, 'margin_min_free_usd': 5000})
    assert response.status_code == 200

    raw = json.loads((client.tmp_path / "config.json").read_text())
    limits = raw['risk_limits']
    assert limits['MARGIN_BREAKER_ENABLED'] is True
    assert limits['MARGIN_HALT_LEVEL'] == 250
    assert limits['MARGIN_REDUCE_ENABLED'] is True
    assert limits['MARGIN_MIN_SIZE_FRACTION'] == 0.3

    config = client.get('/api/config').get_json()
    assert config['margin_breaker_enabled'] is True
    assert config['margin_halt_level'] == 250

    # ...and back off again
    client.post('/api/config', json={'margin_breaker_enabled': False})
    assert client.get('/api/config').get_json()[
        'margin_breaker_enabled'] is False


def test_socketio_server_is_wired(client):
    """The Nexus navbar shows Connected/Disconnected from a socket.io
    session — without a server it sat on 'Disconnected' forever."""
    pytest.importorskip("flask_socketio")
    assert client.application.socketio is not None
    handshake = client.get('/socket.io/?EIO=4&transport=polling')
    assert handshake.status_code == 200
    assert b'"sid"' in handshake.data          # engine.io session opened


def test_static_assets_served(client):
    for asset in ('logo-nexus.svg', 'favicon.svg'):
        assert client.get(f'/static/{asset}').status_code == 200


# ---------------------------------------------------------------------------
# Every endpoint the templates call must answer
# ---------------------------------------------------------------------------

def template_endpoints():
    """Scrape fetch('/api/...') out of the vendored templates so this
    test tracks the UI automatically."""
    found = set()
    for name in os.listdir(TEMPLATES):
        text = open(os.path.join(TEMPLATES, name), encoding='utf-8').read()
        for match in re.finditer(r"fetch\(['\"`](/api/[^'\"`?]+)", text):
            url = match.group(1)
            if '${' in url or '{' in url:
                continue                    # dynamic id routes
            found.add(url)
    return sorted(found)


@pytest.mark.parametrize('url', template_endpoints())
def test_template_endpoint_responds(client, url):
    """GET must not 404/500. POST-only endpoints answer 405, which
    proves the route exists."""
    response = client.get(url)
    assert response.status_code in (200, 400, 405), \
        f"{url} -> {response.status_code}"


def test_engine_status_shape(client):
    data = client.get('/api/engine/status').get_json()
    assert data['algo_enabled'] is True
    assert data['paper_trading'] is True
    assert data['execution_backend'] == 'MT5'
    assert data['signal']['zscore'] == 1.2
    assert data['spot_tick']['bid'] == 3299.9
    trade = data['open_trade']
    assert trade['position_type'] == 'SHORT'      # SELL_BASIS -> SHORT
    assert trade['spread_levels']['break_even'] == 19.4
    assert trade['max_hold_minutes'] == 40.0


def test_trade_journal_shape(client):
    data = client.get('/api/trade-journal').get_json()
    assert len(data['trades']) == 6
    trade = data['trades'][0]
    for field in ('entry_spread', 'exit_spread', 'be_spread', 'tp_spread',
                  'sl_spread', 'pnl_usd', 'notional_usd', 'exit_reason'):
        assert field in trade
    stats = data['statistics']
    assert stats['total_trades'] == 6
    assert stats['winning_trades'] == 3
    assert stats['win_rate'] == 50.0


def test_spread_history_and_sd_touches(client):
    history = client.get('/api/spread-history?n=50').get_json()
    assert 'spreads' in history and 'zscores' in history
    touches = client.get('/api/sd-touches?asset=GOLD&limit=10').get_json()
    assert touches[0]['sd_level'] == 2


def test_shadow_summary_aggregates_at_five(client):
    data = client.get('/api/shadow-summary').get_json()
    assert data['count'] == 6
    assert data['active'] == 1
    assert data['revert_be_rate'] == 100.0


# ---------------------------------------------------------------------------
# Config round trip in the UI's own vocabulary
# ---------------------------------------------------------------------------

def test_config_get_speaks_w3_field_names(client):
    config = client.get('/api/config').get_json()
    assert config['entry_threshold'] == 3.0        # SIGNALS.ENTRY_Z
    assert config['hedge_ratio'] == 1.0
    assert config['spot_symbol'] == 'XAUUSD'
    assert config['futures_symbol'] == 'GC1225'
    assert config['paper_trading'] is True
    assert config['clip_lots'] == 50.0


def test_config_post_from_ui_maps_back_to_sections(client):
    response = client.post('/api/config', json={
        'entry_threshold': 2.4, 'exit_threshold': 0.4,
        'hard_max_hold_minutes': 90, 'max_loss_usd': 25,
        'exit_signal_mode': 'hybrid', 'entry_execution_mode': 'LIMIT',
        'clip_lots': 40, 'spot_symbol': 'XAUUSD_', 'asset': 'GOLD',
        'paper_trading': True})
    assert response.status_code == 200

    raw = json.loads((client.tmp_path / "config.json").read_text())
    assert raw['signals']['ENTRY_Z'] == 2.4
    assert raw['signals']['EXIT_Z'] == 0.4
    assert raw['signals']['EXIT_MODE'] == 'hybrid'
    assert raw['exits']['HARD_MAX_HOLD_MIN'] == 90
    assert raw['exits']['STOP_USD_PER_LOT'] == 25
    assert raw['execution']['ENTRY_STYLE'] == 'limit'
    assert raw['trading']['CLIP_LOTS'] == 40
    assert raw['assets']['GOLD']['spot_symbols'] == ['XAUUSD_']
    # Untouched keys survive
    assert raw['trading']['HEDGE_RATIO'] == 1.0


def test_beta_change_rejected_while_in_trade(client):
    response = client.post('/api/config', json={'hedge_ratio': 2.0})
    assert response.status_code == 409
    raw = json.loads((client.tmp_path / "config.json").read_text())
    assert raw['trading']['HEDGE_RATIO'] == 1.0


# ---------------------------------------------------------------------------
# Controls write the control file the coordinator reads
# ---------------------------------------------------------------------------

def test_algo_toggle_and_close_and_test(client):
    assert client.post('/api/engine/toggle-algo').status_code == 200
    control = json.loads((client.tmp_path / "control.json").read_text())
    assert control['algo_enabled'] is False

    assert client.post('/api/engine/close-position', json={}).status_code == 200
    control = json.loads((client.tmp_path / "control.json").read_text())
    assert control['close']['position_id'] == 'POS_0009'   # the open one

    assert client.post('/api/engine/test',
                       json={'kind': 'orders'}).status_code == 200
    control = json.loads((client.tmp_path / "control.json").read_text())
    assert control['test']['kind'] == 'orders'


def test_manual_spread_trade_endpoint(client):
    response = client.post('/api/engine/open', json={
        'asset': 'GOLD', 'direction': 'BUY_BASIS', 'lots': 10})
    assert response.status_code == 200
    control = json.loads((client.tmp_path / "control.json").read_text())
    assert control['open']['direction'] == 'BUY_BASIS'


# ---------------------------------------------------------------------------
# Brokers page (MT5 replacement for W3's exchanges)
# ---------------------------------------------------------------------------

def test_broker_list_add_and_leg_mapping(client):
    brokers = client.get('/api/exchanges').get_json()
    assert {b['id'] for b in brokers} == {'account_a', 'account_b'}
    assert [b for b in brokers if b['id'] == 'account_a'][0]['role'] == 'SPOT'

    response = client.post('/api/exchanges', json={
        'name': 'account_c', 'terminal_path': 'C:/C/terminal64.exe',
        'login': 333, 'server': 'Other-Demo', 'password': 'Sup3r!'})
    assert response.status_code == 200

    raw = json.loads((client.tmp_path / "config.json").read_text())
    assert raw['accounts']['account_c']['login'] == 333
    assert 'Sup3r!' not in (client.tmp_path / "config.json").read_text()
    assert 'MT5_PASSWORD_ACCOUNT_C="Sup3r!"' in \
        (client.tmp_path / ".env").read_text()

    assert client.post('/api/set-active-exchanges',
                       json={'futures_id': 'account_c'}).status_code == 200
    raw = json.loads((client.tmp_path / "config.json").read_text())
    assert raw['leg_accounts']['futures'] == 'account_c'


def test_telegram_secrets_go_to_env(client):
    response = client.post('/api/telegram/config', json={
        'telegram_enabled': True, 'telegram_bot_token': '123:ABC',
        'telegram_chat_id': '999'})
    assert response.status_code == 200
    env = (client.tmp_path / ".env").read_text()
    assert 'TELEGRAM_BOT_TOKEN="123:ABC"' in env
    assert 'TELEGRAM_CHAT_ID="999"' in env
    assert '123:ABC' not in (client.tmp_path / "config.json").read_text()
    # GET masks the token
    assert client.get('/api/telegram/config').get_json()[
        'telegram_bot_token'] == '***'


def test_the_settings_suite_table_gets_renderable_rows(client, tmp_path):
    """It rendered a table of 'undefined': the per-leg order check rows
    were handed straight to the vendored scenario table, which reads
    label / mode / order_type / status / detail."""
    (client.tmp_path / "runtime_status.json").write_text(json.dumps({
        'test_results': {'kind': 'orders', 'ts': '11:00:00', 'results': [
            {'leg': 'Uts', 'check': 'connection ping', 'ok': True,
             'detail': ''},
            {'leg': 'Uts', 'check': 'account info', 'ok': True,
             'detail': 'login 32020119 equity $50,019'},
            {'leg': 'Uts', 'check': 'market open XAUUSD_', 'ok': False,
             'detail': 'Unsupported filling mode'}]}}))
    state = client.get('/api/test-suite/status').get_json()
    rows = state['scenarios']
    assert [r['label'] for r in rows] == ['connection ping', 'account info',
                                          'market open XAUUSD_']
    assert [r['status'] for r in rows] == ['pass', 'pass', 'fail']
    assert all(r['order_type'] == 'Uts' for r in rows)
    assert all(r['id'] for r in rows)
    assert state['pass'] == 2 and state['fail'] == 1
    assert state['current'] == 3 and state['total'] == 3
    assert 'undefined' not in json.dumps(rows)
