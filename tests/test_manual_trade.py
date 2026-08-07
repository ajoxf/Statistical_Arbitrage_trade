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


# --- the operator's own STOP spread ---------------------------------------

def test_manual_stop_spread_closes_a_short_spread(config):
    """A short-spread trade loses as the spread RISES, so its manual
    stop sits above entry."""
    ladder = ExitLadder(config)
    short = make_position(SignalType.SELL_BASIS)
    p = plan(manual_stop_spread=23.0)
    assert ladder.evaluate(short, p, z=3.0, gross_pnl=-100,
                           age_sec=60, spread=22.9) is None
    assert ladder.evaluate(short, p, z=3.0, gross_pnl=-100,
                           age_sec=60, spread=23.0) == 'MANUAL_STOP'


def test_manual_stop_spread_mirrored_for_long_spread(config):
    ladder = ExitLadder(config)
    long_spread = make_position(SignalType.BUY_BASIS)
    p = plan(manual_stop_spread=17.0)
    assert ladder.evaluate(long_spread, p, z=-3.0, gross_pnl=-100,
                           age_sec=60, spread=17.1) is None
    assert ladder.evaluate(long_spread, p, z=-3.0, gross_pnl=-100,
                           age_sec=60, spread=16.9) == 'MANUAL_STOP'


def test_manual_stop_outranks_the_manual_target(config):
    """Both levels reachable in one tick: the stop wins. Nothing on
    this panel may outrank the operator's own stop."""
    ladder = ExitLadder(config)
    short = make_position(SignalType.SELL_BASIS)
    p = plan(manual_stop_spread=23.0, manual_exit_spread=23.0)
    assert ladder.evaluate(short, p, z=3.0, gross_pnl=0,
                           age_sec=60, spread=23.5) == 'MANUAL_STOP'


def test_engine_dollar_stop_still_armed_beside_a_manual_stop(config):
    """A manual stop does not disarm the engine's own — whichever is
    reached first closes the trade."""
    ladder = ExitLadder(config)
    short = make_position(SignalType.SELL_BASIS)
    p = plan(manual_stop_spread=99.0)          # far away, never hit
    assert ladder.evaluate(short, p, z=3.0, gross_pnl=-2000,
                           age_sec=60, spread=20.0) == 'DOLLAR_STOP'


def test_manual_stop_is_tagged_as_a_stop():
    assert outcome_tag('MANUAL_STOP', False) == 'STOPPED_IN_TREND'
    assert outcome_tag('MANUAL_STOP', True) == 'STOPPED_AFTER_FULL_REVERSION'


# --- level geometry -------------------------------------------------------

def test_manual_level_geometry_rules():
    """The one mistake on this panel that costs money immediately: a
    stop on the winning side fires the moment the trade goes right."""
    from statarb.webapi import manual_level_error as err
    # Short spread: TP below entry, SL above.
    assert err('SELL_BASIS', 20.0, 19.0, 21.0) is None
    assert 'Take profit' in err('SELL_BASIS', 20.0, 21.0, None)
    assert 'Stop loss' in err('SELL_BASIS', 20.0, None, 19.0)
    # Long spread: mirrored.
    assert err('BUY_BASIS', 20.0, 21.0, 19.0) is None
    assert 'Take profit' in err('BUY_BASIS', 20.0, 19.0, None)
    assert 'Stop loss' in err('BUY_BASIS', 20.0, None, 21.0)
    # Nothing to measure against, and omitted levels, are both fine.
    assert err('SELL_BASIS', None, 21.0, 19.0) is None
    assert err('SELL_BASIS', 20.0, None, None) is None


def test_spread_levels_show_whichever_stop_comes_first(config):
    """The in-position card must show the level the spread reaches
    FIRST, not simply the engine's."""
    ladder = ExitLadder(config)
    # oz such that the engine's $1500 stop is 1.5 spread units away
    oz = 1000.0
    p = plan(rt_cost_usd=0.0)
    near = ladder.spread_levels(dict(p, manual_stop_spread=20.5),
                                20.0, oz, SignalType.SELL_BASIS)
    assert near['sl'] == pytest.approx(20.5)     # operator's is nearer
    assert near['manual_sl'] == 20.5
    far = ladder.spread_levels(dict(p, manual_stop_spread=25.0),
                               20.0, oz, SignalType.SELL_BASIS)
    assert far['sl'] == pytest.approx(21.5)      # engine's is nearer


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
    return {'spread': spread, 'spot_price': 3300.0,
            'futures_price': 3320.0, 'actual_basis': 20.0,
            'basis_pct': 5.0, 'spot_bid': 3299.9, 'spot_ask': 3300.1,
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


def test_stop_spread_travels_with_the_armed_trade(coordinator):
    arm(coordinator, direction='SELL_BASIS', entry_spread=22.0,
        exit_spread=19.0, stop_spread=24.0, lots=1.0)
    coordinator._check_manual_arm('GOLD', market(22.3))
    position = next(iter(
        coordinator.position_manager.get_active_positions().values()))
    assert position.exit_plan['manual_stop_spread'] == 24.0
    assert position.exit_plan['manual_exit_spread'] == 19.0


def test_upside_down_stop_is_refused_at_arm_time(coordinator):
    """A stop below entry on a SHORT spread would fire the instant the
    trade went right. Caught when it is armed, not hours later when it
    executes — and the refusal is published for the panel to show."""
    arm(coordinator, direction='SELL_BASIS', entry_spread=22.0,
        stop_spread=20.0, lots=1.0)
    assert coordinator.manual_order is None
    assert coordinator.manual_note['ok'] is False
    assert 'Stop loss' in coordinator.manual_note['text']


def test_upside_down_target_is_refused_at_arm_time(coordinator):
    arm(coordinator, direction='BUY_BASIS', entry_spread=18.0,
        exit_spread=17.0, lots=1.0)
    assert coordinator.manual_order is None
    assert 'Take profit' in coordinator.manual_note['text']


def test_fire_now_checks_levels_against_the_live_spread(coordinator):
    """No entry level to measure against, so the geometry is judged at
    the price the pair actually opens at."""
    coordinator.active_assets['GOLD']['last_data'] = market(20.0)
    arm(coordinator, direction='SELL_BASIS', stop_spread=18.0, lots=1.0)
    assert not coordinator.position_manager.get_active_positions()
    assert coordinator.manual_note['ok'] is False


def test_a_refusal_reaches_the_ui_not_just_the_log(coordinator):
    """Every rejection path used to end in logging.warning and nothing
    else — the operator pressed Activate and saw nothing happen."""
    coordinator.config.RISK_LIMITS['MAX_LOT_SIZE'] = 0.5
    arm(coordinator, direction='SELL_BASIS', lots=5.0)
    assert coordinator.manual_note['ok'] is False
    assert 'MAX_LOT_SIZE' in coordinator.manual_note['text']


def test_a_successful_manual_trade_reports_itself(coordinator):
    coordinator.active_assets['GOLD']['last_data'] = market(20.0)
    arm(coordinator, direction='SELL_BASIS', lots=1.0)
    assert coordinator.manual_note['ok'] is True
    assert 'POS_' in coordinator.manual_note['text']


# --- the engine must not veto the operator's own target -------------------
#
# Live 2026-08-07: armed SHORT at 59.00 with TP 57 and SL 69, triggered
# at 59.12, then "Exit plan not viable: cost floor $59 exceeds plausible
# full reversion $15 — blocking entry". The badge went back to IDLE and
# the panel said "order was not filled". Both were wrong: the operator's
# own target was 2.12 spread units away, worth $212 against $59 of cost.
# The engine measured a target THEY had not asked for.

def test_the_operators_target_is_what_gets_measured(config):
    ladder = ExitLadder(config)
    md = {'spot_price': 4292.61, 'futures_price': 4351.55,
          'spot_bid': 4292.55, 'spot_ask': 4292.68,
          'futures_bid': 4351.38, 'futures_ask': 4351.72}
    # A full reversion of this z is worth very little...
    assert ladder.build_plan(1.0, 100, 2.4, 0.063, 600, md) is None
    assert 'cannot pay for itself' in ladder.last_refusal
    # ...but the operator's 2.12-wide target is worth $212.
    plan = ladder.build_plan(1.0, 100, 2.4, 0.063, 600, md,
                             manual_target_usd=212.0)
    assert plan is not None
    assert plan['tp_usd'] == pytest.approx(212.0)


def test_a_manual_target_below_cost_is_allowed_but_warned(config, caplog):
    """Manual means manual. It is stated loudly and placed anyway —
    the operator may be hedging something the engine cannot see."""
    import logging
    ladder = ExitLadder(config)
    md = {'spot_price': 4292.61, 'futures_price': 4351.55,
          'spot_bid': 4292.55, 'spot_ask': 4292.68,
          'futures_bid': 4351.38, 'futures_ask': 4351.72}
    with caplog.at_level(logging.WARNING):
        plan = ladder.build_plan(1.0, 100, 2.4, 0.063, 600, md,
                                 manual_target_usd=5.0)
    assert plan is not None
    assert 'cannot make money at that level' in caplog.text


def test_a_signal_entry_is_still_vetoed(config):
    """The viability check is the edge filter's last line for AUTOMATIC
    entries and must not be weakened by any of this."""
    ladder = ExitLadder(config)
    md = {'spot_price': 4292.61, 'futures_price': 4351.55,
          'spot_bid': 4292.55, 'spot_ask': 4292.68,
          'futures_bid': 4351.38, 'futures_ask': 4351.72}
    assert ladder.build_plan(1.0, 100, 2.4, 0.063, 600, md) is None


def test_the_refusal_reaches_the_panel_not_just_the_log(coordinator):
    """"order was not filled — see the log" sent the operator to a file
    to find a decision the engine had already made and could simply
    have reported."""
    coordinator.exit_ladder.last_refusal = 'the trade cannot pay for itself'
    coordinator.exit_ladder.build_plan = lambda *a, **k: None
    coordinator.active_assets['GOLD']['last_data'] = market(20.0)
    arm(coordinator, direction='SELL_BASIS', lots=1.0)

    assert not coordinator.position_manager.get_active_positions()
    note = coordinator.manual_note
    assert note['ok'] is False
    assert note['text'] == 'the trade cannot pay for itself'
    assert 'see the log' not in note['text']


def test_the_operators_target_is_handed_to_the_exit_ladder(coordinator):
    """The wiring behind the fix: what the operator's own take-profit
    is worth, so the viability test measures THEIR distance."""
    seen = {}
    real = coordinator.exit_ladder.build_plan

    def spy(*args, **kwargs):
        seen['manual_target_usd'] = kwargs.get('manual_target_usd')
        return real(*args, **kwargs)

    coordinator.exit_ladder.build_plan = spy
    coordinator.active_assets['GOLD']['last_data'] = market(20.0)
    arm(coordinator, direction='SELL_BASIS', lots=1.0, exit_spread=15.0)

    # 5.0 spread units x 1 lot x 100 contract = $500
    assert seen['manual_target_usd'] == pytest.approx(500.0)
    position = next(iter(
        coordinator.position_manager.get_active_positions().values()))
    assert position.exit_plan['manual_exit_spread'] == 15.0
    assert position.exit_plan['tp_usd'] == pytest.approx(500.0)
    assert coordinator.manual_note['ok'] is True


def test_a_signal_entry_hands_over_no_manual_target(coordinator):
    """Only a hand-placed trade gets to name its own target."""
    seen = {}
    real = coordinator.exit_ladder.build_plan
    coordinator.exit_ladder.build_plan = lambda *a, **k: (
        seen.update(target=k.get('manual_target_usd')) or real(*a, **k))
    # The fixture builds active_assets by hand, so give GOLD the
    # SpreadStats a real run would have.
    from statarb.spread import SpreadStats
    coordinator.stats['GOLD'] = SpreadStats(coordinator.config.SIGNALS)
    coordinator._open_position('GOLD', SignalType.SELL_BASIS, 1.0,
                               market(20.0), coordinator.stats['GOLD'], 100)
    assert seen['target'] is None
