"""Unit tests for Stage 36 multi-executor agent pool resolution."""

from __future__ import annotations

import contextlib
from dataclasses import dataclass, field
from typing import Any
from unittest.mock import patch

import pytest

from cognis.core.executor_policy import ExecutorPolicy
from cognis.core.executor_pool import (
    ExecutorAvailability,
    ExecutorPool,
    ResolvedExecutorTarget,
    parse_additional_executors,
    pick_initial_active,
    resolve_executor_pool,
    tool_observed_on,
)


@dataclass
class FakeExecutorRow:
    executor_id: str
    name: str = "test"
    executor_type: str = "in_process"
    labels: dict[str, Any] | None = None
    enabled_tools: list[str] | None = field(default_factory=lambda: ["*"])
    enabled_tool_groups: list[str] | None = field(default_factory=list)
    status: str = "active"
    is_default: bool = False
    owner_email: str | None = "user@example.com"
    runtime_state: str = "active"
    desired_config_version: int = 0
    applied_config_version: int = 0
    observed_tools: list[dict[str, Any]] = field(default_factory=list)
    config: dict[str, Any] | None = None
    last_observed_at: Any | None = None


class _FakeSession:
    def __init__(self, rows: list[FakeExecutorRow]) -> None:
        self._rows = rows

    async def __aenter__(self) -> _FakeSession:
        return self

    async def __aexit__(self, *args: Any) -> None:
        return None


class _FakeSessionFactory:
    def __init__(self, rows: list[FakeExecutorRow]) -> None:
        self._rows = rows

    def __call__(self) -> _FakeSession:
        return _FakeSession(self._rows)


@contextlib.asynccontextmanager
async def _patched_queries(rows: list[FakeExecutorRow]):
    """Patch get_executor_row + list_executors used by resolve_executor_pool."""

    async def _list_executors(_session, *, owner_email: str, include_shared: bool) -> list[Any]:
        return list(rows)

    async def _get_executor_row(_session, executor_id, *, owner_email, include_shared):
        for r in rows:
            if r.executor_id == executor_id:
                return r
        return None

    with (
        patch("cognis.store.queries.list_executors", _list_executors),
        patch("cognis.store.queries.get_executor_row", _get_executor_row),
    ):
        yield


# --------------------------------------------------------------------------
# parse_additional_executors
# --------------------------------------------------------------------------


class TestParseAdditionalExecutors:
    def test_none(self) -> None:
        assert parse_additional_executors(None) == []
        assert parse_additional_executors({}) == []

    def test_explicit_id(self) -> None:
        result = parse_additional_executors(
            {"additional_executors": [{"executor_id": "exec-1", "description": "Mac"}]}
        )
        assert result == [{"executor_id": "exec-1", "description": "Mac"}]

    def test_selector(self) -> None:
        result = parse_additional_executors(
            {"additional_executors": [{"executor_selector": {"role": "browser"}}]}
        )
        assert result == [{"executor_selector": {"role": "browser"}}]

    def test_skips_malformed(self) -> None:
        # Both id and selector → invalid, skipped
        result = parse_additional_executors(
            {
                "additional_executors": [
                    {"executor_id": "x", "executor_selector": {"a": "b"}},
                    {"executor_id": "y"},
                ]
            }
        )
        assert result == [{"executor_id": "y"}]

    def test_skips_neither(self) -> None:
        result = parse_additional_executors({"additional_executors": [{}]})
        assert result == []


# --------------------------------------------------------------------------
# resolve_executor_pool — primary explicit
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pool_primary_explicit_id() -> None:
    rows = [
        FakeExecutorRow(executor_id="exec-1", labels={"tier": "primary"}),
        FakeExecutorRow(executor_id="exec-2", labels={"tier": "extra"}),
    ]
    factory = _FakeSessionFactory(rows)
    async with _patched_queries(rows):
        pool = await resolve_executor_pool(
            session_factory=factory,
            agent_execution={"executor_id": "exec-1"},
            user_email="user@example.com",
            executor_owner_email="user@example.com",
            policy=ExecutorPolicy(),
        )
    assert len(pool.primary) == 1
    assert pool.primary[0].executor_id == "exec-1"
    assert pool.primary[0].is_primary is True
    assert pool.primary[0].selection_source == "explicit"
    assert pool.additional == []


@pytest.mark.asyncio
async def test_pool_primary_explicit_id_missing_row() -> None:
    rows = [FakeExecutorRow(executor_id="other")]
    async with _patched_queries(rows):
        pool = await resolve_executor_pool(
            session_factory=_FakeSessionFactory(rows),
            agent_execution={"executor_id": "ghost"},
            user_email="user@example.com",
            executor_owner_email="user@example.com",
            policy=ExecutorPolicy(),
        )
    assert len(pool.primary) == 1
    assert pool.primary[0].executor_id == "ghost"
    assert pool.primary[0].state == ExecutorAvailability.NOT_FOUND
    assert pool.primary[0].usable is False


# --------------------------------------------------------------------------
# resolve_executor_pool — primary selector multi-match (Stage 36 change)
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pool_primary_selector_matches_multiple() -> None:
    """Stage 36: primary selector matching N>=1 yields a primary set of size N."""

    rows = [
        FakeExecutorRow(executor_id="exec-a", labels={"tier": "primary", "loc": "us"}),
        FakeExecutorRow(executor_id="exec-b", labels={"tier": "primary", "loc": "eu"}),
        FakeExecutorRow(executor_id="exec-c", labels={"tier": "extra"}),
    ]
    async with _patched_queries(rows):
        pool = await resolve_executor_pool(
            session_factory=_FakeSessionFactory(rows),
            agent_execution={"executor_selector": {"tier": "primary"}},
            user_email="user@example.com",
            executor_owner_email="user@example.com",
            policy=ExecutorPolicy(),
        )
    assert len(pool.primary) == 2
    assert sorted(t.executor_id for t in pool.primary) == ["exec-a", "exec-b"]
    assert all(t.is_primary for t in pool.primary)


# --------------------------------------------------------------------------
# resolve_executor_pool — additional bindings
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pool_additional_explicit() -> None:
    rows = [
        FakeExecutorRow(executor_id="exec-primary"),
        FakeExecutorRow(executor_id="exec-extra"),
    ]
    async with _patched_queries(rows):
        pool = await resolve_executor_pool(
            session_factory=_FakeSessionFactory(rows),
            agent_execution={
                "executor_id": "exec-primary",
                "additional_executors": [{"executor_id": "exec-extra", "description": "Mac"}],
            },
            user_email="user@example.com",
            executor_owner_email="user@example.com",
            policy=ExecutorPolicy(),
        )
    assert len(pool.primary) == 1
    assert len(pool.additional) == 1
    assert pool.additional[0].executor_id == "exec-extra"
    assert pool.additional[0].description == "Mac"
    assert pool.additional[0].is_primary is False


@pytest.mark.asyncio
async def test_pool_additional_collision_with_primary_dedupes_to_primary() -> None:
    rows = [FakeExecutorRow(executor_id="exec-x")]
    async with _patched_queries(rows):
        pool = await resolve_executor_pool(
            session_factory=_FakeSessionFactory(rows),
            agent_execution={
                "executor_id": "exec-x",
                "additional_executors": [{"executor_id": "exec-x"}],
            },
            user_email="user@example.com",
            executor_owner_email="user@example.com",
            policy=ExecutorPolicy(),
        )
    assert len(pool.primary) == 1
    assert len(pool.additional) == 0
    assert pool.primary[0].is_primary is True


@pytest.mark.asyncio
async def test_pool_additional_selector_overlap_with_primary() -> None:
    rows = [
        FakeExecutorRow(executor_id="a", labels={"role": "everywhere"}),
        FakeExecutorRow(executor_id="b", labels={"role": "everywhere"}),
    ]
    async with _patched_queries(rows):
        pool = await resolve_executor_pool(
            session_factory=_FakeSessionFactory(rows),
            agent_execution={
                "executor_selector": {"role": "everywhere"},
                "additional_executors": [{"executor_selector": {"role": "everywhere"}}],
            },
            user_email="user@example.com",
            executor_owner_email="user@example.com",
            policy=ExecutorPolicy(),
        )
    # Both executors should land in primary; additional dedupes away.
    assert len(pool.primary) == 2
    assert len(pool.additional) == 0


# --------------------------------------------------------------------------
# pick_initial_active
# --------------------------------------------------------------------------


def _target(
    executor_id: str,
    *,
    is_primary: bool = True,
    state: ExecutorAvailability = ExecutorAvailability.USABLE,
) -> ResolvedExecutorTarget:
    return ResolvedExecutorTarget(
        executor_id=executor_id,
        executor_type="in_process",
        is_primary=is_primary,
        selection_source="explicit",
        description=None,
        state=state,
    )


class TestPickInitialActive:
    def test_picks_primary_active_over_degraded(self) -> None:
        pool = ExecutorPool(
            primary=[
                _target("a", state=ExecutorAvailability.DEGRADED),
                _target("b", state=ExecutorAvailability.USABLE),
            ]
        )
        assert pick_initial_active(pool).executor_id == "b"

    def test_breaks_ties_by_id(self) -> None:
        pool = ExecutorPool(primary=[_target("z"), _target("a"), _target("m")])
        assert pick_initial_active(pool).executor_id == "a"

    def test_returns_none_when_no_usable_primary(self) -> None:
        pool = ExecutorPool(
            primary=[_target("a", state=ExecutorAvailability.OFFLINE)],
            additional=[_target("b", is_primary=False)],
        )
        # Stage 36: never auto-pick an additional executor.
        assert pick_initial_active(pool) is None

    def test_returns_none_for_empty_pool(self) -> None:
        assert pick_initial_active(ExecutorPool()) is None


# --------------------------------------------------------------------------
# ExecutorPool helpers
# --------------------------------------------------------------------------


class TestExecutorPoolHelpers:
    def test_by_id(self) -> None:
        pool = ExecutorPool(primary=[_target("a")], additional=[_target("b", is_primary=False)])
        assert pool.by_id("a").executor_id == "a"
        assert pool.by_id("b").executor_id == "b"
        assert pool.by_id("missing") is None

    def test_is_assigned_and_is_primary(self) -> None:
        pool = ExecutorPool(primary=[_target("a")], additional=[_target("b", is_primary=False)])
        assert pool.is_assigned("a") is True
        assert pool.is_assigned("b") is True
        assert pool.is_assigned("c") is False
        assert pool.is_primary("a") is True
        assert pool.is_primary("b") is False

    def test_usable_primaries_filters(self) -> None:
        pool = ExecutorPool(
            primary=[
                _target("a"),
                _target("b", state=ExecutorAvailability.OFFLINE),
            ]
        )
        usable = pool.usable_primaries()
        assert [t.executor_id for t in usable] == ["a"]


# --------------------------------------------------------------------------
# tool_observed_on
# --------------------------------------------------------------------------


class TestToolObservedOn:
    def test_unusable_returns_false(self) -> None:
        target = _target("a", state=ExecutorAvailability.OFFLINE)
        assert tool_observed_on(target, "bash") is False

    def test_observed_when_in_inventory(self) -> None:
        target = ResolvedExecutorTarget(
            executor_id="a",
            executor_type="ws",
            is_primary=True,
            selection_source="explicit",
            description=None,
            state=ExecutorAvailability.USABLE,
            enabled_tools=["*"],
            observed_tools=[{"name": "bash"}],
        )
        assert tool_observed_on(target, "bash") is True
        assert tool_observed_on(target, "missing") is False

    def test_permissive_when_no_observation(self) -> None:
        target = ResolvedExecutorTarget(
            executor_id="a",
            executor_type="ws",
            is_primary=True,
            selection_source="explicit",
            description=None,
            state=ExecutorAvailability.USABLE,
            enabled_tools=["*"],
            observed_tools=[],
        )
        assert tool_observed_on(target, "bash") is True

    def test_filters_by_enabled_tools(self) -> None:
        target = ResolvedExecutorTarget(
            executor_id="a",
            executor_type="ws",
            is_primary=True,
            selection_source="explicit",
            description=None,
            state=ExecutorAvailability.USABLE,
            enabled_tools=["read"],
            observed_tools=[{"name": "bash"}, {"name": "read"}],
        )
        assert tool_observed_on(target, "bash") is False
        assert tool_observed_on(target, "read") is True
