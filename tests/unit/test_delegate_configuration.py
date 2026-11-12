"""Configuration propagation through durable child and managed session boundaries."""

from __future__ import annotations

import pytest

from cognis.core.agent_loop import SECONDARY_AGENT_DELEGATION_POLICY
from cognis.core.agent_profiles import resolve_agent_profile
from cognis.core.delegate_configuration import DelegateConfiguration, delegated_configuration
from cognis.core.managed_conversations import inherited_managed_session_policy
from cognis.core.session import SessionManager, _intaris_session_policy
from cognis.models.agent import AgentCapabilities, AgentDefinition, AgentRuntimeProfile
from cognis.models.session import ConversationContext, SessionModel, SessionTransition
from cognis.providers.memory.policy import resolve_memory_policy
from cognis.runtime_context import RuntimeAccessContext, scoped_runtime_context
from cognis.store.models import Agent
from cognis.store.queries import get_session_row
from tests.unit.test_session_manager import _Cache, _Providers, _session_factory


def test_snapshot_preserves_specialist_identity_routing_and_access() -> None:
    parent = AgentDefinition(
        agent_id="parent",
        owner_email="user@example.com",
        name="Parent",
        capabilities=AgentCapabilities(guardrails_backend="none", analytics_enabled=False),
        agent_profiles={
            "focused": AgentRuntimeProfile(
                profile_id="focused", memory_backend_options={"mode": "on_demand"}
            )
        },
    )
    session = SessionModel(
        session_id="parent-session",
        conversation_id="conversation",
        user_email=parent.owner_email,
        agent_id=parent.agent_id,
        agent_profile_id="focused",
    )
    specialist = AgentDefinition(
        agent_id="specialist",
        owner_email=parent.owner_email,
        name="Specialist",
        agent_type="secondary",
        system_prompt="Review only",
        llm_config={"provider_id": "specialist-provider", "model": "specialist-model"},
        permissions={"allowed_secrets": []},
        execution={"executor_id": "restricted-executor"},
        agent_profiles={
            "review": AgentRuntimeProfile(
                profile_id="review",
                model="review-model",
                memory_enabled=False,
                memory_backend_options={"mode": "full_auto"},
            )
        },
    )
    snapshot = DelegateConfiguration.capture(parent, session)
    child = snapshot.apply(specialist)
    assert child.agent_id == specialist.agent_id
    assert child.owner_email == specialist.owner_email
    assert child.system_prompt == specialist.system_prompt
    assert child.permissions == specialist.permissions
    assert child.execution == specialist.execution
    assert child.llm_config == specialist.llm_config
    assert resolve_agent_profile(child, "review").model == "review-model"
    assert child.capabilities.guardrails_enabled is False
    assert child.capabilities.analytics_enabled is False
    assert child.capabilities.memory_backend_options["mode"] == "on_demand"
    assert resolve_memory_policy(child, resolve_agent_profile(child, "review")).enabled is False
    assert SECONDARY_AGENT_DELEGATION_POLICY.skip_memory is True
    assert specialist.capabilities.analytics_enabled is True
    assert specialist.agent_profiles["review"].memory_backend_options == {"mode": "full_auto"}


@pytest.mark.asyncio
async def test_memory_disabled_parent_profile_snapshot_and_child_creation(tmp_path) -> None:
    from cognis.store.queries import get_agent

    engine, factory = await _session_factory(
        tmp_path,
        capabilities={
            "memory_backend": "mnemory",
            "memory_backend_options": {"mode": "full_auto"},
        },
    )
    providers = _Providers()
    manager = SessionManager(factory, providers, _Cache())
    try:
        async with factory() as db:
            row = await get_agent(db, "agent-1")
            row.agent_profiles = {
                "no-memory": {
                    "profile_id": "no-memory",
                    "memory_enabled": False,
                    "memory_backend_options": {"mode": "on_demand"},
                }
            }
            row.default_agent_profile_id = "no-memory"
            await db.commit()
        _, parent = await manager.create_conversation_with_root_session(
            user_email="user@example.com",
            agent_id="agent-1",
            context=ConversationContext(type="web"),
        )
        child = await manager.create_child_session(
            parent,
            mode="delegate_sync",
            task_description="Review without memory",
            agent_id="system:research",
            effective_agent_id="system:research",
        )
        snapshot = delegated_configuration(child.delegation_metadata)
        assert snapshot.capabilities.memory_backend == "none"
        assert snapshot.capabilities.memory_backend_options == {}
        restored = DelegateConfiguration.model_validate(snapshot.model_dump(mode="json"))
        specialist = AgentDefinition(
            agent_id=child.agent_id,
            name="Research",
            owner_email=parent.user_email,
        )
        effective = AgentDefinition.model_validate(restored.apply(specialist).model_dump())
        assert resolve_memory_policy(effective, resolve_agent_profile(effective)).enabled is False
        legacy = child.model_copy(update={"delegation_metadata": {}})
        recovered = await manager.effective_session_agent(legacy, specialist)
        assert recovered.capabilities.memory_backend == "none"
        assert recovered.capabilities.memory_backend_options == {}
    finally:
        await engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("guardrails", ["none", "intaris"])
@pytest.mark.parametrize("analytics", [False, True])
async def test_child_configuration_survives_reload_refresh_and_rotation(
    tmp_path, guardrails, analytics
) -> None:
    engine, factory = await _session_factory(
        tmp_path,
        capabilities={
            "memory_backend": "none",
            "guardrails_backend": guardrails,
            "analytics_enabled": analytics,
            "intaris_evaluation": "strict",
        },
    )
    providers = _Providers()
    manager = SessionManager(factory, providers, _Cache())
    try:
        _, parent = await manager.create_conversation_with_root_session(
            user_email="user@example.com",
            agent_id="agent-1",
            context=ConversationContext(type="web"),
        )
        child = await manager.create_child_session(
            parent,
            mode="delegate_sync",
            task_description="Review the patch",
            agent_id="system:research",
            effective_agent_id="system:research",
        )
        assert providers.guardrails.flags[child.session_id] == {
            "analytics_enabled": analytics,
            "guardrails_enabled": guardrails == "intaris",
        }
        async with factory() as db:
            row = await get_session_row(db, child.session_id)
            reloaded = SessionModel.model_validate(row, from_attributes=True)
        snapshot = delegated_configuration(reloaded.delegation_metadata)
        assert snapshot.capabilities.memory_backend == "none"
        assert snapshot.capabilities.analytics_enabled is analytics
        cold = SessionManager(factory, providers, _Cache())
        await cold.refresh_intaris_session_policy(reloaded)
        assert providers.guardrails.flags[child.session_id]["analytics_enabled"] is analytics
        rotated = await cold.rotate_session(
            current_session=reloaded,
            conversation_id=reloaded.conversation_id,
            intention="Continue review",
            transition=SessionTransition.COMPACT,
            completion_reason="compaction",
            compaction_summary="Review remains in progress",
        )
        assert (
            providers.guardrails.flags[rotated.session_id]
            == providers.guardrails.flags[child.session_id]
        )
        assert delegated_configuration(rotated.delegation_metadata) == snapshot
        await cold.refresh_intaris_session_policy(rotated)
        assert providers.guardrails.last_policy["profile"] == "strict"
    finally:
        await engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("same_agent", [False, True])
async def test_managed_target_configuration_even_for_same_identity(tmp_path, same_agent) -> None:
    engine, factory = await _session_factory(
        tmp_path, capabilities={"analytics_enabled": False, "intaris_evaluation": "balanced"}
    )
    if not same_agent:
        async with factory() as db:
            db.add(
                Agent(
                    agent_id="target-agent",
                    owner_email="user@example.com",
                    name="Target",
                    capabilities={"analytics_enabled": True, "intaris_evaluation": "legacy"},
                )
            )
            await db.commit()
    providers = _Providers()
    manager = SessionManager(factory, providers, _Cache())
    parent_policy = {
        "intaris_evaluation": "strict",
        "deny_policies": ["Do not publish"],
        "allow_policies": ["Read task inputs"],
    }
    inherited = inherited_managed_session_policy({}, parent_policy)
    assert "intaris_evaluation" not in inherited
    assert inherited["deny_policies"] == parent_policy["deny_policies"]
    try:
        # Exercise the actual ambient-context leak on initial root provisioning.
        with scoped_runtime_context(
            access_context=RuntimeAccessContext(
                user_email="user@example.com", agent_id="agent-1", session_policy=parent_policy
            )
        ):
            _, session = await manager.create_conversation_with_root_session(
                user_email="user@example.com",
                agent_id="agent-1" if same_agent else "target-agent",
                context=ConversationContext(
                    type="agent_work",
                    platform_data={
                        "managed_session_policy": inherited,
                    },
                ),
            )
        flags = providers.guardrails.flags[session.session_id]
        assert flags["analytics_enabled"] is (not same_agent)
        expected_version = "hybrid_v1" if same_agent else "legacy"
        assert providers.guardrails.last_policy["evaluation_policy_version"] == expected_version
        if same_agent:
            assert providers.guardrails.last_policy["profile"] == "balanced"
        assert providers.guardrails.last_policy["deny_policies"] == ["Do not publish"]
        await manager.refresh_intaris_session_policy(session, session_policy_override=parent_policy)
        assert providers.guardrails.last_policy["evaluation_policy_version"] == expected_version
    finally:
        await engine.dispose()


def test_managed_legacy_and_derived_policy_keeps_restrictions_not_evaluation() -> None:
    policy = {"intaris_evaluation": "strict", "deny_policies": ["Do not send"]}
    for platform_data in ({}, {"managed_session_policy": policy}):
        inherited = inherited_managed_session_policy(platform_data, policy)
        assert inherited == {"deny_policies": ["Do not send"]}
    agent = AgentDefinition(agent_id="target", owner_email="owner@cognis.test", name="Target")
    resolved = _intaris_session_policy(
        None,
        agent=agent,
        session_policy=policy,
        conversation_data={"managed_session_policy": policy},
    )
    assert resolved["profile"] == "balanced"
    assert resolved["deny_policies"] == ["Do not send"]
