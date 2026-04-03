"""WebSocket chat handler with first-message auth and runtime event fanout."""

from __future__ import annotations

import asyncio
import contextlib
import uuid
from collections import defaultdict, deque
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
from fastapi import WebSocket, WebSocketDisconnect
from prometheus_client import Counter, Gauge
from sqlalchemy import select

from cognis.api.error_sanitizer import sanitize_client_error_detail
from cognis.api.models import (
    WebSocketAuthenticated,
    WebSocketChunkGap,
    WebSocketError,
    WebSocketPong,
)
from cognis.api.serializers import agent_to_response
from cognis.core.agent_loop import PauseResolution
from cognis.core.compaction import ROTATION_TOTAL
from cognis.core.events import Event, EventType
from cognis.core.session import _to_conversation_model, _to_session_model
from cognis.logging import get_logger
from cognis.models.agent import AgentDefinition
from cognis.models.session import BLOCKED_STATES, ConversationModel, SessionModel, SessionStatus
from cognis.models.task import TaskDelivery
from cognis.runtime_context import current_agent_id, current_user_email
from cognis.store.models import Task
from cognis.store.queries import (
    get_agent,
    get_conversation,
    get_session_row,
    get_task,
)

logger = get_logger(__name__)

_NEW_SESSION_STREAM_GRACE = timedelta(seconds=30)


def _follow_up_turn_prompt(
    status: str | None,
    *,
    task_id: str | None = None,
    task_title: str | None = None,
    result_summary: str | None = None,
) -> str:
    """Build a system prompt for the follow-up turn after a task/delegation completes.

    When task details are available (task-based follow-ups), the prompt includes
    the task_id, title, and result summary so the agent can present the result
    without needing to look it up.  For delegation-based follow-ups (no task_id),
    a generic prompt is used.
    """
    status_name = (status or "updated").lower()

    # Task-specific prompts (from workflow engine)
    if task_id:
        title_str = f'"{task_title}"' if task_title else task_id
        if status_name == "completed":
            lines = [
                f"Background task {title_str} (task_id: {task_id}) has completed.",
            ]
            if result_summary:
                lines.append(f"\nResult summary: {result_summary}")
            lines.append(
                "\nPresent this result to the user concisely. "
                f'If you need the full detailed output, use the get_task_output tool with task_id="{task_id}".'
            )
            return "\n".join(lines)
        if status_name == "failed":
            lines = [
                f"Background task {title_str} (task_id: {task_id}) has failed.",
            ]
            if result_summary:
                lines.append(f"\nError details: {result_summary}")
            lines.append(
                "\nInform the user that the task has failed and briefly explain "
                "why based on the error details above. Do NOT attempt to complete "
                "the task yourself, do NOT call retry_task or create_task, and do "
                "NOT make additional tool calls to gather the task's results. "
                "Simply inform the user and let them decide what to do next."
            )
            return "\n".join(lines)
        if status_name == "cancelled":
            return (
                f"Background task {title_str} (task_id: {task_id}) was cancelled. "
                "Provide a brief follow-up to the user if warranted."
            )
        # Generic task update
        return (
            f"Background task {title_str} (task_id: {task_id}) status: {status_name}. "
            f"Summary: {result_summary or 'No summary available.'}. "
            "Provide a concise follow-up to the user."
        )

    # Delegation-specific prompts (from agent_loop async delegations)
    if status_name == "failed":
        return (
            "A delegated sub-session has failed. "
            "Review the recent delegation_failed event in the session history "
            "and provide a concise user-facing follow-up."
        )
    if status_name == "completed":
        return (
            "A delegated sub-session has completed. "
            "Review the recent delegation_completed event in the session history "
            "and present the result to the user."
        )
    return (
        "A background operation has completed. "
        "Review the recent events in the session history and provide a concise follow-up."
    )


# Per-conversation lock to prevent duplicate deferred session creation
# when multiple WebSocket tabs send a message simultaneously after /compact.
# Locks are lightweight (~200 bytes each) and are cleaned up when not held
# once the dict exceeds _MAX_DEFERRED_LOCKS entries.
_deferred_creation_locks: dict[str, asyncio.Lock] = {}
_MAX_DEFERRED_LOCKS = 500


class SessionCreationFailedError(RuntimeError):
    """Raised when the controller cannot create or recover a root session."""


async def _classify_turn_error(
    app: Any, error: Exception
) -> tuple[str, str, bool, dict[str, Any] | None]:
    lowered = str(error).lower()
    safe_detail = sanitize_client_error_detail(error, fallback="request failed")

    if isinstance(error, SessionCreationFailedError):
        return (
            "session_creation_failed",
            "Could not create a session. Try again or check the diagnostics page.",
            True,
            {"error_detail": safe_detail},
        )
    if isinstance(error, ValueError) and "no llm model configured" in lowered:
        return (
            "provider_not_configured:llm",
            "No LLM provider is configured. Go to Settings > Providers to add one.",
            True,
            {"error_detail": safe_detail},
        )

    provider_checks = []
    for provider_name in ("guardrails", "llm", "memory"):
        provider = getattr(app.state.providers, provider_name, None)
        if provider is None:
            continue
        try:
            provider_checks.append((provider_name, await provider.health()))
        except Exception:
            continue

    for provider_name, health in provider_checks:
        if health.status == "healthy":
            continue
        if provider_name == "guardrails":
            return (
                "provider_unreachable:guardrails",
                "Guardrails service is unreachable — tool calls are blocked until it recovers. Check that Intaris is running.",
                True,
                {"error_detail": safe_detail},
            )
        if provider_name == "llm":
            if "no llm model configured" in lowered or "not configured" in lowered:
                return (
                    "provider_not_configured:llm",
                    "No LLM provider is configured. Go to Settings > Providers to add one.",
                    True,
                    {"error_detail": safe_detail},
                )
            return (
                "provider_error:llm",
                "LLM provider returned an error. Check your provider configuration in Settings.",
                True,
                {"error_detail": safe_detail},
            )
        if provider_name == "memory":
            return (
                "provider_unreachable:memory",
                "Memory is currently unavailable — this conversation won't have access to past context.",
                True,
                {"error_detail": safe_detail},
            )

    if isinstance(error, (httpx.HTTPError, TimeoutError)):
        return (
            "provider_error:llm",
            "A provider request failed while processing this turn.",
            True,
            {"error_detail": safe_detail},
        )

    return (
        "turn_failed",
        "Turn execution failed.",
        True,
        {"error_detail": safe_detail},
    )


def _normalize_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value


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
MAX_QUEUED_MESSAGES = 5
DEFAULT_TURN_LIMIT = 3
DEFAULT_INBOUND_RATE_LIMIT = 10
DEFAULT_OUTBOUND_BUFFER = 100
DEFAULT_REPLAY_LIMIT = 200


@dataclass(slots=True)
class AuthenticatedWebSocket:
    connection_id: str
    websocket: WebSocket
    user_email: str
    role: str
    name: str | None = None
    subscriptions: set[str] = field(default_factory=set)
    send_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    pending_sends: int = 0
    recent_message_times: deque[float] = field(default_factory=deque)
    dropped_chunks: dict[str, int] = field(default_factory=dict)
    recovery_notified: set[str] = field(default_factory=set)

    async def send_json(self, payload: dict[str, Any]) -> None:
        """Send a WebSocket message with chunk backpressure handling."""
        message_type = str(payload.get("type", ""))
        message_id = str(payload.get("message_id", ""))
        critical = message_type != "chunk"
        if not critical and self.pending_sends >= DEFAULT_OUTBOUND_BUFFER:
            if message_id:
                self.dropped_chunks[message_id] = self.dropped_chunks.get(message_id, 0) + 1
                WS_CHUNK_GAP_FRAMES_TOTAL.inc()
            return

        self.pending_sends += 1
        try:
            async with self.send_lock:
                if message_id and message_id in self.dropped_chunks and message_type != "chunk_gap":
                    gap_frame = WebSocketChunkGap(
                        conversation_id=str(payload.get("conversation_id", "")),
                        session_id=payload.get("session_id"),
                        message_id=message_id,
                        dropped_count=self.dropped_chunks.pop(message_id),
                    )
                    await self.websocket.send_json(gap_frame.model_dump())
                await self.websocket.send_json(payload)
        finally:
            self.pending_sends = max(0, self.pending_sends - 1)

    def allow_inbound_message(self) -> bool:
        """Return True when the connection is under the inbound rate limit."""
        now = asyncio.get_running_loop().time()
        while self.recent_message_times and now - self.recent_message_times[0] > 1.0:
            self.recent_message_times.popleft()
        if len(self.recent_message_times) >= DEFAULT_INBOUND_RATE_LIMIT:
            return False
        self.recent_message_times.append(now)
        return True


@dataclass(slots=True)
class QueuedMessage:
    content: str
    system_initiated: bool = False
    received_at: datetime = field(default_factory=lambda: datetime.now(UTC))


class WebSocketConnectionManager:
    """Tracks WebSocket clients, active turns, and event fanout."""

    def __init__(self, app: Any) -> None:
        self.app = app
        self._connections: dict[str, AuthenticatedWebSocket] = {}
        self._by_conversation: dict[str, set[str]] = defaultdict(set)
        self._by_user: dict[str, set[str]] = defaultdict(set)
        self._active_turns: dict[str, asyncio.Task[None]] = {}
        self._turn_controls: dict[str, asyncio.Event] = {}
        self._turn_sessions: dict[str, str] = {}  # conversation_id → session_id
        self._queued_messages: dict[str, deque[QueuedMessage]] = defaultdict(deque)
        self._event_bus_registered = False
        self._register_event_bus_handler()

    def _register_event_bus_handler(self) -> None:
        if self._event_bus_registered:
            return
        self.app.state.event_bus.subscribe_all(self._handle_event)
        self._event_bus_registered = True

    async def connect(
        self, websocket: WebSocket, *, claims: dict[str, Any]
    ) -> AuthenticatedWebSocket:
        connection = AuthenticatedWebSocket(
            connection_id=f"ws_{uuid.uuid4().hex[:12]}",
            websocket=websocket,
            user_email=str(claims["sub"]),
            role=str(claims.get("role", "user")),
            name=claims.get("name"),
        )
        self._connections[connection.connection_id] = connection
        self._by_user[connection.user_email].add(connection.connection_id)
        WS_CONNECTIONS_TOTAL.inc()
        WS_CONNECTIONS_ACTIVE.set(len(self._connections))
        return connection

    def disconnect(self, connection: AuthenticatedWebSocket) -> None:
        self._connections.pop(connection.connection_id, None)
        self._by_user[connection.user_email].discard(connection.connection_id)
        for conversation_id in list(connection.subscriptions):
            self._by_conversation[conversation_id].discard(connection.connection_id)
        WS_CONNECTIONS_ACTIVE.set(len(self._connections))

    def subscribe(self, connection: AuthenticatedWebSocket, conversation_id: str) -> None:
        connection.subscriptions.add(conversation_id)
        self._by_conversation[conversation_id].add(connection.connection_id)

    async def send_error(
        self,
        connection: AuthenticatedWebSocket,
        *,
        code: str,
        message: str,
        recoverable: bool,
        detail: dict[str, Any] | None = None,
    ) -> None:
        error_detail = None
        if detail is not None and isinstance(detail.get("error_detail"), str):
            error_detail = str(detail["error_detail"])
        await connection.send_json(
            WebSocketError(
                code=code,
                message=message,
                recoverable=recoverable,
                error_detail=error_detail,
                detail=detail,
            ).model_dump()
        )

    async def send_to_conversation(
        self,
        conversation_id: str,
        payload: dict[str, Any],
    ) -> None:
        connection_ids = list(self._by_conversation.get(conversation_id, set()))
        coroutines = []
        for connection_id in connection_ids:
            connection = self._connections.get(connection_id)
            if connection is None:
                continue
            coroutines.append(connection.send_json(payload))
        if coroutines:
            await asyncio.gather(*coroutines, return_exceptions=True)

    def user_active_turn_count(self, user_email: str) -> int:
        count = 0
        for conversation_id in self._active_turns:
            connection_ids = self._by_conversation.get(conversation_id, set())
            for connection_id in connection_ids:
                connection = self._connections.get(connection_id)
                if connection is not None and connection.user_email == user_email:
                    count += 1
                    break
        return count

    async def enqueue_or_start_turn(
        self,
        connection: AuthenticatedWebSocket,
        *,
        conversation: ConversationModel,
        session: SessionModel,
        agent: Any,
        content: str,
    ) -> None:
        conversation_id = conversation.conversation_id
        self.subscribe(connection, conversation_id)
        if conversation_id in self._active_turns and not self._active_turns[conversation_id].done():
            queue = self._queued_messages[conversation_id]
            if len(queue) >= MAX_QUEUED_MESSAGES:
                await self.send_error(
                    connection,
                    code="queue_full",
                    message="Too many queued messages for this conversation",
                    recoverable=True,
                )
                return
            queue.append(QueuedMessage(content=content))
            await connection.send_json(
                {
                    "type": "queued",
                    "conversation_id": conversation_id,
                    "queued_count": len(queue),
                }
            )
            return

        if self.user_active_turn_count(connection.user_email) >= DEFAULT_TURN_LIMIT:
            await self.send_error(
                connection,
                code="turn_limit",
                message="Too many active turns for this user",
                recoverable=True,
            )
            return

        self._launch_turn(
            conversation=conversation,
            session=session,
            agent=agent,
            content=content,
        )

    def _launch_turn(
        self,
        *,
        conversation: ConversationModel,
        session: SessionModel,
        agent: Any,
        content: str,
        system_initiated: bool = False,
    ) -> None:
        control = asyncio.Event()
        conversation_id = conversation.conversation_id
        self._turn_controls[conversation_id] = control
        self._turn_sessions[conversation_id] = session.session_id
        self._active_turns[conversation_id] = asyncio.create_task(
            self._run_turn(
                conversation=conversation,
                session=session,
                agent=agent,
                content=content,
                system_initiated=system_initiated,
                cancel_event=control,
            )
        )

    async def cancel_turn(self, conversation_id: str) -> bool:
        """Cancel the active turn and all its child sub-sessions."""
        control = self._turn_controls.get(conversation_id)
        if control is None:
            return False
        control.set()
        # Also cancel child sub-sessions via the agent loop
        session_id = self._turn_sessions.get(conversation_id)
        if session_id:
            agent_loop = self.app.state.agent_loop
            cancelled = await agent_loop.cancel_children(session_id)
            if cancelled:
                logger.info(
                    "cancel_turn: cancelled child sub-sessions",
                    extra={"extra_data": {"count": cancelled, "session_id": session_id}},
                )
        return True

    async def _run_turn(
        self,
        *,
        conversation: ConversationModel,
        session: SessionModel,
        agent: Any,
        content: str,
        system_initiated: bool,
        cancel_event: asyncio.Event,
    ) -> None:
        conversation_id = conversation.conversation_id
        message_id = f"msg_{uuid.uuid4().hex[:12]}"
        _pre_turn_title = conversation.title
        try:
            current_user_email.set(session.user_email)
            current_agent_id.set(agent.agent_id)
            logger.info(
                "turn: started",
                extra={
                    "extra_data": {
                        "conversation_id": conversation_id,
                        "session_id": session.session_id,
                        "intaris_session_id": session.intaris_session_id,
                        "agent_id": agent.agent_id,
                        "user_email": session.user_email,
                        "system_initiated": system_initiated,
                    }
                },
            )
            if not system_initiated:
                decision = await self.app.state.decision_engine.decide(
                    user_message=content,
                    agent=agent,
                )
            else:
                decision = None

            if decision is not None and decision.decision == "delegate":
                workflow_id = await self._select_workflow(agent, content)
                task = await self.app.state.task_queue.submit(
                    created_by=session.user_email,
                    agent_id=agent.agent_id,
                    title=content[:80],
                    description=content,
                    source_type="chat",
                    source_ref=conversation_id,
                    delivery=TaskDelivery(mode="same_conversation"),
                    workflow_id=workflow_id,
                    status="queued",
                )
                await self.send_to_conversation(
                    conversation_id,
                    {
                        "type": "delegation_started",
                        "conversation_id": conversation_id,
                        "parent_session_id": session.session_id,
                        "child_session_id": task.task_id,
                        "mode": "task",
                        "agent_id": agent.agent_id,
                        "task": content,
                    },
                )
                await self.send_to_conversation(
                    conversation_id,
                    {
                        "type": "chunk",
                        "conversation_id": conversation_id,
                        "session_id": session.session_id,
                        "message_id": message_id,
                        "content": "Working on that in the background.",
                        "index": 0,
                    },
                )
                await self.send_to_conversation(
                    conversation_id,
                    {
                        "type": "message_complete",
                        "conversation_id": conversation_id,
                        "session_id": session.session_id,
                        "message_id": message_id,
                        "seq": 0,
                        "token_usage": None,
                        "queued_count": len(self._queued_messages.get(conversation_id, [])),
                    },
                )
                return

            async def on_token(delta: str) -> None:
                await self.send_to_conversation(
                    conversation_id,
                    {
                        "type": "chunk",
                        "conversation_id": conversation_id,
                        "session_id": session.session_id,
                        "message_id": message_id,
                        "content": delta,
                        "index": 0,
                    },
                )

            async def on_tool_call(
                tool_name: str, call_id: str, arguments: dict[str, Any] | None = None
            ) -> None:
                payload: dict[str, Any] = {
                    "type": "tool_call",
                    "conversation_id": conversation_id,
                    "session_id": session.session_id,
                    "call_id": call_id,
                    "tool_name": tool_name,
                    "status": "started",
                }
                if arguments is not None:
                    payload["arguments"] = arguments
                await self.send_to_conversation(conversation_id, payload)

            async def on_tool_result(
                call_id: str,
                tool_name: str,
                result: str,
                is_error: bool,
                duration_ms: int | None,
                evaluation: dict[str, Any] | None = None,
            ) -> None:
                payload: dict[str, Any] = {
                    "type": "tool_result",
                    "conversation_id": conversation_id,
                    "session_id": session.session_id,
                    "call_id": call_id,
                    "tool_name": tool_name,
                    "result": result,
                    "is_error": is_error,
                    "duration_ms": duration_ms,
                }
                if evaluation:
                    payload["evaluation"] = evaluation
                await self.send_to_conversation(conversation_id, payload)

            await self.app.state.workflow_engine.run_direct_turn(
                conversation=conversation,
                session=session,
                agent=agent,
                user_message=content,
                system_initiated=system_initiated,
                on_progress=on_token,
                on_tool_call=on_tool_call,
                on_tool_result=on_tool_result,
                cancel_event=cancel_event,
            )

            async with self.app.state.session_factory() as db_session:
                row = await get_conversation(db_session, conversation_id)
                if row is not None:
                    row.last_message_at = datetime.now(UTC)
                    row.updated_at = row.last_message_at
                    await db_session.commit()

            last_seq = 0
            try:
                entry = await self.app.state.session_cache.refresh(session)
                last_seq = entry.last_event_seq
            except Exception:
                logger.warning(
                    "Post-turn session cache refresh failed (response already sent)",
                    extra={
                        "extra_data": {
                            "conversation_id": conversation_id,
                            "session_id": session.session_id,
                        }
                    },
                )
                cached = self.app.state.session_cache.get_entry(session.session_id)
                if cached is not None:
                    last_seq = cached.last_event_seq
            context_usage = self.app.state.session_cache.get_context_usage(session.session_id)
            await self.send_to_conversation(
                conversation_id,
                {
                    "type": "message_complete",
                    "conversation_id": conversation_id,
                    "session_id": session.session_id,
                    "message_id": message_id,
                    "seq": last_seq,
                    "token_usage": None,
                    "context_usage": context_usage,
                    "queued_count": len(self._queued_messages.get(conversation_id, [])),
                },
            )
            # Notify clients if the conversation title changed during
            # this turn (Intaris generates titles alongside intentions).
            if conversation.title and conversation.title != _pre_turn_title:
                await self.send_to_conversation(
                    conversation_id,
                    {
                        "type": "conversation_updated",
                        "conversation_id": conversation_id,
                        "title": conversation.title,
                    },
                )

            logger.info(
                "turn: completed",
                extra={
                    "extra_data": {
                        "conversation_id": conversation_id,
                        "session_id": session.session_id,
                        "last_seq": last_seq,
                    }
                },
            )
        except asyncio.CancelledError:
            await self.send_to_conversation(
                conversation_id,
                WebSocketError(
                    code="turn_cancelled",
                    message="The current turn was cancelled.",
                    recoverable=True,
                ).model_dump(),
            )
        except Exception as exc:
            logger.exception(
                "WebSocket turn failed",
                extra={"extra_data": {"conversation_id": conversation_id}},
            )
            code, message, recoverable, detail = await _classify_turn_error(self.app, exc)
            await self.send_to_conversation(
                conversation_id,
                WebSocketError(
                    code=code,
                    message=message,
                    recoverable=recoverable,
                    error_detail=detail.get("error_detail") if detail else None,
                    detail=detail,
                ).model_dump(),
            )
        finally:
            self._active_turns.pop(conversation_id, None)
            self._turn_controls.pop(conversation_id, None)
            self._turn_sessions.pop(conversation_id, None)
            queue = self._queued_messages.get(conversation_id)
            if queue:
                queued = queue.popleft()
                runtime = await _load_conversation_runtime(
                    self.app, conversation_id, user_message=queued.content
                )
                if runtime is not None:
                    next_conversation, next_session, next_agent = runtime
                    self._launch_turn(
                        conversation=next_conversation,
                        session=next_session,
                        agent=next_agent,
                        content=queued.content,
                        system_initiated=queued.system_initiated,
                    )

    # Title generation has been moved to Intaris. The IntentionBarrier
    # generates a session title alongside the intention on every user
    # message. Cognis reads it in context assembly and syncs it to the
    # conversation. The _generate_auto_title method has been removed.

    async def _select_workflow(self, agent: Any, task_description: str) -> str:
        execution = agent.execution or {}
        available_ids = execution.get("available_workflow_ids")
        available_workflows = await self.app.state.workflow_registry.list_all(
            owner_email=agent.owner_email
        )
        if isinstance(available_ids, list) and available_ids:
            available_workflows = [
                w for w in available_workflows if w.workflow_id in set(available_ids)
            ]
        if not available_workflows:
            default_workflow_id = execution.get("default_workflow_id")
            return (
                default_workflow_id if isinstance(default_workflow_id, str) else "system:research"
            )
        from cognis.core.decision import select_workflow

        selection = await select_workflow(
            llm=self.app.state.providers.llm,
            task_description=task_description,
            available_workflows=[
                {
                    "workflow_id": workflow.workflow_id,
                    "name": workflow.name,
                    "criteria": workflow.criteria,
                }
                for workflow in available_workflows
            ],
            default_workflow_id=execution.get("default_workflow_id", "system:research"),
            selection_mode=execution.get("workflow_selection_mode", "automatic"),
        )
        return selection.workflow_id

    async def replay(
        self,
        connection: AuthenticatedWebSocket,
        *,
        conversation_id: str,
        last_seq: int,
    ) -> None:
        runtime = await _load_conversation_runtime(self.app, conversation_id)
        if runtime is None:
            await self.send_error(
                connection, code="not_found", message="Conversation not found", recoverable=False
            )
            return
        conversation, session, _agent = runtime
        if not _can_access_owner(connection, conversation.user_email):
            await self.send_error(
                connection,
                code="forbidden",
                message="Conversation access denied",
                recoverable=False,
            )
            return
        self.subscribe(connection, conversation_id)
        # Always tolerate missing event streams on reconnect — the stream
        # may not exist yet (new session) or may have been purged.
        result = await self.app.state.providers.guardrails.read_events(
            session_id=session.intaris_session_id or session.session_id,
            after_seq=last_seq,
            limit=DEFAULT_REPLAY_LIMIT,
            allow_missing_stream=True,
        )
        replayed = 0
        for item in result.events:
            event_type = item.get("type")
            if event_type == "assistant_message":
                message_id = f"replay_{item.get('seq', uuid.uuid4().hex)}"
                await connection.send_json(
                    {
                        "type": "chunk",
                        "conversation_id": conversation_id,
                        "session_id": session.session_id,
                        "message_id": message_id,
                        "content": str(item.get("data", {}).get("content", "")),
                        "index": 0,
                    }
                )
                await connection.send_json(
                    {
                        "type": "message_complete",
                        "conversation_id": conversation_id,
                        "session_id": session.session_id,
                        "message_id": message_id,
                        "seq": item.get("seq", 0),
                        "token_usage": None,
                        "queued_count": len(self._queued_messages.get(conversation_id, [])),
                    }
                )
                replayed += 1
            elif event_type == "task_result":
                await connection.send_json(
                    {
                        "type": "workflow_completed",
                        "conversation_id": conversation_id,
                        "task_id": item.get("data", {}).get("task_id"),
                        "result": item.get("data", {}).get("result_summary"),
                    }
                )
                replayed += 1
            elif event_type == "task_failed":
                await connection.send_json(
                    {
                        "type": "workflow_failed",
                        "conversation_id": conversation_id,
                        "task_id": item.get("data", {}).get("task_id"),
                        "reason": item.get("data", {}).get("result_summary"),
                    }
                )
                replayed += 1
            elif event_type == "task_cancelled":
                await connection.send_json(
                    {
                        "type": "workflow_cancelled",
                        "conversation_id": conversation_id,
                        "task_id": item.get("data", {}).get("task_id"),
                        "reason": item.get("data", {}).get("result_summary") or "cancelled",
                    }
                )
                replayed += 1
            elif event_type == "delegation":
                data = item.get("data", {})
                status = data.get("status")
                if status == "completed":
                    await connection.send_json(
                        {
                            "type": "delegation_completed",
                            "conversation_id": conversation_id,
                            "child_session_id": data.get("child_session_id"),
                            "result": data.get("result_summary"),
                        }
                    )
                elif status == "failed":
                    await connection.send_json(
                        {
                            "type": "delegation_failed",
                            "conversation_id": conversation_id,
                            "child_session_id": data.get("child_session_id"),
                            "reason": data.get("error"),
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
                        }
                    )
                replayed += 1
        pending_pauses = await _load_pending_task_prompts(self.app, conversation_id)
        for payload in pending_pauses:
            await connection.send_json(payload)
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
        await connection.send_json(
            {
                "type": "reconnected",
                "conversation_id": conversation_id,
                "missed_events_count": replayed,
            }
        )
        WS_RECONNECTIONS_TOTAL.inc()
        WS_MISSED_EVENTS_REPLAYED.inc(replayed)

    async def _handle_event(self, event: Event) -> None:
        if event.type == EventType.FOLLOW_UP_TURN_REQUESTED:
            await self._handle_follow_up_turn_request(event)
            return
        conversation_id = await self._resolve_conversation_id(event)
        if conversation_id is None:
            return
        payload = _event_to_payload(event, conversation_id)
        if payload is None:
            return
        await self.send_to_conversation(conversation_id, payload)

    async def _handle_follow_up_turn_request(self, event: Event) -> None:
        conversation_id = event.data.get("conversation_id")
        if not isinstance(conversation_id, str):
            return
        # Run the follow-up turn even without active WebSocket clients.
        # The turn results persist to Intaris and are visible when the
        # user reconnects.  Streaming callbacks are simply skipped.
        prompt = _follow_up_turn_prompt(
            event.data.get("status") if isinstance(event.data.get("status"), str) else None,
            task_id=event.data.get("task_id")
            if isinstance(event.data.get("task_id"), str)
            else None,
            task_title=event.data.get("task_title")
            if isinstance(event.data.get("task_title"), str)
            else None,
            result_summary=event.data.get("result_summary")
            if isinstance(event.data.get("result_summary"), str)
            else None,
        )
        if conversation_id in self._active_turns and not self._active_turns[conversation_id].done():
            # Turn already active — queue the follow-up instead of dropping it
            self._queued_messages[conversation_id].append(
                QueuedMessage(content=prompt, system_initiated=True)
            )
            return
        runtime = await _load_conversation_runtime(self.app, conversation_id)
        if runtime is None:
            return
        conversation, session, agent = runtime
        if conversation.status != "active":
            return
        self._launch_turn(
            conversation=conversation,
            session=session,
            agent=agent,
            content=prompt,
            system_initiated=True,
        )

    async def _resolve_conversation_id(self, event: Event) -> str | None:
        if isinstance(event.data.get("conversation_id"), str):
            return str(event.data["conversation_id"])
        session_id = event.data.get("session_id")
        if isinstance(session_id, str):
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


async def handle_websocket(websocket: WebSocket) -> None:
    await websocket.accept()
    timeout_seconds = getattr(websocket.app.state, "ws_auth_timeout_seconds", 10)
    try:
        first_message = await asyncio.wait_for(websocket.receive_json(), timeout=timeout_seconds)
    except TimeoutError:
        await websocket.close(code=4401, reason="Authentication timeout")
        return
    except WebSocketDisconnect:
        return

    if first_message.get("type") != "auth" or not isinstance(first_message.get("token"), str):
        await websocket.close(code=4401, reason="Authentication required")
        return

    try:
        claims = websocket.app.state.auth_provider.verify_jwt(
            first_message["token"],
            audience=["cognis"],
        )
    except Exception:
        await websocket.close(code=4401, reason="Invalid token")
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
                conversation_id = message.get("conversation_id")
                content = message.get("content")
                if (
                    not isinstance(conversation_id, str)
                    or not isinstance(content, str)
                    or not content.strip()
                ):
                    await manager.send_error(
                        connection,
                        code="validation_error",
                        message="conversation_id and content are required",
                        recoverable=True,
                    )
                    continue
                runtime = await _load_conversation_runtime(
                    websocket.app, conversation_id, user_message=content
                )
                if runtime is None:
                    await manager.send_error(
                        connection,
                        code="not_found",
                        message="Conversation not found",
                        recoverable=False,
                    )
                    continue
                conversation, session, agent = runtime
                if not _can_access_owner(connection, conversation.user_email):
                    await manager.send_error(
                        connection,
                        code="forbidden",
                        message="Conversation access denied",
                        recoverable=False,
                    )
                    continue
                if conversation.status in {"archived", "deleted"}:
                    await manager.send_error(
                        connection,
                        code="conflict",
                        message="Conversation is not active",
                        recoverable=False,
                    )
                    continue

                # -------------------------------------------------------
                # Session state enforcement
                # -------------------------------------------------------
                if session.status in BLOCKED_STATES:
                    if session.status == SessionStatus.SUSPENDED:
                        await manager.send_error(
                            connection,
                            code="session_suspended",
                            message="Session is suspended. Resolve the pending escalation to continue.",
                            recoverable=True,
                        )
                    else:
                        await manager.send_error(
                            connection,
                            code="session_ended",
                            message="This session has ended. Use /new to start a fresh conversation.",
                            recoverable=False,
                        )
                    continue

                # -------------------------------------------------------
                # Slash commands
                # -------------------------------------------------------
                stripped = content.strip()

                # /compact or /summarize — trigger manual compaction
                if stripped in ("/compact", "/summarize"):
                    if (
                        conversation_id in manager._active_turns
                        and not manager._active_turns[conversation_id].done()
                    ):  # noqa: SLF001
                        await manager.send_error(
                            connection,
                            code="turn_active",
                            message="Cannot compact while a turn is active. Wait for it to finish or cancel it.",
                            recoverable=True,
                        )
                    else:
                        await _handle_slash_compact(
                            websocket.app, manager, connection, conversation, session
                        )
                    continue

                # /new, /reset, /clear — start fresh
                if stripped in ("/new", "/reset", "/clear"):
                    if (
                        conversation_id in manager._active_turns
                        and not manager._active_turns[conversation_id].done()
                    ):  # noqa: SLF001
                        await manager.send_error(
                            connection,
                            code="turn_active",
                            message="Cannot reset while a turn is active. Wait for it to finish or cancel it.",
                            recoverable=True,
                        )
                    else:
                        await _handle_slash_new(
                            websocket.app, manager, connection, conversation, session, agent
                        )
                    continue

                # /context — show context window usage
                if stripped == "/context":
                    await _handle_slash_context(
                        websocket.app, manager, connection, conversation, session
                    )
                    continue

                # /info — show session info, context, Intaris stats
                if stripped == "/info":
                    await _handle_slash_info(
                        websocket.app, manager, connection, conversation, session
                    )
                    continue

                # /model [name] — list or switch LLM model
                if stripped == "/model" or stripped.startswith("/model "):
                    arg = stripped[6:].strip() if len(stripped) > 6 else ""
                    await _handle_slash_model(
                        websocket.app, manager, connection, conversation, session, arg
                    )
                    continue

                # /thinking [level] — list or switch reasoning effort
                if stripped == "/thinking" or stripped.startswith("/thinking "):
                    arg = stripped[9:].strip() if len(stripped) > 9 else ""
                    await _handle_slash_thinking(
                        websocket.app, manager, connection, conversation, session, arg
                    )
                    continue

                # /lsp — show LSP diagnostics status
                if stripped == "/lsp":
                    await _handle_slash_lsp(websocket.app, manager, connection, conversation)
                    continue

                # /help — show available commands
                if stripped == "/help":
                    await _handle_slash_help(manager, connection, conversation)
                    continue

                if stripped.startswith("/approve") or stripped.startswith("/deny"):
                    is_approve = stripped.startswith("/approve")
                    cmd_word = "/approve" if is_approve else "/deny"
                    note = stripped[len(cmd_word) :].strip() or None
                    esc_decision = "approve" if is_approve else "deny"

                    pending = websocket.app.state.pause_waiter.find_pending(
                        pause_type="escalation",
                        conversation_id=conversation_id,
                    )
                    if pending is None:
                        await manager.send_to_conversation(
                            conversation_id,
                            {
                                "type": "system_message",
                                "conversation_id": conversation_id,
                                "text": "No pending escalation to resolve.",
                            },
                        )
                        continue

                    tool_name = (pending.context or {}).get("tool_name", "tool call")
                    intaris_call_id = (pending.context or {}).get("call_id", pending.pause_id)

                    # Try the unified notification service first
                    current_user_email.set(connection.user_email)
                    svc = getattr(websocket.app.state, "notification_service", None)
                    if svc is not None:
                        await svc.resolve(
                            pending.pause_id,
                            esc_decision,
                            {"note": note or ""},
                        )
                    else:
                        # Legacy fallback
                        websocket.app.state.pause_waiter.resolve(
                            pending.pause_id,
                            PauseResolution(
                                decision=esc_decision,
                                data={"note": note or ""},
                            ),
                        )
                        with contextlib.suppress(Exception):
                            await websocket.app.state.providers.guardrails.submit_decision(
                                intaris_call_id, esc_decision, note
                            )

                    # Emit system message for the action
                    verb = "approved" if is_approve else "denied"
                    note_suffix = f": {note}" if note else ""
                    await manager.send_to_conversation(
                        conversation_id,
                        {
                            "type": "system_message",
                            "conversation_id": conversation_id,
                            "text": f"User {verb} {tool_name}{note_suffix}",
                        },
                    )
                    continue

                # -------------------------------------------------------
                # Queue messages while an escalation is pending
                # -------------------------------------------------------
                pending_esc = websocket.app.state.pause_waiter.find_pending(
                    pause_type="escalation",
                    conversation_id=conversation_id,
                )
                if pending_esc is not None:
                    manager._queued_messages[conversation_id].append(  # noqa: SLF001
                        QueuedMessage(content=content)
                    )
                    await manager.send_to_conversation(
                        conversation_id,
                        {
                            "type": "queued",
                            "conversation_id": conversation_id,
                            "queued_count": len(
                                manager._queued_messages.get(conversation_id, [])  # noqa: SLF001
                            ),
                            "reason": "Waiting for escalation resolution. "
                            "Use /approve or /deny, or use the buttons above.",
                        },
                    )
                    continue

                await manager.enqueue_or_start_turn(
                    connection,
                    conversation=conversation,
                    session=session,
                    agent=agent,
                    content=content,
                )
                continue

            if message_type == "cancel":
                conversation_id = message.get("conversation_id")
                if not isinstance(conversation_id, str):
                    await manager.send_error(
                        connection,
                        code="validation_error",
                        message="conversation_id is required",
                        recoverable=True,
                    )
                    continue
                runtime = await _load_conversation_runtime(websocket.app, conversation_id)
                if runtime is None:
                    await manager.send_error(
                        connection,
                        code="not_found",
                        message="Conversation not found",
                        recoverable=False,
                    )
                    continue
                conversation, _session, _agent = runtime
                if not _can_access_owner(connection, conversation.user_email):
                    await manager.send_error(
                        connection,
                        code="forbidden",
                        message="Conversation access denied",
                        recoverable=False,
                    )
                    continue
                cancelled = await manager.cancel_turn(conversation_id)
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
                continue

            if message_type == "resolve_escalation":
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
                    continue
                if decision not in ("approve", "deny"):
                    await manager.send_error(
                        connection,
                        code="validation_error",
                        message="decision must be 'approve' or 'deny'",
                        recoverable=True,
                    )
                    continue
                current_user_email.set(connection.user_email)
                # Look up the pending pause to get tool_name for the system message
                # Try both notification_id (call_id) and legacy pause_id formats
                pending_pause = websocket.app.state.pause_waiter.find_pending(
                    pause_type="escalation",
                )
                # Match by call_id in context or by pause_id
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
                # Try the unified notification service first (call_id is notification_id)
                svc = getattr(websocket.app.state, "notification_service", None)
                resolved = False
                if svc is not None:
                    resolved = await svc.resolve(
                        call_id,
                        decision,
                        {"note": note if isinstance(note, str) else ""},
                    )
                if not resolved:
                    # Legacy fallback: try PauseWaiter directly
                    pause_id = f"escalation:{call_id}"
                    resolved = websocket.app.state.pause_waiter.resolve(
                        pause_id,
                        PauseResolution(
                            decision=decision,
                            data={"note": note if isinstance(note, str) else ""},
                        ),
                    )
                    if not resolved:
                        # Also try call_id directly as pause_id
                        resolved = websocket.app.state.pause_waiter.resolve(
                            call_id,
                            PauseResolution(
                                decision=decision,
                                data={"note": note if isinstance(note, str) else ""},
                            ),
                        )
                if not resolved:
                    # No pending pause — check Intaris as fallback
                    pending_escalations = (
                        await websocket.app.state.providers.guardrails.list_pending_escalations()
                    )
                    if not any(item.call_id == call_id for item in pending_escalations):
                        await manager.send_error(
                            connection,
                            code="not_found",
                            message="Escalation not found or already resolved",
                            recoverable=True,
                        )
                        continue
                # Submit to Intaris for audit trail (if not already done by notification service)
                if svc is None:
                    with contextlib.suppress(Exception):
                        await websocket.app.state.providers.guardrails.submit_decision(
                            call_id, decision, note if isinstance(note, str) else None
                        )
                # Emit system message for the action
                verb = "approved" if decision == "approve" else "denied"
                note_str = note if isinstance(note, str) and note else ""
                note_suffix = f": {note_str}" if note_str else ""
                # Find conversation_id from the pending pause
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
                continue

            if message_type == "gate_response":
                task_id = message.get("task_id")
                action = message.get("action")
                step_name = message.get("step_name")
                feedback = message.get("feedback")
                if not isinstance(task_id, str) or not isinstance(action, str):
                    await manager.send_error(
                        connection,
                        code="validation_error",
                        message="task_id and action are required",
                        recoverable=True,
                    )
                    continue
                if await _load_task_for_user(websocket.app, connection, task_id) is None:
                    await manager.send_error(
                        connection,
                        code="not_found",
                        message="Task not found",
                        recoverable=True,
                    )
                    continue
                if isinstance(feedback, str) and feedback:
                    await _persist_task_feedback(websocket.app, task_id, feedback)
                current_user_email.set(connection.user_email)
                # Try the unified notification service first
                svc = getattr(websocket.app.state, "notification_service", None)
                resolved = False
                if svc is not None:
                    notif = await svc.find_by_task(
                        task_id, notification_type="gate", status="pending"
                    )
                    if notif is not None:
                        resolved = await svc.resolve(
                            notif.notification_id,
                            action,
                            {"feedback": feedback if isinstance(feedback, str) else ""},
                        )
                if not resolved:
                    # Legacy fallback: direct PauseWaiter
                    pause = websocket.app.state.pause_waiter.find_pending(
                        task_id=task_id,
                        step_name=step_name if isinstance(step_name, str) else None,
                        pause_type="gate",
                    )
                    if pause is None:
                        await manager.send_error(
                            connection,
                            code="not_found",
                            message="No pending gate",
                            recoverable=True,
                        )
                        continue
                    websocket.app.state.pause_waiter.resolve(
                        pause.pause_id,
                        PauseResolution(
                            decision=action,
                            data={"feedback": feedback if isinstance(feedback, str) else ""},
                        ),
                    )
                continue

            if message_type == "step_response":
                task_id = message.get("task_id")
                step_name = message.get("step_name")
                response = message.get("response", "")
                if not isinstance(task_id, str):
                    await manager.send_error(
                        connection,
                        code="validation_error",
                        message="task_id is required",
                        recoverable=True,
                    )
                    continue
                if await _load_task_for_user(websocket.app, connection, task_id) is None:
                    await manager.send_error(
                        connection,
                        code="not_found",
                        message="Task not found",
                        recoverable=True,
                    )
                    continue
                current_user_email.set(connection.user_email)
                # Try the unified notification service first
                svc = getattr(websocket.app.state, "notification_service", None)
                resolved = False
                if svc is not None:
                    notif = await svc.find_by_task(
                        task_id, notification_type="step_question", status="pending"
                    )
                    if notif is not None:
                        resolved = await svc.resolve(
                            notif.notification_id,
                            "continue",
                            {"response": str(response)},
                        )
                if not resolved:
                    # Legacy fallback: direct PauseWaiter
                    pause = websocket.app.state.pause_waiter.find_pending(
                        task_id=task_id,
                        step_name=step_name if isinstance(step_name, str) else None,
                        pause_type="step_input",
                    )
                    if pause is None:
                        await manager.send_error(
                            connection,
                            code="not_found",
                            message="No pending step question",
                            recoverable=True,
                        )
                        continue
                    websocket.app.state.pause_waiter.resolve(
                        pause.pause_id,
                        PauseResolution(decision="continue", data={"response": str(response)}),
                    )
                if not websocket.app.state.task_queue.has_active_run(task_id):
                    # Post-restart recovery path
                    await _store_recovered_step_input_response(
                        websocket.app,
                        task_id,
                        str(response),
                    )
                    pause = websocket.app.state.pause_waiter.find_pending(
                        task_id=task_id, pause_type="step_input"
                    )
                    if pause is not None:
                        websocket.app.state.pause_waiter.clear(pause.pause_id)
                    try:
                        await websocket.app.state.task_queue.resume_task(task_id)
                    except ValueError as exc:
                        await manager.send_error(
                            connection,
                            code="conflict",
                            message=str(exc),
                            recoverable=True,
                        )
                        continue
                continue

            if message_type == "reconnect":
                conversation_id = message.get("conversation_id")
                last_seq = message.get("last_seq", 0)
                if not isinstance(conversation_id, str) or not isinstance(last_seq, int):
                    await manager.send_error(
                        connection,
                        code="validation_error",
                        message="conversation_id and last_seq are required",
                        recoverable=True,
                    )
                    continue
                await manager.replay(connection, conversation_id=conversation_id, last_seq=last_seq)
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


async def _handle_slash_compact(
    app: Any,
    manager: WebSocketConnectionManager,
    connection: AuthenticatedWebSocket,
    conversation: ConversationModel,
    session: SessionModel,
) -> None:
    """Handle /compact or /summarize slash command.

    Triggers LLM compaction on the current session, marks it completed
    with ``completion_reason="compacted"``, and notifies the client.
    Session creation is deferred until the next user message.
    """
    conversation_id = conversation.conversation_id

    # Notify start
    await manager.send_to_conversation(
        conversation_id,
        {
            "type": "system_message",
            "conversation_id": conversation_id,
            "text": "Compacting conversation history...",
        },
    )

    try:
        compaction_result = await app.state.compaction_strategy.compact(session, trigger="manual")
    except Exception:
        logger.exception(
            "Slash compact failed",
            extra={"extra_data": {"session_id": session.session_id}},
        )
        await manager.send_error(
            connection,
            code="compaction_failed",
            message="Compaction failed. Try again or continue chatting.",
            recoverable=True,
        )
        return

    if not compaction_result.compacted:
        await manager.send_to_conversation(
            conversation_id,
            {
                "type": "system_message",
                "conversation_id": conversation_id,
                "text": "Not enough conversation history to compact.",
            },
        )
        return

    # Mark current session as completed (deferred creation)
    await app.state.session_manager.mark_completed(
        session.session_id,
        result_summary=f"Compacted ({compaction_result.method})",
        completion_reason="compacted",
    )

    # Send compaction event to clients
    summary_preview = (compaction_result.summary or "")[:500]
    await manager.send_to_conversation(
        conversation_id,
        {
            "type": "session_compacted",
            "conversation_id": conversation_id,
            "session_id": session.session_id,
            "previous_session_id": session.session_id,
            "summary_preview": summary_preview,
            "method": compaction_result.method,
            "turns_compacted": compaction_result.turns_compacted,
        },
    )


async def _handle_slash_new(
    app: Any,
    manager: WebSocketConnectionManager,
    connection: AuthenticatedWebSocket,
    conversation: ConversationModel,
    session: SessionModel,
    agent: Any,
) -> None:
    """Handle /new, /reset, or /clear slash command.

    For web context: creates a new conversation + session.
    For channel-bound context: creates a new root session within the same conversation.
    """
    conversation_id = conversation.conversation_id

    if conversation.context.type == "web":
        # Web context: create a new conversation entirely
        try:
            (
                new_conversation,
                new_session,
            ) = await app.state.session_manager.create_conversation_with_root_session(
                user_email=connection.user_email,
                agent_id=agent.agent_id,
                context=conversation.context,
                intention=f"New conversation with {agent.name}",
            )
        except Exception:
            logger.exception("Slash /new failed to create conversation")
            await manager.send_error(
                connection,
                code="creation_failed",
                message="Could not create a new conversation.",
                recoverable=True,
            )
            return

        # Mark old session completed
        await app.state.session_manager.mark_completed(
            session.session_id,
            result_summary="User started new conversation",
            completion_reason="user_reset",
        )

        await manager.send_to_conversation(
            conversation_id,
            {
                "type": "conversation_created",
                "conversation_id": new_conversation.conversation_id,
                "old_conversation_id": conversation_id,
            },
        )
    else:
        # Channel-bound: create new root session within same conversation
        try:
            new_session = await app.state.session_manager.rotate_session(
                conversation_id=conversation_id,
                current_session=session,
                intention=f"Conversation with {agent.name}",
                completion_reason="user_reset",
            )
        except Exception:
            logger.exception("Slash /new failed to rotate session")
            await manager.send_error(
                connection,
                code="creation_failed",
                message="Could not create a new session.",
                recoverable=True,
            )
            return

        await manager.send_to_conversation(
            conversation_id,
            {
                "type": "session_reset",
                "conversation_id": conversation_id,
                "session_id": new_session.session_id,
                "previous_session_id": session.session_id,
            },
        )


async def _handle_slash_context(
    app: Any,
    manager: Any,
    connection: Any,
    conversation: Any,
    session: Any,
) -> None:
    """Handle /context — display context window usage."""
    conversation_id = conversation.conversation_id
    usage = app.state.session_cache.get_context_usage(session.session_id)

    if usage is None:
        await manager.send_to_conversation(
            conversation_id,
            {
                "type": "system_message",
                "conversation_id": conversation_id,
                "text": "Context usage: no data yet (send a message first).",
            },
        )
        return

    lines = [
        f"Context: {usage['prompt_tokens']:,} / {usage['max_context_tokens']:,} tokens ({usage['percentage']}%)",
        f"Model: {usage['model']}",
        f"Compaction threshold: {int(app.state.compaction_strategy.compaction_threshold * 100)}%",
    ]

    await manager.send_to_conversation(
        conversation_id,
        {
            "type": "system_message",
            "conversation_id": conversation_id,
            "text": "\n".join(lines),
        },
    )


async def _handle_slash_info(
    app: Any,
    manager: Any,
    connection: Any,
    conversation: Any,
    session: Any,
) -> None:
    """Handle /info — display session details, context, and Intaris stats."""
    conversation_id = conversation.conversation_id
    lines: list[str] = []

    # Session metadata
    lines.append(f"Session: {session.session_id}")
    lines.append(f"Agent: {session.agent_id}")
    lines.append(f"Status: {session.status}")

    # Context usage + model + reasoning effort
    usage = app.state.session_cache.get_context_usage(session.session_id)
    if usage:
        lines.append(f"Model: {usage['model']}")
        lines.append(
            f"Context: {usage['prompt_tokens']:,} / {usage['max_context_tokens']:,} tokens ({usage['percentage']}%)"
        )
    reasoning = app.state.session_cache.get_reasoning_effort_override(session.session_id)
    if reasoning:
        lines.append(f"Reasoning effort: {reasoning}")

    # Intaris session stats
    intaris_sid = session.intaris_session_id or session.session_id
    try:
        intaris_session = await app.state.providers.guardrails.get_session(intaris_sid)
        if intaris_session.intention:
            lines.append(f"Intention: {intaris_session.intention}")
        stats_parts = [f"{intaris_session.total_calls} total"]
        if intaris_session.approved_count:
            stats_parts.append(f"{intaris_session.approved_count} approved")
        if intaris_session.denied_count:
            stats_parts.append(f"{intaris_session.denied_count} denied")
        if intaris_session.escalated_count:
            stats_parts.append(f"{intaris_session.escalated_count} escalated")
        lines.append(f"Tool calls: {', '.join(stats_parts)}")
    except Exception:
        lines.append("Intaris stats: unavailable")

    if session.started_at:
        lines.append(f"Started: {session.started_at}")

    await manager.send_to_conversation(
        conversation_id,
        {
            "type": "system_message",
            "conversation_id": conversation_id,
            "text": "\n".join(lines),
        },
    )


_DEFAULT_REASONING_EFFORTS: dict[str, list[str]] = {
    "anthropic": ["low", "medium", "high"],
    "openai": ["low", "medium", "high"],
}


def _infer_reasoning_efforts(model: str) -> list[str]:
    """Best-effort reasoning effort levels for a model."""
    m = model.lower()
    if "opus" in m:
        return ["low", "medium", "high", "max"]
    if any(p in m for p in ("claude", "anthropic")):
        return ["low", "medium", "high"]
    if any(p in m for p in ("o1", "o3", "o4")):
        return ["low", "medium", "high"]
    if "gpt-5" in m:
        return ["none", "low", "medium", "high"]
    return ["low", "medium", "high"]


async def _handle_slash_model(
    app: Any,
    manager: Any,
    connection: Any,
    conversation: Any,
    session: Any,
    arg: str,
) -> None:
    """Handle /model [name] — list or switch LLM model."""
    conversation_id = conversation.conversation_id
    session_id = session.session_id

    if not arg:
        # List available models with current highlighted
        try:
            model_ids = await app.state.providers.llm.list_model_ids()
        except Exception:
            model_ids = []

        current = app.state.session_cache.get_model_override(session_id)
        if not current:
            usage = app.state.session_cache.get_context_usage(session_id)
            current = usage["model"] if usage else None

        if not model_ids:
            text = "No models configured. Add LLM providers in Settings → Providers."
        else:
            lines = ["Available models:"]
            for mid in model_ids:
                marker = " *" if mid == current else ""
                lines.append(f"  {mid}{marker}")
            lines.append(f"\nCurrent: {current or 'system default'}")
            lines.append("Usage: /model <model_name>")
            text = "\n".join(lines)

        await manager.send_to_conversation(
            conversation_id,
            {"type": "system_message", "conversation_id": conversation_id, "text": text},
        )
        return

    # Switch model
    try:
        model_ids = await app.state.providers.llm.list_model_ids()
    except Exception:
        model_ids = []

    if model_ids and arg not in model_ids:
        await manager.send_to_conversation(
            conversation_id,
            {
                "type": "system_message",
                "conversation_id": conversation_id,
                "text": f"Unknown model: {arg}\nAvailable: {', '.join(model_ids)}",
            },
        )
        return

    app.state.session_cache.set_model_override(session_id, arg)
    await manager.send_to_conversation(
        conversation_id,
        {
            "type": "system_message",
            "conversation_id": conversation_id,
            "text": f"Model switched to: {arg}\nTakes effect on next message.",
        },
    )


async def _handle_slash_thinking(
    app: Any,
    manager: Any,
    connection: Any,
    conversation: Any,
    session: Any,
    arg: str,
) -> None:
    """Handle /thinking [level] — list or switch reasoning effort."""
    conversation_id = conversation.conversation_id
    session_id = session.session_id

    # Determine current model for effort level inference
    current_model = app.state.session_cache.get_model_override(session_id)
    if not current_model:
        usage = app.state.session_cache.get_context_usage(session_id)
        current_model = usage["model"] if usage else ""

    # Get supported effort levels
    try:
        if current_model:
            model_info = await app.state.providers.llm.get_model_info(current_model)
            available = model_info.reasoning_efforts if model_info.reasoning_efforts else []
        else:
            available = []
    except Exception:
        available = []

    if not available and current_model:
        available = _infer_reasoning_efforts(current_model)

    current_effort = app.state.session_cache.get_reasoning_effort_override(session_id)

    if not arg:
        # Show current effort and available levels
        lines = []
        if current_effort:
            lines.append(f"Current reasoning effort: {current_effort}")
        else:
            lines.append("Reasoning effort: default (not set)")
        if available:
            lines.append(f"Available levels: {', '.join(available)}")
        else:
            lines.append("No reasoning effort levels available for current model.")
        lines.append("Usage: /thinking <level>  (use 'off' to reset to default)")
        await manager.send_to_conversation(
            conversation_id,
            {
                "type": "system_message",
                "conversation_id": conversation_id,
                "text": "\n".join(lines),
            },
        )
        return

    # Reset
    if arg in ("off", "default", "reset", "none"):
        app.state.session_cache.set_reasoning_effort_override(session_id, None)
        await manager.send_to_conversation(
            conversation_id,
            {
                "type": "system_message",
                "conversation_id": conversation_id,
                "text": "Reasoning effort reset to default.",
            },
        )
        return

    # Validate
    if available and arg not in available:
        await manager.send_to_conversation(
            conversation_id,
            {
                "type": "system_message",
                "conversation_id": conversation_id,
                "text": f"Unsupported level: {arg}\nAvailable: {', '.join(available)}",
            },
        )
        return

    app.state.session_cache.set_reasoning_effort_override(session_id, arg)
    await manager.send_to_conversation(
        conversation_id,
        {
            "type": "system_message",
            "conversation_id": conversation_id,
            "text": f"Reasoning effort set to: {arg}\nTakes effect on next message.",
        },
    )


async def _handle_slash_lsp(
    app: Any,
    manager: Any,
    connection: Any,
    conversation: Any,
) -> None:
    """Handle /lsp — display LSP diagnostics subsystem status."""
    conversation_id = conversation.conversation_id
    lines: list[str] = []

    # Collect LSP managers from all active executor runtimes
    executor = app.state.providers.executor
    lsp_managers = executor.get_lsp_managers() if hasattr(executor, "get_lsp_managers") else []

    if not lsp_managers:
        lines.append("LSP Diagnostics")
        lines.append("  Status: no active LSP managers")
        await manager.send_to_conversation(
            conversation_id,
            {
                "type": "system_message",
                "conversation_id": conversation_id,
                "text": "\n".join(lines),
            },
        )
        return

    # Aggregate status from all managers (typically just one)
    for lsp_mgr in lsp_managers:
        status = lsp_mgr.status()
        cfg = status["config"]
        totals = status["totals"]

        lines.append("LSP Diagnostics")
        lines.append(f"  Status: {'enabled' if cfg['enabled'] else 'disabled'}")
        lines.append(f"  Auto-install: {'enabled' if cfg['auto_install'] else 'disabled'}")
        lines.append(f"  Timeout: {cfg['diagnostics_timeout_ms']}ms")
        lines.append(f"  Max servers: {cfg['max_concurrent_servers']}")

        active = status["active_servers"]
        if active:
            lines.append(
                f"\nActive servers ({totals['active_server_count']}/{cfg['max_concurrent_servers']}):"
            )
            for srv in active:
                pid_str = f"PID {srv['pid']}" if srv["pid"] else "no PID"
                alive_str = "" if srv["alive"] else " [dead]"
                lines.append(f"  {srv['server_name']} ({pid_str}{alive_str})")
                lines.append(f"    Root: {srv['root_path']}")
                lines.append(
                    f"    Files: {srv['file_count']}, "
                    f"diagnostics: {srv['error_count']} errors, "
                    f"{srv['warning_count']} warnings"
                )
                idle = srv["idle_seconds"]
                if idle >= 60:
                    lines.append(f"    Idle: {idle // 60}m {idle % 60}s")
                else:
                    lines.append(f"    Idle: {idle}s")
        else:
            lines.append("\nNo active servers")

        broken = status["broken_servers"]
        if broken:
            lines.append(f"\nBroken servers ({len(broken)}):")
            for brk in broken:
                retry = brk["retry_in_seconds"]
                retry_str = f"{retry // 60}m {retry % 60}s" if retry >= 60 else f"{retry}s"
                lines.append(f"  {brk['client_key']} (retry in {retry_str})")

        if status["spawning_count"] > 0:
            lines.append(f"\nSpawning: {status['spawning_count']} server(s)")

        lines.append(
            f"\nTotals: {totals['files_tracked']} files tracked, "
            f"{totals['total_errors']} errors, {totals['total_warnings']} warnings"
        )

        # Available servers (detected on system)
        try:
            available = await lsp_mgr.available_servers()
            if available:
                lines.append("\nAvailable servers:")
                for srv in available:
                    status_str = ""
                    if srv["active"]:
                        status_str = "active"
                    elif srv["available"]:
                        status_str = srv["path"]
                    elif srv["has_auto_install"]:
                        status_str = "not found (auto-install available)"
                    else:
                        status_str = "not found"
                    lines.append(f"  {srv['server_id']} ({srv['extensions']}) — {status_str}")
        except Exception:
            pass  # Best-effort

    await manager.send_to_conversation(
        conversation_id,
        {
            "type": "system_message",
            "conversation_id": conversation_id,
            "text": "\n".join(lines),
        },
    )


_HELP_TEXT = """\
Available commands:
  /help              Show this help message
  /lsp               Show LSP diagnostics status
  /model [name]      List available models or switch model
  /thinking [level]  Show or set reasoning effort (low/medium/high)
  /context           Show context window usage
  /info              Show session details and statistics
  /compact           Compact conversation history
  /new               Start a new conversation
  /approve [note]    Approve pending tool escalation
  /deny [note]       Deny pending tool escalation"""


async def _handle_slash_help(
    manager: Any,
    connection: Any,
    conversation: Any,
) -> None:
    """Handle /help — show available slash commands."""
    await manager.send_to_conversation(
        conversation.conversation_id,
        {
            "type": "system_message",
            "conversation_id": conversation.conversation_id,
            "text": _HELP_TEXT,
        },
    )


async def _load_conversation_runtime(
    app: Any,
    conversation_id: str,
    *,
    user_message: str | None = None,
) -> tuple[ConversationModel, SessionModel, Any] | None:
    """Load conversation, session, and agent for a WebSocket turn.

    When the root session is completed with ``completion_reason="compacted"``
    (deferred session creation after ``/compact``), this function creates a
    new root session carrying the compaction summary as initial context.

    When *user_message* is provided and the root session needs creation
    (either brand-new or deferred after compaction), the message is used
    to derive the initial intention via Intaris.
    """

    async with app.state.session_factory() as session:
        conversation_row = await get_conversation(session, conversation_id)
        if conversation_row is None:
            return None
        agent_row = await get_agent(session, conversation_row.agent_id)
        if agent_row is None:
            return None
        agent_model = AgentDefinition.model_validate(agent_to_response(agent_row).model_dump())
        conversation_model = _to_conversation_model(conversation_row)

        if conversation_row.active_session_id is None:
            # No session at all — create one
            intention = (
                conversation_row.title
                or agent_row.description
                or f"Conversation with {agent_row.name}"
            )
            try:
                root_session = await app.state.session_manager.create_root_session(
                    conversation_id=conversation_row.conversation_id,
                    user_email=conversation_row.user_email,
                    agent_id=conversation_row.agent_id,
                    intention=intention,
                )
            except Exception as exc:
                raise SessionCreationFailedError("Could not create a session") from exc
            conversation_model.active_session_id = root_session.session_id
            return conversation_model, root_session, agent_model

        session_row = await get_session_row(session, conversation_row.active_session_id)

    if session_row is None:
        return None

    session_model = _to_session_model(session_row)

    # --- Deferred session creation after /compact ---
    if (
        session_model.status == SessionStatus.COMPLETED
        and session_model.completion_reason == "compacted"
    ):
        # Acquire per-conversation lock to prevent duplicate creation
        # from multiple WebSocket tabs sending messages simultaneously.
        lock = _deferred_creation_locks.setdefault(conversation_id, asyncio.Lock())
        async with lock:
            # Double-check: re-read root session after acquiring lock —
            # another tab may have already created the new session.
            async with app.state.session_factory() as db_session_check:
                conv_row_check = await get_conversation(db_session_check, conversation_id)
            if (
                conv_row_check is not None
                and conv_row_check.active_session_id != session_model.session_id
            ):
                # Another tab already rotated — reload with new session
                async with app.state.session_factory() as db_session_reload:
                    new_row = await get_session_row(
                        db_session_reload, conv_row_check.active_session_id
                    )
                if new_row is not None:
                    conversation_model.active_session_id = conv_row_check.active_session_id
                    return conversation_model, _to_session_model(new_row), agent_model

            compaction_summary = await _read_compaction_summary_from_session(app, session_model)
            intention = user_message[:200] if user_message else "Continued conversation"
            if compaction_summary:
                intention = f"Continuation: {intention}"
            try:
                new_session = await app.state.session_manager.rotate_session(
                    conversation_id=conversation_model.conversation_id,
                    current_session=session_model,
                    intention=intention,
                    completion_reason="compacted",
                    compaction_summary=compaction_summary,
                )
                ROTATION_TOTAL.labels(trigger="deferred").inc()
            except Exception as exc:
                raise SessionCreationFailedError(
                    "Could not create session after compaction"
                ) from exc
            conversation_model.active_session_id = new_session.session_id

            # Pre-populate session cache with compaction summary.
            # compaction_seq=0 because the new session's Intaris event stream
            # starts fresh — there are no events to prune. The summary is
            # injected purely as context for the LLM, not as an Intaris event.
            if compaction_summary:
                await app.state.session_cache.refresh(new_session)
                await app.state.session_cache.apply_compaction(
                    new_session,
                    summary=compaction_summary,
                    compaction_seq=0,
                )

            logger.info(
                "ws: deferred session created after compaction",
                extra={
                    "extra_data": {
                        "conversation_id": conversation_id,
                        "old_session_id": session_model.session_id,
                        "new_session_id": new_session.session_id,
                    }
                },
            )
            return conversation_model, new_session, agent_model

        # Periodic cleanup: remove unheld locks when the dict grows too large
        if len(_deferred_creation_locks) > _MAX_DEFERRED_LOCKS:
            to_remove = [cid for cid, lk in _deferred_creation_locks.items() if not lk.locked()]
            for cid in to_remove:
                _deferred_creation_locks.pop(cid, None)

    logger.debug(
        "ws: conversation runtime loaded",
        extra={
            "extra_data": {
                "conversation_id": conversation_id,
                "session_id": session_model.session_id,
                "intaris_session_id": session_model.intaris_session_id,
                "agent_id": conversation_model.agent_id,
                "is_new_session": False,
            }
        },
    )
    return conversation_model, session_model, agent_model


async def _read_compaction_summary_from_session(app: Any, session: SessionModel) -> str | None:
    """Read the last compaction_summary event from a completed session's Intaris stream."""

    try:
        result = await app.state.providers.guardrails.read_events(
            session_id=session.intaris_session_id or session.session_id,
            after_seq=0,
            allow_missing_stream=True,
        )
        # Find the last compaction_summary event
        for event in reversed(result.events):
            if event.get("type") == "compaction_summary":
                return event.get("data", {}).get("summary")
    except Exception:
        logger.warning(
            "Failed to read compaction summary from previous session",
            extra={"extra_data": {"session_id": session.session_id}},
        )
    return None


async def _load_pending_task_prompts(app: Any, conversation_id: str) -> list[dict[str, Any]]:
    """Load pending notifications for a conversation on reconnect.

    Queries the unified notification service first (DB-persistent,
    survives restarts).  Falls back to PauseWaiter for legacy pauses.
    """
    payloads: list[dict[str, Any]] = []

    # Try the unified notification service first
    svc = getattr(app.state, "notification_service", None)
    if svc is not None:
        # We need a user_email for the query — get it from the conversation
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

    # Legacy fallback: PauseWaiter (in-memory only, lost on restart)
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


def _event_to_payload(event: Event, conversation_id: str) -> dict[str, Any] | None:
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
    if event.type == EventType.WORKFLOW_PROGRESS and event.data.get("event") in {
        "tool_call_started",
        "tool_call_completed",
    }:
        return {
            "type": "tool_call",
            "conversation_id": conversation_id,
            "session_id": event.data.get("session_id"),
            "call_id": event.data.get("call_id"),
            "tool_name": event.data.get("tool_name"),
            "status": "completed"
            if event.data.get("event") == "tool_call_completed"
            else "started",
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
    # Unified notification events — map to the appropriate legacy WS type
    # so existing UI clients continue to work without changes.
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
    if event.type == EventType.NOTIFICATION_RESOLVED:
        ntype = event.data.get("notification_type")
        if ntype == "escalation":
            return {
                "type": "escalation_resolved",
                "conversation_id": conversation_id,
                "call_id": event.data.get("notification_id"),
                "decision": event.data.get("decision"),
            }
    return None


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
