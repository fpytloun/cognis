from __future__ import annotations

import sys
from pathlib import Path

import pytest

from cognis.models.tool import MCPServerConfig
from cognis.tools.mcp import StdioMCPClient, mcp_tools_to_definitions


def _server_script() -> str:
    return """
from __future__ import annotations
import json
import sys


def read_message() -> dict:
    header = bytearray()
    while True:
        line = sys.stdin.buffer.readline()
        if not line:
            raise EOFError
        header.extend(line)
        if header.endswith(b"\\r\\n\\r\\n"):
            break
    content_length = 0
    for raw_header in header.decode("utf-8").split("\\r\\n"):
        if raw_header.lower().startswith("content-length:"):
            content_length = int(raw_header.split(":", 1)[1].strip())
            break
    body = sys.stdin.buffer.read(content_length)
    return json.loads(body.decode("utf-8"))


def write_message(payload: dict) -> None:
    body = json.dumps(payload).encode("utf-8")
    sys.stdout.buffer.write(f"Content-Length: {len(body)}\\r\\n\\r\\n".encode("utf-8"))
    sys.stdout.buffer.write(body)
    sys.stdout.buffer.flush()


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
        write_message({"jsonrpc": "2.0", "id": request["id"], "result": {"capabilities": {}}})
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
            name="filesystem", command=sys.executable, args=[str(server)], timeout_seconds=2
        )
    )

    await client.start()
    tools = await client.list_tools()
    output = await client.call_tool("inspect", {"path": "/tmp/example"})
    await client.close()

    assert tools[0]["name"] == "inspect"
    assert output == "hello\n[image content omitted]"

    definitions = mcp_tools_to_definitions("filesystem", tools, timeout_seconds=2)
    assert definitions[0].name == "filesystem/inspect"
