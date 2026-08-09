"""WebSocket-based remote executor provider and connection."""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import json
import os
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path
from time import perf_counter
from typing import Any, cast

from prometheus_client import Counter, Gauge, Histogram
from sqlalchemy import select
from starlette.websockets import WebSocket, WebSocketDisconnect

from cognis.core.executor_connection_ownership import (
    EXECUTOR_CONNECTION_LEASE_TTL_SECONDS,
    ExecutorConnectionOwner,
)
from cognis.logging import get_logger
from cognis.models.config import ProviderHealth
from cognis.models.executor_resources import normalize_executor_resource_snapshot
from cognis.models.local_models import (
    OllamaRuntimeOperationStatus,
    OllamaRuntimeStartRequest,
    OllamaRuntimeStatus,
)
from cognis.models.tool import (
    ExecutorCapabilities,
    ExecutorConfig,
    ExecutorHandle,
    ToolCall,
    ToolResult,
)
from cognis.ownership import is_shared_owner_email
from cognis.providers.base import ToolOutputChunkCallback
from cognis.providers.circuit_breaker import CircuitBreaker, CircuitBreakerError
from cognis.providers.executor.delivery import DeliveryState, ExecutorDeliveryError
from cognis.providers.executor.forwarding import ForwardedExecutorConnection
from cognis.store.coordination import database_now_expression
from cognis.store.models import CoordinationLeaseRow, ExecutorRow
from cognis.tools.executor.lsp.runtime import (
    LSP_STATUS_CAPABILITY,
    LSPStatusReport,
    build_lsp_unavailable_report,
)

_logger = get_logger(__name__)


def _inference_chunk_timeout_seconds() -> float:
    """Max inter-chunk wait for executor-routed inference streams.

    Executors forward provider liveness markers as chunks, so a healthy
    provider produces at least one chunk per liveness event even during long
    reasoning phases. This timeout is a dead-man's switch for a genuinely
    stalled executor/provider, not a policy limit — the previous hard-coded
    120s killed long thinking phases whose liveness chunks were filtered.
    """
    raw = os.environ.get("COGNIS_EXECUTOR_INFERENCE_CHUNK_TIMEOUT_SECONDS", "300")
    try:
        value = float(raw)
    except ValueError:
        return 300.0
    return value if value > 0 else 300.0


# ---------------------------------------------------------------------------
# Prometheus metrics
# ---------------------------------------------------------------------------

EXECUTOR_WS_CONNECTIONS = Gauge(
    "cognis_executor_ws_connections_active",
    "Currently connected remote executors",
)
EXECUTOR_WS_RPC_DURATION = Histogram(
    "cognis_executor_ws_rpc_duration_seconds",
    "WebSocket RPC call duration",
    labelnames=("method",),
)
EXECUTOR_WS_RPC_ERRORS = Counter(
    "cognis_executor_ws_rpc_errors_total",
    "WebSocket RPC errors",
    labelnames=("method", "error_type"),
)
EXECUTOR_WS_RECONNECTIONS = Counter(
    "cognis_executor_ws_reconnections_total",
    "Executor reconnection events",
)

# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------

_RPC_TIMEOUT_SECONDS = 300  # default per-call timeout
_HEARTBEAT_INTERVAL = 15  # seconds between executor heartbeats
_HEARTBEAT_TIMEOUT = 45  # mark unhealthy after this many seconds
_LSP_STATUS_TIMEOUT_SECONDS = 5.0
_RESOURCE_SNAPSHOT_DISPATCH_INTERVAL_SECONDS = 30.0
_RESOURCE_SNAPSHOT_MAX_NODES = 256
_RESOURCE_SNAPSHOT_MAX_DEPTH = 8
_RESOURCE_SNAPSHOT_MAX_STRING_LENGTH = 512
_LOCAL_MODEL_NOTIFICATION_MAX_PENDING = 256
_OWNED_CALLBACK_POLL_SECONDS = 0.25
_TOOL_PROGRESS_MAX_PENDING_PER_CALL = 64
_INFERENCE_EVENT_QUEUE_MAX_PENDING = 128
_PHYSICAL_CANCEL_SEND_TIMEOUT_SECONDS = 1.0
_MIN_RECONNECT_RETRY_BUDGET_SECONDS = 60.0
_RECONNECT_RETRY_BUDGET_ENV = "COGNIS_EXECUTOR_RECONNECT_RETRY_BUDGET_SECONDS"


def executor_reconnect_retry_budget_seconds() -> float:
    """Return the same-executor reconnect wait budget for transient drops."""

    raw = os.environ.get(_RECONNECT_RETRY_BUDGET_ENV)
    if raw is None or not raw.strip():
        return _MIN_RECONNECT_RETRY_BUDGET_SECONDS
    try:
        configured = float(raw)
    except ValueError:
        _logger.warning(
            "executor_ws: invalid reconnect retry budget, using minimum",
            extra={
                "extra_data": {
                    "env_var": _RECONNECT_RETRY_BUDGET_ENV,
                    "value": raw,
                    "minimum_seconds": _MIN_RECONNECT_RETRY_BUDGET_SECONDS,
                }
            },
        )
        return _MIN_RECONNECT_RETRY_BUDGET_SECONDS
    return max(_MIN_RECONNECT_RETRY_BUDGET_SECONDS, configured)


def _resource_snapshot_payload_is_bounded(value: dict[str, Any]) -> bool:
    """Reject oversized/deep notification payloads before scheduling persistence."""

    stack: list[tuple[Any, int]] = [(value, 0)]
    nodes = 0
    while stack:
        current, depth = stack.pop()
        nodes += 1
        if nodes > _RESOURCE_SNAPSHOT_MAX_NODES or depth > _RESOURCE_SNAPSHOT_MAX_DEPTH:
            return False
        if isinstance(current, dict):
            if len(current) > _RESOURCE_SNAPSHOT_MAX_NODES:
                return False
            for key, item in current.items():
                if not isinstance(key, str) or len(key) > _RESOURCE_SNAPSHOT_MAX_STRING_LENGTH:
                    return False
                stack.append((item, depth + 1))
        elif isinstance(current, list):
            if len(current) > _RESOURCE_SNAPSHOT_MAX_NODES:
                return False
            stack.extend((item, depth + 1) for item in current)
        elif isinstance(current, str):
            if len(current) > _RESOURCE_SNAPSHOT_MAX_STRING_LENGTH:
                return False
        elif current is not None and not isinstance(current, (bool, int, float)):
            return False
    return True


def _normalize_local_model_progress(value: dict[str, Any]) -> dict[str, Any] | None:
    """Allowlist and bound one managed-model progress notification."""

    operation_id = value.get("operation_id")
    phase = value.get("phase")
    progress_seq = value.get("progress_seq")
    progress_bytes = value.get("progress_bytes")
    if (
        not isinstance(operation_id, str)
        or not operation_id
        or len(operation_id) > 128
        or not isinstance(phase, str)
        or len(phase) > 120
        or isinstance(progress_seq, bool)
        or not isinstance(progress_seq, int)
        or progress_seq < 0
        or progress_seq > 2**63 - 1
        or isinstance(progress_bytes, bool)
        or not isinstance(progress_bytes, int)
        or progress_bytes < 0
        or progress_bytes > 2**63 - 1
    ):
        return None
    return {
        "operation_id": operation_id,
        "phase": phase,
        "progress_seq": progress_seq,
        "progress_bytes": progress_bytes,
    }


def _normalize_local_model_completion(value: dict[str, Any]) -> dict[str, Any] | None:
    """Allowlist and bound one managed-model terminal notification."""

    operation_id = value.get("operation_id")
    state = value.get("state")
    if (
        not isinstance(operation_id, str)
        or not operation_id
        or len(operation_id) > 128
        or state not in {"succeeded", "failed", "cancelled"}
    ):
        return None
    normalized: dict[str, Any] = {"operation_id": operation_id, "state": state}
    for key in ("message", "error"):
        item = value.get(key)
        if isinstance(item, str) and item:
            normalized[key] = item[:512]
    return normalized


def _transient_executor_output(
    *,
    executor_id: str,
    code: str,
    same_executor_reconnected: bool | None = None,
    auto_retried: bool | None = None,
    auto_retry_skipped_reason: str | None = None,
) -> str:
    if code == "executor_circuit_open":
        base = f"Executor '{executor_id}' circuit breaker is open."
    else:
        base = f"Executor '{executor_id}' disconnected during tool execution."
    details: list[str] = []
    if same_executor_reconnected is True:
        details.append("The same executor reconnected.")
    elif same_executor_reconnected is False:
        details.append("The same executor did not reconnect within the retry budget.")
    if auto_retried is True:
        details.append("The controller retried the call on the same executor.")
    elif auto_retried is False:
        if auto_retry_skipped_reason == "tool_not_idempotent":
            details.append(
                "The tool was not retried automatically because it may have side effects."
            )
        elif auto_retry_skipped_reason == "same_executor_reconnect_timeout":
            details.append("The tool was not retried automatically because reconnect timed out.")
        else:
            details.append("The tool was not retried automatically.")
    if not details:
        details.append("The controller may retry this call on the same executor if it is safe.")
    return f"{base} {' '.join(details)}"


class ExecutorDisconnectedError(ExecutorDeliveryError):
    """Raised when the executor WebSocket is not connected."""


class ExecutorRPCError(RuntimeError):
    """Raised when the executor returns a JSON-RPC error."""

    def __init__(self, code: int, message: str, data: Any = None) -> None:
        super().__init__(message)
        self.code = code
        self.data = data


# ---------------------------------------------------------------------------
# WebSocketExecutorConnection
# ---------------------------------------------------------------------------


class WebSocketExecutorConnection:
    """``ExecutorConnection`` implementation over a real WebSocket.

    Uses JSON-RPC 2.0 with correlation-based request/response.  A background
    receiver task reads all incoming messages and dispatches responses to
    pending ``asyncio.Future`` objects, and notifications to registered
    callbacks.
    """

    def __init__(
        self,
        ws: WebSocket,
        executor_id: str,
        capabilities: ExecutorCapabilities,
        breaker: CircuitBreaker | None = None,
        *,
        connection_owner: ExecutorConnectionOwner | None = None,
        replay_connection_resolver: Callable[
            [str, WebSocketExecutorConnection, float],
            Awaitable[WebSocketExecutorConnection | None],
        ]
        | None = None,
    ) -> None:
        self._ws = ws
        self.executor_id = executor_id
        self.capabilities = capabilities
        self.breaker = breaker or CircuitBreaker(failure_threshold=5, recovery_timeout=30.0)
        self.connection_owner = connection_owner
        self._replay_connection_resolver = replay_connection_resolver

        # Correlation tracking
        self._pending: dict[str, asyncio.Future[dict[str, Any]]] = {}
        self._receiver_task: asyncio.Task[None] | None = None
        self._send_lock = asyncio.Lock()

        # Heartbeat tracking
        self.last_heartbeat: datetime = datetime.now(UTC)
        self._connected = True
        self._close_code: int | None = None
        self._close_reason: str | None = None
        self._close_error_type: str | None = None

        # LLM inference streaming queues (request_id → queue)
        self._inference_queues: dict[str, asyncio.Queue[dict[str, Any]]] = {}
        self._inference_overflowed: set[str] = set()
        self._tool_chunk_callbacks: dict[str, ToolOutputChunkCallback] = {}
        self._tool_progress_tasks: dict[str, set[asyncio.Task[Any]]] = {}
        self._tool_progress_overflowed: set[str] = set()

        # Channel adapter notification callbacks (account_id → callback)
        self._channel_message_callbacks: dict[str, Any] = {}
        self._channel_status_callbacks: dict[str, Any] = {}
        self._background_shell_completed_callback: Any | None = None
        self._oauth_loopback_callback: Any | None = None
        self._resource_snapshot_callback: Any | None = None
        self._resource_snapshot_task: asyncio.Task[None] | None = None
        self._pending_resource_snapshot: dict[str, Any] | None = None
        self._resource_snapshot_last_dispatched_at: float | None = None
        self._local_model_progress_callback: Any | None = None
        self._local_model_completed_callback: Any | None = None
        self._heartbeat_callback: Any | None = None
        self._ownership_check_callback: Any | None = None
        self._owned_callback_dispatcher: Any | None = None
        self._owned_effect_dispatcher: Any | None = None
        self._ownership_deadline: float | None = None
        self._ownership_watchdog_task: asyncio.Task[None] | None = None
        self._ownership_closing = False
        self._ownership_closing_task: asyncio.Task[Any] | None = None
        self._notification_tasks: set[asyncio.Task[Any]] = set()
        self._local_model_notification_task: asyncio.Task[None] | None = None
        self._pending_local_model_progress: dict[str, dict[str, Any]] = {}

    @property
    def connected(self) -> bool:
        return self._connected

    @property
    def epoch(self) -> int | None:
        return self.connection_owner.epoch if self.connection_owner is not None else None

    @property
    def close_metadata(self) -> dict[str, Any]:
        """Return bounded transport-close diagnostics for controller logs."""

        return {
            "close_code": self._close_code,
            "close_reason": self._close_reason,
            "error_type": self._close_error_type,
        }

    def start_receiver(self) -> None:
        """Start the background message receiver task."""
        if self._receiver_task is None or self._receiver_task.done():
            self._receiver_task = asyncio.create_task(
                self._receive_loop(), name=f"executor-rx-{self.executor_id}"
            )
            self._start_ownership_watchdog()

    async def wait_until_closed(self) -> None:
        """Block until the receiver loop finishes (connection closed)."""
        if self._receiver_task is not None:
            with contextlib.suppress(asyncio.CancelledError):
                await self._receiver_task

    async def close(self) -> None:
        """Close the connection and cancel the receiver."""
        self._connected = False
        if self._receiver_task is not None and not self._receiver_task.done():
            self._receiver_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._receiver_task
        if (
            self._ownership_watchdog_task is not None
            and self._ownership_watchdog_task is not asyncio.current_task()
            and not self._ownership_watchdog_task.done()
        ):
            self._ownership_watchdog_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._ownership_watchdog_task
        if self._resource_snapshot_task is not None and not self._resource_snapshot_task.done():
            self._resource_snapshot_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._resource_snapshot_task
        await self._terminate_pending("Executor connection closed")

    # ------------------------------------------------------------------
    # ExecutorConnection protocol
    # ------------------------------------------------------------------

    async def rpc_call(
        self,
        method: str,
        params: dict[str, Any],
        timeout: float | None = None,
        *,
        on_sent: Callable[[], Awaitable[None]] | None = None,
        stable_call_id: str | None = None,
        replay_safe: bool = False,
        _retry_attempted: bool = False,
    ) -> dict[str, Any]:
        """Send a JSON-RPC request and await the correlated response."""
        if not self._connected:
            raise ExecutorDisconnectedError(
                f"Executor {self.executor_id} is not connected",
                DeliveryState.NOT_SENT,
                code="executor_disconnected",
                executor_id=self.executor_id,
                retry_after=1.0,
            )
        await self._ensure_current_ownership()

        request_id = uuid.uuid4().hex
        request_params = dict(params)
        if replay_safe:
            from cognis.executor.unary_dedup import (
                UnaryDedupCache,
                is_replay_safe_unary_method,
            )

            if not is_replay_safe_unary_method(method):
                raise ValueError("Replay-safe unary RPC requires an allowlisted method")
            stable_call_id = stable_call_id or uuid.uuid4().hex
            request_params.update(
                {
                    "_cognis_replay_safe_unary": True,
                    "_cognis_unary_call_id": stable_call_id,
                    "_cognis_unary_payload_digest": UnaryDedupCache.digest(
                        {"method": method, "params": params}
                    ),
                }
            )
        request = {
            "jsonrpc": "2.0",
            "method": method,
            "params": request_params,
            "id": request_id,
        }

        future: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future

        effective_timeout = timeout or _RPC_TIMEOUT_SECONDS
        start = perf_counter()
        submitted = False
        send_attempted = False
        try:

            async def _do_call() -> dict[str, Any]:
                nonlocal send_attempted
                async with self._send_lock:
                    send_attempted = True
                    await self._ws.send_json(request)
                nonlocal submitted
                submitted = True
                if on_sent is not None:
                    await on_sent()
                return await asyncio.wait_for(future, timeout=effective_timeout)

            result = await self.breaker.call(_do_call)
            EXECUTOR_WS_RPC_DURATION.labels(method=method).observe(perf_counter() - start)
            return result
        except TimeoutError as exc:
            EXECUTOR_WS_RPC_ERRORS.labels(method=method, error_type="timeout").inc()
            self._pending.pop(request_id, None)
            if replay_safe and stable_call_id and not _retry_attempted:
                return await self._retry_replay_safe_rpc(
                    method=method,
                    params=params,
                    timeout=timeout,
                    on_sent=on_sent,
                    stable_call_id=stable_call_id,
                )
            if send_attempted and method in {"tool.execute", "llm.complete"}:
                if submitted:
                    await self._cancel_physical_call(method, params)
                raise ExecutorDeliveryError(
                    f"Executor call {method} timed out after send began",
                    DeliveryState.ACCEPTED_UNKNOWN,
                    executor_id=self.executor_id,
                    retry_after=1.0,
                ) from exc
            raise
        except CircuitBreakerError:
            EXECUTOR_WS_RPC_ERRORS.labels(method=method, error_type="circuit_open").inc()
            self._pending.pop(request_id, None)
            raise
        except ExecutorDisconnectedError as exc:
            EXECUTOR_WS_RPC_ERRORS.labels(method=method, error_type="disconnected").inc()
            self._pending.pop(request_id, None)
            if replay_safe and stable_call_id and not _retry_attempted:
                return await self._retry_replay_safe_rpc(
                    method=method,
                    params=params,
                    timeout=timeout,
                    on_sent=on_sent,
                    stable_call_id=stable_call_id,
                )
            if send_attempted:
                raise ExecutorDisconnectedError(
                    str(exc),
                    DeliveryState.ACCEPTED_UNKNOWN,
                    code="executor_disconnected",
                    executor_id=self.executor_id,
                    retry_after=1.0,
                ) from exc
            raise
        except asyncio.CancelledError:
            self._pending.pop(request_id, None)
            if submitted and method in {"tool.execute", "llm.complete"}:
                await self._cancel_physical_call(method, params)
            raise
        except ExecutorRPCError:
            raise
        except Exception as exc:
            EXECUTOR_WS_RPC_ERRORS.labels(method=method, error_type="unknown").inc()
            self._pending.pop(request_id, None)
            if send_attempted:
                raise ExecutorDeliveryError(
                    f"Executor call {method} failed after send began",
                    DeliveryState.ACCEPTED_UNKNOWN,
                    executor_id=self.executor_id,
                    retry_after=1.0,
                ) from exc
            raise

    async def _retry_replay_safe_rpc(
        self,
        *,
        method: str,
        params: dict[str, Any],
        timeout: float | None,
        on_sent: Callable[[], Awaitable[None]] | None,
        stable_call_id: str,
    ) -> dict[str, Any]:
        """Retry one allowlisted unary call on this or its replacement connection."""

        connection: WebSocketExecutorConnection | None = self if self.connected else None
        if connection is None and self._replay_connection_resolver is not None:
            connection = await self._replay_connection_resolver(
                self.executor_id,
                self,
                min(timeout or _RPC_TIMEOUT_SECONDS, 5.0),
            )
        if connection is None:
            raise ExecutorDeliveryError(
                f"Executor {self.executor_id} did not reconnect for replay-safe retry",
                DeliveryState.TERMINAL,
                code="executor_recovery_timeout",
                executor_id=self.executor_id,
            )
        return await connection.rpc_call(
            method,
            params,
            timeout,
            on_sent=on_sent,
            stable_call_id=stable_call_id,
            replay_safe=True,
            _retry_attempted=True,
        )

    async def _cancel_physical_call(self, method: str, params: dict[str, Any]) -> None:
        """Best-effort cancellation for an already submitted physical call."""
        cancel_method: str | None = None
        cancel_params: dict[str, Any] | None = None
        if method == "tool.execute" and isinstance(params.get("call_id"), str):
            cancel_method = "tool.cancel"
            cancel_params = {"call_id": params["call_id"]}
        elif method == "llm.complete" and isinstance(params.get("request_id"), str):
            cancel_method = "llm.cancel"
            cancel_params = {"request_id": params["request_id"]}
        if cancel_method is None or cancel_params is None or not self._connected:
            return
        with contextlib.suppress(Exception):
            async with asyncio.timeout(_PHYSICAL_CANCEL_SEND_TIMEOUT_SECONDS):
                async with self._send_lock:
                    await self._ws.send_json(
                        {
                            "jsonrpc": "2.0",
                            "method": cancel_method,
                            "params": cancel_params,
                            "id": uuid.uuid4().hex,
                        }
                    )

    async def isolated_rpc_call(
        self,
        method: str,
        params: dict[str, Any],
        *,
        timeout: float,
    ) -> dict[str, Any]:
        """Execute a bounded control RPC without affecting the shared circuit breaker."""
        if not self._connected:
            raise ExecutorDisconnectedError(
                f"Executor {self.executor_id} is not connected",
                DeliveryState.NOT_SENT,
                code="executor_disconnected",
                executor_id=self.executor_id,
            )
        await self._ensure_current_ownership()
        request_id = uuid.uuid4().hex
        future: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future
        try:
            async with self._send_lock:
                await self._ws.send_json(
                    {
                        "jsonrpc": "2.0",
                        "method": method,
                        "params": params,
                        "id": request_id,
                    }
                )
            return await asyncio.wait_for(future, timeout=timeout)
        finally:
            self._pending.pop(request_id, None)

    async def list_tools(self) -> list[dict[str, Any]]:
        """Return tool metadata from the remote executor."""
        result = await self.rpc_call(
            "tool.list",
            {},
            replay_safe=True,
        )
        tools = result.get("tools")
        if not isinstance(tools, list):
            return []
        return [cast(dict[str, Any], tool) for tool in tools if isinstance(tool, dict)]

    async def tool_execute(
        self,
        tool_call: ToolCall,
        timeout_seconds: int | None = None,
        output_chunk_callback: ToolOutputChunkCallback | None = None,
        *,
        on_sent: Callable[[], Awaitable[None]] | None = None,
    ) -> ToolResult:
        """Execute a tool call on the remote executor."""
        if output_chunk_callback is not None:
            self._tool_chunk_callbacks[tool_call.call_id] = output_chunk_callback
        try:
            result = await self.rpc_call(
                "tool.execute",
                {
                    "call_id": tool_call.call_id,
                    "tool_name": tool_call.name,
                    "arguments": tool_call.arguments,
                    "runtime_metadata": tool_call.runtime_metadata,
                    "execution_scope_id": tool_call.execution_scope_id,
                    "timeout_seconds": timeout_seconds or 300,
                },
                timeout=float(timeout_seconds) if timeout_seconds else None,
                on_sent=on_sent,
            )
            return ToolResult(
                output=str(result.get("output", "")),
                is_error=bool(result.get("is_error", False)),
                duration_ms=result.get("duration_ms"),
                metadata=result.get("metadata")
                if isinstance(result.get("metadata"), dict)
                else None,
                attachments=result.get("attachments"),
            )
        except ExecutorDeliveryError as exc:
            metadata = exc.metadata()
            metadata["code"] = metadata["error_code"]
            metadata["executor_id"] = metadata.get("executor_id") or self.executor_id
            metadata["owner_id"] = metadata.get("owner_id") or (
                self.connection_owner.owner_id if self.connection_owner is not None else None
            )
            metadata["epoch"] = metadata.get("epoch") or self.epoch
            metadata["retryable"] = exc.delivery_state != DeliveryState.TERMINAL
            return ToolResult(
                output="Executor delivery failed; recovery is restricted to the same executor.",
                is_error=True,
                metadata=metadata,
            )
        except TimeoutError:
            return ToolResult(
                output="Tool execution timed out.",
                is_error=True,
                metadata={"code": "tool_execution_timeout", "retryable": False},
            )
        except asyncio.CancelledError:
            raise
        except CircuitBreakerError:
            code = "executor_circuit_open"
            return ToolResult(
                output=_transient_executor_output(executor_id=self.executor_id, code=code),
                is_error=True,
                metadata={
                    "code": code,
                    "delivery_state": DeliveryState.NOT_SENT.value,
                    "executor_id": self.executor_id,
                    "retryable": True,
                    "same_executor_only": True,
                },
            )
        except ExecutorRPCError as exc:
            return ToolResult(output=f"Executor RPC error: {exc}", is_error=True)
        except Exception as exc:
            error_detail = str(exc)[:500]
            return ToolResult(output=f"Tool execution failed: {error_detail}", is_error=True)
        finally:
            self._tool_chunk_callbacks.pop(tool_call.call_id, None)

    async def cancel_call(self, call_id: str) -> None:
        """Cancel a running tool execution on the remote executor."""
        try:
            await self.rpc_call("tool.cancel", {"call_id": call_id}, timeout=10.0)
        except Exception:
            _logger.debug(
                "executor_ws: failed to cancel call",
                extra={"extra_data": {"executor_id": self.executor_id, "call_id": call_id}},
            )

    async def cancel_inference(self, request_id: str) -> None:
        try:
            await self.rpc_call(
                "llm.cancel",
                {"request_id": request_id},
                timeout=10.0,
            )
        except Exception:
            _logger.debug(
                "executor_ws: failed to cancel inference",
                extra={
                    "extra_data": {
                        "executor_id": self.executor_id,
                        "request_id": request_id,
                    }
                },
            )

    # ------------------------------------------------------------------
    # LLM inference streaming (via rpc_call, not a protocol method)
    # ------------------------------------------------------------------

    async def llm_complete_stream(
        self,
        request_id: str,
        messages: list[dict[str, Any]],
        model: str,
        *,
        on_sent: Callable[[], Awaitable[None]] | None = None,
        **kwargs: Any,
    ) -> AsyncIterator[dict[str, Any]]:
        """Route an LLM completion request to the executor and stream chunks.

        This is NOT on the ``ExecutorConnection`` protocol — it uses
        ``rpc_call("llm.complete", ...)`` and an ``asyncio.Queue`` bridge
        fed by ``llm.chunk`` / ``llm.done`` notifications from the receiver.
        """
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(
            maxsize=_INFERENCE_EVENT_QUEUE_MAX_PENDING
        )
        self._inference_queues[request_id] = queue

        try:
            # Send the request — the executor will respond with the initial
            # ack and then stream llm.chunk notifications.
            params: dict[str, Any] = {
                "request_id": request_id,
                "messages": messages,
                "model": model,
                "stream": True,
                **kwargs,
            }
            await self.rpc_call("llm.complete", params, timeout=10.0, on_sent=on_sent)

            # Yield chunks from the queue until llm.done arrives
            chunk_timeout = _inference_chunk_timeout_seconds()
            while True:
                chunk = await asyncio.wait_for(queue.get(), timeout=chunk_timeout)
                if chunk.get("error"):
                    error_chunk = {
                        "error": chunk["error"],
                        "mid_stream_failure": True,
                    }
                    response_error = chunk.get("response_error")
                    if isinstance(response_error, dict):
                        error_chunk["response_error"] = response_error
                    yield error_chunk
                    break
                if chunk.get("done"):
                    yield chunk
                    break
                yield chunk
        except ExecutorDeliveryError as exc:
            yield {
                "error": str(exc),
                "delivery_state": exc.delivery_state,
                "mid_stream_failure": True,
            }
        except TimeoutError:
            yield {
                "error": "LLM inference timed out",
                "mid_stream_failure": True,
            }
        except ExecutorDisconnectedError:
            yield {
                "error": "Executor disconnected during inference",
                "mid_stream_failure": True,
            }
        finally:
            self._inference_queues.pop(request_id, None)
            self._inference_overflowed.discard(request_id)

    def _enqueue_inference_event(
        self, request_id: str, payload: dict[str, Any], *, done: bool = False
    ) -> None:
        queue = self._inference_queues.get(request_id)
        if queue is None:
            return
        if done:
            payload["done"] = True
        try:
            queue.put_nowait(payload)
        except asyncio.QueueFull:
            if request_id in self._inference_overflowed:
                return
            self._inference_overflowed.add(request_id)
            with contextlib.suppress(asyncio.QueueEmpty):
                queue.get_nowait()
            queue.put_nowait(
                {
                    "error": "Executor inference event queue overflow",
                    "delivery_state": "accepted_unknown",
                    "code": "inference_event_queue_overflow",
                    "mid_stream_failure": True,
                    "done": True,
                }
            )
            self._track_notification_task(
                self.cancel_inference(request_id),
                name=f"executor-inference-overflow-cancel-{request_id}",
            )

    async def llm_transcribe(
        self,
        *,
        request_id: str,
        audio_base64: str,
        audio_encoding: str,
        mime_type: str,
        filename: str,
        model: str,
        provider_preset: str | None = None,
        request_kwargs: dict[str, Any] | None = None,
        prompt: str | None = None,
        language: str | None = None,
    ) -> dict[str, Any]:
        return await self.rpc_call(
            "llm.transcribe",
            {
                "request_id": request_id,
                "audio_base64": audio_base64,
                "audio_encoding": audio_encoding,
                "mime_type": mime_type,
                "filename": filename,
                "model": model,
                "provider_preset": provider_preset,
                "prompt": prompt,
                "language": language,
                "request_kwargs": request_kwargs or {},
            },
            timeout=300.0,
        )

    async def llm_discover_models(
        self,
        *,
        preset: str,
        base_url: str,
        api_key: str = "",
        provider_id: str | None = None,
        owner_email: str | None = None,
    ) -> list[dict[str, Any]]:
        """Discover provider models from the executor host perspective."""

        result = await self.rpc_call(
            "llm.discover_models",
            {
                "preset": preset,
                "base_url": base_url,
                "api_key": api_key,
                "provider_id": provider_id,
                "owner_email": owner_email,
            },
            timeout=30.0,
        )
        models = result.get("models", [])
        return (
            [dict(item) for item in models if isinstance(item, dict)]
            if isinstance(models, list)
            else []
        )

    async def local_model_status(self) -> OllamaRuntimeStatus:
        """Inspect the pinned executor-local Ollama runtime."""

        result = await self.rpc_call(
            "local_model.status",
            {},
            timeout=30.0,
            replay_safe=True,
        )
        return OllamaRuntimeStatus.model_validate(result)

    async def local_model_show(self, runtime_name: str) -> dict[str, Any]:
        """Fetch read-only metadata for one canonical model."""

        return await self.rpc_call(
            "local_model.show",
            {"runtime_name": runtime_name},
            timeout=30.0,
            replay_safe=True,
        )

    async def local_model_operation_start(
        self,
        request: OllamaRuntimeStartRequest,
    ) -> OllamaRuntimeOperationStatus:
        """Start or resume one idempotent managed Ollama operation."""

        result = await self.rpc_call(
            "local_model.operation.start",
            request.model_dump(mode="json"),
            timeout=30.0,
        )
        return OllamaRuntimeOperationStatus.model_validate(result)

    async def local_model_operation_status(
        self,
        operation_id: str,
    ) -> OllamaRuntimeOperationStatus:
        result = await self.rpc_call(
            "local_model.operation.status",
            {"operation_id": operation_id},
            timeout=15.0,
        )
        return OllamaRuntimeOperationStatus.model_validate(result)

    async def local_model_operation_cancel(self, operation_id: str) -> dict[str, Any]:
        return await self.rpc_call(
            "local_model.operation.cancel",
            {"operation_id": operation_id},
            timeout=30.0,
        )

    async def lsp_status(self) -> dict[str, Any]:
        """Fetch normalized LSP status from a remote executor."""
        return await self.rpc_call(
            "lsp.status",
            {},
            timeout=_LSP_STATUS_TIMEOUT_SECONDS,
            replay_safe=True,
        )

    async def background_shell_status(self, *, include_completed: bool = False) -> dict[str, Any]:
        """Fetch background shell status from a remote executor."""
        return await self.rpc_call(
            "shell.background_status",
            {"include_completed": include_completed},
            timeout=10.0,
        )

    async def oauth_loopback_start(
        self,
        *,
        state: str,
        ttl_seconds: int,
        callback_path: str = "/oauth/callback",
    ) -> dict[str, Any]:
        """Start a temporary executor-local OAuth loopback callback listener."""

        return await self.rpc_call(
            "oauth.loopback_start",
            {
                "state": state,
                "ttl_seconds": ttl_seconds,
                "callback_path": callback_path,
            },
            timeout=10.0,
        )

    async def oauth_loopback_stop(self, *, listener_id: str) -> dict[str, Any]:
        """Stop a temporary executor-local OAuth loopback callback listener."""

        return await self.rpc_call(
            "oauth.loopback_stop",
            {"listener_id": listener_id},
            timeout=10.0,
        )

    # ------------------------------------------------------------------
    # Background receiver
    # ------------------------------------------------------------------

    async def _receive_loop(self) -> None:
        """Read messages from the WebSocket and dispatch them."""
        try:
            while self._connected:
                try:
                    data = await self._ws.receive_json()
                except WebSocketDisconnect as exc:
                    self._close_code = exc.code
                    self._close_reason = str(exc.reason)[:200] if exc.reason else None
                    self._close_error_type = type(exc).__name__
                    break

                if data is None:
                    break

                # Every physical-socket frame is fenced at receipt time.  A
                # takeover may happen after a request was sent but before its
                # response arrives, so outbound validation alone is not enough.
                await self._ensure_current_ownership()

                # JSON-RPC response (has "id" and "result" or "error")
                msg_id = data.get("id")
                if msg_id and msg_id in self._pending:

                    def resolve_response(
                        response_id: str = msg_id,
                        response: dict[str, Any] = data,
                    ) -> None:
                        future = self._pending.pop(response_id)
                        if not future.done():
                            if "error" in response:
                                err = response["error"]
                                future.set_exception(
                                    ExecutorRPCError(
                                        err.get("code", -1),
                                        err.get("message", "Unknown error"),
                                        err.get("data"),
                                    )
                                )
                            else:
                                future.set_result(response.get("result", {}))

                    if not await self._apply_owned_effect(resolve_response):
                        await self._close_for_ownership_loss()
                        break
                    continue

                # JSON-RPC notification (has "method" but no "id", or id is
                # not in pending — treat as notification)
                method = data.get("method")
                if method == "executor.heartbeat":
                    self.last_heartbeat = datetime.now(UTC)
                    if self._heartbeat_callback is not None:
                        progress_task = self._track_notification_task(
                            self._renew_ownership_from_heartbeat(self.last_heartbeat),
                            name=f"executor-heartbeat-renewal-{self.executor_id}",
                        )
                    params = data.get("params", {})
                    snapshot = params.get("resource_snapshot") if isinstance(params, dict) else None
                    if (
                        isinstance(snapshot, dict)
                        and self._resource_snapshot_callback is not None
                        and _resource_snapshot_payload_is_bounded(snapshot)
                    ):
                        normalized_snapshot = normalize_executor_resource_snapshot(snapshot)
                        if normalized_snapshot is not None:
                            self._queue_resource_snapshot(
                                normalized_snapshot.model_dump(
                                    mode="json",
                                    exclude={"freshness"},
                                )
                            )
                elif method == "tool.progress":
                    params = data.get("params", {})
                    call_id = str(params.get("call_id") or "")
                    chunk = params.get("delta")
                    if chunk is None:
                        chunk = params.get("content")
                    if chunk is None:
                        chunk = params.get("text")
                    callback = self._tool_chunk_callbacks.get(call_id)
                    if callback is not None and chunk is not None:
                        tasks = self._tool_progress_tasks.setdefault(call_id, set())
                        if len(tasks) >= _TOOL_PROGRESS_MAX_PENDING_PER_CALL:
                            if call_id not in self._tool_progress_overflowed:
                                self._tool_progress_overflowed.add(call_id)
                                self._track_notification_task(
                                    self.cancel_call(call_id),
                                    name=f"executor-tool-progress-overflow-{call_id}",
                                )
                            continue
                        stream = params.get("stream")
                        progress_task = self._track_notification_task(
                            self._dispatch_callback(
                                callback,
                                str(chunk),
                                str(stream) if stream is not None else None,
                                include_owner=False,
                                fence_callback=True,
                            ),
                            name=f"executor-tool-progress-{self.executor_id}-{call_id}",
                        )
                        tasks.add(progress_task)
                        progress_task.add_done_callback(tasks.discard)
                    _logger.debug(
                        "executor_ws: tool progress",
                        extra={
                            "extra_data": {
                                "executor_id": self.executor_id,
                                "call_id": call_id,
                            }
                        },
                    )
                elif method == "llm.chunk":
                    params = data.get("params", {})
                    req_id = params.get("request_id")
                    if (
                        req_id
                        and req_id in self._inference_queues
                        and not await self._apply_owned_effect(
                            lambda req_id=req_id, payload=params: self._enqueue_inference_event(
                                req_id, payload
                            )
                        )
                    ):
                        await self._close_for_ownership_loss()
                        break
                elif method == "llm.done":
                    params = data.get("params", {})
                    req_id = params.get("request_id")
                    if req_id and req_id in self._inference_queues:
                        params["done"] = True
                        if not await self._apply_owned_effect(
                            lambda req_id=req_id, payload=params: self._enqueue_inference_event(
                                req_id, payload, done=True
                            )
                        ):
                            await self._close_for_ownership_loss()
                            break
                elif method == "shell.background_completed":
                    params = data.get("params", {})
                    if self._background_shell_completed_callback is not None:
                        self._track_notification_task(
                            self._dispatch_callback(
                                self._background_shell_completed_callback,
                                self.executor_id,
                                params if isinstance(params, dict) else {},
                                include_owner=True,
                                fence_callback=True,
                            ),
                            name=f"executor-shell-completed-{self.executor_id}",
                        )
                elif method == "channel.message":
                    params = data.get("params", {})
                    acct_id = params.get("account_id")
                    cb = self._channel_message_callbacks.get(acct_id) if acct_id else None
                    if cb is not None:
                        self._track_notification_task(
                            self._dispatch_callback(
                                cb,
                                params.get("message", {}),
                                include_owner=True,
                                fence_callback=False,
                            ),
                            name=f"executor-channel-message-{self.executor_id}",
                        )
                elif method == "channel.status":
                    params = data.get("params", {})
                    acct_id = params.get("account_id")
                    cb = self._channel_status_callbacks.get(acct_id) if acct_id else None
                    if cb is not None:
                        self._track_notification_task(
                            self._dispatch_callback(
                                cb,
                                params.get("status", {}),
                                include_owner=True,
                                fence_callback=True,
                            ),
                            name=f"executor-channel-status-{self.executor_id}",
                        )
                elif method == "oauth.loopback_callback":
                    params = data.get("params", {})
                    if self._oauth_loopback_callback is not None:
                        self._track_notification_task(
                            self._dispatch_callback(
                                self._oauth_loopback_callback,
                                self.executor_id,
                                params if isinstance(params, dict) else {},
                                include_owner=True,
                                fence_callback=False,
                            ),
                            name=f"executor-oauth-callback-{self.executor_id}",
                        )
                elif method == "local_model.progress":
                    params = data.get("params", {})
                    if isinstance(params, dict):
                        progress = _normalize_local_model_progress(params)
                        if progress is not None:
                            self._queue_local_model_progress(progress)
                elif method == "local_model.completed":
                    params = data.get("params", {})
                    if (
                        isinstance(params, dict)
                        and self._local_model_completed_callback is not None
                    ):
                        completion = _normalize_local_model_completion(params)
                        if completion is None:
                            continue
                        operation_id = str(completion["operation_id"])
                        if operation_id:
                            self._pending_local_model_progress.pop(operation_id, None)
                        self._track_notification_task(
                            self._dispatch_callback(
                                self._local_model_completed_callback,
                                self.executor_id,
                                completion,
                                include_owner=True,
                                fence_callback=False,
                            ),
                            name=(
                                f"executor-local-model-completed-{self.executor_id}-{operation_id}"
                            ),
                        )
                else:
                    _logger.debug(
                        "executor_ws: unknown notification",
                        extra={
                            "extra_data": {
                                "executor_id": self.executor_id,
                                "method": method,
                            }
                        },
                    )
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            self._close_error_type = type(exc).__name__
            _logger.warning(
                "executor_ws: receiver loop error",
                extra={
                    "extra_data": {
                        "executor_id": self.executor_id,
                        **self.close_metadata,
                    }
                },
                exc_info=True,
            )
        finally:
            self._connected = False
            await self._terminate_pending("Executor disconnected")

    # ------------------------------------------------------------------
    # Channel adapter callback registration
    # ------------------------------------------------------------------

    def register_channel_callback(
        self,
        account_id: str,
        on_message: Any,
        on_status: Any | None = None,
    ) -> None:
        """Register callbacks for channel.message and channel.status notifications."""
        self._channel_message_callbacks[account_id] = on_message
        if on_status is not None:
            self._channel_status_callbacks[account_id] = on_status

    def unregister_channel_callback(self, account_id: str) -> None:
        """Remove channel notification callbacks for an account."""
        self._channel_message_callbacks.pop(account_id, None)
        self._channel_status_callbacks.pop(account_id, None)

    def register_background_shell_completed_callback(self, callback: Any | None) -> None:
        """Register a callback for background shell completion notifications."""

        self._background_shell_completed_callback = callback

    def register_oauth_loopback_callback(self, callback: Any | None) -> None:
        """Register a callback for executor-local OAuth callback notifications."""

        self._oauth_loopback_callback = callback

    def register_resource_snapshot_callback(self, callback: Any | None) -> None:
        """Register a callback for cadence-limited current resource snapshots."""

        self._resource_snapshot_callback = callback

    def register_heartbeat_callback(self, callback: Any | None) -> None:
        """Register the DB ownership renewal callback for executor heartbeats."""

        self._heartbeat_callback = callback
        if callback is not None and self._receiver_task is not None:
            self._start_ownership_watchdog()

    def register_ownership_check_callback(self, callback: Any | None) -> None:
        """Register the authoritative pre-dispatch ownership check."""

        self._ownership_check_callback = callback

    def register_owned_callback_dispatcher(self, callback: Any | None) -> None:
        """Register durable callback dispatch serialized by owner/epoch."""

        self._owned_callback_dispatcher = callback

    def register_owned_effect_dispatcher(self, callback: Any | None) -> None:
        """Register atomic in-memory dispatch under the durable owner fence."""

        self._owned_effect_dispatcher = callback

    def _queue_resource_snapshot(self, snapshot: dict[str, Any]) -> None:
        """Keep only the newest pending snapshot and one bounded dispatch task."""

        self._pending_resource_snapshot = snapshot
        if self._resource_snapshot_task is None or self._resource_snapshot_task.done():
            self._resource_snapshot_task = asyncio.create_task(
                self._drain_resource_snapshots(),
                name=f"executor-resource-snapshot-{self.executor_id}",
            )

    async def _drain_resource_snapshots(self) -> None:
        while self._connected and self._pending_resource_snapshot is not None:
            loop = asyncio.get_running_loop()
            if self._resource_snapshot_last_dispatched_at is not None:
                remaining = _RESOURCE_SNAPSHOT_DISPATCH_INTERVAL_SECONDS - (
                    loop.time() - self._resource_snapshot_last_dispatched_at
                )
                if remaining > 0:
                    await asyncio.sleep(remaining)
            snapshot = self._pending_resource_snapshot
            self._pending_resource_snapshot = None
            callback = self._resource_snapshot_callback
            if callback is None:
                return
            try:
                await self._dispatch_callback(
                    callback,
                    self.executor_id,
                    snapshot,
                    include_owner=True,
                    fence_callback=False,
                )
            except Exception:
                _logger.debug(
                    "executor_ws: resource snapshot callback failed",
                    extra={"extra_data": {"executor_id": self.executor_id}},
                    exc_info=True,
                )
            finally:
                self._resource_snapshot_last_dispatched_at = loop.time()

    def register_local_model_callbacks(
        self,
        *,
        on_progress: Any | None,
        on_completed: Any | None,
    ) -> None:
        """Register controller callbacks for managed Ollama notifications."""

        self._local_model_progress_callback = on_progress
        self._local_model_completed_callback = on_completed

    def _queue_local_model_progress(self, payload: dict[str, Any]) -> None:
        operation_id = str(payload.get("operation_id") or "")
        if not operation_id:
            return
        pending = self._pending_local_model_progress
        if operation_id not in pending and len(pending) >= _LOCAL_MODEL_NOTIFICATION_MAX_PENDING:
            pending.pop(next(iter(pending)))
        pending[operation_id] = payload
        if (
            self._local_model_notification_task is None
            or self._local_model_notification_task.done()
        ):
            self._local_model_notification_task = asyncio.create_task(
                self._drain_local_model_notifications(),
                name=f"executor-local-model-notifications-{self.executor_id}",
            )

    async def _drain_local_model_notifications(self) -> None:
        while self._connected and self._pending_local_model_progress:
            if self._pending_local_model_progress:
                operation_id = next(iter(self._pending_local_model_progress))
                payload = self._pending_local_model_progress.pop(operation_id)
            else:
                return
            callback = self._local_model_progress_callback
            if callback is None:
                continue
            try:
                await self._dispatch_callback(
                    callback,
                    self.executor_id,
                    payload,
                    include_owner=True,
                    fence_callback=False,
                )
            except Exception:
                _logger.debug(
                    "executor_ws: local model callback failed",
                    extra={
                        "extra_data": {
                            "executor_id": self.executor_id,
                            "operation_id": operation_id,
                            "kind": "progress",
                        }
                    },
                    exc_info=True,
                )

    def _fail_pending(self, reason: str) -> None:
        """Fail all pending RPC futures with a disconnection error."""
        for future in self._pending.values():
            if not future.done():
                future.set_exception(ExecutorDisconnectedError(reason))
        self._pending.clear()

    async def _terminate_pending(self, reason: str) -> None:
        """Promptly terminate all RPC, stream, and queued notification work."""

        self._pending_resource_snapshot = None
        current_task = asyncio.current_task()
        for task in (self._resource_snapshot_task, self._local_model_notification_task):
            if task is not None and task is not current_task and not task.done():
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
        watchdog = self._ownership_watchdog_task
        if (
            not self._ownership_closing
            and watchdog is not None
            and watchdog is not current_task
            and not watchdog.done()
        ):
            watchdog.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await watchdog
        self._pending_local_model_progress.clear()
        notification_tasks = [
            task
            for task in self._notification_tasks
            if task is not current_task
            and task is not self._ownership_closing_task
            and not task.done()
        ]
        for task in notification_tasks:
            task.cancel()
        if notification_tasks:
            await asyncio.gather(*notification_tasks, return_exceptions=True)
        self._notification_tasks.clear()
        self._tool_progress_tasks.clear()
        self._tool_progress_overflowed.clear()
        self._fail_pending(reason)
        for queue in self._inference_queues.values():
            while True:
                with contextlib.suppress(asyncio.QueueEmpty):
                    queue.get_nowait()
                    continue
                break
            queue.put_nowait({"error": reason, "done": True})
        self._inference_queues.clear()
        self._inference_overflowed.clear()

    async def _ensure_current_ownership(self) -> None:
        if self._ownership_check_callback is None:
            return
        if await self._ownership_check_callback() is True:
            return
        await self._close_for_ownership_loss()
        raise ExecutorDisconnectedError(
            "Executor connection ownership lost",
            DeliveryState.NOT_SENT,
            code="executor_disconnected",
            executor_id=self.executor_id,
        )

    def _start_ownership_watchdog(self) -> None:
        if self._heartbeat_callback is None or self._ownership_watchdog_task is not None:
            return
        self._ownership_deadline = (
            asyncio.get_running_loop().time() + EXECUTOR_CONNECTION_LEASE_TTL_SECONDS
        )
        self._ownership_watchdog_task = asyncio.create_task(
            self._ownership_watchdog_loop(),
            name=f"executor-ownership-watchdog-{self.executor_id}",
        )

    async def _ownership_watchdog_loop(self) -> None:
        while self._connected:
            deadline = self._ownership_deadline
            if deadline is None:
                return
            delay = deadline - asyncio.get_running_loop().time()
            if delay > 0:
                await asyncio.sleep(delay)
                continue
            await self._close_for_ownership_loss()
            return

    async def _close_for_ownership_loss(self) -> None:
        self._ownership_closing = True
        self._ownership_closing_task = asyncio.current_task()
        self._close_code = 4409
        self._close_reason = "Executor connection ownership lost"
        self._close_error_type = "ExecutorOwnershipLost"
        self._connected = False
        with contextlib.suppress(Exception):
            await self._ws.close(code=4409, reason=self._close_reason)
        receiver = self._receiver_task
        if receiver is not None and receiver is not asyncio.current_task() and not receiver.done():
            receiver.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await receiver
        watchdog = self._ownership_watchdog_task
        if watchdog is not None and watchdog is not asyncio.current_task() and not watchdog.done():
            watchdog.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await watchdog
        await self._terminate_pending(self._close_reason)

    def _track_notification_task(self, awaitable: Any, *, name: str) -> asyncio.Task[Any]:
        task = asyncio.create_task(awaitable, name=name)
        self._notification_tasks.add(task)
        task.add_done_callback(self._notification_tasks.discard)
        return task

    async def wait_tool_progress(self, call_id: str) -> bool:
        tasks = tuple(self._tool_progress_tasks.pop(call_id, set()))
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        overflowed = call_id in self._tool_progress_overflowed
        self._tool_progress_overflowed.discard(call_id)
        return not overflowed

    async def _dispatch_callback(
        self,
        callback: Any,
        *args: Any,
        include_owner: bool,
        fence_callback: bool,
    ) -> None:
        try:
            if self.connection_owner is not None:
                callback_args = (self.connection_owner, *args) if include_owner else args
                if self._owned_callback_dispatcher is None or not fence_callback:
                    result = callback(*callback_args)
                else:
                    result = self._owned_callback_dispatcher(
                        self.connection_owner,
                        callback,
                        *callback_args,
                    )
            else:
                result = callback(*args)
            if inspect.isawaitable(result):
                if include_owner and not fence_callback and self.connection_owner is not None:
                    await self._await_callback_while_owned(result)
                else:
                    await result
        except asyncio.CancelledError:
            raise
        except Exception:
            _logger.debug(
                "executor_ws: executor callback failed",
                extra={"extra_data": {"executor_id": self.executor_id}},
                exc_info=True,
            )

    async def _await_callback_while_owned(self, awaitable: Any) -> None:
        """Cancel connection-owned work promptly when its owner epoch changes."""

        task = asyncio.ensure_future(awaitable)
        try:
            while not task.done():
                done, _pending = await asyncio.wait(
                    {task},
                    timeout=_OWNED_CALLBACK_POLL_SECONDS,
                )
                if done:
                    break
                if (
                    self._ownership_check_callback is not None
                    and await self._ownership_check_callback() is not True
                ):
                    task.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await task
                    return
            await task
        finally:
            if not task.done():
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task

    async def _renew_ownership_from_heartbeat(self, received_at: datetime) -> None:
        if self._heartbeat_callback is None:
            return
        current = await self._heartbeat_callback(received_at)
        if current is not True:
            await self._close_for_ownership_loss()
            return
        self._ownership_deadline = (
            asyncio.get_running_loop().time() + EXECUTOR_CONNECTION_LEASE_TTL_SECONDS
        )

    async def _apply_owned_effect(self, effect: Callable[[], None]) -> bool:
        if self.connection_owner is None:
            effect()
            return True
        if self._owned_effect_dispatcher is not None:
            return bool(await self._owned_effect_dispatcher(self.connection_owner, effect))
        if (
            self._ownership_check_callback is not None
            and await self._ownership_check_callback() is not True
        ):
            return False
        effect()
        return True


# ---------------------------------------------------------------------------
# WebSocketExecutorProvider
# ---------------------------------------------------------------------------


class WebSocketExecutorProvider:
    """Manages remote executor connections via WebSocket.

    Executors are pre-registered in the database.  When an executor process
    starts, it connects to ``WS /api/executor/ws`` and sends
    ``executor.ready``.  The provider matches the connection to a pending
    registration and makes it available for tool execution.
    """

    def __init__(self, *, browser_terminal_state_path: Path | None = None) -> None:
        self._connections: dict[str, WebSocketExecutorConnection] = {}
        self._handles: dict[str, ExecutorHandle] = {}
        self._ready_events: dict[str, asyncio.Event] = {}
        self._connection_waiters: dict[str, set[asyncio.Event]] = {}
        self._background_shell_completed_callback: Any | None = None
        self._oauth_loopback_callback: Any | None = None
        self._local_model_progress_callback: Any | None = None
        self._local_model_completed_callback: Any | None = None
        self._browser_terminal_state_path = browser_terminal_state_path
        self._pending_browser_terminal = self._load_browser_terminal_pending()
        self._browser_terminal_flush_tasks: dict[str, asyncio.Task[None]] = {}
        self._browser_terminal_flush_locks: dict[str, asyncio.Lock] = {}
        self._browser_terminal_shutdown = False
        self._cluster_enabled = False
        self._cluster_session_factory: Any | None = None
        self._cluster_directory: Any | None = None
        self._cluster_runtime: Any | None = None
        self._cluster_auth: Any | None = None
        self._forwarded_connections: dict[tuple[str, str, int], ForwardedExecutorConnection] = {}
        self._forwarded_by_executor: dict[str, ForwardedExecutorConnection] = {}
        self._cluster_refresh_task: asyncio.Task[None] | None = None
        self._cluster_refresh_lock = asyncio.Lock()

    async def configure_cluster(
        self,
        *,
        enabled: bool,
        session_factory: Any,
        controller_directory: Any,
        controller_runtime: Any,
        auth_provider: Any,
    ) -> None:
        """Enable DB-backed remote-owner discovery only in HA mode."""

        self._cluster_enabled = enabled
        if not enabled:
            return
        self._cluster_session_factory = session_factory
        self._cluster_directory = controller_directory
        self._cluster_runtime = controller_runtime
        self._cluster_auth = auth_provider
        await self.refresh_cluster_directory()
        self._cluster_refresh_task = asyncio.create_task(
            self._cluster_refresh_loop(),
            name="executor-cluster-directory-refresh",
        )

    async def _cluster_refresh_loop(self) -> None:
        while self._cluster_enabled:
            try:
                await asyncio.sleep(1.0)
                await self.refresh_cluster_directory()
            except asyncio.CancelledError:
                raise
            except Exception:
                _logger.warning("executor cluster directory refresh failed", exc_info=True)

    async def refresh_cluster_directory(self) -> None:
        async with self._cluster_refresh_lock:
            await self._refresh_cluster_directory_once()

    async def _refresh_cluster_directory_once(self) -> None:
        if not self._cluster_enabled or self._cluster_session_factory is None:
            return
        if (
            self._cluster_directory is None
            or self._cluster_runtime is None
            or self._cluster_auth is None
        ):
            return
        discovered: dict[str, ForwardedExecutorConnection] = {}
        discovered_handles: dict[str, ExecutorHandle] = {}
        candidate_cache = {
            key: proxy for key, proxy in self._forwarded_connections.items() if not proxy.closing
        }
        async with self._cluster_session_factory() as session:
            now = database_now_expression(session)
            leases = list(
                (
                    await session.scalars(
                        select(CoordinationLeaseRow).where(
                            CoordinationLeaseRow.resource_key.like("executor_connection:%"),
                            CoordinationLeaseRow.lease_expires_at > now,
                        )
                    )
                ).all()
            )
            for lease in leases:
                executor_id = lease.resource_key.removeprefix("executor_connection:")
                if executor_id in self._connections:
                    continue
                if (
                    self._cluster_runtime is None
                    or lease.owner_id == self._cluster_runtime.owner_id
                ):
                    continue
                controller = await self._cluster_directory.get_ready(lease.owner_id)
                if controller is None or not controller.internal_url:
                    continue
                row = await session.get(ExecutorRow, executor_id)
                if row is None or row.status != "active":
                    continue
                capabilities = ExecutorCapabilities.model_validate(
                    (row.runtime_metadata or {}).get("capabilities") or {}
                )
                runtime_metadata = dict(row.runtime_metadata or {})
                key = (executor_id, lease.owner_id, int(lease.fencing_token))
                proxy = candidate_cache.get(key)
                if proxy is None or proxy.closing:
                    proxy = ForwardedExecutorConnection(
                        executor_id=executor_id,
                        capabilities=capabilities,
                        owner_id=lease.owner_id,
                        epoch=int(lease.fencing_token),
                        owner_internal_url=controller.internal_url,
                        requester_owner_id=self._cluster_runtime.owner_id,
                        auth_provider=self._cluster_auth,
                    )
                    candidate_cache[key] = proxy
                else:
                    # Keep a stable proxy transport, but refresh its advertised
                    # capabilities and endpoint metadata on every directory poll.
                    proxy.capabilities = capabilities
                    proxy.owner_internal_url = controller.internal_url
                discovered[executor_id] = proxy
                discovered_handles[executor_id] = ExecutorHandle(
                    executor_id=executor_id,
                    executor_type="websocket",
                    capabilities=capabilities,
                    status="ready",
                    metadata={
                        "labels": row.labels or {},
                        "platform": runtime_metadata.get("platform") or {},
                        "environment": runtime_metadata.get("environment") or {},
                        "runtime_metadata": runtime_metadata,
                        "status": row.status,
                        "owner_email": row.owner_email,
                        "shared": is_shared_owner_email(row.owner_email),
                        "owner_id": lease.owner_id,
                        "connection_epoch": int(lease.fencing_token),
                        "forwarded": True,
                    },
                )
        for executor_id in self._connections:
            discovered.pop(executor_id, None)
            discovered_handles.pop(executor_id, None)
        stale = [
            proxy
            for executor_id, proxy in self._forwarded_by_executor.items()
            if discovered.get(executor_id) is not proxy
        ]
        local_handles = {
            executor_id: handle
            for executor_id, handle in self._handles.items()
            if not bool((handle.metadata or {}).get("forwarded"))
        }
        active_proxies = set(discovered.values())
        next_cache = {
            key: proxy for key, proxy in candidate_cache.items() if proxy in active_proxies
        }
        self._forwarded_connections = next_cache
        self._forwarded_by_executor = discovered
        self._handles = {**local_handles, **discovered_handles}
        for executor_id in discovered:
            for waiter in self._connection_waiters.pop(executor_id, set()):
                waiter.set()
        for proxy in stale:
            await proxy.close()

    # ------------------------------------------------------------------
    # Called by the WS endpoint when an executor connects
    # ------------------------------------------------------------------

    def register_connection(
        self,
        executor_id: str,
        ws: WebSocket,
        capabilities: ExecutorCapabilities | None = None,
        *,
        ready: bool = True,
        metadata: dict[str, Any] | None = None,
        connection_owner: ExecutorConnectionOwner | None = None,
        start_receiver: bool = True,
    ) -> WebSocketExecutorConnection:
        """Register a newly connected executor.

        Returns the ``WebSocketExecutorConnection`` so the endpoint can
        drive the receiver loop.
        """
        conn = WebSocketExecutorConnection(
            ws,
            executor_id,
            capabilities or ExecutorCapabilities(),
            connection_owner=connection_owner,
            replay_connection_resolver=self._wait_for_replacement_connection,
        )
        conn.register_background_shell_completed_callback(self._background_shell_completed_callback)
        conn.register_oauth_loopback_callback(self._oauth_loopback_callback)
        conn.register_local_model_callbacks(
            on_progress=self._local_model_progress_callback,
            on_completed=self._local_model_completed_callback,
        )
        if start_receiver:
            conn.start_receiver()

        # If this is a reconnection, close the old connection so its receiver
        # task stops and pending RPCs fail fast.  The old handle_executor_websocket
        # coroutine is in wait_until_closed(); closing the connection unblocks it.
        old = self._connections.pop(executor_id, None)
        if old is not None:
            EXECUTOR_WS_RECONNECTIONS.inc()
            _logger.info(
                "executor_ws: executor reconnected, closing previous connection",
                extra={"extra_data": {"executor_id": executor_id}},
            )
            asyncio.create_task(old.close(), name=f"executor-old-conn-close-{executor_id}")

        self._connections[executor_id] = conn
        EXECUTOR_WS_CONNECTIONS.inc()

        # Update or create handle
        if executor_id not in self._handles:
            self._handles[executor_id] = ExecutorHandle(
                executor_id=executor_id,
                executor_type="websocket",
                capabilities=capabilities or ExecutorCapabilities(),
                status="pending",
                metadata=metadata or {},
            )
        else:
            if metadata:
                self._handles[executor_id].metadata = metadata

        if ready:
            self.mark_ready(
                executor_id,
                capabilities or ExecutorCapabilities(),
                metadata=metadata,
                expected_connection=conn,
            )

        return conn

    def mark_ready(
        self,
        executor_id: str,
        capabilities: ExecutorCapabilities,
        *,
        metadata: dict[str, Any] | None = None,
        expected_connection: WebSocketExecutorConnection | None = None,
    ) -> bool:
        """Mark a connected executor as fully configured and ready."""
        connection = self._connections.get(executor_id)
        if expected_connection is not None and connection is not expected_connection:
            return False
        handle = self._handles.get(executor_id)
        if handle is None:
            handle = ExecutorHandle(
                executor_id=executor_id,
                executor_type="websocket",
                capabilities=capabilities,
                status="ready",
                metadata=metadata or {},
            )
            self._handles[executor_id] = handle
        else:
            handle.capabilities = capabilities
            handle.status = "ready"
            if metadata is not None:
                handle.metadata = metadata
        if connection is not None:
            connection.capabilities = capabilities

        event = self._ready_events.get(executor_id)
        if event is not None:
            event.set()
        for waiter in self._connection_waiters.pop(executor_id, set()):
            waiter.set()
        if self._pending_browser_terminal.get(executor_id):
            self.schedule_browser_terminal_flush(executor_id)
        return True

    def unregister_connection(
        self,
        executor_id: str,
        connection: WebSocketExecutorConnection | None = None,
    ) -> None:
        """Called when an executor WebSocket disconnects."""
        conn = self._connections.get(executor_id)
        if connection is not None and conn is not connection:
            return
        conn = self._connections.pop(executor_id, None)
        if conn is not None:
            EXECUTOR_WS_CONNECTIONS.dec()
        handle = self._handles.get(executor_id)
        if handle is not None:
            handle.status = "disconnected"
        _logger.info(
            "executor_ws: executor disconnected",
            extra={"extra_data": {"executor_id": executor_id}},
        )

    def get_handle_metadata(self, executor_id: str) -> dict[str, Any] | None:
        """Return the current metadata for a connected executor handle."""

        handle = self._handles.get(executor_id)
        if handle is None:
            return None
        return dict(handle.metadata)

    def get_handle(self, executor_id: str) -> ExecutorHandle | None:
        return self._handles.get(executor_id)

    def iter_ready_handles(self) -> tuple[ExecutorHandle, ...]:
        return tuple(handle for handle in self._handles.values() if handle.status == "ready")

    def iter_local_ready_handles(self) -> tuple[ExecutorHandle, ...]:
        return tuple(
            handle
            for executor_id, handle in self._handles.items()
            if executor_id in self._connections and handle.status == "ready"
        )

    def register_background_shell_completed_callback(self, callback: Any | None) -> None:
        """Register callback for all current and future executor shell completions."""

        self._background_shell_completed_callback = callback
        for connection in self._connections.values():
            connection.register_background_shell_completed_callback(callback)

    def register_oauth_loopback_callback(self, callback: Any | None) -> None:
        """Register callback for all current and future OAuth loopback callbacks."""

        self._oauth_loopback_callback = callback
        for connection in self._connections.values():
            connection.register_oauth_loopback_callback(callback)

    def register_local_model_callbacks(
        self,
        *,
        on_progress: Any | None,
        on_completed: Any | None,
    ) -> None:
        """Register callbacks for all current and future executor connections."""

        self._local_model_progress_callback = on_progress
        self._local_model_completed_callback = on_completed
        for connection in self._connections.values():
            connection.register_local_model_callbacks(
                on_progress=on_progress,
                on_completed=on_completed,
            )

    def first_ready_connection(self) -> WebSocketExecutorConnection | None:
        for executor_id, conn in self._connections.items():
            handle = self._handles.get(executor_id)
            if conn.connected and handle is not None and handle.status == "ready":
                return conn
        return None

    # ------------------------------------------------------------------
    # ExecutorProvider protocol
    # ------------------------------------------------------------------

    async def spawn(self, config: ExecutorConfig) -> ExecutorHandle:
        """Wait for a remote executor to connect and send ``executor.ready``.

        For pre-registered (always-on) executors, this waits up to 30 s for
        the executor to connect.  If the executor is already connected, it
        returns immediately.
        """
        executor_id = config.executor_id

        # Already connected?
        if (
            executor_id in self._connections
            and self._connections[executor_id].connected
            and self._handles.get(executor_id) is not None
            and self._handles[executor_id].status == "ready"
        ):
            return self._handles[executor_id]

        # Wait for connection
        event = asyncio.Event()
        self._ready_events[executor_id] = event
        try:
            await asyncio.wait_for(event.wait(), timeout=30.0)
        except TimeoutError:
            self._ready_events.pop(executor_id, None)
            raise TimeoutError(
                f"Executor {executor_id} did not connect within 30 seconds"
            ) from None
        finally:
            self._ready_events.pop(executor_id, None)

        return self._handles[executor_id]

    async def get_executor(
        self, handle: ExecutorHandle
    ) -> WebSocketExecutorConnection | ForwardedExecutorConnection:
        """Return the live connection for a handle."""
        conn = self.get_connection(handle.executor_id)
        if conn is None or not conn.connected:
            raise ExecutorDisconnectedError(
                f"Executor {handle.executor_id} is not connected",
                DeliveryState.NOT_SENT,
                code="executor_disconnected",
                executor_id=handle.executor_id,
            )
        return conn

    async def cancel(self, handle: ExecutorHandle) -> None:
        """Send ``executor.cancel`` and close the connection."""
        conn = self._connections.get(handle.executor_id)
        if conn is not None and conn.connected:
            _logger.info(
                "executor_ws: sending executor.cancel",
                extra={"extra_data": {"executor_id": handle.executor_id}},
            )
            with contextlib.suppress(Exception):
                await conn.rpc_call("executor.cancel", {"reason": "cancelled"}, timeout=5.0)
            await conn.close()
        self._connections.pop(handle.executor_id, None)
        self._handles.pop(handle.executor_id, None)

    async def list_active(self) -> list[ExecutorHandle]:
        """List handles for all connected executors."""
        if self._cluster_enabled:
            await self.refresh_cluster_directory()
        return [h for h in self._handles.values() if h.status == "ready"]

    async def cleanup(self) -> None:
        """Drop controller-side websocket connections without stopping remote runtimes."""
        self._browser_terminal_shutdown = True
        tasks = list(self._browser_terminal_flush_tasks.values())
        if tasks:
            _, unfinished = await asyncio.wait(tasks, timeout=5.5)
        else:
            unfinished = set()
        if not self._persist_browser_terminal_pending():
            raise RuntimeError("Failed to persist pending browser terminal notifications")
        for task in unfinished:
            task.cancel()
        if unfinished:
            await asyncio.gather(
                *unfinished,
                return_exceptions=True,
            )
        self._browser_terminal_flush_tasks.clear()
        for executor_id, conn in list(self._connections.items()):
            _logger.info(
                "executor_ws: closing controller-side connection during cleanup",
                extra={"extra_data": {"executor_id": executor_id}},
            )
            with contextlib.suppress(Exception):
                await conn.close()
            self.unregister_connection(executor_id, conn)
        if self._cluster_refresh_task is not None:
            self._cluster_refresh_task.cancel()
            await asyncio.gather(self._cluster_refresh_task, return_exceptions=True)
            self._cluster_refresh_task = None
        for proxy in self._forwarded_connections.values():
            await proxy.close()
        self._forwarded_connections.clear()
        self._forwarded_by_executor.clear()

    async def health(self) -> ProviderHealth:
        """Report health of the WebSocket executor provider."""
        connected = sum(1 for c in self._connections.values() if c.connected)
        return ProviderHealth(
            name="executor_websocket",
            status="healthy" if connected > 0 or not self._handles else "degraded",
            details={
                "connected_executors": connected,
                "registered_executors": len(self._handles),
            },
        )

    def get_local_connection(self, executor_id: str) -> WebSocketExecutorConnection | None:
        """Get only a physical connection owned by this controller."""

        conn = self._connections.get(executor_id)
        return conn if conn is not None and conn.connected else None

    async def _wait_for_replacement_connection(
        self,
        executor_id: str,
        previous: WebSocketExecutorConnection,
        timeout: float,
    ) -> WebSocketExecutorConnection | None:
        """Wait briefly for a ready same-ID connection replacing a failed transport."""

        deadline = asyncio.get_running_loop().time() + max(0.0, timeout)
        while True:
            current = self.get_local_connection(executor_id)
            handle = self._handles.get(executor_id)
            if (
                current is not None
                and current is not previous
                and handle is not None
                and handle.status == "ready"
            ):
                return current
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                return None
            waiter = asyncio.Event()
            self._connection_waiters.setdefault(executor_id, set()).add(waiter)
            try:
                await asyncio.wait_for(waiter.wait(), timeout=remaining)
            except TimeoutError:
                return None
            finally:
                waiters = self._connection_waiters.get(executor_id)
                if waiters is not None:
                    waiters.discard(waiter)
                    if not waiters:
                        self._connection_waiters.pop(executor_id, None)

    def get_connection(
        self, executor_id: str
    ) -> WebSocketExecutorConnection | ForwardedExecutorConnection | None:
        """Get a connection by executor ID (used by inference router)."""
        local = self.get_local_connection(executor_id)
        if local is not None:
            return local
        forwarded = self._forwarded_by_executor.get(executor_id)
        return forwarded if forwarded is not None and not forwarded.closing else None

    async def invalidate_forwarded_connection(
        self,
        executor_id: str,
        connection: ForwardedExecutorConnection,
    ) -> None:
        """Drop one stale forwarded proxy before owner/epoch re-resolution."""

        async with self._cluster_refresh_lock:
            proxy = self._forwarded_by_executor.get(executor_id)
            if proxy is not connection:
                return
            self._forwarded_by_executor.pop(executor_id, None)
            for key, candidate in tuple(self._forwarded_connections.items()):
                if candidate is connection:
                    self._forwarded_connections.pop(key, None)
            await connection.close()

    async def notify_browser_session_terminal(
        self,
        executor_ids: list[str],
        owner: dict[str, Any],
    ) -> None:
        scope_id = str(owner.get("execution_scope_id") or "")
        if not scope_id:
            return
        for executor_id in executor_ids:
            self._pending_browser_terminal.setdefault(executor_id, {})[scope_id] = dict(owner)
            if not self._persist_browser_terminal_pending():
                raise RuntimeError("Failed to persist browser terminal notification before enqueue")
            self.schedule_browser_terminal_flush(executor_id)

    def schedule_browser_terminal_flush(self, executor_id: str) -> None:
        if self._browser_terminal_shutdown:
            return
        task = self._browser_terminal_flush_tasks.get(executor_id)
        if task is not None and not task.done():
            return
        task = asyncio.create_task(
            self._run_browser_terminal_flush(executor_id),
            name=f"browser-terminal-flush-{executor_id}",
        )
        self._browser_terminal_flush_tasks[executor_id] = task

        def flush_done(done: asyncio.Future[None]) -> None:
            self._browser_terminal_flush_done(executor_id, cast(asyncio.Task[None], done))

        task.add_done_callback(flush_done)

    def _browser_terminal_flush_done(
        self,
        executor_id: str,
        task: asyncio.Task[None],
    ) -> None:
        if self._browser_terminal_flush_tasks.get(executor_id) is task:
            self._browser_terminal_flush_tasks.pop(executor_id, None)
        if not task.cancelled() and task.exception() is not None:
            _logger.warning(
                "executor_ws: browser terminal retry worker failed",
                extra={"extra_data": {"executor_id": executor_id}},
                exc_info=task.exception(),
            )
        if (
            self._pending_browser_terminal.get(executor_id)
            and self.get_connection(executor_id) is not None
        ):
            self.schedule_browser_terminal_flush(executor_id)

    async def _run_browser_terminal_flush(self, executor_id: str) -> None:
        retry_delay = 0.25
        while self._pending_browser_terminal.get(executor_id):
            if self.get_connection(executor_id) is None:
                return
            await self.flush_browser_terminal_notifications(executor_id)
            if self._pending_browser_terminal.get(executor_id):
                await asyncio.sleep(retry_delay)
                retry_delay = min(retry_delay * 2, 5.0)

    async def flush_browser_terminal_notifications(self, executor_id: str) -> None:
        lock = self._browser_terminal_flush_locks.setdefault(executor_id, asyncio.Lock())
        async with lock:
            conn = self.get_connection(executor_id)
            pending = self._pending_browser_terminal.get(executor_id)
            if conn is None or not pending:
                return

            async def deliver(
                scope_id: str,
                owner: dict[str, Any],
            ) -> tuple[str, bool]:
                try:
                    result = await conn.isolated_rpc_call(
                        "browser.session_terminal",
                        {"owner": owner},
                        timeout=5.0,
                    )
                    current_pending = self._pending_browser_terminal.get(executor_id, {})
                    current_connection = self.get_connection(executor_id)
                    return (
                        scope_id,
                        result.get("complete") is True
                        and current_connection is conn
                        and current_pending.get(scope_id) == owner,
                    )
                except Exception:
                    _logger.warning(
                        "executor_ws: browser terminal notification deferred",
                        extra={
                            "extra_data": {
                                "executor_id": executor_id,
                                "execution_scope_id": scope_id,
                            }
                        },
                        exc_info=True,
                    )
                    return scope_id, False

            results = await asyncio.gather(
                *(deliver(scope_id, owner) for scope_id, owner in list(pending.items()))
            )
            for scope_id, complete in results:
                if complete:
                    pending.pop(scope_id, None)
            if not pending:
                self._pending_browser_terminal.pop(executor_id, None)
            self._persist_browser_terminal_pending()

    def _load_browser_terminal_pending(self) -> dict[str, dict[str, dict[str, Any]]]:
        path = self._browser_terminal_state_path
        if path is None or not path.exists():
            return {}
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            _logger.warning("executor_ws: failed to load browser terminal state", exc_info=True)
            return {}
        if not isinstance(raw, dict):
            return {}
        return {
            str(executor_id): {
                str(scope_id): dict(owner)
                for scope_id, owner in scopes.items()
                if isinstance(owner, dict)
            }
            for executor_id, scopes in raw.items()
            if isinstance(scopes, dict)
        }

    def _persist_browser_terminal_pending(self) -> bool:
        path = self._browser_terminal_state_path
        if path is None:
            return True
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            temp_path = path.with_suffix(f"{path.suffix}.tmp")
            temp_path.write_text(
                json.dumps(self._pending_browser_terminal, sort_keys=True),
                encoding="utf-8",
            )
            os.chmod(temp_path, 0o600)
            os.replace(temp_path, path)
            return True
        except OSError:
            _logger.error("executor_ws: failed to persist browser terminal state", exc_info=True)
            return False

    async def wait_for_connection(
        self,
        executor_id: str,
        *,
        timeout: float | None = None,
        failed_connection: WebSocketExecutorConnection | ForwardedExecutorConnection | None = None,
        delivery_state: DeliveryState | str = DeliveryState.NOT_SENT,
        failed_generation: int | None = None,
        failed_owner_id: str | None = None,
        failed_epoch: int | None = None,
    ) -> WebSocketExecutorConnection | ForwardedExecutorConnection | None:
        """Wait for an eligible same-ID transport under delivery-certainty rules."""

        state = DeliveryState(delivery_state)
        deadline = asyncio.get_running_loop().time() + (
            timeout if timeout is not None else executor_reconnect_retry_budget_seconds()
        )
        event = asyncio.Event()
        refresh_delay = 0.1
        self._connection_waiters.setdefault(executor_id, set()).add(event)
        try:
            while True:
                remaining = deadline - asyncio.get_running_loop().time()
                if remaining <= 0:
                    return None
                if self._cluster_enabled:
                    try:
                        await asyncio.wait_for(
                            self.refresh_cluster_directory(),
                            timeout=remaining,
                        )
                    except TimeoutError:
                        return None
                conn = self.get_connection(executor_id)
                if self._connection_is_retry_eligible(
                    executor_id,
                    conn,
                    failed_connection=failed_connection,
                    delivery_state=state,
                    failed_generation=failed_generation,
                    failed_owner_id=failed_owner_id,
                    failed_epoch=failed_epoch,
                ):
                    return conn
                remaining = deadline - asyncio.get_running_loop().time()
                if remaining <= 0:
                    return None
                event.clear()
                try:
                    await asyncio.wait_for(
                        event.wait(),
                        timeout=min(remaining, refresh_delay),
                    )
                except TimeoutError:
                    refresh_delay = min(refresh_delay * 2, 1.0)
                    continue
        finally:
            waiters = self._connection_waiters.get(executor_id)
            if waiters is not None:
                waiters.discard(event)
                if not waiters:
                    self._connection_waiters.pop(executor_id, None)

    def _connection_is_retry_eligible(
        self,
        executor_id: str,
        connection: WebSocketExecutorConnection | ForwardedExecutorConnection | None,
        *,
        failed_connection: WebSocketExecutorConnection | ForwardedExecutorConnection | None,
        delivery_state: DeliveryState,
        failed_generation: int | None,
        failed_owner_id: str | None,
        failed_epoch: int | None,
    ) -> bool:
        if connection is None:
            return False
        if isinstance(connection, ForwardedExecutorConnection) and connection.closing:
            return False
        if delivery_state is DeliveryState.NOT_SENT:
            return True
        if connection is failed_connection:
            return False
        handle = self._handles.get(executor_id)
        metadata = dict(handle.metadata or {}) if handle is not None else {}
        candidate_generation = metadata.get("generation")
        if not isinstance(candidate_generation, int):
            candidate_generation = metadata.get("active_executor_generation")
        if not isinstance(candidate_generation, int):
            candidate_generation = None
        if isinstance(connection, ForwardedExecutorConnection):
            candidate_owner_id = connection.owner_id
            candidate_epoch = connection.epoch
        else:
            owner = connection.connection_owner
            candidate_owner_id = owner.owner_id if owner is not None else None
            candidate_epoch = owner.epoch if owner is not None else None
        comparisons = [
            candidate != failed
            for candidate, failed in (
                (candidate_generation, failed_generation),
                (candidate_owner_id, failed_owner_id),
                (candidate_epoch, failed_epoch),
            )
            if candidate is not None and failed is not None
        ]
        return bool(comparisons) and any(comparisons)

    async def get_lsp_status(
        self,
        handle: ExecutorHandle,
        *,
        source: dict[str, Any] | None = None,
    ) -> LSPStatusReport:
        """Return normalized LSP status for a websocket-backed executor."""
        runtime_metadata = (handle.metadata or {}).get("runtime_metadata") or {}
        configure_capabilities = runtime_metadata.get("configure_capabilities") or []
        config_source = source or {}
        if not bool(config_source.get("lsp_enabled", True)):
            return build_lsp_unavailable_report(
                executor_id=handle.executor_id,
                executor_type=handle.executor_type,
                source=config_source,
                state="disabled",
            )
        conn = self.get_connection(handle.executor_id)
        if conn is None:
            return build_lsp_unavailable_report(
                executor_id=handle.executor_id,
                executor_type=handle.executor_type,
                source=config_source,
                state="unavailable",
                warning="Executor is not connected.",
            )
        if LSP_STATUS_CAPABILITY not in configure_capabilities:
            return build_lsp_unavailable_report(
                executor_id=handle.executor_id,
                executor_type=handle.executor_type,
                source=config_source,
                state="unsupported",
                warning="Executor does not support LSP status.",
                supported=False,
            )
        try:
            result = await conn.lsp_status()
            report = LSPStatusReport.model_validate(result)
            return report.model_copy(
                update={
                    "executor_id": handle.executor_id,
                    "executor_type": handle.executor_type,
                }
            )
        except Exception:
            _logger.debug(
                "executor_ws: lsp.status failed",
                extra={"extra_data": {"executor_id": handle.executor_id}},
                exc_info=True,
            )
            return build_lsp_unavailable_report(
                executor_id=handle.executor_id,
                executor_type=handle.executor_type,
                source=config_source,
                state="unavailable",
                warning="Timed out or failed to fetch LSP status.",
            )

    def owns_connection(self, executor_id: str, connection: WebSocketExecutorConnection) -> bool:
        """Return whether the given connection is still current for an executor."""
        return self._connections.get(executor_id) is connection
