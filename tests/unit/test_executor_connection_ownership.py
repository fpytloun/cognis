from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
from sqlalchemy import inspect, select
from starlette.websockets import WebSocketDisconnect

from cognis.bootstrap import run_schema_bootstrap
from cognis.channels.inbound import InboundPipeline
from cognis.core.controller_directory import ControllerInstanceDirectory
from cognis.core.controller_runtime import ControllerRuntime
from cognis.core.executor_connection_ownership import ExecutorConnectionOwnership
from cognis.models.tool import ExecutorCapabilities
from cognis.providers.executor.websocket import (
    ExecutorDisconnectedError,
    WebSocketExecutorConnection,
)
from cognis.store.database import create_engine, create_session_factory
from cognis.store.models import ControllerInstanceRow
from cognis.store.queries import create_executor, get_executor_row


class _FakeWebSocket:
    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []
        self.received: asyncio.Queue[Any] = asyncio.Queue()
        self.closed = False

    async def send_json(self, data: dict[str, Any]) -> None:
        self.sent.append(data)

    async def receive_json(self) -> dict[str, Any]:
        value = await self.received.get()
        if isinstance(value, BaseException):
            raise value
        return value

    async def close(self, code: int = 1000, reason: str | None = None) -> None:
        del code, reason
        self.closed = True


@pytest.mark.asyncio
async def test_takeover_fences_stale_executor_mutations_and_release(tmp_path: Path) -> None:
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path / 'ownership.db'}")
    await run_schema_bootstrap(engine)
    factory = create_session_factory(engine)
    async with factory() as session:
        await create_executor(
            session,
            executor_id="executor-1",
            name="Executor",
            executor_type="websocket",
        )
        await session.commit()

    first_authority = ExecutorConnectionOwnership(factory, "controller-a:boot-a")
    second_authority = ExecutorConnectionOwnership(factory, "controller-b:boot-b")
    first = await first_authority.takeover_validated("executor-1", token_version=0)
    assert first is not None
    async with factory() as session:
        assert (
            await first_authority.update_runtime_state(
                session,
                first,
                runtime_state="active",
            )
            is not None
        )
        await session.commit()

    second = await second_authority.takeover("executor-1")
    assert second.epoch > first.epoch
    assert not await first_authority.is_current(first)
    inbound = InboundPipeline(
        session_factory=factory,
        turn_scheduler=MagicMock(),
        session_manager=MagicMock(),
        pairing_service=MagicMock(),
        channel_manager_ref=MagicMock(),
    )
    assert not await inbound._admit_executor_inbound(first)
    assert await second_authority.is_current(second)
    async with factory() as session:
        assert (
            await first_authority.update_runtime_state(
                session,
                first,
                runtime_state="offline",
            )
            is None
        )
        assert (
            await second_authority.update_runtime_state(
                session,
                second,
                runtime_state="degraded",
            )
            is not None
        )
        await session.commit()
    async with factory() as session:
        row = await get_executor_row(session, "executor-1")
        assert row is not None and row.runtime_state == "degraded"

    assert await first_authority.release(first) is False
    assert await second_authority.renew_from_heartbeat(
        second,
        heartbeat_received_at=datetime.now(UTC),
    )
    assert not await second_authority.renew_from_heartbeat(
        second,
        heartbeat_received_at=datetime.now(UTC) - timedelta(minutes=2),
    )
    assert await second_authority.revoke("executor-1")
    assert not await second_authority.is_current(second)
    async with factory() as session:
        row = await get_executor_row(session, "executor-1")
        assert row is not None
        row.status = "inactive"
        await session.commit()
    assert await first_authority.takeover_validated("executor-1", token_version=0) is None
    await engine.dispose()


@pytest.mark.asyncio
async def test_takeover_rejects_old_socket_frame_before_waiter_or_db_mutation(
    tmp_path: Path,
) -> None:
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path / 'old-frames.db'}")
    await run_schema_bootstrap(engine)
    factory = create_session_factory(engine)
    async with factory() as session:
        await create_executor(
            session,
            executor_id="executor-1",
            name="Executor",
            executor_type="websocket",
        )
        await session.commit()

    first_authority = ExecutorConnectionOwnership(factory, "controller-a:boot-a")
    second_authority = ExecutorConnectionOwnership(factory, "controller-b:boot-b")
    first = await first_authority.takeover_validated("executor-1", token_version=0)
    assert first is not None
    ws = _FakeWebSocket()
    conn = WebSocketExecutorConnection(
        ws,
        "executor-1",
        ExecutorCapabilities(),
        connection_owner=first,
    )
    conn.register_ownership_check_callback(lambda: first_authority.is_current(first))

    callback_called = asyncio.Event()

    async def _persist_stale_completion(
        owner: Any,
        _executor_id: str,
        _payload: dict[str, Any],
    ) -> None:
        callback_called.set()
        async with factory() as session:
            await first_authority.update_runtime_state(
                session,
                owner,
                runtime_state="blocked",
            )
            await session.commit()

    conn.register_local_model_callbacks(on_progress=None, on_completed=_persist_stale_completion)
    conn.start_receiver()
    pending = asyncio.create_task(conn.rpc_call("local_model.status", {}, timeout=5.0))
    while not ws.sent:
        await asyncio.sleep(0)

    second = await second_authority.takeover("executor-1")
    assert second.epoch > first.epoch
    ws.received.put_nowait(
        {
            "jsonrpc": "2.0",
            "method": "local_model.completed",
            "params": {"operation_id": "operation-1", "state": "succeeded"},
        }
    )

    with pytest.raises(ExecutorDisconnectedError, match="ownership lost"):
        await asyncio.wait_for(pending, timeout=1.0)
    assert not callback_called.is_set()
    async with factory() as session:
        row = await get_executor_row(session, "executor-1")
        assert row is not None
        assert row.runtime_state == "offline"
    await engine.dispose()


@pytest.mark.asyncio
async def test_blocking_owned_callback_does_not_block_heartbeat_or_disconnect(
    tmp_path: Path,
) -> None:
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path / 'callback-disconnect.db'}")
    await run_schema_bootstrap(engine)
    factory = create_session_factory(engine)
    async with factory() as session:
        await create_executor(
            session,
            executor_id="executor-1",
            name="Executor",
            executor_type="websocket",
        )
        await session.commit()
    authority = ExecutorConnectionOwnership(factory, "controller-a:boot-a")
    owner = await authority.takeover_validated("executor-1", token_version=0)
    assert owner is not None
    ws = _FakeWebSocket()
    conn = WebSocketExecutorConnection(
        ws,
        "executor-1",
        ExecutorCapabilities(),
        connection_owner=owner,
    )
    conn.register_ownership_check_callback(lambda: authority.is_current(owner))
    conn.register_heartbeat_callback(
        lambda received_at: authority.renew_from_heartbeat(
            owner,
            heartbeat_received_at=received_at,
        )
    )
    conn.register_owned_callback_dispatcher(
        lambda current_owner, callback, *args: authority.run_callback_if_current(
            current_owner,
            callback,
            *args,
        )
    )
    started = asyncio.Event()
    cancelled = asyncio.Event()

    async def _blocking_progress(_chunk: str, _stream: str | None) -> None:
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.set()
            raise

    conn._tool_chunk_callbacks["call-1"] = _blocking_progress
    conn.start_receiver()
    ws.received.put_nowait(
        {
            "jsonrpc": "2.0",
            "method": "tool.progress",
            "params": {"call_id": "call-1", "delta": "chunk"},
        }
    )
    await asyncio.wait_for(started.wait(), timeout=1.0)
    ws.received.put_nowait({"jsonrpc": "2.0", "method": "executor.heartbeat", "params": {}})
    ws.received.put_nowait(WebSocketDisconnect(code=1011, reason="physical disconnect"))

    await asyncio.wait_for(conn.wait_until_closed(), timeout=1.0)
    assert cancelled.is_set()
    await engine.dispose()


@pytest.mark.asyncio
async def test_controller_directory_lifecycle_and_bootstrap_parity(tmp_path: Path) -> None:
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path / 'directory.db'}")
    await run_schema_bootstrap(engine)
    await run_schema_bootstrap(engine)
    factory = create_session_factory(engine)
    runtime = ControllerRuntime("controller-a", incarnation_id="boot-a")
    directory = ControllerInstanceDirectory(factory, runtime, internal_url=None)

    await directory.start()
    assert await directory.get_reachable(runtime.owner_id) is None
    await directory.mark_ready()
    live = await directory.get_live(runtime.owner_id)
    assert live is not None
    assert live.internal_url is None
    assert await directory.get_reachable("controller-a:wrong-boot") is None
    async with factory() as session:
        row = await session.scalar(
            select(ControllerInstanceRow).where(ControllerInstanceRow.owner_id == runtime.owner_id)
        )
        assert row is not None
        assert row.controller_id == "controller-a"
        assert row.incarnation_id == "boot-a"
        assert row.internal_url is None
        assert row.lifecycle_state == "ready"
        assert row.expires_at > row.heartbeat_at
        row.expires_at = datetime.now(UTC) - timedelta(seconds=1)
        await session.commit()
    assert await directory.get_reachable(runtime.owner_id) is None
    await directory._heartbeat_once()

    await directory.begin_draining()
    assert await directory.get_live(runtime.owner_id) is None
    assert await directory.get_ready(runtime.owner_id) is None
    reachable = await directory.get_reachable(runtime.owner_id)
    assert reachable is not None
    assert reachable.lifecycle_state == "draining"
    await directory.stop()
    assert await directory.get_reachable(runtime.owner_id) is None
    async with factory() as session:
        row = await session.get(ControllerInstanceRow, runtime.owner_id)
        assert row is not None
        assert row.lifecycle_state == "stopped"
        assert row.expires_at <= row.heartbeat_at
    async with engine.connect() as connection:
        indexes = await connection.run_sync(
            lambda sync_connection: {
                item["name"]
                for item in inspect(sync_connection).get_indexes("controller_instances")
            }
        )
    assert indexes == {
        "ix_controller_instances_controller",
        "ix_controller_instances_expires",
    }
    await engine.dispose()


@pytest.mark.asyncio
async def test_controller_directory_heartbeat_cannot_regress_draining(
    tmp_path: Path,
) -> None:
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path / 'directory-race.db'}")
    await run_schema_bootstrap(engine)
    factory = create_session_factory(engine)
    runtime = ControllerRuntime("controller-a", incarnation_id="boot-a")
    directory = ControllerInstanceDirectory(factory, runtime, internal_url=None)
    await directory.start()
    await directory.mark_ready()

    original_write = directory._write
    heartbeat_started = asyncio.Event()
    release_heartbeat = asyncio.Event()
    block_ready_once = True

    async def _controlled_write(lifecycle_state: str, *, live: bool) -> None:
        nonlocal block_ready_once
        if lifecycle_state == "ready" and block_ready_once:
            block_ready_once = False
            heartbeat_started.set()
            await release_heartbeat.wait()
        await original_write(lifecycle_state, live=live)

    directory._write = _controlled_write  # type: ignore[method-assign]
    heartbeat = asyncio.create_task(directory._heartbeat_once())
    await asyncio.wait_for(heartbeat_started.wait(), timeout=1.0)
    draining = asyncio.create_task(directory.begin_draining())
    await asyncio.sleep(0)
    assert not draining.done()

    release_heartbeat.set()
    await asyncio.gather(heartbeat, draining)
    async with factory() as session:
        row = await session.get(ControllerInstanceRow, runtime.owner_id)
        assert row is not None
        assert row.lifecycle_state == "draining"

    await directory.stop()
    await engine.dispose()
