"""What it costs to hold a basis pair to expiry — the convergence trade.

Operator, 2026-08-19: "swap can be entered manually and the number of
days can be calculated to identify if the Spread is higher or not. We
can place a manual trade according to it."

This answers a different question from the z-score. z asks "is this
spread unusual against its own recent history". This asks "at expiry
the future must converge to spot, so is today's spread wider than what
it costs to hold the pair until then". A basis pair has a date on
which the trade is decided, and that makes the arithmetic a
subtraction rather than a statistic:

    net = |spread| x k  -  carry to expiry  -  round-trip cost

`k` is leg B's units, the same multiplier that turns any spread move
into dollars (sizing.spread_units). Positive net is an edge that does
not depend on mean reversion happening — only on the contract expiring,
which it will.

## Why this is NOT the fair value module

`fairvalue.py` computes a theoretical basis from an annual risk-free
rate and is barred from every trading path. This one is priced from
the broker's OWN financing — the swap it will actually charge — and
exists to inform a MANUAL trade. It still does not feed the signal
generator: the operator reads it and decides.

## Swap units are the whole difficulty

MT5 reports `swap_long` / `swap_short` in whatever `swap_mode` says,
and the same "-4.5" is 4.5 points on one symbol, 4.5 units of account
currency on another and 4.5 percent a year on a third. The old
carry-detrended spread read it as money regardless, which is how it
produced a basis nobody could reconcile against the two prices beside
it.

So each mode is converted explicitly, and a mode this module cannot
convert returns None with the reason rather than a number. An
unconvertible swap is not a zero swap.
"""

from datetime import datetime

# MT5 SYMBOL_SWAP_MODE_* values. Named here rather than imported
# because this module must work off-Windows, where the MetaTrader5
# package does not exist.
SWAP_DISABLED = 0
SWAP_POINTS = 1
SWAP_CURRENCY_SYMBOL = 2
SWAP_CURRENCY_MARGIN = 3
SWAP_CURRENCY_DEPOSIT = 4
SWAP_INTEREST_CURRENT = 5
SWAP_INTEREST_OPEN = 6
SWAP_REOPEN_CURRENT = 7
SWAP_REOPEN_BID = 8

#: Modes whose value is already money per lot per night.
MONEY_MODES = (SWAP_CURRENCY_SYMBOL, SWAP_CURRENCY_MARGIN,
               SWAP_CURRENCY_DEPOSIT)

MODE_NAMES = {
    SWAP_DISABLED: 'no swap charged',
    SWAP_POINTS: 'points per night',
    SWAP_CURRENCY_SYMBOL: 'symbol currency per lot per night',
    SWAP_CURRENCY_MARGIN: 'margin currency per lot per night',
    SWAP_CURRENCY_DEPOSIT: 'deposit currency per lot per night',
    SWAP_INTEREST_CURRENT: 'annual percent (current price)',
    SWAP_INTEREST_OPEN: 'annual percent (open price)',
    SWAP_REOPEN_CURRENT: 'position reopened at close price',
    SWAP_REOPEN_BID: 'position reopened at bid',
}


def days_to_expiry(expiry, now=None):
    """Whole days until the contract expires, or None.

    None for a missing or passed expiry — a rolling contract never
    converges, so there is no date for this trade to be decided on.
    """
    if not expiry:
        return None
    now = now or datetime.now()
    days = (expiry - now).total_seconds() / 86400.0
    return days if days > 0 else None


def swap_per_lot_night(swap, mode, contract_size=None, price=None,
                       tick_size=None, tick_value=None):
    """(money per lot per night, note) for one leg, or (None, why not).

    Sign is preserved: a positive swap is credited and a negative one
    charged, and a pair is long one leg and short the other, so the two
    frequently pull in opposite directions. Collapsing them to costs
    would hide the case worth finding — a positive net carry, where the
    market pays you to hold the convergence.
    """
    if swap is None:
        return None, 'the broker did not report a swap for this symbol'
    if mode is None:
        return None, ('the broker did not report swap_mode, so the '
                      'number cannot be read as money, points or percent')
    swap = float(swap)
    if mode == SWAP_DISABLED or swap == 0:
        return 0.0, 'no swap charged on this symbol'
    if mode in MONEY_MODES:
        return swap, f'{swap:+.2f} per lot per night, as quoted'
    if mode == SWAP_POINTS:
        # A point is worth tick_value per tick_size of price. Falling
        # back to contract size assumes a point IS a unit of price,
        # which is true on most CFDs and not on all of them — so the
        # note says which route was taken.
        if tick_size and tick_value:
            per_point = tick_value / tick_size
            return swap * per_point, (
                f'{swap:+.1f} points x {per_point:,.2f} per point '
                f'(tick value {tick_value} / tick size {tick_size})')
        if contract_size:
            return swap * float(contract_size), (
                f'{swap:+.1f} points x {contract_size:g} per lot — no tick '
                f'value reported, so a point is taken as one unit of price')
        return None, 'swap is in points but nothing prices a point'
    if mode in (SWAP_INTEREST_CURRENT, SWAP_INTEREST_OPEN):
        if not (contract_size and price):
            return None, ('swap is an annual percent and needs the '
                          'contract size and a live price to convert')
        notional = float(contract_size) * float(price)
        return notional * swap / 100.0 / 360.0, (
            f'{swap:+.3f}% a year on {notional:,.0f} of notional, '
            f'over a 360-day year')
    return None, (f'swap mode {mode} '
                  f'({MODE_NAMES.get(mode, "unrecognised")}) is not one '
                  f'this can convert')


#: How far the swap-implied basis may sit from the rate-implied one
#: before the swap input is the likeliest explanation.
SANITY_MULT = 3.0


def sanity(carry_spread, fair_value):
    """Do the broker's swap and the carry rate tell the same story?

    Two independent estimates of ONE physical quantity end up on the
    dashboard together: `fairvalue` prices the basis from an annual
    rate, and this module prices it from the swap the broker actually
    charges. They are computed from different inputs by different code,
    so when they disagree one of the two inputs is wrong — and that is
    the cheapest check available anywhere on this screen.

    Live 2026-08-24 it would have caught the whole thing at a glance.
    The operator entered +58.00 per lot per night on the spot leg. The
    MAGNITUDE was right — 58 x 365 / (100 oz x 4,646) is 4.56% a year,
    which is exactly what gold funds at — but the SIGN was inverted:
    a long spot position is CHARGED that, not paid it. The card then
    reported a carry-implied basis of -51.82 next to a fair value of
    +48.55 and concluded "you are paid to hold this to expiry at any
    spread", which is a licence to print money and should never be
    displayed without challenge. Flip the sign and the two agree to
    within 7%.

    Returns a message, or None when they are consistent.
    """
    if carry_spread is None or fair_value is None:
        return None
    if abs(fair_value) < 1e-9:
        return None
    if carry_spread * fair_value < 0:
        return (f'Swap says the basis is {carry_spread:+,.2f}, the carry '
                f'rate says {fair_value:+,.2f} — opposite signs. A leg you '
                f'are LONG is normally charged, so its swap is negative.')
    ratio = abs(carry_spread) / abs(fair_value)
    if ratio > SANITY_MULT or ratio < 1.0 / SANITY_MULT:
        return (f'Swap says the basis is {carry_spread:+,.2f}, the carry '
                f'rate says {fair_value:+,.2f} — {max(ratio, 1 / ratio):,.1f}x '
                f'apart. They price the same thing, so check the units.')
    return None


def credited_long_leg(per_leg):
    """Flag a leg you are LONG that is showing a CREDIT, or None.

    `sanity` above needs a fair value to compare against, and a RELATED
    pair has none — so on those pairs an inverted swap sign sailed
    straight through (operator, 2026-08-24, reading a card whose SWAP
    row was +$103.43: "One of them should be Negative").

    This check needs no second estimate. Holding a long position is
    financed, so the broker charges you: a long leg's swap is negative
    on essentially every instrument this engine trades. A CREDIT there
    is the signature of a magnitude typed without its minus sign, which
    is exactly the mistake that produced a card reading "you are paid to
    hold this to expiry at any spread".

    It is possible to be paid on a long leg — a deeply negative-rate
    funding currency — so this reports rather than refuses. But it must
    say so, because the alternative is a fabricated edge that looks like
    the best trade on the screen.
    """
    for leg in per_leg or ():
        rate = leg.get('per_lot_night')
        if leg.get('side') == 'L' and rate is not None and rate > 0:
            return (f"{leg.get('symbol') or 'Leg A'} is the leg you would "
                    f"be LONG and its swap is a CREDIT ({rate:+.2f} a "
                    f"night). A long leg is normally charged — check the "
                    f"sign.")
    return None


def convergence_plan(spread, days, spread_units, legs, cost_usd=0.0,
                     nights_per_week=7.0, fair_value=None):
    """Is the spread wider than the carry to expiry?

    `legs` is an iterable of (money_per_lot_night, lots, note) — one
    entry per leg, already signed for the direction that leg will be
    traded in. Returns a dict the UI can render whole, with `net_usd`
    None whenever any leg's swap could not be converted: a carry
    estimate missing one of its two legs is not a smaller estimate, it
    is not an estimate.
    """
    # `spread` and `units` are echoed back so the UI can show the
    # multiplication rather than only its answer. Every other money
    # figure on this dashboard is checkable against the two numbers
    # beside it, and a bare "$794.20" is the kind of total that gets
    # believed for months.
    out = {'days': days, 'spread': spread, 'spread_units': spread_units,
           'gross_usd': None, 'carry_usd': None,
           'cost_usd': cost_usd, 'net_usd': None, 'per_leg': [],
           'carry_spread': None, 'breakeven_spread': None,
           'spread_gap': None, 'schedule': [], 'reason': None,
           'warning': None}
    if spread is None or not spread_units:
        out['reason'] = 'no live spread or no sizing yet'
        return out
    if days is None:
        out['reason'] = ('no expiry on the futures leg — a rolling '
                         'contract never converges, so there is no date '
                         'for this trade to be decided on')
        return out

    # At expiry the future is the spot, so the whole spread is what
    # convergence pays. Its SIGN is the direction, not the size.
    out['gross_usd'] = abs(spread) * spread_units

    # Swap is charged per night held, and the weekend is charged too
    # (usually as a triple on one weekday). Counting calendar days is
    # therefore right, not conservative.
    nights = days * (nights_per_week / 7.0)
    total, unknown = 0.0, []
    for per_night, lots, note in legs:
        entry = {'per_lot_night': per_night, 'lots': lots, 'note': note}
        if per_night is None:
            entry['carry_usd'] = None
            unknown.append(note)
        else:
            entry['carry_usd'] = per_night * (lots or 0.0) * nights
            total += entry['carry_usd']
        out['per_leg'].append(entry)
    if unknown:
        out['reason'] = '; '.join(unknown)
        return out

    out['carry_usd'] = total
    # carry is SIGNED: positive means the pair is paid to wait.
    out['net_usd'] = out['gross_usd'] + total - (cost_usd or 0.0)

    # The same arithmetic turned back into SPREAD, which is the unit the
    # operator is actually looking at on the screen. Answering only in
    # dollars means a figure that moves with lots and leverage; the
    # spread the pair has to beat does not.
    #
    #   carry_spread     what the basis SHOULD be on financing alone —
    #                    the theoretical spread for these remaining days
    #   breakeven_spread the same plus the round trip: what it has to be
    #                    for the trade to be worth placing
    #
    # net = 0  =>  |spread| x k + carry - cost = 0  =>
    #              |spread| = (cost - carry) / k
    out['carry_spread'] = -total / spread_units
    out['breakeven_spread'] = ((cost_usd or 0.0) - total) / spread_units
    out['spread_gap'] = abs(spread) - out['breakeven_spread']
    # Does the broker's swap agree with the carry rate about which way
    # this basis points? They price the same thing from different
    # inputs, so a disagreement means one input is wrong.
    out['warning'] = sanity(out['carry_spread'], fair_value)

    # How that threshold decays as expiry approaches. Carry shrinks with
    # the days left; the ROUND TRIP DOES NOT, so the curve flattens onto
    # cost/k rather than onto zero. Stating that is the point of the
    # table: waiting reduces the financing you pay and never the
    # commission, so there is a floor under how good this can get.
    per_day = total / days if days else 0.0
    # Today first, then round-number milestones, and ALWAYS expiry last —
    # the 0-day row is the one that shows the floor, so it survives the
    # cap rather than being the first thing trimmed off the end.
    milestones = [d for d in (180, 90, 60, 30, 14, 7, 3, 1) if d < days]
    for d in [days] + milestones[-5:] + [0.0]:
        carry_d = per_day * d
        out['schedule'].append({
            'days': d,
            'carry_usd': carry_d,
            'breakeven_spread': ((cost_usd or 0.0) - carry_d) / spread_units,
        })
    return out
