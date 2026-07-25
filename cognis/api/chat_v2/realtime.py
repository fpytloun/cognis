"""Chat v2 native realtime frame and runtime item helpers."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Any, Literal, cast

from cognis.api.chat_v2.cycles import cycle_states_from_items
from cognis.api.chat_v2.item_keys import (
    KIND_RANK,
    assistant_message_item_id,
    late_runtime_timeline_sort_key,
    pre_turn_runtime_timeline_sort_key,
    runtime_timeline_sort_key,
    thinking_item_id,
)
from cognis.api.chat_v2.schemas import (
    ChatRealtimeFrame,
    CompactionTimelineItem,
    FileDiffRef,
    MessageTimelineItem,
    RuntimeActiveTurn,
    RuntimeOverlaySnapshot,
    SourceRef,
    ThinkingBlock,
    ThinkingTimelineItem,
    TimelineItem,
    TimelineItemStatus,
    TimelineScope,
    ToolCallTimelineItem,
)
from cognis.api.chat_v2.sync import current_projection_version, runtime_epoch_for
from cognis.models.artifact import AttachmentRef
from cognis.models.config import GenerationPerformanceSnapshot


def scope_accepts_runtime(
    scope: TimelineScope,
    *,
    conversation_id: str,
    active_session_id: str | None,
) -> bool:
    """Return whether runtime from a conversation belongs to a subscribed scope."""

    if scope.missing_stream:
        return False
    if scope.conversation_id != conversation_id:
        return False
    if scope.kind == "conversation":
        return True
    return bool(scope.session_id and scope.session_id == active_session_id)


def runtime_overlay_from_items(
    *,
    conversation_id: str,
    scope: TimelineScope | None = None,
    runtime_revision: int,
    has_active_turn: bool,
    active_turn: Mapping[str, Any] | None,
    volatile_items: Sequence[TimelineItem],
    context_usage: Mapping[str, Any] | None = None,
    last_generation: Mapping[str, Any] | None = None,
    generated_at: str | None = None,
) -> RuntimeOverlaySnapshot:
    """Build a strict Chat v2 runtime overlay from native TimelineItem models."""

    active_turn_model: RuntimeActiveTurn | None = None
    if has_active_turn and active_turn is not None:
        active_turn_model = RuntimeActiveTurn.model_validate(active_turn)
    sorted_volatile_items = sorted(list(volatile_items), key=lambda item: item.sort_key)
    return RuntimeOverlaySnapshot(
        runtime_epoch=runtime_epoch_for(
            scope.key if scope is not None else f"conversation:{conversation_id}"
        ),
        runtime_revision=runtime_revision,
        generated_at=generated_at or datetime.now(UTC).isoformat(),
        has_active_turn=active_turn_model is not None,
        active_turn=active_turn_model,
        volatile_items=sorted_volatile_items,
        cycle_states=cycle_states_from_items(sorted_volatile_items),
        context_usage=dict(context_usage) if context_usage is not None else None,
        last_generation=(
            GenerationPerformanceSnapshot.model_validate(last_generation)
            if last_generation is not None
            else None
        ),
    )


def runtime_frame(
    *,
    conversation_id: str,
    scope: TimelineScope | None = None,
    cursor: str,
    runtime: RuntimeOverlaySnapshot,
    server_time: str | None = None,
) -> ChatRealtimeFrame:
    """Build a runtime-only Chat v2 frame without advancing the canonical cursor."""

    resolved_scope = scope or TimelineScope(
        key=f"conversation:{conversation_id}",
        kind="conversation",
        conversation_id=conversation_id,
    )
    return ChatRealtimeFrame(
        projection_version=current_projection_version(),
        scope=resolved_scope,
        conversation_id=conversation_id,
        cursor_before=cursor,
        cursor_after=cursor,
        ops=[],
        cycle_states=runtime.cycle_states,
        runtime=runtime,
        server_time=server_time or datetime.now(UTC).isoformat(),
    )


def compaction_runtime_item(
    event_data: Mapping[str, Any],
    *,
    status: Literal["running", "compacted", "failed", "skipped"] = "running",
) -> CompactionTimelineItem | None:
    """Project an in-flight compaction lifecycle event into the runtime overlay."""

    session_id = _str(event_data.get("session_id"))
    if session_id is None:
        return None
    previous_session_id = _str(event_data.get("previous_session_id"))
    source_session_id = previous_session_id or session_id
    timestamp = datetime.now(UTC).isoformat()
    return CompactionTimelineItem(
        id=f"compaction:{source_session_id}",
        kind="compaction",
        sort_key=pre_turn_runtime_timeline_sort_key(
            kind_rank=KIND_RANK["compaction"],
            local=0,
        ),
        source_refs=[
            SourceRef(
                store="runtime",
                session_id=session_id,
                seq=0,
                event_type="session_compaction_started",
            )
        ],
        created_at=timestamp,
        updated_at=timestamp,
        status=status,
        stable=False,
        session_id=session_id,
        previous_session_id=previous_session_id,
        summary_preview=(
            _str(event_data.get("summary_preview"))
            or (
                "Compacting conversation history…"
                if status == "running"
                else "Conversation compaction did not complete."
            )
        ),
        method=_str(event_data.get("method")) or "pending",
        turns_compacted=max(0, _int(event_data.get("turns_compacted")) or 0),
        trigger=_str(event_data.get("trigger")),
        reason=_str(event_data.get("reason")),
        hard_pressure_exceeded=bool(event_data.get("hard_pressure_exceeded", False)),
        used_timeout_fallback=bool(event_data.get("used_timeout_fallback", False)),
    )


def runtime_items_from_snapshots(
    *,
    active_streams: Sequence[Mapping[str, Any]] | None = None,
    active_tool_outputs: Sequence[Mapping[str, Any]] | None = None,
    active_thinking: Sequence[Mapping[str, Any]] | None = None,
    phase_hint_items: Sequence[TimelineItem] | None = None,
    chat_mode: str | None = None,
    chat_mode_source: str | None = None,
) -> list[TimelineItem]:
    """Project volatile runtime snapshots directly into strict Chat v2 items."""

    items: list[TimelineItem] = []

    for index, snapshot in enumerate(active_streams or []):
        message_item = assistant_stream_runtime_item(
            snapshot,
            local=index,
            phase_hint_items=phase_hint_items,
            chat_mode=chat_mode,
            chat_mode_source=chat_mode_source,
        )
        if message_item is not None:
            items.append(message_item)

    for index, snapshot in enumerate(active_tool_outputs or []):
        tool_item = tool_output_runtime_item(snapshot, local=index)
        if tool_item is not None:
            items.append(tool_item)

    think_local = 0
    for snapshot in active_thinking or []:
        for thinking_item in thinking_runtime_items(
            snapshot,
            local_start=think_local,
            phase_hint_items=phase_hint_items,
        ):
            items.append(thinking_item)
            think_local += 1

    return sorted(items, key=lambda item: item.sort_key)


def assistant_stream_runtime_item(
    snapshot: Mapping[str, Any],
    *,
    local: int,
    phase_hint_items: Sequence[TimelineItem] | None = None,
    chat_mode: str | None = None,
    chat_mode_source: str | None = None,
) -> MessageTimelineItem | None:
    content = snapshot.get("content")
    message_id = snapshot.get("message_id")
    if not isinstance(content, str) or not content:
        return None
    if not isinstance(message_id, str) or not message_id:
        return None
    turn_id = _str(snapshot.get("turn_id")) or message_id
    phase = _phase(snapshot.get("assistant_phase_index"))
    if phase is None:
        phase = _next_runtime_assistant_phase(phase_hint_items, turn_id)
    turn_cycle_index = _cycle(snapshot.get("turn_cycle_index"))
    if "turn_cycle_index" not in snapshot and phase is not None:
        turn_cycle_index = phase
    timestamp = _str(snapshot.get("updated_at"))
    return MessageTimelineItem(
        id=assistant_message_item_id(message_id=message_id, phase=phase),
        kind="message",
        sort_key=runtime_timeline_sort_key(
            phase=phase,
            kind_rank=KIND_RANK["assistant_message"],
            local=local,
        ),
        source_refs=[_runtime_source_ref(snapshot, event_type="assistant_stream")],
        created_at=timestamp,
        updated_at=timestamp,
        status="running",
        stable=False,
        role="assistant",
        content=content,
        message_id=message_id,
        turn_id=turn_id,
        assistant_phase_index=phase,
        turn_cycle_index=turn_cycle_index,
        partial=True,
        chat_mode=cast(Any, chat_mode) if chat_mode in {"default", "plan", "build"} else None,
        chat_mode_source=chat_mode_source if isinstance(chat_mode_source, str) else None,
    )


def assistant_completion_runtime_item(
    *,
    message_id: str,
    turn_id: str | None,
    session_id: str | None,
    phase: int,
    content: str,
    timestamp: str,
    partial: bool,
    chat_mode: str | None = None,
    chat_mode_source: str | None = None,
    turn_cycle_index: int | None = None,
) -> MessageTimelineItem:
    return MessageTimelineItem(
        id=assistant_message_item_id(message_id=message_id, phase=phase),
        kind="message",
        sort_key=runtime_timeline_sort_key(
            phase=phase,
            kind_rank=KIND_RANK["assistant_message"],
            local=0,
        ),
        source_refs=[
            SourceRef(
                store="runtime",
                session_id=session_id or "runtime",
                seq=0,
                event_type="assistant_complete",
            )
        ],
        created_at=timestamp,
        updated_at=timestamp,
        status="complete",
        stable=False,
        role="assistant",
        content=content,
        message_id=message_id,
        turn_id=turn_id or message_id,
        assistant_phase_index=phase,
        # Preserve None (do NOT coerce to 0). The completion frame shares the
        # streamed item's id, and the client merges turn_cycle_index as
        # `incoming ?? existing`. Coercing an unknown cycle to 0 would clobber
        # the correct streamed cycle and make the settled message fold into the
        # cycle-0 tool group. A genuine None lets the client keep what streamed.
        turn_cycle_index=_phase(turn_cycle_index),
        partial=partial,
        chat_mode=cast(Any, chat_mode) if chat_mode in {"default", "plan", "build"} else None,
        chat_mode_source=chat_mode_source if isinstance(chat_mode_source, str) else None,
    )


def tool_call_runtime_item(
    *,
    session_id: str,
    call_id: str,
    tool_name: str,
    arguments: dict[str, Any] | None,
    turn_id: str | None,
    assistant_phase_index: int | None,
    turn_cycle_index: int | None,
    timestamp: str,
) -> ToolCallTimelineItem:
    return ToolCallTimelineItem(
        id=f"tool:{call_id}",
        kind="tool_call",
        sort_key=runtime_timeline_sort_key(
            phase=assistant_phase_index,
            kind_rank=KIND_RANK["tool_call"],
            local=0,
        ),
        source_refs=[
            SourceRef(store="runtime", session_id=session_id, seq=0, event_type="tool_call")
        ],
        created_at=timestamp,
        updated_at=timestamp,
        status="running",
        stable=False,
        call_id=call_id,
        tool_name=tool_name,
        turn_id=turn_id,
        assistant_phase_index=assistant_phase_index,
        turn_cycle_index=_cycle(turn_cycle_index),
        arguments=arguments,
        arguments_preview=_preview(arguments),
    )


def tool_result_runtime_item(
    *,
    session_id: str,
    call_id: str,
    tool_name: str,
    result: str,
    is_error: bool,
    duration_ms: int | None,
    evaluation: dict[str, Any] | None,
    attachments: list[dict[str, Any]] | None,
    file_diffs: list[dict[str, Any]] | None,
    turn_id: str | None,
    assistant_phase_index: int | None,
    turn_cycle_index: int | None,
    timestamp: str,
    presentation: dict[str, Any] | None = None,
) -> ToolCallTimelineItem:
    presentation = presentation or {}
    return ToolCallTimelineItem(
        id=f"tool:{call_id}",
        kind="tool_call",
        sort_key=runtime_timeline_sort_key(
            phase=assistant_phase_index,
            kind_rank=KIND_RANK["tool_call"],
            local=0,
        ),
        source_refs=[
            SourceRef(store="runtime", session_id=session_id, seq=0, event_type="tool_result")
        ],
        created_at=timestamp,
        updated_at=timestamp,
        status="failed" if is_error else "complete",
        stable=False,
        call_id=call_id,
        tool_name=tool_name,
        turn_id=turn_id,
        assistant_phase_index=assistant_phase_index,
        turn_cycle_index=_cycle(turn_cycle_index),
        result_preview=result,
        is_error=is_error,
        duration_ms=duration_ms if isinstance(duration_ms, int) else None,
        attachments=_attachments(attachments),
        file_diffs=[FileDiffRef.model_validate(diff) for diff in file_diffs or []],
        output_size=_int(presentation.get("output_size")) or len(result),
        truncated=bool(presentation.get("truncated")),
        has_full_output=bool(presentation.get("has_full_output")),
        recovery_call_id=_str(presentation.get("recovery_call_id")),
        tool_output_artifact_id=_str(presentation.get("tool_output_artifact_id")),
        evaluation=evaluation,
    )


def tool_output_runtime_item(
    snapshot: Mapping[str, Any],
    *,
    local: int,
) -> ToolCallTimelineItem | None:
    call_id = _str(snapshot.get("call_id"))
    if not call_id:
        return None
    tool_name = _str(snapshot.get("tool_name")) or "unknown"
    result = snapshot.get("result")
    result_text = result if isinstance(result, str) else ""
    phase = _phase(snapshot.get("assistant_phase_index"))
    timestamp = _str(snapshot.get("updated_at"))
    output_size = _int(snapshot.get("output_size")) or len(result_text)
    return ToolCallTimelineItem(
        id=f"tool:{call_id}",
        kind="tool_call",
        sort_key=runtime_timeline_sort_key(
            phase=phase,
            kind_rank=KIND_RANK["tool_call"],
            local=local,
        ),
        source_refs=[_runtime_source_ref(snapshot, event_type="tool_output")],
        created_at=timestamp,
        updated_at=timestamp,
        status=_status(snapshot.get("status")) or "running",
        stable=False,
        call_id=call_id,
        tool_name=tool_name,
        turn_id=_str(snapshot.get("turn_id")),
        assistant_phase_index=phase,
        turn_cycle_index=_cycle(snapshot.get("turn_cycle_index")),
        arguments=snapshot.get("arguments")
        if isinstance(snapshot.get("arguments"), dict)
        else None,
        result_preview=result_text or None,
        is_error=snapshot.get("is_error") is True,
        output_size=output_size,
        truncated=snapshot.get("truncated") is True,
        has_full_output=snapshot.get("has_full_output") is True,
        recovery_call_id=_str(snapshot.get("recovery_call_id")),
        tool_output_artifact_id=_str(snapshot.get("tool_output_artifact_id")),
        progress_phase=_str(snapshot.get("progress_phase")),
        progress_input_chars=_int(snapshot.get("progress_input_chars")),
        progress_input_lines=_int(snapshot.get("progress_input_lines")),
        progress_complete=snapshot.get("progress_complete")
        if isinstance(snapshot.get("progress_complete"), bool)
        else None,
        managed_conversation=snapshot.get("managed_conversation")
        if isinstance(snapshot.get("managed_conversation"), dict)
        else None,
    )


def thinking_runtime_items(
    snapshot: Mapping[str, Any],
    *,
    local_start: int,
    phase_hint_items: Sequence[TimelineItem] | None = None,
) -> list[ThinkingTimelineItem]:
    message_id = _str(snapshot.get("message_id"))
    if not message_id:
        return []
    turn_id = _str(snapshot.get("turn_id")) or message_id
    phase = _phase(snapshot.get("assistant_phase_index"))
    if phase is None:
        phase = _next_runtime_assistant_phase(phase_hint_items, turn_id)
    turn_cycle_index = _cycle(snapshot.get("turn_cycle_index"))
    if "turn_cycle_index" not in snapshot and phase is not None:
        turn_cycle_index = phase
    raw_blocks = snapshot.get("blocks")
    if not isinstance(raw_blocks, list):
        return []
    timestamp = _str(snapshot.get("updated_at"))
    items: list[ThinkingTimelineItem] = []
    for offset, raw_block in enumerate(raw_blocks):
        if not isinstance(raw_block, Mapping):
            continue
        block_id = _thinking_runtime_block_id(snapshot, raw_block, offset)
        content = raw_block.get("content")
        if not block_id or not isinstance(content, str) or not content:
            continue
        complete = raw_block.get("complete") is True
        title = _str(raw_block.get("title")) or "Thinking"
        block = ThinkingBlock(
            id=block_id,
            title=title,
            content=content,
            status="complete" if complete else "running",
            started_at=_str(raw_block.get("started_at")),
            completed_at=_str(raw_block.get("completed_at")),
            duration_ms=_int(raw_block.get("duration_ms")),
        )
        items.append(
            ThinkingTimelineItem(
                id=thinking_item_id(message_id=message_id, phase=phase, block_id=block_id),
                kind="thinking",
                sort_key=runtime_timeline_sort_key(
                    phase=phase,
                    kind_rank=KIND_RANK["thinking"],
                    local=local_start + offset,
                ),
                source_refs=[_runtime_source_ref(snapshot, event_type="thinking")],
                created_at=timestamp,
                updated_at=timestamp,
                status="complete" if complete else "running",
                stable=False,
                message_id=message_id,
                turn_id=turn_id,
                assistant_phase_index=phase,
                turn_cycle_index=turn_cycle_index,
                blocks=[block],
                active_title=None if complete else title,
            )
        )
    return items


def _thinking_runtime_block_id(
    snapshot: Mapping[str, Any], raw_block: Mapping[str, Any], offset: int
) -> str | None:
    explicit = _str(raw_block.get("block_id")) or _str(raw_block.get("id"))
    if explicit:
        return explicit
    first_block_id = _str(snapshot.get("first_block_id"))
    if first_block_id:
        return first_block_id if offset == 0 else f"{first_block_id}:{offset}"
    seq = (
        _int(raw_block.get("seq"))
        or _int(raw_block.get("source_seq"))
        or _int(snapshot.get("seq"))
        or _int(snapshot.get("source_seq"))
    )
    if seq is not None:
        return f"seq-{seq}" if offset == 0 else f"seq-{seq}:{offset}"
    return f"block-{offset}"


def delegation_runtime_item(
    event_data: Mapping[str, Any], *, timestamp: str
) -> ToolCallTimelineItem | None:
    call_id = _str(event_data.get("call_id"))
    if not call_id:
        return None
    mode = event_data.get("mode")
    tool_name = mode if mode in {"delegate", "fork"} else "delegate"
    status = _delegation_status(event_data)
    session_id = (
        _str(event_data.get("parent_session_id")) or _str(event_data.get("session_id")) or "runtime"
    )
    phase = _phase(event_data.get("assistant_phase_index"))
    sort_key = (
        runtime_timeline_sort_key(
            phase=phase,
            kind_rank=KIND_RANK["tool_call"],
            local=0,
        )
        if phase is not None
        else late_runtime_timeline_sort_key(
            kind_rank=KIND_RANK["tool_call"],
            local=0,
        )
    )
    delegation: dict[str, Any] = {
        "child_session_id": event_data.get("child_session_id") or event_data.get("session_id"),
        "status": status,
        "turn_id": _str(event_data.get("turn_id")),
        "assistant_phase_index": phase,
        "turn_cycle_index": _cycle(event_data.get("turn_cycle_index")),
        "agent_id": event_data.get("agent_id") or event_data.get("used_agent_id"),
        "used_agent_id": event_data.get("used_agent_id"),
        "title": event_data.get("title") or event_data.get("task_title") or event_data.get("label"),
        "summary": event_data.get("summary") or event_data.get("task"),
        "started_at": event_data.get("started_at"),
        "duration_ms": event_data.get("duration_ms"),
        "result_summary": event_data.get("result_summary") or event_data.get("result"),
        "result_content": event_data.get("result_content"),
        "result_source": event_data.get("result_source"),
        "result_truncated": event_data.get("result_truncated"),
        "result_anchors": event_data.get("result_anchors"),
        "todos": event_data.get("todos") if isinstance(event_data.get("todos"), list) else [],
        "tool_call_count": event_data.get("tool_call_count"),
        "max_tool_calls": event_data.get("max_tool_calls"),
        "last_tool": event_data.get("last_tool"),
        "error": event_data.get("error") or event_data.get("reason"),
    }
    return ToolCallTimelineItem(
        id=f"tool:{call_id}",
        kind="tool_call",
        sort_key=sort_key,
        source_refs=[
            SourceRef(store="runtime", session_id=session_id, seq=0, event_type="delegation")
        ],
        created_at=timestamp,
        updated_at=timestamp,
        status=_status(status) or "running",
        stable=False,
        call_id=call_id,
        tool_name=str(tool_name),
        turn_id=_str(event_data.get("turn_id")),
        assistant_phase_index=phase,
        turn_cycle_index=_cycle(event_data.get("turn_cycle_index")),
        delegation=delegation,
    )


def _runtime_source_ref(snapshot: Mapping[str, Any], *, event_type: str) -> SourceRef:
    return SourceRef(
        store="runtime",
        session_id=_str(snapshot.get("session_id")) or "runtime",
        seq=0,
        event_type=event_type,
    )


def _next_runtime_assistant_phase(
    items: Sequence[TimelineItem] | None,
    turn_id: str | None,
) -> int | None:
    if not turn_id:
        return None
    if not items:
        return None
    phases = [
        phase
        for item in items or []
        if getattr(item, "turn_id", None) == turn_id
        for phase in [getattr(item, "assistant_phase_index", None)]
        if isinstance(phase, int)
    ]
    if phases:
        return max(phases) + 1
    return sum(
        1
        for item in items or []
        if isinstance(item, MessageTimelineItem)
        and item.role == "assistant"
        and item.turn_id == turn_id
    )


def _delegation_status(data: Mapping[str, Any]) -> str:
    status = data.get("status")
    if isinstance(status, str) and status:
        return status
    event_status = data.get("event")
    return str(event_status) if isinstance(event_status, str) and event_status else "running"


def _attachments(value: Any) -> list[AttachmentRef]:
    if not isinstance(value, list):
        return []
    attachments: list[AttachmentRef] = []
    for attachment in value:
        if isinstance(attachment, AttachmentRef):
            attachments.append(attachment)
        elif isinstance(attachment, dict):
            attachments.append(AttachmentRef.model_validate(attachment))
    return attachments


def _status(value: Any) -> TimelineItemStatus | None:
    if value in {"pending", "running", "waiting", "complete", "failed", "cancelled"}:
        return cast(TimelineItemStatus, value)
    if value in {"started", "in_progress"}:
        return "running"
    if value in {"completed", "success", "succeeded"}:
        return "complete"
    if value == "error":
        return "failed"
    return None


def _preview(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    return repr(value)


def _str(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _int(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _phase(value: Any) -> int | None:
    return value if isinstance(value, int) and value >= 0 else None


def _cycle(value: Any) -> int:
    phase = _phase(value)
    return phase if phase is not None else 0
