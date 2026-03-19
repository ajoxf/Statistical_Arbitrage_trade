"""
Pegged Limit Order Executor for Spread Trades.

Implements maker-style execution for spread trades by placing limit orders
that "peg" to the best bid/ask and follow the market until filled or timeout.

Benefits:
- Lower fees (maker vs taker)
- Less slippage
- Better entry/exit prices

Risks:
- May not fill in fast markets
- Leg risk if partial fill (handled by market close)
"""

import logging
import time
from datetime import datetime, timedelta
from typing import Optional, Tuple, Dict, Any
from dataclasses import dataclass, field
from enum import Enum

logger = logging.getLogger(__name__)


class ExecutionMode(Enum):
    """Order execution mode."""
    MARKET = "MARKET"
    PEGGED_LIMIT = "PEGGED_LIMIT"


class LegStatus(Enum):
    """Status of a single leg in a spread trade."""
    PENDING = "PENDING"
    OPEN = "OPEN"
    PARTIAL = "PARTIAL"
    FILLED = "FILLED"
    CANCELLED = "CANCELLED"
    FAILED = "FAILED"


@dataclass
class LegOrder:
    """Represents one leg of a spread trade."""
    symbol: str
    side: str  # BUY or SELL
    quantity: float
    target_price: float = 0.0
    order_ticket: int = 0  # MT5 order ticket (current)
    order_ticket_history: list = field(default_factory=list)  # All order tickets used (for re-pegged orders)
    position_ticket: int = 0  # MT5 position ticket (for closing)
    status: LegStatus = LegStatus.PENDING
    filled_qty: float = 0.0
    filled_price: float = 0.0
    last_update: Optional[datetime] = None


@dataclass
class SpreadOrder:
    """Represents a complete spread trade (both legs)."""
    spot_leg: LegOrder
    futures_leg: LegOrder
    created_at: datetime = field(default_factory=datetime.now)
    timeout_at: datetime = None
    is_entry: bool = True  # True for entry, False for exit
    position_type: str = ""  # LONG or SHORT

    @property
    def is_complete(self) -> bool:
        """Check if both legs are filled."""
        return (self.spot_leg.status == LegStatus.FILLED and
                self.futures_leg.status == LegStatus.FILLED)

    @property
    def is_failed(self) -> bool:
        """Check if either leg failed."""
        return (self.spot_leg.status == LegStatus.FAILED or
                self.futures_leg.status == LegStatus.FAILED)

    @property
    def has_partial_fill(self) -> bool:
        """Check if we have a partial fill (leg risk situation)."""
        spot_filled = self.spot_leg.status in (LegStatus.FILLED, LegStatus.PARTIAL)
        futures_filled = self.futures_leg.status in (LegStatus.FILLED, LegStatus.PARTIAL)
        return spot_filled != futures_filled


class PeggedOrderExecutor:
    """
    Handles pegged limit order execution for spread trades using MT5.

    Places limit orders near best bid/ask and continuously adjusts (re-pegs)
    them as the market moves until filled or timeout.
    """

    # How often to update limit order prices (seconds)
    PRICE_UPDATE_INTERVAL_SEC = 0.2  # 200ms

    def __init__(self, config):
        """
        Initialize the executor.

        Args:
            config: TradingConfig with execution parameters
        """
        self.config = config
        self.active_order: Optional[SpreadOrder] = None
        self._executing = False
        self._logger = logging.getLogger(__name__)

    def update_config(self, config) -> None:
        """Update configuration."""
        self.config = config

    def execute_entry(
        self,
        position_type: str,
        spot_broker,
        futures_broker,
        spot_tick: Dict[str, float],
        futures_tick: Dict[str, float],
        quantity: float,
        spot_position_ticket: int = None,
        futures_position_ticket: int = None,
    ) -> Optional[SpreadOrder]:
        """
        Execute entry trade for a spread position.

        LONG spread: Buy spot, Sell futures
        SHORT spread: Sell spot, Buy futures

        Args:
            position_type: 'LONG' or 'SHORT'
            spot_broker: Spot broker configuration
            futures_broker: Futures broker configuration
            spot_tick: {'bid': float, 'ask': float}
            futures_tick: {'bid': float, 'ask': float}
            quantity: Trade size (lot size)

        Returns:
            SpreadOrder with execution results
        """
        if self._executing:
            self._logger.warning("[PEGGED] Already executing an order")
            return None

        # Determine leg sides
        if position_type == "LONG":
            spot_side = "BUY"
            futures_side = "SELL"
        else:  # SHORT
            spot_side = "SELL"
            futures_side = "BUY"

        # Create spread order
        spread_order = SpreadOrder(
            spot_leg=LegOrder(
                symbol=spot_broker.symbol,
                side=spot_side,
                quantity=quantity,
                position_ticket=spot_position_ticket or 0,
            ),
            futures_leg=LegOrder(
                symbol=futures_broker.symbol,
                side=futures_side,
                quantity=quantity,
                position_ticket=futures_position_ticket or 0,
            ),
            is_entry=True,
            position_type=position_type,
            timeout_at=datetime.now() + timedelta(seconds=self.config.limit_order_timeout_sec),
        )

        return self._execute_spread(spread_order, spot_broker, futures_broker,
                                    spot_tick, futures_tick)

    def execute_exit(
        self,
        position_type: str,
        spot_broker,
        futures_broker,
        spot_tick: Dict[str, float],
        futures_tick: Dict[str, float],
        quantity: float,
        spot_position_ticket: int = None,
        futures_position_ticket: int = None,
    ) -> Optional[SpreadOrder]:
        """
        Execute exit trade to close a spread position.

        Close LONG spread: Sell spot, Buy futures
        Close SHORT spread: Buy spot, Sell futures
        """
        if self._executing:
            self._logger.warning("[PEGGED] Already executing an order")
            return None

        # Opposite of entry
        if position_type == "LONG":
            spot_side = "SELL"
            futures_side = "BUY"
        else:  # SHORT
            spot_side = "BUY"
            futures_side = "SELL"

        spread_order = SpreadOrder(
            spot_leg=LegOrder(
                symbol=spot_broker.symbol,
                side=spot_side,
                quantity=quantity,
                position_ticket=spot_position_ticket or 0,
            ),
            futures_leg=LegOrder(
                symbol=futures_broker.symbol,
                side=futures_side,
                quantity=quantity,
                position_ticket=futures_position_ticket or 0,
            ),
            is_entry=False,
            position_type=position_type,
            timeout_at=datetime.now() + timedelta(seconds=self.config.limit_order_timeout_sec),
        )

        return self._execute_spread(spread_order, spot_broker, futures_broker,
                                    spot_tick, futures_tick)

    def _execute_spread(
        self,
        spread_order: SpreadOrder,
        spot_broker,
        futures_broker,
        spot_tick: Dict[str, float],
        futures_tick: Dict[str, float],
    ) -> SpreadOrder:
        """Execute a spread order using configured mode."""
        self._executing = True
        self.active_order = spread_order

        try:
            # Note: Settings UI saves to 'order_type' field (MARKET, LIMIT, PEGGED_LIMIT)
            mode = getattr(self.config, 'order_type', 'MARKET')
            self._logger.info(f"[PEGGED] Execution mode from config: '{mode}'")

            if mode == "MARKET":
                return self._execute_market(spread_order, spot_broker, futures_broker)
            else:
                return self._execute_pegged_limit(spread_order, spot_broker, futures_broker,
                                                  spot_tick, futures_tick)
        finally:
            self._executing = False
            self.active_order = None

    def _execute_market(
        self,
        spread_order: SpreadOrder,
        spot_broker,
        futures_broker,
    ) -> SpreadOrder:
        """Execute spread using market orders (immediate fill)."""
        import MetaTrader5 as mt5

        self._logger.info(f"[PEGGED] Executing spread with MARKET orders: {spread_order.position_type} "
                         f"{'ENTRY' if spread_order.is_entry else 'EXIT'}")

        if not mt5.initialize():
            self._logger.error("[PEGGED] MT5 initialization failed")
            spread_order.spot_leg.status = LegStatus.FAILED
            spread_order.futures_leg.status = LegStatus.FAILED
            return spread_order

        try:
            # Execute spot leg
            spot_result = self._place_market_order_mt5(
                mt5, spot_broker.symbol, spread_order.spot_leg,
                spread_order.spot_leg.position_ticket
            )
            self._update_leg_from_result(spread_order.spot_leg, spot_result)

            # Execute futures leg
            futures_result = self._place_market_order_mt5(
                mt5, futures_broker.symbol, spread_order.futures_leg,
                spread_order.futures_leg.position_ticket
            )
            self._update_leg_from_result(spread_order.futures_leg, futures_result)

            # Handle partial fills (leg risk)
            if spread_order.has_partial_fill:
                self._logger.warning("[PEGGED] PARTIAL FILL - Leg risk detected!")
                self._handle_leg_risk(spread_order, spot_broker, futures_broker, mt5)

        finally:
            mt5.shutdown()

        return spread_order

    def _cancel_all_pending_orders(self, mt5, symbols: list) -> int:
        """
        Cancel ALL pending orders for the given symbols.

        This is critical to prevent orphan orders from accumulating when:
        - Previous execution timed out
        - Cancel failed during amend
        - Multiple signals triggered before cleanup

        Returns: Number of orders cancelled
        """
        cancelled_count = 0

        for symbol in symbols:
            try:
                orders = mt5.orders_get(symbol=symbol)
                if orders is None or len(orders) == 0:
                    continue

                self._logger.warning(f"[PEGGED] Found {len(orders)} pending orders for {symbol} - cancelling all")

                for order in orders:
                    try:
                        cancel_request = {
                            "action": mt5.TRADE_ACTION_REMOVE,
                            "order": order.ticket,
                        }
                        result = mt5.order_send(cancel_request)
                        if result and result.retcode == mt5.TRADE_RETCODE_DONE:
                            self._logger.info(f"[PEGGED] Cancelled orphan order {order.ticket} for {symbol}")
                            cancelled_count += 1
                        else:
                            self._logger.warning(f"[PEGGED] Failed to cancel order {order.ticket}: {result.retcode if result else 'None'}")
                    except Exception as e:
                        self._logger.error(f"[PEGGED] Error cancelling order {order.ticket}: {e}")

            except Exception as e:
                self._logger.error(f"[PEGGED] Error getting orders for {symbol}: {e}")

        if cancelled_count > 0:
            self._logger.info(f"[PEGGED] Cancelled {cancelled_count} orphan orders total")

        return cancelled_count

    def _execute_pegged_limit(
        self,
        spread_order: SpreadOrder,
        spot_broker,
        futures_broker,
        spot_tick: Dict[str, float],
        futures_tick: Dict[str, float],
    ) -> SpreadOrder:
        """Execute spread using pegged limit orders."""
        import MetaTrader5 as mt5

        self._logger.info("-" * 50)
        self._logger.info(f"[PEGGED] Starting PEGGED LIMIT execution: {spread_order.position_type} {'ENTRY' if spread_order.is_entry else 'EXIT'}")
        self._logger.info("-" * 50)

        if not mt5.initialize():
            self._logger.error("[PEGGED] MT5 initialization failed")
            spread_order.spot_leg.status = LegStatus.FAILED
            spread_order.futures_leg.status = LegStatus.FAILED
            return spread_order

        try:
            # CRITICAL: Cancel ALL existing pending orders for these symbols first
            # This prevents orphan orders from accumulating after failed/timed-out executions
            self._cancel_all_pending_orders(mt5, [spot_broker.symbol, futures_broker.symbol])

            # Calculate initial target prices
            self._update_target_prices(spread_order, spot_tick, futures_tick)

            # Place initial limit orders
            self._place_limit_orders(spread_order, spot_broker, futures_broker, mt5)

            # Monitor and adjust until filled or timeout
            while not spread_order.is_complete and not spread_order.is_failed:
                # Check timeout
                if datetime.now() >= spread_order.timeout_at:
                    self._logger.warning("[PEGGED] Limit order timeout reached")
                    self._handle_timeout(spread_order, spot_broker, futures_broker, mt5)
                    break

                # Wait before next update
                time.sleep(self.PRICE_UPDATE_INTERVAL_SEC)

                # Get fresh ticks
                spot_tick_info = mt5.symbol_info_tick(spot_broker.symbol)
                futures_tick_info = mt5.symbol_info_tick(futures_broker.symbol)

                if not spot_tick_info or not futures_tick_info:
                    continue

                new_spot_tick = {'bid': spot_tick_info.bid, 'ask': spot_tick_info.ask}
                new_futures_tick = {'bid': futures_tick_info.bid, 'ask': futures_tick_info.ask}

                # Check order status
                self._check_order_status(spread_order, mt5)

                # CRITICAL: If one leg filled, immediately fill the other with market order
                # This prevents infinite re-pegging and leg risk exposure
                if spread_order.has_partial_fill:
                    self._logger.warning("[PEGGED] PARTIAL FILL DETECTED - One leg filled, switching to MARKET for remaining leg")
                    self._fill_remaining_leg_with_market(spread_order, spot_broker, futures_broker, mt5)
                    break

                # If not filled, update prices (re-peg)
                if not spread_order.is_complete:
                    old_spot_price = spread_order.spot_leg.target_price
                    old_futures_price = spread_order.futures_leg.target_price

                    self._update_target_prices(spread_order, new_spot_tick, new_futures_tick)

                    # Get tick sizes for comparison threshold
                    spot_info = mt5.symbol_info(spot_broker.symbol)
                    futures_info = mt5.symbol_info(futures_broker.symbol)
                    spot_tick_size = spot_info.trade_tick_size if spot_info else 0.01
                    futures_tick_size = futures_info.trade_tick_size if futures_info else 0.01

                    # Amend orders if prices changed by more than 1 tick
                    spot_price_changed = abs(spread_order.spot_leg.target_price - old_spot_price) > spot_tick_size
                    futures_price_changed = abs(spread_order.futures_leg.target_price - old_futures_price) > futures_tick_size

                    if spot_price_changed or futures_price_changed:
                        self._amend_limit_orders(spread_order, spot_broker, futures_broker, mt5)

            # Handle partial fills
            if spread_order.has_partial_fill:
                self._logger.warning("[PEGGED] PARTIAL FILL after limit execution - Leg risk!")
                self._handle_leg_risk(spread_order, spot_broker, futures_broker, mt5)

        finally:
            mt5.shutdown()

        return spread_order

    def _update_target_prices(
        self,
        spread_order: SpreadOrder,
        spot_tick: Dict[str, float],
        futures_tick: Dict[str, float],
    ) -> None:
        """
        Calculate target prices for limit orders based on current orderbook.

        MT5 Limit Order Rules:
        - BUY_LIMIT: price must be BELOW current Ask (we wait to buy cheaper)
        - SELL_LIMIT: price must be ABOVE current Bid (we wait to sell higher)

        Strategy: Place near best bid/ask to be a maker, but ensure price is valid.
        - For BUY: place at bid (or bid + tiny offset, but must stay < ask)
        - For SELL: place at ask (or ask - tiny offset, but must stay > bid)
        """
        offset_bps = getattr(self.config, 'limit_order_price_offset_bps', 1.0) / 10000

        # SPOT LEG
        spot_bid = spot_tick['bid']
        spot_ask = spot_tick['ask']
        if spread_order.spot_leg.side == "BUY":
            # BUY_LIMIT: price < ask, place near bid to be maker
            target = spot_bid * (1 + offset_bps)
            # CRITICAL: Ensure price stays below ask
            if target >= spot_ask:
                target = spot_bid  # Fall back to bid
            spread_order.spot_leg.target_price = target
        else:
            # SELL_LIMIT: price > bid, place near ask to be maker
            target = spot_ask * (1 - offset_bps)
            # CRITICAL: Ensure price stays above bid
            if target <= spot_bid:
                target = spot_ask  # Fall back to ask
            spread_order.spot_leg.target_price = target

        # FUTURES LEG
        futures_bid = futures_tick['bid']
        futures_ask = futures_tick['ask']
        if spread_order.futures_leg.side == "BUY":
            # BUY_LIMIT: price < ask
            target = futures_bid * (1 + offset_bps)
            if target >= futures_ask:
                target = futures_bid
            spread_order.futures_leg.target_price = target
        else:
            # SELL_LIMIT: price > bid
            target = futures_ask * (1 - offset_bps)
            if target <= futures_bid:
                target = futures_ask
            spread_order.futures_leg.target_price = target

        self._logger.info(f"[PEGGED] Target prices calculated: "
                         f"spot={spread_order.spot_leg.target_price:.2f} ({spread_order.spot_leg.side}), "
                         f"futures={spread_order.futures_leg.target_price:.2f} ({spread_order.futures_leg.side})")
        self._logger.info(f"[PEGGED] Market: spot bid/ask={spot_bid:.2f}/{spot_ask:.2f}, "
                         f"futures bid/ask={futures_bid:.2f}/{futures_ask:.2f}")

    def _place_market_order_mt5(
        self,
        mt5,
        symbol: str,
        leg: LegOrder,
        position_ticket: int = None,
    ) -> Dict[str, Any]:
        """Place a market order using MT5."""
        order_type = mt5.ORDER_TYPE_BUY if leg.side == "BUY" else mt5.ORDER_TYPE_SELL

        tick = mt5.symbol_info_tick(symbol)
        if not tick:
            return {'success': False, 'error': 'Could not get tick'}

        price = tick.ask if leg.side == "BUY" else tick.bid

        # Get filling mode
        symbol_info = mt5.symbol_info(symbol)
        filling_mode = mt5.ORDER_FILLING_RETURN
        if symbol_info:
            fm = symbol_info.filling_mode
            if fm == 1:
                filling_mode = mt5.ORDER_FILLING_FOK
            elif fm == 2:
                filling_mode = mt5.ORDER_FILLING_IOC

        request = {
            "action": mt5.TRADE_ACTION_DEAL,
            "symbol": symbol,
            "volume": leg.quantity,
            "type": order_type,
            "price": price,
            "deviation": 20,
            "magic": 123456,
            "comment": "Pegged Market",
            "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": filling_mode,
        }

        if position_ticket:
            request["position"] = int(position_ticket)

        result = mt5.order_send(request)

        if result is None:
            return {'success': False, 'error': 'order_send returned None'}

        if result.retcode == mt5.TRADE_RETCODE_DONE:
            return {
                'success': True,
                'order_id': result.order,
                'filled_qty': leg.quantity,
                'filled_price': result.price,
            }
        else:
            return {'success': False, 'error': f'{result.retcode} - {result.comment}'}

    def _place_limit_orders(
        self,
        spread_order: SpreadOrder,
        spot_broker,
        futures_broker,
        mt5,
    ) -> None:
        """Place initial limit orders for both legs.

        CRITICAL: If spot order fails, we don't place futures to avoid orphaned orders.
        If futures order fails after spot succeeds, we cancel the spot order.
        """
        self._logger.info(f"[PEGGED] Placing limit orders: spot={spot_broker.symbol}, futures={futures_broker.symbol}")

        # Place spot limit order FIRST
        spot_result = self._place_limit_order_mt5(
            mt5, spot_broker.symbol, spread_order.spot_leg,
            spread_order.spot_leg.position_ticket
        )

        if spot_result['success']:
            spread_order.spot_leg.order_ticket = spot_result['order_id']
            spread_order.spot_leg.order_ticket_history.append(spot_result['order_id'])
            spread_order.spot_leg.status = LegStatus.OPEN
            self._logger.info(f"[PEGGED] Spot limit order placed: ticket={spot_result['order_id']}, "
                             f"price={spread_order.spot_leg.target_price:.2f}")
        else:
            spread_order.spot_leg.status = LegStatus.FAILED
            self._logger.error(f"[PEGGED] Failed to place spot limit order: {spot_result['error']}")
            # DON'T place futures order if spot failed - would create orphaned order!
            self._logger.error("[PEGGED] Aborting - will not place futures order since spot failed")
            return

        # Place futures limit order (only if spot succeeded)
        futures_result = self._place_limit_order_mt5(
            mt5, futures_broker.symbol, spread_order.futures_leg,
            spread_order.futures_leg.position_ticket
        )

        if futures_result['success']:
            spread_order.futures_leg.order_ticket = futures_result['order_id']
            spread_order.futures_leg.order_ticket_history.append(futures_result['order_id'])
            spread_order.futures_leg.status = LegStatus.OPEN
            self._logger.info(f"[PEGGED] Futures limit order placed: ticket={futures_result['order_id']}, "
                             f"price={spread_order.futures_leg.target_price:.2f}")
        else:
            spread_order.futures_leg.status = LegStatus.FAILED
            self._logger.error(f"[PEGGED] Failed to place futures limit order: {futures_result['error']}")
            # Cancel the spot order since futures failed - avoid orphaned spot order
            self._logger.warning("[PEGGED] Futures failed - cancelling spot order to prevent orphan")
            try:
                cancel_request = {
                    "action": mt5.TRADE_ACTION_REMOVE,
                    "order": spread_order.spot_leg.order_ticket,
                }
                cancel_result = mt5.order_send(cancel_request)
                if cancel_result and cancel_result.retcode == mt5.TRADE_RETCODE_DONE:
                    spread_order.spot_leg.status = LegStatus.CANCELLED
                    self._logger.info("[PEGGED] Spot order cancelled successfully")
                else:
                    self._logger.error(f"[PEGGED] Failed to cancel spot order: {cancel_result.retcode if cancel_result else 'None'}")
            except Exception as e:
                self._logger.error(f"[PEGGED] Exception cancelling spot order: {e}")

    def _place_limit_order_mt5(
        self,
        mt5,
        symbol: str,
        leg: LegOrder,
        position_ticket: int = None,
    ) -> Dict[str, Any]:
        """Place a limit order using MT5."""
        # Determine order type
        if leg.side == "BUY":
            order_type = mt5.ORDER_TYPE_BUY_LIMIT
        else:
            order_type = mt5.ORDER_TYPE_SELL_LIMIT

        # Get symbol info for tick size rounding
        symbol_info = mt5.symbol_info(symbol)
        if not symbol_info:
            self._logger.error(f"[PEGGED] Could not get symbol info for {symbol}")
            return {'success': False, 'error': f'Could not get symbol info for {symbol}'}

        # Ensure symbol is visible in Market Watch
        if not symbol_info.visible:
            self._logger.info(f"[PEGGED] Selecting symbol {symbol} in Market Watch")
            mt5.symbol_select(symbol, True)
            symbol_info = mt5.symbol_info(symbol)

        tick_size = symbol_info.trade_tick_size if symbol_info.trade_tick_size > 0 else 0.01

        # Get fresh tick - CRITICAL: market may have moved since target was calculated
        tick = mt5.symbol_info_tick(symbol)
        if not tick:
            self._logger.error(f"[PEGGED] Could not get tick for {symbol}")
            return {'success': False, 'error': f'Could not get tick for {symbol}'}

        self._logger.info(f"[PEGGED] Market: bid={tick.bid:.2f}, ask={tick.ask:.2f}")

        # For pegged limit orders, ALWAYS use current bid/ask to ensure validity
        # Some brokers require:
        # - BUY_LIMIT: price <= bid (to be a maker in the bid queue)
        # - SELL_LIMIT: price >= ask (to be a maker in the ask queue)
        #
        # Using the exact bid/ask ensures we're always valid
        if leg.side == "BUY":
            # BUY LIMIT at current bid (maker style)
            price = round(tick.bid / tick_size) * tick_size
            if leg.target_price != tick.bid:
                self._logger.info(f"[PEGGED] BUY price: using current bid={price:.2f} (target was {leg.target_price:.2f})")
        else:  # SELL
            # SELL LIMIT at current ask (maker style)
            price = round(tick.ask / tick_size) * tick_size
            if leg.target_price != tick.ask:
                self._logger.info(f"[PEGGED] SELL price: using current ask={price:.2f} (target was {leg.target_price:.2f})")

        # Get filling mode - use bitwise check for proper detection
        FILLING_FOK = getattr(mt5, 'SYMBOL_FILLING_FOK', 1)
        FILLING_IOC = getattr(mt5, 'SYMBOL_FILLING_IOC', 2)
        fm = symbol_info.filling_mode

        filling_mode = mt5.ORDER_FILLING_RETURN  # Default for pending orders
        if fm & FILLING_FOK:
            filling_mode = mt5.ORDER_FILLING_FOK
        elif fm & FILLING_IOC:
            filling_mode = mt5.ORDER_FILLING_IOC

        self._logger.info(f"[PEGGED] Placing {leg.side} LIMIT: {symbol}, price={price:.2f}, "
                         f"qty={leg.quantity}, filling_mode={fm}→{filling_mode}")

        request = {
            "action": mt5.TRADE_ACTION_PENDING,
            "symbol": symbol,
            "volume": leg.quantity,
            "type": order_type,
            "price": price,
            "magic": 123456,
            "comment": "Pegged Limit",
            "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": filling_mode,
        }

        if position_ticket:
            request["position"] = int(position_ticket)

        result = mt5.order_send(request)

        if result is None:
            error = mt5.last_error()
            self._logger.error(f"[PEGGED] order_send returned None for {symbol}, last_error: {error}")
            return {'success': False, 'error': f'order_send returned None: {error}'}

        if result.retcode == mt5.TRADE_RETCODE_DONE:
            self._logger.info(f"[PEGGED] Order placed successfully: ticket={result.order}")
            return {'success': True, 'order_id': result.order}
        else:
            self._logger.error(f"[PEGGED] Order rejected for {symbol}: retcode={result.retcode}, comment={result.comment}")
            return {'success': False, 'error': f'{result.retcode} - {result.comment}'}

    def _check_order_status(self, spread_order: SpreadOrder, mt5) -> None:
        """Check the fill status of both legs.

        Uses multiple detection methods:
        1. Check if order still pending (orders_get)
        2. Check deal history with time range (history_deals_get)
        3. Check open positions as fallback (positions_get)
        """
        from datetime import datetime, timedelta

        # Time range for deal history (last 24 hours)
        time_from = datetime.now() - timedelta(hours=24)

        # Check spot leg
        if spread_order.spot_leg.status == LegStatus.OPEN:
            orders = mt5.orders_get(ticket=spread_order.spot_leg.order_ticket)
            if orders is None or len(orders) == 0:
                # Order no longer pending - check if filled
                self._logger.info(f"[PEGGED] Spot order {spread_order.spot_leg.order_ticket} not in pending, checking fills...")

                # Method 1: Check deal history for ALL order tickets in history (handles re-pegged orders)
                all_deals = []
                tickets_to_check = spread_order.spot_leg.order_ticket_history if spread_order.spot_leg.order_ticket_history else [spread_order.spot_leg.order_ticket]
                for ticket in tickets_to_check:
                    deals = mt5.history_deals_get(time_from, datetime.now(), order=ticket)
                    if deals and len(deals) > 0:
                        all_deals.extend(deals)
                        self._logger.info(f"[PEGGED] Found {len(deals)} deals for spot order ticket {ticket}")

                if all_deals:
                    total_filled = sum(d.volume for d in all_deals)
                    avg_price = sum(d.price * d.volume for d in all_deals) / total_filled if total_filled > 0 else 0
                    spread_order.spot_leg.filled_qty = total_filled
                    spread_order.spot_leg.filled_price = avg_price
                    spread_order.spot_leg.status = LegStatus.FILLED if total_filled >= spread_order.spot_leg.quantity else LegStatus.PARTIAL
                    # Get position_id from most recent deal for later closing
                    latest_deal = max(all_deals, key=lambda d: getattr(d, 'time', 0))
                    if hasattr(latest_deal, 'position_id') and latest_deal.position_id:
                        spread_order.spot_leg.position_ticket = latest_deal.position_id
                        self._logger.info(f"[PEGGED] Spot leg filled (from deals): qty={total_filled}, price={avg_price:.2f}, pos_id={latest_deal.position_id}")
                    else:
                        self._logger.info(f"[PEGGED] Spot leg filled (from deals): qty={total_filled}, price={avg_price:.2f}")
                else:
                    # Method 2: Check if there's an open position for this symbol
                    positions = mt5.positions_get(symbol=spread_order.spot_leg.symbol)
                    if positions and len(positions) > 0:
                        # Find most recent position matching our direction
                        for pos in positions:
                            pos_side = "BUY" if pos.type == mt5.POSITION_TYPE_BUY else "SELL"
                            if pos_side == spread_order.spot_leg.side and abs(pos.volume - spread_order.spot_leg.quantity) < 0.001:
                                spread_order.spot_leg.filled_qty = pos.volume
                                spread_order.spot_leg.filled_price = pos.price_open
                                spread_order.spot_leg.status = LegStatus.FILLED
                                spread_order.spot_leg.position_ticket = pos.ticket
                                self._logger.info(f"[PEGGED] Spot leg filled (from position): qty={pos.volume}, price={pos.price_open:.2f}, pos_ticket={pos.ticket}")
                                break

                    if spread_order.spot_leg.status == LegStatus.OPEN:
                        self._logger.warning(f"[PEGGED] Spot order {spread_order.spot_leg.order_ticket} gone but no fill detected (checked tickets: {tickets_to_check})")

        # Check futures leg
        if spread_order.futures_leg.status == LegStatus.OPEN:
            orders = mt5.orders_get(ticket=spread_order.futures_leg.order_ticket)
            if orders is None or len(orders) == 0:
                self._logger.info(f"[PEGGED] Futures order {spread_order.futures_leg.order_ticket} not in pending, checking fills...")

                # Method 1: Check deal history for ALL order tickets in history (handles re-pegged orders)
                all_deals = []
                tickets_to_check = spread_order.futures_leg.order_ticket_history if spread_order.futures_leg.order_ticket_history else [spread_order.futures_leg.order_ticket]
                for ticket in tickets_to_check:
                    deals = mt5.history_deals_get(time_from, datetime.now(), order=ticket)
                    if deals and len(deals) > 0:
                        all_deals.extend(deals)
                        self._logger.info(f"[PEGGED] Found {len(deals)} deals for futures order ticket {ticket}")

                if all_deals:
                    total_filled = sum(d.volume for d in all_deals)
                    avg_price = sum(d.price * d.volume for d in all_deals) / total_filled if total_filled > 0 else 0
                    spread_order.futures_leg.filled_qty = total_filled
                    spread_order.futures_leg.filled_price = avg_price
                    spread_order.futures_leg.status = LegStatus.FILLED if total_filled >= spread_order.futures_leg.quantity else LegStatus.PARTIAL
                    # Get position_id from most recent deal for later closing
                    latest_deal = max(all_deals, key=lambda d: getattr(d, 'time', 0))
                    if hasattr(latest_deal, 'position_id') and latest_deal.position_id:
                        spread_order.futures_leg.position_ticket = latest_deal.position_id
                        self._logger.info(f"[PEGGED] Futures leg filled (from deals): qty={total_filled}, price={avg_price:.2f}, pos_id={latest_deal.position_id}")
                    else:
                        self._logger.info(f"[PEGGED] Futures leg filled (from deals): qty={total_filled}, price={avg_price:.2f}")
                else:
                    # Method 2: Check if there's an open position for this symbol
                    positions = mt5.positions_get(symbol=spread_order.futures_leg.symbol)
                    if positions and len(positions) > 0:
                        for pos in positions:
                            pos_side = "BUY" if pos.type == mt5.POSITION_TYPE_BUY else "SELL"
                            if pos_side == spread_order.futures_leg.side and abs(pos.volume - spread_order.futures_leg.quantity) < 0.001:
                                spread_order.futures_leg.filled_qty = pos.volume
                                spread_order.futures_leg.filled_price = pos.price_open
                                spread_order.futures_leg.status = LegStatus.FILLED
                                spread_order.futures_leg.position_ticket = pos.ticket
                                self._logger.info(f"[PEGGED] Futures leg filled (from position): qty={pos.volume}, price={pos.price_open:.2f}, pos_ticket={pos.ticket}")
                                break

                    if spread_order.futures_leg.status == LegStatus.OPEN:
                        self._logger.warning(f"[PEGGED] Futures order {spread_order.futures_leg.order_ticket} gone but no fill detected (checked tickets: {tickets_to_check})")

    def _amend_limit_orders(
        self,
        spread_order: SpreadOrder,
        spot_broker,
        futures_broker,
        mt5,
    ) -> None:
        """Amend (cancel and replace) existing limit orders with new prices.

        CRITICAL: Must NOT use 'return' for one leg - would skip the other leg!
        Must verify cancel succeeded before placing new order.
        """
        # Amend spot order if still open
        if spread_order.spot_leg.status == LegStatus.OPEN:
            try:
                # First check if order still exists as pending
                orders = mt5.orders_get(ticket=spread_order.spot_leg.order_ticket)
                if orders is None or len(orders) == 0:
                    # Order no longer pending - might have filled or been cancelled
                    self._logger.info(f"[PEGGED] Spot order {spread_order.spot_leg.order_ticket} no longer pending, skipping amend")
                    # Check if it filled
                    self._check_order_status(spread_order, mt5)
                    # NOTE: Don't return - still need to check futures leg!
                else:
                    # Cancel existing order
                    old_ticket = spread_order.spot_leg.order_ticket
                    cancel_request = {
                        "action": mt5.TRADE_ACTION_REMOVE,
                        "order": old_ticket,
                    }
                    cancel_result = mt5.order_send(cancel_request)

                    # Check if cancel succeeded
                    if cancel_result is None or cancel_result.retcode != mt5.TRADE_RETCODE_DONE:
                        self._logger.warning(f"[PEGGED] Failed to cancel spot order {old_ticket}: {cancel_result.retcode if cancel_result else 'None'}")
                        # Check if order filled while we tried to cancel
                        self._check_order_status(spread_order, mt5)
                        # NOTE: Don't place new order, but DON'T return - check futures
                    else:
                        self._logger.info(f"[PEGGED] Cancelled spot order {old_ticket}")
                        # Verify order is actually gone before placing new one
                        time.sleep(0.05)  # Brief pause to ensure cancel is processed
                        verify = mt5.orders_get(ticket=old_ticket)
                        if verify and len(verify) > 0:
                            self._logger.error(f"[PEGGED] Spot order {old_ticket} still exists after cancel!")
                        else:
                            # Place new order at updated price
                            remaining_qty = spread_order.spot_leg.quantity - spread_order.spot_leg.filled_qty
                            if remaining_qty > 0:
                                result = self._place_limit_order_mt5(
                                    mt5, spot_broker.symbol,
                                    LegOrder(
                                        symbol=spot_broker.symbol,
                                        side=spread_order.spot_leg.side,
                                        quantity=remaining_qty,
                                        target_price=spread_order.spot_leg.target_price,
                                    ),
                                    spread_order.spot_leg.position_ticket
                                )
                                if result['success']:
                                    spread_order.spot_leg.order_ticket = result['order_id']
                                    spread_order.spot_leg.order_ticket_history.append(result['order_id'])
                                    self._logger.info(f"[PEGGED] Spot order re-pegged: new ticket={result['order_id']}, history={spread_order.spot_leg.order_ticket_history}")
                                else:
                                    self._logger.error(f"[PEGGED] Failed to re-peg spot order: {result['error']}")
            except Exception as e:
                self._logger.error(f"[PEGGED] Failed to amend spot order: {e}")

        # Amend futures order if still open
        if spread_order.futures_leg.status == LegStatus.OPEN:
            try:
                # First check if order still exists as pending
                orders = mt5.orders_get(ticket=spread_order.futures_leg.order_ticket)
                if orders is None or len(orders) == 0:
                    self._logger.info(f"[PEGGED] Futures order {spread_order.futures_leg.order_ticket} no longer pending, skipping amend")
                    self._check_order_status(spread_order, mt5)
                    # NOTE: No return needed here - this is the last leg
                else:
                    old_ticket = spread_order.futures_leg.order_ticket
                    cancel_request = {
                        "action": mt5.TRADE_ACTION_REMOVE,
                        "order": old_ticket,
                    }
                    cancel_result = mt5.order_send(cancel_request)

                    if cancel_result is None or cancel_result.retcode != mt5.TRADE_RETCODE_DONE:
                        self._logger.warning(f"[PEGGED] Failed to cancel futures order {old_ticket}: {cancel_result.retcode if cancel_result else 'None'}")
                        self._check_order_status(spread_order, mt5)
                    else:
                        self._logger.info(f"[PEGGED] Cancelled futures order {old_ticket}")
                        time.sleep(0.05)  # Brief pause to ensure cancel is processed
                        verify = mt5.orders_get(ticket=old_ticket)
                        if verify and len(verify) > 0:
                            self._logger.error(f"[PEGGED] Futures order {old_ticket} still exists after cancel!")
                        else:
                            remaining_qty = spread_order.futures_leg.quantity - spread_order.futures_leg.filled_qty
                            if remaining_qty > 0:
                                result = self._place_limit_order_mt5(
                                    mt5, futures_broker.symbol,
                                    LegOrder(
                                        symbol=futures_broker.symbol,
                                        side=spread_order.futures_leg.side,
                                        quantity=remaining_qty,
                                        target_price=spread_order.futures_leg.target_price,
                                    ),
                                    spread_order.futures_leg.position_ticket
                                )
                                if result['success']:
                                    spread_order.futures_leg.order_ticket = result['order_id']
                                    spread_order.futures_leg.order_ticket_history.append(result['order_id'])
                                    self._logger.info(f"[PEGGED] Futures order re-pegged: new ticket={result['order_id']}, history={spread_order.futures_leg.order_ticket_history}")
                                else:
                                    self._logger.error(f"[PEGGED] Failed to re-peg futures order: {result['error']}")
            except Exception as e:
                self._logger.error(f"[PEGGED] Failed to amend futures order: {e}")

    def _handle_timeout(
        self,
        spread_order: SpreadOrder,
        spot_broker,
        futures_broker,
        mt5,
    ) -> None:
        """Handle timeout - cancel unfilled orders and close any partial fills.

        CRITICAL: Check if orders filled before marking as cancelled.
        """
        self._logger.info("[PEGGED] Handling limit order timeout")

        # First, check current status of orders (they might have filled)
        self._check_order_status(spread_order, mt5)

        # Cancel spot order if still open (not already filled)
        if spread_order.spot_leg.status == LegStatus.OPEN:
            try:
                cancel_request = {
                    "action": mt5.TRADE_ACTION_REMOVE,
                    "order": spread_order.spot_leg.order_ticket,
                }
                result = mt5.order_send(cancel_request)
                if result and result.retcode == mt5.TRADE_RETCODE_DONE:
                    spread_order.spot_leg.status = LegStatus.CANCELLED
                    self._logger.info(f"[PEGGED] Spot limit order cancelled")
                else:
                    # Cancel failed - order might have filled, check again
                    self._logger.warning(f"[PEGGED] Spot cancel returned: {result.retcode if result else 'None'}, checking if filled")
                    self._check_order_status(spread_order, mt5)
            except Exception as e:
                self._logger.error(f"[PEGGED] Failed to cancel spot order: {e}")

        # Cancel futures order if still open (not already filled)
        if spread_order.futures_leg.status == LegStatus.OPEN:
            try:
                cancel_request = {
                    "action": mt5.TRADE_ACTION_REMOVE,
                    "order": spread_order.futures_leg.order_ticket,
                }
                result = mt5.order_send(cancel_request)
                if result and result.retcode == mt5.TRADE_RETCODE_DONE:
                    spread_order.futures_leg.status = LegStatus.CANCELLED
                    self._logger.info(f"[PEGGED] Futures limit order cancelled")
                else:
                    # Cancel failed - order might have filled, check again
                    self._logger.warning(f"[PEGGED] Futures cancel returned: {result.retcode if result else 'None'}, checking if filled")
                    self._check_order_status(spread_order, mt5)
            except Exception as e:
                self._logger.error(f"[PEGGED] Failed to cancel futures order: {e}")

        # Log final status
        self._logger.info(f"[PEGGED] After timeout: spot={spread_order.spot_leg.status.value}, futures={spread_order.futures_leg.status.value}")

        # Handle partial fills (one filled, one didn't)
        if spread_order.has_partial_fill:
            self._handle_leg_risk(spread_order, spot_broker, futures_broker, mt5)

    def _handle_leg_risk(
        self,
        spread_order: SpreadOrder,
        spot_broker,
        futures_broker,
        mt5,
    ) -> None:
        """
        Handle leg risk when one leg is filled but the other isn't.

        Strategy: Market-close the filled leg to eliminate directional exposure.
        """
        self._logger.warning("[PEGGED] Handling leg risk - closing partial position with market order")

        spot_filled = spread_order.spot_leg.status in (LegStatus.FILLED, LegStatus.PARTIAL)
        futures_filled = spread_order.futures_leg.status in (LegStatus.FILLED, LegStatus.PARTIAL)

        if spot_filled and not futures_filled:
            # Spot filled, futures didn't - close spot position with market order
            close_side = "SELL" if spread_order.spot_leg.side == "BUY" else "BUY"
            self._logger.warning(f"[PEGGED] Closing spot leg: {close_side} {spread_order.spot_leg.filled_qty}")

            close_leg = LegOrder(
                symbol=spot_broker.symbol,
                side=close_side,
                quantity=spread_order.spot_leg.filled_qty,
            )
            result = self._place_market_order_mt5(mt5, spot_broker.symbol, close_leg)
            self._logger.info(f"[PEGGED] Closed spot leg to handle leg risk: {result}")

        elif futures_filled and not spot_filled:
            # Futures filled, spot didn't - close futures position with market order
            close_side = "SELL" if spread_order.futures_leg.side == "BUY" else "BUY"
            self._logger.warning(f"[PEGGED] Closing futures leg: {close_side} {spread_order.futures_leg.filled_qty}")

            close_leg = LegOrder(
                symbol=futures_broker.symbol,
                side=close_side,
                quantity=spread_order.futures_leg.filled_qty,
            )
            result = self._place_market_order_mt5(mt5, futures_broker.symbol, close_leg)
            self._logger.info(f"[PEGGED] Closed futures leg to handle leg risk: {result}")

    def _fill_remaining_leg_with_market(
        self,
        spread_order: SpreadOrder,
        spot_broker,
        futures_broker,
        mt5,
    ) -> None:
        """
        Fill the remaining unfilled leg with a market order.

        Called when one leg fills during pegged limit execution.
        This prevents infinite re-pegging and reduces leg risk exposure time.
        """
        spot_filled = spread_order.spot_leg.status in (LegStatus.FILLED, LegStatus.PARTIAL)
        futures_filled = spread_order.futures_leg.status in (LegStatus.FILLED, LegStatus.PARTIAL)

        if spot_filled and not futures_filled:
            # Spot filled, need to fill futures with market
            self._logger.warning(f"[PEGGED] Spot filled, filling futures with MARKET order")

            # Cancel any pending futures limit order first
            if spread_order.futures_leg.order_ticket:
                try:
                    cancel_request = {
                        "action": mt5.TRADE_ACTION_REMOVE,
                        "order": spread_order.futures_leg.order_ticket,
                    }
                    result = mt5.order_send(cancel_request)
                    if result and result.retcode == mt5.TRADE_RETCODE_DONE:
                        self._logger.info(f"[PEGGED] Cancelled pending futures limit order {spread_order.futures_leg.order_ticket}")
                    else:
                        self._logger.warning(f"[PEGGED] Could not cancel futures order: {result.retcode if result else 'None'}")
                except Exception as e:
                    self._logger.error(f"[PEGGED] Error cancelling futures order: {e}")

            # Place market order for futures
            remaining_qty = spread_order.futures_leg.quantity - spread_order.futures_leg.filled_qty
            if remaining_qty > 0:
                market_leg = LegOrder(
                    symbol=futures_broker.symbol,
                    side=spread_order.futures_leg.side,
                    quantity=remaining_qty,
                )
                result = self._place_market_order_mt5(
                    mt5, futures_broker.symbol, market_leg,
                    spread_order.futures_leg.position_ticket
                )
                self._update_leg_from_result(spread_order.futures_leg, result)
                self._logger.info(f"[PEGGED] Futures leg filled with MARKET: {result}")

        elif futures_filled and not spot_filled:
            # Futures filled, need to fill spot with market
            self._logger.warning(f"[PEGGED] Futures filled, filling spot with MARKET order")

            # Cancel any pending spot limit order first
            if spread_order.spot_leg.order_ticket:
                try:
                    cancel_request = {
                        "action": mt5.TRADE_ACTION_REMOVE,
                        "order": spread_order.spot_leg.order_ticket,
                    }
                    result = mt5.order_send(cancel_request)
                    if result and result.retcode == mt5.TRADE_RETCODE_DONE:
                        self._logger.info(f"[PEGGED] Cancelled pending spot limit order {spread_order.spot_leg.order_ticket}")
                    else:
                        self._logger.warning(f"[PEGGED] Could not cancel spot order: {result.retcode if result else 'None'}")
                except Exception as e:
                    self._logger.error(f"[PEGGED] Error cancelling spot order: {e}")

            # Place market order for spot
            remaining_qty = spread_order.spot_leg.quantity - spread_order.spot_leg.filled_qty
            if remaining_qty > 0:
                market_leg = LegOrder(
                    symbol=spot_broker.symbol,
                    side=spread_order.spot_leg.side,
                    quantity=remaining_qty,
                )
                result = self._place_market_order_mt5(
                    mt5, spot_broker.symbol, market_leg,
                    spread_order.spot_leg.position_ticket
                )
                self._update_leg_from_result(spread_order.spot_leg, result)
                self._logger.info(f"[PEGGED] Spot leg filled with MARKET: {result}")

    def _update_leg_from_result(self, leg: LegOrder, result: Dict[str, Any]) -> None:
        """Update leg status from order result."""
        if result.get('success'):
            leg.order_ticket = result.get('order_id', 0)
            leg.filled_qty = result.get('filled_qty', 0)
            leg.filled_price = result.get('filled_price', 0)
            leg.status = LegStatus.FILLED if leg.filled_qty >= leg.quantity else LegStatus.PARTIAL
        else:
            leg.status = LegStatus.FAILED

        leg.last_update = datetime.now()

    def cancel_active_order(self) -> None:
        """Cancel any active order."""
        if self.active_order:
            import MetaTrader5 as mt5
            if mt5.initialize():
                try:
                    self._handle_timeout(self.active_order, None, None, mt5)
                finally:
                    mt5.shutdown()
