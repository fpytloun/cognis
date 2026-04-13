"""Unit tests for the executor runner module."""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock

import pytest

from cognis.executor.runner import ExecutorRunner, _normalize_result
from cognis.models.tool import ExecutorConfig, ToolDefinition, ToolResult, ToolSource
from cognis.tools.executor.lsp import LSP_MANAGER_KEY, LSP_STATUS_CAPABILITY


class DummyWebSocket:
    def __init__(self) -> None:
        self.sent: list[dict] = []

    async def send(self, raw: str) -> None:
        self.sent.append(json.loads(raw))


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
    assert "message" not in ws.sent[-1]["result"]["runtime_metadata"]["mcp_servers"][1]


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
