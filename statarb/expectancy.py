"""What a trade is worth BEFORE it is placed.

The exit ladder freezes a take-profit and a stop in dollars at entry.
Those two numbers plus the round-trip cost already determine the
arithmetic of the trade — what has been missing is the one line that
states it:

    EV = p x TP  -  (1 - p) x (STOP + cost)

Everything here is about getting `p` honestly.

WHAT THE LEVELS MEAN (matching exits.evaluate, not approximating it):

- TAKE_PROFIT fires on NET >= tp_usd, so a win banks exactly +tp_usd
  net. The cost is already inside that number.
- DOLLAR_STOP fires on GROSS <= -stop_usd, so a loss books -stop_usd
  gross, which is -(stop_usd + cost) NET. The round trip is paid on the
  way out of a loser too, and an EV that forgets this flatters every
  trade by the cost of the trade.

So the loss leg is `stop_usd + rt_cost`, never `stop_usd`.

WHERE p COMES FROM

Two barriers, one diffusing spread. For a DRIFTLESS walk the answer is
famously just the distance ratio, `dz_stop / (dz_target + dz_stop)` —
and at that p the EV is exactly zero minus costs. That is the null
hypothesis, and it is worth printing beside the real one: it says "if
this spread does not actually revert, here is what the trade is worth",
which is always a loss equal to the round trip.

The reason we are in the trade at all is that the spread is supposed to
be mean-reverting. For an Ornstein-Uhlenbeck process the two-barrier
probability follows from the scale function, and in z-units (the
stationary standard deviation as the unit, which is exactly what a
z-score is) it collapses to something clean:

    dz = -theta z dt + sqrt(2 theta) dW   =>   S'(z) = exp(z^2 / 2)

    p = integral(z_entry -> z_stop) / integral(z_target -> z_stop)

**The reversion SPEED cancels.** theta is nowhere in that expression:
only the z geometry survives. That matters here, because this engine's
AR(1) half-life is fitted on quotes 0.6s apart and is often measuring
tick noise rather than the spread (hence EXITS.MIN_MAX_HOLD_SEC). A p
that depended on the half-life would inherit that unreliability. This
one depends only on sigma and the frozen dollar levels.

Replace exp(z^2/2) with 1 and the formula reduces exactly to the
driftless ratio, so the gap between the two probabilities IS the
mean-reversion edge, measured rather than asserted.

WHAT THIS DOES NOT MODEL — read before trusting the number:

- **The clock.** MAX_HOLD, HARD_MAX_HOLD_MIN and TIME_STOP can close a
  trade that reached neither barrier, and the reversion gate can bank
  less than the full target. This is the run-to-a-barrier EV, not the
  EV of the whole ladder. Time exits mostly convert would-be losers
  into smaller losers, so the realised spread of outcomes is tighter
  than this suggests, in both directions.
- **Sigma being real.** Every z here is dollars divided by
  `sigma x oz`. A collapsed sigma inflates the z-distances, which makes
  the target look far away and the stop look far away too — see
  SIGNALS.MIN_SIGMA.
- **A stationary mean.** OU reverts to a fixed mean; the engine's mu is
  a rolling one that chases the spread. This is the same caveat that
  makes exits act on money rather than on z.

So this is a decision aid with stated assumptions, not a forecast.
`realised` expectancy on the Analysis page is the number that actually
scores the strategy; this is what the trade looked like going in.
"""

import math


def break_even_win_rate(tp_usd, stop_usd, cost_usd=0.0):
    """The win rate this geometry needs just to break even.

    CLAUDE.md's tuning rule ("verify measured win rate clears
    stop/(target+stop)") in one function, with the cost included on the
    loss side where it belongs.
    """
    tp = float(tp_usd or 0)
    loss = float(stop_usd or 0) + float(cost_usd or 0)
    if tp <= 0 or loss <= 0:
        return None
    return loss / (tp + loss)


def expected_value(tp_usd, stop_usd, cost_usd, p_win):
    """EV in NET dollars. A win banks tp_usd net; a loss costs
    stop_usd gross, which is stop_usd + cost net."""
    if p_win is None:
        return None
    loss = float(stop_usd or 0) + float(cost_usd or 0)
    return p_win * float(tp_usd or 0) - (1 - p_win) * loss


def _scaled_integral(low, high, peak, steps=400):
    """integral of exp((u^2 - peak^2)/2) du over [low, high].

    Factored by the largest value in play so the integrand never leaves
    (0, 1]. Unfactored, exp(z^2/2) overflows a float around z = 38 and
    is already 10^54 at z = 15 — and MAX_ABS_Z defaults to 25. The
    factor cancels in the ratio these integrals are used for.
    """
    if high <= low:
        return 0.0
    steps = max(2, steps + steps % 2)          # Simpson needs it even
    width = (high - low) / steps
    total = 0.0
    for index in range(steps + 1):
        u = low + index * width
        weight = 1 if index in (0, steps) else (4 if index % 2 else 2)
        total += weight * math.exp((u * u - peak * peak) / 2)
    return total * width / 3


def ou_hit_probability(z_entry, z_target, z_stop):
    """P(spread reaches the target before the stop) under OU.

    Signs follow the trade, not the axis: `z_target` is nearer the mean
    than `z_entry` and `z_stop` is further away. Because OU is symmetric
    about its mean, a long-spread entry (negative z) is the mirror image
    of a short one, so callers pass distances and this works in |z|.
    """
    if not (z_target < z_entry < z_stop):
        # Already at or through a barrier — no diffusion required.
        if z_entry <= z_target:
            return 1.0
        if z_entry >= z_stop:
            return 0.0
        return None
    peak = max(abs(z_target), abs(z_stop), abs(z_entry))
    span = _scaled_integral(z_target, z_stop, peak)
    if span <= 0:
        return None
    return min(1.0, max(0.0, _scaled_integral(z_entry, z_stop, peak) / span))


def drift_free_probability(dz_target, dz_stop):
    """The same probability for a spread that does NOT revert — the
    null hypothesis. A driftless walk hits the nearer barrier more
    often in exact proportion to the distances."""
    total = float(dz_target or 0) + float(dz_stop or 0)
    if total <= 0:
        return None
    return float(dz_stop) / total


def trade_expectancy(tp_usd, stop_usd, cost_usd, entry_z, sigma, oz):
    """The full picture for one prospective trade.

    `oz` is the position's spread-to-dollars multiplier (lots x contract
    size, the same one the ladder uses for its levels), so a dollar
    level divided by `oz` is a spread distance and dividing again by
    `sigma` puts it in z.

    Returns a dict always carrying `reason` — None when the numbers are
    good, and a plain sentence when they are not. An unmeasurable EV
    must never render as 0.00, which reads as "break-even trade".
    """
    blank = {
        'ev_usd': None, 'ev_ratio': None, 'p_win': None,
        'p_win_drift_free': None, 'ev_drift_free_usd': None,
        'be_win_rate': None, 'reversion_edge': None,
        'win_usd': None, 'loss_usd': None,
        'z_entry': None, 'z_target': None, 'z_stop': None,
    }
    tp = float(tp_usd or 0)
    stop = float(stop_usd or 0)
    cost = float(cost_usd or 0)
    loss = stop + cost

    if tp <= 0:
        return dict(blank, reason='no take-profit is armed')
    if stop <= 0:
        return dict(blank, reason='no dollar stop is armed')
    if not sigma or not oz:
        return dict(blank, reason='no sigma yet — z distances unknown')

    unit = float(sigma) * float(oz)         # dollars per 1 sigma of z
    # The TP level is NET, so reaching it means travelling far enough to
    # cover the fees as well — the same +fees the ladder puts into its
    # spread levels.
    dz_target = (tp + cost) / unit
    dz_stop = stop / unit
    z0 = abs(entry_z or 0.0)

    p_win = ou_hit_probability(z0, z0 - dz_target, z0 + dz_stop)
    p_flat = drift_free_probability(dz_target, dz_stop)

    ev = expected_value(tp, stop, cost, p_win)
    # A target BEYOND the mean needs the spread to overshoot, not merely
    # to come home — the trade is no longer the mean-reversion bet the
    # z-score was measuring. build_plan's full-reversion veto keeps
    # SIGNAL entries out of this, but a manual target can land here, and
    # then the honest reading is that the clock decides the trade, not
    # the levels. Worth saying beside a p that still looks reassuring:
    # the barrier ratio can be favourable while both barriers are out of
    # reach.
    overshoot = (z0 - dz_target) < 0
    return {
        'ev_usd': ev,
        # EV per dollar risked: comparable across sizes and instruments.
        'ev_ratio': (ev / loss) if (ev is not None and loss > 0) else None,
        'p_win': p_win,
        'p_win_drift_free': p_flat,
        'ev_drift_free_usd': expected_value(tp, stop, cost, p_flat),
        'be_win_rate': break_even_win_rate(tp, stop, cost),
        'reversion_edge': (p_win - p_flat)
        if (p_win is not None and p_flat is not None) else None,
        'win_usd': tp,
        'loss_usd': loss,
        'z_entry': z0,
        'z_target': z0 - dz_target,
        'z_stop': z0 + dz_stop,
        'needs_overshoot': overshoot,
        'reason': None,
    }


def summarise(block):
    """One line for the log and the Telegram entry message."""
    if not block or block.get('reason'):
        return f"EV unavailable — {(block or {}).get('reason', 'no plan')}"
    text = (
        f"EV ${block['ev_usd']:+,.0f} per trade "
        f"({block['ev_ratio']:+.2f}R): "
        f"{block['p_win'] * 100:.0f}% x ${block['win_usd']:,.0f} "
        f"vs {(1 - block['p_win']) * 100:.0f}% x -${block['loss_usd']:,.0f}, "
        f"break-even needs {block['be_win_rate'] * 100:.0f}% "
        f"(no-reversion baseline "
        f"${block['ev_drift_free_usd']:+,.0f})")
    if block.get('needs_overshoot'):
        text += (f" — WARNING: the target sits at z {block['z_target']:+.1f}, "
                 f"PAST the mean, so it needs an overshoot rather than a "
                 f"reversion; expect the clock to close this trade")
    return text
