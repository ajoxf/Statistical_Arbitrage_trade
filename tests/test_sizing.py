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


# --- the health report (2026-08-07) ---------------------------------------

@pytest.fixture
def coord(tmp_path, monkeypatch, config):
    monkeypatch.chdir(tmp_path)
    from statarb.coordinator import Coordinator, PaperExecutor

    class Leg:
        name = 'broker'

        def ensure_symbol(self, sym):
            spot = 'XAU' in sym
            return {'ok': True,
                    'volume_step': 0.01 if spot else 0.1,
                    'volume_min': 0.01 if spot else 0.1,
                    'volume_max': 100.0, 'point': 0.01}

        def tick(self, s):
            return None

        def account_info(self):
            return {}

        def order_log(self, hours=24):
            return []

        def ping(self):
            return True

    config.TRADING.update({'SIZING_MODE': 'notional',
                           'NOTIONAL_PER_LEG_USD': 20_000.0,
                           'HEDGE_RATIO': 1.0})
    c = Coordinator(config, trading_mode='PAPER')
    c.spot_leg = c.futures_leg = Leg()
    c.executor = PaperExecutor(c.spot_leg, c.futures_leg, config)
    c.active_assets['GOLD'] = {'config': config.ASSETS['GOLD'],
                               'spot_symbol': 'XAUUSD_',
                               'futures_symbol': 'GC1226',
                               'last_data': None}
    return c


def gold_md(**over):
    md = {'spot_price': 4292.61, 'futures_price': 4351.55, 'spread': 58.94,
          'spot_bid': 4292.55, 'spot_ask': 4292.68,
          'futures_bid': 4351.38, 'futures_ask': 4351.72,
          'basis_pct': 1.37, 'tick_age_ms': 180, 'quote_id': 'q1'}
    md.update(over)
    return md


def states(coord, md=None):
    return {name: state for name, state, _ in
            coord._health('GOLD', md or gold_md())}


def details(coord, md=None):
    return {name: detail for name, _, detail in
            coord._health('GOLD', md or gold_md())}


def test_paper_sees_the_same_volume_limits_as_live(coord):
    """PaperExecutor has no `_meta`, so the plan used to be computed
    with no step and no minimum: fractional lots no broker would take,
    and the minimum-notional guard never fired in paper at all."""
    plan = coord._sizing_plan('GOLD', gold_md())
    assert plan['leg_a_lots'] == pytest.approx(0.05)     # stepped, not 0.0466
    assert plan['reason'] is not None                    # guard DOES fire


def test_symbol_limits_are_fetched_once(coord):
    """A RemoteLeg answers over IPC and this runs on every poll."""
    calls = []
    real = coord.spot_leg.ensure_symbol
    coord.spot_leg.ensure_symbol = lambda s: (calls.append(s), real(s))[1]
    for _ in range(5):
        coord._sizing_plan('GOLD', gold_md())
    assert len(calls) <= 2          # one per symbol, then cached


def test_the_health_report_names_every_subsystem(coord):
    """Operator: "I am more interested to get details on what is
    working and what is not working"."""
    assert set(states(coord)) == {'feed', 'stats', 'sizing', 'entries',
                                  'exits', 'risk'}


def test_a_dead_feed_is_reported_as_failed(coord):
    assert states(coord, gold_md(tick_age_ms=45_000))['feed'] == 'FAILED'
    assert 'check both terminals' in \
        details(coord, gold_md(tick_age_ms=45_000))['feed']


def test_the_blocking_gate_is_named(coord):
    coord.z_gen._blocking['GOLD'] = 'edge'
    assert states(coord)['entries'] == 'BLOCKED'
    assert 'edge' in details(coord)['entries']


def test_a_stopped_algo_says_exits_still_run(coord):
    coord.algo_enabled = False
    assert states(coord)['entries'] == '--'
    assert 'exits and armed manual trades still run' in \
        details(coord)['entries']


def test_the_sizing_refusal_appears_in_the_report(coord):
    """The operator's live blocker, stated in the health block rather
    than only when a trade is attempted."""
    assert states(coord)['sizing'] == 'BLOCKED'
    assert '42,926' in details(coord)['sizing']


def test_only_the_verdicts_decide_whether_to_reprint(coord):
    """Live numbers move every tick; reprinting on those would put the
    flood straight back."""
    first = coord._status_state('GOLD', gold_md())
    same = coord._status_state('GOLD', gold_md(spot_price=4293.9,
                                               spread=58.97))
    assert first == same
    changed = coord._status_state('GOLD', gold_md(tick_age_ms=45_000))
    assert changed != first


def test_a_position_that_will_not_close_is_the_loudest_line(coord):
    """Live 2026-08-07: both legs failed to close with 10013, and the
    very next health block read "exits -- flat" while the position sat
    open at the broker."""
    from statarb.models import OrderSide, SignalType as ST, Trade
    spot = Trade('XAUUSD_', OrderSide.BUY, 1.0)
    fut = Trade('GC1226', OrderSide.SELL, 1.0)
    spot.executed_price, fut.executed_price = 4335.11, 4394.03
    pos = coord.position_manager.create_position('GOLD', ST.SELL_BASIS,
                                                 spot, fut, 1.36)
    pos.close_failures = 2
    pos.last_close_error = '10013 - Invalid request'

    row = {name: (state, detail) for name, state, detail
           in coord._health('GOLD', gold_md())}['exits']
    assert row[0] == 'FAILED'
    assert 'WILL NOT CLOSE' in row[1]
    assert '10013' in row[1]
    assert 'Still open at the broker' in row[1]


def test_close_retries_are_rate_limited(coord):
    """The position stays ACTIVE so the ladder keeps asking, which
    without a limit re-sends the order three times a second."""
    from datetime import datetime, timedelta
    from statarb.models import OrderSide, SignalType as ST, Trade
    pos = coord.position_manager.create_position(
        'GOLD', ST.SELL_BASIS, Trade('XAUUSD_', OrderSide.BUY, 1.0),
        Trade('GC1226', OrderSide.SELL, 1.0), 1.36)

    assert coord._close_is_due(pos)                  # never tried
    pos.close_failures = 1
    pos.last_close_attempt = datetime.now()
    assert not coord._close_is_due(pos)              # too soon
    pos.last_close_attempt = datetime.now() - timedelta(seconds=30)
    assert coord._close_is_due(pos)                  # cooldown elapsed


# --- the round trip is priced per leg -------------------------------

def test_each_leg_pays_its_own_spread_on_its_own_units():
    """Live 2026-08-10 on XAGUSD/XAUUSD the cost card read:

        XAGUSD spread 0.0460 x 5000 = $230.00
        XAUUSD spread 0.2400 x 5000 = $1200.00   <-- gold is 100/lot

    Both legs were multiplied by LEG A's units. Gold's real cost on
    1.15 lots of a 100-unit contract is 0.24 x 100 x 1.15 = $27.60,
    not $1,200 — and the edge filter, the exit ladder's cost floor and
    the expected value all read that number.
    """
    from statarb import costs

    md = {'spot_bid': 64.633, 'spot_ask': 64.679,
          'futures_bid': 4349.91, 'futures_ask': 4350.15}
    cfg = {'SPREAD_COST_FACTOR': 1.0}

    cost = costs.round_trip_cost(md, lots=1.55, contract_size=5000.0,
                                 costs_cfg=cfg,
                                 lots_b=1.15, contract_b=100.0)
    silver = 0.046 * 1.55 * 5000
    gold = 0.24 * 1.15 * 100
    assert cost == pytest.approx(silver + gold)
    assert cost == pytest.approx(356.5 + 27.6, rel=1e-3)
    # ...and nowhere near the figure the old model produced
    assert cost < 1000


def test_leg_b_defaults_to_leg_a_so_matched_pairs_are_unchanged():
    """Gold spot vs its future: 100 oz both legs, equal lots. The old
    single-size call must give exactly the same answer."""
    from statarb import costs

    md = {'spot_bid': 4292.55, 'spot_ask': 4292.68,
          'futures_bid': 4351.38, 'futures_ask': 4351.72}
    cfg = {'SPREAD_COST_FACTOR': 1.0}
    one = costs.round_trip_cost(md, 1.15, 100.0, cfg)
    both = costs.round_trip_cost(md, 1.15, 100.0, cfg,
                                 lots_b=1.15, contract_b=100.0)
    assert one == pytest.approx(both)
    assert one == pytest.approx((0.13 + 0.34) * 1.15 * 100, rel=1e-9)


def test_commission_is_charged_on_each_legs_own_lots():
    from statarb import costs

    md = {'spot_bid': 100.0, 'spot_ask': 100.0,
          'futures_bid': 100.0, 'futures_ask': 100.0}
    cfg = {'COMMISSION_PER_LOT_SPOT': 10.0, 'COMMISSION_PER_LOT_FUT': 20.0}
    cost = costs.round_trip_cost(md, lots=2.0, contract_size=100.0,
                                 costs_cfg=cfg, lots_b=0.5, contract_b=1000.0)
    assert cost == pytest.approx(10.0 * 2.0 + 20.0 * 0.5)


# --- the broker's ceiling, on BOTH legs -----------------------------

def _plan(config, beta, **meta_b_extra):
    config.TRADING.update({'SIZING_MODE': 'lots', 'CLIP_LOTS': 1.54,
                           'HEDGE_RATIO': beta, 'HEDGE_MODE': 'units'})
    meta_a = {'volume_min': 0.01, 'volume_step': 0.01, 'volume_max': 50.0}
    meta_b = {'volume_min': 0.01, 'volume_step': 0.01, 'volume_max': 50.0}
    meta_b.update(meta_b_extra)
    return sizing.plan(config, 5000.0, 100.0, 65.10, 4360.10,
                       meta_a=meta_a, meta_b=meta_b)


def test_an_oversized_hedge_leg_is_refused(config):
    """Live 2026-08-10: an inverted HEDGE_RATIO (0.0149 where 67 was
    meant) sized leg B at 5,167.78 lots of gold — $2.25 BILLION — and
    the plan reported it as fine. Minimums were always checked;
    maximums never were, and MAX_LOT_SIZE only ever measured leg A.
    """
    result = _plan(config, beta=0.0149)
    assert result['leg_b_lots'] > 5000
    assert result['reason']
    assert 'maximum is 50' in result['reason']
    assert 'HEDGE_RATIO 0.0149' in result['reason']
    # and it names the direction of the mistake
    assert 'too SMALL' in result['reason']


def test_the_right_beta_passes_the_same_check(config):
    """67 on the same pair gives ~1.15 lots — well inside the ceiling.
    The hedge rounds DOWN (1.1498 -> 1.14): short is the recoverable
    error, and the executor trims leg A to the matched size."""
    result = _plan(config, beta=66.97)
    assert result['leg_b_lots'] == pytest.approx(1.14, abs=0.001)
    assert not result['reason']


def test_leg_a_has_a_ceiling_too(config):
    config.TRADING.update({'SIZING_MODE': 'lots', 'CLIP_LOTS': 500.0,
                           'HEDGE_RATIO': 1.0})
    result = sizing.plan(
        config, 100.0, 100.0, 4292.0, 4351.0,
        meta_a={'volume_min': 0.01, 'volume_step': 0.01, 'volume_max': 50.0},
        meta_b={'volume_min': 0.01, 'volume_step': 0.01, 'volume_max': 50.0})
    assert result['reason'] and 'leg A wants 500' in result['reason']


def test_a_broker_that_reports_no_maximum_is_not_second_guessed(config):
    result = _plan(config, beta=0.0149, volume_max=None)
    assert not result['reason']      # nothing to check it against


# --- "per leg" has to mean something on BOTH legs -------------------

def _notional_plan(config, hedge_mode, beta=0.0150):
    config.TRADING.update({'SIZING_MODE': 'notional',
                           'NOTIONAL_PER_LEG_USD': 500_000.0,
                           'HEDGE_RATIO': beta, 'HEDGE_MODE': hedge_mode})
    meta = {'volume_min': 0.01, 'volume_step': 0.01, 'volume_max': 100000.0}
    return sizing.plan(config, 5000.0, 100.0, 65.191, 4360.51,
                       meta_a=dict(meta), meta_b=dict(meta))


def test_a_hedge_nowhere_near_the_money_asked_for_is_refused(config):
    """Live 2026-08-10: "$500,000 per leg" gave $498,726 on XAGUSD and
    $2,238,784,332 on XAUUSD, and the card still said "Asked for
    $500,000 per leg". An inverted beta, reported as fine."""
    result = _notional_plan(config, 'units', beta=0.0150)
    assert result['leg_a_notional_usd'] == pytest.approx(500_000, rel=0.01)
    assert result['reason']
    assert 'HEDGE_RATIO 0.015' in result['reason']
    assert 'price ratio' in result['reason']


def test_the_right_beta_lands_both_legs_on_the_target(config):
    """At beta near the price ratio, unit-neutral and dollar-neutral
    coincide, so "per leg" comes out true on both sides."""
    result = _notional_plan(config, 'units', beta=66.89)
    assert result['leg_b_notional_usd'] == pytest.approx(500_000, rel=0.02)
    assert not result['reason']


def test_the_hedge_construction_is_still_the_owners_choice(config):
    """Unit-neutral is NOT overridden: for a basis pair the pair's P&L
    is the spread move the z-score is measured on, and the legs'
    notionals differing by the basis is the trade, not an error."""
    config.TRADING.update({'SIZING_MODE': 'notional',
                           'NOTIONAL_PER_LEG_USD': 2_000_000.0,
                           'HEDGE_RATIO': 1.0, 'HEDGE_MODE': 'units'})
    meta = {'volume_min': 0.01, 'volume_step': 0.01, 'volume_max': 1000.0}
    result = sizing.plan(config, 100.0, 100.0, 4292.61, 4351.55,
                         meta_a=dict(meta), meta_b=dict(meta))
    assert result['hedge_mode'] == 'units'
    assert not result['reason']
    # the legs differ by the basis, ~1.4%, and that is allowed
    gap = abs(result['leg_b_notional_usd'] - result['leg_a_notional_usd'])
    assert gap / result['leg_a_notional_usd'] < 0.05
