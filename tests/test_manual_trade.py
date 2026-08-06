"""Manual Spread Trade: arm at a target entry spread, exit at a
target spread, overnight handling. Ported from the old app's
dashboard panel, on the current engine."""

import json
from datetime import datetime, timedelta

import pytest

from statarb.exits import ExitLadder, outcome_tag, overnight_exit
from statarb.models import OrderSide, Position, SignalType, Trade


def make_position(signal_type=SignalType.SELL_BASIS):
    spot = Trade('XAUUSD', OrderSide.BUY, 50.0)
    fut = Trade('GC1225', OrderSide.SELL, 50.0)
    return Position('POS_0001', 'GOLD', signal_type, spot, fut)


def plan(**overrides):
    base = {'tp_usd': 15000.0, 'stop_usd': 1500.0, 'gate_floor_usd': 0.0,
            'max_hold_sec': 2400, 'entry_z': 3.0, 'entry_sigma': 2.0,
            'rt_cost_usd': 3000.0, 'capital_at_risk': None,
            'half_life_sec': 600}
    base.update(overrides)
    return base


# --- exit at the operator's target spread ---------------------------------

def test_manual_exit_spread_closes_the_trade(config):
    ladder = ExitLadder(config)
    short = make_position(SignalType.SELL_BASIS)   # profits as spread falls
    p = plan(manual_exit_spread=19.0)
    # Not there yet
    assert ladder.evaluate(short, p, z=2.0, gross_pnl=100,
                           age_sec=60, spread=19.5) is None
    # Target reached -> exit, even though no signal says so
    assert ladder.evaluate(short, p, z=2.0, gross_pnl=100,
                           age_sec=60, spread=18.9) == 'MANUAL_TARGET'


def test_manual_exit_spread_mirrored_for_long_spread(config):
    ladder = ExitLadder(config)
    long_spread = make_position(SignalType.BUY_BASIS)
    p = plan(manual_exit_spread=25.0)
    assert ladder.evaluate(long_spread, p, z=-2.0, gross_pnl=100,
                           age_sec=60, spread=24.5) is None
    assert ladder.evaluate(long_spread, p, z=-2.0, gross_pnl=100,
                           age_sec=60, spread=25.1) == 'MANUAL_TARGET'


def test_stop_still_outranks_the_manual_target(config):
    ladder = ExitLadder(config)
    short = make_position()
    p = plan(manual_exit_spread=19.0)
    assert ladder.evaluate(short, p, z=2.0, gross_pnl=-2000,
                           age_sec=60, spread=18.9) == 'DOLLAR_STOP'


# --- overnight handling ---------------------------------------------------

def test_overnight_modes():
    before = datetime(2026, 8, 6, 16, 30)
    after = datetime(2026, 8, 6, 17, 10)
    assert overnight_exit('ALLOW', 500, after, 16, 55) is None
    assert overnight_exit(None, 500, after, 16, 55) is None
    # Cutoff not reached yet
    assert overnight_exit('EXIT_ALWAYS', -500, before, 16, 55) is None
    # Past the cutoff
    assert overnight_exit('EXIT_ALWAYS', -500, after, 16, 55) == \
        'OVERNIGHT_CLOSE'
    assert overnight_exit('EXIT_IF_PROFIT', 500, after, 16, 55) == \
        'OVERNIGHT_CLOSE'
    assert overnight_exit('EXIT_IF_PROFIT', -500, after, 16, 55) is None


def test_manual_outcome_tags():
    assert outcome_tag('MANUAL_TARGET', False) == 'TARGET_HIT'
    assert outcome_tag('OVERNIGHT_CLOSE', False) == 'TIME_EXIT'
    assert outcome_tag('MANUAL_CLOSE', False) == 'TIME_EXIT'


# --- arming through the control file --------------------------------------

@pytest.fixture
def coordinator(tmp_path, monkeypatch, config):
    monkeypatch.chdir(tmp_path)
    from statarb.coordinator import Coordinator, PaperExecutor
    from tests.test_limit_execution import LimitFakeLeg

    coord = Coordinator(config, trading_mode='PAPER')
    spot_leg = LimitFakeLeg('a', price=3300.0)
    fut_leg = LimitFakeLeg('b', price=3320.0)
    coord.spot_leg, coord.futures_leg = spot_leg, fut_leg
    coord.executor = PaperExecutor(spot_leg, fut_leg)
    coord.active_assets['GOLD'] = {
        'config': config.ASSETS['GOLD'], 'spot_symbol': 'XAUUSD',
        'futures_symbol': 'GC1225', 'last_data': None}
    coord.control_path = str(tmp_path / "control.json")
    return coord


def arm(coordinator, **order):
    payload = {'algo_enabled': True,
               'open': dict({'asset': 'GOLD', 'ts': 1.0}, **order)}
    with open(coordinator.control_path, 'w') as f:
        json.dump(payload, f)
    coordinator._control_mtime = 0
    coordinator._read_control()


def market(spread):
    return {'swap_diff': spread, 'spot_price': 3300.0,
            'futures_price': 3320.0, 'actual_basis': 20.0,
            'swap_premium_pct': 5.0, 'spot_bid': 3299.9, 'spot_ask': 3300.1,
            'futures_bid': 3319.9, 'futures_ask': 3320.1,
            'timestamp': datetime.now()}


def test_armed_trade_waits_for_the_entry_spread(coordinator):
    arm(coordinator, direction='SELL_BASIS', entry_spread=22.0,
        exit_spread=19.0, lots=1.0, overnight='EXIT_ALWAYS')
    assert coordinator.manual_order is not None

    # Spread below the arm level -> nothing happens
    coordinator._check_manual_arm('GOLD', market(21.0))
    assert not coordinator.position_manager.get_active_positions()
    assert coordinator.manual_order is not None

    # Spread reaches it -> the pair goes on
    coordinator._check_manual_arm('GOLD', market(22.3))
    positions = coordinator.position_manager.get_active_positions()
    assert len(positions) == 1
    position = next(iter(positions.values()))
    assert position.signal_type == SignalType.SELL_BASIS
    assert position.exit_plan['manual_exit_spread'] == 19.0
    assert position.exit_plan['overnight_mode'] == 'EXIT_ALWAYS'
    assert position.exit_plan['source'] == 'MANUAL'
    assert coordinator.manual_order is None      # consumed


def test_long_spread_arms_from_below(coordinator):
    arm(coordinator, direction='BUY_BASIS', entry_spread=18.0, lots=1.0)
    coordinator._check_manual_arm('GOLD', market(19.0))
    assert not coordinator.position_manager.get_active_positions()
    coordinator._check_manual_arm('GOLD', market(17.5))
    assert len(coordinator.position_manager.get_active_positions()) == 1


def test_no_entry_spread_fires_immediately(coordinator):
    arm(coordinator, direction='SELL_BASIS', lots=1.0)
    assert coordinator.manual_order is None      # not armed — executed
    assert len(coordinator.position_manager.get_active_positions()) == 1


def test_cancel_disarms_without_touching_positions(coordinator):
    arm(coordinator, direction='SELL_BASIS', entry_spread=22.0, lots=1.0)
    assert coordinator.manual_order is not None
    payload = {'algo_enabled': True, 'open': {'asset': None, 'ts': 2.0}}
    with open(coordinator.control_path, 'w') as f:
        json.dump(payload, f)
    coordinator._control_mtime = 0
    coordinator._read_control()
    assert coordinator.manual_order is None
    coordinator._check_manual_arm('GOLD', market(25.0))
    assert not coordinator.position_manager.get_active_positions()


def test_armed_trade_does_not_double_fire(coordinator):
    arm(coordinator, direction='SELL_BASIS', entry_spread=22.0, lots=1.0)
    coordinator._check_manual_arm('GOLD', market(22.5))
    coordinator._check_manual_arm('GOLD', market(23.0))
    assert len(coordinator.position_manager.get_active_positions()) == 1
