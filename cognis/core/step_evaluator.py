"""Semantic step completion evaluator.

Verifies whether a step's output satisfies its definition of done via
an independent LLM call. Uses a cheap model via routing policy.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from typing import Any

from prometheus_client import Counter, Histogram
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from cognis.core.json_utils import (
    extract_json_object,
    extract_text_from_response,
    infer_evaluation_from_text,
)
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

Agent's last response:
{content}

Task context:
{task_context}

Evaluation checklist:
1. Does the response address ALL parts of the step objective?
2. For each claim, is there evidence in the response content?
3. Are there obvious errors, missing pieces, or incomplete work?
4. If the objective mentions tests/validation, are they present and passing?

Decide:
- "approved" — the step objective is satisfactorily met based on actual \
response content (not just claims)
- "revise" — the step is incomplete, claims don't match content, or \
quality is insufficient. Provide specific, actionable feedback.
- "failed" — the step fundamentally cannot succeed (wrong approach, \
impossible constraint, repeated identical failures)

Be skeptical. Agents tend to declare victory prematurely. Partial \
completion is a revise, not an approval.

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
                from cognis.core.agent_registry import SYSTEM_AGENTS

                evaluator_agent = SYSTEM_AGENTS.get("system:evaluator")
                evaluator_prompt = (
                    evaluator_agent.system_prompt
                    if evaluator_agent and evaluator_agent.system_prompt
                    else "You are a workflow step evaluator. Respond with JSON only."
                )

                messages = [
                    {"role": "system", "content": evaluator_prompt},
                    {"role": "user", "content": prompt},
                ]
                generate_kwargs = {
                    "temperature": 0,
                    "response_format": {"type": "json_object"},
                }
                try:
                    response = await asyncio.wait_for(
                        self.llm.generate(messages, task_type="evaluator", **generate_kwargs),
                        timeout=self.evaluator_timeout_seconds,
                    )
                except ValueError:
                    # "evaluator" task type not configured — fall back to default
                    logger.debug("Evaluator task_type not configured, falling back to default")
                    response = await asyncio.wait_for(
                        self.llm.generate(messages, task_type="default", **generate_kwargs),
                        timeout=self.evaluator_timeout_seconds,
                    )
                evaluation = self._parse_response(response)
                logger.info(
                    "Step evaluation complete",
                    extra={
                        "extra_data": {
                            "step": step_definition.name,
                            "decision": evaluation.decision,
                        }
                    },
                )
                return evaluation
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

        # Include the agent's actual response content so the evaluator can
        # verify claims against evidence.  Use the tail of the content since
        # the final deliverable is typically at the end.
        raw_content = step_output.content or ""
        if len(raw_content) > 4000:
            formatted_content = f"...(truncated)...\n{raw_content[-4000:]}"
        else:
            formatted_content = raw_content or "(no content produced)"

        return template.format(
            objective=step_definition.prompt or step_definition.description,
            inputs=formatted_inputs,
            summary=step_output.summary,
            claims=formatted_claims,
            outputs=formatted_outputs,
            content=formatted_content,
            task_context=task_context or "(none)",
        )

    def _parse_response(self, response: dict[str, Any]) -> StepEvaluation:
        """Parse the LLM evaluator response into a StepEvaluation.

        Uses multi-layer JSON extraction with semantic inference fallback.
        For capable models that respect ``response_format``, the first
        layer (direct parse) succeeds immediately with no overhead.
        """
        content = extract_text_from_response(response)
        try:
            payload = extract_json_object(content, label="evaluator")
        except ValueError:
            # All JSON extraction layers failed — use semantic inference
            payload = infer_evaluation_from_text(content)
            logger.warning(
                "JSON extraction failed for evaluator response, using semantic inference",
                extra={
                    "extra_data": {
                        "inferred_decision": payload.get("decision"),
                        "content_length": len(content),
                    }
                },
            )

        decision = str(payload.get("decision", "approved")).lower()
        if decision not in {"approved", "revise", "failed"}:
            decision = "approved"

        evaluation = StepEvaluation(
            decision=decision,
            reasoning=str(payload.get("reasoning", "")),
            feedback=payload.get("feedback"),
            evaluated_at=datetime.now(UTC),
        )

        EVALUATIONS_TOTAL.labels(decision=evaluation.decision).inc()
        return evaluation
