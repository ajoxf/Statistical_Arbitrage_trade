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
    order_ticket: int = 0  # MT5 order ticket
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

        self._logger.info(f"[PEGGED] Executing spread with PEGGED LIMIT orders: {spread_order.position_type} "
                         f"{'ENTRY' if spread_order.is_entry else 'EXIT'}")

        if not mt5.initialize():
            self._logger.error("[PEGGED] MT5 initialization failed")
            spread_order.spot_leg.status = LegStatus.FAILED
            spread_order.futures_leg.status = LegStatus.FAILED
            return spread_order

        try:
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

        For BUY: place slightly above best bid to be near top of book (maker)
        For SELL: place slightly below best ask to be near top of book (maker)
        """
        offset_bps = getattr(self.config, 'limit_order_price_offset_bps', 1.0) / 10000

        if spread_order.spot_leg.side == "BUY":
            # Want to buy: place at bid + small offset (maker, won't cross)
            spread_order.spot_leg.target_price = spot_tick['bid'] * (1 + offset_bps)
        else:
            # Want to sell: place at ask - small offset (maker, won't cross)
            spread_order.spot_leg.target_price = spot_tick['ask'] * (1 - offset_bps)

        if spread_order.futures_leg.side == "BUY":
            spread_order.futures_leg.target_price = futures_tick['bid'] * (1 + offset_bps)
        else:
            spread_order.futures_leg.target_price = futures_tick['ask'] * (1 - offset_bps)

        self._logger.debug(f"[PEGGED] Target prices: spot={spread_order.spot_leg.target_price:.2f}, "
                          f"futures={spread_order.futures_leg.target_price:.2f}")

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
        """Place initial limit orders for both legs."""
        # Place spot limit order
        spot_result = self._place_limit_order_mt5(
            mt5, spot_broker.symbol, spread_order.spot_leg,
            spread_order.spot_leg.position_ticket
        )

        if spot_result['success']:
            spread_order.spot_leg.order_ticket = spot_result['order_id']
            spread_order.spot_leg.status = LegStatus.OPEN
            self._logger.info(f"[PEGGED] Spot limit order placed: ticket={spot_result['order_id']}, "
                             f"price={spread_order.spot_leg.target_price:.2f}")
        else:
            spread_order.spot_leg.status = LegStatus.FAILED
            self._logger.error(f"[PEGGED] Failed to place spot limit order: {spot_result['error']}")

        # Place futures limit order
        futures_result = self._place_limit_order_mt5(
            mt5, futures_broker.symbol, spread_order.futures_leg,
            spread_order.futures_leg.position_ticket
        )

        if futures_result['success']:
            spread_order.futures_leg.order_ticket = futures_result['order_id']
            spread_order.futures_leg.status = LegStatus.OPEN
            self._logger.info(f"[PEGGED] Futures limit order placed: ticket={futures_result['order_id']}, "
                             f"price={spread_order.futures_leg.target_price:.2f}")
        else:
            spread_order.futures_leg.status = LegStatus.FAILED
            self._logger.error(f"[PEGGED] Failed to place futures limit order: {futures_result['error']}")

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
            return {'success': False, 'error': 'Could not get symbol info'}

        tick_size = symbol_info.trade_tick_size if symbol_info.trade_tick_size > 0 else 0.01
        price = round(leg.target_price / tick_size) * tick_size

        # Get filling mode
        filling_mode = mt5.ORDER_FILLING_RETURN
        if symbol_info:
            fm = symbol_info.filling_mode
            if fm == 1:
                filling_mode = mt5.ORDER_FILLING_FOK
            elif fm == 2:
                filling_mode = mt5.ORDER_FILLING_IOC

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
            return {'success': False, 'error': 'order_send returned None'}

        if result.retcode == mt5.TRADE_RETCODE_DONE:
            return {'success': True, 'order_id': result.order}
        else:
            return {'success': False, 'error': f'{result.retcode} - {result.comment}'}

    def _check_order_status(self, spread_order: SpreadOrder, mt5) -> None:
        """Check the fill status of both legs."""
        # Check spot leg
        if spread_order.spot_leg.status == LegStatus.OPEN:
            orders = mt5.orders_get(ticket=spread_order.spot_leg.order_ticket)
            if orders is None or len(orders) == 0:
                # Order no longer pending - check if filled
                deals = mt5.history_deals_get(order=spread_order.spot_leg.order_ticket)
                if deals and len(deals) > 0:
                    total_filled = sum(d.volume for d in deals)
                    avg_price = sum(d.price * d.volume for d in deals) / total_filled if total_filled > 0 else 0
                    spread_order.spot_leg.filled_qty = total_filled
                    spread_order.spot_leg.filled_price = avg_price
                    spread_order.spot_leg.status = LegStatus.FILLED if total_filled >= spread_order.spot_leg.quantity else LegStatus.PARTIAL
                    self._logger.info(f"[PEGGED] Spot leg filled: qty={total_filled}, price={avg_price:.2f}")

        # Check futures leg
        if spread_order.futures_leg.status == LegStatus.OPEN:
            orders = mt5.orders_get(ticket=spread_order.futures_leg.order_ticket)
            if orders is None or len(orders) == 0:
                deals = mt5.history_deals_get(order=spread_order.futures_leg.order_ticket)
                if deals and len(deals) > 0:
                    total_filled = sum(d.volume for d in deals)
                    avg_price = sum(d.price * d.volume for d in deals) / total_filled if total_filled > 0 else 0
                    spread_order.futures_leg.filled_qty = total_filled
                    spread_order.futures_leg.filled_price = avg_price
                    spread_order.futures_leg.status = LegStatus.FILLED if total_filled >= spread_order.futures_leg.quantity else LegStatus.PARTIAL
                    self._logger.info(f"[PEGGED] Futures leg filled: qty={total_filled}, price={avg_price:.2f}")

    def _amend_limit_orders(
        self,
        spread_order: SpreadOrder,
        spot_broker,
        futures_broker,
        mt5,
    ) -> None:
        """Amend (cancel and replace) existing limit orders with new prices."""
        # Amend spot order if still open
        if spread_order.spot_leg.status == LegStatus.OPEN:
            try:
                # Cancel existing order
                cancel_request = {
                    "action": mt5.TRADE_ACTION_REMOVE,
                    "order": spread_order.spot_leg.order_ticket,
                }
                mt5.order_send(cancel_request)

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
                        self._logger.debug(f"[PEGGED] Spot order re-pegged: {spread_order.spot_leg.target_price:.2f}")
            except Exception as e:
                self._logger.error(f"[PEGGED] Failed to amend spot order: {e}")

        # Amend futures order if still open
        if spread_order.futures_leg.status == LegStatus.OPEN:
            try:
                cancel_request = {
                    "action": mt5.TRADE_ACTION_REMOVE,
                    "order": spread_order.futures_leg.order_ticket,
                }
                mt5.order_send(cancel_request)

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
                        self._logger.debug(f"[PEGGED] Futures order re-pegged: {spread_order.futures_leg.target_price:.2f}")
            except Exception as e:
                self._logger.error(f"[PEGGED] Failed to amend futures order: {e}")

    def _handle_timeout(
        self,
        spread_order: SpreadOrder,
        spot_broker,
        futures_broker,
        mt5,
    ) -> None:
        """Handle timeout - cancel unfilled orders and close any partial fills."""
        self._logger.info("[PEGGED] Handling limit order timeout")

        # Cancel spot order if still open
        if spread_order.spot_leg.status == LegStatus.OPEN:
            try:
                cancel_request = {
                    "action": mt5.TRADE_ACTION_REMOVE,
                    "order": spread_order.spot_leg.order_ticket,
                }
                result = mt5.order_send(cancel_request)
                spread_order.spot_leg.status = LegStatus.CANCELLED
                self._logger.info(f"[PEGGED] Spot limit order cancelled")
            except Exception as e:
                self._logger.error(f"[PEGGED] Failed to cancel spot order: {e}")

        # Cancel futures order if still open
        if spread_order.futures_leg.status == LegStatus.OPEN:
            try:
                cancel_request = {
                    "action": mt5.TRADE_ACTION_REMOVE,
                    "order": spread_order.futures_leg.order_ticket,
                }
                result = mt5.order_send(cancel_request)
                spread_order.futures_leg.status = LegStatus.CANCELLED
                self._logger.info(f"[PEGGED] Futures limit order cancelled")
            except Exception as e:
                self._logger.error(f"[PEGGED] Failed to cancel futures order: {e}")

        # Handle partial fills
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
