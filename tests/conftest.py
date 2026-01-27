"""
Pytest configuration and fixtures for Statistical Arbitrage tests.
"""

import pytest
import asyncio
import sys
from pathlib import Path
from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch

# AsyncMock is only available in Python 3.8+
# Provide a fallback for Python 3.7
try:
    from unittest.mock import AsyncMock
except ImportError:
    # For Python < 3.8, create a simple AsyncMock implementation
    class AsyncMock(MagicMock):
        async def __call__(self, *args, **kwargs):
            return super().__call__(*args, **kwargs)

        def __await__(self):
            return self().__await__()

# Add parent directory to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent.parent / "feature_files"))


@pytest.fixture
def event_loop():
    """Create an instance of the default event loop for each test case."""
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()


@pytest.fixture
def mock_trading_config():
    """Create a mock TradingConfig for testing."""
    from feature_files.models import TradingConfig
    return TradingConfig(
        id=1,
        asset_name="GOLD",
        spot_symbol="BTC-USDT",
        futures_symbol="BTC-USDT-SWAP",
        contract_size=100.0,
        swap_charge=0.0,
        lookback_period=90,
        lookback_unit="minutes",
        entry_std_dev=2.0,
        exit_std_dev=0.5,
        stop_loss_std_dev=3.0,
        max_positions=3,
        lot_size=0.1,
        commission_per_lot=7.0,
        hurst_enabled=True,
        hurst_threshold=0.5,
        std_filter_enabled=True,
        spot_spread_cost=0.40,
        futures_spread_cost=0.10,
        profit_margin=1.5,
        order_type="MARKET",
        limit_order_timeout=60,
        limit_peg_interval=1.5,
        paper_mode=True
    )


@pytest.fixture
def mock_broker_config_spot():
    """Create a mock BrokerConfig for spot trading."""
    from feature_files.base import BrokerConfig
    return BrokerConfig(
        broker_id="spot_broker_1",
        name="OKX Spot",
        role="SPOT",
        backend_type="OKX",
        symbol="BTC-USDT",
        okx_api_key="",  # Empty for mock mode
        okx_api_secret="",
        okx_passphrase="",
        okx_simulated=True,
        okx_account_type="spot",
        contract_size=1.0,
        commission_per_lot=0.001
    )


@pytest.fixture
def mock_broker_config_futures():
    """Create a mock BrokerConfig for futures trading."""
    from feature_files.base import BrokerConfig
    return BrokerConfig(
        broker_id="futures_broker_1",
        name="OKX Futures",
        role="FUTURES",
        backend_type="OKX",
        symbol="BTC-USDT-SWAP",
        okx_api_key="",  # Empty for mock mode
        okx_api_secret="",
        okx_passphrase="",
        okx_simulated=True,
        okx_account_type="swap",
        contract_size=100.0,
        commission_per_lot=0.001
    )


@pytest.fixture
def mock_broker_config_unified():
    """Create a mock BrokerConfig for unified (same account) trading."""
    from feature_files.base import BrokerConfig
    return BrokerConfig(
        broker_id="unified_broker",
        name="OKX Unified",
        role="UNIFIED",
        backend_type="OKX",
        unified_mode=True,
        spot_symbol="BTC-USDT",
        futures_symbol="BTC-USDT-SWAP",
        symbol="BTC-USDT",
        okx_api_key="",  # Empty for mock mode
        okx_api_secret="",
        okx_passphrase="",
        okx_simulated=True,
        okx_account_type="spot",
        contract_size=100.0,
        commission_per_lot=0.001
    )


@pytest.fixture
def signal_generator():
    """Create a SignalGenerator instance for testing."""
    from feature_files.core.signals import SignalGenerator
    return SignalGenerator(
        lookback_period=90,
        lookback_unit="minutes",
        entry_threshold=2.0,
        exit_threshold=0.5,
        stop_loss_threshold=3.0,
        hurst_enabled=True,
        hurst_threshold=0.5,
        hurst_window=100,
        std_filter_enabled=True,
        lot_size=0.1,
        contract_size=100.0,
        spot_spread_cost=0.40,
        futures_spread_cost=0.10,
        commission_per_lot=7.0,
        swap_cost_per_day=0.0,
        profit_margin=1.5
    )


@pytest.fixture
def sample_spread_data():
    """Generate sample spread data for testing."""
    import numpy as np

    # Generate mean-reverting spread data
    np.random.seed(42)
    n_points = 100

    # Base spread with mean 50 and std ~5
    base_mean = 50.0
    base_std = 5.0

    spreads = []
    timestamps = []
    base_time = datetime.now() - timedelta(minutes=100)

    current_spread = base_mean
    for i in range(n_points):
        # Mean-reverting random walk
        shock = np.random.normal(0, base_std * 0.3)
        reversion = 0.1 * (base_mean - current_spread)
        current_spread = current_spread + shock + reversion

        spreads.append(current_spread)
        timestamps.append(base_time + timedelta(minutes=i))

    return spreads, timestamps
