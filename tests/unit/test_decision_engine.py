from __future__ import annotations

import pytest

from cognis.core.decision import DecisionEngine, build_routing_reminder
from cognis.models.agent import AgentDefinition, AgentLLMConfig, AgentPermissions


class _LLM:
    """Fail if the rules-only routing engine attempts inference."""

    async def generate(self, *_args: object, **_kwargs: object) -> dict[str, object]:
        raise AssertionError("Inline routing must not invoke an LLM")


def _agent(can_delegate: bool = True, max_depth: int = 5) -> AgentDefinition:
    return AgentDefinition(
        agent_id="agent-1",
        owner_email="user@example.com",
        name="Agent",
        llm_config=AgentLLMConfig(model="test-model"),
        permissions=AgentPermissions(can_delegate=can_delegate, max_delegation_depth=max_depth),
    )


def test_build_routing_reminder_matches_implementation_work() -> None:
    reminder = build_routing_reminder("Implement refresh token support across the API.")
    assert reminder is not None
    assert reminder.category == "implementation"
    assert "background task" in reminder.reminder


def test_build_routing_reminder_skips_blank_and_explicit_override_messages() -> None:
    assert build_routing_reminder("") is None
    assert build_routing_reminder("/implement auth") is None
    assert build_routing_reminder("Just answer this directly.") is None


def test_build_routing_reminder_avoids_broad_false_positives() -> None:
    assert build_routing_reminder("What is the current model setting?") is None
    assert build_routing_reminder("Can you help me fix my explanation?") is None
    assert build_routing_reminder("Can you build the argument for this approach?") is None


@pytest.mark.asyncio
async def test_decision_engine_honors_explicit_inline_override() -> None:
    engine = DecisionEngine(llm=_LLM(), inline_max_length=200, max_delegation_depth=5)
    result = await engine.decide(user_message="Just answer this directly.", agent=_agent())
    assert result.decision == "inline"
    assert result.override_source == "keyword"


@pytest.mark.asyncio
async def test_decision_engine_defaults_ambiguous_to_inline() -> None:
    """Ambiguous messages default to inline — the agent decides via tools."""
    engine = DecisionEngine(llm=_LLM(), inline_max_length=20, max_delegation_depth=5)
    result = await engine.decide(
        user_message="Please analyze this architecture and plan the implementation steps.",
        agent=_agent(),
    )
    assert result.decision == "inline"
    assert result.confidence == 0.9
    assert result.degraded is False


@pytest.mark.asyncio
async def test_decision_engine_leaves_task_slash_commands_inline() -> None:
    """Slash commands are owned by CommandDispatcher, not DecisionEngine."""
    engine = DecisionEngine(llm=_LLM(), inline_max_length=200, max_delegation_depth=5)
    result = await engine.decide(
        user_message="/research best practices for async Python", agent=_agent()
    )
    assert result.decision == "inline"
    assert result.override_source is None


@pytest.mark.asyncio
async def test_decision_engine_does_not_parse_delegate_prefix_at_depth_limit() -> None:
    engine = DecisionEngine(llm=_LLM(), inline_max_length=200, max_delegation_depth=3)
    result = await engine.decide(
        user_message="/delegate this task", agent=_agent(max_depth=3), current_depth=3
    )
    assert result.decision == "inline"
    assert "limit" in result.reason.lower()


@pytest.mark.asyncio
async def test_decision_engine_does_not_delegate_natural_language_task_queries() -> None:
    engine = DecisionEngine(llm=_LLM(), inline_max_length=200, max_delegation_depth=5)
    for message in (
        "run in background",
        "background task status",
        "query for task",
        "continue?",
        "/taskfoo create something",
    ):
        result = await engine.decide(user_message=message, agent=_agent())
        assert result.decision == "inline"


@pytest.mark.asyncio
async def test_decision_engine_conversational_is_inline() -> None:
    engine = DecisionEngine(llm=_LLM(), inline_max_length=200, max_delegation_depth=5)
    result = await engine.decide(user_message="hello, how are you?", agent=_agent())
    assert result.decision == "inline"
    assert result.confidence == 0.85
