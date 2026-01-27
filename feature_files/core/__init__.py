"""
Core trading modules for the Multi-Broker Arbitrage System.
"""

from .trading_engine import TradingEngine, EngineState, MarketData, Signal
from .ipc import IPCManager, IPCMessage, MessageType, CommandType
from .multi_broker import (
    MultiBrokerCoordinator,
    BrokerConfig,
    BrokerRole,
    ExecutionMode,
    OrderSide,
    TradeResult,
    BasisTradeResult,
    MarketTick,
    create_coordinator_from_database
)

__all__ = [
    # Trading Engine
    'TradingEngine',
    'EngineState',
    'MarketData',
    'Signal',
    # IPC
    'IPCManager',
    'IPCMessage',
    'MessageType',
    'CommandType',
    # Multi-Broker
    'MultiBrokerCoordinator',
    'BrokerConfig',
    'BrokerRole',
    'ExecutionMode',
    'OrderSide',
    'TradeResult',
    'BasisTradeResult',
    'MarketTick',
    'create_coordinator_from_database',
]
