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


class TestTicketBasedOperations:
    """Comprehensive tests for ticket-based order management.

    Tests the full lifecycle:
    1. Open position -> get ticket
    2. Find position by ticket
    3. Close position by ticket
    4. Verify closure
    """

    def get_test_symbol(self):
        """Get a valid symbol for testing."""
        mt5.initialize()
        for symbol in ["XAUUSD", "GOLD", "XAUUSDm", "GOLDm"]:
            info = mt5.symbol_info(symbol)
            if info is not None:
                mt5.symbol_select(symbol, True)
                mt5.shutdown()
                return symbol
        mt5.shutdown()
        pytest.skip("No valid symbol found")

    def test_open_order_returns_ticket(self):
        """Test that opening an order returns a valid ticket number."""
        symbol = self.get_test_symbol()

        mt5.initialize()
        mt5.symbol_select(symbol, True)

        symbol_info = mt5.symbol_info(symbol)
        tick = mt5.symbol_info_tick(symbol)

        request = {
            "action": mt5.TRADE_ACTION_DEAL,
            "symbol": symbol,
            "volume": symbol_info.volume_min,
            "type": mt5.ORDER_TYPE_BUY,
            "price": tick.ask,
            "deviation": 20,
            "magic": 888888,
            "comment": "Ticket Test",
            "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": mt5.ORDER_FILLING_IOC,
        }

        result = mt5.order_send(request)

        print(f"\n  Order Result:")
        print(f"    retcode: {result.retcode}")
        print(f"    deal: {result.deal}")
        print(f"    order: {result.order}")
        print(f"    comment: {result.comment}")

        assert result.retcode == mt5.TRADE_RETCODE_DONE, f"Order failed: {result.comment}"
        assert result.order > 0, "Order ticket should be positive"
        assert result.deal > 0, "Deal ticket should be positive"

        order_ticket = result.order
        print(f"    ORDER TICKET: {order_ticket}")

        # Clean up - close the position
        import time
        time.sleep(0.5)

        positions = mt5.positions_get(symbol=symbol)
        if positions:
            for pos in positions:
                if pos.magic == 888888:
                    close_req = {
                        "action": mt5.TRADE_ACTION_DEAL,
                        "symbol": symbol,
                        "volume": pos.volume,
                        "type": mt5.ORDER_TYPE_SELL,
                        "position": pos.ticket,
                        "price": mt5.symbol_info_tick(symbol).bid,
                        "deviation": 20,
                        "magic": 888888,
                        "comment": "Ticket Test Close",
                        "type_time": mt5.ORDER_TIME_GTC,
                        "type_filling": mt5.ORDER_FILLING_IOC,
                    }
                    mt5.order_send(close_req)

        mt5.shutdown()

    def test_find_position_by_ticket(self):
        """Test finding a specific position using its ticket number."""
        symbol = self.get_test_symbol()

        mt5.initialize()
        mt5.symbol_select(symbol, True)

        symbol_info = mt5.symbol_info(symbol)
        tick = mt5.symbol_info_tick(symbol)

        # Open position
        request = {
            "action": mt5.TRADE_ACTION_DEAL,
            "symbol": symbol,
            "volume": symbol_info.volume_min,
            "type": mt5.ORDER_TYPE_BUY,
            "price": tick.ask,
            "deviation": 20,
            "magic": 777777,
            "comment": "Find By Ticket Test",
            "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": mt5.ORDER_FILLING_IOC,
        }

        result = mt5.order_send(request)
        assert result.retcode == mt5.TRADE_RETCODE_DONE, f"Order failed: {result.comment}"

        import time
        time.sleep(0.5)

        # Find position by ticket
        positions = mt5.positions_get(symbol=symbol)

        print(f"\n  All positions for {symbol}:")
        position_ticket = None
        for pos in positions:
            print(f"    Ticket: {pos.ticket}, Magic: {pos.magic}, Volume: {pos.volume}")
            if pos.magic == 777777:
                position_ticket = pos.ticket

        assert position_ticket is not None, "Could not find position with magic 777777"

        # Now find specifically by ticket
        found_positions = mt5.positions_get(ticket=position_ticket)

        print(f"\n  Found by ticket {position_ticket}:")
        assert found_positions is not None, f"positions_get(ticket={position_ticket}) returned None"
        assert len(found_positions) == 1, f"Expected 1 position, got {len(found_positions)}"

        found_pos = found_positions[0]
        print(f"    Symbol: {found_pos.symbol}")
        print(f"    Volume: {found_pos.volume}")
        print(f"    Profit: {found_pos.profit}")
        print(f"    Price Open: {found_pos.price_open}")

        assert found_pos.ticket == position_ticket

        # Clean up
        close_req = {
            "action": mt5.TRADE_ACTION_DEAL,
            "symbol": symbol,
            "volume": found_pos.volume,
            "type": mt5.ORDER_TYPE_SELL,
            "position": found_pos.ticket,
            "price": mt5.symbol_info_tick(symbol).bid,
            "deviation": 20,
            "magic": 777777,
            "comment": "Find By Ticket Close",
            "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": mt5.ORDER_FILLING_IOC,
        }
        mt5.order_send(close_req)

        mt5.shutdown()

    def test_close_position_by_ticket(self):
        """Test closing a specific position using its ticket number."""
        symbol = self.get_test_symbol()

        mt5.initialize()
        mt5.symbol_select(symbol, True)

        symbol_info = mt5.symbol_info(symbol)
        tick = mt5.symbol_info_tick(symbol)

        # Open position
        open_request = {
            "action": mt5.TRADE_ACTION_DEAL,
            "symbol": symbol,
            "volume": symbol_info.volume_min,
            "type": mt5.ORDER_TYPE_BUY,
            "price": tick.ask,
            "deviation": 20,
            "magic": 666666,
            "comment": "Close By Ticket Test",
            "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": mt5.ORDER_FILLING_IOC,
        }

        open_result = mt5.order_send(open_request)
        assert open_result.retcode == mt5.TRADE_RETCODE_DONE, f"Open failed: {open_result.comment}"

        import time
        time.sleep(0.5)

        # Get the position ticket
        positions = mt5.positions_get(symbol=symbol)
        position_ticket = None
        for pos in positions:
            if pos.magic == 666666:
                position_ticket = pos.ticket
                break

        assert position_ticket is not None, "Could not find opened position"
        print(f"\n  Opened position with ticket: {position_ticket}")

        # Close by ticket
        tick = mt5.symbol_info_tick(symbol)
        close_request = {
            "action": mt5.TRADE_ACTION_DEAL,
            "symbol": symbol,
            "volume": symbol_info.volume_min,
            "type": mt5.ORDER_TYPE_SELL,
            "position": position_ticket,  # <-- This is the key: closing by ticket
            "price": tick.bid,
            "deviation": 20,
            "magic": 666666,
            "comment": "Close By Ticket",
            "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": mt5.ORDER_FILLING_IOC,
        }

        close_result = mt5.order_send(close_request)

        print(f"  Close result:")
        print(f"    retcode: {close_result.retcode}")
        print(f"    comment: {close_result.comment}")

        assert close_result.retcode == mt5.TRADE_RETCODE_DONE, \
            f"Close by ticket failed: {close_result.comment}"

        # Verify position is closed
        time.sleep(0.5)
        remaining = mt5.positions_get(ticket=position_ticket)

        assert remaining is None or len(remaining) == 0, \
            f"Position {position_ticket} still exists after close!"

        print(f"  Position {position_ticket} successfully closed")

        mt5.shutdown()

    def test_multiple_positions_close_specific(self):
        """Test opening multiple positions and closing a specific one by ticket."""
        symbol = self.get_test_symbol()

        mt5.initialize()
        mt5.symbol_select(symbol, True)

        symbol_info = mt5.symbol_info(symbol)
        tick = mt5.symbol_info_tick(symbol)
        min_vol = symbol_info.volume_min

        tickets = []

        # Open 3 positions
        for i in range(3):
            request = {
                "action": mt5.TRADE_ACTION_DEAL,
                "symbol": symbol,
                "volume": min_vol,
                "type": mt5.ORDER_TYPE_BUY,
                "price": tick.ask,
                "deviation": 20,
                "magic": 555550 + i,
                "comment": f"Multi Test {i}",
                "type_time": mt5.ORDER_TIME_GTC,
                "type_filling": mt5.ORDER_FILLING_IOC,
            }
            result = mt5.order_send(request)
            if result.retcode == mt5.TRADE_RETCODE_DONE:
                # Get the position ticket
                import time
                time.sleep(0.3)
                positions = mt5.positions_get(symbol=symbol)
                for pos in positions:
                    if pos.magic == 555550 + i and pos.ticket not in tickets:
                        tickets.append(pos.ticket)
                        break
            tick = mt5.symbol_info_tick(symbol)  # Refresh tick

        print(f"\n  Opened {len(tickets)} positions:")
        for t in tickets:
            print(f"    Ticket: {t}")

        assert len(tickets) >= 2, f"Need at least 2 positions, got {len(tickets)}"

        # Close only the middle position
        ticket_to_close = tickets[1] if len(tickets) > 1 else tickets[0]
        print(f"\n  Closing only ticket: {ticket_to_close}")

        # Find the position
        pos_to_close = mt5.positions_get(ticket=ticket_to_close)
        assert pos_to_close and len(pos_to_close) == 1

        tick = mt5.symbol_info_tick(symbol)
        close_request = {
            "action": mt5.TRADE_ACTION_DEAL,
            "symbol": symbol,
            "volume": pos_to_close[0].volume,
            "type": mt5.ORDER_TYPE_SELL,
            "position": ticket_to_close,
            "price": tick.bid,
            "deviation": 20,
            "magic": pos_to_close[0].magic,
            "comment": "Close Specific",
            "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": mt5.ORDER_FILLING_IOC,
        }

        close_result = mt5.order_send(close_request)
        assert close_result.retcode == mt5.TRADE_RETCODE_DONE, \
            f"Close failed: {close_result.comment}"

        import time
        time.sleep(0.5)

        # Verify only that specific ticket is closed
        closed_check = mt5.positions_get(ticket=ticket_to_close)
        assert closed_check is None or len(closed_check) == 0, \
            f"Ticket {ticket_to_close} should be closed"

        # Verify others still exist
        remaining_positions = mt5.positions_get(symbol=symbol)
        remaining_tickets = [p.ticket for p in remaining_positions] if remaining_positions else []

        print(f"  Remaining positions: {remaining_tickets}")

        for t in tickets:
            if t != ticket_to_close:
                assert t in remaining_tickets, f"Position {t} should still exist"

        # Clean up - close remaining positions
        if remaining_positions:
            for pos in remaining_positions:
                if pos.magic >= 555550 and pos.magic <= 555559:
                    tick = mt5.symbol_info_tick(symbol)
                    close_req = {
                        "action": mt5.TRADE_ACTION_DEAL,
                        "symbol": symbol,
                        "volume": pos.volume,
                        "type": mt5.ORDER_TYPE_SELL,
                        "position": pos.ticket,
                        "price": tick.bid,
                        "deviation": 20,
                        "magic": pos.magic,
                        "comment": "Cleanup",
                        "type_time": mt5.ORDER_TIME_GTC,
                        "type_filling": mt5.ORDER_FILLING_IOC,
                    }
                    mt5.order_send(close_req)

        mt5.shutdown()

    def test_partial_close_by_ticket(self):
        """Test partially closing a position (if broker supports it)."""
        symbol = self.get_test_symbol()

        mt5.initialize()
        mt5.symbol_select(symbol, True)

        symbol_info = mt5.symbol_info(symbol)
        tick = mt5.symbol_info_tick(symbol)
        min_vol = symbol_info.volume_min

        # Open with 2x minimum volume
        open_volume = min_vol * 2

        request = {
            "action": mt5.TRADE_ACTION_DEAL,
            "symbol": symbol,
            "volume": open_volume,
            "type": mt5.ORDER_TYPE_BUY,
            "price": tick.ask,
            "deviation": 20,
            "magic": 444444,
            "comment": "Partial Close Test",
            "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": mt5.ORDER_FILLING_IOC,
        }

        result = mt5.order_send(request)

        if result.retcode != mt5.TRADE_RETCODE_DONE:
            mt5.shutdown()
            pytest.skip(f"Could not open position: {result.comment}")

        import time
        time.sleep(0.5)

        # Get the position
        positions = mt5.positions_get(symbol=symbol)
        position_ticket = None
        for pos in positions:
            if pos.magic == 444444:
                position_ticket = pos.ticket
                print(f"\n  Opened position:")
                print(f"    Ticket: {pos.ticket}")
                print(f"    Volume: {pos.volume}")
                break

        assert position_ticket is not None

        # Try partial close (close half)
        tick = mt5.symbol_info_tick(symbol)
        partial_close_request = {
            "action": mt5.TRADE_ACTION_DEAL,
            "symbol": symbol,
            "volume": min_vol,  # Close only half
            "type": mt5.ORDER_TYPE_SELL,
            "position": position_ticket,
            "price": tick.bid,
            "deviation": 20,
            "magic": 444444,
            "comment": "Partial Close",
            "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": mt5.ORDER_FILLING_IOC,
        }

        partial_result = mt5.order_send(partial_close_request)
        print(f"\n  Partial close result: {partial_result.retcode} - {partial_result.comment}")

        time.sleep(0.5)

        # Check remaining position
        remaining = mt5.positions_get(ticket=position_ticket)

        if remaining and len(remaining) > 0:
            print(f"  Remaining volume: {remaining[0].volume}")
            # Partial close worked - clean up the rest
            tick = mt5.symbol_info_tick(symbol)
            final_close = {
                "action": mt5.TRADE_ACTION_DEAL,
                "symbol": symbol,
                "volume": remaining[0].volume,
                "type": mt5.ORDER_TYPE_SELL,
                "position": position_ticket,
                "price": tick.bid,
                "deviation": 20,
                "magic": 444444,
                "comment": "Final Close",
                "type_time": mt5.ORDER_TIME_GTC,
                "type_filling": mt5.ORDER_FILLING_IOC,
            }
            mt5.order_send(final_close)
        else:
            print("  Position fully closed (broker may not support partial close)")

        mt5.shutdown()


class TestSpotFuturesPairTrade:
    """Test opening and closing paired spot/futures positions (arbitrage scenario)."""

    def get_symbols(self):
        """Get spot and futures symbols for testing."""
        mt5.initialize()

        # Try to find spot and futures gold symbols
        spot_symbols = ["XAUUSD", "GOLD", "XAUUSDm"]
        futures_symbols = ["XAUUSD_F", "GOLD_F", "XAUUSDm_F", "GCZ24", "GCH25"]

        spot = None
        futures = None

        for s in spot_symbols:
            info = mt5.symbol_info(s)
            if info is not None:
                mt5.symbol_select(s, True)
                spot = s
                break

        for f in futures_symbols:
            info = mt5.symbol_info(f)
            if info is not None:
                mt5.symbol_select(f, True)
                futures = f
                break

        mt5.shutdown()

        if not spot:
            pytest.skip("No spot symbol found")

        # If no futures, use spot for both (just to test the mechanics)
        if not futures:
            futures = spot
            print(f"\n  NOTE: Using same symbol for spot and futures: {spot}")

        return spot, futures

    def test_open_paired_positions(self):
        """Test opening both spot and futures positions (simulating arbitrage entry)."""
        spot_symbol, futures_symbol = self.get_symbols()

        mt5.initialize()
        mt5.symbol_select(spot_symbol, True)
        if futures_symbol != spot_symbol:
            mt5.symbol_select(futures_symbol, True)

        spot_info = mt5.symbol_info(spot_symbol)
        spot_tick = mt5.symbol_info_tick(spot_symbol)

        spot_tickets = []
        futures_tickets = []

        print(f"\n  Opening paired positions:")
        print(f"    Spot symbol: {spot_symbol}")
        print(f"    Futures symbol: {futures_symbol}")

        # Open SPOT position (BUY)
        spot_request = {
            "action": mt5.TRADE_ACTION_DEAL,
            "symbol": spot_symbol,
            "volume": spot_info.volume_min,
            "type": mt5.ORDER_TYPE_BUY,
            "price": spot_tick.ask,
            "deviation": 20,
            "magic": 111111,
            "comment": "Arb Spot BUY",
            "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": mt5.ORDER_FILLING_IOC,
        }

        spot_result = mt5.order_send(spot_request)
        print(f"\n  Spot BUY: retcode={spot_result.retcode}, order={spot_result.order}")

        if spot_result.retcode == mt5.TRADE_RETCODE_DONE:
            import time
            time.sleep(0.3)
            positions = mt5.positions_get(symbol=spot_symbol)
            for pos in positions:
                if pos.magic == 111111:
                    spot_tickets.append(pos.ticket)
                    print(f"    Spot position ticket: {pos.ticket}")

        # Open FUTURES position (SELL - opposite side for arbitrage)
        if futures_symbol != spot_symbol:
            futures_info = mt5.symbol_info(futures_symbol)
            futures_tick = mt5.symbol_info_tick(futures_symbol)
        else:
            futures_info = spot_info
            futures_tick = mt5.symbol_info_tick(spot_symbol)

        futures_request = {
            "action": mt5.TRADE_ACTION_DEAL,
            "symbol": futures_symbol,
            "volume": futures_info.volume_min,
            "type": mt5.ORDER_TYPE_SELL,
            "price": futures_tick.bid,
            "deviation": 20,
            "magic": 222222,
            "comment": "Arb Futures SELL",
            "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": mt5.ORDER_FILLING_IOC,
        }

        futures_result = mt5.order_send(futures_request)
        print(f"\n  Futures SELL: retcode={futures_result.retcode}, order={futures_result.order}")

        if futures_result.retcode == mt5.TRADE_RETCODE_DONE:
            import time
            time.sleep(0.3)
            positions = mt5.positions_get(symbol=futures_symbol)
            for pos in positions:
                if pos.magic == 222222:
                    futures_tickets.append(pos.ticket)
                    print(f"    Futures position ticket: {pos.ticket}")

        # Store tickets for closing
        print(f"\n  OPENED POSITIONS:")
        print(f"    Spot tickets: {spot_tickets}")
        print(f"    Futures tickets: {futures_tickets}")

        # Now close both positions by their tickets
        import time
        time.sleep(0.5)

        # Close spot (SELL to close BUY)
        for ticket in spot_tickets:
            pos = mt5.positions_get(ticket=ticket)
            if pos:
                tick = mt5.symbol_info_tick(spot_symbol)
                close_req = {
                    "action": mt5.TRADE_ACTION_DEAL,
                    "symbol": spot_symbol,
                    "volume": pos[0].volume,
                    "type": mt5.ORDER_TYPE_SELL,
                    "position": ticket,
                    "price": tick.bid,
                    "deviation": 20,
                    "magic": 111111,
                    "comment": "Arb Spot CLOSE",
                    "type_time": mt5.ORDER_TIME_GTC,
                    "type_filling": mt5.ORDER_FILLING_IOC,
                }
                close_result = mt5.order_send(close_req)
                print(f"\n  Closed spot ticket {ticket}: {close_result.retcode}")

        # Close futures (BUY to close SELL)
        for ticket in futures_tickets:
            pos = mt5.positions_get(ticket=ticket)
            if pos:
                tick = mt5.symbol_info_tick(futures_symbol)
                close_req = {
                    "action": mt5.TRADE_ACTION_DEAL,
                    "symbol": futures_symbol,
                    "volume": pos[0].volume,
                    "type": mt5.ORDER_TYPE_BUY,
                    "position": ticket,
                    "price": tick.ask,
                    "deviation": 20,
                    "magic": 222222,
                    "comment": "Arb Futures CLOSE",
                    "type_time": mt5.ORDER_TIME_GTC,
                    "type_filling": mt5.ORDER_FILLING_IOC,
                }
                close_result = mt5.order_send(close_req)
                print(f"  Closed futures ticket {ticket}: {close_result.retcode}")

        mt5.shutdown()


if __name__ == "__main__":
    # Run tests directly
    pytest.main([__file__, "-v", "-s"])
