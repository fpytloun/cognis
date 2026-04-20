"""Internal event bus for Cognis controller.

Provides async pub/sub for controller-internal events. For MVP, only
after-hooks (observe pattern) are supported. Before-hooks (block/modify)
are deferred to Phase 2.

Events are controller-internal and do NOT leave the process. Intaris
events (session recording) are a separate concern.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections import defaultdict
from collections.abc import Callable, Coroutine
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from prometheus_client import Counter
from pydantic import BaseModel

from cognis.logging import get_logger

logger = get_logger(__name__)

_EVENT_SUBSCRIBER_ERRORS_TOTAL = Counter(
    "cognis_event_subscriber_errors_total",
    "Errors raised by EventBus subscribers.",
    labelnames=("subscriber_type",),
)
_EVENT_SUBSCRIBERS_AUTO_REMOVED_TOTAL = Counter(
    "cognis_event_subscribers_auto_removed_total",
    "EventBus subscribers removed after repeated failures.",
    labelnames=("subscriber_type", "reason"),
)
_MAX_SUBSCRIBER_ERRORS = 5

# Type alias for async event handlers.
EventHandler = Callable[["Event"], Coroutine[Any, Any, None]]


class EventType(StrEnum):
    """Controller-internal event types."""

    # Session lifecycle
    SESSION_COMPLETED = "session_completed"
    SESSION_FAILED = "session_failed"
    SESSION_RECOVERED = "session_recovered"
    SESSION_COMPACTED = "session_compacted"

    # Step lifecycle
    STEP_STARTED = "step_started"
    STEP_COMPLETED = "step_completed"
    STEP_FAILED = "step_failed"
    STEP_PAUSED = "step_paused"

    # Task lifecycle
    TASK_CREATED = "task_created"
    TASK_QUEUED = "task_queued"
    TASK_STARTED = "task_started"
    TASK_COMPLETED = "task_completed"
    TASK_FAILED = "task_failed"
    TASK_CANCELLED = "task_cancelled"
    TASK_PAUSED = "task_paused"
    FOLLOW_UP_TURN_REQUESTED = "follow_up_turn_requested"

    # Workflow
    WORKFLOW_GATE = "workflow_gate"
    WORKFLOW_PROGRESS = "workflow_progress"
    WORKFLOW_COMPOSED = "workflow_composed"

    # Escalation
    ESCALATION_CREATED = "escalation_created"
    ESCALATION_RESOLVED = "escalation_resolved"

    # Delegation (sub-session lifecycle)
    DELEGATION_STARTED = "delegation_started"
    DELEGATION_COMPLETED = "delegation_completed"
    DELEGATION_FAILED = "delegation_failed"

    # Turn lifecycle (TurnScheduler)
    TURN_STARTED = "turn_started"
    TURN_COMPLETED = "turn_completed"
    TURN_ERROR = "turn_error"
    USER_MESSAGE = "user_message"

    # Agent lifecycle
    AGENT_PROFILE_UPDATED = "agent_profile_updated"

    # Schedule lifecycle
    SCHEDULE_FIRED = "schedule_fired"
    SCHEDULE_ERROR = "schedule_error"
    SCHEDULE_DISABLED = "schedule_disabled"

    # Unified notifications
    NOTIFICATION_CREATED = "notification_created"
    NOTIFICATION_RESOLVED = "notification_resolved"

    # Session-scoped system notices
    SYSTEM_NOTICE = "system_notice"


class Event(BaseModel):
    """A controller-internal event."""

    type: EventType
    data: dict[str, Any] = {}
    timestamp: datetime | None = None

    def model_post_init(self, _context: Any) -> None:
        """Set timestamp if not provided."""
        if self.timestamp is None:
            self.timestamp = datetime.now(UTC)


class EventBus:
    """Async pub/sub event bus for controller-internal events.

    Handlers are async callables that receive an Event. Handler errors
    are logged but never propagate — the event bus must not break callers.
    """

    def __init__(self) -> None:
        self._handlers: dict[EventType, list[EventHandler]] = defaultdict(list)
        self._global_handlers: list[EventHandler] = []
        self._handler_error_counts: dict[int, int] = {}

    def subscribe(self, event_type: EventType, handler: EventHandler) -> None:
        """Register a handler for a specific event type."""
        self._handlers[event_type].append(handler)

    def subscribe_all(self, handler: EventHandler) -> None:
        """Register a handler for all event types."""
        self._global_handlers.append(handler)

    def unsubscribe(self, event_type: EventType, handler: EventHandler) -> None:
        """Remove a handler for a specific event type."""
        handlers = self._handlers.get(event_type)
        if handlers:
            with contextlib.suppress(ValueError):
                handlers.remove(handler)
        self._handler_error_counts.pop(id(handler), None)

    def unsubscribe_all(self, handler: EventHandler) -> None:
        """Remove a handler from all subscriptions."""
        for handlers in self._handlers.values():
            with contextlib.suppress(ValueError):
                handlers.remove(handler)
        with contextlib.suppress(ValueError):
            self._global_handlers.remove(handler)
        self._handler_error_counts.pop(id(handler), None)

    async def publish(self, event: Event) -> None:
        """Publish an event to all matching handlers.

        Handlers are called concurrently. Individual handler errors are
        logged but never propagate.
        """
        handlers = list(self._handlers.get(event.type, []))
        handlers.extend(self._global_handlers)
        if not handlers:
            return

        results = await asyncio.gather(
            *(_safe_call(handler, event) for handler in handlers),
            return_exceptions=True,
        )
        for i, result in enumerate(results):
            handler = handlers[i]
            handler_id = id(handler)
            subscriber_type = _subscriber_type(handler)
            if result is True:
                self._handler_error_counts.pop(handler_id, None)
                continue
            if isinstance(result, Exception) or result is False:
                failures = self._handler_error_counts.get(handler_id, 0) + 1
                self._handler_error_counts[handler_id] = failures
                _EVENT_SUBSCRIBER_ERRORS_TOTAL.labels(subscriber_type=subscriber_type).inc()
                logger.warning(
                    "Event handler error",
                    extra={
                        "extra_data": {
                            "event_type": event.type,
                            "handler_index": i,
                            "consecutive_errors": failures,
                            "subscriber_type": subscriber_type,
                        }
                    },
                )
                if failures >= _MAX_SUBSCRIBER_ERRORS:
                    self.unsubscribe_all(handler)
                    _EVENT_SUBSCRIBERS_AUTO_REMOVED_TOTAL.labels(
                        subscriber_type=subscriber_type,
                        reason="consecutive_errors",
                    ).inc()
                    logger.warning(
                        "Event handler auto-removed after repeated failures",
                        extra={
                            "extra_data": {
                                "event_type": event.type,
                                "subscriber_type": subscriber_type,
                                "consecutive_errors": failures,
                            }
                        },
                    )

    def handler_count(self, event_type: EventType | None = None) -> int:
        """Return the number of registered handlers."""
        if event_type is None:
            total = sum(len(h) for h in self._handlers.values())
            return total + len(self._global_handlers)
        return len(self._handlers.get(event_type, [])) + len(self._global_handlers)


def _subscriber_type(handler: EventHandler) -> str:
    """Best-effort label for metrics and logs."""
    subscriber_type = getattr(handler, "__subscriber_type__", None)
    if isinstance(subscriber_type, str) and subscriber_type.strip():
        return subscriber_type.strip().lower()
    bound_self = getattr(handler, "__self__", None)
    if bound_self is not None:
        return type(bound_self).__name__.lower()
    return getattr(handler, "__name__", "function").lower()


async def _safe_call(handler: EventHandler, event: Event) -> bool:
    """Call a handler, catching all exceptions."""
    try:
        await handler(event)
        return True
    except Exception:
        logger.exception(
            "Event handler raised exception",
            extra={"extra_data": {"event_type": event.type}},
        )
        return False
