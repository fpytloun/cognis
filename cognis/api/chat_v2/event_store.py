"""Session event-store protocol for Chat v2 projection.

The first implementation will adapt Intaris, but Chat v2 consumers depend on
this protocol rather than Intaris-specific response shapes.
"""

from __future__ import annotations

import contextlib
from datetime import datetime
from typing import Any, Literal, Protocol

from pydantic import Field

from cognis.api.chat_v2.schemas import StrictModel
from cognis.providers.guardrails.events import EventStoreAuthority
from cognis.runtime_context import scoped_runtime_context


class RawSessionEvent(StrictModel):
    """Raw event from a pluggable session event-store backend."""

    store_id: str
    session_id: str
    seq: int = Field(ge=0)
    type: str
    data: dict[str, Any] = Field(default_factory=dict)
    event_id: str | None = None
    timestamp: datetime | None = None
    lane: str | None = None
    prompt_visibility: str | None = None


class SessionEventPage(StrictModel):
    """Page of raw events for one session stream."""

    store_id: str
    session_id: str
    events: list[RawSessionEvent] = Field(default_factory=list)
    first_seq: int | None = Field(default=None, ge=0)
    last_seq: int | None = Field(default=None, ge=0)
    has_more_before: bool = False
    has_more_after: bool = False
    verified_empty: bool = False


class SessionWatermark(StrictModel):
    """High-watermark metadata for one session stream."""

    store_id: str
    session_id: str
    last_seq: int = Field(ge=0)


class SessionEventStore(Protocol):
    """Read-only event-store protocol used by Chat v2 projection."""

    store_id: str

    async def read_session_events(
        self,
        *,
        session_id: str,
        after_seq: int | None = None,
        before_seq: int | None = None,
        limit: int = 500,
        direction: Literal["forward", "backward"] = "forward",
    ) -> SessionEventPage:
        """Read a page of events from one session stream."""

    async def read_session_high_watermark(
        self,
        *,
        session_id: str,
    ) -> SessionWatermark:
        """Return the latest known sequence for one session stream."""


class IntarisSessionEventStore:
    """Chat v2 event-store adapter over the current Guardrails/Intaris provider."""

    store_id = "intaris"

    def __init__(self, guardrails: Any, *, authority: EventStoreAuthority | None = None) -> None:
        self._guardrails = guardrails
        self._authority = authority

    def bind(self, authority: EventStoreAuthority) -> IntarisSessionEventStore:
        return IntarisSessionEventStore(self._guardrails, authority=authority)

    async def read_session_events(
        self,
        *,
        session_id: str,
        after_seq: int | None = None,
        before_seq: int | None = None,
        limit: int = 500,
        direction: Literal["forward", "backward"] = "forward",
    ) -> SessionEventPage:
        """Read a page of session events while preserving backend ordering.

        Intaris supports forward reads via ``after_seq``, tail reads via
        ``last_n``, and bounded reverse reads via ``before_seq``.
        """

        if limit < 1:
            raise ValueError("limit must be >= 1")
        if before_seq is not None and before_seq < 0:
            raise ValueError("before_seq must be >= 0")
        if after_seq is not None and after_seq < 0:
            raise ValueError("after_seq must be >= 0")

        if direction == "backward":
            return await self._read_backward(
                session_id=session_id, before_seq=before_seq, limit=limit
            )

        with self._authority_context():
            result = await self._guardrails.read_events(
                session_id=session_id,
                after_seq=after_seq or 0,
                limit=limit,
                allow_missing_stream=True,
            )
        events = [_raw_event(self.store_id, session_id, item) for item in result.events]
        return SessionEventPage(
            store_id=self.store_id,
            session_id=session_id,
            events=events,
            first_seq=events[0].seq if events else None,
            last_seq=result.last_seq,
            has_more_after=bool(result.has_more),
            verified_empty=not events,
        )

    async def read_session_high_watermark(
        self,
        *,
        session_id: str,
    ) -> SessionWatermark:
        with self._authority_context():
            last_seq = await self._guardrails.get_last_seq(
                session_id,
                allow_missing_stream=True,
            )
        return SessionWatermark(store_id=self.store_id, session_id=session_id, last_seq=last_seq)

    async def _read_backward(
        self,
        *,
        session_id: str,
        before_seq: int | None,
        limit: int,
    ) -> SessionEventPage:
        if before_seq is None:
            with self._authority_context():
                result = await self._guardrails.read_events(
                    session_id=session_id,
                    last_n=limit,
                    allow_missing_stream=True,
                )
            events = [_raw_event(self.store_id, session_id, item) for item in result.events]
            return SessionEventPage(
                store_id=self.store_id,
                session_id=session_id,
                events=events,
                first_seq=events[0].seq if events else None,
                last_seq=events[-1].seq if events else result.last_seq,
                has_more_before=len(events) == limit and bool(events and events[0].seq > 1),
                has_more_after=False,
                verified_empty=not events,
            )

        with self._authority_context():
            result = await self._guardrails.read_events(
                session_id=session_id,
                before_seq=before_seq,
                limit=limit,
                allow_missing_stream=True,
            )
        events = [_raw_event(self.store_id, session_id, item) for item in result.events]
        return SessionEventPage(
            store_id=self.store_id,
            session_id=session_id,
            events=events,
            first_seq=events[0].seq if events else None,
            last_seq=events[-1].seq if events else None,
            has_more_before=bool(result.has_more),
            has_more_after=False,
            verified_empty=not events,
        )

    def _authority_context(self):
        authority = self._authority
        if authority is None:
            return contextlib.nullcontext()
        return scoped_runtime_context(
            user_email=authority.user_email,
            agent_id=authority.agent_id,
            agent_owner_email=authority.agent_owner_email,
        )


def _raw_event(store_id: str, session_id: str, item: dict[str, Any]) -> RawSessionEvent:
    data = item.get("data")
    if not isinstance(data, dict):
        data = {}
    seq = int(item.get("seq") or data.get("seq") or 0)
    timestamp = (
        item.get("timestamp")
        or item.get("created_at")
        or item.get("received_at")
        or item.get("ts")
        or data.get("timestamp")
        or data.get("created_at")
        or data.get("createdAt")
        or data.get("received_at")
        or data.get("receivedAt")
        or data.get("updated_at")
        or data.get("updatedAt")
        or data.get("ts")
    )
    parsed_timestamp = _parse_timestamp(timestamp)
    return RawSessionEvent(
        store_id=store_id,
        session_id=session_id,
        seq=seq,
        type=str(item.get("type") or data.get("type") or "unknown"),
        data=data,
        event_id=_str_or_none(item.get("event_id") or item.get("id") or data.get("event_id")),
        timestamp=parsed_timestamp,
        lane=_str_or_none(item.get("lane") or data.get("lane")),
        prompt_visibility=_str_or_none(
            item.get("prompt_visibility") or data.get("prompt_visibility")
        ),
    )


def _parse_timestamp(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _str_or_none(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None
