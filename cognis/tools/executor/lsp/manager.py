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
from pathlib import Path
from time import monotonic, perf_counter
from typing import Any

from prometheus_client import Counter, Gauge, Histogram

from cognis.logging import get_logger
from cognis.tools.executor.lsp.client import LSPClient, file_uri, uri_to_path
from cognis.tools.executor.lsp.install import get_cache_dir, resolve_command
from cognis.tools.executor.lsp.servers import LSPServerDefinition, get_servers_for_extension
from cognis.tools.executor.lsp.types import Diagnostic

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
    ) -> None:
        self.enabled = enabled
        self.auto_install = auto_install
        self.diagnostics_timeout_ms = diagnostics_timeout_ms
        self.idle_timeout_seconds = idle_timeout_seconds
        self.max_concurrent_servers = max_concurrent_servers
        self.cache_dir = cache_dir or get_cache_dir()

        # Active clients keyed by "{server_id}:{root_path}"
        self._clients: dict[str, LSPClient] = {}

        # Broken server+root combos with retry-after timestamp
        self._broken: dict[str, float] = {}

        # Dedup concurrent spawns
        self._spawning: dict[str, asyncio.Task[LSPClient | None]] = {}

        # File version tracking for didChange
        self._file_versions: dict[str, int] = {}

        # Files that have been opened on each client
        self._opened_files: dict[str, set[str]] = {}  # client_key → set of URIs

        # Last access time per client for idle timeout
        self._last_access: dict[str, float] = {}

        # Background idle check task
        self._idle_check_task: asyncio.Task[None] | None = None
        self._cleanup_done = False

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def touch_file(self, file_path: str, *, wait: bool = True) -> None:
        """Notify LSP servers of a file change.

        Finds matching server definitions by file extension, lazily spawns
        clients, and sends ``didOpen``/``didChange`` notifications.

        Args:
            file_path: Absolute path to the file.
            wait: If True, wait for diagnostics (with timeout).
        """
        if not self.enabled:
            return

        abs_path = os.path.abspath(file_path)
        ext = os.path.splitext(abs_path)[1].lower()
        if not ext:
            return

        servers = get_servers_for_extension(ext)
        if not servers:
            logger.debug(
                "lsp: no server for extension",
                extra={"extra_data": {"extension": ext}},
            )
            return

        # Start idle check task if not running
        if self._idle_check_task is None or self._idle_check_task.done():
            self._idle_check_task = asyncio.create_task(
                self._idle_check_loop(), name="lsp-idle-check"
            )

        clients_to_wait: list[tuple[LSPClient, str]] = []

        for server_def in servers:
            root_path = _find_project_root(abs_path, server_def.root_markers)
            client_key = f"{server_def.server_id}:{root_path}"

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
                # Check concurrent server limit
                if len(self._clients) >= self.max_concurrent_servers:
                    logger.debug(
                        "lsp: concurrent server limit reached",
                        extra={
                            "extra_data": {
                                "server_id": server_def.server_id,
                                "active": len(self._clients),
                                "max": self.max_concurrent_servers,
                            }
                        },
                    )
                    LSP_SPAWN_REJECTED.inc()
                    continue

                # Dedup concurrent spawns
                if client_key in self._spawning:
                    spawn_task = self._spawning[client_key]
                    logger.debug(
                        "lsp: reusing existing spawn",
                        extra={"extra_data": {"client_key": client_key}},
                    )
                else:
                    spawn_task = asyncio.create_task(
                        self._spawn_client(server_def, root_path, client_key)
                    )
                    self._spawning[client_key] = spawn_task

                if wait:
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
                    self._file_versions[abs_path] = 0
                else:
                    # Subsequent change — didChange
                    version = self._file_versions.get(abs_path, 0) + 1
                    self._file_versions[abs_path] = version
                    text = await asyncio.to_thread(Path(abs_path).read_text, "utf-8")
                    await client.did_change(uri, version, text)
            except Exception:
                logger.debug(
                    "lsp: file notification failed",
                    extra={"extra_data": {"server_id": server_def.server_id, "uri": uri}},
                )
                LSP_ERRORS_TOTAL.labels(error_type="notification").inc()
                continue

            if wait:
                clients_to_wait.append((client, uri))

        # Wait for diagnostics from all notified clients concurrently
        if clients_to_wait:
            await asyncio.gather(
                *(self._wait_client_diagnostics(c, u) for c, u in clients_to_wait),
                return_exceptions=True,
            )

    async def _wait_client_diagnostics(self, client: LSPClient, uri: str) -> None:
        """Wait for diagnostics from a single client with metrics."""
        start = perf_counter()
        try:
            diags = await client.wait_for_diagnostics(uri, timeout_ms=self.diagnostics_timeout_ms)
            # Record metrics
            for d in diags:
                if d.severity is not None:
                    LSP_DIAGNOSTICS_TOTAL.labels(severity=d.severity.name.lower()).inc()
        except Exception:
            LSP_ERRORS_TOTAL.labels(error_type="diagnostics_wait").inc()
        finally:
            LSP_DIAGNOSTICS_WAIT.labels(server_id=client.server_id).observe(perf_counter() - start)

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
        """Return whether any active or spawnable client exists for a file."""

        return bool(await self._clients_for_file(file_path, wait=False))

    async def definition(self, file_path: str, line: int, character: int) -> list[dict[str, Any]]:
        """Return LSP definitions for a file position."""

        clients = await self._clients_for_file(file_path, wait=True)
        return await self._fanout_query(clients, "definition", file_path, line, character)

    async def references(self, file_path: str, line: int, character: int) -> list[dict[str, Any]]:
        """Return LSP references for a file position."""

        clients = await self._clients_for_file(file_path, wait=True)
        return await self._fanout_query(clients, "references", file_path, line, character)

    async def hover(self, file_path: str, line: int, character: int) -> list[dict[str, Any]]:
        """Return hover information for a file position."""

        clients = await self._clients_for_file(file_path, wait=True)
        results = await asyncio.gather(
            *(client.hover(file_path, line, character) for client in clients),
            return_exceptions=True,
        )
        return [result for result in results if isinstance(result, dict)]

    async def document_symbol(self, file_path: str) -> list[dict[str, Any]]:
        """Return document symbols for a file."""

        clients = await self._clients_for_file(file_path, wait=True)
        results = await asyncio.gather(
            *(client.document_symbol(file_path) for client in clients),
            return_exceptions=True,
        )
        return [item for result in results if isinstance(result, list) for item in result]

    async def workspace_symbol(self, file_path: str, query: str) -> list[dict[str, Any]]:
        """Return workspace symbols from relevant clients."""

        clients = await self._clients_for_file(file_path, wait=True)
        results = await asyncio.gather(
            *(client.workspace_symbol(query) for client in clients),
            return_exceptions=True,
        )
        return [item for result in results if isinstance(result, list) for item in result]

    async def implementation(
        self, file_path: str, line: int, character: int
    ) -> list[dict[str, Any]]:
        """Return implementations for a file position."""

        clients = await self._clients_for_file(file_path, wait=True)
        return await self._fanout_query(clients, "implementation", file_path, line, character)

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

    async def _clients_for_file(self, file_path: str, *, wait: bool) -> list[LSPClient]:
        abs_path = os.path.abspath(file_path)
        ext = os.path.splitext(abs_path)[1].lower()
        if not ext:
            return []
        servers = get_servers_for_extension(ext)
        if not servers:
            return []
        clients: list[LSPClient] = []
        for server_def in servers:
            root_path = _find_project_root(abs_path, server_def.root_markers)
            client_key = f"{server_def.server_id}:{root_path}"
            client = self._clients.get(client_key)
            if client is None or not client.is_alive:
                if not wait:
                    continue
                client = await self._spawn_or_reuse_client(server_def, root_path, client_key)
            if client is not None:
                self._last_access[client_key] = monotonic()
                clients.append(client)
        return clients

    async def _spawn_or_reuse_client(
        self, server_def: LSPServerDefinition, root_path: str, client_key: str
    ) -> LSPClient | None:
        if client_key in self._spawning:
            spawn_task = self._spawning[client_key]
        else:
            spawn_task = asyncio.create_task(self._spawn_client(server_def, root_path, client_key))
            self._spawning[client_key] = spawn_task
        try:
            return await asyncio.wait_for(spawn_task, timeout=15.0)
        except (TimeoutError, Exception):
            return None

    async def _fanout_query(
        self,
        clients: list[LSPClient],
        method_name: str,
        file_path: str,
        line: int,
        character: int,
    ) -> list[dict[str, Any]]:
        results = await asyncio.gather(
            *(getattr(client, method_name)(file_path, line, character) for client in clients),
            return_exceptions=True,
        )
        return [item for result in results if isinstance(result, list) for item in result]

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
            "lsp: spawning client",
            extra={
                "extra_data": {
                    "server_id": server_def.server_id,
                    "root_path": root_path,
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
                init_options=server_def.init_options,
            )
            await client.start()

            # Register client
            self._clients[client_key] = client
            self._last_access[client_key] = monotonic()
            LSP_ACTIVE_SERVERS.inc()

            duration_s = perf_counter() - start
            LSP_SPAWN_DURATION.labels(server_id=server_def.server_id, outcome="success").observe(
                duration_s
            )

            logger.info(
                "lsp: client spawned successfully",
                extra={
                    "extra_data": {
                        "server_id": server_def.server_id,
                        "root_path": root_path,
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
                        "lsp: shutting down idle server",
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
