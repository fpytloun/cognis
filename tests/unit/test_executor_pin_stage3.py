from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from cognis.core.executor_pin_lifecycle import (
    ensure_active_executor_pin,
    normalize_active_executor_source,
)
from cognis.core.executor_pool import ExecutorAvailability, ExecutorPool, ResolvedExecutorTarget


@dataclass
class _Row:
    executor_id: str


def _target(
    executor_id: str, *, source: str = "selector", primary: bool = True
) -> ResolvedExecutorTarget:
    return ResolvedExecutorTarget(
        executor_id=executor_id,
        executor_type="websocket",
        is_primary=primary,
        selection_source=source,
        description=None,
        state=ExecutorAvailability.USABLE,
        row=_Row(executor_id),
    )


class _Connection:
    def __init__(self, present: bool) -> None:
        self.present = present


class _Provider:
    def __init__(self, present: bool | dict[str, bool]) -> None:
        self.present = present

    def get_connection(self, executor_id: str) -> _Connection | None:
        present = (
            self.present.get(executor_id, False) if isinstance(self.present, dict) else self.present
        )
        return _Connection(True) if present else None


class _Session:
    async def __aenter__(self) -> _Session:
        return self

    async def __aexit__(self, *_args: Any) -> None:
        return None

    async def commit(self) -> None:
        return None


@pytest.mark.asyncio
async def test_selector_failover_has_one_cas_winner(monkeypatch: pytest.MonkeyPatch) -> None:
    from cognis.store import queries

    calls = 0

    async def cas(*args: Any, **kwargs: Any) -> tuple[bool, int, str | None]:
        nonlocal calls
        calls += 1
        return (calls == 1, 2, "notice") if calls <= 2 else (False, 2, None)

    monkeypatch.setattr(queries, "cas_executor_failover", cas)
    pool = ExecutorPool(primary=[_target("old"), _target("new")])
    provider = _Provider({"old": False, "new": True})
    kwargs = dict(
        session_factory=_Session,
        conversation_id="conv",
        task_id=None,
        pool=pool,
        active_executor_id="old",
        active_executor_expires_at=datetime.now(UTC),
        active_executor_generation=1,
        active_executor_source="selector_primary",
        active_executor_unavailable_since=datetime.now(UTC) - timedelta(seconds=10),
        ws_provider=provider,
        retry_seconds=0,
        retry_interval_seconds=1,
    )
    results = await asyncio.gather(
        ensure_active_executor_pin(**kwargs),
        ensure_active_executor_pin(**kwargs),
    )
    assert calls == 2
    assert {result.active_executor_id for result in results} == {"old", "new"}


@pytest.mark.asyncio
async def test_selector_primary_reconnect_grace_preserves_same_pin() -> None:
    pool = ExecutorPool(primary=[_target("same"), _target("other")])
    result = await ensure_active_executor_pin(
        session_factory=_Session,
        conversation_id="conv",
        task_id=None,
        pool=pool,
        active_executor_id="same",
        active_executor_expires_at=None,
        active_executor_generation=1,
        active_executor_source="selector_primary",
        ws_provider=_Provider(True),
        retry_seconds=0,
    )
    assert result.active_executor_id == "same"
    assert result.notice is None


@pytest.mark.asyncio
async def test_explicit_primary_and_additional_are_not_selector_failover_targets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from cognis.store import queries

    async def should_not_run(*args: Any, **kwargs: Any) -> tuple[bool, int, str | None]:
        raise AssertionError("unexpected automatic failover")

    monkeypatch.setattr(queries, "cas_executor_failover", should_not_run)
    target = _target("explicit", source="explicit")
    pool = ExecutorPool(primary=[target])
    result = await ensure_active_executor_pin(
        session_factory=_Session,
        conversation_id="conv",
        task_id=None,
        pool=pool,
        active_executor_id=target.executor_id,
        active_executor_expires_at=datetime.now(UTC) - timedelta(seconds=1),
        active_executor_generation=1,
        active_executor_source="explicit_primary",
        active_executor_unavailable_since=datetime.now(UTC) - timedelta(seconds=10),
        ws_provider=_Provider({"additional": False, "primary": True}),
        retry_seconds=0,
    )
    assert result.active_executor_id == target.executor_id

    monkeypatch.undo()

    async def additional_cas(*args: Any, **kwargs: Any) -> tuple[bool, int, str | None]:
        return True, 2, "notice"

    monkeypatch.setattr(queries, "cas_executor_failover", additional_cas)
    target = _target("additional", source="additional_explicit", primary=False)
    pool = ExecutorPool(primary=[_target("primary")], additional=[target])
    result = await ensure_active_executor_pin(
        session_factory=_Session,
        conversation_id="conv",
        task_id=None,
        pool=pool,
        active_executor_id=target.executor_id,
        active_executor_expires_at=datetime.now(UTC) - timedelta(seconds=1),
        active_executor_generation=1,
        active_executor_source="additional",
        execution={"executor_selector": {"tier": "primary"}},
        active_executor_unavailable_since=datetime.now(UTC) - timedelta(seconds=10),
        ws_provider=_Provider({"additional": False, "primary": True}),
        retry_seconds=0,
    )
    assert result.active_executor_id == "primary"


@pytest.mark.asyncio
async def test_connected_expired_additional_bypasses_reconnect_grace(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from cognis.store import queries

    reasons: list[str] = []

    async def additional_cas(*args: Any, **kwargs: Any) -> tuple[bool, int, str | None]:
        reasons.append(kwargs["reason"])
        return True, 2, "notice"

    async def should_not_mark(*args: Any, **kwargs: Any) -> bool:
        raise AssertionError("expired additional pin must not enter reconnect grace")

    monkeypatch.setattr(queries, "cas_executor_failover", additional_cas)
    monkeypatch.setattr(queries, "mark_executor_unavailable", should_not_mark)
    additional = _target("additional", source="additional_explicit", primary=False)
    result = await ensure_active_executor_pin(
        session_factory=_Session,
        conversation_id="conv",
        task_id=None,
        pool=ExecutorPool(
            primary=[_target("a-disconnected"), _target("b-connected")],
            additional=[additional],
        ),
        active_executor_id=additional.executor_id,
        active_executor_expires_at=datetime.now(UTC) - timedelta(seconds=1),
        active_executor_generation=1,
        active_executor_source="additional",
        execution={"executor_selector": {"tier": "primary"}},
        active_executor_unavailable_since=None,
        ws_provider=_Provider(
            {
                "additional": True,
                "a-disconnected": False,
                "b-connected": True,
            }
        ),
        retry_seconds=30,
    )
    assert result.active_executor_id == "b-connected"
    assert reasons == ["secondary assignment expired"]


@pytest.mark.asyncio
async def test_transport_disconnect_reason_is_factual(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from cognis.store import queries

    reasons: list[str] = []

    async def cas(*args: Any, **kwargs: Any) -> tuple[bool, int, str | None]:
        reasons.append(kwargs["reason"])
        return True, 2, "notice"

    monkeypatch.setattr(queries, "cas_executor_failover", cas)
    result = await ensure_active_executor_pin(
        session_factory=_Session,
        conversation_id="conv",
        task_id=None,
        pool=ExecutorPool(primary=[_target("old"), _target("new")]),
        active_executor_id="old",
        active_executor_expires_at=None,
        active_executor_generation=1,
        active_executor_source="selector_primary",
        active_executor_unavailable_since=datetime.now(UTC) - timedelta(seconds=10),
        ws_provider=_Provider({"old": False, "new": True}),
        retry_seconds=0,
    )
    assert result.active_executor_id == "new"
    assert reasons == ["executor transport disconnected or not ready"]


@pytest.mark.asyncio
async def test_failover_skips_unready_replacement(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from cognis.store import queries

    destinations: list[str] = []

    async def cas(*args: Any, **kwargs: Any) -> tuple[bool, int, str | None]:
        destinations.append(kwargs["new_executor_id"])
        return True, 2, "notice"

    monkeypatch.setattr(queries, "cas_executor_failover", cas)
    pool = ExecutorPool(
        primary=[
            _target("old"),
            _target("a-disconnected"),
            _target("b-connected"),
        ]
    )
    result = await ensure_active_executor_pin(
        session_factory=_Session,
        conversation_id="conv",
        task_id=None,
        pool=pool,
        active_executor_id="old",
        active_executor_expires_at=None,
        active_executor_generation=1,
        active_executor_source="selector_primary",
        active_executor_unavailable_since=datetime.now(UTC) - timedelta(seconds=10),
        ws_provider=_Provider(
            {
                "old": False,
                "a-disconnected": False,
                "b-connected": True,
            }
        ),
        retry_seconds=0,
    )
    assert result.active_executor_id == "b-connected"
    assert destinations == ["b-connected"]


@pytest.mark.asyncio
async def test_missing_selector_observation_grace_reconnect_and_failover(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from cognis.store import queries

    marks: list[datetime] = []
    clears = 0
    cas_args: list[dict[str, Any]] = []

    async def mark(*_args: Any, **kwargs: Any) -> tuple[bool, datetime]:
        marks.append(kwargs["observed_at"])
        return True, kwargs["observed_at"]

    async def clear(*_args: Any, **_kwargs: Any) -> bool:
        nonlocal clears
        clears += 1
        return True

    async def cas(*_args: Any, **kwargs: Any) -> tuple[bool, int, str | None]:
        cas_args.append(kwargs)
        return True, 2, "notice"

    monkeypatch.setattr(queries, "mark_executor_unavailable", mark)
    monkeypatch.setattr(queries, "clear_executor_unavailable", clear)
    monkeypatch.setattr(queries, "cas_executor_failover", cas)
    base = datetime(2026, 7, 27, tzinfo=UTC)
    pool = ExecutorPool(primary=[_target("replacement")])
    common = dict(
        session_factory=_Session,
        conversation_id="conv",
        task_id=None,
        pool=pool,
        active_executor_id="missing",
        active_executor_expires_at=None,
        active_executor_generation=4,
        active_executor_source="selector_primary",
        ws_provider=_Provider({"replacement": True}),
        retry_seconds=15,
    )
    first = await ensure_active_executor_pin(**common, now=base)
    assert first.active_executor_id == "missing"
    assert marks == [base]
    before_grace = await ensure_active_executor_pin(
        **common, active_executor_unavailable_since=base, now=base + timedelta(seconds=5)
    )
    assert before_grace.active_executor_id == "missing"
    reconnected = await ensure_active_executor_pin(
        **{
            **common,
            "pool": ExecutorPool(primary=[_target("missing"), _target("replacement")]),
            "active_executor_unavailable_since": base,
            "now": base + timedelta(seconds=5),
            "ws_provider": _Provider({"missing": True, "replacement": True}),
        }
    )
    assert reconnected.active_executor_id == "missing"
    assert clears == 1
    assert cas_args == []
    failed = await ensure_active_executor_pin(
        **common, active_executor_unavailable_since=base, now=base + timedelta(seconds=16)
    )
    assert failed.active_executor_id == "replacement"
    assert cas_args[0]["reason"] == "executor is missing from the current assigned pool"
    assert cas_args[0]["failover_source"] == "selector_primary"


@pytest.mark.asyncio
async def test_missing_explicit_and_unknown_provenance_remain_hard_bound(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from cognis.store import queries

    async def fail(*_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("hard-bound pin must not fail over")

    monkeypatch.setattr(queries, "cas_executor_failover", fail)
    for source in ("explicit_primary", "unknown"):
        result = await ensure_active_executor_pin(
            session_factory=_Session,
            conversation_id="conv",
            task_id=None,
            pool=ExecutorPool(primary=[_target("replacement")]),
            active_executor_id="missing",
            active_executor_expires_at=None,
            active_executor_generation=1,
            active_executor_source=source,
            ws_provider=_Provider({"replacement": True}),
            retry_seconds=0,
        )
        assert result.active_executor_id == "missing"


def test_legacy_switch_and_default_sources_are_canonicalized_without_pool_facts() -> None:
    assert (
        normalize_active_executor_source(
            "user_switch", expires_at=None, execution={"executor_selector": {"labels": ["x"]}}
        )
        == "explicit_primary"
    )
    assert (
        normalize_active_executor_source("agent_switch", expires_at=datetime.now(UTC), execution={})
        == "additional"
    )
    assert normalize_active_executor_source("default", expires_at=None, execution={}) is None
    assert normalize_active_executor_source("unknown", expires_at=None, execution={}) == "unknown"
    assert (
        normalize_active_executor_source(
            "initial",
            expires_at=None,
            execution={"executor_selector": {"labels": ["gpu"]}},
        )
        == "selector_primary"
    )
    assert (
        normalize_active_executor_source(
            None,
            expires_at=None,
            execution={"executor_id": "explicit"},
        )
        == "explicit_primary"
    )
    assert normalize_active_executor_source(None, expires_at=None, execution={}) is None


@pytest.mark.asyncio
async def test_missing_legacy_selector_does_not_fail_over_after_binding_becomes_explicit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from cognis.store import queries

    async def fail(*_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("selector failover requires a current selector")

    monkeypatch.setattr(queries, "cas_executor_failover", fail)
    result = await ensure_active_executor_pin(
        session_factory=_Session,
        conversation_id="conv",
        task_id=None,
        pool=ExecutorPool(primary=[_target("explicit-replacement", source="explicit")]),
        active_executor_id="missing-selector",
        active_executor_expires_at=None,
        active_executor_generation=3,
        active_executor_unavailable_since=datetime.now(UTC) - timedelta(seconds=30),
        active_executor_source="selector_primary",
        execution={"executor_id": "explicit-replacement"},
        ws_provider=_Provider({"explicit-replacement": True}),
        retry_seconds=15,
    )
    assert result.active_executor_id == "missing-selector"


@pytest.mark.asyncio
async def test_missing_additional_uses_grace_unless_expired_or_unbounded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from cognis.store import queries

    marks = 0
    destinations: list[str] = []

    async def mark(*_args: Any, **_kwargs: Any) -> tuple[bool, datetime]:
        nonlocal marks
        marks += 1
        return True, datetime.now(UTC)

    async def cas(*_args: Any, **kwargs: Any) -> tuple[bool, int, str | None]:
        destinations.append(kwargs["new_executor_id"])
        return True, 2, "notice"

    monkeypatch.setattr(queries, "mark_executor_unavailable", mark)
    monkeypatch.setattr(queries, "cas_executor_failover", cas)
    base = datetime(2026, 7, 27, tzinfo=UTC)
    pool = ExecutorPool(primary=[_target("ready-primary")])
    common = dict(
        session_factory=_Session,
        conversation_id="conv",
        task_id=None,
        pool=pool,
        active_executor_id="missing-additional",
        active_executor_generation=1,
        active_executor_source="additional",
        execution={"executor_selector": {"tier": "primary"}},
        ws_provider=_Provider({"ready-primary": True}),
        retry_seconds=15,
    )
    unexpired = await ensure_active_executor_pin(
        **common,
        active_executor_expires_at=base + timedelta(seconds=60),
        active_executor_unavailable_since=None,
        now=base,
    )
    assert unexpired.active_executor_id == "missing-additional"
    assert marks == 1
    grace = await ensure_active_executor_pin(
        **common,
        active_executor_expires_at=base + timedelta(seconds=60),
        active_executor_unavailable_since=base,
        now=base + timedelta(seconds=5),
    )
    assert grace.active_executor_id == "missing-additional"
    expired = await ensure_active_executor_pin(
        **common,
        active_executor_expires_at=base - timedelta(seconds=1),
        active_executor_unavailable_since=None,
        now=base,
    )
    unbounded = await ensure_active_executor_pin(
        **common,
        active_executor_expires_at=None,
        active_executor_unavailable_since=None,
        now=base,
    )
    assert expired.active_executor_id == "ready-primary"
    assert unbounded.active_executor_id == "ready-primary"
    assert destinations == ["ready-primary", "ready-primary"]
