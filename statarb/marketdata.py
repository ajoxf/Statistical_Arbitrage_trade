"""The spread the strategy trades, built from two ticks.

    spread = futures - hedge_ratio * spot

Owner's definition (2026-08-06). Leg B minus the hedge ratio times Leg
A, and nothing else: no carry term, no swap cost, no dependence on the
futures expiry. The hedge ratio is the same number that sizes the
hedge, so the spread is exactly the P&L of the pair per unit — which is
why HEDGE_RATIO is structural and cannot change under an open position.

This replaced a carry-detrended spread (basis minus a swap-implied
basis). The theory there was that the raw basis drifts toward zero as
the contract approaches expiry, biasing a rolling mean. It does, but
the drift is spread over months while the window is hours: on gold at a
59-point basis four months out, that is ~0.03 of drift across a
two-hour window — far below the noise the z-score is measuring. What
the carry term did do reliably was make the spread depend on a swap
number nobody could verify.
"""

import time as time_mod
from datetime import datetime

from .fairvalue import fair_value_block


class QuoteAgeTracker:
    """How long since each leg's quote last CHANGED, measured locally.

    Live 2026-08-25: a manual short's take-profit fired on a spread of
    55.67 and filled at 57.18 — $15.10 of slippage against a $9.40
    target. Spot had run up 2.60 while the futures quote moved 1.29, so
    the spread appeared to collapse ~2.9 sigma; three minutes later it
    was back where it started, having gone nowhere. The futures fill
    came back 1.29 ABOVE the ask we had, which a market order cannot do
    unless the ask has moved. The engine took a profit that only existed
    on a stale price.

    Measured against `time.monotonic()` and the quote's own identity, NOT
    against the broker's timestamp. `tick.time - time.time()` conflates
    the broker's clock offset with how old the tick is — that conflation
    is what made the broker-clock line flap for weeks — and a clock
    offset would poison a guard that has to gate real orders.

    Ages are None until a leg has been seen CHANGE twice: an unknown age
    is not a fresh one, but it must not read as stale on the first poll
    either, so callers treat None as "no opinion".
    """

    def __init__(self, clock=time_mod.monotonic):
        self.clock = clock
        self._seen = {}

    def observe(self, key, market_data):
        """Stamp `spot_quote_age_sec` / `fut_quote_age_sec` on the
        snapshot and return them."""
        now = self.clock()
        ages = {}
        for leg, field in (('spot', 'spot_quote_age_sec'),
                           ('fut', 'fut_quote_age_sec')):
            qid = market_data.get(f'{leg}_quote_id')
            prev = self._seen.get((key, leg))
            if prev is None or prev[0] != qid:
                self._seen[(key, leg)] = (qid, now)
                age = 0.0 if prev is not None else None
            else:
                age = now - prev[1]
            market_data[field] = age
            ages[leg] = age
        return ages['spot'], ages['fut']

    def forget(self, key):
        for leg in ('spot', 'fut'):
            self._seen.pop((key, leg), None)


def stale_quote(market_data, max_age_sec):
    """A one-line reason why this snapshot must not price an order, or
    None when both legs are fresh enough.

    A pair trade is only as good as its WORSE leg: the spread is a
    difference, so one lagging quote makes the whole number fictitious
    even while the other leg ticks perfectly. `max_age_sec` of 0 turns
    the check off.
    """
    if not market_data or not max_age_sec or max_age_sec <= 0:
        return None
    worst, name = None, None
    for label, field in (('Leg A', 'spot_quote_age_sec'),
                         ('Leg B', 'fut_quote_age_sec')):
        age = market_data.get(field)
        if age is not None and age > max_age_sec \
                and (worst is None or age > worst):
            worst, name = age, label
    if worst is None:
        return None
    return (f"{name}'s quote has not moved for {worst:.1f}s "
            f"(limit {max_age_sec:g}s) — the spread is stale")


class SpreadJumpTracker:
    """Catches the OTHER way a pair's price goes wrong.

    `QuoteAgeTracker` finds a leg that has STOPPED. It cannot find a leg
    that lags during a fast move, because both legs are ticking hard —
    one is simply a moment behind the other, and the difference between
    them prints a spread that neither book ever offered.

    Live 2026-08-26, POS_0004: gold fell ~12 points in a minute, the
    futures leg led, and the spread printed 53.26 against a market that
    read 54.96 a minute before and 55.26 five seconds after. The
    operator's 54.18 target fired on it and filled at 55.30 — 2.04 of
    slippage, +$9.14 turned into -$2.10. The feed reported
    `oldest leg 0.0s` throughout, correctly: nothing was stale.

    The scale is the spread's own SIGMA, which is the unit every other
    number here is read in. Note it is the sigma of the LEVEL, not of
    the tick-to-tick change, and the change distribution is much
    tighter — so this threshold is generously wide and errs firmly
    towards letting a real move through. That is the right direction
    for a guard that can withhold an exit.

    A jump makes the price unusable until it has SETTLED: `settle_sec`
    with no further jump. A disturbance jumps twice — out and back —
    and the window covers both, clearing once the series is quiet.
    """

    def __init__(self, clock=time_mod.monotonic):
        self.clock = clock
        self._last = {}       # key -> (quote_id, spread)
        self._until = {}      # key -> (expiry, jump, sigmas)

    def observe(self, key, market_data, sigma, max_sigmas, settle_sec):
        """Stamp the jump on `market_data` and return a reason or None."""
        spread = (market_data or {}).get('spread')
        quote_id = (market_data or {}).get('quote_id')
        if spread is None:
            return None

        previous = self._last.get(key)
        moved = previous is not None and previous[0] != quote_id
        if previous is None or moved:
            self._last[key] = (quote_id, spread)

        # No sigma yet (cold start) or the guard is off: still TRACK the
        # series, so the first quote after warm-up has something to be
        # measured against, but hold no opinion.
        if not sigma or sigma <= 0 or not max_sigmas or max_sigmas <= 0:
            self._until.pop(key, None)
            return None

        now = self.clock()
        if moved:
            jump = abs(spread - previous[1])
            sigmas = jump / sigma
            market_data['spread_jump_sigmas'] = sigmas
            if sigmas > max_sigmas:
                self._until[key] = (now + max(settle_sec or 0.0, 0.0),
                                    jump, sigmas)

        pending = self._until.get(key)
        if pending is None:
            return None
        expiry, jump, sigmas = pending
        if now >= expiry:
            self._until.pop(key, None)
            return None
        return (f"the spread jumped {jump:.2f} ({sigmas:.1f} sigma) between "
                f"two quotes — one leg is lagging the other, so this level "
                f"is not one the market is offering")


def executable_spread(market_data, signal_type, closing=False):
    """The spread THIS action can actually be done at.

    Operator, 2026-08-24: "If the signal is short Spread - the relevant
    spread should be used for the calculations."

    A short-spread position SELLS the spread to get in and BUYS it back
    to get out, so the same position reads a different touch at each
    end. Getting that backwards is worse than using the mid: it reports
    the favourable side at both ends and makes every trade look like it
    cleared its costs.

        SELL_BASIS  entering -> short_spread   exiting -> long_spread
        BUY_BASIS   entering -> long_spread    exiting -> short_spread

    Falls back to the mid when the touches are not in the snapshot,
    which keeps older callers and replayed rows working — a missing
    touch is not a reason to refuse to price a level.
    """
    if not market_data:
        return None
    selling = (getattr(signal_type, 'value', signal_type) == 'SELL_BASIS')
    if closing:
        selling = not selling
    key = 'short_spread' if selling else 'long_spread'
    value = market_data.get(key)
    return market_data.get('spread') if value is None else value


def closing_prices(market_data, signal_type):
    """The two touches this position would actually be CLOSED at.

    Operator, 2026-08-25: "Do not use Mid. I would like the exact - Bid
    and Ask Price and the right Bid and Ask values should be taken."

    `executable_spread` above answers the same question for the SPREAD;
    this answers it per LEG, because the position's P&L is marked leg by
    leg. The two agree by construction — `futures - beta x spot` of the
    pair below IS the closing executable spread — and a test pins that,
    since a mark that disagreed with the level it is compared against is
    the fault this replaced.

        SELL_BASIS   long spot, short futures
                     -> close by SELLING spot (hit the BID) and BUYING
                        futures (lift the ASK)
        BUY_BASIS    the mirror: spot ASK, futures BID

    Falls back to each leg's mid when the touch is absent, so replayed
    rows and older snapshots still price.
    """
    if not market_data:
        return None, None
    selling = (getattr(signal_type, 'value', signal_type) == 'SELL_BASIS')
    spot_key, fut_key = (('spot_bid', 'futures_ask') if selling
                         else ('spot_ask', 'futures_bid'))
    spot = market_data.get(spot_key)
    fut = market_data.get(fut_key)
    return (market_data.get('spot_price') if spot is None else spot,
            market_data.get('futures_price') if fut is None else fut)


def compute_market_data(asset_cfg, spot_tick, futures_tick,
                        hedge_ratio=1.0):
    """Build the market-data snapshot from two ticks.

    Ticks are any objects with bid/ask/last attributes (mt5 ticks or
    SimpleNamespace built from IPC dicts).
    """
    multiplier = asset_cfg.get('multiplier', 1.0)

    # Identity of the two quotes this snapshot was built from. Two polls
    # that read the same pair of ticks are ONE observation of the
    # spread, however many times we looked; SpreadStats uses this to
    # keep its window a series of quote events rather than of poll
    # iterations. Prices join the tick times because some brokers stamp
    # ticks only to the second.
    spot_quote_id = "{}:{}/{}".format(
        getattr(spot_tick, 'time', ''), spot_tick.bid, spot_tick.ask)
    fut_quote_id = "{}:{}/{}".format(
        getattr(futures_tick, 'time', ''), futures_tick.bid, futures_tick.ask)
    quote_id = f"{spot_quote_id}|{fut_quote_id}"

    # The MID of the book, always — never `tick.last`.
    #
    # `last` is the price of the most recent TRADE. It preferred it
    # whenever the broker reported one, which made the spread a
    # different quantity from everything quoted beside it: short_spread,
    # long_spread, spread_cost, costs.round_trip_cost and slippage's
    # decision mid are all built from bid/ask. The module's own comment
    # below asserts `short <= mid <= long` "by construction", and with a
    # `last` outside the book that is simply false — a futures leg whose
    # last print sat 0.30 above the ask put the mid ABOVE the long
    # spread, i.e. above the best price anyone could buy at.
    #
    # It also matters for the series. mu/sigma/z need one continuous
    # definition, and a spread that switches from a midpoint to a trade
    # print whenever a trade happens to cross is not one — the jump
    # between them is noise the sigma then has to carry.
    spot_price = (spot_tick.bid + spot_tick.ask) / 2
    futures_price = (futures_tick.bid + futures_tick.ask) / 2 * multiplier

    beta = float(hedge_ratio or 1.0)
    spread = futures_price - beta * spot_price
    actual_basis = futures_price - spot_price      # raw, for reference

    # The two EXECUTABLE spreads (operator, 2026-08-24). The mid spread
    # above is a midpoint of two midpoints — nobody can trade it. Each
    # direction crosses a different pair of touches:
    #
    #   SHORT the spread = SELL futures, BUY spot
    #                    -> you are hit on the futures BID and lifted on
    #                       the spot ASK, so it is the WORSE (lower) one
    #   LONG the spread  = BUY futures, SELL spot
    #                    -> futures ASK and spot BID, the HIGHER one
    #
    # By construction short <= mid <= long, and the gap between them is
    # exactly one round turn of both legs' bid-ask in spread units:
    #   long - short = (fut_ask - fut_bid) + beta x (spot_ask - spot_bid)
    # which is the same quantity costs.round_trip_cost charges in
    # dollars. They are two views of one cost, NOT two costs — see
    # `spread_cost` below and the note in costs.py.
    fut_bid = futures_tick.bid * multiplier
    fut_ask = futures_tick.ask * multiplier
    short_spread = fut_bid - beta * spot_tick.ask
    long_spread = fut_ask - beta * spot_tick.bid

    # Expiry is OPTIONAL and no longer touches the spread — it is kept
    # only so the operator can see how far out the contract is and be
    # warned when it has rolled.
    expiry = asset_cfg.get('futures_expiry')
    time_to_expiry = ((expiry - datetime.now()).total_seconds()
                      / (365.25 * 24 * 3600)) if expiry else None
    days_to_expiry = time_to_expiry * 365.25 if time_to_expiry else 0

    snapshot = {
        'asset_name': asset_cfg['name'],
        'timestamp': datetime.now(),
        'quote_id': quote_id,
        # Per leg, so a lagging leg can be named. The spread is a
        # difference: one stale quote makes it fictitious even while the
        # other leg ticks perfectly.
        'spot_quote_id': spot_quote_id,
        'fut_quote_id': fut_quote_id,
        'spot_price': spot_price,
        'futures_price': futures_price,
        'spot_bid': spot_tick.bid,
        'spot_ask': spot_tick.ask,
        'futures_bid': futures_tick.bid * multiplier,
        'futures_ask': futures_tick.ask * multiplier,
        'spot_spread': (spot_tick.ask - spot_tick.bid) * 100,
        'futures_spread': (futures_tick.ask - futures_tick.bid) * 100,
        'spread_unit': '¢',
        'spread': spread,
        # What each direction can actually be done at, right now.
        'short_spread': short_spread,
        'long_spread': long_spread,
        # One round turn of both legs' bid-ask, in spread units.
        'spread_cost': long_spread - short_spread,
        'actual_basis': actual_basis,
        'hedge_ratio': beta,
        'basis_pct': (actual_basis / spot_price * 100) if spot_price else 0.0,
        'time_to_expiry': time_to_expiry,
        'days_to_expiry': days_to_expiry,
        # Spelled out so the number on the card can be checked against
        # the two prices beside it.
        'spread_formula': f"spread = futures - {beta:g} x spot",
    }
    # Reference only. Nothing in the signal, sizing or exit path reads
    # these keys — the traded spread above is already final.
    snapshot.update(fair_value_block(asset_cfg, spot_price, futures_price,
                                     spread, beta))
    return snapshot
