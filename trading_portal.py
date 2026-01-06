#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ALGORITHMIC TRADING PORTAL - GOLD & SILVER
Based on Arb_Monitor UI style with added algo trading capabilities

Features:
- Real-time monitoring (same UI as Arb_Monitor)
- User-configurable trading parameters (lookback, std dev, stop loss)
- Algo trading toggle for non-technical users
- Persistent mean calculation (handles connectivity issues)
- Paper/Live trading modes
- Shareable web interface
"""

from flask import Flask, render_template, request, jsonify, redirect, url_for
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
                silver_spot_symbol TEXT DEFAULT '',
                silver_futures_symbol TEXT DEFAULT '',
                silver_futures_expiry TEXT DEFAULT '',
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
                updated_at TEXT
            )
        ''')

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
                net_pnl REAL DEFAULT 0,
                return_pct REAL DEFAULT 0,
                lot_size REAL DEFAULT 0.1,
                mt5_spot_ticket INTEGER,
                mt5_futures_ticket INTEGER,
                order_status TEXT DEFAULT 'PENDING',
                status TEXT DEFAULT 'OPEN'
            )
        ''')

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
        cursor.execute('SELECT * FROM trading_config WHERE id = 1')
        row = cursor.fetchone()
        conn.close()

        if row:
            return {
                'gold_swap_charge': row[1] if row[1] is not None else 0.0,
                'silver_swap_charge': row[2] if row[2] is not None else 0.0,
                'gold_spot_symbol': row[3] or '',
                'gold_futures_symbol': row[4] or '',
                'gold_futures_expiry': row[5] or '',
                'silver_spot_symbol': row[6] or '',
                'silver_futures_symbol': row[7] or '',
                'silver_futures_expiry': row[8] or '',
                'lookback_period': row[9] if row[9] is not None else 90,
                'lookback_unit': row[10] or 'minutes',
                'entry_std_dev': row[11] if row[11] is not None else 2.0,
                'exit_std_dev': row[12] if row[12] is not None else 0.5,
                'stop_loss_std_dev': row[13] if row[13] is not None else 3.0,
                'time_stop_loss_days': row[14] if row[14] is not None else 0,
                'max_positions': row[15] if row[15] is not None else 3,
                'lot_size': row[16] if row[16] is not None else 0.1,
                'algo_enabled': bool(row[17]),
                'paper_mode': bool(row[18]) if row[18] is not None else True
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
                    silver_spot_symbol = ?,
                    silver_futures_symbol = ?,
                    silver_futures_expiry = ?,
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
                    updated_at = ?
                WHERE id = 1
            ''', (
                config.get('gold_swap_charge', 0),
                config.get('silver_swap_charge', 0),
                config.get('gold_spot_symbol', ''),
                config.get('gold_futures_symbol', ''),
                config.get('gold_futures_expiry', ''),
                config.get('silver_spot_symbol', ''),
                config.get('silver_futures_symbol', ''),
                config.get('silver_futures_expiry', ''),
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
                datetime.now().isoformat()
            ))
            conn.commit()
            conn.close()

    def save_trade(self, trade):
        """Save trade to database"""
        with self.lock:
            conn = self.get_connection()
            cursor = conn.cursor()
            cursor.execute('''
                INSERT OR REPLACE INTO trades VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
        if status:
            cursor.execute('SELECT * FROM trades WHERE status = ? ORDER BY entry_date DESC LIMIT ?', (status, limit))
        else:
            cursor.execute('SELECT * FROM trades ORDER BY entry_date DESC LIMIT ?', (limit,))
        rows = cursor.fetchall()
        conn.close()

        return [{
            'trade_id': r[0], 'asset': r[1], 'direction': r[2],
            'entry_date': r[3], 'exit_date': r[4], 'days_held': r[5],
            'entry_zscore': r[6], 'exit_zscore': r[7],
            'entry_spot_price': r[8], 'entry_futures_price': r[9],
            'exit_spot_price': r[10], 'exit_futures_price': r[11],
            'spot_pnl': r[12], 'futures_pnl': r[13],
            'gross_pnl': r[14], 'swap_cost': r[15], 'commission': r[16],
            'net_pnl': r[17], 'return_pct': r[18], 'lot_size': r[19],
            'mt5_spot_ticket': r[20], 'mt5_futures_ticket': r[21],
            'order_status': r[22], 'status': r[23]
        } for r in rows]

    def get_trade_summary(self):
        """Get total P&L summary"""
        conn = self.get_connection()
        cursor = conn.cursor()
        cursor.execute('''
            SELECT
                COUNT(*) as total_trades,
                SUM(CASE WHEN status = 'CLOSED' THEN net_pnl ELSE 0 END) as total_pnl,
                SUM(CASE WHEN status = 'CLOSED' AND net_pnl > 0 THEN 1 ELSE 0 END) as winning_trades,
                SUM(CASE WHEN status = 'CLOSED' AND net_pnl <= 0 THEN 1 ELSE 0 END) as losing_trades
            FROM trades
        ''')
        row = cursor.fetchone()
        conn.close()
        return {
            'total_trades': row[0] or 0,
            'total_pnl': row[1] or 0,
            'winning_trades': row[2] or 0,
            'losing_trades': row[3] or 0,
            'win_rate': (row[2] / row[0] * 100) if row[0] and row[0] > 0 else 0
        }


# =============================================================================
# TRADING MONITOR - Core monitoring and trading logic
# =============================================================================
class TradingMonitor:
    """Main trading monitor with algo trading capabilities"""

    def __init__(self):
        self.db = DatabaseManager()
        self.config = self.db.get_config() or {}

        self.assets = {
            'GOLD': {
                'name': 'GOLD',
                'spot_symbols': ['XAUUSD', 'XAUUSD_', 'GOLD'],
                'futures_symbols': ['GC0226', 'GC1225', 'XAUUSD.f', 'GCZ4'],
                'futures_expiry': datetime(2026, 2, 24),
                'multiplier': 1.0,
                'lot_size': 100,
                'swap_long': 0.0  # Will be auto-detected from MT5
            },
            'SILVER': {
                'name': 'SILVER',
                'spot_symbols': ['XAGUSD', 'XAGUSD_', 'SILVER'],
                'futures_symbols': ['SI0326', 'SI1225', 'XAGUSD.f', 'SIU4'],
                'futures_expiry': datetime(2026, 2, 26),
                'multiplier': 1.0,
                'lot_size': 5000,
                'swap_long': 0.0  # Will be auto-detected from MT5
            }
        }

        self.active_assets = {}
        self.is_initialized = False
        self.last_update = None
        self.error_message = None

        # Mean calculation cache
        self.spread_cache = {'GOLD': deque(maxlen=1000), 'SILVER': deque(maxlen=1000)}
        self.last_price_save = {}

        # Active positions - load from database
        self.positions = {}
        self._load_open_positions()

        # Background thread
        self.running = False
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
        """Setup symbols for Gold and Silver using user-configured swap charges"""
        for asset_key, asset_config in self.assets.items():
            spot_symbol = None
            futures_symbol = None

            # First try user-configured symbols from config
            config_spot = self.config.get(f'{asset_key.lower()}_spot_symbol', '')
            config_futures = self.config.get(f'{asset_key.lower()}_futures_symbol', '')
            config_expiry = self.config.get(f'{asset_key.lower()}_futures_expiry', '')

            # Parse user-configured expiry date (format: YYYY-MM-DD)
            if config_expiry:
                try:
                    expiry_date = datetime.strptime(config_expiry, '%Y-%m-%d')
                    self.assets[asset_key]['futures_expiry'] = expiry_date
                    logger.info(f"{asset_key}: Using configured expiry: {config_expiry}")
                except ValueError:
                    logger.warning(f"{asset_key}: Invalid expiry date format '{config_expiry}', using default")

            if config_spot:
                symbol_info = mt5.symbol_info(config_spot)
                if symbol_info:
                    spot_symbol = config_spot
                    mt5.symbol_select(config_spot, True)
                    logger.info(f"{asset_key}: Using configured spot symbol: {config_spot}")
                else:
                    logger.warning(f"{asset_key}: Configured spot symbol '{config_spot}' not found in MT5")

            if config_futures:
                if mt5.symbol_info(config_futures):
                    futures_symbol = config_futures
                    mt5.symbol_select(config_futures, True)
                    logger.info(f"{asset_key}: Using configured futures symbol: {config_futures}")
                else:
                    logger.warning(f"{asset_key}: Configured futures symbol '{config_futures}' not found in MT5")

            # Fall back to auto-detection if user didn't configure or symbol not found
            if not spot_symbol:
                for symbol in asset_config['spot_symbols']:
                    symbol_info = mt5.symbol_info(symbol)
                    if symbol_info:
                        spot_symbol = symbol
                        mt5.symbol_select(symbol, True)
                        logger.info(f"{asset_key}: Auto-detected spot symbol: {symbol}")
                        break

            if not futures_symbol:
                for symbol in asset_config['futures_symbols']:
                    if mt5.symbol_info(symbol):
                        futures_symbol = symbol
                        mt5.symbol_select(symbol, True)
                        logger.info(f"{asset_key}: Auto-detected futures symbol: {symbol}")
                        break

            if spot_symbol and futures_symbol:
                # Get user-configured swap charge (required for accurate calculation)
                swap_charge = self.config.get(f'{asset_key.lower()}_swap_charge', 0)

                self.active_assets[asset_key] = {
                    'config': asset_config,
                    'spot_symbol': spot_symbol,
                    'futures_symbol': futures_symbol,
                    'swap_charge': swap_charge
                }
                logger.info(f"{asset_key}: {spot_symbol} + {futures_symbol} | Swap: ${swap_charge:.2f}/lot/day")
            else:
                logger.warning(f"{asset_key}: Could not find symbols - Spot: {spot_symbol}, Futures: {futures_symbol}")

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
                for asset_key in self.active_assets.keys():
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
        # Use user-configured swap charge - must be entered manually for accuracy
        swap_charge = self.config.get(f'{asset_key.lower()}_swap_charge', 0)
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
                swap_charge = self.config.get(f'{asset_key.lower()}_swap_charge', 0)

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

            # Generate signal (pass stats for progress info)
            signal = self._generate_signal(asset_key, zscore, stats)

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
                'timestamp': datetime.now().strftime('%H:%M:%S')
            }

        except Exception as e:
            logger.error(f"Error getting market data for {asset_key}: {e}")
            return None

    def _generate_signal(self, asset_key, zscore, stats=None):
        """Generate trading signal based on z-score"""
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

        # Check existing positions
        has_position = asset_key in self.positions

        if not has_position:
            if zscore > entry_std:
                return {
                    'type': 'SELL_BASIS',
                    'reason': f'Z-score {zscore:.2f} > {entry_std}',
                    'action': 'Buy Spot + Sell Futures'
                }
            elif zscore < -entry_std:
                return {
                    'type': 'BUY_BASIS',
                    'reason': f'Z-score {zscore:.2f} < -{entry_std}',
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
            if asset_key not in self.positions:
                self._open_position(asset_key, signal_type, data)

        elif signal_type in ['CLOSE', 'STOP_LOSS', 'TIME_STOP']:
            if asset_key in self.positions:
                self._close_position(asset_key, signal_type, data)

    def _execute_mt5_order(self, symbol, order_type, volume, comment=""):
        """Execute an order through MT5"""
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
            deviation = 20  # Max price deviation in points

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
                # Ensure precision (avoid floating point issues)
                volume = round(volume, 2)

            logger.info(f"MT5 order: {symbol} volume={volume} (min={vol_min}, max={vol_max}, step={vol_step})")

            request = {
                "action": mt5.TRADE_ACTION_DEAL,
                "symbol": symbol,
                "volume": volume,
                "type": order_type,
                "price": price,
                "deviation": deviation,
                "magic": 123456,
                "comment": comment,
                "type_time": mt5.ORDER_TIME_GTC,
                "type_filling": mt5.ORDER_FILLING_IOC,
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

    def _close_mt5_position(self, ticket, symbol, volume, position_type):
        """Close an MT5 position by ticket"""
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

            request = {
                "action": mt5.TRADE_ACTION_DEAL,
                "symbol": symbol,
                "volume": volume,
                "type": close_type,
                "position": ticket,
                "price": price,
                "deviation": 20,
                "magic": 123456,
                "comment": "Close position",
                "type_time": mt5.ORDER_TIME_GTC,
                "type_filling": mt5.ORDER_FILLING_IOC,
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

        position = {
            'trade_id': trade_id,
            'asset': asset_key,
            'direction': direction,
            'entry_date': datetime.now().strftime('%Y-%m-%d'),
            'entry_zscore': data['zscore'],
            'entry_spot_price': data['spot_price'],
            'entry_futures_price': data['futures_price'],
            'lot_size': lot_size,
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

        # Execute futures order
        futures_result = self._execute_mt5_order(
            futures_symbol, futures_order_type, lot_size,
            f"{asset_key} {direction} Futures"
        )

        # Execute spot order
        spot_result = self._execute_mt5_order(
            spot_symbol, spot_order_type, lot_size,
            f"{asset_key} {direction} Spot"
        )

        if futures_result['success'] and spot_result['success']:
            position['mt5_futures_ticket'] = futures_result['ticket']
            position['mt5_spot_ticket'] = spot_result['ticket']
            position['order_status'] = 'FILLED'
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
                    'swap': pos.swap,
                    'commission': pos.commission,
                    'profit': pos.profit
                }
        except Exception as e:
            logger.error(f"Error getting MT5 position costs: {e}")
        return {'swap': 0, 'commission': 0, 'profit': 0}

    def _close_position(self, asset_key, close_reason, data):
        """Close an existing position with P&L calculation"""
        if asset_key not in self.positions:
            return

        position = self.positions[asset_key]
        entry_date = datetime.strptime(position['entry_date'], '%Y-%m-%d')
        exit_date = datetime.now()

        # Update position with exit data
        position['exit_date'] = exit_date.strftime('%Y-%m-%d')
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
        position['commission'] = spot_costs['commission'] + futures_costs['commission']

        logger.info(f"MT5 Costs - Swap: ${position['swap_cost']:.2f}, Commission: ${position['commission']:.2f}")

        position['net_pnl'] = position['gross_pnl'] + position['swap_cost'] + position['commission']

        # Calculate return % based on margin used (approximate)
        margin_used = position['entry_spot_price'] * lot_size * contract_size * 0.1  # 10% margin
        position['return_pct'] = (position['net_pnl'] / margin_used * 100) if margin_used > 0 else 0

        mode_label = 'PAPER' if self.config.get('paper_mode', True) else 'LIVE'

        # Close MT5 positions (both paper and live mode)
        asset_data = self.active_assets.get(asset_key, {})
        spot_symbol = asset_data.get('spot_symbol')
        futures_symbol = asset_data.get('futures_symbol')

        if position.get('mt5_futures_ticket'):
            self._close_mt5_position(
                position['mt5_futures_ticket'],
                futures_symbol, lot_size,
                mt5.ORDER_TYPE_BUY if position['direction'] == 'Short Spread' else mt5.ORDER_TYPE_SELL
            )

        if position.get('mt5_spot_ticket'):
            self._close_mt5_position(
                position['mt5_spot_ticket'],
                spot_symbol, lot_size,
                mt5.ORDER_TYPE_SELL if position['direction'] == 'Short Spread' else mt5.ORDER_TYPE_BUY
            )

        logger.info(f"{mode_label} CLOSE: {asset_key} - {close_reason} - Gross: ${position['gross_pnl']:.2f}, Swap: ${position['swap_cost']:.2f}, Comm: ${position['commission']:.2f}, Net: ${position['net_pnl']:.2f}")

        position['status'] = 'CLOSED'
        self.db.save_trade(position)
        del self.positions[asset_key]

    def get_all_data(self):
        """Get data for all assets"""
        data = {}
        for asset_key in ['GOLD', 'SILVER']:
            market_data = self.get_market_data(asset_key)
            if market_data:
                data[asset_key] = market_data

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
                    'sl': pos.sl,
                    'tp': pos.tp,
                    'profit': pos.profit,
                    'return_pct': return_pct,
                    'swap': pos.swap,
                    'commission': pos.commission,
                    'time': datetime.fromtimestamp(pos.time).strftime('%Y-%m-%d %H:%M:%S'),
                    'magic': pos.magic,
                    'comment': pos.comment
                })

            return result
        except Exception as e:
            logger.error(f"Error getting MT5 positions: {e}")
            return []

    def get_enriched_positions(self):
        """Get algo positions with unrealized P&L from current prices"""
        enriched = []
        for asset_key, position in self.positions.items():
            pos_copy = position.copy()

            # Get current prices for this asset
            asset_data = self.active_assets.get(asset_key, {})
            if asset_data:
                current_data = self.get_market_data(asset_key)
                if current_data:
                    entry_spot = position.get('entry_spot_price', 0)
                    entry_futures = position.get('entry_futures_price', 0)
                    current_spot = current_data.get('spot_price', 0)
                    current_futures = current_data.get('futures_price', 0)
                    lot_size = position.get('lot_size', 0.1)

                    # Calculate unrealized P&L based on direction
                    # Long Spread: Buy Futures + Sell Spot
                    # Short Spread: Sell Futures + Buy Spot
                    if position.get('direction') == 'Long Spread':
                        # Long spread profits when spread widens
                        futures_pnl = (current_futures - entry_futures) * lot_size * 100
                        spot_pnl = (entry_spot - current_spot) * lot_size * 100
                    else:
                        # Short spread profits when spread narrows
                        futures_pnl = (entry_futures - current_futures) * lot_size * 100
                        spot_pnl = (current_spot - entry_spot) * lot_size * 100

                    unrealized_pnl = futures_pnl + spot_pnl
                    pos_copy['unrealized_pnl'] = unrealized_pnl
                    pos_copy['current_spot'] = current_spot
                    pos_copy['current_futures'] = current_futures
                    pos_copy['current_spread'] = current_futures - current_spot
                    pos_copy['entry_spread'] = entry_futures - entry_spot

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
            # Get symbol configurations
            gold_spot = request.form.get('gold_spot_symbol', '').strip()
            gold_futures = request.form.get('gold_futures_symbol', '').strip()
            gold_expiry = request.form.get('gold_futures_expiry', '').strip()
            silver_spot = request.form.get('silver_spot_symbol', '').strip()
            silver_futures = request.form.get('silver_futures_symbol', '').strip()
            silver_expiry = request.form.get('silver_futures_expiry', '').strip()

            # Swap charges are optional - will auto-detect from MT5
            gold_swap = float(request.form.get('gold_swap', 0) or 0)
            silver_swap = float(request.form.get('silver_swap', 0) or 0)

            # Save config
            monitor.config['gold_spot_symbol'] = gold_spot
            monitor.config['gold_futures_symbol'] = gold_futures
            monitor.config['gold_futures_expiry'] = gold_expiry
            monitor.config['silver_spot_symbol'] = silver_spot
            monitor.config['silver_futures_symbol'] = silver_futures
            monitor.config['silver_futures_expiry'] = silver_expiry
            monitor.config['gold_swap_charge'] = gold_swap
            monitor.config['silver_swap_charge'] = silver_swap
            monitor.db.save_config(monitor.config)

            logger.info(f"Setup: Gold={gold_spot}/{gold_futures} (exp:{gold_expiry}), Silver={silver_spot}/{silver_futures} (exp:{silver_expiry})")

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
    trade_history = monitor.db.get_trades(limit=50)
    trade_summary = monitor.db.get_trade_summary()

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
        'config': {
            'algo_enabled': monitor.config.get('algo_enabled', False),
            'paper_mode': monitor.config.get('paper_mode', True),
            'lookback_period': monitor.config.get('lookback_period', 90),
            'lookback_unit': monitor.config.get('lookback_unit', 'minutes'),
            'entry_std_dev': monitor.config.get('entry_std_dev', 2.0),
            'exit_std_dev': monitor.config.get('exit_std_dev', 0.5),
            'stop_loss_std_dev': monitor.config.get('stop_loss_std_dev', 3.0),
            'time_stop_loss_days': monitor.config.get('time_stop_loss_days', 0)
        },
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


@app.route('/api/trades')
def get_trades():
    """Get trade history"""
    trades = monitor.db.get_trades(50)
    return jsonify(trades)


@app.route('/restart')
def restart():
    """Restart and go back to setup"""
    monitor.stop_background_updates()
    monitor.is_initialized = False
    mt5.shutdown()
    return redirect(url_for('setup'))


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
        <div class="subtitle">Gold & Silver Basis Trading</div>

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
                <div class="section-title">GOLD INSTRUMENTS</div>
                <div class="form-group">
                    <label>Gold Spot Symbol</label>
                    <input type="text" name="gold_spot_symbol" value="{{ config.gold_spot_symbol or '' }}" placeholder="e.g., XAUUSD, GOLD, XAUUSDm">
                    <div class="help-text">Your broker's gold spot symbol (leave empty to auto-detect)</div>
                </div>
                <div class="form-group">
                    <label>Gold Futures Symbol</label>
                    <input type="text" name="gold_futures_symbol" value="{{ config.gold_futures_symbol or '' }}" placeholder="e.g., GC0226, GCZ4, XAUUSD.f">
                    <div class="help-text">Your broker's gold futures symbol (required for basis trading)</div>
                </div>
                <div class="form-group">
                    <label>Futures Expiry Date</label>
                    <input type="date" name="gold_futures_expiry" value="{{ config.gold_futures_expiry or '' }}">
                    <div class="help-text">Gold futures contract expiry date (default: 2026-02-24)</div>
                </div>
                <div class="form-group">
                    <label>Daily Swap Charge (USD per lot)</label>
                    <input type="number" name="gold_swap" step="0.01" min="0" value="{{ config.gold_swap_charge or 0 }}" placeholder="e.g., 45.67">
                    <div class="help-text">Check MT5: Right-click XAUUSD → Specification → Swap Long. Lot size: 100 oz</div>
                </div>
            </div>

            <div class="section">
                <div class="section-title">SILVER INSTRUMENTS</div>
                <div class="form-group">
                    <label>Silver Spot Symbol</label>
                    <input type="text" name="silver_spot_symbol" value="{{ config.silver_spot_symbol or '' }}" placeholder="e.g., XAGUSD, SILVER, XAGUSDm">
                    <div class="help-text">Your broker's silver spot symbol (leave empty to auto-detect)</div>
                </div>
                <div class="form-group">
                    <label>Silver Futures Symbol</label>
                    <input type="text" name="silver_futures_symbol" value="{{ config.silver_futures_symbol or '' }}" placeholder="e.g., SI0326, SIZ4, XAGUSD.f">
                    <div class="help-text">Your broker's silver futures symbol (required for basis trading)</div>
                </div>
                <div class="form-group">
                    <label>Futures Expiry Date</label>
                    <input type="date" name="silver_futures_expiry" value="{{ config.silver_futures_expiry or '' }}">
                    <div class="help-text">Silver futures contract expiry date (default: 2026-02-26)</div>
                </div>
                <div class="form-group">
                    <label>Daily Swap Charge (USD per lot)</label>
                    <input type="number" name="silver_swap" step="0.01" min="0" value="{{ config.silver_swap_charge or 0 }}" placeholder="e.g., 5.23">
                    <div class="help-text">Check MT5: Right-click XAGUSD → Specification → Swap Long. Lot size: 5,000 oz</div>
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
        .signal-section.stop-loss { border-color: #9c27b0; background: #f3e5f5; }

        .zscore-display {
            font-size: 2.5em;
            font-weight: 700;
            margin: 10px 0;
        }
        .signal-section.sell-basis .zscore-display { color: #c62828; }
        .signal-section.buy-basis .zscore-display { color: #2e7d32; }
        .signal-section.time-stop .zscore-display { color: #ff9800; }
        .signal-section.stop-loss .zscore-display { color: #9c27b0; }
        .signal-type { font-size: 1.1em; font-weight: 600; margin-bottom: 5px; }
        .signal-reason { color: #666; font-size: 0.9em; }

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
        .trade-history-table-wrapper { overflow-x: auto; }
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

        @media (max-width: 600px) {
            .price-grid { grid-template-columns: repeat(2, 1fr); }
            .controls { flex-direction: column; align-items: flex-start; }
            .summary { flex-direction: column; }
        }
    </style>
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
        <a href="/settings" class="settings-link">⚙ Settings</a>
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

    <div class="summary">
        <div class="summary-item cheap">
            <div class="summary-count" id="cheap-count">0</div>
            <div class="summary-label">CHEAP</div>
        </div>
        <div class="summary-item fair">
            <div class="summary-count" id="fair-count">0</div>
            <div class="summary-label">FAIR</div>
        </div>
        <div class="summary-item expensive">
            <div class="summary-count" id="expensive-count">0</div>
            <div class="summary-label">EXPENSIVE</div>
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
                <span class="summary-stat">Win Rate: <strong id="win-rate">0%</strong></span>
                <span class="summary-stat">Trades: <strong id="total-trades">0</strong></span>
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
                        <th>Net P&L</th>
                        <th>Return</th>
                    </tr>
                </thead>
                <tbody id="trade-history-body">
                    <tr><td colspan="15" style="text-align: center; color: #666;">No trades yet</td></tr>
                </tbody>
            </table>
        </div>
    </div>

    <div class="footer">
        <div>Last update: <span id="last-update">-</span></div>
        <a href="/restart">↻ Restart</a>
    </div>

    <script>
        function updateData() {
            fetch('/api/data')
                .then(res => res.json())
                .then(data => {
                    if (data.error) return;

                    document.getElementById('timestamp').textContent = new Date().toLocaleTimeString();
                    document.getElementById('last-update').textContent = data.last_update;

                    document.getElementById('cheap-count').textContent = data.summary.cheap;
                    document.getElementById('fair-count').textContent = data.summary.fair;
                    document.getElementById('expensive-count').textContent = data.summary.expensive;

                    const cfg = data.config;
                    document.getElementById('algo-toggle').checked = cfg.algo_enabled;
                    document.getElementById('paper-toggle').checked = cfg.paper_mode;
                    document.getElementById('algo-status').textContent = cfg.algo_enabled ? 'ON' : 'OFF';
                    document.getElementById('algo-status').className = 'status-badge ' + (cfg.algo_enabled ? 'active' : 'inactive');
                    document.getElementById('mode-status').textContent = cfg.paper_mode ? 'PAPER' : 'LIVE';
                    document.getElementById('mode-status').className = 'status-badge ' + (cfg.paper_mode ? 'paper' : 'live');
                    let thresholdText = `Entry: ±${cfg.entry_std_dev}σ | Exit: ±${cfg.exit_std_dev}σ | Stop: ±${cfg.stop_loss_std_dev}σ | Lookback: ${cfg.lookback_period} ${cfg.lookback_unit}`;
                    if (cfg.time_stop_loss_days > 0) {
                        thresholdText += ` | Time Stop: ${cfg.time_stop_loss_days} days`;
                    }
                    document.getElementById('thresholds').textContent = thresholdText;

                    const container = document.getElementById('assets-container');
                    container.innerHTML = '';
                    for (const [key, asset] of Object.entries(data.data)) {
                        container.appendChild(createAssetCard(asset));
                    }

                    // Update account info
                    updateAccountInfo(data.account);

                    // Update MT5 positions
                    updateMT5Positions(data.mt5_positions);

                    // Update algo positions
                    updatePositions(data.positions);

                    // Update trade history
                    updateTradeHistory(data.trade_history, data.trade_summary);
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

            let html = `
                <div class="position-header">
                    <div>Symbol</div>
                    <div>Type</div>
                    <div>Volume</div>
                    <div>Open Price</div>
                    <div>P&L</div>
                    <div>Return %</div>
                </div>
            `;

            positions.forEach(pos => {
                const pnlClass = pos.profit >= 0 ? 'positive' : 'negative';
                const typeClass = pos.type.toLowerCase();
                const pnlSign = pos.profit >= 0 ? '+' : '';
                const returnSign = pos.return_pct >= 0 ? '+' : '';

                html += `
                    <div class="position-card">
                        <div class="pos-symbol">${pos.symbol}</div>
                        <div><span class="pos-type ${typeClass}">${pos.type}</span></div>
                        <div>${pos.volume}</div>
                        <div>${pos.price_open.toFixed(pos.price_open > 100 ? 2 : 4)}</div>
                        <div class="pos-pnl ${pnlClass}">${pnlSign}$${pos.profit.toFixed(2)}</div>
                        <div class="pos-return ${pnlClass}">${returnSign}${pos.return_pct.toFixed(2)}%</div>
                    </div>
                `;
            });

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
            else if (signal.type === 'STOP_LOSS') signalClass = 'stop-loss';

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
                </div>

                <div class="signal-section ${signalClass}">
                    <div style="font-size: 0.75em; color: #888; margin-bottom: 5px;">PURE MEAN REVERSION</div>
                    <div class="signal-type">${signal.type.replace('_', ' ')}</div>
                    <div class="zscore-display">${zscore}σ</div>
                    <div class="signal-reason">${signal.reason || ''}</div>
                    ${asset.stats ? `
                    <div class="stats-row" style="font-size: 16px;">
                        <span>Mean: ${asset.stats.mean.toFixed(2)}</span>
                        <span>Std: ${asset.stats.std.toFixed(2)}</span>
                        <span>Points: ${asset.stats.count}</span>
                    </div>
                    <div style="margin-top: 8px; padding-top: 8px; border-top: 1px solid #ddd; font-size: 16px;">
                        <div style="color: #888; margin-bottom: 8px;">Current Spread: <strong style="color: #333;">${asset.actual_basis.toFixed(2)}</strong></div>
                        <div style="margin-bottom: 6px;">
                            <div style="color: #d9534f; margin-bottom: 2px;"><strong>Short Spread</strong> (Entry ↑): ${asset.stats.upper_entry.toFixed(2)}</div>
                            <div style="color: #5cb85c;">Exit: ${asset.stats.upper_exit.toFixed(2)}</div>
                        </div>
                        <div>
                            <div style="color: #5cb85c; margin-bottom: 2px;"><strong>Long Spread</strong> (Entry ↓): ${asset.stats.lower_entry.toFixed(2)}</div>
                            <div style="color: #d9534f;">Exit: ${asset.stats.lower_exit.toFixed(2)}</div>
                        </div>
                    </div>` : ''}
                </div>
            `;

            return card;
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
                const pnlSign = unrealizedPnl >= 0 ? '+' : '';
                const entrySpread = p.entry_spread ? p.entry_spread.toFixed(2) : '--';
                const currentSpread = p.current_spread ? p.current_spread.toFixed(2) : '--';

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
                        <div>Date: ${p.entry_date || '--'}</div>
                        <div>Status: <span class="${p.order_status === 'FILLED' ? 'pnl-positive' : ''}">${p.order_status || 'PENDING'}</span></div>
                    </div>
                    <div style="margin-top: 10px; padding-top: 8px; border-top: 1px solid #ddd; text-align: center;">
                        <div style="font-size: 0.8em; color: #888;">Unrealized P&L</div>
                        <div class="${pnlClass}" style="font-size: 1.3em; font-weight: bold;">${pnlSign}$${Math.abs(unrealizedPnl).toFixed(2)}</div>
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

            document.getElementById('win-rate').textContent = (summary.win_rate || 0).toFixed(1) + '%';
            document.getElementById('total-trades').textContent = summary.total_trades || 0;

            // Update table
            const tbody = document.getElementById('trade-history-body');
            if (!trades || trades.length === 0) {
                tbody.innerHTML = '<tr><td colspan="15" style="text-align: center; color: #666;">No trades yet</td></tr>';
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
                    <td style="color: #c62828;">$${(t.swap_cost || 0).toFixed(2)}</td>
                    <td style="color: #c62828;">$${(t.commission || 0).toFixed(2)}</td>
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
                <div class="card-title">Position Sizing</div>

                <div class="form-group">
                    <label>Max Positions per Asset</label>
                    <input type="number" name="max_positions" value="{{ config.max_positions }}" min="1" max="10">
                    <div class="help-text">Maximum concurrent positions allowed per asset</div>
                </div>

                <div class="form-group">
                    <label>Lot Size</label>
                    <input type="number" name="lot_size" value="{{ config.lot_size }}" min="0.01" max="10" step="0.01">
                    <div class="help-text">Size of each trade in lots</div>
                </div>
            </div>

            <button type="submit" class="btn">Save Settings</button>
            <a href="/" class="btn btn-secondary" style="display: block; text-align: center; margin-top: 10px;">← Back to Monitor</a>
        </form>
    </div>
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
    print("Gold & Silver Basis Trading")
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
