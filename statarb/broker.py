"""Broker session: the only module that touches the MetaTrader5 package.

HARD CONSTRAINT: the MetaTrader5 Python package holds ONE global
connection per process. Initializing a second session replaces the
first. True simultaneous streaming from two accounts therefore
requires one process per account plus a coordinator (see CLAUDE.md).
This class makes the connection explicit (path/login/server from
config) so each such process can be pointed at its own terminal.
"""

import logging
from datetime import datetime

from .models import OrderSide

try:
    import MetaTrader5 as mt5
except ImportError:  # not available off-Windows; tests use FakeBroker
    mt5 = None

MAGIC_NUMBER = 12345


class OrderResult:
    """Outcome of a market order, decoupled from mt5 result objects."""

    def __init__(self, success, requested_price=None, executed_price=None,
                 ticket=None, error=None, volume=0.0):
        self.success = success
        self.requested_price = requested_price
        self.executed_price = executed_price
        self.ticket = ticket
        self.error = error
        self.volume = volume  # filled volume (IOC may partially fill)


class BrokerSession:
    """One MT5 terminal connection for one account."""

    def __init__(self, account):
        self.account = account
        self.connected = False

    def initialize(self):
        if mt5 is None:
            logging.error(
                "MetaTrader5 package not installed (Windows-only). "
                "Install it on the trading machine.")
            return False

        kwargs = {}
        if self.account.terminal_path:
            kwargs['path'] = self.account.terminal_path
        if self.account.login:
            kwargs['login'] = self.account.login
            kwargs['password'] = self.account.password or ""
            kwargs['server'] = self.account.server or ""

        if not mt5.initialize(**kwargs):
            logging.error("MT5 initialization failed for account '%s': %s",
                          self.account.name, mt5.last_error())
            return False

        self.connected = True
        info = mt5.account_info()
        if info:
            logging.info("Connected [%s]: %s / %s (login %s)",
                         self.account.name, info.server, info.name, info.login)
        return True

    def shutdown(self):
        if mt5 is not None:
            mt5.shutdown()
        self.connected = False

    def is_alive(self):
        return mt5 is not None and mt5.terminal_info() is not None

    def account_info(self):
        return mt5.account_info() if mt5 else None

    def symbol_info(self, symbol):
        return mt5.symbol_info(symbol) if mt5 else None

    def ensure_symbol(self, symbol):
        """Return symbol info, selecting it into Market Watch if hidden."""
        info = self.symbol_info(symbol)
        if info and not info.visible:
            mt5.symbol_select(symbol, True)
        return info

    def symbol_tick(self, symbol):
        return mt5.symbol_info_tick(symbol) if mt5 else None

    def positions_by_magic(self, symbol=None):
        """Open positions created by THIS system (magic-scoped) —
        never touches manual or third-party positions."""
        if mt5 is None:
            return []
        raw = (mt5.positions_get(symbol=symbol) if symbol
               else mt5.positions_get()) or ()
        out = []
        for p in raw:
            if p.magic != MAGIC_NUMBER:
                continue
            out.append({
                'ticket': p.ticket,
                'symbol': p.symbol,
                'side': ('BUY' if p.type == mt5.POSITION_TYPE_BUY
                         else 'SELL'),
                'volume': p.volume,
                'price_open': p.price_open,
            })
        return out

    def account_is_hedging(self):
        """True when the account holds one position per order (hedging
        mode) rather than netting per symbol."""
        if mt5 is None:
            return False
        info = mt5.account_info()
        return bool(info) and info.margin_mode == \
            mt5.ACCOUNT_MARGIN_MODE_RETAIL_HEDGING

    def symbol_filling_modes(self, symbol):
        """Which type_filling values this symbol allows (broker-dependent)."""
        info = self.symbol_info(symbol)
        if not info:
            return []
        mask = getattr(info, 'filling_mode', 0)
        modes = []
        if mask & 1:
            modes.append('FOK')
        if mask & 2:
            modes.append('IOC')
        modes.append('RETURN')  # always available for pending orders
        return modes

    def place_pending_limit(self, symbol, side, volume, price, comment=""):
        """Rest a limit order (RETURN filling: partials stay working)."""
        try:
            request = {
                "action": mt5.TRADE_ACTION_PENDING,
                "symbol": symbol,
                "volume": volume,
                "type": (mt5.ORDER_TYPE_BUY_LIMIT if side is OrderSide.BUY
                         else mt5.ORDER_TYPE_SELL_LIMIT),
                "price": price,
                "magic": MAGIC_NUMBER,
                "comment": comment,
                "type_time": mt5.ORDER_TIME_GTC,
                "type_filling": mt5.ORDER_FILLING_RETURN,
            }
            result = mt5.order_send(request)
            if result is None or result.retcode != mt5.TRADE_RETCODE_DONE:
                error = (mt5.last_error() if result is None
                         else f"{result.retcode} - {result.comment}")
                return {'ok': False, 'ticket': None, 'error': str(error)}
            return {'ok': True, 'ticket': result.order, 'error': None}
        except Exception as e:
            return {'ok': False, 'ticket': None, 'error': str(e)}

    def modify_pending(self, ticket, price):
        """Re-peg a resting limit in place — no cancel/replace round trip."""
        try:
            result = mt5.order_send({
                "action": mt5.TRADE_ACTION_MODIFY,
                "order": ticket,
                "price": price,
            })
            ok = result is not None and result.retcode == mt5.TRADE_RETCODE_DONE
            return {'ok': ok,
                    'error': None if ok else
                    (str(mt5.last_error()) if result is None
                     else f"{result.retcode} - {result.comment}")}
        except Exception as e:
            return {'ok': False, 'error': str(e)}

    def cancel_pending(self, ticket):
        """Remove a resting order, then ALWAYS report what filled first —
        a 'cancelled' order can carry partial fills."""
        try:
            result = mt5.order_send({
                "action": mt5.TRADE_ACTION_REMOVE,
                "order": ticket,
            })
            state = self.order_fill_state(ticket)
            state['cancelled'] = (result is not None and
                                  result.retcode == mt5.TRADE_RETCODE_DONE)
            return state
        except Exception as e:
            state = self.order_fill_state(ticket)
            state['cancelled'] = False
            state['error'] = str(e)
            return state

    def order_fill_state(self, ticket):
        """Filled volume / VWAP / position tickets for an order, from the
        deal history (works for pending and market orders alike)."""
        filled = 0.0
        notional = 0.0
        position_tickets = []
        try:
            deals = mt5.history_deals_get(ticket=ticket) or ()
            for deal in deals:
                if deal.order != ticket:
                    continue
                filled += deal.volume
                notional += deal.volume * deal.price
                if deal.position_id and deal.position_id not in position_tickets:
                    position_tickets.append(deal.position_id)
            still_open = bool(mt5.orders_get(ticket=ticket))
        except Exception as e:
            return {'ok': False, 'filled_volume': filled, 'price': None,
                    'position_tickets': position_tickets,
                    'still_open': False, 'error': str(e)}
        vwap = notional / filled if filled > 0 else None
        return {'ok': True, 'filled_volume': filled, 'price': vwap,
                'position_tickets': position_tickets,
                'still_open': still_open, 'error': None}

    def close_position_ticket(self, symbol, ticket, volume, entry_side,
                              slippage_points=1.0, comment=""):
        """Close a specific position by ticket. REQUIRED on hedging-mode
        accounts, where a plain opposite order would open a second
        position instead of closing this one."""
        try:
            tick = self.symbol_tick(symbol)
            info = self.symbol_info(symbol)
            if not tick or not info:
                return OrderResult(False, error=f"No market data for {symbol}")
            close_side = entry_side.opposite
            price = tick.ask if close_side is OrderSide.BUY else tick.bid
            request = {
                "action": mt5.TRADE_ACTION_DEAL,
                "symbol": symbol,
                "volume": volume,
                "type": (mt5.ORDER_TYPE_BUY if close_side is OrderSide.BUY
                         else mt5.ORDER_TYPE_SELL),
                "position": ticket,
                "price": price,
                "deviation": int(slippage_points / info.point),
                "magic": MAGIC_NUMBER,
                "comment": comment,
                "type_time": mt5.ORDER_TIME_GTC,
                "type_filling": mt5.ORDER_FILLING_IOC,
            }
            result = mt5.order_send(request)
            if result is None or result.retcode != mt5.TRADE_RETCODE_DONE:
                error = (mt5.last_error() if result is None
                         else f"{result.retcode} - {result.comment}")
                return OrderResult(False, requested_price=price,
                                   error=f"Close failed: {error}")
            return OrderResult(True, requested_price=price,
                               executed_price=result.price,
                               ticket=result.order,
                               volume=getattr(result, 'volume', volume))
        except Exception as e:
            return OrderResult(False, error=f"Close error: {e}")

    def send_market_order(self, symbol, side, volume,
                          slippage_points=1.0, comment=""):
        """Send an IOC market order; returns OrderResult."""
        try:
            info = self.ensure_symbol(symbol)
            if not info:
                return OrderResult(False, error=f"Symbol {symbol} not found")

            tick = self.symbol_tick(symbol)
            if not tick:
                return OrderResult(False, error=f"No tick data for {symbol}")

            price = tick.ask if side is OrderSide.BUY else tick.bid
            deviation = int(slippage_points / info.point)

            request = {
                "action": mt5.TRADE_ACTION_DEAL,
                "symbol": symbol,
                "volume": volume,
                "type": (mt5.ORDER_TYPE_BUY if side is OrderSide.BUY
                         else mt5.ORDER_TYPE_SELL),
                "price": price,
                "deviation": deviation,
                "magic": MAGIC_NUMBER,
                "comment": comment,
                "type_time": mt5.ORDER_TIME_GTC,
                "type_filling": mt5.ORDER_FILLING_IOC,
            }

            result = mt5.order_send(request)
            if result is None:
                return OrderResult(False, requested_price=price,
                                   error=f"order_send failed: {mt5.last_error()}")
            if result.retcode != mt5.TRADE_RETCODE_DONE:
                return OrderResult(
                    False, requested_price=price,
                    error=f"Order failed: {result.retcode} - {result.comment}")

            return OrderResult(True, requested_price=price,
                               executed_price=result.price,
                               ticket=result.order,
                               volume=getattr(result, 'volume', volume))

        except Exception as e:
            logging.error("Order exception on %s: %s", symbol, e)
            return OrderResult(False, error=f"Execution error: {e}")
