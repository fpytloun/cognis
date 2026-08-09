from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import httpx
import pytest
from pydantic import ValidationError

from cognis.api.chat_v2.event_store import (
    IntarisSessionEventStore,
    RawSessionEvent,
    SessionEventPage,
    _raw_event,
)
from cognis.providers.guardrails.intaris import IntarisProvider
from cognis.runtime_context import scoped_runtime_context


@dataclass
class FakeReadResult:
    events: list[dict[str, Any]]
    last_seq: int
    has_more: bool
    missing_stream_fallback_used: bool = False


class PagedGuardrails:
    def __init__(self, events: list[dict[str, Any]], *, page_size: int) -> None:
        self.events = events
        self.page_size = page_size
        self.calls: list[dict[str, int | None]] = []

    async def read_events(
        self,
        *,
        session_id: str,
        after_seq: int = 0,
        limit: int = 0,
        types: list[str] | None = None,
        last_n: int | None = None,
        before_seq: int | None = None,
        seqs: list[int] | None = None,
        allow_missing_stream: bool = False,
    ) -> FakeReadResult:
        del session_id, types, seqs, allow_missing_stream
        self.calls.append({"after_seq": after_seq, "before_seq": before_seq, "limit": limit})
        if last_n is not None:
            page = self.events[-last_n:]
            return FakeReadResult(events=page, last_seq=self.events[-1]["seq"], has_more=False)
        if before_seq is not None:
            candidates = [event for event in self.events if int(event["seq"]) < before_seq]
            page = candidates[-limit:]
            return FakeReadResult(
                events=page,
                last_seq=self.events[-1]["seq"],
                has_more=len(candidates) > len(page),
            )
        effective_limit = min(limit or self.page_size, self.page_size)
        remaining = [event for event in self.events if int(event["seq"]) > after_seq]
        page = remaining[:effective_limit]
        return FakeReadResult(
            events=page,
            last_seq=self.events[-1]["seq"] if self.events else 0,
            has_more=len(remaining) > len(page),
        )

    async def get_last_seq(self, session_id: str, *, allow_missing_stream: bool = False) -> int:
        del session_id
        assert allow_missing_stream is True
        return int(self.events[-1]["seq"]) if self.events else 0


class AuthProvider:
    def sign_service_jwt(
        self,
        subject: str,
        agent_id: str,
        audience: list[str],
        *,
        agent_owner_email: str | None = None,
    ) -> str:
        del subject, agent_id, audience, agent_owner_email
        return "token"


def test_raw_session_event_preserves_lane_and_visibility_metadata() -> None:
    event = RawSessionEvent(
        store_id="intaris",
        session_id="sess-1",
        seq=7,
        type="assistant_message",
        lane="side",
        prompt_visibility="hidden",
        data={"content": "internal"},
    )

    assert event.lane == "side"
    assert event.prompt_visibility == "hidden"
    assert event.data == {"content": "internal"}


def test_raw_session_event_rejects_negative_sequence() -> None:
    with pytest.raises(ValidationError):
        RawSessionEvent(
            store_id="intaris",
            session_id="sess-1",
            seq=-1,
            type="user_message",
        )


def test_session_event_page_defaults_to_empty_events() -> None:
    page = SessionEventPage(store_id="intaris", session_id="sess-1")

    assert page.events == []
    assert page.has_more_before is False
    assert page.has_more_after is False


def test_raw_event_uses_data_created_at_when_top_level_timestamp_missing() -> None:
    event = _raw_event(
        "intaris",
        "sess-1",
        {
            "seq": 9,
            "type": "assistant_message",
            "data": {
                "content": "old answer",
                "created_at": "2026-01-01T12:34:56Z",
            },
        },
    )

    assert event.timestamp is not None
    assert event.timestamp.isoformat() == "2026-01-01T12:34:56+00:00"


def test_raw_event_uses_top_level_ts_timestamp() -> None:
    event = _raw_event(
        "intaris",
        "sess-1",
        {
            "seq": 9,
            "type": "assistant_message",
            "ts": "2026-07-01T19:35:39.901699+00:00",
            "data": {"content": "answer"},
        },
    )

    assert event.timestamp is not None
    assert event.timestamp.isoformat() == "2026-07-01T19:35:39.901699+00:00"


@pytest.mark.anyio
async def test_intaris_backward_read_pages_until_before_sequence() -> None:
    guardrails = PagedGuardrails(
        [
            {"seq": 1, "type": "user_message", "data": {"content": "one"}},
            {"seq": 2, "type": "user_message", "data": {"content": "two"}},
            {"seq": 3, "type": "user_message", "data": {"content": "three"}},
            {"seq": 4, "type": "user_message", "data": {"content": "four"}},
            {"seq": 5, "type": "user_message", "data": {"content": "five"}},
        ],
        page_size=2,
    )
    store = IntarisSessionEventStore(guardrails)

    page = await store.read_session_events(
        session_id="sess-1",
        before_seq=5,
        limit=2,
        direction="backward",
    )

    assert [event.seq for event in page.events] == [3, 4]
    assert page.has_more_before is True
    assert guardrails.calls == [{"after_seq": 0, "before_seq": 5, "limit": 2}]


@pytest.mark.anyio
async def test_intaris_high_watermark_allows_verified_empty_stream() -> None:
    guardrails = PagedGuardrails([], page_size=2)
    store = IntarisSessionEventStore(guardrails)

    watermark = await store.read_session_high_watermark(session_id="sess-empty")

    assert watermark.last_seq == 0


@pytest.mark.anyio
async def test_intaris_empty_page_is_marked_verified() -> None:
    guardrails = PagedGuardrails([], page_size=2)
    store = IntarisSessionEventStore(guardrails)

    page = await store.read_session_events(session_id="sess-empty")

    assert page.events == []
    assert page.verified_empty is True


@pytest.mark.anyio
async def test_intaris_provider_verifies_existing_session_with_missing_event_stream() -> None:
    provider = IntarisProvider("http://localhost:8060", AuthProvider())
    calls: list[str] = []

    async def fake_get(path: str, **_: object) -> httpx.Response:
        calls.append(path)
        request = httpx.Request("GET", f"http://localhost:8060{path}")
        if path.endswith("/events"):
            return httpx.Response(404, request=request)
        return httpx.Response(200, request=request, json={"session_id": "sess-empty"})

    provider.client.get = fake_get  # type: ignore[method-assign]
    try:
        with scoped_runtime_context(
            user_email="user@example.com",
            agent_id="agent-a",
            agent_owner_email="owner@example.com",
        ):
            page = await IntarisSessionEventStore(provider).read_session_events(
                session_id="sess-empty"
            )
    finally:
        await provider.client.aclose()

    assert page.events == []
    assert page.verified_empty is True
    assert calls == ["/api/v1/session/sess-empty/events", "/api/v1/session/sess-empty"]


@pytest.mark.anyio
async def test_intaris_provider_does_not_verify_missing_session_as_empty() -> None:
    provider = IntarisProvider("http://localhost:8060", AuthProvider())
    calls: list[str] = []

    async def fake_get(path: str, **_: object) -> httpx.Response:
        calls.append(path)
        request = httpx.Request("GET", f"http://localhost:8060{path}")
        return httpx.Response(404, request=request)

    provider.client.get = fake_get  # type: ignore[method-assign]
    try:
        with (
            scoped_runtime_context(
                user_email="user@example.com",
                agent_id="agent-a",
                agent_owner_email="owner@example.com",
            ),
            pytest.raises(httpx.HTTPStatusError),
        ):
            await IntarisSessionEventStore(provider).read_session_events(
                session_id="missing-session"
            )
    finally:
        await provider.client.aclose()

    assert calls == [
        "/api/v1/session/missing-session/events",
        "/api/v1/session/missing-session",
    ]
