"""Core-layer follow-up turn handler.

Handles FOLLOW_UP_TURN_REQUESTED events from the EventBus to run
system-initiated agent turns (task completion, delegation completion).

This handler has **no dependency on WebSocket clients** — it runs turns
directly through the workflow engine and persists results to Intaris.
The WebSocket layer handles real-time streaming independently.
"""

from __future__ import annotations

import asyncio
from collections import defaultdict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from cognis.core.events import Event, EventBus, EventType
from cognis.logging import get_logger
from cognis.models.agent import AgentDefinition
from cognis.models.session import ConversationModel, SessionModel, SessionStatus

logger = get_logger(__name__)


def _build_follow_up_prompt(
    status: str | None,
    *,
    task_id: str | None = None,
    task_title: str | None = None,
    result_summary: str | None = None,
) -> str:
    """Build a system prompt for the follow-up turn after a task/delegation completes."""
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
                "If you need the full detailed output, use the get_task_output "
                f'tool with task_id="{task_id}".'
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


@dataclass(slots=True)
class _QueuedFollowUp:
    """A queued follow-up turn waiting for an active turn to finish."""

    prompt: str
    conversation_id: str
    received_at: datetime = field(default_factory=lambda: datetime.now(UTC))


class FollowUpTurnHandler:
    """Core-layer handler for FOLLOW_UP_TURN_REQUESTED events.

    Runs system-initiated agent turns through the workflow engine.
    No WebSocket dependency — results are persisted to Intaris and
    the conversation's ``last_message_at`` is updated for unread tracking.

    Streaming callbacks are optional — the WS manager can register a
    callback to forward tokens to connected clients.
    """

    def __init__(
        self,
        *,
        session_factory: Any,
        workflow_engine: Any,
        session_manager: Any,
        event_bus: EventBus,
        on_turn_message: Callable[[str, dict[str, Any]], Awaitable[None]] | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._workflow_engine = workflow_engine
        self._session_manager = session_manager
        self._event_bus = event_bus
        self._on_turn_message = on_turn_message

        # Per-conversation turn serialization
        self._active_turns: dict[str, asyncio.Task[None]] = {}
        self._queued: dict[str, list[_QueuedFollowUp]] = defaultdict(list)

        # Register on EventBus
        event_bus.subscribe(EventType.FOLLOW_UP_TURN_REQUESTED, self._handle_event)
        logger.info("follow_up: handler registered on EventBus")

    async def _handle_event(self, event: Event) -> None:
        """Handle a FOLLOW_UP_TURN_REQUESTED event."""
        conversation_id = event.data.get("conversation_id")
        if not isinstance(conversation_id, str):
            logger.warning("follow_up: event missing conversation_id, dropping")
            return

        prompt = _build_follow_up_prompt(
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

        # Serialize turns per conversation — queue if one is already running
        active = self._active_turns.get(conversation_id)
        if active is not None and not active.done():
            self._queued[conversation_id].append(
                _QueuedFollowUp(prompt=prompt, conversation_id=conversation_id)
            )
            logger.info(
                "follow_up: queued behind active turn",
                extra={
                    "extra_data": {
                        "conversation_id": conversation_id,
                        "queue_depth": len(self._queued[conversation_id]),
                    }
                },
            )
            return

        self._launch(conversation_id, prompt)

    def _launch(self, conversation_id: str, prompt: str) -> None:
        """Launch a follow-up turn as a background task."""
        self._active_turns[conversation_id] = asyncio.create_task(
            self._run_follow_up(conversation_id, prompt)
        )

    async def _run_follow_up(self, conversation_id: str, prompt: str) -> None:
        """Execute a single follow-up turn and drain the queue."""
        try:
            runtime = await self._load_runtime(conversation_id)
            if runtime is None:
                logger.warning(
                    "follow_up: could not load conversation runtime, dropping",
                    extra={"extra_data": {"conversation_id": conversation_id}},
                )
                return

            conversation, session, agent = runtime
            if conversation.status != "active":
                logger.info(
                    "follow_up: conversation not active, skipping",
                    extra={
                        "extra_data": {
                            "conversation_id": conversation_id,
                            "status": conversation.status,
                        }
                    },
                )
                return

            logger.info(
                "follow_up: running turn",
                extra={
                    "extra_data": {
                        "conversation_id": conversation_id,
                        "session_id": session.session_id,
                        "agent_id": agent.agent_id,
                    }
                },
            )

            # Optional streaming callback for WS clients
            on_progress = None
            if self._on_turn_message is not None:
                _cid = conversation_id

                async def on_progress(token: str) -> None:
                    if self._on_turn_message is not None:
                        await self._on_turn_message(
                            _cid,
                            {
                                "type": "chunk",
                                "conversation_id": _cid,
                                "session_id": session.session_id,
                                "content": token,
                            },
                        )

            await self._workflow_engine.run_direct_turn(
                conversation=conversation,
                session=session,
                agent=agent,
                user_message=prompt,
                system_initiated=True,
                on_progress=on_progress,
            )

            # Update last_message_at for unread tracking
            await self._touch_conversation(conversation_id)

            # Notify WS clients that the turn is complete
            if self._on_turn_message is not None:
                await self._on_turn_message(
                    conversation_id,
                    {
                        "type": "message_complete",
                        "conversation_id": conversation_id,
                        "session_id": session.session_id,
                        "message_id": f"followup_{conversation_id[:12]}",
                        "seq": 0,
                        "token_usage": None,
                        "context_usage": None,
                        "queued_count": 0,
                    },
                )

            logger.info(
                "follow_up: turn completed",
                extra={"extra_data": {"conversation_id": conversation_id}},
            )

        except Exception:
            logger.exception(
                "follow_up: turn failed",
                extra={"extra_data": {"conversation_id": conversation_id}},
            )
        finally:
            self._active_turns.pop(conversation_id, None)
            # Drain queue — process next queued follow-up for this conversation
            queue = self._queued.get(conversation_id)
            if queue:
                next_item = queue.pop(0)
                self._launch(next_item.conversation_id, next_item.prompt)

    async def _load_runtime(
        self, conversation_id: str
    ) -> tuple[ConversationModel, SessionModel, AgentDefinition] | None:
        """Load conversation, session, and agent for a follow-up turn."""
        from cognis.api.serializers import agent_to_response
        from cognis.store.queries import get_agent, get_conversation, get_session_row

        async with self._session_factory() as db_session:
            conversation_row = await get_conversation(db_session, conversation_id)
            if conversation_row is None:
                return None
            agent_row = await get_agent(db_session, conversation_row.agent_id)
            if agent_row is None:
                return None

            agent_model = AgentDefinition.model_validate(agent_to_response(agent_row).model_dump())
            conversation_model = ConversationModel(
                conversation_id=conversation_row.conversation_id,
                user_email=conversation_row.user_email,
                agent_id=conversation_row.agent_id,
                title=conversation_row.title,
                context=_build_context(conversation_row),
                status=conversation_row.status,
                active_session_id=conversation_row.active_session_id,
            )

            if conversation_row.active_session_id is None:
                # Create a root session for the follow-up turn
                intention = (
                    conversation_row.title
                    or agent_row.description
                    or f"Conversation with {agent_row.name}"
                )
                try:
                    root_session = await self._session_manager.create_root_session(
                        conversation_id=conversation_row.conversation_id,
                        user_email=conversation_row.user_email,
                        agent_id=conversation_row.agent_id,
                        intention=intention,
                    )
                except Exception:
                    logger.exception(
                        "follow_up: failed to create root session",
                        extra={"extra_data": {"conversation_id": conversation_id}},
                    )
                    return None
                conversation_model.active_session_id = root_session.session_id
                return conversation_model, root_session, agent_model

            session_row = await get_session_row(db_session, conversation_row.active_session_id)

        if session_row is None:
            return None

        session_model = SessionModel(
            session_id=session_row.session_id,
            conversation_id=session_row.conversation_id,
            parent_session_id=getattr(session_row, "parent_session_id", None),
            previous_session_id=getattr(session_row, "previous_session_id", None),
            user_email=session_row.user_email,
            agent_id=session_row.agent_id,
            delegation_mode=getattr(session_row, "delegation_mode", None),
            delegation_task=getattr(session_row, "delegation_task", None),
            status=SessionStatus(session_row.status),
            completion_reason=getattr(session_row, "completion_reason", None),
            intaris_session_id=getattr(session_row, "intaris_session_id", None),
            mnemory_session_id=getattr(session_row, "mnemory_session_id", None),
        )

        # Handle compacted sessions — create a new session
        if (
            session_model.status == SessionStatus.COMPLETED
            and session_model.completion_reason == "compacted"
        ):
            try:
                new_session = await self._session_manager.create_root_session(
                    conversation_id=conversation_id,
                    user_email=session_model.user_email,
                    agent_id=session_model.agent_id,
                    intention=conversation_model.title or "Follow-up turn",
                    previous_session_id=session_model.session_id,
                )
                conversation_model.active_session_id = new_session.session_id
                return conversation_model, new_session, agent_model
            except Exception:
                logger.exception(
                    "follow_up: failed to create session after compaction",
                    extra={"extra_data": {"conversation_id": conversation_id}},
                )
                return None

        return conversation_model, session_model, agent_model

    async def _touch_conversation(self, conversation_id: str) -> None:
        """Update last_message_at on the conversation for unread tracking."""
        try:
            from cognis.store.queries import get_conversation

            async with self._session_factory() as db_session:
                row = await get_conversation(db_session, conversation_id)
                if row is not None:
                    row.last_message_at = datetime.now(UTC)
                    row.updated_at = row.last_message_at
                    await db_session.commit()
        except Exception:
            logger.warning(
                "follow_up: failed to update last_message_at",
                extra={"extra_data": {"conversation_id": conversation_id}},
            )


def _build_context(row: Any) -> Any:
    """Build a ConversationContext from a DB row."""
    from cognis.models.session import ConversationContext

    return ConversationContext(
        type=row.context_type if hasattr(row, "context_type") else "web",
        ref=row.context_ref if hasattr(row, "context_ref") else None,
        platform_data=row.context_data or {},
        memory_labels=row.memory_labels or {},
    )
