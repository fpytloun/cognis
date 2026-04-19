from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from cognis.core.agent_loop import PauseWaiter, PendingPause
from cognis.core.commands import CommandDispatcher
from cognis.models.agent import AgentDefinition
from cognis.models.session import (
    ConversationContext,
    ConversationModel,
    IntarisSession,
    SessionModel,
)
from cognis.tools.executor.lsp.runtime import LSPStatusConfig, LSPStatusReport, LSPStatusTotals


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

    async def cancel_turn(self, conversation_id: str) -> bool:
        self.calls.append(conversation_id)
        return self.cancelled


class _SessionCache:
    def __init__(self, usage: dict[str, object] | None = None) -> None:
        self.usage = usage

    def get_context_usage(self, _: str) -> dict[str, object] | None:
        return self.usage

    def get_reasoning_effort_override(self, _: str) -> None:
        return None


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
                "max_context_tokens": 250_000,
                "percentage": 11.6,
                "model": "gpt-5.4",
                "provider_id": "proxy",
                "reasoning_effort": None,
                "reserve_output_tokens": 500_000,
                "effective_reserve_output_tokens": 62_500,
                "effective_prompt_budget": 187_500,
                "loop_pressure_threshold": 178_125,
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
    assert (
        "Reserved output tokens: 500,000 (clamped to 62,500 for loop pressure checks)"
        in result.text
    )
    assert "Effective prompt budget: 187,500 tokens" in result.text
    assert "Loop pressure threshold: 178,125 tokens" in result.text


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
        session=parent,
        agent=_agent(),
        user_email="user@example.com",
        has_active_turn=True,
    )

    assert result is not None
    assert result.type == "system_message"
    assert result.text is not None
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
