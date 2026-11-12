from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from cognis.api.chat_v2.event_store import RawSessionEvent
from cognis.api.chat_v2.normalizer import normalize_session_events
from cognis.api.chat_v2.projector import project_timeline
from cognis.core.notifications import NotificationService
from tests.unit.test_notifications import (
    _FakeEventBus,
    _FakePauseWaiter,
    _FakeSessionFactory,
    _notification_row,
)


@pytest.mark.asyncio
@pytest.mark.parametrize("human", [True, False])
async def test_question_answer_reaches_intention_before_resolution(monkeypatch, human):
    row = _notification_row()
    row.notification_type = "step_question"
    row.payload = {
        "questions": [
            {
                "id": "target",
                "question": "Which sample exhibit replaces exhibit 203?",
                "allow_custom": True,
            }
        ]
    }
    reply = {
        "answers": [
            {
                "question_id": "target",
                "selected_option_ids": [],
                "custom_answer": "Use sample exhibit https://museum.example.test/exhibits/407",
            }
        ],
        "human_answer": True,
    }
    events = []

    async def record(**kwargs):
        assert row.status == "resolving"
        events.extend(kwargs["events"])
        assert kwargs["idempotency_key"].endswith(":question_answer:call-1")
        return SimpleNamespace(ok=True)

    async def refresh(**kwargs):
        assert row.status == "resolving"
        assert events and kwargs["from_events"] and kwargs["wait_for_intention"]

    provider = SimpleNamespace(
        record_events=AsyncMock(side_effect=record),
        report_reasoning=AsyncMock(side_effect=refresh),
    )
    monkeypatch.setattr(
        "cognis.core.notifications.get_session_row",
        AsyncMock(
            return_value=SimpleNamespace(
                user_email=row.user_email, intaris_session_id="guardrail-session"
            )
        ),
    )
    service = NotificationService(
        session_factory=_FakeSessionFactory(row),
        pause_waiter=_FakePauseWaiter(),
        event_bus=_FakeEventBus(),
        providers=SimpleNamespace(guardrails=provider),
    )
    service._record_user_interaction = AsyncMock()
    assert await service.resolve(
        row.notification_id,
        "continue",
        reply,
        user_email=row.user_email,
        human_answer=human,
    )
    assert row.status == "resolved"
    assert provider.report_reasoning.await_count == int(human)
    if human:
        assert [event.type for event in events] == ["assistant_message", "user_message"]
        assert "407" in events[1].data["content"]
        assert "203" not in events[1].data["content"]
        assert events[1].data["intention_eligible"]
        # The guardrail receives question context and the answer; chat replay shows
        # neither, because the resolved interaction card owns their presentation.
        normalized = normalize_session_events(
            [
                RawSessionEvent(
                    store_id="intaris",
                    session_id="guardrail-session",
                    seq=seq,
                    type=event.type,
                    data=event.data,
                )
                for seq, event in enumerate(events, 1)
            ]
        )
        assert project_timeline(normalized.events).timeline.items == []
    assert await service.resolve(
        row.notification_id,
        "continue",
        reply,
        user_email=row.user_email,
        human_answer=human,
    )
    assert provider.report_reasoning.await_count == int(human)


@pytest.mark.asyncio
async def test_failed_publication_keeps_claim_and_blocks_competing_answer(monkeypatch):
    monkeypatch.setattr("cognis.core.notifications._RESOLUTION_CLAIM_SECONDS", 0.05)
    monkeypatch.setattr("cognis.core.notifications._RESOLUTION_POLL_SECONDS", 0.005)
    row = _notification_row()
    row.notification_type = "step_question"
    row.payload = {"questions": [{"id": "scope", "question": "Which scope?"}]}
    service = NotificationService(
        session_factory=_FakeSessionFactory(row),
        pause_waiter=_FakePauseWaiter(),
        event_bus=_FakeEventBus(),
        providers=SimpleNamespace(),
    )
    service._record_user_interaction = AsyncMock()
    service._record_question_answer = AsyncMock(side_effect=RuntimeError("unavailable"))
    reply = {
        "answers": [
            {"question_id": "scope", "selected_option_ids": [], "custom_answer": "Read only"}
        ],
        "submission_id": "first-submission",
    }
    with pytest.raises(RuntimeError, match="unavailable"):
        await service.resolve(
            row.notification_id, "continue", reply, user_email=row.user_email, human_answer=True
        )
    assert row.status == "resolving"
    assert not await service.resolve(
        row.notification_id,
        "continue",
        {**reply, "answers": []},
        user_email=row.user_email,
        human_answer=True,
    )
    service._record_question_answer = AsyncMock()
    # Waiting recovery must preserve human provenance when the claim expires.
    service._wait_for_existing_resolution_claim = AsyncMock(
        wraps=service._wait_for_existing_resolution_claim
    )
    assert await service.resolve(
        row.notification_id, "continue", reply, user_email=row.user_email, human_answer=True
    )
    assert row.status == "resolved"
    service._wait_for_existing_resolution_claim.assert_awaited_once()


@pytest.mark.asyncio
async def test_authenticated_task_response_sets_human_origin(monkeypatch):
    from cognis.api.routes import tasks

    monkeypatch.setattr(tasks, "forbid_mutation_for_viewer", lambda _: None)
    monkeypatch.setattr(
        tasks,
        "_require_task",
        AsyncMock(return_value=SimpleNamespace(created_by="owner@example.com")),
    )
    respond = AsyncMock(return_value={"status": "running"})
    monkeypatch.setattr(tasks, "respond_task_input", respond)
    state = SimpleNamespace(
        pause_waiter=None, notification_service=None, task_queue=None, session_factory=None
    )
    await tasks.step_response(
        SimpleNamespace(app=SimpleNamespace(state=state)),
        "task-review",
        SimpleNamespace(model_dump=lambda **_: {"answers": []}),
    )
    assert respond.await_args.kwargs["human_answer"] is True
