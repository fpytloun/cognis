"""LSP Manager — orchestrates language server lifecycle and diagnostics.

The manager lazily spawns language servers when files are first accessed,
routes file notifications to the appropriate servers, and collects
diagnostics for tool result injection.

Design properties:
- **Graceful degradation**: LSP failures never break file operations.
- **Lazy spawning**: Servers start on first file access, not at boot.
- **Bounded waiting**: ``wait=True`` waits for first-use diagnostics within
  bounded spawn and diagnostics timeouts.
- **Bounded resources**: ``max_concurrent_servers`` caps memory usage.
- **Idle timeout**: Unused servers are shut down automatically.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import shutil
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from time import monotonic, perf_counter
from typing import Any

from prometheus_client import Counter, Gauge, Histogram

from cognis.logging import get_logger
from cognis.tools.executor.lsp.client import (
    MAX_RESPONSE_ITEMS,
    BoundedResultList,
    LSPClient,
    _normalize_lsp_result_list,
    file_uri,
    uri_to_path,
)
from cognis.tools.executor.lsp.hierarchy import (
    Direction,
    HierarchyLimits,
    HierarchyResult,
    traverse_call_hierarchy,
)
from cognis.tools.executor.lsp.install import get_cache_dir, resolve_command
from cognis.tools.executor.lsp.query import QueryOutcome, QueryStatus
from cognis.tools.executor.lsp.servers import LSPServerDefinition, get_servers_for_extension
from cognis.tools.executor.lsp.types import (
    Diagnostic,
    DiagnosticCollection,
    DiagnosticFreshness,
    DiagnosticSnapshot,
    DiagnosticWaitResult,
)

logger = get_logger(__name__)

# ---------------------------------------------------------------------------
# Prometheus metrics
# ---------------------------------------------------------------------------

LSP_SPAWN_DURATION = Histogram(
    "cognis_lsp_spawn_duration_seconds",
    "Time to spawn an LSP server",
    labelnames=("server_id", "outcome"),
)
LSP_DIAGNOSTICS_WAIT = Histogram(
    "cognis_lsp_diagnostics_wait_seconds",
    "Time waiting for LSP diagnostics after file edit",
    labelnames=("server_id",),
)
LSP_DIAGNOSTICS_TOTAL = Counter(
    "cognis_lsp_diagnostics_total",
    "Total LSP diagnostics received",
    labelnames=("severity",),
)
LSP_ACTIVE_SERVERS = Gauge(
    "cognis_lsp_active_servers",
    "Number of currently active LSP server processes",
)
LSP_ERRORS_TOTAL = Counter(
    "cognis_lsp_errors_total",
    "Total LSP errors",
    labelnames=("error_type",),
)
LSP_SPAWN_REJECTED = Counter(
    "cognis_lsp_spawn_rejected_total",
    "LSP server spawns rejected due to concurrent server limit",
)

# Runtime metadata key for the LSP manager instance
LSP_MANAGER_KEY = "lsp_manager"

# How often to check for idle servers
_IDLE_CHECK_INTERVAL = 60.0

# Retry-after for broken servers (5 minutes)
_BROKEN_RETRY_SECONDS = 300.0

# Heavy workspace analyzers are useful for project files but pathological for
# temporary scratch copies because every unique temp directory becomes a new root.
_SCRATCH_ROOTS = ("/tmp", "/var/tmp")
_SCRATCH_DISABLED_SERVERS = frozenset({"pyright"})

# After spawning a server, the first explicit query waits briefly for the
# server's project-loading progress so it is not answered from a partial
# program.  Servers that report no progress cost only the grace period once.
_COLD_START_PROGRESS_GRACE_S = 0.3
_COLD_START_PROGRESS_TIMEOUT_S = 8.0

# Server responses are untrusted; the client inspects a bounded prefix and
# the manager reports truncation from the raw length (see MAX_RESPONSE_ITEMS).
_MAX_RESPONSE_ITEMS = MAX_RESPONSE_ITEMS

# Explicit query requests are bounded separately from diagnostics waits.
_DEFAULT_QUERY_TIMEOUT_S = 8.0

# Bound on cached document symbol answers (one entry per absolute path).
SYMBOL_CACHE_MAX_ENTRIES = 512


@dataclass(slots=True, frozen=True)
class PreparedClient:
    """A live client with the target document synchronized."""

    client: LSPClient
    client_key: str
    uri: str
    language_id: str
    version: int


class LSPManager:
    """Manages LSP client lifecycle, file routing, and diagnostics aggregation."""

    def __init__(
        self,
        *,
        enabled: bool = True,
        auto_install: bool = False,
        diagnostics_timeout_ms: int = 10_000,
        idle_timeout_seconds: int = 600,
        max_concurrent_servers: int = 8,
        cache_dir: Path | None = None,
        python_type_diagnostics: bool = False,
    ) -> None:
        self.enabled = enabled
        self.auto_install = auto_install
        self.diagnostics_timeout_ms = diagnostics_timeout_ms
        self.idle_timeout_seconds = idle_timeout_seconds
        self.max_concurrent_servers = max_concurrent_servers
        self.python_type_diagnostics = python_type_diagnostics
        self.cache_dir = cache_dir or get_cache_dir()

        # Active clients keyed by "{server_id}:{root_path}"
        self._clients: dict[str, LSPClient] = {}

        # Broken server+root combos with retry-after timestamp
        self._broken: dict[str, float] = {}

        # Dedup concurrent spawns
        self._spawning: dict[str, asyncio.Task[LSPClient | None]] = {}

        # Clients spawned by a query path that have not yet had their
        # cold-start progress wait.
        self._cold_clients: set[str] = set()

        # File version tracking for didChange, per client and URI.
        self._file_versions: dict[tuple[str, str], int] = {}

        # Files that have been opened on each client
        self._opened_files: dict[str, set[str]] = {}  # client_key → set of URIs

        # Last access time per client for idle timeout
        self._last_access: dict[str, float] = {}

        # Document symbol cache keyed by absolute path; value carries the
        # on-disk freshness key the outcomes were computed for.
        self._symbol_cache: dict[str, tuple[tuple[int, int], list[QueryOutcome]]] = {}

        # Background idle check task
        self._idle_check_task: asyncio.Task[None] | None = None
        self._cleanup_done = False

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def touch_file(
        self,
        file_path: str,
        *,
        wait: bool = True,
        save: bool = False,
        purpose: str = "diagnostics",
    ) -> DiagnosticCollection:
        """Notify LSP servers of a file change.

        Finds matching server definitions by file extension, lazily spawns
        clients, and sends ``didOpen``/``didChange`` notifications.

        Args:
            file_path: Absolute path to the file.
            wait: If True, wait for diagnostics (with timeout).
            save: If True, send didSave after didOpen/didChange.
            purpose: ``diagnostics`` for edit-time diagnostics, ``semantic`` for explicit LSP queries.
        """
        if not self.enabled:
            return DiagnosticCollection()

        prepared = await self._sync_file(file_path, purpose=purpose, wait_spawn=wait, save=save)
        if not wait:
            return DiagnosticCollection()

        # Wait for diagnostics from all notified clients concurrently
        wait_results: list[DiagnosticWaitResult] = []
        if prepared:
            results = await asyncio.gather(
                *(self._wait_client_diagnostics(p.client, p.uri, p.version) for p in prepared),
                return_exceptions=True,
            )
            wait_results = [
                result for result in results if isinstance(result, DiagnosticWaitResult)
            ]
        snapshots_by_path: dict[str, list[DiagnosticSnapshot]] = {}
        for wait_result in wait_results:
            if wait_result.snapshot is None:
                continue
            path = uri_to_path(wait_result.snapshot.uri)
            snapshots_by_path.setdefault(path, []).append(wait_result.snapshot)
        return DiagnosticCollection(
            waits=wait_results,
            snapshots_by_path=snapshots_by_path,
        )

    async def prepare_file(self, file_path: str) -> list[PreparedClient]:
        """Prepare semantic clients for a file without waiting for diagnostics.

        Spawns or reuses every semantic server for the file extension, waits
        for initialization, and synchronizes the current document text.
        Diagnostics are not normally awaited. A cold TypeScript server needs
        one bounded project-ready barrier before semantic navigation.
        """
        if not self.enabled:
            return []
        prepared = await self._sync_file(file_path, purpose="semantic", wait_spawn=True, save=False)
        # A freshly spawned server may still be loading its project (tsserver
        # answers from an inferred single-file project until then).  Wait
        # once, bounded, for its work-done progress to settle.
        for entry in prepared:
            if entry.client_key not in self._cold_clients:
                continue
            if entry.client.server_id == "typescript":
                # tsserver can answer from an inferred single-file project
                # before it reports any work-done progress. A first diagnostic
                # publication establishes the configured project's imports.
                try:
                    await asyncio.wait_for(
                        self._wait_client_diagnostics(entry.client, entry.uri, entry.version),
                        timeout=_COLD_START_PROGRESS_TIMEOUT_S,
                    )
                except TimeoutError:
                    logger.info("lsp: TypeScript project-ready diagnostics wait timed out")
            # Concurrent first queries all wait; the key is released afterwards.
            idle = await entry.client.wait_for_progress_idle(
                grace_s=_COLD_START_PROGRESS_GRACE_S, timeout_s=_COLD_START_PROGRESS_TIMEOUT_S
            )
            self._cold_clients.discard(entry.client_key)
            if not idle:
                logger.info(
                    "lsp: server still loading after cold-start wait",
                    extra={"extra_data": {"server_id": entry.client.server_id}},
                )
        return prepared

    async def _sync_file(
        self,
        file_path: str,
        *,
        purpose: str,
        wait_spawn: bool,
        save: bool,
    ) -> list[PreparedClient]:
        """Route a file to its servers and send didOpen/didChange.

        Returns the clients that received the notification.  Servers that
        are broken, over the concurrency limit, or still spawning when
        ``wait_spawn`` is False are skipped.
        """
        abs_path = os.path.abspath(file_path)
        ext = os.path.splitext(abs_path)[1].lower()
        if not ext:
            return []
        if purpose == "diagnostics":
            # An edit went through: cached structure for this file is stale
            # even if the on-disk freshness key happens to collide.
            self._symbol_cache.pop(abs_path, None)

        servers = get_servers_for_extension(
            ext,
            purpose="diagnostics" if purpose == "diagnostics" else "semantic",
            python_type_diagnostics=self.python_type_diagnostics,
        )
        if not servers:
            logger.debug(
                "lsp: no server for extension",
                extra={"extra_data": {"extension": ext}},
            )
            return []

        # Start idle check task if not running
        if self._idle_check_task is None or self._idle_check_task.done():
            self._idle_check_task = asyncio.create_task(
                self._idle_check_loop(), name="lsp-idle-check"
            )

        prepared: list[PreparedClient] = []

        for server_def in servers:
            root_path = _find_project_root(abs_path, server_def.root_markers)
            client_key = f"{server_def.server_id}:{root_path}"
            if _should_skip_server_for_path(server_def.server_id, abs_path, root_path):
                logger.info(
                    "lsp: skipping server for scratch path "
                    f"server_id={server_def.server_id} root_path={root_path} file_path={abs_path}",
                    extra={
                        "extra_data": {
                            "server_id": server_def.server_id,
                            "root_path": root_path,
                            "file_path": abs_path,
                        }
                    },
                )
                continue

            # Check if broken (with retry-after)
            broken_until = self._broken.get(client_key)
            if broken_until is not None:
                if monotonic() < broken_until:
                    continue
                # Retry-after expired — remove from broken
                del self._broken[client_key]

            # Get or spawn client
            client = self._clients.get(client_key)
            if client is not None and not client.is_alive:
                # Client died — remove and try to respawn
                logger.warning(
                    "lsp: client process died",
                    extra={"extra_data": {"server_id": server_def.server_id, "root": root_path}},
                )
                await self._remove_client(client_key)
                client = None

            if client is None:
                # Dedup concurrent spawns
                if client_key in self._spawning:
                    spawn_task = self._spawning[client_key]
                    logger.debug(
                        "lsp: reusing existing spawn",
                        extra={"extra_data": {"client_key": client_key}},
                    )
                else:
                    # Reserve starts before their first await; reuse needs no new slot.
                    occupied_slots = len(self._clients.keys() | self._spawning.keys())
                    if occupied_slots >= self.max_concurrent_servers:
                        logger.debug(
                            "lsp: concurrent server limit reached",
                            extra={
                                "extra_data": {
                                    "server_id": server_def.server_id,
                                    "active": len(self._clients),
                                    "starting": len(self._spawning),
                                    "max": self.max_concurrent_servers,
                                }
                            },
                        )
                        LSP_SPAWN_REJECTED.inc()
                        continue
                    spawn_task = asyncio.create_task(
                        self._spawn_client(server_def, root_path, client_key)
                    )
                    self._spawning[client_key] = spawn_task

                if wait_spawn:
                    try:
                        client = await asyncio.wait_for(spawn_task, timeout=15.0)
                    except (TimeoutError, Exception):
                        LSP_ERRORS_TOTAL.labels(error_type="spawn_wait").inc()
                        continue
                else:
                    continue

            if client is None:
                continue

            # Update access time
            self._last_access[client_key] = monotonic()

            # Send file notification
            uri = file_uri(abs_path)
            language_id = server_def.language_id(ext)

            opened_set = self._opened_files.setdefault(client_key, set())
            try:
                if uri not in opened_set:
                    # First time this file on this client — didOpen
                    text = await asyncio.to_thread(Path(abs_path).read_text, "utf-8")
                    await client.did_open(uri, language_id, text)
                    opened_set.add(uri)
                    version = 0
                    self._file_versions[(client_key, uri)] = version
                else:
                    # Subsequent change — didChange
                    version = self._file_versions.get((client_key, uri), 0) + 1
                    self._file_versions[(client_key, uri)] = version
                    text = await asyncio.to_thread(Path(abs_path).read_text, "utf-8")
                    await client.did_change(uri, version, text)
                if save:
                    await client.did_save(uri)
            except Exception:
                logger.debug(
                    "lsp: file notification failed",
                    extra={"extra_data": {"server_id": server_def.server_id, "uri": uri}},
                )
                LSP_ERRORS_TOTAL.labels(error_type="notification").inc()
                continue

            prepared.append(
                PreparedClient(
                    client=client,
                    client_key=client_key,
                    uri=uri,
                    language_id=language_id,
                    version=version,
                )
            )

        return prepared

    async def _wait_client_diagnostics(
        self, client: LSPClient, uri: str, target_version: int | None
    ) -> DiagnosticWaitResult:
        """Wait for diagnostics from a single client with metrics."""
        start = perf_counter()
        try:
            result = await client.wait_for_diagnostics(
                uri,
                target_version=target_version,
                timeout_ms=self.diagnostics_timeout_ms,
            )
            # Record metrics
            if result.snapshot is not None:
                for d in result.snapshot.diagnostics:
                    if d.severity is not None:
                        LSP_DIAGNOSTICS_TOTAL.labels(severity=d.severity.name.lower()).inc()
            return result
        except Exception:
            LSP_ERRORS_TOTAL.labels(error_type="diagnostics_wait").inc()
            return DiagnosticWaitResult(
                server_id=client.server_id,
                uri=uri,
                target_version=target_version,
                status=DiagnosticFreshness.FAILED,
                duration_ms=int((perf_counter() - start) * 1000),
                message="diagnostics wait failed",
            )
        finally:
            LSP_DIAGNOSTICS_WAIT.labels(server_id=client.server_id).observe(perf_counter() - start)

    def has_pending_diagnostics(self, file_paths: list[str]) -> bool:
        """Return whether any active client is already analyzing these files."""
        if not file_paths:
            return False

        uris = {file_uri(os.path.abspath(path)) for path in file_paths}
        return any(
            client.has_pending_diagnostics(uri) for client in self._clients.values() for uri in uris
        )

    def has_cached_diagnostics(self, file_paths: list[str]) -> bool:
        """Return whether any active client has diagnostics state for these files."""
        if not file_paths:
            return False

        uris = {file_uri(os.path.abspath(path)) for path in file_paths}
        return any(
            client.has_cached_diagnostics(uri) for client in self._clients.values() for uri in uris
        )

    def get_diagnostics(self, file_path: str | None = None) -> dict[str, list[Diagnostic]]:
        """Return aggregated diagnostics from all active clients.

        If ``file_path`` is provided, results are filtered to that file
        (and any related files with diagnostics from the same servers).
        Paths are converted from URIs to filesystem paths.
        """
        result: dict[str, list[Diagnostic]] = {}

        for client in self._clients.values():
            if file_path is not None:
                uri = file_uri(file_path)
                client_diags = client.get_diagnostics(uri)
            else:
                client_diags = client.get_diagnostics()

            for uri, diags in client_diags.items():
                path = uri_to_path(uri)
                existing = result.get(path, [])
                existing.extend(diags)
                result[path] = existing

        # Also collect diagnostics for related files (other files from same clients)
        if file_path is not None:
            for client in self._clients.values():
                all_diags = client.get_diagnostics()
                for uri, diags in all_diags.items():
                    path = uri_to_path(uri)
                    if path != os.path.abspath(file_path) and path not in result:
                        result[path] = diags

        return result

    def get_diagnostic_snapshots(
        self, file_path: str | None = None
    ) -> dict[str, list[DiagnosticSnapshot]]:
        """Return aggregated diagnostic snapshots from all active clients."""
        result: dict[str, list[DiagnosticSnapshot]] = {}

        for client in self._clients.values():
            if file_path is not None:
                uri = file_uri(file_path)
                client_snapshots = client.get_diagnostic_snapshots(uri)
            else:
                client_snapshots = client.get_diagnostic_snapshots()

            for uri, snapshot in client_snapshots.items():
                path = uri_to_path(uri)
                result.setdefault(path, []).append(snapshot)

        if file_path is not None:
            abs_file = os.path.abspath(file_path)
            for client in self._clients.values():
                for uri, snapshot in client.get_diagnostic_snapshots().items():
                    path = uri_to_path(uri)
                    if path != abs_file and path not in result:
                        result[path] = [snapshot]

        return result

    def status(self) -> dict[str, Any]:
        """Return structured status information for the ``/lsp`` command.

        Returns a dict with configuration, active servers, broken servers,
        and aggregate totals.
        """
        now = monotonic()

        # Configuration
        config = {
            "enabled": self.enabled,
            "auto_install": self.auto_install,
            "diagnostics_timeout_ms": self.diagnostics_timeout_ms,
            "idle_timeout_seconds": self.idle_timeout_seconds,
            "max_concurrent_servers": self.max_concurrent_servers,
        }

        # Active servers
        active_servers: list[dict[str, Any]] = []
        total_files = 0
        total_errors = 0
        total_warnings = 0

        for client_key, client in self._clients.items():
            # Parse server_id and root from key
            parts = client_key.split(":", 1)
            server_id = parts[0] if parts else client_key
            root_path = parts[1] if len(parts) > 1 else ""

            # Count files opened on this client
            opened = self._opened_files.get(client_key, set())
            file_count = len(opened)
            total_files += file_count

            # Count diagnostics by severity
            error_count = 0
            warning_count = 0
            all_diags = client.get_diagnostics()
            for diag_list in all_diags.values():
                for d in diag_list:
                    if d.severity is not None:
                        if d.severity.value == 1:
                            error_count += 1
                        elif d.severity.value == 2:
                            warning_count += 1
            total_errors += error_count
            total_warnings += warning_count

            # Idle time
            last_access = self._last_access.get(client_key, now)
            idle_seconds = int(now - last_access)

            pid = client.process.pid if client.process else None

            active_servers.append(
                {
                    "server_id": server_id,
                    "server_name": client.server_name or server_id,
                    "root_path": root_path,
                    "pid": pid,
                    "alive": client.is_alive,
                    "file_count": file_count,
                    "error_count": error_count,
                    "warning_count": warning_count,
                    "idle_seconds": idle_seconds,
                    "diagnostics": client.diagnostic_status(),
                }
            )

        # Broken servers
        broken_servers: list[dict[str, Any]] = []
        for client_key, broken_until in self._broken.items():
            retry_in = max(0, int(broken_until - now))
            broken_servers.append(
                {
                    "client_key": client_key,
                    "retry_in_seconds": retry_in,
                }
            )

        return {
            "config": config,
            "active_servers": active_servers,
            "broken_servers": broken_servers,
            "spawning_count": len(self._spawning),
            "totals": {
                "active_server_count": len(active_servers),
                "files_tracked": total_files,
                "total_errors": total_errors,
                "total_warnings": total_warnings,
            },
        }

    async def available_servers(self) -> list[dict[str, Any]]:
        """Detect which language servers are available on the system.

        Checks PATH and cache for each built-in server definition.
        Returns a list of dicts with server info and detection status.
        """
        from cognis.tools.executor.lsp.servers import BUILTIN_SERVERS

        results: list[dict[str, Any]] = []
        for server_def in BUILTIN_SERVERS:
            path = await asyncio.to_thread(shutil.which, server_def.command)
            if path is None and server_def.install_strategy is not None:
                cached = await server_def.install_strategy.detect(
                    server_def.server_id, self.cache_dir
                )
                if cached is not None:
                    path = str(cached)

            # Summarise extensions
            exts = sorted(server_def.extensions)
            ext_str = ", ".join(exts[:4])
            if len(exts) > 4:
                ext_str += f" +{len(exts) - 4}"

            # Check if currently active
            active_key = None
            for key in self._clients:
                if key.startswith(f"{server_def.server_id}:"):
                    active_key = key
                    break

            results.append(
                {
                    "server_id": server_def.server_id,
                    "extensions": ext_str,
                    "path": path,
                    "available": path is not None,
                    "has_auto_install": server_def.install_strategy is not None,
                    "active": active_key is not None,
                }
            )
        return results

    async def has_clients(self, file_path: str) -> bool:
        """Return whether any active or spawnable semantic server exists for a file.

        This checks server definitions and scratch-path policy only; it does
        not spawn, and it does not consult the broken-server backoff because
        callers use it to decide whether an LSP attempt is meaningful at all.
        """
        if not self.enabled:
            return False
        abs_path = os.path.abspath(file_path)
        ext = os.path.splitext(abs_path)[1].lower()
        if not ext:
            return False
        for server_def in get_servers_for_extension(ext, purpose="semantic"):
            root_path = _find_project_root(abs_path, server_def.root_markers)
            if not _should_skip_server_for_path(server_def.server_id, abs_path, root_path):
                return True
        return False

    async def query(
        self,
        prepared: list[PreparedClient],
        method: str,
        invoke: Callable[[LSPClient], Awaitable[Any]],
        *,
        timeout_s: float = _DEFAULT_QUERY_TIMEOUT_S,
    ) -> list[QueryOutcome]:
        """Run one request against every prepared client with capability gating.

        ``invoke`` receives a client and performs the actual request.  Each
        server produces exactly one :class:`QueryOutcome`; clients that did
        not negotiate ``method`` are reported as unsupported without a call.
        """
        if not prepared:
            return []

        async def run(entry: PreparedClient) -> QueryOutcome:
            client = entry.client
            if not client.supports(method, uri=entry.uri, language_id=entry.language_id):
                return QueryOutcome(
                    server_id=client.server_id,
                    status=QueryStatus.UNSUPPORTED,
                    message="method not negotiated",
                )
            start = perf_counter()
            try:
                result = await asyncio.wait_for(invoke(client), timeout=timeout_s)
            except TimeoutError:
                return QueryOutcome(
                    server_id=client.server_id,
                    status=QueryStatus.TIMEOUT,
                    duration_ms=int((perf_counter() - start) * 1000),
                    message=f"no response within {timeout_s:g}s",
                )
            except Exception:
                LSP_ERRORS_TOTAL.labels(error_type="query").inc()
                logger.debug(
                    "lsp: query failed",
                    extra={"extra_data": {"server_id": client.server_id, "method": method}},
                    exc_info=True,
                )
                return QueryOutcome(
                    server_id=client.server_id,
                    status=QueryStatus.FAILED,
                    duration_ms=int((perf_counter() - start) * 1000),
                    message="request failed" if client.is_alive else "server exited",
                )
            duration_ms = int((perf_counter() - start) * 1000)
            items = _as_item_list(result)
            # Truncation is decided on the raw reply length, carried by the
            # client normalization, so a malformed entry cannot mask it.
            if items.truncated or len(items) > _MAX_RESPONSE_ITEMS:
                return QueryOutcome(
                    server_id=client.server_id,
                    status=QueryStatus.PARTIAL,
                    items=items[:_MAX_RESPONSE_ITEMS],
                    duration_ms=duration_ms,
                    message=f"response truncated to {_MAX_RESPONSE_ITEMS} items",
                )
            return QueryOutcome(
                server_id=client.server_id,
                status=QueryStatus.OK if items else QueryStatus.EMPTY,
                items=items,
                duration_ms=duration_ms,
            )

        outcomes = await asyncio.gather(*(run(entry) for entry in prepared))
        for entry in prepared:
            self._last_access[entry.client_key] = monotonic()
        return list(outcomes)

    async def definition(self, file_path: str, line: int, character: int) -> list[QueryOutcome]:
        """Return LSP definitions for a file position."""

        prepared = await self.prepare_file(file_path)
        return await self.query(
            prepared,
            "textDocument/definition",
            lambda client: client.definition(file_path, line, character),
        )

    async def references(self, file_path: str, line: int, character: int) -> list[QueryOutcome]:
        """Return LSP references for a file position."""

        prepared = await self.prepare_file(file_path)
        return await self.query(
            prepared,
            "textDocument/references",
            lambda client: client.references(file_path, line, character),
        )

    async def hover(self, file_path: str, line: int, character: int) -> list[QueryOutcome]:
        """Return hover information for a file position."""

        prepared = await self.prepare_file(file_path)
        return await self.query(
            prepared,
            "textDocument/hover",
            lambda client: client.hover(file_path, line, character),
        )

    async def document_symbol(self, file_path: str) -> list[QueryOutcome]:
        """Return document symbols for a file."""

        prepared = await self.prepare_file(file_path)
        return await self.query(
            prepared,
            "textDocument/documentSymbol",
            lambda client: client.document_symbol(file_path),
        )

    async def document_symbol_cached(self, file_path: str) -> tuple[list[QueryOutcome], bool]:
        """Return document symbols, reusing a cached answer while the file is unchanged.

        The freshness key is ``(mtime_ns, size)`` of the file on disk; any
        edit through the executor also evicts the entry.  Answers with a
        transient failure (timeout, failed, partial) are not cached so they
        are retried on the next call; ``unsupported`` is a stable per-server
        fact and does not block caching.  Returns the
        outcomes and whether they came from the cache.
        """
        abs_path = os.path.abspath(file_path)
        try:
            stat = os.stat(abs_path)
        except OSError:
            return await self.document_symbol(abs_path), False
        key = (stat.st_mtime_ns, stat.st_size)
        cached = self._symbol_cache.get(abs_path)
        if cached is not None and cached[0] == key:
            return cached[1], True
        outcomes = await self.document_symbol(abs_path)
        if outcomes and all(
            outcome.status in (QueryStatus.OK, QueryStatus.EMPTY, QueryStatus.UNSUPPORTED)
            for outcome in outcomes
        ):
            if len(self._symbol_cache) >= SYMBOL_CACHE_MAX_ENTRIES:
                oldest = next(iter(self._symbol_cache))
                del self._symbol_cache[oldest]
            self._symbol_cache[abs_path] = (key, outcomes)
        return outcomes, False

    async def workspace_symbol(self, file_path: str, query: str) -> list[QueryOutcome]:
        """Return workspace symbols from relevant clients."""

        prepared = await self.prepare_file(file_path)
        return await self.query(
            prepared,
            "workspace/symbol",
            lambda client: client.workspace_symbol(query),
        )

    async def implementation(self, file_path: str, line: int, character: int) -> list[QueryOutcome]:
        """Return implementations for a file position."""

        prepared = await self.prepare_file(file_path)
        return await self.query(
            prepared,
            "textDocument/implementation",
            lambda client: client.implementation(file_path, line, character),
        )

    async def type_definition(
        self, file_path: str, line: int, character: int
    ) -> list[QueryOutcome]:
        """Return type definitions for a file position."""

        prepared = await self.prepare_file(file_path)
        return await self.query(
            prepared,
            "textDocument/typeDefinition",
            lambda client: client.type_definition(file_path, line, character),
        )

    async def call_hierarchy(
        self,
        file_path: str,
        line: int,
        character: int,
        *,
        direction: Direction,
        limits: HierarchyLimits | None = None,
        cwd: str | None = None,
    ) -> tuple[list[QueryOutcome], HierarchyResult | None]:
        """Run a bounded call hierarchy traversal on one capable server.

        The first prepared client that negotiated call hierarchy owns the
        whole traversal; other servers are reported as unsupported.  Items
        are never routed across clients.
        """
        prepared = await self.prepare_file(file_path)
        outcomes: list[QueryOutcome] = []
        chosen: PreparedClient | None = None
        for entry in prepared:
            if chosen is None and entry.client.supports(
                "textDocument/prepareCallHierarchy",
                uri=entry.uri,
                language_id=entry.language_id,
            ):
                chosen = entry
                continue
            outcomes.append(
                QueryOutcome(
                    server_id=entry.client.server_id,
                    status=QueryStatus.UNSUPPORTED,
                    message="method not negotiated",
                )
            )
        if chosen is None:
            return outcomes, None

        result = await traverse_call_hierarchy(
            chosen.client,
            file_path,
            line,
            character,
            direction=direction,
            limits=limits or HierarchyLimits(),
            cwd=cwd,
        )
        self._last_access[chosen.client_key] = monotonic()
        if result.status is QueryStatus.FAILED:
            LSP_ERRORS_TOTAL.labels(error_type="query").inc()
        outcomes.insert(
            0,
            QueryOutcome(
                server_id=chosen.client.server_id,
                status=result.status,
                items=[root.to_metadata() for root in result.roots],
                duration_ms=result.duration_ms,
                message=result.stop_reason,
            ),
        )
        return outcomes, result

    async def cleanup(self) -> None:
        """Shutdown all LSP clients and cancel background tasks."""
        if self._cleanup_done:
            return
        self._cleanup_done = True

        if self._idle_check_task is not None and not self._idle_check_task.done():
            self._idle_check_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._idle_check_task

        # Cancel pending spawns
        for task in self._spawning.values():
            if not task.done():
                task.cancel()

        # Close all clients
        client_count = len(self._clients)
        start = perf_counter()

        close_tasks = [client.close() for client in self._clients.values()]
        if close_tasks:
            await asyncio.gather(*close_tasks, return_exceptions=True)

        self._clients.clear()
        self._spawning.clear()
        self._broken.clear()
        self._file_versions.clear()
        self._opened_files.clear()
        self._last_access.clear()
        LSP_ACTIVE_SERVERS.set(0)

        duration_ms = int((perf_counter() - start) * 1000)
        logger.info(
            "lsp: manager cleanup complete",
            extra={
                "extra_data": {
                    "client_count": client_count,
                    "duration_ms": duration_ms,
                }
            },
        )

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    async def _spawn_client(
        self,
        server_def: LSPServerDefinition,
        root_path: str,
        client_key: str,
    ) -> LSPClient | None:
        """Spawn and initialize a new LSP client."""
        start = perf_counter()
        logger.info(
            "lsp: spawning client "
            f"server_id={server_def.server_id} root_path={root_path} client_key={client_key}",
            extra={
                "extra_data": {
                    "server_id": server_def.server_id,
                    "root_path": root_path,
                    "client_key": client_key,
                }
            },
        )

        try:
            # Resolve the command (PATH → cache → auto-install)
            resolved = await resolve_command(
                server_def.command,
                server_def.server_id,
                server_def.install_strategy,
                auto_install=self.auto_install,
                cache_dir=self.cache_dir,
            )

            if resolved is None:
                logger.debug(
                    "lsp: server command not found",
                    extra={
                        "extra_data": {
                            "server_id": server_def.server_id,
                            "command": server_def.command,
                        }
                    },
                )
                self._mark_broken(client_key)
                LSP_SPAWN_DURATION.labels(
                    server_id=server_def.server_id, outcome="not_found"
                ).observe(perf_counter() - start)
                return None

            # Build the actual command
            if server_def.npm_run and resolved.endswith((".js", ".mjs", ".cjs")):
                # npm-installed JS entry point — run via node
                node = await asyncio.to_thread(shutil.which, "node")
                if node is None:
                    logger.warning(
                        "lsp: node not found, cannot run npm-installed server",
                        extra={"extra_data": {"server_id": server_def.server_id}},
                    )
                    self._mark_broken(client_key)
                    return None
                command = node
                args = [resolved, *server_def.args]
            else:
                command = resolved
                args = list(server_def.args)

            root_uri = file_uri(root_path)
            client = LSPClient(
                server_id=server_def.server_id,
                command=command,
                args=args,
                root_uri=root_uri,
                init_options=server_def.initialization_options_for(root_path),
                workspace_configuration=server_def.workspace_configuration_for(root_path),
            )
            try:
                await client.start()
            except asyncio.CancelledError:
                # A caller deadline (outline, tool timeout) cancelled the
                # spawn mid-handshake: the process exists but is not yet
                # registered, so nothing else would ever close it.
                with contextlib.suppress(Exception):
                    await asyncio.shield(client.close())
                raise

            # Register client.  Whichever path spawned it (a non-waiting read
            # warm-up included), the first explicit query must wait for
            # project loading.
            self._clients[client_key] = client
            self._cold_clients.add(client_key)
            self._last_access[client_key] = monotonic()
            LSP_ACTIVE_SERVERS.inc()

            duration_s = perf_counter() - start
            LSP_SPAWN_DURATION.labels(server_id=server_def.server_id, outcome="success").observe(
                duration_s
            )

            logger.info(
                "lsp: client spawned successfully "
                f"server_id={server_def.server_id} root_path={root_path} "
                f"client_key={client_key} duration_ms={int(duration_s * 1000)}",
                extra={
                    "extra_data": {
                        "server_id": server_def.server_id,
                        "root_path": root_path,
                        "client_key": client_key,
                        "duration_ms": int(duration_s * 1000),
                    }
                },
            )
            return client

        except Exception:
            logger.warning(
                "lsp: client spawn failed",
                extra={
                    "extra_data": {
                        "server_id": server_def.server_id,
                        "root_path": root_path,
                    }
                },
                exc_info=True,
            )
            self._mark_broken(client_key)
            LSP_SPAWN_DURATION.labels(server_id=server_def.server_id, outcome="failure").observe(
                perf_counter() - start
            )
            LSP_ERRORS_TOTAL.labels(error_type="spawn").inc()
            return None
        finally:
            self._spawning.pop(client_key, None)

    def _mark_broken(self, client_key: str) -> None:
        """Mark a server+root combo as broken with retry-after."""
        self._broken[client_key] = monotonic() + _BROKEN_RETRY_SECONDS
        logger.warning(
            "lsp: server marked broken (retry in %ds)",
            int(_BROKEN_RETRY_SECONDS),
            extra={"extra_data": {"client_key": client_key}},
        )

    async def _remove_client(self, client_key: str) -> None:
        """Remove and close a client."""
        client = self._clients.pop(client_key, None)
        self._opened_files.pop(client_key, None)
        self._last_access.pop(client_key, None)
        self._cold_clients.discard(client_key)
        # Cached outlines carry this client's provenance; drop them so the
        # replacement server is consulted instead of a stale answer.
        self._symbol_cache.clear()
        if client is not None:
            LSP_ACTIVE_SERVERS.dec()
            with contextlib.suppress(Exception):
                await client.close()

    async def _idle_check_loop(self) -> None:
        """Periodically check for and shut down idle LSP servers."""
        try:
            while True:
                await asyncio.sleep(_IDLE_CHECK_INTERVAL)
                now = monotonic()
                to_remove: list[str] = []
                for key, last_access in self._last_access.items():
                    idle_seconds = now - last_access
                    if idle_seconds > self.idle_timeout_seconds:
                        to_remove.append(key)

                for key in to_remove:
                    logger.info(
                        "lsp: shutting down idle server "
                        f"client_key={key} "
                        f"idle_seconds={int(monotonic() - self._last_access.get(key, 0))}",
                        extra={
                            "extra_data": {
                                "client_key": key,
                                "idle_seconds": int(monotonic() - self._last_access.get(key, 0)),
                            }
                        },
                    )
                    await self._remove_client(key)
        except asyncio.CancelledError:
            raise


def _as_item_list(result: Any) -> BoundedResultList:
    """Normalize a query result to a bounded list of items.

    Client query methods already return a ``BoundedResultList`` carrying the
    raw truncation flag; it is passed through unchanged.  Other lists are
    normalized here.  A single dict (for example a ``Hover``) becomes a
    one-item list.  ``None`` and scalars yield no items.
    """
    if isinstance(result, BoundedResultList):
        return result
    return _normalize_lsp_result_list(result if isinstance(result, (list, dict)) else None)


def _find_project_root(file_path: str, root_markers: tuple[str, ...]) -> str:
    """Walk up from the file to find the project root.

    Returns the first directory containing any of the root markers.
    Falls back to the file's parent directory if no marker is found.
    """
    current = Path(file_path).parent
    while True:
        for marker in root_markers:
            if (current / marker).exists():
                return str(current)
        parent = current.parent
        if parent == current:
            break
        current = parent

    # Fallback: file's parent directory
    return str(Path(file_path).parent)


def _should_skip_server_for_path(server_id: str, file_path: str, root_path: str) -> bool:
    """Return whether a heavyweight server should be skipped for scratch files."""
    if server_id not in _SCRATCH_DISABLED_SERVERS:
        return False
    file_resolved = Path(file_path).resolve(strict=False)
    root_resolved = Path(root_path).resolve(strict=False)
    for scratch in _SCRATCH_ROOTS:
        scratch_path = Path(scratch)
        if _is_relative_to(file_resolved, scratch_path) or _is_relative_to(
            root_resolved, scratch_path
        ):
            return True
    return False


def _is_relative_to(path: Path, parent: Path) -> bool:
    """Return whether path is under parent."""
    try:
        path.relative_to(parent)
    except ValueError:
        return False
    return True
