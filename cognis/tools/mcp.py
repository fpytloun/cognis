"""MCP client transport helpers using the official MCP Python SDK."""

from __future__ import annotations

import asyncio
import io
import json
import os
import re
import threading
from collections.abc import Sequence
from contextlib import AsyncExitStack, suppress
from datetime import timedelta
from typing import Any, Protocol

import httpx
from mcp.client.session import ClientSession
from mcp.client.sse import sse_client
from mcp.client.stdio import StdioServerParameters, stdio_client
from mcp.client.streamable_http import streamable_http_client

from cognis.logging import get_logger
from cognis.models.tool import MCPServerConfig, ToolDefinition, ToolSource, sanitize_mcp_tool_name
from cognis.tools.argument_normalization import strip_empty_optional_values

logger = get_logger(__name__)

_MAX_SAFE_STDERR_LENGTH = 240
_HTTP_READ_TIMEOUT_SECONDS = 300
HTTP_MCP_TRANSPORTS = {"sse", "streamable_http"}
_SENSITIVE_FRAGMENT_PATTERNS = [
    re.compile(r"(?i)\b(bearer)\s+([^\s,;]+)"),
    re.compile(r"(?i)\b(authorization)\b\s*([:=])\s*((?:basic|bearer|token)\s+)?([^\s,;]+)"),
    re.compile(r"(?i)\b(api[_-]?key|token|secret|password)\b\s*([:=])\s*([^\s,;]+)"),
    re.compile(r"\b(sk|pk)_[A-Za-z0-9_-]{8,}\b"),
    re.compile(r"\b[A-Za-z0-9+/=_-]{24,}\b"),
]


class MCPClientError(RuntimeError):
    """Structured MCP client error with safe diagnostics."""

    def __init__(
        self,
        server_name: str,
        phase: str,
        message: str,
        *,
        error_class: str,
        timed_out: bool = False,
        safe_stderr: str | None = None,
    ) -> None:
        super().__init__(message)
        self.server_name = server_name
        self.phase = phase
        self.error_class = error_class
        self.timed_out = timed_out
        self.safe_stderr = safe_stderr


class MCPClient(Protocol):
    """Minimal transport-agnostic MCP client interface."""

    config: MCPServerConfig

    async def connect(self) -> None: ...

    async def list_tools(self) -> list[dict[str, Any]]: ...

    async def call_tool(self, tool_name: str, arguments: dict[str, Any]) -> str: ...

    async def close(self) -> None: ...


class _StderrLogger(io.RawIOBase):
    """Routes subprocess stderr to Python logging via an OS pipe.

    Ported from Intaris. Uses a real OS pipe so that the MCP SDK can
    wire up the child process's stderr via fileno(). A background daemon
    thread reads from the pipe and routes complete lines through logging.
    """

    def __init__(self, server_name: str) -> None:
        super().__init__()
        self._server_name = server_name
        self._recent_lines: list[str] = []
        self._lock = threading.Lock()
        self._read_fd, self._write_fd = os.pipe()
        self._thread = threading.Thread(
            target=self._reader_loop,
            daemon=True,
            name=f"mcp-stderr-{server_name}",
        )
        self._thread.start()

    def _reader_loop(self) -> None:
        try:
            with os.fdopen(self._read_fd, "r", errors="replace") as f:
                for line in f:
                    stripped = line.rstrip()
                    if stripped:
                        with self._lock:
                            self._recent_lines.append(stripped)
                            self._recent_lines = self._recent_lines[-3:]
                        logger.debug(
                            "MCP stdio: %s stderr: %s",
                            self._server_name,
                            _safe_message(stripped),
                        )
        except Exception:
            pass

    def fileno(self) -> int:
        return self._write_fd

    def writable(self) -> bool:
        return True

    def write(self, b: bytes | bytearray) -> int:
        return os.write(self._write_fd, b)

    def summary(self) -> str | None:
        with self._lock:
            if not self._recent_lines:
                return None
            return _safe_message(" | ".join(self._recent_lines))

    def close(self) -> None:
        with suppress(OSError):
            os.close(self._write_fd)


class _SessionMCPClient:
    """Shared session-based MCP client behavior."""

    def __init__(self, config: MCPServerConfig) -> None:
        self.config = config
        self._exit_stack: AsyncExitStack | None = None
        self._session: ClientSession | None = None
        self._tool_schemas: dict[str, dict[str, Any]] = {}

    async def _enter_transport(self, exit_stack: AsyncExitStack) -> tuple[Any, Any]:
        raise NotImplementedError

    async def connect(self) -> None:
        """Open the MCP transport and perform initialize."""

        exit_stack = AsyncExitStack()
        try:
            read_stream, write_stream = await self._enter_transport(exit_stack)
            session = await exit_stack.enter_async_context(
                ClientSession(
                    read_stream,
                    write_stream,
                    read_timeout_seconds=timedelta(seconds=self.config.timeout_seconds),
                )
            )
            await session.initialize()
        except Exception as exc:
            await exit_stack.aclose()
            raise _coerce_client_error(self.config.name, "initialize", exc) from exc

        self._exit_stack = exit_stack
        self._session = session

    async def list_tools(self) -> list[dict[str, Any]]:
        """Discover tools exposed by the MCP server."""

        if self._session is None:
            raise RuntimeError("MCP client is not started")
        logger.debug("MCP: %s requesting tools/list", self.config.name)
        try:
            result = await self._session.list_tools()
        except Exception as exc:
            raise _coerce_client_error(self.config.name, "list_tools", exc) from exc
        tools = [
            {
                "name": t.name,
                "description": t.description or "",
                "inputSchema": t.inputSchema.model_dump()
                if hasattr(t.inputSchema, "model_dump")
                else dict(t.inputSchema),
            }
            for t in result.tools
        ]
        for t in tools:
            name = t["name"]
            input_schema = t.get("inputSchema", {})
            if isinstance(name, str) and isinstance(input_schema, dict):
                self._tool_schemas[name] = input_schema
        if hasattr(self._session, "_tool_output_schemas"):
            self._session._tool_output_schemas.clear()
        logger.info("MCP: %s discovered %d tool(s)", self.config.name, len(tools))
        logger.debug("MCP: %s tool names: %s", self.config.name, [t["name"] for t in tools])
        return tools

    async def call_tool(self, tool_name: str, arguments: dict[str, Any]) -> str:
        """Execute a tool and return the normalized result string."""

        if self._session is None:
            raise RuntimeError("MCP client is not started")
        schema = self._tool_schemas.get(tool_name, {})
        sanitized = _strip_empty_optionals(arguments, schema)
        try:
            result = await self._session.call_tool(tool_name, sanitized)
        except Exception as exc:
            raise _coerce_client_error(self.config.name, "call_tool", exc) from exc
        return _normalize_call_result(result)

    async def close(self) -> None:
        """Close the transport and client session."""

        logger.debug("MCP: %s closing", self.config.name)
        if self._exit_stack is not None:
            try:
                await self._exit_stack.aclose()
            except asyncio.CancelledError:
                current = asyncio.current_task()
                if current is not None and current.cancelling():
                    raise
                logger.debug("MCP: %s close cancelled during transport teardown", self.config.name)
            except Exception:
                pass
            self._exit_stack = None
            self._session = None
        logger.debug("MCP: %s closed", self.config.name)


class StdioMCPClient(_SessionMCPClient):
    """MCP client for a local stdio server using the official MCP SDK."""

    def __init__(self, config: MCPServerConfig, env: dict[str, str] | None = None) -> None:
        super().__init__(config)
        self.env = env
        self._stderr_logger: _StderrLogger | None = None

    async def _enter_transport(self, exit_stack: AsyncExitStack) -> tuple[Any, Any]:
        if self.config.command is None:
            raise MCPClientError(
                self.config.name,
                "spawn",
                "MCP stdio command is required",
                error_class="missing_command",
            )

        logger.info(
            "MCP stdio: spawning %s (command=%s, timeout=%ds)",
            self.config.name,
            self.config.command,
            self.config.timeout_seconds,
        )
        logger.debug(
            "MCP stdio: %s full spawn: command=%s args=%s env_keys=%s",
            self.config.name,
            self.config.command,
            self.config.args,
            sorted(self.env.keys()) if self.env else [],
        )

        params = StdioServerParameters(
            command=self.config.command,
            args=self.config.args,
            env=self.env,
        )

        self._stderr_logger = _StderrLogger(self.config.name)
        try:
            return await exit_stack.enter_async_context(
                stdio_client(params, errlog=self._stderr_logger)
            )
        except Exception as exc:
            stderr_summary = (
                self._stderr_logger.summary() if self._stderr_logger is not None else None
            )
            if self._stderr_logger is not None:
                self._stderr_logger.close()
                self._stderr_logger = None
            self._stderr_logger = None
            logger.warning(
                "MCP stdio: %s spawn failed: %s",
                self.config.name,
                _safe_message(str(exc)),
            )
            raise MCPClientError(
                self.config.name,
                "spawn",
                _safe_message(str(exc)),
                error_class=_error_class(exc),
                timed_out=_is_timeout(exc),
                safe_stderr=stderr_summary,
            ) from exc

    async def connect(self) -> None:
        await super().connect()
        logger.info("MCP stdio: %s initialize handshake complete", self.config.name)

    async def start(self) -> None:
        """Backward-compatible alias for connect()."""

        await self.connect()

    async def close(self) -> None:
        """Terminate the MCP server and clean up resources."""

        await super().close()
        if self._stderr_logger is not None:
            self._stderr_logger.close()
            self._stderr_logger = None


class SSEMCPClient(_SessionMCPClient):
    """MCP client for an HTTP SSE transport."""

    def __init__(self, config: MCPServerConfig, headers: dict[str, str] | None = None) -> None:
        super().__init__(config)
        self.headers = headers

    async def _enter_transport(self, exit_stack: AsyncExitStack) -> tuple[Any, Any]:
        if self.config.url is None:
            raise MCPClientError(
                self.config.name,
                "connect",
                "MCP SSE url is required",
                error_class="missing_url",
            )
        logger.info("MCP sse: connecting %s (%s)", self.config.name, self.config.url)
        return await exit_stack.enter_async_context(
            sse_client(
                self.config.url,
                headers=self.headers,
                timeout=self.config.timeout_seconds,
                sse_read_timeout=max(self.config.timeout_seconds, _HTTP_READ_TIMEOUT_SECONDS),
            )
        )


class StreamableHTTPMCPClient(_SessionMCPClient):
    """MCP client for the streamable HTTP transport."""

    def __init__(self, config: MCPServerConfig, headers: dict[str, str] | None = None) -> None:
        super().__init__(config)
        self.headers = headers

    async def _enter_transport(self, exit_stack: AsyncExitStack) -> tuple[Any, Any]:
        if self.config.url is None:
            raise MCPClientError(
                self.config.name,
                "connect",
                "MCP streamable HTTP url is required",
                error_class="missing_url",
            )
        logger.info("MCP streamable_http: connecting %s (%s)", self.config.name, self.config.url)
        client = await exit_stack.enter_async_context(
            httpx.AsyncClient(
                headers=self.headers,
                timeout=httpx.Timeout(
                    self.config.timeout_seconds,
                    read=max(self.config.timeout_seconds, _HTTP_READ_TIMEOUT_SECONDS),
                ),
            )
        )
        read_stream, write_stream, _get_session_id = await exit_stack.enter_async_context(
            streamable_http_client(self.config.url, http_client=client)
        )
        return read_stream, write_stream


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


def build_mcp_client(config: MCPServerConfig, secrets: dict[str, str]) -> MCPClient:
    """Build a transport-specific MCP client from config."""

    if config.transport == "stdio":
        return StdioMCPClient(config, env=resolve_secret_refs(config.env, secrets))
    if config.transport == "sse":
        return SSEMCPClient(config, headers=resolve_secret_refs(config.headers, secrets))
    if config.transport == "streamable_http":
        return StreamableHTTPMCPClient(config, headers=resolve_secret_refs(config.headers, secrets))
    raise ValueError(f"Unsupported MCP transport: {config.transport}")


def runtime_mcp_server_key(config: MCPServerConfig | ToolSource) -> str:
    """Return the immutable routing key for an MCP server."""

    return (
        getattr(config, "server_id", None)
        or getattr(config, "server_name", None)
        or getattr(config, "name", "unknown")
    )


def invalid_mcp_config_reason(
    *,
    transport: str,
    command: str | None,
    url: str | None,
    env: dict[str, str] | None,
    headers: dict[str, str] | None,
) -> str | None:
    """Return a user-visible invalidity reason for persisted config."""

    env = env or {}
    headers = headers or {}
    if transport == "stdio":
        if not command:
            return "Stdio MCP servers must define a command."
        if headers:
            return "Stdio MCP servers cannot define HTTP headers."
        return None
    if transport in HTTP_MCP_TRANSPORTS:
        if not url:
            return "HTTP MCP servers must define a URL."
        if env:
            return "HTTP MCP servers must use headers instead of environment variables."
        return None
    return f"Unsupported MCP transport: {transport}"


def canonicalize_mcp_headers(headers: dict[str, str]) -> dict[str, str]:
    """Normalize header names and reject case-insensitive duplicates."""

    canonical: dict[str, str] = {}
    seen_lower: dict[str, str] = {}
    for raw_key, value in headers.items():
        key = "-".join(part[:1].upper() + part[1:] for part in raw_key.strip().split("-"))
        if not key:
            raise ValueError("HTTP header names cannot be empty")
        lowered = key.lower()
        previous = seen_lower.get(lowered)
        if previous is not None and previous != key:
            raise ValueError(
                f"Duplicate HTTP header differs only by case: {previous} and {raw_key.strip()}"
            )
        seen_lower[lowered] = key
        canonical[key] = value
    return canonical


def validate_unique_server_names(servers: Sequence[MCPServerConfig]) -> None:
    names = [server.name for server in servers]
    if len(names) != len(set(names)):
        duplicates = sorted({name for name in names if names.count(name) > 1})
        msg = f"Duplicate MCP server names are not allowed: {', '.join(duplicates)}"
        raise ValueError(msg)


def _strip_empty_optionals(
    arguments: dict[str, Any],
    schema: dict[str, Any],
) -> dict[str, Any]:
    """Compatibility wrapper for older tests/imports."""
    return strip_empty_optional_values(arguments, schema)


def _normalize_call_result(result: Any) -> str:
    """Normalize a CallToolResult from the MCP SDK into a string."""
    content = getattr(result, "content", None)
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            item_type = getattr(item, "type", None)
            if item_type == "text":
                parts.append(str(getattr(item, "text", "")))
            elif item_type == "image":
                parts.append("[image content omitted]")
            elif item_type == "resource":
                parts.append("[resource content omitted]")
            else:
                try:
                    parts.append(json.dumps(item, sort_keys=True, default=str))
                except Exception:
                    parts.append(str(item))
        if parts:
            return "\n".join(parts)
    # Fallback: try dict-style access (older SDK versions)
    if isinstance(result, dict):
        raw_content = result.get("content")
        if isinstance(raw_content, list):
            texts = [
                item.get("text", "")
                for item in raw_content
                if isinstance(item, dict) and item.get("type") == "text"
            ]
            if texts:
                return "\n".join(texts)
    return str(result)


def _safe_message(message: str, *, limit: int = _MAX_SAFE_STDERR_LENGTH) -> str:
    collapsed = " ".join(message.split())
    redacted = collapsed
    for pattern in _SENSITIVE_FRAGMENT_PATTERNS:
        redacted = pattern.sub(_redact_match, redacted)
    return redacted[:limit]


def _redact_match(match: re.Match[str]) -> str:
    groups = match.groups()
    if len(groups) == 2:
        return f"{groups[0]} [redacted]"
    if len(groups) == 3:
        return f"{groups[0]}{groups[1]}[redacted]"
    if len(groups) == 4:
        scheme = groups[2] or ""
        return f"{groups[0]}{groups[1]}{scheme}[redacted]"
    return "[redacted]"


def _is_timeout(exc: Exception) -> bool:
    return "timed out" in str(exc).lower() or "timeout" in type(exc).__name__.lower()


def _error_class(exc: Exception) -> str:
    return "timeout" if _is_timeout(exc) else type(exc).__name__.lower()


def _coerce_client_error(server_name: str, phase: str, exc: Exception) -> MCPClientError:
    if isinstance(exc, MCPClientError):
        return exc
    return MCPClientError(
        server_name,
        phase,
        _safe_message(str(exc)),
        error_class=_error_class(exc),
        timed_out=_is_timeout(exc),
    )
