from __future__ import annotations

import asyncio

import pytest

from cognis.core.decision import DecisionEngine
from cognis.models.agent import AgentDefinition, AgentLLMConfig, AgentPermissions


class _LLM:
    def __init__(self, delay: float = 0.0, payload: str | None = None) -> None:
        self.delay = delay
        self.payload = payload or (
            '{"decision":"delegate","reason":"complex task","confidence":0.9,"predicted_tool_intensity":"high"}'
        )

    async def generate(
        self,
        messages: list[dict[str, object]],
        model: str | None = None,
        task_type: str = "default",
        **kwargs: object,
    ) -> dict[str, object]:
        del messages, model, task_type, kwargs
        if self.delay:
            await asyncio.sleep(self.delay)
        return {"choices": [{"message": {"content": self.payload}}]}


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
        classifier_timeout_seconds=0.05,
        classifier_fallback="inline",
        max_delegation_depth=5,
    )

    result = await engine.decide(user_message="Just answer this directly.", agent=_agent())

    assert result.decision == "inline"
    assert result.override_source == "keyword"


@pytest.mark.asyncio
async def test_decision_engine_uses_classifier_for_ambiguous_messages() -> None:
    engine = DecisionEngine(
        llm=_LLM(),
        inline_max_length=20,
        classifier_timeout_seconds=0.1,
        classifier_fallback="inline",
        max_delegation_depth=5,
    )

    result = await engine.decide(
        user_message="Please analyze this architecture and plan the implementation steps.",
        agent=_agent(),
    )

    assert result.decision == "delegate"
    assert result.predicted_tool_intensity == "high"


@pytest.mark.asyncio
async def test_decision_engine_times_out_to_inline_fallback() -> None:
    engine = DecisionEngine(
        llm=_LLM(delay=0.2),
        inline_max_length=10,
        classifier_timeout_seconds=0.01,
        classifier_fallback="inline",
        max_delegation_depth=5,
    )

    result = await engine.decide(
        user_message="This should require classification because it is too long.",
        agent=_agent(),
    )

    assert result.decision == "inline"
    assert result.degraded is True


@pytest.mark.asyncio
async def test_decision_engine_blocks_delegation_when_depth_limit_reached() -> None:
    engine = DecisionEngine(
        llm=_LLM(),
        inline_max_length=200,
        classifier_timeout_seconds=0.05,
        classifier_fallback="inline",
        max_delegation_depth=3,
    )

    result = await engine.decide(
        user_message="/delegate this task",
        agent=_agent(max_depth=3),
        current_depth=3,
    )

    assert result.decision == "ask_user"
    assert "limit" in result.reason.lower()
