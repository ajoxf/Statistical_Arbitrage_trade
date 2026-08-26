"""Exit ladder: frozen dollar levels, BE-aware net targets, priority
order, gating rules, spread levels, exit modes, time stops.

Plans here carry a round trip of $4,000: $3,000 of CROSSING (the 0.30
spread on both legs at 50 lots) plus $1,000 of COMMISSION ($10/lot each
side of 50 lots). Only the commission comes off the mark, because the
position is marked at the price it would actually close at and both
crossings are already in those two prices — so NET = GROSS - 1,000 and
a $15,000 net target needs $16,000 gross. `rt_cost_usd` keeps the full
$4,000: the edge filter and the expected value are pricing a trade that
has not happened yet.
"""

import pytest

from statarb.exits import ExitLadder, outcome_tag
from statarb.models import Position, SignalType, Trade, OrderSide

CROSSING = 3000.0    # 0.30 on both legs at 50 lots x 100 units
COMMISSION = 1000.0  # $10/lot per leg x 50 lots
FEES = CROSSING + COMMISSION     # plan['rt_cost_usd']
MARK_FEES = COMMISSION           # what still comes off an exit-side mark


def market_data(spread_dollars=0.30):
    half = spread_dollars / 2
    return {'spot_price': 3300.0, 'futures_price': 3320.0,
            'spot_bid': 3300 - half, 'spot_ask': 3300 + half,
            'futures_bid': 3320 - half, 'futures_ask': 3320 + half}


def make_position(signal_type=SignalType.SELL_BASIS):
    spot = Trade('XAUUSD', OrderSide.BUY, 50.0)
    fut = Trade('GC1225', OrderSide.SELL, 50.0)
    return Position('POS_0001', 'GOLD', signal_type, spot, fut)


@pytest.fixture
def exit_config(config):
    config.SIGNALS.update({'EXIT_Z': 0.5, 'STOP_Z': 4.0,
                           'EXIT_MODE': 'zscore'})
    config.COSTS.update({'TARGET_FRACTION': 0.5, 'SPREAD_COST_FACTOR': 1.0,
                         'COMMISSION_PER_LOT_SPOT': 10.0,
                         'COMMISSION_PER_LOT_FUT': 10.0})
    config.EXITS.update({
        'USE_SIGMA_TARGET': True, 'COST_FLOOR_MULT': 1.2,
        # $80/lot x 50 = $4,000, which clears the $3,000 of bid-ask
        # crossed on the way in. A stop INSIDE that crossing is refused
        # now — the position is marked at the touches it would close
        # at, so it opens exactly one round turn down and a tighter
        # stop is tripped before the spread has moved (2026-08-26).
        'STOP_USD_PER_LOT': 80.0, 'RR': 0.3, 'GATE_FLOOR_USD': 0.0,
        'MAX_HOLD_HALF_LIVES': 4.0, 'MAX_HOLD_FALLBACK_MIN': 240,
        'MAX_HOLD_PROGRESS_SUPPRESS': 0.5,
        'HARD_TIME_STOP_MULT': 3.0,
        'HARD_MAX_HOLD_MIN': 0.0,      # isolated in its own test
        'Z_STOP_EXIT_ENABLED': False,
        'LEVERAGE': 0.0, 'TP_CAPITAL_PCT': 0.0, 'STOP_CAPITAL_PCT': 0.0,
    })
    return config


def build(exit_config, entry_z=3.0, sigma=2.0, half_life=600):
    ladder = ExitLadder(exit_config)
    plan = ladder.build_plan(50.0, 100, entry_z, sigma, half_life,
                             market_data())
    return ladder, plan


def test_plan_sigma_target_and_stop(exit_config):
    _, plan = build(exit_config)
    assert plan['tp_usd'] == pytest.approx(15000)   # 0.5 * 3 * 2 * 5000
    assert plan['stop_usd'] == pytest.approx(4000)  # per-lot 80*50 binds
    assert plan['max_hold_sec'] == pytest.approx(2400)
    assert plan['rt_cost_usd'] == pytest.approx(FEES)


def test_stop_rr_side_can_bind(exit_config):
    exit_config.EXITS['STOP_USD_PER_LOT'] = 1000.0    # far outside; RR binds
    exit_config.EXITS['RR'] = 0.9
    _, plan = build(exit_config)
    assert plan['stop_usd'] == pytest.approx(15000 / 0.9)


def test_cost_floor_blocks_unwinnable_entry(exit_config):
    _, plan = build(exit_config, entry_z=3.0, sigma=0.05)
    assert plan is None


def test_capital_pct_forms_bind_when_leverage_set(exit_config):
    exit_config.EXITS.update({'LEVERAGE': 100.0, 'USE_SIGMA_TARGET': False,
                              'TP_CAPITAL_PCT': 0.5,
                              'STOP_CAPITAL_PCT': 0.3,
                              'COST_FLOOR_MULT': 0.0, 'RR': 0.0,
                              'STOP_USD_PER_LOT': 0.0})
    # The %-of-capital stop is the form under test and it lands at
    # $993, inside this fixture's $3,000 of crossing — which is refused
    # now. Zero the crossing so the test measures the knob rather than
    # the guard; the commission stays.
    exit_config.COSTS['SPREAD_COST_FACTOR'] = 0.0
    ladder, plan = build(exit_config)
    assert plan['capital_at_risk'] == pytest.approx(331000, rel=0.001)
    assert plan['tp_usd'] == pytest.approx(1655, rel=0.001)
    assert plan['stop_usd'] == pytest.approx(993, rel=0.001)


# ---------------------------------------------------------------------------
# Ladder evaluation — gross in, break-even aware
# ---------------------------------------------------------------------------


def test_dollar_stop_fires_first_on_gross_and_ungated(exit_config):
    ladder, plan = build(exit_config)
    position = make_position()
    # z home AND huge gross loss: stop wins, not reversion
    assert ladder.evaluate(position, plan, z=0.1, gross_pnl=-4500,
                           age_sec=10) == 'DOLLAR_STOP'
    # Stop means SPREAD distance: gross -3500 is inside the $4,000 line
    # even though net (-4,500) is past it
    assert ladder.evaluate(position, plan, z=2.0, gross_pnl=-3500,
                           age_sec=10) is None


def test_take_profit_is_profit_on_top_of_breakeven(exit_config):
    ladder, plan = build(exit_config)
    position = make_position()
    # Gross $15.5k = net $14.5k < target $15k -> HOLD
    assert ladder.evaluate(position, plan, z=2.5, gross_pnl=15500,
                           age_sec=10) is None
    # Gross $16.1k = net $15.1k >= target -> TP (z still far from home)
    assert ladder.evaluate(position, plan, z=2.5, gross_pnl=16100,
                           age_sec=10) == 'TAKE_PROFIT'


def test_reversion_exit_gated_at_breakeven(exit_config):
    ladder, plan = build(exit_config)
    position = make_position()
    # z home but gross below break-even (net < 0) -> HOLD
    assert ladder.evaluate(position, plan, z=0.2, gross_pnl=MARK_FEES - 100,
                           age_sec=10) is None
    # At/above break-even -> exit
    assert ladder.evaluate(position, plan, z=0.2, gross_pnl=MARK_FEES + 50,
                           age_sec=10) == 'REVERSION_EXIT'


def test_reversion_fails_open_without_pnl(exit_config):
    ladder, plan = build(exit_config)
    assert ladder.evaluate(make_position(), plan, z=0.2, gross_pnl=None,
                           age_sec=10) == 'REVERSION_EXIT'


def test_gate_floor_decays_with_age_deadlock_fix(exit_config):
    exit_config.EXITS['GATE_FLOOR_USD'] = 50.0
    ladder, plan = build(exit_config)
    position = make_position()
    max_hold = plan['max_hold_sec']

    # Young, z home, net +$20 (< $50 floor) -> gate HOLDS
    assert ladder.evaluate(position, plan, z=0.2, gross_pnl=MARK_FEES + 20,
                           age_sec=60) is None
    # Past 1x max-hold the floor decays to break-even: +$20 net releases
    assert ladder.evaluate(position, plan, z=0.2, gross_pnl=MARK_FEES + 20,
                           age_sec=max_hold + 1) == 'REVERSION_EXIT'
    # A net loser still holds between 1x and 2x
    assert ladder.evaluate(position, plan, z=0.2, gross_pnl=MARK_FEES - 20,
                           age_sec=max_hold + 1) is None
    # Past 2x the gate releases entirely
    assert ladder.evaluate(position, plan, z=0.2, gross_pnl=MARK_FEES - 20,
                           age_sec=2 * max_hold + 1) == 'REVERSION_EXIT'


def test_max_hold_only_walks_away_with_net_profit(exit_config):
    ladder, plan = build(exit_config)
    position = make_position()
    age = plan['max_hold_sec'] + 1
    # Gross +$900 is a NET loser -> hold (z=2.6: no suppression)
    assert ladder.evaluate(position, plan, z=2.6, gross_pnl=MARK_FEES - 100,
                           age_sec=age) is None
    assert ladder.evaluate(position, plan, z=2.6, gross_pnl=MARK_FEES + 100,
                           age_sec=age) == 'MAX_HOLD'


def test_max_hold_suppressed_while_travelling_toward_tp(exit_config):
    ladder, plan = build(exit_config, entry_z=3.0)
    position = make_position()
    age = plan['max_hold_sec'] + 1
    assert ladder.evaluate(position, plan, z=1.2, gross_pnl=MARK_FEES + 100,
                           age_sec=age) is None       # 60% home + TP set
    plan_no_tp = dict(plan, tp_usd=None)
    assert ladder.evaluate(position, plan_no_tp, z=1.2,
                           gross_pnl=MARK_FEES + 100, age_sec=age) == 'MAX_HOLD'


def test_hard_time_stop_multiple_of_max_hold(exit_config):
    ladder, plan = build(exit_config)
    position = make_position()
    max_hold = plan['max_hold_sec']
    assert ladder.evaluate(position, plan, z=2.6, gross_pnl=-100,
                           age_sec=2.5 * max_hold) is None
    assert ladder.evaluate(position, plan, z=2.6, gross_pnl=-100,
                           age_sec=3 * max_hold + 1) == 'TIME_STOP'
    exit_config.EXITS['HARD_TIME_STOP_MULT'] = 0
    assert ladder.evaluate(position, plan, z=2.6, gross_pnl=-100,
                           age_sec=10 * max_hold) is None


def test_hard_max_hold_fixed_minutes(exit_config):
    """The ~90-minute drift window: a FIXED clock independent of the
    measured half-life, P&L-agnostic."""
    exit_config.EXITS.update({'HARD_TIME_STOP_MULT': 0.0,
                              'HARD_MAX_HOLD_MIN': 90.0})
    ladder, plan = build(exit_config, half_life=6000)   # max_hold 400min
    position = make_position()
    assert ladder.evaluate(position, plan, z=2.6, gross_pnl=-100,
                           age_sec=89 * 60) is None
    assert ladder.evaluate(position, plan, z=2.6, gross_pnl=-100,
                           age_sec=91 * 60) == 'TIME_STOP'


def test_z_stop_suppression_matrix(exit_config, caplog):
    import logging as logging_mod
    sell = make_position(SignalType.SELL_BASIS)

    ladder, plan = build(exit_config)
    with caplog.at_level(logging_mod.WARNING):
        assert ladder.evaluate(sell, plan, z=4.5, gross_pnl=-100,
                               age_sec=10) is None
    assert any('WOULD HAVE FIRED' in r.message for r in caplog.records)
    assert ladder.evaluate(sell, plan, z=4.5, gross_pnl=-4500,
                           age_sec=10) == 'DOLLAR_STOP'

    exit_config.EXITS['Z_STOP_EXIT_ENABLED'] = True
    ladder2, plan2 = build(exit_config)
    assert ladder2.evaluate(sell, plan2, z=4.5, gross_pnl=-100,
                            age_sec=10) == 'Z_STOP'
    assert ladder2.evaluate(sell, plan2, z=-4.5, gross_pnl=-100,
                            age_sec=10) is None
    buy = make_position(SignalType.BUY_BASIS)
    assert ladder2.evaluate(buy, plan2, z=-4.5, gross_pnl=-100,
                            age_sec=10) == 'Z_STOP'

    exit_config.EXITS['Z_STOP_EXIT_ENABLED'] = False
    ladder3, plan3 = build(exit_config)
    plan_unarmed = dict(plan3, stop_usd=0.0)
    assert ladder3.evaluate(sell, plan_unarmed, z=4.5, gross_pnl=-100,
                            age_sec=10) == 'Z_STOP'


# ---------------------------------------------------------------------------
# Exit modes (zscore | spread | hybrid)
# ---------------------------------------------------------------------------


def test_spread_mode_exits_on_mean_cross_not_z(exit_config):
    exit_config.SIGNALS['EXIT_MODE'] = 'spread'
    ladder, plan = build(exit_config)
    plan['entry_mu'] = 15.0                      # mean frozen at entry
    sell = make_position(SignalType.SELL_BASIS)  # entered with S above mu
    gross = MARK_FEES + 100                           # net-positive, gate passes

    # z still far, but spread crossed the frozen mean -> exit
    assert ladder.evaluate(sell, plan, z=2.0, gross_pnl=gross,
                           age_sec=10, spread=14.8) == 'REVERSION_EXIT'
    # Spread above the mean -> no exit even though z is home
    assert ladder.evaluate(sell, plan, z=0.1, gross_pnl=gross,
                           age_sec=10, spread=16.0) is None


def test_hybrid_mode_takes_either_signal(exit_config):
    exit_config.SIGNALS['EXIT_MODE'] = 'hybrid'
    ladder, plan = build(exit_config)
    plan['entry_mu'] = 15.0
    sell = make_position(SignalType.SELL_BASIS)
    gross = MARK_FEES + 100
    assert ladder.evaluate(sell, plan, z=0.1, gross_pnl=gross,
                           age_sec=10, spread=16.0) == 'REVERSION_EXIT'
    assert ladder.evaluate(sell, plan, z=2.0, gross_pnl=gross,
                           age_sec=10, spread=14.8) == 'REVERSION_EXIT'


# ---------------------------------------------------------------------------
# Spread levels (the in-position card's BE / EX / TP / SL)
# ---------------------------------------------------------------------------


def test_spread_levels_sell_basis(exit_config):
    _, plan = build(exit_config)
    levels = ExitLadder.spread_levels(plan, entry_spread=20.0, oz=5000,
                                      signal_type=SignalType.SELL_BASIS)
    # Profit needs the spread to FALL: BE below entry by fees/oz
    assert levels['favorable'] == 'down'
    assert levels['be'] == pytest.approx(20.0 - MARK_FEES / 5000)   # 19.80
    assert levels['tp'] == pytest.approx(20.0 - 16000 / 5000)     # 16.80
    assert levels['sl'] == pytest.approx(20.0 + 4000 / 5000)      # 20.80
    assert levels['ex'] == pytest.approx(levels['be'])            # floor 0


def test_spread_levels_buy_basis_mirrored(exit_config):
    _, plan = build(exit_config)
    levels = ExitLadder.spread_levels(plan, entry_spread=-10.0, oz=5000,
                                      signal_type=SignalType.BUY_BASIS)
    assert levels['favorable'] == 'up'
    assert levels['be'] == pytest.approx(-10.0 + MARK_FEES / 5000)
    assert levels['tp'] == pytest.approx(-10.0 + 16000 / 5000)
    assert levels['sl'] == pytest.approx(-10.0 - 4000 / 5000)


def test_outcome_tags_are_deterministic():
    assert outcome_tag('TAKE_PROFIT', False) == 'TARGET_HIT'
    assert outcome_tag('REVERSION_EXIT', True) == 'REVERSION_BANKED'
    assert outcome_tag('MAX_HOLD', True) == 'TIME_EXIT'
    assert outcome_tag('TIME_STOP', False) == 'TIME_EXIT'
    assert outcome_tag('DOLLAR_STOP', False) == 'STOPPED_IN_TREND'
    assert outcome_tag('DOLLAR_STOP', True) == 'STOPPED_AFTER_FULL_REVERSION'
    assert outcome_tag('Z_STOP', True) == 'STOPPED_AFTER_FULL_REVERSION'


# --- the max-hold horizon has a floor (live 2026-08-07) -------------------

def test_a_seconds_long_half_life_does_not_produce_a_seconds_long_hold(
        config):
    """The AR(1) fit runs on consecutive QUOTES, ~0.6s apart on a live
    gold feed, so a spread that is mostly tick noise fits a half-life
    of a few seconds. Live that gave max_hold 12s and a hard time stop
    at 36s: a manual trade with a $215 target was force-closed 37
    seconds after entry, paying the full round trip with no chance of
    reaching it."""
    ladder = ExitLadder(config)
    # The real CFI gold book: 0.13 + 0.34 = $47 of crossing per lot.
    # The stop has to clear that or the plan is refused (2026-08-26) —
    # incidental here, these three are about the max-hold floor.
    config.EXITS['STOP_USD_PER_LOT'] = 100.0
    md = {'spot_price': 4335.11, 'futures_price': 4394.03,
          'spot_bid': 4335.05, 'spot_ask': 4335.18,
          'futures_bid': 4394.03, 'futures_ask': 4394.37}
    plan = ladder.build_plan(1.0, 100, 3.0, 0.5, 3.0, md)
    assert plan is not None
    floor = config.EXITS['MIN_MAX_HOLD_SEC']
    assert plan['max_hold_sec'] == pytest.approx(floor)
    # 4 x 3s = 12s under the old rule.
    assert plan['max_hold_sec'] > 12


def test_a_real_half_life_is_left_alone(config):
    """The floor must not override a genuine reversion horizon."""
    ladder = ExitLadder(config)
    # The real CFI gold book: 0.13 + 0.34 = $47 of crossing per lot.
    # The stop has to clear that or the plan is refused (2026-08-26) —
    # incidental here, these three are about the max-hold floor.
    config.EXITS['STOP_USD_PER_LOT'] = 100.0
    md = {'spot_price': 4335.11, 'futures_price': 4394.03,
          'spot_bid': 4335.05, 'spot_ask': 4335.18,
          'futures_bid': 4394.03, 'futures_ask': 4394.37}
    plan = ladder.build_plan(1.0, 100, 3.0, 0.5, 1800.0, md)
    assert plan['max_hold_sec'] == pytest.approx(4 * 1800.0)


def test_the_floor_can_be_switched_off(config):
    config.EXITS['MIN_MAX_HOLD_SEC'] = 0
    ladder = ExitLadder(config)
    # The real CFI gold book: 0.13 + 0.34 = $47 of crossing per lot.
    # The stop has to clear that or the plan is refused (2026-08-26) —
    # incidental here, these three are about the max-hold floor.
    config.EXITS['STOP_USD_PER_LOT'] = 100.0
    md = {'spot_price': 4335.11, 'futures_price': 4394.03,
          'spot_bid': 4335.05, 'spot_ask': 4335.18,
          'futures_bid': 4394.03, 'futures_ask': 4394.37}
    plan = ladder.build_plan(1.0, 100, 3.0, 0.5, 3.0, md)
    assert plan['max_hold_sec'] == pytest.approx(12.0)


# --- why is the stop that number? ---------------------------------------
# Operator, 2026-08-24: "Why is the Stop Loss at -$4.77?" Three knobs in
# three different units resolve to one dollar figure, and the figure
# does not say which one bound.

def _plan(config, **over):
    md = {'spot_price': 4658.10, 'futures_price': 4714.22,
          'spot_bid': 4658.05, 'spot_ask': 4658.15,
          'futures_bid': 4714.17, 'futures_ask': 4714.27}
    config.EXITS.update(over)
    # 1 lot, not 0.02: opening this pair crosses $20 of bid-ask, and a
    # stop inside that is refused outright now (it would fire on the
    # tick the position opens). These tests are about WHICH KNOB set
    # the stop, so they need stops that can actually exist.
    return ExitLadder(config).build_plan(
        lots=1.0, contract_size=100.0, entry_z=3.0, sigma=0.5,
        half_life_sec=600.0, market_data=md)


def test_the_plan_names_the_knob_that_set_the_stop(config):
    """RR turns the TARGET into the stop, so a hand-set target of $1.43
    at RR 0.3 produces a $4.77 stop nobody typed."""
    plan = _plan(config, STOP_USD_PER_LOT=0.0, STOP_CAPITAL_PCT=0.0, RR=0.3)
    assert plan['stop_usd'] == pytest.approx(plan['tp_usd'] / 0.3)
    assert 'RR' in plan['stop_source']
    assert '0.3' in plan['stop_source']


def test_a_per_lot_stop_that_binds_says_so(config):
    plan = _plan(config, STOP_USD_PER_LOT=30.0, STOP_CAPITAL_PCT=0.0, RR=0.3)
    assert plan['stop_usd'] == pytest.approx(30.0)
    assert 'STOP_USD_PER_LOT' in plan['stop_source']


def test_the_plan_states_the_win_rate_the_geometry_needs(config):
    """CLAUDE.md has carried the rule 'verify measured win rate clears
    stop/(target+stop)' since the cost measurements. Nothing computed
    it. Unlike EV it needs no sigma, so it is there on a cold start."""
    plan = _plan(config, STOP_USD_PER_LOT=0.0, STOP_CAPITAL_PCT=0.0, RR=0.3)
    assert plan['breakeven_win_rate'] == pytest.approx(
        plan['stop_usd'] / (plan['tp_usd'] + plan['stop_usd']))
    # RR below 1 means the stop is wider than the target, so more than
    # half the trades have to win.
    assert plan['breakeven_win_rate'] > 0.5


def test_rr_above_one_needs_fewer_than_half_the_trades(config):
    plan = _plan(config, STOP_USD_PER_LOT=0.0, STOP_CAPITAL_PCT=0.0, RR=2.0)
    assert plan['breakeven_win_rate'] < 0.5


# --- a stop inside the entry crossing ------------------------------------
# Found in the pre-live review of 2026-08-26, asked for by the operator
# ("if I turned the Algo on, would it all work as expected?").
#
# Since a position is marked at the touches it would CLOSE at, its gross
# P&L at t=0 is exactly minus one round turn of both legs' bid-ask.
# DOLLAR_STOP compares gross against `stop_usd`, so a stop at or inside
# that crossing is tripped before the spread has moved at all.

def _gold_book(lots):
    """The real CFI gold book — Leg A 0.13, Leg B 0.34 wide."""
    return {'spot_price': 4335.11, 'futures_price': 4394.03,
            'spot_bid': 4335.045, 'spot_ask': 4335.175,
            'futures_bid': 4393.86, 'futures_ask': 4394.20}


def test_a_stop_inside_the_entry_crossing_is_refused(config):
    """The shipped default: $30/lot against gold's $47/lot round turn."""
    config.EXITS.update({'STOP_USD_PER_LOT': 30.0, 'RR': 0.0,
                         'STOP_CAPITAL_PCT': 0.0})
    ladder = ExitLadder(config)
    plan = ladder.build_plan(1.0, 100, 3.0, 0.5, 600.0, _gold_book(1.0))
    assert plan is None
    assert 'stopped out on the tick it opens' in ladder.last_refusal
    assert '$30.00' in ladder.last_refusal      # names the stop...
    assert '$47.00' in ladder.last_refusal      # ...and what it is under


@pytest.mark.parametrize('lots', [0.1, 1.0, 10.0])
def test_it_does_not_wash_out_with_size(config, lots):
    """Both sides scale with lots, so a stop inside the crossing is
    inside it at every clip. Trading smaller is not the fix."""
    config.EXITS.update({'STOP_USD_PER_LOT': 30.0, 'RR': 0.0,
                         'STOP_CAPITAL_PCT': 0.0})
    ladder = ExitLadder(config)
    assert ladder.build_plan(lots, 100, 3.0, 0.5, 600.0,
                             _gold_book(lots)) is None


def test_a_stop_outside_the_crossing_is_fine(config):
    """The guard must not block a sanely configured trade — otherwise
    the algo silently never trades, which is its own kind of failure."""
    config.EXITS.update({'STOP_USD_PER_LOT': 100.0, 'RR': 0.0,
                         'STOP_CAPITAL_PCT': 0.0})
    ladder = ExitLadder(config)
    plan = ladder.build_plan(1.0, 100, 3.0, 0.5, 600.0, _gold_book(1.0))
    assert plan is not None
    assert plan['stop_usd'] == pytest.approx(100.0)


def test_the_refused_plan_really_would_have_stopped_at_once(config):
    """The strong form: with the guard off, the plan it would have
    built stops on the position's opening mark. Otherwise this whole
    check could be pinned by arithmetic that means nothing."""
    from statarb import marketdata
    config.EXITS.update({'STOP_USD_PER_LOT': 100.0, 'RR': 0.0,
                         'STOP_CAPITAL_PCT': 0.0})
    md = _gold_book(1.0)
    plan = ExitLadder(config).build_plan(1.0, 100, 3.0, 0.5, 600.0, md)
    # ...then put back the stop the OLD code would have chosen from the
    # shipped $30/lot default. This is the plan being refused.
    plan['stop_usd'] = 30.0

    # Mark the pair where it would actually close, the way the
    # coordinator does, and read the gross P&L that lands on the ladder.
    from statarb.positions import PositionManager

    class NullLog:
        def __getattr__(self, _name):
            return lambda *a, **k: None

    manager = PositionManager(NullLog())
    position = manager.create_position(
        'GOLD', SignalType.SELL_BASIS,
        Trade('XAUUSD', OrderSide.BUY, 1.0), Trade('GC1225', OrderSide.SELL, 1.0),
        md['futures_price'] - md['spot_price'])
    position.spot_trade.executed_price = md['spot_ask']     # bought at the ask
    position.futures_trade.executed_price = md['futures_bid']  # sold at the bid
    close_spot, close_fut = marketdata.closing_prices(md, SignalType.SELL_BASIS)
    manager.update_position_pnl(position.position_id, close_spot, close_fut,
                                0.0, contract_size=100.0, contract_b=100.0)

    assert position.unrealized_pnl == pytest.approx(-47.0)   # one round turn
    assert ExitLadder(config).evaluate(
        position, plan, z=3.0, gross_pnl=position.unrealized_pnl,
        age_sec=0.5) == 'DOLLAR_STOP'
