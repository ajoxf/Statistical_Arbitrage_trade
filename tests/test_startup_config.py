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
