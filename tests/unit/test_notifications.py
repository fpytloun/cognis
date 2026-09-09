from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from time import monotonic
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from cognis.core.agent_loop import PauseWaiter, PendingPause
from cognis.core.conversation_state import _pending_summary
from cognis.core.notifications import (
    NotificationService,
    _find_direct_turn_owner,
    _user_interaction_display,
    safe_display_arguments,
)


def test_user_interaction_display_maps_question_options_to_safe_labels() -> None:
    display = _user_interaction_display(
        notification_type="step_question",
        notification_payload={
            "questions": [
                {
                    "id": "target",
                    "question": "Where should this deploy?",
                    "options": [{"id": "stage", "label": "Staging"}],
                }
            ]
        },
        resolution_data={
            "answers": [
                {
                    "question_id": "target",
                    "selected_option_ids": ["stage"],
                    "custom_answer": "Run smoke tests",
                }
            ]
        },
        decision="continue",
    )

    assert display == {
        "title": "You answered questions",
        "summary": None,
        "answers": [
            {
                "question": "Where should this deploy?",
                "answer": "Staging, Run smoke tests",
            }
        ],
        "status": "complete",
    }


def test_user_interaction_display_never_includes_credential_secret_payload() -> None:
    display = _user_interaction_display(
        notification_type="credential_request",
        notification_payload={"credential_id": "github_work", "kind": "token"},
        resolution_data={
            "credential_id": "github_work",
            "credential_kind": "token",
            "credential": {"token": "must-not-appear"},
        },
        decision="approve",
    )

    assert display["title"] == "You provided a credential"
    assert display["summary"] == "Created or updated credential `github_work` (token)."
    assert display["answers"] == []


def test_safe_display_arguments_preserves_action_details_and_redacts_secrets() -> None:
    display = safe_display_arguments(
        {
            "command": "uv run pytest tests/unit/test_notifications.py -q",
            "description": "Run notification tests",
            "env": {"API_TOKEN": "must-not-appear", "KEEP": "visible"},
        }
    )

    assert display == {
        "command": "uv run pytest tests/unit/test_notifications.py -q",
        "description": "Run notification tests",
        "env": {"API_TOKEN": "[redacted]", "KEEP": "visible"},
    }


def test_approved_escalation_includes_safe_arguments_and_past_tense_title() -> None:
    display = _user_interaction_display(
        notification_type="escalation",
        notification_payload={
            "tool_name": "bash",
            "arguments_display": {"command": "uv run pytest -q"},
            "reasoning": "The command can change repository state.",
            "risk": "medium",
        },
        resolution_data={},
        decision="approve",
    )

    assert display["title"] == "You approved the action"
    assert display["answers"] == [
        {"question": "Action", "answer": "bash"},
        {"question": "Arguments", "answer": '{\n  "command": "uv run pytest -q"\n}'},
        {"question": "Reason", "answer": "The command can change repository state."},
        {"question": "Risk", "answer": "medium"},
    ]


def test_canonical_pending_escalation_exposes_safe_prompt_fields() -> None:
    row = _notification_row()
    row.payload = {
        "call_id": "audit-call-1",
        "tool_call_id": "tool-call-1",
        "tool_name": "bash",
        "arguments_display": {"command": "uv run pytest -q"},
        "risk": "high",
        "reasoning": "The command can mutate the repository.",
        "timeout_seconds": 300,
    }

    summary = _pending_summary(row)

    assert summary.notification_id == "call-1"
    assert summary.call_id == "audit-call-1"
    assert summary.session_id == "sess-1"
    assert summary.tool_call_id == "tool-call-1"
    assert summary.tool_name == "bash"
    assert summary.arguments_display == {"command": "uv run pytest -q"}
    assert summary.risk == "high"
    assert summary.reasoning == "The command can mutate the repository."
    assert summary.timeout_seconds == 300


def test_canonical_pending_question_exposes_question_set_and_context() -> None:
    row = _notification_row()
    row.notification_type = "step_question"
    row.payload = {
        "questions": [
            {
                "id": "scope",
                "question": "Which scope?",
                "options": [{"id": "focused", "label": "Focused"}],
            }
        ],
        "context": {"context": "Choose the implementation scope."},
        "managed_conversation_title": "Research helper",
        "managed_target_agent_id": "lumi",
        "managed_origin_conversation_id": "conv-child",
    }

    summary = _pending_summary(row)

    assert summary.questions[0]["id"] == "scope"
    assert summary.context == {"context": "Choose the implementation scope."}
    assert summary.managed_conversation_title == "Research helper"
    assert summary.managed_target_agent_id == "lumi"
    assert summary.managed_origin_conversation_id == "conv-child"


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

    async def execute(self, statement: Any) -> Any:
        for key, value in getattr(statement, "_values", {}).items():
            attr = key.key if hasattr(key, "key") else str(key)
            if hasattr(value, "value"):
                value = value.value
            setattr(self._row, attr, value)
        return SimpleNamespace(rowcount=1)

    async def commit(self) -> None:
        return None

    async def rollback(self) -> None:
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


class _OrphanRaceSession(_FakeSession):
    def __init__(self, row: Any, *, concurrent_status: str, concurrent_resolution: dict[str, Any]):
        super().__init__(row)
        self._concurrent_status = concurrent_status
        self._concurrent_resolution = concurrent_resolution

    async def execute(self, statement: Any) -> Any:
        self._row.status = self._concurrent_status
        self._row.resolution = self._concurrent_resolution
        self._row.resolved_at = datetime.now(UTC)
        return SimpleNamespace(rowcount=0)


class _OrphanRaceSessionFactory:
    def __init__(self, row: Any, *, concurrent_status: str, concurrent_resolution: dict[str, Any]):
        self._row = row
        self._concurrent_status = concurrent_status
        self._concurrent_resolution = concurrent_resolution

    def __call__(self) -> _OrphanRaceSession:
        return _OrphanRaceSession(
            self._row,
            concurrent_status=self._concurrent_status,
            concurrent_resolution=self._concurrent_resolution,
        )


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
        expires_at=None,
        created_at=datetime.now(UTC),
        resolved_at=None,
    )


def _task_row(task_id: str, status: str) -> Any:
    return SimpleNamespace(task_id=task_id, status=status)


@pytest.mark.asyncio
async def test_internal_resolution_guard_rejects_stale_notification_mutation() -> None:
    row = _notification_row()
    event_bus = _FakeEventBus()
    pause_waiter = _FakePauseWaiter()
    service = NotificationService(
        session_factory=_FakeSessionFactory(row),
        pause_waiter=pause_waiter,
        event_bus=event_bus,
        providers=SimpleNamespace(guardrails=_FakeGuardrails()),
    )

    async def _stale(_session: Any) -> bool:
        return False

    resolved = await service.resolve_internal(
        "call-1",
        "completed",
        {"transaction_id": "txn-1"},
        admission_guard=_stale,
    )

    assert resolved is False
    assert row.status == "pending"
    assert row.resolution is None
    assert pause_waiter.order == []
    assert event_bus.events == []


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
async def test_question_resolution_bounds_slow_interaction_recording(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    row = _notification_row()
    row.notification_type = "step_question"
    event_bus = _FakeEventBus()
    service = NotificationService(
        session_factory=_FakeSessionFactory(row),
        pause_waiter=_FakePauseWaiter(),
        event_bus=event_bus,
        providers=SimpleNamespace(guardrails=_FakeGuardrails()),
    )

    async def _slow_record(**_: Any) -> None:
        await asyncio.Event().wait()

    monkeypatch.setattr(
        "cognis.core.notifications._INTERACTION_RECORD_TIMEOUT_SECONDS",
        0.01,
    )
    monkeypatch.setattr(service, "_record_user_interaction", _slow_record)

    started_at = asyncio.get_running_loop().time()
    resolved = await service.resolve(
        "call-1",
        "continue",
        {"response_payload": {"mode": "plain_text", "answers": []}},
        user_email="user@example.com",
    )

    assert resolved is True
    assert asyncio.get_running_loop().time() - started_at < 0.2
    assert len(event_bus.events) == 1
    assert event_bus.events[0].data["notification_id"] == "call-1"


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
async def test_cross_controller_resolution_wakes_db_poll_without_shared_waiter() -> None:
    row = _notification_row()
    owner_waiter = PauseWaiter()
    resolver_waiter = _FakePauseWaiter(should_resolve=False)
    owner = NotificationService(
        session_factory=_FakeSessionFactory(row),
        pause_waiter=owner_waiter,
        event_bus=_FakeEventBus(),
        providers=SimpleNamespace(guardrails=_FakeGuardrails()),
    )
    resolver = NotificationService(
        session_factory=_FakeSessionFactory(row),
        pause_waiter=resolver_waiter,
        event_bus=_FakeEventBus(),
        providers=SimpleNamespace(guardrails=_FakeGuardrails()),
    )

    wait_task = asyncio.create_task(
        owner.wait_for_resolution("call-1", timeout=2, poll_seconds=0.01)
    )
    await asyncio.sleep(0)
    assert await resolver.resolve(
        "call-1",
        "approve",
        {"note": "approved elsewhere"},
        user_email="user@example.com",
    )

    resolution = await wait_task
    assert resolution.decision == "approve"
    assert resolution.data["note"] == "approved elsewhere"


@pytest.mark.asyncio
async def test_question_resolution_returns_before_slow_cluster_and_interaction_mirrors() -> None:
    row = _notification_row()
    row.notification_type = "step_question"
    row.payload = {"origin_call_id": "tool-call-1"}
    release = asyncio.Event()

    class _SlowClusterSignals:
        async def publish(self, *_: Any, **__: Any) -> None:
            await release.wait()

    class _SlowGuardrails(_FakeGuardrails):
        async def record_events(self, **_: Any) -> Any:
            await release.wait()
            return SimpleNamespace(ok=True)

    service = NotificationService(
        session_factory=_FakeSessionFactory(row),
        pause_waiter=_FakePauseWaiter(),
        event_bus=_FakeEventBus(),
        providers=SimpleNamespace(guardrails=_SlowGuardrails()),
    )
    service.cluster_signals = _SlowClusterSignals()

    started = monotonic()
    assert await service.resolve(
        "call-1",
        "continue",
        {"mode": "structured", "answers": []},
        user_email="user@example.com",
    )
    elapsed = monotonic() - started

    assert elapsed < 0.1
    assert len(service._background_tasks) == 2
    release.set()
    await asyncio.gather(*service._background_tasks)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("concurrent_status", "concurrent_resolution"),
    [
        (
            "resolving",
            {
                "decision": "continue",
                "state": "submitting",
                "claim_token": "resolve_claim",
                "answers": [],
            },
        ),
        (
            "resolved",
            {
                "decision": "continue",
                "state": "resolved",
                "answers": [{"question_id": "scope", "selected_option_ids": ["focused"]}],
            },
        ),
    ],
)
async def test_mark_orphaned_loses_cas_to_concurrent_answer_without_overwrite(
    concurrent_status: str,
    concurrent_resolution: dict[str, Any],
) -> None:
    row = _notification_row()
    row.notification_type = "step_question"
    row.status = "pending"
    event_bus = _FakeEventBus()
    service = NotificationService(
        session_factory=_OrphanRaceSessionFactory(
            row,
            concurrent_status=concurrent_status,
            concurrent_resolution=concurrent_resolution,
        ),
        pause_waiter=_FakePauseWaiter(),
        event_bus=event_bus,
        providers=SimpleNamespace(guardrails=_FakeGuardrails()),
    )

    assert not await service.mark_orphaned(
        row.notification_id,
        reason="direct_turn_recovery_timeout",
    )
    assert row.status == concurrent_status
    assert row.resolution == concurrent_resolution
    assert event_bus.events == []


@pytest.mark.asyncio
async def test_wait_for_resolution_gives_fresh_claim_completion_grace(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    row = _notification_row()
    row.status = "resolving"
    row.resolution = {"decision": "approve", "state": "submitting"}
    row.resolved_at = datetime.now(UTC)
    waiter = PauseWaiter()
    waiter.register(
        PendingPause(
            pause_id="call-1",
            pause_type="escalation",
        )
    )
    service = NotificationService(
        session_factory=_FakeSessionFactory(row),
        pause_waiter=waiter,
        event_bus=_FakeEventBus(),
        providers=SimpleNamespace(guardrails=_FakeGuardrails()),
    )
    monkeypatch.setattr(
        "cognis.core.notifications._RESOLUTION_CLAIM_SECONDS",
        0.1,
    )
    monkeypatch.setattr(
        "cognis.core.notifications._RESOLUTION_COMPLETION_GRACE_SECONDS",
        0.05,
    )

    async def _finish() -> None:
        await asyncio.sleep(0.02)
        row.status = "resolved"
        row.resolution = {"decision": "approve", "note": "near deadline"}

    asyncio.create_task(_finish())
    resolution = await service.wait_for_resolution(
        "call-1",
        timeout=0.01,
        poll_seconds=0.005,
    )

    assert resolution.decision == "approve"
    assert resolution.data["note"] == "near deadline"


@pytest.mark.asyncio
async def test_stale_resolving_claim_can_retry_submission() -> None:
    row = _notification_row()
    row.status = "resolving"
    row.resolution = {"decision": "approve", "state": "submitting"}
    row.resolved_at = datetime.now(UTC) - timedelta(seconds=60)
    order: list[str] = []
    service = NotificationService(
        session_factory=_FakeSessionFactory(row),
        pause_waiter=_FakePauseWaiter(order=order),
        event_bus=_FakeEventBus(),
        providers=SimpleNamespace(guardrails=_FakeGuardrails(order=order)),
    )

    assert await service.resolve(
        "call-1",
        "approve",
        {"note": "retry"},
        user_email="user@example.com",
    )
    assert order == ["submit", "resolve"]
    assert row.status == "resolved"
    assert row.resolution["note"] == "retry"


@pytest.mark.asyncio
async def test_stale_quick_action_claim_does_not_repeat_side_effect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    row = _notification_row()
    row.status = "resolving"
    row.resolution = {
        "decision": "approve",
        "state": "submitting",
        "claim_token": "owner",
        "submission_id": "submission-1",
    }
    row.resolved_at = datetime.now(UTC) - timedelta(seconds=60)
    order: list[str] = []
    service = NotificationService(
        session_factory=_FakeSessionFactory(row),
        pause_waiter=_FakePauseWaiter(order=order),
        event_bus=_FakeEventBus(),
        providers=SimpleNamespace(guardrails=_FakeGuardrails(order=order)),
    )
    monkeypatch.setattr("cognis.core.notifications._RESOLUTION_POLL_SECONDS", 0.005)
    side_effects = {"submission-1"}
    factory_calls = 0

    async def _side_effect() -> dict[str, str]:
        nonlocal factory_calls
        factory_calls += 1
        side_effects.add("submission-1")
        return {"credential_id": "credential-1"}

    resolved = await service.resolve(
        "call-1",
        "approve",
        {"submission_id": "submission-1"},
        user_email="user@example.com",
        data_factory=_side_effect,
    )

    assert resolved is True
    assert factory_calls == 1
    assert side_effects == {"submission-1"}
    assert row.status == "resolved"


@pytest.mark.asyncio
async def test_same_decision_waits_for_claim_owner_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    row = _notification_row()
    row.status = "resolving"
    row.resolution = {
        "decision": "approve",
        "state": "submitting",
        "claim_token": "owner",
        "note": "same request",
    }
    row.resolved_at = datetime.now(UTC)
    order: list[str] = []
    service = NotificationService(
        session_factory=_FakeSessionFactory(row),
        pause_waiter=_FakePauseWaiter(order=order),
        event_bus=_FakeEventBus(),
        providers=SimpleNamespace(guardrails=_FakeGuardrails(order=order)),
    )
    monkeypatch.setattr("cognis.core.notifications._RESOLUTION_CLAIM_SECONDS", 0.1)
    monkeypatch.setattr("cognis.core.notifications._RESOLUTION_POLL_SECONDS", 0.005)

    async def _fail_owner_submission() -> None:
        await asyncio.sleep(0.02)
        row.status = "pending"
        row.resolution = None
        row.resolved_at = None

    owner = asyncio.create_task(_fail_owner_submission())
    resolved = await service.resolve(
        "call-1",
        "approve",
        {"note": "same request"},
        user_email="user@example.com",
    )
    await owner

    assert resolved is False
    assert row.status == "pending"
    assert order == []


@pytest.mark.asyncio
async def test_same_decision_waits_for_authoritative_terminal_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    row = _notification_row()
    row.status = "resolving"
    row.resolution = {
        "decision": "approve",
        "state": "submitting",
        "claim_token": "owner",
        "note": "same request",
    }
    row.resolved_at = datetime.now(UTC)
    order: list[str] = []
    service = NotificationService(
        session_factory=_FakeSessionFactory(row),
        pause_waiter=_FakePauseWaiter(order=order),
        event_bus=_FakeEventBus(),
        providers=SimpleNamespace(guardrails=_FakeGuardrails(order=order)),
    )
    monkeypatch.setattr("cognis.core.notifications._RESOLUTION_CLAIM_SECONDS", 0.1)
    monkeypatch.setattr("cognis.core.notifications._RESOLUTION_POLL_SECONDS", 0.005)

    async def _finish_owner_submission() -> None:
        await asyncio.sleep(0.02)
        row.status = "resolved"
        row.resolution = {
            "decision": "approve",
            "state": "resolved",
            "note": "same request",
        }

    owner = asyncio.create_task(_finish_owner_submission())
    resolved = await service.resolve(
        "call-1",
        "approve",
        {"note": "same request"},
        user_email="user@example.com",
    )
    await owner

    assert resolved is True
    assert row.status == "resolved"
    assert order == ["resolve"]


class _NoCasSession(_FakeSession):
    async def execute(self, statement: Any) -> Any:
        return SimpleNamespace(rowcount=0)


class _NoCasSessionFactory:
    def __init__(self, row: Any) -> None:
        self._row = row

    def __call__(self) -> _NoCasSession:
        return _NoCasSession(self._row)


@pytest.mark.asyncio
async def test_timeout_requires_successful_authoritative_cas() -> None:
    row = _notification_row()
    row.status = "resolving"
    row.resolution = {"decision": "approve", "state": "submitting"}
    row.resolved_at = datetime.now(UTC)
    service = NotificationService(
        session_factory=_NoCasSessionFactory(row),
        pause_waiter=_FakePauseWaiter(),
        event_bus=_FakeEventBus(),
        providers=SimpleNamespace(guardrails=_FakeGuardrails()),
    )

    resolution = await service.resolve_timeout("call-1")

    assert resolution is None
    assert row.status == "resolving"
    assert row.resolution["decision"] == "approve"


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
        expires_at=None,
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
        expires_at=None,
        created_at=datetime.now(UTC),
        resolved_at=None,
    )
    callback_row = SimpleNamespace(
        notification_id="notif_oauth",
        notification_type="auth_challenge",
        user_email="user@example.com",
        conversation_id="conv-1",
        task_id="task_done",
        step_name="review",
        step_run_id=None,
        session_id="sess-3",
        payload={
            "kind": "oauth_authorization",
            "metadata": {"callback_only": True},
        },
        status="pending",
        resolution=None,
        expires_at=None,
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
        session_factory=_FakeListSessionFactory([active_row, stale_row, callback_row], tasks),
        pause_waiter=_FakePauseWaiter(),
        event_bus=_FakeEventBus(),
        providers=SimpleNamespace(guardrails=_FakeGuardrails()),
    )

    pending = await service.list_pending("user@example.com", conversation_id="conv-1")

    assert [notification.notification_id for notification in pending] == [
        "notif_active",
        "notif_oauth",
    ]
    assert stale_row.status == "resolved"
    assert stale_row.resolution == {"decision": "cancel", "reason": "task_terminal"}
    assert callback_row.status == "pending"
    assert callback_row.resolution is None


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
    assert expired_row.resolution == {
        "decision": "deny",
        "reason": "timeout",
        "state": "timed_out",
    }


@pytest.mark.asyncio
async def test_reconcile_preserves_callback_only_oauth_for_terminal_task(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    row = _notification_row()
    row.notification_type = "auth_challenge"
    row.task_id = "task_done"
    row.payload = {
        "kind": "oauth_authorization",
        "metadata": {"callback_only": True},
    }
    tasks = {"task_done": _task_row("task_done", "completed")}

    async def _fake_get_task(session: Any, task_id: str) -> Any:
        return tasks.get(task_id)

    monkeypatch.setattr("cognis.core.notifications.get_task", _fake_get_task)
    pause_waiter = PauseWaiter()
    pause_waiter.register(
        PendingPause(
            pause_id=row.notification_id,
            pause_type=row.notification_type,
            task_id=row.task_id,
            conversation_id=row.conversation_id,
        )
    )
    service = NotificationService(
        session_factory=_FakeListSessionFactory([row], tasks),
        pause_waiter=pause_waiter,
        event_bus=_FakeEventBus(),
        providers=SimpleNamespace(guardrails=_FakeGuardrails()),
    )

    reconciled = await service.reconcile_pending()

    assert reconciled == 0
    assert pause_waiter.get(row.notification_id) is None
    assert row.status == "pending"
    assert row.resolution is None


@pytest.mark.asyncio
async def test_terminal_task_cleanup_preserves_callback_only_oauth() -> None:
    callback_row = _notification_row()
    callback_row.notification_type = "auth_challenge"
    callback_row.task_id = "task_done"
    callback_row.payload = {
        "kind": "oauth_authorization",
        "metadata": {"callback_only": True},
    }
    interactive_row = _notification_row()
    interactive_row.notification_id = "otp-1"
    interactive_row.notification_type = "auth_challenge"
    interactive_row.task_id = "task_done"
    interactive_row.payload = {"kind": "otp_code", "required_fields": ["code"]}
    pause_waiter = PauseWaiter()
    for row in (callback_row, interactive_row):
        pause_waiter.register(
            PendingPause(
                pause_id=row.notification_id,
                pause_type=row.notification_type,
                task_id=row.task_id,
                conversation_id=row.conversation_id,
            )
        )
    service = NotificationService(
        session_factory=_FakeListSessionFactory([callback_row, interactive_row], {}),
        pause_waiter=pause_waiter,
        event_bus=_FakeEventBus(),
        providers=SimpleNamespace(guardrails=_FakeGuardrails()),
    )
    service.mark_orphaned = AsyncMock(return_value=True)  # type: ignore[method-assign]

    resolved = await service.mark_task_notifications_terminal(
        "task_done",
        reason="task_terminal",
    )

    assert resolved == 1
    assert pause_waiter.get(callback_row.notification_id) is None
    service.mark_orphaned.assert_awaited_once_with(
        interactive_row.notification_id,
        reason="task_terminal",
    )


@pytest.mark.asyncio
async def test_bounded_intaris_timeout_keeps_resolution_claim_for_recovery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    row = _notification_row()

    class _SlowGuardrails(_FakeGuardrails):
        async def submit_decision(
            self, call_id: str, decision: str, note: str | None = None
        ) -> None:
            await asyncio.sleep(1)

    monkeypatch.setattr(
        "cognis.core.notifications._INTARIS_SUBMISSION_TIMEOUT_SECONDS",
        0.01,
    )
    service = NotificationService(
        session_factory=_FakeSessionFactory(row),
        pause_waiter=_FakePauseWaiter(),
        event_bus=_FakeEventBus(),
        providers=SimpleNamespace(guardrails=_SlowGuardrails()),
    )

    assert not await service.resolve(
        "call-1",
        "approve",
        {"note": "bounded"},
        user_email="user@example.com",
    )
    assert row.status == "resolving"
    assert row.resolution["decision"] == "approve"
    assert row.resolution["state"] == "submitting"


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
async def test_list_pending_persists_remote_resolution_when_waiter_is_missing(
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

    assert pending == []
    assert row.status == "resolved"
    assert row.resolution["state"] == "resolved_remote"
    assert event_bus.events[-1].data["notification_id"] == "call-1"


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
        link_id=f"link-{target}",
        target_conversation_id=target,
        controller_conversation_id=controller,
        title=title,
        target_agent_id=target_agent_id,
    )


@pytest.mark.asyncio
async def test_resolve_agent_task_same_conversation_uses_source_ref(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Agent-created task prompts return to the originating Matrix conversation."""

    async def _fake_get_task(session: Any, task_id: str) -> Any:
        return SimpleNamespace(
            task_id=task_id,
            delivery_mode="same_conversation",
            source_type="agent",
            source_ref="conv-matrix-main",
            created_by="user@example.com",
            agent_id="riker",
            delivery_target=None,
        )

    monkeypatch.setattr("cognis.core.notifications.get_task", _fake_get_task)
    service = NotificationService(
        session_factory=_ManagedLinkSessionFactory({}),
        pause_waiter=_FakePauseWaiter(),
        event_bus=_FakeEventBus(),
        providers=SimpleNamespace(guardrails=_FakeGuardrails()),
    )

    result = await service.resolve_target_conversation("task-agent", "conv-task-internal")

    assert result == "conv-matrix-main"


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
async def test_resolve_target_conversation_promotes_delegated_session_to_parent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A child with a distinct conversation still targets its root parent chat."""
    sessions = {
        "sess-child": SimpleNamespace(
            session_id="sess-child",
            conversation_id="conv-child",
            parent_session_id="sess-parent",
            user_email="user@example.com",
        ),
        "sess-parent": SimpleNamespace(
            session_id="sess-parent",
            conversation_id="conv-parent",
            parent_session_id=None,
            user_email="user@example.com",
        ),
    }

    async def _fake_session(session: Any, session_id: str, **_: Any) -> Any:
        return sessions.get(session_id)

    async def _fake_link(session: Any, target_id: str, **_: Any) -> Any:
        return None

    monkeypatch.setattr("cognis.core.notifications.get_session_row", _fake_session)
    monkeypatch.setattr(
        "cognis.core.notifications.get_managed_conversation_link_for_target", _fake_link
    )

    service = NotificationService(
        session_factory=_ManagedLinkSessionFactory({}),
        pause_waiter=_FakePauseWaiter(),
        event_bus=_FakeEventBus(),
        providers=SimpleNamespace(guardrails=_FakeGuardrails()),
    )

    result = await service.resolve_target_conversation(
        None,
        "conv-child",
        session_id="sess-child",
        user_email="user@example.com",
    )

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
async def test_create_promotes_delegated_session_through_managed_parent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A delegated escalation reaches the parent chat with managed context."""
    links = {"conv-managed": _managed_link("conv-managed", "conv-parent")}
    sessions = {
        "sess-child": SimpleNamespace(
            session_id="sess-child",
            conversation_id="conv-child",
            parent_session_id="sess-managed",
            user_email="user@example.com",
        ),
        "sess-managed": SimpleNamespace(
            session_id="sess-managed",
            conversation_id="conv-managed",
            parent_session_id=None,
            user_email="user@example.com",
        ),
    }

    async def _fake_link(session: Any, target_id: str, **_: Any) -> Any:
        return links.get(target_id)

    async def _fake_session(session: Any, session_id: str, **_: Any) -> Any:
        return sessions.get(session_id)

    monkeypatch.setattr(
        "cognis.core.notifications.get_managed_conversation_link_for_target", _fake_link
    )
    monkeypatch.setattr("cognis.core.notifications.get_session_row", _fake_session)

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
    await asyncio.gather(*tuple(service._background_tasks))
    assert event_bus.events[0].data["conversation_id"] == "conv-parent"

    # Managed-origin metadata must be in the enriched payload
    assert add_session.added[0].payload.get("managed_conversation_title") == "Sub-task"
    assert add_session.added[0].payload.get("managed_target_agent_id") == "agent-sub"
    assert add_session.added[0].payload.get("managed_origin_conversation_id") == "conv-managed"
    assert add_session.added[0].payload.get("managed_link_id") == "link-conv-managed"


def test_direct_question_owner_requires_matching_durable_tool_descriptor() -> None:
    notification = SimpleNamespace(
        payload={"origin_call_id": "tool-call-1"},
    )
    live_owner = SimpleNamespace(
        status="running",
        outcome={"tool_calls": [{"call_id": "tool-call-1"}]},
    )
    unrelated = SimpleNamespace(
        status="failed",
        outcome={"tool_calls": [{"call_id": "tool-call-2"}]},
    )

    assert _find_direct_turn_owner(notification, [unrelated, live_owner]) is live_owner
    assert _find_direct_turn_owner(notification, [unrelated]) is None


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
