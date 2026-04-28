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
import json
import uuid
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import WebSocket, WebSocketDisconnect
from prometheus_client import Counter, Gauge
from sqlalchemy import select

from cognis.api.models import (
    WebSocketAuthenticated,
    WebSocketChunkGap,
    WebSocketError,
    WebSocketPong,
)
from cognis.core.attachment_utils import hydrate_attachment_refs, strip_attachment_payload_bytes
from cognis.core.events import Event, EventType
from cognis.core.notification_resolution import build_auth_challenge_resolution_data
from cognis.core.turn_scheduler import (
    SessionCreationFailedError as SessionCreationFailedError,  # noqa: F401 — re-export
)
from cognis.core.turn_scheduler import (
    TurnError,
    TurnResult,
)
from cognis.logging import get_logger
from cognis.models.agent import AgentDefinition
from cognis.runtime_context import current_user_email
from cognis.store.models import Task
from cognis.store.queries import (
    get_browser_session_by_token,
    get_task,
    get_user,
    mark_artifacts_attached,
)

logger = get_logger(__name__)

_NEW_SESSION_STREAM_GRACE = timedelta(seconds=30)

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

DEFAULT_INBOUND_RATE_LIMIT = 10
DEFAULT_OUTBOUND_BUFFER = 100
DEFAULT_REPLAY_LIMIT = 200
COOKIE_NAME = "cognis_session"


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
class AuthenticatedWebSocket:
    """A WebSocket connection with authenticated user identity."""

    connection_id: str
    websocket: WebSocket
    user_email: str
    role: str
    subscriptions: set[str] = field(default_factory=set)
    recent_message_times: Any = field(default_factory=lambda: __import__("collections").deque())
    pending_sends: int = 0
    dropped_chunks: dict[str, int] = field(default_factory=dict)
    recovery_notified: set[str] = field(default_factory=set)
    _send_lock: asyncio.Lock = field(default_factory=asyncio.Lock)

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
        """Send JSON with backpressure handling."""
        msg_type = data.get("type", "")
        message_id = data.get("message_id")

        # Droppable message types when the outbound buffer is full
        _DROPPABLE_TYPES = ("chunk", "assistant_thinking_chunk")

        # Drop non-critical chunks when buffer is full
        if msg_type in _DROPPABLE_TYPES and self.pending_sends >= DEFAULT_OUTBOUND_BUFFER:
            if message_id:
                self.dropped_chunks[message_id] = self.dropped_chunks.get(message_id, 0) + 1
            return

        # Emit chunk_gap frame before non-chunk messages if chunks were dropped
        if msg_type not in _DROPPABLE_TYPES and message_id and message_id in self.dropped_chunks:
            gap_count = self.dropped_chunks.pop(message_id)
            gap_payload = WebSocketChunkGap(
                conversation_id=data.get("conversation_id", ""),
                message_id=message_id,
                dropped_count=gap_count,
            ).model_dump()
            async with self._send_lock:
                self.pending_sends += 1
                try:
                    await self.websocket.send_json(gap_payload)
                finally:
                    self.pending_sends -= 1
            WS_CHUNK_GAP_FRAMES_TOTAL.inc()

        async with self._send_lock:
            self.pending_sends += 1
            try:
                await self.websocket.send_json(data)
            finally:
                self.pending_sends -= 1


# ---------------------------------------------------------------------------
# WebSocketTurnObserver — implements TurnObserver for WS streaming
# ---------------------------------------------------------------------------


class WebSocketTurnObserver:
    """Bridges TurnScheduler streaming callbacks to WebSocket clients.

    One instance per WebSocketConnectionManager. Fans out streaming
    events to all connections subscribed to the relevant conversation.
    """

    def __init__(self, manager: WebSocketConnectionManager) -> None:
        self._manager = manager

    async def on_token(
        self,
        conversation_id: str,
        session_id: str,
        message_id: str,
        turn_id: str | None,
        delta: str,
        chunk_index: int | None = None,
        content_offset: int | None = None,
    ) -> None:
        await self._manager.send_to_conversation(
            conversation_id,
            {
                "type": "chunk",
                "conversation_id": conversation_id,
                "session_id": session_id,
                "message_id": message_id,
                "turn_id": turn_id,
                "content": delta,
                "index": chunk_index if chunk_index is not None else 0,
                "content_offset": content_offset if content_offset is not None else 0,
            },
        )

    async def on_tool_call(
        self,
        conversation_id: str,
        session_id: str,
        call_id: str,
        tool_name: str,
        arguments: dict[str, Any] | None,
        turn_id: str | None,
    ) -> None:
        payload: dict[str, Any] = {
            "type": "tool_call",
            "conversation_id": conversation_id,
            "session_id": session_id,
            "call_id": call_id,
            "tool_name": tool_name,
            "status": "started",
            "timestamp": datetime.now(UTC).isoformat(),
            "turn_id": turn_id,
        }
        if arguments is not None:
            payload["arguments"] = arguments
        await self._manager.send_to_conversation(conversation_id, payload)

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
        turn_id: str | None = None,
    ) -> None:
        payload: dict[str, Any] = {
            "type": "tool_result",
            "conversation_id": conversation_id,
            "session_id": session_id,
            "call_id": call_id,
            "tool_name": tool_name,
            "result": result,
            "is_error": is_error,
            "duration_ms": duration_ms,
            "timestamp": datetime.now(UTC).isoformat(),
            "turn_id": turn_id,
        }
        if evaluation:
            payload["evaluation"] = evaluation
        if attachments:
            payload["attachments"] = strip_attachment_payload_bytes(attachments)
        await self._manager.send_to_conversation(conversation_id, payload)

    async def on_turn_complete(self, result: TurnResult) -> None:
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
            "queued_count": 0,
            "attachments": strip_attachment_payload_bytes(result.attachments or []),
        }
        if result.delegated:
            payload["delegated"] = True
            payload["task_id"] = result.task_id
        await self._manager.send_to_conversation(result.conversation_id, payload)

        # Notify clients if the conversation title changed
        if result.title_changed and result.new_title:
            await self._manager.send_to_conversation(
                result.conversation_id,
                {
                    "type": "conversation_updated",
                    "conversation_id": result.conversation_id,
                    "title": result.new_title,
                },
            )

    async def on_turn_error(self, conversation_id: str, error: TurnError) -> None:
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
    ) -> None:
        """Emit assistant thinking chunk or block boundary frame.

        Streaming deltas → ``assistant_thinking_chunk`` (droppable under
        backpressure, same as regular ``chunk`` frames).
        Block-boundary signals (``complete=True`` with empty delta) →
        ``assistant_thinking_block`` to let the UI finalize the block.
        """
        if delta:
            # Streaming chunk — droppable under backpressure
            await self._manager.send_to_conversation(
                conversation_id,
                {
                    "type": "assistant_thinking_chunk",
                    "conversation_id": conversation_id,
                    "session_id": session_id,
                    "message_id": message_id,
                    "turn_id": turn_id,
                    "block_id": block_id,
                    "delta": delta,
                    "title": title,
                    "complete": complete,
                },
            )
        if complete:
            # Block boundary — always deliver
            await self._manager.send_to_conversation(
                conversation_id,
                {
                    "type": "assistant_thinking_block",
                    "conversation_id": conversation_id,
                    "session_id": session_id,
                    "message_id": message_id,
                    "turn_id": turn_id,
                    "block_id": block_id,
                    "title": title,
                    "complete": True,
                    "content": content,
                },
            )

    async def on_system_message(self, conversation_id: str, text: str) -> None:
        await self._manager.send_to_conversation(
            conversation_id,
            {
                "type": "system_message",
                "conversation_id": conversation_id,
                "text": text,
            },
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

        # Create the TurnObserver bridge
        self._observer = WebSocketTurnObserver(self)

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
        WS_CONNECTIONS_ACTIVE.inc()
        WS_CONNECTIONS_TOTAL.inc()
        return connection

    def disconnect(self, connection: AuthenticatedWebSocket) -> None:
        """Unregister a WebSocket connection."""
        self._connections.pop(connection.connection_id, None)
        for cid in list(connection.subscriptions):
            self._unsubscribe(connection, cid)
        WS_CONNECTIONS_ACTIVE.dec()

    def subscribe(self, connection: AuthenticatedWebSocket, conversation_id: str) -> None:
        """Subscribe a connection to a conversation's events."""
        connection.subscriptions.add(conversation_id)
        already_subscribed = bool(self._by_conversation.get(conversation_id))
        self._by_conversation[conversation_id].add(connection.connection_id)

        # Register observer on TurnScheduler only on first subscription
        # (idempotent — prevents duplicate event delivery)
        if not already_subscribed:
            turn_scheduler = getattr(self.app.state, "turn_scheduler", None)
            if turn_scheduler is not None:
                turn_scheduler.add_observer(conversation_id, self._observer)

    def _unsubscribe(self, connection: AuthenticatedWebSocket, conversation_id: str) -> None:
        """Unsubscribe a connection from a conversation."""
        connection.subscriptions.discard(conversation_id)
        conns = self._by_conversation.get(conversation_id)
        if conns:
            conns.discard(connection.connection_id)
            if not conns:
                del self._by_conversation[conversation_id]
                # Remove observer when no connections are subscribed
                turn_scheduler = getattr(self.app.state, "turn_scheduler", None)
                if turn_scheduler is not None:
                    turn_scheduler.remove_observer(conversation_id, self._observer)

    async def send_to_conversation(self, conversation_id: str, payload: dict[str, Any]) -> None:
        """Fan out a payload to all connections subscribed to a conversation."""
        connection_ids = self._by_conversation.get(conversation_id, set())
        if not connection_ids:
            return
        coroutines = []
        for cid in list(connection_ids):
            conn = self._connections.get(cid)
            if conn is not None:
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
        # FOLLOW_UP_TURN_REQUESTED is handled by TurnScheduler
        if event.type == EventType.FOLLOW_UP_TURN_REQUESTED:
            return
        # TURN_COMPLETED / TURN_ERROR are handled by the TurnObserver bridge.
        # TURN_STARTED is event-bus-only, so let it flow through _event_to_payload.
        if event.type in (
            EventType.TURN_COMPLETED,
            EventType.TURN_ERROR,
        ):
            return

        conversation_id = await self._resolve_conversation_id(event)
        if conversation_id is None:
            return
        payload = _event_to_payload(event, conversation_id)
        if payload is None:
            return
        await self.send_to_conversation(conversation_id, payload)

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

        if session_row is None:
            return

        session = _to_session_model(session_row)
        if client_session_id and client_session_id != session.session_id:
            last_seq = 0

        result = await self.app.state.providers.guardrails.read_events(
            session_id=session.intaris_session_id or session.session_id,
            after_seq=last_seq,
            limit=DEFAULT_REPLAY_LIMIT,
            allow_missing_stream=True,
        )
        replayed = 0
        async with self.app.state.session_factory() as artifact_session:
            artifact_store = self.app.state.artifact_store
            for item in result.events:
                event_type = item.get("type")
                data = item.get("data", {})
                if event_type == "assistant_message":
                    turn_id = data.get("turn_id") if isinstance(data.get("turn_id"), str) else None
                    message_id = turn_id or f"replay_{item.get('seq', uuid.uuid4().hex)}"
                    content = str(data.get("content", ""))
                    attachments = await hydrate_attachment_refs(
                        artifact_session,
                        artifact_store,
                        data.get("attachments")
                        if isinstance(data.get("attachments"), list)
                        else [],
                        owner_email=connection.user_email,
                        conversation_id=conversation_id,
                        session_id=session.session_id,
                    )
                    if content:
                        await connection.send_json(
                            {
                                "type": "chunk",
                                "conversation_id": conversation_id,
                                "session_id": session.session_id,
                                "message_id": message_id,
                                "turn_id": turn_id,
                                "content": content,
                                "index": 0,
                            }
                        )
                    await connection.send_json(
                        {
                            "type": "message_complete",
                            "conversation_id": conversation_id,
                            "session_id": session.session_id,
                            "message_id": message_id,
                            "turn_id": turn_id,
                            "content": content,
                            "seq": item.get("seq", 0),
                            "token_usage": None,
                            "queued_count": 0,
                            "attachments": attachments,
                        }
                    )
                    replayed += 1
                elif event_type == "tool_call":
                    arguments = data.get("arguments")
                    if isinstance(arguments, str):
                        with contextlib.suppress(Exception):
                            arguments = json.loads(arguments)
                    await connection.send_json(
                        {
                            "type": "tool_call",
                            "conversation_id": conversation_id,
                            "session_id": session.session_id,
                            "seq": item.get("seq"),
                            "call_id": data.get("call_id"),
                            "tool_name": data.get("name") or data.get("tool_name"),
                            "status": data.get("status", "started"),
                            "arguments": arguments,
                            "turn_id": data.get("turn_id"),
                        }
                    )
                    replayed += 1
                elif event_type == "tool_result":
                    attachments = await hydrate_attachment_refs(
                        artifact_session,
                        artifact_store,
                        data.get("attachments") if isinstance(data.get("attachments"), list) else [],
                        owner_email=connection.user_email,
                        conversation_id=conversation_id,
                        session_id=session.session_id,
                    )
                    await connection.send_json(
                        {
                            "type": "tool_result",
                            "conversation_id": conversation_id,
                            "session_id": session.session_id,
                            "seq": item.get("seq"),
                            "call_id": data.get("call_id"),
                            "tool_name": data.get("name") or data.get("tool_name"),
                            "result": data.get("result", ""),
                            "is_error": bool(data.get("is_error", False)),
                            "duration_ms": data.get("duration_ms"),
                            "evaluation": data.get("evaluation"),
                            "attachments": attachments,
                            "turn_id": data.get("turn_id"),
                        }
                    )
                    replayed += 1
                elif event_type == "assistant_thinking":
                    # Replay as a single completed block frame (no streaming chunks on replay)
                    block_id = data.get("block_id") or f"thk_replay_{item.get('seq', 0)}"
                    await connection.send_json(
                        {
                            "type": "assistant_thinking_block",
                            "conversation_id": conversation_id,
                            "session_id": session.session_id,
                            "message_id": data.get("message_id") or data.get("turn_id"),
                            "turn_id": data.get("turn_id"),
                            "block_id": block_id,
                            "title": data.get("title"),
                            "content": data.get("content", ""),
                            "complete": True,
                            "seq": item.get("seq"),
                        }
                    )
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
                                "reason": data.get("error"),
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
                                "task": data.get("task"),
                                "turn_id": data.get("turn_id"),
                            }
                        )
                    replayed += 1
                elif event_type == "lifecycle" and data.get("event") == "system_notice":
                    await connection.send_json(
                        {
                            "type": "system_message",
                            "conversation_id": conversation_id,
                            "seq": item.get("seq"),
                            "text": str(data.get("message", "")),
                            "turn_id": data.get("turn_id"),
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
        if turn_scheduler is not None:
            for snapshot in await turn_scheduler.active_stream_snapshots(conversation_id):
                await connection.send_json(
                    {
                        "type": "assistant_stream_snapshot",
                        **snapshot,
                    }
                )

        await connection.send_json(
            {
                "type": "reconnected",
                "conversation_id": conversation_id,
                "session_id": session.session_id,
                "missed_events_count": replayed,
                "last_seq": result.last_seq,
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
        return websocket.app.state.auth_provider.verify_jwt(
            first_message["token"],
            audience=["cognis"],
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

            if message_type == "message":
                await _handle_message(websocket.app, manager, connection, message)
                continue

            if message_type == "cancel":
                await _handle_cancel(websocket.app, manager, connection, message)
                continue

            if message_type == "resolve_escalation":
                await _handle_resolve_escalation(websocket.app, manager, connection, message)
                continue

            if message_type == "gate_response":
                await _handle_gate_response(websocket.app, manager, connection, message)
                continue

            if message_type == "step_response":
                await _handle_step_response(websocket.app, manager, connection, message)
                continue

            if message_type == "reconnect":
                conversation_id = message.get("conversation_id")
                last_seq = message.get("last_seq", 0)
                session_id = message.get("session_id")
                if not isinstance(conversation_id, str) or not isinstance(last_seq, int):
                    await manager.send_error(
                        connection,
                        code="validation_error",
                        message="conversation_id and last_seq are required",
                        recoverable=True,
                    )
                    continue
                await manager.replay(
                    connection,
                    conversation_id=conversation_id,
                    last_seq=last_seq,
                    client_session_id=session_id if isinstance(session_id, str) else None,
                )
                continue

            await manager.send_error(
                connection,
                code="validation_error",
                message="Unsupported WebSocket message type",
                recoverable=True,
            )
    except WebSocketDisconnect:
        manager.disconnect(connection)
    except Exception:
        logger.exception("WebSocket handler failed")
        manager.disconnect(connection)


# ---------------------------------------------------------------------------
# Message dispatch handlers
# ---------------------------------------------------------------------------


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

    # Subscribe to conversation events
    manager.subscribe(connection, conversation_id)

    # Try slash command dispatch first
    command_dispatcher = getattr(app.state, "command_dispatcher", None)
    turn_scheduler = getattr(app.state, "turn_scheduler", None)

    if command_dispatcher is not None:
        # Load minimal runtime for command dispatch
        from cognis.api.serializers import agent_to_response
        from cognis.core.session import _to_conversation_model, _to_session_model
        from cognis.store.queries import get_agent, get_conversation, get_session_row

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
            if not _can_access_owner(connection, conversation_row.user_email):
                await manager.send_error(
                    connection,
                    code="forbidden",
                    message="Conversation access denied",
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

        # Try command dispatch
        if session_model is not None:
            has_active = turn_scheduler.has_running_turn(conversation_id) if turn_scheduler else False
            has_busy = turn_scheduler.has_active_turn(conversation_id) if turn_scheduler else False
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
                await _render_command_result(manager, conversation_id, cmd_result)
                return

    # Not a command — submit to TurnScheduler
    if turn_scheduler is not None:
        error = await turn_scheduler.submit_turn(
            conversation_id,
            content,
            user_email=connection.user_email,
            attachments=[item for item in attachments if isinstance(item, dict)],
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

    cancelled = await turn_scheduler.cancel_turn(conversation_id)
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
        resolved = await svc.resolve(
            notification.notification_id,
            "continue",
            {"response": str(response)},
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
        resolved = await svc.resolve(
            notif.notification_id,
            "continue",
            {"response": str(response)},
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
            app.state.pause_waiter.resolve(
                pause.pause_id,
                PauseResolution(decision="continue", data={"response": str(response)}),
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
    if not app.state.task_queue.has_active_run(task_id):
        await _store_recovered_step_input_response(app, task_id, str(response))
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
) -> None:
    """Render a CommandResult into WebSocket payloads."""
    if result.type == "system_message":
        await manager.send_to_conversation(
            conversation_id,
            {
                "type": "system_message",
                "conversation_id": conversation_id,
                "text": result.text,
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
    elif result.type == "session_reset":
        await manager.send_to_conversation(
            conversation_id,
            {
                "type": "session_reset",
                **result.data,
            },
        )
    elif result.type == "queued":
        await manager.send_to_conversation(
            conversation_id,
            {
                "type": "queued",
                "conversation_id": conversation_id,
                "reason": result.text,
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
            "question": event.data.get("question"),
            "options": event.data.get("options"),
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
        return {
            "type": "system_message",
            "conversation_id": conversation_id,
            "session_id": event.data.get("session_id"),
            "text": event.data.get("message"),
            "turn_id": event.data.get("turn_id"),
        }
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
        }
    if event.type == EventType.TURN_COMPLETED:
        return {
            "type": "turn_settled",
            "conversation_id": conversation_id,
            "session_id": event.data.get("session_id"),
            "message_id": event.data.get("message_id"),
            "queued_count": event.data.get("queued_count", 0),
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
            "task": event.data.get("task"),
        }
    if event.type == EventType.DELEGATION_COMPLETED:
        return {
            "type": "delegation_completed",
            "conversation_id": conversation_id,
            "child_session_id": event.data.get("child_session_id"),
            "result": event.data.get("result_summary"),
        }
    if event.type == EventType.DELEGATION_FAILED:
        return {
            "type": "delegation_failed",
            "conversation_id": conversation_id,
            "child_session_id": event.data.get("child_session_id"),
            "reason": event.data.get("reason"),
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
            "tool_name": event.data.get("tool_name"),
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
    if event.type == EventType.SESSION_COMPACTED:
        return {
            "type": "session_compacted",
            "conversation_id": conversation_id,
            "session_id": event.data.get("session_id"),
            "previous_session_id": event.data.get("previous_session_id"),
            "summary_preview": event.data.get("summary_preview"),
            "method": event.data.get("method"),
            "turns_compacted": event.data.get("turns_compacted"),
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
                "question": payload.get("question"),
                "options": payload.get("options"),
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
            "content": event.data.get("content", ""),
            "attachments": event.data.get("attachments", []),
            "turn_id": event.data.get("turn_id"),
        }
    return None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _can_access_owner(connection: AuthenticatedWebSocket, owner_email: str) -> bool:
    return connection.role == "admin" or connection.user_email == owner_email


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


async def _store_recovered_step_input_response(app: Any, task_id: str, response: str) -> None:
    async with app.state.session_factory() as session:
        row = await get_task(session, task_id)
        if row is None or not row.workflow_state:
            return
        state = dict(row.workflow_state)
        if state.get("pending_pause_type") != "step_input":
            return
        payload = dict(state.get("pending_pause_payload") or {})
        payload["response"] = response
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
                            "question": payload.get("question", ""),
                            "options": payload.get("options"),
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
                    "question": pause.question,
                    "options": pause.options,
                    "context": pause.context,
                }
            )
    return payloads
