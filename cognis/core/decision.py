"""Decision engine for inline vs delegated turns."""

from __future__ import annotations

import asyncio
import json
from typing import Any, cast

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
CLASSIFIER_TIMEOUTS = Counter(
    "cognis_decision_classifier_timeouts_total",
    "Decision classifier timeouts",
)
CLASSIFIER_FAILURES = Counter(
    "cognis_decision_classifier_failures_total",
    "Decision classifier failures",
)

INLINE_OVERRIDE_KEYWORDS = ("just answer", "don't delegate", "do not delegate")
DELEGATE_OVERRIDE_KEYWORDS = ("run in background", "background task", "delegate this")
DELEGATE_PREFIXES = ("/research", "/implement", "/delegate", "/fork", "/worker")
CONVERSATIONAL_PREFIXES = ("hi", "hello", "hey", "thanks", "thank you", "what do you think")


class DecisionResult(BaseModel):
    """Normalized decision-engine output."""

    decision: str
    reason: str
    confidence: float
    predicted_tool_intensity: str
    override_source: str | None = None
    degraded: bool = False


class DecisionEngine:
    """Combine deterministic rules with a lightweight LLM classifier."""

    def __init__(
        self,
        *,
        llm: Any,
        inline_max_length: int,
        classifier_timeout_seconds: float,
        classifier_fallback: str,
        max_delegation_depth: int,
    ) -> None:
        self.llm = llm
        self.inline_max_length = inline_max_length
        self.classifier_timeout_seconds = classifier_timeout_seconds
        self.classifier_fallback = classifier_fallback
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
            classifier_timeout_ms = await get_setting_value(
                db_session, "decision_engine.classifier_timeout_ms", 500
            )
            classifier_fallback = await get_setting_value(
                db_session, "decision_engine.classifier_fallback", "inline"
            )
            max_delegation_depth = await get_setting_value(
                db_session, "session.max_delegation_depth", 5
            )
        return cls(
            llm=llm,
            inline_max_length=int(inline_max_length) if isinstance(inline_max_length, int) else 200,
            classifier_timeout_seconds=(
                classifier_timeout_ms / 1000 if isinstance(classifier_timeout_ms, int) else 0.5
            ),
            classifier_fallback=(
                classifier_fallback if isinstance(classifier_fallback, str) else "inline"
            ),
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
        """Classify a user message as inline, delegate, or ask_user."""

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

        if len(text) <= self.inline_max_length and "?" in text and len(text.split()) <= 16:
            return self._result(
                decision="inline",
                reason="Short direct question",
                confidence=0.75,
                predicted_tool_intensity="low",
                source="rules",
            )

        return await self._classify_with_model(text)

    async def _classify_with_model(self, user_message: str) -> DecisionResult:
        prompt = [
            {
                "role": "system",
                "content": (
                    "Classify the user request for orchestration. Return JSON only with keys: "
                    "decision (inline|delegate|ask_user), reason, confidence, predicted_tool_intensity (low|medium|high)."
                ),
            },
            {"role": "user", "content": user_message},
        ]
        try:
            response = await asyncio.wait_for(
                self.llm.generate(prompt, task_type="classifier", temperature=0),
                timeout=self.classifier_timeout_seconds,
            )
            content = _extract_text_from_response(response)
            payload = _parse_classifier_payload(content)
            decision = payload.get("decision")
            if decision not in {"inline", "delegate", "ask_user"}:
                raise ValueError("Invalid classifier decision")
            return self._result(
                decision=decision,
                reason=str(payload.get("reason") or "Classifier decision"),
                confidence=float(payload.get("confidence") or 0.5),
                predicted_tool_intensity=str(payload.get("predicted_tool_intensity") or "medium"),
                source="classifier",
            )
        except TimeoutError:
            CLASSIFIER_TIMEOUTS.inc()
            logger.warning(
                "Decision classifier timed out",
                extra={"extra_data": {"fallback": self.classifier_fallback}},
            )
        except Exception:
            CLASSIFIER_FAILURES.inc()
            logger.warning(
                "Decision classifier failed",
                extra={"extra_data": {"fallback": self.classifier_fallback}},
            )

        return self._result(
            decision=self.classifier_fallback,
            reason="Classifier fallback",
            confidence=0.4,
            predicted_tool_intensity="medium",
            source="fallback",
            degraded=True,
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


def _extract_text_from_response(response: dict[str, Any]) -> str:
    choices = response.get("choices")
    if not isinstance(choices, list) or not choices:
        return ""
    message = choices[0].get("message")
    if not isinstance(message, dict):
        return ""
    content = message.get("content")
    return content if isinstance(content, str) else ""


def _parse_classifier_payload(content: str) -> dict[str, Any]:
    cleaned = content.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.strip("`")
        if cleaned.startswith("json"):
            cleaned = cleaned[4:].strip()
    return cast(dict[str, Any], json.loads(cleaned))
