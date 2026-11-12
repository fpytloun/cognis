"""Cold nested recovery through real task fences, stores and agent run steps."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import select, text, update
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from cognis.core.agent_loop import (
    AgentLoop,
    PauseWaiter,
    SessionLock,
    StepInterrupted,
    _append_tool_call_event,
)
from cognis.core.events import EventBus
from cognis.core.task_execution import (
    StaleTaskExecutionOwner,
    TaskExecutionFence,
    TaskExecutionStore,
    delegation_arguments_binding,
)
from cognis.core.task_recovery_errors import TaskRecoveryBlocked
from cognis.core.task_tool_recovery import recover_task_tools
from cognis.models.session import EventAppendResult, EventReadResult, SessionModel
from cognis.models.tool import ToolCall, ToolResult
from cognis.models.workflow import StepOutput
from cognis.store.database import create_engine, create_session_factory
from cognis.store.models import Agent, Base, CoordinationLeaseRow, Session, Task, User
from cognis.store.queries import create_conversation, create_task
from cognis.tools.builtin.orchestration import DELEGATE_TOOL
from cognis.tools.registry import RegisteredTool, ToolRegistry
from tests.unit.test_agent_loop import _parallel_group_ctx

pytestmark = pytest.mark.integration


@pytest_asyncio.fixture(params=["sqlite", "postgres"])
async def database(request, tmp_path):
    admin = None
    schema = f"nested_recovery_{uuid4().hex}"
    if request.param == "postgres":
        url = os.environ.get("COGNIS_TEST_POSTGRES_URL")
        if not url:
            pytest.skip("COGNIS_TEST_POSTGRES_URL is not configured")
        admin = create_async_engine(url)
        async with admin.begin() as db:
            await db.execute(text(f'CREATE SCHEMA "{schema}"'))
        engine = create_async_engine(url, connect_args={"server_settings": {"search_path": schema}})
        async with engine.begin() as db:
            await db.run_sync(Base.metadata.create_all)
        factory = async_sessionmaker(engine, expire_on_commit=False)
    else:
        engine = create_engine(f"sqlite+aiosqlite:///{tmp_path / 'nested.db'}")
        async with engine.begin() as db:
            await db.run_sync(Base.metadata.create_all)
        factory = create_session_factory(engine)
    async with factory() as db:
        db.add(User(email="user@example.com", name="User", role="admin"))
        await db.flush()
        db.add(Agent(agent_id="primary", owner_email="user@example.com", name="Primary"))
        await db.commit()
    try:
        yield factory
    finally:
        await engine.dispose()
        if admin is not None:
            async with admin.begin() as db:
                await db.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
            await admin.dispose()


async def _seed(database):
    task_id, parent_id, call_id, turn_id = (uuid4().hex for _ in range(4))
    conversation_id = uuid4().hex
    async with database() as db:
        await create_task(
            db,
            task_id=task_id,
            title="Nested recovery",
            description="",
            created_by="user@example.com",
            agent_id="primary",
            status="ready",
        )
        await create_conversation(
            db,
            user_email="user@example.com",
            agent_id="primary",
            context_type="web",
            conversation_id=conversation_id,
        )
        await db.flush()
        db.add(
            Session(
                session_id=parent_id,
                intaris_session_id=parent_id,
                conversation_id=conversation_id,
                user_email="user@example.com",
                agent_id="primary",
            )
        )
        await db.commit()
    return task_id, parent_id, call_id, turn_id, conversation_id


async def _claim(database, task_id, owner):
    store = TaskExecutionStore(
        database,
        owner_id=owner,
        max_active_global=1,
        max_active_per_agent=1,
        ttl_seconds=300,
    )
    async with database() as db:
        status = await db.scalar(select(Task.status).where(Task.task_id == task_id))
    claim = (
        await store.claim_ready()
        if status == "ready"
        else await store.claim_existing(task_id, statuses={"running"})
    )
    assert claim is not None
    assert claim.task_id == task_id
    return TaskExecutionFence(store, claim, asyncio.Event())


async def _expire(database):
    async with database() as db:
        await db.execute(
            update(CoordinationLeaseRow).values(
                lease_expires_at=datetime.now(UTC) - timedelta(seconds=1)
            )
        )
        await db.commit()


async def _reserve(fence, parent_id, turn_id, call_id):
    return await fence.reserve_delegation(
        session_id=parent_id, turn_id=turn_id, call_id=call_id, binding="binding-digest"
    )


async def _child(database, ids, child_id, child_turn):
    _, parent_id, call_id, turn_id, conversation_id = ids
    async with database() as db:
        db.add(
            Session(
                session_id=child_id,
                intaris_session_id=child_id,
                parent_session_id=parent_id,
                conversation_id=conversation_id,
                user_email="user@example.com",
                agent_id="primary",
                delegation_metadata={
                    "task_call": {
                        "session_id": parent_id,
                        "turn_id": turn_id,
                        "call_id": call_id,
                        "child_turn_id": child_turn,
                        "binding": "binding-digest",
                    }
                },
            )
        )
        await db.commit()


@pytest.mark.asyncio
async def test_task_followup_backlog_does_not_starve_interactive_recovery(database):
    from cognis.core.followups import FollowUpPolicy
    from cognis.store.models import Conversation, FollowUpDedupeRow, FollowUpIntentRow, StepRun
    from tests.unit.test_turn_scheduler import _scheduler_for_redo_invalidation

    task_id, parent_id, _, turn_id, task_conversation = await _seed(database)
    child_conversation, interactive_conversation = uuid4().hex, uuid4().hex
    now = datetime.now(UTC)
    interactive_intent_id = ""
    async with database() as db:
        (await db.get(Task, task_id)).status = "paused"
        (await db.get(Conversation, task_conversation)).active_session_id = parent_id
        db.add(
            StepRun(
                step_run_id=turn_id,
                task_id=task_id,
                step_name="execute",
                step_type="run",
                agent_id="primary",
                status="paused",
                session_id=parent_id,
                conversation_id=task_conversation,
                attempt_number=1,
            )
        )
        for conversation_id in (child_conversation, interactive_conversation):
            await create_conversation(
                db,
                user_email="user@example.com",
                agent_id="primary",
                context_type="web",
                conversation_id=conversation_id,
            )
        for index in range(101):
            conversation_id = (
                task_conversation
                if index < 50
                else child_conversation
                if index < 100
                else interactive_conversation
            )
            follow_up = FollowUpPolicy().build_background_tool_follow_up(
                conversation_id=conversation_id,
                shell_id=uuid4().hex,
                executor_id="test-executor",
                executor_type="websocket",
                status="completed",
                exit_code=0,
                command=None,
                description=None,
                runtime_seconds=2,
                output_tail=None,
            )
            payload = {
                "conversation_id": conversation_id,
                "user_email": "user@example.com",
                "follow_up": follow_up.model_dump(mode="json"),
            }
            if 50 <= index < 100:
                payload["task_owner"] = {
                    "task_id": task_id,
                    "attempt_number": 1,
                    "step_run_id": turn_id,
                }
            intent_id = uuid4().hex
            if index == 100:
                interactive_intent_id = intent_id
            db.add(
                FollowUpIntentRow(
                    intent_id=intent_id,
                    conversation_id=conversation_id,
                    follow_up_id=follow_up.follow_up_id,
                    event_payload=payload,
                    status="pending",
                    created_at=now - timedelta(seconds=101 - index),
                    updated_at=now - timedelta(seconds=101 - index),
                )
            )
            db.add(
                FollowUpDedupeRow(
                    dedupe_key=f"{conversation_id}:{follow_up.follow_up_id}",
                    conversation_id=conversation_id,
                    follow_up_id=follow_up.follow_up_id,
                    status="pending",
                    expires_at=now + timedelta(days=1),
                )
            )
        await db.commit()
    scheduler = _scheduler_for_redo_invalidation(database)
    scheduler.submit_turn = AsyncMock(return_value=None)
    await scheduler.recover_follow_up_intents()
    async with database() as db:
        rows = (await db.scalars(select(FollowUpIntentRow))).all()
        assert (await db.get(FollowUpIntentRow, interactive_intent_id)).status == "admitted"
        assert all(
            row.status == "pending" for row in rows if row.intent_id != interactive_intent_id
        )
        assert (await db.get(Task, task_id)).status == "paused"


@pytest.mark.asyncio
async def test_task_followup_stays_pending_across_rotation_takeover_and_duplicate_delivery(
    database,
):
    from cognis.core.events import Event, EventType
    from cognis.core.followups import FollowUpPolicy
    from cognis.core.task_execution import bind_task_execution_fence, reset_task_execution_fence
    from cognis.core.task_followups import acknowledge_task_follow_up, pending_task_follow_ups
    from cognis.store.models import Conversation, DirectTurnRequestRow, FollowUpIntentRow, StepRun
    from tests.unit.test_turn_scheduler import _scheduler_for_redo_invalidation

    task_id, parent_id, call_id, turn_id, conversation_id = await _seed(database)
    fence = await _claim(database, task_id, "controller-one")
    async with database() as db:
        db.add(
            StepRun(
                step_run_id=turn_id,
                task_id=task_id,
                step_name="execute",
                step_type="run",
                agent_id="primary",
                status="running",
                session_id=parent_id,
                conversation_id=conversation_id,
                attempt_number=1,
            )
        )
        (await db.get(Conversation, conversation_id)).active_session_id = parent_id
        await db.commit()
    scheduler = _scheduler_for_redo_invalidation(database)
    follow_up = FollowUpPolicy().build_background_tool_follow_up(
        conversation_id=conversation_id,
        shell_id=uuid4().hex,
        executor_id="test-executor",
        executor_type="websocket",
        status="completed",
        exit_code=0,
        command=None,
        description=None,
        runtime_seconds=2,
        output_tail="Qualification completed",
    )
    event = Event(
        type=EventType.FOLLOW_UP_TURN_REQUESTED,
        data={
            "conversation_id": conversation_id,
            "origin_session_id": parent_id,
            "origin_turn_id": turn_id,
            "follow_up": follow_up.model_dump(mode="json"),
        },
    )
    await scheduler._event_bus.publish(event)
    await scheduler._event_bus.publish(event)
    successor_id = uuid4().hex
    async with database() as db:
        db.add(
            Session(
                session_id=successor_id,
                intaris_session_id=successor_id,
                previous_session_id=parent_id,
                conversation_id=conversation_id,
                user_email="user@example.com",
                agent_id="primary",
            )
        )
        await db.flush()
        (await db.get(StepRun, turn_id)).session_id = successor_id
        (await db.get(Conversation, conversation_id)).active_session_id = successor_id
        intents = (await db.execute(select(FollowUpIntentRow))).scalars().all()
        assert len(intents) == 1 and intents[0].status == "pending"
        assert intents[0].event_payload["task_owner"] == {
            "task_id": task_id,
            "attempt_number": 1,
            "step_run_id": turn_id,
        }
        assert (await db.execute(select(DirectTurnRequestRow))).scalars().all() == []
        await db.commit()
    error = await scheduler.submit_turn(
        conversation_id,
        "",
        user_email="user@example.com",
        follow_up=follow_up,
    )
    assert error.code == "task_owned_follow_up"
    await _expire(database)
    successor = await _claim(database, task_id, "controller-two")
    token = bind_task_execution_fence(fence)
    try:
        async with database() as db:
            with pytest.raises(StaleTaskExecutionOwner):
                await pending_task_follow_ups(
                    db,
                    task_id=task_id,
                    attempt_number=1,
                    step_run_id=turn_id,
                )
    finally:
        reset_task_execution_fence(token)
    token = bind_task_execution_fence(successor)
    try:
        async with database() as db:
            items = await pending_task_follow_ups(
                db,
                task_id=task_id,
                attempt_number=1,
                step_run_id=turn_id,
            )
        assert len(items) == 1
        assert items[0]["follow_up"].output_summary == "Qualification completed"
        async with database() as db:
            await acknowledge_task_follow_up(db, items[0])
            await db.commit()
        await scheduler._event_bus.publish(event)
        async with database() as db:
            assert (
                await pending_task_follow_ups(
                    db,
                    task_id=task_id,
                    attempt_number=1,
                    step_run_id=turn_id,
                )
                == []
            )
            assert (await db.execute(select(DirectTurnRequestRow))).scalars().all() == []
        await successor.record_tool_dispatch(
            session_id=parent_id,
            turn_id=turn_id,
            descriptors=[{"call_id": call_id, "tool_name": "delegate"}],
        )
        child_id, child_turn = await successor.reserve_delegation(
            session_id=parent_id,
            turn_id=turn_id,
            call_id=call_id,
            binding=delegation_arguments_binding({}),
            step_run_id=turn_id,
        )
        await _child(
            database,
            (task_id, parent_id, call_id, turn_id, conversation_id),
            child_id,
            child_turn,
        )
        child_notice = FollowUpPolicy().build_background_tool_follow_up(
            conversation_id=conversation_id,
            shell_id=uuid4().hex,
            executor_id="test-executor",
            executor_type="websocket",
            command=None,
            description=None,
            status="completed",
            exit_code=0,
            runtime_seconds=2,
            output_tail="Child job finished",
        )
        await scheduler._event_bus.publish(
            Event(
                type=EventType.FOLLOW_UP_TURN_REQUESTED,
                data={
                    "conversation_id": conversation_id,
                    "origin_session_id": child_id,
                    "origin_turn_id": child_turn,
                    "follow_up": child_notice.model_dump(mode="json"),
                },
            )
        )
        rotated_child_id = uuid4().hex
        async with database() as db:
            old_child = await db.get(Session, child_id)
            old_child.status = "completed"
            db.add(
                Session(
                    session_id=rotated_child_id,
                    intaris_session_id=rotated_child_id,
                    agent_id=old_child.agent_id,
                    user_email=old_child.user_email,
                    conversation_id=conversation_id,
                    parent_session_id=parent_id,
                    previous_session_id=child_id,
                    delegation_metadata=old_child.delegation_metadata,
                    status="active",
                )
            )
            await db.commit()
        async with database() as db:
            assert (
                await pending_task_follow_ups(
                    db,
                    task_id=task_id,
                    attempt_number=1,
                    step_run_id=turn_id,
                    consumer_session_id=successor_id,
                )
                == []
            )
            child_items = await pending_task_follow_ups(
                db,
                task_id=task_id,
                attempt_number=1,
                step_run_id=turn_id,
                consumer_session_id=rotated_child_id,
            )
            assert len(child_items) == 1
            assert child_items[0]["follow_up"].output_summary == "Child job finished"
            await acknowledge_task_follow_up(db, child_items[0])
            await db.commit()
    finally:
        reset_task_execution_fence(token)


@pytest.mark.asyncio
async def test_failed_retry_retains_unsettled_calls_and_rejects_fresh_attempt(database):
    from cognis.core.task_queue import TaskQueue

    task_id, parent_id, call_id, turn_id, _ = await _seed(database)
    fence = await _claim(database, task_id, "controller-one")
    await fence.record_tool_dispatch(
        session_id=parent_id,
        turn_id=turn_id,
        descriptors=[{"call_id": call_id, "tool_name": "bash", "dispatch_state": "sent"}],
    )
    before = await fence.recovery_batches()
    async with database() as db:
        task = await db.get(Task, task_id)
        task.status = "failed"
        task.result_data = {**task.result_data, "error": "stale business result"}
        await db.commit()
    await fence.store.release(fence.claim)
    queue = TaskQueue(
        session_factory=database,
        workflow_engine=SimpleNamespace(),
        workflow_registry=SimpleNamespace(),
        event_bus=EventBus(),
    )
    queue._launch_claimed_task_run = lambda *_: None
    with pytest.raises(ValueError, match="recovery_required"):
        await queue.retry_failed_task(task_id, preserve_step_context=False)
    async with database() as db:
        task = await db.get(Task, task_id)
        assert task.status == "failed" and task.attempt_number == 1
        assert task.result_data["execution_recovery"] == before
    retried = await queue.retry_failed_task(task_id)
    assert retried.attempt_number == 1 and retried.workflow_state.resume_failed_step
    async with database() as db:
        task = await db.get(Task, task_id)
        assert task.status == "running"
        assert task.result_data == {"execution_recovery": before}


@pytest.mark.asyncio
async def test_stale_retry_claim_cannot_resurrect_or_clear_the_failed_task(database):
    from cognis.core.task_queue import TaskQueue

    task_id, parent_id, _, turn_id, _ = await _seed(database)
    fence = await _claim(database, task_id, "controller-one")
    await fence.record_tool_dispatch(
        session_id=parent_id,
        turn_id=turn_id,
        descriptors=[{"call_id": uuid4().hex, "tool_name": "bash", "dispatch_state": "sent"}],
    )
    async with database() as db:
        row = await db.get(Task, task_id)
        evidence = row.result_data["execution_recovery"]
        row.status = "failed"
        await db.commit()
    await fence.store.release(fence.claim)
    queue = TaskQueue(
        session_factory=database,
        workflow_engine=SimpleNamespace(
            deliver_task_outcome=AsyncMock(),
            _pause_waiter=PauseWaiter(),
        ),
        workflow_registry=SimpleNamespace(),
        event_bus=EventBus(),
    )
    claimed, proceed = asyncio.Event(), asyncio.Event()
    original_claim = queue._execution_store.claim_existing

    async def gated_claim(*args, **kwargs):
        result = await original_claim(*args, **kwargs)
        claimed.set()
        await proceed.wait()
        return result

    queue._execution_store.claim_existing = gated_claim
    retry = asyncio.create_task(queue.retry_failed_task(task_id, preserve_step_context=True))
    successor = TaskExecutionStore(
        database,
        owner_id="controller-successor",
        max_active_global=1,
        max_active_per_agent=1,
        ttl_seconds=300,
    )
    try:
        await claimed.wait()
        await _expire(database)
        replacement = await successor.claim_existing(task_id, statuses={"failed"})
    finally:
        proceed.set()
    with pytest.raises(ValueError, match="ownership changed"):
        await retry
    assert replacement is not None
    await successor.renew(replacement)
    async with database() as db:
        row = await db.get(Task, task_id)
        assert row.status == "failed" and row.attempt_number == 1
        assert row.result_data["execution_recovery"] == evidence


@pytest.mark.asyncio
@pytest.mark.parametrize("proof_kind", ["complete", "legacy", "cross_turn", "stale_attempt"])
async def test_orphan_attachment_requires_current_same_turn_provenance_and_can_cancel(
    database,
    proof_kind,
):
    from cognis.core.task_execution import delegation_arguments_binding
    from cognis.core.task_queue import TaskQueue
    from cognis.models.session import SessionModel
    from cognis.store.models import StepRun

    ids = await _seed(database)
    task_id, parent_id, call_id, turn_id, conversation_id = ids
    fence = await _claim(database, task_id, "controller-one")
    arguments = {
        "agent_id": "system:explore",
        "task": "Review",
        "context": "",
        "expected_output": "Findings",
    }
    binding = delegation_arguments_binding(arguments)
    await fence.record_tool_dispatch(
        session_id=parent_id,
        turn_id=turn_id,
        descriptors=[{"call_id": call_id, "tool_name": "delegate"}],
    )
    child_id, child_turn = await fence.reserve_delegation(
        session_id=parent_id,
        turn_id=turn_id,
        call_id=call_id,
        binding=binding,
        step_run_id=turn_id,
    )
    await _child(database, ids, child_id, child_turn)
    read_id = uuid4().hex
    await fence.record_tool_dispatch(
        session_id=child_id,
        turn_id=child_turn,
        descriptors=[{"call_id": read_id, "tool_name": "read", "dispatch_state": "sent"}],
    )
    await fence.complete_tool_dispatch(
        session_id=parent_id,
        turn_id=turn_id,
        call_ids=[call_id],
    )
    async with database() as db:
        db.add(
            StepRun(
                step_run_id=turn_id,
                task_id=task_id,
                step_name="execute",
                step_type="run",
                agent_id="primary",
                status="running",
                session_id=parent_id,
                conversation_id=conversation_id,
                attempt_number=1,
            )
        )
        child = await db.get(Session, child_id)
        proof = {
            **child.delegation_metadata["task_call"],
            "binding": binding,
            "child_session_id": child_id,
            "step_run_id": turn_id,
            "task_id": task_id,
            "attempt_number": 2 if proof_kind == "stale_attempt" else 1,
        }
        if proof_kind == "legacy":
            proof.pop("task_id")
            proof.pop("attempt_number")
        child.delegation_metadata = {
            "task_call": proof,
            "task_runtime_fingerprint": "fixture-runtime",
            "task_execution_turn_id": child_turn,
        }
        if proof_kind != "complete":
            child.status = "failed"
            child.result_summary = "Cancelled by parent session"
        await db.commit()
        parent = SessionModel(
            session_id=parent_id,
            intaris_session_id=parent_id,
            conversation_id=conversation_id,
            user_email="user@example.com",
            agent_id="primary",
        )
    ledger = _Ledger(child_id, parent_id)
    ledger.events[parent_id] = [
        {
            "seq": 1,
            "type": "tool_call",
            "data": {
                "call_id": call_id,
                "name": "delegate",
                "arguments": arguments,
                "turn_id": uuid4().hex if proof_kind == "cross_turn" else turn_id,
            },
        }
    ]
    ctx = _parallel_group_ctx()
    ctx.session, ctx.turn_id, ctx.task_id, ctx.execution_fence = parent, turn_id, task_id, fence
    from cognis.tools.builtin.orchestration import DELEGATE_TOOL
    from cognis.tools.registry import RegisteredTool, ToolRegistry

    ctx.tool_registry = ToolRegistry()
    ctx.tool_registry.register(RegisteredTool(definition=DELEGATE_TOOL))
    loop = SimpleNamespace(
        _session_factory=database,
        providers=SimpleNamespace(guardrails=ledger),
    )
    if proof_kind == "complete":
        # Stop at canonical dispatch after the durable attachment was restored;
        # physical settlement itself is covered by the nested run-step matrix.
        async def suspend(*args, **kwargs):
            from cognis.core.runtime import TransientExecutorUnavailable

            raise TransientExecutorUnavailable("Executor reconnecting")

        loop._handle_orchestration_tool = suspend
        from cognis.core.runtime import TransientExecutorUnavailable

        with pytest.raises(TransientExecutorUnavailable, match="Executor reconnecting"):
            await recover_task_tools(loop, ctx, await fence.recovery_batches())
        recovered = await fence.recovery_batches()
        restored = recovered[f"{parent_id}:{turn_id}"]["tool_calls"][0]
        assert restored["child_session_id"] == child_id
        assert restored["child_turn_id"] == child_turn
    else:
        with pytest.raises(TaskRecoveryBlocked, match="Cancel|cancel"):
            await recover_task_tools(loop, ctx, await fence.recovery_batches())
    queue = TaskQueue(
        session_factory=database,
        workflow_engine=SimpleNamespace(
            deliver_task_outcome=AsyncMock(),
            _pause_waiter=PauseWaiter(),
        ),
        workflow_registry=SimpleNamespace(),
        event_bus=EventBus(),
    )
    cancelled = await queue.cancel_task(task_id, expected_attempt=1)
    assert cancelled.status == "cancelled"
    if proof_kind == "complete":
        assert await fence.cancel_child_if_requested(child_id)
    async with database() as db:
        assert (await db.get(Session, child_id)).status == (
            "cancelled" if proof_kind == "complete" else "failed"
        )
        assert (
            len(
                (await db.execute(select(Session).where(Session.parent_session_id == parent_id)))
                .scalars()
                .all()
            )
            == 1
        )


@pytest.mark.asyncio
async def test_reservation_survives_two_takeovers_and_rejects_late_owner(database):
    ids = await _seed(database)
    task_id, parent_id, call_id, turn_id, _ = ids
    fence = await _claim(database, task_id, "controller-one")
    await fence.record_tool_dispatch(
        session_id=parent_id,
        turn_id=turn_id,
        descriptors=[{"call_id": call_id, "tool_name": "delegate"}],
    )
    child_id, child_turn = await _reserve(fence, parent_id, turn_id, call_id)
    await _child(database, ids, child_id, child_turn)
    await fence.bind_child_runtime(child_id, "original-child-authority")
    for owner in ("controller-two", "controller-three"):
        old = fence
        await _expire(database)
        fence = await _claim(database, task_id, owner)
        assert await _reserve(fence, parent_id, turn_id, call_id) == (child_id, child_turn)
        with pytest.raises(StaleTaskExecutionOwner):
            await old.complete_tool_dispatch(
                session_id=parent_id, turn_id=turn_id, call_ids=[call_id]
            )
        await fence.bind_child_runtime(child_id, "original-child-authority")
    with pytest.raises(TaskRecoveryBlocked, match="binding"):
        await fence.reserve_delegation(
            session_id=parent_id, turn_id=turn_id, call_id=call_id, binding="changed-input"
        )
    with pytest.raises(TaskRecoveryBlocked, match="scope changed"):
        await fence.bind_child_runtime(child_id, "broader-authority")
    assert not await fence.cancel_child_if_requested(child_id)
    async with database() as db:
        assert (
            await db.scalar(select(Session.status).where(Session.session_id == child_id))
            == "active"
        )
        task = await db.get(Task, task_id)
        task.status = "cancelled"
        await db.commit()
    assert await fence.cancel_child_if_requested(child_id)
    async with database() as db:
        assert (
            await db.scalar(select(Session.status).where(Session.session_id == child_id))
            == "cancelled"
        )
        assert (
            len(
                (await db.execute(select(Session).where(Session.parent_session_id == parent_id)))
                .scalars()
                .all()
            )
            == 1
        )


@pytest.mark.asyncio
async def test_child_rotation_preserves_reservation_scope(database):
    from cognis.core.session import SessionManager, _to_session_model

    ids = await _seed(database)
    task_id, parent_id, call_id, turn_id, _ = ids
    fence = await _claim(database, task_id, "controller-one")
    await fence.record_tool_dispatch(
        session_id=parent_id,
        turn_id=turn_id,
        descriptors=[{"call_id": call_id, "tool_name": "delegate"}],
    )
    child_id, child_turn = await _reserve(fence, parent_id, turn_id, call_id)
    await _child(database, ids, child_id, child_turn)
    await fence.bind_child_runtime(child_id, "original-grant")
    successor_id = uuid4().hex
    async with database() as db:
        original = await db.get(Session, child_id)
        values = {
            column.name: getattr(original, column.name) for column in Session.__table__.columns
        }
        values.update(
            session_id=successor_id, intaris_session_id=successor_id, previous_session_id=child_id
        )
        db.add(Session(**values))
        original_model = _to_session_model(original)
        original.status = "completed"
        original.completion_reason = "compacted"
        await db.commit()
    manager = SessionManager(database, SimpleNamespace(), SimpleNamespace())
    latest = await manager.resolve_reserved_child_continuation(original_model)
    assert latest.session_id == successor_id
    await fence.bind_child_runtime(latest.session_id, "original-grant")
    async with database() as db:
        successor = await db.get(Session, successor_id)
        successor.parent_session_id = child_id
        await db.commit()
    with pytest.raises(TaskRecoveryBlocked, match="scope"):
        await manager.resolve_reserved_child_continuation(original_model)
    with pytest.raises(TaskRecoveryBlocked, match="unavailable"):
        await fence.bind_child_runtime(successor_id, "original-grant")


@pytest.mark.asyncio
async def test_recovery_pause_is_coherent_and_normal_cancellation_remains_available(database):
    from cognis.core.task_execution import bind_task_execution_fence, reset_task_execution_fence
    from cognis.core.task_queue import TaskQueue
    from cognis.models.task import TaskModel, TaskStatus
    from cognis.models.workflow import CompletionConfig, StepDefinition, Workflow, WorkflowState
    from cognis.store.models import StepRun
    from tests.unit.test_workflow_engine_runtime import _build_engine

    task_id, parent_id, _, turn_id, conversation_id = await _seed(database)
    fence = await _claim(database, task_id, "controller-one")
    async with database() as db:
        db.add(
            StepRun(
                step_run_id=turn_id,
                task_id=task_id,
                step_name="execute",
                step_type="run",
                agent_id="primary",
                status="running",
                session_id=parent_id,
                conversation_id=conversation_id,
                attempt_number=1,
            )
        )
        await db.commit()
    state = WorkflowState(current_step="execute", current_step_status="running")
    task = TaskModel(
        task_id=task_id,
        title="Recovery",
        agent_id="primary",
        created_by="user@example.com",
        status=TaskStatus.RUNNING,
        workflow_state=state,
    )
    workflow = Workflow(
        workflow_id="recovery",
        name="Recovery",
        steps=[
            StepDefinition(
                name="execute",
                type="run",
                prompt="Work",
                completion=CompletionConfig(evaluate=False),
            ),
        ],
    )
    engine = _build_engine()
    engine._session_factory = database
    engine._execute_run_step = AsyncMock(
        side_effect=TaskRecoveryBlocked(
            "Canonical parent call turn cannot be proven; cancel and start a fresh run."
        )
    )
    token = bind_task_execution_fence(fence)
    try:
        result = await engine.execute_workflow(task, workflow)
    finally:
        reset_task_execution_fence(token)
    assert result.status == TaskStatus.PAUSED
    async with database() as db:
        row = await db.get(Task, task_id)
        assert row.status == "paused"
        assert row.workflow_state["status"] == "paused"
        assert row.workflow_state["current_step_status"] == "paused"
        assert (await db.get(StepRun, turn_id)).status == "paused"
        assert "cannot be proven" in row.result_data["recovery_blocked"]["reason"]
    queue = TaskQueue(
        session_factory=database,
        workflow_engine=SimpleNamespace(
            deliver_task_outcome=AsyncMock(),
            _pause_waiter=PauseWaiter(),
        ),
        workflow_registry=SimpleNamespace(),
        event_bus=EventBus(),
    )
    cancelled = await queue.cancel_task(task_id, expected_attempt=1)
    assert cancelled.status == TaskStatus.CANCELLED


class _Ledger:
    def __init__(self, child_id, parent_id):
        self.events = {}
        self.keys = set()
        self.interrupt = {child_id, parent_id}

    async def read_events(self, session_id, *, after_seq=0, limit=500, **kwargs):
        events = self.events.get(session_id, [])
        return EventReadResult(
            events=[e for e in events if e["seq"] > after_seq][:limit],
            last_seq=len(events),
            has_more=False,
        )

    async def record_events(self, session_id, events, *, idempotency_key=None, **kwargs):
        stream = self.events.setdefault(session_id, [])
        key = session_id, idempotency_key
        if idempotency_key is None or key not in self.keys:
            if idempotency_key is not None:
                self.keys.add(key)
            for event in events:
                stream.append({**event.model_dump(mode="json"), "seq": len(stream) + 1})
            if session_id in self.interrupt and any(e.type == "tool_result" for e in events):
                self.interrupt.remove(session_id)
                raise asyncio.CancelledError()
        return EventAppendResult(
            ok=True,
            first_seq=max(1, len(stream) - len(events) + 1),
            last_seq=len(stream),
            count=len(events),
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "cause", ["shutdown", "pause", "lease_loss", "explicit_cancel", "explicit_denial"]
)
async def test_child_cancellation_owner_is_durable_task_not_process(database, cause):
    ids = await _seed(database)
    task_id, parent_id, call_id, turn_id, conversation_id = ids
    fence = await _claim(database, task_id, "controller-one")
    await fence.record_tool_dispatch(
        session_id=parent_id,
        turn_id=turn_id,
        descriptors=[{"call_id": call_id, "tool_name": "delegate"}],
    )
    child_id, child_turn = await _reserve(fence, parent_id, turn_id, call_id)
    await _child(database, ids, child_id, child_turn)
    if cause == "explicit_cancel":
        async with database() as db:
            task = await db.get(Task, task_id)
            task.status = "cancelled"
            await db.commit()
    loop = AgentLoop(
        providers=SimpleNamespace(),
        session_manager=AsyncMock(),
        session_cache=AsyncMock(),
        context_assembler=AsyncMock(),
        compaction_strategy=None,
        tool_router=SimpleNamespace(),
        remember_queue=None,
        event_bus=EventBus(),
        session_lock=SessionLock(),
        pause_waiter=None,
        session_factory=database,
    )
    interruption = (
        StaleTaskExecutionOwner(task_id) if cause == "lease_loss" else asyncio.CancelledError()
    )
    if cause in {"pause", "explicit_denial"}:
        interruption = StepInterrupted(
            "Input request interrupted", explicit_cancel=cause == "explicit_denial"
        )
    loop._resolve_child_agent = AsyncMock(side_effect=interruption)
    loop._record_parent_session_events = AsyncMock()
    ctx = _parallel_group_ctx()
    child = SessionModel(
        session_id=child_id,
        parent_session_id=parent_id,
        conversation_id=conversation_id,
        user_email="user@example.com",
        agent_id="primary",
    )
    with pytest.raises(type(interruption)):
        await loop._run_child_session(
            child_session=child,
            conversation=ctx.conversation,
            agent=ctx.agent,
            task_description="Recover child",
            parent_intaris_session_id=parent_id,
            execution_fence=fence,
        )
    async with database() as db:
        status = await db.scalar(select(Session.status).where(Session.session_id == child_id))
        assert status == (
            "cancelled" if cause in {"explicit_cancel", "explicit_denial"} else "active"
        )
    loop.session_manager.mark_cancelled.assert_not_awaited()
    loop.session_manager.mark_failed.assert_not_awaited()


@pytest.mark.asyncio
async def test_actual_nested_run_steps_settle_after_repeated_append_interruption(database):
    ids = await _seed(database)
    task_id, parent_id, call_id, turn_id, conversation_id = ids
    fence = await _claim(database, task_id, "controller-one")
    await fence.record_tool_dispatch(
        session_id=parent_id,
        turn_id=turn_id,
        descriptors=[{"call_id": call_id, "tool_name": "delegate"}],
    )
    child_id, child_turn = await _reserve(fence, parent_id, turn_id, call_id)
    await _child(database, ids, child_id, child_turn)
    read_id = uuid4().hex
    await fence.record_tool_dispatch(
        session_id=child_id,
        turn_id=child_turn,
        descriptors=[
            {
                "call_id": read_id,
                "tool_name": "read",
                "dispatch_state": "sent",
            }
        ],
    )
    await fence.bind_tool_dispatch(
        read_id,
        "test-executor",
        uuid4().hex,
        session_id=child_id,
        turn_id=child_turn,
        dispatch_state="sent",
    )
    ledger = _Ledger(child_id, parent_id)
    for sid, tid, cid, tool in (
        (parent_id, turn_id, call_id, "delegate"),
        (child_id, child_turn, read_id, "read"),
    ):
        ledger.events[sid] = [
            {
                "seq": 1,
                "type": "tool_call",
                "data": {
                    "turn_id": tid,
                    "call_id": cid,
                    "name": tool,
                    "arguments": {},
                },
            }
        ]
    receipt_reads = 0
    model_steps = []
    from cognis.models.tool import ToolDefinition, ToolSource
    from cognis.tools.builtin.orchestration import DELEGATE_TOOL
    from cognis.tools.registry import RegisteredTool, ToolRegistry

    for owner in ("controller-one", "controller-two", "controller-three"):
        if owner != "controller-one":
            await _expire(database)
            fence = await _claim(database, task_id, owner)
        loop = AgentLoop(
            providers=SimpleNamespace(guardrails=ledger),
            session_manager=AsyncMock(),
            session_cache=AsyncMock(),
            context_assembler=AsyncMock(),
            compaction_strategy=None,
            tool_router=SimpleNamespace(artifact_store=None),
            remember_queue=None,
            event_bus=EventBus(),
            session_lock=SessionLock(),
            pause_waiter=None,
            session_factory=database,
        )
        loop._refresh_orchestration_target_snapshot = AsyncMock()
        loop._load_session_todos = AsyncMock()
        loop._recover_pending_managed_join_handoffs = AsyncMock()
        loop._finalize_tool_result = AsyncMock(
            side_effect=lambda ctx, tc, registered, result: result
        )
        loop._retire_task_tool_receipts = AsyncMock()
        loop._resolve_target_connection = lambda **kwargs: None
        registry = ToolRegistry()
        registry.register(RegisteredTool(definition=DELEGATE_TOOL))
        registry.register(
            RegisteredTool(
                definition=ToolDefinition(
                    name="read",
                    description="Read fixture",
                    parameters={},
                    source=ToolSource(
                        type="intaris_mcp",
                        server_id="read-fixture",
                        server_name="fixture",
                        raw_tool_name="read",
                    ),
                )
            )
        )

        def context(sid, tid, *, fence=fence, registry=registry):
            ctx = _parallel_group_ctx()
            ctx.session = SessionModel(
                session_id=sid,
                intaris_session_id=sid,
                conversation_id=conversation_id,
                user_email="user@example.com",
                agent_id="primary",
            )
            ctx.turn_id, ctx.execution_fence, ctx.tool_registry = tid, fence, registry
            ctx.task_id = task_id if sid == parent_id else None
            return ctx

        async def receipt(*args, **kwargs):
            nonlocal receipt_reads
            receipt_reads += 1
            return "terminal", ToolResult(output="confirmed read receipt"), "receipt"

        async def model(ctx, *, fence=fence, **kwargs):
            own = [
                b
                for b in (await fence.recovery_batches()).values()
                if b["session_id"] == ctx.session.session_id
            ]
            assert not own
            model_steps.append(ctx.session.session_id)
            return StepOutput(summary="Completed")

        async def delegate(tc, *, ctx, events_to_record, fence=fence, loop=loop):
            assert await _reserve(fence, parent_id, turn_id, call_id) == (child_id, child_turn)
            output = await loop.run_step(context(child_id, child_turn))
            assert output is not None
            return ToolResult(output="confirmed child result")

        loop._execute_step = model
        loop._handle_orchestration_tool = delegate
        loop._reconcile_accepted_tool_call = receipt
        if owner != "controller-three":
            with pytest.raises(asyncio.CancelledError):
                await loop.run_step(context(parent_id, turn_id))
        else:
            assert await loop.run_step(context(parent_id, turn_id)) is not None
    assert await fence.recovery_batches() == {}
    assert receipt_reads == 1
    assert model_steps.count(child_id) == 1
    assert model_steps.count(parent_id) == 1
    for sid in (parent_id, child_id):
        assert sum(e["type"] == "tool_result" for e in ledger.events[sid]) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "history",
    ["canonical_long", "legacy_short", "legacy_title", "legacy_truncated", "legacy_changed"],
)
async def test_reserved_delegation_recovers_exact_input_from_canonical_history(database, history):
    ids = await _seed(database)
    task_id, parent_id, call_id, turn_id, _ = ids
    fence = await _claim(database, task_id, "controller-one")
    arguments = {
        "task": "Review the task-owned change",
        "agent_id": "system:code-review",
        "context": "Review context. " * (400 if history.endswith(("long", "truncated")) else 2),
        "expected_output": "Findings with file and line references",
    }
    if history == "legacy_title":
        arguments["title"] = "Explicit review title"
    binding = hashlib.sha256(
        json.dumps(arguments, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    await fence.record_tool_dispatch(
        session_id=parent_id,
        turn_id=turn_id,
        descriptors=[{"call_id": call_id, "tool_name": "delegate"}],
    )
    reserved = await fence.reserve_delegation(
        session_id=parent_id, turn_id=turn_id, call_id=call_id, binding=binding
    )
    events = []
    _append_tool_call_event(events, ToolCall(call_id=call_id, name="delegate", arguments=arguments))
    events[0].data["turn_id"] = turn_id
    if history.startswith("legacy"):
        events[0].data.pop("recovery_arguments", None)
    if history == "legacy_changed":
        events[0].data["arguments"]["task"] = "A different review"
    ledger = _Ledger(reserved[0], parent_id)
    ledger.interrupt.clear()
    await ledger.record_events(parent_id, events)
    recovered_identities = []

    async def rejoin(tc, **kwargs):
        recovered_binding = hashlib.sha256(
            json.dumps(tc.arguments, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        recovered_identities.append(
            await fence.reserve_delegation(
                session_id=parent_id, turn_id=turn_id, call_id=call_id, binding=recovered_binding
            )
        )
        assert tc.arguments == arguments
        return ToolResult(output="Reserved reviewer completed")

    loop = SimpleNamespace(
        providers=SimpleNamespace(guardrails=ledger),
        session_cache=SimpleNamespace(append_recorded_events=AsyncMock()),
        _handle_orchestration_tool=rejoin,
    )
    ctx = _parallel_group_ctx()
    ctx.session = SimpleNamespace(session_id=parent_id, intaris_session_id=parent_id)
    ctx.turn_id = turn_id
    ctx.execution_fence = fence
    ctx.tool_registry = ToolRegistry()
    ctx.tool_registry.register(RegisteredTool(definition=DELEGATE_TOOL))
    if history in {"legacy_truncated", "legacy_changed"}:
        with pytest.raises(TaskRecoveryBlocked, match="arguments are incomplete or changed"):
            await recover_task_tools(loop, ctx, await fence.recovery_batches())
        assert recovered_identities == []
        batches = await fence.recovery_batches()
        descriptor = next(iter(batches.values()))["tool_calls"][0]
        assert (descriptor["child_session_id"], descriptor["child_turn_id"]) == reserved
        assert all(event["type"] != "tool_result" for event in ledger.events[parent_id])
    else:
        await recover_task_tools(loop, ctx, await fence.recovery_batches())
        assert recovered_identities == [reserved]
        assert await fence.recovery_batches() == {}
        results = [e for e in ledger.events[parent_id] if e["type"] == "tool_result"]
        assert len(results) == 1
        assert results[0]["data"]["output"] == "Reserved reviewer completed"


@pytest.mark.asyncio
@pytest.mark.parametrize("receipt_state", ["absent", "indeterminate", "terminal"])
async def test_restarted_executor_resumes_agent_after_interrupted_result_settlement(
    database, tmp_path, receipt_state
):
    from cognis.executor.call_journal import CallJournal
    from cognis.models.agent import AgentDefinition
    from cognis.models.session import ConversationModel
    from cognis.models.workflow import StepDefinition
    from cognis.tools.executor.definitions import BASH_TOOL
    from cognis.tools.registry import RegisteredTool, ToolRegistry
    from tests.unit.test_agent_loop import (
        _step_complete_test_context,
        _step_complete_test_loop,
        _StepCompleteRetryLLM,
    )

    task_id, session_id, call_id, turn_id, conversation_id = await _seed(database)
    fence = await _claim(database, task_id, "controller-one")
    executor_id, accepted_instance, replacement = (uuid4().hex for _ in range(3))
    arguments = {"command": "printf 'once'"}
    await fence.record_tool_dispatch(
        session_id=session_id,
        turn_id=turn_id,
        descriptors=[{"call_id": call_id, "tool_name": "bash"}],
    )
    await fence.bind_tool_dispatch(
        call_id,
        executor_id,
        accepted_instance,
        session_id=session_id,
        turn_id=turn_id,
        dispatch_state="sent",
    )
    journal = CallJournal(tmp_path / "calls.sqlite3")
    if receipt_state != "absent":
        await journal.accept(
            {
                "call_id": call_id,
                "tool_name": "bash",
                "arguments": arguments,
                "runtime_metadata": {"session_id": session_id, "turn_id": turn_id},
            },
            accepted_instance,
        )
    if receipt_state == "terminal":
        await journal.complete(call_id, {"output": "Completed exactly once", "is_error": False})

    async def fetch(cid, *, scope, **kwargs):
        return await journal.fetch(cid, replacement, scope=scope, require_scope=True) or {
            "executor_instance_id": replacement,
            "state": "unknown",
        }

    connection = SimpleNamespace(
        executor_instance_id=replacement,
        capabilities=SimpleNamespace(durable_tool_receipts=True),
        fetch_tool_result=fetch,
        rpc_call=AsyncMock(),
    )
    websocket = SimpleNamespace(wait_for_connection=AsyncMock(return_value=connection))
    registry = ToolRegistry()
    registry.register(RegisteredTool(definition=BASH_TOOL))
    ledger = _Ledger(session_id, session_id)
    events = []
    _append_tool_call_event(events, ToolCall(call_id=call_id, name="bash", arguments=arguments))
    events[0].data["turn_id"] = turn_id
    await ledger.record_events(session_id, events)

    class RepeatingMutationLLM(_StepCompleteRetryLLM):
        async def stream_generate(self, messages, **kwargs):
            self.calls.append(messages)
            retry = len(self.calls) == 1
            yield {
                "choices": [
                    {
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": uuid4().hex,
                                    "function": {
                                        "name": "bash" if retry else "step_complete",
                                        "arguments": json.dumps(
                                            arguments if retry else {"summary": "Reconciled"}
                                        ),
                                    },
                                }
                            ]
                        }
                    }
                ]
            }

    for owner in ("controller-one", "controller-two"):
        if owner != "controller-one":
            await _expire(database)
            fence = await _claim(database, task_id, owner)
        llm = RepeatingMutationLLM({}, {})
        loop = _step_complete_test_loop(llm, session_factory=database)
        loop.providers.guardrails = ledger
        loop.providers.executor = SimpleNamespace(websocket=websocket)
        loop._refresh_orchestration_target_snapshot = AsyncMock()
        loop._load_session_todos = AsyncMock()
        loop._recover_pending_managed_join_handoffs = AsyncMock()
        loop._finalize_tool_result = AsyncMock(side_effect=lambda ctx, tc, reg, result: result)
        loop.tool_router.finalize_recovered_executor_result = AsyncMock(
            side_effect=lambda *, result, **kwargs: result
        )
        loop._resolve_target_connection = lambda **kwargs: connection
        loop.execute_controller_tool = AsyncMock(side_effect=AssertionError("Unsafe replay"))

        ctx = _step_complete_test_context(
            StepDefinition(name="recover", type="run", prompt="", require_deliverable=False)
        )
        ctx.session = SessionModel(
            session_id=session_id,
            intaris_session_id=session_id,
            conversation_id=conversation_id,
            user_email="user@example.com",
            agent_id="primary",
        )
        ctx.conversation = ConversationModel(
            conversation_id=conversation_id,
            user_email="user@example.com",
            agent_id="primary",
            context={"type": "web"},
        )
        ctx.agent = AgentDefinition(
            agent_id="primary", owner_email="user@example.com", name="Agent"
        )
        ctx.turn_id, ctx.execution_fence, ctx.tool_registry = turn_id, fence, registry
        ctx.task_id = task_id
        if owner == "controller-one":
            with pytest.raises(asyncio.CancelledError):
                await loop.run_step(ctx)
        else:
            output = await loop.run_step(ctx)
            assert output is not None and output.summary == "Reconciled"
            assert await fence.recovery_batches() == {}
            results = [
                e["data"]
                for e in ledger.events[session_id]
                if e["type"] == "tool_result" and e["data"]["call_id"] == call_id
            ]
            assert len(results) == 1
            result = results[0]
            if receipt_state == "terminal":
                assert result["output"] == "Completed exactly once"
                assert result["is_error"] is False
            else:
                assert result["is_error"] is True
                assert result["uncertain"] is True
                assert not ctx.same_turn_tool_call_ledger.already_executed("bash", arguments)
                payload = json.loads(result["output"])
                assert payload["safe_to_retry"] is False
                assert "read-only tools" in payload["reconciliation"]
                assert payload["executor_id"] == executor_id
            # The fresh agent sees the duplicate rejection, not a second execution.
            assert len(llm.calls) == 2
            assert any(
                "blocked_uncertain_outcome" in str(message.get("content"))
                if receipt_state != "terminal"
                else "duplicate_tool_call_same_turn_lineage" in str(message.get("content"))
                for message in llm.calls[1]
            )
