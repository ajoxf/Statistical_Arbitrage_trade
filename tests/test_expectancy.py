"""Expected value of a trade at entry.

The maths has to be right before the number goes on a screen an
operator makes decisions from, so these check the formula against
results that are known independently rather than against itself.
"""

import math

import pytest

from statarb import expectancy as ev_mod


# --- the arithmetic of the two legs ---------------------------------

def test_the_loss_leg_includes_the_round_trip():
    """TAKE_PROFIT fires on NET, DOLLAR_STOP on GROSS. A loss therefore
    costs the stop PLUS the fees, and an EV that forgets that flatters
    every trade by the price of the trade."""
    # 50% odds, symmetric $100 levels, $40 of costs
    ev = ev_mod.expected_value(tp_usd=100, stop_usd=100, cost_usd=40,
                               p_win=0.5)
    assert ev == pytest.approx(0.5 * 100 - 0.5 * 140)
    assert ev == pytest.approx(-20.0)

    # Without the cost on the loss side it would read as break-even
    assert ev_mod.expected_value(100, 100, 0, 0.5) == pytest.approx(0.0)


def test_break_even_win_rate_is_the_tuning_rule():
    """CLAUDE.md: win rate must clear stop/(target+stop)."""
    assert ev_mod.break_even_win_rate(200, 100, 0) == pytest.approx(1 / 3)
    # Costs raise the bar
    assert ev_mod.break_even_win_rate(200, 100, 50) == pytest.approx(150 / 350)


def test_break_even_is_exactly_the_zero_ev_point():
    p = ev_mod.break_even_win_rate(254, 847, 47)
    assert ev_mod.expected_value(254, 847, 47, p) == pytest.approx(0.0,
                                                                   abs=1e-9)


def test_an_unarmed_level_reports_why_not_zero():
    for kwargs in ({'tp_usd': 0}, {'stop_usd': 0}, {'sigma': 0}):
        block = ev_mod.trade_expectancy(
            **{**dict(tp_usd=100, stop_usd=100, cost_usd=10, entry_z=3.0,
                      sigma=0.2, oz=100), **kwargs})
        assert block['ev_usd'] is None
        assert block['reason']          # a sentence, never a silent 0.00


# --- the hitting probability ----------------------------------------

def test_ou_reduces_to_the_distance_ratio_when_the_barriers_hug_the_mean():
    """Near z = 0 the OU drift is negligible over a short span, so the
    answer must approach the driftless walk's b/(a+b)."""
    p = ev_mod.ou_hit_probability(0.0, -0.001, 0.003)
    assert p == pytest.approx(0.003 / 0.004, rel=1e-3)


def test_mean_reversion_beats_a_coin_flip_at_the_same_distances():
    """The whole premise of the strategy: from an extreme z, the target
    (toward the mean) is likelier than the stop (further out), by more
    than the distances alone would give."""
    dz_target, dz_stop = 1.0, 1.0        # equidistant barriers
    p_ou = ev_mod.ou_hit_probability(3.0, 2.0, 4.0)
    p_flat = ev_mod.drift_free_probability(dz_target, dz_stop)
    assert p_flat == pytest.approx(0.5)
    assert p_ou > 0.5
    # And the further out the entry, the stronger the pull home
    assert ev_mod.ou_hit_probability(4.0, 3.0, 5.0) > p_ou


def test_the_reversion_speed_cancels_out():
    """theta appears nowhere in the scale function once z is the unit.
    This is why the number does not inherit the AR(1) half-life's
    unreliability on a tick-noise fit."""
    # Same z geometry reached with different sigma/oz scaling
    slow = ev_mod.trade_expectancy(tp_usd=100, stop_usd=100, cost_usd=0,
                                   entry_z=3.0, sigma=0.2, oz=500)
    fast = ev_mod.trade_expectancy(tp_usd=200, stop_usd=200, cost_usd=0,
                                   entry_z=3.0, sigma=0.4, oz=500)
    assert slow['z_target'] == pytest.approx(fast['z_target'])
    assert slow['p_win'] == pytest.approx(fast['p_win'])


def test_a_barrier_already_reached_is_certainty_not_a_number():
    assert ev_mod.ou_hit_probability(1.0, 1.0, 3.0) == 1.0
    assert ev_mod.ou_hit_probability(3.0, 1.0, 3.0) == 0.0


def test_probabilities_stay_in_range_at_extreme_z():
    """exp(z^2/2) overflows a float near z = 38 and MAX_ABS_Z is 25, so
    the integral is deliberately computed factored."""
    for z in (5.0, 10.0, 20.0, 25.0):
        p = ev_mod.ou_hit_probability(z, z - 1.0, z + 1.0)
        assert p is not None and 0.0 <= p <= 1.0
        assert not math.isnan(p)
    # Far from the mean, reversion is overwhelming
    assert ev_mod.ou_hit_probability(20.0, 19.0, 21.0) > 0.99


# --- the whole block ------------------------------------------------

def test_a_real_gold_plan_reads_sensibly():
    """The operator's own live numbers: TP $254, STOP $847, cost $47,
    entry z 3, sigma 0.2247, 110 oz."""
    block = ev_mod.trade_expectancy(tp_usd=254, stop_usd=847, cost_usd=47,
                                    entry_z=3.0, sigma=0.2247, oz=110)
    assert block['reason'] is None
    assert block['win_usd'] == 254 and block['loss_usd'] == 894
    # A 3.5:1 loss/win ratio needs a very high hit rate to pay
    assert block['be_win_rate'] == pytest.approx(894 / 1148, rel=1e-6)
    assert 0 < block['p_win'] < 1
    assert block['reversion_edge'] > 0     # OU beats the coin flip
    assert block['ev_usd'] == pytest.approx(
        ev_mod.expected_value(254, 847, 47, block['p_win']))


def test_the_no_reversion_baseline_is_always_a_loss():
    """If the spread does not revert, the trade is worth minus the round
    trip — whatever the levels. That is the null hypothesis the real EV
    has to beat, and it makes the cost impossible to overlook."""
    for tp, stop, cost in ((254, 847, 47), (1000, 200, 60), (75, 75, 12)):
        block = ev_mod.trade_expectancy(tp, stop, cost, entry_z=3.0,
                                        sigma=0.2, oz=100)
        assert block['ev_drift_free_usd'] == pytest.approx(-cost, abs=1e-6)


def test_summarise_never_prints_a_misleading_zero():
    blank = ev_mod.trade_expectancy(0, 100, 10, 3.0, 0.2, 100)
    assert 'unavailable' in ev_mod.summarise(blank)
    assert '0.00' not in ev_mod.summarise(blank)

    good = ev_mod.trade_expectancy(254, 847, 47, 3.0, 0.2247, 110)
    text = ev_mod.summarise(good)
    assert 'EV $' in text and 'break-even needs' in text


def test_a_target_past_the_mean_is_called_out():
    """A signal entry can never land here (build_plan vetoes a target
    above a full reversion), but a manual one can — and then the trade
    needs an OVERSHOOT, not a reversion, which is a different bet from
    the one the z-score measured. The barrier ratio can look reassuring
    while both barriers are out of reach and the clock decides."""
    block = ev_mod.trade_expectancy(tp_usd=254, stop_usd=847, cost_usd=47,
                                    entry_z=3.0, sigma=0.2247, oz=110)
    assert block['z_target'] < 0
    assert block['needs_overshoot'] is True
    assert 'PAST the mean' in ev_mod.summarise(block)

    # A target INSIDE a full reversion is the normal case and is silent
    normal = ev_mod.trade_expectancy(tp_usd=20, stop_usd=100, cost_usd=10,
                                     entry_z=3.0, sigma=0.2247, oz=110)
    assert normal['z_target'] > 0
    assert normal['needs_overshoot'] is False
    assert 'PAST the mean' not in ev_mod.summarise(normal)


def test_the_cost_is_visible_as_a_distance_in_sigma():
    """The edge problem stated in the unit that matters. At sigma
    0.2247 on 110 oz, one sigma of spread is worth $24.72, so a $47
    round trip is 1.9 SIGMA of travel before a cent of profit. From
    z = 3 that leaves only 1.1 sigma of reachable target — which is why
    a $35 target already needs an overshoot on this pair, and why the
    edge filter refuses these trades."""
    unit = 0.2247 * 110
    assert unit == pytest.approx(24.717, rel=1e-3)

    # Break-even alone eats most of the available reversion
    cost_in_sigma = 47 / unit
    assert cost_in_sigma == pytest.approx(1.90, rel=0.01)

    # So the largest target still inside a full reversion is small
    biggest = 3.0 * unit - 47
    assert biggest == pytest.approx(27.2, rel=0.01)
    assert ev_mod.trade_expectancy(biggest - 1, 100, 47, 3.0, 0.2247,
                                   110)['needs_overshoot'] is False
    assert ev_mod.trade_expectancy(biggest + 1, 100, 47, 3.0, 0.2247,
                                   110)['needs_overshoot'] is True
