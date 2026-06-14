import pytest

from cognis.core.agent_profiles import (
    normalize_agent_profile_id,
    render_agent_profile_context,
    requested_agent_profile_id,
    resolve_agent_profile,
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

    with pytest.raises(ValueError, match="does not exist"):
        resolve_agent_profile(agent, "missing")

    with pytest.raises(ValueError, match="disabled"):
        resolve_agent_profile(agent, "disabled")

    with pytest.raises(ValueError, match="must not contain '/'"):
        normalize_agent_profile_id("laforge/fast")


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
