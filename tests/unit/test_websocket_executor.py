"""Unit tests for WebSocket executor connection and provider."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from cognis.models.tool import ExecutorCapabilities, ExecutorConfig, ToolCall
from cognis.providers.circuit_breaker import CircuitBreaker
from cognis.providers.executor.websocket import (
    ExecutorDisconnectedError,
    WebSocketExecutorConnection,
    WebSocketExecutorProvider,
)


class FakeWebSocket:
    """Minimal WebSocket mock for testing."""

    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []
        self._receive_queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self.closed = False

    async def send_json(self, data: dict[str, Any]) -> None:
        self.sent.append(data)

    async def receive_json(self) -> dict[str, Any]:
        return await self._receive_queue.get()

    def inject_message(self, data: dict[str, Any]) -> None:
        self._receive_queue.put_nowait(data)


@pytest.mark.asyncio
async def test_rpc_call_sends_jsonrpc_request() -> None:
    """rpc_call sends a JSON-RPC 2.0 request and awaits correlated response."""
    ws = FakeWebSocket()
    conn = WebSocketExecutorConnection(
        ws,
        "exec-1",
        ExecutorCapabilities(tools=["bash"]),
        breaker=CircuitBreaker(failure_threshold=10, recovery_timeout=1),
    )
    conn.start_receiver()

    async def respond() -> None:
        await asyncio.sleep(0.05)
        assert len(ws.sent) == 1
        request = ws.sent[0]
        assert request["jsonrpc"] == "2.0"
        assert request["method"] == "tool.list"
        # Send correlated response
        ws.inject_message(
            {
                "jsonrpc": "2.0",
                "result": {"tools": [{"name": "bash"}]},
                "id": request["id"],
            }
        )

    asyncio.create_task(respond())
    result = await conn.rpc_call("tool.list", {}, timeout=5.0)
    assert result == {"tools": [{"name": "bash"}]}
    await conn.close()


@pytest.mark.asyncio
async def test_tool_execute_returns_tool_result() -> None:
    """tool_execute wraps tool.execute RPC and returns ToolResult."""
    ws = FakeWebSocket()
    conn = WebSocketExecutorConnection(
        ws,
        "exec-1",
        ExecutorCapabilities(tools=["bash"]),
        breaker=CircuitBreaker(failure_threshold=10, recovery_timeout=1),
    )
    conn.start_receiver()

    async def respond() -> None:
        await asyncio.sleep(0.05)
        request = ws.sent[0]
        ws.inject_message(
            {
                "jsonrpc": "2.0",
                "result": {
                    "call_id": "tc-1",
                    "output": "hello world",
                    "is_error": False,
                    "duration_ms": 42,
                },
                "id": request["id"],
            }
        )

    asyncio.create_task(respond())
    result = await conn.tool_execute(
        ToolCall(call_id="tc-1", name="bash", arguments={"command": "echo hello"}),
        timeout_seconds=5,
    )
    assert result.output == "hello world"
    assert result.is_error is False
    assert result.duration_ms == 42
    await conn.close()


@pytest.mark.asyncio
async def test_disconnected_connection_raises_error() -> None:
    """rpc_call raises ExecutorDisconnectedError when not connected."""
    ws = FakeWebSocket()
    conn = WebSocketExecutorConnection(
        ws,
        "exec-1",
        ExecutorCapabilities(),
        breaker=CircuitBreaker(failure_threshold=10, recovery_timeout=1),
    )
    conn._connected = False

    with pytest.raises(ExecutorDisconnectedError):
        await conn.rpc_call("tool.list", {})


@pytest.mark.asyncio
async def test_close_fails_pending_rpcs() -> None:
    """Closing a connection fails all pending RPC futures."""
    ws = FakeWebSocket()
    conn = WebSocketExecutorConnection(
        ws,
        "exec-1",
        ExecutorCapabilities(),
        breaker=CircuitBreaker(failure_threshold=10, recovery_timeout=1),
    )
    conn.start_receiver()

    # Start an RPC call that won't get a response
    task = asyncio.create_task(conn.rpc_call("tool.list", {}, timeout=10.0))
    await asyncio.sleep(0.05)

    # Close the connection — should fail the pending call
    await conn.close()

    with pytest.raises(ExecutorDisconnectedError):
        await task


@pytest.mark.asyncio
async def test_provider_register_and_get_executor() -> None:
    """WebSocketExecutorProvider registers connections and returns them."""
    provider = WebSocketExecutorProvider()
    ws = FakeWebSocket()
    caps = ExecutorCapabilities(tools=["bash", "read"])

    conn = provider.register_connection("exec-1", ws, caps)
    assert conn is not None
    assert conn.executor_id == "exec-1"

    # get_executor should return the same connection
    from cognis.models.tool import ExecutorHandle

    handle = ExecutorHandle(executor_id="exec-1", executor_type="websocket")
    retrieved = await provider.get_executor(handle)
    assert retrieved is conn


@pytest.mark.asyncio
async def test_provider_unregister_marks_disconnected() -> None:
    """Unregistering a connection marks the handle as disconnected."""
    provider = WebSocketExecutorProvider()
    ws = FakeWebSocket()
    provider.register_connection("exec-1", ws, ExecutorCapabilities())

    provider.unregister_connection("exec-1")

    from cognis.models.tool import ExecutorHandle

    handle = ExecutorHandle(executor_id="exec-1", executor_type="websocket")
    with pytest.raises(ExecutorDisconnectedError):
        await provider.get_executor(handle)


@pytest.mark.asyncio
async def test_provider_spawn_waits_for_connection() -> None:
    """spawn() waits for the executor to connect and send executor.ready."""
    provider = WebSocketExecutorProvider()
    ws = FakeWebSocket()

    async def connect_later() -> None:
        await asyncio.sleep(0.1)
        provider.register_connection("exec-1", ws, ExecutorCapabilities(tools=["bash"]))

    asyncio.create_task(connect_later())

    config = ExecutorConfig(executor_id="exec-1")
    handle = await provider.spawn(config)
    assert handle.executor_id == "exec-1"
    assert handle.executor_type == "websocket"


@pytest.mark.asyncio
async def test_provider_spawn_timeout() -> None:
    """spawn() raises TimeoutError if executor doesn't connect in time."""
    provider = WebSocketExecutorProvider()

    # Patch the timeout to be very short for testing
    config = ExecutorConfig(executor_id="exec-never")
    with pytest.raises(TimeoutError):
        # Override the wait_for timeout by monkey-patching

        async def fast_spawn(cfg: ExecutorConfig) -> Any:
            event = asyncio.Event()
            provider._ready_events[cfg.executor_id] = event
            try:
                await asyncio.wait_for(event.wait(), timeout=0.1)
            except TimeoutError:
                provider._ready_events.pop(cfg.executor_id, None)
                raise
            return provider._handles[cfg.executor_id]

        await fast_spawn(config)


@pytest.mark.asyncio
async def test_provider_list_active() -> None:
    """list_active returns only connected executors."""
    provider = WebSocketExecutorProvider()
    ws1 = FakeWebSocket()
    ws2 = FakeWebSocket()

    provider.register_connection("exec-1", ws1, ExecutorCapabilities())
    provider.register_connection("exec-2", ws2, ExecutorCapabilities())

    active = await provider.list_active()
    assert len(active) == 2

    provider.unregister_connection("exec-1")
    active = await provider.list_active()
    assert len(active) == 1
    assert active[0].executor_id == "exec-2"
