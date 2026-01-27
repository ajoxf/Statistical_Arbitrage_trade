# -*- coding: utf-8 -*-
"""
PRODUCTION ALGORITHMIC BASIS TRADING SYSTEM - GOLD & SILVER
- Automated basis trading using swap-cost analysis
- Real-time signal generation and execution
- Comprehensive risk management and position tracking
- Built on proven monitoring framework
- Single executable production system
- MULTI-BROKER SUPPORT: Connect to multiple MT5 instances simultaneously
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
import multiprocessing as mp
from multiprocessing import Process, Queue, Manager, Event
from typing import Dict, List, Optional, Tuple, Any
from dataclasses import dataclass, field
import queue

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

# =============================================================================
# MULTI-BROKER CONFIGURATION
# =============================================================================

@dataclass
class BrokerConfig:
    """Configuration for a single broker connection"""
    broker_id: str
    name: str
    mt5_path: Optional[str] = None  # None = default MT5 installation
    login: Optional[int] = None
    password: Optional[str] = None
    server: Optional[str] = None
    symbols: List[str] = field(default_factory=list)
    enabled: bool = True

    def to_dict(self) -> dict:
        return {
            'broker_id': self.broker_id,
            'name': self.name,
            'mt5_path': self.mt5_path,
            'symbols': self.symbols,
            'enabled': self.enabled
        }

class MultiBrokerConfig:
    """
    Multi-broker configuration manager

    Usage Examples:

    1. Single broker (same as before):
        config = MultiBrokerConfig()
        config.add_broker(BrokerConfig(
            broker_id="main",
            name="MyBroker",
            symbols=["XAUUSD_", "XAGUSD_", "GC1225", "SI1225"]
        ))

    2. Two brokers (spot on one, futures on another):
        config = MultiBrokerConfig()
        config.add_broker(BrokerConfig(
            broker_id="spot_broker",
            name="SpotBroker",
            mt5_path="C:/MT5_Broker1/terminal64.exe",
            symbols=["XAUUSD_", "XAGUSD_"]
        ))
        config.add_broker(BrokerConfig(
            broker_id="futures_broker",
            name="FuturesBroker",
            mt5_path="C:/MT5_Broker2/terminal64.exe",
            symbols=["GC1225", "SI1225"]
        ))
    """

    def __init__(self):
        self.brokers: Dict[str, BrokerConfig] = {}
        self.symbol_to_broker: Dict[str, str] = {}  # Maps symbol -> broker_id

    def add_broker(self, broker: BrokerConfig):
        """Add a broker configuration"""
        self.brokers[broker.broker_id] = broker
        for symbol in broker.symbols:
            self.symbol_to_broker[symbol] = broker.broker_id
        logging.info(f"Added broker: {broker.name} ({broker.broker_id}) with symbols: {broker.symbols}")

    def get_broker_for_symbol(self, symbol: str) -> Optional[str]:
        """Get broker ID for a given symbol"""
        return self.symbol_to_broker.get(symbol)

    def get_broker(self, broker_id: str) -> Optional[BrokerConfig]:
        """Get broker configuration by ID"""
        return self.brokers.get(broker_id)

    def get_enabled_brokers(self) -> List[BrokerConfig]:
        """Get all enabled brokers"""
        return [b for b in self.brokers.values() if b.enabled]

    def is_multi_broker(self) -> bool:
        """Check if using multiple brokers"""
        return len(self.get_enabled_brokers()) > 1

    def get_all_symbols(self) -> List[str]:
        """Get all symbols across all brokers"""
        symbols = []
        for broker in self.get_enabled_brokers():
            symbols.extend(broker.symbols)
        return symbols

# =============================================================================
# BROKER CONNECTION PROCESS (for multi-broker mode)
# =============================================================================

class BrokerConnectionProcess:
    """
    Manages a single MT5 connection in a separate process.
    Used when running multiple broker connections simultaneously.
    """

    @staticmethod
    def run_broker_process(broker_config: dict, command_queue: Queue,
                           response_queue: Queue, market_data_queue: Queue,
                           shutdown_event: Event):
        """
        Main function that runs in a separate process for each broker.

        Args:
            broker_config: Broker configuration dictionary
            command_queue: Queue for receiving commands (execute trades, etc.)
            response_queue: Queue for sending responses back
            market_data_queue: Queue for sending market data updates
            shutdown_event: Event to signal shutdown
        """
        broker_id = broker_config['broker_id']
        broker_name = broker_config['name']
        mt5_path = broker_config.get('mt5_path')
        symbols = broker_config.get('symbols', [])

        logging.info(f"[{broker_id}] Starting broker process: {broker_name}")

        # Initialize MT5 connection
        init_kwargs = {}
        if mt5_path:
            init_kwargs['path'] = mt5_path
        if broker_config.get('login'):
            init_kwargs['login'] = broker_config['login']
        if broker_config.get('password'):
            init_kwargs['password'] = broker_config['password']
        if broker_config.get('server'):
            init_kwargs['server'] = broker_config['server']

        if not mt5.initialize(**init_kwargs) if init_kwargs else mt5.initialize():
            error = mt5.last_error()
            logging.error(f"[{broker_id}] MT5 initialization failed: {error}")
            response_queue.put({
                'type': 'init_result',
                'broker_id': broker_id,
                'success': False,
                'error': str(error)
            })
            return

        # Get account info
        account_info = mt5.account_info()
        if account_info:
            logging.info(f"[{broker_id}] Connected to: {account_info.server}")
            logging.info(f"[{broker_id}] Account: {account_info.name}")

        # Enable symbols
        available_symbols = []
        for symbol in symbols:
            symbol_info = mt5.symbol_info(symbol)
            if symbol_info:
                if not symbol_info.visible:
                    mt5.symbol_select(symbol, True)
                available_symbols.append(symbol)
                logging.info(f"[{broker_id}] Symbol enabled: {symbol}")
            else:
                logging.warning(f"[{broker_id}] Symbol not found: {symbol}")

        response_queue.put({
            'type': 'init_result',
            'broker_id': broker_id,
            'success': True,
            'available_symbols': available_symbols,
            'account_name': account_info.name if account_info else None,
            'server': account_info.server if account_info else None
        })

        # Main loop
        last_market_update = 0
        market_update_interval = 0.25  # 250ms

        while not shutdown_event.is_set():
            try:
                # Check for commands (non-blocking)
                try:
                    command = command_queue.get_nowait()
                    response = BrokerConnectionProcess.handle_command(broker_id, command)
                    response_queue.put(response)
                except queue.Empty:
                    pass

                # Send market data updates periodically
                current_time = time.time()
                if current_time - last_market_update >= market_update_interval:
                    market_data = BrokerConnectionProcess.get_market_data(broker_id, available_symbols)
                    if market_data:
                        market_data_queue.put({
                            'type': 'market_data',
                            'broker_id': broker_id,
                            'timestamp': datetime.now().isoformat(),
                            'data': market_data
                        })
                    last_market_update = current_time

                time.sleep(0.05)  # Small sleep to prevent CPU spinning

            except Exception as e:
                logging.error(f"[{broker_id}] Error in broker process: {e}")
                time.sleep(1)

        # Cleanup
        logging.info(f"[{broker_id}] Shutting down broker process")
        mt5.shutdown()

    @staticmethod
    def handle_command(broker_id: str, command: dict) -> dict:
        """Handle a command from the main process"""
        cmd_type = command.get('type')

        if cmd_type == 'execute_order':
            return BrokerConnectionProcess.execute_order(broker_id, command)
        elif cmd_type == 'get_positions':
            return BrokerConnectionProcess.get_positions(broker_id)
        elif cmd_type == 'get_account_info':
            return BrokerConnectionProcess.get_account_info(broker_id)
        elif cmd_type == 'ping':
            return {'type': 'pong', 'broker_id': broker_id, 'timestamp': datetime.now().isoformat()}
        else:
            return {'type': 'error', 'broker_id': broker_id, 'error': f'Unknown command: {cmd_type}'}

    @staticmethod
    def get_market_data(broker_id: str, symbols: List[str]) -> dict:
        """Get market data for all symbols"""
        data = {}
        for symbol in symbols:
            tick = mt5.symbol_info_tick(symbol)
            if tick:
                data[symbol] = {
                    'bid': tick.bid,
                    'ask': tick.ask,
                    'last': tick.last,
                    'volume': tick.volume,
                    'time': tick.time
                }
        return data

    @staticmethod
    def execute_order(broker_id: str, command: dict) -> dict:
        """Execute a trade order"""
        try:
            symbol = command['symbol']
            order_type = command['order_type']
            lot_size = command['lot_size']
            slippage = command.get('slippage', 10)

            symbol_info = mt5.symbol_info(symbol)
            if not symbol_info:
                return {
                    'type': 'order_result',
                    'broker_id': broker_id,
                    'success': False,
                    'error': f'Symbol {symbol} not found'
                }

            if not symbol_info.visible:
                mt5.symbol_select(symbol, True)

            tick = mt5.symbol_info_tick(symbol)
            if not tick:
                return {
                    'type': 'order_result',
                    'broker_id': broker_id,
                    'success': False,
                    'error': f'No tick data for {symbol}'
                }

            price = tick.ask if order_type == mt5.ORDER_TYPE_BUY else tick.bid

            request = {
                "action": mt5.TRADE_ACTION_DEAL,
                "symbol": symbol,
                "volume": lot_size,
                "type": order_type,
                "price": price,
                "deviation": slippage,
                "magic": 12345,
                "comment": f"MultiBroker_{command.get('trade_id', 'unknown')}",
                "type_time": mt5.ORDER_TIME_GTC,
                "type_filling": mt5.ORDER_FILLING_IOC,
            }

            result = mt5.order_send(request)

            if result.retcode != mt5.TRADE_RETCODE_DONE:
                return {
                    'type': 'order_result',
                    'broker_id': broker_id,
                    'success': False,
                    'error': f'Order failed: {result.retcode} - {result.comment}',
                    'retcode': result.retcode
                }

            return {
                'type': 'order_result',
                'broker_id': broker_id,
                'success': True,
                'order_ticket': result.order,
                'executed_price': result.price,
                'volume': result.volume,
                'symbol': symbol
            }

        except Exception as e:
            return {
                'type': 'order_result',
                'broker_id': broker_id,
                'success': False,
                'error': str(e)
            }

    @staticmethod
    def get_positions(broker_id: str) -> dict:
        """Get all open positions"""
        positions = mt5.positions_get()
        if positions is None:
            return {'type': 'positions', 'broker_id': broker_id, 'positions': []}

        pos_list = []
        for pos in positions:
            pos_list.append({
                'ticket': pos.ticket,
                'symbol': pos.symbol,
                'type': pos.type,
                'volume': pos.volume,
                'price_open': pos.price_open,
                'price_current': pos.price_current,
                'profit': pos.profit,
                'magic': pos.magic
            })

        return {'type': 'positions', 'broker_id': broker_id, 'positions': pos_list}

    @staticmethod
    def get_account_info(broker_id: str) -> dict:
        """Get account information"""
        info = mt5.account_info()
        if info is None:
            return {'type': 'account_info', 'broker_id': broker_id, 'info': None}

        return {
            'type': 'account_info',
            'broker_id': broker_id,
            'info': {
                'balance': info.balance,
                'equity': info.equity,
                'margin': info.margin,
                'margin_free': info.margin_free,
                'profit': info.profit,
                'name': info.name,
                'server': info.server
            }
        }

# =============================================================================
# MULTI-BROKER MANAGER
# =============================================================================

class MultiBrokerManager:
    """
    Manages multiple broker connections.
    Automatically switches between single-process and multi-process mode
    based on the number of brokers configured.
    """

    def __init__(self, broker_config: MultiBrokerConfig):
        self.config = broker_config
        self.is_multi_mode = broker_config.is_multi_broker()

        # For multi-broker mode
        self.processes: Dict[str, Process] = {}
        self.command_queues: Dict[str, Queue] = {}
        self.response_queues: Dict[str, Queue] = {}
        self.market_data_queue: Optional[Queue] = None
        self.shutdown_event: Optional[Event] = None

        # Market data cache (updated from broker processes)
        self.market_data_cache: Dict[str, dict] = {}
        self.cache_lock = threading.Lock()

        # For single-broker mode (direct MT5 connection)
        self.single_broker_connected = False

        logging.info(f"MultiBrokerManager initialized - Multi-broker mode: {self.is_multi_mode}")

    def initialize(self) -> bool:
        """Initialize all broker connections"""
        if self.is_multi_mode:
            return self._initialize_multi_broker()
        else:
            return self._initialize_single_broker()

    def _initialize_single_broker(self) -> bool:
        """Initialize single broker connection (same process)"""
        brokers = self.config.get_enabled_brokers()
        if not brokers:
            logging.error("No brokers configured")
            return False

        broker = brokers[0]
        logging.info(f"Initializing single broker: {broker.name}")

        # Initialize MT5
        init_kwargs = {}
        if broker.mt5_path:
            init_kwargs['path'] = broker.mt5_path
        if broker.login:
            init_kwargs['login'] = broker.login
        if broker.password:
            init_kwargs['password'] = broker.password
        if broker.server:
            init_kwargs['server'] = broker.server

        if init_kwargs:
            if not mt5.initialize(**init_kwargs):
                error = mt5.last_error()
                logging.error(f"MT5 initialization failed: {error}")
                return False
        else:
            if not mt5.initialize():
                error = mt5.last_error()
                logging.error(f"MT5 initialization failed: {error}")
                return False

        # Enable symbols
        for symbol in broker.symbols:
            symbol_info = mt5.symbol_info(symbol)
            if symbol_info:
                if not symbol_info.visible:
                    mt5.symbol_select(symbol, True)
                logging.info(f"Symbol enabled: {symbol}")
            else:
                logging.warning(f"Symbol not found: {symbol}")

        account_info = mt5.account_info()
        if account_info:
            logging.info(f"Connected to: {account_info.server}")
            logging.info(f"Account: {account_info.name}")

        self.single_broker_connected = True
        return True

    def _initialize_multi_broker(self) -> bool:
        """Initialize multiple broker connections (separate processes)"""
        logging.info("Initializing multi-broker mode...")

        self.shutdown_event = Event()
        self.market_data_queue = Queue()

        success_count = 0
        for broker in self.config.get_enabled_brokers():
            # Create queues for this broker
            cmd_queue = Queue()
            resp_queue = Queue()

            self.command_queues[broker.broker_id] = cmd_queue
            self.response_queues[broker.broker_id] = resp_queue

            # Start broker process
            process = Process(
                target=BrokerConnectionProcess.run_broker_process,
                args=(broker.to_dict(), cmd_queue, resp_queue,
                      self.market_data_queue, self.shutdown_event),
                name=f"Broker_{broker.broker_id}"
            )
            process.start()
            self.processes[broker.broker_id] = process

            # Wait for initialization result
            try:
                result = resp_queue.get(timeout=30)
                if result.get('success'):
                    logging.info(f"Broker {broker.name} initialized successfully")
                    logging.info(f"  Available symbols: {result.get('available_symbols')}")
                    success_count += 1
                else:
                    logging.error(f"Broker {broker.name} initialization failed: {result.get('error')}")
            except queue.Empty:
                logging.error(f"Broker {broker.name} initialization timeout")

        if success_count == 0:
            logging.error("No brokers initialized successfully")
            self.shutdown()
            return False

        # Start market data collector thread
        self._start_market_data_collector()

        logging.info(f"Multi-broker initialization complete: {success_count}/{len(self.config.get_enabled_brokers())} brokers")
        return True

    def _start_market_data_collector(self):
        """Start a thread to collect market data from all broker processes"""
        def collector():
            while not self.shutdown_event.is_set():
                try:
                    data = self.market_data_queue.get(timeout=1)
                    if data and data.get('type') == 'market_data':
                        broker_id = data['broker_id']
                        with self.cache_lock:
                            if broker_id not in self.market_data_cache:
                                self.market_data_cache[broker_id] = {}
                            self.market_data_cache[broker_id].update(data['data'])
                except queue.Empty:
                    pass
                except Exception as e:
                    logging.error(f"Market data collector error: {e}")

        self.collector_thread = threading.Thread(target=collector, daemon=True)
        self.collector_thread.start()

    def get_tick(self, symbol: str) -> Optional[dict]:
        """Get tick data for a symbol (works in both modes)"""
        if self.is_multi_mode:
            broker_id = self.config.get_broker_for_symbol(symbol)
            if not broker_id:
                return None
            with self.cache_lock:
                broker_data = self.market_data_cache.get(broker_id, {})
                return broker_data.get(symbol)
        else:
            tick = mt5.symbol_info_tick(symbol)
            if tick:
                return {
                    'bid': tick.bid,
                    'ask': tick.ask,
                    'last': tick.last,
                    'volume': tick.volume,
                    'time': tick.time
                }
            return None

    def get_symbol_info(self, symbol: str):
        """Get symbol info (single broker mode only for now)"""
        if not self.is_multi_mode:
            return mt5.symbol_info(symbol)
        return None

    def execute_order(self, symbol: str, order_type: int, lot_size: float,
                      trade_id: str = None, slippage: int = 10,
                      timeout: float = 10.0) -> dict:
        """
        Execute an order on the appropriate broker.

        Args:
            symbol: Trading symbol
            order_type: mt5.ORDER_TYPE_BUY or mt5.ORDER_TYPE_SELL
            lot_size: Trade volume
            trade_id: Unique trade identifier
            slippage: Allowed slippage in points
            timeout: Timeout for order execution

        Returns:
            dict with 'success', 'order_ticket', 'executed_price', 'error'
        """
        if self.is_multi_mode:
            return self._execute_order_multi(symbol, order_type, lot_size, trade_id, slippage, timeout)
        else:
            return self._execute_order_single(symbol, order_type, lot_size, trade_id, slippage)

    def _execute_order_single(self, symbol: str, order_type: int, lot_size: float,
                               trade_id: str, slippage: int) -> dict:
        """Execute order in single broker mode"""
        try:
            symbol_info = mt5.symbol_info(symbol)
            if not symbol_info:
                return {'success': False, 'error': f'Symbol {symbol} not found'}

            if not symbol_info.visible:
                mt5.symbol_select(symbol, True)

            tick = mt5.symbol_info_tick(symbol)
            if not tick:
                return {'success': False, 'error': f'No tick data for {symbol}'}

            price = tick.ask if order_type == mt5.ORDER_TYPE_BUY else tick.bid
            point = symbol_info.point
            deviation = int(slippage / point) if point > 0 else slippage

            request = {
                "action": mt5.TRADE_ACTION_DEAL,
                "symbol": symbol,
                "volume": lot_size,
                "type": order_type,
                "price": price,
                "deviation": deviation,
                "magic": 12345,
                "comment": f"AlgoTrading_{trade_id or 'unknown'}",
                "type_time": mt5.ORDER_TIME_GTC,
                "type_filling": mt5.ORDER_FILLING_IOC,
            }

            result = mt5.order_send(request)

            if result.retcode != mt5.TRADE_RETCODE_DONE:
                return {
                    'success': False,
                    'error': f'Order failed: {result.retcode} - {result.comment}',
                    'retcode': result.retcode
                }

            return {
                'success': True,
                'order_ticket': result.order,
                'executed_price': result.price,
                'volume': result.volume,
                'symbol': symbol
            }

        except Exception as e:
            return {'success': False, 'error': str(e)}

    def _execute_order_multi(self, symbol: str, order_type: int, lot_size: float,
                              trade_id: str, slippage: int, timeout: float) -> dict:
        """Execute order in multi-broker mode"""
        broker_id = self.config.get_broker_for_symbol(symbol)
        if not broker_id:
            return {'success': False, 'error': f'No broker configured for symbol {symbol}'}

        if broker_id not in self.command_queues:
            return {'success': False, 'error': f'Broker {broker_id} not connected'}

        # Send command to broker process
        command = {
            'type': 'execute_order',
            'symbol': symbol,
            'order_type': order_type,
            'lot_size': lot_size,
            'trade_id': trade_id,
            'slippage': slippage
        }

        self.command_queues[broker_id].put(command)

        # Wait for response
        try:
            response = self.response_queues[broker_id].get(timeout=timeout)
            return response
        except queue.Empty:
            return {'success': False, 'error': 'Order execution timeout'}

    def execute_paired_orders(self, spot_symbol: str, spot_order_type: int,
                               futures_symbol: str, futures_order_type: int,
                               lot_size: float, trade_id: str = None) -> Tuple[dict, dict]:
        """
        Execute paired orders (spot + futures) with coordination.
        For basis trading, both orders should execute as close together as possible.

        Returns:
            Tuple of (spot_result, futures_result)
        """
        spot_broker = self.config.get_broker_for_symbol(spot_symbol)
        futures_broker = self.config.get_broker_for_symbol(futures_symbol)

        if self.is_multi_mode and spot_broker != futures_broker:
            # Different brokers - execute in parallel
            return self._execute_paired_orders_parallel(
                spot_symbol, spot_order_type, futures_symbol, futures_order_type,
                lot_size, trade_id
            )
        else:
            # Same broker - execute sequentially
            spot_result = self.execute_order(spot_symbol, spot_order_type, lot_size,
                                             f"{trade_id}_spot")
            if not spot_result.get('success'):
                return spot_result, {'success': False, 'error': 'Spot order failed, futures not attempted'}

            futures_result = self.execute_order(futures_symbol, futures_order_type, lot_size,
                                                 f"{trade_id}_futures")

            # If futures fails, we may need to reverse spot
            if not futures_result.get('success'):
                logging.error(f"Futures order failed after spot succeeded. Consider reversing spot order.")

            return spot_result, futures_result

    def _execute_paired_orders_parallel(self, spot_symbol: str, spot_order_type: int,
                                         futures_symbol: str, futures_order_type: int,
                                         lot_size: float, trade_id: str) -> Tuple[dict, dict]:
        """Execute paired orders on different brokers in parallel"""
        spot_broker = self.config.get_broker_for_symbol(spot_symbol)
        futures_broker = self.config.get_broker_for_symbol(futures_symbol)

        # Send both commands simultaneously
        spot_cmd = {
            'type': 'execute_order',
            'symbol': spot_symbol,
            'order_type': spot_order_type,
            'lot_size': lot_size,
            'trade_id': f"{trade_id}_spot",
            'slippage': 10
        }

        futures_cmd = {
            'type': 'execute_order',
            'symbol': futures_symbol,
            'order_type': futures_order_type,
            'lot_size': lot_size,
            'trade_id': f"{trade_id}_futures",
            'slippage': 10
        }

        # Send commands to both brokers at nearly the same time
        self.command_queues[spot_broker].put(spot_cmd)
        self.command_queues[futures_broker].put(futures_cmd)

        # Collect responses
        spot_result = None
        futures_result = None
        timeout = 10.0
        start_time = time.time()

        while (spot_result is None or futures_result is None) and \
              (time.time() - start_time) < timeout:

            # Check spot broker response
            if spot_result is None:
                try:
                    resp = self.response_queues[spot_broker].get_nowait()
                    if resp.get('type') == 'order_result':
                        spot_result = resp
                except queue.Empty:
                    pass

            # Check futures broker response
            if futures_result is None:
                try:
                    resp = self.response_queues[futures_broker].get_nowait()
                    if resp.get('type') == 'order_result':
                        futures_result = resp
                except queue.Empty:
                    pass

            if spot_result is None or futures_result is None:
                time.sleep(0.01)

        # Handle timeouts
        if spot_result is None:
            spot_result = {'success': False, 'error': 'Spot order timeout'}
        if futures_result is None:
            futures_result = {'success': False, 'error': 'Futures order timeout'}

        return spot_result, futures_result

    def shutdown(self):
        """Shutdown all broker connections"""
        logging.info("Shutting down MultiBrokerManager...")

        if self.is_multi_mode:
            # Signal all processes to stop
            if self.shutdown_event:
                self.shutdown_event.set()

            # Wait for processes to terminate
            for broker_id, process in self.processes.items():
                logging.info(f"Waiting for broker {broker_id} to shutdown...")
                process.join(timeout=5)
                if process.is_alive():
                    logging.warning(f"Force terminating broker {broker_id}")
                    process.terminate()

            self.processes.clear()
            self.command_queues.clear()
            self.response_queues.clear()
        else:
            mt5.shutdown()
            self.single_broker_connected = False

        logging.info("MultiBrokerManager shutdown complete")


# =============================================================================
# ORIGINAL TRADING SYSTEM CLASSES (Updated for multi-broker support)
# =============================================================================

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
        'RETRY_ATTEMPTS': 3
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

class Trade:
    """Represents a single trade"""
    def __init__(self, symbol, order_type, lot_size, price=None, broker_id=None):
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
        self.broker_id = broker_id  # NEW: Track which broker executed this trade

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

class DataLogger:
    """Database logger for trades and market data"""

    def __init__(self, db_path="algo_trading.db"):
        self.db_path = db_path
        self.init_database()

    def init_database(self):
        """Initialize SQLite database"""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()

        # Trades table (updated with broker_id)
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
                error_message TEXT,
                broker_id TEXT
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

        # Broker connections table (NEW)
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS broker_connections (
                broker_id TEXT PRIMARY KEY,
                name TEXT,
                mt5_path TEXT,
                server TEXT,
                symbols TEXT,
                last_connected TEXT,
                status TEXT
            )
        ''')

        conn.commit()
        conn.close()

    def log_trade(self, trade, position_id=None):
        """Log trade to database"""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()

        cursor.execute('''
            INSERT OR REPLACE INTO trades VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', (
            trade.trade_id, position_id, trade.symbol, str(trade.order_type),
            trade.lot_size, trade.requested_price, trade.executed_price,
            trade.order_ticket, trade.status, trade.timestamp.isoformat(),
            trade.execution_time.isoformat() if trade.execution_time else None,
            trade.error_message, getattr(trade, 'broker_id', None)
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
    """Handles order execution through MultiBrokerManager"""

    def __init__(self, config, broker_manager: MultiBrokerManager):
        self.config = config
        self.broker_manager = broker_manager
        self.pending_orders = {}

    def execute_market_order(self, trade) -> bool:
        """Execute market order through broker manager"""
        try:
            result = self.broker_manager.execute_order(
                symbol=trade.symbol,
                order_type=trade.order_type,
                lot_size=trade.lot_size,
                trade_id=trade.trade_id,
                slippage=int(self.config.EXECUTION['SLIPPAGE_TOLERANCE'])
            )

            if not result.get('success'):
                trade.status = "ERROR"
                trade.error_message = result.get('error', 'Unknown error')
                logging.error(f"Order execution failed: {trade.symbol} - {trade.error_message}")
                return False

            trade.order_ticket = result.get('order_ticket')
            trade.executed_price = result.get('executed_price')
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
            trade_id = str(uuid.uuid4())[:8]
            spot_trade = Trade(spot_symbol, spot_order_type, lot_size)
            spot_trade.broker_id = self.broker_manager.config.get_broker_for_symbol(spot_symbol)
            futures_trade = Trade(futures_symbol, futures_order_type, lot_size)
            futures_trade.broker_id = self.broker_manager.config.get_broker_for_symbol(futures_symbol)

            # Execute paired orders (handles both same-broker and multi-broker scenarios)
            spot_result, futures_result = self.broker_manager.execute_paired_orders(
                spot_symbol, spot_order_type,
                futures_symbol, futures_order_type,
                lot_size, trade_id
            )

            # Update spot trade
            if spot_result.get('success'):
                spot_trade.order_ticket = spot_result.get('order_ticket')
                spot_trade.executed_price = spot_result.get('executed_price')
                spot_trade.status = "EXECUTED"
                spot_trade.execution_time = datetime.now()
            else:
                spot_trade.status = "ERROR"
                spot_trade.error_message = spot_result.get('error')

            # Update futures trade
            if futures_result.get('success'):
                futures_trade.order_ticket = futures_result.get('order_ticket')
                futures_trade.executed_price = futures_result.get('executed_price')
                futures_trade.status = "EXECUTED"
                futures_trade.execution_time = datetime.now()
            else:
                futures_trade.status = "ERROR"
                futures_trade.error_message = futures_result.get('error')

            # Check if both succeeded
            if spot_result.get('success') and futures_result.get('success'):
                logging.info(f"Trade pair executed successfully: {asset} {signal_type.value}")
                return True, spot_trade, futures_trade

            # Handle partial execution
            if spot_result.get('success') and not futures_result.get('success'):
                logging.error("Futures trade failed after spot succeeded - attempting reversal")
                reverse_type = mt5.ORDER_TYPE_SELL if spot_order_type == mt5.ORDER_TYPE_BUY else mt5.ORDER_TYPE_BUY
                self.broker_manager.execute_order(spot_symbol, reverse_type, lot_size, f"{trade_id}_reversal")

            return False, spot_trade, futures_trade

        except Exception as e:
            logging.error(f"Trade pair execution error: {e}")
            if spot_trade:
                spot_trade.status = "ERROR"
                spot_trade.error_message = str(e)
            if futures_trade:
                futures_trade.status = "ERROR"
                futures_trade.error_message = str(e)
            return False, spot_trade, futures_trade

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
    """Main algorithmic trading system with multi-broker support"""

    def __init__(self, trading_mode="PAPER", broker_config: MultiBrokerConfig = None):
        self.config = AlgoTradingConfig()
        self.trading_mode = trading_mode  # "PAPER" or "LIVE"
        self.is_running = False

        # Multi-broker configuration
        self.broker_config = broker_config
        self.broker_manager: Optional[MultiBrokerManager] = None

        # Initialize components (order_manager created after broker_manager)
        self.data_logger = DataLogger()
        self.order_manager = None  # Set after broker_manager initialization
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

    def configure_brokers(self):
        """Interactive broker configuration"""
        print("\n" + "="*84)
        print("MULTI-BROKER CONFIGURATION")
        print("="*84)
        print("\nThis system supports multiple MT5 broker connections.")
        print("You can use the same broker for all symbols, or different brokers for spot/futures.")
        print("-"*84)

        # Ask about broker setup
        print("\nBroker Setup Options:")
        print("1. Single broker (all symbols on one MT5 terminal)")
        print("2. Multiple brokers (different terminals for spot/futures)")

        while True:
            choice = input("\nSelect option (1 or 2, default: 1): ").strip()
            if choice == "" or choice == "1":
                self.broker_config = self._configure_single_broker()
                break
            elif choice == "2":
                self.broker_config = self._configure_multi_broker()
                break
            else:
                print("Please enter 1 or 2")

        print("\n" + "="*84)
        print("Broker configuration complete!")
        print("="*84)

    def _configure_single_broker(self) -> MultiBrokerConfig:
        """Configure single broker setup"""
        config = MultiBrokerConfig()

        print("\n--- Single Broker Configuration ---")

        # Get broker name
        broker_name = input("Broker name (default: MyBroker): ").strip()
        if not broker_name:
            broker_name = "MyBroker"

        # Get MT5 path (optional)
        print("\nMT5 Terminal Path (leave empty for default installation):")
        print("Example: C:/Program Files/MetaTrader 5/terminal64.exe")
        mt5_path = input("MT5 path: ").strip()
        if not mt5_path:
            mt5_path = None

        # Collect all symbols
        all_symbols = []
        for asset_key, asset_config in self.config.ASSETS.items():
            if asset_config['enabled']:
                all_symbols.extend(asset_config['spot_symbols'][:1])  # First spot symbol
                all_symbols.extend(asset_config['futures_symbols'][:1])  # First futures symbol

        broker = BrokerConfig(
            broker_id="main",
            name=broker_name,
            mt5_path=mt5_path,
            symbols=all_symbols
        )
        config.add_broker(broker)

        print(f"\nConfigured single broker: {broker_name}")
        print(f"Symbols: {all_symbols}")

        return config

    def _configure_multi_broker(self) -> MultiBrokerConfig:
        """Configure multiple broker setup"""
        config = MultiBrokerConfig()

        print("\n--- Multi-Broker Configuration ---")
        print("You'll configure separate brokers for spot and futures trading.")

        # Spot broker
        print("\n[SPOT BROKER]")
        spot_name = input("Spot broker name (default: SpotBroker): ").strip() or "SpotBroker"
        print("MT5 path for spot broker (required for multi-broker):")
        print("Example: C:/MT5_Broker1/terminal64.exe")
        spot_path = input("Spot MT5 path: ").strip()

        spot_symbols = []
        for asset_key, asset_config in self.config.ASSETS.items():
            if asset_config['enabled']:
                spot_symbols.extend(asset_config['spot_symbols'][:1])

        spot_broker = BrokerConfig(
            broker_id="spot_broker",
            name=spot_name,
            mt5_path=spot_path if spot_path else None,
            symbols=spot_symbols
        )
        config.add_broker(spot_broker)

        # Futures broker
        print("\n[FUTURES BROKER]")
        futures_name = input("Futures broker name (default: FuturesBroker): ").strip() or "FuturesBroker"
        print("MT5 path for futures broker:")
        print("Example: C:/MT5_Broker2/terminal64.exe")
        futures_path = input("Futures MT5 path: ").strip()

        futures_symbols = []
        for asset_key, asset_config in self.config.ASSETS.items():
            if asset_config['enabled']:
                futures_symbols.extend(asset_config['futures_symbols'][:1])

        futures_broker = BrokerConfig(
            broker_id="futures_broker",
            name=futures_name,
            mt5_path=futures_path if futures_path else None,
            symbols=futures_symbols
        )
        config.add_broker(futures_broker)

        print(f"\nConfigured brokers:")
        print(f"  Spot: {spot_name} -> {spot_symbols}")
        print(f"  Futures: {futures_name} -> {futures_symbols}")

        return config

    def get_swap_charges(self):
        """Get swap charges from user"""
        print("\n" + "="*84)
        print("SWAP CONFIGURATION")
        print("="*84)
        print("Enter daily swap charges for long positions (USD per lot per day)")
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
        """Initialize MT5 connection(s) through broker manager"""
        logging.info("Initializing broker connections...")

        # Create broker manager
        self.broker_manager = MultiBrokerManager(self.broker_config)

        if not self.broker_manager.initialize():
            logging.error("Failed to initialize broker connections")
            return False

        # Create order manager with broker manager
        self.order_manager = OrderManager(self.config, self.broker_manager)

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
                if self.broker_manager.is_multi_mode:
                    # In multi-mode, check if symbol is configured
                    broker_id = self.broker_config.get_broker_for_symbol(symbol)
                    if broker_id:
                        spot_symbol = symbol
                        break
                else:
                    # In single mode, check MT5 directly
                    symbol_info = mt5.symbol_info(symbol)
                    if symbol_info:
                        if not symbol_info.visible:
                            mt5.symbol_select(symbol, True)
                        spot_symbol = symbol
                        break

            # Find available futures symbol
            for symbol in asset_config['futures_symbols']:
                if self.broker_manager.is_multi_mode:
                    broker_id = self.broker_config.get_broker_for_symbol(symbol)
                    if broker_id:
                        futures_symbol = symbol
                        break
                else:
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
                    'spot_broker': self.broker_config.get_broker_for_symbol(spot_symbol),
                    'futures_broker': self.broker_config.get_broker_for_symbol(futures_symbol),
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

    def get_market_data(self, asset_key):
        """Get market data for specific asset"""
        if asset_key not in self.active_assets:
            return None

        try:
            asset = self.active_assets[asset_key]
            spot_symbol = asset['spot_symbol']
            futures_symbol = asset['futures_symbol']
            config = asset['config']

            # Get tick data through broker manager
            spot_tick = self.broker_manager.get_tick(spot_symbol)
            futures_tick = self.broker_manager.get_tick(futures_symbol)

            if not spot_tick or not futures_tick:
                return None

            # Handle price multipliers
            multiplier = config.get('multiplier', 1.0)

            # Use mid-price when last price is 0
            spot_price = spot_tick['last'] if spot_tick['last'] > 0 else (spot_tick['bid'] + spot_tick['ask']) / 2
            futures_price = (futures_tick['last'] if futures_tick['last'] > 0 else (futures_tick['bid'] + futures_tick['ask']) / 2) * multiplier

            # Calculate spreads
            if asset_key in ['GOLD', 'SILVER']:
                spot_spread = (spot_tick['ask'] - spot_tick['bid']) * 100  # cents
                futures_spread = (futures_tick['ask'] - futures_tick['bid']) * 100  # cents
                spread_unit = '¢'
            else:
                spot_spread = (spot_tick['ask'] - spot_tick['bid']) * 100
                futures_spread = (futures_tick['ask'] - futures_tick['bid']) * 100
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
                'spot_bid': spot_tick['bid'],
                'spot_ask': spot_tick['ask'],
                'futures_bid': futures_tick['bid'] * multiplier,
                'futures_ask': futures_tick['ask'] * multiplier,
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

            # Update P&L for active positions
            for position_id, position in active_positions.items():
                self.position_manager.update_position_pnl(
                    position_id,
                    market_data['spot_price'],
                    market_data['futures_price'],
                    market_data['swap_premium_pct']
                )

                # Check for risk management actions
                needs_action, action_type = self.risk_manager.check_position_risk(
                    position, market_data['swap_premium_pct']
                )

                if needs_action:
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
        broker_mode = "MULTI-BROKER" if self.broker_config.is_multi_broker() else "SINGLE-BROKER"
        print(f"ALGORITHMIC BASIS TRADING SYSTEM - GOLD & SILVER [{broker_mode}]")
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

        # Broker status (for multi-broker mode)
        if self.broker_config.is_multi_broker():
            self.print_broker_status()

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

        # Get broker info
        asset = self.active_assets[asset_key]
        spot_broker = asset.get('spot_broker', 'main')
        futures_broker = asset.get('futures_broker', 'main')

        print(f"{market_data['asset_name']}")
        print("=" * 84)

        if self.broker_config.is_multi_broker():
            print(f"SPOT       | {spot_price_str} | Bid: {market_data['spot_bid']:>10.4f} | Ask: {market_data['spot_ask']:>10.4f} | [{spot_broker}]")
            print(f"FUTURES    | {futures_price_str} | Bid: {market_data['futures_bid']:>10.4f} | Ask: {market_data['futures_ask']:>10.4f} | [{futures_broker}]")
        else:
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
        print(f"Exit Thresholds:  Premium ≤{self.config.SIGNAL_THRESHOLDS['PREMIUM_EXIT']:+.0f}% | Discount ≥{self.config.SIGNAL_THRESHOLDS['DISCOUNT_EXIT']:+.0f}%")

        # Risk status
        risk_status = "NORMAL"
        if len(active_positions) >= len(self.active_assets) * self.config.RISK_LIMITS['MAX_POSITIONS_PER_ASSET']:
            risk_status = "MAX_POSITIONS"
        elif metrics['daily_trades'] >= self.config.RISK_LIMITS['MAX_DAILY_TRADES'] * 0.8:
            risk_status = "HIGH_FREQUENCY"

        print(f"Risk Status: {risk_status} | Mode: {self.trading_mode} | Updated: {datetime.now().strftime('%H:%M:%S')}")
        print("=" * 84)

    def print_broker_status(self):
        """Print broker connection status (multi-broker mode only)"""
        print("\nBROKER CONNECTIONS")
        print("-" * 84)
        for broker in self.broker_config.get_enabled_brokers():
            symbols_str = ", ".join(broker.symbols[:3])
            if len(broker.symbols) > 3:
                symbols_str += f"... (+{len(broker.symbols)-3})"
            print(f"  {broker.broker_id}: {broker.name} | Symbols: {symbols_str}")
        print("-" * 84)

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

                # Check MT5 connection periodically (single broker mode only)
                if not self.broker_config.is_multi_broker() and loop_count % 100 == 0:
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
                    print("Reinitializing broker connections...")

                    self.broker_manager.shutdown()
                    if self.broker_manager.initialize():
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
        self.configure_brokers()
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
        print(f"Broker Mode: {'MULTI-BROKER' if self.broker_config.is_multi_broker() else 'SINGLE-BROKER'}")
        print("\nActive Assets:")
        for asset_key, asset_data in self.active_assets.items():
            swap_charge = self.config.ASSETS[asset_key]['swap_charge']
            if self.broker_config.is_multi_broker():
                print(f"  {asset_key}:")
                print(f"    Spot: {asset_data['spot_symbol']} [{asset_data['spot_broker']}]")
                print(f"    Futures: {asset_data['futures_symbol']} [{asset_data['futures_broker']}]")
                print(f"    Swap: ${swap_charge:.2f}/day")
            else:
                print(f"  {asset_key}: {asset_data['spot_symbol']} + {asset_data['futures_symbol']} | Swap: ${swap_charge:.2f}/day")

        print("\nTrading Strategy:")
        print(f"  • SELL BASIS: Premium >{self.config.SIGNAL_THRESHOLDS['PREMIUM_ENTRY']:+.0f}% → Exit ≤{self.config.SIGNAL_THRESHOLDS['PREMIUM_EXIT']:+.0f}%")
        print(f"  • BUY BASIS:  Discount <{self.config.SIGNAL_THRESHOLDS['DISCOUNT_ENTRY']:+.0f}% → Exit ≥{self.config.SIGNAL_THRESHOLDS['DISCOUNT_EXIT']:+.0f}%")

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
                self.position_manager.close_position(position_id, "SYSTEM_SHUTDOWN", self.order_manager)

        # Shutdown broker manager
        if self.broker_manager:
            self.broker_manager.shutdown()

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
    print("\n*** MULTI-BROKER SUPPORT ***")
    print("  • Single broker mode: All symbols on one MT5 terminal")
    print("  • Multi-broker mode: Different brokers for spot vs futures")
    print("  • Automatic parallel execution when using multiple brokers")
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
    print("\nTrading Logic:")
    print("  • SELL BASIS when premium >20% (auto: buy spot, sell futures)")
    print("  • BUY BASIS when discount <-15% (auto: buy futures, sell spot)")
    print("  • Exit when premium/discount normalizes (automatic closure)")
    print("  • Stop loss and position limits protect capital")
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
