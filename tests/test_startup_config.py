"""Startup configuration that must never take the system down silently.

Both faults here were seen on the operator's machine on 2026-08-06:

1. an endpoint typed '127.0.0.1.9101' (dot instead of colon) crashed
   BOTH the coordinator and the leg runner at startup, in a restart
   loop, with an int() traceback;
2. an account named 'Ut 2' produced the .env key 'MT5_PASSWORD_UT 2' —
   a key with a space, which dotenv cannot parse, so the password
   never loaded and MT5 could not log in.
"""

import json
import os

import pytest

from statarb import ipc


# --- endpoints ------------------------------------------------------------

def test_the_normal_form_parses():
    assert ipc.parse_endpoint('127.0.0.1:9101') == ('127.0.0.1', 9101)


def test_a_dot_where_the_colon_should_be_is_understood():
    """The exact typo that took the system down — five dot-separated
    numbers can only mean host.port."""
    assert ipc.parse_endpoint('127.0.0.1.9101') == ('127.0.0.1', 9101)


def test_a_bare_port_means_localhost():
    assert ipc.parse_endpoint('9102') == ('127.0.0.1', 9102)


def test_whitespace_and_quotes_are_tolerated():
    assert ipc.parse_endpoint('  "127.0.0.1:9101" ') == ('127.0.0.1', 9101)


def test_a_hostname_still_works():
    assert ipc.parse_endpoint('localhost:9101') == ('localhost', 9101)


def test_an_empty_endpoint_explains_when_it_is_legal():
    with pytest.raises(ValueError) as excinfo:
        ipc.parse_endpoint('')
    assert '127.0.0.1:9101' in str(excinfo.value)
    assert 'blank ONLY when both legs share one account' in str(excinfo.value)


def test_a_missing_port_says_use_a_colon():
    with pytest.raises(ValueError) as excinfo:
        ipc.parse_endpoint('127.0.0.1')
    assert 'COLON' in str(excinfo.value)


def test_a_non_numeric_port_is_rejected_with_the_expected_format():
    with pytest.raises(ValueError) as excinfo:
        ipc.parse_endpoint('127.0.0.1:abc')
    assert 'host:port' in str(excinfo.value)


def test_an_impossible_port_number_is_rejected():
    with pytest.raises(ValueError) as excinfo:
        ipc.parse_endpoint('127.0.0.1:99999')
    assert 'not a valid' in str(excinfo.value)


def test_the_coordinator_names_the_account_it_cannot_start(config,
                                                           monkeypatch,
                                                           tmp_path):
    """A restart loop of tracebacks tells the operator nothing; the
    message has to name the account and where to fix it."""
    monkeypatch.chdir(tmp_path)
    from types import SimpleNamespace
    from statarb.coordinator import Coordinator
    config.accounts = {
        'Ut 2': SimpleNamespace(name='Ut 2', endpoint='127.0.0.1:not-a-port',
                                login=1, server='X', terminal_path=None,
                                password=None, password_env='X')}
    config.leg_accounts = {'spot': 'Ut 2', 'futures': 'Ut 2'}
    with pytest.raises(ValueError) as excinfo:
        Coordinator(config, trading_mode='PAPER')
    message = str(excinfo.value)
    assert "Ut 2" in message and 'Exchanges page' in message


# --- .env keys and values -------------------------------------------------

pytest.importorskip("flask")

from statarb.webapp import (create_app, env_var_name,     # noqa: E402
                            update_env_file)


def test_an_account_name_with_a_space_gets_a_legal_env_key():
    assert env_var_name('MT5_PASSWORD_', 'Ut 2') == 'MT5_PASSWORD_UT_2'
    assert env_var_name('MT5_PASSWORD_', 'account-a') == \
        'MT5_PASSWORD_ACCOUNT_A'
    assert env_var_name('MT5_PASSWORD_', 'FxPro (live)') == \
        'MT5_PASSWORD_FXPRO_LIVE'


def test_passwords_are_quoted_so_special_characters_survive(tmp_path):
    path = str(tmp_path / '.env')
    update_env_file(path, {'MT5_PASSWORD_A': 'pa ss#word"x'})
    written = (tmp_path / '.env').read_text()
    assert written.strip() == 'MT5_PASSWORD_A="pa ss#word\\"x"'


def test_a_password_with_spaces_round_trips_through_dotenv(tmp_path):
    dotenv = pytest.importorskip("dotenv")
    path = str(tmp_path / '.env')
    update_env_file(path, {'MT5_PASSWORD_A': 'my secret # 1'})
    assert dotenv.dotenv_values(path)['MT5_PASSWORD_A'] == 'my secret # 1'


def test_the_broken_lines_that_block_dotenv_are_repaired(tmp_path):
    """One unreadable line is enough for dotenv to skip that statement
    — and the credential it held never reaches the engine."""
    path = tmp_path / '.env'
    path.write_text('# comment\nTELEGRAM_BOT_TOKEN=abc\n'
                    'MT5_PASSWORD_UT 2=secret\nMT5_PASSWORD_UT 1=other\n')
    dropped = update_env_file(str(path), {'MT5_PASSWORD_UT_2': 'secret'})
    text = path.read_text()
    assert len(dropped) == 2
    assert 'MT5_PASSWORD_UT 2' not in text
    assert 'TELEGRAM_BOT_TOKEN=abc' in text      # untouched
    assert '# comment' in text                   # comments preserved
    assert 'MT5_PASSWORD_UT_2="secret"' in text

    dotenv = pytest.importorskip("dotenv")
    values = dotenv.dotenv_values(str(path))
    assert values['MT5_PASSWORD_UT_2'] == 'secret'


def test_an_existing_value_is_replaced_not_duplicated(tmp_path):
    path = tmp_path / '.env'
    update_env_file(str(path), {'MT5_PASSWORD_A': 'old'})
    update_env_file(str(path), {'MT5_PASSWORD_A': 'new'})
    lines = [line for line in path.read_text().splitlines() if line]
    assert lines == ['MT5_PASSWORD_A="new"']


# --- the UI refuses to save a config that cannot start --------------------

@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(os, 'environ', dict(os.environ))
    (tmp_path / "config.json").write_text(json.dumps({
        'accounts': {'account_a': {'login': 111,
                                   'endpoint': '127.0.0.1:9101'}},
        'leg_accounts': {'spot': 'account_a', 'futures': 'account_a'}}))
    (tmp_path / "runtime_status.json").write_text('{}')
    app = create_app(db_path=str(tmp_path / "algo.db"),
                     status_path=str(tmp_path / "runtime_status.json"),
                     config_path=str(tmp_path / "config.json"),
                     control_path=str(tmp_path / "control.json"),
                     env_path=str(tmp_path / ".env"))
    app.config['TESTING'] = True
    client = app.test_client()
    client.tmp_path = tmp_path
    return client


def saved_accounts(client):
    with open(client.tmp_path / "config.json") as f:
        return json.load(f)['accounts']


def test_a_broken_endpoint_is_rejected_at_save_time(client):
    response = client.post('/api/exchanges',
                           json={'name': 'Ut 2',
                                 'endpoint': '127.0.0.1:oops'})
    assert response.status_code == 400
    assert 'host:port' in response.get_json()['error']
    assert 'Ut 2' not in saved_accounts(client)


def test_the_dot_typo_is_normalised_on_save(client):
    response = client.post('/api/exchanges',
                           json={'name': 'Ut 2', 'login': 222,
                                 'endpoint': '127.0.0.1.9102'})
    assert response.status_code == 200
    assert saved_accounts(client)['Ut 2']['endpoint'] == '127.0.0.1:9102'


def test_a_blank_endpoint_stays_blank_for_single_account_setups(client):
    client.post('/api/exchanges', json={'name': 'solo', 'endpoint': ''})
    assert saved_accounts(client)['solo']['endpoint'] == ''


def test_the_bulk_account_save_validates_too(client):
    response = client.post('/api/config', json={'accounts': {
        'Ut 1': {'login': 111, 'endpoint': '127.0.0.1:9101'},
        'Ut 2': {'login': 222, 'endpoint': 'garbage'}}})
    assert response.status_code == 400
    assert 'Ut 2' in response.get_json()['error']
    assert 'Ut 1' not in saved_accounts(client)      # nothing written


def test_a_password_saved_from_the_ui_uses_a_safe_key(client):
    client.post('/api/exchanges', json={'name': 'Ut 2', 'login': 222,
                                        'endpoint': '127.0.0.1:9102',
                                        'password': 'secret pass'})
    assert saved_accounts(client)['Ut 2']['password_env'] == \
        'MT5_PASSWORD_UT_2'
    env = (client.tmp_path / ".env").read_text()
    assert 'MT5_PASSWORD_UT_2="secret pass"' in env


# --- the checker reports both faults --------------------------------------

def test_check_mt5_reports_unreadable_env_lines_and_port_clashes(
        tmp_path, monkeypatch, capsys, config):
    import check_mt5
    from types import SimpleNamespace
    monkeypatch.chdir(tmp_path)
    (tmp_path / '.env').write_text('MT5_PASSWORD_UT 2=secret\n')
    config.accounts = {
        'Ut 1': SimpleNamespace(name='Ut 1', endpoint='127.0.0.1.9101',
                                password_env='MT5_PASSWORD_UT 1'),
        'Ut 2': SimpleNamespace(name='Ut 2', endpoint='127.0.0.1:9101',
                                password_env='MT5_PASSWORD_UT 2')}
    check_mt5.ok_count = check_mt5.warn_count = check_mt5.fail_count = 0
    check_mt5.check_config_files(config, str(tmp_path / '.env'))
    out = capsys.readouterr().out
    assert 'was read as 127.0.0.1:9101' in out        # the dot typo
    assert 'share port 9101' in out                   # both on one port
    assert 'cannot be read by dotenv' in out          # the .env fault
    assert 'is not a legal environment variable name' in out


# --- symbols live on the broker row (the old app's model) ----------------

def test_saving_a_broker_sets_its_leg_and_symbol_together(client):
    """One row = one account, one leg, one symbol — the way app.py did
    it, instead of a separate symbols panel."""
    response = client.post('/api/exchanges', json={
        'name': 'fut_cfi', 'role': 'FUTURES', 'symbol': 'GC1225',
        'login': 222, 'server': 'CFI-Demo', 'endpoint': '127.0.0.1:9102',
        'contract_size': 100, 'swap_charge': 45,
        'futures_expiry': '2026-12-24'})
    assert response.status_code == 200

    with open(client.tmp_path / "config.json") as f:
        raw = json.load(f)
    assert raw['leg_accounts']['futures'] == 'fut_cfi'
    asset = raw['assets']['GOLD']
    assert asset['futures_symbols'] == ['GC1225']
    assert asset['lot_size'] == 100 and asset['swap_charge'] == 45
    assert asset['futures_expiry'] == '2026-12-24'


def test_the_spot_row_writes_the_spot_symbol(client):
    client.post('/api/exchanges', json={
        'name': 'spot_fxpro', 'role': 'SPOT', 'symbol': 'GOLD.r',
        'endpoint': '127.0.0.1:9101'})
    with open(client.tmp_path / "config.json") as f:
        raw = json.load(f)
    assert raw['assets']['GOLD']['spot_symbols'] == ['GOLD.r']
    assert raw['leg_accounts']['spot'] == 'spot_fxpro'


def test_a_symbol_without_a_leg_is_refused(client):
    response = client.post('/api/exchanges', json={
        'name': 'mystery', 'symbol': 'XAUUSD'})
    assert response.status_code == 400
    assert 'role' in response.get_json()['error'].lower()


def test_the_broker_row_reports_the_symbol_it_trades(client):
    client.post('/api/exchanges', json={
        'name': 'account_a', 'role': 'SPOT', 'symbol': 'XAUUSD',
        'endpoint': '127.0.0.1:9101'})
    client.post('/api/exchanges', json={
        'name': 'fut', 'role': 'FUTURES', 'symbol': 'GC1225',
        'endpoint': '127.0.0.1:9102'})
    rows = {b['id']: b for b in client.get('/api/exchanges').get_json()}
    assert rows['account_a']['symbol'] == 'XAUUSD'
    assert rows['account_a']['role'] == 'SPOT'
    assert rows['fut']['symbol'] == 'GC1225'
    assert rows['fut']['role'] == 'FUTURES'


def test_editing_a_broker_keeps_the_other_legs_symbol(client):
    client.post('/api/exchanges', json={'name': 'a', 'role': 'SPOT',
                                        'symbol': 'XAUUSD',
                                        'endpoint': '127.0.0.1:9101'})
    client.post('/api/exchanges', json={'name': 'b', 'role': 'FUTURES',
                                        'symbol': 'GC1225',
                                        'endpoint': '127.0.0.1:9102'})
    client.post('/api/exchanges', json={'name': 'a', 'role': 'SPOT',
                                        'symbol': 'GOLD',
                                        'endpoint': '127.0.0.1:9101'})
    with open(client.tmp_path / "config.json") as f:
        asset = json.load(f)['assets']['GOLD']
    assert asset['spot_symbols'] == ['GOLD']
    assert asset['futures_symbols'] == ['GC1225']


# --- two accounts: same broker or different, but never one terminal ------

def two_account_config(config, spot_path=None, fut_path=None,
                       spot_endpoint='127.0.0.1:9101',
                       fut_endpoint='127.0.0.1:9102'):
    from types import SimpleNamespace
    config.accounts = {
        'spot_acct': SimpleNamespace(name='spot_acct', login=111,
                                     server='FxPro', password=None,
                                     password_env='A',
                                     terminal_path=spot_path,
                                     endpoint=spot_endpoint),
        'fut_acct': SimpleNamespace(name='fut_acct', login=222,
                                    server='FxPro', password=None,
                                    password_env='B',
                                    terminal_path=fut_path,
                                    endpoint=fut_endpoint)}
    config.leg_accounts = {'spot': 'spot_acct', 'futures': 'fut_acct'}
    return config


def test_two_accounts_at_the_same_broker_are_fine(config, monkeypatch,
                                                  tmp_path):
    """Same broker, two logins, two terminal installations — the whole
    point of the two-account topology."""
    monkeypatch.chdir(tmp_path)
    from statarb.coordinator import Coordinator
    coord = Coordinator(two_account_config(
        config, spot_path='C:/MT5-A/terminal64.exe',
        fut_path='C:/MT5-B/terminal64.exe'), trading_mode='PAPER')
    assert coord.spot_leg.name == 'spot_acct'
    assert coord.futures_leg.name == 'fut_acct'
    assert coord.spot_leg is not coord.futures_leg


def test_two_accounts_sharing_one_terminal_install_is_refused(
        config, monkeypatch, tmp_path):
    """A terminal holds ONE login — both leg runners would end up
    trading the same account."""
    monkeypatch.chdir(tmp_path)
    from statarb.coordinator import Coordinator
    shared = 'C:/Program Files/MetaTrader 5/terminal64.exe'
    with pytest.raises(ValueError) as excinfo:
        Coordinator(two_account_config(config, spot_path=shared,
                                       fut_path=shared),
                    trading_mode='PAPER')
    message = str(excinfo.value)
    assert 'same MT5 installation' in message
    assert 'second copy' in message


def test_a_half_configured_split_warns_but_still_runs(config, monkeypatch,
                                                      tmp_path, caplog):
    """One endpoint, one blank: the coordinator holds that account's MT5
    connection itself. Legal, but the operator should know."""
    monkeypatch.chdir(tmp_path)
    from statarb.coordinator import Coordinator
    with caplog.at_level('WARNING'):
        coord = Coordinator(two_account_config(config, fut_endpoint=None),
                            trading_mode='PAPER')
    assert coord.spot_leg is not coord.futures_leg
    assert 'no leg runner endpoint' in caplog.text
    assert 'coordinator holds its MT5 connection itself' in caplog.text


# --- expiry is optional: just pick up the symbols -------------------------

def market_data(config, expiry='unset'):
    from types import SimpleNamespace
    from statarb.marketdata import compute_market_data
    asset = dict(config.ASSETS['GOLD'])
    if expiry == 'unset':
        asset.pop('futures_expiry', None)
    else:
        asset['futures_expiry'] = expiry
    spot = SimpleNamespace(bid=3299.9, ask=3300.1, last=3300.0)
    fut = SimpleNamespace(bid=3319.9, ask=3320.1, last=3320.0)
    return compute_market_data(asset, spot, fut)


def test_a_snapshot_carries_the_identity_of_its_two_quotes(config):
    """SpreadStats dedups on this: two polls that read the same pair of
    ticks are ONE observation, however many times we looked."""
    from types import SimpleNamespace
    from statarb.marketdata import compute_market_data
    asset = dict(config.ASSETS['GOLD'])
    spot = SimpleNamespace(bid=3299.9, ask=3300.1, last=3300.0, time=1000)
    fut = SimpleNamespace(bid=3319.9, ask=3320.1, last=3320.0, time=1000)
    first = compute_market_data(asset, spot, fut)
    assert compute_market_data(asset, spot, fut)['quote_id'] == \
        first['quote_id']

    moved = SimpleNamespace(bid=3319.9, ask=3320.3, last=3320.1, time=1000)
    assert compute_market_data(asset, spot, moved)['quote_id'] != \
        first['quote_id']       # same second, new price
    later = SimpleNamespace(bid=3319.9, ask=3320.1, last=3320.0, time=1001)
    assert compute_market_data(asset, spot, later)['quote_id'] != \
        first['quote_id']       # same price, new tick


def test_an_asset_with_no_expiry_does_not_crash_the_engine(config):
    """The operator's config had no futures_expiry and startup died
    with KeyError before the coordinator ever ran."""
    asset = dict(config.ASSETS['GOLD'])
    asset.pop('futures_expiry', None)
    config.ASSETS = {'GOLD': asset}
    assert config.validate_expiries() == []      # no crash, nothing stale


def test_without_an_expiry_the_engine_still_has_a_live_spread(config):
    """A zero spread would mean z never moves and nothing ever trades —
    indistinguishable from a dead engine."""
    data = market_data(config, expiry='unset')
    assert data['spread'] == pytest.approx(20.0)        # 3320 - 3300
    assert data['actual_basis'] == pytest.approx(20.0)


def test_the_expiry_does_not_change_the_spread(config):
    """It used to: an expiry switched on a carry adjustment, and an
    EXPIRED one silently switched it back off. The spread is now
    futures minus hedge_ratio x spot whatever the contract says."""
    from datetime import datetime, timedelta
    future = market_data(config, expiry=datetime.now() + timedelta(days=90))
    stale = market_data(config, expiry=datetime.now() - timedelta(days=5))
    unset = market_data(config, expiry='unset')
    assert future['spread'] == stale['spread'] == unset['spread']


def test_a_past_expiry_is_still_reported_as_stale(config):
    """The spread no longer depends on it, but the operator should
    still be told the contract has rolled."""
    from datetime import datetime, timedelta
    stale = datetime.now() - timedelta(days=5)
    config.ASSETS = {'GOLD': dict(config.ASSETS['GOLD'],
                                  futures_expiry=stale)}
    assert config.validate_expiries() == ['GOLD']


def test_saving_a_broker_without_an_expiry_is_accepted(client):
    """The Exchanges page must not require an expiry to save a leg."""
    response = client.post('/api/exchanges', json={
        'name': 'fut', 'role': 'FUTURES', 'symbol': 'XAUUSD.f',
        'endpoint': '127.0.0.1:9102'})
    assert response.status_code == 200
    with open(client.tmp_path / "config.json") as f:
        asset = json.load(f)['assets']['GOLD']
    assert asset['futures_symbols'] == ['XAUUSD.f']
    assert 'futures_expiry' not in asset


# --- symbol resolution at startup ----------------------------------------

class SymbolFakeLeg:
    """A leg that only knows the symbols its broker actually lists."""

    def __init__(self, name, available):
        self.name = name
        self.available = dict(available)   # symbol -> description

    def connect(self):
        return True

    def ping(self):
        return True

    def account_info(self):
        return {'account': self.name, 'login': 1, 'server': 'X',
                'equity': 50000.0}

    def ensure_symbol(self, symbol):
        if symbol in self.available:
            return {'ok': True, 'volume_min': 0.01, 'volume_step': 0.01,
                    'volume_max': 50.0, 'point': 0.01, 'tick_size': 0.01}
        return {'ok': False, 'error': f'{symbol} not found'}

    def find_symbols(self, pattern, limit=40):
        pattern = pattern.upper()
        return [{'symbol': s, 'description': d}
                for s, d in self.available.items()
                if pattern in s.upper() or pattern in d.upper()][:limit]


def symbol_coordinator(config, tmp_path, monkeypatch, spot_leg, fut_leg):
    monkeypatch.chdir(tmp_path)
    from statarb.coordinator import Coordinator
    coord = Coordinator(config, trading_mode='PAPER')
    coord.spot_leg, coord.futures_leg = spot_leg, fut_leg
    return coord


def test_an_asset_resolves_when_both_legs_list_their_symbol(config, tmp_path,
                                                            monkeypatch):
    config.ASSETS = {'GOLD': dict(config.ASSETS['GOLD'],
                                  spot_symbols=['XAUUSD_', 'XAUUSD'],
                                  futures_symbols=['GC1225'])}
    coord = symbol_coordinator(
        config, tmp_path, monkeypatch,
        SymbolFakeLeg('spot_acct', {'XAUUSD_': 'Gold spot'}),
        SymbolFakeLeg('fut_acct', {'GC1225': 'Gold futures'}))
    assert coord._setup_symbols() is True
    assert coord.active_assets['GOLD']['spot_symbol'] == 'XAUUSD_'
    assert coord.active_assets['GOLD']['futures_symbol'] == 'GC1225'


def test_a_missing_futures_symbol_lists_what_the_account_does_offer(
        config, tmp_path, monkeypatch, caplog):
    """The operator's case: spot resolved, futures did not, and the log
    said nothing about what the broker actually lists."""
    config.ASSETS = {'GOLD': dict(config.ASSETS['GOLD'],
                                  spot_symbols=['XAUUSD_'],
                                  futures_symbols=['GC1225', 'XAUUSD.f'])}
    coord = symbol_coordinator(
        config, tmp_path, monkeypatch,
        SymbolFakeLeg('Ut 2', {'XAUUSD_': 'Gold spot'}),
        SymbolFakeLeg('Ut 2', {'XAUUSD_': 'Gold spot',
                               'XAUUSD.fut': 'Gold futures Dec',
                               'GOLDMINI': 'Gold mini'}))
    with caplog.at_level('WARNING'):
        assert coord._setup_symbols() is False
    text = caplog.text
    assert "none of ['GC1225', 'XAUUSD.f'] exist on account 'Ut 2'" in text
    assert 'XAUUSD.fut' in text          # what the broker DOES offer
    assert 'Exchanges page' in text


def test_an_account_with_nothing_matching_says_so(config, tmp_path,
                                                  monkeypatch, caplog):
    """A CFD account with no futures at all — the answer is to point
    the futures leg at the other account, not to keep guessing names."""
    config.ASSETS = {'GOLD': dict(config.ASSETS['GOLD'],
                                  spot_symbols=['XAUUSD_'],
                                  futures_symbols=['GC1225'])}
    coord = symbol_coordinator(
        config, tmp_path, monkeypatch,
        SymbolFakeLeg('spot_acct', {'XAUUSD_': 'Gold spot'}),
        SymbolFakeLeg('spot_acct', {'EURUSD': 'Euro'}))
    with caplog.at_level('WARNING'):
        assert coord._setup_symbols() is False
    assert 'is this the right account for the futures leg?' in caplog.text


def test_one_working_asset_is_enough_to_start(config, tmp_path, monkeypatch):
    """A stale SILVER default must not stop GOLD from trading."""
    config.ASSETS = {
        'GOLD': dict(config.ASSETS['GOLD'], spot_symbols=['XAUUSD_'],
                     futures_symbols=['GC1225']),
        'SILVER': dict(config.ASSETS['SILVER'], spot_symbols=['XAGUSD_'],
                       futures_symbols=['SI1225'])}
    coord = symbol_coordinator(
        config, tmp_path, monkeypatch,
        SymbolFakeLeg('spot_acct', {'XAUUSD_': 'Gold', 'XAGUSD_': 'Silver'}),
        SymbolFakeLeg('fut_acct', {'GC1225': 'Gold futures'}))
    assert coord._setup_symbols() is True
    assert list(coord.active_assets) == ['GOLD']


def test_disabled_assets_are_skipped(config, tmp_path, monkeypatch):
    config.ASSETS = {
        'GOLD': dict(config.ASSETS['GOLD'], spot_symbols=['XAUUSD_'],
                     futures_symbols=['GC1225']),
        'SILVER': dict(config.ASSETS['SILVER'], enabled=False)}
    coord = symbol_coordinator(
        config, tmp_path, monkeypatch,
        SymbolFakeLeg('spot_acct', {'XAUUSD_': 'Gold'}),
        SymbolFakeLeg('fut_acct', {'GC1225': 'Gold futures'}))
    assert coord._setup_symbols() is True
    assert list(coord.active_assets) == ['GOLD']


# --- contract specs come from the terminal, not from typing --------------

class SpecLeg(SymbolFakeLeg):
    def __init__(self, name, available, specs=None):
        super().__init__(name, available)
        self.specs = specs or {}

    def symbol_report(self, symbol):
        if symbol not in self.available:
            return {'symbol': symbol, 'found': False}
        return dict({'symbol': symbol, 'found': True}, **self.specs)


def spec_coordinator(config, tmp_path, monkeypatch, spot_leg, fut_leg):
    monkeypatch.chdir(tmp_path)
    from statarb.coordinator import Coordinator
    coord = Coordinator(config, trading_mode='PAPER')
    coord.spot_leg, coord.futures_leg = spot_leg, fut_leg
    coord._setup_symbols()
    return coord


def gold_only(config, **overrides):
    config.ASSETS = {'GOLD': dict(config.ASSETS['GOLD'],
                                  spot_symbols=['XAUUSD'],
                                  futures_symbols=['GC1225'], **overrides)}
    return config


def test_contract_size_is_read_from_the_broker(config, tmp_path,
                                               monkeypatch):
    """The operator should never have to type this — MT5 knows it, and
    a typed value can only be right by luck."""
    config = gold_only(config, lot_size=1)
    coord = spec_coordinator(
        config, tmp_path, monkeypatch,
        SpecLeg('spot', {'XAUUSD': 'Gold'}, {'contract_size': 100.0}),
        SpecLeg('fut', {'GC1225': 'Gold fut'}, {'contract_size': 100.0}))
    assert coord.config.ASSETS['GOLD']['lot_size'] == 100.0


def test_a_contract_size_that_contradicts_the_config_is_flagged(
        config, tmp_path, monkeypatch, caplog):
    config = gold_only(config, lot_size=100)
    with caplog.at_level('WARNING'):
        coord = spec_coordinator(
            config, tmp_path, monkeypatch,
            SpecLeg('spot', {'XAUUSD': 'Gold'}, {'contract_size': 10.0}),
            SpecLeg('fut', {'GC1225': 'Gold fut'}, {'contract_size': 10.0}))
    assert coord.config.ASSETS['GOLD']['lot_size'] == 10.0
    assert "using the broker's number" in caplog.text


def test_legs_with_different_contract_sizes_warn_about_hedge_ratio(
        config, tmp_path, monkeypatch, caplog):
    config = gold_only(config)
    config.TRADING['HEDGE_RATIO'] = 1.0
    with caplog.at_level('WARNING'):
        spec_coordinator(
            config, tmp_path, monkeypatch,
            SpecLeg('spot', {'XAUUSD': 'Gold'}, {'contract_size': 100.0}),
            SpecLeg('fut', {'GC1225': 'Gold fut'}, {'contract_size': 50.0}))
    assert 'implied by the contract specs is 2.0000' in caplog.text


def test_a_real_futures_expiry_is_picked_up_from_the_broker(
        config, tmp_path, monkeypatch):
    """Set nothing: if the contract genuinely has an expiry, MT5 says
    so and the spread becomes carry-detrended by itself."""
    from datetime import datetime, timedelta
    when = datetime.now() + timedelta(days=45)
    config = gold_only(config)
    config.ASSETS['GOLD'].pop('futures_expiry', None)
    coord = spec_coordinator(
        config, tmp_path, monkeypatch,
        SpecLeg('spot', {'XAUUSD': 'Gold'}, {'contract_size': 100.0}),
        SpecLeg('fut', {'GC1225': 'Gold fut'},
                {'contract_size': 100.0, 'expiry': int(when.timestamp())}))
    assert coord.config.ASSETS['GOLD']['futures_expiry'].date() == when.date()


def test_a_rolling_contract_stays_on_the_raw_basis(config, tmp_path,
                                                   monkeypatch):
    config = gold_only(config)
    config.ASSETS['GOLD'].pop('futures_expiry', None)
    coord = spec_coordinator(
        config, tmp_path, monkeypatch,
        SpecLeg('spot', {'XAUUSD': 'Gold'}, {'contract_size': 100.0}),
        SpecLeg('fut', {'GC1225': 'Gold CFD'},
                {'contract_size': 100.0, 'expiry': 0}))
    assert 'futures_expiry' not in coord.config.ASSETS['GOLD']


def test_legs_without_symbol_reports_still_start(config, tmp_path,
                                                 monkeypatch):
    """Older leg runners have no symbol_report — resolution must not
    depend on it."""
    config = gold_only(config, lot_size=100)
    coord = spec_coordinator(
        config, tmp_path, monkeypatch,
        SymbolFakeLeg('spot', {'XAUUSD': 'Gold'}),
        SymbolFakeLeg('fut', {'GC1225': 'Gold fut'}))
    assert 'GOLD' in coord.active_assets
    assert coord.config.ASSETS['GOLD']['lot_size'] == 100


# --- leg mapping: the engine cannot start without one --------------------

def account_ns(name, endpoint='127.0.0.1:9101'):
    from types import SimpleNamespace
    return SimpleNamespace(name=name, login=1, server='X', password=None,
                           password_env='A', terminal_path=None,
                           endpoint=endpoint)


def coordinator_with(config, tmp_path, monkeypatch, accounts, legs):
    monkeypatch.chdir(tmp_path)
    from statarb.coordinator import Coordinator
    config.accounts = accounts
    config.leg_accounts = legs
    return Coordinator(config, trading_mode='PAPER')


def test_an_unmapped_leg_says_what_to_do_not_keyerror(config, tmp_path,
                                                      monkeypatch):
    """The operator deleted and re-added an account, which cleared the
    leg mapping. It crashed with KeyError: 'default' in a restart loop."""
    with pytest.raises(ValueError) as excinfo:
        coordinator_with(config, tmp_path, monkeypatch,
                         {'Ut 2': account_ns('Ut 2')}, {})
    message = str(excinfo.value)
    assert 'No account is mapped to the SPOT leg' in message
    assert 'Exchanges page' in message
    assert 'Ut 2' in message              # names what IS configured


def test_only_the_futures_leg_missing_is_named(config, tmp_path,
                                               monkeypatch):
    with pytest.raises(ValueError) as excinfo:
        coordinator_with(config, tmp_path, monkeypatch,
                         {'a': account_ns('a')}, {'spot': 'a'})
    assert 'FUTURES leg' in str(excinfo.value)


def test_a_leg_pointing_at_a_deleted_account_is_explained(config, tmp_path,
                                                          monkeypatch):
    with pytest.raises(ValueError) as excinfo:
        coordinator_with(config, tmp_path, monkeypatch,
                         {'a': account_ns('a')},
                         {'spot': 'a', 'futures': 'gone'})
    message = str(excinfo.value)
    assert "'gone', which no longer exists" in message


def test_no_accounts_at_all_says_add_one(config, tmp_path, monkeypatch):
    with pytest.raises(ValueError) as excinfo:
        coordinator_with(config, tmp_path, monkeypatch, {}, {})
    assert 'No MT5 accounts configured' in str(excinfo.value)


def test_a_valid_mapping_still_builds_both_legs(config, tmp_path,
                                                monkeypatch):
    coord = coordinator_with(
        config, tmp_path, monkeypatch,
        {'a': account_ns('a', '127.0.0.1:9101'),
         'b': account_ns('b', '127.0.0.1:9102')},
        {'spot': 'a', 'futures': 'b'})
    assert coord.spot_leg.name == 'a' and coord.futures_leg.name == 'b'


def test_the_exchanges_page_warns_before_it_becomes_a_crash_loop(client):
    page = client.get('/setup').get_data(as_text=True)
    assert 'leg-mapping-warning' in page
    assert 'cannot start' in page
