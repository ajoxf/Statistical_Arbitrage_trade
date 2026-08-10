"""Which HEDGE_RATIO belongs to the pair that is actually configured.

Beta is a property of the PAIR, not of the installation, and nothing
carried it across a pair change. The symbols are edited on the
Exchanges page, the launcher is restarted, and the beta from the last
instrument is still sitting in config.json defining the spread.

Three live incidents in one day (2026-08-10), all the same shape:

    beta 10       USOIL/UKOIL at 81.76 / 85.07  ->  spread  -732.53
    beta 0.0149   XAGUSD/XAUUSD                 ->  a 5,167-lot hedge
    beta 66.94    left on USOIL/UKOIL           ->  spread -5469.59

Every one of them made mu, sigma, z and every exit level describe a
series that does not exist, and the third arrived purely by changing
the instruments. Operator, 2026-08-10: "Can you make sure the Hedge
Ratio is calculated and changed everytime the pair is changed?"

So the value is STAMPED with the pair it was computed for, and the
engine re-derives it at startup whenever the running pair no longer
matches the stamp. What it does NOT do is second-guess a beta that
belongs to the current pair — an operator who tunes beta on their own
pair keeps their number.
"""

BASIS_TYPES = ('SPOT_FUTURE', 'FUTURE_FUTURE')

# How far the spread may travel from zero before it stops being a
# DIFFERENCE between two comparable prices and starts being one price
# scaled by a wrong coefficient. Half the smaller leg is generous —
# a real basis is a percent or two — and it is the threshold the health
# block blocks entries on.
MAX_SPREAD_FRACTION = 0.5


def pair_signature(spot_symbol, futures_symbol):
    """What a beta was computed FOR. Stamped beside the value so a pair
    change is detectable; without it, a stale beta and a deliberately
    tuned one are indistinguishable."""
    return f'{spot_symbol or ""}|{futures_symbol or ""}'


def suggest(pair_type, price_a, price_b):
    """(beta, why) for this pair, or (None, why not).

    The right answer depends on what the two legs ARE, and getting it
    backwards is harmful in both directions:

    Same underlying (spot vs its own future, or two contract months) —
        beta is 1 and the spread IS the basis. Using the price ratio
        here collapses the spread to roughly zero and deletes the very
        thing the strategy trades: gold spot against its future has a
        ratio of ~1.014, and a beta of 1.014 turns a $59 basis into
        pennies of rounding.

    Two different instruments (WTI vs Brent, silver vs gold) — no
        arbitrage ties them, so the price ratio is what makes a spread
        between them mean anything. Beta 1 on XAGUSD (38) against
        XAUUSD (4,000) is not a spread at all, it is gold's own price
        with a rounding error subtracted.
    """
    pair_type = (pair_type or 'SPOT_FUTURE').upper()
    if pair_type in BASIS_TYPES:
        return 1.0, (f'{pair_type}: the two legs are the same '
                     f'underlying, so the spread IS the basis and beta '
                     f'is 1')
    if not price_a or not price_b or price_a <= 0 or price_b <= 0:
        return None, 'both legs need a live price before beta can be derived'
    return round(price_b / price_a, 6), (
        f'{pair_type}: {price_b:,.4f} / {price_a:,.4f} — two different '
        f'instruments have no basis tying them, so beta is the price '
        f'ratio, which is what makes a spread between them meaningful')


def spread_for(beta, price_a, price_b):
    """The spread this beta produces: futures - beta * spot."""
    return (price_b or 0.0) - float(beta or 1.0) * (price_a or 0.0)


def implausible(beta, price_a, price_b, spread=None):
    """Is the resulting spread not a difference between these prices?

    Returns the offending spread, or None when the beta is usable. One
    threshold, shared by the startup adoption and the health block that
    blocks entries, so the engine cannot adopt a beta it will then
    refuse to trade on.
    """
    smaller = min(abs(price_a or 0.0), abs(price_b or 0.0))
    if not smaller:
        return None
    if spread is None:
        spread = spread_for(beta, price_a, price_b)
    return spread if abs(spread) > MAX_SPREAD_FRACTION * smaller else None


def resolve(configured_beta, stamped_for, pair_type, spot_symbol,
            futures_symbol, price_a, price_b):
    """Decide what beta this pair should run with.

    Returns (beta, reason) when it should CHANGE, or (None, reason)
    when the configured value stands. The three cases:

    Stamped for THIS pair — the value belongs here. Left alone, even if
        it is unusual: beta is a strategy parameter and an operator who
        tuned it on their own pair keeps their number.

    Stamped for a DIFFERENT pair — the pair was changed and the beta
        was not. Re-derived. This is the case the operator asked for.

    Not stamped at all — an install predating the stamp, so which pair
        the number was meant for is unknowable. Adopting blindly would
        overwrite a deliberate choice, so the value is kept and merely
        stamped UNLESS it is implausible against the live prices, which
        settles the question on its own.
    """
    signature = pair_signature(spot_symbol, futures_symbol)
    suggested, why = suggest(pair_type, price_a, price_b)

    if stamped_for == signature:
        return None, f'HEDGE_RATIO {configured_beta:g} was set for this pair'

    if suggested is None:
        return None, why                    # cannot derive; leave it be

    if not stamped_for:
        broken = implausible(configured_beta, price_a, price_b)
        if broken is None:
            return None, (f'HEDGE_RATIO {configured_beta:g} is plausible for '
                          f'{spot_symbol}/{futures_symbol} — kept, and now '
                          f'stamped with the pair it belongs to')
        return suggested, (
            f'HEDGE_RATIO {configured_beta:g} gives a spread of '
            f'{broken:,.2f} on legs priced {price_a:,.4f} / {price_b:,.4f}, '
            f'which is not a difference between them. {why}')

    return suggested, (
        f'the pair changed to {spot_symbol}/{futures_symbol} but '
        f'HEDGE_RATIO {configured_beta:g} was set for '
        f'{stamped_for.replace("|", "/")}. {why}')
