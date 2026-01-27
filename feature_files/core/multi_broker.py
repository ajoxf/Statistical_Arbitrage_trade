"""
Multi-Broker Coordinator

Coordinates trading across multiple brokers for basis trading strategy.

Architecture:
- Single broker mode: No multiprocessing, direct MT5 connection (zero overhead)
- Multi-broker mode: Separate process per broker, IPC-based communication

The system automatically detects the appropriate mode based on configuration.
"""

import asyncio
import logging
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from multiprocessing import Process, Queue
from typing import Dict, List, Optional, Callable, Any, Tuple
import threading

from .ipc import IPCManager, IPCMessage, MessageType, CommandType

logger = logging.getLogger(__name__)


class BrokerRole(Enum):
    """Role of broker in the trading strategy"""
    SPOT = "SPOT"
    FUTURES = "FUTURES"
    UNIFIED = "UNIFIED"  # Same broker handles both


class ExecutionMode(Enum):
    """Execution mode for the coordinator"""
    SINGLE_PROCESS = "SINGLE_PROCESS"  # Same broker for spot/futures
    MULTI_PROCESS = "MULTI_PROCESS"    # Different brokers


class OrderSide(Enum):
    """Order side"""
    BUY = "BUY"
    SELL = "SELL"


@dataclass
class BrokerConfig:
    """Configuration for a single broker"""
    broker_id: str
    name: str
    role: str  # SPOT, FUTURES, or UNIFIED
    backend_type: str  # MT5, OKX, FIX, etc.

    # Symbols
    symbol: str = ""
    spot_symbol: str = ""
    futures_symbol: str = ""

    # MT5 specific
    mt5_path: str = ""
    mt5_account: int = 0
    mt5_server: str = ""
    mt5_password: str = ""

    # OKX specific
    okx_api_key: str = ""
    okx_api_secret: str = ""
    okx_passphrase: str = ""
    okx_simulated: bool = True
    okx_account_type: str = "spot"

    # FIX specific
    fix_host: str = ""
    fix_port: int = 0
    fix_sender_comp: str = ""
    fix_target_comp: str = ""

    # Common settings
    contract_size: float = 100.0
    commission_per_lot: float = 0.0
    min_volume: float = 0.01

    # Unified mode - one broker for both legs
    unified_mode: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return {
            'broker_id': self.broker_id,
            'name': self.name,
            'role': self.role,
            'backend_type': self.backend_type,
            'symbol': self.symbol,
            'spot_symbol': self.spot_symbol,
            'futures_symbol': self.futures_symbol,
            'unified_mode': self.unified_mode,
            'contract_size': self.contract_size
        }


@dataclass
class TradeResult:
    """Result of a trade execution"""
    success: bool
    broker_id: str
    symbol: str
    side: str
    volume: float
    ticket: Optional[int] = None
    price: Optional[float] = None
    error: Optional[str] = None
    timestamp: datetime = field(default_factory=datetime.now)


@dataclass
class BasisTradeResult:
    """Result of a coordinated basis trade (spot + futures)"""
    success: bool
    spot_result: Optional[TradeResult] = None
    futures_result: Optional[TradeResult] = None
    error: Optional[str] = None
    execution_time_ms: float = 0.0


@dataclass
class MarketTick:
    """Market tick data"""
    symbol: str
    bid: float
    ask: float
    timestamp: datetime = field(default_factory=datetime.now)


class BrokerConnection(ABC):
    """Abstract base class for broker connections"""

    @abstractmethod
    async def connect(self) -> bool:
        """Connect to broker"""
        pass

    @abstractmethod
    async def disconnect(self) -> None:
        """Disconnect from broker"""
        pass

    @abstractmethod
    async def get_tick(self, symbol: str) -> Optional[MarketTick]:
        """Get current tick for symbol"""
        pass

    @abstractmethod
    async def place_market_order(self, symbol: str, side: OrderSide,
                                 volume: float) -> TradeResult:
        """Place market order"""
        pass

    @abstractmethod
    async def close_position(self, ticket: int, volume: float) -> TradeResult:
        """Close existing position"""
        pass


class MT5DirectConnection(BrokerConnection):
    """
    Direct MT5 connection (no multiprocessing).

    Used when running in single-process mode for same-broker spot/futures.
    Provides zero-overhead execution.
    """

    def __init__(self, config: BrokerConfig):
        self.config = config
        self._connected = False
        self._mt5 = None

    async def connect(self) -> bool:
        try:
            import MetaTrader5 as mt5
            self._mt5 = mt5

            # Initialize with specific path if provided
            if self.config.mt5_path:
                if not mt5.initialize(path=self.config.mt5_path):
                    logger.error(f"MT5 init failed for {self.config.broker_id}")
                    return False
            else:
                if not mt5.initialize():
                    logger.error(f"MT5 init failed for {self.config.broker_id}")
                    return False

            # Login if credentials provided
            if self.config.mt5_account and self.config.mt5_password:
                if not mt5.login(
                    self.config.mt5_account,
                    password=self.config.mt5_password,
                    server=self.config.mt5_server
                ):
                    logger.error(f"MT5 login failed for {self.config.broker_id}")
                    return False

            self._connected = True
            logger.info(f"MT5 connected: {self.config.broker_id}")
            return True

        except ImportError:
            logger.error("MetaTrader5 library not installed")
            return False
        except Exception as e:
            logger.error(f"MT5 connection error: {e}")
            return False

    async def disconnect(self) -> None:
        if self._connected and self._mt5:
            self._mt5.shutdown()
            self._connected = False
            logger.info(f"MT5 disconnected: {self.config.broker_id}")

    async def get_tick(self, symbol: str) -> Optional[MarketTick]:
        if not self._connected:
            return None

        try:
            tick = self._mt5.symbol_info_tick(symbol)
            if tick:
                return MarketTick(
                    symbol=symbol,
                    bid=tick.bid,
                    ask=tick.ask
                )
            return None
        except Exception as e:
            logger.error(f"MT5 tick error: {e}")
            return None

    async def place_market_order(self, symbol: str, side: OrderSide,
                                 volume: float) -> TradeResult:
        if not self._connected:
            return TradeResult(
                success=False,
                broker_id=self.config.broker_id,
                symbol=symbol,
                side=side.value,
                volume=volume,
                error="Not connected"
            )

        try:
            mt5 = self._mt5

            # Get symbol info
            symbol_info = mt5.symbol_info(symbol)
            if not symbol_info:
                return TradeResult(
                    success=False,
                    broker_id=self.config.broker_id,
                    symbol=symbol,
                    side=side.value,
                    volume=volume,
                    error=f"Symbol {symbol} not found"
                )

            if not symbol_info.visible:
                mt5.symbol_select(symbol, True)

            # Get current price
            tick = mt5.symbol_info_tick(symbol)
            if not tick:
                return TradeResult(
                    success=False,
                    broker_id=self.config.broker_id,
                    symbol=symbol,
                    side=side.value,
                    volume=volume,
                    error="No tick data"
                )

            # Determine order type and price
            if side == OrderSide.BUY:
                order_type = mt5.ORDER_TYPE_BUY
                price = tick.ask
            else:
                order_type = mt5.ORDER_TYPE_SELL
                price = tick.bid

            # Determine filling mode
            filling_mode = mt5.ORDER_FILLING_IOC
            if symbol_info.filling_mode & 1:  # FOK
                filling_mode = mt5.ORDER_FILLING_FOK
            elif symbol_info.filling_mode & 2:  # IOC
                filling_mode = mt5.ORDER_FILLING_IOC

            # Prepare request
            request = {
                "action": mt5.TRADE_ACTION_DEAL,
                "symbol": symbol,
                "volume": volume,
                "type": order_type,
                "price": price,
                "deviation": 20,
                "magic": 12345678,
                "comment": f"MultiBroker_{self.config.broker_id}",
                "type_time": mt5.ORDER_TIME_GTC,
                "type_filling": filling_mode,
            }

            # Send order
            result = mt5.order_send(request)

            if result.retcode != mt5.TRADE_RETCODE_DONE:
                return TradeResult(
                    success=False,
                    broker_id=self.config.broker_id,
                    symbol=symbol,
                    side=side.value,
                    volume=volume,
                    error=f"Order failed: {result.comment} (code: {result.retcode})"
                )

            return TradeResult(
                success=True,
                broker_id=self.config.broker_id,
                symbol=symbol,
                side=side.value,
                volume=volume,
                ticket=result.order,
                price=result.price
            )

        except Exception as e:
            logger.error(f"MT5 order error: {e}")
            return TradeResult(
                success=False,
                broker_id=self.config.broker_id,
                symbol=symbol,
                side=side.value,
                volume=volume,
                error=str(e)
            )

    async def close_position(self, ticket: int, volume: float) -> TradeResult:
        if not self._connected:
            return TradeResult(
                success=False,
                broker_id=self.config.broker_id,
                symbol="",
                side="",
                volume=volume,
                error="Not connected"
            )

        try:
            mt5 = self._mt5

            # Get position info
            position = mt5.positions_get(ticket=ticket)
            if not position:
                return TradeResult(
                    success=False,
                    broker_id=self.config.broker_id,
                    symbol="",
                    side="",
                    volume=volume,
                    error=f"Position {ticket} not found"
                )

            pos = position[0]
            symbol = pos.symbol

            # Determine close direction (opposite of position)
            if pos.type == mt5.POSITION_TYPE_BUY:
                order_type = mt5.ORDER_TYPE_SELL
                side = OrderSide.SELL
            else:
                order_type = mt5.ORDER_TYPE_BUY
                side = OrderSide.BUY

            tick = mt5.symbol_info_tick(symbol)
            price = tick.bid if side == OrderSide.SELL else tick.ask

            request = {
                "action": mt5.TRADE_ACTION_DEAL,
                "symbol": symbol,
                "volume": volume,
                "type": order_type,
                "position": ticket,
                "price": price,
                "deviation": 20,
                "magic": 12345678,
                "comment": f"Close_{ticket}",
                "type_time": mt5.ORDER_TIME_GTC,
                "type_filling": mt5.ORDER_FILLING_IOC,
            }

            result = mt5.order_send(request)

            if result.retcode != mt5.TRADE_RETCODE_DONE:
                return TradeResult(
                    success=False,
                    broker_id=self.config.broker_id,
                    symbol=symbol,
                    side=side.value,
                    volume=volume,
                    error=f"Close failed: {result.comment}"
                )

            return TradeResult(
                success=True,
                broker_id=self.config.broker_id,
                symbol=symbol,
                side=side.value,
                volume=volume,
                ticket=result.order,
                price=result.price
            )

        except Exception as e:
            logger.error(f"MT5 close error: {e}")
            return TradeResult(
                success=False,
                broker_id=self.config.broker_id,
                symbol="",
                side="",
                volume=volume,
                error=str(e)
            )


class MultiBrokerCoordinator:
    """
    Coordinates trading across single or multiple brokers.

    Features:
    - Automatic mode detection (single vs multi-broker)
    - Zero-overhead for same-broker spot/futures
    - Atomic execution for basis trades
    - Process isolation for different brokers
    """

    def __init__(self, config: Dict[str, Any]):
        """
        Initialize coordinator with broker configuration.

        Args:
            config: Configuration dict with format:
                {
                    "brokers": {
                        "broker_id": {
                            "name": "Broker Name",
                            "path": "C:/MT5/terminal64.exe",
                            "symbols": ["XAUUSD_", "GC1225"],
                            ...
                        }
                    },
                    "spot_broker": "broker_id",
                    "futures_broker": "broker_id"
                }
        """
        self.config = config
        self._mode = ExecutionMode.SINGLE_PROCESS
        self._connections: Dict[str, BrokerConnection] = {}
        self._processes: Dict[str, Process] = {}
        self._ipc: Optional[IPCManager] = None
        self._running = False

        # Tick data
        self._latest_ticks: Dict[str, MarketTick] = {}
        self._tick_callbacks: List[Callable] = []

        # Determine execution mode
        self._detect_mode()

    def _detect_mode(self) -> None:
        """Detect appropriate execution mode based on config"""
        brokers = self.config.get("brokers", {})
        spot_broker = self.config.get("spot_broker")
        futures_broker = self.config.get("futures_broker")

        # If same broker for both, use single process mode
        if spot_broker == futures_broker:
            self._mode = ExecutionMode.SINGLE_PROCESS
            logger.info("Mode: SINGLE_PROCESS (same broker for spot and futures)")
        elif len(brokers) == 1:
            # Only one broker configured
            self._mode = ExecutionMode.SINGLE_PROCESS
            logger.info("Mode: SINGLE_PROCESS (single broker)")
        else:
            self._mode = ExecutionMode.MULTI_PROCESS
            logger.info("Mode: MULTI_PROCESS (different brokers)")

    @property
    def mode(self) -> ExecutionMode:
        """Get current execution mode"""
        return self._mode

    @property
    def is_single_broker(self) -> bool:
        """Check if running in single broker mode"""
        return self._mode == ExecutionMode.SINGLE_PROCESS

    async def initialize(self) -> bool:
        """Initialize all broker connections"""
        logger.info(f"Initializing MultiBrokerCoordinator in {self._mode.value} mode")

        if self._mode == ExecutionMode.SINGLE_PROCESS:
            return await self._init_single_process()
        else:
            return await self._init_multi_process()

    async def _init_single_process(self) -> bool:
        """Initialize in single-process mode"""
        brokers = self.config.get("brokers", {})

        for broker_id, broker_config in brokers.items():
            config = BrokerConfig(
                broker_id=broker_id,
                name=broker_config.get("name", broker_id),
                role=broker_config.get("role", "UNIFIED"),
                backend_type=broker_config.get("type", "MT5"),
                mt5_path=broker_config.get("path", ""),
                symbol=broker_config.get("symbol", ""),
                spot_symbol=broker_config.get("spot_symbol", ""),
                futures_symbol=broker_config.get("futures_symbol", ""),
                unified_mode=True
            )

            # Create direct connection
            if config.backend_type.upper() == "MT5":
                connection = MT5DirectConnection(config)
            else:
                logger.warning(f"Unsupported backend: {config.backend_type}")
                continue

            if await connection.connect():
                self._connections[broker_id] = connection
                logger.info(f"Connected to {broker_id}")
            else:
                logger.error(f"Failed to connect to {broker_id}")
                return False

        self._running = True
        return len(self._connections) > 0

    async def _init_multi_process(self) -> bool:
        """Initialize in multi-process mode with IPC"""
        # Initialize IPC
        self._ipc = IPCManager(single_broker_mode=False)
        await self._ipc.initialize()

        # Start worker processes for each broker
        brokers = self.config.get("brokers", {})

        for broker_id, broker_config in brokers.items():
            # Worker processes will be spawned separately
            # Here we just set up the IPC subscriptions

            # Subscribe to tick updates from this broker
            await self._ipc.subscribe_ticks(broker_id, self._on_tick_received)

            logger.info(f"Subscribed to broker: {broker_id}")

        self._running = True
        return True

    async def shutdown(self) -> None:
        """Shutdown all connections and processes"""
        logger.info("Shutting down MultiBrokerCoordinator")
        self._running = False

        if self._mode == ExecutionMode.SINGLE_PROCESS:
            for connection in self._connections.values():
                await connection.disconnect()
        else:
            # Send shutdown commands to worker processes
            if self._ipc:
                for broker_id in self.config.get("brokers", {}).keys():
                    await self._ipc.send_command(broker_id, CommandType.SHUTDOWN)
                await self._ipc.shutdown()

            # Terminate processes
            for process in self._processes.values():
                if process.is_alive():
                    process.terminate()
                    process.join(timeout=5.0)

    def on_tick(self, callback: Callable) -> None:
        """Register tick callback"""
        self._tick_callbacks.append(callback)

    def _on_tick_received(self, message: IPCMessage) -> None:
        """Handle tick received via IPC"""
        payload = message.payload
        tick = MarketTick(
            symbol=payload['symbol'],
            bid=payload['bid'],
            ask=payload['ask']
        )
        self._latest_ticks[payload['symbol']] = tick

        for callback in self._tick_callbacks:
            try:
                callback(tick)
            except Exception as e:
                logger.error(f"Tick callback error: {e}")

    async def get_tick(self, symbol: str) -> Optional[MarketTick]:
        """Get latest tick for symbol"""
        if self._mode == ExecutionMode.SINGLE_PROCESS:
            # Direct query to connection
            for connection in self._connections.values():
                tick = await connection.get_tick(symbol)
                if tick:
                    return tick
            return None
        else:
            # Return cached tick from IPC
            return self._latest_ticks.get(symbol)

    async def get_all_ticks(self) -> Dict[str, MarketTick]:
        """Get all latest ticks"""
        if self._mode == ExecutionMode.SINGLE_PROCESS:
            ticks = {}
            for connection in self._connections.values():
                config = connection.config

                # Get spot tick
                if config.spot_symbol:
                    tick = await connection.get_tick(config.spot_symbol)
                    if tick:
                        ticks[config.spot_symbol] = tick

                # Get futures tick
                if config.futures_symbol:
                    tick = await connection.get_tick(config.futures_symbol)
                    if tick:
                        ticks[config.futures_symbol] = tick

                # Get default symbol
                if config.symbol:
                    tick = await connection.get_tick(config.symbol)
                    if tick:
                        ticks[config.symbol] = tick

            return ticks
        else:
            return self._latest_ticks.copy()

    async def execute_basis_trade(
        self,
        signal_type: str,  # "SELL_BASIS" or "BUY_BASIS"
        spot_symbol: str,
        futures_symbol: str,
        volume: float,
        atomic: bool = True
    ) -> BasisTradeResult:
        """
        Execute coordinated basis trade (spot + futures).

        Args:
            signal_type: SELL_BASIS (buy spot, sell futures) or
                        BUY_BASIS (sell spot, buy futures)
            spot_symbol: Spot trading symbol
            futures_symbol: Futures trading symbol
            volume: Trade volume
            atomic: If True, reverse spot trade if futures fails

        Returns:
            BasisTradeResult with both leg results
        """
        start_time = datetime.now()

        # Determine trade directions
        if signal_type == "SELL_BASIS":
            spot_side = OrderSide.BUY
            futures_side = OrderSide.SELL
        else:  # BUY_BASIS
            spot_side = OrderSide.SELL
            futures_side = OrderSide.BUY

        logger.info(f"Executing basis trade: {signal_type}")
        logger.info(f"  Spot: {spot_side.value} {volume} {spot_symbol}")
        logger.info(f"  Futures: {futures_side.value} {volume} {futures_symbol}")

        spot_result = None
        futures_result = None

        if self._mode == ExecutionMode.SINGLE_PROCESS:
            # Direct execution - same broker handles both legs
            connection = list(self._connections.values())[0]

            # Execute spot leg
            spot_result = await connection.place_market_order(
                spot_symbol, spot_side, volume
            )

            if not spot_result.success:
                return BasisTradeResult(
                    success=False,
                    spot_result=spot_result,
                    error=f"Spot trade failed: {spot_result.error}",
                    execution_time_ms=(datetime.now() - start_time).total_seconds() * 1000
                )

            # Execute futures leg
            futures_result = await connection.place_market_order(
                futures_symbol, futures_side, volume
            )

            if not futures_result.success and atomic:
                # Reverse the spot trade
                logger.warning("Futures failed, reversing spot trade")
                reverse_side = OrderSide.SELL if spot_side == OrderSide.BUY else OrderSide.BUY
                await connection.place_market_order(
                    spot_symbol, reverse_side, volume
                )

                return BasisTradeResult(
                    success=False,
                    spot_result=spot_result,
                    futures_result=futures_result,
                    error=f"Futures failed, spot reversed: {futures_result.error}",
                    execution_time_ms=(datetime.now() - start_time).total_seconds() * 1000
                )

        else:
            # Multi-process execution - coordinate via IPC
            spot_broker = self.config.get("spot_broker")
            futures_broker = self.config.get("futures_broker")

            # Send orders to both brokers simultaneously
            spot_corr_id = str(uuid.uuid4())
            futures_corr_id = str(uuid.uuid4())

            # Send both orders
            await asyncio.gather(
                self._ipc.send_order(
                    spot_broker, "OPEN", spot_symbol,
                    spot_side.value, volume, "MARKET",
                    correlation_id=spot_corr_id
                ),
                self._ipc.send_order(
                    futures_broker, "OPEN", futures_symbol,
                    futures_side.value, volume, "MARKET",
                    correlation_id=futures_corr_id
                )
            )

            # Wait for results
            spot_msg, futures_msg = await asyncio.gather(
                self._ipc.wait_for_result(spot_corr_id, timeout=30.0),
                self._ipc.wait_for_result(futures_corr_id, timeout=30.0)
            )

            # Convert to TradeResult
            if spot_msg:
                spot_result = TradeResult(
                    success=spot_msg.payload.get('success', False),
                    broker_id=spot_broker,
                    symbol=spot_symbol,
                    side=spot_side.value,
                    volume=volume,
                    ticket=spot_msg.payload.get('ticket'),
                    price=spot_msg.payload.get('price'),
                    error=spot_msg.payload.get('error')
                )
            else:
                spot_result = TradeResult(
                    success=False,
                    broker_id=spot_broker,
                    symbol=spot_symbol,
                    side=spot_side.value,
                    volume=volume,
                    error="Timeout waiting for spot result"
                )

            if futures_msg:
                futures_result = TradeResult(
                    success=futures_msg.payload.get('success', False),
                    broker_id=futures_broker,
                    symbol=futures_symbol,
                    side=futures_side.value,
                    volume=volume,
                    ticket=futures_msg.payload.get('ticket'),
                    price=futures_msg.payload.get('price'),
                    error=futures_msg.payload.get('error')
                )
            else:
                futures_result = TradeResult(
                    success=False,
                    broker_id=futures_broker,
                    symbol=futures_symbol,
                    side=futures_side.value,
                    volume=volume,
                    error="Timeout waiting for futures result"
                )

            # Handle atomic rollback if needed
            if atomic and spot_result.success and not futures_result.success:
                logger.warning("Futures failed, reversing spot trade via IPC")
                reverse_side = "SELL" if spot_side == OrderSide.BUY else "BUY"
                await self._ipc.send_order(
                    spot_broker, "OPEN", spot_symbol,
                    reverse_side, volume, "MARKET"
                )

        # Build final result
        success = spot_result.success and futures_result.success
        execution_time = (datetime.now() - start_time).total_seconds() * 1000

        return BasisTradeResult(
            success=success,
            spot_result=spot_result,
            futures_result=futures_result,
            execution_time_ms=execution_time
        )

    async def close_basis_position(
        self,
        spot_ticket: int,
        futures_ticket: int,
        volume: float
    ) -> BasisTradeResult:
        """
        Close an existing basis position.

        Args:
            spot_ticket: Spot position ticket
            futures_ticket: Futures position ticket
            volume: Volume to close

        Returns:
            BasisTradeResult
        """
        start_time = datetime.now()

        if self._mode == ExecutionMode.SINGLE_PROCESS:
            connection = list(self._connections.values())[0]

            # Close both positions
            spot_result = await connection.close_position(spot_ticket, volume)
            futures_result = await connection.close_position(futures_ticket, volume)

        else:
            # Via IPC
            spot_broker = self.config.get("spot_broker")
            futures_broker = self.config.get("futures_broker")

            spot_corr_id = str(uuid.uuid4())
            futures_corr_id = str(uuid.uuid4())

            await asyncio.gather(
                self._ipc.send_order(
                    spot_broker, "CLOSE", "", "", volume, "MARKET",
                    ticket=spot_ticket, correlation_id=spot_corr_id
                ),
                self._ipc.send_order(
                    futures_broker, "CLOSE", "", "", volume, "MARKET",
                    ticket=futures_ticket, correlation_id=futures_corr_id
                )
            )

            spot_msg, futures_msg = await asyncio.gather(
                self._ipc.wait_for_result(spot_corr_id, timeout=30.0),
                self._ipc.wait_for_result(futures_corr_id, timeout=30.0)
            )

            spot_result = TradeResult(
                success=spot_msg.payload.get('success', False) if spot_msg else False,
                broker_id=spot_broker,
                symbol="",
                side="",
                volume=volume,
                error=spot_msg.payload.get('error') if spot_msg else "Timeout"
            )

            futures_result = TradeResult(
                success=futures_msg.payload.get('success', False) if futures_msg else False,
                broker_id=futures_broker,
                symbol="",
                side="",
                volume=volume,
                error=futures_msg.payload.get('error') if futures_msg else "Timeout"
            )

        return BasisTradeResult(
            success=spot_result.success and futures_result.success,
            spot_result=spot_result,
            futures_result=futures_result,
            execution_time_ms=(datetime.now() - start_time).total_seconds() * 1000
        )


def create_coordinator_from_database(db_path: str = "trading.db") -> MultiBrokerCoordinator:
    """
    Create MultiBrokerCoordinator from database configuration.

    Reads broker configuration from the database and creates
    appropriate coordinator instance.
    """
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).parent.parent))

    from database.manager import DatabaseManager

    db = DatabaseManager(db_path)
    db.initialize()

    # Load brokers from database
    brokers = db.get_brokers()
    config = db.get_config()

    # Build coordinator config
    broker_config = {}
    spot_broker = None
    futures_broker = None

    for broker in brokers:
        broker_config[broker.broker_id] = {
            "name": broker.name,
            "type": broker.broker_type,
            "path": broker.mt5_path,
            "symbol": broker.symbol,
            "role": broker.role
        }

        if broker.role == "SPOT":
            spot_broker = broker.broker_id
        elif broker.role == "FUTURES":
            futures_broker = broker.broker_id
        elif broker.role == "UNIFIED":
            spot_broker = broker.broker_id
            futures_broker = broker.broker_id

    coordinator_config = {
        "brokers": broker_config,
        "spot_broker": spot_broker,
        "futures_broker": futures_broker
    }

    return MultiBrokerCoordinator(coordinator_config)
