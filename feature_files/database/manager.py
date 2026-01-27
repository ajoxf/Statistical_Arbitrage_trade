"""
Database Manager

SQLite database operations for the Multi-Broker Arbitrage System.
Manages configuration, brokers, trades, and price history.
"""

import sqlite3
import json
import logging
from pathlib import Path
from typing import List, Optional, Dict, Any
from datetime import datetime

from database.models import (
    TradingConfig, Broker, Trade, PriceHistory,
    SDTouchLog, LimitOrderLog
)

logger = logging.getLogger(__name__)


class DatabaseManager:
    """SQLite database manager for the trading system"""

    def __init__(self, db_path: str = "trading.db"):
        self.db_path = db_path
        self._connection: Optional[sqlite3.Connection] = None

    def _get_connection(self) -> sqlite3.Connection:
        """Get database connection with row factory"""
        if self._connection is None:
            self._connection = sqlite3.connect(self.db_path, check_same_thread=False)
            self._connection.row_factory = sqlite3.Row
        return self._connection

    def initialize(self):
        """Create database tables if they don't exist"""
        conn = self._get_connection()
        cursor = conn.cursor()

        # Trading configuration table (singleton)
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS trading_config (
                id INTEGER PRIMARY KEY DEFAULT 1,
                asset_name TEXT DEFAULT 'GOLD',
                spot_symbol TEXT DEFAULT 'XAUUSD',
                futures_symbol TEXT DEFAULT 'GC0226',
                futures_expiry TEXT,
                contract_size REAL DEFAULT 100.0,
                swap_charge REAL DEFAULT 0.0,
                lookback_period INTEGER DEFAULT 90,
                lookback_unit TEXT DEFAULT 'minutes',
                entry_std_dev REAL DEFAULT 2.0,
                exit_std_dev REAL DEFAULT 0.5,
                stop_loss_std_dev REAL DEFAULT 3.0,
                exit_at_opposite_sd REAL DEFAULT 0.0,
                time_stop_loss_days REAL DEFAULT 0.0,
                max_positions INTEGER DEFAULT 3,
                lot_size REAL DEFAULT 0.1,
                commission_per_lot REAL DEFAULT 0.0,
                min_profit_per_lot REAL DEFAULT 50.0,
                max_loss_per_lot REAL DEFAULT 100.0,
                hurst_enabled INTEGER DEFAULT 1,
                hurst_threshold REAL DEFAULT 0.5,
                trending_duration_minutes INTEGER DEFAULT 15,
                std_filter_enabled INTEGER DEFAULT 1,
                spot_spread_cost REAL DEFAULT 0.40,
                futures_spread_cost REAL DEFAULT 0.10,
                profit_margin REAL DEFAULT 1.5,
                close_before_overnight INTEGER DEFAULT 0,
                overnight_close_hour INTEGER DEFAULT 16,
                overnight_close_minute INTEGER DEFAULT 55,
                order_type TEXT DEFAULT 'MARKET',
                limit_order_timeout INTEGER DEFAULT 60,
                limit_peg_interval REAL DEFAULT 1.5,
                algo_enabled INTEGER DEFAULT 0,
                paper_mode INTEGER DEFAULT 1,
                selected_asset TEXT DEFAULT 'GOLD',
                active_spot_broker TEXT,
                active_futures_broker TEXT
            )
        ''')

        # Brokers table
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS brokers (
                broker_id TEXT PRIMARY KEY,
                name TEXT,
                broker_type TEXT DEFAULT 'MT5',
                role TEXT,
                mt5_path TEXT,
                mt5_account INTEGER,
                mt5_server TEXT,
                mt5_password TEXT,
                fix_host TEXT,
                fix_port INTEGER,
                fix_sender_comp TEXT,
                fix_target_comp TEXT,
                fix_username TEXT,
                fix_password TEXT,
                flex_host TEXT,
                flex_port INTEGER,
                flex_api_key TEXT,
                ib_host TEXT,
                ib_port INTEGER,
                ib_client_id INTEGER,
                okx_api_key TEXT,
                okx_api_secret TEXT,
                okx_passphrase TEXT,
                okx_simulated INTEGER DEFAULT 1,
                okx_account_type TEXT DEFAULT 'spot',
                symbol TEXT,
                contract_size REAL DEFAULT 100.0,
                commission_per_lot REAL DEFAULT 0.0,
                swap_charge REAL DEFAULT 0.0,
                futures_expiry TEXT,
                min_volume REAL DEFAULT 0.01,
                status TEXT DEFAULT 'DISCONNECTED',
                last_heartbeat TEXT,
                latency_ms INTEGER,
                config_json TEXT
            )
        ''')

        # Trades table
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS trades (
                trade_id TEXT PRIMARY KEY,
                asset TEXT,
                direction TEXT,
                entry_date TEXT,
                exit_date TEXT,
                days_held REAL,
                entry_zscore REAL,
                exit_zscore REAL,
                entry_spot_price REAL,
                entry_futures_price REAL,
                exit_spot_price REAL,
                exit_futures_price REAL,
                spot_pnl REAL,
                futures_pnl REAL,
                gross_pnl REAL,
                swap_cost REAL,
                commission REAL,
                spread_cost REAL,
                net_pnl REAL,
                return_pct REAL,
                lot_size REAL,
                spot_broker_id TEXT,
                mt5_spot_ticket INTEGER,
                futures_broker_id TEXT,
                mt5_futures_ticket INTEGER,
                order_status TEXT,
                status TEXT DEFAULT 'OPEN'
            )
        ''')

        # Price history table
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS price_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT,
                asset TEXT DEFAULT 'ACTIVE',
                spot_price REAL,
                futures_price REAL,
                spread REAL,
                swap_diff REAL
            )
        ''')

        # SD Touch log table
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS sd_touch_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                asset TEXT,
                touch_date TEXT,
                touch_time TEXT,
                sd_level TEXT,
                direction TEXT,
                touch_spread REAL,
                touch_zscore REAL,
                mean_at_touch REAL,
                std_at_touch REAL,
                reached_mean INTEGER DEFAULT 0,
                mean_reached_time TEXT,
                spread_at_mean REAL,
                potential_profit REAL,
                max_adverse_move REAL,
                status TEXT DEFAULT 'PENDING',
                entry_spot_spread REAL,
                entry_futures_spread REAL,
                exit_spot_spread REAL,
                exit_futures_spread REAL
            )
        ''')

        # Limit order log table
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS limit_order_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT,
                broker_id TEXT,
                symbol TEXT,
                order_type TEXT,
                side TEXT,
                volume REAL,
                target_price REAL,
                fill_price REAL,
                status TEXT,
                elapsed_seconds REAL,
                iterations INTEGER,
                error_message TEXT,
                context TEXT
            )
        ''')

        # Insert default config if not exists
        cursor.execute('SELECT COUNT(*) FROM trading_config')
        if cursor.fetchone()[0] == 0:
            cursor.execute('INSERT INTO trading_config (id) VALUES (1)')

        conn.commit()
        logger.info(f"Database initialized: {self.db_path}")

    def get_config(self) -> TradingConfig:
        """Get trading configuration"""
        conn = self._get_connection()
        cursor = conn.cursor()
        cursor.execute('SELECT * FROM trading_config WHERE id = 1')
        row = cursor.fetchone()

        if row:
            return TradingConfig(
                id=row['id'],
                asset_name=row['asset_name'] or 'GOLD',
                spot_symbol=row['spot_symbol'] or 'XAUUSD',
                futures_symbol=row['futures_symbol'] or 'GC0226',
                futures_expiry=row['futures_expiry'],
                contract_size=row['contract_size'] or 100.0,
                swap_charge=row['swap_charge'] or 0.0,
                lookback_period=row['lookback_period'] or 90,
                lookback_unit=row['lookback_unit'] or 'minutes',
                entry_std_dev=row['entry_std_dev'] or 2.0,
                exit_std_dev=row['exit_std_dev'] or 0.5,
                stop_loss_std_dev=row['stop_loss_std_dev'] or 3.0,
                exit_at_opposite_sd=row['exit_at_opposite_sd'] or 0.0,
                time_stop_loss_days=row['time_stop_loss_days'] or 0.0,
                max_positions=row['max_positions'] or 3,
                lot_size=row['lot_size'] or 0.1,
                commission_per_lot=row['commission_per_lot'] or 0.0,
                min_profit_per_lot=row['min_profit_per_lot'] or 50.0,
                max_loss_per_lot=row['max_loss_per_lot'] or 100.0,
                hurst_enabled=bool(row['hurst_enabled']),
                hurst_threshold=row['hurst_threshold'] or 0.5,
                trending_duration_minutes=row['trending_duration_minutes'] or 15,
                std_filter_enabled=bool(row['std_filter_enabled']),
                spot_spread_cost=row['spot_spread_cost'] or 0.40,
                futures_spread_cost=row['futures_spread_cost'] or 0.10,
                profit_margin=row['profit_margin'] or 1.5,
                close_before_overnight=bool(row['close_before_overnight']),
                overnight_close_hour=row['overnight_close_hour'] or 16,
                overnight_close_minute=row['overnight_close_minute'] or 55,
                order_type=row['order_type'] or 'MARKET',
                limit_order_timeout=row['limit_order_timeout'] or 60,
                limit_peg_interval=row['limit_peg_interval'] or 1.5,
                algo_enabled=bool(row['algo_enabled']),
                paper_mode=bool(row['paper_mode']),
                selected_asset=row['selected_asset'] or 'GOLD'
            )

        return TradingConfig()

    def update_config(self, config: TradingConfig):
        """Update trading configuration"""
        conn = self._get_connection()
        cursor = conn.cursor()

        cursor.execute('''
            UPDATE trading_config SET
                asset_name = ?,
                spot_symbol = ?,
                futures_symbol = ?,
                futures_expiry = ?,
                contract_size = ?,
                swap_charge = ?,
                lookback_period = ?,
                lookback_unit = ?,
                entry_std_dev = ?,
                exit_std_dev = ?,
                stop_loss_std_dev = ?,
                exit_at_opposite_sd = ?,
                time_stop_loss_days = ?,
                max_positions = ?,
                lot_size = ?,
                commission_per_lot = ?,
                min_profit_per_lot = ?,
                max_loss_per_lot = ?,
                hurst_enabled = ?,
                hurst_threshold = ?,
                trending_duration_minutes = ?,
                std_filter_enabled = ?,
                spot_spread_cost = ?,
                futures_spread_cost = ?,
                profit_margin = ?,
                close_before_overnight = ?,
                overnight_close_hour = ?,
                overnight_close_minute = ?,
                order_type = ?,
                limit_order_timeout = ?,
                limit_peg_interval = ?,
                algo_enabled = ?,
                paper_mode = ?,
                selected_asset = ?
            WHERE id = 1
        ''', (
            config.asset_name,
            config.spot_symbol,
            config.futures_symbol,
            config.futures_expiry,
            config.contract_size,
            config.swap_charge,
            config.lookback_period,
            config.lookback_unit,
            config.entry_std_dev,
            config.exit_std_dev,
            config.stop_loss_std_dev,
            config.exit_at_opposite_sd,
            config.time_stop_loss_days,
            config.max_positions,
            config.lot_size,
            config.commission_per_lot,
            config.min_profit_per_lot,
            config.max_loss_per_lot,
            int(config.hurst_enabled),
            config.hurst_threshold,
            config.trending_duration_minutes,
            int(config.std_filter_enabled),
            config.spot_spread_cost,
            config.futures_spread_cost,
            config.profit_margin,
            int(config.close_before_overnight),
            config.overnight_close_hour,
            config.overnight_close_minute,
            config.order_type,
            config.limit_order_timeout,
            config.limit_peg_interval,
            int(config.algo_enabled),
            int(config.paper_mode),
            config.selected_asset
        ))

        conn.commit()
        logger.info("Trading config updated")

    def update_config_field(self, field: str, value: Any):
        """Update a single config field"""
        conn = self._get_connection()
        cursor = conn.cursor()

        # Convert boolean to int for SQLite
        if isinstance(value, bool):
            value = int(value)

        cursor.execute(f'UPDATE trading_config SET {field} = ? WHERE id = 1', (value,))
        conn.commit()

    def get_brokers(self) -> List[Broker]:
        """Get all brokers"""
        conn = self._get_connection()
        cursor = conn.cursor()
        cursor.execute('SELECT * FROM brokers')

        brokers = []
        for row in cursor.fetchall():
            broker = Broker(
                broker_id=row['broker_id'],
                name=row['name'],
                broker_type=row['broker_type'],
                role=row['role'],
                mt5_path=row['mt5_path'],
                mt5_account=row['mt5_account'],
                mt5_server=row['mt5_server'],
                mt5_password=row['mt5_password'],
                fix_host=row['fix_host'],
                fix_port=row['fix_port'],
                fix_sender_comp=row['fix_sender_comp'],
                fix_target_comp=row['fix_target_comp'],
                fix_username=row['fix_username'],
                fix_password=row['fix_password'],
                symbol=row['symbol'],
                contract_size=row['contract_size'] or 100.0,
                commission_per_lot=row['commission_per_lot'] or 0.0,
                min_volume=row['min_volume'] or 0.01,
                status=row['status'] or 'DISCONNECTED',
                last_heartbeat=row['last_heartbeat'],
                latency_ms=row['latency_ms'],
                config_json=row['config_json']
            )
            # Add OKX fields if present
            if 'okx_api_key' in row.keys():
                broker.okx_api_key = row['okx_api_key']
                broker.okx_api_secret = row['okx_api_secret']
                broker.okx_passphrase = row['okx_passphrase']
                broker.okx_simulated = bool(row['okx_simulated']) if row['okx_simulated'] is not None else True
                broker.okx_account_type = row['okx_account_type'] or 'spot'
            if 'swap_charge' in row.keys():
                broker.swap_charge = row['swap_charge'] or 0.0
            if 'futures_expiry' in row.keys():
                broker.futures_expiry = row['futures_expiry']
            brokers.append(broker)

        return brokers

    def get_broker(self, broker_id: str) -> Optional[Broker]:
        """Get broker by ID"""
        conn = self._get_connection()
        cursor = conn.cursor()
        cursor.execute('SELECT * FROM brokers WHERE broker_id = ?', (broker_id,))
        row = cursor.fetchone()

        if row:
            broker = Broker(
                broker_id=row['broker_id'],
                name=row['name'],
                broker_type=row['broker_type'],
                role=row['role'],
                mt5_path=row['mt5_path'],
                mt5_account=row['mt5_account'],
                mt5_server=row['mt5_server'],
                mt5_password=row['mt5_password'],
                fix_host=row['fix_host'],
                fix_port=row['fix_port'],
                fix_sender_comp=row['fix_sender_comp'],
                fix_target_comp=row['fix_target_comp'],
                fix_username=row['fix_username'],
                fix_password=row['fix_password'],
                symbol=row['symbol'],
                contract_size=row['contract_size'] or 100.0,
                commission_per_lot=row['commission_per_lot'] or 0.0,
                min_volume=row['min_volume'] or 0.01,
                status=row['status'] or 'DISCONNECTED',
                last_heartbeat=row['last_heartbeat'],
                latency_ms=row['latency_ms'],
                config_json=row['config_json']
            )
            # Add OKX fields if present
            if 'okx_api_key' in row.keys():
                broker.okx_api_key = row['okx_api_key']
                broker.okx_api_secret = row['okx_api_secret']
                broker.okx_passphrase = row['okx_passphrase']
                broker.okx_simulated = bool(row['okx_simulated']) if row['okx_simulated'] is not None else True
                broker.okx_account_type = row['okx_account_type'] or 'spot'
            if 'swap_charge' in row.keys():
                broker.swap_charge = row['swap_charge'] or 0.0
            if 'futures_expiry' in row.keys():
                broker.futures_expiry = row['futures_expiry']
            return broker

        return None

    def add_broker(self, broker: Broker):
        """Add or update broker"""
        conn = self._get_connection()
        cursor = conn.cursor()

        cursor.execute('''
            INSERT OR REPLACE INTO brokers (
                broker_id, name, broker_type, role,
                mt5_path, mt5_account, mt5_server, mt5_password,
                fix_host, fix_port, fix_sender_comp, fix_target_comp,
                fix_username, fix_password,
                okx_api_key, okx_api_secret, okx_passphrase, okx_simulated, okx_account_type,
                symbol, contract_size, commission_per_lot, swap_charge, futures_expiry,
                min_volume, status, last_heartbeat, latency_ms, config_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', (
            broker.broker_id,
            broker.name,
            broker.broker_type,
            broker.role,
            broker.mt5_path,
            broker.mt5_account,
            broker.mt5_server,
            broker.mt5_password,
            broker.fix_host,
            broker.fix_port,
            broker.fix_sender_comp,
            broker.fix_target_comp,
            broker.fix_username,
            broker.fix_password,
            getattr(broker, 'okx_api_key', None),
            getattr(broker, 'okx_api_secret', None),
            getattr(broker, 'okx_passphrase', None),
            int(getattr(broker, 'okx_simulated', True)),
            getattr(broker, 'okx_account_type', 'spot'),
            broker.symbol,
            broker.contract_size,
            broker.commission_per_lot,
            getattr(broker, 'swap_charge', 0.0),
            getattr(broker, 'futures_expiry', None),
            broker.min_volume,
            broker.status,
            broker.last_heartbeat,
            broker.latency_ms,
            broker.config_json
        ))

        conn.commit()

    def delete_broker(self, broker_id: str):
        """Delete broker by ID"""
        conn = self._get_connection()
        cursor = conn.cursor()
        cursor.execute('DELETE FROM brokers WHERE broker_id = ?', (broker_id,))
        conn.commit()

    def get_trades(self, status: Optional[str] = None, limit: int = 100) -> List[Trade]:
        """Get trades with optional status filter"""
        conn = self._get_connection()
        cursor = conn.cursor()

        if status:
            cursor.execute(
                'SELECT * FROM trades WHERE status = ? ORDER BY entry_date DESC LIMIT ?',
                (status, limit)
            )
        else:
            cursor.execute(
                'SELECT * FROM trades ORDER BY entry_date DESC LIMIT ?',
                (limit,)
            )

        trades = []
        for row in cursor.fetchall():
            trades.append(Trade(
                trade_id=row['trade_id'],
                asset=row['asset'],
                direction=row['direction'],
                entry_date=row['entry_date'],
                exit_date=row['exit_date'],
                days_held=row['days_held'],
                entry_zscore=row['entry_zscore'],
                exit_zscore=row['exit_zscore'],
                entry_spot_price=row['entry_spot_price'],
                entry_futures_price=row['entry_futures_price'],
                exit_spot_price=row['exit_spot_price'],
                exit_futures_price=row['exit_futures_price'],
                spot_pnl=row['spot_pnl'],
                futures_pnl=row['futures_pnl'],
                gross_pnl=row['gross_pnl'],
                swap_cost=row['swap_cost'],
                commission=row['commission'],
                spread_cost=row['spread_cost'],
                net_pnl=row['net_pnl'],
                return_pct=row['return_pct'],
                lot_size=row['lot_size'],
                spot_broker_id=row['spot_broker_id'],
                mt5_spot_ticket=row['mt5_spot_ticket'],
                futures_broker_id=row['futures_broker_id'],
                mt5_futures_ticket=row['mt5_futures_ticket'],
                order_status=row['order_status'],
                status=row['status']
            ))

        return trades

    def get_open_trades(self) -> List[Trade]:
        """Get open trades"""
        return self.get_trades(status='OPEN')

    def add_trade(self, trade: Trade):
        """Add or update trade"""
        conn = self._get_connection()
        cursor = conn.cursor()

        cursor.execute('''
            INSERT OR REPLACE INTO trades (
                trade_id, asset, direction, entry_date, exit_date, days_held,
                entry_zscore, exit_zscore, entry_spot_price, entry_futures_price,
                exit_spot_price, exit_futures_price, spot_pnl, futures_pnl,
                gross_pnl, swap_cost, commission, spread_cost, net_pnl, return_pct,
                lot_size, spot_broker_id, mt5_spot_ticket, futures_broker_id,
                mt5_futures_ticket, order_status, status
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', (
            trade.trade_id, trade.asset, trade.direction, trade.entry_date,
            trade.exit_date, trade.days_held, trade.entry_zscore, trade.exit_zscore,
            trade.entry_spot_price, trade.entry_futures_price, trade.exit_spot_price,
            trade.exit_futures_price, trade.spot_pnl, trade.futures_pnl,
            trade.gross_pnl, trade.swap_cost, trade.commission, trade.spread_cost,
            trade.net_pnl, trade.return_pct, trade.lot_size, trade.spot_broker_id,
            trade.mt5_spot_ticket, trade.futures_broker_id, trade.mt5_futures_ticket,
            trade.order_status, trade.status
        ))

        conn.commit()

    def get_trade_statistics(self) -> Dict[str, Any]:
        """Get trade statistics"""
        conn = self._get_connection()
        cursor = conn.cursor()

        cursor.execute('SELECT COUNT(*) as total FROM trades')
        total = cursor.fetchone()['total']

        cursor.execute('SELECT COUNT(*) as wins FROM trades WHERE net_pnl > 0')
        wins = cursor.fetchone()['wins']

        cursor.execute('SELECT COUNT(*) as losses FROM trades WHERE net_pnl <= 0')
        losses = cursor.fetchone()['losses']

        cursor.execute('SELECT SUM(net_pnl) as total_pnl FROM trades')
        total_pnl = cursor.fetchone()['total_pnl'] or 0

        cursor.execute('SELECT AVG(net_pnl) as avg_pnl FROM trades WHERE net_pnl IS NOT NULL')
        avg_pnl = cursor.fetchone()['avg_pnl'] or 0

        cursor.execute('SELECT COUNT(*) as open_count FROM trades WHERE status = "OPEN"')
        open_count = cursor.fetchone()['open_count']

        return {
            'total_trades': total,
            'winning_trades': wins,
            'losing_trades': losses,
            'win_rate': (wins / total * 100) if total > 0 else 0,
            'total_pnl': total_pnl,
            'average_pnl': avg_pnl,
            'open_positions': open_count
        }

    def get_sd_touches(self, sd_level: Optional[str] = None, limit: int = 100) -> List[SDTouchLog]:
        """Get SD touch log entries"""
        conn = self._get_connection()
        cursor = conn.cursor()

        if sd_level:
            cursor.execute(
                'SELECT * FROM sd_touch_log WHERE sd_level = ? ORDER BY touch_date DESC, touch_time DESC LIMIT ?',
                (sd_level, limit)
            )
        else:
            cursor.execute(
                'SELECT * FROM sd_touch_log ORDER BY touch_date DESC, touch_time DESC LIMIT ?',
                (limit,)
            )

        touches = []
        for row in cursor.fetchall():
            touches.append(SDTouchLog(
                id=row['id'],
                asset=row['asset'],
                touch_date=row['touch_date'],
                touch_time=row['touch_time'],
                sd_level=row['sd_level'],
                direction=row['direction'],
                touch_spread=row['touch_spread'],
                touch_zscore=row['touch_zscore'],
                mean_at_touch=row['mean_at_touch'],
                std_at_touch=row['std_at_touch'],
                reached_mean=bool(row['reached_mean']),
                mean_reached_time=row['mean_reached_time'],
                spread_at_mean=row['spread_at_mean'],
                potential_profit=row['potential_profit'],
                max_adverse_move=row['max_adverse_move'],
                status=row['status']
            ))

        return touches

    def get_sd_touch_stats(self) -> Dict[str, Any]:
        """Get SD touch statistics"""
        conn = self._get_connection()
        cursor = conn.cursor()

        stats = {}

        for level in ['2σ', '2.5σ', '3σ', '3.5σ', '4σ']:
            cursor.execute(
                'SELECT COUNT(*) as count FROM sd_touch_log WHERE sd_level = ?',
                (level,)
            )
            total = cursor.fetchone()['count']

            cursor.execute(
                'SELECT COUNT(*) as count FROM sd_touch_log WHERE sd_level = ? AND reached_mean = 1',
                (level,)
            )
            reached = cursor.fetchone()['count']

            stats[level] = {
                'total': total,
                'reached_mean': reached,
                'success_rate': (reached / total * 100) if total > 0 else 0
            }

        return stats

    def get_limit_order_stats(self) -> Dict[str, Any]:
        """Get limit order statistics"""
        conn = self._get_connection()
        cursor = conn.cursor()

        cursor.execute('SELECT COUNT(*) as total FROM limit_order_log')
        total = cursor.fetchone()['total']

        cursor.execute('SELECT COUNT(*) as filled FROM limit_order_log WHERE status = "FILLED"')
        filled = cursor.fetchone()['filled']

        cursor.execute('SELECT AVG(elapsed_seconds) as avg_time FROM limit_order_log WHERE status = "FILLED"')
        avg_time = cursor.fetchone()['avg_time'] or 0

        return {
            'total_orders': total,
            'filled_orders': filled,
            'fill_rate': (filled / total * 100) if total > 0 else 0,
            'average_fill_time': avg_time
        }

    def clear_price_history(self):
        """Clear price history table"""
        conn = self._get_connection()
        cursor = conn.cursor()
        cursor.execute('DELETE FROM price_history')
        conn.commit()
        logger.info("Price history cleared")

    def clear_trades(self):
        """Clear trades table"""
        conn = self._get_connection()
        cursor = conn.cursor()
        cursor.execute('DELETE FROM trades')
        conn.commit()
        logger.info("Trades cleared")

    def clear_sd_touches(self):
        """Clear SD touch log table"""
        conn = self._get_connection()
        cursor = conn.cursor()
        cursor.execute('DELETE FROM sd_touch_log')
        conn.commit()
        logger.info("SD touches cleared")

    def close(self):
        """Close database connection"""
        if self._connection:
            self._connection.close()
            self._connection = None
