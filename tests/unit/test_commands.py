from __future__ import annotations

from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock

import pytest

from cognis.core.agent_direct import AGENT_DIRECT_KIND, agent_direct_context_ref
from cognis.core.agent_loop import PauseWaiter, PendingPause
from cognis.core.commands import CommandDispatcher, is_system_slash_command_message
from cognis.models.agent import AgentDefinition, AgentLLMConfig
from cognis.models.session import (
    ConversationContext,
    ConversationModel,
    IntarisSession,
    SessionModel,
)
from cognis.tools.executor.lsp.runtime import LSPStatusConfig, LSPStatusReport, LSPStatusTotals


class _HistoryRebaseResult(SimpleNamespace):
    pass


class _NotificationService:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict[str, object]]] = []
        self.orphaned: list[tuple[str, str]] = []
        self.resolve_result = True
        self.pending_notifications: list[object] = []

    async def resolve(
        self,
        notification_id: str,
        decision: str,
        data: dict[str, object],
        *,
        user_email: str | None = None,
    ) -> bool:
        self.calls.append((notification_id, decision, data))
        return self.resolve_result

    async def mark_orphaned(self, notification_id: str, *, reason: str) -> bool:
        self.orphaned.append((notification_id, reason))
        return True

    async def list_pending(
        self, user_email: str, *, conversation_id: str | None = None
    ) -> list[object]:
        del user_email, conversation_id
        return list(self.pending_notifications)


class _TurnScheduler:
    def __init__(self, *, cancelled: bool) -> None:
        self.cancelled = cancelled
        self.calls: list[str] = []
        self.submitted: list[tuple[str, str, str]] = []
        self.submit_error: object | None = None
        self.checkpoints: dict[str, dict[str, str | None]] = {}

    async def cancel_turn(self, conversation_id: str) -> bool:
        self.calls.append(conversation_id)
        return self.cancelled

    async def submit_turn(
        self, conversation_id: str, content: str, *, user_email: str
    ) -> object | None:
        self.submitted.append((conversation_id, content, user_email))
        return self.submit_error

    def active_turn_checkpoint(self, conversation_id: str) -> dict[str, str | None] | None:
        return self.checkpoints.get(conversation_id)


class _TaskQueue:
    def __init__(self) -> None:
        self.submit = AsyncMock(return_value=SimpleNamespace(task_id="task-1"))


class _TurnSchedulerWithTaskQueue:
    def __init__(self) -> None:
        self._task_queue = _TaskQueue()


class _CompactionStrategy:
    def __init__(self) -> None:
        self.compaction_threshold = 0.85
        self.calls: list[dict[str, object]] = []

    async def compact(self, session: SessionModel, **kwargs: object) -> object:
        self.calls.append({"session": session, **kwargs})
        return SimpleNamespace(
            compacted=False,
            method="skipped",
            reason="nothing_to_compact",
        )


class _SessionCache:
    def __init__(
        self,
        usage: dict[str, object] | None = None,
        tool_runtime_info: dict[str, object] | None = None,
    ) -> None:
        self.usage = usage
        self.tool_runtime_info = tool_runtime_info
        self.reasoning_effort_override: str | None = None

    def get_context_usage(self, _: str) -> dict[str, object] | None:
        return self.usage

    def get_model_override(self, _: str) -> None:
        return None

    def get_reasoning_effort_override(self, _: str) -> str | None:
        return self.reasoning_effort_override

    def get_tool_runtime_info(self, _: str) -> dict[str, object] | None:
        return self.tool_runtime_info

    def set_reasoning_effort_override(self, _: str, effort: str | None) -> None:
        self.reasoning_effort_override = effort


class _GuardrailsProvider:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail

    async def get_session(self, session_id: str) -> IntarisSession:
        if self.fail:
            raise RuntimeError("boom")
        return IntarisSession(
            session_id=session_id,
            user_id="user@example.com",
            agent_id="agent-1",
            status="completed",
            intention="Investigate issue",
            total_calls=4,
            approved_count=3,
            denied_count=1,
            escalated_count=0,
            created_at="2026-04-12T10:00:00Z",
            updated_at="2026-04-12T10:05:00Z",
        )


class _SessionFactory:
    class _Context:
        async def __aenter__(self) -> object:
            return object()

        async def __aexit__(self, exc_type: object, exc: object, tb: object) -> bool:
            return False

    def __call__(self) -> _Context:
        return self._Context()


class _DBSession:
    def __init__(self) -> None:
        self.commits = 0
        self.rollbacks = 0

    async def commit(self) -> None:
        self.commits += 1

    async def rollback(self) -> None:
        self.rollbacks += 1


class _DBSessionFactory:
    class _Context:
        def __init__(self, session: _DBSession) -> None:
            self._session = session

        async def __aenter__(self) -> _DBSession:
            return self._session

        async def __aexit__(self, exc_type: object, exc: object, tb: object) -> bool:
            return False

    def __init__(self, session: _DBSession | None = None) -> None:
        self.session = session or _DBSession()

    def __call__(self) -> _Context:
        return self._Context(self.session)


def _conversation() -> ConversationModel:
    return ConversationModel(
        conversation_id="conv-1",
        user_email="user@example.com",
        agent_id="agent-1",
        context=ConversationContext(type="web"),
    )


class _SessionManager:
    def __init__(self) -> None:
        self.create_conversation_with_root_session = AsyncMock(
            return_value=(
                ConversationModel(
                    conversation_id="conv-2",
                    user_email="user@example.com",
                    agent_id="agent-1",
                    context=ConversationContext(type="web"),
                    active_session_id="sess-2",
                ),
                SessionModel(
                    session_id="sess-2",
                    conversation_id="conv-2",
                    user_email="user@example.com",
                    agent_id="agent-1",
                ),
            )
        )
        self.rotate_session = AsyncMock(
            return_value=SessionModel(
                session_id="sess-2",
                conversation_id="conv-1",
                user_email="user@example.com",
                agent_id="agent-1",
            )
        )
        self.mark_completed = AsyncMock(return_value=True)
        self.fork_into_new_conversation = AsyncMock(
            return_value=(
                ConversationModel(
                    conversation_id="conv-2",
                    user_email="user@example.com",
                    agent_id="agent-1",
                    context=ConversationContext(type="web"),
                    active_session_id="sess-2",
                ),
                SessionModel(
                    session_id="sess-2",
                    conversation_id="conv-2",
                    user_email="user@example.com",
                    agent_id="agent-1",
                ),
                True,
            )
        )
        self.fork_active_turn_checkpoint_into_new_conversation = AsyncMock(
            return_value=(
                ConversationModel(
                    conversation_id="conv-2",
                    user_email="user@example.com",
                    agent_id="agent-1",
                    context=ConversationContext(type="web"),
                    active_session_id="sess-2",
                ),
                SessionModel(
                    session_id="sess-2",
                    conversation_id="conv-2",
                    user_email="user@example.com",
                    agent_id="agent-1",
                ),
                True,
            )
        )
        self.undo_last_turn = AsyncMock(return_value=None)
        self.redo_last_undo = AsyncMock(return_value=None)


def _session() -> SessionModel:
    return SessionModel(
        session_id="sess-1",
        conversation_id="conv-1",
        user_email="user@example.com",
        agent_id="agent-1",
    )


def _agent() -> AgentDefinition:
    return AgentDefinition(agent_id="agent-1", owner_email="user@example.com", name="Agent")


@pytest.mark.asyncio
async def test_manual_compact_resolves_same_session_model_from_default_route() -> None:
    strategy = _CompactionStrategy()
    resolve_model_target = AsyncMock(
        return_value=(
            "default-model",
            SimpleNamespace(provider_id="default-provider"),
        )
    )
    dispatcher = CommandDispatcher(
        session_factory=None,
        session_manager=None,
        session_cache=_SessionCache(),
        compaction_strategy=strategy,
        providers=SimpleNamespace(llm=SimpleNamespace(resolve_model_target=resolve_model_target)),
        pause_waiter=PauseWaiter(),
        notification_service=_NotificationService(),
    )

    result = await dispatcher.dispatch(
        "/compact",
        conversation=_conversation(),
        session=_session(),
        agent=_agent(),
        user_email="user@example.com",
    )

    assert result is not None
    resolve_model_target.assert_awaited_once_with(
        explicit_provider_id=None,
        task_type="default",
        acting_user_email="user@example.com",
    )
    model_context = cast(Any, strategy.calls[0]["model_context"])
    assert model_context.model == "default-model"
    assert model_context.provider_id == "default-provider"


@pytest.mark.asyncio
async def test_manual_compact_preserves_explicit_agent_model_context() -> None:
    strategy = _CompactionStrategy()
    resolve_model_target = AsyncMock()
    dispatcher = CommandDispatcher(
        session_factory=None,
        session_manager=None,
        session_cache=_SessionCache(),
        compaction_strategy=strategy,
        providers=SimpleNamespace(llm=SimpleNamespace(resolve_model_target=resolve_model_target)),
        pause_waiter=PauseWaiter(),
        notification_service=_NotificationService(),
    )

    result = await dispatcher.dispatch(
        "/compact",
        conversation=_conversation(),
        session=_session(),
        agent=AgentDefinition(
            agent_id="agent-1",
            owner_email="user@example.com",
            name="Agent",
            llm_config=AgentLLMConfig(
                model="agent-model",
                provider_id="agent-provider",
                reasoning_effort="none",
            ),
        ),
        user_email="user@example.com",
    )

    assert result is not None
    resolve_model_target.assert_not_awaited()
    model_context = cast(Any, strategy.calls[0]["model_context"])
    assert model_context.model == "agent-model"
    assert model_context.provider_id == "agent-provider"
    assert model_context.reasoning_effort == "none"


@pytest.mark.asyncio
async def test_new_web_conversation_does_not_clone_execution_paths() -> None:
    manager = _SessionManager()
    dispatcher = CommandDispatcher(
        session_factory=None,
        session_manager=manager,
        session_cache=None,
        compaction_strategy=None,
        providers=None,
        pause_waiter=PauseWaiter(),
        notification_service=_NotificationService(),
    )

    conversation = ConversationModel(
        conversation_id="conv-1",
        user_email="user@example.com",
        agent_id="agent-1",
        context=ConversationContext(
            type="web",
            ref="web:user:user@example.com:default",
            platform_data={
                "workspace_root": "/home/riker/src/esphome",
                "working_directory": "/home/riker/src/esphome",
                "draft_id": "draft-1",
            },
            memory_labels={"origin": "chat"},
        ),
    )

    result = await dispatcher.dispatch(
        "/new",
        conversation=conversation,
        session=_session(),
        agent=_agent(),
        user_email="user@example.com",
    )

    assert result is not None
    assert result.type == "conversation_created"
    context = manager.create_conversation_with_root_session.await_args.kwargs["context"]
    assert isinstance(context, ConversationContext)
    assert context.platform_data == {"draft_id": "draft-1"}
    assert context.memory_labels == {"origin": "chat"}
    manager.mark_completed.assert_awaited_once()


@pytest.mark.asyncio
async def test_new_web_command_cancels_busy_turn_and_creates_conversation() -> None:
    manager = _SessionManager()
    scheduler = _TurnScheduler(cancelled=True)
    dispatcher = CommandDispatcher(
        session_factory=None,
        session_manager=manager,
        session_cache=None,
        compaction_strategy=None,
        providers=None,
        pause_waiter=PauseWaiter(),
        notification_service=_NotificationService(),
        turn_scheduler=scheduler,
    )

    result = await dispatcher.dispatch(
        "/new",
        conversation=_conversation(),
        session=_session(),
        agent=_agent(),
        user_email="user@example.com",
        has_busy_turn=True,
    )

    assert result is not None
    assert result.type == "conversation_created"
    assert scheduler.calls == ["conv-1"]
    manager.create_conversation_with_root_session.assert_awaited_once()


@pytest.mark.asyncio
async def test_new_agent_direct_command_rotates_session_in_same_conversation() -> None:
    manager = _SessionManager()
    scheduler = _TurnScheduler(cancelled=True)
    dispatcher = CommandDispatcher(
        session_factory=None,
        session_manager=manager,
        session_cache=None,
        compaction_strategy=None,
        providers=None,
        pause_waiter=PauseWaiter(),
        notification_service=_NotificationService(),
        turn_scheduler=scheduler,
    )
    conversation = ConversationModel(
        conversation_id="conv-1",
        user_email="user@example.com",
        agent_id="agent-1",
        context=ConversationContext(
            type="web",
            ref=agent_direct_context_ref("user@example.com", "agent-1"),
            platform_data={"kind": AGENT_DIRECT_KIND},
        ),
    )

    result = await dispatcher.dispatch(
        "/new",
        conversation=conversation,
        session=_session(),
        agent=_agent(),
        user_email="user@example.com",
        has_busy_turn=True,
    )

    assert result is not None
    assert result.type == "session_reset"
    assert result.data["conversation_id"] == "conv-1"
    assert result.data["session_id"] == "sess-2"
    assert scheduler.calls == ["conv-1"]
    manager.rotate_session.assert_awaited_once()
    manager.create_conversation_with_root_session.assert_not_awaited()
    manager.mark_completed.assert_not_awaited()


@pytest.mark.asyncio
async def test_new_channel_command_cancels_busy_turn_and_rotates_session() -> None:
    manager = _SessionManager()
    scheduler = _TurnScheduler(cancelled=True)
    dispatcher = CommandDispatcher(
        session_factory=None,
        session_manager=manager,
        session_cache=None,
        compaction_strategy=None,
        providers=None,
        pause_waiter=PauseWaiter(),
        notification_service=_NotificationService(),
        turn_scheduler=scheduler,
    )
    conversation = ConversationModel(
        conversation_id="conv-1",
        user_email="user@example.com",
        agent_id="agent-1",
        context=ConversationContext(type="signal", ref="signal:acct:chat"),
    )

    result = await dispatcher.dispatch(
        "/new",
        conversation=conversation,
        session=_session(),
        agent=_agent(),
        user_email="user@example.com",
        has_busy_turn=True,
    )

    assert result is not None
    assert result.type == "session_reset"
    assert scheduler.calls == ["conv-1"]
    manager.rotate_session.assert_awaited_once()
    manager.create_conversation_with_root_session.assert_not_awaited()


@pytest.mark.asyncio
async def test_fork_command_creates_new_conversation_without_initial_message() -> None:
    manager = _SessionManager()
    scheduler = _TurnScheduler(cancelled=True)
    dispatcher = CommandDispatcher(
        session_factory=None,
        session_manager=manager,
        session_cache=None,
        compaction_strategy=None,
        providers=None,
        pause_waiter=PauseWaiter(),
        notification_service=_NotificationService(),
        turn_scheduler=scheduler,
    )

    result = await dispatcher.dispatch(
        "/fork",
        conversation=_conversation(),
        session=_session(),
        agent=_agent(),
        user_email="user@example.com",
    )

    assert result is not None
    assert result.type == "conversation_created"
    assert result.data["conversation_id"] == "conv-2"
    assert result.data["session_id"] == "sess-2"
    assert "initial_message_submitted" not in result.data
    assert scheduler.submitted == []
    manager.fork_into_new_conversation.assert_awaited_once()


@pytest.mark.asyncio
async def test_fork_command_with_message_submits_initial_turn_to_fork() -> None:
    manager = _SessionManager()
    scheduler = _TurnScheduler(cancelled=True)
    dispatcher = CommandDispatcher(
        session_factory=None,
        session_manager=manager,
        session_cache=None,
        compaction_strategy=None,
        providers=None,
        pause_waiter=PauseWaiter(),
        notification_service=_NotificationService(),
        turn_scheduler=scheduler,
    )

    result = await dispatcher.dispatch(
        "/fork continue exploring this topic",
        conversation=_conversation(),
        session=_session(),
        agent=_agent(),
        user_email="user@example.com",
    )

    assert result is not None
    assert result.type == "conversation_created"
    assert result.data["conversation_id"] == "conv-2"
    assert result.data["session_id"] == "sess-2"
    assert result.data["initial_message_submitted"] is True
    assert scheduler.submitted == [("conv-2", "continue exploring this topic", "user@example.com")]
    manager.fork_into_new_conversation.assert_awaited_once()


@pytest.mark.asyncio
async def test_fork_command_with_message_reports_turn_start_error() -> None:
    manager = _SessionManager()
    scheduler = _TurnScheduler(cancelled=True)
    scheduler.submit_error = SimpleNamespace(
        code="conflict",
        message="Conversation is not active",
        recoverable=False,
    )
    dispatcher = CommandDispatcher(
        session_factory=None,
        session_manager=manager,
        session_cache=None,
        compaction_strategy=None,
        providers=None,
        pause_waiter=PauseWaiter(),
        notification_service=_NotificationService(),
        turn_scheduler=scheduler,
    )

    result = await dispatcher.dispatch(
        "/fork continue exploring this topic",
        conversation=_conversation(),
        session=_session(),
        agent=_agent(),
        user_email="user@example.com",
    )

    assert result is not None
    assert result.type == "conversation_created"
    assert result.data["initial_message_submitted"] is False
    assert result.data["initial_message_error"] == {
        "code": "conflict",
        "message": "Conversation is not active",
        "recoverable": False,
    }


@pytest.mark.asyncio
async def test_fork_command_with_message_uses_checkpoint_during_busy_turn() -> None:
    manager = _SessionManager()
    scheduler = _TurnScheduler(cancelled=True)
    scheduler.checkpoints["conv-1"] = {"session_id": "sess-1", "turn_id": "turn-active"}
    dispatcher = CommandDispatcher(
        session_factory=None,
        session_manager=manager,
        session_cache=None,
        compaction_strategy=None,
        providers=None,
        pause_waiter=PauseWaiter(),
        notification_service=_NotificationService(),
        turn_scheduler=scheduler,
    )

    result = await dispatcher.dispatch(
        "/fork continue exploring this topic",
        conversation=_conversation(),
        session=_session(),
        agent=_agent(),
        user_email="user@example.com",
        has_busy_turn=True,
    )

    assert result is not None
    assert result.type == "conversation_created"
    assert result.data["checkpoint"] == "last_completed_turn"
    assert result.data["excluded_active_turn_id"] == "turn-active"
    assert result.data["initial_message_submitted"] is True
    assert scheduler.submitted == [("conv-2", "continue exploring this topic", "user@example.com")]
    manager.fork_into_new_conversation.assert_not_awaited()
    manager.fork_active_turn_checkpoint_into_new_conversation.assert_awaited_once()
    kwargs = manager.fork_active_turn_checkpoint_into_new_conversation.await_args.kwargs
    assert kwargs["active_turn_id"] == "turn-active"


@pytest.mark.asyncio
async def test_fork_command_without_message_uses_checkpoint_during_busy_turn() -> None:
    manager = _SessionManager()
    scheduler = _TurnScheduler(cancelled=True)
    scheduler.checkpoints["conv-1"] = {"session_id": "sess-1", "turn_id": "turn-active"}
    dispatcher = CommandDispatcher(
        session_factory=None,
        session_manager=manager,
        session_cache=None,
        compaction_strategy=None,
        providers=None,
        pause_waiter=PauseWaiter(),
        notification_service=_NotificationService(),
        turn_scheduler=scheduler,
    )

    result = await dispatcher.dispatch(
        "/fork",
        conversation=_conversation(),
        session=_session(),
        agent=_agent(),
        user_email="user@example.com",
        has_busy_turn=True,
    )

    assert result is not None
    assert result.type == "conversation_created"
    assert result.text == "Conversation forked from the last completed turn."
    assert result.data["checkpoint"] == "last_completed_turn"
    assert result.data["excluded_active_turn_id"] == "turn-active"
    assert "initial_message_submitted" not in result.data
    assert scheduler.submitted == []
    manager.fork_into_new_conversation.assert_not_awaited()
    manager.fork_active_turn_checkpoint_into_new_conversation.assert_awaited_once()


@pytest.mark.asyncio
async def test_fork_command_reports_missing_checkpoint_during_busy_turn() -> None:
    manager = _SessionManager()
    scheduler = _TurnScheduler(cancelled=True)
    scheduler.checkpoints["conv-1"] = {"session_id": "sess-1", "turn_id": None}
    dispatcher = CommandDispatcher(
        session_factory=None,
        session_manager=manager,
        session_cache=None,
        compaction_strategy=None,
        providers=None,
        pause_waiter=PauseWaiter(),
        notification_service=_NotificationService(),
        turn_scheduler=scheduler,
    )

    result = await dispatcher.dispatch(
        "/fork continue exploring this topic",
        conversation=_conversation(),
        session=_session(),
        agent=_agent(),
        user_email="user@example.com",
        has_busy_turn=True,
    )

    assert result is not None
    assert result.type == "error"
    assert result.data == {"code": "active_turn_checkpoint_unavailable"}
    assert scheduler.submitted == []
    manager.fork_into_new_conversation.assert_not_awaited()
    manager.fork_active_turn_checkpoint_into_new_conversation.assert_not_awaited()


@pytest.mark.asyncio
async def test_fork_command_with_message_requires_turn_scheduler() -> None:
    manager = _SessionManager()
    dispatcher = CommandDispatcher(
        session_factory=None,
        session_manager=manager,
        session_cache=None,
        compaction_strategy=None,
        providers=None,
        pause_waiter=PauseWaiter(),
        notification_service=_NotificationService(),
    )

    result = await dispatcher.dispatch(
        "/fork continue exploring this topic",
        conversation=_conversation(),
        session=_session(),
        agent=_agent(),
        user_email="user@example.com",
    )

    assert result is not None
    assert result.type == "error"
    assert result.data == {"code": "turn_scheduler_unavailable"}
    manager.fork_into_new_conversation.assert_not_awaited()


@pytest.mark.asyncio
async def test_plan_command_sets_conversation_chat_mode() -> None:
    dispatcher = CommandDispatcher(
        session_factory=None,
        session_manager=None,
        session_cache=None,
        compaction_strategy=None,
        providers=None,
        pause_waiter=PauseWaiter(),
        notification_service=_NotificationService(),
    )
    conversation = _conversation()

    result = await dispatcher.dispatch(
        "/plan",
        conversation=conversation,
        session=_session(),
        agent=_agent(),
        user_email="user@example.com",
    )

    assert result is not None
    assert result.type == "system_message"
    assert conversation.context.platform_data["chat_mode"] == "plan"
    assert result.data == {"chat_mode": "plan", "chat_mode_source": "conversation_override"}


@pytest.mark.asyncio
async def test_default_command_clears_conversation_chat_mode() -> None:
    dispatcher = CommandDispatcher(
        session_factory=None,
        session_manager=None,
        session_cache=None,
        compaction_strategy=None,
        providers=None,
        pause_waiter=PauseWaiter(),
        notification_service=_NotificationService(),
    )
    conversation = _conversation()
    conversation.context.platform_data["chat_mode"] = "plan"

    result = await dispatcher.dispatch(
        "/default",
        conversation=conversation,
        session=_session(),
        agent=_agent(),
        user_email="user@example.com",
    )

    assert result is not None
    assert result.type == "system_message"
    assert "chat_mode" not in conversation.context.platform_data
    assert result.data == {"chat_mode": "default", "chat_mode_source": "system_default"}


@pytest.mark.asyncio
async def test_one_shot_chat_mode_command_is_not_dispatched() -> None:
    dispatcher = CommandDispatcher(
        session_factory=None,
        session_manager=None,
        session_cache=None,
        compaction_strategy=None,
        providers=None,
        pause_waiter=PauseWaiter(),
        notification_service=_NotificationService(),
    )

    result = await dispatcher.dispatch(
        "/plan inspect the code",
        conversation=_conversation(),
        session=_session(),
        agent=_agent(),
        user_email="user@example.com",
    )

    assert result is None


@pytest.mark.asyncio
async def test_undo_blocked_during_busy_turn() -> None:
    manager = _SessionManager()
    dispatcher = CommandDispatcher(
        session_factory=None,
        session_manager=manager,
        session_cache=None,
        compaction_strategy=None,
        providers=None,
        pause_waiter=PauseWaiter(),
        notification_service=_NotificationService(),
    )

    result = await dispatcher.dispatch(
        "/undo",
        conversation=_conversation(),
        session=_session(),
        agent=_agent(),
        user_email="user@example.com",
        has_busy_turn=True,
    )

    assert result is not None
    assert result.type == "error"
    assert result.data == {"code": "turn_active"}
    manager.undo_last_turn.assert_not_called()


@pytest.mark.asyncio
async def test_redo_blocked_during_busy_turn() -> None:
    manager = _SessionManager()
    dispatcher = CommandDispatcher(
        session_factory=None,
        session_manager=manager,
        session_cache=None,
        compaction_strategy=None,
        providers=None,
        pause_waiter=PauseWaiter(),
        notification_service=_NotificationService(),
    )

    result = await dispatcher.dispatch(
        "/redo",
        conversation=_conversation(),
        session=_session(),
        agent=_agent(),
        user_email="user@example.com",
        has_busy_turn=True,
    )

    assert result is not None
    assert result.type == "error"
    assert result.data == {"code": "turn_active"}
    manager.redo_last_undo.assert_not_called()


@pytest.mark.asyncio
async def test_undo_nothing_to_undo() -> None:
    manager = _SessionManager()
    dispatcher = CommandDispatcher(
        session_factory=None,
        session_manager=manager,
        session_cache=None,
        compaction_strategy=None,
        providers=None,
        pause_waiter=PauseWaiter(),
        notification_service=_NotificationService(),
    )

    result = await dispatcher.dispatch(
        "/undo",
        conversation=_conversation(),
        session=_session(),
        agent=_agent(),
        user_email="user@example.com",
    )

    assert result is not None
    assert result.type == "system_message"
    assert result.text == "Nothing to undo."


@pytest.mark.asyncio
async def test_redo_nothing_to_redo() -> None:
    manager = _SessionManager()
    dispatcher = CommandDispatcher(
        session_factory=None,
        session_manager=manager,
        session_cache=None,
        compaction_strategy=None,
        providers=None,
        pause_waiter=PauseWaiter(),
        notification_service=_NotificationService(),
    )

    result = await dispatcher.dispatch(
        "/redo",
        conversation=_conversation(),
        session=_session(),
        agent=_agent(),
        user_email="user@example.com",
    )

    assert result is not None
    assert result.type == "system_message"
    assert result.text == "Nothing to redo."


@pytest.mark.asyncio
async def test_undo_returns_history_rebased_without_new_conversation() -> None:
    manager = _SessionManager()
    new_session = SessionModel(
        session_id="sess-undo",
        conversation_id="conv-1",
        user_email="user@example.com",
        agent_id="agent-1",
    )
    manager.undo_last_turn.return_value = _HistoryRebaseResult(
        operation="undo",
        session=new_session,
        previous_session=_session(),
        undo_available=True,
        redo_available=True,
        message="Undid last turn.",
    )
    dispatcher = CommandDispatcher(
        session_factory=None,
        session_manager=manager,
        session_cache=None,
        compaction_strategy=None,
        providers=None,
        pause_waiter=PauseWaiter(),
        notification_service=_NotificationService(),
    )

    result = await dispatcher.dispatch(
        "/undo",
        conversation=_conversation(),
        session=_session(),
        agent=_agent(),
        user_email="user@example.com",
    )

    assert result is not None
    assert result.type == "history_rebased"
    assert result.data["operation"] == "undo"
    assert result.data["conversation_id"] == "conv-1"
    assert result.data["session_id"] == "sess-undo"
    manager.create_conversation_with_root_session.assert_not_called()


@pytest.mark.asyncio
async def test_redo_returns_history_rebased_same_conversation() -> None:
    manager = _SessionManager()
    restored_session = SessionModel(
        session_id="sess-1",
        conversation_id="conv-1",
        user_email="user@example.com",
        agent_id="agent-1",
    )
    manager.redo_last_undo.return_value = _HistoryRebaseResult(
        operation="redo",
        session=restored_session,
        previous_session=SessionModel(
            session_id="sess-undo",
            conversation_id="conv-1",
            user_email="user@example.com",
            agent_id="agent-1",
        ),
        undo_available=True,
        redo_available=False,
        message="Redid last turn.",
    )
    dispatcher = CommandDispatcher(
        session_factory=None,
        session_manager=manager,
        session_cache=None,
        compaction_strategy=None,
        providers=None,
        pause_waiter=PauseWaiter(),
        notification_service=_NotificationService(),
    )

    result = await dispatcher.dispatch(
        "/redo",
        conversation=_conversation(),
        session=_session(),
        agent=_agent(),
        user_email="user@example.com",
    )

    assert result is not None
    assert result.type == "history_rebased"
    assert result.data["operation"] == "redo"
    assert result.data["conversation_id"] == "conv-1"
    assert result.data["session_id"] == "sess-1"


def test_slash_command_classification_ignores_one_shot_chat_modes() -> None:
    assert is_system_slash_command_message("/undo")
    assert is_system_slash_command_message("/fork continue exploring this topic")
    assert is_system_slash_command_message("/model gpt-5")
    assert is_system_slash_command_message("/task do work")
    assert is_system_slash_command_message("/research compare options")
    assert is_system_slash_command_message("/implement add support")
    assert is_system_slash_command_message("/delegate coordinate this")
    assert not is_system_slash_command_message("/plan inspect this code")
    assert not is_system_slash_command_message("/taskfoo do work")


@pytest.mark.asyncio
async def test_task_slash_command_creates_background_task() -> None:
    scheduler = _TurnSchedulerWithTaskQueue()
    dispatcher = CommandDispatcher(
        session_factory=None,
        session_manager=None,
        session_cache=None,
        compaction_strategy=None,
        providers=None,
        pause_waiter=PauseWaiter(),
        notification_service=_NotificationService(),
        turn_scheduler=scheduler,
    )
    conversation = _conversation()
    conversation.context.platform_data = {
        "workspace_root": "/repo",
        "working_directory": "/repo/src",
    }

    result = await dispatcher.dispatch(
        "/task inspect task routing",
        conversation=conversation,
        session=_session(),
        agent=_agent(),
        user_email="user@example.com",
    )

    assert result is not None
    assert result.type == "queued"
    assert result.data["task_id"] == "task-1"
    scheduler._task_queue.submit.assert_awaited_once()
    kwargs = scheduler._task_queue.submit.await_args.kwargs
    assert kwargs["title"] == "inspect task routing"
    assert kwargs["description"] == "inspect task routing"
    assert kwargs["source_type"] == "chat"
    assert kwargs["source_ref"] == "conv-1"
    assert kwargs["workflow_id"] is None
    assert kwargs["workspace_root"] == "/repo"
    assert kwargs["working_directory"] == "/repo/src"


@pytest.mark.asyncio
async def test_task_slash_command_requires_description() -> None:
    dispatcher = CommandDispatcher(
        session_factory=None,
        session_manager=None,
        session_cache=None,
        compaction_strategy=None,
        providers=None,
        pause_waiter=PauseWaiter(),
        notification_service=_NotificationService(),
        turn_scheduler=_TurnSchedulerWithTaskQueue(),
    )

    result = await dispatcher.dispatch(
        "/task",
        conversation=_conversation(),
        session=_session(),
        agent=_agent(),
        user_email="user@example.com",
    )

    assert result is not None
    assert result.type == "error"
    assert result.data["code"] == "missing_task_description"


@pytest.mark.asyncio
async def test_research_and_implement_commands_set_workflow_hints() -> None:
    scheduler = _TurnSchedulerWithTaskQueue()
    dispatcher = CommandDispatcher(
        session_factory=None,
        session_manager=None,
        session_cache=None,
        compaction_strategy=None,
        providers=None,
        pause_waiter=PauseWaiter(),
        notification_service=_NotificationService(),
        turn_scheduler=scheduler,
    )

    await dispatcher.dispatch(
        "/research compare task routing options",
        conversation=_conversation(),
        session=_session(),
        agent=_agent(),
        user_email="user@example.com",
    )
    await dispatcher.dispatch(
        "/implement clean task routing",
        conversation=_conversation(),
        session=_session(),
        agent=_agent(),
        user_email="user@example.com",
    )

    calls = scheduler._task_queue.submit.await_args_list
    assert calls[0].kwargs["workflow_id"] == "system:research"
    assert calls[1].kwargs["workflow_id"] == "system:software-development"


@pytest.mark.asyncio
async def test_new_channel_session_clears_execution_paths_but_preserves_routing(
    monkeypatch,
) -> None:
    import cognis.store.queries as store_queries

    manager = _SessionManager()
    session_factory = _DBSessionFactory()
    update_context_data = AsyncMock(return_value=True)
    reset_active_executor = AsyncMock(return_value=True)
    monkeypatch.setattr(store_queries, "update_conversation_context_data", update_context_data)
    monkeypatch.setattr(store_queries, "reset_conversation_active_executor", reset_active_executor)
    pause_waiter = PauseWaiter()
    pause_waiter.register(
        PendingPause(
            pause_id="cred-direct",
            pause_type="credential_request",
            conversation_id="conv-1",
            session_id="sess-1",
        )
    )
    pause_waiter.register(
        PendingPause(
            pause_id="auth-direct",
            pause_type="auth_challenge",
            conversation_id="conv-1",
            session_id="sess-1",
        )
    )
    pause_waiter.register(
        PendingPause(
            pause_id="cred-task",
            pause_type="credential_request",
            conversation_id="conv-1",
            session_id="sess-1",
            task_id="task-1",
        )
    )
    notifications = _NotificationService()
    dispatcher = CommandDispatcher(
        session_factory=session_factory,
        session_manager=manager,
        session_cache=None,
        compaction_strategy=None,
        providers=None,
        pause_waiter=pause_waiter,
        notification_service=notifications,
    )

    conversation = ConversationModel(
        conversation_id="conv-1",
        user_email="user@example.com",
        agent_id="agent-1",
        context=ConversationContext(
            type="signal",
            ref="signal:acct-1:chat-1",
            platform_data={
                "channel_type": "signal",
                "account_id": "acct-1",
                "chat_id": "chat-1",
                "thread_id": None,
                "workspace_root": "/home/riker/src/esphome",
                "working_directory": "/home/riker/src/esphome",
            },
        ),
    )

    result = await dispatcher.dispatch(
        "/new",
        conversation=conversation,
        session=_session(),
        agent=_agent(),
        user_email="user@example.com",
    )

    assert result is not None
    assert result.type == "session_reset"
    assert "Executor selection will be resolved again" in (result.text or "")
    manager.rotate_session.assert_awaited_once()
    update_context_data.assert_awaited_once_with(
        session_factory.session,
        "conv-1",
        context_data={
            "channel_type": "signal",
            "account_id": "acct-1",
            "chat_id": "chat-1",
            "thread_id": None,
        },
    )
    assert conversation.context.platform_data == {
        "channel_type": "signal",
        "account_id": "acct-1",
        "chat_id": "chat-1",
        "thread_id": None,
    }
    assert session_factory.session.commits == 2
    reset_active_executor.assert_awaited_once_with(session_factory.session, "conv-1")
    assert session_factory.session.rollbacks == 0
    assert notifications.calls == [
        ("cred-direct", "cancel", {}),
        ("auth-direct", "cancel", {}),
    ]
    remaining_direct_credentials = [
        pause
        for pause in pause_waiter.list_pending(
            conversation_id="conv-1", pause_type="credential_request"
        )
        if pause.task_id is None
    ]
    assert remaining_direct_credentials == []
    assert pause_waiter.find_pending(task_id="task-1", pause_type="credential_request") is not None
    assert pause_waiter.find_pending(conversation_id="conv-1", pause_type="auth_challenge") is None


@pytest.mark.asyncio
async def test_stop_cancels_turn_and_resolves_pending_pauses() -> None:
    pause_waiter = PauseWaiter()
    pause_waiter.register(
        PendingPause(
            pause_id="notif-direct",
            pause_type="step_question",
            conversation_id="conv-1",
            session_id="sess-1",
        )
    )
    pause_waiter.register(
        PendingPause(
            pause_id="esc-1",
            pause_type="escalation",
            conversation_id="conv-1",
            session_id="sess-1",
        )
    )
    pause_waiter.register(
        PendingPause(
            pause_id="cred-direct",
            pause_type="credential_request",
            conversation_id="conv-1",
            session_id="sess-1",
        )
    )
    pause_waiter.register(
        PendingPause(
            pause_id="auth-direct",
            pause_type="auth_challenge",
            conversation_id="conv-1",
            session_id="sess-1",
        )
    )
    pause_waiter.register(
        PendingPause(
            pause_id="cred-task",
            pause_type="credential_request",
            conversation_id="conv-1",
            session_id="sess-1",
            task_id="task-1",
        )
    )
    notifications = _NotificationService()
    scheduler = _TurnScheduler(cancelled=True)
    dispatcher = CommandDispatcher(
        session_factory=None,
        session_manager=None,
        session_cache=None,
        compaction_strategy=None,
        providers=None,
        pause_waiter=pause_waiter,
        notification_service=notifications,
        turn_scheduler=scheduler,
    )

    result = await dispatcher.dispatch(
        "/stop",
        conversation=_conversation(),
        session=_session(),
        agent=_agent(),
        user_email="user@example.com",
    )

    assert result is not None
    assert result.type == "system_message"
    assert "Stopped the current work" in (result.text or "")
    assert scheduler.calls == ["conv-1"]
    assert notifications.calls == [
        ("notif-direct", "cancel", {"reason": "user_stop"}),
        ("cred-direct", "cancel", {}),
        ("auth-direct", "cancel", {}),
        ("esc-1", "deny", {"note": "Stopped by user"}),
    ]
    assert pause_waiter.find_pending(conversation_id="conv-1", pause_type="step_question") is None
    assert pause_waiter.find_pending(conversation_id="conv-1", pause_type="auth_challenge") is None
    assert pause_waiter.find_pending(task_id="task-1", pause_type="credential_request") is not None


@pytest.mark.asyncio
async def test_stop_reports_when_nothing_is_active() -> None:
    dispatcher = CommandDispatcher(
        session_factory=None,
        session_manager=None,
        session_cache=None,
        compaction_strategy=None,
        providers=None,
        pause_waiter=PauseWaiter(),
        notification_service=_NotificationService(),
        turn_scheduler=_TurnScheduler(cancelled=False),
    )

    result = await dispatcher.dispatch(
        "/stop",
        conversation=_conversation(),
        session=_session(),
        agent=_agent(),
        user_email="user@example.com",
    )

    assert result is not None
    assert result.text == "No active work to stop."


@pytest.mark.asyncio
async def test_help_lists_stop_and_alias_commands() -> None:
    dispatcher = CommandDispatcher(
        session_factory=None,
        session_manager=None,
        session_cache=None,
        compaction_strategy=None,
        providers=None,
        pause_waiter=PauseWaiter(),
        notification_service=SimpleNamespace(
            **_NotificationService().__dict__,
            list_pending=AsyncMock(
                return_value=[SimpleNamespace(notification_id="gate-1", notification_type="gate")]
            ),
        ),
    )

    result = await dispatcher.dispatch(
        "/help",
        conversation=_conversation(),
        session=_session(),
        agent=_agent(),
        user_email="user@example.com",
    )

    assert result is not None
    assert "/stop" in (result.text or "")
    assert "/cancel" in (result.text or "")
    assert "/summarize" in (result.text or "")
    assert "/reset" in (result.text or "")


@pytest.mark.asyncio
async def test_help_tolerates_accidental_space_after_slash() -> None:
    dispatcher = CommandDispatcher(
        session_factory=None,
        session_manager=None,
        session_cache=None,
        compaction_strategy=None,
        providers=None,
        pause_waiter=PauseWaiter(),
        notification_service=SimpleNamespace(
            **_NotificationService().__dict__,
            list_pending=AsyncMock(return_value=[]),
        ),
    )

    result = await dispatcher.dispatch(
        "/ help",
        conversation=_conversation(),
        session=_session(),
        agent=_agent(),
        user_email="user@example.com",
    )

    assert result is not None
    assert result.type == "system_message"
    assert "/help" in (result.text or "")


@pytest.mark.asyncio
async def test_stop_recovers_local_pause_when_notification_resolution_fails() -> None:
    pause_waiter = PauseWaiter()
    pause_waiter.register(
        PendingPause(
            pause_id="esc-1",
            pause_type="escalation",
            conversation_id="conv-1",
            session_id="sess-1",
        )
    )
    notifications = _NotificationService()
    notifications.resolve_result = False
    dispatcher = CommandDispatcher(
        session_factory=None,
        session_manager=None,
        session_cache=None,
        compaction_strategy=None,
        providers=None,
        pause_waiter=pause_waiter,
        notification_service=notifications,
        turn_scheduler=_TurnScheduler(cancelled=False),
    )

    result = await dispatcher.dispatch(
        "/stop",
        conversation=_conversation(),
        session=_session(),
        agent=_agent(),
        user_email="user@example.com",
    )

    assert result is not None
    assert result.type == "system_message"
    assert "Stopped the current work" in (result.text or "")
    assert notifications.orphaned == [("esc-1", "user_stop_recovery")]
    assert pause_waiter.find_pending(conversation_id="conv-1", pause_type="escalation") is None


@pytest.mark.asyncio
async def test_approve_reports_failure_when_notification_service_cannot_resolve() -> None:
    pause_waiter = PauseWaiter()
    pause_waiter.register(
        PendingPause(
            pause_id="esc-1",
            pause_type="escalation",
            conversation_id="conv-1",
            session_id="sess-1",
            context={"tool_name": "bash"},
        )
    )
    notifications = _NotificationService()
    notifications.resolve_result = False
    dispatcher = CommandDispatcher(
        session_factory=None,
        session_manager=None,
        session_cache=None,
        compaction_strategy=None,
        providers=None,
        pause_waiter=pause_waiter,
        notification_service=notifications,
    )

    result = await dispatcher.dispatch(
        "/approve looks safe",
        conversation=_conversation(),
        session=_session(),
        agent=_agent(),
        user_email="user@example.com",
    )

    assert result is not None
    assert result.type == "error"
    assert result.data["code"] == "escalation_resolve_failed"


@pytest.mark.asyncio
async def test_retry_resolves_pending_gate_with_note() -> None:
    pause_waiter = PauseWaiter()
    pause_waiter.register(
        PendingPause(
            pause_id="gate-1",
            pause_type="gate",
            conversation_id="conv-1",
            task_id="task-1",
            step_name="review",
            options=[{"label": "Retry step", "action": "revise(plan)"}],
        )
    )
    notifications = _NotificationService()
    notifications.pending_notifications = [
        SimpleNamespace(notification_id="gate-1", notification_type="gate")
    ]
    dispatcher = CommandDispatcher(
        session_factory=None,
        session_manager=None,
        session_cache=None,
        compaction_strategy=None,
        providers=None,
        pause_waiter=pause_waiter,
        notification_service=notifications,
    )

    result = await dispatcher.dispatch(
        "/retry incorporate the review",
        conversation=_conversation(),
        session=_session(),
        agent=_agent(),
        user_email="user@example.com",
    )

    assert result is not None
    assert result.type == "system_message"
    assert result.text == "Retrying the paused workflow step: incorporate the review"
    assert notifications.calls == [("gate-1", "revise(plan)", {"note": "incorporate the review"})]


@pytest.mark.asyncio
async def test_continue_resolves_pending_gate_with_note() -> None:
    pause_waiter = PauseWaiter()
    pause_waiter.register(
        PendingPause(
            pause_id="gate-1",
            pause_type="gate",
            conversation_id="conv-1",
            task_id="task-1",
            step_name="review",
            options=[{"label": "Continue", "action": "continue"}],
        )
    )
    notifications = _NotificationService()
    notifications.pending_notifications = [
        SimpleNamespace(notification_id="gate-1", notification_type="gate")
    ]
    dispatcher = CommandDispatcher(
        session_factory=None,
        session_manager=None,
        session_cache=None,
        compaction_strategy=None,
        providers=None,
        pause_waiter=pause_waiter,
        notification_service=notifications,
    )

    result = await dispatcher.dispatch(
        "/continue continue anyway",
        conversation=_conversation(),
        session=_session(),
        agent=_agent(),
        user_email="user@example.com",
    )

    assert result is not None
    assert result.text == "Continuing the paused workflow: continue anyway"
    assert notifications.calls == [("gate-1", "continue", {"note": "continue anyway"})]


@pytest.mark.asyncio
async def test_cancel_prefers_pending_gate_over_stop() -> None:
    pause_waiter = PauseWaiter()
    pause_waiter.register(
        PendingPause(
            pause_id="gate-1",
            pause_type="gate",
            conversation_id="conv-1",
            task_id="task-1",
            step_name="review",
            options=[{"label": "Cancel", "action": "cancel"}],
        )
    )
    notifications = _NotificationService()
    notifications.pending_notifications = [
        SimpleNamespace(notification_id="gate-1", notification_type="gate")
    ]
    scheduler = _TurnScheduler(cancelled=False)
    dispatcher = CommandDispatcher(
        session_factory=None,
        session_manager=None,
        session_cache=None,
        compaction_strategy=None,
        providers=None,
        pause_waiter=pause_waiter,
        notification_service=notifications,
        turn_scheduler=scheduler,
    )

    result = await dispatcher.dispatch(
        "/cancel stop the task",
        conversation=_conversation(),
        session=_session(),
        agent=_agent(),
        user_email="user@example.com",
    )

    assert result is not None
    assert result.text == "Cancelled the paused workflow: stop the task"
    assert notifications.calls == [("gate-1", "cancel", {"note": "stop the task"})]
    assert scheduler.calls == []


@pytest.mark.asyncio
async def test_retry_reports_when_pending_gate_has_no_retry_action() -> None:
    pause_waiter = PauseWaiter()
    pause_waiter.register(
        PendingPause(
            pause_id="gate-1",
            pause_type="gate",
            conversation_id="conv-1",
            task_id="task-1",
            step_name="review",
            options=[{"label": "Continue", "action": "continue"}],
        )
    )
    notifications = _NotificationService()
    notifications.pending_notifications = [
        SimpleNamespace(notification_id="gate-1", notification_type="gate")
    ]
    dispatcher = CommandDispatcher(
        session_factory=None,
        session_manager=None,
        session_cache=None,
        compaction_strategy=None,
        providers=None,
        pause_waiter=pause_waiter,
        notification_service=notifications,
    )

    result = await dispatcher.dispatch(
        "/retry",
        conversation=_conversation(),
        session=_session(),
        agent=_agent(),
        user_email="user@example.com",
    )

    assert result is not None
    assert result.type == "error"
    assert result.data == {"code": "gate_retry_unavailable", "pause_id": "gate-1"}


@pytest.mark.asyncio
async def test_continue_reports_when_gate_does_not_offer_continue() -> None:
    pause_waiter = PauseWaiter()
    pause_waiter.register(
        PendingPause(
            pause_id="gate-1",
            pause_type="gate",
            conversation_id="conv-1",
            task_id="task-1",
            step_name="review",
            options=[{"label": "Retry step", "action": "revise(plan)"}],
        )
    )
    notifications = _NotificationService()
    notifications.pending_notifications = [
        SimpleNamespace(notification_id="gate-1", notification_type="gate")
    ]
    dispatcher = CommandDispatcher(
        session_factory=None,
        session_manager=None,
        session_cache=None,
        compaction_strategy=None,
        providers=None,
        pause_waiter=pause_waiter,
        notification_service=notifications,
    )

    result = await dispatcher.dispatch(
        "/continue",
        conversation=_conversation(),
        session=_session(),
        agent=_agent(),
        user_email="user@example.com",
    )

    assert result is not None
    assert result.type == "error"
    assert result.data == {"code": "gate_action_unavailable", "pause_id": "gate-1"}


@pytest.mark.asyncio
async def test_gate_commands_target_latest_pending_gate_in_conversation() -> None:
    pause_waiter = PauseWaiter()
    pause_waiter.register(
        PendingPause(
            pause_id="gate-1",
            pause_type="gate",
            conversation_id="conv-1",
            task_id="task-1",
            step_name="review-1",
            options=[{"label": "Retry step", "action": "revise(plan)"}],
        )
    )
    pause_waiter.register(
        PendingPause(
            pause_id="gate-2",
            pause_type="gate",
            conversation_id="conv-1",
            task_id="task-2",
            step_name="review-2",
            options=[{"label": "Retry step", "action": "revise(plan)"}],
        )
    )
    notifications = _NotificationService()
    notifications.pending_notifications = [
        SimpleNamespace(notification_id="gate-2", notification_type="gate"),
        SimpleNamespace(notification_id="gate-1", notification_type="gate"),
    ]
    dispatcher = CommandDispatcher(
        session_factory=None,
        session_manager=None,
        session_cache=None,
        compaction_strategy=None,
        providers=None,
        pause_waiter=pause_waiter,
        notification_service=notifications,
    )

    result = await dispatcher.dispatch(
        "/retry",
        conversation=_conversation(),
        session=_session(),
        agent=_agent(),
        user_email="user@example.com",
    )

    assert result is not None
    assert result.type == "system_message"
    assert notifications.calls == [("gate-2", "revise(plan)", {"note": ""})]


@pytest.mark.asyncio
async def test_gate_commands_ignore_stale_pause_waiter_entries_when_notifications_absent() -> None:
    pause_waiter = PauseWaiter()
    pause_waiter.register(
        PendingPause(
            pause_id="gate-stale",
            pause_type="gate",
            conversation_id="conv-1",
            task_id="task-1",
            step_name="review",
            options=[{"label": "Retry step", "action": "revise(plan)"}],
        )
    )
    notifications = _NotificationService()
    notifications.pending_notifications = []
    dispatcher = CommandDispatcher(
        session_factory=None,
        session_manager=None,
        session_cache=None,
        compaction_strategy=None,
        providers=None,
        pause_waiter=pause_waiter,
        notification_service=notifications,
    )

    result = await dispatcher.dispatch(
        "/retry",
        conversation=_conversation(),
        session=_session(),
        agent=_agent(),
        user_email="user@example.com",
    )

    assert result is not None
    assert result.type == "system_message"
    assert result.text == "No pending workflow gate to resolve."


@pytest.mark.asyncio
async def test_gate_commands_do_not_fall_back_to_older_waiter_when_latest_notification_has_no_waiter() -> (
    None
):
    pause_waiter = PauseWaiter()
    pause_waiter.register(
        PendingPause(
            pause_id="gate-1",
            pause_type="gate",
            conversation_id="conv-1",
            task_id="task-1",
            step_name="review-1",
            options=[{"label": "Retry step", "action": "revise(plan)"}],
        )
    )
    notifications = _NotificationService()
    notifications.pending_notifications = [
        SimpleNamespace(notification_id="gate-2", notification_type="gate"),
        SimpleNamespace(notification_id="gate-1", notification_type="gate"),
    ]
    dispatcher = CommandDispatcher(
        session_factory=None,
        session_manager=None,
        session_cache=None,
        compaction_strategy=None,
        providers=None,
        pause_waiter=pause_waiter,
        notification_service=notifications,
    )

    result = await dispatcher.dispatch(
        "/retry",
        conversation=_conversation(),
        session=_session(),
        agent=_agent(),
        user_email="user@example.com",
    )

    assert result is not None
    assert result.type == "system_message"
    assert result.text == "No pending workflow gate to resolve."
    assert notifications.calls == []


@pytest.mark.asyncio
async def test_cancel_without_pending_gate_falls_back_to_stop() -> None:
    notifications = _NotificationService()
    scheduler = _TurnScheduler(cancelled=True)
    dispatcher = CommandDispatcher(
        session_factory=None,
        session_manager=None,
        session_cache=None,
        compaction_strategy=None,
        providers=None,
        pause_waiter=PauseWaiter(),
        notification_service=notifications,
        turn_scheduler=scheduler,
    )

    result = await dispatcher.dispatch(
        "/cancel",
        conversation=_conversation(),
        session=_session(),
        agent=_agent(),
        user_email="user@example.com",
    )

    assert result is not None
    assert result.type == "system_message"
    assert "Stopped the current work" in (result.text or "")
    assert scheduler.calls == ["conv-1"]


@pytest.mark.asyncio
async def test_context_reports_effective_prompt_budget() -> None:
    dispatcher = CommandDispatcher(
        session_factory=None,
        session_manager=None,
        session_cache=_SessionCache(
            {
                "prompt_tokens": 29_000,
                "max_context_tokens": 1_048_576,
                "percentage": 2.8,
                "model": "gpt-5.4",
                "provider_id": "proxy",
                "reasoning_effort": None,
                "reserve_output_tokens": 500_000,
                "effective_reserve_output_tokens": 32_768,
                "effective_prompt_budget": 1_015_808,
                "loop_pressure_threshold": 965_017,
                "projection_policy": {
                    "phase": "within_turn",
                    "pressure_mode": "normal",
                    "steady_target_tokens": 320_000,
                    "burst_target_tokens": 600_000,
                    "hard_prompt_tokens": 660_000,
                    "cross_turn_tool_budget_tokens": 57_600,
                    "within_turn_tool_budget_tokens": 228_000,
                    "preserve_recent_tool_groups": 20,
                    "preserve_recent_tool_bytes": 912_000,
                    "max_historical_tool_result_bytes": 25_600,
                },
                "last_llm_usage": {
                    "prompt_tokens": 12_345,
                    "completion_tokens": 678,
                    "total_tokens": 13_023,
                    "cache_read_input_tokens": 7_277,
                    "cache_creation_input_tokens": 248,
                },
            }
        ),
        compaction_strategy=SimpleNamespace(compaction_threshold=0.85),
        providers=SimpleNamespace(
            llm=SimpleNamespace(
                get_model_info=AsyncMock(return_value=SimpleNamespace(context_window=1_048_576))
            )
        ),
        pause_waiter=PauseWaiter(),
        notification_service=_NotificationService(),
    )

    result = await dispatcher.dispatch(
        "/context",
        conversation=_conversation(),
        session=_session(),
        agent=_agent(),
        user_email="user@example.com",
    )

    assert result is not None
    assert result.text is not None
    assert "Model context window: 1,048,576 tokens" in result.text
    assert "Effective context window: 1,048,576 tokens" in result.text
    assert (
        "Requested output tokens: 500,000 (controller reserve: 32,768 for prompt budgeting)"
        in result.text
    )
    assert "Effective prompt budget: 1,015,808 tokens" in result.text
    assert "Loop pressure threshold: 965,017 tokens" in result.text
    assert "Projection policy: within_turn / normal" in result.text
    assert "Projection prompt targets: 320,000 steady, 600,000 burst, 660,000 hard" in result.text
    assert "Projection tool budgets: 57,600 cross-turn, 228,000 within-turn tokens" in result.text
    assert (
        "Projection retention: 20 recent groups, 912,000 recent bytes, 25,600 historical bytes"
        in result.text
    )
    assert "Last LLM call tokens: 12,345 prompt, 678 completion, 13,023 total" in result.text
    assert "Last LLM call cache read tokens: 7,277" in result.text
    assert "Last LLM call cache write tokens: 248" in result.text


@pytest.mark.asyncio
async def test_thinking_command_uses_model_specific_levels() -> None:
    cache = _SessionCache({"model": "gpt-5.4"})
    dispatcher = CommandDispatcher(
        session_factory=None,
        session_manager=None,
        session_cache=cache,
        compaction_strategy=None,
        providers=SimpleNamespace(
            llm=SimpleNamespace(
                get_model_info=AsyncMock(
                    return_value=SimpleNamespace(
                        reasoning_efforts=["default", "none", "low", "medium", "high", "xhigh"]
                    )
                )
            )
        ),
        pause_waiter=PauseWaiter(),
        notification_service=_NotificationService(),
    )

    result = await dispatcher.dispatch(
        "/thinking",
        conversation=_conversation(),
        session=_session(),
        agent=_agent(),
        user_email="user@example.com",
    )

    assert result is not None
    assert result.text is not None
    assert "Thinking effort: default (not set)" in result.text
    assert "Available levels: default, none, low, medium, high, xhigh" in result.text


@pytest.mark.asyncio
async def test_thinking_command_remaps_generic_level_to_current_model() -> None:
    cache = _SessionCache({"model": "gpt-5.4"})
    dispatcher = CommandDispatcher(
        session_factory=None,
        session_manager=None,
        session_cache=cache,
        compaction_strategy=None,
        providers=SimpleNamespace(
            llm=SimpleNamespace(
                get_model_info=AsyncMock(
                    return_value=SimpleNamespace(
                        reasoning_efforts=["default", "none", "low", "medium", "high", "xhigh"]
                    )
                )
            )
        ),
        pause_waiter=PauseWaiter(),
        notification_service=_NotificationService(),
    )

    result = await dispatcher.dispatch(
        "/thinking max",
        conversation=_conversation(),
        session=_session(),
        agent=_agent(),
        user_email="user@example.com",
    )

    assert result is not None
    assert (
        result.text
        == "Thinking effort set to: xhigh (mapped from max)\nTakes effect on next message."
    )
    assert cache.reasoning_effort_override == "xhigh"


@pytest.mark.asyncio
async def test_info_renders_runtime_intaris_and_subsession_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parent = SessionModel(
        session_id="sess-1",
        conversation_id="conv-1",
        user_email="user@example.com",
        agent_id="agent-1",
        status="idle",
        previous_session_id="sess-0",
    )
    child = SessionModel(
        session_id="sess-child-1",
        conversation_id="conv-1",
        user_email="user@example.com",
        agent_id="agent-2",
        parent_session_id="sess-1",
        delegation_mode="delegate",
        delegation_task="Research the root cause",
        status="completed",
        result_summary="Prepared the investigation summary",
        completion_reason="completed",
    )

    async def _get_session_row(_: object, __: str) -> SessionModel:
        return parent

    async def _list_child_sessions(_: object, __: str) -> list[SessionModel]:
        return [child]

    monkeypatch.setattr("cognis.store.queries.get_session_row", _get_session_row)
    monkeypatch.setattr("cognis.store.queries.list_child_sessions", _list_child_sessions)
    monkeypatch.setattr("cognis.core.session._to_session_model", lambda row: row)

    dispatcher = CommandDispatcher(
        session_factory=_SessionFactory(),
        session_manager=None,
        session_cache=_SessionCache(
            usage={
                "prompt_tokens": 29_000,
                "max_context_tokens": 1_048_576,
                "percentage": 2.8,
                "model": "gpt-5.4",
                "provider_id": "proxy",
                "last_llm_usage": {},
            },
            tool_runtime_info={
                "strategy": "openai_responses_controller_search_fallback",
                "step_profile_id": "system:direct-default",
                "step_profile_mode": "soft",
                "allow_tool_search": True,
                "inventory_tool_count": 12,
                "visible_tool_count": 4,
                "hidden_searchable_count": 8,
                "promoted_requested_count": 2,
                "promoted_visible_count": 1,
            },
        ),
        compaction_strategy=None,
        providers=SimpleNamespace(
            guardrails=_GuardrailsProvider(),
            llm=SimpleNamespace(
                get_model_info=AsyncMock(return_value=SimpleNamespace(context_window=1_048_576)),
                has_hosted_instruction_drift=lambda provider_id, model_id: (
                    (
                        provider_id,
                        model_id,
                    )
                    == ("proxy", "gpt-5.4")
                ),
                hosted_instruction_drift_reason=lambda provider_id, model_id: (
                    "server_returned_different_instructions"
                    if (provider_id, model_id) == ("proxy", "gpt-5.4")
                    else None
                ),
            ),
        ),
        pause_waiter=PauseWaiter(),
        notification_service=_NotificationService(),
    )

    result = await dispatcher.dispatch(
        "/info",
        conversation=_conversation(),
        session=parent,
        agent=_agent(),
        user_email="user@example.com",
        has_active_turn=True,
    )

    assert result is not None
    assert result.type == "system_message"
    assert result.text is not None
    assert "Model: gpt-5.4" in result.text
    assert "Model context window: 1,048,576 tokens" in result.text
    assert (
        "LLM diagnostics: provider reported hosted instruction drift "
        "(server_returned_different_instructions)" in result.text
    )
    assert "Status: running" in result.text
    assert "Session lifecycle: idle" in result.text
    assert "Previous session: sess-0" in result.text
    assert "Intaris status: completed" in result.text
    assert "Sub-sessions: 1" in result.text
    assert "sess-child-1 (completed, agent=agent-2)" in result.text
    assert "Delegation mode: delegate" in result.text
    assert "Task summary: Research the root cause" in result.text
    assert "Result summary: Prepared the investigation summary" in result.text
    assert "Completion reason: completed" in result.text
    assert "Tool exposure mode: openai_responses_controller_search_fallback" in result.text
    assert "Step profile: system:direct-default (soft)" in result.text
    assert "Tool search: enabled" in result.text
    # promoted shows as "visible/requested" when cap pressure drops some.
    assert "Tools: 4 visible, 12 eligible, 8 hidden, 1/2 promoted" in result.text


@pytest.mark.asyncio
async def test_info_reports_settling_when_turn_is_busy_but_not_running() -> None:
    dispatcher = CommandDispatcher(
        session_factory=None,
        session_manager=None,
        session_cache=_SessionCache(),
        compaction_strategy=None,
        providers=SimpleNamespace(
            guardrails=_GuardrailsProvider(),
            llm=SimpleNamespace(get_model_info=None),
        ),
        pause_waiter=PauseWaiter(),
        notification_service=_NotificationService(),
    )

    result = await dispatcher.dispatch(
        "/info",
        conversation=_conversation(),
        session=_session(),
        agent=_agent(),
        user_email="user@example.com",
        has_active_turn=False,
        has_busy_turn=True,
    )

    assert result is not None
    assert result.text is not None
    assert "Status: settling" in result.text


@pytest.mark.asyncio
async def test_info_uses_unavailable_when_intaris_fetch_fails() -> None:
    dispatcher = CommandDispatcher(
        session_factory=None,
        session_manager=None,
        session_cache=_SessionCache(),
        compaction_strategy=None,
        providers=SimpleNamespace(
            guardrails=_GuardrailsProvider(fail=True),
            llm=SimpleNamespace(get_model_info=None),
        ),
        pause_waiter=PauseWaiter(),
        notification_service=_NotificationService(),
    )

    result = await dispatcher.dispatch(
        "/info",
        conversation=_conversation(),
        session=_session(),
        agent=_agent(),
        user_email="user@example.com",
    )

    assert result is not None
    assert result.text is not None
    assert "Intaris status: unavailable" in result.text
    assert "Intaris stats: unavailable" in result.text


@pytest.mark.asyncio
async def test_lsp_renders_normalized_statuses() -> None:
    dispatcher = CommandDispatcher(
        session_factory=None,
        session_manager=None,
        session_cache=None,
        compaction_strategy=None,
        providers=SimpleNamespace(
            executor=SimpleNamespace(
                get_lsp_statuses=AsyncMock(
                    return_value=[
                        LSPStatusReport(
                            supported=True,
                            enabled=True,
                            executor_id="exec-1",
                            executor_type="websocket",
                            state="ready",
                            config=LSPStatusConfig(
                                enabled=True,
                                auto_install=False,
                                diagnostics_timeout_ms=10000,
                                idle_timeout_seconds=600,
                                max_concurrent_servers=8,
                            ),
                            totals=LSPStatusTotals(active_server_count=1, files_tracked=2),
                        ),
                        LSPStatusReport(
                            supported=True,
                            enabled=False,
                            executor_id="exec-2",
                            executor_type="subprocess",
                            state="disabled",
                            config=LSPStatusConfig(
                                enabled=False,
                                auto_install=False,
                                diagnostics_timeout_ms=10000,
                                idle_timeout_seconds=600,
                                max_concurrent_servers=8,
                            ),
                            totals=LSPStatusTotals(),
                        ),
                    ]
                )
            )
        ),
        pause_waiter=PauseWaiter(),
        notification_service=_NotificationService(),
    )

    result = await dispatcher.dispatch(
        "/lsp",
        conversation=_conversation(),
        session=_session(),
        agent=_agent(),
        user_email="user@example.com",
    )

    assert result is not None
    assert result.text is not None
    assert "exec-1 (websocket) - ready" in result.text
    assert "exec-2 (subprocess) - disabled" in result.text


# ---------------------------------------------------------------------------
# Stage 36: /executor slash command
# ---------------------------------------------------------------------------


def _conversation_with_active(active: str | None = None) -> ConversationModel:
    return ConversationModel(
        conversation_id="conv-1",
        user_email="user@example.com",
        agent_id="agent-1",
        context=ConversationContext(type="web"),
        active_executor_id=active,
    )


@pytest.mark.asyncio
async def test_executor_command_no_arg_shows_status(monkeypatch) -> None:
    """/executor with no arg renders the active executor + assigned pool."""

    from cognis.core.executor_pool import (
        ExecutorAvailability,
        ExecutorPool,
        ResolvedExecutorTarget,
    )

    pool = ExecutorPool(
        primary=[
            ResolvedExecutorTarget(
                executor_id="exec-primary",
                executor_type="websocket",
                is_primary=True,
                selection_source="explicit",
                description=None,
                state=ExecutorAvailability.USABLE,
            )
        ],
        additional=[
            ResolvedExecutorTarget(
                executor_id="exec-add",
                executor_type="websocket",
                is_primary=False,
                selection_source="additional_explicit",
                description="Mac",
                state=ExecutorAvailability.USABLE,
            )
        ],
    )

    async def _fake_pool(self, agent, user_email):  # type: ignore[no-untyped-def]
        return pool

    monkeypatch.setattr(
        CommandDispatcher,
        "_resolve_executor_pool_for_command",
        _fake_pool,
    )

    dispatcher = CommandDispatcher(
        session_factory=_DBSessionFactory(),
        session_manager=_SessionManager(),
        session_cache=None,
        compaction_strategy=None,
        providers=None,
        pause_waiter=PauseWaiter(),
        notification_service=_NotificationService(),
    )
    result = await dispatcher.dispatch(
        "/executor",
        conversation=_conversation_with_active(active="exec-primary"),
        session=_session(),
        agent=_agent(),
        user_email="user@example.com",
    )
    assert result is not None
    assert result.type == "system_message"
    assert "Active executor: exec-primary" in result.text
    assert "exec-add" in result.text
    assert "additional" in result.text


@pytest.mark.asyncio
async def test_executor_command_switch_succeeds(monkeypatch) -> None:
    """/executor <id> with assigned + usable target persists the switch."""

    from cognis.core.executor_pool import (
        ExecutorAvailability,
        ExecutorPool,
        ResolvedExecutorTarget,
    )

    pool = ExecutorPool(
        primary=[
            ResolvedExecutorTarget(
                executor_id="exec-1",
                executor_type="websocket",
                is_primary=True,
                selection_source="explicit",
                description=None,
                state=ExecutorAvailability.USABLE,
            ),
            ResolvedExecutorTarget(
                executor_id="exec-2",
                executor_type="websocket",
                is_primary=True,
                selection_source="selector",
                description=None,
                state=ExecutorAvailability.USABLE,
            ),
        ]
    )

    async def _fake_pool(self, agent, user_email):  # type: ignore[no-untyped-def]
        return pool

    monkeypatch.setattr(
        CommandDispatcher,
        "_resolve_executor_pool_for_command",
        _fake_pool,
    )

    persisted: dict[str, str | None] = {"id": None}

    async def _set_active(_session, conversation_id, active_executor_id, **_metadata):
        persisted["id"] = active_executor_id
        return True

    import cognis.store.queries as store_queries

    monkeypatch.setattr(
        store_queries,
        "set_conversation_active_executor",
        _set_active,
    )

    dispatcher = CommandDispatcher(
        session_factory=_DBSessionFactory(),
        session_manager=_SessionManager(),
        session_cache=None,
        compaction_strategy=None,
        providers=None,
        pause_waiter=PauseWaiter(),
        notification_service=_NotificationService(),
    )
    result = await dispatcher.dispatch(
        "/executor exec-2",
        conversation=_conversation_with_active(active="exec-1"),
        session=_session(),
        agent=_agent(),
        user_email="user@example.com",
    )
    assert result is not None
    assert result.type == "system_message"
    assert persisted["id"] == "exec-2"
    assert result.data.get("code") == "executor_switched"
    assert result.data.get("executor_id") == "exec-2"


@pytest.mark.asyncio
async def test_executor_command_switch_to_unassigned_errors(monkeypatch) -> None:
    """/executor <unassigned> returns an error and leaves active unchanged."""

    from cognis.core.executor_pool import (
        ExecutorAvailability,
        ExecutorPool,
        ResolvedExecutorTarget,
    )

    pool = ExecutorPool(
        primary=[
            ResolvedExecutorTarget(
                executor_id="exec-1",
                executor_type="websocket",
                is_primary=True,
                selection_source="explicit",
                description=None,
                state=ExecutorAvailability.USABLE,
            )
        ]
    )

    async def _fake_pool(self, agent, user_email):  # type: ignore[no-untyped-def]
        return pool

    monkeypatch.setattr(
        CommandDispatcher,
        "_resolve_executor_pool_for_command",
        _fake_pool,
    )

    dispatcher = CommandDispatcher(
        session_factory=_DBSessionFactory(),
        session_manager=_SessionManager(),
        session_cache=None,
        compaction_strategy=None,
        providers=None,
        pause_waiter=PauseWaiter(),
        notification_service=_NotificationService(),
    )
    result = await dispatcher.dispatch(
        "/executor exec-ghost",
        conversation=_conversation_with_active(active="exec-1"),
        session=_session(),
        agent=_agent(),
        user_email="user@example.com",
    )
    assert result is not None
    assert result.type == "error"
    assert "not assigned" in result.text.lower()
    assert result.data.get("reason") == "not_assigned"


@pytest.mark.asyncio
async def test_executor_command_in_task_conversation_propagates_to_task_pin(
    monkeypatch,
) -> None:
    """Stage 36: /executor in a task-bound conversation updates the task pin."""

    from cognis.core.executor_pool import (
        ExecutorAvailability,
        ExecutorPool,
        ResolvedExecutorTarget,
    )

    pool = ExecutorPool(
        primary=[
            ResolvedExecutorTarget(
                executor_id="exec-1",
                executor_type="websocket",
                is_primary=True,
                selection_source="explicit",
                description=None,
                state=ExecutorAvailability.USABLE,
            ),
            ResolvedExecutorTarget(
                executor_id="exec-2",
                executor_type="websocket",
                is_primary=True,
                selection_source="selector",
                description=None,
                state=ExecutorAvailability.USABLE,
            ),
        ]
    )

    async def _fake_pool(self, agent, user_email):  # type: ignore[no-untyped-def]
        return pool

    monkeypatch.setattr(
        CommandDispatcher,
        "_resolve_executor_pool_for_command",
        _fake_pool,
    )

    persisted_conv: list[tuple[str, str]] = []
    persisted_task: list[tuple[str, str]] = []

    async def _set_active(_session, conversation_id, executor_id, **_metadata):
        persisted_conv.append((conversation_id, executor_id))
        return True

    async def _set_task(_session, task_id, executor_id, **_metadata):
        persisted_task.append((task_id, executor_id))
        return True

    import cognis.store.queries as store_queries

    monkeypatch.setattr(store_queries, "set_conversation_active_executor", _set_active)
    monkeypatch.setattr(store_queries, "set_task_active_executor", _set_task)

    # Conversation context.type=task, ref=task-id triggers task-pin update.
    conversation = ConversationModel(
        conversation_id="conv-step-1",
        user_email="user@example.com",
        agent_id="agent-1",
        context=ConversationContext(type="task", ref="task-99"),
        active_executor_id="exec-1",
    )

    dispatcher = CommandDispatcher(
        session_factory=_DBSessionFactory(),
        session_manager=_SessionManager(),
        session_cache=None,
        compaction_strategy=None,
        providers=None,
        pause_waiter=PauseWaiter(),
        notification_service=_NotificationService(),
    )
    result = await dispatcher.dispatch(
        "/executor exec-2",
        conversation=conversation,
        session=_session(),
        agent=_agent(),
        user_email="user@example.com",
    )
    assert result is not None
    assert result.type == "system_message"
    assert persisted_conv == [("conv-step-1", "exec-2")]
    assert persisted_task == [("task-99", "exec-2")]
