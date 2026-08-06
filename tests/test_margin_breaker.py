"""Margin breaker: halts NEW ENTRIES when the weakest MT5 account
tightens, optionally tapering clip size first. Exits, stops and
reconciliation are never blocked by it."""

import pytest

from statarb.models import SignalType
from statarb.positions import PositionManager
from statarb.risk import RiskManager


def accounts(level_a=None, level_b=None, free_a=None, free_b=None):
    return {
        'account_a': {'account': 'account_a', 'equity': 98000.0,
                      'margin': 20000.0, 'margin_free': free_a,
                      'margin_level': level_a},
        'account_b': {'account': 'account_b', 'equity': 51500.0,
                      'margin': 40000.0, 'margin_free': free_b,
                      'margin_level': level_b},
    }


@pytest.fixture
def rm(config):
    config.RISK_LIMITS.update({
        'MARGIN_BREAKER_ENABLED': True, 'MARGIN_HALT_LEVEL': 200.0,
        'MARGIN_MIN_FREE_USD': 0.0, 'MARGIN_REDUCE_ENABLED': False,
        'MARGIN_REDUCE_LEVEL': 400.0, 'MARGIN_MIN_SIZE_FRACTION': 0.25,
        'DAILY_MAX_LOSS_USD': 0.0, 'LOSS_STREAK_PAUSE': 0,
        'LOSS_STREAK_REDUCE': 0,
    })
    return RiskManager(config)


def test_off_by_default(config):
    manager = RiskManager(config)
    manager.update_accounts(accounts(level_a=50.0, level_b=40.0))
    assert config.RISK_LIMITS['MARGIN_BREAKER_ENABLED'] is False
    assert manager.margin_halt() == (False, None)
    assert manager.halted() == (False, None)


def test_weakest_account_governs_not_the_average(rm):
    # Combined would look healthy (249%) but account_b is at 129%
    rm.update_accounts(accounts(level_a=490.0, level_b=128.75))
    weakest = rm.weakest_margin()
    assert weakest[0] == 'account_b'
    halted, why = rm.halted()
    assert halted
    assert 'account_b' in why and '129%' in why


def test_healthy_accounts_do_not_halt(rm):
    rm.update_accounts(accounts(level_a=490.0, level_b=520.0))
    assert rm.halted() == (False, None)


def test_flat_accounts_post_no_margin_and_never_trigger(rm):
    """MT5 reports no margin level when nothing is open — that is not
    a constraint, and must not block the first trade of the day."""
    rm.update_accounts(accounts(level_a=None, level_b=0))
    assert rm.weakest_margin() is None
    assert rm.margin_halt() == (False, None)
    assert rm.margin_size_multiplier() == 1.0


def test_free_margin_floor_is_a_second_trigger(rm):
    rm.config.RISK_LIMITS['MARGIN_MIN_FREE_USD'] = 15000.0
    rm.update_accounts(accounts(level_a=490.0, level_b=520.0,
                                free_a=78000.0, free_b=11500.0))
    halted, why = rm.halted()
    assert halted and 'free margin' in why and 'account_b' in why


def test_size_taper_between_reduce_and_halt(rm):
    rm.config.RISK_LIMITS['MARGIN_REDUCE_ENABLED'] = True
    # At/above the reduce level -> full size
    rm.update_accounts(accounts(level_a=500.0, level_b=400.0))
    assert rm.margin_size_multiplier() == 1.0
    # Halfway between halt (200) and reduce (400) -> half size
    rm.update_accounts(accounts(level_a=500.0, level_b=300.0))
    assert rm.margin_size_multiplier() == pytest.approx(0.5)
    # Just above the halt level -> floored, not zero
    rm.update_accounts(accounts(level_a=500.0, level_b=205.0))
    assert rm.margin_size_multiplier() == pytest.approx(0.25)


def test_taper_requires_both_switches(rm):
    rm.update_accounts(accounts(level_a=500.0, level_b=300.0))
    assert rm.margin_size_multiplier() == 1.0     # reduce switch off
    rm.config.RISK_LIMITS['MARGIN_REDUCE_ENABLED'] = True
    assert rm.margin_size_multiplier() == pytest.approx(0.5)
    rm.config.RISK_LIMITS['MARGIN_BREAKER_ENABLED'] = False
    assert rm.margin_size_multiplier() == 1.0     # master switch off


def test_taper_compounds_with_loss_streak_reducer(rm):
    rm.config.RISK_LIMITS.update({'MARGIN_REDUCE_ENABLED': True,
                                  'LOSS_STREAK_REDUCE': 3,
                                  'STREAK_SIZE_CUT': 0.2})
    for _ in range(3):
        rm.on_position_closed(-100)
    rm.update_accounts(accounts(level_a=500.0, level_b=300.0))
    assert rm.size_multiplier() == pytest.approx(0.8 * 0.5)


def test_breaker_blocks_entries_through_validate(rm, data_logger):
    pm = PositionManager(data_logger)
    rm.update_accounts(accounts(level_a=490.0, level_b=150.0))
    ok, reason = rm.validate_new_position('GOLD', SignalType.SELL_BASIS,
                                          1.0, pm)
    assert not ok
    assert 'margin breaker' in reason


def test_missing_account_data_fails_open(rm):
    """No margin snapshot yet (legs not connected) must not wedge the
    engine into a permanent halt."""
    rm.update_accounts({})
    assert rm.margin_halt() == (False, None)
    assert rm.margin_size_multiplier() == 1.0


def test_coordinator_publishes_breaker_state(tmp_path, monkeypatch, config):
    monkeypatch.chdir(tmp_path)
    from statarb.coordinator import Coordinator
    config.RISK_LIMITS.update({'MARGIN_BREAKER_ENABLED': True,
                               'MARGIN_HALT_LEVEL': 200.0})
    coordinator = Coordinator(config, trading_mode='PAPER')
    coordinator.risk_manager.update_accounts(
        accounts(level_a=490.0, level_b=128.75))

    state = coordinator._margin_breaker_state()
    assert state['enabled'] is True
    assert state['weakest_account'] == 'account_b'
    assert state['halted'] is True
    assert 'account_b' in state['reason']
