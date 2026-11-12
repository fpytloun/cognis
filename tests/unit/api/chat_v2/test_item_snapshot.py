from __future__ import annotations

from typing import Any

import pytest

from cognis.api.chat_v2 import item_snapshot as snapshot
from cognis.api.chat_v2.event_store import (
    RawSessionEvent,
    SessionEventPage,
    SessionHistoryAvailability,
    SessionHistoryGap,
    SessionHistoryUnavailableError,
)
from cognis.api.chat_v2.normalizer import normalize_session_events
from cognis.api.chat_v2.projector import project_timeline
from cognis.api.chat_v2.schemas import SourceRef


def message(
    seq: int, content: str, *, delta: bool = False, turn: str = "turn-a"
) -> RawSessionEvent:
    return RawSessionEvent(
        store_id="intaris",
        session_id="session-a",
        seq=seq,
        event_id=f"event-{seq}",
        type="assistant_message",
        data={
            "content": content,
            "turn_id": turn,
            "message_id": turn,
            "assistant_phase_index": 0,
            "content_is_delta": delta,
        },
    )


def identity(event: RawSessionEvent) -> tuple[SourceRef, str]:
    item = project_timeline(normalize_session_events([event]).events).timeline.items[0]
    return item.source_refs[0], item.id


class Reader:
    store_id = "intaris"

    def __init__(self, events: list[RawSessionEvent], page_cap: int = 400) -> None:
        self.events, self.page_cap = events, page_cap
        self.calls: list[dict[str, Any]] = []
        self.gap: SessionHistoryGap | None = None

    async def read_session_events(self, **kwargs: Any) -> SessionEventPage:
        self.calls.append(kwargs)
        events = sorted(
            (e for e in self.events if e.seq < kwargs["before_seq"]),
            key=lambda e: e.seq,
            reverse=True,
        )[: min(kwargs["limit"], self.page_cap)]
        return SessionEventPage(
            store_id="intaris",
            session_id="session-a",
            events=events,
            has_more_before=bool(events and events[-1].seq > 1),
            availability=SessionHistoryAvailability(
                durable_last_seq=max(e.seq for e in self.events),
                first_available_seq=min(e.seq for e in self.events),
                history_gap=self.gap,
            ),
        )


@pytest.mark.asyncio
async def test_append_replacement_and_future_exclusion() -> None:
    events = [
        message(1, "First"),
        message(2, " second", delta=True),
        message(3, "Replacement"),
        message(4, " final", delta=True),
    ]
    reader = Reader(events, page_cap=1)
    for seq, expected in [
        (1, "First"),
        (2, "First second"),
        (3, "Replacement"),
        (4, "Replacement final"),
    ]:
        ref, item_id = identity(events[seq - 1])
        result = await snapshot.read_assistant_item_as_of(reader, source=ref, item_id=item_id)
        assert result.content == expected
        assert max(r.seq for r in result.source_refs) == seq
    reader.events.append(message(5, "Future replacement"))
    ref, item_id = identity(events[1])
    result = await snapshot.read_assistant_item_as_of(reader, source=ref, item_id=item_id)
    assert result.content == "First second"


@pytest.mark.asyncio
async def test_base_fast_path_needs_no_history_or_normalization_of_prior_payload() -> None:
    events = [message(1, "x" * snapshot.MAX_REPLAY_BYTES), message(2, "Replacement")]
    reader = Reader(events)
    ref, item_id = identity(events[1])
    assert (
        await snapshot.read_assistant_item_as_of(reader, source=ref, item_id=item_id)
    ).content == "Replacement"
    assert len(reader.calls) == 1 and reader.calls[0]["limit"] == 1


@pytest.mark.asyncio
async def test_unrelated_turns_and_hidden_events_never_contribute() -> None:
    events = [
        message(1, "Base"),
        message(2, "Other", turn="turn-b"),
        message(3, "hidden", delta=True).model_copy(update={"prompt_visibility": "hidden"}),
        message(4, " final", delta=True),
    ]
    ref, item_id = identity(events[-1])
    result = await snapshot.read_assistant_item_as_of(Reader(events), source=ref, item_id=item_id)
    assert result.content == "Base final"
    assert [r.seq for r in result.source_refs] == [1, 4]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "change",
    [
        {"event_id": "another-event"},
        {"event_type": "tool_result"},
        {"session_id": "another-session"},
        {"store": "another-store"},
        {"seq": 5},
    ],
)
async def test_wrong_selected_identity(change: dict[str, Any]) -> None:
    event = message(1, "Base")
    ref, item_id = identity(event)
    with pytest.raises(snapshot.ItemSnapshotUnavailable):
        await snapshot.read_assistant_item_as_of(
            Reader([event]), source=ref.model_copy(update=change), item_id=item_id
        )


@pytest.mark.asyncio
async def test_wrong_item_hidden_selected_and_missing_base() -> None:
    event = message(1, "Base")
    ref, item_id = identity(event)
    for events, target in [
        ([event], "unrelated-item"),
        ([event.model_copy(update={"lane": "internal"})], item_id),
        ([event.model_copy(update={"prompt_visibility": "hidden"})], item_id),
        ([message(1, "orphan", delta=True)], item_id),
    ]:
        with pytest.raises(snapshot.ItemSnapshotUnavailable):
            await snapshot.read_assistant_item_as_of(Reader(events), source=ref, item_id=target)


@pytest.mark.asyncio
async def test_gap_and_pagination_stall_are_unavailable() -> None:
    events = [
        message(1, "Base"),
        message(2, " middle", delta=True),
        message(3, " final", delta=True),
    ]
    ref, item_id = identity(events[-1])
    reader = Reader(events)
    reader.gap = SessionHistoryGap(from_seq=2, to_seq=2, reason="internal_gap")
    with pytest.raises(SessionHistoryUnavailableError):
        await snapshot.read_assistant_item_as_of(reader, source=ref, item_id=item_id)
    with pytest.raises(snapshot.ItemSnapshotUnavailable):
        await snapshot.read_assistant_item_as_of(
            Reader([events[0], events[-1]]), source=ref, item_id=item_id
        )

    class Stalled(Reader):
        async def read_session_events(self, **kwargs: Any) -> SessionEventPage:
            page = await super().read_session_events(**kwargs)
            return (
                page if kwargs["limit"] == 1 else page.model_copy(update={"events": [events[-1]]})
            )

    with pytest.raises(snapshot.ItemSnapshotUnavailable):
        await snapshot.read_assistant_item_as_of(Stalled(events), source=ref, item_id=item_id)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "change",
    [
        {"events": []},
        {"store_id": "another-store"},
        {"session_id": "another-session"},
        {"events": [message(1, "Base")] * (snapshot.PAGE_EVENTS + 1)},
    ],
)
async def test_unproven_backfill_pages_are_unavailable(change: dict[str, Any]) -> None:
    events = [message(1, "Base"), message(2, " delta", delta=True)]
    ref, item_id = identity(events[-1])

    class Unproven(Reader):
        async def read_session_events(self, **kwargs: Any) -> SessionEventPage:
            page = await super().read_session_events(**kwargs)
            return page if kwargs["limit"] == 1 else page.model_copy(update=change)

    with pytest.raises(snapshot.ItemSnapshotUnavailable):
        await snapshot.read_assistant_item_as_of(Unproven(events), source=ref, item_id=item_id)


@pytest.mark.asyncio
async def test_page_and_event_limits_are_real_limits_not_configured_down() -> None:
    events = [message(i, "x", delta=i > 1) for i in range(1, snapshot.MAX_EVENTS + 2)]
    ref, item_id = identity(events[-1])
    reader = Reader(events)
    with pytest.raises(snapshot.ItemSnapshotUnavailable):
        await snapshot.read_assistant_item_as_of(reader, source=ref, item_id=item_id)
    assert sum(call["limit"] for call in reader.calls) == snapshot.MAX_EVENTS
    assert max(call["limit"] for call in reader.calls) == snapshot.PAGE_EVENTS
    reader = Reader(events[-30:], page_cap=1)
    with pytest.raises(snapshot.ItemSnapshotUnavailable):
        await snapshot.read_assistant_item_as_of(reader, source=ref, item_id=item_id)
    assert len(reader.calls) == snapshot.MAX_PAGES


@pytest.mark.asyncio
async def test_exact_event_limit_succeeds() -> None:
    events = [message(i, "x", delta=i > 1) for i in range(1, snapshot.MAX_EVENTS + 1)]
    ref, item_id = identity(events[-1])
    result = await snapshot.read_assistant_item_as_of(Reader(events), source=ref, item_id=item_id)
    assert len(result.content) == snapshot.MAX_EVENTS


@pytest.mark.asyncio
async def test_metadata_byte_cap_precedes_expansion(monkeypatch: pytest.MonkeyPatch) -> None:
    selected = message(1, "small")
    ref, item_id = identity(selected)
    selected = selected.model_copy(update={"event_id": "i" * snapshot.MAX_REPLAY_BYTES})
    monkeypatch.setattr(
        snapshot,
        "normalize_session_events",
        lambda _: pytest.fail("oversized metadata reached canonical expansion"),
    )
    with pytest.raises(snapshot.ItemSnapshotUnavailable):
        await snapshot.read_assistant_item_as_of(
            Reader([selected]), source=ref.model_copy(update={"event_id": None}), item_id=item_id
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "text", ["x" * snapshot.MAX_REPLAY_BYTES, "\0" * 1_500_000, "😀" * 2_100_000]
)
async def test_selected_byte_budget_precedes_expansion(
    text: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    event = message(1, text)
    ref, item_id = identity(message(1, "small"))

    def forbidden(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("oversized event reached canonical expansion")

    monkeypatch.setattr(snapshot, "normalize_session_events", forbidden)
    with pytest.raises(snapshot.ItemSnapshotUnavailable):
        await snapshot.read_assistant_item_as_of(Reader([event]), source=ref, item_id=item_id)


@pytest.mark.asyncio
async def test_cumulative_bytes_fail_before_expanding_predecessor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events = [message(1, "x" * 4_200_000), message(2, "y" * 4_200_000, delta=True)]
    ref, item_id = identity(message(2, "small", delta=True))
    original = snapshot.normalize_session_events
    seen = []

    def observed(events: Any) -> Any:
        seen.extend(e.seq for e in events)
        return original(events)

    monkeypatch.setattr(snapshot, "normalize_session_events", observed)
    with pytest.raises(snapshot.ItemSnapshotUnavailable):
        await snapshot.read_assistant_item_as_of(Reader(events), source=ref, item_id=item_id)
    assert seen == [2]


@pytest.mark.asyncio
async def test_oversized_page_is_rejected_before_expansion(monkeypatch: pytest.MonkeyPatch) -> None:
    events = [message(1, "Base"), message(2, "delta", delta=True)]
    ref, item_id = identity(events[1])

    class Oversized(Reader):
        async def read_session_events(self, **kwargs: Any) -> SessionEventPage:
            page = await super().read_session_events(**kwargs)
            return page.model_copy(update={"events": events})

    def forbidden(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("oversized page expanded")

    monkeypatch.setattr(snapshot, "normalize_session_events", forbidden)
    with pytest.raises(snapshot.ItemSnapshotUnavailable):
        await snapshot.read_assistant_item_as_of(Oversized(events), source=ref, item_id=item_id)
