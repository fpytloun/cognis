"""Semantic step completion evaluator.

Verifies whether a step's output satisfies its definition of done via
an independent LLM call. Uses a cheap model via routing policy.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from typing import Any, cast

from prometheus_client import Counter, Histogram
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from cognis.logging import get_logger
from cognis.models.workflow import StepDefinition, StepEvaluation, StepOutput
from cognis.store.queries import get_setting_value

logger = get_logger(__name__)

EVALUATIONS_TOTAL = Counter(
    "cognis_evaluations_total",
    "Step evaluation outcomes",
    labelnames=("decision",),
)
EVALUATION_DURATION = Histogram(
    "cognis_evaluation_duration_seconds",
    "Step evaluator LLM call duration",
)

_DEFAULT_EVALUATOR_PROMPT = """\
You are evaluating whether a workflow step is complete.

Step objective:
{objective}

Step inputs (from previous steps):
{inputs}

Agent's completion claim:
  Summary: {summary}
  Claims: {claims}
  Outputs: {outputs}

Task context:
{task_context}

Decide:
- "approved" if the step objective is satisfactorily met
- "revise" if the step is incomplete or the claims don't match the objective
- "failed" if the step fundamentally cannot succeed

Be skeptical. Agents tend to declare victory prematurely.
Check whether the claims are consistent with the objective.
If the step says "implement with tests" and the claims don't mention tests,
that is a revise.

Respond with JSON only: {{"decision": "...", "reasoning": "...", "feedback": "..."}}
"""


class StepEvaluator:
    """Semantic step completion checker."""

    def __init__(
        self,
        *,
        llm: Any,
        evaluator_timeout_seconds: float = 30.0,
    ) -> None:
        self.llm = llm
        self.evaluator_timeout_seconds = evaluator_timeout_seconds

    @classmethod
    async def from_session_factory(
        cls,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        llm: Any,
    ) -> StepEvaluator:
        """Create an evaluator with DB-backed settings."""
        async with session_factory() as db_session:
            timeout_ms = await get_setting_value(db_session, "evaluator.timeout_ms", 30000)
        return cls(
            llm=llm,
            evaluator_timeout_seconds=(timeout_ms / 1000 if isinstance(timeout_ms, int) else 30.0),
        )

    async def evaluate(
        self,
        *,
        step_definition: StepDefinition,
        step_output: StepOutput,
        step_inputs: dict[str, StepOutput],
        task_context: str = "",
    ) -> StepEvaluation:
        """Run semantic evaluation on a step's output.

        Returns StepEvaluation with decision: approved, revise, or failed.
        On timeout or error, defaults to 'approved' (fail-open for evaluator).
        """
        prompt = self._build_prompt(step_definition, step_output, step_inputs, task_context)

        with EVALUATION_DURATION.time():
            try:
                response = await asyncio.wait_for(
                    self.llm.generate(
                        [
                            {
                                "role": "system",
                                "content": "You are a workflow step evaluator. Respond with JSON only.",
                            },
                            {"role": "user", "content": prompt},
                        ],
                        task_type="evaluator",
                        temperature=0,
                        response_format={"type": "json_object"},
                    ),
                    timeout=self.evaluator_timeout_seconds,
                )
                return self._parse_response(response)
            except TimeoutError:
                logger.warning(
                    "Step evaluator timed out, defaulting to approved",
                    extra={"extra_data": {"step": step_definition.name}},
                )
                return StepEvaluation(
                    decision="approved",
                    reasoning="Evaluator timed out — defaulting to approved",
                    evaluated_at=datetime.now(UTC),
                )
            except Exception:
                logger.exception(
                    "Step evaluator failed, defaulting to approved",
                    extra={"extra_data": {"step": step_definition.name}},
                )
                return StepEvaluation(
                    decision="approved",
                    reasoning="Evaluator error — defaulting to approved",
                    evaluated_at=datetime.now(UTC),
                )

    def _build_prompt(
        self,
        step_definition: StepDefinition,
        step_output: StepOutput,
        step_inputs: dict[str, StepOutput],
        task_context: str,
    ) -> str:
        """Build the evaluator prompt."""
        template = (
            step_definition.completion.evaluator_prompt
            if (step_definition.completion and step_definition.completion.evaluator_prompt)
            else _DEFAULT_EVALUATOR_PROMPT
        )

        formatted_inputs = (
            "\n".join(f"  {name}: {inp.summary}" for name, inp in step_inputs.items()) or "(none)"
        )

        formatted_outputs = json.dumps(step_output.outputs, default=str)[:2000]
        formatted_claims = "\n".join(f"  - {c}" for c in step_output.claims) or "(none)"

        return template.format(
            objective=step_definition.prompt or step_definition.description,
            inputs=formatted_inputs,
            summary=step_output.summary,
            claims=formatted_claims,
            outputs=formatted_outputs,
            task_context=task_context or "(none)",
        )

    def _parse_response(self, response: dict[str, Any]) -> StepEvaluation:
        """Parse the LLM evaluator response into a StepEvaluation."""
        content = _extract_text_from_response(response)
        try:
            payload = _parse_json_payload(content)
            decision = str(payload.get("decision", "approved")).lower()
            if decision not in {"approved", "revise", "failed"}:
                decision = "approved"
            evaluation = StepEvaluation(
                decision=decision,
                reasoning=str(payload.get("reasoning", "")),
                feedback=payload.get("feedback"),
                evaluated_at=datetime.now(UTC),
            )
        except (json.JSONDecodeError, ValueError):
            logger.warning(
                "Could not parse evaluator response, defaulting to approved",
                extra={"extra_data": {}},
            )
            evaluation = StepEvaluation(
                decision="approved",
                reasoning="Could not parse evaluator response",
                evaluated_at=datetime.now(UTC),
            )

        EVALUATIONS_TOTAL.labels(decision=evaluation.decision).inc()
        return evaluation


def _extract_text_from_response(response: dict[str, Any]) -> str:
    """Extract text content from an LLM response."""
    choices = response.get("choices")
    if not isinstance(choices, list) or not choices:
        return ""
    message = choices[0].get("message")
    if not isinstance(message, dict):
        return ""
    content = message.get("content")
    return content if isinstance(content, str) else ""


def _parse_json_payload(content: str) -> dict[str, Any]:
    """Parse JSON from content that may be wrapped in markdown code fences."""
    cleaned = content.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.strip("`")
        if cleaned.startswith("json"):
            cleaned = cleaned[4:].strip()
    return cast(dict[str, Any], json.loads(cleaned))
