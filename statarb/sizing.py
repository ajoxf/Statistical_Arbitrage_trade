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


def round_step(volume, step, minimum=0.0):
    """Round DOWN to a tradable volume. Down, because rounding up can
    breach a max or a margin the caller already checked."""
    if step and step > 0:
        volume = math.floor(volume / step + 1e-9) * step
        volume = round(volume, 8)
    return volume if volume >= minimum - 1e-9 else 0.0


def lots_for_notional(notional_usd, contract_size, price, step=0.0,
                      minimum=0.0):
    """Lots whose notional is as close to (but not over) the target.

    notional = lots x contract_size x price, so lots is that inverted.
    Returns None when it cannot be computed — a missing price must not
    silently become a zero-lot or a full-clip order."""
    if not notional_usd or not contract_size or not price:
        return None
    if notional_usd < 0 or contract_size <= 0 or price <= 0:
        return None
    return round_step(notional_usd / (contract_size * price), step, minimum)


def hedge_lots(leg_a_lots, contract_a, contract_b, beta, step=0.0,
               minimum=0.0):
    """Leg B lots that actually hedge leg A: L_B = L_A*C_A / (beta*C_B).

    See the module docstring for the derivation. This is the formula
    that makes the pair's P&L equal to the spread move, which is the
    whole premise of the strategy."""
    beta = float(beta or 1.0)
    if not leg_a_lots or not contract_a or not contract_b or beta == 0:
        return 0.0
    return round_step(leg_a_lots * contract_a / (beta * contract_b),
                      step, minimum)


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

    lots_a = round_step(lots_a * float(size_multiplier or 1.0), step_a, min_a)
    lots_b = hedge_lots(lots_a, contract_a, contract_b, beta, step_b, min_b)
    if lots_a > 0 and lots_b <= 0 and not reason:
        reason = ('the hedge for that size rounds to zero on leg B '
                  f'(minimum {min_b:g} lots)')

    notional_a = notional(lots_a, contract_a, price_a)
    notional_b = notional(lots_b, contract_b, price_b)
    lev_a = exits.get('SPOT_LEVERAGE') or exits.get('LEVERAGE')
    lev_b = exits.get('FUT_LEVERAGE') or exits.get('LEVERAGE')
    margin_a, margin_b = margin(notional_a, lev_a), margin(notional_b, lev_b)
    bigger = max(notional_a, notional_b)

    return {
        'mode': mode,
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
        'reason': reason,
    }
