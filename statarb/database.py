"""SQLite logging of trades, positions, market data, crash-safe
position state, the untracked-close ledger, and per-trade reviews."""

import json
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
        # Crash-safe live-position snapshots for restart recovery
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS position_state (
                position_id TEXT PRIMARY KEY,
                status TEXT,
                state_json TEXT,
                updated TEXT
            )
        ''')
        # Money that moved outside a recorded trade (orphan cleanups)
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS untracked_closes (
                timestamp TEXT,
                leg TEXT,
                symbol TEXT,
                ticket INTEGER,
                volume REAL,
                price REAL,
                note TEXT
            )
        ''')
        # Per-trade review metrics
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS trade_review (
                position_id TEXT PRIMARY KEY,
                asset TEXT,
                entry_z REAL,
                exit_z REAL,
                entry_sigma REAL,
                capture_target REAL,
                cost_est REAL,
                realized_pnl REAL,
                exit_reason TEXT,
                lots REAL,
                opened TEXT,
                closed TEXT
            )
        ''')
        conn.commit()
        conn.close()

    # -- crash-safe position state ---------------------------------------

    def save_position_state(self, position):
        conn = sqlite3.connect(self.db_path)
        conn.execute('''
            INSERT OR REPLACE INTO position_state VALUES (?, ?, ?, ?)
        ''', (position.position_id, position.status.value,
              json.dumps(position.to_dict()), datetime.now().isoformat()))
        conn.commit()
        conn.close()

    def clear_position_state(self, position_id):
        conn = sqlite3.connect(self.db_path)
        conn.execute('DELETE FROM position_state WHERE position_id = ?',
                     (position_id,))
        conn.commit()
        conn.close()

    def load_open_position_states(self):
        conn = sqlite3.connect(self.db_path)
        rows = conn.execute(
            "SELECT state_json FROM position_state "
            "WHERE status IN ('ACTIVE', 'CLOSING')").fetchall()
        conn.close()
        return [json.loads(r[0]) for r in rows]

    # -- ledgers -----------------------------------------------------------

    def log_untracked_close(self, leg, symbol, ticket, volume, price, note):
        conn = sqlite3.connect(self.db_path)
        conn.execute('''
            INSERT INTO untracked_closes VALUES (?, ?, ?, ?, ?, ?, ?)
        ''', (datetime.now().isoformat(), leg, symbol, ticket, volume,
              price, note))
        conn.commit()
        conn.close()

    def log_trade_review(self, position, exit_z=None, capture_target=None,
                         cost_est=None):
        conn = sqlite3.connect(self.db_path)
        plan = position.exit_plan or {}
        conn.execute('''
            INSERT OR REPLACE INTO trade_review VALUES
            (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', (
            position.position_id, position.asset,
            plan.get('entry_z'), exit_z, plan.get('entry_sigma'),
            capture_target if capture_target is not None
            else plan.get('tp_usd'),
            cost_est if cost_est is not None else plan.get('rt_cost_usd'),
            position.realized_pnl, position.close_reason,
            position.spot_trade.lot_size if position.spot_trade else None,
            position.entry_time.isoformat(),
            position.close_time.isoformat() if position.close_time else None,
        ))
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
