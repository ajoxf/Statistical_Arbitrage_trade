"""Order execution: paired entries and closes with hedge-leg protection.

Fixes the legacy bug where closing a position routed through the
entry path with SignalType.NO_SIGNAL and always raised ValueError —
live positions could never be closed. Closes now have their own path
(execute_close_pair) that derives directions from the position.
"""

import logging
from datetime import datetime

from .models import OrderSide, SignalType, Trade


class OrderManager:
    def __init__(self, config, broker):
        self.config = config
        self.broker = broker

    def execute_market_order(self, trade):
        result = self.broker.send_market_order(
            trade.symbol, trade.side, trade.lot_size,
            slippage_points=self.config.EXECUTION['SLIPPAGE_TOLERANCE'],
            comment=f"AlgoTrading_{trade.trade_id}",
        )

        trade.requested_price = result.requested_price
        if not result.success:
            trade.status = "ERROR"
            trade.error_message = result.error
            logging.error("Order failed: %s %s %.2f lots - %s",
                          trade.symbol, trade.side.value, trade.lot_size,
                          result.error)
            return False

        trade.order_ticket = result.ticket
        trade.executed_price = result.executed_price
        trade.status = "EXECUTED"
        trade.execution_time = datetime.now()
        logging.info("Order executed: %s %s %.2f lots at %s",
                     trade.symbol, trade.side.value, trade.lot_size,
                     trade.executed_price)
        return True

    def _execute_pair(self, spot_trade, futures_trade):
        """Execute spot then futures; unwind spot if the hedge leg fails."""
        if not self.execute_market_order(spot_trade):
            return False

        if not self.execute_market_order(futures_trade):
            logging.error("Hedge leg failed, reversing spot leg %s",
                          spot_trade.symbol)
            reverse = Trade(spot_trade.symbol, spot_trade.side.opposite,
                            spot_trade.lot_size)
            if not self.execute_market_order(reverse):
                logging.critical(
                    "UNHEDGED EXPOSURE: could not reverse spot leg %s %s "
                    "%.2f lots — manual intervention required",
                    spot_trade.symbol, spot_trade.side.value,
                    spot_trade.lot_size)
            return False

        return True

    def execute_trade_pair(self, asset, signal_type, lot_size,
                           spot_symbol, futures_symbol):
        """Open a basis position: simultaneous spot + futures entry."""
        if signal_type == SignalType.SELL_BASIS:
            spot_side, futures_side = OrderSide.BUY, OrderSide.SELL
        elif signal_type == SignalType.BUY_BASIS:
            spot_side, futures_side = OrderSide.SELL, OrderSide.BUY
        else:
            raise ValueError(f"Invalid signal type for opening: {signal_type}")

        spot_trade = Trade(spot_symbol, spot_side, lot_size)
        futures_trade = Trade(futures_symbol, futures_side, lot_size)

        success = self._execute_pair(spot_trade, futures_trade)
        if success:
            logging.info("Trade pair executed: %s %s", asset, signal_type.value)
        return success, spot_trade, futures_trade

    def execute_close_pair(self, position, reason=None):
        """Close a basis position by reversing both entry legs."""
        close_spot = Trade(position.spot_trade.symbol,
                           position.spot_trade.side.opposite,
                           position.spot_trade.lot_size)
        close_futures = Trade(position.futures_trade.symbol,
                              position.futures_trade.side.opposite,
                              position.futures_trade.lot_size)

        success = self._execute_pair(close_spot, close_futures)
        if success:
            logging.info("Close pair executed for %s", position.position_id)
        return success, close_spot, close_futures
