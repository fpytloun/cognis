from __future__ import annotations

from types import SimpleNamespace

import pytest

from cognis.core.agent_loop import PauseWaiter, PendingPause
from cognis.core.events import EventBus
from cognis.core.turn_scheduler import TurnScheduler
from cognis.models.session import SessionStatus


@pytest.mark.asyncio
async def test_submit_turn_blocks_same_conversation_for_live_direct_question() -> None:
    pause_waiter = PauseWaiter()
    pause_waiter.register(
        PendingPause(
            pause_id="notif-1",
            pause_type="step_question",
            conversation_id="conv-1",
            session_id="sess-1",
        )
    )

    scheduler = TurnScheduler(
        session_factory=SimpleNamespace(),
        workflow_engine=SimpleNamespace(),
        decision_engine=SimpleNamespace(),
        task_queue=SimpleNamespace(),
        session_manager=SimpleNamespace(),
        session_cache=SimpleNamespace(),
        compaction_strategy=SimpleNamespace(),
        agent_loop=SimpleNamespace(),
        pause_waiter=pause_waiter,
        notification_service=SimpleNamespace(),
        providers=SimpleNamespace(),
        artifact_store=SimpleNamespace(),
        workflow_registry=SimpleNamespace(),
        event_bus=EventBus(),
    )

    async def _runtime(_: str, **__: object) -> tuple[object, object, object, bool]:
        return (
            SimpleNamespace(
                conversation_id="conv-1", user_email="user@example.com", status="active"
            ),
            SimpleNamespace(session_id="sess-1", status=SessionStatus.ACTIVE),
            SimpleNamespace(agent_id="agent-1"),
            False,
        )

    async def _attachments(**_: object) -> tuple[list[object], object]:
        return [], None

    async def _notice(**_: object) -> None:
        return None

    scheduler._load_conversation_runtime = _runtime  # type: ignore[method-assign]
    scheduler._resolve_attachments_for_turn = _attachments  # type: ignore[method-assign]
    scheduler._build_attachment_notice = _notice  # type: ignore[method-assign]

    error = await scheduler.submit_turn(
        "conv-1",
        "hello",
        user_email="user@example.com",
    )

    assert error is not None
    assert error.code == "pending_question"


@pytest.mark.asyncio
async def test_submit_turn_ignores_task_backed_step_questions() -> None:
    pause_waiter = PauseWaiter()
    pause_waiter.register(
        PendingPause(
            pause_id="notif-task",
            pause_type="step_question",
            conversation_id="conv-1",
            session_id="sess-1",
            task_id="task-1",
        )
    )

    scheduler = TurnScheduler(
        session_factory=SimpleNamespace(),
        workflow_engine=SimpleNamespace(),
        decision_engine=SimpleNamespace(),
        task_queue=SimpleNamespace(),
        session_manager=SimpleNamespace(),
        session_cache=SimpleNamespace(),
        compaction_strategy=SimpleNamespace(),
        agent_loop=SimpleNamespace(),
        pause_waiter=pause_waiter,
        notification_service=SimpleNamespace(),
        providers=SimpleNamespace(),
        artifact_store=SimpleNamespace(),
        workflow_registry=SimpleNamespace(),
        event_bus=EventBus(),
    )

    async def _runtime(_: str, **__: object) -> tuple[object, object, object, bool]:
        return (
            SimpleNamespace(
                conversation_id="conv-1", user_email="user@example.com", status="active"
            ),
            SimpleNamespace(session_id="sess-1", status=SessionStatus.ACTIVE),
            SimpleNamespace(agent_id="agent-1"),
            False,
        )

    async def _attachments(**_: object) -> tuple[list[object], object]:
        return [], None

    async def _notice(**_: object) -> None:
        return None

    scheduler._load_conversation_runtime = _runtime  # type: ignore[method-assign]
    scheduler._resolve_attachments_for_turn = _attachments  # type: ignore[method-assign]
    scheduler._build_attachment_notice = _notice  # type: ignore[method-assign]
    scheduler._launch_turn = lambda **_: None  # type: ignore[assignment]

    error = await scheduler.submit_turn(
        "conv-1",
        "hello",
        user_email="user@example.com",
    )

    assert error is None
