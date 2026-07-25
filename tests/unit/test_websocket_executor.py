"""Unit tests for WebSocket executor connection and provider."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from starlette.websockets import WebSocketDisconnect

from cognis.models.tool import ExecutorCapabilities, ExecutorConfig, ExecutorHandle, ToolCall
from cognis.providers.circuit_breaker import CircuitBreaker
from cognis.providers.executor import websocket as websocket_module
from cognis.providers.executor.websocket import (
    ExecutorDisconnectedError,
    WebSocketExecutorConnection,
    WebSocketExecutorProvider,
    executor_reconnect_retry_budget_seconds,
)


def test_executor_token_expired_helper_requires_valid_future_exp() -> None:
    from cognis.api.executor_ws import _executor_token_expired

    assert _executor_token_expired({}) is True
    assert _executor_token_expired({"exp": "not-int"}) is True
    assert _executor_token_expired({"exp": 1}) is True
    assert _executor_token_expired({"exp": 4_102_444_800}) is False


def test_ready_runtime_metadata_preserves_existing_state_and_refreshes_platform() -> None:
    from cognis.api.executor_ws import _ready_runtime_metadata
    from cognis.models.executor_resources import ExecutorResourceSnapshot

    row = type(
        "Row",
        (),
        {
            "runtime_metadata": {
                "mcp_servers": [{"name": "todoist", "status": "ready"}],
                "platform": {"os": "linux", "arch": "x86_64"},
            }
        },
    )()
    snapshot = ExecutorResourceSnapshot(
        observed_at="2026-07-13T10:00:00Z",
        os="darwin",
        arch="arm64",
    )

    metadata = _ready_runtime_metadata(
        row,
        environment={"home": "/Users/alice", "token": "discard"},
        platform={
            "os": "darwin",
            "arch": "arm64",
            "python": "x" * 200,
            "private": "discarded",
        },
        resource_snapshot=snapshot,
        received_at=datetime(2026, 7, 13, 10, 0, 5, tzinfo=UTC),
    )

    assert metadata["mcp_servers"] == [{"name": "todoist", "status": "ready"}]
    assert metadata["platform"] == {
        "os": "darwin",
        "arch": "arm64",
        "python": "x" * 128,
    }
    assert metadata["resource_snapshot"]["arch"] == "arm64"
    assert metadata["resource_snapshot_received_at"] == "2026-07-13T10:00:05+00:00"
    assert metadata["environment"] == {"home": "/Users/alice"}


def test_ready_runtime_metadata_does_not_replace_newer_resource_snapshot() -> None:
    from cognis.api.executor_ws import _ready_runtime_metadata
    from cognis.models.executor_resources import ExecutorResourceSnapshot

    row = type(
        "Row",
        (),
        {
            "runtime_metadata": {
                "resource_snapshot": {
                    "observed_at": "2026-07-13T10:01:00Z",
                    "os": "linux",
                },
                "resource_snapshot_received_at": "2026-07-13T10:01:05Z",
            }
        },
    )()

    metadata = _ready_runtime_metadata(
        row,
        environment=None,
        platform=None,
        resource_snapshot=ExecutorResourceSnapshot(
            observed_at="2026-07-13T10:00:00Z",
            os="darwin",
        ),
        received_at=datetime(2026, 7, 13, 10, 2, tzinfo=UTC),
    )

    assert metadata["resource_snapshot"]["os"] == "linux"
    assert metadata["resource_snapshot_received_at"] == "2026-07-13T10:01:05Z"


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


class DisconnectingWebSocket(FakeWebSocket):
    async def receive_json(self) -> dict[str, Any]:
        raise WebSocketDisconnect(code=1011, reason="heartbeat timeout")


class CloseTrackingConnection:
    def __init__(self) -> None:
        self.connected = True
        self.closed = False

    async def close(self) -> None:
        self.connected = False
        self.closed = True


@pytest.mark.asyncio
async def test_receiver_preserves_bounded_websocket_close_metadata() -> None:
    conn = WebSocketExecutorConnection(
        DisconnectingWebSocket(),
        "exec-1",
        ExecutorCapabilities(),
    )

    conn.start_receiver()
    await conn.wait_until_closed()

    assert conn.connected is False
    assert conn.close_metadata == {
        "close_code": 1011,
        "close_reason": "heartbeat timeout",
        "error_type": "WebSocketDisconnect",
    }


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
                    "metadata": {"analysis": "ok"},
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
    assert result.metadata == {"analysis": "ok"}
    await conn.close()


@pytest.mark.asyncio
async def test_oauth_loopback_callback_notification_dispatches() -> None:
    ws = FakeWebSocket()
    conn = WebSocketExecutorConnection(
        ws,
        "exec-1",
        ExecutorCapabilities(),
        breaker=CircuitBreaker(failure_threshold=10, recovery_timeout=1),
    )
    received: list[tuple[str, dict[str, Any]]] = []

    async def callback(executor_id: str, payload: dict[str, Any]) -> None:
        received.append((executor_id, payload))

    conn.register_oauth_loopback_callback(callback)
    conn.start_receiver()
    ws.inject_message(
        {
            "jsonrpc": "2.0",
            "method": "oauth.loopback_callback",
            "params": {"listener_id": "listener-1", "state": "state", "code": "code"},
        }
    )
    for _ in range(20):
        if received:
            break
        await asyncio.sleep(0.01)

    assert received == [
        (
            "exec-1",
            {"listener_id": "listener-1", "state": "state", "code": "code"},
        )
    ]
    await conn.close()


@pytest.mark.asyncio
async def test_heartbeat_dispatches_resource_snapshot() -> None:
    ws = FakeWebSocket()
    conn = WebSocketExecutorConnection(
        ws,
        "exec-1",
        ExecutorCapabilities(),
        breaker=CircuitBreaker(failure_threshold=10, recovery_timeout=1),
    )
    received: list[tuple[str, dict[str, Any]]] = []

    async def callback(executor_id: str, payload: dict[str, Any]) -> None:
        received.append((executor_id, payload))

    conn.register_resource_snapshot_callback(callback)
    conn.start_receiver()
    ws.inject_message(
        {
            "jsonrpc": "2.0",
            "method": "executor.heartbeat",
            "params": {
                "resource_snapshot": {
                    "observed_at": "2026-07-13T10:00:00Z",
                    "os": "linux",
                }
            },
        }
    )
    for _ in range(20):
        if received:
            break
        await asyncio.sleep(0.01)

    assert len(received) == 1
    assert received[0][0] == "exec-1"
    assert received[0][1]["observed_at"] == "2026-07-13T10:00:00Z"
    assert received[0][1]["os"] == "linux"
    await conn.close()


@pytest.mark.asyncio
async def test_heartbeat_resource_snapshots_are_coalesced_and_rate_limited(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        websocket_module,
        "_RESOURCE_SNAPSHOT_DISPATCH_INTERVAL_SECONDS",
        0.01,
    )
    ws = FakeWebSocket()
    conn = WebSocketExecutorConnection(
        ws,
        "exec-1",
        ExecutorCapabilities(),
        breaker=CircuitBreaker(failure_threshold=10, recovery_timeout=1),
    )
    received: list[dict[str, Any]] = []
    first_started = asyncio.Event()
    release_first = asyncio.Event()

    async def callback(_executor_id: str, payload: dict[str, Any]) -> None:
        received.append(payload)
        if len(received) == 1:
            first_started.set()
            await release_first.wait()

    conn.register_resource_snapshot_callback(callback)
    conn.start_receiver()
    observed_at = datetime.now(UTC).isoformat()
    ws.inject_message(
        {
            "jsonrpc": "2.0",
            "method": "executor.heartbeat",
            "params": {
                "resource_snapshot": {
                    "observed_at": observed_at,
                    "runtime": {"active_calls": 0},
                }
            },
        }
    )
    await asyncio.wait_for(first_started.wait(), timeout=1)
    for active_calls in range(1, 50):
        ws.inject_message(
            {
                "jsonrpc": "2.0",
                "method": "executor.heartbeat",
                "params": {
                    "resource_snapshot": {
                        "observed_at": observed_at,
                        "runtime": {"active_calls": active_calls},
                    }
                },
            }
        )
    for _ in range(20):
        if ws._receive_queue.empty():
            break
        await asyncio.sleep(0.01)

    assert len(received) == 1
    release_first.set()
    for _ in range(40):
        if len(received) == 2:
            break
        await asyncio.sleep(0.01)

    assert len(received) == 2
    assert received[-1]["runtime"]["active_calls"] == 49
    await conn.close()


@pytest.mark.asyncio
async def test_heartbeat_rejects_unbounded_resource_snapshot_cardinality() -> None:
    ws = FakeWebSocket()
    conn = WebSocketExecutorConnection(
        ws,
        "exec-1",
        ExecutorCapabilities(),
        breaker=CircuitBreaker(failure_threshold=10, recovery_timeout=1),
    )
    received: list[dict[str, Any]] = []

    async def callback(_executor_id: str, payload: dict[str, Any]) -> None:
        received.append(payload)

    conn.register_resource_snapshot_callback(callback)
    conn.start_receiver()
    ws.inject_message(
        {
            "jsonrpc": "2.0",
            "method": "executor.heartbeat",
            "params": {
                "resource_snapshot": {
                    "observed_at": datetime.now(UTC).isoformat(),
                    "accelerators": [
                        {"backend": "nvidia", "name": f"gpu-{index}"} for index in range(17)
                    ],
                }
            },
        }
    )
    await asyncio.sleep(0.05)

    assert received == []
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
async def test_provider_stores_executor_environment_metadata() -> None:
    provider = WebSocketExecutorProvider()
    ws = FakeWebSocket()
    provider.register_connection(
        "exec-1",
        ws,
        ExecutorCapabilities(),
        ready=False,
        metadata={
            "environment": {"home": "/remote/home", "cwd": "/remote/cwd"},
            "platform": {"os": "linux"},
        },
    )
    provider.mark_ready(
        "exec-1",
        ExecutorCapabilities(),
        metadata={
            "environment": {"home": "/remote/home-2", "cwd": "/remote/cwd-2"},
            "platform": {"os": "linux"},
        },
    )

    metadata = provider.get_handle_metadata("exec-1")

    assert metadata is not None
    assert metadata["environment"]["home"] == "/remote/home-2"


@pytest.mark.asyncio
async def test_provider_reconnect_replaces_metadata() -> None:
    provider = WebSocketExecutorProvider()
    provider.register_connection(
        "exec-1",
        FakeWebSocket(),
        ExecutorCapabilities(),
        metadata={"environment": {"home": "/old/home"}},
    )
    provider.register_connection(
        "exec-1",
        FakeWebSocket(),
        ExecutorCapabilities(),
        metadata={"environment": {"home": "/new/home"}},
    )

    metadata = provider.get_handle_metadata("exec-1")

    assert metadata is not None
    assert metadata["environment"]["home"] == "/new/home"


@pytest.mark.asyncio
async def test_provider_metadata_absent_environment_is_supported() -> None:
    provider = WebSocketExecutorProvider()
    provider.register_connection(
        "exec-1",
        FakeWebSocket(),
        ExecutorCapabilities(),
        metadata={"platform": {"os": "linux"}},
    )

    metadata = provider.get_handle_metadata("exec-1")

    assert metadata is not None
    assert "environment" not in metadata


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
async def test_provider_unregister_ignores_stale_connection_cleanup() -> None:
    provider = WebSocketExecutorProvider()
    first = provider.register_connection("exec-1", FakeWebSocket(), ExecutorCapabilities())
    second = provider.register_connection("exec-1", FakeWebSocket(), ExecutorCapabilities())

    provider.unregister_connection("exec-1", first)

    handle = await provider.get_executor(
        ExecutorHandle(executor_id="exec-1", executor_type="websocket")
    )
    assert handle is second


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


@pytest.mark.asyncio
async def test_provider_cleanup_does_not_send_executor_cancel() -> None:
    provider = WebSocketExecutorProvider()
    conn = CloseTrackingConnection()
    provider._connections["exec-1"] = conn  # type: ignore[assignment]
    provider._handles["exec-1"] = ExecutorHandle(
        executor_id="exec-1",
        executor_type="websocket",
        capabilities=ExecutorCapabilities(),
        status="ready",
    )

    await provider.cleanup()

    assert conn.closed is True
    assert provider._connections == {}
    assert provider._handles["exec-1"].status == "disconnected"


@pytest.mark.asyncio
async def test_provider_reconnect_closes_old_connection() -> None:
    """register_connection must schedule close() on the previous connection.

    Previously the old connection was only pop()ped from _connections but
    not closed, leaving its receiver task running and pending RPCs dangling.
    """
    provider = WebSocketExecutorProvider()

    old_conn = CloseTrackingConnection()
    provider._connections["exec-1"] = old_conn  # type: ignore[assignment]
    provider._handles["exec-1"] = ExecutorHandle(
        executor_id="exec-1",
        executor_type="websocket",
        capabilities=ExecutorCapabilities(),
        status="ready",
    )

    # Register a new connection for the same executor_id.
    new_ws = FakeWebSocket()
    new_conn = provider.register_connection("exec-1", new_ws, ExecutorCapabilities())

    # Allow the background close task to run.
    await asyncio.sleep(0)

    assert old_conn.closed is True, "Old connection must be closed on reconnect"
    assert new_conn is not old_conn
    assert provider._connections["exec-1"] is new_conn


@pytest.mark.asyncio
async def test_tool_execute_disconnected_returns_retryable_metadata() -> None:
    ws = FakeWebSocket()
    conn = WebSocketExecutorConnection(
        ws,
        "exec-1",
        ExecutorCapabilities(tools=["read"]),
        breaker=CircuitBreaker(failure_threshold=10, recovery_timeout=1),
    )
    conn._connected = False

    result = await conn.tool_execute(
        ToolCall(call_id="tc-1", name="read", arguments={"file_path": "/tmp/x"}),
        timeout_seconds=5,
    )

    assert result.is_error is True
    assert result.metadata == {
        "code": "executor_disconnected",
        "executor_id": "exec-1",
        "retryable": True,
        "same_executor_only": True,
    }


@pytest.mark.asyncio
async def test_tool_execute_cancelled_is_not_retryable_disconnect() -> None:
    ws = FakeWebSocket()
    conn = WebSocketExecutorConnection(
        ws,
        "exec-1",
        ExecutorCapabilities(tools=["read"]),
        breaker=CircuitBreaker(failure_threshold=10, recovery_timeout=1),
    )

    async def respond() -> None:
        while not ws.sent:
            await asyncio.sleep(0)
        task.cancel()

    task = asyncio.create_task(
        conn.tool_execute(
            ToolCall(call_id="tc-1", name="read", arguments={"file_path": "/tmp/x"}),
            timeout_seconds=5,
        )
    )
    asyncio.create_task(respond())

    result = await task

    assert result.is_error is True
    assert result.metadata == {"code": "tool_execution_cancelled", "retryable": False}


@pytest.mark.asyncio
async def test_provider_wait_for_connection_returns_same_executor_reconnect() -> None:
    provider = WebSocketExecutorProvider()

    wait_task = asyncio.create_task(provider.wait_for_connection("exec-1", timeout=1.0))
    await asyncio.sleep(0)

    new_conn = provider.register_connection("exec-1", FakeWebSocket(), ExecutorCapabilities())

    assert await wait_task is new_conn


@pytest.mark.asyncio
async def test_provider_wait_for_connection_times_out() -> None:
    provider = WebSocketExecutorProvider()

    assert await provider.wait_for_connection("exec-never", timeout=0.01) is None


@pytest.mark.asyncio
async def test_browser_terminal_notification_replays_after_reconnect() -> None:
    provider = WebSocketExecutorProvider()
    owner = {
        "execution_scope_id": "child-session",
        "user_email": "user@example.com",
    }

    await provider.notify_browser_session_terminal(["executor-1"], owner)
    assert provider._pending_browser_terminal["executor-1"]["child-session"] == owner

    calls: list[tuple[str, dict[str, object]]] = []

    class _Connection:
        connected = True

        async def isolated_rpc_call(
            self,
            method: str,
            params: dict[str, object],
            *,
            timeout: float,
        ) -> dict[str, int]:
            assert timeout == 5.0
            calls.append((method, params))
            return {"closed": 1, "complete": True}

    provider._connections["executor-1"] = _Connection()  # type: ignore[assignment]  # noqa: SLF001
    await provider.flush_browser_terminal_notifications("executor-1")

    assert calls == [("browser.session_terminal", {"owner": owner})]
    assert "executor-1" not in provider._pending_browser_terminal


@pytest.mark.asyncio
async def test_browser_terminal_notification_survives_provider_shutdown_restart(
    tmp_path: Path,
) -> None:
    state_path = tmp_path / "browser-terminal.json"
    owner = {
        "execution_scope_id": "child-session",
        "user_email": "user@example.com",
    }
    provider = WebSocketExecutorProvider(browser_terminal_state_path=state_path)
    await provider.notify_browser_session_terminal(["executor-1"], owner)

    await provider.cleanup()

    restarted = WebSocketExecutorProvider(browser_terminal_state_path=state_path)
    assert restarted._pending_browser_terminal == {  # noqa: SLF001
        "executor-1": {"child-session": owner}
    }
    calls: list[dict[str, object]] = []

    class _Connection:
        connected = True

        async def isolated_rpc_call(
            self,
            method: str,
            params: dict[str, object],
            *,
            timeout: float,
        ) -> dict[str, object]:
            del method, timeout
            calls.append(params)
            return {"closed": 0, "complete": True}

    restarted._connections["executor-1"] = _Connection()  # type: ignore[assignment]  # noqa: SLF001
    restarted.mark_ready("executor-1", ExecutorCapabilities())
    await asyncio.gather(*restarted._browser_terminal_flush_tasks.values())  # noqa: SLF001

    assert calls == [{"owner": owner}]
    assert restarted._pending_browser_terminal == {}  # noqa: SLF001
    assert json.loads(state_path.read_text()) == {}
    await restarted.cleanup()


@pytest.mark.asyncio
async def test_browser_terminal_notification_surfaces_persistence_failure(
    tmp_path: Path,
) -> None:
    blocked_parent = tmp_path / "not-a-directory"
    blocked_parent.write_text("blocked")
    provider = WebSocketExecutorProvider(
        browser_terminal_state_path=blocked_parent / "browser-terminal.json"
    )

    with pytest.raises(RuntimeError, match="Failed to persist"):
        await provider.notify_browser_session_terminal(
            ["executor-1"],
            {
                "execution_scope_id": "child-session",
                "user_email": "user@example.com",
            },
        )

    assert provider._pending_browser_terminal["executor-1"]["child-session"]  # noqa: SLF001


@pytest.mark.asyncio
async def test_shutdown_preserves_notification_from_cancelled_inflight_worker(
    tmp_path: Path,
) -> None:
    state_path = tmp_path / "browser-terminal.json"
    provider = WebSocketExecutorProvider(browser_terminal_state_path=state_path)
    delivery_started = asyncio.Event()

    class _BlockedConnection:
        connected = True

        async def isolated_rpc_call(
            self,
            method: str,
            params: dict[str, object],
            *,
            timeout: float,
        ) -> dict[str, object]:
            del method, params, timeout
            delivery_started.set()
            await asyncio.Event().wait()
            return {"complete": True}

        async def close(self) -> None:
            self.connected = False

    provider._connections["executor-1"] = _BlockedConnection()  # type: ignore[assignment]  # noqa: SLF001
    owner = {
        "execution_scope_id": "child-session",
        "user_email": "user@example.com",
    }
    await provider.notify_browser_session_terminal(["executor-1"], owner)
    await delivery_started.wait()

    await provider.cleanup()

    restarted = WebSocketExecutorProvider(browser_terminal_state_path=state_path)
    assert restarted._pending_browser_terminal == {  # noqa: SLF001
        "executor-1": {"child-session": owner}
    }


@pytest.mark.asyncio
async def test_browser_terminal_successful_transport_retries_incomplete_close() -> None:
    provider = WebSocketExecutorProvider()
    calls = 0

    class _Connection:
        connected = True

        async def isolated_rpc_call(
            self,
            method: str,
            params: dict[str, object],
            *,
            timeout: float,
        ) -> dict[str, object]:
            del method, params, timeout
            nonlocal calls
            calls += 1
            return {"closed": 0, "complete": calls > 1}

    provider._connections["executor-1"] = _Connection()  # type: ignore[assignment]  # noqa: SLF001
    await provider.notify_browser_session_terminal(
        ["executor-1"],
        {"execution_scope_id": "child-session", "user_email": "user@example.com"},
    )

    async with asyncio.timeout(2):
        while "executor-1" in provider._pending_browser_terminal:
            await asyncio.sleep(0.01)

    assert calls == 2
    await provider.cleanup()


@pytest.mark.asyncio
async def test_concurrent_browser_terminal_flushes_deliver_once() -> None:
    provider = WebSocketExecutorProvider()
    owner = {"execution_scope_id": "child-session", "user_email": "user@example.com"}
    provider._pending_browser_terminal["executor-1"] = {"child-session": owner}
    release = asyncio.Event()
    calls = 0

    class _Connection:
        connected = True

        async def isolated_rpc_call(
            self,
            method: str,
            params: dict[str, object],
            *,
            timeout: float,
        ) -> dict[str, object]:
            del method, params, timeout
            nonlocal calls
            calls += 1
            await release.wait()
            return {"closed": 1, "complete": True}

    provider._connections["executor-1"] = _Connection()  # type: ignore[assignment]  # noqa: SLF001
    first = asyncio.create_task(provider.flush_browser_terminal_notifications("executor-1"))
    second = asyncio.create_task(provider.flush_browser_terminal_notifications("executor-1"))
    await asyncio.sleep(0)
    release.set()
    await asyncio.gather(first, second)

    assert calls == 1
    assert "executor-1" not in provider._pending_browser_terminal


@pytest.mark.asyncio
async def test_stale_connection_ack_does_not_remove_terminal_notification() -> None:
    provider = WebSocketExecutorProvider()
    owner = {"execution_scope_id": "child-session", "user_email": "user@example.com"}
    provider._pending_browser_terminal["executor-1"] = {"child-session": owner}
    release_old = asyncio.Event()

    class _Connection:
        connected = True

        def __init__(self, release: asyncio.Event | None = None) -> None:
            self.release = release

        async def isolated_rpc_call(
            self,
            method: str,
            params: dict[str, object],
            *,
            timeout: float,
        ) -> dict[str, object]:
            del method, params, timeout
            if self.release is not None:
                await self.release.wait()
            return {"closed": 1, "complete": True}

    old_connection = _Connection(release_old)
    provider._connections["executor-1"] = old_connection  # type: ignore[assignment]  # noqa: SLF001
    old_flush = asyncio.create_task(provider.flush_browser_terminal_notifications("executor-1"))
    await asyncio.sleep(0)
    provider._connections["executor-1"] = _Connection()  # type: ignore[assignment]  # noqa: SLF001
    release_old.set()
    await old_flush

    assert provider._pending_browser_terminal["executor-1"]["child-session"] == owner
    await provider.flush_browser_terminal_notifications("executor-1")
    assert "executor-1" not in provider._pending_browser_terminal


@pytest.mark.asyncio
async def test_isolated_terminal_rpc_timeout_does_not_open_shared_breaker() -> None:
    connection = WebSocketExecutorConnection(
        FakeWebSocket(),
        "executor-1",
        ExecutorCapabilities(),
        breaker=CircuitBreaker(failure_threshold=1, recovery_timeout=30),
    )

    with pytest.raises(TimeoutError):
        await connection.isolated_rpc_call(
            "browser.session_terminal",
            {"owner": {"execution_scope_id": "child-session"}},
            timeout=0.01,
        )

    assert connection.breaker.state == "closed"


@pytest.mark.asyncio
async def test_browser_terminal_pool_notification_does_not_wait_for_executors() -> None:
    provider = WebSocketExecutorProvider()
    blocked = asyncio.Event()

    class _Connection:
        connected = True

        async def isolated_rpc_call(
            self,
            method: str,
            params: dict[str, object],
            *,
            timeout: float,
        ) -> dict[str, object]:
            del method, params, timeout
            await blocked.wait()
            return {"closed": 1, "complete": True}

    executor_ids = [f"executor-{index}" for index in range(20)]
    for executor_id in executor_ids:
        provider._connections[executor_id] = _Connection()  # type: ignore[assignment]  # noqa: SLF001

    await asyncio.wait_for(
        provider.notify_browser_session_terminal(
            executor_ids,
            {"execution_scope_id": "child-session", "user_email": "user@example.com"},
        ),
        timeout=0.05,
    )

    assert set(provider._pending_browser_terminal) == set(executor_ids)
    await provider.cleanup()


def test_reconnect_retry_budget_has_sixty_second_floor(monkeypatch) -> None:
    monkeypatch.setenv("COGNIS_EXECUTOR_RECONNECT_RETRY_BUDGET_SECONDS", "5")

    assert executor_reconnect_retry_budget_seconds() == 60.0


def test_reconnect_retry_budget_can_be_configured_above_floor(monkeypatch) -> None:
    monkeypatch.setenv("COGNIS_EXECUTOR_RECONNECT_RETRY_BUDGET_SECONDS", "90")

    assert executor_reconnect_retry_budget_seconds() == 90.0
