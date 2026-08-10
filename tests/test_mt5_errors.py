"""Decoding MT5's error codes into advice an operator can act on.

Live 2026-08-10: a Mento Markets terminal was open and logged in, the
web checklist reported "(-10001, 'IPC send failed')", and its advice
was "Open the MT5 terminal for this account" and "Log in to the trading
account" — both already done. Advice that describes completed work
reads as the tool being broken.
"""

import pytest

from statarb import mt5_errors


def test_the_code_is_found_in_whatever_shape_it_arrives():
    """last_error() gives a tuple; broker.py str()s it; some paths pass
    a bare int. All three reach the same advice."""
    for value in ((-10001, 'IPC send failed'),
                  "(-10001, 'IPC send failed')",
                  -10001):
        assert mt5_errors.parse_code(value) == -10001


def test_an_unknown_error_gets_no_confident_advice():
    """Guessing a code would attach a specific fix to an unknown fault."""
    assert mt5_errors.parse_code('no digits here') is None
    assert mt5_errors.explain('something unrecognised') == (None, [])
    assert mt5_errors.explain(-99999) == (None, [])


def test_ipc_send_failure_leads_with_the_elevation_mismatch():
    """The terminal is visibly running, so 'open the terminal' is not
    the answer. Elevation mismatch is what it usually is."""
    summary, fixes = mt5_errors.explain((-10001, 'IPC send failed'))
    assert 'IPC send failed' in summary
    assert fixes and 'Administrator' in fixes[0]
    assert any('terminal_path' in f for f in fixes)
    # and specifically NOT the advice that sent the operator in circles
    joined = ' '.join(fixes).lower()
    assert 'open the mt5 terminal' not in joined
    assert 'log in to the trading account' not in joined


def test_every_ipc_code_is_covered():
    """The whole -1000x family means the same class of thing; missing
    one is how -10001 fell through to the generic list."""
    for code in range(-10005, -9999):
        summary, fixes = mt5_errors.explain(code)
        assert summary and fixes, code


def test_auth_and_algo_trading_keep_their_own_advice():
    _, fixes = mt5_errors.explain(-6)
    assert any('investor password' in f for f in fixes)
    _, fixes = mt5_errors.explain(-8)
    assert any('Allow algorithmic trading' in f for f in fixes)


def test_the_checklist_uses_the_decoded_advice(monkeypatch):
    """The web checklist and check_mt5.py must not drift apart."""
    import check_mt5
    assert check_mt5.INIT_ERRORS is mt5_errors.INIT_ERRORS

    from statarb import diagnostics
    checklist = diagnostics.Checklist()
    diagnostics.check_leg(
        checklist, 'spot', 'Mento Markets',
        {'library': True, 'terminal': False,
         'error': "(-10001, 'IPC send failed')"},
        None, None, None)
    row = [c for c in checklist.checks if c['name'] == 'MT5 terminal'][0]
    assert row['status'] == diagnostics.FAIL
    assert 'IPC send failed' in row['message']
    assert any('Administrator' in f for f in row['fix'])
    # the circular advice is gone
    joined = ' '.join(row['fix']).lower()
    assert 'open the mt5 terminal' not in joined
