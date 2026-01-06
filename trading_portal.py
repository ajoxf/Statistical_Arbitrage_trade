# -*- coding: utf-8 -*-
"""
WEB-BASED ALGORITHMIC TRADING PORTAL
- Real-time monitoring with shareable web interface
- User-configurable trading parameters
- Algo trading toggle for non-technical users
- Persistent mean calculation (handles connectivity issues)
- MT5 integration for live trading
"""

import MetaTrader5 as mt5
import pandas as pd
import numpy as np
import sqlite3
import json
import math
import threading
import time
import logging
from datetime import datetime, timedelta
from flask import Flask, render_template, jsonify, request
from flask_socketio import SocketIO, emit
from collections import deque
import uuid
import os

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

app = Flask(__name__)
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'your-secret-key-change-in-production')
socketio = SocketIO(app, cors_allowed_origins="*", async_mode='threading')


# =============================================================================
# DATABASE MANAGER - Persistent storage for mean calculation & trades
# =============================================================================
class DatabaseManager:
    """Handles all database operations with connection pooling"""

    def __init__(self, db_path="trading_portal.db"):
        self.db_path = db_path
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
                basis_pct REAL,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
        ''')

        # Trading configuration
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS trading_config (
                id INTEGER PRIMARY KEY,
                lookback_period INTEGER DEFAULT 90,
                entry_std_dev REAL DEFAULT 2.0,
                exit_std_dev REAL DEFAULT 0.5,
                stop_loss_std_dev REAL DEFAULT 3.0,
                max_positions INTEGER DEFAULT 3,
                lot_size REAL DEFAULT 0.1,
                gold_swap_charge REAL DEFAULT 0.0,
                silver_swap_charge REAL DEFAULT 0.0,
                algo_trading_enabled INTEGER DEFAULT 0,
                paper_trading INTEGER DEFAULT 1,
                updated_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
        ''')

        # Trades table
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS trades (
                trade_id TEXT PRIMARY KEY,
                position_id TEXT,
                asset TEXT,
                direction TEXT,
                lot_size REAL,
                entry_price REAL,
                exit_price REAL,
                entry_time TEXT,
                exit_time TEXT,
                pnl REAL,
                status TEXT,
                signal_type TEXT
            )
        ''')

        # Positions table
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS positions (
                position_id TEXT PRIMARY KEY,
                asset TEXT,
                signal_type TEXT,
                entry_spread REAL,
                entry_zscore REAL,
                entry_time TEXT,
                current_spread REAL,
                current_zscore REAL,
                unrealized_pnl REAL,
                status TEXT
            )
        ''')

        # Insert default config if not exists
        cursor.execute('SELECT COUNT(*) FROM trading_config')
        if cursor.fetchone()[0] == 0:
            cursor.execute('''
                INSERT INTO trading_config (id, lookback_period, entry_std_dev, exit_std_dev,
                    stop_loss_std_dev, max_positions, lot_size, algo_trading_enabled, paper_trading)
                VALUES (1, 90, 2.0, 0.5, 3.0, 3, 0.1, 0, 1)
            ''')

        conn.commit()
        conn.close()
        logger.info("Database initialized")

    def save_price_data(self, asset, spot_price, futures_price, spread, basis_pct):
        """Save price data for mean calculation"""
        conn = self.get_connection()
        cursor = conn.cursor()
        cursor.execute('''
            INSERT INTO price_history (timestamp, asset, spot_price, futures_price, spread, basis_pct)
            VALUES (?, ?, ?, ?, ?, ?)
        ''', (datetime.now().isoformat(), asset, spot_price, futures_price, spread, basis_pct))
        conn.commit()
        conn.close()

    def get_price_history(self, asset, lookback_periods):
        """Get price history for mean calculation"""
        conn = self.get_connection()
        cursor = conn.cursor()
        cursor.execute('''
            SELECT timestamp, spread, basis_pct FROM price_history
            WHERE asset = ?
            ORDER BY timestamp DESC
            LIMIT ?
        ''', (asset, lookback_periods))
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
                'lookback_period': row[1],
                'entry_std_dev': row[2],
                'exit_std_dev': row[3],
                'stop_loss_std_dev': row[4],
                'max_positions': row[5],
                'lot_size': row[6],
                'gold_swap_charge': row[7],
                'silver_swap_charge': row[8],
                'algo_trading_enabled': bool(row[9]),
                'paper_trading': bool(row[10])
            }
        return None

    def update_config(self, config):
        """Update trading configuration"""
        conn = self.get_connection()
        cursor = conn.cursor()
        cursor.execute('''
            UPDATE trading_config SET
                lookback_period = ?,
                entry_std_dev = ?,
                exit_std_dev = ?,
                stop_loss_std_dev = ?,
                max_positions = ?,
                lot_size = ?,
                gold_swap_charge = ?,
                silver_swap_charge = ?,
                algo_trading_enabled = ?,
                paper_trading = ?,
                updated_at = ?
            WHERE id = 1
        ''', (
            config['lookback_period'],
            config['entry_std_dev'],
            config['exit_std_dev'],
            config['stop_loss_std_dev'],
            config['max_positions'],
            config['lot_size'],
            config['gold_swap_charge'],
            config['silver_swap_charge'],
            1 if config['algo_trading_enabled'] else 0,
            1 if config['paper_trading'] else 0,
            datetime.now().isoformat()
        ))
        conn.commit()
        conn.close()
        logger.info(f"Config updated: algo_trading={config['algo_trading_enabled']}")

    def save_trade(self, trade_data):
        """Save trade to database"""
        conn = self.get_connection()
        cursor = conn.cursor()
        cursor.execute('''
            INSERT OR REPLACE INTO trades VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', (
            trade_data['trade_id'], trade_data['position_id'], trade_data['asset'],
            trade_data['direction'], trade_data['lot_size'], trade_data['entry_price'],
            trade_data.get('exit_price'), trade_data['entry_time'],
            trade_data.get('exit_time'), trade_data.get('pnl', 0),
            trade_data['status'], trade_data['signal_type']
        ))
        conn.commit()
        conn.close()

    def get_recent_trades(self, limit=50):
        """Get recent trades"""
        conn = self.get_connection()
        cursor = conn.cursor()
        cursor.execute('''
            SELECT * FROM trades ORDER BY entry_time DESC LIMIT ?
        ''', (limit,))
        rows = cursor.fetchall()
        conn.close()

        trades = []
        for row in rows:
            trades.append({
                'trade_id': row[0], 'position_id': row[1], 'asset': row[2],
                'direction': row[3], 'lot_size': row[4], 'entry_price': row[5],
                'exit_price': row[6], 'entry_time': row[7], 'exit_time': row[8],
                'pnl': row[9], 'status': row[10], 'signal_type': row[11]
            })
        return trades


# =============================================================================
# MT5 CONNECTION MANAGER - Handles connection with auto-reconnect
# =============================================================================
class MT5Manager:
    """Manages MT5 connection with automatic reconnection"""

    def __init__(self):
        self.connected = False
        self.last_connection_attempt = None
        self.connection_lock = threading.Lock()
        self.symbols = {
            'GOLD': {
                'spot_symbols': ['XAUUSD_', 'XAUUSD', 'GOLD'],
                'futures_symbols': ['GC1225', 'XAUUSD.f', 'GCZ4'],
                'lot_size': 100,
                'futures_expiry': datetime(2025, 11, 26)
            },
            'SILVER': {
                'spot_symbols': ['XAGUSD_', 'XAGUSD', 'SILVER'],
                'futures_symbols': ['SI1225', 'XAGUSD.f', 'SIU4'],
                'lot_size': 5000,
                'futures_expiry': datetime(2025, 11, 26)
            }
        }
        self.active_symbols = {}

    def connect(self):
        """Connect to MT5 with retry logic"""
        with self.connection_lock:
            if self.connected:
                return True

            # Rate limit connection attempts
            if self.last_connection_attempt:
                elapsed = (datetime.now() - self.last_connection_attempt).total_seconds()
                if elapsed < 5:
                    return False

            self.last_connection_attempt = datetime.now()

            try:
                if not mt5.initialize():
                    logger.error(f"MT5 initialization failed: {mt5.last_error()}")
                    return False

                self.connected = True
                self._setup_symbols()
                logger.info("MT5 connected successfully")
                return True

            except Exception as e:
                logger.error(f"MT5 connection error: {e}")
                return False

    def _setup_symbols(self):
        """Find and setup available symbols"""
        for asset_key, asset_config in self.symbols.items():
            spot_symbol = None
            futures_symbol = None

            for symbol in asset_config['spot_symbols']:
                if mt5.symbol_info(symbol):
                    mt5.symbol_select(symbol, True)
                    spot_symbol = symbol
                    break

            for symbol in asset_config['futures_symbols']:
                if mt5.symbol_info(symbol):
                    mt5.symbol_select(symbol, True)
                    futures_symbol = symbol
                    break

            if spot_symbol and futures_symbol:
                self.active_symbols[asset_key] = {
                    'spot': spot_symbol,
                    'futures': futures_symbol,
                    'config': asset_config
                }
                logger.info(f"{asset_key}: {spot_symbol} + {futures_symbol}")

    def disconnect(self):
        """Disconnect from MT5"""
        with self.connection_lock:
            if self.connected:
                mt5.shutdown()
                self.connected = False
                logger.info("MT5 disconnected")

    def ensure_connection(self):
        """Ensure MT5 is connected, reconnect if needed"""
        if not self.connected:
            return self.connect()

        # Check if connection is still alive
        try:
            if not mt5.terminal_info():
                self.connected = False
                return self.connect()
            return True
        except:
            self.connected = False
            return self.connect()

    def get_tick(self, symbol):
        """Get current tick data"""
        if not self.ensure_connection():
            return None

        try:
            tick = mt5.symbol_info_tick(symbol)
            return tick
        except Exception as e:
            logger.error(f"Error getting tick for {symbol}: {e}")
            return None

    def execute_order(self, symbol, order_type, lot_size, comment=""):
        """Execute market order"""
        if not self.ensure_connection():
            return None, "MT5 not connected"

        try:
            symbol_info = mt5.symbol_info(symbol)
            if not symbol_info:
                return None, f"Symbol {symbol} not found"

            tick = mt5.symbol_info_tick(symbol)
            if not tick:
                return None, f"No tick data for {symbol}"

            price = tick.ask if order_type == mt5.ORDER_TYPE_BUY else tick.bid

            request = {
                "action": mt5.TRADE_ACTION_DEAL,
                "symbol": symbol,
                "volume": lot_size,
                "type": order_type,
                "price": price,
                "deviation": 10,
                "magic": 12345,
                "comment": comment,
                "type_time": mt5.ORDER_TIME_GTC,
                "type_filling": mt5.ORDER_FILLING_IOC,
            }

            result = mt5.order_send(request)

            if result.retcode != mt5.TRADE_RETCODE_DONE:
                return None, f"Order failed: {result.comment}"

            return result, None

        except Exception as e:
            return None, str(e)


# =============================================================================
# MEAN CALCULATOR - Persistent calculation with connectivity handling
# =============================================================================
class MeanCalculator:
    """Calculates rolling statistics with persistence"""

    def __init__(self, db_manager):
        self.db = db_manager
        self.cache = {}  # In-memory cache for fast access
        self.last_update = {}

    def update(self, asset, spread, basis_pct, spot_price, futures_price):
        """Update with new price data"""
        # Save to database for persistence
        self.db.save_price_data(asset, spot_price, futures_price, spread, basis_pct)

        # Update cache
        if asset not in self.cache:
            self.cache[asset] = deque(maxlen=500)

        self.cache[asset].append({
            'timestamp': datetime.now(),
            'spread': spread,
            'basis_pct': basis_pct
        })

        self.last_update[asset] = datetime.now()

    def get_statistics(self, asset, lookback_period):
        """Get rolling statistics"""
        # First try cache
        if asset in self.cache and len(self.cache[asset]) >= lookback_period:
            data = list(self.cache[asset])[-lookback_period:]
            spreads = [d['spread'] for d in data]
            basis_pcts = [d['basis_pct'] for d in data]
        else:
            # Fall back to database
            history = self.db.get_price_history(asset, lookback_period)
            if len(history) < 10:  # Need minimum data
                return None

            spreads = [row[1] for row in history]
            basis_pcts = [row[2] for row in history]

            # Rebuild cache from database
            if asset not in self.cache:
                self.cache[asset] = deque(maxlen=500)
            for row in reversed(history):
                self.cache[asset].append({
                    'timestamp': row[0],
                    'spread': row[1],
                    'basis_pct': row[2]
                })

        return {
            'mean_spread': np.mean(spreads),
            'std_spread': np.std(spreads),
            'mean_basis_pct': np.mean(basis_pcts),
            'std_basis_pct': np.std(basis_pcts),
            'data_points': len(spreads)
        }

    def calculate_zscore(self, asset, current_spread, lookback_period):
        """Calculate z-score for current spread"""
        stats = self.get_statistics(asset, lookback_period)
        if not stats or stats['std_spread'] == 0:
            return None, stats

        zscore = (current_spread - stats['mean_spread']) / stats['std_spread']
        return zscore, stats

    def get_data_health(self, asset):
        """Check data collection health"""
        last = self.last_update.get(asset)
        if not last:
            return {'status': 'NO_DATA', 'gap_seconds': None}

        gap = (datetime.now() - last).total_seconds()

        if gap < 5:
            status = 'HEALTHY'
        elif gap < 30:
            status = 'DELAYED'
        else:
            status = 'STALE'

        return {'status': status, 'gap_seconds': gap}


# =============================================================================
# SIGNAL GENERATOR - Trading signal generation
# =============================================================================
class SignalGenerator:
    """Generates trading signals based on z-score"""

    def __init__(self, config):
        self.config = config

    def generate(self, asset, zscore, current_positions):
        """Generate trading signal"""
        if zscore is None:
            return {'signal': 'NO_DATA', 'reason': 'Insufficient data'}

        entry_threshold = self.config['entry_std_dev']
        exit_threshold = self.config['exit_std_dev']
        stop_loss_threshold = self.config['stop_loss_std_dev']

        # Check for existing positions
        has_long = any(p['signal_type'] == 'SELL_BASIS' for p in current_positions)
        has_short = any(p['signal_type'] == 'BUY_BASIS' for p in current_positions)

        # Entry signals (only if no position)
        if not has_long and not has_short:
            if zscore > entry_threshold:
                return {
                    'signal': 'SELL_BASIS',
                    'reason': f'Z-score {zscore:.2f} > {entry_threshold}',
                    'action': 'Buy Spot + Sell Futures'
                }
            elif zscore < -entry_threshold:
                return {
                    'signal': 'BUY_BASIS',
                    'reason': f'Z-score {zscore:.2f} < -{entry_threshold}',
                    'action': 'Sell Spot + Buy Futures'
                }

        # Exit signals
        if has_long:  # SELL_BASIS position
            if zscore <= exit_threshold:
                return {
                    'signal': 'CLOSE_LONG',
                    'reason': f'Z-score {zscore:.2f} <= {exit_threshold}',
                    'action': 'Close SELL_BASIS position'
                }
            if zscore > stop_loss_threshold:
                return {
                    'signal': 'STOP_LOSS',
                    'reason': f'Z-score {zscore:.2f} > {stop_loss_threshold}',
                    'action': 'Stop loss triggered'
                }

        if has_short:  # BUY_BASIS position
            if zscore >= -exit_threshold:
                return {
                    'signal': 'CLOSE_SHORT',
                    'reason': f'Z-score {zscore:.2f} >= -{exit_threshold}',
                    'action': 'Close BUY_BASIS position'
                }
            if zscore < -stop_loss_threshold:
                return {
                    'signal': 'STOP_LOSS',
                    'reason': f'Z-score {zscore:.2f} < -{stop_loss_threshold}',
                    'action': 'Stop loss triggered'
                }

        return {'signal': 'HOLD', 'reason': 'No action required'}


# =============================================================================
# TRADING ENGINE - Main trading logic
# =============================================================================
class TradingEngine:
    """Main trading engine"""

    def __init__(self):
        self.db = DatabaseManager()
        self.mt5 = MT5Manager()
        self.mean_calc = MeanCalculator(self.db)
        self.config = self.db.get_config()
        self.signal_gen = SignalGenerator(self.config)

        self.running = False
        self.positions = {}
        self.last_prices = {}
        self.update_thread = None

    def start(self):
        """Start the trading engine"""
        if self.running:
            return

        self.running = True
        self.mt5.connect()
        self.update_thread = threading.Thread(target=self._update_loop, daemon=True)
        self.update_thread.start()
        logger.info("Trading engine started")

    def stop(self):
        """Stop the trading engine"""
        self.running = False
        if self.update_thread:
            self.update_thread.join(timeout=5)
        self.mt5.disconnect()
        logger.info("Trading engine stopped")

    def _update_loop(self):
        """Main update loop"""
        while self.running:
            try:
                self._update_cycle()
                time.sleep(0.5)  # 500ms update interval
            except Exception as e:
                logger.error(f"Update loop error: {e}")
                time.sleep(2)

    def _update_cycle(self):
        """Single update cycle"""
        # Reload config (allows live updates)
        self.config = self.db.get_config()
        self.signal_gen.config = self.config

        market_data = {}

        for asset_key, symbols in self.mt5.active_symbols.items():
            data = self._get_asset_data(asset_key, symbols)
            if data:
                market_data[asset_key] = data
                self.last_prices[asset_key] = data

                # Update mean calculator
                self.mean_calc.update(
                    asset_key,
                    data['spread'],
                    data['basis_pct'],
                    data['spot_price'],
                    data['futures_price']
                )

                # Generate signal
                zscore, stats = self.mean_calc.calculate_zscore(
                    asset_key,
                    data['spread'],
                    self.config['lookback_period']
                )

                data['zscore'] = zscore
                data['stats'] = stats
                data['health'] = self.mean_calc.get_data_health(asset_key)

                # Get positions for this asset
                asset_positions = [p for p in self.positions.values() if p['asset'] == asset_key]

                signal = self.signal_gen.generate(asset_key, zscore, asset_positions)
                data['signal'] = signal

                # Execute if algo trading enabled
                if self.config['algo_trading_enabled']:
                    self._process_signal(asset_key, signal, data)

        # Emit to connected clients
        socketio.emit('market_update', {
            'timestamp': datetime.now().isoformat(),
            'data': market_data,
            'config': self.config,
            'positions': list(self.positions.values())
        })

    def _get_asset_data(self, asset_key, symbols):
        """Get market data for an asset"""
        spot_tick = self.mt5.get_tick(symbols['spot'])
        futures_tick = self.mt5.get_tick(symbols['futures'])

        if not spot_tick or not futures_tick:
            return None

        spot_price = spot_tick.last if spot_tick.last > 0 else (spot_tick.bid + spot_tick.ask) / 2
        futures_price = futures_tick.last if futures_tick.last > 0 else (futures_tick.bid + futures_tick.ask) / 2

        spread = futures_price - spot_price
        basis_pct = (spread / spot_price) * 100 if spot_price > 0 else 0

        # Calculate days to expiry
        config = symbols['config']
        days_to_expiry = (config['futures_expiry'] - datetime.now()).days

        return {
            'asset': asset_key,
            'spot_symbol': symbols['spot'],
            'futures_symbol': symbols['futures'],
            'spot_price': spot_price,
            'spot_bid': spot_tick.bid,
            'spot_ask': spot_tick.ask,
            'futures_price': futures_price,
            'futures_bid': futures_tick.bid,
            'futures_ask': futures_tick.ask,
            'spread': spread,
            'basis_pct': basis_pct,
            'days_to_expiry': days_to_expiry,
            'timestamp': datetime.now().isoformat()
        }

    def _process_signal(self, asset_key, signal, data):
        """Process trading signal"""
        signal_type = signal['signal']

        if signal_type in ['SELL_BASIS', 'BUY_BASIS']:
            # Check position limits
            asset_positions = [p for p in self.positions.values() if p['asset'] == asset_key]
            if len(asset_positions) >= self.config['max_positions']:
                return

            self._open_position(asset_key, signal_type, data)

        elif signal_type in ['CLOSE_LONG', 'CLOSE_SHORT', 'STOP_LOSS']:
            self._close_positions(asset_key, signal_type)

    def _open_position(self, asset_key, signal_type, data):
        """Open a new position"""
        position_id = str(uuid.uuid4())[:8]
        symbols = self.mt5.active_symbols[asset_key]
        lot_size = self.config['lot_size']

        if self.config['paper_trading']:
            # Paper trade - just record
            logger.info(f"PAPER TRADE: {asset_key} {signal_type}")
        else:
            # Live trade
            if signal_type == 'SELL_BASIS':
                # Buy spot, sell futures
                spot_result, spot_err = self.mt5.execute_order(
                    symbols['spot'], mt5.ORDER_TYPE_BUY, lot_size, f"SPOT_{position_id}"
                )
                if spot_err:
                    logger.error(f"Spot order failed: {spot_err}")
                    return

                futures_result, futures_err = self.mt5.execute_order(
                    symbols['futures'], mt5.ORDER_TYPE_SELL, lot_size, f"FUT_{position_id}"
                )
                if futures_err:
                    logger.error(f"Futures order failed: {futures_err}")
                    # Reverse spot trade
                    self.mt5.execute_order(symbols['spot'], mt5.ORDER_TYPE_SELL, lot_size)
                    return
            else:  # BUY_BASIS
                # Sell spot, buy futures
                spot_result, spot_err = self.mt5.execute_order(
                    symbols['spot'], mt5.ORDER_TYPE_SELL, lot_size, f"SPOT_{position_id}"
                )
                if spot_err:
                    logger.error(f"Spot order failed: {spot_err}")
                    return

                futures_result, futures_err = self.mt5.execute_order(
                    symbols['futures'], mt5.ORDER_TYPE_BUY, lot_size, f"FUT_{position_id}"
                )
                if futures_err:
                    logger.error(f"Futures order failed: {futures_err}")
                    self.mt5.execute_order(symbols['spot'], mt5.ORDER_TYPE_BUY, lot_size)
                    return

        # Record position
        position = {
            'position_id': position_id,
            'asset': asset_key,
            'signal_type': signal_type,
            'entry_spread': data['spread'],
            'entry_zscore': data['zscore'],
            'entry_time': datetime.now().isoformat(),
            'lot_size': lot_size,
            'status': 'ACTIVE'
        }

        self.positions[position_id] = position
        logger.info(f"Position opened: {position_id} {asset_key} {signal_type}")

        # Save trade to DB
        self.db.save_trade({
            'trade_id': str(uuid.uuid4())[:8],
            'position_id': position_id,
            'asset': asset_key,
            'direction': signal_type,
            'lot_size': lot_size,
            'entry_price': data['spread'],
            'entry_time': datetime.now().isoformat(),
            'status': 'OPEN',
            'signal_type': signal_type
        })

    def _close_positions(self, asset_key, close_reason):
        """Close positions for an asset"""
        to_close = [pid for pid, p in self.positions.items()
                    if p['asset'] == asset_key and p['status'] == 'ACTIVE']

        for position_id in to_close:
            position = self.positions[position_id]
            symbols = self.mt5.active_symbols[asset_key]
            lot_size = position['lot_size']

            if not self.config['paper_trading']:
                # Execute closing trades
                if position['signal_type'] == 'SELL_BASIS':
                    self.mt5.execute_order(symbols['spot'], mt5.ORDER_TYPE_SELL, lot_size)
                    self.mt5.execute_order(symbols['futures'], mt5.ORDER_TYPE_BUY, lot_size)
                else:
                    self.mt5.execute_order(symbols['spot'], mt5.ORDER_TYPE_BUY, lot_size)
                    self.mt5.execute_order(symbols['futures'], mt5.ORDER_TYPE_SELL, lot_size)

            position['status'] = 'CLOSED'
            position['close_reason'] = close_reason
            position['close_time'] = datetime.now().isoformat()

            logger.info(f"Position closed: {position_id} - {close_reason}")

            # Remove from active positions
            del self.positions[position_id]

    def get_state(self):
        """Get current engine state"""
        return {
            'running': self.running,
            'connected': self.mt5.connected,
            'config': self.config,
            'positions': list(self.positions.values()),
            'last_prices': self.last_prices,
            'active_symbols': list(self.mt5.active_symbols.keys())
        }


# =============================================================================
# GLOBAL ENGINE INSTANCE
# =============================================================================
engine = TradingEngine()


# =============================================================================
# FLASK ROUTES
# =============================================================================
@app.route('/')
def index():
    """Main dashboard"""
    return render_template('index.html')


@app.route('/api/config', methods=['GET', 'POST'])
def config_endpoint():
    """Get or update configuration"""
    if request.method == 'GET':
        return jsonify(engine.db.get_config())
    else:
        config = request.json
        engine.db.update_config(config)
        engine.config = config
        return jsonify({'status': 'success'})


@app.route('/api/status')
def status():
    """Get system status"""
    return jsonify(engine.get_state())


@app.route('/api/trades')
def trades():
    """Get recent trades"""
    return jsonify(engine.db.get_recent_trades())


@app.route('/api/toggle_algo', methods=['POST'])
def toggle_algo():
    """Toggle algo trading on/off"""
    data = request.json
    enabled = data.get('enabled', False)

    config = engine.db.get_config()
    config['algo_trading_enabled'] = enabled
    engine.db.update_config(config)
    engine.config = config

    status = "ENABLED" if enabled else "DISABLED"
    logger.info(f"Algo trading {status}")

    return jsonify({'status': 'success', 'algo_trading_enabled': enabled})


@app.route('/api/toggle_paper', methods=['POST'])
def toggle_paper():
    """Toggle paper/live trading"""
    data = request.json
    paper = data.get('paper', True)

    config = engine.db.get_config()
    config['paper_trading'] = paper
    engine.db.update_config(config)
    engine.config = config

    mode = "PAPER" if paper else "LIVE"
    logger.info(f"Trading mode: {mode}")

    return jsonify({'status': 'success', 'paper_trading': paper})


# =============================================================================
# SOCKETIO EVENTS
# =============================================================================
@socketio.on('connect')
def handle_connect():
    """Handle client connection"""
    logger.info("Client connected")
    emit('status', engine.get_state())


@socketio.on('disconnect')
def handle_disconnect():
    """Handle client disconnection"""
    logger.info("Client disconnected")


@socketio.on('request_update')
def handle_update_request():
    """Handle manual update request"""
    emit('status', engine.get_state())


# =============================================================================
# HTML TEMPLATE (inline for single-file deployment)
# =============================================================================
TEMPLATE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'templates')
os.makedirs(TEMPLATE_DIR, exist_ok=True)

INDEX_HTML = '''<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Algorithmic Trading Portal</title>
    <script src="https://cdnjs.cloudflare.com/ajax/libs/socket.io/4.0.1/socket.io.js"></script>
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }
        body {
            font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif;
            background: linear-gradient(135deg, #1a1a2e 0%, #16213e 100%);
            color: #e0e0e0;
            min-height: 100vh;
        }
        .container { max-width: 1400px; margin: 0 auto; padding: 20px; }

        /* Header */
        .header {
            background: rgba(255,255,255,0.05);
            border-radius: 12px;
            padding: 20px;
            margin-bottom: 20px;
            display: flex;
            justify-content: space-between;
            align-items: center;
        }
        .header h1 { color: #00d4ff; font-size: 1.8em; }
        .status-indicator {
            display: flex;
            align-items: center;
            gap: 10px;
        }
        .status-dot {
            width: 12px; height: 12px;
            border-radius: 50%;
            animation: pulse 2s infinite;
        }
        .status-dot.connected { background: #00ff88; }
        .status-dot.disconnected { background: #ff4444; }
        @keyframes pulse {
            0%, 100% { opacity: 1; }
            50% { opacity: 0.5; }
        }

        /* Grid Layout */
        .grid { display: grid; grid-template-columns: 1fr 350px; gap: 20px; }
        .main-content { display: flex; flex-direction: column; gap: 20px; }

        /* Cards */
        .card {
            background: rgba(255,255,255,0.05);
            border-radius: 12px;
            padding: 20px;
            border: 1px solid rgba(255,255,255,0.1);
        }
        .card h2 {
            color: #00d4ff;
            font-size: 1.2em;
            margin-bottom: 15px;
            padding-bottom: 10px;
            border-bottom: 1px solid rgba(255,255,255,0.1);
        }

        /* Asset Cards */
        .asset-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(300px, 1fr)); gap: 20px; }
        .asset-card {
            background: rgba(0,212,255,0.05);
            border: 1px solid rgba(0,212,255,0.2);
            border-radius: 12px;
            padding: 20px;
        }
        .asset-card h3 {
            color: #ffd700;
            font-size: 1.3em;
            margin-bottom: 15px;
        }
        .price-row {
            display: flex;
            justify-content: space-between;
            padding: 8px 0;
            border-bottom: 1px solid rgba(255,255,255,0.05);
        }
        .price-label { color: #888; }
        .price-value { font-weight: bold; font-family: monospace; }
        .price-value.positive { color: #00ff88; }
        .price-value.negative { color: #ff4444; }

        /* Z-Score Display */
        .zscore-display {
            background: rgba(0,0,0,0.3);
            border-radius: 8px;
            padding: 15px;
            margin-top: 15px;
            text-align: center;
        }
        .zscore-value {
            font-size: 2.5em;
            font-weight: bold;
            font-family: monospace;
        }
        .zscore-label { color: #888; font-size: 0.9em; }

        /* Signal Badge */
        .signal-badge {
            display: inline-block;
            padding: 5px 15px;
            border-radius: 20px;
            font-weight: bold;
            font-size: 0.9em;
            margin-top: 10px;
        }
        .signal-badge.sell-basis { background: #ff6b6b; color: white; }
        .signal-badge.buy-basis { background: #4ecdc4; color: white; }
        .signal-badge.hold { background: #666; color: white; }
        .signal-badge.no-data { background: #444; color: #888; }

        /* Config Panel */
        .config-panel { position: sticky; top: 20px; }
        .config-group { margin-bottom: 20px; }
        .config-group label {
            display: block;
            color: #888;
            font-size: 0.9em;
            margin-bottom: 5px;
        }
        .config-group input, .config-group select {
            width: 100%;
            padding: 10px;
            border: 1px solid rgba(255,255,255,0.2);
            border-radius: 8px;
            background: rgba(0,0,0,0.3);
            color: white;
            font-size: 1em;
        }
        .config-group input:focus {
            outline: none;
            border-color: #00d4ff;
        }

        /* Toggle Switches */
        .toggle-container {
            display: flex;
            align-items: center;
            justify-content: space-between;
            padding: 15px;
            background: rgba(0,0,0,0.3);
            border-radius: 8px;
            margin-bottom: 15px;
        }
        .toggle-label { font-weight: bold; }
        .toggle-switch {
            position: relative;
            width: 60px;
            height: 30px;
        }
        .toggle-switch input {
            opacity: 0;
            width: 0;
            height: 0;
        }
        .toggle-slider {
            position: absolute;
            cursor: pointer;
            top: 0; left: 0; right: 0; bottom: 0;
            background: #444;
            border-radius: 30px;
            transition: 0.3s;
        }
        .toggle-slider:before {
            position: absolute;
            content: "";
            height: 22px;
            width: 22px;
            left: 4px;
            bottom: 4px;
            background: white;
            border-radius: 50%;
            transition: 0.3s;
        }
        .toggle-switch input:checked + .toggle-slider {
            background: #00d4ff;
        }
        .toggle-switch input:checked + .toggle-slider:before {
            transform: translateX(30px);
        }
        .toggle-switch.danger input:checked + .toggle-slider {
            background: #ff4444;
        }

        /* Buttons */
        .btn {
            padding: 12px 24px;
            border: none;
            border-radius: 8px;
            font-size: 1em;
            cursor: pointer;
            transition: all 0.3s;
            width: 100%;
            margin-top: 10px;
        }
        .btn-primary {
            background: #00d4ff;
            color: #1a1a2e;
        }
        .btn-primary:hover {
            background: #00a8cc;
        }
        .btn-danger {
            background: #ff4444;
            color: white;
        }
        .btn-danger:hover {
            background: #cc3333;
        }

        /* Positions Table */
        .positions-table {
            width: 100%;
            border-collapse: collapse;
        }
        .positions-table th, .positions-table td {
            padding: 12px;
            text-align: left;
            border-bottom: 1px solid rgba(255,255,255,0.1);
        }
        .positions-table th {
            color: #888;
            font-weight: normal;
            font-size: 0.9em;
        }

        /* Stats Grid */
        .stats-grid {
            display: grid;
            grid-template-columns: repeat(3, 1fr);
            gap: 10px;
            margin-top: 15px;
        }
        .stat-item {
            background: rgba(0,0,0,0.3);
            padding: 10px;
            border-radius: 8px;
            text-align: center;
        }
        .stat-value { font-size: 1.2em; font-weight: bold; }
        .stat-label { color: #888; font-size: 0.8em; }

        /* Health Indicator */
        .health-indicator {
            display: flex;
            align-items: center;
            gap: 5px;
            font-size: 0.8em;
        }
        .health-dot {
            width: 8px; height: 8px;
            border-radius: 50%;
        }
        .health-dot.healthy { background: #00ff88; }
        .health-dot.delayed { background: #ffd700; }
        .health-dot.stale { background: #ff4444; }

        /* Responsive */
        @media (max-width: 900px) {
            .grid { grid-template-columns: 1fr; }
            .config-panel { position: static; }
        }
    </style>
</head>
<body>
    <div class="container">
        <div class="header">
            <div>
                <h1>Algorithmic Trading Portal</h1>
                <div style="color: #888; margin-top: 5px;">Gold & Silver Basis Trading</div>
            </div>
            <div class="status-indicator">
                <span id="connection-status">Connecting...</span>
                <div class="status-dot disconnected" id="status-dot"></div>
            </div>
        </div>

        <div class="grid">
            <div class="main-content">
                <!-- Asset Cards -->
                <div class="asset-grid" id="asset-cards">
                    <!-- Populated by JavaScript -->
                </div>

                <!-- Active Positions -->
                <div class="card">
                    <h2>Active Positions</h2>
                    <table class="positions-table">
                        <thead>
                            <tr>
                                <th>Asset</th>
                                <th>Type</th>
                                <th>Entry Z-Score</th>
                                <th>Current Z-Score</th>
                                <th>Entry Time</th>
                                <th>Status</th>
                            </tr>
                        </thead>
                        <tbody id="positions-body">
                            <tr><td colspan="6" style="text-align: center; color: #666;">No active positions</td></tr>
                        </tbody>
                    </table>
                </div>

                <!-- Recent Trades -->
                <div class="card">
                    <h2>Recent Trades</h2>
                    <div id="recent-trades">
                        <p style="color: #666; text-align: center;">Loading...</p>
                    </div>
                </div>
            </div>

            <!-- Config Panel -->
            <div class="config-panel">
                <div class="card">
                    <h2>Trading Configuration</h2>

                    <!-- Algo Trading Toggle -->
                    <div class="toggle-container">
                        <span class="toggle-label">Algo Trading</span>
                        <label class="toggle-switch danger">
                            <input type="checkbox" id="algo-toggle">
                            <span class="toggle-slider"></span>
                        </label>
                    </div>

                    <!-- Paper Trading Toggle -->
                    <div class="toggle-container">
                        <span class="toggle-label">Paper Trading</span>
                        <label class="toggle-switch">
                            <input type="checkbox" id="paper-toggle" checked>
                            <span class="toggle-slider"></span>
                        </label>
                    </div>

                    <div class="config-group">
                        <label>Lookback Period (days)</label>
                        <input type="number" id="lookback" value="90" min="10" max="500">
                    </div>

                    <div class="config-group">
                        <label>Entry Threshold (Std Dev)</label>
                        <input type="number" id="entry-std" value="2.0" min="0.5" max="5" step="0.1">
                    </div>

                    <div class="config-group">
                        <label>Exit Threshold (Std Dev)</label>
                        <input type="number" id="exit-std" value="0.5" min="0" max="2" step="0.1">
                    </div>

                    <div class="config-group">
                        <label>Stop Loss (Std Dev)</label>
                        <input type="number" id="stop-loss-std" value="3.0" min="2" max="6" step="0.1">
                    </div>

                    <div class="config-group">
                        <label>Max Positions per Asset</label>
                        <input type="number" id="max-positions" value="3" min="1" max="10">
                    </div>

                    <div class="config-group">
                        <label>Lot Size</label>
                        <input type="number" id="lot-size" value="0.1" min="0.01" max="10" step="0.01">
                    </div>

                    <div class="config-group">
                        <label>Gold Swap Charge ($/day)</label>
                        <input type="number" id="gold-swap" value="0" min="0" step="0.01">
                    </div>

                    <div class="config-group">
                        <label>Silver Swap Charge ($/day)</label>
                        <input type="number" id="silver-swap" value="0" min="0" step="0.01">
                    </div>

                    <button class="btn btn-primary" onclick="saveConfig()">Save Configuration</button>
                </div>

                <div class="card" style="margin-top: 20px;">
                    <h2>System Status</h2>
                    <div class="stats-grid">
                        <div class="stat-item">
                            <div class="stat-value" id="stat-connected">--</div>
                            <div class="stat-label">MT5</div>
                        </div>
                        <div class="stat-item">
                            <div class="stat-value" id="stat-positions">0</div>
                            <div class="stat-label">Positions</div>
                        </div>
                        <div class="stat-item">
                            <div class="stat-value" id="stat-mode">PAPER</div>
                            <div class="stat-label">Mode</div>
                        </div>
                    </div>
                </div>
            </div>
        </div>
    </div>

    <script>
        const socket = io();
        let config = {};

        socket.on('connect', () => {
            document.getElementById('connection-status').textContent = 'Connected';
            document.getElementById('status-dot').classList.remove('disconnected');
            document.getElementById('status-dot').classList.add('connected');
        });

        socket.on('disconnect', () => {
            document.getElementById('connection-status').textContent = 'Disconnected';
            document.getElementById('status-dot').classList.remove('connected');
            document.getElementById('status-dot').classList.add('disconnected');
        });

        socket.on('status', (data) => {
            updateStatus(data);
        });

        socket.on('market_update', (data) => {
            updateMarketData(data);
        });

        function updateStatus(data) {
            document.getElementById('stat-connected').textContent = data.connected ? 'OK' : 'OFF';
            document.getElementById('stat-positions').textContent = data.positions.length;
            document.getElementById('stat-mode').textContent = data.config.paper_trading ? 'PAPER' : 'LIVE';

            // Update config form
            config = data.config;
            document.getElementById('algo-toggle').checked = config.algo_trading_enabled;
            document.getElementById('paper-toggle').checked = config.paper_trading;
            document.getElementById('lookback').value = config.lookback_period;
            document.getElementById('entry-std').value = config.entry_std_dev;
            document.getElementById('exit-std').value = config.exit_std_dev;
            document.getElementById('stop-loss-std').value = config.stop_loss_std_dev;
            document.getElementById('max-positions').value = config.max_positions;
            document.getElementById('lot-size').value = config.lot_size;
            document.getElementById('gold-swap').value = config.gold_swap_charge;
            document.getElementById('silver-swap').value = config.silver_swap_charge;
        }

        function updateMarketData(data) {
            const container = document.getElementById('asset-cards');
            container.innerHTML = '';

            for (const [asset, info] of Object.entries(data.data)) {
                const card = createAssetCard(asset, info);
                container.appendChild(card);
            }

            updatePositions(data.positions);
            updateStatus({
                connected: true,
                positions: data.positions,
                config: data.config
            });
        }

        function createAssetCard(asset, data) {
            const card = document.createElement('div');
            card.className = 'asset-card';

            const zscore = data.zscore !== null ? data.zscore.toFixed(2) : '--';
            const zscoreClass = data.zscore > 0 ? 'positive' : data.zscore < 0 ? 'negative' : '';

            let signalClass = 'hold';
            let signalText = 'HOLD';
            if (data.signal) {
                if (data.signal.signal === 'SELL_BASIS') {
                    signalClass = 'sell-basis';
                    signalText = 'SELL BASIS';
                } else if (data.signal.signal === 'BUY_BASIS') {
                    signalClass = 'buy-basis';
                    signalText = 'BUY BASIS';
                } else if (data.signal.signal === 'NO_DATA') {
                    signalClass = 'no-data';
                    signalText = 'NO DATA';
                }
            }

            const healthClass = data.health ? data.health.status.toLowerCase() : 'stale';

            card.innerHTML = `
                <h3>${asset}</h3>
                <div class="health-indicator">
                    <div class="health-dot ${healthClass}"></div>
                    <span>${data.health ? data.health.status : 'UNKNOWN'}</span>
                </div>
                <div class="price-row">
                    <span class="price-label">Spot (${data.spot_symbol})</span>
                    <span class="price-value">${data.spot_price.toFixed(2)}</span>
                </div>
                <div class="price-row">
                    <span class="price-label">Futures (${data.futures_symbol})</span>
                    <span class="price-value">${data.futures_price.toFixed(2)}</span>
                </div>
                <div class="price-row">
                    <span class="price-label">Spread</span>
                    <span class="price-value ${data.spread > 0 ? 'positive' : 'negative'}">${data.spread.toFixed(2)}</span>
                </div>
                <div class="price-row">
                    <span class="price-label">Basis %</span>
                    <span class="price-value">${data.basis_pct.toFixed(3)}%</span>
                </div>
                <div class="price-row">
                    <span class="price-label">Days to Expiry</span>
                    <span class="price-value">${data.days_to_expiry}</span>
                </div>
                ${data.stats ? `
                <div class="stats-grid" style="margin-top: 10px;">
                    <div class="stat-item">
                        <div class="stat-value">${data.stats.mean_spread.toFixed(2)}</div>
                        <div class="stat-label">Mean</div>
                    </div>
                    <div class="stat-item">
                        <div class="stat-value">${data.stats.std_spread.toFixed(2)}</div>
                        <div class="stat-label">Std Dev</div>
                    </div>
                    <div class="stat-item">
                        <div class="stat-value">${data.stats.data_points}</div>
                        <div class="stat-label">Points</div>
                    </div>
                </div>
                ` : ''}
                <div class="zscore-display">
                    <div class="zscore-label">Z-Score</div>
                    <div class="zscore-value ${zscoreClass}">${zscore}</div>
                </div>
                <div style="text-align: center;">
                    <span class="signal-badge ${signalClass}">${signalText}</span>
                </div>
            `;

            return card;
        }

        function updatePositions(positions) {
            const tbody = document.getElementById('positions-body');

            if (!positions || positions.length === 0) {
                tbody.innerHTML = '<tr><td colspan="6" style="text-align: center; color: #666;">No active positions</td></tr>';
                return;
            }

            tbody.innerHTML = positions.map(p => `
                <tr>
                    <td>${p.asset}</td>
                    <td><span class="signal-badge ${p.signal_type === 'SELL_BASIS' ? 'sell-basis' : 'buy-basis'}">${p.signal_type}</span></td>
                    <td>${p.entry_zscore ? p.entry_zscore.toFixed(2) : '--'}</td>
                    <td>${p.current_zscore ? p.current_zscore.toFixed(2) : '--'}</td>
                    <td>${new Date(p.entry_time).toLocaleString()}</td>
                    <td>${p.status}</td>
                </tr>
            `).join('');
        }

        function saveConfig() {
            const newConfig = {
                lookback_period: parseInt(document.getElementById('lookback').value),
                entry_std_dev: parseFloat(document.getElementById('entry-std').value),
                exit_std_dev: parseFloat(document.getElementById('exit-std').value),
                stop_loss_std_dev: parseFloat(document.getElementById('stop-loss-std').value),
                max_positions: parseInt(document.getElementById('max-positions').value),
                lot_size: parseFloat(document.getElementById('lot-size').value),
                gold_swap_charge: parseFloat(document.getElementById('gold-swap').value),
                silver_swap_charge: parseFloat(document.getElementById('silver-swap').value),
                algo_trading_enabled: document.getElementById('algo-toggle').checked,
                paper_trading: document.getElementById('paper-toggle').checked
            };

            fetch('/api/config', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(newConfig)
            })
            .then(res => res.json())
            .then(data => {
                alert('Configuration saved!');
            })
            .catch(err => {
                alert('Error saving configuration: ' + err);
            });
        }

        // Toggle handlers
        document.getElementById('algo-toggle').addEventListener('change', function() {
            if (this.checked) {
                if (!confirm('Are you sure you want to enable algorithmic trading? This will automatically execute trades based on signals.')) {
                    this.checked = false;
                    return;
                }
            }

            fetch('/api/toggle_algo', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ enabled: this.checked })
            });
        });

        document.getElementById('paper-toggle').addEventListener('change', function() {
            if (!this.checked) {
                if (!confirm('WARNING: You are switching to LIVE trading mode. Real money will be at risk. Are you sure?')) {
                    this.checked = true;
                    return;
                }
            }

            fetch('/api/toggle_paper', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ paper: this.checked })
            });
        });

        // Load trades
        fetch('/api/trades')
            .then(res => res.json())
            .then(trades => {
                const container = document.getElementById('recent-trades');
                if (trades.length === 0) {
                    container.innerHTML = '<p style="color: #666; text-align: center;">No trades yet</p>';
                    return;
                }

                container.innerHTML = `
                    <table class="positions-table">
                        <thead>
                            <tr>
                                <th>Time</th>
                                <th>Asset</th>
                                <th>Type</th>
                                <th>Status</th>
                                <th>P&L</th>
                            </tr>
                        </thead>
                        <tbody>
                            ${trades.slice(0, 10).map(t => `
                                <tr>
                                    <td>${new Date(t.entry_time).toLocaleString()}</td>
                                    <td>${t.asset}</td>
                                    <td>${t.signal_type}</td>
                                    <td>${t.status}</td>
                                    <td class="${t.pnl >= 0 ? 'positive' : 'negative'}">${t.pnl ? '$' + t.pnl.toFixed(2) : '--'}</td>
                                </tr>
                            `).join('')}
                        </tbody>
                    </table>
                `;
            });
    </script>
</body>
</html>
'''

# Write template file
with open(os.path.join(TEMPLATE_DIR, 'index.html'), 'w', encoding='utf-8') as f:
    f.write(INDEX_HTML)


# =============================================================================
# MAIN ENTRY POINT
# =============================================================================
def main():
    """Main entry point"""
    print("=" * 70)
    print("ALGORITHMIC TRADING PORTAL")
    print("=" * 70)
    print("Starting web server...")
    print()
    print("Access the portal at: http://localhost:5000")
    print("Share this URL with others on your network: http://<your-ip>:5000")
    print()
    print("Features:")
    print("  - Real-time price monitoring")
    print("  - Configurable trading parameters")
    print("  - Algo trading toggle (ON/OFF)")
    print("  - Paper/Live trading modes")
    print("  - Persistent mean calculation")
    print()
    print("Press Ctrl+C to stop")
    print("=" * 70)

    # Start trading engine
    engine.start()

    try:
        # Run Flask app
        socketio.run(app, host='0.0.0.0', port=5000, debug=False)
    except KeyboardInterrupt:
        print("\nShutting down...")
    finally:
        engine.stop()


if __name__ == "__main__":
    main()
