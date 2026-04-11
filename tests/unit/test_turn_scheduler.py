from __future__ import annotations

import asyncio
import contextlib
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from cognis.core.agent_loop import PauseWaiter, PendingPause
from cognis.core.events import EventBus, EventType
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
async def test_submit_turn_only_notifies_once_per_pending_escalation() -> None:
    pause_waiter = PauseWaiter()
    pause_waiter.register(
        PendingPause(
            pause_id="esc-1",
            pause_type="escalation",
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
    scheduler._notify_observers_system_message = AsyncMock()  # type: ignore[method-assign]

    first = await scheduler.submit_turn("conv-1", "hello", user_email="user@example.com")
    second = await scheduler.submit_turn("conv-1", "hello again", user_email="user@example.com")

    assert first is None
    assert second is None
    scheduler._notify_observers_system_message.assert_awaited_once()
    assert len(scheduler._queued_messages["conv-1"]) == 2


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


@pytest.mark.asyncio
async def test_run_turn_publishes_effective_user_message_content() -> None:
    event_bus = EventBus()
    observed: list[object] = []

    async def _record(event: object) -> None:
        observed.append(event)

    event_bus.subscribe(EventType.USER_MESSAGE, _record)

    scheduler = TurnScheduler(
        session_factory=SimpleNamespace(),
        workflow_engine=SimpleNamespace(run_direct_turn=AsyncMock(return_value=SimpleNamespace())),
        decision_engine=SimpleNamespace(
            decide=AsyncMock(return_value=SimpleNamespace(decision="inline"))
        ),
        task_queue=SimpleNamespace(),
        session_manager=SimpleNamespace(),
        session_cache=SimpleNamespace(
            refresh=AsyncMock(return_value=SimpleNamespace(last_event_seq=0)),
            get_context_usage=MagicMock(return_value=None),
            get_entry=MagicMock(return_value=None),
        ),
        compaction_strategy=SimpleNamespace(),
        agent_loop=SimpleNamespace(),
        pause_waiter=PauseWaiter(),
        notification_service=SimpleNamespace(),
        providers=SimpleNamespace(),
        artifact_store=SimpleNamespace(),
        workflow_registry=SimpleNamespace(),
        event_bus=event_bus,
    )
    scheduler._publish_turn_completed = AsyncMock()  # type: ignore[method-assign]
    scheduler._touch_conversation = AsyncMock()  # type: ignore[method-assign]

    await scheduler._run_turn(
        conversation=SimpleNamespace(
            conversation_id="conv-1", title="", user_email="user@example.com"
        ),
        session=SimpleNamespace(session_id="sess-1"),
        agent=SimpleNamespace(agent_id="agent-1"),
        content="",
        user_email="user@example.com",
        attachments=[
            AttachmentRef(
                artifact_id="att-1",
                kind=ArtifactKind.AUDIO,
                mime_type="audio/ogg",
                filename="voice.ogg",
                size_bytes=10,
            )
        ],
        outbound_attachments=None,
        attachment_notice=None,
        system_initiated=False,
        channel_deliverable=False,
        delivery_id=None,
        delivery_fallback_text=None,
        bootstrap_wait_for_intention=False,
        cancel_event=AsyncMock(),
    )

    user_events = [
        event for event in observed if getattr(event, "type", None) == EventType.USER_MESSAGE
    ]
    assert len(user_events) == 1
    assert user_events[0].data["content"] == "User attached an audio file."


@pytest.mark.asyncio
async def test_cancel_turn_cancels_active_task() -> None:
    scheduler = TurnScheduler(
        session_factory=SimpleNamespace(),
        workflow_engine=SimpleNamespace(),
        decision_engine=SimpleNamespace(),
        task_queue=SimpleNamespace(),
        session_manager=SimpleNamespace(),
        session_cache=SimpleNamespace(),
        compaction_strategy=SimpleNamespace(),
        agent_loop=SimpleNamespace(cancel_children=AsyncMock(return_value=0)),
        pause_waiter=PauseWaiter(),
        notification_service=SimpleNamespace(),
        providers=SimpleNamespace(),
        artifact_store=SimpleNamespace(),
        workflow_registry=SimpleNamespace(),
        event_bus=EventBus(),
    )

    started = asyncio.Event()

    async def _hang() -> None:
        started.set()
        await asyncio.Future()

    task = asyncio.create_task(_hang())
    scheduler._active_turns["conv-1"] = task
    scheduler._turn_controls["conv-1"] = asyncio.Event()
    scheduler._turn_sessions["conv-1"] = "sess-1"

    await started.wait()

    cancelled = await scheduler.cancel_turn("conv-1")
    assert cancelled is True

    with contextlib.suppress(asyncio.CancelledError):
        await task
    assert task.cancelled() is True
