"""Dollars per 1.00 of spread is LEG B's quantity, everywhere.

From the hedge derivation `L_A x C_A = beta x L_B x C_B`, a spread move
is worth `k = L_B x C_B`. `costs.expected_capture` was corrected to leg
B on 2026-08-11; the exit ladder and the slippage report kept using leg
A's `lots x contract_size`, which is the same number ONLY at beta 1
with equal contract sizes — the one configuration this has ever been
run in. Away from there every dollar figure was out by exactly 1/beta.

The strong form pinned here: move the market, compute the pair's P&L
the way `positions.update_position_pnl` does (each leg on its OWN
units), and it must equal the spread move times the multiplier the plan
was built with. If that ever fails, the target, the stop, the levels
and the entry-cost row are all quoting a different trade from the one
on the books.
"""

import pytest

from statarb import sizing, slippage
from statarb.exits import ExitLadder
from statarb.models import SignalType


# beta != 1 AND unequal contract sizes: the case the old code got wrong
# in both of its factors at once.
BETA, CONTRACT_A, CONTRACT_B, LOTS_A = 2.0, 100.0, 50.0, 1.0


@pytest.fixture
def ladder(config):
    config.TRADING['HEDGE_RATIO'] = BETA
    config.ASSETS.clear()
    config.ASSETS['GOLD'] = {'name': 'GOLD', 'enabled': True,
                             'lot_size': CONTRACT_A,
                             'fut_lot_size': CONTRACT_B,
                             'spot_symbols': ['A'], 'futures_symbols': ['B']}
    config.EXITS.update({'LEVERAGE': 0, 'SPOT_LEVERAGE': 0,
                         'FUT_LEVERAGE': 0, 'USE_SIGMA_TARGET': True,
                         'STOP_USD_PER_LOT': 500.0, 'TP_RR': 0.0,
                         'STOP_CAPITAL_PCT': 0.0, 'COST_FLOOR_MULT': 0.0})
    config.COSTS.update({'TARGET_FRACTION': 0.5, 'SPREAD_COST_FACTOR': 0.0,
                         'COMMISSION_PER_LOT_SPOT': 0.0,
                         'COMMISSION_PER_LOT_FUT': 0.0,
                         'MIN_EDGE_MULTIPLE': 0.0})
    return ExitLadder(config)


# A zero-width book, so the round trip is commission only and the
# level arithmetic is not buried under a spread cost.
MARKET = {'spot_price': 1000.0, 'futures_price': 2050.0,
          'spot_bid': 1000.0, 'spot_ask': 1000.0,
          'futures_bid': 2050.0, 'futures_ask': 2050.0,
          'spot_spread': 0.0, 'futures_spread': 0.0}


def expected_k():
    lots_b = sizing.hedge_lots(LOTS_A, CONTRACT_A, CONTRACT_B, BETA)
    return sizing.spread_units(lots_b, CONTRACT_B)


def test_the_setup_is_actually_the_broken_case():
    """A regression test that accidentally uses beta 1 with equal
    contracts proves nothing — the two multipliers agree there."""
    assert expected_k() != LOTS_A * CONTRACT_A


# --- the plan ------------------------------------------------------------

def test_the_sigma_target_is_priced_on_leg_b(ladder):
    plan = ladder.build_plan(LOTS_A, CONTRACT_A, entry_z=3.0, sigma=0.20,
                             half_life_sec=600, market_data=MARKET,
                             asset_cfg=ladder.config.ASSETS['GOLD'])
    assert plan['spread_units'] == pytest.approx(expected_k())
    # f x |z| x sigma x k, the same form costs.expected_capture uses.
    assert plan['tp_usd'] == pytest.approx(0.5 * 3.0 * 0.20 * expected_k())


def test_the_plan_publishes_the_multiplier_it_used(ladder):
    """Published so the levels, the manual target and the slippage
    report all translate spread<->dollars with the SAME number, rather
    than each deriving one and drifting apart."""
    plan = ladder.build_plan(LOTS_A, CONTRACT_A, 3.0, 0.20, 600, MARKET,
                             asset_cfg=ladder.config.ASSETS['GOLD'])
    lots_b = sizing.hedge_lots(LOTS_A, CONTRACT_A, CONTRACT_B, BETA)
    assert plan['leg_b_lots'] == pytest.approx(lots_b)
    assert plan['contract_b'] == CONTRACT_B
    assert plan['spread_units'] == pytest.approx(lots_b * CONTRACT_B)


# --- the levels, against the P&L that actually fires them ----------------

def pair_pnl(d_spot, d_fut, units_a, units_b):
    """Gross P&L of a SHORT-spread pair (long leg A, short leg B) for a
    move in each leg — exactly how positions.update_position_pnl adds
    the two legs up, each on its own units."""
    return d_spot * units_a - d_fut * units_b


def test_the_stop_level_loses_exactly_the_stop(ladder):
    """Walk the spread to the displayed SL and the pair must be down
    stop_usd. With leg A's multiplier it was down by 1/beta of it."""
    plan = ladder.build_plan(LOTS_A, CONTRACT_A, 3.0, 0.20, 600, MARKET,
                             asset_cfg=ladder.config.ASSETS['GOLD'])
    k = plan['spread_units']
    fill_spread = MARKET['futures_price'] - BETA * MARKET['spot_price']
    levels = ExitLadder.spread_levels(plan, fill_spread, k,
                                      SignalType.SELL_BASIS)

    # Move leg B alone: the spread moves with it, one for one.
    d_fut = levels['sl'] - fill_spread
    lots_b = sizing.hedge_lots(LOTS_A, CONTRACT_A, CONTRACT_B, BETA)
    pnl = pair_pnl(0.0, d_fut, LOTS_A * CONTRACT_A, lots_b * CONTRACT_B)
    assert pnl == pytest.approx(-plan['stop_usd'])


def test_the_same_level_is_reached_by_moving_leg_a(ladder):
    """The spread does not care WHICH leg moved, and neither may the
    money: leg A moving by d shifts the spread by -beta x d."""
    plan = ladder.build_plan(LOTS_A, CONTRACT_A, 3.0, 0.20, 600, MARKET,
                             asset_cfg=ladder.config.ASSETS['GOLD'])
    k = plan['spread_units']
    fill_spread = MARKET['futures_price'] - BETA * MARKET['spot_price']
    levels = ExitLadder.spread_levels(plan, fill_spread, k,
                                      SignalType.SELL_BASIS)

    d_spot = (levels['sl'] - fill_spread) / -BETA
    lots_b = sizing.hedge_lots(LOTS_A, CONTRACT_A, CONTRACT_B, BETA)
    pnl = pair_pnl(d_spot, 0.0, LOTS_A * CONTRACT_A, lots_b * CONTRACT_B)
    assert pnl == pytest.approx(-plan['stop_usd'])


def test_the_target_level_makes_exactly_the_target(ladder):
    plan = ladder.build_plan(LOTS_A, CONTRACT_A, 3.0, 0.20, 600, MARKET,
                             asset_cfg=ladder.config.ASSETS['GOLD'])
    k = plan['spread_units']
    fill_spread = MARKET['futures_price'] - BETA * MARKET['spot_price']
    levels = ExitLadder.spread_levels(plan, fill_spread, k,
                                      SignalType.SELL_BASIS)
    lots_b = sizing.hedge_lots(LOTS_A, CONTRACT_A, CONTRACT_B, BETA)
    pnl = pair_pnl(0.0, levels['tp'] - fill_spread,
                   LOTS_A * CONTRACT_A, lots_b * CONTRACT_B)
    # TAKE_PROFIT fires on NET, so the level carries the round trip.
    assert pnl == pytest.approx(plan['tp_usd'] + plan['rt_cost_usd'])


# --- capital at risk -----------------------------------------------------

def test_each_leg_s_margin_uses_its_own_units(ladder):
    """The legs can be levered differently AND sized differently. Leg
    A's units were being used for both notionals."""
    ladder.config.EXITS.update({'SPOT_LEVERAGE': 100.0,
                                'FUT_LEVERAGE': 500.0,
                                'M2M_BUFFER_PCT': 0.0})
    lots_b = sizing.hedge_lots(LOTS_A, CONTRACT_A, CONTRACT_B, BETA)
    units_a, units_b = LOTS_A * CONTRACT_A, lots_b * CONTRACT_B
    capital = ladder._capital_at_risk(LOTS_A, CONTRACT_A, MARKET,
                                      units_b=units_b)
    assert capital == pytest.approx(
        MARKET['spot_price'] * units_a / 100.0
        + MARKET['futures_price'] * units_b / 500.0)


def test_capital_falls_back_to_leg_a_when_units_b_is_unknown(ladder):
    """The old behaviour, kept for callers that do not know leg B —
    right at beta 1 with equal contracts, which is every existing
    caller outside build_plan."""
    ladder.config.EXITS.update({'SPOT_LEVERAGE': 100.0,
                                'FUT_LEVERAGE': 100.0,
                                'M2M_BUFFER_PCT': 0.0})
    units_a = LOTS_A * CONTRACT_A
    assert ladder._capital_at_risk(LOTS_A, CONTRACT_A, MARKET) \
        == pytest.approx((MARKET['spot_price'] + MARKET['futures_price'])
                         * units_a / 100.0)


# --- the slippage report -------------------------------------------------

class FakeTrade:
    def __init__(self, lots):
        self.lot_size = lots


def test_slippage_prices_the_spread_on_leg_b(config):
    config.ASSETS.clear()
    config.ASSETS['GOLD'] = {'lot_size': CONTRACT_A,
                             'fut_lot_size': CONTRACT_B, 'enabled': True}
    lots_b = sizing.hedge_lots(LOTS_A, CONTRACT_A, CONTRACT_B, BETA)
    units = slippage.spread_units(config, 'GOLD', FakeTrade(LOTS_A),
                                  FakeTrade(lots_b))
    assert units == pytest.approx(lots_b * CONTRACT_B)
    assert units != pytest.approx(LOTS_A * CONTRACT_A)


def test_slippage_uses_the_lots_that_actually_filled(config):
    """A partial hedge is a smaller pair, and its dollar cost is
    smaller with it."""
    config.ASSETS.clear()
    config.ASSETS['GOLD'] = {'lot_size': CONTRACT_A,
                             'fut_lot_size': CONTRACT_B, 'enabled': True}
    full = slippage.spread_units(config, 'GOLD', FakeTrade(LOTS_A),
                                 FakeTrade(1.0))
    half = slippage.spread_units(config, 'GOLD', FakeTrade(LOTS_A),
                                 FakeTrade(0.5))
    assert half == pytest.approx(full / 2)


def test_slippage_falls_back_to_leg_a_without_a_futures_contract(config):
    """No fut_lot_size declared — the common case, and the old
    behaviour exactly."""
    config.ASSETS.clear()
    config.ASSETS['GOLD'] = {'lot_size': CONTRACT_A, 'enabled': True}
    units = slippage.spread_units(config, 'GOLD', FakeTrade(2.0),
                                  FakeTrade(2.0))
    assert units == pytest.approx(2.0 * CONTRACT_A)


def test_slippage_units_survive_an_unfilled_hedge(config):
    """Leg B never filled: report leg A rather than zero, because a
    zero would price the whole entry cost at nothing."""
    config.ASSETS.clear()
    config.ASSETS['GOLD'] = {'lot_size': CONTRACT_A,
                             'fut_lot_size': CONTRACT_B, 'enabled': True}
    units = slippage.spread_units(config, 'GOLD', FakeTrade(LOTS_A),
                                  FakeTrade(0.0))
    assert units == pytest.approx(LOTS_A * CONTRACT_A)


def test_the_dollar_slippage_is_the_spread_slippage_times_leg_b(config):
    """End to end through build(): the pair's dollar cost is
    `fut_slip x units_b + spot_slip x units_a`, and because the hedge
    is sized so `units_a = beta x units_b` that collapses to
    `spread_slip x units_b` exactly."""
    from statarb.models import OrderSide
    lots_b = sizing.hedge_lots(LOTS_A, CONTRACT_A, CONTRACT_B, BETA)
    units_a, units_b = LOTS_A * CONTRACT_A, lots_b * CONTRACT_B
    assert units_a == pytest.approx(BETA * units_b)

    reference = {'spot_bid': 999.0, 'spot_ask': 1001.0,
                 'futures_bid': 2049.0, 'futures_ask': 2051.0}
    spot_fill, fut_fill = 1001.5, 2048.5     # both a little against us
    report = slippage.build(SignalType.SELL_BASIS, False, BETA, units_b,
                            OrderSide.BUY, OrderSide.SELL, reference,
                            spot_fill, fut_fill)
    spot_slip = spot_fill - 1001.0           # paid above the ask
    fut_slip = 2049.0 - fut_fill             # sold below the bid
    assert report['slippage_usd'] == pytest.approx(
        fut_slip * units_b + spot_slip * units_a)
