from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

from cognis.api.chat_v2 import deliverables as inline
from cognis.api.chat_v2.event_store import RawSessionEvent
from cognis.api.chat_v2.schemas import ConversationStateView, ConversationSummary
from cognis.api.chat_v2.sync import (
    ConversationSessionRef,
    build_chat_snapshot,
    build_timeline_backfill_response,
    clear_chat_v2_read_caches,
)
from cognis.store.queries import create_deliverable
from tests.unit.api.chat_v2.test_inline_deliverables import _response, _seed
from tests.unit.api.chat_v2.test_sync import FakeEventStore

pytestmark = pytest.mark.asyncio


async def _history(factory, count=6):
    app = await _seed(factory)
    refs = [
        ConversationSessionRef(
            session_id=f"history-{index}",
            event_store_session_id=f"history-{index}",
            ordinal=index,
        )
        for index in range(2)
    ]
    events = {ref.event_store_session_id: [] for ref in refs}
    async with factory() as session:
        for index in range(count):
            id = f"dlv_history_{index}"
            await create_deliverable(
                session,
                deliverable_id=id,
                conversation_id="conv-inline",
                content=f"History {index}\n" + "A" * 1200,
                artifact_store=factory.artifact_store,
            )
            bucket = events[refs[index // 3].event_store_session_id]
            bucket.append(
                RawSessionEvent(
                    store_id="intaris",
                    session_id=refs[index // 3].event_store_session_id,
                    seq=len(bucket) + 1,
                    event_id=f"history-event-{index}",
                    type="lifecycle",
                    timestamp=datetime(2026, 1, 1, tzinfo=UTC) + timedelta(seconds=index),
                    data={
                        "event": "assistant_deliverable",
                        "deliverable_id": id,
                        "format": "markdown",
                    },
                )
            )
        await session.commit()
    store = FakeEventStore(events)
    scope = _response([]).scope

    async def snapshot(limit):
        return await build_chat_snapshot(
            conversation=ConversationSummary(conversation_id="conv-inline", agent_id="agent-owner"),
            scope=scope,
            state=ConversationStateView(
                state_version=1,
                snapshot_generated_at="2026-01-01T00:00:00Z",
            ),
            session_refs=refs,
            event_store=store,
            cursor_secret="history-pagination-test",
            initial_read_window=limit,
            strict_pairing=True,
        )

    async def backfill(before, limit):
        return await build_timeline_backfill_response(
            scope=scope,
            before=before,
            limit=limit,
            session_refs=refs,
            event_store=store,
            cursor_secret="history-pagination-test",
            strict_pairing=True,
        )

    clear_chat_v2_read_caches()
    return app, snapshot, backfill


async def test_ready_native_pages_recover_every_document_across_sessions(
    task_continuation_db, monkeypatch
):
    app, read_snapshot, read_backfill = await _history(task_continuation_db)
    monkeypatch.setattr(inline, "MAX_INLINE_TOTAL_BYTES", 2000)
    original = await read_snapshot(8)
    first = await inline.hydrate_history_page(
        app,
        original,
        caller_email="owner@example.com",
        conversation_id="conv-inline",
        limit=8,
        read_page=read_snapshot,
    )
    assert first.timeline.history_window_reduced
    assert all(item.deliverable is None for item in original.timeline.items)
    pages = [first.timeline.items]
    before = first.timeline.before_cursor
    cursors = set()
    while before is not None:
        assert before not in cursors
        cursors.add(before)

        async def read_page(limit, boundary=before):
            return await read_backfill(boundary, limit)

        page = await inline.hydrate_history_page(
            app,
            await read_page(8),
            caller_email="owner@example.com",
            conversation_id="conv-inline",
            limit=8,
            read_page=read_page,
        )
        pages.append(page.items)
        before = page.before_cursor
    recovered = [item for page in reversed(pages) for item in page]
    assert [item.id for item in recovered] == [item.id for item in original.timeline.items]
    assert [item.source_refs for item in recovered] == [
        item.source_refs for item in original.timeline.items
    ]
    assert all(item.deliverable is not None for item in recovered)
    assert len({item.id for item in recovered}) == 6


async def test_atomic_page_can_exceed_target_without_restoring_card_loading(
    task_continuation_db, monkeypatch
):
    app = await _seed(task_continuation_db)
    monkeypatch.setattr(inline, "MAX_INLINE_TOTAL_BYTES", 1)
    read = AsyncMock()
    page = await inline.hydrate_history_page(
        app,
        _response(["dlv_inline"]),
        caller_email="owner@example.com",
        conversation_id="conv-inline",
        limit=1,
        read_page=read,
    )
    assert page.items[0].deliverable.content == "Full fallback"
    read.assert_not_awaited()


async def test_history_does_not_turn_live_overlay_deadline_into_card_fallback(
    task_continuation_db, monkeypatch
):
    app = await _seed(task_continuation_db)
    monkeypatch.setattr(inline, "INLINE_TIMEOUT_SECONDS", 0)
    page = await inline.hydrate_history_page(
        app,
        _response(["dlv_inline"]),
        caller_email="owner@example.com",
        conversation_id="conv-inline",
        limit=8,
        read_page=AsyncMock(),
    )
    assert page.items[0].deliverable is not None


async def test_failed_older_body_stays_behind_ready_page_and_blocks_advancement(
    task_continuation_db, monkeypatch
):
    app, read_snapshot, read_backfill = await _history(task_continuation_db, count=3)
    original_load = app.state.artifact_store.async_load

    async def load(namespace, object_id, filename):
        if object_id == "dlv_history_1":
            raise FileNotFoundError()
        return await original_load(namespace, object_id, filename)

    monkeypatch.setattr(app.state.artifact_store, "async_load", load)
    page = await inline.hydrate_history_page(
        app,
        await read_snapshot(8),
        caller_email="owner@example.com",
        conversation_id="conv-inline",
        limit=8,
        read_page=read_snapshot,
    )
    assert [item.deliverable_id for item in page.timeline.items] == ["dlv_history_2"]
    before = page.timeline.before_cursor
    assert before

    async def read_page(limit):
        return await read_backfill(before, limit)

    with pytest.raises(HTTPException) as error:
        await inline.hydrate_history_page(
            app,
            await read_page(8),
            caller_email="owner@example.com",
            conversation_id="conv-inline",
            limit=8,
            read_page=read_page,
        )
    assert error.value.status_code == 409
    assert error.value.detail["code"] == "history_document_unavailable"
