"""
Base Broker Adapter Interface

Abstract base class that all broker backends must implement.
Provides a unified interface for:
- Connection management
- Market data retrieval
- Order execution (market, limit, pegged limit)
- Position management
- Account information
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import List, Optional, Dict, Any
from datetime import datetime


class OrderType(Enum):
    """Order execution types"""
    MARKET = "MARKET"
    LIMIT = "LIMIT"
    PEGGED_LIMIT = "PEGGED_LIMIT"


class OrderSide(Enum):
    """Order direction"""
    BUY = "BUY"
    SELL = "SELL"


class PositionType(Enum):
    """Position type for tracking"""
    LONG = "LONG"
    SHORT = "SHORT"


class BrokerStatus(Enum):
    """Broker connection status"""
    DISCONNECTED = "DISCONNECTED"
    CONNECTING = "CONNECTING"
    CONNECTED = "CONNECTED"
    ERROR = "ERROR"


@dataclass
class Tick:
    """Real-time price tick data"""
    symbol: str
    bid: float
    ask: float
    timestamp: datetime
    last: Optional[float] = None
    volume: Optional[float] = None

    @property
    def mid(self) -> float:
        """Calculate mid-price"""
        return (self.bid + self.ask) / 2

    @property
    def spread(self) -> float:
        """Calculate bid-ask spread"""
        return self.ask - self.bid

    @property
    def spread_pct(self) -> float:
        """Calculate spread as percentage of mid"""
        return (self.spread / self.mid) * 100 if self.mid > 0 else 0


@dataclass
class OrderResult:
    """Result of an order operation"""
    success: bool
    ticket: Optional[int] = None
    order_id: Optional[str] = None
    price: Optional[float] = None
    volume: Optional[float] = None
    filled_volume: Optional[float] = None
    error: Optional[str] = None
    error_code: Optional[int] = None
    timestamp: Optional[datetime] = None
    execution_time_ms: Optional[float] = None
    slippage: Optional[float] = None

    def __post_init__(self):
        if self.timestamp is None:
            self.timestamp = datetime.now()


@dataclass
class Position:
    """Open position information"""
    ticket: int
    symbol: str
    volume: float
    entry_price: float
    current_price: float
    profit: float
    position_type: PositionType
    open_time: datetime
    swap: float = 0.0
    commission: float = 0.0
    magic: int = 0
    comment: str = ""
    broker_id: Optional[str] = None

    @property
    def unrealized_pnl(self) -> float:
        """Calculate unrealized P&L"""
        return self.profit

    @property
    def holding_time_hours(self) -> float:
        """Calculate how long position has been held"""
        delta = datetime.now() - self.open_time
        return delta.total_seconds() / 3600


@dataclass
class AccountInfo:
    """Broker account information"""
    balance: float
    equity: float
    margin: float
    free_margin: float
    margin_level: Optional[float] = None
    profit: float = 0.0
    currency: str = "USD"
    leverage: int = 100
    name: str = ""
    server: str = ""
    company: str = ""

    @property
    def margin_used_pct(self) -> float:
        """Calculate margin utilization percentage"""
        if self.equity > 0:
            return (self.margin / self.equity) * 100
        return 0.0


@dataclass
class SymbolInfo:
    """Trading symbol information"""
    symbol: str
    description: str
    base_currency: str
    quote_currency: str
    contract_size: float
    min_volume: float
    max_volume: float
    volume_step: float
    point: float
    digits: int
    spread: float
    trade_mode: str
    trade_allowed: bool

    def normalize_volume(self, volume: float) -> float:
        """Normalize volume to valid lot size"""
        if volume < self.min_volume:
            return self.min_volume
        if volume > self.max_volume:
            return self.max_volume
        # Round to volume step
        steps = round(volume / self.volume_step)
        return steps * self.volume_step


@dataclass
class BrokerConfig:
    """Configuration for a broker connection"""
    broker_id: str
    name: str
    role: str  # 'SPOT', 'FUTURES', or 'UNIFIED' (handles both)
    backend_type: str  # 'MT5', 'FIX', 'FLEXTRADE', 'IB', 'OKX'

    # Unified mode - single broker handles both spot and futures
    unified_mode: bool = False
    spot_symbol: Optional[str] = None      # Symbol for spot leg
    futures_symbol: Optional[str] = None   # Symbol for futures leg

    # MT5 specific
    mt5_path: Optional[str] = None
    mt5_account: Optional[int] = None
    mt5_server: Optional[str] = None
    mt5_password: Optional[str] = None

    # FIX specific
    fix_host: Optional[str] = None
    fix_port: Optional[int] = None
    fix_sender_comp: Optional[str] = None
    fix_target_comp: Optional[str] = None
    fix_username: Optional[str] = None
    fix_password: Optional[str] = None
    fix_heartbeat_interval: int = 30

    # FlexTrade specific
    flex_host: Optional[str] = None
    flex_port: Optional[int] = None
    flex_api_key: Optional[str] = None

    # IB specific
    ib_host: Optional[str] = None
    ib_port: Optional[int] = None
    ib_client_id: Optional[int] = None

    # OKX specific
    okx_api_key: Optional[str] = None
    okx_api_secret: Optional[str] = None
    okx_passphrase: Optional[str] = None
    okx_simulated: bool = True  # True for demo/paper trading
    okx_account_type: str = "spot"  # 'spot' or 'swap' (perpetual futures)

    # Common settings
    symbol: str = ""
    contract_size: float = 100.0
    commission_per_lot: float = 0.0
    min_volume: float = 0.01
    max_volume: float = 100.0

    # Connection settings
    timeout_seconds: int = 30
    reconnect_attempts: int = 3
    reconnect_delay_seconds: int = 5

    # Additional config
    extra: Dict[str, Any] = field(default_factory=dict)


class BrokerAdapter(ABC):
    """
    Abstract base class for broker connections.

    All broker backends (MT5, FIX, FlexTrade, IB) must implement this interface
    to provide a unified API for the trading system.
    """

    def __init__(self, config: BrokerConfig):
        """
        Initialize broker adapter with configuration.

        Args:
            config: BrokerConfig instance with connection details
        """
        self.config = config
        self.broker_id = config.broker_id
        self.name = config.name
        self.role = config.role
        self._status = BrokerStatus.DISCONNECTED
        self._last_heartbeat: Optional[datetime] = None
        self._latency_ms: Optional[float] = None

    @property
    def status(self) -> BrokerStatus:
        """Get current connection status"""
        return self._status

    @property
    def is_connected(self) -> bool:
        """Check if broker is connected"""
        return self._status == BrokerStatus.CONNECTED

    @property
    def latency_ms(self) -> Optional[float]:
        """Get last measured latency in milliseconds"""
        return self._latency_ms

    # ==================== Connection Management ====================

    @abstractmethod
    async def connect(self) -> bool:
        """
        Establish connection to the broker.

        Returns:
            True if connection successful, False otherwise
        """
        pass

    @abstractmethod
    async def disconnect(self) -> None:
        """Close connection to the broker."""
        pass

    @abstractmethod
    async def heartbeat(self) -> bool:
        """
        Send heartbeat to verify connection is alive.

        Returns:
            True if heartbeat successful
        """
        pass

    # ==================== Market Data ====================

    @abstractmethod
    async def get_tick(self, symbol: str) -> Optional[Tick]:
        """
        Get current bid/ask prices for a symbol.

        Args:
            symbol: Trading symbol (e.g., 'XAUUSD', 'GC0226')

        Returns:
            Tick object with current prices, or None if unavailable
        """
        pass

    @abstractmethod
    async def get_symbol_info(self, symbol: str) -> Optional[SymbolInfo]:
        """
        Get symbol specification/contract details.

        Args:
            symbol: Trading symbol

        Returns:
            SymbolInfo object with contract specifications
        """
        pass

    async def subscribe_tick(self, symbol: str, callback) -> bool:
        """
        Subscribe to real-time tick updates for a symbol.
        Override in implementations that support streaming.

        Args:
            symbol: Trading symbol
            callback: Function to call with each new tick

        Returns:
            True if subscription successful
        """
        return False

    async def unsubscribe_tick(self, symbol: str) -> bool:
        """
        Unsubscribe from tick updates.

        Args:
            symbol: Trading symbol

        Returns:
            True if unsubscription successful
        """
        return False

    # ==================== Order Execution ====================

    @abstractmethod
    async def place_market_order(
        self,
        symbol: str,
        side: OrderSide,
        volume: float,
        magic: int = 0,
        comment: str = ""
    ) -> OrderResult:
        """
        Place a market order for immediate execution.

        Args:
            symbol: Trading symbol
            side: BUY or SELL
            volume: Position size in lots
            magic: Magic number for order identification
            comment: Order comment

        Returns:
            OrderResult with execution details
        """
        pass

    @abstractmethod
    async def place_limit_order(
        self,
        symbol: str,
        side: OrderSide,
        volume: float,
        price: float,
        magic: int = 0,
        comment: str = ""
    ) -> OrderResult:
        """
        Place a limit order at specified price.

        Args:
            symbol: Trading symbol
            side: BUY or SELL
            volume: Position size in lots
            price: Limit price
            magic: Magic number
            comment: Order comment

        Returns:
            OrderResult with order details
        """
        pass

    @abstractmethod
    async def execute_pegged_limit_order(
        self,
        symbol: str,
        side: OrderSide,
        volume: float,
        timeout_seconds: int,
        peg_interval_seconds: float,
        ticket: Optional[int] = None
    ) -> OrderResult:
        """
        Execute a pegged limit order that tracks the market.

        Places a limit order at the current bid/ask and updates the price
        periodically to maintain best execution. Used to avoid paying
        the full bid-ask spread.

        Args:
            symbol: Trading symbol
            side: BUY or SELL
            volume: Position size in lots
            timeout_seconds: Maximum time to wait for fill
            peg_interval_seconds: How often to update limit price
            ticket: Existing position ticket (for closing)

        Returns:
            OrderResult with execution details
        """
        pass

    @abstractmethod
    async def cancel_order(self, order_id: str) -> bool:
        """
        Cancel a pending order.

        Args:
            order_id: Order identifier

        Returns:
            True if cancellation successful
        """
        pass

    @abstractmethod
    async def modify_order(
        self,
        order_id: str,
        price: Optional[float] = None,
        volume: Optional[float] = None
    ) -> bool:
        """
        Modify a pending order.

        Args:
            order_id: Order identifier
            price: New limit price
            volume: New volume

        Returns:
            True if modification successful
        """
        pass

    # ==================== Position Management ====================

    @abstractmethod
    async def close_position(
        self,
        ticket: int,
        volume: Optional[float] = None
    ) -> OrderResult:
        """
        Close an open position.

        Args:
            ticket: Position ticket number
            volume: Volume to close (None = close all)

        Returns:
            OrderResult with close execution details
        """
        pass

    @abstractmethod
    async def get_positions(self, symbol: Optional[str] = None) -> List[Position]:
        """
        Get all open positions.

        Args:
            symbol: Filter by symbol (None = all positions)

        Returns:
            List of Position objects
        """
        pass

    @abstractmethod
    async def get_position_by_ticket(self, ticket: int) -> Optional[Position]:
        """
        Get a specific position by ticket number.

        Args:
            ticket: Position ticket

        Returns:
            Position object or None if not found
        """
        pass

    # ==================== Account Information ====================

    @abstractmethod
    async def get_account_info(self) -> Optional[AccountInfo]:
        """
        Get account balance, margin, and equity information.

        Returns:
            AccountInfo object with account details
        """
        pass

    # ==================== Utility Methods ====================

    async def check_ready(self, symbol: str, volume: float) -> Dict[str, Any]:
        """
        Pre-flight check before order execution.

        Args:
            symbol: Trading symbol
            volume: Planned order volume

        Returns:
            Dict with 'ready' bool and 'errors' list
        """
        errors = []

        if not self.is_connected:
            errors.append("Broker not connected")
            return {'ready': False, 'errors': errors}

        # Check symbol is tradeable
        symbol_info = await self.get_symbol_info(symbol)
        if symbol_info is None:
            errors.append(f"Symbol {symbol} not found")
        elif not symbol_info.trade_allowed:
            errors.append(f"Trading not allowed for {symbol}")
        elif volume < symbol_info.min_volume:
            errors.append(f"Volume {volume} below minimum {symbol_info.min_volume}")
        elif volume > symbol_info.max_volume:
            errors.append(f"Volume {volume} above maximum {symbol_info.max_volume}")

        # Check account margin
        account = await self.get_account_info()
        if account is None:
            errors.append("Could not retrieve account info")
        elif account.free_margin <= 0:
            errors.append("Insufficient free margin")

        return {
            'ready': len(errors) == 0,
            'errors': errors,
            'symbol_info': symbol_info,
            'account_info': account
        }

    def __repr__(self) -> str:
        return f"{self.__class__.__name__}(id={self.broker_id}, role={self.role}, status={self.status.value})"
