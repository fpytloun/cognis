"""Unit tests for the executor runner module."""

from __future__ import annotations

import asyncio
import json
import os
import sys
from unittest.mock import AsyncMock

import pytest

from cognis.executor import __main__ as executor_main
from cognis.executor.runner import ExecutorRunner, _normalize_result
from cognis.models.tool import ExecutorConfig, ToolDefinition, ToolResult, ToolSource
from cognis.tools.executor.lsp import LSP_MANAGER_KEY, LSP_STATUS_CAPABILITY


class DummyWebSocket:
    def __init__(self) -> None:
        self.sent: list[dict] = []

    async def send(self, raw: str) -> None:
        self.sent.append(json.loads(raw))


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
        {"call_id": "call-1", "tool_name": "read", "arguments": {}},
    )

    assert ws.sent[-1]["result"]["is_error"] is True
    assert "not configured" in ws.sent[-1]["result"]["output"].lower()


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
        {"call_id": "call-1", "tool_name": "read", "arguments": {}},
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
            "arguments": {},
            "execution_scope_id": "session-123",
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


def test_executor_main_suppresses_cancelled_error(monkeypatch: pytest.MonkeyPatch) -> None:
    class _Runner:
        def __init__(self, config: object) -> None:
            self.config = config

        async def run(self) -> None:
            raise asyncio.CancelledError()

    monkeypatch.setattr("cognis.executor.runner.ExecutorRunner", _Runner)
    monkeypatch.setenv("COGNIS_CONTROLLER_URL", "ws://localhost:8080/api/executor/ws")
    monkeypatch.setenv("COGNIS_EXECUTOR_TOKEN", "token")
    monkeypatch.setattr(sys, "argv", ["cognis-executor"])
    with open(os.devnull) as devnull:
        monkeypatch.setattr(sys, "stdin", devnull)
        executor_main.main()
