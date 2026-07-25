"""Standalone executor runner for remote tool and inference proxying."""

from __future__ import annotations

import asyncio
import contextlib
import getpass
import hashlib
import json
import logging
import os
import platform
import threading
import uuid
from builtins import BaseExceptionGroup
from collections import OrderedDict
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from time import monotonic, perf_counter
from typing import Any
from urllib.parse import parse_qs, urlsplit

from websockets.exceptions import ConnectionClosed

from cognis.core.executor_resolution import filter_tools_by_executor
from cognis.executor.inference_types import json_safe_inference_payload
from cognis.executor.resources import ExecutorResourceCollector
from cognis.models.executor_inference import resolve_executor_local_inference_config
from cognis.models.executor_resources import ExecutorRuntimeResourceSnapshot
from cognis.models.local_models import (
    OLLAMA_MANAGED_ENDPOINT,
    OllamaRuntimeModelRequest,
    OllamaRuntimeStartRequest,
)
from cognis.models.tool import (
    ExecutorConfig,
    MCPServerConfig,
    ToolCall,
    ToolDefinition,
    ToolResult,
    ToolSource,
)
from cognis.tools.executor.browser.handlers import build_manager_from_config
from cognis.tools.executor.browser.manager import (
    BROWSER_MANAGER_KEY,
    BrowserLifecycleError,
    BrowserManager,
    BrowserManagerCleanupRetainer,
    BrowserSessionOwner,
)
from cognis.tools.executor.definitions import (
    executor_tool_definitions,
    executor_tool_handlers,
    office_executor_tool_definitions,
)
from cognis.tools.executor.file_freshness import _FILE_FRESHNESS_KEY, get_file_freshness_tracker
from cognis.tools.executor.lsp import (
    LSP_MANAGER_KEY,
    LSP_STATUS_CAPABILITY,
    build_lsp_manager,
    build_lsp_status_report,
    cleanup_lsp_manager,
    resolve_lsp_runtime_config,
)
from cognis.tools.executor.officecli import (
    OFFICECLI_RUNTIME_METADATA_KEY,
    ensure_officecli,
    resolve_officecli_runtime_config,
)
from cognis.tools.executor.project_context import (
    INTERNAL_PROJECT_CONTEXT_PROBE_TOOL,
    handle_project_context_probe,
)
from cognis.tools.executor.shell import (
    SHELL_MANAGER_KEY,
    BackgroundShellCompletionCallback,
    cleanup_shell_manager,
    list_background_shell_statuses,
    notify_pending_background_shell_completions,
    set_background_shell_completion_callback,
)
from cognis.tools.mcp import (
    MCPClient,
    MCPClientError,
    _safe_message,
    build_mcp_client,
    mcp_tools_to_definitions,
    runtime_mcp_server_key,
    validate_unique_server_names,
)

logger = logging.getLogger("cognis.executor.runner")

_HEARTBEAT_INTERVAL = 15
_RECONNECT_BASE = 1.0
_RECONNECT_MAX = 60.0
_MCP_PREPARE_TOTAL_TIMEOUT_SECONDS = 90.0
_RUNTIME_METADATA_SCHEMA_VERSION = 1
_OAUTH_LOOPBACK_DEFAULT_TTL_SECONDS = 600
_OAUTH_LOOPBACK_MAX_TTL_SECONDS = 900
_OAUTH_LOOPBACK_CALLBACK_PATH = "/oauth/callback"
_LOCAL_INFERENCE_START_METHODS = {
    "llm.complete",
    "llm.discover_models",
    "llm.image_generate",
    "llm.transcribe",
    "llm.synthesize",
    "local_model.show",
    "local_model.operation.start",
}


def _env_float(name: str, default: float, *, minimum: float | None = None) -> float:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = float(raw)
    except ValueError:
        logger.warning("Invalid %s=%r; using default %.1f", name, raw, default)
        return default
    if minimum is not None:
        return max(minimum, value)
    return value


def _coerce_bounded_int(
    value: Any,
    *,
    default: int,
    minimum: int,
    maximum: int,
) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return max(minimum, min(maximum, parsed))


_WS_PING_INTERVAL_SECONDS = _env_float(
    "COGNIS_EXECUTOR_WS_PING_INTERVAL_SECONDS",
    30.0,
    minimum=1.0,
)
_WS_PING_TIMEOUT_SECONDS = _env_float(
    "COGNIS_EXECUTOR_WS_PING_TIMEOUT_SECONDS",
    90.0,
    minimum=1.0,
)
_EVENT_LOOP_WATCHDOG_INTERVAL_SECONDS = _env_float(
    "COGNIS_EXECUTOR_EVENT_LOOP_WATCHDOG_INTERVAL_SECONDS",
    5.0,
    minimum=1.0,
)
_EVENT_LOOP_WATCHDOG_TIMEOUT_SECONDS = _env_float(
    "COGNIS_EXECUTOR_EVENT_LOOP_WATCHDOG_TIMEOUT_SECONDS",
    45.0,
    minimum=5.0,
)
_RESOURCE_SNAPSHOT_INTERVAL_SECONDS = _env_float(
    "COGNIS_EXECUTOR_RESOURCE_SNAPSHOT_INTERVAL_SECONDS",
    60.0,
    minimum=15.0,
)


class _EventLoopWatchdog:
    """Force process recovery when the executor event loop stops scheduling."""

    def __init__(
        self,
        loop: asyncio.AbstractEventLoop,
        *,
        interval_seconds: float,
        timeout_seconds: float,
        exit_process: Callable[[int], object] = os._exit,
        clock: Callable[[], float] = monotonic,
    ) -> None:
        self._loop = loop
        self._interval_seconds = interval_seconds
        self._timeout_seconds = timeout_seconds
        self._exit_process = exit_process
        self._clock = clock
        self._last_ack = clock()
        self._ack_lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(
            target=self._run,
            name="cognis-executor-event-loop-watchdog",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=self._interval_seconds + 1.0)

    def acknowledge(self) -> None:
        with self._ack_lock:
            self._last_ack = self._clock()

    def poll_once(self) -> bool:
        """Schedule an acknowledgement; return false after fatal timeout."""
        with self._ack_lock:
            elapsed = self._clock() - self._last_ack
        if elapsed > self._timeout_seconds:
            logger.critical(
                "Executor event loop unresponsive for %.1fs; forcing process restart",
                elapsed,
            )
            self._exit_process(70)
            return False
        try:
            self._loop.call_soon_threadsafe(self.acknowledge)
        except RuntimeError:
            return False
        return True

    def _run(self) -> None:
        while not self._stop.wait(self._interval_seconds):
            if not self.poll_once():
                return


def _contains_process_exit_exception(exc: BaseException) -> bool:
    if isinstance(exc, (KeyboardInterrupt, SystemExit)):
        return True
    if isinstance(exc, BaseExceptionGroup):
        return any(_contains_process_exit_exception(child) for child in exc.exceptions)
    return False


def _is_pure_cancellation_exception(exc: BaseException) -> bool:
    if isinstance(exc, asyncio.CancelledError):
        return True
    if isinstance(exc, BaseExceptionGroup):
        return bool(exc.exceptions) and all(
            _is_pure_cancellation_exception(child) for child in exc.exceptions
        )
    return False


def _should_reraise_isolated_exception(exc: BaseException) -> bool:
    """Return whether isolation code must not suppress this BaseException."""

    return _contains_process_exit_exception(exc) or _is_pure_cancellation_exception(exc)


def _safe_base_exception_message(exc: BaseException, *, limit: int = 500) -> str:
    """Build a redacted, compact message for ExceptionGroup/BaseException errors."""

    if isinstance(exc, BaseExceptionGroup):
        parts = [
            f"{type(exc).__name__}: {_safe_message(str(exc), limit=limit)}",
        ]
        for child in exc.exceptions[:3]:
            parts.append(f"{type(child).__name__}: {_safe_message(str(child), limit=limit)}")
        if len(exc.exceptions) > 3:
            parts.append(f"... {len(exc.exceptions) - 3} more sub-exception(s)")
        return _safe_message("; ".join(parts), limit=limit)
    return _safe_message(str(exc), limit=limit)


def _websocket_close_metadata(ws: Any, exc: BaseException | None = None) -> dict[str, Any]:
    """Return bounded transport close details without exposing payload data."""

    close = getattr(exc, "rcvd", None) if exc is not None else None
    code = getattr(close, "code", None)
    reason = getattr(close, "reason", None)
    if code is None:
        code = getattr(ws, "close_code", None)
    if reason is None:
        reason = getattr(ws, "close_reason", None)
    return {
        "close_code": code if isinstance(code, int) else None,
        "close_reason": _safe_message(reason, limit=200) if isinstance(reason, str) else None,
    }


def _build_browser_manager(browser_config: dict[str, Any]) -> BrowserManager | None:
    """Build a BrowserManager from the browser config block.

    Returns ``None`` when browser is disabled so callers can skip the
    BROWSER_MANAGER_KEY assignment gracefully.
    """
    try:
        if not browser_config.get("enabled", True):
            return None
        return build_manager_from_config({"browser": browser_config})
    except Exception as exc:
        logger.warning("executor: failed to build BrowserManager: %s", exc)
        return None


def _build_environment_payload() -> dict[str, str]:
    """Capture executor-local environment metadata for controller guidance."""

    try:
        user = getpass.getuser()
    except Exception:
        user = "unknown"
    return {
        "user": user,
        "home": str(Path.home()),
        "cwd": os.getcwd(),
        "hostname": platform.node(),
        "source": "executor_runtime",
        "observed_at": datetime.now(UTC).isoformat(),
    }


def _build_platform_payload() -> dict[str, str]:
    """Capture stable executor platform metadata."""

    return {
        "os": platform.system().lower(),
        "arch": platform.machine().lower(),
        "python": platform.python_version(),
    }


def _same_turn_tool_call_identity(params: dict[str, Any]) -> tuple[tuple[str, str], str] | None:
    """Return provider-neutral turn scope and exact-call key."""

    runtime_metadata = params.get("runtime_metadata")
    if not isinstance(runtime_metadata, dict):
        return None
    turn_id = runtime_metadata.get("turn_id")
    execution_scope_id = params.get("execution_scope_id")
    tool_name = params.get("tool_name")
    if not all(
        isinstance(value, str) and value for value in (turn_id, execution_scope_id, tool_name)
    ):
        return None
    turn_scope = (execution_scope_id, turn_id)
    return turn_scope, json.dumps(
        {
            "execution_scope_id": execution_scope_id,
            "turn_id": turn_id,
            "tool_name": tool_name,
            "arguments": params.get("arguments", {}),
        },
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )


def _same_turn_tool_call_key(params: dict[str, Any]) -> str | None:
    """Return only the exact-call portion of a same-turn duplicate identity."""

    identity = _same_turn_tool_call_identity(params)
    return identity[1] if identity is not None else None


class _SameTurnToolCallDeduplicator:
    """Reject later exact calls within one execution scope and turn."""

    def __init__(self) -> None:
        self._seen_keys: OrderedDict[tuple[str, str], dict[str, str]] = OrderedDict()
        self._digest_key = os.urandom(32)
        self._max_turn_scopes = 4096

    def original_call_id(self, params: dict[str, Any]) -> str | None:
        identity = _same_turn_tool_call_identity(params)
        if identity is None:
            return None
        turn_scope, canonical_identity = identity
        duplicate_key = hashlib.blake2b(
            canonical_identity.encode("utf-8"),
            key=self._digest_key,
            digest_size=16,
        ).hexdigest()
        seen_calls = self._seen_keys.setdefault(turn_scope, {})
        self._seen_keys.move_to_end(turn_scope)
        while len(self._seen_keys) > self._max_turn_scopes:
            self._seen_keys.popitem(last=False)
        call_id = str(params.get("call_id", "unknown"))
        original_call_id = seen_calls.get(duplicate_key)
        if original_call_id is None:
            seen_calls[duplicate_key] = call_id
        return original_call_id


class ExecutorRunner:
    """Thin remote hand for tool execution and inference proxying."""

    def __init__(self, config: ExecutorConfig) -> None:
        self.config = config
        self._active_calls: dict[str, asyncio.Task[Any]] = {}
        self._same_turn_tool_call_deduplicator = _SameTurnToolCallDeduplicator()
        self._connection_handler_tasks: set[asyncio.Task[Any]] = set()
        self._running = True
        self._configured = False
        self._runtime_state = "offline"
        self._config_version = 0
        self._tool_handlers: dict[str, Any] = {}
        self._configured_tool_definitions: list[ToolDefinition] = []
        self._mcp_clients: dict[str, MCPClient] = {}
        self._inference_handler: Any | None = None
        self._local_inference_enabled = True
        self._ollama_runtime_handler: Any | None = None
        self._channel_handler: Any | None = None
        self._runtime_metadata: dict[str, Any] = {}
        self._resource_collector = ExecutorResourceCollector()
        self._resource_snapshot: dict[str, Any] | None = None
        self._resource_snapshot_collected_at: float | None = None
        self._background_shell_completion_callback: BackgroundShellCompletionCallback | None = None
        self._started_at = perf_counter()
        self._ws_send_lock = asyncio.Lock()
        self._oauth_loopback_listeners: dict[str, dict[str, Any]] = {}
        self._browser_cleanup_retainer = BrowserManagerCleanupRetainer()

    async def run(self) -> None:
        reconnect_delay = _RECONNECT_BASE
        watchdog = _EventLoopWatchdog(
            asyncio.get_running_loop(),
            interval_seconds=_EVENT_LOOP_WATCHDOG_INTERVAL_SECONDS,
            timeout_seconds=_EVENT_LOOP_WATCHDOG_TIMEOUT_SECONDS,
        )
        watchdog.start()
        try:
            while self._running:
                try:
                    await self._connect_and_serve()
                except Exception:
                    logger.warning("Connection lost, reconnecting in %.1fs", reconnect_delay)
                    await asyncio.sleep(reconnect_delay)
                    reconnect_delay = min(reconnect_delay * 2, _RECONNECT_MAX)
                    continue
                reconnect_delay = _RECONNECT_BASE
                if not self._running:
                    break
                logger.warning("Connection closed, reconnecting immediately")
        except asyncio.CancelledError:
            logger.info("Executor runner cancelled, shutting down")
            raise
        finally:
            logger.info("Executor shutting down, cleaning up resources")
            browser_manager = self._runtime_metadata.get("browser_manager")
            if isinstance(browser_manager, BrowserManager):
                self._browser_cleanup_retainer.retain(browser_manager)
            elif browser_manager is not None:
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await browser_manager.cleanup()
            await self._browser_cleanup_retainer.wait_until_empty()
            lsp_manager = self._runtime_metadata.get(LSP_MANAGER_KEY)
            if lsp_manager is not None:
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await cleanup_lsp_manager(lsp_manager, executor_id=self.config.executor_id)
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await cleanup_shell_manager(self._runtime_metadata)
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._close_mcp_clients()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._close_oauth_loopback_listeners()
            if self._channel_handler is not None:
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await self._channel_handler.stop_all()
            if self._inference_handler is not None:
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await self._inference_handler.close()
            if self._ollama_runtime_handler is not None:
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await self._ollama_runtime_handler.close()
            watchdog.stop()
            logger.info("Executor shutdown complete")

    async def _connect_and_serve(self) -> None:
        try:
            import websockets
        except ImportError:
            logger.error("websockets package is required for remote executor")
            self._running = False
            return

        url = self.config.controller_url
        if not url or not self.config.controller_token:
            logger.error("controller_url and controller_token are required")
            self._running = False
            return

        self._configured = False
        self._runtime_state = "offline"
        self._config_version = 0
        self._tool_handlers = {}
        self._configured_tool_definitions = []

        logger.info("Connecting to controller at %s", url)
        async with websockets.connect(
            url,
            compression="deflate",
            max_size=10 * 1024 * 1024,
            ping_interval=_WS_PING_INTERVAL_SECONDS,
            ping_timeout=_WS_PING_TIMEOUT_SECONDS,
        ) as ws:
            logger.info("WebSocket connected, sending executor.ready")
            resource_snapshot = await self._refresh_resource_snapshot(force=True)
            ready_id = uuid.uuid4().hex
            await self._send_ws(
                ws,
                json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "method": "executor.ready",
                        "params": {
                            "token": self.config.controller_token,
                            "environment": _build_environment_payload(),
                            "platform": _build_platform_payload(),
                            "resource_snapshot": resource_snapshot,
                        },
                        "id": ready_id,
                    }
                ),
            )
            response = json.loads(await asyncio.wait_for(ws.recv(), timeout=30))
            if "error" in response:
                logger.error("Registration failed: %s", response["error"].get("message", "unknown"))
                self._running = False
                return

            logger.info(
                "Registered with controller as %s",
                response.get("result", {}).get("executor_id", "unknown"),
            )

            # Update channel handler WS reference early so surviving
            # channel adapters can forward inbound messages to the new
            # controller immediately, rather than silently dropping them
            # until executor.configure completes.
            if self._channel_handler is not None:
                self._channel_handler.set_ws(ws)

            async def _background_shell_completed(status: dict[str, Any]) -> None:
                await self._send_ws(
                    ws,
                    json.dumps(
                        {
                            "jsonrpc": "2.0",
                            "method": "shell.background_completed",
                            "params": status,
                        }
                    ),
                )

            self._background_shell_completion_callback = _background_shell_completed
            set_background_shell_completion_callback(
                self._runtime_metadata,
                self._background_shell_completion_callback,
            )
            await notify_pending_background_shell_completions(self._runtime_metadata)

            try:
                await self._run_connection_loops(ws)
            finally:
                self._background_shell_completion_callback = None
                set_background_shell_completion_callback(self._runtime_metadata, None)
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await self._close_oauth_loopback_listeners()

    async def _run_connection_loops(self, ws: Any) -> None:
        """Supervise receive and heartbeat loops as one connection lifetime."""

        message_task = asyncio.create_task(
            self._message_loop(ws),
            name=f"executor-message-loop-{self.config.executor_id}",
        )
        heartbeat_task = asyncio.create_task(
            self._heartbeat_loop(ws),
            name=f"executor-heartbeat-{self.config.executor_id}",
        )
        tasks = {message_task, heartbeat_task}
        try:
            done, _pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            heartbeat_error = (
                heartbeat_task.exception()
                if heartbeat_task in done and not heartbeat_task.cancelled()
                else None
            )
            if heartbeat_error is not None:
                metadata = _websocket_close_metadata(ws, heartbeat_error)
                logger.warning(
                    "Executor heartbeat failed; closing controller connection: %s: %s",
                    type(heartbeat_error).__name__,
                    _safe_base_exception_message(heartbeat_error),
                    extra={
                        "extra_data": {
                            "executor_id": self.config.executor_id,
                            "error_type": type(heartbeat_error).__name__,
                            **metadata,
                        }
                    },
                )
                with contextlib.suppress(Exception):
                    await ws.close(code=1011, reason="executor heartbeat failure")
                raise heartbeat_error
            if message_task in done:
                message_error = message_task.exception()
                if message_error is not None:
                    raise message_error
                logger.info(
                    "Controller connection closed",
                    extra={
                        "extra_data": {
                            "executor_id": self.config.executor_id,
                            **_websocket_close_metadata(ws),
                        }
                    },
                )
                return

            if heartbeat_error is None and not self._running:
                with contextlib.suppress(Exception):
                    await ws.close(code=1000, reason="executor shutdown")
                return
            if heartbeat_error is None:
                heartbeat_error = RuntimeError("executor heartbeat stopped unexpectedly")
            metadata = _websocket_close_metadata(ws, heartbeat_error)
            logger.warning(
                "Executor heartbeat failed; closing controller connection: %s: %s",
                type(heartbeat_error).__name__,
                _safe_base_exception_message(heartbeat_error),
                extra={
                    "extra_data": {
                        "executor_id": self.config.executor_id,
                        "error_type": type(heartbeat_error).__name__,
                        **metadata,
                    }
                },
            )
            with contextlib.suppress(Exception):
                await ws.close(code=1011, reason="executor heartbeat failure")
            raise heartbeat_error
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            await self._cancel_connection_handler_tasks()

    async def _message_loop(self, ws: Any) -> None:
        logger.info("Entering message loop, waiting for controller commands")
        async for raw_message in ws:
            try:
                msg = json.loads(raw_message)
            except json.JSONDecodeError:
                continue

            method = msg.get("method")
            msg_id = msg.get("id")
            params = msg.get("params", {})
            if method in _LOCAL_INFERENCE_START_METHODS and not self._local_inference_enabled:
                await self._send_rpc_error(
                    ws,
                    msg_id,
                    -32045,
                    "Local inference is disabled on this executor",
                )
                continue

            if method == "executor.configure":
                await self._handle_configure(ws, msg_id, params)
            elif method == "tool.list":
                logger.debug("Received tool.list request")
                await self._handle_tool_list(ws, msg_id)
            elif method == "tool.execute":
                tool_name = params.get("tool_name", params.get("name", "?"))
                logger.debug("Received tool.execute: %s", tool_name)
                call_id = str(params.get("call_id", msg_id or "unknown"))
                original_call_id = self._same_turn_tool_call_deduplicator.original_call_id(params)
                if original_call_id is not None:
                    logger.warning(
                        "Rejected duplicate same-turn tool call",
                        extra={
                            "extra_data": {
                                "executor_id": self.config.executor_id,
                                "call_id": call_id,
                                "original_call_id": original_call_id,
                                "tool_name": tool_name,
                                "reason": "duplicate_tool_call_same_turn",
                            }
                        },
                    )
                    await self._send_rpc_result(
                        ws,
                        msg_id,
                        {
                            "call_id": call_id,
                            "output": json.dumps(
                                {
                                    "status": "rejected",
                                    "reason": "duplicate_tool_call_same_turn",
                                    "original_call_id": original_call_id,
                                },
                                separators=(",", ":"),
                            ),
                            "is_error": True,
                            "duration_ms": 0,
                        },
                    )
                    continue
                task = self._create_background_handler_task(
                    self._handle_tool_execute(ws, msg_id, params),
                    "tool.execute",
                    msg_id=msg_id,
                )
                self._active_calls[params.get("call_id", msg_id)] = task
            elif method == "tool.cancel":
                call_id = params.get("call_id")
                logger.debug("Received tool.cancel: %s", call_id)
                if call_id and call_id in self._active_calls:
                    self._active_calls[call_id].cancel()
            elif method == "browser.session_terminal":
                self._create_background_handler_task(
                    self._handle_browser_session_terminal(ws, msg_id, params),
                    method,
                    msg_id=msg_id,
                )
            elif method == "llm.complete":
                logger.debug("Received llm.complete")
                self._create_background_handler_task(
                    self._handle_llm_complete(ws, msg_id, params), "llm.complete", msg_id=msg_id
                )
            elif method == "llm.discover_models":
                logger.debug("Received llm.discover_models")
                self._create_background_handler_task(
                    self._handle_llm_discover_models(ws, msg_id, params),
                    "llm.discover_models",
                    msg_id=msg_id,
                )
            elif method == "local_model.status":
                self._create_background_handler_task(
                    self._handle_local_model_status(ws, msg_id),
                    "local_model.status",
                    msg_id=msg_id,
                )
            elif method == "local_model.show":
                self._create_background_handler_task(
                    self._handle_local_model_show(ws, msg_id, params),
                    "local_model.show",
                    msg_id=msg_id,
                )
            elif method == "local_model.operation.start":
                self._create_background_handler_task(
                    self._handle_local_model_operation_start(ws, msg_id, params),
                    "local_model.operation.start",
                    msg_id=msg_id,
                )
            elif method == "local_model.operation.status":
                self._create_background_handler_task(
                    self._handle_local_model_operation_status(ws, msg_id, params),
                    "local_model.operation.status",
                    msg_id=msg_id,
                )
            elif method == "local_model.operation.cancel":
                self._create_background_handler_task(
                    self._handle_local_model_operation_cancel(ws, msg_id, params),
                    "local_model.operation.cancel",
                    msg_id=msg_id,
                )
            elif method == "llm.image_generate":
                logger.debug("Received llm.image_generate")
                self._create_background_handler_task(
                    self._handle_llm_image_generate(ws, msg_id, params),
                    "llm.image_generate",
                    msg_id=msg_id,
                )
            elif method == "llm.transcribe":
                logger.debug("Received llm.transcribe")
                self._create_background_handler_task(
                    self._handle_llm_transcribe(ws, msg_id, params),
                    "llm.transcribe",
                    msg_id=msg_id,
                )
            elif method == "llm.synthesize":
                logger.debug("Received llm.synthesize")
                self._create_background_handler_task(
                    self._handle_llm_synthesize(ws, msg_id, params),
                    "llm.synthesize",
                    msg_id=msg_id,
                )
            elif method == "channel.start":
                logger.info("Received channel.start for account %s", params.get("account_id", "?"))
                self._create_background_handler_task(
                    self._handle_channel_start(ws, msg_id, params),
                    "channel.start",
                    msg_id=msg_id,
                )
            elif method == "channel.stop":
                logger.info("Received channel.stop for account %s", params.get("account_id", "?"))
                self._create_background_handler_task(
                    self._handle_channel_stop(ws, msg_id, params), "channel.stop", msg_id=msg_id
                )
            elif method == "channel.send":
                logger.debug("Received channel.send for account %s", params.get("account_id", "?"))
                self._create_background_handler_task(
                    self._handle_channel_send(ws, msg_id, params), "channel.send", msg_id=msg_id
                )
            elif method == "channel.fetch_media":
                self._create_background_handler_task(
                    self._handle_channel_fetch_media(ws, msg_id, params),
                    "channel.fetch_media",
                    msg_id=msg_id,
                )
            elif method == "channel.typing":
                self._create_background_handler_task(
                    self._handle_channel_typing(ws, msg_id, params),
                    "channel.typing",
                    msg_id=msg_id,
                )
            elif method == "channel.mark_read":
                self._create_background_handler_task(
                    self._handle_channel_mark_read(ws, msg_id, params),
                    "channel.mark_read",
                    msg_id=msg_id,
                )
            elif method == "channel.sync_profile":
                self._create_background_handler_task(
                    self._handle_channel_sync_profile(ws, msg_id, params),
                    "channel.sync_profile",
                    msg_id=msg_id,
                )
            elif method == "lsp.status":
                self._create_background_handler_task(
                    self._handle_lsp_status(ws, msg_id, params), "lsp.status", msg_id=msg_id
                )
            elif method == "shell.background_status":
                self._create_background_handler_task(
                    self._handle_background_shell_status(ws, msg_id, params),
                    "shell.background_status",
                    msg_id=msg_id,
                )
            elif method == "oauth.loopback_start":
                self._create_background_handler_task(
                    self._handle_oauth_loopback_start(ws, msg_id, params),
                    "oauth.loopback_start",
                    msg_id=msg_id,
                )
            elif method == "oauth.loopback_stop":
                self._create_background_handler_task(
                    self._handle_oauth_loopback_stop(ws, msg_id, params),
                    "oauth.loopback_stop",
                    msg_id=msg_id,
                )
            elif method == "executor.cancel":
                logger.info("Received executor.cancel, shutting down")
                if msg_id is not None:
                    await self._send_rpc_result(ws, msg_id, {"status": "shutting_down"})
                self._running = False
                break
            else:
                logger.debug("Received unknown method: %s", method)
        if self._running:
            logger.warning("Controller websocket closed, leaving message loop")
        else:
            logger.info("Executor message loop stopped")

    def _create_background_handler_task(
        self,
        coro: Any,
        method: str,
        *,
        msg_id: str | None,
    ) -> asyncio.Task[Any]:
        task = asyncio.create_task(coro)
        self._connection_handler_tasks.add(task)

        def _consume_result(done: asyncio.Task[Any]) -> None:
            self._connection_handler_tasks.discard(done)
            try:
                done.result()
            except asyncio.CancelledError:
                logger.debug(
                    "Background handler task cancelled",
                    extra={"extra_data": {"method": method, "msg_id": msg_id}},
                )
            except ConnectionClosed:
                logger.debug(
                    "Background handler task could not reply because websocket closed",
                    extra={"extra_data": {"method": method, "msg_id": msg_id}},
                )
            except Exception:
                logger.exception(
                    "Background handler task failed",
                    extra={"extra_data": {"method": method, "msg_id": msg_id}},
                )

        task.add_done_callback(_consume_result)
        return task

    async def _cancel_connection_handler_tasks(self) -> None:
        """Cancel and drain RPC handlers owned by the closing connection."""

        tasks = tuple(self._connection_handler_tasks)
        for task in tasks:
            if not task.done():
                task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._connection_handler_tasks.difference_update(tasks)
        for call_id, task in tuple(self._active_calls.items()):
            if task in tasks or task.done():
                self._active_calls.pop(call_id, None)

    async def _handle_configure(self, ws: Any, msg_id: str | None, params: dict[str, Any]) -> None:
        requested_version = int(params.get("config_version") or (self._config_version + 1))
        mcp_servers_raw = params.get("mcp_servers") or []
        logger.info(
            "Received executor.configure v%d (current v%d, %d MCP server(s))",
            requested_version,
            self._config_version,
            len(mcp_servers_raw),
        )
        if requested_version <= self._config_version:
            logger.warning(
                "Ignoring stale executor.configure v%d (already at v%d)",
                requested_version,
                self._config_version,
            )
            await self._send_rpc_error(ws, msg_id, -32020, "Stale executor.configure version")
            return

        previous_configured = self._configured
        previous_runtime_state = self._runtime_state
        self._runtime_state = "reconfiguring"

        config = params.get("config", {})
        enabled_tools = params.get("enabled_tools", [])
        enabled_tool_groups = params.get("enabled_tool_groups", [])
        secrets = dict(params.get("secrets") or {})

        previous_clients = self._mcp_clients
        previous_tool_handlers = dict(self._tool_handlers)
        previous_tool_definitions = list(self._configured_tool_definitions)
        previous_runtime_metadata = dict(self._runtime_metadata)
        previous_lsp_manager = previous_runtime_metadata.get(LSP_MANAGER_KEY)
        previous_shell_manager = previous_runtime_metadata.get(SHELL_MANAGER_KEY)
        previous_ollama_handler = self._ollama_runtime_handler
        previous_local_inference_enabled = self._local_inference_enabled
        previous_ollama_config = (
            previous_ollama_handler.config if previous_ollama_handler is not None else None
        )

        try:
            inference_config = resolve_executor_local_inference_config(
                config if isinstance(config, dict) else {}
            )
            ollama_config = inference_config.ollama_runtime.model_copy(
                update={"management_enabled": inference_config.ollama_management_enabled}
            )
            mcp_servers = [MCPServerConfig.model_validate(item) for item in mcp_servers_raw]
            validate_unique_server_names(mcp_servers)
            async with asyncio.timeout(_MCP_PREPARE_TOTAL_TIMEOUT_SECONDS):
                (
                    staged_mcp_clients,
                    discovered_tools,
                    mcp_statuses,
                    mcp_warnings,
                ) = await self._prepare_mcp_runtime(mcp_servers, secrets)
        except Exception as exc:
            logger.warning(
                "Configure v%d failed during MCP preparation: %s", requested_version, exc
            )
            self._mcp_clients = previous_clients
            self._tool_handlers = previous_tool_handlers
            self._configured_tool_definitions = previous_tool_definitions
            self._runtime_metadata = previous_runtime_metadata
            self._local_inference_enabled = previous_local_inference_enabled
            self._configured = previous_configured
            self._runtime_state = "blocked" if not previous_configured else previous_runtime_state
            await self._send_rpc_error(ws, msg_id, -32021, f"Executor configure failed: {exc}")
            return
        except BaseException as exc:
            if _should_reraise_isolated_exception(exc):
                raise
            message = _safe_base_exception_message(exc)
            logger.warning(
                "Configure v%d failed during isolated MCP preparation error: %s",
                requested_version,
                message,
                exc_info=True,
            )
            self._mcp_clients = previous_clients
            self._tool_handlers = previous_tool_handlers
            self._configured_tool_definitions = previous_tool_definitions
            self._runtime_metadata = previous_runtime_metadata
            self._local_inference_enabled = previous_local_inference_enabled
            self._configured = previous_configured
            self._runtime_state = "blocked" if not previous_configured else previous_runtime_state
            await self._send_rpc_error(
                ws,
                msg_id,
                -32021,
                f"Executor configure failed during MCP preparation: {message}",
            )
            return

        native_defs = filter_tools_by_executor(
            executor_tool_definitions(),
            enabled_tools,
            enabled_tool_groups,
        )

        # Generate dynamic web tool definitions from controller-provided config
        web_config_raw = params.get("web_config") or {}
        web_backends = web_config_raw.get("web_available_backends", ["direct"])
        browser_config = config.get("browser") if isinstance(config, dict) else {}
        from cognis.tools.executor.web.definitions import web_tool_definitions

        web_defs = web_tool_definitions(
            web_backends,
            default_backend=web_config_raw.get("web_backend"),
            available_search_backends=web_config_raw.get("web_available_search_backends"),
            available_fetch_backends=web_config_raw.get("web_available_fetch_backends"),
            default_search_backend=web_config_raw.get("web_search_backend"),
            default_fetch_backend=web_config_raw.get("web_fetch_backend"),
        )
        # Build or reuse the BrowserManager eagerly so every tool call shares
        # the same persistent manager rather than allocating a new one per call.
        browser_config_dict = browser_config if isinstance(browser_config, dict) else {}
        new_browser_manager = _build_browser_manager(browser_config_dict)

        # Store web runtime metadata for handler context
        runtime_state = "degraded" if mcp_warnings else "active"
        try:
            effective_lsp_config = resolve_lsp_runtime_config(
                config if isinstance(config, dict) else {}
            )
            self._runtime_metadata = {
                "schema_version": _RUNTIME_METADATA_SCHEMA_VERSION,
                "configure_capabilities": ["mcp_runtime_status_v1", LSP_STATUS_CAPABILITY],
                "legacy_metadata": False,
                "lsp_enabled": effective_lsp_config.enabled,
                "lsp_auto_install": effective_lsp_config.auto_install,
                "lsp_diagnostics_timeout_ms": effective_lsp_config.diagnostics_timeout_ms,
                "lsp_idle_timeout_seconds": effective_lsp_config.idle_timeout_seconds,
                "lsp_max_concurrent_servers": effective_lsp_config.max_concurrent_servers,
                "web_backend": web_config_raw.get("web_backend", "direct"),
                "web_search_backend": web_config_raw.get("web_search_backend", "direct"),
                "web_fetch_backend": web_config_raw.get("web_fetch_backend", "direct"),
                "web_fetch_fallback_browser": web_config_raw.get(
                    "web_fetch_fallback_browser", True
                ),
                "web_searxng_url": web_config_raw.get("web_searxng_url", ""),
                "web_searxng_engines": web_config_raw.get("web_searxng_engines", ""),
                "web_searxng_categories": web_config_raw.get("web_searxng_categories", ""),
                "web_searxng_language": web_config_raw.get("web_searxng_language", ""),
                "web_browser_fetch_session_idle_seconds": web_config_raw.get(
                    "web_browser_fetch_session_idle_seconds", 60
                ),
                "web_browser_fetch_wait_timeout_seconds": web_config_raw.get(
                    "web_browser_fetch_wait_timeout_seconds", 30
                ),
                "web_browser_fetch_navigation_timeout_seconds": web_config_raw.get(
                    "web_browser_fetch_navigation_timeout_seconds", 60
                ),
                "web_browser_fetch_wait_until": web_config_raw.get(
                    "web_browser_fetch_wait_until", "domcontentloaded"
                ),
                "web_browser_fetch_network_idle_after_dom_seconds": web_config_raw.get(
                    "web_browser_fetch_network_idle_after_dom_seconds", 3
                ),
                "web_browser_fetch_headed_fallback_enabled": web_config_raw.get(
                    "web_browser_fetch_headed_fallback_enabled", True
                ),
                "web_concurrency": web_config_raw.get("web_concurrency", {}),
                "web_available_backends": web_backends,
                "web_available_search_backends": web_config_raw.get(
                    "web_available_search_backends",
                    [b for b in web_backends if b != "browser"],
                ),
                "web_available_fetch_backends": web_config_raw.get(
                    "web_available_fetch_backends",
                    [b for b in web_backends if b not in {"brave", "searxng"}],
                ),
                "web_secrets": secrets,
                "controller_url": self.config.controller_url,
                "browser": browser_config_dict,
                "environment": _build_environment_payload(),
                "platform": _build_platform_payload(),
                "mcp_servers": mcp_statuses,
                "warnings": mcp_warnings,
                "local_inference_enabled": inference_config.local_inference_enabled,
            }
            if previous_shell_manager is not None:
                self._runtime_metadata[SHELL_MANAGER_KEY] = previous_shell_manager
            if self._background_shell_completion_callback is not None:
                set_background_shell_completion_callback(
                    self._runtime_metadata,
                    self._background_shell_completion_callback,
                )
                await notify_pending_background_shell_completions(self._runtime_metadata)
            if new_browser_manager is not None:
                self._runtime_metadata[BROWSER_MANAGER_KEY] = new_browser_manager
            get_file_freshness_tracker(self._runtime_metadata)
            try:
                lsp_manager = build_lsp_manager(config if isinstance(config, dict) else {})
            except Exception:
                self._runtime_metadata["lsp_init_failed"] = True
                self._runtime_metadata["lsp_warning"] = "LSP manager initialization failed."
                logger.warning(
                    "Failed to initialize LSP manager for executor runtime", exc_info=True
                )
                lsp_manager = None
            if lsp_manager is not None:
                self._runtime_metadata[LSP_MANAGER_KEY] = lsp_manager
            officecli_config = resolve_officecli_runtime_config(
                config if isinstance(config, dict) else {}
            )
            officecli_status = await ensure_officecli(officecli_config)
            self._runtime_metadata[OFFICECLI_RUNTIME_METADATA_KEY] = officecli_status.metadata()
            self._runtime_metadata["officecli_available"] = officecli_status.available
            self._runtime_metadata["officecli_enabled"] = officecli_status.enabled
            self._runtime_metadata["officecli_auto_install"] = officecli_status.auto_install
            self._runtime_metadata["officecli_version"] = officecli_status.version
            self._runtime_metadata["officecli_platform"] = officecli_status.platform_key
            self._runtime_metadata["officecli_command"] = officecli_status.command
            self._runtime_metadata["officecli_capabilities"] = officecli_status.capabilities or {}
            self._runtime_metadata["officecli_error"] = officecli_status.error
            self._runtime_metadata["officecli_installed_from"] = officecli_status.installed_from
            office_defs = filter_tools_by_executor(
                office_executor_tool_definitions(self._runtime_metadata),
                enabled_tools,
                enabled_tool_groups,
            )
            native_defs = [*native_defs, *office_defs]

            self._configured_tool_definitions = [*native_defs, *web_defs, *discovered_tools]
            native_handlers = executor_tool_handlers()
            allowed_native = {t.name for t in native_defs}
            allowed_web = {t.name for t in web_defs}
            self._tool_handlers = {
                name: handler
                for name, handler in native_handlers.items()
                if name in allowed_native or name in allowed_web
            }
            self._tool_handlers[INTERNAL_PROJECT_CONTEXT_PROBE_TOOL] = handle_project_context_probe
            for tool in discovered_tools:
                if tool.source.server_name is not None:
                    self._tool_handlers[tool.name] = self._build_mcp_handler(tool)

            old_clients = self._mcp_clients
            self._mcp_clients = staged_mcp_clients

            # Register skill tool handlers from controller-provided manifests
            skill_manifests_raw = params.get("skill_manifests") or []
            self._runtime_metadata["skill_manifests"] = [
                manifest for manifest in skill_manifests_raw if isinstance(manifest, dict)
            ]
            await self._register_skill_handlers(skill_manifests_raw, secrets)

            if self._inference_handler is None:
                from cognis.executor.inference import InferenceHandler

                self._inference_handler = InferenceHandler()
            if self._ollama_runtime_handler is None:
                from cognis.executor.ollama_runtime import OllamaRuntimeHandler

                self._ollama_runtime_handler = OllamaRuntimeHandler(ollama_config)
            else:
                await self._ollama_runtime_handler.reconfigure(ollama_config)
            self._runtime_metadata["ollama_runtime"] = self._ollama_runtime_handler.capability()
            self._local_inference_enabled = inference_config.local_inference_enabled
            if self._channel_handler is None:
                from cognis.executor.channel_handler import ChannelHandler

                self._channel_handler = ChannelHandler()
            self._channel_handler.set_ws(ws)
            self._channel_handler.set_executor_config(config)

            if old_clients is not previous_clients:
                await self._close_clients(old_clients, suppress_cancelled=True)
            elif previous_clients is not staged_mcp_clients:
                # MCP SDK transports use anyio cancel scopes that must be exited
                # from the same asyncio task that entered them.  Keep stale
                # client teardown inline with the message-loop/configure task;
                # closing from a background task leaves async generators for
                # loop shutdown and triggers anyio cross-task scope errors.
                await self._close_clients(previous_clients, suppress_cancelled=True)
            if previous_lsp_manager is not self._runtime_metadata.get(LSP_MANAGER_KEY):
                await cleanup_lsp_manager(previous_lsp_manager, executor_id=self.config.executor_id)
        except BaseException as exc:
            logger.warning(
                "Configure v%d failed during tool/handler setup: %s", requested_version, exc
            )
            current_lsp_manager = self._runtime_metadata.get(LSP_MANAGER_KEY)
            current_browser_manager = self._runtime_metadata.get(BROWSER_MANAGER_KEY)
            self._mcp_clients = previous_clients
            self._tool_handlers = previous_tool_handlers
            self._configured_tool_definitions = previous_tool_definitions
            self._runtime_metadata = previous_runtime_metadata
            self._local_inference_enabled = previous_local_inference_enabled
            self._configured = previous_configured
            self._runtime_state = "blocked" if not previous_configured else previous_runtime_state
            if isinstance(
                current_browser_manager, BrowserManager
            ) and current_browser_manager is not previous_runtime_metadata.get(BROWSER_MANAGER_KEY):
                self._browser_cleanup_retainer.retain(current_browser_manager)
            if self._ollama_runtime_handler is not previous_ollama_handler:
                if self._ollama_runtime_handler is not None:
                    with contextlib.suppress(Exception, asyncio.CancelledError):
                        await self._ollama_runtime_handler.close()
                self._ollama_runtime_handler = previous_ollama_handler
            elif previous_ollama_config is not None and self._ollama_runtime_handler is not None:
                with contextlib.suppress(Exception, asyncio.CancelledError):
                    await self._ollama_runtime_handler.reconfigure(previous_ollama_config)
            if not isinstance(exc, Exception):
                with contextlib.suppress(Exception, asyncio.CancelledError):
                    await self._close_clients(staged_mcp_clients, suppress_cancelled=True)
                if current_lsp_manager is not previous_lsp_manager:
                    with contextlib.suppress(Exception, asyncio.CancelledError):
                        await cleanup_lsp_manager(
                            current_lsp_manager,
                            executor_id=self.config.executor_id,
                        )
                raise
            await self._close_clients(staged_mcp_clients, suppress_cancelled=True)
            if current_lsp_manager is not previous_lsp_manager:
                await cleanup_lsp_manager(current_lsp_manager, executor_id=self.config.executor_id)
            await self._send_rpc_error(ws, msg_id, -32021, f"Executor configure failed: {exc}")
            return

        old_browser_manager = previous_runtime_metadata.get(BROWSER_MANAGER_KEY)
        if isinstance(
            old_browser_manager, BrowserManager
        ) and old_browser_manager is not self._runtime_metadata.get(BROWSER_MANAGER_KEY):
            self._browser_cleanup_retainer.retain(old_browser_manager)
        self._config_version = requested_version
        self._configured = True
        self._runtime_state = runtime_state
        previous_probe_endpoint = (
            previous_ollama_config.endpoint
            if previous_ollama_config is not None
            else OLLAMA_MANAGED_ENDPOINT
        )
        previous_model_store_path = (
            previous_ollama_config.model_store_path if previous_ollama_config is not None else None
        )
        if (
            ollama_config.endpoint != previous_probe_endpoint
            or ollama_config.model_store_path != previous_model_store_path
        ):
            self._resource_snapshot_collected_at = None
        await self._refresh_resource_snapshot(force=False, return_cached=True)
        logger.info(
            "Configure v%d complete: state=%s, %d tool(s), %d MCP client(s)%s",
            requested_version,
            runtime_state,
            len(self._configured_tool_definitions),
            len(self._mcp_clients),
            f", warnings: {mcp_warnings}" if mcp_warnings else "",
        )
        if msg_id is not None:
            await self._send_rpc_result(
                ws,
                msg_id,
                {
                    "status": "configured",
                    "applied_version": self._config_version,
                    "ready": True,
                    "runtime_state": runtime_state,
                    "observed_at": datetime.now(UTC).isoformat(),
                    "capabilities": {
                        "tools": [tool.name for tool in self._configured_tool_definitions],
                        "inference": self._local_inference_enabled,
                        "local_inference": self._local_inference_enabled,
                        "inference_models": [],
                        "inference_type": "litellm_proxy",
                        "channels": True,
                        "local_model_runtime": self._ollama_runtime_handler.capability()
                        if self._ollama_runtime_handler is not None
                        and self._local_inference_enabled
                        else None,
                    },
                    "observed_tools": [
                        tool.model_dump(mode="json") for tool in self._configured_tool_definitions
                    ],
                    "config_keys": sorted(config.keys()) if isinstance(config, dict) else [],
                    "environment": self._runtime_metadata.get("environment")
                    or _build_environment_payload(),
                    "runtime_metadata": self._public_runtime_metadata(),
                },
            )

    async def _register_skill_handlers(
        self, skill_manifests_raw: list[dict[str, Any]], secrets: dict[str, str]
    ) -> None:
        """Register executable skill tool handlers from controller-provided manifests.

        Each manifest contains skill metadata, tool specs with recipes,
        and asset references with controller-signed URLs for on-demand staging.
        """
        for manifest in skill_manifests_raw:
            skill_id = manifest.get("skill_id", "")
            skill_tools = manifest.get("tools", [])
            asset_manifest = manifest.get("asset_manifest", [])

            # Register handlers for each tool in this skill
            for tool_spec in skill_tools:
                tool_name = tool_spec.get("qualified_name") or tool_spec.get("name", "")
                recipe = tool_spec.get("recipe")
                if not tool_name or not recipe:
                    continue

                tool_def = ToolDefinition(
                    name=tool_name,
                    description=tool_spec.get("description", ""),
                    parameters=tool_spec.get("parameters", {"type": "object", "properties": {}}),
                    source=ToolSource(
                        type="skill",
                        skill_id=skill_id,
                        skill_version_id=manifest.get("version_id"),
                    ),
                    category="skill",
                    read_only=bool(tool_spec.get("read_only", False)),
                    non_bypassable=True,
                    timeout_seconds=int(tool_spec.get("timeout_seconds", 60)),
                )
                self._configured_tool_definitions.append(tool_def)
                self._tool_handlers[tool_name] = self._build_skill_recipe_handler(
                    recipe,
                    asset_manifest,
                    secrets,
                )

    def _build_skill_recipe_handler(
        self,
        recipe: dict[str, Any],
        asset_manifest: list[dict[str, Any]],
        secrets: dict[str, str],
    ) -> Any:
        """Build a handler closure for a skill tool recipe."""
        import asyncio
        import hashlib
        import shutil
        import tempfile
        from pathlib import Path

        import httpx

        mode = recipe.get("mode", "command")
        entry = recipe.get("entry", "")
        recipe_args = recipe.get("args", [])
        recipe_env = recipe.get("env", {})
        recipe_timeout = recipe.get("timeout_seconds", 60)
        working_dir = recipe.get("working_dir")
        secret_placeholders = recipe.get("secret_placeholders", [])

        def resolve_staged_path(base_dir: Path, relative_path: str) -> Path:
            target = (base_dir / relative_path).resolve()
            if not target.is_relative_to(base_dir.resolve()):
                raise ValueError(f"Unsafe recipe path rejected: {relative_path}")
            return target

        async def handler(arguments: dict[str, Any], context: Any) -> str:
            staging_dir = Path(tempfile.mkdtemp(prefix="cognis_skill_"))
            try:
                try:

                    async def _run() -> str:
                        proc: asyncio.subprocess.Process | None = None
                        async with httpx.AsyncClient(timeout=recipe_timeout) as client:
                            for asset in asset_manifest:
                                filename = asset.get("filename", "")
                                asset_url = asset.get("url", "")
                                expected_hash = asset.get("content_hash", "")
                                if not filename or not asset_url:
                                    continue
                                asset_path = resolve_staged_path(staging_dir, filename)
                                asset_path.parent.mkdir(parents=True, exist_ok=True)
                                try:
                                    response = await client.get(asset_url)
                                    response.raise_for_status()
                                except Exception as exc:
                                    return f"Failed to stage asset {filename}: {exc}"
                                content = response.content
                                if expected_hash:
                                    actual_hash = hashlib.sha256(content).hexdigest()
                                    if actual_hash != expected_hash:
                                        return f"Asset hash mismatch for {filename}"
                                asset_path.write_bytes(content)

                        env = dict(os.environ)
                        env.update(recipe_env)
                        for placeholder in secret_placeholders:
                            if placeholder in secrets:
                                env[placeholder] = secrets[placeholder]
                        env["SKILL_STAGING_DIR"] = str(staging_dir)

                        cwd = (
                            resolve_staged_path(staging_dir, working_dir)
                            if working_dir
                            else staging_dir
                        )

                        if mode == "script":
                            script_path = resolve_staged_path(staging_dir, entry)
                            if not script_path.exists():
                                return f"Script not found: {entry}"
                            script_path.chmod(0o755)
                            cmd = [str(script_path), *recipe_args]
                        elif mode == "command":
                            cmd = [entry, *recipe_args]
                        else:
                            return f"Unsupported recipe mode: {mode}"

                        for key, value in arguments.items():
                            cmd = [c.replace(f"{{{key}}}", str(value)) for c in cmd]
                            env[f"SKILL_ARG_{key.upper()}"] = str(value)

                        proc = await asyncio.create_subprocess_exec(
                            *cmd,
                            cwd=str(cwd),
                            env=env,
                            stdout=asyncio.subprocess.PIPE,
                            stderr=asyncio.subprocess.PIPE,
                        )
                        try:
                            stdout, stderr = await proc.communicate()
                        except asyncio.CancelledError:
                            if proc.returncode is None:
                                proc.kill()
                                await proc.wait()
                            raise
                        output = stdout.decode(errors="replace")
                        if proc.returncode != 0:
                            err = stderr.decode(errors="replace")
                            return f"Exit code {proc.returncode}\n{output}\n{err}".strip()
                        return output

                    return await asyncio.wait_for(_run(), timeout=recipe_timeout)
                except TimeoutError:
                    return f"Skill tool execution timed out after {recipe_timeout}s"
            finally:
                shutil.rmtree(staging_dir, ignore_errors=True)

        return handler

    async def _handle_lsp_status(self, ws: Any, msg_id: str | None, _: dict[str, Any]) -> None:
        report = await build_lsp_status_report(
            manager=self._runtime_metadata.get(LSP_MANAGER_KEY),
            executor_id=self.config.executor_id,
            executor_type="websocket",
            source=self._runtime_metadata,
            warnings=list(self._runtime_metadata.get("warnings") or []),
        )
        await self._send_rpc_result(ws, msg_id, report.model_dump(mode="json"))

    async def _handle_background_shell_status(
        self, ws: Any, msg_id: str | None, params: dict[str, Any]
    ) -> None:
        statuses = await list_background_shell_statuses(
            self._runtime_metadata,
            include_completed=bool(params.get("include_completed", False)),
        )
        await self._send_rpc_result(ws, msg_id, {"shells": statuses})

    async def _handle_tool_list(self, ws: Any, msg_id: str | None) -> None:
        if msg_id is None:
            return
        await self._send_rpc_result(
            ws,
            msg_id,
            {"tools": [tool.model_dump(mode="json") for tool in self._configured_tool_definitions]},
        )

    async def _handle_tool_execute(
        self, ws: Any, msg_id: str | None, params: dict[str, Any]
    ) -> None:
        call_id = params.get("call_id", msg_id or uuid.uuid4().hex)
        if not self._configured or self._runtime_state not in {"active", "degraded"}:
            await self._send_rpc_result(
                ws,
                msg_id,
                {
                    "call_id": call_id,
                    "output": (
                        "Executor is not configured or ready yet. "
                        f"Current runtime state: {self._runtime_state}."
                    ),
                    "is_error": True,
                    "duration_ms": 0,
                },
            )
            return

        tool_name = params.get("tool_name", "")
        arguments = params.get("arguments", {})
        timeout_seconds = params.get("timeout_seconds")
        start = perf_counter()
        send_lock = asyncio.Lock()
        request_runtime_metadata = params.get("runtime_metadata")
        if not isinstance(request_runtime_metadata, dict):
            request_runtime_metadata = {}
        tool_definition = next(
            (
                definition
                for definition in self._configured_tool_definitions
                if definition.name == tool_name
            ),
            None,
        )
        tool_server_id = (
            getattr(getattr(tool_definition, "source", None), "server_id", None)
            if tool_definition is not None
            else None
        )
        try:
            handler = self._tool_handlers.get(tool_name)
            if handler is None:
                result = ToolResult(
                    output=f"Tool '{tool_name}' not available on this executor.", is_error=True
                )
            else:
                from cognis.core.tool_arguments import validate_tool_arguments
                from cognis.models.tool import tool_input_schema

                definition = next(
                    (tool for tool in self._configured_tool_definitions if tool.name == tool_name),
                    None,
                )
                expected_hash = request_runtime_metadata.get("tool_contract_hash")
                local_hash = (
                    definition.descriptor.schema_hash
                    if definition is not None and definition.descriptor is not None
                    else None
                )
                if local_hash is not None and expected_hash != local_hash:
                    result = ToolResult(
                        output="Controller and executor tool contracts do not match.",
                        is_error=True,
                        metadata={
                            "code": "tool_contract_mismatch",
                            "expected_hash": expected_hash,
                            "local_hash": local_hash,
                        },
                    )
                    await self._send_rpc_result(
                        ws,
                        msg_id,
                        {
                            "call_id": call_id,
                            **result.model_dump(mode="json"),
                        },
                    )
                    return
                validation_error = validate_tool_arguments(
                    tool_name,
                    arguments,
                    schema=tool_input_schema(definition) if definition is not None else None,
                )
                if validation_error is not None:
                    result = ToolResult(
                        output=json.dumps(validation_error.as_tool_result()),
                        is_error=True,
                        metadata={"code": "invalid_tool_arguments"},
                    )
                    await self._send_rpc_result(
                        ws,
                        msg_id,
                        {
                            "call_id": call_id,
                            **result.model_dump(mode="json"),
                        },
                    )
                    return
                tool_call = ToolCall(call_id=call_id, name=tool_name, arguments=arguments)
                from cognis.models.tool import ExecutorHandle
                from cognis.tools.registry import ToolExecutionContext

                ctx = ToolExecutionContext(
                    executor_handle=ExecutorHandle(
                        executor_id=self.config.executor_id,
                        executor_type="remote",
                    ),
                    runtime_metadata={
                        **self._runtime_metadata,
                        **request_runtime_metadata,
                    },
                    shared_runtime_metadata=self._runtime_metadata,
                    execution_scope_id=str(
                        params.get("execution_scope_id")
                        or f"{self.config.executor_id}:{self._runtime_metadata.get('user_email', 'runtime')}"
                    ),
                )

                async def _send_tool_chunk(delta: str, stream: str | None) -> None:
                    async with send_lock:
                        await self._send_ws(
                            ws,
                            json.dumps(
                                {
                                    "jsonrpc": "2.0",
                                    "method": "tool.progress",
                                    "params": {
                                        "call_id": call_id,
                                        "tool_name": tool_name,
                                        "delta": delta,
                                        "stream": stream,
                                    },
                                }
                            ),
                        )

                ctx.output_chunk_callback = _send_tool_chunk

                async def _invoke() -> Any:
                    return await handler(tool_call.arguments, ctx)

                raw = (
                    await asyncio.wait_for(_invoke(), timeout=timeout_seconds)
                    if timeout_seconds
                    else await _invoke()
                )
                result = _normalize_result(raw, int((perf_counter() - start) * 1000))
        except TimeoutError:
            result = ToolResult(output="Tool execution timed out.", is_error=True)
        except asyncio.CancelledError:
            result = ToolResult(output="Tool execution cancelled.", is_error=True)
        except MCPClientError as exc:
            result = ToolResult(
                output=f"MCP authentication failed: {exc.auth_error or exc.error_class}",
                is_error=True,
                duration_ms=int((perf_counter() - start) * 1000),
                metadata={
                    "mcp_auth_error": True,
                    "server_id": tool_server_id,
                    "server_name": exc.server_name,
                    "phase": exc.phase,
                    "status_code": exc.status_code,
                    "auth_error": exc.auth_error,
                    "authorization_required": exc.authorization_required,
                    "www_authenticate": exc.www_authenticate,
                    "authorization_challenge": exc.authorization_challenge,
                },
            )
        except BrowserLifecycleError as exc:
            result = ToolResult(
                output=str(exc),
                is_error=True,
                duration_ms=int((perf_counter() - start) * 1000),
                metadata={"browser_lifecycle_error": exc.code},
            )
        except Exception as exc:
            result = ToolResult(
                output=f"Tool execution failed: {str(exc)[:500]}",
                is_error=True,
                duration_ms=int((perf_counter() - start) * 1000),
            )
        finally:
            self._active_calls.pop(call_id, None)

        async with send_lock:
            await self._send_rpc_result(
                ws,
                msg_id,
                {
                    "call_id": call_id,
                    "output": result.output,
                    "is_error": result.is_error,
                    "duration_ms": result.duration_ms,
                    "metadata": result.metadata,
                    "attachments": result.attachments,
                },
            )

    async def _handle_browser_session_terminal(
        self,
        ws: Any,
        msg_id: str | None,
        params: dict[str, Any],
    ) -> None:
        manager = self._runtime_metadata.get(BROWSER_MANAGER_KEY)
        retainer = self._browser_cleanup_retainer
        raw_owner = params.get("owner")
        if not isinstance(manager, BrowserManager) and not isinstance(
            retainer, BrowserManagerCleanupRetainer
        ):
            await self._send_rpc_result(ws, msg_id, {"closed": 0, "complete": True})
            return
        if not isinstance(raw_owner, dict):
            await self._send_rpc_error(
                ws,
                msg_id,
                -32602,
                "browser.session_terminal requires owner metadata",
            )
            return
        try:
            owner = BrowserSessionOwner.from_dict(raw_owner)
            closed = 0
            if isinstance(manager, BrowserManager):
                closed += await manager.mark_owner_terminal(owner)
            if isinstance(retainer, BrowserManagerCleanupRetainer):
                closed += await retainer.mark_owner_terminal(owner)
        except BrowserLifecycleError as exc:
            await self._send_rpc_error(
                ws,
                msg_id,
                -32020,
                str(exc),
                data={"code": exc.code, "retryable": True},
            )
            return
        await self._send_rpc_result(ws, msg_id, {"closed": closed, "complete": True})

    async def _handle_llm_complete(
        self, ws: Any, msg_id: str | None, params: dict[str, Any]
    ) -> None:
        if self._inference_handler is None:
            await self._send_rpc_error(ws, msg_id, -32601, "Inference handler unavailable")
            return

        request_id = params.get("request_id", msg_id or uuid.uuid4().hex)
        if msg_id is not None:
            await self._send_rpc_result(ws, msg_id, {"status": "streaming"})

        request_kwargs = dict(params.get("request_kwargs") or {})

        async for chunk in self._inference_handler.stream_complete(
            model=str(params.get("model", "")),
            messages=list(params.get("messages", [])),
            request_kwargs=request_kwargs,
            request_id=str(request_id),
            backend=params.get("backend") if isinstance(params.get("backend"), str) else None,
            provider_id=params.get("provider_id")
            if isinstance(params.get("provider_id"), str)
            else None,
            owner_email=params.get("owner_email")
            if isinstance(params.get("owner_email"), str)
            else None,
            backend_metadata=params.get("backend_metadata")
            if isinstance(params.get("backend_metadata"), dict)
            else None,
        ):
            safe_chunk = json_safe_inference_payload(chunk)
            if not isinstance(safe_chunk, dict):
                safe_chunk = {}
            if chunk.get("done"):
                await self._send_ws(
                    ws,
                    json.dumps(
                        {
                            "jsonrpc": "2.0",
                            "method": "llm.done",
                            "params": {
                                "request_id": request_id,
                                "usage": safe_chunk.get("usage", {}),
                                "finish_reason": safe_chunk.get("finish_reason", "stop"),
                                "response_status": safe_chunk.get("response_status", "completed"),
                                "error": safe_chunk.get("error"),
                                "backend_metadata": safe_chunk.get("backend_metadata"),
                            },
                        }
                    ),
                )
            else:
                chunk_params: dict[str, Any] = {
                    "request_id": request_id,
                    "content": safe_chunk.get("content"),
                    "tool_calls": safe_chunk.get("tool_calls"),
                    "reasoning_content": safe_chunk.get("reasoning_content"),
                    "reasoning": safe_chunk.get("reasoning"),
                    "refusal": safe_chunk.get("refusal"),
                    "index": safe_chunk.get("index", 0),
                }
                # Structured stream fields (thinking block boundaries, raw
                # Responses output items, apply_patch progress, liveness
                # markers) — required for executor-routed inference to match
                # controller-direct behavior.
                for extra_key in (
                    "anthropic_native_events",
                    "reasoning_part_boundary",
                    "tool_progress",
                    "responses_output_item",
                    "provider_event_type",
                    "response_item_id",
                    "content_source",
                    "response_message_phase",
                    "provider_thinking_blocks",
                ):
                    if safe_chunk.get(extra_key) is not None:
                        chunk_params[extra_key] = safe_chunk[extra_key]
                await self._send_ws(
                    ws,
                    json.dumps(
                        {
                            "jsonrpc": "2.0",
                            "method": "llm.chunk",
                            "params": chunk_params,
                        }
                    ),
                )

    async def _handle_llm_discover_models(
        self, ws: Any, msg_id: str | None, params: dict[str, Any]
    ) -> None:
        if self._inference_handler is None:
            await self._send_rpc_error(ws, msg_id, -32601, "Inference handler unavailable")
            return

        try:
            models = await self._inference_handler.discover_models(
                preset=str(params.get("preset", "")),
                base_url=str(params.get("base_url", "")),
                api_key=str(params.get("api_key") or ""),
            )
            safe_models = json_safe_inference_payload(models)
            await self._send_rpc_result(
                ws,
                msg_id,
                {"models": safe_models if isinstance(safe_models, list) else []},
            )
        except Exception as exc:
            await self._send_rpc_error(ws, msg_id, -32000, str(exc)[:500])

    async def _handle_local_model_status(self, ws: Any, msg_id: str | None) -> None:
        if self._ollama_runtime_handler is None:
            await self._send_rpc_error(
                ws,
                msg_id,
                -32601,
                "Managed Ollama runtime unavailable",
            )
            return
        status = await self._ollama_runtime_handler.inspect()
        await self._send_rpc_result(ws, msg_id, status.model_dump(mode="json"))

    async def _handle_local_model_show(
        self,
        ws: Any,
        msg_id: str | None,
        params: dict[str, Any],
    ) -> None:
        if self._ollama_runtime_handler is None:
            await self._send_rpc_error(
                ws,
                msg_id,
                -32601,
                "Managed Ollama runtime unavailable",
            )
            return
        try:
            request = OllamaRuntimeModelRequest.model_validate(params)
            result = await self._ollama_runtime_handler.inspect_model(request.runtime_name)
            await self._send_rpc_result(ws, msg_id, result)
        except Exception as exc:
            await self._send_rpc_error(
                ws,
                msg_id,
                -32040,
                _safe_message(str(exc), limit=500),
            )

    async def _handle_local_model_operation_start(
        self,
        ws: Any,
        msg_id: str | None,
        params: dict[str, Any],
    ) -> None:
        if self._ollama_runtime_handler is None:
            await self._send_rpc_error(
                ws,
                msg_id,
                -32601,
                "Managed Ollama runtime unavailable",
            )
            return
        try:
            operation = OllamaRuntimeStartRequest.model_validate(params)

            async def _progress(payload: dict[str, Any]) -> None:
                await self._send_ws(
                    ws,
                    json.dumps(
                        {
                            "jsonrpc": "2.0",
                            "method": "local_model.progress",
                            "params": payload,
                        }
                    ),
                )

            async def _complete(payload: dict[str, Any]) -> None:
                await self._send_ws(
                    ws,
                    json.dumps(
                        {
                            "jsonrpc": "2.0",
                            "method": "local_model.completed",
                            "params": payload,
                        }
                    ),
                )

            status = await self._ollama_runtime_handler.start(
                operation,
                on_progress=_progress,
                on_complete=_complete,
            )
            await self._send_rpc_result(ws, msg_id, status.model_dump(mode="json"))
        except Exception as exc:
            await self._send_rpc_error(
                ws,
                msg_id,
                -32040,
                _safe_message(str(exc), limit=500),
            )

    async def _handle_local_model_operation_status(
        self,
        ws: Any,
        msg_id: str | None,
        params: dict[str, Any],
    ) -> None:
        if self._ollama_runtime_handler is None:
            await self._send_rpc_error(
                ws,
                msg_id,
                -32601,
                "Managed Ollama runtime unavailable",
            )
            return
        operation_id = str(params.get("operation_id") or "")
        status = self._ollama_runtime_handler.operation_status(operation_id)
        if status is None:
            await self._send_rpc_error(
                ws,
                msg_id,
                -32044,
                "Managed Ollama operation not found",
            )
            return
        await self._send_rpc_result(ws, msg_id, status.model_dump(mode="json"))

    async def _handle_local_model_operation_cancel(
        self,
        ws: Any,
        msg_id: str | None,
        params: dict[str, Any],
    ) -> None:
        if self._ollama_runtime_handler is None:
            await self._send_rpc_error(
                ws,
                msg_id,
                -32601,
                "Managed Ollama runtime unavailable",
            )
            return
        operation_id = str(params.get("operation_id") or "")
        cancelled = await self._ollama_runtime_handler.cancel(operation_id)
        await self._send_rpc_result(
            ws,
            msg_id,
            {
                "acknowledged": cancelled,
                "rollback_guaranteed": False,
            },
        )

    async def _handle_llm_transcribe(
        self, ws: Any, msg_id: str | None, params: dict[str, Any]
    ) -> None:
        if self._inference_handler is None:
            await self._send_rpc_error(ws, msg_id, -32601, "Inference handler unavailable")
            return

        try:
            encoding = str(params.get("audio_encoding", "hex"))
            audio_payload = str(params.get("audio_base64", ""))
            if encoding != "hex":
                raise ValueError("Unsupported audio encoding")
            audio_bytes = bytes.fromhex(audio_payload)
            request_kwargs = dict(params.get("request_kwargs") or {})
            result = await self._inference_handler.transcribe(
                audio_bytes=audio_bytes,
                mime_type=str(params.get("mime_type", "application/octet-stream")),
                filename=str(params.get("filename", "audio.bin")),
                model=str(params.get("model", "")),
                provider_preset=params.get("provider_preset"),
                supported_audio_mime_types=params.get("supported_audio_mime_types"),
                request_kwargs=request_kwargs,
                prompt=params.get("prompt"),
                language=params.get("language"),
            )
            await self._send_rpc_result(ws, msg_id, result)
        except Exception as exc:
            await self._send_rpc_error(ws, msg_id, -32000, str(exc)[:500])

    async def _handle_llm_image_generate(
        self, ws: Any, msg_id: str | None, params: dict[str, Any]
    ) -> None:
        if self._inference_handler is None:
            await self._send_rpc_error(ws, msg_id, -32601, "Inference handler unavailable")
            return

        try:
            request_kwargs = dict(params.get("request_kwargs") or {})
            result = await self._inference_handler.image_generate(
                prompt=str(params.get("prompt", "")),
                model=str(params.get("model", "")),
                strategy=str(params.get("strategy", "aimage_generation")),
                n=int(params.get("n", 1) or 1),
                size=params.get("size"),
                quality=params.get("quality"),
                response_format=str(params.get("response_format", "b64_json")),
                image=params.get("image"),
                request_kwargs=request_kwargs,
            )
            await self._send_rpc_result(ws, msg_id, result)
        except Exception as exc:
            await self._send_rpc_error(ws, msg_id, -32000, str(exc)[:500])

    async def _handle_llm_synthesize(
        self, ws: Any, msg_id: str | None, params: dict[str, Any]
    ) -> None:
        if self._inference_handler is None:
            await self._send_rpc_error(ws, msg_id, -32601, "Inference handler unavailable")
            return

        try:
            request_kwargs = dict(params.get("request_kwargs") or {})
            speed = params.get("speed", 1.0)
            if not isinstance(speed, int | float):
                speed = 1.0
            result = await self._inference_handler.synthesize(
                text=str(params.get("text", "")),
                voice=str(params.get("voice", "")),
                model=str(params.get("model", "")),
                provider_preset=params.get("provider_preset"),
                response_format=str(params.get("response_format", "mp3")),
                speed=float(speed),
                request_kwargs=request_kwargs,
                low_latency=bool(params.get("low_latency", False)),
            )
            await self._send_rpc_result(ws, msg_id, result)
        except Exception as exc:
            await self._send_rpc_error(ws, msg_id, -32000, str(exc)[:500])

    async def _handle_channel_start(
        self, ws: Any, msg_id: str | None, params: dict[str, Any]
    ) -> None:
        if self._channel_handler is None:
            await self._send_rpc_error(ws, msg_id, -32601, "Channel handler unavailable")
            return
        try:
            result = await self._channel_handler.start(
                account_id=params.get("account_id", ""),
                channel_type=params.get("channel_type", ""),
                config=params.get("config", {}),
                credentials=params.get("credentials", {}),
            )
            await self._send_rpc_result(ws, msg_id, result)
        except Exception as exc:
            await self._send_rpc_error(ws, msg_id, -32000, str(exc)[:500])

    async def _handle_channel_stop(
        self, ws: Any, msg_id: str | None, params: dict[str, Any]
    ) -> None:
        if self._channel_handler is None:
            await self._send_rpc_error(ws, msg_id, -32601, "Channel handler unavailable")
            return
        try:
            result = await self._channel_handler.stop(account_id=params.get("account_id", ""))
            await self._send_rpc_result(ws, msg_id, result)
        except Exception as exc:
            await self._send_rpc_error(ws, msg_id, -32000, str(exc)[:500])

    async def _handle_channel_send(
        self, ws: Any, msg_id: str | None, params: dict[str, Any]
    ) -> None:
        if self._channel_handler is None:
            await self._send_rpc_error(ws, msg_id, -32601, "Channel handler unavailable")
            return
        try:
            result = await self._channel_handler.send(
                account_id=params.get("account_id", ""),
                message=params.get("message", {}),
            )
            await self._send_rpc_result(ws, msg_id, result)
        except Exception as exc:
            await self._send_rpc_error(ws, msg_id, -32000, str(exc)[:500])

    async def _handle_channel_fetch_media(
        self, ws: Any, msg_id: str | None, params: dict[str, Any]
    ) -> None:
        if self._channel_handler is None:
            await self._send_rpc_error(ws, msg_id, -32601, "Channel handler unavailable")
            return
        try:
            result = await self._channel_handler.fetch_media(
                account_id=params.get("account_id", ""),
                message=params.get("message", {}),
                attachment=params.get("attachment", {}),
                stt_supported_mime_types=params.get("stt_supported_mime_types"),
            )
            await self._send_rpc_result(ws, msg_id, result)
        except Exception as exc:
            await self._send_rpc_error(ws, msg_id, -32000, str(exc)[:500])

    async def _handle_channel_typing(
        self, ws: Any, msg_id: str | None, params: dict[str, Any]
    ) -> None:
        if self._channel_handler is None:
            await self._send_rpc_error(ws, msg_id, -32601, "Channel handler unavailable")
            return
        try:
            result = await self._channel_handler.send_typing(
                account_id=params.get("account_id", ""),
                chat_id=params.get("chat_id", ""),
            )
            await self._send_rpc_result(ws, msg_id, result)
        except Exception as exc:
            await self._send_rpc_error(ws, msg_id, -32000, str(exc)[:500])

    async def _handle_channel_mark_read(
        self, ws: Any, msg_id: str | None, params: dict[str, Any]
    ) -> None:
        if self._channel_handler is None:
            await self._send_rpc_error(ws, msg_id, -32601, "Channel handler unavailable")
            return
        try:
            result = await self._channel_handler.mark_read(
                account_id=params.get("account_id", ""),
                chat_id=params.get("chat_id", ""),
                message_id=params.get("message_id", ""),
            )
            await self._send_rpc_result(ws, msg_id, result)
        except Exception as exc:
            await self._send_rpc_error(ws, msg_id, -32000, str(exc)[:500])

    async def _handle_channel_sync_profile(
        self, ws: Any, msg_id: str | None, params: dict[str, Any]
    ) -> None:
        if self._channel_handler is None:
            await self._send_rpc_error(ws, msg_id, -32601, "Channel handler unavailable")
            return
        try:
            result = await self._channel_handler.sync_profile(
                account_id=params.get("account_id", ""),
                profile_data=params,
            )
            await self._send_rpc_result(ws, msg_id, result)
        except Exception as exc:
            await self._send_rpc_error(ws, msg_id, -32000, str(exc)[:500])

    async def _heartbeat_loop(self, ws: Any) -> None:
        while self._running:
            resource_snapshot = await self._refresh_resource_snapshot(force=False)
            params: dict[str, Any] = {
                "uptime_seconds": int(perf_counter() - self._started_at),
                "active_calls": len(self._active_calls),
                "configured": self._configured,
                "runtime_state": self._runtime_state,
                "config_version": self._config_version,
            }
            if resource_snapshot is not None:
                params["resource_snapshot"] = resource_snapshot
            await self._send_ws(
                ws,
                json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "method": "executor.heartbeat",
                        "params": params,
                    }
                ),
            )
            await asyncio.sleep(_HEARTBEAT_INTERVAL)

    async def _refresh_resource_snapshot(
        self,
        *,
        force: bool,
        return_cached: bool = False,
    ) -> dict[str, Any] | None:
        now = perf_counter()
        runtime = ExecutorRuntimeResourceSnapshot(
            uptime_seconds=max(0, int(now - self._started_at)),
            active_calls=len(self._active_calls),
            configured=self._configured,
            state=self._runtime_state,
        )
        if (
            not force
            and self._resource_snapshot_collected_at is not None
            and now - self._resource_snapshot_collected_at < _RESOURCE_SNAPSHOT_INTERVAL_SECONDS
        ):
            if return_cached and self._resource_snapshot is not None:
                cached_snapshot = {
                    **self._resource_snapshot,
                    "runtime": runtime.model_dump(mode="json"),
                }
                self._resource_snapshot = cached_snapshot
                if self._runtime_metadata:
                    self._runtime_metadata["resource_snapshot"] = cached_snapshot
                    self._runtime_metadata["platform"] = _build_platform_payload()
                return cached_snapshot
            return None
        try:
            ollama_config = (
                self._ollama_runtime_handler.config
                if self._ollama_runtime_handler is not None
                else None
            )
            snapshot = await self._resource_collector.collect(
                runtime=runtime,
                ollama_endpoint=(
                    ollama_config.endpoint if ollama_config is not None else OLLAMA_MANAGED_ENDPOINT
                ),
                ollama_model_store_path=(
                    ollama_config.model_store_path if ollama_config is not None else None
                ),
            )
        except Exception:
            logger.debug("Failed to collect executor resource snapshot", exc_info=True)
            return None
        payload = snapshot.model_dump(mode="json", exclude={"freshness"})
        self._resource_snapshot = payload
        self._resource_snapshot_collected_at = now
        if self._runtime_metadata:
            self._runtime_metadata["resource_snapshot"] = payload
            self._runtime_metadata["platform"] = _build_platform_payload()
        return payload

    async def _start_mcp_clients(
        self, servers: list[MCPServerConfig], secrets: dict[str, str]
    ) -> dict[str, MCPClient]:
        clients: dict[str, MCPClient] = {}
        for server in servers:
            client = build_mcp_client(server, secrets)
            await client.connect()
            clients[runtime_mcp_server_key(server)] = client
        return clients

    async def _prepare_mcp_runtime(
        self, servers: list[MCPServerConfig], secrets: dict[str, str]
    ) -> tuple[dict[str, MCPClient], list[ToolDefinition], list[dict[str, Any]], list[str]]:
        clients: dict[str, MCPClient] = {}
        discovered: list[ToolDefinition] = []
        statuses: list[dict[str, Any]] = []
        warnings: list[str] = []
        for server in servers:
            logger.info(
                "MCP: starting server %s (command=%s, transport=%s)",
                server.name,
                server.command,
                server.transport,
            )
            logger.debug(
                "MCP: server %s full config: args=%s, env_keys=%s, header_keys=%s, timeout=%ds",
                server.name,
                server.args,
                sorted(server.env.keys()) if server.env else [],
                sorted(server.headers.keys()) if server.headers else [],
                server.timeout_seconds,
            )
            client = build_mcp_client(server, secrets)
            try:
                await client.connect()
                tools = await client.list_tools()
            except MCPClientError as exc:
                logger.warning(
                    "MCP: server %s failed during %s (%s, timed_out=%s)",
                    server.name,
                    exc.phase,
                    exc.error_class,
                    exc.timed_out,
                )
                if exc.safe_stderr:
                    logger.warning("MCP: server %s stderr: %s", server.name, exc.safe_stderr)
                await self._close_failed_mcp_client(client, server.name)
                statuses.append(
                    {
                        "server_id": server.server_id,
                        "name": server.name,
                        "phase": exc.phase,
                        "status": "failed",
                        "error_class": exc.error_class,
                        "timed_out": exc.timed_out,
                        "message": str(exc),
                        "stderr_summary": exc.safe_stderr,
                        "auth_error": exc.auth_error,
                        "authorization_required": exc.authorization_required,
                        "status_code": exc.status_code,
                        "www_authenticate": exc.www_authenticate,
                        "authorization_challenge": exc.authorization_challenge,
                    }
                )
                if exc.authorization_required:
                    warnings.append(
                        f"MCP server {server.name} requires authorization during {exc.phase}."
                    )
                else:
                    warnings.append(f"MCP server {server.name} failed during {exc.phase}.")
                continue
            except BaseException as exc:
                if _should_reraise_isolated_exception(exc):
                    raise
                logger.warning(
                    "MCP: server %s failed with isolated initialization error: %s",
                    server.name,
                    _safe_base_exception_message(exc),
                    exc_info=True,
                )
                await self._close_failed_mcp_client(client, server.name)
                statuses.append(
                    {
                        "server_id": server.server_id,
                        "name": server.name,
                        "phase": "initialize",
                        "status": "failed",
                        "error_class": exc.__class__.__name__.lower(),
                        "timed_out": False,
                        "message": _safe_base_exception_message(exc),
                    }
                )
                warnings.append(f"MCP server {server.name} failed to initialize.")
                continue
            logger.info(
                "MCP: server %s ready (%d tool(s) discovered)",
                server.name,
                len(tools),
            )
            clients[runtime_mcp_server_key(server)] = client
            statuses.append(
                {
                    "server_id": server.server_id,
                    "name": server.name,
                    "phase": "ready",
                    "status": "ready",
                    "tool_count": len(tools),
                }
            )
            discovered.extend(
                mcp_tools_to_definitions(
                    server.name,
                    tools,
                    timeout_seconds=server.timeout_seconds,
                    server_id=server.server_id,
                )
            )
        return clients, discovered, statuses, warnings

    async def _close_failed_mcp_client(self, client: MCPClient, server_name: str) -> None:
        """Best-effort MCP cleanup that cannot abort executor configuration."""

        try:
            await client.close(suppress_cancelled=True)
        except BaseException as exc:
            logger.debug(
                "MCP: server %s cleanup failed after initialization error: %s",
                server_name,
                type(exc).__name__,
            )

    async def _discover_mcp_tools(self, servers: list[MCPServerConfig]) -> list[ToolDefinition]:
        discovered: list[ToolDefinition] = []
        for server in servers:
            tools = await self._mcp_clients[runtime_mcp_server_key(server)].list_tools()
            discovered.extend(
                mcp_tools_to_definitions(
                    server.name,
                    tools,
                    timeout_seconds=server.timeout_seconds,
                    server_id=server.server_id,
                )
            )
        return discovered

    def _build_mcp_handler(self, tool: ToolDefinition) -> Any:
        async def _handler(arguments: dict[str, Any], _: Any) -> Any:
            client = self._mcp_clients[runtime_mcp_server_key(tool.source)]
            raw_tool_name = tool.source.raw_tool_name or tool.name
            return await client.call_tool(raw_tool_name, arguments)

        return _handler

    async def _close_mcp_clients(self) -> None:
        await self._close_clients(self._mcp_clients, suppress_cancelled=True)
        self._mcp_clients = {}

    async def _close_clients(
        self, clients: dict[str, MCPClient], *, suppress_cancelled: bool = False
    ) -> None:
        for client in clients.values():
            try:
                await client.close(suppress_cancelled=suppress_cancelled)
            except asyncio.CancelledError:
                if not suppress_cancelled:
                    raise
                logger.debug("MCP client close cancelled; suppressed during stale client teardown")
            except BaseException as exc:
                # Catches Exception, BaseExceptionGroup (Python 3.11+ / anyio
                # exceptiongroup backport), and other BaseException subclasses
                # raised by anyio cross-task cancel-scope teardown.  We must
                # not let these propagate: they would bypass run()'s except
                # Exception handler and cause the executor to exit.
                # Re-raise process-level signals so shutdown is never blocked.
                if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                    raise
                logger.debug(
                    "MCP client close raised; suppressed during stale client teardown",
                    exc_info=True,
                )

    def _public_runtime_metadata(self) -> dict[str, Any]:
        metadata = dict(self._runtime_metadata)
        metadata.pop("web_secrets", None)
        metadata.pop("skill_manifests", None)
        metadata.pop(BROWSER_MANAGER_KEY, None)
        metadata.pop(LSP_MANAGER_KEY, None)
        metadata.pop(SHELL_MANAGER_KEY, None)
        metadata.pop("background_shell_completion_callback", None)
        metadata.pop(_FILE_FRESHNESS_KEY, None)
        return metadata

    async def _handle_oauth_loopback_start(
        self, ws: Any, msg_id: str | None, params: dict[str, Any]
    ) -> None:
        ttl_seconds = _coerce_bounded_int(
            params.get("ttl_seconds"),
            default=_OAUTH_LOOPBACK_DEFAULT_TTL_SECONDS,
            minimum=1,
            maximum=_OAUTH_LOOPBACK_MAX_TTL_SECONDS,
        )
        callback_path = str(params.get("callback_path") or _OAUTH_LOOPBACK_CALLBACK_PATH)
        if not callback_path.startswith("/") or "?" in callback_path or "#" in callback_path:
            await self._send_rpc_error(ws, msg_id, -32602, "Invalid OAuth callback path")
            return
        expected_state = str(params.get("state") or "")
        if not expected_state:
            await self._send_rpc_error(ws, msg_id, -32602, "OAuth state is required")
            return

        listener_id = f"oauthlb_{uuid.uuid4().hex[:16]}"

        async def _handle_client(
            reader: asyncio.StreamReader,
            writer: asyncio.StreamWriter,
        ) -> None:
            await self._handle_oauth_loopback_http_request(
                ws=ws,
                listener_id=listener_id,
                expected_state=expected_state,
                callback_path=callback_path,
                reader=reader,
                writer=writer,
            )

        try:
            server = await asyncio.start_server(_handle_client, "127.0.0.1", 0)
        except OSError as exc:
            await self._send_rpc_error(
                ws,
                msg_id,
                -32060,
                f"Failed to start OAuth loopback listener: {_safe_message(str(exc))}",
            )
            return

        sockets: list[Any] = list(server.sockets or [])
        if not sockets:
            server.close()
            await server.wait_closed()
            await self._send_rpc_error(ws, msg_id, -32060, "OAuth loopback listener has no socket")
            return
        port = int(sockets[0].getsockname()[1])
        redirect_uri = f"http://127.0.0.1:{port}{callback_path}"
        expires_at = datetime.now(UTC).timestamp() + ttl_seconds
        cleanup_task = asyncio.create_task(
            self._expire_oauth_loopback_listener(listener_id, ttl_seconds),
            name=f"oauth-loopback-expire-{listener_id}",
        )
        self._oauth_loopback_listeners[listener_id] = {
            "server": server,
            "cleanup_task": cleanup_task,
            "redirect_uri": redirect_uri,
        }
        await self._send_rpc_result(
            ws,
            msg_id,
            {
                "listener_id": listener_id,
                "redirect_uri": redirect_uri,
                "expires_at": datetime.fromtimestamp(expires_at, UTC).isoformat(),
            },
        )

    async def _handle_oauth_loopback_stop(
        self, ws: Any, msg_id: str | None, params: dict[str, Any]
    ) -> None:
        listener_id = str(params.get("listener_id") or "")
        stopped = await self._close_oauth_loopback_listener(listener_id)
        await self._send_rpc_result(ws, msg_id, {"stopped": stopped})

    async def _handle_oauth_loopback_http_request(
        self,
        *,
        ws: Any,
        listener_id: str,
        expected_state: str,
        callback_path: str,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        status = "400 Bad Request"
        body = "MCP OAuth callback failed."
        notify_payload: dict[str, Any] | None = None
        try:
            request_line = await asyncio.wait_for(reader.readline(), timeout=5.0)
            line = request_line.decode("latin-1", errors="replace").strip()
            parts = line.split(" ", 2)
            method = parts[0] if len(parts) >= 1 else ""
            target = parts[1] if len(parts) >= 2 else ""
            while True:
                header_line = await asyncio.wait_for(reader.readline(), timeout=5.0)
                if header_line in {b"\r\n", b"\n", b""}:
                    break
            parsed = urlsplit(target)
            query = parse_qs(parsed.query, keep_blank_values=True)
            state = query.get("state", [""])[0]
            code = query.get("code", [""])[0]
            error = query.get("error", [""])[0]
            error_description = query.get("error_description", [""])[0]
            listener = self._oauth_loopback_listeners.get(listener_id)
            redirect_uri = str(listener.get("redirect_uri") if listener else "")
            if method != "GET" or parsed.path != callback_path:
                body = "Invalid MCP OAuth callback path."
            elif state != expected_state:
                body = "Invalid MCP OAuth state."
            elif not code and not error:
                body = "MCP OAuth callback is missing code or error."
            else:
                status = "200 OK"
                body = "MCP OAuth callback received. You can close this window."
                notify_payload = {
                    "listener_id": listener_id,
                    "redirect_uri": redirect_uri,
                    "state": state,
                    "code": code,
                    "error": error or None,
                    "error_description": error_description or None,
                }
        except Exception:
            logger.warning(
                "executor: failed to handle OAuth loopback callback",
                extra={"extra_data": {"listener_id": listener_id}},
                exc_info=True,
            )
        finally:
            escaped_body = body.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            content = (f"<!doctype html><html><body><h1>{escaped_body}</h1></body></html>").encode()
            writer.write(
                (
                    f"HTTP/1.1 {status}\r\n"
                    "Content-Type: text/html; charset=utf-8\r\n"
                    f"Content-Length: {len(content)}\r\n"
                    "Connection: close\r\n\r\n"
                ).encode()
                + content
            )
            with contextlib.suppress(Exception):
                await writer.drain()
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()
        if notify_payload is not None:
            await self._send_ws(
                ws,
                json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "method": "oauth.loopback_callback",
                        "params": notify_payload,
                    }
                ),
            )
            await self._close_oauth_loopback_listener(listener_id)

    async def _expire_oauth_loopback_listener(self, listener_id: str, ttl_seconds: int) -> None:
        try:
            await asyncio.sleep(ttl_seconds)
            await self._close_oauth_loopback_listener(listener_id)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning(
                "executor: failed to expire OAuth loopback listener",
                extra={"extra_data": {"listener_id": listener_id}},
                exc_info=True,
            )

    async def _close_oauth_loopback_listener(self, listener_id: str) -> bool:
        if not listener_id:
            return False
        listener = self._oauth_loopback_listeners.pop(listener_id, None)
        if listener is None:
            return False
        cleanup_task = listener.get("cleanup_task")
        if isinstance(cleanup_task, asyncio.Task) and cleanup_task is not asyncio.current_task():
            cleanup_task.cancel()
        server = listener.get("server")
        if server is not None:
            server.close()
            await server.wait_closed()
        return True

    async def _close_oauth_loopback_listeners(self) -> None:
        for listener_id in list(self._oauth_loopback_listeners):
            await self._close_oauth_loopback_listener(listener_id)

    async def _send_rpc_result(self, ws: Any, msg_id: str | None, result: dict[str, Any]) -> None:
        if msg_id is None:
            return
        await self._send_ws(ws, json.dumps({"jsonrpc": "2.0", "result": result, "id": msg_id}))

    async def _send_rpc_error(
        self,
        ws: Any,
        msg_id: str | None,
        code: int,
        message: str,
        *,
        data: dict[str, Any] | None = None,
    ) -> None:
        if msg_id is None:
            return
        error: dict[str, Any] = {"code": code, "message": message}
        if data is not None:
            error["data"] = data
        await self._send_ws(
            ws,
            json.dumps({"jsonrpc": "2.0", "error": error, "id": msg_id}),
        )

    async def _send_ws(self, ws: Any, payload: str) -> None:
        async with self._ws_send_lock:
            try:
                await ws.send(payload)
            except ConnectionClosed:
                logger.debug("Skipping websocket send because connection is closed")
                raise


def _normalize_result(raw: Any, duration_ms: int) -> ToolResult:
    if isinstance(raw, ToolResult):
        return raw.model_copy(update={"duration_ms": raw.duration_ms or duration_ms})
    if isinstance(raw, (dict, list)):
        output = json.dumps(raw, sort_keys=True, default=str)
    elif isinstance(raw, str):
        output = raw
    else:
        output = str(raw)
    return ToolResult(output=output, duration_ms=duration_ms)
