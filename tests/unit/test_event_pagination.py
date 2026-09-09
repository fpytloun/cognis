"""Tests for Intaris forward-event cursor semantics."""

from __future__ import annotations

import pytest

from cognis.models.session import (
    EventPaginationError,
    EventReadResult,
    next_event_page_after_seq,
)


def _page(*seqs: int, last_seq: int, has_more: bool) -> EventReadResult:
    return EventReadResult(
        events=[{"seq": seq, "type": "system_message", "data": {}} for seq in seqs],
        last_seq=last_seq,
        has_more=has_more,
    )


def test_next_event_page_cursor_uses_returned_page_not_stream_high_water() -> None:
    page = _page(*range(1, 501), last_seq=1201, has_more=True)

    assert next_event_page_after_seq(page, 0) == 500


def test_next_event_page_cursor_handles_sparse_filtered_events() -> None:
    page = _page(504, 611, 799, last_seq=1201, has_more=True)

    assert next_event_page_after_seq(page, 500) == 799


def test_next_event_page_cursor_stops_on_complete_page() -> None:
    page = _page(1201, last_seq=1201, has_more=False)

    assert next_event_page_after_seq(page, 1200) is None


def test_next_event_page_cursor_rejects_stalled_page() -> None:
    page = _page(500, last_seq=1201, has_more=True)

    with pytest.raises(EventPaginationError, match="did not advance"):
        next_event_page_after_seq(page, 500)


def test_next_event_page_cursor_rejects_malformed_sequences() -> None:
    page = EventReadResult(
        events=[
            {"seq": True, "type": "system_message", "data": {}},
            {"seq": "501", "type": "system_message", "data": {}},
            {"type": "system_message", "data": {}},
        ],
        last_seq=1201,
        has_more=True,
    )

    with pytest.raises(EventPaginationError, match="did not advance"):
        next_event_page_after_seq(page, 500)
