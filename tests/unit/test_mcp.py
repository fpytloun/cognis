from __future__ import annotations

import sys
from pathlib import Path

import pytest

from cognis.models.tool import MCPServerConfig, sanitize_mcp_tool_name
from cognis.tools.mcp import StdioMCPClient, _strip_empty_optionals, mcp_tools_to_definitions


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
    assert output == "hello\n[image content omitted]"

    definitions = mcp_tools_to_definitions("filesystem", tools, timeout_seconds=2)
    assert definitions[0].name == sanitize_mcp_tool_name("filesystem", "inspect")
    assert definitions[0].source.raw_tool_name == "inspect"


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
