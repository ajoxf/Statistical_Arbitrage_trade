"""
Broker Worker Process

Runs as a separate process for each broker connection.
Handles:
- Broker adapter lifecycle
- Tick streaming
- Order execution
- Command processing
- Heartbeat monitoring

Usage:
    python -m workers.broker_worker --broker-id spot_broker --config config.yaml
"""

import asyncio
import argparse
import logging
import signal
import sys
from datetime import datetime
from typing import Optional, Dict, Any
from pathlib import Path

# Add parent directory to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent))

from adapters.base import BrokerAdapter, BrokerConfig, BrokerStatus, OrderSide
from adapters.mt5_adapter import MT5Adapter
from adapters.fix_adapter import FIXAdapter
from adapters.okx_adapter import OKXAdapter
from core.ipc import IPCManager, IPCMessage, MessageType, CommandType
from database.manager import DatabaseManager

logger = logging.getLogger(__name__)


class BrokerWorker:
    """
    Broker worker process manager.

    Manages the lifecycle of a single broker connection and handles
    communication with the main trading process via IPC.
    """

    # Tick streaming interval in seconds
    TICK_INTERVAL = 0.1

    # Heartbeat interval in seconds
    HEARTBEAT_INTERVAL = 5.0

    def __init__(
        self,
        broker_id: str,
        config: BrokerConfig,
        db_path: str = "trading.db",
        use_redis: bool = True
    ):
        """
        Initialize broker worker.

        Args:
            broker_id: Unique broker identifier
            config: Broker configuration
            db_path: Path to database
            use_redis: Use Redis for IPC if available
        """
        self.broker_id = broker_id
        self.config = config
        self.db_path = db_path

        # Components
        self._adapter: Optional[BrokerAdapter] = None
        self._ipc: Optional[IPCManager] = None
        self._db: Optional[DatabaseManager] = None

        # State
        self._running = False
        self._subscribed_symbols: set = set()

        # Background tasks
        self._tick_task: Optional[asyncio.Task] = None
        self._heartbeat_task: Optional[asyncio.Task] = None
        self._command_task: Optional[asyncio.Task] = None

        # IPC settings
        self._use_redis = use_redis

    def _create_adapter(self) -> BrokerAdapter:
        """Create appropriate adapter based on backend type"""
        backend_type = self.config.backend_type.upper()

        if backend_type == "MT5":
            return MT5Adapter(self.config)
        elif backend_type == "FIX":
            return FIXAdapter(self.config)
        elif backend_type == "OKX":
            return OKXAdapter(self.config)
        elif backend_type == "FLEXTRADE":
            # TODO: Implement FlexTradeAdapter
            logger.warning("FlexTrade adapter not implemented, using MT5 mock")
            return MT5Adapter(self.config)
        elif backend_type == "IB":
            # TODO: Implement IBAdapter
            logger.warning("IB adapter not implemented, using MT5 mock")
            return MT5Adapter(self.config)
        else:
            raise ValueError(f"Unknown backend type: {backend_type}")

    async def initialize(self) -> bool:
        """Initialize worker components"""
        logger.info(f"Initializing broker worker: {self.broker_id}")

        try:
            # Initialize database
            self._db = DatabaseManager(self.db_path)
            self._db.initialize()

            # Initialize IPC
            self._ipc = IPCManager(use_redis=self._use_redis)
            await self._ipc.initialize()

            # Create adapter
            self._adapter = self._create_adapter()

            # Subscribe to commands
            await self._ipc.subscribe_commands(
                self.broker_id,
                self._handle_command
            )

            # Subscribe to orders
            await self._ipc.subscribe_orders(
                self.broker_id,
                self._handle_order_request
            )

            logger.info(f"Worker initialized: {self.broker_id}")
            return True

        except Exception as e:
            logger.error(f"Worker initialization failed: {e}")
            return False

    async def start(self) -> None:
        """Start worker and connect to broker"""
        self._running = True

        # Connect to broker
        if not await self._adapter.connect():
            logger.error(f"Failed to connect to broker: {self.broker_id}")
            await self._ipc.publish_status(self.broker_id, "ERROR")
            return

        # Update database status
        self._db.update_broker_status(self.broker_id, "CONNECTED")
        await self._ipc.publish_status(self.broker_id, "CONNECTED")

        # Handle unified mode - subscribe to both spot and futures symbols
        if self.config.unified_mode:
            if self.config.spot_symbol:
                self._subscribed_symbols.add(self.config.spot_symbol)
                logger.info(f"[UNIFIED] Subscribed to spot: {self.config.spot_symbol}")
            if self.config.futures_symbol:
                self._subscribed_symbols.add(self.config.futures_symbol)
                logger.info(f"[UNIFIED] Subscribed to futures: {self.config.futures_symbol}")
        elif self.config.symbol:
            # Standard mode - single symbol
            self._subscribed_symbols.add(self.config.symbol)

        # Start background tasks
        self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())
        self._tick_task = asyncio.create_task(self._tick_streaming_loop())

        logger.info(f"Worker started: {self.broker_id}")

        # Wait until stopped
        while self._running:
            await asyncio.sleep(1)

    async def stop(self) -> None:
        """Stop worker and disconnect"""
        logger.info(f"Stopping worker: {self.broker_id}")
        self._running = False

        # Cancel background tasks
        for task in [self._tick_task, self._heartbeat_task, self._command_task]:
            if task and not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

        # Disconnect from broker
        if self._adapter:
            await self._adapter.disconnect()

        # Update status
        if self._db:
            self._db.update_broker_status(self.broker_id, "DISCONNECTED")

        if self._ipc:
            await self._ipc.publish_status(self.broker_id, "DISCONNECTED")
            await self._ipc.shutdown()

        if self._db:
            self._db.close()

        logger.info(f"Worker stopped: {self.broker_id}")

    async def _heartbeat_loop(self) -> None:
        """Background heartbeat monitoring"""
        while self._running:
            try:
                start = datetime.now()
                alive = await self._adapter.heartbeat()
                latency_ms = (datetime.now() - start).total_seconds() * 1000

                if alive:
                    status = "CONNECTED"
                    self._db.update_broker_status(
                        self.broker_id,
                        status,
                        latency_ms=int(latency_ms)
                    )
                else:
                    status = "ERROR"
                    logger.warning(f"Heartbeat failed for {self.broker_id}")

                await self._ipc.publish_status(self.broker_id, status, latency_ms)

            except Exception as e:
                logger.error(f"Heartbeat error: {e}")
                await self._ipc.publish_status(self.broker_id, "ERROR")

            await asyncio.sleep(self.HEARTBEAT_INTERVAL)

    async def _tick_streaming_loop(self) -> None:
        """Stream tick data for subscribed symbols"""
        while self._running:
            try:
                for symbol in list(self._subscribed_symbols):
                    tick = await self._adapter.get_tick(symbol)
                    if tick:
                        await self._ipc.publish_tick(
                            self.broker_id,
                            symbol,
                            tick.bid,
                            tick.ask
                        )

            except Exception as e:
                logger.error(f"Tick streaming error: {e}")

            await asyncio.sleep(self.TICK_INTERVAL)

    def _handle_command(self, message: IPCMessage) -> None:
        """Handle incoming command messages"""
        asyncio.create_task(self._process_command(message))

    async def _process_command(self, message: IPCMessage) -> None:
        """Process command message"""
        try:
            payload = message.payload
            command = payload.get('command')

            logger.debug(f"Processing command: {command}")

            if command == CommandType.CONNECT.value:
                success = await self._adapter.connect()
                status = "CONNECTED" if success else "ERROR"
                await self._ipc.publish_status(self.broker_id, status)

            elif command == CommandType.DISCONNECT.value:
                await self._adapter.disconnect()
                await self._ipc.publish_status(self.broker_id, "DISCONNECTED")

            elif command == CommandType.SUBSCRIBE.value:
                symbol = payload.get('symbol')
                if symbol:
                    self._subscribed_symbols.add(symbol)
                    logger.info(f"Subscribed to {symbol}")

            elif command == CommandType.UNSUBSCRIBE.value:
                symbol = payload.get('symbol')
                if symbol:
                    self._subscribed_symbols.discard(symbol)

            elif command == CommandType.GET_POSITIONS.value:
                positions = await self._adapter.get_positions()
                response = IPCMessage(
                    msg_type=MessageType.POSITION_UPDATE.value,
                    broker_id=self.broker_id,
                    payload={
                        'positions': [
                            {
                                'ticket': p.ticket,
                                'symbol': p.symbol,
                                'volume': p.volume,
                                'entry_price': p.entry_price,
                                'current_price': p.current_price,
                                'profit': p.profit,
                                'type': p.position_type.value
                            }
                            for p in positions
                        ]
                    },
                    correlation_id=message.correlation_id
                )
                await self._ipc._backend.publish(
                    f"arb:commands:{self.broker_id}",
                    response
                )

            elif command == CommandType.GET_ACCOUNT.value:
                account = await self._adapter.get_account_info()
                if account:
                    response = IPCMessage(
                        msg_type=MessageType.ACCOUNT_UPDATE.value,
                        broker_id=self.broker_id,
                        payload={
                            'balance': account.balance,
                            'equity': account.equity,
                            'margin': account.margin,
                            'free_margin': account.free_margin,
                            'profit': account.profit
                        },
                        correlation_id=message.correlation_id
                    )
                    await self._ipc._backend.publish(
                        f"arb:commands:{self.broker_id}",
                        response
                    )

            elif command == CommandType.SHUTDOWN.value:
                self._running = False

        except Exception as e:
            logger.error(f"Command processing error: {e}")

    def _handle_order_request(self, message: IPCMessage) -> None:
        """Handle incoming order requests"""
        asyncio.create_task(self._process_order(message))

    async def _process_order(self, message: IPCMessage) -> None:
        """Process order request"""
        try:
            payload = message.payload
            action = payload.get('action')
            symbol = payload.get('symbol')
            side_str = payload.get('side')
            volume = payload.get('volume')
            order_type = payload.get('order_type', 'MARKET')
            price = payload.get('price')
            ticket = payload.get('ticket')

            side = OrderSide.BUY if side_str == 'BUY' else OrderSide.SELL

            logger.info(f"Processing order: {action} {side_str} {volume} {symbol}")

            result = None

            if action == 'OPEN':
                if order_type == 'MARKET':
                    result = await self._adapter.place_market_order(
                        symbol=symbol,
                        side=side,
                        volume=volume
                    )
                elif order_type == 'LIMIT':
                    result = await self._adapter.place_limit_order(
                        symbol=symbol,
                        side=side,
                        volume=volume,
                        price=price
                    )
                elif order_type == 'PEGGED_LIMIT':
                    result = await self._adapter.execute_pegged_limit_order(
                        symbol=symbol,
                        side=side,
                        volume=volume,
                        timeout_seconds=60,
                        peg_interval_seconds=1.5
                    )

            elif action == 'CLOSE':
                if ticket:
                    if order_type == 'MARKET':
                        result = await self._adapter.close_position(
                            ticket=ticket,
                            volume=volume
                        )
                    elif order_type == 'PEGGED_LIMIT':
                        result = await self._adapter.execute_pegged_limit_order(
                            symbol=symbol,
                            side=side,
                            volume=volume,
                            timeout_seconds=60,
                            peg_interval_seconds=1.5,
                            ticket=ticket
                        )

            elif action == 'CANCEL':
                order_id = payload.get('order_id')
                if order_id:
                    success = await self._adapter.cancel_order(order_id)
                    result = type('Result', (), {'success': success, 'ticket': None, 'price': None, 'error': None})()

            # Send result back
            if result:
                await self._ipc.publish_order_result(
                    broker_id=self.broker_id,
                    success=result.success,
                    ticket=result.ticket,
                    price=result.price,
                    error=result.error if hasattr(result, 'error') else None,
                    correlation_id=message.correlation_id
                )
            else:
                await self._ipc.publish_order_result(
                    broker_id=self.broker_id,
                    success=False,
                    error="Unknown action or missing parameters",
                    correlation_id=message.correlation_id
                )

        except Exception as e:
            logger.error(f"Order processing error: {e}")
            await self._ipc.publish_order_result(
                broker_id=self.broker_id,
                success=False,
                error=str(e),
                correlation_id=message.correlation_id
            )


async def run_worker(broker_id: str, config_path: str, db_path: str):
    """Run broker worker from configuration"""
    import yaml

    # Load configuration
    with open(config_path, 'r') as f:
        config_data = yaml.safe_load(f)

    # Find broker config
    brokers = config_data.get('brokers', [])
    broker_config = None
    for b in brokers:
        if b.get('broker_id') == broker_id:
            broker_config = b
            break

    if broker_config is None:
        logger.error(f"Broker {broker_id} not found in config")
        return

    # Create BrokerConfig
    config = BrokerConfig(
        broker_id=broker_config['broker_id'],
        name=broker_config.get('name', broker_id),
        role=broker_config.get('role', 'SPOT'),
        backend_type=broker_config.get('type', 'MT5'),
        # Unified mode - same broker for spot and futures
        unified_mode=broker_config.get('unified_mode', False),
        spot_symbol=broker_config.get('spot_symbol'),
        futures_symbol=broker_config.get('futures_symbol'),
        # MT5 specific
        mt5_path=broker_config.get('mt5_path'),
        mt5_account=broker_config.get('mt5_account'),
        mt5_server=broker_config.get('mt5_server'),
        mt5_password=broker_config.get('mt5_password'),
        # FIX specific
        fix_host=broker_config.get('fix_host'),
        fix_port=broker_config.get('fix_port'),
        fix_sender_comp=broker_config.get('fix_sender_comp'),
        fix_target_comp=broker_config.get('fix_target_comp'),
        # OKX specific
        okx_api_key=broker_config.get('okx_api_key'),
        okx_api_secret=broker_config.get('okx_api_secret'),
        okx_passphrase=broker_config.get('okx_passphrase'),
        okx_simulated=broker_config.get('okx_simulated', True),
        okx_account_type=broker_config.get('okx_account_type', 'spot'),
        # Common settings
        symbol=broker_config.get('symbol', ''),
        contract_size=broker_config.get('contract_size', 100.0),
        commission_per_lot=broker_config.get('commission_per_lot', 0.0)
    )

    # Create and run worker
    worker = BrokerWorker(broker_id, config, db_path)

    # Setup signal handlers
    loop = asyncio.get_event_loop()

    def signal_handler():
        asyncio.create_task(worker.stop())

    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, signal_handler)

    # Initialize and start
    if await worker.initialize():
        await worker.start()


def main():
    """Entry point for broker worker"""
    parser = argparse.ArgumentParser(description='Broker Worker Process')
    parser.add_argument('--broker-id', required=True, help='Broker identifier')
    parser.add_argument('--config', default='config/brokers.yaml', help='Config file path')
    parser.add_argument('--db', default='trading.db', help='Database path')
    parser.add_argument('--log-level', default='INFO', help='Log level')

    args = parser.parse_args()

    # Setup logging
    logging.basicConfig(
        level=getattr(logging, args.log_level.upper()),
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
    )

    # Run worker
    asyncio.run(run_worker(args.broker_id, args.config, args.db))


if __name__ == '__main__':
    main()
