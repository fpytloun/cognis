"""Minimal stdio MCP client for local tool servers."""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import Sequence
from contextlib import suppress
from typing import Any

from cognis.logging import get_logger
from cognis.models.tool import MCPServerConfig, ToolDefinition, ToolSource, sanitize_mcp_tool_name

logger = get_logger(__name__)


class StdioMCPClient:
    """JSON-RPC 2.0 MCP client over Content-Length framed stdio."""

    def __init__(self, config: MCPServerConfig, env: dict[str, str] | None = None) -> None:
        self.config = config
        self.env = {**os.environ, **(env or {})}
        self.process: asyncio.subprocess.Process | None = None
        self._next_id = 0
        self._lock = asyncio.Lock()
        self._stderr_task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        """Start the subprocess and perform the initialize handshake."""

        if self.config.command is None:
            raise RuntimeError("MCP stdio command is required")
        self.process = await asyncio.create_subprocess_exec(
            self.config.command,
            *self.config.args,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=self.env,
        )
        self._stderr_task = asyncio.create_task(self._drain_stderr())
        await self._request(
            "initialize",
            {
                "protocolVersion": "2024-11-05",
                "clientInfo": {"name": "cognis", "version": "0.1.0"},
                "capabilities": {},
            },
        )
        # MCP spec requires notifications/initialized after handshake
        await self._send_notification("notifications/initialized")

    async def list_tools(self) -> list[dict[str, Any]]:
        """Discover tools exposed by the MCP server."""

        payload = await self._request("tools/list", {})
        tools = payload.get("tools", [])
        return tools if isinstance(tools, list) else []

    async def call_tool(self, tool_name: str, arguments: dict[str, Any]) -> str:
        """Execute a tool and normalize its content into a text result."""

        payload = await self._request("tools/call", {"name": tool_name, "arguments": arguments})
        return _normalize_mcp_result(payload)

    async def close(self) -> None:
        """Terminate the MCP subprocess and cleanup resources."""

        process = self.process
        if process is None:
            return
        if process.returncode is None:
            process.terminate()
            try:
                await asyncio.wait_for(process.wait(), timeout=2.0)
            except TimeoutError:
                process.kill()
                await process.wait()
        if self._stderr_task is not None:
            self._stderr_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._stderr_task
        self.process = None

    async def _drain_stderr(self) -> None:
        process = self.process
        if process is None or process.stderr is None:
            return
        try:
            while True:
                line = await process.stderr.readline()
                if not line:
                    break
                text = line.decode("utf-8", errors="replace").rstrip()
                if text:
                    logger.debug(
                        "MCP server stderr",
                        extra={"extra_data": {"server": self.config.name, "line": text}},
                    )
        except asyncio.CancelledError:
            raise

    async def _send_notification(self, method: str, params: dict[str, Any] | None = None) -> None:
        """Send a JSON-RPC notification (no id, no response expected)."""
        process = self.process
        if process is None or process.stdin is None:
            return
        payload: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            payload["params"] = params
        body = json.dumps(payload).encode("utf-8")
        message = f"Content-Length: {len(body)}\r\n\r\n".encode() + body
        process.stdin.write(message)
        await process.stdin.drain()

    async def _request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        process = self.process
        if process is None or process.stdin is None or process.stdout is None:
            raise RuntimeError("MCP client is not started")

        async with self._lock:
            self._next_id += 1
            request_id = self._next_id
            payload = {
                "jsonrpc": "2.0",
                "id": request_id,
                "method": method,
                "params": params,
            }
            body = json.dumps(payload).encode("utf-8")
            message = f"Content-Length: {len(body)}\r\n\r\n".encode() + body
            process.stdin.write(message)
            await process.stdin.drain()

            # Read messages until we get one with our request ID.
            # MCP servers may send notifications (no "id" field)
            # between request and response — skip those.
            deadline = asyncio.get_event_loop().time() + self.config.timeout_seconds
            while True:
                remaining = deadline - asyncio.get_event_loop().time()
                if remaining <= 0:
                    raise TimeoutError(f"MCP request {method} timed out")
                response = await asyncio.wait_for(
                    self._read_message(process.stdout), timeout=remaining
                )
                # Notifications have no "id" — skip them
                if "id" not in response:
                    logger.debug(
                        "MCP notification received (skipped)",
                        extra={
                            "extra_data": {
                                "server": self.config.name,
                                "method": response.get("method"),
                            }
                        },
                    )
                    continue
                if response.get("id") != request_id:
                    raise RuntimeError(
                        f"MCP response ID mismatch: expected {request_id}, got {response.get('id')}"
                    )
                break

        if "error" in response:
            error = response["error"]
            raise RuntimeError(json.dumps(error, sort_keys=True))
        result = response.get("result")
        if not isinstance(result, dict):
            raise RuntimeError("MCP result payload must be an object")
        return result

    async def _read_message(self, stdout: asyncio.StreamReader) -> dict[str, Any]:
        header_bytes = bytearray()
        while True:
            line = await stdout.readline()
            if not line:
                raise RuntimeError("MCP server closed stdout")
            header_bytes.extend(line)
            if header_bytes.endswith(b"\r\n\r\n"):
                break
        headers = header_bytes.decode("utf-8").split("\r\n")
        content_length = 0
        for header in headers:
            if header.lower().startswith("content-length:"):
                content_length = int(header.split(":", 1)[1].strip())
                break
        if content_length <= 0:
            raise RuntimeError("Missing MCP Content-Length header")
        body = await stdout.readexactly(content_length)
        payload = json.loads(body.decode("utf-8"))
        if not isinstance(payload, dict):
            raise RuntimeError("MCP payload must be an object")
        return payload


def mcp_tools_to_definitions(
    server_name: str,
    tools: Sequence[dict[str, Any]],
    timeout_seconds: int,
    *,
    server_id: str | None = None,
) -> list[ToolDefinition]:
    """Convert MCP tool metadata into Cognis tool definitions."""

    definitions: list[ToolDefinition] = []
    for tool in tools:
        name = tool.get("name")
        if not isinstance(name, str):
            continue
        parameters = tool.get("inputSchema")
        if not isinstance(parameters, dict):
            parameters = {"type": "object", "properties": {}}
        definitions.append(
            ToolDefinition(
                name=sanitize_mcp_tool_name(server_name, name),
                description=str(tool.get("description") or f"MCP tool {name}"),
                parameters=parameters,
                source=ToolSource(
                    type="local_mcp",
                    server_name=server_name,
                    server_id=server_id,
                    raw_tool_name=name,
                ),
                category="mcp",
                timeout_seconds=timeout_seconds,
            )
        )
    return definitions


def resolve_secret_refs(env: dict[str, str], secrets: dict[str, str]) -> dict[str, str]:
    """Resolve ``$secret:NAME`` references in MCP environment variables."""
    resolved: dict[str, str] = {}
    for key, value in env.items():
        if value.startswith("$secret:"):
            resolved[key] = secrets.get(value[len("$secret:") :], "")
        else:
            resolved[key] = value
    return resolved


def validate_unique_server_names(servers: Sequence[MCPServerConfig]) -> None:
    names = [server.name for server in servers]
    if len(names) != len(set(names)):
        duplicates = sorted({name for name in names if names.count(name) > 1})
        msg = f"Duplicate MCP server names are not allowed: {', '.join(duplicates)}"
        raise ValueError(msg)


def _normalize_mcp_result(result: dict[str, Any]) -> str:
    content = result.get("content")
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if not isinstance(item, dict):
                parts.append(json.dumps(item, sort_keys=True, default=str))
                continue
            item_type = item.get("type")
            if item_type == "text" and isinstance(item.get("text"), str):
                parts.append(item["text"])
            elif item_type == "image":
                parts.append("[image content omitted]")
            elif item_type == "resource":
                parts.append("[resource content omitted]")
            else:
                parts.append(f"[{item_type or 'content'} omitted]")
        if parts:
            return "\n".join(parts)
    structured_content = result.get("structuredContent")
    if isinstance(structured_content, (dict, list)):
        return json.dumps(structured_content, sort_keys=True, default=str)
    if isinstance(result.get("text"), str):
        return str(result["text"])
    return json.dumps(result, sort_keys=True, default=str)
