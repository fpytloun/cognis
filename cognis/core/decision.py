"""Decision engine for inline vs delegated turns.

Uses deterministic fast-path rules only.  The main agent is the
orchestrator and decides whether to delegate via its ``delegate``
and ``create_task`` tools during the turn.  The decision engine
handles only unambiguous cases (explicit slash commands, keywords,
conversational messages) to short-circuit the turn lifecycle.
"""

from __future__ import annotations

import asyncio
from typing import Any

from prometheus_client import Counter
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from cognis.core.json_utils import (
    extract_json_object,
    extract_text_from_response,
    maybe_fallback_to_plain_json_response,
)
from cognis.logging import get_logger
from cognis.models.agent import AgentDefinition
from cognis.store.queries import get_setting_value

logger = get_logger(__name__)

DECISIONS_TOTAL = Counter(
    "cognis_decisions_total",
    "Decision engine outcomes",
    labelnames=("decision", "source"),
)

INLINE_OVERRIDE_KEYWORDS = ("just answer", "don't delegate", "do not delegate")
DELEGATE_OVERRIDE_KEYWORDS = ("run in background", "background task", "delegate this")
DELEGATE_PREFIXES = ("/research", "/implement", "/delegate", "/task")
CONVERSATIONAL_PREFIXES = ("hi", "hello", "hey", "thanks", "thank you", "what do you think")

_ROUTING_REMINDER_PATTERNS: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        "review",
        (
            "code review",
            "review this",
            "review the",
            "audit",
            "pull request",
            "pr review",
            "inspect this diff",
            "check this diff",
        ),
    ),
    (
        "research",
        (
            "research",
            "investigate",
            "compare",
            "look up",
            "latest ",
            "current best",
        ),
    ),
    (
        "exploration",
        (
            "find where",
            "trace",
            "locate",
            "explore the codebase",
            "understand this codebase",
            "survey the codebase",
        ),
    ),
    (
        "implementation",
        (
            "implement ",
            "build a",
            "build an",
            "fix bug",
            "fix the bug",
            "fix this bug",
            "refactor",
            "add feature",
            "change the code",
            "write code",
        ),
    ),
)


class DecisionResult(BaseModel):
    """Normalized decision-engine output."""

    decision: str
    reason: str
    confidence: float
    predicted_tool_intensity: str
    override_source: str | None = None
    degraded: bool = False


class RoutingReminderAdvice(BaseModel):
    """Short advisory reminder for eligible chat turns.

    This value is prompt guidance only. It must not influence controller-side
    routing decisions or be persisted as session content.
    """

    category: str
    reminder: str


def build_routing_reminder(user_message: str) -> RoutingReminderAdvice | None:
    """Return short turn-local routing advice for strongly matched user text.

    The reminder is advisory only and is intended for mutable prompt suffix
    injection immediately before the current user message.
    """

    text = user_message.strip()
    if not text:
        return None

    lowered = text.lower()
    if lowered.startswith("/"):
        return None
    if any(keyword in lowered for keyword in INLINE_OVERRIDE_KEYWORDS):
        return None
    if any(keyword in lowered for keyword in DELEGATE_OVERRIDE_KEYWORDS):
        return None

    for category, patterns in _ROUTING_REMINDER_PATTERNS:
        if any(pattern in lowered for pattern in patterns):
            return RoutingReminderAdvice(
                category=category,
                reminder=_render_routing_reminder(category),
            )
    return None


def _render_routing_reminder(category: str) -> str:
    labels = {
        "implementation": "non-trivial implementation work",
        "research": "research or investigation work",
        "review": "code review or audit work",
        "exploration": "codebase exploration work",
    }
    label = labels.get(category, "substantial work")
    return (
        f"Routing hint: this request looks like {label}.\n"
        "Consider delegation or a background task before doing it inline.\n"
        "If it is truly small enough to complete correctly in this turn, inline execution is fine."
    )


class DecisionEngine:
    """Deterministic fast-path rules for inline vs delegate classification.

    The LLM classifier has been removed — the main agent decides whether
    to delegate via its orchestration tools (``delegate``, ``create_task``).
    This engine only handles unambiguous cases: explicit slash commands,
    keyword overrides, and conversational messages.
    """

    def __init__(
        self,
        *,
        llm: Any,
        inline_max_length: int,
        max_delegation_depth: int,
    ) -> None:
        self.llm = llm
        self.inline_max_length = inline_max_length
        self.max_delegation_depth = max_delegation_depth

    @classmethod
    async def from_session_factory(
        cls,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        llm: Any,
    ) -> DecisionEngine:
        """Create a decision engine from DB-backed settings."""

        async with session_factory() as db_session:
            inline_max_length = await get_setting_value(
                db_session, "decision_engine.inline_max_length", 200
            )
            max_delegation_depth = await get_setting_value(
                db_session, "session.max_delegation_depth", 5
            )
        return cls(
            llm=llm,
            inline_max_length=int(inline_max_length) if isinstance(inline_max_length, int) else 200,
            max_delegation_depth=(
                int(max_delegation_depth) if isinstance(max_delegation_depth, int) else 5
            ),
        )

    async def decide(
        self,
        *,
        user_message: str,
        agent: AgentDefinition,
        current_depth: int = 0,
        override: str | None = None,
    ) -> DecisionResult:
        """Classify a user message using deterministic rules only.

        All ambiguous messages default to ``inline`` — the agent decides
        whether to delegate during its turn via orchestration tools.
        """

        text = user_message.strip()
        if override in {"inline", "delegate", "ask_user"}:
            return self._result(
                decision=override,
                reason="UI override",
                confidence=1.0,
                predicted_tool_intensity="medium",
                source="override",
                override_source="ui",
            )

        lower_text = text.lower()
        explicit_inline = any(keyword in lower_text for keyword in INLINE_OVERRIDE_KEYWORDS)
        explicit_delegate = lower_text.startswith(DELEGATE_PREFIXES) or any(
            keyword in lower_text for keyword in DELEGATE_OVERRIDE_KEYWORDS
        )

        if explicit_inline:
            return self._result(
                decision="inline",
                reason="Explicit inline user preference",
                confidence=1.0,
                predicted_tool_intensity="low",
                source="override",
                override_source="keyword",
            )

        if not self._can_delegate(agent, current_depth):
            decision = "ask_user" if explicit_delegate else "inline"
            return self._result(
                decision=decision,
                reason="Delegation limit reached",
                confidence=1.0,
                predicted_tool_intensity="medium",
                source="limits",
                override_source="policy" if explicit_delegate else None,
            )

        if explicit_delegate:
            return self._result(
                decision="delegate",
                reason="Explicit delegation request",
                confidence=1.0,
                predicted_tool_intensity="high",
                source="override",
                override_source="keyword",
            )

        if self._is_conversational(lower_text):
            return self._result(
                decision="inline",
                reason="Short conversational message",
                confidence=0.85,
                predicted_tool_intensity="low",
                source="rules",
            )

        # All other messages: inline by default.
        # The agent decides whether to delegate during its turn.
        return self._result(
            decision="inline",
            reason="Default inline — agent orchestrates via tools",
            confidence=0.9,
            predicted_tool_intensity="medium",
            source="rules",
        )

    def _can_delegate(self, agent: AgentDefinition, current_depth: int) -> bool:
        if agent.permissions is None:
            return current_depth < self.max_delegation_depth
        if not agent.permissions.can_delegate:
            return False
        allowed_depth = min(agent.permissions.max_delegation_depth, self.max_delegation_depth)
        return current_depth < allowed_depth

    def _is_conversational(self, lowered_text: str) -> bool:
        return len(lowered_text) <= self.inline_max_length and lowered_text.startswith(
            CONVERSATIONAL_PREFIXES
        )

    def _result(
        self,
        *,
        decision: str,
        reason: str,
        confidence: float,
        predicted_tool_intensity: str,
        source: str,
        override_source: str | None = None,
        degraded: bool = False,
    ) -> DecisionResult:
        DECISIONS_TOTAL.labels(decision=decision, source=source).inc()
        logger.info(
            "Decision engine classified message",
            extra={"extra_data": {"decision": decision, "source": source}},
        )
        return DecisionResult(
            decision=decision,
            reason=reason,
            confidence=confidence,
            predicted_tool_intensity=predicted_tool_intensity,
            override_source=override_source,
            degraded=degraded,
        )


# ---------------------------------------------------------------------------
# Workflow selection
# ---------------------------------------------------------------------------


class WorkflowSelectionResult(BaseModel):
    """Result of workflow selection."""

    workflow_id: str
    confidence: float
    reason: str
    source: str  # "explicit" | "default" | "classifier"


async def select_workflow(
    *,
    llm: Any,
    task_description: str,
    available_workflows: list[dict[str, str]],
    default_workflow_id: str | None = None,
    selection_mode: str = "automatic",
    classifier_timeout_seconds: float = 10.0,
) -> WorkflowSelectionResult:
    """Select a workflow for a task based on agent config and classifier.

    Args:
        llm: LLM provider for classifier calls.
        task_description: Description of the task.
        available_workflows: List of {workflow_id, name, criteria} dicts.
        default_workflow_id: Agent's default workflow.
        selection_mode: "automatic" | "always_ask" | "use_default".
        classifier_timeout_seconds: Timeout for the LLM classifier.
    """
    if selection_mode == "use_default" and default_workflow_id:
        return WorkflowSelectionResult(
            workflow_id=default_workflow_id,
            confidence=1.0,
            reason="Agent default workflow",
            source="default",
        )

    if not available_workflows:
        return WorkflowSelectionResult(
            workflow_id=default_workflow_id or "system:general-task",
            confidence=0.5,
            reason="No workflows available, using default",
            source="default",
        )

    # Build classifier prompt from system agent
    from cognis.core.agent_registry import SYSTEM_AGENTS

    classifier_agent = SYSTEM_AGENTS.get("system:classifier")
    classifier_prompt = (
        classifier_agent.system_prompt
        if classifier_agent and classifier_agent.system_prompt
        else (
            "Select the best workflow for the given task. "
            "You MUST respond with a single JSON object and nothing else."
        )
    )

    workflow_options = "\n".join(
        f"- {w['workflow_id']}: {w.get('name', '')} — {w.get('criteria', '')}"
        for w in available_workflows
    )
    prompt = [
        {"role": "system", "content": classifier_prompt},
        {
            "role": "user",
            "content": f"Task: {task_description}\n\nAvailable workflows:\n{workflow_options}",
        },
    ]

    try:

        async def _generate(generate_kwargs: dict[str, Any]) -> dict[str, Any]:
            return await asyncio.wait_for(
                llm.generate(
                    prompt,
                    task_type="classifier",
                    temperature=0,
                    reasoning_effort="low",
                    **generate_kwargs,
                ),
                timeout=classifier_timeout_seconds,
            )

        response = await _generate({"response_format": {"type": "json_object"}})
        response = await maybe_fallback_to_plain_json_response(
            response,
            generate_response=_generate,
            label="classifier",
            logger_obj=logger,
            warning_context={"mode": "workflow_selection"},
        )
        content = extract_text_from_response(response)
        if not content or not content.strip():
            raise ValueError("Classifier returned empty response")
        payload = extract_json_object(content, label="classifier")
        workflow_id = str(payload.get("workflow_id", ""))

        # Validate the selected workflow exists
        valid_ids = {w["workflow_id"] for w in available_workflows}
        if workflow_id not in valid_ids:
            workflow_id = default_workflow_id or "system:general-task"

        return WorkflowSelectionResult(
            workflow_id=workflow_id,
            confidence=float(payload.get("confidence", 0.5)),
            reason=str(payload.get("reason", "Classifier selection")),
            source="classifier",
        )

    except TimeoutError:
        logger.warning(
            "Workflow selector timed out, using default",
            extra={"extra_data": {"default": default_workflow_id}},
        )
        return WorkflowSelectionResult(
            workflow_id=default_workflow_id or "system:general-task",
            confidence=0.3,
            reason="Classifier timeout fallback",
            source="default",
        )
    except Exception:
        logger.warning(
            "Workflow selector failed, using default",
            extra={"extra_data": {"default": default_workflow_id}},
            exc_info=True,
        )
        return WorkflowSelectionResult(
            workflow_id=default_workflow_id or "system:general-task",
            confidence=0.3,
            reason="Classifier fallback",
            source="default",
        )
