from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest

from cognis.core.notifications import NotificationService


class _FakeSession:
    def __init__(self, row: Any) -> None:
        self._row = row

    async def __aenter__(self) -> _FakeSession:
        return self

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        return None

    async def get(self, model: Any, notification_id: str) -> Any:
        if self._row.notification_id == notification_id:
            return self._row
        return None

    async def execute(self, statement: Any) -> None:
        for key, value in getattr(statement, "_values", {}).items():
            attr = key.key if hasattr(key, "key") else str(key)
            if hasattr(value, "value"):
                value = value.value
            setattr(self._row, attr, value)

    async def commit(self) -> None:
        return None


class _FakeSessionFactory:
    def __init__(self, row: Any) -> None:
        self._row = row

    def __call__(self) -> _FakeSession:
        return _FakeSession(self._row)


class _FakePauseWaiter:
    def __init__(self, *, should_resolve: bool = True, order: list[str] | None = None) -> None:
        self.should_resolve = should_resolve
        self.order = order if order is not None else []

    def resolve(self, pause_id: str, resolution: Any) -> bool:
        self.order.append("resolve")
        return self.should_resolve


class _FakeGuardrails:
    def __init__(self, *, fail: bool = False, order: list[str] | None = None) -> None:
        self.fail = fail
        self.order = order if order is not None else []

    async def submit_decision(self, call_id: str, decision: str, note: str | None = None) -> None:
        self.order.append("submit")
        if self.fail:
            raise RuntimeError("submit failed")


class _FakeEventBus:
    def __init__(self) -> None:
        self.events: list[Any] = []

    async def publish(self, event: Any) -> None:
        self.events.append(event)


def _notification_row() -> Any:
    return SimpleNamespace(
        notification_id="call-1",
        notification_type="escalation",
        user_email="user@example.com",
        conversation_id="conv-1",
        task_id=None,
        step_name=None,
        step_run_id=None,
        session_id="sess-1",
        payload={},
        status="pending",
        resolution=None,
        created_at=datetime.now(UTC),
        resolved_at=None,
    )


@pytest.mark.asyncio
async def test_escalation_resolution_submits_before_unblocking_waiter() -> None:
    row = _notification_row()
    order: list[str] = []
    service = NotificationService(
        session_factory=_FakeSessionFactory(row),
        pause_waiter=_FakePauseWaiter(order=order),
        event_bus=_FakeEventBus(),
        providers=SimpleNamespace(guardrails=_FakeGuardrails(order=order)),
    )

    resolved = await service.resolve(
        "call-1",
        "approve",
        {"note": "safe"},
        user_email="user@example.com",
    )

    assert resolved is True
    assert order == ["submit", "resolve"]
    assert row.status == "resolved"
    assert row.resolution["decision"] == "approve"
    assert row.resolution["state"] == "resolved"


@pytest.mark.asyncio
async def test_escalation_resolution_keeps_pending_when_submit_fails() -> None:
    row = _notification_row()
    order: list[str] = []
    service = NotificationService(
        session_factory=_FakeSessionFactory(row),
        pause_waiter=_FakePauseWaiter(order=order),
        event_bus=_FakeEventBus(),
        providers=SimpleNamespace(guardrails=_FakeGuardrails(fail=True, order=order)),
    )

    resolved = await service.resolve(
        "call-1",
        "approve",
        {"note": "safe"},
        user_email="user@example.com",
    )

    assert resolved is False
    assert order == ["submit"]
    assert row.status == "pending"
    assert row.resolution is None
