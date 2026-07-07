"""Risk management, pre-trade validation and circuit breakers."""

import logging
from collections import deque
from datetime import datetime

from .models import SignalType


class RiskManager:
    def __init__(self, config):
        self.config = config
        self.daily_trades = deque(maxlen=1000)   # (timestamp, asset, lots)
        self.last_signal_time = {}
        # Circuit breakers
        self.consecutive_losses = 0
        self.daily_realized_pnl = 0.0
        self._breaker_date = datetime.now().date()

    # -- circuit breakers ------------------------------------------------

    def _roll_breaker_date(self):
        today = datetime.now().date()
        if today != self._breaker_date:
            self._breaker_date = today
            self.daily_realized_pnl = 0.0
            self.consecutive_losses = 0
            logging.info("Risk breakers reset for new day")

    def on_position_closed(self, pnl):
        """Feed every realized close (including untracked cleanups)."""
        self._roll_breaker_date()
        self.daily_realized_pnl += pnl
        if pnl < 0:
            self.consecutive_losses += 1
        else:
            self.consecutive_losses = 0

    def size_multiplier(self):
        """Streak reducer: cut size after consecutive losses."""
        self._roll_breaker_date()
        reduce_at = self.config.RISK_LIMITS.get('LOSS_STREAK_REDUCE', 3)
        cut = self.config.RISK_LIMITS.get('STREAK_SIZE_CUT', 0.2)
        if reduce_at and self.consecutive_losses >= reduce_at:
            return 1.0 - cut
        return 1.0

    def halted(self):
        """(halted, reason) — daily-loss breach or loss-streak pause."""
        self._roll_breaker_date()
        max_loss = self.config.RISK_LIMITS.get('DAILY_MAX_LOSS_USD', 0)
        if max_loss and self.daily_realized_pnl <= -max_loss:
            return True, (f"daily loss ${-self.daily_realized_pnl:.0f} >= "
                          f"limit ${max_loss:.0f}")
        pause_at = self.config.RISK_LIMITS.get('LOSS_STREAK_PAUSE', 6)
        if pause_at and self.consecutive_losses >= pause_at:
            return True, f"{self.consecutive_losses} consecutive losses"
        return False, None

    def lots_traded_today(self, asset):
        """Entry lots done today — progress toward DAILY_LOT_TARGET.

        The target is throughput to aim for, NOT a cap: nothing is
        rejected for exceeding it. Hard limits are MAX_DAILY_TRADES,
        MAX_LOT_SIZE and MAX_POSITIONS_PER_ASSET.
        """
        today = datetime.now().date()
        return sum(lots for ts, a, lots in self.daily_trades
                   if a == asset and ts.date() == today)

    def validate_new_position(self, asset, signal_type, lot_size,
                              position_manager):
        is_halted, why = self.halted()
        if is_halted:
            return False, f"Circuit breaker: {why}"

        active = position_manager.get_positions_for_asset(asset)
        if len(active) >= self.config.RISK_LIMITS['MAX_POSITIONS_PER_ASSET']:
            return False, f"Maximum positions reached for {asset}"

        if lot_size > self.config.RISK_LIMITS['MAX_LOT_SIZE']:
            return False, (f"Lot size {lot_size} exceeds maximum "
                           f"{self.config.RISK_LIMITS['MAX_LOT_SIZE']}")

        today = datetime.now().date()
        today_trades = [t for t in self.daily_trades if t[0].date() == today]
        if len(today_trades) >= self.config.RISK_LIMITS['MAX_DAILY_TRADES']:
            return False, "Daily trade limit reached"

        last_time = self.last_signal_time.get(asset, datetime.min)
        elapsed = (datetime.now() - last_time).total_seconds()
        if elapsed < self.config.EXECUTION['MIN_TIME_BETWEEN_SIGNALS']:
            return False, f"Too soon since last signal for {asset}"

        return True, "OK"

    def record_trade(self, asset, lots=0.0):
        self.daily_trades.append((datetime.now(), asset, lots))
        self.last_signal_time[asset] = datetime.now()

    def check_position_risk(self, position, current_premium):
        premium_change = current_premium - position.entry_premium

        if position.signal_type == SignalType.SELL_BASIS:
            # Loss when the premium widens further
            if premium_change > self.config.RISK_LIMITS['STOP_LOSS_PCT']:
                return True, "STOP_LOSS"
        else:  # BUY_BASIS: loss when the discount deepens
            if premium_change < -self.config.RISK_LIMITS['STOP_LOSS_PCT']:
                return True, "STOP_LOSS"

        return False, None
