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
        self.data_logger.save_position_state(position)

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

        # Lifecycle extremes with timing — tunes TP/gate/max-hold from
        # the measured peak distribution instead of opinion
        age_min = (datetime.now() - position.entry_time).total_seconds() / 60
        if position.peak_pnl is None \
                or position.unrealized_pnl > position.peak_pnl:
            position.peak_pnl = position.unrealized_pnl
            position.peak_min = age_min
        if position.trough_pnl is None \
                or position.unrealized_pnl < position.trough_pnl:
            position.trough_pnl = position.unrealized_pnl
            position.trough_min = age_min

        self.data_logger.log_position(position)

    @staticmethod
    def realized_pnl_from_fills(position, close_spot, close_futures,
                                contract_size=1.0):
        """Per-leg P&L from actual entry and exit fills — matches the
        broker to the cent. Falls back to the last mark when a close
        price is missing."""
        entry_spot = position.spot_trade.executed_price
        entry_fut = position.futures_trade.executed_price
        exit_spot = close_spot.executed_price if close_spot else None
        exit_fut = close_futures.executed_price if close_futures else None
        if None in (entry_spot, entry_fut, exit_spot, exit_fut):
            return position.unrealized_pnl

        spot_units = position.spot_trade.lot_size * contract_size
        fut_units = position.futures_trade.lot_size * contract_size

        if position.signal_type == SignalType.SELL_BASIS:
            # Long spot, short futures
            return (exit_spot - entry_spot) * spot_units \
                + (entry_fut - exit_fut) * fut_units
        return (entry_spot - exit_spot) * spot_units \
            + (exit_fut - entry_fut) * fut_units

    def close_position(self, position_id, close_reason, order_manager,
                       contract_size=1.0, reference=None):
        if position_id not in self.positions:
            return False

        position = self.positions[position_id]
        if position.status != PositionStatus.ACTIVE:
            return False

        position.status = PositionStatus.CLOSING
        self.data_logger.save_position_state(position)
        try:
            # `reference` is the market_data the EXIT decision was made
            # on. Passed through by keyword so the single-account
            # OrderManager, which does not measure slippage, still works.
            extra = {'reference': reference} if reference is not None else {}
            success, close_spot, close_futures = \
                order_manager.execute_close_pair(position,
                                                 reason=close_reason, **extra)

            if success:
                # Mark closed and persist ONLY after exit orders succeeded
                position.status = PositionStatus.CLOSED
                position.close_time = datetime.now()
                position.close_reason = close_reason
                position.realized_pnl = self.realized_pnl_from_fills(
                    position, close_spot, close_futures, contract_size)
                position.unrealized_pnl = 0.0
                position.exit_spot_price = close_spot.executed_price
                position.exit_fut_price = close_futures.executed_price
                position.exit_slippage = close_spot.slippage

                self.data_logger.log_trade(close_spot, position_id)
                self.data_logger.log_trade(close_futures, position_id)
                self.data_logger.log_position(position)
                self.data_logger.clear_position_state(position_id)

                logging.info("Position closed: %s - %s - P&L: $%.2f",
                             position_id, close_reason, position.realized_pnl)
                return True

            return self._close_failed(position, 'the broker rejected the '
                                                 'close')

        except Exception as e:
            return self._close_failed(position, str(e))

    def _close_failed(self, position, why):
        """A close that did not go through leaves the position OPEN at
        the broker, so it must stay ACTIVE here.

        It used to be marked ERROR, and every lookup in this class
        filters on ACTIVE — so the position vanished from the exit
        loop, from the health report and from the dashboard while a
        real position sat on the account. Live 2026-08-07: two legs
        failed to close with 10013, the engine logged CRITICAL and the
        very next status block read "exits -- flat". Nothing would ever
        have retried it.

        Staying ACTIVE means the exit ladder re-evaluates it on the
        next tick and tries again; the coordinator rate-limits the
        retries and escalates once they keep failing.
        """
        position.status = PositionStatus.ACTIVE
        position.close_failures = getattr(position, 'close_failures', 0) + 1
        position.last_close_error = why
        position.last_close_attempt = datetime.now()
        self.data_logger.save_position_state(position)
        logging.error(
            "Close FAILED for %s (attempt %d): %s — the position is still "
            "OPEN at the broker and stays under management",
            position.position_id, position.close_failures, why)
        return False

    def restore_position(self, position):
        """Re-attach a position recovered from the DB after a restart."""
        self.positions[position.position_id] = position
        try:
            number = int(position.position_id.split('_')[-1])
            self.position_counter = max(self.position_counter, number)
        except ValueError:
            pass
        logging.info("Recovered position %s from DB (%s %s, %.2f lots)",
                     position.position_id, position.asset,
                     position.signal_type.value,
                     position.spot_trade.lot_size)

    def get_active_positions(self):
        return {pid: p for pid, p in self.positions.items()
                if p.status == PositionStatus.ACTIVE}

    def get_positions_for_asset(self, asset):
        return {pid: p for pid, p in self.positions.items()
                if p.asset == asset and p.status == PositionStatus.ACTIVE}
