"""
OKX Exchange Adapter

Implements the BrokerAdapter interface for OKX exchange.
Supports both Spot and Swap (perpetual futures) trading for basis arbitrage.

OKX API Documentation: https://www.okx.com/docs-v5/en/

Features:
- REST API for market data and order execution
- WebSocket for real-time tick streaming
- HMAC-SHA256 request signing
- Support for demo/simulated trading
"""

import asyncio
import base64
import hashlib
import hmac
import json
import logging
import os
import time
from datetime import datetime, timezone
from typing import Optional, Dict, Any, List

try:
    import aiohttp
    HAS_AIOHTTP = True
except ImportError:
    HAS_AIOHTTP = False

from .base import (
    BrokerAdapter, BrokerConfig, BrokerStatus,
    Tick, OrderResult, Position, AccountInfo, SymbolInfo,
    OrderType, OrderSide, PositionType
)

logger = logging.getLogger(__name__)


class OKXAdapter(BrokerAdapter):
    """
    OKX Exchange adapter for spot and perpetual futures trading.

    Supports basis trading by connecting to the same OKX account
    for both spot and swap (perpetual futures) prices.

    Symbol formats:
        Spot: BTC-USDT, ETH-USDT
        Swap: BTC-USDT-SWAP, ETH-USDT-SWAP
    """

    # OKX API endpoints
    REST_URL = "https://www.okx.com"
    REST_URL_DEMO = "https://www.okx.com"  # Same URL, different header
    WS_PUBLIC = "wss://ws.okx.com:8443/ws/v5/public"
    WS_PRIVATE = "wss://ws.okx.com:8443/ws/v5/private"
    WS_PUBLIC_DEMO = "wss://wspap.okx.com:8443/ws/v5/public?brokerId=9999"
    WS_PRIVATE_DEMO = "wss://wspap.okx.com:8443/ws/v5/private?brokerId=9999"

    def __init__(self, config: BrokerConfig):
        """
        Initialize OKX adapter.

        Args:
            config: BrokerConfig with OKX credentials
                - okx_api_key: API key (or set OKX_API_KEY env var)
                - okx_api_secret: API secret (or set OKX_API_SECRET env var)
                - okx_passphrase: API passphrase (or set OKX_PASSPHRASE env var)
                - okx_simulated: Use demo trading (default True, or set OKX_SIMULATED env var)
                - okx_account_type: 'spot' or 'swap'
        """
        super().__init__(config)

        if not HAS_AIOHTTP:
            raise ImportError("aiohttp is required for OKX adapter. Install with: pip install aiohttp")

        # Load credentials from config or environment variables
        self._api_key = config.okx_api_key or os.environ.get('OKX_API_KEY', '')
        self._api_secret = config.okx_api_secret or os.environ.get('OKX_API_SECRET', '')
        self._passphrase = config.okx_passphrase or os.environ.get('OKX_PASSPHRASE', '')

        # Check for simulated mode from env var (string 'true'/'false')
        env_simulated = os.environ.get('OKX_SIMULATED', '').lower()
        if env_simulated in ('true', '1', 'yes'):
            self._simulated = True
        elif env_simulated in ('false', '0', 'no'):
            self._simulated = False
        else:
            self._simulated = config.okx_simulated

        self._account_type = config.okx_account_type  # 'spot' or 'swap'

        self._session: Optional[aiohttp.ClientSession] = None
        self._ws: Optional[aiohttp.ClientWebSocketResponse] = None
        self._ws_task: Optional[asyncio.Task] = None

        # Cache for latest ticks
        self._tick_cache: Dict[str, Tick] = {}
        self._subscribed_symbols: List[str] = []

        # Mock mode for testing
        self._mock_mode = not self._api_key
        if self._mock_mode:
            logger.warning(f"[{self.name}] No API key provided, running in MOCK mode")

    def _get_timestamp(self) -> str:
        """Get ISO format timestamp for API requests."""
        return datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.%f')[:-3] + 'Z'

    def _sign(self, timestamp: str, method: str, path: str, body: str = "") -> str:
        """
        Create HMAC-SHA256 signature for authenticated requests.

        Args:
            timestamp: ISO format timestamp
            method: HTTP method (GET, POST, etc.)
            path: API endpoint path
            body: Request body (for POST requests)

        Returns:
            Base64 encoded signature
        """
        message = timestamp + method.upper() + path + body
        mac = hmac.new(
            self._api_secret.encode('utf-8'),
            message.encode('utf-8'),
            hashlib.sha256
        )
        return base64.b64encode(mac.digest()).decode('utf-8')

    def _get_headers(self, method: str, path: str, body: str = "") -> Dict[str, str]:
        """
        Get headers for authenticated API requests.

        Args:
            method: HTTP method
            path: API endpoint path
            body: Request body

        Returns:
            Headers dict with authentication
        """
        timestamp = self._get_timestamp()
        signature = self._sign(timestamp, method, path, body)

        headers = {
            'OK-ACCESS-KEY': self._api_key,
            'OK-ACCESS-SIGN': signature,
            'OK-ACCESS-TIMESTAMP': timestamp,
            'OK-ACCESS-PASSPHRASE': self._passphrase,
            'Content-Type': 'application/json'
        }

        # Add demo trading flag if simulated
        if self._simulated:
            headers['x-simulated-trading'] = '1'

        return headers

    async def _request(
        self,
        method: str,
        path: str,
        params: Optional[Dict] = None,
        body: Optional[Dict] = None,
        authenticated: bool = False
    ) -> Dict[str, Any]:
        """
        Make HTTP request to OKX API.

        Args:
            method: HTTP method
            path: API endpoint path
            params: Query parameters
            body: Request body
            authenticated: Whether to add auth headers

        Returns:
            API response as dict
        """
        if self._mock_mode:
            return self._mock_response(method, path, params, body)

        if self._session is None:
            raise RuntimeError("Session not initialized. Call connect() first.")

        url = self.REST_URL + path
        body_str = json.dumps(body) if body else ""

        # Build query string
        if params:
            query = "&".join(f"{k}={v}" for k, v in params.items())
            full_path = f"{path}?{query}"
            url = f"{url}?{query}"
        else:
            full_path = path

        headers = {}
        if authenticated:
            headers = self._get_headers(method, full_path, body_str)

        try:
            async with self._session.request(
                method,
                url,
                headers=headers,
                data=body_str if body else None
            ) as response:
                data = await response.json()

                if data.get('code') != '0':
                    logger.error(f"[{self.name}] API error: {data}")

                return data

        except Exception as e:
            logger.error(f"[{self.name}] Request error: {e}")
            return {'code': '-1', 'msg': str(e), 'data': []}

    def _mock_response(
        self,
        method: str,
        path: str,
        params: Optional[Dict],
        body: Optional[Dict]
    ) -> Dict[str, Any]:
        """Generate mock responses for testing."""
        import random

        if '/market/ticker' in path:
            # Mock ticker data
            base_price = 50000 if 'BTC' in str(params) else 3000
            return {
                'code': '0',
                'data': [{
                    'instId': params.get('instId', 'BTC-USDT'),
                    'last': str(base_price + random.uniform(-100, 100)),
                    'bidPx': str(base_price - random.uniform(1, 5)),
                    'askPx': str(base_price + random.uniform(1, 5)),
                    'ts': str(int(time.time() * 1000))
                }]
            }
        elif '/account/balance' in path:
            return {
                'code': '0',
                'data': [{
                    'totalEq': '100000',
                    'details': [
                        {'ccy': 'USDT', 'availBal': '50000', 'frozenBal': '0'}
                    ]
                }]
            }
        elif '/trade/order' in path:
            return {
                'code': '0',
                'data': [{
                    'ordId': str(int(time.time() * 1000)),
                    'clOrdId': body.get('clOrdId', ''),
                    'sCode': '0',
                    'sMsg': ''
                }]
            }

        return {'code': '0', 'data': []}

    # ==================== Connection Management ====================

    async def connect(self) -> bool:
        """
        Establish connection to OKX.

        Creates HTTP session and optionally connects WebSocket.

        Returns:
            True if connection successful
        """
        try:
            self._status = BrokerStatus.CONNECTING
            logger.info(f"[{self.name}] Connecting to OKX...")

            # Create HTTP session
            timeout = aiohttp.ClientTimeout(total=self.config.timeout_seconds)
            self._session = aiohttp.ClientSession(timeout=timeout)

            if not self._mock_mode:
                # Test connection by fetching account info
                result = await self._request(
                    'GET',
                    '/api/v5/account/balance',
                    authenticated=True
                )

                if result.get('code') != '0':
                    logger.error(f"[{self.name}] Connection failed: {result.get('msg')}")
                    self._status = BrokerStatus.ERROR
                    return False

            self._status = BrokerStatus.CONNECTED
            self._last_heartbeat = datetime.now()
            logger.info(f"[{self.name}] Connected to OKX successfully")

            # Subscribe to symbol if configured
            if self.config.symbol:
                await self.subscribe_tick(self.config.symbol)

            return True

        except Exception as e:
            logger.error(f"[{self.name}] Connection error: {e}")
            self._status = BrokerStatus.ERROR
            return False

    async def disconnect(self) -> None:
        """Close connection to OKX."""
        logger.info(f"[{self.name}] Disconnecting from OKX...")

        # Close WebSocket
        if self._ws_task:
            self._ws_task.cancel()
            try:
                await self._ws_task
            except asyncio.CancelledError:
                pass

        if self._ws:
            await self._ws.close()
            self._ws = None

        # Close HTTP session
        if self._session:
            await self._session.close()
            self._session = None

        self._status = BrokerStatus.DISCONNECTED
        logger.info(f"[{self.name}] Disconnected from OKX")

    async def heartbeat(self) -> bool:
        """
        Send heartbeat to verify connection.

        Returns:
            True if connection is alive
        """
        if self._mock_mode:
            self._last_heartbeat = datetime.now()
            return True

        try:
            start = time.time()
            result = await self._request('GET', '/api/v5/public/time')

            if result.get('code') == '0':
                self._latency_ms = (time.time() - start) * 1000
                self._last_heartbeat = datetime.now()
                return True

            return False

        except Exception as e:
            logger.error(f"[{self.name}] Heartbeat error: {e}")
            return False

    # ==================== Market Data ====================

    async def get_tick(self, symbol: str) -> Optional[Tick]:
        """
        Get current bid/ask prices for a symbol.

        Args:
            symbol: OKX instrument ID (e.g., 'BTC-USDT' or 'BTC-USDT-SWAP')

        Returns:
            Tick object with current prices
        """
        # Check cache first
        if symbol in self._tick_cache:
            cached = self._tick_cache[symbol]
            # Return cached if less than 1 second old
            if (datetime.now() - cached.timestamp).total_seconds() < 1:
                return cached

        try:
            result = await self._request(
                'GET',
                '/api/v5/market/ticker',
                params={'instId': symbol}
            )

            if result.get('code') == '0' and result.get('data'):
                data = result['data'][0]
                tick = Tick(
                    symbol=symbol,
                    bid=float(data['bidPx']),
                    ask=float(data['askPx']),
                    last=float(data['last']),
                    timestamp=datetime.now(),
                    volume=float(data.get('vol24h', 0))
                )
                self._tick_cache[symbol] = tick
                return tick

            return None

        except Exception as e:
            logger.error(f"[{self.name}] Error getting tick for {symbol}: {e}")
            return None

    async def get_symbol_info(self, symbol: str) -> Optional[SymbolInfo]:
        """
        Get trading symbol information.

        Args:
            symbol: OKX instrument ID

        Returns:
            SymbolInfo with trading parameters
        """
        try:
            # Determine instrument type from symbol
            inst_type = 'SWAP' if symbol.endswith('-SWAP') else 'SPOT'

            result = await self._request(
                'GET',
                '/api/v5/public/instruments',
                params={'instType': inst_type, 'instId': symbol}
            )

            if result.get('code') == '0' and result.get('data'):
                data = result['data'][0]

                # Parse currencies from symbol
                parts = symbol.replace('-SWAP', '').split('-')
                base_ccy = parts[0] if len(parts) > 0 else ''
                quote_ccy = parts[1] if len(parts) > 1 else 'USDT'

                return SymbolInfo(
                    symbol=symbol,
                    description=f"OKX {symbol}",
                    base_currency=base_ccy,
                    quote_currency=quote_ccy,
                    contract_size=float(data.get('ctVal', 1)),
                    min_volume=float(data.get('minSz', 0.001)),
                    max_volume=float(data.get('maxSz', 10000)),
                    volume_step=float(data.get('lotSz', 0.001)),
                    point=float(data.get('tickSz', 0.01)),
                    digits=len(data.get('tickSz', '0.01').split('.')[-1]),
                    spread=0,  # Not provided by OKX
                    trade_mode=data.get('state', 'live'),
                    trade_allowed=data.get('state') == 'live'
                )

            return None

        except Exception as e:
            logger.error(f"[{self.name}] Error getting symbol info for {symbol}: {e}")
            return None

    async def subscribe_tick(self, symbol: str) -> bool:
        """
        Subscribe to real-time tick updates via WebSocket.

        Args:
            symbol: OKX instrument ID

        Returns:
            True if subscription successful
        """
        # For now, just add to subscribed list
        # Full WebSocket implementation can be added later
        if symbol not in self._subscribed_symbols:
            self._subscribed_symbols.append(symbol)
            logger.info(f"[{self.name}] Subscribed to {symbol}")
        return True

    # ==================== Order Execution ====================

    async def place_market_order(
        self,
        symbol: str,
        side: OrderSide,
        volume: float,
        **kwargs
    ) -> OrderResult:
        """
        Place a market order.

        Args:
            symbol: OKX instrument ID
            side: BUY or SELL
            volume: Order size

        Returns:
            OrderResult with execution details
        """
        start_time = time.time()

        try:
            # Determine trade mode based on symbol
            td_mode = 'cross' if symbol.endswith('-SWAP') else 'cash'

            order_body = {
                'instId': symbol,
                'tdMode': td_mode,
                'side': 'buy' if side == OrderSide.BUY else 'sell',
                'ordType': 'market',
                'sz': str(volume),
                'clOrdId': f"arb_{int(time.time() * 1000)}"
            }

            result = await self._request(
                'POST',
                '/api/v5/trade/order',
                body=order_body,
                authenticated=True
            )

            exec_time = (time.time() - start_time) * 1000

            if result.get('code') == '0' and result.get('data'):
                data = result['data'][0]

                if data.get('sCode') == '0':
                    return OrderResult(
                        success=True,
                        order_id=data.get('ordId'),
                        ticket=int(data.get('ordId', 0)),
                        volume=volume,
                        execution_time_ms=exec_time
                    )
                else:
                    return OrderResult(
                        success=False,
                        error=data.get('sMsg', 'Order failed'),
                        execution_time_ms=exec_time
                    )

            return OrderResult(
                success=False,
                error=result.get('msg', 'Unknown error'),
                execution_time_ms=exec_time
            )

        except Exception as e:
            return OrderResult(
                success=False,
                error=str(e),
                execution_time_ms=(time.time() - start_time) * 1000
            )

    async def place_limit_order(
        self,
        symbol: str,
        side: OrderSide,
        volume: float,
        price: float,
        **kwargs
    ) -> OrderResult:
        """
        Place a limit order.

        Args:
            symbol: OKX instrument ID
            side: BUY or SELL
            volume: Order size
            price: Limit price

        Returns:
            OrderResult with execution details
        """
        start_time = time.time()

        try:
            td_mode = 'cross' if symbol.endswith('-SWAP') else 'cash'

            order_body = {
                'instId': symbol,
                'tdMode': td_mode,
                'side': 'buy' if side == OrderSide.BUY else 'sell',
                'ordType': 'limit',
                'sz': str(volume),
                'px': str(price),
                'clOrdId': f"arb_{int(time.time() * 1000)}"
            }

            result = await self._request(
                'POST',
                '/api/v5/trade/order',
                body=order_body,
                authenticated=True
            )

            exec_time = (time.time() - start_time) * 1000

            if result.get('code') == '0' and result.get('data'):
                data = result['data'][0]

                if data.get('sCode') == '0':
                    return OrderResult(
                        success=True,
                        order_id=data.get('ordId'),
                        ticket=int(data.get('ordId', 0)),
                        price=price,
                        volume=volume,
                        execution_time_ms=exec_time
                    )
                else:
                    return OrderResult(
                        success=False,
                        error=data.get('sMsg', 'Order failed'),
                        execution_time_ms=exec_time
                    )

            return OrderResult(
                success=False,
                error=result.get('msg', 'Unknown error'),
                execution_time_ms=exec_time
            )

        except Exception as e:
            return OrderResult(
                success=False,
                error=str(e),
                execution_time_ms=(time.time() - start_time) * 1000
            )

    async def execute_pegged_limit_order(
        self,
        symbol: str,
        side: OrderSide,
        volume: float,
        offset_ticks: int = 1,
        max_attempts: int = 10,
        update_interval: float = 0.5,
        **kwargs
    ) -> OrderResult:
        """
        Execute a pegged limit order that tracks the market.

        Places limit orders that follow the bid/ask, updating periodically.

        Args:
            symbol: OKX instrument ID
            side: BUY or SELL
            volume: Order size
            offset_ticks: Ticks away from best bid/ask
            max_attempts: Maximum order updates before giving up
            update_interval: Seconds between price updates

        Returns:
            OrderResult with final execution details
        """
        start_time = time.time()
        current_order_id = None
        filled_volume = 0.0
        last_price = 0.0

        try:
            for attempt in range(max_attempts):
                # Get current tick
                tick = await self.get_tick(symbol)
                if tick is None:
                    await asyncio.sleep(update_interval)
                    continue

                # Calculate pegged price
                tick_size = 0.01  # Default, should get from symbol info
                if side == OrderSide.BUY:
                    price = tick.ask - (offset_ticks * tick_size)
                else:
                    price = tick.bid + (offset_ticks * tick_size)

                # Cancel existing order if price changed significantly
                if current_order_id and abs(price - last_price) > tick_size:
                    await self.cancel_order(symbol, current_order_id)
                    current_order_id = None

                # Place new order if needed
                if current_order_id is None:
                    remaining = volume - filled_volume
                    if remaining <= 0:
                        break

                    result = await self.place_limit_order(
                        symbol, side, remaining, price
                    )

                    if result.success:
                        current_order_id = result.order_id
                        last_price = price

                # Check order status
                if current_order_id:
                    order_info = await self.get_order_status(symbol, current_order_id)
                    if order_info:
                        filled_volume = float(order_info.get('fillSz', 0))
                        if order_info.get('state') == 'filled':
                            return OrderResult(
                                success=True,
                                order_id=current_order_id,
                                price=float(order_info.get('avgPx', price)),
                                volume=volume,
                                filled_volume=filled_volume,
                                execution_time_ms=(time.time() - start_time) * 1000
                            )

                await asyncio.sleep(update_interval)

            # Cancel any remaining order
            if current_order_id:
                await self.cancel_order(symbol, current_order_id)

            # Return partial fill result
            return OrderResult(
                success=filled_volume > 0,
                order_id=current_order_id,
                price=last_price,
                volume=volume,
                filled_volume=filled_volume,
                error="Max attempts reached" if filled_volume < volume else None,
                execution_time_ms=(time.time() - start_time) * 1000
            )

        except Exception as e:
            return OrderResult(
                success=False,
                error=str(e),
                execution_time_ms=(time.time() - start_time) * 1000
            )

    async def cancel_order(self, symbol: str, order_id: str) -> bool:
        """
        Cancel an open order.

        Args:
            symbol: OKX instrument ID
            order_id: Order ID to cancel

        Returns:
            True if cancellation successful
        """
        try:
            result = await self._request(
                'POST',
                '/api/v5/trade/cancel-order',
                body={'instId': symbol, 'ordId': order_id},
                authenticated=True
            )

            return result.get('code') == '0'

        except Exception as e:
            logger.error(f"[{self.name}] Error cancelling order: {e}")
            return False

    async def modify_order(
        self,
        order_id: str,
        price: Optional[float] = None,
        volume: Optional[float] = None
    ) -> bool:
        """
        Modify an existing order.

        Args:
            order_id: Order ID to modify
            price: New price (optional)
            volume: New volume (optional)

        Returns:
            True if modification successful
        """
        try:
            # OKX amend-order requires instId - for now use stored symbol
            body = {
                'instId': self._symbol,
                'ordId': order_id
            }

            if price is not None:
                body['newPx'] = str(price)
            if volume is not None:
                body['newSz'] = str(volume)

            result = await self._request(
                'POST',
                '/api/v5/trade/amend-order',
                body=body,
                authenticated=True
            )

            return result.get('code') == '0'

        except Exception as e:
            logger.error(f"[{self.name}] Error modifying order: {e}")
            return False

    async def get_order_status(self, symbol: str, order_id: str) -> Optional[Dict]:
        """
        Get order status.

        Args:
            symbol: OKX instrument ID
            order_id: Order ID

        Returns:
            Order info dict or None
        """
        try:
            result = await self._request(
                'GET',
                '/api/v5/trade/order',
                params={'instId': symbol, 'ordId': order_id},
                authenticated=True
            )

            if result.get('code') == '0' and result.get('data'):
                return result['data'][0]

            return None

        except Exception as e:
            logger.error(f"[{self.name}] Error getting order status: {e}")
            return None

    # ==================== Position Management ====================

    async def close_position(
        self,
        symbol: str,
        ticket: Optional[int] = None,
        volume: Optional[float] = None
    ) -> OrderResult:
        """
        Close an open position.

        Args:
            symbol: OKX instrument ID
            ticket: Position ticket (not used for OKX)
            volume: Volume to close (None = close all)

        Returns:
            OrderResult with close execution details
        """
        try:
            # Get current position
            positions = await self.get_positions(symbol)
            if not positions:
                return OrderResult(success=False, error="No position to close")

            pos = positions[0]
            close_volume = volume or pos.volume

            # Reverse the position
            close_side = OrderSide.SELL if pos.position_type == PositionType.LONG else OrderSide.BUY

            return await self.place_market_order(symbol, close_side, close_volume)

        except Exception as e:
            return OrderResult(success=False, error=str(e))

    async def get_positions(self, symbol: Optional[str] = None) -> List[Position]:
        """
        Get open positions.

        Args:
            symbol: Filter by symbol (optional)

        Returns:
            List of Position objects
        """
        try:
            params = {}
            if symbol:
                params['instId'] = symbol

            result = await self._request(
                'GET',
                '/api/v5/account/positions',
                params=params if params else None,
                authenticated=True
            )

            positions = []
            if result.get('code') == '0' and result.get('data'):
                for data in result['data']:
                    pos_amt = float(data.get('pos', 0))
                    if pos_amt == 0:
                        continue

                    positions.append(Position(
                        ticket=int(data.get('posId', 0)),
                        symbol=data.get('instId', ''),
                        volume=abs(pos_amt),
                        entry_price=float(data.get('avgPx', 0)),
                        current_price=float(data.get('markPx', 0)),
                        profit=float(data.get('upl', 0)),
                        position_type=PositionType.LONG if pos_amt > 0 else PositionType.SHORT,
                        open_time=datetime.fromtimestamp(int(data.get('cTime', 0)) / 1000),
                        broker_id=self.broker_id
                    ))

            return positions

        except Exception as e:
            logger.error(f"[{self.name}] Error getting positions: {e}")
            return []

    async def get_position_by_ticket(self, ticket: int) -> Optional[Position]:
        """
        Get position by ticket/ID.

        Args:
            ticket: Position ID

        Returns:
            Position if found, None otherwise
        """
        positions = await self.get_positions()
        for pos in positions:
            if pos.ticket == ticket:
                return pos
        return None

    # ==================== Account Information ====================

    async def get_account_info(self) -> Optional[AccountInfo]:
        """
        Get account balance and margin information.

        Returns:
            AccountInfo with account details
        """
        try:
            result = await self._request(
                'GET',
                '/api/v5/account/balance',
                authenticated=True
            )

            if result.get('code') == '0' and result.get('data'):
                data = result['data'][0]

                # Find USDT balance
                usdt_bal = 0.0
                for detail in data.get('details', []):
                    if detail.get('ccy') == 'USDT':
                        usdt_bal = float(detail.get('availBal', 0))
                        break

                total_eq = float(data.get('totalEq', 0))

                return AccountInfo(
                    balance=total_eq,
                    equity=total_eq,
                    margin=float(data.get('imr', 0)),  # Initial margin
                    free_margin=usdt_bal,
                    margin_level=float(data.get('mgnRatio', 0)) * 100 if data.get('mgnRatio') else None,
                    profit=float(data.get('upl', 0)),
                    currency='USDT',
                    leverage=1,  # Varies by position
                    name=f"OKX {'Demo' if self._simulated else 'Live'}",
                    server='OKX',
                    company='OKX'
                )

            return None

        except Exception as e:
            logger.error(f"[{self.name}] Error getting account info: {e}")
            return None
