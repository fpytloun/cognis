"""WebSocket chat handler with first-message auth and runtime event fanout."""

from __future__ import annotations

import asyncio
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
from cognis.core.events import Event, EventType
from cognis.core.session import _to_conversation_model, _to_session_model
from cognis.logging import get_logger
from cognis.models.agent import AgentDefinition
from cognis.models.session import ConversationModel, SessionModel
from cognis.models.task import TaskDelivery
from cognis.runtime_context import current_agent_id, current_user_email
from cognis.store.models import Task
from cognis.store.queries import get_agent, get_conversation, get_session_row, get_task

logger = get_logger(__name__)

_NEW_SESSION_STREAM_GRACE = timedelta(seconds=30)


def _follow_up_turn_prompt(status: str | None) -> str:
    status_name = (status or "updated").lower()
    if status_name == "failed":
        return (
            "A background task failure was delivered to this conversation. "
            "Review the recent task_failed event and provide a concise user-facing follow-up if warranted."
        )
    if status_name == "cancelled":
        return (
            "A background task cancellation was delivered to this conversation. "
            "Review the recent task_cancelled event and provide a concise user-facing follow-up if warranted."
        )
    return (
        "A background task update was delivered to this conversation. "
        "Review the recent task_result event and provide a concise user-facing follow-up if warranted."
    )


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
        control = self._turn_controls.get(conversation_id)
        if control is None:
            return False
        control.set()
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

            async def on_tool_call(tool_name: str, call_id: str) -> None:
                await self.send_to_conversation(
                    conversation_id,
                    {
                        "type": "tool_call",
                        "conversation_id": conversation_id,
                        "session_id": session.session_id,
                        "call_id": call_id,
                        "tool_name": tool_name,
                        "status": "started",
                    },
                )

            await self.app.state.workflow_engine.run_direct_turn(
                conversation=conversation,
                session=session,
                agent=agent,
                user_message=content,
                system_initiated=system_initiated,
                on_progress=on_token,
                on_tool_call=on_tool_call,
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
            await self.send_to_conversation(
                conversation_id,
                {
                    "type": "message_complete",
                    "conversation_id": conversation_id,
                    "session_id": session.session_id,
                    "message_id": message_id,
                    "seq": last_seq,
                    "token_usage": None,
                    "queued_count": len(self._queued_messages.get(conversation_id, [])),
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
            queue = self._queued_messages.get(conversation_id)
            if queue:
                queued = queue.popleft()
                runtime = await _load_conversation_runtime(self.app, conversation_id)
                if runtime is not None:
                    next_conversation, next_session, next_agent = runtime
                    self._launch_turn(
                        conversation=next_conversation,
                        session=next_session,
                        agent=next_agent,
                        content=queued.content,
                    )

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
                        "task_id": item.get("data", {}).get("task_id"),
                        "result": item.get("data", {}).get("result_summary"),
                    }
                )
                replayed += 1
            elif event_type == "task_failed":
                await connection.send_json(
                    {
                        "type": "workflow_failed",
                        "task_id": item.get("data", {}).get("task_id"),
                        "reason": item.get("data", {}).get("result_summary"),
                    }
                )
                replayed += 1
            elif event_type == "task_cancelled":
                await connection.send_json(
                    {
                        "type": "workflow_cancelled",
                        "task_id": item.get("data", {}).get("task_id"),
                        "reason": item.get("data", {}).get("result_summary") or "cancelled",
                    }
                )
                replayed += 1
        pending_pauses = await _load_pending_task_prompts(self.app, conversation_id)
        for payload in pending_pauses:
            await connection.send_json(payload)
        if session.session_id in set(getattr(self.app.state, "recovered_session_ids", [])):
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
        if not self._by_conversation.get(conversation_id):
            return
        if conversation_id in self._active_turns and not self._active_turns[conversation_id].done():
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
            content=_follow_up_turn_prompt(
                event.data.get("status") if isinstance(event.data.get("status"), str) else None
            ),
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
                runtime = await _load_conversation_runtime(websocket.app, conversation_id)
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
                if not cancelled:
                    await manager.send_error(
                        connection,
                        code="not_found",
                        message="No active turn to cancel",
                        recoverable=True,
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
                current_user_email.set(connection.user_email)
                pending_escalations = (
                    await websocket.app.state.providers.guardrails.list_pending_escalations()
                )
                if not any(item.call_id == call_id for item in pending_escalations):
                    await manager.send_error(
                        connection,
                        code="not_found",
                        message="Escalation not found",
                        recoverable=True,
                    )
                    continue
                current_user_email.set(connection.user_email)
                await websocket.app.state.providers.guardrails.submit_decision(
                    call_id, decision, note if isinstance(note, str) else None
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
                pause = websocket.app.state.pause_waiter.find_pending(
                    task_id=task_id,
                    step_name=step_name if isinstance(step_name, str) else None,
                    pause_type="gate",
                )
                if pause is None:
                    await manager.send_error(
                        connection, code="not_found", message="No pending gate", recoverable=True
                    )
                    continue
                if isinstance(feedback, str) and feedback:
                    await _persist_task_feedback(websocket.app, task_id, feedback)
                ok = websocket.app.state.pause_waiter.resolve(
                    pause.pause_id,
                    PauseResolution(
                        decision=action,
                        data={"feedback": feedback if isinstance(feedback, str) else ""},
                    ),
                )
                if not ok:
                    await manager.send_error(
                        connection,
                        code="conflict",
                        message="Gate already resolved",
                        recoverable=True,
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
                ok = websocket.app.state.pause_waiter.resolve(
                    pause.pause_id,
                    PauseResolution(decision="continue", data={"response": str(response)}),
                )
                if not ok:
                    await manager.send_error(
                        connection,
                        code="conflict",
                        message="Question already resolved",
                        recoverable=True,
                    )
                    continue
                if not websocket.app.state.task_queue.has_active_run(task_id):
                    await _store_recovered_step_input_response(
                        websocket.app,
                        task_id,
                        str(response),
                    )
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


async def _load_conversation_runtime(
    app: Any,
    conversation_id: str,
) -> tuple[ConversationModel, SessionModel, Any] | None:
    async with app.state.session_factory() as session:
        conversation_row = await get_conversation(session, conversation_id)
        if conversation_row is None:
            return None
        agent_row = await get_agent(session, conversation_row.agent_id)
        if agent_row is None:
            return None
        agent_model = AgentDefinition.model_validate(agent_to_response(agent_row).model_dump())
        conversation_model = _to_conversation_model(conversation_row)
        if conversation_row.root_session_id is None:
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
            conversation_model.root_session_id = root_session.session_id
            return conversation_model, root_session, agent_model
        session_row = await get_session_row(session, conversation_row.root_session_id)
    if session_row is None:
        return None
    session_model = _to_session_model(session_row)
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


async def _load_pending_task_prompts(app: Any, conversation_id: str) -> list[dict[str, Any]]:
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

    payloads: list[dict[str, Any]] = []
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
    if event.type == EventType.SESSION_RECOVERED:
        return {
            "type": "session_recovered",
            "conversation_id": conversation_id,
            "session_id": event.data.get("session_id"),
            "reason": event.data.get("reason") or "controller_restart",
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
