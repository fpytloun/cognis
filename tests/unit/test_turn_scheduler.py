from __future__ import annotations

import asyncio
import contextlib
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from cognis.core.agent_loop import PauseWaiter, PendingPause
from cognis.core.attachment_utils import normalize_attachment_refs, strip_attachment_payload_bytes
from cognis.core.events import Event, EventBus, EventType
from cognis.core.followups import (
    FollowUpMode,
    FollowUpOriginKind,
    FollowUpRelevanceHint,
    FollowUpRequiredAction,
    FollowUpStatus,
    TaskResultFollowUp,
)
from cognis.core.turn_scheduler import (
    ActiveToolOutputSnapshot,
    TurnScheduler,
    _effective_user_content,
    _QueuedMessage,
    _TurnControl,
)
from cognis.models.agent import AgentDefinition
from cognis.models.artifact import ArtifactKind, AttachmentRef
from cognis.models.session import SessionStatus
from cognis.store.models import Base
from cognis.store.queries import create_agent, create_conversation, create_user, get_conversation


class _NoopAsyncContext:
    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False


class _RecordingObserver:
    def __init__(self) -> None:
        self.tokens: list[str] = []
        self.system_messages: list[str] = []
        self.completed: list[str] = []
        self.queued: list[int] = []
        self.queued_messages: list[list[dict[str, object]]] = []

    async def on_token(
        self,
        conversation_id: str,
        session_id: str,
        message_id: str,
        turn_id: str | None,
        delta: str,
        chunk_index: int | None = None,
        content_offset: int | None = None,
    ) -> None:
        self.tokens.append(delta)

    async def on_tool_call(
        self,
        conversation_id: str,
        session_id: str,
        call_id: str,
        tool_name: str,
        arguments: dict[str, object] | None,
        turn_id: str | None,
    ) -> None:
        return None

    async def on_tool_result(
        self,
        conversation_id: str,
        session_id: str,
        call_id: str,
        tool_name: str,
        result: str,
        is_error: bool,
        duration_ms: int | None,
        evaluation: dict[str, object] | None,
        attachments: list[dict[str, object]] | None = None,
        file_diffs: list[dict[str, object]] | None = None,
        turn_id: str | None = None,
        presentation: dict[str, object] | None = None,
    ) -> None:
        return None

    async def on_tool_output_chunk(
        self,
        conversation_id: str,
        session_id: str,
        call_id: str,
        tool_name: str,
        delta: str,
        stream: str | None,
        turn_id: str | None = None,
        chunk_index: int | None = None,
        content_offset: int | None = None,
    ) -> None:
        return None

    async def on_thinking(
        self,
        conversation_id: str,
        session_id: str,
        message_id: str,
        turn_id: str | None,
        block_id: str,
        delta: str,
        title: str | None,
        complete: bool,
        content: str | None = None,
        started_at: str | None = None,
        completed_at: str | None = None,
        duration_ms: int | None = None,
        source: str | None = None,
        provider_block_index: int | None = None,
    ) -> None:
        return None

    async def on_turn_complete(self, result: object) -> None:
        self.completed.append(getattr(result, "message_id", ""))

    async def on_turn_error(self, conversation_id: str, error: object) -> None:
        return None

    async def on_system_message(self, conversation_id: str, text: str) -> None:
        self.system_messages.append(text)

    async def on_queued(self, conversation_id: str, queued_count: int) -> None:
        self.queued.append(queued_count)

    async def on_queued_messages(
        self, conversation_id: str, messages: list[dict[str, object]]
    ) -> None:
        self.queued_messages.append(messages)


class _LegacyThinkingObserver:
    def __init__(self) -> None:
        self.thinking: list[tuple[str, str, str | None, bool, str | None]] = []

    async def on_thinking(
        self,
        conversation_id: str,
        session_id: str,
        message_id: str,
        turn_id: str | None,
        block_id: str,
        delta: str,
        title: str | None,
        complete: bool,
        content: str | None = None,
    ) -> None:
        self.thinking.append((block_id, delta, title, complete, content))


class _CommitSession:
    def __init__(self) -> None:
        self.commits = 0

    async def __aenter__(self) -> _CommitSession:
        return self

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> bool:
        return False

    async def commit(self) -> None:
        self.commits += 1


def _scheduler_for_redo_invalidation(session_factory: object) -> TurnScheduler:
    scheduler = TurnScheduler(
        session_factory=session_factory,
        workflow_engine=SimpleNamespace(),
        decision_engine=SimpleNamespace(),
        task_queue=SimpleNamespace(),
        session_manager=SimpleNamespace(mark_active=AsyncMock(return_value=False)),
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

    async def _runtime(_: str, **__: object) -> tuple[object, object, object, bool]:
        return (
            SimpleNamespace(
                conversation_id="conv-1", user_email="user@example.com", status="active"
            ),
            SimpleNamespace(session_id="sess-1", status=SessionStatus.ACTIVE),
            SimpleNamespace(agent_id="agent-1", owner_email="user@example.com"),
            False,
        )

    scheduler._load_conversation_runtime = _runtime  # type: ignore[method-assign]
    scheduler._resolve_attachments_for_turn = AsyncMock(return_value=([], None))  # type: ignore[method-assign]
    scheduler._build_attachment_notice = AsyncMock(return_value=None)  # type: ignore[method-assign]
    scheduler._load_turn_limits = AsyncMock(return_value=(10, 10))  # type: ignore[method-assign]
    scheduler._launch_turn = MagicMock()  # type: ignore[method-assign]
    return scheduler


@pytest.mark.asyncio
async def test_accepted_immediate_turn_clears_redo_metadata(monkeypatch) -> None:
    db_session = _CommitSession()
    clear_metadata = AsyncMock(return_value=True)
    monkeypatch.setattr(
        "cognis.core.turn_scheduler.queries.clear_conversation_history_rebase_metadata",
        clear_metadata,
    )
    scheduler = _scheduler_for_redo_invalidation(lambda: db_session)

    error = await scheduler.submit_turn("conv-1", "hello", user_email="user@example.com")

    assert error is None
    clear_metadata.assert_awaited_once_with(db_session, "conv-1")
    assert db_session.commits == 1


@pytest.mark.asyncio
async def test_accepted_queued_turn_clears_redo_metadata(monkeypatch) -> None:
    db_session = _CommitSession()
    clear_metadata = AsyncMock(return_value=True)
    monkeypatch.setattr(
        "cognis.core.turn_scheduler.queries.clear_conversation_history_rebase_metadata",
        clear_metadata,
    )
    scheduler = _scheduler_for_redo_invalidation(lambda: db_session)
    scheduler._active_turns["conv-1"] = asyncio.create_task(asyncio.sleep(60))
    try:
        error = await scheduler.submit_turn("conv-1", "hello", user_email="user@example.com")
    finally:
        scheduler._active_turns["conv-1"].cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await scheduler._active_turns["conv-1"]

    assert error is None
    clear_metadata.assert_awaited_once_with(db_session, "conv-1")
    assert db_session.commits == 1


@pytest.mark.asyncio
async def test_rejected_turn_preserves_redo_metadata(monkeypatch) -> None:
    clear_metadata = AsyncMock(return_value=True)
    monkeypatch.setattr(
        "cognis.core.turn_scheduler.queries.clear_conversation_history_rebase_metadata",
        clear_metadata,
    )
    scheduler = _scheduler_for_redo_invalidation(lambda: _CommitSession())
    scheduler._load_turn_limits = AsyncMock(return_value=(0, 10))  # type: ignore[method-assign]

    error = await scheduler.submit_turn("conv-1", "hello", user_email="user@example.com")

    assert error is not None
    assert error.code == "rate_limited"
    clear_metadata.assert_not_awaited()


@pytest.mark.asyncio
async def test_slash_command_turn_preserves_redo_metadata(monkeypatch) -> None:
    clear_metadata = AsyncMock(return_value=True)
    monkeypatch.setattr(
        "cognis.core.turn_scheduler.queries.clear_conversation_history_rebase_metadata",
        clear_metadata,
    )
    scheduler = _scheduler_for_redo_invalidation(lambda: _CommitSession())

    error = await scheduler.submit_turn("conv-1", "/undo", user_email="user@example.com")

    assert error is None
    clear_metadata.assert_not_awaited()


@pytest.mark.asyncio
async def test_submit_turn_uses_configurable_per_user_limit() -> None:
    scheduler = TurnScheduler(
        session_factory=SimpleNamespace(),
        workflow_engine=SimpleNamespace(),
        decision_engine=SimpleNamespace(),
        task_queue=SimpleNamespace(),
        session_manager=SimpleNamespace(refresh_intaris_session_policy=AsyncMock()),
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

    async def _runtime(conversation_id: str, **__: object) -> tuple[object, object, object, bool]:
        return (
            SimpleNamespace(
                conversation_id=conversation_id, user_email="user@example.com", status="active"
            ),
            SimpleNamespace(session_id=f"sess-{conversation_id}", status=SessionStatus.ACTIVE),
            SimpleNamespace(agent_id="agent-1"),
            False,
        )

    scheduler._load_conversation_runtime = _runtime  # type: ignore[method-assign]
    scheduler._resolve_attachments_for_turn = AsyncMock(return_value=([], None))  # type: ignore[method-assign]
    scheduler._build_attachment_notice = AsyncMock(return_value=None)  # type: ignore[method-assign]
    scheduler._load_turn_limits = AsyncMock(return_value=(1, 20))  # type: ignore[method-assign]

    first = await scheduler.submit_turn("conv-1", "hello", user_email="user@example.com")
    second = await scheduler.submit_turn("conv-2", "hello", user_email="user@example.com")

    assert first is None
    assert second is not None
    assert second.code == "rate_limited"

    scheduler._active_turns["conv-1"].cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await scheduler._active_turns["conv-1"]


@pytest.mark.asyncio
async def test_has_running_turn_hides_settled_cleanup_state() -> None:
    scheduler = TurnScheduler(
        session_factory=SimpleNamespace(),
        workflow_engine=SimpleNamespace(),
        decision_engine=SimpleNamespace(),
        task_queue=SimpleNamespace(),
        session_manager=SimpleNamespace(refresh_intaris_session_policy=AsyncMock()),
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
    task = asyncio.create_task(asyncio.sleep(60))
    try:
        scheduler._active_turns["conv-1"] = task
        scheduler._turn_controls["conv-1"] = _TurnControl()

        assert scheduler.has_active_turn("conv-1") is True
        assert scheduler.has_running_turn("conv-1") is True

        scheduler._turn_controls["conv-1"].settled = True

        assert scheduler.has_active_turn("conv-1") is True
        assert scheduler.has_running_turn("conv-1") is False
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


@pytest.mark.asyncio
async def test_active_stream_snapshots_track_unpersisted_assistant_text() -> None:
    scheduler = TurnScheduler(
        session_factory=SimpleNamespace(),
        workflow_engine=SimpleNamespace(),
        decision_engine=SimpleNamespace(),
        task_queue=SimpleNamespace(),
        session_manager=SimpleNamespace(refresh_intaris_session_policy=AsyncMock()),
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
    observer = _RecordingObserver()
    on_token, _on_thinking, on_tool_call, _on_tool_result, _on_tool_output_chunk = (
        scheduler._build_callbacks(
            "conv-1",
            "sess-1",
            "turn-1",
            "turn-1",
            turn_observers=(observer,),
        )
    )

    await on_token("Hello")
    await on_token(" world")

    snapshots = await scheduler.active_stream_snapshots("conv-1")
    assert snapshots == [
        {
            "conversation_id": "conv-1",
            "session_id": "sess-1",
            "message_id": "turn-1",
            "turn_id": "turn-1",
            "content": "Hello world",
            "chunk_count": 2,
            "content_offset": 11,
            "updated_at": snapshots[0]["updated_at"],
        }
    ]
    assert observer.tokens == ["Hello", " world"]

    await on_tool_call("example_tool", "call-1", {})
    assert await scheduler.active_stream_snapshots("conv-1") == []


@pytest.mark.asyncio
async def test_thinking_callback_trims_metadata_for_legacy_observers() -> None:
    scheduler = TurnScheduler(
        session_factory=SimpleNamespace(),
        workflow_engine=SimpleNamespace(),
        decision_engine=SimpleNamespace(),
        task_queue=SimpleNamespace(),
        session_manager=SimpleNamespace(refresh_intaris_session_policy=AsyncMock()),
        session_cache=SimpleNamespace(update_active_thinking=MagicMock()),
        compaction_strategy=SimpleNamespace(),
        agent_loop=SimpleNamespace(),
        pause_waiter=PauseWaiter(),
        notification_service=SimpleNamespace(),
        providers=SimpleNamespace(),
        artifact_store=SimpleNamespace(),
        workflow_registry=SimpleNamespace(),
        event_bus=EventBus(),
    )
    observer = _LegacyThinkingObserver()
    _on_token, on_thinking, _on_tool_call, _on_tool_result, _on_tool_output_chunk = (
        scheduler._build_callbacks(
            "conv-1",
            "sess-1",
            "turn-1",
            "turn-1",
            turn_observers=(observer,),
        )
    )

    await on_thinking(
        "thk_request_1",
        "Thinking",
        "Reasoning title",
        False,
        None,
        "2026-04-20T00:00:00Z",
        None,
        None,
        "summary",
        0,
    )

    assert observer.thinking == [("thk_request_1", "Thinking", "Reasoning title", False, None)]


@pytest.mark.asyncio
async def test_active_tool_output_snapshots_are_bounded_and_completed() -> None:
    scheduler = TurnScheduler(
        session_factory=SimpleNamespace(),
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

    await scheduler._append_active_tool_output_chunk(
        conversation_id="conv-1",
        session_id="sess-1",
        call_id="call-1",
        tool_name="bash",
        turn_id="turn-1",
        delta="hello",
        stream="stdout",
    )
    snapshots = await scheduler.active_tool_output_snapshots("conv-1")
    assert len(snapshots) == 1
    assert snapshots[0]["result"] == "hello"
    assert snapshots[0]["status"] == "running"

    await scheduler._finalize_active_tool_output(
        conversation_id="conv-1",
        session_id="sess-1",
        call_id="call-1",
        tool_name="bash",
        turn_id="turn-1",
        result="cut",
        is_error=False,
        metadata={"transport_truncated": True, "output_size": 5},
    )
    snapshots = await scheduler.active_tool_output_snapshots("conv-1")
    assert snapshots[0]["status"] == "completed"
    assert snapshots[0]["result"] == "hello"
    assert "_raw_output" not in snapshots[0]


@pytest.mark.asyncio
async def test_active_tool_output_chunk_offsets_remain_monotonic_after_truncation() -> None:
    scheduler = TurnScheduler(
        session_factory=SimpleNamespace(),
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

    first_index, first_offset = await scheduler._append_active_tool_output_chunk(
        conversation_id="conv-1",
        session_id="sess-1",
        call_id="call-1",
        tool_name="bash",
        turn_id="turn-1",
        delta="a" * 70_000,
        stream="stdout",
    )
    second_index, second_offset = await scheduler._append_active_tool_output_chunk(
        conversation_id="conv-1",
        session_id="sess-1",
        call_id="call-1",
        tool_name="bash",
        turn_id="turn-1",
        delta="b",
        stream="stdout",
    )
    snapshots = await scheduler.active_tool_output_snapshots("conv-1")

    assert (first_index, first_offset) == (0, 0)
    assert (second_index, second_offset) == (1, 70_000)
    assert snapshots[0]["content_offset"] == 70_001
    assert snapshots[0]["output_size"] == 70_001
    assert snapshots[0]["truncated"] is True


@pytest.mark.asyncio
async def test_active_tool_output_snapshots_filter_expired_items() -> None:
    scheduler = TurnScheduler(
        session_factory=SimpleNamespace(),
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
    stale = scheduler._active_tool_outputs[("conv-1", "sess-1", "call-1")] = (
        ActiveToolOutputSnapshot(
            conversation_id="conv-1",
            session_id="sess-1",
            call_id="call-1",
            tool_name="bash",
            turn_id="turn-1",
            result="stale",
        )
    )
    stale.updated_at = stale.updated_at - timedelta(hours=7)

    assert await scheduler.active_tool_output_snapshots("conv-1") == []
    assert scheduler._active_tool_outputs == {}


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
            SimpleNamespace(agent_id="agent-1", owner_email="user@example.com"),
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
async def test_submit_turn_blocks_same_conversation_for_live_auth_challenge() -> None:
    pause_waiter = PauseWaiter()
    pause_waiter.register(
        PendingPause(
            pause_id="auth-1",
            pause_type="auth_challenge",
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
            SimpleNamespace(agent_id="agent-1", owner_email="user@example.com"),
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
        "123456",
        user_email="user@example.com",
    )

    assert error is not None
    assert error.code == "pending_input_request"


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
            SimpleNamespace(agent_id="agent-1", owner_email="user@example.com"),
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
async def test_build_attachment_notice_uses_pdf_text_fallback() -> None:
    scheduler = TurnScheduler(
        session_factory=SimpleNamespace(),
        workflow_engine=SimpleNamespace(),
        decision_engine=SimpleNamespace(),
        task_queue=SimpleNamespace(),
        session_manager=SimpleNamespace(),
        session_cache=SimpleNamespace(get_model_override=lambda _sid: None),
        compaction_strategy=SimpleNamespace(),
        agent_loop=SimpleNamespace(),
        pause_waiter=PauseWaiter(),
        notification_service=SimpleNamespace(),
        providers=SimpleNamespace(
            llm=SimpleNamespace(
                resolve_model_target=AsyncMock(return_value=("model-a", None)),
                get_model_info=AsyncMock(
                    return_value=SimpleNamespace(
                        supports_vision=False,
                        supports_pdf_input=False,
                        supports_audio_input=False,
                        supports_file_input=False,
                    )
                ),
            )
        ),
        artifact_store=SimpleNamespace(),
        workflow_registry=SimpleNamespace(),
        event_bus=EventBus(),
    )

    async def _extract_pdf_text(_attachment: AttachmentRef) -> str | None:
        return "Extracted text from spec.pdf:\nHello PDF"

    scheduler._extract_pdf_text = _extract_pdf_text  # type: ignore[method-assign]

    notice = await scheduler._build_attachment_notice(
        session=SimpleNamespace(session_id="sess-1"),
        agent=AgentDefinition(
            agent_id="agent-1",
            owner_email="user@example.com",
            name="Agent",
        ),
        attachments=[
            AttachmentRef(
                artifact_id="art-1",
                kind=ArtifactKind.PDF,
                mime_type="application/pdf",
                filename="spec.pdf",
                size_bytes=123,
            )
        ],
    )

    assert notice is not None
    assert "using extracted text fallback" in notice
    assert "artifact_read" in notice
    assert "Extracted text from spec.pdf" not in notice

    context = await scheduler._build_attachment_context(
        session=SimpleNamespace(session_id="sess-1"),
        agent=AgentDefinition(
            agent_id="agent-1",
            owner_email="user@example.com",
            name="Agent",
        ),
        attachments=[
            AttachmentRef(
                artifact_id="art-1",
                kind=ArtifactKind.PDF,
                mime_type="application/pdf",
                filename="spec.pdf",
                size_bytes=123,
            )
        ],
    )

    assert context is not None
    assert '<attachment_context trust="untrusted">' in context
    assert "Extracted text from spec.pdf" in context


def test_attachment_normalization_accepts_pydantic_refs() -> None:
    attachment = AttachmentRef(
        artifact_id="art-1",
        kind=ArtifactKind.IMAGE,
        mime_type="image/png",
        filename="image.png",
        size_bytes=123,
        url="http://example.test/image.png",
    )

    normalized = normalize_attachment_refs([attachment])
    safe = strip_attachment_payload_bytes([attachment])

    assert normalized == [
        {
            "artifact_id": "art-1",
            "kind": "image",
            "mime_type": "image/png",
            "filename": "image.png",
            "size_bytes": 123,
            "url": "http://example.test/image.png",
        }
    ]
    assert safe == normalized


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
async def test_submit_turn_uses_configurable_queue_limit() -> None:
    scheduler = TurnScheduler(
        session_factory=SimpleNamespace(),
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

    conversation = SimpleNamespace(
        conversation_id="conv-1",
        user_email="user@example.com",
        status="active",
    )
    session = SimpleNamespace(status=SessionStatus.ACTIVE)
    agent = SimpleNamespace()
    scheduler._load_conversation_runtime = AsyncMock(  # type: ignore[method-assign]
        return_value=(conversation, session, agent, False)
    )
    scheduler._build_attachment_support_messages = AsyncMock(return_value=(None, None))  # type: ignore[method-assign]
    scheduler._load_turn_limits = AsyncMock(return_value=(20, 1))  # type: ignore[method-assign]
    scheduler._active_turns["conv-1"] = asyncio.create_task(asyncio.sleep(1))

    first = await scheduler.submit_turn("conv-1", "one", user_email="user@example.com")
    second = await scheduler.submit_turn("conv-1", "two", user_email="user@example.com")

    assert first is None
    assert second is not None
    assert second.code == "queue_full"

    scheduler._active_turns["conv-1"].cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await scheduler._active_turns["conv-1"]


@pytest.mark.asyncio
async def test_submit_turn_touches_conversation_when_user_message_is_accepted(
    tmp_path: Path,
) -> None:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'activity.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)

    async with session_factory() as db_session:
        await create_user(db_session, "user@example.com", "User", "hash")
        await create_agent(
            db_session,
            agent_id="agent-1",
            owner_email="user@example.com",
            name="Agent",
            status="active",
        )
        conversation = await create_conversation(
            db_session,
            user_email="user@example.com",
            agent_id="agent-1",
            context_type="web",
        )
        await db_session.commit()

    scheduler = TurnScheduler(
        session_factory=session_factory,
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
    scheduler._load_conversation_runtime = AsyncMock(  # type: ignore[method-assign]
        return_value=(
            SimpleNamespace(
                conversation_id=conversation.conversation_id,
                user_email="user@example.com",
                status="active",
            ),
            SimpleNamespace(session_id="sess-1", status=SessionStatus.ACTIVE),
            SimpleNamespace(agent_id="agent-1", owner_email="user@example.com"),
            False,
        )
    )
    scheduler._build_attachment_support_messages = AsyncMock(return_value=(None, None))  # type: ignore[method-assign]
    scheduler._load_turn_limits = AsyncMock(return_value=(20, 20))  # type: ignore[method-assign]
    scheduler._active_turns[conversation.conversation_id] = asyncio.create_task(asyncio.sleep(1))

    try:
        error = await scheduler.submit_turn(
            conversation.conversation_id,
            "queued user message",
            user_email="user@example.com",
        )

        assert error is None
        async with session_factory() as db_session:
            row = await get_conversation(db_session, conversation.conversation_id)
            assert row is not None
            assert row.last_message_at is not None
            assert row.updated_at == row.last_message_at
    finally:
        scheduler._active_turns[conversation.conversation_id].cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await scheduler._active_turns[conversation.conversation_id]
        await engine.dispose()


@pytest.mark.asyncio
async def test_submit_turn_touches_conversation_before_launch() -> None:
    scheduler = TurnScheduler(
        session_factory=lambda: _NoopAsyncContext(),
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
    scheduler._load_conversation_runtime = AsyncMock(  # type: ignore[method-assign]
        return_value=(
            SimpleNamespace(
                conversation_id="conv-1", user_email="user@example.com", status="active"
            ),
            SimpleNamespace(session_id="sess-1", status=SessionStatus.ACTIVE),
            SimpleNamespace(agent_id="agent-1", owner_email="user@example.com"),
            False,
        )
    )
    scheduler._build_attachment_support_messages = AsyncMock(return_value=(None, None))  # type: ignore[method-assign]
    scheduler._load_turn_limits = AsyncMock(return_value=(20, 20))  # type: ignore[method-assign]
    scheduler._touch_conversation = AsyncMock()  # type: ignore[method-assign]
    scheduler._launch_turn = MagicMock()  # type: ignore[method-assign]

    error = await scheduler.submit_turn("conv-1", "hello", user_email="user@example.com")

    assert error is None
    scheduler._touch_conversation.assert_awaited_once_with("conv-1")
    scheduler._launch_turn.assert_called_once()


@pytest.mark.asyncio
async def test_submit_turn_touches_conversation_when_queued_behind_escalation() -> None:
    pause_waiter = PauseWaiter()
    pause_waiter.register(
        PendingPause(
            pause_id="pause-1",
            conversation_id="conv-1",
            session_id="sess-1",
            pause_type="escalation",
        )
    )
    scheduler = TurnScheduler(
        session_factory=lambda: _NoopAsyncContext(),
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
    scheduler._load_conversation_runtime = AsyncMock(  # type: ignore[method-assign]
        return_value=(
            SimpleNamespace(
                conversation_id="conv-1", user_email="user@example.com", status="active"
            ),
            SimpleNamespace(session_id="sess-1", status=SessionStatus.ACTIVE),
            SimpleNamespace(agent_id="agent-1", owner_email="user@example.com"),
            False,
        )
    )
    scheduler._build_attachment_support_messages = AsyncMock(return_value=(None, None))  # type: ignore[method-assign]
    scheduler._touch_conversation = AsyncMock()  # type: ignore[method-assign]
    scheduler._notify_observers_system_message = AsyncMock()  # type: ignore[method-assign]

    error = await scheduler.submit_turn("conv-1", "hello", user_email="user@example.com")

    assert error is None
    scheduler._touch_conversation.assert_awaited_once_with("conv-1")
    assert len(scheduler._queued_messages["conv-1"]) == 1


@pytest.mark.asyncio
async def test_queued_messages_include_stable_metadata() -> None:
    scheduler = TurnScheduler(
        session_factory=SimpleNamespace(),
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
    scheduler._active_turns["conv-1"] = asyncio.create_task(asyncio.sleep(1))
    scheduler._queued_messages["conv-1"].extend(
        [
            _QueuedMessage(
                queue_id="qmsg_one",
                content="metadata one",
                user_email="user@example.com",
                client_message_id="client-one",
            ),
            _QueuedMessage(
                queue_id="qmsg_two",
                content="metadata two",
                user_email="user@example.com",
                client_message_id="client-two",
            ),
        ]
    )

    queued = scheduler.queued_messages("conv-1")
    assert [item["queue_id"] for item in queued] == ["qmsg_one", "qmsg_two"]
    assert [item["client_message_id"] for item in queued] == ["client-one", "client-two"]
    assert [item["position"] for item in queued] == [1, 2]

    scheduler._active_turns["conv-1"].cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await scheduler._active_turns["conv-1"]


@pytest.mark.asyncio
async def test_duplicate_client_message_id_is_not_enqueued_twice() -> None:
    scheduler = TurnScheduler(
        session_factory=SimpleNamespace(),
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
    scheduler._queued_messages["conv-1"].append(
        _QueuedMessage(
            content="already queued",
            user_email="user@example.com",
            client_message_id="client-dup",
        )
    )

    conversation = SimpleNamespace(
        conversation_id="conv-1",
        user_email="user@example.com",
        status="active",
    )
    session = SimpleNamespace(status=SessionStatus.ACTIVE)
    agent = SimpleNamespace()
    scheduler._load_conversation_runtime = AsyncMock(
        return_value=(conversation, session, agent, False)
    )  # type: ignore[method-assign]
    scheduler._build_attachment_support_messages = AsyncMock(return_value=(None, None))  # type: ignore[method-assign]
    scheduler._active_turns["conv-1"] = asyncio.create_task(asyncio.sleep(1))

    error = await scheduler.submit_turn(
        "conv-1",
        "duplicate content",
        user_email="user@example.com",
        client_message_id="client-dup",
    )

    assert error is None
    queued = scheduler.queued_messages("conv-1")
    assert len(queued) == 1
    assert queued[0]["content"] == "already queued"

    scheduler._active_turns["conv-1"].cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await scheduler._active_turns["conv-1"]


@pytest.mark.asyncio
async def test_update_queued_message_edits_text_before_processing() -> None:
    scheduler = TurnScheduler(
        session_factory=SimpleNamespace(),
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
    scheduler._queued_messages["conv-1"].append(
        _QueuedMessage(
            content="original text",
            user_email="user@example.com",
            client_message_id="client-edit-1",
        )
    )
    queue_id = str(scheduler.queued_messages("conv-1")[0]["queue_id"])

    updated = await scheduler.update_queued_message("conv-1", queue_id, content="edited text")

    assert updated is not None
    assert updated["content"] == "edited text"
    queued = scheduler.queued_messages("conv-1")
    assert queued[0]["content"] == "edited text"
    assert queued[0]["client_message_id"] == "client-edit-1"


@pytest.mark.asyncio
async def test_queued_message_cancel_removes_item_before_follow_up_cleanup() -> None:
    scheduler = TurnScheduler(
        session_factory=SimpleNamespace(),
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
    cleanup_started = asyncio.Event()
    release_cleanup = asyncio.Event()

    async def _cleanup(_: str, __: str) -> None:
        cleanup_started.set()
        await release_cleanup.wait()

    scheduler._clear_follow_up_pending = _cleanup  # type: ignore[method-assign]
    scheduler._queued_messages["conv-1"].append(
        _QueuedMessage(
            queue_id="qmsg_cancel",
            content="cancel me",
            user_email="user@example.com",
            follow_up=SimpleNamespace(follow_up_id="fup_1"),
        )
    )

    cancel_task = asyncio.create_task(scheduler.cancel_queued_message("conv-1", "qmsg_cancel"))
    await cleanup_started.wait()
    assert scheduler.queued_messages("conv-1") == []
    release_cleanup.set()
    assert await cancel_task is True


@pytest.mark.asyncio
async def test_drained_queued_message_preserves_prepared_attachment_context() -> None:
    scheduler = TurnScheduler(
        session_factory=SimpleNamespace(),
        workflow_engine=SimpleNamespace(run_direct_turn=AsyncMock()),
        decision_engine=SimpleNamespace(decide=AsyncMock(return_value=None)),
        task_queue=SimpleNamespace(),
        session_manager=SimpleNamespace(),
        session_cache=SimpleNamespace(
            refresh=AsyncMock(return_value=SimpleNamespace(last_event_seq=1)),
            get_context_usage=lambda _session_id: None,
            get_entry=lambda _session_id: None,
        ),
        compaction_strategy=SimpleNamespace(),
        agent_loop=SimpleNamespace(),
        pause_waiter=PauseWaiter(),
        notification_service=SimpleNamespace(),
        providers=SimpleNamespace(),
        artifact_store=SimpleNamespace(),
        workflow_registry=SimpleNamespace(),
        event_bus=EventBus(),
    )
    attachment = AttachmentRef(
        artifact_id="art_queued",
        kind=ArtifactKind.PDF,
        filename="queued.pdf",
        mime_type="application/pdf",
        size_bytes=1024,
    )
    queued = _QueuedMessage(
        queue_id="qmsg_attachment",
        content="use queued attachment",
        user_email="user@example.com",
        attachments=[attachment.model_dump(mode="json")],
        attachment_notice="prepared attachment notice",
        attachment_context="prepared attachment context",
    )
    scheduler._queued_messages["conv-1"].append(queued)
    conversation = SimpleNamespace(
        conversation_id="conv-1",
        user_email="user@example.com",
        status="active",
        title="Conversation",
    )
    session = SimpleNamespace(session_id="sess-1", status=SessionStatus.ACTIVE)
    agent = SimpleNamespace(agent_id="agent-1", owner_email="owner@example.com")
    scheduler._load_conversation_runtime = AsyncMock(
        return_value=(conversation, session, agent, False)
    )  # type: ignore[method-assign]
    scheduler._build_attachment_support_messages = AsyncMock(
        return_value=("rebuilt notice", "rebuilt context")
    )  # type: ignore[method-assign]
    scheduler._resolve_attachments_for_turn = AsyncMock(return_value=([attachment], None))  # type: ignore[method-assign]
    scheduler._workflow_engine.run_direct_turn.return_value = SimpleNamespace(
        content="done",
        attachments=[],
    )
    scheduler._touch_conversation = AsyncMock()  # type: ignore[method-assign]
    scheduler._publish_turn_completed = AsyncMock()  # type: ignore[method-assign]
    scheduler._publish_turn_error = AsyncMock()  # type: ignore[method-assign]
    scheduler._event_bus.publish = AsyncMock()  # type: ignore[method-assign]
    scheduler._launch_turn = MagicMock()  # type: ignore[method-assign]

    await scheduler._run_turn(
        conversation=conversation,
        session=session,
        agent=agent,
        content="active turn",
        user_email="user@example.com",
        attachments=[],
        outbound_attachments=None,
        attachment_notice=None,
        attachment_context=None,
        system_initiated=False,
        follow_up=None,
        channel_deliverable=False,
        delivery_id=None,
        delivery_fallback_text=None,
        bootstrap_wait_for_intention=False,
        cancel_event=asyncio.Event(),
        turn_control=_TurnControl(),
    )
    launch_call = scheduler._launch_turn.call_args
    assert launch_call is not None
    assert launch_call.kwargs["content"] == "use queued attachment"
    assert launch_call.kwargs["attachment_notice"] == "prepared attachment notice"
    assert launch_call.kwargs["attachment_context"] == "prepared attachment context"
    assert launch_call.kwargs["attachments"] == [attachment]
    scheduler._build_attachment_support_messages.assert_not_awaited()


@pytest.mark.asyncio
async def test_submit_turn_reactivates_idle_session_before_launch() -> None:
    session_manager = SimpleNamespace(mark_active=AsyncMock(return_value=True))
    scheduler = TurnScheduler(
        session_factory=SimpleNamespace(),
        workflow_engine=SimpleNamespace(),
        decision_engine=SimpleNamespace(),
        task_queue=SimpleNamespace(),
        session_manager=session_manager,
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

    idle_session = SimpleNamespace(
        session_id="sess-1",
        status=SessionStatus.IDLE,
        idle_since="2026-04-12T10:00:00Z",
    )

    async def _runtime(_: str, **__: object) -> tuple[object, object, object, bool]:
        return (
            SimpleNamespace(
                conversation_id="conv-1", user_email="user@example.com", status="active"
            ),
            idle_session,
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
    session_manager.mark_active.assert_awaited_once_with("sess-1")
    assert idle_session.status == SessionStatus.ACTIVE
    assert idle_session.idle_since is None


@pytest.mark.asyncio
async def test_queued_turn_observer_only_receives_its_own_turn() -> None:
    first_started = asyncio.Event()
    release_first = asyncio.Event()
    turn_order: list[str] = []

    async def _run_direct_turn(**kwargs: object) -> object:
        user_message = kwargs["user_message"]
        on_progress = kwargs["on_progress"]
        assert callable(on_progress)
        turn_order.append(str(user_message))
        await on_progress(f"token:{user_message}")
        if user_message == "first":
            first_started.set()
            await release_first.wait()
        return SimpleNamespace(content=f"reply:{user_message}", attachments=[])

    scheduler = TurnScheduler(
        session_factory=SimpleNamespace(),
        workflow_engine=SimpleNamespace(run_direct_turn=_run_direct_turn),
        decision_engine=SimpleNamespace(
            decide=AsyncMock(return_value=SimpleNamespace(decision="inline"))
        ),
        task_queue=SimpleNamespace(),
        session_manager=SimpleNamespace(),
        session_cache=SimpleNamespace(
            refresh=AsyncMock(return_value=SimpleNamespace(last_event_seq=1)),
            get_context_usage=MagicMock(return_value=None),
            get_entry=MagicMock(return_value=None),
        ),
        compaction_strategy=SimpleNamespace(),
        agent_loop=SimpleNamespace(cancel_children=AsyncMock(return_value=0)),
        pause_waiter=PauseWaiter(),
        notification_service=SimpleNamespace(),
        providers=SimpleNamespace(),
        artifact_store=SimpleNamespace(),
        workflow_registry=SimpleNamespace(),
        event_bus=EventBus(),
    )
    scheduler._publish_turn_completed = TurnScheduler._publish_turn_completed.__get__(
        scheduler, TurnScheduler
    )
    scheduler._touch_conversation = AsyncMock()  # type: ignore[method-assign]

    async def _runtime(_: str, **__: object) -> tuple[object, object, object, bool]:
        return (
            SimpleNamespace(
                conversation_id="conv-1", user_email="user@example.com", title="", status="active"
            ),
            SimpleNamespace(session_id="sess-1", status=SessionStatus.ACTIVE),
            SimpleNamespace(agent_id="agent-1", owner_email="user@example.com"),
            False,
        )

    async def _attachments(**_: object) -> tuple[list[object], object]:
        return [], None

    async def _notice(**_: object) -> None:
        return None

    scheduler._load_conversation_runtime = _runtime  # type: ignore[method-assign]
    scheduler._resolve_attachments_for_turn = _attachments  # type: ignore[method-assign]
    scheduler._build_attachment_notice = _notice  # type: ignore[method-assign]

    first_observer = _RecordingObserver()
    second_observer = _RecordingObserver()

    first_error = await asyncio.wait_for(
        scheduler.submit_turn(
            "conv-1",
            "first",
            user_email="user@example.com",
            turn_observers=[first_observer],
        ),
        timeout=1,
    )
    assert first_error is None
    await asyncio.wait_for(first_started.wait(), timeout=1)

    second_error = await scheduler.submit_turn(
        "conv-1",
        "second",
        user_email="user@example.com",
        turn_observers=[second_observer],
    )
    assert second_error is None
    assert second_observer.tokens == []
    assert second_observer.completed == []
    assert second_observer.queued == [1]

    release_first.set()
    while scheduler.has_active_turn("conv-1") or scheduler.queued_count("conv-1"):
        await asyncio.sleep(0.01)

    assert turn_order == ["first", "second"]
    assert first_observer.tokens == ["token:first"]
    assert second_observer.tokens == ["token:second"]
    assert len(first_observer.completed) == 1
    assert len(second_observer.completed) == 1


@pytest.mark.asyncio
async def test_consume_queued_batch_for_active_turn_publishes_user_messages() -> None:
    event_bus = EventBus()
    seen_user_messages: list[dict[str, object]] = []

    async def _capture_user_message(event: Event) -> None:
        seen_user_messages.append(dict(event.data))

    event_bus.subscribe(EventType.USER_MESSAGE, _capture_user_message)

    scheduler = TurnScheduler(
        session_factory=SimpleNamespace(),
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
        event_bus=event_bus,
    )
    observer = _RecordingObserver()
    control = _TurnControl(turn_observers=[observer])
    scheduler._turn_controls["conv-1"] = control
    scheduler._turn_sessions["conv-1"] = "sess-1"
    scheduler._queued_messages["conv-1"].extend(
        [
            _QueuedMessage(
                content="first queued",
                user_email="user@example.com",
                client_message_id="cmsg-first",
                attachment_notice="Attachment warning",
                outbound_attachments=[{"artifact_id": "art-1", "filename": "report.txt"}],
                channel_deliverable=True,
                delivery_id="reply-latest",
                delivery_fallback_text="fallback",
            ),
            _QueuedMessage(content="second queued", user_email="user@example.com"),
        ]
    )

    batch = await scheduler._consume_queued_batch_for_active_turn(
        "conv-1",
        reason="after_tool_cycle",
    )

    assert [item["content"] for item in batch] == ["first queued", "second queued"]
    assert [item["content"] for item in seen_user_messages] == ["first queued", "second queued"]
    assert seen_user_messages[0]["event_id"] == "client:cmsg-first"
    assert seen_user_messages[0]["message_id"] == "client:cmsg-first"
    assert str(seen_user_messages[1]["event_id"]).startswith("queue:qmsg_")
    assert seen_user_messages[1]["message_id"] == seen_user_messages[1]["event_id"]
    assert scheduler.queued_count("conv-1") == 0
    assert observer.queued == [0]
    assert observer.system_messages == ["Attachment warning"]
    assert control.absorbed_outbound_attachments == [
        {"artifact_id": "art-1", "filename": "report.txt"}
    ]
    assert control.absorbed_channel_deliverable is True
    assert control.absorbed_delivery_id == "reply-latest"
    assert control.absorbed_delivery_fallback_text == "fallback"


def test_merge_active_turn_observers_skips_non_absorbable_observers() -> None:
    scheduler = TurnScheduler(
        session_factory=SimpleNamespace(),
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
    active_observer = _RecordingObserver()
    active_observers = [active_observer]
    queued_observer = _RecordingObserver()

    scheduler._merge_active_turn_observers(
        active_observers,
        [
            _QueuedMessage(
                content="queued", user_email="user@example.com", turn_observers=(queued_observer,)
            )
        ],
    )

    assert active_observers == [active_observer]


@pytest.mark.asyncio
async def test_consume_queued_batch_preserves_non_absorbable_tail() -> None:
    scheduler = TurnScheduler(
        session_factory=SimpleNamespace(),
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
    scheduler._turn_controls["conv-1"] = _TurnControl()
    scheduler._turn_sessions["conv-1"] = "sess-1"
    blocking_observer = _RecordingObserver()
    scheduler._queued_messages["conv-1"].extend(
        [
            _QueuedMessage(content="first queued", user_email="user@example.com"),
            _QueuedMessage(
                content="second queued",
                user_email="user@example.com",
                turn_observers=(blocking_observer,),
            ),
        ]
    )

    batch = await scheduler._consume_queued_batch_for_active_turn(
        "conv-1",
        reason="after_tool_cycle",
    )

    assert [item["content"] for item in batch] == ["first queued"]
    assert scheduler.queued_count("conv-1") == 1
    assert scheduler._queued_messages["conv-1"][0].content == "second queued"


@pytest.mark.asyncio
async def test_follow_up_event_threads_channel_delivery_metadata() -> None:
    session_factory = SimpleNamespace()
    follow_up = TaskResultFollowUp(
        follow_up_id="fup_1",
        mode=FollowUpMode.NOTIFY,
        origin_kind=FollowUpOriginKind.TASK_RESULT,
        relevance_hint="unknown",
        required_action=FollowUpRequiredAction.PRESENT_UPDATE,
        topic_ref="task-1",
        status=FollowUpStatus.COMPLETED,
        task_id="task-1",
        task_title="Background task",
        source_type="api",
        delivery_mode="latest_active_for_agent",
        result_summary="Done",
        description="",
    )

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
                    "follow_up": follow_up.model_dump(mode="json"),
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
    assert scheduler.submit_turn.await_args.kwargs["follow_up"] == follow_up
    assert scheduler.submit_turn.await_args.kwargs["channel_deliverable"] is True
    assert scheduler.submit_turn.await_args.kwargs["delivery_id"] == "cdel_1"
    assert scheduler.submit_turn.await_args.kwargs["delivery_fallback_text"] == "fallback"


@pytest.mark.asyncio
async def test_follow_up_event_publishes_turn_error_on_immediate_rejection() -> None:
    follow_up = TaskResultFollowUp(
        follow_up_id="fup_1",
        mode=FollowUpMode.NOTIFY,
        origin_kind=FollowUpOriginKind.TASK_RESULT,
        relevance_hint="unknown",
        required_action=FollowUpRequiredAction.PRESENT_UPDATE,
        topic_ref="task-1",
        status=FollowUpStatus.COMPLETED,
        task_id="task-1",
        task_title="Background task",
        source_type="api",
        delivery_mode="latest_active_for_agent",
        result_summary="Done",
        description="",
    )

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
                    "follow_up": follow_up.model_dump(mode="json"),
                    "delivery_id": "cdel_1",
                    "channel_deliverable": True,
                    "delivery_fallback_text": "fallback",
                }
            )
        )
    finally:
        queries.get_conversation = original  # type: ignore[assignment]

    scheduler._publish_turn_error.assert_awaited_once()


@pytest.mark.asyncio
async def test_follow_up_event_suppresses_duplicate_follow_up_id() -> None:
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
    scheduler.submit_turn = AsyncMock(return_value=None)  # type: ignore[method-assign]

    async def _get_conversation(_session, conversation_id: str):
        return SimpleNamespace(conversation_id=conversation_id, user_email="user@example.com")

    import cognis.store.queries as queries

    original = queries.get_conversation
    queries.get_conversation = _get_conversation  # type: ignore[assignment]
    follow_up = TaskResultFollowUp(
        follow_up_id="fup_1",
        mode=FollowUpMode.NOTIFY,
        origin_kind=FollowUpOriginKind.TASK_RESULT,
        relevance_hint="unknown",
        required_action=FollowUpRequiredAction.PRESENT_UPDATE,
        topic_ref="task-1",
        status=FollowUpStatus.COMPLETED,
        task_id="task-1",
        task_title="Background task",
        source_type="api",
        delivery_mode="latest_active_for_agent",
        result_summary="Done",
        description="",
    )
    try:
        event = SimpleNamespace(
            data={"conversation_id": "conv-1", "follow_up": follow_up.model_dump(mode="json")}
        )
        await scheduler._handle_follow_up_event(event)
        await scheduler._handle_follow_up_event(event)
    finally:
        queries.get_conversation = original  # type: ignore[assignment]

    scheduler.submit_turn.assert_awaited_once()


@pytest.mark.asyncio
async def test_follow_up_event_retries_after_immediate_rejection() -> None:
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
        return_value=SimpleNamespace(code="queue_full", message="full", recoverable=True)
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
    follow_up = TaskResultFollowUp(
        follow_up_id="fup_1",
        mode=FollowUpMode.NOTIFY,
        origin_kind=FollowUpOriginKind.TASK_RESULT,
        relevance_hint="unknown",
        required_action=FollowUpRequiredAction.PRESENT_UPDATE,
        topic_ref="task-1",
        status=FollowUpStatus.COMPLETED,
        task_id="task-1",
        task_title="Background task",
        source_type="api",
        delivery_mode="latest_active_for_agent",
        result_summary="Done",
        description="",
    )
    try:
        event = SimpleNamespace(
            data={"conversation_id": "conv-1", "follow_up": follow_up.model_dump(mode="json")}
        )
        await scheduler._handle_follow_up_event(event)
        await scheduler._handle_follow_up_event(event)
    finally:
        queries.get_conversation = original  # type: ignore[assignment]

    assert scheduler.submit_turn.await_count == 2


@pytest.mark.asyncio
async def test_follow_up_dedupe_persists_across_scheduler_instances(tmp_path: Path) -> None:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'follow-up-dedupe.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)

    scheduler_a = TurnScheduler(
        session_factory=session_factory,
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
    scheduler_b = TurnScheduler(
        session_factory=session_factory,
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
    scheduler_a.submit_turn = AsyncMock(return_value=None)  # type: ignore[method-assign]
    scheduler_b.submit_turn = AsyncMock(return_value=None)  # type: ignore[method-assign]

    async def _get_conversation(_session, conversation_id: str):
        return SimpleNamespace(conversation_id=conversation_id, user_email="user@example.com")

    import cognis.store.queries as queries

    original = queries.get_conversation
    queries.get_conversation = _get_conversation  # type: ignore[assignment]
    follow_up = TaskResultFollowUp(
        follow_up_id="fup_cross_instance",
        mode=FollowUpMode.NOTIFY,
        origin_kind=FollowUpOriginKind.TASK_RESULT,
        relevance_hint="unknown",
        required_action=FollowUpRequiredAction.PRESENT_UPDATE,
        topic_ref="task-1",
        status=FollowUpStatus.COMPLETED,
        task_id="task-1",
        task_title="Background task",
        source_type="api",
        delivery_mode="latest_active_for_agent",
        result_summary="Done",
        description="",
    )
    try:
        event = SimpleNamespace(
            data={
                "conversation_id": "conv-1",
                "follow_up": follow_up.model_dump(mode="json"),
            }
        )
        await scheduler_a._handle_follow_up_event(event)
        await scheduler_b._handle_follow_up_event(event)
    finally:
        queries.get_conversation = original  # type: ignore[assignment]

    scheduler_a.submit_turn.assert_awaited_once()
    scheduler_b.submit_turn.assert_not_awaited()
    await engine.dispose()


@pytest.mark.asyncio
async def test_cancel_turn_clears_pending_queued_follow_up() -> None:
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
    follow_up = TaskResultFollowUp(
        follow_up_id="fup_1",
        mode=FollowUpMode.NOTIFY,
        origin_kind=FollowUpOriginKind.TASK_RESULT,
        relevance_hint="unknown",
        required_action=FollowUpRequiredAction.PRESENT_UPDATE,
        topic_ref="task-1",
        status=FollowUpStatus.COMPLETED,
        task_id="task-1",
        task_title="Background task",
        source_type="api",
        delivery_mode="latest_active_for_agent",
        result_summary="Done",
        description="",
    )
    scheduler._pending_follow_ups.add(("conv-1", "fup_1"))
    scheduler._queued_messages["conv-1"].append(SimpleNamespace(follow_up=follow_up))

    cleared = await scheduler.cancel_turn("conv-1")

    assert cleared is True
    assert ("conv-1", "fup_1") not in scheduler._pending_follow_ups


@pytest.mark.asyncio
async def test_cancel_turn_can_preserve_queued_messages() -> None:
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
    follow_up = TaskResultFollowUp(
        follow_up_id="fup_1",
        mode=FollowUpMode.NOTIFY,
        origin_kind=FollowUpOriginKind.TASK_RESULT,
        relevance_hint=FollowUpRelevanceHint.UNKNOWN,
        required_action=FollowUpRequiredAction.PRESENT_UPDATE,
        topic_ref="task-1",
        status=FollowUpStatus.COMPLETED,
        task_id="task-1",
        task_title="Background task",
        source_type="api",
        delivery_mode="latest_active_for_agent",
        result_summary="Done",
        description="",
    )
    scheduler._pending_follow_ups.add(("conv-1", "fup_1"))
    scheduler._queued_messages["conv-1"].append(SimpleNamespace(follow_up=follow_up))

    cancelled = await scheduler.cancel_turn("conv-1", clear_queue=False)

    assert cancelled is False
    assert len(scheduler._queued_messages["conv-1"]) == 1
    assert ("conv-1", "fup_1") in scheduler._pending_follow_ups


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
    assert (
        _effective_user_content(
            "",
            [
                AttachmentRef(
                    artifact_id="att-2",
                    kind=ArtifactKind.IMAGE,
                    mime_type="image/png",
                    filename="photo.png",
                    size_bytes=10,
                )
            ],
        )
        == "User attached an image file."
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
        session_manager=SimpleNamespace(refresh_intaris_session_policy=AsyncMock()),
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
        agent=SimpleNamespace(agent_id="agent-1", owner_email="user@example.com", execution={}),
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
        attachment_context=None,
        system_initiated=False,
        channel_deliverable=False,
        delivery_id=None,
        delivery_fallback_text=None,
        bootstrap_wait_for_intention=False,
        cancel_event=AsyncMock(),
        turn_observers=(),
    )

    user_events = [
        event for event in observed if getattr(event, "type", None) == EventType.USER_MESSAGE
    ]
    assert len(user_events) == 1
    # The server now broadcasts the raw (empty) content for attachment-only messages
    # so the UI optimistic-bubble deduplication can match it directly.
    assert user_events[0].data["content"] == ""
    assert user_events[0].data["event_id"] == user_events[0].data["message_id"]
    assert str(user_events[0].data["event_id"]).startswith("user:sess-1:turn_")


@pytest.mark.asyncio
async def test_run_turn_delegation_inherits_conversation_execution_paths() -> None:
    submit = AsyncMock(return_value=SimpleNamespace(task_id="task-1"))
    scheduler = TurnScheduler(
        session_factory=SimpleNamespace(),
        workflow_engine=SimpleNamespace(run_direct_turn=AsyncMock()),
        decision_engine=SimpleNamespace(
            decide=AsyncMock(return_value=SimpleNamespace(decision="delegate"))
        ),
        task_queue=SimpleNamespace(submit=submit),
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
        event_bus=EventBus(),
    )
    scheduler._publish_turn_completed = AsyncMock()  # type: ignore[method-assign]
    scheduler._touch_conversation = AsyncMock()  # type: ignore[method-assign]
    scheduler._select_workflow = AsyncMock(return_value="wf-1")  # type: ignore[method-assign]

    await scheduler._run_turn(
        conversation=SimpleNamespace(
            conversation_id="conv-1",
            title="",
            user_email="user@example.com",
            project_id=None,
            context=SimpleNamespace(
                platform_data={
                    "workspace_root": "/workspace/cognis",
                    "working_directory": "/workspace/cognis/ui",
                }
            ),
        ),
        session=SimpleNamespace(session_id="sess-1"),
        agent=SimpleNamespace(agent_id="agent-1", owner_email="user@example.com"),
        content="please handle this in the background",
        user_email="user@example.com",
        attachments=None,
        outbound_attachments=None,
        attachment_notice=None,
        attachment_context=None,
        system_initiated=False,
        channel_deliverable=False,
        delivery_id=None,
        delivery_fallback_text=None,
        bootstrap_wait_for_intention=False,
        cancel_event=AsyncMock(),
        turn_observers=(),
    )

    submit.assert_awaited_once()
    assert submit.await_args.kwargs["workspace_root"] == "/workspace/cognis"
    assert submit.await_args.kwargs["working_directory"] == "/workspace/cognis/ui"


@pytest.mark.asyncio
async def test_run_turn_merges_absorbed_delivery_metadata() -> None:
    async def _run_direct_turn(**kwargs: object) -> object:
        consume_boundary_batch = kwargs["consume_boundary_batch"]
        assert callable(consume_boundary_batch)
        await consume_boundary_batch("after_tool_cycle")
        return SimpleNamespace(content="reply", attachments=[])

    scheduler = TurnScheduler(
        session_factory=SimpleNamespace(),
        workflow_engine=SimpleNamespace(run_direct_turn=_run_direct_turn),
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
        event_bus=EventBus(),
    )
    scheduler._touch_conversation = AsyncMock()  # type: ignore[method-assign]
    published: list[object] = []

    async def _capture_publish(result: object, **_: object) -> None:
        published.append(result)

    scheduler._publish_turn_completed = _capture_publish  # type: ignore[method-assign]
    control = _TurnControl()
    scheduler._turn_controls["conv-1"] = control
    scheduler._turn_sessions["conv-1"] = "sess-1"
    scheduler._queued_messages["conv-1"].append(
        _QueuedMessage(
            content="queued follow-up",
            user_email="user@example.com",
            system_initiated=True,
            outbound_attachments=[{"artifact_id": "art-2", "filename": "image.png"}],
            channel_deliverable=True,
            delivery_id="reply-2",
            delivery_fallback_text="fallback text",
        )
    )

    await scheduler._run_turn(
        conversation=SimpleNamespace(
            conversation_id="conv-1", title="", user_email="user@example.com"
        ),
        session=SimpleNamespace(session_id="sess-1"),
        agent=SimpleNamespace(agent_id="agent-1", owner_email="user@example.com"),
        content="hello",
        user_email="user@example.com",
        attachments=[],
        outbound_attachments=None,
        attachment_notice=None,
        attachment_context=None,
        system_initiated=False,
        channel_deliverable=False,
        delivery_id=None,
        delivery_fallback_text=None,
        bootstrap_wait_for_intention=False,
        cancel_event=AsyncMock(),
        turn_control=control,
        turn_observers=(),
    )

    assert len(published) == 1
    result = published[0]
    assert result.channel_deliverable is True
    assert result.delivery_id == "reply-2"
    assert result.delivery_fallback_text == "fallback text"
    assert result.attachments == [{"artifact_id": "art-2", "filename": "image.png"}]


@pytest.mark.asyncio
async def test_run_turn_error_merges_absorbed_delivery_metadata() -> None:
    async def _run_direct_turn(**kwargs: object) -> object:
        consume_boundary_batch = kwargs["consume_boundary_batch"]
        assert callable(consume_boundary_batch)
        await consume_boundary_batch("after_tool_cycle")
        raise RuntimeError("boom")

    scheduler = TurnScheduler(
        session_factory=SimpleNamespace(),
        workflow_engine=SimpleNamespace(run_direct_turn=_run_direct_turn),
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
        event_bus=EventBus(),
    )
    scheduler._touch_conversation = AsyncMock()  # type: ignore[method-assign]
    captured_errors: list[dict[str, object]] = []

    async def _capture_error(
        conversation_id: str,
        session_id: str,
        error: object,
        **kwargs: object,
    ) -> None:
        captured_errors.append(
            {
                "conversation_id": conversation_id,
                "session_id": session_id,
                "error": error,
                **kwargs,
            }
        )

    scheduler._publish_turn_error = _capture_error  # type: ignore[method-assign]
    control = _TurnControl()
    scheduler._turn_controls["conv-1"] = control
    scheduler._turn_sessions["conv-1"] = "sess-1"
    scheduler._queued_messages["conv-1"].append(
        _QueuedMessage(
            content="queued follow-up",
            user_email="user@example.com",
            system_initiated=True,
            outbound_attachments=[{"artifact_id": "art-3", "filename": "report.pdf"}],
            channel_deliverable=True,
            delivery_id="reply-3",
            delivery_fallback_text="fallback error",
        )
    )

    await scheduler._run_turn(
        conversation=SimpleNamespace(
            conversation_id="conv-1", title="", user_email="user@example.com"
        ),
        session=SimpleNamespace(session_id="sess-1"),
        agent=SimpleNamespace(agent_id="agent-1", owner_email="user@example.com"),
        content="hello",
        user_email="user@example.com",
        attachments=[],
        outbound_attachments=None,
        attachment_notice=None,
        attachment_context=None,
        system_initiated=False,
        channel_deliverable=False,
        delivery_id=None,
        delivery_fallback_text=None,
        bootstrap_wait_for_intention=False,
        cancel_event=AsyncMock(),
        turn_control=control,
        turn_observers=(),
    )

    assert len(captured_errors) == 1
    assert captured_errors[0]["channel_deliverable"] is True
    assert captured_errors[0]["delivery_id"] == "reply-3"
    assert captured_errors[0]["delivery_fallback_text"] == "fallback error"


@pytest.mark.asyncio
async def test_run_turn_clears_pending_follow_up_when_queued_relaunch_fails() -> None:
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
        event_bus=EventBus(),
    )
    scheduler._publish_turn_completed = AsyncMock()  # type: ignore[method-assign]
    scheduler._touch_conversation = AsyncMock()  # type: ignore[method-assign]
    scheduler.submit_turn = AsyncMock(
        return_value=SimpleNamespace(code="queue_full", message="full", recoverable=True)
    )  # type: ignore[method-assign]
    follow_up = TaskResultFollowUp(
        follow_up_id="fup_queued",
        mode=FollowUpMode.NOTIFY,
        origin_kind=FollowUpOriginKind.TASK_RESULT,
        relevance_hint="unknown",
        required_action=FollowUpRequiredAction.PRESENT_UPDATE,
        topic_ref="task-1",
        status=FollowUpStatus.COMPLETED,
        task_id="task-1",
        task_title="Background task",
        source_type="api",
        delivery_mode="latest_active_for_agent",
        result_summary="Done",
        description="",
    )
    scheduler._pending_follow_ups.add(("conv-1", "fup_queued"))
    scheduler._queued_messages["conv-1"].append(
        SimpleNamespace(
            content="",
            user_email="user@example.com",
            attachments=None,
            outbound_attachments=None,
            system_initiated=True,
            follow_up=follow_up,
            channel_deliverable=False,
            delivery_id=None,
            delivery_fallback_text=None,
            turn_observers=(),
        )
    )

    await scheduler._run_turn(
        conversation=SimpleNamespace(
            conversation_id="conv-1", title="", user_email="user@example.com"
        ),
        session=SimpleNamespace(session_id="sess-1"),
        agent=SimpleNamespace(agent_id="agent-1"),
        content="hello",
        user_email="user@example.com",
        attachments=[],
        outbound_attachments=None,
        attachment_notice=None,
        attachment_context=None,
        system_initiated=False,
        follow_up=None,
        channel_deliverable=False,
        delivery_id=None,
        delivery_fallback_text=None,
        bootstrap_wait_for_intention=False,
        cancel_event=AsyncMock(),
        turn_observers=(),
    )

    assert ("conv-1", "fup_queued") not in scheduler._pending_follow_ups


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


@pytest.mark.asyncio
async def test_load_runtime_bootstrap_ignores_persisted_conversation_title() -> None:
    class _Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

    captured: dict[str, str] = {}

    async def _ensure_root_session(**kwargs: object):
        captured["intention"] = str(kwargs["intention"])
        return SimpleNamespace(session_id="sess-1", intaris_session_id="sess-1")

    scheduler = TurnScheduler(
        session_factory=lambda: _Session(),
        workflow_engine=SimpleNamespace(),
        decision_engine=SimpleNamespace(),
        task_queue=SimpleNamespace(),
        session_manager=SimpleNamespace(ensure_root_session=_ensure_root_session),
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

    import cognis.store.queries as queries

    original_get_agent = queries.get_agent
    original_get_conversation = queries.get_conversation
    original_get_session_row = queries.get_session_row

    async def _get_conversation(_session, conversation_id: str):
        return SimpleNamespace(
            conversation_id=conversation_id,
            user_email="user@example.com",
            agent_id="agent-1",
            title="Pinned title",
            title_source="manual",
            context_type="web",
            context_ref=None,
            context_data={},
            memory_labels={},
            active_session_id=None,
            status="active",
            last_message_at=None,
            created_at=None,
            updated_at=None,
        )

    async def _get_agent(_session, agent_id: str):
        return AgentDefinition(agent_id=agent_id, owner_email="user@example.com", name="Agent")

    async def _get_session_row(_session, session_id: str):
        del _session, session_id
        return None

    queries.get_conversation = _get_conversation  # type: ignore[assignment]
    queries.get_agent = _get_agent  # type: ignore[assignment]
    queries.get_session_row = _get_session_row  # type: ignore[assignment]
    try:
        result = await scheduler._load_conversation_runtime("conv-1", user_message="")
    finally:
        queries.get_conversation = original_get_conversation  # type: ignore[assignment]
        queries.get_agent = original_get_agent  # type: ignore[assignment]
        queries.get_session_row = original_get_session_row  # type: ignore[assignment]

    assert result is not None
    assert captured["intention"] == "Conversation with Agent"
