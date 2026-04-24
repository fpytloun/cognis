"""Context compaction for long-running sessions."""

from __future__ import annotations

from typing import Any

from prometheus_client import Counter
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from cognis.core.attachment_utils import merge_content_and_attachment_note
from cognis.core.json_utils import extract_text_from_response
from cognis.logging import get_logger
from cognis.models.session import SessionEvent, SessionModel, with_session_events_turn_id
from cognis.runtime_context import scoped_runtime_context
from cognis.store.queries import get_setting_value

logger = get_logger(__name__)

COMPACTION_TOTAL = Counter(
    "cognis_session_compactions_total",
    "Total compaction attempts",
    ["trigger", "method"],
)
ROTATION_TOTAL = Counter(
    "cognis_session_rotations_total",
    "Total session rotations after compaction",
    ["trigger"],
)


class CompactionResult(BaseModel):
    """Outcome of a compaction attempt."""

    compacted: bool
    method: str
    summary: str | None = None
    compaction_seq: int | None = None
    turns_compacted: int = 0
    tokens_before: int = 0
    tokens_after: int = 0


class CompactionStrategy:
    """Compact session history when token usage crosses a threshold."""

    def __init__(
        self,
        *,
        guardrails: Any,
        llm: Any,
        session_cache: Any,
        compaction_threshold: float,
        preserve_turns: int,
    ) -> None:
        self.guardrails = guardrails
        self.llm = llm
        self.session_cache = session_cache
        self.compaction_threshold = compaction_threshold
        self.preserve_turns = preserve_turns

    @classmethod
    async def from_session_factory(
        cls,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        guardrails: Any,
        llm: Any,
        session_cache: Any,
    ) -> CompactionStrategy:
        async with session_factory() as db_session:
            compaction_threshold = await get_setting_value(
                db_session, "session.compaction_threshold", 0.85
            )
            preserve_turns = await get_setting_value(
                db_session, "session.compaction_preserve_turns", 10
            )
        return cls(
            guardrails=guardrails,
            llm=llm,
            session_cache=session_cache,
            compaction_threshold=float(compaction_threshold)
            if isinstance(compaction_threshold, (int, float))
            else 0.85,
            preserve_turns=int(preserve_turns) if isinstance(preserve_turns, int) else 10,
        )

    def should_compact(self, *, prompt_tokens: int, max_context_tokens: int) -> bool:
        """Check whether token usage exceeds the compaction threshold."""

        if max_context_tokens <= 0:
            return False
        return (prompt_tokens / max_context_tokens) >= self.compaction_threshold

    async def compact(self, session: SessionModel, *, trigger: str = "manual") -> CompactionResult:
        """Attempt LLM-driven compaction of buffered session history.

        *trigger* is used for observability labels: ``"manual"`` for
        ``/compact`` slash command, ``"automatic"`` for token-threshold
        compaction during context assembly.
        """

        entry = self.session_cache.get_entry(session.session_id)
        if entry is None:
            entry = await self.session_cache.refresh(session)

        older_events, _ = _split_events(entry.events, preserve_turns=self.preserve_turns)
        if not older_events:
            return CompactionResult(compacted=False, method="noop")

        from cognis.core.agent_registry import SYSTEM_AGENTS

        compaction_agent = SYSTEM_AGENTS.get("system:compaction")
        compaction_prompt = (
            compaction_agent.system_prompt
            if compaction_agent and compaction_agent.system_prompt
            else "Summarize the conversation history concisely."
        )

        prompt_messages = [
            {"role": "system", "content": compaction_prompt},
            {"role": "user", "content": _format_events_for_compaction(older_events)},
        ]

        try:
            response = await self.llm.generate(
                prompt_messages,
                task_type="compaction",
            )
            summary = extract_text_from_response(response).strip()
            if not summary:
                raise ValueError("LLM compaction returned empty summary")
        except Exception:
            logger.warning(
                "LLM compaction failed, falling back to mechanical",
                extra={"extra_data": {"session_id": session.session_id}},
            )
            return await self.compact_with_fallback(session, trigger=trigger)

        COMPACTION_TOTAL.labels(trigger=trigger, method="llm").inc()
        return await self._record_compaction(
            session=session,
            summary=summary,
            older_events=older_events,
            method="llm",
        )

    async def compact_with_fallback(
        self, session: SessionModel, *, trigger: str = "manual"
    ) -> CompactionResult:
        """Use metadata-only compaction if LLM compaction fails."""

        entry = self.session_cache.get_entry(session.session_id)
        if entry is None:
            entry = await self.session_cache.refresh(session)

        older_events, _ = _split_events(entry.events, preserve_turns=self.preserve_turns)
        if not older_events:
            return CompactionResult(compacted=False, method="noop")
        summary = _mechanical_summary(older_events)
        COMPACTION_TOTAL.labels(trigger=trigger, method="mechanical").inc()
        return await self._record_compaction(
            session=session,
            summary=summary,
            older_events=older_events,
            method="mechanical",
        )

    async def _record_compaction(
        self,
        *,
        session: SessionModel,
        summary: str,
        older_events: list[Any],
        method: str,
    ) -> CompactionResult:
        resolved_model = await self.llm.resolve_model(task_type="compaction")
        tokens_before = self.llm.count_tokens(
            _format_events_for_compaction(older_events), resolved_model
        )
        tokens_after = self.llm.count_tokens(summary, resolved_model)
        turns_compacted = sum(1 for event in older_events if event.type == "user_message")
        compaction_event = SessionEvent(
            type="compaction_summary",
            data={
                "summary": summary,
                "tokens_before": tokens_before,
                "tokens_after": tokens_after,
                "turns_compacted": turns_compacted,
                "method": method,
            },
        )
        # Retry is handled by the Intaris provider (exponential backoff).
        idempotency_key = f"{session.session_id}:compaction:{method}:{older_events[-1].seq}"
        compaction_events = with_session_events_turn_id([compaction_event], None)
        with scoped_runtime_context(user_email=session.user_email, agent_id=session.agent_id):
            append_result = await self.guardrails.record_events(
                session_id=session.intaris_session_id or session.session_id,
                events=compaction_events,
                idempotency_key=idempotency_key,
            )
        await self.session_cache.apply_compaction(
            session,
            summary=summary,
            compaction_seq=append_result.last_seq,
        )
        return CompactionResult(
            compacted=True,
            method=method,
            summary=summary,
            compaction_seq=append_result.last_seq,
            turns_compacted=turns_compacted,
            tokens_before=tokens_before,
            tokens_after=tokens_after,
        )


def _split_events(events: list[Any], preserve_turns: int) -> tuple[list[Any], list[Any]]:
    user_event_indices = [
        index for index, event in enumerate(events) if event.type == "user_message"
    ]
    if len(user_event_indices) <= preserve_turns:
        preserve_events = min(max(50, preserve_turns * 20), 200)
        if len(events) <= preserve_events:
            return [], list(events)
        keep_from = len(events) - preserve_events
        return list(events[:keep_from]), list(events[keep_from:])
    keep_from = user_event_indices[-preserve_turns]
    return list(events[:keep_from]), list(events[keep_from:])


def _format_events_for_compaction(events: list[Any]) -> str:
    lines: list[str] = []
    for event in events:
        etype = event.type
        data = event.data

        if etype in ("user_message", "assistant_message"):
            payload = merge_content_and_attachment_note(
                str(data.get("content", "")),
                [a for a in data.get("attachments", []) if isinstance(a, dict)],
            )
        elif etype == "tool_call":
            name = data.get("name", "unknown")
            args = data.get("arguments", "")
            # Truncate large arguments for the compaction prompt
            if isinstance(args, str) and len(args) > 500:
                args = args[:500] + "..."
            payload = f"{name}({args})"
        elif etype == "tool_result":
            name = data.get("name", "")
            result = data.get("result") or data.get("output", "")
            is_error = data.get("is_error", False)
            prefix = f"[ERROR] {name}: " if is_error else f"{name}: "
            # Truncate large results for the compaction prompt
            if isinstance(result, str) and len(result) > 1000:
                result = result[:1000] + "..."
            payload = prefix + str(result)
        elif etype == "delegation":
            status = data.get("status", "")
            mode = data.get("mode", "")
            summary = data.get("result_summary", "")
            payload = f"[{mode}/{status}] {summary}"
        else:
            content = data.get("content")
            payload = content if isinstance(content, str) else str(data)

        lines.append(f"[{event.seq}] {etype}: {payload}")
    return "\n".join(lines)


def _mechanical_summary(events: list[Any]) -> str:
    type_counts: dict[str, int] = {}
    recent_user_requests: list[str] = []
    for event in events:
        type_counts[event.type] = type_counts.get(event.type, 0) + 1
        if event.type == "user_message":
            content = event.data.get("content")
            if isinstance(content, str) and content.strip():
                recent_user_requests.append(content.strip().replace("\n", " ")[:120])
    summary_lines = ["Older conversation summary (mechanical fallback):"]
    summary_lines.extend(
        f"- {event_type}: {count}" for event_type, count in sorted(type_counts.items())
    )
    if recent_user_requests:
        summary_lines.append("Recent preserved requests before compaction:")
        summary_lines.extend(f"- {request}" for request in recent_user_requests[-5:])
    return "\n".join(summary_lines)
