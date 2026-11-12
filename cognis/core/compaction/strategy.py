"""Context compaction strategy for long-running sessions."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import httpx
from prometheus_client import Counter
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from cognis import json_codec as json
from cognis.core.compaction.banding import build_compaction_input
from cognis.core.compaction.fallback import build_sliding_window_summary
from cognis.core.compaction.input_format import format_events_for_compaction
from cognis.core.compaction.publication import compaction_publication_guard
from cognis.core.compaction.recovery import append_recoverable_tool_output_handles
from cognis.core.json_utils import extract_text_from_response
from cognis.core.session_cache import is_retry_admission_notice
from cognis.logging import get_logger
from cognis.models.session import SessionEvent, SessionModel, with_session_events_turn_id
from cognis.runtime_context import scoped_runtime_context
from cognis.store.queries import get_setting_value

logger = get_logger(__name__)
SAME_SESSION_MODEL_SENTINEL = "__same_session_model__"
_PRESERVED_TAIL_TOKEN_RATIO = 0.30
_DEFAULT_PRESERVED_TAIL_EVENT_COUNT = 50
_MAX_PRESERVED_TAIL_EVENT_COUNT = 200
LONG_LIVED_CHAT_COMPACTION_ADDENDUM = (
    "This is a long-lived ambient chat. There may be no single task goal. "
    "Preserve standing preferences, ongoing topics, open threads, decisions, "
    "background work references, user-specific context, and recent conversational "
    'continuity. Use "(none)" for task-specific sections that do not apply.'
)
IDLE_RENEWAL_COMPACTION_ADDENDUM = (
    "This summary seeds a renewed activity scope after an idle checkpoint. "
    "Preserve ambient context, standing preferences, decisions, and recent conversational "
    "continuity. Describe prior TODOs, delegated or managed work, and task next steps only "
    "as historical context. Do not present them as active obligations in the renewed scope."
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


class CompactionConfigurationError(RuntimeError):
    """Compaction cannot proceed because model routing/configuration is invalid."""


def _is_retryable_llm_error(exc: BaseException) -> bool:
    """Return True for transient errors that warrant a single LLM retry."""
    if isinstance(exc, (TimeoutError, asyncio.TimeoutError, ConnectionError)):
        return True
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code == 429 or exc.response.status_code >= 500
    if isinstance(exc, httpx.TransportError):
        return True
    # LiteLLM wraps provider errors; check message for 5xx signals.
    msg = str(exc).lower()
    return any(
        token in msg
        for token in (
            "rate limit",
            "429",
            "503",
            "502",
            "504",
            "connection",
            "timeout",
            "overloaded",
        )
    )


class CompactionResult(BaseModel):
    """Outcome of a compaction attempt."""

    compacted: bool
    method: str
    summary: str | None = None
    compaction_seq: int | None = None
    turns_compacted: int = 0
    # Summarized events. A cut inside one long user turn compacts history
    # without summarizing a whole turn, so ``turns_compacted`` can be zero.
    events_compacted: int = 0
    tokens_before: int = 0
    tokens_after: int = 0
    preserved_tail_events: list[Any] = []
    tail_start_seq: int | None = None


class CompactionModelContext(BaseModel):
    """Resolved model context for same-session compaction."""

    model: str | None = None
    provider_id: str | None = None
    reasoning_effort: str | None = None
    compaction_id: str | None = None


def _split_events(
    events: list[Any],
    preserve_turns: int,
    *,
    tail_token_budget: int | None = None,
    count_tokens_fn: Any = None,
    model: str | None = None,
) -> tuple[list[Any], list[Any]]:
    """Split events into summarized history and a verbatim preserved tail.

    A turn is delimited by user messages, but system-started continuations can
    extend one user turn far beyond the tail budget. In that case the cut falls
    inside the latest turn at a tool-cycle boundary. Admission anchors for the
    executions represented in the tail are pinned before it, including retry
    source admissions. They remain verbatim, keep their relative order, and are
    excluded from ``older`` so continuation validation still finds the active
    instruction after rotation, even after later question answers.
    """
    user_event_indices = [
        index for index, event in enumerate(events) if event.type == "user_message"
    ]
    max_preserved_turns = max(1, int(preserve_turns or 1))
    latest_turn_start = user_event_indices[-1] if user_event_indices else None

    if (
        tail_token_budget is not None
        and tail_token_budget > 0
        and count_tokens_fn is not None
        and model is not None
        and latest_turn_start is not None
    ):
        intra_turn_cut = _oversized_latest_turn_cut(
            events,
            latest_turn_start,
            tail_token_budget=tail_token_budget,
            count_tokens_fn=count_tokens_fn,
            model=model,
        )
        if intra_turn_cut is not None:
            return _pin_tail_admissions(events, intra_turn_cut, latest_turn_start)
        keep_from = _token_budgeted_tail_start(
            events,
            user_event_indices,
            max_preserved_turns=max_preserved_turns,
            tail_token_budget=tail_token_budget,
            count_tokens_fn=count_tokens_fn,
            model=model,
        )
        if keep_from is not None:
            keep_from = _snap_keep_from_to_cycle_boundary(events, keep_from)
            return _pin_tail_admissions(events, keep_from, latest_turn_start)

    if len(user_event_indices) <= max_preserved_turns:
        preserve_events = min(
            max(_DEFAULT_PRESERVED_TAIL_EVENT_COUNT, max_preserved_turns * 20),
            _MAX_PRESERVED_TAIL_EVENT_COUNT,
        )
        if len(events) <= preserve_events:
            return [], list(events)
        keep_from = len(events) - preserve_events
        keep_from = _snap_keep_from_to_cycle_boundary(events, keep_from)
        return _pin_tail_admissions(events, keep_from, latest_turn_start)
    keep_from = user_event_indices[-max_preserved_turns]
    return _pin_tail_admissions(events, keep_from, latest_turn_start)


def _is_turn_admission_event(event: Any) -> bool:
    """Return whether continuation validation needs this event verbatim."""
    return getattr(event, "type", None) == "user_message" or is_retry_admission_notice(event)


def _pin_tail_admissions(
    events: list[Any],
    keep_from: int,
    latest_turn_start: int | None,
) -> tuple[list[Any], list[Any]]:
    """Keep admission dependencies for executions represented by the retained tail."""
    required_turns = {
        turn_id
        for event in events[keep_from:]
        if isinstance(turn_id := event.data.get("turn_id"), str) and turn_id
    }
    pinned_indices: set[int] = set()
    # Backward traversal follows retained retry notices to their earlier source
    # admissions. Later question answers do not replace execution admission.
    for index in range(len(events) - 1, -1, -1):
        event = events[index]
        if not _is_turn_admission_event(event):
            continue
        if index < keep_from:
            in_latest_turn = latest_turn_start is not None and index >= latest_turn_start
            turn_id = event.data.get("turn_id")
            if not in_latest_turn and (
                not isinstance(turn_id, str) or turn_id not in required_turns
            ):
                continue
            pinned_indices.add(index)
        source_turn = event.data.get("retry_source_turn_id")
        if isinstance(source_turn, str) and source_turn:
            required_turns.add(source_turn)
    pinned = [event for index, event in enumerate(events[:keep_from]) if index in pinned_indices]
    older = [event for index, event in enumerate(events[:keep_from]) if index not in pinned_indices]
    return older, pinned + list(events[keep_from:])


def _event_prompt_text(event: Any) -> str:
    """Return text approximating an event's model-prompt cost.

    Compaction formatting truncates tool results, so it underestimates what the
    preserved tail costs once replayed. Use the longer of the formatted line and
    the raw payload.
    """
    formatted = format_events_for_compaction([event])
    data = getattr(event, "data", None)
    if not isinstance(data, dict):
        return formatted
    raw_parts: list[str] = []
    for key in ("content", "result", "output", "arguments"):
        value = data.get(key)
        if value is None or value == "":
            continue
        raw_parts.append(value if isinstance(value, str) else json.dumps(value, default=str))
    raw = "\n".join(raw_parts)
    return raw if len(raw) > len(formatted) else formatted


def _oversized_latest_turn_cut(
    events: list[Any],
    latest_turn_start: int,
    *,
    tail_token_budget: int,
    count_tokens_fn: Any,
    model: str,
) -> int | None:
    """Return a cut inside the latest turn when that turn exceeds the tail budget.

    Walks backward from the end and stops once the budget is exceeded, so cost
    is bounded by the budget rather than the size of the turn. Returns ``None``
    when the whole latest turn fits (normal turn-boundary splitting applies) or
    when no cycle boundary leaves anything to summarize.
    """
    _, latest_events = _pin_tail_admissions(events, latest_turn_start, latest_turn_start)
    anchors = [event for event in latest_events if _is_turn_admission_event(event)]
    try:
        budget = tail_token_budget - int(
            count_tokens_fn(format_events_for_compaction(anchors), model)
        )
        cost = 0
        keep_from: int | None = None
        for index in range(len(events) - 1, latest_turn_start, -1):
            event = events[index]
            if not _is_turn_admission_event(event):
                cost += int(count_tokens_fn(_event_prompt_text(event), model))
            if cost > budget:
                break
            # Only a stable boundary counts: a later call inside a parallel
            # batch would snap back over earlier calls whose cost is unbudgeted.
            if (
                _is_compaction_tail_cycle_boundary(event)
                and _snap_keep_from_to_cycle_boundary(events, index) == index
            ):
                keep_from = index
        else:
            return None
    except Exception:
        logger.warning("compaction: latest-turn token estimate failed", exc_info=True)
        return None
    # Even the latest complete cycle can exceed the budget alone; keep it verbatim.
    if keep_from is None:
        keep_from = _snap_keep_from_to_cycle_boundary(events, len(events) - 1)
    if keep_from <= latest_turn_start + 1:
        return None
    return keep_from


_SUMMARIZABLE_EVENT_TYPES = frozenset(
    {
        "user_message",
        "assistant_message",
        "assistant_thinking",
        "tool_call",
        "tool_result",
        "delegation",
    }
)


def _has_summarizable_history(older_events: list[Any]) -> bool:
    """Return whether compaction would remove conversation content.

    Context seeds and lifecycle notices alone do not justify recording a
    summary or rotating the session.
    """
    return any(getattr(event, "type", None) in _SUMMARIZABLE_EVENT_TYPES for event in older_events)


def _summary_input_events(older_events: list[Any], preserved_events: list[Any]) -> list[Any]:
    """Return events the summarizer reads, including pinned admission anchors.

    Pinned anchors stay verbatim in the tail, but the summarizer still needs the
    instruction to describe progress on it.
    """
    if not older_events:
        return []
    last_older_seq = getattr(older_events[-1], "seq", None)
    if not isinstance(last_older_seq, int):
        return list(older_events)
    pinned = [
        event
        for event in preserved_events
        if isinstance(getattr(event, "seq", None), int) and event.seq < last_older_seq
    ]
    if not pinned:
        return list(older_events)
    return sorted([*older_events, *pinned], key=lambda event: event.seq)


def _tail_layout(
    preserved_events: list[Any], older_events: list[Any]
) -> tuple[int | None, list[int]]:
    """Return the contiguous tail start and seqs of pinned anchors before it."""
    last_older_seq = (
        older_events[-1].seq
        if older_events and isinstance(getattr(older_events[-1], "seq", None), int)
        else None
    )
    pinned_seqs: list[int] = []
    tail_start_seq: int | None = None
    for event in preserved_events:
        seq = getattr(event, "seq", None)
        if not isinstance(seq, int):
            continue
        if last_older_seq is not None and seq < last_older_seq:
            pinned_seqs.append(seq)
        elif tail_start_seq is None:
            tail_start_seq = seq
    return tail_start_seq, pinned_seqs


def _token_budgeted_tail_start(
    events: list[Any],
    user_event_indices: list[int],
    *,
    max_preserved_turns: int,
    tail_token_budget: int,
    count_tokens_fn: Any,
    model: str,
) -> int | None:
    selected_keep_from: int | None = None
    for boundary_index in reversed(user_event_indices):
        candidate = events[boundary_index:]
        user_turn_count = sum(1 for event in candidate if event.type == "user_message")
        if user_turn_count > max_preserved_turns:
            break
        _, candidate = _pin_tail_admissions(events, boundary_index, boundary_index)
        try:
            candidate_tokens = int(count_tokens_fn(format_events_for_compaction(candidate), model))
        except Exception:
            return None
        if candidate_tokens > tail_token_budget and selected_keep_from is not None:
            break
        selected_keep_from = boundary_index
        if user_turn_count >= max_preserved_turns:
            break
    return selected_keep_from


def _snap_keep_from_to_cycle_boundary(events: list[Any], keep_from: int) -> int:
    keep_from = max(0, min(keep_from, len(events)))
    while keep_from > 0 and not _is_compaction_tail_cycle_boundary(events[keep_from]):
        keep_from -= 1
    if keep_from < len(events) and getattr(events[keep_from], "type", None) == "tool_call":
        while keep_from > 0 and getattr(events[keep_from - 1], "type", None) == "tool_call":
            keep_from -= 1
    return keep_from


def _is_compaction_tail_cycle_boundary(event: Any) -> bool:
    return getattr(event, "type", None) in {
        "user_message",
        "assistant_message",
        "assistant_thinking",
        # Preserve the complete tool cycle from its call onward. Without this
        # boundary, a long single user turn made only of tool events can snap
        # the tail to index zero and incorrectly make compaction a no-op.
        "tool_call",
    }


def _preserved_tail_token_budget(entry: Any, max_input_tokens: int | None) -> int | None:
    available_prompt_tokens = getattr(entry, "available_prompt_tokens", 0)
    if isinstance(available_prompt_tokens, int) and available_prompt_tokens > 0:
        return max(1, int(available_prompt_tokens * _PRESERVED_TAIL_TOKEN_RATIO))
    if isinstance(max_input_tokens, int) and max_input_tokens > 0:
        return max(1, int(max_input_tokens * _PRESERVED_TAIL_TOKEN_RATIO))
    return None


def _previous_summary_wrapper(previous_summary: str, new_history: str = "") -> str:
    return (
        "Update the anchored summary below using the new conversation history.\n"
        "<previous-summary>\n"
        f"{previous_summary}\n"
        "</previous-summary>\n\n"
        "<new-history>\n"
        f"{new_history}\n"
        "</new-history>"
    )


def _budget_without_previous_summary(
    *,
    max_input_tokens: int | None,
    previous_summary: str | None,
    count_tokens_fn: Any,
    model: str | None,
) -> int | None:
    if not previous_summary or max_input_tokens is None:
        return max_input_tokens
    wrapper_without_history = _previous_summary_wrapper(previous_summary, "")
    try:
        reserved = (
            int(count_tokens_fn(wrapper_without_history, model))
            if count_tokens_fn is not None and model is not None
            else max(1, len(wrapper_without_history) // 4)
        )
    except Exception:
        reserved = max(1, len(wrapper_without_history) // 4)
    return max(1, max_input_tokens - reserved)


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
        session_factory: async_sessionmaker[AsyncSession] | None = None,
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
        self.session_factory = session_factory

    @classmethod
    async def from_session_factory(
        cls,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        guardrails: Any,
        llm: Any,
        session_cache: Any,
    ) -> CompactionStrategy:
        settings = await cls._load_settings(session_factory)
        return cls(
            guardrails=guardrails,
            llm=llm,
            session_cache=session_cache,
            session_factory=session_factory,
            **settings,
        )

    @staticmethod
    async def _load_settings(
        session_factory: async_sessionmaker[AsyncSession],
    ) -> dict[str, Any]:
        async with session_factory() as db_session:
            compaction_threshold = await get_setting_value(
                db_session, "session.compaction_threshold", 0.85
            )
            preserve_turns = await get_setting_value(
                db_session, "session.compaction_preserve_turns", 10
            )
            max_input_tokens = await get_setting_value(
                db_session, "session.compaction_max_input_tokens", 0
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
        return {
            "compaction_threshold": float(compaction_threshold)
            if isinstance(compaction_threshold, (int, float))
            else 0.85,
            "preserve_turns": int(preserve_turns) if isinstance(preserve_turns, int) else 10,
            "max_input_tokens": int(max_input_tokens)
            if isinstance(max_input_tokens, int) and max_input_tokens > 0
            else None,
            "llm_max_attempts": max(1, int(llm_max_attempts))
            if isinstance(llm_max_attempts, int)
            else 2,
            "max_recursion": max(1, int(max_recursion)) if isinstance(max_recursion, int) else 2,
            "fallback_enabled": bool(fallback_enabled)
            if isinstance(fallback_enabled, bool)
            else True,
        }

    async def refresh_settings(self) -> None:
        """Refresh DB-backed compaction settings immediately before use."""

        if self.session_factory is None:
            return
        try:
            settings = await self._load_settings(self.session_factory)
        except Exception:
            logger.debug(
                "compaction: failed to refresh settings; using cached values", exc_info=True
            )
            return
        self.compaction_threshold = settings["compaction_threshold"]
        self.preserve_turns = settings["preserve_turns"]
        self.max_input_tokens = settings["max_input_tokens"]
        self.llm_max_attempts = settings["llm_max_attempts"]
        self.max_recursion = settings["max_recursion"]
        self.fallback_enabled = settings["fallback_enabled"]

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
            same_session_model = model_context.model if model_context is not None else None
            if same_session_model and same_session_model != SAME_SESSION_MODEL_SENTINEL:
                return same_session_model, True
            raise CompactionConfigurationError(
                "compaction route uses __same_session_model__ but no resolved session model "
                "context was provided"
            )
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
            resolved_model, use_same_session_model = await self._resolve_compaction_target(
                model_context,
                acting_user_email=acting_user_email,
            )
            if resolved_model is None:
                return None
            provider_id = (
                model_context.provider_id
                if use_same_session_model and model_context is not None
                else None
            )
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
        await self.refresh_settings()
        return await self._generate_summary(
            session,
            trigger=trigger,
            model_context=model_context,
            long_lived_chat=long_lived_chat,
            record=True,
        )

    async def preview_summary(
        self,
        session: SessionModel,
        *,
        trigger: str = "preview",
        model_context: CompactionModelContext | None = None,
        long_lived_chat: bool = False,
    ) -> CompactionResult:
        """Generate a compaction-format summary without mutating session state."""
        await self.refresh_settings()
        return await self._generate_summary(
            session,
            trigger=trigger,
            model_context=model_context,
            long_lived_chat=long_lived_chat,
            record=False,
        )

    async def preview_summary_from_events(
        self,
        session: SessionModel,
        *,
        events: list[Any],
        last_compaction_summary: str | None = None,
        trigger: str = "preview",
        model_context: CompactionModelContext | None = None,
        long_lived_chat: bool = False,
    ) -> CompactionResult:
        """Generate a read-only compaction-format summary from caller-provided events."""
        await self.refresh_settings()
        entry = SimpleNamespace(
            events=list(events),
            last_compaction_summary=last_compaction_summary,
        )
        return await self._generate_summary_from_entry(
            session,
            entry=entry,
            trigger=trigger,
            model_context=model_context,
            long_lived_chat=long_lived_chat,
            record=False,
        )

    async def _generate_summary(
        self,
        session: SessionModel,
        *,
        trigger: str,
        model_context: CompactionModelContext | None,
        long_lived_chat: bool,
        record: bool,
    ) -> CompactionResult:
        """Generate a compaction summary and optionally record it."""
        entry = self.session_cache.get_entry(session.session_id)
        if (
            entry is None
            or not bool(getattr(entry, "initialized", False))
            or bool(getattr(entry, "canonical_stale", False))
        ):
            if not record:
                return CompactionResult(compacted=False, method="noop")
            entry = await self.session_cache.refresh(session)
        return await self._generate_summary_from_entry(
            session,
            entry=await self.session_cache.acquire_context_snapshot(session),
            trigger=trigger,
            model_context=model_context,
            long_lived_chat=long_lived_chat,
            record=record,
        )

    async def _generate_summary_from_entry(
        self,
        session: SessionModel,
        *,
        entry: Any,
        trigger: str,
        model_context: CompactionModelContext | None,
        long_lived_chat: bool,
        record: bool,
    ) -> CompactionResult:
        """Generate a compaction summary from an event-bearing cache-like entry."""

        # Resolve the model and token budget before splitting so the preserved
        # tail itself is budgeted, not just the summary input.
        max_input_tokens = await self._resolve_max_input_tokens(
            model_context,
            acting_user_email=session.user_email,
        )
        resolved_model, use_same_session_model = await self._resolve_compaction_target(
            model_context,
            acting_user_email=session.user_email,
        )
        count_tokens_fn = getattr(self.llm, "count_tokens", None)
        tail_token_budget = _preserved_tail_token_budget(entry, max_input_tokens)
        older_events, preserved_events = _split_events(
            entry.events,
            preserve_turns=self.preserve_turns,
            tail_token_budget=tail_token_budget,
            count_tokens_fn=count_tokens_fn,
            model=resolved_model,
        )
        if not _has_summarizable_history(older_events):
            return CompactionResult(compacted=False, method="noop")

        from cognis.core.agent_registry import SYSTEM_AGENTS

        compaction_agent = SYSTEM_AGENTS.get("system:compaction")
        compaction_prompt = (
            compaction_agent.system_prompt
            if compaction_agent and compaction_agent.system_prompt
            else "Summarize the conversation history concisely."
        )
        if trigger == "idle_checkpoint":
            compaction_prompt = f"{compaction_prompt}\n\n{IDLE_RENEWAL_COMPACTION_ADDENDUM}"
        elif long_lived_chat:
            compaction_prompt = f"{compaction_prompt}\n\n{LONG_LIVED_CHAT_COMPACTION_ADDENDUM}"

        history_input_tokens = _budget_without_previous_summary(
            max_input_tokens=max_input_tokens,
            previous_summary=entry.last_compaction_summary,
            count_tokens_fn=count_tokens_fn,
            model=resolved_model,
        )
        compaction_input = build_compaction_input(
            _summary_input_events(older_events, preserved_events),
            max_input_tokens=history_input_tokens,
            count_tokens_fn=count_tokens_fn,
            model=resolved_model,
        )
        compacted_text = compaction_input.text

        if entry.last_compaction_summary:
            compacted_text = _previous_summary_wrapper(
                entry.last_compaction_summary,
                compacted_text,
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
                    cognis_conversation_id=session.conversation_id,
                    cognis_agent_id=session.agent_id,
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
            exc_info = (
                (type(last_exc), last_exc, last_exc.__traceback__) if last_exc is not None else None
            )
            logger.warning(
                "compaction: LLM compaction failed after %d attempt(s)",
                self.llm_max_attempts,
                extra={
                    "extra_data": {
                        "session_id": session.session_id,
                        "error_type": type(last_exc).__name__ if last_exc is not None else None,
                    }
                },
                exc_info=exc_info,
            )
            if not self.fallback_enabled:
                return CompactionResult(compacted=False, method="llm_failed")
            return await self._mechanical_fallback(
                session,
                older_events=older_events,
                preserved_events=preserved_events,
                previous_summary=entry.last_compaction_summary,
                trigger=trigger,
                model_context=model_context,
                record=record,
            )

        summary = append_recoverable_tool_output_handles(summary, older_events)
        if not record:
            return self._build_compaction_result(
                session=session,
                summary=summary,
                formatted_input=compaction_input.text,
                older_events=older_events,
                preserved_tail_events=preserved_events,
                method="llm",
                resolved_model=resolved_model,
                compaction_seq=None,
            )
        COMPACTION_TOTAL.labels(trigger=trigger, method="llm").inc()
        return await self._record_compaction(
            session=session,
            summary=summary,
            formatted_input=compaction_input.text,
            older_events=older_events,
            preserved_tail_events=preserved_events,
            method="llm",
            resolved_model=resolved_model,
            compaction_id=model_context.compaction_id if model_context else None,
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
        await self.refresh_settings()
        if not self.fallback_enabled:
            return CompactionResult(compacted=False, method="llm_failed")

        await self.session_cache.refresh(session)
        entry = await self.session_cache.acquire_context_snapshot(session)

        max_input_tokens = await self._resolve_max_input_tokens(
            model_context,
            acting_user_email=session.user_email,
        )
        try:
            resolved_model = await self._resolve_compaction_model(
                model_context,
                acting_user_email=session.user_email,
            )
        except Exception:
            resolved_model = None
        count_tokens_fn = getattr(self.llm, "count_tokens", None)
        older_events, preserved_events = _split_events(
            entry.events,
            preserve_turns=self.preserve_turns,
            tail_token_budget=_preserved_tail_token_budget(entry, max_input_tokens),
            count_tokens_fn=count_tokens_fn,
            model=resolved_model,
        )
        if not _has_summarizable_history(older_events):
            return CompactionResult(compacted=False, method="noop")

        return await self._mechanical_fallback(
            session,
            older_events=older_events,
            preserved_events=preserved_events,
            previous_summary=entry.last_compaction_summary,
            trigger=trigger,
            model_context=model_context,
            record=True,
        )

    async def _mechanical_fallback(
        self,
        session: SessionModel,
        *,
        older_events: list[Any],
        preserved_events: list[Any],
        previous_summary: str | None = None,
        trigger: str,
        model_context: CompactionModelContext | None = None,
        record: bool = True,
    ) -> CompactionResult:
        summary_input_events = _summary_input_events(older_events, preserved_events)
        summary = build_sliding_window_summary(
            summary_input_events,
            previous_summary=previous_summary,
        )
        try:
            resolved_model = await self._resolve_compaction_model(
                model_context,
                acting_user_email=session.user_email,
            )
        except Exception:
            resolved_model = None
        formatted_input = format_events_for_compaction(summary_input_events)
        if not record:
            return self._build_compaction_result(
                session=session,
                summary=summary,
                formatted_input=formatted_input,
                older_events=older_events,
                preserved_tail_events=preserved_events,
                method="mechanical_sliding_window",
                resolved_model=resolved_model,
                compaction_seq=None,
            )
        COMPACTION_TOTAL.labels(trigger=trigger, method="mechanical_sliding_window").inc()
        COMPACTION_FALLBACK_USED.labels(trigger=trigger).inc()
        return await self._record_compaction(
            session=session,
            summary=summary,
            formatted_input=formatted_input,
            older_events=older_events,
            preserved_tail_events=preserved_events,
            method="mechanical_sliding_window",
            resolved_model=resolved_model,
            compaction_id=model_context.compaction_id if model_context else None,
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
        compaction_id: str | None,
    ) -> CompactionResult:
        result = self._build_compaction_result(
            session=session,
            summary=summary,
            formatted_input=formatted_input,
            older_events=older_events,
            preserved_tail_events=preserved_tail_events,
            method=method,
            resolved_model=resolved_model,
            compaction_seq=None,
        )
        tokens_before = result.tokens_before
        tokens_after = result.tokens_after
        turns_compacted = result.turns_compacted
        _, tail_pinned_seqs = _tail_layout(preserved_tail_events, older_events)
        compaction_event = SessionEvent(
            type="compaction_summary",
            data={
                "summary": summary,
                "compaction_id": compaction_id,
                "tokens_before": tokens_before,
                "tokens_after": tokens_after,
                "turns_compacted": turns_compacted,
                "events_compacted": result.events_compacted,
                "method": method,
                # The contiguous tail starts here; pinned admission anchors of
                # the active turn precede the summarized range and are listed
                # separately so deferred rotation can re-read both.
                "tail_start_seq": result.tail_start_seq,
                "tail_pinned_seqs": tail_pinned_seqs,
                "tail_event_count": len(preserved_tail_events),
                # This source-session checkpoint is a durable fallback if the
                # subsequent rotation fails. The rotated marker shares its
                # stable timeline ID, so a successful rotation replaces this
                # provisional card instead of creating a duplicate.
                "timeline_visible": True,
            },
        )
        # Retry is handled by the Intaris provider (exponential backoff).
        idempotency_key = f"{session.session_id}:compaction:{method}:{older_events[-1].seq}"
        guard = compaction_publication_guard.get()
        if guard is not None:
            # Last ownership check before the durable append: a controller that
            # lost its direct-turn fence or the compaction lease must not publish.
            await guard.check()
            idempotency_key = f"{idempotency_key}:{guard.token}"
        compaction_events = with_session_events_turn_id([compaction_event], None)
        with scoped_runtime_context(user_email=session.user_email, agent_id=session.agent_id):
            append_result = await self.guardrails.record_events(
                session_id=session.intaris_session_id or session.session_id,
                events=compaction_events,
                idempotency_key=idempotency_key,
            )
        if not append_result.ok:
            from cognis.core.canonical_history import CanonicalHistoryUnavailable

            raise CanonicalHistoryUnavailable("Compaction summary was not durably acknowledged")
        await self.session_cache.apply_compaction(
            session,
            summary=summary,
            compaction_seq=append_result.last_seq,
        )
        result.compaction_seq = append_result.last_seq
        return result

    def _build_compaction_result(
        self,
        *,
        session: SessionModel,
        summary: str,
        formatted_input: str,
        older_events: list[Any],
        preserved_tail_events: list[Any],
        method: str,
        resolved_model: str | None,
        compaction_seq: int | None,
    ) -> CompactionResult:
        del session
        count_tokens = getattr(self.llm, "count_tokens", None)
        if callable(count_tokens) and resolved_model:
            tokens_before = count_tokens(formatted_input, resolved_model)
            tail_text = format_events_for_compaction(preserved_tail_events)
            after_text = summary if not tail_text else f"{summary}\n\n{tail_text}"
            tokens_after = count_tokens(after_text, resolved_model)
        else:
            tokens_before = 0
            tokens_after = 0

        turns_compacted = sum(1 for event in older_events if event.type == "user_message")
        tail_start_seq, _ = _tail_layout(preserved_tail_events, older_events)
        return CompactionResult(
            compacted=True,
            method=method,
            summary=summary,
            compaction_seq=compaction_seq,
            turns_compacted=turns_compacted,
            events_compacted=len(older_events),
            tokens_before=tokens_before,
            tokens_after=tokens_after,
            preserved_tail_events=preserved_tail_events,
            tail_start_seq=tail_start_seq,
        )
