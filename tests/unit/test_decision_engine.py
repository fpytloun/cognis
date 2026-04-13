from __future__ import annotations

import pytest

from cognis.core.decision import DecisionEngine, select_workflow
from cognis.models.agent import AgentDefinition, AgentLLMConfig, AgentPermissions


class _LLM:
    """Stub LLM for testing (not used by the rules-only decision engine)."""

    async def generate(
        self,
        messages: list[dict[str, object]],
        model: str | None = None,
        task_type: str = "default",
        **kwargs: object,
    ) -> dict[str, object]:
        del messages, model, task_type, kwargs
        return {"choices": [{"message": {"content": "{}"}}]}


class _InvalidWorkflowLLM:
    async def generate(
        self,
        messages: list[dict[str, object]],
        model: str | None = None,
        task_type: str = "default",
        **kwargs: object,
    ) -> dict[str, object]:
        del messages, model, task_type, kwargs
        return {
            "choices": [
                {
                    "message": {
                        "content": '{"workflow_id": "system:unknown", "confidence": 0.9, "reason": "bad pick"}'
                    }
                }
            ]
        }


def _agent(can_delegate: bool = True, max_depth: int = 5) -> AgentDefinition:
    return AgentDefinition(
        agent_id="agent-1",
        owner_email="user@example.com",
        name="Agent",
        llm_config=AgentLLMConfig(model="test-model"),
        permissions=AgentPermissions(can_delegate=can_delegate, max_delegation_depth=max_depth),
    )


@pytest.mark.asyncio
async def test_decision_engine_honors_explicit_inline_override() -> None:
    engine = DecisionEngine(
        llm=_LLM(),
        inline_max_length=200,
        max_delegation_depth=5,
    )

    result = await engine.decide(user_message="Just answer this directly.", agent=_agent())

    assert result.decision == "inline"
    assert result.override_source == "keyword"


@pytest.mark.asyncio
async def test_decision_engine_defaults_ambiguous_to_inline() -> None:
    """Ambiguous messages default to inline — the agent decides via tools."""
    engine = DecisionEngine(
        llm=_LLM(),
        inline_max_length=20,
        max_delegation_depth=5,
    )

    result = await engine.decide(
        user_message="Please analyze this architecture and plan the implementation steps.",
        agent=_agent(),
    )

    # No LLM classifier — ambiguous messages go inline by default
    assert result.decision == "inline"
    assert result.confidence == 0.9
    assert result.degraded is False


@pytest.mark.asyncio
async def test_decision_engine_explicit_delegate_prefix() -> None:
    """Slash commands like /research trigger delegation."""
    engine = DecisionEngine(
        llm=_LLM(),
        inline_max_length=200,
        max_delegation_depth=5,
    )

    result = await engine.decide(
        user_message="/research best practices for async Python",
        agent=_agent(),
    )

    assert result.decision == "delegate"
    assert result.override_source == "keyword"


@pytest.mark.asyncio
async def test_decision_engine_blocks_delegation_when_depth_limit_reached() -> None:
    engine = DecisionEngine(
        llm=_LLM(),
        inline_max_length=200,
        max_delegation_depth=3,
    )

    result = await engine.decide(
        user_message="/delegate this task",
        agent=_agent(max_depth=3),
        current_depth=3,
    )

    assert result.decision == "ask_user"
    assert "limit" in result.reason.lower()


@pytest.mark.asyncio
async def test_decision_engine_conversational_is_inline() -> None:
    """Short conversational messages are always inline."""
    engine = DecisionEngine(
        llm=_LLM(),
        inline_max_length=200,
        max_delegation_depth=5,
    )

    result = await engine.decide(user_message="hello, how are you?", agent=_agent())

    assert result.decision == "inline"
    assert result.confidence == 0.85


@pytest.mark.asyncio
async def test_select_workflow_uses_general_task_when_no_workflows_available() -> None:
    result = await select_workflow(
        llm=_LLM(),
        task_description="Do something useful",
        available_workflows=[],
        default_workflow_id=None,
    )

    assert result.workflow_id == "system:general-task"


@pytest.mark.asyncio
async def test_select_workflow_invalid_classifier_pick_falls_back_to_default() -> None:
    result = await select_workflow(
        llm=_InvalidWorkflowLLM(),
        task_description="Implement slash command",
        available_workflows=[
            {
                "workflow_id": "system:general-task",
                "name": "General Task",
                "criteria": "Generic execution",
            },
            {
                "workflow_id": "system:software-development",
                "name": "Software Development",
                "criteria": "Implementation work",
            },
        ],
        default_workflow_id="system:general-task",
    )

    assert result.workflow_id == "system:general-task"
