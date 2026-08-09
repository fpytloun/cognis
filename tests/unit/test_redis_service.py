from __future__ import annotations

import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from cognis.core.redis_service import RedisService


class _Client:
    def __init__(self) -> None:
        self.ping = AsyncMock(return_value=True)
        self.get = AsyncMock(return_value=b"value")
        self.set = AsyncMock(return_value=True)
        self.delete = AsyncMock(return_value=1)
        self.eval = AsyncMock(return_value=b"result")
        self.publish = AsyncMock(return_value=0)
        self.aclose = AsyncMock()
        self.consumer = _PubSub()

    def pubsub(self) -> _PubSub:
        return self.consumer


class _PubSub:
    def __init__(self) -> None:
        self.subscribe = AsyncMock()
        self.unsubscribe = AsyncMock()
        self.get_message = AsyncMock(return_value={"data": b"message"})
        self.aclose = AsyncMock()


def _service(
    monkeypatch: pytest.MonkeyPatch,
    client: _Client,
    *,
    from_url_calls: list[tuple[tuple[object, ...], dict[str, object]]] | None = None,
) -> RedisService:
    def _from_url(*args: object, **kwargs: object) -> _Client:
        if from_url_calls is not None:
            from_url_calls.append((args, kwargs))
        return client

    module = SimpleNamespace(from_url=_from_url)
    monkeypatch.setattr("cognis.core.redis_service.importlib.import_module", lambda _name: module)
    return RedisService("redis://user:secret@redis.internal/0")


@pytest.mark.asyncio
async def test_unconfigured_and_package_missing_degrade_cleanly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    unconfigured = RedisService("")
    assert not unconfigured.configured
    assert not await unconfigured.start()
    assert await unconfigured.get(b"key") is None

    def _missing(_name: str) -> None:
        raise ModuleNotFoundError

    monkeypatch.setattr("cognis.core.redis_service.importlib.import_module", _missing)
    missing = RedisService("redis://localhost")
    assert missing.configured
    assert not missing.available
    assert not await missing.ping()


def test_constructor_failure_does_not_leak_url(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    secret_url = "redis://user:secret@redis.internal/0"
    module = SimpleNamespace(from_url=lambda *_args, **_kwargs: (_ for _ in ()).throw(ValueError()))
    monkeypatch.setattr("cognis.core.redis_service.importlib.import_module", lambda _name: module)

    with caplog.at_level(logging.WARNING):
        service = RedisService(secret_url)

    assert service.configured
    assert secret_url not in caplog.text
    assert "secret" not in caplog.text


@pytest.mark.asyncio
async def test_start_and_operation_failures_recover(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _Client()
    client.ping.side_effect = [ConnectionError, True]
    service = _service(monkeypatch, client)

    assert not await service.start()
    assert not service.available
    assert await service.ping()
    assert service.available

    client.get.side_effect = [ConnectionError, b"recovered"]
    assert await service.get(b"key") is None
    assert not service.available
    assert await service.get(b"key") == b"recovered"
    assert service.available


@pytest.mark.asyncio
async def test_availability_epoch_tracks_only_state_transitions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _Client()
    client.get.side_effect = [b"value", ConnectionError, ConnectionError, b"value"]
    service = _service(monkeypatch, client)

    assert service.availability_epoch == 0
    assert await service.get(b"key") == b"value"
    assert service.availability_epoch == 1
    assert await service.get(b"key") is None
    assert service.availability_epoch == 2
    assert await service.get(b"key") is None
    assert service.availability_epoch == 2
    assert await service.get(b"key") == b"value"
    assert service.availability_epoch == 3


@pytest.mark.asyncio
async def test_operations_are_byte_safe_and_use_ttl(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _Client()
    from_url_calls: list[tuple[tuple[object, ...], dict[str, object]]] = []
    service = _service(monkeypatch, client, from_url_calls=from_url_calls)

    assert await service.get(b"key") == b"value"
    assert await service.set(b"key", b"value", ttl_seconds=30)
    assert await service.delete(b"key")
    assert await service.eval(b"script", keys=[b"key"], args=[b"arg"]) == b"result"
    assert await service.publish(b"channel", b"message")
    assert from_url_calls == [
        (
            ("redis://user:secret@redis.internal/0",),
            {
                "decode_responses": False,
                "socket_connect_timeout": 2.0,
                "socket_timeout": 2.0,
            },
        )
    ]
    client.get.assert_awaited_once_with(b"key")
    client.set.assert_awaited_once_with(b"key", b"value", ex=30)
    client.eval.assert_awaited_once_with(b"script", 1, b"key", b"arg")


@pytest.mark.asyncio
async def test_pubsub_operations_recover_and_preserve_binary_messages(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _Client()
    client.consumer.subscribe.side_effect = [ConnectionError, None]
    client.consumer.get_message.side_effect = [
        ConnectionError,
        {"channel": b"runtime", "data": b"\x00runtime"},
    ]
    service = _service(monkeypatch, client)
    consumer = service.create_pubsub()
    assert consumer is not None

    assert not await consumer.subscribe(b"runtime")
    assert not service.available
    assert await consumer.subscribe(b"runtime")
    assert service.available
    assert await consumer.get_message() is None
    assert not service.available
    assert await consumer.get_message() == {
        "channel": b"runtime",
        "data": b"\x00runtime",
    }
    assert service.available


@pytest.mark.asyncio
async def test_pubsub_listen_uses_bounded_message_polling(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _Client()
    client.consumer.get_message.side_effect = [
        None,
        {"channel": b"runtime", "data": b"payload"},
    ]
    service = _service(monkeypatch, client)
    consumer = service.create_pubsub()
    assert consumer is not None

    message = await anext(consumer.listen())

    assert message == {"channel": b"runtime", "data": b"payload"}
    assert client.consumer.get_message.await_count == 2
    client.consumer.get_message.assert_awaited_with(
        ignore_subscribe_messages=True,
        timeout=1.0,
    )


@pytest.mark.asyncio
async def test_pubsub_listen_propagates_connection_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _Client()
    client.consumer.get_message.side_effect = ConnectionError
    service = _service(monkeypatch, client)
    consumer = service.create_pubsub()
    assert consumer is not None

    with pytest.raises(ConnectionError):
        await anext(consumer.listen())

    assert not service.available
    client.consumer.get_message.assert_awaited_once_with(
        ignore_subscribe_messages=True,
        timeout=1.0,
    )


@pytest.mark.asyncio
async def test_close_orders_pubsub_before_client_and_is_idempotent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    order: list[str] = []
    client = _Client()
    client.consumer.aclose.side_effect = lambda: order.append("pubsub")
    client.aclose.side_effect = lambda: order.append("client")
    service = _service(monkeypatch, client)
    consumer = service.create_pubsub()
    assert consumer is not None

    await service.aclose()
    await service.aclose()

    assert order == ["pubsub", "client"]
    client.consumer.aclose.assert_awaited_once()
    client.aclose.assert_awaited_once()
