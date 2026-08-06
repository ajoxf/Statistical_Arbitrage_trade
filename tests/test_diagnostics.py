"""Connectivity checklist for the Exchanges page.

The point of the checklist is to catch, BEFORE money is at risk, the
things that silently break a two-account basis trade: algo trading
switched off, an investor password, a netting account, a symbol that
does not exist under that name on that broker, contract sizes that make
the configured hedge ratio wrong, an expired futures contract.
"""

import json
import threading
import time
from datetime import datetime, timedelta

import pytest

from statarb import diagnostics


def terminal(**overrides):
    base = {'library': True, 'terminal': True, 'terminal_name': 'MetaTrader 5',
            'terminal_connected': True, 'algo_trading': True, 'ping_ms': 42.0,
            'logged_in': True, 'login': 111, 'server': 'FxPro-Demo',
            'name': 'Trader', 'currency': 'USD', 'leverage': 100,
            'balance': 1_000_000.0, 'equity': 1_000_000.0,
            'margin_free': 900_000.0, 'trade_allowed': True,
            'trade_expert': True, 'margin_mode': 2, 'hedging': True}
    base.update(overrides)
    return base


def symbol(**overrides):
    base = {'symbol': 'XAUUSD', 'found': True, 'description': 'Gold vs USD',
            'visible': True, 'bid': 3300.0, 'ask': 3300.2,
            'tick_time': int(time.time()), 'digits': 2, 'point': 0.01,
            'tick_size': 0.01, 'contract_size': 100.0, 'volume_min': 0.01,
            'volume_max': 50.0, 'volume_step': 0.01, 'currency': 'USD',
            'filling_mode': 1, 'trade_mode': 4, 'trade_allowed': True,
            'expiry': 0}
    base.update(overrides)
    return base


def side(role='spot', account='account_a', term=None, sym=None, asset=None):
    return {'role': role, 'account': account,
            'terminal': term or terminal(),
            'symbol': sym or symbol(),
            'asset': asset or {'lot_size': 100.0}}


def report(config, spot=None, futures=None, **kwargs):
    futures = futures or side('futures', 'account_b',
                              sym=symbol(symbol='GC1225'))
    return diagnostics.build_report(config, spot or side(), futures, **kwargs)


def find(result, name, scope=None):
    for check in result['checks']:
        if check['name'] == name and (scope is None
                                      or scope in check['scope']):
            return check
    return None


@pytest.fixture
def cfg(config):
    config.TRADING.update({'CLIP_LOTS': 50.0, 'SLICE_LOTS': 10.0,
                           'HEDGE_RATIO': 1.0})
    return config


# --- a healthy pair -------------------------------------------------------

def test_a_healthy_pair_passes_everything(cfg):
    result = report(cfg)
    assert result['overall'] == 'PASS'
    assert result['failed'] == 0 and result['warnings'] == 0
    assert find(result, 'Account login')['status'] == 'PASS'
    assert find(result, 'Hedge ratio', 'PAIR')['status'] == 'PASS'


def test_each_leg_is_checked_under_its_own_account(cfg):
    result = report(cfg)
    scopes = {c['scope'] for c in result['checks']}
    assert 'SPOT · account_a' in scopes
    assert 'FUTURES · account_b' in scopes
    assert 'PAIR' in scopes


# --- terminal / account ---------------------------------------------------

def test_missing_mt5_package_is_a_single_clear_failure(cfg):
    result = report(cfg, spot=side(term={'library': False,
                                         'error': 'not installed'}))
    check = find(result, 'MT5 library')
    assert check['status'] == 'FAIL'
    assert any('pip install' in step for step in check['fix'])


def test_terminal_not_running_stops_that_leg_early(cfg):
    result = report(cfg, spot=side(term={'library': True, 'terminal': False,
                                         'error': 'no terminal'}))
    assert find(result, 'MT5 terminal')['status'] == 'FAIL'
    assert find(result, 'Symbol', 'SPOT') is None    # nothing to ask


def test_not_logged_in_is_reported_before_symbols(cfg):
    result = report(cfg, spot=side(term=terminal(logged_in=False)))
    assert find(result, 'Account login')['status'] == 'FAIL'
    assert find(result, 'Symbol', 'SPOT') is None


def test_algo_trading_off_fails_with_the_menu_path(cfg):
    result = report(cfg, spot=side(term=terminal(algo_trading=False)))
    check = find(result, 'Algo trading')
    assert check['status'] == 'FAIL'
    assert any('Expert Advisors' in step for step in check['fix'])


def test_investor_password_shows_up_as_no_trading_permission(cfg):
    result = report(cfg, spot=side(term=terminal(trade_allowed=False)))
    check = find(result, 'Trading permission')
    assert check['status'] == 'FAIL'
    assert any('investor' in step for step in check['fix'])


def test_netting_account_warns_because_closes_go_by_ticket(cfg):
    result = report(cfg, spot=side(term=terminal(hedging=False,
                                                 margin_mode=0)))
    check = find(result, 'Margin mode')
    assert check['status'] == 'WARN' and 'NETTING' in check['message']


def test_terminal_logged_into_the_wrong_account_is_caught(cfg):
    """The classic two-account mistake: both leg runners pointed at the
    same terminal, so one leg trades the wrong account."""
    result = report(cfg, expected_logins={'account_a': 999})
    check = find(result, 'Account mapping')
    assert check['status'] == 'FAIL' and '999' in check['message']


def test_no_mapping_check_when_the_login_matches(cfg):
    assert find(report(cfg, expected_logins={'account_a': 111}),
                'Account mapping') is None


# --- leverage (MT5 sets it broker-side; Settings must mirror it) ----------

def test_matching_leverage_passes(cfg):
    result = report(cfg, leverages={'spot': 100, 'futures': 100})
    assert find(result, 'Leverage', 'SPOT')['status'] == 'PASS'


def test_leverage_mismatch_warns_with_the_real_number(cfg):
    futures = side('futures', 'account_b', term=terminal(leverage=500),
                   sym=symbol(symbol='GC1225'))
    result = report(cfg, futures=futures,
                    leverages={'spot': 100, 'futures': 100})
    check = find(result, 'Leverage', 'FUTURES')
    assert check['status'] == 'WARN'
    assert '500x' in check['message'] and '100x' in check['message']
    assert any('broker-side' in step for step in check['fix'])


def test_legs_may_carry_different_leverage(cfg):
    futures = side('futures', 'account_b', term=terminal(leverage=500),
                   sym=symbol(symbol='GC1225'))
    result = report(cfg, futures=futures,
                    leverages={'spot': 100, 'futures': 500})
    assert find(result, 'Leverage', 'SPOT')['status'] == 'PASS'
    assert find(result, 'Leverage', 'FUTURES')['status'] == 'PASS'


# --- symbols --------------------------------------------------------------

def test_unknown_symbol_points_at_broker_naming(cfg):
    result = report(cfg, spot=side(sym={'symbol': 'XAUUSD', 'found': False,
                                        'error': 'XAUUSD does not exist'}))
    check = find(result, 'Symbol', 'SPOT')
    assert check['status'] == 'FAIL'
    assert any('GOLD' in step for step in check['fix'])


def test_symbol_hidden_from_market_watch_only_warns(cfg):
    result = report(cfg, spot=side(sym=symbol(visible=False)))
    assert find(result, 'Market Watch')['status'] == 'WARN'


def test_no_quotes_is_a_failure_the_basis_depends_on(cfg):
    result = report(cfg, spot=side(sym=symbol(bid=None, ask=None)))
    assert find(result, 'Price data', 'SPOT')['status'] == 'FAIL'


def test_close_only_symbol_is_flagged(cfg):
    result = report(cfg, spot=side(sym=symbol(trade_allowed=False)))
    assert find(result, 'Symbol trading')['status'] == 'FAIL'


def test_slice_larger_than_the_brokers_max_order_fails(cfg):
    cfg.TRADING['SLICE_LOTS'] = 80.0
    result = report(cfg, spot=side(sym=symbol(volume_max=50.0)))
    check = find(result, 'Order size', 'SPOT')
    assert check['status'] == 'FAIL' and '50' in check['message']


def test_slice_off_the_volume_step_warns(cfg):
    cfg.TRADING['SLICE_LOTS'] = 10.05
    result = report(cfg, spot=side(sym=symbol(volume_step=0.1)))
    assert find(result, 'Order size', 'SPOT')['status'] == 'WARN'


def test_contract_size_mismatch_is_a_hard_failure(cfg):
    """100 oz vs 10 oz per lot silently makes every P&L and hedge
    number wrong by 10x."""
    result = report(cfg, spot=side(sym=symbol(contract_size=10.0),
                                   asset={'lot_size': 100.0}))
    check = find(result, 'Contract size', 'SPOT')
    assert check['status'] == 'FAIL' and '10' in check['message']


# --- futures expiry -------------------------------------------------------

def test_expired_configured_contract_fails_loudly(cfg):
    stale = datetime.now() - timedelta(days=3)
    futures = side('futures', 'account_b', sym=symbol(symbol='GC1225'),
                   asset={'lot_size': 100.0, 'futures_expiry': stale})
    check = find(report(cfg, futures=futures), 'Futures expiry')
    assert check['status'] == 'FAIL'
    assert 'zeroes the swap basis' in check['message']


def test_expiry_within_days_warns_to_roll(cfg):
    soon = datetime.now() + timedelta(days=2)
    futures = side('futures', 'account_b',
                   sym=symbol(symbol='GC1225',
                              expiry=int(soon.timestamp())),
                   asset={'lot_size': 100.0})
    check = find(report(cfg, futures=futures), 'Futures expiry')
    assert check['status'] == 'WARN' and 'Roll' in check['fix'][0]


def test_broker_expiry_disagreeing_with_config_warns(cfg):
    broker_date = datetime.now() + timedelta(days=60)
    configured = datetime.now() + timedelta(days=30)
    futures = side('futures', 'account_b',
                   sym=symbol(symbol='GC1225',
                              expiry=int(broker_date.timestamp())),
                   asset={'lot_size': 100.0, 'futures_expiry': configured})
    check = find(report(cfg, futures=futures), 'Futures expiry')
    assert check['status'] == 'WARN' and 'Settings says' in check['message']


def test_the_spot_leg_is_not_asked_about_expiry(cfg):
    result = report(cfg)
    assert find(result, 'Futures expiry', 'SPOT') is None


# --- the pair -------------------------------------------------------------

def test_two_accounts_are_reported_as_the_two_runner_topology(cfg):
    check = find(report(cfg), 'Topology')
    assert check['status'] == 'PASS' and 'one leg runner each' in \
        check['message']


def test_one_account_topology_is_recognised(cfg):
    futures = side('futures', 'account_a', sym=symbol(symbol='GC1225'))
    check = find(report(cfg, futures=futures), 'Topology')
    assert check['status'] == 'INFO' and 'one account' in check['message']


def test_currency_mismatch_warns_about_the_combined_totals(cfg):
    futures = side('futures', 'account_b', term=terminal(currency='EUR'),
                   sym=symbol(symbol='GC1225'))
    check = find(report(cfg, futures=futures), 'Account currency')
    assert check['status'] == 'WARN' and 'EUR' in check['message']


def test_hedge_ratio_is_checked_against_both_contract_sizes(cfg):
    """CLAUDE.md's open item: HEDGE_RATIO must be verified against the
    brokers' real contract specs before LIVE. This is that check."""
    cfg.TRADING['HEDGE_RATIO'] = 1.0
    futures = side('futures', 'account_b',
                   sym=symbol(symbol='GC1225', contract_size=50.0))
    check = find(report(cfg, futures=futures), 'Hedge ratio')
    assert check['status'] == 'FAIL'
    assert '2.0000' in check['message']
    assert check['details']['implied hedge ratio'] == 2.0


def test_hedge_ratio_matching_the_specs_passes(cfg):
    cfg.TRADING['HEDGE_RATIO'] = 2.0
    futures = side('futures', 'account_b',
                   sym=symbol(symbol='GC1225', contract_size=50.0))
    assert find(report(cfg, futures=futures), 'Hedge ratio')['status'] == \
        'PASS'


def test_both_legs_on_one_symbol_and_one_account_is_no_spread(cfg):
    futures = side('futures', 'account_a', sym=symbol(symbol='XAUUSD'))
    check = find(report(cfg, futures=futures), 'Symbols')
    assert check['status'] == 'FAIL' and 'no spread' in check['message']


def test_live_basis_is_shown_from_both_feeds(cfg):
    futures = side('futures', 'account_b',
                   sym=symbol(symbol='GC1225', bid=3320.0, ask=3320.2))
    check = find(report(cfg, futures=futures), 'Live basis')
    assert check['status'] == 'PASS' and '+20.0000' in check['message']


def test_a_stale_feed_is_flagged(cfg):
    old = int(time.time()) - 600
    futures = side('futures', 'account_b',
                   sym=symbol(symbol='GC1225', tick_time=old))
    check = find(report(cfg, futures=futures), 'Quote freshness')
    assert check['status'] == 'WARN'


# --- aggregation ----------------------------------------------------------

def test_overall_is_the_worst_status(cfg):
    assert report(cfg)['overall'] == 'PASS'
    warned = report(cfg, spot=side(sym=symbol(visible=False)))
    assert warned['overall'] == 'WARN'
    failed = report(cfg, spot=side(term=terminal(algo_trading=False)))
    assert failed['overall'] == 'FAIL'


# --- per-leg leverage in the money math -----------------------------------

def test_capital_at_risk_divides_each_leg_by_its_own_leverage(config):
    from statarb.exits import ExitLadder
    config.EXITS.update({'SPOT_LEVERAGE': 100.0, 'FUT_LEVERAGE': 500.0,
                         'LEVERAGE': 0.0, 'M2M_BUFFER_PCT': 0.0})
    market = {'spot_price': 3300.0, 'futures_price': 3320.0}
    capital = ExitLadder(config)._capital_at_risk(50.0, 100.0, market)
    # 50 lots x 100oz: 16.5m spot at 100x + 16.6m futures at 500x
    assert capital == pytest.approx(3300 * 5000 / 100 + 3320 * 5000 / 500)


def test_per_leg_leverage_falls_back_to_the_shared_knob(config):
    from statarb.exits import ExitLadder
    config.EXITS.update({'SPOT_LEVERAGE': 0.0, 'FUT_LEVERAGE': 0.0,
                         'LEVERAGE': 100.0, 'M2M_BUFFER_PCT': 0.0})
    market = {'spot_price': 3300.0, 'futures_price': 3320.0}
    capital = ExitLadder(config)._capital_at_risk(50.0, 100.0, market)
    assert capital == pytest.approx((3300 + 3320) * 5000 / 100)


def test_no_leverage_anywhere_disables_the_capital_forms(config):
    from statarb.exits import ExitLadder
    config.EXITS.update({'SPOT_LEVERAGE': 0.0, 'FUT_LEVERAGE': 0.0,
                         'LEVERAGE': 0.0})
    assert ExitLadder(config)._capital_at_risk(
        50.0, 100.0, {'spot_price': 3300.0, 'futures_price': 3320.0}) is None


# --- coordinator wiring ---------------------------------------------------

class DiagFakeLeg:
    def __init__(self, name, term=None, sym=None, symbols=None):
        self.name = name
        self._terminal = term or terminal()
        self._symbol = sym or symbol()
        self._symbols = symbols
        self.asked = []

    def ping(self):
        return True

    def account_info(self):
        return {'account': self.name, 'login': self._terminal.get('login'),
                'equity': 1e6}

    def terminal_report(self):
        return dict(self._terminal, account=self.name)

    def symbol_report(self, sym):
        self.asked.append(sym)
        return dict(self._symbol, symbol=sym, account=self.name)

    def find_symbols(self, pattern, limit=40):
        if self._symbols is None:
            return None
        return [s for s in self._symbols
                if pattern.upper() in s['symbol'].upper()]


@pytest.fixture
def coordinator(tmp_path, monkeypatch, cfg):
    monkeypatch.chdir(tmp_path)
    from statarb.coordinator import Coordinator
    coord = Coordinator(cfg, trading_mode='PAPER')
    coord.spot_leg = DiagFakeLeg('account_a', symbols=[
        {'symbol': 'XAUUSD', 'description': 'Gold'},
        {'symbol': 'XAGUSD', 'description': 'Silver'}])
    coord.futures_leg = DiagFakeLeg(
        'account_b', sym=symbol(symbol='GC1225'),
        symbols=[{'symbol': 'GC1225', 'description': 'Gold futures'}])
    coord.active_assets['GOLD'] = {
        'config': cfg.ASSETS['GOLD'], 'spot_symbol': 'XAUUSD',
        'futures_symbol': 'GC1225', 'last_data': None}
    coord.control_path = str(tmp_path / "control.json")
    return coord


_diag_ts = [0.0]


def request_diagnose(coordinator, **payload):
    _diag_ts[0] += 1.0
    with open(coordinator.control_path, 'w') as f:
        json.dump({'algo_enabled': coordinator.algo_enabled,
                   'diagnose': dict({'ts': _diag_ts[0]}, **payload)}, f)
    coordinator._control_mtime = 0
    coordinator._read_control()
    return _diag_ts[0]


def test_the_control_file_runs_the_checklist_on_both_legs(coordinator):
    ts = request_diagnose(coordinator)
    result = coordinator._diagnostics
    assert result['ts'] == ts
    assert coordinator.spot_leg.asked == ['XAUUSD']
    assert coordinator.futures_leg.asked == ['GC1225']
    assert set(result['legs']) == {'account_a', 'account_b'}
    # Every leg check ran against the live reports
    assert find(result, 'Algo trading', 'SPOT')['status'] == 'PASS'
    assert find(result, 'Hedge ratio', 'PAIR')['status'] == 'PASS'


def test_the_default_configs_stale_futures_expiry_is_caught(coordinator):
    """The shipped config still names last year's contract — an
    expired expiry zeroes the swap basis and disables every signal,
    so the checklist must say so before LIVE."""
    request_diagnose(coordinator)
    check = find(coordinator._diagnostics, 'Futures expiry')
    assert check['status'] == 'FAIL'
    assert coordinator._diagnostics['overall'] == 'FAIL'


def test_the_checklist_reaches_runtime_status(coordinator, tmp_path):
    ts = request_diagnose(coordinator)
    with open(tmp_path / "runtime_status.json") as f:
        assert json.load(f)['diagnostics']['ts'] == ts


def test_diagnostics_run_even_while_the_algo_is_trading(coordinator):
    """It is read-only — unlike the order scenarios, there is no reason
    to make the operator stop the engine to check connectivity."""
    coordinator.algo_enabled = True
    request_diagnose(coordinator)
    assert coordinator._diagnostics['checks']


def test_configured_logins_are_compared_against_the_terminals(coordinator):
    account = next(iter(coordinator.config.accounts.values()))
    account.login, account.name = 999, 'account_a'
    coordinator.config.accounts['account_a'] = account
    request_diagnose(coordinator)
    check = find(coordinator._diagnostics, 'Account mapping')
    assert check['status'] == 'FAIL' and '999' in check['message']


def test_symbol_search_asks_the_requested_leg(coordinator):
    request_diagnose(coordinator, find_symbols='XAU', leg='spot')
    found = coordinator._symbol_search
    assert found['account'] == 'account_a'
    assert [s['symbol'] for s in found['symbols']] == ['XAUUSD']

    request_diagnose(coordinator, find_symbols='GC', leg='futures')
    assert coordinator._symbol_search['account'] == 'account_b'


def test_symbol_search_reports_an_unreadable_account(coordinator):
    coordinator.spot_leg._symbols = None        # IPC failure
    request_diagnose(coordinator, find_symbols='XAU', leg='spot')
    assert coordinator._symbol_search['error']
    assert coordinator._symbol_search['symbols'] == []


def test_no_active_asset_is_explained_not_crashed(coordinator):
    coordinator.active_assets.clear()
    request_diagnose(coordinator)
    assert coordinator._diagnostics['overall'] == 'FAIL'
    assert 'No active asset' in \
        coordinator._diagnostics['checks'][0]['message']


# --- the API the Exchanges page calls -------------------------------------

pytest.importorskip("flask")

from statarb.webapp import create_app                     # noqa: E402


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "config.json").write_text(json.dumps({
        'accounts': {'account_a': {'login': 111},
                     'account_b': {'login': 222}},
        'leg_accounts': {'spot': 'account_a', 'futures': 'account_b'},
        'assets': {'GOLD': {'name': 'GOLD', 'enabled': True,
                            'spot_symbols': ['XAUUSD'],
                            'futures_symbols': ['GC1225'],
                            'lot_size': 100}}}))
    (tmp_path / "runtime_status.json").write_text(json.dumps({}))
    app = create_app(db_path=str(tmp_path / "algo.db"),
                     status_path=str(tmp_path / "runtime_status.json"),
                     config_path=str(tmp_path / "config.json"),
                     control_path=str(tmp_path / "control.json"),
                     env_path=str(tmp_path / ".env"),
                     scenario_timeout=1.0, diagnose_timeout=1.0)
    app.config['TESTING'] = True
    client = app.test_client()
    client.tmp_path = tmp_path
    return client


def answer_as_coordinator(tmp_path, key, payload):
    """Stand in for the coordinator: watch control.json, publish the
    answer under the ts it was asked with."""
    def worker():
        for _ in range(80):
            try:
                with open(tmp_path / "control.json") as f:
                    spec = json.load(f).get('diagnose') or {}
            except (OSError, ValueError):
                spec = {}
            if spec.get('ts'):
                with open(tmp_path / "runtime_status.json", 'w') as f:
                    json.dump({key: dict(payload, ts=spec['ts'])}, f)
                return
            time.sleep(0.05)
    thread = threading.Thread(target=worker, daemon=True)
    thread.start()
    return thread


DIAG_PAYLOAD = {
    'overall': 'WARN', 'passed': 8, 'warnings': 1, 'failed': 0, 'info': 0,
    'ran_at': '12:00:00',
    'checks': [
        {'scope': 'SPOT · account_a', 'name': 'Account login',
         'status': 'PASS', 'message': 'ok'},
        {'scope': 'FUTURES · account_b', 'name': 'Market Watch',
         'status': 'WARN', 'message': 'hidden'},
        {'scope': 'PAIR', 'name': 'Hedge ratio', 'status': 'PASS',
         'message': 'ok'}],
    'legs': {
        'account_a': {'role': 'spot',
                      'terminal': terminal(),
                      'symbol': symbol()},
        'account_b': {'role': 'futures',
                      'terminal': terminal(login=222, leverage=500),
                      'symbol': symbol(symbol='GC1225', bid=3320.0,
                                       ask=3320.2)}},
}


def test_diagnose_endpoint_returns_the_whole_checklist(client):
    answer_as_coordinator(client.tmp_path, 'diagnostics', DIAG_PAYLOAD)
    data = client.post('/api/brokers/diagnose').get_json()
    assert data['overall'] == 'WARN' and len(data['checks']) == 3


def test_diagnose_for_one_account_keeps_its_checks_and_the_pair(client):
    answer_as_coordinator(client.tmp_path, 'diagnostics', DIAG_PAYLOAD)
    data = client.post('/api/brokers/account_a/diagnose').get_json()
    scopes = {c['scope'] for c in data['checks']}
    assert scopes == {'SPOT · account_a', 'PAIR'}


def test_test_connection_answers_in_the_old_apps_shape(client):
    answer_as_coordinator(client.tmp_path, 'diagnostics', DIAG_PAYLOAD)
    data = client.post('/api/brokers/account_b/test').get_json()
    assert data['success']
    assert data['account_info']['login'] == 222
    assert data['account_info']['leverage'] == 500
    assert data['price_info']['symbol'] == 'GC1225'


def test_test_connection_on_an_unmapped_account_says_so(client):
    answer_as_coordinator(client.tmp_path, 'diagnostics', DIAG_PAYLOAD)
    data = client.post('/api/brokers/account_z/test').get_json()
    assert not data['success'] and 'not mapped' in data['error']


def test_a_silent_coordinator_is_reported_not_hung(client):
    data = client.post('/api/brokers/diagnose').get_json()
    assert data['overall'] == 'FAIL'
    assert 'No answer from the coordinator' in data['checks'][0]['message']


def test_symbol_search_passes_the_pattern_and_leg_through(client):
    answer_as_coordinator(client.tmp_path, 'symbol_search', {
        'leg': 'futures', 'account': 'account_b', 'pattern': 'GC',
        'symbols': [{'symbol': 'GC1225', 'description': 'Gold futures'}]})
    data = client.get('/api/symbols/search?leg=futures&q=GC').get_json()
    assert data['symbols'][0]['symbol'] == 'GC1225'
    with open(client.tmp_path / "control.json") as f:
        asked = json.load(f)['diagnose']
    assert asked['find_symbols'] == 'GC' and asked['leg'] == 'futures'


def test_leg_symbols_get_shows_what_each_account_trades(client):
    data = client.get('/api/leg-symbols').get_json()
    assert data['spot_symbol'] == 'XAUUSD'
    assert data['futures_symbol'] == 'GC1225'
    assert data['spot_account'] == 'account_a'
    assert data['futures_account'] == 'account_b'


def test_saving_symbols_writes_config_and_warns_about_the_restart(client):
    data = client.post('/api/leg-symbols',
                       json={'spot_symbol': 'GOLD',
                             'futures_symbol': 'GOLD-DEC25',
                             'futures_expiry': '2026-12-24'}).get_json()
    assert data['success'] and 'restart' in data['note']
    with open(client.tmp_path / "config.json") as f:
        asset = json.load(f)['assets']['GOLD']
    assert asset['spot_symbols'] == ['GOLD']
    assert asset['futures_symbols'] == ['GOLD-DEC25']
    assert asset['futures_expiry'] == '2026-12-24'


def test_saving_nothing_changes_nothing(client):
    data = client.post('/api/leg-symbols',
                       json={'spot_symbol': 'XAUUSD'}).get_json()
    assert data['success'] and data['note'] == 'Nothing changed.'


def test_per_leg_leverage_round_trips_through_the_settings_api(client):
    """The Leg A / Leg B leverage selectors used to be dead controls —
    they now persist and drive each leg's margin."""
    assert client.post('/api/config', json={'spot_leverage': 100,
                                            'futures_leverage': 500}
                       ).status_code == 200
    with open(client.tmp_path / "config.json") as f:
        exits = json.load(f)['exits']
    assert exits['SPOT_LEVERAGE'] == 100 and exits['FUT_LEVERAGE'] == 500
    ui = client.get('/api/config').get_json()
    assert ui['spot_leverage'] == 100 and ui['futures_leverage'] == 500


def test_settings_page_offers_mt5_scale_leverage(client):
    page = client.get('/settings').get_data(as_text=True)
    block = page.split('id="spot_leverage"', 1)[1].split('</select>', 1)[0]
    for value in ('100', '200', '500'):
        assert f'value="{value}"' in block


# --- the page -------------------------------------------------------------

def test_exchanges_page_has_the_connectivity_controls(client):
    page = client.get('/setup').get_data(as_text=True)
    assert 'Run Full Connectivity Check' in page
    assert 'Connectivity Checklist' in page
    assert '/api/brokers/' in page and '/api/symbols/search' in page
    assert 'Leg runner endpoint' in page
    assert 'Save Symbols' in page
