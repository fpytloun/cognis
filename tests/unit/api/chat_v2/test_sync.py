"""Tests for Chat v2 snapshot/sync/backfill orchestration."""

from __future__ import annotations

import asyncio
from collections.abc import Coroutine
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any, Literal

import pytest

from cognis.api.chat_v2 import sync as sync_module
from cognis.api.chat_v2.cursors import ChatCursorError
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
    assert store.max_concurrent_reads == 2


def test_snapshot_reuses_immutable_session_reads_and_projection_cache() -> None:
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
    assert store.watermark_calls == []
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
    assert store.watermark_calls == ["intaris_1"]
    assert [call["session_id"] for call in store.calls] == ["intaris_1"]


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


def test_active_session_snapshot_reads_from_warm_session_cache() -> None:
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

    assert [item.id for item in snapshot.timeline.items] == ["user:cached"]
    assert store.watermark_calls == []
    assert store.calls == []


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


def _event(seq: int, event_type: str, data: dict[str, object]) -> RawSessionEvent:
    return _event_for("intaris_1", seq, event_type, data)


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
