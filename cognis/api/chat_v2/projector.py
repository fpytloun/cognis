"""Pure Chat v2 timeline projection from normalized session events."""

from __future__ import annotations

import json
from collections.abc import Iterable
from typing import Any, Literal, cast

from pydantic import Field

from cognis.api.chat_v2.cycles import cycle_states_from_items
from cognis.api.chat_v2.item_keys import (
    KIND_RANK,
    assistant_message_item_id,
    encode_timeline_sort_key,
    thinking_item_id,
)
from cognis.api.chat_v2.normalizer import NormalizedChatEvent
from cognis.api.chat_v2.schemas import (
    ArtifactTimelineItem,
    AssistantDeliverableTimelineItem,
    AuthChallengeTimelineItem,
    ChatMode,
    CompactionTimelineItem,
    CredentialRequestTimelineItem,
    DelegationTimelineItem,
    ErrorTimelineItem,
    FileDiffRef,
    FileDiffTimelineItem,
    ManagedConversationTimelineItem,
    MessageTimelineItem,
    NoticeTimelineItem,
    QuestionSetTimelineItem,
    QuestionSpec,
    SourceRef,
    StrictModel,
    TaskTimelineItem,
    ThinkingBlock,
    ThinkingTimelineItem,
    TimelineItem,
    TimelineItemStatus,
    TimelineWindow,
    TodoStateTimelineItem,
    ToolCallTimelineItem,
)

ThinkingStatus = Literal["running", "complete", "failed"]
QuestionStatus = Literal["waiting", "complete", "cancelled"]
RequestStatus = Literal["waiting", "complete", "cancelled", "failed"]
HIDDEN_EVENT = object()


class ProjectionWarning(StrictModel):
    """Non-fatal projection diagnostic."""

    code: str
    message: str
    source_ref: SourceRef | None = None


class TimelineProjection(StrictModel):
    """Pure timeline projection result."""

    timeline: TimelineWindow
    warnings: list[ProjectionWarning] = Field(default_factory=list)


def project_timeline(events: Iterable[NormalizedChatEvent]) -> TimelineProjection:
    """Project normalized events into a canonical Chat v2 timeline window."""

    normalized_events = _fill_missing_assistant_turn_cycles(list(events))
    items_by_id: dict[str, TimelineItem] = {}
    evaluations_by_call_id: dict[str, tuple[dict[str, Any], SourceRef]] = {}
    delegation_folds = _DelegationFolds()
    warnings: list[ProjectionWarning] = []

    for event in normalized_events:
        item = _project_event(event, items_by_id, evaluations_by_call_id, delegation_folds)
        if item is HIDDEN_EVENT:
            continue
        if item is None:
            warnings.append(
                ProjectionWarning(
                    code="unsupported_event",
                    message=f"Unsupported chat event type: {event.source_ref.event_type}",
                    source_ref=event.source_ref,
                )
            )
            item = _unknown_notice(event)
        _upsert_item(items_by_id, item)

    items = sorted(items_by_id.values(), key=lambda item: item.sort_key)
    return TimelineProjection(
        timeline=TimelineWindow(items=items, cycle_states=cycle_states_from_items(items)),
        warnings=warnings,
    )


_CYCLE_BEARING_KINDS = {"tool_call", "tool_result", "thinking"}
_TURN_BOUNDARY_KINDS = {"user_message", "assistant_message"}


def _infer_forward_cycle(
    events: list[NormalizedChatEvent],
    start_index: int,
    turn_id: str,
) -> int | None:
    """Return the first following same-turn tool/thinking cycle before a boundary."""

    for candidate in events[start_index:]:
        candidate_turn_id = candidate.data.get("turn_id")
        if candidate_turn_id != turn_id:
            if candidate.kind in _TURN_BOUNDARY_KINDS:
                break
            continue
        if candidate.kind in _TURN_BOUNDARY_KINDS:
            break
        if candidate.kind in _CYCLE_BEARING_KINDS and isinstance(candidate.turn_cycle_index, int):
            return candidate.turn_cycle_index
    return None


def _fill_missing_assistant_turn_cycles(
    events: list[NormalizedChatEvent],
) -> list[NormalizedChatEvent]:
    """Repair events that predate (or were persisted without) cycle metadata.

    The UI relies on ``turn_cycle_index`` to fold assistant text into the
    matching tool activity. Two historical gaps exist:

    1. Assistant message events lacked the field before tool events did.
    2. The primary regular-tool persistence path recorded ``tool_call`` events
       without the field at all (its paired ``tool_result`` carries it, and the
       ``_tool_result_item`` projector now backfills from it — but the very
       first histories predate any tool stamping).

    This pass fills a missing cycle on assistant/tool/thinking events from the
    nearest same-turn neighbor that carries one, scanning forward first (the
    first following same-turn cycle-bearing event before the next
    assistant/user boundary) and then backward (the last preceding same-turn
    cycle-bearing event within the current turn). Events that cannot be
    resolved remain unannotated and therefore render standalone.
    """

    needs_repair = any(
        event.turn_cycle_index is None
        and (event.kind == "assistant_message" or event.kind in _CYCLE_BEARING_KINDS)
        and isinstance(event.data.get("turn_id"), str)
        for event in events
    )
    if not needs_repair:
        return events

    # Backward pass reference: last seen cycle per turn, reset at turn boundary.
    last_cycle_by_turn: dict[str, int] = {}

    repaired: list[NormalizedChatEvent] = []
    for index, event in enumerate(events):
        turn_id = event.data.get("turn_id")
        if isinstance(turn_id, str) and isinstance(event.turn_cycle_index, int):
            last_cycle_by_turn[turn_id] = event.turn_cycle_index

        repairable = (
            event.turn_cycle_index is None
            and (event.kind == "assistant_message" or event.kind in _CYCLE_BEARING_KINDS)
            and isinstance(turn_id, str)
        )
        if not repairable:
            repaired.append(event)
            continue

        assert isinstance(turn_id, str)
        inferred_cycle = _infer_forward_cycle(events, index + 1, turn_id)
        # Backward fallback applies ONLY to tool/thinking events, which always
        # belong to an in-progress cycle. Assistant messages stay forward-only
        # (their existing, proven behavior): a trailing assistant message with
        # no following tool activity is the turn's final answer and must remain
        # standalone rather than inheriting the prior cycle and folding.
        if inferred_cycle is None and event.kind in _CYCLE_BEARING_KINDS:
            inferred_cycle = last_cycle_by_turn.get(turn_id)

        if inferred_cycle is None:
            repaired.append(event)
            continue

        data = {**event.data, "turn_cycle_index": inferred_cycle}
        repaired.append(event.model_copy(update={"data": data, "turn_cycle_index": inferred_cycle}))
        last_cycle_by_turn[turn_id] = inferred_cycle
    return repaired


def _project_event(
    event: NormalizedChatEvent,
    items_by_id: dict[str, TimelineItem],
    evaluations_by_call_id: dict[str, tuple[dict[str, Any], SourceRef]],
    delegation_folds: _DelegationFolds,
) -> TimelineItem | object | None:
    if event.kind == "user_message":
        return _message_item(event, role="user")
    if event.kind == "assistant_message":
        return _message_item(event, role="assistant")
    if event.kind == "system_message":
        return _message_item(event, role="system")
    if event.kind == "thinking":
        return _thinking_item(event)
    if event.kind == "tool_call":
        return _tool_call_item(event, items_by_id, evaluations_by_call_id, delegation_folds)
    if event.kind == "tool_result":
        return _tool_result_item(event, items_by_id, evaluations_by_call_id, delegation_folds)
    if event.kind == "evaluation":
        if _evaluation_call_id(event) is None:
            # Step evaluation feedback carries no tool anchor. Render it as a
            # notice instead of silently swallowing it — the legacy replay
            # showed a "Step Evaluation (attempt N)" entry for these.
            if str(event.data.get("event") or "") == "evaluation_feedback":
                return _evaluation_feedback_notice(event)
            return HIDDEN_EVENT
        _record_tool_evaluation(event, items_by_id, evaluations_by_call_id)
        return HIDDEN_EVENT
    if event.kind == "delegation":
        # Fold synchronous delegate/fork delegations onto their originating
        # tool call so they render as a single rich, auto-expanding tool call.
        # Asynchronous task/workflow delegations (and any delegation without a
        # correlated delegate tool call) keep their standalone card.
        folded = _record_tool_delegation(event, items_by_id, delegation_folds)
        if folded:
            return HIDDEN_EVENT
        return _delegation_item(event)
    if event.kind == "managed_conversation":
        return _managed_conversation_item(event)
    if event.kind == "task":
        return _task_item(event)
    if event.kind == "question_set":
        return _question_set_item(event)
    if event.kind == "auth_challenge":
        return _auth_challenge_item(event)
    if event.kind == "credential_request":
        return _credential_request_item(event)
    if event.kind == "todo_state":
        return _todo_state_item(event)
    if event.kind == "artifact":
        return _artifact_item(event)
    if event.kind == "assistant_deliverable":
        return _assistant_deliverable_item(event)
    if event.kind == "file_diff":
        return _file_diff_item(event)
    if event.kind == "notice":
        return _notice_item(event)
    if event.kind == "compaction":
        return _compaction_item(event)
    if event.kind == "error":
        return _error_item(event)
    return None


def _message_item(event: NormalizedChatEvent, *, role: str) -> MessageTimelineItem:
    data = event.data
    message_id = _message_id(event, prefix=role)
    item_id = _message_item_id(event, role=role, message_id=message_id)
    content = data.get("content")
    if role == "system" and not content:
        content = data.get("message") or data.get("text")
    return MessageTimelineItem(
        id=item_id,
        sort_key=_sort_key(event),
        source_refs=[event.source_ref],
        created_at=event.timestamp,
        updated_at=event.timestamp,
        role=role,  # type: ignore[arg-type]
        content=str(content or ""),
        message_id=message_id,
        client_message_id=_str_or_none(data.get("client_message_id")),
        client_txn_id=_str_or_none(data.get("client_txn_id")),
        turn_id=_str_or_none(data.get("turn_id")),
        assistant_phase_index=event.assistant_phase_index,
        turn_cycle_index=event.turn_cycle_index,
        notice_id=_str_or_none(data.get("notice_id")),
        notice_kind=_str_or_none(data.get("kind")),
        notice_scope=_str_or_none(data.get("scope")),
        reason_class=_str_or_none(data.get("reason_class")),
        provider_id=_str_or_none(data.get("provider_id")),
        model=_str_or_none(data.get("model")),
        retry_after_seconds=_nonnegative_float(data.get("retry_after_seconds")),
        provider_retry_after_seconds=_nonnegative_float(data.get("provider_retry_after_seconds")),
        retry_at=_str_or_none(data.get("retry_at")),
        attempt=_nonnegative_int(data.get("attempt")),
        max_attempts=_nonnegative_int(data.get("max_attempts")),
        attempts=_nonnegative_int(data.get("attempts")),
        attempts_per_cycle=_nonnegative_int(data.get("attempts_per_cycle")),
        continuation_attempts=_nonnegative_int(data.get("continuation_attempts")),
        recoverable=data.get("recoverable") if isinstance(data.get("recoverable"), bool) else None,
        follow_up_conversation_id=_str_or_none(data.get("follow_up_conversation_id")),
        follow_up_session_id=_str_or_none(data.get("follow_up_session_id")),
        partial=bool(data.get("partial", False)),
        attachments=list(data.get("attachments") or []),
        chat_mode=_chat_mode(data),
        chat_mode_source=_str_or_none(data.get("chat_mode_source")),
    )


def _thinking_item(
    event: NormalizedChatEvent,
) -> ThinkingTimelineItem:
    data = event.data
    block_id = _str_or_none(data.get("block_id")) or f"seq-{event.source_ref.seq}"
    message_id = _str_or_none(data.get("message_id"))
    block = ThinkingBlock(
        id=block_id,
        title=_str_or_none(data.get("title")),
        content=str(data.get("content") or ""),
        status=_thinking_status(data),
        started_at=_str_or_none(data.get("started_at")),
        completed_at=_str_or_none(data.get("completed_at")),
        duration_ms=_int_or_none(data.get("duration_ms")),
    )
    return ThinkingTimelineItem(
        id=_thinking_item_id(event, message_id=message_id, block_id=block_id),
        sort_key=_sort_key(event),
        source_refs=[event.source_ref],
        created_at=event.timestamp,
        updated_at=event.timestamp,
        status="complete",
        message_id=message_id,
        turn_id=_str_or_none(data.get("turn_id")),
        assistant_phase_index=event.assistant_phase_index,
        turn_cycle_index=event.turn_cycle_index,
        blocks=[block],
        active_title=_str_or_none(data.get("active_title") or data.get("title")),
    )


def _tool_call_item(
    event: NormalizedChatEvent,
    items_by_id: dict[str, TimelineItem],
    evaluations_by_call_id: dict[str, tuple[dict[str, Any], SourceRef]],
    delegation_folds: _DelegationFolds,
) -> ToolCallTimelineItem:
    data = event.data
    call_id = _call_id(event)
    item_id = f"tool:{call_id}"
    existing = items_by_id.get(item_id)
    sidecar = evaluations_by_call_id.get(call_id)
    _suppress_standalone_delegation_card(call_id, items_by_id, delegation_folds)
    return ToolCallTimelineItem(
        id=item_id,
        sort_key=_sort_key(event),
        source_refs=_merged_source_refs(
            existing, event.source_ref, sidecar[1] if sidecar else None
        ),
        created_at=existing.created_at
        if isinstance(existing, ToolCallTimelineItem)
        else event.timestamp,
        updated_at=event.timestamp,
        status=_status(data, default="running"),
        call_id=call_id,
        tool_name=str(data.get("name") or data.get("tool_name") or "tool"),
        display_name=_str_or_none(data.get("visible_name") or data.get("display_name")),
        turn_id=_str_or_none(data.get("turn_id")),
        assistant_phase_index=event.assistant_phase_index,
        turn_cycle_index=event.turn_cycle_index,
        arguments=_structured_arguments(data.get("arguments")),
        arguments_preview=_arguments_preview(data.get("arguments")),
        evaluation=sidecar[0]
        if sidecar
        else existing.evaluation
        if isinstance(existing, ToolCallTimelineItem)
        else None,
        # A delegation event may have been projected before its tool_call in
        # rare orderings; attach it if already recorded.
        delegation=delegation_folds.for_call(call_id)
        or (existing.delegation if isinstance(existing, ToolCallTimelineItem) else None),
    )


def _tool_result_item(
    event: NormalizedChatEvent,
    items_by_id: dict[str, TimelineItem],
    evaluations_by_call_id: dict[str, tuple[dict[str, Any], SourceRef]],
    delegation_folds: _DelegationFolds,
) -> ToolCallTimelineItem:
    data = event.data
    call_id = _call_id(event)
    item_id = f"tool:{call_id}"
    existing = items_by_id.get(item_id)
    existing_tool = existing if isinstance(existing, ToolCallTimelineItem) else None
    sidecar = evaluations_by_call_id.get(call_id)
    _suppress_standalone_delegation_card(call_id, items_by_id, delegation_folds)
    is_error = bool(data.get("is_error", False))
    status: TimelineItemStatus = "failed" if is_error else _status(data, default="complete")
    return ToolCallTimelineItem(
        id=item_id,
        sort_key=existing_tool.sort_key if existing_tool is not None else _sort_key(event),
        source_refs=_merged_source_refs(
            existing, event.source_ref, sidecar[1] if sidecar else None
        ),
        created_at=existing_tool.created_at if existing_tool is not None else event.timestamp,
        updated_at=event.timestamp,
        status=status,
        call_id=call_id,
        tool_name=(
            existing_tool.tool_name
            if existing_tool is not None
            else str(data.get("name") or data.get("tool_name") or "tool")
        ),
        display_name=(
            existing_tool.display_name
            if existing_tool is not None
            else _str_or_none(data.get("visible_name") or data.get("display_name"))
        ),
        turn_id=(
            existing_tool.turn_id
            if existing_tool is not None and existing_tool.turn_id is not None
            else _str_or_none(data.get("turn_id"))
        ),
        # Coalesce phase/cycle: prefer the tool_call item's value when present,
        # but fall back to the tool_result event's value when the tool_call
        # event landed without a stamp. The primary regular-tool persistence
        # path historically recorded tool_call events with null cycle/phase
        # while the paired tool_result event carries them, so preferring the
        # existing null here would permanently strip the grouping key. Falling
        # back retroactively repairs those conversations without a data migration.
        assistant_phase_index=(
            existing_tool.assistant_phase_index
            if existing_tool is not None and existing_tool.assistant_phase_index is not None
            else event.assistant_phase_index
        ),
        turn_cycle_index=(
            existing_tool.turn_cycle_index
            if existing_tool is not None and existing_tool.turn_cycle_index is not None
            else event.turn_cycle_index
        ),
        arguments=existing_tool.arguments if existing_tool is not None else None,
        arguments_preview=existing_tool.arguments_preview if existing_tool is not None else None,
        result_preview=_preview(data.get("result")),
        streamed_output=_str_or_none(data.get("streamed_output")),
        is_error=is_error,
        duration_ms=_int_or_none(data.get("duration_ms")),
        attachments=list(data.get("attachments") or []),
        file_diffs=_file_diffs(data.get("file_diffs")),
        output_size=_int_or_none(data.get("output_size")),
        truncated=bool(data.get("truncated", False)),
        has_full_output=bool(data.get("has_full_output", False)),
        recovery_call_id=_str_or_none(data.get("recovery_call_id")),
        tool_output_artifact_id=_str_or_none(data.get("tool_output_artifact_id")),
        evaluation=(
            _evaluation_payload(data)
            if isinstance(data.get("evaluation"), dict)
            else sidecar[0]
            if sidecar
            else existing_tool.evaluation
            if existing_tool is not None
            else None
        ),
        delegation=delegation_folds.for_call(call_id)
        or (existing_tool.delegation if existing_tool is not None else None),
    )


_FOLDED_DELEGATION_TOOL_NAMES = frozenset(
    {"delegate", "retry_subsession", "follow_up_subsession", "fork_subsession", "fork"}
)


class _DelegationFolds:
    """Tracks delegated sub-session lifecycle payloads folded onto tool calls.

    Correlation is by the originating tool ``call_id`` (present on the
    ``started`` event) OR the ``child_session_id`` (the only key present on
    ``completed``/``cancelled``/``failed`` events). Only synchronous delegated
    sub-session tools fold; ``task``/``workflow`` (and any delegation with no
    correlated delegated sub-session tool call) keep a standalone card.
    """

    def __init__(self) -> None:
        self._by_call_id: dict[str, dict[str, Any]] = {}
        self._call_id_by_child: dict[str, str] = {}

    def for_call(self, call_id: str) -> dict[str, Any] | None:
        return self._by_call_id.get(call_id)

    def record(
        self, *, call_id: str, child_session_id: str | None, payload: dict[str, Any]
    ) -> None:
        self._by_call_id[call_id] = payload
        if child_session_id:
            self._call_id_by_child[child_session_id] = call_id

    def resolve_call_id(self, *, call_id: str | None, child_session_id: str | None) -> str | None:
        if call_id and (call_id in self._by_call_id or child_session_id is None):
            return call_id
        if child_session_id and child_session_id in self._call_id_by_child:
            return self._call_id_by_child[child_session_id]
        return call_id


def _suppress_standalone_delegation_card(
    call_id: str,
    items_by_id: dict[str, TimelineItem],
    delegation_folds: _DelegationFolds,
) -> None:
    """Remove the standalone delegation card once its tool call materializes.

    A delegation event projected BEFORE its tool_call (separate seq batches,
    or a sync window boundary bisecting them) emits a standalone
    ``delegation:{child}`` card. When the delegate tool call later appears in
    the same projection, the delegation payload folds onto the tool item — the
    standalone card would otherwise remain as a duplicate representation.
    """
    payload = delegation_folds.for_call(call_id)
    if not payload:
        return
    child_session_id = payload.get("child_session_id")
    if isinstance(child_session_id, str) and child_session_id:
        items_by_id.pop(f"delegation:{child_session_id}", None)


def _record_tool_delegation(
    event: NormalizedChatEvent,
    items_by_id: dict[str, TimelineItem],
    delegation_folds: _DelegationFolds,
) -> bool:
    """Record a delegated sub-session payload and fold it onto its tool call.

    Returns True when the delegation was folded onto an existing delegated
    sub-session tool call (so the standalone card is suppressed), False when
    it is an async task/workflow delegation or has no correlated tool call and
    a standalone delegation card should be emitted.
    """
    data = event.data
    mode = _str_or_none(data.get("mode"))
    # Only synchronous sub-session delegations render as folded tool calls.
    # Async task/workflow delegations keep their standalone task card.
    if mode is not None and mode not in {"delegate", "fork"}:
        return False
    child_session_id = _str_or_none(data.get("child_session_id") or data.get("session_id"))
    call_id = delegation_folds.resolve_call_id(
        call_id=_str_or_none(data.get("call_id")), child_session_id=child_session_id
    )
    if not call_id:
        return False
    item_id = f"tool:{call_id}"
    existing = items_by_id.get(item_id)
    # Fold only when the correlated tool call is a delegated sub-session tool.
    if not (
        isinstance(existing, ToolCallTimelineItem)
        and existing.tool_name in _FOLDED_DELEGATION_TOOL_NAMES
    ):
        # The tool_call may still arrive later in this window (delegation
        # lifecycle events are recorded in separate seq batches). Record the
        # payload so _tool_call_item can attach it and suppress the standalone
        # card that is emitted now.
        if mode in {"delegate", "fork"}:
            delegation_folds.record(
                call_id=call_id,
                child_session_id=child_session_id,
                payload=_delegation_payload(event),
            )
        return False
    payload = _delegation_payload(event)
    delegation_folds.record(call_id=call_id, child_session_id=child_session_id, payload=payload)
    items_by_id[item_id] = existing.model_copy(
        update={
            "source_refs": _merged_source_refs(existing, event.source_ref, None),
            "updated_at": event.timestamp,
            "delegation": payload,
        }
    )
    return True


def _delegation_payload(event: NormalizedChatEvent) -> dict[str, Any]:
    """Structured delegation details folded onto a delegate tool call."""
    data = event.data
    child_session_id = _str_or_none(data.get("child_session_id") or data.get("session_id"))
    return {
        "child_session_id": child_session_id,
        "status": data.get("status"),
        "turn_id": _str_or_none(data.get("turn_id")),
        "assistant_phase_index": event.assistant_phase_index,
        "turn_cycle_index": event.turn_cycle_index,
        "agent_id": _str_or_none(data.get("agent_id") or data.get("used_agent_id")),
        "used_agent_id": _str_or_none(data.get("used_agent_id")),
        "title": _str_or_none(data.get("title") or data.get("task_title") or data.get("label")),
        "summary": _str_or_none(data.get("summary") or data.get("task")),
        "started_at": _str_or_none(data.get("started_at")),
        "duration_ms": _int_or_none(data.get("duration_ms")),
        "result_summary": _str_or_none(data.get("result_summary")),
        "result_content": _str_or_none(data.get("result_content")),
        "result_source": _str_or_none(data.get("result_source")),
        "result_truncated": bool(data.get("result_truncated"))
        if data.get("result_truncated") is not None
        else None,
        "result_anchors": data.get("result_anchors"),
        "todos": _todo_list(data.get("todos")),
        "tool_call_count": _int_or_none(data.get("tool_call_count")),
        "max_tool_calls": _int_or_none(data.get("max_tool_calls")),
        "last_tool": _str_or_none(data.get("last_tool")),
        "error": _str_or_none(data.get("error")),
        "error_code": _str_or_none(data.get("error_code")),
    }


def _record_tool_evaluation(
    event: NormalizedChatEvent,
    items_by_id: dict[str, TimelineItem],
    evaluations_by_call_id: dict[str, tuple[dict[str, Any], SourceRef]],
) -> None:
    data = event.data
    call_id = _evaluation_call_id(event)
    if call_id is None:
        return
    evaluation = _evaluation_payload(data)
    evaluations_by_call_id[call_id] = (evaluation, event.source_ref)
    item_id = f"tool:{call_id}"
    existing = items_by_id.get(item_id)
    if isinstance(existing, ToolCallTimelineItem):
        items_by_id[item_id] = existing.model_copy(
            update={
                "source_refs": _merged_source_refs(existing, event.source_ref),
                "updated_at": event.timestamp,
                "evaluation": evaluation,
            }
        )


def _delegation_item(event: NormalizedChatEvent) -> DelegationTimelineItem:
    data = event.data
    child_session_id = str(
        data.get("child_session_id") or data.get("session_id") or _fallback_id(event)
    )
    return DelegationTimelineItem(
        id=f"delegation:{child_session_id}",
        sort_key=_sort_key(event),
        source_refs=[event.source_ref],
        created_at=event.timestamp,
        updated_at=event.timestamp,
        status=_status(data, default="running"),
        child_session_id=child_session_id,
        turn_id=_str_or_none(data.get("turn_id")),
        assistant_phase_index=event.assistant_phase_index,
        turn_cycle_index=event.turn_cycle_index,
        agent_id=_str_or_none(data.get("agent_id") or data.get("used_agent_id")),
        used_agent_id=_str_or_none(data.get("used_agent_id")),
        title=_str_or_none(data.get("title") or data.get("task_title") or data.get("label")),
        summary=_str_or_none(data.get("summary") or data.get("task")),
        result_summary=_str_or_none(data.get("result_summary")),
        result_anchors=_dict_str_str_or_none(data.get("result_anchors")),
        todos=_todo_list(data.get("todos")),
        tool_call_count=_int_or_none(data.get("tool_call_count")),
        max_tool_calls=_int_or_none(data.get("max_tool_calls")),
        last_tool=_str_or_none(data.get("last_tool")),
    )


def _managed_conversation_item(event: NormalizedChatEvent) -> ManagedConversationTimelineItem:
    data = event.data
    managed_conversation_id = str(
        data.get("managed_conversation_id") or data.get("conversation_id") or _fallback_id(event)
    )
    return ManagedConversationTimelineItem(
        id=f"managed-conversation:{managed_conversation_id}",
        sort_key=_sort_key(event),
        source_refs=[event.source_ref],
        created_at=event.timestamp,
        updated_at=event.timestamp,
        status=_status(data, default="running"),
        managed_conversation_id=managed_conversation_id,
        agent_id=str(data.get("agent_id") or "unknown"),
        title=_str_or_none(data.get("title")),
        result_summary=_str_or_none(data.get("result_summary")),
    )


def _task_item(event: NormalizedChatEvent) -> TaskTimelineItem:
    data = event.data
    task_id = str(data.get("task_id") or data.get("id") or _fallback_id(event))
    return TaskTimelineItem(
        id=f"task:{task_id}",
        sort_key=_sort_key(event),
        source_refs=[event.source_ref],
        created_at=event.timestamp,
        updated_at=event.timestamp,
        status=_status(data, default=_task_status(event)),
        task_id=task_id,
        title=str(data.get("title") or data.get("task_title") or "Task"),
        workflow_id=_str_or_none(data.get("workflow_id")),
        workflow_step=_str_or_none(data.get("workflow_step") or data.get("step_name")),
        result_summary=_str_or_none(data.get("result_summary")),
        deliverable_ids=[str(value) for value in data.get("deliverable_ids") or []],
    )


def _question_set_item(event: NormalizedChatEvent) -> QuestionSetTimelineItem:
    data = event.data
    request_id = str(data.get("request_id") or data.get("notification_id") or _fallback_id(event))
    return QuestionSetTimelineItem(
        id=f"question-set:{request_id}",
        sort_key=_sort_key(event),
        source_refs=[event.source_ref],
        created_at=event.timestamp,
        updated_at=event.timestamp,
        request_id=request_id,
        title=_str_or_none(data.get("title") or data.get("label")),
        questions=[QuestionSpec.model_validate(item) for item in data.get("questions") or []],
        status=_question_status(data),
    )


def _auth_challenge_item(event: NormalizedChatEvent) -> AuthChallengeTimelineItem:
    data = event.data
    challenge_id = str(data.get("challenge_id") or data.get("request_id") or _fallback_id(event))
    return AuthChallengeTimelineItem(
        id=f"auth-challenge:{challenge_id}",
        sort_key=_sort_key(event),
        source_refs=[event.source_ref],
        created_at=event.timestamp,
        updated_at=event.timestamp,
        challenge_id=challenge_id,
        challenge_kind=str(data.get("kind") or data.get("challenge_kind") or "auth"),
        label=str(data.get("label") or "Authentication required"),
        message=str(data.get("message") or ""),
        metadata=dict(data.get("metadata") or {}),
        required_fields=[str(value) for value in data.get("required_fields") or []],
        status=_request_status(data),
    )


def _credential_request_item(event: NormalizedChatEvent) -> CredentialRequestTimelineItem:
    data = event.data
    credential_request_id = str(
        data.get("credential_request_id") or data.get("request_id") or _fallback_id(event)
    )
    return CredentialRequestTimelineItem(
        id=f"credential-request:{credential_request_id}",
        sort_key=_sort_key(event),
        source_refs=[event.source_ref],
        created_at=event.timestamp,
        updated_at=event.timestamp,
        credential_request_id=credential_request_id,
        credential_id=str(data.get("credential_id") or credential_request_id),
        credential_kind=str(data.get("kind") or data.get("credential_kind") or "text"),
        label=str(data.get("label") or "Credential required"),
        description=_str_or_none(data.get("description")),
        required_fields=[str(value) for value in data.get("required_fields") or []],
        status=_request_status(data),
    )


def _todo_state_item(event: NormalizedChatEvent) -> TodoStateTimelineItem:
    return TodoStateTimelineItem(
        id=f"todo-state:{event.source_ref.session_id}:{event.source_ref.seq}",
        sort_key=_sort_key(event),
        source_refs=[event.source_ref],
        created_at=event.timestamp,
        updated_at=event.timestamp,
        todos=list(event.data.get("todos") or []),
    )


def _artifact_item(event: NormalizedChatEvent) -> ArtifactTimelineItem:
    data = event.data
    artifact_id = str(data.get("artifact_id") or data.get("id") or _fallback_id(event))
    return ArtifactTimelineItem(
        id=f"artifact:{artifact_id}",
        sort_key=_sort_key(event),
        source_refs=[event.source_ref],
        created_at=event.timestamp,
        updated_at=event.timestamp,
        artifact_id=artifact_id,
        filename=str(data.get("filename") or artifact_id),
        mime_type=_str_or_none(data.get("mime_type")),
        size_bytes=_int_or_none(data.get("size_bytes")),
        title=_str_or_none(data.get("title")),
    )


def _assistant_deliverable_item(event: NormalizedChatEvent) -> AssistantDeliverableTimelineItem:
    data = event.data
    deliverable_id = str(data.get("deliverable_id") or data.get("id") or _fallback_id(event))
    return AssistantDeliverableTimelineItem(
        id=f"assistant-deliverable:{deliverable_id}",
        sort_key=_sort_key(event),
        source_refs=[event.source_ref],
        created_at=event.timestamp,
        updated_at=event.timestamp,
        status="complete",
        deliverable_id=deliverable_id,
        format=str(data.get("format") or "markdown"),
        title=_str_or_none(data.get("title")),
        content=_str_or_none(data.get("content")),
        render_metadata=data.get("render_metadata")
        if isinstance(data.get("render_metadata"), dict)
        else None,
        export_metadata=data.get("export_metadata")
        if isinstance(data.get("export_metadata"), dict)
        else None,
    )


def _file_diff_item(event: NormalizedChatEvent) -> FileDiffTimelineItem:
    return FileDiffTimelineItem(
        id=f"file-diff:{event.source_ref.session_id}:{event.source_ref.seq}",
        sort_key=_sort_key(event),
        source_refs=[event.source_ref],
        created_at=event.timestamp,
        updated_at=event.timestamp,
        file_diffs=_file_diffs(event.data.get("file_diffs")),
        title=_str_or_none(event.data.get("title")),
    )


def _notice_item(event: NormalizedChatEvent) -> NoticeTimelineItem:
    data = event.data
    notice_id = str(data.get("notice_id") or data.get("id") or _fallback_id(event))
    return NoticeTimelineItem(
        id=f"notice:{notice_id}",
        sort_key=_sort_key(event),
        source_refs=[event.source_ref],
        created_at=event.timestamp,
        updated_at=event.timestamp,
        level="warning" if event.source_ref.event_type == "history_gap" else "info",
        title=str(data.get("title") or data.get("message") or event.source_ref.event_type),
        message=_str_or_none(data.get("message") or data.get("summary")),
    )


def _compaction_item(event: NormalizedChatEvent) -> CompactionTimelineItem | object:
    data = event.data
    if data.get("timeline_visible") is False:
        return HIDDEN_EVENT

    session_id = _str_or_none(data.get("session_id")) or event.source_ref.session_id
    # Legacy compaction checkpoints were recorded in the source session before
    # rotation and carry no explicit source_session_id. Give them the same
    # stable identity as the rotated marker so historical conversations fold to
    # one card instead of rendering a duplicate.
    previous_session_id = _str_or_none(data.get("source_session_id")) or event.source_ref.session_id
    summary = str(data.get("summary") or "")
    item_id = (
        f"compaction:{previous_session_id}"
        if previous_session_id
        else f"compaction:{_fallback_id(event)}"
    )
    turns_compacted = data.get("turns_compacted")
    return CompactionTimelineItem(
        id=item_id,
        sort_key=_sort_key(event),
        source_refs=[event.source_ref],
        created_at=event.timestamp,
        updated_at=event.timestamp,
        status="compacted",
        session_id=session_id,
        previous_session_id=previous_session_id,
        summary_preview=summary[:500],
        summary=summary,
        method=str(data.get("method") or "unknown"),
        turns_compacted=turns_compacted
        if isinstance(turns_compacted, int) and turns_compacted >= 0
        else 0,
        trigger=_str_or_none(data.get("trigger")),
        reason=_str_or_none(data.get("reason")),
        previous_usage_percentage=_float_or_none(data.get("previous_usage_percentage")),
        effective_usage_percentage=_float_or_none(data.get("effective_usage_percentage")),
        hard_pressure_exceeded=data.get("hard_pressure_exceeded") is True,
        used_timeout_fallback=data.get("used_timeout_fallback") is True,
    )


def _error_item(event: NormalizedChatEvent) -> ErrorTimelineItem:
    data = event.data
    error_id = str(data.get("error_id") or data.get("id") or _fallback_id(event))
    return ErrorTimelineItem(
        id=f"error:{error_id}",
        sort_key=_sort_key(event),
        source_refs=[event.source_ref],
        created_at=event.timestamp,
        updated_at=event.timestamp,
        status="failed",
        title=str(data.get("title") or data.get("error") or "Error"),
        message=_str_or_none(data.get("message")),
        error_code=_str_or_none(data.get("error_code") or data.get("code")),
        recoverable=bool(data.get("recoverable", False)),
    )


def _evaluation_feedback_notice(event: NormalizedChatEvent) -> NoticeTimelineItem:
    data = event.data
    attempt = _int_or_none(data.get("attempt"))
    decision = _str_or_none(data.get("decision"))
    title = f"Step evaluation (attempt {attempt})" if attempt is not None else "Step evaluation"
    if decision:
        title = f"{title}: {decision}"
    return NoticeTimelineItem(
        id=f"notice:evaluation:{_fallback_id(event)}",
        sort_key=_sort_key(event),
        source_refs=[event.source_ref],
        created_at=event.timestamp,
        updated_at=event.timestamp,
        level="info",
        title=title,
        message=_str_or_none(data.get("feedback") or data.get("reasoning")),
    )


def _unknown_notice(event: NormalizedChatEvent) -> NoticeTimelineItem:
    return NoticeTimelineItem(
        id=f"unknown:{_fallback_id(event)}",
        sort_key=_sort_key(event),
        source_refs=[event.source_ref],
        created_at=event.timestamp,
        updated_at=event.timestamp,
        level="warning",
        title="Unsupported chat event",
        message=f"Unsupported event type: {event.source_ref.event_type}",
    )


def _upsert_item(items_by_id: dict[str, TimelineItem], item: TimelineItem) -> None:
    existing = items_by_id.get(item.id)
    if existing is None:
        items_by_id[item.id] = item
        return
    # Later projections with the same stable ID are authoritative, but retain
    # source refs from earlier events for audit/debug provenance.
    item.source_refs = _merged_source_refs(existing, *item.source_refs)
    items_by_id[item.id] = item


def _sort_key(event: NormalizedChatEvent) -> str:
    phase = event.assistant_phase_index if event.assistant_phase_index is not None else 0
    return encode_timeline_sort_key(
        lineage=event.lineage_ordinal,
        seq=event.source_ref.seq,
        phase=phase,
        kind_rank=KIND_RANK[event.kind],
        local=event.local_ordinal,
    )


def _message_id(event: NormalizedChatEvent, *, prefix: str) -> str:
    value = event.data.get("message_id")
    if isinstance(value, str) and value:
        return value
    if prefix == "system":
        notice_id = _str_or_none(event.data.get("notice_id") or event.data.get("id"))
        if notice_id:
            return notice_id
        notice_kind = _str_or_none(event.data.get("kind"))
        turn_id = _str_or_none(event.data.get("turn_id"))
        if notice_kind and turn_id:
            return f"{notice_kind}:{turn_id}"
    return f"{prefix}:{event.source_ref.session_id}:{event.source_ref.seq}"


def _message_item_id(event: NormalizedChatEvent, *, role: str, message_id: str) -> str:
    data = event.data
    if role == "user":
        client_message_id = _str_or_none(data.get("client_message_id"))
        if client_message_id:
            return f"user:{client_message_id}"
        queue_id = _str_or_none(data.get("queue_id"))
        if queue_id:
            return f"user:{queue_id}"
        client_txn_id = _str_or_none(data.get("client_txn_id"))
        if client_txn_id:
            return f"user-txn:{client_txn_id}"
        return f"user:{message_id}"
    if role == "assistant":
        # Phase-aware id so a multi-phase turn (text → tool_call → text …) keeps
        # each assistant segment as a distinct item. Every segment shares the
        # same message_id (= turn_id) but carries a distinct assistant_phase_index;
        # without the phase they collide onto one id and _upsert_item overwrites
        # earlier segments, dropping mid-turn assistant text after reload. This
        # keeps canonical, runtime overlay, and completion items merged 1:1 by id.
        return assistant_message_item_id(message_id=message_id, phase=event.assistant_phase_index)
    return f"system:{message_id}"


def _thinking_item_id(event: NormalizedChatEvent, *, message_id: str | None, block_id: str) -> str:
    base = message_id or f"{event.source_ref.session_id}:{event.source_ref.seq}"
    return thinking_item_id(message_id=base, phase=event.assistant_phase_index, block_id=block_id)


def _call_id(event: NormalizedChatEvent) -> str:
    return str(event.data.get("call_id") or event.data.get("id") or _fallback_id(event))


def _evaluation_call_id(event: NormalizedChatEvent) -> str | None:
    """Return the real tool call id targeted by an evaluation sidecar."""

    value = (
        event.data.get("tool_call_id")
        or event.data.get("source_tool_call_id")
        or event.data.get("evaluated_tool_call_id")
        or event.data.get("call_id")
    )
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _fallback_id(event: NormalizedChatEvent) -> str:
    return f"{event.source_ref.store}:{event.source_ref.session_id}:{event.source_ref.seq}"


def _merged_source_refs(existing: TimelineItem | None, *refs: SourceRef | None) -> list[SourceRef]:
    merged: list[SourceRef] = []
    seen: set[tuple[str, str, int, str]] = set()
    for ref in [*(existing.source_refs if existing is not None else []), *refs]:
        if ref is None:
            continue
        key = (ref.store, ref.session_id, ref.seq, ref.event_type)
        if key in seen:
            continue
        seen.add(key)
        merged.append(ref)
    return merged


def _preview(value: Any, *, limit: int = 4000) -> str | None:
    if value is None:
        return None
    text = value if isinstance(value, str) else repr(value)
    if len(text) <= limit:
        return text
    return f"{text[:limit]}…"


def _arguments_preview(value: Any) -> str | None:
    """Raw-payload fallback preview, ONLY when structured args are unavailable.

    When ``_structured_arguments`` yields a named-parameter dict, the client
    renders the per-tool subtitle/body from it and never needs the preview. In
    that case we omit the preview so a stale ``repr(dict)`` string (single
    quotes) can never leak into the UI if a downstream merge drops the dict.
    The preview is retained only for arguments that cannot be structured (e.g.
    a non-dict JSON scalar or an unparseable string).
    """
    if _structured_arguments(value) is not None:
        return None
    return _preview(value)


def _structured_arguments(value: Any) -> dict[str, Any] | None:
    """Return the tool arguments as a named-parameter dict.

    Tool arguments are persisted either as a dict or a JSON string. The client
    renders concise per-tool subtitles and rich bodies from the named keys
    (path/command/pattern/query/title/todos/...), so we expose the structured
    dict, parsing JSON strings back into a dict when possible.
    """
    if isinstance(value, dict):
        return dict(value)
    if isinstance(value, str) and value:
        try:
            parsed = json.loads(value)
        except (ValueError, TypeError):
            return None
        return parsed if isinstance(parsed, dict) else None
    return None


def _evaluation_payload(data: dict[str, Any]) -> dict[str, Any]:
    value = data.get("evaluation")
    if isinstance(value, dict):
        return dict(value)
    payload: dict[str, Any] = {}
    for key in ("decision", "reasoning", "risk", "path", "latency_ms"):
        if key in data and data[key] is not None:
            payload[key] = data[key]
    if not payload:
        payload["decision"] = str(data.get("event") or data.get("type") or "evaluation")
    return payload


def _status(data: dict[str, Any], *, default: TimelineItemStatus) -> TimelineItemStatus:
    value = data.get("status")
    if value in {"pending", "running", "waiting", "complete", "failed", "cancelled"}:
        return cast(TimelineItemStatus, value)
    return default


def _thinking_status(data: dict[str, Any]) -> ThinkingStatus | None:
    value = data.get("status")
    if value in {"running", "complete", "failed"}:
        return cast(ThinkingStatus, value)
    if data.get("completed_at"):
        return "complete"
    return None


def _question_status(data: dict[str, Any]) -> QuestionStatus:
    value = data.get("status")
    if value in {"waiting", "complete", "cancelled"}:
        return cast(QuestionStatus, value)
    return "waiting"


def _request_status(data: dict[str, Any]) -> RequestStatus:
    value = data.get("status")
    if value in {"waiting", "complete", "cancelled", "failed"}:
        return cast(RequestStatus, value)
    return "waiting"


def _task_status(event: NormalizedChatEvent) -> TimelineItemStatus:
    event_type = event.source_ref.event_type
    lifecycle_event = str(event.data.get("event") or event.data.get("type") or "")
    if event_type in {"task_failed"} or lifecycle_event == "task_failed":
        return "failed"
    if event_type in {"task_cancelled"} or lifecycle_event == "task_cancelled":
        return "cancelled"
    if event_type in {"task_result"} or lifecycle_event in {"task_result", "workflow_composed"}:
        return "complete"
    return "running"


def _file_diffs(value: Any) -> list[FileDiffRef]:
    if not isinstance(value, list):
        return []
    result: list[FileDiffRef] = []
    for item in value:
        if isinstance(item, dict):
            result.append(
                FileDiffRef(
                    path=str(item.get("path") or item.get("file_path") or ""),
                    diff=str(item.get("diff") or item.get("patch") or ""),
                )
            )
    return result


def _dict_str_str_or_none(value: Any) -> dict[str, str] | None:
    if not isinstance(value, dict):
        return None
    return {str(key): str(inner_value) for key, inner_value in value.items()}


def _int_or_none(value: Any) -> int | None:
    if isinstance(value, int):
        return value
    return None


def _float_or_none(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        return float(value)
    return None


def _nonnegative_int(value: Any) -> int | None:
    number = _int_or_none(value)
    if number is None or number < 0:
        return None
    return number


def _nonnegative_float(value: Any) -> float | None:
    number = _float_or_none(value)
    if number is None or number < 0:
        return None
    return number


def _todo_list(value: Any) -> list[dict[str, Any]] | None:
    if not isinstance(value, list):
        return None
    todos = [item for item in value if isinstance(item, dict)]
    return todos or None


def _chat_mode(data: dict[str, Any]) -> ChatMode | None:
    value = data.get("chat_mode")
    if value in {"default", "plan", "build"}:
        return cast(ChatMode, value)
    return None


def _str_or_none(value: Any) -> str | None:
    if isinstance(value, str) and value:
        return value
    return None
