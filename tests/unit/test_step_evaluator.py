"""Tests for the step evaluator."""

from __future__ import annotations

import asyncio
import json

import pytest

from cognis.core.step_evaluator import (
    DEFAULT_EVALUATOR_TIMEOUT_MS,
    StepEvaluator,
    is_evaluator_malfunction,
)
from cognis.models.workflow import CompletionConfig, StepDefinition, StepOutput


class _LLM:
    """Stub LLM provider for testing."""

    def __init__(
        self,
        *,
        response: str = '{"decision": "approved", "reasoning": "Good work", "feedback": null}',
        delay: float = 0.0,
        fail: bool = False,
    ) -> None:
        self._response = response
        self._delay = delay
        self._fail = fail

    async def generate(
        self,
        messages: list[dict[str, object]],
        task_type: str = "default",
        **kwargs: object,
    ) -> dict[str, object]:
        if self._delay:
            await asyncio.sleep(self._delay)
        if self._fail:
            raise RuntimeError("LLM error")
        return {"choices": [{"message": {"content": self._response}}]}


class _SequenceLLM:
    def __init__(self, responses: list[dict[str, object]]) -> None:
        self._responses = list(responses)
        self.calls = 0

    async def generate(
        self,
        messages: list[dict[str, object]],
        task_type: str = "default",
        **kwargs: object,
    ) -> dict[str, object]:
        del messages, task_type, kwargs
        self.calls += 1
        return self._responses.pop(0)


class _CaptureLLM:
    def __init__(self) -> None:
        self.messages: list[dict[str, object]] | None = None

    async def generate(
        self,
        messages: list[dict[str, object]],
        task_type: str = "default",
        **kwargs: object,
    ) -> dict[str, object]:
        del task_type, kwargs
        self.messages = messages
        return {
            "choices": [{"message": {"content": '{"decision": "approved", "reasoning": "ok"}'}}]
        }


class _SessionFactory:
    def __init__(self, value: object) -> None:
        self.value = value

    def __call__(self) -> object:
        value = self.value

        class _Context:
            async def __aenter__(self) -> object:
                return value

            async def __aexit__(self, exc_type: object, exc: object, tb: object) -> bool:
                return False

        return _Context()


def _step_def(prompt: str = "Implement the feature") -> StepDefinition:
    return StepDefinition(
        name="implement",
        type="run",
        prompt=prompt,
        completion=CompletionConfig(evaluate=True),
    )


def _step_output(summary: str = "Done", claims: list[str] | None = None) -> StepOutput:
    return StepOutput(
        summary=summary,
        content="Implemented the feature.",
        outputs={"result": "ok"},
        metadata={"status": "complete"},
        claims=claims or ["Implemented the feature"],
    )


@pytest.mark.asyncio
async def test_evaluator_returns_approved() -> None:
    evaluator = StepEvaluator(llm=_LLM(), evaluator_timeout_seconds=5.0)

    result = await evaluator.evaluate(
        step_definition=_step_def(),
        step_output=_step_output(),
        step_inputs={},
        task_context="Build a REST API",
    )

    assert result.decision == "approved"
    assert result.reasoning == "Good work"
    assert result.evaluated_at is not None


@pytest.mark.asyncio
async def test_evaluator_returns_revise() -> None:
    response = json.dumps(
        {
            "decision": "revise",
            "reasoning": "Tests are missing",
            "feedback": "Add unit tests for the API endpoints",
        }
    )
    evaluator = StepEvaluator(llm=_LLM(response=response), evaluator_timeout_seconds=5.0)

    result = await evaluator.evaluate(
        step_definition=_step_def("Implement with tests"),
        step_output=_step_output(claims=["Implemented feature"]),
        step_inputs={},
    )

    assert result.decision == "revise"
    assert "tests" in result.feedback.lower()


@pytest.mark.asyncio
async def test_evaluator_timeout_forces_failure() -> None:
    evaluator = StepEvaluator(llm=_LLM(delay=1.0), evaluator_timeout_seconds=0.01)

    result = await evaluator.evaluate(
        step_definition=_step_def(),
        step_output=_step_output(),
        step_inputs={},
    )

    assert result.decision == "failed"
    assert is_evaluator_malfunction(result) is True
    assert "timed out" in result.reasoning.lower()


@pytest.mark.asyncio
async def test_from_session_factory_uses_seeded_timeout_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _fake_get_setting_value(session: object, key: str, default: object = None) -> object:
        del session
        assert key == "evaluator.timeout_ms"
        assert default == DEFAULT_EVALUATOR_TIMEOUT_MS
        return default

    monkeypatch.setattr("cognis.core.step_evaluator.get_setting_value", _fake_get_setting_value)

    evaluator = await StepEvaluator.from_session_factory(
        session_factory=_SessionFactory(object()),
        llm=_LLM(),
    )

    assert evaluator.evaluator_timeout_seconds == DEFAULT_EVALUATOR_TIMEOUT_MS / 1000


def test_constructor_uses_seeded_timeout_default() -> None:
    evaluator = StepEvaluator(llm=_LLM())

    assert evaluator.evaluator_timeout_seconds == DEFAULT_EVALUATOR_TIMEOUT_MS / 1000


@pytest.mark.asyncio
async def test_evaluator_error_forces_failure() -> None:
    evaluator = StepEvaluator(llm=_LLM(fail=True), evaluator_timeout_seconds=5.0)

    result = await evaluator.evaluate(
        step_definition=_step_def(),
        step_output=_step_output(),
        step_inputs={},
    )

    assert result.decision == "failed"
    assert is_evaluator_malfunction(result) is True
    assert "failed" in result.reasoning.lower()


@pytest.mark.asyncio
async def test_evaluator_middle_truncates_large_deliverable_content() -> None:
    capture = _CaptureLLM()
    evaluator = StepEvaluator(llm=capture, evaluator_timeout_seconds=5.0)
    output = StepOutput(
        summary="Done",
        content="start-" + ("x" * 40_000) + "-end",
        claims=["Implemented feature"],
    )

    result = await evaluator.evaluate(
        step_definition=_step_def(),
        step_output=output,
        step_inputs={},
    )

    assert result.decision == "approved"
    assert capture.messages is not None
    prompt = str(capture.messages[1]["content"])
    assert "start-" in prompt
    assert "-end" in prompt
    assert len(prompt) < 30_000


@pytest.mark.asyncio
async def test_evaluator_prompt_uses_plain_deliverable_text_exactly() -> None:
    capture = _CaptureLLM()
    evaluator = StepEvaluator(llm=capture, evaluator_timeout_seconds=5.0)
    plain_text = "🏠 Osobní\nHotovo.\n\nPozn.: závěrečná věta."

    result = await evaluator.evaluate(
        step_definition=_step_def("Write a plain evening summary."),
        step_output=StepOutput(
            summary="Summary written",
            content=plain_text,
            deliverable_id="dlv_plain",
            deliverable_format="plain",
        ),
        step_inputs={},
    )

    assert result.decision == "approved"
    assert capture.messages is not None
    prompt = str(capture.messages[1]["content"])
    assert "Assistant written deliverable:" in prompt
    assert plain_text in prompt
    assert "Execution evidence:" in prompt
    assert "('🏠 Osobní" not in prompt
    assert "', False)" not in prompt


@pytest.mark.asyncio
async def test_evaluator_rejects_structural_only_pulse_v2_before_llm() -> None:
    capture = _CaptureLLM()
    evaluator = StepEvaluator(llm=capture, evaluator_timeout_seconds=5.0)
    output = _step_output()
    output.metadata = {
        "deliverable_render_metadata": {
            "pulse_version": 2,
            "pulse_quality": {
                "quality_gate_passed": False,
                "visual_count": 0,
                "uncited_story_count": 2,
            },
        }
    }

    result = await evaluator.evaluate(
        step_definition=_step_def("Create a Pulse v2 daily brief."),
        step_output=output,
        step_inputs={},
    )

    assert result.decision == "revise"
    assert "structural-only" in str(result.feedback)
    assert capture.messages is None


@pytest.mark.asyncio
async def test_evaluator_prompt_consumes_passing_pulse_v2_quality_metadata() -> None:
    capture = _CaptureLLM()
    evaluator = StepEvaluator(llm=capture, evaluator_timeout_seconds=5.0)
    output = _step_output()
    output.metadata = {
        "deliverable_render_metadata": {
            "pulse_version": 2,
            "pulse_quality": {
                "quality_gate_passed": True,
                "visual_count": 1,
                "meaningful_chart_count": 1,
                "cited_story_count": 4,
                "uncited_story_count": 0,
                "collapsible_count": 2,
                "unavailable_count": 1,
            },
        }
    }

    result = await evaluator.evaluate(
        step_definition=_step_def("Create a Pulse v2 daily brief."),
        step_output=output,
        step_inputs={},
    )

    assert result.decision == "approved"
    assert capture.messages is not None
    prompt = str(capture.messages[1]["content"])
    assert '"quality_gate_passed": true' in prompt
    assert '"meaningful_chart_count": 1' in prompt
    assert "Structural composition alone is not success" in prompt


@pytest.mark.asyncio
async def test_evaluator_uses_step_inputs() -> None:
    capture = _CaptureLLM()
    evaluator = StepEvaluator(llm=capture, evaluator_timeout_seconds=5.0)

    plan_output = StepOutput(
        summary="Plan: build REST API",
        content="Plan deliverable requires backend and frontend work.",
        outputs={"scope": ["backend", "frontend"]},
        metadata={"scope_contract": [{"id": "frontend", "required": True}]},
        claims=["Planned frontend scope"],
    )
    result = await evaluator.evaluate(
        step_definition=_step_def(),
        step_output=_step_output(),
        step_inputs={"plan": plan_output},
    )

    assert result.decision == "approved"
    assert capture.messages is not None
    prompt = str(capture.messages[1]["content"])
    assert "Metadata:" in prompt
    assert "scope_contract" in prompt
    assert "Structured outputs" not in prompt
    assert "Outputs:" in prompt
    assert "Plan deliverable requires backend and frontend work" in prompt


# ---------------------------------------------------------------------------
# Robust parsing fallback tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_evaluator_json_in_code_fences() -> None:
    response = '```json\n{"decision": "revise", "reasoning": "tests missing", "feedback": "add tests"}\n```'
    evaluator = StepEvaluator(llm=_LLM(response=response), evaluator_timeout_seconds=5.0)

    result = await evaluator.evaluate(
        step_definition=_step_def("Implement with tests"),
        step_output=_step_output(claims=["Implemented feature"]),
        step_inputs={},
    )

    assert result.decision == "revise"
    assert "tests" in result.reasoning.lower()


@pytest.mark.asyncio
async def test_evaluator_json_in_prose() -> None:
    response = (
        "After analysis, here is my evaluation:\n"
        '{"decision": "failed", "reasoning": "API is broken", "feedback": "fix the endpoint"}\n'
        "Please review."
    )
    evaluator = StepEvaluator(llm=_LLM(response=response), evaluator_timeout_seconds=5.0)

    result = await evaluator.evaluate(
        step_definition=_step_def(),
        step_output=_step_output(),
        step_inputs={},
    )

    assert result.decision == "failed"
    assert "broken" in result.reasoning.lower()


@pytest.mark.asyncio
async def test_evaluator_plain_text_revise_via_inference() -> None:
    """When the model returns plain text instead of JSON, semantic inference kicks in."""
    response = "The step is incomplete because unit tests are missing from the implementation."
    evaluator = StepEvaluator(llm=_LLM(response=response), evaluator_timeout_seconds=5.0)

    result = await evaluator.evaluate(
        step_definition=_step_def("Implement with tests"),
        step_output=_step_output(claims=["Implemented feature"]),
        step_inputs={},
    )

    assert result.decision == "revise"
    assert "Inferred from text" in result.reasoning


@pytest.mark.asyncio
async def test_evaluator_plain_text_failed_via_inference() -> None:
    response = "This step cannot succeed because the external API is fundamentally broken."
    evaluator = StepEvaluator(llm=_LLM(response=response), evaluator_timeout_seconds=5.0)

    result = await evaluator.evaluate(
        step_definition=_step_def(),
        step_output=_step_output(),
        step_inputs={},
    )

    assert result.decision == "failed"


@pytest.mark.asyncio
async def test_evaluator_empty_response_fails_evaluation() -> None:
    evaluator = StepEvaluator(llm=_LLM(response=""), evaluator_timeout_seconds=5.0)

    result = await evaluator.evaluate(
        step_definition=_step_def(),
        step_output=_step_output(),
        step_inputs={},
    )

    assert result.decision == "failed"
    assert is_evaluator_malfunction(result) is True
    assert "no usable output" in result.reasoning.lower()


@pytest.mark.asyncio
async def test_evaluator_leaves_transport_fallback_to_provider() -> None:
    llm = _SequenceLLM([{"choices": [{"message": {"content": ""}}]}])
    evaluator = StepEvaluator(llm=llm, evaluator_timeout_seconds=5.0)

    result = await evaluator.evaluate(
        step_definition=_step_def(),
        step_output=_step_output(),
        step_inputs={},
    )

    assert llm.calls == 1
    assert result.decision == "failed"
    assert is_evaluator_malfunction(result) is True


@pytest.mark.asyncio
async def test_evaluator_incomplete_response_fails_evaluation() -> None:
    llm = _SequenceLLM(
        [
            {
                "choices": [
                    {
                        "message": {"content": '{"decision":'},
                        "finish_reason": "length",
                    }
                ]
            },
            {
                "choices": [
                    {
                        "message": {"content": '{"decision":'},
                        "finish_reason": "length",
                    }
                ]
            },
            {
                "choices": [
                    {
                        "message": {"content": '{"decision":'},
                        "finish_reason": "length",
                    }
                ]
            },
        ]
    )
    evaluator = StepEvaluator(llm=llm, evaluator_timeout_seconds=5.0)

    result = await evaluator.evaluate(
        step_definition=_step_def(),
        step_output=_step_output(),
        step_inputs={},
    )

    assert result.decision == "failed"
    assert is_evaluator_malfunction(result) is True
    assert "incomplete" in result.reasoning.lower()


@pytest.mark.asyncio
async def test_evaluator_refusal_forces_revise() -> None:
    llm = _SequenceLLM(
        [
            {
                "choices": [
                    {
                        "message": {"content": None, "refusal": "Cannot comply"},
                    }
                ]
            }
        ]
    )
    evaluator = StepEvaluator(llm=llm, evaluator_timeout_seconds=5.0)

    result = await evaluator.evaluate(
        step_definition=_step_def(),
        step_output=_step_output(),
        step_inputs={},
    )

    assert result.decision == "revise"
    assert "refused" in result.reasoning.lower()


@pytest.mark.asyncio
async def test_evaluator_empty_provider_response_fails_evaluation() -> None:
    llm = _SequenceLLM(
        [
            {"choices": [{"message": {"content": ""}}]},
        ]
    )
    evaluator = StepEvaluator(llm=llm, evaluator_timeout_seconds=5.0)

    result = await evaluator.evaluate(
        step_definition=_step_def(),
        step_output=_step_output(),
        step_inputs={},
    )

    assert result.decision == "failed"
    assert is_evaluator_malfunction(result) is True
    assert "no usable output" in result.reasoning.lower()


@pytest.mark.asyncio
async def test_evaluator_invalid_decision_fails_evaluation() -> None:
    response = json.dumps({"decision": "maybe", "reasoning": "not sure"})
    evaluator = StepEvaluator(llm=_LLM(response=response), evaluator_timeout_seconds=5.0)

    result = await evaluator.evaluate(
        step_definition=_step_def(),
        step_output=_step_output(),
        step_inputs={},
    )

    assert result.decision == "failed"
    assert is_evaluator_malfunction(result) is True


@pytest.mark.asyncio
async def test_evaluator_prompt_includes_full_long_content() -> None:
    capture = _CaptureLLM()
    evaluator = StepEvaluator(llm=capture, evaluator_timeout_seconds=5.0)
    long_review = (
        "### Summary\nReview overview.\n\n"
        "### Strengths\n- Reuses existing workflow state.\n\n"
        + ("Filler paragraph for a long review.\n" * 300)
        + "### Verdict\n**REQUEST REWORK**\n"
    )

    result = await evaluator.evaluate(
        step_definition=_step_def("Review the implementation plan"),
        step_output=StepOutput(
            summary="Review complete",
            content=long_review,
            outputs={"verdict": "REQUEST REWORK"},
            claims=["Included Strengths and Verdict"],
        ),
        step_inputs={},
    )

    assert result.decision == "approved"
    assert capture.messages is not None
    prompt = str(capture.messages[1]["content"])
    assert "### Strengths" in prompt
    assert "### Verdict" in prompt
    assert "truncated" not in prompt


@pytest.mark.asyncio
async def test_evaluator_prompt_explicitly_allows_success_for_review_steps() -> None:
    capture = _CaptureLLM()
    evaluator = StepEvaluator(llm=capture, evaluator_timeout_seconds=5.0)

    result = await evaluator.evaluate(
        step_definition=_step_def("Review the implementation plan and approve it if it is sound."),
        step_output=StepOutput(
            summary="Review complete",
            content="The revised plan is sound and ready for implementation.",
            claims=["Reviewed the plan and found no blocking issues"],
        ),
        step_inputs={},
    )

    assert result.decision == "approved"
    assert capture.messages is not None
    prompt = str(capture.messages[1]["content"])
    assert 'outcome of "success"' in prompt
    assert '"rejected"' in prompt
    assert '"failed"' in prompt
    assert "Success is valid when the review approves the plan or work" in prompt
    assert 'self-reported outcome of "failed"' in prompt
    assert "workflow can continue" in prompt


@pytest.mark.asyncio
async def test_evaluator_prompt_separates_prose_metadata_and_execution_evidence() -> None:
    capture = _CaptureLLM()
    evaluator = StepEvaluator(llm=capture, evaluator_timeout_seconds=5.0)

    result = await evaluator.evaluate(
        step_definition=_step_def("Implement the feature with tests."),
        step_output=StepOutput(
            summary="Implemented /todo",
            content="Implemented the slash command and added tests.",
            claims=["Added /todo command", "Added unit tests"],
        ),
        step_inputs={},
        execution_evidence={
            "tools": [{"name": "edit", "ok": True}],
            "files_written": [{"path": "cognis/core/commands.py"}],
            "commands": [
                {
                    "program": "pytest",
                    "summary": "uv run pytest tests/unit/test_commands.py -q",
                    "cwd": "/repo",
                    "ok": True,
                    "exit_code": 0,
                }
            ],
        },
    )

    assert result.decision == "approved"
    assert capture.messages is not None
    prompt = str(capture.messages[1]["content"])
    assert "Assistant written deliverable:" in prompt
    assert "step_complete metadata:" in prompt
    assert "Notification:" in prompt
    assert "Execution evidence:" in prompt
    assert "require step_complete metadata" in prompt
    assert "Expected output as strong guidance for output shape" in prompt
    assert "Silent completion can be valid" in prompt
    assert "Direct completion can also be valid" in prompt
