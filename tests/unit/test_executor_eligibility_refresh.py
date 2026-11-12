"""Admission refresh preserves assignment scope and inventory."""

from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from cognis.core import executor_pool
from cognis.core.executor_policy import ExecutorPolicy
from cognis.core.executor_pool import (
    ExecutorAvailability,
    ExecutorPool,
    ResolvedExecutorTarget,
    refresh_executor_eligibility,
    unknown_executor_eligibility,
)
from cognis.models.agent import AgentDefinition


def _target(executor_id: str, state: ExecutorAvailability) -> ResolvedExecutorTarget:
    return ResolvedExecutorTarget(
        executor_id=executor_id,
        executor_type="websocket",
        is_primary=True,
        selection_source="explicit",
        description="Assigned executor",
        state=state,
        observed_tools=[{"name": "bash"}],
        owner_email="owner@cognis.test",
    )


@pytest.mark.asyncio
async def test_refresh_uses_current_scoped_resolution_without_expanding_membership(monkeypatch):
    retained = _target("assigned-workstation", ExecutorAvailability.OFFLINE)
    pool = ExecutorPool(primary=[retained])
    observed = replace(
        retained,
        state=ExecutorAvailability.USABLE,
        enabled_tools=["read"],
        observed_tools=[{"name": "read"}],
        applied_config_version=4,
    )
    resolver = AsyncMock(
        return_value=ExecutorPool(
            primary=[observed, _target("new-selector-match", ExecutorAvailability.USABLE)]
        )
    )
    monkeypatch.setattr(executor_pool, "resolve_executor_pool", resolver)
    factory = Mock()
    policy = ExecutorPolicy()
    execution = {"executor_id": retained.executor_id}

    refreshed = await refresh_executor_eligibility(
        pool,
        session_factory=factory,
        agent_execution=execution,
        user_email="grantee@cognis.test",
        executor_owner_email="owner@cognis.test",
        policy=policy,
    )

    resolver.assert_awaited_once_with(
        session_factory=factory,
        agent_execution=execution,
        user_email="grantee@cognis.test",
        executor_owner_email="owner@cognis.test",
        policy=policy,
        defer_observed_tools=True,
    )
    assert [target.executor_id for target in refreshed.all] == [retained.executor_id]
    assert refreshed.primary[0].usable
    assert refreshed.primary[0].observed_tools is retained.observed_tools
    assert refreshed.primary[0].enabled_tools == ["read"]
    assert refreshed.primary[0].applied_config_version == 4
    assert pool.primary[0].state is ExecutorAvailability.OFFLINE


@pytest.mark.asyncio
@pytest.mark.parametrize("unauthorized", [False, True])
async def test_missing_or_foreign_targets_cannot_supply_new_private_metadata(
    monkeypatch, unauthorized: bool
):
    retained = _target("assigned-workstation", ExecutorAvailability.USABLE)
    foreign = replace(
        retained,
        state=ExecutorAvailability.UNAUTHORIZED,
        owner_email="other-owner@cognis.test",
        config={"workspace_root": "/private/other-owner"},
        labels={"private": "another-owner"},
        row=object(),
    )
    monkeypatch.setattr(
        executor_pool,
        "resolve_executor_pool",
        AsyncMock(return_value=ExecutorPool(primary=[foreign] if unauthorized else [])),
    )

    refreshed = await refresh_executor_eligibility(
        ExecutorPool(primary=[retained]),
        session_factory=Mock(),
        agent_execution={"executor_id": retained.executor_id},
        user_email="grantee@cognis.test",
        executor_owner_email="owner@cognis.test",
        policy=ExecutorPolicy(),
    )

    target = refreshed.primary[0]
    assert not target.usable
    assert target.row is None
    assert target.config == {}
    assert target.labels == {}
    assert target.owner_email == retained.owner_email


def test_failed_observation_is_unknown_not_stale_usable_or_falsely_offline():
    retained = _target("assigned-workstation", ExecutorAvailability.USABLE)
    pool = ExecutorPool(primary=[retained])

    unknown = unknown_executor_eligibility(pool)

    assert unknown.primary[0].state is ExecutorAvailability.UNKNOWN
    assert not unknown.primary[0].usable
    assert pool.primary[0].usable


@pytest.mark.asyncio
@pytest.mark.parametrize("scope", ["owner_executor", "grantee_executor"])
async def test_scope_refresh_revalidates_grant_and_preserves_effective_owner(monkeypatch, scope):
    from cognis.api import runtime_support
    from cognis.core import project_access

    grant = SimpleNamespace(
        executor_scope=scope,
        grantee_overrides={"execution": {"executor_id": "grantee-workstation"}},
    )
    resolve_grant = AsyncMock(side_effect=[grant, None])
    monkeypatch.setattr(project_access, "resolve_agent_use_grant", resolve_grant)
    monkeypatch.setattr(runtime_support, "_execution_project_id", AsyncMock(return_value=None))
    agent = AgentDefinition(
        agent_id="shared-coding-agent",
        owner_email="owner@cognis.test",
        name="Shared coding agent",
        execution={"executor_id": "owner-workstation"},
    )
    session = Mock()

    execution, owner, allow_default = await runtime_support._resolve_executor_scope(
        session,
        agent,
        "grantee@cognis.test",
        conversation_id=None,
        task_id=None,
    )

    if scope == "owner_executor":
        assert owner == agent.owner_email
        assert execution["executor_id"] == "owner-workstation"
        assert not allow_default
    else:
        assert owner == "grantee@cognis.test"
        assert execution["executor_id"] == "grantee-workstation"
        assert allow_default
    with pytest.raises(RuntimeError, match="not accessible"):
        await runtime_support._resolve_executor_scope(
            session,
            agent,
            "grantee@cognis.test",
            conversation_id=None,
            task_id=None,
        )
