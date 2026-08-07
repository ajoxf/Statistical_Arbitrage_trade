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
        # What we wanted: the executable touch at the moment the
        # DECISION was made. Compared against executed_price this is
        # the slippage the operator asked to track (statarb/slippage.py).
        self.requested_price = price
        self.executed_price = None
        # Pair-level decision-to-fill account, set on the spot leg of
        # each pair. None means it could not be measured, which is not
        # the same as zero.
        self.slippage = None
        self.order_ticket = None
        # MT5 position tickets created by the fills. Hedging-mode
        # accounts REQUIRE closes to target these tickets.
        self.position_tickets = []
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
        self.exit_plan = None      # frozen dollar levels (exits.py)
        # Lifecycle extremes — the raw data that tunes TP/gate/max-hold
        # from measurements instead of opinion (peak distribution)
        self.peak_pnl = None
        self.peak_min = None       # minutes after entry
        self.trough_pnl = None
        self.trough_min = None
        self.z_reverted = False    # z entered the exit band during hold
        self.z_min = None          # z range during the hold (z path)
        self.z_max = None
        self.exit_spot_price = None
        self.exit_fut_price = None
        # Decision-to-fill accounts (statarb/slippage.py): what the
        # signal saw, what it was executable at, what MT5 gave us.
        # None means unmeasured, which is NOT the same as zero.
        self.entry_slippage = None
        self.exit_slippage = None
        # A close the broker refused. The position is still OPEN, so it
        # stays ACTIVE and under management; these track the retries so
        # the engine neither hammers the broker nor goes quiet about it.
        self.close_failures = 0
        self.last_close_error = None
        self.last_close_attempt = None

    # -- crash-safe persistence -----------------------------------------

    @staticmethod
    def _trade_to_dict(trade):
        return {
            'trade_id': trade.trade_id, 'symbol': trade.symbol,
            'side': trade.side.value, 'lot_size': trade.lot_size,
            'executed_price': trade.executed_price,
            'requested_price': trade.requested_price,
            'order_ticket': trade.order_ticket,
            'position_tickets': list(trade.position_tickets),
            'status': trade.status,
        }

    @staticmethod
    def _trade_from_dict(d):
        trade = Trade(d['symbol'], OrderSide(d['side']), d['lot_size'])
        trade.trade_id = d['trade_id']
        trade.executed_price = d['executed_price']
        trade.requested_price = d.get('requested_price')
        trade.order_ticket = d['order_ticket']
        trade.position_tickets = list(d.get('position_tickets') or [])
        trade.status = d['status']
        return trade

    def to_dict(self):
        return {
            'position_id': self.position_id,
            'asset': self.asset,
            'signal_type': self.signal_type.value,
            'entry_time': self.entry_time.isoformat(),
            'entry_premium': self.entry_premium,
            'status': self.status.value,
            'unrealized_pnl': self.unrealized_pnl,
            'exit_plan': self.exit_plan,
            'peak_pnl': self.peak_pnl,
            'peak_min': self.peak_min,
            'trough_pnl': self.trough_pnl,
            'trough_min': self.trough_min,
            'z_reverted': self.z_reverted,
            'z_min': self.z_min,
            'z_max': self.z_max,
            'entry_slippage': self.entry_slippage,
            'close_failures': self.close_failures,
            'last_close_error': self.last_close_error,
            'spot_trade': self._trade_to_dict(self.spot_trade),
            'futures_trade': self._trade_to_dict(self.futures_trade),
        }

    @classmethod
    def from_dict(cls, d):
        position = cls(d['position_id'], d['asset'],
                       SignalType(d['signal_type']),
                       cls._trade_from_dict(d['spot_trade']),
                       cls._trade_from_dict(d['futures_trade']))
        position.entry_time = datetime.fromisoformat(d['entry_time'])
        position.entry_premium = d['entry_premium']
        position.current_premium = d['entry_premium']
        position.status = PositionStatus(d['status'])
        position.unrealized_pnl = d.get('unrealized_pnl', 0.0)
        position.exit_plan = d.get('exit_plan')
        position.peak_pnl = d.get('peak_pnl')
        position.peak_min = d.get('peak_min')
        position.trough_pnl = d.get('trough_pnl')
        position.trough_min = d.get('trough_min')
        position.entry_slippage = d.get('entry_slippage')
        position.close_failures = d.get('close_failures', 0)
        position.last_close_error = d.get('last_close_error')
        position.z_reverted = d.get('z_reverted', False)
        position.z_min = d.get('z_min')
        position.z_max = d.get('z_max')
        return position
