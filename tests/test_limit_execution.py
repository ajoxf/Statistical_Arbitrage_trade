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
        self.modifies = []              # (ticket, price)
        self.cancels = []
        self.market_orders = []         # (symbol, side, volume)
        self.closed_tickets = []        # (symbol, ticket, volume)
        self.states = {}
        self.next_ticket = 500

    def ensure_symbol(self, symbol):
        return {'ok': True, 'volume_min': 0.01, 'volume_max': 100.0,
                'volume_step': 0.01, 'point': 0.01}

    def tick(self, symbol):
        self.price += self.drift
        return {'bid': self.price - 0.05, 'ask': self.price + 0.05,
                'last': self.price, 'time': 0}

    def place_limit(self, symbol, side, volume, price, comment=""):
        self.next_ticket += 1
        self.placed.append((symbol, side, volume, price))
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
        state = self.states[ticket]
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


def test_hedging_close_targets_position_tickets(limit_config, data_logger):
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

    assert pm.close_position(position.position_id, "TAKE_PROFIT", px)
    assert position.status == PositionStatus.CLOSED
    # Every recorded ticket closed BY TICKET — mandatory in hedging mode
    assert sorted(t for _, t, _ in spot.closed_tickets) == \
        sorted(spot_trade.position_tickets)
    assert sorted(t for _, t, _ in fut.closed_tickets) == \
        sorted(fut_trade.position_tickets)
    # And no netting-style opposite market orders were sent
    assert spot.market_orders == [] and fut.market_orders == []


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
