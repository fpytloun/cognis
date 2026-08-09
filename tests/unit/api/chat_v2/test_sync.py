"""Tests for Chat v2 snapshot/sync/backfill orchestration."""

from __future__ import annotations

import asyncio
import random
from collections.abc import Coroutine
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any, Literal

import pytest

from cognis.api.chat_v2 import sync as sync_module
from cognis.api.chat_v2.cursors import ChatCursorError, validate_cursor
from cognis.api.chat_v2.event_store import RawSessionEvent, SessionEventPage, SessionWatermark
from cognis.api.chat_v2.schemas import (
    ConversationStateView,
    ConversationSummary,
    MessageTimelineItem,
    TimelineScope,
    ToolCallTimelineItem,
    UpsertTimelineItemOp,
)
from cognis.api.chat_v2.sync import (
    PROJECTION_VERSION,
    ChatV2SyncError,
    ConversationSessionRef,
    build_chat_snapshot,
    build_chat_sync_response,
    build_timeline_backfill_response,
    conversation_summary_from_row,
    queue_state_from_messages,
    validate_backfill_limit,
    validate_sync_limit,
)

NOW = datetime(2026, 6, 29, 12, 0, tzinfo=UTC)
SECRET = "test-secret"


def _scope() -> TimelineScope:
    return TimelineScope(key="conversation:conv_1", kind="conversation", conversation_id="conv_1")


# Keep the historical orchestration cases focused on projection behavior while
# supplying the now-required first-class scope contract.
_build_chat_snapshot = build_chat_snapshot
_build_chat_sync_response = build_chat_sync_response
_build_timeline_backfill_response = build_timeline_backfill_response


async def build_chat_snapshot(**kwargs: Any) -> Any:
    kwargs.setdefault("scope", _scope())
    return await _build_chat_snapshot(**kwargs)


async def build_chat_sync_response(**kwargs: Any) -> Any:
    kwargs.pop("conversation_id", None)
    kwargs.setdefault("scope", _scope())
    return await _build_chat_sync_response(**kwargs)


async def build_timeline_backfill_response(**kwargs: Any) -> Any:
    kwargs.pop("conversation_id", None)
    kwargs.setdefault("scope", _scope())
    return await _build_timeline_backfill_response(**kwargs)


class FakeEventStore:
    def __init__(
        self,
        events_by_session: dict[str, list[RawSessionEvent]],
        *,
        page_cap: int | None = None,
        read_delay: float = 0,
    ) -> None:
        self.events_by_session = events_by_session
        self.page_cap = page_cap
        self.read_delay = read_delay
        self.calls: list[dict[str, Any]] = []
        self.watermark_calls: list[str] = []
        self.in_flight_reads = 0
        self.max_concurrent_reads = 0
        self.in_flight_watermarks = 0
        self.max_concurrent_watermarks = 0

    async def read_session_events(
        self,
        *,
        session_id: str,
        after_seq: int | None = None,
        before_seq: int | None = None,
        limit: int = 500,
        direction: Literal["forward", "backward"] = "forward",
    ) -> SessionEventPage:
        self.calls.append(
            {
                "session_id": session_id,
                "after_seq": after_seq,
                "before_seq": before_seq,
                "limit": limit,
                "direction": direction,
            }
        )
        self.in_flight_reads += 1
        self.max_concurrent_reads = max(self.max_concurrent_reads, self.in_flight_reads)
        if self.read_delay:
            await asyncio.sleep(self.read_delay)
        self.in_flight_reads -= 1
        source_events = self.events_by_session.get(session_id, [])
        effective_limit = min(limit, self.page_cap) if self.page_cap is not None else limit
        if direction == "backward":
            candidates = [
                event for event in source_events if before_seq is None or event.seq < before_seq
            ]
            events = candidates[-effective_limit:]
            return SessionEventPage(
                store_id="intaris",
                session_id=session_id,
                events=events,
                first_seq=events[0].seq if events else None,
                last_seq=max((event.seq for event in source_events), default=0),
                has_more_before=len(candidates) > len(events),
            )

        remaining = [event for event in source_events if event.seq > (after_seq or 0)]
        events = remaining[:effective_limit]
        return SessionEventPage(
            store_id="intaris",
            session_id=session_id,
            events=events,
            first_seq=events[0].seq if events else None,
            last_seq=max((event.seq for event in source_events), default=0),
            has_more_after=len(remaining) > len(events),
        )

    async def read_session_high_watermark(self, *, session_id: str) -> SessionWatermark:
        self.watermark_calls.append(session_id)
        self.in_flight_watermarks += 1
        self.max_concurrent_watermarks = max(
            self.max_concurrent_watermarks,
            self.in_flight_watermarks,
        )
        if self.read_delay:
            await asyncio.sleep(self.read_delay)
        self.in_flight_watermarks -= 1
        events = self.events_by_session.get(session_id, [])
        return SessionWatermark(
            store_id="intaris",
            session_id=session_id,
            last_seq=max((event.seq for event in events), default=0),
        )


@pytest.fixture(autouse=True)
def _clear_chat_v2_read_caches() -> None:
    sync_module.clear_chat_v2_read_caches()


def test_projection_cache_key_contains_only_opaque_authority_token() -> None:
    authority_token = "f" * 64
    ref = ConversationSessionRef(
        session_id="session-1",
        event_store_session_id="intaris-1",
        ordinal=0,
        authority_token=authority_token,
    )

    key = sync_module._snapshot_projection_cache_key(  # noqa: SLF001
        conversation_id="conversation-1",
        session_refs=[ref],
        watermarks={("intaris", "intaris-1"): 3},
        limit=500,
        event_post_processor_cache_key="projection-v1",
    )
    serialized = repr(key)

    assert authority_token in serialized
    assert "user@example.com" not in serialized
    assert "agent-a" not in serialized
    assert "owner@example.com" not in serialized


def test_snapshot_builds_projection_and_cursor() -> None:
    store = FakeEventStore(
        {
            "intaris_1": [
                _event(1, "user_message", {"content": "hello", "client_message_id": "c1"}),
                _event(2, "assistant_message", {"content": "hi", "message_id": "a1"}),
            ]
        }
    )

    snapshot = _run(
        build_chat_snapshot(
            conversation=_conversation(),
            session_refs=_lineage(),
            event_store=store,
            cursor_secret=SECRET,
            state=_state(),
            now=NOW,
        )
    )

    assert snapshot.projection_version == PROJECTION_VERSION
    assert [item.id for item in snapshot.timeline.items] == ["user:c1", "message:a1"]
    assert snapshot.cursor
    assert snapshot.runtime.has_active_turn is False


def test_snapshot_hydrates_legacy_attachment_refs_before_projection() -> None:
    store = FakeEventStore(
        {
            "intaris_1": [
                _event(
                    1,
                    "user_message",
                    {
                        "content": "see attached",
                        "client_message_id": "c1",
                        "attachments": [{"artifact_id": "art_1"}],
                    },
                ),
            ]
        }
    )

    snapshot = _run(
        build_chat_snapshot(
            conversation=_conversation(),
            session_refs=_lineage(),
            event_store=store,
            cursor_secret=SECRET,
            event_post_processor=_attachment_processor,
            now=NOW,
        )
    )

    item = snapshot.timeline.items[0]
    assert isinstance(item, MessageTimelineItem)
    assert item.attachments[0].artifact_id == "art_1"
    assert item.attachments[0].filename == "hydrated-art_1.txt"
    assert item.attachments[0].url == "https://artifacts.test/art_1"


def test_snapshot_parallelizes_watermark_and_window_reads() -> None:
    store = FakeEventStore(
        {
            "intaris_old": [
                _event_for(
                    "intaris_old", 1, "user_message", {"content": "old", "client_message_id": "old"}
                )
            ],
            "intaris_1": [
                _event(1, "user_message", {"content": "active", "client_message_id": "c1"})
            ],
        },
        read_delay=0.01,
    )

    snapshot = _run(
        build_chat_snapshot(
            conversation=_conversation(),
            session_refs=_lineage_with_compacted_parent(),
            event_store=store,
            cursor_secret=SECRET,
            now=NOW,
        )
    )

    assert [item.id for item in snapshot.timeline.items] == ["user:old", "user:c1"]
    assert store.max_concurrent_watermarks == 2
    assert store.max_concurrent_reads == 1


def test_snapshot_dense_eighteen_session_lineage_reads_only_newest_page(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sync_module, "SNAPSHOT_WINDOW_EVENT_LIMIT", 800)
    refs = _many_lineage(18)
    events_by_session = {
        ref.event_store_session_id: [
            _event_for(
                ref.event_store_session_id,
                seq,
                "user_message",
                {"content": str(seq), "client_message_id": f"{ref.ordinal}-{seq}"},
            )
            for seq in range(1, (801 if ref.ordinal == 17 else 3))
        ]
        for ref in refs
    }
    store = FakeEventStore(events_by_session)

    snapshot = _run(
        build_chat_snapshot(
            conversation=_conversation(),
            session_refs=refs,
            event_store=store,
            cursor_secret=SECRET,
            now=NOW,
        )
    )

    assert len(snapshot.timeline.items) == 800
    assert {call["session_id"] for call in store.calls} == {"intaris_17"}
    assert len(store.watermark_calls) == 18
    cursor = validate_cursor(
        snapshot.cursor,
        SECRET,
        scope_key=_scope().key,
        projection_version=sync_module.current_projection_version(),
        now=NOW,
    )
    assert len(cursor.session_watermarks) == 18


def test_latest_window_sparse_lineage_expands_in_batches_and_caps_concurrency() -> None:
    refs = _many_lineage(18)
    events_by_session = {
        ref.event_store_session_id: [
            _event_for(
                ref.event_store_session_id,
                1,
                "user_message",
                {"client_message_id": f"message-{ref.ordinal}"},
            )
        ]
        if ref.ordinal in {0, 4, 9, 17}
        else []
        for ref in refs
    }
    store = FakeEventStore(events_by_session, read_delay=0.001)

    actual = _run(
        sync_module._read_latest_window(  # noqa: SLF001
            session_refs=refs,
            event_store=store,
            limit=20,
        )
    )

    assert [event.data["client_message_id"] for event in actual.events] == [
        "message-0",
        "message-4",
        "message-9",
        "message-17",
    ]
    assert actual.has_more_before is False
    assert [call["session_id"] for call in store.calls] == [
        "intaris_17",
        "intaris_15",
        "intaris_16",
        "intaris_11",
        "intaris_12",
        "intaris_13",
        "intaris_14",
        "intaris_3",
        "intaris_4",
        "intaris_5",
        "intaris_6",
        "intaris_7",
        "intaris_8",
        "intaris_9",
        "intaris_10",
        "intaris_0",
        "intaris_1",
        "intaris_2",
    ]
    assert store.max_concurrent_reads == 8


def test_latest_window_equal_timestamps_preserves_lineage_seq_event_id_order() -> None:
    refs = _many_lineage(3)
    events_by_session: dict[str, list[RawSessionEvent]] = {}
    for ref in refs:
        events_by_session[ref.event_store_session_id] = [
            _event_for(
                ref.event_store_session_id,
                seq,
                "user_message",
                {"client_message_id": f"{ref.ordinal}-{seq}"},
            ).model_copy(update={"timestamp": NOW, "event_id": f"event-{2 - seq}"})
            for seq in (1, 2)
        ]
    store = FakeEventStore(events_by_session)

    actual = _run(
        sync_module._read_latest_window(  # noqa: SLF001
            session_refs=refs,
            event_store=store,
            limit=5,
        )
    )

    assert [
        (event.data["_lineage_index"], event.seq, event.event_id) for event in actual.events
    ] == [
        (0, 2, "event-0"),
        (1, 1, "event-1"),
        (1, 2, "event-0"),
        (2, 1, "event-1"),
        (2, 2, "event-0"),
    ]
    assert actual.has_more_before is False


@pytest.mark.parametrize("seed", range(20))
def test_latest_window_matches_eager_reference_for_randomized_lineages(seed: int) -> None:
    rng = random.Random(seed)
    session_count = rng.randint(1, 30)
    limit = rng.choice([1, 2, 7, 31, 100, 800])
    refs = _many_lineage(session_count)
    events_by_session: dict[str, list[RawSessionEvent]] = {}
    for ref in refs:
        count = rng.randint(0, min(120, limit + 20))
        seq = 0
        events: list[RawSessionEvent] = []
        for event_index in range(count):
            seq += rng.randint(1, 3)
            events.append(
                _event_for(
                    ref.event_store_session_id,
                    seq,
                    "user_message",
                    {"client_message_id": f"{ref.ordinal}-{event_index}"},
                ).model_copy(
                    update={
                        "event_id": f"event-{rng.randint(0, 2)}",
                        "timestamp": NOW,
                    }
                )
            )
        events_by_session[ref.event_store_session_id] = events
    page_cap = rng.choice([None, None, max(1, min(limit, 5))])
    eager_store = FakeEventStore(events_by_session, page_cap=page_cap)
    adaptive_store = FakeEventStore(events_by_session, page_cap=page_cap)

    expected = _run(_read_latest_window_eager(refs, eager_store, limit))
    actual = _run(
        sync_module._read_latest_window(  # noqa: SLF001
            session_refs=refs,
            event_store=adaptive_store,
            limit=limit,
        )
    )

    assert actual.model_dump(mode="json") == expected.model_dump(mode="json")
    assert adaptive_store.max_concurrent_reads <= 8


def test_latest_window_exact_session_boundary_sets_has_more_before() -> None:
    refs = _many_lineage(3)
    store = FakeEventStore(
        {
            ref.event_store_session_id: [
                _event_for(
                    ref.event_store_session_id,
                    seq,
                    "user_message",
                    {"client_message_id": f"{ref.ordinal}-{seq}"},
                )
                for seq in (1, 2)
            ]
            for ref in refs
        }
    )

    window = _run(
        sync_module._read_latest_window(  # noqa: SLF001
            session_refs=refs,
            event_store=store,
            limit=4,
        )
    )

    assert [event.data["client_message_id"] for event in window.events] == [
        "1-1",
        "1-2",
        "2-1",
        "2-2",
    ]
    assert window.has_more_before is True


def test_latest_window_records_window_metrics_only_for_snapshot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class MetricsRecorder:
        def __init__(self) -> None:
            self.windows: list[dict[str, int]] = []

        def observe_window(self, **values: int) -> None:
            self.windows.append(values)

    metrics = MetricsRecorder()
    monkeypatch.setattr(sync_module, "SNAPSHOT_SYNC_METRICS", metrics)
    store = FakeEventStore({"intaris_1": [_event(1, "user_message", {})]})

    _run(
        sync_module._read_latest_window(  # noqa: SLF001
            session_refs=_lineage(),
            event_store=store,
            limit=1,
        )
    )
    assert metrics.windows == []

    _run(
        sync_module._read_latest_window(  # noqa: SLF001
            session_refs=_lineage(),
            event_store=store,
            limit=1,
            record_metrics=True,
        )
    )
    assert metrics.windows == [
        {
            "sessions_read": 1,
            "pages_read": 1,
            "events_fetched": 1,
            "events_selected": 1,
            "events_discarded": 0,
            "rounds": 1,
        }
    ]


def test_adaptive_snapshot_before_cursor_backfills_without_gap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sync_module, "SNAPSHOT_WINDOW_EVENT_LIMIT", 2)
    refs = _many_lineage(3)
    store = FakeEventStore(
        {
            ref.event_store_session_id: [
                _event_for(
                    ref.event_store_session_id,
                    seq,
                    "user_message",
                    {"client_message_id": f"{ref.ordinal}-{seq}"},
                )
                for seq in (1, 2)
            ]
            for ref in refs
        }
    )

    snapshot = _run(
        build_chat_snapshot(
            conversation=_conversation(),
            session_refs=refs,
            event_store=store,
            cursor_secret=SECRET,
            now=NOW,
        )
    )
    assert [item.id for item in snapshot.timeline.items] == ["user:2-1", "user:2-2"]
    assert snapshot.timeline.has_more_before is True
    assert snapshot.timeline.before_cursor

    page = _run(
        build_timeline_backfill_response(
            before=snapshot.timeline.before_cursor,
            session_refs=refs,
            event_store=store,
            cursor_secret=SECRET,
            limit=2,
            now=NOW,
        )
    )

    assert [item.id for item in page.items] == ["user:1-1", "user:1-2"]
    assert page.has_more_before is True
    assert page.before_cursor


def test_adaptive_snapshot_hydrates_pairing_across_window_boundary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sync_module, "SNAPSHOT_WINDOW_EVENT_LIMIT", 1)
    store = FakeEventStore(
        {
            "intaris_1": [
                _event(
                    1,
                    "tool_call",
                    {"call_id": "call_boundary", "name": "bash", "arguments": {"command": "pwd"}},
                ),
                _event(
                    2,
                    "tool_result",
                    {"call_id": "call_boundary", "name": "bash", "result": "/tmp"},
                ),
            ]
        }
    )

    snapshot = _run(
        build_chat_snapshot(
            conversation=_conversation(),
            session_refs=_lineage(),
            event_store=store,
            cursor_secret=SECRET,
            now=NOW,
        )
    )

    assert [item.id for item in snapshot.timeline.items] == ["tool:call_boundary"]
    item = snapshot.timeline.items[0]
    assert isinstance(item, ToolCallTimelineItem)
    assert item.arguments == {"command": "pwd"}
    assert item.result_preview == "/tmp"
    assert ":000000000000001:" in item.sort_key
    assert [call["before_seq"] for call in store.calls] == [None, 2]


def test_adaptive_snapshot_preserves_attachment_hydration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sync_module, "SNAPSHOT_WINDOW_EVENT_LIMIT", 1)
    refs = _many_lineage(18)
    store = FakeEventStore(
        {
            ref.event_store_session_id: [
                _event_for(
                    ref.event_store_session_id,
                    1,
                    "user_message",
                    {
                        "content": "attachment",
                        "client_message_id": f"attachment-{ref.ordinal}",
                        "attachments": [{"artifact_id": f"art_{ref.ordinal}"}],
                    },
                )
            ]
            for ref in refs
        }
    )

    async def hydrate(events: list[RawSessionEvent]) -> list[RawSessionEvent]:
        assert len(events) == 1
        event = events[0]
        attachment = event.data["attachments"][0]
        return [
            event.model_copy(
                update={
                    "data": {
                        **event.data,
                        "attachments": [
                            {
                                **attachment,
                                "kind": "file",
                                "filename": "latest.txt",
                                "mime_type": "text/plain",
                                "size_bytes": 1,
                            }
                        ],
                    }
                }
            )
        ]

    snapshot = _run(
        build_chat_snapshot(
            conversation=_conversation(),
            session_refs=refs,
            event_store=store,
            cursor_secret=SECRET,
            event_post_processor=hydrate,
            now=NOW,
        )
    )

    item = snapshot.timeline.items[0]
    assert isinstance(item, MessageTimelineItem)
    assert item.id == "user:attachment-17"
    assert item.attachments[0].filename == "latest.txt"
    assert {call["session_id"] for call in store.calls} == {"intaris_17"}


def test_adaptive_snapshot_uses_each_session_authority_reader() -> None:
    refs: list[ConversationSessionRef] = []
    authority_stores: list[FakeEventStore] = []
    for ordinal in range(3):
        event_store_session_id = f"authority_{ordinal}"
        authority_store = FakeEventStore(
            {
                event_store_session_id: [
                    _event_for(
                        event_store_session_id,
                        1,
                        "user_message",
                        {"client_message_id": f"authority-{ordinal}"},
                    )
                ]
            }
        )
        authority_stores.append(authority_store)
        refs.append(
            ConversationSessionRef(
                session_id=f"sess_authority_{ordinal}",
                event_store_session_id=event_store_session_id,
                ordinal=ordinal,
                reader=authority_store,
                authority_token=str(ordinal) * 64,
            )
        )
    fallback = FakeEventStore({})

    snapshot = _run(
        build_chat_snapshot(
            conversation=_conversation(),
            session_refs=refs,
            event_store=fallback,
            cursor_secret=SECRET,
            now=NOW,
        )
    )

    assert [item.id for item in snapshot.timeline.items] == [
        "user:authority-0",
        "user:authority-1",
        "user:authority-2",
    ]
    assert fallback.calls == []
    assert fallback.watermark_calls == []
    for ref, authority_store in zip(refs, authority_stores, strict=True):
        assert authority_store.watermark_calls == [ref.event_store_session_id]
        assert [call["session_id"] for call in authority_store.calls] == [
            ref.event_store_session_id
        ]


def test_snapshot_reuses_projection_cache_after_canonical_watermark_read() -> None:
    store = FakeEventStore(
        {
            "intaris_old": [
                _event_for(
                    "intaris_old", 1, "user_message", {"content": "old", "client_message_id": "old"}
                )
            ]
        }
    )
    session_refs = [
        ConversationSessionRef(
            session_id="sess_old",
            event_store_session_id="intaris_old",
            ordinal=0,
            status="completed",
            completion_reason="compacted",
        )
    ]

    first = _run(
        build_chat_snapshot(
            conversation=_conversation(),
            session_refs=session_refs,
            event_store=store,
            cursor_secret=SECRET,
            now=NOW,
        )
    )
    store.calls.clear()
    store.watermark_calls.clear()
    second = _run(
        build_chat_snapshot(
            conversation=_conversation(),
            session_refs=session_refs,
            event_store=store,
            cursor_secret=SECRET,
            now=NOW,
        )
    )

    assert second.timeline.model_dump(mode="json") == first.timeline.model_dump(mode="json")
    assert store.watermark_calls == ["intaris_old"]
    assert store.calls == []

    rotated_refs = [
        *session_refs,
        ConversationSessionRef(
            session_id="sess_1",
            event_store_session_id="intaris_1",
            ordinal=1,
            status="active",
        ),
    ]
    store.events_by_session["intaris_1"] = [
        _event(1, "user_message", {"content": "new active", "client_message_id": "c1"})
    ]
    rotated = _run(
        build_chat_snapshot(
            conversation=_conversation(),
            session_refs=rotated_refs,
            event_store=store,
            cursor_secret=SECRET,
            now=NOW,
        )
    )

    assert [item.id for item in rotated.timeline.items] == ["user:old", "user:c1"]
    assert store.watermark_calls == ["intaris_old", "intaris_old", "intaris_1"]
    assert [call["session_id"] for call in store.calls] == ["intaris_1", "intaris_old"]


def test_snapshot_projection_cache_invalidates_when_active_watermark_changes() -> None:
    store = FakeEventStore(
        {"intaris_1": [_event(1, "user_message", {"content": "one", "client_message_id": "c1"})]}
    )
    first = _run(
        build_chat_snapshot(
            conversation=_conversation(),
            session_refs=_lineage(),
            event_store=store,
            cursor_secret=SECRET,
            now=NOW,
        )
    )
    store.calls.clear()

    second = _run(
        build_chat_snapshot(
            conversation=_conversation(),
            session_refs=_lineage(),
            event_store=store,
            cursor_secret=SECRET,
            now=NOW,
        )
    )

    assert second.timeline.model_dump(mode="json") == first.timeline.model_dump(mode="json")
    assert store.calls == []

    store.events_by_session["intaris_1"].append(
        _event(2, "user_message", {"content": "two", "client_message_id": "c2"})
    )
    changed = _run(
        build_chat_snapshot(
            conversation=_conversation(),
            session_refs=_lineage(),
            event_store=store,
            cursor_secret=SECRET,
            now=NOW,
        )
    )

    assert [item.id for item in changed.timeline.items] == ["user:c1", "user:c2"]
    assert [call["session_id"] for call in store.calls] == ["intaris_1"]


def test_active_session_snapshot_ignores_session_cache_for_canonical_reads() -> None:
    store = FakeEventStore({"intaris_1": []})
    session_cache = SimpleNamespace(
        get_entry=lambda session_id: SimpleNamespace(
            session_id=session_id,
            intaris_session_id="intaris_1",
            initialized=True,
            last_event_seq=1,
            events=[
                SimpleNamespace(
                    seq=1,
                    type="user_message",
                    data={"content": "cached", "client_message_id": "cached"},
                )
            ],
        )
    )

    snapshot = _run(
        build_chat_snapshot(
            conversation=_conversation(),
            session_refs=_lineage(),
            event_store=store,
            cursor_secret=SECRET,
            session_cache=session_cache,
            now=NOW,
        )
    )

    assert snapshot.timeline.items == []
    assert store.watermark_calls == ["intaris_1"]
    assert [call["session_id"] for call in store.calls] == ["intaris_1"]


def test_active_session_snapshot_falls_back_when_session_cache_is_pruned() -> None:
    store = FakeEventStore(
        {
            "intaris_1": [
                _event(1, "user_message", {"content": "one", "client_message_id": "c1"}),
                _event(2, "assistant_message", {"content": "two", "message_id": "a1"}),
                _event(3, "user_message", {"content": "three", "client_message_id": "c2"}),
            ]
        }
    )
    session_cache = SimpleNamespace(
        get_entry=lambda session_id: SimpleNamespace(
            session_id=session_id,
            intaris_session_id="intaris_1",
            initialized=True,
            last_event_seq=3,
            last_compaction_seq=2,
            events=[
                SimpleNamespace(
                    seq=3,
                    type="user_message",
                    data={"content": "cached-after-compaction", "client_message_id": "cached"},
                )
            ],
        )
    )

    snapshot = _run(
        build_chat_snapshot(
            conversation=_conversation(),
            session_refs=_lineage(),
            event_store=store,
            cursor_secret=SECRET,
            session_cache=session_cache,
            now=NOW,
        )
    )

    assert [item.id for item in snapshot.timeline.items] == ["user:c1", "message:a1", "user:c2"]
    assert store.watermark_calls == ["intaris_1"]
    assert [call["session_id"] for call in store.calls] == ["intaris_1"]


def test_sync_reads_only_events_after_cursor_watermark() -> None:
    store = FakeEventStore(
        {
            "intaris_1": [
                _event(1, "tool_call", {"call_id": "call_1", "tool_name": "bash"}),
            ]
        }
    )
    snapshot = _run(
        build_chat_snapshot(
            conversation=_conversation(),
            session_refs=_lineage(),
            event_store=store,
            cursor_secret=SECRET,
            now=NOW,
        )
    )

    store.events_by_session["intaris_1"].append(
        _event(2, "tool_result", {"call_id": "call_1", "result": "done"})
    )
    response = _run(
        build_chat_sync_response(
            conversation_id="conv_1",
            cursor=snapshot.cursor,
            session_refs=_lineage(),
            event_store=store,
            cursor_secret=SECRET,
            now=NOW,
        )
    )

    assert response.reset_required is False
    assert response.has_more is False
    assert [op.op for op in response.ops] == ["upsert_item"]
    sync_calls = [call for call in store.calls if call["direction"] == "forward"]
    assert sync_calls == [
        {
            "session_id": "intaris_1",
            "after_seq": 1,
            "before_seq": None,
            "limit": 501,
            "direction": "forward",
        }
    ]
    op = response.ops[0]
    assert isinstance(op, UpsertTimelineItemOp)
    item = op.item
    assert item.id == "tool:call_1"
    assert isinstance(item, ToolCallTimelineItem)
    assert item.result_preview == "done"


def test_sync_hydrates_tool_result_attachment_refs_before_projection() -> None:
    store = FakeEventStore({"intaris_1": []})
    snapshot = _run(
        build_chat_snapshot(
            conversation=_conversation(),
            session_refs=_lineage(),
            event_store=store,
            cursor_secret=SECRET,
            now=NOW,
        )
    )
    store.events_by_session["intaris_1"] = [
        _event(1, "tool_call", {"call_id": "call_1", "tool_name": "artifact_read"}),
        _event(
            2,
            "tool_result",
            {
                "call_id": "call_1",
                "result": "attached",
                "attachments": [{"artifact_id": "art_tool"}],
            },
        ),
    ]

    response = _run(
        build_chat_sync_response(
            conversation_id="conv_1",
            cursor=snapshot.cursor,
            session_refs=_lineage(),
            event_store=store,
            cursor_secret=SECRET,
            event_post_processor=_attachment_processor,
            now=NOW,
        )
    )

    assert len(response.ops) == 1
    op = response.ops[0]
    assert isinstance(op, UpsertTimelineItemOp)
    item = op.item
    assert isinstance(item, ToolCallTimelineItem)
    assert item.attachments[0].artifact_id == "art_tool"
    assert item.attachments[0].filename == "hydrated-art_tool.txt"


def test_limited_sync_returns_range_reset_instead_of_partial_cursor() -> None:
    store = FakeEventStore({"intaris_1": []})
    snapshot = _run(
        build_chat_snapshot(
            conversation=_conversation(),
            session_refs=_lineage(),
            event_store=store,
            cursor_secret=SECRET,
            now=NOW,
        )
    )
    store.events_by_session["intaris_1"] = [
        _event(1, "user_message", {"content": "one", "client_message_id": "c1"}),
        _event(2, "user_message", {"content": "two", "client_message_id": "c2"}),
        _event(3, "user_message", {"content": "three", "client_message_id": "c3"}),
    ]

    response = _run(
        build_chat_sync_response(
            conversation_id="conv_1",
            cursor=snapshot.cursor,
            session_refs=_lineage(),
            event_store=store,
            cursor_secret=SECRET,
            limit=1,
            now=NOW,
        )
    )

    assert response.reset_required is True
    assert response.reset_reason == "range_too_large"
    assert response.cursor_after == snapshot.cursor
    assert response.ops == []


def test_limited_sync_with_merged_item_returns_range_reset() -> None:
    store = FakeEventStore({"intaris_1": []})
    snapshot = _run(
        build_chat_snapshot(
            conversation=_conversation(),
            session_refs=_lineage(),
            event_store=store,
            cursor_secret=SECRET,
            now=NOW,
        )
    )
    store.events_by_session["intaris_1"] = [
        _event(1, "tool_call", {"call_id": "call_1", "tool_name": "bash"}),
        _event(2, "user_message", {"content": "middle", "client_message_id": "c2"}),
        _event(3, "tool_result", {"call_id": "call_1", "result": "done"}),
    ]

    response = _run(
        build_chat_sync_response(
            conversation_id="conv_1",
            cursor=snapshot.cursor,
            session_refs=_lineage(),
            event_store=store,
            cursor_secret=SECRET,
            limit=1,
            now=NOW,
        )
    )

    assert response.reset_required is True
    assert response.reset_reason == "range_too_large"
    assert response.cursor_after == snapshot.cursor
    assert response.ops == []


def test_sync_returns_reset_when_event_store_page_is_partial() -> None:
    store = FakeEventStore(
        {
            "intaris_1": [
                _event(1, "user_message", {"content": "one", "client_message_id": "c1"}),
            ]
        },
        page_cap=1,
    )
    snapshot = _run(
        build_chat_snapshot(
            conversation=_conversation(),
            session_refs=_lineage(),
            event_store=store,
            cursor_secret=SECRET,
            now=NOW,
        )
    )
    store.calls.clear()
    store.events_by_session["intaris_1"].extend(
        [
            _event(2, "user_message", {"content": "two", "client_message_id": "c2"}),
            _event(3, "user_message", {"content": "three", "client_message_id": "c3"}),
        ]
    )

    response = _run(
        build_chat_sync_response(
            conversation_id="conv_1",
            cursor=snapshot.cursor,
            session_refs=_lineage(),
            event_store=store,
            cursor_secret=SECRET,
            limit=500,
            now=NOW,
        )
    )

    assert response.reset_required is True
    assert response.reset_reason == "range_too_large"
    assert response.cursor_after == snapshot.cursor
    assert response.ops == []
    assert store.calls == [
        {
            "session_id": "intaris_1",
            "after_seq": 1,
            "before_seq": None,
            "limit": 501,
            "direction": "forward",
        }
    ]


def test_snapshot_reads_bounded_latest_window_with_high_watermark_cursor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sync_module, "SNAPSHOT_WINDOW_EVENT_LIMIT", 2)
    store = FakeEventStore(
        {
            "intaris_1": [
                _event(1, "user_message", {"content": "one", "client_message_id": "c1"}),
                _event(2, "user_message", {"content": "two", "client_message_id": "c2"}),
                _event(3, "user_message", {"content": "three", "client_message_id": "c3"}),
            ]
        }
    )

    snapshot = _run(
        build_chat_snapshot(
            conversation=_conversation(),
            session_refs=_lineage(),
            event_store=store,
            cursor_secret=SECRET,
            now=NOW,
        )
    )

    assert [item.id for item in snapshot.timeline.items] == ["user:c2", "user:c3"]
    assert snapshot.timeline.has_more_before is True
    assert snapshot.timeline.before_cursor

    store.calls.clear()
    store.events_by_session["intaris_1"].append(
        _event(4, "user_message", {"content": "four", "client_message_id": "c4"})
    )
    response = _run(
        build_chat_sync_response(
            conversation_id="conv_1",
            cursor=snapshot.cursor,
            session_refs=_lineage(),
            event_store=store,
            cursor_secret=SECRET,
            now=NOW,
        )
    )

    assert [op.op for op in response.ops] == ["upsert_item"]
    assert store.calls == [
        {
            "session_id": "intaris_1",
            "after_seq": 3,
            "before_seq": None,
            "limit": 501,
            "direction": "forward",
        }
    ]


def test_sync_rejects_malformed_cursor_with_cursor_error() -> None:
    store = FakeEventStore({"intaris_1": []})

    with pytest.raises(ChatCursorError) as exc_info:
        _run(
            build_chat_sync_response(
                conversation_id="conv_1",
                cursor="not-valid",
                session_refs=_lineage(),
                event_store=store,
                cursor_secret=SECRET,
                now=NOW,
            )
        )

    assert exc_info.value.code == "cursor_invalid"


def test_sync_requires_reset_when_lineage_changes() -> None:
    store = FakeEventStore({"intaris_1": [_event(1, "user_message", {"content": "hello"})]})
    snapshot = _run(
        build_chat_snapshot(
            conversation=_conversation(),
            session_refs=_lineage(),
            event_store=store,
            cursor_secret=SECRET,
            now=NOW,
        )
    )

    response = _run(
        build_chat_sync_response(
            conversation_id="conv_1",
            cursor=snapshot.cursor,
            session_refs=[
                ConversationSessionRef(
                    session_id="sess_2",
                    event_store_session_id="intaris_2",
                    ordinal=0,
                )
            ],
            event_store=store,
            cursor_secret=SECRET,
            now=NOW,
        )
    )

    assert response.reset_required is True
    assert response.reset_reason == "lineage_changed"
    assert response.ops == []


def test_backfill_pages_before_opaque_source_cursor() -> None:
    store = FakeEventStore(
        {
            "intaris_1": [
                _event(1, "user_message", {"content": "one", "client_message_id": "c1"}),
                _event(2, "user_message", {"content": "two", "client_message_id": "c2"}),
                _event(3, "user_message", {"content": "three", "client_message_id": "c3"}),
            ]
        }
    )

    snapshot = _run(
        build_chat_snapshot(
            conversation=_conversation(),
            session_refs=_lineage(),
            event_store=store,
            cursor_secret=SECRET,
            now=NOW,
        )
    )
    before_cursor = _run(
        build_timeline_backfill_response(
            conversation_id="conv_1",
            before=None,
            session_refs=_lineage(),
            event_store=store,
            cursor_secret=SECRET,
            limit=1,
            now=NOW,
        )
    ).before_cursor
    assert snapshot.cursor
    assert before_cursor

    response = _run(
        build_timeline_backfill_response(
            conversation_id="conv_1",
            before=before_cursor,
            session_refs=_lineage(),
            event_store=store,
            cursor_secret=SECRET,
            limit=1,
            now=NOW,
        )
    )

    assert [item.id for item in response.items] == ["user:c2"]
    assert response.has_more_before is True
    assert response.before_cursor
    assert response.before_cursor != before_cursor


def test_backfill_hydrates_attachment_refs_before_projection() -> None:
    store = FakeEventStore(
        {
            "intaris_1": [
                _event(
                    1,
                    "assistant_message",
                    {
                        "content": "old attachment",
                        "message_id": "a1",
                        "attachments": [{"artifact_id": "art_old"}],
                    },
                )
            ]
        }
    )

    response = _run(
        build_timeline_backfill_response(
            conversation_id="conv_1",
            before=None,
            session_refs=_lineage(),
            event_store=store,
            cursor_secret=SECRET,
            event_post_processor=_attachment_processor,
            now=NOW,
        )
    )

    item = response.items[0]
    assert isinstance(item, MessageTimelineItem)
    assert item.attachments[0].artifact_id == "art_old"
    assert item.attachments[0].filename == "hydrated-art_old.txt"


def test_limit_validation() -> None:
    assert validate_sync_limit(1) == 1
    assert validate_backfill_limit(200) == 200
    with pytest.raises(ChatV2SyncError):
        validate_sync_limit(0)
    with pytest.raises(ChatV2SyncError):
        validate_backfill_limit(201)


def test_conversation_and_queue_conversion() -> None:
    row = type(
        "Row",
        (),
        {
            "conversation_id": "conv_1",
            "title": "Chat",
            "agent_id": "laforge",
            "agent_profile_id": None,
            "project_id": None,
            "status": "active",
            "active_session_id": "sess_1",
            "last_message_at": NOW,
            "last_read_at": None,
        },
    )()

    conversation = conversation_summary_from_row(row)
    queue = queue_state_from_messages(
        [
            {
                "queue_id": "q1",
                "client_message_id": "c1",
                "content": "queued",
                "attachments": [],
                "position": 1,
            }
        ]
    )

    assert conversation == _conversation(title="Chat")
    assert queue.queued_count == 1
    assert queue.messages[0].queue_id == "q1"


def test_sync_hydrates_tool_call_for_out_of_window_tool_result() -> None:
    """A tool_result whose tool_call fell in an earlier window keeps its anchor.

    Without hydration the upserted tool item re-anchors to the RESULT's seq
    (jumping past intervening items), loses its tool name (fallback "tool"),
    and drops its arguments.
    """
    store = FakeEventStore(
        {
            "intaris_1": [
                _event(
                    1,
                    "tool_call",
                    {"call_id": "call_1", "name": "bash", "arguments": {"command": "ls"}},
                ),
                _event(2, "assistant_message", {"content": "running", "message_id": "a1"}),
            ]
        }
    )
    snapshot = _run(
        build_chat_snapshot(
            conversation=_conversation(),
            session_refs=_lineage(),
            event_store=store,
            cursor_secret=SECRET,
            now=NOW,
        )
    )

    # tool_result lands in the NEXT window (different seq batch).
    store.events_by_session["intaris_1"].append(
        _event(3, "tool_result", {"call_id": "call_1", "name": "bash", "result": "file.txt"})
    )
    response = _run(
        build_chat_sync_response(
            conversation_id="conv_1",
            cursor=snapshot.cursor,
            session_refs=_lineage(),
            event_store=store,
            cursor_secret=SECRET,
            now=NOW,
        )
    )

    tool_ops = [
        op
        for op in response.ops
        if isinstance(op, UpsertTimelineItemOp) and op.item.id == "tool:call_1"
    ]
    assert len(tool_ops) == 1
    item = tool_ops[0].item
    assert isinstance(item, ToolCallTimelineItem)
    assert item.tool_name == "bash"
    assert item.arguments == {"command": "ls"}
    assert item.result_preview == "file.txt"
    # Anchored at the CALL's seq (1), not the result's seq (3).
    assert ":000000000000001:" in item.sort_key


def test_sync_folds_delegation_onto_hydrated_tool_call() -> None:
    """A delegation event in a later window folds onto its hydrated delegate call."""
    store = FakeEventStore(
        {
            "intaris_1": [
                _event(1, "tool_call", {"call_id": "call_d", "name": "delegate"}),
            ]
        }
    )
    snapshot = _run(
        build_chat_snapshot(
            conversation=_conversation(),
            session_refs=_lineage(),
            event_store=store,
            cursor_secret=SECRET,
            now=NOW,
        )
    )

    store.events_by_session["intaris_1"].append(
        _event(
            2,
            "delegation",
            {
                "status": "completed",
                "mode": "delegate",
                "call_id": "call_d",
                "child_session_id": "child_1",
                "result_summary": "Done.",
            },
        )
    )
    response = _run(
        build_chat_sync_response(
            conversation_id="conv_1",
            cursor=snapshot.cursor,
            session_refs=_lineage(),
            event_store=store,
            cursor_secret=SECRET,
            now=NOW,
        )
    )

    upserts = [op for op in response.ops if isinstance(op, UpsertTimelineItemOp)]
    # No standalone delegation card — only the folded tool item.
    assert [op.item.id for op in upserts] == ["tool:call_d"]
    item = upserts[0].item
    assert isinstance(item, ToolCallTimelineItem)
    assert item.delegation is not None
    assert item.delegation["child_session_id"] == "child_1"
    assert item.delegation["status"] == "completed"


def test_sync_attaches_evaluation_sidecar_to_hydrated_tool_call() -> None:
    store = FakeEventStore(
        {
            "intaris_1": [
                _event(1, "tool_call", {"call_id": "call_e", "name": "bash"}),
            ]
        }
    )
    snapshot = _run(
        build_chat_snapshot(
            conversation=_conversation(),
            session_refs=_lineage(),
            event_store=store,
            cursor_secret=SECRET,
            now=NOW,
        )
    )

    store.events_by_session["intaris_1"].append(
        _event(2, "evaluation", {"tool_call_id": "call_e", "decision": "allow"})
    )
    response = _run(
        build_chat_sync_response(
            conversation_id="conv_1",
            cursor=snapshot.cursor,
            session_refs=_lineage(),
            event_store=store,
            cursor_secret=SECRET,
            now=NOW,
        )
    )

    upserts = [op for op in response.ops if isinstance(op, UpsertTimelineItemOp)]
    tool_items = [op.item for op in upserts if op.item.id == "tool:call_e"]
    assert len(tool_items) == 1
    assert isinstance(tool_items[0], ToolCallTimelineItem)
    assert tool_items[0].evaluation is not None
    assert tool_items[0].evaluation["decision"] == "allow"


def _conversation(title: str | None = None) -> ConversationSummary:
    return ConversationSummary(
        conversation_id="conv_1",
        title=title,
        agent_id="laforge",
        active_session_id="sess_1",
        last_message_at=NOW.isoformat() if title else None,
    )


def _state() -> ConversationStateView:
    return ConversationStateView(
        state_version=1,
        snapshot_generated_at=NOW.isoformat(),
        capabilities=[],
        active_turn={},
        pending={},
        active_session={},
    )


def _lineage() -> list[ConversationSessionRef]:
    return [
        ConversationSessionRef(
            session_id="sess_1",
            event_store_session_id="intaris_1",
            ordinal=0,
        )
    ]


def _lineage_with_compacted_parent() -> list[ConversationSessionRef]:
    return [
        ConversationSessionRef(
            session_id="sess_old",
            event_store_session_id="intaris_old",
            ordinal=0,
            status="completed",
            completion_reason="compacted",
        ),
        ConversationSessionRef(
            session_id="sess_1",
            event_store_session_id="intaris_1",
            ordinal=1,
            status="active",
        ),
    ]


def _many_lineage(count: int) -> list[ConversationSessionRef]:
    return [
        ConversationSessionRef(
            session_id=f"sess_{ordinal}",
            event_store_session_id=f"intaris_{ordinal}",
            ordinal=ordinal,
        )
        for ordinal in range(count)
    ]


async def _read_latest_window_eager(
    session_refs: list[ConversationSessionRef],
    event_store: FakeEventStore,
    limit: int,
) -> Any:
    pages = await asyncio.gather(
        *[
            event_store.read_session_events(
                session_id=ref.event_store_session_id,
                limit=limit,
                direction="backward",
            )
            for ref in session_refs
        ]
    )
    raw_events: list[RawSessionEvent] = []
    remaining = limit
    has_more_before = False
    for lineage_index in range(len(session_refs) - 1, -1, -1):
        if remaining <= 0:
            has_more_before = lineage_index >= 0
            break
        ref = session_refs[lineage_index]
        page = pages[lineage_index]
        tagged = sync_module._tag_events(page.events[-remaining:], ref)  # noqa: SLF001
        raw_events.extend(tagged)
        remaining -= len(tagged)
        if page.has_more_before:
            has_more_before = True
            break
        if remaining <= 0 and lineage_index > 0:
            has_more_before = True
    return sync_module._EventWindow(  # noqa: SLF001
        events=sync_module._sort_raw_events(raw_events),  # noqa: SLF001
        has_more_before=has_more_before,
    )


def _event(seq: int, event_type: str, data: dict[str, object]) -> RawSessionEvent:
    return _event_for("intaris_1", seq, event_type, data)


def test_parallel_backfill_pages_by_global_event_time_without_skip_or_duplicate() -> None:
    refs = [
        ConversationSessionRef(
            session_id="root",
            event_store_session_id="root-store",
            ordinal=0,
        ),
        ConversationSessionRef(
            session_id="late-child",
            event_store_session_id="child-store",
            ordinal=1,
        ),
    ]
    base = datetime(2026, 1, 1, tzinfo=UTC)
    root_events = [
        _event_for(
            "root-store",
            seq,
            "user_message",
            {"content": f"root-{seq}", "client_message_id": f"root-{seq}"},
        ).model_copy(update={"timestamp": base + timedelta(seconds=second)})
        for seq, second in enumerate((1, 2, 5, 6), start=1)
    ]
    child_events = [
        _event_for(
            "child-store",
            seq,
            "user_message",
            {"content": f"child-{seq}", "client_message_id": f"child-{seq}"},
        ).model_copy(update={"timestamp": base + timedelta(seconds=second)})
        for seq, second in enumerate((3, 4), start=1)
    ]
    store = FakeEventStore(
        {"root-store": root_events, "child-store": child_events},
        read_delay=0.001,
    )
    cursor: str | None = None
    seen: list[str] = []
    for _ in range(3):
        response = _run(
            build_timeline_backfill_response(
                before=cursor,
                session_refs=refs,
                event_store=store,
                cursor_secret=SECRET,
                limit=2,
                now=NOW,
            )
        )
        seen.extend(item.id for item in response.items)
        cursor = response.before_cursor
    assert seen == [
        "user:root-3",
        "user:root-4",
        "user:child-1",
        "user:child-2",
        "user:root-1",
        "user:root-2",
    ]
    assert len(seen) == len(set(seen))
    assert store.max_concurrent_reads == 2


def test_composite_work_cursor_is_compact_at_128_streams_and_rejects_graph_change() -> None:
    refs = [
        ConversationSessionRef(
            session_id=f"session-{index}",
            event_store_session_id=f"store-{index}",
            ordinal=index,
        )
        for index in range(128)
    ]
    store = FakeEventStore(
        {
            ref.event_store_session_id: [
                _event_for(
                    ref.event_store_session_id,
                    1,
                    "user_message",
                    {
                        "content": str(ref.ordinal),
                        "client_message_id": f"message-{ref.ordinal}",
                    },
                )
            ]
            for ref in refs
        }
    )
    first = _run(
        build_timeline_backfill_response(
            before=None,
            session_refs=refs,
            event_store=store,
            cursor_secret=SECRET,
            limit=10,
            now=NOW,
        )
    )
    assert first.before_cursor is not None
    assert len(first.before_cursor.encode()) <= 8 * 1024
    with pytest.raises(ChatV2SyncError, match="fingerprint"):
        _run(
            build_timeline_backfill_response(
                before=first.before_cursor,
                session_refs=refs[:-1],
                event_store=store,
                cursor_secret=SECRET,
                limit=10,
                now=NOW,
            )
        )


def _event_for(
    session_id: str,
    seq: int,
    event_type: str,
    data: dict[str, object],
) -> RawSessionEvent:
    return RawSessionEvent(
        store_id="intaris",
        session_id=session_id,
        seq=seq,
        type=event_type,
        data=data,
    )


async def _attachment_processor(events: list[RawSessionEvent]) -> list[RawSessionEvent]:
    processed: list[RawSessionEvent] = []
    for event in events:
        assert event.data.get("cognis_session_id") == "sess_1"
        attachments = event.data.get("attachments")
        if not isinstance(attachments, list):
            processed.append(event)
            continue
        hydrated = [
            {
                **attachment,
                "kind": "file",
                "mime_type": "text/plain",
                "filename": f"hydrated-{attachment['artifact_id']}.txt",
                "size_bytes": 12,
                "url": f"https://artifacts.test/{attachment['artifact_id']}",
            }
            for attachment in attachments
            if isinstance(attachment, dict) and isinstance(attachment.get("artifact_id"), str)
        ]
        processed.append(event.model_copy(update={"data": {**event.data, "attachments": hydrated}}))
    return processed


def _run(coro: Coroutine[Any, Any, Any]) -> Any:
    return asyncio.run(coro)
