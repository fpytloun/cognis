from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from cognis.core.agent_loop import PauseWaiter, PendingPause
from cognis.core.events import EventBus
from cognis.core.turn_scheduler import TurnScheduler, _effective_user_content
from cognis.models.artifact import ArtifactKind, AttachmentRef
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


@pytest.mark.asyncio
async def test_follow_up_event_threads_channel_delivery_metadata() -> None:
    session_factory = SimpleNamespace()

    class _Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

    session_factory.__call__ = lambda self=None: _Session()  # type: ignore[attr-defined]

    scheduler = TurnScheduler(
        session_factory=lambda: _Session(),
        workflow_engine=SimpleNamespace(),
        decision_engine=SimpleNamespace(),
        task_queue=SimpleNamespace(),
        session_manager=SimpleNamespace(),
        session_cache=SimpleNamespace(),
        compaction_strategy=SimpleNamespace(),
        agent_loop=SimpleNamespace(),
        pause_waiter=PauseWaiter(),
        notification_service=SimpleNamespace(),
        providers=SimpleNamespace(),
        artifact_store=SimpleNamespace(),
        workflow_registry=SimpleNamespace(),
        event_bus=EventBus(),
    )

    scheduler.submit_turn = AsyncMock(return_value=None)  # type: ignore[method-assign]

    async def _get_conversation(_session, conversation_id: str):
        return SimpleNamespace(conversation_id=conversation_id, user_email="user@example.com")

    import cognis.store.queries as queries

    original = queries.get_conversation
    queries.get_conversation = _get_conversation  # type: ignore[assignment]
    try:
        await scheduler._handle_follow_up_event(
            SimpleNamespace(
                data={
                    "conversation_id": "conv-1",
                    "status": "completed",
                    "delivery_id": "cdel_1",
                    "channel_deliverable": True,
                    "delivery_fallback_text": "fallback",
                }
            )
        )
    finally:
        queries.get_conversation = original  # type: ignore[assignment]

    scheduler.submit_turn.assert_awaited_once()
    assert scheduler.submit_turn.await_args.kwargs["system_initiated"] is True
    assert scheduler.submit_turn.await_args.kwargs["channel_deliverable"] is True
    assert scheduler.submit_turn.await_args.kwargs["delivery_id"] == "cdel_1"
    assert scheduler.submit_turn.await_args.kwargs["delivery_fallback_text"] == "fallback"


@pytest.mark.asyncio
async def test_follow_up_event_publishes_turn_error_on_immediate_rejection() -> None:
    class _Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

    scheduler = TurnScheduler(
        session_factory=lambda: _Session(),
        workflow_engine=SimpleNamespace(),
        decision_engine=SimpleNamespace(),
        task_queue=SimpleNamespace(),
        session_manager=SimpleNamespace(),
        session_cache=SimpleNamespace(),
        compaction_strategy=SimpleNamespace(),
        agent_loop=SimpleNamespace(),
        pause_waiter=PauseWaiter(),
        notification_service=SimpleNamespace(),
        providers=SimpleNamespace(),
        artifact_store=SimpleNamespace(),
        workflow_registry=SimpleNamespace(),
        event_bus=EventBus(),
    )

    scheduler.submit_turn = AsyncMock(
        return_value=SimpleNamespace(code="session_ended", message="ended", recoverable=False)
    )  # type: ignore[method-assign]
    scheduler._publish_turn_error = AsyncMock()  # type: ignore[method-assign]

    async def _get_conversation(_session, conversation_id: str):
        return SimpleNamespace(
            conversation_id=conversation_id,
            user_email="user@example.com",
            active_session_id="sess-1",
        )

    import cognis.store.queries as queries

    original = queries.get_conversation
    queries.get_conversation = _get_conversation  # type: ignore[assignment]
    try:
        await scheduler._handle_follow_up_event(
            SimpleNamespace(
                data={
                    "conversation_id": "conv-1",
                    "status": "completed",
                    "delivery_id": "cdel_1",
                    "channel_deliverable": True,
                    "delivery_fallback_text": "fallback",
                }
            )
        )
    finally:
        queries.get_conversation = original  # type: ignore[assignment]

    scheduler._publish_turn_error.assert_awaited_once()


def test_effective_user_content_describes_audio_only_turns() -> None:
    assert _effective_user_content("hello", []) == "hello"
    assert _effective_user_content("", []) == ""
    assert (
        _effective_user_content(
            "",
            [
                AttachmentRef(
                    artifact_id="att-1",
                    kind=ArtifactKind.AUDIO,
                    mime_type="audio/ogg",
                    filename="voice.ogg",
                    size_bytes=10,
                )
            ],
        )
        == "User attached an audio file."
    )
