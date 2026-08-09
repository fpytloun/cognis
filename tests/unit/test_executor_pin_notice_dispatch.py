from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from cognis.core.context import events_to_messages
from cognis.core.executor_pin_notice_dispatch import ExecutorPinNoticeDispatcher


class _Result:
    ok = True


class _Session:
    def __init__(self, row: Any, transition: Any, conversation: Any, active: Any) -> None:
        self.row = row
        self.transition = transition
        self.conversation = conversation
        self.active = active
        self.committed = False

    async def __aenter__(self) -> _Session:
        return self

    async def __aexit__(self, *_args: Any) -> None:
        return None

    async def get(self, model: Any, key: str) -> Any:
        name = getattr(model, "__name__", "")
        if name == "ExecutorPinNoticeOutboxRow":
            return self.row
        if name == "ExecutorPinTransitionRow":
            return self.transition
        if name == "Conversation":
            return self.conversation
        return self.active

    async def execute(self, _statement: Any) -> Any:
        self.row.delivered_at = object()
        self.transition.notice_appended_at = object()
        return SimpleNamespace(rowcount=1)

    async def commit(self) -> None:
        self.committed = True


class _Factory:
    def __init__(self, session: _Session) -> None:
        self.session = session

    def __call__(self) -> _Session:
        return self.session


@pytest.mark.asyncio
async def test_notice_dispatch_uses_stable_key_and_exactly_once() -> None:
    row = SimpleNamespace(
        outbox_id="outbox",
        delivered_at=None,
        transition_id="transition",
        payload={
            "event": "system_notice",
            "notice_id": "executor_failover:conv:2",
        },
        conversation_id="conv",
        intaris_session_id="intaris",
        idempotency_key="intaris:executor_failover:executor_failover:conv:2",
        user_email="u",
        agent_id="a",
    )
    transition = SimpleNamespace(notice_id="executor_failover:conv:2", notice_appended_at=None)
    conversation = SimpleNamespace(active_session_id="session")
    active = SimpleNamespace(intaris_session_id="intaris", user_email="u", agent_id="a")
    session = _Session(row, transition, conversation, active)
    keys: list[str | None] = []

    class Guardrails:
        async def record_events(self, **kwargs: Any) -> _Result:
            keys.append(kwargs["idempotency_key"])
            assert len(kwargs["events"]) == 1
            assert kwargs["events"][0].type == "lifecycle"
            assert kwargs["events"][0].data["event"] == "system_notice"
            return _Result()

    published: list[Any] = []

    class EventBus:
        async def publish(self, event: Any) -> None:
            published.append(event)

    dispatcher = ExecutorPinNoticeDispatcher(
        session_factory=_Factory(session),
        guardrails=Guardrails(),
        event_bus=EventBus(),
    )
    assert await dispatcher.dispatch_one("outbox") is True
    assert await dispatcher.dispatch_one("outbox") is True
    assert keys == ["intaris:executor_failover:executor_failover:conv:2"]
    assert len(published) == 1
    assert events_to_messages(
        [{"type": "lifecycle", "data": {**row.payload, "message": "Executor changed."}}]
    ) == [{"role": "system", "content": "Executor changed."}]


@pytest.mark.asyncio
async def test_append_failure_keeps_outbox_recoverable() -> None:
    row = SimpleNamespace(
        outbox_id="outbox",
        delivered_at=None,
        transition_id="transition",
        payload={
            "event": "system_notice",
            "notice_id": "n",
        },
        conversation_id="conv",
        intaris_session_id="intaris",
        idempotency_key="intaris:executor_failover:n",
        user_email="u",
        agent_id="a",
    )
    transition = SimpleNamespace(notice_id="n", notice_appended_at=None)
    session = _Session(
        row,
        transition,
        SimpleNamespace(active_session_id="session"),
        SimpleNamespace(intaris_session_id="intaris", user_email="u", agent_id="a"),
    )

    class Guardrails:
        async def record_events(self, **kwargs: Any) -> _Result:
            raise RuntimeError("Intaris unavailable")

    dispatcher = ExecutorPinNoticeDispatcher(
        session_factory=_Factory(session),
        guardrails=Guardrails(),
    )
    assert await dispatcher.dispatch_one("outbox") is False
    assert row.delivered_at is None
    assert transition.notice_appended_at is None
