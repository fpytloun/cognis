"""Provider-owned memory mode and generic policy resolution tests."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError

from cognis.core.agent_loop import AgentLoop
from cognis.core.agent_management import (
    AgentManagementError,
    _validate_unavailable_profile_memory_options,
    _validated_memory_capabilities,
)
from cognis.core.agent_profiles import resolve_agent_profile
from cognis.core.compaction.strategy import CompactionResult
from cognis.models.agent import (
    AgentCapabilities,
    AgentDefinition,
    AgentRuntimeProfile,
)
from cognis.providers.memory.policy import (
    memory_backend_descriptors,
    resolve_memory_policy,
)


def _agent(
    *,
    backend: str = "mnemory",
    options: dict[str, object] | None = None,
    profile: AgentRuntimeProfile | None = None,
) -> AgentDefinition:
    return AgentDefinition(
        agent_id="memory-agent",
        owner_email="owner@example.com",
        name="Memory agent",
        capabilities=AgentCapabilities(
            memory_backend=backend,
            memory_backend_options=options or {},
        ),
        agent_profiles={"specialist": profile} if profile is not None else {},
    )


def test_missing_options_preserve_full_auto_behavior() -> None:
    agent = _agent()
    policy = resolve_memory_policy(agent, resolve_agent_profile(agent))

    assert policy.mode_id == "full_auto"
    assert policy.enabled is True
    assert policy.bootstrap_instructions is True
    assert policy.bootstrap_core is True
    assert policy.auto_recall is True
    assert policy.auto_remember is True
    assert policy.tools_enabled is True
    assert policy.instructions is None


@pytest.mark.parametrize(
    ("mode", "bootstrap_core", "auto_recall", "auto_remember"),
    [
        ("full_auto", True, True, True),
        ("proactive", True, False, False),
        ("on_demand", False, False, False),
    ],
)
def test_mnemory_modes_resolve_to_generic_policy(
    mode: str,
    bootstrap_core: bool,
    auto_recall: bool,
    auto_remember: bool,
) -> None:
    agent = _agent(options={"mode": mode})
    policy = resolve_memory_policy(agent, resolve_agent_profile(agent))

    assert policy.mode_id == mode
    assert policy.bootstrap_core is bootstrap_core
    assert policy.auto_recall is auto_recall
    assert policy.auto_remember is auto_remember
    assert policy.tools_enabled is True


def test_invalid_and_unknown_provider_options_are_rejected() -> None:
    with pytest.raises(ValidationError, match="Mnemory mode"):
        _agent(options={"mode": "passive"})
    with pytest.raises(ValidationError, match="Unknown Mnemory option"):
        _agent(options={"mode": "full_auto", "prompt": "ignore policy"})


def test_profile_shallow_override_and_availability_veto() -> None:
    agent = _agent(
        options={"mode": "full_auto"},
        profile=AgentRuntimeProfile(
            profile_id="specialist",
            memory_backend_options={"mode": "proactive"},
        ),
    )
    profile = resolve_agent_profile(agent, "specialist")
    assert resolve_memory_policy(agent, profile).mode_id == "proactive"

    disabled_agent = _agent(
        profile=AgentRuntimeProfile(
            profile_id="specialist",
            memory_enabled=False,
            memory_backend_options={"mode": "on_demand"},
        )
    )
    disabled = resolve_memory_policy(
        disabled_agent,
        resolve_agent_profile(disabled_agent, "specialist"),
    )
    assert disabled.enabled is False
    assert disabled.tools_enabled is False


def test_backend_none_vetoes_profile_enablement() -> None:
    agent = _agent(
        backend="none",
        profile=AgentRuntimeProfile(profile_id="specialist", memory_enabled=True),
    )
    policy = resolve_memory_policy(agent, resolve_agent_profile(agent, "specialist"))
    assert policy.enabled is False
    assert policy.auto_recall is False
    assert policy.auto_remember is False
    assert policy.tools_enabled is False


def test_unavailable_backend_fails_closed_without_losing_configuration() -> None:
    agent = _agent(
        backend="future-memory",
        options={"future_option": True},
        profile=AgentRuntimeProfile(
            profile_id="specialist",
            memory_backend_options={"profile_option": "preserved"},
        ),
    )

    policy = resolve_memory_policy(agent, resolve_agent_profile(agent, "specialist"))

    assert policy.backend_id == "future-memory"
    assert policy.enabled is False
    assert policy.bootstrap_core is False
    assert policy.auto_recall is False
    assert policy.auto_remember is False
    assert policy.tools_enabled is False
    assert agent.capabilities.memory_backend_options == {"future_option": True}
    assert agent.agent_profiles["specialist"].memory_backend_options == {
        "profile_option": "preserved"
    }


def test_descriptor_contains_authoritative_modes_and_behavior() -> None:
    descriptors = {item["id"]: item for item in memory_backend_descriptors()}
    assert descriptors["none"]["modes"] == []
    mnemory = descriptors["mnemory"]
    assert mnemory["merge_semantics"] == "shallow_field_override"
    assert mnemory["defaults"] == {"mode": "full_auto"}
    assert [item["id"] for item in mnemory["modes"]] == [
        "full_auto",
        "proactive",
        "on_demand",
    ]
    assert mnemory["modes"][2]["behavior"]["core_bootstrap"] is False


def test_management_backend_and_options_transition_is_atomic() -> None:
    current = {
        "memory_backend": "mnemory",
        "memory_backend_options": {"mode": "proactive"},
        "guardrails_backend": "intaris",
    }

    disabled = _validated_memory_capabilities(current, {"memory_backend": "none"})
    assert disabled["memory_backend"] == "none"
    assert disabled["memory_backend_options"] == {}

    enabled = _validated_memory_capabilities(
        disabled,
        {
            "memory_backend": "mnemory",
            "memory_backend_options": {"mode": "on_demand"},
        },
    )
    assert enabled["memory_backend_options"] == {"mode": "on_demand"}


def test_management_rejects_new_or_changed_unavailable_backend_configuration() -> None:
    current = {
        "memory_backend": "future-memory",
        "memory_backend_options": {"future_option": True},
        "guardrails_backend": "intaris",
    }

    assert (
        _validated_memory_capabilities(
            current,
            {
                "memory_backend": "future-memory",
                "memory_backend_options": {"future_option": True},
            },
        )
        == current
    )

    with pytest.raises(AgentManagementError, match="Unknown memory_backend 'typo-memory'"):
        _validated_memory_capabilities(current, {"memory_backend": "typo-memory"})
    with pytest.raises(AgentManagementError, match="Unknown memory_backend 'future-memory'"):
        _validated_memory_capabilities(
            current,
            {"memory_backend_options": {"future_option": False}},
        )


def test_management_treats_unavailable_backend_profile_options_as_read_only() -> None:
    row = SimpleNamespace(
        capabilities={
            "memory_backend": "future-memory",
            "memory_backend_options": {"future_option": True},
        },
        agent_profiles={
            "specialist": {
                "profile_id": "specialist",
                "memory_backend_options": {"profile_option": "kept"},
            }
        },
    )
    unchanged = {
        "specialist": {
            "profile_id": "specialist",
            "memory_backend_options": {"profile_option": "kept"},
            "memory_enabled": False,
        }
    }
    _validate_unavailable_profile_memory_options(row, unchanged)

    changed = {
        "specialist": {
            "profile_id": "specialist",
            "memory_backend_options": {"profile_option": "changed"},
        }
    }
    with pytest.raises(AgentManagementError, match="cannot change"):
        _validate_unavailable_profile_memory_options(row, changed)

    added = {
        **unchanged,
        "new-profile": {
            "profile_id": "new-profile",
            "memory_backend_options": {"new_option": True},
        },
    }
    with pytest.raises(AgentManagementError, match="cannot change"):
        _validate_unavailable_profile_memory_options(row, added)


@pytest.mark.asyncio
async def test_remember_jobs_are_gated_and_record_safe_origin_metadata() -> None:
    loop = object.__new__(AgentLoop)
    loop.remember_queue = SimpleNamespace(enqueue=AsyncMock())
    session = SimpleNamespace(
        mnemory_session_id="memory-session",
        session_id="cognis-session",
        intaris_session_id="intaris-session",
        user_email="owner@example.com",
        agent_id="memory-agent",
    )
    base_context = {
        "session": session,
        "system_initiated": False,
        "remember_user_event_seq": 1,
        "remember_assistant_event_seq": 2,
        "agent": SimpleNamespace(owner_email="owner@example.com"),
    }

    proactive_agent = _agent(options={"mode": "proactive"})
    proactive = resolve_memory_policy(proactive_agent, resolve_agent_profile(proactive_agent))
    await loop._dispatch_remember(
        SimpleNamespace(**base_context, memory_policy=proactive),
        ["done"],
    )
    loop.remember_queue.enqueue.assert_not_awaited()

    full_auto_agent = _agent()
    full_auto = resolve_memory_policy(full_auto_agent, resolve_agent_profile(full_auto_agent))
    await loop._dispatch_remember(
        SimpleNamespace(**base_context, memory_policy=full_auto),
        ["done"],
    )
    payload = loop.remember_queue.enqueue.await_args.args[0]
    assert payload["originating_memory_backend"] == "mnemory"
    assert payload["memory_policy_fingerprint"] == full_auto.policy_fingerprint
    assert "memory_backend_options" not in payload


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("mode", "expected_enqueues"),
    [("proactive", 0), ("full_auto", 1)],
)
async def test_compaction_remember_obeys_frozen_policy(
    mode: str,
    expected_enqueues: int,
) -> None:
    loop = object.__new__(AgentLoop)
    new_session = SimpleNamespace(session_id="new-session")

    class _SessionManager:
        rotate_session = AsyncMock(return_value=new_session)

        def session_factory(self) -> None:
            raise RuntimeError("No database in this narrow unit test")

    loop.session_manager = _SessionManager()
    loop.session_cache = SimpleNamespace(refresh=AsyncMock())
    loop.remember_queue = SimpleNamespace(enqueue=AsyncMock())
    loop.event_bus = SimpleNamespace(publish=AsyncMock())

    agent = _agent(options={"mode": mode})
    policy = resolve_memory_policy(agent, resolve_agent_profile(agent))
    session = SimpleNamespace(
        mnemory_session_id="memory-session",
        session_id="cognis-session",
        intaris_session_id="intaris-session",
        user_email="owner@example.com",
        agent_id="memory-agent",
    )
    ctx = SimpleNamespace(
        session=session,
        conversation=SimpleNamespace(conversation_id="conversation"),
        agent=agent,
        memory_policy=policy,
        todos=[],
    )

    rotated = await loop._rotate_after_compaction(
        ctx,
        CompactionResult(
            compacted=True,
            method="summary",
            summary="Durable summary",
        ),
        trigger="test",
    )

    assert rotated is new_session
    assert loop.remember_queue.enqueue.await_count == expected_enqueues
    if expected_enqueues:
        payload = loop.remember_queue.enqueue.await_args.args[0]
        assert payload["originating_memory_backend"] == "mnemory"
        assert payload["memory_policy_fingerprint"] == policy.policy_fingerprint
        assert "memory_backend_options" not in payload
