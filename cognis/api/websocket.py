"""WebSocket transport layer for real-time chat streaming.

This module is a **thin transport adapter** — it handles WebSocket
connection lifecycle, authentication, message framing, backpressure,
and event fanout. All turn orchestration, command dispatch, and
business logic live in the core layer:

- ``cognis.core.turn_scheduler.TurnScheduler`` — turn execution
- ``cognis.core.commands.CommandDispatcher`` — slash commands

The WebSocket manager implements ``TurnObserver`` to receive streaming
callbacks from the TurnScheduler and forward them to connected clients.
"""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import json
import os
import uuid
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, cast

from fastapi import WebSocket, WebSocketDisconnect
from prometheus_client import Counter, Gauge
from sqlalchemy import select

from cognis.api.chat_v2.cursors import ChatCursorError, validate_cursor
from cognis.api.chat_v2.event_store import RawSessionEvent
from cognis.api.chat_v2.normalizer import normalize_session_events
from cognis.api.chat_v2.projector import project_timeline
from cognis.api.chat_v2.realtime import (
    assistant_completion_runtime_item,
    compaction_runtime_item,
    delegation_runtime_item,
    runtime_frame,
    runtime_items_from_snapshots,
    runtime_overlay_from_items,
    scope_accepts_runtime,
    system_message_runtime_item,
    tool_call_runtime_item,
    tool_result_runtime_item,
)
from cognis.api.chat_v2.schemas import TimelineItem, TimelineScope
from cognis.api.chat_v2.sync import current_projection_version
from cognis.api.models import (
    WebSocketAuthenticated,
    WebSocketChunkGap,
    WebSocketError,
    WebSocketPong,
)
from cognis.api.serializers import conversation_to_response
from cognis.api.timeline_visibility import (
    is_transient_compaction_start_notice,
    is_visible_persisted_system_message,
)
from cognis.api.view_state import cognis_build_id, runtime_generation, server_time_iso
from cognis.core.attachment_utils import hydrate_attachment_refs, strip_attachment_payload_bytes
from cognis.core.chat_v2_runtime_relay import (
    AdmissionDecision,
    ChatV2RuntimeRelayEnvelope,
    RelayKind,
)
from cognis.core.command_notices import persist_command_system_notice
from cognis.core.conversation_state import (
    build_state_delta,
    linked_conversation_ids_for_task,
    snapshot_for_conversation,
)
from cognis.core.events import Event, EventType
from cognis.core.notification_resolution import build_auth_challenge_resolution_data
from cognis.core.question_sets import validate_reply_for_questions
from cognis.core.turn_scheduler import (
    SessionCreationFailedError as SessionCreationFailedError,  # noqa: F401 — re-export
)
from cognis.core.turn_scheduler import (
    TurnError,
    TurnResult,
)
from cognis.logging import get_logger
from cognis.models.agent import AgentDefinition
from cognis.providers.circuit_breaker import CircuitBreakerError
from cognis.runtime_context import current_user_email
from cognis.store.models import Conversation, ExecutorRow, NotificationRow, Session, Task
from cognis.store.queries import (
    get_browser_session_by_token,
    get_conversation,
    get_task,
    get_user,
    list_pending_notification_types_by_conversation,
    mark_artifacts_attached,
)

logger = get_logger(__name__)


async def _scheduler_queued_messages(
    turn_scheduler: Any,
    conversation_id: str,
) -> list[dict[str, Any]]:
    durable_reader = getattr(turn_scheduler, "get_queued_messages", None)
    if callable(durable_reader):
        return list(await durable_reader(conversation_id))
    return list(turn_scheduler.queued_messages(conversation_id))


def _assistant_runtime_payload(data: dict[str, Any]) -> dict[str, Any] | None:
    """Return persisted assistant runtime metadata when it has the expected shape."""

    runtime = data.get("runtime")
    return runtime if isinstance(runtime, dict) else None


_NEW_SESSION_STREAM_GRACE = timedelta(seconds=30)
_MANAGED_CONVERSATION_CONTEXT_TYPES = {"agent_work", "managed_agent_conversation"}


# ---------------------------------------------------------------------------
# Prometheus metrics (transport-specific)
# ---------------------------------------------------------------------------

WS_CONNECTIONS_ACTIVE = Gauge("cognis_ws_connections_active", "Active WebSocket connections")
WS_CONNECTIONS_TOTAL = Counter("cognis_ws_connections_total", "Total WebSocket connections")
WS_RECONNECTIONS_TOTAL = Counter("cognis_ws_reconnections_total", "Total WebSocket reconnects")
WS_MISSED_EVENTS_REPLAYED = Counter(
    "cognis_ws_missed_events_replayed",
    "Missed events replayed over WebSocket",
)
WS_CHUNK_GAP_FRAMES_TOTAL = Counter(
    "cognis_ws_chunk_gap_frames_total",
    "Chunk gap frames emitted due to dropped streaming chunks",
)


def _positive_int_env(name: str, default: int) -> int:
    value = os.getenv(name)
    if value is None:
        return default
    try:
        parsed = int(value)
    except ValueError:
        return default
    return parsed if parsed > 0 else default


def _positive_float_env(name: str, default: float) -> float:
    value = os.getenv(name)
    if value is None:
        return default
    try:
        parsed = float(value)
    except ValueError:
        return default
    return parsed if parsed > 0 else default


DEFAULT_INBOUND_RATE_LIMIT = _positive_int_env("COGNIS_WS_INBOUND_RATE_LIMIT", 60)
DEFAULT_OUTBOUND_BUFFER = 100
DEFAULT_SEND_TIMEOUT_SECONDS = _positive_float_env("COGNIS_WS_SEND_TIMEOUT_SECONDS", 15.0)
DEFAULT_REPLAY_LIMIT = 200
COOKIE_NAME = "cognis_session"


# Memoized phase-hint projections keyed by session. The hint computation runs
# normalize+project over the ENTIRE cached event list and used to execute per
# token/thinking delta on the event loop — O(session events) CPU per token on
# long sessions, delaying frame delivery. Cached events only change at flush
# boundaries, so tokens between flushes hit the memo.
_PHASE_HINT_MEMO_MAX_SESSIONS = 256
_phase_hint_memo: dict[tuple[str, str | None], tuple[tuple[int, int, int], list[TimelineItem]]] = {}


@dataclass
class _ChatV2CoalescePending:
    """Dirty runtime sources to rebuild once per coalescer flush window."""

    active_session_id: str | None = None
    turn_id: str | None = None
    include_streams: bool = False
    include_thinking: bool = False
    include_tool_outputs: bool = False


def _runtime_relay_cumulative_boundary(items: list[TimelineItem], *, has_active_turn: bool) -> bool:
    """Return whether a runtime relay frame must retain retry ordering."""

    if not has_active_turn:
        return True
    return any(
        (
            item.kind
            in {
                "tool_call",
                "delegation",
                "managed_conversation",
                "compaction",
            }
            and not (
                item.kind == "tool_call"
                and getattr(item, "tool_name", None) == "apply_patch"
                and getattr(item, "progress_phase", None) == "preparing_input"
                and not getattr(item, "progress_complete", False)
                and not getattr(item, "arguments", None)
                and not getattr(item, "result_preview", None)
                and not getattr(item, "file_diffs", None)
            )
        )
        or getattr(item, "status", None) in {"completed", "failed", "cancelled", "compacted"}
        for item in items
    )


def _invalidate_chat_v2_phase_hint_items(
    session_id: str | None,
    turn_id: str | None = None,
) -> None:
    if not session_id:
        return
    if turn_id is None:
        for key in [key for key in _phase_hint_memo if key[0] == session_id]:
            _phase_hint_memo.pop(key, None)
        return
    _phase_hint_memo.pop((session_id, turn_id), None)


def _chat_v2_phase_hint_items_from_session_cache(
    session_cache: Any,
    session_id: str | None,
    turn_id: str | None = None,
) -> list[TimelineItem]:
    """Return canonical Chat v2 items from cached active-session events for phase fallback."""

    if session_cache is None or not session_id:
        return []
    if not hasattr(session_cache, "get_events_since_compaction"):
        return []
    try:
        cached = session_cache.get_events_since_compaction(session_id)
        if not cached:
            return []
        memo_scope = (session_id, turn_id)
        memo_key = (id(cached), cached[-1].seq, len(cached))
        memoized = _phase_hint_memo.get(memo_scope)
        if memoized is not None and memoized[0] == memo_key:
            return memoized[1]
        raw_events = [
            RawSessionEvent(
                store_id="runtime-cache",
                session_id=session_id,
                seq=ev.seq,
                type=ev.type,
                data=ev.data,
                timestamp=ev.ts if isinstance(ev.ts, datetime) else None,
            )
            for ev in cached
        ]
        normalized = normalize_session_events(raw_events)
        items = list(project_timeline(normalized.events).timeline.items)
        if len(_phase_hint_memo) >= _PHASE_HINT_MEMO_MAX_SESSIONS:
            _phase_hint_memo.pop(next(iter(_phase_hint_memo)), None)
        _phase_hint_memo[memo_scope] = (memo_key, items)
        return items
    except Exception:  # noqa: BLE001
        return []


def _chat_v2_delegation_runtime_item(event: Event) -> TimelineItem | None:
    """Fold live delegation bus events onto the parent delegate tool card.

    The legacy timeline patch for a single delegation event cannot fold onto the
    original tool call because that projector invocation sees only the delegation
    event. Chat v2 therefore gets a small runtime tool_call overlay keyed by the
    original delegate call_id; the frontend merge keeps the canonical arguments
    and overlays child-session progress/result details.
    """

    if event.type not in {
        EventType.DELEGATION_STARTED,
        EventType.DELEGATION_PROGRESS,
        EventType.DELEGATION_COMPLETED,
        EventType.DELEGATION_FAILED,
    }:
        return None
    data = dict(event.data)
    call_id = data.get("call_id")
    if not isinstance(call_id, str) or not call_id:
        return None
    status = data.get("status")
    if event.type == EventType.DELEGATION_STARTED:
        status = status or "started"
    elif event.type == EventType.DELEGATION_PROGRESS:
        status = status or "running"
    elif event.type == EventType.DELEGATION_COMPLETED:
        status = "completed"
    elif event.type == EventType.DELEGATION_FAILED:
        status = "failed"
        data = {**data, "error": data.get("error") or data.get("reason")}
    data["status"] = status
    timestamp = event.timestamp.isoformat() if event.timestamp else datetime.now(UTC).isoformat()
    return delegation_runtime_item(data, timestamp=timestamp)


def _workflow_composed_payload(conversation_id: str, data: dict[str, Any]) -> dict[str, Any]:
    """Build the WebSocket payload for a composed workflow event."""

    return {
        "type": "workflow_composed",
        "conversation_id": conversation_id,
        "task_id": data.get("task_id"),
        "schedule_id": data.get("schedule_id"),
        "workflow_id": data.get("workflow_id"),
        "workflow_name": data.get("workflow_name") or data.get("workflow_id"),
        "lifecycle": data.get("lifecycle", "ephemeral"),
        "steps": data.get("steps") or [],
    }


# ---------------------------------------------------------------------------
# AuthenticatedWebSocket
# ---------------------------------------------------------------------------


@dataclass
class _OutboundFrame:
    """A serialized or JSON websocket frame queued for one connection."""

    msg_type: str
    message_id: str | None
    conversation_id: str
    droppable: bool
    payload: dict[str, Any] | None = None
    text: str | None = None


_DROPPABLE_TYPES = frozenset({"chunk", "assistant_thinking_chunk"})
_CHAT_V2_CURSOR_PLACEHOLDER = "__cognis_chat_v2_cursor__"


def _json_dumps_frame(payload: dict[str, Any]) -> str:
    """Match Starlette's compact websocket JSON encoding for serialized fanout."""

    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def _parse_iso_datetime(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=UTC)
        return parsed.astimezone(UTC)
    except ValueError:
        return None


def _has_unread_from_payload(payload: dict[str, Any]) -> bool:
    last_message_at = _parse_iso_datetime(payload.get("last_message_at"))
    if last_message_at is None:
        return False
    last_read_at = _parse_iso_datetime(payload.get("last_read_at"))
    return last_read_at is None or last_message_at > last_read_at


@dataclass
class AuthenticatedWebSocket:
    """A WebSocket connection with authenticated user identity."""

    connection_id: str
    websocket: WebSocket
    user_email: str
    role: str
    subscriptions: set[str] = field(default_factory=set)
    recent_message_times: Any = field(default_factory=lambda: __import__("collections").deque())
    dropped_chunks: dict[str, int] = field(default_factory=dict)
    recovery_notified: set[str] = field(default_factory=set)
    chat_v2_cursors: dict[str, str] = field(default_factory=dict)
    chat_v2_scopes: dict[str, TimelineScope] = field(default_factory=dict)
    # Voice mode (conversation overlay). Populated by `enable_tts`/
    # `disable_tts` inbound frames; consumed by `WebSocketTurnObserver`
    # to gate `tts_sentence_ready` emission.
    tts_enabled: bool = False
    tts_voice: str | None = None
    send_timeout_seconds: float = DEFAULT_SEND_TIMEOUT_SECONDS
    _outbound_queue: asyncio.Queue[_OutboundFrame] = field(init=False)
    _writer_task: asyncio.Task[None] | None = field(default=None, init=False)
    _enqueue_tail: asyncio.Task[None] | None = field(default=None, init=False)
    _closed: bool = field(default=False, init=False)

    def __post_init__(self) -> None:
        self._outbound_queue = asyncio.Queue(maxsize=DEFAULT_OUTBOUND_BUFFER)

    def allow_inbound_message(self) -> bool:
        """Rate-limit inbound messages."""
        now = asyncio.get_running_loop().time()
        while self.recent_message_times and now - self.recent_message_times[0] > 1.0:
            self.recent_message_times.popleft()
        if len(self.recent_message_times) >= DEFAULT_INBOUND_RATE_LIMIT:
            return False
        self.recent_message_times.append(now)
        return True

    async def send_json(self, data: dict[str, Any]) -> None:
        """Enqueue a JSON frame for this connection's writer task."""

        await self._enqueue_payload(data, block=True)
        await asyncio.sleep(0)

    def send_scope_invalidation_nowait(self, data: dict[str, Any]) -> bool:
        """Coalesce and enqueue a droppable scope wakeup without blocking."""
        conversation_id = data.get("conversation_id")
        scope_key = self._scope_invalidation_key(data)
        queue = self._outbound_queue._queue  # noqa: SLF001
        for frame in reversed(queue):
            if (
                frame.msg_type == "scope_invalidated"
                and frame.message_id == scope_key
                and frame.payload is not None
            ):
                frame.payload = data
                return True
        frame = _OutboundFrame(
            msg_type="scope_invalidated",
            message_id=scope_key,
            conversation_id=str(conversation_id or ""),
            droppable=True,
            payload=data,
        )
        self._ensure_writer()
        return self._put_frame_nowait(frame)

    @staticmethod
    def _scope_invalidation_key(data: dict[str, Any]) -> str:
        conversation_id = data.get("conversation_id")
        return (
            f"{data.get('reason')}:{conversation_id}"
            if isinstance(conversation_id, str)
            else (f"{data.get('reason')}:{data.get('task_id') or data.get('step_run_id') or ''}")
        )

    async def send_text(
        self,
        text: str,
        *,
        msg_type: str,
        message_id: str | None,
        conversation_id: str,
        block: bool = True,
    ) -> None:
        """Enqueue a pre-serialized JSON text frame for this connection."""

        await self._enqueue_frame(
            _OutboundFrame(
                msg_type=msg_type,
                message_id=message_id,
                conversation_id=conversation_id,
                droppable=msg_type in _DROPPABLE_TYPES,
                text=text,
            ),
            block=block,
        )
        await asyncio.sleep(0)

    async def close(self) -> None:
        """Cancel writer/enqueue tasks and drain queued frames for disconnect."""

        self._closed = True
        if self._enqueue_tail is not None and not self._enqueue_tail.done():
            self._enqueue_tail.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._enqueue_tail
        self._enqueue_tail = None
        if self._writer_task is not None and not self._writer_task.done():
            self._writer_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._writer_task
        self._writer_task = None
        self._drain_outbound_queue()

    async def wait_outbound_drained(self) -> None:
        """Wait until all currently queued outbound frames are written."""

        await self._outbound_queue.join()
        await asyncio.sleep(0)

    async def _enqueue_payload(self, data: dict[str, Any], *, block: bool) -> None:
        msg_type = data.get("type")
        message_id = data.get("message_id")
        conversation_id = data.get("conversation_id")
        await self._enqueue_frame(
            _OutboundFrame(
                msg_type=msg_type if isinstance(msg_type, str) else "",
                message_id=message_id if isinstance(message_id, str) else None,
                conversation_id=conversation_id if isinstance(conversation_id, str) else "",
                droppable=msg_type in _DROPPABLE_TYPES,
                payload=data,
            ),
            block=block,
        )

    async def _enqueue_frame(self, frame: _OutboundFrame, *, block: bool) -> None:
        if self._closed:
            return
        self._ensure_writer()
        tail = self._enqueue_tail
        if tail is not None and not tail.done():
            if block:
                with contextlib.suppress(asyncio.CancelledError):
                    await tail
            else:
                self._enqueue_tail = asyncio.create_task(self._enqueue_after_tail(tail, [frame]))
                return
        if frame.droppable:
            self._enqueue_droppable_nowait(frame)
            return
        await self._enqueue_non_droppable(frame, block=block)

    async def _enqueue_after_tail(
        self, previous: asyncio.Task[None], frames: list[_OutboundFrame]
    ) -> None:
        with contextlib.suppress(asyncio.CancelledError):
            await previous
        for frame in frames:
            if frame.droppable:
                self._enqueue_droppable_nowait(frame)
            else:
                await self._enqueue_non_droppable(frame, block=True)

    async def _enqueue_non_droppable(self, frame: _OutboundFrame, *, block: bool) -> None:
        frames = [*self._gap_frames_for(frame), frame]
        if block:
            for item in frames:
                await self._put_frame_blocking(item)
            return

        remaining: list[_OutboundFrame] = []
        for index, item in enumerate(frames):
            if self._put_frame_nowait(item):
                continue
            remaining = frames[index:]
            break
        if remaining:
            previous = self._enqueue_tail
            if previous is not None and not previous.done():
                self._enqueue_tail = asyncio.create_task(
                    self._enqueue_after_tail(previous, remaining)
                )
            else:
                self._enqueue_tail = asyncio.create_task(self._enqueue_frames_blocking(remaining))

    async def _enqueue_frames_blocking(self, frames: list[_OutboundFrame]) -> None:
        for frame in frames:
            if frame.droppable:
                self._enqueue_droppable_nowait(frame)
            else:
                await self._enqueue_non_droppable(frame, block=True)

    def _gap_frames_for(self, frame: _OutboundFrame) -> list[_OutboundFrame]:
        if frame.message_id is None:
            return []
        gap_count = self.dropped_chunks.pop(frame.message_id, 0)
        if gap_count <= 0:
            return []
        gap_payload = WebSocketChunkGap(
            conversation_id=frame.conversation_id,
            message_id=frame.message_id,
            dropped_count=gap_count,
        ).model_dump()
        WS_CHUNK_GAP_FRAMES_TOTAL.inc()
        return [
            _OutboundFrame(
                msg_type="chunk_gap",
                message_id=frame.message_id,
                conversation_id=frame.conversation_id,
                droppable=False,
                payload=gap_payload,
            )
        ]

    def _enqueue_droppable_nowait(self, frame: _OutboundFrame) -> None:
        if self._put_frame_nowait(frame):
            return
        self._record_dropped_frame(frame)

    async def _put_frame_blocking(self, frame: _OutboundFrame) -> None:
        while not self._closed:
            if self._put_frame_nowait(frame):
                return
            try:
                await asyncio.wait_for(self._outbound_queue.put(frame), timeout=0.1)
            except TimeoutError:
                continue
            if self._closed:
                queue_items = self._outbound_queue._queue  # type: ignore[attr-defined]  # noqa: SLF001
                with contextlib.suppress(ValueError):
                    queue_items.remove(frame)
                    self._outbound_queue.task_done()
            return

    def _put_frame_nowait(self, frame: _OutboundFrame) -> bool:
        while not self._closed:
            try:
                self._outbound_queue.put_nowait(frame)
                return True
            except asyncio.QueueFull:
                if not self._drop_oldest_droppable():
                    return False
        return False

    def _drop_oldest_droppable(self) -> bool:
        queue_items = self._outbound_queue._queue  # type: ignore[attr-defined]  # noqa: SLF001
        for queued in list(queue_items):
            if queued.droppable:
                queue_items.remove(queued)
                self._outbound_queue.task_done()
                self._record_dropped_frame(queued)
                return True
        return False

    def _record_dropped_frame(self, frame: _OutboundFrame) -> None:
        if frame.message_id:
            self.dropped_chunks[frame.message_id] = self.dropped_chunks.get(frame.message_id, 0) + 1

    def _ensure_writer(self) -> None:
        if self._closed:
            return
        if self._writer_task is None or self._writer_task.done():
            self._writer_task = asyncio.create_task(self._writer_loop())

    async def _writer_loop(self) -> None:
        try:
            while True:
                frame = await self._outbound_queue.get()
                try:
                    if frame.text is not None:
                        await asyncio.wait_for(
                            self.websocket.send_text(frame.text),
                            timeout=self.send_timeout_seconds,
                        )
                    elif frame.payload is not None:
                        await asyncio.wait_for(
                            self.websocket.send_json(frame.payload),
                            timeout=self.send_timeout_seconds,
                        )
                except TimeoutError:
                    await self._abort_stalled_transport()
                    return
                except WebSocketDisconnect:
                    self._closed = True
                    return
                except Exception:
                    self._closed = True
                    logger.debug(
                        "WebSocket writer failed",
                        extra={"connection_id": self.connection_id},
                        exc_info=True,
                    )
                    return
                finally:
                    self._outbound_queue.task_done()
        except asyncio.CancelledError:
            raise

    async def _abort_stalled_transport(self) -> None:
        """Close a socket that stopped accepting outbound frames."""
        self._closed = True
        tail = self._enqueue_tail
        self._enqueue_tail = None
        if tail is not None and not tail.done():
            tail.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await tail
        self._drain_outbound_queue()
        logger.warning(
            "WebSocket send timed out",
            extra={"connection_id": self.connection_id},
        )
        with contextlib.suppress(Exception):
            await asyncio.wait_for(
                self.websocket.close(code=1013, reason="WebSocket send timeout"),
                timeout=self.send_timeout_seconds,
            )

    def _drain_outbound_queue(self) -> None:
        while True:
            try:
                self._outbound_queue.get_nowait()
            except asyncio.QueueEmpty:
                return
            self._outbound_queue.task_done()


# ---------------------------------------------------------------------------
# WebSocketTurnObserver — implements TurnObserver for WS streaming
# ---------------------------------------------------------------------------


class WebSocketTurnObserver:
    """Bridges TurnScheduler streaming callbacks to WebSocket clients.

    One instance per WebSocketConnectionManager. Fans out streaming
    events to all connections subscribed to the relevant conversation.

    Chat v2 runtime coalescing
    --------------------------
    Streaming assistant tokens arrive at LLM token rate (potentially
    hundreds per second). Sending a full runtime overlay snapshot on every
    token floods the client with redundant frames.

    Instead we coalesce: each ``on_token`` call stores the latest
    snapshot for the conversation and schedules a flush after
    ``_COALESCE_INTERVAL_S`` seconds. If another token arrives before
    the flush fires, the stored snapshot is replaced (latest wins —
    content is always cumulative). The flush sends exactly one Chat v2 frame
    per coalesce window regardless of token rate.

    The coalesce window is intentionally short (~60 ms) so streaming
    still feels live. The client-side rAF batching provides an
    additional layer of smoothing.
    """

    _COALESCE_INTERVAL_S: float = 0.06  # 60 ms

    def __init__(self, manager: WebSocketConnectionManager) -> None:
        self._manager = manager
        # Per-(conversation_id, message_id) sentence buffer for the
        # `tts_sentence_ready` voice-mode frame.
        from cognis.core.sentence_buffer import SentenceBuffer

        self._SentenceBuffer = SentenceBuffer
        self._sentence_buffers: dict[tuple[str, str], SentenceBuffer] = {}

        # Chat v2 runtime snapshot coalescing state.
        self._chat_v2_coalesce_pending: dict[str, _ChatV2CoalescePending] = {}
        self._chat_v2_coalesce_tasks: dict[str, asyncio.Task[None]] = {}

    def _get_sentence_buffer(self, conversation_id: str, message_id: str) -> Any:
        key = (conversation_id, message_id)
        buf = self._sentence_buffers.get(key)
        if buf is None:
            buf = self._SentenceBuffer()
            self._sentence_buffers[key] = buf
        return buf

    def _release_sentence_buffer(self, conversation_id: str, message_id: str) -> Any:
        return self._sentence_buffers.pop((conversation_id, message_id), None)

    async def _has_legacy_subscribers(self, conversation_id: str) -> bool:
        checker = getattr(self._manager, "has_legacy_subscribers", None)
        if checker is None:
            return True
        result = checker(conversation_id)
        if inspect.isawaitable(result):
            result = await result
        return bool(result)

    async def _send_legacy_to_conversation(
        self, conversation_id: str, payload: dict[str, Any]
    ) -> None:
        sender = getattr(self._manager, "send_legacy_to_conversation", None)
        if sender is None:
            sender = self._manager.send_to_conversation
        await sender(conversation_id, payload)

    async def _chat_v2_coalesce_flush(self, conversation_id: str) -> None:
        """Wait for the coalesce interval then send the pending Chat v2 runtime frame."""

        current_task = asyncio.current_task()
        try:
            await asyncio.sleep(self._COALESCE_INTERVAL_S)
            pending = self._chat_v2_coalesce_pending.pop(conversation_id, None)
            if pending is None:
                return
            items = await self._chat_v2_items_for_pending(conversation_id, pending)
            if not items:
                return
            await self._manager.send_chat_v2_runtime_to_conversation(
                conversation_id,
                volatile_items=items,
                active_session_id=pending.active_session_id,
            )
        finally:
            if self._chat_v2_coalesce_tasks.get(conversation_id) is current_task:
                self._chat_v2_coalesce_tasks.pop(conversation_id, None)
            if (
                conversation_id in self._chat_v2_coalesce_pending
                and conversation_id not in self._chat_v2_coalesce_tasks
            ):
                self._chat_v2_coalesce_tasks[conversation_id] = asyncio.create_task(
                    self._chat_v2_coalesce_flush(conversation_id)
                )

    async def _chat_v2_items_for_pending(
        self,
        conversation_id: str,
        pending: _ChatV2CoalescePending,
    ) -> list[TimelineItem]:
        """Rebuild the latest volatile Chat v2 items for one coalesced flush."""

        state = getattr(getattr(self._manager, "app", None), "state", None)
        turn_scheduler = getattr(state, "turn_scheduler", None)
        active_streams: list[dict[str, Any]] = []
        active_tool_outputs: list[dict[str, Any]] = []
        active_turn_state: dict[str, Any] | None = None
        if turn_scheduler is not None:
            if pending.include_streams:
                active_streams = await turn_scheduler.active_stream_snapshots(conversation_id)
            if pending.include_tool_outputs:
                active_tool_outputs = await turn_scheduler.active_tool_output_snapshots(
                    conversation_id
                )
            if pending.include_streams and hasattr(turn_scheduler, "running_turn_state"):
                active_turn_state = turn_scheduler.running_turn_state(conversation_id)

        session_cache = getattr(state, "session_cache", None)
        active_thinking: list[dict[str, Any]] = []
        if (
            pending.include_thinking
            and pending.active_session_id
            and session_cache is not None
            and hasattr(session_cache, "active_thinking_snapshots")
        ):
            active_thinking = session_cache.active_thinking_snapshots(pending.active_session_id)

        if not active_streams and not active_tool_outputs and not active_thinking:
            return []

        phase_hint_items = (
            _chat_v2_phase_hint_items_from_session_cache(
                session_cache,
                pending.active_session_id,
                pending.turn_id,
            )
            if active_streams or active_thinking
            else []
        )
        return runtime_items_from_snapshots(
            active_streams=active_streams,
            active_tool_outputs=active_tool_outputs,
            active_thinking=active_thinking,
            phase_hint_items=phase_hint_items,
            chat_mode=active_turn_state.get("chat_mode") if active_turn_state else None,
            chat_mode_source=active_turn_state.get("chat_mode_source")
            if active_turn_state
            else None,
        )

    async def _chat_v2_coalesce_or_send(
        self,
        conversation_id: str,
        *,
        active_session_id: str | None,
        turn_id: str | None = None,
        include_streams: bool = False,
        include_thinking: bool = False,
        include_tool_outputs: bool = False,
    ) -> None:
        """Buffer a Chat v2 runtime overlay for coalesced delivery."""

        pending = self._chat_v2_coalesce_pending.get(conversation_id)
        if pending is None:
            pending = _ChatV2CoalescePending()
            self._chat_v2_coalesce_pending[conversation_id] = pending
        pending.active_session_id = active_session_id or pending.active_session_id
        pending.turn_id = turn_id or pending.turn_id
        pending.include_streams = pending.include_streams or include_streams
        pending.include_thinking = pending.include_thinking or include_thinking
        pending.include_tool_outputs = pending.include_tool_outputs or include_tool_outputs
        task = self._chat_v2_coalesce_tasks.get(conversation_id)
        if task is None or task.done():
            self._chat_v2_coalesce_tasks[conversation_id] = asyncio.create_task(
                self._chat_v2_coalesce_flush(conversation_id)
            )

    async def _flush_coalesced(self, conversation_id: str) -> None:
        """Immediately flush any pending coalesced snapshot for a conversation.

        Called before non-streaming events (tool_call, message_complete, etc.)
        so the client receives the final streaming state before the boundary.
        """
        chat_v2_pending = self._chat_v2_coalesce_pending.pop(conversation_id, None)
        chat_v2_task = self._chat_v2_coalesce_tasks.pop(conversation_id, None)
        if chat_v2_pending is None:
            # If the scheduled task already popped the pending payload it may
            # be rebuilding or sending the runtime frame. Await it so boundary
            # frames cannot overtake the last buffered streaming update.
            if chat_v2_task is not None and not chat_v2_task.done():
                await chat_v2_task
            return
        if chat_v2_task is not None and not chat_v2_task.done():
            chat_v2_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await chat_v2_task
        items = await self._chat_v2_items_for_pending(conversation_id, chat_v2_pending)
        if not items:
            return
        await self._manager.send_chat_v2_runtime_to_conversation(
            conversation_id,
            volatile_items=items,
            active_session_id=chat_v2_pending.active_session_id,
        )

    async def _send_conversation_activity(
        self,
        conversation_id: str,
        *,
        has_active_turn: bool,
        last_message_at: datetime | None = None,
        active_turn_chat_mode: str | None = None,
        active_turn_chat_mode_source: str | None = None,
    ) -> None:
        payload: dict[str, Any] = {
            "type": "conversation_updated",
            "conversation_id": conversation_id,
            "has_active_turn": has_active_turn,
        }
        if has_active_turn:
            if active_turn_chat_mode is not None:
                payload["active_turn_chat_mode"] = active_turn_chat_mode
            if active_turn_chat_mode_source is not None:
                payload["active_turn_chat_mode_source"] = active_turn_chat_mode_source
        else:
            payload["active_turn_chat_mode"] = None
            payload["active_turn_chat_mode_source"] = None
        if last_message_at is not None:
            payload["last_message_at"] = last_message_at.isoformat()
            payload["updated_at"] = last_message_at.isoformat()
        await self._manager.send_to_conversation(conversation_id, payload)
        # Fan out the same activity correction to owner tabs that are not
        # subscribed to this conversation (e.g. a tab viewing a different chat
        # or a second device). This keeps sidebar turn indicators and
        # last_message_at timestamps accurate across all open clients.
        await self._manager.send_sidebar_update_to_owner(conversation_id, payload)

    async def on_token(
        self,
        conversation_id: str,
        session_id: str,
        message_id: str,
        turn_id: str | None,
        delta: str,
        chunk_index: int | None = None,
        content_offset: int | None = None,
        turn_cycle_index: int | None = None,
    ) -> None:
        del chunk_index, content_offset, turn_cycle_index
        # Conversation-mode TTS streaming: feed the sentence buffer and
        # emit `tts_sentence_ready` to TTS-enabled subscribers only when
        # at least one connection on this conversation has it enabled.
        if delta and self._manager.has_tts_enabled_subscribers(conversation_id):
            buffer = self._get_sentence_buffer(conversation_id, message_id)
            for index, sentence in buffer.feed(delta):
                await self._manager.send_to_tts_subscribers(
                    conversation_id,
                    {
                        "type": "tts_sentence_ready",
                        "conversation_id": conversation_id,
                        "message_id": message_id,
                        "sentence_index": index,
                        "text": sentence,
                    },
                )

        await self._chat_v2_coalesce_or_send(
            conversation_id,
            active_session_id=session_id,
            turn_id=turn_id,
            include_streams=True,
        )

    async def on_tool_call(
        self,
        conversation_id: str,
        session_id: str,
        call_id: str,
        tool_name: str,
        arguments: dict[str, Any] | None,
        turn_id: str | None,
        assistant_phase_index: int | None = None,
        turn_cycle_index: int | None = None,
    ) -> None:
        timestamp = datetime.now(UTC).isoformat()
        _invalidate_chat_v2_phase_hint_items(session_id, turn_id)
        chat_v2_tool_item = tool_call_runtime_item(
            session_id=session_id,
            call_id=call_id,
            tool_name=tool_name,
            arguments=arguments,
            turn_id=turn_id,
            assistant_phase_index=assistant_phase_index,
            turn_cycle_index=turn_cycle_index,
            timestamp=timestamp,
        )
        # Flush any coalesced streaming snapshot before the tool boundary so the
        # client sees the final assistant content before the tool call.
        await self._flush_coalesced(conversation_id)
        session_cache = getattr(self._manager.app.state, "session_cache", None)
        active_thinking = (
            session_cache.active_thinking_snapshots(session_id)
            if session_cache is not None and hasattr(session_cache, "active_thinking_snapshots")
            else []
        )
        phase_hint_items = _chat_v2_phase_hint_items_from_session_cache(
            session_cache,
            session_id,
            turn_id,
        )
        chat_v2_items = [
            *runtime_items_from_snapshots(
                active_thinking=active_thinking,
                phase_hint_items=phase_hint_items,
            ),
            chat_v2_tool_item,
        ]
        await self._manager.send_chat_v2_runtime_to_conversation(
            conversation_id,
            volatile_items=chat_v2_items,
            active_session_id=session_id,
        )

    async def on_tool_progress(
        self,
        conversation_id: str,
        session_id: str,
        call_id: str,
        tool_name: str,
        progress: dict[str, Any],
        turn_id: str | None = None,
        turn_cycle_index: int | None = None,
    ) -> None:
        del call_id, tool_name, progress, turn_cycle_index
        await self._chat_v2_coalesce_or_send(
            conversation_id,
            active_session_id=session_id,
            turn_id=turn_id,
            include_tool_outputs=True,
        )

    async def on_tool_result(
        self,
        conversation_id: str,
        session_id: str,
        call_id: str,
        tool_name: str,
        result: str,
        is_error: bool,
        duration_ms: int | None,
        evaluation: dict[str, Any] | None,
        attachments: list[dict[str, Any]] | None = None,
        file_diffs: list[dict[str, Any]] | None = None,
        turn_id: str | None = None,
        presentation: dict[str, Any] | None = None,
        assistant_phase_index: int | None = None,
        turn_cycle_index: int | None = None,
    ) -> None:
        timestamp = datetime.now(UTC).isoformat()
        _invalidate_chat_v2_phase_hint_items(session_id, turn_id)
        chat_v2_item = tool_result_runtime_item(
            session_id=session_id,
            call_id=call_id,
            tool_name=tool_name,
            result=result,
            is_error=is_error,
            duration_ms=duration_ms,
            evaluation=evaluation,
            attachments=strip_attachment_payload_bytes(attachments or []),
            file_diffs=file_diffs,
            turn_id=turn_id,
            assistant_phase_index=assistant_phase_index,
            turn_cycle_index=turn_cycle_index,
            timestamp=timestamp,
            presentation=presentation,
        )
        # Flush any coalesced stream/tool-output snapshot before the result
        # boundary. Tool calls already do this so the client sees the final
        # assistant stream before the call; results need the same ordering so a
        # buffered live tool-output preview cannot arrive after the terminal
        # result overlay and briefly resurrect stale running state.
        await self._flush_coalesced(conversation_id)
        await self._manager.send_chat_v2_runtime_to_conversation(
            conversation_id,
            volatile_items=[chat_v2_item],
            active_session_id=session_id,
        )

    async def on_tool_output_chunk(
        self,
        conversation_id: str,
        session_id: str,
        call_id: str,
        tool_name: str,
        delta: str,
        stream: str | None,
        turn_id: str | None = None,
        chunk_index: int | None = None,
        content_offset: int | None = None,
        turn_cycle_index: int | None = None,
    ) -> None:
        del call_id, tool_name, delta, stream, chunk_index, content_offset, turn_cycle_index
        await self._chat_v2_coalesce_or_send(
            conversation_id,
            active_session_id=session_id,
            turn_id=turn_id,
            include_tool_outputs=True,
        )

    async def on_context_usage(
        self,
        conversation_id: str,
        session_id: str,
        usage: dict[str, Any],
        turn_id: str | None = None,
    ) -> None:
        del turn_id
        await self._manager.send_chat_v2_runtime_to_conversation(
            conversation_id,
            volatile_items=[],
            active_session_id=session_id,
            context_usage=usage,
        )

    async def on_turn_complete(self, result: TurnResult) -> None:
        # Flush any pending coalesced streaming frame BEFORE the completion
        # frame. Without this a buffered partial-content frame (60 ms coalesce
        # window) fires AFTER the completion item with a HIGHER runtime
        # revision and has_active_turn defaulting to True — downgrading the
        # completed message back to streaming and re-arming the active-turn
        # indicator on the client.
        await self._flush_coalesced(result.conversation_id)
        queued_messages = await _scheduler_queued_messages(
            self._manager.app.state.turn_scheduler,
            result.conversation_id,
        )
        # Flush any trailing sentence to TTS-enabled subscribers.
        if result.message_id and self._manager.has_tts_enabled_subscribers(result.conversation_id):
            buffer = self._release_sentence_buffer(result.conversation_id, result.message_id)
            if buffer is not None:
                trailing = buffer.flush()
                if trailing is not None:
                    index, sentence = trailing
                    await self._manager.send_to_tts_subscribers(
                        result.conversation_id,
                        {
                            "type": "tts_sentence_ready",
                            "conversation_id": result.conversation_id,
                            "message_id": result.message_id,
                            "sentence_index": index,
                            "text": sentence,
                        },
                    )

        if result.final_content:
            completed_at_iso = (
                result.completed_at.isoformat()
                if result.completed_at is not None
                else datetime.now(UTC).isoformat()
            )
            chat_v2_completion_item = assistant_completion_runtime_item(
                message_id=result.message_id,
                turn_id=result.turn_id,
                session_id=result.session_id,
                phase=result.assistant_phase_index,
                content=result.final_content,
                timestamp=completed_at_iso,
                partial=result.partial,
                chat_mode=result.chat_mode if result.chat_mode != "default" else None,
                chat_mode_source=result.chat_mode_source
                if result.chat_mode_source != "system_default"
                else None,
                turn_cycle_index=result.turn_cycle_index,
            )
            await self._manager.send_chat_v2_runtime_to_conversation(
                result.conversation_id,
                volatile_items=[chat_v2_completion_item],
                active_session_id=result.session_id,
                has_active_turn=result.managed_continuation_pending,
                context_usage=result.context_usage,
                last_generation=result.last_generation,
            )

        if await self._has_legacy_subscribers(result.conversation_id):
            payload: dict[str, Any] = {
                "type": "message_complete",
                "conversation_id": result.conversation_id,
                "session_id": result.session_id,
                "message_id": result.message_id,
                "turn_id": result.turn_id,
                "content": result.final_content,
                "seq": result.last_seq,
                "token_usage": None,
                "context_usage": result.context_usage,
                "last_generation": result.last_generation,
                "queued_count": len(queued_messages),
                "messages": queued_messages,
                "completed_at": (
                    result.completed_at.isoformat()
                    if result.completed_at is not None
                    else datetime.now(UTC).isoformat()
                ),
                "attachments": strip_attachment_payload_bytes(result.attachments or []),
                "chat_mode": result.chat_mode,
                "chat_mode_source": result.chat_mode_source,
                "partial": result.partial,
                "finish_reason": result.finish_reason,
                "assistant_phase_index": result.assistant_phase_index,
                "turn_cycle_index": result.turn_cycle_index,
                "managed_continuation_pending": result.managed_continuation_pending,
                "runtime": result.runtime,
            }
            if result.delegated:
                payload["delegated"] = True
                payload["task_id"] = result.task_id
            await self._send_legacy_to_conversation(result.conversation_id, payload)
        if not result.final_content and not result.managed_continuation_pending:
            await self._manager.send_chat_v2_runtime_to_conversation(
                result.conversation_id,
                volatile_items=[],
                active_session_id=result.session_id,
                has_active_turn=False,
                context_usage=result.context_usage,
                last_generation=result.last_generation,
            )

        completed_at = result.completed_at if result.completed_at is not None else datetime.now(UTC)
        await self._send_conversation_activity(
            result.conversation_id,
            has_active_turn=result.managed_continuation_pending,
            last_message_at=completed_at,
            active_turn_chat_mode=result.chat_mode if result.managed_continuation_pending else None,
            active_turn_chat_mode_source=result.chat_mode_source
            if result.managed_continuation_pending
            else None,
        )

        # Notify clients if the conversation title changed
        if result.title_changed and result.new_title:
            await self._manager.send_to_conversation(
                result.conversation_id,
                {
                    "type": "conversation_updated",
                    "conversation_id": result.conversation_id,
                    "title": result.new_title,
                    "has_active_turn": result.managed_continuation_pending,
                    "last_message_at": completed_at.isoformat(),
                    "updated_at": completed_at.isoformat(),
                },
            )

    async def on_turn_error(self, conversation_id: str, error: TurnError) -> None:
        # Flush any coalesced live.thinking / live.assistant_stream patch BEFORE
        # sending the error frame. Without this, a buffered streaming:true patch
        # (coalesce=True, ~60ms window) arrives AFTER the turn_cancelled error
        # and AFTER the has_active_turn=false activity frame, causing:
        #   (1) the thinking block to visibly stream in after cancel, and
        #   (2) the client to re-arm turnInProgress=true (timelinePatchHasActiveWork).
        # Flushing first ensures the client sees the streaming content (if any)
        # before the teardown signal, not after it.
        await self._flush_coalesced(conversation_id)
        await self._manager.send_to_conversation(
            conversation_id,
            WebSocketError(
                code=error.code,
                message=error.message,
                recoverable=error.recoverable,
                error_detail=error.detail.get("error_detail") if error.detail else None,
                detail=error.detail,
            ).model_dump(),
        )
        await self._send_conversation_activity(conversation_id, has_active_turn=False)
        await self._manager.send_chat_v2_runtime_to_conversation(
            conversation_id,
            volatile_items=[],
            has_active_turn=False,
        )

    async def on_thinking(
        self,
        conversation_id: str,
        session_id: str,
        message_id: str,
        turn_id: str | None,
        block_id: str,
        delta: str,
        title: str | None,
        complete: bool,
        content: str | None = None,
        started_at: str | None = None,
        completed_at: str | None = None,
        duration_ms: int | None = None,
        source: str | None = None,
        provider_block_index: int | None = None,
        turn_cycle_index: int | None = None,
    ) -> None:
        """Emit assistant thinking chunk or block boundary frame.

        Streaming deltas → ``assistant_thinking_chunk`` (droppable under
        backpressure, same as regular ``chunk`` frames).
        Block-boundary signals (``complete=True`` with empty delta) →
        ``assistant_thinking_block`` to let the UI finalize the block.

        NOTE: a running_turn_state guard was previously placed here to drop
        thinking frames for settled turns. It was removed because it dropped
        the FINAL thinking block at the settled/drain race: reasoning models
        emit the last block boundary right at the completion transition, and
        running_turn_state() returns None as soon as control.settled=True —
        which is set at the START of the completion path, before the agent
        loop finishes draining. This caused thinking to never render or stick
        in a streaming state while text streamed normally (on_token has no
        such guard). The cancel-stale case is already covered by:
          1. on_turn_error flushes the coalescer before sending the error frame.
          2. session_cache._cleared_thinking_turns blocks re-creation after clear.
          3. clear_active_thinking() is called at turn teardown.
        """
        del (
            message_id,
            block_id,
            delta,
            title,
            complete,
            content,
            started_at,
            completed_at,
            duration_ms,
            source,
            provider_block_index,
            turn_cycle_index,
        )
        await self._chat_v2_coalesce_or_send(
            conversation_id,
            active_session_id=session_id,
            turn_id=turn_id,
            include_thinking=True,
        )

    async def on_system_message(
        self,
        conversation_id: str,
        text: str,
        notice_id: str | None = None,
        kind: str | None = None,
        scope: str | None = None,
        turn_id: str | None = None,
        retry_reason: str | None = None,
        retry_source_turn_id: str | None = None,
        attempt: int | None = None,
    ) -> None:
        payload: dict[str, Any] = {
            "type": "system_message",
            "conversation_id": conversation_id,
            "text": text,
            "notice_id": notice_id,
            "kind": kind,
            "scope": scope,
            "turn_id": turn_id,
        }
        if retry_reason is not None:
            payload["retry_reason"] = retry_reason
        if retry_source_turn_id is not None:
            payload["retry_source_turn_id"] = retry_source_turn_id
        if attempt is not None:
            payload["attempt"] = attempt
        await self._manager.send_to_conversation(conversation_id, payload)
        if notice_id is not None:
            scheduler = getattr(self._manager.app.state, "turn_scheduler", None)
            context = (
                scheduler.relay_generation_context(conversation_id)
                if scheduler is not None and hasattr(scheduler, "relay_generation_context")
                else None
            )
            session_id = getattr(context, "session_id", None)
            await self._manager.send_chat_v2_runtime_to_conversation(
                conversation_id,
                volatile_items=[
                    system_message_runtime_item(
                        notice_id=notice_id,
                        content=text,
                        turn_id=turn_id,
                        session_id=session_id,
                        timestamp=datetime.now(UTC).isoformat(),
                        notice_kind=kind,
                        notice_scope=scope,
                        retry_reason=retry_reason,
                        retry_source_turn_id=retry_source_turn_id,
                        attempt=attempt,
                    )
                ],
                active_session_id=session_id,
            )

    async def on_queued(self, conversation_id: str, queued_count: int) -> None:
        await self._manager.send_to_conversation(
            conversation_id,
            {
                "type": "queued",
                "conversation_id": conversation_id,
                "queued_count": queued_count,
            },
        )

    async def on_queued_messages(
        self, conversation_id: str, messages: list[dict[str, Any]]
    ) -> None:
        await self._manager.send_to_conversation(
            conversation_id,
            {
                "type": "queued_messages_updated",
                "conversation_id": conversation_id,
                "queued_count": len(messages),
                "messages": messages,
            },
        )


# ---------------------------------------------------------------------------
# WebSocketConnectionManager — thin transport layer
# ---------------------------------------------------------------------------


class WebSocketConnectionManager:
    """Manages WebSocket connections and event fanout.

    This is a **transport-only** component. It does NOT contain any
    turn orchestration, command dispatch, or business logic. Those
    responsibilities belong to ``TurnScheduler`` and ``CommandDispatcher``.
    """

    def __init__(self, app: Any) -> None:
        self.app = app
        self._connections: dict[str, AuthenticatedWebSocket] = {}
        self._by_conversation: dict[str, set[str]] = defaultdict(set)
        self._by_chat_v2_conversation: dict[str, set[str]] = defaultdict(set)
        self._by_chat_v2_scope: dict[str, set[str]] = defaultdict(set)
        self._by_user: dict[str, set[str]] = defaultdict(set)
        self._chat_v2_runtime_revisions: dict[str, int] = defaultdict(int)

        # Create the TurnObserver bridge
        self._observer = WebSocketTurnObserver(self)
        self._relay_runtime_items: dict[str, tuple[str, dict[str, TimelineItem]]] = {}

        # Register as global EventBus subscriber for UI fanout
        event_bus = getattr(app.state, "event_bus", None)
        if event_bus is not None:
            event_bus.subscribe_all(self._handle_event)

        # Register observer on the TurnScheduler for all conversations
        turn_scheduler = getattr(app.state, "turn_scheduler", None)
        if turn_scheduler is not None:
            # We register per-conversation when clients subscribe
            pass

    async def connect(
        self, websocket: WebSocket, *, claims: dict[str, Any]
    ) -> AuthenticatedWebSocket:
        """Register a new WebSocket connection."""
        connection = AuthenticatedWebSocket(
            connection_id=f"ws_{uuid.uuid4().hex[:12]}",
            websocket=websocket,
            user_email=claims["sub"],
            role=claims.get("role", "user"),
        )
        self._connections[connection.connection_id] = connection
        self._by_user[connection.user_email].add(connection.connection_id)
        WS_CONNECTIONS_ACTIVE.inc()
        WS_CONNECTIONS_TOTAL.inc()
        return connection

    async def disconnect(self, connection: AuthenticatedWebSocket) -> None:
        """Unregister a WebSocket connection."""
        self._connections.pop(connection.connection_id, None)
        user_connections = self._by_user.get(connection.user_email)
        if user_connections is not None:
            user_connections.discard(connection.connection_id)
            if not user_connections:
                del self._by_user[connection.user_email]
        for cid in list(connection.subscriptions):
            self._unsubscribe(connection, cid)
        for scope_key in list(connection.chat_v2_scopes):
            self.unsubscribe_chat_v2(connection, scope_key)
        await connection.close()
        WS_CONNECTIONS_ACTIVE.dec()

    def _has_conversation_observers(self, conversation_id: str) -> bool:
        """Return True when any connection needs turn observer events."""

        return bool(
            self._by_conversation.get(conversation_id)
            or self._by_chat_v2_conversation.get(conversation_id)
        )

    def _ensure_turn_observer(self, conversation_id: str) -> None:
        """Register the turn observer for the first subscriber of any stream."""

        turn_scheduler = getattr(self.app.state, "turn_scheduler", None)
        if turn_scheduler is not None:
            turn_scheduler.add_observer(conversation_id, self._observer)

    def _remove_turn_observer_if_unused(self, conversation_id: str) -> None:
        """Remove the turn observer when no legacy or Chat v2 subscriber remains."""

        if self._has_conversation_observers(conversation_id):
            return
        turn_scheduler = getattr(self.app.state, "turn_scheduler", None)
        if turn_scheduler is not None:
            turn_scheduler.remove_observer(conversation_id, self._observer)

    def subscribe(self, connection: AuthenticatedWebSocket, conversation_id: str) -> None:
        """Subscribe a connection to a conversation's events."""
        already_observed = self._has_conversation_observers(conversation_id)
        connection.subscriptions.add(conversation_id)
        self._by_conversation[conversation_id].add(connection.connection_id)

        # Register observer on TurnScheduler only on first subscription
        # (idempotent — prevents duplicate event delivery)
        if not already_observed:
            self._ensure_turn_observer(conversation_id)

    def subscribe_chat_v2(
        self,
        connection: AuthenticatedWebSocket,
        scope: TimelineScope,
        *,
        cursor: str,
    ) -> None:
        """Opt a connection into Chat v2 realtime frames for one verified scope."""

        if scope.missing_stream:
            return
        # Task-step scopes must have been rehydrated from the store by the
        # websocket subscribe handler.  A client-created TimelineScope must
        # never be enough to register a task-step runtime stream.
        if scope.kind == "task_step" and not scope._server_authoritative:
            return
        scope_key = scope.key
        conversation_id = scope.conversation_id
        if not conversation_id:
            return
        already_observed = self._has_conversation_observers(conversation_id)
        self._by_chat_v2_conversation[conversation_id].add(connection.connection_id)
        self._by_chat_v2_scope[scope_key].add(connection.connection_id)
        connection.chat_v2_cursors[scope_key] = cursor
        connection.chat_v2_scopes[scope_key] = scope
        if not already_observed:
            self._ensure_turn_observer(conversation_id)

    def update_chat_v2_cursor(
        self,
        connection: AuthenticatedWebSocket,
        scope_key: str,
        *,
        cursor: str,
    ) -> None:
        """Update the latest Chat v2 cursor known for a subscribed connection."""

        if scope_key in connection.chat_v2_cursors:
            connection.chat_v2_cursors[scope_key] = cursor

    def unsubscribe_chat_v2(
        self,
        connection: AuthenticatedWebSocket,
        scope_key: str,
    ) -> None:
        """Disable Chat v2 frames without changing the legacy subscription."""

        scope = connection.chat_v2_scopes.pop(scope_key, None)
        connection.chat_v2_cursors.pop(scope_key, None)
        conns = self._by_chat_v2_scope.get(scope_key)
        if conns:
            conns.discard(connection.connection_id)
            if not conns:
                del self._by_chat_v2_scope[scope_key]
        if scope is not None and scope.conversation_id:
            conversation_scopes = self._by_chat_v2_conversation.get(scope.conversation_id)
            still_subscribed = any(
                subscribed_scope.conversation_id == scope.conversation_id
                for subscribed_scope in connection.chat_v2_scopes.values()
            )
            if conversation_scopes and not still_subscribed:
                conversation_scopes.discard(connection.connection_id)
                if not conversation_scopes:
                    del self._by_chat_v2_conversation[scope.conversation_id]
            self._remove_turn_observer_if_unused(scope.conversation_id)

    def _next_chat_v2_runtime_revision(self, conversation_id: str) -> int:
        self._chat_v2_runtime_revisions[conversation_id] += 1
        return self._chat_v2_runtime_revisions[conversation_id]

    async def send_queue_snapshot(
        self, connection: AuthenticatedWebSocket, conversation_id: str
    ) -> None:
        """Send the current in-memory queue state to a subscribed client."""
        turn_scheduler = getattr(self.app.state, "turn_scheduler", None)
        if turn_scheduler is None:
            return
        messages = await _scheduler_queued_messages(turn_scheduler, conversation_id)
        await connection.send_json(
            {
                "type": "queued_messages_updated",
                "conversation_id": conversation_id,
                "queued_count": len(messages),
                "messages": messages,
            }
        )

    async def _conversation_runtime_snapshot(
        self,
        conversation_id: str,
        *,
        active_session_id: str | None,
    ) -> dict[str, Any]:
        """Return authoritative volatile runtime state for a conversation."""

        turn_scheduler = getattr(self.app.state, "turn_scheduler", None)
        queued_messages: list[dict[str, Any]] = []
        active_streams: list[dict[str, Any]] = []
        active_tool_outputs: list[dict[str, Any]] = []
        active_turn_state: dict[str, Any] | None = None
        runtime_server_time = server_time_iso()
        if turn_scheduler is not None:
            queued_messages = await _scheduler_queued_messages(
                turn_scheduler,
                conversation_id,
            )
            active_streams = await turn_scheduler.active_stream_snapshots(conversation_id)
            active_tool_outputs = await turn_scheduler.active_tool_output_snapshots(conversation_id)
            durable_running = getattr(turn_scheduler, "durable_running_turn_state", None)
            active_turn_state = (
                await durable_running(conversation_id)
                if callable(durable_running)
                else turn_scheduler.running_turn_state(conversation_id)
                if hasattr(turn_scheduler, "running_turn_state")
                else None
            )

        active_thinking: list[dict[str, Any]] = []
        session_cache = getattr(self.app.state, "session_cache", None)
        if (
            active_session_id
            and session_cache is not None
            and hasattr(session_cache, "active_thinking_snapshots")
        ):
            active_thinking = session_cache.active_thinking_snapshots(active_session_id)

        has_active_turn = active_turn_state is not None

        # Defense-in-depth: when no turn is running, suppress any stale
        # active_streams / active_tool_outputs / active_thinking from the
        # snapshot so the client never receives runtime items for a finished
        # turn.
        # The primary fix is clearing _active_thinking at turn teardown
        # (session_cache.clear_active_thinking), but this guard ensures that
        # any residual state (e.g. from a controller restart that lost the
        # teardown signal) never re-injects hanging spinners on reconnect.
        if not has_active_turn:
            active_streams = []
            active_tool_outputs = []
            active_thinking = []

        payload = {
            "queued_messages": queued_messages,
            "queued_count": len(queued_messages),
            "has_active_turn": has_active_turn,
            "active_turn": active_turn_state,
            "active_turn_chat_mode": (
                active_turn_state.get("chat_mode") if active_turn_state else None
            ),
            "active_turn_chat_mode_source": (
                active_turn_state.get("chat_mode_source") if active_turn_state else None
            ),
            "active_streams": active_streams,
            "active_tool_outputs": active_tool_outputs,
            "active_thinking": active_thinking,
            "last_generation": (
                session_cache.get_last_generation_performance(active_session_id)
                if active_session_id
                and session_cache is not None
                and hasattr(session_cache, "get_last_generation_performance")
                else None
            ),
        }
        payload["runtime_generation"] = runtime_generation(payload)
        payload["server_time"] = runtime_server_time
        payload["build_id"] = cognis_build_id()
        return payload

    async def _send_conversation_runtime_snapshot(
        self,
        connection: AuthenticatedWebSocket,
        conversation_id: str,
        *,
        active_session_id: str | None,
    ) -> None:
        runtime = await self._conversation_runtime_snapshot(
            conversation_id,
            active_session_id=active_session_id,
        )
        await connection.send_json(
            {
                "type": "conversation_runtime_snapshot",
                "conversation_id": conversation_id,
                **runtime,
            }
        )
        chat_v2_cursors = getattr(connection, "chat_v2_cursors", {})
        cursor = chat_v2_cursors.get(conversation_id) if isinstance(chat_v2_cursors, dict) else None
        if cursor:
            runtime_revision = self._next_chat_v2_runtime_revision(conversation_id)
            session_cache = getattr(self.app.state, "session_cache", None)
            phase_hint_items = _chat_v2_phase_hint_items_from_session_cache(
                session_cache, active_session_id
            )
            volatile_items = runtime_items_from_snapshots(
                active_streams=runtime.get("active_streams")
                if isinstance(runtime.get("active_streams"), list)
                else [],
                active_tool_outputs=runtime.get("active_tool_outputs")
                if isinstance(runtime.get("active_tool_outputs"), list)
                else [],
                active_thinking=runtime.get("active_thinking")
                if isinstance(runtime.get("active_thinking"), list)
                else [],
                phase_hint_items=phase_hint_items,
                chat_mode=runtime.get("active_turn_chat_mode")
                if isinstance(runtime.get("active_turn_chat_mode"), str)
                else None,
                chat_mode_source=runtime.get("active_turn_chat_mode_source")
                if isinstance(runtime.get("active_turn_chat_mode_source"), str)
                else None,
            )
            overlay = runtime_overlay_from_items(
                conversation_id=conversation_id,
                runtime_revision=runtime_revision,
                has_active_turn=bool(runtime.get("has_active_turn")),
                active_turn=self._chat_v2_active_turn_payload(
                    conversation_id,
                    active_session_id=active_session_id,
                    runtime=runtime,
                ),
                volatile_items=volatile_items,
                context_usage=(
                    session_cache.get_context_usage(active_session_id)
                    if session_cache is not None
                    and hasattr(session_cache, "get_context_usage")
                    and active_session_id
                    else None
                ),
                last_generation=runtime.get("last_generation")
                if isinstance(runtime.get("last_generation"), dict)
                else None,
                generated_at=runtime.get("server_time"),
            )
            await connection.send_json(
                runtime_frame(
                    conversation_id=conversation_id,
                    cursor=cursor,
                    runtime=overlay,
                    server_time=runtime.get("server_time"),
                ).model_dump(mode="json")
            )

    async def send_chat_v2_scope_runtime_snapshot(
        self,
        connection: AuthenticatedWebSocket,
        scope: TimelineScope,
    ) -> None:
        """Send the current runtime overlay using the subscribed scope identity."""

        if not scope.conversation_id:
            return
        runtime = await self._conversation_runtime_snapshot(
            scope.conversation_id,
            active_session_id=scope.session_id,
        )
        runtime_turn = runtime.get("active_turn")
        if (
            isinstance(runtime_turn, dict)
            and scope.kind != "conversation"
            and scope.session_id
            and runtime_turn.get("session_id") != scope.session_id
        ):
            runtime["has_active_turn"] = False
            runtime["active_turn"] = None
            runtime["active_streams"] = []
            runtime["active_tool_outputs"] = []
            runtime["active_thinking"] = []
        cursor = connection.chat_v2_cursors.get(scope.key)
        if not cursor:
            return
        session_cache = getattr(self.app.state, "session_cache", None)
        phase_hint_items = _chat_v2_phase_hint_items_from_session_cache(
            session_cache, scope.session_id
        )
        volatile_items = runtime_items_from_snapshots(
            active_streams=runtime.get("active_streams")
            if isinstance(runtime.get("active_streams"), list)
            else [],
            active_tool_outputs=runtime.get("active_tool_outputs")
            if isinstance(runtime.get("active_tool_outputs"), list)
            else [],
            active_thinking=runtime.get("active_thinking")
            if isinstance(runtime.get("active_thinking"), list)
            else [],
            phase_hint_items=phase_hint_items,
            chat_mode=runtime.get("active_turn_chat_mode")
            if isinstance(runtime.get("active_turn_chat_mode"), str)
            else None,
            chat_mode_source=runtime.get("active_turn_chat_mode_source")
            if isinstance(runtime.get("active_turn_chat_mode_source"), str)
            else None,
        )
        overlay = runtime_overlay_from_items(
            conversation_id=scope.conversation_id,
            scope=scope,
            runtime_revision=self._next_chat_v2_runtime_revision(scope.key),
            has_active_turn=bool(runtime.get("has_active_turn")),
            active_turn=self._chat_v2_active_turn_payload(
                scope.conversation_id,
                active_session_id=scope.session_id,
                runtime=runtime,
            ),
            volatile_items=volatile_items,
            context_usage=(
                session_cache.get_context_usage(scope.session_id)
                if session_cache is not None
                and hasattr(session_cache, "get_context_usage")
                and scope.session_id
                else None
            ),
            last_generation=runtime.get("last_generation")
            if isinstance(runtime.get("last_generation"), dict)
            else None,
            generated_at=runtime.get("server_time"),
        )
        await connection.send_json(
            runtime_frame(
                conversation_id=scope.conversation_id,
                scope=scope,
                cursor=cursor,
                runtime=overlay,
                server_time=runtime.get("server_time"),
            ).model_dump(mode="json")
        )
        relay = cast(Any, getattr(self.app.state, "chat_v2_runtime_relay", None))
        scheduler = getattr(self.app.state, "turn_scheduler", None)
        if (
            relay is not None
            and bool(runtime.get("has_active_turn"))
            and scheduler is not None
            and hasattr(scheduler, "durable_relay_generation_context")
        ):
            context = await scheduler.durable_relay_generation_context(scope.conversation_id)
            if context is not None:
                envelope = await relay.hydrate_latest(context)
                if envelope is not None:
                    await self.apply_relayed_runtime(envelope)

    def _unsubscribe(self, connection: AuthenticatedWebSocket, conversation_id: str) -> None:
        """Unsubscribe a connection from a conversation."""
        connection.subscriptions.discard(conversation_id)
        self.unsubscribe_chat_v2(connection, conversation_id)
        conns = self._by_conversation.get(conversation_id)
        if conns:
            conns.discard(connection.connection_id)
            if not conns:
                del self._by_conversation[conversation_id]
        self._remove_turn_observer_if_unused(conversation_id)

    async def send_to_conversation(
        self,
        conversation_id: str,
        payload: dict[str, Any],
        *,
        include_chat_v2: bool = False,
    ) -> None:
        """Fan out a payload to all connections subscribed to a conversation."""
        connection_ids = set(self._by_conversation.get(conversation_id, set()))
        if include_chat_v2:
            connection_ids.update(self._by_chat_v2_conversation.get(conversation_id, set()))
        if not connection_ids:
            return
        payload = await self._enrich_conversation_updated_payload(conversation_id, payload)
        serialized = _json_dumps_frame(payload)
        msg_type = payload.get("type")
        message_id = payload.get("message_id")
        for cid in list(connection_ids):
            conn = self._connections.get(cid)
            if conn is not None:
                await conn.send_text(
                    serialized,
                    msg_type=msg_type if isinstance(msg_type, str) else "",
                    message_id=message_id if isinstance(message_id, str) else None,
                    conversation_id=conversation_id,
                    block=False,
                )

    def has_legacy_subscribers(self, conversation_id: str) -> bool:
        """Return True when any subscribed connection still needs legacy frames."""

        for cid in self._by_conversation.get(conversation_id, set()):
            conn = self._connections.get(cid)
            if conn is not None and not any(
                scope.kind == "conversation" and scope.conversation_id == conversation_id
                for scope in conn.chat_v2_scopes.values()
            ):
                return True
        return False

    async def send_legacy_to_conversation(
        self, conversation_id: str, payload: dict[str, Any]
    ) -> None:
        """Fan out a legacy payload only to non-Chat-v2 subscribers."""

        connection_ids = self._by_conversation.get(conversation_id, set())
        if not connection_ids:
            return
        serialized = _json_dumps_frame(payload)
        msg_type = payload.get("type")
        message_id = payload.get("message_id")
        for cid in list(connection_ids):
            conn = self._connections.get(cid)
            if conn is None or any(
                scope.kind == "conversation" and scope.conversation_id == conversation_id
                for scope in conn.chat_v2_scopes.values()
            ):
                continue
            await conn.send_text(
                serialized,
                msg_type=msg_type if isinstance(msg_type, str) else "",
                message_id=message_id if isinstance(message_id, str) else None,
                conversation_id=conversation_id,
                block=False,
            )

    async def send_chat_v2_runtime_to_conversation(
        self,
        conversation_id: str,
        *,
        volatile_items: list[TimelineItem],
        has_active_turn: bool = True,
        active_session_id: str | None = None,
        context_usage: dict[str, Any] | None = None,
        last_generation: dict[str, Any] | None = None,
    ) -> None:
        """Fan out locally first, then enqueue the same generation for Redis relay."""
        relay = cast(Any, getattr(self.app.state, "chat_v2_runtime_relay", None))
        turn_scheduler = getattr(self.app.state, "turn_scheduler", None)
        context = (
            turn_scheduler.relay_generation_context(conversation_id)
            if turn_scheduler is not None and hasattr(turn_scheduler, "relay_generation_context")
            else None
        )
        effective_items = volatile_items
        if context is not None:
            turn_id, cumulative = self._relay_runtime_items.get(
                conversation_id,
                (context.turn_id, {}),
            )
            if turn_id != context.turn_id:
                cumulative = {}
            if has_active_turn or volatile_items:
                for item in volatile_items:
                    cumulative[item.id] = item
                effective_items = list(cumulative.values())
                self._relay_runtime_items[conversation_id] = (context.turn_id, cumulative)
            else:
                effective_items = []
        if not has_active_turn:
            effective_items = [
                item
                for item in effective_items
                if not (
                    item.kind == "message"
                    and item.role == "system"
                    and item.notice_scope == "transient_retry"
                )
            ]
        await self._fanout_chat_v2_runtime(
            conversation_id,
            volatile_items=effective_items,
            has_active_turn=has_active_turn,
            active_session_id=active_session_id,
            context_usage=context_usage,
            last_generation=last_generation,
        )
        if context is None or relay is None:
            if not has_active_turn:
                self._relay_runtime_items.pop(conversation_id, None)
            return
        try:
            running_state = (
                turn_scheduler.running_turn_state(conversation_id)
                if turn_scheduler is not None
                else None
            )
            active_turn_data = (
                {
                    "turn_id": context.turn_id,
                    "session_id": context.session_id,
                    "status": (running_state or {}).get("status") or "running",
                    "chat_mode": (running_state or {}).get("chat_mode"),
                    "chat_mode_source": (running_state or {}).get("chat_mode_source"),
                }
                if has_active_turn
                else None
            )
            from cognis.api.chat_v2.schemas import RuntimeActiveTurn
            from cognis.models.config import GenerationPerformanceSnapshot

            envelope = relay.make_envelope(
                context,
                kind=RelayKind.RUNTIME if has_active_turn else RelayKind.TERMINAL,
                has_active_turn=has_active_turn,
                active_turn=(
                    RuntimeActiveTurn.model_validate(active_turn_data)
                    if active_turn_data is not None
                    else None
                ),
                volatile_items=effective_items,
                context_usage=context_usage,
                last_generation=(
                    GenerationPerformanceSnapshot.model_validate(last_generation)
                    if last_generation is not None
                    else None
                ),
            )
            cumulative_boundary = _runtime_relay_cumulative_boundary(
                effective_items,
                has_active_turn=has_active_turn,
            )
            relay.enqueue(envelope, cumulative_boundary=cumulative_boundary)
        except (TypeError, ValueError):
            return
        finally:
            if not has_active_turn:
                self._relay_runtime_items.pop(conversation_id, None)

    async def _fanout_chat_v2_runtime(
        self,
        conversation_id: str,
        *,
        volatile_items: list[TimelineItem],
        has_active_turn: bool,
        active_session_id: str | None,
        context_usage: dict[str, Any] | None,
        last_generation: dict[str, Any] | None,
        active_turn: dict[str, Any] | None = None,
    ) -> None:
        """Apply a runtime overlay to authorized local scopes only."""

        connection_ids = self._by_chat_v2_conversation.get(conversation_id, set())
        if not connection_ids:
            return
        server_time = server_time_iso()
        for cid in list(connection_ids):
            conn = self._connections.get(cid)
            if conn is None:
                continue
            for scope_key, scope in list(conn.chat_v2_scopes.items()):
                if not scope_accepts_runtime(
                    scope,
                    conversation_id=conversation_id,
                    active_session_id=active_session_id,
                ):
                    continue
                cursor = conn.chat_v2_cursors.get(scope_key)
                if not cursor:
                    continue
                runtime_revision = self._next_chat_v2_runtime_revision(scope_key)
                overlay = runtime_overlay_from_items(
                    conversation_id=conversation_id,
                    scope=scope,
                    runtime_revision=runtime_revision,
                    has_active_turn=has_active_turn,
                    active_turn=(
                        active_turn
                        or self._chat_v2_active_turn_payload(
                            conversation_id, active_session_id=active_session_id
                        )
                        if has_active_turn
                        else None
                    ),
                    volatile_items=volatile_items,
                    context_usage=context_usage,
                    last_generation=last_generation,
                    generated_at=server_time,
                )
                serialized = _json_dumps_frame(
                    runtime_frame(
                        conversation_id=conversation_id,
                        scope=scope,
                        cursor=cursor,
                        runtime=overlay,
                        server_time=server_time,
                    ).model_dump(mode="json")
                )
                await conn.send_text(
                    serialized,
                    msg_type="chat_v2_frame",
                    message_id=None,
                    conversation_id=conversation_id,
                    block=False,
                )

    def has_chat_v2_subscriber(self, conversation_id: str) -> bool:
        return bool(self._by_chat_v2_conversation.get(conversation_id))

    async def validate_relay_envelope(
        self, envelope: ChatV2RuntimeRelayEnvelope
    ) -> AdmissionDecision:
        """Validate Redis control data against the current PostgreSQL owner generation."""
        scheduler = getattr(self.app.state, "turn_scheduler", None)
        context = (
            await scheduler.durable_relay_generation_context(envelope.conversation_id)
            if scheduler is not None and hasattr(scheduler, "durable_relay_generation_context")
            else None
        )
        if context is None and envelope.kind == RelayKind.TERMINAL:
            context = (
                await scheduler.durable_terminal_relay_generation_context(
                    envelope.direct_request_id
                )
                if scheduler is not None
                and hasattr(scheduler, "durable_terminal_relay_generation_context")
                else None
            )
        if context is None:
            return AdmissionDecision.STALE
        if (
            context.conversation_id != envelope.conversation_id
            or context.turn_id != envelope.turn_id
            or context.direct_request_id != envelope.direct_request_id
            or context.session_id != envelope.session_id
        ):
            return AdmissionDecision.WRONG_TURN
        if context.fencing_token != envelope.fencing_token:
            return AdmissionDecision.WRONG_FENCE
        if (
            context.owner_controller_id != envelope.owner.controller_id
            or context.owner_incarnation_id != envelope.owner.incarnation_id
        ):
            return AdmissionDecision.STALE
        return AdmissionDecision.ACCEPT

    async def apply_relayed_runtime(self, envelope: ChatV2RuntimeRelayEnvelope) -> None:
        """Apply a validated relay envelope locally without publishing it again."""
        if not self.has_chat_v2_subscriber(envelope.conversation_id):
            return
        await self._fanout_chat_v2_runtime(
            envelope.conversation_id,
            volatile_items=list(envelope.volatile_items),
            has_active_turn=envelope.has_active_turn,
            active_session_id=envelope.session_id,
            context_usage=envelope.context_usage,
            last_generation=(
                envelope.last_generation.model_dump(mode="json")
                if envelope.last_generation is not None
                else None
            ),
            active_turn=(
                envelope.active_turn.model_dump(mode="json")
                if envelope.active_turn is not None
                else None
            ),
        )

    def _chat_v2_active_turn_payload(
        self,
        conversation_id: str,
        *,
        active_session_id: str | None,
        runtime: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        """Return the Chat v2 active-turn payload for runtime overlay frames."""

        has_active_turn = bool(runtime.get("has_active_turn")) if runtime is not None else True
        if not has_active_turn:
            return None
        durable_turn = runtime.get("active_turn") if isinstance(runtime, dict) else None
        if (
            isinstance(durable_turn, dict)
            and isinstance(durable_turn.get("turn_id"), str)
            and isinstance(durable_turn.get("session_id"), str)
        ):
            return durable_turn
        turn_scheduler = getattr(self.app.state, "turn_scheduler", None)
        checkpoint = (
            turn_scheduler.active_turn_checkpoint(conversation_id)
            if turn_scheduler is not None and hasattr(turn_scheduler, "active_turn_checkpoint")
            else None
        )
        running_state = (
            turn_scheduler.running_turn_state(conversation_id)
            if turn_scheduler is not None and hasattr(turn_scheduler, "running_turn_state")
            else None
        )
        return {
            "turn_id": (checkpoint or {}).get("turn_id") or f"active:{conversation_id}",
            "session_id": (checkpoint or {}).get("session_id") or active_session_id or "",
            "status": "running",
            "chat_mode": (
                (running_state or {}).get("chat_mode")
                or (runtime or {}).get("active_turn_chat_mode")
            ),
            "chat_mode_source": (
                (running_state or {}).get("chat_mode_source")
                or (runtime or {}).get("active_turn_chat_mode_source")
            ),
        }

    async def send_to_user(self, user_email: str, payload: dict[str, Any]) -> None:
        """Fan out a payload to all connections authenticated as one user."""
        connection_ids = self._by_user.get(user_email, set())
        if not connection_ids:
            return
        conversation_id = payload.get("conversation_id")
        if isinstance(conversation_id, str):
            payload = await self._enrich_conversation_updated_payload(conversation_id, payload)
        coroutines = []
        for cid in list(connection_ids):
            conn = self._connections.get(cid)
            if conn is not None:
                coroutines.append(conn.send_json(payload))
        if coroutines:
            await asyncio.gather(*coroutines, return_exceptions=True)

    async def send_sidebar_update_to_owner(
        self,
        conversation_id: str,
        payload: dict[str, Any],
        *,
        include_subscribers: bool = False,
    ) -> None:
        """Fan out sidebar metadata to the conversation owner's sidebar clients.

        Conversation streams are subscription-scoped, but sidebar rows are user-scoped.
        Tabs opened on other conversations must still receive low-volume activity
        corrections so spinners and ordering do not stay stale.  By default we
        avoid duplicating frames to connections already subscribed to the
        conversation stream; REST mutations use ``include_subscribers=True``
        because there is no parallel conversation-scoped websocket event.
        """

        payload = await self._enrich_conversation_updated_payload(conversation_id, payload)
        session_cache = getattr(self.app.state, "session_cache", None)
        owner_email = (
            session_cache.get_conversation_owner(conversation_id)
            if session_cache is not None and hasattr(session_cache, "get_conversation_owner")
            else None
        )
        needs_conversation_row = (
            payload.get("type") == "sidebar_conversation_upsert" and "conversation" not in payload
        )
        session_factory = getattr(self.app.state, "session_factory", None)
        if session_factory is None and (owner_email is None or needs_conversation_row):
            return
        fanout_payload = payload
        conversation: Conversation | None = None
        try:
            if session_factory is not None and (owner_email is None or needs_conversation_row):
                async with session_factory() as db_session:
                    conversation = await get_conversation(db_session, conversation_id)
                    if conversation is None:
                        return
                    if session_cache is not None and hasattr(
                        session_cache, "remember_conversation_owner"
                    ):
                        owner_email = session_cache.remember_conversation_owner(
                            conversation_id,
                            conversation.user_email,
                        )
                    else:
                        owner_email = conversation.user_email
                    if needs_conversation_row:
                        from cognis.store.queries import get_session_row

                        active_session = (
                            await get_session_row(db_session, conversation.active_session_id)
                            if conversation.active_session_id
                            else None
                        )
                        pending_by_conversation = (
                            await list_pending_notification_types_by_conversation(
                                db_session,
                                conversation.user_email,
                                [conversation.conversation_id],
                            )
                        )
                        turn_scheduler = getattr(self.app.state, "turn_scheduler", None)
                        durable_running = (
                            getattr(turn_scheduler, "durable_running_turn_state", None)
                            if turn_scheduler is not None
                            else None
                        )
                        running_turn_state = (
                            await durable_running(conversation.conversation_id)
                            if callable(durable_running)
                            else turn_scheduler.running_turn_state(conversation.conversation_id)
                            if turn_scheduler is not None
                            and hasattr(turn_scheduler, "running_turn_state")
                            else None
                        )
                        fanout_payload = {
                            **payload,
                            "conversation": conversation_to_response(
                                conversation,
                                has_active_turn=running_turn_state is not None,
                                active_session=active_session,
                                active_turn_state=running_turn_state,
                                pending_notification_types=pending_by_conversation.get(
                                    conversation.conversation_id,
                                    [],
                                ),
                            ).model_dump(mode="json"),
                        }
        except Exception as exc:
            logger.debug(
                "Unable to resolve conversation owner for sidebar fanout",
                extra={"conversation_id": conversation_id, "error": str(exc)},
            )
            return
        if owner_email is None:
            return

        conversation_connection_ids = self._by_conversation.get(conversation_id, set())
        owner_connection_ids = self._by_user.get(owner_email, set())
        target_connection_ids = (
            set(owner_connection_ids)
            if include_subscribers
            else set(owner_connection_ids) - set(conversation_connection_ids)
        )
        if not target_connection_ids:
            return

        coroutines = []
        for cid in list(target_connection_ids):
            conn = self._connections.get(cid)
            if conn is not None:
                coroutines.append(conn.send_json(fanout_payload))
        if coroutines:
            await asyncio.gather(*coroutines, return_exceptions=True)

    async def _enrich_conversation_updated_payload(
        self,
        conversation_id: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        if payload.get("type") != "conversation_updated":
            return payload
        needs_row = "last_read_at" not in payload or "last_message_at" not in payload
        enriched = dict(payload)
        if needs_row:
            session_factory = getattr(self.app.state, "session_factory", None)
            if session_factory is not None:
                try:
                    async with session_factory() as db_session:
                        conversation = await get_conversation(db_session, conversation_id)
                    if conversation is not None:
                        enriched.setdefault(
                            "last_read_at",
                            conversation.last_read_at.isoformat()
                            if conversation.last_read_at
                            else None,
                        )
                        enriched.setdefault(
                            "last_message_at",
                            conversation.last_message_at.isoformat()
                            if conversation.last_message_at
                            else None,
                        )
                except Exception as exc:  # noqa: BLE001
                    logger.debug(
                        "Unable to enrich conversation_updated read timestamps",
                        extra={"conversation_id": conversation_id, "error": str(exc)},
                    )
        if "last_read_at" in enriched or "last_message_at" in enriched:
            enriched["has_unread"] = _has_unread_from_payload(enriched)
        return enriched

    def has_tts_enabled_subscribers(self, conversation_id: str) -> bool:
        """Return True when at least one TTS-enabled connection is subscribed."""
        connection_ids = self._by_conversation.get(conversation_id, set())
        for cid in connection_ids:
            conn = self._connections.get(cid)
            if conn is not None and conn.tts_enabled:
                return True
        return False

    async def send_to_tts_subscribers(self, conversation_id: str, payload: dict[str, Any]) -> None:
        """Fan out a payload only to TTS-enabled connections."""
        connection_ids = self._by_conversation.get(conversation_id, set())
        if not connection_ids:
            return
        coroutines = []
        for cid in list(connection_ids):
            conn = self._connections.get(cid)
            if conn is not None and conn.tts_enabled:
                coroutines.append(conn.send_json(payload))
        if coroutines:
            await asyncio.gather(*coroutines, return_exceptions=True)

    async def send_error(
        self,
        connection: AuthenticatedWebSocket,
        *,
        code: str,
        message: str,
        recoverable: bool,
    ) -> None:
        """Send an error to a specific connection."""
        await connection.send_json(
            WebSocketError(code=code, message=message, recoverable=recoverable).model_dump()
        )

    # ------------------------------------------------------------------
    # EventBus handler — UI event fanout
    # ------------------------------------------------------------------

    async def _handle_event(self, event: Event) -> None:
        """Convert EventBus events to WS payloads and fan out."""
        if event.type == EventType.CLUSTER_SCOPE_INVALIDATED:
            await self._handle_cluster_scope_invalidated(event)
            return
        # FOLLOW_UP_TURN_REQUESTED is handled by TurnScheduler
        if event.type == EventType.FOLLOW_UP_TURN_REQUESTED:
            return
        if event.type == EventType.CONVERSATION_STATE_CHANGED:
            await self._fanout_conversation_state_delta(event)
            return

        conversation_id = await self._resolve_conversation_id(event)
        if conversation_id is None:
            return

        delegation_runtime_item = _chat_v2_delegation_runtime_item(event)
        if delegation_runtime_item is not None:
            await self.send_chat_v2_runtime_to_conversation(
                conversation_id,
                volatile_items=[delegation_runtime_item],
                active_session_id=event.data.get("parent_session_id")
                if isinstance(event.data.get("parent_session_id"), str)
                else None,
            )

        is_idle_checkpoint = event.data.get("trigger") == "idle_checkpoint"
        if event.type == EventType.SESSION_COMPACTION_STARTED and is_idle_checkpoint:
            compaction_item = compaction_runtime_item(event.data)
            if compaction_item is not None:
                await self.send_chat_v2_runtime_to_conversation(
                    conversation_id,
                    volatile_items=[compaction_item],
                    active_session_id=compaction_item.session_id,
                )
        elif event.type == EventType.SESSION_COMPACTION_FINISHED and is_idle_checkpoint:
            status = event.data.get("status")
            compaction_item = compaction_runtime_item(
                event.data,
                status=status if status in {"failed", "skipped"} else "failed",
            )
            if compaction_item is not None:
                await self.send_chat_v2_runtime_to_conversation(
                    conversation_id,
                    volatile_items=[compaction_item],
                    active_session_id=compaction_item.session_id,
                )
        elif event.type == EventType.SESSION_COMPACTED and is_idle_checkpoint:
            compaction_item = compaction_runtime_item(event.data, status="compacted")
            if compaction_item is not None:
                await self.send_chat_v2_runtime_to_conversation(
                    conversation_id,
                    volatile_items=[compaction_item],
                    active_session_id=compaction_item.session_id,
                )
        elif event.type == EventType.SYSTEM_NOTICE:
            notice_id = event.data.get("notice_id")
            message = event.data.get("message")
            if isinstance(notice_id, str) and notice_id and isinstance(message, str) and message:
                session_id = event.data.get("session_id")
                await self.send_chat_v2_runtime_to_conversation(
                    conversation_id,
                    volatile_items=[
                        system_message_runtime_item(
                            notice_id=notice_id,
                            content=message,
                            turn_id=event.data.get("turn_id"),
                            session_id=session_id if isinstance(session_id, str) else None,
                            timestamp=datetime.now(UTC).isoformat(),
                            notice_kind=event.data.get("kind"),
                            notice_scope=event.data.get("scope"),
                            retry_reason=event.data.get("retry_reason"),
                            retry_source_turn_id=event.data.get("retry_source_turn_id"),
                            attempt=event.data.get("attempt"),
                        )
                    ],
                    active_session_id=session_id if isinstance(session_id, str) else None,
                )

        suppress_legacy_payload = False
        if event.type == EventType.WORKFLOW_PROGRESS and event.data.get("event") in {
            "tool_call_started",
            "tool_call_completed",
        }:
            session_id = event.data.get("session_id")
            suppress_legacy_payload = isinstance(session_id, str) and await self._is_subsession(
                session_id
            )

        payload = None if suppress_legacy_payload else _event_to_payload(event, conversation_id)
        if payload is not None:
            is_escalation = event.type in {
                EventType.ESCALATION_CREATED,
                EventType.ESCALATION_RESOLVED,
            } or (
                event.type in {EventType.NOTIFICATION_CREATED, EventType.NOTIFICATION_RESOLVED}
                and event.data.get("notification_type") == "escalation"
            )
            if is_escalation:
                await self.send_to_conversation(
                    conversation_id,
                    payload,
                    include_chat_v2=True,
                )
            else:
                await self.send_to_conversation(conversation_id, payload)
        activity_payload = self._conversation_activity_payload(event, conversation_id)
        if activity_payload is not None:
            await self.send_to_conversation(conversation_id, activity_payload)
            await self.send_sidebar_update_to_owner(conversation_id, activity_payload)
        # Fan out CONVERSATION_UPDATED events to non-subscribed owner tabs so
        # sidebar rows (title, unread state, last_message_at) stay current on
        # all open clients without requiring a subscription to every conversation.
        if event.type == EventType.CONVERSATION_UPDATED:
            conv_updated_payload = _event_to_payload(event, conversation_id)
            if conv_updated_payload is not None:
                await self.send_sidebar_update_to_owner(conversation_id, conv_updated_payload)
        attention_payload = await self._notification_attention_payload(event, conversation_id)
        if attention_payload is not None:
            await self.send_to_user(attention_payload["user_email"], attention_payload["payload"])
        if event.type in {
            EventType.STEP_STARTED,
            EventType.STEP_COMPLETED,
            EventType.STEP_FAILED,
            EventType.STEP_PAUSED,
            EventType.TASK_STARTED,
            EventType.TASK_COMPLETED,
            EventType.TASK_FAILED,
            EventType.TASK_CANCELLED,
            EventType.TASK_PAUSED,
            EventType.NOTIFICATION_CREATED,
            EventType.NOTIFICATION_RESOLVED,
        }:
            source_kind = {
                EventType.NOTIFICATION_CREATED: "task.notification.created",
                EventType.NOTIFICATION_RESOLVED: "task.notification.resolved",
            }.get(event.type, f"task.{event.type.value}")
            await self._fanout_conversation_state_delta(
                Event(
                    type=EventType.CONVERSATION_STATE_CHANGED,
                    data={**event.data, "source_kind": source_kind},
                )
            )

    def subscribed_cluster_scopes(self) -> list[dict[str, str]]:
        """Return unique server-authorized scopes for bounded reconciliation."""
        scopes: dict[str, dict[str, str]] = {}
        cluster_signals = getattr(self.app.state, "cluster_signals", None)
        for connection in self._connections.values():
            owner_token = (
                cluster_signals.owner_token(connection.user_email)
                if cluster_signals is not None
                else None
            )
            scopes.setdefault(
                f"user:{connection.user_email}",
                {
                    "user_email": connection.user_email,
                    **({"owner_token": owner_token} if owner_token else {}),
                },
            )
            for scope in connection.chat_v2_scopes.values():
                payload = {
                    key: value
                    for key in (
                        "conversation_id",
                        "session_id",
                        "task_id",
                        "step_run_id",
                    )
                    if isinstance((value := getattr(scope, key, None)), str) and value
                }
                payload["user_email"] = connection.user_email
                scopes.setdefault(scope.key, payload)
                scopes.setdefault(
                    f"work:{scope.key}",
                    {
                        "user_email": connection.user_email,
                        "work_scope_key": scope.key,
                    },
                )
        return list(scopes.values())

    async def _handle_cluster_scope_invalidated(self, event: Event) -> None:
        raw_scope = event.data.get("scope")
        revision = event.data.get("revision")
        kind = event.data.get("kind")
        if not isinstance(raw_scope, dict) or not isinstance(revision, str):
            return
        event_session_token = raw_scope.get("event_session_token")
        if kind == "event_store_session_invalidated" and isinstance(event_session_token, str):
            cached_event_store = getattr(self.app.state, "cached_event_store", None)
            if cached_event_store is not None:
                await cached_event_store.invalidate_session_token(
                    event_session_token,
                    source="cluster_signal",
                )
                connection_ids = await self._chat_v2_connection_ids_for_event_session_token(
                    event_session_token,
                    cached_event_store,
                )
                payload = {
                    "type": "scope_invalidated",
                    "reason": str(kind),
                    "revision": revision,
                }
                for connection_id in connection_ids:
                    connection = self._connections.get(connection_id)
                    if connection is not None:
                        connection.send_scope_invalidation_nowait(payload)
            return
        conversation_id = raw_scope.get("conversation_id")
        session_id = raw_scope.get("session_id")
        task_id = raw_scope.get("task_id")
        step_run_id = raw_scope.get("step_run_id")
        owner_token = raw_scope.get("owner_token")
        work_scope_key = raw_scope.get("work_scope_key")
        owner_email = await self._resolve_cluster_signal_owner(raw_scope)
        if owner_email is None and isinstance(owner_token, str):
            cluster_signals = getattr(self.app.state, "cluster_signals", None)
            if cluster_signals is not None:
                owner_email = next(
                    (
                        email
                        for email in self._by_user
                        if cluster_signals.owner_token_matches(email, owner_token)
                    ),
                    None,
                )

        if isinstance(session_id, str):
            session_cache = getattr(self.app.state, "session_cache", None)
            if session_cache is not None:
                await session_cache.invalidate_canonical(session_id)
        if isinstance(conversation_id, str):
            relay = getattr(self.app.state, "chat_v2_runtime_relay", None)
            if relay is not None:
                relay.invalidate(conversation_id)
            self._relay_runtime_items.pop(conversation_id, None)

        connection_ids: set[str] = set()
        for scope_key, subscribed in self._by_chat_v2_scope.items():
            sample = next(
                (
                    connection.chat_v2_scopes.get(scope_key)
                    for connection_id in subscribed
                    if (connection := self._connections.get(connection_id)) is not None
                ),
                None,
            )
            if sample is None:
                continue
            matches = (
                (isinstance(conversation_id, str) and sample.conversation_id == conversation_id)
                or (isinstance(session_id, str) and sample.session_id == session_id)
                or (isinstance(task_id, str) and sample.task_id == task_id)
                or (isinstance(step_run_id, str) and sample.step_run_id == step_run_id)
                or (isinstance(work_scope_key, str) and sample.key == work_scope_key)
            )
            if matches:
                connection_ids.update(subscribed)

        if (
            kind in {"sidebar_changed", "executor_state_changed", "chat_scope_changed"}
            and owner_email is not None
        ):
            connection_ids.update(self._by_user.get(owner_email, set()))

        payload = {
            "type": "work_invalidated" if kind == "work_invalidated" else "scope_invalidated",
            "reason": str(kind),
            "revision": revision,
            **{
                key: value
                for key, value in raw_scope.items()
                if key
                in {
                    "conversation_id",
                    "session_id",
                    "task_id",
                    "step_run_id",
                    "work_scope_key",
                }
                and isinstance(value, str)
            },
        }
        for connection_id in connection_ids:
            connection = self._connections.get(connection_id)
            if connection is None:
                continue
            # Scope registries were populated only after server-side authorization.
            # Owner-wide sidebar invalidations are additionally constrained here.
            if owner_email is not None and connection.user_email != owner_email:
                continue
            connection.send_scope_invalidation_nowait(payload)

    async def _chat_v2_connection_ids_for_event_session_token(
        self,
        event_session_token: str,
        cached_event_store: Any,
    ) -> set[str]:
        """Return local Chat v2 subscribers whose event store session was invalidated."""

        subscribed_scopes = [
            (connection_id, scope)
            for connection_id, connection in self._connections.items()
            for scope in connection.chat_v2_scopes.values()
            if isinstance(scope.session_id, str) and scope.session_id
        ]
        if not subscribed_scopes:
            return set()

        async with self.app.state.session_factory() as session:
            conversation_ids = {
                scope.conversation_id
                for _, scope in subscribed_scopes
                if scope.kind == "conversation"
                and isinstance(scope.conversation_id, str)
                and scope.conversation_id
            }
            active_session_ids_by_conversation: dict[str, str] = {}
            if conversation_ids:
                result = await session.execute(
                    select(Conversation.conversation_id, Conversation.active_session_id).where(
                        Conversation.conversation_id.in_(conversation_ids)
                    )
                )
                active_session_ids_by_conversation = {
                    conversation_id: active_session_id
                    for conversation_id, active_session_id in result
                    if isinstance(active_session_id, str) and active_session_id
                }
            session_id_by_scope_key = {
                scope.key: (
                    active_session_ids_by_conversation.get(scope.conversation_id, scope.session_id)
                    if scope.kind == "conversation" and scope.conversation_id
                    else scope.session_id
                )
                for _, scope in subscribed_scopes
            }
            subscribed_session_ids = set(session_id_by_scope_key.values())
            result = await session.execute(
                select(Session).where(Session.session_id.in_(subscribed_session_ids))
            )
            affected_session_ids = {
                row.session_id
                for row in result.scalars()
                if cached_event_store.session_token(
                    "intaris",
                    row.intaris_session_id or row.session_id,
                )
                == event_session_token
            }
        if not affected_session_ids:
            return set()

        return {
            connection_id
            for connection_id, scope in subscribed_scopes
            if session_id_by_scope_key[scope.key] in affected_session_ids
        }

    async def _resolve_cluster_signal_owner(self, raw_scope: dict[str, Any]) -> str | None:
        conversation_id = raw_scope.get("conversation_id")
        task_id = raw_scope.get("task_id")
        executor_id = raw_scope.get("executor_id")
        notification_id = raw_scope.get("notification_id")
        async with self.app.state.session_factory() as session:
            if isinstance(conversation_id, str):
                conversation = await session.get(Conversation, conversation_id)
                if conversation is not None:
                    return str(conversation.user_email)
            if isinstance(task_id, str):
                task = await session.get(Task, task_id)
                if task is not None:
                    return str(task.created_by)
            if isinstance(executor_id, str):
                executor = await session.get(ExecutorRow, executor_id)
                if executor is not None and executor.owner_email:
                    return str(executor.owner_email)
            if isinstance(notification_id, str):
                notification = await session.get(NotificationRow, notification_id)
                if notification is not None:
                    return str(notification.user_email)
        return None

    async def _send_conversation_state_snapshot(
        self,
        connection: AuthenticatedWebSocket,
        conversation_id: str,
        *,
        active_session_last_seq: int | None = None,
    ) -> None:
        async with self.app.state.session_factory() as session:
            snapshot = await snapshot_for_conversation(
                session,
                user_email=connection.user_email,
                conversation_id=conversation_id,
                turn_scheduler=getattr(self.app.state, "turn_scheduler", None),
                active_session_last_seq=active_session_last_seq,
            )
        if snapshot is None:
            return
        await connection.send_json(
            {
                "type": "conversation_state_snapshot",
                "conversation_id": conversation_id,
                "state": snapshot.model_dump(mode="json"),
            }
        )

    async def _fanout_conversation_state_delta(self, event: Event) -> None:
        source_kind = event.data.get("source_kind")
        if not isinstance(source_kind, str) or not source_kind:
            source_kind = "task.state.changed"
        if source_kind == "session.todos.changed":
            conversation_id = event.data.get("conversation_id")
            user_email = event.data.get("user_email")
            if not isinstance(conversation_id, str) or not isinstance(user_email, str):
                return
            async with self.app.state.session_factory() as session:
                snapshot = await snapshot_for_conversation(
                    session,
                    user_email=user_email,
                    conversation_id=conversation_id,
                    turn_scheduler=getattr(self.app.state, "turn_scheduler", None),
                )
            if snapshot is None:
                return
            delta = build_state_delta(
                conversation_id=conversation_id,
                source_kind=source_kind,
                changed_paths=["state", "active_session.todos"],
                replace={"state": snapshot.model_dump(mode="json")},
            )
            payload = {
                "type": "conversation_state_delta",
                "conversation_id": conversation_id,
                **delta.model_dump(mode="json"),
            }
            await self.send_to_conversation(conversation_id, payload)
            await self.send_sidebar_update_to_owner(conversation_id, payload)
            return

        task_id = event.data.get("task_id")
        user_email = event.data.get("user_email")
        if not isinstance(task_id, str):
            return
        if not isinstance(user_email, str) or not user_email:
            async with self.app.state.session_factory() as session:
                task = await get_task(session, task_id)
            if task is None:
                return
            user_email = task.created_by
        step_run_id = event.data.get("step_run_id")
        if not isinstance(step_run_id, str):
            step_run_id = None
        async with self.app.state.session_factory() as session:
            conversation_ids = await linked_conversation_ids_for_task(
                session,
                user_email=user_email,
                task_id=task_id,
                step_run_id=step_run_id if source_kind == "task_step.todos.changed" else None,
            )
            conversation_rows = {}
            if conversation_ids:
                conversation_result = await session.execute(
                    select(Conversation)
                    .where(Conversation.user_email == user_email)
                    .where(Conversation.status != "deleted")
                    .where(Conversation.conversation_id.in_(conversation_ids))
                )
                conversation_rows = {
                    row.conversation_id: row for row in conversation_result.scalars().all()
                }
            snapshots = {
                conversation_id: snapshot
                for conversation_id in conversation_ids
                if (conversation := conversation_rows.get(conversation_id)) is not None
                and (
                    snapshot := await snapshot_for_conversation(
                        session,
                        user_email=user_email,
                        conversation_id=conversation_id,
                        turn_scheduler=getattr(self.app.state, "turn_scheduler", None),
                        conversation=conversation,
                    )
                )
                is not None
            }
        for conversation_id, snapshot in snapshots.items():
            changed_paths = ["state"]
            if source_kind == "task_step.todos.changed":
                changed_paths.append("task.relevant_step.todos")
            elif source_kind.startswith("task.notification."):
                changed_paths.append("pending")
            else:
                changed_paths.append("task")
            delta = build_state_delta(
                conversation_id=conversation_id,
                source_kind=source_kind,
                task_id=task_id,
                step_run_id=step_run_id,
                changed_paths=changed_paths,
                replace={"state": snapshot.model_dump(mode="json")},
            )
            payload = {
                "type": "conversation_state_delta",
                "conversation_id": conversation_id,
                **delta.model_dump(mode="json"),
            }
            await self.send_to_conversation(conversation_id, payload)
            await self.send_sidebar_update_to_owner(conversation_id, payload)

    def _conversation_activity_payload(
        self,
        event: Event,
        conversation_id: str,
    ) -> dict[str, Any] | None:
        """Build an authoritative sidebar activity correction payload."""

        if event.type == EventType.TURN_STARTED:
            started_at = event.data.get("started_at") or (
                event.timestamp.isoformat() if event.timestamp else None
            )
            payload = {
                "type": "conversation_updated",
                "conversation_id": conversation_id,
                "has_active_turn": True,
                "active_turn_chat_mode": event.data.get("chat_mode"),
                "active_turn_chat_mode_source": event.data.get("chat_mode_source"),
            }
            if started_at is not None:
                payload["last_message_at"] = started_at
                payload["updated_at"] = started_at
            return payload

        if event.type == EventType.TURN_COMPLETED:
            completed_at = event.data.get("completed_at") or (
                event.timestamp.isoformat() if event.timestamp else None
            )
            continuation_pending = bool(event.data.get("managed_continuation_pending"))
            completion_payload: dict[str, Any] = {
                "type": "conversation_updated",
                "conversation_id": conversation_id,
                "has_active_turn": continuation_pending,
                "active_turn_chat_mode": event.data.get("chat_mode")
                if continuation_pending
                else None,
                "active_turn_chat_mode_source": event.data.get("chat_mode_source")
                if continuation_pending
                else None,
            }
            if completed_at is not None:
                completion_payload["last_message_at"] = completed_at
                completion_payload["updated_at"] = completed_at
            return completion_payload

        if event.type in (EventType.TURN_ERROR, EventType.TASK_PAUSED):
            return {
                "type": "conversation_updated",
                "conversation_id": conversation_id,
                "has_active_turn": False,
                "active_turn_chat_mode": None,
                "active_turn_chat_mode_source": None,
            }

        return None

    async def _notification_attention_payload(
        self,
        event: Event,
        conversation_id: str,
    ) -> dict[str, Any] | None:
        """Build a user-wide sidebar refresh payload for notification events."""
        if event.type not in (EventType.NOTIFICATION_CREATED, EventType.NOTIFICATION_RESOLVED):
            return None
        user_email = event.data.get("user_email")
        if not isinstance(user_email, str) or not user_email:
            return None

        async with self.app.state.session_factory() as session:
            pending_by_conversation = await list_pending_notification_types_by_conversation(
                session,
                user_email,
                [conversation_id],
            )
        scheduler = getattr(self.app.state, "turn_scheduler", None)
        durable_running = (
            getattr(scheduler, "durable_running_turn_state", None)
            if scheduler is not None
            else None
        )
        running_turn_state = (
            await durable_running(conversation_id)
            if callable(durable_running)
            else scheduler.running_turn_state(conversation_id)
            if scheduler is not None and hasattr(scheduler, "running_turn_state")
            else None
        )
        has_active_turn = running_turn_state is not None
        return {
            "user_email": user_email,
            "payload": {
                "type": "conversation_updated",
                "conversation_id": conversation_id,
                "pending_notification_types": pending_by_conversation.get(conversation_id, []),
                "has_active_turn": has_active_turn,
                "active_turn_chat_mode": (
                    running_turn_state.get("chat_mode") if running_turn_state else None
                ),
                "active_turn_chat_mode_source": (
                    running_turn_state.get("chat_mode_source") if running_turn_state else None
                ),
            },
        }

    async def _is_subsession(self, session_id: str) -> bool:
        """Return True if the session is a sub-session (has parent_session_id set).

        Sub-session tool_call/tool_result events must not be projected into the
        parent conversation's main timeline — they belong in the sub-session
        detail panel. The delegation cards (DELEGATION_*) are the correct
        parent-visible artifact for child session activity.
        """
        from cognis.store.queries import get_session_row

        try:
            async with self.app.state.session_factory() as session:
                session_row = await get_session_row(session, session_id)
            return session_row is not None and session_row.parent_session_id is not None
        except Exception:  # noqa: BLE001
            return False

    async def _resolve_conversation_id(self, event: Event) -> str | None:
        """Resolve the conversation_id from an event."""
        if isinstance(event.data.get("conversation_id"), str):
            return str(event.data["conversation_id"])
        session_id = event.data.get("session_id")
        if isinstance(session_id, str):
            from cognis.store.queries import get_session_row

            async with self.app.state.session_factory() as session:
                session_row = await get_session_row(session, session_id)
            if session_row is not None:
                return session_row.conversation_id
        task_id = event.data.get("task_id")
        if not isinstance(task_id, str):
            return None
        async with self.app.state.session_factory() as session:
            task_row = await get_task(session, task_id)
        if task_row is None or task_row.source_type != "chat":
            return None
        return task_row.source_ref

    # ------------------------------------------------------------------
    # Reconnection / replay
    # ------------------------------------------------------------------

    async def replay(
        self,
        connection: AuthenticatedWebSocket,
        *,
        conversation_id: str,
        last_seq: int,
        client_session_id: str | None = None,
        chat_v2_cursor: str | None = None,
    ) -> None:
        """Replay missed events for a reconnecting client."""
        from cognis.core.session import _to_session_model
        from cognis.store.queries import (
            get_conversation,
            get_session_row,
            update_conversation_active_session,
        )

        async with self.app.state.session_factory() as db_session:
            conversation_row = await get_conversation(db_session, conversation_id)
            if conversation_row is None:
                await self.send_error(
                    connection,
                    code="not_found",
                    message="Conversation not found",
                    recoverable=False,
                )
                return
            if not _can_access_owner(connection, conversation_row.user_email):
                await self.send_error(
                    connection,
                    code="forbidden",
                    message="Conversation access denied",
                    recoverable=False,
                )
                return
            session_row = (
                await get_session_row(db_session, conversation_row.active_session_id)
                if conversation_row.active_session_id
                else None
            )
            if conversation_row.active_session_id and session_row is None:
                await update_conversation_active_session(db_session, conversation_id, None)
                await db_session.commit()

        self.subscribe(connection, conversation_id)
        if chat_v2_cursor:
            self.subscribe_chat_v2(
                connection,
                TimelineScope(
                    key=f"conversation:{conversation_id}",
                    kind="conversation",
                    conversation_id=conversation_id,
                    session_id=conversation_row.active_session_id,
                    status=conversation_row.status,
                ),
                cursor=chat_v2_cursor,
            )
        await self.send_queue_snapshot(connection, conversation_id)

        if session_row is None:
            await self._send_conversation_state_snapshot(connection, conversation_id)
            return

        session = _to_session_model(session_row)
        if client_session_id and client_session_id != session.session_id:
            last_seq = 0

        try:
            result = await self.app.state.providers.guardrails.read_events(
                session_id=session.intaris_session_id or session.session_id,
                after_seq=last_seq,
                limit=DEFAULT_REPLAY_LIMIT,
                allow_missing_stream=True,
            )
        except CircuitBreakerError:
            await self.send_error(
                connection,
                code="event_store_unavailable",
                message="Session event store is temporarily unavailable; realtime connection remains active.",
                recoverable=True,
            )
            await self._send_conversation_runtime_snapshot(
                connection,
                conversation_id,
                active_session_id=session.session_id,
            )
            return
        replayed = 0
        async with self.app.state.session_factory() as _artifact_session:
            artifact_store = self.app.state.artifact_store
            for item in result.events:
                event_type = item.get("type")
                data = item.get("data", {})
                if event_type == "user_message":
                    replay_data = dict(data)
                    raw_attachments = replay_data.get("attachments")
                    replay_attachments = (
                        raw_attachments if isinstance(raw_attachments, list) else []
                    )
                    attachments = await hydrate_attachment_refs(
                        self.app.state.session_factory,
                        artifact_store,
                        replay_attachments,
                        owner_email=connection.user_email,
                        conversation_id=conversation_id,
                        session_id=session.session_id,
                    )
                    replay_data["attachments"] = attachments
                    replay_data.setdefault("session_id", session.session_id)
                    replayed += 1
                elif event_type == "assistant_message":
                    replay_data = dict(data)
                    turn_id = data.get("turn_id") if isinstance(data.get("turn_id"), str) else None
                    raw_attachments = replay_data.get("attachments")
                    replay_attachments = (
                        raw_attachments if isinstance(raw_attachments, list) else []
                    )
                    attachments = await hydrate_attachment_refs(
                        self.app.state.session_factory,
                        artifact_store,
                        replay_attachments,
                        owner_email=connection.user_email,
                        conversation_id=conversation_id,
                        session_id=session.session_id,
                    )
                    replay_data["attachments"] = attachments
                    replay_data.setdefault("session_id", session.session_id)
                    if turn_id is not None:
                        replay_data.setdefault("message_id", turn_id)
                    replayed += 1
                elif event_type == "tool_call":
                    replay_data = dict(data)
                    arguments = data.get("arguments")
                    if isinstance(arguments, str):
                        with contextlib.suppress(Exception):
                            arguments = json.loads(arguments)
                    replay_data["arguments"] = arguments
                    replay_data.setdefault("session_id", session.session_id)
                    replayed += 1
                elif event_type == "tool_result":
                    replay_data = dict(data)
                    raw_attachments = replay_data.get("attachments")
                    replay_attachments = (
                        raw_attachments if isinstance(raw_attachments, list) else []
                    )
                    attachments = await hydrate_attachment_refs(
                        self.app.state.session_factory,
                        artifact_store,
                        replay_attachments,
                        owner_email=connection.user_email,
                        conversation_id=conversation_id,
                        session_id=session.session_id,
                    )
                    replay_data["attachments"] = attachments
                    replay_data.setdefault("session_id", session.session_id)
                    replayed += 1
                elif event_type == "assistant_thinking":
                    replay_data = dict(data)
                    replay_data.setdefault("session_id", session.session_id)
                    replayed += 1
                elif event_type == "task_result":
                    await connection.send_json(
                        {
                            "type": "workflow_completed",
                            "conversation_id": conversation_id,
                            "task_id": data.get("task_id"),
                            "result": data.get("result_summary"),
                        }
                    )
                    replayed += 1
                elif event_type == "task_failed":
                    await connection.send_json(
                        {
                            "type": "workflow_failed",
                            "conversation_id": conversation_id,
                            "task_id": data.get("task_id"),
                            "reason": data.get("result_summary"),
                        }
                    )
                    replayed += 1
                elif event_type == "task_cancelled":
                    await connection.send_json(
                        {
                            "type": "workflow_cancelled",
                            "conversation_id": conversation_id,
                            "task_id": data.get("task_id"),
                            "reason": data.get("result_summary") or "cancelled",
                        }
                    )
                    replayed += 1
                elif event_type == "workflow_composed" or (
                    event_type == "lifecycle" and data.get("event") == "workflow_composed"
                ):
                    await connection.send_json(_workflow_composed_payload(conversation_id, data))
                    replayed += 1
                elif event_type == "delegation":
                    status = data.get("status")
                    if status == "completed":
                        await connection.send_json(
                            {
                                "type": "delegation_completed",
                                "conversation_id": conversation_id,
                                "child_session_id": data.get("child_session_id"),
                                "agent_id": data.get("agent_id"),
                                "task": data.get("title")
                                or data.get("task_title")
                                or data.get("task"),
                                "title": data.get("title") or data.get("task_title"),
                                "task_title": data.get("task_title") or data.get("title"),
                                "used_agent_id": data.get("used_agent_id"),
                                "duration_ms": data.get("duration_ms"),
                                "result": data.get("result_summary"),
                                "turn_id": data.get("turn_id"),
                            }
                        )
                    elif status == "failed":
                        await connection.send_json(
                            {
                                "type": "delegation_failed",
                                "conversation_id": conversation_id,
                                "child_session_id": data.get("child_session_id"),
                                "agent_id": data.get("agent_id"),
                                "task": data.get("title")
                                or data.get("task_title")
                                or data.get("task"),
                                "title": data.get("title") or data.get("task_title"),
                                "task_title": data.get("task_title") or data.get("title"),
                                "used_agent_id": data.get("used_agent_id"),
                                "duration_ms": data.get("duration_ms"),
                                "reason": data.get("error"),
                                "recoverable": data.get("recoverable"),
                                "turn_id": data.get("turn_id"),
                            }
                        )
                    else:
                        await connection.send_json(
                            {
                                "type": "delegation_started",
                                "conversation_id": conversation_id,
                                "parent_session_id": session.session_id,
                                "child_session_id": data.get("child_session_id"),
                                "mode": data.get("mode"),
                                "agent_id": data.get("agent_id"),
                                "task": data.get("title")
                                or data.get("task_title")
                                or data.get("task"),
                                "title": data.get("title") or data.get("task_title"),
                                "task_title": data.get("task_title") or data.get("title"),
                                "used_agent_id": data.get("used_agent_id"),
                                "input_redacted": data.get("input_redacted"),
                                "turn_id": data.get("turn_id"),
                            }
                        )
                    replayed += 1
                elif event_type == "system_message":
                    if not is_visible_persisted_system_message(data):
                        continue
                    await connection.send_json(
                        {
                            "type": "system_message",
                            "conversation_id": conversation_id,
                            "seq": item.get("seq"),
                            "text": str(
                                data.get("content") or data.get("text") or data.get("message") or ""
                            ),
                            "turn_id": data.get("turn_id"),
                            "notice_id": data.get("notice_id"),
                            "kind": data.get("kind"),
                            "scope": data.get("scope"),
                            "retry_reason": data.get("retry_reason"),
                            "retry_source_turn_id": data.get("retry_source_turn_id"),
                            "attempt": data.get("attempt"),
                        }
                    )
                    replayed += 1
                elif event_type == "lifecycle" and data.get("event") == "system_notice":
                    if is_transient_compaction_start_notice(data):
                        continue
                    await connection.send_json(
                        {
                            "type": "system_message",
                            "conversation_id": conversation_id,
                            "seq": item.get("seq"),
                            "text": str(data.get("message", "")),
                            "turn_id": data.get("turn_id"),
                            "notice_id": data.get("notice_id"),
                            "kind": data.get("kind"),
                            "retry_reason": data.get("retry_reason"),
                            "retry_source_turn_id": data.get("retry_source_turn_id"),
                            "attempt": data.get("attempt"),
                            "scope": data.get("scope"),
                        }
                    )
                    replayed += 1
                elif event_type == "evaluation" and data.get("event") == "evaluation_feedback":
                    await connection.send_json(
                        {
                            "type": "history_notice",
                            "conversation_id": conversation_id,
                            "seq": item.get("seq"),
                            "title": f"Step Evaluation (attempt {data.get('attempt', '?')})",
                            "description": f"{data.get('decision', 'unknown')} — {data.get('feedback', '')}",
                            "tone": "info"
                            if data.get("decision") in {"approved", "approve"}
                            else "error"
                            if data.get("decision") in {"failed", "reject"}
                            else "warning",
                        }
                    )
                    replayed += 1
                elif event_type == "history_gap":
                    await connection.send_json(
                        {
                            "type": "history_notice",
                            "conversation_id": conversation_id,
                            "seq": item.get("seq"),
                            "title": "History incomplete",
                            "description": f"History gap detected: {data.get('reason', 'unknown')}.",
                            "tone": "warning",
                        }
                    )
                    replayed += 1

        if (
            session.session_id in set(getattr(self.app.state, "recovered_session_ids", []))
            and session.session_id not in connection.recovery_notified
        ):
            connection.recovery_notified.add(session.session_id)
            await connection.send_json(
                {
                    "type": "session_recovered",
                    "conversation_id": conversation_id,
                    "session_id": session.session_id,
                    "reason": "controller_restart",
                }
            )

        turn_scheduler = getattr(self.app.state, "turn_scheduler", None)
        has_active_turn = bool(turn_scheduler and turn_scheduler.has_running_turn(conversation_id))
        await self._send_conversation_state_snapshot(
            connection,
            conversation_id,
            active_session_last_seq=result.last_seq,
        )
        await self._send_conversation_runtime_snapshot(
            connection,
            conversation_id,
            active_session_id=session.session_id,
        )

        await connection.send_json(
            {
                "type": "reconnected",
                "conversation_id": conversation_id,
                "session_id": session.session_id,
                "missed_events_count": replayed,
                "last_seq": result.last_seq,
                "has_active_turn": has_active_turn,
            }
        )

        pending_pauses = await _load_pending_task_prompts(self.app, conversation_id)
        for payload in pending_pauses:
            await connection.send_json(payload)

        WS_RECONNECTIONS_TOTAL.inc()
        WS_MISSED_EVENTS_REPLAYED.inc(replayed)


# ---------------------------------------------------------------------------
# WebSocket handler
# ---------------------------------------------------------------------------


def _allowed_websocket_origins(websocket: WebSocket) -> set[str]:
    config = getattr(websocket.app.state, "config", None)
    allowed: set[str] = set()
    for origin in getattr(config, "cors_origins", []) or []:
        if origin:
            allowed.add(str(origin).rstrip("/"))
    public_base_url = str(getattr(config, "public_base_url", "") or "").rstrip("/")
    if public_base_url:
        allowed.add(public_base_url)
    host = websocket.headers.get("host", "").strip()
    if host:
        forwarded = websocket.headers.get("x-forwarded-proto", "").split(",", 1)[0].strip().lower()
        scheme = forwarded or ("https" if websocket.url.scheme == "wss" else "http")
        allowed.add(f"{scheme}://{host}".rstrip("/"))
    return allowed


def _origin_allowed(websocket: WebSocket, origin: str) -> bool:
    return origin.rstrip("/") in _allowed_websocket_origins(websocket)


async def _authenticate_browser_session(websocket: WebSocket) -> dict[str, Any] | None:
    raw_token = websocket.cookies.get(COOKIE_NAME)
    if not raw_token:
        return None

    origin = websocket.headers.get("origin")
    if origin and not _origin_allowed(websocket, origin):
        await websocket.close(code=4403, reason="Origin not allowed")
        return None

    async with websocket.app.state.session_factory() as session:
        browser_session = await get_browser_session_by_token(session, raw_token)
        if browser_session is None or browser_session.revoked_at is not None:
            await websocket.close(code=4401, reason="Invalid session")
            return None
        expires_at = browser_session.expires_at
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=UTC)
        if expires_at < datetime.now(UTC):
            await websocket.close(code=4401, reason="Session expired")
            return None
        user = await get_user(session, browser_session.user_email)
        if user is None:
            await websocket.close(code=4401, reason="Unknown session owner")
            return None
        if not user.is_active:
            await websocket.close(code=4403, reason="Account disabled")
            return None
        return {"sub": user.email, "role": user.role, "name": user.name, "typ": "session"}


async def _authenticate_websocket(websocket: WebSocket) -> dict[str, Any] | None:
    browser_claims = await _authenticate_browser_session(websocket)
    if browser_claims is not None:
        return browser_claims

    timeout_seconds = getattr(websocket.app.state, "ws_auth_timeout_seconds", 10)
    try:
        first_message = await asyncio.wait_for(websocket.receive_json(), timeout=timeout_seconds)
    except TimeoutError:
        await websocket.close(code=4401, reason="Authentication timeout")
        return None
    except WebSocketDisconnect:
        return None

    if first_message.get("type") != "auth" or not isinstance(first_message.get("token"), str):
        await websocket.close(code=4401, reason="Authentication required")
        return None

    try:
        return cast(
            dict[str, Any],
            websocket.app.state.auth_provider.verify_jwt(
                first_message["token"],
                audience=["cognis"],
            ),
        )
    except Exception:
        await websocket.close(code=4401, reason="Invalid token")
        return None


async def handle_websocket(websocket: WebSocket) -> None:
    """Main WebSocket handler — auth, message loop, dispatch."""
    await websocket.accept()
    claims = await _authenticate_websocket(websocket)
    if claims is None:
        return

    manager = getattr(websocket.app.state, "ws_manager", None)
    if manager is None:
        manager = WebSocketConnectionManager(websocket.app)
        websocket.app.state.ws_manager = manager

    connection = await manager.connect(websocket, claims=claims)
    await connection.send_json(WebSocketAuthenticated().model_dump())

    try:
        while True:
            message = await websocket.receive_json()
            if message.get("type") != "ping" and not connection.allow_inbound_message():
                await manager.send_error(
                    connection,
                    code="rate_limited",
                    message="Too many WebSocket messages",
                    recoverable=True,
                )
                continue

            message_type = message.get("type")

            if message_type == "ping":
                await connection.send_json(WebSocketPong().model_dump())
                continue

            if message_type == "chat_v2_subscribe":
                await _handle_chat_v2_subscribe(websocket.app, manager, connection, message)
                continue

            if message_type == "chat_v2_unsubscribe":
                await _handle_chat_v2_unsubscribe(websocket.app, manager, connection, message)
                continue

            if message_type == "enable_tts":
                voice = message.get("voice")
                connection.tts_enabled = True
                connection.tts_voice = voice if isinstance(voice, str) and voice.strip() else None
                continue

            if message_type == "disable_tts":
                connection.tts_enabled = False
                connection.tts_voice = None
                continue

            await manager.send_error(
                connection,
                code="validation_error",
                message="Unsupported WebSocket message type",
                recoverable=True,
            )
    except WebSocketDisconnect:
        await manager.disconnect(connection)
    except Exception:
        logger.exception("WebSocket handler failed")
        await manager.disconnect(connection)


# ---------------------------------------------------------------------------
# Message dispatch handlers
# ---------------------------------------------------------------------------


async def _handle_chat_v2_subscribe(
    app: Any,
    manager: WebSocketConnectionManager,
    connection: AuthenticatedWebSocket,
    message: dict[str, Any],
) -> None:
    """Opt a websocket connection into Chat v2 realtime frames."""

    raw_scope = message.get("scope")
    cursor = message.get("cursor")
    if not isinstance(raw_scope, dict) or not isinstance(cursor, str) or not cursor:
        await manager.send_error(
            connection,
            code="validation_error",
            message="scope and cursor are required",
            recoverable=True,
        )
        return
    try:
        scope = TimelineScope.model_validate(raw_scope)
    except ValueError:
        await manager.send_error(
            connection,
            code="validation_error",
            message="Invalid Chat v2 timeline scope",
            recoverable=True,
        )
        return
    scope = await _rehydrate_chat_v2_scope(app, scope)
    if scope is None:
        await manager.send_error(
            connection,
            code="forbidden",
            message="Chat v2 timeline scope access denied",
            recoverable=False,
        )
        return
    scope._server_authoritative = True
    if not await _authorize_chat_v2_scope(app, manager, connection, scope):
        return
    if scope.missing_stream:
        return
    cursor_secret = getattr(app.state, "chat_v2_cursor_secret", None)
    if not isinstance(cursor_secret, str) or not cursor_secret:
        await manager.send_error(
            connection,
            code="cursor_invalid",
            message="Chat v2 cursor validation is unavailable",
            recoverable=True,
        )
        return
    try:
        # A websocket cursor is a continuation token, not merely an opaque
        # client hint. Validate its signature, projection, expiry, and exact
        # scope before mutating the subscription registries.
        validate_cursor(
            cursor,
            cursor_secret,
            scope_key=scope.key,
            projection_version=current_projection_version(),
        )
    except ChatCursorError as exc:
        await manager.send_error(
            connection,
            code=exc.code,
            message=str(exc),
            recoverable=True,
        )
        return
    # A missing-stream task step can be a durable task record without either
    # conversation or session lineage. It remains readable over REST, but
    # there is no realtime stream to subscribe to.
    if not scope.conversation_id:
        return
    manager.subscribe_chat_v2(connection, scope, cursor=cursor)
    await manager.send_chat_v2_scope_runtime_snapshot(connection, scope)


async def _handle_chat_v2_unsubscribe(
    app: Any,
    manager: WebSocketConnectionManager,
    connection: AuthenticatedWebSocket,
    message: dict[str, Any],
) -> None:
    """Opt a websocket connection out of Chat v2 realtime frames."""

    scope_key = message.get("scope_key")
    if not isinstance(scope_key, str) or not scope_key:
        await manager.send_error(
            connection,
            code="validation_error",
            message="scope_key is required",
            recoverable=True,
        )
        return
    # Unsubscribe is an ownership operation, not an authorization operation.
    # The backing task/session/conversation may have been deleted since the
    # scope was registered, but this connection must still be able to remove
    # its own registry entry. A connection with no such key is a no-op and
    # cannot affect another connection's scope.
    if scope_key not in connection.chat_v2_scopes:
        return
    manager.unsubscribe_chat_v2(connection, scope_key)


async def _handle_message(
    app: Any,
    manager: WebSocketConnectionManager,
    connection: AuthenticatedWebSocket,
    message: dict[str, Any],
) -> None:
    """Handle a 'message' type WebSocket frame."""
    conversation_id = message.get("conversation_id")
    content = message.get("content")
    attachments = message.get("attachments")
    client_message_id = message.get("client_message_id")
    if not isinstance(client_message_id, str) or len(client_message_id) > 128:
        client_message_id = None
    if not isinstance(attachments, list):
        attachments = []
    if len(attachments) > 20:
        await manager.send_error(
            connection,
            code="validation_error",
            message="Too many attachments",
            recoverable=True,
        )
        return
    if (
        not isinstance(conversation_id, str)
        or not isinstance(content, str)
        or (not content.strip() and len(attachments) == 0)
    ):
        await manager.send_error(
            connection,
            code="validation_error",
            message="conversation_id and content are required",
            recoverable=True,
        )
        return

    if not await _authorize_conversation_frame(
        app,
        manager,
        connection,
        conversation_id,
        require_mutation=True,
    ):
        return

    # Subscribe only after authorization, because queue snapshots include user content.
    manager.subscribe(connection, conversation_id)
    await manager.send_queue_snapshot(connection, conversation_id)

    # Try slash command dispatch first
    command_dispatcher = getattr(app.state, "command_dispatcher", None)
    turn_scheduler = getattr(app.state, "turn_scheduler", None)

    if command_dispatcher is not None and content.strip().startswith("/"):
        # Load minimal runtime for command dispatch
        from cognis.api.serializers import agent_to_response
        from cognis.core.session import _to_conversation_model, _to_session_model
        from cognis.store.queries import get_agent, get_conversation, get_session_row

        session_manager = getattr(app.state, "session_manager", None)
        async with app.state.session_factory() as db_session:
            conversation_row = await get_conversation(db_session, conversation_id)
            if conversation_row is None:
                await manager.send_error(
                    connection,
                    code="not_found",
                    message="Conversation not found",
                    recoverable=False,
                )
                return
            agent_row = await get_agent(db_session, conversation_row.agent_id)
            if agent_row is None:
                await manager.send_error(
                    connection, code="not_found", message="Agent not found", recoverable=False
                )
                return
            agent_model = AgentDefinition.model_validate(agent_to_response(agent_row).model_dump())
            conversation_model = _to_conversation_model(conversation_row)
            session_row = (
                await get_session_row(db_session, conversation_row.active_session_id)
                if conversation_row.active_session_id
                else None
            )

        session_model = _to_session_model(session_row) if session_row else None
        if session_model is None and session_manager is not None:
            session_model = await session_manager.ensure_root_session(
                conversation_id=conversation_id,
                user_email=connection.user_email,
                agent_id=conversation_model.agent_id,
                intention=content,
            )
            conversation_model = conversation_model.model_copy(
                update={"active_session_id": session_model.session_id}
            )

        # Try command dispatch
        if session_model is not None:
            durable_running = (
                getattr(turn_scheduler, "durable_running_turn_state", None)
                if turn_scheduler is not None
                else None
            )
            runtime_turn = (
                await durable_running(conversation_id)
                if callable(durable_running)
                else turn_scheduler.running_turn_state(conversation_id)
                if turn_scheduler is not None
                else None
            )
            has_active = runtime_turn is not None
            has_busy = has_active or (
                turn_scheduler.has_active_turn(conversation_id) if turn_scheduler else False
            )
            cmd_result = await command_dispatcher.dispatch(
                content,
                conversation=conversation_model,
                session=session_model,
                agent=agent_model,
                user_email=connection.user_email,
                has_active_turn=has_active,
                has_busy_turn=has_busy,
            )
            if cmd_result is not None:
                await _render_command_result(
                    manager,
                    conversation_id,
                    cmd_result,
                    app=app,
                    session=session_model,
                    agent=agent_model,
                    user_email=connection.user_email,
                )
                return

    # Not a command — submit to TurnScheduler
    if turn_scheduler is not None:
        error = await turn_scheduler.submit_turn(
            conversation_id,
            content,
            user_email=connection.user_email,
            attachments=[item for item in attachments if isinstance(item, dict)],
            client_message_id=client_message_id,
        )
        if error is not None:
            await manager.send_to_conversation(
                conversation_id,
                WebSocketError(
                    code=error.code,
                    message=error.message,
                    recoverable=error.recoverable,
                    error_detail=error.detail.get("error_detail") if error.detail else None,
                    detail=error.detail,
                ).model_dump(),
            )
        else:
            try:
                async with app.state.session_factory() as db_session:
                    from cognis.store.queries import get_conversation

                    conversation_row = await get_conversation(db_session, conversation_id)
                    await mark_artifacts_attached(
                        db_session,
                        [
                            str(item.get("artifact_id"))
                            for item in attachments
                            if isinstance(item, dict) and item.get("artifact_id")
                        ],
                        owner_email=connection.user_email,
                        conversation_id=conversation_id,
                        session_id=conversation_row.active_session_id if conversation_row else None,
                    )
                    await db_session.commit()
            except Exception:
                logger.warning(
                    "websocket: failed to persist post-submit attachment association",
                    extra={"extra_data": {"conversation_id": conversation_id}},
                    exc_info=True,
                )
    else:
        await manager.send_error(
            connection,
            code="internal_error",
            message="Turn scheduler not available",
            recoverable=False,
        )


async def _handle_cancel(
    app: Any,
    manager: WebSocketConnectionManager,
    connection: AuthenticatedWebSocket,
    message: dict[str, Any],
) -> None:
    """Handle a 'cancel' type WebSocket frame."""
    conversation_id = message.get("conversation_id")
    if not isinstance(conversation_id, str):
        await manager.send_error(
            connection,
            code="validation_error",
            message="conversation_id is required",
            recoverable=True,
        )
        return

    turn_scheduler = getattr(app.state, "turn_scheduler", None)
    if turn_scheduler is None:
        return

    cancelled = await turn_scheduler.cancel_turn(conversation_id, clear_queue=False)
    if cancelled:
        await manager.send_to_conversation(
            conversation_id,
            {
                "type": "system_message",
                "conversation_id": conversation_id,
                "text": "User stopped the current turn.",
            },
        )
    else:
        await manager.send_to_conversation(
            conversation_id,
            {
                "type": "system_message",
                "conversation_id": conversation_id,
                "text": "No active turn to cancel.",
            },
        )


async def _handle_cancel_queued_message(
    app: Any,
    manager: WebSocketConnectionManager,
    connection: AuthenticatedWebSocket,
    message: dict[str, Any],
) -> None:
    """Handle a targeted queued-message cancellation frame."""
    conversation_id = message.get("conversation_id")
    queue_id = message.get("queue_id")
    if not isinstance(conversation_id, str) or not isinstance(queue_id, str):
        await manager.send_error(
            connection,
            code="validation_error",
            message="conversation_id and queue_id are required",
            recoverable=True,
        )
        return
    if len(queue_id) > 128:
        await manager.send_error(
            connection,
            code="validation_error",
            message="queue_id is too long",
            recoverable=True,
        )
        return

    if not await _authorize_conversation_frame(
        app, manager, connection, conversation_id, require_mutation=True
    ):
        return
    turn_scheduler = getattr(app.state, "turn_scheduler", None)
    if turn_scheduler is None:
        await manager.send_error(
            connection,
            code="internal_error",
            message="Turn scheduler not available",
            recoverable=False,
        )
        return

    cancelled = await turn_scheduler.cancel_queued_message(conversation_id, queue_id)
    if not cancelled:
        await manager.send_error(
            connection,
            code="not_found",
            message="Queued message not found",
            recoverable=True,
        )


async def _handle_update_queued_message(
    app: Any,
    manager: WebSocketConnectionManager,
    connection: AuthenticatedWebSocket,
    message: dict[str, Any],
) -> None:
    """Handle a targeted queued-message text update frame."""
    conversation_id = message.get("conversation_id")
    queue_id = message.get("queue_id")
    content = message.get("content")
    if not isinstance(conversation_id, str) or not isinstance(queue_id, str):
        await manager.send_error(
            connection,
            code="validation_error",
            message="conversation_id and queue_id are required",
            recoverable=True,
        )
        return
    if not isinstance(content, str) or not content.strip():
        await manager.send_error(
            connection,
            code="validation_error",
            message="content is required",
            recoverable=True,
        )
        return
    if len(queue_id) > 128 or len(content) > 100_000:
        await manager.send_error(
            connection,
            code="validation_error",
            message="Queued message update is too large",
            recoverable=True,
        )
        return

    if not await _authorize_conversation_frame(
        app, manager, connection, conversation_id, require_mutation=True
    ):
        return
    turn_scheduler = getattr(app.state, "turn_scheduler", None)
    if turn_scheduler is None:
        await manager.send_error(
            connection,
            code="internal_error",
            message="Turn scheduler not available",
            recoverable=False,
        )
        return

    updated = await turn_scheduler.update_queued_message(
        conversation_id, queue_id, content=content.strip()
    )
    if updated is None:
        await manager.send_error(
            connection,
            code="not_found",
            message="Queued message not found",
            recoverable=True,
        )


async def _handle_resolve_escalation(
    app: Any,
    manager: WebSocketConnectionManager,
    connection: AuthenticatedWebSocket,
    message: dict[str, Any],
) -> None:
    """Handle a 'resolve_escalation' type WebSocket frame.

    Delegates to NotificationService.resolve() — the same path used by
    the REST ``POST /api/v1/escalations/{call_id}/resolve`` endpoint.
    """
    call_id = message.get("call_id")
    decision = message.get("decision")
    note = message.get("note")
    if not isinstance(call_id, str) or not isinstance(decision, str):
        await manager.send_error(
            connection,
            code="validation_error",
            message="call_id and decision are required",
            recoverable=True,
        )
        return
    if decision not in ("approve", "deny"):
        await manager.send_error(
            connection,
            code="validation_error",
            message="decision must be 'approve' or 'deny'",
            recoverable=True,
        )
        return

    current_user_email.set(connection.user_email)

    # Look up tool name for the system message before resolving
    pending_pause = app.state.pause_waiter.find_pending(pause_type="escalation")
    if (
        pending_pause
        and (pending_pause.context or {}).get("call_id") != call_id
        and pending_pause.pause_id != call_id
    ):
        pending_pause = None
    tool_name = (
        (pending_pause.context or {}).get("tool_name", "tool call")
        if pending_pause
        else "tool call"
    )

    svc = app.state.notification_service
    resolved = await svc.resolve(
        call_id,
        decision,
        {"note": note if isinstance(note, str) else ""},
        user_email=connection.user_email,
    )
    if not resolved:
        notification = await svc.get(call_id)
        resolution = notification.resolution if notification is not None else None
        if (
            notification is not None
            and notification.status == "resolved"
            and isinstance(resolution, dict)
            and str(resolution.get("decision") or "").lower() == decision.lower()
        ):
            resolved = True
        elif isinstance(resolution, dict) and resolution.get("reason") == "timeout":
            await manager.send_error(
                connection,
                code="expired",
                message="Escalation expired before it could be resolved",
                recoverable=True,
            )
            return
        else:
            await manager.send_error(
                connection,
                code="not_found",
                message="Escalation not found or already resolved",
                recoverable=True,
            )
            return

    # System message to conversation
    verb = "approved" if decision == "approve" else "denied"
    note_suffix = f": {note}" if isinstance(note, str) and note else ""
    conv_id = pending_pause.conversation_id if pending_pause else None
    if conv_id:
        await manager.send_to_conversation(
            conv_id,
            {
                "type": "system_message",
                "conversation_id": conv_id,
                "text": f"User {verb} {tool_name}{note_suffix}",
            },
        )


async def _handle_gate_response(
    app: Any,
    manager: WebSocketConnectionManager,
    connection: AuthenticatedWebSocket,
    message: dict[str, Any],
) -> None:
    """Handle a 'gate_response' type WebSocket frame.

    Delegates to NotificationService — same path as
    ``POST /api/v1/tasks/{task_id}/gate-response``.
    """
    task_id = message.get("task_id")
    action = message.get("action")
    feedback = message.get("feedback")
    if not isinstance(task_id, str) or not isinstance(action, str):
        await manager.send_error(
            connection,
            code="validation_error",
            message="task_id and action are required",
            recoverable=True,
        )
        return
    if await _load_task_for_user(app, connection, task_id) is None:
        await manager.send_error(
            connection,
            code="not_found",
            message="Task not found",
            recoverable=True,
        )
        return

    # Persist feedback to workflow state (read by evaluation loop)
    if isinstance(feedback, str) and feedback:
        await _persist_task_feedback(app, task_id, feedback)

    current_user_email.set(connection.user_email)
    svc = app.state.notification_service
    notif = await svc.find_by_task(task_id, notification_type="gate", status="pending")
    if notif is None:
        await manager.send_error(
            connection,
            code="not_found",
            message="No pending gate",
            recoverable=True,
        )
        return
    resolved = await svc.resolve(
        notif.notification_id,
        action,
        {"feedback": feedback if isinstance(feedback, str) else ""},
    )
    if not resolved:
        await manager.send_error(
            connection,
            code="conflict",
            message="Gate already resolved",
            recoverable=True,
        )


async def _handle_step_response(
    app: Any,
    manager: WebSocketConnectionManager,
    connection: AuthenticatedWebSocket,
    message: dict[str, Any],
) -> None:
    """Handle a 'step_response' type WebSocket frame.

    Delegates to NotificationService — same path as
    ``POST /api/v1/tasks/{task_id}/step-response``.
    """
    task_id = message.get("task_id")
    notification_id = message.get("notification_id")
    response = message.get("response", "")
    raw_reply = {"answers": message.get("answers"), "mode": message.get("mode", "structured")}
    reply: dict[str, Any] | None = None
    if notification_id is not None and not isinstance(notification_id, str):
        await manager.send_error(
            connection,
            code="validation_error",
            message="notification_id must be a string",
            recoverable=True,
        )
        return
    if task_id is not None and not isinstance(task_id, str):
        await manager.send_error(
            connection,
            code="validation_error",
            message="task_id must be a string",
            recoverable=True,
        )
        return
    if not isinstance(task_id, str) and not isinstance(notification_id, str):
        await manager.send_error(
            connection,
            code="validation_error",
            message="task_id or notification_id is required",
            recoverable=True,
        )
        return

    current_user_email.set(connection.user_email)
    svc = app.state.notification_service

    notification = None
    if isinstance(notification_id, str):
        notification = await svc.get(notification_id)
        if (
            notification is None
            or notification.notification_type not in {"step_question", "auth_challenge"}
            or notification.user_email != connection.user_email
        ):
            await manager.send_error(
                connection,
                code="not_found",
                message="Pending input request not found",
                recoverable=True,
            )
            return
        if task_id is not None and notification.task_id != task_id:
            await manager.send_error(
                connection,
                code="conflict",
                message="task_id does not match the referenced input request",
                recoverable=True,
            )
            return
        task_id = notification.task_id

    if isinstance(task_id, str) and await _load_task_for_user(app, connection, task_id) is None:
        await manager.send_error(
            connection,
            code="not_found",
            message="Task not found",
            recoverable=True,
        )
        return

    if notification is not None and notification.task_id is None:
        pause = app.state.pause_waiter.get(notification.notification_id)
        expected_pause_type = notification.notification_type
        if (
            pause is None
            or pause.pause_type != expected_pause_type
            or pause.task_id is not None
            or pause.conversation_id != notification.conversation_id
            or pause.session_id != notification.session_id
        ):
            await manager.send_error(
                connection,
                code="conflict",
                message="Input request can no longer be resumed",
                recoverable=True,
            )
            return
        if notification.notification_type == "auth_challenge":
            try:
                data = await build_auth_challenge_resolution_data(
                    notification=notification,
                    decision="continue",
                    user_email=notification.user_email,
                    credentials_provider=app.state.providers.credentials,
                    response=str(response),
                )
            except ValueError as exc:
                await manager.send_error(
                    connection,
                    code="validation_error",
                    message=str(exc),
                    recoverable=True,
                )
                return
            resolved = await svc.resolve(
                notification.notification_id,
                "continue",
                data,
                user_email=notification.user_email,
            )
            if not resolved:
                await manager.send_error(
                    connection,
                    code="conflict",
                    message="Input request already resolved",
                    recoverable=True,
                )
            return
        try:
            reply = validate_reply_for_questions(raw_reply, pause.questions or [])
        except ValueError as exc:
            await manager.send_error(
                connection,
                code="validation_error",
                message=str(exc),
                recoverable=True,
            )
            return
        resolved = await svc.resolve(
            notification.notification_id,
            "continue",
            reply,
            user_email=notification.user_email,
        )
        if not resolved:
            await manager.send_error(
                connection,
                code="conflict",
                message="Input request already resolved",
                recoverable=True,
            )
        return

    resolved = False
    notif = notification
    if notif is None and isinstance(task_id, str):
        notif = await svc.find_by_task(task_id, notification_type="step_question", status="pending")
    if notif is not None:
        try:
            questions = (
                (
                    app.state.pause_waiter.get(notif.notification_id).questions
                    if app.state.pause_waiter.get(notif.notification_id) is not None
                    else None
                )
                or (notif.payload or {}).get("questions")
                or []
            )
            reply = validate_reply_for_questions(raw_reply, questions)
        except ValueError as exc:
            await manager.send_error(
                connection,
                code="validation_error",
                message=str(exc),
                recoverable=True,
            )
            return
        resolved = await svc.resolve(
            notif.notification_id,
            "continue",
            reply,
        )
        if not resolved:
            await manager.send_error(
                connection,
                code="conflict",
                message="Step question already resolved",
                recoverable=True,
            )
            return

    # Fallback for recovered tasks (PauseWaiter registered but no notification row)
    if not resolved:
        from cognis.core.agent_loop import PauseResolution

        pause = app.state.pause_waiter.find_pending(
            task_id=task_id,
            pause_type="step_input",
        )
        if pause is not None:
            try:
                reply = validate_reply_for_questions(raw_reply, pause.questions or [])
            except ValueError as exc:
                await manager.send_error(
                    connection,
                    code="validation_error",
                    message=str(exc),
                    recoverable=True,
                )
                return
            app.state.pause_waiter.resolve(
                pause.pause_id,
                PauseResolution(decision="continue", data=reply),
            )
            resolved = True

    if not resolved:
        await manager.send_error(
            connection,
            code="not_found",
            message="No pending step question",
            recoverable=True,
        )
        return

    # Handle task resume for recovered tasks (task not actively running)
    if not isinstance(task_id, str):
        return
    if not app.state.task_queue.has_active_run(task_id):
        if reply is None:
            await manager.send_error(
                connection,
                code="validation_error",
                message="Structured question-set reply is required",
                recoverable=True,
            )
            return
        await _store_recovered_step_input_response(app, task_id, reply)
        pause = app.state.pause_waiter.find_pending(task_id=task_id, pause_type="step_input")
        if pause is not None:
            app.state.pause_waiter.clear(pause.pause_id)
        try:
            await app.state.task_queue.resume_task(task_id)
        except ValueError as exc:
            await manager.send_error(
                connection,
                code="conflict",
                message=str(exc),
                recoverable=True,
            )


# ---------------------------------------------------------------------------
# Command result rendering
# ---------------------------------------------------------------------------


async def _render_command_result(
    manager: WebSocketConnectionManager,
    conversation_id: str,
    result: Any,
    *,
    app: Any | None = None,
    session: Any | None = None,
    agent: AgentDefinition | None = None,
    user_email: str | None = None,
) -> None:
    """Render a CommandResult into WebSocket payloads."""
    if result.type == "system_message":
        if app is not None and session is not None and agent is not None and user_email:
            await persist_command_system_notice(
                conversation_id=conversation_id,
                result=result,
                providers=app.state.providers,
                session_cache=getattr(app.state, "session_cache", None),
                session=session,
                agent=agent,
                user_email=user_email,
            )
        await manager.send_to_conversation(
            conversation_id,
            {
                "type": "system_message",
                "conversation_id": conversation_id,
                "text": result.text,
                "command_result": True,
                **result.data,
            },
        )
    elif result.type == "error":
        await manager.send_to_conversation(
            conversation_id,
            WebSocketError(
                code=result.data.get("code", "command_error"),
                message=result.text or "Command failed",
                recoverable=True,
            ).model_dump(),
        )
    elif result.type == "session_compacted":
        await manager.send_to_conversation(
            conversation_id,
            {
                "type": "session_compacted",
                "conversation_id": conversation_id,
                "message": result.text,
                "command_result": True,
                **result.data,
            },
        )
    elif result.type == "conversation_created":
        await manager.send_to_conversation(
            conversation_id,
            {
                "type": "conversation_created",
                **result.data,
            },
        )
        # Notify all other owner tabs about the new conversation so their
        # sidebar lists stay current without a manual refresh. The creating
        # tab already handles this via the conversation_created event above;
        # send_sidebar_update_to_owner excludes subscribed connections so we
        # use send_to_user here to reach every tab including the creator's
        # other windows.
        new_conversation_id = result.data.get("conversation_id")
        if isinstance(new_conversation_id, str) and new_conversation_id:
            await manager.send_sidebar_update_to_owner(
                new_conversation_id,
                {
                    "type": "sidebar_conversation_upsert",
                    "conversation_id": new_conversation_id,
                },
            )
    elif result.type == "session_reset":
        await manager.send_to_conversation(
            conversation_id,
            {
                "type": "session_reset",
                **result.data,
            },
        )
    elif result.type == "history_rebased":
        await manager.send_to_conversation(
            conversation_id,
            {
                "type": "history_rebased",
                "conversation_id": conversation_id,
                "message": result.text,
                **result.data,
            },
        )
    elif result.type == "queued":
        await manager.send_to_conversation(
            conversation_id,
            {
                "type": "queued",
                "conversation_id": conversation_id,
                "queued_count": result.data.get("queued_count", 0),
                "reason": result.text,
                "command_result": True,
                **result.data,
            },
        )


# ---------------------------------------------------------------------------
# Event-to-payload mapping
# ---------------------------------------------------------------------------


def _event_to_payload(event: Event, conversation_id: str) -> dict[str, Any] | None:
    """Map an EventBus event to a WebSocket payload."""
    if event.type == EventType.WORKFLOW_GATE:
        return {
            "type": "workflow_gate",
            "conversation_id": conversation_id,
            "task_id": event.data.get("task_id"),
            "step_name": event.data.get("step"),
            "message": event.data.get("message"),
            "options": event.data.get("options"),
            "context": event.data.get("context"),
        }
    if event.type == EventType.STEP_PAUSED and event.data.get("pause_type") == "step_input":
        return {
            "type": "workflow_step_question",
            "conversation_id": conversation_id,
            "task_id": event.data.get("task_id"),
            "step_name": event.data.get("step_name"),
            "questions": event.data.get("questions"),
            "context": event.data.get("context"),
        }
    if event.type == EventType.STEP_STARTED:
        return {
            "type": "workflow_step_started",
            "conversation_id": conversation_id,
            "task_id": event.data.get("task_id"),
            "step_name": event.data.get("step_name"),
            "step_run_id": event.data.get("step_run_id"),
        }
    if event.type == EventType.STEP_COMPLETED:
        return {
            "type": "workflow_step_completed",
            "conversation_id": conversation_id,
            "task_id": event.data.get("task_id"),
            "step_name": event.data.get("step_name"),
            "attempt": event.data.get("attempt", 1),
        }
    if event.type == EventType.WORKFLOW_COMPOSED:
        return _workflow_composed_payload(conversation_id, event.data)
    if event.type == EventType.SYSTEM_NOTICE:
        if is_transient_compaction_start_notice(event.data):
            return None
        payload = {
            "type": "system_message",
            "conversation_id": conversation_id,
            "session_id": event.data.get("session_id"),
            "text": event.data.get("message"),
            "turn_id": event.data.get("turn_id"),
            "notice_id": event.data.get("notice_id"),
            "kind": event.data.get("kind"),
            "scope": event.data.get("scope"),
        }
        for key in (
            "reason_class",
            "provider_id",
            "model",
            "retry_after_seconds",
            "provider_retry_after_seconds",
            "retry_at",
            "attempt",
            "max_attempts",
            "attempts",
            "attempts_per_cycle",
            "continuation_attempts",
            "recoverable",
            "retry_reason",
            "retry_source_turn_id",
        ):
            if key in event.data:
                payload[key] = event.data.get(key)
        return payload
    if event.type == EventType.CONVERSATION_UPDATED:
        payload: dict[str, Any] = {
            "type": "conversation_updated",
            "conversation_id": conversation_id,
        }
        if event.data.get("title") is not None:
            payload["title"] = event.data.get("title")
        if isinstance(event.data.get("has_active_turn"), bool):
            payload["has_active_turn"] = event.data.get("has_active_turn")
        if "active_turn_chat_mode" in event.data:
            payload["active_turn_chat_mode"] = event.data.get("active_turn_chat_mode")
        if "active_turn_chat_mode_source" in event.data:
            payload["active_turn_chat_mode_source"] = event.data.get("active_turn_chat_mode_source")
        if "active_session_status" in event.data:
            payload["active_session_status"] = event.data.get("active_session_status")
        if "active_session_completion_reason" in event.data:
            payload["active_session_completion_reason"] = event.data.get(
                "active_session_completion_reason"
            )
        if isinstance(event.data.get("pending_notification_types"), list):
            payload["pending_notification_types"] = event.data.get("pending_notification_types")
        if isinstance(event.data.get("has_unread"), bool):
            payload["has_unread"] = event.data.get("has_unread")
        if event.data.get("last_read_at") is not None:
            payload["last_read_at"] = event.data.get("last_read_at")
        if event.data.get("last_message_at") is not None:
            payload["last_message_at"] = event.data.get("last_message_at")
        if event.data.get("updated_at") is not None:
            payload["updated_at"] = event.data.get("updated_at")
        if isinstance(event.data.get("created_conversation_id"), str):
            payload["created_conversation_id"] = event.data.get("created_conversation_id")
        return payload
    if event.type == EventType.WORKFLOW_PROGRESS and event.data.get("event") in {
        "tool_call_started",
        "tool_call_completed",
    }:
        if event.data.get("event") == "tool_call_completed":
            return {
                "type": "tool_result",
                "conversation_id": conversation_id,
                "session_id": event.data.get("session_id"),
                "call_id": event.data.get("call_id"),
                "tool_name": event.data.get("tool_name"),
                "result": event.data.get("result", ""),
                "is_error": bool(event.data.get("is_error", False)),
                "duration_ms": event.data.get("duration_ms"),
                "evaluation": event.data.get("evaluation"),
                "file_diffs": event.data.get("file_diffs") or [],
                "attachments": event.data.get("attachments") or [],
                "timestamp": event.timestamp.isoformat() if event.timestamp else None,
                "turn_id": event.data.get("turn_id"),
            }
        return {
            "type": "tool_call",
            "conversation_id": conversation_id,
            "session_id": event.data.get("session_id"),
            "call_id": event.data.get("call_id"),
            "tool_name": event.data.get("tool_name"),
            "status": "started",
            "arguments": event.data.get("arguments"),
            "timestamp": event.timestamp.isoformat() if event.timestamp else None,
            "turn_id": event.data.get("turn_id"),
        }
    if event.type == EventType.TURN_STARTED:
        return {
            "type": "turn_started",
            "conversation_id": conversation_id,
            "session_id": event.data.get("session_id"),
            "message_id": event.data.get("message_id"),
            "turn_id": event.data.get("turn_id"),
            "chat_mode": event.data.get("chat_mode"),
            "chat_mode_source": event.data.get("chat_mode_source"),
        }
    if event.type == EventType.TURN_COMPLETED:
        return {
            "type": "turn_settled",
            "conversation_id": conversation_id,
            "session_id": event.data.get("session_id"),
            "message_id": event.data.get("message_id"),
            "queued_count": event.data.get("queued_count", 0),
            "chat_mode": event.data.get("chat_mode"),
            "chat_mode_source": event.data.get("chat_mode_source"),
            "completed_at": event.data.get("completed_at")
            or (event.timestamp.isoformat() if event.timestamp else None),
        }
    if event.type == EventType.TASK_STARTED:
        return {
            "type": "delegation_progress",
            "conversation_id": conversation_id,
            "child_session_id": event.data.get("task_id"),
            "step": "workflow",
            "progress": "running",
        }
    if event.type == EventType.TASK_PAUSED:
        return {
            "type": "task_paused",
            "conversation_id": conversation_id,
            "task_id": event.data.get("task_id"),
            "task_title": event.data.get("task_title"),
        }
    if event.type == EventType.TASK_COMPLETED:
        return {
            "type": "workflow_completed",
            "conversation_id": conversation_id,
            "task_id": event.data.get("task_id"),
            "result": event.data.get("result_summary"),
        }
    if event.type == EventType.TASK_FAILED:
        return {
            "type": "workflow_failed",
            "conversation_id": conversation_id,
            "task_id": event.data.get("task_id"),
            "reason": event.data.get("result_summary"),
        }
    if event.type == EventType.TASK_CANCELLED:
        return {
            "type": "workflow_cancelled",
            "conversation_id": conversation_id,
            "task_id": event.data.get("task_id"),
            "reason": event.data.get("result_summary") or "cancelled",
        }
    if event.type == EventType.DELEGATION_STARTED:
        return {
            "type": "delegation_started",
            "conversation_id": conversation_id,
            "parent_session_id": event.data.get("parent_session_id"),
            "child_session_id": event.data.get("child_session_id"),
            "mode": event.data.get("mode"),
            "agent_id": event.data.get("agent_id"),
            "used_agent_id": event.data.get("used_agent_id"),
            "task": event.data.get("title")
            or event.data.get("task_title")
            or event.data.get("task"),
            "title": event.data.get("title") or event.data.get("task_title"),
            "task_title": event.data.get("task_title") or event.data.get("title"),
            "input_redacted": event.data.get("input_redacted"),
        }
    if event.type == EventType.DELEGATION_PROGRESS:
        return {
            "type": "delegation_progress",
            "conversation_id": conversation_id,
            "child_session_id": event.data.get("child_session_id"),
            "tool_call_count": event.data.get("tool_call_count"),
            "max_tool_calls": event.data.get("max_tool_calls"),
            "last_tool": event.data.get("last_tool"),
            "title": event.data.get("title") or event.data.get("task_title"),
            "task_title": event.data.get("task_title") or event.data.get("title"),
            "todos": event.data.get("todos"),
        }
    if event.type == EventType.DELEGATION_COMPLETED:
        return {
            "type": "delegation_completed",
            "conversation_id": conversation_id,
            "child_session_id": event.data.get("child_session_id"),
            "agent_id": event.data.get("agent_id"),
            "used_agent_id": event.data.get("used_agent_id"),
            "task": event.data.get("title")
            or event.data.get("task_title")
            or event.data.get("task"),
            "title": event.data.get("title") or event.data.get("task_title"),
            "task_title": event.data.get("task_title") or event.data.get("title"),
            "duration_ms": event.data.get("duration_ms"),
            "result": event.data.get("result_summary"),
            "result_content": event.data.get("result_content"),
            "result_source": event.data.get("result_source"),
            "result_anchors": event.data.get("result_anchors"),
            "result_truncated": event.data.get("result_truncated"),
            "todos": event.data.get("todos"),
        }
    if event.type == EventType.DELEGATION_FAILED:
        return {
            "type": "delegation_failed",
            "conversation_id": conversation_id,
            "child_session_id": event.data.get("child_session_id"),
            "agent_id": event.data.get("agent_id"),
            "used_agent_id": event.data.get("used_agent_id"),
            "task": event.data.get("title")
            or event.data.get("task_title")
            or event.data.get("task"),
            "title": event.data.get("title") or event.data.get("task_title"),
            "task_title": event.data.get("task_title") or event.data.get("title"),
            "duration_ms": event.data.get("duration_ms"),
            "reason": event.data.get("reason"),
            "recoverable": event.data.get("recoverable"),
            "todos": event.data.get("todos"),
        }
    if event.type == EventType.SESSION_RECOVERED:
        return {
            "type": "session_recovered",
            "conversation_id": conversation_id,
            "session_id": event.data.get("session_id"),
            "reason": event.data.get("reason") or "controller_restart",
        }
    if event.type == EventType.ESCALATION_CREATED:
        return {
            "type": "escalation",
            "conversation_id": conversation_id,
            "session_id": event.data.get("session_id"),
            "call_id": event.data.get("call_id"),
            "tool_call_id": event.data.get("tool_call_id"),
            "tool_name": event.data.get("tool_name"),
            "arguments_display": event.data.get("arguments_display"),
            "risk": event.data.get("risk"),
            "reasoning": event.data.get("reasoning"),
            "timeout_seconds": event.data.get("timeout_seconds"),
        }
    if event.type == EventType.ESCALATION_RESOLVED:
        return {
            "type": "escalation_resolved",
            "conversation_id": conversation_id,
            "call_id": event.data.get("call_id"),
            "decision": event.data.get("decision"),
            "reason": event.data.get("reason"),
        }
    if event.type == EventType.SESSION_COMPACTION_STARTED:
        return {
            "type": "session_compaction_started",
            "conversation_id": conversation_id,
            "session_id": event.data.get("session_id"),
            "trigger": event.data.get("trigger"),
            "reason": event.data.get("reason"),
            "prompt_tokens": event.data.get("prompt_tokens"),
            "max_context_tokens": event.data.get("max_context_tokens"),
            "max_input_tokens": event.data.get("max_input_tokens"),
            "available_prompt_tokens": event.data.get("available_prompt_tokens"),
            "compaction_threshold_prompt_tokens": event.data.get(
                "compaction_threshold_prompt_tokens"
            ),
            "loop_pressure_threshold_prompt_tokens": event.data.get(
                "loop_pressure_threshold_prompt_tokens"
            ),
            "compaction_threshold": event.data.get("compaction_threshold"),
            "previous_usage_percentage": event.data.get("previous_usage_percentage"),
            "effective_usage_percentage": event.data.get("effective_usage_percentage"),
            "hard_pressure_exceeded": event.data.get("hard_pressure_exceeded"),
            "used_timeout_fallback": event.data.get("used_timeout_fallback"),
            "phase": event.data.get("phase"),
            "status": event.data.get("status"),
            "provider_id": event.data.get("provider_id"),
            "model_id": event.data.get("model_id"),
            "fallback_reason": event.data.get("fallback_reason"),
        }
    if event.type == EventType.SESSION_COMPACTION_FINISHED:
        return {
            "type": "session_compaction_finished",
            "conversation_id": conversation_id,
            "session_id": event.data.get("session_id"),
            "trigger": event.data.get("trigger"),
            "reason": event.data.get("reason"),
            "status": event.data.get("status"),
            "fallback_reason": event.data.get("fallback_reason"),
        }
    if event.type == EventType.SESSION_COMPACTED:
        return {
            "type": "session_compacted",
            "conversation_id": conversation_id,
            "session_id": event.data.get("session_id"),
            "previous_session_id": event.data.get("previous_session_id"),
            "summary_preview": event.data.get("summary_preview"),
            "method": event.data.get("method"),
            "turns_compacted": event.data.get("turns_compacted"),
            "trigger": event.data.get("trigger"),
            "reason": event.data.get("reason"),
            "tokens_before": event.data.get("tokens_before"),
            "tokens_after": event.data.get("tokens_after"),
            "prompt_tokens": event.data.get("prompt_tokens"),
            "max_context_tokens": event.data.get("max_context_tokens"),
            "max_input_tokens": event.data.get("max_input_tokens"),
            "available_prompt_tokens": event.data.get("available_prompt_tokens"),
            "compaction_threshold_prompt_tokens": event.data.get(
                "compaction_threshold_prompt_tokens"
            ),
            "loop_pressure_threshold_prompt_tokens": event.data.get(
                "loop_pressure_threshold_prompt_tokens"
            ),
            "compaction_threshold": event.data.get("compaction_threshold"),
            "previous_usage_percentage": event.data.get("previous_usage_percentage"),
            "effective_usage_percentage": event.data.get("effective_usage_percentage"),
            "hard_pressure_exceeded": event.data.get("hard_pressure_exceeded"),
            "used_timeout_fallback": event.data.get("used_timeout_fallback"),
            "phase": event.data.get("phase"),
            "status": event.data.get("status"),
            "provider_id": event.data.get("provider_id"),
            "model_id": event.data.get("model_id"),
            "fallback_reason": event.data.get("fallback_reason"),
        }
    # Unified notification events
    if event.type == EventType.NOTIFICATION_CREATED:
        ntype = event.data.get("notification_type")
        payload = event.data.get("payload", {})
        if ntype == "escalation":
            return {
                "type": "escalation",
                "conversation_id": conversation_id,
                "session_id": event.data.get("session_id"),
                "call_id": payload.get("call_id"),
                "tool_call_id": payload.get("tool_call_id"),
                "tool_name": payload.get("tool_name"),
                "risk": payload.get("risk"),
                "reasoning": payload.get("reasoning"),
                "timeout_seconds": payload.get("timeout_seconds"),
                "task_id": event.data.get("task_id"),
            }
        if ntype == "gate":
            return {
                "type": "workflow_gate",
                "conversation_id": conversation_id,
                "notification_id": event.data.get("notification_id"),
                "task_id": event.data.get("task_id"),
                "step_name": event.data.get("step_name"),
                "message": payload.get("message"),
                "options": payload.get("options"),
                "context": payload.get("context"),
            }
        if ntype == "step_question":
            return {
                "type": "workflow_step_question",
                "conversation_id": conversation_id,
                "notification_id": event.data.get("notification_id"),
                "task_id": event.data.get("task_id"),
                "step_name": event.data.get("step_name"),
                "questions": payload.get("questions"),
                "context": payload.get("context"),
            }
        if ntype == "auth_challenge":
            return {
                "type": "auth_challenge",
                "conversation_id": conversation_id,
                "notification_id": event.data.get("notification_id"),
                "task_id": event.data.get("task_id"),
                "step_name": event.data.get("step_name"),
                "label": payload.get("label", "Authentication required"),
                "message": payload.get("message", ""),
                "kind": payload.get("kind"),
                "metadata": payload.get("metadata"),
                "required_fields": payload.get("required_fields"),
                "expires_at": payload.get("expires_at"),
            }
        if ntype == "credential_request":
            return {
                "type": "credential_request",
                "conversation_id": conversation_id,
                "notification_id": event.data.get("notification_id"),
                "task_id": event.data.get("task_id"),
                "step_name": event.data.get("step_name"),
                "label": payload.get("label", "Credential required"),
                "message": payload.get("message") or payload.get("description", ""),
                "credential_id": payload.get("credential_id"),
                "kind": payload.get("kind"),
                "metadata": payload.get("metadata"),
                "required_fields": payload.get("required_fields"),
            }
    if event.type == EventType.NOTIFICATION_RESOLVED:
        ntype = event.data.get("notification_type")
        if ntype == "escalation":
            return {
                "type": "escalation_resolved",
                "conversation_id": conversation_id,
                "call_id": event.data.get("notification_id"),
                "decision": event.data.get("decision"),
            }
        if ntype == "gate":
            return {
                "type": "workflow_gate_resolved",
                "conversation_id": conversation_id,
                "notification_id": event.data.get("notification_id"),
                "decision": event.data.get("decision"),
            }
        if ntype == "step_question":
            return {
                "type": "workflow_step_question_resolved",
                "conversation_id": conversation_id,
                "notification_id": event.data.get("notification_id"),
                "decision": event.data.get("decision"),
            }
        if ntype == "auth_challenge":
            return {
                "type": "auth_challenge_resolved",
                "conversation_id": conversation_id,
                "notification_id": event.data.get("notification_id"),
                "decision": event.data.get("decision"),
            }
        if ntype == "credential_request":
            return {
                "type": "credential_request_resolved",
                "conversation_id": conversation_id,
                "notification_id": event.data.get("notification_id"),
                "decision": event.data.get("decision"),
            }
    if event.type == EventType.USER_MESSAGE:
        return {
            "type": "user_message",
            "conversation_id": conversation_id,
            "session_id": event.data.get("session_id"),
            "message_id": event.data.get("message_id") or event.data.get("event_id"),
            "event_id": event.data.get("event_id") or event.data.get("message_id"),
            "timestamp": event.timestamp.isoformat() if event.timestamp else None,
            "content": event.data.get("content", ""),
            "attachments": event.data.get("attachments", []),
            "turn_id": event.data.get("turn_id"),
            "queue_id": event.data.get("queue_id"),
            "client_message_id": event.data.get("client_message_id"),
            "chat_mode": event.data.get("chat_mode"),
            "chat_mode_source": event.data.get("chat_mode_source"),
        }
    return None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _can_access_owner(connection: AuthenticatedWebSocket, owner_email: str) -> bool:
    return connection.role == "admin" or connection.user_email == owner_email


async def _authorize_conversation_frame(
    app: Any,
    manager: WebSocketConnectionManager,
    connection: AuthenticatedWebSocket,
    conversation_id: str,
    *,
    require_mutation: bool = False,
    allow_managed_conversation_mutation: bool = False,
) -> bool:
    async with app.state.session_factory() as db_session:
        conversation_row = await get_conversation(db_session, conversation_id)
    if conversation_row is None:
        await manager.send_error(
            connection,
            code="not_found",
            message="Conversation not found",
            recoverable=False,
        )
        return False
    if not _can_access_owner(connection, conversation_row.user_email):
        await manager.send_error(
            connection,
            code="forbidden",
            message="Conversation access denied",
            recoverable=False,
        )
        return False
    if require_mutation and connection.role == "viewer":
        await manager.send_error(
            connection,
            code="forbidden",
            message="Viewer accounts are read-only",
            recoverable=False,
        )
        return False
    if (
        require_mutation
        and not allow_managed_conversation_mutation
        and conversation_row.context_type in _MANAGED_CONVERSATION_CONTEXT_TYPES
    ):
        await manager.send_error(
            connection,
            code="managed_conversation_read_only",
            message=(
                "Managed conversations are read-only from the target chat; "
                "use managed actions from the controller conversation."
            ),
            recoverable=True,
        )
        return False
    return True


async def _authorize_chat_v2_scope(
    app: Any,
    manager: WebSocketConnectionManager,
    connection: AuthenticatedWebSocket,
    scope: TimelineScope,
) -> bool:
    """Verify scope ownership and all parent/child relationships server-side."""

    from cognis.store.queries import get_session_row, get_step_run

    async with app.state.session_factory() as db_session:
        if scope.kind == "conversation":
            if scope.conversation_id is None:
                return False
            conversation_row = await get_conversation(db_session, scope.conversation_id)
            allowed = (
                conversation_row is not None
                and getattr(conversation_row, "status", None) != "deleted"
                and _can_access_owner(connection, conversation_row.user_email)
            )
        elif scope.kind == "session":
            if scope.session_id is None:
                return False
            session_row = await get_session_row(db_session, scope.session_id)
            conversation = (
                await get_conversation(db_session, session_row.conversation_id)
                if session_row is not None
                else None
            )
            allowed = (
                session_row is not None
                and conversation is not None
                and session_row.conversation_id == scope.conversation_id
                and _can_access_owner(connection, session_row.user_email)
                and _can_access_owner(connection, conversation.user_email)
            )
        else:
            if scope.step_run_id is None:
                return False
            step = await get_step_run(db_session, scope.step_run_id)
            task = await get_task(db_session, step.task_id) if step is not None else None
            session = (
                await get_session_row(db_session, step.session_id)
                if step is not None and step.session_id
                else None
            )
            conversation = (
                await get_conversation(db_session, step.conversation_id)
                if step is not None and step.conversation_id
                else None
            )
            if conversation is None and session is not None:
                conversation = await get_conversation(db_session, session.conversation_id)
            effective_conversation_id = (
                step.conversation_id
                if step is not None and step.conversation_id
                else session.conversation_id
                if session is not None
                else None
            )
            allowed = (
                step is not None
                and task is not None
                and (not (step.conversation_id or step.session_id) or conversation is not None)
                and _can_access_owner(connection, task.created_by)
                and (
                    session is None
                    or (
                        _can_access_owner(connection, session.user_email)
                        and session.conversation_id == effective_conversation_id
                    )
                )
                and (conversation is None or _can_access_owner(connection, conversation.user_email))
                and scope.task_id == step.task_id
                and scope.session_id == step.session_id
                and scope.conversation_id == effective_conversation_id
            )
    if allowed:
        return True
    await manager.send_error(
        connection,
        code="forbidden",
        message="Chat v2 timeline scope access denied",
        recoverable=False,
    )
    return False


async def _rehydrate_chat_v2_scope(app: Any, scope: TimelineScope) -> TimelineScope | None:
    """Rebuild scope metadata from authoritative rows, never from the client."""

    from cognis.store.queries import get_session_row, get_step_run

    async with app.state.session_factory() as db_session:
        if scope.kind == "conversation":
            if not scope.conversation_id:
                return None
            row = await get_conversation(db_session, scope.conversation_id)
            if row is None:
                return None
            return TimelineScope(
                key=f"conversation:{row.conversation_id}",
                kind="conversation",
                conversation_id=row.conversation_id,
                session_id=getattr(row, "active_session_id", None),
                label=getattr(row, "title", None),
                status=getattr(row, "status", None),
            )

        if scope.kind == "session":
            if not scope.session_id:
                return None
            row = await get_session_row(db_session, scope.session_id)
            if row is None:
                return None
            conversation = await get_conversation(db_session, row.conversation_id)
            if conversation is None:
                return None
            return TimelineScope(
                key=f"session:{row.session_id}",
                kind="session",
                conversation_id=row.conversation_id,
                session_id=row.session_id,
                parent_session_id=getattr(row, "parent_session_id", None),
                label=getattr(row, "delegation_task", None) or getattr(row, "agent_id", None),
                status=getattr(row, "status", None),
            )

        if not scope.step_run_id:
            return None
        step = await get_step_run(db_session, scope.step_run_id)
        if step is None:
            return None
        session = await get_session_row(db_session, step.session_id) if step.session_id else None
        conversation_id = step.conversation_id or (
            getattr(session, "conversation_id", None) if session is not None else None
        )
        conversation = (
            await get_conversation(db_session, conversation_id) if conversation_id else None
        )
        if conversation_id and conversation is None:
            return None
        if session is not None and conversation_id != session.conversation_id:
            return None
        return TimelineScope(
            key=f"task_step:{step.step_run_id}",
            kind="task_step",
            conversation_id=conversation_id,
            session_id=step.session_id,
            task_id=step.task_id,
            step_run_id=step.step_run_id,
            parent_session_id=getattr(session, "parent_session_id", None),
            label=f"{step.step_name} (attempt {step.attempt_number})",
            status=step.status,
            missing_stream=session is None,
        )


async def _load_task_for_user(
    app: Any,
    connection: AuthenticatedWebSocket,
    task_id: str,
) -> Any | None:
    async with app.state.session_factory() as session:
        row = await get_task(session, task_id)
    if row is None:
        return None
    if not _can_access_owner(connection, row.created_by):
        return None
    return row


async def _persist_task_feedback(app: Any, task_id: str, feedback: str) -> None:
    async with app.state.session_factory() as session:
        row = await get_task(session, task_id)
        if row is None or not row.workflow_state:
            return
        state = (
            row.workflow_state if isinstance(row.workflow_state, dict) else dict(row.workflow_state)
        )
        state["last_evaluation_feedback"] = feedback
        row.workflow_state = state
        await session.commit()


async def _store_recovered_step_input_response(
    app: Any, task_id: str, reply: dict[str, Any]
) -> None:
    async with app.state.session_factory() as session:
        row = await get_task(session, task_id)
        if row is None or not row.workflow_state:
            return
        state = dict(row.workflow_state)
        if state.get("pending_pause_type") != "step_input":
            return
        payload = dict(state.get("pending_pause_payload") or {})
        payload["answers"] = reply.get("answers", [])
        payload["mode"] = reply.get("mode", "structured")
        state["pending_pause_payload"] = payload
        row.workflow_state = state
        await session.commit()


async def _load_pending_task_prompts(app: Any, conversation_id: str) -> list[dict[str, Any]]:
    """Load pending notifications for a conversation on reconnect."""
    payloads: list[dict[str, Any]] = []

    svc = getattr(app.state, "notification_service", None)
    if svc is not None:
        from cognis.store.queries import get_conversation

        user_email: str | None = None
        async with app.state.session_factory() as session:
            conv_row = await get_conversation(session, conversation_id)
            if conv_row is not None:
                user_email = conv_row.user_email

        if user_email:
            notifications = await svc.list_pending(user_email, conversation_id=conversation_id)
            for notif in notifications:
                payload = notif.payload or {}
                if notif.notification_type == "gate":
                    payloads.append(
                        {
                            "type": "workflow_gate",
                            "notification_id": notif.notification_id,
                            "task_id": notif.task_id,
                            "step_name": notif.step_name,
                            "message": payload.get("message") or payload.get("question", ""),
                            "options": payload.get("options"),
                            "context": payload.get("context"),
                        }
                    )
                elif notif.notification_type == "step_question":
                    payloads.append(
                        {
                            "type": "workflow_step_question",
                            "notification_id": notif.notification_id,
                            "task_id": notif.task_id,
                            "step_name": notif.step_name,
                            "questions": payload.get("questions"),
                            "context": payload.get("context"),
                        }
                    )
                elif notif.notification_type == "credential_request":
                    payloads.append(
                        {
                            "type": "credential_request",
                            "notification_id": notif.notification_id,
                            "task_id": notif.task_id,
                            "step_name": notif.step_name,
                            "label": payload.get("label", "Credential required"),
                            "message": payload.get("message") or payload.get("description", ""),
                            "credential_id": payload.get("credential_id"),
                            "kind": payload.get("kind"),
                            "metadata": payload.get("metadata"),
                            "required_fields": payload.get("required_fields"),
                        }
                    )
                elif notif.notification_type == "auth_challenge":
                    payloads.append(
                        {
                            "type": "auth_challenge",
                            "notification_id": notif.notification_id,
                            "task_id": notif.task_id,
                            "step_name": notif.step_name,
                            "label": payload.get("label", "Authentication required"),
                            "message": payload.get("message", ""),
                            "kind": payload.get("kind"),
                            "metadata": payload.get("metadata"),
                            "required_fields": payload.get("required_fields"),
                            "expires_at": payload.get("expires_at"),
                        }
                    )
                elif notif.notification_type == "escalation":
                    payloads.append(
                        {
                            "type": "escalation",
                            "notification_id": notif.notification_id,
                            "call_id": payload.get("call_id"),
                            "tool_name": payload.get("tool_name"),
                            "risk": payload.get("risk"),
                            "reasoning": payload.get("reasoning"),
                            "timeout_seconds": payload.get("timeout_seconds"),
                            "task_id": notif.task_id,
                        }
                    )
            if payloads:
                return payloads

    # Legacy fallback
    async with app.state.session_factory() as session:
        rows = list(
            (
                await session.execute(
                    select(Task).where(
                        Task.source_type == "chat",
                        Task.source_ref == conversation_id,
                        Task.status == "paused",
                    )
                )
            )
            .scalars()
            .all()
        )

    for row in rows:
        pause = app.state.pause_waiter.find_pending(task_id=row.task_id)
        if pause is None:
            continue
        if pause.pause_type == "gate":
            payloads.append(
                {
                    "type": "workflow_gate",
                    "task_id": row.task_id,
                    "step_name": pause.step_name,
                    "message": pause.question,
                    "options": pause.options,
                    "context": pause.context,
                }
            )
        elif pause.pause_type == "step_input":
            payloads.append(
                {
                    "type": "workflow_step_question",
                    "task_id": row.task_id,
                    "step_name": pause.step_name,
                    "questions": pause.questions,
                    "context": pause.context,
                }
            )
    return payloads
