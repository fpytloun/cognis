"""Regression coverage for turn-owned preparation, not wall-clock thresholds."""

from __future__ import annotations

import asyncio
from copy import deepcopy
from typing import Any
from unittest.mock import AsyncMock

import pytest

from cognis.core.policy_acknowledgements import (
    isolate_policy_acknowledgements,
    policy_acknowledgement,
)
from cognis.core.turn_timing import isolate_turn_timing, timing_identity
from cognis.models.tool import ExecutorCapabilities
from cognis.providers.executor.websocket import WebSocketExecutorConnection


class CatalogSocket:
    def __init__(self, *, notifications: bool = True) -> None:
        self.notifications = notifications
        self.catalog = [{"name": "read", "input_schema": {"type": "object"}}]
        self.messages: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self.calls: list[str] = []
        self.invalidate_during_read = False

    async def receive_json(self) -> dict[str, Any]:
        return await self.messages.get()

    async def send_json(self, data: dict[str, Any]) -> None:
        self.calls.append(data["method"])
        if self.invalidate_during_read:
            self.invalidate_during_read = False
            self.messages.put_nowait(
                {"jsonrpc": "2.0", "method": "executor.tools_changed", "params": {}}
            )
        self.messages.put_nowait(
            {
                "jsonrpc": "2.0",
                "id": data["id"],
                "result": {
                    "tools": deepcopy(self.catalog),
                    "catalog_notifications": self.notifications,
                },
            }
        )

    async def close(self, **_: Any) -> None:
        pass


@pytest.mark.asyncio
@pytest.mark.parametrize("notifications", [False, True])
async def test_catalog_reuse_requires_executor_invalidation_contract(notifications: bool) -> None:
    socket = CatalogSocket(notifications=notifications)
    connection = WebSocketExecutorConnection(socket, "exec-catalog", ExecutorCapabilities())
    connection.start_receiver()
    try:
        first, second = await asyncio.gather(connection.list_tools(), connection.list_tools())
        assert first == second == socket.catalog
        assert socket.calls == ["tool.list"] * (1 if notifications else 2)
        first[0]["input_schema"]["type"] = "array"
        third = await connection.list_tools()
        assert third[0]["input_schema"]["type"] == "object"
        assert socket.calls == ["tool.list"] * (1 if notifications else 3)
    finally:
        await connection.close()


@pytest.mark.asyncio
async def test_catalog_deadline_covers_cache_lock_and_cached_reads_do_not_claim_delivery():
    socket = CatalogSocket()
    connection = WebSocketExecutorConnection(socket, "exec-catalog", ExecutorCapabilities())
    connection.start_receiver()
    on_sent = AsyncMock()
    try:
        await connection.list_tools(timeout=2, on_sent=on_sent)
        on_sent.assert_awaited_once()
        on_sent.reset_mock()
        await connection.list_tools(timeout=2, on_sent=on_sent)
        on_sent.assert_not_awaited()
        async with connection._tool_catalog_lock:
            with pytest.raises(TimeoutError):
                await connection.list_tools(timeout=0.01, on_sent=on_sent)
        on_sent.assert_not_awaited()
        assert socket.calls == ["tool.list"]
        assert await connection.list_tools(timeout=2) == socket.catalog
    finally:
        await connection.close()


@pytest.mark.asyncio
async def test_catalog_notification_invalidates_inflight_response_and_cached_result() -> None:
    socket = CatalogSocket()
    socket.invalidate_during_read = True
    connection = WebSocketExecutorConnection(socket, "exec-catalog", ExecutorCapabilities())
    connection.start_receiver()
    try:
        await connection.list_tools()
        await connection.list_tools()
        assert socket.calls == ["tool.list", "tool.list"]
        # Process the actual notification receive boundary before reading again.
        socket.messages.put_nowait(
            {"jsonrpc": "2.0", "method": "executor.tools_changed", "params": {}}
        )
        marker = asyncio.Event()
        original = connection._invalidate_tool_catalog

        def invalidate() -> None:
            original()
            marker.set()

        connection._invalidate_tool_catalog = invalidate
        await asyncio.wait_for(marker.wait(), 2)
        socket.catalog = [{"name": "new"}]
        assert await connection.list_tools() == [{"name": "new"}]
        assert socket.calls == ["tool.list"] * 3
        # Cached catalogs cannot bypass current ownership.
        connection._ensure_current_ownership = AsyncMock(side_effect=RuntimeError("ownership lost"))
        with pytest.raises(RuntimeError, match="ownership lost"):
            await connection.list_tools()
    finally:
        await connection.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["executor.configure", "executor.mcp.retry"])
async def test_catalog_invalidates_before_runtime_mutation(method: str) -> None:
    socket = CatalogSocket()
    connection = WebSocketExecutorConnection(socket, "exec-catalog", ExecutorCapabilities())
    connection.start_receiver()
    try:
        await connection.list_tools()
        await connection.rpc_call(method, {})
        await connection.list_tools()
        assert socket.calls == ["tool.list", method, "tool.list"]
    finally:
        await connection.close()


@pytest.mark.asyncio
async def test_policy_acknowledgements_are_success_only_turn_local_and_input_exact() -> None:
    request = {"session_id": "session-a", "policy": {"allow_paths": ["/work"]}, "agent_id": "a"}

    @isolate_policy_acknowledgements
    async def turn() -> None:
        for delta in (
            {"session_id": "session-b"},
            {"agent_id": "b"},
            {"policy": {"allow_paths": ["/other"]}},
            {"analytics_enabled": False},
        ):
            with policy_acknowledgement("session-a", request) as write:
                write.acknowledge({"revision": 1})
            with policy_acknowledgement("session-a", request) as hit:
                assert hit.policy == {"revision": 1}
                hit.policy["revision"] = 2
            with policy_acknowledgement("session-a", request) as hit:
                assert hit.policy == {"revision": 1}
            with policy_acknowledgement("session-a", {**request, **delta}) as miss:
                assert miss.policy is None
            # Even a failed or uncertain intervening write invalidates A.
            with policy_acknowledgement("session-a", request) as miss:
                assert miss.policy is None

    await turn()
    with policy_acknowledgement("session-a", request) as outside:
        assert outside.policy is None
    await turn()


@pytest.mark.asyncio
async def test_concurrent_policy_writes_cannot_publish_reordered_acknowledgements() -> None:
    @isolate_policy_acknowledgements
    async def turn() -> None:
        with policy_acknowledgement("session-a", {"policy": "a"}) as first:
            with policy_acknowledgement("session-a", {"policy": "b"}) as second:
                second.acknowledge({"revision": 2})
            first.acknowledge({"revision": 1})
        with policy_acknowledgement("session-a", {"policy": "a"}) as after:
            assert after.policy is None
        with policy_acknowledgement("session-a", {"policy": "b"}) as after:
            assert after.policy is None
            after.acknowledge({"revision": 3})
        with policy_acknowledgement("session-a", {"policy": "b"}) as after:
            assert after.policy == {"revision": 3}

    await turn()


@pytest.mark.asyncio
async def test_inherited_acknowledgements_close_with_owner_and_cancellation() -> None:
    release = asyncio.Event()
    inherited: asyncio.Task[None] | None = None

    async def child() -> None:
        await release.wait()
        with policy_acknowledgement("session-a", {}) as write:
            assert write.policy is None
            write.acknowledge({"revision": 2})
        with policy_acknowledgement("session-a", {}) as miss:
            assert miss.policy is None

    @isolate_policy_acknowledgements
    async def turn() -> None:
        nonlocal inherited
        with policy_acknowledgement("session-a", {}) as write:
            write.acknowledge({"revision": 1})
        inherited = asyncio.create_task(child())
        raise asyncio.CancelledError()

    with pytest.raises(asyncio.CancelledError):
        await turn()
    release.set()
    assert inherited is not None
    await inherited
    with policy_acknowledgement("session-a", {}) as outside:
        assert outside.policy is None


@pytest.mark.asyncio
async def test_timing_identity_does_not_leak_across_turns() -> None:
    from types import SimpleNamespace

    @isolate_turn_timing
    async def turn(*, session: Any, turn_id: str) -> None:
        assert timing_identity() == {"session_id": session.session_id, "turn_id": turn_id}
        assert await asyncio.to_thread(timing_identity) == timing_identity()

    await turn(session=SimpleNamespace(session_id="session-a"), turn_id="turn-a")
    assert timing_identity() == {}
