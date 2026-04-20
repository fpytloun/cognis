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
    maybe_fallback_to_plain_json_response,
)
from cognis.logging import get_logger
from cognis.models.workflow import StepDefinition, StepEvaluation, StepOutput
from cognis.store.queries import get_setting_value

logger = get_logger(__name__)

EVALUATOR_MALFUNCTION_REASON_PREFIX = "Evaluator malfunction:"
DEFAULT_EVALUATOR_TIMEOUT_MS = 180000

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

step_complete metadata:
  Summary: {summary}
  Claims: {claims}
  Outputs: {outputs}
  Outcome: {outcome}
  Notification: {notification}

Assistant written deliverable:
{content}

Execution evidence:
{execution_evidence}

Task context:
{task_context}

Evaluation checklist:
1. Does the response address ALL parts of the step objective?
2. For each claim, is there evidence in the response content?
3. Are there obvious errors, missing pieces, or incomplete work?
4. If the objective mentions tests/validation, are they present and passing?
5. A proper step completion may report an outcome of "success", "rejected",
   or "failed". Success is valid when the review approves the plan or work
   with no required changes. Treat a self-reported outcome of "failed" as
   input, not as the final authority. If the step made meaningful progress and
   can recover with targeted revisions, prefer "revise" over agreeing that the
   step failed. Judge whether the step was completed correctly, not whether the
   business result was positive.
6. Do not require step_complete metadata fields to appear inside the assistant's
   written deliverable. They are supplied separately above.
7. Only require artifacts explicitly requested by the step objective. Process
   guidance is not automatically a required output artifact.
8. Use execution evidence to validate claims when it is relevant and available.
9. Treat Expected output as strong guidance for output shape, tone, format, and
   level of detail, but do not fail the step solely because the assistant
   produced the minimum deliverable required by the runtime step contract.
10. Silent completion can be valid when the step completed successfully and the
   runtime policy explicitly allows no user-facing notification. Evaluate task
   completion separately from whether the result should be shown to the user.
11. Direct completion can also be valid for ready-to-read outputs. Do not
   penalize the step for bypassing the normal follow-up flow when direct
   delivery was explicitly selected.

Decide:
- "approved" — the step objective is satisfactorily met based on actual \
response content (not just claims)
- "revise" — the step is incomplete, claims don't match content, or \
quality is insufficient. Provide specific, actionable feedback.
- "failed" — the step fundamentally cannot succeed (wrong approach, \
impossible constraint, repeated identical failures)

Be skeptical. Agents tend to declare victory prematurely. Partial \
completion is a revise, not an approval.

If the agent reported outcome="failed" but the work is still salvageable,
return "revise" so the workflow can continue.

If the summary or evidence says the step hit a tool-call ceiling or context
window before completion, treat that as incomplete work and prefer "revise"
unless the deliverable is clearly complete and correct.

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
            timeout_ms = await get_setting_value(
                db_session,
                "evaluator.timeout_ms",
                DEFAULT_EVALUATOR_TIMEOUT_MS,
            )
        return cls(
            llm=llm,
            evaluator_timeout_seconds=(
                timeout_ms / 1000
                if isinstance(timeout_ms, int)
                else DEFAULT_EVALUATOR_TIMEOUT_MS / 1000
            ),
        )

    async def evaluate(
        self,
        *,
        step_definition: StepDefinition,
        step_output: StepOutput,
        step_inputs: dict[str, StepOutput],
        task_context: str = "",
        execution_evidence: dict[str, Any] | None = None,
    ) -> StepEvaluation:
        """Run semantic evaluation on a step's output.

        Returns StepEvaluation with decision: approved, revise, or failed.
        On timeout or transport error, returns a forced failed evaluator
        malfunction rather than silently approving incomplete work.
        Empty or truncated evaluator output is treated as an evaluator failure.
        """
        prompt = self._build_prompt(
            step_definition,
            step_output,
            step_inputs,
            task_context,
            execution_evidence or {},
        )

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
                response = await self._generate_evaluator_response(messages, generate_kwargs)
                if self._should_retry_response(response):
                    logger.warning(
                        "Evaluator returned empty or incomplete response, retrying once",
                        extra={"extra_data": {"step": step_definition.name}},
                    )
                    try:
                        response = await self._generate_evaluator_response(
                            messages, generate_kwargs
                        )
                    except TimeoutError:
                        logger.warning(
                            "Evaluator retry timed out after unusable response, failing evaluation",
                            extra={"extra_data": {"step": step_definition.name}},
                        )
                        return self._forced_failed(
                            reasoning="retry timed out after empty or incomplete output",
                            feedback="Evaluator could not return a usable response after retry.",
                        )
                    except Exception:
                        logger.exception(
                            "Evaluator retry failed after unusable response, failing evaluation",
                            extra={"extra_data": {"step": step_definition.name}},
                        )
                        return self._forced_failed(
                            reasoning="retry failed after empty or incomplete output",
                            feedback="Evaluator could not return a usable response after retry.",
                        )
                try:
                    response = await maybe_fallback_to_plain_json_response(
                        response,
                        generate_response=lambda kwargs: self._generate_evaluator_response(
                            messages, kwargs
                        ),
                        label="evaluator",
                        logger_obj=logger,
                        warning_context={"step": step_definition.name},
                    )
                except TimeoutError:
                    logger.warning(
                        "Evaluator plain-text JSON fallback timed out",
                        extra={"extra_data": {"step": step_definition.name}},
                    )
                    return self._forced_failed(
                        reasoning="plain-text JSON fallback timed out after unusable output",
                        feedback="Evaluator fallback could not return a usable response.",
                    )
                except Exception:
                    logger.exception(
                        "Evaluator plain-text JSON fallback failed",
                        extra={"extra_data": {"step": step_definition.name}},
                    )
                    return self._forced_failed(
                        reasoning="plain-text JSON fallback failed after unusable output",
                        feedback="Evaluator fallback could not return a usable response.",
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
                    "Step evaluator timed out, forcing failure",
                    extra={"extra_data": {"step": step_definition.name}},
                )
                return self._forced_failed(
                    reasoning="Evaluator timed out before producing a usable judgment",
                    feedback="Evaluator timed out before a usable judgment was produced.",
                )
            except Exception:
                logger.exception(
                    "Step evaluator failed, forcing malfunction failure",
                    extra={"extra_data": {"step": step_definition.name}},
                )
                return self._forced_failed(
                    reasoning="Evaluator failed before producing a usable judgment",
                    feedback="Evaluator failed before a usable judgment was produced.",
                )

    def _build_prompt(
        self,
        step_definition: StepDefinition,
        step_output: StepOutput,
        step_inputs: dict[str, StepOutput],
        task_context: str,
        execution_evidence: dict[str, Any],
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
        formatted_outcome = (
            step_output.outcome.model_dump_json()
            if step_output.outcome is not None
            else "(implicit success)"
        )
        formatted_notification = (
            step_output.notification.model_dump_json()
            if step_output.notification is not None
            else "(use configured delivery family)"
        )
        formatted_execution_evidence = json.dumps(execution_evidence, default=str)[:4000] or "{}"

        # Include the full response content so the evaluator can verify claims
        # against evidence anywhere in the deliverable.
        formatted_content = step_output.content or "(no content produced)"

        return template.format(
            objective=step_definition.prompt or step_definition.description,
            inputs=formatted_inputs,
            summary=step_output.summary,
            claims=formatted_claims,
            outputs=formatted_outputs,
            outcome=formatted_outcome,
            notification=formatted_notification,
            content=formatted_content,
            execution_evidence=formatted_execution_evidence,
            task_context=task_context or "(none)",
        )

    def _parse_response(self, response: dict[str, Any]) -> StepEvaluation:
        """Parse the LLM evaluator response into a StepEvaluation.

        Uses multi-layer JSON extraction with semantic inference fallback.
        For capable models that respect ``response_format``, the first
        layer (direct parse) succeeds immediately with no overhead.
        """
        refusal = self._extract_refusal_text(response)
        if refusal:
            logger.warning("Evaluator refused to answer, forcing revise")
            evaluation = self._forced_revise(
                reasoning="Evaluator refused to provide a usable judgment",
                feedback="Retry the step; evaluator refused to answer.",
            )
            return evaluation
        content = extract_text_from_response(response)
        finish_reason = self._extract_finish_reason(response)
        if finish_reason == "length":
            logger.warning(
                "Evaluator response incomplete, failing evaluation",
                extra={"extra_data": {"content_length": len(content)}},
            )
            return self._forced_failed(
                reasoning="response was incomplete or truncated",
                feedback="Evaluator response was incomplete or truncated.",
            )
        if not content.strip():
            logger.warning("Evaluator returned empty response, failing evaluation")
            return self._forced_failed(
                reasoning="returned no usable output",
                feedback="Evaluator response was empty.",
            )
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

        decision = str(payload.get("decision", "revise")).lower()
        if decision not in {"approved", "revise", "failed"}:
            return self._forced_failed(
                reasoning=f"invalid evaluator decision: {decision}",
                feedback="Evaluator returned an invalid decision.",
            )

        evaluation = StepEvaluation(
            decision=decision,
            reasoning=str(payload.get("reasoning", "")),
            feedback=payload.get("feedback"),
            evaluated_at=datetime.now(UTC),
        )

        EVALUATIONS_TOTAL.labels(decision=evaluation.decision).inc()
        return evaluation

    def _should_retry_response(self, response: dict[str, Any]) -> bool:
        if self._extract_refusal_text(response):
            return False
        content = extract_text_from_response(response)
        if not content.strip():
            return True
        return self._extract_finish_reason(response) == "length"

    def _extract_finish_reason(self, response: dict[str, Any]) -> str:
        choices = response.get("choices")
        if not isinstance(choices, list) or not choices:
            return "stop"
        finish_reason = choices[0].get("finish_reason")
        return str(finish_reason or "stop")

    def _extract_refusal_text(self, response: dict[str, Any]) -> str:
        choices = response.get("choices")
        if not isinstance(choices, list) or not choices:
            return ""
        message = choices[0].get("message")
        if not isinstance(message, dict):
            return ""
        refusal = message.get("refusal")
        return refusal.strip() if isinstance(refusal, str) else ""

    def _forced_revise(self, *, reasoning: str, feedback: str) -> StepEvaluation:
        evaluation = StepEvaluation(
            decision="revise",
            reasoning=reasoning,
            feedback=feedback,
            evaluated_at=datetime.now(UTC),
        )
        EVALUATIONS_TOTAL.labels(decision=evaluation.decision).inc()
        return evaluation

    def _forced_failed(self, *, reasoning: str, feedback: str) -> StepEvaluation:
        evaluation = StepEvaluation(
            decision="failed",
            reasoning=f"{EVALUATOR_MALFUNCTION_REASON_PREFIX} {reasoning}",
            feedback=feedback,
            evaluated_at=datetime.now(UTC),
        )
        EVALUATIONS_TOTAL.labels(decision=evaluation.decision).inc()
        return evaluation

    async def _generate_evaluator_response(
        self, messages: list[dict[str, str]], generate_kwargs: dict[str, Any]
    ) -> dict[str, Any]:
        try:
            return await asyncio.wait_for(
                self.llm.generate(messages, task_type="evaluator", **generate_kwargs),
                timeout=self.evaluator_timeout_seconds,
            )
        except ValueError:
            logger.debug("Evaluator task_type not configured, falling back to default")
            return await asyncio.wait_for(
                self.llm.generate(messages, task_type="default", **generate_kwargs),
                timeout=self.evaluator_timeout_seconds,
            )


def is_evaluator_malfunction(evaluation: StepEvaluation) -> bool:
    """Return True when failure came from evaluator unusable output, not the step itself."""

    return evaluation.reasoning.startswith(EVALUATOR_MALFUNCTION_REASON_PREFIX)
