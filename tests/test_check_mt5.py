"""The standalone MT5 connection checker.

It is the tool the operator reaches for when nothing is connecting, so
the error decoding has to stay accurate — these lock the codes that
actually come out of MT5 to the advice we give for them.
"""

import check_mt5


def test_the_ipc_failure_operators_hit_is_explained():
    """-10003 is what MT5 returns when the terminal is not running or
    the path is wrong — the error the launcher log showed."""
    meaning, fixes = check_mt5.explain_init_error(-10003)
    assert 'could not start or attach' in meaning
    assert any('Open the MT5 terminal yourself' in f for f in fixes)
    assert any('64-bit' in f for f in fixes)
    assert any('portable' in f for f in fixes)


def test_a_bad_login_is_not_confused_with_a_missing_terminal():
    meaning, fixes = check_mt5.explain_init_error(-6)
    assert 'Authorization failed' in meaning
    assert any('investor password' in f for f in fixes)


def test_an_unknown_code_still_gets_an_answer():
    meaning, fixes = check_mt5.explain_init_error(-99999)
    assert '-99999' in meaning and fixes


def test_algo_trading_disabled_is_called_out_by_name():
    """10027 is the retcode behind 'orders are not being placed'."""
    assert 'ALGO TRADING IS DISABLED' in check_mt5.ORDER_ERRORS[10027]


def test_the_order_rejections_worth_naming_are_covered():
    for code in (10014, 10015, 10018, 10019, 10027, 10030, 10031):
        assert check_mt5.ORDER_ERRORS[code]


def test_it_refuses_to_pretend_without_the_mt5_package(monkeypatch, capsys):
    monkeypatch.setattr(check_mt5, 'mt5', None)
    check_mt5.ok_count = check_mt5.warn_count = check_mt5.fail_count = 0
    assert check_mt5.check_environment() is False
    out = capsys.readouterr().out
    assert 'MetaTrader5 package is NOT installed' in out
    assert 'pip install MetaTrader5' in out


def test_endpoint_check_reports_a_leg_runner_that_is_not_up(capsys, config):
    """A coordinator with no prices is usually leg runners that died,
    not MT5 itself — the checker has to separate the two."""
    from types import SimpleNamespace
    config.accounts = {
        'account_a': SimpleNamespace(name='account_a',
                                     endpoint='127.0.0.1:9'),
    }
    check_mt5.ok_count = check_mt5.warn_count = check_mt5.fail_count = 0
    check_mt5.check_endpoints(config)
    out = capsys.readouterr().out
    assert 'nothing listening on 127.0.0.1:9' in out
    assert 'leg runner for this account is not running' in out


def test_single_account_topology_is_not_reported_as_broken(capsys, config):
    from types import SimpleNamespace
    config.accounts = {'default': SimpleNamespace(name='default',
                                                  endpoint=None)}
    check_mt5.check_endpoints(config)
    out = capsys.readouterr().out
    assert 'single-account topology' in out
