"""Launcher topology planning, UI-managed secrets, MT5 self-tests,
and the extended Telegram command set."""

import json
import os

import pytest

from start import plan_leg_runners
from statarb.webapp import update_env_file


def test_plan_leg_runners_three_topologies():
    # Two brokers, two accounts with endpoints -> two runners
    two = {'accounts': {'a': {'endpoint': '127.0.0.1:9101'},
                        'b': {'endpoint': '127.0.0.1:9102'}},
           'leg_accounts': {'spot': 'a', 'futures': 'b'}}
    assert plan_leg_runners(two) == ['a', 'b']
    # Same broker, two accounts -> still two runners
    two['accounts']['b']['endpoint'] = '127.0.0.1:9103'
    assert plan_leg_runners(two) == ['a', 'b']
    # One account, both legs, no endpoint -> coordinator in-process
    one = {'accounts': {'a': {}},
           'leg_accounts': {'spot': 'a', 'futures': 'a'}}
    assert plan_leg_runners(one) == []


def test_update_env_file_merges_and_preserves(tmp_path, monkeypatch):
    monkeypatch.setattr(os, 'environ', dict(os.environ))
    env = tmp_path / ".env"
    env.write_text("# managed\nEXISTING=keep\nMT5_PASSWORD_A=old\n")
    update_env_file(str(env), {'MT5_PASSWORD_A': 'new-secret',
                               'TELEGRAM_BOT_TOKEN': 'tok'})
    content = env.read_text()
    assert 'EXISTING=keep' in content
    assert 'MT5_PASSWORD_A="new-secret"' in content   # quoted: a
    # password with spaces or # must survive dotenv
    assert 'old' not in content
    assert 'TELEGRAM_BOT_TOKEN="tok"' in content
    assert os.environ['MT5_PASSWORD_A'] == 'new-secret'


def test_password_saved_to_env_not_config(tmp_path, monkeypatch):
    monkeypatch.setattr(os, 'environ', dict(os.environ))
    from statarb.webapp import create_app
    config_path = tmp_path / "config.json"
    env_path = tmp_path / ".env"
    config_path.write_text(json.dumps({
        'accounts': {'account_a': {'login': 1}},
        'leg_accounts': {'spot': 'account_a', 'futures': 'account_a'}}))
    app = create_app(db_path=str(tmp_path / "d.db"),
                     status_path=str(tmp_path / "s.json"),
                     config_path=str(config_path),
                     control_path=str(tmp_path / "c.json"),
                     env_path=str(env_path))
    app.config['TESTING'] = True
    client = app.test_client()

    response = client.post('/api/config', json={
        'accounts': {'account_a': {'login': 1,
                                   '_password': 'S3cret!'}},
        'trading_mode': 'paper'})
    assert response.status_code == 200

    saved = json.loads(config_path.read_text())
    assert 'S3cret!' not in config_path.read_text()   # NEVER in config
    assert saved['accounts']['account_a']['password_env'] == \
        'MT5_PASSWORD_ACCOUNT_A'
    assert 'MT5_PASSWORD_ACCOUNT_A="S3cret!"' in env_path.read_text()


def test_order_selftest_via_control(tmp_path, monkeypatch, config):
    monkeypatch.chdir(tmp_path)
    from statarb.coordinator import Coordinator
    from tests.test_limit_execution import LimitFakeLeg

    LimitFakeLeg.ping = lambda self: True
    LimitFakeLeg.account_info = lambda self: {
        'login': 1, 'server': 'Fake', 'balance': 1e6, 'equity': 1e6}

    coordinator = Coordinator(config, trading_mode='PAPER')
    spot_leg = LimitFakeLeg('a', price=3300.0,
                            limit_fill_polls={'XAUUSD': None})
    fut_leg = LimitFakeLeg('b', price=3320.0,
                           limit_fill_polls={'GC1225': None})
    coordinator.spot_leg, coordinator.futures_leg = spot_leg, fut_leg
    coordinator.active_assets['GOLD'] = {
        'config': config.ASSETS['GOLD'], 'spot_symbol': 'XAUUSD',
        'futures_symbol': 'GC1225', 'last_data': None}

    # Algo on -> order test refused by precondition
    coordinator._run_tests('orders')
    assert not coordinator._test_results['results'][0]['ok']

    coordinator.algo_enabled = False
    coordinator._run_tests('orders')
    results = coordinator._test_results['results']
    checks = {(r['leg'], r['check']): r['ok'] for r in results}
    assert checks[('a', 'connection ping')]
    assert checks[('a', 'limit place XAUUSD')]
    assert checks[('a', 'limit resting XAUUSD')]
    assert checks[('a', 'limit cancel XAUUSD')]
    assert checks[('a', 'market open XAUUSD')]
    assert checks[('a', 'round trip close XAUUSD')]
    assert checks[('b', 'round trip close GC1225')]
    assert all(r['ok'] for r in results)
    # The round trip actually closed by ticket
    assert spot_leg.closed_tickets and fut_leg.closed_tickets


def test_telegram_command_set(tmp_path, monkeypatch, config):
    monkeypatch.chdir(tmp_path)
    from statarb.coordinator import Coordinator
    coordinator = Coordinator(config, trading_mode='PAPER')

    assert 'Mode' in coordinator._telegram_command('/status')
    assert coordinator._telegram_command('/positions') == \
        'No open positions'
    assert 'P&L' in coordinator._telegram_command('/pnl')
    assert 'No closed trades' in coordinator._telegram_command('/trades')
    assert 'No closed trades' in coordinator._telegram_command('/stats')
    assert 'SETTINGS' in coordinator._telegram_command('/settings')
    assert 'END-OF-DAY' in coordinator._telegram_command('/eod')

    # pause/resume flip the entry gate
    assert 'PAUSED' in coordinator._telegram_command('/pause')
    assert not coordinator.algo_enabled
    assert 'RESUMED' in coordinator._telegram_command('/resume')
    assert coordinator.algo_enabled

    # /set changes a live value, refuses beta
    reply = coordinator._telegram_command('/set ENTRY_Z 2.5')
    assert '3.0 → 2.5' in reply
    assert coordinator.config.SIGNALS['ENTRY_Z'] == 2.5
    assert 'structural' in coordinator._telegram_command(
        '/set HEDGE_RATIO 2.0')
    assert coordinator._telegram_command('/closeall') == \
        'Nothing to close'
