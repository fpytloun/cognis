from __future__ import annotations

import pytest

from cognis.core.decision import DecisionEngine, build_routing_reminder, select_workflow
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


class _SequenceWorkflowLLM:
    def __init__(self, responses: list[dict[str, object]]) -> None:
        self._responses = list(responses)
        self.calls = 0

    async def generate(
        self,
        messages: list[dict[str, object]],
        model: str | None = None,
        task_type: str = "default",
        **kwargs: object,
    ) -> dict[str, object]:
        del messages, model, task_type, kwargs
        self.calls += 1
        return self._responses.pop(0)


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
    assert "inline execution is fine" in reminder.reminder


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


@pytest.mark.asyncio
async def test_select_workflow_classifier_falls_back_to_plain_json_text() -> None:
    llm = _SequenceWorkflowLLM(
        [
            {"choices": [{"message": {"content": ""}}]},
            {
                "choices": [
                    {
                        "message": {
                            "content": '{"workflow_id": "system:software-development", "confidence": 0.8, "reason": "implementation work"}'
                        }
                    }
                ]
            },
        ]
    )

    result = await select_workflow(
        llm=llm,
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

    assert llm.calls == 2
    assert result.workflow_id == "system:software-development"


@pytest.mark.asyncio
async def test_select_workflow_uses_research_heuristic_before_classifier() -> None:
    llm = _LLM()

    result = await select_workflow(
        llm=llm,
        task_description="Research best MCP server for Twitter/X",
        available_workflows=[
            {
                "workflow_id": "system:general-task",
                "name": "General Task",
                "criteria": "Generic execution",
            },
            {
                "workflow_id": "system:research",
                "name": "Research",
                "criteria": "Research and investigation",
            },
        ],
        default_workflow_id="system:general-task",
    )

    assert result.workflow_id == "system:research"
