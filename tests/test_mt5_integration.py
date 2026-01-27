"""
MT5 Integration Tests - These tests actually connect to MetaTrader 5 and place real orders.

IMPORTANT: These tests will place REAL orders on your MT5 account!
- Uses minimum lot size to minimize risk
- Orders are immediately closed after testing
- Requires MT5 to be running and logged in

Run with: pytest tests/test_mt5_integration.py -v -s
Skip in CI with: pytest tests/ --ignore=tests/test_mt5_integration.py
"""

import pytest
import sys
from pathlib import Path

# Add parent directory to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent.parent / "feature_files"))

# Check if MT5 is available
try:
    import MetaTrader5 as mt5
    MT5_AVAILABLE = True
except ImportError:
    MT5_AVAILABLE = False
    mt5 = None


def mt5_connected():
    """Check if MT5 is running and connected."""
    if not MT5_AVAILABLE:
        return False
    if not mt5.initialize():
        return False
    info = mt5.account_info()
    mt5.shutdown()
    return info is not None


# Skip all tests in this module if MT5 is not available
pytestmark = pytest.mark.skipif(
    not MT5_AVAILABLE or not mt5_connected(),
    reason="MT5 not available or not connected"
)


class TestMT5Connection:
    """Test MT5 connection and account info."""

    def test_mt5_initialize(self):
        """Test that MT5 can be initialized."""
        assert mt5.initialize(), "Failed to initialize MT5"
        mt5.shutdown()

    def test_mt5_account_info(self):
        """Test that we can get account info."""
        mt5.initialize()
        info = mt5.account_info()
        assert info is not None, "Failed to get account info"
        print(f"\n  Account: {info.login}")
        print(f"  Server: {info.server}")
        print(f"  Balance: ${info.balance:.2f}")
        print(f"  Equity: ${info.equity:.2f}")
        mt5.shutdown()

    def test_mt5_symbols_available(self):
        """Test that trading symbols are available."""
        mt5.initialize()

        # Common gold symbols to check
        gold_symbols = ["XAUUSD", "GOLD", "XAUUSDm", "GOLDm"]
        found_symbols = []

        for symbol in gold_symbols:
            info = mt5.symbol_info(symbol)
            if info is not None:
                found_symbols.append(symbol)
                print(f"\n  Found symbol: {symbol}")
                print(f"    Bid: {info.bid}, Ask: {info.ask}")
                print(f"    Min lot: {info.volume_min}, Max lot: {info.volume_max}")

        mt5.shutdown()
        assert len(found_symbols) > 0, f"No gold symbols found. Tried: {gold_symbols}"


class TestMT5RealOrders:
    """Test actual order placement on MT5.

    WARNING: These tests place REAL orders!
    """

    def get_test_symbol(self):
        """Get a valid gold symbol for testing."""
        mt5.initialize()

        # Try common gold symbols
        for symbol in ["XAUUSD", "GOLD", "XAUUSDm", "GOLDm"]:
            info = mt5.symbol_info(symbol)
            if info is not None and info.visible:
                mt5.shutdown()
                return symbol
            elif info is not None:
                # Try to make it visible
                mt5.symbol_select(symbol, True)
                mt5.shutdown()
                return symbol

        mt5.shutdown()
        pytest.skip("No valid gold symbol found")

    def test_place_buy_order_and_close(self):
        """Test placing a BUY order and immediately closing it."""
        symbol = self.get_test_symbol()

        mt5.initialize()
        mt5.symbol_select(symbol, True)

        symbol_info = mt5.symbol_info(symbol)
        tick = mt5.symbol_info_tick(symbol)

        assert tick is not None, f"Could not get tick for {symbol}"

        min_volume = symbol_info.volume_min
        price = tick.ask

        print(f"\n  Symbol: {symbol}")
        print(f"  Price (Ask): {price}")
        print(f"  Min Volume: {min_volume}")

        # Place BUY order
        request = {
            "action": mt5.TRADE_ACTION_DEAL,
            "symbol": symbol,
            "volume": min_volume,
            "type": mt5.ORDER_TYPE_BUY,
            "price": price,
            "deviation": 20,
            "magic": 999999,  # Magic number to identify test orders
            "comment": "Integration Test BUY",
            "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": mt5.ORDER_FILLING_IOC,
        }

        result = mt5.order_send(request)
        print(f"  Order result: {result}")

        assert result.retcode == mt5.TRADE_RETCODE_DONE, \
            f"BUY order failed: {result.comment} (code: {result.retcode})"

        print(f"  BUY Order placed! Ticket: {result.order}")

        # Wait a moment then close the position
        import time
        time.sleep(1)

        # Get current position
        positions = mt5.positions_get(symbol=symbol)
        assert positions is not None and len(positions) > 0, "No position found to close"

        position = positions[0]
        print(f"  Position ticket: {position.ticket}, Volume: {position.volume}")

        # Close the position
        close_request = {
            "action": mt5.TRADE_ACTION_DEAL,
            "symbol": symbol,
            "volume": position.volume,
            "type": mt5.ORDER_TYPE_SELL,
            "position": position.ticket,
            "price": mt5.symbol_info_tick(symbol).bid,
            "deviation": 20,
            "magic": 999999,
            "comment": "Integration Test CLOSE",
            "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": mt5.ORDER_FILLING_IOC,
        }

        close_result = mt5.order_send(close_request)
        print(f"  Close result: {close_result}")

        assert close_result.retcode == mt5.TRADE_RETCODE_DONE, \
            f"Close order failed: {close_result.comment} (code: {close_result.retcode})"

        print(f"  Position closed! Ticket: {close_result.order}")

        mt5.shutdown()

    def test_place_sell_order_and_close(self):
        """Test placing a SELL order and immediately closing it."""
        symbol = self.get_test_symbol()

        mt5.initialize()
        mt5.symbol_select(symbol, True)

        symbol_info = mt5.symbol_info(symbol)
        tick = mt5.symbol_info_tick(symbol)

        assert tick is not None, f"Could not get tick for {symbol}"

        min_volume = symbol_info.volume_min
        price = tick.bid

        print(f"\n  Symbol: {symbol}")
        print(f"  Price (Bid): {price}")
        print(f"  Min Volume: {min_volume}")

        # Place SELL order
        request = {
            "action": mt5.TRADE_ACTION_DEAL,
            "symbol": symbol,
            "volume": min_volume,
            "type": mt5.ORDER_TYPE_SELL,
            "price": price,
            "deviation": 20,
            "magic": 999999,
            "comment": "Integration Test SELL",
            "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": mt5.ORDER_FILLING_IOC,
        }

        result = mt5.order_send(request)
        print(f"  Order result: {result}")

        assert result.retcode == mt5.TRADE_RETCODE_DONE, \
            f"SELL order failed: {result.comment} (code: {result.retcode})"

        print(f"  SELL Order placed! Ticket: {result.order}")

        # Wait a moment then close the position
        import time
        time.sleep(1)

        # Get current position
        positions = mt5.positions_get(symbol=symbol)
        assert positions is not None and len(positions) > 0, "No position found to close"

        position = positions[0]
        print(f"  Position ticket: {position.ticket}, Volume: {position.volume}")

        # Close the position (buy to close short)
        close_request = {
            "action": mt5.TRADE_ACTION_DEAL,
            "symbol": symbol,
            "volume": position.volume,
            "type": mt5.ORDER_TYPE_BUY,
            "position": position.ticket,
            "price": mt5.symbol_info_tick(symbol).ask,
            "deviation": 20,
            "magic": 999999,
            "comment": "Integration Test CLOSE",
            "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": mt5.ORDER_FILLING_IOC,
        }

        close_result = mt5.order_send(close_request)
        print(f"  Close result: {close_result}")

        assert close_result.retcode == mt5.TRADE_RETCODE_DONE, \
            f"Close order failed: {close_result.comment} (code: {close_result.retcode})"

        print(f"  Position closed! Ticket: {close_result.order}")

        mt5.shutdown()


class TestMT5FromAppEndpoint:
    """Test the /api/test-order endpoint logic with real MT5."""

    def test_app_test_order_logic(self):
        """Test the same logic used by the app's test order endpoint."""
        mt5.initialize()

        # This mirrors what /api/test-order does
        symbol = None
        for s in ["XAUUSD", "GOLD", "XAUUSDm", "GOLDm"]:
            info = mt5.symbol_info(s)
            if info is not None:
                symbol = s
                break

        if not symbol:
            mt5.shutdown()
            pytest.skip("No gold symbol found")

        mt5.symbol_select(symbol, True)
        symbol_info = mt5.symbol_info(symbol)
        tick = mt5.symbol_info_tick(symbol)

        assert tick is not None, "Could not get price"

        min_volume = symbol_info.volume_min

        # BUY order (same as app endpoint)
        request = {
            "action": mt5.TRADE_ACTION_DEAL,
            "symbol": symbol,
            "volume": min_volume,
            "type": mt5.ORDER_TYPE_BUY,
            "price": tick.ask,
            "deviation": 20,
            "magic": 123456,  # Same magic as app
            "comment": "Test Order",  # Same comment as app
            "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": mt5.ORDER_FILLING_IOC,
        }

        print(f"\n  Placing order: {request}")
        result = mt5.order_send(request)

        print(f"  Result: retcode={result.retcode}, comment={result.comment}")

        if result.retcode == mt5.TRADE_RETCODE_DONE:
            print(f"  SUCCESS! Order ticket: {result.order}")

            # Close it
            import time
            time.sleep(0.5)

            positions = mt5.positions_get(symbol=symbol)
            if positions:
                pos = positions[0]
                close_req = {
                    "action": mt5.TRADE_ACTION_DEAL,
                    "symbol": symbol,
                    "volume": pos.volume,
                    "type": mt5.ORDER_TYPE_SELL,
                    "position": pos.ticket,
                    "price": mt5.symbol_info_tick(symbol).bid,
                    "deviation": 20,
                    "magic": 123456,
                    "comment": "Test Order Close",
                    "type_time": mt5.ORDER_TIME_GTC,
                    "type_filling": mt5.ORDER_FILLING_IOC,
                }
                close_result = mt5.order_send(close_req)
                print(f"  Closed position: {close_result.retcode}")
        else:
            print(f"  FAILED: {result.comment}")

        mt5.shutdown()

        assert result.retcode == mt5.TRADE_RETCODE_DONE, \
            f"Order failed: {result.comment} (code: {result.retcode})"


if __name__ == "__main__":
    # Run tests directly
    pytest.main([__file__, "-v", "-s"])
