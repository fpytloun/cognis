"""Coalescing invariants exercised through both real controller transports."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from cognis.core.shell_target_evidence import shell_target_evidence
from cognis.models.tool import ExecutorCapabilities
from cognis.providers.executor.delivery import ExecutorDeliveryError
from cognis.providers.executor.forwarding import ForwardedExecutorConnection
from cognis.providers.executor.websocket import WebSocketExecutorConnection
from tests.unit.test_executor_bridge import _FakeClientBridge
from tests.unit.test_shell_target_evidence import row
from tests.unit.test_websocket_executor import FakeWebSocket


class _StatusWebSocket(FakeWebSocket):
    def __init__(self) -> None:
        super().__init__()
        self.requests: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self.status_calls: list[dict[str, Any]] = []

    async def send_json(self, data: dict[str, Any]) -> None:
        await super().send_json(data)
        self.status_calls.append(data)
        self.requests.put_nowait(data)

    def respond(self, frame: dict[str, Any], snapshot: dict[str, Any]) -> None:
        self._receive_queue.put_nowait({"jsonrpc": "2.0", "id": frame["id"], "result": snapshot})

    def fail(self, frame: dict[str, Any]) -> None:
        self._receive_queue.put_nowait(
            {
                "jsonrpc": "2.0",
                "id": frame["id"],
                "error": {"code": -32000, "message": "status failed"},
            }
        )


class _StatusBridge(_FakeClientBridge):
    def __init__(self) -> None:
        super().__init__()
        self.requests: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self.status_calls: list[dict[str, Any]] = []

    async def send(self, raw: str) -> None:
        frame = json.loads(raw)
        if frame["type"] != "call":
            await super().send(raw)
            return
        self.sent.append(frame)
        self.status_calls.append(frame)
        self.requests.put_nowait(frame)
        self.incoming.put_nowait(json.dumps({"type": "accepted", "call_id": frame["call_id"]}))

    def respond(self, frame: dict[str, Any], snapshot: dict[str, Any]) -> None:
        self.incoming.put_nowait(
            json.dumps({"type": "result", "call_id": frame["call_id"], "result": snapshot})
        )

    def fail(self, frame: dict[str, Any]) -> None:
        self.incoming.put_nowait(
            json.dumps(
                {
                    "type": "error",
                    "call_id": frame["call_id"],
                    "delivery_state": "accepted_unknown",
                    "message": "status failed",
                }
            )
        )


@dataclass
class _Transport:
    connection: WebSocketExecutorConnection | ForwardedExecutorConnection
    peer: _StatusWebSocket | _StatusBridge
    tasks: list[asyncio.Task[Any]] = field(default_factory=list)

    def read(self, *, include_completed: bool = False) -> asyncio.Task[dict[str, Any]]:
        task = asyncio.create_task(
            self.connection.background_shell_status(include_completed=include_completed)
        )
        self.tasks.append(task)
        return task

    async def next_request(self) -> dict[str, Any]:
        return await asyncio.wait_for(self.peer.requests.get(), timeout=1)


@pytest.fixture(params=["direct", "forwarded"])
async def make_transport(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[Callable[[], _Transport]]:
    transports: list[_Transport] = []

    def make() -> _Transport:
        if request.param == "direct":
            peer = _StatusWebSocket()
            connection = WebSocketExecutorConnection(
                ws=peer, executor_id="exec-1", capabilities=ExecutorCapabilities()
            )
            connection.start_receiver()
        else:
            peer = _StatusBridge()

            async def connect(*_args: Any, **_kwargs: Any) -> _StatusBridge:
                return peer

            monkeypatch.setattr("cognis.providers.executor.forwarding.connect", connect)
            connection = ForwardedExecutorConnection(
                executor_id="exec-1",
                capabilities=ExecutorCapabilities(),
                owner_id="controller-b",
                epoch=len(transports) + 1,
                owner_internal_url="http://controller-b:8080",
                requester_owner_id="controller-a",
                auth_provider=SimpleNamespace(sign_controller_jwt=lambda *_args: "jwt"),
            )
        transport = _Transport(connection, peer)
        transports.append(transport)
        return transport

    yield make
    for transport in transports:
        for task in transport.tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*transport.tasks, return_exceptions=True)
        await transport.connection.close()


@pytest.mark.asyncio
async def test_status_burst_uses_one_rpc_and_independent_snapshots(make_transport) -> None:
    transport = make_transport()
    readers = [transport.read() for _ in range(72)]
    request = await transport.next_request()
    await asyncio.sleep(0)
    assert len(transport.peer.status_calls) == 1
    snapshot = {"shells": [row(executor_id="remote")], "metadata": {"revision": 1}}
    transport.peer.respond(request, snapshot)
    results = await asyncio.gather(*readers)

    assert all(result["shells"][0]["executor_id"] == "exec-1" for result in results)
    results[0]["shells"][0]["user_email"] = "changed"
    results[0]["metadata"]["revision"] = 2
    assert results[1]["shells"][0]["user_email"] != "changed"
    assert results[1]["metadata"]["revision"] == 1
    assert snapshot["metadata"]["revision"] == 1
    assert transport.connection._shell_status_reads._reads == {}

    fresh = transport.read()
    next_request = await transport.next_request()
    assert len(transport.peer.status_calls) == 2
    transport.peer.respond(next_request, {"shells": [], "metadata": {"revision": 3}})
    assert (await fresh)["metadata"]["revision"] == 3


@pytest.mark.asyncio
async def test_completed_flag_has_separate_in_flight_reads(make_transport) -> None:
    transport = make_transport()
    readers = [transport.read(include_completed=flag) for flag in [False, True, False, True]]
    requests = [await transport.next_request(), await transport.next_request()]
    assert len(transport.peer.status_calls) == 2
    for request in requests:
        params = request["payload"]["params"] if "payload" in request else request["params"]
        transport.peer.respond(request, {"shells": [], "completed": params["include_completed"]})
    results = await asyncio.gather(*readers)
    assert [result["completed"] for result in results] == [False, True, False, True]


@pytest.mark.asyncio
async def test_one_cancelled_waiter_does_not_cancel_other_readers(make_transport) -> None:
    transport = make_transport()
    cancelled, survivor = transport.read(), transport.read()
    request = await transport.next_request()
    cancelled.cancel()
    with pytest.raises(asyncio.CancelledError):
        await cancelled
    assert not survivor.done()
    assert len(transport.connection._pending) == 1
    late_joiner = transport.read()
    await asyncio.sleep(0)
    assert len(transport.peer.status_calls) == 1
    transport.peer.respond(request, {"shells": []})
    assert await survivor == await late_joiner == {"shells": []}


@pytest.mark.asyncio
async def test_last_waiter_cancellation_releases_rpc_and_allows_fresh_read(make_transport) -> None:
    transport = make_transport()
    readers = [transport.read(), transport.read()]
    await transport.next_request()
    for reader in readers:
        reader.cancel()
    outcomes = await asyncio.gather(*readers, return_exceptions=True)
    assert all(isinstance(outcome, asyncio.CancelledError) for outcome in outcomes)
    assert transport.connection._pending == {}
    assert transport.connection._shell_status_reads._reads == {}
    fresh = transport.read()
    request = await transport.next_request()
    assert len(transport.peer.status_calls) == 2
    transport.peer.respond(request, {"shells": []})
    assert await fresh == {"shells": []}


@pytest.mark.asyncio
async def test_failed_read_fans_out_without_caching_error(make_transport) -> None:
    transport = make_transport()
    readers = [transport.read(), transport.read()]
    request = await transport.next_request()
    transport.peer.fail(request)
    attempts = 1
    if isinstance(transport.peer, _StatusBridge):
        # Preserve the existing one-shot replay of accepted-unknown unary RPCs.
        retry = await transport.next_request()
        assert retry["payload"]["stable_call_id"] == request["payload"]["stable_call_id"]
        transport.peer.fail(retry)
        attempts = 2
    outcomes = await asyncio.gather(*readers, return_exceptions=True)
    assert all(
        isinstance(outcome, Exception) and "status failed" in str(outcome) for outcome in outcomes
    )
    fresh = transport.read()
    request = await transport.next_request()
    assert len(transport.peer.status_calls) == attempts + 1
    transport.peer.respond(request, {"shells": []})
    assert await fresh == {"shells": []}


@pytest.mark.asyncio
async def test_close_terminates_shared_read_and_rejects_new_joiners(make_transport) -> None:
    transport = make_transport()
    readers = [transport.read(), transport.read()]
    await transport.next_request()
    await transport.connection.close()
    outcomes = await asyncio.gather(*readers, return_exceptions=True)
    assert all(isinstance(outcome, ExecutorDeliveryError) for outcome in outcomes)
    with pytest.raises(ExecutorDeliveryError) as exc:
        await transport.connection.background_shell_status()
    assert exc.value.delivery_state == "not_sent"
    assert len(transport.peer.status_calls) == 1
    assert transport.connection._shell_status_reads._reads == {}


@pytest.mark.asyncio
async def test_routing_admission_is_rechecked_before_join(make_transport) -> None:
    transport = make_transport()
    existing = transport.read()
    request = await transport.next_request()
    if isinstance(transport.connection, WebSocketExecutorConnection):
        ownership_check = AsyncMock(return_value=False)
        transport.connection.register_ownership_check_callback(ownership_check)
    else:
        await transport.connection.retire()
    with pytest.raises(ExecutorDeliveryError) as exc:
        await transport.connection.background_shell_status()
    assert exc.value.delivery_state == "not_sent"
    assert len(transport.peer.status_calls) == 1
    if isinstance(transport.connection, WebSocketExecutorConnection):
        ownership_check.assert_awaited_once()
        with pytest.raises(ExecutorDeliveryError):
            await existing
    else:
        transport.peer.respond(request, {"shells": []})
        assert await existing == {"shells": []}


@pytest.mark.asyncio
async def test_connection_replacement_does_not_share_old_in_flight_snapshot(make_transport) -> None:
    old = make_transport()
    old_read = old.read()
    old_request = await old.next_request()
    replacement = make_transport()
    new_read = replacement.read()
    new_request = await replacement.next_request()
    replacement.peer.respond(new_request, {"shells": [], "incarnation": 2})
    assert (await new_read)["incarnation"] == 2
    assert not old_read.done()
    old.peer.respond(old_request, {"shells": [], "incarnation": 1})
    assert (await old_read)["incarnation"] == 1


@pytest.mark.asyncio
async def test_shared_snapshot_keeps_per_caller_ownership_filters(make_transport) -> None:
    transport = make_transport()

    async def lookup(user_id: str) -> dict[str, Any]:
        return await shell_target_evidence(
            transport.connection,
            shell_id="shell_monitor",
            user_id=user_id,
            session_id="session_after_compaction",
            conversation_id="conversation_monitor",
        )

    owner = asyncio.create_task(lookup("owner@example.com"))
    foreign = asyncio.create_task(lookup("foreign@example.com"))
    transport.tasks.extend([owner, foreign])
    request = await transport.next_request()
    transport.peer.respond(request, {"shells": [row(executor_id="remote")]})
    assert (await owner)["coverage"] == "complete"
    assert (await foreign)["reason"] == "not_found_or_not_owned"
    assert len(transport.peer.status_calls) == 1
