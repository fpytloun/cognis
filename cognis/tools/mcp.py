"""MCP client transport helpers using the official MCP Python SDK."""

from __future__ import annotations

import asyncio
import base64
import io
import json
import mimetypes
import os
import re
import threading
from builtins import BaseExceptionGroup
from collections.abc import Iterable, Sequence
from contextlib import AsyncExitStack, suppress
from datetime import timedelta
from typing import Any, Protocol, TextIO, cast
from urllib.parse import urlsplit, urlunsplit

import httpx
from mcp.client.session import ClientSession
from mcp.client.sse import sse_client
from mcp.client.stdio import StdioServerParameters, stdio_client
from mcp.client.streamable_http import streamable_http_client

from cognis import __version__ as COGNIS_VERSION
from cognis.logging import get_logger
from cognis.models.tool import (
    MCPServerConfig,
    ToolDefinition,
    ToolResult,
    ToolSource,
    effective_mcp_auth_config,
    mcp_headers_have_authorization,
    sanitize_mcp_tool_name,
    sanitize_mcp_tool_name_with_suffix,
    stable_tool_id,
)
from cognis.tools.argument_normalization import strip_empty_optional_values

logger = get_logger(__name__)

_MAX_SAFE_STDERR_LENGTH = 240
_HTTP_READ_TIMEOUT_SECONDS = 300
_DEFAULT_HTTP_USER_AGENT = f"Cognis/{COGNIS_VERSION}"
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
        status_code: int | None = None,
        auth_error: str | None = None,
        www_authenticate: str | None = None,
    ) -> None:
        super().__init__(message)
        self.server_name = server_name
        self.phase = phase
        self.error_class = error_class
        self.timed_out = timed_out
        self.safe_stderr = safe_stderr
        self.status_code = status_code
        self.auth_error = auth_error
        self.www_authenticate = www_authenticate
        self.authorization_required = status_code in {401, 403} or auth_error is not None


class MCPClient(Protocol):
    """Minimal transport-agnostic MCP client interface."""

    config: MCPServerConfig

    async def connect(self) -> None: ...

    async def list_tools(self) -> list[dict[str, Any]]: ...

    async def call_tool(self, tool_name: str, arguments: dict[str, Any]) -> Any: ...

    async def close(self, *, suppress_cancelled: bool = False) -> None: ...


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

    def write(self, b: Any) -> int:
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
        self._task: asyncio.Task[None] | None = None
        self._requests: asyncio.Queue[tuple[str, tuple[Any, ...], asyncio.Future[Any]]] | None = (
            None
        )
        self._ready: asyncio.Future[None] | None = None
        self._tool_schemas: dict[str, dict[str, Any]] = {}
        self._last_owner_error: BaseException | None = None

    @staticmethod
    def _consume_future_result(future: asyncio.Future[Any]) -> None:
        with suppress(BaseException):
            future.result()

    async def _enter_transport(self, exit_stack: AsyncExitStack) -> tuple[Any, Any]:
        raise NotImplementedError

    async def _probe_timeout_authorization(self) -> MCPClientError | None:
        return None

    async def connect(self) -> None:
        """Open the MCP transport and perform initialize."""

        if self._task is not None:
            return
        loop = asyncio.get_running_loop()
        self._requests = asyncio.Queue()
        self._ready = loop.create_future()
        self._last_owner_error = None
        self._task = asyncio.create_task(self._run_session_owner(), name=f"mcp:{self.config.name}")
        ready = self._ready
        try:
            await asyncio.wait_for(
                asyncio.shield(ready),
                timeout=self.config.connect_timeout_seconds,
            )
        except TimeoutError as exc:
            owner_task = self._task
            await self.close(suppress_cancelled=True)
            late_error = _coerce_future_error(
                self.config.name,
                "initialize",
                ready,
            )
            if late_error is None and self._last_owner_error is not None:
                late_error = _coerce_client_error(
                    self.config.name,
                    "initialize",
                    self._last_owner_error,
                )
            late_error = late_error or _coerce_owner_task_error(
                self.config.name,
                "initialize",
                owner_task,
            )
            if late_error is None or not late_error.authorization_required:
                late_error = await self._probe_timeout_authorization()
            if late_error is not None and late_error.authorization_required:
                raise late_error from exc
            raise MCPClientError(
                self.config.name,
                "initialize",
                f"MCP initialize timed out after {self.config.connect_timeout_seconds}s",
                error_class="timeout",
                timed_out=True,
            ) from exc
        except asyncio.CancelledError:
            await self.close(suppress_cancelled=True)
            raise

    async def list_tools(self) -> list[dict[str, Any]]:
        """Discover tools exposed by the MCP server."""

        if self._task is None:
            raise RuntimeError("MCP client is not started")
        logger.debug("MCP: %s requesting tools/list", self.config.name)
        result = await self._submit("list_tools")
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
        logger.info("MCP: %s discovered %d tool(s)", self.config.name, len(tools))
        logger.debug("MCP: %s tool names: %s", self.config.name, [t["name"] for t in tools])
        return tools

    async def call_tool(self, tool_name: str, arguments: dict[str, Any]) -> Any:
        """Execute a tool and return the normalized result payload."""

        if self._task is None:
            raise RuntimeError("MCP client is not started")
        schema = self._tool_schemas.get(tool_name, {})
        sanitized = _strip_empty_optionals(arguments, schema)
        result = await self._submit("call_tool", tool_name, sanitized)
        return _normalize_call_result(result)

    async def close(self, *, suppress_cancelled: bool = False) -> None:
        """Close the transport and client session."""

        logger.debug("MCP: %s closing", self.config.name)
        task = self._task
        requests = self._requests
        self._task = None
        self._requests = None
        self._ready = None
        if task is not None and requests is not None:
            if task.done():
                try:
                    await task
                except BaseException as exc:
                    if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                        raise
                    logger.debug(
                        "MCP: %s owner task already finished during close; suppressing",
                        self.config.name,
                        exc_info=True,
                    )
                logger.debug("MCP: %s closed", self.config.name)
                return
            task.cancel()
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                if not task.cancelled() and not suppress_cancelled:
                    raise
                logger.debug("MCP: %s owner task cancelled during close", self.config.name)
            except BaseException as exc:
                if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                    raise
                logger.debug(
                    "MCP: %s owner task raised during close; suppressing",
                    self.config.name,
                    exc_info=True,
                )
        logger.debug("MCP: %s closed", self.config.name)

    async def _submit(self, operation: str, *args: Any) -> Any:
        if self._requests is None:
            raise RuntimeError("MCP client is not started")
        loop = asyncio.get_running_loop()
        future: asyncio.Future[Any] = loop.create_future()
        await self._requests.put((operation, args, future))
        try:
            return await asyncio.wait_for(
                asyncio.shield(future),
                timeout=self.config.timeout_seconds,
            )
        except TimeoutError as exc:
            await self.close(suppress_cancelled=True)
            raise MCPClientError(
                self.config.name,
                operation,
                f"MCP {operation} timed out after {self.config.timeout_seconds}s",
                error_class="timeout",
                timed_out=True,
            ) from exc
        except asyncio.CancelledError:
            future.add_done_callback(self._consume_future_result)
            raise

    async def _run_session_owner(self) -> None:
        exit_stack = AsyncExitStack()
        assert self._ready is not None
        assert self._requests is not None
        requests = self._requests
        current_future: asyncio.Future[Any] | None = None
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
            self._last_owner_error = exc
            await _close_exit_stack_after_connect_failure(exit_stack, self.config.name)
            if not self._ready.done():
                self._ready.set_exception(_coerce_client_error(self.config.name, "initialize", exc))
            return
        if not self._ready.done():
            self._ready.set_result(None)

        try:
            while True:
                operation, args, future = await requests.get()
                current_future = future
                if operation == "close":
                    if not future.done():
                        future.set_result(None)
                    break
                try:
                    result: Any
                    if operation == "list_tools":
                        result = await session.list_tools()
                        if hasattr(session, "_tool_output_schemas"):
                            session._tool_output_schemas.clear()
                    elif operation == "call_tool":
                        result = await session.call_tool(*args)
                    else:
                        raise RuntimeError(f"Unsupported MCP operation: {operation}")
                except Exception as exc:
                    phase = operation if operation == "list_tools" else "call_tool"
                    if not future.done():
                        future.set_exception(_coerce_client_error(self.config.name, phase, exc))
                else:
                    if not future.done():
                        future.set_result(result)
                finally:
                    current_future = None
        finally:
            closed_exc = RuntimeError("MCP client closed")
            if current_future is not None and not current_future.done():
                current_future.set_exception(closed_exc)
            while True:
                try:
                    _operation, _args, future = requests.get_nowait()
                except asyncio.QueueEmpty:
                    break
                if not future.done():
                    future.set_exception(RuntimeError("MCP client closed"))
            try:
                await exit_stack.aclose()
            except BaseException as exc:
                self._last_owner_error = exc
                raise


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
                stdio_client(params, errlog=cast(TextIO, self._stderr_logger))
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

    async def close(self, *, suppress_cancelled: bool = False) -> None:
        """Terminate the MCP server and clean up resources."""

        await super().close(suppress_cancelled=suppress_cancelled)
        if self._stderr_logger is not None:
            self._stderr_logger.close()
            self._stderr_logger = None


class SSEMCPClient(_SessionMCPClient):
    """MCP client for an HTTP SSE transport."""

    def __init__(self, config: MCPServerConfig, headers: dict[str, str] | None = None) -> None:
        super().__init__(config)
        self.headers = _with_default_http_headers(headers)

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
                timeout=self.config.connect_timeout_seconds,
                sse_read_timeout=max(self.config.timeout_seconds, _HTTP_READ_TIMEOUT_SECONDS),
            )
        )

    async def _probe_timeout_authorization(self) -> MCPClientError | None:
        if self.config.url is None:
            return None
        return await _probe_http_authorization(
            server_name=self.config.name,
            url=self.config.url,
            headers=self.headers,
            timeout_seconds=self.config.connect_timeout_seconds,
        )


class StreamableHTTPMCPClient(_SessionMCPClient):
    """MCP client for the streamable HTTP transport."""

    def __init__(self, config: MCPServerConfig, headers: dict[str, str] | None = None) -> None:
        super().__init__(config)
        self.headers = _with_default_http_headers(headers)

    async def _enter_transport(self, exit_stack: AsyncExitStack) -> tuple[Any, Any]:
        if self.config.url is None:
            raise MCPClientError(
                self.config.name,
                "connect",
                "MCP streamable HTTP url is required",
                error_class="missing_url",
            )
        url = normalize_streamable_http_url(self.config.url)
        logger.info("MCP streamable_http: connecting %s (%s)", self.config.name, url)
        client = await exit_stack.enter_async_context(
            httpx.AsyncClient(
                headers=self.headers,
                follow_redirects=True,
                timeout=httpx.Timeout(
                    self.config.timeout_seconds,
                    connect=self.config.connect_timeout_seconds,
                    read=max(self.config.timeout_seconds, _HTTP_READ_TIMEOUT_SECONDS),
                ),
            )
        )
        read_stream, write_stream, _get_session_id = await exit_stack.enter_async_context(
            streamable_http_client(url, http_client=client)
        )
        return read_stream, write_stream

    async def _probe_timeout_authorization(self) -> MCPClientError | None:
        if self.config.url is None:
            return None
        return await _probe_http_authorization(
            server_name=self.config.name,
            url=normalize_streamable_http_url(self.config.url),
            headers=self.headers,
            timeout_seconds=self.config.connect_timeout_seconds,
        )


async def _probe_http_authorization(
    *,
    server_name: str,
    url: str,
    headers: dict[str, str],
    timeout_seconds: int,
) -> MCPClientError | None:
    """Best-effort HTTP auth probe after an MCP initialize timeout."""

    try:
        async with (
            httpx.AsyncClient(
                headers=headers,
                follow_redirects=True,
                timeout=httpx.Timeout(max(timeout_seconds, 1), read=2),
            ) as client,
            client.stream("GET", url) as response,
        ):
            if response.status_code not in {401, 403}:
                return None
            response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        error = _coerce_client_error(server_name, "initialize", exc)
        return error if error.authorization_required else None
    except httpx.HTTPError:
        return None
    return None


def normalize_streamable_http_url(url: str) -> str:
    """Return a redirect-resistant streamable HTTP MCP endpoint URL."""

    parsed = urlsplit(url)
    if parsed.path.endswith("/mcp/"):
        return urlunsplit(
            (parsed.scheme, parsed.netloc, parsed.path[:-1], parsed.query, parsed.fragment)
        )
    return url


def _with_default_http_headers(headers: dict[str, str] | None) -> dict[str, str]:
    resolved = dict(headers or {})
    if not any(key.lower() == "user-agent" for key in resolved):
        resolved["User-Agent"] = _DEFAULT_HTTP_USER_AGENT
    return resolved


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
        else:
            parameters = _strip_json_schema_metadata(parameters)
        definitions.append(
            ToolDefinition(
                name=sanitize_mcp_tool_name(server_name, name),
                description=_clamp_mcp_description(
                    str(tool.get("description") or f"MCP tool {name}")
                ),
                parameters=parameters,
                source=ToolSource(
                    type="local_mcp",
                    server_name=server_name,
                    server_id=server_id,
                    raw_tool_name=name,
                ),
                category="mcp",
                content_trust="untrusted",
                timeout_seconds=timeout_seconds,
            )
        )
    return disambiguate_mcp_tool_name_collisions(definitions)


_MCP_DESCRIPTION_MAX_CHARS = 1024
_MCP_DESCRIPTION_TAIL = " ... (full description via search_tools)"
_JSON_SCHEMA_METADATA_KEYS = {"$schema", "$id", "$comment"}


def _clamp_mcp_description(description: str) -> str:
    if len(description) <= _MCP_DESCRIPTION_MAX_CHARS:
        return description
    prefix_len = max(0, _MCP_DESCRIPTION_MAX_CHARS - len(_MCP_DESCRIPTION_TAIL))
    return description[:prefix_len].rstrip() + _MCP_DESCRIPTION_TAIL


def _strip_json_schema_metadata(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: _strip_json_schema_metadata(item)
            for key, item in value.items()
            if key not in _JSON_SCHEMA_METADATA_KEYS
        }
    if isinstance(value, list):
        return [_strip_json_schema_metadata(item) for item in value]
    return value


def disambiguate_mcp_tool_name_collisions(
    definitions: Sequence[ToolDefinition],
) -> list[ToolDefinition]:
    """Suffix MCP tool names only when distinct tools resolve to the same runtime name."""

    identities_by_name: dict[str, set[str]] = {}
    for definition in definitions:
        if definition.source.type not in {"local_mcp", "intaris_mcp"}:
            continue
        identities_by_name.setdefault(definition.name, set()).add(stable_tool_id(definition))

    collision_names = {
        name for name, identities in identities_by_name.items() if len(identities) > 1
    }
    if not collision_names:
        return list(definitions)

    resolved: list[ToolDefinition] = []
    for definition in definitions:
        if definition.name not in collision_names:
            resolved.append(definition)
            continue
        server_name = definition.source.server_name
        raw_tool_name = definition.source.raw_tool_name
        if not server_name or not raw_tool_name:
            resolved.append(definition)
            continue
        resolved.append(
            definition.model_copy(
                update={"name": sanitize_mcp_tool_name_with_suffix(server_name, raw_tool_name)}
            )
        )
    return resolved


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


async def _close_exit_stack_after_connect_failure(
    exit_stack: AsyncExitStack, server_name: str
) -> None:
    """Best-effort cleanup that never masks the primary MCP connect error."""

    try:
        await exit_stack.aclose()
    except BaseException as exc:
        logger.debug(
            "MCP: %s ignored cleanup error after failed connect: %s",
            server_name,
            type(exc).__name__,
        )


def runtime_mcp_server_key(config: MCPServerConfig | ToolSource) -> str:
    """Return the immutable routing key for an MCP server."""

    return str(
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
    auth_config: dict[str, Any] | None = None,
) -> str | None:
    """Return a user-visible invalidity reason for persisted config."""

    env = env or {}
    headers = headers or {}
    effective_auth = effective_mcp_auth_config(auth_config, headers)
    if transport == "stdio":
        if not command:
            return "Stdio MCP servers must define a command."
        if headers:
            return "Stdio MCP servers cannot define HTTP headers."
        if effective_auth.type == "oauth2":
            return "OAuth is only supported for HTTP MCP transports."
        return None
    if transport in HTTP_MCP_TRANSPORTS:
        if not url:
            return "HTTP MCP servers must define a URL."
        if env:
            return "HTTP MCP servers must use headers instead of environment variables."
        if effective_auth.type == "oauth2" and mcp_headers_have_authorization(headers):
            return "Authorization headers are not allowed when OAuth is enabled."
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


def _normalize_call_result(result: Any) -> ToolResult:
    """Normalize a CallToolResult from the MCP SDK into a ToolResult.

    Text blocks are preserved in ``output``. Binary/image/resource blocks are
    surfaced as inline attachments so the normal artifact-materialization path
    can persist them and expose them back to the model as attachment context.
    """
    content = getattr(result, "content", None)
    if isinstance(content, list):
        parts: list[str] = []
        attachments: list[dict[str, Any]] = []
        for item in content:
            item_type = _item_value(item, "type")
            if item_type == "text":
                parts.append(str(getattr(item, "text", "")))
            elif item_type == "image":
                attachment = _mcp_content_attachment(item, default_prefix="image")
                if attachment is not None:
                    attachments.append(attachment)
                    parts.append(f"[Image attachment available: {attachment['filename']}]")
                else:
                    parts.append("[image content unavailable]")
            elif item_type == "resource":
                attachment = _mcp_content_attachment(item, default_prefix="resource")
                if attachment is not None:
                    attachments.append(attachment)
                    parts.append(f"[Resource attachment available: {attachment['filename']}]")
                else:
                    resource_text = _mcp_resource_text(item)
                    if resource_text:
                        parts.append(resource_text)
                    else:
                        parts.append("[resource content unavailable]")
            else:
                try:
                    parts.append(json.dumps(item, sort_keys=True, default=str))
                except Exception:
                    parts.append(str(item))
        if parts or attachments:
            output = "\n".join(parts) if parts else "[Binary MCP content attached]"
            return ToolResult(output=output, attachments=attachments or None)
    # Fallback: try dict-style access (older SDK versions)
    if isinstance(result, dict):
        raw_content = result.get("content")
        if isinstance(raw_content, list):
            texts = [
                item.get("text", "")
                for item in raw_content
                if isinstance(item, dict) and item.get("type") == "text"
            ]
            attachments = [
                attachment
                for attachment in (
                    _mcp_content_attachment(
                        item, default_prefix=str(item.get("type") or "attachment")
                    )
                    for item in raw_content
                    if isinstance(item, dict) and item.get("type") in {"image", "resource"}
                )
                if attachment is not None
            ]
            if texts or attachments:
                output = "\n".join(texts) if texts else "[Binary MCP content attached]"
                return ToolResult(output=output, attachments=attachments or None)
    return ToolResult(output=str(result))


def _mcp_content_attachment(item: Any, *, default_prefix: str) -> dict[str, Any] | None:
    """Convert an MCP image/resource block to an inline attachment."""
    content_b64 = _coerce_attachment_payload(item)
    if content_b64 is None:
        return None
    mime_type = _coerce_mime_type(item, default_prefix=default_prefix)
    filename = _coerce_filename(item, mime_type=mime_type, default_prefix=default_prefix)
    return {
        "content_b64": content_b64,
        "mime_type": mime_type,
        "filename": filename,
        "purpose": "mcp_tool_result",
    }


def _coerce_attachment_payload(item: Any) -> str | None:
    for key in ("data", "blob", "content_b64", "base64"):
        value = _item_value(item, key)
        if isinstance(value, str) and value.strip():
            return value.strip()
        if isinstance(value, (bytes, bytearray)) and value:
            return base64.b64encode(bytes(value)).decode("ascii")
    return None


def _coerce_mime_type(item: Any, *, default_prefix: str) -> str:
    mime_type = _item_value(item, "mimeType") or _item_value(item, "mime_type")
    if isinstance(mime_type, str) and mime_type.strip():
        return mime_type.strip()
    return "image/png" if default_prefix == "image" else "application/octet-stream"


def _coerce_filename(item: Any, *, mime_type: str, default_prefix: str) -> str:
    raw_name = (
        _item_value(item, "filename")
        or _item_value(item, "name")
        or _item_value(item, "title")
        or _item_value(item, "uri")
    )
    if isinstance(raw_name, str) and raw_name.strip():
        candidate = raw_name.strip().rsplit("/", 1)[-1]
        if candidate:
            return candidate
    extension = mimetypes.guess_extension(mime_type) or ""
    return f"{default_prefix}_attachment{extension}"


def _mcp_resource_text(item: Any) -> str:
    value = _item_value(item, "text")
    if isinstance(value, str) and value.strip():
        return value
    return ""


def _item_value(item: Any, key: str) -> Any:
    if isinstance(item, dict):
        return item.get(key)
    return getattr(item, key, None)


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


def _is_timeout(exc: BaseException) -> bool:
    return "timed out" in str(exc).lower() or "timeout" in type(exc).__name__.lower()


def _error_class(exc: BaseException) -> str:
    return "timeout" if _is_timeout(exc) else type(exc).__name__.lower()


def _iter_exception_tree(exc: BaseException) -> Iterable[BaseException]:
    yield exc
    if isinstance(exc, BaseExceptionGroup):
        for child in exc.exceptions:
            yield from _iter_exception_tree(child)


def _coerce_client_error(server_name: str, phase: str, exc: BaseException) -> MCPClientError:
    if isinstance(exc, MCPClientError):
        return exc
    status_code = None
    www_authenticate = None
    for candidate in _iter_exception_tree(exc):
        candidate_status = getattr(candidate, "status_code", None)
        response = getattr(candidate, "response", None)
        if candidate_status is None and response is not None:
            candidate_status = getattr(response, "status_code", None)
        if status_code is None and isinstance(candidate_status, int):
            status_code = candidate_status
        if www_authenticate is None and response is not None:
            headers = getattr(response, "headers", None)
            if headers is not None:
                www_authenticate = headers.get("www-authenticate")
        if status_code is not None and www_authenticate is not None:
            break
    auth_error = None
    if status_code == 401:
        auth_error = "authorization_required"
    elif status_code == 403:
        auth_error = "insufficient_scope" if www_authenticate else "forbidden"
    return MCPClientError(
        server_name,
        phase,
        _safe_message(str(exc)),
        error_class=_error_class(exc),
        timed_out=_is_timeout(exc),
        status_code=status_code if isinstance(status_code, int) else None,
        auth_error=auth_error,
        www_authenticate=_safe_message(www_authenticate) if www_authenticate else None,
    )


def _coerce_owner_task_error(
    server_name: str,
    phase: str,
    task: asyncio.Task[None] | None,
) -> MCPClientError | None:
    """Return a structured owner-task error when timeout cleanup exposed one."""

    if task is None or not task.done() or task.cancelled():
        return None
    try:
        exc = task.exception()
    except (asyncio.CancelledError, Exception):
        return None
    if exc is None:
        return None
    return _coerce_client_error(server_name, phase, exc)


def _coerce_future_error(
    server_name: str,
    phase: str,
    future: asyncio.Future[None] | None,
) -> MCPClientError | None:
    """Return a structured future error when a late initialize result arrived."""

    if future is None or not future.done() or future.cancelled():
        return None
    try:
        exc = future.exception()
    except (asyncio.CancelledError, Exception):
        return None
    if not isinstance(exc, Exception):
        return None
    return _coerce_client_error(server_name, phase, exc)
