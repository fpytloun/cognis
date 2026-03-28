"""Tests for the step evaluator."""

from __future__ import annotations

import asyncio
import json

import pytest

from cognis.core.step_evaluator import StepEvaluator
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
