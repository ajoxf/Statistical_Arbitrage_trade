"""
Trading Engine

Core trading logic for the Multi-Broker Arbitrage System.
Manages signal generation, order execution, and position tracking.
"""

import asyncio
import logging
from enum import Enum
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable, Optional, List, Dict, Any

logger = logging.getLogger(__name__)


class EngineState(Enum):
    """Trading engine states"""
    STOPPED = "STOPPED"
    STARTING = "STARTING"
    RUNNING = "RUNNING"
    STOPPING = "STOPPING"
    ERROR = "ERROR"


@dataclass
class MarketData:
    """Real-time market data snapshot"""
    spot_bid: float = 0.0
    spot_ask: float = 0.0
    futures_bid: float = 0.0
    futures_ask: float = 0.0
    spread: float = 0.0
    zscore: float = 0.0
    timestamp: Optional[datetime] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            'spot_bid': self.spot_bid,
            'spot_ask': self.spot_ask,
            'futures_bid': self.futures_bid,
            'futures_ask': self.futures_ask,
            'spread': self.spread,
            'zscore': self.zscore,
            'timestamp': self.timestamp.isoformat() if self.timestamp else None
        }


@dataclass
class Signal:
    """Trading signal"""
    signal_type: str  # 'ENTRY_LONG', 'ENTRY_SHORT', 'EXIT', 'STOP_LOSS'
    zscore: float = 0.0
    spread: float = 0.0
    timestamp: Optional[datetime] = None
    reason: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            'signal_type': self.signal_type,
            'zscore': self.zscore,
            'spread': self.spread,
            'timestamp': self.timestamp.isoformat() if self.timestamp else None,
            'reason': self.reason
        }


class TradingEngine:
    """
    Main trading engine for statistical arbitrage.

    Coordinates between brokers, generates signals based on z-score,
    and manages position entry/exit logic.
    """

    def __init__(self, db_path: str = "trading.db"):
        self.db_path = db_path
        self._state = EngineState.STOPPED
        self._running = False

        # Callbacks
        self._tick_callbacks: List[Callable] = []
        self._signal_callbacks: List[Callable] = []
        self._trade_callbacks: List[Callable] = []

        # Market data
        self._current_market = MarketData()
        self._price_history: List[float] = []

        # Position tracking
        self._has_position = False
        self._position_direction: Optional[str] = None

        # Configuration (loaded from database)
        self._config: Optional[Any] = None

        logger.info("Trading engine initialized")

    @property
    def state(self) -> EngineState:
        """Get current engine state"""
        return self._state

    def on_tick(self, callback: Callable):
        """Register tick callback"""
        self._tick_callbacks.append(callback)

    def on_signal(self, callback: Callable):
        """Register signal callback"""
        self._signal_callbacks.append(callback)

    def on_trade(self, callback: Callable):
        """Register trade callback"""
        self._trade_callbacks.append(callback)

    def set_position_state(self, has_position: bool, direction: Optional[str] = None):
        """Update engine position state (called by AutoTrader after trade execution)"""
        self._has_position = has_position
        self._position_direction = direction
        logger.info(f"Engine position state updated: has_position={has_position}, direction={direction}")

    async def initialize(self):
        """Initialize the trading engine"""
        self._state = EngineState.STARTING
        logger.info("Initializing trading engine...")

        try:
            # Import here to avoid circular imports
            import sys
            from pathlib import Path
            sys.path.insert(0, str(Path(__file__).parent.parent))

            from database.manager import DatabaseManager

            # Load configuration from database
            db = DatabaseManager(self.db_path)
            db.initialize()
            self._config = db.get_config()

            logger.info(f"Loaded config: algo_enabled={self._config.algo_enabled}")

            # Load open position state from database
            try:
                conn = db._get_connection()
                cursor = conn.cursor()
                cursor.execute('''
                    SELECT trade_id, position_direction FROM trades
                    WHERE status = 'OPEN'
                    ORDER BY entry_date DESC LIMIT 1
                ''')
                row = cursor.fetchone()
                if row:
                    self._has_position = True
                    self._position_direction = row['position_direction']
                    logger.info(f"Engine loaded open position: direction={self._position_direction}")
                else:
                    self._has_position = False
                    self._position_direction = None
                    logger.info("Engine: No open position found")
            except Exception as e:
                logger.warning(f"Could not load position state: {e}")
                self._has_position = False
                self._position_direction = None

            self._state = EngineState.STOPPED
            logger.info("Trading engine initialized successfully")

        except Exception as e:
            logger.error(f"Failed to initialize engine: {e}")
            self._state = EngineState.ERROR
            raise

    async def start(self):
        """Start the trading engine"""
        if self._state == EngineState.RUNNING:
            logger.warning("Engine already running")
            return

        self._state = EngineState.RUNNING
        self._running = True
        logger.info("Trading engine started")

        # Main trading loop
        try:
            while self._running:
                await self._tick()
                await asyncio.sleep(0.5)  # 500ms tick interval

        except asyncio.CancelledError:
            logger.info("Engine loop cancelled")
        except Exception as e:
            logger.error(f"Engine error: {e}")
            self._state = EngineState.ERROR
        finally:
            self._state = EngineState.STOPPED

    async def stop(self):
        """Stop the trading engine"""
        logger.info("Stopping trading engine...")
        self._running = False
        self._state = EngineState.STOPPING

        # Allow loop to exit
        await asyncio.sleep(0.1)

        self._state = EngineState.STOPPED
        logger.info("Trading engine stopped")

    def reload_config(self):
        """Reload configuration from database"""
        try:
            from database.manager import DatabaseManager

            db = DatabaseManager(self.db_path)
            self._config = db.get_config()
            logger.info(f"Config reloaded: algo_enabled={self._config.algo_enabled}")

        except Exception as e:
            logger.error(f"Failed to reload config: {e}")

    def get_status(self) -> Dict[str, Any]:
        """Get current engine status"""
        return {
            'state': self._state.value,
            'config': self._config.to_dict() if self._config else {},
            'market': self._current_market.to_dict(),
            'position': {
                'has_position': self._has_position,
                'direction': self._position_direction
            }
        }

    async def _tick(self):
        """Process one market tick"""
        try:
            # This is a placeholder - real implementation would fetch from brokers
            # For now, just update timestamp
            self._current_market.timestamp = datetime.now()

            # Notify callbacks
            for callback in self._tick_callbacks:
                try:
                    callback(self._current_market)
                except Exception as e:
                    logger.error(f"Tick callback error: {e}")

            # Generate signals if algo is enabled
            if self._config and self._config.algo_enabled:
                await self._check_signals()

        except Exception as e:
            logger.error(f"Tick processing error: {e}")

    async def _check_signals(self):
        """Check for trading signals based on z-score"""
        if not self._config:
            return

        zscore = self._current_market.zscore

        # Entry signals
        if not self._has_position:
            if zscore >= self._config.entry_std_dev:
                # Z-score is high - spread is above mean
                # Short the spread (sell futures, buy spot)
                signal = Signal(
                    signal_type='ENTRY_SHORT',
                    zscore=zscore,
                    spread=self._current_market.spread,
                    timestamp=datetime.now(),
                    reason=f"Z-score {zscore:.2f} >= {self._config.entry_std_dev} (entry threshold)"
                )
                await self._emit_signal(signal)

            elif zscore <= -self._config.entry_std_dev:
                # Z-score is low - spread is below mean
                # Long the spread (buy futures, sell spot)
                signal = Signal(
                    signal_type='ENTRY_LONG',
                    zscore=zscore,
                    spread=self._current_market.spread,
                    timestamp=datetime.now(),
                    reason=f"Z-score {zscore:.2f} <= -{self._config.entry_std_dev} (entry threshold)"
                )
                await self._emit_signal(signal)

        # Exit signals
        else:
            # Exit at mean reversion
            if abs(zscore) <= self._config.exit_std_dev:
                signal = Signal(
                    signal_type='EXIT',
                    zscore=zscore,
                    spread=self._current_market.spread,
                    timestamp=datetime.now(),
                    reason=f"Z-score {zscore:.2f} returned to mean (exit threshold: {self._config.exit_std_dev})"
                )
                await self._emit_signal(signal)

            # Stop loss
            elif abs(zscore) >= self._config.stop_loss_std_dev:
                signal = Signal(
                    signal_type='STOP_LOSS',
                    zscore=zscore,
                    spread=self._current_market.spread,
                    timestamp=datetime.now(),
                    reason=f"Z-score {zscore:.2f} hit stop loss (threshold: {self._config.stop_loss_std_dev})"
                )
                await self._emit_signal(signal)

    async def _emit_signal(self, signal: Signal):
        """Emit trading signal to callbacks"""
        logger.info(f"Signal: {signal.signal_type} - {signal.reason}")

        for callback in self._signal_callbacks:
            try:
                callback(signal)
            except Exception as e:
                logger.error(f"Signal callback error: {e}")

    def update_market_data(self, spot_bid: float, spot_ask: float,
                           futures_bid: float, futures_ask: float):
        """Update market data from external source"""
        spot_mid = (spot_bid + spot_ask) / 2
        futures_mid = (futures_bid + futures_ask) / 2

        self._current_market.spot_bid = spot_bid
        self._current_market.spot_ask = spot_ask
        self._current_market.futures_bid = futures_bid
        self._current_market.futures_ask = futures_ask
        self._current_market.spread = futures_mid - spot_mid
        self._current_market.timestamp = datetime.now()

        # Update price history for z-score calculation
        self._price_history.append(self._current_market.spread)

        # Keep only lookback period of history
        if self._config:
            max_history = self._config.lookback_period * 60  # Convert to approximate ticks
            if len(self._price_history) > max_history:
                self._price_history = self._price_history[-max_history:]

        # Calculate z-score
        if len(self._price_history) >= 20:  # Minimum history for meaningful stats
            import numpy as np
            spreads = np.array(self._price_history)
            mean = np.mean(spreads)
            std = np.std(spreads)

            if std > 0:
                self._current_market.zscore = (self._current_market.spread - mean) / std
            else:
                self._current_market.zscore = 0.0
