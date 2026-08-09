"""Process-local notifications for canonical Intaris event appends."""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

MAX_EVENT_NOTIFICATION_ID_LENGTH = 512


def _normalized_identifier(value: str, *, name: str, email: bool = False) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{name} must be a string")
    normalized = value.strip()
    if email:
        normalized = normalized.casefold()
    if not normalized:
        raise ValueError(f"{name} must not be empty")
    if len(normalized) > MAX_EVENT_NOTIFICATION_ID_LENGTH:
        raise ValueError(f"{name} exceeds maximum length")
    return normalized


@dataclass(frozen=True, slots=True)
class EventStoreAuthority:
    """Complete normalized partition authority for an Intaris event stream."""

    user_email: str
    agent_id: str
    agent_owner_email: str

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "user_email",
            _normalized_identifier(self.user_email, name="user_email", email=True),
        )
        object.__setattr__(
            self,
            "agent_id",
            _normalized_identifier(self.agent_id, name="agent_id"),
        )
        object.__setattr__(
            self,
            "agent_owner_email",
            _normalized_identifier(
                self.agent_owner_email,
                name="agent_owner_email",
                email=True,
            ),
        )


@dataclass(frozen=True, slots=True)
class EventAppendNotification:
    """Immutable local notification emitted after a durable canonical append."""

    authority: EventStoreAuthority
    session_id: str
    first_seq: int
    last_seq: int
    event_count: int
    events: tuple[Any, ...] = ()
    payload_bytes: int = 0

    def __post_init__(self) -> None:
        if not isinstance(self.authority, EventStoreAuthority):
            raise ValueError("authority must be an EventStoreAuthority")
        object.__setattr__(
            self,
            "session_id",
            _normalized_identifier(self.session_id, name="session_id"),
        )
        for name in ("first_seq", "last_seq", "event_count"):
            value = getattr(self, name)
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise ValueError(f"{name} must be a nonnegative integer")
        if self.event_count == 0:
            if self.first_seq != 0 or self.last_seq != 0:
                raise ValueError("zero-event notification must use a zero sequence range")
        elif (
            self.first_seq <= 0
            or self.last_seq < self.first_seq
            or self.last_seq - self.first_seq + 1 != self.event_count
        ):
            raise ValueError("sequence range must match event_count")
        if self.events and len(self.events) != self.event_count:
            raise ValueError("events must match event_count")
        if not isinstance(self.payload_bytes, int) or isinstance(self.payload_bytes, bool):
            raise ValueError("payload_bytes must be an integer")
        if self.payload_bytes < 0:
            raise ValueError("payload_bytes must be nonnegative")
        if self.events:
            payload_bytes = 0
            for event in self.events:
                value = event.model_dump(mode="json") if hasattr(event, "model_dump") else event
                payload_bytes += len(
                    json.dumps(
                        value,
                        ensure_ascii=False,
                        separators=(",", ":"),
                    ).encode("utf-8")
                )
            if self.payload_bytes not in {0, payload_bytes}:
                raise ValueError("payload_bytes must match the retained events")
            object.__setattr__(self, "payload_bytes", payload_bytes)


EventAppendListener = Callable[[EventAppendNotification], Awaitable[None]]


__all__ = [
    "EventAppendListener",
    "EventAppendNotification",
    "EventStoreAuthority",
    "MAX_EVENT_NOTIFICATION_ID_LENGTH",
]
