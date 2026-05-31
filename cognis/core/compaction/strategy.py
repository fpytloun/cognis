"""Context compaction strategy for long-running sessions."""

from __future__ import annotations

import asyncio
from typing import Any

import httpx
from prometheus_client import Counter
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from cognis.core.compaction.banding import build_compaction_input
from cognis.core.compaction.fallback import build_sliding_window_summary
from cognis.core.compaction.input_format import format_events_for_compaction
from cognis.core.compaction.recovery import append_recoverable_tool_output_handles
from cognis.core.json_utils import extract_text_from_response
from cognis.logging import get_logger
from cognis.models.session import SessionEvent, SessionModel, with_session_events_turn_id
from cognis.runtime_context import scoped_runtime_context
from cognis.store.queries import get_setting_value

logger = get_logger(__name__)
SAME_SESSION_MODEL_SENTINEL = "__same_session_model__"
LONG_LIVED_CHAT_COMPACTION_ADDENDUM = (
    "This is a long-lived ambient chat. There may be no single task goal. "
    "Preserve standing preferences, ongoing topics, open threads, decisions, "
    "background work references, user-specific context, and recent conversational "
    'continuity. Use "(none)" for task-specific sections that do not apply.'
)

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
COMPACTION_FALLBACK_USED = Counter(
    "cognis_compaction_fallback_used_total",
    "Mechanical fallback compaction uses (alert-worthy — indicates LLM compaction failure)",
    ["trigger"],
)
COMPACTION_DEFERRED_TAIL_SEEDED = Counter(
    "cognis_compaction_deferred_tail_events_seeded_total",
    "Tail events seeded into a new session during deferred rotation",
)


def _is_retryable_llm_error(exc: BaseException) -> bool:
    """Return True for transient errors that warrant a single LLM retry."""
    if isinstance(exc, (TimeoutError, asyncio.TimeoutError, ConnectionError)):
        return True
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code >= 500
    if isinstance(exc, httpx.TransportError):
        return True
    # LiteLLM wraps provider errors; check message for 5xx signals.
    msg = str(exc).lower()
    return any(
        token in msg
        for token in ("rate limit", "503", "502", "504", "connection", "timeout", "overloaded")
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
    preserved_tail_events: list[Any] = []
    tail_start_seq: int | None = None


class CompactionModelContext(BaseModel):
    """Resolved model context for same-session compaction."""

    model: str | None = None
    provider_id: str | None = None
    reasoning_effort: str | None = None


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
        max_input_tokens: int | None = None,
        llm_max_attempts: int = 2,
        max_recursion: int = 2,
        fallback_enabled: bool = True,
    ) -> None:
        self.guardrails = guardrails
        self.llm = llm
        self.session_cache = session_cache
        self.compaction_threshold = compaction_threshold
        self.preserve_turns = preserve_turns
        self.max_input_tokens = max_input_tokens
        self.llm_max_attempts = llm_max_attempts
        self.max_recursion = max_recursion
        self.fallback_enabled = fallback_enabled

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
            max_input_tokens = await get_setting_value(
                db_session, "session.compaction_max_input_tokens", None
            )
            llm_max_attempts = await get_setting_value(
                db_session, "session.compaction_llm_max_attempts", 2
            )
            max_recursion = await get_setting_value(
                db_session, "session.compaction_max_recursion", 2
            )
            fallback_enabled = await get_setting_value(
                db_session, "session.compaction_fallback_enabled", True
            )
        return cls(
            guardrails=guardrails,
            llm=llm,
            session_cache=session_cache,
            compaction_threshold=float(compaction_threshold)
            if isinstance(compaction_threshold, (int, float))
            else 0.85,
            preserve_turns=int(preserve_turns) if isinstance(preserve_turns, int) else 10,
            max_input_tokens=int(max_input_tokens) if isinstance(max_input_tokens, int) else None,
            llm_max_attempts=int(llm_max_attempts) if isinstance(llm_max_attempts, int) else 2,
            max_recursion=int(max_recursion) if isinstance(max_recursion, int) else 2,
            fallback_enabled=bool(fallback_enabled) if isinstance(fallback_enabled, bool) else True,
        )

    def should_compact(self, *, prompt_tokens: int, max_context_tokens: int) -> bool:
        """Check whether token usage exceeds the compaction threshold."""
        if max_context_tokens <= 0:
            return False
        return (prompt_tokens / max_context_tokens) >= self.compaction_threshold

    async def _resolve_compaction_target(
        self,
        model_context: CompactionModelContext | None = None,
        *,
        acting_user_email: str | None = None,
    ) -> tuple[str | None, bool]:
        try:
            try:
                resolved_model = await self.llm.resolve_model(
                    task_type="compaction",
                    acting_user_email=acting_user_email,
                )
            except TypeError:
                resolved_model = await self.llm.resolve_model(task_type="compaction")
        except Exception:
            logger.debug("Failed to resolve compaction model route", exc_info=True)
            return None, False
        if resolved_model == SAME_SESSION_MODEL_SENTINEL:
            return (model_context.model if model_context is not None else None, True)
        return resolved_model, False

    async def _resolve_compaction_model(
        self,
        model_context: CompactionModelContext | None = None,
        *,
        acting_user_email: str | None = None,
    ) -> str | None:
        resolved_model, _use_same_session_model = await self._resolve_compaction_target(
            model_context,
            acting_user_email=acting_user_email,
        )
        return resolved_model

    async def _resolve_max_input_tokens(
        self,
        model_context: CompactionModelContext | None = None,
        *,
        acting_user_email: str | None = None,
    ) -> int | None:
        """Resolve effective token budget for compaction input.

        Uses the explicit setting when set; otherwise derives from the
        compaction model's ``max_input_tokens`` with 15% headroom reserved
        for system prompt, wrappers, and recovery-handle trailer.
        """
        if self.max_input_tokens is not None:
            return self.max_input_tokens
        try:
            resolved_model = await self._resolve_compaction_model(
                model_context,
                acting_user_email=acting_user_email,
            )
            if resolved_model is None:
                return None
            provider_id = model_context.provider_id if model_context is not None else None
            if provider_id is not None:
                try:
                    model_info = await self.llm.get_model_info(
                        resolved_model,
                        provider_id=provider_id,
                        acting_user_email=acting_user_email,
                    )
                except TypeError:
                    model_info = await self.llm.get_model_info(
                        resolved_model,
                        provider_id=provider_id,
                    )
            else:
                try:
                    model_info = await self.llm.get_model_info(
                        resolved_model,
                        acting_user_email=acting_user_email,
                    )
                except TypeError:
                    model_info = await self.llm.get_model_info(resolved_model)
            raw = getattr(model_info, "max_input_tokens", None)
            if isinstance(raw, int) and raw > 0:
                return int(raw * 0.85)
        except Exception:
            logger.debug(
                "compaction: could not resolve model max_input_tokens; using char fallback"
            )
        return None

    async def compact(
        self,
        session: SessionModel,
        *,
        trigger: str = "manual",
        model_context: CompactionModelContext | None = None,
        long_lived_chat: bool = False,
    ) -> CompactionResult:
        """Attempt LLM-driven compaction of buffered session history.

        *trigger* is used for observability labels: ``"manual"`` for
        ``/compact`` slash command, ``"automatic"`` for token-threshold
        compaction during context assembly.

        Retries once on transient errors before falling back to mechanical
        compaction (or raising a classified failure when fallback is disabled).
        """
        entry = self.session_cache.get_entry(session.session_id)
        if entry is None:
            entry = await self.session_cache.refresh(session)

        older_events, preserved_events = _split_events(
            entry.events,
            preserve_turns=self.preserve_turns,
        )
        if not older_events:
            return CompactionResult(compacted=False, method="noop")

        from cognis.core.agent_registry import SYSTEM_AGENTS

        compaction_agent = SYSTEM_AGENTS.get("system:compaction")
        compaction_prompt = (
            compaction_agent.system_prompt
            if compaction_agent and compaction_agent.system_prompt
            else "Summarize the conversation history concisely."
        )
        if long_lived_chat:
            compaction_prompt = f"{compaction_prompt}\n\n{LONG_LIVED_CHAT_COMPACTION_ADDENDUM}"

        # Resolve token budget and build the three-band input.
        max_input_tokens = await self._resolve_max_input_tokens(
            model_context,
            acting_user_email=session.user_email,
        )
        resolved_model, use_same_session_model = await self._resolve_compaction_target(
            model_context,
            acting_user_email=session.user_email,
        )

        compaction_input = build_compaction_input(
            older_events,
            max_input_tokens=max_input_tokens,
            count_tokens_fn=getattr(self.llm, "count_tokens", None),
            model=resolved_model,
        )
        compacted_text = compaction_input.text

        if entry.last_compaction_summary:
            compacted_text = (
                "Update the anchored summary below using the new conversation history.\n"
                "<previous-summary>\n"
                f"{entry.last_compaction_summary}\n"
                "</previous-summary>\n\n"
                "<new-history>\n"
                f"{compacted_text}\n"
                "</new-history>"
            )

        prompt_messages = [
            {"role": "system", "content": compaction_prompt},
            {"role": "user", "content": compacted_text},
        ]

        summary: str | None = None
        last_exc: BaseException | None = None

        for attempt in range(1, self.llm_max_attempts + 1):
            try:
                response = await self.llm.generate(
                    prompt_messages,
                    model=resolved_model if use_same_session_model else None,
                    task_type="compaction",
                    cognis_session_id=session.session_id,
                    acting_user_email=session.user_email,
                    provider_id=model_context.provider_id
                    if use_same_session_model and model_context is not None
                    else None,
                    reasoning_effort=model_context.reasoning_effort
                    if use_same_session_model and model_context is not None
                    else None,
                )
                candidate = extract_text_from_response(response).strip()
                if not candidate:
                    raise ValueError("LLM compaction returned empty summary")
                summary = candidate
                break
            except Exception as exc:
                last_exc = exc
                if attempt < self.llm_max_attempts and _is_retryable_llm_error(exc):
                    wait = 2.0 ** (attempt - 1)  # 1s, 2s, …
                    logger.warning(
                        "compaction: LLM attempt %d/%d failed (retryable); retrying in %.1fs",
                        attempt,
                        self.llm_max_attempts,
                        wait,
                        extra={"extra_data": {"session_id": session.session_id}},
                        exc_info=True,
                    )
                    await asyncio.sleep(wait)
                    continue
                # Non-retryable or retries exhausted.
                break

        if summary is None:
            logger.warning(
                "compaction: LLM compaction failed after %d attempt(s)",
                self.llm_max_attempts,
                extra={"extra_data": {"session_id": session.session_id}},
                exc_info=last_exc is not None,
            )
            if not self.fallback_enabled:
                return CompactionResult(compacted=False, method="llm_failed")
            return await self._mechanical_fallback(
                session,
                older_events=older_events,
                preserved_events=preserved_events,
                trigger=trigger,
                model_context=model_context,
            )

        summary = append_recoverable_tool_output_handles(summary, older_events)
        COMPACTION_TOTAL.labels(trigger=trigger, method="llm").inc()
        return await self._record_compaction(
            session=session,
            summary=summary,
            formatted_input=compaction_input.text,
            older_events=older_events,
            preserved_tail_events=preserved_events,
            method="llm",
            resolved_model=resolved_model,
        )

    async def compact_with_fallback(
        self,
        session: SessionModel,
        *,
        trigger: str = "manual",
        model_context: CompactionModelContext | None = None,
    ) -> CompactionResult:
        """Use sliding-window mechanical compaction directly (bypasses LLM).

        Called by ``_auto_compact`` when the LLM compaction times out at the
        outer ``wait_for`` boundary (the strategy's own retry loop never gets
        a chance to run in that case).

        Respects ``fallback_enabled``: when ``False``, returns a classified
        failure result instead of producing a degraded mechanical summary.
        """
        if not self.fallback_enabled:
            return CompactionResult(compacted=False, method="llm_failed")

        entry = self.session_cache.get_entry(session.session_id)
        if entry is None:
            entry = await self.session_cache.refresh(session)

        older_events, preserved_events = _split_events(
            entry.events,
            preserve_turns=self.preserve_turns,
        )
        if not older_events:
            return CompactionResult(compacted=False, method="noop")

        return await self._mechanical_fallback(
            session,
            older_events=older_events,
            preserved_events=preserved_events,
            trigger=trigger,
            model_context=model_context,
        )

    async def _mechanical_fallback(
        self,
        session: SessionModel,
        *,
        older_events: list[Any],
        preserved_events: list[Any],
        trigger: str,
        model_context: CompactionModelContext | None = None,
    ) -> CompactionResult:
        summary = build_sliding_window_summary(older_events)
        COMPACTION_TOTAL.labels(trigger=trigger, method="mechanical_sliding_window").inc()
        COMPACTION_FALLBACK_USED.labels(trigger=trigger).inc()
        try:
            resolved_model = await self._resolve_compaction_model(
                model_context,
                acting_user_email=session.user_email,
            )
        except Exception:
            resolved_model = None
        return await self._record_compaction(
            session=session,
            summary=summary,
            formatted_input=format_events_for_compaction(older_events),
            older_events=older_events,
            preserved_tail_events=preserved_events,
            method="mechanical_sliding_window",
            resolved_model=resolved_model,
        )

    async def _record_compaction(
        self,
        *,
        session: SessionModel,
        summary: str,
        formatted_input: str,
        older_events: list[Any],
        preserved_tail_events: list[Any],
        method: str,
        resolved_model: str | None,
    ) -> CompactionResult:
        count_tokens = getattr(self.llm, "count_tokens", None)
        if callable(count_tokens) and resolved_model:
            tokens_before = count_tokens(formatted_input, resolved_model)
            tokens_after = count_tokens(summary, resolved_model)
        else:
            tokens_before = 0
            tokens_after = 0

        turns_compacted = sum(1 for event in older_events if event.type == "user_message")
        compaction_event = SessionEvent(
            type="compaction_summary",
            data={
                "summary": summary,
                "tokens_before": tokens_before,
                "tokens_after": tokens_after,
                "turns_compacted": turns_compacted,
                "method": method,
                "tail_start_seq": (preserved_tail_events[0].seq if preserved_tail_events else None),
                "tail_event_count": len(preserved_tail_events),
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
            preserved_tail_events=preserved_tail_events,
            tail_start_seq=preserved_tail_events[0].seq if preserved_tail_events else None,
        )
