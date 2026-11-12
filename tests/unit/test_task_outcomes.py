from __future__ import annotations

import asyncio
import hashlib
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from sqlalchemy import func, select

from cognis.api.chat_v2.event_store import RawSessionEvent
from cognis.api.chat_v2.normalizer import normalize_session_events
from cognis.api.chat_v2.projector import project_timeline
from cognis.artifacts.store import ArtifactStore, ArtifactStoreConfig
from cognis.channels.delivery import ChannelDeliveryService, ChannelDeliveryStatus
from cognis.core.agent_direct import agent_direct_context_ref
from cognis.core.agent_loop import PauseWaiter
from cognis.core.events import EventBus, EventType
from cognis.core.followups import FollowUpMode, FollowUpPolicy, render_follow_up_block
from cognis.core.prompts import PromptContext, build_follow_up_guidance
from cognis.core.task_outcomes import format_task_outcome, task_outcome_key
from cognis.core.task_queue import TaskQueue
from cognis.core.workflow_engine import WorkflowEngine
from cognis.models.channel import ChannelCapabilities
from cognis.models.session import EventAppendResult
from cognis.models.task import TaskDelivery, TaskModel, TaskStatus, validate_task_delivery
from cognis.models.workflow import CompletionDeliveryPolicy, WorkflowState
from cognis.store.app_channels import app_channel_id, ensure_app_channels
from cognis.store.database import create_engine, create_session_factory
from cognis.store.models import (
    Base,
    ChannelAccountRow,
    ChannelDeliveryOutboxRow,
    InboxNotificationRow,
)
from cognis.store.queries import (
    create_agent,
    create_channel_account,
    create_conversation,
    create_deliverable,
    create_schedule,
    create_session,
    create_task,
    create_user,
    get_channel_account,
    get_schedule,
    get_task,
    set_session_intaris_session_id,
    update_channel_account,
    update_conversation_active_session,
    update_schedule,
    update_task_fields,
)


@pytest_asyncio.fixture
async def outcome_runtime(tmp_path: Path):
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path / 'outcomes.db'}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = create_session_factory(engine)
    guardrails = SimpleNamespace(
        record_events=AsyncMock(
            return_value=EventAppendResult(ok=True, count=2, first_seq=1, last_seq=2)
        )
    )
    channel = SimpleNamespace(
        deliver_task_to_conversation=AsyncMock(return_value=ChannelDeliveryStatus.SENT),
    )
    events = []
    bus = EventBus()

    async def capture(event):
        events.append(event)

    bus.subscribe_all(capture)
    workflow = WorkflowEngine(
        session_factory=factory,
        providers=SimpleNamespace(guardrails=guardrails),
        agent_loop=SimpleNamespace(),
        step_evaluator=SimpleNamespace(),
        workflow_registry=SimpleNamespace(),
        session_manager=SimpleNamespace(),
        event_bus=bus,
        pause_waiter=PauseWaiter(),
        channel_delivery=channel,
    )
    async with factory() as db:
        await create_user(
            db, email="owner@example.com", name="Owner", password_hash="hash", role="user"
        )
        await create_agent(
            db, agent_id="agent-outcomes", owner_email="owner@example.com", name="Outcome agent"
        )
        app = await create_conversation(
            db,
            user_email="owner@example.com",
            agent_id="agent-outcomes",
            context_type="web",
            title="Main",
            context_ref=agent_direct_context_ref("owner@example.com", "agent-outcomes"),
        )
        external = await create_conversation(
            db,
            user_email="owner@example.com",
            agent_id="agent-outcomes",
            context_type="signal",
            context_ref="signal:signal-outcomes:chat-outcomes",
            context_data={
                "channel_type": "signal",
                "account_id": "signal-outcomes",
                "chat_id": "chat-outcomes",
            },
        )
        for conversation in (app, external):
            session = await create_session(
                db,
                conversation_id=conversation.conversation_id,
                user_email="owner@example.com",
                agent_id="agent-outcomes",
            )
            await set_session_intaris_session_id(
                db, session.session_id, f"intaris-{session.session_id}"
            )
            await update_conversation_active_session(
                db, conversation.conversation_id, session.session_id
            )
        await create_channel_account(
            db,
            account_id="signal-outcomes",
            user_email="owner@example.com",
            agent_id="agent-outcomes",
            channel_type="signal",
            display_name="Signal",
            default_conversation_id=external.conversation_id,
        )
        await db.commit()
    yield SimpleNamespace(
        engine=engine,
        factory=factory,
        workflow=workflow,
        guardrails=guardrails,
        channel=channel,
        events=events,
        bus=bus,
        app=app,
        external=external,
    )
    await engine.dispose()


def outcome_task(*, status="failed", mode="default", destination="preferred_channel", **kwargs):
    return TaskModel(
        task_id="task-outcomes",
        title="Restore service health",
        description="Check and restore the service",
        created_by="owner@example.com",
        agent_id="agent-outcomes",
        status=TaskStatus(status),
        delivery=TaskDelivery(mode=destination),
        completion_delivery=CompletionDeliveryPolicy(completion_mode_family=mode),
        completed_at=datetime(2026, 10, 26, 20, tzinfo=UTC),
        result_summary="Health check failed" if status == "failed" else "Execution ended",
        **kwargs,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("running", [False, True])
@pytest.mark.parametrize("mode", ["auto", "default", "direct"])
async def test_queue_cancellation_routes_once_after_worker_cleanup(outcome_runtime, running, mode):
    runtime = outcome_runtime
    queue = TaskQueue(
        session_factory=runtime.factory,
        workflow_engine=runtime.workflow,
        workflow_registry=SimpleNamespace(),
        event_bus=runtime.bus,
    )
    async with runtime.factory() as db:
        row = await create_task(
            db,
            task_id="cancel-outcome",
            title="Cancel outcome",
            description="Check cancellation delivery",
            created_by="owner@example.com",
            agent_id="agent-outcomes",
            status="running" if running else "queued",
            completion_mode_family=mode,
            delivery_mode="in_app_notifications" if mode == "direct" else "preferred_channel",
        )
        row.applied_completion_mode = "silent"
        await db.commit()
    stopped = asyncio.Event()
    if running:
        started = asyncio.Event()

        async def worker():
            try:
                started.set()
                await asyncio.Event().wait()
            finally:
                stopped.set()

        queue._active_runs["cancel-outcome"] = asyncio.create_task(worker())
        await started.wait()
    result = await queue.cancel_task("cancel-outcome")
    assert result.status == TaskStatus.CANCELLED
    assert not queue._pick_lock.locked()
    if running:
        assert stopped.is_set()
    await queue.cancel_task("cancel-outcome")
    assert len([event for event in runtime.events if event.type == EventType.TASK_CANCELLED]) == 1
    follow_ups = [
        event for event in runtime.events if event.type == EventType.FOLLOW_UP_TURN_REQUESTED
    ]
    assert len(follow_ups) == (0 if mode == "direct" else 1)
    async with runtime.factory() as db:
        notifications = list((await db.scalars(select(InboxNotificationRow))).all())
    assert len(notifications) == (1 if mode == "direct" else 0)


@pytest.mark.asyncio
@pytest.mark.parametrize("format_name", ["markdown", "rich"])
async def test_canonical_direct_transport_does_not_duplicate_outcome_wrapper(
    outcome_runtime, tmp_path, monkeypatch, format_name
):
    runtime = outcome_runtime
    store = ArtifactStore(ArtifactStoreConfig(path=str(tmp_path / "artifacts")))
    adapter = SimpleNamespace(
        capabilities=ChannelCapabilities(max_message_length=200),
        send_message=AsyncMock(return_value="report-sent"),
    )
    manager = SimpleNamespace(
        _artifact_store=store,
        find_adapter_for_channel=lambda *_: (adapter, object()),
    )
    service = ChannelDeliveryService(
        session_factory=runtime.factory,
        event_bus=runtime.bus,
        channel_manager_ref=lambda: manager,
    )
    monkeypatch.setattr(runtime.workflow, "_channel_delivery", service)
    async with runtime.factory() as db:
        deliverable = await create_deliverable(
            db,
            conversation_id=runtime.external.conversation_id,
            content="Canonical health report" if format_name == "markdown" else "Fallback summary",
            format=format_name,
            title="Health report",
            rich={"blocks": [{"type": "markdown", "content": "Canonical health report"}]}
            if format_name == "rich"
            else None,
            artifact_store=store,
        )
        await db.commit()
    task = outcome_task(
        status="completed",
        mode="direct",
        result_data={
            "final_deliverable_id": deliverable.deliverable_id,
            "final_content": "Canonical health report",
        },
    )
    task.delivery = TaskDelivery(
        mode="specific_conversation", target=runtime.external.conversation_id
    )
    await runtime.workflow._deliver_task_result(task)
    messages = [call.args[0] for call in adapter.send_message.call_args_list]
    rendered = "\n".join(message.content for message in messages)
    assert "Task completed:" not in rendered
    assert "Goal:" not in rendered
    assert rendered.count("Canonical health report") == 1
    assert all(len(message.content) <= 200 for message in messages)
    sent = len(messages)
    await runtime.workflow._deliver_task_result(task)
    assert adapter.send_message.await_count == sent
    async with runtime.factory() as db:
        rows = list((await db.scalars(select(ChannelDeliveryOutboxRow))).all())
    assert len(rows) == 1 and rows[0].status == "sent"
    assert rows[0].source_type == "task_outcome"


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["completed", "failed", "cancelled"])
@pytest.mark.parametrize("mode", ["auto", "default", "direct"])
@pytest.mark.parametrize("destination", ["web", "signal"])
async def test_terminal_delivery_matrix(outcome_runtime, status, mode, destination):
    runtime = outcome_runtime
    async with runtime.factory() as db:
        await update_channel_account(
            db,
            app_channel_id("owner@example.com", "agent-outcomes")
            if destination == "web"
            else "signal-outcomes",
            preferred_for_task_delivery=True,
        )
        await db.commit()
    task = outcome_task(status=status, mode=mode)
    await runtime.workflow._deliver_task_result(task)
    follow_ups = [
        event for event in runtime.events if event.type == EventType.FOLLOW_UP_TURN_REQUESTED
    ]
    agent_handling = mode == "default" or (mode == "auto" and status != "completed")
    assert len(follow_ups) == int(agent_handling)
    if agent_handling:
        payload = follow_ups[0].data["follow_up"]
        assert payload["mode"] == "handle"
        assert payload["required_action"] == "handle_outcome"
        assert payload["outcome_context"]["task_url"] == "/tasks/task-outcomes"
    elif destination == "signal":
        call = runtime.channel.deliver_task_to_conversation.call_args
        assert call.args[0] == runtime.external.conversation_id
        assert f"Task {status}:" in call.kwargs["content"]
        assert "/tasks/task-outcomes" not in call.kwargs["content"]
        assert "Inspect it before retrying" not in call.kwargs["content"]
        assert call.kwargs["outcome_key"] == task_outcome_key(task)
    else:
        recorded = runtime.guardrails.record_events.call_args.kwargs["events"]
        messages = [event for event in recorded if event.type == "assistant_message"]
        if status == "completed":
            assert len(messages) == 1
            assert "Task completed:" in messages[0].data["content"]
            assert messages[0].data["task_outcome"] == {
                "task_id": "task-outcomes",
                "title": "Restore service health",
                "status": "completed",
                "attempt_number": 1,
            }
            projected = project_timeline(
                normalize_session_events(
                    [
                        RawSessionEvent(
                            store_id="intaris",
                            session_id="outcome-session",
                            seq=1,
                            type="assistant_message",
                            data=messages[0].data,
                        )
                    ]
                ).events
            ).timeline.items
            assert projected[0].task_outcome.title == "Restore service health"
            assert projected[0].task_outcome.attempt_number == 1
        else:
            assert not messages
            assert recorded[0].data["task_outcome_card"] is True
            raw = [
                RawSessionEvent(
                    store_id="intaris",
                    session_id="outcome-session",
                    seq=i,
                    type=event.type,
                    data=event.data,
                )
                for i, event in enumerate(recorded, 1)
            ]
            items = project_timeline(normalize_session_events(raw).events).timeline.items
            assert len(items) == 1
            assert items[0].kind == "task"
            assert items[0].status == status
            assert items[0].attempt_number == 1
            # Unmarked historical lifecycle records must not become new cards.
            raw[0].data.pop("task_outcome_card")
            assert not project_timeline(normalize_session_events(raw).events).timeline.items
            legacy_text = f"Task {status}: Restore service health\nTask details and follow-up: /tasks/task-outcomes"
            raw.append(
                RawSessionEvent(
                    store_id="intaris",
                    session_id="outcome-session",
                    seq=2,
                    type="assistant_message",
                    data={"role": "assistant", "content": legacy_text, "task_id": "task-outcomes"},
                )
            )
            legacy_items = project_timeline(normalize_session_events(raw).events).timeline.items
            assert len(legacy_items) == 1
            assert legacy_items[0].content == legacy_text
            assert legacy_items[0].task_outcome is None
        runtime.channel.deliver_task_to_conversation.assert_not_called()
    expected = {
        "completed": EventType.TASK_COMPLETED,
        "failed": EventType.TASK_FAILED,
        "cancelled": EventType.TASK_CANCELLED,
    }[status]
    assert any(event.type == expected for event in runtime.events)


async def _create_outcome_deliverable(
    runtime, tmp_path, *, format_name="markdown", private=False, content="Canonical health report"
):
    store = ArtifactStore(ArtifactStoreConfig(path=str(tmp_path / "artifacts")))
    async with runtime.factory() as db:
        conversation_id = runtime.app.conversation_id
        if private:
            await create_user(
                db, email="other@example.com", name="Other", password_hash="hash", role="user"
            )
            await create_agent(
                db, agent_id="other-report-agent", owner_email="other@example.com", name="Other"
            )
            conversation = await create_conversation(
                db,
                user_email="other@example.com",
                agent_id="other-report-agent",
                context_type="web",
            )
            conversation_id = conversation.conversation_id
        deliverable = await create_deliverable(
            db,
            conversation_id=conversation_id,
            content=f"<p>{content}</p>" if format_name == "html" else content,
            format=format_name,
            title="Health report",
            rich={"blocks": [{"type": "markdown", "content": content}]}
            if format_name == "rich"
            else None,
            artifact_store=store,
        )
        await db.commit()
    return deliverable


@pytest.mark.asyncio
@pytest.mark.parametrize("legacy", [False, True])
@pytest.mark.parametrize("start_index", [0, 1])
async def test_canonical_outcome_retry_preserves_committed_projection(
    outcome_runtime, tmp_path, monkeypatch, legacy, start_index
):
    runtime = outcome_runtime
    store = ArtifactStore(ArtifactStoreConfig(path=str(tmp_path / "artifacts")))
    deliverable = await _create_outcome_deliverable(
        runtime, tmp_path, content="Canonical health report. " * 12
    )
    capabilities = ChannelCapabilities(max_message_length=70)
    adapter = SimpleNamespace(
        capabilities=capabilities, send_message=AsyncMock(return_value="report-sent")
    )
    manager = SimpleNamespace(
        _artifact_store=store, find_adapter_for_channel=lambda *_: (adapter, object())
    )
    service = ChannelDeliveryService(
        session_factory=runtime.factory, event_bus=runtime.bus, channel_manager_ref=lambda: manager
    )
    wrapper = (
        "Task completed: Health check\n\nGoal: Verify health\n\nResult: "
        + deliverable.deliverable_id
    )
    projection = await service._deliverable_channel_projection(
        deliverable_id=deliverable.deliverable_id,
        fallback_text=wrapper,
        capabilities=capabilities,
        artifact_store=store,
        owner_email="owner@example.com",
        conversation_id=runtime.external.conversation_id,
        materialize_media=False,
        outcome_context=wrapper if legacy else None,
    )
    digest = hashlib.sha256(
        f"signal:{capabilities.model_dump_json()}:{projection.identity}".encode()
    ).hexdigest()
    assert len(projection.chunks) > 1
    assert "Canonical health report" in " ".join(projection.chunks)

    result = await service._send_to_route(
        channel_type="signal",
        account_id=runtime.external.context_data["account_id"],
        chat_id=runtime.external.context_data["chat_id"],
        thread_id=None,
        content=wrapper,
        deliverable_id=deliverable.deliverable_id,
        outcome_context=wrapper,
        start_chunk_index=start_index,
        expected_projection_digest=digest,
        expected_projected_chunk_count=len(projection.chunks),
        delivery_owner_email="owner@example.com",
        delivery_conversation_id=runtime.external.conversation_id,
    )
    assert result.value == "sent"
    assert [call.args[0].content for call in adapter.send_message.call_args_list] == list(
        projection.chunks[start_index:]
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["auto", "direct"])
@pytest.mark.parametrize("format_name", ["markdown", "rich", "html"])
async def test_app_success_presents_canonical_deliverable_only(
    outcome_runtime, tmp_path, mode, format_name
):
    runtime = outcome_runtime
    deliverable = await _create_outcome_deliverable(runtime, tmp_path, format_name=format_name)
    task = outcome_task(
        status="completed",
        mode=mode,
        result_data={
            "final_deliverable_id": deliverable.deliverable_id,
            "final_format": format_name,
            "final_content": "Canonical health report",
        },
    )
    task.delivery = TaskDelivery(mode="specific_conversation", target=runtime.app.conversation_id)
    await runtime.workflow._deliver_task_result(task)
    recorded = runtime.guardrails.record_events.call_args.kwargs["events"]
    assert [event.data.get("event") for event in recorded] == [
        "task_result",
        "assistant_deliverable",
    ]
    assert recorded[1].data["deliverable_id"] == deliverable.deliverable_id
    assert recorded[1].data["format"] == format_name
    assert recorded[1].data["task_outcome"]["title"] == "Restore service health"
    assert recorded[1].data["task_outcome"]["attempt_number"] == 1
    assert not any(event.type == EventType.FOLLOW_UP_TURN_REQUESTED for event in runtime.events)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "canonical"),
    [
        ("completed", "missing"),
        ("completed", "private"),
        ("failed", "owned"),
        ("failed", "private"),
        ("cancelled", "owned"),
    ],
)
async def test_app_outcome_falls_back_without_readable_success_result(
    outcome_runtime, tmp_path, status, canonical
):
    runtime = outcome_runtime
    deliverable = await _create_outcome_deliverable(
        runtime, tmp_path, private=canonical == "private"
    )
    if canonical == "missing":
        async with runtime.factory() as db:
            await db.delete(await db.merge(deliverable))
            await db.commit()
    task = outcome_task(
        status=status,
        mode="direct",
        result_data={
            "final_deliverable_id": deliverable.deliverable_id,
            "final_content": "Authorized fallback result.",
        },
    )
    task.delivery = TaskDelivery(mode="specific_conversation", target=runtime.app.conversation_id)
    await runtime.workflow._deliver_task_result(task)
    recorded = runtime.guardrails.record_events.call_args.kwargs["events"]
    messages = [event for event in recorded if event.type == "assistant_message"]
    if status == "completed":
        assert len(messages) == 1
        assert "Task completed:" in messages[0].data["content"]
        assert "/tasks/task-outcomes" in messages[0].data["content"]
        assert "Canonical health report" not in messages[0].data["content"]
    else:
        assert not messages
        assert recorded[0].data["task_outcome_card"] is True
        if canonical == "private":
            assert recorded[0].data["deliverable_ids"] == []
            assert recorded[0].data["partial_result"] is None
        else:
            assert recorded[0].data["deliverable_ids"] == [deliverable.deliverable_id]
            assert recorded[0].data["partial_result"]["freshness"] == {
                "state": "stale",
                "replacement_status": status,
                "label": f"Partial output — task {status}",
            }
    assert not any(event.data.get("event") == "assistant_deliverable" for event in recorded)


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["completed", "failed", "cancelled"])
async def test_inbox_delivery_is_idempotent_and_never_starts_turn(outcome_runtime, status):
    runtime = outcome_runtime
    task = outcome_task(status=status, mode="direct", destination="in_app_notifications")
    await runtime.workflow._deliver_task_result(task)
    await runtime.workflow._deliver_task_result(task)
    async with runtime.factory() as db:
        rows = list((await db.scalars(select(InboxNotificationRow))).all())
    assert len(rows) == 1
    assert rows[0].user_email == task.created_by
    assert rows[0].href == "/tasks/task-outcomes"
    assert f"Task {status}:" in rows[0].body
    # The title is the short line a toast shows; the full outcome stays in the body.
    assert rows[0].title == f"{status.capitalize()}: {task.title}"
    runtime.guardrails.record_events.assert_not_called()
    runtime.channel.deliver_task_to_conversation.assert_not_called()
    assert not any(event.type == EventType.FOLLOW_UP_TURN_REQUESTED for event in runtime.events)
    task.completed_at += timedelta(seconds=1)
    await runtime.workflow._deliver_task_result(task)
    async with runtime.factory() as db:
        assert await db.scalar(select(func.count()).select_from(InboxNotificationRow)) == 2


@pytest.mark.asyncio
async def test_direct_missing_channel_falls_back_to_inbox_not_agent(outcome_runtime):
    runtime = outcome_runtime
    async with runtime.factory() as db:
        await update_channel_account(db, "signal-outcomes", preferred_for_task_delivery=True)
        await db.commit()
    from cognis.store.queries import get_conversation

    async with runtime.factory() as db:
        conversation = await get_conversation(db, runtime.external.conversation_id)
        conversation.context_ref = None
        conversation.context_data = {}
        await db.commit()
    task = outcome_task(mode="direct")
    task.delivery = TaskDelivery(
        mode="specific_conversation", target=runtime.external.conversation_id
    )
    await runtime.workflow._deliver_task_result(task)
    async with runtime.factory() as db:
        assert await db.scalar(select(func.count()).select_from(InboxNotificationRow)) == 1
    assert not any(event.type == EventType.FOLLOW_UP_TURN_REQUESTED for event in runtime.events)


@pytest.mark.asyncio
async def test_rejected_app_append_falls_back_without_admitting_turn(outcome_runtime):
    runtime = outcome_runtime
    runtime.guardrails.record_events.return_value = EventAppendResult(
        ok=False, count=0, first_seq=0, last_seq=0
    )
    async with runtime.factory() as db:
        await update_channel_account(
            db,
            app_channel_id("owner@example.com", "agent-outcomes"),
            preferred_for_task_delivery=True,
        )
        await db.commit()
    await runtime.workflow._deliver_task_result(outcome_task(mode="direct"))
    async with runtime.factory() as db:
        assert await db.scalar(select(func.count()).select_from(InboxNotificationRow)) == 1
    assert not any(event.type == EventType.FOLLOW_UP_TURN_REQUESTED for event in runtime.events)


@pytest.mark.asyncio
async def test_default_channel_outbox_reuses_occurrence_not_later_outcome(outcome_runtime):
    runtime = outcome_runtime
    async with runtime.factory() as db:
        await update_channel_account(db, "signal-outcomes", preferred_for_task_delivery=True)
        await db.commit()
    task = outcome_task()
    await runtime.workflow._deliver_task_result(task)
    await runtime.workflow._deliver_task_result(task)
    follow_ups = [
        event for event in runtime.events if event.type == EventType.FOLLOW_UP_TURN_REQUESTED
    ]
    assert (
        follow_ups[0].data["follow_up"]["follow_up_id"]
        == follow_ups[1].data["follow_up"]["follow_up_id"]
    )
    async with runtime.factory() as db:
        assert await db.scalar(select(func.count()).select_from(ChannelDeliveryOutboxRow)) == 1
    task.completed_at += timedelta(seconds=1)
    await runtime.workflow._deliver_task_result(task)
    async with runtime.factory() as db:
        assert await db.scalar(select(func.count()).select_from(ChannelDeliveryOutboxRow)) == 2


@pytest.mark.asyncio
async def test_direct_channel_outbox_distinguishes_terminal_occurrences(outcome_runtime):
    runtime = outcome_runtime
    service = ChannelDeliveryService(
        session_factory=runtime.factory,
        event_bus=EventBus(),
        channel_manager_ref=lambda: None,
    )
    task = outcome_task(mode="direct")
    for _ in range(2):
        await service.deliver_task_to_conversation(
            runtime.external.conversation_id,
            task_id=task.task_id,
            outcome_key=task_outcome_key(task),
            content=format_task_outcome(task),
        )
    async with runtime.factory() as db:
        assert await db.scalar(select(func.count()).select_from(ChannelDeliveryOutboxRow)) == 1
    task.completed_at += timedelta(seconds=1)
    await service.deliver_task_to_conversation(
        runtime.external.conversation_id,
        task_id=task.task_id,
        outcome_key=task_outcome_key(task),
        content=format_task_outcome(task),
    )
    async with runtime.factory() as db:
        assert await db.scalar(select(func.count()).select_from(ChannelDeliveryOutboxRow)) == 2


@pytest.mark.asyncio
async def test_task_and_schedule_crud_preserve_and_clear_finish_instructions(outcome_runtime):
    async with outcome_runtime.factory() as db:
        task = await create_task(
            db,
            task_id="crud-outcome",
            title="Outcome",
            description="Check",
            created_by="owner@example.com",
            agent_id="agent-outcomes",
            status="draft",
            on_finish_instructions="Inspect failures before retrying",
        )
        await update_task_fields(db, task.task_id, title="Updated")
        await db.refresh(task)
        assert task.on_finish_instructions == "Inspect failures before retrying"
        with pytest.raises(ValueError, match="requires Default or Agent"):
            await update_task_fields(db, task.task_id, completion_mode_family="direct")
        await update_task_fields(
            db,
            task.task_id,
            clear_on_finish_instructions=True,
            completion_mode_family="direct",
            delivery_mode="in_app_notifications",
        )
        await db.refresh(task)
        assert task.on_finish_instructions is None
        assert task.delivery_mode == "in_app_notifications"
        schedule = await create_schedule(
            db,
            schedule_id="sched-outcomes",
            agent_id="agent-outcomes",
            name="Check",
            schedule_type="interval",
            interval_seconds=300,
            task_template={},
            created_by="owner@example.com",
            on_finish_instructions="Inspect before repair",
        )
        await update_schedule(db, schedule.schedule_id, name="Updated")
        assert schedule.on_finish_instructions == "Inspect before repair"
        with pytest.raises(ValueError, match="requires direct"):
            await update_schedule(
                db,
                schedule.schedule_id,
                task_template={"delivery": {"mode": "in_app_notifications"}},
            )
        await update_schedule(
            db,
            schedule.schedule_id,
            on_finish_instructions=None,
            completion_mode_family="direct",
            task_template={"delivery": {"mode": "in_app_notifications"}},
        )
        await db.commit()
        assert (await get_task(db, task.task_id)).on_finish_instructions is None
        assert (await get_schedule(db, schedule.schedule_id)).on_finish_instructions is None


@pytest.mark.asyncio
async def test_app_channel_is_always_enabled_and_backfill_preserves_preference(outcome_runtime):
    runtime = outcome_runtime
    async with runtime.factory() as db:
        account = await get_channel_account(
            db, app_channel_id("owner@example.com", "agent-outcomes")
        )
        assert account.enabled and account.channel_type == "web"
        with pytest.raises(ValueError, match="only supports"):
            await update_channel_account(db, account.account_id, enabled=False)
        await update_channel_account(db, "signal-outcomes", preferred_for_task_delivery=True)
        await db.commit()
    async with runtime.engine.begin() as connection:
        await connection.run_sync(ensure_app_channels)
        await connection.run_sync(ensure_app_channels)
    async with runtime.factory() as db:
        rows = list((await db.scalars(select(ChannelAccountRow))).all())
        assert len([row for row in rows if row.channel_type == "web"]) == 1
        assert [row.account_id for row in rows if row.preferred_for_task_delivery] == [
            "signal-outcomes"
        ]


@pytest.mark.asyncio
async def test_neutral_follow_up_has_evidence_instructions_and_occurrence_identity():
    policy = FollowUpPolicy()
    task = outcome_task(source_type="schedule", source_ref="sched-outcomes")
    common = dict(
        conversation_id="conv-outcomes",
        task_id=task.task_id,
        task_title=task.title,
        status="failed",
        source_type="schedule",
        delivery_mode="preferred_channel",
        result_summary=task.result_summary,
        description=task.description,
        on_finish_instructions="Inspect, repair if authorized, verify once.",
        outcome_context={"task_url": "/tasks/task-outcomes", "final_deliverable_id": "dlv_report"},
    )
    first = await policy.build_task_result_follow_up(**common, outcome_key=task_outcome_key(task))
    repeat = await policy.build_task_result_follow_up(**common, outcome_key=task_outcome_key(task))
    task.completed_at += timedelta(seconds=1)
    later = await policy.build_task_result_follow_up(**common, outcome_key=task_outcome_key(task))
    assert first.follow_up_id == repeat.follow_up_id != later.follow_up_id
    assert first.mode == FollowUpMode.HANDLE
    rendered = render_follow_up_block(first)
    assert "repair if authorized" in rendered and "/tasks/task-outcomes" in rendered
    guidance = build_follow_up_guidance(PromptContext.FOLLOW_UP_OUTCOME)
    assert "No option is the default" in guidance
    assert "scheduled retry" in guidance


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["default", "direct"])
async def test_task_outcome_cannot_target_another_users_conversation(outcome_runtime, mode):
    from cognis.store.queries import get_conversation

    runtime = outcome_runtime
    async with runtime.factory() as db:
        await create_user(
            db, email="other@example.com", name="Other", password_hash="hash", role="user"
        )
        conversation = await get_conversation(db, runtime.external.conversation_id)
        conversation.user_email = "other@example.com"
        await db.commit()
    task = outcome_task(mode=mode)
    task.delivery = TaskDelivery(
        mode="specific_conversation", target=runtime.external.conversation_id
    )
    await runtime.workflow._deliver_task_result(task)
    runtime.guardrails.record_events.assert_not_called()
    runtime.channel.deliver_task_to_conversation.assert_not_called()
    assert not any(event.type == EventType.FOLLOW_UP_TURN_REQUESTED for event in runtime.events)
    async with runtime.factory() as db:
        rows = list((await db.scalars(select(InboxNotificationRow))).all())
        assert len(rows) == (1 if mode == "direct" else 0)
        assert all(row.user_email == "owner@example.com" for row in rows)


def test_delivery_policy_rejects_ambiguous_execution_and_formats_partial_failure():
    with pytest.raises(ValueError, match="requires direct"):
        validate_task_delivery(
            TaskDelivery(mode="in_app_notifications"), CompletionDeliveryPolicy()
        )
    with pytest.raises(ValueError, match="requires Default or Agent"):
        CompletionDeliveryPolicy(completion_mode_family="direct", on_finish_instructions="Repair")
    task = outcome_task(
        workflow_state=WorkflowState(
            workflow_id="health",
            current_step_index=0,
            effective_workflow_definition={"steps": [{"name": "verify"}]},
            last_evaluation_feedback="Connection refused",
        )
    )
    text = format_task_outcome(task)
    assert "Connection refused" in text and "Step: verify" in text
    assert "partial" in text and "/tasks/task-outcomes" in text
    assert task_outcome_key(task) == task_outcome_key(
        task.model_copy(
            update={
                "completed_at": task.completed_at.replace(tzinfo=None),
            }
        )
    )
