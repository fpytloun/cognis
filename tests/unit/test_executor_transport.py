"""Executor transport regression tests, including real size-limited sockets."""

from __future__ import annotations

import asyncio
import base64
import json
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock

import pytest
from starlette.websockets import WebSocketDisconnect
from websockets.asyncio.client import connect
from websockets.asyncio.server import serve

from cognis import executor_transport
from cognis.core.executor_connection_ownership import ExecutorConnectionOwner
from cognis.executor.runner import ExecutorRunner, _ConnectionSender
from cognis.executor_transport import (
    CHUNK_CHARS,
    CHUNKED_JSON_PROTOCOL,
    JSONMessageReceiver,
    JSONMessageSender,
)
from cognis.models.tool import ExecutorCapabilities, ExecutorConfig
from cognis.providers.executor.websocket import WebSocketExecutorConnection
from cognis.store.coordination import Lease


def _chunk(*, index: int = 0, data: str = "{", final: bool = False) -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "method": "transport.chunk",
        "params": {
            "transfer_id": uuid.uuid4().hex,
            "index": index,
            "data": data,
            "final": final,
        },
    }


@pytest.mark.asyncio
async def test_unicode_and_escaping_are_bounded_and_lossless() -> None:
    receiver = JSONMessageReceiver()
    receiver.enabled = True
    results = []
    frame_sizes = []

    async def send_text(text: str) -> None:
        frame_sizes.append(len(text.encode()))
        result = await receiver.feed(json.loads(text))
        if result is not None:
            results.append(result)

    sender = JSONMessageSender(send_text)
    sender.enabled = True
    original = {"result": {"text": '☃😀\x00"\\\n' * CHUNK_CHARS}}
    await sender.send(json.dumps(original, ensure_ascii=False))
    assert results == [original]
    assert len(frame_sizes) > 1
    assert max(frame_sizes) < 1024 * 1024
    assert receiver.receive_timeout is None
    assert receiver._file is None


@pytest.mark.asyncio
async def test_chunking_requires_negotiation_and_preserves_legacy_messages() -> None:
    sent = []

    async def send_text(text: str) -> None:
        sent.append(text)

    sender = JSONMessageSender(send_text)
    payload = json.dumps({"result": "a" * (CHUNK_CHARS * 2)})
    await sender.send(payload)
    assert sent == [payload]
    receiver = JSONMessageReceiver()
    with pytest.raises(ValueError, match="Unnegotiated"):
        await receiver.feed(_chunk())


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "change",
    [
        {"index": 1},
        {"index": True},
        {"data": ""},
        {"data": "a" * (CHUNK_CHARS + 1)},
        {"final": 1},
        {"transfer_id": ""},
    ],
)
async def test_invalid_chunk_is_rejected(change: dict[str, Any]) -> None:
    receiver = JSONMessageReceiver()
    receiver.enabled = True
    chunk = _chunk()
    chunk["params"].update(change)
    try:
        with pytest.raises(ValueError):
            await receiver.feed(chunk)
    finally:
        await receiver.close()
    assert receiver._file is None


@pytest.mark.asyncio
async def test_partial_transfer_cleanup_sequence_and_control_interleaving() -> None:
    receiver = JSONMessageReceiver()
    receiver.enabled = True
    chunk = _chunk()
    assert await receiver.feed(chunk) is None
    partial_file = receiver._file
    timeout = receiver.receive_timeout
    heartbeat = {"method": "executor.heartbeat", "params": {}}
    assert await receiver.feed(heartbeat) == heartbeat
    assert receiver.receive_timeout <= timeout
    with pytest.raises(ValueError, match="Out-of-order"):
        await receiver.feed(chunk)
    await receiver.close()
    assert partial_file.closed
    assert receiver.receive_timeout is None
    assert await receiver.feed(_chunk(data='{"result":{}}', final=True)) == {"result": {}}


@pytest.mark.asyncio
async def test_concurrent_large_messages_do_not_overlap_and_heartbeat_can_pass() -> None:
    receiver = JSONMessageReceiver()
    receiver.enabled = True
    results = []
    first_chunk = asyncio.Event()
    release = asyncio.Event()

    async def send_text(text: str) -> None:
        frame = json.loads(text)
        result = await receiver.feed(frame)
        if result is not None:
            results.append(result)
        if frame.get("method") == "transport.chunk" and not first_chunk.is_set():
            first_chunk.set()
            await release.wait()

    sender = JSONMessageSender(send_text)
    sender.enabled = True
    first = {"id": "first", "result": "a" * (CHUNK_CHARS * 3)}
    second = {"id": "second", "result": "b" * (CHUNK_CHARS * 3)}
    task = asyncio.create_task(sender.send(json.dumps(first)))
    await first_chunk.wait()
    other = asyncio.create_task(sender.send(json.dumps(second)))
    heartbeat = {"method": "executor.heartbeat", "params": {}}
    control = asyncio.create_task(sender.send(json.dumps(heartbeat)))
    release.set()
    await asyncio.gather(task, other, control)
    assert results == [heartbeat, first, second]


@pytest.mark.asyncio
async def test_interrupted_executor_send_retires_socket() -> None:
    socket = AsyncMock()
    socket.send.side_effect = asyncio.CancelledError
    sender = _ConnectionSender(socket)
    sender.transport.enabled = True
    with pytest.raises(asyncio.CancelledError):
        await sender.send(json.dumps({"result": "a" * (CHUNK_CHARS * 2)}))
    assert sender.retired
    socket.close.assert_awaited_once()
    with pytest.raises(RuntimeError, match="retired"):
        await sender.send("{}")


@pytest.mark.asyncio
@pytest.mark.parametrize("large", [False, True])
async def test_cancelling_unsent_executor_message_keeps_connection_usable(large: bool) -> None:
    started = asyncio.Event()
    release = asyncio.Event()
    socket = AsyncMock()
    receiver = JSONMessageReceiver()
    receiver.enabled = True
    results = []

    async def send_text(text: str) -> None:
        if not started.is_set():
            started.set()
            await release.wait()
        result = await receiver.feed(json.loads(text))
        if result is not None:
            results.append(result)

    socket.send.side_effect = send_text
    sender = _ConnectionSender(socket)
    sender.transport.enabled = True
    payload = {"result": "a" * (CHUNK_CHARS * 3)}
    active = asyncio.create_task(sender.send(json.dumps(payload)))
    await started.wait()
    queued = asyncio.create_task(
        sender.send(json.dumps({"result": "b" * (CHUNK_CHARS * 2)}) if large else "{}")
    )
    await asyncio.sleep(0)
    queued.cancel()
    with pytest.raises(asyncio.CancelledError):
        await queued
    assert not sender.retired
    socket.close.assert_not_awaited()
    release.set()
    await active
    await sender.send('{"result":"after cancellation"}')
    assert results == [payload, {"result": "after cancellation"}]


@pytest.mark.asyncio
async def test_controller_cancellation_passes_a_paused_large_request() -> None:
    first_chunk = asyncio.Event()
    release = asyncio.Event()
    cancelled = asyncio.Event()
    request_complete = asyncio.Event()
    socket = AsyncMock()
    receiver = JSONMessageReceiver()
    receiver.enabled = True

    async def send_text(text: str) -> None:
        frame = json.loads(text)
        result = await receiver.feed(frame)
        if frame.get("method") == "transport.chunk" and not first_chunk.is_set():
            first_chunk.set()
            await release.wait()
        if result is not None and result.get("method") == "tool.cancel":
            assert not request_complete.is_set()
            cancelled.set()
        if result is not None and result.get("method") == "tool.execute":
            request_complete.set()

    socket.send_text.side_effect = send_text
    conn = WebSocketExecutorConnection(socket, "transport-test", ExecutorCapabilities())
    conn.negotiate_message_transport(CHUNKED_JSON_PROTOCOL)
    request = asyncio.create_task(
        conn.rpc_call("tool.execute", {"padding": "p" * (CHUNK_CHARS * 3)}, timeout=10)
    )
    await first_chunk.wait()
    cancel = asyncio.create_task(conn._cancel_physical_call("tool.execute", {"call_id": "running"}))
    await asyncio.sleep(0)
    release.set()
    await asyncio.wait_for(cancelled.wait(), timeout=1)
    await cancel
    await asyncio.wait_for(request_complete.wait(), timeout=1)
    future = next(iter(conn._pending.values()))
    future.set_result({})
    assert await request == {}


@pytest.mark.asyncio
async def test_runner_receives_chunked_request_through_dispatch_boundary() -> None:
    frames = []

    async def append_frame(text: str) -> None:
        frames.append(text)

    sender = JSONMessageSender(append_frame)
    sender.enabled = True
    params = {"padding": "p" * (CHUNK_CHARS * 2)}
    await sender.send(
        json.dumps({"jsonrpc": "2.0", "id": "status", "method": "lsp.status", "params": params})
    )

    class Socket:
        async def send(self, text: str) -> None:
            pass

        def __aiter__(self):
            async def iterate():
                for frame in frames:
                    yield frame

            return iterate()

    socket = Socket()
    runner = ExecutorRunner(ExecutorConfig(executor_id="transport-test"))
    connection_sender = _ConnectionSender(socket)
    connection_sender.transport.enabled = True
    runner._connection_senders[id(socket)] = connection_sender
    runner._handle_lsp_status = AsyncMock()
    await runner._message_loop(socket)
    await asyncio.sleep(0)
    runner._handle_lsp_status.assert_awaited_once_with(socket, "status", params)


@pytest.mark.asyncio
@pytest.mark.parametrize("acknowledged", [None, CHUNKED_JSON_PROTOCOL, "chunked-json-v2"])
async def test_runner_registration_negotiates_only_supported_protocol(
    acknowledged: str | None,
) -> None:
    observed = []

    async def server_handler(socket: Any) -> None:
        ready = json.loads(await socket.recv())
        assert ready["params"]["message_transport"] == CHUNKED_JSON_PROTOCOL
        result = {"status": "registered", "executor_id": "transport-test"}
        if acknowledged is not None:
            result["message_transport"] = acknowledged
        await socket.send(json.dumps({"jsonrpc": "2.0", "id": ready["id"], "result": result}))
        await socket.wait_closed()

    async with serve(server_handler, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        runner = ExecutorRunner(
            ExecutorConfig(
                executor_id="transport-test",
                controller_url=f"ws://127.0.0.1:{port}",
                controller_token="transport-test-token",
            )
        )
        runner._refresh_resource_snapshot = AsyncMock(return_value=None)

        async def check_negotiation(socket: Any) -> None:
            observed.append(runner._connection_senders[id(socket)].transport.enabled)

        runner._run_connection_loops = check_negotiation
        await runner._connect_and_serve()
    assert observed == [acknowledged == CHUNKED_JSON_PROTOCOL]
    conn = WebSocketExecutorConnection(AsyncMock(), "transport-test", ExecutorCapabilities())
    assert conn.negotiate_message_transport(acknowledged) == (
        CHUNKED_JSON_PROTOCOL if acknowledged == CHUNKED_JSON_PROTOCOL else None
    )


@pytest.mark.asyncio
async def test_controller_stalled_transfer_expires_and_discards_file(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(executor_transport, "CHUNK_IDLE_SECONDS", 0.02)
    socket = AsyncMock()
    frames = asyncio.Queue()
    await frames.put(_chunk())
    await frames.put({"method": "executor.heartbeat", "params": {}})
    socket.receive_json.side_effect = frames.get
    conn = WebSocketExecutorConnection(socket, "transport-test", ExecutorCapabilities())
    conn.negotiate_message_transport(CHUNKED_JSON_PROTOCOL)
    conn.start_receiver()
    await asyncio.wait_for(conn.wait_until_closed(), timeout=1)
    assert not conn.connected
    assert conn._message_receiver._file is None
    assert conn._message_receiver.receive_timeout is None


@pytest.mark.asyncio
async def test_disconnect_discards_partial_transfer() -> None:
    socket = AsyncMock()
    socket.receive_json.side_effect = [_chunk(), WebSocketDisconnect(code=1006)]
    conn = WebSocketExecutorConnection(socket, "transport-test", ExecutorCapabilities())
    conn.negotiate_message_transport(CHUNKED_JSON_PROTOCOL)
    conn.start_receiver()
    await asyncio.wait_for(conn.wait_until_closed(), timeout=1)
    assert not conn.connected
    assert conn._message_receiver._file is None


@pytest.mark.asyncio
async def test_takeover_during_reassembly_cannot_resolve_response() -> None:
    socket = AsyncMock()
    frames = asyncio.Queue()
    owner = ExecutorConnectionOwner(
        executor_id="transport-test",
        lease=Lease(
            resource_key="executor_connection:transport-test",
            owner_id="controller-transport-test",
            fencing_token=1,
            lease_expires_at=datetime.now(UTC) + timedelta(minutes=1),
        ),
    )
    conn = WebSocketExecutorConnection(
        socket, "transport-test", ExecutorCapabilities(), connection_owner=owner
    )
    conn.negotiate_message_transport(CHUNKED_JSON_PROTOCOL)
    owned = True
    ownership_checks = []

    async def check_owned() -> bool:
        ownership_checks.append(owned)
        return owned

    conn.register_ownership_check_callback(check_owned)
    original_feed = conn._message_receiver.feed

    async def takeover_after_decode(frame: dict[str, Any]) -> dict[str, Any] | None:
        nonlocal owned
        result = await original_feed(frame)
        if result is not None:
            owned = False
        return result

    conn._message_receiver.feed = takeover_after_decode
    socket.receive_json.side_effect = frames.get
    pending = asyncio.get_running_loop().create_future()
    conn._pending["response-under-takeover"] = pending
    payload = {"id": "response-under-takeover", "result": {"padding": "p" * (CHUNK_CHARS * 2)}}

    async def enqueue(text: str) -> None:
        await frames.put(json.loads(text))

    sender = JSONMessageSender(enqueue)
    sender.enabled = True
    await sender.send(json.dumps(payload))
    conn.start_receiver()
    await asyncio.wait_for(conn.wait_until_closed(), timeout=1)
    assert not conn.connected
    with pytest.raises(Exception, match="ownership lost"):
        await pending
    assert ownership_checks.count(True) >= 2
    assert False in ownership_checks
    assert conn._message_receiver._file is None


class _ServerSocket:
    """Adapt a real websockets socket to the controller's Starlette boundary."""

    def __init__(self, socket: Any) -> None:
        self.socket = socket

    async def send_text(self, text: str) -> None:
        await self.socket.send(text)

    async def send_json(self, frame: dict[str, Any]) -> None:
        await self.send_text(json.dumps(frame))

    async def receive_json(self) -> dict[str, Any]:
        from websockets.exceptions import ConnectionClosed

        try:
            return json.loads(await self.socket.recv())
        except ConnectionClosed as exc:
            raise WebSocketDisconnect(code=1000) from exc

    async def close(self, **kwargs: Any) -> None:
        await self.socket.close(**kwargs)


@pytest.mark.asyncio
async def test_40_mib_artifact_response_and_large_request_over_one_mib_sockets() -> None:
    """Exercise both adapters with actual sockets smaller than the payload."""
    completed = asyncio.get_running_loop().create_future()
    binary = bytes(range(256)) * (160 * 1024)  # 40 MiB, larger than both old defaults.
    encoded = base64.b64encode(binary).decode()
    request_params = {"content": "q" * (2 * 1024 * 1024)}
    expected = {"attachments": [{"content_b64": encoded}]}

    async def server_handler(socket: Any) -> None:
        conn = WebSocketExecutorConnection(
            _ServerSocket(socket), "transport-test", ExecutorCapabilities()
        )
        conn.negotiate_message_transport(CHUNKED_JSON_PROTOCOL)
        conn.start_receiver()
        try:
            result = await conn.rpc_call("tool.execute", request_params, timeout=60)
            assert result == expected
            assert conn.connected
            completed.set_result(None)
        except BaseException as exc:
            completed.set_exception(exc)
        finally:
            await conn.close()

    async with serve(server_handler, "127.0.0.1", 0, max_size=1024 * 1024) as server:
        port = server.sockets[0].getsockname()[1]
        async with connect(f"ws://127.0.0.1:{port}", max_size=1024 * 1024) as socket:
            receiver = JSONMessageReceiver()
            receiver.enabled = True
            request = None
            try:
                while request is None:
                    request = await receiver.feed(json.loads(await socket.recv()))
                assert request["params"] == request_params
                runner = ExecutorRunner(ExecutorConfig(executor_id="transport-test"))
                sender = _ConnectionSender(socket)
                sender.transport.enabled = True
                runner._connection_senders[id(socket)] = sender
                await runner._send_rpc_result(socket, request["id"], expected)
                await asyncio.wait_for(completed, timeout=60)
            finally:
                await receiver.close()
