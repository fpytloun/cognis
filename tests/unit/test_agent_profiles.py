import pytest

from cognis.core.agent_profiles import (
    agent_profile_options,
    agent_switch_eligible_profiles,
    normalize_agent_profile_id,
    render_agent_profile_context,
    requested_agent_profile_id,
    resolve_agent_profile,
    resolve_conversation_agent_profile,
)
from cognis.models.agent import AgentDefinition, AgentLLMConfig, AgentRuntimeProfile
from cognis.models.session import ConversationContext, ConversationModel, SessionModel
from cognis.models.workflow import StepDefinition


def _agent(**kwargs) -> AgentDefinition:
    data = {
        "agent_id": "laforge",
        "name": "LaForge",
        "owner_email": "filip@example.com",
        "description": "Engineering agent",
        "system_prompt": "You are LaForge.",
        "llm_config": AgentLLMConfig(
            provider_id="openai",
            model="gpt-5",
            reasoning_effort="medium",
        ),
    }
    data.update(kwargs)
    return AgentDefinition(**data)


def test_resolves_synthetic_default_when_agent_has_no_profiles() -> None:
    resolved = resolve_agent_profile(_agent(), None)

    assert resolved.profile_id == "default"
    assert resolved.source == "synthetic_default"
    assert resolved.provider_id == "openai"
    assert resolved.model == "gpt-5"
    assert resolved.reasoning_effort == "medium"


def test_agent_definition_accepts_legacy_null_agent_profiles() -> None:
    agent = AgentDefinition.model_validate(
        {
            "agent_id": "laforge",
            "name": "LaForge",
            "owner_email": "filip@example.com",
            "agent_profiles": None,
        }
    )

    assert agent.agent_profiles == {}


def test_agent_definition_strips_legacy_agent_profile_metadata() -> None:
    agent = AgentDefinition.model_validate(
        {
            "agent_id": "laforge",
            "name": "LaForge",
            "owner_email": "filip@example.com",
            "agent_profiles": {
                "fast": {
                    "profile_id": "fast",
                    "description": "Low latency",
                    "metadata": {},
                }
            },
        }
    )

    assert agent.agent_profiles["fast"].profile_id == "fast"
    assert agent.agent_profiles["fast"].description == "Low latency"


def test_runtime_profile_strips_legacy_metadata_directly() -> None:
    profile = AgentRuntimeProfile.model_validate(
        {
            "profile_id": "fast",
            "description": "Low latency",
            "metadata": {},
        }
    )

    assert profile.profile_id == "fast"
    assert profile.description == "Low latency"


def test_agent_switchable_profile_requires_routing_description() -> None:
    with pytest.raises(ValueError, match="require description routing guidance"):
        AgentRuntimeProfile(profile_id="senior", agent_switchable=True)


def test_agent_definition_rejects_invalid_runtime_profile_keys() -> None:
    with pytest.raises(ValueError, match="must not contain '/'"):
        _agent(
            agent_profiles={
                "bad/id": AgentRuntimeProfile(
                    description="Invalid key.",
                    agent_switchable=True,
                )
            }
        )


def test_switch_eligible_profiles_exclude_disabled_and_restricted_profiles() -> None:
    agent = _agent(
        agent_profiles={
            "junior": AgentRuntimeProfile(
                profile_id="junior",
                description="Use for bounded routine work.",
                agent_switchable=True,
            ),
            "restricted": AgentRuntimeProfile(
                profile_id="restricted",
                description="User-selected only.",
            ),
            "disabled": AgentRuntimeProfile(
                profile_id="disabled",
                description="Unavailable.",
                enabled=False,
                agent_switchable=True,
            ),
        }
    )

    assert agent_switch_eligible_profiles(agent) == [("junior", "Use for bounded routine work.")]


def test_profile_context_includes_mutable_switch_routing_guidance() -> None:
    resolved = resolve_agent_profile(
        _agent(
            agent_profiles={
                "developer": AgentRuntimeProfile(
                    profile_id="developer",
                    description="Use for normal implementation.",
                    agent_switchable=True,
                )
            }
        ),
        "developer",
    )

    context = render_agent_profile_context(
        resolved,
        switch_eligible_profiles=[
            ("developer", "Use for normal implementation."),
            ("developer-senior", "Use for complex high-risk implementation."),
        ],
    )

    assert context is not None
    assert "developer (current)" in context
    assert "developer-senior: Use for complex high-risk implementation." in context
    assert "Call switch_agent_profile alone" in context
    assert "not private chain-of-thought" in context


def test_resolves_explicit_profile_without_changing_agent_identity() -> None:
    agent = _agent(
        agent_profiles={
            "fast": AgentRuntimeProfile(
                profile_id="fast",
                provider_id="anthropic",
                model="claude-fast",
                reasoning_effort="low",
                system_prompt_extra="Prefer quick, bounded answers.",
                description="Use for low-latency turns.",
            )
        },
        default_agent_profile_id="fast",
    )

    resolved = resolve_agent_profile(agent, "fast", source="explicit")

    assert agent.agent_id == "laforge"
    assert resolved.requested_profile_id == "fast"
    assert resolved.profile_id == "fast"
    assert resolved.provider_id == "anthropic"
    assert resolved.model == "claude-fast"
    assert resolved.reasoning_effort == "low"
    assert "does not redefine identity" in render_agent_profile_context(resolved)
    assert "Prefer quick" in render_agent_profile_context(resolved)


def test_rejects_missing_disabled_or_malformed_profile_ids() -> None:
    agent = _agent(
        agent_profiles={"disabled": AgentRuntimeProfile(profile_id="disabled", enabled=False)}
    )

    with pytest.raises(
        ValueError,
        match="Available profiles: default: Synthetic default profile",
    ):
        resolve_agent_profile(agent, "missing")

    with pytest.raises(ValueError, match="disabled"):
        resolve_agent_profile(agent, "disabled")

    with pytest.raises(ValueError, match="must not contain '/'"):
        normalize_agent_profile_id("laforge/fast")


def test_agent_profile_options_include_descriptions_and_default() -> None:
    agent = _agent(
        agent_profiles={
            "quality": AgentRuntimeProfile(
                profile_id="quality",
                description="Maximum implementation quality.",
            ),
            "fast": AgentRuntimeProfile(
                profile_id="fast",
                description="Low-latency routine work.",
            ),
            "disabled": AgentRuntimeProfile(profile_id="disabled", enabled=False),
        },
        default_agent_profile_id="quality",
    )

    assert agent_profile_options(agent) == [
        {
            "profile_id": "fast",
            "description": "Low-latency routine work.",
            "is_default": False,
            "synthetic": False,
        },
        {
            "profile_id": "quality",
            "description": "Maximum implementation quality.",
            "is_default": True,
            "synthetic": False,
        },
    ]


def test_requested_profile_prefers_session_over_conversation() -> None:
    conversation = ConversationModel(
        conversation_id="conv",
        user_email="filip@example.com",
        agent_id="laforge",
        agent_profile_id="quality",
        title="Conversation",
        context=ConversationContext(type="web"),
    )
    session = SessionModel(
        session_id="sess",
        conversation_id="conv",
        user_email="filip@example.com",
        agent_id="laforge",
        agent_profile_id="fast",
    )

    assert requested_agent_profile_id(session, conversation) == "fast"


def test_requested_profile_uses_conversation_for_same_agent_session() -> None:
    conversation = ConversationModel(
        conversation_id="conv",
        user_email="filip@example.com",
        agent_id="laforge",
        agent_profile_id="quality",
        title="Conversation",
        context=ConversationContext(type="web"),
    )
    session = SessionModel(
        session_id="sess",
        conversation_id="conv",
        user_email="filip@example.com",
        agent_id="laforge",
        agent_profile_id=None,
    )

    assert requested_agent_profile_id(session, conversation) == "quality"


def test_requested_profile_ignores_conversation_for_cross_agent_child_session() -> None:
    conversation = ConversationModel(
        conversation_id="conv",
        user_email="filip@example.com",
        agent_id="laforge",
        agent_profile_id="smart",
        title="Conversation",
        context=ConversationContext(type="web"),
    )
    child_session = SessionModel(
        session_id="child",
        conversation_id="conv",
        user_email="filip@example.com",
        agent_id="system:explore",
        agent_profile_id=None,
        parent_session_id="parent",
    )

    assert requested_agent_profile_id(child_session, conversation) is None


def test_channel_default_profile_is_turn_scoped_fallback_with_audit_source() -> None:
    agent = _agent(
        agent_profiles={
            "chat": AgentRuntimeProfile(profile_id="chat", description="Interactive chat"),
            "quality": AgentRuntimeProfile(profile_id="quality", description="Quality"),
        },
        default_agent_profile_id="quality",
    )
    conversation = ConversationModel(
        conversation_id="conv",
        user_email="filip@example.com",
        agent_id="laforge",
        context=ConversationContext(type="signal"),
    )
    session = SessionModel(
        session_id="sess",
        conversation_id="conv",
        user_email="filip@example.com",
        agent_id="laforge",
        channel_default_agent_profile_id="chat",
    )

    resolved = resolve_conversation_agent_profile(agent, session, conversation)

    assert resolved.profile_id == "chat"
    assert resolved.source == "channel_default"
    assert resolved.audit_metadata()["agent_profile_source"] == "channel_default"
    assert session.model_dump().get("channel_default_agent_profile_id") is None


@pytest.mark.parametrize(
    ("session_profile", "conversation_profile", "expected", "source"),
    [
        ("quality", "chat", "quality", "session"),
        (None, "quality", "quality", "conversation"),
        (None, None, "chat", "channel_default"),
    ],
)
def test_interactive_profile_precedence(
    session_profile: str | None,
    conversation_profile: str | None,
    expected: str,
    source: str,
) -> None:
    agent = _agent(
        agent_profiles={
            "chat": AgentRuntimeProfile(profile_id="chat"),
            "quality": AgentRuntimeProfile(profile_id="quality"),
        }
    )
    conversation = ConversationModel(
        conversation_id="conv",
        user_email="filip@example.com",
        agent_id="laforge",
        agent_profile_id=conversation_profile,
        context=ConversationContext(type="signal"),
    )
    session = SessionModel(
        session_id="sess",
        conversation_id="conv",
        user_email="filip@example.com",
        agent_id="laforge",
        agent_profile_id=session_profile,
        channel_default_agent_profile_id="chat",
    )

    resolved = resolve_conversation_agent_profile(agent, session, conversation)

    assert resolved.profile_id == expected
    assert resolved.source == source


def test_workflow_step_agent_profile_is_separate_from_step_profile() -> None:
    step = StepDefinition(
        name="build",
        type="run",
        prompt="Build the feature.",
        agent_profile_id="fast",
        step_profile_id="restricted-tools",
    )

    assert step.agent_profile_id == "fast"
    assert step.step_profile_id == "restricted-tools"
