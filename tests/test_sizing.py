"""How many lots each leg trades.

Owner (2026-08-07): "the way we had it before in the W3 project is —
User fixes the notional value of the leg and the lots are calculated by
itself and after considering the leverage", and "if I am doing WTI and
BRENT the quantities after Hedge Ratio should be balanced".

Two things are under test: the notional -> lots conversion, and the
hedge relationship, which the engine had wrong in a way that only
showed up away from gold.
"""

import pytest

from statarb import sizing
from statarb.models import SignalType


# --- notional -> lots ------------------------------------------------------

def test_lots_come_from_the_money_and_the_price():
    # $1m of gold at 100 oz/lot and $4,265/oz
    lots = sizing.lots_for_notional(1_000_000, 100, 4265.0, step=0.01)
    assert lots == pytest.approx(2.34, abs=0.005)
    assert sizing.notional(lots, 100, 4265.0) <= 1_000_000


def test_lots_round_DOWN_to_a_tradable_step():
    """Rounding up would breach the notional the operator set, and any
    max or margin already checked against it."""
    lots = sizing.lots_for_notional(1_000_000, 100, 4265.0, step=0.1)
    assert lots == pytest.approx(2.3)


def test_a_notional_below_one_lot_is_zero_not_a_fraction():
    """Below the minimum tradable size there is no order to place, and
    a fractional lot would be rejected by the broker."""
    assert sizing.lots_for_notional(1000, 100, 4265.0,
                                    step=0.01, minimum=0.01) == 0.0


def test_a_missing_price_gives_None_not_a_size():
    """None is refusable; a zero or a default clip is not."""
    assert sizing.lots_for_notional(1_000_000, 100, None) is None
    assert sizing.lots_for_notional(1_000_000, 100, 0) is None
    assert sizing.lots_for_notional(0, 100, 4265.0) is None


# --- the hedge relationship ------------------------------------------------

def test_equal_contracts_at_beta_one_hedge_one_for_one():
    """Gold today: 100 oz on both legs, beta 1. Nothing changes here,
    which is why the bugs below never showed."""
    assert sizing.hedge_lots(50.0, 100, 100, 1.0) == pytest.approx(50.0)


def test_different_contract_sizes_are_accounted_for():
    """A 1,000 bbl spot CFD against a 100 bbl futures contract needs
    TEN futures lots per spot lot. The old formula traded one, leaving
    the pair 10x unbalanced with no warning."""
    assert sizing.hedge_lots(1.0, 1000, 100, 1.0) == pytest.approx(10.0)
    assert sizing.hedge_lots(1.0, 100, 1000, 1.0) == pytest.approx(0.1)


def test_beta_divides_the_hedge_it_does_not_multiply_it():
    """The inversion. spread = fut - beta*spot, so a beta of 2 means
    the futures leg moves twice as much per unit and needs HALF the
    size. The old code traded double."""
    assert sizing.hedge_lots(1.0, 100, 100, 2.0) == pytest.approx(0.5)
    assert sizing.hedge_lots(1.0, 100, 100, 0.5) == pytest.approx(2.0)


@pytest.mark.parametrize('beta', [0.5, 1.0, 1.5, 2.0])
@pytest.mark.parametrize('contract_a,contract_b', [(100, 100), (1000, 100),
                                                   (100, 1000), (50, 20)])
def test_a_pure_beta_move_nets_to_zero(beta, contract_a, contract_b):
    """The property the hedge exists for. If the futures price moves
    exactly beta times the spot move, the spread has not changed and a
    correctly-sized pair must make and lose nothing.

    This is the test that would have caught both bugs above: it fails
    for every beta != 1, and for every unequal contract pair, under the
    old `hedge = spot_lots * beta` rule."""
    lots_a = 3.0
    lots_b = sizing.hedge_lots(lots_a, contract_a, contract_b, beta)
    d_spot = 1.0
    d_fut = beta * d_spot                     # spread unchanged
    # Short spread: long leg A, short leg B.
    pnl = d_spot * lots_a * contract_a - d_fut * lots_b * contract_b
    assert pnl == pytest.approx(0.0, abs=1e-6)


def test_a_one_unit_spread_move_is_worth_the_spread_units():
    """And the scale factor that turns a spread move into dollars is
    leg B's units — not leg A's, which is what the exit ladder and the
    slippage report assume. Equal only at beta 1 with equal contracts."""
    lots_a, beta, c_a, c_b = 2.0, 1.5, 100, 50
    lots_b = sizing.hedge_lots(lots_a, c_a, c_b, beta)
    k = sizing.spread_units(lots_b, c_b)
    # Move the futures leg alone by 1.0: the spread moves 1.0 too.
    pnl = -1.0 * lots_b * c_b                 # short leg B
    assert pnl == pytest.approx(-1.0 * k)


def test_a_hedge_that_rounds_to_nothing_is_zero_not_a_token_lot():
    assert sizing.hedge_lots(0.01, 100, 100, 1.0,
                             step=0.1, minimum=0.1) == 0.0


# --- leverage --------------------------------------------------------------

def test_margin_is_notional_over_leverage():
    assert sizing.margin(1_000_000, 100) == pytest.approx(10_000)
    assert sizing.margin(1_000_000, 500) == pytest.approx(2_000)


def test_no_leverage_locks_the_whole_notional():
    """Unlevered is 1:1, not free."""
    assert sizing.margin(1_000_000, None) == pytest.approx(1_000_000)
    assert sizing.margin(1_000_000, 0) == pytest.approx(1_000_000)


# --- the resolved plan -----------------------------------------------------

def plan_for(config, mode='notional', notional=1_000_000, contract_b=None,
             price_a=4265.0, price_b=4324.0, beta=1.0, **meta):
    config.TRADING['SIZING_MODE'] = mode
    config.TRADING['NOTIONAL_PER_LEG_USD'] = notional
    config.TRADING['HEDGE_RATIO'] = beta
    return sizing.plan(config, 100, contract_b or 100, price_a, price_b,
                       meta_a={'volume_step': 0.01, 'volume_min': 0.01},
                       meta_b={'volume_step': 0.01, 'volume_min': 0.01},
                       **meta)


def test_notional_mode_sizes_both_legs_from_the_money(config):
    p = plan_for(config)
    assert p['mode'] == 'notional'
    assert p['leg_a_lots'] == pytest.approx(2.34, abs=0.005)
    assert p['leg_b_lots'] == pytest.approx(2.34, abs=0.005)
    assert p['leg_a_notional_usd'] == pytest.approx(998_010, abs=500)


def test_the_two_legs_come_out_balanced(config):
    """The owner's WTI/BRENT requirement, on two instruments priced
    within a few percent of each other."""
    p = plan_for(config, price_a=68.50, price_b=71.20)
    assert abs(p['imbalance_pct']) < 5.0


def test_the_residual_imbalance_is_stated_not_hidden(config):
    """Rounding to a tradable lot makes exact balance impossible, so
    the honest thing is to report what is left over."""
    p = plan_for(config, price_a=68.50, price_b=71.20)
    assert p['imbalance_usd'] == pytest.approx(
        p['leg_a_notional_usd'] - p['leg_b_notional_usd'])


def test_lots_mode_still_anchors_on_the_clip(config):
    config.TRADING['CLIP_LOTS'] = 5.0
    p = plan_for(config, mode='lots')
    assert p['leg_a_lots'] == pytest.approx(5.0)
    assert p['target_notional_usd'] is None


def test_a_notional_too_small_to_trade_is_refused_with_a_reason(config):
    p = plan_for(config, notional=100)
    assert p['leg_a_lots'] == 0.0
    assert 'below one tradable lot' in p['reason']


def test_no_price_is_refused_rather_than_defaulted(config):
    p = plan_for(config, price_a=None)
    assert p['leg_a_lots'] == 0.0
    assert 'live price' in p['reason']


def test_the_streak_reducer_still_scales_the_size(config):
    full = plan_for(config)
    half = plan_for(config, size_multiplier=0.5)
    assert half['leg_a_lots'] == pytest.approx(full['leg_a_lots'] * 0.5,
                                               abs=0.011)


def test_the_plan_reports_margin_per_leg(config):
    config.EXITS['SPOT_LEVERAGE'] = 100
    config.EXITS['FUT_LEVERAGE'] = 200
    p = plan_for(config)
    assert p['leg_a_margin_usd'] == pytest.approx(
        p['leg_a_notional_usd'] / 100)
    assert p['leg_b_margin_usd'] == pytest.approx(
        p['leg_b_notional_usd'] / 200)
    assert p['margin_usd'] == pytest.approx(p['leg_a_margin_usd']
                                            + p['leg_b_margin_usd'])


def test_unequal_contracts_still_balance_in_money(config):
    """The case a lot-based setting cannot express at all."""
    p = plan_for(config, contract_b=1000, price_a=68.50, price_b=68.60)
    # Within one volume step: the hedge rounds DOWN to something the
    # broker will accept, which is why the residual is reported.
    assert p['leg_b_lots'] == pytest.approx(p['leg_a_lots'] * 100 / 1000,
                                            abs=0.01)
    assert abs(p['imbalance_pct']) < 2.0
