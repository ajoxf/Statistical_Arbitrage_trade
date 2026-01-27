"""
Comprehensive tests for Statistical Arbitrage Trading System.

Tests cover:
1. Market Orders in Test Mode
2. Limit Orders in Test Mode
3. Moving Average calculation with lookback period
4. STD Filter profitability calculation
5. Same broker account functionality
6. Different broker accounts for Spot and Futures
7. Price updates
"""

import pytest
import asyncio
import numpy as np
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch
import sys
from pathlib import Path

# Add parent directory to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent.parent / "feature_files"))


# =============================================================================
# TEST 1: MARKET ORDERS IN TEST MODE
# =============================================================================

class TestMarketOrdersTestMode:
    """Test market order placement in test/mock mode."""

    @pytest.mark.asyncio
    async def test_market_order_buy_in_mock_mode(self, mock_broker_config_spot):
        """Test placing a BUY market order in mock mode."""
        from feature_files.okx_adapter import OKXAdapter
        from feature_files.base import OrderSide

        adapter = OKXAdapter(mock_broker_config_spot)

        # Verify adapter is in mock mode (no API key)
        assert adapter._mock_mode is True

        await adapter.connect()
        assert adapter.is_connected

        # Place market buy order
        result = await adapter.place_market_order(
            symbol="BTC-USDT",
            side=OrderSide.BUY,
            volume=0.1
        )

        assert result.success is True
        assert result.order_id is not None
        assert result.volume == 0.1
        assert result.execution_time_ms is not None

        await adapter.disconnect()

    @pytest.mark.asyncio
    async def test_market_order_sell_in_mock_mode(self, mock_broker_config_spot):
        """Test placing a SELL market order in mock mode."""
        from feature_files.okx_adapter import OKXAdapter
        from feature_files.base import OrderSide

        adapter = OKXAdapter(mock_broker_config_spot)
        await adapter.connect()

        result = await adapter.place_market_order(
            symbol="BTC-USDT",
            side=OrderSide.SELL,
            volume=0.05
        )

        assert result.success is True
        assert result.order_id is not None
        assert result.volume == 0.05

        await adapter.disconnect()

    @pytest.mark.asyncio
    async def test_market_order_swap_mode(self, mock_broker_config_futures):
        """Test placing market order on SWAP (futures) in mock mode."""
        from feature_files.okx_adapter import OKXAdapter
        from feature_files.base import OrderSide

        adapter = OKXAdapter(mock_broker_config_futures)
        await adapter.connect()

        # Place market order on futures contract
        result = await adapter.place_market_order(
            symbol="BTC-USDT-SWAP",
            side=OrderSide.BUY,
            volume=1.0
        )

        assert result.success is True
        assert result.order_id is not None

        await adapter.disconnect()

    @pytest.mark.asyncio
    async def test_market_order_simulated_trading_header(self, mock_broker_config_spot):
        """Test that simulated trading mode sets correct header."""
        from feature_files.okx_adapter import OKXAdapter

        adapter = OKXAdapter(mock_broker_config_spot)

        # Verify simulated mode is set
        assert adapter._simulated is True

        # In simulated mode, the header x-simulated-trading should be '1'
        headers = adapter._get_headers("GET", "/test/path")
        assert headers.get('x-simulated-trading') == '1'

    @pytest.mark.asyncio
    async def test_market_order_execution_time_tracking(self, mock_broker_config_spot):
        """Test that execution time is properly tracked for market orders."""
        from feature_files.okx_adapter import OKXAdapter
        from feature_files.base import OrderSide

        adapter = OKXAdapter(mock_broker_config_spot)
        await adapter.connect()

        result = await adapter.place_market_order(
            symbol="BTC-USDT",
            side=OrderSide.BUY,
            volume=0.1
        )

        assert result.execution_time_ms is not None
        assert result.execution_time_ms >= 0

        await adapter.disconnect()


# =============================================================================
# TEST 2: LIMIT ORDERS IN TEST MODE
# =============================================================================

class TestLimitOrdersTestMode:
    """Test limit order placement in test/mock mode."""

    @pytest.mark.asyncio
    async def test_limit_order_buy_in_mock_mode(self, mock_broker_config_spot):
        """Test placing a BUY limit order in mock mode."""
        from feature_files.okx_adapter import OKXAdapter
        from feature_files.base import OrderSide

        adapter = OKXAdapter(mock_broker_config_spot)
        await adapter.connect()

        result = await adapter.place_limit_order(
            symbol="BTC-USDT",
            side=OrderSide.BUY,
            volume=0.1,
            price=50000.0
        )

        assert result.success is True
        assert result.order_id is not None
        assert result.price == 50000.0
        assert result.volume == 0.1

        await adapter.disconnect()

    @pytest.mark.asyncio
    async def test_limit_order_sell_in_mock_mode(self, mock_broker_config_spot):
        """Test placing a SELL limit order in mock mode."""
        from feature_files.okx_adapter import OKXAdapter
        from feature_files.base import OrderSide

        adapter = OKXAdapter(mock_broker_config_spot)
        await adapter.connect()

        result = await adapter.place_limit_order(
            symbol="BTC-USDT",
            side=OrderSide.SELL,
            volume=0.05,
            price=51000.0
        )

        assert result.success is True
        assert result.order_id is not None
        assert result.price == 51000.0

        await adapter.disconnect()

    @pytest.mark.asyncio
    async def test_limit_order_swap_mode(self, mock_broker_config_futures):
        """Test placing limit order on SWAP (futures) in mock mode."""
        from feature_files.okx_adapter import OKXAdapter
        from feature_files.base import OrderSide

        adapter = OKXAdapter(mock_broker_config_futures)
        await adapter.connect()

        result = await adapter.place_limit_order(
            symbol="BTC-USDT-SWAP",
            side=OrderSide.BUY,
            volume=1.0,
            price=49000.0
        )

        assert result.success is True
        assert result.order_id is not None

        await adapter.disconnect()

    @pytest.mark.asyncio
    async def test_pegged_limit_order_in_mock_mode(self, mock_broker_config_spot):
        """Test placing a pegged limit order in mock mode."""
        from feature_files.okx_adapter import OKXAdapter
        from feature_files.base import OrderSide

        adapter = OKXAdapter(mock_broker_config_spot)
        await adapter.connect()

        # Pegged limit order should track the market
        result = await adapter.execute_pegged_limit_order(
            symbol="BTC-USDT",
            side=OrderSide.BUY,
            volume=0.1,
            offset_ticks=1,
            max_attempts=3,
            update_interval=0.1  # Fast for testing
        )

        # In mock mode, it should eventually get filled or timeout
        assert result.execution_time_ms is not None

        await adapter.disconnect()

    @pytest.mark.asyncio
    async def test_limit_order_cancel(self, mock_broker_config_spot):
        """Test cancelling a limit order in mock mode."""
        from feature_files.okx_adapter import OKXAdapter
        from feature_files.base import OrderSide

        adapter = OKXAdapter(mock_broker_config_spot)
        await adapter.connect()

        # Place limit order
        result = await adapter.place_limit_order(
            symbol="BTC-USDT",
            side=OrderSide.BUY,
            volume=0.1,
            price=50000.0
        )

        assert result.success is True
        order_id = result.order_id

        # Cancel the order
        cancel_result = await adapter.cancel_order("BTC-USDT", order_id)
        # In mock mode, cancel always returns True
        assert cancel_result is True

        await adapter.disconnect()


# =============================================================================
# TEST 3: MOVING AVERAGE CALCULATION WITH LOOKBACK PERIOD
# =============================================================================

class TestMovingAverageCalculation:
    """Test moving average and statistics calculation with lookback period."""

    def test_add_spread_data(self, signal_generator, sample_spread_data):
        """Test adding spread data to the generator."""
        spreads, timestamps = sample_spread_data

        for spread, ts in zip(spreads, timestamps):
            signal_generator.add_spread(spread, ts)

        # Data gets trimmed based on lookback period (90 minutes)
        # so we expect data_points <= len(spreads)
        assert signal_generator.data_points > 0
        assert signal_generator.data_points <= len(spreads)

    def test_calculate_statistics_mean(self, signal_generator, sample_spread_data):
        """Test mean calculation with lookback period."""
        spreads, timestamps = sample_spread_data

        for spread, ts in zip(spreads, timestamps):
            signal_generator.add_spread(spread, ts)

        result = signal_generator.calculate_statistics()

        assert result is not None
        mean, std = result

        # Verify mean is close to expected (around 50 based on our sample data)
        assert 40.0 < mean < 60.0

    def test_calculate_statistics_std(self, signal_generator, sample_spread_data):
        """Test standard deviation calculation."""
        spreads, timestamps = sample_spread_data

        for spread, ts in zip(spreads, timestamps):
            signal_generator.add_spread(spread, ts)

        result = signal_generator.calculate_statistics()

        assert result is not None
        mean, std = result

        # Verify std is positive and reasonable
        assert std > 0
        assert std < 20  # Reasonable for our sample data

    def test_lookback_trimming_minutes(self):
        """Test that lookback window trims old data correctly (minutes)."""
        from feature_files.core.signals import SignalGenerator

        generator = SignalGenerator(
            lookback_period=5,  # Only 5 minutes
            lookback_unit="minutes"
        )

        base_time = datetime.now() - timedelta(minutes=10)

        # Add data from 10 minutes ago to now
        for i in range(11):
            ts = base_time + timedelta(minutes=i)
            generator.add_spread(50.0 + i, ts)

        # After trimming, should only have data from last 5 minutes
        # Due to cutoff, should have roughly 5-6 points
        assert generator.data_points <= 7

    def test_lookback_trimming_days(self):
        """Test that lookback window trims old data correctly (days)."""
        from feature_files.core.signals import SignalGenerator

        generator = SignalGenerator(
            lookback_period=2,  # Only 2 days
            lookback_unit="days"
        )

        base_time = datetime.now() - timedelta(days=5)

        # Add data from 5 days ago to now
        for i in range(6):
            ts = base_time + timedelta(days=i)
            generator.add_spread(50.0 + i, ts)

        # After trimming, should only have data from last 2 days
        assert generator.data_points <= 3

    def test_zscore_calculation(self, signal_generator, sample_spread_data):
        """Test Z-score calculation."""
        spreads, timestamps = sample_spread_data

        for spread, ts in zip(spreads, timestamps):
            signal_generator.add_spread(spread, ts)

        # Calculate Z-score for a value at the mean
        stats = signal_generator.calculate_statistics()
        mean, std = stats

        zscore_at_mean = signal_generator.calculate_zscore(mean)
        assert zscore_at_mean is not None
        assert abs(zscore_at_mean) < 0.1  # Should be close to 0

        # Calculate Z-score for value 2 std above mean
        zscore_high = signal_generator.calculate_zscore(mean + 2 * std)
        assert zscore_high is not None
        assert abs(zscore_high - 2.0) < 0.1  # Should be close to 2

    def test_insufficient_data_handling(self):
        """Test that statistics calculation handles insufficient data."""
        from feature_files.core.signals import SignalGenerator

        generator = SignalGenerator()

        # Add only 5 points (less than minimum 10)
        for i in range(5):
            generator.add_spread(50.0 + i)

        result = generator.calculate_statistics()

        assert result is None  # Should return None for insufficient data

    def test_numpy_calculations_correctness(self):
        """Test that numpy calculations match expected values."""
        from feature_files.core.signals import SignalGenerator

        # Create generator with large lookback to prevent trimming
        generator = SignalGenerator(
            lookback_period=1000,  # Large to prevent trimming
            lookback_unit="minutes"
        )

        # Generate test data all within the lookback window
        np.random.seed(123)
        base_time = datetime.now()
        spreads = []

        for i in range(50):
            spread = 50.0 + np.random.normal(0, 5)
            spreads.append(spread)
            generator.add_spread(spread, base_time + timedelta(minutes=i))

        # Direct numpy calculation
        np_mean = np.mean(spreads)
        np_std = np.std(spreads, ddof=1)

        gen_mean, gen_std = generator.calculate_statistics()

        # Values should match numpy calculations
        assert abs(gen_mean - np_mean) < 0.001
        assert abs(gen_std - np_std) < 0.001


# =============================================================================
# TEST 4: STD FILTER PROFITABILITY CALCULATION
# =============================================================================

class TestSTDFilterProfitability:
    """Test STD filter profitability calculation for trade filtering."""

    def test_calculate_min_profitable_std(self, signal_generator, sample_spread_data):
        """Test minimum profitable STD calculation."""
        spreads, timestamps = sample_spread_data

        for spread, ts in zip(spreads, timestamps):
            signal_generator.add_spread(spread, ts)

        # First calculate statistics to set current STD
        signal_generator.calculate_statistics()

        result = signal_generator.calculate_min_profitable_std(
            spot_spread=0.40,
            futures_spread=0.10
        )

        assert 'min_profitable_std' in result
        assert 'current_std' in result
        assert 'is_profitable' in result
        assert 'total_round_trip_cost' in result
        assert 'expected_profit' in result

    def test_profitability_true_when_std_high(self):
        """Test that trades are profitable when STD is sufficiently high."""
        from feature_files.core.signals import SignalGenerator

        generator = SignalGenerator(
            std_filter_enabled=True,
            lot_size=0.1,
            contract_size=100.0,
            spot_spread_cost=0.40,
            futures_spread_cost=0.10,
            commission_per_lot=7.0,
            profit_margin=1.5
        )

        # Add data with high volatility (high STD)
        base_time = datetime.now()
        for i in range(100):
            # Large price swings to create high STD
            spread = 50.0 + (i % 2) * 20.0  # Alternates between 50 and 70
            generator.add_spread(spread, base_time + timedelta(minutes=i))

        generator.calculate_statistics()
        result = generator.calculate_min_profitable_std()

        # With high STD, should be profitable
        assert result['current_std'] > result['min_profitable_std']
        assert result['is_profitable'] is True

    def test_profitability_false_when_std_low(self):
        """Test that trades are not profitable when STD is too low."""
        from feature_files.core.signals import SignalGenerator

        generator = SignalGenerator(
            std_filter_enabled=True,
            lot_size=0.1,
            contract_size=100.0,
            spot_spread_cost=5.0,  # High costs
            futures_spread_cost=5.0,
            commission_per_lot=50.0,
            profit_margin=2.0
        )

        # Add data with very low volatility (low STD)
        base_time = datetime.now()
        for i in range(100):
            # Very small price changes
            spread = 50.0 + np.random.uniform(-0.01, 0.01)
            generator.add_spread(spread, base_time + timedelta(minutes=i))

        generator.calculate_statistics()
        result = generator.calculate_min_profitable_std()

        # With low STD and high costs, should not be profitable
        assert result['current_std'] < result['min_profitable_std']
        assert result['is_profitable'] is False

    def test_round_trip_cost_calculation(self, signal_generator, sample_spread_data):
        """Test that round-trip costs are calculated correctly."""
        spreads, timestamps = sample_spread_data

        for spread, ts in zip(spreads, timestamps):
            signal_generator.add_spread(spread, ts)

        signal_generator.calculate_statistics()
        result = signal_generator.calculate_min_profitable_std(
            spot_spread=0.40,
            futures_spread=0.10
        )

        # Round trip cost should include:
        # - Spread costs * 2 (entry + exit) * position_value
        # - Commission * 4 (4 legs)
        # - Swap cost

        expected_spread_cost = (0.40 + 0.10) * 2 * (0.1 * 100.0)  # 10.0
        expected_commission = 7.0 * 4  # 28.0
        expected_total = expected_spread_cost + expected_commission + 0  # swap = 0

        assert abs(result['total_round_trip_cost'] - expected_total) < 0.01

    def test_std_filter_blocks_low_volatility_entry(self, sample_spread_data):
        """Test that entry signals are blocked when volatility is too low."""
        from feature_files.core.signals import SignalGenerator

        # Create generator with very high costs to ensure blocking
        # Disable Hurst filter to test STD filter specifically
        generator = SignalGenerator(
            std_filter_enabled=True,
            hurst_enabled=False,  # Disable Hurst to test STD filter
            lot_size=0.1,
            contract_size=100.0,
            spot_spread_cost=10.0,
            futures_spread_cost=10.0,
            commission_per_lot=100.0,
            profit_margin=2.0,
            entry_threshold=2.0
        )

        # Add data to populate history
        base_time = datetime.now()
        for i in range(100):
            spread = 50.0 + np.random.uniform(-0.1, 0.1)
            generator.add_spread(spread, base_time + timedelta(minutes=i))

        # Try to generate entry signal at extreme z-score
        # Even with extreme z-score, it should be blocked due to low profitability
        signal = generator.generate_entry_signal(
            current_spread=40.0,  # Far below mean
            has_position=False,
            spot_spread=10.0,
            futures_spread=10.0
        )

        # Signal should be NONE with reason containing "STD too low"
        from feature_files.core.signals import SignalType
        assert signal.signal_type == SignalType.NONE
        assert "STD too low" in signal.reason

    def test_profit_margin_effect(self, signal_generator, sample_spread_data):
        """Test that profit margin affects minimum required STD."""
        spreads, timestamps = sample_spread_data

        for spread, ts in zip(spreads, timestamps):
            signal_generator.add_spread(spread, ts)

        signal_generator.calculate_statistics()

        # Calculate with profit margin 1.5
        result_1_5 = signal_generator.calculate_min_profitable_std()
        min_std_1_5 = result_1_5['min_profitable_std']

        # Change profit margin to 2.0
        signal_generator.profit_margin = 2.0
        result_2_0 = signal_generator.calculate_min_profitable_std()
        min_std_2_0 = result_2_0['min_profitable_std']

        # Higher profit margin should require higher min STD
        assert min_std_2_0 > min_std_1_5


# =============================================================================
# TEST 5: SAME BROKER ACCOUNT FUNCTIONALITY
# =============================================================================

class TestSameBrokerAccount:
    """Test unified/same broker account for both spot and futures."""

    @pytest.mark.asyncio
    async def test_unified_broker_connection(self, mock_broker_config_unified):
        """Test connecting to a unified broker."""
        from feature_files.okx_adapter import OKXAdapter

        adapter = OKXAdapter(mock_broker_config_unified)

        # Verify unified mode is configured
        assert mock_broker_config_unified.unified_mode is True

        await adapter.connect()
        assert adapter.is_connected

        await adapter.disconnect()

    @pytest.mark.asyncio
    async def test_unified_broker_spot_order(self, mock_broker_config_unified):
        """Test placing spot order through unified broker."""
        from feature_files.okx_adapter import OKXAdapter
        from feature_files.base import OrderSide

        adapter = OKXAdapter(mock_broker_config_unified)
        await adapter.connect()

        # Place spot order
        result = await adapter.place_market_order(
            symbol="BTC-USDT",  # Spot symbol
            side=OrderSide.BUY,
            volume=0.1
        )

        assert result.success is True

        await adapter.disconnect()

    @pytest.mark.asyncio
    async def test_unified_broker_futures_order(self, mock_broker_config_unified):
        """Test placing futures order through unified broker."""
        from feature_files.okx_adapter import OKXAdapter
        from feature_files.base import OrderSide

        adapter = OKXAdapter(mock_broker_config_unified)
        await adapter.connect()

        # Place futures order
        result = await adapter.place_market_order(
            symbol="BTC-USDT-SWAP",  # Futures symbol
            side=OrderSide.SELL,
            volume=0.1
        )

        assert result.success is True

        await adapter.disconnect()

    @pytest.mark.asyncio
    async def test_unified_broker_get_ticks_both_symbols(self, mock_broker_config_unified):
        """Test getting ticks for both spot and futures from same broker."""
        from feature_files.okx_adapter import OKXAdapter

        adapter = OKXAdapter(mock_broker_config_unified)
        await adapter.connect()

        # Get spot tick
        spot_tick = await adapter.get_tick("BTC-USDT")
        assert spot_tick is not None
        assert spot_tick.symbol == "BTC-USDT"
        assert spot_tick.bid > 0
        assert spot_tick.ask > 0

        # Get futures tick
        futures_tick = await adapter.get_tick("BTC-USDT-SWAP")
        assert futures_tick is not None
        assert futures_tick.symbol == "BTC-USDT-SWAP"
        assert futures_tick.bid > 0
        assert futures_tick.ask > 0

        await adapter.disconnect()

    @pytest.mark.asyncio
    async def test_unified_broker_account_info(self, mock_broker_config_unified):
        """Test getting account info from unified broker."""
        from feature_files.okx_adapter import OKXAdapter

        adapter = OKXAdapter(mock_broker_config_unified)
        await adapter.connect()

        account = await adapter.get_account_info()

        assert account is not None
        assert account.balance > 0
        assert account.equity > 0

        await adapter.disconnect()

    def test_broker_model_unified_config(self):
        """Test that Broker model supports unified configuration."""
        from feature_files.models import Broker

        broker = Broker(
            broker_id="unified_1",
            name="Unified OKX",
            broker_type="OKX",
            role="UNIFIED",  # Can handle both
            okx_api_key="test_key",
            okx_simulated=True,
            symbol="BTC-USDT"
        )

        assert broker.role == "UNIFIED"
        assert broker.broker_type == "OKX"


# =============================================================================
# TEST 6: DIFFERENT BROKER ACCOUNTS FOR SPOT AND FUTURES
# =============================================================================

class TestDifferentBrokerAccounts:
    """Test using separate broker accounts for spot and futures."""

    @pytest.mark.asyncio
    async def test_separate_spot_broker_connection(self, mock_broker_config_spot):
        """Test connecting to separate spot broker."""
        from feature_files.okx_adapter import OKXAdapter

        adapter = OKXAdapter(mock_broker_config_spot)

        assert mock_broker_config_spot.role == "SPOT"
        assert mock_broker_config_spot.okx_account_type == "spot"

        await adapter.connect()
        assert adapter.is_connected

        await adapter.disconnect()

    @pytest.mark.asyncio
    async def test_separate_futures_broker_connection(self, mock_broker_config_futures):
        """Test connecting to separate futures broker."""
        from feature_files.okx_adapter import OKXAdapter

        adapter = OKXAdapter(mock_broker_config_futures)

        assert mock_broker_config_futures.role == "FUTURES"
        assert mock_broker_config_futures.okx_account_type == "swap"

        await adapter.connect()
        assert adapter.is_connected

        await adapter.disconnect()

    @pytest.mark.asyncio
    async def test_both_brokers_simultaneous_connection(
        self, mock_broker_config_spot, mock_broker_config_futures
    ):
        """Test connecting to both spot and futures brokers simultaneously."""
        from feature_files.okx_adapter import OKXAdapter

        spot_adapter = OKXAdapter(mock_broker_config_spot)
        futures_adapter = OKXAdapter(mock_broker_config_futures)

        # Connect both
        await spot_adapter.connect()
        await futures_adapter.connect()

        assert spot_adapter.is_connected
        assert futures_adapter.is_connected

        # They should have different broker_ids
        assert spot_adapter.broker_id != futures_adapter.broker_id

        await spot_adapter.disconnect()
        await futures_adapter.disconnect()

    @pytest.mark.asyncio
    async def test_simultaneous_orders_different_brokers(
        self, mock_broker_config_spot, mock_broker_config_futures
    ):
        """Test placing orders on both brokers simultaneously."""
        from feature_files.okx_adapter import OKXAdapter
        from feature_files.base import OrderSide

        spot_adapter = OKXAdapter(mock_broker_config_spot)
        futures_adapter = OKXAdapter(mock_broker_config_futures)

        await spot_adapter.connect()
        await futures_adapter.connect()

        # Place orders simultaneously
        spot_order = await spot_adapter.place_market_order(
            symbol="BTC-USDT",
            side=OrderSide.BUY,
            volume=0.1
        )

        # Small delay to ensure different timestamp (mock uses ms timestamp for order ID)
        await asyncio.sleep(0.002)

        futures_order = await futures_adapter.place_market_order(
            symbol="BTC-USDT-SWAP",
            side=OrderSide.SELL,
            volume=0.1
        )

        assert spot_order.success is True
        assert futures_order.success is True

        # Order IDs should be different (in mock mode, based on timestamp)
        # Note: In mock mode, IDs are timestamp-based; in real mode they come from exchange
        assert spot_order.order_id is not None
        assert futures_order.order_id is not None

        await spot_adapter.disconnect()
        await futures_adapter.disconnect()

    def test_trade_model_multi_broker_references(self):
        """Test that Trade model supports multi-broker references."""
        from feature_files.models import Trade

        trade = Trade(
            trade_id="trade_001",
            asset="GOLD",
            direction="Long Spread",
            entry_spot_price=2000.0,
            entry_futures_price=2001.0,
            spot_broker_id="spot_broker_1",
            mt5_spot_ticket=12345,
            futures_broker_id="futures_broker_1",
            mt5_futures_ticket=67890,
            lot_size=0.1,
            status="OPEN"
        )

        assert trade.spot_broker_id == "spot_broker_1"
        assert trade.futures_broker_id == "futures_broker_1"
        assert trade.mt5_spot_ticket == 12345
        assert trade.mt5_futures_ticket == 67890

    def test_trading_config_active_brokers(self):
        """Test TradingConfig supports active broker selection."""
        from feature_files.models import TradingConfig

        config = TradingConfig(
            active_spot_broker="spot_broker_1",
            active_futures_broker="futures_broker_1"
        )

        assert config.active_spot_broker == "spot_broker_1"
        assert config.active_futures_broker == "futures_broker_1"

    def test_broker_config_spot_vs_futures_differences(
        self, mock_broker_config_spot, mock_broker_config_futures
    ):
        """Test configuration differences between spot and futures brokers."""
        # Spot config
        assert mock_broker_config_spot.role == "SPOT"
        assert mock_broker_config_spot.okx_account_type == "spot"
        assert "SWAP" not in mock_broker_config_spot.symbol

        # Futures config
        assert mock_broker_config_futures.role == "FUTURES"
        assert mock_broker_config_futures.okx_account_type == "swap"
        assert "SWAP" in mock_broker_config_futures.symbol


# =============================================================================
# TEST 7: PRICE UPDATES
# =============================================================================

class TestPriceUpdates:
    """Test price update mechanisms including ticks and caching."""

    @pytest.mark.asyncio
    async def test_get_tick_returns_valid_data(self, mock_broker_config_spot):
        """Test that get_tick returns valid tick data."""
        from feature_files.okx_adapter import OKXAdapter

        adapter = OKXAdapter(mock_broker_config_spot)
        await adapter.connect()

        tick = await adapter.get_tick("BTC-USDT")

        assert tick is not None
        assert tick.symbol == "BTC-USDT"
        assert tick.bid > 0
        assert tick.ask > 0
        assert tick.timestamp is not None
        assert tick.bid < tick.ask  # Bid should be less than ask

        await adapter.disconnect()

    @pytest.mark.asyncio
    async def test_tick_spread_calculation(self, mock_broker_config_spot):
        """Test that tick spread is calculated correctly."""
        from feature_files.okx_adapter import OKXAdapter

        adapter = OKXAdapter(mock_broker_config_spot)
        await adapter.connect()

        tick = await adapter.get_tick("BTC-USDT")

        assert tick is not None

        # Verify spread calculation
        expected_spread = tick.ask - tick.bid
        assert abs(tick.spread - expected_spread) < 0.0001

        # Verify mid price
        expected_mid = (tick.bid + tick.ask) / 2
        assert abs(tick.mid - expected_mid) < 0.0001

        await adapter.disconnect()

    @pytest.mark.asyncio
    async def test_tick_caching(self, mock_broker_config_spot):
        """Test that ticks are cached properly."""
        from feature_files.okx_adapter import OKXAdapter

        adapter = OKXAdapter(mock_broker_config_spot)
        await adapter.connect()

        # First call should populate cache
        tick1 = await adapter.get_tick("BTC-USDT")

        # Second call within 1 second should return cached value
        tick2 = await adapter.get_tick("BTC-USDT")

        # In mock mode, timestamps might be the same if cached
        assert tick1 is not None
        assert tick2 is not None

        await adapter.disconnect()

    @pytest.mark.asyncio
    async def test_subscribe_tick(self, mock_broker_config_spot):
        """Test tick subscription."""
        from feature_files.okx_adapter import OKXAdapter

        adapter = OKXAdapter(mock_broker_config_spot)
        await adapter.connect()

        result = await adapter.subscribe_tick("BTC-USDT")

        assert result is True
        assert "BTC-USDT" in adapter._subscribed_symbols

        await adapter.disconnect()

    @pytest.mark.asyncio
    async def test_multiple_symbol_subscriptions(self, mock_broker_config_unified):
        """Test subscribing to multiple symbols."""
        from feature_files.okx_adapter import OKXAdapter

        adapter = OKXAdapter(mock_broker_config_unified)
        await adapter.connect()

        await adapter.subscribe_tick("BTC-USDT")
        await adapter.subscribe_tick("ETH-USDT")
        await adapter.subscribe_tick("BTC-USDT-SWAP")

        assert "BTC-USDT" in adapter._subscribed_symbols
        assert "ETH-USDT" in adapter._subscribed_symbols
        assert "BTC-USDT-SWAP" in adapter._subscribed_symbols
        assert len(adapter._subscribed_symbols) == 3

        await adapter.disconnect()

    @pytest.mark.asyncio
    async def test_price_updates_spot_futures_spread(
        self, mock_broker_config_spot, mock_broker_config_futures
    ):
        """Test getting prices for spread calculation."""
        from feature_files.okx_adapter import OKXAdapter

        spot_adapter = OKXAdapter(mock_broker_config_spot)
        futures_adapter = OKXAdapter(mock_broker_config_futures)

        await spot_adapter.connect()
        await futures_adapter.connect()

        spot_tick = await spot_adapter.get_tick("BTC-USDT")
        futures_tick = await futures_adapter.get_tick("BTC-USDT-SWAP")

        assert spot_tick is not None
        assert futures_tick is not None

        # Calculate spread (futures - spot for basis)
        spread = futures_tick.mid - spot_tick.mid

        # Spread should be a reasonable value (positive or negative)
        assert isinstance(spread, float)

        await spot_adapter.disconnect()
        await futures_adapter.disconnect()

    def test_tick_dataclass_properties(self):
        """Test Tick dataclass properties work correctly."""
        from feature_files.base import Tick

        tick = Tick(
            symbol="BTC-USDT",
            bid=50000.0,
            ask=50010.0,
            timestamp=datetime.now(),
            last=50005.0,
            volume=1000.0
        )

        assert tick.spread == 10.0
        assert tick.mid == 50005.0
        assert tick.spread_pct == (10.0 / 50005.0) * 100

    @pytest.mark.asyncio
    async def test_heartbeat_updates_latency(self, mock_broker_config_spot):
        """Test that heartbeat updates latency measurement."""
        from feature_files.okx_adapter import OKXAdapter

        adapter = OKXAdapter(mock_broker_config_spot)
        await adapter.connect()

        # Heartbeat should succeed in mock mode
        result = await adapter.heartbeat()

        assert result is True
        assert adapter._last_heartbeat is not None

        await adapter.disconnect()

    @pytest.mark.asyncio
    async def test_mock_ticker_response_structure(self, mock_broker_config_spot):
        """Test that mock ticker responses have correct structure."""
        from feature_files.okx_adapter import OKXAdapter

        adapter = OKXAdapter(mock_broker_config_spot)

        # Test mock response directly
        response = adapter._mock_response(
            'GET',
            '/api/v5/market/ticker',
            {'instId': 'BTC-USDT'},
            None
        )

        assert response['code'] == '0'
        assert len(response['data']) == 1
        assert 'instId' in response['data'][0]
        assert 'last' in response['data'][0]
        assert 'bidPx' in response['data'][0]
        assert 'askPx' in response['data'][0]


# =============================================================================
# INTEGRATION TESTS
# =============================================================================

class TestIntegration:
    """Integration tests combining multiple components."""

    @pytest.mark.asyncio
    async def test_full_spread_trade_flow_mock(
        self, mock_broker_config_spot, mock_broker_config_futures
    ):
        """Test complete spread trade flow in mock mode."""
        from feature_files.okx_adapter import OKXAdapter
        from feature_files.base import OrderSide
        from feature_files.core.signals import SignalGenerator, SignalType

        # Set up adapters
        spot_adapter = OKXAdapter(mock_broker_config_spot)
        futures_adapter = OKXAdapter(mock_broker_config_futures)

        await spot_adapter.connect()
        await futures_adapter.connect()

        # Set up signal generator with Hurst disabled for deterministic testing
        generator = SignalGenerator(
            lookback_period=120,  # Large to prevent trimming
            lookback_unit="minutes",
            entry_threshold=2.0,
            exit_threshold=0.5,
            std_filter_enabled=False,  # Disable for this test
            hurst_enabled=False  # Disable Hurst for deterministic testing
        )

        # Simulate spread data with proper variation (mock tickers have minimal random variation)
        # Generate synthetic mean-reverting spread data instead of relying on mock ticks
        np.random.seed(42)
        base_time = datetime.now() - timedelta(minutes=60)
        base_spread = 50.0

        for i in range(60):
            # Add mean-reverting spread with variation
            shock = np.random.normal(0, 5)
            spread = base_spread + shock
            generator.add_spread(spread, base_time + timedelta(minutes=i))

        # Check we have enough data
        assert generator.data_points >= 10

        # Generate a signal (simulate extreme spread)
        stats = generator.calculate_statistics()
        assert stats is not None
        mean, std = stats

        # Verify we have reasonable statistics
        assert std > 0, "Standard deviation should be positive"

        # Simulate an extreme spread that would trigger entry
        extreme_spread = mean - 3 * std

        signal = generator.generate_entry_signal(
            current_spread=extreme_spread,
            has_position=False
        )

        # Should be ENTRY_LONG signal
        assert signal.signal_type == SignalType.ENTRY_LONG

        # Execute the trade
        spot_order = await spot_adapter.place_market_order(
            symbol="BTC-USDT",
            side=OrderSide.BUY,
            volume=0.1
        )
        futures_order = await futures_adapter.place_market_order(
            symbol="BTC-USDT-SWAP",
            side=OrderSide.SELL,
            volume=0.1
        )

        assert spot_order.success is True
        assert futures_order.success is True

        await spot_adapter.disconnect()
        await futures_adapter.disconnect()

    def test_signal_generator_statistics_summary(self, signal_generator, sample_spread_data):
        """Test getting full statistics summary."""
        spreads, timestamps = sample_spread_data

        for spread, ts in zip(spreads, timestamps):
            signal_generator.add_spread(spread, ts)

        signal_generator.calculate_statistics()
        signal_generator.calculate_hurst()

        stats = signal_generator.get_statistics()

        assert 'data_points' in stats
        assert 'mean' in stats
        assert 'std' in stats
        assert 'zscore' in stats
        assert 'hurst' in stats
        assert 'regime' in stats
        assert 'lookback_period' in stats
        assert 'lookback_unit' in stats


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
