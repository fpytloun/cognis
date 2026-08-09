"""Controller-to-controller forwarded executor connection."""

from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
import json
import math
import uuid
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from websockets.asyncio.client import ClientConnection, connect

from cognis.models.local_models import (
    OllamaRuntimeOperationStatus,
    OllamaRuntimeStartRequest,
    OllamaRuntimeStatus,
)
from cognis.models.tool import ExecutorCapabilities, ToolCall, ToolResult
from cognis.providers.executor.delivery import DeliveryState, ExecutorDeliveryError

BRIDGE_EVENT_QUEUE_SIZE = 128
BRIDGE_MAX_PENDING_CALLS = 32
BRIDGE_PROTOCOL_VERSION = 1
BRIDGE_CAPABILITIES = ("result_chunks_v1",)
BRIDGE_MAX_FRAME_BYTES = 1024 * 1024
BRIDGE_RESULT_MAX_BYTES = 32 * 1024 * 1024
BRIDGE_RESULT_AGGREGATE_MAX_BYTES = 64 * 1024 * 1024
BRIDGE_RESULT_CHUNK_BYTES = 180 * 1024
BRIDGE_RESULT_ASSEMBLY_TIMEOUT_SECONDS = 30.0
BRIDGE_MAX_RESULT_CHUNKS = math.ceil(BRIDGE_RESULT_MAX_BYTES / BRIDGE_RESULT_CHUNK_BYTES)
BRIDGE_MAX_ACTIVE_ASSEMBLIES = 32


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
        )


@dataclass(slots=True)
class _PendingCall:
    future: asyncio.Future[dict[str, Any]]
    events: asyncio.Queue[dict[str, Any]] = field(
        default_factory=lambda: asyncio.Queue(maxsize=BRIDGE_EVENT_QUEUE_SIZE)
    )
    accepted: bool = False
    submitted: bool = False
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
        self._tool_bridge_ids: dict[str, str] = {}
        self._connected = False
        self._negotiated_capabilities: set[str] = set()
        self._assembly_bytes = 0
        self._active_assemblies = 0
        self._cancel_tasks: set[asyncio.Task[None]] = set()
        self._closing = False

    @property
    def connected(self) -> bool:
        return self._connected

    @property
    def closing(self) -> bool:
        return self._closing

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
                negotiated_capabilities = (
                    {"result_chunks_v1"}
                    if server_version >= BRIDGE_PROTOCOL_VERSION
                    and isinstance(server_capabilities, list)
                    and "result_chunks_v1" in server_capabilities
                    and isinstance(negotiated, list)
                    and "result_chunks_v1" in negotiated
                    else set()
                )
                if self._closing:
                    raise ForwardedDeliveryError("Controller bridge is closing", "not_sent")
                self._negotiated_capabilities = negotiated_capabilities
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
        on_send_attempt: Callable[[], None] | None = None,
    ) -> None:
        encoded = self._encode_frame(frame)
        await self._ensure_open()
        if self._ws is None:
            raise ForwardedDeliveryError("Bridge is unavailable", "not_sent")
        async with self._send_lock:
            if on_send_attempt is not None:
                on_send_attempt()
            await self._ws.send(encoded)

    async def _receive_loop(self) -> None:
        try:
            assert self._ws is not None
            async for raw in self._ws:
                frame = json.loads(raw)
                call_id = str(frame.get("call_id") or "")
                pending = self._pending.get(call_id)
                if pending is None:
                    continue
                frame_type = frame.get("type")
                if frame_type == "accepted":
                    pending.accepted = True
                elif frame_type == "event":
                    try:
                        pending.events.put_nowait(frame)
                    except asyncio.QueueFull:
                        if not pending.future.done():
                            pending.future.set_exception(
                                ForwardedDeliveryError(
                                    "Forwarded event queue overflow",
                                    "accepted_unknown",
                                )
                            )
                        await self.close()
                        return
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
                        )
                    )
        except asyncio.CancelledError:
            raise
        except Exception:
            pass
        finally:
            self._connected = False
            for call_id in tuple(self._pending):
                pending = self._pop_pending(call_id)
                if pending is None:
                    continue
                if not pending.future.done():
                    pending.future.set_exception(
                        ForwardedDeliveryError(
                            "Controller bridge disconnected",
                            "accepted_unknown" if pending.submitted else "not_sent",
                        )
                    )

    async def _start_call(
        self,
        operation: str,
        payload: dict[str, Any],
        *,
        timeout: float,
    ) -> tuple[str, _PendingCall]:
        if len(self._pending) >= BRIDGE_MAX_PENDING_CALLS:
            raise ForwardedDeliveryError("Forwarded call limit exceeded", "not_sent")
        call_id = uuid.uuid4().hex
        pending = _PendingCall(future=asyncio.get_running_loop().create_future())
        self._pending[call_id] = pending
        try:
            await self._ensure_open()
        except Exception as exc:
            self._pop_pending(call_id)
            if isinstance(exc, ForwardedDeliveryError):
                raise
            raise ForwardedDeliveryError(str(exc), "not_sent") from exc
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
                on_send_attempt=lambda: setattr(pending, "submitted", True),
            )
        except asyncio.CancelledError:
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
            if isinstance(exc, ForwardedDeliveryError) and exc.delivery_state == "not_sent":
                raise
            raise ForwardedDeliveryError(
                "Bridge call submission outcome is unknown",
                "accepted_unknown",
            ) from exc
        return call_id, pending

    async def _cancel(self, call_id: str) -> None:
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

    async def tool_execute(
        self,
        tool_call: ToolCall,
        timeout_seconds: int | None = None,
        output_chunk_callback: Any | None = None,
    ) -> ToolResult:
        timeout = float(timeout_seconds or 300)
        call_id, pending = await self._start_call(
            "tool",
            {"tool_call": tool_call.model_dump(mode="json")},
            timeout=timeout,
        )
        self._tool_bridge_ids[tool_call.call_id] = call_id
        try:
            while not pending.future.done() or not pending.events.empty():
                event_task = asyncio.create_task(pending.events.get())
                done, _ = await asyncio.wait(
                    {pending.future, event_task},
                    timeout=timeout + 1,
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if not done:
                    event_task.cancel()
                    await asyncio.gather(event_task, return_exceptions=True)
                    await self._cancel(call_id)
                    raise ForwardedDeliveryError(
                        "Forwarded tool timed out",
                        "accepted_unknown" if pending.submitted else "not_sent",
                    )
                if event_task in done:
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
            self._pop_pending(call_id)
            self._tool_bridge_ids.pop(tool_call.call_id, None)

    async def cancel_call(self, call_id: str) -> None:
        bridge_id = self._tool_bridge_ids.get(call_id)
        pending = self._pending.get(bridge_id) if bridge_id is not None else None
        if bridge_id is not None:
            await self._cancel(bridge_id)
        if pending is not None and not pending.future.done():
            pending.future.cancel()

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
        receiver = self._receiver
        if receiver is not None and receiver is not asyncio.current_task():
            receiver.cancel()
        ws = self._ws
        self._ws = None
        opening_ws = self._opening_ws
        self._opening_ws = None
        self._connected = False
        for call_id in tuple(self._pending):
            pending = self._pop_pending(call_id)
            if pending is None:
                continue
            if not pending.future.done():
                pending.future.set_exception(
                    ForwardedDeliveryError(
                        "Controller bridge closed",
                        "accepted_unknown" if pending.submitted else "not_sent",
                    )
                )
        self._tool_bridge_ids.clear()
        if ws is not None:
            await ws.close()
        if opening_ws is not None and opening_ws is not ws:
            await opening_ws.close()
        if receiver is not None and receiver is not asyncio.current_task():
            await asyncio.gather(receiver, return_exceptions=True)
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
        return await self.rpc_call(
            "shell.background_status",
            {"include_completed": include_completed},
            replay_safe=True,
        )

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
    ) -> list[dict[str, Any]]:
        result = await self.rpc_call(
            "llm.discover_models",
            {
                "preset": preset,
                "base_url": base_url,
                "api_key": api_key,
                "provider_id": provider_id,
                "owner_email": owner_email,
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
