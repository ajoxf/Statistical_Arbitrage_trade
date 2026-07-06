"""Risk management and pre-trade validation."""

from collections import deque
from datetime import datetime

from .models import SignalType


class RiskManager:
    def __init__(self, config):
        self.config = config
        self.daily_trades = deque(maxlen=1000)   # (timestamp, asset, lots)
        self.last_signal_time = {}

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
