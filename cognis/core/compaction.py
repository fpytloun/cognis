"""Context compaction for long-running sessions."""

from __future__ import annotations

import json
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

_TOOL_CALL_ARGUMENT_MAX_CHARS = 1_000
_TOOL_RESULT_MAX_CHARS = 2_000


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
            summary = _append_recoverable_tool_output_handles(summary, older_events)
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
            args_text = _stringify_compaction_value(args)
            args_text = _truncate_compaction_text(
                args_text,
                _TOOL_CALL_ARGUMENT_MAX_CHARS,
            )
            metadata = _tool_event_metadata(data)
            payload = f"{name}{metadata} args={args_text}"
        elif etype == "tool_result":
            name = data.get("name", "")
            result = data.get("result") or data.get("output", "")
            is_error = data.get("is_error", False)
            prefix = f"[ERROR] {name}" if is_error else str(name)
            recovery_hint = _tool_result_recovery_hint(data)
            result_text = _truncate_compaction_text(
                _stringify_compaction_value(result),
                _TOOL_RESULT_MAX_CHARS,
                recovery_hint=recovery_hint,
            )
            payload = f"{prefix}{_tool_event_metadata(data)}: {result_text}"
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


def _stringify_compaction_value(value: Any) -> str:
    """Return a stable text representation for compaction prompts."""

    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, default=str, sort_keys=True)
    except TypeError:
        return str(value)


def _truncate_compaction_text(
    text: str,
    max_chars: int,
    *,
    recovery_hint: str | None = None,
) -> str:
    """Truncate text for compaction while preserving recovery instructions."""

    if len(text) <= max_chars:
        return text
    omitted = len(text) - max_chars
    marker = f"[truncated for compaction: omitted {omitted:,} chars"
    if recovery_hint:
        marker += f"; {recovery_hint}"
    marker += "]"
    return text[:max_chars].rstrip() + "\n" + marker


def _tool_event_metadata(data: dict[str, Any]) -> str:
    """Return compact tool metadata that helps summaries retain recovery handles."""

    fields: list[str] = []
    for key in ("call_id", "recovery_call_id", "source_call_id"):
        value = data.get(key)
        if isinstance(value, str) and value.strip():
            fields.append(f"{key}={value!r}")
    output_size = data.get("output_size")
    if isinstance(output_size, int) and output_size > 0:
        fields.append(f"output_size={output_size}")
    if data.get("has_full_output") is True:
        fields.append("has_full_output=true")
    return "" if not fields else " " + " ".join(fields)


def _tool_result_recovery_hint(data: dict[str, Any]) -> str | None:
    """Return concrete recovery calls for a saved tool result, if available."""

    recovery_call_id = data.get("recovery_call_id")
    call_id = recovery_call_id
    if not isinstance(call_id, str) or not call_id.strip():
        call_id = data.get("call_id") if data.get("has_full_output") is True else None
    if not isinstance(call_id, str) or not call_id.strip():
        return None
    quoted = repr(call_id)
    return (
        f"recover with read_tool_output(call_id={quoted}) or "
        f"search_tool_output(call_id={quoted}, pattern='keyword')"
    )


def _recoverable_tool_output_lines(events: list[Any]) -> list[str]:
    """Return deterministic recoverable tool-output handle lines."""

    lines: list[str] = []
    for event in events:
        if event.type != "tool_result":
            continue
        hint = _tool_result_recovery_hint(event.data)
        if not hint:
            continue
        name = event.data.get("name") or "tool"
        lines.append(f"- [{event.seq}] {name}: {hint}")
    return lines


def _append_recoverable_tool_output_handles(summary: str, events: list[Any]) -> str:
    """Ensure LLM compaction cannot drop saved tool-output recovery handles."""

    lines = _recoverable_tool_output_lines(events)
    if not lines:
        return summary
    block_lines = ["Recoverable tool outputs before compaction:"]
    block_lines.extend(lines)
    block = "\n".join(block_lines)
    if block in summary:
        return summary
    return summary.rstrip() + "\n\n" + block


def _mechanical_summary(events: list[Any]) -> str:
    type_counts: dict[str, int] = {}
    recent_user_requests: list[str] = []
    for event in events:
        type_counts[event.type] = type_counts.get(event.type, 0) + 1
        if event.type == "user_message":
            content = event.data.get("content")
            if isinstance(content, str) and content.strip():
                recent_user_requests.append(content.strip().replace("\n", " ")[:120])
    recoverable_tool_outputs = _recoverable_tool_output_lines(events)
    summary_lines = ["Older conversation summary (mechanical fallback):"]
    summary_lines.extend(
        f"- {event_type}: {count}" for event_type, count in sorted(type_counts.items())
    )
    if recent_user_requests:
        summary_lines.append("Recent preserved requests before compaction:")
        summary_lines.extend(f"- {request}" for request in recent_user_requests[-5:])
    if recoverable_tool_outputs:
        summary_lines.append("Recoverable tool outputs before compaction:")
        summary_lines.extend(recoverable_tool_outputs)
    return "\n".join(summary_lines)
