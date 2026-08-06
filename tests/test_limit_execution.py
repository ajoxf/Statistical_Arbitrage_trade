"""Limit-first execution: peg, re-peg via modify, timeout escalation,
leaked-partial accounting, and hedging-mode closes by ticket."""

import pytest

from statarb.models import PositionStatus, SignalType
from statarb.pair_executor import PairExecutor
from statarb.positions import PositionManager


class FakeClock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t

    def sleep(self, dt):
        self.t += dt


class LimitFakeLeg:
    """Leg with resting-order simulation.

    limit_fill_polls[symbol]: order_state polls until a resting order
    fills (None = never fills). leak_on_cancel[symbol]: volume that
    turns out to have filled when the order is cancelled.
    """

    def __init__(self, name, price=100.0, limit_fill_polls=None,
                 leak_on_cancel=None, liquidity=None, fail_market=None,
                 drift=0.0):
        self.name = name
        self.price = price
        self.drift = drift              # price moves per tick() call
        self.limit_fill_polls = dict(limit_fill_polls or {})
        self.leak_on_cancel = dict(leak_on_cancel or {})
        self.liquidity = dict(liquidity or {})
        self.fail_market = set(fail_market or [])
        self.placed = []                # (symbol, side, volume, price)
        self.placed_position_tickets = []   # position kwarg per place_limit
        self.modifies = []              # (ticket, price)
        self.cancels = []
        self.market_orders = []         # (symbol, side, volume)
        self.volume_min = 0.01
        self.closed_tickets = []        # (symbol, ticket, volume)
        self.closes = []                # + entry_side
        self.stale_orders = []          # for pending_orders()
        self.states = {}
        self.next_ticket = 500

    def ensure_symbol(self, symbol):
        # volume_min is overridable: the two legs of a real pair often
        # have DIFFERENT minimums (CFI: spot 0.01, futures 0.1).
        return {'ok': True, 'volume_min': self.volume_min,
                'volume_max': 100.0, 'volume_step': 0.01,
                'point': 0.01, 'tick_size': 0.01}

    def pending_orders(self, symbol=None):
        return [o for o in self.stale_orders
                if symbol is None or o['symbol'] == symbol]

    def tick(self, symbol):
        self.price += self.drift
        return {'bid': self.price - 0.05, 'ask': self.price + 0.05,
                'last': self.price, 'time': 0}

    def place_limit(self, symbol, side, volume, price, comment="",
                    position_ticket=None):
        self.next_ticket += 1
        self.placed.append((symbol, side, volume, price))
        self.placed_position_tickets.append(position_ticket)
        self.states[self.next_ticket] = {
            'symbol': symbol, 'volume': volume, 'limit': price,
            'filled': 0.0, 'open': True,
            'polls_left': self.limit_fill_polls.get(symbol),
        }
        return {'ok': True, 'ticket': self.next_ticket, 'error': None}

    def modify_order(self, ticket, price):
        state = self.states.get(ticket)
        if not state or not state['open']:
            return {'ok': False, 'error': 'not open'}
        state['limit'] = price
        self.modifies.append((ticket, price))
        return {'ok': True, 'error': None}

    def order_state(self, ticket):
        state = self.states[ticket]
        if state['open'] and state['polls_left'] is not None:
            state['polls_left'] -= 1
            if state['polls_left'] <= 0:
                state['filled'] = state['volume']
                state['open'] = False
        return {'ok': True, 'filled_volume': state['filled'],
                'price': state['limit'] if state['filled'] else None,
                'position_tickets': [ticket] if state['filled'] else [],
                'still_open': state['open'], 'error': None}

    def cancel_order(self, ticket):
        state = self.states.get(ticket)
        if state is None:      # stale order swept from a prior session
            self.cancels.append(ticket)
            return {'ok': True, 'cancelled': True, 'filled_volume': 0.0,
                    'price': None, 'position_tickets': [],
                    'still_open': False, 'error': None}
        leak = self.leak_on_cancel.get(state['symbol'], 0.0)
        if state['open'] and leak > 0 and state['filled'] == 0:
            state['filled'] = min(leak, state['volume'])
        state['open'] = False
        self.cancels.append(ticket)
        return {'ok': True, 'cancelled': True,
                'filled_volume': state['filled'],
                'price': state['limit'] if state['filled'] else None,
                'position_tickets': [ticket] if state['filled'] else [],
                'still_open': False, 'error': None}

    def order(self, symbol, side, volume, slippage_points=1.0, comment=""):
        if symbol in self.fail_market:
            return {'ok': False, 'filled_volume': 0.0, 'price': None,
                    'ticket': None, 'position_tickets': [],
                    'error': 'forced failure'}
        # Liquidity budgets are per (symbol, side): consuming the book
        # on entry does not starve the unwind direction
        key = (symbol, side)
        available = self.liquidity.get(key, float('inf'))
        filled = min(volume, available)
        self.liquidity[key] = available - filled
        self.next_ticket += 1
        self.market_orders.append((symbol, side, volume))
        if filled <= 0:
            return {'ok': False, 'filled_volume': 0.0, 'price': None,
                    'ticket': None, 'position_tickets': [],
                    'error': 'no liquidity'}
        return {'ok': True, 'filled_volume': filled, 'price': self.price,
                'ticket': self.next_ticket,
                'position_tickets': [self.next_ticket], 'error': None}

    def close_ticket(self, symbol, ticket, volume, entry_side,
                     slippage_points=1.0, comment=""):
        self.closed_tickets.append((symbol, ticket, volume))
        # entry_side too: the fake used to DROP it, which is how
        # a leg role ('SPOT') reached OrderSide() unnoticed.
        self.closes.append((symbol, ticket, volume, entry_side))
        return {'ok': True, 'filled_volume': volume, 'price': self.price,
                'error': None}


@pytest.fixture
def limit_config(config):
    config.TRADING.update({'CLIP_LOTS': 50.0, 'SLICE_LOTS': 10.0,
                           'HEDGE_RATIO': 1.0})
    config.RISK_LIMITS['MAX_LOT_SIZE'] = 50.0
    config.EXECUTION.update({
        'ENTRY_STYLE': 'limit', 'REPEG_INTERVAL_SEC': 1.0,
        'LIMIT_TIMEOUT_SEC': 5.0, 'HEDGE_TIMEOUT_SEC': 2.0,
        'ON_TIMEOUT': 'cross', 'ORDER_POLL_SEC': 0.5,
        'MIN_MATCHED_FRACTION': 0.4,
    })
    return config


def make_executor(cfg, spot, fut):
    clock = FakeClock()
    return PairExecutor(cfg, spot, fut, clock=clock, sleep=clock.sleep), clock


def test_passive_fill_no_market_orders(limit_config):
    spot = LimitFakeLeg('a', limit_fill_polls={'XAUUSD': 1})
    fut = LimitFakeLeg('b', limit_fill_polls={'GC1225': 1})
    px, _ = make_executor(limit_config, spot, fut)

    ok, spot_trade, fut_trade = px.execute_trade_pair(
        'GOLD', SignalType.SELL_BASIS, 50.0, 'XAUUSD', 'GC1225')

    assert ok
    # Everything filled passively: 5 resting children per leg, zero
    # market orders — the whole clip saved the spread
    assert len(spot.placed) == 5 and len(fut.placed) == 5
    assert spot.market_orders == [] and fut.market_orders == []
    assert spot_trade.lot_size == pytest.approx(50.0)
    # Buys rested at the bid (price - 0.05)
    assert all(p[3] == pytest.approx(spot.price - 0.05) for p in spot.placed)


def test_repeg_uses_modify_not_cancel(limit_config):
    # Never fills, market drifting -> executor must chase via MODIFY,
    # then cancel once at timeout and cross the remainder
    limit_config.TRADING['SLICE_LOTS'] = 0.0   # one child per leg
    spot = LimitFakeLeg('a', limit_fill_polls={'XAUUSD': None}, drift=0.02)
    fut = LimitFakeLeg('b', limit_fill_polls={'GC1225': 1})
    px, _ = make_executor(limit_config, spot, fut)

    ok, spot_trade, _ = px.execute_trade_pair(
        'GOLD', SignalType.SELL_BASIS, 50.0, 'XAUUSD', 'GC1225')

    assert ok
    assert len(spot.modifies) >= 2          # re-pegged while resting
    assert len(spot.cancels) == 1           # exactly one cancel at timeout
    assert len(spot.market_orders) == 1     # remainder crossed
    assert spot_trade.lot_size == pytest.approx(50.0)


def test_leaked_partial_on_cancel_is_counted(limit_config):
    limit_config.TRADING['SLICE_LOTS'] = 0.0
    spot = LimitFakeLeg('a', limit_fill_polls={'XAUUSD': None},
                        leak_on_cancel={'XAUUSD': 12.0})
    fut = LimitFakeLeg('b', limit_fill_polls={'GC1225': 1})
    px, _ = make_executor(limit_config, spot, fut)

    ok, spot_trade, fut_trade = px.execute_trade_pair(
        'GOLD', SignalType.SELL_BASIS, 50.0, 'XAUUSD', 'GC1225')

    assert ok
    # 12 leaked on cancel + 38 crossed = 50; the leak was NOT lost
    assert spot_trade.lot_size == pytest.approx(50.0)
    assert spot.market_orders[0][2] == pytest.approx(38.0)


def test_matched_floor_rejects_runt_position(limit_config):
    # Futures can only fill 15 of 50 -> matched 15 < 40% floor (20):
    # everything must be unwound on BOTH legs and the entry failed
    spot = LimitFakeLeg('a', limit_fill_polls={'XAUUSD': 1})
    fut = LimitFakeLeg('b', limit_fill_polls={'GC1225': None},
                       liquidity={('GC1225', 'SELL'): 15.0})
    px, _ = make_executor(limit_config, spot, fut)

    ok, spot_trade, fut_trade = px.execute_trade_pair(
        'GOLD', SignalType.SELL_BASIS, 50.0, 'XAUUSD', 'GC1225')

    assert not ok
    spot_unwound = sum(v for s, side, v in spot.market_orders
                       if side == 'SELL')
    fut_unwound = sum(v for s, side, v in fut.market_orders
                      if side == 'BUY')
    assert spot_unwound == pytest.approx(50.0)
    assert fut_unwound == pytest.approx(15.0)


def test_matched_floor_keeps_at_40_percent(limit_config):
    spot = LimitFakeLeg('a', limit_fill_polls={'XAUUSD': 1})
    fut = LimitFakeLeg('b', limit_fill_polls={'GC1225': None},
                       liquidity={('GC1225', 'SELL'): 20.0})
    px, _ = make_executor(limit_config, spot, fut)

    ok, spot_trade, fut_trade = px.execute_trade_pair(
        'GOLD', SignalType.SELL_BASIS, 50.0, 'XAUUSD', 'GC1225')

    assert ok
    assert spot_trade.lot_size == pytest.approx(20.0)
    assert fut_trade.lot_size == pytest.approx(20.0)


def test_hedging_close_limit_first_targets_tickets(limit_config,
                                                   data_logger):
    spot = LimitFakeLeg('a', limit_fill_polls={'XAUUSD': 1})
    fut = LimitFakeLeg('b', limit_fill_polls={'GC1225': 1})
    px, _ = make_executor(limit_config, spot, fut)
    pm = PositionManager(data_logger)

    ok, spot_trade, fut_trade = px.execute_trade_pair(
        'GOLD', SignalType.SELL_BASIS, 50.0, 'XAUUSD', 'GC1225')
    assert ok
    assert len(spot_trade.position_tickets) == 5   # one per child fill
    position = pm.create_position('GOLD', SignalType.SELL_BASIS,
                                  spot_trade, fut_trade, 25.0)
    entry_limits = len(spot.placed)

    assert pm.close_position(position.position_id, "TAKE_PROFIT", px)
    assert position.status == PositionStatus.CLOSED
    # Non-urgent close on hedging mode = closing LIMIT orders, each
    # targeting a recorded position ticket (saves the spread on exit)
    close_tickets = [t for t in spot.placed_position_tickets[entry_limits:]
                     if t is not None]
    assert sorted(close_tickets) == sorted(spot_trade.position_tickets)
    # All filled at the limit: no market closes needed
    assert spot.closed_tickets == [] and fut.closed_tickets == []
    assert spot.market_orders == [] and fut.market_orders == []


def test_urgent_ticket_close_goes_straight_to_market(limit_config,
                                                     data_logger):
    spot = LimitFakeLeg('a', limit_fill_polls={'XAUUSD': 1})
    fut = LimitFakeLeg('b', limit_fill_polls={'GC1225': 1})
    px, _ = make_executor(limit_config, spot, fut)
    pm = PositionManager(data_logger)

    ok, spot_trade, fut_trade = px.execute_trade_pair(
        'GOLD', SignalType.SELL_BASIS, 50.0, 'XAUUSD', 'GC1225')
    assert ok
    position = pm.create_position('GOLD', SignalType.SELL_BASIS,
                                  spot_trade, fut_trade, 25.0)
    entry_limits = len(spot.placed)

    assert pm.close_position(position.position_id, "DOLLAR_STOP", px)
    assert position.status == PositionStatus.CLOSED
    # A stop NEVER rests: market ticket-closes, zero new limits
    assert len(spot.placed) == entry_limits
    assert sorted(t for _, t, _ in spot.closed_tickets) == \
        sorted(spot_trade.position_tickets)


def test_limit_close_timeout_escalates_to_market_close(limit_config,
                                                       data_logger):
    limit_config.TRADING['SLICE_LOTS'] = 0.0    # one ticket per leg
    spot = LimitFakeLeg('a', limit_fill_polls={'XAUUSD': 1})
    fut = LimitFakeLeg('b', limit_fill_polls={'GC1225': 1})
    px, _ = make_executor(limit_config, spot, fut)
    pm = PositionManager(data_logger)

    ok, spot_trade, fut_trade = px.execute_trade_pair(
        'GOLD', SignalType.SELL_BASIS, 50.0, 'XAUUSD', 'GC1225')
    assert ok
    position = pm.create_position('GOLD', SignalType.SELL_BASIS,
                                  spot_trade, fut_trade, 25.0)
    # Closing limits never fill this time
    spot.limit_fill_polls['XAUUSD'] = None
    fut.limit_fill_polls['GC1225'] = None

    assert pm.close_position(position.position_id, "TAKE_PROFIT", px)
    assert position.status == PositionStatus.CLOSED
    # Escalation used the ticket-close endpoint, not a plain market order
    assert [t for _, t, _ in spot.closed_tickets] == \
        spot_trade.position_tickets
    assert spot.market_orders == []


def test_stale_pending_orders_swept_before_entry(limit_config):
    spot = LimitFakeLeg('a', limit_fill_polls={'XAUUSD': 1})
    fut = LimitFakeLeg('b', limit_fill_polls={'GC1225': 1})
    spot.stale_orders = [{'ticket': 7001, 'symbol': 'XAUUSD',
                          'volume': 10.0, 'price': 3300.0}]
    px, _ = make_executor(limit_config, spot, fut)

    ok, _, _ = px.execute_trade_pair(
        'GOLD', SignalType.SELL_BASIS, 50.0, 'XAUUSD', 'GC1225')
    assert ok
    # The leftover order from a prior timed-out execution was cancelled
    # BEFORE any new order went out
    assert spot.cancels[0] == 7001


def test_peg_price_clamped_inside_book_and_rounded(limit_config):
    # Offset so large it would cross the ask -> falls back to the bid;
    # and prices always land on the tick grid
    limit_config.EXECUTION['PEG_OFFSET_POINTS'] = 10.0   # 0.10 vs 0.10 spread
    spot = LimitFakeLeg('a', price=100.003)
    px, _ = make_executor(limit_config, spot, LimitFakeLeg('b'))
    from statarb.models import OrderSide
    meta = spot.ensure_symbol('XAUUSD')

    buy_price = px._peg_price(spot, 'XAUUSD', OrderSide.BUY, meta)
    tick = spot.tick('XAUUSD')
    assert buy_price < tick['ask']                       # never crosses
    assert abs(buy_price / 0.01 - round(buy_price / 0.01)) < 1e-9  # on grid


def test_urgent_close_goes_to_market_when_no_tickets(limit_config,
                                                     data_logger):
    # No tickets recorded (netting-style fills) + STOP reason ->
    # market path, never a resting limit
    from tests.test_pair_executor import FakeLeg
    spot = FakeLeg('a')
    fut = FakeLeg('b')
    limit_config.EXECUTION['ENTRY_STYLE'] = 'market'
    px, _ = make_executor(limit_config, spot, fut)
    pm = PositionManager(data_logger)

    ok, spot_trade, fut_trade = px.execute_trade_pair(
        'GOLD', SignalType.SELL_BASIS, 50.0, 'XAUUSD', 'GC1225')
    assert ok
    position = pm.create_position('GOLD', SignalType.SELL_BASIS,
                                  spot_trade, fut_trade, 25.0)

    assert pm.close_position(position.position_id, "DOLLAR_STOP", px)
    assert position.status == PositionStatus.CLOSED
