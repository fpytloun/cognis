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
    DiagnosticFreshness,
    DiagnosticSeverity,
    DiagnosticSnapshot,
    DiagnosticWaitResult,
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
        workspace_configuration: dict[str, Any] | None = None,
    ) -> None:
        self.server_id = server_id
        self.command = command
        self.args = args
        self.root_uri = root_uri
        self.env: dict[str, str] = {**os.environ, **(env or {})}
        self.init_options = init_options
        self.workspace_configuration = workspace_configuration

        self.process: asyncio.subprocess.Process | None = None
        self.server_name: str | None = None

        # Request/response
        self._next_id = 0
        self._pending: dict[int, asyncio.Future[dict[str, Any]]] = {}

        # Diagnostics state
        self._diagnostics: dict[str, list[Diagnostic]] = {}
        self._diagnostic_snapshots: dict[str, DiagnosticSnapshot] = {}
        self._diag_events: dict[str, asyncio.Event] = {}
        self._pending_diagnostics: set[str] = set()
        self._document_versions: dict[str, int] = {}
        self._document_update_sequences: dict[str, int] = {}
        self._diagnostic_sequence = 0
        self._last_waits: list[DiagnosticWaitResult] = []

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
        version = 0
        self._mark_document_updated(uri, version)
        await self._notify(
            "textDocument/didOpen",
            {
                "textDocument": {
                    "uri": uri,
                    "languageId": language_id,
                    "version": version,
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
                    "version": version,
                }
            },
        )

    async def did_change(self, uri: str, version: int, text: str) -> None:
        """Send ``textDocument/didChange`` notification (full sync)."""
        self._mark_document_updated(uri, version)
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
        logger.debug(
            "lsp: didSave",
            extra={
                "extra_data": {
                    "server_id": self.server_id,
                    "uri": uri,
                    "version": self._document_versions.get(uri),
                }
            },
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

    def get_diagnostic_snapshots(self, uri: str | None = None) -> dict[str, DiagnosticSnapshot]:
        """Return current diagnostic snapshots, optionally filtered to a URI."""
        if uri is not None:
            snapshot = self._diagnostic_snapshots.get(uri)
            return {uri: snapshot} if snapshot is not None else {}
        return dict(self._diagnostic_snapshots)

    def current_document_version(self, uri: str) -> int | None:
        """Return the last document version sent to the server for a URI."""
        return self._document_versions.get(uri)

    def has_pending_diagnostics(self, uri: str) -> bool:
        """Return whether a diagnostics wait is already active for a URI."""
        return uri in self._pending_diagnostics

    def has_cached_diagnostics(self, uri: str) -> bool:
        """Return whether this client has seen diagnostics for a URI."""
        return uri in self._diagnostics

    async def wait_for_diagnostics(
        self,
        uri: str,
        *,
        target_version: int | None = None,
        timeout_ms: int = 10_000,
        debounce_ms: int = _DIAGNOSTICS_DEBOUNCE_MS,
    ) -> DiagnosticWaitResult:
        """Wait for diagnostics for a specific URI.

        Uses a debounce strategy: waits ``debounce_ms`` after the last
        ``publishDiagnostics`` notification for this URI.  If no
        matching fresh notification arrives within ``timeout_ms``, returns
        an explicit timeout result instead of pretending stale cache is fresh.

        The ``timeout_ms`` is an *absolute* deadline from call start.
        """
        start = monotonic()
        deadline = start + timeout_ms / 1000.0
        target_version = (
            target_version if target_version is not None else self._document_versions.get(uri)
        )

        # Ensure we have an event for this URI
        if uri not in self._diag_events:
            self._diag_events[uri] = asyncio.Event()
        event = self._diag_events[uri]
        event.clear()
        self._pending_diagnostics.add(uri)
        snapshot = None
        if self._matching_snapshot(uri, target_version) is not None:
            # A matching snapshot may arrive between didChange/didSave and the
            # wait setup.  Treat it as an immediate event but still pass
            # through the debounce window so a follow-up batch can supersede it.
            event.set()

        try:
            while snapshot is None:
                remaining = deadline - monotonic()
                if remaining <= 0:
                    cached_snapshot = self._diagnostic_snapshots.get(uri)
                    logger.debug(
                        "lsp: diagnostics timeout",
                        extra={
                            "extra_data": {
                                "server_id": self.server_id,
                                "uri": uri,
                                "timeout_ms": timeout_ms,
                                "target_version": target_version,
                                "cached_version": cached_snapshot.diagnostic_version
                                if cached_snapshot is not None
                                else None,
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
                    snapshot = self._matching_snapshot(uri, target_version)
                    if snapshot is not None:
                        break
        finally:
            self._pending_diagnostics.discard(uri)

        duration_ms = int((monotonic() - start) * 1000)
        if snapshot is None:
            diags: list[Diagnostic] = []
            status = DiagnosticFreshness.TIMEOUT
            message: str | None = "timed out waiting for fresh diagnostics"
        else:
            diags = snapshot.diagnostics
            status = snapshot.freshness
            message = snapshot.reason
        result = DiagnosticWaitResult(
            server_id=self.server_id,
            uri=uri,
            target_version=target_version,
            status=status,
            duration_ms=duration_ms,
            snapshot=snapshot,
            message=message,
            error_count=sum(1 for d in diags if d.severity == DiagnosticSeverity.ERROR),
            warning_count=sum(1 for d in diags if d.severity == DiagnosticSeverity.WARNING),
        )
        self._last_waits.append(result)
        self._last_waits = self._last_waits[-20:]
        logger.debug(
            "lsp: diagnostics collected",
            extra={
                "extra_data": {
                    "server_id": self.server_id,
                    "uri": uri,
                    "duration_ms": duration_ms,
                    "target_version": target_version,
                    "status": result.status.value,
                    "diagnostic_version": snapshot.diagnostic_version if snapshot else None,
                    "count": len(diags),
                    "error_count": result.error_count,
                    "warning_count": result.warning_count,
                }
            },
        )
        return result

    def diagnostic_status(self) -> dict[str, Any]:
        """Return compact diagnostic status for observability and /lsp."""
        return {
            "tracked_uri_count": len(self._diagnostic_snapshots),
            "pending_uri_count": len(self._pending_diagnostics),
            "latest_sequence": self._diagnostic_sequence,
            "latest_snapshots": [
                {
                    "uri": uri,
                    "document_version": snapshot.document_version,
                    "diagnostic_version": snapshot.diagnostic_version,
                    "received_sequence": snapshot.received_sequence,
                    "freshness": snapshot.freshness.value,
                    "diagnostic_count": len(snapshot.diagnostics),
                    "error_count": sum(
                        1 for d in snapshot.diagnostics if d.severity == DiagnosticSeverity.ERROR
                    ),
                    "warning_count": sum(
                        1 for d in snapshot.diagnostics if d.severity == DiagnosticSeverity.WARNING
                    ),
                }
                for uri, snapshot in sorted(self._diagnostic_snapshots.items())
            ],
            "last_waits": [
                {
                    "uri": wait.uri,
                    "target_version": wait.target_version,
                    "status": wait.status.value,
                    "duration_ms": wait.duration_ms,
                    "error_count": wait.error_count,
                    "warning_count": wait.warning_count,
                    "message": wait.message,
                }
                for wait in self._last_waits
            ],
        }

    # ------------------------------------------------------------------
    # Query operations
    # ------------------------------------------------------------------

    async def definition(self, file_path: str, line: int, character: int) -> list[dict[str, Any]]:
        """Return definitions at the given position."""

        result = await self._request(
            "textDocument/definition",
            _position_params(file_path, line, character),
        )
        return _normalize_lsp_result_list(result)

    async def references(self, file_path: str, line: int, character: int) -> list[dict[str, Any]]:
        """Return references at the given position."""

        result = await self._request(
            "textDocument/references",
            {
                **_position_params(file_path, line, character),
                "context": {"includeDeclaration": True},
            },
        )
        return _normalize_lsp_result_list(result)

    async def hover(self, file_path: str, line: int, character: int) -> dict[str, Any] | None:
        """Return hover information at the given position."""

        result = await self._request(
            "textDocument/hover",
            _position_params(file_path, line, character),
        )
        return result if isinstance(result, dict) else None

    async def document_symbol(self, file_path: str) -> list[dict[str, Any]]:
        """Return document symbols for a file."""

        result = await self._request(
            "textDocument/documentSymbol",
            {"textDocument": {"uri": file_uri(file_path)}},
        )
        return _normalize_lsp_result_list(result)

    async def workspace_symbol(self, query: str) -> list[dict[str, Any]]:
        """Return workspace symbols matching a query."""

        result = await self._request("workspace/symbol", {"query": query})
        return _normalize_lsp_result_list(result)

    async def implementation(
        self, file_path: str, line: int, character: int
    ) -> list[dict[str, Any]]:
        """Return implementations at the given position."""

        result = await self._request(
            "textDocument/implementation",
            _position_params(file_path, line, character),
        )
        return _normalize_lsp_result_list(result)

    # ------------------------------------------------------------------
    # JSON-RPC transport
    # ------------------------------------------------------------------

    async def _request(
        self, method: str, params: dict[str, Any] | None, *, timeout: float = 30.0
    ) -> Any:
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

        return response.get("result")

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
            # Some servers request configuration during initialization.
            if "id" in message:
                items = params.get("items", [])
                response = self._configuration_response(items)
                requested_sections = self._configuration_sections(items)
                configured_sections = list((self.workspace_configuration or {}).keys())
                logger.info(
                    "lsp: workspace configuration requested "
                    f"server_id={self.server_id} requested_sections={requested_sections} "
                    f"configured_sections={configured_sections} response_count={len(response)}",
                    extra={
                        "extra_data": {
                            "server_id": self.server_id,
                            "requested_sections": requested_sections,
                            "configured_sections": configured_sections,
                            "response_count": len(response),
                        }
                    },
                )
                asyncio.create_task(self._respond(message["id"], response))
        elif method == "client/registerCapability" and "id" in message:
            # Accept dynamic registration requests
            asyncio.create_task(self._respond(message["id"], None))

    async def _respond(self, request_id: Any, result: Any) -> None:
        """Send a JSON-RPC response to a server-initiated request."""
        await self._send({"jsonrpc": "2.0", "id": request_id, "result": result})

    def _configuration_response(self, items: Any) -> list[dict[str, Any]]:
        """Return workspace/configuration results for server-requested sections."""
        if not isinstance(items, list):
            return []

        config = self.workspace_configuration or {}
        response: list[dict[str, Any]] = []
        for item in items:
            section = item.get("section") if isinstance(item, dict) else None
            if isinstance(section, str) and section in config:
                section_config = config[section]
                response.append(section_config if isinstance(section_config, dict) else {})
            else:
                response.append({})
        return response

    def _configuration_sections(self, items: Any) -> list[str | None]:
        """Return requested workspace configuration section names for logging."""
        if not isinstance(items, list):
            return []
        sections: list[str | None] = []
        for item in items:
            section = item.get("section") if isinstance(item, dict) else None
            sections.append(section if isinstance(section, str) else None)
        return sections

    def _handle_publish_diagnostics(self, params: dict[str, Any]) -> None:
        """Process a ``textDocument/publishDiagnostics`` notification."""
        uri = params.get("uri", "")
        raw_version = params.get("version")
        diagnostic_version = raw_version if isinstance(raw_version, int) else None
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

        self._diagnostic_sequence += 1
        received_sequence = self._diagnostic_sequence
        document_version = self._document_versions.get(uri)
        min_sequence = self._document_update_sequences.get(uri, 0)
        freshness = _diagnostic_freshness(
            document_version=document_version,
            diagnostic_version=diagnostic_version,
            received_sequence=received_sequence,
            min_sequence=min_sequence,
        )
        reason = _diagnostic_freshness_reason(
            document_version=document_version,
            diagnostic_version=diagnostic_version,
            received_sequence=received_sequence,
            min_sequence=min_sequence,
        )
        snapshot = DiagnosticSnapshot(
            server_id=self.server_id,
            uri=uri,
            document_version=document_version,
            diagnostic_version=diagnostic_version,
            received_sequence=received_sequence,
            received_at_monotonic=monotonic(),
            diagnostics=diagnostics,
            freshness=freshness,
            reason=reason,
        )

        previous = self._diagnostic_snapshots.get(uri)
        if freshness is DiagnosticFreshness.STALE:
            logger.debug(
                "lsp: stale publishDiagnostics discarded",
                extra={
                    "extra_data": {
                        "server_id": self.server_id,
                        "uri": uri,
                        "document_version": document_version,
                        "diagnostic_version": diagnostic_version,
                        "received_sequence": received_sequence,
                        "reason": reason,
                    }
                },
            )
        else:
            if previous is None or _should_replace_snapshot(previous, snapshot):
                self._diagnostic_snapshots[uri] = snapshot
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
                    "document_version": document_version,
                    "diagnostic_version": diagnostic_version,
                    "received_sequence": received_sequence,
                    "freshness": freshness.value,
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

    def _mark_document_updated(self, uri: str, version: int) -> None:
        self._document_versions[uri] = version
        self._document_update_sequences[uri] = self._diagnostic_sequence + 1
        logger.debug(
            "lsp: document version advanced",
            extra={
                "extra_data": {
                    "server_id": self.server_id,
                    "uri": uri,
                    "version": version,
                    "minimum_diagnostic_sequence": self._document_update_sequences[uri],
                }
            },
        )

    def _matching_snapshot(self, uri: str, target_version: int | None) -> DiagnosticSnapshot | None:
        snapshot = self._diagnostic_snapshots.get(uri)
        if snapshot is None or not snapshot.is_fresh:
            return None
        if target_version is None:
            return snapshot
        if snapshot.diagnostic_version is not None:
            return snapshot if snapshot.diagnostic_version >= target_version else None
        min_sequence = self._document_update_sequences.get(uri, 0)
        return snapshot if snapshot.received_sequence >= min_sequence else None

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


def _diagnostic_freshness(
    *,
    document_version: int | None,
    diagnostic_version: int | None,
    received_sequence: int,
    min_sequence: int,
) -> DiagnosticFreshness:
    if diagnostic_version is not None and document_version is not None:
        return (
            DiagnosticFreshness.FRESH
            if diagnostic_version >= document_version
            else DiagnosticFreshness.STALE
        )
    if diagnostic_version is None:
        return (
            DiagnosticFreshness.FRESH_UNVERSIONED
            if received_sequence >= min_sequence
            else DiagnosticFreshness.STALE
        )
    return DiagnosticFreshness.FRESH


def _diagnostic_freshness_reason(
    *,
    document_version: int | None,
    diagnostic_version: int | None,
    received_sequence: int,
    min_sequence: int,
) -> str | None:
    if diagnostic_version is not None and document_version is not None:
        if diagnostic_version < document_version:
            return (
                f"diagnostic version {diagnostic_version} is older than document "
                f"version {document_version}"
            )
        return None
    if diagnostic_version is None:
        if received_sequence < min_sequence:
            return (
                f"unversioned diagnostics sequence {received_sequence} predates "
                f"document update sequence {min_sequence}"
            )
        return "server did not include publishDiagnostics.version"
    return None


def _should_replace_snapshot(previous: DiagnosticSnapshot, incoming: DiagnosticSnapshot) -> bool:
    if incoming.diagnostic_version is not None and previous.diagnostic_version is not None:
        return incoming.diagnostic_version >= previous.diagnostic_version
    return incoming.received_sequence >= previous.received_sequence


def _position_params(file_path: str, line: int, character: int) -> dict[str, Any]:
    return {
        "textDocument": {"uri": file_uri(file_path)},
        "position": {"line": line, "character": character},
    }


def _normalize_lsp_result_list(result: dict[str, Any] | list[Any] | None) -> list[dict[str, Any]]:
    if isinstance(result, list):
        return [item for item in result if isinstance(item, dict)]
    if isinstance(result, dict):
        return [result]
    return []
