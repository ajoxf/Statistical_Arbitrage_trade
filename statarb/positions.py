"""Position lifecycle and P&L tracking."""

import logging
from datetime import datetime

from .models import Position, PositionStatus, SignalType


class PositionManager:
    def __init__(self, data_logger):
        self.positions = {}
        self.data_logger = data_logger
        self.position_counter = 0

    def create_position(self, asset, signal_type, spot_trade, futures_trade,
                        entry_premium):
        self.position_counter += 1
        position_id = f"POS_{self.position_counter:04d}"

        position = Position(position_id, asset, signal_type,
                            spot_trade, futures_trade)
        position.entry_premium = entry_premium
        position.current_premium = entry_premium
        self.positions[position_id] = position

        self.data_logger.log_position(position)
        self.data_logger.log_trade(spot_trade, position_id)
        self.data_logger.log_trade(futures_trade, position_id)

        logging.info("Position created: %s - %s %s at %.2f%%",
                     position_id, asset, signal_type.value, entry_premium)
        return position

    def update_position_pnl(self, position_id, current_spot_price,
                            current_futures_price, current_premium,
                            contract_size=1.0):
        """Mark position to market.

        contract_size converts lots to units (e.g. 100 oz/lot for gold);
        the legacy code omitted it and understated P&L by that factor.
        """
        if position_id not in self.positions:
            return

        position = self.positions[position_id]
        position.current_premium = current_premium

        spot_units = position.spot_trade.lot_size * contract_size
        fut_units = position.futures_trade.lot_size * contract_size

        if position.signal_type == SignalType.SELL_BASIS:
            # Long spot, short futures
            spot_pnl = (current_spot_price - position.spot_trade.executed_price) * spot_units
            futures_pnl = (position.futures_trade.executed_price - current_futures_price) * fut_units
        else:  # BUY_BASIS: short spot, long futures
            spot_pnl = (position.spot_trade.executed_price - current_spot_price) * spot_units
            futures_pnl = (current_futures_price - position.futures_trade.executed_price) * fut_units

        position.unrealized_pnl = spot_pnl + futures_pnl
        self.data_logger.log_position(position)

    def close_position(self, position_id, close_reason, order_manager):
        if position_id not in self.positions:
            return False

        position = self.positions[position_id]
        if position.status != PositionStatus.ACTIVE:
            return False

        position.status = PositionStatus.CLOSING
        try:
            success, close_spot, close_futures = \
                order_manager.execute_close_pair(position)

            if success:
                position.status = PositionStatus.CLOSED
                position.close_time = datetime.now()
                position.close_reason = close_reason
                position.realized_pnl = position.unrealized_pnl
                position.unrealized_pnl = 0.0

                self.data_logger.log_trade(close_spot, position_id)
                self.data_logger.log_trade(close_futures, position_id)
                self.data_logger.log_position(position)

                logging.info("Position closed: %s - %s - P&L: $%.2f",
                             position_id, close_reason, position.realized_pnl)
                return True

            position.status = PositionStatus.ERROR
            logging.error("Failed to close position: %s", position_id)
            return False

        except Exception as e:
            position.status = PositionStatus.ERROR
            logging.error("Error closing position %s: %s", position_id, e)
            return False

    def get_active_positions(self):
        return {pid: p for pid, p in self.positions.items()
                if p.status == PositionStatus.ACTIVE}

    def get_positions_for_asset(self, asset):
        return {pid: p for pid, p in self.positions.items()
                if p.asset == asset and p.status == PositionStatus.ACTIVE}
