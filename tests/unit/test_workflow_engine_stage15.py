from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from cognis.core.events import EventBus, EventType
from cognis.core.workflow_engine import WorkflowEngine
from cognis.models.task import TaskDelivery, TaskModel, TaskStatus
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
            delivery=TaskDelivery(mode="silent"),
            workflow_id=None,
            result_summary="Done",
        )
    )

    assert guardrails.recorded == []
    assert seen == []
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
