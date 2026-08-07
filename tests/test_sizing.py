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


def test_lots_snap_to_the_NEAREST_tradable_step():
    """The notional is a target, not a ceiling. Flooring cost a fifth
    of a small position and produced the operator's "Leg A notional
    calculated incorrectly" (2026-08-07)."""
    # 2.3446 exact -> 2.3 either way at a 0.1 step
    assert sizing.lots_for_notional(1_000_000, 100, 4265.0,
                                    step=0.1) == pytest.approx(2.3)
    # 0.0466 exact: floor gives 0.04 (-14%), nearest gives 0.05 (+7%)
    assert sizing.lots_for_notional(20_000, 100, 4292.61,
                                    step=0.01) == pytest.approx(0.05)


def test_a_hedge_never_rounds_UP(config):
    """Leg B's step is ten times leg A's, so nearest would turn a
    wanted 0.05 into 0.1 — a hedge twice the size of the position it
    hedges, and past the minimum-notional guard that exists to catch
    exactly that."""
    assert sizing.hedge_lots(0.05, 100, 100, 1.0,
                             step=0.1, minimum=0.1) == 0.0
    assert sizing.hedge_lots(0.19, 100, 100, 1.0,
                             step=0.1, minimum=0.1) == pytest.approx(0.1)


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


def test_a_notional_too_small_to_trade_names_the_minimum(config):
    """The refusal has to carry the number the operator can act on.
    "the hedge rounds to zero" is the same fact with the useful part
    left out (operator, 2026-08-07)."""
    p = plan_for(config, notional=100)
    assert p['leg_a_lots'] == 0.0
    assert 'minimum of $' in p['reason']
    assert p['min_notional_usd'] == pytest.approx(4265.0, abs=1)


def test_the_binding_minimum_is_the_larger_legs(config):
    """Live on CFI: spot XAUUSD_ trades from 0.01 lots but futures
    GC1226 from 0.1, so the futures leg sets the floor at ten times
    what leg A alone would need. Sizing to leg A's minimum produces a
    spot order the hedge cannot match."""
    p = sizing.plan(config, 100, 100, 4292.61, 4351.55,
                    meta_a={'volume_step': 0.01, 'volume_min': 0.01},
                    meta_b={'volume_step': 0.1, 'volume_min': 0.1})
    assert p['min_notional_usd'] == pytest.approx(42_926, abs=5)


def test_the_operators_20k_on_gold_is_refused_with_the_real_floor(config):
    """Exactly the screenshot: $20,000 gives 0.04 spot lots and a hedge
    of 0.04 against a 0.1-lot minimum, so leg B is zero and the pair is
    100% unbalanced."""
    config.TRADING.update({'SIZING_MODE': 'notional',
                           'NOTIONAL_PER_LEG_USD': 20_000.0,
                           'HEDGE_RATIO': 1.0})
    p = sizing.plan(config, 100, 100, 4292.61, 4351.55,
                    meta_a={'volume_step': 0.01, 'volume_min': 0.01},
                    meta_b={'volume_step': 0.1, 'volume_min': 0.1})
    # Leg A rounds to NEAREST (0.0466 -> 0.05, the target is a target),
    # the hedge rounds DOWN (0.05 -> 0 against a 0.1 step, because a
    # hedge must never overshoot) — so the pair is still refused, with
    # the floor named.
    assert p['leg_a_lots'] == pytest.approx(0.05)
    assert p['leg_b_lots'] == 0.0
    assert "0.1-lot minimum" in p['reason']
    assert '42,926' in p['reason']


def gold_plan(config, notional):
    config.TRADING.update({'SIZING_MODE': 'notional',
                           'NOTIONAL_PER_LEG_USD': notional,
                           'HEDGE_RATIO': 1.0})
    return sizing.plan(config, 100, 100, 4292.61, 4351.55,
                       meta_a={'volume_step': 0.01, 'volume_min': 0.01},
                       meta_b={'volume_step': 0.1, 'volume_min': 0.1})


def test_a_coarse_step_on_leg_b_leaves_a_real_imbalance(config):
    """Above the floor the pair trades — but leg B's 0.1-lot STEP is
    ten times leg A's, so at small sizes the hedge is coarse: 0.23 spot
    lots against 0.2 futures is 12% under-hedged, and that residual is
    naked directional risk, not basis. The card shows it in red."""
    p = gold_plan(config, 100_000.0)
    assert p['reason'] is None
    assert p['leg_a_lots'] == pytest.approx(0.23)
    assert p['leg_b_lots'] == pytest.approx(0.2)      # stepped down
    assert abs(p['imbalance_pct']) > 2                # flagged, not hidden


def test_size_washes_the_step_rounding_out(config):
    """The same coarse step shrinks as a fraction of the position: at
    $20k one futures step is 21% of the trade, at $2m it is 2%."""
    small = gold_plan(config, 100_000.0)
    big = gold_plan(config, 2_000_000.0)
    assert big['reason'] is None
    assert abs(big['imbalance_pct']) < abs(small['imbalance_pct']) / 4


def test_leg_a_lands_near_the_notional_that_was_asked_for(config):
    """Operator, 2026-08-07: "Why is Leg A notional being calculated
    incorrectly" — $20,000 requested, $17,170 shown. It was rounding
    DOWN, and one 0.01 gold lot is $4,293, so it lost a fifth of the
    position to the rounding rule alone."""
    p = gold_plan(config, 20_000.0)
    assert p['leg_a_lots'] == pytest.approx(0.05)          # not 0.04
    assert p['leg_a_notional_usd'] == pytest.approx(21_463, abs=5)
    # Under the old floor rule this was -14.1%.
    assert p['notional_gap_pct'] == pytest.approx(7.3, abs=0.2)


def test_the_size_of_one_step_is_published(config):
    """The gap is not a defect to hide — it is the granularity of the
    instrument, and at small sizes it dominates."""
    p = gold_plan(config, 20_000.0)
    assert p['lot_step_usd'] == pytest.approx(4292.61, abs=1)
    big = gold_plan(config, 2_000_000.0)
    assert abs(big['notional_gap_pct']) < 0.5      # invisible at size


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


# --- the settings have to actually reach the running engine ---------------

def test_sizing_settings_hot_reload_into_a_running_engine(tmp_path):
    """Operator, 2026-08-07: saved notional sizing, and the dashboard
    kept reporting "Sized from 50 lots per leg". A key absent from
    HOT_TRADING_KEYS is written to config.json and then ignored until a
    restart — silently, because hot_apply only reports what it DID
    apply."""
    from statarb.config import AlgoTradingConfig
    live = AlgoTradingConfig()
    fresh = AlgoTradingConfig()
    fresh.TRADING['SIZING_MODE'] = 'notional'
    fresh.TRADING['NOTIONAL_PER_LEG_USD'] = 20_000.0

    applied, blocked = live.hot_apply(fresh)
    assert 'TRADING.SIZING_MODE' in applied
    assert 'TRADING.NOTIONAL_PER_LEG_USD' in applied
    assert live.TRADING['SIZING_MODE'] == 'notional'
    assert live.TRADING['NOTIONAL_PER_LEG_USD'] == 20_000.0
    assert not blocked


def test_leverage_hot_reloads_and_changes_the_margin(tmp_path):
    """Owner: "if I change the leverage on the legs - the notional
    value calculations should reflect accordingly and not be 100x
    always"."""
    from statarb.config import AlgoTradingConfig
    live = AlgoTradingConfig()
    live.TRADING.update({'SIZING_MODE': 'notional',
                         'NOTIONAL_PER_LEG_USD': 1_000_000.0})
    live.EXITS['SPOT_LEVERAGE'] = 100
    live.EXITS['FUT_LEVERAGE'] = 100
    before = sizing.plan(live, 100, 100, 4265.0, 4324.0)
    assert before['leg_a_leverage'] == 100

    # Only the leverage differs; hot_apply copies whatever `fresh`
    # holds, so the sizing keys have to carry over too.
    fresh = AlgoTradingConfig()
    fresh.TRADING.update({'SIZING_MODE': 'notional',
                          'NOTIONAL_PER_LEG_USD': 1_000_000.0})
    fresh.EXITS['SPOT_LEVERAGE'] = 20
    fresh.EXITS['FUT_LEVERAGE'] = 50
    live.hot_apply(fresh)

    after = sizing.plan(live, 100, 100, 4265.0, 4324.0)
    assert after['leg_a_leverage'] == 20
    assert after['leg_b_leverage'] == 50
    # Same notional, five times the margin on leg A.
    assert after['leg_a_notional_usd'] == pytest.approx(
        before['leg_a_notional_usd'])
    assert after['leg_a_margin_usd'] == pytest.approx(
        before['leg_a_margin_usd'] * 5)


def test_the_two_legs_can_carry_different_leverage():
    """They are different accounts at (possibly) different brokers."""
    from statarb.config import AlgoTradingConfig
    cfg = AlgoTradingConfig()
    cfg.TRADING.update({'SIZING_MODE': 'notional',
                        'NOTIONAL_PER_LEG_USD': 1_000_000.0})
    cfg.EXITS['SPOT_LEVERAGE'] = 30
    cfg.EXITS['FUT_LEVERAGE'] = 200
    p = sizing.plan(cfg, 100, 100, 4265.0, 4324.0)
    assert p['leg_a_margin_usd'] == pytest.approx(
        p['leg_a_notional_usd'] / 30)
    assert p['leg_b_margin_usd'] == pytest.approx(
        p['leg_b_notional_usd'] / 200)


def test_the_engine_publishes_the_leverage_it_used():
    """The card read leg_a_leverage/leg_b_leverage; nothing published
    them, so it kept the number baked into the page at load time and
    showed the same leverage however the settings changed."""
    from statarb import webapi
    ui = webapi.status_to_ui({'assets': [{
        'asset': 'GOLD', 'z': 1.0,
        'sizing': {'leg_a_leverage': 20, 'leg_b_leverage': 50,
                   'leg_a_margin_usd': 1000.0, 'leg_b_margin_usd': 400.0},
    }]}, {})
    assert ui['signal']['leg_a_leverage'] == 20
    assert ui['signal']['leg_b_leverage'] == 50
    assert ui['signal']['leg_a_margin'] == 1000.0


# --- dollar-neutral hedging (owner asked for it, 2026-08-07) --------------

def test_unit_neutral_holds_the_same_quantity_on_both_legs(config):
    """The default, and the right hedge for a basis pair: the two legs'
    dollar values differ by the basis, which IS the trade."""
    p = gold_plan(config, 2_000_000.0)
    assert p['hedge_mode'] == 'units'
    assert p['leg_a_lots'] * 100 == pytest.approx(p['leg_b_lots'] * 100,
                                                  rel=0.02)


def test_dollar_neutral_puts_the_same_money_on_each_leg(config):
    config.TRADING['HEDGE_MODE'] = 'notional'
    p = gold_plan(config, 2_000_000.0)
    assert p['hedge_mode'] == 'notional'
    # Within one 0.1-lot step of exactly equal notional.
    assert abs(p['imbalance_pct']) < 2.5
    assert p['leg_b_lots'] < p['leg_a_lots']    # leg B is dearer per oz


def test_dollar_neutral_ignores_beta_and_uses_the_prices(config):
    """L_B*C_B*P_B = L_A*C_A*P_A — beta plays no part."""
    lots_units = sizing.hedge_lots(2.0, 100, 100, 2.0, mode='units')
    lots_money = sizing.hedge_lots(2.0, 100, 100, 2.0, mode='notional',
                                   price_a=4292.61, price_b=4351.55)
    assert lots_units == pytest.approx(1.0)              # halved by beta
    assert lots_money == pytest.approx(2.0 * 4292.61 / 4351.55)


def test_dollar_neutral_trades_the_return_spread(config):
    """The defining property: equal money means the P&L is the
    difference in RETURNS, so two legs moving the same percentage net
    to zero however different their prices."""
    lots_a, c_a, c_b, p_a, p_b = 2.0, 100, 100, 4292.61, 4351.55
    lots_b = sizing.hedge_lots(lots_a, c_a, c_b, 1.0, mode='notional',
                               price_a=p_a, price_b=p_b)
    move = 0.01                                    # both legs +1%
    pnl = (p_a * move) * lots_a * c_a - (p_b * move) * lots_b * c_b
    assert pnl == pytest.approx(0.0, abs=1e-6)


def test_unit_neutral_does_NOT_net_out_a_common_percentage_move(config):
    """The flip side, and why this is a real choice rather than a
    preference: equal ounces means a 1% move in both legs leaves the
    basis difference on the table."""
    lots_b = sizing.hedge_lots(2.0, 100, 100, 1.0, mode='units')
    move = 0.01
    pnl = (4292.61 * move) * 2.0 * 100 - (4351.55 * move) * lots_b * 100
    assert abs(pnl) > 100                          # not neutral in money


def test_the_beta_disagreement_is_reported(config):
    """Dollar-neutral sizing and a fixed HEDGE_RATIO agree only when
    beta equals the price ratio. Anywhere else the position does not
    track the spread the z-score is built on, so say so."""
    config.TRADING['HEDGE_MODE'] = 'notional'
    p = gold_plan(config, 2_000_000.0)
    assert p['dollar_neutral_beta'] == pytest.approx(4351.55 / 4292.61)
    assert p['beta_gap_pct'] == pytest.approx(-1.354, abs=0.01)


def test_the_hedge_mode_hot_reloads():
    from statarb.config import AlgoTradingConfig
    live, fresh = AlgoTradingConfig(), AlgoTradingConfig()
    fresh.TRADING['HEDGE_MODE'] = 'notional'
    applied, _ = live.hot_apply(fresh)
    assert 'TRADING.HEDGE_MODE' in applied
    assert live.TRADING['HEDGE_MODE'] == 'notional'
