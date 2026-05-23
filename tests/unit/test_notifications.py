from __future__ import annotations

from datetime import UTC, datetime, timedelta
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


class _FakeScalarResult:
    def __init__(self, rows: list[Any]) -> None:
        self._rows = rows

    def all(self) -> list[Any]:
        return list(self._rows)


class _FakeQueryResult:
    def __init__(self, rows: list[Any]) -> None:
        self._rows = rows

    def scalars(self) -> _FakeScalarResult:
        return _FakeScalarResult(self._rows)

    def scalar_one_or_none(self) -> Any:
        return self._rows[0] if self._rows else None


class _FakeListSession:
    def __init__(self, rows: list[Any], tasks: dict[str, Any]) -> None:
        self._rows = rows
        self._tasks = tasks

    async def __aenter__(self) -> _FakeListSession:
        return self

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        return None

    async def execute(self, statement: Any) -> _FakeQueryResult:
        values = getattr(statement, "_values", None)
        if values:
            target_id = None
            for criterion in getattr(
                statement, "_where_criteria", ()
            ):  # pragma: no branch - simple test stub
                right = getattr(criterion, "right", None)
                if hasattr(right, "value"):
                    target_id = right.value
                    break
            for row in self._rows:
                if target_id is not None and row.notification_id != target_id:
                    continue
                for key, value in values.items():
                    attr = key.key if hasattr(key, "key") else str(key)
                    if hasattr(value, "value"):
                        value = value.value
                    setattr(row, attr, value)
        return _FakeQueryResult(self._rows)

    async def get(self, model: Any, key: str) -> Any:
        for row in self._rows:
            if row.notification_id == key:
                return row
        return None

    async def commit(self) -> None:
        return None


class _FakeListSessionFactory:
    def __init__(self, rows: list[Any], tasks: dict[str, Any]) -> None:
        self._rows = rows
        self._tasks = tasks

    def __call__(self) -> _FakeListSession:
        return _FakeListSession(self._rows, self._tasks)


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
    def __init__(
        self,
        *,
        fail: bool = False,
        order: list[str] | None = None,
        escalations: dict[str, Any] | None = None,
    ) -> None:
        self.fail = fail
        self.order = order if order is not None else []
        self.escalations = escalations or {}

    async def submit_decision(self, call_id: str, decision: str, note: str | None = None) -> None:
        self.order.append("submit")
        if self.fail:
            raise RuntimeError("submit failed")

    async def get_escalation(self, call_id: str) -> Any:
        self.order.append("get_escalation")
        return self.escalations.get(call_id)


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


def _task_row(task_id: str, status: str) -> Any:
    return SimpleNamespace(task_id=task_id, status=status)


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


@pytest.mark.asyncio
async def test_list_pending_omits_and_orphans_notifications_for_terminal_tasks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    active_row = SimpleNamespace(
        notification_id="notif_active",
        notification_type="gate",
        user_email="user@example.com",
        conversation_id="conv-1",
        task_id="task_active",
        step_name="review",
        step_run_id=None,
        session_id="sess-1",
        payload={},
        status="pending",
        resolution=None,
        created_at=datetime.now(UTC),
        resolved_at=None,
    )
    stale_row = SimpleNamespace(
        notification_id="notif_done",
        notification_type="gate",
        user_email="user@example.com",
        conversation_id="conv-1",
        task_id="task_done",
        step_name="review",
        step_run_id=None,
        session_id="sess-2",
        payload={},
        status="pending",
        resolution=None,
        created_at=datetime.now(UTC),
        resolved_at=None,
    )
    tasks = {
        "task_active": _task_row("task_active", "paused"),
        "task_done": _task_row("task_done", "completed"),
    }

    async def _fake_get_task(session: Any, task_id: str) -> Any:
        return tasks.get(task_id)

    monkeypatch.setattr("cognis.core.notifications.get_task", _fake_get_task)

    service = NotificationService(
        session_factory=_FakeListSessionFactory([active_row, stale_row], tasks),
        pause_waiter=_FakePauseWaiter(),
        event_bus=_FakeEventBus(),
        providers=SimpleNamespace(guardrails=_FakeGuardrails()),
    )

    pending = await service.list_pending("user@example.com", conversation_id="conv-1")

    assert [notification.notification_id for notification in pending] == ["notif_active"]
    assert stale_row.status == "resolved"
    assert stale_row.resolution == {"decision": "cancel", "reason": "task_terminal"}


@pytest.mark.asyncio
async def test_list_pending_omits_and_orphans_expired_escalations() -> None:
    active_row = _notification_row()
    active_row.notification_id = "call-active"
    active_row.payload = {"timeout_seconds": 300}

    expired_row = _notification_row()
    expired_row.notification_id = "call-expired"
    expired_row.payload = {"timeout_seconds": 30}
    expired_row.created_at = datetime.now(UTC) - timedelta(seconds=60)

    service = NotificationService(
        session_factory=_FakeListSessionFactory([active_row, expired_row], {}),
        pause_waiter=_FakePauseWaiter(),
        event_bus=_FakeEventBus(),
        providers=SimpleNamespace(guardrails=_FakeGuardrails()),
    )

    pending = await service.list_pending("user@example.com", conversation_id="conv-1")

    assert [notification.notification_id for notification in pending] == ["call-active"]
    assert expired_row.status == "resolved"
    assert expired_row.resolution == {"decision": "cancel", "reason": "timeout"}


@pytest.mark.asyncio
async def test_list_pending_reconciles_externally_resolved_submitted_escalations() -> None:
    row = _notification_row()
    row.task_id = "task_live"
    row.resolution = {"decision": "approve", "state": "submitted", "note": "approved in intaris"}
    event_bus = _FakeEventBus()
    service = NotificationService(
        session_factory=_FakeListSessionFactory(
            [row], {"task_live": _task_row("task_live", "paused")}
        ),
        pause_waiter=_FakePauseWaiter(),
        event_bus=event_bus,
        providers=SimpleNamespace(
            guardrails=_FakeGuardrails(
                escalations={
                    "call-1": SimpleNamespace(
                        call_id="call-1",
                        resolved=True,
                        decision="approve",
                    )
                }
            )
        ),
    )

    pending = await service.list_pending("user@example.com", conversation_id="conv-1")

    assert pending == []
    assert row.status == "resolved"
    assert row.resolution["state"] == "resolved_remote"
    assert row.resolution["decision"] == "approve"
    assert event_bus.events[-1].data["notification_id"] == "call-1"


@pytest.mark.asyncio
async def test_reconcile_remote_escalation_uses_intaris_user_decision() -> None:
    row = _notification_row()
    event_bus = _FakeEventBus()
    service = NotificationService(
        session_factory=_FakeListSessionFactory([row], {}),
        pause_waiter=_FakePauseWaiter(),
        event_bus=event_bus,
        providers=SimpleNamespace(
            guardrails=_FakeGuardrails(
                escalations={
                    "call-1": SimpleNamespace(
                        call_id="call-1",
                        resolved=False,
                        decision="escalate",
                        user_decision="deny",
                        user_note="denied in Intaris",
                    )
                }
            )
        ),
    )

    resolved = await service.reconcile_remote_escalation("call-1")

    assert resolved is True
    assert row.status == "resolved"
    assert row.resolution["decision"] == "deny"
    assert row.resolution["note"] == "denied in Intaris"
    assert row.resolution["state"] == "resolved_remote"


@pytest.mark.asyncio
async def test_list_pending_keeps_remote_resolution_visible_when_waiter_is_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    row = _notification_row()
    row.task_id = "task_live"
    event_bus = _FakeEventBus()

    async def _fake_get_task(session: Any, task_id: str) -> Any:
        return _task_row(task_id, "paused")

    monkeypatch.setattr("cognis.core.notifications.get_task", _fake_get_task)

    service = NotificationService(
        session_factory=_FakeListSessionFactory(
            [row], {"task_live": _task_row("task_live", "paused")}
        ),
        pause_waiter=_FakePauseWaiter(should_resolve=False),
        event_bus=event_bus,
        providers=SimpleNamespace(
            guardrails=_FakeGuardrails(
                escalations={
                    "call-1": SimpleNamespace(
                        call_id="call-1",
                        resolved=True,
                        decision="approve",
                    )
                }
            )
        ),
    )

    pending = await service.list_pending("user@example.com", conversation_id="conv-1")

    assert [notification.notification_id for notification in pending] == ["call-1"]
    assert row.status == "pending"
    assert row.resolution["state"] == "submitted_remote"
    assert event_bus.events == []
