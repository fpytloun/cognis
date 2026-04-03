"""Unit tests for the executor runner module."""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock

import pytest

from cognis.executor.runner import ExecutorRunner, _normalize_result
from cognis.models.tool import ExecutorConfig, ToolResult


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
    assert set(runner._tool_handlers) == {"read", "glob"}
    assert ws.sent[-1]["result"]["capabilities"]["tools"] == ["read", "glob"]


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
async def test_heartbeat_includes_configuration_state() -> None:
    runner = ExecutorRunner(ExecutorConfig(executor_id="remote", controller_token="t"))
    ws = DummyWebSocket()
    task = asyncio.create_task(runner._heartbeat_loop(ws))
    await asyncio.sleep(0)
    runner._running = False
    await task
    assert ws.sent[0]["params"]["configured"] is False
