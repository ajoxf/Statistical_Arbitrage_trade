"""
Signal Generator

Calculates trading signals based on:
- Z-score of the spread between spot and futures prices
- Hurst exponent for regime detection (mean-reverting vs trending)

Z-Score Formula:
    z = (spread - mean) / std

Hurst Exponent:
    H < 0.5: Mean-reverting (good for arbitrage)
    H = 0.5: Random walk
    H > 0.5: Trending (avoid trading)
"""

import numpy as np
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import List, Optional, Tuple, Dict, Any
from enum import Enum
import logging

logger = logging.getLogger(__name__)


class SignalType(Enum):
    """Trading signal types"""
    NONE = "NONE"
    ENTRY_LONG = "ENTRY_LONG"      # Spread too low, buy spread
    ENTRY_SHORT = "ENTRY_SHORT"    # Spread too high, sell spread
    EXIT = "EXIT"                   # Close position
    STOP_LOSS = "STOP_LOSS"        # Stop loss triggered


class MarketRegime(Enum):
    """Market regime based on Hurst exponent"""
    MEAN_REVERTING = "MEAN_REVERTING"  # H < 0.5
    RANDOM = "RANDOM"                    # H ≈ 0.5
    TRENDING = "TRENDING"                # H > 0.5


@dataclass
class SpreadData:
    """Spread calculation result"""
    timestamp: datetime
    spot_price: float
    futures_price: float
    spread: float
    mean: float
    std: float
    zscore: float


@dataclass
class Signal:
    """Trading signal with context"""
    signal_type: SignalType
    zscore: float
    spread: float
    mean: float
    std: float
    hurst: Optional[float] = None
    regime: Optional[MarketRegime] = None
    confidence: float = 1.0
    timestamp: datetime = None
    reason: str = ""

    def __post_init__(self):
        if self.timestamp is None:
            self.timestamp = datetime.now()

    @property
    def is_entry(self) -> bool:
        """Check if signal is an entry signal"""
        return self.signal_type in (SignalType.ENTRY_LONG, SignalType.ENTRY_SHORT)

    @property
    def is_exit(self) -> bool:
        """Check if signal is an exit signal"""
        return self.signal_type in (SignalType.EXIT, SignalType.STOP_LOSS)

    def to_dict(self) -> Dict[str, Any]:
        return {
            'signal_type': self.signal_type.value,
            'zscore': self.zscore,
            'spread': self.spread,
            'mean': self.mean,
            'std': self.std,
            'hurst': self.hurst,
            'regime': self.regime.value if self.regime else None,
            'confidence': self.confidence,
            'timestamp': self.timestamp.isoformat() if self.timestamp else None,
            'reason': self.reason
        }


class SignalGenerator:
    """
    Generates trading signals based on statistical analysis.

    Features:
    - Z-score calculation with configurable lookback
    - Hurst exponent regime filter
    - STD profitability filter (blocks trades when volatility too low)
    - SD touch tracking
    - Entry/exit signal generation
    """

    # SD levels for tracking
    SD_LEVELS = [2.0, 2.5, 3.0, 3.5, 4.0]

    def __init__(
        self,
        lookback_period: int = 90,
        lookback_unit: str = "minutes",
        entry_threshold: float = 2.0,
        exit_threshold: float = 0.5,
        stop_loss_threshold: float = 3.0,
        hurst_enabled: bool = True,
        hurst_threshold: float = 0.5,
        hurst_window: int = 100,
        # STD Profitability Filter parameters
        std_filter_enabled: bool = True,
        lot_size: float = 0.1,
        contract_size: float = 100.0,
        spot_spread_cost: float = 0.40,
        futures_spread_cost: float = 0.10,
        commission_per_lot: float = 7.0,
        swap_cost_per_day: float = 0.0,
        profit_margin: float = 1.5
    ):
        """
        Initialize signal generator.

        Args:
            lookback_period: Number of periods for mean/std calculation
            lookback_unit: 'minutes' or 'days'
            entry_threshold: Z-score threshold for entry (e.g., 2.0 = 2σ)
            exit_threshold: Z-score threshold for exit (e.g., 0.5 = 0.5σ)
            stop_loss_threshold: Z-score threshold for stop loss
            hurst_enabled: Enable Hurst exponent filter
            hurst_threshold: Hurst value below which to allow trading
            hurst_window: Window size for Hurst calculation
            std_filter_enabled: Enable STD profitability filter
            lot_size: Trading lot size
            contract_size: Contract size (e.g., 100 oz for gold)
            spot_spread_cost: Bid-ask spread for spot
            futures_spread_cost: Bid-ask spread for futures
            commission_per_lot: Commission per lot per side
            swap_cost_per_day: Daily swap/financing cost
            profit_margin: Required profit multiplier over costs
        """
        self.lookback_period = lookback_period
        self.lookback_unit = lookback_unit
        self.entry_threshold = entry_threshold
        self.exit_threshold = exit_threshold
        self.stop_loss_threshold = stop_loss_threshold
        self.hurst_enabled = hurst_enabled
        self.hurst_threshold = hurst_threshold
        self.hurst_window = hurst_window

        # STD Profitability Filter settings
        self.std_filter_enabled = std_filter_enabled
        self.lot_size = lot_size
        self.contract_size = contract_size
        self.spot_spread_cost = spot_spread_cost
        self.futures_spread_cost = futures_spread_cost
        self.commission_per_lot = commission_per_lot
        self.swap_cost_per_day = swap_cost_per_day
        self.profit_margin = profit_margin

        # Price history buffer
        self._spreads: List[float] = []
        self._timestamps: List[datetime] = []

        # Last calculated values
        self._last_mean: Optional[float] = None
        self._last_std: Optional[float] = None
        self._last_zscore: Optional[float] = None
        self._last_hurst: Optional[float] = None
        self._last_min_profitable_std: Optional[float] = None

        # SD touch tracking
        self._sd_touches: Dict[str, datetime] = {}

    def add_spread(self, spread: float, timestamp: Optional[datetime] = None) -> None:
        """
        Add new spread value to history.

        Args:
            spread: Spot price - Futures price
            timestamp: Time of observation
        """
        if timestamp is None:
            timestamp = datetime.now()

        self._spreads.append(spread)
        self._timestamps.append(timestamp)

        # Trim old data
        self._trim_history()

    def load_spreads(self, spreads: List[float], timestamps: List[datetime] = None) -> int:
        """
        Load historical spreads (e.g., from database on reconnect).

        This allows the signal generator to resume without waiting for
        the full lookback period after a disconnection.

        Args:
            spreads: List of historical spread values
            timestamps: List of corresponding timestamps (optional, will use
                       evenly spaced times within lookback window if not provided)

        Returns:
            Number of data points loaded (after trimming to lookback window)
        """
        if not spreads:
            return 0

        if timestamps is None:
            # Generate evenly spaced timestamps within lookback window
            now = datetime.now()
            if self.lookback_unit == "minutes":
                interval = timedelta(minutes=self.lookback_period / max(len(spreads), 1))
            else:
                interval = timedelta(days=self.lookback_period / max(len(spreads), 1))

            timestamps = [now - interval * (len(spreads) - i - 1) for i in range(len(spreads))]

        self._spreads = list(spreads)
        self._timestamps = list(timestamps)

        # Trim to keep only data within lookback window
        self._trim_history()

        logger.info(f"Loaded {len(self._spreads)} historical spreads for lookback recovery")
        return len(self._spreads)

    def get_spreads(self) -> Tuple[List[float], List[datetime]]:
        """
        Get current spread history (for persistence).

        Returns:
            Tuple of (spreads list, timestamps list)
        """
        return self._spreads.copy(), self._timestamps.copy()

    def _trim_history(self) -> None:
        """Remove data outside lookback window"""
        if not self._timestamps:
            return

        if self.lookback_unit == "minutes":
            cutoff = datetime.now() - timedelta(minutes=self.lookback_period)
        else:  # days
            cutoff = datetime.now() - timedelta(days=self.lookback_period)

        # Find first index within window
        idx = 0
        for i, ts in enumerate(self._timestamps):
            if ts >= cutoff:
                idx = i
                break
        else:
            idx = len(self._timestamps)

        if idx > 0:
            self._spreads = self._spreads[idx:]
            self._timestamps = self._timestamps[idx:]

    def calculate_statistics(self) -> Optional[Tuple[float, float]]:
        """
        Calculate mean and standard deviation of spreads.

        Returns:
            Tuple of (mean, std) or None if insufficient data
        """
        if len(self._spreads) < 10:  # Minimum data requirement
            logger.warning(f"Insufficient data for statistics: {len(self._spreads)} points")
            return None

        spreads = np.array(self._spreads)
        self._last_mean = float(np.mean(spreads))
        self._last_std = float(np.std(spreads, ddof=1))  # Sample std

        if self._last_std < 1e-10:
            logger.warning("Standard deviation too small")
            return None

        return self._last_mean, self._last_std

    def calculate_min_profitable_std(self, spot_spread: float = 0, futures_spread: float = 0) -> Dict[str, Any]:
        """
        Calculate minimum STD required for profitable trades.

        This filter ensures that the expected profit from mean reversion
        exceeds the total round-trip costs (spreads + commissions).

        Args:
            spot_spread: Current spot bid-ask spread
            futures_spread: Current futures bid-ask spread

        Returns:
            Dictionary with profitability analysis
        """
        # Use provided spreads or defaults
        spot_cost = spot_spread if spot_spread > 0 else self.spot_spread_cost
        futures_cost = futures_spread if futures_spread > 0 else self.futures_spread_cost

        # Calculate total round-trip costs
        spread_cost_per_unit = (spot_cost + futures_cost) * 2  # Entry + Exit
        total_commission = self.commission_per_lot * 4  # 4 legs (spot entry/exit + futures entry/exit)
        position_value = self.lot_size * self.contract_size

        # Total cost in dollars
        spread_cost_dollars = spread_cost_per_unit * position_value
        total_cost = spread_cost_dollars + total_commission + self.swap_cost_per_day

        # Expected move: entry at 2σ, exit at 0.5σ = 1.5σ move
        expected_sd_move = self.entry_threshold - self.exit_threshold

        # Minimum STD needed: total_cost / (expected_move * position_value) * profit_margin
        if expected_sd_move > 0 and position_value > 0:
            min_std = (total_cost * self.profit_margin) / (expected_sd_move * position_value)
        else:
            min_std = 0

        self._last_min_profitable_std = min_std

        # Calculate expected profit if current STD is available
        expected_profit = None
        is_profitable = False
        if self._last_std is not None and self._last_std > 0:
            expected_gross = expected_sd_move * self._last_std * position_value
            expected_profit = expected_gross - total_cost
            is_profitable = expected_profit > 0

        return {
            'min_profitable_std': min_std,
            'current_std': self._last_std,
            'is_profitable': is_profitable,
            'expected_profit': expected_profit,
            'total_round_trip_cost': total_cost,
            'spread_cost': spread_cost_dollars,
            'commission_cost': total_commission,
            'swap_cost': self.swap_cost_per_day,
            'expected_sd_move': expected_sd_move,
            'position_value': position_value,
            'profit_margin': self.profit_margin
        }

    def calculate_zscore(self, current_spread: float) -> Optional[float]:
        """
        Calculate Z-score for current spread.

        Args:
            current_spread: Current spread value

        Returns:
            Z-score or None if cannot calculate
        """
        stats = self.calculate_statistics()
        if stats is None:
            return None

        mean, std = stats
        self._last_zscore = (current_spread - mean) / std
        return self._last_zscore

    def calculate_hurst(self, min_window: int = 10) -> Optional[float]:
        """
        Calculate Hurst exponent using R/S analysis.

        The Hurst exponent indicates:
        - H < 0.5: Mean-reverting series (anti-persistent)
        - H = 0.5: Random walk (no memory)
        - H > 0.5: Trending series (persistent)

        Args:
            min_window: Minimum window size for R/S calculation

        Returns:
            Hurst exponent or None if cannot calculate
        """
        spreads = np.array(self._spreads[-self.hurst_window:])

        if len(spreads) < min_window * 2:
            return None

        try:
            # R/S Analysis
            n = len(spreads)
            max_k = n // 2
            rs_values = []
            ns = []

            for k in range(min_window, max_k + 1):
                # Number of sub-series
                num_series = n // k
                rs_list = []

                for i in range(num_series):
                    start = i * k
                    end = start + k
                    sub_series = spreads[start:end]

                    # Mean-adjusted series
                    mean_adj = sub_series - np.mean(sub_series)

                    # Cumulative deviation
                    cumsum = np.cumsum(mean_adj)

                    # Range
                    R = np.max(cumsum) - np.min(cumsum)

                    # Standard deviation
                    S = np.std(sub_series, ddof=1)

                    if S > 1e-10:
                        rs_list.append(R / S)

                if rs_list:
                    rs_values.append(np.mean(rs_list))
                    ns.append(k)

            if len(ns) < 3:
                return None

            # Linear regression on log-log scale
            log_n = np.log(ns)
            log_rs = np.log(rs_values)

            # Fit: log(R/S) = H * log(n) + c
            coeffs = np.polyfit(log_n, log_rs, 1)
            hurst = coeffs[0]

            # Clamp to valid range
            self._last_hurst = max(0.0, min(1.0, hurst))
            return self._last_hurst

        except Exception as e:
            logger.error(f"Hurst calculation error: {e}")
            return None

    def get_regime(self) -> MarketRegime:
        """
        Determine market regime based on Hurst exponent.

        Returns:
            MarketRegime enum value
        """
        if self._last_hurst is None:
            return MarketRegime.RANDOM

        if self._last_hurst < 0.45:
            return MarketRegime.MEAN_REVERTING
        elif self._last_hurst > 0.55:
            return MarketRegime.TRENDING
        else:
            return MarketRegime.RANDOM

    def generate_entry_signal(
        self,
        current_spread: float,
        has_position: bool = False,
        spot_spread: float = 0,
        futures_spread: float = 0
    ) -> Signal:
        """
        Generate entry signal based on current market conditions.

        Args:
            current_spread: Current spread value
            has_position: Whether already in a position
            spot_spread: Current spot bid-ask spread (for STD filter)
            futures_spread: Current futures bid-ask spread (for STD filter)

        Returns:
            Signal object
        """
        # Calculate Z-score
        zscore = self.calculate_zscore(current_spread)
        if zscore is None:
            return Signal(
                signal_type=SignalType.NONE,
                zscore=0,
                spread=current_spread,
                mean=0,
                std=0,
                reason="Insufficient data"
            )

        # Calculate Hurst if enabled
        hurst = None
        regime = None
        if self.hurst_enabled:
            hurst = self.calculate_hurst()
            regime = self.get_regime()

            # Block entries in trending regime
            if regime == MarketRegime.TRENDING and hurst and hurst > self.hurst_threshold:
                return Signal(
                    signal_type=SignalType.NONE,
                    zscore=zscore,
                    spread=current_spread,
                    mean=self._last_mean,
                    std=self._last_std,
                    hurst=hurst,
                    regime=regime,
                    reason=f"Trending regime (H={hurst:.3f})"
                )

        # STD Profitability Filter - block trades when volatility too low
        if self.std_filter_enabled and self._last_std is not None:
            profitability = self.calculate_min_profitable_std(spot_spread, futures_spread)
            if not profitability['is_profitable']:
                min_std = profitability['min_profitable_std']
                current_std = profitability['current_std']
                return Signal(
                    signal_type=SignalType.NONE,
                    zscore=zscore,
                    spread=current_spread,
                    mean=self._last_mean,
                    std=self._last_std,
                    hurst=hurst,
                    regime=regime,
                    reason=f"STD too low: ${current_std:.2f} < ${min_std:.2f} required"
                )

        # Check for entry signals
        if has_position:
            signal_type = SignalType.NONE
            reason = "Already in position"
        elif zscore <= -self.entry_threshold:
            signal_type = SignalType.ENTRY_LONG
            reason = f"Z-score {zscore:.2f} <= -{self.entry_threshold}"
        elif zscore >= self.entry_threshold:
            signal_type = SignalType.ENTRY_SHORT
            reason = f"Z-score {zscore:.2f} >= {self.entry_threshold}"
        else:
            signal_type = SignalType.NONE
            reason = f"Z-score {zscore:.2f} within thresholds"

        # Track SD touches
        self._track_sd_touch(zscore)

        return Signal(
            signal_type=signal_type,
            zscore=zscore,
            spread=current_spread,
            mean=self._last_mean,
            std=self._last_std,
            hurst=hurst,
            regime=regime,
            reason=reason
        )

    def generate_exit_signal(
        self,
        current_spread: float,
        entry_zscore: float,
        position_direction: str,  # 'LONG' or 'SHORT'
        entry_time: Optional[datetime] = None,
        current_pnl: Optional[float] = None,
        min_profit: Optional[float] = None,
        max_loss: Optional[float] = None,
        time_stop_days: Optional[float] = None,
        exit_at_opposite_sd: Optional[float] = None
    ) -> Signal:
        """
        Generate exit signal for an open position.

        Args:
            current_spread: Current spread value
            entry_zscore: Z-score at entry
            position_direction: 'LONG' or 'SHORT'
            entry_time: Position entry time
            current_pnl: Current unrealized P&L
            min_profit: Minimum profit target
            max_loss: Maximum loss threshold
            time_stop_days: Maximum holding time in days
            exit_at_opposite_sd: Exit at opposite SD level

        Returns:
            Signal object
        """
        zscore = self.calculate_zscore(current_spread)
        if zscore is None:
            return Signal(
                signal_type=SignalType.NONE,
                zscore=0,
                spread=current_spread,
                mean=0,
                std=0,
                reason="Cannot calculate Z-score"
            )

        signal_type = SignalType.NONE
        reason = ""

        # Check stop loss
        if position_direction == 'LONG' and zscore <= -self.stop_loss_threshold:
            signal_type = SignalType.STOP_LOSS
            reason = f"Stop loss: Z-score {zscore:.2f} <= -{self.stop_loss_threshold}"
        elif position_direction == 'SHORT' and zscore >= self.stop_loss_threshold:
            signal_type = SignalType.STOP_LOSS
            reason = f"Stop loss: Z-score {zscore:.2f} >= {self.stop_loss_threshold}"

        # Check mean reversion exit
        elif position_direction == 'LONG' and zscore >= -self.exit_threshold:
            signal_type = SignalType.EXIT
            reason = f"Mean reversion: Z-score {zscore:.2f} >= -{self.exit_threshold}"
        elif position_direction == 'SHORT' and zscore <= self.exit_threshold:
            signal_type = SignalType.EXIT
            reason = f"Mean reversion: Z-score {zscore:.2f} <= {self.exit_threshold}"

        # Check opposite SD exit
        if exit_at_opposite_sd and exit_at_opposite_sd > 0:
            if position_direction == 'LONG' and zscore >= exit_at_opposite_sd:
                signal_type = SignalType.EXIT
                reason = f"Opposite SD exit: Z-score {zscore:.2f} >= {exit_at_opposite_sd}"
            elif position_direction == 'SHORT' and zscore <= -exit_at_opposite_sd:
                signal_type = SignalType.EXIT
                reason = f"Opposite SD exit: Z-score {zscore:.2f} <= -{exit_at_opposite_sd}"

        # Check P&L stops
        if current_pnl is not None:
            if max_loss and current_pnl <= -max_loss:
                signal_type = SignalType.STOP_LOSS
                reason = f"Max loss: P&L ${current_pnl:.2f} <= -${max_loss:.2f}"
            elif min_profit and current_pnl >= min_profit:
                signal_type = SignalType.EXIT
                reason = f"Profit target: P&L ${current_pnl:.2f} >= ${min_profit:.2f}"

        # Check time stop
        if entry_time and time_stop_days and time_stop_days > 0:
            days_held = (datetime.now() - entry_time).total_seconds() / 86400
            if days_held >= time_stop_days:
                signal_type = SignalType.EXIT
                reason = f"Time stop: {days_held:.1f} days >= {time_stop_days} days"

        return Signal(
            signal_type=signal_type,
            zscore=zscore,
            spread=current_spread,
            mean=self._last_mean,
            std=self._last_std,
            hurst=self._last_hurst,
            regime=self.get_regime(),
            reason=reason
        )

    def _track_sd_touch(self, zscore: float) -> None:
        """Track when spread touches SD levels"""
        abs_z = abs(zscore)
        direction = "HIGH" if zscore > 0 else "LOW"

        for level in self.SD_LEVELS:
            if abs_z >= level:
                key = f"{level}_{direction}"
                if key not in self._sd_touches:
                    self._sd_touches[key] = datetime.now()
                    logger.info(f"SD Touch: {level}σ {direction} at Z={zscore:.2f}")

    def get_sd_touch_info(self, sd_level: float, direction: str) -> Optional[Dict[str, Any]]:
        """Get information about SD touch"""
        key = f"{sd_level}_{direction}"
        if key in self._sd_touches:
            touch_time = self._sd_touches[key]
            return {
                'sd_level': sd_level,
                'direction': direction,
                'touch_time': touch_time,
                'time_since_touch': (datetime.now() - touch_time).total_seconds()
            }
        return None

    def clear_sd_touches(self) -> None:
        """Clear SD touch tracking"""
        self._sd_touches.clear()

    @property
    def data_points(self) -> int:
        """Number of data points in history"""
        return len(self._spreads)

    @property
    def last_zscore(self) -> Optional[float]:
        """Last calculated Z-score"""
        return self._last_zscore

    @property
    def last_mean(self) -> Optional[float]:
        """Last calculated mean"""
        return self._last_mean

    @property
    def last_std(self) -> Optional[float]:
        """Last calculated standard deviation"""
        return self._last_std

    @property
    def last_hurst(self) -> Optional[float]:
        """Last calculated Hurst exponent"""
        return self._last_hurst

    def get_statistics(self) -> Dict[str, Any]:
        """Get current statistics summary"""
        stats = {
            'data_points': self.data_points,
            'mean': self._last_mean,
            'std': self._last_std,
            'zscore': self._last_zscore,
            'hurst': self._last_hurst,
            'regime': self.get_regime().value,
            'lookback_period': self.lookback_period,
            'lookback_unit': self.lookback_unit,
            'entry_threshold': self.entry_threshold,
            'exit_threshold': self.exit_threshold,
            'stop_loss_threshold': self.stop_loss_threshold,
            'std_filter_enabled': self.std_filter_enabled,
            'min_profitable_std': self._last_min_profitable_std
        }

        # Add profitability analysis if STD filter is enabled
        if self.std_filter_enabled and self._last_std is not None:
            profitability = self.calculate_min_profitable_std()
            stats['profitability'] = profitability

        return stats
