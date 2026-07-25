"""Unit tests for the executor runner module."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
from urllib.parse import urlsplit

import pytest

from cognis.cli import executor as cli_executor
from cognis.executor import __main__ as executor_main
from cognis.executor.runner import (
    _LOCAL_INFERENCE_START_METHODS,
    ExecutorRunner,
    _EventLoopWatchdog,
    _normalize_result,
    _same_turn_tool_call_identity,
    _same_turn_tool_call_key,
    _SameTurnToolCallDeduplicator,
)
from cognis.models.executor_resources import ExecutorResourceSnapshot
from cognis.models.tool import (
    ExecutorConfig,
    MCPServerConfig,
    ToolResult,
    ToolSource,
)
from cognis.models.tool import NativeToolDefinition as ToolDefinition
from cognis.tools.executor.browser.manager import (
    BROWSER_MANAGER_KEY,
    BrowserManager,
    BrowserSession,
)
from cognis.tools.executor.lsp import LSP_MANAGER_KEY, LSP_STATUS_CAPABILITY
from cognis.tools.executor.shell import set_background_shell_completion_callback
from cognis.tools.mcp import MCPClientError


class DummyWebSocket:
    def __init__(self) -> None:
        self.sent: list[dict] = []

    async def send(self, raw: str) -> None:
        self.sent.append(json.loads(raw))


def test_same_turn_tool_call_key_is_provider_neutral_and_cycle_scoped() -> None:
    first = {
        "execution_scope_id": "scope",
        "tool_name": "bash",
        "arguments": {"command": "true", "timeout": 1},
        "runtime_metadata": {"turn_id": "turn", "tool_contract_hash": "contract"},
    }
    reordered = {
        **first,
        "arguments": {"timeout": 1, "command": "true"},
    }
    next_turn = {
        **first,
        "runtime_metadata": {"turn_id": "next-turn", "tool_contract_hash": "contract"},
    }

    assert _same_turn_tool_call_key(first) == _same_turn_tool_call_key(reordered)
    assert _same_turn_tool_call_key(first) != _same_turn_tool_call_key(next_turn)
    assert _same_turn_tool_call_identity(first)[0] != _same_turn_tool_call_identity(next_turn)[0]
    assert _same_turn_tool_call_key(first) == _same_turn_tool_call_key(
        {
            **first,
            "runtime_metadata": {"turn_id": "turn", "tool_contract_hash": "changed-contract"},
        }
    )


def test_same_turn_tool_call_deduplicator_rejects_serial_siblings_and_allows_next_turn() -> None:
    deduplicator = _SameTurnToolCallDeduplicator()
    first = {
        "call_id": "call-first",
        "execution_scope_id": "scope",
        "tool_name": "bash",
        "arguments": {"command": "true"},
        "runtime_metadata": {"turn_id": "turn-one"},
    }
    duplicate = {**first, "call_id": "call-duplicate"}
    next_turn = {
        **first,
        "call_id": "call-next-turn",
        "runtime_metadata": {"turn_id": "turn-two"},
    }

    assert deduplicator.original_call_id(first) is None
    assert deduplicator.original_call_id(duplicate) == "call-first"
    assert deduplicator.original_call_id(next_turn) is None
    assert deduplicator.original_call_id(duplicate) == "call-first"


def _contract_metadata(runner: ExecutorRunner, tool_name: str) -> dict[str, str]:
    definition = next(
        tool for tool in runner._configured_tool_definitions if tool.name == tool_name
    )
    assert definition.descriptor is not None
    return {"tool_contract_hash": definition.descriptor.schema_hash}


class DummyMessageWebSocket(DummyWebSocket):
    def __init__(self, messages: list[dict]) -> None:
        super().__init__()
        self._messages = [json.dumps(message) for message in messages]

    def __aiter__(self) -> DummyMessageWebSocket:
        return self

    async def __anext__(self) -> str:
        if not self._messages:
            raise StopAsyncIteration
        return self._messages.pop(0)


class ClosingWebSocket(DummyWebSocket):
    async def send(self, raw: str) -> None:
        from websockets.exceptions import ConnectionClosedError
        from websockets.frames import Close

        raise ConnectionClosedError(Close(1011, "test"), Close(1011, "test"), True)


class BlockingWebSocket(DummyWebSocket):
    def __init__(self) -> None:
        super().__init__()
        self.close_calls: list[tuple[int, str]] = []

    async def close(self, *, code: int, reason: str) -> None:
        self.close_calls.append((code, reason))


def test_event_loop_watchdog_acknowledges_responsive_loop() -> None:
    callbacks: list[object] = []
    exits: list[int] = []

    class _Loop:
        def call_soon_threadsafe(self, callback: object) -> None:
            callbacks.append(callback)

    watchdog = _EventLoopWatchdog(
        _Loop(),  # type: ignore[arg-type]
        interval_seconds=1.0,
        timeout_seconds=5.0,
        exit_process=exits.append,
        clock=lambda: 10.0,
    )

    assert watchdog.poll_once() is True
    assert len(callbacks) == 1
    assert exits == []


def test_event_loop_watchdog_forces_restart_after_timeout() -> None:
    exits: list[int] = []
    clock_values = iter((10.0, 16.0))
    watchdog = _EventLoopWatchdog(
        SimpleNamespace(call_soon_threadsafe=lambda _: None),
        interval_seconds=1.0,
        timeout_seconds=5.0,
        exit_process=exits.append,
        clock=lambda: next(clock_values),
    )

    assert watchdog.poll_once() is False
    assert exits == [70]


@pytest.mark.asyncio
async def test_local_inference_disabled_is_advertised_and_enforced_at_dispatch() -> None:
    expected_methods = {
        "llm.complete",
        "llm.discover_models",
        "llm.image_generate",
        "llm.transcribe",
        "llm.synthesize",
        "local_model.show",
        "local_model.operation.start",
    }
    assert expected_methods == _LOCAL_INFERENCE_START_METHODS
    runner = ExecutorRunner(ExecutorConfig(executor_id="remote", controller_token="t"))
    configure_ws = DummyWebSocket()
    await runner._handle_configure(
        configure_ws,
        "cfg-disabled",
        {
            "enabled_tools": [],
            "config": {"local_inference_enabled": False},
        },
    )

    capabilities = configure_ws.sent[-1]["result"]["capabilities"]
    assert capabilities["inference"] is False
    assert capabilities["local_inference"] is False
    assert capabilities["local_model_runtime"] is None

    ws = DummyMessageWebSocket(
        [
            {
                "jsonrpc": "2.0",
                "id": f"disabled-{index}",
                "method": method,
                "params": {},
            }
            for index, method in enumerate(sorted(_LOCAL_INFERENCE_START_METHODS))
        ]
    )
    await runner._message_loop(ws)

    assert len(ws.sent) == len(_LOCAL_INFERENCE_START_METHODS)
    assert "local_model.status" not in _LOCAL_INFERENCE_START_METHODS
    assert "local_model.operation.status" not in _LOCAL_INFERENCE_START_METHODS
    assert "local_model.operation.cancel" not in _LOCAL_INFERENCE_START_METHODS
    assert {item["error"]["code"] for item in ws.sent} == {-32045}
    assert {item["error"]["message"] for item in ws.sent} == {
        "Local inference is disabled on this executor"
    }


@pytest.mark.asyncio
async def test_custom_ollama_port_reaches_runner_capability_and_resource_probe() -> None:
    runner = ExecutorRunner(ExecutorConfig(executor_id="remote", controller_token="t"))
    runner._resource_collector.collect = AsyncMock(
        return_value=ExecutorResourceSnapshot(observed_at="2026-07-14T10:00:00Z")
    )
    ws = DummyWebSocket()

    await runner._handle_configure(
        ws,
        "cfg-custom-port",
        {
            "enabled_tools": [],
            "config": {
                "ollama_runtime": {
                    "port": 22434,
                    "management_enabled": True,
                }
            },
        },
    )

    capability = ws.sent[-1]["result"]["capabilities"]["local_model_runtime"]
    assert capability["port"] == 22434
    assert capability["endpoint"] == "http://127.0.0.1:22434"
    runner._resource_collector.collect.assert_awaited_once()
    assert (
        runner._resource_collector.collect.await_args.kwargs["ollama_endpoint"]
        == "http://127.0.0.1:22434"
    )
    await runner._ollama_runtime_handler.close()


@pytest.mark.asyncio
async def test_oauth_loopback_listener_relays_callback_and_cleans_up() -> None:
    runner = ExecutorRunner(ExecutorConfig(executor_id="remote", controller_token="t"))
    ws = DummyWebSocket()

    await runner._handle_oauth_loopback_start(
        ws,
        "rpc-1",
        {"state": "state-1", "ttl_seconds": 30, "callback_path": "/oauth/callback"},
    )

    start_response = ws.sent[0]
    assert start_response["id"] == "rpc-1"
    redirect_uri = start_response["result"]["redirect_uri"]
    listener_id = start_response["result"]["listener_id"]
    parsed = urlsplit(redirect_uri)
    assert parsed.hostname == "127.0.0.1"
    assert parsed.path == "/oauth/callback"

    reader, writer = await asyncio.open_connection("127.0.0.1", parsed.port)
    writer.write(
        b"GET /oauth/callback?state=state-1&code=code-1 HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\n"
    )
    await writer.drain()
    response = await reader.read(1024)
    writer.close()
    await writer.wait_closed()

    assert b"200 OK" in response
    callback = ws.sent[1]
    assert callback["method"] == "oauth.loopback_callback"
    assert callback["params"] == {
        "listener_id": listener_id,
        "redirect_uri": redirect_uri,
        "state": "state-1",
        "code": "code-1",
        "error": None,
        "error_description": None,
    }
    assert listener_id not in runner._oauth_loopback_listeners


@pytest.mark.asyncio
async def test_runner_retries_immediately_after_clean_disconnect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = ExecutorRunner(ExecutorConfig(executor_id="remote", controller_token="t"))
    attempts = 0
    sleeps: list[float] = []

    async def _connect_and_serve() -> None:
        nonlocal attempts
        attempts += 1
        if attempts == 2:
            runner._running = False

    async def _sleep(delay: float) -> None:
        sleeps.append(delay)

    monkeypatch.setattr(runner, "_connect_and_serve", _connect_and_serve)
    monkeypatch.setattr(asyncio, "sleep", _sleep)

    await runner.run()

    assert attempts == 2
    assert sleeps == []


@pytest.mark.asyncio
async def test_heartbeat_send_failure_is_not_swallowed() -> None:
    runner = ExecutorRunner(ExecutorConfig(executor_id="remote", controller_token="t"))

    with pytest.raises(Exception, match="received 1011"):
        await runner._heartbeat_loop(ClosingWebSocket())


@pytest.mark.asyncio
async def test_heartbeat_failure_closes_connection_and_cancels_message_loop(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    runner = ExecutorRunner(ExecutorConfig(executor_id="remote", controller_token="t"))
    ws = BlockingWebSocket()
    message_started = asyncio.Event()
    message_cancelled = asyncio.Event()

    async def _message_loop(_ws: object) -> None:
        message_started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            message_cancelled.set()
            raise

    async def _heartbeat_loop(_ws: object) -> None:
        await message_started.wait()
        raise ConnectionError("test heartbeat failure")

    monkeypatch.setattr(runner, "_message_loop", _message_loop)
    monkeypatch.setattr(runner, "_heartbeat_loop", _heartbeat_loop)
    caplog.set_level(logging.WARNING, logger="cognis.executor.runner")

    with pytest.raises(ConnectionError, match="test heartbeat failure"):
        await runner._run_connection_loops(ws)

    assert message_cancelled.is_set()
    assert ws.close_calls == [(1011, "executor heartbeat failure")]
    assert "Executor heartbeat failed" in caplog.text
    assert "ConnectionError" in caplog.text


@pytest.mark.asyncio
async def test_heartbeat_failure_cancels_and_drains_connection_owned_handlers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = ExecutorRunner(ExecutorConfig(executor_id="remote", controller_token="t"))
    handler_started = asyncio.Event()
    handler_cancelled = asyncio.Event()

    async def _handler() -> None:
        handler_started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            handler_cancelled.set()
            raise

    handler_task = runner._create_background_handler_task(
        _handler(),
        "tool.execute",
        msg_id="rpc-1",
    )
    runner._active_calls["call-1"] = handler_task

    async def _message_loop(_ws: object) -> None:
        await asyncio.Event().wait()

    async def _heartbeat_loop(_ws: object) -> None:
        await handler_started.wait()
        raise ConnectionError("heartbeat disconnected")

    monkeypatch.setattr(runner, "_message_loop", _message_loop)
    monkeypatch.setattr(runner, "_heartbeat_loop", _heartbeat_loop)

    with pytest.raises(ConnectionError):
        await runner._run_connection_loops(BlockingWebSocket())

    assert handler_cancelled.is_set()
    assert runner._connection_handler_tasks == set()
    assert runner._active_calls == {}


@pytest.mark.asyncio
async def test_simultaneous_receive_close_does_not_hide_heartbeat_failure(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    runner = ExecutorRunner(ExecutorConfig(executor_id="remote", controller_token="t"))

    async def _message_loop(_ws: object) -> None:
        return

    async def _heartbeat_loop(_ws: object) -> None:
        raise ConnectionError("simultaneous heartbeat failure")

    monkeypatch.setattr(runner, "_message_loop", _message_loop)
    monkeypatch.setattr(runner, "_heartbeat_loop", _heartbeat_loop)
    caplog.set_level(logging.WARNING, logger="cognis.executor.runner")

    with pytest.raises(ConnectionError, match="simultaneous heartbeat failure"):
        await runner._run_connection_loops(BlockingWebSocket())

    assert "Executor heartbeat failed" in caplog.text


@pytest.mark.asyncio
async def test_clean_controller_close_cancels_heartbeat_without_false_error_or_leak(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    runner = ExecutorRunner(ExecutorConfig(executor_id="remote", controller_token="t"))
    heartbeat_cancelled = asyncio.Event()

    async def _message_loop(_ws: object) -> None:
        return

    async def _heartbeat_loop(_ws: object) -> None:
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            heartbeat_cancelled.set()
            raise

    monkeypatch.setattr(runner, "_message_loop", _message_loop)
    monkeypatch.setattr(runner, "_heartbeat_loop", _heartbeat_loop)
    caplog.set_level(logging.INFO, logger="cognis.executor.runner")

    await runner._run_connection_loops(BlockingWebSocket())

    assert heartbeat_cancelled.is_set()
    assert "Controller connection closed" in caplog.text
    assert "Executor heartbeat failed" not in caplog.text


@pytest.mark.asyncio
async def test_heartbeat_failure_reaches_existing_reconnect_loop_without_duplicate_connections(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = ExecutorRunner(ExecutorConfig(executor_id="remote", controller_token="t"))
    attempts = 0
    active_message_loops = 0
    max_active_message_loops = 0
    sleeps: list[float] = []
    message_started = asyncio.Event()

    async def _connect_and_serve() -> None:
        nonlocal attempts
        attempts += 1
        if attempts == 2:
            runner._running = False
            return
        await runner._run_connection_loops(BlockingWebSocket())

    async def _message_loop(_ws: object) -> None:
        nonlocal active_message_loops, max_active_message_loops
        active_message_loops += 1
        max_active_message_loops = max(max_active_message_loops, active_message_loops)
        message_started.set()
        try:
            await asyncio.Event().wait()
        finally:
            active_message_loops -= 1

    async def _heartbeat_loop(_ws: object) -> None:
        await message_started.wait()
        raise ConnectionError("heartbeat disconnected")

    async def _sleep(delay: float) -> None:
        sleeps.append(delay)

    monkeypatch.setattr(runner, "_connect_and_serve", _connect_and_serve)
    monkeypatch.setattr(runner, "_message_loop", _message_loop)
    monkeypatch.setattr(runner, "_heartbeat_loop", _heartbeat_loop)
    monkeypatch.setattr(asyncio, "sleep", _sleep)

    await runner.run()

    assert attempts == 2
    assert sleeps == [1.0]
    assert active_message_loops == 0
    assert max_active_message_loops == 1


@pytest.mark.asyncio
async def test_runner_logs_cancellation(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    runner = ExecutorRunner(ExecutorConfig(executor_id="remote", controller_token="t"))

    async def _connect_and_serve() -> None:
        raise asyncio.CancelledError

    monkeypatch.setattr(runner, "_connect_and_serve", _connect_and_serve)
    caplog.set_level(logging.INFO, logger="cognis.executor.runner")

    with pytest.raises(asyncio.CancelledError):
        await runner.run()

    assert "Executor runner cancelled, shutting down" in caplog.text


def test_normalize_result_from_string() -> None:
    result = _normalize_result("hello world", 42)
    assert isinstance(result, ToolResult)
    assert result.output == "hello world"
    assert result.duration_ms == 42


def test_normalize_result_from_dict() -> None:
    result = _normalize_result({"key": "value"}, 10)
    assert result.output == '{"key": "value"}'


def test_normalize_result_from_tool_result() -> None:
    result = _normalize_result(ToolResult(output="x", is_error=True), 7)
    assert result.is_error is True
    assert result.duration_ms == 7


@pytest.mark.asyncio
async def test_handle_llm_discover_models_returns_models() -> None:
    runner = ExecutorRunner(ExecutorConfig(executor_id="remote", controller_token="t"))
    ws = DummyWebSocket()

    class FakeInferenceHandler:
        async def discover_models(self, **kwargs: object) -> list[dict[str, object]]:
            assert kwargs == {
                "preset": "ollama",
                "base_url": "http://localhost:11434",
                "api_key": "",
            }
            return [{"model_id": "ollama/ornith:9b", "name": "ornith:9b"}]

    runner._inference_handler = FakeInferenceHandler()  # type: ignore[assignment]

    await runner._handle_llm_discover_models(
        ws,
        "disc-1",
        {"preset": "ollama", "base_url": "http://localhost:11434"},
    )

    assert ws.sent == [
        {
            "jsonrpc": "2.0",
            "id": "disc-1",
            "result": {"models": [{"model_id": "ollama/ornith:9b", "name": "ornith:9b"}]},
        }
    ]


@pytest.mark.asyncio
async def test_handle_configure_filters_tools() -> None:
    runner = ExecutorRunner(ExecutorConfig(executor_id="remote", controller_token="t"))
    ws = DummyWebSocket()

    await runner._handle_configure(
        ws,
        "cfg-1",
        {"enabled_tools": ["read", "glob"], "enabled_tool_groups": [], "config": {}},
    )

    assert runner._configured is True
    # Native tools filtered to read + glob; web tools (web_fetch, web_search)
    # are always added from the default "direct" backend.
    assert {"read", "glob"}.issubset(set(runner._tool_handlers))
    assert "bash" not in runner._tool_handlers  # not in enabled_tools
    caps_tools = ws.sent[-1]["result"]["capabilities"]["tools"]
    assert "read" in caps_tools
    assert "glob" in caps_tools
    assert "environment" in ws.sent[-1]["result"]


@pytest.mark.asyncio
async def test_handle_configure_preserves_platform_and_resource_snapshot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = ExecutorRunner(ExecutorConfig(executor_id="remote", controller_token="t"))
    ws = DummyWebSocket()
    cached = ExecutorResourceSnapshot(
        observed_at="2026-07-13T10:00:00Z",
        os="darwin",
        arch="arm64",
    ).model_dump(mode="json", exclude={"freshness"})
    runner._resource_snapshot = cached
    runner._resource_snapshot_collected_at = runner._started_at
    collect = AsyncMock()
    monkeypatch.setattr(runner._resource_collector, "collect", collect)
    monkeypatch.setattr(
        "cognis.executor.runner._build_platform_payload",
        lambda: {"os": "darwin", "arch": "arm64", "python": "3.12.10"},
    )

    await runner._handle_configure(
        ws,
        "cfg-platform",
        {"enabled_tools": [], "enabled_tool_groups": [], "config": {}},
    )

    metadata = ws.sent[-1]["result"]["runtime_metadata"]
    assert metadata["platform"] == {
        "os": "darwin",
        "arch": "arm64",
        "python": "3.12.10",
    }
    assert metadata["resource_snapshot"]["os"] == "darwin"
    assert metadata["resource_snapshot"]["runtime"]["configured"] is True
    assert metadata["resource_snapshot"]["runtime"]["state"] == "active"
    collect.assert_not_awaited()


@pytest.mark.asyncio
async def test_handle_configure_preserves_background_shell_completion_callback() -> None:
    runner = ExecutorRunner(ExecutorConfig(executor_id="remote", controller_token="t"))
    ws = DummyWebSocket()
    completed: list[dict[str, object]] = []

    async def _background_shell_completed(status: dict[str, object]) -> None:
        completed.append(status)

    runner._background_shell_completion_callback = _background_shell_completed
    set_background_shell_completion_callback(
        runner._runtime_metadata,
        _background_shell_completed,
    )

    await runner._handle_configure(
        ws,
        "cfg-1",
        {"enabled_tools": ["bash"], "enabled_tool_groups": [], "config": {}},
    )

    await runner._handle_tool_execute(
        ws,
        "call-1",
        {
            "call_id": "call-1",
            "tool_name": "bash",
            "arguments": {
                "command": "printf background-done",
                "description": "background completion test",
                "timeout": 1,
                "run_in_background": True,
            },
            "runtime_metadata": _contract_metadata(runner, "bash"),
        },
    )

    for _ in range(20):
        if completed:
            break
        await asyncio.sleep(0.05)

    assert completed
    assert completed[-1]["status"] == "completed"
    assert completed[-1]["description"] == "background completion test"


@pytest.mark.asyncio
async def test_handle_configure_retries_pending_background_shell_completion() -> None:
    runner = ExecutorRunner(ExecutorConfig(executor_id="remote", controller_token="t"))
    ws = DummyWebSocket()
    stale_attempts: list[dict[str, object]] = []
    delivered: list[dict[str, object]] = []

    async def _stale_background_shell_completed(status: dict[str, object]) -> None:
        stale_attempts.append(status)
        raise ConnectionError("websocket closed")

    async def _fresh_background_shell_completed(status: dict[str, object]) -> None:
        delivered.append(status)

    runner._background_shell_completion_callback = _stale_background_shell_completed
    set_background_shell_completion_callback(
        runner._runtime_metadata,
        _stale_background_shell_completed,
    )
    await runner._handle_configure(
        ws,
        "cfg-1",
        {"enabled_tools": ["bash"], "enabled_tool_groups": [], "config": {}},
    )

    await runner._handle_tool_execute(
        ws,
        "call-1",
        {
            "call_id": "call-1",
            "tool_name": "bash",
            "arguments": {
                "command": "printf pending-completion",
                "description": "pending completion retry test",
                "timeout": 1,
                "run_in_background": True,
            },
            "runtime_metadata": _contract_metadata(runner, "bash"),
        },
    )

    for _ in range(20):
        if stale_attempts:
            break
        await asyncio.sleep(0.05)
    assert stale_attempts
    assert delivered == []

    runner._background_shell_completion_callback = _fresh_background_shell_completed
    await runner._handle_configure(
        ws,
        "cfg-2",
        {"enabled_tools": ["bash"], "enabled_tool_groups": [], "config": {}},
    )

    for _ in range(20):
        if delivered:
            break
        await asyncio.sleep(0.05)

    assert delivered
    assert delivered[-1]["status"] == "completed"
    assert delivered[-1]["description"] == "pending completion retry test"


@pytest.mark.asyncio
async def test_handle_configure_exposes_skill_assets_to_materialize_tool(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner = ExecutorRunner(ExecutorConfig(executor_id="remote", controller_token="t"))
    ws = DummyWebSocket()
    monkeypatch.setenv("COGNIS_DATA_DIR", str(tmp_path / "data"))
    target = tmp_path / "data" / "skill_assets" / "custom" / "youtube_transcript.py"

    await runner._handle_configure(
        ws,
        "cfg-1",
        {
            "enabled_tools": ["skill_asset_materialize"],
            "enabled_tool_groups": [],
            "config": {},
            "skill_manifests": [
                {
                    "skill_id": "youtube-transcript",
                    "asset_manifest": [
                        {
                            "filename": "assets/youtube_transcript.py",
                            "asset_id": "sa-script",
                            "content_b64": "cHJpbnQoJ2hpJykK",
                            "content_type": "text/x-python",
                        }
                    ],
                }
            ],
        },
    )

    await runner._handle_tool_execute(
        ws,
        "call-1",
        {
            "call_id": "call-1",
            "tool_name": "skill_asset_materialize",
            "arguments": {
                "skill_id": "youtube-transcript",
                "asset_id": "sa-script",
                "target_path": str(target),
            },
            "runtime_metadata": _contract_metadata(runner, "skill_asset_materialize"),
        },
    )

    result = ws.sent[-1]["result"]
    assert result["is_error"] is False
    assert target.read_text() == "print('hi')\n"
    assert "skill_manifests" not in ws.sent[0]["result"]["runtime_metadata"]


@pytest.mark.asyncio
async def test_handle_tool_list_returns_configured_definitions() -> None:
    runner = ExecutorRunner(ExecutorConfig(executor_id="remote", controller_token="t"))
    ws = DummyWebSocket()
    await runner._handle_configure(ws, "cfg-1", {"enabled_tools": ["read"], "config": {}})
    ws.sent.clear()

    await runner._handle_tool_list(ws, "list-1")

    assert ws.sent[-1]["result"]["tools"][0]["name"] == "read"


@pytest.mark.asyncio
async def test_handle_tool_execute_requires_configuration() -> None:
    runner = ExecutorRunner(ExecutorConfig(executor_id="remote", controller_token="t"))
    ws = DummyWebSocket()

    await runner._handle_tool_execute(
        ws,
        "call-1",
        {
            "call_id": "call-1",
            "tool_name": "read",
            "arguments": {"file_path": "/tmp/file", "offset": 1, "limit": 1},
        },
    )

    assert ws.sent[-1]["result"]["is_error"] is True
    assert "not configured" in ws.sent[-1]["result"]["output"].lower()


@pytest.mark.asyncio
async def test_handle_tool_execute_rejects_controller_executor_contract_skew() -> None:
    runner = ExecutorRunner(ExecutorConfig(executor_id="remote", controller_token="t"))
    ws = DummyWebSocket()
    await runner._handle_configure(ws, "cfg-1", {"enabled_tools": ["read"], "config": {}})
    ws.sent.clear()

    await runner._handle_tool_execute(
        ws,
        "call-1",
        {
            "call_id": "call-1",
            "tool_name": "read",
            "arguments": {"file_path": "/tmp/file", "offset": 1, "limit": 1},
            "runtime_metadata": {"tool_contract_hash": "sha256:stale"},
        },
    )

    result = ws.sent[-1]["result"]
    assert result["is_error"] is True
    assert result["metadata"]["code"] == "tool_contract_mismatch"


@pytest.mark.asyncio
async def test_handle_tool_execute_rejects_missing_contract_hash() -> None:
    runner = ExecutorRunner(ExecutorConfig(executor_id="remote", controller_token="t"))
    ws = DummyWebSocket()
    await runner._handle_configure(ws, "cfg-1", {"enabled_tools": ["read"], "config": {}})
    ws.sent.clear()

    await runner._handle_tool_execute(
        ws,
        "call-1",
        {
            "call_id": "call-1",
            "tool_name": "read",
            "arguments": {"file_path": "/tmp/file", "offset": 1, "limit": 1},
        },
    )

    result = ws.sent[-1]["result"]
    assert result["is_error"] is True
    assert result["metadata"]["code"] == "tool_contract_mismatch"


@pytest.mark.asyncio
async def test_background_handler_closed_websocket_exception_is_consumed(
    caplog: pytest.LogCaptureFixture,
) -> None:
    runner = ExecutorRunner(ExecutorConfig(executor_id="remote", controller_token="t"))
    caplog.set_level(logging.DEBUG, logger="cognis.executor.runner")

    task = runner._create_background_handler_task(
        runner._send_rpc_result(ClosingWebSocket(), "rpc-1", {"ok": True}),
        "shell.background_status",
        msg_id="rpc-1",
    )
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    assert task.done()
    assert "could not reply because websocket closed" in caplog.text


def test_executor_runner_uses_explicit_ping_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    import importlib

    import cognis.executor.runner as runner_module

    original_module = runner_module
    with monkeypatch.context() as env:
        env.setenv("COGNIS_EXECUTOR_WS_PING_INTERVAL_SECONDS", "7")
        env.setenv("COGNIS_EXECUTOR_WS_PING_TIMEOUT_SECONDS", "11")

        reloaded = importlib.reload(runner_module)

        assert reloaded._WS_PING_INTERVAL_SECONDS == 7
        assert reloaded._WS_PING_TIMEOUT_SECONDS == 11

    importlib.reload(original_module)


@pytest.mark.asyncio
async def test_handle_llm_complete_streams_chunks() -> None:
    runner = ExecutorRunner(ExecutorConfig(executor_id="remote", controller_token="t"))
    ws = DummyWebSocket()
    await runner._handle_configure(ws, "cfg-1", {"enabled_tools": [], "config": {}})

    runner._inference_handler = AsyncMock()

    async def _stream_complete(**_: object):
        yield {"content": "Hello", "index": 0}
        yield {"done": True, "usage": {"prompt_tokens": 1}, "finish_reason": "stop"}

    runner._inference_handler.stream_complete = _stream_complete
    ws.sent.clear()

    await runner._handle_llm_complete(
        ws,
        "rpc-1",
        {"request_id": "req-1", "model": "openai/gpt-4o-mini", "messages": []},
    )

    assert ws.sent[0]["result"]["status"] == "streaming"
    assert ws.sent[1]["method"] == "llm.chunk"
    assert ws.sent[2]["method"] == "llm.done"


@pytest.mark.asyncio
async def test_handle_llm_complete_forwards_anthropic_native_events() -> None:
    runner = ExecutorRunner(ExecutorConfig(executor_id="remote", controller_token="t"))
    ws = DummyWebSocket()
    await runner._handle_configure(ws, "cfg-1", {"enabled_tools": [], "config": {}})

    runner._inference_handler = AsyncMock()
    native_events = [
        {
            "type": "content_block_delta",
            "index": 0,
            "delta": {"type": "input_json_delta", "partial_json": '{"query":"docs"}'},
        }
    ]

    async def _stream_complete(**_: object):
        yield {"anthropic_native_events": native_events}
        yield {"done": True, "finish_reason": "pause_turn"}

    runner._inference_handler.stream_complete = _stream_complete
    ws.sent.clear()

    await runner._handle_llm_complete(
        ws,
        "rpc-1",
        {"request_id": "req-1", "model": "claude-opus-4-7", "messages": []},
    )

    assert ws.sent[1]["method"] == "llm.chunk"
    assert ws.sent[1]["params"]["anthropic_native_events"] == native_events
    assert ws.sent[2]["method"] == "llm.done"


@pytest.mark.asyncio
async def test_handle_llm_complete_serializes_model_tool_calls() -> None:
    runner = ExecutorRunner(ExecutorConfig(executor_id="remote", controller_token="t"))
    ws = DummyWebSocket()
    await runner._handle_configure(ws, "cfg-1", {"enabled_tools": [], "config": {}})

    class ToolCall:
        def model_dump(self, **_: object) -> dict[str, object]:
            return {
                "index": 0,
                "id": "call_1",
                "type": "function",
                "function": {"name": "bash", "arguments": '{"command":"pwd"}'},
            }

    runner._inference_handler = AsyncMock()

    async def _stream_complete(**_: object):
        yield {"tool_calls": [ToolCall()], "index": 0}
        yield {"done": True, "usage": {"prompt_tokens": 1}, "finish_reason": "tool_calls"}

    runner._inference_handler.stream_complete = _stream_complete
    ws.sent.clear()

    await runner._handle_llm_complete(
        ws,
        "rpc-1",
        {"request_id": "req-1", "model": "anthropic/claude-opus-4-7", "messages": []},
    )

    assert ws.sent[1]["params"]["tool_calls"][0]["function"]["name"] == "bash"
    assert ws.sent[2]["params"]["finish_reason"] == "tool_calls"


@pytest.mark.asyncio
async def test_handle_llm_transcribe_returns_result() -> None:
    runner = ExecutorRunner(ExecutorConfig(executor_id="remote", controller_token="t"))
    ws = DummyWebSocket()
    await runner._handle_configure(ws, "cfg-1", {"enabled_tools": [], "config": {}})

    runner._inference_handler = AsyncMock()
    runner._inference_handler.transcribe = AsyncMock(
        return_value={"text": "hello", "model": "whisper-1"}
    )
    ws.sent.clear()

    await runner._handle_llm_transcribe(
        ws,
        "rpc-1",
        {
            "audio_base64": b"abc".hex(),
            "audio_encoding": "hex",
            "mime_type": "audio/ogg",
            "filename": "voice.ogg",
            "model": "whisper-1",
            "request_kwargs": {},
        },
    )

    assert ws.sent[-1]["result"]["text"] == "hello"


@pytest.mark.asyncio
async def test_heartbeat_includes_configuration_state() -> None:
    runner = ExecutorRunner(ExecutorConfig(executor_id="remote", controller_token="t"))
    ws = DummyWebSocket()
    task = asyncio.create_task(runner._heartbeat_loop(ws))
    await asyncio.sleep(0)
    runner._running = False
    await task
    assert ws.sent[0]["params"]["configured"] is False


@pytest.mark.asyncio
async def test_executor_cancel_acknowledges_before_shutdown() -> None:
    runner = ExecutorRunner(ExecutorConfig(executor_id="remote", controller_token="t"))
    ws = DummyMessageWebSocket(
        [
            {
                "jsonrpc": "2.0",
                "method": "executor.cancel",
                "id": "cancel-1",
                "params": {"reason": "cancelled"},
            }
        ]
    )

    await runner._handle_configure(ws, "cfg-1", {"enabled_tools": [], "config": {}})
    ws.sent.clear()

    await runner._message_loop(ws)

    assert runner._running is False
    assert ws.sent[-1]["id"] == "cancel-1"
    assert ws.sent[-1]["result"]["status"] == "shutting_down"


@pytest.mark.asyncio
async def test_handle_configure_reports_degraded_when_some_mcp_servers_fail(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = ExecutorRunner(ExecutorConfig(executor_id="remote", controller_token="t"))
    ws = DummyWebSocket()

    async def _prepare_mcp_runtime(
        servers: list[object], secrets: dict[str, str]
    ) -> tuple[dict[str, object], list[ToolDefinition], list[dict[str, object]], list[str]]:
        del servers, secrets
        return (
            {},
            [
                ToolDefinition(
                    name="mcp_todoist__list_tasks",
                    description="List tasks",
                    parameters={"type": "object", "properties": {}},
                    source=ToolSource(
                        type="local_mcp", server_name="todoist", raw_tool_name="list_tasks"
                    ),
                    category="mcp",
                )
            ],
            [
                {"name": "todoist", "status": "ready", "phase": "ready", "tool_count": 1},
                {
                    "name": "github",
                    "status": "failed",
                    "phase": "initialize",
                    "error_class": "timeout",
                    "timed_out": True,
                    "message": "github startup timed out",
                    "stderr_summary": "npm error: missing token",
                },
            ],
            ["MCP server github failed during initialize."],
        )

    monkeypatch.setattr(runner, "_prepare_mcp_runtime", _prepare_mcp_runtime)

    await runner._handle_configure(
        ws,
        "cfg-1",
        {
            "enabled_tools": ["read"],
            "mcp_servers": [
                {
                    "name": "todoist",
                    "transport": "stdio",
                    "command": "npx",
                    "args": ["-y", "todoist"],
                }
            ],
            "config": {},
        },
    )

    assert runner._configured is True
    assert runner._runtime_state == "degraded"
    assert ws.sent[-1]["result"]["runtime_state"] == "degraded"
    assert ws.sent[-1]["result"]["runtime_metadata"]["warnings"]
    degraded_server = ws.sent[-1]["result"]["runtime_metadata"]["mcp_servers"][1]
    assert degraded_server["message"] == "github startup timed out"
    assert degraded_server["stderr_summary"] == "npm error: missing token"


@pytest.mark.asyncio
async def test_prepare_mcp_runtime_suppresses_failed_client_cleanup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = ExecutorRunner(ExecutorConfig(executor_id="remote", controller_token="t"))

    class _BrokenClient:
        async def connect(self) -> None:
            raise MCPClientError(
                "googleworkspace",
                "initialize",
                "redirect failed",
                error_class="httpstatuserror",
            )

        async def list_tools(self) -> list[dict[str, object]]:
            return []

        async def call_tool(self, tool_name: str, arguments: dict[str, object]) -> object:
            del tool_name, arguments
            return {}

        async def close(self, *, suppress_cancelled: bool = False) -> None:
            del suppress_cancelled
            raise BaseExceptionGroup("cleanup failed", [RuntimeError("cleanup")])

    monkeypatch.setattr("cognis.executor.runner.build_mcp_client", lambda *_: _BrokenClient())

    clients, discovered, statuses, warnings = await runner._prepare_mcp_runtime(
        [
            MCPServerConfig(
                name="googleworkspace",
                transport="streamable_http",
                url="http://mcp-gws.openwebui.svc.cluster.local/mcp/",
            )
        ],
        {},
    )

    assert clients == {}
    assert discovered == []
    assert warnings == ["MCP server googleworkspace failed during initialize."]
    assert statuses[0]["status"] == "failed"
    assert statuses[0]["message"] == "redirect failed"


@pytest.mark.asyncio
async def test_prepare_mcp_runtime_reports_authorization_required_warning(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = ExecutorRunner(ExecutorConfig(executor_id="remote", controller_token="t"))

    class _UnauthorizedClient:
        async def connect(self) -> None:
            raise MCPClientError(
                "mfg-portal",
                "initialize",
                "HTTP 401 Unauthorized",
                error_class="httpstatuserror",
                status_code=401,
                auth_error="authorization_required",
                www_authenticate='Bearer resource_metadata="https://mfg.example/.well-known/oauth-protected-resource/mcp"',
                authorization_challenge={
                    "resource_metadata": (
                        "https://mfg.example/.well-known/oauth-protected-resource/mcp"
                    ),
                    "scope": "tools.read",
                },
            )

        async def list_tools(self) -> list[dict[str, object]]:
            return []

        async def call_tool(self, tool_name: str, arguments: dict[str, object]) -> object:
            del tool_name, arguments
            return {}

        async def close(self, *, suppress_cancelled: bool = False) -> None:
            del suppress_cancelled
            return None

    monkeypatch.setattr("cognis.executor.runner.build_mcp_client", lambda *_: _UnauthorizedClient())

    clients, discovered, statuses, warnings = await runner._prepare_mcp_runtime(
        [
            MCPServerConfig(
                name="mfg-portal",
                transport="streamable_http",
                url="https://mfg.example/mcp",
            )
        ],
        {},
    )

    assert clients == {}
    assert discovered == []
    assert warnings == ["MCP server mfg-portal requires authorization during initialize."]
    assert statuses[0]["authorization_required"] is True
    assert statuses[0]["status_code"] == 401
    assert statuses[0]["auth_error"] == "authorization_required"
    assert statuses[0]["authorization_challenge"] == {
        "resource_metadata": "https://mfg.example/.well-known/oauth-protected-resource/mcp",
        "scope": "tools.read",
    }


@pytest.mark.asyncio
async def test_prepare_mcp_runtime_isolates_transport_base_exception_group(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = ExecutorRunner(ExecutorConfig(executor_id="remote", controller_token="t"))
    closed: list[bool] = []

    class _BrokenClient:
        async def connect(self) -> None:
            raise BaseExceptionGroup("stream failure", [RuntimeError("SSE stream died")])

        async def list_tools(self) -> list[dict[str, object]]:
            return []

        async def call_tool(self, tool_name: str, arguments: dict[str, object]) -> object:
            del tool_name, arguments
            return {}

        async def close(self, *, suppress_cancelled: bool = False) -> None:
            del suppress_cancelled
            closed.append(True)

    monkeypatch.setattr("cognis.executor.runner.build_mcp_client", lambda *_: _BrokenClient())

    clients, discovered, statuses, warnings = await runner._prepare_mcp_runtime(
        [
            MCPServerConfig(
                name="googleworkspace",
                transport="streamable_http",
                url="http://mcp-gws.openwebui.svc.cluster.local/mcp/",
            )
        ],
        {},
    )

    assert clients == {}
    assert discovered == []
    assert warnings == ["MCP server googleworkspace failed to initialize."]
    assert closed == [True]
    assert statuses == [
        {
            "server_id": None,
            "name": "googleworkspace",
            "phase": "initialize",
            "status": "failed",
            "error_class": "exceptiongroup",
            "timed_out": False,
            "message": (
                "ExceptionGroup: stream failure (1 sub-exception); RuntimeError: SSE stream died"
            ),
        }
    ]


@pytest.mark.asyncio
async def test_prepare_mcp_runtime_reraises_transport_cancellation_group(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = ExecutorRunner(ExecutorConfig(executor_id="remote", controller_token="t"))

    class _CancellingClient:
        async def connect(self) -> None:
            raise BaseExceptionGroup("cancelled", [asyncio.CancelledError()])

        async def list_tools(self) -> list[dict[str, object]]:
            return []

        async def call_tool(self, tool_name: str, arguments: dict[str, object]) -> object:
            del tool_name, arguments
            return {}

        async def close(self, *, suppress_cancelled: bool = False) -> None:
            del suppress_cancelled

    monkeypatch.setattr("cognis.executor.runner.build_mcp_client", lambda *_: _CancellingClient())

    with pytest.raises(BaseExceptionGroup):
        await runner._prepare_mcp_runtime(
            [
                MCPServerConfig(
                    name="googleworkspace",
                    transport="streamable_http",
                    url="http://mcp-gws.openwebui.svc.cluster.local/mcp/",
                )
            ],
            {},
        )


@pytest.mark.asyncio
async def test_handle_tool_execute_allows_degraded_runtime() -> None:
    runner = ExecutorRunner(ExecutorConfig(executor_id="remote", controller_token="t"))
    ws = DummyWebSocket()
    await runner._handle_configure(ws, "cfg-1", {"enabled_tools": ["read"], "config": {}})
    runner._runtime_state = "degraded"

    async def _handler(_: dict[str, object], context: object) -> ToolResult:
        del context
        return ToolResult(output="ok", is_error=False, metadata={"analysis": "ok"})

    runner._tool_handlers["read"] = _handler
    ws.sent.clear()

    await runner._handle_tool_execute(
        ws,
        "tool-1",
        {
            "call_id": "call-1",
            "tool_name": "read",
            "arguments": {"file_path": "/tmp/file", "offset": 1, "limit": 1},
            "runtime_metadata": _contract_metadata(runner, "read"),
        },
    )

    assert ws.sent[-1]["result"]["is_error"] is False
    assert ws.sent[-1]["result"]["output"] == "ok"
    assert ws.sent[-1]["result"]["metadata"] == {"analysis": "ok"}


@pytest.mark.asyncio
async def test_failed_reconfigure_preserves_previous_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = ExecutorRunner(ExecutorConfig(executor_id="remote", controller_token="t"))
    ws = DummyWebSocket()
    await runner._handle_configure(ws, "cfg-1", {"enabled_tools": ["read"], "config": {}})

    async def _broken_prepare(
        servers: list[object], secrets: dict[str, str]
    ) -> tuple[dict[str, object], list[ToolDefinition], list[dict[str, object]], list[str]]:
        del servers, secrets
        raise RuntimeError("boom")

    monkeypatch.setattr(runner, "_prepare_mcp_runtime", _broken_prepare)
    ws.sent.clear()

    await runner._handle_configure(ws, "cfg-2", {"enabled_tools": ["glob"], "config": {}})

    assert runner._configured is True
    assert "read" in runner._tool_handlers
    assert "glob" not in runner._tool_handlers
    assert ws.sent[-1]["error"]["message"].startswith("Executor configure failed")


@pytest.mark.asyncio
async def test_runner_shutdown_cleans_browser_manager() -> None:
    runner = ExecutorRunner(ExecutorConfig(executor_id="remote", controller_token="t"))
    cleaned: list[str] = []

    class _BrowserManager:
        async def cleanup(self) -> None:
            cleaned.append("cleanup")

    async def _fake_connect_and_serve() -> None:
        runner._runtime_metadata["browser_manager"] = _BrowserManager()
        runner._running = False

    runner._connect_and_serve = _fake_connect_and_serve  # type: ignore[method-assign]

    await runner.run()

    assert cleaned == ["cleanup"]


@pytest.mark.asyncio
async def test_runner_shutdown_suppresses_cancelled_cleanup() -> None:
    runner = ExecutorRunner(ExecutorConfig(executor_id="remote", controller_token="t"))

    class _BrowserManager:
        async def cleanup(self) -> None:
            raise asyncio.CancelledError()

    class _ChannelHandler:
        async def stop_all(self) -> None:
            raise asyncio.CancelledError()

    class _InferenceHandler:
        async def close(self) -> None:
            raise asyncio.CancelledError()

    async def _fake_connect_and_serve() -> None:
        runner._runtime_metadata["browser_manager"] = _BrowserManager()
        runner._channel_handler = _ChannelHandler()
        runner._inference_handler = _InferenceHandler()
        runner._running = False

    async def _fake_close_mcp_clients() -> None:
        raise asyncio.CancelledError()

    runner._connect_and_serve = _fake_connect_and_serve  # type: ignore[method-assign]
    runner._close_mcp_clients = _fake_close_mcp_clients  # type: ignore[method-assign]

    await runner.run()


@pytest.mark.asyncio
async def test_runner_propagates_external_cancellation() -> None:
    runner = ExecutorRunner(ExecutorConfig(executor_id="remote", controller_token="t"))

    async def _fake_connect_and_serve() -> None:
        await asyncio.Future()

    runner._connect_and_serve = _fake_connect_and_serve  # type: ignore[method-assign]

    task = asyncio.create_task(runner.run())
    await asyncio.sleep(0)
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.asyncio
async def test_handle_configure_creates_lsp_manager() -> None:
    runner = ExecutorRunner(ExecutorConfig(executor_id="remote", controller_token="t"))
    ws = DummyWebSocket()

    await runner._handle_configure(
        ws,
        "cfg-1",
        {"enabled_tools": ["read"], "config": {"lsp_enabled": True}},
    )

    assert runner._runtime_metadata.get(LSP_MANAGER_KEY) is not None
    caps = ws.sent[-1]["result"]["runtime_metadata"]["configure_capabilities"]
    assert LSP_STATUS_CAPABILITY in caps


@pytest.mark.asyncio
async def test_handle_lsp_status_returns_disabled_when_manager_missing() -> None:
    runner = ExecutorRunner(ExecutorConfig(executor_id="remote", controller_token="t"))
    ws = DummyWebSocket()
    await runner._handle_configure(
        ws,
        "cfg-1",
        {"enabled_tools": [], "config": {"lsp_enabled": False}},
    )
    ws.sent.clear()

    await runner._handle_lsp_status(ws, "lsp-1", {})

    assert ws.sent[-1]["result"]["state"] == "disabled"
    assert ws.sent[-1]["result"]["enabled"] is False


@pytest.mark.asyncio
async def test_tool_execute_passes_execution_scope_id() -> None:
    runner = ExecutorRunner(ExecutorConfig(executor_id="remote", controller_token="t"))
    ws = DummyWebSocket()
    await runner._handle_configure(ws, "cfg-1", {"enabled_tools": ["read"], "config": {}})

    seen_scope: dict[str, str | None] = {}

    async def _handler(_: dict[str, object], context: object) -> dict[str, object]:
        seen_scope["value"] = getattr(context, "execution_scope_id", None)
        return {"output": "ok", "is_error": False}

    runner._tool_handlers["read"] = _handler
    ws.sent.clear()

    await runner._handle_tool_execute(
        ws,
        "tool-1",
        {
            "call_id": "call-1",
            "tool_name": "read",
            "arguments": {"file_path": "/tmp/file", "offset": 1, "limit": 1},
            "execution_scope_id": "session-123",
            "runtime_metadata": _contract_metadata(runner, "read"),
        },
    )

    assert seen_scope["value"] == "session-123"


@pytest.mark.asyncio
async def test_handle_configure_degrades_when_lsp_manager_init_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = ExecutorRunner(ExecutorConfig(executor_id="remote", controller_token="t"))
    ws = DummyWebSocket()

    def _broken_build_lsp_manager(_: object) -> object:
        raise RuntimeError("boom")

    monkeypatch.setattr("cognis.executor.runner.build_lsp_manager", _broken_build_lsp_manager)

    await runner._handle_configure(
        ws,
        "cfg-1",
        {"enabled_tools": ["read"], "config": {"lsp_enabled": True}},
    )

    assert runner._configured is True
    assert runner._runtime_metadata.get(LSP_MANAGER_KEY) is None
    assert runner._runtime_metadata["lsp_init_failed"] is True

    ws.sent.clear()
    await runner._handle_lsp_status(ws, "lsp-2", {})
    assert ws.sent[-1]["result"]["state"] == "unavailable"


def test_executor_main_suppresses_cancelled_error(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    workdir = tmp_path / "workdir"
    launch = tmp_path / "launch"
    workdir.mkdir()
    launch.mkdir()
    monkeypatch.chdir(launch)

    class _Runner:
        def __init__(self, config: object) -> None:
            self.config = config

        async def run(self) -> None:
            raise asyncio.CancelledError()

    monkeypatch.setattr("cognis.executor.runner.ExecutorRunner", _Runner)
    monkeypatch.setenv("COGNIS_CONTROLLER_URL", "ws://localhost:8080/api/executor/ws")
    monkeypatch.setenv("COGNIS_EXECUTOR_TOKEN", "token")
    monkeypatch.setenv("COGNIS_EXECUTOR_WORKDIR", str(workdir))
    monkeypatch.setattr(sys, "argv", ["cognis-executor"])
    with open(os.devnull) as devnull:
        monkeypatch.setattr(sys, "stdin", devnull)
        executor_main.main()


def test_executor_main_defaults_workdir_to_home(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    launch = tmp_path / "launch"
    home.mkdir()
    launch.mkdir()
    monkeypatch.chdir(launch)
    monkeypatch.delenv("COGNIS_EXECUTOR_WORKDIR", raising=False)
    monkeypatch.setattr(executor_main.Path, "home", staticmethod(lambda: home))

    class _Runner:
        def __init__(self, config: object) -> None:
            self.config = config

        async def run(self) -> None:
            assert Path.cwd() == home
            raise asyncio.CancelledError()

    monkeypatch.setattr("cognis.executor.runner.ExecutorRunner", _Runner)
    monkeypatch.setenv("COGNIS_CONTROLLER_URL", "ws://localhost:8080/api/executor/ws")
    monkeypatch.setenv("COGNIS_EXECUTOR_TOKEN", "token")
    monkeypatch.setattr(sys, "argv", ["cognis-executor"])
    with open(os.devnull) as devnull:
        monkeypatch.setattr(sys, "stdin", devnull)
        executor_main.main()


def test_executor_main_uses_env_workdir(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    env_workdir = tmp_path / "env-workdir"
    launch = tmp_path / "launch"
    env_workdir.mkdir()
    launch.mkdir()
    monkeypatch.chdir(launch)
    monkeypatch.setenv("COGNIS_EXECUTOR_WORKDIR", str(env_workdir))

    class _Runner:
        def __init__(self, config: object) -> None:
            self.config = config

        async def run(self) -> None:
            assert Path.cwd() == env_workdir
            raise asyncio.CancelledError()

    monkeypatch.setattr("cognis.executor.runner.ExecutorRunner", _Runner)
    monkeypatch.setenv("COGNIS_CONTROLLER_URL", "ws://localhost:8080/api/executor/ws")
    monkeypatch.setenv("COGNIS_EXECUTOR_TOKEN", "token")
    monkeypatch.setattr(sys, "argv", ["cognis-executor"])
    with open(os.devnull) as devnull:
        monkeypatch.setattr(sys, "stdin", devnull)
        executor_main.main()


def test_executor_main_cli_workdir_overrides_env(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    env_workdir = tmp_path / "env-workdir"
    cli_workdir = tmp_path / "cli-workdir"
    launch = tmp_path / "launch"
    env_workdir.mkdir()
    cli_workdir.mkdir()
    launch.mkdir()
    monkeypatch.chdir(launch)
    monkeypatch.setenv("COGNIS_EXECUTOR_WORKDIR", str(env_workdir))

    class _Runner:
        def __init__(self, config: object) -> None:
            self.config = config

        async def run(self) -> None:
            assert Path.cwd() == cli_workdir
            raise asyncio.CancelledError()

    monkeypatch.setattr("cognis.executor.runner.ExecutorRunner", _Runner)
    monkeypatch.setenv("COGNIS_CONTROLLER_URL", "ws://localhost:8080/api/executor/ws")
    monkeypatch.setenv("COGNIS_EXECUTOR_TOKEN", "token")
    monkeypatch.setattr(
        sys,
        "argv",
        ["cognis-executor", "--workdir", str(cli_workdir)],
    )
    with open(os.devnull) as devnull:
        monkeypatch.setattr(sys, "stdin", devnull)
        executor_main.main()


def test_executor_main_rejects_invalid_workdir(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    invalid = tmp_path / "missing"
    monkeypatch.setenv("COGNIS_CONTROLLER_URL", "ws://localhost:8080/api/executor/ws")
    monkeypatch.setenv("COGNIS_EXECUTOR_TOKEN", "token")
    monkeypatch.setattr(sys, "argv", ["cognis-executor", "--workdir", str(invalid)])
    with open(os.devnull) as devnull:
        monkeypatch.setattr(sys, "stdin", devnull)
        with pytest.raises(SystemExit) as exc_info:
            executor_main.main()

    assert exc_info.value.code == 1
    assert "Executor working directory does not exist" in capsys.readouterr().err


def test_cli_executor_workdir_defaults_to_home(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.delenv("COGNIS_EXECUTOR_WORKDIR", raising=False)
    monkeypatch.setattr(cli_executor.Path, "home", staticmethod(lambda: home))

    assert cli_executor._resolve_workdir(None) == str(home)


# ---------------------------------------------------------------------------
# Regression tests: reconfigure shutdown caused by anyio cross-task teardown
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_close_clients_swallows_base_exception_group() -> None:
    """_close_clients must not let BaseExceptionGroup escape to the caller.

    When anyio detects that exit stacks entered in one task are being
    aclose()d from a different task, it raises BaseExceptionGroup.  The
    runner's except Exception: guard does NOT catch BaseExceptionGroup
    (it is a BaseException subclass, not Exception), so we must handle it
    explicitly to prevent the executor from shutting down on reconfigure.
    """
    runner = ExecutorRunner(ExecutorConfig(executor_id="remote", controller_token="t"))
    closed: list[str] = []

    class _FakeClient:
        def __init__(self, name: str) -> None:
            self.name = name

        async def close(self, *, suppress_cancelled: bool = False) -> None:
            if self.name == "bomb":
                raise BaseExceptionGroup("anyio-cancel", [RuntimeError("cross-task scope")])
            closed.append(self.name)

    clients = {"ok": _FakeClient("ok"), "bomb": _FakeClient("bomb"), "ok2": _FakeClient("ok2")}

    # Must not raise and must continue iterating past the "bomb" client.
    await runner._close_clients(clients, suppress_cancelled=True)  # type: ignore[arg-type]

    assert "ok" in closed
    assert "ok2" in closed
    assert "bomb" not in closed


@pytest.mark.asyncio
async def test_close_clients_reraises_cancelled_when_not_suppressed() -> None:
    """_close_clients propagates CancelledError when suppress_cancelled=False."""
    runner = ExecutorRunner(ExecutorConfig(executor_id="remote", controller_token="t"))

    class _CancellingClient:
        async def close(self, *, suppress_cancelled: bool = False) -> None:
            raise asyncio.CancelledError()

    clients = {"c": _CancellingClient()}

    with pytest.raises(asyncio.CancelledError):
        await runner._close_clients(clients, suppress_cancelled=False)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_mcp_prepare_runs_in_configure_task(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """MCP transports must be entered in the same task that later closes them.

    asyncio.wait_for(coro) wraps ``coro`` in a child task.  The MCP SDK's anyio
    transports bind cancel scopes to the entering task, so configure uses
    asyncio.timeout() instead to keep transport enter/exit in the message-loop
    task.
    """
    runner = ExecutorRunner(ExecutorConfig(executor_id="remote", controller_token="t"))
    ws = DummyWebSocket()
    configure_task = asyncio.current_task()
    seen_tasks: list[asyncio.Task[object] | None] = []

    class _RecordingClient:
        async def connect(self) -> None:
            seen_tasks.append(asyncio.current_task())

        async def list_tools(self) -> list[dict[str, object]]:
            seen_tasks.append(asyncio.current_task())
            return []

        async def call_tool(self, tool_name: str, arguments: dict[str, object]) -> object:
            del tool_name, arguments
            return {}

        async def close(self, *, suppress_cancelled: bool = False) -> None:
            del suppress_cancelled
            seen_tasks.append(asyncio.current_task())

    monkeypatch.setattr("cognis.executor.runner.build_mcp_client", lambda *_: _RecordingClient())

    await runner._handle_configure(
        ws,
        "cfg-1",
        {
            "enabled_tools": ["read"],
            "mcp_servers": [
                {
                    "name": "recording",
                    "transport": "stdio",
                    "command": "recording",
                }
            ],
            "config": {},
        },
    )

    await runner._close_mcp_clients()

    assert seen_tasks
    assert all(task is configure_task for task in seen_tasks)


@pytest.mark.asyncio
async def test_reconfigure_closes_stale_clients_inline() -> None:
    """Reconfigure closes stale MCP clients in the configure/message-loop task."""
    runner = ExecutorRunner(ExecutorConfig(executor_id="remote", controller_token="t"))
    ws = DummyWebSocket()
    configure_task = asyncio.current_task()

    # First configure – establishes v1 state.
    await runner._handle_configure(ws, "cfg-1", {"enabled_tools": ["read"], "config": {}})
    assert runner._config_version == 1
    ws.sent.clear()

    closed_in_task: list[asyncio.Task[object] | None] = []

    class _StaleClient:
        async def close(self, *, suppress_cancelled: bool = False) -> None:
            del suppress_cancelled
            closed_in_task.append(asyncio.current_task())

    runner._mcp_clients = {"stale": _StaleClient()}  # type: ignore[assignment]

    # Second configure (reconfigure) – must succeed and NOT shut the runner down.
    await runner._handle_configure(ws, "cfg-2", {"enabled_tools": ["glob"], "config": {}})

    assert runner._config_version == 2
    assert runner._configured is True
    assert runner._running is True  # critical: runner must not exit
    assert ws.sent[-1]["result"]["applied_version"] == 2
    assert closed_in_task == [configure_task]


@pytest.mark.asyncio
async def test_reconfigure_retains_manager_after_permanent_cleanup_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = ExecutorRunner(ExecutorConfig(executor_id="remote", controller_token="t"))
    ws = DummyWebSocket()
    await runner._handle_configure(
        ws,
        "cfg-1",
        {
            "enabled_tools": [],
            "config": {"browser": {"enabled": True}},
        },
    )
    old_manager = runner._runtime_metadata[BROWSER_MANAGER_KEY]
    assert isinstance(old_manager, BrowserManager)
    old_manager._reserved_profile_ids["profile"] = None  # noqa: SLF001

    async def _permanent_teardown_failure() -> list[BaseException]:
        return [RuntimeError("forced teardown failed")]

    monkeypatch.setattr(old_manager, "_force_runtime_teardown", _permanent_teardown_failure)

    await runner._handle_configure(
        ws,
        "cfg-2",
        {
            "enabled_tools": [],
            "config": {"browser": {"enabled": True}},
        },
    )

    assert old_manager is not runner._runtime_metadata[BROWSER_MANAGER_KEY]
    assert old_manager in runner._browser_cleanup_retainer.managers  # noqa: SLF001
    assert old_manager._reserved_profile_ids == {"profile": None}  # noqa: SLF001
    tasks = list(runner._browser_cleanup_retainer._tasks.values())  # noqa: SLF001
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)


@pytest.mark.asyncio
async def test_cancelled_reconfigure_restores_old_manager_and_retains_staged_manager(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = ExecutorRunner(ExecutorConfig(executor_id="remote", controller_token="t"))
    ws = DummyWebSocket()
    await runner._handle_configure(
        ws,
        "cfg-1",
        {
            "enabled_tools": [],
            "config": {"browser": {"enabled": True}},
        },
    )
    old_manager = runner._runtime_metadata[BROWSER_MANAGER_KEY]
    staged_manager = BrowserManager()
    staged_manager._reserved_profile_ids["staged-profile"] = None  # noqa: SLF001
    staged_client_closed = False
    staged_lsp_manager = object()
    cleaned_lsp_managers: list[object] = []

    class _StagedClient:
        async def close(self, *, suppress_cancelled: bool = False) -> None:
            nonlocal staged_client_closed
            del suppress_cancelled
            staged_client_closed = True

    async def _permanent_teardown_failure() -> list[BaseException]:
        return [RuntimeError("forced teardown failed")]

    monkeypatch.setattr(
        staged_manager,
        "_force_runtime_teardown",
        _permanent_teardown_failure,
    )
    monkeypatch.setattr(
        "cognis.executor.runner._build_browser_manager",
        lambda _: staged_manager,
    )
    monkeypatch.setattr(
        runner,
        "_prepare_mcp_runtime",
        lambda *_: asyncio.sleep(0, result=({"staged": _StagedClient()}, [], [], [])),
    )
    monkeypatch.setattr(
        "cognis.executor.runner.build_lsp_manager",
        lambda _: staged_lsp_manager,
    )

    async def _cleanup_lsp_manager(manager: object, **_: object) -> None:
        cleaned_lsp_managers.append(manager)

    monkeypatch.setattr(
        "cognis.executor.runner.cleanup_lsp_manager",
        _cleanup_lsp_manager,
    )

    async def _cancel_configure(*_: object) -> None:
        raise asyncio.CancelledError()

    monkeypatch.setattr(runner, "_register_skill_handlers", _cancel_configure)

    with pytest.raises(asyncio.CancelledError):
        await runner._handle_configure(
            ws,
            "cfg-2",
            {
                "enabled_tools": [],
                "config": {"browser": {"enabled": True}},
            },
        )

    assert runner._runtime_metadata[BROWSER_MANAGER_KEY] is old_manager
    assert staged_manager in runner._browser_cleanup_retainer.managers  # noqa: SLF001
    assert staged_manager._reserved_profile_ids == {"staged-profile": None}  # noqa: SLF001
    assert staged_client_closed is True
    assert cleaned_lsp_managers == [staged_lsp_manager]
    tasks = list(runner._browser_cleanup_retainer._tasks.values())  # noqa: SLF001
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)


@pytest.mark.asyncio
async def test_runner_shutdown_drains_retainer_without_current_browser_manager(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = ExecutorRunner(ExecutorConfig(executor_id="remote", controller_token="t"))
    runner._running = False
    manager = BrowserManager()
    cleanup_started = asyncio.Event()
    release_cleanup = asyncio.Event()

    async def _cleanup() -> None:
        cleanup_started.set()
        await release_cleanup.wait()

    monkeypatch.setattr(manager, "cleanup", _cleanup)
    runner._browser_cleanup_retainer.retain(manager)  # noqa: SLF001
    await cleanup_started.wait()
    assert BROWSER_MANAGER_KEY not in runner._runtime_metadata  # noqa: SLF001

    run_task = asyncio.create_task(runner.run())
    await asyncio.sleep(0)
    assert not run_task.done()

    release_cleanup.set()
    await run_task

    assert runner._browser_cleanup_retainer.managers == ()  # noqa: SLF001


@pytest.mark.asyncio
async def test_cancel_after_old_cleanup_point_restores_usable_previous_manager(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = ExecutorRunner(ExecutorConfig(executor_id="remote", controller_token="t"))
    ws = DummyWebSocket()
    await runner._handle_configure(
        ws,
        "cfg-1",
        {
            "enabled_tools": [],
            "config": {"browser": {"enabled": True}},
        },
    )
    old_manager = runner._runtime_metadata[BROWSER_MANAGER_KEY]
    assert isinstance(old_manager, BrowserManager)
    close_calls = 0

    class _Context:
        async def close(self) -> None:
            nonlocal close_calls
            close_calls += 1

    old_manager._sessions["existing"] = BrowserSession(  # noqa: SLF001
        session_id="existing",
        context=_Context(),
        page=SimpleNamespace(url="https://example.com"),
    )
    staged_manager = BrowserManager()
    monkeypatch.setattr(
        "cognis.executor.runner._build_browser_manager",
        lambda _: staged_manager,
    )
    monkeypatch.setattr(
        "cognis.executor.runner.build_lsp_manager",
        lambda _: object(),
    )

    async def _cancel_during_previous_lsp_cleanup(*_: object, **__: object) -> None:
        raise asyncio.CancelledError()

    monkeypatch.setattr(
        "cognis.executor.runner.cleanup_lsp_manager",
        _cancel_during_previous_lsp_cleanup,
    )

    with pytest.raises(asyncio.CancelledError):
        await runner._handle_configure(
            ws,
            "cfg-2",
            {
                "enabled_tools": [],
                "config": {"browser": {"enabled": True}},
            },
        )

    assert runner._runtime_metadata[BROWSER_MANAGER_KEY] is old_manager
    assert old_manager not in runner._browser_cleanup_retainer.managers  # noqa: SLF001
    assert "existing" in old_manager._sessions  # noqa: SLF001
    assert close_calls == 0
    tasks = list(runner._browser_cleanup_retainer._tasks.values())  # noqa: SLF001
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
