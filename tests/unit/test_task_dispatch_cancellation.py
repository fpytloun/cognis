from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import create_async_engine

from cognis.core.events import EventBus
from cognis.core.invariants.task_dispatches import reconcile_cancelled_dispatches
from cognis.core.task_queue import TaskQueue
from cognis.core.workflow_registry import WorkflowRegistry
from cognis.store.database import create_session_factory
from cognis.store.models import Base, Task
from cognis.store.queries import (
    create_agent,
    create_step_run,
    create_task,
    create_user,
    update_task_status,
)


@pytest_asyncio.fixture
async def cancelled_task(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'cancel.sqlite3'}")
    async with engine.begin() as db:
        await db.run_sync(Base.metadata.create_all)
    factory = create_session_factory(engine)
    owner, agent_id, task_id, session_id, call_id, executor_id, instance_id = (
        uuid4().hex for _ in range(7)
    )
    owner += "@example.org"
    async with factory() as db:
        await create_user(db, email=owner, name="Cancellation", password_hash="hash", role="user")
        await create_agent(db, agent_id=agent_id, name="Cancellation", owner_email=owner)
        task = await create_task(
            db,
            task_id=task_id,
            title="Deferred cancellation",
            agent_id=agent_id,
            created_by=owner,
            status="cancelled",
        )
        step = await create_step_run(
            db,
            task_id=task_id,
            step_name="run",
            step_type="run",
            agent_id=agent_id,
        )
        step.intaris_session_id = session_id
        batch = {
            "session_id": session_id,
            "turn_id": step.step_run_id,
            "attempt_number": 1,
            "tool_calls": [
                {
                    "call_id": call_id,
                    "tool_name": "bash",
                    "executor_id": executor_id,
                    "executor_instance_id": instance_id,
                    "dispatch_state": "sent",
                }
            ],
        }
        task.result_data = {"execution_recovery": {"batch": batch}}
        await db.commit()
    try:
        yield factory, task_id, batch
    finally:
        await engine.dispose()


def _connection(batch):
    descriptor = batch["tool_calls"][0]
    return SimpleNamespace(
        executor_instance_id=descriptor["executor_instance_id"],
        capabilities=SimpleNamespace(durable_tool_receipts=True),
        fetch_tool_result=AsyncMock(
            return_value={
                "state": "active",
                "accepted_executor_instance_id": descriptor["executor_instance_id"],
            }
        ),
        rpc_call=AsyncMock(return_value={"cancelled": True}),
    )


@pytest.mark.asyncio
async def test_queue_reconciler_uses_explicit_provider_without_starting_workflow_workers(
    cancelled_task, monkeypatch
):
    factory, _, batch = cancelled_task
    provider = SimpleNamespace(get_ready_connection=Mock(return_value=_connection(batch)))
    queue = await TaskQueue.from_session_factory(
        session_factory=factory,
        workflow_engine=SimpleNamespace(),
        workflow_registry=WorkflowRegistry(factory),
        event_bus=EventBus(),
        websocket_provider=provider,
    )

    async def reconcile(session_factory, websocket_provider):
        assert session_factory is factory
        assert websocket_provider is provider
        queue._stop_event.set()
        queue._cancel_dispatch_wakeup.set()
        return 0

    reconciler = AsyncMock(side_effect=reconcile)
    monkeypatch.setattr(
        "cognis.core.invariants.task_dispatches.reconcile_cancelled_dispatches", reconciler
    )
    await queue._cancelled_dispatch_loop()
    reconciler.assert_awaited_once()
    assert not queue._active_runs


@pytest.mark.asyncio
@pytest.mark.parametrize("provider_available", [False, True])
async def test_cancelled_not_sent_needs_no_connection_and_preserves_unknown_sibling(
    cancelled_task, provider_available
) -> None:
    factory, task_id, batch = cancelled_task
    unresolved_call = uuid4().hex
    descriptor = {
        **batch["tool_calls"][0],
        "dispatch_state": "not_sent",
        "executor_instance_id": None,
    }
    unresolved = {
        **descriptor,
        "call_id": unresolved_call,
        "dispatch_state": "pending",
    }
    async with factory() as db:
        row = await db.get(Task, task_id)
        row.result_data = {
            "execution_recovery": {"batch": {**batch, "tool_calls": [descriptor, unresolved]}}
        }
        await db.commit()
    provider = SimpleNamespace(get_ready_connection=Mock()) if provider_available else None

    assert await reconcile_cancelled_dispatches(factory, provider) == 1
    assert await reconcile_cancelled_dispatches(factory, provider) == 0
    if provider is not None:
        provider.get_ready_connection.assert_not_called()
    async with factory() as db:
        row = await db.get(Task, task_id)
        remaining = row.result_data["execution_recovery"]["batch"]["tool_calls"]
        assert [entry["call_id"] for entry in remaining] == [unresolved_call]


@pytest.mark.asyncio
async def test_cancel_deferred_call_retries_after_reconnect_without_workflow_worker(cancelled_task):
    factory, task_id, batch = cancelled_task
    connection = _connection(batch)
    provider = SimpleNamespace(get_ready_connection=Mock(return_value=None))
    assert await reconcile_cancelled_dispatches(factory, provider, task_id=task_id) == 0
    async with factory() as db:
        assert (await db.get(Task, task_id)).result_data["execution_recovery"]
    provider.get_ready_connection.return_value = connection
    assert await reconcile_cancelled_dispatches(factory, provider, task_id=task_id) == 0
    connection.rpc_call.assert_awaited_once_with(
        "tool.cancel",
        {
            "call_id": batch["tool_calls"][0]["call_id"],
            "scope": {"session_id": batch["session_id"], "turn_id": batch["turn_id"]},
        },
        timeout=5.0,
    )
    async with factory() as db:
        assert (await db.get(Task, task_id)).result_data["execution_recovery"]
    connection.fetch_tool_result.return_value["state"] = "terminal"
    assert await reconcile_cancelled_dispatches(factory, provider, task_id=task_id) == 1
    assert connection.rpc_call.await_args.args[0] == "tool.result_ack"
    async with factory() as db:
        assert not (await db.get(Task, task_id)).result_data["execution_recovery"]
    assert await reconcile_cancelled_dispatches(factory, provider, task_id=task_id) == 0


@pytest.mark.asyncio
async def test_cancelled_calls_keep_metadata_until_terminal_receipt_is_acknowledged(cancelled_task):
    factory, task_id, batch = cancelled_task
    connection = _connection(batch)
    connection.fetch_tool_result.return_value["state"] = "terminal"
    connection.rpc_call.side_effect = TimeoutError
    provider = SimpleNamespace(get_ready_connection=Mock(return_value=connection))
    assert await reconcile_cancelled_dispatches(factory, provider, task_id=task_id) == 0
    async with factory() as db:
        assert (await db.get(Task, task_id)).result_data["execution_recovery"]
    connection.rpc_call.side_effect = None
    assert await reconcile_cancelled_dispatches(factory, provider, task_id=task_id) == 1


@pytest.mark.asyncio
async def test_unresolved_cancellations_cannot_starve_later_tasks(cancelled_task):
    factory, task_id, batch = cancelled_task
    async with factory() as db:
        original = await db.get(Task, task_id)
        for _ in range(50):
            row = await create_task(
                db,
                task_id=f"0-{uuid4().hex}",
                title="Unresolved cancellation",
                agent_id=original.agent_id,
                created_by=original.created_by,
                status="cancelled",
            )
            row.result_data = {"execution_recovery": {"batch": batch}}
        await db.commit()
    connection = _connection(batch)
    connection.fetch_tool_result.return_value["state"] = "terminal"
    provider = SimpleNamespace(get_ready_connection=Mock(return_value=connection))
    assert await reconcile_cancelled_dispatches(factory, provider) == 1
    connection.rpc_call.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["status", "attempt", "turn", "session", "origin"])
async def test_cancellation_is_fenced_to_cancelled_attempt_and_step_scope(cancelled_task, change):
    factory, task_id, batch = cancelled_task
    connection = _connection(batch)
    async with factory() as db:
        task = await db.get(Task, task_id)
        if change == "status":
            task.status = "running"
        elif change == "attempt":
            task.attempt_number = 2
        elif change == "turn":
            batch = {**batch, "turn_id": uuid4().hex}
        elif change == "session":
            batch = {**batch, "session_id": uuid4().hex}
        else:
            connection.fetch_tool_result.return_value["accepted_executor_instance_id"] = uuid4().hex
        task.result_data = {"execution_recovery": {"batch": batch}}
        await db.commit()
    provider = SimpleNamespace(get_ready_connection=Mock(return_value=connection))
    assert await reconcile_cancelled_dispatches(factory, provider, task_id=task_id) == 0
    connection.rpc_call.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("preserve_current", [True, False])
async def test_terminal_business_snapshot_cannot_resurrect_or_drop_dispatch_metadata(
    cancelled_task, preserve_current
):
    factory, task_id, batch = cancelled_task
    async with factory() as db:
        task = await db.get(Task, task_id)
        task.status = "running"
        task.result_data = {"execution_recovery": {"batch": batch}} if preserve_current else {}
        await db.commit()
        assert await update_task_status(
            db,
            task_id,
            "completed",
            result_data={"final_content": "completed", "execution_recovery": {"stale": batch}},
        )
        await db.commit()
    async with factory() as db:
        data = (await db.get(Task, task_id)).result_data
        assert data["final_content"] == "completed"
        assert data.get("execution_recovery", {}) == ({"batch": batch} if preserve_current else {})
