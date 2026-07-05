"""Core domain models: enums, trades and positions."""

import uuid
from datetime import datetime
from enum import Enum


class OrderSide(Enum):
    BUY = "BUY"
    SELL = "SELL"

    @property
    def opposite(self):
        return OrderSide.SELL if self is OrderSide.BUY else OrderSide.BUY


class SignalType(Enum):
    NO_SIGNAL = "NO_SIGNAL"
    SELL_BASIS = "SELL_BASIS"   # Buy spot, sell futures (premium too high)
    BUY_BASIS = "BUY_BASIS"     # Buy futures, sell spot (discount too deep)
    CLOSE_LONG = "CLOSE_LONG"
    CLOSE_SHORT = "CLOSE_SHORT"


class PositionStatus(Enum):
    ACTIVE = "ACTIVE"
    CLOSING = "CLOSING"
    CLOSED = "CLOSED"
    ERROR = "ERROR"


class TradingSession(Enum):
    ASIAN_PRE = "ASIAN_PRE"
    CHINA_OPEN = "CHINA_OPEN"
    ASIAN_LATE = "ASIAN_LATE"
    LONDON_OPEN = "LONDON_OPEN"
    EUROPEAN = "EUROPEAN"
    US_OPEN = "US_OPEN"
    US_AFTERNOON = "US_AFTERNOON"
    AFTER_HOURS = "AFTER_HOURS"


class Trade:
    """A single order on one symbol."""

    def __init__(self, symbol, side, lot_size, price=None):
        self.trade_id = str(uuid.uuid4())[:8]
        self.symbol = symbol
        self.side = side                    # OrderSide
        self.lot_size = lot_size
        self.requested_price = price
        self.executed_price = None
        self.order_ticket = None
        self.status = "PENDING"
        self.timestamp = datetime.now()
        self.execution_time = None
        self.error_message = None


class Position:
    """A basis position: paired spot + futures trades."""

    def __init__(self, position_id, asset, signal_type, spot_trade, futures_trade):
        self.position_id = position_id
        self.asset = asset
        self.signal_type = signal_type
        self.spot_trade = spot_trade
        self.futures_trade = futures_trade
        self.entry_time = datetime.now()
        self.entry_premium = None
        self.current_premium = None
        self.status = PositionStatus.ACTIVE
        self.unrealized_pnl = 0.0
        self.realized_pnl = 0.0
        self.close_time = None
        self.close_reason = None
