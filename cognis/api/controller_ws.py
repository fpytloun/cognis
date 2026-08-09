"""Authenticated controller-to-controller executor forwarding bridge."""

from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
import json
import math
from dataclasses import dataclass
from typing import Any

from starlette.websockets import WebSocket, WebSocketDisconnect

from cognis import __version__
from cognis.core.executor_connection_ownership import ExecutorConnectionOwner
from cognis.logging import get_logger
from cognis.models.tool import ToolCall
from cognis.providers.executor.delivery import ExecutorDeliveryError
from cognis.providers.executor.websocket import ExecutorRPCError

logger = get_logger(__name__)

BRIDGE_MAX_FRAME_BYTES = 1024 * 1024
BRIDGE_MAX_CALLS = 32
BRIDGE_SEND_TIMEOUT_SECONDS = 10.0
BRIDGE_MAX_TIMEOUT_SECONDS = 600.0
BRIDGE_FIRST_FRAME_TIMEOUT_SECONDS = 5.0
BRIDGE_PROTOCOL_VERSION = 1
BRIDGE_CAPABILITIES = ("result_chunks_v1",)
BRIDGE_RESULT_MAX_BYTES = 32 * 1024 * 1024
BRIDGE_RESULT_CHUNK_BYTES = 180 * 1024
BRIDGE_RESULT_FRAME_BYTES = 256 * 1024
BRIDGE_MAX_RESULT_CHUNKS = math.ceil(BRIDGE_RESULT_MAX_BYTES / BRIDGE_RESULT_CHUNK_BYTES)


class _BridgeProtocolError(ValueError):
    pass


@dataclass(slots=True)
class _BridgeCall:
    task: asyncio.Task[None]
    executor_call_id: str | None = None
    inference_request_id: str | None = None


def _bounded_frame(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError("Bridge frame must be an object")
    if len(json.dumps(value, separators=(",", ":")).encode()) > BRIDGE_MAX_FRAME_BYTES:
        raise ValueError("Bridge frame exceeds maximum size")
    return value


def _canonical_result_bytes(result: Any) -> bytes:
    if not isinstance(result, dict):
        raise _BridgeProtocolError("Bridge success result must be an object")
    encoded = json.dumps(result, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode(
        "utf-8"
    )
    if len(encoded) > BRIDGE_RESULT_MAX_BYTES:
        raise _BridgeProtocolError("Bridge result exceeds maximum size")
    return encoded


async def handle_controller_executor_websocket(websocket: WebSocket) -> None:
    """Serve one peer-controller bridge bound to one executor owner epoch."""

    await websocket.accept()
    app = websocket.app
    send_lock = asyncio.Lock()
    calls: dict[str, _BridgeCall] = {}
    connection: Any | None = None
    tearing_down = False
    negotiated_chunks = False

    async def send(frame: dict[str, Any]) -> None:
        _bounded_frame(frame)
        async with send_lock:
            async with asyncio.timeout(BRIDGE_SEND_TIMEOUT_SECONDS):
                await websocket.send_json(frame)

    try:
        open_frame = _bounded_frame(
            await asyncio.wait_for(
                websocket.receive_json(), timeout=BRIDGE_FIRST_FRAME_TIMEOUT_SECONDS
            )
        )
        if open_frame.get("type") != "open":
            raise ValueError("First bridge frame must be open")
        token = str(open_frame.get("token") or "")
        claims = app.state.providers.auth.verify_controller_jwt(token)
        requester_owner_id = str(open_frame.get("requester_owner_id") or "")
        if claims.get("sub") != requester_owner_id:
            raise PermissionError("Controller token subject mismatch")
        requester = await app.state.controller_directory.get_reachable(requester_owner_id)
        if requester is None:
            raise PermissionError("Requester controller is not live")

        executor_id = str(open_frame.get("executor_id") or "")
        target_owner_id = str(open_frame.get("target_owner_id") or "")
        target_epoch = int(open_frame.get("target_epoch") or 0)
        if target_owner_id != app.state.controller_runtime.owner_id:
            raise PermissionError("Bridge target owner mismatch")
        connection = app.state.providers.executor.websocket.get_local_connection(executor_id)
        owner: ExecutorConnectionOwner | None = (
            getattr(connection, "connection_owner", None) if connection is not None else None
        )
        if (
            connection is None
            or owner is None
            or owner.owner_id != target_owner_id
            or owner.epoch != target_epoch
            or not await app.state.executor_connection_ownership.is_current(owner)
        ):
            raise PermissionError("Bridge target owner epoch is stale")
        peer_version = open_frame.get("protocol_version")
        peer_capabilities = open_frame.get("capabilities")
        if not isinstance(peer_version, int) or isinstance(peer_version, bool):
            peer_version = 0
        if not isinstance(peer_capabilities, list):
            peer_capabilities = []
        negotiated_chunks = (
            peer_version >= BRIDGE_PROTOCOL_VERSION and "result_chunks_v1" in peer_capabilities
        )
        await send(
            {
                "type": "opened",
                "executor_id": executor_id,
                "owner_id": owner.owner_id,
                "epoch": owner.epoch,
                "server_version": __version__,
                "protocol_version": BRIDGE_PROTOCOL_VERSION,
                "capabilities": list(BRIDGE_CAPABILITIES),
                "negotiated_capabilities": (["result_chunks_v1"] if negotiated_chunks else []),
            }
        )

        async def send_result(call_id: str, result: Any) -> None:
            result_bytes = _canonical_result_bytes(result)
            small_frame = {"type": "result", "call_id": call_id, "result": result}
            if (
                len(json.dumps(small_frame, separators=(",", ":")).encode("utf-8"))
                <= BRIDGE_MAX_FRAME_BYTES
            ):
                await send(small_frame)
                return
            if not negotiated_chunks:
                raise _BridgeProtocolError(
                    "Bridge result exceeds maximum frame size without negotiated chunking"
                )
            digest = hashlib.sha256(result_bytes).hexdigest()
            chunk_count = math.ceil(len(result_bytes) / BRIDGE_RESULT_CHUNK_BYTES)
            if chunk_count > BRIDGE_MAX_RESULT_CHUNKS:
                raise _BridgeProtocolError("Bridge result chunk count exceeds maximum")
            await send(
                {
                    "type": "result_start",
                    "call_id": call_id,
                    "sequence": 0,
                    "chunk_count": chunk_count,
                    "byte_length": len(result_bytes),
                    "sha256": digest,
                    "encoding": "base64",
                    "serialization": "canonical-json-v1",
                }
            )
            for sequence in range(chunk_count):
                payload = base64.b64encode(
                    result_bytes[
                        sequence * BRIDGE_RESULT_CHUNK_BYTES : (sequence + 1)
                        * BRIDGE_RESULT_CHUNK_BYTES
                    ]
                ).decode("ascii")
                frame = {
                    "type": "result_chunk",
                    "call_id": call_id,
                    "sequence": sequence,
                    "payload": payload,
                }
                if (
                    len(json.dumps(frame, separators=(",", ":")).encode("utf-8"))
                    >= BRIDGE_RESULT_FRAME_BYTES
                ):
                    raise _BridgeProtocolError("Bridge result chunk frame exceeds safety ceiling")
                await send(frame)
            await send(
                {
                    "type": "result_end",
                    "call_id": call_id,
                    "chunk_count": chunk_count,
                    "byte_length": len(result_bytes),
                    "sha256": digest,
                }
            )

        async def run_call(frame: dict[str, Any]) -> None:
            bridge_call_id = str(frame["call_id"])
            accepted = False

            async def on_sent() -> None:
                nonlocal accepted
                accepted = True
                await send({"type": "accepted", "call_id": bridge_call_id})

            try:
                current = app.state.providers.executor.websocket.get_local_connection(executor_id)
                if (
                    current is not connection
                    or not await app.state.executor_connection_ownership.is_current(owner)
                ):
                    raise PermissionError("Bridge owner changed before physical send")
                operation = str(frame.get("operation") or "")
                payload = frame.get("payload")
                if not isinstance(payload, dict):
                    raise ValueError("Bridge call payload must be an object")
                timeout = min(
                    max(float(frame.get("timeout_seconds") or 300.0), 0.1),
                    BRIDGE_MAX_TIMEOUT_SECONDS,
                )
                if operation == "rpc":
                    method = str(payload.get("method") or "")
                    params = payload.get("params")
                    if not method or not isinstance(params, dict):
                        raise ValueError("Invalid rpc bridge payload")
                    stable_call_id = payload.get("stable_call_id")
                    replay_safe = payload.get("replay_safe") is True
                    if method == "executor.reconcile":
                        requested_executor_id = str(params.get("executor_id") or "")
                        if requested_executor_id != executor_id:
                            raise PermissionError("Bridge reconcile executor mismatch")
                        from cognis.api.executor_runtime import reconcile_executor

                        reconciled = await reconcile_executor(
                            app, executor_id, connection=connection
                        )
                        await send_result(bridge_call_id, {"reconciled": reconciled})
                        return
                    rpc_kwargs = {"timeout": timeout, "on_sent": on_sent}
                    if replay_safe:
                        rpc_kwargs.update(
                            stable_call_id=stable_call_id,
                            replay_safe=True,
                        )
                    result = await connection.rpc_call(method, params, **rpc_kwargs)
                    await send_result(bridge_call_id, result)
                elif operation == "tool":
                    tool_call = ToolCall.model_validate(payload.get("tool_call"))
                    calls[bridge_call_id].executor_call_id = tool_call.call_id

                    async def progress(chunk: str, stream: str | None) -> None:
                        await send(
                            {
                                "type": "event",
                                "call_id": bridge_call_id,
                                "event": "tool.progress",
                                "payload": {"chunk": chunk, "stream": stream},
                            }
                        )

                    result = await connection.tool_execute(
                        tool_call,
                        timeout_seconds=int(timeout),
                        output_chunk_callback=progress,
                        on_sent=on_sent,
                    )
                    if not await connection.wait_tool_progress(tool_call.call_id):
                        raise RuntimeError("Forwarded tool progress backpressure exceeded")
                    await send_result(bridge_call_id, result.model_dump(mode="json"))
                elif operation == "inference":
                    request_id = str(payload.get("request_id") or "")
                    messages = payload.get("messages")
                    model = str(payload.get("model") or "")
                    kwargs = payload.get("kwargs") or {}
                    if (
                        not request_id
                        or not isinstance(messages, list)
                        or not model
                        or not isinstance(kwargs, dict)
                    ):
                        raise ValueError("Invalid inference bridge payload")
                    calls[bridge_call_id].inference_request_id = request_id
                    async for chunk in connection.llm_complete_stream(
                        request_id,
                        messages,
                        model,
                        on_sent=on_sent,
                        **kwargs,
                    ):
                        await send(
                            {
                                "type": "event",
                                "call_id": bridge_call_id,
                                "event": "llm.chunk",
                                "payload": chunk,
                            }
                        )
                    await send_result(bridge_call_id, {})
                else:
                    raise ValueError("Unsupported bridge operation")
            except asyncio.CancelledError:
                if accepted and not tearing_down:
                    await send(
                        {
                            "type": "error",
                            "call_id": bridge_call_id,
                            "delivery_state": "accepted_unknown",
                            "message": "Forwarded call cancelled after acceptance",
                        }
                    )
                raise
            except ExecutorRPCError as exc:
                await send(
                    {
                        "type": "error",
                        "call_id": bridge_call_id,
                        "delivery_state": "terminal",
                        "message": str(exc),
                        "code": exc.code,
                    }
                )
            except ExecutorDeliveryError as exc:
                connection_owner = getattr(connection, "connection_owner", None)
                await send(
                    {
                        "type": "error",
                        "call_id": bridge_call_id,
                        "delivery_state": exc.delivery_state.value,
                        "message": str(exc)[:500],
                        "code": exc.code,
                        "executor_id": exc.executor_id,
                        "generation": exc.generation,
                        "owner_id": exc.owner_id or getattr(connection_owner, "owner_id", None),
                        "epoch": exc.epoch or getattr(connection_owner, "epoch", None),
                        "same_executor_only": exc.same_executor_only,
                        "retry_after": exc.retry_after,
                    }
                )
            except _BridgeProtocolError as exc:
                await send(
                    {
                        "type": "error",
                        "call_id": bridge_call_id,
                        "delivery_state": "terminal",
                        "code": "protocol_error",
                        "message": str(exc)[:500],
                    }
                )
            except Exception as exc:
                await send(
                    {
                        "type": "error",
                        "call_id": bridge_call_id,
                        "delivery_state": "accepted_unknown" if accepted else "not_sent",
                        "message": str(exc)[:500],
                    }
                )
            finally:
                calls.pop(bridge_call_id, None)

        while True:
            frame = _bounded_frame(await websocket.receive_json())
            frame_type = frame.get("type")
            if frame_type == "call":
                call_id = str(frame.get("call_id") or "")
                if not call_id or call_id in calls:
                    raise ValueError("Invalid or duplicate bridge call ID")
                if len(calls) >= BRIDGE_MAX_CALLS:
                    await send(
                        {
                            "type": "error",
                            "call_id": call_id,
                            "delivery_state": "not_sent",
                            "message": "Bridge call limit exceeded",
                        }
                    )
                    continue
                task = asyncio.create_task(run_call(frame), name=f"bridge-call-{call_id}")
                calls[call_id] = _BridgeCall(task=task)
            elif frame_type == "cancel":
                call_id = str(frame.get("call_id") or "")
                call = calls.get(call_id)
                if call is not None:
                    if call.executor_call_id:
                        await connection.cancel_call(call.executor_call_id)
                    if call.inference_request_id:
                        await connection.cancel_inference(call.inference_request_id)
                    call.task.cancel()
            else:
                raise ValueError("Unsupported bridge frame")
    except (WebSocketDisconnect, asyncio.CancelledError):
        pass
    except Exception as exc:
        logger.warning("controller executor bridge rejected", exc_info=True)
        with contextlib.suppress(Exception):
            await send({"type": "error", "delivery_state": "not_sent", "message": str(exc)[:500]})
    finally:
        tearing_down = True
        active_calls = tuple(calls.values())
        for call in active_calls:
            call.task.cancel()
        cancellation_tasks: list[asyncio.Task[Any]] = []
        if connection is not None:
            for call in active_calls:
                if call.executor_call_id:
                    cancellation_tasks.append(
                        asyncio.create_task(connection.cancel_call(call.executor_call_id))
                    )
                if call.inference_request_id:
                    cancellation_tasks.append(
                        asyncio.create_task(connection.cancel_inference(call.inference_request_id))
                    )
        if cancellation_tasks:
            try:
                async with asyncio.timeout(3.0):
                    await asyncio.gather(*cancellation_tasks, return_exceptions=True)
            except TimeoutError:
                for task in cancellation_tasks:
                    task.cancel()
                await asyncio.gather(*cancellation_tasks, return_exceptions=True)
        if active_calls:
            call_tasks = tuple(call.task for call in active_calls)
            try:
                async with asyncio.timeout(1.0):
                    await asyncio.gather(*call_tasks, return_exceptions=True)
            except TimeoutError:
                for task in call_tasks:
                    task.cancel()
                await asyncio.gather(*call_tasks, return_exceptions=True)
        with contextlib.suppress(Exception):
            await websocket.close()
