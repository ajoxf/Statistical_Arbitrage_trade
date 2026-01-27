"""
Inter-Process Communication (IPC) Module

Provides communication between the main coordinator and broker worker processes.
Supports both Redis (for production) and in-process queues (for single broker mode).
"""

import asyncio
import json
import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Callable, Optional, Dict, Any, List
from multiprocessing import Queue, Process
import threading
import queue

logger = logging.getLogger(__name__)


class MessageType(Enum):
    """IPC message types"""
    TICK = "TICK"
    ORDER_REQUEST = "ORDER_REQUEST"
    ORDER_RESULT = "ORDER_RESULT"
    COMMAND = "COMMAND"
    STATUS = "STATUS"
    HEARTBEAT = "HEARTBEAT"
    POSITION_UPDATE = "POSITION_UPDATE"
    ACCOUNT_UPDATE = "ACCOUNT_UPDATE"
    ERROR = "ERROR"


class CommandType(Enum):
    """Broker command types"""
    CONNECT = "CONNECT"
    DISCONNECT = "DISCONNECT"
    SUBSCRIBE = "SUBSCRIBE"
    UNSUBSCRIBE = "UNSUBSCRIBE"
    GET_POSITIONS = "GET_POSITIONS"
    GET_ACCOUNT = "GET_ACCOUNT"
    SHUTDOWN = "SHUTDOWN"


@dataclass
class IPCMessage:
    """Message container for IPC communication"""
    msg_type: str
    broker_id: str
    payload: Dict[str, Any] = field(default_factory=dict)
    timestamp: datetime = field(default_factory=datetime.now)
    correlation_id: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            'msg_type': self.msg_type,
            'broker_id': self.broker_id,
            'payload': self.payload,
            'timestamp': self.timestamp.isoformat(),
            'correlation_id': self.correlation_id
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'IPCMessage':
        return cls(
            msg_type=data['msg_type'],
            broker_id=data['broker_id'],
            payload=data.get('payload', {}),
            timestamp=datetime.fromisoformat(data['timestamp']),
            correlation_id=data.get('correlation_id')
        )

    def to_json(self) -> str:
        return json.dumps(self.to_dict())

    @classmethod
    def from_json(cls, data: str) -> 'IPCMessage':
        return cls.from_dict(json.loads(data))


class IPCBackend(ABC):
    """Abstract base class for IPC backends"""

    @abstractmethod
    async def initialize(self) -> None:
        """Initialize the backend"""
        pass

    @abstractmethod
    async def shutdown(self) -> None:
        """Shutdown the backend"""
        pass

    @abstractmethod
    async def publish(self, channel: str, message: IPCMessage) -> None:
        """Publish message to channel"""
        pass

    @abstractmethod
    async def subscribe(self, channel: str, callback: Callable) -> None:
        """Subscribe to channel with callback"""
        pass


class InProcessBackend(IPCBackend):
    """
    In-process IPC backend using queues.

    Used when running single broker mode (no multiprocessing needed).
    Provides zero-overhead communication for same-broker spot+futures.
    """

    def __init__(self):
        self._channels: Dict[str, List[Callable]] = {}
        self._running = False
        self._message_queue: asyncio.Queue = None
        self._process_task: Optional[asyncio.Task] = None

    async def initialize(self) -> None:
        self._message_queue = asyncio.Queue()
        self._running = True
        self._process_task = asyncio.create_task(self._process_messages())
        logger.info("InProcess IPC backend initialized")

    async def shutdown(self) -> None:
        self._running = False
        if self._process_task:
            self._process_task.cancel()
            try:
                await self._process_task
            except asyncio.CancelledError:
                pass
        logger.info("InProcess IPC backend shut down")

    async def publish(self, channel: str, message: IPCMessage) -> None:
        if channel in self._channels:
            for callback in self._channels[channel]:
                try:
                    callback(message)
                except Exception as e:
                    logger.error(f"Callback error on {channel}: {e}")

    async def subscribe(self, channel: str, callback: Callable) -> None:
        if channel not in self._channels:
            self._channels[channel] = []
        self._channels[channel].append(callback)
        logger.debug(f"Subscribed to channel: {channel}")

    async def _process_messages(self) -> None:
        while self._running:
            await asyncio.sleep(0.01)  # Small delay to prevent busy loop


class MultiprocessBackend(IPCBackend):
    """
    Multiprocess IPC backend using multiprocessing queues.

    Used when running multiple broker processes for true parallel execution.
    """

    def __init__(self):
        self._channels: Dict[str, Queue] = {}
        self._subscribers: Dict[str, List[Callable]] = {}
        self._running = False
        self._listener_threads: List[threading.Thread] = []

    async def initialize(self) -> None:
        self._running = True
        logger.info("Multiprocess IPC backend initialized")

    async def shutdown(self) -> None:
        self._running = False
        for thread in self._listener_threads:
            thread.join(timeout=1.0)
        logger.info("Multiprocess IPC backend shut down")

    def get_queue(self, channel: str) -> Queue:
        """Get or create a queue for a channel"""
        if channel not in self._channels:
            self._channels[channel] = Queue()
        return self._channels[channel]

    async def publish(self, channel: str, message: IPCMessage) -> None:
        q = self.get_queue(channel)
        q.put(message.to_dict())

    async def subscribe(self, channel: str, callback: Callable) -> None:
        if channel not in self._subscribers:
            self._subscribers[channel] = []
            # Start listener thread for this channel
            thread = threading.Thread(
                target=self._listener_loop,
                args=(channel,),
                daemon=True
            )
            thread.start()
            self._listener_threads.append(thread)

        self._subscribers[channel].append(callback)
        logger.debug(f"Subscribed to channel: {channel}")

    def _listener_loop(self, channel: str) -> None:
        """Background thread to listen for messages on a channel"""
        q = self.get_queue(channel)
        while self._running:
            try:
                data = q.get(timeout=0.1)
                message = IPCMessage.from_dict(data)
                for callback in self._subscribers.get(channel, []):
                    try:
                        callback(message)
                    except Exception as e:
                        logger.error(f"Callback error on {channel}: {e}")
            except queue.Empty:
                continue
            except Exception as e:
                logger.error(f"Listener error on {channel}: {e}")


class RedisBackend(IPCBackend):
    """
    Redis-based IPC backend for distributed deployment.

    Optional - falls back to Multiprocess if Redis is not available.
    """

    def __init__(self, redis_url: str = "redis://localhost:6379"):
        self._redis_url = redis_url
        self._redis = None
        self._pubsub = None
        self._running = False
        self._listener_task = None

    async def initialize(self) -> None:
        try:
            import redis.asyncio as redis
            self._redis = redis.from_url(self._redis_url)
            self._pubsub = self._redis.pubsub()
            self._running = True
            logger.info("Redis IPC backend initialized")
        except ImportError:
            logger.warning("Redis not installed, falling back to multiprocess")
            raise
        except Exception as e:
            logger.warning(f"Redis connection failed: {e}, falling back to multiprocess")
            raise

    async def shutdown(self) -> None:
        self._running = False
        if self._listener_task:
            self._listener_task.cancel()
        if self._pubsub:
            await self._pubsub.close()
        if self._redis:
            await self._redis.close()
        logger.info("Redis IPC backend shut down")

    async def publish(self, channel: str, message: IPCMessage) -> None:
        await self._redis.publish(channel, message.to_json())

    async def subscribe(self, channel: str, callback: Callable) -> None:
        await self._pubsub.subscribe(channel)

        if self._listener_task is None:
            self._listener_task = asyncio.create_task(self._listener_loop(callback))

    async def _listener_loop(self, callback: Callable) -> None:
        while self._running:
            try:
                message = await self._pubsub.get_message(ignore_subscribe_messages=True)
                if message:
                    data = IPCMessage.from_json(message['data'])
                    callback(data)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Redis listener error: {e}")
            await asyncio.sleep(0.01)


class IPCManager:
    """
    High-level IPC manager for broker communication.

    Automatically selects the appropriate backend:
    - InProcess for single broker mode
    - Multiprocess for multi-broker mode
    - Redis if available and configured
    """

    CHANNEL_TICKS = "arb:ticks"
    CHANNEL_ORDERS = "arb:orders"
    CHANNEL_COMMANDS = "arb:commands"
    CHANNEL_STATUS = "arb:status"

    def __init__(self, use_redis: bool = False, single_broker_mode: bool = False):
        self._use_redis = use_redis
        self._single_broker_mode = single_broker_mode
        self._backend: Optional[IPCBackend] = None
        self._correlation_futures: Dict[str, asyncio.Future] = {}

    async def initialize(self) -> None:
        """Initialize IPC backend"""
        if self._single_broker_mode:
            self._backend = InProcessBackend()
        elif self._use_redis:
            try:
                self._backend = RedisBackend()
            except Exception:
                self._backend = MultiprocessBackend()
        else:
            self._backend = MultiprocessBackend()

        await self._backend.initialize()

    async def shutdown(self) -> None:
        """Shutdown IPC"""
        if self._backend:
            await self._backend.shutdown()

    # ==================== Tick Publishing ====================

    async def publish_tick(self, broker_id: str, symbol: str, bid: float, ask: float) -> None:
        """Publish tick data from broker"""
        message = IPCMessage(
            msg_type=MessageType.TICK.value,
            broker_id=broker_id,
            payload={
                'symbol': symbol,
                'bid': bid,
                'ask': ask
            }
        )
        channel = f"{self.CHANNEL_TICKS}:{broker_id}"
        await self._backend.publish(channel, message)

    async def subscribe_ticks(self, broker_id: str, callback: Callable) -> None:
        """Subscribe to tick data from broker"""
        channel = f"{self.CHANNEL_TICKS}:{broker_id}"
        await self._backend.subscribe(channel, callback)

    # ==================== Order Management ====================

    async def send_order(self, broker_id: str, action: str, symbol: str,
                        side: str, volume: float, order_type: str = "MARKET",
                        price: float = None, ticket: int = None,
                        correlation_id: str = None) -> None:
        """Send order request to broker"""
        message = IPCMessage(
            msg_type=MessageType.ORDER_REQUEST.value,
            broker_id=broker_id,
            payload={
                'action': action,
                'symbol': symbol,
                'side': side,
                'volume': volume,
                'order_type': order_type,
                'price': price,
                'ticket': ticket
            },
            correlation_id=correlation_id
        )
        channel = f"{self.CHANNEL_ORDERS}:{broker_id}"
        await self._backend.publish(channel, message)

    async def subscribe_orders(self, broker_id: str, callback: Callable) -> None:
        """Subscribe to order requests (for broker workers)"""
        channel = f"{self.CHANNEL_ORDERS}:{broker_id}"
        await self._backend.subscribe(channel, callback)

    async def publish_order_result(self, broker_id: str, success: bool,
                                   ticket: int = None, price: float = None,
                                   error: str = None, correlation_id: str = None) -> None:
        """Publish order execution result"""
        message = IPCMessage(
            msg_type=MessageType.ORDER_RESULT.value,
            broker_id=broker_id,
            payload={
                'success': success,
                'ticket': ticket,
                'price': price,
                'error': error
            },
            correlation_id=correlation_id
        )
        channel = f"{self.CHANNEL_ORDERS}:{broker_id}:results"
        await self._backend.publish(channel, message)

        # Resolve any waiting futures
        if correlation_id and correlation_id in self._correlation_futures:
            future = self._correlation_futures.pop(correlation_id)
            if not future.done():
                future.set_result(message)

    # ==================== Commands ====================

    async def send_command(self, broker_id: str, command: CommandType,
                          **kwargs) -> None:
        """Send command to broker worker"""
        message = IPCMessage(
            msg_type=MessageType.COMMAND.value,
            broker_id=broker_id,
            payload={
                'command': command.value,
                **kwargs
            }
        )
        channel = f"{self.CHANNEL_COMMANDS}:{broker_id}"
        await self._backend.publish(channel, message)

    async def subscribe_commands(self, broker_id: str, callback: Callable) -> None:
        """Subscribe to commands (for broker workers)"""
        channel = f"{self.CHANNEL_COMMANDS}:{broker_id}"
        await self._backend.subscribe(channel, callback)

    # ==================== Status ====================

    async def publish_status(self, broker_id: str, status: str,
                            latency_ms: float = None) -> None:
        """Publish broker status update"""
        message = IPCMessage(
            msg_type=MessageType.STATUS.value,
            broker_id=broker_id,
            payload={
                'status': status,
                'latency_ms': latency_ms
            }
        )
        channel = f"{self.CHANNEL_STATUS}:{broker_id}"
        await self._backend.publish(channel, message)

    async def subscribe_status(self, broker_id: str, callback: Callable) -> None:
        """Subscribe to broker status updates"""
        channel = f"{self.CHANNEL_STATUS}:{broker_id}"
        await self._backend.subscribe(channel, callback)

    # ==================== Utilities ====================

    async def wait_for_result(self, correlation_id: str, timeout: float = 30.0) -> Optional[IPCMessage]:
        """Wait for a result with given correlation ID"""
        future = asyncio.get_event_loop().create_future()
        self._correlation_futures[correlation_id] = future

        try:
            return await asyncio.wait_for(future, timeout)
        except asyncio.TimeoutError:
            self._correlation_futures.pop(correlation_id, None)
            return None
