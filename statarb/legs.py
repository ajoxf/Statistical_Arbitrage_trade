"""Leg interface: the coordinator's view of one account's execution.

Two implementations with identical methods:
- LocalLeg  — wraps a BrokerSession in-process (same-terminal setups).
- RemoteLeg — talks to a leg_runner process over localhost TCP
  (required when spot and futures live on different MT5 terminals,
  because one process can hold only one MT5 connection).

All methods speak plain dicts so results are identical whether the
leg is local or remote.
"""

import logging
import time

from . import ipc
from .models import OrderSide


class LocalLeg:
    def __init__(self, broker):
        self.broker = broker
        self.name = broker.account.name

    def connect(self):
        return self.broker.initialize()

    def close(self):
        self.broker.shutdown()

    def ping(self):
        return self.broker.is_alive()

    def account_info(self):
        info = self.broker.account_info()
        if not info:
            return None
        return {'login': info.login, 'server': info.server,
                'name': info.name, 'balance': info.balance,
                'equity': info.equity}

    def ensure_symbol(self, symbol):
        info = self.broker.ensure_symbol(symbol)
        if not info:
            return {'ok': False, 'error': f'Symbol {symbol} not found'}
        return {
            'ok': True,
            'volume_min': getattr(info, 'volume_min', 0.01),
            'volume_max': getattr(info, 'volume_max', 1000.0),
            'volume_step': getattr(info, 'volume_step', 0.01),
            'point': getattr(info, 'point', 0.01),
        }

    def tick(self, symbol):
        t = self.broker.symbol_tick(symbol)
        if not t:
            return None
        return {'bid': t.bid, 'ask': t.ask, 'last': t.last,
                'time': getattr(t, 'time', time.time())}

    def order(self, symbol, side, volume, slippage_points=1.0, comment=""):
        result = self.broker.send_market_order(
            symbol, OrderSide(side), volume,
            slippage_points=slippage_points, comment=comment)
        position_tickets = []
        if result.success and result.ticket:
            # Resolve which position(s) the fill created (hedging mode)
            state = self.broker.order_fill_state(result.ticket)
            position_tickets = state.get('position_tickets') or [result.ticket]
        return {
            'ok': result.success,
            'filled_volume': result.volume if result.success else 0.0,
            'price': result.executed_price,
            'ticket': result.ticket,
            'position_tickets': position_tickets,
            'error': result.error,
        }

    def place_limit(self, symbol, side, volume, price, comment=""):
        return self.broker.place_pending_limit(
            symbol, OrderSide(side), volume, price, comment=comment)

    def modify_order(self, ticket, price):
        return self.broker.modify_pending(ticket, price)

    def cancel_order(self, ticket):
        return self.broker.cancel_pending(ticket)

    def order_state(self, ticket):
        return self.broker.order_fill_state(ticket)

    def close_ticket(self, symbol, ticket, volume, entry_side,
                     slippage_points=1.0, comment=""):
        result = self.broker.close_position_ticket(
            symbol, ticket, volume, OrderSide(entry_side),
            slippage_points=slippage_points, comment=comment)
        return {
            'ok': result.success,
            'filled_volume': result.volume if result.success else 0.0,
            'price': result.executed_price,
            'error': result.error,
        }

    def positions(self, symbol=None):
        return self.broker.positions_by_magic(symbol)


class RemoteLeg:
    def __init__(self, name, endpoint, timeout=10.0):
        self.name = name
        self.host, self.port = ipc.parse_endpoint(endpoint)
        self.timeout = timeout
        self.conn = None

    def connect(self, retries=5, delay=2.0):
        for attempt in range(1, retries + 1):
            try:
                self.conn = ipc.connect(self.host, self.port, self.timeout)
                reply = self.conn.request({'cmd': 'ping'})
                if reply and reply.get('ok'):
                    logging.info("Connected to leg runner '%s' at %s:%s",
                                 self.name, self.host, self.port)
                    return True
            except OSError as e:
                logging.warning(
                    "Leg '%s' not reachable at %s:%s (attempt %d/%d): %s "
                    "— is its leg runner started?",
                    self.name, self.host, self.port, attempt, retries, e)
                time.sleep(delay)
        return False

    def close(self):
        if self.conn:
            try:
                self.conn.close()
            except OSError:
                pass
            self.conn = None

    def _request(self, msg):
        if not self.conn:
            return None
        try:
            return self.conn.request(msg)
        except (OSError, ValueError) as e:
            logging.error("IPC failure to leg '%s': %s", self.name, e)
            self.close()
            return None

    def ping(self):
        reply = self._request({'cmd': 'ping'})
        return bool(reply and reply.get('ok'))

    def account_info(self):
        reply = self._request({'cmd': 'account_info'})
        return reply.get('account') if reply and reply.get('ok') else None

    def ensure_symbol(self, symbol):
        reply = self._request({'cmd': 'ensure_symbol', 'symbol': symbol})
        return reply if reply else {'ok': False, 'error': 'IPC failure'}

    def tick(self, symbol):
        reply = self._request({'cmd': 'tick', 'symbol': symbol})
        if reply and reply.get('ok'):
            return reply['tick']
        return None

    def order(self, symbol, side, volume, slippage_points=1.0, comment=""):
        reply = self._request({
            'cmd': 'order', 'symbol': symbol, 'side': side,
            'volume': volume, 'slippage_points': slippage_points,
            'comment': comment,
        })
        if not reply:
            return {'ok': False, 'filled_volume': 0.0, 'price': None,
                    'ticket': None, 'position_tickets': [],
                    'error': 'IPC failure during order'}
        return reply

    def place_limit(self, symbol, side, volume, price, comment=""):
        reply = self._request({
            'cmd': 'place_limit', 'symbol': symbol, 'side': side,
            'volume': volume, 'price': price, 'comment': comment,
        })
        return reply or {'ok': False, 'ticket': None, 'error': 'IPC failure'}

    def modify_order(self, ticket, price):
        reply = self._request({'cmd': 'modify_order', 'ticket': ticket,
                               'price': price})
        return reply or {'ok': False, 'error': 'IPC failure'}

    def cancel_order(self, ticket):
        reply = self._request({'cmd': 'cancel_order', 'ticket': ticket})
        return reply or {'ok': False, 'cancelled': False, 'filled_volume': 0.0,
                         'price': None, 'position_tickets': [],
                         'still_open': True, 'error': 'IPC failure'}

    def order_state(self, ticket):
        reply = self._request({'cmd': 'order_state', 'ticket': ticket})
        return reply or {'ok': False, 'filled_volume': 0.0, 'price': None,
                         'position_tickets': [], 'still_open': True,
                         'error': 'IPC failure'}

    def close_ticket(self, symbol, ticket, volume, entry_side,
                     slippage_points=1.0, comment=""):
        reply = self._request({
            'cmd': 'close_ticket', 'symbol': symbol, 'ticket': ticket,
            'volume': volume, 'entry_side': entry_side,
            'slippage_points': slippage_points, 'comment': comment,
        })
        return reply or {'ok': False, 'filled_volume': 0.0, 'price': None,
                         'error': 'IPC failure during close'}

    def positions(self, symbol=None):
        reply = self._request({'cmd': 'positions', 'symbol': symbol})
        if reply and reply.get('ok'):
            return reply['positions']
        return None    # None = unknown (IPC failure), NOT "flat"
