from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from cognis.core.events import EventBus, EventType
from cognis.core.workflow_engine import WorkflowEngine
from cognis.models.task import TaskDelivery, TaskModel, TaskStatus
from cognis.models.workflow import CompletionDeliveryPolicy, WorkflowState
from cognis.store.database import create_engine, create_session_factory
from cognis.store.models import Base
from cognis.store.queries import (
    create_agent,
    create_conversation,
    create_session,
    create_user,
    get_channel_delivery_outbox,
    set_session_intaris_session_id,
    update_conversation_active_session,
)


class _Guardrails:
    def __init__(self) -> None:
        self.recorded: list[tuple[str, list[object]]] = []

    async def record_events(
        self, session_id: str, events: list[object], source: str = "cognis"
    ) -> object:
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
    ) -> bool:
        self.calls.append((conversation_id, content, attachments))
        return self.sent


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
async def test_deliver_task_result_skips_silent_delivery(tmp_path: object) -> None:
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
    assert seen == []
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
        await set_session_intaris_session_id(session, root_session.session_id, "intaris-signal")
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

    await engine.dispose()


async def _runtime(tmp_path: object):
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path}/cognis.db")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    return engine, create_session_factory(engine)
