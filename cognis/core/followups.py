"""Typed follow-up metadata, policy, and rendering helpers."""

from __future__ import annotations

import asyncio
import hashlib
import html
import json
from enum import StrEnum
from time import monotonic
from typing import Any, Literal

from prometheus_client import Counter, Histogram
from pydantic import BaseModel, Field, model_validator

from cognis.core.json_utils import (
    extract_json_object,
    extract_text_from_response,
)
from cognis.logging import get_logger

logger = get_logger(__name__)

FOLLOW_UP_CLASSIFICATIONS_TOTAL = Counter(
    "cognis_follow_up_classifications_total",
    "Follow-up mode decisions",
    labelnames=("mode", "origin", "source"),
)
FOLLOW_UP_CLASSIFIER_FALLBACKS_TOTAL = Counter(
    "cognis_follow_up_classifier_fallbacks_total",
    "Same-conversation follow-up classifier fallbacks",
    labelnames=("reason",),
)
FOLLOW_UP_CLASSIFIER_DURATION = Histogram(
    "cognis_follow_up_classifier_duration_seconds",
    "Duration of same-conversation follow-up classification",
    buckets=(0.1, 0.25, 0.5, 1, 2, 5),
)

_MAX_TEXT_FIELD_CHARS = 600
_MAX_DESCRIPTION_CHARS = 1200
_MAX_SNIPPETS = 5
_MAX_SNIPPET_CHARS = 220


class FollowUpMode(StrEnum):
    INTEGRATE = "integrate"
    NOTIFY = "notify"


class FollowUpOriginKind(StrEnum):
    TASK_RESULT = "task_result"
    DELEGATION_RESULT = "delegation_result"
    GATE = "gate"
    SCHEDULE = "schedule"
    OTHER = "other"


class FollowUpRelevanceHint(StrEnum):
    SAME_THREAD = "same_thread"
    UNRELATED = "unrelated"
    UNKNOWN = "unknown"


class FollowUpRequiredAction(StrEnum):
    INTEGRATE_RESULT = "integrate_result"
    PRESENT_UPDATE = "present_update"
    EXPLAIN_PAUSE = "explain_pause"
    INFORM_FAILURE = "inform_failure"
    OTHER = "other"


class FollowUpStatus(StrEnum):
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    PAUSED = "paused"


class FollowUpBase(BaseModel):
    version: Literal[1] = 1
    follow_up_id: str = Field(min_length=1, max_length=64)
    mode: FollowUpMode
    origin_kind: FollowUpOriginKind
    relevance_hint: FollowUpRelevanceHint
    required_action: FollowUpRequiredAction
    topic_ref: str | None = Field(default=None, max_length=120)
    status: FollowUpStatus


class TaskResultFollowUp(FollowUpBase):
    origin_kind: Literal[FollowUpOriginKind.TASK_RESULT, FollowUpOriginKind.SCHEDULE]
    task_id: str = Field(min_length=1, max_length=120)
    task_title: str = Field(min_length=1, max_length=240)
    source_type: str = Field(min_length=1, max_length=32)
    delivery_mode: str = Field(min_length=1, max_length=64)
    result_summary: str | None = Field(default=None, max_length=_MAX_TEXT_FIELD_CHARS)
    description: str | None = Field(default=None, max_length=_MAX_DESCRIPTION_CHARS)


class DelegationResultFollowUp(FollowUpBase):
    origin_kind: Literal[FollowUpOriginKind.DELEGATION_RESULT]
    child_session_id: str = Field(min_length=1, max_length=120)
    result_summary: str | None = Field(default=None, max_length=_MAX_TEXT_FIELD_CHARS)


class GateFollowUp(FollowUpBase):
    origin_kind: Literal[FollowUpOriginKind.GATE]
    task_id: str = Field(min_length=1, max_length=120)
    task_title: str = Field(min_length=1, max_length=240)
    gate_message: str = Field(min_length=1, max_length=_MAX_TEXT_FIELD_CHARS)
    gate_options: list[dict[str, Any]] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_gate_status(self) -> GateFollowUp:
        if self.status is not FollowUpStatus.PAUSED:
            raise ValueError("Gate follow-ups must use paused status")
        return self


FollowUpMetadata = TaskResultFollowUp | DelegationResultFollowUp | GateFollowUp


def parse_follow_up_metadata(payload: dict[str, Any]) -> FollowUpMetadata:
    origin_kind = str(payload.get("origin_kind", ""))
    try:
        origin = FollowUpOriginKind(origin_kind)
    except ValueError as exc:
        raise ValueError(f"Unsupported follow-up origin: {origin_kind}") from exc

    if origin in {FollowUpOriginKind.TASK_RESULT, FollowUpOriginKind.SCHEDULE}:
        return TaskResultFollowUp.model_validate(payload)
    if origin is FollowUpOriginKind.DELEGATION_RESULT:
        return DelegationResultFollowUp.model_validate(payload)
    if origin is FollowUpOriginKind.GATE:
        return GateFollowUp.model_validate(payload)
    raise ValueError(f"Unsupported follow-up origin: {origin.value}")


def truncate_follow_up_text(value: str | None, *, max_chars: int) -> str | None:
    if value is None:
        return None
    text = " ".join(value.split())
    if not text:
        return None
    if len(text) <= max_chars:
        return text
    return text[: max_chars - 12].rstrip() + " [truncated]"


def build_follow_up_id(*, kind: str, conversation_id: str, parts: dict[str, Any]) -> str:
    canonical = {
        "kind": kind.strip().lower(),
        "conversation_id": conversation_id.strip(),
        "parts": parts,
    }
    digest = hashlib.sha256(
        json.dumps(canonical, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()
    return f"fup_{digest[:16]}"


def build_history_boundary_message() -> str:
    return (
        "The messages above are historical conversation context. They are not pending requests by "
        "default. Respond to the active follow-up event below."
    )


def render_follow_up_block(follow_up: FollowUpMetadata) -> str:
    attrs = (
        f'mode="{follow_up.mode.value}" '
        f'origin="{follow_up.origin_kind.value}" '
        f'action="{follow_up.required_action.value}" '
        f'relevance="{follow_up.relevance_hint.value}"'
    )
    lines = [f"<follow_up_event {attrs}>"]
    if isinstance(follow_up, TaskResultFollowUp):
        lines.extend(
            [
                f"task_id: {_xml_safe(follow_up.task_id)}",
                f"title: {_xml_safe(follow_up.task_title)}",
                f"status: {follow_up.status.value}",
            ]
        )
        if follow_up.result_summary:
            lines.append(f"summary: {_xml_safe(follow_up.result_summary)}")
        if follow_up.description:
            lines.append(f"description: {_xml_safe(follow_up.description)}")
    elif isinstance(follow_up, DelegationResultFollowUp):
        lines.extend(
            [
                f"child_session_id: {_xml_safe(follow_up.child_session_id)}",
                f"status: {follow_up.status.value}",
            ]
        )
        if follow_up.result_summary:
            lines.append(f"summary: {_xml_safe(follow_up.result_summary)}")
    elif isinstance(follow_up, GateFollowUp):
        lines.extend(
            [
                f"task_id: {_xml_safe(follow_up.task_id)}",
                f"title: {_xml_safe(follow_up.task_title)}",
                f"status: {follow_up.status.value}",
                f"reason: {_xml_safe(follow_up.gate_message)}",
            ]
        )
        if follow_up.gate_options:
            labels = ", ".join(
                _xml_safe(str(option.get("label") or option.get("action") or "?"))
                for option in follow_up.gate_options
                if isinstance(option, dict)
            )
            if labels:
                lines.append(f"options: {labels}")
    lines.append("</follow_up_event>")
    return "\n".join(lines)


def _xml_safe(value: str) -> str:
    return html.escape(value, quote=True)


class FollowUpPolicy:
    def __init__(
        self,
        *,
        llm: Any,
        classifier_timeout_seconds: float = 2.0,
        max_snippets: int = _MAX_SNIPPETS,
        max_snippet_chars: int = _MAX_SNIPPET_CHARS,
    ) -> None:
        self._llm = llm
        self._classifier_timeout_seconds = classifier_timeout_seconds
        self._max_snippets = max_snippets
        self._max_snippet_chars = max_snippet_chars

    async def build_task_result_follow_up(
        self,
        *,
        conversation_id: str,
        task_id: str,
        task_title: str,
        status: str,
        source_type: str,
        delivery_mode: str,
        result_summary: str | None,
        description: str | None,
        session_id: str | None,
        session_cache: Any,
    ) -> TaskResultFollowUp:
        normalized_status = FollowUpStatus(status)
        origin = (
            FollowUpOriginKind.SCHEDULE
            if source_type == "scheduler"
            else FollowUpOriginKind.TASK_RESULT
        )
        mode = FollowUpMode.NOTIFY
        relevance = FollowUpRelevanceHint.UNKNOWN
        required_action = FollowUpRequiredAction.PRESENT_UPDATE

        if normalized_status is FollowUpStatus.PAUSED:
            required_action = FollowUpRequiredAction.EXPLAIN_PAUSE
        elif normalized_status is FollowUpStatus.FAILED:
            required_action = FollowUpRequiredAction.INFORM_FAILURE

        if origin is FollowUpOriginKind.SCHEDULE:
            relevance = FollowUpRelevanceHint.UNRELATED
        elif delivery_mode != "same_conversation":
            relevance = FollowUpRelevanceHint.UNKNOWN
        else:
            mode = await self._classify_same_conversation_task_result(
                conversation_id=conversation_id,
                task_title=task_title,
                status=normalized_status,
                result_summary=result_summary,
                description=description,
                session_id=session_id,
                session_cache=session_cache,
            )
            relevance = (
                FollowUpRelevanceHint.SAME_THREAD
                if mode is FollowUpMode.INTEGRATE
                else FollowUpRelevanceHint.UNKNOWN
            )
            if mode is FollowUpMode.INTEGRATE and normalized_status is FollowUpStatus.COMPLETED:
                required_action = FollowUpRequiredAction.INTEGRATE_RESULT
            elif normalized_status is FollowUpStatus.FAILED:
                required_action = FollowUpRequiredAction.INFORM_FAILURE

        follow_up = TaskResultFollowUp(
            follow_up_id=build_follow_up_id(
                kind=origin.value,
                conversation_id=conversation_id,
                parts={
                    "task_id": task_id,
                    "status": normalized_status.value,
                    "delivery_mode": delivery_mode,
                },
            ),
            mode=mode,
            origin_kind=origin,
            relevance_hint=relevance,
            required_action=required_action,
            topic_ref=task_id,
            status=normalized_status,
            task_id=task_id,
            task_title=truncate_follow_up_text(task_title, max_chars=240) or task_title,
            source_type=source_type,
            delivery_mode=delivery_mode,
            result_summary=truncate_follow_up_text(result_summary, max_chars=_MAX_TEXT_FIELD_CHARS),
            description=truncate_follow_up_text(description, max_chars=_MAX_DESCRIPTION_CHARS),
        )
        FOLLOW_UP_CLASSIFICATIONS_TOTAL.labels(
            mode=follow_up.mode.value,
            origin=follow_up.origin_kind.value,
            source="policy",
        ).inc()
        return follow_up

    def build_gate_follow_up(
        self,
        *,
        conversation_id: str,
        pause_id: str,
        task_id: str,
        task_title: str,
        gate_message: str,
        gate_options: list[dict[str, Any]],
    ) -> GateFollowUp:
        follow_up = GateFollowUp(
            follow_up_id=build_follow_up_id(
                kind=FollowUpOriginKind.GATE.value,
                conversation_id=conversation_id,
                parts={"task_id": task_id, "pause_id": pause_id},
            ),
            mode=FollowUpMode.NOTIFY,
            origin_kind=FollowUpOriginKind.GATE,
            relevance_hint=FollowUpRelevanceHint.SAME_THREAD,
            required_action=FollowUpRequiredAction.EXPLAIN_PAUSE,
            topic_ref=task_id,
            status=FollowUpStatus.PAUSED,
            task_id=task_id,
            task_title=truncate_follow_up_text(task_title, max_chars=240) or task_title,
            gate_message=truncate_follow_up_text(gate_message, max_chars=_MAX_TEXT_FIELD_CHARS)
            or "Task needs attention.",
            gate_options=gate_options,
        )
        FOLLOW_UP_CLASSIFICATIONS_TOTAL.labels(
            mode=follow_up.mode.value,
            origin=follow_up.origin_kind.value,
            source="policy",
        ).inc()
        return follow_up

    def build_delegation_follow_up(
        self,
        *,
        conversation_id: str,
        child_session_id: str,
        status: str,
        result_summary: str | None,
    ) -> DelegationResultFollowUp:
        normalized_status = FollowUpStatus(status)
        required_action = (
            FollowUpRequiredAction.INTEGRATE_RESULT
            if normalized_status is FollowUpStatus.COMPLETED
            else FollowUpRequiredAction.INFORM_FAILURE
        )
        follow_up = DelegationResultFollowUp(
            follow_up_id=build_follow_up_id(
                kind=FollowUpOriginKind.DELEGATION_RESULT.value,
                conversation_id=conversation_id,
                parts={"child_session_id": child_session_id, "status": normalized_status.value},
            ),
            mode=FollowUpMode.INTEGRATE,
            origin_kind=FollowUpOriginKind.DELEGATION_RESULT,
            relevance_hint=FollowUpRelevanceHint.SAME_THREAD,
            required_action=required_action,
            topic_ref=child_session_id,
            status=normalized_status,
            child_session_id=child_session_id,
            result_summary=truncate_follow_up_text(result_summary, max_chars=_MAX_TEXT_FIELD_CHARS),
        )
        FOLLOW_UP_CLASSIFICATIONS_TOTAL.labels(
            mode=follow_up.mode.value,
            origin=follow_up.origin_kind.value,
            source="policy",
        ).inc()
        return follow_up

    async def _classify_same_conversation_task_result(
        self,
        *,
        conversation_id: str,
        task_title: str,
        status: FollowUpStatus,
        result_summary: str | None,
        description: str | None,
        session_id: str | None,
        session_cache: Any,
    ) -> FollowUpMode:
        if self._llm is None or session_id is None or session_cache is None:
            return self._classifier_fallback("unavailable")

        snippets = self._recent_conversation_snippets(session_id, session_cache)
        if not snippets:
            return self._classifier_fallback("no_context")

        prompt = [
            {
                "role": "system",
                "content": (
                    "Decide whether a completed background task belongs to the currently active "
                    'conversation thread. Return JSON only: {"mode": "integrate"|"notify", '
                    '"reason": string, "confidence": number}. Choose integrate only when the '
                    "recent conversation is clearly still about the same work. If uncertain, choose notify."
                ),
            },
            {
                "role": "user",
                "content": (
                    f"Conversation id: {conversation_id}\n"
                    f"Task title: {truncate_follow_up_text(task_title, max_chars=240) or task_title}\n"
                    f"Task status: {status.value}\n"
                    f"Task summary: {truncate_follow_up_text(result_summary, max_chars=400) or 'None'}\n"
                    f"Task description: {truncate_follow_up_text(description, max_chars=500) or 'None'}\n\n"
                    f"Recent conversation:\n{snippets}"
                ),
            },
        ]

        started_at = monotonic()
        try:

            async def _generate(generate_kwargs: dict[str, Any]) -> dict[str, Any]:
                return await asyncio.wait_for(
                    self._llm.generate(
                        prompt,
                        task_type="classifier",
                        temperature=0,
                        **generate_kwargs,
                    ),
                    timeout=self._classifier_timeout_seconds,
                )

            response = await _generate({"response_format": {"type": "json_object"}})
            content = extract_text_from_response(response)
            if not content or not content.strip():
                return self._classifier_fallback("empty")
            payload = extract_json_object(content, label="follow_up_classifier")
            mode = str(payload.get("mode", "notify")).strip().lower()
            if mode not in {FollowUpMode.INTEGRATE.value, FollowUpMode.NOTIFY.value}:
                return self._classifier_fallback("invalid")
            decided = FollowUpMode(mode)
            FOLLOW_UP_CLASSIFICATIONS_TOTAL.labels(
                mode=decided.value,
                origin=FollowUpOriginKind.TASK_RESULT.value,
                source="classifier",
            ).inc()
            return decided
        except TimeoutError:
            return self._classifier_fallback("timeout")
        except Exception:
            logger.warning(
                "follow_up: same-conversation classifier failed",
                extra={"extra_data": {"reason": "exception"}},
                exc_info=True,
            )
            return self._classifier_fallback("error")
        finally:
            FOLLOW_UP_CLASSIFIER_DURATION.observe(max(0.0, monotonic() - started_at))

    def _recent_conversation_snippets(self, session_id: str, session_cache: Any) -> str:
        try:
            events = session_cache.get_events_since_compaction(
                session_id, ["user_message", "assistant_message"]
            )
        except Exception:
            return ""

        snippets: list[str] = []
        for event in events[-self._max_snippets :]:
            event_type = (
                event.get("type") if isinstance(event, dict) else getattr(event, "type", None)
            )
            event_data = (
                event.get("data", {}) if isinstance(event, dict) else getattr(event, "data", {})
            )
            if event_type not in {"user_message", "assistant_message"} or not isinstance(
                event_data, dict
            ):
                continue
            content = truncate_follow_up_text(
                str(event_data.get("content", "")), max_chars=self._max_snippet_chars
            )
            if not content:
                continue
            role = "User" if event_type == "user_message" else "Assistant"
            snippets.append(f"{role}: {content}")
        return "\n".join(snippets)

    def _classifier_fallback(self, reason: str) -> FollowUpMode:
        FOLLOW_UP_CLASSIFIER_FALLBACKS_TOTAL.labels(reason=reason).inc()
        FOLLOW_UP_CLASSIFICATIONS_TOTAL.labels(
            mode=FollowUpMode.NOTIFY.value,
            origin=FollowUpOriginKind.TASK_RESULT.value,
            source="fallback",
        ).inc()
        return FollowUpMode.NOTIFY
