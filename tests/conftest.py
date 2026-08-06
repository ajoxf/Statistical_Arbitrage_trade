import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from statarb.broker import OrderResult  # noqa: E402
from statarb.config import AlgoTradingConfig  # noqa: E402
from statarb.database import DataLogger  # noqa: E402


class FakeBroker:
    """In-memory broker: records orders, no MT5 required."""

    def __init__(self, fail_symbols=None, price=100.0):
        self.orders = []          # (symbol, side_value, volume)
        self.pending = []         # (symbol, side_value, volume, price)
        self.modifies = []        # (ticket, new_price)
        self.closed_tickets = []  # (symbol, ticket, volume)
        self.closes = []          # + entry_side
        self.fill_states = {}     # ticket -> state
        self.pending_fill_after = {}   # symbol -> polls until fill
        self.live_positions = []       # for positions_by_magic
        self.broker_order_log = []     # for order_log (Exchange Order Log)
        self.fail_symbols = set(fail_symbols or [])
        self.price = price
        self.next_ticket = 1000
        self.account = SimpleNamespace(name="fake")

    def send_market_order(self, symbol, side, volume,
                          slippage_points=1.0, comment=""):
        if symbol in self.fail_symbols:
            return OrderResult(False, error="forced failure")
        self.orders.append((symbol, side.value, volume))
        self.next_ticket += 1
        self.fill_states[self.next_ticket] = {
            'filled': volume, 'price': self.price,
            'position_tickets': [self.next_ticket], 'open': False}
        return OrderResult(True, requested_price=self.price,
                           executed_price=self.price,
                           ticket=self.next_ticket, volume=volume)

    # --- pending-order simulation (limit path) ---

    def place_pending_limit(self, symbol, side, volume, price, comment="",
                            position_ticket=None):
        if symbol in self.fail_symbols:
            return {'ok': False, 'ticket': None, 'error': 'forced failure'}
        self.next_ticket += 1
        # pending_fill_after: how many order_fill_state polls until the
        # resting order fills (default: fills on first poll)
        polls = self.pending_fill_after.get(symbol, 1)
        self.fill_states[self.next_ticket] = {
            'filled': 0.0, 'price': None, 'position_tickets': [],
            'open': True, 'volume': volume, 'limit_price': price,
            'polls_left': polls}
        self.pending.append((symbol, side.value, volume, price))
        return {'ok': True, 'ticket': self.next_ticket, 'error': None}

    def modify_pending(self, ticket, price):
        state = self.fill_states.get(ticket)
        if not state or not state['open']:
            return {'ok': False, 'error': 'order not open'}
        state['limit_price'] = price
        self.modifies.append((ticket, price))
        return {'ok': True, 'error': None}

    def cancel_pending(self, ticket):
        state = self.order_fill_state(ticket)
        live = self.fill_states.get(ticket)
        if live:
            live['open'] = False
        state['cancelled'] = True
        return state

    def order_fill_state(self, ticket):
        state = self.fill_states.get(ticket)
        if state is None:
            return {'ok': False, 'filled_volume': 0.0, 'price': None,
                    'position_tickets': [], 'still_open': False,
                    'error': 'unknown ticket'}
        if state['open'] and state.get('polls_left') is not None:
            state['polls_left'] -= 1
            if state['polls_left'] <= 0:
                state['filled'] = state['volume']
                state['price'] = state['limit_price']
                state['position_tickets'] = [ticket]
                state['open'] = False
        return {'ok': True, 'filled_volume': state['filled'],
                'price': state['price'],
                'position_tickets': list(state['position_tickets']),
                'still_open': state['open'], 'error': None}

    def close_position_ticket(self, symbol, ticket, volume, entry_side,
                              slippage_points=1.0, comment=""):
        if symbol in self.fail_symbols:
            return OrderResult(False, error="forced failure")
        self.closed_tickets.append((symbol, ticket, volume))
        # entry_side too: the fake used to DROP it, which is how
        # a leg role ('SPOT') reached OrderSide() unnoticed.
        self.closes.append((symbol, ticket, volume, entry_side))
        return OrderResult(True, executed_price=self.price,
                           ticket=ticket, volume=volume)

    def positions_by_magic(self, symbol=None):
        return [p for p in self.live_positions
                if symbol is None or p['symbol'] == symbol]

    def pending_orders_by_magic(self, symbol=None):
        return []

    def order_log(self, hours=24):
        return [dict(row) for row in self.broker_order_log]

    # --- methods used by LocalLeg / LegServer ---

    def initialize(self):
        return True

    def shutdown(self):
        pass

    def is_alive(self):
        return True

    def account_info(self):
        return SimpleNamespace(login=1, server="FakeServer", name="Fake",
                               balance=1e6, equity=1e6)

    def ensure_symbol(self, symbol):
        if symbol in self.fail_symbols:
            return None
        return SimpleNamespace(visible=True, point=0.01, volume_min=0.01,
                               volume_max=200.0, volume_step=0.01,
                               trade_tick_size=0.01)

    def symbol_tick(self, symbol):
        if symbol in self.fail_symbols:
            return None
        return SimpleNamespace(bid=self.price - 0.05, ask=self.price + 0.05,
                               last=self.price, time=int(time.time()))


@pytest.fixture
def config():
    return AlgoTradingConfig()


@pytest.fixture
def data_logger(tmp_path):
    return DataLogger(db_path=str(tmp_path / "test.db"))


@pytest.fixture
def fake_broker():
    return FakeBroker()
