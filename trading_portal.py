#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ALGORITHMIC TRADING PORTAL - Single Asset Mode
Based on Arb_Monitor UI style with added algo trading capabilities

Features:
- Real-time monitoring (same UI as Arb_Monitor)
- User-configurable trading parameters (lookback, std dev, stop loss)
- Algo trading toggle for non-technical users
- Persistent mean calculation (handles connectivity issues)
- Paper/Live trading modes
- Shareable web interface
"""

from flask import Flask, render_template, render_template_string, request, jsonify, redirect, url_for
import MetaTrader5 as mt5
import pandas as pd
import numpy as np
import sqlite3
import math
import threading
import time
import logging
import uuid
import os
from datetime import datetime, timedelta
from collections import deque

app = Flask(__name__)
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'change-this-in-production')

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('trading_portal.log', encoding='utf-8'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)


# =============================================================================
# DATABASE MANAGER - Persistent storage for mean calculation
# =============================================================================
class DatabaseManager:
    """Handles database operations for persistent mean calculation"""

    def __init__(self, db_path="trading_portal.db"):
        self.db_path = db_path
        self.lock = threading.Lock()
        self.init_database()

    def get_connection(self):
        return sqlite3.connect(self.db_path, check_same_thread=False)

    def init_database(self):
        """Initialize database tables"""
        conn = self.get_connection()
        cursor = conn.cursor()

        # Price history for persistent mean calculation
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS price_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                asset TEXT NOT NULL,
                spot_price REAL,
                futures_price REAL,
                spread REAL,
                swap_diff REAL
            )
        ''')

        # Trading configuration
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS trading_config (
                id INTEGER PRIMARY KEY,
                gold_swap_charge REAL DEFAULT 0.0,
                silver_swap_charge REAL DEFAULT 0.0,
                gold_spot_symbol TEXT DEFAULT '',
                gold_futures_symbol TEXT DEFAULT '',
                gold_futures_expiry TEXT DEFAULT '',
                gold_asset_name TEXT DEFAULT 'GOLD',
                gold_contract_size REAL DEFAULT 100,
                silver_spot_symbol TEXT DEFAULT '',
                silver_futures_symbol TEXT DEFAULT '',
                silver_futures_expiry TEXT DEFAULT '',
                silver_asset_name TEXT DEFAULT 'SILVER',
                silver_contract_size REAL DEFAULT 5000,
                lookback_period INTEGER DEFAULT 90,
                lookback_unit TEXT DEFAULT 'minutes',
                entry_std_dev REAL DEFAULT 2.0,
                exit_std_dev REAL DEFAULT 0.5,
                stop_loss_std_dev REAL DEFAULT 3.0,
                time_stop_loss_days REAL DEFAULT 0,
                max_positions INTEGER DEFAULT 3,
                lot_size REAL DEFAULT 0.1,
                algo_enabled INTEGER DEFAULT 0,
                paper_mode INTEGER DEFAULT 1,
                commission_per_lot REAL DEFAULT 0,
                hurst_threshold REAL DEFAULT 0.5,
                trending_duration_minutes INTEGER DEFAULT 15,
                updated_at TEXT
            )
        ''')

        # Add columns if they don't exist (for existing DBs)
        for col_def in [
            ('commission_per_lot', 'REAL DEFAULT 0'),
            ('hurst_threshold', 'REAL DEFAULT 0.5'),
            ('trending_duration_minutes', 'INTEGER DEFAULT 15'),
            ('gold_asset_name', "TEXT DEFAULT 'GOLD'"),
            ('gold_contract_size', 'REAL DEFAULT 100'),
            ('silver_asset_name', "TEXT DEFAULT 'SILVER'"),
            ('silver_contract_size', 'REAL DEFAULT 5000'),
            ('hurst_enabled', 'INTEGER DEFAULT 1'),
            ('close_before_overnight', 'INTEGER DEFAULT 0'),
            ('overnight_close_hour', 'INTEGER DEFAULT 16'),
            ('overnight_close_minute', 'INTEGER DEFAULT 55'),
            ('selected_asset', "TEXT DEFAULT 'GOLD'"),
            ('min_profit_per_lot', 'REAL DEFAULT 50'),
            ('max_loss_per_lot', 'REAL DEFAULT 100'),
            # Generic asset fields (replaces gold_*/silver_* for single-asset mode)
            ('asset_name', "TEXT DEFAULT ''"),
            ('spot_symbol', "TEXT DEFAULT ''"),
            ('futures_symbol', "TEXT DEFAULT ''"),
            ('futures_expiry', "TEXT DEFAULT ''"),
            ('contract_size', 'REAL DEFAULT 100'),
            ('swap_charge', 'REAL DEFAULT 0')
        ]:
            try:
                cursor.execute(f'ALTER TABLE trading_config ADD COLUMN {col_def[0]} {col_def[1]}')
            except:
                pass  # Column already exists

        # Migration: Copy gold_* values to generic fields if generic fields are empty
        try:
            cursor.execute('''
                UPDATE trading_config
                SET asset_name = gold_asset_name,
                    spot_symbol = gold_spot_symbol,
                    futures_symbol = gold_futures_symbol,
                    futures_expiry = gold_futures_expiry,
                    contract_size = gold_contract_size,
                    swap_charge = gold_swap_charge
                WHERE id = 1 AND (asset_name IS NULL OR asset_name = '')
            ''')
            conn.commit()
        except:
            pass  # Migration already done or fields don't exist

        # Trades log - comprehensive trade journal
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS trades (
                trade_id TEXT PRIMARY KEY,
                asset TEXT,
                direction TEXT,
                entry_date TEXT,
                exit_date TEXT,
                days_held INTEGER DEFAULT 0,
                entry_zscore REAL,
                exit_zscore REAL,
                entry_spot_price REAL,
                entry_futures_price REAL,
                exit_spot_price REAL,
                exit_futures_price REAL,
                spot_pnl REAL DEFAULT 0,
                futures_pnl REAL DEFAULT 0,
                gross_pnl REAL DEFAULT 0,
                swap_cost REAL DEFAULT 0,
                commission REAL DEFAULT 0,
                spread_cost REAL DEFAULT 0,
                net_pnl REAL DEFAULT 0,
                return_pct REAL DEFAULT 0,
                lot_size REAL DEFAULT 0.1,
                mt5_spot_ticket INTEGER,
                mt5_futures_ticket INTEGER,
                order_status TEXT DEFAULT 'PENDING',
                status TEXT DEFAULT 'OPEN'
            )
        ''')

        # Add spread_cost column if it doesn't exist (for existing DBs)
        try:
            cursor.execute('ALTER TABLE trades ADD COLUMN spread_cost REAL DEFAULT 0')
        except:
            pass  # Column already exists

        # SD Touch Tracking - tracks when spread touches various SD levels and returns to mean
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS sd_touch_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                asset TEXT NOT NULL,
                touch_date TEXT NOT NULL,
                touch_time TEXT NOT NULL,
                sd_level REAL NOT NULL,
                direction TEXT NOT NULL,
                touch_spread REAL NOT NULL,
                touch_zscore REAL NOT NULL,
                mean_at_touch REAL NOT NULL,
                std_at_touch REAL NOT NULL,
                reached_mean INTEGER DEFAULT 0,
                mean_reached_time TEXT,
                spread_at_mean REAL,
                potential_profit REAL,
                max_adverse_move REAL DEFAULT 0,
                status TEXT DEFAULT 'PENDING',
                entry_spot_spread REAL DEFAULT 0,
                entry_futures_spread REAL DEFAULT 0,
                exit_spot_spread REAL DEFAULT 0,
                exit_futures_spread REAL DEFAULT 0
            )
        ''')

        # Add bid-ask spread columns if they don't exist (for existing DBs)
        for col_def in [
            ('entry_spot_spread', 'REAL DEFAULT 0'),
            ('entry_futures_spread', 'REAL DEFAULT 0'),
            ('exit_spot_spread', 'REAL DEFAULT 0'),
            ('exit_futures_spread', 'REAL DEFAULT 0')
        ]:
            try:
                cursor.execute(f'ALTER TABLE sd_touch_log ADD COLUMN {col_def[0]} {col_def[1]}')
            except:
                pass  # Column already exists

        # Insert default config if not exists
        cursor.execute('SELECT COUNT(*) FROM trading_config')
        if cursor.fetchone()[0] == 0:
            cursor.execute('''
                INSERT INTO trading_config (id) VALUES (1)
            ''')

        conn.commit()
        conn.close()
        logger.info("Database initialized")

    def save_price(self, asset, spot_price, futures_price, spread, swap_diff):
        """Save price data point"""
        with self.lock:
            conn = self.get_connection()
            cursor = conn.cursor()
            cursor.execute('''
                INSERT INTO price_history (timestamp, asset, spot_price, futures_price, spread, swap_diff)
                VALUES (?, ?, ?, ?, ?, ?)
            ''', (datetime.now().isoformat(), asset, spot_price, futures_price, spread, swap_diff))
            conn.commit()
            conn.close()

    def get_price_history(self, asset, limit=500, max_age_hours=None):
        """Get price history for mean calculation

        Args:
            asset: Asset key (GOLD, SILVER)
            limit: Max number of records
            max_age_hours: Only return data within this many hours (None = no filter)
        """
        conn = self.get_connection()
        cursor = conn.cursor()
        if max_age_hours:
            cursor.execute('''
                SELECT spread, swap_diff FROM price_history
                WHERE asset = ? AND timestamp > datetime('now', ?)
                ORDER BY timestamp DESC
                LIMIT ?
            ''', (asset, f'-{max_age_hours} hours', limit))
        else:
            cursor.execute('''
                SELECT spread, swap_diff FROM price_history
                WHERE asset = ?
                ORDER BY timestamp DESC
                LIMIT ?
            ''', (asset, limit))
        rows = cursor.fetchall()
        conn.close()
        return rows

    def get_config(self):
        """Get trading configuration"""
        conn = self.get_connection()
        cursor = conn.cursor()
        # IMPORTANT: Always use explicit column names to prevent data corruption
        # when schema changes. Never use SELECT * which depends on physical column order.
        cursor.execute('''
            SELECT id, gold_swap_charge, silver_swap_charge, gold_spot_symbol,
                   gold_futures_symbol, gold_futures_expiry, gold_asset_name, gold_contract_size,
                   silver_spot_symbol, silver_futures_symbol, silver_futures_expiry,
                   silver_asset_name, silver_contract_size, lookback_period,
                   lookback_unit, entry_std_dev, exit_std_dev, stop_loss_std_dev,
                   time_stop_loss_days, max_positions, lot_size, algo_enabled,
                   paper_mode, commission_per_lot, hurst_threshold, trending_duration_minutes,
                   hurst_enabled, close_before_overnight, overnight_close_hour, overnight_close_minute,
                   selected_asset, min_profit_per_lot, max_loss_per_lot,
                   asset_name, spot_symbol, futures_symbol, futures_expiry, contract_size, swap_charge
            FROM trading_config WHERE id = 1
        ''')
        row = cursor.fetchone()
        conn.close()

        if row:
            # Column order matches the explicit SELECT above
            # Generic fields (33-38) with fallback to gold_* fields for migration
            # (indices shifted by 1 due to max_loss_per_lot at index 32)
            asset_name = row[33] if len(row) > 33 and row[33] else (row[6] or 'GOLD')
            spot_symbol = row[34] if len(row) > 34 and row[34] else (row[3] or '')
            futures_symbol = row[35] if len(row) > 35 and row[35] else (row[4] or '')
            futures_expiry = row[36] if len(row) > 36 and row[36] else (row[5] or '')
            contract_size = row[37] if len(row) > 37 and row[37] is not None else (row[7] if row[7] is not None else 100)
            swap_charge = row[38] if len(row) > 38 and row[38] is not None else (row[1] if row[1] is not None else 0)

            return {
                # Legacy fields (kept for backward compatibility)
                'gold_swap_charge': row[1] if row[1] is not None else 0.0,
                'silver_swap_charge': row[2] if row[2] is not None else 0.0,
                'gold_spot_symbol': row[3] or '',
                'gold_futures_symbol': row[4] or '',
                'gold_futures_expiry': row[5] or '',
                'gold_asset_name': row[6] or 'GOLD',
                'gold_contract_size': row[7] if row[7] is not None else 100,
                'silver_spot_symbol': row[8] or '',
                'silver_futures_symbol': row[9] or '',
                'silver_futures_expiry': row[10] or '',
                'silver_asset_name': row[11] or 'SILVER',
                'silver_contract_size': row[12] if row[12] is not None else 5000,
                # Trading parameters
                'lookback_period': row[13] if row[13] is not None else 90,
                'lookback_unit': row[14] or 'minutes',
                'entry_std_dev': row[15] if row[15] is not None else 2.0,
                'exit_std_dev': row[16] if row[16] is not None else 0.5,
                'stop_loss_std_dev': row[17] if row[17] is not None else 3.0,
                'time_stop_loss_days': row[18] if row[18] is not None else 0,
                'max_positions': row[19] if row[19] is not None else 3,
                'lot_size': row[20] if row[20] is not None else 0.1,
                'algo_enabled': bool(row[21]),
                'paper_mode': bool(row[22]) if row[22] is not None else True,
                'commission_per_lot': row[23] if row[23] is not None else 0,
                'hurst_threshold': row[24] if row[24] is not None else 0.5,
                'trending_duration_minutes': row[25] if row[25] is not None else 15,
                'hurst_enabled': bool(row[26]) if row[26] is not None else True,
                'close_before_overnight': bool(row[27]) if row[27] is not None else False,
                'overnight_close_hour': row[28] if row[28] is not None else 16,
                'overnight_close_minute': row[29] if row[29] is not None else 55,
                'selected_asset': row[30] if row[30] is not None else 'GOLD',
                'min_profit_per_lot': row[31] if row[31] is not None else 50,
                'max_loss_per_lot': row[32] if row[32] is not None else 100,
                # NEW: Generic asset fields (single-asset mode)
                'asset_name': asset_name,
                'spot_symbol': spot_symbol,
                'futures_symbol': futures_symbol,
                'futures_expiry': futures_expiry,
                'contract_size': contract_size,
                'swap_charge': swap_charge
            }
        return None

    def save_config(self, config):
        """Save trading configuration"""
        with self.lock:
            conn = self.get_connection()
            cursor = conn.cursor()
            cursor.execute('''
                UPDATE trading_config SET
                    gold_swap_charge = ?,
                    silver_swap_charge = ?,
                    gold_spot_symbol = ?,
                    gold_futures_symbol = ?,
                    gold_futures_expiry = ?,
                    gold_asset_name = ?,
                    gold_contract_size = ?,
                    silver_spot_symbol = ?,
                    silver_futures_symbol = ?,
                    silver_futures_expiry = ?,
                    silver_asset_name = ?,
                    silver_contract_size = ?,
                    lookback_period = ?,
                    lookback_unit = ?,
                    entry_std_dev = ?,
                    exit_std_dev = ?,
                    stop_loss_std_dev = ?,
                    time_stop_loss_days = ?,
                    max_positions = ?,
                    lot_size = ?,
                    algo_enabled = ?,
                    paper_mode = ?,
                    commission_per_lot = ?,
                    hurst_threshold = ?,
                    trending_duration_minutes = ?,
                    hurst_enabled = ?,
                    close_before_overnight = ?,
                    overnight_close_hour = ?,
                    overnight_close_minute = ?,
                    selected_asset = ?,
                    min_profit_per_lot = ?,
                    max_loss_per_lot = ?,
                    asset_name = ?,
                    spot_symbol = ?,
                    futures_symbol = ?,
                    futures_expiry = ?,
                    contract_size = ?,
                    swap_charge = ?,
                    updated_at = ?
                WHERE id = 1
            ''', (
                config.get('gold_swap_charge', 0),
                config.get('silver_swap_charge', 0),
                config.get('gold_spot_symbol', ''),
                config.get('gold_futures_symbol', ''),
                config.get('gold_futures_expiry', ''),
                config.get('gold_asset_name', 'GOLD'),
                config.get('gold_contract_size', 100),
                config.get('silver_spot_symbol', ''),
                config.get('silver_futures_symbol', ''),
                config.get('silver_futures_expiry', ''),
                config.get('silver_asset_name', 'SILVER'),
                config.get('silver_contract_size', 5000),
                config.get('lookback_period', 90),
                config.get('lookback_unit', 'minutes'),
                config.get('entry_std_dev', 2.0),
                config.get('exit_std_dev', 0.5),
                config.get('stop_loss_std_dev', 3.0),
                config.get('time_stop_loss_days', 0),
                config.get('max_positions', 3),
                config.get('lot_size', 0.1),
                1 if config.get('algo_enabled') else 0,
                1 if config.get('paper_mode', True) else 0,
                config.get('commission_per_lot', 0),
                config.get('hurst_threshold', 0.5),
                config.get('trending_duration_minutes', 15),
                1 if config.get('hurst_enabled', True) else 0,
                1 if config.get('close_before_overnight', False) else 0,
                config.get('overnight_close_hour', 16),
                config.get('overnight_close_minute', 55),
                config.get('selected_asset', 'GOLD'),
                config.get('min_profit_per_lot', 50),
                config.get('max_loss_per_lot', 100),
                config.get('asset_name', 'GOLD'),
                config.get('spot_symbol', ''),
                config.get('futures_symbol', ''),
                config.get('futures_expiry', ''),
                config.get('contract_size', 100),
                config.get('swap_charge', 0),
                datetime.now().isoformat()
            ))
            conn.commit()
            conn.close()

    def save_trade(self, trade):
        """Save trade to database"""
        with self.lock:
            conn = self.get_connection()
            cursor = conn.cursor()
            # IMPORTANT: Always use explicit column names to prevent data corruption
            # when schema changes. Never use VALUES alone without column names.
            cursor.execute('''
                INSERT OR REPLACE INTO trades (
                    trade_id, asset, direction, entry_date, exit_date, days_held,
                    entry_zscore, exit_zscore, entry_spot_price, entry_futures_price,
                    exit_spot_price, exit_futures_price, spot_pnl, futures_pnl,
                    gross_pnl, swap_cost, commission, spread_cost, net_pnl, return_pct,
                    lot_size, mt5_spot_ticket, mt5_futures_ticket, order_status, status
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''', (
                trade['trade_id'],
                trade['asset'],
                trade['direction'],
                trade['entry_date'],
                trade.get('exit_date'),
                trade.get('days_held', 0),
                trade['entry_zscore'],
                trade.get('exit_zscore'),
                trade['entry_spot_price'],
                trade['entry_futures_price'],
                trade.get('exit_spot_price'),
                trade.get('exit_futures_price'),
                trade.get('spot_pnl', 0),
                trade.get('futures_pnl', 0),
                trade.get('gross_pnl', 0),
                trade.get('swap_cost', 0),
                trade.get('commission', 0),
                trade.get('spread_cost', 0),
                trade.get('net_pnl', 0),
                trade.get('return_pct', 0),
                trade.get('lot_size', 0.1),
                trade.get('mt5_spot_ticket'),
                trade.get('mt5_futures_ticket'),
                trade.get('order_status', 'PENDING'),
                trade['status']
            ))
            conn.commit()
            conn.close()

    def get_trades(self, limit=50, status=None):
        """Get recent trades"""
        conn = self.get_connection()
        cursor = conn.cursor()
        # IMPORTANT: Always use explicit column names to prevent data corruption
        # when schema changes. Never use SELECT * which depends on physical column order.
        columns = '''
            trade_id, asset, direction, entry_date, exit_date, days_held,
            entry_zscore, exit_zscore, entry_spot_price, entry_futures_price,
            exit_spot_price, exit_futures_price, spot_pnl, futures_pnl,
            gross_pnl, swap_cost, commission, spread_cost, net_pnl, return_pct,
            lot_size, mt5_spot_ticket, mt5_futures_ticket, order_status, status
        '''
        if status:
            cursor.execute(f'SELECT {columns} FROM trades WHERE status = ? ORDER BY entry_date DESC LIMIT ?', (status, limit))
        else:
            cursor.execute(f'SELECT {columns} FROM trades ORDER BY entry_date DESC LIMIT ?', (limit,))
        rows = cursor.fetchall()
        conn.close()

        # Column order matches the explicit SELECT above (not dependent on physical storage)
        return [{
            'trade_id': r[0], 'asset': r[1], 'direction': r[2],
            'entry_date': r[3], 'exit_date': r[4], 'days_held': r[5],
            'entry_zscore': r[6], 'exit_zscore': r[7],
            'entry_spot_price': r[8], 'entry_futures_price': r[9],
            'exit_spot_price': r[10], 'exit_futures_price': r[11],
            'spot_pnl': r[12], 'futures_pnl': r[13],
            'gross_pnl': r[14], 'swap_cost': r[15], 'commission': r[16],
            'spread_cost': r[17], 'net_pnl': r[18], 'return_pct': r[19],
            'lot_size': r[20], 'mt5_spot_ticket': r[21], 'mt5_futures_ticket': r[22],
            'order_status': r[23], 'status': r[24]
        } for r in rows]

    def get_trade_summary(self):
        """Get total P&L summary with Sharpe ratio and drawdown"""
        conn = self.get_connection()
        cursor = conn.cursor()

        # Basic stats
        cursor.execute('''
            SELECT
                COUNT(*) as total_trades,
                SUM(CASE WHEN status = 'CLOSED' THEN net_pnl ELSE 0 END) as total_pnl,
                SUM(CASE WHEN status = 'CLOSED' AND net_pnl > 0 THEN 1 ELSE 0 END) as winning_trades,
                SUM(CASE WHEN status = 'CLOSED' AND net_pnl <= 0 THEN 1 ELSE 0 END) as losing_trades
            FROM trades
        ''')
        row = cursor.fetchone()
        total_pnl = row[1] or 0

        # Get trade details for margin calculation and returns
        cursor.execute('''
            SELECT asset, entry_spot_price, entry_futures_price, lot_size, return_pct, exit_date
            FROM trades
            WHERE status = 'CLOSED'
            ORDER BY exit_date ASC
        ''')
        trades = cursor.fetchall()
        conn.close()

        # Calculate total margin used across all trades
        # Margin = (Spot Value + Futures Value) × 1.15 buffer / Leverage
        leverage = 100  # Default leverage
        try:
            account = mt5.account_info()
            if account:
                leverage = account.leverage
        except:
            pass

        total_margin_used = 0
        returns = []
        for t in trades:
            asset, entry_spot, entry_futures, lot_size, return_pct, exit_date = t
            if entry_spot and entry_futures and lot_size:
                # Get contract size for this asset
                if asset == 'GOLD':
                    contract_size = 100
                elif asset == 'SILVER':
                    contract_size = 5000
                else:
                    contract_size = 100  # Default

                spot_value = entry_spot * lot_size * contract_size
                futures_value = entry_futures * lot_size * contract_size
                margin = (spot_value + futures_value) * 1.15 / leverage
                total_margin_used += margin

            if return_pct is not None:
                returns.append(return_pct)

        # Calculate overall return based on total margin used
        cumulative_return = (total_pnl / total_margin_used * 100) if total_margin_used > 0 else 0

        # Calculate Sharpe ratio (per-trade basis, not annualized)
        sharpe_ratio = 0.0
        if len(returns) >= 2:
            import statistics
            mean_return = statistics.mean(returns)
            std_return = statistics.stdev(returns)
            if std_return > 0:
                sharpe_ratio = mean_return / std_return

        # Calculate maximum drawdown from cumulative returns
        max_drawdown = 0.0
        current_drawdown = 0.0
        if len(returns) > 0:
            cumulative = 0.0
            peak = 0.0
            for ret in returns:
                cumulative += ret
                if cumulative > peak:
                    peak = cumulative
                drawdown = peak - cumulative
                if drawdown > max_drawdown:
                    max_drawdown = drawdown
            current_drawdown = peak - cumulative

        return {
            'total_trades': row[0] or 0,
            'total_pnl': row[1] or 0,
            'winning_trades': row[2] or 0,
            'losing_trades': row[3] or 0,
            'win_rate': (row[2] / row[0] * 100) if row[0] and row[0] > 0 else 0,
            'cumulative_return': cumulative_return,
            'sharpe_ratio': sharpe_ratio,
            'max_drawdown': max_drawdown,
            'current_drawdown': current_drawdown
        }


# =============================================================================
# SD TOUCH TRACKER - Track spread touches at various SD levels
# =============================================================================
class SDTouchTracker:
    """
    Tracks when spread touches various standard deviation levels and whether
    it subsequently returns to mean. Helps analyze which SD entry levels
    would result in profitable trades.

    Tracked SD levels: 4.0, 3.5, 3.0, 2.5, 2.0
    """

    SD_LEVELS = [4.0, 3.5, 3.0, 2.5, 2.0]

    def __init__(self, db_manager):
        self.db = db_manager
        self.active_touches = {}  # Track pending touches waiting for mean reversion
        self.last_zscore = None
        self.cooldown = {}  # Prevent duplicate touches within cooldown period
        self.paused = False  # Pause tracking new touches

    def check_and_log_touches(self, asset, current_spread, zscore, mean, std, contract_size=100,
                               spot_bid_ask=0, futures_bid_ask=0):
        """
        Check if spread has touched any SD level and track it.
        Also check if any pending touches have reached mean.

        Args:
            spot_bid_ask: Current bid-ask spread for spot (ask - bid)
            futures_bid_ask: Current bid-ask spread for futures (ask - bid)
        """
        if std <= 0:
            return []

        now = datetime.now()
        results = []

        # Check for new SD level touches (only if not paused)
        if not self.paused:
            for sd_level in self.SD_LEVELS:
                # Check upper touch (spread above mean + sd*std)
                upper_key = f"{asset}_upper_{sd_level}"
                if zscore >= sd_level:
                    if upper_key not in self.cooldown or (now - self.cooldown[upper_key]).total_seconds() > 300:
                        if upper_key not in self.active_touches:
                            touch_id = self._log_touch(asset, sd_level, 'SHORT', current_spread, zscore, mean, std,
                                                       spot_bid_ask, futures_bid_ask)
                            self.active_touches[upper_key] = {
                                'id': touch_id,
                                'sd_level': sd_level,
                                'direction': 'SHORT',
                                'touch_spread': current_spread,
                                'mean': mean,
                                'std': std,
                                'touch_time': now,
                                'max_adverse': 0,
                                'contract_size': contract_size
                            }
                            self.cooldown[upper_key] = now
                            results.append(f"Touch logged: {sd_level}σ SHORT at spread {current_spread:.4f}")

                # Check lower touch (spread below mean - sd*std)
                lower_key = f"{asset}_lower_{sd_level}"
                if zscore <= -sd_level:
                    if lower_key not in self.cooldown or (now - self.cooldown[lower_key]).total_seconds() > 300:
                        if lower_key not in self.active_touches:
                            touch_id = self._log_touch(asset, sd_level, 'LONG', current_spread, zscore, mean, std,
                                                       spot_bid_ask, futures_bid_ask)
                            self.active_touches[lower_key] = {
                                'id': touch_id,
                                'sd_level': sd_level,
                                'direction': 'LONG',
                                'touch_spread': current_spread,
                                'mean': mean,
                                'std': std,
                                'touch_time': now,
                                'max_adverse': 0,
                                'contract_size': contract_size
                            }
                            self.cooldown[lower_key] = now
                            results.append(f"Touch logged: {sd_level}σ LONG at spread {current_spread:.4f}")

        # Check if any active touches have reached mean (z-score crosses 0)
        keys_to_remove = []
        for key, touch in self.active_touches.items():
            # Track max adverse move
            if touch['direction'] == 'SHORT':
                adverse = current_spread - touch['touch_spread']
                if adverse > touch['max_adverse']:
                    touch['max_adverse'] = adverse
                # Check if reached mean (zscore <= 0)
                if zscore <= 0:
                    profit = (touch['touch_spread'] - current_spread) * touch['contract_size']
                    self._update_touch_reached_mean(touch['id'], current_spread, profit, touch['max_adverse'],
                                                    spot_bid_ask, futures_bid_ask)
                    results.append(f"Mean reached: {touch['sd_level']}σ SHORT, Profit: ${profit:.2f}")
                    keys_to_remove.append(key)
            else:  # LONG
                adverse = touch['touch_spread'] - current_spread
                if adverse > touch['max_adverse']:
                    touch['max_adverse'] = adverse
                # Check if reached mean (zscore >= 0)
                if zscore >= 0:
                    profit = (current_spread - touch['touch_spread']) * touch['contract_size']
                    self._update_touch_reached_mean(touch['id'], current_spread, profit, touch['max_adverse'],
                                                    spot_bid_ask, futures_bid_ask)
                    results.append(f"Mean reached: {touch['sd_level']}σ LONG, Profit: ${profit:.2f}")
                    keys_to_remove.append(key)

        # Clean up completed touches
        for key in keys_to_remove:
            del self.active_touches[key]

        self.last_zscore = zscore
        return results

    def _log_touch(self, asset, sd_level, direction, spread, zscore, mean, std,
                   spot_bid_ask=0, futures_bid_ask=0):
        """Log a new SD touch to database with bid-ask spread data"""
        conn = self.db.get_connection()
        cursor = conn.cursor()
        now = datetime.now()
        cursor.execute('''
            INSERT INTO sd_touch_log
            (asset, touch_date, touch_time, sd_level, direction, touch_spread,
             touch_zscore, mean_at_touch, std_at_touch, status,
             entry_spot_spread, entry_futures_spread)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'PENDING', ?, ?)
        ''', (asset, now.strftime('%Y-%m-%d'), now.strftime('%H:%M:%S'),
              sd_level, direction, spread, zscore, mean, std,
              spot_bid_ask, futures_bid_ask))
        touch_id = cursor.lastrowid
        conn.commit()
        conn.close()
        logger.info(f"SD Touch logged: {asset} {sd_level}σ {direction} at {spread:.4f} "
                    f"(z={zscore:.2f}, spot_spread={spot_bid_ask:.4f}, fut_spread={futures_bid_ask:.4f})")
        return touch_id

    def _update_touch_reached_mean(self, touch_id, spread_at_mean, profit, max_adverse,
                                   spot_bid_ask=0, futures_bid_ask=0):
        """Update touch record when mean is reached, including exit bid-ask spreads"""
        conn = self.db.get_connection()
        cursor = conn.cursor()
        now = datetime.now()
        cursor.execute('''
            UPDATE sd_touch_log
            SET reached_mean = 1,
                mean_reached_time = ?,
                spread_at_mean = ?,
                potential_profit = ?,
                max_adverse_move = ?,
                exit_spot_spread = ?,
                exit_futures_spread = ?,
                status = 'REACHED_MEAN'
            WHERE id = ?
        ''', (now.strftime('%H:%M:%S'), spread_at_mean, profit, max_adverse,
              spot_bid_ask, futures_bid_ask, touch_id))
        conn.commit()
        conn.close()
        logger.info(f"SD Touch #{touch_id} reached mean: profit=${profit:.2f}, max_adverse=${max_adverse:.2f}, "
                    f"exit_spreads=(spot={spot_bid_ask:.4f}, fut={futures_bid_ask:.4f})")

    def get_daily_statistics(self, asset=None, days=7):
        """Get SD touch statistics grouped by date and SD level"""
        conn = self.db.get_connection()
        cursor = conn.cursor()

        query = '''
            SELECT
                touch_date,
                sd_level,
                direction,
                COUNT(*) as total_touches,
                SUM(CASE WHEN reached_mean = 1 THEN 1 ELSE 0 END) as reached_mean_count,
                AVG(CASE WHEN reached_mean = 1 THEN potential_profit ELSE NULL END) as avg_profit,
                AVG(max_adverse_move) as avg_max_adverse,
                MAX(max_adverse_move) as worst_adverse
            FROM sd_touch_log
            WHERE touch_date >= date('now', ?)
        '''
        params = [f'-{days} days']

        if asset:
            query += ' AND asset = ?'
            params.append(asset)

        query += ' GROUP BY touch_date, sd_level, direction ORDER BY touch_date DESC, sd_level DESC'

        cursor.execute(query, params)
        rows = cursor.fetchall()
        conn.close()

        results = []
        for row in rows:
            success_rate = (row[4] / row[3] * 100) if row[3] > 0 else 0
            results.append({
                'date': row[0],
                'sd_level': row[1],
                'direction': row[2],
                'total_touches': row[3],
                'reached_mean': row[4],
                'success_rate': round(success_rate, 1),
                'avg_profit': round(row[5], 2) if row[5] else 0,
                'avg_max_adverse': round(row[6], 2) if row[6] else 0,
                'worst_adverse': round(row[7], 2) if row[7] else 0
            })

        return results

    def get_summary_by_sd_level(self, asset=None, days=30):
        """Get aggregated statistics by SD level"""
        conn = self.db.get_connection()
        cursor = conn.cursor()

        query = '''
            SELECT
                sd_level,
                direction,
                COUNT(*) as total_touches,
                SUM(CASE WHEN reached_mean = 1 THEN 1 ELSE 0 END) as reached_mean_count,
                AVG(CASE WHEN reached_mean = 1 THEN potential_profit ELSE NULL END) as avg_profit,
                SUM(CASE WHEN reached_mean = 1 THEN potential_profit ELSE 0 END) as total_profit,
                AVG(max_adverse_move) as avg_max_adverse,
                MAX(max_adverse_move) as worst_adverse
            FROM sd_touch_log
            WHERE touch_date >= date('now', ?)
        '''
        params = [f'-{days} days']

        if asset:
            query += ' AND asset = ?'
            params.append(asset)

        query += ' GROUP BY sd_level, direction ORDER BY sd_level DESC'

        cursor.execute(query, params)
        rows = cursor.fetchall()
        conn.close()

        results = []
        for row in rows:
            success_rate = (row[3] / row[2] * 100) if row[2] > 0 else 0
            results.append({
                'sd_level': row[0],
                'direction': row[1],
                'total_touches': row[2],
                'reached_mean': row[3],
                'success_rate': round(success_rate, 1),
                'avg_profit': round(row[4], 2) if row[4] else 0,
                'total_profit': round(row[5], 2) if row[5] else 0,
                'avg_max_adverse': round(row[6], 2) if row[6] else 0,
                'worst_adverse': round(row[7], 2) if row[7] else 0
            })

        return results

    def get_recent_touches(self, asset=None, limit=50):
        """Get recent touch events with details including bid-ask spreads"""
        conn = self.db.get_connection()
        cursor = conn.cursor()

        query = '''
            SELECT
                id, asset, touch_date, touch_time, sd_level, direction,
                touch_spread, touch_zscore, mean_at_touch, std_at_touch,
                reached_mean, mean_reached_time, spread_at_mean,
                potential_profit, max_adverse_move, status,
                entry_spot_spread, entry_futures_spread,
                exit_spot_spread, exit_futures_spread
            FROM sd_touch_log
        '''
        params = []

        if asset:
            query += ' WHERE asset = ?'
            params.append(asset)

        query += ' ORDER BY touch_date DESC, touch_time DESC LIMIT ?'
        params.append(limit)

        cursor.execute(query, params)
        rows = cursor.fetchall()
        conn.close()

        results = []
        for row in rows:
            # Calculate entry and exit costs
            entry_spot = row[16] if row[16] else 0
            entry_futures = row[17] if row[17] else 0
            exit_spot = row[18] if row[18] else 0
            exit_futures = row[19] if row[19] else 0

            results.append({
                'id': row[0],
                'asset': row[1],
                'date': row[2],
                'time': row[3],
                'sd_level': row[4],
                'direction': row[5],
                'touch_spread': row[6],
                'touch_zscore': round(row[7], 2),
                'mean': round(row[8], 4),
                'std': round(row[9], 4),
                'reached_mean': bool(row[10]),
                'mean_time': row[11],
                'spread_at_mean': row[12],
                'profit': round(row[13], 2) if row[13] else None,
                'max_adverse': round(row[14], 2) if row[14] else 0,
                'status': row[15],
                'entry_spot_spread': round(entry_spot, 4),
                'entry_futures_spread': round(entry_futures, 4),
                'exit_spot_spread': round(exit_spot, 4),
                'exit_futures_spread': round(exit_futures, 4),
                'entry_cost': round((entry_spot + entry_futures) * 100, 2),  # Per lot
                'exit_cost': round((exit_spot + exit_futures) * 100, 2),     # Per lot
                'round_trip_cost': round((entry_spot + entry_futures + exit_spot + exit_futures) * 100, 2)
            })

        return results


# =============================================================================
# TRADING MONITOR - Core monitoring and trading logic
# =============================================================================
class TradingMonitor:
    """Main trading monitor with algo trading capabilities"""

    def __init__(self):
        self.db = DatabaseManager()
        self.config = self.db.get_config() or {}

        # Asset configuration - SINGLE ASSET MODE
        # Uses generic config fields (asset_name, spot_symbol, futures_symbol, etc.)
        # Internal key is "ACTIVE" - the display name comes from config.asset_name
        asset_name = self.config.get('asset_name', 'GOLD')
        self.assets = {
            'ACTIVE': {
                'name': asset_name,
                'spot_symbols': [self.config.get('spot_symbol', '')],  # User-configured symbol
                'futures_symbols': [self.config.get('futures_symbol', '')],  # User-configured symbol
                'futures_expiry': self._parse_expiry(self.config.get('futures_expiry', '')),
                'multiplier': 1.0,
                'lot_size': self.config.get('contract_size', 100),  # Contract size (units per lot)
                'swap_long': self.config.get('swap_charge', 0.0)
            }
        }

        self.active_assets = {}
        self.is_initialized = False
        self.last_update = None
        self.error_message = None

        # Mean calculation cache - keyed by internal asset key
        self.spread_cache = {'ACTIVE': deque(maxlen=1000)}
        self.last_price_save = {}

        # Z-score history for charting (store last 200 points per asset)
        self.zscore_history = {'ACTIVE': deque(maxlen=200)}

        # Price history for spot/futures charting (store last 200 points per asset)
        self.price_history = {'ACTIVE': deque(maxlen=200)}

        # Track when Hurst became trending for each asset
        self.trending_since = {'ACTIVE': None}

        # Track consecutive stop losses (for auto-enabling Hurst protection)
        self.consecutive_stop_losses = 0

        # Active positions - load from database
        self.positions = {}
        self._load_open_positions()

        # Background thread
        self.running = False

        # SD Touch Tracker - tracks touches at various SD levels
        self.sd_tracker = SDTouchTracker(self.db)
        self.update_thread = None

    def _load_open_positions(self):
        """Load open positions from database on startup"""
        open_trades = self.db.get_trades(status='OPEN', limit=100)
        for trade in open_trades:
            asset_key = trade['asset']
            self.positions[asset_key] = {
                'trade_id': trade['trade_id'],
                'asset': trade['asset'],
                'direction': trade['direction'],
                'entry_date': trade['entry_date'],
                'entry_zscore': trade['entry_zscore'],
                'entry_spot_price': trade['entry_spot_price'],
                'entry_futures_price': trade['entry_futures_price'],
                'lot_size': trade['lot_size'],
                'status': trade['status'],
                'order_status': trade['order_status'] or 'UNKNOWN',
                'mt5_spot_ticket': trade['mt5_spot_ticket'],
                'mt5_futures_ticket': trade['mt5_futures_ticket']
            }
            logger.info(f"Loaded open position: {asset_key} {trade['direction']} from {trade['entry_date']}")

    def _parse_expiry(self, expiry_str):
        """Parse expiry date string to datetime"""
        if not expiry_str:
            return datetime(2026, 12, 31)  # Default far future
        try:
            return datetime.strptime(expiry_str, '%Y-%m-%d')
        except ValueError:
            return datetime(2026, 12, 31)

    def initialize_mt5(self):
        """Initialize MT5 connection"""
        if not mt5.initialize():
            error = mt5.last_error()
            self.error_message = f"MT5 initialization failed: {error}"
            logger.error(self.error_message)
            return False

        logger.info("MT5 connected successfully")
        return self.setup_symbols()

    def setup_symbols(self):
        """Setup symbols for the active asset using user-configured values"""
        # Refresh asset configuration from generic config fields
        asset_name = self.config.get('asset_name', 'GOLD')
        self.assets['ACTIVE']['name'] = asset_name
        self.assets['ACTIVE']['lot_size'] = self.config.get('contract_size', 100)
        self.assets['ACTIVE']['swap_long'] = self.config.get('swap_charge', 0)

        # Get user-configured symbols from generic fields
        config_spot = self.config.get('spot_symbol', '')
        config_futures = self.config.get('futures_symbol', '')
        config_expiry = self.config.get('futures_expiry', '')

        # Update the spot_symbols and futures_symbols lists with configured values
        if config_spot:
            self.assets['ACTIVE']['spot_symbols'] = [config_spot]
        if config_futures:
            self.assets['ACTIVE']['futures_symbols'] = [config_futures]

        # Parse user-configured expiry date (format: YYYY-MM-DD)
        if config_expiry:
            self.assets['ACTIVE']['futures_expiry'] = self._parse_expiry(config_expiry)
            logger.info(f"{asset_name}: Using configured expiry: {config_expiry}")

        spot_symbol = None
        futures_symbol = None

        # Validate spot symbol in MT5
        if config_spot:
            symbol_info = mt5.symbol_info(config_spot)
            if symbol_info:
                spot_symbol = config_spot
                mt5.symbol_select(config_spot, True)
                logger.info(f"{asset_name}: Using configured spot symbol: {config_spot}")
            else:
                logger.warning(f"{asset_name}: Configured spot symbol '{config_spot}' not found in MT5")

        # Validate futures symbol in MT5
        if config_futures:
            if mt5.symbol_info(config_futures):
                futures_symbol = config_futures
                mt5.symbol_select(config_futures, True)
                logger.info(f"{asset_name}: Using configured futures symbol: {config_futures}")
            else:
                logger.warning(f"{asset_name}: Configured futures symbol '{config_futures}' not found in MT5")

        if spot_symbol and futures_symbol:
            # Get user-configured swap charge
            swap_charge = self.config.get('swap_charge', 0)

            self.active_assets['ACTIVE'] = {
                'config': self.assets['ACTIVE'],
                'spot_symbol': spot_symbol,
                'futures_symbol': futures_symbol,
                'swap_charge': swap_charge
            }
            logger.info(f"{asset_name}: {spot_symbol} + {futures_symbol} | Swap: ${swap_charge:.2f}/lot/day")
        else:
            logger.warning(f"{asset_name}: Could not find symbols - Spot: {config_spot}, Futures: {config_futures}")
            logger.warning("Please configure valid MT5 symbols in Setup page")

        return len(self.active_assets) > 0

    def start_background_updates(self):
        """Start background price collection"""
        if self.running:
            return

        self.running = True
        self.update_thread = threading.Thread(target=self._background_loop, daemon=True)
        self.update_thread.start()
        logger.info("Background updates started")

    def stop_background_updates(self):
        """Stop background updates"""
        self.running = False
        if self.update_thread:
            self.update_thread.join(timeout=5)

    def _background_loop(self):
        """Background loop for continuous price collection"""
        while self.running:
            try:
                # Collect prices every 0.3 seconds for fast UI updates
                # SINGLE ASSET MODE: Only process the active asset
                for asset_key in ['ACTIVE'] if 'ACTIVE' in self.active_assets else []:
                    data = self.get_market_data(asset_key)
                    if data:
                        # Determine save interval based on lookback_unit
                        lookback_unit = self.config.get('lookback_unit', 'minutes')
                        if lookback_unit == 'days':
                            save_interval = 3600  # Save 1 point per hour (24 points per day)
                        else:  # minutes
                            save_interval = 60  # Save 1 point per minute

                        # Save to database at the configured interval
                        last_save = self.last_price_save.get(asset_key, datetime.min)
                        if (datetime.now() - last_save).total_seconds() >= save_interval:
                            self.db.save_price(
                                asset_key, data['spot_price'], data['futures_price'],
                                data['actual_basis'], data['actual_basis']  # Store raw spread
                            )
                            self.last_price_save[asset_key] = datetime.now()

                            # Also add to cache for statistics (spaced data points)
                            self.spread_cache[asset_key].append({
                                'timestamp': datetime.now(),
                                'spread': data['actual_basis'],
                                'actual_basis': data['actual_basis']
                            })
                            # Keep cache at reasonable size
                            if len(self.spread_cache[asset_key]) > 2000:
                                self.spread_cache[asset_key] = list(self.spread_cache[asset_key])[-1000:]

                        # Process algo trading if enabled
                        if self.config.get('algo_enabled'):
                            self._process_algo_trading(asset_key, data)

                time.sleep(0.3)

            except Exception as e:
                logger.error(f"Background loop error: {e}")
                time.sleep(1)

    def calculate_swap_basis(self, asset_key, spot_price, time_to_expiry):
        """Calculate swap-based basis using user-configured swap charge (like Arb_Monitor)"""
        # Use user-configured swap charge from generic field (single asset mode)
        swap_charge = self.config.get('swap_charge', 0)
        lot_size = self.assets[asset_key]['lot_size']

        if swap_charge <= 0:
            return spot_price, 0, 0, 0

        position_value = spot_price * lot_size
        daily_swap_rate = swap_charge / position_value
        annual_swap_rate = daily_swap_rate * 365

        swap_futures_price = spot_price * math.exp(annual_swap_rate * time_to_expiry)
        swap_basis = swap_futures_price - spot_price

        return swap_futures_price, swap_basis, annual_swap_rate, swap_charge

    def get_statistics(self, asset_key):
        """Get rolling statistics for z-score calculation (pure mean reversion on raw spread)"""
        lookback = self.config.get('lookback_period', 90)
        lookback_unit = self.config.get('lookback_unit', 'minutes')

        # Calculate required data points
        # For 'minutes': 1 point per minute, so lookback = points needed
        # For 'days': 24 points per day (1 per hour), so lookback * 24 = points needed
        if lookback_unit == 'days':
            required_points = lookback * 24
        else:
            required_points = lookback

        # Calculate max age for database query (only use recent data)
        # Add 10% buffer to account for gaps
        if lookback_unit == 'days':
            max_age_hours = int(lookback * 24 * 1.1)  # days to hours + 10% buffer
        else:
            max_age_hours = max(2, int(lookback / 60 * 1.1) + 1)  # minutes to hours + buffer, min 2 hours

        # First try cache
        cache = list(self.spread_cache.get(asset_key, []))

        if len(cache) >= required_points:
            spreads = [d['spread'] for d in cache[-required_points:]]
        else:
            # Fall back to database - only get data within the lookback window
            history = self.db.get_price_history(asset_key, required_points, max_age_hours=max_age_hours)
            spreads = [row[0] for row in history]  # spread column (raw basis)

            # Rebuild cache from database
            if len(spreads) > len(cache):
                self.spread_cache[asset_key] = deque(maxlen=2000)
                for row in reversed(history):
                    self.spread_cache[asset_key].append({
                        'timestamp': datetime.now(),
                        'spread': row[0]
                    })

        # Get threshold config for band calculations
        entry_std = self.config.get('entry_std_dev', 2.0)
        exit_std = self.config.get('exit_std_dev', 0.5)
        stop_std = self.config.get('stop_loss_std_dev', 3.0)

        # STRICT: Don't trade until we have FULL lookback period
        if len(spreads) < required_points:
            mean_val = np.mean(spreads) if spreads else 0
            std_val = np.std(spreads) if spreads else 0
            return {
                'mean': mean_val,
                'std': std_val,
                'count': len(spreads),
                'required': required_points,
                'complete': False,
                'upper_entry': mean_val + (entry_std * std_val),
                'lower_entry': mean_val - (entry_std * std_val),
                'upper_exit': mean_val + (exit_std * std_val),
                'lower_exit': mean_val - (exit_std * std_val),
                'upper_stop': mean_val + (stop_std * std_val),
                'lower_stop': mean_val - (stop_std * std_val)
            }

        mean_val = np.mean(spreads)
        std_val = np.std(spreads)
        return {
            'mean': mean_val,
            'std': std_val,
            'count': len(spreads),
            'required': required_points,
            'complete': True,
            'upper_entry': mean_val + (entry_std * std_val),
            'lower_entry': mean_val - (entry_std * std_val),
            'upper_exit': mean_val + (exit_std * std_val),
            'lower_exit': mean_val - (exit_std * std_val),
            'upper_stop': mean_val + (stop_std * std_val),
            'lower_stop': mean_val - (stop_std * std_val)
        }

    def calculate_zscore(self, asset_key, current_value):
        """Calculate z-score for current spread"""
        stats = self.get_statistics(asset_key)
        if not stats or stats['std'] == 0:
            return None, stats

        # Don't calculate valid z-score until lookback is complete
        if not stats.get('complete', False):
            return None, stats

        zscore = (current_value - stats['mean']) / stats['std']
        return zscore, stats

    def calculate_hurst_exponent(self, asset_key, min_points=20):
        """
        Calculate Hurst Exponent using R/S (Rescaled Range) method.

        H < 0.5: Mean-reverting (anti-persistent) - GOOD for mean reversion strategy
        H = 0.5: Random walk (Brownian motion) - No edge
        H > 0.5: Trending (persistent) - BAD for mean reversion, good for momentum

        Returns: (hurst_value, regime_label)
        """
        cache = self.spread_cache.get(asset_key, [])
        if len(cache) < min_points:
            return None, 'INSUFFICIENT_DATA'

        # Get spread values from cache
        spreads = np.array([item['spread'] for item in cache])

        # Use last N points (more recent data is more relevant)
        max_points = min(100, len(spreads))
        ts = spreads[-max_points:]

        # R/S Analysis
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

    def get_market_data(self, asset_key):
        """Get market data for specific asset"""
        if asset_key not in self.active_assets:
            return None

        try:
            asset = self.active_assets[asset_key]
            spot_symbol = asset['spot_symbol']
            futures_symbol = asset['futures_symbol']
            config = asset['config']

            spot_tick = mt5.symbol_info_tick(spot_symbol)
            futures_tick = mt5.symbol_info_tick(futures_symbol)

            if not spot_tick or not futures_tick:
                return None

            multiplier = config.get('multiplier', 1.0)
            spot_price = spot_tick.last if spot_tick.last > 0 else (spot_tick.bid + spot_tick.ask) / 2
            futures_price = (futures_tick.last if futures_tick.last > 0 else (futures_tick.bid + futures_tick.ask) / 2) * multiplier

            spot_spread = (spot_tick.ask - spot_tick.bid) * 100
            futures_spread = (futures_tick.ask - futures_tick.bid) * 100

            actual_basis = futures_price - spot_price

            current_time = datetime.now()
            time_to_expiry = (config['futures_expiry'] - current_time).total_seconds() / (365.25 * 24 * 3600)
            days_to_expiry = time_to_expiry * 365.25

            # Get swap charge from user config and lot size
            lot_size = config['lot_size']

            if time_to_expiry > 0:
                swap_futures_price, swap_basis, annual_swap_rate, swap_charge = self.calculate_swap_basis(
                    asset_key, spot_price, time_to_expiry
                )

                if abs(swap_basis) > 0.001:
                    swap_premium_pct = ((actual_basis - swap_basis) / abs(swap_basis)) * 100
                else:
                    swap_premium_pct = (actual_basis / spot_price) * 100

                swap_diff = actual_basis - swap_basis
            else:
                swap_futures_price = futures_price
                swap_basis = 0
                swap_premium_pct = 0
                swap_diff = 0
                annual_swap_rate = 0
                swap_charge = self.config.get('swap_charge', 0)

            # Determine status (like Arb_Monitor)
            if swap_diff < 0:
                status = 'CHEAP'
                status_class = 'cheap'
            elif swap_diff > 5:
                status = 'EXPENSIVE'
                status_class = 'expensive'
            else:
                status = 'FAIR'
                status_class = 'fair'

            # Calculate z-score on RAW SPREAD (pure mean reversion)
            zscore, stats = self.calculate_zscore(asset_key, actual_basis)

            # Calculate Hurst exponent for regime detection
            hurst_value, hurst_regime = self.calculate_hurst_exponent(asset_key)

            # Generate signal (pass stats, hurst, and current prices for filtering)
            signal = self._generate_signal(asset_key, zscore, stats, hurst_value, hurst_regime, spot_price, futures_price)

            # Store z-score in history for charting
            entry_threshold = self.config.get('entry_std_dev', 2.0)
            exit_threshold = self.config.get('exit_std_dev', 0.5)
            if zscore is not None:
                self.zscore_history[asset_key].append({
                    'time': datetime.now().strftime('%H:%M:%S'),
                    'zscore': zscore,
                    'entry_upper': entry_threshold,
                    'entry_lower': -entry_threshold,
                    'exit_upper': exit_threshold,
                    'exit_lower': -exit_threshold
                })

            # Store price history for spot/futures charting
            current_spread = futures_price - spot_price
            self.price_history[asset_key].append({
                'time': datetime.now().strftime('%H:%M:%S'),
                'spot_price': spot_price,
                'futures_price': futures_price,
                'spread': current_spread
            })

            # Track SD level touches for analysis
            if zscore is not None and stats:
                contract_size_for_tracking = config.get('lot_size', 100)
                # Get raw bid-ask spreads (in dollars, not cents)
                spot_bid_ask = spot_tick.ask - spot_tick.bid
                futures_bid_ask = futures_tick.ask - futures_tick.bid
                self.sd_tracker.check_and_log_touches(
                    asset_key,
                    current_spread,
                    zscore,
                    stats.get('mean', 0),
                    stats.get('std', 0),
                    contract_size_for_tracking,
                    spot_bid_ask,
                    futures_bid_ask
                )

            # Margin calculations
            # Get account leverage
            account = mt5.account_info()
            leverage = account.leverage if account else 100  # Default 1:100

            # Get contract size from asset config (units per lot - configurable per asset)
            contract_size = config.get('lot_size', 100)

            # User's configured lot size for trading
            user_lot_size = self.config.get('lot_size', 0.1)

            # Margin per lot (Spot) = (Price × Contract Size) / Leverage
            margin_per_lot_spot = (spot_price * contract_size) / leverage

            # Margin per lot (Futures) - typically similar or slightly different
            margin_per_lot_futures = (futures_price * contract_size) / leverage

            # Total margin per lot (both legs of spread trade)
            margin_per_lot_total = margin_per_lot_spot + margin_per_lot_futures

            # Margin required for current position size
            margin_required = margin_per_lot_total * user_lot_size

            # Margin with 15% buffer for price fluctuation
            margin_with_buffer = margin_required * 1.15

            return {
                'asset_name': config['name'],
                'spot_symbol': spot_symbol,
                'futures_symbol': futures_symbol,
                'spot_price': spot_price,
                'futures_price': futures_price,
                'swap_futures_price': swap_futures_price,
                'spot_bid': spot_tick.bid,
                'spot_ask': spot_tick.ask,
                'futures_bid': futures_tick.bid * multiplier,
                'futures_ask': futures_tick.ask * multiplier,
                'spot_spread': spot_spread,
                'futures_spread': futures_spread,
                'actual_basis': actual_basis,
                'swap_basis': swap_basis,
                'swap_premium_pct': swap_premium_pct,
                'swap_diff': swap_diff,
                'swap_charge': swap_charge,
                'lot_size': lot_size,
                'annual_swap_rate': annual_swap_rate,
                'days_to_expiry': days_to_expiry,
                'status': status,
                'status_class': status_class,
                'zscore': zscore,
                'stats': stats,
                'signal': signal,
                'hurst': hurst_value,
                'hurst_regime': hurst_regime,
                'leverage': leverage,
                'margin_per_lot_spot': margin_per_lot_spot,
                'margin_per_lot_futures': margin_per_lot_futures,
                'margin_per_lot_total': margin_per_lot_total,
                'margin_required': margin_required,
                'margin_with_buffer': margin_with_buffer,
                'user_lot_size': user_lot_size,
                'timestamp': datetime.now().strftime('%H:%M:%S')
            }

        except Exception as e:
            logger.error(f"Error getting market data for {asset_key}: {e}")
            return None

    def _get_market_session(self):
        """Determine current market session based on UTC time"""
        utc_now = datetime.utcnow()
        hour = utc_now.hour

        # Market session times (approximate, in UTC):
        # Sydney: 22:00 - 07:00 UTC
        # Tokyo/China: 00:00 - 09:00 UTC (China opens ~01:30 UTC)
        # London: 07:00 - 16:00 UTC (opens 07:00/08:00 depending on DST)
        # New York: 13:00 - 22:00 UTC (opens 13:30/14:30 depending on DST)

        sessions = []

        # China/Tokyo session
        if 0 <= hour < 9:
            if hour < 2:
                sessions.append("🇨🇳 China Opening")
            else:
                sessions.append("🇨🇳 China/Tokyo")

        # London session
        if 7 <= hour < 16:
            if 7 <= hour < 8:
                sessions.append("🇬🇧 London Opening")
            else:
                sessions.append("🇬🇧 London")

        # New York session
        if 13 <= hour < 22:
            if 13 <= hour < 14:
                sessions.append("🇺🇸 NY Opening")
            else:
                sessions.append("🇺🇸 New York")

        # Sydney session (overnight)
        if hour >= 22 or hour < 7:
            sessions.append("🇦🇺 Sydney")

        if sessions:
            return " | ".join(sessions)
        return "Between Sessions"

    def _generate_signal(self, asset_key, zscore, stats=None, hurst_value=None, hurst_regime=None, current_spot=None, current_futures=None):
        """Generate trading signal based on z-score with Hurst exponent regime filter"""
        if zscore is None:
            # Show progress if stats available
            if stats and not stats.get('complete', False):
                count = stats.get('count', 0)
                required = stats.get('required', 0)
                pct = (count / required * 100) if required > 0 else 0
                return {
                    'type': 'COLLECTING',
                    'reason': f'Collecting data: {count}/{required} points ({pct:.0f}%)'
                }
            return {'type': 'NO_DATA', 'reason': 'Insufficient data for signal'}

        entry_std = self.config.get('entry_std_dev', 2.0)
        exit_std = self.config.get('exit_std_dev', 0.5)
        stop_loss_std = self.config.get('stop_loss_std_dev', 3.0)
        time_stop_days = self.config.get('time_stop_loss_days', 0)

        # Hurst exponent threshold for mean reversion trading (configurable)
        hurst_threshold = self.config.get('hurst_threshold', 0.5)
        trending_duration_minutes = self.config.get('trending_duration_minutes', 15)
        hurst_enabled = self.config.get('hurst_enabled', True)

        # Get current market session
        market_session = self._get_market_session()

        # Check existing positions
        has_position = asset_key in self.positions

        if not has_position:
            # HURST FILTER: Only enter new positions if regime is mean-reverting (if enabled)
            if hurst_enabled and hurst_value is not None and hurst_value >= hurst_threshold:
                # Track when trending started
                if self.trending_since.get(asset_key) is None:
                    self.trending_since[asset_key] = datetime.now()

                # Check if trending for required duration
                trending_start = self.trending_since[asset_key]
                trending_minutes = (datetime.now() - trending_start).total_seconds() / 60

                # Only block if trending for required duration (or immediate if 0)
                if trending_duration_minutes == 0 or trending_minutes >= trending_duration_minutes:
                    return {
                        'type': 'REGIME_FILTER',
                        'reason': f'Hurst {hurst_value:.3f} >= {hurst_threshold} ({hurst_regime}) for {trending_minutes:.0f}min | {market_session}',
                        'action': 'Entry blocked - Wait for mean-reverting regime'
                    }
                else:
                    # Still in grace period
                    remaining = trending_duration_minutes - trending_minutes
                    return {
                        'type': 'REGIME_FILTER',
                        'reason': f'Hurst {hurst_value:.3f} trending - grace period {remaining:.0f}min left | {market_session}',
                        'action': f'May enter if reverts within {remaining:.0f}min'
                    }
            else:
                # Reset trending tracker when mean-reverting again
                self.trending_since[asset_key] = None

            if zscore > entry_std:
                return {
                    'type': 'SELL_BASIS',
                    'reason': f'Z-score {zscore:.2f} > {entry_std} | Hurst {hurst_value:.3f} ({hurst_regime})' if hurst_value else f'Z-score {zscore:.2f} > {entry_std}',
                    'action': 'Buy Spot + Sell Futures'
                }
            elif zscore < -entry_std:
                return {
                    'type': 'BUY_BASIS',
                    'reason': f'Z-score {zscore:.2f} < -{entry_std} | Hurst {hurst_value:.3f} ({hurst_regime})' if hurst_value else f'Z-score {zscore:.2f} < -{entry_std}',
                    'action': 'Sell Spot + Buy Futures'
                }
        else:
            position = self.positions[asset_key]

            # Check time-based stop loss first (if enabled)
            if time_stop_days > 0:
                try:
                    entry_date = datetime.strptime(position['entry_date'], '%Y-%m-%d')
                    position_age_days = (datetime.now() - entry_date).total_seconds() / 86400  # seconds per day
                    if position_age_days >= time_stop_days:
                        return {
                            'type': 'TIME_STOP',
                            'reason': f'Position held {position_age_days:.1f} days >= {time_stop_days} day limit',
                            'action': 'Time stop - Close position'
                        }
                except (ValueError, KeyError):
                    pass  # If we can't parse entry_date, skip time check

            # Check overnight close (close positions before swap time)
            close_before_overnight = self.config.get('close_before_overnight', False)
            if close_before_overnight:
                close_hour = self.config.get('overnight_close_hour', 16)
                close_minute = self.config.get('overnight_close_minute', 55)
                now = datetime.now()
                # Check if current time is at or past the close time
                if now.hour > close_hour or (now.hour == close_hour and now.minute >= close_minute):
                    return {
                        'type': 'OVERNIGHT_CLOSE',
                        'reason': f'Closing before overnight swap (time: {now.strftime("%H:%M")} >= {close_hour:02d}:{close_minute:02d})',
                        'action': 'Close position to avoid overnight swap'
                    }

            # Check MAX LOSS (absolute dollar stop - regardless of z-score)
            max_loss_per_lot = self.config.get('max_loss_per_lot', 0)
            if max_loss_per_lot > 0 and current_spot is not None and current_futures is not None:
                lot_size = position.get('lot_size', 0.1)
                max_loss = max_loss_per_lot * lot_size

                # Get contract size
                asset_config = self.assets.get(asset_key, {})
                contract_size = asset_config.get('lot_size', 100)

                # Calculate unrealized P&L
                entry_spot = position.get('entry_spot_price', current_spot)
                entry_futures = position.get('entry_futures_price', current_futures)
                spot_diff = current_spot - entry_spot
                futures_diff = current_futures - entry_futures

                if position['direction'] == 'Short Spread':
                    # Short Spread: Sold Futures, Bought Spot
                    futures_pnl = -futures_diff * lot_size * contract_size
                    spot_pnl = spot_diff * lot_size * contract_size
                else:
                    # Long Spread: Bought Futures, Sold Spot
                    futures_pnl = futures_diff * lot_size * contract_size
                    spot_pnl = -spot_diff * lot_size * contract_size

                unrealized_pnl = spot_pnl + futures_pnl

                if unrealized_pnl < -max_loss:
                    return {
                        'type': 'MAX_LOSS',
                        'reason': f'Unrealized loss ${abs(unrealized_pnl):.2f} > max ${max_loss:.2f} (${max_loss_per_lot}/lot × {lot_size} lots)',
                        'action': 'Max loss stop - Close position immediately'
                    }

            if position['direction'] == 'Short Spread':
                if zscore <= exit_std:
                    return {
                        'type': 'CLOSE',
                        'reason': f'Z-score {zscore:.2f} <= {exit_std} (exit)',
                        'action': 'Close Short Spread position'
                    }
                if zscore > stop_loss_std:
                    return {
                        'type': 'STOP_LOSS',
                        'reason': f'Z-score {zscore:.2f} > {stop_loss_std} (stop)',
                        'action': 'Stop loss - Close position'
                    }
            elif position['direction'] == 'Long Spread':
                if zscore >= -exit_std:
                    return {
                        'type': 'CLOSE',
                        'reason': f'Z-score {zscore:.2f} >= -{exit_std} (exit)',
                        'action': 'Close Long Spread position'
                    }
                if zscore < -stop_loss_std:
                    return {
                        'type': 'STOP_LOSS',
                        'reason': f'Z-score {zscore:.2f} < -{stop_loss_std} (stop)',
                        'action': 'Stop loss - Close position'
                    }

        return {'type': 'HOLD', 'reason': 'No action required'}

    def _process_algo_trading(self, asset_key, data):
        """Process algorithmic trading logic"""
        signal = data.get('signal', {})
        signal_type = signal.get('type')

        if signal_type in ['SELL_BASIS', 'BUY_BASIS']:
            # Only open if no existing position for this asset AND no other positions exist
            # Enforce single position mode - only 1 spread at a time
            max_positions = self.config.get('max_positions', 1)
            if asset_key not in self.positions and len(self.positions) < max_positions:
                self._open_position(asset_key, signal_type, data)
            elif len(self.positions) >= max_positions:
                logger.info(f"Skipping {signal_type} - max positions ({max_positions}) reached")

        elif signal_type in ['CLOSE', 'STOP_LOSS', 'TIME_STOP', 'OVERNIGHT_CLOSE', 'MAX_LOSS']:
            if asset_key in self.positions:
                self._close_position(asset_key, signal_type, data)

    def _execute_mt5_order(self, symbol, order_type, volume, comment="", use_limit=False):
        """Execute an order through MT5

        Args:
            symbol: Trading symbol
            order_type: mt5.ORDER_TYPE_BUY or mt5.ORDER_TYPE_SELL
            volume: Lot size
            comment: Order comment
            use_limit: If True, use limit order (guaranteed price, may not fill)
                      If False, use market order (guaranteed fill, may have slippage)
        """
        try:
            symbol_info = mt5.symbol_info(symbol)
            if symbol_info is None:
                return {'success': False, 'error': f'Symbol {symbol} not found'}

            if not symbol_info.visible:
                mt5.symbol_select(symbol, True)

            # Get current price
            tick = mt5.symbol_info_tick(symbol)
            if tick is None:
                return {'success': False, 'error': f'Cannot get tick for {symbol}'}

            price = tick.ask if order_type == mt5.ORDER_TYPE_BUY else tick.bid

            # Validate and adjust volume to MT5 requirements
            vol_min = symbol_info.volume_min
            vol_max = symbol_info.volume_max
            vol_step = symbol_info.volume_step

            # Ensure volume is within valid range
            if volume < vol_min:
                logger.warning(f"Volume {volume} below minimum {vol_min} for {symbol}, adjusting to minimum")
                volume = vol_min
            elif volume > vol_max:
                logger.warning(f"Volume {volume} above maximum {vol_max} for {symbol}, adjusting to maximum")
                volume = vol_max

            # Round to valid step
            if vol_step > 0:
                volume = round(volume / vol_step) * vol_step
                volume = round(volume, 2)

            # Determine filling mode based on what the symbol/broker supports
            # For market orders, prefer IOC as it's more flexible than FOK
            filling_mode = symbol_info.filling_mode
            if not use_limit and (filling_mode & 2):  # Market order and IOC supported
                filling_type = mt5.ORDER_FILLING_IOC
            elif filling_mode & 1:  # FOK supported
                filling_type = mt5.ORDER_FILLING_FOK
            elif filling_mode & 2:  # IOC supported
                filling_type = mt5.ORDER_FILLING_IOC
            else:  # Use IOC as fallback when filling_mode is 0 (some brokers don't report correctly)
                filling_type = mt5.ORDER_FILLING_IOC if filling_mode == 0 else mt5.ORDER_FILLING_RETURN

            order_mode = "LIMIT" if use_limit else "MARKET"
            logger.info(f"MT5 {order_mode} order: {symbol} volume={volume} price={price} filling={filling_type} (mode={filling_mode})")

            # Build request - same structure for both limit and market, just different deviation
            request = {
                "action": mt5.TRADE_ACTION_DEAL,
                "symbol": symbol,
                "volume": volume,
                "type": order_type,
                "price": price,
                "deviation": 0 if use_limit else 20,
                "magic": 123456,
                "comment": comment,
                "type_time": mt5.ORDER_TIME_GTC,
                "type_filling": filling_type,
            }

            result = mt5.order_send(request)
            if result is None:
                return {'success': False, 'error': 'Order send failed - no result'}

            if result.retcode != mt5.TRADE_RETCODE_DONE:
                return {
                    'success': False,
                    'error': f'Order failed: {result.comment}',
                    'retcode': result.retcode
                }

            return {
                'success': True,
                'ticket': result.order,
                'price': result.price,
                'volume': result.volume,
                'comment': result.comment
            }

        except Exception as e:
            logger.error(f"MT5 order execution error: {e}")
            return {'success': False, 'error': str(e)}

    def _close_mt5_position(self, ticket, symbol, volume, position_type, use_limit=False):
        """Close an MT5 position by ticket

        Args:
            ticket: MT5 position ticket number
            symbol: Trading symbol
            volume: Lot size
            position_type: Original position type (BUY or SELL)
            use_limit: If True, use limit order (guaranteed price, may not fill)
                      If False, use market order (guaranteed fill, may have slippage)
        """
        try:
            # Opposite order to close
            close_type = mt5.ORDER_TYPE_SELL if position_type == mt5.ORDER_TYPE_BUY else mt5.ORDER_TYPE_BUY

            symbol_info = mt5.symbol_info(symbol)
            if symbol_info is None:
                return {'success': False, 'error': f'Symbol {symbol} not found'}

            tick = mt5.symbol_info_tick(symbol)
            if tick is None:
                return {'success': False, 'error': f'Cannot get tick for {symbol}'}

            price = tick.bid if position_type == mt5.ORDER_TYPE_BUY else tick.ask

            # Validate and adjust volume to MT5 requirements
            vol_min = symbol_info.volume_min
            vol_max = symbol_info.volume_max
            vol_step = symbol_info.volume_step

            if volume < vol_min:
                volume = vol_min
            elif volume > vol_max:
                volume = vol_max

            if vol_step > 0:
                volume = round(volume / vol_step) * vol_step
                volume = round(volume, 2)

            # Determine filling mode based on what the symbol/broker supports
            # For market orders (especially closes), prefer IOC as it's more flexible
            filling_mode = symbol_info.filling_mode
            if not use_limit and (filling_mode & 2):  # Market order and IOC supported
                filling_type = mt5.ORDER_FILLING_IOC
            elif filling_mode & 1:  # FOK supported
                filling_type = mt5.ORDER_FILLING_FOK
            elif filling_mode & 2:  # IOC supported
                filling_type = mt5.ORDER_FILLING_IOC
            else:  # Use RETURN as fallback (or try IOC if filling_mode is 0)
                filling_type = mt5.ORDER_FILLING_IOC if filling_mode == 0 else mt5.ORDER_FILLING_RETURN

            order_mode = "LIMIT" if use_limit else "MARKET"
            logger.info(f"MT5 {order_mode} close: {symbol} ticket={ticket} volume={volume} price={price} filling={filling_type} (mode={filling_mode})")

            # Build request - same structure for both limit and market, just different deviation
            request = {
                "action": mt5.TRADE_ACTION_DEAL,
                "symbol": symbol,
                "volume": volume,
                "type": close_type,
                "position": ticket,
                "price": price,
                "deviation": 0 if use_limit else 20,
                "magic": 123456,
                "comment": "Close position",
                "type_time": mt5.ORDER_TIME_GTC,
                "type_filling": filling_type,
            }

            result = mt5.order_send(request)
            if result is None:
                return {'success': False, 'error': 'Close order failed - no result'}

            if result.retcode != mt5.TRADE_RETCODE_DONE:
                return {'success': False, 'error': f'Close failed: {result.comment}'}

            return {'success': True, 'ticket': result.order, 'price': result.price}

        except Exception as e:
            logger.error(f"MT5 close position error: {e}")
            return {'success': False, 'error': str(e)}

    def _open_position(self, asset_key, signal_type, data):
        """Open a new position with full trade tracking"""
        trade_id = str(uuid.uuid4())[:8]
        lot_size = self.config.get('lot_size', 0.1)

        asset_data = self.active_assets.get(asset_key, {})
        spot_symbol = asset_data.get('spot_symbol')
        futures_symbol = asset_data.get('futures_symbol')

        # Direction: SELL_BASIS = Short Spread (Sell Futures, Buy Spot)
        #            BUY_BASIS = Long Spread (Buy Futures, Sell Spot)
        direction = 'Short Spread' if signal_type == 'SELL_BASIS' else 'Long Spread'

        # Calculate spread cost (bid-ask spread for entry)
        spot_spread_cents = data.get('spot_spread', 0)  # in cents
        futures_spread_cents = data.get('futures_spread', 0)  # in cents
        asset_config = self.assets.get(asset_key, {})
        contract_size = asset_config.get('lot_size', 100)  # oz per lot
        spread_cost = ((spot_spread_cents + futures_spread_cents) / 100) * lot_size * contract_size

        # Get current statistics to lock in target/stop levels at entry
        stats = self.get_statistics(asset_key)
        entry_spread = data['futures_price'] - data['spot_price']

        # COST-AWARE TARGET CALCULATION
        # Round-trip cost = entry spread cost + exit spread cost (approximately 2x entry)
        round_trip_cost = spread_cost * 2
        # Min profit scales with lot size: min_profit_per_lot * lot_size
        min_profit_per_lot = self.config.get('min_profit_per_lot', 50)
        min_profit = min_profit_per_lot * lot_size

        # IMPORTANT: Target is based on min_profit ONLY, not min_profit + costs
        # The min_profit_per_lot setting represents desired NET profit after costs
        # Costs are subtracted from realized P&L at close, not added to required spread movement
        #
        # OLD (buggy): total_required = round_trip_cost + min_profit  <- Caused targets to be too far!
        # NEW (correct): Target movement only needs to cover the net profit target
        total_required = min_profit

        # Minimum spread move needed to achieve target profit
        # spread_move * lot_size * contract_size = total_required
        min_spread_move = total_required / (lot_size * contract_size)

        logger.info(f"Cost calculation: Entry cost=${spread_cost:.2f}, Round-trip=${round_trip_cost:.2f}, "
                    f"Min profit=${min_profit:.2f} (${min_profit_per_lot}/lot × {lot_size} lots), "
                    f"Required spread move={min_spread_move:.4f} (costs deducted at close)")

        # Calculate target exit and stop loss based on entry statistics
        # Long Spread: entered low (z < -2), exit when z >= -exit_std (spread rises to lower_exit)
        # Short Spread: entered high (z > 2), exit when z <= exit_std (spread falls to upper_exit)
        if direction == 'Long Spread':
            # Statistical target: spread rises toward mean
            statistical_target = stats.get('lower_exit', stats.get('mean', entry_spread)) if stats else entry_spread
            # Cost-aware target: ensure minimum profit (spread must rise at least min_spread_move)
            cost_aware_target = entry_spread + min_spread_move
            # Use the HIGHER of the two (further from entry = more profit)
            target_exit = max(statistical_target, cost_aware_target)
            # Stop: spread falls further (hits lower_stop)
            stop_loss_spread = stats.get('lower_stop', entry_spread) if stats else entry_spread
            logger.info(f"Long Spread targets: Statistical={statistical_target:.4f}, Cost-aware={cost_aware_target:.4f}, Final={target_exit:.4f}")
        else:  # Short Spread
            # Statistical target: spread falls toward mean
            statistical_target = stats.get('upper_exit', stats.get('mean', entry_spread)) if stats else entry_spread
            # Cost-aware target: ensure minimum profit (spread must fall at least min_spread_move)
            cost_aware_target = entry_spread - min_spread_move
            # Use the LOWER of the two (further from entry = more profit)
            target_exit = min(statistical_target, cost_aware_target)
            # Stop: spread rises further (hits upper_stop)
            stop_loss_spread = stats.get('upper_stop', entry_spread) if stats else entry_spread
            logger.info(f"Short Spread targets: Statistical={statistical_target:.4f}, Cost-aware={cost_aware_target:.4f}, Final={target_exit:.4f}")

        position = {
            'trade_id': trade_id,
            'asset': asset_key,
            'direction': direction,
            'entry_date': datetime.now().strftime('%Y-%m-%d %H:%M'),
            'entry_zscore': data['zscore'],
            'entry_spot_price': data['spot_price'],
            'entry_futures_price': data['futures_price'],
            'entry_spread': entry_spread,
            'target_exit': target_exit,
            'stop_loss_spread': stop_loss_spread,
            'lot_size': lot_size,
            'spread_cost': spread_cost,
            'status': 'OPEN',
            'order_status': 'PENDING'
        }

        # Execute trades through MT5 (both paper and live mode place real orders)
        # SELL_BASIS: Sell Futures + Buy Spot
        # BUY_BASIS: Buy Futures + Sell Spot
        if signal_type == 'SELL_BASIS':
            futures_order_type = mt5.ORDER_TYPE_SELL
            spot_order_type = mt5.ORDER_TYPE_BUY
        else:
            futures_order_type = mt5.ORDER_TYPE_BUY
            spot_order_type = mt5.ORDER_TYPE_SELL

        mode_label = 'PAPER' if self.config.get('paper_mode', True) else 'LIVE'

        # Execute futures order (market order for guaranteed fill)
        futures_result = self._execute_mt5_order(
            futures_symbol, futures_order_type, lot_size,
            f"{asset_key} {direction} Futures",
            use_limit=False
        )

        # Execute spot order (market order for guaranteed fill)
        spot_result = self._execute_mt5_order(
            spot_symbol, spot_order_type, lot_size,
            f"{asset_key} {direction} Spot",
            use_limit=False
        )

        if futures_result['success'] and spot_result['success']:
            position['mt5_futures_ticket'] = futures_result['ticket']
            position['mt5_spot_ticket'] = spot_result['ticket']
            position['order_status'] = 'FILLED'

            # Use ACTUAL fill prices from MT5 (not signal-time prices)
            # Keep original signal z-score (shows where signal fired)
            actual_futures_price = futures_result.get('price')
            actual_spot_price = spot_result.get('price')

            if actual_futures_price and actual_spot_price:
                position['entry_futures_price'] = actual_futures_price
                position['entry_spot_price'] = actual_spot_price
                logger.info(f"Using actual fill prices: Futures={actual_futures_price:.2f}, Spot={actual_spot_price:.2f}")

            logger.info(f"{mode_label} TRADE FILLED: {asset_key} {direction} - Futures #{futures_result['ticket']}, Spot #{spot_result['ticket']}")
        else:
            position['order_status'] = 'PARTIAL' if (futures_result['success'] or spot_result['success']) else 'REJECTED'
            error_msg = f"Futures: {futures_result.get('error', 'OK')}, Spot: {spot_result.get('error', 'OK')}"
            logger.error(f"{mode_label} TRADE FAILED: {asset_key} {direction} - {error_msg}")

        self.positions[asset_key] = position
        self.db.save_trade(position)

    def _get_mt5_position_costs(self, ticket):
        """Get swap and commission from MT5 position by ticket"""
        try:
            positions = mt5.positions_get(ticket=ticket)
            if positions and len(positions) > 0:
                pos = positions[0]
                return {
                    'swap': getattr(pos, 'swap', 0),
                    'commission': getattr(pos, 'commission', 0),
                    'profit': getattr(pos, 'profit', 0)
                }
        except Exception as e:
            logger.error(f"Error getting MT5 position costs: {e}")
        return {'swap': 0, 'commission': 0, 'profit': 0}

    def _close_position(self, asset_key, close_reason, data):
        """Close an existing position with P&L calculation"""
        if asset_key not in self.positions:
            return

        position = self.positions[asset_key]
        # Parse entry_date (handles both old '%Y-%m-%d' and new '%Y-%m-%d %H:%M' formats)
        entry_date_str = position['entry_date']
        try:
            entry_date = datetime.strptime(entry_date_str, '%Y-%m-%d %H:%M')
        except ValueError:
            entry_date = datetime.strptime(entry_date_str, '%Y-%m-%d')
        exit_date = datetime.now()

        # Update position with exit data (include time)
        position['exit_date'] = exit_date.strftime('%Y-%m-%d %H:%M')
        position['days_held'] = (exit_date - entry_date).days or 1
        position['exit_zscore'] = data['zscore']
        position['exit_spot_price'] = data['spot_price']
        position['exit_futures_price'] = data['futures_price']

        # Calculate P&L for each leg
        lot_size = position.get('lot_size', 0.1)
        asset_config = self.assets.get(asset_key, {})
        contract_size = asset_config.get('lot_size', 100)  # oz per lot

        # Price differences
        spot_diff = position['exit_spot_price'] - position['entry_spot_price']
        futures_diff = position['exit_futures_price'] - position['entry_futures_price']

        # Calculate P&L based on direction
        if position['direction'] == 'Short Spread':
            # Short Spread: Sold Futures, Bought Spot
            # Profit on futures when price drops, profit on spot when price rises
            position['futures_pnl'] = -futures_diff * lot_size * contract_size
            position['spot_pnl'] = spot_diff * lot_size * contract_size
        else:
            # Long Spread: Bought Futures, Sold Spot
            # Profit on futures when price rises, profit on spot when price drops
            position['futures_pnl'] = futures_diff * lot_size * contract_size
            position['spot_pnl'] = -spot_diff * lot_size * contract_size

        position['gross_pnl'] = position['spot_pnl'] + position['futures_pnl']

        # Get swap and commission from MT5 positions before closing
        spot_costs = self._get_mt5_position_costs(position.get('mt5_spot_ticket'))
        futures_costs = self._get_mt5_position_costs(position.get('mt5_futures_ticket'))

        position['swap_cost'] = spot_costs['swap'] + futures_costs['swap']

        # Get commission from MT5, or use manual setting if MT5 doesn't report it
        mt5_commission = spot_costs['commission'] + futures_costs['commission']
        # Manual commission is entered as positive but stored as negative (it's a cost)
        # *2 for spot + futures legs (each leg = entry + exit trades)
        manual_commission = -abs(self.config.get('commission_per_lot', 0)) * lot_size * 2

        # Use manual commission if MT5 commission is 0 (common in paper mode or with some brokers)
        position['commission'] = mt5_commission if mt5_commission != 0 else manual_commission

        logger.info(f"MT5 Costs - Swap: ${position['swap_cost']:.2f}, MT5 Comm: ${mt5_commission:.2f}, Manual Comm: ${manual_commission:.2f}, Using: ${position['commission']:.2f}")

        position['net_pnl'] = position['gross_pnl'] + position['swap_cost'] + position['commission']

        # Calculate return % based on actual margin used
        # Margin = (Spot Position Value + Futures Position Value) × (1 + Buffer) / Leverage
        account = mt5.account_info()
        leverage = account.leverage if account else 100  # Default 1:100

        spot_position_value = position['entry_spot_price'] * lot_size * contract_size
        futures_position_value = position['entry_futures_price'] * lot_size * contract_size
        total_notional = spot_position_value + futures_position_value
        margin_with_buffer = (total_notional * 1.15) / leverage  # 15% buffer for price fluctuation

        position['return_pct'] = (position['net_pnl'] / margin_with_buffer * 100) if margin_with_buffer > 0 else 0

        mode_label = 'PAPER' if self.config.get('paper_mode', True) else 'LIVE'

        # Close MT5 positions (both paper and live mode)
        asset_data = self.active_assets.get(asset_key, {})
        spot_symbol = asset_data.get('spot_symbol')
        futures_symbol = asset_data.get('futures_symbol')

        # Track if MT5 closes were successful (always use market orders for guaranteed fill)
        futures_closed = False
        spot_closed = False

        if position.get('mt5_futures_ticket'):
            result = self._close_mt5_position(
                position['mt5_futures_ticket'],
                futures_symbol, lot_size,
                mt5.ORDER_TYPE_BUY if position['direction'] == 'Short Spread' else mt5.ORDER_TYPE_SELL,
                use_limit=False
            )
            if result['success']:
                futures_closed = True
                logger.info(f"Futures position closed: ticket {position['mt5_futures_ticket']}")
            else:
                logger.error(f"CRITICAL: Failed to close futures position {position['mt5_futures_ticket']}: {result.get('error')}")
        else:
            futures_closed = True  # No ticket to close

        if position.get('mt5_spot_ticket'):
            result = self._close_mt5_position(
                position['mt5_spot_ticket'],
                spot_symbol, lot_size,
                mt5.ORDER_TYPE_SELL if position['direction'] == 'Short Spread' else mt5.ORDER_TYPE_BUY,
                use_limit=False
            )
            if result['success']:
                spot_closed = True
                logger.info(f"Spot position closed: ticket {position['mt5_spot_ticket']}")
            else:
                logger.error(f"CRITICAL: Failed to close spot position {position['mt5_spot_ticket']}: {result.get('error')}")
        else:
            spot_closed = True  # No ticket to close

        # Only mark as closed if BOTH positions were successfully closed
        if not (futures_closed and spot_closed):
            logger.error(f"POSITION NOT FULLY CLOSED: {asset_key} - Futures: {futures_closed}, Spot: {spot_closed}")
            # Keep position in tracking so we can try again
            return

        logger.info(f"{mode_label} CLOSE: {asset_key} - {close_reason} - Gross: ${position['gross_pnl']:.2f}, Swap: ${position['swap_cost']:.2f}, Comm: ${position['commission']:.2f}, Net: ${position['net_pnl']:.2f}")

        # Track consecutive stop losses for auto-enabling Hurst protection
        if close_reason == 'STOP_LOSS':
            self.consecutive_stop_losses += 1
            logger.warning(f"Stop loss #{self.consecutive_stop_losses} triggered")

            # After 3 consecutive stop losses, auto-enable Hurst protection
            if self.consecutive_stop_losses >= 3:
                previous_lot_size = self.config.get('lot_size', 0.1)
                logger.warning(f"3 consecutive stop losses! Auto-enabling Hurst filter (threshold=0.5, duration=20min) and reducing lot size from {previous_lot_size} to 0.01")
                self.config['hurst_enabled'] = True
                self.config['hurst_threshold'] = 0.5
                self.config['trending_duration_minutes'] = 20
                self.config['lot_size'] = 0.01  # Reset to minimum lot size
                self.db.save_config(self.config)
                # Reset counter after enabling protection
                self.consecutive_stop_losses = 0
        else:
            # Reset counter on normal exit (CLOSE, TIME_STOP, etc.)
            if self.consecutive_stop_losses > 0:
                logger.info(f"Resetting stop loss counter (was {self.consecutive_stop_losses})")
            self.consecutive_stop_losses = 0

        position['status'] = 'CLOSED'
        self.db.save_trade(position)
        del self.positions[asset_key]

    def get_all_data(self):
        """Get data for the active asset (single-asset mode)"""
        data = {}

        # SINGLE ASSET MODE: Only process the active asset
        market_data = self.get_market_data('ACTIVE')
        if market_data:
            data['ACTIVE'] = market_data

        self.last_update = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        return data

    def reload_config(self):
        """Reload configuration from database"""
        self.config = self.db.get_config() or {}

    def get_mt5_account_info(self):
        """Get MT5 account information"""
        try:
            account = mt5.account_info()
            if account is None:
                return None

            return {
                'login': account.login,
                'server': account.server,
                'name': account.name,
                'currency': account.currency,
                'balance': account.balance,
                'equity': account.equity,
                'margin': account.margin,
                'free_margin': account.margin_free,
                'margin_level': account.margin_level if account.margin > 0 else 0,
                'profit': account.profit,
                'leverage': account.leverage,
                'trade_allowed': account.trade_allowed,
                'trade_expert': account.trade_expert
            }
        except Exception as e:
            logger.error(f"Error getting MT5 account info: {e}")
            return None

    def get_mt5_positions(self):
        """Get all open positions from MT5 with P&L"""
        try:
            positions = mt5.positions_get()
            if positions is None or len(positions) == 0:
                return []

            result = []
            for pos in positions:
                # Calculate return percentage
                if pos.price_open > 0:
                    if pos.type == 0:  # BUY
                        return_pct = ((pos.price_current - pos.price_open) / pos.price_open) * 100
                    else:  # SELL
                        return_pct = ((pos.price_open - pos.price_current) / pos.price_open) * 100
                else:
                    return_pct = 0

                result.append({
                    'ticket': pos.ticket,
                    'symbol': pos.symbol,
                    'type': 'BUY' if pos.type == 0 else 'SELL',
                    'volume': pos.volume,
                    'price_open': pos.price_open,
                    'price_current': pos.price_current,
                    'sl': getattr(pos, 'sl', 0),
                    'tp': getattr(pos, 'tp', 0),
                    'profit': getattr(pos, 'profit', 0),
                    'return_pct': return_pct,
                    'swap': getattr(pos, 'swap', 0),
                    'commission': getattr(pos, 'commission', 0),
                    'time': datetime.fromtimestamp(pos.time).strftime('%Y-%m-%d %H:%M:%S'),
                    'magic': getattr(pos, 'magic', 0),
                    'comment': getattr(pos, 'comment', '')
                })

            return result
        except Exception as e:
            logger.error(f"Error getting MT5 positions: {e}")
            return []

    def get_enriched_positions(self):
        """Get algo positions with unrealized P&L from MT5 actual positions"""
        enriched = []
        for asset_key, position in self.positions.items():
            pos_copy = position.copy()

            # Get current prices for this asset
            asset_data = self.active_assets.get(asset_key, {})
            if asset_data:
                current_data = self.get_market_data(asset_key)
                if current_data:
                    entry_spot = position.get('entry_spot_price') or 0
                    entry_futures = position.get('entry_futures_price') or 0
                    current_spot = current_data.get('spot_price') or 0
                    current_futures = current_data.get('futures_price') or 0
                    lot_size = position.get('lot_size') or 0.1

                    # Get ACTUAL P&L from MT5 positions using stored ticket numbers
                    # This ensures P&L matches MT5 exactly (includes slippage, real fill prices)
                    spot_ticket = position.get('mt5_spot_ticket')
                    futures_ticket = position.get('mt5_futures_ticket')

                    mt5_spot_pnl = 0
                    mt5_futures_pnl = 0

                    if spot_ticket:
                        try:
                            spot_positions = mt5.positions_get(ticket=spot_ticket)
                            if spot_positions and len(spot_positions) > 0:
                                mt5_spot_pnl = getattr(spot_positions[0], 'profit', 0)
                        except:
                            pass

                    if futures_ticket:
                        try:
                            futures_positions = mt5.positions_get(ticket=futures_ticket)
                            if futures_positions and len(futures_positions) > 0:
                                mt5_futures_pnl = getattr(futures_positions[0], 'profit', 0)
                        except:
                            pass

                    # Use MT5's actual P&L (matches what you see in MT5 terminal)
                    unrealized_pnl = mt5_spot_pnl + mt5_futures_pnl
                    pos_copy['unrealized_pnl'] = unrealized_pnl
                    pos_copy['mt5_spot_pnl'] = mt5_spot_pnl
                    pos_copy['mt5_futures_pnl'] = mt5_futures_pnl
                    pos_copy['current_spot'] = current_spot
                    pos_copy['current_futures'] = current_futures
                    pos_copy['current_spread'] = current_futures - current_spot
                    pos_copy['entry_spread'] = position.get('entry_spread', entry_futures - entry_spot)

                    # Use stored target/stop from entry time (locked in when position opened)
                    pos_copy['target_exit'] = position.get('target_exit', pos_copy['entry_spread'])
                    pos_copy['stop_loss_exit'] = position.get('stop_loss_spread', pos_copy['entry_spread'])

                    # Calculate spread cost (bid-ask spread for both spot + futures)
                    spot_spread_cents = current_data.get('spot_spread', 0)  # in cents
                    futures_spread_cents = current_data.get('futures_spread', 0)  # in cents
                    # Total spread cost = (spot spread + futures spread) * lot_size * 100 oz
                    # Since spread is in cents, divide by 100 to get dollars
                    spread_cost = ((spot_spread_cents + futures_spread_cents) / 100) * lot_size * 100
                    pos_copy['spread_cost'] = spread_cost
                    pos_copy['spot_spread'] = spot_spread_cents
                    pos_copy['futures_spread'] = futures_spread_cents

            enriched.append(pos_copy)

        return enriched

    def sync_positions_with_mt5(self):
        """Sync portal positions with actual MT5 positions"""
        mt5_positions = self.get_mt5_positions()

        # Get relevant symbols for our assets
        relevant_symbols = set()
        for asset_key, asset_data in self.active_assets.items():
            relevant_symbols.add(asset_data.get('spot_symbol', ''))
            relevant_symbols.add(asset_data.get('futures_symbol', ''))

        # Find MT5 positions that match our symbols
        synced_positions = []
        for pos in mt5_positions:
            if pos['symbol'] in relevant_symbols:
                # Determine which asset this belongs to
                asset_key = None
                for ak, ad in self.active_assets.items():
                    if pos['symbol'] in [ad.get('spot_symbol'), ad.get('futures_symbol')]:
                        asset_key = ak
                        break

                if asset_key:
                    pos['asset'] = asset_key
                    pos['is_spot'] = pos['symbol'] == self.active_assets[asset_key].get('spot_symbol')
                    synced_positions.append(pos)

        return synced_positions


# =============================================================================
# GLOBAL MONITOR INSTANCE
# =============================================================================
monitor = TradingMonitor()


# =============================================================================
# FLASK ROUTES
# =============================================================================
@app.route('/')
def index():
    """Main monitoring page"""
    if not monitor.is_initialized:
        return redirect(url_for('setup'))
    return render_template('monitor.html')


@app.route('/setup', methods=['GET', 'POST'])
def setup():
    """Setup page for configuration"""
    if request.method == 'POST':
        try:
            # Get generic asset configuration (single asset mode)
            asset_name = request.form.get('asset_name', 'GOLD').strip() or 'GOLD'
            spot_symbol = request.form.get('spot_symbol', '').strip()
            futures_symbol = request.form.get('futures_symbol', '').strip()
            futures_expiry = request.form.get('futures_expiry', '').strip()
            contract_size = float(request.form.get('contract_size', 100) or 100)
            swap_charge = float(request.form.get('swap_charge', 0) or 0)

            # Save to generic config fields
            monitor.config['asset_name'] = asset_name
            monitor.config['spot_symbol'] = spot_symbol
            monitor.config['futures_symbol'] = futures_symbol
            monitor.config['futures_expiry'] = futures_expiry
            monitor.config['contract_size'] = contract_size
            monitor.config['swap_charge'] = swap_charge

            # Also save to gold_* fields for backwards compatibility
            monitor.config['gold_asset_name'] = asset_name
            monitor.config['gold_spot_symbol'] = spot_symbol
            monitor.config['gold_futures_symbol'] = futures_symbol
            monitor.config['gold_futures_expiry'] = futures_expiry
            monitor.config['gold_contract_size'] = contract_size
            monitor.config['gold_swap_charge'] = swap_charge

            monitor.db.save_config(monitor.config)

            logger.info(f"Setup: {asset_name}={spot_symbol}/{futures_symbol}")

            if monitor.initialize_mt5():
                monitor.is_initialized = True
                monitor.start_background_updates()
                return redirect(url_for('index'))
            else:
                error_msg = monitor.error_message or "Failed to connect to MT5. Make sure MT5 is running and logged in."
                logger.error(f"Setup failed: {error_msg}")
                return render_template('setup.html', error=error_msg, config=monitor.config)

        except ValueError as e:
            logger.error(f"Setup ValueError: {e}")
            return render_template('setup.html', error=f"Invalid input: {e}", config=monitor.config)
        except Exception as e:
            import traceback
            logger.error(f"Setup error: {e}\n{traceback.format_exc()}")
            return render_template('setup.html', error=f"Error: {e}", config=monitor.config)

    return render_template('setup.html', error=None, config=monitor.config)


@app.route('/settings', methods=['GET', 'POST'])
def settings():
    """Trading settings page"""
    if request.method == 'POST':
        try:
            monitor.config['lookback_period'] = int(request.form.get('lookback_period', 90))
            monitor.config['lookback_unit'] = request.form.get('lookback_unit', 'minutes')
            monitor.config['entry_std_dev'] = float(request.form.get('entry_std_dev', 2.0))
            monitor.config['exit_std_dev'] = float(request.form.get('exit_std_dev', 0.5))
            monitor.config['stop_loss_std_dev'] = float(request.form.get('stop_loss_std_dev', 3.0))
            monitor.config['time_stop_loss_days'] = float(request.form.get('time_stop_loss_days', 0))
            monitor.config['max_positions'] = int(request.form.get('max_positions', 3))
            monitor.config['lot_size'] = float(request.form.get('lot_size', 0.1))
            monitor.config['commission_per_lot'] = float(request.form.get('commission_per_lot', 0))
            monitor.config['min_profit_per_lot'] = float(request.form.get('min_profit_per_lot', 50))
            monitor.config['max_loss_per_lot'] = float(request.form.get('max_loss_per_lot', 100))
            monitor.config['hurst_threshold'] = float(request.form.get('hurst_threshold', 0.5))
            monitor.config['trending_duration_minutes'] = int(request.form.get('trending_duration_minutes', 15))
            monitor.config['hurst_enabled'] = request.form.get('hurst_enabled') == 'on'
            monitor.config['close_before_overnight'] = request.form.get('close_before_overnight') == 'on'
            monitor.config['overnight_close_hour'] = int(request.form.get('overnight_close_hour', 16))
            monitor.config['overnight_close_minute'] = int(request.form.get('overnight_close_minute', 55))
            monitor.config['selected_asset'] = request.form.get('selected_asset', 'GOLD')

            monitor.db.save_config(monitor.config)
            return redirect(url_for('settings') + '?saved=1')

        except ValueError as e:
            return render_template('settings.html', error=str(e), config=monitor.config)

    saved = request.args.get('saved')
    return render_template('settings.html', error=None, config=monitor.config, saved=saved)


@app.route('/api/data')
def get_data():
    """API endpoint for market data"""
    if not monitor.is_initialized:
        return jsonify({'error': 'Not initialized'}), 400

    data = monitor.get_all_data()

    # Calculate summary
    cheap_count = sum(1 for d in data.values() if d['swap_diff'] < 0)
    expensive_count = sum(1 for d in data.values() if d['swap_diff'] > 5)
    fair_count = len(data) - cheap_count - expensive_count

    # Get MT5 account info and positions
    account_info = monitor.get_mt5_account_info()
    mt5_positions = monitor.get_mt5_positions()

    # Get trade history and summary
    trade_history = monitor.db.get_trades(limit=500)
    trade_summary = monitor.db.get_trade_summary()

    # Get z-score history for charting (single asset mode)
    zscore_history = {
        'ACTIVE': list(monitor.zscore_history.get('ACTIVE', []))
    }

    # Get price history for charting (single asset mode)
    price_history = {
        'ACTIVE': list(monitor.price_history.get('ACTIVE', []))
    }

    return jsonify({
        'data': data,
        'summary': {
            'cheap': cheap_count,
            'fair': fair_count,
            'expensive': expensive_count
        },
        'account': account_info,
        'mt5_positions': mt5_positions,
        'positions': monitor.get_enriched_positions(),
        'trade_history': trade_history,
        'trade_summary': trade_summary,
        'zscore_history': zscore_history,
        'price_history': price_history,
        'config': {
            'algo_enabled': monitor.config.get('algo_enabled', False),
            'paper_mode': monitor.config.get('paper_mode', True),
            'lookback_period': monitor.config.get('lookback_period', 90),
            'lookback_unit': monitor.config.get('lookback_unit', 'minutes'),
            'entry_std_dev': monitor.config.get('entry_std_dev', 2.0),
            'exit_std_dev': monitor.config.get('exit_std_dev', 0.5),
            'stop_loss_std_dev': monitor.config.get('stop_loss_std_dev', 3.0),
            'time_stop_loss_days': monitor.config.get('time_stop_loss_days', 0),
            'hurst_threshold': monitor.config.get('hurst_threshold', 0.5),
            'trending_duration_minutes': monitor.config.get('trending_duration_minutes', 15)
        },
        'market_session': monitor._get_market_session(),
        'last_update': monitor.last_update
    })


@app.route('/api/toggle_algo', methods=['POST'])
def toggle_algo():
    """Toggle algorithmic trading"""
    data = request.json
    enabled = data.get('enabled', False)

    monitor.config['algo_enabled'] = enabled
    monitor.db.save_config(monitor.config)

    logger.info(f"Algo trading {'ENABLED' if enabled else 'DISABLED'}")
    return jsonify({'status': 'success', 'algo_enabled': enabled})


@app.route('/api/toggle_paper', methods=['POST'])
def toggle_paper():
    """Toggle paper/live mode"""
    data = request.json
    paper = data.get('paper', True)

    monitor.config['paper_mode'] = paper
    monitor.db.save_config(monitor.config)

    logger.info(f"Trading mode: {'PAPER' if paper else 'LIVE'}")
    return jsonify({'status': 'success', 'paper_mode': paper})


@app.route('/api/reset_statistics', methods=['POST'])
def reset_statistics():
    """Reset all statistics - clears spread cache, price history, and z-score history"""
    try:
        # Clear in-memory spread cache
        for asset_key in monitor.spread_cache:
            monitor.spread_cache[asset_key].clear()

        # Clear in-memory z-score history
        for asset_key in monitor.zscore_history:
            monitor.zscore_history[asset_key].clear()

        # Clear in-memory price history
        for asset_key in monitor.price_history:
            monitor.price_history[asset_key].clear()

        # Clear price history from database
        conn = monitor.db.get_connection()
        cursor = conn.cursor()
        cursor.execute('DELETE FROM price_history')
        conn.commit()
        conn.close()

        logger.info("Statistics reset - spread cache, z-score history, and price history cleared")
        return jsonify({'status': 'success', 'message': 'Statistics reset successfully'})

    except Exception as e:
        logger.error(f"Error resetting statistics: {e}")
        return jsonify({'status': 'error', 'message': str(e)}), 500


@app.route('/api/trades')
def get_trades():
    """Get trade history"""
    trades = monitor.db.get_trades(500)
    return jsonify(trades)


@app.route('/api/trades/csv')
def download_trades_csv():
    """Download trade history as CSV"""
    import csv
    import io

    trades = monitor.db.get_trades(limit=10000)  # Get all trades

    # Create CSV in memory
    output = io.StringIO()
    writer = csv.writer(output)

    # Header row
    writer.writerow([
        'Trade ID', 'Asset', 'Direction', 'Entry Date', 'Exit Date', 'Days Held',
        'Entry Z-Score', 'Exit Z-Score', 'Entry Spot', 'Entry Futures',
        'Exit Spot', 'Exit Futures', 'Spot P&L', 'Futures P&L', 'Gross P&L',
        'Swap Cost', 'Commission', 'Spread Cost', 'Net P&L', 'Return %',
        'Lot Size', 'Status'
    ])

    # Data rows
    for t in trades:
        writer.writerow([
            t.get('trade_id', ''),
            t.get('asset', ''),
            t.get('direction', ''),
            t.get('entry_date', ''),
            t.get('exit_date', ''),
            t.get('days_held', ''),
            t.get('entry_zscore', ''),
            t.get('exit_zscore', ''),
            t.get('entry_spot_price', ''),
            t.get('entry_futures_price', ''),
            t.get('exit_spot_price', ''),
            t.get('exit_futures_price', ''),
            t.get('spot_pnl', ''),
            t.get('futures_pnl', ''),
            t.get('gross_pnl', ''),
            t.get('swap_cost', ''),
            t.get('commission', ''),
            t.get('spread_cost', ''),
            t.get('net_pnl', ''),
            t.get('return_pct', ''),
            t.get('lot_size', ''),
            t.get('status', '')
        ])

    # Create response
    output.seek(0)
    from flask import Response
    return Response(
        output.getvalue(),
        mimetype='text/csv',
        headers={'Content-Disposition': f'attachment; filename=trade_journal_{datetime.now().strftime("%Y%m%d_%H%M%S")}.csv'}
    )


@app.route('/restart')
def restart():
    """Restart and go back to setup"""
    monitor.stop_background_updates()
    monitor.is_initialized = False
    mt5.shutdown()
    return redirect(url_for('setup'))


@app.route('/clear_trades')
def clear_trades():
    """Clear all trades from the journal (redirect version)"""
    try:
        conn = monitor.db.get_connection()
        cursor = conn.cursor()
        cursor.execute('DELETE FROM trades')
        conn.commit()
        conn.close()
        monitor.positions = {}  # Clear open positions too
        logger.info("Trade journal cleared")
        return redirect(url_for('index'))
    except Exception as e:
        logger.error(f"Error clearing trades: {e}")
        return f"Error: {e}", 500


@app.route('/api/clear_trades', methods=['POST'])
def api_clear_trades():
    """Clear all trades from the journal (API version)"""
    try:
        conn = monitor.db.get_connection()
        cursor = conn.cursor()
        cursor.execute('DELETE FROM trades')
        conn.commit()
        conn.close()
        monitor.positions = {}  # Clear open positions too
        logger.info("Trade journal cleared via API")
        return jsonify({'status': 'success', 'message': 'Trade journal cleared'})
    except Exception as e:
        logger.error(f"Error clearing trades: {e}")
        return jsonify({'status': 'error', 'message': str(e)}), 500


@app.route('/api/close_position', methods=['POST'])
def api_close_position():
    """Manually close an open position"""
    try:
        asset_key = request.json.get('asset_key', 'ACTIVE')

        if asset_key not in monitor.positions:
            return jsonify({
                'status': 'error',
                'message': f'No open position found for {asset_key}'
            }), 400

        # Get current market data for the close
        market_data = monitor.get_market_data(asset_key)
        if not market_data:
            return jsonify({
                'status': 'error',
                'message': 'Could not get current market data'
            }), 400

        # Close the position with MANUAL reason
        position = monitor.positions[asset_key]
        logger.info(f"Manual close requested for {asset_key} position: {position['direction']}")

        monitor._close_position(asset_key, 'MANUAL', market_data)

        return jsonify({
            'status': 'success',
            'message': f'Position {asset_key} closed manually'
        })

    except Exception as e:
        logger.error(f"Error closing position: {e}")
        return jsonify({'status': 'error', 'message': str(e)}), 500


@app.route('/api/search_symbols', methods=['GET'])
def api_search_symbols():
    """Search for available symbols in MT5

    Query params:
        q: Search query (e.g., 'BTC', 'EUR', 'XAU')
        type: Filter by type - 'forex', 'crypto', 'futures', 'all' (default: 'all')
    """
    try:
        if not mt5.terminal_info():
            return jsonify({'status': 'error', 'message': 'MT5 not connected'}), 400

        query = request.args.get('q', '').upper()
        symbol_type = request.args.get('type', 'all').lower()

        # Get all symbols
        all_symbols = mt5.symbols_get()
        if not all_symbols:
            return jsonify({'status': 'error', 'message': 'No symbols found'}), 400

        results = []
        for sym in all_symbols:
            name = sym.name.upper()

            # Filter by query
            if query and query not in name:
                continue

            # Determine symbol category
            is_forex = sym.path and 'forex' in sym.path.lower()
            is_crypto = any(c in name for c in ['BTC', 'ETH', 'LTC', 'XRP', 'CRYPTO'])
            is_futures = 'FUT' in name or '.F' in name or '_F' in name or 'FUTURE' in name.upper()
            is_metal = any(m in name for m in ['XAU', 'XAG', 'GOLD', 'SILVER', 'PLAT', 'PALL'])
            is_oil = any(o in name for o in ['WTI', 'BRENT', 'OIL', 'CL', 'UKOIL', 'USOIL'])

            # Filter by type
            if symbol_type == 'forex' and not is_forex:
                continue
            if symbol_type == 'crypto' and not is_crypto:
                continue
            if symbol_type == 'futures' and not is_futures:
                continue
            if symbol_type == 'metals' and not is_metal:
                continue
            if symbol_type == 'oil' and not is_oil:
                continue

            # Get current price if available
            tick = mt5.symbol_info_tick(sym.name)
            bid = tick.bid if tick else 0
            ask = tick.ask if tick else 0
            spread = (ask - bid) if tick else 0

            results.append({
                'name': sym.name,
                'description': sym.description if hasattr(sym, 'description') else '',
                'path': sym.path if hasattr(sym, 'path') else '',
                'bid': bid,
                'ask': ask,
                'spread': round(spread, 5),
                'contract_size': sym.trade_contract_size,
                'min_lot': sym.volume_min,
                'is_futures': is_futures,
                'is_crypto': is_crypto,
                'is_forex': is_forex,
                'is_metal': is_metal,
                'is_oil': is_oil
            })

        # Sort by name
        results.sort(key=lambda x: x['name'])

        return jsonify({
            'status': 'success',
            'query': query,
            'type_filter': symbol_type,
            'count': len(results),
            'symbols': results[:100]  # Limit to 100 results
        })

    except Exception as e:
        logger.error(f"Error searching symbols: {e}")
        return jsonify({'status': 'error', 'message': str(e)}), 500


@app.route('/api/test_orders', methods=['POST'])
def api_test_orders():
    """Test order placement for both Spot and Futures symbols

    Opens and immediately closes a minimum lot size position on each symbol
    to verify that order execution is working correctly.
    """
    results = {
        'spot': {'symbol': '', 'open': None, 'close': None},
        'futures': {'symbol': '', 'open': None, 'close': None},
        'summary': {'success': False, 'message': ''}
    }

    try:
        # Get configured symbols
        spot_symbol = monitor.config.get('spot_symbol', '')
        futures_symbol = monitor.config.get('futures_symbol', '')

        if not spot_symbol or not futures_symbol:
            return jsonify({
                'status': 'error',
                'message': 'Spot and Futures symbols must be configured in Setup page first',
                'results': results
            }), 400

        results['spot']['symbol'] = spot_symbol
        results['futures']['symbol'] = futures_symbol

        # Check MT5 connection
        if not mt5.terminal_info():
            return jsonify({
                'status': 'error',
                'message': 'MT5 not connected. Please restart the portal.',
                'results': results
            }), 500

        all_success = True
        errors = []

        # ========== TEST SPOT SYMBOL ==========
        logger.info(f"Testing SPOT symbol: {spot_symbol}")

        # Get minimum lot size for spot
        spot_info = mt5.symbol_info(spot_symbol)
        if spot_info is None:
            results['spot']['open'] = {'success': False, 'error': f'Symbol {spot_symbol} not found in MT5'}
            all_success = False
            errors.append(f"Spot: {spot_symbol} not found")
        else:
            spot_min_lot = spot_info.volume_min
            spot_filling = spot_info.filling_mode

            # Test BUY order on spot
            logger.info(f"Opening test BUY on {spot_symbol} with {spot_min_lot} lots (filling_mode={spot_filling})")
            spot_open = monitor._execute_mt5_order(
                spot_symbol,
                mt5.ORDER_TYPE_BUY,
                spot_min_lot,
                comment="TEST_ORDER"
            )
            results['spot']['open'] = {
                'success': spot_open['success'],
                'volume': spot_min_lot,
                'filling_mode': spot_filling,
                'filling_mode_desc': _describe_filling_mode(spot_filling),
                'price': spot_open.get('price'),
                'ticket': spot_open.get('ticket'),
                'error': spot_open.get('error')
            }

            if spot_open['success']:
                # Immediately close the position
                import time
                time.sleep(0.5)  # Small delay to ensure position is registered

                spot_close = monitor._close_mt5_position(
                    spot_open['ticket'],
                    spot_symbol,
                    spot_min_lot,
                    mt5.ORDER_TYPE_BUY
                )
                results['spot']['close'] = {
                    'success': spot_close['success'],
                    'price': spot_close.get('price'),
                    'error': spot_close.get('error')
                }

                if not spot_close['success']:
                    all_success = False
                    errors.append(f"Spot close failed: {spot_close.get('error')}")
            else:
                all_success = False
                errors.append(f"Spot open failed: {spot_open.get('error')}")

        # ========== TEST FUTURES SYMBOL ==========
        logger.info(f"Testing FUTURES symbol: {futures_symbol}")

        # Get minimum lot size for futures
        futures_info = mt5.symbol_info(futures_symbol)
        if futures_info is None:
            results['futures']['open'] = {'success': False, 'error': f'Symbol {futures_symbol} not found in MT5'}
            all_success = False
            errors.append(f"Futures: {futures_symbol} not found")
        else:
            futures_min_lot = futures_info.volume_min
            futures_filling = futures_info.filling_mode

            # Test SELL order on futures (opposite of spot to simulate spread)
            logger.info(f"Opening test SELL on {futures_symbol} with {futures_min_lot} lots (filling_mode={futures_filling})")
            futures_open = monitor._execute_mt5_order(
                futures_symbol,
                mt5.ORDER_TYPE_SELL,
                futures_min_lot,
                comment="TEST_ORDER"
            )
            results['futures']['open'] = {
                'success': futures_open['success'],
                'volume': futures_min_lot,
                'filling_mode': futures_filling,
                'filling_mode_desc': _describe_filling_mode(futures_filling),
                'price': futures_open.get('price'),
                'ticket': futures_open.get('ticket'),
                'error': futures_open.get('error')
            }

            if futures_open['success']:
                # Immediately close the position
                import time
                time.sleep(0.5)

                futures_close = monitor._close_mt5_position(
                    futures_open['ticket'],
                    futures_symbol,
                    futures_min_lot,
                    mt5.ORDER_TYPE_SELL
                )
                results['futures']['close'] = {
                    'success': futures_close['success'],
                    'price': futures_close.get('price'),
                    'error': futures_close.get('error')
                }

                if not futures_close['success']:
                    all_success = False
                    errors.append(f"Futures close failed: {futures_close.get('error')}")
            else:
                all_success = False
                errors.append(f"Futures open failed: {futures_open.get('error')}")

        # Summary
        if all_success:
            results['summary'] = {
                'success': True,
                'message': 'All order tests passed! Both Spot and Futures orders opened and closed successfully.'
            }
            logger.info("Order test completed successfully")
        else:
            results['summary'] = {
                'success': False,
                'message': 'Some tests failed: ' + '; '.join(errors)
            }
            logger.warning(f"Order test had failures: {errors}")

        return jsonify({
            'status': 'success' if all_success else 'partial',
            'results': results
        })

    except Exception as e:
        import traceback
        logger.error(f"Order test error: {e}\n{traceback.format_exc()}")
        results['summary'] = {'success': False, 'message': str(e)}
        return jsonify({
            'status': 'error',
            'message': str(e),
            'results': results
        }), 500


def _describe_filling_mode(mode):
    """Convert filling mode bitmask to human readable description"""
    if mode == 0:
        return "Not specified (broker default)"
    parts = []
    if mode & 1:
        parts.append("FOK")
    if mode & 2:
        parts.append("IOC")
    if mode & 4:
        parts.append("RETURN")
    return " | ".join(parts) if parts else f"Unknown ({mode})"


@app.route('/api/estimate_costs', methods=['GET'])
def api_estimate_costs():
    """Estimate round-trip trading costs based on current bid-ask spreads

    Returns cost breakdown to help set appropriate min_profit_per_lot
    """
    try:
        # Get configured symbols and settings
        spot_symbol = monitor.config.get('spot_symbol', '')
        futures_symbol = monitor.config.get('futures_symbol', '')
        contract_size = monitor.config.get('contract_size', 100)
        lot_size = monitor.config.get('lot_size', 1)
        asset_name = monitor.config.get('asset_name', 'Asset')

        if not spot_symbol or not futures_symbol:
            return jsonify({
                'status': 'error',
                'message': 'Spot and Futures symbols must be configured first'
            }), 400

        # Check MT5 connection
        if not mt5.terminal_info():
            return jsonify({
                'status': 'error',
                'message': 'MT5 not connected'
            }), 500

        results = {
            'asset_name': asset_name,
            'contract_size': contract_size,
            'lot_size': lot_size,
            'spot': {},
            'futures': {},
            'totals': {}
        }

        # Get spot bid-ask spread
        spot_tick = mt5.symbol_info_tick(spot_symbol)
        if spot_tick:
            spot_spread = spot_tick.ask - spot_tick.bid
            spot_cost_per_lot = spot_spread * contract_size
            results['spot'] = {
                'symbol': spot_symbol,
                'bid': spot_tick.bid,
                'ask': spot_tick.ask,
                'spread': spot_spread,
                'spread_display': f"${spot_spread:.4f}" if spot_spread >= 0.01 else f"{spot_spread*100:.2f}¢",
                'cost_per_lot': spot_cost_per_lot,
                'cost_per_lot_display': f"${spot_cost_per_lot:.2f}"
            }
        else:
            results['spot'] = {'error': f'Cannot get tick for {spot_symbol}'}

        # Get futures bid-ask spread
        futures_tick = mt5.symbol_info_tick(futures_symbol)
        if futures_tick:
            futures_spread = futures_tick.ask - futures_tick.bid
            futures_cost_per_lot = futures_spread * contract_size
            results['futures'] = {
                'symbol': futures_symbol,
                'bid': futures_tick.bid,
                'ask': futures_tick.ask,
                'spread': futures_spread,
                'spread_display': f"${futures_spread:.4f}" if futures_spread >= 0.01 else f"{futures_spread*100:.2f}¢",
                'cost_per_lot': futures_cost_per_lot,
                'cost_per_lot_display': f"${futures_cost_per_lot:.2f}"
            }
        else:
            results['futures'] = {'error': f'Cannot get tick for {futures_symbol}'}

        # Calculate totals
        if 'cost_per_lot' in results['spot'] and 'cost_per_lot' in results['futures']:
            entry_cost = results['spot']['cost_per_lot'] + results['futures']['cost_per_lot']
            round_trip_cost = entry_cost * 2  # Entry + Exit

            # Add commission if configured
            commission_per_lot = monitor.config.get('commission_per_lot', 0)
            total_commission = commission_per_lot * 2  # Both legs, entry + exit

            total_cost_per_lot = round_trip_cost + total_commission

            # Suggested minimums
            suggested_min = total_cost_per_lot * 1.5  # 50% profit margin
            conservative_min = total_cost_per_lot * 2  # 100% profit margin

            results['totals'] = {
                'entry_cost_per_lot': entry_cost,
                'entry_cost_display': f"${entry_cost:.2f}",
                'round_trip_spread_cost': round_trip_cost,
                'round_trip_spread_display': f"${round_trip_cost:.2f}",
                'commission_per_lot': total_commission,
                'commission_display': f"${total_commission:.2f}",
                'total_cost_per_lot': total_cost_per_lot,
                'total_cost_display': f"${total_cost_per_lot:.2f}",
                'suggested_min_profit': suggested_min,
                'suggested_min_display': f"${suggested_min:.0f}",
                'conservative_min_profit': conservative_min,
                'conservative_min_display': f"${conservative_min:.0f}",
                'current_min_profit': monitor.config.get('min_profit_per_lot', 50)
            }

            # Check if current setting covers costs
            current_min = monitor.config.get('min_profit_per_lot', 50)
            if current_min < total_cost_per_lot:
                results['totals']['warning'] = f"Current min profit (${current_min}) is LESS than costs (${total_cost_per_lot:.2f})!"
            elif current_min < suggested_min:
                results['totals']['warning'] = f"Current min profit (${current_min}) covers costs but leaves thin margin"

        return jsonify({
            'status': 'success',
            'results': results
        })

    except Exception as e:
        import traceback
        logger.error(f"Cost estimation error: {e}\n{traceback.format_exc()}")
        return jsonify({
            'status': 'error',
            'message': str(e)
        }), 500


@app.route('/api/calculate_max_loss', methods=['GET'])
def api_calculate_max_loss():
    """Calculate what max loss per lot means in terms of spread movement"""
    try:
        # Get config values
        max_loss_per_lot = monitor.config.get('max_loss_per_lot', 100)
        lot_size = monitor.config.get('lot_size', 0.1)
        contract_size = monitor.config.get('contract_size', 100)
        asset_name = monitor.config.get('asset_name', 'Asset')

        # Calculate total max loss for configured lot size
        total_max_loss = max_loss_per_lot * lot_size

        # Calculate spread move that would trigger max loss
        # Loss = spread_move × lot_size × contract_size
        # spread_move = Loss / (lot_size × contract_size)
        if lot_size > 0 and contract_size > 0:
            spread_move_to_trigger = max_loss_per_lot / contract_size
        else:
            spread_move_to_trigger = 0

        # Get current spread statistics for context
        stats = monitor.get_statistics('ACTIVE')
        current_std = stats.get('std', 0) if stats else 0

        # Calculate how many standard deviations the max loss represents
        std_equivalent = spread_move_to_trigger / current_std if current_std > 0 else 0

        # Get cost estimate for suggestions
        spot_symbol = monitor.config.get('spot_symbol', '')
        futures_symbol = monitor.config.get('futures_symbol', '')
        round_trip_cost = 0

        if spot_symbol and futures_symbol and mt5.terminal_info():
            spot_tick = mt5.symbol_info_tick(spot_symbol)
            futures_tick = mt5.symbol_info_tick(futures_symbol)
            if spot_tick and futures_tick:
                spot_spread = spot_tick.ask - spot_tick.bid
                futures_spread = futures_tick.ask - futures_tick.bid
                entry_cost = (spot_spread + futures_spread) * contract_size
                round_trip_cost = entry_cost * 2

        # Suggestions based on costs
        suggested_2x = round_trip_cost * 2 if round_trip_cost > 0 else 100
        suggested_3x = round_trip_cost * 3 if round_trip_cost > 0 else 150

        results = {
            'asset_name': asset_name,
            'contract_size': contract_size,
            'lot_size': lot_size,
            'max_loss_per_lot': max_loss_per_lot,
            'total_max_loss': total_max_loss,
            'total_max_loss_display': f"${total_max_loss:.2f}",
            'spread_move_to_trigger': spread_move_to_trigger,
            'spread_move_display': f"${spread_move_to_trigger:.4f}" if spread_move_to_trigger >= 0.01 else f"{spread_move_to_trigger*100:.2f}¢",
            'current_std': current_std,
            'current_std_display': f"${current_std:.4f}" if current_std >= 0.01 else f"{current_std*100:.2f}¢",
            'std_equivalent': std_equivalent,
            'std_equivalent_display': f"{std_equivalent:.1f}σ",
            'round_trip_cost': round_trip_cost,
            'round_trip_cost_display': f"${round_trip_cost:.2f}",
            'suggested_2x': suggested_2x,
            'suggested_2x_display': f"${suggested_2x:.0f}",
            'suggested_3x': suggested_3x,
            'suggested_3x_display': f"${suggested_3x:.0f}",
            'is_disabled': max_loss_per_lot == 0
        }

        # Warnings
        if max_loss_per_lot > 0:
            if round_trip_cost > 0 and max_loss_per_lot < round_trip_cost:
                results['warning'] = f"Max loss (${max_loss_per_lot}) is less than round-trip cost (${round_trip_cost:.2f})! You'll always lose money."
            elif std_equivalent > 0 and std_equivalent < 1.5:
                results['warning'] = f"Max loss triggers at only {std_equivalent:.1f}σ - might exit too early on normal volatility."

        return jsonify({
            'status': 'success',
            'results': results
        })

    except Exception as e:
        import traceback
        logger.error(f"Max loss calculation error: {e}\n{traceback.format_exc()}")
        return jsonify({
            'status': 'error',
            'message': str(e)
        }), 500


# =============================================================================
# SD TOUCH STATISTICS API
# =============================================================================
@app.route('/api/sd_touches')
def api_sd_touches():
    """Get SD touch statistics and recent touches"""
    try:
        days = request.args.get('days', 7, type=int)
        view = request.args.get('view', 'summary')  # 'summary', 'daily', 'recent'

        if view == 'summary':
            data = monitor.sd_tracker.get_summary_by_sd_level(days=days)
        elif view == 'daily':
            data = monitor.sd_tracker.get_daily_statistics(days=days)
        elif view == 'recent':
            limit = request.args.get('limit', 50, type=int)
            data = monitor.sd_tracker.get_recent_touches(limit=limit)
        else:
            data = monitor.sd_tracker.get_summary_by_sd_level(days=days)

        # Get current cost for context
        contract_size = monitor.config.get('contract_size', 100)
        spot_symbol = monitor.config.get('spot_symbol', '')
        futures_symbol = monitor.config.get('futures_symbol', '')
        commission_per_lot = monitor.config.get('commission_per_lot', 0)
        round_trip_cost = commission_per_lot * 2  # Commission on entry + exit (even if no MT5 data)

        if spot_symbol and futures_symbol and mt5.terminal_info():
            spot_tick = mt5.symbol_info_tick(spot_symbol)
            futures_tick = mt5.symbol_info_tick(futures_symbol)
            if spot_tick and futures_tick:
                spot_spread = spot_tick.ask - spot_tick.bid
                futures_spread = futures_tick.ask - futures_tick.bid
                entry_cost = (spot_spread + futures_spread) * contract_size
                # Round-trip = spread cost × 2 + commission × 2
                round_trip_cost = (entry_cost * 2) + (commission_per_lot * 2)

        return jsonify({
            'status': 'success',
            'view': view,
            'days': days,
            'round_trip_cost': round_trip_cost,
            'paused': monitor.sd_tracker.paused,
            'data': data
        })

    except Exception as e:
        import traceback
        logger.error(f"SD touches API error: {e}\n{traceback.format_exc()}")
        return jsonify({
            'status': 'error',
            'message': str(e)
        }), 500


@app.route('/api/sd_touches/pause', methods=['POST'])
def api_sd_touches_pause():
    """Toggle pause state for SD touch tracking"""
    try:
        monitor.sd_tracker.paused = not monitor.sd_tracker.paused
        return jsonify({
            'status': 'success',
            'paused': monitor.sd_tracker.paused
        })
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)}), 500


@app.route('/api/sd_touches/reset', methods=['POST'])
def api_sd_touches_reset():
    """Delete all SD touch records"""
    try:
        conn = monitor.db.get_connection()
        cursor = conn.cursor()
        cursor.execute('DELETE FROM sd_touch_log')
        conn.commit()
        conn.close()
        # Clear active touches in memory
        monitor.sd_tracker.active_touches = {}
        monitor.sd_tracker.cooldown = {}
        return jsonify({'status': 'success', 'message': 'All SD touch records deleted'})
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)}), 500


@app.route('/api/sd_touches/delete', methods=['POST'])
def api_sd_touches_delete():
    """Delete specific SD touch records by ID"""
    try:
        data = request.get_json()
        ids = data.get('ids', [])
        if not ids:
            return jsonify({'status': 'error', 'message': 'No IDs provided'}), 400

        conn = monitor.db.get_connection()
        cursor = conn.cursor()
        placeholders = ','.join('?' * len(ids))
        cursor.execute(f'DELETE FROM sd_touch_log WHERE id IN ({placeholders})', ids)
        deleted = cursor.rowcount
        conn.commit()
        conn.close()
        return jsonify({'status': 'success', 'deleted': deleted})
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)}), 500


@app.route('/api/sd_touches/delete_by_level', methods=['POST'])
def api_sd_touches_delete_by_level():
    """Delete SD touch records by SD level and direction"""
    try:
        data = request.get_json()
        levels = data.get('levels', [])
        if not levels:
            return jsonify({'status': 'error', 'message': 'No levels provided'}), 400

        conn = monitor.db.get_connection()
        cursor = conn.cursor()
        total_deleted = 0
        for level in levels:
            cursor.execute(
                'DELETE FROM sd_touch_log WHERE sd_level = ? AND direction = ?',
                (level['sd_level'], level['direction'])
            )
            total_deleted += cursor.rowcount
        conn.commit()
        conn.close()
        return jsonify({'status': 'success', 'deleted': total_deleted})
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)}), 500


@app.route('/sd_analysis')
def sd_analysis_page():
    """SD Touch Analysis page"""
    return render_template_string(SD_ANALYSIS_TEMPLATE)


SD_ANALYSIS_TEMPLATE = '''
<!DOCTYPE html>
<html>
<head>
    <title>SD Touch Analysis</title>
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <style>
        * { box-sizing: border-box; margin: 0; padding: 0; }
        body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; background: #f5f5f5; padding: 20px; }
        .container { max-width: 1400px; margin: 0 auto; }
        h1 { margin-bottom: 20px; color: #333; }
        .card { background: white; border-radius: 8px; padding: 20px; margin-bottom: 20px; box-shadow: 0 2px 4px rgba(0,0,0,0.1); }
        .card h2 { margin-bottom: 15px; color: #444; font-size: 1.2em; }
        table { width: 100%; border-collapse: collapse; }
        th, td { padding: 10px; text-align: left; border-bottom: 1px solid #eee; }
        th { background: #f8f9fa; font-weight: 600; }
        .success { color: #28a745; }
        .danger { color: #dc3545; }
        .warning { color: #ffc107; }
        .profit { color: #28a745; font-weight: bold; }
        .loss { color: #dc3545; font-weight: bold; }
        .badge { display: inline-block; padding: 3px 8px; border-radius: 4px; font-size: 0.85em; }
        .badge-short { background: #ffe0e0; color: #c00; }
        .badge-long { background: #e0ffe0; color: #080; }
        .controls { display: flex; gap: 10px; margin-bottom: 20px; align-items: center; flex-wrap: wrap; }
        select, button { padding: 8px 16px; border-radius: 4px; border: 1px solid #ddd; }
        button { background: #007bff; color: white; border: none; cursor: pointer; }
        button:hover { background: #0056b3; }
        .btn-danger { background: #dc3545; }
        .btn-danger:hover { background: #c82333; }
        .btn-warning { background: #ffc107; color: #333; }
        .btn-warning:hover { background: #e0a800; }
        .btn-success { background: #28a745; }
        .btn-success:hover { background: #218838; }
        .info-box { background: #e7f3ff; border: 1px solid #b8daff; border-radius: 4px; padding: 15px; margin-bottom: 20px; }
        .back-link { display: inline-block; margin-bottom: 20px; color: #007bff; text-decoration: none; }
        .back-link:hover { text-decoration: underline; }
        .highlight { background: #fffde7; }
        .tabs { display: flex; gap: 5px; margin-bottom: 20px; }
        .tab { padding: 10px 20px; background: #e9ecef; border: none; cursor: pointer; border-radius: 4px 4px 0 0; }
        .tab.active { background: white; border-bottom: 2px solid #007bff; }
        .status-box { padding: 10px 15px; border-radius: 4px; margin-bottom: 15px; }
        .status-running { background: #d4edda; border: 1px solid #c3e6cb; color: #155724; }
        .status-paused { background: #fff3cd; border: 1px solid #ffeeba; color: #856404; }
        .action-bar { display: flex; gap: 10px; margin-bottom: 15px; align-items: center; }
        .checkbox-col { width: 30px; text-align: center; }
        input[type="checkbox"] { width: 18px; height: 18px; cursor: pointer; }
    </style>
</head>
<body>
    <div class="container">
        <a href="/" class="back-link">← Back to Monitor</a>
        <h1>📊 SD Touch Analysis</h1>

        <div class="info-box">
            <strong>What this shows:</strong> Tracks when the spread touches various standard deviation levels (2σ, 2.5σ, 3σ, 3.5σ, 4σ)
            and whether it subsequently returns to the mean. Helps identify which entry levels have the best success rates.
            <br><br>
            <strong>Round-trip Cost:</strong> <span id="roundTripCost">Loading...</span>
        </div>

        <div id="trackingStatus" class="status-box status-running">
            <strong>Status:</strong> <span id="statusText">Tracking Active</span>
        </div>

        <div class="controls">
            <label>Time Period:</label>
            <select id="daysSelect">
                <option value="1">Today</option>
                <option value="7" selected>Last 7 days</option>
                <option value="14">Last 14 days</option>
                <option value="30">Last 30 days</option>
            </select>
            <button onclick="loadData()">Refresh</button>
            <button id="pauseBtn" class="btn-warning" onclick="togglePause()">⏸ Pause</button>
            <button class="btn-danger" onclick="resetAllTouches()">🗑 Reset All</button>
        </div>

        <div class="tabs">
            <button class="tab active" onclick="switchTab('summary')">Summary by SD Level</button>
            <button class="tab" onclick="switchTab('daily')">Daily Breakdown</button>
            <button class="tab" onclick="switchTab('recent')">Recent Touches</button>
        </div>

        <div class="card" id="summaryCard">
            <h2>Summary by SD Level</h2>
            <div class="action-bar">
                <button class="btn-danger" onclick="deleteSelectedSummary()" id="deleteSummaryBtn" style="display:none;">Delete Selected SD Levels</button>
            </div>
            <table id="summaryTable">
                <thead>
                    <tr>
                        <th class="checkbox-col"><input type="checkbox" id="selectAllSummary" onclick="toggleSelectAllSummary()"></th>
                        <th>SD Level</th>
                        <th>Direction</th>
                        <th>Total Touches</th>
                        <th>Reached Mean</th>
                        <th>Success Rate</th>
                        <th>Avg Profit</th>
                        <th>Total Profit</th>
                        <th>Avg Max Adverse</th>
                        <th>Profitable?</th>
                    </tr>
                </thead>
                <tbody></tbody>
            </table>
        </div>

        <div class="card" id="dailyCard" style="display:none;">
            <h2>Daily Breakdown</h2>
            <table id="dailyTable">
                <thead>
                    <tr>
                        <th>Date</th>
                        <th>SD Level</th>
                        <th>Direction</th>
                        <th>Touches</th>
                        <th>Reached Mean</th>
                        <th>Success Rate</th>
                        <th>Avg Profit</th>
                    </tr>
                </thead>
                <tbody></tbody>
            </table>
        </div>

        <div class="card" id="recentCard" style="display:none;">
            <h2>Recent Touches</h2>
            <div class="action-bar">
                <button class="btn-danger" onclick="deleteSelectedRecent()" id="deleteRecentBtn" style="display:none;">Delete Selected</button>
                <span id="selectedCount" style="color: #666;"></span>
            </div>
            <table id="recentTable">
                <thead>
                    <tr>
                        <th class="checkbox-col"><input type="checkbox" id="selectAllRecent" onclick="toggleSelectAllRecent()"></th>
                        <th>Date</th>
                        <th>Time</th>
                        <th>SD</th>
                        <th>Dir</th>
                        <th>Entry Price</th>
                        <th>Exit Price</th>
                        <th>Status</th>
                        <th>Gross Profit</th>
                        <th>Entry Spot</th>
                        <th>Entry Fut</th>
                        <th>Exit Spot</th>
                        <th>Exit Fut</th>
                        <th>Round-Trip Cost</th>
                        <th>Net Profit</th>
                    </tr>
                </thead>
                <tbody></tbody>
            </table>
        </div>
    </div>

    <script>
        let currentView = 'summary';
        let roundTripCost = 0;
        let isPaused = false;

        function switchTab(view) {
            currentView = view;
            document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
            event.target.classList.add('active');
            document.getElementById('summaryCard').style.display = view === 'summary' ? 'block' : 'none';
            document.getElementById('dailyCard').style.display = view === 'daily' ? 'block' : 'none';
            document.getElementById('recentCard').style.display = view === 'recent' ? 'block' : 'none';
            loadData();
        }

        function loadData() {
            const days = document.getElementById('daysSelect').value;
            fetch(`/api/sd_touches?view=${currentView}&days=${days}&limit=100`)
                .then(r => r.json())
                .then(data => {
                    if (data.status === 'success') {
                        roundTripCost = data.round_trip_cost;
                        isPaused = data.paused || false;
                        updatePauseStatus();
                        document.getElementById('roundTripCost').innerHTML =
                            `<strong>$${roundTripCost.toFixed(2)}/lot</strong> (Break-even profit needed)`;

                        if (currentView === 'summary') renderSummary(data.data);
                        else if (currentView === 'daily') renderDaily(data.data);
                        else if (currentView === 'recent') renderRecent(data.data);
                    }
                });
        }

        function updatePauseStatus() {
            const statusBox = document.getElementById('trackingStatus');
            const statusText = document.getElementById('statusText');
            const pauseBtn = document.getElementById('pauseBtn');

            if (isPaused) {
                statusBox.className = 'status-box status-paused';
                statusText.textContent = 'Tracking Paused';
                pauseBtn.textContent = '▶ Resume';
                pauseBtn.className = 'btn-success';
            } else {
                statusBox.className = 'status-box status-running';
                statusText.textContent = 'Tracking Active';
                pauseBtn.textContent = '⏸ Pause';
                pauseBtn.className = 'btn-warning';
            }
        }

        function togglePause() {
            fetch('/api/sd_touches/pause', { method: 'POST' })
                .then(r => r.json())
                .then(data => {
                    if (data.status === 'success') {
                        isPaused = data.paused;
                        updatePauseStatus();
                    }
                });
        }

        function resetAllTouches() {
            if (!confirm('Are you sure you want to delete ALL SD touch records? This cannot be undone.')) return;

            fetch('/api/sd_touches/reset', { method: 'POST' })
                .then(r => r.json())
                .then(data => {
                    if (data.status === 'success') {
                        alert('All SD touch records have been deleted.');
                        loadData();
                    }
                });
        }

        function toggleSelectAllSummary() {
            const checked = document.getElementById('selectAllSummary').checked;
            document.querySelectorAll('.summary-checkbox').forEach(cb => cb.checked = checked);
            updateSummaryDeleteBtn();
        }

        function toggleSelectAllRecent() {
            const checked = document.getElementById('selectAllRecent').checked;
            document.querySelectorAll('.recent-checkbox').forEach(cb => cb.checked = checked);
            updateRecentDeleteBtn();
        }

        function updateSummaryDeleteBtn() {
            const checked = document.querySelectorAll('.summary-checkbox:checked').length;
            document.getElementById('deleteSummaryBtn').style.display = checked > 0 ? 'inline-block' : 'none';
        }

        function updateRecentDeleteBtn() {
            const checked = document.querySelectorAll('.recent-checkbox:checked').length;
            document.getElementById('deleteRecentBtn').style.display = checked > 0 ? 'inline-block' : 'none';
            document.getElementById('selectedCount').textContent = checked > 0 ? `${checked} selected` : '';
        }

        function deleteSelectedSummary() {
            const selected = [];
            document.querySelectorAll('.summary-checkbox:checked').forEach(cb => {
                selected.push({ sd_level: cb.dataset.sdLevel, direction: cb.dataset.direction });
            });

            if (selected.length === 0) return;
            if (!confirm(`Delete all touches for ${selected.length} SD level(s)?`)) return;

            fetch('/api/sd_touches/delete_by_level', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ levels: selected })
            })
            .then(r => r.json())
            .then(data => {
                if (data.status === 'success') {
                    loadData();
                }
            });
        }

        function deleteSelectedRecent() {
            const ids = [];
            document.querySelectorAll('.recent-checkbox:checked').forEach(cb => {
                ids.push(parseInt(cb.dataset.id));
            });

            if (ids.length === 0) return;
            if (!confirm(`Delete ${ids.length} selected touch record(s)?`)) return;

            fetch('/api/sd_touches/delete', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ ids: ids })
            })
            .then(r => r.json())
            .then(data => {
                if (data.status === 'success') {
                    loadData();
                }
            });
        }

        function renderSummary(data) {
            const tbody = document.querySelector('#summaryTable tbody');
            document.getElementById('selectAllSummary').checked = false;
            document.getElementById('deleteSummaryBtn').style.display = 'none';

            tbody.innerHTML = data.map(row => {
                const profitable = row.avg_profit > roundTripCost;
                const profitClass = profitable ? 'profit' : 'loss';
                const verdict = profitable ? '✅ YES' : '❌ NO';
                const netProfit = row.avg_profit - roundTripCost;
                return `
                    <tr class="${profitable ? 'highlight' : ''}">
                        <td class="checkbox-col">
                            <input type="checkbox" class="summary-checkbox"
                                data-sd-level="${row.sd_level}"
                                data-direction="${row.direction}"
                                onchange="updateSummaryDeleteBtn()">
                        </td>
                        <td><strong>${row.sd_level}σ</strong></td>
                        <td><span class="badge badge-${row.direction.toLowerCase()}">${row.direction}</span></td>
                        <td>${row.total_touches}</td>
                        <td>${row.reached_mean}</td>
                        <td>${row.success_rate}%</td>
                        <td>$${row.avg_profit.toFixed(2)}</td>
                        <td>$${row.total_profit.toFixed(2)}</td>
                        <td>$${row.avg_max_adverse.toFixed(2)}</td>
                        <td class="${profitClass}">${verdict}<br><small>Net: $${netProfit.toFixed(2)}</small></td>
                    </tr>
                `;
            }).join('');
        }

        function renderDaily(data) {
            const tbody = document.querySelector('#dailyTable tbody');
            tbody.innerHTML = data.map(row => `
                <tr>
                    <td>${row.date}</td>
                    <td><strong>${row.sd_level}σ</strong></td>
                    <td><span class="badge badge-${row.direction.toLowerCase()}">${row.direction}</span></td>
                    <td>${row.total_touches}</td>
                    <td>${row.reached_mean}</td>
                    <td>${row.success_rate}%</td>
                    <td>$${row.avg_profit.toFixed(2)}</td>
                </tr>
            `).join('');
        }

        function renderRecent(data) {
            const tbody = document.querySelector('#recentTable tbody');
            document.getElementById('selectAllRecent').checked = false;
            document.getElementById('deleteRecentBtn').style.display = 'none';
            document.getElementById('selectedCount').textContent = '';

            tbody.innerHTML = data.map(row => {
                const statusClass = row.reached_mean ? 'success' : 'warning';
                const grossProfit = row.profit !== null ? row.profit : 0;
                const rtCost = row.round_trip_cost || 0;
                const netProfit = grossProfit - rtCost;
                const netClass = netProfit > 0 ? 'profit' : (netProfit < 0 ? 'loss' : '');

                // Format bid-ask spreads in cents for readability
                const entrySpot = (row.entry_spot_spread * 100).toFixed(1);
                const entryFut = (row.entry_futures_spread * 100).toFixed(1);
                const exitSpot = (row.exit_spot_spread * 100).toFixed(1);
                const exitFut = (row.exit_futures_spread * 100).toFixed(1);

                return `
                    <tr>
                        <td class="checkbox-col">
                            <input type="checkbox" class="recent-checkbox" data-id="${row.id}" onchange="updateRecentDeleteBtn()">
                        </td>
                        <td>${row.date}</td>
                        <td>${row.time}</td>
                        <td><strong>${row.sd_level}σ</strong></td>
                        <td><span class="badge badge-${row.direction.toLowerCase()}">${row.direction}</span></td>
                        <td>${row.touch_spread ? row.touch_spread.toFixed(4) : '-'}</td>
                        <td>${row.spread_at_mean ? row.spread_at_mean.toFixed(4) : '-'}</td>
                        <td class="${statusClass}">${row.status}</td>
                        <td>${row.profit !== null ? '$' + row.profit.toFixed(2) : '-'}</td>
                        <td>${entrySpot}¢</td>
                        <td>${entryFut}¢</td>
                        <td>${row.reached_mean ? exitSpot + '¢' : '-'}</td>
                        <td>${row.reached_mean ? exitFut + '¢' : '-'}</td>
                        <td>$${rtCost.toFixed(2)}</td>
                        <td class="${netClass}">${row.reached_mean ? '$' + netProfit.toFixed(2) : '-'}</td>
                    </tr>
                `;
            }).join('');
        }

        // Initial load
        loadData();
        // Auto-refresh every 30 seconds
        setInterval(loadData, 30000);
    </script>
</body>
</html>
'''


# =============================================================================
# HTML TEMPLATES
# =============================================================================
TEMPLATE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'templates')
os.makedirs(TEMPLATE_DIR, exist_ok=True)

# Setup page template - Clean Black & White style
SETUP_HTML = '''<!DOCTYPE html>
<html>
<head>
    <title>Trading Portal - Setup</title>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }
        body {
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
            background: #fff;
            color: #333;
            min-height: 100vh;
            padding: 40px 20px;
        }
        .container {
            max-width: 600px;
            margin: 0 auto;
        }
        h1 {
            font-size: 1.8em;
            margin-bottom: 5px;
            font-weight: 600;
        }
        .subtitle {
            color: #666;
            margin-bottom: 30px;
        }
        .info-box {
            background: #f8f9fa;
            border-left: 4px solid #333;
            padding: 15px 20px;
            margin-bottom: 30px;
        }
        .info-box p {
            margin-bottom: 8px;
            color: #555;
        }
        .info-box p:last-child { margin-bottom: 0; }
        .section {
            margin-bottom: 30px;
            padding-bottom: 20px;
            border-bottom: 1px solid #eee;
        }
        .section-title {
            font-size: 1.1em;
            font-weight: 600;
            margin-bottom: 15px;
            padding-bottom: 10px;
            border-bottom: 2px solid #333;
        }
        .form-group {
            margin-bottom: 20px;
        }
        label {
            display: block;
            margin-bottom: 5px;
            font-weight: 500;
        }
        .help-text {
            color: #888;
            font-size: 0.85em;
            margin-top: 5px;
        }
        input {
            width: 100%;
            padding: 12px;
            border: 1px solid #ddd;
            border-radius: 4px;
            font-size: 1em;
        }
        input:focus {
            outline: none;
            border-color: #333;
        }
        .btn {
            width: 100%;
            padding: 15px;
            background: #333;
            color: #fff;
            border: none;
            border-radius: 4px;
            font-size: 1em;
            cursor: pointer;
            font-weight: 500;
        }
        .btn:hover {
            background: #555;
        }
        .error {
            background: #fee;
            border: 1px solid #fcc;
            color: #c00;
            padding: 12px;
            margin-bottom: 20px;
            border-radius: 4px;
        }
    </style>
</head>
<body>
    <div class="container">
        <h1>Algorithmic Trading Portal</h1>
        <div class="subtitle">Spot-Futures Basis Trading</div>

        <div class="info-box">
            <p>• Make sure MetaTrader5 is running and logged in</p>
            <p>• Enter your broker's exact symbol names (check Market Watch in MT5)</p>
            <p>• Leave symbol fields empty to auto-detect common symbols</p>
            <p>• <strong>Enter swap charges manually</strong> - check MT5 symbol specification for accurate values</p>
        </div>

        {% if error %}
        <div class="error">{{ error }}</div>
        {% endif %}

        <form method="POST">
            <div class="section">
                <div class="section-title">ASSET CONFIGURATION (Single Asset Mode)</div>
                <div class="form-group">
                    <label>Asset Name</label>
                    <input type="text" name="asset_name" value="{{ config.asset_name or config.gold_asset_name or 'GOLD' }}" placeholder="e.g., GOLD, SILVER, COFFEE, CRUDE OIL">
                    <div class="help-text">Display name for this asset (e.g., GOLD, SILVER, COFFEE, CRUDE OIL)</div>
                </div>
                <div class="form-group">
                    <label>Spot Symbol</label>
                    <input type="text" name="spot_symbol" value="{{ config.spot_symbol or config.gold_spot_symbol or '' }}" placeholder="e.g., XAUUSD, XAGUSD, KC">
                    <div class="help-text">Your broker's spot/cash symbol from MT5 Market Watch</div>
                </div>
                <div class="form-group">
                    <label>Futures Symbol</label>
                    <input type="text" name="futures_symbol" value="{{ config.futures_symbol or config.gold_futures_symbol or '' }}" placeholder="e.g., GC0226, SI0326, KC0325">
                    <div class="help-text">Your broker's futures symbol (required for basis trading)</div>
                </div>
                <div class="form-group">
                    <label>Futures Expiry Date</label>
                    <input type="date" name="futures_expiry" value="{{ config.futures_expiry or config.gold_futures_expiry or '' }}">
                    <div class="help-text">Futures contract expiry date</div>
                </div>
                <div class="form-group">
                    <label>Contract Size (units per lot)</label>
                    <input type="number" name="contract_size" step="0.01" min="0.01" value="{{ config.contract_size or config.gold_contract_size or 100 }}" placeholder="e.g., 100">
                    <div class="help-text">Units per lot: Gold=100oz, Silver=5000oz, Coffee=37500lbs</div>
                </div>
                <div class="form-group">
                    <label>Daily Swap Charge (USD per lot)</label>
                    <input type="number" name="swap_charge" step="0.01" min="0" value="{{ config.swap_charge or config.gold_swap_charge or 0 }}" placeholder="e.g., 45.67">
                    <div class="help-text">Check MT5: Right-click symbol → Specification → Swap Long</div>
                </div>
            </div>

            <button type="submit" class="btn">START MONITORING</button>
        </form>
    </div>
</body>
</html>'''

# Main monitor page template - Clean Black & White style
MONITOR_HTML = '''<!DOCTYPE html>
<html>
<head>
    <title>Trading Portal - Monitor</title>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }
        body {
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
            background: #fff;
            color: #333;
            min-height: 100vh;
            padding: 20px;
        }
        .header {
            display: flex;
            justify-content: space-between;
            align-items: center;
            margin-bottom: 20px;
            padding-bottom: 15px;
            border-bottom: 2px solid #333;
        }
        h1 { font-size: 1.5em; font-weight: 600; }
        .timestamp { color: #666; }

        .controls {
            display: flex;
            gap: 20px;
            margin-bottom: 20px;
            padding: 15px;
            background: #f8f9fa;
            border-radius: 8px;
            flex-wrap: wrap;
            align-items: center;
        }
        .control-group {
            display: flex;
            align-items: center;
            gap: 10px;
        }
        .control-label { color: #333; font-weight: 500; }
        .toggle {
            position: relative;
            width: 50px;
            height: 26px;
        }
        .toggle input { opacity: 0; width: 0; height: 0; }
        .toggle-slider {
            position: absolute;
            cursor: pointer;
            top: 0; left: 0; right: 0; bottom: 0;
            background: #ccc;
            border-radius: 26px;
            transition: 0.3s;
        }
        .toggle-slider:before {
            position: absolute;
            content: "";
            height: 20px;
            width: 20px;
            left: 3px;
            bottom: 3px;
            background: #fff;
            border-radius: 50%;
            transition: 0.3s;
        }
        .toggle input:checked + .toggle-slider { background: #333; }
        .toggle input:checked + .toggle-slider:before { transform: translateX(24px); }
        .toggle.danger input:checked + .toggle-slider { background: #c00; }

        .status-badge {
            padding: 4px 12px;
            border-radius: 4px;
            font-size: 0.8em;
            font-weight: 500;
        }
        .status-badge.active { background: #333; color: #fff; }
        .status-badge.inactive { background: #eee; color: #666; }
        .status-badge.paper { background: #f0f0f0; color: #666; border: 1px solid #ccc; }
        .status-badge.live { background: #c00; color: #fff; }
        .status-badge.cheap { background: #e8f5e9; color: #2e7d32; }
        .status-badge.fair { background: #fff3e0; color: #f57c00; }
        .status-badge.expensive { background: #ffebee; color: #c62828; }

        .summary {
            display: flex;
            gap: 15px;
            margin-bottom: 25px;
        }
        .summary-item {
            padding: 20px 30px;
            border: 2px solid;
            border-radius: 8px;
            text-align: center;
        }
        .summary-item.cheap { border-color: #4caf50; }
        .summary-item.fair { border-color: #ff9800; }
        .summary-item.expensive { border-color: #f44336; }
        .summary-count { font-size: 2.5em; font-weight: 700; }
        .summary-item.cheap .summary-count { color: #2e7d32; }
        .summary-item.fair .summary-count { color: #f57c00; }
        .summary-item.expensive .summary-count { color: #c62828; }
        .summary-label { font-size: 0.85em; color: #666; margin-top: 5px; }

        .assets { display: grid; grid-template-columns: repeat(auto-fit, minmax(450px, 1fr)); gap: 20px; }

        .asset-card {
            border: 1px solid #ddd;
            border-radius: 8px;
            padding: 20px;
            background: #fff;
        }
        .asset-header {
            display: flex;
            justify-content: space-between;
            align-items: center;
            margin-bottom: 15px;
            padding-bottom: 10px;
            border-bottom: 2px solid #333;
        }
        .asset-name { font-size: 1.3em; font-weight: 700; }

        .price-grid {
            display: grid;
            grid-template-columns: repeat(4, 1fr);
            gap: 10px;
            margin-bottom: 15px;
        }
        .price-item {
            background: #f8f9fa;
            padding: 12px;
            border-radius: 4px;
        }
        .price-label { color: #666; font-size: 0.85em; text-transform: uppercase; }
        .price-value { font-size: 18px; font-weight: 600; margin-top: 4px; }

        .basis-section {
            background: #f8f9fa;
            padding: 15px;
            border-radius: 4px;
            margin-bottom: 15px;
        }
        .basis-row {
            display: flex;
            justify-content: space-between;
            padding: 6px 0;
            border-bottom: 1px solid #eee;
        }
        .basis-row:last-child { border-bottom: none; }
        .basis-label { color: #666; font-size: 16px; }
        .basis-value { font-weight: 600; font-size: 18px; }
        .basis-value.positive { color: #c62828; }
        .basis-value.negative { color: #2e7d32; }

        .signal-section {
            border: 2px solid #ddd;
            border-radius: 8px;
            padding: 20px;
            text-align: center;
        }
        .signal-section.sell-basis { border-color: #c62828; background: #ffebee; }
        .signal-section.buy-basis { border-color: #2e7d32; background: #e8f5e9; }
        .signal-section.hold { border-color: #ddd; background: #fafafa; }
        .signal-section.no-data { border-color: #eee; background: #f5f5f5; }
        .signal-section.collecting { border-color: #1976d2; background: #e3f2fd; }
        .signal-section.time-stop { border-color: #ff9800; background: #fff3e0; }
        .signal-section.overnight-close { border-color: #795548; background: #efebe9; }
        .signal-section.stop-loss { border-color: #9c27b0; background: #f3e5f5; }
        .signal-section.regime-filter { border-color: #9e9e9e; background: #f5f5f5; }

        .zscore-display {
            font-size: 2.5em;
            font-weight: 700;
            margin: 10px 0;
        }
        .signal-section.sell-basis .zscore-display { color: #c62828; }
        .signal-section.buy-basis .zscore-display { color: #2e7d32; }
        .signal-section.time-stop .zscore-display { color: #ff9800; }
        .signal-section.overnight-close .zscore-display { color: #795548; }
        .signal-section.stop-loss .zscore-display { color: #9c27b0; }
        .signal-section.regime-filter .zscore-display { color: #616161; }
        .signal-type { font-size: 1.1em; font-weight: 600; margin-bottom: 5px; }
        .signal-reason { color: #666; font-size: 0.9em; }

        /* Hurst exponent colors */
        .hurst-good { color: #2e7d32; }  /* Green - mean reverting, good for strategy */
        .hurst-bad { color: #c62828; }   /* Red - trending, bad for mean reversion */

        .stats-row {
            display: flex;
            justify-content: center;
            gap: 30px;
            margin-top: 12px;
            font-size: 0.85em;
            color: #888;
        }

        .account-section {
            background: #f8f9fa;
            border: 1px solid #ddd;
            border-radius: 8px;
            padding: 15px 20px;
            margin-bottom: 20px;
        }
        .account-header {
            display: flex;
            justify-content: space-between;
            align-items: center;
            margin-bottom: 15px;
            padding-bottom: 10px;
            border-bottom: 2px solid #333;
        }
        .account-title { font-weight: 600; font-size: 1.1em; }
        .account-id { color: #666; font-size: 0.9em; }
        .account-grid {
            display: grid;
            grid-template-columns: repeat(6, 1fr);
            gap: 15px;
        }
        .account-item { text-align: center; }
        .account-label { color: #666; font-size: 0.85em; margin-bottom: 4px; }
        .account-value { font-size: 18px; font-weight: 600; }
        .account-value.positive { color: #2e7d32; }
        .account-value.negative { color: #c62828; }

        .positions-section {
            margin-top: 25px;
            border: 1px solid #ddd;
            border-radius: 8px;
            padding: 20px;
        }
        .positions-title {
            font-weight: 600;
            margin-bottom: 15px;
            padding-bottom: 10px;
            border-bottom: 2px solid #333;
        }
        .position-card {
            background: #f8f9fa;
            padding: 15px;
            margin-bottom: 10px;
            border-radius: 4px;
            display: grid;
            grid-template-columns: 2fr 1fr 1fr 1fr 1fr 1fr;
            gap: 15px;
            align-items: center;
            font-size: 16px;
        }
        .position-card .pos-symbol { font-weight: 600; font-size: 18px; }
        .position-card .pos-type { padding: 4px 8px; border-radius: 4px; font-weight: 500; }
        .position-card .pos-type.buy { background: #e8f5e9; color: #2e7d32; }
        .position-card .pos-type.sell { background: #ffebee; color: #c62828; }
        .position-card .pos-pnl { font-weight: 600; font-size: 18px; }
        .position-card .pos-pnl.positive { color: #2e7d32; }
        .position-card .pos-pnl.negative { color: #c62828; }
        .position-card .pos-return { font-size: 14px; }
        .position-header {
            display: grid;
            grid-template-columns: 2fr 1fr 1fr 1fr 1fr 1fr;
            gap: 15px;
            padding: 10px 15px;
            font-weight: 600;
            color: #666;
            font-size: 0.85em;
            border-bottom: 1px solid #ddd;
            margin-bottom: 10px;
        }

        /* Trade History Table */
        .trade-history-section {
            margin-top: 25px;
            border: 1px solid #ddd;
            border-radius: 8px;
            padding: 20px;
        }
        .trade-history-header {
            display: flex;
            justify-content: space-between;
            align-items: center;
            margin-bottom: 15px;
            padding-bottom: 10px;
            border-bottom: 2px solid #333;
        }
        .trade-history-title { font-weight: 600; font-size: 1.1em; }
        .trade-summary { display: flex; gap: 30px; }
        .summary-stat { font-size: 0.9em; color: #666; }
        .summary-stat strong { color: #333; }
        .summary-stat strong.positive { color: #2e7d32; }
        .summary-stat strong.negative { color: #c62828; }
        .summary-stat strong.warning { color: #f57c00; }
        .trade-history-table-wrapper { overflow-x: auto; max-height: 400px; overflow-y: auto; }
        .download-btn { padding: 4px 10px; background: #1976d2; color: white; border: none; border-radius: 4px; cursor: pointer; font-size: 0.85em; margin-left: 10px; }
        .download-btn:hover { background: #1565c0; }
        .trade-history-table {
            width: 100%;
            border-collapse: collapse;
            font-size: 14px;
        }
        .trade-history-table th {
            background: #f8f9fa;
            padding: 12px 8px;
            text-align: left;
            font-weight: 600;
            color: #666;
            border-bottom: 2px solid #ddd;
            white-space: nowrap;
        }
        .trade-history-table td {
            padding: 12px 8px;
            border-bottom: 1px solid #eee;
            white-space: nowrap;
        }
        .trade-history-table tr:hover { background: #f8f9fa; }
        .trade-history-table .direction-long { color: #2e7d32; }
        .trade-history-table .direction-short { color: #c62828; }
        .trade-history-table .pnl-positive { color: #2e7d32; font-weight: 600; }
        .trade-history-table .pnl-negative { color: #c62828; font-weight: 600; }
        .trade-history-table .status-open { color: #1976d2; }
        .trade-history-table .status-closed { color: #666; }
        .direction-icon { margin-right: 5px; }

        .footer {
            margin-top: 25px;
            padding-top: 15px;
            border-top: 1px solid #ddd;
            display: flex;
            justify-content: space-between;
            align-items: center;
            color: #666;
        }
        .footer a {
            color: #c00;
            text-decoration: none;
            padding: 10px 20px;
            border: 1px solid #c00;
            border-radius: 4px;
        }
        .footer a:hover { background: #fff0f0; }

        .settings-link {
            color: #333;
            text-decoration: none;
            padding: 10px 20px;
            border: 1px solid #333;
            border-radius: 4px;
        }
        .settings-link:hover { background: #f5f5f5; }

        .reset-btn {
            color: #c62828;
            background: white;
            padding: 10px 20px;
            border: 1px solid #c62828;
            border-radius: 4px;
            cursor: pointer;
            font-size: 14px;
        }
        .reset-btn:hover { background: #ffebee; }

        @media (max-width: 600px) {
            .price-grid { grid-template-columns: repeat(2, 1fr); }
            .controls { flex-direction: column; align-items: flex-start; }
            .summary { flex-direction: column; }
        }

        /* Z-Score Chart Styles */
        .chart-section {
            background: white;
            margin: 20px;
            padding: 20px;
            border-radius: 8px;
            box-shadow: 0 2px 4px rgba(0,0,0,0.1);
        }
        .chart-header {
            display: flex;
            justify-content: space-between;
            align-items: center;
            margin-bottom: 15px;
        }
        .chart-title {
            font-size: 18px;
            font-weight: 600;
            color: #333;
        }
        .chart-tabs {
            display: flex;
            gap: 10px;
        }
        .chart-tab {
            padding: 8px 16px;
            border: 1px solid #ddd;
            border-radius: 4px;
            background: #f5f5f5;
            cursor: pointer;
            font-size: 14px;
        }
        .chart-tab.active {
            background: #333;
            color: white;
            border-color: #333;
        }
        .chart-container {
            position: relative;
            height: 300px;
        }
    </style>
    <script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
</head>
<body>
    <div class="header">
        <h1>Algorithmic Trading Portal</h1>
        <div class="timestamp" id="timestamp">Loading...</div>
    </div>

    <div class="controls">
        <div class="control-group">
            <span class="control-label">Algo Trading:</span>
            <label class="toggle danger">
                <input type="checkbox" id="algo-toggle" onchange="toggleAlgo(this.checked)">
                <span class="toggle-slider"></span>
            </label>
            <span class="status-badge inactive" id="algo-status">OFF</span>
        </div>
        <div class="control-group">
            <span class="control-label">Mode:</span>
            <label class="toggle">
                <input type="checkbox" id="paper-toggle" checked onchange="togglePaper(this.checked)">
                <span class="toggle-slider"></span>
            </label>
            <span class="status-badge paper" id="mode-status">PAPER</span>
        </div>
        <div class="control-group">
            <span class="control-label">Thresholds:</span>
            <span style="color: #666;" id="thresholds">Entry: ±2.0σ | Exit: ±0.5σ | Stop: ±3.0σ</span>
        </div>
        <div class="control-group">
            <span class="control-label">Session:</span>
            <span style="color: #1976d2; font-weight: 500;" id="market-session">Loading...</span>
        </div>
        <a href="/settings" class="settings-link">⚙ Settings</a>
        <a href="/sd_analysis" class="settings-link">📊 SD Analysis</a>
        <button class="reset-btn" onclick="resetStatistics()">↺ Reset Stats</button>
        <button class="reset-btn" onclick="clearTrades()">🗑 Clear Trades</button>
    </div>

    <div class="account-section" id="account-section">
        <div class="account-header">
            <span class="account-title">MT5 Account</span>
            <span class="account-id" id="account-id">---</span>
        </div>
        <div class="account-grid">
            <div class="account-item">
                <div class="account-label">Balance</div>
                <div class="account-value" id="account-balance">$0.00</div>
            </div>
            <div class="account-item">
                <div class="account-label">Equity</div>
                <div class="account-value" id="account-equity">$0.00</div>
            </div>
            <div class="account-item">
                <div class="account-label">Margin</div>
                <div class="account-value" id="account-margin">$0.00</div>
            </div>
            <div class="account-item">
                <div class="account-label">Free Margin</div>
                <div class="account-value" id="account-free-margin">$0.00</div>
            </div>
            <div class="account-item">
                <div class="account-label">Open P&L</div>
                <div class="account-value" id="account-profit">$0.00</div>
            </div>
            <div class="account-item">
                <div class="account-label">Leverage</div>
                <div class="account-value" id="account-leverage">1:100</div>
            </div>
        </div>
    </div>

    <div class="chart-section">
        <div class="chart-header">
            <div class="chart-title">Z-Score Chart</div>
        </div>
        <div class="chart-container">
            <canvas id="zscore-chart"></canvas>
        </div>
    </div>

    <div class="chart-section">
        <div class="chart-header">
            <div class="chart-title" id="price-chart-title">Price Chart</div>
        </div>
        <div class="chart-container">
            <canvas id="price-chart"></canvas>
        </div>
    </div>

    <div class="assets" id="assets-container">
        <div style="color: #666; text-align: center; padding: 50px;">Loading market data...</div>
    </div>

    <div class="positions-section">
        <div class="positions-title">MT5 Open Positions</div>
        <div id="mt5-positions-container">
            <div style="color: #666; text-align: center;">No open positions in MT5</div>
        </div>
    </div>

    <div class="positions-section" style="margin-top: 15px;">
        <div class="positions-title">Portal Algo Positions</div>
        <div id="positions-container">
            <div style="color: #666; text-align: center;">No algo positions</div>
        </div>
    </div>

    <div class="trade-history-section">
        <div class="trade-history-header">
            <span class="trade-history-title">Trade Journal</span>
            <div class="trade-summary" id="trade-summary">
                <span class="summary-stat">Total P&L: <strong id="total-pnl">$0.00</strong></span>
                <span class="summary-stat">Return: <strong id="cumulative-return">0.00%</strong></span>
                <span class="summary-stat">Drawdown: <strong id="current-drawdown">0.00%</strong></span>
                <span class="summary-stat">Sharpe: <strong id="sharpe-ratio">0.00</strong></span>
                <span class="summary-stat">Win Rate: <strong id="win-rate">0%</strong></span>
                <span class="summary-stat">Trades: <strong id="total-trades">0</strong></span>
                <button class="download-btn" onclick="downloadTradesCSV()" title="Download as CSV">⬇ CSV</button>
            </div>
        </div>
        <div class="trade-history-table-wrapper">
            <table class="trade-history-table" id="trade-history-table">
                <thead>
                    <tr>
                        <th>#</th>
                        <th>Direction</th>
                        <th>Lots</th>
                        <th>Entry</th>
                        <th>Exit</th>
                        <th>Days</th>
                        <th>Entry Z</th>
                        <th>Exit Z</th>
                        <th>Spot P&L</th>
                        <th>Futures P&L</th>
                        <th>Gross P&L</th>
                        <th>Swap</th>
                        <th>Comm</th>
                        <th>Spread</th>
                        <th>Net P&L</th>
                        <th>Return</th>
                    </tr>
                </thead>
                <tbody id="trade-history-body">
                    <tr><td colspan="16" style="text-align: center; color: #666;">No trades yet</td></tr>
                </tbody>
            </table>
        </div>
    </div>

    <div class="footer">
        <div>Last update: <span id="last-update">-</span></div>
        <a href="/restart">↻ Restart</a>
    </div>

    <script>
        // Get asset filter from URL parameter (?asset=1 or ?asset=2)
        const urlParams = new URLSearchParams(window.location.search);
        const assetFilter = urlParams.get('asset'); // '1', '2', or null (show all)

        function updateData() {
            fetch('/api/data')
                .then(res => res.json())
                .then(data => {
                    if (data.error) return;

                    document.getElementById('timestamp').textContent = new Date().toLocaleTimeString();
                    document.getElementById('last-update').textContent = data.last_update;

                    const cfg = data.config;
                    document.getElementById('algo-toggle').checked = cfg.algo_enabled;
                    document.getElementById('paper-toggle').checked = cfg.paper_mode;
                    document.getElementById('algo-status').textContent = cfg.algo_enabled ? 'ON' : 'OFF';
                    document.getElementById('algo-status').className = 'status-badge ' + (cfg.algo_enabled ? 'active' : 'inactive');
                    document.getElementById('mode-status').textContent = cfg.paper_mode ? 'PAPER' : 'LIVE';
                    document.getElementById('mode-status').className = 'status-badge ' + (cfg.paper_mode ? 'paper' : 'live');
                    let thresholdText = `Entry: ±${cfg.entry_std_dev}σ | Exit: ±${cfg.exit_std_dev}σ | Stop: ±${cfg.stop_loss_std_dev}σ | Hurst: ${cfg.hurst_threshold}`;
                    if (cfg.trending_duration_minutes > 0) {
                        thresholdText += ` (${cfg.trending_duration_minutes}min)`;
                    }
                    if (cfg.time_stop_loss_days > 0) {
                        thresholdText += ` | Time Stop: ${cfg.time_stop_loss_days}d`;
                    }
                    document.getElementById('thresholds').textContent = thresholdText;

                    // Update market session
                    if (data.market_session) {
                        document.getElementById('market-session').textContent = data.market_session;
                    }

                    // Filter data by asset if URL parameter is set
                    const assetKeys = Object.keys(data.data);
                    let filteredData = data.data;
                    let filteredKeys = assetKeys;
                    let selectedAssetKey = null;

                    if (assetFilter === '1' && assetKeys.length > 0) {
                        selectedAssetKey = assetKeys[0];
                        filteredData = { [selectedAssetKey]: data.data[selectedAssetKey] };
                        filteredKeys = [selectedAssetKey];
                    } else if (assetFilter === '2' && assetKeys.length > 1) {
                        selectedAssetKey = assetKeys[1];
                        filteredData = { [selectedAssetKey]: data.data[selectedAssetKey] };
                        filteredKeys = [selectedAssetKey];
                    }

                    // Update chart titles with asset name (single asset mode)
                    const assetName = filteredData[filteredKeys[0]]?.asset_name || 'Asset';
                    document.querySelector('.chart-title').textContent = `${assetName} Z-Score`;
                    document.getElementById('price-chart-title').textContent = `${assetName} Price`;
                    // Set charts to this asset
                    if (filteredKeys.length > 0) {
                        currentChartAsset = filteredKeys[0];
                        currentPriceChartAsset = filteredKeys[0];
                    }

                    const container = document.getElementById('assets-container');
                    container.innerHTML = '';
                    for (const [key, asset] of Object.entries(filteredData)) {
                        container.appendChild(createAssetCard(asset));
                    }

                    // Update account info
                    updateAccountInfo(data.account);

                    // Show ALL MT5 positions (matching MT5's Trade tab exactly)
                    // Don't filter - user needs to see everything in their account
                    updateMT5Positions(data.mt5_positions);

                    // Update algo positions - filter by asset if single-asset mode
                    let filteredPositions = data.positions;
                    if (assetFilter && selectedAssetKey && data.positions) {
                        filteredPositions = data.positions.filter(p => p.asset === selectedAssetKey);
                    }
                    updatePositions(filteredPositions);

                    // Update trade history - filter by asset if single-asset mode
                    let filteredHistory = data.trade_history;
                    if (assetFilter && selectedAssetKey && data.trade_history) {
                        filteredHistory = data.trade_history.filter(t => t.asset === selectedAssetKey);
                    }
                    updateTradeHistory(filteredHistory, data.trade_summary);

                    // Update Z-Score chart - filter by asset if single-asset mode
                    let filteredZscoreHistory = data.zscore_history;
                    if (assetFilter && selectedAssetKey && data.zscore_history) {
                        filteredZscoreHistory = { [selectedAssetKey]: data.zscore_history[selectedAssetKey] };
                    }
                    updateZscoreChart(filteredZscoreHistory, filteredHistory);

                    // Update Price chart - filter by asset if single-asset mode
                    let filteredPriceHistory = data.price_history;
                    if (assetFilter && selectedAssetKey && data.price_history) {
                        filteredPriceHistory = { [selectedAssetKey]: data.price_history[selectedAssetKey] };
                    }
                    updatePriceChart(filteredPriceHistory);
                })
                .catch(err => console.error('Error:', err));
        }

        function updateAccountInfo(account) {
            if (!account) {
                document.getElementById('account-id').textContent = 'Not Connected';
                return;
            }

            document.getElementById('account-id').textContent = `${account.login} @ ${account.server}`;
            document.getElementById('account-balance').textContent = '$' + account.balance.toLocaleString(undefined, {minimumFractionDigits: 2, maximumFractionDigits: 2});
            document.getElementById('account-equity').textContent = '$' + account.equity.toLocaleString(undefined, {minimumFractionDigits: 2, maximumFractionDigits: 2});
            document.getElementById('account-margin').textContent = '$' + account.margin.toLocaleString(undefined, {minimumFractionDigits: 2, maximumFractionDigits: 2});
            document.getElementById('account-free-margin').textContent = '$' + account.free_margin.toLocaleString(undefined, {minimumFractionDigits: 2, maximumFractionDigits: 2});

            const profitEl = document.getElementById('account-profit');
            profitEl.textContent = (account.profit >= 0 ? '+$' : '-$') + Math.abs(account.profit).toLocaleString(undefined, {minimumFractionDigits: 2, maximumFractionDigits: 2});
            profitEl.className = 'account-value ' + (account.profit >= 0 ? 'positive' : 'negative');

            document.getElementById('account-leverage').textContent = '1:' + account.leverage;
        }

        function updateMT5Positions(positions) {
            const container = document.getElementById('mt5-positions-container');
            if (!positions || positions.length === 0) {
                container.innerHTML = '<div style="color: #666; text-align: center;">No open positions in MT5</div>';
                return;
            }

            // Calculate total P&L
            let totalPnL = 0;
            positions.forEach(pos => { totalPnL += pos.profit; });

            let html = `
                <div class="position-header" style="grid-template-columns: repeat(7, 1fr);">
                    <div>Symbol</div>
                    <div>Type</div>
                    <div>Volume</div>
                    <div>Open Price</div>
                    <div>Current</div>
                    <div>P&L</div>
                    <div>Time</div>
                </div>
            `;

            positions.forEach(pos => {
                const pnlClass = pos.profit >= 0 ? 'positive' : 'negative';
                const typeClass = pos.type.toLowerCase();
                const pnlSign = pos.profit >= 0 ? '+' : '';
                const priceDecimals = pos.price_open > 100 ? 2 : 4;
                // Extract just time from datetime string
                const timeOnly = pos.time ? pos.time.split(' ')[1] || pos.time : '--';

                html += `
                    <div class="position-card" style="grid-template-columns: repeat(7, 1fr);">
                        <div class="pos-symbol">${pos.symbol}</div>
                        <div><span class="pos-type ${typeClass}">${pos.type}</span></div>
                        <div>${pos.volume}</div>
                        <div>${pos.price_open.toFixed(priceDecimals)}</div>
                        <div>${pos.price_current.toFixed(priceDecimals)}</div>
                        <div class="pos-pnl ${pnlClass}">${pnlSign}$${pos.profit.toFixed(2)}</div>
                        <div style="font-size: 0.85em;">${timeOnly}</div>
                    </div>
                `;
            });

            // Add total P&L row
            const totalClass = totalPnL >= 0 ? 'positive' : 'negative';
            const totalSign = totalPnL >= 0 ? '+' : '';
            html += `
                <div class="position-card" style="grid-template-columns: repeat(7, 1fr); background: #f8f9fa; font-weight: bold; margin-top: 5px; border-top: 2px solid #dee2e6;">
                    <div>TOTAL</div>
                    <div></div>
                    <div></div>
                    <div></div>
                    <div></div>
                    <div class="pos-pnl ${totalClass}">${totalSign}$${totalPnL.toFixed(2)}</div>
                    <div></div>
                </div>
            `;

            container.innerHTML = html;
        }

        function createAssetCard(asset) {
            const card = document.createElement('div');
            card.className = 'asset-card';

            const zscore = asset.zscore !== null ? asset.zscore.toFixed(2) : '--';
            const signal = asset.signal || { type: 'NO_DATA', reason: '' };

            let signalClass = 'hold';
            if (signal.type === 'SELL_BASIS') signalClass = 'sell-basis';
            else if (signal.type === 'BUY_BASIS') signalClass = 'buy-basis';
            else if (signal.type === 'NO_DATA') signalClass = 'no-data';
            else if (signal.type === 'COLLECTING') signalClass = 'collecting';
            else if (signal.type === 'TIME_STOP') signalClass = 'time-stop';
            else if (signal.type === 'OVERNIGHT_CLOSE') signalClass = 'overnight-close';
            else if (signal.type === 'STOP_LOSS') signalClass = 'stop-loss';
            else if (signal.type === 'MAX_LOSS') signalClass = 'stop-loss';
            else if (signal.type === 'REGIME_FILTER') signalClass = 'regime-filter';

            // Hurst exponent display
            const hurst = asset.hurst !== null && asset.hurst !== undefined ? asset.hurst.toFixed(3) : '--';
            const hurstRegime = asset.hurst_regime || 'N/A';
            const hurstClass = asset.hurst < 0.5 ? 'hurst-good' : (asset.hurst >= 0.5 ? 'hurst-bad' : '');

            const diffClass = asset.swap_diff > 0 ? 'positive' : 'negative';

            card.innerHTML = `
                <div class="asset-header">
                    <span class="asset-name">${asset.asset_name}</span>
                    <span class="status-badge ${asset.status_class}">${asset.status}</span>
                </div>

                <div class="price-grid">
                    <div class="price-item">
                        <div class="price-label">Spot</div>
                        <div class="price-value">${asset.spot_price.toFixed(2)}</div>
                    </div>
                    <div class="price-item">
                        <div class="price-label">Futures</div>
                        <div class="price-value">${asset.futures_price.toFixed(2)}</div>
                    </div>
                    <div class="price-item">
                        <div class="price-label">Spot Spread</div>
                        <div class="price-value">${asset.spot_spread.toFixed(1)}¢</div>
                    </div>
                    <div class="price-item">
                        <div class="price-label">Fut Spread</div>
                        <div class="price-value">${asset.futures_spread.toFixed(1)}¢</div>
                    </div>
                </div>

                <div class="basis-section">
                    <div class="basis-row">
                        <span class="basis-label"><strong>Basis (F-S)</strong></span>
                        <span class="basis-value"><strong>${asset.actual_basis.toFixed(2)}</strong></span>
                    </div>
                    <div class="basis-row">
                        <span class="basis-label">Days to Expiry</span>
                        <span class="basis-value">${Math.round(asset.days_to_expiry)}</span>
                    </div>
                    <div class="basis-row" style="border-top: 1px solid #ddd; padding-top: 8px; margin-top: 4px;">
                        <span class="basis-label" style="color: #888; font-size: 0.85em;">Swap Info (Reference)</span>
                        <span class="basis-value" style="color: #888; font-size: 0.85em;">$${asset.swap_charge.toFixed(2)}/day/lot</span>
                    </div>
                    <div class="basis-row">
                        <span class="basis-label" style="color: #888; font-size: 0.85em;">Fair Value Basis</span>
                        <span class="basis-value" style="color: #888; font-size: 0.85em;">${asset.swap_basis.toFixed(2)}</span>
                    </div>
                    <div class="basis-row">
                        <span class="basis-label" style="color: #888; font-size: 0.85em;">Diff (Actual - Fair)</span>
                        <span class="basis-value" style="color: ${(asset.actual_basis - asset.swap_basis) >= 0 ? '#5cb85c' : '#d9534f'}; font-size: 0.85em;">${(asset.actual_basis - asset.swap_basis).toFixed(2)}</span>
                    </div>
                    <div class="basis-row">
                        <span class="basis-label" style="color: #888; font-size: 0.85em;">Premium %</span>
                        <span class="basis-value" style="color: ${(asset.actual_basis - asset.swap_basis) >= 0 ? '#5cb85c' : '#d9534f'}; font-size: 0.85em;">${asset.swap_basis !== 0 ? (((asset.actual_basis - asset.swap_basis) / Math.abs(asset.swap_basis)) * 100).toFixed(1) : 0}%</span>
                    </div>
                </div>

                <div class="basis-section" style="background: #f8f9fa; border: 1px solid #e9ecef;">
                    <div style="font-weight: 600; margin-bottom: 8px; color: #495057;">Margin Requirements (1:${asset.leverage})</div>
                    <div class="basis-row">
                        <span class="basis-label">Spot Leg (Buy/Sell)</span>
                        <span class="basis-value"><strong>$${asset.margin_per_lot_spot.toLocaleString(undefined, {minimumFractionDigits: 2, maximumFractionDigits: 2})}</strong>/lot</span>
                    </div>
                    <div class="basis-row">
                        <span class="basis-label">Futures Leg (Buy/Sell)</span>
                        <span class="basis-value"><strong>$${asset.margin_per_lot_futures.toLocaleString(undefined, {minimumFractionDigits: 2, maximumFractionDigits: 2})}</strong>/lot</span>
                    </div>
                    <div class="basis-row" style="border-top: 1px solid #dee2e6; padding-top: 6px; margin-top: 6px;">
                        <span class="basis-label">Total (Spread Trade)</span>
                        <span class="basis-value"><strong>$${asset.margin_per_lot_total.toLocaleString(undefined, {minimumFractionDigits: 2, maximumFractionDigits: 2})}</strong>/lot</span>
                    </div>
                    <div class="basis-row">
                        <span class="basis-label">For ${asset.user_lot_size} lots</span>
                        <span class="basis-value" style="color: #1976d2;"><strong>$${asset.margin_required.toLocaleString(undefined, {minimumFractionDigits: 2, maximumFractionDigits: 2})}</strong></span>
                    </div>
                    <div class="basis-row">
                        <span class="basis-label">If Price ±15%</span>
                        <span class="basis-value" style="color: #c62828;"><strong>$${asset.margin_with_buffer.toLocaleString(undefined, {minimumFractionDigits: 2, maximumFractionDigits: 2})}</strong></span>
                    </div>
                </div>

                <div class="signal-section ${signalClass}">
                    <div class="signal-type">${signal.type.replace('_', ' ')}</div>
                    ${signal.reason ? `<div class="signal-reason">${signal.reason}</div>` : ''}
                    <div class="zscore-display">${zscore}σ</div>
                    ${asset.stats ? `
                    <div style="display: inline-block; padding: 6px 16px; border-radius: 20px; margin: 8px 0; font-size: 16px; font-weight: 600; ${asset.hurst < 0.5 ? 'background: #e8f5e9; color: #2e7d32;' : 'background: #ffebee; color: #c62828;'}">
                        Hurst: ${hurst} &nbsp;|&nbsp; ${hurstRegime}
                    </div>
                    <div style="display: grid; grid-template-columns: repeat(3, 1fr); gap: 10px; margin: 10px 0; font-size: 16px;">
                        <div style="text-align: center; padding: 8px; background: #f5f5f5; border-radius: 6px;">
                            <div style="color: #888; font-size: 12px;">MEAN</div>
                            <div style="font-weight: 700; font-size: 18px;">${asset.stats.mean.toFixed(2)}</div>
                        </div>
                        <div style="text-align: center; padding: 8px; background: #f5f5f5; border-radius: 6px;">
                            <div style="color: #888; font-size: 12px;">STD</div>
                            <div style="font-weight: 700; font-size: 18px;">${asset.stats.std.toFixed(2)}</div>
                        </div>
                        <div style="text-align: center; padding: 8px; background: #f5f5f5; border-radius: 6px;">
                            <div style="color: #888; font-size: 12px;">SPREAD</div>
                            <div style="font-weight: 700; font-size: 18px;">${asset.actual_basis.toFixed(2)}</div>
                        </div>
                    </div>
                    <div style="display: grid; grid-template-columns: 1fr 1fr; gap: 8px; margin-top: 10px; font-size: 16px;">
                        <div style="background: #ffebee; padding: 10px; border-radius: 6px; border-left: 4px solid #d9534f;">
                            <div style="color: #c62828; font-weight: 600; margin-bottom: 4px;">Short Spread</div>
                            <div style="display: flex; justify-content: space-between;"><span style="color: #888;">Entry ↑</span><strong>${asset.stats.upper_entry.toFixed(2)}</strong></div>
                            <div style="display: flex; justify-content: space-between;"><span style="color: #888;">Exit</span><strong>${asset.stats.upper_exit.toFixed(2)}</strong></div>
                        </div>
                        <div style="background: #e8f5e9; padding: 10px; border-radius: 6px; border-left: 4px solid #5cb85c;">
                            <div style="color: #2e7d32; font-weight: 600; margin-bottom: 4px;">Long Spread</div>
                            <div style="display: flex; justify-content: space-between;"><span style="color: #888;">Entry ↓</span><strong>${asset.stats.lower_entry.toFixed(2)}</strong></div>
                            <div style="display: flex; justify-content: space-between;"><span style="color: #888;">Exit</span><strong>${asset.stats.lower_exit.toFixed(2)}</strong></div>
                        </div>
                    </div>` : ''}
                </div>
            `;

            return card;
        }

        async function closePosition(assetKey) {
            if (!confirm('Are you sure you want to close this position?')) {
                return;
            }

            try {
                const response = await fetch('/api/close_position', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ asset_key: assetKey })
                });

                const data = await response.json();

                if (data.status === 'success') {
                    alert('Position closed successfully');
                    // Refresh data immediately
                    fetchData();
                } else {
                    alert('Error closing position: ' + data.message);
                }
            } catch (error) {
                alert('Error closing position: ' + error.message);
            }
        }

        function updatePositions(positions) {
            const container = document.getElementById('positions-container');
            if (!positions || positions.length === 0) {
                container.innerHTML = '<div style="color: #666; text-align: center;">No algo positions</div>';
                return;
            }
            container.innerHTML = positions.map(p => {
                const unrealizedPnl = p.unrealized_pnl || 0;
                const pnlClass = unrealizedPnl >= 0 ? 'pnl-positive' : 'pnl-negative';
                const pnlSign = unrealizedPnl >= 0 ? '+' : '-';
                const entrySpread = p.entry_spread ? p.entry_spread.toFixed(2) : '--';
                const currentSpread = p.current_spread ? p.current_spread.toFixed(2) : '--';
                const targetExit = p.target_exit ? p.target_exit.toFixed(2) : '--';
                const stopLossExit = p.stop_loss_exit ? p.stop_loss_exit.toFixed(2) : '--';
                const spreadCost = p.spread_cost ? p.spread_cost.toFixed(2) : '0.00';
                const spotSpread = p.spot_spread ? p.spot_spread.toFixed(1) : '--';
                const futSpread = p.futures_spread ? p.futures_spread.toFixed(1) : '--';

                return `
                <div class="position-card" style="padding: 12px; border: 1px solid #ddd; border-radius: 8px; margin-bottom: 10px; background: #fafafa;">
                    <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 8px;">
                        <div class="pos-symbol" style="font-weight: bold; font-size: 1.1em;">${p.asset}</div>
                        <span class="pos-type ${p.direction === 'Long Spread' ? 'buy' : 'sell'}" style="padding: 2px 8px; border-radius: 4px;">${p.direction}</span>
                    </div>
                    <div style="display: grid; grid-template-columns: 1fr 1fr; gap: 6px; font-size: 0.9em;">
                        <div>Entry Z: <strong>${p.entry_zscore ? p.entry_zscore.toFixed(2) : '--'}σ</strong></div>
                        <div>Lots: <strong>${p.lot_size || 0.1}</strong></div>
                        <div>Entry Spread: <strong>${entrySpread}</strong></div>
                        <div>Current Spread: <strong>${currentSpread}</strong></div>
                        <div style="color: #5cb85c;">Target Exit: <strong>${targetExit}</strong></div>
                        <div style="color: #d9534f;">Stop Loss: <strong>${stopLossExit}</strong></div>
                        <div>Status: <span class="${p.order_status === 'FILLED' ? 'pnl-positive' : ''}">${p.order_status || 'PENDING'}</span></div>
                        <div>Date: ${p.entry_date || '--'}</div>
                    </div>
                    <div style="margin-top: 8px; padding-top: 8px; border-top: 1px solid #ddd; font-size: 0.85em; color: #888;">
                        <div style="display: flex; justify-content: space-between;">
                            <span>Bid-Ask Spread: Spot ${spotSpread}¢ + Fut ${futSpread}¢</span>
                            <span style="color: #c62828;">Cost: $${spreadCost}</span>
                        </div>
                    </div>
                    <div style="margin-top: 10px; padding-top: 8px; border-top: 1px solid #ddd; text-align: center;">
                        <div style="font-size: 0.8em; color: #888;">Unrealized P&L</div>
                        <div class="${pnlClass}" style="font-size: 1.3em; font-weight: bold;">${pnlSign}$${Math.abs(unrealizedPnl).toFixed(2)}</div>
                    </div>
                    <div style="margin-top: 10px; text-align: center;">
                        <button onclick="closePosition('${p.asset}')" style="background: #d9534f; color: white; border: none; padding: 8px 20px; border-radius: 4px; cursor: pointer; font-weight: 500;">
                            Close Position
                        </button>
                    </div>
                </div>
            `}).join('');
        }

        function updateTradeHistory(trades, summary) {
            // Update summary
            const totalPnl = summary.total_pnl || 0;
            const totalPnlEl = document.getElementById('total-pnl');
            totalPnlEl.textContent = (totalPnl >= 0 ? '$' : '-$') + Math.abs(totalPnl).toFixed(2);
            totalPnlEl.className = totalPnl >= 0 ? 'positive' : 'negative';

            // Cumulative return
            const cumReturn = summary.cumulative_return || 0;
            const cumReturnEl = document.getElementById('cumulative-return');
            cumReturnEl.textContent = (cumReturn >= 0 ? '+' : '') + cumReturn.toFixed(2) + '%';
            cumReturnEl.className = cumReturn >= 0 ? 'positive' : 'negative';

            // Current drawdown (always shown as negative since it's a decline)
            const drawdown = summary.current_drawdown || 0;
            const drawdownEl = document.getElementById('current-drawdown');
            drawdownEl.textContent = drawdown > 0 ? '-' + drawdown.toFixed(2) + '%' : '0.00%';
            drawdownEl.className = drawdown > 5 ? 'negative' : (drawdown > 0 ? 'warning' : '');
            // Show max drawdown in tooltip
            const maxDd = summary.max_drawdown || 0;
            drawdownEl.title = 'Max Drawdown: -' + maxDd.toFixed(2) + '%';

            // Sharpe ratio
            const sharpe = summary.sharpe_ratio || 0;
            const sharpeEl = document.getElementById('sharpe-ratio');
            sharpeEl.textContent = sharpe.toFixed(2);
            sharpeEl.className = sharpe >= 1 ? 'positive' : (sharpe >= 0 ? '' : 'negative');

            document.getElementById('win-rate').textContent = (summary.win_rate || 0).toFixed(1) + '%';
            document.getElementById('total-trades').textContent = summary.total_trades || 0;

            // Update table
            const tbody = document.getElementById('trade-history-body');
            if (!trades || trades.length === 0) {
                tbody.innerHTML = '<tr><td colspan="16" style="text-align: center; color: #666;">No trades yet</td></tr>';
                return;
            }

            let runningPnl = 0;
            tbody.innerHTML = trades.slice().reverse().map((t, i) => {
                const isLong = t.direction === 'Long Spread';
                const dirClass = isLong ? 'direction-long' : 'direction-short';
                const icon = isLong ? '📈' : '📉';

                const spotPnlClass = (t.spot_pnl || 0) >= 0 ? 'pnl-positive' : 'pnl-negative';
                const futuresPnlClass = (t.futures_pnl || 0) >= 0 ? 'pnl-positive' : 'pnl-negative';
                const grossPnlClass = (t.gross_pnl || 0) >= 0 ? 'pnl-positive' : 'pnl-negative';
                const netPnlClass = (t.net_pnl || 0) >= 0 ? 'pnl-positive' : 'pnl-negative';
                const returnClass = (t.return_pct || 0) >= 0 ? 'pnl-positive' : 'pnl-negative';

                runningPnl += t.net_pnl || 0;

                return `<tr class="${t.status === 'OPEN' ? 'status-open' : ''}">
                    <td>${i + 1}</td>
                    <td class="${dirClass}"><span class="direction-icon">${icon}</span>${t.direction}</td>
                    <td>${t.lot_size || 0.1}</td>
                    <td>${t.entry_date || '--'}</td>
                    <td>${t.exit_date || (t.status === 'OPEN' ? '<em>Open</em>' : '--')}</td>
                    <td>${t.days_held || '--'}</td>
                    <td>${t.entry_zscore ? t.entry_zscore.toFixed(2) : '--'}</td>
                    <td>${t.exit_zscore ? t.exit_zscore.toFixed(2) : '--'}</td>
                    <td class="${spotPnlClass}">$${(t.spot_pnl || 0).toFixed(2)}</td>
                    <td class="${futuresPnlClass}">$${(t.futures_pnl || 0).toFixed(2)}</td>
                    <td class="${grossPnlClass}">$${(t.gross_pnl || 0).toFixed(2)}</td>
                    <td style="color: #c62828;">$${Math.abs(t.swap_cost || 0).toFixed(2)}</td>
                    <td style="color: #c62828;">$${Math.abs(t.commission || 0).toFixed(2)}</td>
                    <td style="color: #888;">$${Math.abs(t.spread_cost || 0).toFixed(2)}</td>
                    <td class="${netPnlClass}"><strong>$${(t.net_pnl || 0).toFixed(2)}</strong></td>
                    <td class="${returnClass}">${(t.return_pct || 0).toFixed(2)}%</td>
                </tr>`;
            }).join('');
        }

        function toggleAlgo(enabled) {
            if (enabled && !confirm('Enable algorithmic trading? This will automatically execute trades based on signals.')) {
                document.getElementById('algo-toggle').checked = false;
                return;
            }
            fetch('/api/toggle_algo', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ enabled: enabled })
            });
        }

        function togglePaper(paper) {
            if (!paper && !confirm('WARNING: Switching to LIVE mode will execute REAL trades with REAL money. Are you sure?')) {
                document.getElementById('paper-toggle').checked = true;
                return;
            }
            fetch('/api/toggle_paper', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ paper: paper })
            });
        }

        function resetStatistics() {
            if (!confirm('Reset all statistics? This will clear:\\n- Spread cache (mean/std calculation)\\n- Price history\\n- Z-Score chart\\n- Price chart\\n\\nLookback period will restart from scratch.')) {
                return;
            }
            fetch('/api/reset_statistics', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' }
            })
            .then(res => res.json())
            .then(data => {
                if (data.status === 'success') {
                    alert('Statistics reset successfully. Lookback will restart.');
                    // Clear local chart data
                    zscoreData = { GOLD: [], SILVER: [] };
                    if (zscoreChart) {
                        zscoreChart.data.labels = [];
                        zscoreChart.data.datasets.forEach(ds => ds.data = []);
                        zscoreChart.update();
                    }
                    // Clear price chart data
                    priceData = { GOLD: [], SILVER: [] };
                    if (priceChart) {
                        priceChart.data.labels = [];
                        priceChart.data.datasets.forEach(ds => ds.data = []);
                        priceChart.update();
                    }
                } else {
                    alert('Error: ' + data.message);
                }
            })
            .catch(err => alert('Error resetting statistics: ' + err));
        }

        function downloadTradesCSV() {
            window.location.href = '/api/trades/csv';
        }

        function clearTrades() {
            if (!confirm('Are you sure you want to clear all trades?\\n\\nThis will delete:\\n- All trade history\\n- All open positions\\n\\nThis action cannot be undone.')) {
                return;
            }
            fetch('/api/clear_trades', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' }
            })
            .then(res => res.json())
            .then(data => {
                if (data.status === 'success') {
                    alert('Trade journal cleared successfully.');
                    updateData();  // Refresh the page data
                } else {
                    alert('Error: ' + data.message);
                }
            })
            .catch(err => alert('Error clearing trades: ' + err));
        }

        // Z-Score Chart
        let zscoreChart = null;
        let currentChartAsset = 'ACTIVE';
        let zscoreData = { ACTIVE: [] };
        let tradeMarkers = [];

        function initChart() {
            const ctx = document.getElementById('zscore-chart').getContext('2d');
            zscoreChart = new Chart(ctx, {
                type: 'line',
                data: {
                    labels: [],
                    datasets: [
                        {
                            label: 'Z-Score',
                            data: [],
                            borderColor: '#2196F3',
                            backgroundColor: 'rgba(33, 150, 243, 0.1)',
                            borderWidth: 2,
                            fill: false,
                            tension: 0.1,
                            pointRadius: 0
                        },
                        {
                            label: 'Entry Upper (+)',
                            data: [],
                            borderColor: '#f44336',
                            borderWidth: 1,
                            borderDash: [5, 5],
                            fill: false,
                            pointRadius: 0
                        },
                        {
                            label: 'Entry Lower (-)',
                            data: [],
                            borderColor: '#f44336',
                            borderWidth: 1,
                            borderDash: [5, 5],
                            fill: false,
                            pointRadius: 0
                        },
                        {
                            label: 'Exit Upper',
                            data: [],
                            borderColor: '#4CAF50',
                            borderWidth: 1,
                            borderDash: [2, 2],
                            fill: false,
                            pointRadius: 0
                        },
                        {
                            label: 'Exit Lower',
                            data: [],
                            borderColor: '#4CAF50',
                            borderWidth: 1,
                            borderDash: [2, 2],
                            fill: false,
                            pointRadius: 0
                        },
                        {
                            label: 'Zero Line',
                            data: [],
                            borderColor: '#999',
                            borderWidth: 1,
                            fill: false,
                            pointRadius: 0
                        },
                        {
                            label: 'Trade Entry',
                            data: [],
                            borderColor: '#FF9800',
                            backgroundColor: '#FF9800',
                            borderWidth: 0,
                            pointRadius: 8,
                            pointStyle: 'triangle',
                            showLine: false
                        },
                        {
                            label: 'Trade Exit',
                            data: [],
                            borderColor: '#9C27B0',
                            backgroundColor: '#9C27B0',
                            borderWidth: 0,
                            pointRadius: 8,
                            pointStyle: 'rect',
                            showLine: false
                        }
                    ]
                },
                options: {
                    responsive: true,
                    maintainAspectRatio: false,
                    interaction: {
                        intersect: false,
                        mode: 'index'
                    },
                    plugins: {
                        legend: {
                            display: true,
                            position: 'top',
                            labels: {
                                usePointStyle: true,
                                boxWidth: 8,
                                font: { size: 11 }
                            }
                        },
                        tooltip: {
                            callbacks: {
                                label: function(context) {
                                    if (context.dataset.label === 'Z-Score') {
                                        return 'Z-Score: ' + context.parsed.y.toFixed(3);
                                    }
                                    return context.dataset.label + ': ' + context.parsed.y.toFixed(2);
                                }
                            }
                        }
                    },
                    scales: {
                        x: {
                            display: true,
                            grid: { display: false },
                            ticks: { maxTicksLimit: 10 }
                        },
                        y: {
                            display: true,
                            grid: { color: '#eee' },
                            suggestedMin: -3,
                            suggestedMax: 3
                        }
                    }
                }
            });
        }

        function updateZscoreChart(zscore_history, trade_history) {
            if (!zscore_history) return;

            zscoreData = zscore_history;

            // Find trade markers for current asset
            tradeMarkers = [];
            if (trade_history) {
                trade_history.forEach(trade => {
                    if (trade.asset === currentChartAsset) {
                        // Entry marker
                        if (trade.entry_zscore) {
                            tradeMarkers.push({
                                type: 'entry',
                                time: trade.entry_date ? trade.entry_date.split(' ')[1] || trade.entry_date : '',
                                zscore: trade.entry_zscore,
                                direction: trade.direction
                            });
                        }
                        // Exit marker
                        if (trade.exit_zscore && trade.status === 'CLOSED') {
                            tradeMarkers.push({
                                type: 'exit',
                                time: trade.exit_date ? trade.exit_date.split(' ')[1] || trade.exit_date : '',
                                zscore: trade.exit_zscore,
                                direction: trade.direction
                            });
                        }
                    }
                });
            }

            updateChartDisplay();
        }

        function updateChartDisplay() {
            if (!zscoreChart || !zscoreData[currentChartAsset]) return;

            const history = zscoreData[currentChartAsset];
            if (history.length === 0) return;

            const labels = history.map(h => h.time);
            const zscores = history.map(h => h.zscore);
            const entryUpper = history.map(h => h.entry_upper);
            const entryLower = history.map(h => h.entry_lower);
            const exitUpper = history.map(h => h.exit_upper);
            const exitLower = history.map(h => h.exit_lower);
            const zeroLine = history.map(() => 0);

            // Find trade markers that match times in our history
            const entryPoints = labels.map(time => {
                const marker = tradeMarkers.find(m => m.type === 'entry' && m.time === time);
                return marker ? marker.zscore : null;
            });
            const exitPoints = labels.map(time => {
                const marker = tradeMarkers.find(m => m.type === 'exit' && m.time === time);
                return marker ? marker.zscore : null;
            });

            zscoreChart.data.labels = labels;
            zscoreChart.data.datasets[0].data = zscores;
            zscoreChart.data.datasets[1].data = entryUpper;
            zscoreChart.data.datasets[2].data = entryLower;
            zscoreChart.data.datasets[3].data = exitUpper;
            zscoreChart.data.datasets[4].data = exitLower;
            zscoreChart.data.datasets[5].data = zeroLine;
            zscoreChart.data.datasets[6].data = entryPoints;
            zscoreChart.data.datasets[7].data = exitPoints;

            zscoreChart.update('none');
        }

        // Price Chart
        let priceChart = null;
        let currentPriceChartAsset = 'ACTIVE';
        let priceData = { ACTIVE: [] };

        function initPriceChart() {
            const ctx = document.getElementById('price-chart').getContext('2d');
            priceChart = new Chart(ctx, {
                type: 'line',
                data: {
                    labels: [],
                    datasets: [
                        {
                            label: 'Spot Price',
                            data: [],
                            borderColor: '#2196F3',
                            backgroundColor: 'rgba(33, 150, 243, 0.1)',
                            borderWidth: 2,
                            fill: false,
                            tension: 0.1,
                            pointRadius: 0,
                            yAxisID: 'y'
                        },
                        {
                            label: 'Futures Price',
                            data: [],
                            borderColor: '#FF9800',
                            backgroundColor: 'rgba(255, 152, 0, 0.1)',
                            borderWidth: 2,
                            fill: false,
                            tension: 0.1,
                            pointRadius: 0,
                            yAxisID: 'y'
                        },
                        {
                            label: 'Spread (F-S)',
                            data: [],
                            borderColor: '#4CAF50',
                            backgroundColor: 'rgba(76, 175, 80, 0.1)',
                            borderWidth: 2,
                            borderDash: [5, 5],
                            fill: false,
                            tension: 0.1,
                            pointRadius: 0,
                            yAxisID: 'y1'
                        }
                    ]
                },
                options: {
                    responsive: true,
                    maintainAspectRatio: false,
                    interaction: {
                        intersect: false,
                        mode: 'index'
                    },
                    plugins: {
                        legend: {
                            display: true,
                            position: 'top',
                            labels: {
                                usePointStyle: true,
                                boxWidth: 8,
                                font: { size: 11 }
                            }
                        },
                        tooltip: {
                            callbacks: {
                                label: function(context) {
                                    if (context.dataset.label === 'Spread (F-S)') {
                                        return context.dataset.label + ': ' + context.parsed.y.toFixed(4);
                                    }
                                    return context.dataset.label + ': $' + context.parsed.y.toFixed(2);
                                }
                            }
                        }
                    },
                    scales: {
                        x: {
                            display: true,
                            grid: { display: false },
                            ticks: { maxTicksLimit: 10 }
                        },
                        y: {
                            type: 'linear',
                            display: true,
                            position: 'left',
                            grid: { color: '#eee' },
                            title: {
                                display: true,
                                text: 'Price ($)'
                            }
                        },
                        y1: {
                            type: 'linear',
                            display: true,
                            position: 'right',
                            grid: { drawOnChartArea: false },
                            title: {
                                display: true,
                                text: 'Spread'
                            }
                        }
                    }
                }
            });
        }

        function updatePriceChart(price_history) {
            if (!price_history) return;
            priceData = price_history;
            updatePriceChartDisplay();
        }

        function updatePriceChartDisplay() {
            if (!priceChart || !priceData[currentPriceChartAsset]) return;

            const history = priceData[currentPriceChartAsset];
            if (history.length === 0) return;

            const labels = history.map(h => h.time);
            const spotPrices = history.map(h => h.spot_price);
            const futuresPrices = history.map(h => h.futures_price);
            const spreads = history.map(h => h.spread);

            priceChart.data.labels = labels;
            priceChart.data.datasets[0].data = spotPrices;
            priceChart.data.datasets[1].data = futuresPrices;
            priceChart.data.datasets[2].data = spreads;

            priceChart.update('none');
        }

        // Initialize charts on page load
        initChart();
        initPriceChart();

        updateData();
        setInterval(updateData, 300);
    </script>
</body>
</html>'''

# Settings page template - Clean Black & White style
SETTINGS_HTML = '''<!DOCTYPE html>
<html>
<head>
    <title>Trading Portal - Settings</title>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }
        body {
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
            background: #fff;
            color: #333;
            min-height: 100vh;
            padding: 40px 20px;
        }
        .container { max-width: 600px; margin: 0 auto; }
        h1 {
            font-size: 1.8em;
            margin-bottom: 30px;
            font-weight: 600;
        }
        .card {
            border: 1px solid #ddd;
            border-radius: 8px;
            padding: 20px;
            margin-bottom: 20px;
        }
        .card-title {
            font-weight: 600;
            margin-bottom: 20px;
            padding-bottom: 10px;
            border-bottom: 2px solid #333;
        }
        .form-group {
            margin-bottom: 20px;
        }
        label {
            display: block;
            margin-bottom: 5px;
            font-weight: 500;
        }
        .help-text {
            color: #888;
            font-size: 0.85em;
            margin-top: 5px;
        }
        input {
            width: 100%;
            padding: 12px;
            border: 1px solid #ddd;
            border-radius: 4px;
            font-size: 1em;
        }
        input:focus {
            outline: none;
            border-color: #333;
        }
        .btn {
            width: 100%;
            padding: 15px;
            background: #333;
            color: #fff;
            border: none;
            border-radius: 4px;
            font-size: 1em;
            cursor: pointer;
            font-weight: 500;
            margin-top: 10px;
        }
        .btn:hover { background: #555; }
        .btn-secondary {
            background: #fff;
            color: #333;
            border: 1px solid #333;
        }
        .btn-secondary:hover { background: #f5f5f5; }
        .success {
            background: #e8f5e9;
            border: 1px solid #4caf50;
            color: #2e7d32;
            padding: 12px;
            margin-bottom: 20px;
            border-radius: 4px;
            text-align: center;
        }
        .error {
            background: #ffebee;
            border: 1px solid #f44336;
            color: #c62828;
            padding: 12px;
            margin-bottom: 20px;
            border-radius: 4px;
        }
        a { color: #333; text-decoration: none; }
    </style>
</head>
<body>
    <div class="container">
        <h1>⚙ Trading Settings</h1>

        {% if saved %}
        <div class="success">Settings saved successfully!</div>
        {% endif %}

        {% if error %}
        <div class="error">{{ error }}</div>
        {% endif %}

        <form method="POST">
            <div class="card">
                <div class="card-title">Signal Parameters</div>

                <div class="form-group">
                    <label>Lookback Period</label>
                    <div style="display: flex; gap: 10px;">
                        <input type="number" name="lookback_period" value="{{ config.lookback_period }}" min="1" max="10000" style="flex: 1;">
                        <select name="lookback_unit" style="width: 120px; padding: 12px; border: 1px solid #ddd; border-radius: 4px;">
                            <option value="minutes" {{ 'selected' if config.lookback_unit == 'minutes' else '' }}>Minutes</option>
                            <option value="days" {{ 'selected' if config.lookback_unit == 'days' else '' }}>Days</option>
                        </select>
                    </div>
                    <div class="help-text">1 data point = 0.3 seconds. For intraday: use minutes. For swing trades: use days.</div>
                </div>

                <div class="form-group">
                    <label>Entry Threshold (Standard Deviations)</label>
                    <input type="number" name="entry_std_dev" value="{{ config.entry_std_dev }}" min="0.5" max="5" step="0.1">
                    <div class="help-text">Z-score threshold to open position (e.g., 2.0 = ±2σ)</div>
                </div>

                <div class="form-group">
                    <label>Exit Threshold (Standard Deviations)</label>
                    <input type="number" name="exit_std_dev" value="{{ config.exit_std_dev }}" min="0" max="2" step="0.1">
                    <div class="help-text">Z-score threshold to close position (e.g., 0.5 = ±0.5σ)</div>
                </div>

                <div class="form-group">
                    <label>Stop Loss Threshold (Standard Deviations)</label>
                    <input type="number" name="stop_loss_std_dev" value="{{ config.stop_loss_std_dev }}" min="2" max="6" step="0.1">
                    <div class="help-text">Z-score threshold for stop loss (e.g., 3.0 = ±3σ)</div>
                </div>

                <div class="form-group">
                    <label>Time-Based Stop Loss (Days)</label>
                    <input type="number" name="time_stop_loss_days" value="{{ config.time_stop_loss_days or 0 }}" min="0" max="365" step="0.5">
                    <div class="help-text">Auto-close position after X days (0 = disabled). Use 0.5 for 12 hours, 1 for 1 day, etc.</div>
                </div>
            </div>

            <div class="card">
                <div class="card-title">Overnight Swap Protection</div>

                <div class="form-group">
                    <label class="toggle-label">
                        <input type="checkbox" name="close_before_overnight" {% if config.close_before_overnight %}checked{% endif %}>
                        <span>Close Positions Before Overnight</span>
                    </label>
                    <div class="help-text">Automatically close all positions before swap time to avoid overnight charges</div>
                </div>

                <div class="form-group" style="display: flex; gap: 15px; align-items: flex-end;">
                    <div style="flex: 1;">
                        <label>Close Hour (24h format)</label>
                        <input type="number" name="overnight_close_hour" value="{{ config.overnight_close_hour or 16 }}" min="0" max="23" step="1">
                    </div>
                    <div style="flex: 1;">
                        <label>Close Minute</label>
                        <input type="number" name="overnight_close_minute" value="{{ config.overnight_close_minute or 55 }}" min="0" max="59" step="1">
                    </div>
                </div>
                <div class="help-text">Default 16:55 (4:55 PM) - close before 5 PM EST swap rollover. Adjust based on your broker's swap time.</div>
            </div>

            <div class="card">
                <div class="card-title">Regime Filter (Hurst Exponent)</div>

                <div class="form-group">
                    <label class="toggle-label">
                        <input type="checkbox" name="hurst_enabled" {% if config.hurst_enabled %}checked{% endif %}>
                        <span>Enable Hurst Filter</span>
                    </label>
                    <div class="help-text">When enabled, blocks new entries during trending markets (Hurst >= threshold)</div>
                </div>

                <div class="form-group">
                    <label>Hurst Threshold: <span id="hurst_threshold_value">{{ config.hurst_threshold or 0.5 }}</span></label>
                    <input type="range" name="hurst_threshold" value="{{ config.hurst_threshold or 0.5 }}" min="0.3" max="0.9" step="0.05" oninput="document.getElementById('hurst_threshold_value').textContent = this.value">
                    <div class="help-text">Block entries when Hurst >= this value (0.5 = strict, 0.7 = relaxed). Lower = more filtering.</div>
                </div>

                <div class="form-group">
                    <label>Trending Duration (Minutes)</label>
                    <input type="number" name="trending_duration_minutes" value="{{ config.trending_duration_minutes or 15 }}" min="0" max="120" step="5">
                    <div class="help-text">Only block if trending for X minutes continuously (0 = immediate, 15 = recommended)</div>
                </div>
            </div>

            <div class="card">
                <div class="card-title">Position Sizing</div>

                <div class="form-group">
                    <label>Max Positions</label>
                    <input type="number" name="max_positions" value="{{ config.max_positions }}" min="1" max="10">
                    <div class="help-text">Maximum concurrent positions allowed (1 = one spread at a time)</div>
                </div>

                <div class="form-group">
                    <label>Lot Size</label>
                    <input type="number" name="lot_size" value="{{ config.lot_size }}" min="0.01" max="700" step="0.01">
                    <div class="help-text">Size of each trade in lots</div>
                </div>

                <div class="form-group">
                    <label>Commission per Lot ($)</label>
                    <input type="number" name="commission_per_lot" value="{{ config.commission_per_lot }}" min="0" max="100" step="0.01">
                    <div class="help-text">Manual commission charge per lot (both entry + exit)</div>
                </div>

                <div class="form-group">
                    <label>Minimum Profit per Lot ($)</label>
                    <input type="number" name="min_profit_per_lot" id="min_profit_per_lot" value="{{ config.min_profit_per_lot }}" min="0" max="50000" step="1">
                    <div class="help-text">Target profit per lot AFTER costs. Total = this × lot size. E.g., $50/lot × 30 lots = $1,500 min profit. Set to 0 for statistical exit only.</div>
                </div>

                <div class="form-group">
                    <label>Maximum Loss per Lot ($)</label>
                    <input type="number" name="max_loss_per_lot" id="max_loss_per_lot" value="{{ config.max_loss_per_lot or 100 }}" min="0" max="50000" step="1">
                    <div class="help-text">Absolute stop loss per lot. If unrealized loss exceeds this × lot size, position closes immediately regardless of z-score. Set to 0 to disable.</div>
                </div>

                <!-- Max Loss Calculator -->
                <div style="margin-top: 20px; padding: 15px; background: #fff3e0; border-radius: 8px; border: 1px solid #ff9800;">
                    <div style="font-weight: 600; margin-bottom: 10px; color: #e65100;">🛡️ Max Loss Calculator</div>
                    <p style="color: #666; font-size: 0.9em; margin-bottom: 10px;">
                        See what your max loss setting means in terms of spread movement and get suggestions.
                    </p>
                    <button type="button" id="calc-max-loss-btn" class="btn btn-secondary" style="width: auto; padding: 10px 20px;" onclick="calculateMaxLoss()">
                        Calculate Max Loss
                    </button>
                    <div id="max-loss-results" style="margin-top: 15px; display: none;"></div>
                </div>

                <!-- Cost Estimator -->
                <div style="margin-top: 20px; padding: 15px; background: #f5f5f5; border-radius: 8px; border: 1px solid #ddd;">
                    <div style="font-weight: 600; margin-bottom: 10px;">📊 Cost Estimator</div>
                    <p style="color: #666; font-size: 0.9em; margin-bottom: 10px;">
                        Calculate round-trip costs based on current bid-ask spreads to set appropriate min profit.
                    </p>
                    <button type="button" id="estimate-costs-btn" class="btn btn-secondary" style="width: auto; padding: 10px 20px;" onclick="estimateCosts()">
                        Calculate Costs
                    </button>
                    <div id="cost-results" style="margin-top: 15px; display: none;"></div>
                </div>
            </div>

            <button type="submit" class="btn">Save Settings</button>
            <a href="/" class="btn btn-secondary" style="display: block; text-align: center; margin-top: 10px;">← Back to Monitor</a>
        </form>

        <!-- Test Order Section (outside form) -->
        <div class="card" style="margin-top: 30px; border: 2px solid #ff9800;">
            <div class="card-title" style="color: #ff9800;">Order Connectivity Test</div>
            <p style="margin-bottom: 15px; color: #666;">
                Test that orders can be placed and closed correctly before enabling the algo.
                This will open and immediately close minimum lot size positions on both Spot and Futures symbols.
            </p>
            <p style="margin-bottom: 15px; color: #c62828; font-weight: 500;">
                ⚠ WARNING: This places REAL orders (even in paper mode if broker doesn't support it).
                Uses minimum lot size to minimize cost.
            </p>
            <button type="button" id="test-orders-btn" class="btn" style="background: #ff9800;" onclick="testOrders()">
                Run Order Test
            </button>
            <div id="test-results" style="margin-top: 20px; display: none;">
                <div class="card-title" style="font-size: 0.9em;">Test Results</div>
                <div id="test-results-content"></div>
            </div>
        </div>
    </div>

    <script>
        async function testOrders() {
            const btn = document.getElementById('test-orders-btn');
            const resultsDiv = document.getElementById('test-results');
            const resultsContent = document.getElementById('test-results-content');

            btn.disabled = true;
            btn.textContent = 'Testing... Please wait';
            resultsDiv.style.display = 'none';

            try {
                const response = await fetch('/api/test_orders', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' }
                });

                const data = await response.json();
                resultsDiv.style.display = 'block';

                let html = '';

                // Spot Results
                html += '<div style="margin-bottom: 15px; padding: 10px; border: 1px solid #ddd; border-radius: 4px;">';
                html += '<strong>SPOT: ' + (data.results.spot.symbol || 'Not configured') + '</strong><br>';
                if (data.results.spot.open) {
                    const spotOpen = data.results.spot.open;
                    if (spotOpen.success) {
                        html += '<span style="color: green;">✓ OPEN: Success</span>';
                        html += ' (Lot: ' + spotOpen.volume + ', Price: ' + (spotOpen.price || 'N/A') + ', Filling: ' + spotOpen.filling_mode_desc + ')<br>';
                    } else {
                        html += '<span style="color: red;">✗ OPEN: Failed - ' + spotOpen.error + '</span><br>';
                    }

                    if (data.results.spot.close) {
                        const spotClose = data.results.spot.close;
                        if (spotClose.success) {
                            html += '<span style="color: green;">✓ CLOSE: Success</span>';
                            html += ' (Price: ' + (spotClose.price || 'N/A') + ')';
                        } else {
                            html += '<span style="color: red;">✗ CLOSE: Failed - ' + spotClose.error + '</span>';
                        }
                    }
                } else {
                    html += '<span style="color: red;">✗ ' + (data.results.spot.open?.error || 'Not tested') + '</span>';
                }
                html += '</div>';

                // Futures Results
                html += '<div style="margin-bottom: 15px; padding: 10px; border: 1px solid #ddd; border-radius: 4px;">';
                html += '<strong>FUTURES: ' + (data.results.futures.symbol || 'Not configured') + '</strong><br>';
                if (data.results.futures.open) {
                    const futuresOpen = data.results.futures.open;
                    if (futuresOpen.success) {
                        html += '<span style="color: green;">✓ OPEN: Success</span>';
                        html += ' (Lot: ' + futuresOpen.volume + ', Price: ' + (futuresOpen.price || 'N/A') + ', Filling: ' + futuresOpen.filling_mode_desc + ')<br>';
                    } else {
                        html += '<span style="color: red;">✗ OPEN: Failed - ' + futuresOpen.error + '</span><br>';
                    }

                    if (data.results.futures.close) {
                        const futuresClose = data.results.futures.close;
                        if (futuresClose.success) {
                            html += '<span style="color: green;">✓ CLOSE: Success</span>';
                            html += ' (Price: ' + (futuresClose.price || 'N/A') + ')';
                        } else {
                            html += '<span style="color: red;">✗ CLOSE: Failed - ' + futuresClose.error + '</span>';
                        }
                    }
                } else {
                    html += '<span style="color: red;">✗ ' + (data.results.futures.open?.error || 'Not tested') + '</span>';
                }
                html += '</div>';

                // Summary
                html += '<div style="padding: 10px; border-radius: 4px; ' +
                    (data.results.summary.success ? 'background: #e8f5e9; border: 1px solid #4caf50;' : 'background: #ffebee; border: 1px solid #f44336;') + '">';
                html += '<strong>' + (data.results.summary.success ? '✓ ALL TESTS PASSED' : '✗ SOME TESTS FAILED') + '</strong><br>';
                html += data.results.summary.message;
                html += '</div>';

                resultsContent.innerHTML = html;

            } catch (error) {
                resultsDiv.style.display = 'block';
                resultsContent.innerHTML = '<div style="color: red; padding: 10px; background: #ffebee; border-radius: 4px;">' +
                    '<strong>Error:</strong> ' + error.message + '</div>';
            } finally {
                btn.disabled = false;
                btn.textContent = 'Run Order Test';
            }
        }

        async function estimateCosts() {
            const btn = document.getElementById('estimate-costs-btn');
            const resultsDiv = document.getElementById('cost-results');

            btn.disabled = true;
            btn.textContent = 'Calculating...';

            try {
                const response = await fetch('/api/estimate_costs');
                const data = await response.json();

                if (data.status === 'error') {
                    resultsDiv.innerHTML = '<div style="color: red; padding: 10px; background: #ffebee; border-radius: 4px;">' +
                        '<strong>Error:</strong> ' + data.message + '</div>';
                    resultsDiv.style.display = 'block';
                    return;
                }

                const r = data.results;
                let html = '<table style="width: 100%; font-size: 0.9em; border-collapse: collapse;">';

                // Asset info
                html += '<tr style="background: #e3f2fd;"><td colspan="2" style="padding: 8px; font-weight: 600;">' +
                    r.asset_name + ' (Contract: ' + r.contract_size + ' units/lot)</td></tr>';

                // Spot spread
                if (r.spot.spread_display) {
                    html += '<tr><td style="padding: 6px; border-bottom: 1px solid #eee;">Spot Spread (' + r.spot.symbol + ')</td>' +
                        '<td style="padding: 6px; border-bottom: 1px solid #eee; text-align: right;">' + r.spot.spread_display + ' → <strong>' + r.spot.cost_per_lot_display + '/lot</strong></td></tr>';
                }

                // Futures spread
                if (r.futures.spread_display) {
                    html += '<tr><td style="padding: 6px; border-bottom: 1px solid #eee;">Futures Spread (' + r.futures.symbol + ')</td>' +
                        '<td style="padding: 6px; border-bottom: 1px solid #eee; text-align: right;">' + r.futures.spread_display + ' → <strong>' + r.futures.cost_per_lot_display + '/lot</strong></td></tr>';
                }

                // Entry cost
                if (r.totals.entry_cost_display) {
                    html += '<tr><td style="padding: 6px; border-bottom: 1px solid #eee;">Entry Cost (both legs)</td>' +
                        '<td style="padding: 6px; border-bottom: 1px solid #eee; text-align: right;">' + r.totals.entry_cost_display + '/lot</td></tr>';
                }

                // Round-trip spread cost
                if (r.totals.round_trip_spread_display) {
                    html += '<tr><td style="padding: 6px; border-bottom: 1px solid #eee;">Round-Trip Spread Cost</td>' +
                        '<td style="padding: 6px; border-bottom: 1px solid #eee; text-align: right;">' + r.totals.round_trip_spread_display + '/lot</td></tr>';
                }

                // Commission
                if (r.totals.commission_per_lot > 0) {
                    html += '<tr><td style="padding: 6px; border-bottom: 1px solid #eee;">Commission (entry + exit)</td>' +
                        '<td style="padding: 6px; border-bottom: 1px solid #eee; text-align: right;">' + r.totals.commission_display + '/lot</td></tr>';
                }

                // Total cost
                html += '<tr style="background: #fff3e0;"><td style="padding: 8px; font-weight: 600;">TOTAL COST</td>' +
                    '<td style="padding: 8px; text-align: right; font-weight: 600; font-size: 1.1em;">' + r.totals.total_cost_display + '/lot</td></tr>';

                // Current setting
                html += '<tr><td style="padding: 6px; border-bottom: 1px solid #eee;">Current Min Profit Setting</td>' +
                    '<td style="padding: 6px; border-bottom: 1px solid #eee; text-align: right;">$' + r.totals.current_min_profit + '/lot</td></tr>';

                // Suggestions
                html += '<tr style="background: #e8f5e9;"><td style="padding: 8px;">Suggested Min (50% margin)</td>' +
                    '<td style="padding: 8px; text-align: right;"><button type="button" onclick="document.getElementById(\\'min_profit_per_lot\\').value=' + Math.round(r.totals.suggested_min_profit) + '" style="padding: 4px 12px; cursor: pointer;">' +
                    'Use ' + r.totals.suggested_min_display + '</button></td></tr>';

                html += '<tr style="background: #e8f5e9;"><td style="padding: 8px;">Conservative Min (100% margin)</td>' +
                    '<td style="padding: 8px; text-align: right;"><button type="button" onclick="document.getElementById(\\'min_profit_per_lot\\').value=' + Math.round(r.totals.conservative_min_profit) + '" style="padding: 4px 12px; cursor: pointer;">' +
                    'Use ' + r.totals.conservative_min_display + '</button></td></tr>';

                html += '</table>';

                // Warning if applicable
                if (r.totals.warning) {
                    html += '<div style="margin-top: 10px; padding: 10px; background: #ffebee; border: 1px solid #f44336; border-radius: 4px; color: #c62828;">' +
                        '<strong>⚠ Warning:</strong> ' + r.totals.warning + '</div>';
                }

                resultsDiv.innerHTML = html;
                resultsDiv.style.display = 'block';

            } catch (error) {
                resultsDiv.innerHTML = '<div style="color: red; padding: 10px; background: #ffebee; border-radius: 4px;">' +
                    '<strong>Error:</strong> ' + error.message + '</div>';
                resultsDiv.style.display = 'block';
            } finally {
                btn.disabled = false;
                btn.textContent = 'Calculate Costs';
            }
        }

        async function calculateMaxLoss() {
            const btn = document.getElementById('calc-max-loss-btn');
            const resultsDiv = document.getElementById('max-loss-results');

            btn.disabled = true;
            btn.textContent = 'Calculating...';

            try {
                const response = await fetch('/api/calculate_max_loss');
                const data = await response.json();

                if (data.status === 'error') {
                    resultsDiv.innerHTML = '<div style="color: red; padding: 10px; background: #ffebee; border-radius: 4px;">' +
                        '<strong>Error:</strong> ' + data.message + '</div>';
                    resultsDiv.style.display = 'block';
                    return;
                }

                const r = data.results;
                let html = '<table style="width: 100%; font-size: 0.9em; border-collapse: collapse;">';

                // Status
                if (r.is_disabled) {
                    html += '<tr style="background: #fff3e0;"><td colspan="2" style="padding: 8px; color: #e65100;">' +
                        '<strong>⚠ Max Loss is DISABLED</strong> (set to $0)</td></tr>';
                }

                // Asset info
                html += '<tr style="background: #e3f2fd;"><td colspan="2" style="padding: 8px; font-weight: 600;">' +
                    r.asset_name + ' (Contract: ' + r.contract_size + ' units/lot)</td></tr>';

                // Current setting
                html += '<tr><td style="padding: 6px; border-bottom: 1px solid #eee;">Max Loss per Lot Setting</td>' +
                    '<td style="padding: 6px; border-bottom: 1px solid #eee; text-align: right; font-weight: 600;">$' + r.max_loss_per_lot + '</td></tr>';

                // Total max loss with lot size
                html += '<tr><td style="padding: 6px; border-bottom: 1px solid #eee;">Total Max Loss (' + r.lot_size + ' lots)</td>' +
                    '<td style="padding: 6px; border-bottom: 1px solid #eee; text-align: right;">' + r.total_max_loss_display + '</td></tr>';

                // Spread move to trigger
                html += '<tr><td style="padding: 6px; border-bottom: 1px solid #eee;">Spread Move to Trigger</td>' +
                    '<td style="padding: 6px; border-bottom: 1px solid #eee; text-align: right;">' + r.spread_move_display + '</td></tr>';

                // Current std
                if (r.current_std > 0) {
                    html += '<tr><td style="padding: 6px; border-bottom: 1px solid #eee;">Current Spread σ</td>' +
                        '<td style="padding: 6px; border-bottom: 1px solid #eee; text-align: right;">' + r.current_std_display + '</td></tr>';

                    // Sigma equivalent
                    html += '<tr style="background: #fff8e1;"><td style="padding: 8px; font-weight: 600;">Triggers at</td>' +
                        '<td style="padding: 8px; text-align: right; font-weight: 600; font-size: 1.1em;">' + r.std_equivalent_display + ' from entry</td></tr>';
                }

                // Round-trip cost reference
                if (r.round_trip_cost > 0) {
                    html += '<tr><td style="padding: 6px; border-bottom: 1px solid #eee;">Round-Trip Cost (reference)</td>' +
                        '<td style="padding: 6px; border-bottom: 1px solid #eee; text-align: right;">' + r.round_trip_cost_display + '/lot</td></tr>';

                    // Suggestions
                    html += '<tr style="background: #e8f5e9;"><td style="padding: 8px;">Suggested (2× cost)</td>' +
                        '<td style="padding: 8px; text-align: right;"><button type="button" onclick="document.getElementById(\\'max_loss_per_lot\\').value=' + Math.round(r.suggested_2x) + '" style="padding: 4px 12px; cursor: pointer;">' +
                        'Use ' + r.suggested_2x_display + '</button></td></tr>';

                    html += '<tr style="background: #e8f5e9;"><td style="padding: 8px;">Conservative (3× cost)</td>' +
                        '<td style="padding: 8px; text-align: right;"><button type="button" onclick="document.getElementById(\\'max_loss_per_lot\\').value=' + Math.round(r.suggested_3x) + '" style="padding: 4px 12px; cursor: pointer;">' +
                        'Use ' + r.suggested_3x_display + '</button></td></tr>';
                }

                html += '</table>';

                // Warning if applicable
                if (r.warning) {
                    html += '<div style="margin-top: 10px; padding: 10px; background: #ffebee; border: 1px solid #f44336; border-radius: 4px; color: #c62828;">' +
                        '<strong>⚠ Warning:</strong> ' + r.warning + '</div>';
                }

                // Tip
                if (!r.is_disabled && r.std_equivalent >= 2 && r.std_equivalent <= 4) {
                    html += '<div style="margin-top: 10px; padding: 10px; background: #e8f5e9; border: 1px solid #4caf50; border-radius: 4px; color: #2e7d32;">' +
                        '✓ Good range: ' + r.std_equivalent_display + ' gives room for normal volatility while protecting against tail risk.</div>';
                }

                resultsDiv.innerHTML = html;
                resultsDiv.style.display = 'block';

            } catch (error) {
                resultsDiv.innerHTML = '<div style="color: red; padding: 10px; background: #ffebee; border-radius: 4px;">' +
                    '<strong>Error:</strong> ' + error.message + '</div>';
                resultsDiv.style.display = 'block';
            } finally {
                btn.disabled = false;
                btn.textContent = 'Calculate Max Loss';
            }
        }
    </script>
</body>
</html>'''

# Write templates
with open(os.path.join(TEMPLATE_DIR, 'setup.html'), 'w', encoding='utf-8') as f:
    f.write(SETUP_HTML)

with open(os.path.join(TEMPLATE_DIR, 'monitor.html'), 'w', encoding='utf-8') as f:
    f.write(MONITOR_HTML)

with open(os.path.join(TEMPLATE_DIR, 'settings.html'), 'w', encoding='utf-8') as f:
    f.write(SETTINGS_HTML)


# =============================================================================
# MAIN ENTRY POINT
# =============================================================================
def main():
    """Main entry point"""
    print("=" * 70)
    print("ALGORITHMIC TRADING PORTAL")
    print("Spot-Futures Basis Trading")
    print("=" * 70)
    print()
    print("Starting web server...")
    print()
    print("Open your browser and go to:")
    print()
    print("  http://localhost:8080")
    print()
    print("Features:")
    print("  ▸ Real-time price monitoring")
    print("  ▸ Z-score based signals")
    print("  ▸ Configurable thresholds")
    print("  ▸ Algo trading toggle")
    print("  ▸ Paper/Live modes")
    print("  ▸ Persistent mean calculation")
    print()
    print("Press Ctrl+C to stop")
    print("=" * 70)

    app.run(host='0.0.0.0', port=8080, debug=False)


if __name__ == "__main__":
    main()
