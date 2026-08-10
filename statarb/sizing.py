"""How many lots each leg trades.

Owner (2026-08-07): "the way we had it before in the W3 project is —
User fixes the notional value of the leg and the lots are calculated by
itself and after considering the leverage. The User saves the leg
Notional Value in the Settings page."

Two modes:

    lots      CLIP_LOTS is the anchor. What this engine has always
              done. Fine for one instrument you know well, but the lot
              is a different amount of money on every symbol, so the
              same number means $43m of gold and $50k of oil.
    notional  NOTIONAL_PER_LEG_USD is the anchor and the lots follow
              from the live price. What the operator asked for, and
              the only mode in which "balanced" means anything across
              two different instruments.

## Why the hedge is not simply `spot_lots x HEDGE_RATIO`

The spread is `S = P_fut - beta * P_spot` (owner's definition). Hold
L_A lots of leg A and L_B lots of leg B, with contract sizes C_A and
C_B. For a short-spread position (long A, short B) the P&L of a price
move is

    P&L = dP_spot * L_A*C_A  -  dP_fut * L_B*C_B

and we want that to be exactly `-dS * k` for a positive scale k:

    -dS * k = -dP_fut * k + beta * dP_spot * k

Matching the two coefficients gives `L_B*C_B = k` and
`L_A*C_A = beta * k`, so

    L_A * C_A = beta * L_B * C_B          (units, not lots)

The engine used to size the hedge as `L_B = L_A * beta`, which is the
same thing ONLY when beta is 1 and both contract sizes are equal —
which is exactly the configuration it has been run in (gold, 100 oz on
both legs, beta 1.0), so the error has never shown. Away from there it
is wrong in both directions at once:

  * Different contract sizes (1,000 bbl spot CFD vs 100 bbl futures)
    leave the pair 10x unbalanced with no warning.
  * beta != 1 INVERTS it. With beta = 2 the correct hedge is half the
    spot lots; the old formula traded double, so a move that should
    have netted zero produced a loss three times the size of the
    intended one.

`k` above is also the number that turns a spread move into dollars, and
it is `L_B * C_B` — the LEG B units, not leg A's. Same caveat: equal at
beta 1 with equal contracts, different otherwise.
"""

import math


def round_step(volume, step, minimum=0.0, down=False):
    """Snap to a tradable volume.

    NEAREST by default. Flooring was the original rule and it is wrong
    for a target: the operator asks for $20,000 a leg, gold is $4,293 a
    lot at 0.01 lots, so the exact answer is 0.0466 lots and flooring
    gives 0.04 — $17,170, fourteen percent short, with nothing on
    screen explaining the gap (operator, 2026-08-07: "Why is Leg A
    notional being calculated incorrectly"). Nearest gives 0.05 and
    halves the error.

    It matters most exactly where it is least visible: one step is 21%
    of a $20,000 gold position but 0.2% of a $2m one, so the rule is
    invisible at size and dominant when small.

    `down=True` where overshooting is genuinely unsafe."""
    if step and step > 0:
        scaled = volume / step
        volume = (math.floor(scaled + 1e-9) if down
                  else math.floor(scaled + 0.5 + 1e-9)) * step
        volume = round(volume, 8)
    return volume if volume >= minimum - 1e-9 else 0.0


def lots_for_notional(notional_usd, contract_size, price, step=0.0,
                      minimum=0.0):
    """Lots whose notional is as close as a tradable step allows.

    notional = lots x contract_size x price, so lots is that inverted,
    snapped to the NEAREST tradable step: the notional is a target the
    operator set, not a ceiling, and flooring it can land a fifth of
    the way below at small sizes.

    Returns None when it cannot be computed — a missing price must not
    silently become a zero-lot or a full-clip order."""
    if not notional_usd or not contract_size or not price:
        return None
    if notional_usd < 0 or contract_size <= 0 or price <= 0:
        return None
    return round_step(notional_usd / (contract_size * price), step, minimum)


def hedge_lots(leg_a_lots, contract_a, contract_b, beta, step=0.0,
               minimum=0.0, mode='units', price_a=None, price_b=None):
    """Leg B lots that hedge leg A.

    Two constructions, and they answer different questions:

    units (default) — L_B*C_B = L_A*C_A / beta. Equal UNITS: the same
        ounces of gold long and short, weighted by beta. The pair's
        P&L is then exactly the spread move, which is what the z-score
        is measured on, so this is the right hedge for a basis trade.
        The two legs' NOTIONALS differ by the basis itself, and that
        difference is the thing being traded, not an imbalance.

    notional — L_B*C_B*P_B = L_A*C_A*P_A. Equal MONEY on both legs
        (owner asked for this, 2026-08-07). The position then trades
        the RETURN spread rather than the price spread:

            P&L = notional * (return_A - return_B)

        which is the correct construction for two related instruments
        with no arbitrage tying them (WTI vs Brent), where equal
        dollars is the neutral position and equal barrels is not.

    The two coincide exactly when beta equals the price ratio P_B/P_A —
    the "live beta" the dashboard shows. Away from that, dollar-neutral
    sizing and a fixed HEDGE_RATIO disagree, and the position will not
    track the spread the signal measures; `plan` reports the gap so the
    UI can say so rather than let it pass silently."""
    beta = float(beta or 1.0)
    if not leg_a_lots or not contract_a or not contract_b:
        return 0.0
    if str(mode).lower() == 'notional':
        if not price_a or not price_b:
            return 0.0
        target = leg_a_lots * contract_a * price_a / (contract_b * price_b)
    else:
        if beta == 0:
            return 0.0
        target = leg_a_lots * contract_a / (beta * contract_b)
    # DOWN, unlike leg A. Leg A's notional is a target and nearest is
    # the honest reading of it; the hedge is a quantity that must not
    # overshoot. With leg B's step ten times leg A's, nearest would
    # turn a wanted 0.05 into 0.1 — a hedge twice the position it is
    # hedging, net short the difference, and it would also slip past
    # the minimum-notional guard that exists to catch exactly this.
    # Rounding down leaves the pair short instead, which the executor
    # already handles by trimming leg A to the matched size.
    return round_step(target, step, minimum, down=True)


def spread_units(leg_b_lots, contract_b):
    """Dollars per 1.00 of spread movement.

    `k` from the derivation: leg B's units. Everything that converts a
    spread distance into money — exit levels, slippage, edge — is
    multiplying by this."""
    if not leg_b_lots or not contract_b:
        return 0.0
    return leg_b_lots * contract_b


def notional(lots, contract_size, price):
    if not lots or not contract_size or not price:
        return 0.0
    return lots * contract_size * price


def margin(notional_usd, leverage):
    """Capital the broker locks for that notional. Leverage is
    broker-side; the config only mirrors it."""
    if not notional_usd:
        return 0.0
    if not leverage or leverage <= 0:
        return notional_usd          # unlevered: the whole amount
    return notional_usd / float(leverage)


def matched_minimum_lots(min_a, min_b, step_a, step_b, beta=1.0):
    """The smallest MATCHED pair both legs can actually trade.

    Each leg's own minimum is right for a single-leg test, but using
    both on a pair is not a hedge: on CFI the spot minimum is 0.01
    (1 oz) and the futures minimum is 0.1 (10 oz), so a "LONG_SPR"
    built that way is 9 oz net short. Size the smaller leg UP until
    both clear their minimum at the hedge ratio.

    Shared by ScenarioRunner.pair_volumes (which trades it) and the
    published sizing plan (which displays it), so the number shown on
    the Full Order Test Suite before a run is the number that run
    sends. Two implementations of this would eventually disagree, and
    the one on screen would be the wrong one.
    """
    ratio = float(beta or 1.0)
    step_a = step_a or 0.01
    step_b = step_b or 0.01
    lots_a = max(min_a or 0.0, (min_b or 0.0) / ratio if ratio else 0.0)
    # Round UP, always: the minimum must never be undercut.
    lots_a = math.ceil(lots_a / step_a - 1e-9) * step_a
    lots_b = math.ceil(lots_a * ratio / step_b - 1e-9) * step_b
    return round(lots_a, 8), round(lots_b, 8)


def minimum_notional(contract_a, contract_b, price_a, price_b, beta,
                     min_a=0.0, min_b=0.0, mode='units'):
    """The smallest per-leg notional this PAIR can actually trade.

    Both legs have a minimum volume and they are usually different —
    live on CFI, spot XAUUSD_ is 0.01 lots and futures GC1226 is 0.1,
    ten times larger. The binding constraint is whichever leg needs
    more money, and it is almost always the futures leg: sizing leg A
    to a notional that leaves the hedge under leg B's minimum produces
    a spot order with no hedge, which the engine then refuses.

    Returning the number lets the UI say "you need at least $43,515"
    instead of "the hedge rounds to zero", which is the same fact
    without the one figure the operator can act on."""
    beta = float(beta or 1.0)
    if not contract_a or not contract_b or not price_a or not price_b:
        return None
    needs = [min_a * contract_a * price_a] if min_a else []
    if min_b:
        # Leg B's minimum, expressed as the leg A notional that would
        # generate it. Dollar-neutral inverts through the prices
        # instead of through beta.
        if str(mode).lower() == 'notional':
            lots_a_needed = min_b * contract_b * price_b / (
                contract_a * price_a)
        else:
            lots_a_needed = min_b * beta * contract_b / contract_a
        needs.append(lots_a_needed * contract_a * price_a)
    return max(needs) if needs else None


def plan(config, contract_a, contract_b, price_a, price_b,
         meta_a=None, meta_b=None, size_multiplier=1.0):
    """Resolve one entry's sizing, whichever mode is configured.

    Returns a dict that is both the execution instruction (`leg_a_lots`,
    `leg_b_lots`) and the display block (notionals, margin, imbalance).
    `reason` is set when the sizing could not be resolved, so the caller
    refuses the entry instead of guessing at a size."""
    trading = config.TRADING
    exits = getattr(config, 'EXITS', {}) or {}
    beta = float(trading.get('HEDGE_RATIO', 1.0) or 1.0)
    mode = str(trading.get('SIZING_MODE', 'lots') or 'lots').lower()
    meta_a, meta_b = meta_a or {}, meta_b or {}
    step_a = meta_a.get('volume_step') or 0.0
    step_b = meta_b.get('volume_step') or 0.0
    min_a = meta_a.get('volume_min') or 0.0
    min_b = meta_b.get('volume_min') or 0.0
    pair_min_a, pair_min_b = matched_minimum_lots(
        min_a, min_b, step_a, step_b, beta)

    target_notional = float(trading.get('NOTIONAL_PER_LEG_USD', 0.0) or 0.0)
    reason = None

    if mode == 'notional':
        lots_a = lots_for_notional(target_notional, contract_a, price_a,
                                   step_a, min_a)
        if lots_a is None:
            reason = ('notional sizing needs NOTIONAL_PER_LEG_USD, the '
                      'contract size and a live price')
            lots_a = 0.0
        elif lots_a <= 0:
            reason = (f'${target_notional:,.0f} per leg is below one '
                      f'tradable lot of leg A '
                      f'(${contract_a * price_a:,.0f} minimum)')
    else:
        lots_a = float(trading.get('CLIP_LOTS', 1.0) or 0.0)

    hedge_mode = str(trading.get('HEDGE_MODE', 'units') or 'units').lower()
    lots_a = round_step(lots_a * float(size_multiplier or 1.0), step_a, min_a)
    lots_b = hedge_lots(lots_a, contract_a, contract_b, beta, step_b, min_b,
                        mode=hedge_mode, price_a=price_a, price_b=price_b)
    floor = minimum_notional(contract_a, contract_b, price_a, price_b,
                             beta, min_a, min_b, mode=hedge_mode)
    if lots_a > 0 and lots_b <= 0 and not reason:
        reason = (f"the hedge for {lots_a:g} lots on leg A is under leg B's "
                  f"{min_b:g}-lot minimum"
                  + (f' — this pair needs at least ${floor:,.0f} per leg'
                     if floor else ''))
    elif lots_a <= 0 and mode == 'notional' and floor and target_notional:
        reason = (f'${target_notional:,.0f} per leg is below this pair\'s '
                  f'minimum of ${floor:,.0f}')

    # The broker's CEILING, on both legs. Minimums were checked from the
    # start and maximums never were, and only leg A was ever measured
    # against MAX_LOT_SIZE — so an oversized HEDGE leg had nothing
    # standing in front of it at all.
    #
    # Live 2026-08-10: an inverted HEDGE_RATIO (0.0149 where 67 was
    # meant) sized leg B at 5,167.78 lots of gold, $2.25 BILLION, and
    # the plan reported it as fine. MT5 would have rejected it with
    # 10014 — AFTER leg A had already filled, leaving a naked position
    # exactly as the pair executor exists to prevent.
    # "Leg Notional Value" says PER LEG. Unit-neutral hedging leaves the
    # two legs' notionals differing by the basis, which is small and is
    # the trade — but a WRONG beta turns that into orders of magnitude
    # while the card still reads "Asked for $500,000 per leg". Live
    # 2026-08-10: $500,000 per leg gave $498,726 on XAGUSD and
    # $2,238,784,332 on XAUUSD, and nothing objected.
    #
    # The hedge construction is the owner's choice and is NOT overridden
    # here (unit-neutral is right for a basis pair: the pair's P&L is
    # then the spread move the z-score is measured on). What is refused
    # is a leg B that bears no relation to the money asked for, because
    # that is always a beta error, never an intended hedge.
    if not reason and mode == 'notional' and target_notional:
        notional_b_now = notional(lots_b, contract_b, price_b)
        if notional_b_now and abs(notional_b_now - target_notional) \
                > 0.5 * target_notional:
            ratio = notional_b_now / target_notional
            live_beta = (price_b / price_a) if price_a else 0.0
            reason = (
                f'you asked for ${target_notional:,.0f} per leg and leg A '
                f'is right, but the hedge comes to '
                f'${notional_b_now:,.0f} — {ratio:,.4g}x the target. '
                f'Leg B is leg A x contract A / (HEDGE_RATIO x contract '
                f'B), so a HEDGE_RATIO that is too small inflates it, '
                f'and {beta:g} is far below the {live_beta:,.4f} price '
                f'ratio. Either set HEDGE_RATIO to about {live_beta:,.2f}, '
                f'or set Hedge Balance to dollar-neutral, which sizes '
                f'leg B from the notional directly and ignores beta')

    max_a = meta_a.get('volume_max') or 0.0
    max_b = meta_b.get('volume_max') or 0.0
    if not reason and max_a and lots_a > max_a + 1e-9:
        reason = (f'leg A wants {lots_a:g} lots but the broker\'s maximum '
                  f'is {max_a:g} — the order would be rejected')
    if not reason and max_b and lots_b > max_b + 1e-9:
        reason = (f'the hedge wants {lots_b:g} lots on leg B but the '
                  f'broker\'s maximum is {max_b:g}. Check HEDGE_RATIO '
                  f'{beta:g}: leg B is sized as leg A x contract A / '
                  f'(beta x contract B), so a beta that is too SMALL '
                  f'inflates the hedge.')

    notional_a = notional(lots_a, contract_a, price_a)
    notional_b = notional(lots_b, contract_b, price_b)
    lev_a = exits.get('SPOT_LEVERAGE') or exits.get('LEVERAGE')
    lev_b = exits.get('FUT_LEVERAGE') or exits.get('LEVERAGE')
    margin_a, margin_b = margin(notional_a, lev_a), margin(notional_b, lev_b)
    bigger = max(notional_a, notional_b)

    # What the rounding actually cost against the target. A tradable
    # step is a fixed number of dollars, so the smaller the position
    # the bigger a slice of it one step is — at $20k of gold a step is
    # 21% of the whole thing. Stated, never implied.
    step_usd = step_a * contract_a * price_a if (step_a and price_a) else None
    shortfall_pct = None
    if mode == 'notional' and target_notional and notional_a:
        shortfall_pct = 100.0 * (notional_a - target_notional) / target_notional

    # Dollar-neutral sizing and a fixed HEDGE_RATIO agree only when
    # beta equals the live price ratio. Away from that the position
    # does not track the spread the z-score is measured on, so the
    # disagreement is reported rather than left to be discovered.
    dollar_neutral_beta = (price_b / price_a) if (price_a and price_b) else None
    beta_gap_pct = (100.0 * (beta - dollar_neutral_beta) / dollar_neutral_beta
                    if dollar_neutral_beta else None)

    return {
        'mode': mode,
        'hedge_mode': hedge_mode,
        'dollar_neutral_beta': dollar_neutral_beta,
        'beta_gap_pct': beta_gap_pct,
        'target_notional_usd': target_notional if mode == 'notional' else None,
        'hedge_ratio': beta,
        'leg_a_lots': lots_a, 'leg_b_lots': lots_b,
        'leg_a_contract': contract_a, 'leg_b_contract': contract_b,
        'leg_a_notional_usd': notional_a, 'leg_b_notional_usd': notional_b,
        'leg_a_units': lots_a * (contract_a or 0),
        'leg_b_units': lots_b * (contract_b or 0),
        'leg_a_margin_usd': margin_a, 'leg_b_margin_usd': margin_b,
        'leg_a_leverage': lev_a, 'leg_b_leverage': lev_b,
        'margin_usd': margin_a + margin_b,
        'spread_units': spread_units(lots_b, contract_b),
        # How far off equal-money the two legs ended up. Rounding to a
        # tradable lot makes exact balance impossible, so the honest
        # thing is to state the residual rather than imply there is
        # none. This is the number the owner means by "balanced".
        'imbalance_usd': notional_a - notional_b,
        'imbalance_pct': (100.0 * (notional_a - notional_b) / bigger
                          if bigger else 0.0),
        # The smallest per-leg notional this pair can trade at all,
        # so the operator has a target rather than a rejection.
        'min_notional_usd': floor,
        'lot_step_usd': step_usd,
        'notional_gap_pct': shortfall_pct,
        # Each leg's own broker minimum, and what one of them is worth.
        # The Full Order Test Suite trades at exactly these sizes and
        # places REAL orders, but nothing published them, so the only
        # way to find out what a run would cost was to run it. They are
        # NOT interchangeable between legs — CFI's futures minimum is
        # ten times its spot minimum.
        'leg_a_min_lots': min_a,
        'leg_b_min_lots': min_b,
        'leg_a_step': step_a,
        'leg_b_step': step_b,
        'leg_a_min_notional_usd': (min_a * contract_a * price_a
                                   if price_a else None),
        'leg_b_min_notional_usd': (min_b * contract_b * price_b
                                   if price_b else None),
        # What a SPREAD scenario trades: the smaller leg sized up so
        # both clear their minimum. Same function the runner calls.
        'pair_min_lots_a': pair_min_a,
        'pair_min_lots_b': pair_min_b,
        'pair_min_notional_a_usd': (pair_min_a * contract_a * price_a
                                    if price_a else None),
        'pair_min_notional_b_usd': (pair_min_b * contract_b * price_b
                                    if price_b else None),
        'reason': reason,
    }
