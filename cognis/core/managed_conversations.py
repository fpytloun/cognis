"""Shared helpers for managed agent conversations."""

from __future__ import annotations

import json
import uuid
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from typing import Any, cast

from cognis.core.chat_modes import CHAT_MODES, ChatMode
from cognis.logging import get_logger
from cognis.models.workflow import normalize_session_policy
from cognis.store import queries

logger = get_logger(__name__)


class ManagedConversationAdmissionConflict(RuntimeError):
    """Raised when a managed link already owns a different queued admission."""


def new_managed_turn_id() -> str:
    """Return a scheduler-compatible identity for a managed turn admission."""

    return f"turn_{uuid.uuid4().hex[:12]}"


def is_allowed_managed_conversation_target(agent_id: str | None) -> bool:
    """Return whether a non-blank target was supplied.

    This is syntactic validation only. Typed eligibility is resolved through
    ``OrchestrationTargetService`` immediately before work is created.
    """

    normalized = str(agent_id or "").strip()
    return bool(normalized)


def managed_conversation_target_error(agent_id: str | None) -> str:
    """Return the user-facing rejection message for invalid managed targets."""

    return (
        "Managed conversations require an active, accessible primary, non-system agent. "
        "Use delegate() for secondary or system specialists."
    )


def managed_link_owned_by_controller(
    link: Any,
    *,
    controller_agent_id: str,
    controller_conversation_id: str,
) -> bool:
    """Return whether a control-plane link belongs to the invoking controller."""

    return (
        getattr(link, "controller_agent_id", None) == controller_agent_id
        and getattr(link, "controller_conversation_id", None) == controller_conversation_id
    )


def managed_target_repeats_ancestry(target_agent_id: str, ancestry: list[Any]) -> bool:
    """Return whether a nested target repeats an agent in durable link ancestry."""

    agent_ids = {str(getattr(link, "target_agent_id", "")) for link in ancestry}
    if ancestry:
        agent_ids.add(str(getattr(ancestry[-1], "controller_agent_id", "")))
    return target_agent_id in agent_ids


def inherited_managed_session_policy(
    platform_data: dict[str, Any],
    fallback: dict[str, Any],
) -> dict[str, Any]:
    """Return the explicitly inherited policy, never a broader ambient fallback."""

    inherited = platform_data.get("managed_session_policy")
    if isinstance(inherited, dict):
        return normalize_session_policy(inherited)
    return normalize_session_policy(fallback)


@dataclass(frozen=True, slots=True)
class _SessionCandidate:
    session_id: str
    intaris_session_id: str


@dataclass(frozen=True, slots=True)
class ManagedConversationRetryMessage:
    """User message data needed to retry a managed conversation turn."""

    content: str
    one_shot_chat_mode: ChatMode | None = None
    turn_id: str | None = None


class ManagedConversationTurnObserver:
    """No-op observer that keeps managed admissions turn-distinct."""

    supports_mid_turn_absorb = False

    async def on_turn_complete(self, result: Any) -> None:
        return None

    async def on_turn_error(self, conversation_id: str, error: Any) -> None:
        return None

    def __getattr__(self, name: str) -> Any:
        if name.startswith("on_"):

            async def _noop(*args: Any, **kwargs: Any) -> None:
                return None

            return _noop
        raise AttributeError(name)


class ManagedConversationProgressObserver(ManagedConversationTurnObserver):
    """Forward bounded child-turn activity to a joined controller tool call."""

    def __init__(self, publish: Callable[..., Awaitable[None]]) -> None:
        self._publish = publish
        self._seen_tool_calls: set[str] = set()
        self._tool_call_count = 0
        self._todos: list[dict[str, Any]] = []

    async def _emit(self, *, last_tool: str | None = None) -> None:
        try:
            await self._publish(
                tool_call_count=self._tool_call_count,
                last_tool=last_tool,
                todos=self._todos,
            )
        except Exception:
            logger.exception("managed conversation progress publication failed")

    async def on_tool_call(
        self,
        _conversation_id: str,
        _session_id: str,
        call_id: str,
        tool_name: str,
        _arguments: dict[str, Any] | None,
        _turn_id: str | None,
        **_kwargs: Any,
    ) -> None:
        if call_id not in self._seen_tool_calls:
            self._seen_tool_calls.add(call_id)
            self._tool_call_count += 1
        await self._emit(last_tool=tool_name)

    async def on_tool_result(
        self,
        _conversation_id: str,
        _session_id: str,
        call_id: str,
        tool_name: str,
        result: str,
        _is_error: bool,
        _duration_ms: int | None,
        _evaluation: dict[str, Any] | None,
        **_kwargs: Any,
    ) -> None:
        if call_id not in self._seen_tool_calls:
            self._seen_tool_calls.add(call_id)
            self._tool_call_count += 1
        if tool_name in {"todo_write", "todo_list", "step_todo_write", "step_todo_list"}:
            try:
                payload = json.loads(result)
            except (TypeError, ValueError):
                payload = None
            todos = payload.get("todos") if isinstance(payload, dict) else None
            if isinstance(todos, list):
                self._todos = [
                    {
                        "content": str(todo.get("content") or "")[:280],
                        "status": str(todo.get("status") or "pending"),
                    }
                    for todo in todos
                    if isinstance(todo, dict) and str(todo.get("content") or "").strip()
                ]
        await self._emit(last_tool=tool_name)

    async def on_turn_complete(self, _result: Any) -> None:
        """Refresh the controller card after the child reaches a terminal state."""
        await self._emit()

    async def on_turn_error(self, _conversation_id: str, _error: Any) -> None:
        """Refresh the controller card after the child fails or is interrupted."""
        await self._emit()


@dataclass(frozen=True, slots=True)
class ManagedConversationProjection:
    """Scheduler-aware lifecycle projection for one managed conversation link."""

    conversation_state: str
    turn_state: str
    active_turn_id: str | None
    last_result_summary: str | None
    last_result_turn_id: str | None
    last_error: str | None
    completed_at: Any | None
    consistency_warnings: tuple[str, ...]

    @property
    def last_settlement_is_current(self) -> bool:
        if not self.last_result_turn_id:
            return False
        if self.active_turn_id:
            return self.active_turn_id == self.last_result_turn_id
        return self.turn_state in {"completed", "failed", "interrupted"}


def project_managed_conversation_state(
    link: Any,
    *,
    scheduler_active_turn_id: str | None = None,
) -> ManagedConversationProjection:
    """Project durable state without presenting live scheduler work as terminal.

    A queued managed admission intentionally owns a different turn ID than the
    scheduler turn ahead of it, so queued link identity remains authoritative.
    Durable inconsistencies remain visible to invariant and warning code
    through the original ORM row.
    """

    durable_conversation_state = str(getattr(link, "conversation_state", "") or "")
    durable_turn_state = str(getattr(link, "turn_state", "") or "")
    durable_active_turn_id = getattr(link, "active_turn_id", None)
    durable_completed_at = getattr(link, "completed_at", None)
    conversation_state = durable_conversation_state
    turn_state = durable_turn_state
    active_turn_id = durable_active_turn_id
    completed_at = durable_completed_at
    warnings: list[str] = []
    if durable_turn_state in {"queued", "running"} and durable_completed_at is not None:
        warnings.append("running+completed_at")
    if scheduler_active_turn_id and turn_state != "queued":
        conversation_state = "open"
        turn_state = "running"
        active_turn_id = scheduler_active_turn_id
        completed_at = None
        if (
            durable_conversation_state != conversation_state
            or durable_turn_state != turn_state
            or durable_active_turn_id != active_turn_id
            or durable_completed_at is not None
        ):
            warnings.append("scheduler-projection-overrode-durable-state")
            logger.warning(
                "managed conversation projection overrode inconsistent durable state",
                extra={
                    "extra_data": {
                        "link_id": getattr(link, "link_id", None),
                        "target_conversation_id": getattr(link, "target_conversation_id", None),
                        "durable_conversation_state": durable_conversation_state,
                        "durable_turn_state": durable_turn_state,
                        "durable_active_turn_id": durable_active_turn_id,
                        "scheduler_active_turn_id": scheduler_active_turn_id,
                        "durable_completed_at": (
                            str(durable_completed_at) if durable_completed_at else None
                        ),
                    }
                },
            )
    return ManagedConversationProjection(
        conversation_state=conversation_state,
        turn_state=turn_state,
        active_turn_id=active_turn_id,
        last_result_summary=getattr(link, "last_result_summary", None),
        last_result_turn_id=getattr(link, "last_result_turn_id", None),
        last_error=getattr(link, "last_error", None),
        completed_at=completed_at,
        consistency_warnings=tuple(dict.fromkeys(warnings)),
    )


def _event_type(event: Any) -> str | None:
    raw_type = event.get("type") if isinstance(event, dict) else getattr(event, "type", None)
    return raw_type if isinstance(raw_type, str) else None


def _event_data(event: Any) -> dict[str, Any] | None:
    raw_data = event.get("data") if isinstance(event, dict) else getattr(event, "data", None)
    return raw_data if isinstance(raw_data, dict) else None


def _one_shot_chat_mode_from_event_data(data: dict[str, Any]) -> ChatMode | None:
    if data.get("chat_mode_source") != "one_shot":
        return None
    raw_mode = data.get("chat_mode")
    if not isinstance(raw_mode, str):
        return None
    mode = raw_mode.strip().lower()
    if mode not in CHAT_MODES:
        return None
    return cast(ChatMode, mode)


def _last_user_message_from_events(events: list[Any]) -> ManagedConversationRetryMessage | None:
    for event in reversed(events):
        if _event_type(event) != "user_message":
            continue
        data = _event_data(event)
        if data is None:
            continue
        content = str(data.get("content") or "").strip()
        if content:
            return ManagedConversationRetryMessage(
                content=content,
                one_shot_chat_mode=_one_shot_chat_mode_from_event_data(data),
                turn_id=str(data.get("turn_id") or "").strip() or None,
            )
    return None


def _cached_events_for_session(session_cache: Any, session_id: str) -> list[Any]:
    getter = getattr(session_cache, "get_events_since_compaction", None)
    if not callable(getter):
        return []
    try:
        typed_events = getter(session_id, types=["user_message"])
    except TypeError:
        typed_events = getter(session_id)
    if not isinstance(typed_events, Iterable):
        return []
    return list(typed_events)


async def _candidate_sessions(
    session_factory: Callable[[], Any],
    link: Any,
) -> list[_SessionCandidate]:
    target_session_id = getattr(link, "target_session_id", None)
    target_conversation_id = getattr(link, "target_conversation_id", None)
    candidates: list[_SessionCandidate] = []
    seen: set[str] = set()

    def add(session_id: str | None, intaris_session_id: str | None = None) -> None:
        if not session_id or session_id in seen:
            return
        seen.add(session_id)
        candidates.append(
            _SessionCandidate(
                session_id=session_id,
                intaris_session_id=intaris_session_id or session_id,
            )
        )

    try:
        async with session_factory() as db_session:
            if not callable(getattr(db_session, "execute", None)):
                add(target_session_id)
                return candidates

            target_row = (
                await queries.get_session_row(db_session, target_session_id)
                if target_session_id
                else None
            )
            rows = (
                await queries.list_conversation_sessions(
                    db_session,
                    target_conversation_id,
                    root_only=True,
                    order="desc",
                    limit=50,
                )
                if target_conversation_id
                else []
            )
            if target_row is not None and target_row.session_id not in {
                row.session_id for row in rows
            }:
                rows.insert(0, target_row)
            for row in rows:
                add(row.session_id, row.intaris_session_id or row.session_id)
            if target_row is None:
                add(target_session_id)
    except Exception:
        logger.warning(
            "managed conversation: failed to list retry session candidates",
            extra={
                "extra_data": {
                    "link_id": getattr(link, "link_id", None),
                    "target_conversation_id": target_conversation_id,
                    "target_session_id": target_session_id,
                }
            },
            exc_info=True,
        )
        add(target_session_id)

    return candidates


async def last_managed_conversation_user_message_for_retry(
    *,
    session_cache: Any,
    guardrails: Any,
    session_factory: Callable[[], Any],
    link: Any,
) -> ManagedConversationRetryMessage | None:
    """Return the newest durable target user message available for retry."""

    read_events: Callable[..., Awaitable[Any]] | None = getattr(guardrails, "read_events", None)
    for candidate in await _candidate_sessions(session_factory, link):
        cached_message = _last_user_message_from_events(
            _cached_events_for_session(session_cache, candidate.session_id)
        )
        if cached_message:
            return cached_message
        if not callable(read_events):
            continue
        try:
            events: list[Any] = []
            after_seq = 0
            while True:
                result = await read_events(
                    session_id=candidate.intaris_session_id,
                    after_seq=after_seq,
                    limit=500,
                    types=["user_message"],
                    allow_missing_stream=True,
                )
                events.extend(list(getattr(result, "events", []) or []))
                last_seq = int(getattr(result, "last_seq", 0) or 0)
                if not getattr(result, "has_more", False) or last_seq <= after_seq:
                    break
                after_seq = last_seq
        except Exception:
            logger.warning(
                "managed conversation: failed to read retry user messages",
                extra={
                    "extra_data": {
                        "link_id": getattr(link, "link_id", None),
                        "session_id": candidate.session_id,
                        "intaris_session_id": candidate.intaris_session_id,
                    }
                },
                exc_info=True,
            )
            continue
        durable_message = _last_user_message_from_events(events)
        if durable_message:
            return durable_message
    return None
