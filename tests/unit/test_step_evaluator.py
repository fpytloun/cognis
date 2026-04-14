"""Tests for the step evaluator."""

from __future__ import annotations

import asyncio
import json

import pytest

from cognis.core.step_evaluator import StepEvaluator, is_evaluator_malfunction
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
        outputs={"result": "ok"},
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
async def test_evaluator_timeout_defaults_to_approved() -> None:
    evaluator = StepEvaluator(llm=_LLM(delay=1.0), evaluator_timeout_seconds=0.01)

    result = await evaluator.evaluate(
        step_definition=_step_def(),
        step_output=_step_output(),
        step_inputs={},
    )

    assert result.decision == "approved"
    assert "timed out" in result.reasoning.lower()


@pytest.mark.asyncio
async def test_evaluator_error_defaults_to_approved() -> None:
    evaluator = StepEvaluator(llm=_LLM(fail=True), evaluator_timeout_seconds=5.0)

    result = await evaluator.evaluate(
        step_definition=_step_def(),
        step_output=_step_output(),
        step_inputs={},
    )

    assert result.decision == "approved"
    assert "error" in result.reasoning.lower()


@pytest.mark.asyncio
async def test_evaluator_uses_step_inputs() -> None:
    evaluator = StepEvaluator(llm=_LLM(), evaluator_timeout_seconds=5.0)

    plan_output = StepOutput(summary="Plan: build REST API", outputs={}, claims=[])
    result = await evaluator.evaluate(
        step_definition=_step_def(),
        step_output=_step_output(),
        step_inputs={"plan": plan_output},
    )

    assert result.decision == "approved"


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
async def test_evaluator_retries_empty_response_once() -> None:
    llm = _SequenceLLM(
        [
            {"choices": [{"message": {"content": ""}}]},
            {
                "choices": [
                    {"message": {"content": '{"decision": "approved", "reasoning": "Looks good"}'}}
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

    assert llm.calls == 2
    assert result.decision == "approved"


@pytest.mark.asyncio
async def test_evaluator_falls_back_to_plain_json_text_after_structured_retry() -> None:
    llm = _SequenceLLM(
        [
            {"choices": [{"message": {"content": ""}}]},
            {"choices": [{"message": {"content": ""}}]},
            {
                "choices": [
                    {
                        "message": {
                            "content": '{"decision": "revise", "reasoning": "Tests missing", "feedback": "Add tests"}'
                        }
                    }
                ]
            },
        ]
    )
    evaluator = StepEvaluator(llm=llm, evaluator_timeout_seconds=5.0)

    result = await evaluator.evaluate(
        step_definition=_step_def("Implement with tests"),
        step_output=_step_output(claims=["Implemented feature"]),
        step_inputs={},
    )

    assert llm.calls == 3
    assert result.decision == "revise"
    assert result.feedback == "Add tests"


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
async def test_evaluator_retry_failure_after_empty_fails_evaluation() -> None:
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
    assert "retry failed" in result.reasoning.lower()


@pytest.mark.asyncio
async def test_evaluator_invalid_decision_defaults_to_approved() -> None:
    response = json.dumps({"decision": "maybe", "reasoning": "not sure"})
    evaluator = StepEvaluator(llm=_LLM(response=response), evaluator_timeout_seconds=5.0)

    result = await evaluator.evaluate(
        step_definition=_step_def(),
        step_output=_step_output(),
        step_inputs={},
    )

    assert result.decision == "approved"


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
