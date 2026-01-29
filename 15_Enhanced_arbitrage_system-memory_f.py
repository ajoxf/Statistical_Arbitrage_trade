# -*- coding: utf-8 -*-
"""
PRODUCTION ALGORITHMIC BASIS TRADING SYSTEM - GOLD & SILVER
- Automated basis trading using swap-cost analysis
- Real-time signal generation and execution
- Comprehensive risk management and position tracking
- Built on proven monitoring framework
- Single executable production system
"""

import MetaTrader5 as mt5
import pandas as pd
import numpy as np
import time
import sys
import sqlite3
import json
from datetime import datetime, timedelta, time as dt_time
from enum import Enum
import pytz
import logging
import math
import threading
from collections import deque
import uuid

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('algo_trading_system.log', encoding='utf-8'),
        logging.StreamHandler()
    ]
)

class TradingSession(Enum):
    ASIAN_PRE = "ASIAN_PRE"
    CHINA_OPEN = "CHINA_OPEN"
    ASIAN_LATE = "ASIAN_LATE" 
    LONDON_OPEN = "LONDON_OPEN"
    EUROPEAN = "EUROPEAN"
    US_OPEN = "US_OPEN"
    US_AFTERNOON = "US_AFTERNOON"
    AFTER_HOURS = "AFTER_HOURS"

class SignalType(Enum):
    NO_SIGNAL = "NO_SIGNAL"
    SELL_BASIS = "SELL_BASIS"  # Buy spot, sell futures (premium > 20%)
    BUY_BASIS = "BUY_BASIS"    # Buy futures, sell spot (discount < -15%)
    CLOSE_LONG = "CLOSE_LONG"  # Close sell basis position
    CLOSE_SHORT = "CLOSE_SHORT" # Close buy basis position

class PositionStatus(Enum):
    ACTIVE = "ACTIVE"
    CLOSING = "CLOSING"
    CLOSED = "CLOSED"
    ERROR = "ERROR"

class AlgoTradingConfig:
    """Configuration for algorithmic trading system"""
    
    SIGNAL_THRESHOLDS = {
        'PREMIUM_ENTRY': 20.0,    # % premium to enter sell basis
        'PREMIUM_EXIT': 5.0,      # % premium to exit sell basis
        'DISCOUNT_ENTRY': -15.0,  # % discount to enter buy basis
        'DISCOUNT_EXIT': -5.0     # % discount to exit buy basis
    }
    
    RISK_LIMITS = {
        'MAX_POSITIONS_PER_ASSET': 3,
        'MAX_LOT_SIZE': 1.0,
        'MAX_DAILY_TRADES': 20,
        'STOP_LOSS_PCT': 5.0,
        'MAX_EXPOSURE_USD': 100000
    }
    
    EXECUTION = {
        'SLIPPAGE_TOLERANCE': 1.0,    # points
        'ORDER_TIMEOUT': 30,          # seconds
        'MIN_TIME_BETWEEN_SIGNALS': 180,  # seconds
        'RETRY_ATTEMPTS': 3,
        'USE_LIMIT_ORDERS_FOR_EXIT': True,  # Use limit orders for exits to save bid-ask spread
        'LIMIT_ORDER_PRICE_BUFFER_PCT': 0.01,  # Buffer % for limit order prices (0.01 = 1 cent per $100)
        'LIMIT_ORDER_UPDATE_THRESHOLD': 0.5,  # Update limit orders if target price changes by this %
    }
    
    ASSETS = {
        'GOLD': {
            'name': 'GOLD',
            'spot_symbols': ['XAUUSD_', 'XAUUSD', 'GOLD'],
            'futures_symbols': ['GC1225', 'XAUUSD.f', 'GCZ4'],
            'futures_expiry': datetime(2025, 11, 26),
            'risk_free_rate': 0.0425,
            'multiplier': 1.0,
            'lot_size': 100,  # oz per lot
            'swap_charge': 0.0,  # Will be set by user
            'enabled': True
        },
        'SILVER': {
            'name': 'SILVER',
            'spot_symbols': ['XAGUSD_', 'XAGUSD', 'SILVER'],
            'futures_symbols': ['SI1225', 'XAGUSD.f', 'SIU4'],
            'futures_expiry': datetime(2025, 11, 26),
            'risk_free_rate': 0.0425,
            'multiplier': 1.0,
            'lot_size': 5000,  # oz per lot
            'swap_charge': 0.0,  # Will be set by user
            'enabled': True
        }
    }

class OrderType(Enum):
    """Order execution type"""
    MARKET = "MARKET"
    LIMIT = "LIMIT"

class LimitOrder:
    """Represents a pending limit order for position exit"""
    def __init__(self, order_id, symbol, order_type, lot_size, limit_price, position_id):
        self.order_id = order_id
        self.symbol = symbol
        self.order_type = order_type  # mt5.ORDER_TYPE_BUY_LIMIT or mt5.ORDER_TYPE_SELL_LIMIT
        self.lot_size = lot_size
        self.limit_price = limit_price
        self.position_id = position_id
        self.mt5_ticket = None
        self.status = "PENDING"  # PENDING, PLACED, FILLED, CANCELLED, ERROR
        self.created_time = datetime.now()
        self.last_modified = datetime.now()
        self.fill_price = None
        self.fill_time = None
        self.error_message = None

class Trade:
    """Represents a single trade"""
    def __init__(self, symbol, order_type, lot_size, price=None):
        self.trade_id = str(uuid.uuid4())[:8]
        self.symbol = symbol
        self.order_type = order_type  # mt5.ORDER_TYPE_BUY or mt5.ORDER_TYPE_SELL
        self.lot_size = lot_size
        self.requested_price = price
        self.executed_price = None
        self.order_ticket = None
        self.status = "PENDING"
        self.timestamp = datetime.now()
        self.execution_time = None
        self.error_message = None

class Position:
    """Represents a basis trading position (spot + futures pair)"""
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
        # Limit order tracking for exits
        self.spot_limit_order = None  # LimitOrder for spot exit
        self.futures_limit_order = None  # LimitOrder for futures exit
        self.target_spot_exit_price = None
        self.target_futures_exit_price = None
        self.exit_order_type = OrderType.LIMIT  # Default to limit orders for exits

class DataLogger:
    """Database logger for trades and market data"""
    
    def __init__(self, db_path="algo_trading.db"):
        self.db_path = db_path
        self.init_database()
    
    def init_database(self):
        """Initialize SQLite database"""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        
        # Trades table
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
        
        # Positions table
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
        
        # Market data table
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
        """Log trade to database"""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        
        cursor.execute('''
            INSERT OR REPLACE INTO trades VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', (
            trade.trade_id, position_id, trade.symbol, str(trade.order_type),
            trade.lot_size, trade.requested_price, trade.executed_price,
            trade.order_ticket, trade.status, trade.timestamp.isoformat(),
            trade.execution_time.isoformat() if trade.execution_time else None,
            trade.error_message
        ))
        
        conn.commit()
        conn.close()
    
    def log_position(self, position):
        """Log position to database"""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        
        cursor.execute('''
            INSERT OR REPLACE INTO positions VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', (
            position.position_id, position.asset, position.signal_type.value,
            position.entry_time.isoformat(),
            position.entry_premium,
            position.close_time.isoformat() if position.close_time else None,
            position.close_reason, position.unrealized_pnl, position.realized_pnl,
            position.status.value
        ))
        
        conn.commit()
        conn.close()
    
    def log_market_data(self, asset, market_data, signal):
        """Log market data snapshot"""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        
        cursor.execute('''
            INSERT INTO market_data VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ''', (
            datetime.now().isoformat(), asset, market_data['spot_price'],
            market_data['futures_price'], market_data['actual_basis'],
            market_data['swap_basis'], market_data['swap_premium_pct'],
            signal.value
        ))
        
        conn.commit()
        conn.close()

class OrderManager:
    """Handles MT5 order execution"""
    
    def __init__(self, config):
        self.config = config
        self.pending_orders = {}
    
    def execute_market_order(self, trade):
        """Execute market order"""
        try:
            # Prepare request
            symbol_info = mt5.symbol_info(trade.symbol)
            if not symbol_info:
                trade.status = "ERROR"
                trade.error_message = f"Symbol {trade.symbol} not found"
                return False
            
            if not symbol_info.visible:
                mt5.symbol_select(trade.symbol, True)
            
            point = symbol_info.point
            
            # Get current price
            tick = mt5.symbol_info_tick(trade.symbol)
            if not tick:
                trade.status = "ERROR"
                trade.error_message = f"No tick data for {trade.symbol}"
                return False
            
            price = tick.ask if trade.order_type == mt5.ORDER_TYPE_BUY else tick.bid
            trade.requested_price = price
            
            # Calculate deviation
            deviation = int(self.config.EXECUTION['SLIPPAGE_TOLERANCE'] / point)
            
            # Prepare order request
            request = {
                "action": mt5.TRADE_ACTION_DEAL,
                "symbol": trade.symbol,
                "volume": trade.lot_size,
                "type": trade.order_type,
                "price": price,
                "deviation": deviation,
                "magic": 12345,
                "comment": f"AlgoTrading_{trade.trade_id}",
                "type_time": mt5.ORDER_TIME_GTC,
                "type_filling": mt5.ORDER_FILLING_IOC,
            }
            
            # Send order
            result = mt5.order_send(request)
            
            if result.retcode != mt5.TRADE_RETCODE_DONE:
                trade.status = "ERROR"
                trade.error_message = f"Order failed: {result.retcode} - {result.comment}"
                logging.error(f"Order execution failed: {trade.symbol} - {result.comment}")
                return False
            
            # Update trade with execution details
            trade.order_ticket = result.order
            trade.executed_price = result.price
            trade.status = "EXECUTED"
            trade.execution_time = datetime.now()
            
            logging.info(f"Order executed: {trade.symbol} {trade.lot_size} lots at {trade.executed_price}")
            return True
            
        except Exception as e:
            trade.status = "ERROR"
            trade.error_message = f"Execution error: {str(e)}"
            logging.error(f"Order execution exception: {e}")
            return False
    
    def execute_trade_pair(self, asset, signal_type, lot_size, spot_symbol, futures_symbol):
        """Execute simultaneous spot and futures trades"""
        spot_trade = None
        futures_trade = None
        
        try:
            # Determine trade directions based on signal
            if signal_type == SignalType.SELL_BASIS:
                # Buy spot, sell futures
                spot_order_type = mt5.ORDER_TYPE_BUY
                futures_order_type = mt5.ORDER_TYPE_SELL
            elif signal_type == SignalType.BUY_BASIS:
                # Buy futures, sell spot
                spot_order_type = mt5.ORDER_TYPE_SELL
                futures_order_type = mt5.ORDER_TYPE_BUY
            else:
                raise ValueError(f"Invalid signal type for opening: {signal_type}")
            
            # Create trade objects
            spot_trade = Trade(spot_symbol, spot_order_type, lot_size)
            futures_trade = Trade(futures_symbol, futures_order_type, lot_size)
            
            # Execute spot trade first
            if not self.execute_market_order(spot_trade):
                return False, spot_trade, futures_trade
            
            # Execute futures trade
            if not self.execute_market_order(futures_trade):
                # Spot trade succeeded but futures failed - need to reverse spot
                logging.error("Futures trade failed, attempting to reverse spot trade")
                reverse_spot_type = mt5.ORDER_TYPE_SELL if spot_order_type == mt5.ORDER_TYPE_BUY else mt5.ORDER_TYPE_BUY
                reverse_trade = Trade(spot_symbol, reverse_spot_type, lot_size)
                self.execute_market_order(reverse_trade)
                return False, spot_trade, futures_trade
            
            logging.info(f"Trade pair executed successfully: {asset} {signal_type.value}")
            return True, spot_trade, futures_trade

        except Exception as e:
            logging.error(f"Trade pair execution error: {e}")
            if spot_trade:
                spot_trade.status = "ERROR"
                spot_trade.error_message = str(e)
            if futures_trade:
                futures_trade.status = "ERROR"
                futures_trade.error_message = str(e)
            return False, spot_trade, futures_trade

    def place_limit_order(self, limit_order):
        """Place a limit order on MT5"""
        try:
            symbol_info = mt5.symbol_info(limit_order.symbol)
            if not symbol_info:
                limit_order.status = "ERROR"
                limit_order.error_message = f"Symbol {limit_order.symbol} not found"
                return False

            if not symbol_info.visible:
                mt5.symbol_select(limit_order.symbol, True)

            # Determine the MT5 order type based on the limit order direction
            # For exit orders: SELL_LIMIT to close long, BUY_LIMIT to close short
            if limit_order.order_type == mt5.ORDER_TYPE_SELL:
                mt5_order_type = mt5.ORDER_TYPE_SELL_LIMIT
            else:
                mt5_order_type = mt5.ORDER_TYPE_BUY_LIMIT

            # Prepare limit order request
            request = {
                "action": mt5.TRADE_ACTION_PENDING,
                "symbol": limit_order.symbol,
                "volume": limit_order.lot_size,
                "type": mt5_order_type,
                "price": limit_order.limit_price,
                "magic": 12346,  # Different magic number for limit orders
                "comment": f"LimitExit_{limit_order.order_id}",
                "type_time": mt5.ORDER_TIME_GTC,
                "type_filling": mt5.ORDER_FILLING_RETURN,  # Allow partial fills
            }

            result = mt5.order_send(request)

            if result.retcode != mt5.TRADE_RETCODE_DONE:
                limit_order.status = "ERROR"
                limit_order.error_message = f"Limit order failed: {result.retcode} - {result.comment}"
                logging.error(f"Limit order placement failed: {limit_order.symbol} - {result.comment}")
                return False

            limit_order.mt5_ticket = result.order
            limit_order.status = "PLACED"
            limit_order.last_modified = datetime.now()
            self.pending_orders[limit_order.order_id] = limit_order

            logging.info(f"Limit order placed: {limit_order.symbol} {limit_order.lot_size} lots @ {limit_order.limit_price} (ticket: {result.order})")
            return True

        except Exception as e:
            limit_order.status = "ERROR"
            limit_order.error_message = f"Limit order error: {str(e)}"
            logging.error(f"Limit order exception: {e}")
            return False

    def modify_limit_order(self, limit_order, new_price):
        """Modify an existing limit order price"""
        try:
            if not limit_order.mt5_ticket:
                logging.error(f"Cannot modify limit order {limit_order.order_id}: no MT5 ticket")
                return False

            # Determine order type
            if limit_order.order_type == mt5.ORDER_TYPE_SELL:
                mt5_order_type = mt5.ORDER_TYPE_SELL_LIMIT
            else:
                mt5_order_type = mt5.ORDER_TYPE_BUY_LIMIT

            request = {
                "action": mt5.TRADE_ACTION_MODIFY,
                "order": limit_order.mt5_ticket,
                "price": new_price,
                "type_time": mt5.ORDER_TIME_GTC,
            }

            result = mt5.order_send(request)

            if result.retcode != mt5.TRADE_RETCODE_DONE:
                logging.error(f"Failed to modify limit order: {result.comment}")
                return False

            limit_order.limit_price = new_price
            limit_order.last_modified = datetime.now()

            logging.info(f"Limit order modified: {limit_order.symbol} new price @ {new_price}")
            return True

        except Exception as e:
            logging.error(f"Error modifying limit order: {e}")
            return False

    def cancel_limit_order(self, limit_order):
        """Cancel a pending limit order"""
        try:
            if not limit_order.mt5_ticket:
                logging.warning(f"Limit order {limit_order.order_id} has no MT5 ticket to cancel")
                limit_order.status = "CANCELLED"
                return True

            request = {
                "action": mt5.TRADE_ACTION_REMOVE,
                "order": limit_order.mt5_ticket,
            }

            result = mt5.order_send(request)

            if result.retcode != mt5.TRADE_RETCODE_DONE:
                # Check if order was already filled or doesn't exist
                orders = mt5.orders_get(ticket=limit_order.mt5_ticket)
                if not orders:
                    # Order no longer exists - might have been filled
                    limit_order.status = "CANCELLED"
                    logging.info(f"Limit order {limit_order.order_id} no longer exists (may have filled)")
                    return True
                logging.error(f"Failed to cancel limit order: {result.comment}")
                return False

            limit_order.status = "CANCELLED"
            if limit_order.order_id in self.pending_orders:
                del self.pending_orders[limit_order.order_id]

            logging.info(f"Limit order cancelled: {limit_order.symbol} @ {limit_order.limit_price}")
            return True

        except Exception as e:
            logging.error(f"Error cancelling limit order: {e}")
            return False

    def check_limit_order_filled(self, limit_order):
        """Check if a limit order has been filled"""
        try:
            if not limit_order.mt5_ticket:
                return False

            # Check if order still exists in pending orders
            orders = mt5.orders_get(ticket=limit_order.mt5_ticket)

            if orders is None or len(orders) == 0:
                # Order no longer pending - check if it was filled by looking at deals
                deals = mt5.history_deals_get(position=limit_order.mt5_ticket)
                if deals and len(deals) > 0:
                    # Order was filled
                    limit_order.status = "FILLED"
                    limit_order.fill_price = deals[-1].price
                    limit_order.fill_time = datetime.fromtimestamp(deals[-1].time)

                    if limit_order.order_id in self.pending_orders:
                        del self.pending_orders[limit_order.order_id]

                    logging.info(f"Limit order filled: {limit_order.symbol} @ {limit_order.fill_price}")
                    return True

                # Also check history orders to see final state
                history_orders = mt5.history_orders_get(ticket=limit_order.mt5_ticket)
                if history_orders and len(history_orders) > 0:
                    last_order = history_orders[-1]
                    if last_order.state == mt5.ORDER_STATE_FILLED:
                        limit_order.status = "FILLED"
                        limit_order.fill_price = last_order.price_current
                        limit_order.fill_time = datetime.fromtimestamp(last_order.time_done)

                        if limit_order.order_id in self.pending_orders:
                            del self.pending_orders[limit_order.order_id]

                        logging.info(f"Limit order filled (from history): {limit_order.symbol} @ {limit_order.fill_price}")
                        return True

            return False

        except Exception as e:
            logging.error(f"Error checking limit order status: {e}")
            return False

class PositionManager:
    """Manages trading positions and P&L"""
    
    def __init__(self, data_logger):
        self.positions = {}
        self.data_logger = data_logger
        self.position_counter = 0
    
    def create_position(self, asset, signal_type, spot_trade, futures_trade, entry_premium):
        """Create new position"""
        self.position_counter += 1
        position_id = f"POS_{self.position_counter:04d}"
        
        position = Position(position_id, asset, signal_type, spot_trade, futures_trade)
        position.entry_premium = entry_premium
        position.current_premium = entry_premium
        
        self.positions[position_id] = position
        
        # Log to database
        self.data_logger.log_position(position)
        self.data_logger.log_trade(spot_trade, position_id)
        self.data_logger.log_trade(futures_trade, position_id)
        
        logging.info(f"Position created: {position_id} - {asset} {signal_type.value} at {entry_premium:.2f}%")
        return position
    
    def update_position_pnl(self, position_id, current_spot_price, current_futures_price, current_premium):
        """Update position P&L"""
        if position_id not in self.positions:
            return
        
        position = self.positions[position_id]
        position.current_premium = current_premium
        
        # Calculate unrealized P&L
        if position.signal_type == SignalType.SELL_BASIS:
            # Long spot, short futures
            spot_pnl = (current_spot_price - position.spot_trade.executed_price) * position.spot_trade.lot_size
            futures_pnl = (position.futures_trade.executed_price - current_futures_price) * position.futures_trade.lot_size
        else:  # BUY_BASIS
            # Short spot, long futures
            spot_pnl = (position.spot_trade.executed_price - current_spot_price) * position.spot_trade.lot_size
            futures_pnl = (current_futures_price - position.futures_trade.executed_price) * position.futures_trade.lot_size
        
        position.unrealized_pnl = spot_pnl + futures_pnl
        
        # Update database
        self.data_logger.log_position(position)
    
    def close_position(self, position_id, close_reason, order_manager):
        """Close position"""
        if position_id not in self.positions:
            return False
        
        position = self.positions[position_id]
        if position.status != PositionStatus.ACTIVE:
            return False
        
        position.status = PositionStatus.CLOSING
        
        try:
            # Create closing trades (reverse of opening trades)
            if position.signal_type == SignalType.SELL_BASIS:
                # Close: Sell spot, buy futures
                close_spot_type = mt5.ORDER_TYPE_SELL
                close_futures_type = mt5.ORDER_TYPE_BUY
            else:  # BUY_BASIS
                # Close: Buy spot, sell futures
                close_spot_type = mt5.ORDER_TYPE_BUY
                close_futures_type = mt5.ORDER_TYPE_SELL
            
            # Execute closing trades
            success, close_spot_trade, close_futures_trade = order_manager.execute_trade_pair(
                position.asset, SignalType.NO_SIGNAL, position.spot_trade.lot_size,
                position.spot_trade.symbol, position.futures_trade.symbol
            )
            
            if success:
                position.status = PositionStatus.CLOSED
                position.close_time = datetime.now()
                position.close_reason = close_reason
                position.realized_pnl = position.unrealized_pnl
                position.unrealized_pnl = 0.0
                
                # Log closing trades
                self.data_logger.log_trade(close_spot_trade, position_id)
                self.data_logger.log_trade(close_futures_trade, position_id)
                self.data_logger.log_position(position)
                
                logging.info(f"Position closed: {position_id} - {close_reason} - P&L: ${position.realized_pnl:.2f}")
                return True
            else:
                position.status = PositionStatus.ERROR
                logging.error(f"Failed to close position: {position_id}")
                return False
                
        except Exception as e:
            position.status = PositionStatus.ERROR
            logging.error(f"Error closing position {position_id}: {e}")
            return False
    
    def get_active_positions(self):
        """Get all active positions"""
        return {pid: pos for pid, pos in self.positions.items() if pos.status == PositionStatus.ACTIVE}
    
    def get_positions_for_asset(self, asset):
        """Get active positions for specific asset"""
        return {pid: pos for pid, pos in self.positions.items() 
                if pos.asset == asset and pos.status == PositionStatus.ACTIVE}

class RiskManager:
    """Risk management and validation"""
    
    def __init__(self, config):
        self.config = config
        self.daily_trades = deque(maxlen=1000)
        self.last_signal_time = {}
    
    def validate_new_position(self, asset, signal_type, lot_size, position_manager):
        """Validate if new position can be opened"""
        # Check maximum positions per asset
        active_positions = position_manager.get_positions_for_asset(asset)
        if len(active_positions) >= self.config.RISK_LIMITS['MAX_POSITIONS_PER_ASSET']:
            return False, f"Maximum positions reached for {asset}"
        
        # Check lot size limit
        if lot_size > self.config.RISK_LIMITS['MAX_LOT_SIZE']:
            return False, f"Lot size {lot_size} exceeds maximum {self.config.RISK_LIMITS['MAX_LOT_SIZE']}"
        
        # Check daily trade limit
        today = datetime.now().date()
        today_trades = [t for t in self.daily_trades if t.date() == today]
        if len(today_trades) >= self.config.RISK_LIMITS['MAX_DAILY_TRADES']:
            return False, "Daily trade limit reached"
        
        # Check minimum time between signals
        last_time = self.last_signal_time.get(asset, datetime.min)
        time_diff = (datetime.now() - last_time).total_seconds()
        if time_diff < self.config.EXECUTION['MIN_TIME_BETWEEN_SIGNALS']:
            return False, f"Too soon since last signal for {asset}"
        
        return True, "OK"
    
    def record_trade(self, asset):
        """Record trade for risk tracking"""
        self.daily_trades.append(datetime.now())
        self.last_signal_time[asset] = datetime.now()
    
    def check_position_risk(self, position, current_premium):
        """Check if position needs risk management action"""
        # Check stop loss
        entry_premium = position.entry_premium
        premium_change = current_premium - entry_premium
        
        if position.signal_type == SignalType.SELL_BASIS:
            # For sell basis, loss occurs when premium increases further
            if premium_change > self.config.RISK_LIMITS['STOP_LOSS_PCT']:
                return True, "STOP_LOSS"
        else:  # BUY_BASIS
            # For buy basis, loss occurs when discount increases further (more negative)
            if premium_change < -self.config.RISK_LIMITS['STOP_LOSS_PCT']:
                return True, "STOP_LOSS"
        
        return False, None

class SignalGenerator:
    """Generates trading signals based on swap premium analysis"""
    
    def __init__(self, config):
        self.config = config
    
    def generate_signal(self, asset, market_data, active_positions):
        """Generate trading signal for asset"""
        swap_premium = market_data['swap_premium_pct']
        
        # Check for entry signals only if no active positions
        if not active_positions:
            if swap_premium > self.config.SIGNAL_THRESHOLDS['PREMIUM_ENTRY']:
                return SignalType.SELL_BASIS
            elif swap_premium < self.config.SIGNAL_THRESHOLDS['DISCOUNT_ENTRY']:
                return SignalType.BUY_BASIS
        
        # Check for exit signals on existing positions
        exit_signals = []
        for position_id, position in active_positions.items():
            if position.signal_type == SignalType.SELL_BASIS:
                if swap_premium <= self.config.SIGNAL_THRESHOLDS['PREMIUM_EXIT']:
                    exit_signals.append((position_id, SignalType.CLOSE_LONG))
            elif position.signal_type == SignalType.BUY_BASIS:
                if swap_premium >= self.config.SIGNAL_THRESHOLDS['DISCOUNT_EXIT']:
                    exit_signals.append((position_id, SignalType.CLOSE_SHORT))
        
        if exit_signals:
            return exit_signals[0]  # Return first exit signal
        
        return SignalType.NO_SIGNAL

class PerformanceTracker:
    """Track trading performance metrics"""
    
    def __init__(self):
        self.reset_daily_metrics()
        self.total_trades = 0
        self.winning_trades = 0
        self.total_pnl = 0.0
        self.max_drawdown = 0.0
        self.peak_pnl = 0.0
    
    def reset_daily_metrics(self):
        """Reset daily performance metrics"""
        self.daily_pnl = 0.0
        self.daily_trades = 0
        self.daily_winners = 0
    
    def update_with_closed_position(self, position):
        """Update metrics when position is closed"""
        self.total_trades += 1
        self.daily_trades += 1
        
        pnl = position.realized_pnl
        self.total_pnl += pnl
        self.daily_pnl += pnl
        
        if pnl > 0:
            self.winning_trades += 1
            self.daily_winners += 1
        
        # Update peak and drawdown
        if self.total_pnl > self.peak_pnl:
            self.peak_pnl = self.total_pnl
        
        current_drawdown = self.peak_pnl - self.total_pnl
        if current_drawdown > self.max_drawdown:
            self.max_drawdown = current_drawdown
    
    def get_metrics(self):
        """Get current performance metrics"""
        win_rate = (self.winning_trades / max(self.total_trades, 1)) * 100
        daily_win_rate = (self.daily_winners / max(self.daily_trades, 1)) * 100
        
        return {
            'total_pnl': self.total_pnl,
            'daily_pnl': self.daily_pnl,
            'total_trades': self.total_trades,
            'daily_trades': self.daily_trades,
            'win_rate': win_rate,
            'daily_win_rate': daily_win_rate,
            'max_drawdown': self.max_drawdown
        }

class AlgorithmicTradingSystem:
    """Main algorithmic trading system"""
    
    def __init__(self, trading_mode="PAPER"):
        self.config = AlgoTradingConfig()
        self.trading_mode = trading_mode  # "PAPER" or "LIVE"
        self.is_running = False
        
        # Initialize components
        self.data_logger = DataLogger()
        self.order_manager = OrderManager(self.config)
        self.position_manager = PositionManager(self.data_logger)
        self.risk_manager = RiskManager(self.config)
        self.signal_generator = SignalGenerator(self.config)
        self.performance_tracker = PerformanceTracker()
        
        # Market data and symbols
        self.active_assets = {}
        self.last_market_data = {}
        self.last_signals = {}
        
        # Display management
        self.update_counter = 0
        
        # Timezone setup
        self.trading_tz = pytz.timezone('US/Eastern')
        self.session_schedule = {
            TradingSession.ASIAN_PRE: (dt_time(18, 0), dt_time(21, 30)),
            TradingSession.CHINA_OPEN: (dt_time(21, 30), dt_time(23, 59)),
            TradingSession.ASIAN_LATE: (dt_time(0, 0), dt_time(3, 0)),
            TradingSession.LONDON_OPEN: (dt_time(3, 0), dt_time(6, 0)),
            TradingSession.EUROPEAN: (dt_time(6, 0), dt_time(9, 30)),
            TradingSession.US_OPEN: (dt_time(9, 30), dt_time(12, 0)),
            TradingSession.US_AFTERNOON: (dt_time(12, 0), dt_time(16, 0)),
            TradingSession.AFTER_HOURS: (dt_time(16, 0), dt_time(18, 0))
        }
        
        logging.info(f"Algorithmic Trading System initialized in {trading_mode} mode")
    
    def get_swap_charges(self):
        """Get swap charges from user"""
        print("\n" + "="*84)
        print("ALGORITHMIC TRADING SYSTEM - SWAP CONFIGURATION")
        print("="*84)
        print("Enter daily swap charges for long positions (USD per lot per day)")
        print("These will be used for automated basis trading signals")
        print("-"*84)
        
        for asset_key, asset_config in self.config.ASSETS.items():
            if not asset_config['enabled']:
                continue
                
            while True:
                try:
                    lot_size = asset_config['lot_size']
                    unit = "oz" if asset_key in ['GOLD', 'SILVER'] else "unit"
                    
                    prompt = f"{asset_config['name']} (lot size: {lot_size:,} {unit}): $"
                    user_input = input(prompt).strip()
                    
                    if user_input == "":
                        print("  Error: Swap charge required for algorithmic trading")
                        continue
                    else:
                        swap_charge = float(user_input)
                        if swap_charge <= 0:
                            print("  Error: Please enter a positive swap charge")
                            continue
                        break
                        
                except ValueError:
                    print("  Please enter a valid positive number")
                    continue
            
            self.config.ASSETS[asset_key]['swap_charge'] = swap_charge
            print(f"  → Set to ${swap_charge:.2f} per lot per day")
        
        print("-"*84)
        print("Swap configuration complete!")
        print("="*84)
    
    def get_trading_mode(self):
        """Get trading mode from user"""
        print("\n" + "="*84)
        print("TRADING MODE SELECTION")
        print("="*84)
        print("1. PAPER TRADING - Simulate trades without real execution")
        print("2. LIVE TRADING - Execute real trades on your account")
        print("-"*84)
        
        while True:
            choice = input("Select trading mode (1 for PAPER, 2 for LIVE, default: 1): ").strip()
            if choice == "" or choice == "1":
                self.trading_mode = "PAPER"
                print("Selected: PAPER TRADING MODE")
                break
            elif choice == "2":
                confirm = input("WARNING: This will execute REAL trades with REAL money. Are you sure? (yes/no): ").strip().lower()
                if confirm == "yes":
                    self.trading_mode = "LIVE"
                    print("Selected: LIVE TRADING MODE")
                    break
                else:
                    print("Returning to paper trading mode")
                    self.trading_mode = "PAPER"
                    break
            else:
                print("Please enter 1 or 2")
        
        print("="*84)
    
    def initialize_mt5(self):
        """Initialize MT5 connection"""
        logging.info("Connecting to MT5...")
        
        if not mt5.initialize():
            error = mt5.last_error()
            logging.error(f"MT5 initialization failed: {error}")
            return False
        
        account_info = mt5.account_info()
        if account_info:
            logging.info(f"Connected to: {account_info.server}")
            logging.info(f"Account: {account_info.name}")
            
        return self.setup_all_symbols()
    
    def setup_all_symbols(self):
        """Setup symbols for all enabled assets"""
        logging.info("Setting up symbols for algorithmic trading...")
        
        for asset_key, asset_config in self.config.ASSETS.items():
            if not asset_config['enabled']:
                continue
                
            spot_symbol = None
            futures_symbol = None
            
            # Find available spot symbol
            for symbol in asset_config['spot_symbols']:
                symbol_info = mt5.symbol_info(symbol)
                if symbol_info:
                    if not symbol_info.visible:
                        mt5.symbol_select(symbol, True)
                    spot_symbol = symbol
                    break
            
            # Find available futures symbol
            for symbol in asset_config['futures_symbols']:
                symbol_info = mt5.symbol_info(symbol)
                if symbol_info:
                    if not symbol_info.visible:
                        mt5.symbol_select(symbol, True)
                    futures_symbol = symbol
                    break
            
            if spot_symbol and futures_symbol:
                self.active_assets[asset_key] = {
                    'config': asset_config,
                    'spot_symbol': spot_symbol,
                    'futures_symbol': futures_symbol,
                    'last_data': None
                }
                logging.info(f"{asset_key}: {spot_symbol} + {futures_symbol}")
            else:
                logging.warning(f"{asset_key}: Missing symbols - Spot: {spot_symbol}, Futures: {futures_symbol}")
        
        return len(self.active_assets) > 0
    
    def get_current_session(self):
        """Get current trading session"""
        et_time = datetime.now(self.trading_tz)
        current_time = et_time.time()
        
        for session, (start_time, end_time) in self.session_schedule.items():
            if start_time > end_time:  # Crosses midnight
                if current_time >= start_time or current_time <= end_time:
                    return session
            else:
                if start_time <= end_time and start_time <= current_time <= end_time:
                    return session
        
        return TradingSession.AFTER_HOURS
    
    def calculate_swap_basis(self, asset_key, spot_price, time_to_expiry):
        """Calculate swap-based basis using real trading costs"""
        config = self.config.ASSETS[asset_key]
        swap_charge = config['swap_charge']
        lot_size = config['lot_size']

        # Calculate daily swap rate as percentage of position value
        position_value = spot_price * lot_size
        daily_swap_rate = swap_charge / position_value
        annual_swap_rate = daily_swap_rate * 365

        # Calculate swap-implied futures price and basis
        swap_futures_price = spot_price * math.exp(annual_swap_rate * time_to_expiry)
        swap_basis = swap_futures_price - spot_price

        return swap_futures_price, swap_basis, annual_swap_rate

    def calculate_target_exit_prices(self, position, current_spot_price, current_futures_price, time_to_expiry):
        """
        Calculate target exit prices for limit orders based on the exit threshold.

        For SELL_BASIS: Exit when premium ≤ 5% (target_premium = 5%)
        For BUY_BASIS: Exit when discount ≥ -5% (target_premium = -5%)

        Returns (target_spot_price, target_futures_price) for limit orders.

        Strategy: We set limit orders that achieve the target premium when filled.
        Since we're trading a spread, we calculate target prices for BOTH legs
        to maximize the chance of getting filled at favorable prices.
        """
        asset_key = position.asset

        # Get swap basis at current spot price
        swap_futures_price, swap_basis, _ = self.calculate_swap_basis(
            asset_key, current_spot_price, time_to_expiry
        )

        # Determine target premium based on position type
        if position.signal_type == SignalType.SELL_BASIS:
            target_premium_pct = self.config.SIGNAL_THRESHOLDS['PREMIUM_EXIT']
        else:  # BUY_BASIS
            target_premium_pct = self.config.SIGNAL_THRESHOLDS['DISCOUNT_EXIT']

        # Calculate target basis that corresponds to target premium
        # swap_premium_pct = ((actual_basis - swap_basis) / abs(swap_basis)) * 100
        # Solving for target_basis:
        # target_premium_pct = ((target_basis - swap_basis) / abs(swap_basis)) * 100
        # target_basis = swap_basis + (target_premium_pct / 100) * abs(swap_basis)
        target_basis = swap_basis + (target_premium_pct / 100) * abs(swap_basis)

        # Now we need to determine target prices for spot and futures
        # target_basis = target_futures_price - target_spot_price

        # For SELL_BASIS (long spot, short futures):
        #   Exit: Sell spot (want high price), Buy futures (want low price)
        #   We want basis to decrease (converge)
        #   Place SELL_LIMIT on spot, BUY_LIMIT on futures

        # For BUY_BASIS (short spot, long futures):
        #   Exit: Buy spot (want low price), Sell futures (want high price)
        #   We want basis to increase (converge toward zero)
        #   Place BUY_LIMIT on spot, SELL_LIMIT on futures

        # Calculate target prices using current spot as anchor
        # Target futures = current_spot + target_basis
        target_futures_price = current_spot_price + target_basis

        # Add a small buffer for better fill probability
        buffer_pct = self.config.EXECUTION['LIMIT_ORDER_PRICE_BUFFER_PCT']

        if position.signal_type == SignalType.SELL_BASIS:
            # Exit: Sell spot (slightly lower limit), Buy futures (slightly higher limit)
            target_spot_exit = current_spot_price * (1 - buffer_pct / 100)
            target_futures_exit = target_futures_price * (1 + buffer_pct / 100)
        else:  # BUY_BASIS
            # Exit: Buy spot (slightly higher limit), Sell futures (slightly lower limit)
            target_spot_exit = current_spot_price * (1 + buffer_pct / 100)
            target_futures_exit = target_futures_price * (1 - buffer_pct / 100)

        return target_spot_exit, target_futures_exit, target_basis

    def place_limit_exit_orders(self, position, market_data):
        """
        Place limit exit orders for a position to save on bid-ask spread.
        Called after position entry to set up automatic exit at target prices.
        """
        if not self.config.EXECUTION['USE_LIMIT_ORDERS_FOR_EXIT']:
            return False

        try:
            # Calculate target exit prices
            target_spot, target_futures, target_basis = self.calculate_target_exit_prices(
                position,
                market_data['spot_price'],
                market_data['futures_price'],
                market_data['time_to_expiry']
            )

            # Store target prices on position
            position.target_spot_exit_price = target_spot
            position.target_futures_exit_price = target_futures

            # Determine exit order types based on position
            if position.signal_type == SignalType.SELL_BASIS:
                # Exit: Sell spot, Buy futures
                spot_exit_type = mt5.ORDER_TYPE_SELL
                futures_exit_type = mt5.ORDER_TYPE_BUY
            else:  # BUY_BASIS
                # Exit: Buy spot, Sell futures
                spot_exit_type = mt5.ORDER_TYPE_BUY
                futures_exit_type = mt5.ORDER_TYPE_SELL

            # Create limit order objects
            spot_limit_order = LimitOrder(
                order_id=f"{position.position_id}_SPOT_EXIT",
                symbol=position.spot_trade.symbol,
                order_type=spot_exit_type,
                lot_size=position.spot_trade.lot_size,
                limit_price=round(target_spot, 2),
                position_id=position.position_id
            )

            futures_limit_order = LimitOrder(
                order_id=f"{position.position_id}_FUT_EXIT",
                symbol=position.futures_trade.symbol,
                order_type=futures_exit_type,
                lot_size=position.futures_trade.lot_size,
                limit_price=round(target_futures, 2),
                position_id=position.position_id
            )

            # Place spot limit order
            spot_success = self.order_manager.place_limit_order(spot_limit_order)
            if spot_success:
                position.spot_limit_order = spot_limit_order
                logging.info(f"Placed spot limit exit order: {spot_limit_order.symbol} @ {target_spot:.2f}")
            else:
                logging.warning(f"Failed to place spot limit exit order for {position.position_id}")

            # Place futures limit order
            futures_success = self.order_manager.place_limit_order(futures_limit_order)
            if futures_success:
                position.futures_limit_order = futures_limit_order
                logging.info(f"Placed futures limit exit order: {futures_limit_order.symbol} @ {target_futures:.2f}")
            else:
                logging.warning(f"Failed to place futures limit exit order for {position.position_id}")

            return spot_success and futures_success

        except Exception as e:
            logging.error(f"Error placing limit exit orders: {e}")
            return False

    def update_limit_exit_orders(self, position, market_data):
        """
        Update limit exit order prices if market has moved significantly.
        This keeps limit orders at optimal prices as swap_basis changes with time.
        """
        if not self.config.EXECUTION['USE_LIMIT_ORDERS_FOR_EXIT']:
            return

        if not position.spot_limit_order or not position.futures_limit_order:
            return

        try:
            # Calculate new target exit prices
            new_target_spot, new_target_futures, _ = self.calculate_target_exit_prices(
                position,
                market_data['spot_price'],
                market_data['futures_price'],
                market_data['time_to_expiry']
            )

            update_threshold = self.config.EXECUTION['LIMIT_ORDER_UPDATE_THRESHOLD']

            # Check if spot limit needs update
            if position.spot_limit_order.status == "PLACED":
                spot_change_pct = abs(new_target_spot - position.spot_limit_order.limit_price) / position.spot_limit_order.limit_price * 100
                if spot_change_pct > update_threshold:
                    self.order_manager.modify_limit_order(position.spot_limit_order, round(new_target_spot, 2))
                    position.target_spot_exit_price = new_target_spot

            # Check if futures limit needs update
            if position.futures_limit_order.status == "PLACED":
                futures_change_pct = abs(new_target_futures - position.futures_limit_order.limit_price) / position.futures_limit_order.limit_price * 100
                if futures_change_pct > update_threshold:
                    self.order_manager.modify_limit_order(position.futures_limit_order, round(new_target_futures, 2))
                    position.target_futures_exit_price = new_target_futures

        except Exception as e:
            logging.error(f"Error updating limit exit orders: {e}")

    def check_limit_order_fills(self, position):
        """
        Check if limit exit orders have been filled.
        Returns True if BOTH orders are filled (position fully closed via limits).
        """
        if not position.spot_limit_order or not position.futures_limit_order:
            return False

        spot_filled = False
        futures_filled = False

        # Check spot limit order
        if position.spot_limit_order.status == "PLACED":
            spot_filled = self.order_manager.check_limit_order_filled(position.spot_limit_order)
        elif position.spot_limit_order.status == "FILLED":
            spot_filled = True

        # Check futures limit order
        if position.futures_limit_order.status == "PLACED":
            futures_filled = self.order_manager.check_limit_order_filled(position.futures_limit_order)
        elif position.futures_limit_order.status == "FILLED":
            futures_filled = True

        return spot_filled and futures_filled

    def cancel_limit_exit_orders(self, position):
        """Cancel pending limit exit orders for a position (e.g., before market exit or stop loss)."""
        if position.spot_limit_order and position.spot_limit_order.status == "PLACED":
            self.order_manager.cancel_limit_order(position.spot_limit_order)

        if position.futures_limit_order and position.futures_limit_order.status == "PLACED":
            self.order_manager.cancel_limit_order(position.futures_limit_order)

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
            
            # Handle price multipliers
            multiplier = config.get('multiplier', 1.0)
            
            # Use mid-price when last price is 0
            spot_price = spot_tick.last if spot_tick.last > 0 else (spot_tick.bid + spot_tick.ask) / 2
            futures_price = (futures_tick.last if futures_tick.last > 0 else (futures_tick.bid + futures_tick.ask) / 2) * multiplier
            
            # Calculate spreads
            if asset_key in ['GOLD', 'SILVER']:
                spot_spread = (spot_tick.ask - spot_tick.bid) * 100  # cents
                futures_spread = (futures_tick.ask - futures_tick.bid) * 100  # cents
                spread_unit = '¢'
            else:
                spot_spread = (spot_tick.ask - spot_tick.bid) * 100
                futures_spread = (futures_tick.ask - futures_tick.bid) * 100
                spread_unit = '¢'
            
            # Actual market basis
            actual_basis = futures_price - spot_price
            
            # Calculate time to expiry
            current_time = datetime.now()
            time_to_expiry = (config['futures_expiry'] - current_time).total_seconds() / (365.25 * 24 * 3600)
            days_to_expiry = time_to_expiry * 365.25
            
            if time_to_expiry > 0:
                # Calculate swap-based basis
                swap_futures_price, swap_basis, annual_swap_rate = self.calculate_swap_basis(asset_key, spot_price, time_to_expiry)
                
                # Calculate swap premium
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
                days_to_expiry = 0
            
            return {
                'asset_name': config['name'],
                'timestamp': datetime.now(),
                'spot_price': spot_price,
                'futures_price': futures_price,
                'swap_futures_price': swap_futures_price,
                'spot_bid': spot_tick.bid,
                'spot_ask': spot_tick.ask,
                'futures_bid': futures_tick.bid * multiplier,
                'futures_ask': futures_tick.ask * multiplier,
                'spot_spread': spot_spread,
                'futures_spread': futures_spread,
                'spread_unit': spread_unit,
                'actual_basis': actual_basis,
                'swap_basis': swap_basis,
                'swap_premium_pct': swap_premium_pct,
                'swap_diff': swap_diff,
                'annual_swap_rate': annual_swap_rate,
                'time_to_expiry': time_to_expiry,
                'days_to_expiry': days_to_expiry
            }
            
        except Exception as e:
            logging.error(f"Error getting market data for {asset_key}: {e}")
            return None
    
    def get_all_market_data(self):
        """Get market data for all assets"""
        all_data = {}
        
        for asset_key in self.active_assets.keys():
            market_data = self.get_market_data(asset_key)
            if market_data:
                all_data[asset_key] = market_data
                self.active_assets[asset_key]['last_data'] = market_data
            else:
                # Use last known data if recent
                last_data = self.active_assets[asset_key]['last_data']
                if last_data:
                    age = (datetime.now() - last_data['timestamp']).total_seconds()
                    if age < 30:  # Use if less than 30 seconds old
                        all_data[asset_key] = last_data
        
        return all_data
    
    def process_trading_signals(self, all_market_data):
        """Process trading signals for all assets"""
        for asset_key, market_data in all_market_data.items():
            # Get active positions for this asset
            active_positions = self.position_manager.get_positions_for_asset(asset_key)

            # Update P&L and process limit orders for active positions
            positions_to_close = []
            for position_id, position in active_positions.items():
                self.position_manager.update_position_pnl(
                    position_id,
                    market_data['spot_price'],
                    market_data['futures_price'],
                    market_data['swap_premium_pct']
                )

                # Check if limit exit orders have been filled (position closed via limits)
                if self.config.EXECUTION['USE_LIMIT_ORDERS_FOR_EXIT']:
                    if self.check_limit_order_fills(position):
                        # Both limit orders filled - position closed at target prices
                        position.status = PositionStatus.CLOSED
                        position.close_time = datetime.now()
                        position.close_reason = "LIMIT_ORDER_EXIT"
                        position.realized_pnl = position.unrealized_pnl
                        position.unrealized_pnl = 0.0
                        self.data_logger.log_position(position)
                        self.performance_tracker.update_with_closed_position(position)
                        logging.info(f"Position {position_id} closed via limit orders - saved bid-ask spread!")
                        continue  # Skip further processing for this closed position

                    # Update limit order prices if market has moved
                    self.update_limit_exit_orders(position, market_data)

                # Check for risk management actions (stop loss)
                needs_action, action_type = self.risk_manager.check_position_risk(
                    position, market_data['swap_premium_pct']
                )

                if needs_action:
                    # Cancel pending limit orders before forced market exit
                    if self.config.EXECUTION['USE_LIMIT_ORDERS_FOR_EXIT']:
                        self.cancel_limit_exit_orders(position)
                        logging.info(f"Cancelled limit orders for {position_id} due to {action_type}")

                    self.position_manager.close_position(position_id, action_type, self.order_manager)
                    self.performance_tracker.update_with_closed_position(position)

            # Generate new signal
            signal = self.signal_generator.generate_signal(asset_key, market_data, active_positions)
            self.last_signals[asset_key] = signal

            # Log market data
            self.data_logger.log_market_data(asset_key, market_data, signal)

            # Process signal
            if signal == SignalType.SELL_BASIS or signal == SignalType.BUY_BASIS:
                self.execute_entry_signal(asset_key, signal, market_data)
            elif isinstance(signal, tuple) and len(signal) == 2:
                position_id, close_signal = signal
                position = self.position_manager.positions.get(position_id)

                # Cancel pending limit orders before signal-based market exit
                if position and self.config.EXECUTION['USE_LIMIT_ORDERS_FOR_EXIT']:
                    self.cancel_limit_exit_orders(position)
                    logging.info(f"Cancelled limit orders for {position_id} due to signal exit")

                self.position_manager.close_position(position_id, "SIGNAL_EXIT", self.order_manager)
                if position_id in self.position_manager.positions:
                    self.performance_tracker.update_with_closed_position(
                        self.position_manager.positions[position_id]
                    )
    
    def execute_entry_signal(self, asset_key, signal_type, market_data):
        """Execute entry signal"""
        # Validate with risk manager
        lot_size = self.config.RISK_LIMITS['MAX_LOT_SIZE']  # Use max lot size for now
        
        valid, reason = self.risk_manager.validate_new_position(
            asset_key, signal_type, lot_size, self.position_manager
        )
        
        if not valid:
            logging.info(f"Signal rejected for {asset_key}: {reason}")
            return
        
        # Get symbols
        asset = self.active_assets[asset_key]
        spot_symbol = asset['spot_symbol']
        futures_symbol = asset['futures_symbol']
        
        if self.trading_mode == "LIVE":
            # Execute real trades
            success, spot_trade, futures_trade = self.order_manager.execute_trade_pair(
                asset_key, signal_type, lot_size, spot_symbol, futures_symbol
            )

            if success:
                # Create position
                position = self.position_manager.create_position(
                    asset_key, signal_type, spot_trade, futures_trade,
                    market_data['swap_premium_pct']
                )

                self.risk_manager.record_trade(asset_key)
                logging.info(f"Position opened: {position.position_id} - {asset_key} {signal_type.value}")

                # Place limit exit orders to save on bid-ask spread
                if self.config.EXECUTION['USE_LIMIT_ORDERS_FOR_EXIT']:
                    limit_success = self.place_limit_exit_orders(position, market_data)
                    if limit_success:
                        logging.info(f"Limit exit orders placed for {position.position_id}")
                    else:
                        logging.warning(f"Failed to place limit exit orders for {position.position_id}, will use market orders for exit")
            else:
                logging.error(f"Failed to execute trades for {asset_key} {signal_type.value}")

        else:  # PAPER mode
            # Simulate trades
            logging.info(f"PAPER TRADE: {asset_key} {signal_type.value} at premium {market_data['swap_premium_pct']:.2f}%")
            self.risk_manager.record_trade(asset_key)
    
    def print_trading_display(self, all_market_data):
        """Print comprehensive trading display"""
        # Smooth update mechanism
        self.update_counter += 1
        
        # Only clear screen occasionally to reduce flashing
        if self.update_counter == 1 or self.update_counter % 50 == 0:
            print("\033[H\033[2J", end="", flush=True)
        else:
            print("\033[H", end="", flush=True)
        
        session = self.get_current_session()
        current_time = datetime.now().strftime('%H:%M:%S')
        
        # Header
        print("ALGORITHMIC BASIS TRADING SYSTEM - GOLD & SILVER")
        print("=" * 84)
        print(f"Session: {session.value:<12} | Time: {current_time} | Mode: {self.trading_mode:<5} | Trading: ACTIVE")
        print("Focus: Automated Basis Trading with Real Swap Costs")
        print("=" * 84)
        print()
        
        # Asset data
        display_order = ['GOLD', 'SILVER']
        
        for asset_key in display_order:
            if asset_key in self.active_assets and asset_key in all_market_data:
                self.print_asset_trading_data(asset_key, all_market_data[asset_key])
        
        # Trading status
        self.print_trading_status()
        
        sys.stdout.flush()
    
    def print_asset_trading_data(self, asset_key, market_data):
        """Print asset data with trading information"""
        # Format prices
        if asset_key in ['GOLD', 'SILVER']:
            price_format = "{:>8.2f}"
            basis_format = "{:>8.2f}"
        else:
            price_format = "{:>8.3f}"
            basis_format = "{:>8.4f}"
        
        spot_price_str = price_format.format(market_data['spot_price'])
        futures_price_str = price_format.format(market_data['futures_price'])
        actual_basis_str = basis_format.format(market_data['actual_basis'])
        swap_basis_str = basis_format.format(market_data['swap_basis'])
        
        print(f"{market_data['asset_name']}")
        print("=" * 84)
        print(f"SPOT       | {spot_price_str} | Bid: {market_data['spot_bid']:>10.4f} | Ask: {market_data['spot_ask']:>10.4f} | Spr: {market_data['spot_spread']:>6.1f}{market_data['spread_unit']}")
        print(f"FUTURES    | {futures_price_str} | Bid: {market_data['futures_bid']:>10.4f} | Ask: {market_data['futures_ask']:>10.4f} | Spr: {market_data['futures_spread']:>6.1f}{market_data['spread_unit']}")
        
        # Basis analysis
        print(f"Actual Basis    | {actual_basis_str} | Market Pricing | Days to Expiry: {market_data['days_to_expiry']:>4.0f}")
        print(f"Swap-Based Basis| {swap_basis_str} | Your Real Cost | Swap Diff: {market_data['swap_diff']:>+8.2f}")
        
        # Signal and premium info
        signal = self.last_signals.get(asset_key, SignalType.NO_SIGNAL)
        signal_str = signal.value if hasattr(signal, 'value') else str(signal)
        
        premium_color = "EXPENSIVE" if market_data['swap_diff'] > 5 else "CHEAP" if market_data['swap_diff'] < -5 else "FAIR"
        print(f"Market vs Swap  | Premium: {market_data['swap_premium_pct']:>+7.2f}% | Status: {premium_color:>9} | Signal: {signal_str}")
        
        # Active positions for this asset
        active_positions = self.position_manager.get_positions_for_asset(asset_key)
        if active_positions:
            print("ACTIVE POSITIONS:")
            for position_id, position in active_positions.items():
                age = datetime.now() - position.entry_time
                age_str = f"{age.seconds//3600}h {(age.seconds%3600)//60}m"
                print(f"  {position_id}: {position.signal_type.value} | Entry: {position.entry_premium:+.1f}% | Current: {position.current_premium:+.1f}% | P&L: ${position.unrealized_pnl:>+8.0f} | Age: {age_str}")

                # Show limit order status if enabled
                if self.config.EXECUTION['USE_LIMIT_ORDERS_FOR_EXIT']:
                    spot_status = "N/A"
                    fut_status = "N/A"
                    if position.spot_limit_order:
                        spot_status = f"{position.spot_limit_order.status}@{position.spot_limit_order.limit_price:.2f}"
                    if position.futures_limit_order:
                        fut_status = f"{position.futures_limit_order.status}@{position.futures_limit_order.limit_price:.2f}"
                    print(f"    Limit Orders: Spot={spot_status} | Fut={fut_status}")
        else:
            print("No active positions")
        
        print()
    
    def print_trading_status(self):
        """Print overall trading status"""
        metrics = self.performance_tracker.get_metrics()
        active_positions = self.position_manager.get_active_positions()
        
        print("TRADING STATUS")
        print("=" * 84)
        print(f"Strategy: ACTIVE | Positions: {len(active_positions)}/{self.config.RISK_LIMITS['MAX_POSITIONS_PER_ASSET']*len(self.active_assets)} | Daily P&L: ${metrics['daily_pnl']:>+8.0f} | Total P&L: ${metrics['total_pnl']:>+8.0f}")
        print(f"Daily Trades: {metrics['daily_trades']}/{self.config.RISK_LIMITS['MAX_DAILY_TRADES']} | Win Rate: {metrics['win_rate']:>5.1f}% | Max Drawdown: ${metrics['max_drawdown']:>8.0f}")
        
        # Trading thresholds
        print(f"Entry Thresholds: Premium >{self.config.SIGNAL_THRESHOLDS['PREMIUM_ENTRY']:+.0f}% | Discount <{self.config.SIGNAL_THRESHOLDS['DISCOUNT_ENTRY']:+.0f}%")
        exit_mode = "LIMIT ORDERS" if self.config.EXECUTION['USE_LIMIT_ORDERS_FOR_EXIT'] else "MARKET ORDERS"
        print(f"Exit Thresholds:  Premium ≤{self.config.SIGNAL_THRESHOLDS['PREMIUM_EXIT']:+.0f}% | Discount ≥{self.config.SIGNAL_THRESHOLDS['DISCOUNT_EXIT']:+.0f}% | Exit Mode: {exit_mode}")
        
        # Risk status
        risk_status = "NORMAL"
        if len(active_positions) >= len(self.active_assets) * self.config.RISK_LIMITS['MAX_POSITIONS_PER_ASSET']:
            risk_status = "MAX_POSITIONS"
        elif metrics['daily_trades'] >= self.config.RISK_LIMITS['MAX_DAILY_TRADES'] * 0.8:
            risk_status = "HIGH_FREQUENCY"
        
        print(f"Risk Status: {risk_status} | Mode: {self.trading_mode} | Updated: {datetime.now().strftime('%H:%M:%S')}")
        print("=" * 84)
    
    def trading_loop(self):
        """Main trading loop"""
        logging.info("Starting algorithmic trading loop...")
        
        consecutive_errors = 0
        max_consecutive_errors = 10
        loop_count = 0
        last_successful_time = datetime.now()
        
        while self.is_running:
            try:
                loop_start = time.time()
                loop_count += 1
                
                # Check MT5 connection periodically
                if loop_count % 100 == 0:
                    if not mt5.terminal_info():
                        logging.warning("MT5 connection lost, attempting to reconnect...")
                        mt5.shutdown()
                        if not mt5.initialize():
                            logging.error("Failed to reconnect to MT5")
                            consecutive_errors += 1
                            time.sleep(5)
                            continue
                
                # Get market data
                all_market_data = self.get_all_market_data()
                
                if all_market_data:
                    consecutive_errors = 0
                    last_successful_time = datetime.now()
                    
                    # Store for later use
                    self.last_market_data = all_market_data
                    
                    # Process trading signals
                    self.process_trading_signals(all_market_data)
                    
                    # Display
                    self.print_trading_display(all_market_data)
                    
                else:
                    consecutive_errors += 1
                    time_since_success = (datetime.now() - last_successful_time).total_seconds()
                    
                    if time_since_success > 30:
                        print(f"\033[H\033[2J", end="", flush=True)
                        print("=" * 84)
                        print("CONNECTION ISSUE DETECTED")
                        print("=" * 84)
                        print(f"No market data received for {time_since_success:.0f} seconds")
                        print(f"Consecutive errors: {consecutive_errors}")
                        print("Algorithmic trading paused...")
                        print("=" * 84)
                        sys.stdout.flush()
                
                # Handle too many consecutive errors
                if consecutive_errors > max_consecutive_errors:
                    logging.error(f"Too many consecutive errors: {consecutive_errors}")
                    print("\033[H\033[2J", end="", flush=True)
                    print("CRITICAL ERROR - RESTARTING CONNECTION")
                    print("=" * 84)
                    print("Shutting down MT5...")
                    mt5.shutdown()
                    print("Reinitializing MT5...")
                    if self.initialize_mt5():
                        print("Connection restored!")
                        consecutive_errors = 0
                        time.sleep(2)
                    else:
                        print("Failed to restore connection. Retrying in 10 seconds...")
                        time.sleep(10)
                
                # Ensure consistent timing
                loop_duration = time.time() - loop_start
                sleep_time = max(0.5 - loop_duration, 0.1)  # Minimum 0.1s sleep
                time.sleep(sleep_time)
                
                # Debug output every 1000 loops
                if loop_count % 1000 == 0:
                    logging.info(f"Algorithmic trading running: {loop_count} loops completed")
                
            except KeyboardInterrupt:
                logging.info("Trading stopped by user")
                break
            except Exception as e:
                logging.error(f"Error in trading loop: {e}")
                consecutive_errors += 1
                
                # Show error on screen
                print(f"\033[H\033[2J", end="", flush=True)
                print("=" * 84)
                print("TRADING ERROR")
                print("=" * 84)
                print(f"Error: {str(e)}")
                print(f"Loop: {loop_count}, Errors: {consecutive_errors}")
                print("Continuing in 2 seconds...")
                print("=" * 84)
                sys.stdout.flush()
                
                time.sleep(2)
        
        logging.info("Algorithmic trading loop stopped")
    
    def start(self):
        """Start the algorithmic trading system"""
        logging.info("Starting Algorithmic Trading System...")
        
        # Get configuration from user
        self.get_trading_mode()
        self.get_swap_charges()
        
        if not self.initialize_mt5():
            logging.error("Failed to initialize MT5")
            return False
        
        if not self.active_assets:
            logging.error("No assets available for trading")
            return False
        
        print("\nALGORITHMIC BASIS TRADING SYSTEM STARTED")
        print("=" * 84)
        print(f"Trading Mode: {self.trading_mode}")
        print("Active Assets:")
        for asset_key, asset_data in self.active_assets.items():
            swap_charge = self.config.ASSETS[asset_key]['swap_charge']
            print(f"  {asset_key}: {asset_data['spot_symbol']} + {asset_data['futures_symbol']} | Swap: ${swap_charge:.2f}/day")
        
        print("\nTrading Strategy:")
        print(f"  • SELL BASIS: Premium >{self.config.SIGNAL_THRESHOLDS['PREMIUM_ENTRY']:+.0f}% → Exit ≤{self.config.SIGNAL_THRESHOLDS['PREMIUM_EXIT']:+.0f}%")
        print(f"  • BUY BASIS:  Discount <{self.config.SIGNAL_THRESHOLDS['DISCOUNT_ENTRY']:+.0f}% → Exit ≥{self.config.SIGNAL_THRESHOLDS['DISCOUNT_EXIT']:+.0f}%")

        print("\nExit Order Strategy:")
        if self.config.EXECUTION['USE_LIMIT_ORDERS_FOR_EXIT']:
            print("  • LIMIT ORDERS for exits - saves bid-ask spread!")
            print("  • Target prices calculated from exit thresholds")
            print("  • Orders auto-adjusted as market moves")
            print("  • Falls back to market orders for stop loss/forced exit")
        else:
            print("  • MARKET ORDERS for exits")

        print("\nRisk Management:")
        print(f"  • Max positions per asset: {self.config.RISK_LIMITS['MAX_POSITIONS_PER_ASSET']}")
        print(f"  • Max lot size: {self.config.RISK_LIMITS['MAX_LOT_SIZE']}")
        print(f"  • Daily trade limit: {self.config.RISK_LIMITS['MAX_DAILY_TRADES']}")
        print(f"  • Stop loss: {self.config.RISK_LIMITS['STOP_LOSS_PCT']:.1f}%")
        
        print(f"\nUpdates every 0.5 seconds | Press Ctrl+C to stop")
        print("=" * 84)
        
        if self.trading_mode == "LIVE":
            print("WARNING: LIVE TRADING MODE - REAL MONEY AT RISK")
            print("=" * 84)
            final_confirm = input("Type 'START' to begin live trading: ").strip().upper()
            if final_confirm != "START":
                print("Trading cancelled")
                return False
        
        time.sleep(3)
        
        # Reset daily metrics at start
        self.performance_tracker.reset_daily_metrics()
        
        self.is_running = True
        
        try:
            self.trading_loop()
        except KeyboardInterrupt:
            print("\nShutdown requested...")
        finally:
            self.stop()
        
        return True
    
    def stop(self):
        """Stop the trading system"""
        logging.info("Stopping Algorithmic Trading System...")
        self.is_running = False

        # Close any pending orders
        active_positions = self.position_manager.get_active_positions()
        if active_positions:
            print("Closing active positions...")
            for position_id, position in active_positions.items():
                # Cancel pending limit orders before closing with market orders
                if self.config.EXECUTION['USE_LIMIT_ORDERS_FOR_EXIT']:
                    self.cancel_limit_exit_orders(position)
                    logging.info(f"Cancelled limit orders for {position_id} due to system shutdown")

                self.position_manager.close_position(position_id, "SYSTEM_SHUTDOWN", self.order_manager)

        mt5.shutdown()
        
        # Print final summary
        metrics = self.performance_tracker.get_metrics()
        print("\nFINAL TRADING SUMMARY")
        print("=" * 84)
        print(f"Total P&L: ${metrics['total_pnl']:>+8.0f}")
        print(f"Total Trades: {metrics['total_trades']}")
        print(f"Win Rate: {metrics['win_rate']:>5.1f}%")
        print(f"Max Drawdown: ${metrics['max_drawdown']:>8.0f}")
        print("=" * 84)
        print("Algorithmic Trading System stopped")

def main():
    """Main function"""
    print("=" * 84)
    print("PRODUCTION ALGORITHMIC BASIS TRADING SYSTEM")
    print("=" * 84)
    print("Assets: GOLD (XAUUSD_ + GC1225) & SILVER (XAGUSD_ + SI1225)")
    print("Strategy: Automated basis trading using swap-cost analysis")
    print("\nFULL AUTOMATIC ORDER EXECUTION:")
    print("  ✓ Automatically places orders when signals trigger")
    print("  ✓ Automatically closes positions when exit conditions met")
    print("  ✓ No manual intervention required during trading")
    print("  ✓ Real-time monitoring with 0.5-second updates")
    print("\nFeatures:")
    print("  • Real-time signal generation and execution")
    print("  • Comprehensive risk management")
    print("  • Position tracking and P&L monitoring")
    print("  • Paper and live trading modes")
    print("  • SQLite database logging")
    print("  • Built on proven monitoring framework")
    print("  • LIMIT ORDERS for exits - saves bid-ask spread!")
    print("\nTrading Logic:")
    print("  • SELL BASIS when premium >20% (auto: buy spot, sell futures)")
    print("  • BUY BASIS when discount <-15% (auto: buy futures, sell spot)")
    print("  • Exit via LIMIT ORDERS at calculated target prices")
    print("  • Stop loss uses market orders for guaranteed exit")
    print()
    
    confirm = input("Start algorithmic trading system? (y/n, default: y): ").strip().lower()
    if confirm == 'n':
        print("System cancelled")
        return
    
    trading_system = AlgorithmicTradingSystem()
    
    try:
        if trading_system.start():
            print("Algorithmic Trading System completed successfully!")
        else:
            print("Failed to start trading system - check configuration and MT5 connection")
    except Exception as e:
        print(f"Error starting trading system: {e}")
        logging.error(f"Critical error: {e}")

if __name__ == "__main__":
    main()