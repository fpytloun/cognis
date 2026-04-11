from __future__ import annotations

import pytest

from cognis.core.agent_loop import PauseWaiter, PendingPause
from cognis.core.commands import CommandDispatcher
from cognis.models.agent import AgentDefinition
from cognis.models.session import ConversationContext, ConversationModel, SessionModel


class _NotificationService:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict[str, object]]] = []
        self.orphaned: list[tuple[str, str]] = []
        self.resolve_result = True

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


class _TurnScheduler:
    def __init__(self, *, cancelled: bool) -> None:
        self.cancelled = cancelled
        self.calls: list[str] = []

    async def cancel_turn(self, conversation_id: str) -> bool:
        self.calls.append(conversation_id)
        return self.cancelled


def _conversation() -> ConversationModel:
    return ConversationModel(
        conversation_id="conv-1",
        user_email="user@example.com",
        agent_id="agent-1",
        context=ConversationContext(type="web"),
    )


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
        ("esc-1", "deny", {"note": "Stopped by user"}),
    ]


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
        notification_service=_NotificationService(),
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
