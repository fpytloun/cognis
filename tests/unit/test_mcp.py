from __future__ import annotations

import sys
from pathlib import Path

import pytest

from cognis.models.tool import MCPServerConfig, sanitize_mcp_tool_name
from cognis.tools.mcp import StdioMCPClient, mcp_tools_to_definitions


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
