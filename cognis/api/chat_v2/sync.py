"""Snapshot, sync, and backfill orchestration for Chat v2."""

from __future__ import annotations

import logging
import uuid
from collections.abc import Awaitable, Callable, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

from pydantic import Field

from cognis.api.chat_v2.cursors import (
    ChatCursorError,
    CursorLineageEntry,
    CursorSessionWatermark,
    InternalChatCursorPayload,
    encode_cursor,
    validate_cursor,
)
from cognis.api.chat_v2.event_store import RawSessionEvent, SessionEventStore
from cognis.api.chat_v2.normalizer import normalize_session_events
from cognis.api.chat_v2.projector import project_timeline
from cognis.api.chat_v2.schemas import (
    ChatResetReason,
    ChatSnapshot,
    ChatSyncResponse,
    ChatViewOp,
    ConversationStateView,
    ConversationSummary,
    QueueMessage,
    QueueState,
    ReplaceConversationOp,
    ReplaceQueueOp,
    ReplaceStateOp,
    RuntimeActiveTurn,
    RuntimeOverlaySnapshot,
    StrictModel,
    TimelineBackfillResponse,
    TimelineItem,
    TimelineWindow,
    UpsertTimelineItemOp,
)

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 2
# v3: assistant message timeline ids became phase-aware
# (message:{id}:phase:{n}). Bumped so a client holding a pre-deploy cached view
# (with unphased message:{id} items) forces a clean reset on its next sync
# instead of mixing old and new ids under the same projection version.
PROJECTION_VERSION = "chat-v2-projection-v3"
SNAPSHOT_SESSION_EVENT_LIMIT = 5_000
SNAPSHOT_WINDOW_EVENT_LIMIT = 800

# Process-scoped runtime epoch component. The runtime revision counter used for
# WebSocket runtime overlays is an in-memory, per-conversation counter that
# resets to 0 whenever the process restarts. If the epoch stayed constant
# across restarts, a client that had already applied a higher revision would
# silently drop every post-restart runtime frame (the client only accepts a
# strictly greater revision for the same epoch), freezing the live turn until a
# recovery snapshot.
#
# Binding the epoch to a per-process id makes a restart (or a reconnect to a
# different replica) produce a NEW epoch, which the client treats as a clean
# wholesale replacement of the overlay instead of a drop. This is the durable
# fix for "streaming stuck / one device ahead of another" after a restart or
# across replicas.
RUNTIME_PROCESS_EPOCH = uuid.uuid4().hex


def runtime_epoch_for(conversation_id: str) -> str:
    """Return the process-scoped runtime epoch for a conversation."""

    return f"conversation:{conversation_id}:{RUNTIME_PROCESS_EPOCH}"


SYNC_DEFAULT_LIMIT = 500
SYNC_MIN_LIMIT = 1
SYNC_MAX_LIMIT = 1_000
BACKFILL_DEFAULT_LIMIT = 100
BACKFILL_MIN_LIMIT = 1
BACKFILL_MAX_LIMIT = 200
CURSOR_TTL = timedelta(days=30)


class ChatV2SyncError(ValueError):
    """Raised when a Chat v2 sync request is invalid."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class ConversationSessionRef(StrictModel):
    """Session lineage entry used by Chat v2 projection orchestration."""

    session_id: str
    event_store_session_id: str
    store: str = "intaris"
    role: str = "root"
    ordinal: int = Field(ge=0)
    status: str | None = None
    completion_reason: str | None = None


class RuntimeOverlayInput(StrictModel):
    """Framework-neutral runtime overlay inputs collected by the route layer."""

    runtime_epoch: str
    runtime_revision: int = Field(ge=0)
    active_turn: dict[str, Any] | None = None
    context_usage: dict[str, Any] | None = None


class _EventWindow(StrictModel):
    events: list[RawSessionEvent] = Field(default_factory=list)
    has_more_before: bool = False


EventPostProcessor = Callable[[list[RawSessionEvent]], Awaitable[list[RawSessionEvent]]]


async def build_chat_snapshot(
    *,
    conversation: ConversationSummary,
    session_refs: Sequence[ConversationSessionRef],
    event_store: SessionEventStore,
    cursor_secret: str,
    queue: QueueState | None = None,
    state: ConversationStateView | None = None,
    runtime_input: RuntimeOverlayInput | None = None,
    event_post_processor: EventPostProcessor | None = None,
    now: datetime | None = None,
) -> ChatSnapshot:
    """Build an authoritative Chat v2 snapshot from current session events."""

    current_time = now or datetime.now(UTC)
    watermarks = await _read_high_watermarks(session_refs=session_refs, event_store=event_store)
    window = await _read_latest_window(
        session_refs=session_refs,
        event_store=event_store,
        limit=SNAPSHOT_WINDOW_EVENT_LIMIT,
    )
    if event_post_processor is not None:
        window = window.model_copy(update={"events": await event_post_processor(window.events)})
    hydrated_events = await _hydrate_window_pairings(
        list(window.events),
        session_refs=session_refs,
        event_store=event_store,
    )
    projected_window = _project_window(hydrated_events)
    timeline = projected_window.model_copy(
        update={
            "has_more_before": window.has_more_before,
            "before_cursor": _encode_before_cursor_for_items(
                conversation_id=conversation.conversation_id,
                session_refs=session_refs,
                items=projected_window.items,
                cursor_secret=cursor_secret,
                now=current_time,
            )
            if window.has_more_before
            else None,
        }
    )
    cursor = _encode_cursor(
        conversation_id=conversation.conversation_id,
        session_refs=session_refs,
        events=[],
        watermarks=watermarks,
        cursor_secret=cursor_secret,
        now=current_time,
    )
    return ChatSnapshot(
        projection_version=PROJECTION_VERSION,
        conversation=conversation,
        timeline=timeline,
        state=state or _empty_state(current_time),
        queue=queue or QueueState(messages=[], queued_count=0),
        runtime=_runtime_overlay(runtime_input, generated_at=current_time),
        cursor=cursor,
        server_time=current_time.isoformat(),
    )


async def build_chat_sync_response(
    *,
    conversation_id: str,
    cursor: str,
    session_refs: Sequence[ConversationSessionRef],
    event_store: SessionEventStore,
    cursor_secret: str,
    limit: int = SYNC_DEFAULT_LIMIT,
    conversation: ConversationSummary | None = None,
    queue: QueueState | None = None,
    state: ConversationStateView | None = None,
    runtime_input: RuntimeOverlayInput | None = None,
    event_post_processor: EventPostProcessor | None = None,
    now: datetime | None = None,
) -> ChatSyncResponse:
    """Build a conservative cursor-checked incremental sync response."""

    validated_limit = validate_sync_limit(limit)
    current_time = now or datetime.now(UTC)
    try:
        previous = validate_cursor(
            cursor,
            cursor_secret,
            conversation_id=conversation_id,
            projection_version=PROJECTION_VERSION,
            now=current_time,
        )
    except ChatCursorError as exc:
        if exc.code == "cursor_invalid":
            raise
        return _reset_sync_response(
            conversation_id=conversation_id,
            cursor_before=cursor,
            reason=_cursor_error_reason(exc),
            runtime_input=runtime_input,
            now=current_time,
        )

    if _cursor_lineage(previous) != _lineage_entries(session_refs):
        return _reset_sync_response(
            conversation_id=conversation_id,
            cursor_before=cursor,
            reason="lineage_changed",
            runtime_input=runtime_input,
            now=current_time,
        )

    raw_events, watermarks, range_too_large = await _read_events_after_cursor(
        session_refs=session_refs,
        event_store=event_store,
        previous=previous,
        limit=validated_limit + 1,
    )
    if range_too_large or len(raw_events) > validated_limit:
        return _reset_sync_response(
            conversation_id=conversation_id,
            cursor_before=cursor,
            reason="range_too_large",
            runtime_input=runtime_input,
            now=current_time,
        )
    if event_post_processor is not None:
        raw_events = await event_post_processor(raw_events)
    raw_events = await _hydrate_window_pairings(
        raw_events,
        session_refs=session_refs,
        event_store=event_store,
    )
    timeline = _project_window(raw_events)
    touched_items = timeline.items
    if len(touched_items) > validated_limit:
        return _reset_sync_response(
            conversation_id=conversation_id,
            cursor_before=cursor,
            reason="range_too_large",
            runtime_input=runtime_input,
            now=current_time,
        )
    cursor_after = _encode_cursor_for_watermarks(
        conversation_id=conversation_id,
        session_refs=session_refs,
        watermarks=watermarks,
        cursor_secret=cursor_secret,
        now=current_time,
        view_revision=_view_revision(watermarks),
    )

    ops: list[ChatViewOp] = [UpsertTimelineItemOp(item=item) for item in touched_items]
    if conversation is not None:
        ops.insert(0, ReplaceConversationOp(conversation=conversation))
    if state is not None:
        ops.append(ReplaceStateOp(state=state))
    if queue is not None:
        ops.append(ReplaceQueueOp(queue=queue))

    return ChatSyncResponse(
        projection_version=PROJECTION_VERSION,
        conversation_id=conversation_id,
        cursor_before=cursor,
        cursor_after=cursor_after,
        ops=ops,
        cycle_states=timeline.cycle_states,
        runtime=_runtime_overlay(runtime_input, generated_at=current_time),
        reset_required=False,
        has_more=False,
        server_time=current_time.isoformat(),
    )


async def build_timeline_backfill_response(
    *,
    conversation_id: str,
    before: str | None,
    session_refs: Sequence[ConversationSessionRef],
    event_store: SessionEventStore,
    cursor_secret: str,
    limit: int = BACKFILL_DEFAULT_LIMIT,
    event_post_processor: EventPostProcessor | None = None,
    now: datetime | None = None,
) -> TimelineBackfillResponse:
    """Build an older timeline page.

    Phase 4 keeps this intentionally simple and deterministic by projecting the
    available canonical window and slicing before a timeline item ID. Later
    phases can optimize the source reads without changing the public response.
    """

    validated_limit = validate_backfill_limit(limit)
    current_time = now or datetime.now(UTC)
    window = await _read_backfill_window(
        conversation_id=conversation_id,
        before=before,
        session_refs=session_refs,
        event_store=event_store,
        cursor_secret=cursor_secret,
        limit=validated_limit,
        now=current_time,
    )
    if event_post_processor is not None:
        window = window.model_copy(update={"events": await event_post_processor(window.events)})
    hydrated_events = await _hydrate_window_pairings(
        list(window.events),
        session_refs=session_refs,
        event_store=event_store,
    )
    page = _project_window(hydrated_events)
    page_items = page.items
    return TimelineBackfillResponse(
        projection_version=PROJECTION_VERSION,
        conversation_id=conversation_id,
        items=page_items,
        cycle_states=page.cycle_states,
        has_more_before=window.has_more_before,
        before_cursor=_encode_before_cursor_for_items(
            conversation_id=conversation_id,
            session_refs=session_refs,
            items=page_items,
            cursor_secret=cursor_secret,
            now=current_time,
        )
        if window.has_more_before
        else None,
        server_time=current_time.isoformat(),
    )


def validate_sync_limit(limit: int) -> int:
    return _validate_limit(limit, minimum=SYNC_MIN_LIMIT, maximum=SYNC_MAX_LIMIT)


def validate_backfill_limit(limit: int) -> int:
    return _validate_limit(limit, minimum=BACKFILL_MIN_LIMIT, maximum=BACKFILL_MAX_LIMIT)


def conversation_summary_from_row(row: Any) -> ConversationSummary:
    """Convert a store conversation row into a Chat v2 conversation summary."""

    return ConversationSummary(
        conversation_id=str(row.conversation_id),
        title=_str_or_none(getattr(row, "title", None)),
        agent_id=str(row.agent_id),
        agent_profile_id=_str_or_none(getattr(row, "agent_profile_id", None)),
        project_id=_str_or_none(getattr(row, "project_id", None)),
        status=str(getattr(row, "status", "active")),
        active_session_id=_str_or_none(getattr(row, "active_session_id", None)),
        last_message_at=_iso_or_none(getattr(row, "last_message_at", None)),
        last_read_at=_iso_or_none(getattr(row, "last_read_at", None)),
    )


def queue_state_from_messages(messages: Sequence[dict[str, Any]]) -> QueueState:
    """Convert scheduler queued-message snapshots into Chat v2 queue state."""

    queue_messages = [
        QueueMessage(
            queue_id=str(item.get("queue_id")),
            client_message_id=_str_or_none(item.get("client_message_id")),
            client_txn_id=_str_or_none(item.get("client_txn_id")),
            content=str(item.get("content") or ""),
            attachments=list(item.get("attachments") or []),
            position=int(item.get("position") or index),
            created_at=_str_or_none(item.get("created_at")),
            updated_at=_str_or_none(item.get("updated_at")),
        )
        for index, item in enumerate(messages, start=1)
        if item.get("queue_id")
    ]
    return QueueState(messages=queue_messages, queued_count=len(queue_messages))


def state_view_from_snapshot(
    snapshot: Any, *, now: datetime | None = None
) -> ConversationStateView:
    """Convert the existing conversation-state envelope into the v2 state view."""

    current_time = now or datetime.now(UTC)
    if snapshot is None:
        return _empty_state(current_time)
    if hasattr(snapshot, "model_dump"):
        raw = snapshot.model_dump(mode="json")
    elif isinstance(snapshot, dict):
        raw = snapshot
    else:
        raw = {}
    return ConversationStateView(
        state_version=int(raw.get("state_version") or 0),
        snapshot_generated_at=str(raw.get("snapshot_generated_at") or current_time.isoformat()),
        capabilities=list(raw.get("capabilities") or []),
        active_turn=dict(raw.get("active_turn") or {}),
        pending=dict(raw.get("pending") or {}),
        active_session=dict(raw.get("active_session") or {}),
        task=dict(raw["task"]) if isinstance(raw.get("task"), dict) else None,
    )


def runtime_input_from_scheduler(
    *,
    conversation_id: str,
    active_session_id: str | None,
    turn_scheduler: Any,
    session_cache: Any = None,
) -> RuntimeOverlayInput:
    """Build minimal runtime overlay input from the current turn scheduler."""

    context_usage = (
        session_cache.get_context_usage(active_session_id)
        if session_cache is not None
        and hasattr(session_cache, "get_context_usage")
        and active_session_id
        else None
    )
    if turn_scheduler is None:
        return RuntimeOverlayInput(
            runtime_epoch=runtime_epoch_for(conversation_id),
            runtime_revision=0,
            active_turn=None,
            context_usage=context_usage,
        )
    running = turn_scheduler.running_turn_state(conversation_id)
    checkpoint = turn_scheduler.active_turn_checkpoint(conversation_id)
    active_turn: dict[str, Any] | None = None
    if running is not None:
        active_turn = {
            "turn_id": (checkpoint or {}).get("turn_id") or f"active:{conversation_id}",
            "session_id": (checkpoint or {}).get("session_id") or active_session_id or "",
            "status": "running",
            "chat_mode": running.get("chat_mode"),
            "chat_mode_source": running.get("chat_mode_source"),
        }
    return RuntimeOverlayInput(
        runtime_epoch=runtime_epoch_for(conversation_id),
        runtime_revision=1 if active_turn is not None else 0,
        active_turn=active_turn,
        context_usage=context_usage,
    )


def _runtime_overlay(
    runtime_input: RuntimeOverlayInput | None,
    *,
    generated_at: datetime,
) -> RuntimeOverlaySnapshot:
    if runtime_input is None:
        runtime_input = RuntimeOverlayInput(
            runtime_epoch="none",
            runtime_revision=0,
            active_turn=None,
        )
    active_turn = None
    if runtime_input.active_turn is not None:
        active_turn = RuntimeActiveTurn.model_validate(runtime_input.active_turn)
    return RuntimeOverlaySnapshot(
        runtime_epoch=runtime_input.runtime_epoch,
        runtime_revision=runtime_input.runtime_revision,
        generated_at=generated_at.isoformat(),
        has_active_turn=active_turn is not None,
        active_turn=active_turn,
        volatile_items=[],
        context_usage=runtime_input.context_usage,
    )


async def _read_all_events(
    *,
    session_refs: Sequence[ConversationSessionRef],
    event_store: SessionEventStore,
) -> list[RawSessionEvent]:
    raw_events: list[RawSessionEvent] = []
    for ref in session_refs:
        after_seq = 0
        while True:
            page = await event_store.read_session_events(
                session_id=ref.event_store_session_id,
                after_seq=after_seq,
                limit=SNAPSHOT_SESSION_EVENT_LIMIT,
            )
            if not page.events and page.has_more_after:
                raise ChatV2SyncError(
                    "event_store_paging_failed",
                    "Event store reported more events without advancing the page",
                )
            for event in page.events:
                raw_events.append(
                    event.model_copy(
                        update={
                            "store_id": ref.store,
                            "data": {
                                **event.data,
                                "_lineage_index": ref.ordinal,
                                "cognis_session_id": ref.session_id,
                            },
                        }
                    )
                )
            if not page.has_more_after:
                break
            next_after_seq = page.events[-1].seq if page.events else after_seq
            if next_after_seq <= after_seq:
                raise ChatV2SyncError(
                    "event_store_paging_failed",
                    "Event store page did not advance",
                )
            after_seq = next_after_seq
    return raw_events


async def _read_high_watermarks(
    *,
    session_refs: Sequence[ConversationSessionRef],
    event_store: SessionEventStore,
) -> dict[tuple[str, str], int]:
    watermarks: dict[tuple[str, str], int] = {}
    for ref in session_refs:
        watermark = await event_store.read_session_high_watermark(
            session_id=ref.event_store_session_id
        )
        watermarks[(ref.store, ref.event_store_session_id)] = watermark.last_seq
    return watermarks


async def _read_latest_window(
    *,
    session_refs: Sequence[ConversationSessionRef],
    event_store: SessionEventStore,
    limit: int,
) -> _EventWindow:
    raw_events: list[RawSessionEvent] = []
    remaining = limit
    has_more_before = False

    for lineage_index in range(len(session_refs) - 1, -1, -1):
        if remaining <= 0:
            has_more_before = lineage_index >= 0
            break
        ref = session_refs[lineage_index]
        page = await event_store.read_session_events(
            session_id=ref.event_store_session_id,
            limit=remaining,
            direction="backward",
        )
        tagged = _tag_events(page.events, ref)
        raw_events.extend(tagged)
        remaining -= len(tagged)
        if page.has_more_before:
            has_more_before = True
            break
        if remaining <= 0 and lineage_index > 0:
            has_more_before = True

    return _EventWindow(events=_sort_raw_events(raw_events), has_more_before=has_more_before)


async def _read_events_after_cursor(
    *,
    session_refs: Sequence[ConversationSessionRef],
    event_store: SessionEventStore,
    previous: InternalChatCursorPayload,
    limit: int,
) -> tuple[list[RawSessionEvent], dict[tuple[str, str], int], bool]:
    previous_watermarks = {
        (watermark.store, watermark.session_id): watermark.last_seq
        for watermark in previous.session_watermarks
    }
    current_watermarks = await _read_high_watermarks(
        session_refs=session_refs,
        event_store=event_store,
    )
    raw_events: list[RawSessionEvent] = []
    remaining = limit
    range_too_large = False
    for ref in session_refs:
        if remaining <= 0:
            range_too_large = True
            break
        key = (ref.store, ref.event_store_session_id)
        after_seq = previous_watermarks.get(key, 0)
        if current_watermarks.get(key, 0) <= after_seq:
            continue
        page = await event_store.read_session_events(
            session_id=ref.event_store_session_id,
            after_seq=after_seq,
            limit=remaining,
        )
        raw_events.extend(_tag_events(page.events, ref))
        remaining -= len(page.events)
        if page.has_more_after:
            range_too_large = True
            break
        if remaining <= 0:
            range_too_large = True
            break
    return _sort_raw_events(raw_events), current_watermarks, range_too_large


async def _read_backfill_window(
    *,
    conversation_id: str,
    before: str | None,
    session_refs: Sequence[ConversationSessionRef],
    event_store: SessionEventStore,
    cursor_secret: str,
    limit: int,
    now: datetime,
) -> _EventWindow:
    if before is None:
        return await _read_latest_window(
            session_refs=session_refs,
            event_store=event_store,
            limit=limit,
        )

    try:
        payload = validate_cursor(
            before,
            cursor_secret,
            conversation_id=conversation_id,
            projection_version=PROJECTION_VERSION,
            now=now,
        )
    except ChatCursorError as exc:
        raise ChatV2SyncError("invalid_before_cursor", str(exc)) from exc
    if _cursor_lineage(payload) != _lineage_entries(session_refs):
        raise ChatV2SyncError("lineage_changed", "Timeline cursor lineage no longer matches")
    if len(payload.session_watermarks) != 1:
        raise ChatV2SyncError("invalid_before_cursor", "Timeline cursor has invalid shape")
    target = payload.session_watermarks[0]
    target_index = next(
        (
            index
            for index, ref in enumerate(session_refs)
            if ref.store == target.store and ref.event_store_session_id == target.session_id
        ),
        None,
    )
    if target_index is None:
        raise ChatV2SyncError("invalid_before_cursor", "Timeline cursor session is unknown")

    raw_events: list[RawSessionEvent] = []
    remaining = limit
    has_more_before = False
    for lineage_index in range(target_index, -1, -1):
        if remaining <= 0:
            has_more_before = lineage_index >= 0
            break
        ref = session_refs[lineage_index]
        before_seq = target.last_seq if lineage_index == target_index else None
        page = await event_store.read_session_events(
            session_id=ref.event_store_session_id,
            before_seq=before_seq,
            limit=remaining,
            direction="backward",
        )
        events = page.events
        if before_seq is not None:
            events = [event for event in events if event.seq < before_seq]
        tagged = _tag_events(events, ref)
        raw_events.extend(tagged)
        remaining -= len(tagged)
        if page.has_more_before:
            has_more_before = True
            break
        if remaining <= 0 and lineage_index > 0:
            has_more_before = True

    return _EventWindow(events=_sort_raw_events(raw_events), has_more_before=has_more_before)


def _tag_events(
    events: Sequence[RawSessionEvent],
    ref: ConversationSessionRef,
) -> list[RawSessionEvent]:
    return [
        event.model_copy(
            update={
                "store_id": ref.store,
                "data": {
                    **event.data,
                    "_lineage_index": ref.ordinal,
                    "cognis_session_id": ref.session_id,
                },
            }
        )
        for event in events
    ]


def _sort_raw_events(events: Sequence[RawSessionEvent]) -> list[RawSessionEvent]:
    return sorted(
        events,
        key=lambda event: (
            int(event.data.get("_lineage_index") or 0),
            event.seq,
            event.event_id or "",
        ),
    )


def _project_window(raw_events: Sequence[RawSessionEvent]) -> TimelineWindow:
    normalization = normalize_session_events(raw_events)
    projection = project_timeline(normalization.events)
    return projection.timeline


# Bounded backward read used to resolve tool_call events whose pairing
# (tool_result / delegation / evaluation) landed in a later read window.
PAIRING_HYDRATION_LOOKBACK_LIMIT = 400


def _pairing_call_id(event: RawSessionEvent) -> str | None:
    """Return the tool call id a window event pairs with, if any."""

    data = event.data
    if event.type in {"tool_result", "delegation"}:
        value = data.get("call_id")
    elif event.type == "evaluation":
        value = (
            data.get("tool_call_id")
            or data.get("source_tool_call_id")
            or data.get("evaluated_tool_call_id")
            or data.get("call_id")
        )
    else:
        return None
    if value is None:
        return None
    text = str(value).strip()
    return text or None


async def _hydrate_window_pairings(
    raw_events: list[RawSessionEvent],
    *,
    session_refs: Sequence[ConversationSessionRef],
    event_store: SessionEventStore,
    lookback_limit: int = PAIRING_HYDRATION_LOOKBACK_LIMIT,
) -> list[RawSessionEvent]:
    """Backfill tool_call events whose pairing fell outside the read window.

    Tool events are flushed incrementally per boundary, so a sync window can
    contain a ``tool_result``/``delegation``/``evaluation`` event whose
    ``tool_call`` was delivered in an earlier window. Projecting such a
    partial window degrades the tool item: it re-anchors to the RESULT's seq
    (jumping past thinking/text persisted in between), loses its name and
    arguments, emits a duplicate standalone delegation card, and swallows the
    evaluation sidecar. Reading the missing tool_call events back into the
    projection input keeps window projections self-contained.
    """

    if not raw_events:
        return list(raw_events)
    in_window_call_ids = {
        str(event.data.get("call_id") or "").strip()
        for event in raw_events
        if event.type == "tool_call" and event.data.get("call_id")
    }
    orphan_call_ids: dict[tuple[str, str], set[str]] = {}
    min_referencing_seq: dict[tuple[str, str], int] = {}
    for event in raw_events:
        call_id = _pairing_call_id(event)
        if not call_id or call_id in in_window_call_ids:
            continue
        key = (event.store_id, event.session_id)
        orphan_call_ids.setdefault(key, set()).add(call_id)
        current_min = min_referencing_seq.get(key)
        if current_min is None or event.seq < current_min:
            min_referencing_seq[key] = event.seq
    if not orphan_call_ids:
        return list(raw_events)

    refs_by_key = {(ref.store, ref.event_store_session_id): ref for ref in session_refs}
    hydrated: list[RawSessionEvent] = []
    for key, call_ids in orphan_call_ids.items():
        ref = refs_by_key.get(key)
        boundary_seq = min_referencing_seq.get(key)
        if ref is None or boundary_seq is None or boundary_seq <= 1:
            continue
        try:
            page = await event_store.read_session_events(
                session_id=ref.event_store_session_id,
                before_seq=boundary_seq,
                limit=lookback_limit,
                direction="backward",
            )
        except Exception:
            logger.warning(
                "chat_v2: pairing hydration read failed",
                extra={"extra_data": {"session_id": ref.session_id}},
                exc_info=True,
            )
            continue
        remaining = set(call_ids)
        for candidate in page.events:
            if not remaining:
                break
            if candidate.seq >= boundary_seq or candidate.type != "tool_call":
                continue
            candidate_call_id = str(candidate.data.get("call_id") or "").strip()
            if candidate_call_id in remaining:
                hydrated.extend(_tag_events([candidate], ref))
                remaining.discard(candidate_call_id)
    if not hydrated:
        return list(raw_events)
    return _sort_raw_events([*hydrated, *raw_events])


def _encode_cursor(
    *,
    conversation_id: str,
    session_refs: Sequence[ConversationSessionRef],
    events: Sequence[RawSessionEvent],
    watermarks: dict[tuple[str, str], int] | None = None,
    cursor_secret: str,
    now: datetime,
    view_revision: int | None = None,
) -> str:
    watermarks = watermarks if watermarks is not None else _event_watermarks(events)
    payload = InternalChatCursorPayload(
        conversation_id=conversation_id,
        projection_version=PROJECTION_VERSION,
        session_watermarks=[
            CursorSessionWatermark(store=store, session_id=session_id, last_seq=last_seq)
            for (store, session_id), last_seq in sorted(watermarks.items())
        ],
        lineage=_lineage_entries(session_refs),
        view_revision=view_revision if view_revision is not None else _view_revision(watermarks),
        issued_at=now.isoformat(),
        expires_at=(now + CURSOR_TTL).isoformat(),
    )
    return encode_cursor(payload, cursor_secret)


def _encode_before_cursor_for_items(
    *,
    conversation_id: str,
    session_refs: Sequence[ConversationSessionRef],
    items: Sequence[TimelineItem],
    cursor_secret: str,
    now: datetime,
) -> str | None:
    source_ref = _earliest_item_source_ref(items, session_refs=session_refs)
    if source_ref is None:
        return None
    payload = InternalChatCursorPayload(
        conversation_id=conversation_id,
        projection_version=PROJECTION_VERSION,
        session_watermarks=[
            CursorSessionWatermark(
                store=source_ref.store,
                session_id=source_ref.session_id,
                last_seq=source_ref.seq,
            )
        ],
        lineage=_lineage_entries(session_refs),
        view_revision=source_ref.seq,
        issued_at=now.isoformat(),
        expires_at=(now + CURSOR_TTL).isoformat(),
    )
    return encode_cursor(payload, cursor_secret)


def _earliest_item_source_ref(
    items: Sequence[TimelineItem],
    *,
    session_refs: Sequence[ConversationSessionRef],
) -> Any | None:
    lineage_ordinals = {
        (ref.store, ref.event_store_session_id): ref.ordinal for ref in session_refs
    }
    refs = [ref for item in items for ref in item.source_refs]
    if not refs:
        return None
    return min(
        refs,
        key=lambda ref: (lineage_ordinals.get((ref.store, ref.session_id), 0), ref.seq),
    )


def _encode_cursor_for_watermarks(
    *,
    conversation_id: str,
    session_refs: Sequence[ConversationSessionRef],
    watermarks: dict[tuple[str, str], int],
    cursor_secret: str,
    now: datetime,
    view_revision: int,
) -> str:
    payload = InternalChatCursorPayload(
        conversation_id=conversation_id,
        projection_version=PROJECTION_VERSION,
        session_watermarks=[
            CursorSessionWatermark(store=store, session_id=session_id, last_seq=last_seq)
            for (store, session_id), last_seq in sorted(watermarks.items())
        ],
        lineage=_lineage_entries(session_refs),
        view_revision=view_revision,
        issued_at=now.isoformat(),
        expires_at=(now + CURSOR_TTL).isoformat(),
    )
    return encode_cursor(payload, cursor_secret)


def _event_watermarks(events: Sequence[RawSessionEvent]) -> dict[tuple[str, str], int]:
    watermarks: dict[tuple[str, str], int] = {}
    for event in events:
        key = (event.store_id, event.session_id)
        watermarks[key] = max(watermarks.get(key, 0), event.seq)
    return watermarks


def _view_revision(watermarks: dict[tuple[str, str], int]) -> int:
    return sum(watermarks.values())


def _lineage_entries(session_refs: Sequence[ConversationSessionRef]) -> list[CursorLineageEntry]:
    return [
        CursorLineageEntry(
            store=ref.store,
            session_id=ref.event_store_session_id,
            role=ref.role,
            ordinal=ref.ordinal,
        )
        for ref in session_refs
    ]


def _cursor_lineage(payload: InternalChatCursorPayload) -> list[CursorLineageEntry]:
    return payload.lineage


def _reset_sync_response(
    *,
    conversation_id: str,
    cursor_before: str,
    reason: ChatResetReason,
    runtime_input: RuntimeOverlayInput | None,
    now: datetime,
) -> ChatSyncResponse:
    runtime = _runtime_overlay(runtime_input, generated_at=now)
    return ChatSyncResponse(
        projection_version=PROJECTION_VERSION,
        conversation_id=conversation_id,
        cursor_before=cursor_before,
        cursor_after=cursor_before,
        ops=[],
        cycle_states=runtime.cycle_states,
        runtime=runtime,
        reset_required=True,
        reset_reason=reason,
        has_more=False,
        server_time=now.isoformat(),
    )


def _cursor_error_reason(error: ChatCursorError) -> ChatResetReason:
    if error.code == "cursor_expired":
        return "cursor_expired"
    if error.code == "projection_version_changed":
        return "projection_version_changed"
    if error.code == "unsupported_cursor":
        return "unsupported_cursor"
    return "cursor_invalid"


def _find_before_index(items: Sequence[TimelineItem], before: str | None) -> int:
    if before is None:
        return len(items)
    for index, item in enumerate(items):
        if item.id == before or item.sort_key == before:
            return index
    raise ChatV2SyncError("invalid_before_cursor", "Unknown timeline before cursor")


def _validate_limit(limit: int, *, minimum: int, maximum: int) -> int:
    if limit < minimum or limit > maximum:
        raise ChatV2SyncError(
            "invalid_limit",
            f"limit must be between {minimum} and {maximum}",
        )
    return limit


def _empty_state(now: datetime) -> ConversationStateView:
    return ConversationStateView(
        state_version=0,
        snapshot_generated_at=now.isoformat(),
        capabilities=[],
        active_turn={},
        pending={},
        active_session={},
        task=None,
    )


def _iso_or_none(value: Any) -> str | None:
    if isinstance(value, datetime):
        return value.isoformat()
    return value if isinstance(value, str) and value else None


def _str_or_none(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None
