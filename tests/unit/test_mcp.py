from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from cognis.models.tool import MCPServerConfig, ToolSource, sanitize_mcp_tool_name
from cognis.tools import mcp as mcp_module
from cognis.tools.mcp import (
    MCPClientError,
    StdioMCPClient,
    StreamableHTTPMCPClient,
    _normalize_call_result,
    _safe_message,
    _SessionMCPClient,
    _strip_empty_optionals,
    mcp_tools_to_definitions,
    normalize_streamable_http_url,
    runtime_mcp_server_key,
)


def _server_script() -> str:
    # The MCP SDK uses newline-delimited JSON (one JSON object per line),
    # not Content-Length framing.
    return """
from __future__ import annotations
import json
import sys


def read_message() -> dict:
    line = sys.stdin.readline()
    if not line:
        raise EOFError
    return json.loads(line.strip())


def write_message(payload: dict) -> None:
    sys.stdout.write(json.dumps(payload) + "\\n")
    sys.stdout.flush()


while True:
    try:
        request = read_message()
    except EOFError:
        break
    # Skip notifications (no "id" field) — e.g. notifications/initialized
    if "id" not in request:
        continue
    method = request["method"]
    if method == "initialize":
        write_message({"jsonrpc": "2.0", "id": request["id"], "result": {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "serverInfo": {"name": "test-server", "version": "0.1.0"},
        }})
    elif method == "tools/list":
        write_message(
            {
                "jsonrpc": "2.0",
                "id": request["id"],
                "result": {
                    "tools": [
                        {
                            "name": "inspect",
                            "description": "Inspect something",
                            "inputSchema": {"type": "object", "properties": {"path": {"type": "string"}}},
                        }
                    ]
                },
            }
        )
    elif method == "tools/call":
        write_message(
            {
                "jsonrpc": "2.0",
                "id": request["id"],
                "result": {
                    "content": [
                        {"type": "text", "text": "hello"},
                        {"type": "image", "mimeType": "image/png", "data": "..."},
                    ]
                },
            }
        )
    else:
        write_message({"jsonrpc": "2.0", "id": request["id"], "error": {"code": -32601, "message": "Unknown method"}})
"""


@pytest.mark.asyncio
async def test_mcp_client_lists_and_calls_tools(tmp_path: Path) -> None:
    server = tmp_path / "fake_mcp_server.py"
    server.write_text(_server_script())
    client = StdioMCPClient(
        MCPServerConfig(
            name="filesystem", command=sys.executable, args=[str(server)], timeout_seconds=10
        )
    )

    await client.start()
    tools = await client.list_tools()
    output = await client.call_tool("inspect", {"path": "/tmp/example"})
    await client.close()

    assert tools[0]["name"] == "inspect"
    assert output.output == "hello\n[Image attachment available: image_attachment.png]"
    assert output.attachments is not None
    assert output.attachments[0]["mime_type"] == "image/png"

    definitions = mcp_tools_to_definitions("filesystem", tools, timeout_seconds=2)
    assert definitions[0].name == sanitize_mcp_tool_name("filesystem", "inspect")
    assert definitions[0].source.raw_tool_name == "inspect"


def test_normalize_call_result_emits_binary_attachment() -> None:
    result = _normalize_call_result(
        {
            "content": [
                {"type": "text", "text": "hello"},
                {"type": "image", "mimeType": "image/png", "data": "YWJj"},
            ]
        }
    )

    assert result.output == "hello"
    assert result.attachments is not None
    assert result.attachments[0]["content_b64"] == "YWJj"
    assert result.attachments[0]["mime_type"] == "image/png"


def test_normalize_streamable_http_url_removes_mcp_trailing_slash() -> None:
    url = "http://mcp-gws.openwebui.svc.cluster.local/mcp/?x=1#frag"

    assert normalize_streamable_http_url(url) == (
        "http://mcp-gws.openwebui.svc.cluster.local/mcp?x=1#frag"
    )
    assert normalize_streamable_http_url("http://example.test/other/") == (
        "http://example.test/other/"
    )


@pytest.mark.asyncio
async def test_streamable_http_client_follows_redirects_and_uses_canonical_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: dict[str, object] = {}

    class _AsyncContext:
        def __init__(self, value: object) -> None:
            self.value = value

        async def __aenter__(self) -> object:
            return self.value

        async def __aexit__(self, *_: object) -> None:
            return None

    class _HTTPClient:
        def __init__(self, **kwargs: object) -> None:
            calls["http_kwargs"] = kwargs

        async def __aenter__(self) -> object:
            return self

        async def __aexit__(self, *_: object) -> None:
            return None

    def _streamable_http_client(url: str, *, http_client: object) -> _AsyncContext:
        calls["url"] = url
        calls["http_client"] = http_client
        return _AsyncContext((object(), object(), lambda: None))

    monkeypatch.setattr(mcp_module.httpx, "AsyncClient", _HTTPClient)
    monkeypatch.setattr(mcp_module, "streamable_http_client", _streamable_http_client)

    client = StreamableHTTPMCPClient(
        MCPServerConfig(
            name="googleworkspace",
            transport="streamable_http",
            url="http://mcp-gws.openwebui.svc.cluster.local/mcp/",
        )
    )

    async with mcp_module.AsyncExitStack() as stack:
        await client._enter_transport(stack)

    assert calls["url"] == "http://mcp-gws.openwebui.svc.cluster.local/mcp"
    assert calls["http_kwargs"]["follow_redirects"] is True


@pytest.mark.asyncio
async def test_connect_failure_cleanup_does_not_mask_primary_error() -> None:
    class _BadContext:
        async def __aenter__(self) -> object:
            return object()

        async def __aexit__(self, *_: object) -> None:
            raise BaseExceptionGroup("cleanup failed", [RuntimeError("cleanup")])

    class _BrokenClient(_SessionMCPClient):
        async def _enter_transport(
            self, exit_stack: mcp_module.AsyncExitStack
        ) -> tuple[object, object]:
            await exit_stack.enter_async_context(_BadContext())
            raise RuntimeError("primary failure")

    client = _BrokenClient(MCPServerConfig(name="broken", command="ignored"))

    with pytest.raises(MCPClientError) as exc_info:
        await client.connect()

    assert "primary failure" in str(exc_info.value)


@pytest.mark.asyncio
async def test_mcp_client_close_suppresses_cancelled_error() -> None:
    client = StdioMCPClient(
        MCPServerConfig(name="filesystem", command=sys.executable, args=[], timeout_seconds=10)
    )

    class _ExitStack:
        async def aclose(self) -> None:
            raise asyncio.CancelledError()

    client._exit_stack = _ExitStack()
    client._session = SimpleNamespace()

    await client.close(suppress_cancelled=True)

    assert client._exit_stack is None
    assert client._session is None


@pytest.mark.asyncio
async def test_mcp_client_close_propagates_task_cancellation() -> None:
    client = StdioMCPClient(
        MCPServerConfig(name="filesystem", command=sys.executable, args=[], timeout_seconds=10)
    )

    class _ExitStack:
        async def aclose(self) -> None:
            raise asyncio.CancelledError()

    client._exit_stack = _ExitStack()
    client._session = SimpleNamespace()

    with pytest.raises(asyncio.CancelledError):
        await client.close()

    assert client._exit_stack is not None
    assert client._session is not None


# ---------------------------------------------------------------------------
# _strip_empty_optionals tests
# ---------------------------------------------------------------------------

_TODOIST_ADD_TASKS_SCHEMA: dict = {
    "type": "object",
    "required": ["tasks"],
    "properties": {
        "tasks": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["content"],
                "properties": {
                    "content": {"type": "string"},
                    "description": {"type": "string"},
                    "priority": {"type": "string"},
                    "dueString": {"type": "string"},
                    "deadlineDate": {"type": "string"},
                    "duration": {"type": "string"},
                    "labels": {"type": "array", "items": {"type": "string"}},
                    "projectId": {"type": "string"},
                    "sectionId": {"type": "string"},
                    "parentId": {"type": "string"},
                    "order": {"type": "number"},
                    "responsibleUser": {"type": "string"},
                    "isUncompletable": {"type": "boolean"},
                },
            },
        },
    },
}


def test_strip_empty_optionals_removes_empty_strings() -> None:
    """Empty strings on optional fields should be dropped."""
    args = {
        "tasks": [
            {
                "content": "Buy milk",
                "description": "",
                "projectId": "",
                "sectionId": "abc123",
                "parentId": "",
                "responsibleUser": "",
                "labels": [],
                "dueString": "",
            }
        ]
    }
    result = _strip_empty_optionals(args, _TODOIST_ADD_TASKS_SCHEMA)
    task = result["tasks"][0]
    assert task["content"] == "Buy milk"
    assert task["sectionId"] == "abc123"
    assert "projectId" not in task
    assert "parentId" not in task
    assert "responsibleUser" not in task
    assert "description" not in task
    assert "dueString" not in task
    assert "labels" not in task


def test_strip_empty_optionals_preserves_required_empty_strings() -> None:
    """Required fields must never be stripped, even if empty."""
    schema = {
        "type": "object",
        "required": ["name"],
        "properties": {
            "name": {"type": "string"},
            "description": {"type": "string"},
        },
    }
    args = {"name": "", "description": ""}
    result = _strip_empty_optionals(args, schema)
    assert result["name"] == ""
    assert "description" not in result


def test_strip_empty_optionals_preserves_nonempty_values() -> None:
    """Non-empty optional values must be preserved."""
    args = {
        "tasks": [
            {
                "content": "Fix bug",
                "projectId": "6gH6CVp2fcHrJxvh",
                "sectionId": "6gJvjvQxfcmXw3Ph",
                "parentId": "abc",
                "responsibleUser": "filip@pytloun.cz",
                "labels": ["bug"],
            }
        ]
    }
    result = _strip_empty_optionals(args, _TODOIST_ADD_TASKS_SCHEMA)
    task = result["tasks"][0]
    assert task["content"] == "Fix bug"
    assert task["projectId"] == "6gH6CVp2fcHrJxvh"
    assert task["sectionId"] == "6gJvjvQxfcmXw3Ph"
    assert task["parentId"] == "abc"
    assert task["responsibleUser"] == "filip@pytloun.cz"
    assert task["labels"] == ["bug"]


def test_strip_empty_optionals_no_schema() -> None:
    """With no schema, arguments should pass through unchanged."""
    args = {"foo": "", "bar": []}
    assert _strip_empty_optionals(args, {}) == args


def test_safe_message_redacts_sensitive_fragments() -> None:
    raw = "Authorization=Bearer sk-secret-token Authorization: Basic abc123 password=hunter2 api_key=abcdef1234567890"
    safe = _safe_message(raw)

    assert "sk-secret-token" not in safe
    assert "abc123" not in safe
    assert "hunter2" not in safe
    assert "abcdef1234567890" not in safe
    assert "[redacted]" in safe


def test_strip_empty_optionals_nested_object() -> None:
    """Nested object fields should be recursively sanitized."""
    schema = {
        "type": "object",
        "properties": {
            "config": {
                "type": "object",
                "required": ["host"],
                "properties": {
                    "host": {"type": "string"},
                    "port": {"type": "string"},
                },
            },
        },
    }
    args = {"config": {"host": "localhost", "port": ""}}
    result = _strip_empty_optionals(args, schema)
    assert result["config"] == {"host": "localhost"}


@pytest.mark.asyncio
async def test_mcp_client_close_suppresses_base_exception_group_suppress_true() -> None:
    """BaseExceptionGroup from anyio cross-task teardown is silently swallowed."""
    client = StdioMCPClient(MCPServerConfig(name="broken", command="/bin/echo", timeout_seconds=5))

    class _ExitStack:
        async def aclose(self) -> None:
            raise BaseExceptionGroup("anyio-cancel", [RuntimeError("cross-task scope")])

    client._exit_stack = _ExitStack()
    client._session = SimpleNamespace()

    # Must not raise and must clear both references.
    await client.close(suppress_cancelled=True)

    assert client._exit_stack is None
    assert client._session is None


@pytest.mark.asyncio
async def test_mcp_client_close_suppresses_base_exception_group_suppress_false() -> None:
    """BaseExceptionGroup is always suppressed (it is not a real CancelledError)."""
    client = StdioMCPClient(MCPServerConfig(name="broken", command="/bin/echo", timeout_seconds=5))

    class _ExitStack:
        async def aclose(self) -> None:
            raise BaseExceptionGroup("anyio-cancel", [RuntimeError("cross-task scope")])

    client._exit_stack = _ExitStack()
    client._session = SimpleNamespace()

    # BaseExceptionGroup is not re-raised even when suppress_cancelled=False;
    # it is an anyio artifact, not a genuine task cancellation.
    await client.close(suppress_cancelled=False)

    assert client._exit_stack is None
    assert client._session is None


def test_runtime_mcp_server_key_falls_back_safely() -> None:
    assert runtime_mcp_server_key(MCPServerConfig(name="demo", command="/bin/echo")) == "demo"
    assert runtime_mcp_server_key(ToolSource(type="local_mcp", server_name="demo")) == "demo"


def test_strip_empty_optionals_preserves_zero_and_false() -> None:
    """Falsy non-empty values (0, False) must not be stripped."""
    schema = {
        "type": "object",
        "properties": {
            "order": {"type": "number"},
            "isUncompletable": {"type": "boolean"},
            "name": {"type": "string"},
        },
    }
    args = {"order": 0, "isUncompletable": False, "name": ""}
    result = _strip_empty_optionals(args, schema)
    assert result["order"] == 0
    assert result["isUncompletable"] is False
    assert "name" not in result


def test_strip_empty_optionals_unknown_keys_pass_through() -> None:
    """Keys not in the schema properties should pass through unchanged."""
    schema = {
        "type": "object",
        "required": ["id"],
        "properties": {"id": {"type": "string"}},
    }
    args = {"id": "123", "extra": "value", "another": ""}
    result = _strip_empty_optionals(args, schema)
    assert result["id"] == "123"
    assert result["extra"] == "value"
    # "another" is not required and is empty -> stripped
    assert "another" not in result
