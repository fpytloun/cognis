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
        on_submit: Any | None = None,
    ) -> None:
        self.fail = fail
        self.order = order if order is not None else []
        self.escalations = escalations or {}
        self.on_submit = on_submit

    async def submit_decision(self, call_id: str, decision: str, note: str | None = None) -> None:
        self.order.append("submit")
        if self.fail:
            raise RuntimeError("submit failed")
        if self.on_submit is not None:
            self.on_submit(call_id, decision, note)

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
async def test_escalation_resolution_is_idempotent_for_same_terminal_decision() -> None:
    row = _notification_row()
    row.status = "resolved"
    row.resolution = {"decision": "approve", "state": "resolved_remote"}
    order: list[str] = []
    event_bus = _FakeEventBus()
    service = NotificationService(
        session_factory=_FakeSessionFactory(row),
        pause_waiter=_FakePauseWaiter(order=order),
        event_bus=event_bus,
        providers=SimpleNamespace(guardrails=_FakeGuardrails(order=order)),
    )

    resolved = await service.resolve(
        "call-1",
        "approve",
        {"note": "safe"},
        user_email="user@example.com",
    )

    assert resolved is True
    assert order == ["resolve"]
    assert event_bus.events == []


@pytest.mark.asyncio
async def test_escalation_resolution_accepts_concurrent_remote_reconciliation() -> None:
    row = _notification_row()
    order: list[str] = []
    event_bus = _FakeEventBus()

    def _resolve_remotely(_: str, decision: str, __: str | None) -> None:
        row.status = "resolved"
        row.resolution = {"decision": decision, "state": "resolved_remote"}
        row.resolved_at = datetime.now(UTC)

    service = NotificationService(
        session_factory=_FakeSessionFactory(row),
        pause_waiter=_FakePauseWaiter(should_resolve=False, order=order),
        event_bus=event_bus,
        providers=SimpleNamespace(
            guardrails=_FakeGuardrails(order=order, on_submit=_resolve_remotely)
        ),
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
    assert row.resolution["state"] == "resolved_remote"
    assert event_bus.events == []


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
    assert event_bus.events[-1].data["user_email"] == "user@example.com"
    assert event_bus.events[-1].data["conversation_id"] == "conv-1"
    assert event_bus.events[-1].data["task_id"] == "task_live"
    assert event_bus.events[-1].data["session_id"] == "sess-1"


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
    assert event_bus.events[-1].data["user_email"] == "user@example.com"
    assert event_bus.events[-1].data["conversation_id"] == "conv-1"
    assert event_bus.events[-1].data["session_id"] == "sess-1"


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


# ---------------------------------------------------------------------------
# Managed-conversation chain resolution
# ---------------------------------------------------------------------------


class _ManagedLinkSession:
    """Fake DB session that resolves ManagedConversationLink lookups."""

    def __init__(self, links: dict[str, Any]) -> None:
        # links: {target_conversation_id: SimpleNamespace(controller_conversation_id=..., ...)}
        self._links = links

    async def __aenter__(self) -> _ManagedLinkSession:
        return self

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        return None

    async def execute(self, statement: Any) -> _FakeQueryResult:
        # Extract the target_conversation_id from the WHERE clause
        target_id: str | None = None
        for criterion in getattr(statement, "_where_criteria", ()):
            right = getattr(criterion, "right", None)
            if hasattr(right, "value"):
                target_id = right.value
                break
        if target_id is not None and target_id in self._links:
            return _FakeQueryResult([self._links[target_id]])
        return _FakeQueryResult([])

    async def get(self, model: Any, key: str) -> Any:
        return None

    async def commit(self) -> None:
        return None


class _ManagedLinkSessionFactory:
    def __init__(self, links: dict[str, Any]) -> None:
        self._links = links

    def __call__(self) -> _ManagedLinkSession:
        return _ManagedLinkSession(self._links)


def _managed_link(
    target: str,
    controller: str,
    *,
    title: str = "Sub-task",
    target_agent_id: str = "agent-sub",
) -> Any:
    return SimpleNamespace(
        target_conversation_id=target,
        controller_conversation_id=controller,
        title=title,
        target_agent_id=target_agent_id,
    )


@pytest.mark.asyncio
async def test_resolve_target_conversation_redirects_managed_child_to_parent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A notification on a managed child conversation is redirected to the parent."""
    links = {"conv-child": _managed_link("conv-child", "conv-parent")}

    async def _fake_link(session: Any, target_id: str, **_: Any) -> Any:
        return links.get(target_id)

    monkeypatch.setattr(
        "cognis.core.notifications.get_managed_conversation_link_for_target", _fake_link
    )

    service = NotificationService(
        session_factory=_ManagedLinkSessionFactory(links),
        pause_waiter=_FakePauseWaiter(),
        event_bus=_FakeEventBus(),
        providers=SimpleNamespace(guardrails=_FakeGuardrails()),
    )

    result = await service.resolve_target_conversation(None, "conv-child")

    assert result == "conv-parent"


@pytest.mark.asyncio
async def test_resolve_target_conversation_walks_multi_hop_managed_chain(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Three-level chain: delegate conv == managed conv → managed conv → parent conv."""
    links = {
        "conv-managed": _managed_link("conv-managed", "conv-parent"),
    }

    async def _fake_link(session: Any, target_id: str, **_: Any) -> Any:
        return links.get(target_id)

    monkeypatch.setattr(
        "cognis.core.notifications.get_managed_conversation_link_for_target", _fake_link
    )

    service = NotificationService(
        session_factory=_ManagedLinkSessionFactory(links),
        pause_waiter=_FakePauseWaiter(),
        event_bus=_FakeEventBus(),
        providers=SimpleNamespace(guardrails=_FakeGuardrails()),
    )

    # Delegate shares conversation_id with the managed conversation
    result = await service.resolve_target_conversation(None, "conv-managed")

    assert result == "conv-parent"


@pytest.mark.asyncio
async def test_resolve_target_conversation_walks_nested_managed_chain(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Nested managed conversations: child → mid → parent."""
    links = {
        "conv-child": _managed_link("conv-child", "conv-mid"),
        "conv-mid": _managed_link("conv-mid", "conv-parent"),
    }

    async def _fake_link(session: Any, target_id: str, **_: Any) -> Any:
        return links.get(target_id)

    monkeypatch.setattr(
        "cognis.core.notifications.get_managed_conversation_link_for_target", _fake_link
    )

    service = NotificationService(
        session_factory=_ManagedLinkSessionFactory(links),
        pause_waiter=_FakePauseWaiter(),
        event_bus=_FakeEventBus(),
        providers=SimpleNamespace(guardrails=_FakeGuardrails()),
    )

    result = await service.resolve_target_conversation(None, "conv-child")

    assert result == "conv-parent"


@pytest.mark.asyncio
async def test_resolve_target_conversation_cycle_guard(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A cycle in managed links does not loop forever; returns last safe candidate."""
    links = {
        "conv-a": _managed_link("conv-a", "conv-b"),
        "conv-b": _managed_link("conv-b", "conv-a"),
    }

    async def _fake_link(session: Any, target_id: str, **_: Any) -> Any:
        return links.get(target_id)

    monkeypatch.setattr(
        "cognis.core.notifications.get_managed_conversation_link_for_target", _fake_link
    )

    service = NotificationService(
        session_factory=_ManagedLinkSessionFactory(links),
        pause_waiter=_FakePauseWaiter(),
        event_bus=_FakeEventBus(),
        providers=SimpleNamespace(guardrails=_FakeGuardrails()),
    )

    # Should not raise; returns the last candidate before cycle was detected
    result = await service.resolve_target_conversation(None, "conv-a")
    assert result in {"conv-a", "conv-b"}


@pytest.mark.asyncio
async def test_resolve_target_conversation_unchanged_for_direct_chat(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A direct-chat conversation with no managed link is returned unchanged."""

    async def _fake_link(session: Any, target_id: str, **_: Any) -> Any:
        return None

    monkeypatch.setattr(
        "cognis.core.notifications.get_managed_conversation_link_for_target", _fake_link
    )

    service = NotificationService(
        session_factory=_ManagedLinkSessionFactory({}),
        pause_waiter=_FakePauseWaiter(),
        event_bus=_FakeEventBus(),
        providers=SimpleNamespace(guardrails=_FakeGuardrails()),
    )

    result = await service.resolve_target_conversation(None, "conv-direct")

    assert result == "conv-direct"


@pytest.mark.asyncio
async def test_create_registers_pause_under_parent_conversation_for_managed_child(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Escalation created in a managed child registers PauseWaiter under the parent."""
    links = {"conv-child": _managed_link("conv-child", "conv-parent")}

    async def _fake_link(session: Any, target_id: str, **_: Any) -> Any:
        return links.get(target_id)

    monkeypatch.setattr(
        "cognis.core.notifications.get_managed_conversation_link_for_target", _fake_link
    )

    registered: list[Any] = []

    class _CapturingPauseWaiter(_FakePauseWaiter):
        def register(self, pending: Any) -> None:
            registered.append(pending)

    event_bus = _FakeEventBus()

    class _AddSession:
        def __init__(self) -> None:
            self.added: list[Any] = []

        async def __aenter__(self) -> _AddSession:
            return self

        async def __aexit__(self, *_: Any) -> None:
            return None

        def add(self, row: Any) -> None:
            self.added.append(row)

        async def commit(self) -> None:
            return None

    add_session = _AddSession()

    def _session_factory() -> _AddSession:
        return add_session

    service = NotificationService(
        session_factory=_session_factory,
        pause_waiter=_CapturingPauseWaiter(),
        event_bus=event_bus,
        providers=SimpleNamespace(guardrails=_FakeGuardrails()),
    )

    await service.create(
        notification_type="escalation",
        user_email="user@example.com",
        conversation_id="conv-child",
        session_id="sess-child",
        notification_id="call-esc-1",
        payload={
            "call_id": "call-esc-1",
            "tool_name": "bash",
            "risk": "medium",
            "reasoning": "runs shell",
            "timeout_seconds": 300,
        },
    )

    assert len(registered) == 1
    pause = registered[0]
    # PauseWaiter must be registered under the parent conversation
    assert pause.conversation_id == "conv-parent"
    # Child session_id is preserved for resume
    assert pause.session_id == "sess-child"

    # DB row and event must also use the parent conversation
    assert add_session.added[0].conversation_id == "conv-parent"
    assert event_bus.events[0].data["conversation_id"] == "conv-parent"

    # Managed-origin metadata must be in the enriched payload
    assert add_session.added[0].payload.get("managed_conversation_title") == "Sub-task"
    assert add_session.added[0].payload.get("managed_target_agent_id") == "agent-sub"


@pytest.mark.asyncio
async def test_resolve_target_conversation_hop_cap_stops_at_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An acyclic chain longer than the hop cap is truncated at the cap."""
    # Build a chain of 15 hops (cap is 10)
    chain: dict[str, Any] = {}
    for i in range(15):
        chain[f"conv-{i}"] = _managed_link(f"conv-{i}", f"conv-{i + 1}")

    async def _fake_link(session: Any, target_id: str, **_: Any) -> Any:
        return chain.get(target_id)

    monkeypatch.setattr(
        "cognis.core.notifications.get_managed_conversation_link_for_target", _fake_link
    )

    service = NotificationService(
        session_factory=_ManagedLinkSessionFactory(chain),
        pause_waiter=_FakePauseWaiter(),
        event_bus=_FakeEventBus(),
        providers=SimpleNamespace(guardrails=_FakeGuardrails()),
    )

    result = await service.resolve_target_conversation(None, "conv-0")

    # Must stop at hop 10 (conv-10), not reach conv-15
    assert result == "conv-10"


@pytest.mark.asyncio
async def test_create_task_originated_notification_not_managed_enriched(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Task-originated notifications skip managed-link enrichment entirely."""
    links = {"conv-child": _managed_link("conv-child", "conv-parent")}

    async def _fake_link(session: Any, target_id: str, **_: Any) -> Any:
        return links.get(target_id)

    monkeypatch.setattr(
        "cognis.core.notifications.get_managed_conversation_link_for_target", _fake_link
    )

    async def _fake_get_task(session: Any, task_id: str) -> Any:
        return SimpleNamespace(
            task_id=task_id,
            delivery_mode="same_conversation",
            source_type="chat",
            source_ref="conv-source",
            created_by="user@example.com",
            agent_id="agent-1",
            delivery_target=None,
        )

    monkeypatch.setattr("cognis.core.notifications.get_task", _fake_get_task)

    registered: list[Any] = []

    class _CapturingPauseWaiter(_FakePauseWaiter):
        def register(self, pending: Any) -> None:
            registered.append(pending)

    event_bus = _FakeEventBus()

    class _AddSession:
        def __init__(self) -> None:
            self.added: list[Any] = []

        async def __aenter__(self) -> _AddSession:
            return self

        async def __aexit__(self, *_: Any) -> None:
            return None

        def add(self, row: Any) -> None:
            self.added.append(row)

        async def commit(self) -> None:
            return None

    add_session = _AddSession()

    def _session_factory() -> _AddSession:
        return add_session

    service = NotificationService(
        session_factory=_session_factory,
        pause_waiter=_CapturingPauseWaiter(),
        event_bus=event_bus,
        providers=SimpleNamespace(guardrails=_FakeGuardrails()),
    )

    await service.create(
        notification_type="gate",
        user_email="user@example.com",
        conversation_id="conv-child",
        task_id="task-1",
        notification_id="gate-1",
        payload={"message": "approve step?"},
    )

    assert len(registered) == 1
    pause = registered[0]
    # Task delivery redirects to source conversation, not managed parent
    assert pause.conversation_id == "conv-source"
    # No managed-origin metadata injected for task-originated notifications
    assert "managed_conversation_title" not in add_session.added[0].payload
    assert "managed_target_agent_id" not in add_session.added[0].payload
