"""Tests for the step context assembler and StepInputConfig model."""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock

import pytest

from cognis.core.context import ContextAssemblyResult, events_to_messages
from cognis.core.step_context import StepContextAssembler
from cognis.models.agent import AgentDefinition, AgentLLMConfig
from cognis.models.session import ConversationContext, ConversationModel, SessionModel
from cognis.models.workflow import (
    StepDefinition,
    StepInputConfig,
    StepOutput,
    WorkflowState,
    resolve_effective_input,
    resolve_source_names,
)

# ---------------------------------------------------------------------------
# StepInputConfig model tests
# ---------------------------------------------------------------------------


def test_step_input_config_null() -> None:
    config = StepInputConfig(type="null")
    assert config.type == "null"
    assert config.source_names() == []


def test_step_input_config_last_single_source() -> None:
    config = StepInputConfig(type="last", source="plan")
    assert config.source_names() == ["plan"]
    assert config.single_source() == "plan"


def test_step_input_config_last_multiple_sources() -> None:
    config = StepInputConfig(type="last", source=["plan", "review"])
    assert config.source_names() == ["plan", "review"]


def test_step_input_config_full_single_source() -> None:
    config = StepInputConfig(type="full", source="plan")
    assert config.single_source() == "plan"


def test_step_input_config_full_rejects_list_source() -> None:
    with pytest.raises(ValueError, match="single source"):
        StepInputConfig(type="full", source=["plan", "review"])


def test_step_input_config_summary_multiple_sources() -> None:
    config = StepInputConfig(type="summary", source=["plan", "research"])
    assert config.source_names() == ["plan", "research"]


# ---------------------------------------------------------------------------
# Backward compatibility — legacy list[str] coercion
# ---------------------------------------------------------------------------


def test_step_definition_coerces_legacy_list_input() -> None:
    step = StepDefinition(name="impl", type="run", input=["plan"])  # type: ignore[arg-type]
    assert step.input is not None
    assert step.input.type == "last"
    assert step.input.source_names() == ["plan"]


def test_step_definition_coerces_legacy_multi_list_input() -> None:
    step = StepDefinition(name="impl", type="run", input=["plan", "review"])  # type: ignore[arg-type]
    assert step.input is not None
    assert step.input.type == "last"
    assert step.input.source_names() == ["plan", "review"]


def test_step_definition_coerces_legacy_string_input() -> None:
    step = StepDefinition(name="impl", type="run", input="plan")  # type: ignore[arg-type]
    assert step.input is not None
    assert step.input.type == "last"
    assert step.input.single_source() == "plan"


def test_step_definition_accepts_none_input() -> None:
    step = StepDefinition(name="plan", type="run")
    assert step.input is None


def test_step_definition_accepts_structured_input() -> None:
    step = StepDefinition(
        name="impl",
        type="run",
        input=StepInputConfig(type="summary", source=["plan", "review"]),
    )
    assert step.input is not None
    assert step.input.type == "summary"


def test_step_definition_accepts_dict_input() -> None:
    step = StepDefinition(
        name="impl",
        type="run",
        input={"type": "full", "source": "plan"},  # type: ignore[arg-type]
    )
    assert step.input is not None
    assert step.input.type == "full"
    assert step.input.single_source() == "plan"


def test_step_definition_coerces_empty_list_to_none() -> None:
    step = StepDefinition(name="plan", type="run", input=[])  # type: ignore[arg-type]
    assert step.input is None


# ---------------------------------------------------------------------------
# Default input resolution
# ---------------------------------------------------------------------------


def test_resolve_effective_input_first_step_defaults_to_null() -> None:
    steps = [StepDefinition(name="plan", type="run")]
    effective = resolve_effective_input(steps[0], 0, steps)
    assert effective.type == "null"


def test_resolve_effective_input_non_first_step_defaults_to_last_from_previous() -> None:
    steps = [
        StepDefinition(name="plan", type="run"),
        StepDefinition(name="implement", type="run"),
    ]
    effective = resolve_effective_input(steps[1], 1, steps)
    assert effective.type == "last"
    assert effective.source_names() == ["plan"]


def test_resolve_effective_input_uses_explicit_config() -> None:
    steps = [
        StepDefinition(name="plan", type="run"),
        StepDefinition(
            name="implement",
            type="run",
            input=StepInputConfig(type="full", source="plan"),
        ),
    ]
    effective = resolve_effective_input(steps[1], 1, steps)
    assert effective.type == "full"
    assert effective.single_source() == "plan"


def test_resolve_source_names_uses_shared_helper() -> None:
    steps = [
        StepDefinition(name="plan", type="run"),
        StepDefinition(
            name="implement",
            type="run",
            input=StepInputConfig(type="last", source=["plan"]),
        ),
    ]
    names = resolve_source_names(steps[1], 1, steps)
    assert names == ["plan"]


def test_resolve_effective_input_last_without_source_defaults_to_previous() -> None:
    """Explicit type=last with no source should default to previous step."""
    steps = [
        StepDefinition(name="plan", type="run"),
        StepDefinition(
            name="implement",
            type="run",
            input=StepInputConfig(type="last"),
        ),
    ]
    effective = resolve_effective_input(steps[1], 1, steps)
    assert effective.type == "last"
    assert effective.source_names() == ["plan"]


def test_resolve_effective_input_summary_without_source_defaults_to_previous() -> None:
    """Explicit type=summary with no source should default to previous step."""
    steps = [
        StepDefinition(name="plan", type="run"),
        StepDefinition(
            name="implement",
            type="run",
            input=StepInputConfig(type="summary"),
        ),
    ]
    effective = resolve_effective_input(steps[1], 1, steps)
    assert effective.type == "summary"
    assert effective.source_names() == ["plan"]


def test_resolve_effective_input_last_without_source_first_step_becomes_null() -> None:
    """First step with type=last but no source should degrade to null."""
    steps = [
        StepDefinition(
            name="plan",
            type="run",
            input=StepInputConfig(type="last"),
        ),
    ]
    effective = resolve_effective_input(steps[0], 0, steps)
    assert effective.type == "null"


# ---------------------------------------------------------------------------
# StepOutput backward-compatible parsing
# ---------------------------------------------------------------------------


def test_step_output_parses_old_format_without_session_metadata() -> None:
    raw = {"summary": "Plan created", "outputs": {"plan": "test"}, "claims": ["Created plan"]}
    output = StepOutput.model_validate(raw)
    assert output.summary == "Plan created"
    assert output.completed_at is None
    assert output.session_id is None
    assert output.intaris_session_id is None


def test_step_output_parses_new_format_with_session_metadata() -> None:
    raw = {
        "summary": "Plan created",
        "outputs": {},
        "claims": [],
        "completed_at": "2026-03-29T12:00:00Z",
        "session_id": "ses-1",
        "intaris_session_id": "intaris-1",
    }
    output = StepOutput.model_validate(raw)
    assert output.session_id == "ses-1"
    assert output.intaris_session_id == "intaris-1"
    assert output.completed_at is not None


# ---------------------------------------------------------------------------
# WorkflowState source resolution
# ---------------------------------------------------------------------------


def test_workflow_state_get_source_intaris_session_id_present() -> None:
    state = WorkflowState()
    state.step_outputs["plan"] = {
        "summary": "Done",
        "outputs": {},
        "claims": [],
        "intaris_session_id": "intaris-plan",
        "session_id": "ses-plan",
    }
    assert state.get_source_intaris_session_id("plan") == "intaris-plan"


def test_workflow_state_get_source_intaris_session_id_missing_step() -> None:
    state = WorkflowState()
    with pytest.raises(ValueError, match="No output found"):
        state.get_source_intaris_session_id("nonexistent")


def test_workflow_state_get_source_intaris_session_id_missing_field() -> None:
    state = WorkflowState()
    state.step_outputs["plan"] = {"summary": "Done", "outputs": {}, "claims": []}
    with pytest.raises(ValueError, match="missing intaris_session_id"):
        state.get_source_intaris_session_id("plan")


# ---------------------------------------------------------------------------
# Shared event-to-message formatter
# ---------------------------------------------------------------------------


def test_events_to_messages_handles_dict_events() -> None:
    events = [
        {"type": "user_message", "data": {"content": "hello"}},
        {"type": "assistant_message", "data": {"content": "hi"}},
    ]
    messages = events_to_messages(events)
    assert len(messages) == 2
    assert messages[0] == {"role": "user", "content": "hello"}
    assert messages[1] == {"role": "assistant", "content": "hi"}


def test_events_to_messages_handles_evaluation_feedback() -> None:
    events = [
        {
            "type": "evaluation_feedback",
            "data": {"attempt": 1, "decision": "revise", "feedback": "Add tests"},
        },
    ]
    messages = events_to_messages(events)
    assert len(messages) == 1
    assert "evaluation_feedback" in messages[0]["content"]
    assert "Add tests" in messages[0]["content"]


def test_events_to_messages_handles_tool_call_with_name_field() -> None:
    """Tool calls recorded by agent_loop use 'name' not 'tool_name'."""
    events = [{"type": "tool_call", "data": {"name": "filesystem/read_file"}}]
    messages = events_to_messages(events)
    assert len(messages) == 1
    assert "filesystem/read_file" in messages[0]["content"]


# ---------------------------------------------------------------------------
# StepContextAssembler
# ---------------------------------------------------------------------------

_STEP_PROMPT = "## Step: implement\n\nImplement the plan."


def _agent() -> AgentDefinition:
    return AgentDefinition(
        agent_id="agent-1",
        owner_email="user@example.com",
        name="Agent",
        system_prompt="You are helpful.",
        llm_config=AgentLLMConfig(model="test-model", max_tokens=128),
    )


def _conversation() -> ConversationModel:
    return ConversationModel(
        conversation_id="conv-1",
        user_email="user@example.com",
        agent_id="agent-1",
        context=ConversationContext(type="web"),
    )


def _session() -> SessionModel:
    return SessionModel(
        session_id="session-1",
        conversation_id="conv-1",
        user_email="user@example.com",
        agent_id="agent-1",
        intaris_session_id="session-1",
    )


def _base_result() -> ContextAssemblyResult:
    return ContextAssemblyResult(
        messages=[{"role": "system", "content": "You are helpful."}],
        resolved_model="test-model",
        static_tokens=50,
        dynamic_tokens=4000,
        prompt_tokens=50,
    )


def _mock_assembler() -> Any:
    assembler = AsyncMock()
    assembler.assemble.return_value = _base_result()
    return assembler


class _MockSessionCache:
    def get_entry(self, session_id: str) -> None:
        return None


class _MockGuardrails:
    def __init__(self, events: list[dict[str, Any]] | None = None) -> None:
        self.events = events or []

    async def read_events(self, session_id: str, after_seq: int = 0, **kwargs: Any) -> Any:
        from types import SimpleNamespace

        return SimpleNamespace(events=self.events, last_seq=len(self.events))


class _MockLLM:
    def __init__(self, summary_text: str = "Summary of the step.") -> None:
        self.summary_text = summary_text

    async def generate(self, messages: list[dict[str, Any]], **kwargs: Any) -> dict[str, Any]:
        return {"choices": [{"message": {"content": self.summary_text}}]}

    def count_messages_tokens(self, messages: list[dict[str, Any]], model: str) -> int:
        return sum(max(1, len(str(m.get("content", ""))) // 4) for m in messages)


@pytest.mark.asyncio
async def test_step_context_null_input() -> None:
    sca = StepContextAssembler(
        context_assembler=_mock_assembler(),
        session_cache=_MockSessionCache(),
        guardrails=_MockGuardrails(),
        llm=_MockLLM(),
    )
    steps = [StepDefinition(name="plan", type="run", input=StepInputConfig(type="null"))]
    state = WorkflowState()

    result = await sca.assemble(
        session=_session(),
        conversation=_conversation(),
        agent=_agent(),
        step_definition=steps[0],
        step_index=0,
        workflow_steps=steps,
        workflow_state=state,
        step_prompt="Create a plan.",
    )

    # Should have base messages + step prompt, no step input context
    assert result.messages[-1]["role"] == "user"
    assert "Create a plan." in result.messages[-1]["content"]
    # No <step_output> or <step_context> blocks
    all_content = " ".join(m.get("content", "") for m in result.messages)
    assert "<step_output" not in all_content
    assert "<step_context" not in all_content


@pytest.mark.asyncio
async def test_step_context_last_input() -> None:
    sca = StepContextAssembler(
        context_assembler=_mock_assembler(),
        session_cache=_MockSessionCache(),
        guardrails=_MockGuardrails(),
        llm=_MockLLM(),
    )
    steps = [
        StepDefinition(name="plan", type="run"),
        StepDefinition(
            name="implement",
            type="run",
            input=StepInputConfig(type="last", source="plan"),
        ),
    ]
    state = WorkflowState()
    state.step_outputs["plan"] = {
        "summary": "Created 5-step plan",
        "outputs": {"plan": ["step1", "step2"]},
        "claims": ["covers auth"],
        "intaris_session_id": "intaris-plan",
        "session_id": "ses-plan",
    }

    result = await sca.assemble(
        session=_session(),
        conversation=_conversation(),
        agent=_agent(),
        step_definition=steps[1],
        step_index=1,
        workflow_steps=steps,
        workflow_state=state,
        step_prompt="Implement the plan.",
    )

    all_content = " ".join(m.get("content", "") for m in result.messages)
    assert '<step_output source="plan">' in all_content
    assert "Created 5-step plan" in all_content
    assert "covers auth" in all_content
    # Step prompt is the last message
    assert result.messages[-1]["role"] == "user"


@pytest.mark.asyncio
async def test_step_context_last_multi_source_preserves_order() -> None:
    sca = StepContextAssembler(
        context_assembler=_mock_assembler(),
        session_cache=_MockSessionCache(),
        guardrails=_MockGuardrails(),
        llm=_MockLLM(),
    )
    steps = [
        StepDefinition(name="plan", type="run"),
        StepDefinition(name="review", type="run"),
        StepDefinition(
            name="implement",
            type="run",
            input=StepInputConfig(type="last", source=["plan", "review"]),
        ),
    ]
    state = WorkflowState()
    state.step_outputs["plan"] = {"summary": "Plan output", "outputs": {}, "claims": []}
    state.step_outputs["review"] = {"summary": "Review output", "outputs": {}, "claims": []}

    result = await sca.assemble(
        session=_session(),
        conversation=_conversation(),
        agent=_agent(),
        step_definition=steps[2],
        step_index=2,
        workflow_steps=steps,
        workflow_state=state,
        step_prompt="Implement.",
    )

    # Find step_output blocks — plan should come before review
    step_output_msgs = [m for m in result.messages if "<step_output" in m.get("content", "")]
    assert len(step_output_msgs) == 2
    assert 'source="plan"' in step_output_msgs[0]["content"]
    assert 'source="review"' in step_output_msgs[1]["content"]


@pytest.mark.asyncio
async def test_step_context_summary_input() -> None:
    sca = StepContextAssembler(
        context_assembler=_mock_assembler(),
        session_cache=_MockSessionCache(),
        guardrails=_MockGuardrails(
            events=[
                {"type": "user_message", "data": {"content": "Create plan"}},
                {"type": "assistant_message", "data": {"content": "Here is the plan."}},
            ]
        ),
        llm=_MockLLM(summary_text="Concise summary of planning."),
    )
    steps = [
        StepDefinition(name="plan", type="run"),
        StepDefinition(
            name="implement",
            type="run",
            input=StepInputConfig(type="summary", source="plan"),
        ),
    ]
    state = WorkflowState()
    state.step_outputs["plan"] = {
        "summary": "Plan done",
        "outputs": {},
        "claims": [],
        "intaris_session_id": "intaris-plan",
        "session_id": "ses-plan",
    }

    result = await sca.assemble(
        session=_session(),
        conversation=_conversation(),
        agent=_agent(),
        step_definition=steps[1],
        step_index=1,
        workflow_steps=steps,
        workflow_state=state,
        step_prompt="Implement.",
    )

    all_content = " ".join(m.get("content", "") for m in result.messages)
    assert '<step_context source="plan" type="summary">' in all_content
    assert "Concise summary of planning." in all_content


@pytest.mark.asyncio
async def test_step_context_summary_timeout_falls_back_to_last() -> None:
    """If summary generation times out, should fall back to last."""

    class _SlowLLM:
        async def generate(self, messages: list[dict[str, Any]], **kwargs: Any) -> dict[str, Any]:
            await asyncio.sleep(10)
            return {"choices": [{"message": {"content": "Never reached"}}]}

        def count_messages_tokens(self, messages: list[dict[str, Any]], model: str) -> int:
            return 10

    sca = StepContextAssembler(
        context_assembler=_mock_assembler(),
        session_cache=_MockSessionCache(),
        guardrails=_MockGuardrails(
            events=[
                {"type": "user_message", "data": {"content": "test"}},
            ]
        ),
        llm=_SlowLLM(),
        summary_timeout_seconds=0.01,
    )
    steps = [
        StepDefinition(name="plan", type="run"),
        StepDefinition(
            name="implement",
            type="run",
            input=StepInputConfig(type="summary", source="plan"),
        ),
    ]
    state = WorkflowState()
    state.step_outputs["plan"] = {
        "summary": "Fallback plan output",
        "outputs": {},
        "claims": ["claim1"],
        "intaris_session_id": "intaris-plan",
        "session_id": "ses-plan",
    }

    result = await sca.assemble(
        session=_session(),
        conversation=_conversation(),
        agent=_agent(),
        step_definition=steps[1],
        step_index=1,
        workflow_steps=steps,
        workflow_state=state,
        step_prompt="Implement.",
    )

    all_content = " ".join(m.get("content", "") for m in result.messages)
    # Should have fallen back to <step_output> (last) instead of <step_context> (summary)
    assert "<step_output" in all_content
    assert "Fallback plan output" in all_content


@pytest.mark.asyncio
async def test_step_context_full_input_via_direct_intaris_read() -> None:
    """Full input with evicted cache — should read directly from Intaris."""
    events = [
        {"type": "user_message", "data": {"content": "Create plan"}},
        {"type": "assistant_message", "data": {"content": "Here is the plan."}},
    ]
    sca = StepContextAssembler(
        context_assembler=_mock_assembler(),
        session_cache=_MockSessionCache(),  # returns None (evicted)
        guardrails=_MockGuardrails(events=events),
        llm=_MockLLM(),
    )
    steps = [
        StepDefinition(name="plan", type="run"),
        StepDefinition(
            name="implement",
            type="run",
            input=StepInputConfig(type="full", source="plan"),
        ),
    ]
    state = WorkflowState()
    state.step_outputs["plan"] = {
        "summary": "Plan done",
        "outputs": {},
        "claims": [],
        "intaris_session_id": "intaris-plan",
        "session_id": "ses-plan",
    }

    result = await sca.assemble(
        session=_session(),
        conversation=_conversation(),
        agent=_agent(),
        step_definition=steps[1],
        step_index=1,
        workflow_steps=steps,
        workflow_state=state,
        step_prompt="Implement.",
    )

    all_content = " ".join(m.get("content", "") for m in result.messages)
    # Full input injects as message history, not XML blocks
    assert "Create plan" in all_content
    assert "Here is the plan." in all_content


@pytest.mark.asyncio
async def test_step_context_full_falls_back_to_summary_on_budget_overflow() -> None:
    """Full input exceeding token budget should fall back to summary."""
    large_events = [
        {"type": "user_message", "data": {"content": "x" * 20000}},
        {"type": "assistant_message", "data": {"content": "y" * 20000}},
    ]
    # Base result with very small dynamic budget
    small_base = ContextAssemblyResult(
        messages=[{"role": "system", "content": "You are helpful."}],
        resolved_model="test-model",
        static_tokens=50,
        dynamic_tokens=100,  # Very small — full will exceed this
        prompt_tokens=50,
    )
    mock_assembler = AsyncMock()
    mock_assembler.assemble.return_value = small_base

    sca = StepContextAssembler(
        context_assembler=mock_assembler,
        session_cache=_MockSessionCache(),
        guardrails=_MockGuardrails(events=large_events),
        llm=_MockLLM(summary_text="Budget fallback summary."),
    )
    steps = [
        StepDefinition(name="plan", type="run"),
        StepDefinition(
            name="implement",
            type="run",
            input=StepInputConfig(type="full", source="plan"),
        ),
    ]
    state = WorkflowState()
    state.step_outputs["plan"] = {
        "summary": "Plan done",
        "outputs": {},
        "claims": [],
        "intaris_session_id": "intaris-plan",
        "session_id": "ses-plan",
    }

    result = await sca.assemble(
        session=_session(),
        conversation=_conversation(),
        agent=_agent(),
        step_definition=steps[1],
        step_index=1,
        workflow_steps=steps,
        workflow_state=state,
        step_prompt="Implement.",
    )

    all_content = " ".join(m.get("content", "") for m in result.messages)
    # Should have fallen back to summary
    assert '<step_context source="plan" type="summary">' in all_content
    assert "Budget fallback summary." in all_content
