"""Failure-safe optional Redis service shared by controller subsystems."""

from __future__ import annotations

import importlib
from collections.abc import AsyncIterator, Mapping, Sequence
from typing import Any, cast

from cognis.logging import get_logger

logger = get_logger(__name__)


class RedisUnavailableError(RuntimeError):
    """Raised when an operation explicitly requires available Redis."""


class RedisPubSub:
    """Owned Pub/Sub consumer backed by the service's shared pool."""

    def __init__(self, service: RedisService, pubsub: Any) -> None:
        self._service = service
        self._pubsub = pubsub
        self._closed = False

    async def subscribe(self, *channels: str | bytes) -> bool:
        return await self._call("subscribe", *channels)

    async def unsubscribe(self, *channels: str | bytes) -> bool:
        return await self._call("unsubscribe", *channels)

    async def get_message(
        self,
        *,
        ignore_subscribe_messages: bool = False,
        timeout: float = 0.0,
    ) -> Mapping[str, Any] | None:
        if self._closed:
            return None
        try:
            message = await self._pubsub.get_message(
                ignore_subscribe_messages=ignore_subscribe_messages,
                timeout=timeout,
            )
        except Exception:
            self._service._mark_unavailable("Redis Pub/Sub operation failed")
            return None
        self._service._mark_available()
        return cast(Mapping[str, Any] | None, message)

    async def listen(self) -> AsyncIterator[Mapping[str, Any]]:
        """Poll messages with bounded reads so the shared socket timeout is not fatal."""

        while not self._closed:
            try:
                message = await self._pubsub.get_message(
                    ignore_subscribe_messages=True,
                    timeout=1.0,
                )
            except Exception:
                self._service._mark_unavailable("Redis Pub/Sub operation failed")
                raise
            self._service._mark_available()
            if message is not None:
                yield message

    async def _call(self, method: str, *args: str | bytes) -> bool:
        if self._closed:
            return False
        try:
            await getattr(self._pubsub, method)(*args)
        except Exception:
            self._service._mark_unavailable("Redis Pub/Sub operation failed")
            return False
        self._service._mark_available()
        return True

    async def aclose(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            await self._pubsub.aclose()
        except Exception:
            logger.warning("Redis Pub/Sub close failed")
        finally:
            self._service._discard_pubsub(self)


class RedisService:
    """Application-scoped optional Redis client with failure-safe operations."""

    def __init__(
        self,
        url: str,
        *,
        connect_timeout_seconds: float = 2.0,
        operation_timeout_seconds: float = 2.0,
    ) -> None:
        self.configured = bool(url.strip())
        self.available = False
        self._availability_epoch = 0
        self._client: Any | None = None
        self._pubsubs: set[RedisPubSub] = set()
        self._closed = False
        if not self.configured:
            return
        try:
            aioredis = importlib.import_module("redis.asyncio")
            self._client = aioredis.from_url(
                url,
                decode_responses=False,
                socket_connect_timeout=connect_timeout_seconds,
                socket_timeout=operation_timeout_seconds,
            )
        except Exception:
            logger.warning("Redis client initialization failed")

    async def start(self) -> bool:
        """Probe Redis without making application startup depend on it."""
        return await self.ping()

    async def ping(self) -> bool:
        return bool(await self._call("ping", default=False))

    async def get(self, key: str | bytes) -> bytes | None:
        result = await self._call("get", key, default=None)
        if result is None:
            return None
        return result if isinstance(result, bytes) else str(result).encode()

    async def set(self, key: str | bytes, value: bytes, *, ttl_seconds: int) -> bool:
        return bool(await self._call("set", key, value, ex=ttl_seconds, default=False))

    async def set_if_absent(
        self, key: str | bytes, value: bytes, *, ttl_seconds: int
    ) -> bool | None:
        """Set a short-lived coordination value only when the key is absent."""

        result = await self._call(
            "set",
            key,
            value,
            ex=ttl_seconds,
            nx=True,
            default=None,
        )
        return None if result is None and not self.available else bool(result)

    async def delete(self, key: str | bytes) -> bool:
        return bool(await self._call("delete", key, default=False))

    async def eval(
        self,
        script: str | bytes,
        *,
        keys: Sequence[str | bytes] = (),
        args: Sequence[str | bytes | int | float] = (),
    ) -> Any | None:
        return await self._call("eval", script, len(keys), *keys, *args, default=None)

    async def publish(self, channel: str | bytes, message: bytes) -> bool:
        return await self._call("publish", channel, message, default=False) is not False

    def create_pubsub(self) -> RedisPubSub | None:
        """Create a dedicated consumer sharing the client's connection pool."""
        if self._client is None or self._closed:
            return None
        try:
            consumer = RedisPubSub(self, self._client.pubsub())
        except Exception:
            self._mark_unavailable("Redis Pub/Sub creation failed")
            return None
        self._pubsubs.add(consumer)
        return consumer

    async def _call(self, method: str, *args: Any, default: Any, **kwargs: Any) -> Any:
        if self._client is None or self._closed:
            return default
        try:
            result = await getattr(self._client, method)(*args, **kwargs)
        except Exception:
            self._mark_unavailable("Redis operation failed")
            return default
        self._mark_available()
        return result

    def _mark_available(self) -> None:
        self._set_available(True)

    def _mark_unavailable(self, message: str) -> None:
        self._set_available(False)
        logger.warning(message)

    @property
    def availability_epoch(self) -> int:
        """Monotonic process-local marker for observed availability transitions."""
        return self._availability_epoch

    def _set_available(self, available: bool) -> None:
        if self.available != available:
            self.available = available
            self._availability_epoch += 1

    def _discard_pubsub(self, consumer: RedisPubSub) -> None:
        self._pubsubs.discard(consumer)

    async def aclose(self) -> None:
        """Close consumers before the shared client; safe to call repeatedly."""
        if self._closed:
            return
        self._closed = True
        for consumer in list(self._pubsubs):
            await consumer.aclose()
        self._pubsubs.clear()
        client, self._client = self._client, None
        if client is not None:
            try:
                await client.aclose()
            except Exception:
                logger.warning("Redis client close failed")
        self._set_available(False)
