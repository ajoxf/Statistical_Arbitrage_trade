"""
Database Models

SQLAlchemy ORM models for all database tables.
Supports SQLite backend with async operations.
"""

from datetime import datetime
from typing import Optional, Dict, Any
from dataclasses import dataclass, field
import json


@dataclass
class PriceHistory:
    """Historical price data for mean/std calculation"""
    id: Optional[int] = None
    timestamp: str = ""
    asset: str = "ACTIVE"
    spot_price: Optional[float] = None
    futures_price: Optional[float] = None
    spread: Optional[float] = None
    swap_diff: Optional[float] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            'id': self.id,
            'timestamp': self.timestamp,
            'asset': self.asset,
            'spot_price': self.spot_price,
            'futures_price': self.futures_price,
            'spread': self.spread,
            'swap_diff': self.swap_diff
        }


@dataclass
class TradingConfig:
    """Master configuration table - singleton (id=1)"""
    id: int = 1

    # Asset Configuration
    asset_name: str = "GOLD"
    spot_symbol: str = "XAUUSD"
    futures_symbol: str = "GC0226"
    futures_expiry: Optional[str] = None
    contract_size: float = 100.0
    swap_charge: float = 0.0

    # Signal Parameters
    lookback_period: int = 90
    lookback_unit: str = "minutes"  # 'minutes' or 'days'
    entry_std_dev: float = 2.0
    exit_std_dev: float = 0.5
    stop_loss_std_dev: float = 3.0
    exit_at_opposite_sd: float = 0.0

    # Risk Management
    time_stop_loss_days: float = 0.0
    max_positions: int = 3
    lot_size: float = 0.1
    commission_per_lot: float = 0.0
    min_profit_per_lot: float = 50.0
    max_loss_per_lot: float = 100.0

    # Hurst Exponent Filter
    hurst_enabled: bool = True
    hurst_threshold: float = 0.5
    trending_duration_minutes: int = 15

    # STD Profitability Filter
    std_filter_enabled: bool = True
    spot_spread_cost: float = 0.40
    futures_spread_cost: float = 0.10
    profit_margin: float = 1.5

    # Overnight Protection
    close_before_overnight: bool = False
    overnight_close_hour: int = 16
    overnight_close_minute: int = 55

    # Order Execution
    order_type: str = "MARKET"  # 'MARKET' or 'LIMIT'
    limit_order_timeout: int = 60
    limit_peg_interval: float = 1.5

    # Price Streaming
    tick_interval: float = 0.3  # Seconds between price updates (0.1 to 2.0)

    # Mode Settings
    algo_enabled: bool = False
    paper_mode: bool = True
    selected_asset: str = "GOLD"

    # Active Broker Selection
    active_spot_broker: Optional[str] = None
    active_futures_broker: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            'id': self.id,
            'asset_name': self.asset_name,
            'spot_symbol': self.spot_symbol,
            'futures_symbol': self.futures_symbol,
            'futures_expiry': self.futures_expiry,
            'contract_size': self.contract_size,
            'swap_charge': self.swap_charge,
            'lookback_period': self.lookback_period,
            'lookback_unit': self.lookback_unit,
            'entry_std_dev': self.entry_std_dev,
            'exit_std_dev': self.exit_std_dev,
            'stop_loss_std_dev': self.stop_loss_std_dev,
            'exit_at_opposite_sd': self.exit_at_opposite_sd,
            'time_stop_loss_days': self.time_stop_loss_days,
            'max_positions': self.max_positions,
            'lot_size': self.lot_size,
            'commission_per_lot': self.commission_per_lot,
            'min_profit_per_lot': self.min_profit_per_lot,
            'max_loss_per_lot': self.max_loss_per_lot,
            'hurst_enabled': self.hurst_enabled,
            'hurst_threshold': self.hurst_threshold,
            'trending_duration_minutes': self.trending_duration_minutes,
            'std_filter_enabled': self.std_filter_enabled,
            'spot_spread_cost': self.spot_spread_cost,
            'futures_spread_cost': self.futures_spread_cost,
            'profit_margin': self.profit_margin,
            'close_before_overnight': self.close_before_overnight,
            'overnight_close_hour': self.overnight_close_hour,
            'overnight_close_minute': self.overnight_close_minute,
            'order_type': self.order_type,
            'limit_order_timeout': self.limit_order_timeout,
            'limit_peg_interval': self.limit_peg_interval,
            'tick_interval': self.tick_interval,
            'algo_enabled': self.algo_enabled,
            'paper_mode': self.paper_mode,
            'selected_asset': self.selected_asset,
            'active_spot_broker': self.active_spot_broker,
            'active_futures_broker': self.active_futures_broker
        }


@dataclass
class Trade:
    """Trade journal with cross-broker references"""
    trade_id: str = ""
    asset: Optional[str] = None
    direction: Optional[str] = None  # 'Long Spread' or 'Short Spread'

    # Timestamps
    entry_date: Optional[str] = None
    exit_date: Optional[str] = None
    days_held: Optional[float] = None

    # Z-Scores
    entry_zscore: Optional[float] = None
    exit_zscore: Optional[float] = None

    # Entry Prices
    entry_spot_price: Optional[float] = None
    entry_futures_price: Optional[float] = None

    # Exit Prices
    exit_spot_price: Optional[float] = None
    exit_futures_price: Optional[float] = None

    # P&L Components
    spot_pnl: Optional[float] = None
    futures_pnl: Optional[float] = None
    gross_pnl: Optional[float] = None
    swap_cost: Optional[float] = None
    commission: Optional[float] = None
    spread_cost: Optional[float] = None
    net_pnl: Optional[float] = None
    return_pct: Optional[float] = None

    # Position Details
    lot_size: Optional[float] = None

    # Multi-Broker References (NEW)
    spot_broker_id: Optional[str] = None
    mt5_spot_ticket: Optional[int] = None
    futures_broker_id: Optional[str] = None
    mt5_futures_ticket: Optional[int] = None

    # Status
    order_status: Optional[str] = None
    status: str = "OPEN"  # 'OPEN' or 'CLOSED'

    # Locked stats at entry (for stat arb)
    entry_mean: Optional[float] = None
    entry_std: Optional[float] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            'trade_id': self.trade_id,
            'asset': self.asset,
            'direction': self.direction,
            'entry_date': self.entry_date,
            'exit_date': self.exit_date,
            'days_held': self.days_held,
            'entry_zscore': self.entry_zscore,
            'exit_zscore': self.exit_zscore,
            'entry_spot_price': self.entry_spot_price,
            'entry_futures_price': self.entry_futures_price,
            'exit_spot_price': self.exit_spot_price,
            'exit_futures_price': self.exit_futures_price,
            'spot_pnl': self.spot_pnl,
            'futures_pnl': self.futures_pnl,
            'gross_pnl': self.gross_pnl,
            'swap_cost': self.swap_cost,
            'commission': self.commission,
            'spread_cost': self.spread_cost,
            'net_pnl': self.net_pnl,
            'return_pct': self.return_pct,
            'lot_size': self.lot_size,
            'spot_broker_id': self.spot_broker_id,
            'mt5_spot_ticket': self.mt5_spot_ticket,
            'futures_broker_id': self.futures_broker_id,
            'mt5_futures_ticket': self.mt5_futures_ticket,
            'order_status': self.order_status,
            'status': self.status,
            'entry_mean': self.entry_mean,
            'entry_std': self.entry_std
        }


@dataclass
class SDTouchLog:
    """Tracks spread touches at standard deviation levels"""
    id: Optional[int] = None
    asset: Optional[str] = None
    touch_date: Optional[str] = None
    touch_time: Optional[str] = None
    sd_level: Optional[str] = None  # '2σ', '2.5σ', '3σ', '3.5σ', '4σ'
    direction: Optional[str] = None  # 'HIGH' or 'LOW'
    touch_spread: Optional[float] = None
    touch_zscore: Optional[float] = None
    mean_at_touch: Optional[float] = None
    std_at_touch: Optional[float] = None
    reached_mean: bool = False
    mean_reached_time: Optional[str] = None
    spread_at_mean: Optional[float] = None
    potential_profit: Optional[float] = None
    max_adverse_move: Optional[float] = None
    status: str = "PENDING"
    entry_spot_spread: Optional[float] = None
    entry_futures_spread: Optional[float] = None
    exit_spot_spread: Optional[float] = None
    exit_futures_spread: Optional[float] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            'id': self.id,
            'asset': self.asset,
            'touch_date': self.touch_date,
            'touch_time': self.touch_time,
            'sd_level': self.sd_level,
            'direction': self.direction,
            'touch_spread': self.touch_spread,
            'touch_zscore': self.touch_zscore,
            'mean_at_touch': self.mean_at_touch,
            'std_at_touch': self.std_at_touch,
            'reached_mean': self.reached_mean,
            'mean_reached_time': self.mean_reached_time,
            'spread_at_mean': self.spread_at_mean,
            'potential_profit': self.potential_profit,
            'max_adverse_move': self.max_adverse_move,
            'status': self.status,
            'entry_spot_spread': self.entry_spot_spread,
            'entry_futures_spread': self.entry_futures_spread,
            'exit_spot_spread': self.exit_spot_spread,
            'exit_futures_spread': self.exit_futures_spread
        }


@dataclass
class LimitOrderLog:
    """Tracks limit order execution attempts"""
    id: Optional[int] = None
    timestamp: Optional[str] = None
    broker_id: Optional[str] = None  # Multi-broker reference
    symbol: Optional[str] = None
    order_type: Optional[str] = None  # 'PEGGED_LIMIT', 'PEGGED_LIMIT_CLOSE'
    side: Optional[str] = None  # 'BUY' or 'SELL'
    volume: Optional[float] = None
    target_price: Optional[float] = None
    fill_price: Optional[float] = None
    status: Optional[str] = None  # 'FILLED', 'TIMEOUT', 'ERROR', 'CANCELLED'
    elapsed_seconds: Optional[float] = None
    iterations: Optional[int] = None
    error_message: Optional[str] = None
    context: Optional[str] = None  # 'ENTRY', 'EXIT', 'TEST'

    def to_dict(self) -> Dict[str, Any]:
        return {
            'id': self.id,
            'timestamp': self.timestamp,
            'broker_id': self.broker_id,
            'symbol': self.symbol,
            'order_type': self.order_type,
            'side': self.side,
            'volume': self.volume,
            'target_price': self.target_price,
            'fill_price': self.fill_price,
            'status': self.status,
            'elapsed_seconds': self.elapsed_seconds,
            'iterations': self.iterations,
            'error_message': self.error_message,
            'context': self.context
        }


@dataclass
class Broker:
    """Multi-broker configuration"""
    broker_id: str = ""
    name: str = ""
    broker_type: str = "MT5"  # 'MT5', 'FIX', 'FLEXTRADE', 'IB'
    role: str = ""  # 'SPOT' or 'FUTURES'

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
    okx_simulated: bool = True
    okx_account_type: str = "spot"  # 'spot' or 'swap'

    # Common
    symbol: str = ""
    contract_size: float = 100.0
    commission_per_lot: float = 0.0
    min_volume: float = 0.01
    swap_charge: float = 0.0  # Daily swap cost in dollars
    futures_expiry: Optional[str] = None  # Futures expiry date (YYYY-MM-DD)

    # Status
    status: str = "DISCONNECTED"
    last_heartbeat: Optional[str] = None
    latency_ms: Optional[int] = None

    # Additional config as JSON
    config_json: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            'broker_id': self.broker_id,
            'name': self.name,
            'broker_type': self.broker_type,
            'role': self.role,
            'mt5_path': self.mt5_path,
            'mt5_account': self.mt5_account,
            'mt5_server': self.mt5_server,
            'fix_host': self.fix_host,
            'fix_port': self.fix_port,
            'fix_sender_comp': self.fix_sender_comp,
            'fix_target_comp': self.fix_target_comp,
            'fix_username': self.fix_username,
            'flex_host': self.flex_host,
            'flex_port': self.flex_port,
            'ib_host': self.ib_host,
            'ib_port': self.ib_port,
            'ib_client_id': self.ib_client_id,
            'okx_api_key': self.okx_api_key,
            'okx_api_secret': self.okx_api_secret,
            'okx_passphrase': self.okx_passphrase,
            'okx_simulated': self.okx_simulated,
            'okx_account_type': self.okx_account_type,
            'symbol': self.symbol,
            'contract_size': self.contract_size,
            'commission_per_lot': self.commission_per_lot,
            'min_volume': self.min_volume,
            'swap_charge': self.swap_charge,
            'futures_expiry': self.futures_expiry,
            'status': self.status,
            'last_heartbeat': self.last_heartbeat,
            'latency_ms': self.latency_ms
        }

    @property
    def config(self) -> Dict[str, Any]:
        """Parse config_json field"""
        if self.config_json:
            try:
                return json.loads(self.config_json)
            except json.JSONDecodeError:
                return {}
        return {}

    @config.setter
    def config(self, value: Dict[str, Any]):
        """Set config_json field"""
        self.config_json = json.dumps(value)
