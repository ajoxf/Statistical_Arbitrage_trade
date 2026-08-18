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
        'endpoint': '127.0.0.1:9102'})
    with open(client.tmp_path / "config.json") as f:
        raw = json.load(f)
    assert raw['assets']['GOLD']['spot_symbols'] == ['GOLD.r']
    assert raw['leg_accounts']['spot'] == 'spot_fxpro'


def test_a_symbol_without_a_leg_is_refused(client):
    response = client.post('/api/exchanges', json={
        'name': 'mystery', 'symbol': 'XAUUSD'})
    assert response.status_code == 400
    assert 'leg' in response.get_json()['error'].lower()


def test_the_broker_row_reports_the_symbol_it_trades(client):
    client.post('/api/exchanges', json={
        'name': 'account_a', 'role': 'SPOT', 'symbol': 'XAUUSD',
        'endpoint': '127.0.0.1:9102'})
    client.post('/api/exchanges', json={
        'name': 'fut', 'role': 'FUTURES', 'symbol': 'GC1225',
        'endpoint': '127.0.0.1:9103'})
    rows = {b['id']: b for b in client.get('/api/exchanges').get_json()}
    assert rows['account_a']['symbol'] == 'XAUUSD'
    assert rows['account_a']['role'] == 'SPOT'
    assert rows['fut']['symbol'] == 'GC1225'
    assert rows['fut']['role'] == 'FUTURES'


def test_editing_a_broker_keeps_the_other_legs_symbol(client):
    client.post('/api/exchanges', json={'name': 'a', 'role': 'SPOT',
                                        'symbol': 'XAUUSD',
                                        'endpoint': '127.0.0.1:9102'})
    client.post('/api/exchanges', json={'name': 'b', 'role': 'FUTURES',
                                        'symbol': 'GC1225',
                                        'endpoint': '127.0.0.1:9103'})
    client.post('/api/exchanges', json={'name': 'a', 'role': 'SPOT',
                                        'symbol': 'GOLD',
                                        'endpoint': '127.0.0.1:9102'})
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


def test_each_legs_own_contract_size_is_adopted(
        config, tmp_path, monkeypatch, caplog):
    """sizing.plan reads leg B's size as `fut_lot_size` and falls back
    to leg A's when unset. Nothing wrote it, so two legs with different
    contract sizes were sized as if they matched — silently, and by the
    exact ratio between them."""
    config = gold_only(config)
    config.TRADING['HEDGE_RATIO'] = 1.0
    with caplog.at_level('WARNING'):
        coord = spec_coordinator(
            config, tmp_path, monkeypatch,
            SpecLeg('spot', {'XAUUSD': 'Gold'}, {'contract_size': 100.0}),
            SpecLeg('fut', {'GC1225': 'Gold fut'}, {'contract_size': 50.0}))
    asset = coord.config.ASSETS['GOLD']
    assert asset['lot_size'] == 100.0
    assert asset['fut_lot_size'] == 50.0

    # The mismatch is reported, but NOT as a reason to retune beta:
    # HEDGE_RATIO is the spread's price coefficient, and the hedge
    # formula already divides by each leg's contract size.
    assert 'spot is 100/lot but futures is 50/lot' in caplog.text
    assert 'NOT a reason to change HEDGE_RATIO' in caplog.text
    assert 'implied by the contract specs' not in caplog.text


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


def test_both_legs_on_one_account_saves_the_spot_symbol(client):
    """Live 2026-08-10: on a one-account setup the operator saved USOIL
    as Leg A and it kept reverting to the old symbol, while the futures
    symbol beside it saved every time.

    The BOTH row labels its first box "Leg A — Spot symbol" and posts it
    as `symbol`; the save matched that field only for SPOT and FUTURES,
    so on BOTH it was silently dropped.
    """
    def saved():
        with open(client.tmp_path / 'config.json') as f:
            return json.load(f)

    response = client.post('/api/exchanges', json={
        'name': 'Mento Markets', 'role': 'BOTH', 'endpoint': '127.0.0.1:9102',
        'symbol': 'USOIL', 'futures_symbol': 'UKOIL'})
    assert response.status_code == 200, response.get_json()

    raw = saved()
    asset = next(iter(raw['assets'].values()))
    assert asset['spot_symbols'] == ['USOIL']
    assert asset['futures_symbols'] == ['UKOIL']
    assert raw['leg_accounts'] == {'spot': 'Mento Markets',
                                   'futures': 'Mento Markets'}

    # ...and it survives a re-save, which is what "it goes back" meant
    client.post('/api/exchanges', json={
        'name': 'Mento Markets', 'role': 'BOTH', 'endpoint': '127.0.0.1:9102',
        'symbol': 'USOIL', 'futures_symbol': 'UKOIL'})
    asset = next(iter(saved()['assets'].values()))
    assert asset['spot_symbols'] == ['USOIL']

    # The row reads back with BOTH roles and BOTH symbols, so the form
    # repopulates with what was actually saved.
    row = {b['id']: b for b in client.get('/api/exchanges').get_json()}
    assert row['Mento Markets']['roles'] == ['SPOT', 'FUTURES']
    assert row['Mento Markets']['spot_symbol'] == 'USOIL'
    assert row['Mento Markets']['futures_symbol'] == 'UKOIL'


def test_a_symbol_with_no_leg_chosen_is_still_refused(client):
    response = client.post('/api/exchanges', json={
        'name': 'nowhere', 'endpoint': '127.0.0.1:9109', 'symbol': 'USOIL'})
    assert response.status_code == 400
    assert 'leg' in response.get_json()['error'].lower()


# --- HEDGE_RATIO follows the pair ----------------------------------------
# Operator, 2026-08-10: "Can you make sure the Hedge Ratio is calculated
# and changed everytime the pair is changed?" — after 66.94, computed for
# XAGUSD/XAUUSD, was left behind on USOIL/UKOIL and defined the spread as
# -5469.59 on legs priced 82.61 and 86.05.

class PricedLeg(SymbolFakeLeg):
    """A leg that also quotes, so beta can be derived from live prices."""

    def __init__(self, name, available, price):
        super().__init__(name, available)
        self.price = price

    def tick(self, symbol):
        if symbol not in self.available:
            return None
        return {'bid': self.price - 0.05, 'ask': self.price + 0.05,
                'time': 0}


def oil_coordinator(config, tmp_path, monkeypatch, beta, stamp,
                    pair_type='RELATED', legs=('USOIL', 'UKOIL'),
                    prices=(82.61, 86.05)):
    monkeypatch.chdir(tmp_path)
    (tmp_path / 'config.json').write_text(json.dumps(
        {'trading': {'HEDGE_RATIO': beta}}))
    from statarb.coordinator import Coordinator
    config.ASSETS = {'OIL': dict(config.ASSETS['GOLD'],
                                 spot_symbols=[legs[0]],
                                 futures_symbols=[legs[1]],
                                 pair_type=pair_type)}
    config.TRADING['HEDGE_RATIO'] = beta
    if stamp is None:
        config.TRADING.pop('HEDGE_RATIO_FOR', None)
    else:
        config.TRADING['HEDGE_RATIO_FOR'] = stamp
    coord = Coordinator(config, trading_mode='PAPER',
                        config_path=str(tmp_path / 'config.json'))
    coord.spot_leg = PricedLeg('MT5', {legs[0]: 'Leg A'}, prices[0])
    coord.futures_leg = PricedLeg('MT5', {legs[1]: 'Leg B'}, prices[1])
    return coord


# WTI at 83 and Brent at 86 are the SAME scale, so the derived beta is
# 1 and the spread is their difference. Silver at 65 against gold at
# 4,352 is not, and there the price ratio is the only thing that makes
# subtracting them mean anything.
SCALED = {'legs': ('XAGUSD', 'XAUUSD'), 'prices': (64.686, 4352.26)}


def test_changing_the_pair_re_derives_the_hedge_ratio(config, tmp_path,
                                                      monkeypatch, caplog):
    """The live incident: 66.94 belonged to XAGUSD/XAUUSD."""
    coord = oil_coordinator(config, tmp_path, monkeypatch, 66.94,
                            'XAGUSD|XAUUSD')
    with caplog.at_level('WARNING'):
        assert coord._setup_symbols() is True
    assert config.TRADING['HEDGE_RATIO'] == 1.0
    assert config.TRADING['HEDGE_RATIO_FOR'] == 'USOIL|UKOIL'
    assert 'XAGUSD/XAUUSD' in caplog.text        # names the pair it left


def test_the_new_hedge_ratio_reaches_config_json(config, tmp_path,
                                                 monkeypatch):
    """In-memory only would leave the Exchanges checklist reporting a
    fault the engine had already corrected — the same unfixable warning
    the contract-size adoption exists to avoid."""
    coord = oil_coordinator(config, tmp_path, monkeypatch, 66.94,
                            'XAGUSD|XAUUSD')
    coord._setup_symbols()
    saved = json.loads((tmp_path / 'config.json').read_text())['trading']
    assert saved['HEDGE_RATIO'] == 1.0
    assert saved['HEDGE_RATIO_FOR'] == 'USOIL|UKOIL'


def test_a_hedge_ratio_set_for_THIS_pair_is_left_alone(config, tmp_path,
                                                       monkeypatch):
    """Beta is a strategy parameter. An operator who tuned it on their
    own pair keeps their number — the stamp is what separates a tuned
    beta from a stale one."""
    coord = oil_coordinator(config, tmp_path, monkeypatch, 1.0,
                            'USOIL|UKOIL')
    coord._setup_symbols()
    assert config.TRADING['HEDGE_RATIO'] == 1.0


def test_an_unstamped_but_plausible_ratio_is_kept_and_stamped(config,
                                                              tmp_path,
                                                              monkeypatch):
    """An install predating the stamp: which pair the number was meant
    for is unknowable, so a usable value is not second-guessed."""
    coord = oil_coordinator(config, tmp_path, monkeypatch, 1.0, None)
    coord._setup_symbols()
    assert config.TRADING['HEDGE_RATIO'] == 1.0
    assert config.TRADING['HEDGE_RATIO_FOR'] == 'USOIL|UKOIL'


def test_an_unstamped_impossible_ratio_is_corrected(config, tmp_path,
                                                    monkeypatch):
    """66.94 on legs priced 82.61 and 86.05 settles the question by
    itself: no operator chose a spread of -5469."""
    coord = oil_coordinator(config, tmp_path, monkeypatch, 66.94, None)
    coord._setup_symbols()
    assert config.TRADING['HEDGE_RATIO'] == 1.0


def test_an_open_position_freezes_the_hedge_ratio(config, tmp_path,
                                                  monkeypatch, caplog):
    """Beta defines the series a position was entered on. Redefining it
    underneath a live trade orphans its entry geometry."""
    coord = oil_coordinator(config, tmp_path, monkeypatch, 66.94,
                            'XAGUSD|XAUUSD')
    coord.data_logger.load_open_position_states = lambda: [{'id': 'POS_0001'}]
    with caplog.at_level('CRITICAL'):
        coord._setup_symbols()
    assert config.TRADING['HEDGE_RATIO'] == 66.94
    assert 'NOT changing it' in caplog.text
    assert 'Close them' in caplog.text


def test_a_basis_pair_is_re_derived_to_one(config, tmp_path, monkeypatch):
    """Same underlying: the spread IS the basis, so beta is 1 — and it
    needs no price, which is why a quiet feed cannot block it."""
    coord = oil_coordinator(config, tmp_path, monkeypatch, 66.94,
                            'XAGUSD|XAUUSD', pair_type='SPOT_FUTURE')
    coord._setup_symbols()
    assert config.TRADING['HEDGE_RATIO'] == 1.0


def test_the_ratio_is_settled_before_the_window_is_seeded(config, tmp_path,
                                                          monkeypatch):
    """`_series_key` includes beta and `_warm_start` seeds from rows
    matching it, so a beta changed after the warm start would hand the
    strategy a mu and sigma the live spread never visits. The adoption
    therefore runs inside _setup_symbols, which start() calls first."""
    coord = oil_coordinator(config, tmp_path, monkeypatch, 66.94,
                            'XAGUSD|XAUUSD')
    before = coord._series_key('OIL')
    coord._setup_symbols()
    assert coord._series_key('OIL') != before
    assert '1.000000' in coord._series_key('OIL')


def test_a_hand_set_ratio_is_stamped_so_a_restart_keeps_it(client):
    """Change the symbols, type the right beta in Settings, restart —
    without stamping the operator's own save the engine would see a
    stamp naming the OLD pair and overwrite the number just typed."""
    client.post('/api/exchanges', json={
        'name': 'Mento Markets', 'role': 'BOTH', 'endpoint': '127.0.0.1:9102',
        'symbol': 'USOIL', 'futures_symbol': 'UKOIL'})
    response = client.post('/api/config', json={'hedge_ratio': 1.0416})
    assert response.status_code == 200
    with open(client.tmp_path / 'config.json') as f:
        trading = json.load(f)['trading']
    assert trading['HEDGE_RATIO'] == 1.0416
    assert trading['HEDGE_RATIO_FOR'] == 'USOIL|UKOIL'


def test_two_instruments_on_different_scales_get_the_price_ratio(
        config, tmp_path, monkeypatch):
    """Silver at 65 against gold at 4,352: beta 1 there is not a spread,
    it is gold's own price with a rounding error subtracted."""
    coord = oil_coordinator(config, tmp_path, monkeypatch, 1.0,
                            'USOIL|UKOIL', **SCALED)
    coord._setup_symbols()
    assert config.TRADING['HEDGE_RATIO'] == pytest.approx(
        4352.26 / 64.686, rel=1e-4)


def test_a_same_scale_pair_keeps_the_level_that_names_the_trade(
        config, tmp_path, monkeypatch):
    """Operator, 2026-08-10: "Why is the spread Incorrect?" — the oil
    spread had gone from +3.30 to -0.05. Nothing was miscomputed; beta
    had been set to the price ratio, which centres the series on zero
    by construction and throws away the differential the pair is
    traded on."""
    coord = oil_coordinator(config, tmp_path, monkeypatch, 66.94,
                            'XAGUSD|XAUUSD')
    coord._setup_symbols()
    beta = config.TRADING['HEDGE_RATIO']
    assert 86.05 - beta * 82.61 == pytest.approx(3.44, abs=0.01)


# --- one port, one leg runner --------------------------------------------
# Live 2026-08-11, adding a second account: 'Utsav Khanchandani' and
# 'MT5' were both saved at 127.0.0.1:9101. Only one process can bind a
# port, so the second runner cannot start — or, if the first won the
# race, BOTH legs connect to it and trade the SAME MT5 account while
# every screen reports two.

def test_a_second_account_cannot_take_a_used_endpoint(client):
    response = client.post('/api/exchanges', json={
        'name': 'MT5', 'login': 100002, 'endpoint': '127.0.0.1:9101'})
    assert response.status_code == 400
    error = response.get_json()['error']
    assert 'account_a' in error and '127.0.0.1:9101' in error
    assert 'MT5' not in saved_accounts(client)


def test_the_refusal_offers_a_port_that_is_free(client):
    response = client.post('/api/exchanges', json={
        'name': 'MT5', 'endpoint': '127.0.0.1:9101'})
    assert '127.0.0.1:9102' in response.get_json()['error']


def test_an_account_may_keep_its_own_endpoint(client):
    """Re-saving a row must not collide with itself."""
    assert client.post('/api/exchanges', json={
        'name': 'account_a', 'login': 111,
        'endpoint': '127.0.0.1:9101'}).status_code == 200


def test_blank_endpoints_never_collide(client):
    """Blank means "no leg runner" — any number of accounts may have
    one, and the one-account topology depends on it."""
    client.post('/api/exchanges', json={'name': 'x', 'endpoint': ''})
    assert client.post('/api/exchanges',
                       json={'name': 'y', 'endpoint': ''}).status_code == 200


def test_the_bulk_save_rejects_a_shared_endpoint(client):
    response = client.post('/api/config', json={'accounts': {
        'a': {'login': 1, 'endpoint': '127.0.0.1:9101'},
        'b': {'login': 2, 'endpoint': '127.0.0.1:9101'}}})
    assert response.status_code == 400
    assert '9101' in response.get_json()['error']


def test_the_coordinator_refuses_two_legs_on_one_port(config, tmp_path,
                                                      monkeypatch):
    """It must never half-work: both legs silently on one account is
    worse than not starting."""
    monkeypatch.chdir(tmp_path)
    from statarb.coordinator import Coordinator
    config.accounts = {'a': account_ns('a', '127.0.0.1:9101'),
                       'b': account_ns('b', '127.0.0.1:9101')}
    config.leg_accounts = {'spot': 'a', 'futures': 'b'}
    with pytest.raises(ValueError) as excinfo:
        Coordinator(config, trading_mode='PAPER')
    message = str(excinfo.value)
    assert '127.0.0.1:9101' in message
    assert 'same terminal' in message or 'got there first' in message


def test_one_account_on_both_legs_is_still_fine(config, tmp_path,
                                                monkeypatch):
    """The same account on both legs is ONE runner and one port — the
    supported single-account topology, not a collision."""
    monkeypatch.chdir(tmp_path)
    from statarb.coordinator import Coordinator
    config.accounts = {'solo': account_ns('solo', '127.0.0.1:9101')}
    config.leg_accounts = {'spot': 'solo', 'futures': 'solo'}
    coord = Coordinator(config, trading_mode='PAPER')
    assert coord.spot_leg is coord.futures_leg


def test_an_unreachable_leg_is_logged_once_not_every_poll(caplog):
    """The webapp opens a short-lived RemoteLeg every 15s for each leg,
    so a runner that is down wrote hundreds of identical WARNING lines
    and buried the coordinator's own output."""
    from statarb.legs import RemoteLeg
    RemoteLeg._reported.clear()
    with caplog.at_level('WARNING'):
        for _ in range(3):
            RemoteLeg('dead', '127.0.0.1:9  ' .strip() or '127.0.0.1:9')\
                .connect(retries=1, delay=0)
    warnings = [r for r in caplog.records if r.levelname == 'WARNING']
    assert len(warnings) == 1
    assert 'dead' in warnings[0].getMessage()


# --- a symbol belongs to a LEG, not to the box it was typed in -----------
# Live 2026-08-11: a row holding UKOIL as Leg B was switched to Leg A and
# saved. The single Symbol box still carried UKOIL, so spot_symbols became
# UKOIL and the futures leg was released — the page read "MT5 · SPOT ·
# UKOIL" with FUTURES unmapped, one keystroke from Brent against Brent.

def test_the_symbol_box_follows_the_leg_selector():
    """The store is keyed by LEG, so changing the selector re-renders
    rather than carrying the previous leg's symbol across."""
    from tests.test_nexus_ui import template_source
    page = template_source('setup.html')
    block = page[page.index('function updateLegFields'):]
    block = block[:block.index('\n}')]
    assert "_LEG_SYMBOLS.futures : _LEG_SYMBOLS.spot" in block
    # and the store is kept current as the operator types, so the
    # selector never has to guess which leg the box meant
    assert '_rememberVisibleSymbols(); suggestSymbols()' in page


def test_one_account_cannot_put_the_same_symbol_on_both_legs(client):
    client.post('/api/exchanges', json={
        'name': 'account_a', 'role': 'BOTH', 'endpoint': '127.0.0.1:9101',
        'symbol': 'UKOIL', 'futures_symbol': 'UKOIL'})
    response = client.post('/api/exchanges', json={
        'name': 'account_a', 'role': 'BOTH', 'endpoint': '127.0.0.1:9101',
        'symbol': 'UKOIL', 'futures_symbol': 'UKOIL'})
    assert response.status_code == 400
    assert 'two different instruments' in response.get_json()['error']


def test_two_accounts_may_share_a_symbol_name(client):
    """The cross-broker case: USOIL at one broker against USOIL at
    another is a real spread, not a mistake."""
    client.post('/api/exchanges', json={
        'name': 'account_a', 'role': 'SPOT', 'endpoint': '127.0.0.1:9101',
        'symbol': 'USOIL'})
    response = client.post('/api/exchanges', json={
        'name': 'broker_b', 'role': 'FUTURES', 'endpoint': '127.0.0.1:9102',
        'symbol': 'USOIL'})
    assert response.status_code == 200


# --- the launcher waits for PORTS, not the clock -------------------------
# Live 2026-08-11: leg runner 'MM - MT5 - 2' stayed alive with its port
# shut (mt5.initialize was waiting for a terminal to log in), the
# launcher slept 3s and started the coordinator anyway, and the console
# became a coordinator restart loop with the real cause scrolled away.

def _leg_config(port=9109):
    return {'accounts': {'A': {'endpoint': f'127.0.0.1:{port}',
                               'login': 1, 'terminal_path': 'x'}},
            'leg_accounts': {'spot': 'A', 'futures': 'A'}}


def test_the_launcher_reports_a_leg_runner_that_never_listens(capsys):
    import start
    assert start.wait_for_leg_runners(_leg_config(), timeout=0.5) is False
    out = capsys.readouterr().out
    assert 'has NOT opened 127.0.0.1:9109' in out
    assert 'leg_A.log' in out          # where to look next


def test_the_launcher_proceeds_once_the_port_is_open(capsys):
    import socket as _socket
    import start
    listener = _socket.socket()
    listener.bind(('127.0.0.1', 0))
    listener.listen(1)
    port = listener.getsockname()[1]
    try:
        assert start.wait_for_leg_runners(_leg_config(port),
                                          timeout=5.0) is True
    finally:
        listener.close()
    assert 'is listening' in capsys.readouterr().out


def test_no_leg_runners_means_nothing_to_wait_for():
    import start
    assert start.wait_for_leg_runners(
        {'accounts': {'A': {'endpoint': ''}},
         'leg_accounts': {'spot': 'A', 'futures': 'A'}}) is True


# --- the ROLE decides which leg a symbol lands on ------------------------
# Live 2026-08-11, twice: "After saving EU50 on Leg B and restarting it
# still saves UKOIL on Leg B". The second symbol box is hidden on a
# single-leg row but a hidden input still SUBMITS, so the save posted
# symbol=EU50 alongside a stale futures_symbol=UKOIL — and the server's
# `fut_symbol or symbol` preferred the one the operator could not see.

def test_a_futures_row_takes_the_symbol_that_was_typed(client):
    client.post('/api/exchanges', json={
        'name': 'legb', 'role': 'FUTURES', 'endpoint': '127.0.0.1:9102',
        'symbol': 'UKOIL'})
    response = client.post('/api/exchanges', json={
        'name': 'legb', 'role': 'FUTURES', 'endpoint': '127.0.0.1:9102',
        'symbol': 'EU50', 'futures_symbol': 'UKOIL'})   # stale hidden box
    assert response.status_code == 200
    with open(client.tmp_path / 'config.json') as f:
        asset = next(iter(json.load(f)['assets'].values()))
    assert asset['futures_symbols'] == ['EU50']


def test_a_spot_row_ignores_a_stray_futures_symbol(client):
    client.post('/api/exchanges', json={
        'name': 'account_a', 'role': 'SPOT', 'endpoint': '127.0.0.1:9101',
        'symbol': 'GER40', 'futures_symbol': 'UKOIL'})
    with open(client.tmp_path / 'config.json') as f:
        asset = next(iter(json.load(f)['assets'].values()))
    assert asset['spot_symbols'] == ['GER40']
    assert 'futures_symbols' not in asset      # never claimed that leg


def test_a_both_row_still_takes_two_symbols(client):
    client.post('/api/exchanges', json={
        'name': 'account_a', 'role': 'BOTH', 'endpoint': '127.0.0.1:9101',
        'symbol': 'GER40', 'futures_symbol': 'EU50'})
    with open(client.tmp_path / 'config.json') as f:
        asset = next(iter(json.load(f)['assets'].values()))
    assert asset['spot_symbols'] == ['GER40']
    assert asset['futures_symbols'] == ['EU50']


def test_the_hidden_futures_box_is_not_submitted_on_a_single_leg_row():
    """The client half of the same fault: a hidden input still posts."""
    from tests.test_nexus_ui import template_source
    block = template_source('setup.html')
    block = block[block.index('function updateLegFields'):]
    block = block[:block.index('\n}')]
    assert "getElementById('broker-fut-symbol').disabled = !both" in block


# --- a half-read config must never become a saved one --------------------
# Live 2026-08-11: the Exchanges page read "No accounts configured yet"
# while the engine was trading two of them. The coordinator rewrites
# config.json when it adopts broker specs, and it truncated the file in
# place — so a page load caught it half-written, load_config_raw turned
# the parse error into {}, and the next save wrote that back over the
# accounts, the leg mapping and every symbol.

def test_an_unreadable_config_falls_back_to_the_backup(client):
    good = json.loads((client.tmp_path / 'config.json').read_text())
    (client.tmp_path / 'config.json.bak').write_text(json.dumps(good))
    (client.tmp_path / 'config.json').write_text('{"accounts": {"a": ')
    rows = client.get('/api/exchanges').get_json()
    assert [r['id'] for r in rows] == list(good['accounts'])


def test_an_unreadable_config_with_no_backup_refuses_rather_than_empties(
        client):
    (client.tmp_path / 'config.json').write_text('{"accounts": {"a": ')
    response = client.get('/api/exchanges')
    assert response.status_code == 503
    assert 'could not be read' in response.get_json()['error']


def test_a_save_cannot_drop_the_accounts_it_never_touched(client):
    """The guard that would have prevented it: a partial edit must not
    be able to remove a section it was not editing."""
    from statarb.webapp import create_app       # noqa: F401  (documents intent)
    path = client.tmp_path / 'config.json'
    good = json.loads(path.read_text())
    assert good['accounts']
    # A handler that lost the accounts — exactly what a {} read produces.
    import statarb.webapp as webapp_mod
    response = client.post('/api/config', json={'sections': {
        'SIGNALS': {'ENTRY_Z': 2.5}}})
    assert response.status_code == 200
    assert json.loads(path.read_text())['accounts'] == good['accounts']


def test_deleting_an_account_is_still_allowed_to_empty_it(client):
    """The guard must not make the delete button impossible."""
    assert client.delete('/api/exchanges/account_a').status_code == 200
    saved = json.loads((client.tmp_path / 'config.json').read_text())
    assert saved.get('accounts') in ({}, None)


def test_a_good_save_leaves_a_backup_behind(client):
    before = json.loads((client.tmp_path / 'config.json').read_text())
    client.post('/api/config', json={'sections': {'SIGNALS': {'ENTRY_Z': 2.5}}})
    backup = json.loads((client.tmp_path / 'config.json.bak').read_text())
    assert backup['accounts'] == before['accounts']


# --- unused accounts are inert, and easy to clear ------------------------
# Operator, 2026-08-11: "At any point we should only have 2 accounts
# connected, right? So, when new accounts are added the old ones should
# get deleted." Two is right for CONNECTED — the launcher starts a runner
# only for a leg's account — but an unmapped row connects to nothing, and
# deleting it automatically would make "add" silently mean "replace" on a
# row carrying a login, a server and a link to its password.

def test_an_account_with_no_leg_starts_no_runner():
    import start
    raw = {'accounts': {'live': {'endpoint': '127.0.0.1:9101'},
                        'leftover': {'endpoint': '127.0.0.1:9103'}},
           'leg_accounts': {'spot': 'live', 'futures': 'live'}}
    assert start.plan_leg_runners(raw) == ['live']


def test_at_most_two_runners_however_many_accounts_exist():
    import start
    raw = {'accounts': {n: {'endpoint': f'127.0.0.1:910{i}'}
                        for i, n in enumerate('abcde')},
           'leg_accounts': {'spot': 'a', 'futures': 'b'}}
    assert start.plan_leg_runners(raw) == ['a', 'b']


def test_the_page_offers_to_clear_unused_accounts_rather_than_doing_it():
    from tests.test_nexus_ui import template_source
    page = template_source('setup.html')
    assert 'removeUnusedAccounts()' in page
    assert 'unused — not connected' in page
    # Explicitly confirmed, and named, before anything is removed.
    body = page[page.index('async function removeUnusedAccounts'):]
    body = body[:body.index('\nasync function deleteBroker')]
    assert 'showConfirm' in body and 'names.join' in body
    assert 'passwords stay in' in body


def test_removing_an_unused_account_leaves_the_mapped_ones(client):
    client.post('/api/exchanges', json={
        'name': 'leftover', 'endpoint': '127.0.0.1:9109'})
    assert 'leftover' in saved_accounts(client)
    assert client.delete('/api/exchanges/leftover').status_code == 200
    remaining = saved_accounts(client)
    assert 'leftover' not in remaining and 'account_a' in remaining
    with open(client.tmp_path / 'config.json') as f:
        assert json.load(f)['leg_accounts']['spot'] == 'account_a'


def test_two_accounts_cannot_share_one_login(client):
    """One terminal holds one login, so two accounts on one login is
    the same MT5 account twice — both legs would trade it and hedge
    against themselves while the UI reports two."""
    client.post('/api/exchanges', json={
        'name': 'Account_Spot', 'login': 100006, 'role': 'SPOT',
        'endpoint': '127.0.0.1:9102'})
    response = client.post('/api/exchanges', json={
        'name': 'Account_Future', 'login': 100006, 'role': 'FUTURES',
        'endpoint': '127.0.0.1:9103'})
    assert response.status_code == 400
    error = response.get_json()['error']
    assert 'Account_Spot' in error and '100006' in error


def test_re_saving_an_account_keeps_its_own_login(client):
    client.post('/api/exchanges', json={
        'name': 'Account_Spot', 'login': 100006,
        'endpoint': '127.0.0.1:9102'})
    assert client.post('/api/exchanges', json={
        'name': 'Account_Spot', 'login': 100006,
        'endpoint': '127.0.0.1:9102'}).status_code == 200


def test_the_launcher_says_a_config_change_needs_a_relaunch(tmp_path,
                                                            monkeypatch,
                                                            capsys):
    """Live 2026-08-18: the watchdog gave up, the operator added the
    accounts in the web UI, and the console never mentioned it again.
    Leg runners are planned from the account list at STARTUP, so no
    amount of coordinator retrying picks a new account up."""
    import threading
    import time as _time
    import start
    monkeypatch.chdir(tmp_path)
    cfg = tmp_path / 'config.json'
    cfg.write_text('{}')
    stop = threading.Event()
    thread = threading.Thread(target=start.monitor,
                              args=([], stop, str(cfg)), daemon=True)
    thread.start()
    _time.sleep(0.2)
    _time.sleep(1.1)                       # past the mtime resolution
    cfg.write_text('{"accounts": {"a": {}}}')
    _time.sleep(3.0)
    stop.set()
    thread.join(timeout=5)
    out = capsys.readouterr().out
    assert 'config.json changed' in out
    assert 'start it again' in out
