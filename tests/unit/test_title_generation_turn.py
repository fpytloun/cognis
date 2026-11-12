"""Title generation through real direct-turn recording, for all four toggles."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from cognis.core.agent_loop import CHAT_POLICY, AgentLoop, PauseWaiter, SessionLock, StepContext
from cognis.core.events import EventType
from cognis.models.agent import AgentDefinition
from cognis.models.session import EventAppendResult, ReasoningReportResult, SessionEvent
from cognis.models.workflow import StepDefinition
from cognis.providers.backends.guardrails.null import NoGuardrailsProvider
from cognis.store.models import Conversation, Session
from tests.unit.test_agent_loop import (
    _FakeContextAssembler,
    _FinalAssistantContentLLM,
    _NoopRememberQueue,
    _NoopSessionCache,
    _NoopSessionManager,
)
from tests.unit.test_untitled_conversation_recovery import (
    database as database,
)
from tests.unit.test_untitled_conversation_recovery import (
    page,
    seed,
    visible,
    worker,
)


@pytest.mark.asyncio
@pytest.mark.parametrize("analytics_enabled", [False, True])
@pytest.mark.parametrize("backend", ["none", "intaris"])
async def test_real_direct_turn_records_user_evidence_for_independent_title(
    database, analytics_enabled, backend
):
    _, factory = database
    await seed(factory)
    stored: list[dict] = []

    async def record(*_, **kwargs):
        events: list[SessionEvent] = kwargs["events"] if "events" in kwargs else _[1]
        first_seq = len(stored) + 1
        stored.extend(event.model_dump() for event in events)
        return EventAppendResult(
            ok=True, count=len(events), first_seq=first_seq, last_seq=len(stored)
        )

    intaris = SimpleNamespace(
        record_events=AsyncMock(side_effect=record),
        read_events=AsyncMock(side_effect=lambda *_, **__: page(*stored)),
        report_reasoning=AsyncMock(
            return_value=ReasoningReportResult(
                ok=True, title="Intaris suggestion", intention="Fix login"
            )
        ),
        evaluate=AsyncMock(side_effect=AssertionError("No evaluation call")),
        health=AsyncMock(return_value=SimpleNamespace(status="healthy")),
    )
    guardrails = NoGuardrailsProvider(intaris) if backend == "none" else intaris
    agent = AgentDefinition(
        agent_id="shared-agent",
        owner_email="owner@example.com",
        name="Shared",
        capabilities={"guardrails_backend": backend, "analytics_enabled": analytics_enabled},
    )
    from cognis.core.session import _to_conversation_model, _to_session_model

    async with factory() as db:
        conversation = _to_conversation_model(await db.get(Conversation, "conversation"))
        row = await db.get(Session, "conversation-session")
        row.status = "active"
        await db.commit()
        session = _to_session_model(row)
    ctx = StepContext(
        step_definition=StepDefinition(name="direct", type="run", prompt=""),
        session=session,
        conversation=conversation,
        agent=agent,
        policy=CHAT_POLICY,
        user_message="Oprav přihlašování",
        user_attachments=[],
        system_initiated=False,
    )

    class SessionManager(_NoopSessionManager):
        def session_factory(self):
            return factory()

    loop_events = SimpleNamespace(publish=AsyncMock())
    loop = AgentLoop(
        providers=SimpleNamespace(llm=_FinalAssistantContentLLM(), guardrails=guardrails),
        session_manager=SessionManager(),
        session_cache=_NoopSessionCache(),
        context_assembler=_FakeContextAssembler(),
        compaction_strategy=SimpleNamespace(),
        tool_router=SimpleNamespace(),
        remember_queue=_NoopRememberQueue(),
        event_bus=loop_events,
        session_lock=SessionLock(),
        pause_waiter=PauseWaiter(),
    )
    assert await loop.run_step(ctx) is not None
    assert any(event["type"] == "user_message" for event in stored)
    assert await visible(factory) == (None, "unset")
    reconciler, bus = worker(factory, guardrails)
    assert await reconciler.reconcile() == 1
    assert await visible(factory) == ("Oprava přihlašování", "cognis")
    assert intaris.report_reasoning.await_count == (0 if backend == "none" else 1)
    assert not any(
        call.args[0].type == EventType.CONVERSATION_UPDATED
        for call in loop_events.publish.call_args_list
    )
    intaris.evaluate.assert_not_awaited()
    bus.publish.assert_awaited_once()
