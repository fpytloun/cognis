"""WebSocket-based remote executor provider and connection."""

from __future__ import annotations

import asyncio
import contextlib
import os
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from time import perf_counter
from typing import Any

from prometheus_client import Counter, Gauge, Histogram
from starlette.websockets import WebSocket, WebSocketDisconnect

from cognis.logging import get_logger
from cognis.models.config import ProviderHealth
from cognis.models.tool import (
    ExecutorCapabilities,
    ExecutorConfig,
    ExecutorHandle,
    ToolCall,
    ToolResult,
)
from cognis.providers.base import ToolOutputChunkCallback
from cognis.providers.circuit_breaker import CircuitBreaker, CircuitBreakerError
from cognis.tools.executor.lsp.runtime import (
    LSP_STATUS_CAPABILITY,
    LSPStatusReport,
    build_lsp_unavailable_report,
)

_logger = get_logger(__name__)

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


class ExecutorDisconnectedError(RuntimeError):
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
    ) -> None:
        self._ws = ws
        self.executor_id = executor_id
        self.capabilities = capabilities
        self.breaker = breaker or CircuitBreaker(failure_threshold=5, recovery_timeout=30.0)

        # Correlation tracking
        self._pending: dict[str, asyncio.Future[dict[str, Any]]] = {}
        self._receiver_task: asyncio.Task[None] | None = None

        # Heartbeat tracking
        self.last_heartbeat: datetime = datetime.now(UTC)
        self._connected = True

        # LLM inference streaming queues (request_id → queue)
        self._inference_queues: dict[str, asyncio.Queue[dict[str, Any]]] = {}
        self._tool_chunk_callbacks: dict[str, ToolOutputChunkCallback] = {}

        # Channel adapter notification callbacks (account_id → callback)
        self._channel_message_callbacks: dict[str, Any] = {}
        self._channel_status_callbacks: dict[str, Any] = {}
        self._background_shell_completed_callback: Any | None = None

    @property
    def connected(self) -> bool:
        return self._connected

    def start_receiver(self) -> None:
        """Start the background message receiver task."""
        if self._receiver_task is None or self._receiver_task.done():
            self._receiver_task = asyncio.create_task(
                self._receive_loop(), name=f"executor-rx-{self.executor_id}"
            )

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
        # Fail all pending RPCs
        self._fail_pending("Executor connection closed")
        # Terminate inference queues
        for q in self._inference_queues.values():
            q.put_nowait({"error": "Executor disconnected", "done": True})
        self._inference_queues.clear()

    # ------------------------------------------------------------------
    # ExecutorConnection protocol
    # ------------------------------------------------------------------

    async def rpc_call(
        self, method: str, params: dict[str, Any], timeout: float | None = None
    ) -> dict[str, Any]:
        """Send a JSON-RPC request and await the correlated response."""
        if not self._connected:
            raise ExecutorDisconnectedError(f"Executor {self.executor_id} is not connected")

        request_id = uuid.uuid4().hex
        request = {
            "jsonrpc": "2.0",
            "method": method,
            "params": params,
            "id": request_id,
        }

        future: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future

        effective_timeout = timeout or _RPC_TIMEOUT_SECONDS
        start = perf_counter()
        try:

            async def _do_call() -> dict[str, Any]:
                await self._ws.send_json(request)
                return await asyncio.wait_for(future, timeout=effective_timeout)

            result = await self.breaker.call(_do_call)
            EXECUTOR_WS_RPC_DURATION.labels(method=method).observe(perf_counter() - start)
            return result
        except TimeoutError:
            EXECUTOR_WS_RPC_ERRORS.labels(method=method, error_type="timeout").inc()
            self._pending.pop(request_id, None)
            raise
        except CircuitBreakerError:
            EXECUTOR_WS_RPC_ERRORS.labels(method=method, error_type="circuit_open").inc()
            self._pending.pop(request_id, None)
            raise
        except ExecutorDisconnectedError:
            EXECUTOR_WS_RPC_ERRORS.labels(method=method, error_type="disconnected").inc()
            self._pending.pop(request_id, None)
            raise
        except Exception:
            EXECUTOR_WS_RPC_ERRORS.labels(method=method, error_type="unknown").inc()
            self._pending.pop(request_id, None)
            raise

    async def list_tools(self) -> list[dict[str, Any]]:
        """Return tool metadata from the remote executor."""
        result = await self.rpc_call("tool.list", {})
        return result.get("tools", [])

    async def tool_execute(
        self,
        tool_call: ToolCall,
        timeout_seconds: int | None = None,
        output_chunk_callback: ToolOutputChunkCallback | None = None,
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
        except TimeoutError:
            return ToolResult(
                output="Tool execution timed out.",
                is_error=True,
                metadata={"code": "tool_execution_timeout", "retryable": False},
            )
        except asyncio.CancelledError:
            return ToolResult(
                output="Tool execution cancelled.",
                is_error=True,
                metadata={"code": "tool_execution_cancelled", "retryable": False},
            )
        except ExecutorDisconnectedError:
            code = "executor_disconnected"
            return ToolResult(
                output=_transient_executor_output(executor_id=self.executor_id, code=code),
                is_error=True,
                metadata={
                    "code": code,
                    "executor_id": self.executor_id,
                    "retryable": True,
                    "same_executor_only": True,
                },
            )
        except CircuitBreakerError:
            code = "executor_circuit_open"
            return ToolResult(
                output=_transient_executor_output(executor_id=self.executor_id, code=code),
                is_error=True,
                metadata={
                    "code": code,
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

    # ------------------------------------------------------------------
    # LLM inference streaming (via rpc_call, not a protocol method)
    # ------------------------------------------------------------------

    async def llm_complete_stream(
        self,
        request_id: str,
        messages: list[dict[str, Any]],
        model: str,
        **kwargs: Any,
    ) -> AsyncIterator[dict[str, Any]]:
        """Route an LLM completion request to the executor and stream chunks.

        This is NOT on the ``ExecutorConnection`` protocol — it uses
        ``rpc_call("llm.complete", ...)`` and an ``asyncio.Queue`` bridge
        fed by ``llm.chunk`` / ``llm.done`` notifications from the receiver.
        """
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
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
            await self.rpc_call("llm.complete", params, timeout=10.0)

            # Yield chunks from the queue until llm.done arrives
            while True:
                chunk = await asyncio.wait_for(queue.get(), timeout=120.0)
                if chunk.get("done"):
                    yield chunk
                    break
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
                yield chunk
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

    async def lsp_status(self) -> dict[str, Any]:
        """Fetch normalized LSP status from a remote executor."""
        return await self.rpc_call("lsp.status", {}, timeout=_LSP_STATUS_TIMEOUT_SECONDS)

    async def background_shell_status(self, *, include_completed: bool = False) -> dict[str, Any]:
        """Fetch background shell status from a remote executor."""
        return await self.rpc_call(
            "shell.background_status",
            {"include_completed": include_completed},
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
                except WebSocketDisconnect:
                    break

                if data is None:
                    break

                # JSON-RPC response (has "id" and "result" or "error")
                msg_id = data.get("id")
                if msg_id and msg_id in self._pending:
                    future = self._pending.pop(msg_id)
                    if not future.done():
                        if "error" in data:
                            err = data["error"]
                            future.set_exception(
                                ExecutorRPCError(
                                    err.get("code", -1),
                                    err.get("message", "Unknown error"),
                                    err.get("data"),
                                )
                            )
                        else:
                            future.set_result(data.get("result", {}))
                    continue

                # JSON-RPC notification (has "method" but no "id", or id is
                # not in pending — treat as notification)
                method = data.get("method")
                if method == "executor.heartbeat":
                    self.last_heartbeat = datetime.now(UTC)
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
                        stream = params.get("stream")
                        await callback(str(chunk), str(stream) if stream is not None else None)
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
                    if req_id and req_id in self._inference_queues:
                        self._inference_queues[req_id].put_nowait(params)
                elif method == "llm.done":
                    params = data.get("params", {})
                    req_id = params.get("request_id")
                    if req_id and req_id in self._inference_queues:
                        params["done"] = True
                        self._inference_queues[req_id].put_nowait(params)
                elif method == "shell.background_completed":
                    params = data.get("params", {})
                    if self._background_shell_completed_callback is not None:
                        asyncio.create_task(
                            self._background_shell_completed_callback(
                                self.executor_id,
                                params if isinstance(params, dict) else {},
                            )
                        )
                elif method == "channel.message":
                    params = data.get("params", {})
                    acct_id = params.get("account_id")
                    cb = self._channel_message_callbacks.get(acct_id) if acct_id else None
                    if cb is not None:
                        asyncio.create_task(cb(params.get("message", {})))
                elif method == "channel.status":
                    params = data.get("params", {})
                    acct_id = params.get("account_id")
                    cb = self._channel_status_callbacks.get(acct_id) if acct_id else None
                    if cb is not None:
                        cb(params.get("status", {}))
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
        except Exception:
            _logger.warning(
                "executor_ws: receiver loop error",
                extra={"extra_data": {"executor_id": self.executor_id}},
                exc_info=True,
            )
        finally:
            self._connected = False
            self._fail_pending("Executor disconnected")

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

    def _fail_pending(self, reason: str) -> None:
        """Fail all pending RPC futures with a disconnection error."""
        for future in self._pending.values():
            if not future.done():
                future.set_exception(ExecutorDisconnectedError(reason))
        self._pending.clear()


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

    def __init__(self) -> None:
        self._connections: dict[str, WebSocketExecutorConnection] = {}
        self._handles: dict[str, ExecutorHandle] = {}
        self._ready_events: dict[str, asyncio.Event] = {}
        self._connection_waiters: dict[str, set[asyncio.Event]] = {}
        self._background_shell_completed_callback: Any | None = None

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
    ) -> WebSocketExecutorConnection:
        """Register a newly connected executor.

        Returns the ``WebSocketExecutorConnection`` so the endpoint can
        drive the receiver loop.
        """
        conn = WebSocketExecutorConnection(ws, executor_id, capabilities or ExecutorCapabilities())
        conn.register_background_shell_completed_callback(self._background_shell_completed_callback)
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
            )

        return conn

    def mark_ready(
        self,
        executor_id: str,
        capabilities: ExecutorCapabilities,
        *,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        """Mark a connected executor as fully configured and ready."""
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

        event = self._ready_events.get(executor_id)
        if event is not None:
            event.set()
        for waiter in self._connection_waiters.pop(executor_id, set()):
            waiter.set()

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

    def register_background_shell_completed_callback(self, callback: Any | None) -> None:
        """Register callback for all current and future executor shell completions."""

        self._background_shell_completed_callback = callback
        for connection in self._connections.values():
            connection.register_background_shell_completed_callback(callback)

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

    async def get_executor(self, handle: ExecutorHandle) -> WebSocketExecutorConnection:
        """Return the live connection for a handle."""
        conn = self._connections.get(handle.executor_id)
        if conn is None or not conn.connected:
            raise ExecutorDisconnectedError(f"Executor {handle.executor_id} is not connected")
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
        return [h for h in self._handles.values() if h.status == "ready"]

    async def cleanup(self) -> None:
        """Drop controller-side websocket connections without stopping remote runtimes."""
        for executor_id, conn in list(self._connections.items()):
            _logger.info(
                "executor_ws: closing controller-side connection during cleanup",
                extra={"extra_data": {"executor_id": executor_id}},
            )
            with contextlib.suppress(Exception):
                await conn.close()
            self.unregister_connection(executor_id, conn)

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

    def get_connection(self, executor_id: str) -> WebSocketExecutorConnection | None:
        """Get a connection by executor ID (used by inference router)."""
        conn = self._connections.get(executor_id)
        if conn is not None and conn.connected:
            return conn
        return None

    async def wait_for_connection(
        self,
        executor_id: str,
        *,
        timeout: float | None = None,
    ) -> WebSocketExecutorConnection | None:
        """Wait for the same executor ID to reconnect and become ready."""

        conn = self.get_connection(executor_id)
        if conn is not None:
            return conn

        event = asyncio.Event()
        self._connection_waiters.setdefault(executor_id, set()).add(event)
        try:
            await asyncio.wait_for(
                event.wait(),
                timeout=timeout
                if timeout is not None
                else executor_reconnect_retry_budget_seconds(),
            )
        except TimeoutError:
            return None
        finally:
            waiters = self._connection_waiters.get(executor_id)
            if waiters is not None:
                waiters.discard(event)
                if not waiters:
                    self._connection_waiters.pop(executor_id, None)

        return self.get_connection(executor_id)

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
