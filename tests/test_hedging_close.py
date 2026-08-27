"""A close must CLOSE, on every path out of the limit ladder.

These accounts are HEDGING mode, where a plain opposite market order
does not close anything — it opens a second, offsetting position and
leaves the first one on the book.

Live 2026-08-26, the first session run with EXIT_STYLE=limit: the spot
exit fell out of the limit path onto a ticketless market order, so the
engine booked the trade closed while the broker held BOTH rows. The
reconciler force-closed them 40 seconds later — economically flat, two
extra round trips paid, and naked for exactly as long as the reconciler
was unavailable.

Every assertion here is the STRONG form: the leg's book is EMPTY after
the close, not "flat by offsetting".
"""

import pytest

from statarb.models import SignalType
from statarb.pair_executor import PairExecutor
from statarb.positions import PositionManager


class FakeClock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t

    def sleep(self, dt):
        self.t += dt


class HedgingLeg:
    """A leg that keeps a HEDGING book, so an offsetting order is
    visible as a second position rather than as a net-flat account.

    limit_ok=False      -> place_limit is rejected by the broker
    has_tick=False      -> no fresh tick, so no peg price
    limit_fills=False   -> resting orders never fill (timeout path)
    record_tickets=False-> fills come back with no position ticket,
                           the way a netting-style fill does
    """

    def __init__(self, name, price=100.0, limit_ok=True, has_tick=True,
                 limit_fills=True, record_tickets=True):
        self.name = name
        self.price = price
        self.limit_ok = limit_ok
        self.has_tick = has_tick
        self.limit_fills = limit_fills
        self.record_tickets = record_tickets
        self.book = {}                  # ticket -> {symbol, side, volume}
        self.orders = []                # plain market orders sent
        self.closed_tickets = []        # (symbol, ticket, volume)
        self.placed = []                # (symbol, side, volume, ticket)
        self.comments = []              # every comment we were sent
        self.resting = {}
        self.next_ticket = 900

    # -- book helpers --------------------------------------------------
    def _open(self, symbol, side, volume):
        self.next_ticket += 1
        self.book[self.next_ticket] = {'symbol': symbol, 'side': side,
                                       'volume': volume}
        return self.next_ticket

    def _reduce(self, ticket, volume):
        row = self.book.get(ticket)
        if row is None:
            return 0.0
        taken = min(row['volume'], volume)
        row['volume'] -= taken
        if row['volume'] <= 1e-9:
            del self.book[ticket]
        return taken

    # -- leg interface -------------------------------------------------
    def ensure_symbol(self, symbol):
        return {'ok': True, 'volume_min': 0.01, 'volume_max': 100.0,
                'volume_step': 0.01, 'point': 0.01, 'tick_size': 0.01}

    def pending_orders(self, symbol=None):
        return []

    def cancel_pending(self, ticket):
        return {'ok': True}

    def tick(self, symbol):
        if not self.has_tick:
            return None
        return {'bid': self.price - 0.05, 'ask': self.price + 0.05,
                'last': self.price, 'time': 0}

    def positions(self, symbol=None):
        return [{'ticket': t, 'symbol': r['symbol'], 'side': r['side'],
                 'volume': r['volume'], 'price_open': self.price}
                for t, r in sorted(self.book.items())
                if symbol is None or r['symbol'] == symbol]

    def order(self, symbol, side, volume, slippage_points=1.0, comment=""):
        ticket = self._open(symbol, side, volume)
        self.orders.append((symbol, side, volume))
        return {'ok': True, 'filled_volume': volume, 'price': self.price,
                'ticket': ticket,
                'position_tickets': [ticket] if self.record_tickets else [],
                'error': None}

    def place_limit(self, symbol, side, volume, price, comment="",
                    position_ticket=None):
        if not self.limit_ok:
            return {'ok': False, 'ticket': None,
                    'error': '10015 - Invalid price'}
        self.next_ticket += 1
        ticket = self.next_ticket
        self.placed.append((symbol, side, volume, position_ticket))
        self.comments.append(comment)
        self.resting[ticket] = {'symbol': symbol, 'side': side,
                                'volume': volume,
                                'position': position_ticket, 'open': True}
        return {'ok': True, 'ticket': ticket, 'error': None}

    def modify_order(self, ticket, price):
        return {'ok': True, 'error': None}

    def order_state(self, ticket):
        rest = self.resting[ticket]
        if not (rest['open'] and self.limit_fills):
            return {'ok': True, 'filled_volume': 0.0, 'price': None,
                    'position_tickets': [], 'still_open': rest['open'],
                    'error': None}
        rest['open'] = False
        if rest['position']:
            self._reduce(rest['position'], rest['volume'])
            tickets = [rest['position']]
        else:
            # A limit with no position attached OPENS on a hedging
            # account, whichever way round it points.
            tickets = [self._open(rest['symbol'], rest['side'],
                                  rest['volume'])]
        return {'ok': True, 'filled_volume': rest['volume'],
                'price': self.price, 'position_tickets': tickets,
                'still_open': False, 'error': None}

    def cancel_order(self, ticket):
        self.resting[ticket]['open'] = False
        return {'ok': True, 'cancelled': True, 'filled_volume': 0.0,
                'price': None, 'position_tickets': [], 'still_open': False,
                'error': None}

    def close_ticket(self, symbol, ticket, volume, entry_side,
                     slippage_points=1.0, comment=""):
        taken = self._reduce(ticket, volume)
        self.closed_tickets.append((symbol, ticket, volume))
        self.comments.append(comment)
        return {'ok': True, 'filled_volume': taken or volume,
                'price': self.price, 'error': None}


@pytest.fixture
def hedge_config(config):
    config.TRADING.update({'CLIP_LOTS': 1.0, 'SLICE_LOTS': 0.0,
                           'HEDGE_RATIO': 1.0})
    config.RISK_LIMITS['MAX_LOT_SIZE'] = 10.0
    config.EXECUTION.update({
        'ENTRY_STYLE': 'market', 'EXIT_STYLE': 'limit',
        'REPEG_INTERVAL_SEC': 1.0, 'LIMIT_TIMEOUT_SEC': 5.0,
        'EXIT_TIMEOUT_SEC': 5.0, 'HEDGE_TIMEOUT_SEC': 2.0,
        'ON_TIMEOUT': 'cross', 'ORDER_POLL_SEC': 0.5,
        'MIN_MATCHED_FRACTION': 0.4,
    })
    return config


def open_pair(cfg, spot, fut, pm):
    clock = FakeClock()
    px = PairExecutor(cfg, spot, fut, clock=clock, sleep=clock.sleep)
    ok, spot_trade, fut_trade = px.execute_trade_pair(
        'GOLD', SignalType.SELL_BASIS, 1.0, 'XAUUSD', 'GC1225')
    assert ok
    position = pm.create_position('GOLD', SignalType.SELL_BASIS,
                                  spot_trade, fut_trade, 25.0)
    return px, position


def test_a_rejected_closing_limit_crosses_by_ticket(hedge_config,
                                                    data_logger):
    """place_limit refused (10015 on a symbol with a stops level).

    The old fallback sent a plain opposite market order: the entry
    stayed open and a second position appeared beside it."""
    spot = HedgingLeg('a')
    fut = HedgingLeg('b')
    pm = PositionManager(data_logger)
    px, position = open_pair(hedge_config, spot, fut, pm)
    assert len(spot.book) == 1 and len(fut.book) == 1

    spot.limit_ok = False
    fut.limit_ok = False
    assert pm.close_position(position.position_id, "MANUAL_TARGET", px)

    assert spot.book == {} and fut.book == {}
    assert spot.orders == [('XAUUSD', 'BUY', 1.0)]      # the ENTRY only
    assert [t for _, t, _ in spot.closed_tickets] == \
        position.spot_trade.position_tickets


def test_no_tick_to_peg_on_crosses_by_ticket(hedge_config, data_logger):
    spot = HedgingLeg('a')
    fut = HedgingLeg('b')
    pm = PositionManager(data_logger)
    px, position = open_pair(hedge_config, spot, fut, pm)

    spot.has_tick = False
    fut.has_tick = False
    assert pm.close_position(position.position_id, "MANUAL_TARGET", px)

    assert spot.book == {} and fut.book == {}
    assert len(spot.orders) == 1 and len(fut.orders) == 1   # entries only


def test_a_closing_limit_that_times_out_still_closes(hedge_config,
                                                     data_logger):
    spot = HedgingLeg('a')
    fut = HedgingLeg('b')
    pm = PositionManager(data_logger)
    px, position = open_pair(hedge_config, spot, fut, pm)

    spot.limit_fills = False
    fut.limit_fills = False
    assert pm.close_position(position.position_id, "MANUAL_TARGET", px)

    assert spot.book == {} and fut.book == {}
    assert spot.closed_tickets and fut.closed_tickets


def test_a_closing_limit_that_fills_closes_the_ticket(hedge_config,
                                                      data_logger):
    """The happy path, asserted the same strong way — otherwise the
    tests above could pass for the wrong reason."""
    spot = HedgingLeg('a')
    fut = HedgingLeg('b')
    pm = PositionManager(data_logger)
    px, position = open_pair(hedge_config, spot, fut, pm)

    assert pm.close_position(position.position_id, "MANUAL_TARGET", px)

    assert spot.book == {} and fut.book == {}
    assert spot.closed_tickets == []                    # filled passively
    assert [p[3] for p in spot.placed] == \
        position.spot_trade.position_tickets             # position= set


def test_an_entry_limit_may_still_cross_with_a_market_order(hedge_config,
                                                            data_logger):
    """The ticket rule is about CLOSES. An opening order has no ticket
    to close and must still be able to cross."""
    hedge_config.EXECUTION['ENTRY_STYLE'] = 'limit'
    spot = HedgingLeg('a', limit_ok=False)
    fut = HedgingLeg('b', limit_ok=False)
    pm = PositionManager(data_logger)
    px, position = open_pair(hedge_config, spot, fut, pm)

    assert spot.orders == [('XAUUSD', 'BUY', 1.0)]
    assert len(spot.book) == 1


def test_a_close_with_no_recorded_tickets_reads_the_book(hedge_config,
                                                         data_logger):
    """The entry recorded no position tickets (deal history lagged, or
    an older row). The book still knows, so close what it holds — an
    opposite order here is what opened the second position live."""
    spot = HedgingLeg('a', record_tickets=False)
    fut = HedgingLeg('b', record_tickets=False)
    pm = PositionManager(data_logger)
    px, position = open_pair(hedge_config, spot, fut, pm)
    assert position.spot_trade.position_tickets == []
    entry_ticket = next(iter(spot.book))

    assert pm.close_position(position.position_id, "MANUAL_TARGET", px)

    assert spot.book == {} and fut.book == {}
    assert spot.orders == [('XAUUSD', 'BUY', 1.0)]      # no opposite order
    assert [p[3] for p in spot.placed] == [entry_ticket]


def test_book_recovery_never_closes_more_than_the_trade(hedge_config,
                                                        data_logger):
    """A second position on the same symbol and side — another clip, or
    something the operator placed — must be left alone."""
    spot = HedgingLeg('a', record_tickets=False)
    fut = HedgingLeg('b', record_tickets=False)
    pm = PositionManager(data_logger)
    px, position = open_pair(hedge_config, spot, fut, pm)
    stranger = spot._open('XAUUSD', 'BUY', 3.0)

    assert pm.close_position(position.position_id, "MANUAL_TARGET", px)

    assert list(spot.book) == [stranger]
    assert spot.book[stranger]['volume'] == pytest.approx(3.0)


def test_an_empty_book_still_falls_through_to_an_opposite_order(
        hedge_config, data_logger):
    """Netting accounts, and any leg whose book shows nothing of ours:
    the opposite order is the correct instrument there and must
    survive."""
    spot = HedgingLeg('a', record_tickets=False)
    fut = HedgingLeg('b', record_tickets=False)
    pm = PositionManager(data_logger)
    px, position = open_pair(hedge_config, spot, fut, pm)
    spot.book.clear()
    fut.book.clear()

    assert pm.close_position(position.position_id, "MANUAL_TARGET", px)

    # One resting close per leg, with no position attached to it
    assert [p[3] for p in spot.placed] == [None]
    assert spot.closed_tickets == []


# ----------------------------------------------------------------------
# The close carries the SOURCE its entry did
# ----------------------------------------------------------------------

@pytest.mark.parametrize('source, prefix', [
    ('MANUAL', 'MANUAL_CX'),
    ('SIGNAL', 'BASIS_ARB_CX'),
    (None, 'BASIS_ARB_CX'),          # unstamped -> the strategy, fail safe
])
def test_the_close_comment_says_who_placed_the_trade(hedge_config,
                                                     data_logger,
                                                     source, prefix):
    """The Exchange Order Log reads the SOURCE off this comment. Every
    close was tagged BASIS_ARB_CX regardless, so a hand-placed trade's
    entry said MANUAL and its exit said ALGO — one trade, two answers."""
    spot = HedgingLeg('a')
    fut = HedgingLeg('b')
    pm = PositionManager(data_logger)
    px, position = open_pair(hedge_config, spot, fut, pm)
    position.exit_plan = {'source': source} if source else None

    assert pm.close_position(position.position_id, "MANUAL_TARGET", px)

    assert spot.placed, 'the close should have placed something'
    comment_seen = [c for c in spot.comments if '_CX_' in c]
    assert comment_seen, spot.comments
    assert all(c.startswith(prefix) for c in comment_seen), comment_seen
