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
from statarb import webapi                                  # noqa: E402
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
        # Decision-to-fill, on every trade but the first — so the
        # Execution Quality card is exercised with data AND the
        # "unmeasured" case stays represented in the same fixture.
        if i:
            position.entry_slippage = {
                'crossing_spread': 0.30, 'crossing_usd': 1500.0,
                'slippage_spread': 0.01, 'slippage_usd': 50.0}
            position.exit_slippage = {
                'crossing_spread': 0.30, 'crossing_usd': 1500.0,
                'slippage_spread': 0.02, 'slippage_usd': 100.0}
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
                    'spread': 1.5, 'spot_price': 3300.0,
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
    # Both sizing anchors are offered: lots, and W3's notional-per-leg
    # (restored 2026-08-07 at the owner's request — a lot is a different
    # amount of money on every instrument).
    assert 'clip_lots' in body
    assert 'position_size_usd' in body
    assert 'sizing_mode' in body


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


def test_symbols_are_not_editable_in_two_places(client):
    """They live on the Exchanges page next to the account that trades
    them; Settings shows them read-only so the two cannot disagree."""
    page = client.get('/settings').get_data(as_text=True)
    pair = page.split('Pair Selection', 1)[1].split('</div>\n                    </div>', 1)[0]
    for field in ('id="spot_symbol"', 'id="futures_symbol"'):
        block = pair.split(field, 1)[1].split('>', 1)[0]
        assert 'readonly' in block, f'{field} should be read-only'
    assert 'asset_quickpick' not in page       # dropdown removed with it
    assert 'change on Exchanges' in pair


def test_the_hedge_ratio_stays_editable_in_settings(client):
    """Beta is a strategy parameter, not a broker fact."""
    page = client.get('/settings').get_data(as_text=True)
    block = page.split('id="hedge_ratio"', 1)[1].split('>', 1)[0]
    assert 'readonly' not in block
    # (with a flat book — a beta change mid-trade is refused by design)
    status = json.loads((client.tmp_path / "runtime_status.json").read_text())
    status['positions'] = []
    (client.tmp_path / "runtime_status.json").write_text(json.dumps(status))
    assert client.post('/api/config', json={'hedge_ratio': 2.0}
                       ).status_code == 200
    raw = json.loads((client.tmp_path / "config.json").read_text())
    assert raw['trading']['HEDGE_RATIO'] == 2.0


# ---------------------------------------------------------------------------
# Rolling-window controls: seconds vs sample count
# ---------------------------------------------------------------------------
# Operator, 2026-08-06: "the lookback period setting seems incorrect and
# not working". The field was labelled "Number of ticks to collect
# before trading" (W3's meaning) but wrote SIGNALS.LOOKBACK_SEC, a
# window DURATION. MIN_SAMPLES — the value that actually gates trading,
# and what the warm-up bar counts — had no control at all.

def test_the_lookback_field_says_it_is_a_duration(client):
    page = client.get('/settings').get_data(as_text=True)
    assert 'Lookback Window (seconds)' in page
    assert 'Number of ticks to collect before trading' not in page


def test_minimum_samples_has_its_own_control(client):
    page = client.get('/settings').get_data(as_text=True)
    assert 'id="min_samples"' in page
    assert 'Minimum Samples' in page


def test_saving_minimum_samples_reaches_the_config(client):
    client.post('/api/config', json={'min_samples': 120})
    with open(client.tmp_path / 'config.json') as f:
        assert json.load(f)['signals']['MIN_SAMPLES'] == 120


def test_saving_the_lookback_window_reaches_the_config(client):
    client.post('/api/config', json={'lookback_period': 1800})
    with open(client.tmp_path / 'config.json') as f:
        assert json.load(f)['signals']['LOOKBACK_SEC'] == 1800


def test_the_warmup_bar_tracks_whichever_gate_is_binding(client):
    """"181 / 7,200" compared a sample count to a duration in seconds,
    so the bar could never fill. It now tracks the gate that is
    furthest from being met, and shows a plain percentage."""
    page = client.get('/').get_data(as_text=True)
    assert 'const percent = (needHistory || warming)' in page
    assert 'Math.min(quotePct, timePct)' in page


# ---------------------------------------------------------------------------
# Pair type + reference fair value
# ---------------------------------------------------------------------------

def test_the_pair_type_selector_offers_all_three_shapes(client):
    page = client.get('/settings').get_data(as_text=True)
    for value in ('SPOT_FUTURE', 'FUTURE_FUTURE', 'RELATED'):
        assert f'value="{value}"' in page
    assert 'Carry Rate (annual %)' in page


def test_saving_the_pair_type_reaches_the_asset_config(client):
    client.post('/api/config', json={'pair_type': 'FUTURE_FUTURE',
                                     'asset': 'GOLD'})
    with open(client.tmp_path / 'config.json') as f:
        assert json.load(f)['assets']['GOLD']['pair_type'] == 'FUTURE_FUTURE'


def test_the_carry_rate_is_stored_as_a_fraction(client):
    """The UI shows a percentage; the maths wants a rate."""
    client.post('/api/config', json={'carry_rate_pct': 4.25, 'asset': 'GOLD'})
    with open(client.tmp_path / 'config.json') as f:
        assert json.load(f)['assets']['GOLD']['risk_free_rate'] == 0.0425


def test_the_settings_page_says_fair_value_is_reference_only(client):
    page = client.get('/settings').get_data(as_text=True)
    assert 'never reads this' in page


def test_the_dashboard_marks_the_fair_value_reference_only(client):
    page = client.get('/').get_data(as_text=True)
    assert 'id="fair-value-row"' in page
    assert 'ref only' in page
    assert 'REFERENCE ONLY' in page


# ---------------------------------------------------------------------------
# The warm-up counter after warm-up
# ---------------------------------------------------------------------------
# Operator, 2026-08-06: "10,894 / 300 ... Why is it stuck at 300?"
# MIN_SAMPLES is a threshold to CLEAR, not a target to sit at. Once
# cleared the ratio reads like a broken denominator.

def test_the_window_count_is_gone(client):
    """Operator, 2026-08-07: "10,516 quotes in window - not require".

    It was a rolling occupancy, so it FELL whenever the market quietened
    and read as data being lost — it took three attempts to word and no
    decision ever depended on it. The readiness gates answer the
    question it was standing in for, against the thresholds that
    actually gate trading."""
    page = client.get('/').get_data(as_text=True)
    assert 'id="data-count-display"' not in page
    assert 'quotes to warm up' not in page
    assert 'quotes now in the' not in page
    assert 'id="readiness-gates"' in page          # what replaced it


def test_a_collapsed_sigma_is_named_instead_of_collecting_data(client):
    """Enough quotes but no z: saying "collecting data" while the
    counter sits past its target explains nothing."""
    page = client.get('/').get_data(as_text=True)
    assert 'No usable Z-score' in page
    assert 'sigma too small to trust a z' in page


def test_the_ui_payload_carries_the_degenerate_flag():
    status = {'assets': [{'asset': 'GOLD', 'z': None, 'spread': 9.13,
                          'samples': 10894, 'min_samples': 300,
                          'lookback': 7200, 'degenerate': True}]}
    ui = webapi.status_to_ui(status, {})
    assert ui['signal']['degenerate'] is True
    assert ui['signal']['data_ready'] is False
    assert ui['signal']['lookback'] == 300        # threshold
    assert ui['signal']['lookback_sec'] == 7200   # window duration


def test_the_fair_value_links_to_where_its_inputs_live(client):
    """"Give a link where this can be configured" — the carry rate and
    pair type are on Settings, not obvious from a number on a card."""
    page = client.get('/').get_data(as_text=True)
    assert '/settings#pair-selection' in page
    settings = client.get('/settings').get_data(as_text=True)
    assert 'id="pair-selection"' in settings


def test_the_fair_value_detail_names_the_setting(config):
    from datetime import datetime, timedelta
    from statarb import fairvalue
    asset = dict(config.ASSETS['GOLD'],
                 futures_expiry=datetime.now() + timedelta(days=140))
    _, detail = fairvalue.fair_spread(asset, 4269.73, 4328.80, 1.0)
    assert 'carry rate' in detail and 'Pair Selection' in detail


def test_the_window_suggestion_is_published_in_seconds():
    ui = webapi.status_to_ui({'assets': [{
        'asset': 'GOLD', 'z': 0.4, 'spread': 58.3, 'samples': 900,
        'min_samples': 300, 'lookback': 7200,
        'suggested_lookback_sec': 1980.0}]}, {})
    assert ui['signal']['suggested_lookback_sec'] == 1980.0
    assert ui['signal']['lookback_sec'] == 7200


def test_the_tile_no_longer_calls_the_window_a_tick_count(client):
    page = client.get('/').get_data(as_text=True)
    assert 'Suggested Window' in page
    assert 'Suggested Lookback' not in page
    assert "sl + ' pts'" not in page


def test_the_banner_no_longer_says_waiting_for_lookback_period(client):
    page = client.get('/').get_data(as_text=True)
    assert 'Waiting for lookback period' not in page
    assert 'Waiting for enough quotes' in page


def test_the_signal_reason_drops_the_ratio_once_it_is_cleared(client):
    """The same "10,894 / 300" problem lived in the signal-reason line
    too, worded as "ticks"."""
    page = client.get('/').get_data(as_text=True)
    assert 'if (_cachedDataPoints < _cachedLookback)' in page
    assert "' ticks (' + pct + '%)" not in page


# ---------------------------------------------------------------------------
# "take 120 minutes of data ... before going ahead"
# ---------------------------------------------------------------------------

def test_minimum_history_has_a_control(client):
    page = client.get('/settings').get_data(as_text=True)
    assert 'id="min_history_sec"' in page
    assert 'Minimum History (seconds)' in page
    assert '7200 = 120 minutes' in page


def test_saving_minimum_history_reaches_the_config(client):
    client.post('/api/config', json={'min_history_sec': 3600})
    with open(client.tmp_path / 'config.json') as f:
        assert json.load(f)['signals']['MIN_HISTORY_SEC'] == 3600


def test_the_default_is_two_hours(config):
    assert config.SIGNALS['MIN_HISTORY_SEC'] == 7200


def test_the_ui_payload_carries_both_warm_up_gates():
    ui = webapi.status_to_ui({'assets': [{
        'asset': 'GOLD', 'z': None, 'spread': 58.3, 'samples': 10894,
        'min_samples': 300, 'lookback': 7200,
        'history_sec': 1800.0, 'min_history_sec': 7200.0}]}, {})
    signal = ui['signal']
    assert signal['history_sec'] == 1800.0
    assert signal['min_history_sec'] == 7200.0
    assert signal['data_points'] == 10894      # count already cleared...
    assert signal['lookback'] == 300           # ...so time is what blocks


def test_the_banner_names_the_time_gate_when_it_is_the_one_blocking(client):
    page = client.get('/').get_data(as_text=True)
    assert 'const needHistory = history < minHistory' in page
    assert 'minutes collected' in page
    assert 'more before trading can start' in page


# ---------------------------------------------------------------------------
# Dialogs
# ---------------------------------------------------------------------------
# Operator, 2026-08-06: "Make all the Dialog Boxes Professional and also
# Everytime new settings are saved - a dialog box that confirms or gives
# an error."

TEMPLATE_FILES = ('base.html', 'dashboard.html', 'settings.html',
                  'setup.html', 'analysis.html')


def template_source(name):
    return open(os.path.join(TEMPLATES, name), encoding='utf-8').read()


def test_no_native_browser_dialogs_remain():
    """confirm()/alert()/prompt() draw the browser's own chrome, which
    cannot be styled and looks nothing like the rest of the app."""
    offenders = []
    for name in TEMPLATE_FILES:
        for call in ('confirm(', 'alert(', 'prompt('):
            for line in template_source(name).splitlines():
                stripped = line.strip()
                if stripped.startswith('//') or stripped.startswith('*'):
                    continue
                if call in line and 'showConfirm(' not in line \
                        and 'showAlert' not in line:
                    offenders.append(f'{name}: {stripped[:70]}')
    assert not offenders, offenders


def test_the_shared_dialog_is_on_every_page(client):
    for path in ('/', '/settings', '/setup', '/analysis'):
        page = client.get(path).get_data(as_text=True)
        assert 'id="appDialogModal"' in page, path
        assert 'function showConfirm' in page, path


def test_toasts_never_render_the_message_as_markup():
    """Toast text carries broker and server strings — parsing those as
    HTML would be an injection route straight from a trade comment."""
    source = template_source('base.html')
    body = source[source.index('function showToast'):
                  source.index('function showDialog')]
    code = '\n'.join(line for line in body.splitlines()
                     if not line.strip().startswith('//'))
    assert 'body.textContent = message' in code
    assert 'innerHTML' not in code


def test_error_toasts_do_not_disappear_on_their_own():
    """A failure that vanishes after three seconds is one the operator
    misses. Errors wait to be dismissed."""
    source = template_source('base.html')
    assert "danger:  {icon: 'bi-x-octagon-fill',    title: 'Error',     ms: 0}" \
        in source
    assert 'if (style.ms) setTimeout' in source


def test_destructive_confirmations_are_styled_as_destructive():
    dashboard = template_source('dashboard.html')
    for action in ('Close position', 'Reset everything', 'Delete trade record'):
        index = dashboard.index(action)
        assert "variant: 'danger'" in dashboard[index - 300:index + 300], action


# --- every save answers with a dialog ------------------------------------

def test_the_settings_save_reports_through_a_dialog(client):
    page = client.get('/settings').get_data(as_text=True)
    assert 'reportSave(response, data, ' in page
    assert 'showResult(false, ' in page          # network failure path
    assert 'function reportSave' in page


def test_the_save_dialog_carries_the_servers_note(client):
    """The engine's "hot-reloads within ~10s" and any restart-required
    notes must reach the operator, not just "saved"."""
    page = client.get('/settings').get_data(as_text=True)
    source = page[page.index('function reportSave'):]
    assert 'data.note' in source[:800]


def test_a_rejected_save_produces_an_error_dialog(client):
    """A beta change with a position open returns 409 — the operator
    must be told, not left thinking it applied."""
    with open(client.tmp_path / 'runtime_status.json', 'w') as f:
        json.dump({'positions': [{'position_id': 'POS_1'}]}, f)
    response = client.post('/api/config', json={'hedge_ratio': 2.5})
    assert response.status_code == 409
    assert 'rejected' in response.get_json()['error']


def test_a_successful_save_returns_a_note_to_show(client):
    response = client.post('/api/config', json={'min_samples': 400})
    assert response.status_code == 200
    assert response.get_json()['note']


def test_the_exchanges_save_reports_through_a_dialog(client):
    page = client.get('/setup').get_data(as_text=True)
    assert "showResult(true, 'Account saved.'" in page
    assert "showResult(false, 'The account was not saved.'" in page


# ---------------------------------------------------------------------------
# The Save button that silently did nothing (2026-08-06)
# ---------------------------------------------------------------------------
# Operator: "changes are made on the Settings Page and clicked Save - The
# settings are not getting saved - maybe when there is an error - but no
# error shows up". Driven in Chromium, the page threw a temporal dead
# zone ReferenceError partway through its script, so the submit handler
# was never registered: no request, no error, nothing.

def js_block(name, marker):
    """The <script> block of a template that contains `marker`."""
    source = template_source(name)
    end = source.index(marker)
    start = source.rindex('<script', 0, end)
    return source[start:source.index('</script>', end)]


def test_no_const_is_read_before_it_is_declared():
    """A `const` used above its declaration is a ReferenceError that
    kills the whole script block — and everything registered after it,
    including the Save handler."""
    block = js_block('settings.html', 'const instrumentCategories')
    for name in ('instrumentCategories', '_betaSuggestionTimer'):
        declaration = block.index(f'const {name}') if f'const {name}' in block \
            else block.index(f'let {name}')
        uses = [m for m in re.finditer(rf'\b{name}\b', block)]
        top_level_calls = block.index('    updatePairLabel();')
        assert top_level_calls > declaration, (
            f'{name} is read at start-up before it is declared')


def test_the_boot_calls_run_after_every_declaration():
    block = js_block('settings.html', 'const instrumentCategories')
    boot = block.index('    updatePairLabel();')
    for decl in ('const instrumentCategories', 'let _betaSuggestionTimer'):
        assert block.index(decl) < boot, decl


def test_number_inputs_accept_the_engines_own_defaults(client):
    """min/max/step that reject the shipped default make Chrome refuse
    to submit the form — silently, with the offending field usually
    scrolled out of view. That is how saving broke."""
    from statarb.config import AlgoTradingConfig
    from statarb import webapi
    page = client.get('/settings').get_data(as_text=True)
    defaults = webapi.to_ui_config({}, AlgoTradingConfig())
    problems = []
    for tag in re.findall(r'<input[^>]*type="number"[^>]*>', page):
        ident = re.search(r'id="([^"]+)"', tag)
        if not ident or ident.group(1) not in defaults:
            continue
        value = defaults[ident.group(1)]
        if not isinstance(value, (int, float)):
            continue
        for attr, ok in (('min', lambda v, a: v >= a),
                         ('max', lambda v, a: v <= a)):
            found = re.search(rf'{attr}="([-\d.]+)"', tag)
            if found and not ok(float(value), float(found.group(1))):
                problems.append(f'{ident.group(1)}={value} violates '
                                f'{attr}={found.group(1)}')
        step = re.search(r'step="([\d.]+)"', tag)
        if step:
            size = float(step.group(1))
            base = float((re.search(r'min="([-\d.]+)"', tag) or
                          [None, '0'])[1])
            offset = round((float(value) - base) / size, 6)
            if abs(offset - round(offset)) > 1e-6:
                problems.append(f'{ident.group(1)}={value} is not a '
                                f'multiple of step={size}')
    assert not problems, problems


def test_a_form_that_fails_validation_reports_it(client):
    """Belt and braces: even with the attributes right, a future
    mismatch must produce a dialog rather than silence."""
    page = client.get('/settings').get_data(as_text=True)
    assert 'if (!form.checkValidity())' in page
    assert 'need fixing before' in page
    assert 'scrollIntoView' in page


def test_the_pair_label_never_becomes_the_asset_key(client):
    """updatePairLabel() wrote a display label into #asset, so every
    save created a SECOND enabled asset next to the real one."""
    block = js_block('settings.html', 'function updatePairLabel')
    body = block[block.index('function updatePairLabel'):]
    body = body[:body.index('return label;')]
    assert "getElementById('asset').value = label" not in body
    assert "pair_label_display" in body


def test_a_blocked_cdn_cannot_disable_the_page(client):
    """`const socket = io()` threw when the Socket.IO CDN was
    unreachable, killing base.html's script — and with it showToast,
    showDialog and every save handler defined below."""
    page = client.get('/').get_data(as_text=True)
    assert "typeof io === 'function'" in page
    assert 'falling back to polling' in page
    # The helpers must be defined BEFORE anything that can throw.
    assert page.index('function showToast') < page.index('const socket =')


def test_dialogs_do_not_depend_on_bootstrap_javascript(client):
    """The dialog that reports "could not save" has to work when the
    network is the thing that failed."""
    page = client.get('/').get_data(as_text=True)
    source = page[page.index('function showDialog'):
                  page.index('function showConfirm')]
    code = '\n'.join(line for line in source.splitlines()
                     if not line.strip().startswith('//'))
    assert 'bootstrap.Modal' not in code
    assert "modalEl.classList.add('show')" in code


# ---------------------------------------------------------------------------
# Table striping contrast
# ---------------------------------------------------------------------------
# Operator, 2026-08-06: "In the Black bands the text is not readable".
# .table-dark ships near-black striped/hover colours, and Bootstrap 5.3
# paints them over the cell with an inset box-shadow — so overriding
# --bs-table-bg alone left black bands carrying dark text.

def test_dark_tables_bring_every_state_colour_into_the_light_theme(client):
    page = client.get('/').get_data(as_text=True)
    block = page[page.index('.table-dark {'):page.index('.table-dark th')]
    for variable in ('--bs-table-striped-bg', '--bs-table-striped-color',
                     '--bs-table-hover-bg', '--bs-table-hover-color',
                     '--bs-table-active-bg', '--bs-table-active-color'):
        assert variable in block, variable
    # ...and none of them may be left as a raw dark literal.
    assert '#212529' not in block and '#2c3034' not in block


def test_striped_rows_set_the_hooks_bootstrap_actually_reads(client):
    """background-color alone is painted over by the inset box-shadow."""
    page = client.get('/').get_data(as_text=True)
    start = page.index('.table-striped > tbody > tr:nth-of-type(odd) > * {')
    block = page[start:page.index('}', start)]
    assert '--bs-table-bg-type' in block
    assert '--bs-table-color-type' in block


def test_the_scenario_table_is_striped_and_therefore_covered(client):
    """The table in the screenshot: setup.html's order-test suite."""
    page = client.get('/setup').get_data(as_text=True)
    assert 'table-dark table-striped' in page


# ---------------------------------------------------------------------------
# The cost / sizing cards that showed only dashes
# ---------------------------------------------------------------------------
# Operator, 2026-08-07: "Why are the values in these cards not showing
# any values?" The engine computes all of it on every tick inside the
# edge filter — status_to_ui simply never published it.

def status_with_costs():
    return {'assets': [{
        'asset': 'GOLD', 'z': 2.0, 'sigma': 0.063, 'mu': 58.8,
        'spread': 58.6, 'basis': 58.6, 'samples': 900, 'min_samples': 300,
        'spot_price': 4258.0, 'futures_price': 4316.6,
        'clip_lots': 0.1, 'contract_size': 100,
        'spot_notional': 42580.0, 'fut_notional': 43166.0,
        'rt_cost_usd': 5.9, 'rt_fees_usd': 0.0, 'rt_cost_bps': 1.39,
        'rt_fees_bps': 0.0, 'capture_usd': 0.945,
        'edge_ratio': 0.16, 'edge_required': 1.5, 'order_mode': 'MARKET',
    }]}


def test_the_edge_ratio_reaches_the_filters_card():
    signal = webapi.status_to_ui(status_with_costs(), {})['signal']
    assert signal['std_ratio'] == 0.16
    assert signal['std_ratio_required'] == 1.5


def test_the_round_trip_cost_reaches_the_filters_card():
    signal = webapi.status_to_ui(status_with_costs(), {})['signal']
    assert signal['round_trip_cost_bps'] == 1.39
    assert signal['round_trip_fees_bps'] == 0.0
    assert signal['order_mode'] == 'MARKET'


def test_the_notionals_reach_the_position_sizing_card():
    signal = webapi.status_to_ui(status_with_costs(), {})['signal']
    assert signal['leg_a_notional'] == 42580.0
    assert signal['leg_b_notional'] == 43166.0
    assert signal['clip_lots'] == 0.1


def test_the_dashboard_prefers_the_engines_notional(client):
    """W3 sized in configured USD; this engine sizes in LOTS, so the
    card was reading a number that is never set."""
    page = client.get('/').get_data(as_text=True)
    assert '_engineLegANotional' in page
    assert 'data.leg_a_notional' in page


def test_the_cost_block_is_absent_when_the_engine_has_not_published_it():
    """No invented zeros — the card shows dashes until there is data."""
    signal = webapi.status_to_ui({'assets': [{'asset': 'GOLD', 'z': 1.0}]},
                                 {})['signal']
    assert signal['std_ratio'] is None
    assert signal['round_trip_cost_bps'] is None
    assert signal['leg_a_notional'] is None


def test_the_feed_rate_took_the_counter_s_place(client):
    """A feed going thin is what collapses sigma, so the RATE is worth
    a tile even though the raw count was not."""
    page = client.get('/').get_data(as_text=True)
    assert 'id="data-count-rate"' in page
    assert "rateLabel.textContent = 'quotes/min'" in page
    assert 'quoteRate < 6' in page                  # the amber threshold
    assert 'collapses sigma' in page


def test_the_progress_bar_shows_a_percentage_not_a_running_total(client):
    """The gate NUMBERS live on the readiness line, each against the
    threshold it is judged by. A second running total up here only
    duplicated or contradicted it."""
    page = client.get('/').get_data(as_text=True)
    assert "? Math.round(percent) + '%' : ''" in page
    assert 'const heldSec' not in page            # the pinned ratio, gone


def test_the_engine_publishes_its_measured_refresh_rate():
    ui = webapi.status_to_ui({'write_interval_ms': 302,
                              'poll_interval_sec': 0.3,
                              'assets': [{'asset': 'GOLD', 'z': 1.0}]}, {})
    assert ui['write_interval_ms'] == 302
    assert ui['poll_interval_sec'] == 0.3


# ---------------------------------------------------------------------------
# Statistics & Regime card
# ---------------------------------------------------------------------------
# Operator, 2026-08-07: "The values are not updated in this card" —
# Mean 0.00 and Std Dev 0.00 against a live mu of 58.8 and sigma 0.063.
# The card reads data.spread_mean / data.spread_std at the TOP level;
# status_to_ui only put mean/std inside the signal block.

def stats_status():
    return {'assets': [{'asset': 'GOLD', 'z': 1.9, 'mu': 58.7985,
                        'sigma': 0.0631, 'half_life_min': 12.5,
                        'trend_slope': -0.0001, 'regime': 'MEAN_REVERTING',
                        'samples': 14160, 'min_samples': 300}]}


def test_the_mean_and_sigma_reach_the_statistics_card():
    ui = webapi.status_to_ui(stats_status(), {})
    assert ui['spread_mean'] == 58.7985
    assert ui['spread_std'] == 0.0631


def test_the_regime_comes_from_the_ar1_fit():
    assert webapi.status_to_ui(stats_status(), {})['regime'] == 'MEAN_REVERTING'


def test_hurst_is_published_as_absent_not_as_a_half():
    """0.5000 reads as "measured a random walk". We do not compute it."""
    ui = webapi.status_to_ui(stats_status(), {})
    assert ui['hurst'] is None and ui['hurst_ok'] is None


def test_the_card_shows_a_dash_rather_than_a_fabricated_hurst(client):
    page = client.get('/').get_data(as_text=True)
    assert 'const hurst = data.hurst || 0.5' not in page
    assert 'Not computed by this engine' in page


def test_sigma_is_shown_to_four_decimals(client):
    """0.0631 rounds to "0.06" at two decimals, which hides the whole
    sigma-versus-cost question."""
    page = client.get('/').get_data(as_text=True)
    assert 'data.spread_std.toFixed(4)' in page


def test_the_half_life_is_labelled_in_minutes(client):
    page = client.get('/').get_data(as_text=True)
    assert 'Half-Life (minutes)' in page
    assert 'Half-Life (periods)' not in page


# ---------------------------------------------------------------------------
# Margin Details with a flat book
# ---------------------------------------------------------------------------
# Operator, 2026-08-07: "This is still blank". IMR/MMR/Margin Ratio and
# the liquidation prices ARE per-position and correctly blank while
# flat — the card says so. But "Capital req" is knowable with no
# position, and it answers the question that actually matters before a
# trade: can this account afford the configured clip?

def account_status():
    return {'mode': 'PAPER',
            'accounts': {'acct': {'account': 'acct', 'login': 1,
                                  'equity': 49908.78, 'balance': 49908.78,
                                  'margin': 0.0, 'margin_free': 49908.78,
                                  'profit': 0.0}},
            'assets': [{'asset': 'GOLD', 'clip_lots': 0.1,
                        'capital_required': 8574.6,
                        'capital_buffer_pct': 0.0}]}


def test_the_capital_requirement_shows_while_flat(client):
    with open(client.tmp_path / 'runtime_status.json', 'w') as f:
        json.dump(account_status(), f)
    with open(client.tmp_path / 'config.json', 'w') as f:
        json.dump({'accounts': {'acct': {'login': 1}}}, f)
    body = client.get('/api/account-info').get_json()
    assert body['capital_required'] == 8574.6
    assert body['capital_buffer_pct'] == 0.0
    assert body['clip_lots'] == 0.1


def test_the_capital_requirement_is_absent_when_unknown(client):
    with open(client.tmp_path / 'runtime_status.json', 'w') as f:
        json.dump({'accounts': {}}, f)
    body = client.get('/api/account-info').get_json()
    assert body['capital_required'] is None      # hides its row, no fake zero


def test_the_engine_publishes_per_leg_margin_and_the_buffer(tmp_path,
                                                            monkeypatch,
                                                            config):
    monkeypatch.chdir(tmp_path)
    from statarb.coordinator import Coordinator
    from statarb.spread import SpreadStats
    coord = Coordinator(config, trading_mode='PAPER')
    config.TRADING.update({'CLIP_LOTS': 0.1, 'HEDGE_RATIO': 1.0})
    config.EXITS.update({'SPOT_LEVERAGE': 100.0, 'FUT_LEVERAGE': 500.0,
                         'M2M_BUFFER_PCT': 10.0})
    md = {'spot_price': 4258.0, 'futures_price': 4316.6,
          'spot_bid': 4257.9, 'spot_ask': 4258.1,
          'futures_bid': 4316.4, 'futures_ask': 4316.8}
    block = coord._sizing_and_cost('GOLD', md, SpreadStats(config.SIGNALS))
    # Each leg divided by ITS OWN leverage, then the M2M buffer.
    assert block['spot_margin'] == pytest.approx(4258.0 * 10 / 100)
    assert block['fut_margin'] == pytest.approx(4316.6 * 10 / 500)
    assert block['capital_required'] == pytest.approx(
        (block['spot_margin'] + block['fut_margin']) * 1.10)
    assert block['capital_buffer_pct'] == 10.0


def test_the_statistics_card_reads_its_values_from_the_signal_block():
    """updateSignal() is handed status['signal'], so anything the
    Statistics & Regime card reads has to live THERE. Publishing at the
    top level only is why Mean/Std stayed blank while Regime worked."""
    ui = webapi.status_to_ui(stats_status(), {})
    signal = ui['signal']
    assert signal['spread_mean'] == 58.7985
    assert signal['spread_std'] == 0.0631
    assert signal['regime'] == 'MEAN_REVERTING'
    assert signal['half_life'] == 12.5


def test_the_card_updater_is_fed_the_signal_block(client):
    """Guards the assumption above: if this ever changes, the keys need
    to move with it."""
    page = client.get('/').get_data(as_text=True)
    assert 'updateSignal(data.signal)' in page


# ---------------------------------------------------------------------------
# Position Sizing is in LOTS, not dollars
# ---------------------------------------------------------------------------
# Operator, 2026-08-07: could not find CLIP_LOTS / SLICE_LOTS /
# MAX_LOT_SIZE. Two of them were on the page all along, labelled
# "Position Size (USD)" and "Max Position Size (USD)" — the same
# units mislabel as "Lookback Period". The third was not there at all.

def test_the_sizing_fields_are_labelled_in_lots(client):
    page = client.get('/settings').get_data(as_text=True)
    assert 'Clip Size (LOTS per leg)' in page
    assert 'Max Position Size (LOTS)' in page
    assert 'Position Size (USD)' not in page
    assert 'Max Position Size (USD)' not in page


def test_the_slice_size_has_a_control(client):
    """SLICE_LOTS gates whether a child order clears each leg's minimum,
    and it had no field anywhere."""
    page = client.get('/settings').get_data(as_text=True)
    assert 'id="slice_lots"' in page
    assert 'Slice Size (LOTS per child order)' in page


def test_saving_the_slice_size_reaches_the_config(client):
    client.post('/api/config', json={'slice_lots': 0.1})
    with open(client.tmp_path / 'config.json') as f:
        assert json.load(f)['trading']['SLICE_LOTS'] == 0.1


def test_saving_the_clip_and_cap_reach_the_config(client):
    client.post('/api/config', json={'clip_lots': 0.1, 'max_lot_size': 0.5})
    with open(client.tmp_path / 'config.json') as f:
        raw = json.load(f)
    assert raw['trading']['CLIP_LOTS'] == 0.1
    assert raw['risk_limits']['MAX_LOT_SIZE'] == 0.5


def test_the_capital_preview_converts_lots_to_notional(client):
    """It read the lot count as dollars — "$100 required" for a clip
    whose real notional is $42m."""
    page = client.get('/settings').get_data(as_text=True)
    assert 'ps * contract * _cachedLegAPrice' in page
    # Leg B now comes from BOTH contract sizes, not from beta alone.
    assert 'lotsB * contractB * _cachedLegBPrice' in page
    assert 'lots ×' in page          # the breakdown states the conversion


# --- Manual Spread Trade: entry / take profit / stop loss ------------------

def test_the_manual_panel_has_all_three_levels(client):
    """Owner: "The Manual Trade should be Entry - Take Profit and Stop
    loss". It shipped with Entry and a single unlabelled "Exit"."""
    page = client.get('/').get_data(as_text=True)
    assert 'id="manual-entry-spread"' in page
    assert 'id="manual-exit-spread"' in page
    assert 'id="manual-stop-spread"' in page
    assert 'Take Profit' in page
    assert 'Stop Loss' in page


def test_the_manual_panel_says_it_ignores_the_algo_switch(client):
    """Operator: "The Algo was turned off still the trade took place."
    It is by design — _check_manual_arm runs before the algo_enabled
    entry gate — but the panel never said so."""
    page = client.get('/').get_data(as_text=True)
    assert 'ignores the Start/Stop switch' in page
    assert 'even while the algo is stopped' in page


def test_the_armed_footer_says_when_the_trigger_is_already_reached(client):
    """"Waiting for spread 59.0000" while the spread was already past
    59 read as if nothing would happen — it fired on the next tick."""
    page = client.get('/').get_data(as_text=True)
    assert 'Trigger reached' in page
    assert 'opens on the next tick' in page


def test_the_manual_stop_level_reaches_the_engine(client):
    """The whole point of a Stop Loss field is that it travels with
    the order, so check the control file, not just the form."""
    r = client.post('/api/manual-trade', json={
        'asset': 'GOLD', 'direction': 'SELL_BASIS',
        'entry_spread': 59.0, 'exit_spread': 58.5, 'stop_spread': 60.0})
    assert r.get_json()['success'] is True
    with open(client.tmp_path / 'control.json') as f:
        order = json.load(f)['open']
    assert order['stop_spread'] == 60.0
    assert order['exit_spread'] == 58.5


def test_the_api_refuses_an_upside_down_stop(client):
    """A stop on the winning side fires the instant the trade goes
    right. The browser checks it too, but the browser can be bypassed."""
    r = client.post('/api/manual-trade', json={
        'asset': 'GOLD', 'direction': 'SELL_BASIS',
        'entry_spread': 59.0, 'stop_spread': 58.0})
    assert r.status_code == 400
    assert 'Stop loss' in r.get_json()['error']
    assert not (client.tmp_path / 'control.json').exists()


def test_the_api_refuses_an_upside_down_target(client):
    r = client.post('/api/manual-trade', json={
        'asset': 'GOLD', 'direction': 'BUY_BASIS',
        'entry_spread': 59.0, 'exit_spread': 58.0})
    assert r.status_code == 400
    assert 'Take profit' in r.get_json()['error']


def test_the_browser_and_the_server_share_one_level_rule(client):
    """Two copies of this rule would let a trade be refused in one
    place and accepted in the other."""
    page = client.get('/').get_data(as_text=True)
    assert 'function manualLevelError' in page
    assert 'is on the winning side of entry' in page      # same wording
    assert 'is on the losing side of entry' in page


def test_an_engine_refusal_reaches_the_panel(client):
    """Every _manual_open rejection used to end in logging.warning."""
    page = client.get('/').get_data(as_text=True)
    assert 'Last manual trade:' in page
    r = client.get('/api/manual-trade')
    assert 'note' in r.get_json()


# --- the readiness ratio the owner asked to keep ---------------------------

def test_the_readiness_gates_show_their_thresholds(client):
    """Owner: "the 7200/7200 or 120/120 is helpful to understand if you
    have enough data that has been used to calculate the mean and SD
    and is ready to trade". Kept on their OWN line, self-labelled, so
    they never mix spans with the live window count above."""
    page = client.get('/').get_data(as_text=True)
    assert 'id="readiness-gates"' in page
    assert 'Ready to trade:' in page
    assert "Math.floor(Math.min(history, minHistory))" in page   # capped
    assert "Math.min(dataPoints, minSamples)" in page            # capped
    assert "'s history'" in page


# --- a blocked CDN must not disable a page ---------------------------------

def test_a_blocked_chart_cdn_cannot_disable_the_dashboard(client):
    """`const zscoreChart = new Chart(...)` sat at the TOP LEVEL of the
    dashboard's script block. With the Chart.js CDN unreachable it threw
    a ReferenceError that aborted the whole block — killing the prices,
    the position card and the manual trade panel with it. Found by
    driving the page in Chromium behind a proxy that blocks the CDN."""
    page = client.get('/').get_data(as_text=True)
    assert 'function makeChart' in page
    assert "typeof Chart === 'undefined'" in page
    assert 'const zscoreChart = new Chart' not in page
    assert 'const spreadChart = new Chart' not in page


def test_the_analysis_chart_is_guarded_too(client):
    page = client.get('/analysis').get_data(as_text=True)
    assert "typeof Chart === 'undefined'" in page
    assert 'const sdTouchChart = new Chart' not in page


# --- slippage on the pages (2026-08-07) -----------------------------------

def test_the_position_card_shows_all_three_prices(client):
    """Owner: "what your signal wanted to enter at and what the orders
    got placed at on MT5" — visible while the trade is still open, not
    only in the post-mortem."""
    page = client.get('/').get_data(as_text=True)
    assert 'id="position-slippage-row"' in page
    assert 'wanted <span' in page and 'quoted <span' in page
    assert 'crossing ' in page and 'slippage ' in page


def test_the_card_keeps_crossing_and_slippage_apart(client):
    """One combined figure would make a wide spread look like bad
    execution and a fast market look like a wide spread."""
    page = client.get('/').get_data(as_text=True)
    tip = page.split('id="position-slippage-row"', 1)[1][:1200]
    assert 'CROSSING is mid-to-touch' in tip
    assert 'SLIPPAGE is touch-to-fill' in tip
    assert 'price improvement' in tip


def test_the_analysis_page_has_the_execution_quality_card(client):
    page = client.get('/analysis').get_data(as_text=True)
    assert 'Execution Quality' in page
    assert 'Avg slippage / fill' in page
    assert 'Entry / exit slip' in page
    assert 'Avg crossing (spread)' in page
    assert 'Modelled ÷ realised cost' in page


def test_the_tiles_carry_the_measured_numbers(client):
    """The seeded book has 5 measured trades of 6, so the counts must
    read per FILL (10) and exclude the unmeasured one entirely."""
    page = client.get('/analysis').get_data(as_text=True)
    assert '10 fills measured' in page
    # entry $50 / exit $100 on each measured trade (the template puts
    # the two on separate lines, so compare on collapsed whitespace).
    flat = ' '.join(page.split())
    assert '+50.00 / +100.00' in flat
    assert '+75.00' in flat                  # the per-fill average
    assert '0 of 10 improved on the quote' in flat


def test_an_empty_book_explains_itself_rather_than_showing_zeros(tmp_path):
    """With nothing measured the card must say so, not show a row of
    zeros that reads as flawless execution."""
    from statarb.database import DataLogger
    from statarb.webapp import create_app
    db = str(tmp_path / 'empty.db')
    DataLogger(db_path=db)
    (tmp_path / 'status.json').write_text('{}')
    app = create_app(db_path=db,
                     status_path=str(tmp_path / 'status.json'),
                     control_path=str(tmp_path / 'control.json'),
                     config_path=str(tmp_path / 'nope.json'))
    page = app.test_client().get('/analysis').get_data(as_text=True)
    assert 'No fills measured yet' in page
    assert 'rather than counted as zero' in page
    assert 'Avg slippage / fill' not in page


def test_the_journal_has_a_slippage_column(client):
    page = client.get('/analysis').get_data(as_text=True)
    assert '>Slip $</th>' in page
    assert 'Not measured — this trade closed before slippage tracking' in page
    # Blank, never a zero, for trades that predate the measurement.
    assert "slip == null ? '—'" in page


# --- notional sizing (2026-08-07, owner: the W3 model) --------------------

def test_the_operator_can_save_a_leg_notional(client):
    """Owner: "User fixes the notional value of the leg and the lots
    are calculated by itself... The User saves the leg Notional Value
    in the Settings page"."""
    page = client.get('/settings').get_data(as_text=True)
    assert 'Leg Notional Value' in page
    assert 'id="position_size_usd"' in page
    assert 'id="sizing_mode"' in page
    r = client.post('/api/config', json={'sizing_mode': 'notional',
                                         'position_size_usd': 2_500_000})
    assert r.get_json()['success'] is True
    with open(client.tmp_path / 'config.json') as f:
        trading = json.load(f)['trading']
    assert trading['SIZING_MODE'] == 'notional'
    assert trading['NOTIONAL_PER_LEG_USD'] == 2_500_000


def test_neither_sizing_control_is_ever_hidden(client):
    """Hiding the inactive field meant an operator who wanted to change
    the clip could not find it at all (operator, 2026-08-07: "Cannot
    change clip sizing"). The mode decides which value the engine uses,
    not which one exists."""
    page = client.get('/settings').get_data(as_text=True)
    assert 'function updateSizingMode' in page
    assert "style.display =\n            mode === 'notional'" not in page
    # Both are badged instead, so which one wins is still unambiguous.
    assert 'id="clip_badge"' in page and 'id="notional_badge"' in page
    assert "'IN USE' : 'not in use'" in page


def test_the_settings_preview_derives_lots_from_the_notional(client):
    page = client.get('/settings').get_data(as_text=True)
    assert 'function sizingLegALots' in page
    assert 'target / (contract * _cachedLegAPrice)' in page


def test_the_dashboard_states_the_lots_and_the_balance(client):
    """"Balanced" is the point of pairing two instruments, and lot
    rounding makes exact balance impossible — so state the residual."""
    page = client.get('/').get_data(as_text=True)
    assert 'id="sizing-lots"' in page
    assert 'id="sizing-balance"' in page
    assert 'Math.abs(pct) <= 2' in page          # the tolerance band
    assert 'more on A' in page and 'more on B' in page


def test_the_dashboard_explains_how_the_hedge_is_derived(client):
    page = client.get('/').get_data(as_text=True)
    assert 'L_B = L_A × C_A ÷ (β × C_B)' in page


def test_configured_leverage_reaches_the_margin_figures(client):
    """The symbol test inherited from W3 counted hyphens, so no MT5
    symbol ever registered as levered and both legs were rendered as
    unlevered cash — quoting the full notional as the requirement."""
    page = client.get('/').get_data(as_text=True)
    assert '(symbol.match(/-/g) || []).length >= 2' not in page
    assert '_engineSizing.leg_a_margin_usd' in page
    settings = client.get('/settings').get_data(as_text=True)
    assert '(symbol.match(/-/g) || []).length >= 2' not in settings


def test_the_lot_cap_is_related_to_the_notional(client):
    """The cap is in LOTS while the size is in dollars. Without the
    conversion a cap that blocks every entry looks like nothing."""
    page = client.get('/settings').get_data(as_text=True)
    assert 'id="max_lot_hint"' in page
    assert 'ABOVE this' in page and 'every entry would be refused' in page


def test_the_card_takes_leverage_from_the_engine(client):
    """It read a value Jinja baked in at page load, so a Settings
    change never reached it (operator: "not be 100x always")."""
    page = client.get('/').get_data(as_text=True)
    assert '_engineSizing.leg_a_leverage' in page
    assert '_engineSizing.leg_b_leverage' in page


def test_the_card_states_the_target_beside_the_achieved_notional(client):
    """Operator: "Why is Leg A notional being calculated incorrectly" —
    $20,000 asked for, $17,170 shown, and nothing on screen connected
    the two. One 0.01 gold lot is $4,293, so a target that small cannot
    be hit exactly whatever the rounding rule."""
    page = client.get('/').get_data(as_text=True)
    assert 'Asked for ' in page
    assert 'the nearest tradable lot gives' in page
    # The figure beside it is one volume STEP, not one lot: step x
    # contract x price. Calling it "one lot" understated it a
    # hundredfold wherever the step is 0.01 — on oil it read $798.95
    # against a real lot of $79,895.
    assert "' step = '" in page and "'-lot'" in page
    assert 'one lot = ' not in page


# --- log volume (operator, 2026-08-07: "Too many messages") ---------------

def _access(msg):
    import logging as _l
    return _l.LogRecord('werkzeug', _l.INFO, '', 0, msg, (), None)


def test_successful_polls_are_kept_out_of_the_log():
    """The dashboard polls half a dozen endpoints continuously, so
    werkzeug wrote about five 200 lines a second and buried the
    engine's own warnings."""
    from statarb.webapp import _QuietAccessLog
    f = _QuietAccessLog()
    for code in ('200', '204', '304'):
        line = f'127.0.0.1 - - [d] "GET /api/engine/status HTTP/1.1" {code} -'
        assert f.filter(_access(line)) is False, code


def test_failed_requests_still_reach_the_log():
    """A request that FAILED is exactly what someone reading the log
    is looking for."""
    from statarb.webapp import _QuietAccessLog
    f = _QuietAccessLog()
    for code in ('400', '404', '409', '500'):
        line = f'127.0.0.1 - - [d] "POST /api/config HTTP/1.1" {code} -'
        assert f.filter(_access(line)) is True, code


def test_non_request_lines_are_untouched():
    from statarb.webapp import _QuietAccessLog
    f = _QuietAccessLog()
    assert f.filter(_access(' * Running on http://127.0.0.1:8080')) is True


def test_the_filter_is_installed_when_the_server_starts(monkeypatch):
    """It has to be on the path start.py actually uses."""
    import logging
    from statarb import webapp
    logger = logging.getLogger('werkzeug')
    before = len(logger.filters)
    served = {}
    app = type('A', (), {'socketio': None,
                         'run': lambda self, **kw: served.update(kw)})()
    webapp.run_app(app, host='127.0.0.1', port=1)
    try:
        assert len(logger.filters) == before + 1
        assert served['port'] == 1
    finally:
        logger.filters = logger.filters[:before]


def test_the_stub_endpoint_is_not_polled_every_second(client):
    """/api/active-orders returns a constant empty list — 86,400
    requests a day for nothing, and the loudest line in the log."""
    page = client.get('/').get_data(as_text=True)
    assert 'setInterval(updateActiveOrders, 1000)' not in page
    assert 'setInterval(updateActiveOrders, 15000)' in page


# --- the round-trip cost explains itself (2026-08-07) ---------------------

def test_the_round_trip_row_no_longer_describes_w3s_model(client):
    """The tooltip said "Entry (spot+fut) + Exit (spot+fut) + slippage
    x 4 legs". This engine charges each leg its bid-ask ONCE across the
    round trip, and carries no slippage term in the estimate at all."""
    page = client.get('/').get_data(as_text=True)
    assert 'slippage × 4 legs' not in page
    assert 'Four executions, two spreads' in page


def test_the_round_trip_tooltip_shows_the_derivation(client):
    page = client.get('/').get_data(as_text=True)
    for piece in ('Leg A ask − bid', 'Leg B ask − bid',
                  'combined bid-ask', 'spread cost factor',
                  'As bps of leg A notional', 'Size cancels out'):
        assert piece in page, piece


def test_a_zero_commission_is_called_out(client):
    """COMMISSION_PER_LOT_* ship at 0.0 and must be set before LIVE —
    an under-stated cost model lets through trades that cannot pay."""
    page = client.get('/').get_data(as_text=True)
    assert 'COMMISSION_PER_LOT is 0; set it' in page


def test_the_cost_inputs_are_published():
    """The card cannot show a derivation it has not been given."""
    ui = webapi.status_to_ui({'assets': [{
        'asset': 'GOLD', 'z': 1.0, 'rt_cost_bps': 1.09,
        'rt_spot_spread': 0.13, 'rt_fut_spread': 0.34,
        'rt_spread_factor': 1.0, 'rt_commission_per_lot': 0.0,
        'rt_units': 5.0, 'rt_cost_usd': 2.35,
        'spot_notional': 21463.07}]}, {})
    sig = ui['signal']
    assert sig['rt_spot_spread'] == 0.13
    assert sig['rt_fut_spread'] == 0.34
    assert sig['rt_leg_a_notional'] == pytest.approx(21463.07)
    assert sig['round_trip_cost_usd'] == pytest.approx(2.35)


def test_the_published_bps_matches_the_stated_arithmetic(tmp_path,
                                                         monkeypatch,
                                                         config):
    """bps = combined bid-ask ÷ leg A price × 10,000, size-independent —
    which is what the tooltip claims."""
    monkeypatch.chdir(tmp_path)
    from statarb.coordinator import Coordinator, PaperExecutor

    class Leg:
        name = 'b'

        def ensure_symbol(self, s):
            return {'ok': True, 'volume_step': 0.01, 'volume_min': 0.01,
                    'volume_max': 100.0, 'point': 0.01}

        def tick(self, s):
            return None

        def account_info(self):
            return {}

        def order_log(self, hours=24):
            return []

        def ping(self):
            return True

    config.TRADING.update({'SIZING_MODE': 'lots', 'CLIP_LOTS': 0.05,
                           'HEDGE_RATIO': 1.0})
    config.COSTS['SPREAD_COST_FACTOR'] = 1.0
    coord = Coordinator(config, trading_mode='PAPER')
    coord.spot_leg = coord.futures_leg = Leg()
    coord.executor = PaperExecutor(coord.spot_leg, coord.futures_leg, config)
    coord.active_assets['GOLD'] = {'config': config.ASSETS['GOLD'],
                                   'spot_symbol': 'XAUUSD_',
                                   'futures_symbol': 'GC1226',
                                   'last_data': None}
    md = {'spot_price': 4292.615, 'futures_price': 4351.55,
          'spot_bid': 4292.55, 'spot_ask': 4292.68,
          'futures_bid': 4351.38, 'futures_ask': 4351.72,
          'spread': 58.94, 'basis_pct': 1.37}
    block = coord._sizing_and_cost('GOLD', md, None)
    combined = 0.13 + 0.34
    assert block['rt_cost_bps'] == pytest.approx(
        combined / 4292.615 * 1e4, abs=0.01)
    # Size-independent: ten times the clip, same bps.
    config.TRADING['CLIP_LOTS'] = 0.5
    assert coord._sizing_and_cost('GOLD', md, None)['rt_cost_bps'] == \
        pytest.approx(block['rt_cost_bps'], abs=0.01)


def test_the_round_trip_is_published_per_lot_and_scaled(config, tmp_path,
                                                        monkeypatch):
    """The operator reasons about ONE lot ("a round trip costs me $94")
    and then scales it. A single figure at 6.41 lots has to be divided
    before it means anything, and cannot be compared against a sigma."""
    monkeypatch.chdir(tmp_path)
    from statarb.coordinator import Coordinator, PaperExecutor

    class Leg:
        name = 'acct'

        def ensure_symbol(self, symbol):
            return {'ok': True, 'volume_min': 0.01, 'volume_step': 0.01,
                    'volume_max': 100.0}

    config.ASSETS = {'GOLD': dict(config.ASSETS['GOLD'], lot_size=1000.0,
                                  fut_lot_size=1000.0)}
    config.TRADING.update({'SIZING_MODE': 'lots', 'CLIP_LOTS': 6.41,
                           'HEDGE_RATIO': 1.0})
    config.COSTS.update({'COMMISSION_PER_LOT_SPOT': 15.0,
                         'COMMISSION_PER_LOT_FUT': 15.0})
    coord = Coordinator(config, trading_mode='PAPER')
    coord.spot_leg = coord.futures_leg = Leg()
    coord.executor = PaperExecutor(coord.spot_leg, coord.futures_leg, config)
    coord.active_assets['GOLD'] = {'config': config.ASSETS['GOLD'],
                                   'spot_symbol': 'USOIL_U6',
                                   'futures_symbol': 'UKOIL_V6',
                                   'last_data': None}
    md = {'spot_price': 77.96, 'futures_price': 83.455,
          'spot_bid': 77.944, 'spot_ask': 77.976,
          'futures_bid': 83.439, 'futures_ask': 83.471,
          'spread': 5.495, 'basis_pct': 7.05}
    block = coord._sizing_and_cost('GOLD', md, None)

    # (0.032 + 0.032) x 1000 bbl + $30 commission
    assert block['rt_cost_per_lot'] == pytest.approx(94.0, abs=0.01)
    assert block['rt_contract_size'] == 1000.0
    assert block['rt_lots'] == pytest.approx(6.41)
    # ...and the position total is exactly the per-lot figure scaled
    assert block['rt_cost_usd'] == pytest.approx(
        block['rt_cost_per_lot'] * block['rt_lots'], rel=1e-9)


def test_the_edge_badge_verdict_reaches_the_signal_block():
    """The Filters card's Edge badge reads signal.std_filter_ok, and
    NOTHING ever published it — so it sat at "-" while the edge table
    directly beneath it spelled out the whole shortfall.

    It has to live in the `signal` block specifically: the dashboard
    calls updateSignal(d.signal), so a field published one level up is
    invisible to every control on that card.
    """
    from statarb import webapi

    status = {'assets': [{'asset': 'GOLD', 'z': 0.24, 'edge_ok': False,
                          'edge_ratio': 0.09, 'edge_required': 1.5}]}
    signal = webapi.status_to_ui(status, {})['signal']
    assert signal['std_filter_ok'] is False

    status['assets'][0]['edge_ok'] = True
    assert webapi.status_to_ui(status, {})['signal']['std_filter_ok'] is True

    # Unmeasured stays None, never False: warm-up is not a rejection,
    # and the badge renders "-" rather than a red NO.
    status['assets'][0]['edge_ok'] = None
    assert webapi.status_to_ui(status, {})['signal']['std_filter_ok'] is None


def test_the_edge_verdict_is_none_until_there_is_a_usable_z(config, tmp_path,
                                                            monkeypatch):
    monkeypatch.chdir(tmp_path)
    from statarb.coordinator import Coordinator, PaperExecutor

    class Leg:
        name = 'acct'

        def ensure_symbol(self, symbol):
            return {'ok': True, 'volume_min': 0.01, 'volume_step': 0.01,
                    'volume_max': 100.0}

    coord = Coordinator(config, trading_mode='PAPER')
    coord.spot_leg = coord.futures_leg = Leg()
    coord.executor = PaperExecutor(coord.spot_leg, coord.futures_leg, config)
    coord.active_assets['GOLD'] = {'config': config.ASSETS['GOLD'],
                                   'spot_symbol': 'XAUUSD_',
                                   'futures_symbol': 'GC1226',
                                   'last_data': None}
    md = {'spot_price': 4292.615, 'futures_price': 4351.55,
          'spot_bid': 4292.55, 'spot_ask': 4292.68,
          'futures_bid': 4351.38, 'futures_ask': 4351.72,
          'spread': 58.94, 'basis_pct': 1.37}

    # No stats at all -> unmeasured
    assert coord._sizing_and_cost('GOLD', md, None)['edge_ok'] is None

    class Stats:
        z = None
        sigma = 0.2
    assert coord._sizing_and_cost('GOLD', md, Stats())['edge_ok'] is None

    # A real but tiny z -> a real, negative verdict
    class Thin(Stats):
        z = 0.24
    assert coord._sizing_and_cost('GOLD', md, Thin())['edge_ok'] is False


def test_the_published_leg_costs_add_up_to_the_model(config, tmp_path,
                                                     monkeypatch):
    """The card renders these instead of deriving anything, so they
    have to BE the cost model's own arithmetic. It previously applied
    one contract size and one lot count to both legs and printed
    "XAUUSD 0.2200 x 5000" for a 100-unit contract."""
    monkeypatch.chdir(tmp_path)
    from statarb.coordinator import Coordinator, PaperExecutor

    class Leg:
        name = 'acct'

        def ensure_symbol(self, symbol):
            return {'ok': True, 'volume_min': 0.01, 'volume_step': 0.01,
                    'volume_max': 10000.0}

    # Silver on Leg A (5,000/lot), gold on Leg B (100/lot)
    config.ASSETS = {'GOLD': dict(config.ASSETS['GOLD'], lot_size=5000.0,
                                  fut_lot_size=100.0)}
    config.TRADING.update({'SIZING_MODE': 'lots', 'CLIP_LOTS': 1.55,
                           'HEDGE_RATIO': 67.28})
    config.COSTS.update({'COMMISSION_PER_LOT_SPOT': 0.0,
                         'COMMISSION_PER_LOT_FUT': 0.0,
                         'SPREAD_COST_FACTOR': 1.0})
    coord = Coordinator(config, trading_mode='PAPER')
    coord.spot_leg = coord.futures_leg = Leg()
    coord.executor = PaperExecutor(coord.spot_leg, coord.futures_leg, config)
    coord.active_assets['GOLD'] = {'config': config.ASSETS['GOLD'],
                                   'spot_symbol': 'XAGUSD',
                                   'futures_symbol': 'XAUUSD',
                                   'last_data': None}
    md = {'spot_price': 64.686, 'futures_price': 4352.26,
          'spot_bid': 64.663, 'spot_ask': 64.709,
          'futures_bid': 4352.15, 'futures_ask': 4352.37,
          'spread': 4351.30, 'basis_pct': 0.0}
    block = coord._sizing_and_cost('GOLD', md, None)

    # Each leg in ITS OWN units
    assert block['rt_contract_a'] == 5000.0
    assert block['rt_contract_b'] == 100.0
    assert block['rt_lots_b'] != block['rt_lots_a']
    assert block['rt_leg_a_cost'] == pytest.approx(
        0.046 * block['rt_lots_a'] * 5000.0, rel=1e-6)
    assert block['rt_leg_b_cost'] == pytest.approx(
        0.22 * block['rt_lots_b'] * 100.0, rel=1e-6)

    # ...and the parts are exactly the whole the model computed
    assert (block['rt_leg_a_cost'] + block['rt_leg_b_cost']
            + block['rt_commission_a'] + block['rt_commission_b']) == \
        pytest.approx(block['rt_cost_usd'], rel=1e-9)

    # Gold's leg must not be charged silver's contract size
    assert block['rt_leg_b_cost'] < 100.0
