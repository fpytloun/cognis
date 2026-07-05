from __future__ import annotations

import asyncio
import sys
from builtins import BaseExceptionGroup, ExceptionGroup
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from cognis import __version__ as COGNIS_VERSION
from cognis.models.tool import MCPServerConfig, ToolSource, sanitize_mcp_tool_name
from cognis.tools import mcp as mcp_module
from cognis.tools.mcp import (
    AsyncExitStack,
    MCPClientError,
    SSEMCPClient,
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


def test_sanitize_mcp_tool_name_does_not_suffix_simple_normalization() -> None:
    assert (
        sanitize_mcp_tool_name("mfg-portal", "alertmanager.alerts")
        == "mcp_mfg-portal__alertmanager_alerts"
    )


def test_mcp_tools_to_definitions_suffixes_actual_normalized_name_collisions() -> None:
    definitions = mcp_tools_to_definitions(
        "github",
        [
            {"name": "search/issues", "inputSchema": {"type": "object", "properties": {}}},
            {"name": "search_issues", "inputSchema": {"type": "object", "properties": {}}},
        ],
        timeout_seconds=2,
    )

    names = {definition.name for definition in definitions}
    assert names == {
        "mcp_github__search_issues_9287b261",
        "mcp_github__search_issues_28fc1708",
    }
    assert {definition.source.raw_tool_name for definition in definitions} == {
        "search/issues",
        "search_issues",
    }


def test_mcp_tools_to_definitions_clamps_descriptions_and_strips_schema_metadata() -> None:
    definitions = mcp_tools_to_definitions(
        "github",
        [
            {
                "name": "search",
                "description": "x" * 2000,
                "inputSchema": {
                    "$schema": "https://json-schema.org/draft/2020-12/schema",
                    "type": "object",
                    "properties": {
                        "query": {
                            "$id": "inner",
                            "$comment": "drop me",
                            "type": "string",
                        }
                    },
                },
            }
        ],
        timeout_seconds=2,
    )

    definition = definitions[0]
    assert len(definition.description) <= 1024
    assert definition.description.endswith("(full description via search_tools)")
    assert "$schema" not in definition.parameters
    assert "$id" not in definition.parameters["properties"]["query"]
    assert "$comment" not in definition.parameters["properties"]["query"]


@pytest.mark.asyncio
async def test_streamable_http_client_follows_redirects_and_uses_canonical_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: dict[str, Any] = {}

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
    assert calls["http_kwargs"]["headers"]["User-Agent"] == f"Cognis/{COGNIS_VERSION}"


@pytest.mark.asyncio
async def test_streamable_http_client_preserves_configured_user_agent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: dict[str, Any] = {}

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
            name="rohlik",
            transport="streamable_http",
            url="https://mcp.rohlik.cz/mcp",
        ),
        headers={"user-agent": "CustomMCP/1.0", "Rhl-Email": "$secret:email"},
    )

    async with mcp_module.AsyncExitStack() as stack:
        await client._enter_transport(stack)

    assert calls["http_kwargs"]["headers"] == {
        "user-agent": "CustomMCP/1.0",
        "Rhl-Email": "$secret:email",
    }


@pytest.mark.asyncio
async def test_sse_client_uses_default_user_agent(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: dict[str, Any] = {}

    class _AsyncContext:
        async def __aenter__(self) -> tuple[object, object]:
            return object(), object()

        async def __aexit__(self, *_: object) -> None:
            return None

    def _sse_client(url: str, **kwargs: object) -> _AsyncContext:
        calls["url"] = url
        calls["kwargs"] = kwargs
        return _AsyncContext()

    monkeypatch.setattr(mcp_module, "sse_client", _sse_client)

    client = SSEMCPClient(
        MCPServerConfig(
            name="legacy-sse",
            transport="sse",
            url="https://example.test/sse",
        )
    )

    async with mcp_module.AsyncExitStack() as stack:
        await client._enter_transport(stack)

    assert calls["url"] == "https://example.test/sse"
    assert calls["kwargs"]["headers"]["User-Agent"] == f"Cognis/{COGNIS_VERSION}"


@pytest.mark.asyncio
async def test_sse_client_preserves_configured_user_agent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: dict[str, Any] = {}

    class _AsyncContext:
        async def __aenter__(self) -> tuple[object, object]:
            return object(), object()

        async def __aexit__(self, *_: object) -> None:
            return None

    def _sse_client(url: str, **kwargs: object) -> _AsyncContext:
        calls["url"] = url
        calls["kwargs"] = kwargs
        return _AsyncContext()

    monkeypatch.setattr(mcp_module, "sse_client", _sse_client)

    client = SSEMCPClient(
        MCPServerConfig(
            name="legacy-sse",
            transport="sse",
            url="https://example.test/sse",
        ),
        headers={"user-agent": "CustomMCP/1.0", "X-Test": "1"},
    )

    async with mcp_module.AsyncExitStack() as stack:
        await client._enter_transport(stack)

    assert calls["kwargs"]["headers"] == {
        "user-agent": "CustomMCP/1.0",
        "X-Test": "1",
    }


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
async def test_mcp_client_close_uses_session_owner_task() -> None:
    """Close cancels the session owner so teardown runs in that owner task."""
    client = StdioMCPClient(MCPServerConfig(name="broken", command="/bin/echo", timeout_seconds=5))
    owner_task: asyncio.Task[None] | None = None
    cancelled = False

    async def owner() -> None:
        nonlocal cancelled, owner_task
        owner_task = asyncio.current_task()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled = True
            raise

    requests: asyncio.Queue[tuple[str, tuple[object, ...], asyncio.Future[object]]] = (
        asyncio.Queue()
    )
    client._requests = requests
    client._task = asyncio.create_task(owner())
    await asyncio.sleep(0)

    await client.close(suppress_cancelled=True)

    assert cancelled is True
    assert owner_task is not None
    assert owner_task.done()
    assert client._task is None
    assert client._requests is None


@pytest.mark.asyncio
async def test_mcp_session_owner_closes_context_in_entering_task(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entered_task: asyncio.Task[None] | None = None
    exited_task: asyncio.Task[None] | None = None

    class _Context:
        async def __aenter__(self) -> tuple[object, object]:
            nonlocal entered_task
            entered_task = asyncio.current_task()
            return object(), object()

        async def __aexit__(self, *_args: object) -> None:
            nonlocal exited_task
            exited_task = asyncio.current_task()

    class _Session:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            self.tools = [SimpleNamespace(name="inspect", description="", inputSchema={})]

        async def __aenter__(self) -> _Session:
            return self

        async def __aexit__(self, *_args: object) -> None:
            return None

        async def initialize(self) -> None:
            return None

        async def list_tools(self) -> SimpleNamespace:
            return SimpleNamespace(tools=self.tools)

    class _Client(_SessionMCPClient):
        async def _enter_transport(self, exit_stack: AsyncExitStack) -> tuple[object, object]:
            return await exit_stack.enter_async_context(_Context())

    monkeypatch.setattr(mcp_module, "ClientSession", _Session)
    client = _Client(MCPServerConfig(name="fake", command="/bin/echo", timeout_seconds=5))

    await client.connect()
    assert await client.list_tools()
    await client.close()

    assert entered_task is not None
    assert exited_task is entered_task


@pytest.mark.asyncio
async def test_mcp_connect_times_out_and_closes_owner_task() -> None:
    class _Client(_SessionMCPClient):
        async def _enter_transport(self, exit_stack: AsyncExitStack) -> tuple[object, object]:
            await asyncio.Event().wait()
            raise AssertionError("unreachable")

    client = _Client(MCPServerConfig(name="hung", command="/bin/echo", timeout_seconds=1))

    with pytest.raises(MCPClientError, match="timed out") as exc_info:
        await client.connect()

    assert exc_info.value.timed_out is True
    assert client._task is None
    assert client._requests is None


@pytest.mark.asyncio
async def test_mcp_connect_timeout_surfaces_late_auth_cleanup_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = httpx.Request("POST", "https://mcp.example/mcp")
    response = httpx.Response(
        401,
        headers={"www-authenticate": 'Bearer error="invalid_token"'},
        request=request,
    )
    auth_error = ExceptionGroup(
        "streamable-http cleanup",
        [
            httpx.HTTPStatusError(
                "Client error '401 Unauthorized'",
                request=request,
                response=response,
            )
        ],
    )

    class _Context:
        async def __aenter__(self) -> tuple[object, object]:
            return object(), object()

        async def __aexit__(self, *_args: object) -> None:
            raise auth_error

    class _Session:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            pass

        async def __aenter__(self) -> _Session:
            return self

        async def __aexit__(self, *_args: object) -> None:
            return None

        async def initialize(self) -> None:
            await asyncio.Event().wait()

    class _Client(_SessionMCPClient):
        async def _enter_transport(self, exit_stack: AsyncExitStack) -> tuple[object, object]:
            return await exit_stack.enter_async_context(_Context())

        async def _probe_timeout_authorization(self) -> MCPClientError | None:
            return mcp_module._coerce_client_error("rohlik", "initialize", auth_error)

    monkeypatch.setattr(mcp_module, "ClientSession", _Session)
    client = _Client(
        MCPServerConfig(
            name="rohlik",
            transport="streamable_http",
            url="https://mcp.example/mcp",
            timeout_seconds=1,
            connect_timeout_seconds=1,
        )
    )

    with pytest.raises(MCPClientError) as exc_info:
        await client.connect()

    assert exc_info.value.authorization_required is True
    assert exc_info.value.status_code == 401
    assert exc_info.value.auth_error == "authorization_required"


@pytest.mark.asyncio
async def test_mcp_operation_times_out_and_closes_owner_task(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _Session:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            pass

        async def __aenter__(self) -> _Session:
            return self

        async def __aexit__(self, *_args: object) -> None:
            return None

        async def initialize(self) -> None:
            return None

        async def list_tools(self) -> SimpleNamespace:
            await asyncio.Event().wait()
            raise AssertionError("unreachable")

    class _Client(_SessionMCPClient):
        async def _enter_transport(self, exit_stack: AsyncExitStack) -> tuple[object, object]:
            return object(), object()

    monkeypatch.setattr(mcp_module, "ClientSession", _Session)
    client = _Client(MCPServerConfig(name="hung", command="/bin/echo", timeout_seconds=1))

    await client.connect()
    with pytest.raises(MCPClientError, match="timed out") as exc_info:
        await client.list_tools()

    assert exc_info.value.timed_out is True
    assert client._task is None
    assert client._requests is None


def test_runtime_mcp_server_key_falls_back_safely() -> None:
    assert runtime_mcp_server_key(MCPServerConfig(name="demo", command="/bin/echo")) == "demo"
    assert runtime_mcp_server_key(ToolSource(type="local_mcp", server_name="demo")) == "demo"


def test_coerce_client_error_extracts_http_status_from_exception_group() -> None:
    request = httpx.Request("POST", "https://mfg.prd.lumilens.com/mcp")
    response = httpx.Response(
        401,
        headers={"www-authenticate": 'Bearer error="invalid_token"'},
        request=request,
    )
    exc = ExceptionGroup(
        "streamable-http",
        [
            httpx.HTTPStatusError(
                "Client error '401 Unauthorized'",
                request=request,
                response=response,
            )
        ],
    )

    result = mcp_module._coerce_client_error("mfg-portal", "list_tools", exc)

    assert result.status_code == 401
    assert result.auth_error == "authorization_required"
    assert result.authorization_required is True
    assert result.www_authenticate == "Bearer [redacted]"


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
