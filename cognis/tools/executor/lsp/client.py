"""Async LSP client over Content-Length framed stdio.

Manages the full LSP lifecycle (initialize → work → shutdown) for a
single language server process.  A background reader task continuously
processes server-initiated notifications (``publishDiagnostics``, etc.)
while request/response correlation uses per-ID futures.
"""

from __future__ import annotations

import asyncio
import json
import os
from contextlib import suppress
from time import monotonic, perf_counter
from typing import Any
from urllib.parse import quote, unquote, urlparse

from cognis.logging import get_logger
from cognis.tools.executor.lsp.types import (
    Diagnostic,
    DiagnosticSeverity,
    Position,
    Range,
)

logger = get_logger(__name__)

# Debounce: wait this long after the last diagnostic notification before
# resolving ``wait_for_diagnostics``.  Servers often emit multiple batches
# in rapid succession.
_DIAGNOSTICS_DEBOUNCE_MS = 200


def file_uri(path: str) -> str:
    """Convert an absolute file path to a ``file://`` URI."""
    abs_path = os.path.abspath(path)
    # Quote the path but preserve / as path separator
    return "file://" + quote(abs_path, safe="/:@")


def uri_to_path(uri: str) -> str:
    """Convert a ``file://`` URI to an absolute file path."""
    prefix = "file://"
    if uri.startswith(prefix):
        return unquote(uri[len(prefix) :])
    parsed = urlparse(uri)
    return unquote(parsed.path)


class LSPClient:
    """Async LSP client communicating over Content-Length framed stdio.

    Manages a single language server subprocess.  Provides methods for
    the LSP lifecycle and collects diagnostics from
    ``textDocument/publishDiagnostics`` notifications.
    """

    def __init__(
        self,
        server_id: str,
        command: str,
        args: list[str],
        root_uri: str,
        *,
        env: dict[str, str] | None = None,
        init_options: dict[str, Any] | None = None,
    ) -> None:
        self.server_id = server_id
        self.command = command
        self.args = args
        self.root_uri = root_uri
        self.env: dict[str, str] = {**os.environ, **(env or {})}
        self.init_options = init_options

        self.process: asyncio.subprocess.Process | None = None
        self.server_name: str | None = None

        # Request/response
        self._next_id = 0
        self._pending: dict[int, asyncio.Future[dict[str, Any]]] = {}

        # Diagnostics state
        self._diagnostics: dict[str, list[Diagnostic]] = {}
        self._diag_events: dict[str, asyncio.Event] = {}

        # Background tasks
        self._reader_task: asyncio.Task[None] | None = None
        self._stderr_task: asyncio.Task[None] | None = None
        self._closed = False

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Start the server process and perform the LSP initialize handshake."""
        start_time = perf_counter()
        self.process = await asyncio.create_subprocess_exec(
            self.command,
            *self.args,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=self.env,
        )
        logger.info(
            "lsp: server process spawned",
            extra={
                "extra_data": {
                    "server_id": self.server_id,
                    "command": self.command,
                    "pid": self.process.pid,
                    "root_uri": self.root_uri,
                }
            },
        )

        # Start background tasks before handshake so we can read the response
        self._reader_task = asyncio.create_task(
            self._reader_loop(), name=f"lsp-reader-{self.server_id}"
        )
        self._stderr_task = asyncio.create_task(
            self._drain_stderr(), name=f"lsp-stderr-{self.server_id}"
        )

        # LSP initialize handshake
        init_result = await self._request(
            "initialize",
            {
                "processId": os.getpid(),
                "rootUri": self.root_uri,
                "capabilities": {
                    "textDocument": {
                        "synchronization": {
                            "openClose": True,
                            "change": 1,  # Full sync
                            "didSave": True,
                        },
                        "publishDiagnostics": {
                            "versionSupport": True,
                            "relatedInformation": False,
                        },
                    },
                    "workspace": {
                        "configuration": True,
                        "didChangeWatchedFiles": {
                            "dynamicRegistration": False,
                        },
                    },
                },
                **({"initializationOptions": self.init_options} if self.init_options else {}),
            },
            timeout=45.0,
        )

        # Extract server info
        server_info = init_result.get("serverInfo", {})
        self.server_name = server_info.get("name", self.server_id)

        # Send initialized notification
        await self._notify("initialized", {})

        duration_ms = int((perf_counter() - start_time) * 1000)
        logger.info(
            "lsp: initialize handshake complete",
            extra={
                "extra_data": {
                    "server_id": self.server_id,
                    "server_name": self.server_name,
                    "duration_ms": duration_ms,
                }
            },
        )

    async def close(self) -> None:
        """Send shutdown/exit and terminate the server process."""
        if self._closed:
            return
        self._closed = True

        process = self.process
        pid = process.pid if process else None

        # Try graceful shutdown
        if process is not None and process.returncode is None:
            try:
                await asyncio.wait_for(self._request("shutdown", None, timeout=5.0), timeout=5.0)
                await self._notify("exit", None)
            except Exception:
                pass

            # Wait for process to exit
            if process.returncode is None:
                process.terminate()
                try:
                    await asyncio.wait_for(process.wait(), timeout=3.0)
                except TimeoutError:
                    process.kill()
                    await process.wait()

        # Cancel background tasks
        for task in (self._reader_task, self._stderr_task):
            if task is not None and not task.done():
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task

        # Reject any pending futures
        for future in self._pending.values():
            if not future.done():
                future.set_exception(RuntimeError("LSP client closed"))
        self._pending.clear()

        exit_code = process.returncode if process else None
        logger.info(
            "lsp: server closed",
            extra={
                "extra_data": {
                    "server_id": self.server_id,
                    "pid": pid,
                    "exit_code": exit_code,
                }
            },
        )
        self.process = None

    # ------------------------------------------------------------------
    # Document notifications
    # ------------------------------------------------------------------

    async def did_open(self, uri: str, language_id: str, text: str) -> None:
        """Send ``textDocument/didOpen`` notification."""
        await self._notify(
            "textDocument/didOpen",
            {
                "textDocument": {
                    "uri": uri,
                    "languageId": language_id,
                    "version": 0,
                    "text": text,
                },
            },
        )
        logger.debug(
            "lsp: didOpen",
            extra={
                "extra_data": {
                    "server_id": self.server_id,
                    "uri": uri,
                    "language_id": language_id,
                }
            },
        )

    async def did_change(self, uri: str, version: int, text: str) -> None:
        """Send ``textDocument/didChange`` notification (full sync)."""
        await self._notify(
            "textDocument/didChange",
            {
                "textDocument": {"uri": uri, "version": version},
                "contentChanges": [{"text": text}],
            },
        )
        logger.debug(
            "lsp: didChange",
            extra={
                "extra_data": {
                    "server_id": self.server_id,
                    "uri": uri,
                    "version": version,
                }
            },
        )

    async def did_save(self, uri: str) -> None:
        """Send ``textDocument/didSave`` notification."""
        await self._notify(
            "textDocument/didSave",
            {"textDocument": {"uri": uri}},
        )

    # ------------------------------------------------------------------
    # Diagnostics
    # ------------------------------------------------------------------

    def get_diagnostics(self, uri: str | None = None) -> dict[str, list[Diagnostic]]:
        """Return collected diagnostics, optionally filtered to a URI."""
        if uri is not None:
            diags = self._diagnostics.get(uri, [])
            return {uri: diags} if diags else {}
        return dict(self._diagnostics)

    async def wait_for_diagnostics(
        self,
        uri: str,
        *,
        timeout_ms: int = 10_000,
        debounce_ms: int = _DIAGNOSTICS_DEBOUNCE_MS,
    ) -> list[Diagnostic]:
        """Wait for diagnostics for a specific URI.

        Uses a debounce strategy: waits ``debounce_ms`` after the last
        ``publishDiagnostics`` notification for this URI.  If no
        notification arrives within ``timeout_ms``, returns whatever
        diagnostics are available.

        The ``timeout_ms`` is an *absolute* deadline from call start.
        """
        start = monotonic()
        deadline = start + timeout_ms / 1000.0

        # Ensure we have an event for this URI
        if uri not in self._diag_events:
            self._diag_events[uri] = asyncio.Event()
        event = self._diag_events[uri]
        event.clear()

        while True:
            remaining = deadline - monotonic()
            if remaining <= 0:
                logger.debug(
                    "lsp: diagnostics timeout",
                    extra={
                        "extra_data": {
                            "server_id": self.server_id,
                            "uri": uri,
                            "timeout_ms": timeout_ms,
                            "diagnostics_count": len(self._diagnostics.get(uri, [])),
                        }
                    },
                )
                break

            # Wait for a diagnostic event
            try:
                await asyncio.wait_for(event.wait(), timeout=remaining)
            except TimeoutError:
                break

            event.clear()

            # Debounce: wait for silence
            debounce_end = monotonic() + debounce_ms / 1000.0
            settled = True
            while monotonic() < min(debounce_end, deadline):
                wait_time = min(debounce_end - monotonic(), deadline - monotonic())
                if wait_time <= 0:
                    break
                try:
                    await asyncio.wait_for(event.wait(), timeout=wait_time)
                    # Got another diagnostic — reset debounce
                    event.clear()
                    debounce_end = monotonic() + debounce_ms / 1000.0
                    settled = False
                except TimeoutError:
                    settled = True
                    break

            if settled:
                break

        duration_ms = int((monotonic() - start) * 1000)
        diags = self._diagnostics.get(uri, [])
        logger.debug(
            "lsp: diagnostics collected",
            extra={
                "extra_data": {
                    "server_id": self.server_id,
                    "uri": uri,
                    "duration_ms": duration_ms,
                    "count": len(diags),
                    "error_count": sum(1 for d in diags if d.severity == DiagnosticSeverity.ERROR),
                    "warning_count": sum(
                        1 for d in diags if d.severity == DiagnosticSeverity.WARNING
                    ),
                }
            },
        )
        return diags

    # ------------------------------------------------------------------
    # JSON-RPC transport
    # ------------------------------------------------------------------

    async def _request(
        self, method: str, params: dict[str, Any] | None, *, timeout: float = 30.0
    ) -> dict[str, Any]:
        """Send a JSON-RPC request and await the response."""
        process = self.process
        if process is None or process.stdin is None:
            raise RuntimeError("LSP client is not started")

        self._next_id += 1
        request_id = self._next_id

        future: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future

        payload: dict[str, Any] = {
            "jsonrpc": "2.0",
            "id": request_id,
            "method": method,
        }
        if params is not None:
            payload["params"] = params

        await self._send(payload)

        try:
            response = await asyncio.wait_for(future, timeout=timeout)
        except TimeoutError:
            self._pending.pop(request_id, None)
            raise
        finally:
            self._pending.pop(request_id, None)

        if "error" in response:
            error = response["error"]
            logger.warning(
                "lsp: JSON-RPC error",
                extra={
                    "extra_data": {
                        "server_id": self.server_id,
                        "method": method,
                        "error_code": error.get("code"),
                    }
                },
            )
            raise RuntimeError(f"LSP error: {json.dumps(error)}")

        return response.get("result", {})

    async def _notify(self, method: str, params: dict[str, Any] | None) -> None:
        """Send a JSON-RPC notification (no response expected)."""
        payload: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            payload["params"] = params
        await self._send(payload)

    async def _send(self, payload: dict[str, Any]) -> None:
        """Encode and send a Content-Length framed message."""
        process = self.process
        if process is None or process.stdin is None:
            return
        body = json.dumps(payload).encode("utf-8")
        header = f"Content-Length: {len(body)}\r\n\r\n".encode()
        process.stdin.write(header + body)
        await process.stdin.drain()

    # ------------------------------------------------------------------
    # Background reader
    # ------------------------------------------------------------------

    async def _reader_loop(self) -> None:
        """Continuously read server messages and dispatch them."""
        process = self.process
        if process is None or process.stdout is None:
            return

        try:
            while not self._closed:
                message = await self._read_message(process.stdout)
                if message is None:
                    break
                self._dispatch(message)
        except asyncio.CancelledError:
            raise
        except Exception:
            if not self._closed:
                logger.error(
                    "lsp: reader loop error",
                    extra={"extra_data": {"server_id": self.server_id}},
                    exc_info=True,
                )

    async def _read_message(self, reader: asyncio.StreamReader) -> dict[str, Any] | None:
        """Read a single Content-Length framed JSON-RPC message."""
        # Read headers
        headers: dict[str, str] = {}
        while True:
            line = await reader.readline()
            if not line:
                return None  # EOF
            line_str = line.decode("utf-8").strip()
            if not line_str:
                break  # Empty line = end of headers
            if ":" in line_str:
                key, value = line_str.split(":", 1)
                headers[key.strip().lower()] = value.strip()

        content_length = int(headers.get("content-length", "0"))
        if content_length <= 0:
            return None

        body = await reader.readexactly(content_length)
        data = json.loads(body.decode("utf-8"))
        if not isinstance(data, dict):
            return None
        return data

    def _dispatch(self, message: dict[str, Any]) -> None:
        """Route a received message to the right handler."""
        # Response to a pending request
        if "id" in message and "method" not in message:
            request_id = message["id"]
            future = self._pending.get(request_id)
            if future is not None and not future.done():
                future.set_result(message)
            return

        # Notification from server
        method = message.get("method", "")
        params = message.get("params", {})

        if method == "textDocument/publishDiagnostics":
            self._handle_publish_diagnostics(params)
        elif method == "window/logMessage":
            self._handle_log_message(params)
        elif method == "workspace/configuration":
            # Some servers request configuration — respond with empty
            if "id" in message:
                items = params.get("items", [])
                asyncio.create_task(self._respond(message["id"], [{}] * len(items)))
        elif method == "client/registerCapability" and "id" in message:
            # Accept dynamic registration requests
            asyncio.create_task(self._respond(message["id"], None))

    async def _respond(self, request_id: Any, result: Any) -> None:
        """Send a JSON-RPC response to a server-initiated request."""
        await self._send({"jsonrpc": "2.0", "id": request_id, "result": result})

    def _handle_publish_diagnostics(self, params: dict[str, Any]) -> None:
        """Process a ``textDocument/publishDiagnostics`` notification."""
        uri = params.get("uri", "")
        raw_diagnostics = params.get("diagnostics", [])

        diagnostics: list[Diagnostic] = []
        for raw in raw_diagnostics:
            if not isinstance(raw, dict):
                continue
            try:
                raw_range = raw.get("range", {})
                raw_start = raw_range.get("start", {})
                raw_end = raw_range.get("end", {})
                diag = Diagnostic(
                    range=Range(
                        start=Position(
                            line=raw_start.get("line", 0),
                            character=raw_start.get("character", 0),
                        ),
                        end=Position(
                            line=raw_end.get("line", 0),
                            character=raw_end.get("character", 0),
                        ),
                    ),
                    severity=raw.get("severity"),
                    code=raw.get("code"),
                    source=raw.get("source"),
                    message=raw.get("message", ""),
                )
                diagnostics.append(diag)
            except Exception:
                continue

        self._diagnostics[uri] = diagnostics

        # Signal waiters
        event = self._diag_events.get(uri)
        if event is not None:
            event.set()

        logger.debug(
            "lsp: publishDiagnostics received",
            extra={
                "extra_data": {
                    "server_id": self.server_id,
                    "uri": uri,
                    "count": len(diagnostics),
                    "error_count": sum(
                        1 for d in diagnostics if d.severity == DiagnosticSeverity.ERROR
                    ),
                    "warning_count": sum(
                        1 for d in diagnostics if d.severity == DiagnosticSeverity.WARNING
                    ),
                }
            },
        )

    def _handle_log_message(self, params: dict[str, Any]) -> None:
        """Process a ``window/logMessage`` notification."""
        msg_type = params.get("type", 4)  # 1=Error, 2=Warning, 3=Info, 4=Log
        # We log the message type but NOT the content (may contain code)
        if msg_type <= 2:
            logger.debug(
                "lsp: server log message",
                extra={
                    "extra_data": {
                        "server_id": self.server_id,
                        "type": msg_type,
                    }
                },
            )

    async def _drain_stderr(self) -> None:
        """Read and log stderr from the server process."""
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
                    # Truncate to avoid logging large messages
                    logger.debug(
                        "lsp: server stderr",
                        extra={
                            "extra_data": {
                                "server_id": self.server_id,
                                "line": text[:200],
                            }
                        },
                    )
        except asyncio.CancelledError:
            raise

    @property
    def is_alive(self) -> bool:
        """Check if the server process is still running."""
        return self.process is not None and self.process.returncode is None and not self._closed
