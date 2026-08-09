from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from cognis.api.chat_v2.cursors import decode_cursor
from cognis.api.chat_v2.event_store import RawSessionEvent, SessionEventPage, SessionWatermark
from cognis.api.chat_v2.schemas import TimelineScope
from cognis.api.chat_v2.sync import ChatV2SyncError, ConversationSessionRef
from cognis.api.chat_v2.work_graph import WORK_GRAPH_MAX_NODES
from cognis.api.chat_v2.work_projection import is_work_evidence_item
from cognis.api.chat_v2.work_sync import (
    WORK_UPSTREAM_MAX_CONCURRENCY,
    _encode_frontiers,
    _establish_stream_heads,
    _StreamScan,
    build_work_evidence_backfill_response,
    collect_initial_work_frontiers,
)
from cognis.models.tool import ToolDefinition, ToolSource


class _Store:
    store_id = "intaris"

    def __init__(self, events: list[RawSessionEvent]) -> None:
        self.events = events
        self.event_reads = 0
        self.watermark_reads = 0

    async def read_session_events(
        self,
        *,
        session_id: str,
        after_seq: int | None = None,
        before_seq: int | None = None,
        limit: int = 500,
        direction: str = "forward",
    ) -> SessionEventPage:
        del after_seq, direction
        self.event_reads += 1
        eligible = [
            event
            for event in self.events
            if event.session_id == session_id and (before_seq is None or event.seq < before_seq)
        ]
        selected = eligible[-limit:]
        return SessionEventPage(
            store_id=self.store_id,
            session_id=session_id,
            events=selected,
            first_seq=selected[0].seq if selected else None,
            last_seq=selected[-1].seq if selected else None,
            has_more_before=len(eligible) > len(selected),
            verified_empty=not selected,
        )

    async def read_session_high_watermark(self, *, session_id: str) -> SessionWatermark:
        self.watermark_reads += 1
        return SessionWatermark(
            store_id=self.store_id,
            session_id=session_id,
            last_seq=max((event.seq for event in self.events), default=0),
        )


class _ConcurrencyStore(_Store):
    def __init__(self, events: list[RawSessionEvent]) -> None:
        super().__init__(events)
        self.active_reads = 0
        self.max_active_reads = 0

    async def _enter(self) -> None:
        self.active_reads += 1
        self.max_active_reads = max(self.max_active_reads, self.active_reads)
        await asyncio.sleep(0.01)

    async def read_session_events(self, **kwargs) -> SessionEventPage:
        await self._enter()
        try:
            return await super().read_session_events(**kwargs)
        finally:
            self.active_reads -= 1

    async def read_session_high_watermark(self, *, session_id: str) -> SessionWatermark:
        await self._enter()
        try:
            return await super().read_session_high_watermark(session_id=session_id)
        finally:
            self.active_reads -= 1


@pytest.mark.asyncio
async def test_work_hydration_bounds_intaris_concurrency() -> None:
    refs = [
        ConversationSessionRef(
            session_id=f"session-{index}",
            event_store_session_id=f"stream-{index}",
            role="delegate",
            ordinal=index,
            status="completed",
        )
        for index in range(20)
    ]
    store = _ConcurrencyStore([])

    frontiers, truncated = await collect_initial_work_frontiers(
        session_refs=refs,
        event_store=store,
        deadline=asyncio.get_running_loop().time() + 2,
    )

    assert truncated is False
    assert frontiers == [0] * len(refs)
    assert store.max_active_reads <= WORK_UPSTREAM_MAX_CONCURRENCY

    store.max_active_reads = 0
    streams = [
        _StreamScan(ref=ref, reader=store, frontier=0, request_start_frontier=0) for ref in refs
    ]
    await _establish_stream_heads(
        streams,
        deadline=asyncio.get_running_loop().time() + 2,
    )
    assert store.max_active_reads <= WORK_UPSTREAM_MAX_CONCURRENCY
    assert store.event_reads == 0


def test_maximum_graph_cursor_fits_common_proxy_request_line_limit() -> None:
    refs = [
        ConversationSessionRef(
            session_id=f"session-{index}",
            event_store_session_id=f"stream-{index}",
            role="delegate",
            ordinal=index,
            status="completed",
        )
        for index in range(WORK_GRAPH_MAX_NODES)
    ]

    cursor = _encode_frontiers(
        scope=TimelineScope(
            key="conversation:root",
            kind="conversation",
            conversation_id="root",
        ),
        session_refs=refs,
        frontiers=[2**63 - 1] * len(refs),
        cursor_secret="cursor-secret",
        now=datetime(2026, 1, 1, tzinfo=UTC),
        graph_fingerprint="f" * 64,
    )

    assert len(cursor) < 8_192


def _event(seq: int, event_type: str, data: dict[str, object]) -> RawSessionEvent:
    return RawSessionEvent(
        store_id="intaris",
        session_id="stream-1",
        seq=seq,
        type=event_type,
        data=data,
        timestamp=datetime(2026, 1, 1, tzinfo=UTC) + timedelta(seconds=seq),
    )


def _deliverable(seq: int) -> RawSessionEvent:
    return _event(
        seq,
        "lifecycle",
        {
            "event": "assistant_deliverable",
            "deliverable_id": f"dlv-{seq}",
            "format": "markdown",
            "title": f"Deliverable {seq}",
        },
    )


@pytest.mark.asyncio
async def test_initial_page_scans_non_work_tail_and_pages_only_evidence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("cognis.api.chat_v2.work_sync.WORK_SCAN_CHUNK_SIZE", 2)
    events = [
        _event(
            1,
            "tool_call",
            {
                "call_id": "call-old",
                "name": "send_gmail_message",
                "arguments": {"recipient": "old@example.com"},
                "turn_id": "turn-old",
            },
        ),
        _event(
            2,
            "tool_result",
            {
                "call_id": "call-old",
                "name": "send_gmail_message",
                "result": "sent",
                "turn_id": "turn-old",
            },
        ),
        _event(
            3,
            "tool_call",
            {
                "call_id": "call-new",
                "name": "send_gmail_message",
                "arguments": {"recipient": "new@example.com"},
                "turn_id": "turn-new",
            },
        ),
        _event(
            4,
            "tool_result",
            {
                "call_id": "call-new",
                "name": "send_gmail_message",
                "result": "sent",
                "turn_id": "turn-new",
            },
        ),
        *[
            _event(seq, "user_message", {"content": f"non-work-{seq}", "turn_id": f"t-{seq}"})
            for seq in range(5, 305)
        ],
    ]
    # Timestamps are intentionally inverted across pages. Sequence is canonical
    # within the stream and must prevent duplicate tool evidence.
    events[0] = events[0].model_copy(update={"timestamp": datetime(2026, 1, 2, tzinfo=UTC)})
    events[2] = events[2].model_copy(update={"timestamp": datetime(2025, 12, 31, tzinfo=UTC)})
    store = _Store(events)
    definitions = {
        "send_gmail_message": ToolDefinition(
            name="send_gmail_message",
            description="send",
            source=ToolSource(
                type="mcp",
                server_id="server-1",
                raw_tool_name="send_gmail_message",
            ),
            read_only=False,
            category="filesystem",
        )
    }
    kwargs = {
        "scope": TimelineScope(
            key="conversation:conv-1",
            kind="conversation",
            conversation_id="conv-1",
        ),
        "session_refs": [
            ConversationSessionRef(
                session_id="session-1",
                event_store_session_id="stream-1",
                ordinal=0,
            )
        ],
        "event_store": store,
        "cursor_secret": "work-sync-secret",
        "evidence_predicate": lambda item: is_work_evidence_item(item, definitions),
        "limit": 1,
        "graph_fingerprint": "authorized-graph-fingerprint",
    }

    first = await build_work_evidence_backfill_response(
        before=None,
        max_pages=200,
        **kwargs,
    )
    assert [item.id for item in first.items] == ["tool:call-new"]
    assert first.has_more_before is True
    assert first.before_cursor
    assert (
        decode_cursor(first.before_cursor, "work-sync-secret").graph_fingerprint
        == "authorized-graph-fingerprint"
    )

    second = await build_work_evidence_backfill_response(
        before=first.before_cursor,
        max_pages=200,
        **kwargs,
    )
    assert [item.id for item in second.items] == ["tool:call-old"]
    assert second.has_more_before is False
    assert second.before_cursor is None


@pytest.mark.asyncio
async def test_hydrated_call_source_does_not_skip_or_duplicate_intermediate_evidence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("cognis.api.chat_v2.work_sync.WORK_SCAN_CHUNK_SIZE", 5)
    events = [
        _event(
            1,
            "tool_call",
            {
                "call_id": "call-paired",
                "name": "send_gmail_message",
                "arguments": {"recipient": "user@example.com"},
                "turn_id": "turn-paired",
            },
        ),
        *[_deliverable(seq) for seq in range(2, 10)],
        _event(
            10,
            "tool_result",
            {
                "call_id": "call-paired",
                "name": "send_gmail_message",
                "result": "sent",
                "turn_id": "turn-paired",
            },
        ),
    ]
    definition = ToolDefinition(
        name="send_gmail_message",
        description="send",
        source=ToolSource(
            type="mcp",
            server_id="server-1",
            raw_tool_name="send_gmail_message",
        ),
        read_only=False,
        category="external",
    )
    kwargs = {
        "scope": TimelineScope(
            key="conversation:conv-1",
            kind="conversation",
            conversation_id="conv-1",
        ),
        "session_refs": [
            ConversationSessionRef(
                session_id="session-1",
                event_store_session_id="stream-1",
                ordinal=0,
            )
        ],
        "event_store": _Store(events),
        "cursor_secret": "work-sync-secret",
        "evidence_predicate": lambda item: is_work_evidence_item(
            item, {"send_gmail_message": definition}
        ),
        "limit": 2,
        "graph_fingerprint": "graph-a",
    }

    pages = []
    cursor = None
    for _ in range(10):
        page = await build_work_evidence_backfill_response(before=cursor, **kwargs)
        pages.append(page)
        cursor = page.before_cursor
        if cursor is None:
            break
    else:
        pytest.fail("paired evidence pagination did not reach exhaustion")
    items = [item for page in pages for item in page.items]
    ids = [item.id for item in items]
    completed = next(item for item in items if item.id == "tool:call-paired")

    assert ids.count("tool:call-paired") == 1
    assert completed.status == "complete"
    assert set(ids) == {
        "tool:call-paired",
        *{f"assistant-deliverable:dlv-{seq}" for seq in range(2, 10)},
    }
    assert len(ids) == len(set(ids))
    assert pages[-1].has_more_before is False


@pytest.mark.asyncio
async def test_many_pairs_remain_exact_and_cursors_stay_bounded() -> None:
    pair_count = 80
    events: list[RawSessionEvent] = []
    for index in range(pair_count):
        call_seq = index * 3 + 1
        deliverable_seq = call_seq + 1
        result_seq = call_seq + 2
        call_id = f"call-{index:03d}"
        events.extend(
            [
                _event(
                    call_seq,
                    "tool_call",
                    {
                        "call_id": call_id,
                        "name": "send_gmail_message",
                        "arguments": {"recipient": f"user-{index}@example.com"},
                        "turn_id": f"turn-{index}",
                    },
                ),
                _deliverable(deliverable_seq),
                _event(
                    result_seq,
                    "tool_result",
                    {
                        "call_id": call_id,
                        "name": "send_gmail_message",
                        "result": "sent",
                        "turn_id": f"turn-{index}",
                    },
                ),
            ]
        )
    definition = ToolDefinition(
        name="send_gmail_message",
        description="send",
        source=ToolSource(
            type="mcp",
            server_id="server-1",
            raw_tool_name="send_gmail_message",
        ),
        read_only=False,
        category="external",
    )
    kwargs = {
        "scope": TimelineScope(
            key="conversation:conv-1",
            kind="conversation",
            conversation_id="conv-1",
        ),
        "session_refs": [
            ConversationSessionRef(
                session_id="session-1",
                event_store_session_id="stream-1",
                ordinal=0,
            )
        ],
        "event_store": _Store(events),
        "cursor_secret": "work-sync-secret",
        "evidence_predicate": lambda item: is_work_evidence_item(
            item, {"send_gmail_message": definition}
        ),
        "limit": 9,
        "graph_fingerprint": "graph-a",
    }

    items = []
    cursor = None
    cursor_sizes: list[int] = []
    for _ in range(50):
        page = await build_work_evidence_backfill_response(before=cursor, **kwargs)
        items.extend(page.items)
        cursor = page.before_cursor
        if cursor is None:
            break
        cursor_sizes.append(len(cursor.encode()))
    else:
        pytest.fail("many-pair pagination did not reach exhaustion")

    tool_items = [item for item in items if item.id.startswith("tool:")]
    assert len(tool_items) == pair_count
    assert {item.id for item in tool_items} == {
        f"tool:call-{index:03d}" for index in range(pair_count)
    }
    assert all(item.status == "complete" for item in tool_items)
    assert len(items) == pair_count * 2
    assert len({item.id for item in items}) == len(items)
    assert cursor_sizes
    assert max(cursor_sizes) <= 8 * 1024


@pytest.mark.asyncio
async def test_head_discovery_covers_all_streams_before_page_budgeted_refills() -> None:
    store = _Store([])
    refs = [
        ConversationSessionRef(
            session_id=f"session-{index}",
            event_store_session_id=f"stream-{index}",
            ordinal=index,
        )
        for index in range(128)
    ]

    response = await build_work_evidence_backfill_response(
        scope=TimelineScope(
            key="conversation:conv-1",
            kind="conversation",
            conversation_id="conv-1",
        ),
        before=None,
        session_refs=refs,
        event_store=store,
        cursor_secret="work-sync-secret",
        evidence_predicate=lambda item: False,
        limit=10,
        graph_fingerprint="graph-a",
        initial_frontiers=[1] * len(refs),
        max_pages=1,
    )

    assert store.event_reads == 128
    assert response.has_more_before is False
    assert response.before_cursor is None


@pytest.mark.asyncio
async def test_historical_rotation_stream_contributes_evidence() -> None:
    current = _Store([])
    historical = _Store(
        [
            _event(
                1,
                "lifecycle",
                {
                    "event": "assistant_deliverable",
                    "deliverable_id": "dlv-historical",
                    "format": "markdown",
                    "title": "Historical",
                },
            ).model_copy(update={"session_id": "stream-old"})
        ]
    )
    refs = [
        ConversationSessionRef(
            session_id="session-current",
            event_store_session_id="stream-current",
            ordinal=0,
            reader=current,
        ),
        ConversationSessionRef(
            session_id="session-old",
            event_store_session_id="stream-old",
            ordinal=1,
            role="rotation",
            reader=historical,
        ),
    ]

    response = await build_work_evidence_backfill_response(
        scope=TimelineScope(
            key="conversation:conv-1",
            kind="conversation",
            conversation_id="conv-1",
        ),
        before=None,
        session_refs=refs,
        event_store=current,
        cursor_secret="work-sync-secret",
        evidence_predicate=lambda item: is_work_evidence_item(item, {}),
        limit=10,
        graph_fingerprint="graph-a",
    )

    assert [item.id for item in response.items] == ["assistant-deliverable:dlv-historical"]


@pytest.mark.asyncio
async def test_sparse_empty_scan_returns_signed_resumable_progress() -> None:
    events = [
        _event(seq, "user_message", {"content": f"non-work-{seq}", "turn_id": f"t-{seq}"})
        for seq in range(1, 21)
    ]
    scope = TimelineScope(
        key="conversation:conv-1",
        kind="conversation",
        conversation_id="conv-1",
    )
    kwargs = {
        "scope": scope,
        "session_refs": [
            ConversationSessionRef(
                session_id="session-1",
                event_store_session_id="stream-1",
                ordinal=0,
            )
        ],
        "event_store": _Store(events),
        "cursor_secret": "work-sync-secret",
        "evidence_predicate": lambda item: False,
        "limit": 10,
        "graph_fingerprint": "graph-a",
    }

    first = await build_work_evidence_backfill_response(
        before=None,
        max_events=5,
        **kwargs,
    )
    assert first.items == []
    assert first.has_more_before is True
    assert first.before_cursor
    assert decode_cursor(first.before_cursor, "work-sync-secret").ordinal_frontiers == [15]

    cursor = first.before_cursor
    for _ in range(4):
        page = await build_work_evidence_backfill_response(
            before=cursor,
            max_events=5,
            **kwargs,
        )
        cursor = page.before_cursor
        if cursor is None:
            assert page.has_more_before is False
            break
    else:
        pytest.fail("bounded sparse scan did not reach canonical exhaustion")


@pytest.mark.asyncio
async def test_partial_scan_returns_evidence_and_resumable_cursor() -> None:
    events = [
        _event(
            1,
            "tool_call",
            {
                "call_id": "call-1",
                "name": "send_gmail_message",
                "arguments": {"recipient": "user@example.com"},
                "turn_id": "turn-1",
            },
        ),
        _event(
            2,
            "tool_result",
            {
                "call_id": "call-1",
                "name": "send_gmail_message",
                "result": "sent",
                "turn_id": "turn-1",
            },
        ),
        *[
            _event(seq, "user_message", {"content": f"gap-{seq}", "turn_id": f"t-{seq}"})
            for seq in range(3, 12)
        ],
    ]
    definition = ToolDefinition(
        name="send_gmail_message",
        description="send",
        source=ToolSource(
            type="mcp",
            server_id="server-1",
            raw_tool_name="send_gmail_message",
        ),
        read_only=False,
        category="external",
    )
    response = await build_work_evidence_backfill_response(
        scope=TimelineScope(
            key="conversation:conv-1",
            kind="conversation",
            conversation_id="conv-1",
        ),
        before=None,
        session_refs=[
            ConversationSessionRef(
                session_id="session-1",
                event_store_session_id="stream-1",
                ordinal=0,
            )
        ],
        event_store=_Store(events),
        cursor_secret="work-sync-secret",
        evidence_predicate=lambda item: is_work_evidence_item(
            item, {"send_gmail_message": definition}
        ),
        limit=10,
        graph_fingerprint="graph-a",
        max_events=11,
    )
    assert [item.id for item in response.items] == ["tool:call-1"]
    assert response.has_more_before is False
    assert response.before_cursor is None


@pytest.mark.asyncio
async def test_cancelled_scan_propagates_cancellation() -> None:
    entered = asyncio.Event()

    async def block(events: list[RawSessionEvent]) -> list[RawSessionEvent]:
        entered.set()
        await asyncio.Event().wait()
        return events

    task = asyncio.create_task(
        build_work_evidence_backfill_response(
            scope=TimelineScope(
                key="conversation:conv-1",
                kind="conversation",
                conversation_id="conv-1",
            ),
            before=None,
            session_refs=[
                ConversationSessionRef(
                    session_id="session-1",
                    event_store_session_id="stream-1",
                    ordinal=0,
                )
            ],
            event_store=_Store(
                [_event(1, "user_message", {"content": "wait", "turn_id": "turn-1"})]
            ),
            cursor_secret="work-sync-secret",
            evidence_predicate=lambda item: False,
            event_post_processor=block,
            limit=10,
            graph_fingerprint="graph-a",
        )
    )
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.asyncio
async def test_wall_timeout_without_progress_returns_retryable_error() -> None:
    async def block(events: list[RawSessionEvent]) -> list[RawSessionEvent]:
        await asyncio.sleep(1)
        return events

    with pytest.raises(ChatV2SyncError, match="before it made progress") as raised:
        await build_work_evidence_backfill_response(
            scope=TimelineScope(
                key="conversation:conv-1",
                kind="conversation",
                conversation_id="conv-1",
            ),
            before=None,
            session_refs=[
                ConversationSessionRef(
                    session_id="session-1",
                    event_store_session_id="stream-1",
                    ordinal=0,
                )
            ],
            event_store=_Store(
                [_event(1, "user_message", {"content": "wait", "turn_id": "turn-1"})]
            ),
            cursor_secret="work-sync-secret",
            evidence_predicate=lambda item: False,
            event_post_processor=block,
            limit=10,
            graph_fingerprint="graph-a",
            max_seconds=0.01,
        )
    assert raised.value.code == "work_scan_timeout"


@pytest.mark.asyncio
async def test_initial_watermark_cancellation_propagates() -> None:
    entered = asyncio.Event()

    class _BlockingWatermarkStore(_Store):
        async def read_session_high_watermark(self, *, session_id: str) -> SessionWatermark:
            entered.set()
            await asyncio.Event().wait()
            raise AssertionError("unreachable")

    store = _BlockingWatermarkStore([])
    task = asyncio.create_task(
        build_work_evidence_backfill_response(
            scope=TimelineScope(
                key="conversation:conv-1",
                kind="conversation",
                conversation_id="conv-1",
            ),
            before=None,
            session_refs=[
                ConversationSessionRef(
                    session_id="session-1",
                    event_store_session_id="stream-1",
                    ordinal=0,
                )
            ],
            event_store=store,
            cursor_secret="work-sync-secret",
            evidence_predicate=lambda item: False,
            limit=10,
            graph_fingerprint="graph-a",
        )
    )
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert store.event_reads == 0


@pytest.mark.asyncio
async def test_initial_watermark_timeout_without_scan_budget_returns_error() -> None:
    class _SlowWatermarkStore(_Store):
        async def read_session_high_watermark(self, *, session_id: str) -> SessionWatermark:
            await asyncio.sleep(1)
            raise AssertionError("unreachable")

    store = _SlowWatermarkStore([])
    started = asyncio.get_running_loop().time()
    with pytest.raises(ChatV2SyncError) as raised:
        await build_work_evidence_backfill_response(
            scope=TimelineScope(
                key="conversation:conv-1",
                kind="conversation",
                conversation_id="conv-1",
            ),
            before=None,
            session_refs=[
                ConversationSessionRef(
                    session_id="session-1",
                    event_store_session_id="stream-1",
                    ordinal=0,
                )
            ],
            event_store=store,
            cursor_secret="work-sync-secret",
            evidence_predicate=lambda item: False,
            limit=10,
            graph_fingerprint="graph-a",
            max_seconds=0.01,
        )
    assert raised.value.code == "work_watermark_timeout"
    assert store.event_reads == 0
    assert asyncio.get_running_loop().time() - started < 0.2


@pytest.mark.asyncio
async def test_179_stream_merge_reads_every_head_and_returns_last_ordinal_newest() -> None:
    refs: list[ConversationSessionRef] = []
    stores: list[_Store] = []
    for index in range(179):
        stream_id = f"stream-{index:03d}"
        events: list[RawSessionEvent] = []
        if index == 0:
            events = [
                _deliverable(1).model_copy(
                    update={
                        "session_id": stream_id,
                        "timestamp": datetime(2026, 1, 1, tzinfo=UTC),
                    }
                )
            ]
        elif index == 178:
            events = [
                _event(
                    1,
                    "tool_call",
                    {
                        "call_id": "call-newest",
                        "name": "send_gmail_message",
                        "arguments": {"recipient": "newest@example.com"},
                        "turn_id": "turn-newest",
                    },
                ).model_copy(
                    update={
                        "session_id": stream_id,
                        "timestamp": datetime(2026, 1, 2, tzinfo=UTC),
                    }
                ),
                _event(
                    2,
                    "tool_result",
                    {
                        "call_id": "call-newest",
                        "name": "send_gmail_message",
                        "result": "sent",
                        "turn_id": "turn-newest",
                    },
                ).model_copy(
                    update={
                        "session_id": stream_id,
                        "timestamp": datetime(2026, 1, 2, tzinfo=UTC),
                    }
                ),
            ]
        store = _Store(events)
        stores.append(store)
        refs.append(
            ConversationSessionRef(
                session_id=f"session-{index:03d}",
                event_store_session_id=stream_id,
                ordinal=index,
                reader=store,
            )
        )
    definition = ToolDefinition(
        name="send_gmail_message",
        description="send",
        source=ToolSource(
            type="mcp",
            server_id="server-1",
            raw_tool_name="send_gmail_message",
        ),
        read_only=False,
        category="external",
    )

    response = await build_work_evidence_backfill_response(
        scope=TimelineScope(
            key="conversation:conv-1",
            kind="conversation",
            conversation_id="conv-1",
        ),
        before=None,
        session_refs=refs,
        event_store=stores[0],
        cursor_secret="work-sync-secret",
        evidence_predicate=lambda item: is_work_evidence_item(
            item, {"send_gmail_message": definition}
        ),
        limit=1,
        graph_fingerprint="graph-a",
        initial_frontiers=[3 if index == 178 else 2 for index in range(179)],
        max_pages=32,
    )

    assert [item.id for item in response.items] == ["tool:call-newest"]
    assert response.items[0].status == "complete"
    assert sum(store.event_reads for store in stores) >= 179


@pytest.mark.asyncio
async def test_stream_head_timeout_is_explicit_and_bounded() -> None:
    class _BlockingHeadStore(_Store):
        async def read_session_events(self, **kwargs: object) -> SessionEventPage:
            await asyncio.Event().wait()
            raise AssertionError("unreachable")

    started = asyncio.get_running_loop().time()
    with pytest.raises(ChatV2SyncError) as raised:
        await build_work_evidence_backfill_response(
            scope=TimelineScope(
                key="conversation:conv-1",
                kind="conversation",
                conversation_id="conv-1",
            ),
            before=None,
            session_refs=[
                ConversationSessionRef(
                    session_id="session-1",
                    event_store_session_id="stream-1",
                    ordinal=0,
                )
            ],
            event_store=_BlockingHeadStore([]),
            cursor_secret="work-sync-secret",
            evidence_predicate=lambda item: False,
            limit=10,
            graph_fingerprint="graph-a",
            initial_frontiers=[1],
            max_seconds=0.02,
        )
    elapsed = asyncio.get_running_loop().time() - started
    assert raised.value.code == "work_scan_timeout"
    assert elapsed < 0.2
