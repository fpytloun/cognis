"""Decision engine for chat turn routing.

Uses deterministic fast-path rules only. The main agent is the
orchestrator and decides whether to delegate via its ``delegate``
and ``create_task`` tools during the turn. The decision engine handles
only explicit UI overrides and obvious conversational messages.
"""

from __future__ import annotations

from typing import Any

from prometheus_client import Counter
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

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
        f"Routing hint: this request looks like {label}. Delegate it or start a "
        "background task when that helps; do it inline when it fits in this turn."
    )


class DecisionEngine:
    """Deterministic fast-path rules for chat turn classification.

    The LLM classifier has been removed — the main agent decides whether
    to delegate via its orchestration tools (``delegate``, ``create_task``).
    This engine only handles explicit UI overrides, inline keyword
    overrides, and conversational messages.
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
            return self._result(
                decision="inline",
                reason="Delegation limit reached",
                confidence=1.0,
                predicted_tool_intensity="medium",
                source="limits",
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
