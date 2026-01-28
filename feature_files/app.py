"""
Flask Web Application

Provides the web UI for the Multi-Broker Arbitrage System.

Pages:
- Dashboard: Real-time monitoring
- Settings: Trading parameters configuration
- Setup: Broker configuration
- SD Analysis: Standard deviation touch analysis
"""

# IMPORTANT: eventlet/gevent monkey_patch MUST be called before ANY other imports
# to avoid "Working outside of application context" errors with Flask/Werkzeug
import sys
import os

def _setup_async_mode():
    """Setup async mode by monkey patching BEFORE other imports."""
    # Skip eventlet on Windows - it has compatibility issues with Python 3.10+
    # and causes the server to not bind properly
    if sys.platform == 'win32':
        return 'threading'

    # Also allow forcing threading mode via environment variable
    if os.environ.get('FLASK_ASYNC_MODE') == 'threading':
        return 'threading'

    try:
        import eventlet
        eventlet.monkey_patch()
        return 'eventlet'
    except (ImportError, Exception):
        pass
    try:
        import gevent
        from gevent import monkey
        monkey.patch_all()
        return 'gevent'
    except (ImportError, Exception):
        pass
    return 'threading'

_async_mode = _setup_async_mode()

import asyncio
import threading
import json
import os
import math
from datetime import datetime
from functools import wraps
from typing import Optional, Tuple
import logging
from pathlib import Path

# Load .env file for environment variables
try:
    from dotenv import load_dotenv
    env_file = Path(__file__).parent / '.env'
    if env_file.exists():
        load_dotenv(env_file)
        logging.getLogger(__name__).info(f"Loaded environment from {env_file}")
except ImportError:
    pass  # python-dotenv not installed

from flask import Flask, render_template, request, jsonify, redirect, url_for
from flask_socketio import SocketIO, emit

import sys
sys.path.insert(0, str(Path(__file__).parent.parent))

from database.manager import DatabaseManager
from database.models import TradingConfig, Broker, Trade
from core.trading_engine import TradingEngine, EngineState
from core.multi_broker import (
    MultiBrokerCoordinator,
    ExecutionMode,
    create_coordinator_from_database
)

# Active broker config file (workaround for database not saving new fields)
ACTIVE_BROKER_FILE = Path(__file__).parent.parent / "active_brokers.json"


def save_active_brokers(spot_id, futures_id):
    """Save active broker IDs to JSON file"""
    data = {
        'active_spot_broker': spot_id,
        'active_futures_broker': futures_id
    }
    with open(ACTIVE_BROKER_FILE, 'w') as f:
        json.dump(data, f)
    logging.getLogger(__name__).info(f"[BROKERS] Saved active brokers to file: {data}")


def load_active_brokers():
    """Load active broker IDs from JSON file"""
    if ACTIVE_BROKER_FILE.exists():
        try:
            with open(ACTIVE_BROKER_FILE, 'r') as f:
                data = json.load(f)
                return data.get('active_spot_broker'), data.get('active_futures_broker')
        except Exception as e:
            logging.getLogger(__name__).error(f"[BROKERS] Error loading active brokers: {e}")
    return None, None


def calculate_swap_basis(spot_price: float, swap_charge: float, lot_size: float,
                         time_to_expiry: float) -> Tuple[float, float, float]:
    """
    Calculate swap-based fair value basis.

    Args:
        spot_price: Current spot price
        swap_charge: Daily swap cost in dollars per lot
        lot_size: Contract size (units per lot)
        time_to_expiry: Time to expiry in years (fractional)

    Returns:
        Tuple of (swap_futures_price, swap_basis, annual_swap_rate)
    """
    if swap_charge <= 0 or time_to_expiry <= 0:
        return spot_price, 0.0, 0.0

    position_value = spot_price * lot_size
    if position_value <= 0:
        return spot_price, 0.0, 0.0

    daily_swap_rate = swap_charge / position_value
    annual_swap_rate = daily_swap_rate * 365

    swap_futures_price = spot_price * math.exp(annual_swap_rate * time_to_expiry)
    swap_basis = swap_futures_price - spot_price

    return swap_futures_price, swap_basis, annual_swap_rate


def calculate_hurst_exponent(spread_history: list, min_points: int = 20) -> tuple:
    """
    Calculate Hurst Exponent using R/S (Rescaled Range) method.

    H < 0.4: Mean-reverting (anti-persistent) - GOOD for mean reversion strategy
    H = 0.5: Random walk (Brownian motion) - No edge
    H > 0.6: Trending (persistent) - BAD for mean reversion, good for momentum

    Args:
        spread_history: List of spread values
        min_points: Minimum data points required

    Returns: (hurst_value, regime_label)
    """
    import numpy as np

    if len(spread_history) < min_points:
        return None, 'INSUFFICIENT_DATA'

    # Use last N points (more recent data is more relevant)
    max_points = min(100, len(spread_history))
    ts = np.array(spread_history[-max_points:])

    n = len(ts)
    if n < min_points:
        return None, 'INSUFFICIENT_DATA'

    # Calculate returns/differences
    diffs = np.diff(ts)
    if len(diffs) == 0:
        return None, 'INSUFFICIENT_DATA'

    # Range of chunk sizes to test (must be at least 10)
    max_k = n // 2
    min_k = max(10, n // 10)

    if max_k <= min_k:
        return None, 'INSUFFICIENT_DATA'

    # Calculate R/S for different chunk sizes
    rs_values = []
    chunk_sizes = []

    for k in range(min_k, max_k + 1, max(1, (max_k - min_k) // 10)):
        num_chunks = n // k
        if num_chunks < 1:
            continue

        rs_list = []
        for i in range(num_chunks):
            chunk = ts[i*k:(i+1)*k]
            if len(chunk) < 2:
                continue

            # Mean-adjusted cumulative deviations
            mean_chunk = np.mean(chunk)
            deviations = chunk - mean_chunk
            cumsum = np.cumsum(deviations)

            # Range
            R = np.max(cumsum) - np.min(cumsum)

            # Standard deviation
            S = np.std(chunk, ddof=1)

            if S > 0:
                rs_list.append(R / S)

        if rs_list:
            rs_values.append(np.mean(rs_list))
            chunk_sizes.append(k)

    if len(rs_values) < 3:
        return None, 'INSUFFICIENT_DATA'

    # Linear regression on log-log scale to get Hurst exponent
    log_n = np.log(chunk_sizes)
    log_rs = np.log(rs_values)

    # Simple linear regression: H = slope
    slope, _ = np.polyfit(log_n, log_rs, 1)
    hurst = slope

    # Clamp to reasonable range [0, 1]
    hurst = max(0.0, min(1.0, hurst))

    # Determine regime
    if hurst < 0.4:
        regime = 'MEAN_REVERTING'
    elif hurst < 0.6:
        regime = 'RANDOM_WALK'
    else:
        regime = 'TRENDING'

    return round(hurst, 3), regime


def calculate_min_profitable_std(config, current_std: float,
                                  spot_spread_cents: float = None,
                                  futures_spread_cents: float = None) -> dict:
    """
    Calculate the minimum STD required for a trade to be profitable.

    Formula:
        Min_STD = (Round-trip Costs + Min_Profit) / (Entry_Z - Exit_Z) × Lot_Size × Contract_Size

    Args:
        config: TradingConfig object
        current_std: Current standard deviation of spread
        spot_spread_cents: Current spot bid-ask spread in cents (optional)
        futures_spread_cents: Current futures bid-ask spread in cents (optional)

    Returns: dict with min_std, is_profitable, round_trip_cost, expected_profit, etc.
    """
    # Get config values
    entry_z = config.entry_std_dev if config else 2.0
    exit_z = config.exit_std_dev if config else 0.5
    exit_opposite_z = config.exit_at_opposite_sd if config else 0.0
    lot_size = config.lot_size if config else 0.1
    contract_size = config.contract_size if config else 100.0
    min_profit_per_lot = config.min_profit_per_lot if config else 50.0

    # Use configured spread costs if real-time not available
    if spot_spread_cents is None:
        spot_spread_cents = (config.spot_spread_cost if config else 0.40) * 100
    if futures_spread_cents is None:
        futures_spread_cents = (config.futures_spread_cost if config else 0.10) * 100

    # Expected Z-score move from entry to exit
    # If exit_at_opposite_sd is set, we exit on the OTHER side of mean (full mean reversion)
    # e.g., Entry at +3.5σ, exit at -2.0σ = 5.5σ total move
    if exit_opposite_z > 0:
        z_move = entry_z + exit_opposite_z  # Full cross-mean move
    else:
        z_move = entry_z - exit_z  # Normal exit near mean (e.g., 2.0 - 0.5 = 1.5σ)

    # Calculate round-trip costs from bid-ask spreads
    # Entry cost = (spot_spread + futures_spread) × lot_size × contract_size
    entry_cost = ((spot_spread_cents + futures_spread_cents) / 100) * lot_size * contract_size
    round_trip_cost = entry_cost * 2  # Entry + exit

    # Min profit for this trade
    min_profit = min_profit_per_lot * lot_size

    # Total amount the spread move must generate
    total_required = round_trip_cost + min_profit

    # Calculate minimum STD
    denominator = z_move * lot_size * contract_size
    if denominator > 0:
        min_std = total_required / denominator
    else:
        min_std = float('inf')

    # Determine if trading is profitable
    is_profitable = bool(current_std >= min_std) if current_std > 0 else False

    # Calculate expected profit if trade is successful
    if current_std > 0:
        profit_if_successful = (current_std * z_move * lot_size * contract_size) - round_trip_cost
    else:
        profit_if_successful = 0.0

    return {
        'min_std': float(min_std),
        'current_std': float(current_std),
        'is_profitable': is_profitable,
        'round_trip_cost': float(round_trip_cost),
        'min_profit': float(min_profit),
        'total_required': float(total_required),
        'z_move': float(z_move),
        'lot_size': float(lot_size),
        'contract_size': float(contract_size),
        'profit_if_successful': float(profit_if_successful),
        'std_deficit': float(max(0, min_std - current_std)),
        'std_ratio': float(current_std / min_std) if min_std > 0 else 0.0,
        # Debug values to verify config is being read correctly
        'entry_z': float(entry_z),
        'exit_z': float(exit_z),
        'exit_opposite_z': float(exit_opposite_z)
    }


def calculate_entry_exit_bands(mean: float, std: float, config) -> dict:
    """
    Calculate entry and exit price bands based on Z-score thresholds.

    Args:
        mean: Mean of spread over lookback period
        std: Standard deviation of spread
        config: TradingConfig object

    Returns: dict with entry/exit levels for short and long spreads
    """
    entry_std = config.entry_std_dev if config else 2.0
    exit_std = config.exit_std_dev if config else 0.5
    stop_std = config.stop_loss_std_dev if config else 3.0

    return {
        # Short Spread: Enter when spread is HIGH, exit when it falls
        'short_entry': round(mean + (entry_std * std), 2),   # Entry ↑
        'short_exit': round(mean + (exit_std * std), 2),     # Exit (profit target)
        'short_stop': round(mean + (stop_std * std), 2),     # Stop loss

        # Long Spread: Enter when spread is LOW, exit when it rises
        'long_entry': round(mean - (entry_std * std), 2),    # Entry ↓
        'long_exit': round(mean - (exit_std * std), 2),      # Exit (profit target)
        'long_stop': round(mean - (stop_std * std), 2),      # Stop loss

        # Band values for display
        'entry_std': entry_std,
        'exit_std': exit_std,
        'stop_std': stop_std
    }


def calculate_margin_requirements(spot_price: float, futures_price: float,
                                  contract_size: float, leverage: int,
                                  user_lot_size: float) -> dict:
    """
    Calculate margin requirements for spread trade.

    Args:
        spot_price: Current spot price
        futures_price: Current futures price
        contract_size: Units per lot
        leverage: Account leverage (e.g., 100 for 1:100)
        user_lot_size: User's configured lot size

    Returns:
        Dictionary with margin calculations
    """
    if leverage <= 0:
        leverage = 100  # Default

    # Margin per lot (Spot) = (Price × Contract Size) / Leverage
    margin_per_lot_spot = (spot_price * contract_size) / leverage

    # Margin per lot (Futures) - similar calculation
    margin_per_lot_futures = (futures_price * contract_size) / leverage

    # Total margin per lot (both legs of spread trade)
    margin_per_lot_total = margin_per_lot_spot + margin_per_lot_futures

    # Margin required for current position size
    margin_required = margin_per_lot_total * user_lot_size

    # Margin with 15% buffer for price fluctuation
    margin_with_buffer = margin_required * 1.15

    return {
        'leverage': leverage,
        'margin_per_lot_spot': round(margin_per_lot_spot, 2),
        'margin_per_lot_futures': round(margin_per_lot_futures, 2),
        'margin_per_lot_total': round(margin_per_lot_total, 2),
        'margin_required': round(margin_required, 2),
        'margin_with_buffer': round(margin_with_buffer, 2),
        'user_lot_size': user_lot_size
    }


def parse_futures_expiry(expiry_str: Optional[str]) -> Tuple[Optional[datetime], float]:
    """
    Parse futures expiry date string and calculate days to expiry.

    Args:
        expiry_str: Expiry date as string (YYYY-MM-DD format)

    Returns:
        Tuple of (expiry_datetime, days_to_expiry)
    """
    if not expiry_str:
        return None, 0.0

    try:
        expiry_date = datetime.strptime(expiry_str, '%Y-%m-%d')
        current_time = datetime.now()
        time_delta = expiry_date - current_time
        days_to_expiry = max(0, time_delta.total_seconds() / (24 * 3600))
        return expiry_date, days_to_expiry
    except (ValueError, TypeError):
        return None, 0.0


logger = logging.getLogger(__name__)

# Flask app
app = Flask(__name__)
app.config['SECRET_KEY'] = 'multi-broker-arb-secret-key'

# SocketIO for real-time updates
# Uses eventlet/gevent for WebSocket support if available, otherwise falls back to threading (polling)
socketio = SocketIO(app, cors_allowed_origins="*", async_mode=_async_mode, logger=False, engineio_logger=False)
logger.info(f"SocketIO initialized with async_mode='{_async_mode}'")

# Global instances
db: Optional[DatabaseManager] = None
engine: Optional[TradingEngine] = None
engine_loop: Optional[asyncio.AbstractEventLoop] = None
multi_broker: Optional[MultiBrokerCoordinator] = None
auto_trader: Optional['AutoTrader'] = None


class AutoTrader:
    """
    Automatic trade execution handler.

    Receives signals from the trading engine and executes trades
    when all conditions are met (filters pass, algo enabled, etc.)
    """

    def __init__(self, db_path: str = "trading.db"):
        self.db_path = db_path
        self._position_open = False
        self._position_direction: Optional[str] = None  # 'LONG' or 'SHORT'
        self._entry_trade_id: Optional[str] = None
        self._entry_spot_price: Optional[float] = None
        self._entry_futures_price: Optional[float] = None
        self._entry_zscore: Optional[float] = None
        self._entry_time: Optional[datetime] = None
        # MT5 position tickets for closing positions
        self._spot_ticket: Optional[int] = None
        self._futures_ticket: Optional[int] = None
        self._logger = logging.getLogger(__name__)

        # Load any existing open position from database
        self._load_open_position()

    def _load_open_position(self):
        """Load open position from database on startup"""
        try:
            database = DatabaseManager(self.db_path)
            # Check for open trades
            conn = database._get_connection()
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM trades WHERE status = 'OPEN' ORDER BY entry_date DESC LIMIT 1")
            row = cursor.fetchone()
            if row:
                self._position_open = True
                self._position_direction = 'LONG' if row['direction'] == 'Long Spread' else 'SHORT'
                self._entry_trade_id = row['trade_id']
                self._entry_spot_price = row['entry_spot_price']
                self._entry_futures_price = row['entry_futures_price']
                self._entry_zscore = row['entry_zscore']
                self._entry_time = datetime.fromisoformat(row['entry_date']) if row['entry_date'] else None
                # Load MT5 tickets for closing positions
                self._spot_ticket = row['mt5_spot_ticket'] if 'mt5_spot_ticket' in row.keys() else None
                self._futures_ticket = row['mt5_futures_ticket'] if 'mt5_futures_ticket' in row.keys() else None
                self._logger.info(f"[AUTO] Loaded open position: {self._position_direction} (ID: {self._entry_trade_id}, spot_ticket={self._spot_ticket}, futures_ticket={self._futures_ticket})")
        except Exception as e:
            self._logger.error(f"[AUTO] Error loading open position: {e}")

    def _log_std_filter_event(self, database, zscore, signal_type, current_std,
                               min_required_std, std_ratio, is_profitable,
                               spot_spread_cents=None, futures_spread_cents=None,
                               round_trip_cost=None, action_taken=None,
                               trade_id=None, blocked_reason=None):
        """Log STD filter event to database AND emit via SocketIO for real-time updates"""
        try:
            # Log to database
            database.log_std_filter_event(
                zscore=zscore, signal_type=signal_type, current_std=current_std,
                min_required_std=min_required_std, std_ratio=std_ratio, is_profitable=is_profitable,
                spot_spread_cents=spot_spread_cents, futures_spread_cents=futures_spread_cents,
                round_trip_cost=round_trip_cost, action_taken=action_taken,
                trade_id=trade_id, blocked_reason=blocked_reason
            )

            # Emit via SocketIO for real-time UI updates
            socketio.emit('std_filter_event', {
                'timestamp': datetime.now().isoformat(),
                'zscore': zscore,
                'signal_type': signal_type,
                'current_std': current_std,
                'min_required_std': min_required_std,
                'std_ratio': std_ratio,
                'is_profitable': is_profitable,
                'spot_spread_cents': spot_spread_cents,
                'futures_spread_cents': futures_spread_cents,
                'round_trip_cost': round_trip_cost,
                'action_taken': action_taken,
                'trade_id': trade_id,
                'blocked_reason': blocked_reason
            })

            self._logger.info(f"[AUTO] STD filter event logged: {signal_type}, action={action_taken}")

        except Exception as e:
            self._logger.error(f"[AUTO] FAILED to log STD filter event: {e}")
            # Re-raise to alert caller that logging failed
            raise

    def handle_signal(self, signal):
        """
        Handle trading signal from the engine.

        This is called for every signal generated. It checks all conditions
        and executes trades when appropriate.
        """
        self._logger.info(f"[AUTO] >>> handle_signal CALLED with {signal.signal_type} <<<")
        try:
            database = DatabaseManager(self.db_path)
            config = database.get_config()

            if not config:
                self._logger.warning("[AUTO] No config found - ABORTING")
                return

            # Check if algo trading is enabled
            if not config.algo_enabled:
                self._logger.info("[AUTO] Algo trading disabled - ABORTING")
                return

            signal_type = signal.signal_type
            zscore = signal.zscore

            self._logger.info(f"[AUTO] Processing signal: {signal_type} (z={zscore:.2f}) - algo_enabled={config.algo_enabled}")

            # Get current market prices for execution
            spot_broker_id, futures_broker_id = load_active_brokers()
            self._logger.info(f"[AUTO] Active brokers: spot={spot_broker_id}, futures={futures_broker_id}")
            if not spot_broker_id or not futures_broker_id:
                self._logger.warning("[AUTO] No active brokers configured - ABORTING")
                return

            spot_broker = database.get_broker(spot_broker_id)
            futures_broker = database.get_broker(futures_broker_id)

            if not spot_broker or not futures_broker:
                self._logger.warning(f"[AUTO] Broker config not found: spot={spot_broker}, futures={futures_broker} - ABORTING")
                return

            self._logger.info(f"[AUTO] Brokers loaded: spot={spot_broker.symbol}, futures={futures_broker.symbol}")

            # Get current prices
            spot_price, futures_price = self._get_current_prices(spot_broker, futures_broker)
            self._logger.info(f"[AUTO] Current prices: spot={spot_price}, futures={futures_price}")
            if spot_price <= 0 or futures_price <= 0:
                self._logger.warning(f"[AUTO] Invalid prices (spot={spot_price}, futures={futures_price}) - ABORTING")
                return

            # Handle entry signals
            if signal_type in ['ENTRY_LONG', 'ENTRY_SHORT']:
                self._handle_entry_signal(signal, config, database,
                                         spot_broker, futures_broker,
                                         spot_price, futures_price)

            # Handle exit signals
            elif signal_type in ['EXIT', 'STOP_LOSS']:
                self._handle_exit_signal(signal, config, database,
                                        spot_broker, futures_broker,
                                        spot_price, futures_price)

        except Exception as e:
            self._logger.error(f"[AUTO] Error handling signal: {e}")
            import traceback
            traceback.print_exc()

    def _get_current_prices(self, spot_broker, futures_broker) -> Tuple[float, float]:
        """Get current spot and futures prices from MT5"""
        spot_price = 0.0
        futures_price = 0.0

        try:
            import MetaTrader5 as mt5

            if not mt5.initialize():
                self._logger.error("[AUTO] MT5 initialization failed")
                return 0.0, 0.0

            # Get spot price
            if spot_broker.broker_type == 'MT5':
                tick = mt5.symbol_info_tick(spot_broker.symbol)
                if tick:
                    spot_price = (tick.bid + tick.ask) / 2

            # Get futures price
            if futures_broker.broker_type == 'MT5':
                tick = mt5.symbol_info_tick(futures_broker.symbol)
                if tick:
                    futures_price = (tick.bid + tick.ask) / 2

            mt5.shutdown()

        except ImportError:
            self._logger.error("[AUTO] MetaTrader5 not installed")
        except Exception as e:
            self._logger.error(f"[AUTO] Error getting prices: {e}")

        return spot_price, futures_price

    def _check_filters(self, config, zscore: float, spread_history: list = None,
                       current_std: float = None, spot_spread_cents: float = None,
                       futures_spread_cents: float = None) -> Tuple[bool, str]:
        """
        Check if all trading filters pass.

        Args:
            config: TradingConfig object
            zscore: Current z-score
            spread_history: List of historical spread values (for Hurst)
            current_std: Current standard deviation of spread (for STD filter)
            spot_spread_cents: Spot bid-ask spread in cents
            futures_spread_cents: Futures bid-ask spread in cents

        Returns:
            Tuple of (passes, reason)
        """
        # STD Filter check - only at entry, ensures trade is profitable
        if config.std_filter_enabled and current_std is not None:
            std_filter_result = calculate_min_profitable_std(
                config, current_std, spot_spread_cents, futures_spread_cents
            )
            if not std_filter_result['is_profitable']:
                min_std = std_filter_result['min_std']
                std_ratio = std_filter_result['std_ratio']
                return False, f"STD filter: current {current_std:.4f} < min required {min_std:.4f} (ratio: {std_ratio:.1%})"
            else:
                self._logger.info(f"[AUTO] STD filter passed: {current_std:.4f} >= {std_filter_result['min_std']:.4f}")

        # Hurst Filter check - only trade in mean-reverting markets
        if config.hurst_enabled and spread_history:
            hurst_value, hurst_regime = calculate_hurst_exponent(spread_history)
            if hurst_value is not None and hurst_value > config.hurst_threshold:
                return False, f"Hurst filter: {hurst_value:.2f} > {config.hurst_threshold} (trending market)"
            elif hurst_value is not None:
                self._logger.info(f"[AUTO] Hurst filter passed: {hurst_value:.2f} <= {config.hurst_threshold} ({hurst_regime})")

        # Max positions check
        if self._position_open:
            return False, "Position already open"

        return True, "All filters passed"

    def _handle_entry_signal(self, signal, config, database,
                            spot_broker, futures_broker,
                            spot_price: float, futures_price: float):
        """Handle entry signal (ENTRY_LONG or ENTRY_SHORT)"""
        self._logger.info(f"[AUTO] >>> _handle_entry_signal CALLED for {signal.signal_type} <<<")
        self._logger.info(f"[AUTO] Position state: open={self._position_open}, direction={self._position_direction}")

        if self._position_open:
            self._logger.info("[AUTO] Position already open, skipping entry signal - ABORTING")
            return

        # Get current market data for filter checks
        current_std = None
        spot_spread_cents = None
        futures_spread_cents = None
        spread_history = None

        try:
            import MetaTrader5 as mt5
            import numpy as np

            if mt5.initialize():
                # Get bid-ask spreads for STD filter cost calculation
                spot_tick = mt5.symbol_info_tick(spot_broker.symbol)
                futures_tick = mt5.symbol_info_tick(futures_broker.symbol)

                if spot_tick:
                    spot_spread_cents = (spot_tick.ask - spot_tick.bid) * 100
                if futures_tick:
                    futures_spread_cents = (futures_tick.ask - futures_tick.bid) * 100

                mt5.shutdown()

            # Get spread history from database to calculate current STD
            lookback = config.lookback_period if config else 90
            history = database.get_price_history('ACTIVE', limit=lookback, max_age_hours=24)
            if history and len(history) >= 20:
                spreads = [row[0] for row in reversed(history)]  # spread column
                spread_history = spreads
                current_std = float(np.std(spreads))
                self._logger.info(f"[AUTO] Current STD: {current_std:.4f} (from {len(spreads)} samples)")

        except Exception as e:
            self._logger.warning(f"[AUTO] Could not get market data for filters: {e}")

        # Calculate STD filter result for logging
        std_filter_result = None
        if current_std is not None:
            std_filter_result = calculate_min_profitable_std(
                config, current_std, spot_spread_cents, futures_spread_cents
            )

        # Check filters with STD data
        self._logger.info(f"[AUTO] Checking filters: std={current_std}, std_filter_enabled={config.std_filter_enabled}")
        passes, reason = self._check_filters(
            config, signal.zscore, spread_history,
            current_std, spot_spread_cents, futures_spread_cents
        )
        self._logger.info(f"[AUTO] Filter check result: passes={passes}, reason={reason}")

        # Log STD filter event to database (for blocked signals only - successful trades logged after execution)
        if std_filter_result and not passes:
            if 'STD filter' in reason:
                action_taken = 'BLOCKED_STD'
            elif 'Hurst filter' in reason:
                action_taken = 'BLOCKED_HURST'
            elif 'Position already open' in reason:
                action_taken = 'BLOCKED_POSITION'
            else:
                action_taken = 'BLOCKED_OTHER'

            self._log_std_filter_event(
                database, zscore=signal.zscore, signal_type=signal.signal_type,
                current_std=current_std, min_required_std=std_filter_result['min_std'],
                std_ratio=std_filter_result['std_ratio'], is_profitable=std_filter_result['is_profitable'],
                spot_spread_cents=spot_spread_cents, futures_spread_cents=futures_spread_cents,
                round_trip_cost=std_filter_result['round_trip_cost'],
                action_taken=action_taken, blocked_reason=reason
            )

        if not passes:
            self._logger.info(f"[AUTO] Entry blocked: {reason}")
            # Emit blocked signal to frontend
            socketio.emit('auto_trade', {
                'action': 'BLOCKED',
                'reason': reason,
                'zscore': signal.zscore,
                'signal_type': signal.signal_type
            })
            return

        signal_type = signal.signal_type
        direction = 'LONG' if signal_type == 'ENTRY_LONG' else 'SHORT'

        self._logger.info(f"[AUTO] === FILTERS PASSED === Executing {direction} entry trade")
        self._logger.info(f"[AUTO] Trade details: lot_size={config.lot_size}, z={signal.zscore:.2f}")

        # Determine trade direction
        # ENTRY_LONG (z-score low): Buy futures, Sell spot (expecting spread to widen)
        # ENTRY_SHORT (z-score high): Sell futures, Buy spot (expecting spread to narrow)

        try:
            import MetaTrader5 as mt5

            self._logger.info("[AUTO] Attempting MT5 initialization...")
            if not mt5.initialize():
                mt5_error = mt5.last_error()
                self._logger.error(f"[AUTO] MT5 initialization failed for trade - error: {mt5_error}")
                # Log failed attempt
                if std_filter_result:
                    self._log_std_filter_event(
                        database, zscore=signal.zscore, signal_type=signal.signal_type,
                        current_std=current_std, min_required_std=std_filter_result['min_std'],
                        std_ratio=std_filter_result['std_ratio'], is_profitable=std_filter_result['is_profitable'],
                        spot_spread_cents=spot_spread_cents, futures_spread_cents=futures_spread_cents,
                        round_trip_cost=std_filter_result['round_trip_cost'],
                        action_taken='TRADE_FAILED', blocked_reason='MT5 initialization failed'
                    )
                return

            lot_size = config.lot_size
            self._logger.info(f"[AUTO] MT5 initialized successfully, lot_size={lot_size}")

            # Execute spot leg
            if direction == 'SHORT':
                # Short spread: Buy spot
                spot_order_type = mt5.ORDER_TYPE_BUY
                futures_order_type = mt5.ORDER_TYPE_SELL
            else:
                # Long spread: Sell spot
                spot_order_type = mt5.ORDER_TYPE_SELL
                futures_order_type = mt5.ORDER_TYPE_BUY

            # Get spot tick for pricing
            spot_tick = mt5.symbol_info_tick(spot_broker.symbol)
            if not spot_tick:
                self._logger.error(f"[AUTO] Could not get spot tick for {spot_broker.symbol}")
                mt5.shutdown()
                return

            spot_price_for_order = spot_tick.ask if spot_order_type == mt5.ORDER_TYPE_BUY else spot_tick.bid

            # Get correct filling mode from symbol info
            spot_symbol_info = mt5.symbol_info(spot_broker.symbol)
            if spot_symbol_info is None:
                self._logger.error(f"[AUTO] Could not get symbol info for {spot_broker.symbol}")
                mt5.shutdown()
                return

            # Determine filling mode - RETURN is most universally supported
            # filling_mode flags: 1=FOK, 2=IOC. If 0 or other, use RETURN
            spot_filling_mode = mt5.ORDER_FILLING_RETURN  # Most compatible default
            try:
                fm = spot_symbol_info.filling_mode
                if fm == 1:  # Only FOK
                    spot_filling_mode = mt5.ORDER_FILLING_FOK
                elif fm == 2:  # Only IOC
                    spot_filling_mode = mt5.ORDER_FILLING_IOC
                elif fm == 3:  # Both FOK and IOC
                    spot_filling_mode = mt5.ORDER_FILLING_FOK
                # else: keep RETURN (fm=0 or other values)
            except:
                pass  # Keep RETURN on any error
            self._logger.info(f"[AUTO] Spot symbol filling_mode={spot_symbol_info.filling_mode}, using ORDER_FILLING={spot_filling_mode}")

            # Place spot order
            spot_request = {
                "action": mt5.TRADE_ACTION_DEAL,
                "symbol": spot_broker.symbol,
                "volume": lot_size,
                "type": spot_order_type,
                "price": spot_price_for_order,
                "deviation": 20,
                "magic": 123456,
                "comment": f"AutoTrader {direction} Entry - Spot",
                "type_time": mt5.ORDER_TIME_GTC,
                "type_filling": spot_filling_mode,
            }

            self._logger.info(f"[AUTO] Sending SPOT order: symbol={spot_broker.symbol}, type={'BUY' if spot_order_type == mt5.ORDER_TYPE_BUY else 'SELL'}, volume={lot_size}, price={spot_price_for_order}")
            spot_result = mt5.order_send(spot_request)
            self._logger.info(f"[AUTO] SPOT order result: retcode={spot_result.retcode}, order={spot_result.order}, comment={spot_result.comment}")

            if spot_result.retcode != mt5.TRADE_RETCODE_DONE:
                error_msg = f"Spot order failed: {spot_result.retcode} - {spot_result.comment}"
                self._logger.error(f"[AUTO] {error_msg}")
                # Log failed attempt
                if std_filter_result:
                    self._log_std_filter_event(
                        database, zscore=signal.zscore, signal_type=signal.signal_type,
                        current_std=current_std, min_required_std=std_filter_result['min_std'],
                        std_ratio=std_filter_result['std_ratio'], is_profitable=std_filter_result['is_profitable'],
                        spot_spread_cents=spot_spread_cents, futures_spread_cents=futures_spread_cents,
                        round_trip_cost=std_filter_result['round_trip_cost'],
                        action_taken='TRADE_FAILED', blocked_reason=error_msg
                    )
                mt5.shutdown()
                return

            self._logger.info(f"[AUTO] Spot order filled: ticket={spot_result.order}, price={spot_result.price}")

            # Get futures tick for pricing
            futures_tick = mt5.symbol_info_tick(futures_broker.symbol)
            if not futures_tick:
                self._logger.error(f"[AUTO] Could not get futures tick for {futures_broker.symbol}")
                # Reverse spot trade
                self._logger.warning("[AUTO] Reversing spot trade due to missing futures tick")
                reverse_type = mt5.ORDER_TYPE_SELL if spot_order_type == mt5.ORDER_TYPE_BUY else mt5.ORDER_TYPE_BUY
                mt5.order_send({
                    "action": mt5.TRADE_ACTION_DEAL, "symbol": spot_broker.symbol, "volume": lot_size,
                    "type": reverse_type, "price": spot_tick.ask if reverse_type == mt5.ORDER_TYPE_BUY else spot_tick.bid,
                    "deviation": 20, "magic": 123456, "comment": "AutoTrader Reversal", "type_time": mt5.ORDER_TIME_GTC, "type_filling": spot_filling_mode,
                })
                mt5.shutdown()
                return

            futures_price_for_order = futures_tick.ask if futures_order_type == mt5.ORDER_TYPE_BUY else futures_tick.bid

            # Get correct filling mode for futures symbol
            futures_symbol_info = mt5.symbol_info(futures_broker.symbol)
            if futures_symbol_info is None:
                self._logger.error(f"[AUTO] Could not get symbol info for {futures_broker.symbol}")
                # Reverse spot trade
                reverse_type = mt5.ORDER_TYPE_SELL if spot_order_type == mt5.ORDER_TYPE_BUY else mt5.ORDER_TYPE_BUY
                mt5.order_send({
                    "action": mt5.TRADE_ACTION_DEAL, "symbol": spot_broker.symbol, "volume": lot_size,
                    "type": reverse_type, "price": spot_tick.ask if reverse_type == mt5.ORDER_TYPE_BUY else spot_tick.bid,
                    "deviation": 20, "magic": 123456, "comment": "AutoTrader Reversal", "type_time": mt5.ORDER_TIME_GTC, "type_filling": spot_filling_mode,
                })
                mt5.shutdown()
                return

            # Determine filling mode for futures - RETURN is most universally supported
            futures_filling_mode = mt5.ORDER_FILLING_RETURN
            try:
                fm = futures_symbol_info.filling_mode
                if fm == 1:
                    futures_filling_mode = mt5.ORDER_FILLING_FOK
                elif fm == 2:
                    futures_filling_mode = mt5.ORDER_FILLING_IOC
                elif fm == 3:
                    futures_filling_mode = mt5.ORDER_FILLING_FOK
            except:
                pass
            self._logger.info(f"[AUTO] Futures symbol filling_mode={futures_symbol_info.filling_mode}, using ORDER_FILLING={futures_filling_mode}")

            # Place futures order
            futures_request = {
                "action": mt5.TRADE_ACTION_DEAL,
                "symbol": futures_broker.symbol,
                "volume": lot_size,
                "type": futures_order_type,
                "price": futures_price_for_order,
                "deviation": 20,
                "magic": 123456,
                "comment": f"AutoTrader {direction} Entry - Futures",
                "type_time": mt5.ORDER_TIME_GTC,
                "type_filling": futures_filling_mode,
            }

            self._logger.info(f"[AUTO] Sending FUTURES order: symbol={futures_broker.symbol}, type={'BUY' if futures_order_type == mt5.ORDER_TYPE_BUY else 'SELL'}, volume={lot_size}, price={futures_price_for_order}")
            futures_result = mt5.order_send(futures_request)
            self._logger.info(f"[AUTO] FUTURES order result: retcode={futures_result.retcode}, order={futures_result.order}, comment={futures_result.comment}")

            if futures_result.retcode != mt5.TRADE_RETCODE_DONE:
                error_msg = f"Futures order failed: {futures_result.retcode} - {futures_result.comment}"
                self._logger.error(f"[AUTO] {error_msg}")
                # Reverse/close spot trade - MUST include position ticket
                self._logger.warning(f"[AUTO] Closing spot position due to futures failure: ticket={spot_result.order}")
                reverse_type = mt5.ORDER_TYPE_SELL if spot_order_type == mt5.ORDER_TYPE_BUY else mt5.ORDER_TYPE_BUY
                reverse_request = {
                    "action": mt5.TRADE_ACTION_DEAL,
                    "symbol": spot_broker.symbol,
                    "volume": lot_size,
                    "type": reverse_type,
                    "price": mt5.symbol_info_tick(spot_broker.symbol).ask if reverse_type == mt5.ORDER_TYPE_BUY else mt5.symbol_info_tick(spot_broker.symbol).bid,
                    "deviation": 20,
                    "magic": 123456,
                    "comment": "AutoTrader Rollback - Spot",
                    "type_time": mt5.ORDER_TIME_GTC,
                    "type_filling": spot_filling_mode,
                    "position": int(spot_result.order),  # CRITICAL: Include position ticket to close
                }
                rollback_result = mt5.order_send(reverse_request)

                # Check if rollback succeeded
                if rollback_result.retcode != mt5.TRADE_RETCODE_DONE:
                    self._logger.error(f"[AUTO] CRITICAL: Spot rollback FAILED! Manual intervention needed. Ticket={spot_result.order}, Error={rollback_result.retcode} - {rollback_result.comment}")
                    error_msg += f" | ROLLBACK FAILED: {rollback_result.comment}"
                else:
                    self._logger.info(f"[AUTO] Spot position rolled back successfully")

                # Log failed attempt
                if std_filter_result:
                    self._log_std_filter_event(
                        database, zscore=signal.zscore, signal_type=signal.signal_type,
                        current_std=current_std, min_required_std=std_filter_result['min_std'],
                        std_ratio=std_filter_result['std_ratio'], is_profitable=std_filter_result['is_profitable'],
                        spot_spread_cents=spot_spread_cents, futures_spread_cents=futures_spread_cents,
                        round_trip_cost=std_filter_result['round_trip_cost'],
                        action_taken='TRADE_FAILED', blocked_reason=error_msg + ' (spot reversed)'
                    )
                mt5.shutdown()
                return

            self._logger.info(f"[AUTO] Futures order filled: ticket={futures_result.order}, price={futures_result.price}")

            mt5.shutdown()

            # Record trade in database
            trade_id = f"AUTO_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
            trade = Trade(
                trade_id=trade_id,
                asset=config.asset_name,
                direction='Long Spread' if direction == 'LONG' else 'Short Spread',
                entry_date=datetime.now().isoformat(),
                entry_zscore=signal.zscore,
                entry_spot_price=spot_result.price,
                entry_futures_price=futures_result.price,
                lot_size=lot_size,
                spot_broker_id=spot_broker.broker_id,
                mt5_spot_ticket=spot_result.order,
                futures_broker_id=futures_broker.broker_id,
                mt5_futures_ticket=futures_result.order,
                status='OPEN'
            )

            database.add_trade(trade)
            self._logger.info(f"[AUTO] === TRADE RECORDED IN DATABASE: {trade_id} ===")

            # Log successful trade entry to STD filter log
            if std_filter_result:
                self._log_std_filter_event(
                    database, zscore=signal.zscore, signal_type=signal.signal_type,
                    current_std=current_std, min_required_std=std_filter_result['min_std'],
                    std_ratio=std_filter_result['std_ratio'], is_profitable=std_filter_result['is_profitable'],
                    spot_spread_cents=spot_spread_cents, futures_spread_cents=futures_spread_cents,
                    round_trip_cost=std_filter_result['round_trip_cost'],
                    action_taken='TRADE_ENTERED', trade_id=trade_id
                )

            # Update position tracking
            self._position_open = True
            self._position_direction = direction
            self._entry_trade_id = trade_id
            self._entry_spot_price = spot_result.price
            self._entry_futures_price = futures_result.price
            self._entry_zscore = signal.zscore
            self._entry_time = datetime.now()
            # Store MT5 tickets for closing positions later
            self._spot_ticket = spot_result.order
            self._futures_ticket = futures_result.order

            self._logger.info(f"[AUTO] Trade opened: {trade_id} ({direction}) - spot_ticket={self._spot_ticket}, futures_ticket={self._futures_ticket}")

            # Emit to frontend
            socketio.emit('auto_trade', {
                'action': 'ENTRY',
                'direction': direction,
                'trade_id': trade_id,
                'spot_price': spot_result.price,
                'futures_price': futures_result.price,
                'zscore': signal.zscore
            })

        except ImportError:
            self._logger.error("[AUTO] MetaTrader5 not installed")
        except Exception as e:
            self._logger.error(f"[AUTO] Trade execution error: {e}")
            import traceback
            traceback.print_exc()

    def _handle_exit_signal(self, signal, config, database,
                           spot_broker, futures_broker,
                           spot_price: float, futures_price: float):
        """Handle exit signal (EXIT or STOP_LOSS)"""

        if not self._position_open:
            self._logger.info("[AUTO] No position open, skipping exit signal")
            return

        signal_type = signal.signal_type
        self._logger.info(f"[AUTO] Executing exit trade ({signal_type})")

        try:
            import MetaTrader5 as mt5

            if not mt5.initialize():
                self._logger.error("[AUTO] MT5 initialization failed for exit")
                return

            lot_size = config.lot_size

            # Close in opposite direction of entry
            if self._position_direction == 'SHORT':
                # Was short spread (bought spot, sold futures) - now sell spot, buy futures
                spot_order_type = mt5.ORDER_TYPE_SELL
                futures_order_type = mt5.ORDER_TYPE_BUY
            else:
                # Was long spread (sold spot, bought futures) - now buy spot, sell futures
                spot_order_type = mt5.ORDER_TYPE_BUY
                futures_order_type = mt5.ORDER_TYPE_SELL

            # Get correct filling modes - RETURN is most universally supported
            spot_symbol_info = mt5.symbol_info(spot_broker.symbol)
            futures_symbol_info = mt5.symbol_info(futures_broker.symbol)

            spot_filling_mode = mt5.ORDER_FILLING_RETURN
            if spot_symbol_info:
                try:
                    fm = spot_symbol_info.filling_mode
                    if fm == 1:
                        spot_filling_mode = mt5.ORDER_FILLING_FOK
                    elif fm == 2:
                        spot_filling_mode = mt5.ORDER_FILLING_IOC
                    elif fm == 3:
                        spot_filling_mode = mt5.ORDER_FILLING_FOK
                except:
                    pass

            futures_filling_mode = mt5.ORDER_FILLING_RETURN
            if futures_symbol_info:
                try:
                    fm = futures_symbol_info.filling_mode
                    if fm == 1:
                        futures_filling_mode = mt5.ORDER_FILLING_FOK
                    elif fm == 2:
                        futures_filling_mode = mt5.ORDER_FILLING_IOC
                    elif fm == 3:
                        futures_filling_mode = mt5.ORDER_FILLING_FOK
                except:
                    pass

            # Close spot position - MUST include position ticket to close existing position
            self._logger.info(f"[AUTO] Closing spot position: ticket={self._spot_ticket}")
            spot_request = {
                "action": mt5.TRADE_ACTION_DEAL,
                "symbol": spot_broker.symbol,
                "volume": lot_size,
                "type": spot_order_type,
                "price": mt5.symbol_info_tick(spot_broker.symbol).ask if spot_order_type == mt5.ORDER_TYPE_BUY else mt5.symbol_info_tick(spot_broker.symbol).bid,
                "deviation": 20,
                "magic": 123456,
                "comment": f"AutoTrader Exit - Spot ({signal_type})",
                "type_time": mt5.ORDER_TIME_GTC,
                "type_filling": spot_filling_mode,
            }
            # Include position ticket if available (required to close existing position)
            if self._spot_ticket:
                spot_request["position"] = int(self._spot_ticket)

            spot_result = mt5.order_send(spot_request)

            if spot_result.retcode != mt5.TRADE_RETCODE_DONE:
                self._logger.error(f"[AUTO] Spot close failed: {spot_result.retcode} - {spot_result.comment}")
                mt5.shutdown()
                return

            self._logger.info(f"[AUTO] Spot position closed successfully")

            # Close futures position - MUST include position ticket to close existing position
            self._logger.info(f"[AUTO] Closing futures position: ticket={self._futures_ticket}")
            futures_request = {
                "action": mt5.TRADE_ACTION_DEAL,
                "symbol": futures_broker.symbol,
                "volume": lot_size,
                "type": futures_order_type,
                "price": mt5.symbol_info_tick(futures_broker.symbol).ask if futures_order_type == mt5.ORDER_TYPE_BUY else mt5.symbol_info_tick(futures_broker.symbol).bid,
                "deviation": 20,
                "magic": 123456,
                "comment": f"AutoTrader Exit - Futures ({signal_type})",
                "type_time": mt5.ORDER_TIME_GTC,
                "type_filling": futures_filling_mode,
            }
            # Include position ticket if available (required to close existing position)
            if self._futures_ticket:
                futures_request["position"] = int(self._futures_ticket)

            futures_result = mt5.order_send(futures_request)

            if futures_result.retcode != mt5.TRADE_RETCODE_DONE:
                self._logger.error(f"[AUTO] Futures close failed: {futures_result.retcode} - {futures_result.comment}")
                # Don't reverse - we're partially closed, log for manual intervention
                mt5.shutdown()
                return

            self._logger.info(f"[AUTO] Futures position closed successfully")

            mt5.shutdown()

            # Calculate P&L
            if self._position_direction == 'SHORT':
                # Short spread: Bought spot at entry, sold at exit
                spot_pnl = (spot_result.price - self._entry_spot_price) * lot_size * config.contract_size
                # Sold futures at entry, bought at exit
                futures_pnl = (self._entry_futures_price - futures_result.price) * lot_size * config.contract_size
            else:
                # Long spread: Sold spot at entry, bought at exit
                spot_pnl = (self._entry_spot_price - spot_result.price) * lot_size * config.contract_size
                # Bought futures at entry, sold at exit
                futures_pnl = (futures_result.price - self._entry_futures_price) * lot_size * config.contract_size

            gross_pnl = spot_pnl + futures_pnl
            commission = config.commission_per_lot * lot_size * 2  # Both legs, entry and exit
            net_pnl = gross_pnl - commission

            # Update trade in database
            days_held = (datetime.now() - self._entry_time).total_seconds() / 86400 if self._entry_time else 0

            # Load existing trade and update
            conn = database._get_connection()
            cursor = conn.cursor()
            cursor.execute('''
                UPDATE trades SET
                    exit_date = ?,
                    days_held = ?,
                    exit_zscore = ?,
                    exit_spot_price = ?,
                    exit_futures_price = ?,
                    spot_pnl = ?,
                    futures_pnl = ?,
                    gross_pnl = ?,
                    commission = ?,
                    net_pnl = ?,
                    status = 'CLOSED'
                WHERE trade_id = ?
            ''', (
                datetime.now().isoformat(),
                days_held,
                signal.zscore,
                spot_result.price,
                futures_result.price,
                spot_pnl,
                futures_pnl,
                gross_pnl,
                commission,
                net_pnl,
                self._entry_trade_id
            ))
            conn.commit()

            self._logger.info(f"[AUTO] Trade closed: {self._entry_trade_id}, Net P&L: ${net_pnl:.2f}")

            # Emit to frontend
            socketio.emit('auto_trade', {
                'action': 'EXIT',
                'signal_type': signal_type,
                'trade_id': self._entry_trade_id,
                'spot_price': spot_result.price,
                'futures_price': futures_result.price,
                'zscore': signal.zscore,
                'net_pnl': net_pnl
            })

            # Reset position tracking
            self._position_open = False
            self._position_direction = None
            self._entry_trade_id = None
            self._entry_spot_price = None
            self._entry_futures_price = None
            self._entry_zscore = None
            self._entry_time = None
            self._spot_ticket = None
            self._futures_ticket = None

        except ImportError:
            self._logger.error("[AUTO] MetaTrader5 not installed")
        except Exception as e:
            self._logger.error(f"[AUTO] Exit execution error: {e}")
            import traceback
            traceback.print_exc()

    @property
    def has_position(self) -> bool:
        return self._position_open

    @property
    def position_direction(self) -> Optional[str]:
        return self._position_direction


def init_app(db_path: str = "trading.db"):
    """Initialize application components"""
    global db, engine, auto_trader

    db = DatabaseManager(db_path)
    db.initialize()

    # Initialize AutoTrader for automatic trade execution
    auto_trader = AutoTrader(db_path=db_path)
    logger.info("[APP] AutoTrader initialized")

    # Engine will be initialized on first start (optional, not required for auto trading)
    engine = None


def get_db() -> DatabaseManager:
    """Get database manager"""
    global db
    if db is None:
        db = DatabaseManager("trading.db")
        db.initialize()
    return db


# ==================== Routes ====================

@app.route('/')
def index():
    """Redirect to dashboard"""
    return redirect(url_for('dashboard'))


@app.route('/dashboard')
def dashboard():
    """Dashboard page - real-time monitoring"""
    database = get_db()
    config = database.get_config()
    brokers = database.get_brokers()
    open_trades = database.get_open_trades()
    recent_trades = database.get_trades(limit=10)
    stats = database.get_trade_statistics()

    # Get active brokers from JSON file (workaround for database not persisting)
    spot_broker_id, futures_broker_id = load_active_brokers()

    active_spot = None
    active_futures = None
    for b in brokers:
        if spot_broker_id and b.broker_id == spot_broker_id:
            active_spot = b
        if futures_broker_id and b.broker_id == futures_broker_id:
            active_futures = b

    return render_template(
        'dashboard.html',
        config=config,
        brokers=brokers,
        active_spot=active_spot,
        active_futures=active_futures,
        open_trades=open_trades,
        recent_trades=recent_trades,
        stats=stats,
        engine_state=engine.state.value if engine else "STOPPED"
    )


@app.route('/settings')
def settings():
    """Settings page - trading parameters"""
    database = get_db()
    config = database.get_config()
    brokers = database.get_brokers()

    # Separate brokers by role
    spot_brokers = [b for b in brokers if b.role == 'SPOT']
    futures_brokers = [b for b in brokers if b.role == 'FUTURES']

    # Load active broker IDs from JSON file
    spot_broker_id, futures_broker_id = load_active_brokers()

    return render_template('settings.html', config=config,
                           spot_brokers=spot_brokers,
                           futures_brokers=futures_brokers,
                           active_spot_id=spot_broker_id,
                           active_futures_id=futures_broker_id)


@app.route('/setup')
def setup():
    """Setup page - broker configuration"""
    database = get_db()
    config = database.get_config()
    brokers = database.get_brokers()

    return render_template('setup.html', config=config, brokers=brokers)


@app.route('/analysis')
def analysis():
    """SD Analysis page"""
    database = get_db()
    config = database.get_config()
    sd_stats = database.get_sd_touch_stats()
    sd_touches = database.get_sd_touches(limit=100)
    limit_stats = database.get_limit_order_stats()

    return render_template(
        'analysis.html',
        config=config,
        sd_stats=sd_stats,
        sd_touches=sd_touches,
        limit_stats=limit_stats
    )


# ==================== API Routes ====================

@app.route('/api/status')
def api_status():
    """Get current system status"""
    if engine:
        return jsonify(engine.get_status())
    else:
        database = get_db()
        config = database.get_config()
        brokers = database.get_brokers()

        return jsonify({
            'state': 'STOPPED',
            'config': config.to_dict(),
            'brokers': {b.broker_id: {'status': b.status, 'role': b.role} for b in brokers},
            'market': {},
            'position': {'has_position': False}
        })


@app.route('/api/config', methods=['GET', 'POST'])
def api_config():
    """Get or update trading configuration"""
    database = get_db()

    if request.method == 'POST':
        data = request.get_json()
        logger.info(f"[CONFIG] Saving config: {data}")

        config = database.get_config()

        # Update fields from request
        for key, value in data.items():
            if hasattr(config, key):
                current_val = getattr(config, key)
                # Handle string fields (may be None initially)
                if key in ['active_spot_broker', 'active_futures_broker', 'asset_name',
                           'spot_symbol', 'futures_symbol', 'futures_expiry',
                           'lookback_unit', 'order_type', 'selected_asset']:
                    # String fields - keep as string or None
                    setattr(config, key, value if value else None)
                elif isinstance(current_val, bool) or key in ['hurst_enabled', 'std_filter_enabled',
                                                               'close_before_overnight', 'paper_mode', 'algo_enabled']:
                    setattr(config, key, bool(value))
                elif isinstance(current_val, int):
                    setattr(config, key, int(value) if value else 0)
                elif isinstance(current_val, float):
                    setattr(config, key, float(value) if value else 0.0)
                else:
                    setattr(config, key, value)

        logger.info(f"[CONFIG] Active brokers - Spot: {config.active_spot_broker}, Futures: {config.active_futures_broker}")

        # Save active brokers to JSON file (workaround for database not persisting new fields)
        save_active_brokers(config.active_spot_broker, config.active_futures_broker)

        # Verify the save worked
        saved_spot, saved_futures = load_active_brokers()
        logger.info(f"[CONFIG] Verified saved - Spot: {saved_spot}, Futures: {saved_futures}")

        database.update_config(config)

        # Reload config in engine if running
        if engine:
            engine.reload_config()

        return jsonify({'success': True, 'config': config.to_dict()})

    else:
        config = database.get_config()
        return jsonify(config.to_dict())


@app.route('/api/brokers', methods=['GET', 'POST'])
def api_brokers():
    """Get or add broker configuration"""
    database = get_db()

    if request.method == 'POST':
        data = request.get_json()

        broker = Broker(
            broker_id=data.get('broker_id'),
            name=data.get('name'),
            broker_type=data.get('broker_type', 'MT5'),
            role=data.get('role'),
            mt5_path=data.get('mt5_path'),
            mt5_account=data.get('mt5_account'),
            mt5_server=data.get('mt5_server'),
            mt5_password=data.get('mt5_password'),
            fix_host=data.get('fix_host'),
            fix_port=data.get('fix_port'),
            fix_sender_comp=data.get('fix_sender_comp'),
            fix_target_comp=data.get('fix_target_comp'),
            fix_username=data.get('fix_username'),
            fix_password=data.get('fix_password'),
            flex_host=data.get('flex_host'),
            flex_port=data.get('flex_port'),
            flex_api_key=data.get('flex_api_key'),
            ib_host=data.get('ib_host'),
            ib_port=data.get('ib_port'),
            ib_client_id=data.get('ib_client_id'),
            symbol=data.get('symbol', ''),
            contract_size=float(data.get('contract_size', 100.0)),
            commission_per_lot=float(data.get('commission_per_lot', 0.0)),
            min_volume=float(data.get('min_volume', 0.01)),
            okx_api_key=data.get('okx_api_key'),
            okx_api_secret=data.get('okx_api_secret'),
            okx_passphrase=data.get('okx_passphrase'),
            okx_simulated=data.get('okx_simulated', True),
            okx_account_type=data.get('okx_account_type', 'spot')
        )

        database.add_broker(broker)
        return jsonify({'success': True, 'broker': broker.to_dict()})

    else:
        brokers = database.get_brokers()
        return jsonify([b.to_dict() for b in brokers])


@app.route('/api/brokers/<broker_id>', methods=['GET', 'PUT', 'DELETE'])
def api_broker(broker_id):
    """Get, update, or delete a specific broker"""
    database = get_db()

    if request.method == 'DELETE':
        database.delete_broker(broker_id)
        return jsonify({'success': True})

    elif request.method == 'PUT':
        data = request.get_json()
        broker = database.get_broker(broker_id)
        if broker:
            for key, value in data.items():
                if hasattr(broker, key):
                    setattr(broker, key, value)
            database.add_broker(broker)
            return jsonify({'success': True, 'broker': broker.to_dict()})
        return jsonify({'success': False, 'error': 'Broker not found'}), 404

    else:
        broker = database.get_broker(broker_id)
        if broker:
            return jsonify(broker.to_dict())
        return jsonify({'error': 'Broker not found'}), 404


@app.route('/api/brokers/<broker_id>/test', methods=['POST'])
def api_broker_test(broker_id):
    """Test connectivity for a specific broker"""
    database = get_db()
    broker = database.get_broker(broker_id)

    if not broker:
        return jsonify({'success': False, 'error': 'Broker not found'}), 404

    try:
        import time
        start_time = time.time()

        # Create adapter based on broker type
        if broker.broker_type == 'OKX':
            from adapters.okx_adapter import OKXAdapter
            from adapters.base import BrokerConfig
            import os

            config = BrokerConfig(
                broker_id=broker.broker_id,
                name=broker.name,
                role=broker.role,
                backend_type='OKX',
                okx_api_key=broker.okx_api_key or os.environ.get('OKX_API_KEY', ''),
                okx_api_secret=broker.okx_api_secret or os.environ.get('OKX_API_SECRET', ''),
                okx_passphrase=broker.okx_passphrase or os.environ.get('OKX_PASSPHRASE', ''),
                okx_simulated=broker.okx_simulated if hasattr(broker, 'okx_simulated') else True,
                okx_account_type=broker.okx_account_type if hasattr(broker, 'okx_account_type') else 'spot',
                symbol=broker.symbol
            )

            adapter = OKXAdapter(config)

            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)

            try:
                connected = loop.run_until_complete(adapter.connect())
                if not connected:
                    return jsonify({'success': False, 'error': 'Failed to connect to OKX'})

                # Get account info and price
                account = loop.run_until_complete(adapter.get_account_info())
                tick = loop.run_until_complete(adapter.get_tick(broker.symbol))
                loop.run_until_complete(adapter.disconnect())

                latency_ms = int((time.time() - start_time) * 1000)

                # Update broker status in database
                broker.status = 'CONNECTED'
                broker.latency_ms = latency_ms
                database.add_broker(broker)

                result = {
                    'success': True,
                    'latency_ms': latency_ms,
                    'broker_type': 'OKX'
                }

                if account:
                    result['account_info'] = {
                        'balance': account.balance,
                        'equity': account.equity,
                        'currency': 'USDT'
                    }

                if tick:
                    result['price_info'] = {
                        'symbol': broker.symbol,
                        'bid': tick.bid,
                        'ask': tick.ask
                    }

                return jsonify(result)

            finally:
                loop.close()

        elif broker.broker_type == 'MT5':
            # MT5 test using MetaTrader5 library
            try:
                import MetaTrader5 as mt5

                # Initialize MT5 connection
                if not mt5.initialize():
                    error_code = mt5.last_error()
                    return jsonify({
                        'success': False,
                        'error': f'Failed to initialize MT5. Error: {error_code}. Ensure MT5 terminal is running and algo trading is enabled.'
                    })

                # Get account info
                account_info = mt5.account_info()
                if account_info is None:
                    mt5.shutdown()
                    return jsonify({
                        'success': False,
                        'error': 'Could not get account info. Please log in to MT5.'
                    })

                # Get symbol info and price
                symbol = broker.symbol
                symbol_info = mt5.symbol_info(symbol)
                tick = mt5.symbol_info_tick(symbol)

                latency_ms = int((time.time() - start_time) * 1000)

                # Update broker status
                broker.status = 'CONNECTED'
                broker.latency_ms = latency_ms
                database.add_broker(broker)

                result = {
                    'success': True,
                    'latency_ms': latency_ms,
                    'broker_type': 'MT5',
                    'account_info': {
                        'login': account_info.login,
                        'server': account_info.server,
                        'balance': account_info.balance,
                        'equity': account_info.equity,
                        'currency': account_info.currency
                    }
                }

                if tick:
                    result['price_info'] = {
                        'symbol': symbol,
                        'bid': tick.bid,
                        'ask': tick.ask
                    }
                elif symbol_info is None:
                    result['warning'] = f'Symbol "{symbol}" not found in Market Watch. Add it to see prices.'

                mt5.shutdown()
                return jsonify(result)

            except ImportError:
                return jsonify({
                    'success': False,
                    'error': 'MetaTrader5 Python library not installed. Run: pip install MetaTrader5'
                })
            except Exception as e:
                return jsonify({
                    'success': False,
                    'error': f'MT5 error: {str(e)}'
                })

        elif broker.broker_type in ['FIX', 'FLEXTRADE']:
            # FIX test - placeholder
            return jsonify({
                'success': False,
                'error': 'FIX connection test not implemented yet.'
            })

        elif broker.broker_type == 'IB':
            # IB test - placeholder
            return jsonify({
                'success': False,
                'error': 'Interactive Brokers connection test not implemented yet.'
            })

        else:
            return jsonify({
                'success': False,
                'error': f'Unknown broker type: {broker.broker_type}'
            })

    except Exception as e:
        # Update broker status to error
        broker.status = 'ERROR'
        database.add_broker(broker)

        logger.error(f"Broker test error: {e}")
        return jsonify({
            'success': False,
            'error': str(e)
        })


@app.route('/api/brokers/<broker_id>/diagnose', methods=['POST'])
def api_broker_diagnose(broker_id):
    """Comprehensive diagnostic for broker connectivity issues"""
    database = get_db()
    broker = database.get_broker(broker_id)

    if not broker:
        return jsonify({'success': False, 'error': 'Broker not found'}), 404

    diagnostics = {
        'broker_id': broker_id,
        'broker_type': broker.broker_type,
        'broker_name': broker.name,
        'checks': [],
        'suggestions': [],
        'overall_status': 'UNKNOWN'
    }

    def add_check(name, status, message, details=None):
        check = {'name': name, 'status': status, 'message': message}
        if details:
            check['details'] = details
        diagnostics['checks'].append(check)
        return status == 'PASS'

    try:
        if broker.broker_type == 'OKX':
            from adapters.okx_adapter import OKXAdapter
            from adapters.base import BrokerConfig
            import os
            import aiohttp

            # Check 1: Configuration
            api_key = broker.okx_api_key or os.environ.get('OKX_API_KEY', '')
            api_secret = broker.okx_api_secret or os.environ.get('OKX_API_SECRET', '')
            passphrase = broker.okx_passphrase or os.environ.get('OKX_PASSPHRASE', '')

            if not api_key:
                add_check('API Key', 'FAIL', 'API Key is missing')
                diagnostics['suggestions'].append({
                    'issue': 'Missing API Key',
                    'fix': 'Add your OKX API Key in the broker configuration or set OKX_API_KEY environment variable',
                    'steps': [
                        '1. Log in to OKX',
                        '2. Go to Account > API',
                        '3. Create a new API key with trading permissions',
                        '4. Copy the API Key and paste it in the broker config'
                    ]
                })
            else:
                add_check('API Key', 'PASS', f'API Key configured (ends with ...{api_key[-4:]})')

            if not api_secret:
                add_check('API Secret', 'FAIL', 'API Secret is missing')
                diagnostics['suggestions'].append({
                    'issue': 'Missing API Secret',
                    'fix': 'Add your OKX API Secret Key',
                    'steps': ['The secret is shown only once when creating the API key']
                })
            else:
                add_check('API Secret', 'PASS', 'API Secret configured')

            if not passphrase:
                add_check('Passphrase', 'FAIL', 'API Passphrase is missing')
                diagnostics['suggestions'].append({
                    'issue': 'Missing Passphrase',
                    'fix': 'Add your OKX API Passphrase (set during API key creation)',
                    'steps': ['This is the passphrase you created with your API key']
                })
            else:
                add_check('Passphrase', 'PASS', 'Passphrase configured')

            # Check 2: Symbol format
            symbol = broker.symbol or ''
            if not symbol:
                add_check('Symbol', 'FAIL', 'No trading symbol configured')
                diagnostics['suggestions'].append({
                    'issue': 'Missing Symbol',
                    'fix': 'Configure the trading symbol',
                    'steps': [
                        'For Spot: Use format like BTC-USDT, ETH-USDT',
                        'For Futures: Use format like BTC-USDT-SWAP or BTC-USDT-240329'
                    ]
                })
            elif '-' not in symbol:
                add_check('Symbol Format', 'WARN', f'Symbol "{symbol}" may not be in OKX format')
                diagnostics['suggestions'].append({
                    'issue': 'Incorrect Symbol Format',
                    'fix': 'OKX uses dash-separated symbols',
                    'steps': [
                        f'Current: {symbol}',
                        'Expected format: BTC-USDT (spot) or BTC-USDT-SWAP (perpetual)',
                        'Check OKX trading page for exact symbol names'
                    ]
                })
            else:
                add_check('Symbol Format', 'PASS', f'Symbol "{symbol}" appears valid')

            # Check 3: Network connectivity
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)

            async def test_network():
                try:
                    is_simulated = broker.okx_simulated if hasattr(broker, 'okx_simulated') else True
                    base_url = 'https://www.okx.com' if not is_simulated else 'https://www.okx.com'

                    async with aiohttp.ClientSession() as session:
                        async with session.get(f'{base_url}/api/v5/public/time', timeout=aiohttp.ClientTimeout(total=10)) as resp:
                            if resp.status == 200:
                                data = await resp.json()
                                server_time = data.get('data', [{}])[0].get('ts', 'unknown')
                                return True, f'Connected to OKX (server time: {server_time})'
                            return False, f'HTTP {resp.status}'
                except asyncio.TimeoutError:
                    return False, 'Connection timeout - check your internet connection'
                except aiohttp.ClientError as e:
                    return False, f'Network error: {str(e)}'
                except Exception as e:
                    return False, f'Error: {str(e)}'

            network_ok, network_msg = loop.run_until_complete(test_network())
            if network_ok:
                add_check('Network', 'PASS', network_msg)
            else:
                add_check('Network', 'FAIL', network_msg)
                diagnostics['suggestions'].append({
                    'issue': 'Network Connection Failed',
                    'fix': 'Check your internet connection and firewall settings',
                    'steps': [
                        'Verify internet connectivity',
                        'Check if OKX is accessible from your location',
                        'Ensure firewall allows HTTPS connections to okx.com'
                    ]
                })

            # Check 4: Authentication test (only if credentials exist)
            if api_key and api_secret and passphrase and network_ok:
                async def test_auth():
                    try:
                        config = BrokerConfig(
                            broker_id=broker.broker_id,
                            name=broker.name,
                            role=broker.role,
                            backend_type='OKX',
                            okx_api_key=api_key,
                            okx_api_secret=api_secret,
                            okx_passphrase=passphrase,
                            okx_simulated=broker.okx_simulated if hasattr(broker, 'okx_simulated') else True,
                            okx_account_type=broker.okx_account_type if hasattr(broker, 'okx_account_type') else 'spot',
                            symbol=symbol
                        )
                        adapter = OKXAdapter(config)
                        connected = await adapter.connect()

                        if connected:
                            account = await adapter.get_account_info()
                            await adapter.disconnect()
                            if account:
                                return True, f'Authenticated successfully (Balance: {account.balance:.2f} USDT)', account
                            return True, 'Authenticated but could not fetch account info', None
                        return False, 'Authentication failed - check API credentials', None
                    except Exception as e:
                        error_msg = str(e)
                        if 'Invalid API-key' in error_msg or '50111' in error_msg:
                            return False, 'Invalid API Key', None
                        elif 'Invalid Sign' in error_msg or '50113' in error_msg:
                            return False, 'Invalid signature - check API Secret', None
                        elif 'Invalid Passphrase' in error_msg or '50114' in error_msg:
                            return False, 'Invalid Passphrase', None
                        elif 'permission' in error_msg.lower() or '50110' in error_msg:
                            return False, 'API key lacks required permissions', None
                        return False, f'Auth error: {error_msg}', None

                auth_ok, auth_msg, account = loop.run_until_complete(test_auth())
                if auth_ok:
                    add_check('Authentication', 'PASS', auth_msg)

                    # Check 5: Symbol validity (fetch price)
                    async def test_symbol():
                        try:
                            config = BrokerConfig(
                                broker_id=broker.broker_id,
                                name=broker.name,
                                role=broker.role,
                                backend_type='OKX',
                                okx_api_key=api_key,
                                okx_api_secret=api_secret,
                                okx_passphrase=passphrase,
                                okx_simulated=broker.okx_simulated if hasattr(broker, 'okx_simulated') else True,
                                okx_account_type=broker.okx_account_type if hasattr(broker, 'okx_account_type') else 'spot',
                                symbol=symbol
                            )
                            adapter = OKXAdapter(config)
                            await adapter.connect()
                            tick = await adapter.get_tick(symbol)
                            await adapter.disconnect()

                            if tick and tick.bid > 0:
                                return True, f'Symbol valid - Bid: {tick.bid}, Ask: {tick.ask}', tick
                            return False, f'Symbol "{symbol}" returned no price data', None
                        except Exception as e:
                            return False, f'Symbol error: {str(e)}', None

                    if symbol:
                        symbol_ok, symbol_msg, tick = loop.run_until_complete(test_symbol())
                        if symbol_ok:
                            add_check('Symbol Validation', 'PASS', symbol_msg)
                        else:
                            add_check('Symbol Validation', 'FAIL', symbol_msg)
                            diagnostics['suggestions'].append({
                                'issue': 'Invalid Trading Symbol',
                                'fix': f'The symbol "{symbol}" is not valid or not available',
                                'steps': [
                                    'Check OKX for the correct symbol name',
                                    'Spot symbols: BTC-USDT, ETH-USDT, etc.',
                                    'Perpetual swaps: BTC-USDT-SWAP',
                                    'Futures: BTC-USDT-240329 (with expiry date)'
                                ]
                            })
                else:
                    add_check('Authentication', 'FAIL', auth_msg)
                    if 'Invalid API Key' in auth_msg:
                        diagnostics['suggestions'].append({
                            'issue': 'Invalid API Key',
                            'fix': 'The API key is not recognized by OKX',
                            'steps': [
                                'Verify the API key is copied correctly (no extra spaces)',
                                'Check if the API key has been deleted on OKX',
                                'Create a new API key if needed'
                            ]
                        })
                    elif 'Secret' in auth_msg:
                        diagnostics['suggestions'].append({
                            'issue': 'Invalid API Secret',
                            'fix': 'The API secret does not match the key',
                            'steps': [
                                'The secret is only shown once during creation',
                                'If lost, delete and recreate the API key'
                            ]
                        })
                    elif 'Passphrase' in auth_msg:
                        diagnostics['suggestions'].append({
                            'issue': 'Invalid Passphrase',
                            'fix': 'The passphrase is incorrect',
                            'steps': [
                                'This is the passphrase YOU created with the API key',
                                'It is NOT the same as your account password',
                                'If forgotten, delete and recreate the API key'
                            ]
                        })
                    elif 'permission' in auth_msg.lower():
                        diagnostics['suggestions'].append({
                            'issue': 'Insufficient API Permissions',
                            'fix': 'Enable required permissions for the API key',
                            'steps': [
                                'Go to OKX > Account > API',
                                'Edit the API key permissions',
                                'Enable: Read, Trade (and Withdraw if needed)',
                                'For futures: Enable futures trading permission'
                            ]
                        })

            loop.close()

        elif broker.broker_type == 'MT5':
            # MT5 comprehensive diagnostics
            try:
                import MetaTrader5 as mt5
                add_check('MT5 Library', 'PASS', 'MetaTrader5 Python library is installed')

                # Check 1: Initialize MT5
                if mt5.initialize():
                    add_check('MT5 Terminal', 'PASS', 'Connected to MT5 terminal')

                    # Check 2: Account info
                    account_info = mt5.account_info()
                    if account_info:
                        add_check('Account Login', 'PASS',
                                  f'Logged in as {account_info.login} on {account_info.server}')
                        add_check('Account Balance', 'PASS',
                                  f'Balance: {account_info.balance:.2f} {account_info.currency}')

                        # Check trading permissions
                        if account_info.trade_allowed:
                            add_check('Trading Permission', 'PASS', 'Trading is allowed on this account')
                        else:
                            add_check('Trading Permission', 'FAIL', 'Trading is NOT allowed')
                            diagnostics['suggestions'].append({
                                'issue': 'Trading Not Allowed',
                                'fix': 'Enable trading on your MT5 account',
                                'steps': [
                                    'Check if your account has trading privileges',
                                    'Contact your broker if trading is disabled',
                                    'Ensure you are not on a read-only/investor account'
                                ]
                            })
                    else:
                        add_check('Account Login', 'FAIL', 'Not logged in to any account')
                        diagnostics['suggestions'].append({
                            'issue': 'Not Logged In',
                            'fix': 'Log in to your MT5 trading account',
                            'steps': [
                                '1. Open MT5 terminal',
                                '2. File > Login to Trade Account',
                                '3. Enter your credentials'
                            ]
                        })

                    # Check 3: Symbol validation
                    symbol = broker.symbol
                    if symbol:
                        symbol_info = mt5.symbol_info(symbol)
                        if symbol_info:
                            add_check('Symbol', 'PASS', f'Symbol "{symbol}" found')

                            # Check if symbol is visible in Market Watch
                            if symbol_info.visible:
                                add_check('Market Watch', 'PASS', f'Symbol is visible in Market Watch')
                            else:
                                add_check('Market Watch', 'WARN', f'Symbol not in Market Watch')
                                # Try to add it
                                mt5.symbol_select(symbol, True)
                                diagnostics['suggestions'].append({
                                    'issue': 'Symbol Not in Market Watch',
                                    'fix': f'Add {symbol} to Market Watch',
                                    'steps': [
                                        f'Right-click Market Watch > Symbols',
                                        f'Search for {symbol} and click Show'
                                    ]
                                })

                            # Get price
                            tick = mt5.symbol_info_tick(symbol)
                            if tick and tick.bid > 0:
                                add_check('Price Data', 'PASS', f'Bid: {tick.bid}, Ask: {tick.ask}')
                            else:
                                add_check('Price Data', 'WARN', 'No price data available')
                        else:
                            add_check('Symbol', 'FAIL', f'Symbol "{symbol}" not found')
                            diagnostics['suggestions'].append({
                                'issue': 'Symbol Not Found',
                                'fix': f'The symbol "{symbol}" does not exist on this broker',
                                'steps': [
                                    'Check the exact symbol name in MT5 Market Watch',
                                    'Symbols vary by broker (e.g., XAUUSD, GOLD, GOLD_CASH)',
                                    'Update the broker config with the correct symbol'
                                ]
                            })
                    else:
                        add_check('Symbol', 'FAIL', 'No symbol configured')

                    # Check 4: Algo trading
                    terminal_info = mt5.terminal_info()
                    if terminal_info:
                        if terminal_info.trade_allowed:
                            add_check('Algo Trading', 'PASS', 'Algo trading is enabled')
                        else:
                            add_check('Algo Trading', 'FAIL', 'Algo trading is DISABLED')
                            diagnostics['suggestions'].append({
                                'issue': 'Algo Trading Disabled',
                                'fix': 'Enable algorithmic trading in MT5',
                                'steps': [
                                    '1. Tools > Options > Expert Advisors',
                                    '2. Check "Allow algorithmic trading"',
                                    '3. Check "Allow DLL imports" if needed',
                                    '4. Click OK and restart MT5'
                                ]
                            })

                    mt5.shutdown()
                else:
                    error = mt5.last_error()
                    add_check('MT5 Terminal', 'FAIL', f'Cannot connect to MT5: {error}')
                    diagnostics['suggestions'].append({
                        'issue': 'MT5 Not Running',
                        'fix': 'Start MetaTrader 5 terminal',
                        'steps': [
                            '1. Open MetaTrader 5 application',
                            '2. Log in to your trading account',
                            '3. Wait for connection to establish',
                            '4. Try the test again'
                        ]
                    })

            except ImportError:
                add_check('MT5 Library', 'FAIL', 'MetaTrader5 library not installed')
                diagnostics['suggestions'].append({
                    'issue': 'Missing MT5 Library',
                    'fix': 'Install the MetaTrader5 Python package',
                    'steps': [
                        'Run: pip install MetaTrader5',
                        'Restart the application after installation'
                    ]
                })
            except Exception as e:
                add_check('MT5 Error', 'FAIL', f'Unexpected error: {str(e)}')

        elif broker.broker_type in ['FIX', 'FLEXTRADE']:
            add_check('FIX Connection', 'INFO', 'FIX protocol requires gateway configuration')
            diagnostics['suggestions'].append({
                'issue': 'FIX Connection Setup',
                'fix': 'Configure FIX gateway settings',
                'steps': [
                    '1. Verify FIX gateway host and port',
                    '2. Check SenderCompID and TargetCompID',
                    '3. Ensure firewall allows the connection',
                    '4. Verify SSL/TLS certificates if required'
                ]
            })

        elif broker.broker_type == 'IB':
            add_check('IB Gateway', 'INFO', 'Interactive Brokers requires TWS or IB Gateway')
            diagnostics['suggestions'].append({
                'issue': 'IB Connection Setup',
                'fix': 'Ensure TWS or IB Gateway is running',
                'steps': [
                    '1. Start TWS or IB Gateway',
                    '2. Enable API connections in settings',
                    '3. Add your IP to trusted IPs',
                    '4. Note the socket port (default: 7497 for TWS, 4001 for Gateway)'
                ]
            })

        # Calculate overall status
        statuses = [c['status'] for c in diagnostics['checks']]
        if all(s == 'PASS' for s in statuses):
            diagnostics['overall_status'] = 'HEALTHY'
        elif 'FAIL' in statuses:
            diagnostics['overall_status'] = 'ERROR'
        elif 'WARN' in statuses:
            diagnostics['overall_status'] = 'WARNING'
        else:
            diagnostics['overall_status'] = 'INCOMPLETE'

        return jsonify(diagnostics)

    except Exception as e:
        logger.error(f"Diagnostic error: {e}")
        diagnostics['checks'].append({
            'name': 'System Error',
            'status': 'FAIL',
            'message': str(e)
        })
        diagnostics['overall_status'] = 'ERROR'
        return jsonify(diagnostics)


@app.route('/api/basis-premium')
def api_basis_premium():
    """
    Get current basis premium analysis data.

    Returns basis (F-S), days to expiry, fair value basis, premium %, and margin requirements.
    """
    try:
        database = get_db()
        config = database.get_config()
        spot_broker_id, futures_broker_id = load_active_brokers()

        if not spot_broker_id or not futures_broker_id:
            return jsonify({'success': False, 'error': 'No active brokers configured'})

        spot_broker = database.get_broker(spot_broker_id)
        futures_broker = database.get_broker(futures_broker_id)

        if not spot_broker or not futures_broker:
            return jsonify({'success': False, 'error': 'Broker not found'})

        spot_bid, spot_ask = 0, 0
        futures_bid, futures_ask = 0, 0
        leverage = 100

        # Get prices from MT5
        if spot_broker.broker_type == 'MT5' or futures_broker.broker_type == 'MT5':
            try:
                import MetaTrader5 as mt5

                if not mt5.initialize():
                    return jsonify({'success': False, 'error': 'Failed to initialize MT5'})

                # Get account leverage
                account = mt5.account_info()
                if account:
                    leverage = account.leverage

                # Get spot price
                if spot_broker.broker_type == 'MT5':
                    tick = mt5.symbol_info_tick(spot_broker.symbol)
                    if tick:
                        spot_bid = tick.bid
                        spot_ask = tick.ask

                # Get futures price
                if futures_broker.broker_type == 'MT5':
                    tick = mt5.symbol_info_tick(futures_broker.symbol)
                    if tick:
                        futures_bid = tick.bid
                        futures_ask = tick.ask

                mt5.shutdown()

            except ImportError:
                return jsonify({'success': False, 'error': 'MetaTrader5 not installed'})
            except Exception as e:
                return jsonify({'success': False, 'error': f'MT5 error: {str(e)}'})

        if spot_bid <= 0 or futures_bid <= 0:
            return jsonify({'success': False, 'error': 'Could not get prices'})

        spot_mid = (spot_bid + spot_ask) / 2
        futures_mid = (futures_bid + futures_ask) / 2
        actual_basis = futures_mid - spot_mid

        # Get configuration values (prefer broker values, fallback to config)
        swap_charge = getattr(spot_broker, 'swap_charge', 0.0) or (config.swap_charge if config else 0.0)
        lot_size = getattr(spot_broker, 'contract_size', None) or (config.contract_size if config else 100.0)
        futures_expiry_str = getattr(futures_broker, 'futures_expiry', None) or (config.futures_expiry if config else None)
        user_lot_size = config.lot_size if config else 0.1

        # Parse expiry and calculate days to expiry
        _, days_to_expiry = parse_futures_expiry(futures_expiry_str)
        time_to_expiry = days_to_expiry / 365.25 if days_to_expiry > 0 else 0

        # Calculate fair value basis from swap cost
        swap_futures_price, swap_basis, annual_swap_rate = calculate_swap_basis(
            spot_mid, swap_charge, lot_size, time_to_expiry
        )

        # Calculate premium (difference between actual and fair value)
        swap_diff = actual_basis - swap_basis
        swap_premium_pct = ((actual_basis - swap_basis) / abs(swap_basis)) * 100 if abs(swap_basis) > 0.001 else 0

        # Calculate margin requirements
        margin_data = calculate_margin_requirements(
            spot_mid, futures_mid, lot_size, leverage, user_lot_size
        )

        return jsonify({
            'success': True,
            'spot_price': round(spot_mid, 2),
            'futures_price': round(futures_mid, 2),
            'actual_basis': round(actual_basis, 2),
            'days_to_expiry': round(days_to_expiry, 1) if days_to_expiry > 0 else 0,
            'swap_charge': swap_charge,
            'swap_basis': round(swap_basis, 2),
            'swap_diff': round(swap_diff, 2),
            'swap_premium_pct': round(swap_premium_pct, 1),
            'margin': margin_data,
            'timestamp': datetime.now().strftime('%H:%M:%S')
        })

    except Exception as e:
        logger.error(f"Basis premium API error: {e}")
        return jsonify({'success': False, 'error': str(e)})


@app.route('/api/trades')
def api_trades():
    """Get trade history"""
    database = get_db()
    status = request.args.get('status')
    limit = int(request.args.get('limit', 100))

    trades = database.get_trades(status=status, limit=limit)
    return jsonify([t.to_dict() for t in trades])


@app.route('/api/trades/stats')
def api_trade_stats():
    """Get trade statistics"""
    database = get_db()
    stats = database.get_trade_statistics()
    return jsonify(stats)


@app.route('/api/sd-touches')
def api_sd_touches():
    """Get SD touch log"""
    database = get_db()
    limit = int(request.args.get('limit', 100))
    sd_level = request.args.get('sd_level')

    touches = database.get_sd_touches(sd_level=sd_level, limit=limit)
    return jsonify([t.to_dict() for t in touches])


@app.route('/api/sd-touches/stats')
def api_sd_stats():
    """Get SD touch statistics"""
    database = get_db()
    stats = database.get_sd_touch_stats()
    return jsonify(stats)


@app.route('/api/limit-orders/stats')
def api_limit_stats():
    """Get limit order statistics"""
    database = get_db()
    stats = database.get_limit_order_stats()
    return jsonify(stats)


@app.route('/api/engine/start', methods=['POST'])
def api_engine_start():
    """Start trading engine"""
    global engine, engine_loop

    if engine and engine.state == EngineState.RUNNING:
        return jsonify({'success': False, 'error': 'Engine already running'})

    try:
        # Create event loop in background thread
        def run_engine():
            global engine, engine_loop, auto_trader
            engine_loop = asyncio.new_event_loop()
            asyncio.set_event_loop(engine_loop)

            engine = TradingEngine(db_path="trading.db")

            # Initialize AutoTrader for automatic trade execution
            auto_trader = AutoTrader(db_path="trading.db")
            logger.info("[ENGINE] AutoTrader initialized")

            # Register callbacks for SocketIO updates
            engine.on_tick(lambda m: socketio.emit('tick', {
                'spread': m.spread,
                'zscore': m.zscore,
                'spot_bid': m.spot_bid,
                'spot_ask': m.spot_ask,
                'futures_bid': m.futures_bid,
                'futures_ask': m.futures_ask
            }))

            # Signal handler: emit to frontend AND execute auto trades
            def handle_signal(signal):
                # Emit to frontend for display
                socketio.emit('signal', signal.to_dict())
                # Execute trade if algo is enabled
                if auto_trader:
                    auto_trader.handle_signal(signal)

            engine.on_signal(handle_signal)

            engine.on_trade(lambda action, t: socketio.emit('trade', {
                'action': action,
                'trade': t.to_dict()
            }))

            engine_loop.run_until_complete(engine.initialize())
            engine_loop.run_until_complete(engine.start())
            engine_loop.run_forever()

        thread = threading.Thread(target=run_engine, daemon=True)
        thread.start()

        return jsonify({'success': True, 'message': 'Engine starting'})

    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})


@app.route('/api/engine/stop', methods=['POST'])
def api_engine_stop():
    """Stop trading engine"""
    global engine, engine_loop

    if not engine:
        return jsonify({'success': False, 'error': 'Engine not running'})

    try:
        if engine_loop:
            asyncio.run_coroutine_threadsafe(engine.stop(), engine_loop)
            engine_loop.call_soon_threadsafe(engine_loop.stop)

        return jsonify({'success': True, 'message': 'Engine stopping'})

    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})


@app.route('/api/engine/toggle-algo', methods=['POST'])
def api_toggle_algo():
    """Toggle algorithm on/off"""
    database = get_db()
    data = request.get_json()
    enabled = data.get('enabled', False)

    database.update_config_field('algo_enabled', enabled)

    if engine:
        engine.reload_config()

    # Return message for user alert
    if enabled:
        message = "Auto Trading ENABLED! The system will now automatically execute trades when z-score crosses your entry thresholds."
        logger.info("[ALGO] Auto trading enabled by user")
    else:
        message = "Auto Trading DISABLED. No automatic trades will be executed."
        logger.info("[ALGO] Auto trading disabled by user")

    return jsonify({
        'success': True,
        'algo_enabled': enabled,
        'message': message
    })


@app.route('/api/auto-trader/status')
def api_auto_trader_status():
    """Get auto trader status including open position"""
    global auto_trader

    if auto_trader is None:
        return jsonify({
            'initialized': False,
            'has_position': False,
            'position_direction': None,
            'message': 'AutoTrader not initialized (start engine first)'
        })

    return jsonify({
        'initialized': True,
        'has_position': auto_trader.has_position,
        'position_direction': auto_trader.position_direction,
        'entry_trade_id': auto_trader._entry_trade_id,
        'entry_zscore': auto_trader._entry_zscore,
        'entry_spot_price': auto_trader._entry_spot_price,
        'entry_futures_price': auto_trader._entry_futures_price,
        'entry_time': auto_trader._entry_time.isoformat() if auto_trader._entry_time else None
    })


# ==================== STD Filter Log API ====================

@app.route('/api/std-filter-log')
def api_std_filter_log():
    """Get STD filter profitability log"""
    database = get_db()
    limit = request.args.get('limit', 100, type=int)
    profitable_only = request.args.get('profitable_only', 'false').lower() == 'true'

    log = database.get_std_filter_log(limit=limit, profitable_only=profitable_only)

    # Convert to list of dicts
    result = []
    for row in log:
        result.append({
            'id': row['id'],
            'timestamp': row['timestamp'],
            'zscore': row['zscore'],
            'signal_type': row['signal_type'],
            'current_std': row['current_std'],
            'min_required_std': row['min_required_std'],
            'std_ratio': row['std_ratio'],
            'is_profitable': bool(row['is_profitable']),
            'spot_spread_cents': row['spot_spread_cents'],
            'futures_spread_cents': row['futures_spread_cents'],
            'round_trip_cost': row['round_trip_cost'],
            'action_taken': row['action_taken'],
            'trade_id': row['trade_id'],
            'blocked_reason': row['blocked_reason']
        })

    return jsonify(result)


@app.route('/api/std-filter-stats')
def api_std_filter_stats():
    """Get STD filter statistics"""
    database = get_db()
    stats = database.get_std_filter_stats()
    return jsonify(stats)


@app.route('/api/clear-data', methods=['POST'])
def api_clear_data():
    """Clear historical data"""
    database = get_db()
    data = request.get_json()
    data_type = data.get('type', 'all')

    if data_type == 'prices' or data_type == 'all':
        database.clear_price_history()

    if data_type == 'trades' or data_type == 'all':
        database.clear_trades()

    if data_type == 'sd_touches' or data_type == 'all':
        database.clear_sd_touches()

    if data_type == 'std_filter_log' or data_type == 'all':
        database.clear_std_filter_log()

    return jsonify({'success': True})


# ==================== Manual Trading API ====================

def get_okx_adapter():
    """Get or create OKX adapter for manual trading"""
    from adapters.okx_adapter import OKXAdapter
    from adapters.base import BrokerConfig
    import os

    config = BrokerConfig(
        broker_id='manual_okx',
        name='Manual OKX',
        role='SPOT',
        backend_type='OKX',
        okx_api_key=os.environ.get('OKX_API_KEY', ''),
        okx_api_secret=os.environ.get('OKX_API_SECRET', ''),
        okx_passphrase=os.environ.get('OKX_PASSPHRASE', ''),
        okx_simulated=os.environ.get('OKX_SIMULATED', 'true').lower() == 'true',
        okx_account_type='spot',
        symbol='BTC-USDT'
    )

    return OKXAdapter(config)


@app.route('/api/manual/test-connection', methods=['POST'])
def api_manual_test_connection():
    """Test OKX connection"""
    try:
        adapter = get_okx_adapter()

        # Check if running in mock mode (no API credentials)
        is_mock_mode = getattr(adapter, '_mock_mode', False)

        # Run async connect in sync context
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

        try:
            connected = loop.run_until_complete(adapter.connect())
            if connected:
                account = loop.run_until_complete(adapter.get_account_info())
                loop.run_until_complete(adapter.disconnect())

                if is_mock_mode:
                    # Clearly indicate mock mode with warning
                    return jsonify({
                        'success': True,
                        'mock_mode': True,
                        'message': '⚠️ MOCK MODE - No API credentials configured. Data shown is simulated.',
                        'warning': 'Add OKX_API_KEY, OKX_API_SECRET, and OKX_PASSPHRASE to your .env file for real trading.',
                        'account': {
                            'balance': account.balance if account else 100000,
                            'equity': account.equity if account else 100000,
                            'currency': 'USDT (SIMULATED)'
                        }
                    })
                elif account:
                    return jsonify({
                        'success': True,
                        'mock_mode': False,
                        'message': 'Connected successfully',
                        'account': {
                            'balance': account.balance,
                            'equity': account.equity,
                            'currency': 'USDT'
                        }
                    })
                else:
                    return jsonify({
                        'success': True,
                        'mock_mode': False,
                        'message': 'Connected but no account info available'
                    })
            else:
                return jsonify({
                    'success': False,
                    'error': 'Failed to connect'
                })
        finally:
            loop.close()

    except Exception as e:
        logger.error(f"Test connection error: {e}")
        return jsonify({
            'success': False,
            'error': str(e)
        })


@app.route('/api/manual/place-order', methods=['POST'])
def api_manual_place_order():
    """Place a manual order via OKX"""
    try:
        data = request.get_json()
        symbol = data.get('symbol', 'BTC-USDT')
        side = data.get('side', 'BUY')
        price = data.get('price')
        size = data.get('size', 0.001)
        order_type = data.get('order_type', 'limit')

        # Determine if this is swap or spot
        is_swap = '-SWAP' in symbol

        from adapters.okx_adapter import OKXAdapter
        from adapters.base import BrokerConfig, OrderSide
        import os

        config = BrokerConfig(
            broker_id='manual_okx',
            name='Manual OKX',
            role='FUTURES' if is_swap else 'SPOT',
            backend_type='OKX',
            okx_api_key=os.environ.get('OKX_API_KEY', ''),
            okx_api_secret=os.environ.get('OKX_API_SECRET', ''),
            okx_passphrase=os.environ.get('OKX_PASSPHRASE', ''),
            okx_simulated=os.environ.get('OKX_SIMULATED', 'true').lower() == 'true',
            okx_account_type='swap' if is_swap else 'spot',
            symbol=symbol
        )

        adapter = OKXAdapter(config)
        order_side = OrderSide.BUY if side == 'BUY' else OrderSide.SELL

        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

        try:
            connected = loop.run_until_complete(adapter.connect())
            if not connected:
                return jsonify({
                    'success': False,
                    'error': 'Failed to connect to OKX'
                })

            if order_type == 'market':
                result = loop.run_until_complete(
                    adapter.place_market_order(symbol, order_side, size)
                )
            else:
                if not price:
                    # Get current price if not provided
                    tick = loop.run_until_complete(adapter.get_tick(symbol))
                    if tick:
                        price = tick.bid if side == 'SELL' else tick.ask
                    else:
                        return jsonify({
                            'success': False,
                            'error': 'Could not get price. Please specify a price.'
                        })

                result = loop.run_until_complete(
                    adapter.place_limit_order(symbol, order_side, size, price)
                )

            loop.run_until_complete(adapter.disconnect())

            if result and result.success:
                return jsonify({
                    'success': True,
                    'order_id': result.ticket,
                    'fill_price': result.price,
                    'message': 'Order placed successfully'
                })
            else:
                return jsonify({
                    'success': False,
                    'error': result.error if result else 'Unknown error'
                })

        finally:
            loop.close()

    except Exception as e:
        logger.error(f"Place order error: {e}")
        return jsonify({
            'success': False,
            'error': str(e)
        })


@app.route('/api/manual/get-price', methods=['POST'])
def api_manual_get_price():
    """Get current price for a symbol"""
    try:
        data = request.get_json()
        symbol = data.get('symbol', 'BTC-USDT')

        # Determine if this is swap or spot
        is_swap = '-SWAP' in symbol

        from adapters.okx_adapter import OKXAdapter
        from adapters.base import BrokerConfig
        import os

        config = BrokerConfig(
            broker_id='manual_okx',
            name='Manual OKX',
            role='FUTURES' if is_swap else 'SPOT',
            backend_type='OKX',
            okx_api_key=os.environ.get('OKX_API_KEY', ''),
            okx_api_secret=os.environ.get('OKX_API_SECRET', ''),
            okx_passphrase=os.environ.get('OKX_PASSPHRASE', ''),
            okx_simulated=os.environ.get('OKX_SIMULATED', 'true').lower() == 'true',
            okx_account_type='swap' if is_swap else 'spot',
            symbol=symbol
        )

        adapter = OKXAdapter(config)

        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

        try:
            connected = loop.run_until_complete(adapter.connect())
            if not connected:
                return jsonify({
                    'success': False,
                    'error': 'Failed to connect'
                })

            tick = loop.run_until_complete(adapter.get_tick(symbol))
            loop.run_until_complete(adapter.disconnect())

            if tick:
                spread = tick.ask - tick.bid
                spread_pct = (spread / tick.bid) * 100 if tick.bid > 0 else 0
                return jsonify({
                    'success': True,
                    'symbol': symbol,
                    'bid': f'{tick.bid:.2f}',
                    'ask': f'{tick.ask:.2f}',
                    'spread': f'{spread:.2f} ({spread_pct:.4f}%)',
                    'timestamp': tick.timestamp.isoformat() if tick.timestamp else None
                })
            else:
                return jsonify({
                    'success': False,
                    'error': 'Could not get price'
                })

        finally:
            loop.close()

    except Exception as e:
        logger.error(f"Get price error: {e}")
        return jsonify({
            'success': False,
            'error': str(e)
        })


# ==================== Broker Update API ====================

@app.route('/api/brokers/<broker_id>/update', methods=['POST'])
def api_broker_update(broker_id):
    """Update broker settings (swap charge, expiry, etc.)"""
    try:
        data = request.get_json()
        database = get_db()
        broker = database.get_broker(broker_id)

        if not broker:
            return jsonify({'success': False, 'error': 'Broker not found'})

        # Update allowed fields
        if 'swap_charge' in data:
            broker.swap_charge = float(data['swap_charge'])
        if 'futures_expiry' in data:
            broker.futures_expiry = data['futures_expiry']
        if 'contract_size' in data:
            broker.contract_size = float(data['contract_size'])

        database.add_broker(broker)
        logger.info(f"[BROKER] Updated {broker_id}: swap={broker.swap_charge}, expiry={broker.futures_expiry}")

        return jsonify({'success': True, 'broker': broker.to_dict()})

    except Exception as e:
        logger.error(f"Broker update error: {e}")
        return jsonify({'success': False, 'error': str(e)})


# ==================== Test Order API ====================

@app.route('/api/run-tests', methods=['POST'])
def api_run_tests():
    """Run the test suite and return results"""
    import subprocess

    try:
        # Get the project root directory (parent of feature_files)
        project_root = Path(__file__).parent.parent

        # Run pytest with verbose output
        result = subprocess.run(
            ['python', '-m', 'pytest', 'tests/test_trading_system.py', '-v', '--tb=short'],
            cwd=str(project_root),
            capture_output=True,
            text=True,
            timeout=300  # 5 minute timeout
        )

        # Parse the output to get summary
        output = result.stdout + result.stderr

        # Determine if tests passed
        passed = result.returncode == 0

        return jsonify({
            'success': True,
            'passed': passed,
            'return_code': result.returncode,
            'output': output,
            'summary': 'All tests passed!' if passed else 'Some tests failed.'
        })

    except subprocess.TimeoutExpired:
        return jsonify({
            'success': False,
            'error': 'Test execution timed out (5 minute limit)'
        })
    except FileNotFoundError:
        return jsonify({
            'success': False,
            'error': 'Python or pytest not found. Make sure pytest is installed: pip install pytest'
        })
    except Exception as e:
        logger.error(f"Test execution error: {e}")
        return jsonify({
            'success': False,
            'error': str(e)
        })


@app.route('/api/test-order', methods=['POST'])
def api_test_order():
    """Execute a test order on active broker"""
    try:
        data = request.get_json()
        leg = data.get('leg')  # 'spot' or 'futures'
        direction = data.get('direction')  # 'buy' or 'sell'

        database = get_db()

        # Load active brokers from JSON file (consistent with rest of app)
        spot_broker_id, futures_broker_id = load_active_brokers()

        # Get active broker based on leg
        if leg == 'spot':
            broker_id = spot_broker_id
        else:
            broker_id = futures_broker_id

        if not broker_id:
            return jsonify({'success': False, 'error': f'No active {leg} broker selected'})

        broker = database.get_broker(broker_id)
        if not broker:
            return jsonify({'success': False, 'error': f'Broker {broker_id} not found'})

        # Execute based on broker type
        if broker.broker_type == 'MT5':
            try:
                import MetaTrader5 as mt5

                if not mt5.initialize():
                    return jsonify({'success': False, 'error': 'Failed to initialize MT5'})

                symbol = broker.symbol
                symbol_info = mt5.symbol_info(symbol)

                if symbol_info is None:
                    mt5.shutdown()
                    return jsonify({'success': False, 'error': f'Symbol {symbol} not found'})

                if not symbol_info.visible:
                    mt5.symbol_select(symbol, True)

                # Get minimum volume
                min_volume = symbol_info.volume_min
                tick = mt5.symbol_info_tick(symbol)

                if not tick:
                    mt5.shutdown()
                    return jsonify({'success': False, 'error': 'Could not get price'})

                # Prepare order
                if direction == 'buy':
                    order_type = mt5.ORDER_TYPE_BUY
                    price = tick.ask
                else:
                    order_type = mt5.ORDER_TYPE_SELL
                    price = tick.bid

                # Determine the correct filling mode based on symbol support
                # SYMBOL_FILLING_FOK=1, SYMBOL_FILLING_IOC=2 (not always in mt5 module)
                FILLING_FOK = getattr(mt5, 'SYMBOL_FILLING_FOK', 1)
                FILLING_IOC = getattr(mt5, 'SYMBOL_FILLING_IOC', 2)
                filling_mode = mt5.ORDER_FILLING_IOC  # Default
                if symbol_info.filling_mode & FILLING_FOK:
                    filling_mode = mt5.ORDER_FILLING_FOK
                elif symbol_info.filling_mode & FILLING_IOC:
                    filling_mode = mt5.ORDER_FILLING_IOC
                else:
                    filling_mode = mt5.ORDER_FILLING_RETURN  # Fallback

                request_order = {
                    "action": mt5.TRADE_ACTION_DEAL,
                    "symbol": symbol,
                    "volume": min_volume,
                    "type": order_type,
                    "price": price,
                    "deviation": 20,
                    "magic": 123456,
                    "comment": "Test Order",
                    "type_time": mt5.ORDER_TIME_GTC,
                    "type_filling": filling_mode,
                }

                # If closing a position, include the position ticket
                close_ticket = data.get('close_ticket')
                if close_ticket and direction == 'sell':
                    request_order["position"] = int(close_ticket)
                    request_order["comment"] = "Test Order Close"

                # Send order
                result = mt5.order_send(request_order)
                mt5.shutdown()

                if result.retcode != mt5.TRADE_RETCODE_DONE:
                    return jsonify({
                        'success': False,
                        'error': f'Order failed: {result.comment} (code: {result.retcode})'
                    })

                return jsonify({
                    'success': True,
                    'message': f'{direction.upper()} {min_volume} {symbol} @ {price:.2f}',
                    'ticket': result.order,
                    'volume': min_volume,
                    'price': price
                })

            except ImportError:
                return jsonify({'success': False, 'error': 'MetaTrader5 library not installed'})
            except Exception as e:
                return jsonify({'success': False, 'error': str(e)})

        elif broker.broker_type == 'OKX':
            # OKX order execution
            return jsonify({'success': False, 'error': 'OKX test orders not implemented yet'})

        else:
            return jsonify({'success': False, 'error': f'Test orders not supported for {broker.broker_type}'})

    except Exception as e:
        logger.error(f"Test order error: {e}")
        return jsonify({'success': False, 'error': str(e)})


@app.route('/api/close-position', methods=['POST'])
def api_close_position():
    """Close a position by ticket number"""
    try:
        data = request.get_json()
        ticket = data.get('ticket')
        leg = data.get('leg', 'spot')  # 'spot' or 'futures'

        if not ticket:
            return jsonify({'success': False, 'error': 'No ticket provided'})

        database = get_db()
        spot_broker_id, futures_broker_id = load_active_brokers()

        # Get broker based on leg
        broker_id = spot_broker_id if leg == 'spot' else futures_broker_id

        if not broker_id:
            return jsonify({'success': False, 'error': f'No active {leg} broker selected'})

        broker = database.get_broker(broker_id)
        if not broker:
            return jsonify({'success': False, 'error': f'Broker {broker_id} not found'})

        if broker.broker_type == 'MT5':
            try:
                import MetaTrader5 as mt5

                if not mt5.initialize():
                    return jsonify({'success': False, 'error': 'Failed to initialize MT5'})

                # Find the position by ticket
                positions = mt5.positions_get(ticket=int(ticket))

                if not positions or len(positions) == 0:
                    mt5.shutdown()
                    return jsonify({'success': False, 'error': f'Position with ticket {ticket} not found'})

                position = positions[0]
                symbol = position.symbol
                volume = position.volume
                pos_type = position.type  # 0=BUY, 1=SELL

                # Get current price
                tick = mt5.symbol_info_tick(symbol)
                if not tick:
                    mt5.shutdown()
                    return jsonify({'success': False, 'error': 'Could not get price'})

                # Close in opposite direction
                if pos_type == mt5.POSITION_TYPE_BUY:
                    close_type = mt5.ORDER_TYPE_SELL
                    price = tick.bid
                else:
                    close_type = mt5.ORDER_TYPE_BUY
                    price = tick.ask

                # Determine the correct filling mode based on symbol support
                # SYMBOL_FILLING_FOK=1, SYMBOL_FILLING_IOC=2 (not always in mt5 module)
                FILLING_FOK = getattr(mt5, 'SYMBOL_FILLING_FOK', 1)
                FILLING_IOC = getattr(mt5, 'SYMBOL_FILLING_IOC', 2)
                symbol_info = mt5.symbol_info(symbol)
                filling_mode = mt5.ORDER_FILLING_IOC  # Default
                if symbol_info and symbol_info.filling_mode & FILLING_FOK:
                    filling_mode = mt5.ORDER_FILLING_FOK
                elif symbol_info and symbol_info.filling_mode & FILLING_IOC:
                    filling_mode = mt5.ORDER_FILLING_IOC
                else:
                    filling_mode = mt5.ORDER_FILLING_RETURN  # Fallback

                close_request = {
                    "action": mt5.TRADE_ACTION_DEAL,
                    "symbol": symbol,
                    "volume": volume,
                    "type": close_type,
                    "position": int(ticket),
                    "price": price,
                    "deviation": 20,
                    "magic": 123456,
                    "comment": "Close by Ticket",
                    "type_time": mt5.ORDER_TIME_GTC,
                    "type_filling": filling_mode,
                }

                result = mt5.order_send(close_request)
                mt5.shutdown()

                if result.retcode != mt5.TRADE_RETCODE_DONE:
                    return jsonify({
                        'success': False,
                        'error': f'Close failed: {result.comment} (code: {result.retcode})'
                    })

                return jsonify({
                    'success': True,
                    'message': f'Closed ticket {ticket}: {volume} {symbol} @ {price:.2f}',
                    'closed_ticket': ticket,
                    'close_order': result.order,
                    'volume': volume,
                    'price': price
                })

            except ImportError:
                return jsonify({'success': False, 'error': 'MetaTrader5 library not installed'})
            except Exception as e:
                return jsonify({'success': False, 'error': str(e)})

        else:
            return jsonify({'success': False, 'error': f'Close by ticket not supported for {broker.broker_type}'})

    except Exception as e:
        logger.error(f"Close position error: {e}")
        return jsonify({'success': False, 'error': str(e)})


@app.route('/api/test-order-cycle', methods=['POST'])
def api_test_order_cycle():
    """Test full order cycle: open position, find by ticket, close by ticket, verify closure"""
    try:
        import MetaTrader5 as mt5
        import time

        data = request.get_json() or {}
        test_type = data.get('test_type', 'open_close')

        database = get_db()
        spot_broker_id, futures_broker_id = load_active_brokers()

        if not spot_broker_id:
            return jsonify({'success': False, 'error': 'No active spot broker configured'})

        broker = database.get_broker(spot_broker_id)
        if not broker:
            return jsonify({'success': False, 'error': 'Spot broker not found'})

        if broker.broker_type != 'MT5':
            return jsonify({'success': False, 'error': f'Order cycle test only supports MT5, got {broker.broker_type}'})

        # Initialize MT5
        if not mt5.initialize():
            return jsonify({'success': False, 'error': 'Failed to initialize MT5'})

        symbol = broker.symbol
        symbol_info = mt5.symbol_info(symbol)

        if symbol_info is None:
            mt5.shutdown()
            return jsonify({'success': False, 'error': f'Symbol {symbol} not found'})

        if not symbol_info.visible:
            mt5.symbol_select(symbol, True)

        tick = mt5.symbol_info_tick(symbol)
        if not tick:
            mt5.shutdown()
            return jsonify({'success': False, 'error': 'Could not get price'})

        min_volume = symbol_info.volume_min

        # Determine the correct filling mode based on symbol support
        # SYMBOL_FILLING_FOK=1, SYMBOL_FILLING_IOC=2 (not always in mt5 module)
        FILLING_FOK = getattr(mt5, 'SYMBOL_FILLING_FOK', 1)
        FILLING_IOC = getattr(mt5, 'SYMBOL_FILLING_IOC', 2)
        filling_mode = mt5.ORDER_FILLING_IOC  # Default
        if symbol_info.filling_mode & FILLING_FOK:
            filling_mode = mt5.ORDER_FILLING_FOK
        elif symbol_info.filling_mode & FILLING_IOC:
            filling_mode = mt5.ORDER_FILLING_IOC
        else:
            filling_mode = mt5.ORDER_FILLING_RETURN  # Fallback

        # Step 1: Open position
        open_request = {
            "action": mt5.TRADE_ACTION_DEAL,
            "symbol": symbol,
            "volume": min_volume,
            "type": mt5.ORDER_TYPE_BUY,
            "price": tick.ask,
            "deviation": 20,
            "magic": 987654,
            "comment": "Order Cycle Test",
            "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": filling_mode,
        }

        open_result = mt5.order_send(open_request)

        if open_result.retcode != mt5.TRADE_RETCODE_DONE:
            mt5.shutdown()
            return jsonify({
                'success': False,
                'error': f'Failed to open position: {open_result.comment} (code: {open_result.retcode})'
            })

        logger.info(f"Order cycle test: Opened position, order={open_result.order}")

        # Wait for position to appear
        time.sleep(0.5)

        # Step 2: Find position by magic number to get ticket
        positions = mt5.positions_get(symbol=symbol)
        position_ticket = None
        for pos in positions or []:
            if pos.magic == 987654:
                position_ticket = pos.ticket
                break

        if not position_ticket:
            mt5.shutdown()
            return jsonify({
                'success': False,
                'error': 'Could not find opened position by magic number'
            })

        logger.info(f"Order cycle test: Found position ticket={position_ticket}")

        # Step 3: For ticket_close test, verify we can find by ticket
        found_by_ticket = False
        if test_type == 'ticket_close':
            found_positions = mt5.positions_get(ticket=position_ticket)
            found_by_ticket = found_positions is not None and len(found_positions) == 1
            logger.info(f"Order cycle test: Found by ticket={found_by_ticket}")

        # Step 4: Close by ticket
        tick = mt5.symbol_info_tick(symbol)
        close_request = {
            "action": mt5.TRADE_ACTION_DEAL,
            "symbol": symbol,
            "volume": min_volume,
            "type": mt5.ORDER_TYPE_SELL,
            "position": position_ticket,  # Close by ticket
            "price": tick.bid,
            "deviation": 20,
            "magic": 987654,
            "comment": "Order Cycle Close",
            "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": filling_mode,  # Use same filling mode as open
        }

        close_result = mt5.order_send(close_request)

        if close_result.retcode != mt5.TRADE_RETCODE_DONE:
            mt5.shutdown()
            return jsonify({
                'success': False,
                'error': f'Failed to close position: {close_result.comment} (code: {close_result.retcode})',
                'open_ticket': position_ticket
            })

        logger.info(f"Order cycle test: Closed position, order={close_result.order}")

        # Step 5: Verify closure
        time.sleep(0.5)
        remaining = mt5.positions_get(ticket=position_ticket)
        verified_closed = remaining is None or len(remaining) == 0

        # Get profit from the close deal
        profit = 0
        try:
            from datetime import datetime, timedelta
            deals = mt5.history_deals_get(datetime.now() - timedelta(minutes=1), datetime.now())
            for deal in (deals or []):
                if deal.position_id == position_ticket:
                    profit = deal.profit
                    break
        except:
            pass

        mt5.shutdown()

        logger.info(f"Order cycle test: Complete! verified_closed={verified_closed}, profit={profit}")

        return jsonify({
            'success': True,
            'open_ticket': position_ticket,
            'close_ticket': close_result.order,
            'symbol': symbol,
            'volume': min_volume,
            'profit': profit,
            'found_by_ticket': found_by_ticket if test_type == 'ticket_close' else None,
            'verified_closed': verified_closed
        })

    except ImportError:
        return jsonify({'success': False, 'error': 'MetaTrader5 library not installed'})
    except Exception as e:
        logger.error(f"Order cycle test error: {e}")
        return jsonify({'success': False, 'error': str(e)})


@app.route('/api/broker/positions')
def api_broker_positions():
    """Get current open positions from active broker(s)"""
    try:
        database = get_db()
        spot_broker_id, futures_broker_id = load_active_brokers()

        if not spot_broker_id and not futures_broker_id:
            return jsonify({'success': False, 'error': 'No active brokers configured', 'positions': []})

        all_positions = []
        broker_types = set()

        # Get broker info
        for broker_id in [spot_broker_id, futures_broker_id]:
            if broker_id:
                broker = database.get_broker(broker_id)
                if broker:
                    broker_types.add(broker.broker_type)

        # Fetch from MT5 if any broker uses it
        if 'MT5' in broker_types:
            try:
                import MetaTrader5 as mt5
                if mt5.initialize():
                    positions = mt5.positions_get()
                    mt5.shutdown()
                    if positions:
                        for pos in positions:
                            all_positions.append({
                                'broker': 'MT5',
                                'ticket': pos.ticket,
                                'symbol': pos.symbol,
                                'type': 'BUY' if pos.type == 0 else 'SELL',
                                'volume': pos.volume,
                                'price_open': pos.price_open,
                                'price_current': pos.price_current,
                                'profit': pos.profit,
                                'swap': pos.swap,
                                'time': pos.time,
                                'comment': pos.comment
                            })
            except ImportError:
                pass

        # Fetch from OKX if any broker uses it (placeholder - needs OKX API implementation)
        if 'OKX' in broker_types:
            # TODO: Add OKX position fetching when API is configured
            pass

        return jsonify({'success': True, 'positions': all_positions, 'brokers': list(broker_types)})

    except Exception as e:
        logger.error(f"Broker positions error: {e}")
        return jsonify({'success': False, 'error': str(e), 'positions': []})


@app.route('/api/broker/history')
def api_broker_history():
    """Get trade history from active broker(s)"""
    try:
        from datetime import datetime, timedelta

        database = get_db()
        spot_broker_id, futures_broker_id = load_active_brokers()

        if not spot_broker_id and not futures_broker_id:
            return jsonify({'success': False, 'error': 'No active brokers configured', 'deals': []})

        days = int(request.args.get('days', 30))
        all_deals = []
        broker_types = set()

        # Get broker info
        for broker_id in [spot_broker_id, futures_broker_id]:
            if broker_id:
                broker = database.get_broker(broker_id)
                if broker:
                    broker_types.add(broker.broker_type)

        # Fetch from MT5 if any broker uses it
        if 'MT5' in broker_types:
            try:
                import MetaTrader5 as mt5
                if mt5.initialize():
                    from_date = datetime.now() - timedelta(days=days)
                    to_date = datetime.now()
                    deals = mt5.history_deals_get(from_date, to_date)
                    mt5.shutdown()

                    if deals:
                        for deal in deals:
                            # Include both entry and exit deals
                            all_deals.append({
                                'broker': 'MT5',
                                'ticket': deal.ticket,
                                'order': deal.order,
                                'time': deal.time,
                                'type': 'BUY' if deal.type == 0 else 'SELL',
                                'entry': 'IN' if deal.entry == 0 else 'OUT',
                                'symbol': deal.symbol,
                                'volume': deal.volume,
                                'price': deal.price,
                                'profit': deal.profit,
                                'swap': deal.swap,
                                'commission': deal.commission,
                                'comment': deal.comment
                            })
            except ImportError:
                pass

        # Fetch from OKX if any broker uses it (placeholder - needs OKX API implementation)
        if 'OKX' in broker_types:
            # TODO: Add OKX trade history fetching when API is configured
            pass

        # Sort by time descending (most recent first)
        all_deals.sort(key=lambda x: x['time'], reverse=True)

        return jsonify({'success': True, 'deals': all_deals, 'brokers': list(broker_types)})

    except Exception as e:
        logger.error(f"Broker history error: {e}")
        return jsonify({'success': False, 'error': str(e), 'deals': []})


# Legacy MT5-specific endpoints (redirect to generic)
@app.route('/api/mt5/positions')
def api_mt5_positions():
    """Legacy endpoint - redirects to generic broker positions"""
    return api_broker_positions()


@app.route('/api/mt5/history')
def api_mt5_history():
    """Legacy endpoint - redirects to generic broker history"""
    return api_broker_history()


# ==================== Trade Journal API ====================

@app.route('/api/trade-journal')
def api_trade_journal():
    """Get trade journal data with summary statistics"""
    try:
        database = get_db()
        trades = database.get_trades(limit=500)

        # Convert trades to dict format
        trades_list = []
        total_pnl = 0
        winning_trades = 0
        losing_trades = 0
        returns = []
        total_margin_used = 0

        for trade in trades:
            trade_dict = {
                'trade_id': trade.trade_id,
                'asset': trade.asset,
                'direction': trade.direction,
                'entry_date': trade.entry_date,
                'exit_date': trade.exit_date,
                'days_held': trade.days_held,
                'entry_zscore': trade.entry_zscore,
                'exit_zscore': trade.exit_zscore,
                'spot_pnl': trade.spot_pnl,
                'futures_pnl': trade.futures_pnl,
                'gross_pnl': trade.gross_pnl,
                'swap_cost': trade.swap_cost,
                'commission': trade.commission,
                'spread_cost': trade.spread_cost,
                'net_pnl': trade.net_pnl,
                'return_pct': trade.return_pct,
                'lot_size': trade.lot_size,
                'status': trade.status
            }
            trades_list.append(trade_dict)

            # Calculate summary stats for closed trades
            if trade.status == 'CLOSED':
                net_pnl = trade.net_pnl or 0
                total_pnl += net_pnl

                if net_pnl > 0:
                    winning_trades += 1
                else:
                    losing_trades += 1

                # Track return for Sharpe calculation
                if trade.return_pct is not None:
                    returns.append(trade.return_pct)

                # Estimate margin used (simple approximation)
                if trade.lot_size and trade.entry_spot_price:
                    margin = trade.entry_spot_price * trade.lot_size * 100 / 100  # Assuming 1:100 leverage
                    total_margin_used += margin

        total_trades = winning_trades + losing_trades

        # Calculate cumulative return
        cumulative_return = (total_pnl / total_margin_used * 100) if total_margin_used > 0 else 0

        # Calculate Sharpe ratio
        sharpe_ratio = 0.0
        if len(returns) >= 2:
            import statistics
            mean_return = statistics.mean(returns)
            std_return = statistics.stdev(returns)
            if std_return > 0:
                sharpe_ratio = mean_return / std_return

        # Calculate drawdown from cumulative returns
        max_drawdown = 0.0
        current_drawdown = 0.0
        if len(returns) > 0:
            cumulative = 0.0
            peak = 0.0
            for ret in returns:
                cumulative += ret
                if cumulative > peak:
                    peak = cumulative
                dd = peak - cumulative
                if dd > max_drawdown:
                    max_drawdown = dd
            current_drawdown = peak - cumulative if peak > 0 else 0

        summary = {
            'total_trades': total_trades,
            'winning_trades': winning_trades,
            'losing_trades': losing_trades,
            'win_rate': (winning_trades / total_trades * 100) if total_trades > 0 else 0,
            'total_pnl': total_pnl,
            'cumulative_return': cumulative_return,
            'sharpe_ratio': sharpe_ratio,
            'max_drawdown': max_drawdown,
            'current_drawdown': current_drawdown
        }

        return jsonify({
            'success': True,
            'trades': trades_list,
            'summary': summary
        })

    except Exception as e:
        logger.error(f"Trade journal error: {e}")
        return jsonify({'success': False, 'error': str(e), 'trades': [], 'summary': {}})


@app.route('/api/trade-journal/csv')
def api_trade_journal_csv():
    """Download trade journal as CSV"""
    try:
        import csv
        import io
        from datetime import datetime

        database = get_db()
        trades = database.get_trades(limit=1000)

        output = io.StringIO()
        writer = csv.writer(output)

        # Header
        writer.writerow([
            'Trade ID', 'Asset', 'Direction', 'Lots', 'Entry Date', 'Exit Date',
            'Days Held', 'Entry Z', 'Exit Z', 'Spot P&L', 'Futures P&L',
            'Gross P&L', 'Swap', 'Commission', 'Spread', 'Net P&L', 'Return %', 'Status'
        ])

        # Data rows
        for trade in trades:
            writer.writerow([
                trade.trade_id,
                trade.asset,
                trade.direction,
                trade.lot_size,
                trade.entry_date,
                trade.exit_date or '',
                trade.days_held or '',
                f"{trade.entry_zscore:.2f}" if trade.entry_zscore else '',
                f"{trade.exit_zscore:.2f}" if trade.exit_zscore else '',
                f"{trade.spot_pnl:.2f}" if trade.spot_pnl else '0.00',
                f"{trade.futures_pnl:.2f}" if trade.futures_pnl else '0.00',
                f"{trade.gross_pnl:.2f}" if trade.gross_pnl else '0.00',
                f"{trade.swap_cost:.2f}" if trade.swap_cost else '0.00',
                f"{trade.commission:.2f}" if trade.commission else '0.00',
                f"{trade.spread_cost:.2f}" if trade.spread_cost else '0.00',
                f"{trade.net_pnl:.2f}" if trade.net_pnl else '0.00',
                f"{trade.return_pct:.2f}" if trade.return_pct else '0.00',
                trade.status
            ])

        from flask import Response
        return Response(
            output.getvalue(),
            mimetype='text/csv',
            headers={'Content-Disposition': f'attachment; filename=trade_journal_{datetime.now().strftime("%Y%m%d_%H%M%S")}.csv'}
        )

    except Exception as e:
        logger.error(f"Trade journal CSV error: {e}")
        return jsonify({'success': False, 'error': str(e)})


# ==================== SocketIO Events ====================

@socketio.on('disconnect')
def handle_disconnect():
    """Handle client disconnection"""
    logger.info("Client disconnected")


@socketio.on('request_status')
def handle_request_status():
    """Send current status to client"""
    if engine:
        emit('status', engine.get_status())


# ==================== Background Price Streaming ====================

price_streaming_active = False

def start_price_streaming():
    """Start background price streaming from active brokers"""
    global price_streaming_active

    if price_streaming_active:
        logger.info("[PRICES] Price streaming already active")
        return

    price_streaming_active = True
    logger.info("[PRICES] Starting price streaming thread")

    def stream_prices():
        global price_streaming_active
        import time
        import numpy as np
        from collections import deque

        log_counter = 0
        spread_history = deque(maxlen=2000)  # Store spread history for z-score calculation
        last_save_time = time.time()
        history_loaded = False
        mt5_initialized = False  # Track MT5 connection state
        cached_leverage = 100  # Cache leverage to avoid repeated MT5 calls

        # Load spread history from database on startup (handles reconnection)
        try:
            database = get_db()
            config = database.get_config()
            lookback_period = config.lookback_period if config else 90
            lookback_unit = config.lookback_unit if config else 'minutes'

            # Calculate max age based on lookback
            if lookback_unit == 'days':
                max_age_hours = int(lookback_period * 24 * 1.1)  # +10% buffer
            else:
                max_age_hours = max(2, int(lookback_period / 60 * 1.1) + 1)

            # Load historical spreads from database
            history = database.get_price_history('ACTIVE', limit=lookback_period, max_age_hours=max_age_hours)
            if history:
                # history is ordered DESC, reverse to get chronological order
                for row in reversed(history):
                    spread_history.append(row[0])  # spread column
                logger.info(f"[PRICES] Loaded {len(history)} historical spreads from database (no 90min wait needed)")
                history_loaded = True
        except Exception as e:
            logger.error(f"[PRICES] Failed to load spread history: {e}")

        while price_streaming_active:
            try:
                database = get_db()

                # Load active brokers from JSON file (more reliable than database)
                spot_broker_id, futures_broker_id = load_active_brokers()

                # Log every 10 seconds
                log_counter += 1
                if log_counter % 10 == 1:
                    logger.info(f"[PRICES] Active brokers from file - Spot: {spot_broker_id}, Futures: {futures_broker_id}")

                if not spot_broker_id or not futures_broker_id:
                    time.sleep(1)
                    continue

                spot_broker = database.get_broker(spot_broker_id)
                futures_broker = database.get_broker(futures_broker_id)

                if not spot_broker or not futures_broker:
                    logger.warning(f"[PRICES] Broker not found - Spot: {spot_broker}, Futures: {futures_broker}")
                    time.sleep(2)
                    continue

                spot_bid, spot_ask = 0, 0
                futures_bid, futures_ask = 0, 0

                # Fetch MT5 prices - maintain persistent connection
                if spot_broker.broker_type == 'MT5' or futures_broker.broker_type == 'MT5':
                    try:
                        import MetaTrader5 as mt5

                        # Initialize MT5 once and keep connection open
                        if not mt5_initialized:
                            if mt5.initialize():
                                mt5_initialized = True
                                logger.info("[PRICES] MT5 connection established (persistent)")
                                # Get leverage once at startup
                                account = mt5.account_info()
                                if account:
                                    cached_leverage = account.leverage
                                    logger.info(f"[PRICES] Cached leverage: {cached_leverage}")
                            else:
                                logger.warning("[PRICES] MT5 initialization failed, will retry")
                                time.sleep(2)
                                continue

                        # Get spot price
                        if spot_broker.broker_type == 'MT5':
                            tick = mt5.symbol_info_tick(spot_broker.symbol)
                            if tick:
                                spot_bid = tick.bid
                                spot_ask = tick.ask
                            else:
                                # Connection might be lost, try to reinitialize
                                mt5_initialized = False
                                continue

                        # Get futures price
                        if futures_broker.broker_type == 'MT5':
                            tick = mt5.symbol_info_tick(futures_broker.symbol)
                            if tick:
                                futures_bid = tick.bid
                                futures_ask = tick.ask
                            else:
                                # Connection might be lost, try to reinitialize
                                mt5_initialized = False
                                continue

                        # DO NOT shutdown MT5 here - keep connection persistent

                    except ImportError:
                        pass
                    except Exception as e:
                        logger.error(f"MT5 price fetch error: {e}")
                        mt5_initialized = False  # Will reinitialize on next loop

                # Emit price update
                if spot_bid > 0 or futures_bid > 0:
                    spot_mid = (spot_bid + spot_ask) / 2 if spot_bid > 0 else 0
                    futures_mid = (futures_bid + futures_ask) / 2 if futures_bid > 0 else 0
                    spread = futures_mid - spot_mid if spot_mid > 0 and futures_mid > 0 else 0

                    # Add spread to history for z-score calculation
                    if spread != 0:
                        spread_history.append(spread)

                        # Save to database every 60 seconds for persistence (handles reconnection)
                        current_time = time.time()
                        if current_time - last_save_time >= 60:
                            try:
                                database.save_price_data('ACTIVE', spot_mid, futures_mid, spread)
                                last_save_time = current_time
                            except Exception as e:
                                logger.error(f"[PRICES] Failed to save price data: {e}")

                    # Get config for calculations
                    config = database.get_config()
                    lookback_period = config.lookback_period if config else 90

                    # Calculate mean, std, and z-score from spread history
                    mean_val = 0.0
                    std_val = 0.0
                    zscore = None  # None until lookback is complete
                    lookback_complete = len(spread_history) >= lookback_period

                    if lookback_complete:
                        # Use most recent lookback_period points
                        history_list = list(spread_history)[-lookback_period:]
                        mean_val = float(np.mean(history_list))
                        std_val = float(np.std(history_list))

                        if std_val > 0:
                            zscore = (spread - mean_val) / std_val

                    # === Basis Premium Calculation ===
                    # Read swap from spot broker (CFDs charge swap on spot position)
                    # Read expiry from futures broker
                    swap_charge = getattr(spot_broker, 'swap_charge', 0.0) or config.swap_charge if config else 0.0
                    lot_size = getattr(spot_broker, 'contract_size', None) or (config.contract_size if config else 100.0)
                    futures_expiry_str = getattr(futures_broker, 'futures_expiry', None) or (config.futures_expiry if config else None)

                    # Parse expiry and calculate days to expiry
                    _, days_to_expiry = parse_futures_expiry(futures_expiry_str)
                    time_to_expiry = days_to_expiry / 365.25 if days_to_expiry > 0 else 0

                    # Calculate fair value basis from swap cost
                    swap_futures_price, swap_basis, annual_swap_rate = calculate_swap_basis(
                        spot_mid, swap_charge, lot_size, time_to_expiry
                    )

                    # Calculate premium (difference between actual and fair value)
                    swap_diff = spread - swap_basis if swap_basis != 0 else 0
                    swap_premium_pct = ((spread - swap_basis) / abs(swap_basis)) * 100 if abs(swap_basis) > 0.001 else 0

                    # === Margin Requirements ===
                    user_lot_size = config.lot_size if config else 0.1
                    # Use cached leverage from persistent MT5 connection
                    leverage = cached_leverage

                    margin_data = calculate_margin_requirements(
                        spot_mid, futures_mid, lot_size, leverage, user_lot_size
                    )

                    # === STD Filter Calculations ===
                    std_filter_data = None
                    entry_exit_bands = None
                    hurst_data = None

                    if lookback_complete and std_val > 0:
                        # Get bid-ask spreads in cents for cost calculation
                        spot_spread_cents = (spot_ask - spot_bid) * 100 if spot_bid > 0 else None
                        futures_spread_cents = (futures_ask - futures_bid) * 100 if futures_bid > 0 else None

                        # Calculate STD filter (min profitable std)
                        std_filter_data = calculate_min_profitable_std(
                            config, std_val,
                            spot_spread_cents, futures_spread_cents
                        )

                        # Calculate entry/exit bands
                        entry_exit_bands = calculate_entry_exit_bands(mean_val, std_val, config)

                        # Calculate Hurst exponent for regime detection
                        if config and config.hurst_enabled:
                            hurst_value, hurst_regime = calculate_hurst_exponent(
                                list(spread_history), min_points=20
                            )
                            hurst_data = {
                                'value': hurst_value,
                                'regime': hurst_regime,
                                'threshold': config.hurst_threshold if config else 0.5
                            }

                    # === AUTO TRADING SIGNAL GENERATION ===
                    if config and config.algo_enabled and zscore is not None and auto_trader:
                        entry_threshold = config.entry_std_dev
                        exit_threshold = config.exit_std_dev
                        exit_opposite = config.exit_at_opposite_sd
                        stop_loss_threshold = config.stop_loss_std_dev

                        # Create a simple signal object for AutoTrader
                        class Signal:
                            def __init__(self, signal_type, zscore, spread):
                                self.signal_type = signal_type
                                self.zscore = zscore
                                self.spread = spread

                        # Check for entry signals (no position open)
                        if not auto_trader.has_position:
                            if zscore >= entry_threshold:
                                # Z-score high - short the spread
                                signal = Signal('ENTRY_SHORT', zscore, spread)
                                logger.info(f"[SIGNAL] ENTRY_SHORT triggered: z={zscore:.2f} >= {entry_threshold}")
                                auto_trader.handle_signal(signal)
                                socketio.emit('signal', {'type': 'ENTRY_SHORT', 'zscore': zscore})

                            elif zscore <= -entry_threshold:
                                # Z-score low - long the spread
                                signal = Signal('ENTRY_LONG', zscore, spread)
                                logger.info(f"[SIGNAL] ENTRY_LONG triggered: z={zscore:.2f} <= -{entry_threshold}")
                                auto_trader.handle_signal(signal)
                                socketio.emit('signal', {'type': 'ENTRY_LONG', 'zscore': zscore})

                        # Check for exit signals (position open)
                        else:
                            should_exit = False
                            exit_reason = None

                            # Stop loss check
                            if abs(zscore) >= stop_loss_threshold:
                                should_exit = True
                                exit_reason = 'STOP_LOSS'

                            # Exit at opposite SD (if configured)
                            elif exit_opposite > 0:
                                if auto_trader.position_direction == 'SHORT' and zscore <= -exit_opposite:
                                    should_exit = True
                                    exit_reason = 'EXIT'
                                elif auto_trader.position_direction == 'LONG' and zscore >= exit_opposite:
                                    should_exit = True
                                    exit_reason = 'EXIT'

                            # Normal exit at mean
                            elif abs(zscore) <= exit_threshold:
                                should_exit = True
                                exit_reason = 'EXIT'

                            if should_exit:
                                signal = Signal(exit_reason, zscore, spread)
                                logger.info(f"[SIGNAL] {exit_reason} triggered: z={zscore:.2f}")
                                auto_trader.handle_signal(signal)
                                socketio.emit('signal', {'type': exit_reason, 'zscore': zscore})

                    socketio.emit('tick', {
                        'spot_bid': spot_bid,
                        'spot_ask': spot_ask,
                        'futures_bid': futures_bid,
                        'futures_ask': futures_ask,
                        'spread': spread,
                        'zscore': zscore,
                        'mean': mean_val if lookback_complete else None,
                        'std': std_val if lookback_complete else None,
                        'history_count': len(spread_history),
                        'lookback_required': lookback_period,
                        'lookback_complete': lookback_complete,
                        # Basis Premium Data
                        'days_to_expiry': round(days_to_expiry, 1) if days_to_expiry > 0 else None,
                        'swap_charge': swap_charge,
                        'swap_basis': round(swap_basis, 2) if swap_basis != 0 else None,
                        'swap_diff': round(swap_diff, 2) if swap_diff != 0 else None,
                        'swap_premium_pct': round(swap_premium_pct, 1) if swap_premium_pct != 0 else None,
                        # Margin Data
                        'margin': margin_data,
                        # STD Filter Data (from trading_portal.py logic)
                        'std_filter': std_filter_data,
                        # Entry/Exit Bands
                        'bands': entry_exit_bands,
                        # Hurst Exponent (regime detection)
                        'hurst': hurst_data
                    })

                time.sleep(0.3)  # Update every 0.3 seconds

            except Exception as e:
                logger.error(f"Price streaming error: {e}")
                mt5_initialized = False  # Reset on error to trigger reconnection
                time.sleep(1)

        # Cleanup MT5 connection when streaming stops
        if mt5_initialized:
            try:
                import MetaTrader5 as mt5
                mt5.shutdown()
                logger.info("[PRICES] MT5 connection closed on streaming stop")
            except:
                pass

    thread = threading.Thread(target=stream_prices, daemon=True)
    thread.start()
    logger.info("Price streaming started")


@socketio.on('connect')
def handle_connect():
    """Handle client connection and start price streaming"""
    logger.info("Client connected")
    start_price_streaming()
    if engine:
        emit('status', engine.get_status())


# ==================== Multi-Broker API ====================

@app.route('/api/multi-broker/status')
def api_multi_broker_status():
    """Get multi-broker coordinator status"""
    global multi_broker

    if multi_broker is None:
        return jsonify({
            'initialized': False,
            'mode': None,
            'message': 'Multi-broker coordinator not initialized'
        })

    return jsonify({
        'initialized': True,
        'mode': multi_broker.mode.value,
        'is_single_broker': multi_broker.is_single_broker,
        'config': multi_broker.config
    })


@app.route('/api/multi-broker/initialize', methods=['POST'])
def api_multi_broker_initialize():
    """Initialize multi-broker coordinator from database configuration"""
    global multi_broker

    try:
        # Create coordinator from database
        multi_broker = create_coordinator_from_database("trading.db")

        # Initialize in background
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

        try:
            success = loop.run_until_complete(multi_broker.initialize())

            if success:
                return jsonify({
                    'success': True,
                    'mode': multi_broker.mode.value,
                    'is_single_broker': multi_broker.is_single_broker,
                    'message': f'Initialized in {multi_broker.mode.value} mode'
                })
            else:
                return jsonify({
                    'success': False,
                    'error': 'Failed to connect to brokers'
                })
        finally:
            loop.close()

    except Exception as e:
        logger.error(f"Multi-broker init error: {e}")
        return jsonify({
            'success': False,
            'error': str(e)
        })


@app.route('/api/multi-broker/shutdown', methods=['POST'])
def api_multi_broker_shutdown():
    """Shutdown multi-broker coordinator"""
    global multi_broker

    if multi_broker is None:
        return jsonify({'success': True, 'message': 'Not initialized'})

    try:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

        try:
            loop.run_until_complete(multi_broker.shutdown())
            multi_broker = None
            return jsonify({'success': True, 'message': 'Coordinator shut down'})
        finally:
            loop.close()

    except Exception as e:
        logger.error(f"Multi-broker shutdown error: {e}")
        return jsonify({'success': False, 'error': str(e)})


@app.route('/api/multi-broker/execute-basis-trade', methods=['POST'])
def api_execute_basis_trade():
    """
    Execute a coordinated basis trade (spot + futures).

    Request body:
    {
        "signal_type": "SELL_BASIS" or "BUY_BASIS",
        "spot_symbol": "XAUUSD_",
        "futures_symbol": "GC1225",
        "volume": 0.1,
        "atomic": true  // Reverse spot if futures fails
    }
    """
    global multi_broker

    if multi_broker is None:
        return jsonify({
            'success': False,
            'error': 'Multi-broker coordinator not initialized. Call /api/multi-broker/initialize first.'
        })

    try:
        data = request.get_json()
        signal_type = data.get('signal_type', 'SELL_BASIS')
        spot_symbol = data.get('spot_symbol')
        futures_symbol = data.get('futures_symbol')
        volume = float(data.get('volume', 0.1))
        atomic = data.get('atomic', True)

        if not spot_symbol or not futures_symbol:
            return jsonify({
                'success': False,
                'error': 'spot_symbol and futures_symbol are required'
            })

        # Execute in async context
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

        try:
            result = loop.run_until_complete(
                multi_broker.execute_basis_trade(
                    signal_type=signal_type,
                    spot_symbol=spot_symbol,
                    futures_symbol=futures_symbol,
                    volume=volume,
                    atomic=atomic
                )
            )

            response = {
                'success': result.success,
                'execution_time_ms': result.execution_time_ms,
                'mode': multi_broker.mode.value
            }

            if result.spot_result:
                response['spot'] = {
                    'success': result.spot_result.success,
                    'symbol': result.spot_result.symbol,
                    'side': result.spot_result.side,
                    'volume': result.spot_result.volume,
                    'ticket': result.spot_result.ticket,
                    'price': result.spot_result.price,
                    'error': result.spot_result.error
                }

            if result.futures_result:
                response['futures'] = {
                    'success': result.futures_result.success,
                    'symbol': result.futures_result.symbol,
                    'side': result.futures_result.side,
                    'volume': result.futures_result.volume,
                    'ticket': result.futures_result.ticket,
                    'price': result.futures_result.price,
                    'error': result.futures_result.error
                }

            if result.error:
                response['error'] = result.error

            return jsonify(response)

        finally:
            loop.close()

    except Exception as e:
        logger.error(f"Basis trade execution error: {e}")
        return jsonify({'success': False, 'error': str(e)})


@app.route('/api/multi-broker/ticks')
def api_multi_broker_ticks():
    """Get latest tick data from all configured symbols"""
    global multi_broker

    if multi_broker is None:
        return jsonify({
            'success': False,
            'error': 'Multi-broker coordinator not initialized'
        })

    try:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

        try:
            ticks = loop.run_until_complete(multi_broker.get_all_ticks())

            result = {}
            for symbol, tick in ticks.items():
                result[symbol] = {
                    'bid': tick.bid,
                    'ask': tick.ask,
                    'timestamp': tick.timestamp.isoformat()
                }

            return jsonify({
                'success': True,
                'ticks': result,
                'mode': multi_broker.mode.value
            })
        finally:
            loop.close()

    except Exception as e:
        logger.error(f"Get ticks error: {e}")
        return jsonify({'success': False, 'error': str(e)})


@app.route('/api/multi-broker/close-basis-position', methods=['POST'])
def api_close_basis_position():
    """
    Close a basis position (both spot and futures legs).

    Request body:
    {
        "spot_ticket": 12345,
        "futures_ticket": 12346,
        "volume": 0.1
    }
    """
    global multi_broker

    if multi_broker is None:
        return jsonify({
            'success': False,
            'error': 'Multi-broker coordinator not initialized'
        })

    try:
        data = request.get_json()
        spot_ticket = int(data.get('spot_ticket'))
        futures_ticket = int(data.get('futures_ticket'))
        volume = float(data.get('volume', 0.1))

        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

        try:
            result = loop.run_until_complete(
                multi_broker.close_basis_position(
                    spot_ticket=spot_ticket,
                    futures_ticket=futures_ticket,
                    volume=volume
                )
            )

            return jsonify({
                'success': result.success,
                'execution_time_ms': result.execution_time_ms,
                'spot_closed': result.spot_result.success if result.spot_result else False,
                'futures_closed': result.futures_result.success if result.futures_result else False,
                'error': result.error
            })
        finally:
            loop.close()

    except Exception as e:
        logger.error(f"Close basis position error: {e}")
        return jsonify({'success': False, 'error': str(e)})


# ==================== Entry Point ====================

def run_server(host: str = '0.0.0.0', port: int = 5000, debug: bool = False):
    """Run the Flask server"""
    init_app()
    # Disable reloader when using eventlet - it doesn't work properly on Windows
    # and causes the server to not bind to the port
    use_reloader = False if _async_mode == 'eventlet' else debug
    print(f" * Running on http://{host}:{port} (async_mode={_async_mode})")
    socketio.run(app, host=host, port=port, debug=debug,
                 use_reloader=use_reloader, allow_unsafe_werkzeug=True)


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO)
    run_server(debug=True)
