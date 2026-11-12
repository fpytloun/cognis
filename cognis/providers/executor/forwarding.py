"""Controller-to-controller forwarded executor connection."""

from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
import math
import uuid
from collections import deque
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, cast
from urllib.parse import urlsplit, urlunsplit

from websockets.asyncio.client import ClientConnection, connect

from cognis import json_codec as json
from cognis.logging import get_logger
from cognis.models.local_models import (
    OllamaRuntimeOperationStatus,
    OllamaRuntimeStartRequest,
    OllamaRuntimeStatus,
)
from cognis.models.program import ProgramRuntimeBinding
from cognis.models.tool import ExecutorCapabilities, ToolCall, ToolResult
from cognis.providers.executor.bridge_limits import (
    BRIDGE_MAX_CALLS,
    BRIDGE_MAX_PROGRAM_RPCS,
    BRIDGE_MAX_QUEUED_CALLS,
    BRIDGE_QUEUE_TIMEOUT_SECONDS,
)
from cognis.providers.executor.delivery import DeliveryState, ExecutorDeliveryError
from cognis.providers.executor.program_rpc import program_rpc_key, program_rpc_key_from_params
from cognis.providers.executor.shell_status import ShellStatusReads
from cognis.tools.registry import (
    TOOL_BACKGROUND_RPC_TIMEOUT_SECONDS,
    ToolBackgroundRequestOutcome,
    parse_tool_background_rpc_result,
)

_logger = get_logger(__name__)

BRIDGE_EVENT_QUEUE_SIZE = 128
BRIDGE_PROTOCOL_VERSION = 2
BRIDGE_CAPABILITIES = ("result_chunks_v1", "program_rpc_v1")
BRIDGE_MAX_FRAME_BYTES = 1024 * 1024
BRIDGE_RESULT_MAX_BYTES = 32 * 1024 * 1024
BRIDGE_RESULT_AGGREGATE_MAX_BYTES = 64 * 1024 * 1024
BRIDGE_RESULT_CHUNK_BYTES = 180 * 1024
BRIDGE_RESULT_ASSEMBLY_TIMEOUT_SECONDS = 30.0
BRIDGE_MAX_RESULT_CHUNKS = math.ceil(BRIDGE_RESULT_MAX_BYTES / BRIDGE_RESULT_CHUNK_BYTES)
BRIDGE_MAX_ACTIVE_ASSEMBLIES = 32
# A retired proxy keeps serving its in-flight calls for at most this long. One
# session's transport failure must not abort another session's running tool.
BRIDGE_RETIRE_DRAIN_TIMEOUT_SECONDS = 600.0


def _bridge_write_was_rejected(exc: Exception) -> bool:
    """Return whether the local bridge client rejected a write before transport."""

    return isinstance(exc, RuntimeError) and 'Cannot call "send" once' in str(exc)


class ForwardedDeliveryError(ExecutorDeliveryError):
    """Forwarded operation failure with explicit delivery certainty."""

    def __init__(
        self,
        message: str,
        delivery_state: DeliveryState | str,
        *,
        executor_id: str | None = None,
        generation: int | None = None,
        owner_id: str | None = None,
        epoch: int | None = None,
        code: str = "executor_delivery_failure",
        retry_after: float | None = None,
        executor_instance_id: str | None = None,
    ) -> None:
        super().__init__(
            message,
            delivery_state,
            code=code,
            executor_id=executor_id,
            generation=generation,
            owner_id=owner_id,
            epoch=epoch,
            retry_after=retry_after,
            executor_instance_id=executor_instance_id,
        )


@dataclass(slots=True)
class _PendingCall:
    future: asyncio.Future[dict[str, Any]]
    events: asyncio.Queue[dict[str, Any]] = field(
        default_factory=lambda: asyncio.Queue(maxsize=BRIDGE_EVENT_QUEUE_SIZE)
    )
    accepted: bool = False
    submitted: bool = False
    accepted_event: asyncio.Event = field(default_factory=asyncio.Event)
    assembly: _ResultAssembly | None = None


@dataclass(slots=True)
class _ResultAssembly:
    chunk_count: int
    byte_length: int
    sha256: str
    chunks: list[bytes | None]
    next_sequence: int = 0
    received_bytes: int = 0
    timeout_handle: asyncio.TimerHandle | None = None


def _bridge_url(internal_url: str) -> str:
    parsed = urlsplit(internal_url)
    scheme = "wss" if parsed.scheme == "https" else "ws"
    return urlunsplit((scheme, parsed.netloc, "/api/internal/executor-bridge", "", ""))


class ForwardedExecutorConnection:
    """High-level executor connection proxied through the physical owner controller."""

    def __init__(
        self,
        *,
        executor_id: str,
        capabilities: ExecutorCapabilities,
        owner_id: str,
        epoch: int,
        owner_internal_url: str,
        requester_owner_id: str,
        auth_provider: Any,
    ) -> None:
        self.executor_id = executor_id
        self.capabilities = capabilities
        self.owner_id = owner_id
        self.epoch = epoch
        self.owner_internal_url = owner_internal_url
        self._requester_owner_id = requester_owner_id
        self._auth = auth_provider
        self._ws: ClientConnection | None = None
        self._opening_ws: ClientConnection | None = None
        self._receiver: asyncio.Task[None] | None = None
        self._connect_lock = asyncio.Lock()
        self._send_lock = asyncio.Lock()
        self._pending: dict[str, _PendingCall] = {}
        self._program_tool_call_handlers: dict[
            tuple[str, str, str, str, str],
            Callable[[dict[str, Any]], Awaitable[dict[str, Any]]],
        ] = {}
        self._program_tool_call_bindings: dict[
            tuple[str, str, str, str, str], ProgramRuntimeBinding
        ] = {}
        self._program_tool_call_tasks: dict[str, asyncio.Task[None]] = {}
        self._shell_status_reads = ShellStatusReads(self._fetch_background_shell_status)
        self._admission_waiters: deque[object] = deque()
        self._admission_changed = asyncio.Event()
        self._tool_bridge_ids: dict[str, str] = {}
        self._connected = False
        self._negotiated_capabilities: set[str] = set()
        self._assembly_bytes = 0
        self._active_assemblies = 0
        self._cancel_tasks: set[asyncio.Task[None]] = set()
        self._closing = False
        self._retiring = False
        self._drain_task: asyncio.Task[None] | None = None
        self._pending_changed = asyncio.Event()
        self.executor_instance_id: str | None = None
        self.retirement_started_at: float | None = None

    @property
    def connected(self) -> bool:
        return self._connected

    def register_program_tool_call_handler(
        self,
        binding: ProgramRuntimeBinding,
        handler: Callable[[dict[str, Any]], Awaitable[dict[str, Any]]],
    ) -> None:
        if binding.executor_id != self.executor_id:
            raise ValueError("Program callback executor identity does not match this proxy.")
        if (
            self.executor_instance_id is not None
            and binding.executor_instance_id != self.executor_instance_id
        ):
            raise ValueError("Program callback process identity does not match this proxy.")
        self._program_tool_call_handlers[program_rpc_key(binding)] = handler
        self._program_tool_call_bindings[program_rpc_key(binding)] = binding

    def unregister_program_tool_call_handler(self, binding: ProgramRuntimeBinding) -> None:
        key = program_rpc_key(binding)
        self._program_tool_call_handlers.pop(key, None)
        self._program_tool_call_bindings.pop(key, None)

    async def _answer_program_tool_call(
        self,
        ws: ClientConnection,
        call_id: str,
        request_id: str,
        params: dict[str, Any],
    ) -> None:
        task = asyncio.current_task()
        if task is not None:
            self._program_tool_call_tasks[request_id] = task
        key = program_rpc_key_from_params(params)
        handler = self._program_tool_call_handlers.get(key) if key is not None else None
        try:
            if "program_rpc_v1" not in self._negotiated_capabilities or handler is None:
                result = {
                    "text": "The live program invocation is unavailable on this bridge.",
                    "is_error": True,
                    "outcome_certainty": "unknown",
                }
            else:
                result = await handler(params)
            await self._send_on_connection(
                ws,
                {
                    "type": "program_tool_call_result",
                    "call_id": call_id,
                    "request_id": request_id,
                    "binding_key": list(key) if key is not None else [],
                    "sequence": params.get("sequence"),
                    "result": result,
                },
            )
        except asyncio.CancelledError:
            with contextlib.suppress(Exception):
                await self._send_on_connection(
                    ws,
                    {
                        "type": "program_tool_call_cancel",
                        "call_id": call_id,
                        "request_id": request_id,
                    },
                )
            raise
        except Exception as exc:
            _logger.warning(
                "bridge client: nested program callback failed",
                extra={
                    "extra_data": {
                        "executor_id": self.executor_id,
                        "error_type": type(exc).__name__,
                    }
                },
            )
            with contextlib.suppress(Exception):
                await self._send_on_connection(
                    ws,
                    {
                        "type": "program_tool_call_result",
                        "call_id": call_id,
                        "request_id": request_id,
                        "binding_key": list(key) if key is not None else [],
                        "sequence": params.get("sequence"),
                        "result": {
                            "text": "Nested program callback failed; outcome is unresolved.",
                            "is_error": True,
                            "outcome_certainty": "unknown",
                        },
                    },
                )
        finally:
            if self._program_tool_call_tasks.get(request_id) is task:
                self._program_tool_call_tasks.pop(request_id, None)

    @property
    def closing(self) -> bool:
        return self._closing

    @property
    def retiring(self) -> bool:
        """Whether this proxy still drains in-flight calls but takes no new ones."""

        return self._retiring

    @property
    def pending_call_count(self) -> int:
        """Number of forwarded calls still awaiting an outcome."""

        return len(self._pending)

    @property
    def drain_task(self) -> asyncio.Task[None] | None:
        """Task draining a retired proxy, when retirement is active."""

        return self._drain_task

    async def retire(
        self,
        *,
        drain_timeout: float = BRIDGE_RETIRE_DRAIN_TIMEOUT_SECONDS,
    ) -> None:
        """Stop routing new calls here and close once in-flight calls finish.

        One proxy is shared by every session that targets the same remote
        executor owner. Closing it on a single call's transport failure would
        fail every other session's in-flight tool call, so a failed call retires
        the proxy instead: new calls build a fresh transport while existing calls
        are allowed to complete.
        """

        if self._closing or self._retiring:
            return
        self._retiring = True
        self._admission_changed.set()
        self.retirement_started_at = asyncio.get_running_loop().time()
        if not self._pending:
            await self.close()
            return
        _logger.info(
            "bridge client: retiring proxy with in-flight calls",
            extra={
                "extra_data": {
                    "executor_id": self.executor_id,
                    "owner_id": self.owner_id,
                    "epoch": self.epoch,
                    "in_flight_calls": len(self._pending),
                    "drain_timeout_seconds": drain_timeout,
                }
            },
        )
        self._drain_task = asyncio.create_task(
            self._drain_then_close(drain_timeout),
            name=f"forwarded-executor-drain-{self.executor_id}-{self.epoch}",
        )

    async def _drain_then_close(self, drain_timeout: float) -> None:
        deadline = asyncio.get_running_loop().time() + max(drain_timeout, 0.0)
        try:
            while self._pending:
                remaining = deadline - asyncio.get_running_loop().time()
                if remaining <= 0:
                    break
                self._pending_changed.clear()
                if not self._pending:
                    break
                try:
                    await asyncio.wait_for(self._pending_changed.wait(), timeout=remaining)
                except TimeoutError:
                    break
        except asyncio.CancelledError:
            raise
        finally:
            if self._pending:
                _logger.warning(
                    "bridge client: retired proxy drain timed out",
                    extra={
                        "extra_data": {
                            "executor_id": self.executor_id,
                            "owner_id": self.owner_id,
                            "epoch": self.epoch,
                            "in_flight_calls": len(self._pending),
                        }
                    },
                )
            await asyncio.shield(self.close())

    def _clear_assembly(self, pending: _PendingCall) -> None:
        assembly = pending.assembly
        if assembly is not None:
            if assembly.timeout_handle is not None:
                assembly.timeout_handle.cancel()
            self._assembly_bytes -= assembly.byte_length
            self._active_assemblies -= 1
            pending.assembly = None

    def _pop_pending(self, call_id: str) -> _PendingCall | None:
        pending = self._pending.pop(call_id, None)
        if pending is not None:
            self._clear_assembly(pending)
            self._pending_changed.set()
            self._admission_changed.set()
        return pending

    def _schedule_cancel(self, call_id: str) -> None:
        if self._closing:
            return

        async def run_cancel() -> None:
            try:
                await self._cancel(call_id)
            except (asyncio.CancelledError, Exception):
                pass
            finally:
                current = asyncio.current_task()
                if current is not None:
                    self._cancel_tasks.discard(current)

        task = asyncio.create_task(run_cancel())
        self._cancel_tasks.add(task)

    def _protocol_failure(self, call_id: str, message: str) -> None:
        pending = self._pop_pending(call_id)
        if pending is None:
            return
        if not pending.future.done():
            pending.future.set_exception(ForwardedDeliveryError(message, "terminal"))
        self._schedule_cancel(call_id)

    def _assembly_timeout(self, call_id: str) -> None:
        self._protocol_failure(call_id, "Forwarded result chunk assembly timed out")

    def _accept_result_start(self, call_id: str, frame: dict[str, Any]) -> None:
        pending = self._pending.get(call_id)
        if pending is None:
            return
        if pending.assembly is not None or pending.future.done():
            self._protocol_failure(call_id, "Duplicate forwarded result start")
            return
        chunk_count = frame.get("chunk_count")
        byte_length = frame.get("byte_length")
        digest = frame.get("sha256")
        if (
            not isinstance(chunk_count, int)
            or isinstance(chunk_count, bool)
            or chunk_count < 1
            or chunk_count > BRIDGE_MAX_RESULT_CHUNKS
            or not isinstance(byte_length, int)
            or isinstance(byte_length, bool)
            or byte_length < 2
            or byte_length > BRIDGE_RESULT_MAX_BYTES
            or not isinstance(digest, str)
            or len(digest) != 64
            or any(char not in "0123456789abcdef" for char in digest)
            or frame.get("sequence") != 0
            or frame.get("encoding") != "base64"
            or frame.get("serialization") != "canonical-json-v1"
            or chunk_count != math.ceil(byte_length / BRIDGE_RESULT_CHUNK_BYTES)
        ):
            self._protocol_failure(call_id, "Malformed forwarded result start")
            return
        if self._assembly_bytes + byte_length > BRIDGE_RESULT_AGGREGATE_MAX_BYTES:
            self._protocol_failure(call_id, "Forwarded result assembly aggregate limit exceeded")
            return
        if self._active_assemblies >= BRIDGE_MAX_ACTIVE_ASSEMBLIES:
            self._protocol_failure(call_id, "Forwarded result assembly count limit exceeded")
            return
        self._assembly_bytes += byte_length
        self._active_assemblies += 1
        assembly = _ResultAssembly(chunk_count, byte_length, digest, [None] * chunk_count)
        assembly.timeout_handle = asyncio.get_running_loop().call_later(
            BRIDGE_RESULT_ASSEMBLY_TIMEOUT_SECONDS, self._assembly_timeout, call_id
        )
        pending.assembly = assembly

    def _accept_result_chunk(self, call_id: str, frame: dict[str, Any]) -> None:
        pending = self._pending.get(call_id)
        assembly = pending.assembly if pending is not None else None
        sequence = frame.get("sequence")
        payload = frame.get("payload")
        if (
            assembly is None
            or not isinstance(sequence, int)
            or isinstance(sequence, bool)
            or sequence < 0
            or sequence >= assembly.chunk_count
            or sequence != assembly.next_sequence
            or not isinstance(payload, str)
            or len(payload) > 4 * math.ceil(BRIDGE_RESULT_CHUNK_BYTES / 3)
        ):
            self._protocol_failure(call_id, "Malformed or out-of-order forwarded result chunk")
            return
        try:
            decoded = base64.b64decode(payload, validate=True)
        except Exception:
            self._protocol_failure(call_id, "Invalid base64 in forwarded result chunk")
            return
        expected_length = (
            BRIDGE_RESULT_CHUNK_BYTES
            if sequence < assembly.chunk_count - 1
            else assembly.byte_length - BRIDGE_RESULT_CHUNK_BYTES * (assembly.chunk_count - 1)
        )
        if (
            len(decoded) != expected_length
            or assembly.received_bytes + len(decoded) > assembly.byte_length
        ):
            self._protocol_failure(call_id, "Forwarded result chunk length exceeds metadata")
            return
        assembly.chunks[sequence] = decoded
        assembly.received_bytes += len(decoded)
        assembly.next_sequence += 1

    def _accept_result_end(self, call_id: str, frame: dict[str, Any]) -> None:
        pending = self._pending.get(call_id)
        if pending is None:
            return
        assembly = pending.assembly
        if assembly is None:
            self._protocol_failure(call_id, "Unexpected forwarded result end")
            return
        if (
            frame.get("chunk_count") != assembly.chunk_count
            or frame.get("byte_length") != assembly.byte_length
            or frame.get("sha256") != assembly.sha256
            or any(chunk is None for chunk in assembly.chunks)
            or assembly.next_sequence != assembly.chunk_count
            or assembly.received_bytes != assembly.byte_length
        ):
            self._protocol_failure(call_id, "Incomplete or inconsistent forwarded result")
            return
        result_bytes = b"".join(chunk for chunk in assembly.chunks if chunk is not None)
        if (
            len(result_bytes) != assembly.byte_length
            or hashlib.sha256(result_bytes).hexdigest() != assembly.sha256
        ):
            self._protocol_failure(call_id, "Forwarded result digest mismatch")
            return
        try:
            result = json.loads(result_bytes)
        except Exception:
            self._protocol_failure(call_id, "Invalid canonical JSON forwarded result")
            return
        if not isinstance(result, dict):
            self._protocol_failure(call_id, "Forwarded result must be an object")
            return
        try:
            canonical = json.dumps(
                result, sort_keys=True, separators=(",", ":"), ensure_ascii=False
            ).encode("utf-8")
        except (TypeError, ValueError, UnicodeError) as exc:
            self._protocol_failure(call_id, f"Invalid canonical JSON forwarded result: {exc}")
            return
        if canonical != result_bytes:
            self._protocol_failure(call_id, "Non-canonical JSON forwarded result")
            return
        self._clear_assembly(pending)
        if not pending.future.done():
            pending.future.set_result(result)

    async def _ensure_open(self) -> None:
        if self._closing:
            raise ForwardedDeliveryError("Controller bridge is closing", "not_sent")
        if self._connected and self._ws is not None:
            return
        async with self._connect_lock:
            if self._closing:
                raise ForwardedDeliveryError("Controller bridge is closing", "not_sent")
            if self._connected and self._ws is not None:
                return
            token = self._auth.sign_controller_jwt(self._requester_owner_id, 30)
            ws = await connect(
                _bridge_url(self.owner_internal_url),
                max_size=BRIDGE_MAX_FRAME_BYTES,
                open_timeout=10,
                close_timeout=5,
            )
            self._opening_ws = ws
            try:
                if self._closing:
                    raise ForwardedDeliveryError("Controller bridge is closing", "not_sent")
                await ws.send(
                    json.dumps(
                        {
                            "type": "open",
                            "token": token,
                            "requester_owner_id": self._requester_owner_id,
                            "executor_id": self.executor_id,
                            "target_owner_id": self.owner_id,
                            "target_epoch": self.epoch,
                            "protocol_version": BRIDGE_PROTOCOL_VERSION,
                            "capabilities": list(BRIDGE_CAPABILITIES),
                        }
                    )
                )
                response = json.loads(await asyncio.wait_for(ws.recv(), timeout=10))
                if self._closing:
                    raise ForwardedDeliveryError("Controller bridge is closing", "not_sent")
                if (
                    response.get("type") != "opened"
                    or response.get("owner_id") != self.owner_id
                    or int(response.get("epoch") or 0) != self.epoch
                ):
                    raise ForwardedDeliveryError(
                        str(response.get("message") or "Bridge open rejected"),
                        "not_sent",
                    )
                server_version = response.get("protocol_version")
                server_capabilities = response.get("capabilities")
                negotiated = response.get("negotiated_capabilities")
                if not isinstance(server_version, int) or isinstance(server_version, bool):
                    server_version = 0
                negotiated_capabilities = {
                    capability
                    for capability, minimum_version in (
                        ("result_chunks_v1", 1),
                        ("program_rpc_v1", 2),
                    )
                    if server_version >= minimum_version
                    and isinstance(server_capabilities, list)
                    and capability in server_capabilities
                    and isinstance(negotiated, list)
                    and capability in negotiated
                }
                if self._closing:
                    raise ForwardedDeliveryError("Controller bridge is closing", "not_sent")
                self._negotiated_capabilities = negotiated_capabilities
                instance_id = response.get("executor_instance_id")
                self.executor_instance_id = instance_id if isinstance(instance_id, str) else None
                self._opening_ws = None
                self._ws = ws
                self._connected = True
                self._receiver = asyncio.create_task(
                    self._receive_loop(),
                    name=f"forwarded-executor-{self.executor_id}-{self.epoch}",
                )
            except BaseException:
                if self._opening_ws is ws:
                    self._opening_ws = None
                await asyncio.shield(ws.close())
                raise

    async def prepare_program_runtime(self) -> bool:
        """Open and verify the reverse-RPC bridge before a program is dispatched."""
        await self._ensure_open()
        return (
            "program_rpc_v1" in self._negotiated_capabilities
            and isinstance(self.executor_instance_id, str)
            and bool(self.executor_instance_id)
        )

    async def send_program_status(self, params: dict[str, Any]) -> bool:
        """Relay one live approval status update to the physical executor owner."""
        program_id = params.get("program_id")
        bridge_call_id = (
            self._tool_bridge_ids.get(program_id) if isinstance(program_id, str) else None
        )
        pending = self._pending.get(bridge_call_id) if bridge_call_id else None
        ws = self._ws
        binding_key = program_rpc_key_from_params(
            {
                **params,
                "executor_id": self.executor_id,
                "executor_instance_id": self.executor_instance_id,
            }
        )
        binding = self._program_tool_call_bindings.get(binding_key) if binding_key else None
        if (
            pending is None
            or not pending.submitted
            or "program_rpc_v1" not in self._negotiated_capabilities
            or not self._connected
            or ws is None
            or binding is None
            or (
                binding.expected_generation is not None
                and params.get("kernel_generation") != binding.expected_generation
            )
        ):
            return False
        await self._send_on_connection(
            ws, {"type": "program_status", "call_id": bridge_call_id, "params": params}
        )
        return True

    @staticmethod
    def _encode_frame(frame: dict[str, Any]) -> str:
        encoded = json.dumps(frame, separators=(",", ":")).encode("utf-8")
        if len(encoded) > BRIDGE_MAX_FRAME_BYTES:
            raise ForwardedDeliveryError("Bridge frame exceeds maximum size", "not_sent")
        return encoded.decode("utf-8")

    async def _send(
        self,
        frame: dict[str, Any],
        *,
        before_send: Callable[[], Awaitable[None]] | None = None,
        on_sent: Callable[[], None] | None = None,
    ) -> None:
        encoded = self._encode_frame(frame)
        await self._ensure_open()
        if self._ws is None:
            raise ForwardedDeliveryError("Bridge is unavailable", "not_sent")
        async with self._send_lock:
            # Check ownership after connection and queue waits, at physical dispatch.
            if before_send is not None:
                await before_send()
            await self._ws.send(encoded)
            if on_sent is not None:
                on_sent()

    async def _send_on_connection(self, ws: ClientConnection, frame: dict[str, Any]) -> None:
        """Send a reverse-RPC reply only on the socket that delivered its request."""
        encoded = self._encode_frame(frame)
        async with self._send_lock:
            if not self._connected or self._ws is not ws:
                raise ForwardedDeliveryError(
                    "The bridge carrying this program call is no longer live.",
                    "accepted_unknown",
                    executor_id=self.executor_id,
                    owner_id=self.owner_id,
                    epoch=self.epoch,
                    executor_instance_id=self.executor_instance_id,
                )
            await ws.send(encoded)

    async def _receive_loop(self) -> None:
        termination_reason = "event_queue_overflow"
        try:
            receiver_ws = self._ws
            assert receiver_ws is not None
            async for raw in receiver_ws:
                frame = json.loads(raw)
                call_id = str(frame.get("call_id") or "")
                frame_type = frame.get("type")
                if frame_type == "program_tool_call":
                    request_id = str(frame.get("request_id") or "")
                    params = frame.get("params")
                    if (
                        "program_rpc_v1" not in self._negotiated_capabilities
                        or not call_id
                        or not request_id
                        or not isinstance(params, dict)
                    ):
                        continue
                    if len(self._program_tool_call_tasks) >= BRIDGE_MAX_PROGRAM_RPCS:
                        key = program_rpc_key_from_params(params)
                        await self._send_on_connection(
                            receiver_ws,
                            {
                                "type": "program_tool_call_result",
                                "call_id": call_id,
                                "request_id": request_id,
                                "binding_key": list(key) if key is not None else [],
                                "sequence": params.get("sequence"),
                                "result": {
                                    "text": "Nested program bridge capacity is exhausted.",
                                    "is_error": True,
                                    "outcome_certainty": "known",
                                },
                            },
                        )
                        continue
                    task = asyncio.create_task(
                        self._answer_program_tool_call(receiver_ws, call_id, request_id, params),
                        name=f"forwarded-program-rpc-{request_id}",
                    )
                    self._program_tool_call_tasks[request_id] = task
                    continue
                if frame_type == "program_tool_call_cancel":
                    request_id = str(frame.get("request_id") or "")
                    program_task = self._program_tool_call_tasks.get(request_id)
                    if program_task is not None and not program_task.done():
                        program_task.cancel()
                    continue
                pending = self._pending.get(call_id)
                if pending is None:
                    continue
                if frame_type == "accepted":
                    pending.accepted = True
                    pending.accepted_event.set()
                elif frame_type == "event":
                    try:
                        pending.events.put_nowait(frame)
                    except asyncio.QueueFull:
                        if not pending.future.done():
                            pending.future.set_exception(
                                ForwardedDeliveryError(
                                    "Forwarded event queue overflow",
                                    "accepted_unknown",
                                    executor_id=self.executor_id,
                                    owner_id=self.owner_id,
                                    epoch=self.epoch,
                                    executor_instance_id=self.executor_instance_id,
                                )
                            )
                        self._pop_pending(call_id)
                        self._schedule_cancel(call_id)
                        continue
                elif frame_type == "result":
                    if pending.assembly is not None:
                        self._protocol_failure(call_id, "Unexpected unchunked result")
                        continue
                    result = frame.get("result")
                    if not isinstance(result, dict):
                        self._protocol_failure(call_id, "Forwarded result must be an object")
                    elif not pending.future.done():
                        pending.future.set_result(result)
                elif frame_type == "result_start":
                    if "result_chunks_v1" not in self._negotiated_capabilities:
                        self._protocol_failure(call_id, "Unnegotiated forwarded result chunks")
                    else:
                        self._accept_result_start(call_id, frame)
                elif frame_type == "result_chunk":
                    if "result_chunks_v1" not in self._negotiated_capabilities:
                        self._protocol_failure(call_id, "Unnegotiated forwarded result chunks")
                    else:
                        self._accept_result_chunk(call_id, frame)
                elif frame_type == "result_end":
                    if "result_chunks_v1" not in self._negotiated_capabilities:
                        self._protocol_failure(call_id, "Unnegotiated forwarded result chunks")
                    else:
                        self._accept_result_end(call_id, frame)
                elif frame_type == "error" and not pending.future.done():
                    self._pop_pending(call_id)
                    pending.future.set_exception(
                        ForwardedDeliveryError(
                            str(frame.get("message") or "Forwarded call failed"),
                            str(frame.get("delivery_state") or "accepted_unknown"),
                            executor_id=str(frame.get("executor_id") or self.executor_id),
                            generation=frame.get("generation")
                            if isinstance(frame.get("generation"), int)
                            else None,
                            owner_id=str(frame.get("owner_id") or self.owner_id),
                            epoch=frame.get("epoch")
                            if isinstance(frame.get("epoch"), int)
                            else self.epoch,
                            code=str(frame.get("code") or "executor_delivery_failure"),
                            retry_after=float(frame["retry_after"])
                            if isinstance(frame.get("retry_after"), (int, float))
                            else None,
                            executor_instance_id=frame.get("executor_instance_id")
                            if isinstance(frame.get("executor_instance_id"), str)
                            else (self.executor_instance_id if pending.accepted else None),
                        )
                    )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            termination_reason = type(exc).__name__
        else:
            termination_reason = "peer_closed"
        finally:
            current_task = asyncio.current_task()
            program_tasks = [
                task
                for task in self._program_tool_call_tasks.values()
                if task is not current_task and not task.done()
            ]
            for task in program_tasks:
                task.cancel()
            if program_tasks:
                await asyncio.gather(*program_tasks, return_exceptions=True)
            self._program_tool_call_tasks.clear()
            abandoned = tuple(self._pending)
            if abandoned:
                _logger.warning(
                    "bridge client: receive loop ended with in-flight calls",
                    extra={
                        "extra_data": {
                            "executor_id": self.executor_id,
                            "owner_id": self.owner_id,
                            "epoch": self.epoch,
                            "in_flight_calls": len(abandoned),
                            "reason": termination_reason,
                        }
                    },
                )
            for call_id in abandoned:
                pending = self._pop_pending(call_id)
                if pending is None:
                    continue
                if not pending.future.done():
                    pending.future.set_exception(
                        ForwardedDeliveryError(
                            "Controller bridge disconnected",
                            "accepted_unknown" if pending.submitted else "not_sent",
                            executor_id=self.executor_id,
                            owner_id=self.owner_id,
                            epoch=self.epoch,
                            executor_instance_id=(
                                self.executor_instance_id if pending.accepted else None
                            ),
                        )
                    )
            if self._ws is receiver_ws:
                self._connected = False

    def _capacity_error(self, reason: str) -> ForwardedDeliveryError:
        _logger.warning(
            "bridge client: admission capacity exhausted",
            extra={
                "extra_data": {
                    "executor_id": self.executor_id,
                    "owner_id": self.owner_id,
                    "epoch": self.epoch,
                    "reason": reason,
                    "in_flight_calls": len(self._pending),
                    "queued_calls": len(self._admission_waiters),
                }
            },
        )
        return ForwardedDeliveryError(
            "Executor bridge capacity exhausted",
            "not_sent",
            code="executor_bridge_capacity",
            executor_id=self.executor_id,
            owner_id=self.owner_id,
            epoch=self.epoch,
            retry_after=0.1,
        )

    def _check_admission_open(self) -> None:
        if self._closing or self._retiring:
            raise ForwardedDeliveryError(
                "Controller bridge is closed or retired",
                "not_sent",
                executor_id=self.executor_id,
                owner_id=self.owner_id,
                epoch=self.epoch,
            )

    async def _wait_for_capacity(self) -> None:
        self._check_admission_open()
        if len(self._pending) < BRIDGE_MAX_CALLS and not self._admission_waiters:
            return
        if len(self._admission_waiters) >= BRIDGE_MAX_QUEUED_CALLS:
            raise self._capacity_error("queue_full")
        waiter = object()
        self._admission_waiters.append(waiter)
        try:
            async with asyncio.timeout(BRIDGE_QUEUE_TIMEOUT_SECONDS):
                while True:
                    self._check_admission_open()
                    if (
                        self._admission_waiters[0] is waiter
                        and len(self._pending) < BRIDGE_MAX_CALLS
                    ):
                        return
                    self._admission_changed.clear()
                    await self._admission_changed.wait()
        except TimeoutError as exc:
            raise self._capacity_error("queue_timeout") from exc
        finally:
            self._admission_waiters.remove(waiter)
            self._admission_changed.set()

    async def _start_call(
        self,
        operation: str,
        payload: dict[str, Any],
        *,
        timeout: float,
        before_send: Any | None = None,
    ) -> tuple[str, _PendingCall]:
        await self._wait_for_capacity()
        # Admission and reservation have no intervening await, so a newly
        # arriving call cannot take the slot granted to the FIFO head.
        call_id = uuid.uuid4().hex
        pending = _PendingCall(future=asyncio.get_running_loop().create_future())
        self._pending[call_id] = pending
        try:
            await self._ensure_open()
        except asyncio.CancelledError:
            # No call frame has been submitted. Release admission even when a
            # caller times out while waiting for the shared connection lock.
            self._pop_pending(call_id)
            raise
        except Exception as exc:
            self._pop_pending(call_id)
            if isinstance(exc, ForwardedDeliveryError):
                raise
            raise ForwardedDeliveryError(str(exc), "not_sent") from exc
        before_send_failed = False

        async def check_send_fence() -> None:
            nonlocal before_send_failed
            if before_send is None:
                return
            try:
                await before_send(self.executor_id, self.executor_instance_id)
            except (asyncio.CancelledError, Exception):
                before_send_failed = True
                raise

        call_frame = {
            "type": "call",
            "call_id": call_id,
            "operation": operation,
            "payload": payload,
            "timeout_seconds": timeout,
        }
        try:
            self._encode_frame(call_frame)
            await self._send(
                call_frame,
                before_send=check_send_fence if before_send is not None else None,
                on_sent=lambda: setattr(pending, "submitted", True),
            )
        except asyncio.CancelledError:
            if not before_send_failed:
                cancel_task = asyncio.create_task(self._cancel(call_id))
                try:
                    await asyncio.wait_for(asyncio.shield(cancel_task), timeout=1.0)
                except Exception:
                    cancel_task.cancel()
                    await asyncio.gather(cancel_task, return_exceptions=True)
            self._pop_pending(call_id)
            raise
        except Exception as exc:
            self._pop_pending(call_id)
            # Guard rejection is not an ambiguous socket submission.
            if before_send_failed:
                raise
            if isinstance(exc, ForwardedDeliveryError) and exc.delivery_state == "not_sent":
                raise
            raise ForwardedDeliveryError(
                "Bridge call submission outcome is unknown",
                "not_sent" if _bridge_write_was_rejected(exc) else "accepted_unknown",
                executor_id=self.executor_id,
                owner_id=self.owner_id,
                epoch=self.epoch,
                executor_instance_id=(None),
            ) from exc
        return call_id, pending

    async def _cancel(self, call_id: str) -> None:
        from cognis.providers.executor.delivery import preserve_task_execution_on_interrupt

        if await preserve_task_execution_on_interrupt():
            return
        if self._closing:
            return
        with contextlib.suppress(Exception):
            encoded = self._encode_frame({"type": "cancel", "call_id": call_id})
            if self._ws is None or not self._connected:
                return
            ws = self._ws
            async with self._send_lock:
                if self._closing or ws is not self._ws or not self._connected:
                    return
                await ws.send(encoded)

    async def rpc_call(
        self,
        method: str,
        params: dict[str, Any],
        timeout: float | None = None,
        *,
        before_send: Any | None = None,
        stable_call_id: str | None = None,
        replay_safe: bool = False,
        _retry_attempted: bool = False,
    ) -> dict[str, Any]:
        if replay_safe:
            from cognis.executor.unary_dedup import is_replay_safe_unary_method

            if not is_replay_safe_unary_method(method):
                raise ValueError("Replay-safe unary RPC requires an allowlisted method")
            stable_call_id = stable_call_id or uuid.uuid4().hex
        effective_timeout = timeout or 300.0
        call_id, pending = await self._start_call(
            "rpc",
            {
                "method": method,
                "params": params,
                "stable_call_id": stable_call_id,
                "replay_safe": replay_safe,
            },
            timeout=effective_timeout,
            before_send=before_send,
        )
        try:
            return await asyncio.wait_for(pending.future, timeout=effective_timeout + 1)
        except ForwardedDeliveryError as exc:
            if (
                replay_safe
                and stable_call_id
                and not _retry_attempted
                and exc.delivery_state == "accepted_unknown"
            ):
                return await self.rpc_call(
                    method,
                    params,
                    timeout,
                    before_send=before_send,
                    stable_call_id=stable_call_id,
                    replay_safe=True,
                    _retry_attempted=True,
                )
            raise
        except TimeoutError as exc:
            await self._cancel(call_id)
            error = ForwardedDeliveryError(
                "Forwarded RPC timed out",
                "accepted_unknown" if pending.submitted else "not_sent",
                executor_id=self.executor_id,
                owner_id=self.owner_id,
                epoch=self.epoch,
                executor_instance_id=(self.executor_instance_id if pending.accepted else None),
            )
            if (
                replay_safe
                and stable_call_id
                and not _retry_attempted
                and error.delivery_state == "accepted_unknown"
            ):
                return await self.rpc_call(
                    method,
                    params,
                    timeout,
                    before_send=before_send,
                    stable_call_id=stable_call_id,
                    replay_safe=True,
                    _retry_attempted=True,
                )
            raise error from exc
        except asyncio.CancelledError:
            await self._cancel(call_id)
            raise
        finally:
            self._pop_pending(call_id)

    async def list_tools(self) -> list[dict[str, Any]]:
        result = await self.rpc_call(
            "tool.list",
            {},
            replay_safe=True,
        )
        return list(result.get("tools") or [])

    async def fetch_tool_result(
        self, call_id: str, *, timeout: float = 30.0, scope: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """Forward an outcome lookup so HA turns can reconcile after reconnects.

        The owner controller relays this to the executor, whose reply carries the
        authoritative ``executor_instance_id`` the caller must verify.
        """

        result = await self.rpc_call(
            "tool.result_fetch",
            {"call_id": call_id, **({"scope": scope} if scope is not None else {})},
            timeout=timeout,
            replay_safe=True,
        )
        return result if isinstance(result, dict) else {}

    async def tool_execute(
        self,
        tool_call: ToolCall,
        timeout_seconds: int | None = None,
        output_chunk_callback: Any | None = None,
        before_send: Any | None = None,
        on_sent: Any | None = None,
    ) -> ToolResult:
        timeout = float(timeout_seconds or 300)
        if tool_call.name == "python_execute":
            if not await self.prepare_program_runtime():
                return ToolResult(
                    output="The selected executor bridge does not support governed program RPC.",
                    is_error=True,
                    metadata={"code": "program_rpc_unavailable"},
                )
            metadata = tool_call.runtime_metadata
            binding_data = metadata.get("program_runtime") if isinstance(metadata, dict) else None
            try:
                binding = ProgramRuntimeBinding.model_validate(binding_data)
            except Exception:
                return ToolResult(
                    output="Program runtime identity is missing or invalid.",
                    is_error=True,
                    metadata={"code": "program_runtime_context_invalid"},
                )
            if (
                binding.executor_id != self.executor_id
                or binding.executor_instance_id != self.executor_instance_id
                or binding.program_id != tool_call.call_id
            ):
                return ToolResult(
                    output="Program runtime is not bound to this executor process and call.",
                    is_error=True,
                    metadata={"code": "program_runtime_context_invalid"},
                )
        call_id, pending = await self._start_call(
            "tool",
            {"tool_call": tool_call.model_dump(mode="json")},
            timeout=timeout,
            before_send=before_send,
        )
        self._tool_bridge_ids[tool_call.call_id] = call_id
        accepted_task: asyncio.Task[bool] | None = None
        try:
            if on_sent is not None:
                accepted_task = asyncio.create_task(pending.accepted_event.wait())
                accepted_done, _ = await asyncio.wait(
                    cast(set[asyncio.Future[Any]], {accepted_task, pending.future}),
                    timeout=timeout + 1,
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if accepted_task in accepted_done:
                    await on_sent(self.executor_id, self.executor_instance_id)
                elif pending.future in accepted_done:
                    accepted_task.cancel()
                    await asyncio.gather(accepted_task, return_exceptions=True)
                    # Propagate an early bridge error/result before entering the
                    # normal progress loop. No physical acceptance was confirmed.
                    return ToolResult.model_validate(await pending.future)
                else:
                    await self._cancel(call_id)
                    raise ForwardedDeliveryError(
                        "Forwarded tool acceptance timed out",
                        "accepted_unknown" if pending.submitted else "not_sent",
                        executor_id=self.executor_id,
                        owner_id=self.owner_id,
                        epoch=self.epoch,
                        executor_instance_id=None,
                    )
            while not pending.future.done() or not pending.events.empty():
                event_task = asyncio.create_task(pending.events.get())
                event_done, _ = await asyncio.wait(
                    cast(set[asyncio.Future[Any]], {pending.future, event_task}),
                    timeout=timeout + 1,
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if not event_done:
                    event_task.cancel()
                    await asyncio.gather(event_task, return_exceptions=True)
                    await self._cancel(call_id)
                    raise ForwardedDeliveryError(
                        "Forwarded tool timed out",
                        "accepted_unknown" if pending.submitted else "not_sent",
                        executor_id=self.executor_id,
                        owner_id=self.owner_id,
                        epoch=self.epoch,
                        executor_instance_id=(
                            self.executor_instance_id if pending.accepted else None
                        ),
                    )
                if event_task in event_done:
                    event = event_task.result()
                    if output_chunk_callback is not None:
                        payload = event.get("payload") or {}
                        with contextlib.suppress(Exception):
                            await output_chunk_callback(
                                payload.get("chunk", ""), payload.get("stream")
                            )
                else:
                    event_task.cancel()
            return ToolResult.model_validate(await pending.future)
        except asyncio.CancelledError:
            await self._cancel(call_id)
            raise
        finally:
            if accepted_task is not None and not accepted_task.done():
                accepted_task.cancel()
                await asyncio.gather(accepted_task, return_exceptions=True)
            self._pop_pending(call_id)
            self._tool_bridge_ids.pop(tool_call.call_id, None)

    async def cancel_call(self, call_id: str) -> None:
        bridge_id = self._tool_bridge_ids.get(call_id)
        pending = self._pending.get(bridge_id) if bridge_id is not None else None
        if bridge_id is not None:
            await self._cancel(bridge_id)
        else:
            # A replacement bridge has no requester-local bridge ID. Cancel by
            # the stable executor call ID through the physical connection.
            with contextlib.suppress(Exception):
                await self.rpc_call("tool.cancel", {"call_id": call_id}, timeout=10.0)
        if pending is not None and not pending.future.done():
            pending.future.cancel()

    async def request_background(
        self, call_id: str, *, timeout: float = TOOL_BACKGROUND_RPC_TIMEOUT_SECONDS
    ) -> ToolBackgroundRequestOutcome:
        """Ask the owning replica's executor to move a running tool call to background."""

        result = await self.rpc_call("tool.background", {"call_id": call_id}, timeout=timeout)
        return parse_tool_background_rpc_result(result)

    async def llm_complete_stream(
        self,
        request_id: str,
        messages: list[dict[str, Any]],
        model: str,
        **kwargs: Any,
    ) -> AsyncIterator[dict[str, Any]]:
        call_id, pending = await self._start_call(
            "inference",
            {
                "request_id": request_id,
                "messages": messages,
                "model": model,
                "kwargs": kwargs,
            },
            timeout=300.0,
        )
        emitted = False
        try:
            while not pending.future.done() or not pending.events.empty():
                if not pending.events.empty():
                    event = pending.events.get_nowait()
                    emitted = True
                    yield dict(event.get("payload") or {})
                    continue
                event_task = asyncio.create_task(pending.events.get())
                done, _ = await asyncio.wait(
                    {pending.future, event_task},
                    timeout=301,
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if not done:
                    event_task.cancel()
                    await asyncio.gather(event_task, return_exceptions=True)
                    await self._cancel(call_id)
                    yield {
                        "error": "Forwarded inference timed out",
                        "mid_stream_failure": emitted,
                    }
                    return
                if event_task in done:
                    emitted = True
                    yield dict(event_task.result().get("payload") or {})
                else:
                    event_task.cancel()
            await pending.future
        except ForwardedDeliveryError as exc:
            yield {
                "error": str(exc),
                "delivery_state": exc.delivery_state,
                "mid_stream_failure": emitted,
            }
        finally:
            if not pending.future.done():
                await self._cancel(call_id)
            self._pop_pending(call_id)

    async def close(self) -> None:
        self._closing = True
        self._admission_changed.set()
        drain_task = self._drain_task
        self._drain_task = None
        if drain_task is not None and drain_task is not asyncio.current_task():
            drain_task.cancel()
        receiver = self._receiver
        if receiver is not None and receiver is not asyncio.current_task():
            receiver.cancel()
        ws = self._ws
        self._ws = None
        opening_ws = self._opening_ws
        self._opening_ws = None
        self._connected = False
        if self._pending:
            _logger.warning(
                "bridge client: closing proxy with in-flight calls",
                extra={
                    "extra_data": {
                        "executor_id": self.executor_id,
                        "owner_id": self.owner_id,
                        "epoch": self.epoch,
                        "in_flight_calls": len(self._pending),
                    }
                },
            )
        for call_id in tuple(self._pending):
            pending = self._pop_pending(call_id)
            if pending is None:
                continue
            if not pending.future.done():
                pending.future.set_exception(
                    ForwardedDeliveryError(
                        "Controller bridge closed",
                        "accepted_unknown" if pending.submitted else "not_sent",
                        executor_id=self.executor_id,
                        owner_id=self.owner_id,
                        epoch=self.epoch,
                        executor_instance_id=(
                            self.executor_instance_id if pending.accepted else None
                        ),
                    )
                )
        self._tool_bridge_ids.clear()
        if ws is not None:
            await ws.close()
        if opening_ws is not None and opening_ws is not ws:
            await opening_ws.close()
        if receiver is not None and receiver is not asyncio.current_task():
            await asyncio.gather(receiver, return_exceptions=True)
        if drain_task is not None and drain_task is not asyncio.current_task():
            await asyncio.gather(drain_task, return_exceptions=True)
        if self._cancel_tasks:
            try:
                await asyncio.wait_for(
                    asyncio.gather(*self._cancel_tasks, return_exceptions=True),
                    timeout=1.0,
                )
            except TimeoutError:
                for task in tuple(self._cancel_tasks):
                    task.cancel()

    async def background_shell_status(self, *, include_completed: bool = False) -> dict[str, Any]:
        """Share concurrent status reads while this proxy accepts new calls."""
        self._check_admission_open()
        return await self._shell_status_reads.read(include_completed=include_completed)

    async def _fetch_background_shell_status(self, include_completed: bool) -> dict[str, Any]:
        from cognis.providers.executor.shell_status import bind_shell_status

        result = await self.rpc_call(
            "shell.background_status",
            {"include_completed": include_completed},
            replay_safe=True,
        )
        return bind_shell_status(result, self.executor_id)

    async def file_digests(self, *, paths: list[str]) -> dict[str, Any]:
        """Read-only SHA-256 of regular files on the forwarded executor."""
        self._check_admission_open()
        return await self.rpc_call("fs.digest", {"paths": list(paths)}, replay_safe=True)

    async def lsp_status(self) -> dict[str, Any]:
        return await self.rpc_call(
            "lsp.status",
            {},
            replay_safe=True,
        )

    async def isolated_rpc_call(
        self,
        method: str,
        params: dict[str, Any],
        *,
        timeout: float,
    ) -> dict[str, Any]:
        return await self.rpc_call(method, params, timeout=timeout)

    async def llm_discover_models(
        self,
        *,
        preset: str,
        base_url: str,
        api_key: str = "",
        provider_id: str | None = None,
        owner_email: str | None = None,
        aws_config: dict[str, str] | None = None,
    ) -> list[dict[str, Any]]:
        result = await self.rpc_call(
            "llm.discover_models",
            {
                "preset": preset,
                "base_url": base_url,
                "api_key": api_key,
                "provider_id": provider_id,
                "owner_email": owner_email,
                **({"aws_config": aws_config} if aws_config else {}),
            },
            timeout=30,
        )
        return [dict(item) for item in result.get("models", []) if isinstance(item, dict)]

    async def local_model_status(self) -> OllamaRuntimeStatus:
        return OllamaRuntimeStatus.model_validate(
            await self.rpc_call(
                "local_model.status",
                {},
                timeout=30,
                replay_safe=True,
            )
        )

    async def local_model_show(self, runtime_name: str) -> dict[str, Any]:
        return await self.rpc_call(
            "local_model.show",
            {"runtime_name": runtime_name},
            timeout=30,
            replay_safe=True,
        )

    async def local_model_operation_start(
        self, request: OllamaRuntimeStartRequest
    ) -> OllamaRuntimeOperationStatus:
        return OllamaRuntimeOperationStatus.model_validate(
            await self.rpc_call(
                "local_model.operation.start",
                request.model_dump(mode="json"),
                timeout=30,
            )
        )

    async def local_model_operation_status(self, operation_id: str) -> OllamaRuntimeOperationStatus:
        return OllamaRuntimeOperationStatus.model_validate(
            await self.rpc_call(
                "local_model.operation.status",
                {"operation_id": operation_id},
                timeout=15,
            )
        )

    async def local_model_operation_cancel(self, operation_id: str) -> dict[str, Any]:
        return await self.rpc_call(
            "local_model.operation.cancel",
            {"operation_id": operation_id},
            timeout=30,
        )

    async def oauth_loopback_start(self, **params: Any) -> dict[str, Any]:
        return await self.rpc_call("oauth.loopback_start", params)

    async def oauth_loopback_stop(self, *, listener_id: str) -> dict[str, Any]:
        return await self.rpc_call("oauth.loopback_stop", {"listener_id": listener_id})
