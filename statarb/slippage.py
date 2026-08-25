"""What the signal wanted versus what MT5 actually gave us.

Owner (2026-08-07): "what your signal wanted to enter at and what the
orders got placed at on MT5".

The honest answer needs THREE prices per leg, not two, because the
signal and the fill are not quoted on the same thing:

    mid    the price the strategy sees. `compute_market_data` builds
           the spread from mids, so this is the number the z-score,
           the edge filter and the exit ladder were all evaluated
           against.
    quote  the executable touch at that same instant — the ask if we
           have to buy, the bid if we have to sell. This is what the
           decision was actually worth.
    fill   what came back from the broker.

Splitting them separates two costs that behave completely differently:

    crossing  = mid -> quote.  Known BEFORE the trade, quoted on the
                screen, and already modelled in COSTS. Nothing has
                gone wrong when you pay it.
    slippage  = quote -> fill. The surprise: the market moved between
                the decision and the fill, or the broker filled away
                from its own quote. This is the number that tells you
                whether the cost model still holds.

Reporting one number for both would have made a wide spread look like
bad execution and a fast market look like a wide spread, and the fix
for those is not the same fix.

Sign convention, everywhere in this module: POSITIVE IS A COST. A
negative slippage is price improvement, and it does happen on limit
fills, so the sign is kept rather than taken in absolute value.
"""

from .models import OrderSide, SignalType


def spread_units(config, asset_key, spot_trade, futures_trade):
    """Dollars per 1.00 of spread, for pricing this pair's report.

    LEG B's quantity, from the lots that actually FILLED. The spread is
    `futures - beta * spot`, so the pair's dollar cost is
    `fut_slip x units_b + spot_slip x units_a`; the hedge is sized so
    `units_a = beta x units_b`, which collapses that to
    `spread_slip x units_b` exactly. Leg A's units were used here, and
    they agree only at beta 1 with equal contract sizes — the one
    configuration this has ever run in. Away from there every dollar
    figure on the entry-cost row was out by 1/beta.

    Falls back to leg A when the asset declares no separate futures
    contract size, which is the common case and the old behaviour.
    """
    if config is None:
        return 0.0
    asset = (getattr(config, 'ASSETS', None) or {}).get(asset_key) or {}
    contract_a = float(asset.get('lot_size', 0.0) or 0.0)
    contract_b = float(asset.get('fut_lot_size') or contract_a or 0.0)
    lots_b = getattr(futures_trade, 'lot_size', None)
    if not lots_b or not contract_b:
        return (getattr(spot_trade, 'lot_size', 0.0) or 0.0) * contract_a
    return lots_b * contract_b


def touch(bid, ask, side):
    """The price we must actually pay/receive to trade NOW."""
    if bid is None or ask is None:
        return None
    return ask if side is OrderSide.BUY else bid


def mid(bid, ask):
    if bid is None or ask is None:
        return None
    return (bid + ask) / 2.0


def leg_report(side, bid, ask, fill, symbol=None):
    """One leg's decision-to-fill account.

    `bid`/`ask` are the quote at DECISION time — not at order time and
    not at fill time. That is the whole point: the operator is asking
    what the signal thought it was getting."""
    quote = touch(bid, ask, side)
    reference = mid(bid, ask)
    buying = side is OrderSide.BUY
    report = {
        'symbol': symbol,
        'side': side.value if hasattr(side, 'value') else side,
        'mid': reference,
        'quote': quote,
        'fill': fill,
        'crossing': None,
        'slippage': None,
        'total': None,
    }
    if quote is not None and reference is not None:
        # Crossing the book is always a cost, whichever way we trade.
        report['crossing'] = (quote - reference) if buying \
            else (reference - quote)
    if quote is not None and fill is not None:
        report['slippage'] = (fill - quote) if buying else (quote - fill)
    if reference is not None and fill is not None:
        report['total'] = (fill - reference) if buying else (reference - fill)
    return report


def selling_the_spread(signal_type, closing):
    """Is this order SELLING the spread (wants a high level) or BUYING
    it (wants a low one)?

    A short-spread position sells the spread to get in and buys it back
    to get out, so the same signal type flips sign between entry and
    exit. Getting this backwards would report every exit's cost as a
    gain."""
    short = signal_type == SignalType.SELL_BASIS
    return short != bool(closing)


def _signed(selling, quote_level, exec_level):
    """Positive = the level we got was worse than the level quoted."""
    if quote_level is None or exec_level is None:
        return None
    return (quote_level - exec_level) if selling else (exec_level - quote_level)


def pair_report(signal_type, closing, beta, oz, spot, futures):
    """Combine two `leg_report`s into the spread-level account.

    `spot` and `futures` are leg reports; `beta` is HEDGE_RATIO and
    `oz` the spot ounces the pair carries, so the spread numbers can be
    stated in dollars.

    The spread is `futures - beta * spot` (the owner's definition), so
    each leg's cost enters the spread cost with the same weight it has
    in the spread itself — which makes the leg numbers add up to the
    spread number exactly. tests/test_slippage.py asserts that; if it
    ever stops holding, one of the two is being measured wrong."""
    selling = selling_the_spread(signal_type, closing)

    def level(key_spot, key_fut):
        if spot.get(key_spot) is None or futures.get(key_fut) is None:
            return None
        return futures[key_fut] - beta * spot[key_spot]

    mid_spread = level('mid', 'mid')
    quote_spread = level('quote', 'quote')
    exec_spread = level('fill', 'fill')

    crossing = _signed(selling, mid_spread, quote_spread)
    slip = _signed(selling, quote_spread, exec_spread)
    total = _signed(selling, mid_spread, exec_spread)

    def usd(value):
        return None if value is None or not oz else value * oz

    return {
        'selling_spread': selling,
        'closing': bool(closing),
        'hedge_ratio': beta,
        'oz': oz,
        'decision_spread': mid_spread,     # what the signal saw
        'quoted_spread': quote_spread,     # what it was executable at
        'executed_spread': exec_spread,    # what MT5 gave us
        'crossing_spread': crossing,
        'slippage_spread': slip,
        'total_spread': total,
        'crossing_usd': usd(crossing),
        'slippage_usd': usd(slip),
        'total_usd': usd(total),
        'legs': {'spot': spot, 'futures': futures},
    }


def build(signal_type, closing, beta, oz, spot_side, futures_side,
          reference, spot_fill, futures_fill,
          spot_symbol=None, futures_symbol=None):
    """The whole report from a decision-time market_data snapshot.

    Returns None when the snapshot is missing — an unmeasurable trade
    reports nothing rather than a zero, because a zero here would read
    as perfect execution."""
    if not reference:
        return None
    spot = leg_report(spot_side, reference.get('spot_bid'),
                      reference.get('spot_ask'), spot_fill, spot_symbol)
    futures = leg_report(futures_side, reference.get('futures_bid'),
                         reference.get('futures_ask'), futures_fill,
                         futures_symbol)
    return pair_report(signal_type, closing, beta, oz, spot, futures)


def summarise(report, digits=4):
    """One log line. Spread units first (the strategy's own unit),
    dollars second."""
    if not report:
        return 'slippage: not measured (no decision snapshot)'
    def fmt(value, unit=''):
        return 'n/a' if value is None else f'{value:+.{digits}f}{unit}'
    money = report.get('slippage_usd')
    return (
        f"decision {report['decision_spread']:.{digits}f} -> quoted "
        f"{report['quoted_spread']:.{digits}f} -> filled "
        f"{report['executed_spread']:.{digits}f} | "
        f"crossing {fmt(report['crossing_spread'])} + slippage "
        f"{fmt(report['slippage_spread'])} = {fmt(report['total_spread'])}"
        + (f" ({money:+,.2f} USD slipped)" if money is not None else '')
    ) if report.get('executed_spread') is not None else \
        'slippage: not measured (a leg did not fill)'
