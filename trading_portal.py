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
                lookback_period INTEGER DEFAULT 90,
                entry_std_dev REAL DEFAULT 2.0,
                exit_std_dev REAL DEFAULT 0.5,
                stop_loss_std_dev REAL DEFAULT 3.0,
                max_positions INTEGER DEFAULT 3,
                lot_size REAL DEFAULT 0.1,
                algo_enabled INTEGER DEFAULT 0,
                paper_mode INTEGER DEFAULT 1,
                updated_at TEXT
            )
        ''')

        # Trades log
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS trades (
                trade_id TEXT PRIMARY KEY,
                asset TEXT,
                signal_type TEXT,
                entry_spread REAL,
                entry_zscore REAL,
                entry_time TEXT,
                exit_spread REAL,
                exit_zscore REAL,
                exit_time TEXT,
                pnl REAL,
                status TEXT
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

    def get_price_history(self, asset, limit=500):
        """Get price history for mean calculation"""
        conn = self.get_connection()
        cursor = conn.cursor()
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
                'gold_swap_charge': row[1] or 0.0,
                'silver_swap_charge': row[2] or 0.0,
                'lookback_period': row[3] or 90,
                'entry_std_dev': row[4] or 2.0,
                'exit_std_dev': row[5] or 0.5,
                'stop_loss_std_dev': row[6] or 3.0,
                'max_positions': row[7] or 3,
                'lot_size': row[8] or 0.1,
                'algo_enabled': bool(row[9]),
                'paper_mode': bool(row[10]) if row[10] is not None else True
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
                    lookback_period = ?,
                    entry_std_dev = ?,
                    exit_std_dev = ?,
                    stop_loss_std_dev = ?,
                    max_positions = ?,
                    lot_size = ?,
                    algo_enabled = ?,
                    paper_mode = ?,
                    updated_at = ?
                WHERE id = 1
            ''', (
                config.get('gold_swap_charge', 0),
                config.get('silver_swap_charge', 0),
                config.get('lookback_period', 90),
                config.get('entry_std_dev', 2.0),
                config.get('exit_std_dev', 0.5),
                config.get('stop_loss_std_dev', 3.0),
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
                INSERT OR REPLACE INTO trades VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''', (
                trade['trade_id'], trade['asset'], trade['signal_type'],
                trade['entry_spread'], trade['entry_zscore'], trade['entry_time'],
                trade.get('exit_spread'), trade.get('exit_zscore'), trade.get('exit_time'),
                trade.get('pnl', 0), trade['status']
            ))
            conn.commit()
            conn.close()

    def get_trades(self, limit=50):
        """Get recent trades"""
        conn = self.get_connection()
        cursor = conn.cursor()
        cursor.execute('SELECT * FROM trades ORDER BY entry_time DESC LIMIT ?', (limit,))
        rows = cursor.fetchall()
        conn.close()

        return [{
            'trade_id': r[0], 'asset': r[1], 'signal_type': r[2],
            'entry_spread': r[3], 'entry_zscore': r[4], 'entry_time': r[5],
            'exit_spread': r[6], 'exit_zscore': r[7], 'exit_time': r[8],
            'pnl': r[9], 'status': r[10]
        } for r in rows]


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
                'lot_size': 100
            },
            'SILVER': {
                'name': 'SILVER',
                'spot_symbols': ['XAGUSD', 'XAGUSD_', 'SILVER'],
                'futures_symbols': ['SI0326', 'SI1225', 'XAGUSD.f', 'SIU4'],
                'futures_expiry': datetime(2026, 2, 26),
                'multiplier': 1.0,
                'lot_size': 5000
            }
        }

        self.active_assets = {}
        self.is_initialized = False
        self.last_update = None
        self.error_message = None

        # Mean calculation cache
        self.spread_cache = {'GOLD': deque(maxlen=1000), 'SILVER': deque(maxlen=1000)}
        self.last_price_save = {}

        # Active positions
        self.positions = {}

        # Background thread
        self.running = False
        self.update_thread = None

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
        """Setup symbols for Gold and Silver"""
        for asset_key, asset_config in self.assets.items():
            spot_symbol = None
            futures_symbol = None

            for symbol in asset_config['spot_symbols']:
                if mt5.symbol_info(symbol):
                    spot_symbol = symbol
                    mt5.symbol_select(symbol, True)
                    break

            for symbol in asset_config['futures_symbols']:
                if mt5.symbol_info(symbol):
                    futures_symbol = symbol
                    mt5.symbol_select(symbol, True)
                    break

            if spot_symbol and futures_symbol:
                self.active_assets[asset_key] = {
                    'config': asset_config,
                    'spot_symbol': spot_symbol,
                    'futures_symbol': futures_symbol
                }
                logger.info(f"{asset_key}: {spot_symbol} + {futures_symbol}")

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
                # Collect prices every 2 seconds for mean calculation
                for asset_key in self.active_assets.keys():
                    data = self.get_market_data(asset_key)
                    if data:
                        # Save to cache
                        self.spread_cache[asset_key].append({
                            'timestamp': datetime.now(),
                            'spread': data['swap_diff'],
                            'actual_basis': data['actual_basis']
                        })

                        # Save to database every 30 seconds
                        last_save = self.last_price_save.get(asset_key, datetime.min)
                        if (datetime.now() - last_save).total_seconds() > 30:
                            self.db.save_price(
                                asset_key, data['spot_price'], data['futures_price'],
                                data['actual_basis'], data['swap_diff']
                            )
                            self.last_price_save[asset_key] = datetime.now()

                        # Process algo trading if enabled
                        if self.config.get('algo_enabled'):
                            self._process_algo_trading(asset_key, data)

                time.sleep(2)

            except Exception as e:
                logger.error(f"Background loop error: {e}")
                time.sleep(5)

    def calculate_swap_basis(self, asset_key, spot_price, time_to_expiry):
        """Calculate swap-based basis"""
        swap_charge = self.config.get(f'{asset_key.lower()}_swap_charge', 0)
        lot_size = self.assets[asset_key]['lot_size']

        if swap_charge <= 0:
            return spot_price, 0, 0

        position_value = spot_price * lot_size
        daily_swap_rate = swap_charge / position_value
        annual_swap_rate = daily_swap_rate * 365

        swap_futures_price = spot_price * math.exp(annual_swap_rate * time_to_expiry)
        swap_basis = swap_futures_price - spot_price

        return swap_futures_price, swap_basis, annual_swap_rate

    def get_statistics(self, asset_key):
        """Get rolling statistics for z-score calculation"""
        lookback = self.config.get('lookback_period', 90)

        # First try cache
        cache = list(self.spread_cache.get(asset_key, []))

        if len(cache) >= lookback:
            spreads = [d['spread'] for d in cache[-lookback:]]
        else:
            # Fall back to database
            history = self.db.get_price_history(asset_key, lookback)
            if len(history) < 10:
                return None
            spreads = [row[1] for row in history]  # swap_diff column

            # Rebuild cache
            for row in reversed(history):
                if len(self.spread_cache[asset_key]) < 1000:
                    self.spread_cache[asset_key].append({
                        'timestamp': datetime.now(),
                        'spread': row[1]
                    })

        if len(spreads) < 10:
            return None

        return {
            'mean': np.mean(spreads),
            'std': np.std(spreads),
            'count': len(spreads)
        }

    def calculate_zscore(self, asset_key, current_value):
        """Calculate z-score for current spread"""
        stats = self.get_statistics(asset_key)
        if not stats or stats['std'] == 0:
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

            if time_to_expiry > 0:
                swap_futures_price, swap_basis, annual_swap_rate = self.calculate_swap_basis(
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

            # Calculate z-score
            zscore, stats = self.calculate_zscore(asset_key, swap_diff)

            # Generate signal
            signal = self._generate_signal(asset_key, zscore)

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

    def _generate_signal(self, asset_key, zscore):
        """Generate trading signal based on z-score"""
        if zscore is None:
            return {'type': 'NO_DATA', 'reason': 'Insufficient data for signal'}

        entry_std = self.config.get('entry_std_dev', 2.0)
        exit_std = self.config.get('exit_std_dev', 0.5)
        stop_loss_std = self.config.get('stop_loss_std_dev', 3.0)

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
            if position['signal_type'] == 'SELL_BASIS':
                if zscore <= exit_std:
                    return {
                        'type': 'CLOSE',
                        'reason': f'Z-score {zscore:.2f} <= {exit_std} (exit)',
                        'action': 'Close SELL_BASIS position'
                    }
                if zscore > stop_loss_std:
                    return {
                        'type': 'STOP_LOSS',
                        'reason': f'Z-score {zscore:.2f} > {stop_loss_std} (stop)',
                        'action': 'Stop loss - Close position'
                    }
            elif position['signal_type'] == 'BUY_BASIS':
                if zscore >= -exit_std:
                    return {
                        'type': 'CLOSE',
                        'reason': f'Z-score {zscore:.2f} >= -{exit_std} (exit)',
                        'action': 'Close BUY_BASIS position'
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

        elif signal_type in ['CLOSE', 'STOP_LOSS']:
            if asset_key in self.positions:
                self._close_position(asset_key, signal_type, data)

    def _open_position(self, asset_key, signal_type, data):
        """Open a new position"""
        trade_id = str(uuid.uuid4())[:8]

        position = {
            'trade_id': trade_id,
            'asset': asset_key,
            'signal_type': signal_type,
            'entry_spread': data['swap_diff'],
            'entry_zscore': data['zscore'],
            'entry_time': datetime.now().isoformat(),
            'status': 'OPEN'
        }

        # Execute trade if live mode
        if not self.config.get('paper_mode', True):
            # Add MT5 order execution here
            logger.info(f"LIVE TRADE: {asset_key} {signal_type}")
        else:
            logger.info(f"PAPER TRADE: {asset_key} {signal_type}")

        self.positions[asset_key] = position
        self.db.save_trade(position)

    def _close_position(self, asset_key, close_reason, data):
        """Close an existing position"""
        if asset_key not in self.positions:
            return

        position = self.positions[asset_key]
        position['exit_spread'] = data['swap_diff']
        position['exit_zscore'] = data['zscore']
        position['exit_time'] = datetime.now().isoformat()
        position['status'] = 'CLOSED'

        # Calculate P&L (simplified)
        spread_diff = position['exit_spread'] - position['entry_spread']
        if position['signal_type'] == 'SELL_BASIS':
            position['pnl'] = -spread_diff  # Profit when spread decreases
        else:
            position['pnl'] = spread_diff  # Profit when spread increases

        if not self.config.get('paper_mode', True):
            logger.info(f"LIVE CLOSE: {asset_key} - {close_reason}")
        else:
            logger.info(f"PAPER CLOSE: {asset_key} - {close_reason}")

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
            gold_swap = float(request.form.get('gold_swap', 0))
            silver_swap = float(request.form.get('silver_swap', 0))

            if gold_swap <= 0 or silver_swap <= 0:
                return render_template('setup.html', error="Please enter positive swap charges", config=monitor.config)

            # Save basic config
            monitor.config['gold_swap_charge'] = gold_swap
            monitor.config['silver_swap_charge'] = silver_swap
            monitor.db.save_config(monitor.config)

            if monitor.initialize_mt5():
                monitor.is_initialized = True
                monitor.start_background_updates()
                return redirect(url_for('index'))
            else:
                return render_template('setup.html', error=monitor.error_message or "Failed to connect to MT5", config=monitor.config)

        except ValueError:
            return render_template('setup.html', error="Please enter valid numbers", config=monitor.config)

    return render_template('setup.html', error=None, config=monitor.config)


@app.route('/settings', methods=['GET', 'POST'])
def settings():
    """Trading settings page"""
    if request.method == 'POST':
        try:
            monitor.config['lookback_period'] = int(request.form.get('lookback_period', 90))
            monitor.config['entry_std_dev'] = float(request.form.get('entry_std_dev', 2.0))
            monitor.config['exit_std_dev'] = float(request.form.get('exit_std_dev', 0.5))
            monitor.config['stop_loss_std_dev'] = float(request.form.get('stop_loss_std_dev', 3.0))
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

    return jsonify({
        'data': data,
        'summary': {
            'cheap': cheap_count,
            'fair': fair_count,
            'expensive': expensive_count
        },
        'positions': list(monitor.positions.values()),
        'config': {
            'algo_enabled': monitor.config.get('algo_enabled', False),
            'paper_mode': monitor.config.get('paper_mode', True),
            'lookback_period': monitor.config.get('lookback_period', 90),
            'entry_std_dev': monitor.config.get('entry_std_dev', 2.0),
            'exit_std_dev': monitor.config.get('exit_std_dev', 0.5),
            'stop_loss_std_dev': monitor.config.get('stop_loss_std_dev', 3.0)
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

# Setup page template
SETUP_HTML = '''<!DOCTYPE html>
<html>
<head>
    <title>Trading Portal - Setup</title>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }
        body {
            font-family: 'Courier New', monospace;
            background: #000;
            color: #00ff00;
            min-height: 100vh;
            display: flex;
            justify-content: center;
            align-items: center;
        }
        .container {
            max-width: 600px;
            padding: 40px;
            border: 1px solid #00ff00;
        }
        h1 {
            text-align: center;
            margin-bottom: 10px;
            font-size: 1.5em;
        }
        .subtitle {
            text-align: center;
            color: #008800;
            margin-bottom: 30px;
        }
        .info-box {
            background: #001100;
            border: 1px solid #004400;
            padding: 15px;
            margin-bottom: 30px;
            font-size: 0.9em;
        }
        .info-box p { margin-bottom: 10px; }
        .form-group {
            margin-bottom: 20px;
        }
        label {
            display: block;
            margin-bottom: 5px;
            color: #00ff00;
        }
        input {
            width: 100%;
            padding: 10px;
            background: #001100;
            border: 1px solid #00ff00;
            color: #00ff00;
            font-family: 'Courier New', monospace;
            font-size: 1em;
        }
        input:focus {
            outline: none;
            border-color: #00ff88;
        }
        .btn {
            width: 100%;
            padding: 15px;
            background: #00ff00;
            color: #000;
            border: none;
            font-family: 'Courier New', monospace;
            font-size: 1em;
            cursor: pointer;
            font-weight: bold;
        }
        .btn:hover {
            background: #00cc00;
        }
        .error {
            background: #330000;
            border: 1px solid #ff0000;
            color: #ff0000;
            padding: 10px;
            margin-bottom: 20px;
        }
    </style>
</head>
<body>
    <div class="container">
        <h1>ALGORITHMIC TRADING PORTAL</h1>
        <div class="subtitle">Gold & Silver Basis Trading</div>

        <div class="info-box">
            <p>▸ Make sure MetaTrader5 is running and logged in</p>
            <p>▸ Enter your daily swap charges (cost to hold long overnight)</p>
            <p>▸ System will calculate fair value and generate signals</p>
        </div>

        {% if error %}
        <div class="error">{{ error }}</div>
        {% endif %}

        <form method="POST">
            <div class="form-group">
                <label>GOLD Swap Charge (USD per lot/day)</label>
                <label style="color: #006600; font-size: 0.8em;">1 lot = 100 oz</label>
                <input type="number" name="gold_swap" step="0.01" min="0" value="{{ config.gold_swap_charge or '' }}" required>
            </div>

            <div class="form-group">
                <label>SILVER Swap Charge (USD per lot/day)</label>
                <label style="color: #006600; font-size: 0.8em;">1 lot = 5,000 oz</label>
                <input type="number" name="silver_swap" step="0.01" min="0" value="{{ config.silver_swap_charge or '' }}" required>
            </div>

            <button type="submit" class="btn">START MONITORING</button>
        </form>
    </div>
</body>
</html>'''

# Main monitor page template
MONITOR_HTML = '''<!DOCTYPE html>
<html>
<head>
    <title>Trading Portal - Monitor</title>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }
        body {
            font-family: 'Courier New', monospace;
            background: #000;
            color: #00ff00;
            min-height: 100vh;
            padding: 20px;
        }
        .header {
            display: flex;
            justify-content: space-between;
            align-items: center;
            margin-bottom: 20px;
            padding-bottom: 10px;
            border-bottom: 1px solid #004400;
        }
        h1 { font-size: 1.3em; }
        .timestamp { color: #008800; }

        .controls {
            display: flex;
            gap: 20px;
            margin-bottom: 20px;
            padding: 15px;
            background: #001100;
            border: 1px solid #004400;
            flex-wrap: wrap;
        }
        .control-group {
            display: flex;
            align-items: center;
            gap: 10px;
        }
        .control-label { color: #00ff00; }
        .toggle {
            position: relative;
            width: 50px;
            height: 24px;
        }
        .toggle input { opacity: 0; width: 0; height: 0; }
        .toggle-slider {
            position: absolute;
            cursor: pointer;
            top: 0; left: 0; right: 0; bottom: 0;
            background: #333;
            border-radius: 24px;
            transition: 0.3s;
        }
        .toggle-slider:before {
            position: absolute;
            content: "";
            height: 18px;
            width: 18px;
            left: 3px;
            bottom: 3px;
            background: #888;
            border-radius: 50%;
            transition: 0.3s;
        }
        .toggle input:checked + .toggle-slider { background: #004400; }
        .toggle input:checked + .toggle-slider:before {
            transform: translateX(26px);
            background: #00ff00;
        }
        .toggle.danger input:checked + .toggle-slider { background: #440000; }
        .toggle.danger input:checked + .toggle-slider:before { background: #ff4444; }

        .status-badge {
            padding: 3px 10px;
            border-radius: 3px;
            font-size: 0.8em;
        }
        .status-badge.active { background: #004400; color: #00ff00; }
        .status-badge.inactive { background: #222; color: #666; }
        .status-badge.paper { background: #333300; color: #ffff00; }
        .status-badge.live { background: #440000; color: #ff4444; }

        .summary {
            display: flex;
            gap: 20px;
            margin-bottom: 20px;
        }
        .summary-item {
            padding: 15px 25px;
            border: 1px solid;
        }
        .summary-item.cheap { border-color: #00ff00; color: #00ff00; }
        .summary-item.fair { border-color: #ffff00; color: #ffff00; }
        .summary-item.expensive { border-color: #ff8800; color: #ff8800; }
        .summary-count { font-size: 2em; font-weight: bold; }
        .summary-label { font-size: 0.8em; }

        .assets { display: grid; grid-template-columns: repeat(auto-fit, minmax(500px, 1fr)); gap: 20px; }

        .asset-card {
            border: 1px solid #004400;
            padding: 20px;
        }
        .asset-header {
            display: flex;
            justify-content: space-between;
            align-items: center;
            margin-bottom: 15px;
            padding-bottom: 10px;
            border-bottom: 1px solid #004400;
        }
        .asset-name { font-size: 1.3em; font-weight: bold; }

        .price-grid {
            display: grid;
            grid-template-columns: repeat(4, 1fr);
            gap: 10px;
            margin-bottom: 15px;
        }
        .price-item {
            background: #001100;
            padding: 10px;
        }
        .price-label { color: #006600; font-size: 0.8em; }
        .price-value { font-size: 1.1em; }

        .basis-section {
            background: #001100;
            padding: 15px;
            margin-bottom: 15px;
        }
        .basis-row {
            display: flex;
            justify-content: space-between;
            padding: 5px 0;
        }

        .signal-section {
            background: #000;
            border: 2px solid;
            padding: 15px;
            text-align: center;
        }
        .signal-section.sell-basis { border-color: #ff4444; background: #110000; }
        .signal-section.buy-basis { border-color: #44ff44; background: #001100; }
        .signal-section.hold { border-color: #444; }
        .signal-section.no-data { border-color: #333; }

        .zscore-display {
            font-size: 2em;
            font-weight: bold;
            margin: 10px 0;
        }
        .signal-type { font-size: 1.2em; margin-bottom: 5px; }
        .signal-reason { color: #888; font-size: 0.9em; }

        .stats-row {
            display: flex;
            justify-content: center;
            gap: 30px;
            margin-top: 10px;
            font-size: 0.85em;
            color: #666;
        }

        .positions-section {
            margin-top: 20px;
            border: 1px solid #004400;
            padding: 20px;
        }
        .positions-title {
            margin-bottom: 15px;
            padding-bottom: 10px;
            border-bottom: 1px solid #004400;
        }
        .position-card {
            background: #001100;
            padding: 15px;
            margin-bottom: 10px;
            display: flex;
            justify-content: space-between;
            align-items: center;
        }

        .footer {
            margin-top: 20px;
            padding-top: 10px;
            border-top: 1px solid #004400;
            display: flex;
            justify-content: space-between;
            align-items: center;
        }
        .footer a {
            color: #ff4444;
            text-decoration: none;
            padding: 10px 20px;
            border: 1px solid #ff4444;
        }
        .footer a:hover { background: #220000; }

        .settings-link {
            color: #00ff00;
            text-decoration: none;
            padding: 10px 20px;
            border: 1px solid #00ff00;
        }
        .settings-link:hover { background: #002200; }

        @media (max-width: 600px) {
            .price-grid { grid-template-columns: repeat(2, 1fr); }
            .controls { flex-direction: column; }
            .summary { flex-direction: column; }
        }
    </style>
</head>
<body>
    <div class="header">
        <h1>ALGORITHMIC TRADING PORTAL</h1>
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
            <span style="color: #888;" id="thresholds">Entry: ±2.0σ | Exit: ±0.5σ | Stop: ±3.0σ</span>
        </div>
        <a href="/settings" class="settings-link">⚙ SETTINGS</a>
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
        <div class="positions-title">ACTIVE POSITIONS</div>
        <div id="positions-container">
            <div style="color: #666; text-align: center;">No active positions</div>
        </div>
    </div>

    <div class="footer">
        <div>Last update: <span id="last-update">-</span></div>
        <a href="/restart">⟲ RESTART</a>
    </div>

    <script>
        function updateData() {
            fetch('/api/data')
                .then(res => res.json())
                .then(data => {
                    if (data.error) return;

                    // Update timestamp
                    document.getElementById('timestamp').textContent = new Date().toLocaleTimeString();
                    document.getElementById('last-update').textContent = data.last_update;

                    // Update summary
                    document.getElementById('cheap-count').textContent = data.summary.cheap;
                    document.getElementById('fair-count').textContent = data.summary.fair;
                    document.getElementById('expensive-count').textContent = data.summary.expensive;

                    // Update config display
                    const cfg = data.config;
                    document.getElementById('algo-toggle').checked = cfg.algo_enabled;
                    document.getElementById('paper-toggle').checked = cfg.paper_mode;
                    document.getElementById('algo-status').textContent = cfg.algo_enabled ? 'ON' : 'OFF';
                    document.getElementById('algo-status').className = 'status-badge ' + (cfg.algo_enabled ? 'active' : 'inactive');
                    document.getElementById('mode-status').textContent = cfg.paper_mode ? 'PAPER' : 'LIVE';
                    document.getElementById('mode-status').className = 'status-badge ' + (cfg.paper_mode ? 'paper' : 'live');
                    document.getElementById('thresholds').textContent =
                        `Entry: ±${cfg.entry_std_dev}σ | Exit: ±${cfg.exit_std_dev}σ | Stop: ±${cfg.stop_loss_std_dev}σ`;

                    // Update assets
                    const container = document.getElementById('assets-container');
                    container.innerHTML = '';

                    for (const [key, asset] of Object.entries(data.data)) {
                        container.appendChild(createAssetCard(asset));
                    }

                    // Update positions
                    updatePositions(data.positions);
                })
                .catch(err => console.error('Error:', err));
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

            card.innerHTML = `
                <div class="asset-header">
                    <span class="asset-name">${asset.asset_name}</span>
                    <span class="status-badge ${asset.status_class}">${asset.status}</span>
                </div>

                <div class="price-grid">
                    <div class="price-item">
                        <div class="price-label">SPOT</div>
                        <div class="price-value">${asset.spot_price.toFixed(2)}</div>
                    </div>
                    <div class="price-item">
                        <div class="price-label">FUTURES</div>
                        <div class="price-value">${asset.futures_price.toFixed(2)}</div>
                    </div>
                    <div class="price-item">
                        <div class="price-label">SPOT SPREAD</div>
                        <div class="price-value">${asset.spot_spread.toFixed(1)}¢</div>
                    </div>
                    <div class="price-item">
                        <div class="price-label">FUT SPREAD</div>
                        <div class="price-value">${asset.futures_spread.toFixed(1)}¢</div>
                    </div>
                </div>

                <div class="basis-section">
                    <div class="basis-row">
                        <span>Actual Basis:</span>
                        <span>${asset.actual_basis.toFixed(2)}</span>
                    </div>
                    <div class="basis-row">
                        <span>Swap-Based Basis:</span>
                        <span>${asset.swap_basis.toFixed(2)}</span>
                    </div>
                    <div class="basis-row">
                        <span>Difference:</span>
                        <span style="color: ${asset.swap_diff > 0 ? '#ff8800' : '#00ff00'}">${asset.swap_diff > 0 ? '+' : ''}${asset.swap_diff.toFixed(2)}</span>
                    </div>
                    <div class="basis-row">
                        <span>Premium:</span>
                        <span>${asset.swap_premium_pct > 0 ? '+' : ''}${asset.swap_premium_pct.toFixed(1)}%</span>
                    </div>
                    <div class="basis-row">
                        <span>Days to Expiry:</span>
                        <span>${Math.round(asset.days_to_expiry)}</span>
                    </div>
                </div>

                <div class="signal-section ${signalClass}">
                    <div class="signal-type">${signal.type.replace('_', ' ')}</div>
                    <div class="zscore-display">${zscore}σ</div>
                    <div class="signal-reason">${signal.reason || ''}</div>
                    ${asset.stats ? `
                    <div class="stats-row">
                        <span>Mean: ${asset.stats.mean.toFixed(2)}</span>
                        <span>Std: ${asset.stats.std.toFixed(2)}</span>
                        <span>Points: ${asset.stats.count}</span>
                    </div>` : ''}
                </div>
            `;

            return card;
        }

        function updatePositions(positions) {
            const container = document.getElementById('positions-container');

            if (!positions || positions.length === 0) {
                container.innerHTML = '<div style="color: #666; text-align: center;">No active positions</div>';
                return;
            }

            container.innerHTML = positions.map(p => `
                <div class="position-card">
                    <div>
                        <strong>${p.asset}</strong> - ${p.signal_type}
                    </div>
                    <div>
                        Entry Z: ${p.entry_zscore ? p.entry_zscore.toFixed(2) : '--'}σ
                    </div>
                    <div style="color: #888;">
                        ${new Date(p.entry_time).toLocaleString()}
                    </div>
                </div>
            `).join('');
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

        // Initial load and refresh every 2 seconds
        updateData();
        setInterval(updateData, 2000);
    </script>
</body>
</html>'''

# Settings page template
SETTINGS_HTML = '''<!DOCTYPE html>
<html>
<head>
    <title>Trading Portal - Settings</title>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }
        body {
            font-family: 'Courier New', monospace;
            background: #000;
            color: #00ff00;
            min-height: 100vh;
            padding: 20px;
        }
        .container { max-width: 600px; margin: 0 auto; }
        h1 {
            text-align: center;
            margin-bottom: 30px;
            font-size: 1.5em;
        }
        .card {
            border: 1px solid #004400;
            padding: 20px;
            margin-bottom: 20px;
        }
        .card-title {
            margin-bottom: 20px;
            padding-bottom: 10px;
            border-bottom: 1px solid #004400;
        }
        .form-group {
            margin-bottom: 20px;
        }
        label {
            display: block;
            margin-bottom: 5px;
            color: #00ff00;
        }
        .help-text {
            color: #006600;
            font-size: 0.85em;
            margin-bottom: 5px;
        }
        input {
            width: 100%;
            padding: 10px;
            background: #001100;
            border: 1px solid #00ff00;
            color: #00ff00;
            font-family: 'Courier New', monospace;
            font-size: 1em;
        }
        input:focus {
            outline: none;
            border-color: #00ff88;
        }
        .btn {
            width: 100%;
            padding: 15px;
            background: #00ff00;
            color: #000;
            border: none;
            font-family: 'Courier New', monospace;
            font-size: 1em;
            cursor: pointer;
            font-weight: bold;
            margin-top: 10px;
        }
        .btn:hover { background: #00cc00; }
        .btn-secondary {
            background: transparent;
            color: #00ff00;
            border: 1px solid #00ff00;
        }
        .btn-secondary:hover { background: #002200; }
        .success {
            background: #002200;
            border: 1px solid #00ff00;
            color: #00ff00;
            padding: 10px;
            margin-bottom: 20px;
            text-align: center;
        }
        .error {
            background: #220000;
            border: 1px solid #ff0000;
            color: #ff0000;
            padding: 10px;
            margin-bottom: 20px;
        }
        a { color: #00ff00; }
    </style>
</head>
<body>
    <div class="container">
        <h1>⚙ TRADING SETTINGS</h1>

        {% if saved %}
        <div class="success">Settings saved successfully!</div>
        {% endif %}

        {% if error %}
        <div class="error">{{ error }}</div>
        {% endif %}

        <form method="POST">
            <div class="card">
                <div class="card-title">SIGNAL PARAMETERS</div>

                <div class="form-group">
                    <label>Lookback Period (data points)</label>
                    <div class="help-text">Number of data points for mean/std calculation</div>
                    <input type="number" name="lookback_period" value="{{ config.lookback_period }}" min="10" max="1000">
                </div>

                <div class="form-group">
                    <label>Entry Threshold (Standard Deviations)</label>
                    <div class="help-text">Z-score threshold to open position (e.g., 2.0 = ±2σ)</div>
                    <input type="number" name="entry_std_dev" value="{{ config.entry_std_dev }}" min="0.5" max="5" step="0.1">
                </div>

                <div class="form-group">
                    <label>Exit Threshold (Standard Deviations)</label>
                    <div class="help-text">Z-score threshold to close position (e.g., 0.5 = ±0.5σ)</div>
                    <input type="number" name="exit_std_dev" value="{{ config.exit_std_dev }}" min="0" max="2" step="0.1">
                </div>

                <div class="form-group">
                    <label>Stop Loss Threshold (Standard Deviations)</label>
                    <div class="help-text">Z-score threshold for stop loss (e.g., 3.0 = ±3σ)</div>
                    <input type="number" name="stop_loss_std_dev" value="{{ config.stop_loss_std_dev }}" min="2" max="6" step="0.1">
                </div>
            </div>

            <div class="card">
                <div class="card-title">POSITION SIZING</div>

                <div class="form-group">
                    <label>Max Positions per Asset</label>
                    <div class="help-text">Maximum concurrent positions allowed per asset</div>
                    <input type="number" name="max_positions" value="{{ config.max_positions }}" min="1" max="10">
                </div>

                <div class="form-group">
                    <label>Lot Size</label>
                    <div class="help-text">Size of each trade in lots</div>
                    <input type="number" name="lot_size" value="{{ config.lot_size }}" min="0.01" max="10" step="0.01">
                </div>
            </div>

            <button type="submit" class="btn">SAVE SETTINGS</button>
            <a href="/" class="btn btn-secondary" style="display: block; text-align: center; text-decoration: none; margin-top: 10px;">← BACK TO MONITOR</a>
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
