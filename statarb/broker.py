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
                 ticket=None, error=None):
        self.success = success
        self.requested_price = requested_price
        self.executed_price = executed_price
        self.ticket = ticket
        self.error = error


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
                               executed_price=result.price, ticket=result.order)

        except Exception as e:
            logging.error("Order exception on %s: %s", symbol, e)
            return OrderResult(False, error=f"Execution error: {e}")
