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
                            contract_size=1.0, contract_b=None):
        """Mark position to market, at the price each leg would CLOSE at.

        The two prices MUST be the exit-side touches — a long spot leg
        at the BID, a short futures leg at the ASK, and the mirror for
        BUY_BASIS. `marketdata.closing_prices` picks them; callers must
        not pass mids.

        Operator, 2026-08-25, on a short filled at 54.98 with the long
        spread at 55.27 and the card reading +$0.02: "How is the trade
        showing a profit if the price (Long Spread) is more than the BE
        Price?" It was not in profit. Marked at the two MIDS it read
        +$0.02; bought back where it actually would be, it books -$0.58.
        Both numbers are true about different things and only one of
        them is money, and this is the figure the dollar stop, the
        take-profit and the peak/trough distribution all act on — so it
        has to be the one you can take. It also makes our P&L agree with
        MT5's own, which marks each leg at its closing touch.

        The statistics are the opposite case and stay on the MID: z,
        sigma and `entry_mu` are one continuous series, and a series
        that flips definition with the direction under consideration is
        discontinuous.

        contract_size converts lots to units (e.g. 100 oz/lot for gold);
        the legacy code omitted it and understated P&L by that factor.
        `contract_b` is leg B's own contract size and defaults to leg
        A's — the last holdout of the one-multiplier rule, and the two
        differ the moment the legs are different instruments.
        """
        if position_id not in self.positions:
            return

        position = self.positions[position_id]
        position.current_premium = current_premium

        spot_units = position.spot_trade.lot_size * contract_size
        fut_units = position.futures_trade.lot_size * (
            contract_size if contract_b is None else contract_b)

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
                                contract_size=1.0, contract_b=None):
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
        fut_units = position.futures_trade.lot_size * (
            contract_size if contract_b is None else contract_b)

        if position.signal_type == SignalType.SELL_BASIS:
            # Long spot, short futures
            return (exit_spot - entry_spot) * spot_units \
                + (entry_fut - exit_fut) * fut_units
        return (entry_spot - exit_spot) * spot_units \
            + (exit_fut - entry_fut) * fut_units

    def close_position(self, position_id, close_reason, order_manager,
                       contract_size=1.0, reference=None, contract_b=None):
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
                    position, close_spot, close_futures, contract_size,
                    contract_b)
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
