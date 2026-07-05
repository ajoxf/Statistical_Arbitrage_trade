"""SQLite logging of trades, positions and market data."""

import sqlite3
from datetime import datetime


class DataLogger:
    def __init__(self, db_path="algo_trading.db"):
        self.db_path = db_path
        self.init_database()

    def init_database(self):
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS trades (
                trade_id TEXT PRIMARY KEY,
                position_id TEXT,
                symbol TEXT,
                order_type TEXT,
                lot_size REAL,
                requested_price REAL,
                executed_price REAL,
                order_ticket INTEGER,
                status TEXT,
                timestamp TEXT,
                execution_time TEXT,
                error_message TEXT
            )
        ''')
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS positions (
                position_id TEXT PRIMARY KEY,
                asset TEXT,
                signal_type TEXT,
                entry_time TEXT,
                entry_premium REAL,
                close_time TEXT,
                close_reason TEXT,
                unrealized_pnl REAL,
                realized_pnl REAL,
                status TEXT
            )
        ''')
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS market_data (
                timestamp TEXT,
                asset TEXT,
                spot_price REAL,
                futures_price REAL,
                actual_basis REAL,
                swap_basis REAL,
                swap_premium_pct REAL,
                signal TEXT
            )
        ''')
        conn.commit()
        conn.close()

    def log_trade(self, trade, position_id=None):
        conn = sqlite3.connect(self.db_path)
        conn.execute('''
            INSERT OR REPLACE INTO trades VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', (
            trade.trade_id, position_id, trade.symbol, trade.side.value,
            trade.lot_size, trade.requested_price, trade.executed_price,
            trade.order_ticket, trade.status, trade.timestamp.isoformat(),
            trade.execution_time.isoformat() if trade.execution_time else None,
            trade.error_message,
        ))
        conn.commit()
        conn.close()

    def log_position(self, position):
        conn = sqlite3.connect(self.db_path)
        conn.execute('''
            INSERT OR REPLACE INTO positions VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', (
            position.position_id, position.asset, position.signal_type.value,
            position.entry_time.isoformat(),
            position.entry_premium,
            position.close_time.isoformat() if position.close_time else None,
            position.close_reason, position.unrealized_pnl,
            position.realized_pnl, position.status.value,
        ))
        conn.commit()
        conn.close()

    def log_market_data(self, asset, market_data, signal):
        signal_str = signal.value if hasattr(signal, 'value') else str(signal)
        conn = sqlite3.connect(self.db_path)
        conn.execute('''
            INSERT INTO market_data VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ''', (
            datetime.now().isoformat(), asset, market_data['spot_price'],
            market_data['futures_price'], market_data['actual_basis'],
            market_data['swap_basis'], market_data['swap_premium_pct'],
            signal_str,
        ))
        conn.commit()
        conn.close()
