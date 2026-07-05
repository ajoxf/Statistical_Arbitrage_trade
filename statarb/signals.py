"""Signal generation from swap-premium analysis."""

from .models import SignalType


class SignalGenerator:
    def __init__(self, config):
        self.config = config

    def generate_signal(self, asset, market_data, active_positions):
        """Return a SignalType, or (position_id, SignalType) for exits."""
        swap_premium = market_data['swap_premium_pct']

        if not active_positions:
            if swap_premium > self.config.SIGNAL_THRESHOLDS['PREMIUM_ENTRY']:
                return SignalType.SELL_BASIS
            if swap_premium < self.config.SIGNAL_THRESHOLDS['DISCOUNT_ENTRY']:
                return SignalType.BUY_BASIS

        for position_id, position in active_positions.items():
            if position.signal_type == SignalType.SELL_BASIS:
                if swap_premium <= self.config.SIGNAL_THRESHOLDS['PREMIUM_EXIT']:
                    return (position_id, SignalType.CLOSE_LONG)
            elif position.signal_type == SignalType.BUY_BASIS:
                if swap_premium >= self.config.SIGNAL_THRESHOLDS['DISCOUNT_EXIT']:
                    return (position_id, SignalType.CLOSE_SHORT)

        return SignalType.NO_SIGNAL
