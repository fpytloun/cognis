from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import func, select

from cognis.artifacts.store import ArtifactStore, ArtifactStoreConfig
from cognis.core.events import EventBus, EventType
from cognis.core.session import SessionManager
from cognis.core.workflow_engine import WorkflowEngine
from cognis.models.task import TaskDelivery, TaskModel, TaskStatus
from cognis.models.workflow import CompletionDeliveryPolicy, StepDefinition, Workflow, WorkflowState
from cognis.store.database import create_engine, create_session_factory
from cognis.store.models import Base, ChannelDeliveryOutboxRow
from cognis.store.queries import (
    create_agent,
    create_channel_account,
    create_conversation,
    create_deliverable,
    create_session,
    create_step_run,
    create_task,
    create_user,
    get_agent_direct_conversation,
    get_channel_delivery_outbox,
    set_session_intaris_session_id,
    update_conversation_active_session,
)


class _Guardrails:
    def __init__(self) -> None:
        self.recorded: list[tuple[str, list[object]]] = []

    async def record_events(
        self,
        session_id: str,
        events: list[object],
        source: str = "cognis",
        idempotency_key: str | None = None,
    ) -> object:
        del idempotency_key
        self.recorded.append((session_id, events))
        return SimpleNamespace(last_seq=1)


class _ChannelDelivery:
    def __init__(self, *, sent: bool = True) -> None:
        self.sent = sent
        self.calls: list[tuple[str, str, list[dict[str, object]] | None]] = []

    async def send_to_conversation(
        self,
        conversation_id: str,
        content: str,
        attachments: list[dict[str, object]] | None = None,
        deliverable_id: str | None = None,
    ) -> bool:
        del deliverable_id
        self.calls.append((conversation_id, content, attachments))
        return self.sent

    async def deliver_task_to_conversation(
        self,
        conversation_id: str,
        *,
        task_id: str,
        content: str,
        attachments: list[dict[str, object]] | None = None,
        deliverable_id: str | None = None,
    ) -> object:
        from cognis.channels.delivery import ChannelDeliveryStatus

        del task_id, deliverable_id
        self.calls.append((conversation_id, content, attachments))
        return ChannelDeliveryStatus.SENT if self.sent else ChannelDeliveryStatus.FAILED


@pytest.mark.asyncio
async def test_deliver_task_result_uses_latest_active_conversation_and_publishes_follow_up(
    tmp_path: object,
) -> None:
    engine, session_factory = await _runtime(tmp_path)
    guardrails = _Guardrails()
    event_bus = EventBus()
    seen: list[EventType] = []

    async def _capture(event: object) -> None:
        seen.append(event.type)

    event_bus.subscribe_all(_capture)

    workflow_engine = WorkflowEngine(
        session_factory=session_factory,
        providers=SimpleNamespace(guardrails=guardrails),
        agent_loop=SimpleNamespace(),
        step_evaluator=SimpleNamespace(),
        workflow_registry=SimpleNamespace(),
        session_manager=SimpleNamespace(),
        event_bus=event_bus,
        pause_waiter=SimpleNamespace(),
    )

    async with session_factory() as session:
        await create_user(
            session, email="user@example.com", name="User", password_hash="hash", role="user"
        )
        await create_agent(
            session, agent_id="agent-1", owner_email="user@example.com", name="Agent"
        )
        conversation = await create_conversation(
            session,
            user_email="user@example.com",
            agent_id="agent-1",
            context_type="web",
            title="Latest",
        )
        conversation.last_message_at = datetime.now(UTC)
        root_session = await create_session(
            session,
            conversation_id=conversation.conversation_id,
            user_email="user@example.com",
            agent_id="agent-1",
        )
        await set_session_intaris_session_id(session, root_session.session_id, "intaris-root")
        await update_conversation_active_session(
            session, conversation.conversation_id, root_session.session_id
        )
        await session.commit()

    await workflow_engine._deliver_task_result(
        TaskModel(
            task_id="task-1",
            title="Background task",
            description="",
            status=TaskStatus.COMPLETED,
            priority=0,
            created_by="user@example.com",
            agent_id="agent-1",
            source_type="api",
            source_ref=None,
            delivery=TaskDelivery(mode="latest_active_for_agent"),
            workflow_id=None,
            result_summary="Done",
        )
    )

    assert guardrails.recorded[0][0] == "intaris-root"
    assert EventType.TASK_COMPLETED in seen
    assert EventType.FOLLOW_UP_TURN_REQUESTED in seen
    await engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("completion_mode", ["default", "direct"])
async def test_deliver_task_result_from_renewed_scope_is_historical_only(
    tmp_path: object,
    completion_mode: str,
) -> None:
    engine, session_factory = await _runtime(tmp_path)
    guardrails = _Guardrails()
    channel_delivery = _ChannelDelivery()
    event_bus = EventBus()
    seen: list[EventType] = []

    async def _capture(event: object) -> None:
        seen.append(event.type)

    event_bus.subscribe_all(_capture)
    workflow_engine = WorkflowEngine(
        session_factory=session_factory,
        providers=SimpleNamespace(guardrails=guardrails),
        agent_loop=SimpleNamespace(),
        step_evaluator=SimpleNamespace(),
        workflow_registry=SimpleNamespace(),
        session_manager=SimpleNamespace(),
        event_bus=event_bus,
        pause_waiter=SimpleNamespace(),
        channel_delivery=channel_delivery,
    )

    async with session_factory() as session:
        await create_user(
            session, email="user@example.com", name="User", password_hash="hash", role="user"
        )
        await create_agent(
            session, agent_id="agent-1", owner_email="user@example.com", name="Agent"
        )
        conversation = await create_conversation(
            session,
            user_email="user@example.com",
            agent_id="agent-1",
            context_type="signal",
            context_ref="signal:acct-1:chat-1",
            context_data={
                "channel_type": "signal",
                "account_id": "acct-1",
                "chat_id": "chat-1",
            },
        )
        old_session = await create_session(
            session,
            conversation_id=conversation.conversation_id,
            user_email="user@example.com",
            agent_id="agent-1",
            activity_scope_id="scope-old",
        )
        current_session = await create_session(
            session,
            conversation_id=conversation.conversation_id,
            user_email="user@example.com",
            agent_id="agent-1",
            previous_session_id=old_session.session_id,
            activity_scope_id="scope-current",
        )
        await update_conversation_active_session(
            session, conversation.conversation_id, current_session.session_id
        )
        await create_task(
            session,
            task_id=f"task-stale-{completion_mode}",
            created_by="user@example.com",
            agent_id="agent-1",
            title="Historical task",
            status="completed",
            source_type="chat",
            source_ref=conversation.conversation_id,
            source_session_id=old_session.session_id,
            delivery_mode="same_conversation",
            completion_mode_family=completion_mode,
        )
        await session.commit()

    await workflow_engine._deliver_task_result(
        TaskModel(
            task_id=f"task-stale-{completion_mode}",
            title="Historical task",
            status=TaskStatus.COMPLETED,
            created_by="user@example.com",
            agent_id="agent-1",
            source_type="chat",
            source_ref=conversation.conversation_id,
            source_session_id=old_session.session_id,
            delivery=TaskDelivery(mode="same_conversation"),
            completion_delivery=CompletionDeliveryPolicy(completion_mode_family=completion_mode),
            result_summary="Done",
            result_data={"final_content": "Must stay historical"},
            applied_completion_mode=completion_mode,
        )
    )

    assert guardrails.recorded == []
    assert channel_delivery.calls == []
    assert seen == []
    async with session_factory() as session:
        outbox_count = (
            await session.execute(select(func.count()).select_from(ChannelDeliveryOutboxRow))
        ).scalar_one()
        assert outbox_count == 0
    await engine.dispose()


@pytest.mark.asyncio
async def test_deliver_task_result_skips_outward_silent_delivery_but_publishes_terminal_event(
    tmp_path: object,
) -> None:
    engine, session_factory = await _runtime(tmp_path)
    guardrails = _Guardrails()
    event_bus = EventBus()
    seen: list[EventType] = []

    async def _capture(event: object) -> None:
        seen.append(event.type)

    event_bus.subscribe_all(_capture)

    workflow_engine = WorkflowEngine(
        session_factory=session_factory,
        providers=SimpleNamespace(guardrails=guardrails),
        agent_loop=SimpleNamespace(),
        step_evaluator=SimpleNamespace(),
        workflow_registry=SimpleNamespace(),
        session_manager=SimpleNamespace(),
        event_bus=event_bus,
        pause_waiter=SimpleNamespace(),
    )

    await workflow_engine._deliver_task_result(
        TaskModel(
            task_id="task-1",
            title="Background task",
            description="",
            status=TaskStatus.COMPLETED,
            priority=0,
            created_by="user@example.com",
            agent_id="agent-1",
            source_type="api",
            source_ref=None,
            delivery=TaskDelivery(mode="latest_active_for_agent"),
            completion_delivery=CompletionDeliveryPolicy(allow_silent_completion=True),
            workflow_id=None,
            result_summary="Done",
            applied_completion_mode="silent",
        )
    )

    assert guardrails.recorded == []
    assert seen == [EventType.TASK_COMPLETED]
    await engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "task_status, result_data",
    [
        (TaskStatus.FAILED, None),
        (TaskStatus.FAILED, {}),
        (TaskStatus.FAILED, {"attachments": [{"artifact_id": "a", "url": "u"}]}),
        (TaskStatus.CANCELLED, None),
    ],
)
async def test_deliver_task_result_handles_terminal_states_without_final_content(
    tmp_path: object,
    task_status: TaskStatus,
    result_data: dict[str, object] | None,
) -> None:
    """Regression: terminal states other than COMPLETED leave result_data
    without final_channel_content / final_content keys. The delivery path
    must not crash with ``AttributeError: 'NoneType' object has no attribute
    'strip'``.
    """
    engine, session_factory = await _runtime(tmp_path)
    guardrails = _Guardrails()
    event_bus = EventBus()
    seen: list[EventType] = []

    async def _capture(event: object) -> None:
        seen.append(event.type)

    event_bus.subscribe_all(_capture)

    workflow_engine = WorkflowEngine(
        session_factory=session_factory,
        providers=SimpleNamespace(guardrails=guardrails),
        agent_loop=SimpleNamespace(),
        step_evaluator=SimpleNamespace(),
        workflow_registry=SimpleNamespace(),
        session_manager=SimpleNamespace(),
        event_bus=event_bus,
        pause_waiter=SimpleNamespace(),
    )

    async with session_factory() as session:
        await create_user(
            session, email="user@example.com", name="User", password_hash="hash", role="user"
        )
        await create_agent(
            session, agent_id="agent-1", owner_email="user@example.com", name="Agent"
        )
        conversation = await create_conversation(
            session,
            user_email="user@example.com",
            agent_id="agent-1",
            context_type="web",
            title="Latest",
        )
        conversation.last_message_at = datetime.now(UTC)
        root_session = await create_session(
            session,
            conversation_id=conversation.conversation_id,
            user_email="user@example.com",
            agent_id="agent-1",
        )
        await set_session_intaris_session_id(session, root_session.session_id, "intaris-root")
        await update_conversation_active_session(
            session, conversation.conversation_id, root_session.session_id
        )
        await session.commit()

    await workflow_engine._deliver_task_result(
        TaskModel(
            task_id="task-1",
            title="Background task",
            description="",
            status=task_status,
            priority=0,
            created_by="user@example.com",
            agent_id="agent-1",
            source_type="api",
            source_ref=None,
            delivery=TaskDelivery(mode="latest_active_for_agent"),
            workflow_id=None,
            result_summary="Workflow failed",
            result_data=result_data,
        )
    )

    # Delivery landed on the latest active conversation without raising.
    assert guardrails.recorded[0][0] == "intaris-root"
    recorded_events = guardrails.recorded[0][1]
    assert recorded_events, "expected at least one event recorded to Intaris"
    event_payload = recorded_events[0]
    data = getattr(event_payload, "data", event_payload)
    if isinstance(data, dict):
        expected_event = {
            TaskStatus.FAILED: "task_failed",
            TaskStatus.CANCELLED: "task_cancelled",
        }[task_status]
        assert data.get("event") == expected_event
    await engine.dispose()


@pytest.mark.asyncio
async def test_deliver_task_result_direct_sends_channel_message_without_follow_up(
    tmp_path: object,
) -> None:
    engine, session_factory = await _runtime(tmp_path)
    guardrails = _Guardrails()
    channel_delivery = _ChannelDelivery()
    event_bus = EventBus()
    seen: list[EventType] = []

    async def _capture(event: object) -> None:
        seen.append(event.type)

    event_bus.subscribe_all(_capture)
    artifact_store = ArtifactStore(
        ArtifactStoreConfig(
            path=str(tmp_path / "artifacts-direct-duplicate"),
            base_url="http://testserver",
            signing_secret="test-secret",
        )
    )
    workflow_engine = WorkflowEngine(
        session_factory=session_factory,
        providers=SimpleNamespace(guardrails=guardrails),
        agent_loop=SimpleNamespace(artifact_store=artifact_store),
        step_evaluator=SimpleNamespace(),
        workflow_registry=SimpleNamespace(),
        session_manager=SimpleNamespace(),
        event_bus=event_bus,
        pause_waiter=SimpleNamespace(),
        channel_delivery=channel_delivery,
    )

    async with session_factory() as session:
        await create_user(
            session, email="user@example.com", name="User", password_hash="hash", role="user"
        )
        await create_agent(
            session, agent_id="agent-1", owner_email="user@example.com", name="Agent"
        )
        conversation = await create_conversation(
            session,
            user_email="user@example.com",
            agent_id="agent-1",
            context_type="signal",
            context_ref="signal:acct-1:chat-1",
            context_data={
                "channel_type": "signal",
                "account_id": "acct-1",
                "chat_id": "chat-1",
            },
            title="Signal",
        )
        root_session = await create_session(
            session,
            conversation_id=conversation.conversation_id,
            user_email="user@example.com",
            agent_id="agent-1",
        )
        await set_session_intaris_session_id(session, root_session.session_id, "intaris-direct")
        await update_conversation_active_session(
            session, conversation.conversation_id, root_session.session_id
        )
        await session.commit()

    await workflow_engine._deliver_task_result(
        TaskModel(
            task_id="task-direct-1",
            title="Background task",
            description="",
            status=TaskStatus.COMPLETED,
            priority=0,
            created_by="user@example.com",
            agent_id="agent-1",
            source_type="chat",
            source_ref=conversation.conversation_id,
            delivery=TaskDelivery(mode="same_conversation"),
            completion_delivery=CompletionDeliveryPolicy(completion_mode_family="direct"),
            workflow_id=None,
            result_summary="Done",
            result_data={"final_content": "Final direct reply"},
            applied_completion_mode="direct",
        )
    )

    assert channel_delivery.calls == [(conversation.conversation_id, "Final direct reply", [])]
    assert EventType.FOLLOW_UP_TURN_REQUESTED not in seen
    assert EventType.TASK_COMPLETED in seen
    await engine.dispose()


@pytest.mark.asyncio
async def test_direct_workflow_partial_delivery_stays_on_durable_path_without_fallback(
    tmp_path: object,
) -> None:
    engine, session_factory = await _runtime(tmp_path)
    guardrails = _Guardrails()
    channel_delivery = _ChannelDelivery(sent=False)
    event_bus = EventBus()
    events: list[object] = []

    async def capture(event: object) -> None:
        events.append(event)

    event_bus.subscribe_all(capture)
    workflow_engine = WorkflowEngine(
        session_factory=session_factory,
        providers=SimpleNamespace(guardrails=guardrails),
        agent_loop=SimpleNamespace(),
        step_evaluator=SimpleNamespace(),
        workflow_registry=SimpleNamespace(),
        session_manager=SimpleNamespace(),
        event_bus=event_bus,
        pause_waiter=SimpleNamespace(),
        channel_delivery=channel_delivery,
    )

    async with session_factory() as session:
        await create_user(
            session,
            email="user@example.com",
            name="User",
            password_hash="hash",
            role="user",
        )
        await create_agent(
            session,
            agent_id="agent-1",
            owner_email="user@example.com",
            name="Agent",
        )
        conversation = await create_conversation(
            session,
            user_email="user@example.com",
            agent_id="agent-1",
            context_type="matrix",
            context_ref="matrix:acct-1:room-1",
            context_data={
                "channel_type": "matrix",
                "account_id": "acct-1",
                "chat_id": "room-1",
            },
            title="Matrix",
        )
        await session.commit()

    task = TaskModel(
        task_id="task-direct-partial",
        title="Background task",
        description="",
        status=TaskStatus.COMPLETED,
        priority=0,
        created_by="user@example.com",
        agent_id="agent-1",
        source_type="chat",
        source_ref=conversation.conversation_id,
        delivery=TaskDelivery(mode="same_conversation"),
        completion_delivery=CompletionDeliveryPolicy(completion_mode_family="direct"),
        workflow_id=None,
        result_summary="Done",
        result_data={"final_content": "Multipart result"},
        applied_completion_mode="direct",
    )
    await workflow_engine._deliver_task_result(task)

    assert channel_delivery.calls == [(conversation.conversation_id, "Multipart result", [])]
    assert guardrails.recorded == []
    assert task.applied_completion_mode == "direct"
    assert task.applied_completion_reason == "Direct channel delivery is pending durable retry."
    assert any(
        getattr(event, "data", {}).get("channel_delivery_status") == "failed" for event in events
    )
    await engine.dispose()


@pytest.mark.asyncio
async def test_deliver_task_result_direct_publishes_terminal_event_when_deliverable_already_sent(
    tmp_path: object,
) -> None:
    engine, session_factory = await _runtime(tmp_path)
    guardrails = _Guardrails()
    channel_delivery = _ChannelDelivery()
    event_bus = EventBus()
    seen: list[EventType] = []

    async def _capture(event: object) -> None:
        seen.append(event.type)

    event_bus.subscribe_all(_capture)
    artifact_store = ArtifactStore(
        ArtifactStoreConfig(
            path=str(tmp_path / "artifacts-direct-duplicate"),
            base_url="http://testserver",
            signing_secret="test-secret",
        )
    )
    workflow_engine = WorkflowEngine(
        session_factory=session_factory,
        providers=SimpleNamespace(guardrails=guardrails),
        agent_loop=SimpleNamespace(artifact_store=artifact_store),
        step_evaluator=SimpleNamespace(),
        workflow_registry=SimpleNamespace(),
        session_manager=SimpleNamespace(),
        event_bus=event_bus,
        pause_waiter=SimpleNamespace(),
        channel_delivery=channel_delivery,
    )

    async with session_factory() as session:
        await create_user(
            session, email="user@example.com", name="User", password_hash="hash", role="user"
        )
        await create_agent(
            session, agent_id="agent-1", owner_email="user@example.com", name="Agent"
        )
        conversation = await create_conversation(
            session,
            user_email="user@example.com",
            agent_id="agent-1",
            context_type="signal",
            context_ref="signal:acct-1:chat-1",
            context_data={
                "channel_type": "signal",
                "account_id": "acct-1",
                "chat_id": "chat-1",
            },
            title="Signal",
        )
        await create_task(
            session,
            task_id="task-direct-duplicate",
            created_by="user@example.com",
            agent_id="agent-1",
            title="Background task",
            status="completed",
        )
        step_run = await create_step_run(
            session,
            task_id="task-direct-duplicate",
            step_name="deliver",
            step_type="run",
            agent_id="agent-1",
        )
        deliverable = await create_deliverable(
            session,
            step_run_id=step_run.step_run_id,
            deliverable_id="dlv_direct_duplicate",
            content="Already delivered content",
            format="markdown",
            artifact_store=artifact_store,
        )
        deliverable.status = "delivered"
        await session.commit()

    await workflow_engine._deliver_task_result(
        TaskModel(
            task_id="task-direct-duplicate",
            title="Background task",
            description="",
            status=TaskStatus.COMPLETED,
            priority=0,
            created_by="user@example.com",
            agent_id="agent-1",
            source_type="chat",
            source_ref=conversation.conversation_id,
            delivery=TaskDelivery(mode="same_conversation"),
            completion_delivery=CompletionDeliveryPolicy(completion_mode_family="direct"),
            workflow_id=None,
            result_summary="Done",
            result_data={"final_deliverable_id": "dlv_direct_duplicate"},
            applied_completion_mode="direct",
        )
    )

    assert channel_delivery.calls == []
    assert guardrails.recorded == []
    assert EventType.TASK_COMPLETED in seen
    assert EventType.FOLLOW_UP_TURN_REQUESTED not in seen
    await engine.dispose()


@pytest.mark.asyncio
async def test_explicit_direct_delivery_overrides_legacy_silent_target_mode(
    tmp_path: object,
) -> None:
    engine, session_factory = await _runtime(tmp_path)
    guardrails = _Guardrails()
    channel_delivery = _ChannelDelivery()
    event_bus = EventBus()
    workflow_engine = WorkflowEngine(
        session_factory=session_factory,
        providers=SimpleNamespace(guardrails=guardrails),
        agent_loop=SimpleNamespace(),
        step_evaluator=SimpleNamespace(),
        workflow_registry=SimpleNamespace(),
        session_manager=SimpleNamespace(),
        event_bus=event_bus,
        pause_waiter=SimpleNamespace(),
        channel_delivery=channel_delivery,
    )

    async with session_factory() as session:
        await create_user(
            session, email="user@example.com", name="User", password_hash="hash", role="user"
        )
        await create_agent(
            session, agent_id="agent-1", owner_email="user@example.com", name="Agent"
        )
        conversation = await create_conversation(
            session,
            user_email="user@example.com",
            agent_id="agent-1",
            context_type="signal",
            context_ref="signal:acct-1:chat-1",
            context_data={
                "channel_type": "signal",
                "account_id": "acct-1",
                "chat_id": "chat-1",
            },
            title="Signal",
        )
        root_session = await create_session(
            session,
            conversation_id=conversation.conversation_id,
            user_email="user@example.com",
            agent_id="agent-1",
        )
        await set_session_intaris_session_id(
            session, root_session.session_id, "intaris-direct-legacy"
        )
        await update_conversation_active_session(
            session, conversation.conversation_id, root_session.session_id
        )
        await session.commit()

    await workflow_engine._deliver_task_result(
        TaskModel(
            task_id="task-direct-legacy",
            title="Daily brief",
            description="",
            status=TaskStatus.COMPLETED,
            priority=0,
            created_by="user@example.com",
            agent_id="agent-1",
            source_type="chat",
            source_ref=conversation.conversation_id,
            delivery=TaskDelivery(mode="silent"),
            completion_delivery=CompletionDeliveryPolicy(completion_mode_family="default"),
            workflow_id=None,
            result_summary="Done",
            result_data={"final_content": "Direct brief"},
            applied_completion_mode="direct",
        )
    )

    assert channel_delivery.calls == [(conversation.conversation_id, "Direct brief", [])]
    await engine.dispose()


@pytest.mark.asyncio
async def test_explicit_direct_notification_overrides_default_policy(tmp_path: object) -> None:
    engine, session_factory = await _runtime(tmp_path)
    workflow_engine = WorkflowEngine(
        session_factory=session_factory,
        providers=SimpleNamespace(guardrails=_Guardrails()),
        agent_loop=SimpleNamespace(),
        step_evaluator=SimpleNamespace(),
        workflow_registry=SimpleNamespace(),
        session_manager=SimpleNamespace(),
        event_bus=EventBus(),
        pause_waiter=SimpleNamespace(),
    )

    task = TaskModel(
        task_id="task-explicit-direct",
        title="Daily brief",
        description="",
        status=TaskStatus.COMPLETED,
        priority=0,
        created_by="user@example.com",
        agent_id="agent-1",
        source_type="scheduler",
        source_ref="schedule-1",
        delivery=TaskDelivery(mode="latest_active_for_agent"),
        completion_delivery=CompletionDeliveryPolicy(completion_mode_family="default"),
        result_data={"final_content": "Ready-to-read brief"},
    )
    state = WorkflowState(
        current_step="publish",
        step_outputs={
            "publish": {
                "summary": "Prepared the daily brief.",
                "content": "Ready-to-read brief",
                "notification": {"mode": "direct"},
            }
        },
    )

    applied_mode, applied_reason = workflow_engine._resolve_applied_completion(task, state)

    assert applied_mode == "direct"
    assert applied_reason is None
    await engine.dispose()


@pytest.mark.asyncio
async def test_allow_silent_completion_auto_applies_silent_on_success_without_override(
    tmp_path: object,
) -> None:
    engine, session_factory = await _runtime(tmp_path)
    workflow_engine = WorkflowEngine(
        session_factory=session_factory,
        providers=SimpleNamespace(guardrails=_Guardrails()),
        agent_loop=SimpleNamespace(),
        step_evaluator=SimpleNamespace(),
        workflow_registry=SimpleNamespace(),
        session_manager=SimpleNamespace(),
        event_bus=EventBus(),
        pause_waiter=SimpleNamespace(),
    )

    task = TaskModel(
        task_id="task-auto-silent",
        title="Scheduled check",
        description="",
        status=TaskStatus.COMPLETED,
        priority=0,
        created_by="user@example.com",
        agent_id="agent-1",
        source_type="scheduler",
        source_ref="schedule-1",
        delivery=TaskDelivery(mode="preferred_channel"),
        completion_delivery=CompletionDeliveryPolicy(
            completion_mode_family="direct",
            allow_silent_completion=True,
        ),
        result_data={"final_content": "No actionable findings."},
    )
    state = WorkflowState(
        current_step="investigate",
        step_outputs={
            "investigate": {
                "summary": "No actionable findings.",
                "content": "No actionable findings.",
            }
        },
    )

    applied_mode, applied_reason = workflow_engine._resolve_applied_completion(task, state)

    assert applied_mode == "silent"
    assert applied_reason == (
        "Auto-silent completion: allow_silent_completion=true and no explicit notification "
        "override was requested."
    )
    await engine.dispose()


@pytest.mark.asyncio
async def test_allow_silent_completion_does_not_suppress_failed_task(tmp_path: object) -> None:
    engine, session_factory = await _runtime(tmp_path)
    workflow_engine = WorkflowEngine(
        session_factory=session_factory,
        providers=SimpleNamespace(guardrails=_Guardrails()),
        agent_loop=SimpleNamespace(),
        step_evaluator=SimpleNamespace(),
        workflow_registry=SimpleNamespace(),
        session_manager=SimpleNamespace(),
        event_bus=EventBus(),
        pause_waiter=SimpleNamespace(),
    )

    task = TaskModel(
        task_id="task-failed-not-silent",
        title="Scheduled check",
        description="",
        status=TaskStatus.FAILED,
        priority=0,
        created_by="user@example.com",
        agent_id="agent-1",
        source_type="scheduler",
        source_ref="schedule-1",
        delivery=TaskDelivery(mode="preferred_channel"),
        completion_delivery=CompletionDeliveryPolicy(
            completion_mode_family="direct",
            allow_silent_completion=True,
        ),
        result_data={"final_content": "Failure details"},
    )
    state = WorkflowState(
        status="failed",
        current_step="investigate",
        step_outputs={
            "investigate": {
                "summary": "Investigation failed.",
                "error": "executor unavailable",
            }
        },
    )

    applied_mode, applied_reason = workflow_engine._resolve_applied_completion(task, state)

    assert applied_mode == "default"
    assert applied_reason is None
    await engine.dispose()


@pytest.mark.asyncio
async def test_explicit_direct_notification_overrides_auto_silent_policy(tmp_path: object) -> None:
    engine, session_factory = await _runtime(tmp_path)
    workflow_engine = WorkflowEngine(
        session_factory=session_factory,
        providers=SimpleNamespace(guardrails=_Guardrails()),
        agent_loop=SimpleNamespace(),
        step_evaluator=SimpleNamespace(),
        workflow_registry=SimpleNamespace(),
        session_manager=SimpleNamespace(),
        event_bus=EventBus(),
        pause_waiter=SimpleNamespace(),
    )

    task = TaskModel(
        task_id="task-direct-override",
        title="Scheduled report",
        description="",
        status=TaskStatus.COMPLETED,
        priority=0,
        created_by="user@example.com",
        agent_id="agent-1",
        source_type="scheduler",
        source_ref="schedule-1",
        delivery=TaskDelivery(mode="preferred_channel"),
        completion_delivery=CompletionDeliveryPolicy(allow_silent_completion=True),
        result_data={"final_content": "Important report"},
    )
    state = WorkflowState(
        current_step="publish",
        step_outputs={
            "publish": {
                "summary": "Prepared important report.",
                "content": "Important report",
                "notification": {"mode": "direct"},
            }
        },
    )

    applied_mode, applied_reason = workflow_engine._resolve_applied_completion(task, state)

    assert applied_mode == "direct"
    assert applied_reason is None
    await engine.dispose()


@pytest.mark.asyncio
async def test_failed_workflow_summary_uses_current_failed_step_output(tmp_path: object) -> None:
    engine, session_factory = await _runtime(tmp_path)
    workflow_engine = WorkflowEngine(
        session_factory=session_factory,
        providers=SimpleNamespace(),
        agent_loop=SimpleNamespace(),
        step_evaluator=SimpleNamespace(),
        workflow_registry=SimpleNamespace(),
        session_manager=SimpleNamespace(),
        event_bus=EventBus(),
        pause_waiter=SimpleNamespace(),
    )
    workflow = Workflow(
        workflow_id="workflow-test",
        name="Test workflow",
        steps=[
            StepDefinition(name="plan", type="run"),
            StepDefinition(name="execute", type="run"),
            StepDefinition(name="review", type="run"),
        ],
    )
    state = WorkflowState(
        current_step_index=1,
        status="failed",
        step_outputs={
            "execute": {
                "summary": "Failed to resolve step runtime.",
                "error": "executor unavailable",
            }
        },
    )

    assert (
        workflow_engine._build_failure_result_summary(state, workflow)
        == "Failed to resolve step runtime."
    )
    await engine.dispose()


@pytest.mark.asyncio
async def test_deliver_task_result_uses_source_ref_for_specific_conversation(
    tmp_path: object,
) -> None:
    engine, session_factory = await _runtime(tmp_path)
    guardrails = _Guardrails()
    event_bus = EventBus()
    captured: list[object] = []

    async def _capture(event: object) -> None:
        captured.append(event)

    event_bus.subscribe_all(_capture)
    workflow_engine = WorkflowEngine(
        session_factory=session_factory,
        providers=SimpleNamespace(guardrails=guardrails),
        agent_loop=SimpleNamespace(),
        step_evaluator=SimpleNamespace(),
        workflow_registry=SimpleNamespace(),
        session_manager=SimpleNamespace(),
        event_bus=event_bus,
        pause_waiter=SimpleNamespace(),
    )

    async with session_factory() as session:
        await create_user(
            session, email="user@example.com", name="User", password_hash="hash", role="user"
        )
        await create_agent(
            session, agent_id="agent-1", owner_email="user@example.com", name="Agent"
        )
        conversation = await create_conversation(
            session,
            user_email="user@example.com",
            agent_id="agent-1",
            context_type="web",
            title="Specific",
        )
        root_session = await create_session(
            session,
            conversation_id=conversation.conversation_id,
            user_email="user@example.com",
            agent_id="agent-1",
        )
        await set_session_intaris_session_id(session, root_session.session_id, "intaris-specific")
        await update_conversation_active_session(
            session, conversation.conversation_id, root_session.session_id
        )
        await session.commit()

    await workflow_engine._deliver_task_result(
        TaskModel(
            task_id="task-1",
            title="Background task",
            description="",
            status=TaskStatus.FAILED,
            priority=0,
            created_by="user@example.com",
            agent_id="agent-1",
            source_type="api",
            source_ref=None,
            delivery=TaskDelivery(
                mode="specific_conversation", target=conversation.conversation_id
            ),
            workflow_id=None,
            result_summary="Failed",
        )
    )

    assert guardrails.recorded[0][0] == "intaris-specific"
    follow_up = next(
        event for event in captured if event.type == EventType.FOLLOW_UP_TURN_REQUESTED
    )
    assert follow_up.data["follow_up"]["mode"] == "notify"
    await engine.dispose()


@pytest.mark.asyncio
async def test_same_conversation_delivery_accepts_agent_source_conversation(
    tmp_path: object,
) -> None:
    engine, session_factory = await _runtime(tmp_path)
    guardrails = _Guardrails()
    workflow_engine = WorkflowEngine(
        session_factory=session_factory,
        providers=SimpleNamespace(guardrails=guardrails),
        agent_loop=SimpleNamespace(),
        step_evaluator=SimpleNamespace(),
        workflow_registry=SimpleNamespace(),
        session_manager=SimpleNamespace(),
        event_bus=EventBus(),
        pause_waiter=SimpleNamespace(),
    )

    async with session_factory() as session:
        await create_user(
            session, email="user@example.com", name="User", password_hash="hash", role="user"
        )
        await create_agent(
            session, agent_id="agent-1", owner_email="user@example.com", name="Agent"
        )
        conversation = await create_conversation(
            session,
            user_email="user@example.com",
            agent_id="agent-1",
            context_type="web",
            title="Source",
        )
        root_session = await create_session(
            session,
            conversation_id=conversation.conversation_id,
            user_email="user@example.com",
            agent_id="agent-1",
        )
        await set_session_intaris_session_id(session, root_session.session_id, "intaris-agent")
        await update_conversation_active_session(
            session, conversation.conversation_id, root_session.session_id
        )
        await session.commit()

    await workflow_engine._deliver_task_result(
        TaskModel(
            task_id="task-agent-source",
            title="Agent task",
            description="",
            status=TaskStatus.COMPLETED,
            priority=0,
            created_by="user@example.com",
            agent_id="agent-1",
            source_type="agent",
            source_ref=conversation.conversation_id,
            delivery=TaskDelivery(mode="same_conversation"),
            workflow_id=None,
            result_summary="Done",
        )
    )

    assert guardrails.recorded[0][0] == "intaris-agent"
    await engine.dispose()


@pytest.mark.asyncio
async def test_deliver_task_result_creates_channel_follow_up_outbox(tmp_path: object) -> None:
    engine, session_factory = await _runtime(tmp_path)
    guardrails = _Guardrails()
    event_bus = EventBus()
    captured: list[object] = []

    async def _capture(event: object) -> None:
        captured.append(event)

    event_bus.subscribe_all(_capture)

    workflow_engine = WorkflowEngine(
        session_factory=session_factory,
        providers=SimpleNamespace(guardrails=guardrails),
        agent_loop=SimpleNamespace(),
        step_evaluator=SimpleNamespace(),
        workflow_registry=SimpleNamespace(),
        session_manager=SimpleNamespace(),
        event_bus=event_bus,
        pause_waiter=SimpleNamespace(),
    )

    async with session_factory() as session:
        await create_user(
            session, email="user@example.com", name="User", password_hash="hash", role="user"
        )
        await create_agent(
            session, agent_id="agent-1", owner_email="user@example.com", name="Agent"
        )
        conversation = await create_conversation(
            session,
            user_email="user@example.com",
            agent_id="agent-1",
            context_type="signal",
            context_ref="signal:acct-1:chat-1",
            context_data={
                "channel_type": "signal",
                "account_id": "acct-1",
                "chat_id": "chat-1",
            },
            title="Signal",
        )
        root_session = await create_session(
            session,
            conversation_id=conversation.conversation_id,
            user_email="user@example.com",
            agent_id="agent-1",
        )
        await update_conversation_active_session(
            session, conversation.conversation_id, root_session.session_id
        )
        await session.commit()

    await workflow_engine._deliver_task_result(
        TaskModel(
            task_id="task-chan-1",
            title="Background task",
            description="",
            status=TaskStatus.COMPLETED,
            priority=0,
            created_by="user@example.com",
            agent_id="agent-1",
            source_type="chat",
            source_ref=conversation.conversation_id,
            delivery=TaskDelivery(mode="same_conversation"),
            workflow_id=None,
            result_summary="Done",
        )
    )

    follow_up = next(
        event for event in captured if event.type == EventType.FOLLOW_UP_TURN_REQUESTED
    )
    delivery_id = follow_up.data.get("delivery_id")
    assert isinstance(delivery_id, str)
    assert follow_up.data.get("channel_deliverable") is True
    assert follow_up.data["follow_up"]["origin_kind"] == "task_result"

    async with session_factory() as session:
        row = await get_channel_delivery_outbox(session, delivery_id)
        assert row is not None
        assert row.status == "pending"
        assert row.channel_type == "signal"
        assert row.account_id == "acct-1"
        assert row.fallback_text == (
            'Task "Background task" completed.\n'
            "\n"
            "Summary: Done\n"
            "\n"
            "Task ID: task-chan-1\n"
            "\n"
            "Open the conversation for details."
        )

    await engine.dispose()


@pytest.mark.asyncio
async def test_default_scheduled_task_final_content_stays_agent_mediated(
    tmp_path: object,
) -> None:
    engine, session_factory = await _runtime(tmp_path)
    guardrails = _Guardrails()
    event_bus = EventBus()
    captured: list[object] = []

    async def _capture(event: object) -> None:
        captured.append(event)

    event_bus.subscribe_all(_capture)

    workflow_engine = WorkflowEngine(
        session_factory=session_factory,
        providers=SimpleNamespace(guardrails=guardrails),
        agent_loop=SimpleNamespace(),
        step_evaluator=SimpleNamespace(),
        workflow_registry=SimpleNamespace(),
        session_manager=SimpleNamespace(),
        event_bus=event_bus,
        pause_waiter=SimpleNamespace(),
    )

    async with session_factory() as session:
        await create_user(
            session, email="user@example.com", name="User", password_hash="hash", role="user"
        )
        await create_agent(
            session, agent_id="agent-1", owner_email="user@example.com", name="Agent"
        )
        conversation = await create_conversation(
            session,
            user_email="user@example.com",
            agent_id="agent-1",
            context_type="signal",
            context_ref="signal:acct-1:chat-1",
            context_data={
                "channel_type": "signal",
                "account_id": "acct-1",
                "chat_id": "chat-1",
            },
            title="Signal",
        )
        root_session = await create_session(
            session,
            conversation_id=conversation.conversation_id,
            user_email="user@example.com",
            agent_id="agent-1",
        )
        await set_session_intaris_session_id(session, root_session.session_id, "intaris-signal")
        await update_conversation_active_session(
            session, conversation.conversation_id, root_session.session_id
        )
        await session.commit()

    await workflow_engine._deliver_task_result_default(
        TaskModel(
            task_id="task-chan-deliverable",
            title="Background task",
            description="",
            status=TaskStatus.COMPLETED,
            priority=0,
            created_by="user@example.com",
            agent_id="agent-1",
            source_type="scheduler",
            source_ref="schedule-1",
            delivery=TaskDelivery(mode="preferred_channel"),
            workflow_id=None,
            result_summary="Done",
            result_data={
                "final_deliverable_id": "dlv_legacy",
                "final_content": "Bounded deliverable summary",
                "final_channel_content": "FULL DELIVERABLE CONTENT MUST NOT BE STORED IN OUTBOX",
                "final_format": "html",
            },
        ),
        conversation.conversation_id,
    )

    assert not any(event.type == EventType.TURN_COMPLETED for event in captured)
    follow_up = next(
        event for event in captured if event.type == EventType.FOLLOW_UP_TURN_REQUESTED
    )
    delivery_id = follow_up.data.get("delivery_id")
    assert isinstance(delivery_id, str)
    assert follow_up.data.get("channel_deliverable") is True

    async with session_factory() as session:
        row = await get_channel_delivery_outbox(session, delivery_id)
        assert row is not None
        assert row.session_id == root_session.session_id
        assert row.source_type == "task_result_follow_up"
        assert row.fallback_text.startswith('Task "Background task" completed.')
        assert "Bounded deliverable summary" not in row.fallback_text
        assert "FULL DELIVERABLE CONTENT" not in row.fallback_text

    await engine.dispose()


@pytest.mark.asyncio
async def test_deliver_failed_task_channel_fallback_identifies_task(tmp_path: object) -> None:
    engine, session_factory = await _runtime(tmp_path)
    guardrails = _Guardrails()
    event_bus = EventBus()
    captured: list[object] = []

    async def _capture(event: object) -> None:
        captured.append(event)

    event_bus.subscribe_all(_capture)

    workflow_engine = WorkflowEngine(
        session_factory=session_factory,
        providers=SimpleNamespace(guardrails=guardrails),
        agent_loop=SimpleNamespace(),
        step_evaluator=SimpleNamespace(),
        workflow_registry=SimpleNamespace(),
        session_manager=SimpleNamespace(),
        event_bus=event_bus,
        pause_waiter=SimpleNamespace(),
    )

    async with session_factory() as session:
        await create_user(
            session, email="user@example.com", name="User", password_hash="hash", role="user"
        )
        await create_agent(
            session, agent_id="agent-1", owner_email="user@example.com", name="Agent"
        )
        conversation = await create_conversation(
            session,
            user_email="user@example.com",
            agent_id="agent-1",
            context_type="signal",
            context_ref="signal:acct-1:chat-1",
            context_data={
                "channel_type": "signal",
                "account_id": "acct-1",
                "chat_id": "chat-1",
            },
            title="Signal",
        )
        root_session = await create_session(
            session,
            conversation_id=conversation.conversation_id,
            user_email="user@example.com",
            agent_id="agent-1",
        )
        await set_session_intaris_session_id(session, root_session.session_id, "intaris-signal")
        await update_conversation_active_session(
            session, conversation.conversation_id, root_session.session_id
        )
        await session.commit()

    await workflow_engine._deliver_task_result(
        TaskModel(
            task_id="task-failed-1",
            title="Build and rollout restart Cognis",
            description="",
            status=TaskStatus.FAILED,
            priority=0,
            created_by="user@example.com",
            agent_id="agent-1",
            source_type="chat",
            source_ref=conversation.conversation_id,
            delivery=TaskDelivery(mode="same_conversation"),
            workflow_id=None,
            result_summary="Failed to resolve step runtime.",
        )
    )

    follow_up = next(
        event for event in captured if event.type == EventType.FOLLOW_UP_TURN_REQUESTED
    )
    delivery_id = follow_up.data.get("delivery_id")
    assert isinstance(delivery_id, str)

    async with session_factory() as session:
        row = await get_channel_delivery_outbox(session, delivery_id)
        assert row is not None
        assert row.fallback_text == (
            'Task "Build and rollout restart Cognis" failed.\n'
            "\n"
            "Reason: Failed to resolve step runtime.\n"
            "\n"
            "Task ID: task-failed-1\n"
            "\n"
            "Open the conversation for details."
        )

    await engine.dispose()


@pytest.mark.asyncio
async def test_preferred_channel_delivery_uses_preferred_account_conversation(
    tmp_path: object,
) -> None:
    engine, session_factory = await _runtime(tmp_path)
    guardrails = _Guardrails()
    workflow_engine = WorkflowEngine(
        session_factory=session_factory,
        providers=SimpleNamespace(guardrails=guardrails),
        agent_loop=SimpleNamespace(),
        step_evaluator=SimpleNamespace(),
        workflow_registry=SimpleNamespace(),
        session_manager=SimpleNamespace(),
        event_bus=EventBus(),
        pause_waiter=SimpleNamespace(),
    )

    async with session_factory() as session:
        await create_user(
            session, email="user@example.com", name="User", password_hash="hash", role="user"
        )
        await create_agent(
            session, agent_id="agent-1", owner_email="user@example.com", name="Agent"
        )
        web_conversation = await create_conversation(
            session,
            user_email="user@example.com",
            agent_id="agent-1",
            context_type="web",
            title="Web",
        )
        signal_conversation = await create_conversation(
            session,
            user_email="user@example.com",
            agent_id="agent-1",
            context_type="signal",
            context_ref="signal:acct-preferred:chat-1",
            context_data={
                "channel_type": "signal",
                "account_id": "acct-preferred",
                "chat_id": "chat-1",
            },
            title="Signal",
        )
        await create_channel_account(
            session,
            account_id="acct-preferred",
            channel_type="signal",
            display_name="Signal",
            agent_id="agent-1",
            user_email="user@example.com",
            preferred_for_task_delivery=True,
        )
        web_session = await create_session(
            session,
            conversation_id=web_conversation.conversation_id,
            user_email="user@example.com",
            agent_id="agent-1",
        )
        signal_session = await create_session(
            session,
            conversation_id=signal_conversation.conversation_id,
            user_email="user@example.com",
            agent_id="agent-1",
        )
        await set_session_intaris_session_id(session, web_session.session_id, "intaris-web")
        await set_session_intaris_session_id(session, signal_session.session_id, "intaris-signal")
        await update_conversation_active_session(
            session, web_conversation.conversation_id, web_session.session_id
        )
        await update_conversation_active_session(
            session, signal_conversation.conversation_id, signal_session.session_id
        )
        await session.commit()

    await workflow_engine._deliver_task_result(
        TaskModel(
            task_id="task-preferred",
            title="Background task",
            description="",
            status=TaskStatus.COMPLETED,
            priority=0,
            created_by="user@example.com",
            agent_id="agent-1",
            source_type="api",
            source_ref=None,
            delivery=TaskDelivery(mode="preferred_channel"),
            workflow_id=None,
            result_summary="Done",
        )
    )

    assert guardrails.recorded[0][0] == "intaris-signal"
    await engine.dispose()


@pytest.mark.asyncio
async def test_preferred_channel_direct_delivery_uses_matrix_main_conversation_not_thread(
    tmp_path: object,
) -> None:
    engine, session_factory = await _runtime(tmp_path)
    channel_delivery = _ChannelDelivery()
    workflow_engine = WorkflowEngine(
        session_factory=session_factory,
        providers=SimpleNamespace(guardrails=_Guardrails()),
        agent_loop=SimpleNamespace(),
        step_evaluator=SimpleNamespace(),
        workflow_registry=SimpleNamespace(),
        session_manager=SimpleNamespace(),
        event_bus=EventBus(),
        pause_waiter=SimpleNamespace(),
        channel_delivery=channel_delivery,
    )

    async with session_factory() as session:
        await create_user(
            session, email="user@example.com", name="User", password_hash="hash", role="user"
        )
        await create_agent(
            session, agent_id="agent-1", owner_email="user@example.com", name="Agent"
        )
        main_conversation = await create_conversation(
            session,
            user_email="user@example.com",
            agent_id="agent-1",
            context_type="matrix",
            context_ref="matrix:acct-preferred:!room:example.com",
            context_data={
                "channel_type": "matrix",
                "account_id": "acct-preferred",
                "chat_id": "!room:example.com",
                "thread_id": None,
            },
            title="Matrix room",
        )
        thread_conversation = await create_conversation(
            session,
            user_email="user@example.com",
            agent_id="agent-1",
            context_type="matrix",
            context_ref="matrix:acct-preferred:!room:example.com:$thread",
            context_data={
                "channel_type": "matrix",
                "account_id": "acct-preferred",
                "chat_id": "!room:example.com",
                "thread_id": "$thread",
            },
            title="Matrix thread",
        )
        main_conversation.updated_at = datetime(2026, 1, 1, tzinfo=UTC)
        main_conversation.last_message_at = datetime(2026, 1, 1, tzinfo=UTC)
        thread_conversation.updated_at = datetime(2026, 1, 2, tzinfo=UTC)
        thread_conversation.last_message_at = datetime(2026, 1, 2, tzinfo=UTC)
        await create_channel_account(
            session,
            account_id="acct-preferred",
            channel_type="matrix",
            display_name="Matrix",
            agent_id="agent-1",
            user_email="user@example.com",
            preferred_for_task_delivery=True,
        )
        await session.commit()

    await workflow_engine._deliver_task_result(
        TaskModel(
            task_id="task-matrix-main",
            title="Background task",
            description="",
            status=TaskStatus.COMPLETED,
            priority=0,
            created_by="user@example.com",
            agent_id="agent-1",
            source_type="scheduler",
            source_ref=None,
            delivery=TaskDelivery(mode="preferred_channel"),
            completion_delivery=CompletionDeliveryPolicy(completion_mode_family="direct"),
            workflow_id=None,
            result_summary="Done",
            result_data={"final_content": "Matrix direct result"},
            applied_completion_mode="direct",
        )
    )

    assert channel_delivery.calls == [
        (main_conversation.conversation_id, "Matrix direct result", [])
    ]
    await engine.dispose()


@pytest.mark.asyncio
async def test_preferred_channel_delivery_falls_back_to_agent_direct_chat(
    tmp_path: object,
) -> None:
    engine, session_factory = await _runtime(tmp_path)
    guardrails = _Guardrails()
    session_manager = SessionManager(
        session_factory=session_factory,
        providers=SimpleNamespace(guardrails=guardrails),
        session_cache=SimpleNamespace(evict=AsyncMock()),
        event_bus=EventBus(),
    )
    workflow_engine = WorkflowEngine(
        session_factory=session_factory,
        providers=SimpleNamespace(guardrails=guardrails),
        agent_loop=SimpleNamespace(),
        step_evaluator=SimpleNamespace(),
        workflow_registry=SimpleNamespace(),
        session_manager=session_manager,
        event_bus=EventBus(),
        pause_waiter=SimpleNamespace(),
    )

    async with session_factory() as session:
        await create_user(
            session, email="user@example.com", name="User", password_hash="hash", role="user"
        )
        await create_agent(
            session,
            agent_id="agent-1",
            owner_email="user@example.com",
            name="Agent",
            status="active",
        )
        await session.commit()

    await workflow_engine._deliver_task_result(
        TaskModel(
            task_id="task-agent-direct",
            title="Background task",
            description="",
            status=TaskStatus.FAILED,
            priority=0,
            created_by="user@example.com",
            agent_id="agent-1",
            source_type="scheduler",
            source_ref=None,
            delivery=TaskDelivery(mode="preferred_channel"),
            workflow_id=None,
            result_summary="Failed",
        )
    )

    async with session_factory() as session:
        direct = await get_agent_direct_conversation(session, "user@example.com", "agent-1")
        assert direct is not None
        assert direct.context_type == "web"
        assert direct.context_data == {"kind": "agent_direct"}
        assert direct.last_message_at is not None

    await engine.dispose()


async def _runtime(tmp_path: object):
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path}/cognis.db")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    return engine, create_session_factory(engine)
