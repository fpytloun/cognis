from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
from urllib.parse import quote

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from cognis.api.app import create_app
from cognis.api.middleware import AuthenticatedUser
from cognis.api.models import MessageEventResponse, TaskCreateRequest
from cognis.api.routes.conversations import (
    _project_timeline_events,
)
from cognis.api.routes.tasks import task_create
from cognis.api.websocket import (
    AuthenticatedWebSocket,
    WebSocketConnectionManager,
    _handle_resolve_escalation,
    _handle_step_response,
)
from cognis.core.agent_direct import AGENT_DIRECT_KIND, agent_direct_context_ref
from cognis.core.agent_loop import PendingPause
from cognis.core.decision import DecisionResult
from cognis.core.task_queue import TaskRerunResult
from cognis.models.search import SearchMatch, SearchSessionMatch, SearchSessionsResponse
from cognis.models.session import (
    EventReadResult,
    IntarisAgentSummaryRecord,
    IntarisSession,
    IntarisSessionSummaries,
    IntarisSessionSummaryRecord,
)
from cognis.models.task import TaskDelivery, TaskModel, TaskStatus
from cognis.models.workflow import WorkflowState
from cognis.store.models import NotificationRow
from cognis.store.queries import (
    create_agent,
    create_artifact_record,
    create_conversation,
    create_deliverable,
    create_managed_conversation_link,
    create_session,
    create_skill,
    create_skill_asset,
    create_skill_version,
    create_step_run,
    create_task,
    create_user,
    get_conversation,
    get_managed_conversation_link_for_target,
    get_user_ui_state_value,
    set_session_intaris_session_id,
    set_session_status,
    touch_conversation,
    update_conversation_active_session,
    update_managed_conversation_link,
)


def _strip_order_key(item: dict[str, object]) -> dict[str, object]:
    """Return a copy of a projected timeline item without the orderKey field.

    Tests that assert exact item shapes should use this helper so they remain
    stable when the orderKey encoding changes.
    """
    return {k: v for k, v in item.items() if k != "orderKey"}


def _create_test_client(monkeypatch: object, tmp_path: Path) -> TestClient:
    monkeypatch.setenv("COGNIS_DATA_DIR", str(tmp_path))  # type: ignore[attr-defined]
    monkeypatch.setenv("COGNIS_HOST", "127.0.0.1")  # type: ignore[attr-defined]
    app = create_app()
    return TestClient(app)


def _auth_headers(app: object, *, email: str, role: str = "user") -> dict[str, str]:
    token = app.state.auth_provider.sign_access_token(email, email.split("@")[0].title(), role)  # type: ignore[attr-defined]
    return {"Authorization": f"Bearer {token}"}


def _event(
    event_type: str,
    event_id: str,
    *,
    seq: int,
    ts: str,
    data: dict[str, object],
) -> MessageEventResponse:
    return MessageEventResponse(
        seq=seq,
        type=event_type,
        event_id=event_id,
        data=data,
        timestamp=ts,
    )


def test_timeline_projection_keeps_tool_delimited_assistant_phases() -> None:
    items = _project_timeline_events(
        [
            MessageEventResponse(
                seq=1,
                type="assistant_message",
                data={"session_id": "sess_a", "turn_id": "turn_1", "content": "First"},
                timestamp="2026-01-01T00:00:01Z",
            ),
            MessageEventResponse(
                seq=2,
                type="tool_call",
                data={
                    "session_id": "sess_a",
                    "turn_id": "turn_1",
                    "call_id": "call_1",
                    "name": "read",
                    "visible_name": "cat_file",
                    "canonical_name": "read",
                    "arguments": '{"file_path":"README.md"}',
                },
                timestamp="2026-01-01T00:00:02Z",
            ),
            MessageEventResponse(
                seq=3,
                type="tool_result",
                data={
                    "session_id": "sess_a",
                    "turn_id": "turn_1",
                    "call_id": "call_1",
                    "result": "ok",
                },
                timestamp="2026-01-01T00:00:03Z",
            ),
            MessageEventResponse(
                seq=4,
                type="assistant_message",
                data={"session_id": "sess_a", "turn_id": "turn_1", "content": "Second"},
                timestamp="2026-01-01T00:00:04Z",
            ),
        ]
    )

    assistant_messages = [
        item for item in items if item.get("kind") == "message" and item.get("role") == "assistant"
    ]

    assert [item["content"] for item in assistant_messages] == ["First", "Second"]
    assert [item["assistantPhaseIndex"] for item in assistant_messages] == [0, 1]
    assert items[1]["arguments"] == {"file_path": "README.md"}
    assert items[1]["sessionId"] == "sess_a"
    assert items[1]["toolName"] == "read"
    assert items[1]["displayToolName"] == "cat_file"
    assert items[1]["canonicalToolName"] == "read"


def test_timeline_projection_collapses_large_repeated_assistant_body() -> None:
    unit = (
        ("Reviewed read-only. No edits made. " * 12)
        + "\n\n## Immediate root cause\n"
        + "ImageLightbox uses a hardcoded toolbar layout over the image stage."
    )
    items = _project_timeline_events(
        [
            MessageEventResponse(
                seq=1,
                type="assistant_message",
                data={
                    "session_id": "sess_a",
                    "message_id": "msg_1",
                    "turn_id": "turn_1",
                    "content": unit * 3,
                },
                timestamp="2026-01-01T00:00:01Z",
            ),
        ]
    )

    assert len(items) == 1
    assert items[0]["kind"] == "message"
    assert items[0]["role"] == "assistant"
    assert items[0]["content"] == unit


def test_timeline_projection_replaces_repeated_assistant_frames_for_same_message() -> None:
    items = _project_timeline_events(
        [
            MessageEventResponse(
                seq=1,
                type="assistant_message",
                data={
                    "message_id": "msg_1",
                    "turn_id": "turn_1",
                    "content": "First paragraph.",
                },
                timestamp="2026-01-01T00:00:01Z",
                session_id="sess_1",
            ),
            MessageEventResponse(
                seq=2,
                type="assistant_message",
                data={
                    "message_id": "msg_1",
                    "turn_id": "turn_1",
                    "content": "First paragraph.\n\nSecond paragraph.",
                },
                timestamp="2026-01-01T00:00:02Z",
                session_id="sess_1",
            ),
            MessageEventResponse(
                seq=3,
                type="assistant_message",
                data={
                    "message_id": "msg_1",
                    "turn_id": "turn_1",
                    "content": "First paragraph.\n\nSecond paragraph.",
                },
                timestamp="2026-01-01T00:00:03Z",
                session_id="sess_1",
            ),
        ]
    )

    assert len(items) == 1
    assert items[0]["kind"] == "message"
    assert items[0]["content"] == "First paragraph.\n\nSecond paragraph."


def test_timeline_projection_appends_non_overlapping_same_id_assistant_segments() -> None:
    items = _project_timeline_events(
        [
            MessageEventResponse(
                seq=1,
                type="assistant_message",
                data={
                    "message_id": "msg_1",
                    "turn_id": "turn_1",
                    "content": "First segment.",
                },
                timestamp="2026-01-01T00:00:01Z",
                session_id="sess_1",
            ),
            MessageEventResponse(
                seq=2,
                type="assistant_message",
                data={
                    "message_id": "msg_1",
                    "turn_id": "turn_1",
                    "content": "Second segment.",
                },
                timestamp="2026-01-01T00:00:02Z",
                session_id="sess_1",
            ),
        ]
    )

    assert len(items) == 1
    assert items[0]["content"] == "First segment.\n\nSecond segment."


def test_timeline_projection_does_not_merge_tiny_overlap_between_assistant_segments() -> None:
    items = _project_timeline_events(
        [
            MessageEventResponse(
                seq=1,
                type="assistant_message",
                data={
                    "message_id": "msg_1",
                    "turn_id": "turn_1",
                    "content": "Alpha",
                },
                timestamp="2026-01-01T00:00:01Z",
                session_id="sess_1",
            ),
            MessageEventResponse(
                seq=2,
                type="assistant_message",
                data={
                    "message_id": "msg_1",
                    "turn_id": "turn_1",
                    "content": "assistant beta",
                },
                timestamp="2026-01-01T00:00:02Z",
                session_id="sess_1",
            ),
        ]
    )

    assert len(items) == 1
    assert items[0]["content"] == "Alpha\n\nassistant beta"


def test_timeline_projection_orders_assistant_text_before_delegate_card_when_final_persists_late() -> (
    None
):
    items = _project_timeline_events(
        [
            MessageEventResponse(
                seq=1,
                type="tool_call",
                data={
                    "call_id": "call_delegate",
                    "name": "delegate",
                    "turn_id": "turn_1",
                    "arguments": {"task": "Inspect UI renderer"},
                },
                timestamp="2026-01-01T00:00:01Z",
                session_id="sess_1",
            ),
            MessageEventResponse(
                seq=2,
                type="delegation",
                data={
                    "child_session_id": "child_1",
                    "task": "Inspect UI renderer",
                    "agent_id": "system:explore",
                    "status": "started",
                },
                timestamp="2026-01-01T00:00:02Z",
                session_id="sess_1",
            ),
            MessageEventResponse(
                seq=3,
                type="assistant_message",
                data={
                    "message_id": "msg_1",
                    "turn_id": "turn_1",
                    "content": "I'll inspect the current UI rendering path first.",
                },
                timestamp="2026-01-01T00:00:03Z",
                session_id="sess_1",
            ),
        ]
    )

    assert [item["kind"] for item in items[:2]] == ["message", "delegation"]
    assert items[0]["content"] == "I'll inspect the current UI rendering path first."
    assert items[1]["taskLabel"] == "Inspect UI renderer"


def test_timeline_projection_does_not_pull_later_assistant_phase_before_delegate() -> None:
    items = _project_timeline_events(
        [
            MessageEventResponse(
                seq=1,
                type="assistant_message",
                data={
                    "message_id": "msg_phase_0",
                    "turn_id": "turn_1",
                    "content": "I'll inspect the current UI rendering path first.",
                    "assistant_phase_index": 0,
                },
                timestamp="2026-01-01T00:00:01Z",
                session_id="sess_1",
            ),
            MessageEventResponse(
                seq=2,
                type="tool_call",
                data={
                    "call_id": "call_delegate",
                    "name": "delegate",
                    "turn_id": "turn_1",
                    "arguments": {"task": "Inspect UI renderer"},
                },
                timestamp="2026-01-01T00:00:02Z",
                session_id="sess_1",
            ),
            MessageEventResponse(
                seq=3,
                type="delegation",
                data={
                    "child_session_id": "child_1",
                    "task": "Inspect UI renderer",
                    "agent_id": "system:explore",
                    "status": "started",
                },
                timestamp="2026-01-01T00:00:03Z",
                session_id="sess_1",
            ),
            MessageEventResponse(
                seq=4,
                type="assistant_message",
                data={
                    "message_id": "msg_phase_1",
                    "turn_id": "turn_1",
                    "content": "The relevant renderer is in TimelineList.",
                    "assistant_phase_index": 1,
                },
                timestamp="2026-01-01T00:00:04Z",
                session_id="sess_1",
            ),
        ]
    )

    assert [item["kind"] for item in items[:3]] == ["message", "delegation", "message"]


def test_timeline_projection_exposes_user_client_message_id() -> None:
    items = _project_timeline_events(
        [
            MessageEventResponse(
                seq=1,
                type="user_message",
                data={
                    "content": "hello",
                    "message_id": "user_msg_1",
                    "client_message_id": "cmsg_1",
                },
                timestamp="2026-01-01T00:00:00Z",
                session_id="sess_1",
            )
        ]
    )

    assert len(items) == 1
    assert items[0]["kind"] == "message"
    assert items[0]["role"] == "user"
    assert items[0]["content"] == "hello"
    assert items[0]["clientMessageId"] == "cmsg_1"


def test_timeline_projection_infers_post_tool_assistant_phase_for_legacy_events() -> None:
    items = _project_timeline_events(
        [
            _event(
                "tool_call",
                "tool-1",
                seq=1,
                ts="2026-01-01T00:00:01Z",
                data={
                    "call_id": "call_1",
                    "tool_name": "read",
                    "arguments": "{}",
                    "status": "running",
                    "turn_id": "turn_1",
                },
            ),
            _event(
                "assistant_thinking",
                "thinking-1",
                seq=2,
                ts="2026-01-01T00:00:02Z",
                data={
                    "message_id": "msg_after_tool",
                    "turn_id": "turn_1",
                    "block_id": "think_1",
                    "content": "reviewing tool output",
                },
            ),
            _event(
                "assistant_message",
                "assistant-1",
                seq=3,
                ts="2026-01-01T00:00:03Z",
                data={
                    "message_id": "msg_after_tool",
                    "turn_id": "turn_1",
                    "content": "done",
                },
            ),
        ]
    )

    thinking = next(item for item in items if item["kind"] == "thinking")
    assistant = next(item for item in items if item["kind"] == "message")
    assert thinking["assistantPhaseIndex"] == 1
    assert thinking["id"] == "thinking:msg_after_tool:phase:1:think_1"
    assert assistant["assistantPhaseIndex"] == 1
    assert assistant["id"] == "message:msg_after_tool:phase:1"


def test_projected_items_carry_order_key() -> None:
    """Every projected item must carry an orderKey so the client can sort."""
    items = _project_timeline_events(
        [
            _event(
                "user_message",
                "u-1",
                seq=1,
                ts="2026-01-01T00:00:01Z",
                data={"content": "hi", "turn_id": "turn_1"},
            ),
            _event(
                "assistant_thinking",
                "think-1",
                seq=2,
                ts="2026-01-01T00:00:02Z",
                data={
                    "message_id": "msg_1",
                    "turn_id": "turn_1",
                    "block_id": "blk_1",
                    "content": "thinking",
                },
            ),
            _event(
                "assistant_message",
                "a-1",
                seq=3,
                ts="2026-01-01T00:00:03Z",
                data={"message_id": "msg_1", "turn_id": "turn_1", "content": "done"},
            ),
            _event(
                "tool_call",
                "tc-1",
                seq=4,
                ts="2026-01-01T00:00:04Z",
                data={"call_id": "call_1", "tool_name": "bash", "turn_id": "turn_1"},
            ),
        ]
    )

    for item in items:
        assert "orderKey" in item, f"Missing orderKey on {item.get('kind')} item"
        assert isinstance(item["orderKey"], str) and item["orderKey"], (
            f"Empty orderKey on {item.get('kind')} item"
        )

    # Items must be sorted by their own orderKey
    keys = [item["orderKey"] for item in items]
    assert keys == sorted(keys), f"Items not sorted by orderKey: {keys}"


def test_projected_items_order_key_stable_across_arrival_order() -> None:
    """Delegation arriving before assistant must still sort after it."""
    # Simulate: user_message (seq=1), delegation_started (seq=2), assistant_message (seq=3)
    # The delegation tool_call event arrives before the assistant_message in the event stream.
    items = _project_timeline_events(
        [
            _event(
                "user_message",
                "u-1",
                seq=1,
                ts="2026-01-01T00:00:01Z",
                data={"content": "delegate this", "turn_id": "turn_1"},
            ),
            _event(
                "assistant_message",
                "a-1",
                seq=2,
                ts="2026-01-01T00:00:02Z",
                data={"message_id": "msg_1", "turn_id": "turn_1", "content": "I will delegate"},
            ),
            _event(
                "tool_call",
                "tc-1",
                seq=3,
                ts="2026-01-01T00:00:03Z",
                data={
                    "call_id": "call_1",
                    "tool_name": "delegate",
                    "turn_id": "turn_1",
                    "arguments": '{"task": "do the thing"}',
                },
            ),
            _event(
                "delegation",
                "del-1",
                seq=4,
                ts="2026-01-01T00:00:04Z",
                data={
                    "child_session_id": "child_sess_1",
                    "title": "do the thing",
                    "status": "started",
                    "turn_id": "turn_1",
                },
            ),
        ]
    )

    kinds = [item["kind"] for item in items]
    # user → assistant → delegation (delegate tool_call is consumed, not projected)
    assert kinds[0] == "message" and items[0]["role"] == "user"
    assert kinds[1] == "message" and items[1]["role"] == "assistant"
    assert kinds[2] == "delegation"

    keys = [item["orderKey"] for item in items]
    assert keys == sorted(keys), f"Items not sorted by orderKey: {keys}"


def test_persisted_assistant_message_phase_matches_runtime_stream_phase() -> None:
    """When assistant_phase_index is explicit in the event, projection uses it.

    This ensures that a persisted assistant_message event carrying
    assistant_phase_index=1 (set by the scheduler on the normal completion
    path) produces the same stable id as the runtime stream snapshot that
    also used phase=1, preventing a duplicate 'in progress' bubble.
    """
    from cognis.api.routes.conversations import _stable_assistant_timeline_id

    # Simulate: tool_call (seq=1) → assistant_message (seq=2, phase=1 explicit)
    items = _project_timeline_events(
        [
            _event(
                "tool_call",
                "tc-1",
                seq=1,
                ts="2026-01-01T00:00:01Z",
                data={"call_id": "call_1", "tool_name": "bash", "turn_id": "turn_1"},
            ),
            _event(
                "assistant_message",
                "a-1",
                seq=2,
                ts="2026-01-01T00:00:02Z",
                data={
                    "message_id": "msg_1",
                    "turn_id": "turn_1",
                    "content": "done",
                    "assistant_phase_index": 1,  # explicit — set by scheduler
                },
            ),
        ]
    )

    assistant = next(item for item in items if item.get("kind") == "message")
    assert assistant["assistantPhaseIndex"] == 1
    # The stable id must match what the runtime stream would produce
    expected_id = _stable_assistant_timeline_id("msg_1", 1, "msg_1")
    assert assistant["id"] == expected_id


def test_cross_session_lineage_orderkey_sorts_older_session_before_newer() -> None:
    """Post-compaction: session B (seq restarts at 1) must sort AFTER session A.

    Without the lineage_index dimension, session B seq=1 would sort before
    session A seq=40 because orderKey was seq-primary with no session field.
    """
    from cognis.api.routes.conversations import _tag_session_events

    # Session A (older, lineage_index=0): one user message at seq=40
    session_a = SimpleNamespace(session_id="sess_a")
    events_a = [
        {
            "seq": 40,
            "type": "user_message",
            "timestamp": "2026-01-01T00:00:40Z",
            "data": {"content": "old message", "turn_id": "turn_a"},
        }
    ]
    _tag_session_events(session_a, events_a, lineage_index=0)

    # Session B (newer, lineage_index=1): user message at seq=1 (restarted)
    session_b = SimpleNamespace(session_id="sess_b")
    events_b = [
        {
            "seq": 1,
            "type": "user_message",
            "timestamp": "2026-01-01T00:01:00Z",
            "data": {"content": "new message after compaction", "turn_id": "turn_b"},
        }
    ]
    _tag_session_events(session_b, events_b, lineage_index=1)

    # Merge and project (simulating the full-load path)
    all_events_raw = events_a + events_b
    all_events = [
        MessageEventResponse(
            seq=e["seq"],
            type=e["type"],
            data=e["data"],
            timestamp=e["timestamp"],
        )
        for e in all_events_raw
    ]
    items = _project_timeline_events(all_events)

    assert len(items) == 2
    # Session A (lineage=0, seq=40) must come before session B (lineage=1, seq=1)
    assert items[0]["content"] == "old message", (
        f"Expected old message first, got: {[i.get('content') for i in items]}"
    )
    assert items[1]["content"] == "new message after compaction"
    # orderKeys must be sorted
    keys = [item["orderKey"] for item in items]
    assert keys == sorted(keys), f"Items not sorted by orderKey: {keys}"
    # Session B's orderKey must be larger despite lower seq
    assert items[0]["orderKey"] < items[1]["orderKey"]


def test_live_bus_patches_get_distinct_order_keys() -> None:
    """Two concurrent live delegation patches must get distinct orderKeys.

    This covers both the multi-event (one projection call) and separate
    single-event (two separate projection calls) scenarios.
    """
    from cognis.api.routes.conversations import _project_timeline_events

    # Scenario A: two events in one projection call with distinct counters
    events_together = [
        MessageEventResponse(
            seq=None,
            type="delegation",
            data={
                "child_session_id": "child_1",
                "title": "First task",
                "status": "started",
                "_live_patch_counter": 1,
            },
            timestamp="2026-01-01T00:00:01Z",
        ),
        MessageEventResponse(
            seq=None,
            type="delegation",
            data={
                "child_session_id": "child_2",
                "title": "Second task",
                "status": "started",
                "_live_patch_counter": 2,
            },
            timestamp="2026-01-01T00:00:02Z",
        ),
    ]
    items_together = _project_timeline_events(events_together)
    assert len(items_together) == 2
    keys_together = [item["orderKey"] for item in items_together]
    assert keys_together[0] != keys_together[1], (
        f"Multi-event projection got identical orderKeys: {keys_together}"
    )
    assert keys_together == sorted(keys_together)

    # Scenario B: two separate single-event projection calls (the real live path)
    items_a = _project_timeline_events(
        [
            MessageEventResponse(
                seq=None,
                type="delegation",
                data={
                    "child_session_id": "child_3",
                    "title": "Third task",
                    "status": "started",
                    "_live_patch_counter": 3,
                },
                timestamp="2026-01-01T00:00:03Z",
            )
        ]
    )
    items_b = _project_timeline_events(
        [
            MessageEventResponse(
                seq=None,
                type="delegation",
                data={
                    "child_session_id": "child_4",
                    "title": "Fourth task",
                    "status": "started",
                    "_live_patch_counter": 4,
                },
                timestamp="2026-01-01T00:00:04Z",
            )
        ]
    )
    assert len(items_a) == 1
    assert len(items_b) == 1
    key_a = items_a[0]["orderKey"]
    key_b = items_b[0]["orderKey"]
    assert key_a != key_b, (
        f"Separate single-event projections got identical orderKeys: {key_a!r} == {key_b!r}"
    )
    # Counter 3 < counter 4, so key_a < key_b
    assert key_a < key_b, f"Expected key_a < key_b but got {key_a!r} >= {key_b!r}"


def test_orderkey_sentinel_fits_in_format_field() -> None:
    """Sentinel values must fit exactly in their format fields without widening."""
    from cognis.api.routes.conversations import (
        _ORDER_KEY_NO_LINEAGE,
        _ORDER_KEY_NO_SEQ,
        _encode_order_key,
    )

    sentinel_key = _encode_order_key(lineage=None, seq=None, phase=None, kind_rank=0, local=0)
    # Sentinel lineage (9999) must be 4 digits
    assert sentinel_key.startswith(f"{_ORDER_KEY_NO_LINEAGE:04d}:"), (
        f"Sentinel lineage widened: {sentinel_key}"
    )
    # Sentinel seq (10**15-1) must be 15 digits
    seq_part = sentinel_key.split(":")[1]
    assert len(seq_part) == 15, f"Sentinel seq is {len(seq_part)} digits, expected 15: {seq_part}"
    assert int(seq_part) == _ORDER_KEY_NO_SEQ

    # A real persisted key must be lexicographically smaller than the sentinel
    real_key = _encode_order_key(lineage=0, seq=999_999_999_999_999, phase=0, kind_rank=0, local=0)
    assert real_key < sentinel_key, f"Real key {real_key!r} should be < sentinel {sentinel_key!r}"


def test_timeline_projection_hides_lifecycle_compaction_start_notice() -> None:
    items = _project_timeline_events(
        [
            MessageEventResponse(
                seq=1,
                type="lifecycle",
                data={
                    "event": "system_notice",
                    "message": (
                        "The model provider rejected the request because the context window is full. "
                        "Cognis is compacting the saved conversation and will retry the turn in a fresh compacted session."
                    ),
                    "status": "started",
                    "notice_id": "provider-overflow",
                },
                timestamp="2026-01-01T00:00:01Z",
            )
        ]
    )

    assert items == []


def test_timeline_projection_keeps_repeated_same_task_delegations_in_call_order() -> None:
    items = _project_timeline_events(
        [
            MessageEventResponse(
                seq=1,
                type="tool_call",
                data={
                    "call_id": "call_a",
                    "name": "delegate",
                    "arguments": {"task": "Inspect same task"},
                    "turn_id": "turn_1",
                },
                timestamp="2026-01-01T00:00:01Z",
                session_id="sess_1",
            ),
            MessageEventResponse(
                seq=2,
                type="tool_call",
                data={
                    "call_id": "call_b",
                    "name": "delegate",
                    "arguments": {"task": "Inspect same task"},
                    "turn_id": "turn_1",
                },
                timestamp="2026-01-01T00:00:02Z",
                session_id="sess_1",
            ),
            MessageEventResponse(
                seq=3,
                type="delegation",
                data={
                    "child_session_id": "child_a",
                    "task": "Inspect same task",
                    "agent_id": "system:explore",
                    "status": "started",
                },
                timestamp="2026-01-01T00:00:03Z",
                session_id="sess_1",
            ),
            MessageEventResponse(
                seq=4,
                type="delegation",
                data={
                    "child_session_id": "child_b",
                    "task": "Inspect same task",
                    "agent_id": "system:explore",
                    "status": "started",
                },
                timestamp="2026-01-01T00:00:04Z",
                session_id="sess_1",
            ),
        ]
    )

    delegations = [item for item in items if item["kind"] == "delegation"]

    assert [item["taskId"] for item in delegations] == ["child_a", "child_b"]


def test_timeline_projection_missing_sequence_orders_after_sequenced_events_by_timestamp() -> None:
    items = _project_timeline_events(
        [
            MessageEventResponse(
                seq=1,
                type="user_message",
                data={"content": "First", "message_id": "user_1"},
                timestamp="2026-01-01T00:00:02Z",
                session_id="sess_1",
            ),
            MessageEventResponse(
                seq=None,
                type="assistant_message",
                data={"content": "Unsequenced", "message_id": "assistant_missing"},
                timestamp="2026-01-01T00:00:01Z",
                session_id="sess_1",
            ),
            MessageEventResponse(
                seq=2,
                type="assistant_message",
                data={"content": "Second", "message_id": "assistant_2"},
                timestamp="2026-01-01T00:00:03Z",
                session_id="sess_1",
            ),
        ]
    )

    assert [item["content"] for item in items] == ["First", "Second", "Unsequenced"]


def test_timeline_projection_missing_tool_sequence_does_not_sort_before_sequenced_events() -> None:
    items = _project_timeline_events(
        [
            MessageEventResponse(
                seq=1,
                type="assistant_message",
                data={"content": "Sequenced", "message_id": "assistant_1"},
                timestamp="2026-01-01T00:00:02Z",
                session_id="sess_1",
            ),
            MessageEventResponse(
                seq=None,
                type="tool_call",
                data={
                    "call_id": "call_1",
                    "name": "bash",
                    "arguments": {"command": "true"},
                    "status": "running",
                },
                timestamp="2026-01-01T00:00:01Z",
                session_id="sess_1",
            ),
        ]
    )

    assert [item["kind"] for item in items] == ["message", "tool_call"]


def test_timeline_projection_replaces_duplicate_system_notices() -> None:
    items = _project_timeline_events(
        [
            MessageEventResponse(
                seq=1,
                type="system_message",
                data={
                    "session_id": "sess_a",
                    "notice_id": "turn-start",
                    "kind": "turn_initiated",
                    "content": "Starting",
                },
                timestamp="2026-01-01T00:00:01Z",
            ),
            MessageEventResponse(
                seq=2,
                type="system_message",
                data={
                    "session_id": "sess_a",
                    "notice_id": "turn-start",
                    "kind": "turn_initiated",
                    "content": "Started",
                },
                timestamp="2026-01-01T00:00:02Z",
            ),
        ]
    )

    assert [_strip_order_key(item) for item in items] == [
        {
            "id": "system:turn-start",
            "kind": "system_message",
            "text": "Started",
            "noticeId": "turn-start",
            "noticeKind": "turn_initiated",
            "timestamp": "2026-01-01T00:00:02Z",
        }
    ]


def test_timeline_projection_preserves_assistant_thinking_event_order() -> None:
    items = _project_timeline_events(
        [
            MessageEventResponse(
                seq=1,
                type="assistant_message",
                data={
                    "session_id": "sess_a",
                    "turn_id": "turn_1",
                    "message_id": "msg_1",
                    "content": "Answer",
                },
                timestamp="2026-01-01T00:00:01Z",
            ),
            MessageEventResponse(
                seq=2,
                type="assistant_thinking",
                data={
                    "session_id": "sess_a",
                    "turn_id": "turn_1",
                    "message_id": "msg_1",
                    "block_id": "block_1",
                    "content": "first",
                },
                timestamp="2026-01-01T00:00:02Z",
            ),
            MessageEventResponse(
                seq=3,
                type="assistant_thinking",
                data={
                    "session_id": "sess_a",
                    "turn_id": "turn_1",
                    "message_id": "msg_1",
                    "block_id": "block_2",
                    "content": "second",
                },
                timestamp="2026-01-01T00:00:03Z",
            ),
        ]
    )

    assert [item["kind"] for item in items] == ["message", "thinking", "thinking"]
    assert items[0]["content"] == "Answer"
    assert items[0]["assistantPhaseIndex"] == 0
    assert items[1]["messageId"] == "msg_1"
    assert items[1]["sessionId"] == "sess_a"
    assert items[1]["assistantPhaseIndex"] == 1
    assert items[1]["blocks"][0]["content"] == "first"
    assert items[2]["messageId"] == "msg_1"
    assert items[2]["sessionId"] == "sess_a"
    assert items[2]["assistantPhaseIndex"] == 1
    assert items[2]["blocks"][0]["content"] == "second"


def test_timeline_projection_collapses_legacy_repeated_thinking_content() -> None:
    body = "Addressing footer and signature layout. This repeated body came from cumulative snapshots. "
    items = _project_timeline_events(
        [
            MessageEventResponse(
                seq=1,
                session_id="sess_a",
                type="assistant_thinking",
                data={
                    "message_id": "msg_1",
                    "turn_id": "turn_1",
                    "block_id": "thk_1",
                    "title": "Addressing footer and signature layout",
                    "content": body * 3,
                },
                timestamp="2026-01-01T00:00:01Z",
            )
        ]
    )

    assert items[0]["blocks"][0]["content"] == body.strip()


def test_timeline_projection_collapses_repeated_thinking_with_derived_title_ellipsis() -> None:
    body = "Addressing footer and signature layout with a long title that may be truncated before matching the body. "
    items = _project_timeline_events(
        [
            MessageEventResponse(
                seq=1,
                session_id="sess_a",
                type="assistant_thinking",
                data={
                    "message_id": "msg_1",
                    "turn_id": "turn_1",
                    "block_id": "thk_1",
                    "title": "Addressing footer and signature layout with a long title that may be…",
                    "content": body * 3,
                },
                timestamp="2026-01-01T00:00:01Z",
            )
        ]
    )

    assert items[0]["blocks"][0]["content"] == body.strip()


def test_timeline_projection_does_not_merge_assistant_messages_across_thinking() -> None:
    items = _project_timeline_events(
        [
            MessageEventResponse(
                seq=1,
                type="assistant_message",
                data={
                    "session_id": "sess_a",
                    "turn_id": "turn_1",
                    "message_id": "msg_1",
                    "content": "Before thinking",
                },
                timestamp="2026-01-01T00:00:01Z",
            ),
            MessageEventResponse(
                seq=2,
                type="assistant_thinking",
                data={
                    "session_id": "sess_a",
                    "turn_id": "turn_1",
                    "message_id": "msg_1",
                    "block_id": "block_1",
                    "content": "intervening thought",
                },
                timestamp="2026-01-01T00:00:02Z",
            ),
            MessageEventResponse(
                seq=3,
                type="assistant_message",
                data={
                    "session_id": "sess_a",
                    "turn_id": "turn_1",
                    "message_id": "msg_1",
                    "content": "After thinking",
                },
                timestamp="2026-01-01T00:00:03Z",
            ),
        ]
    )

    assert [item["kind"] for item in items] == ["message", "thinking", "message"]
    assert [item.get("assistantPhaseIndex") for item in items] == [0, 1, 1]
    assert items[0]["content"] == "Before thinking"
    assert items[1]["blocks"][0]["content"] == "intervening thought"
    assert items[2]["content"] == "After thinking"


def test_timeline_projection_hides_user_message_reasoning_echo() -> None:
    items = _project_timeline_events(
        [
            MessageEventResponse(
                seq=1,
                type="assistant_thinking",
                data={
                    "session_id": "sess_a",
                    "turn_id": "turn_1",
                    "message_id": "msg_1",
                    "block_id": "block_1",
                    "title": "Reasoning",
                    "content": "User message: check current repo state",
                    "reasoning_source": "reasoning",
                },
                timestamp="2026-01-01T00:00:01Z",
            ),
            MessageEventResponse(
                seq=2,
                type="assistant_message",
                data={
                    "session_id": "sess_a",
                    "turn_id": "turn_1",
                    "message_id": "msg_1",
                    "content": "Answer",
                },
                timestamp="2026-01-01T00:00:02Z",
            ),
        ]
    )

    assert [item["kind"] for item in items] == ["message"]
    assert items[0]["content"] == "Answer"


def test_timeline_projection_uses_unique_thinking_ids_across_tool_boundaries() -> None:
    items = _project_timeline_events(
        [
            MessageEventResponse(
                seq=1,
                type="assistant_thinking",
                data={
                    "session_id": "sess_a",
                    "turn_id": "turn_1",
                    "message_id": "msg_1",
                    "block_id": "block_1",
                    "content": "Before tool",
                },
                timestamp="2026-01-01T00:00:01Z",
            ),
            MessageEventResponse(
                seq=2,
                type="tool_call",
                data={
                    "session_id": "sess_a",
                    "turn_id": "turn_1",
                    "call_id": "call_1",
                    "tool_name": "read",
                },
                timestamp="2026-01-01T00:00:02Z",
            ),
            MessageEventResponse(
                seq=3,
                type="assistant_thinking",
                data={
                    "session_id": "sess_a",
                    "turn_id": "turn_1",
                    "message_id": "msg_1",
                    "block_id": "block_2",
                    "content": "After tool",
                },
                timestamp="2026-01-01T00:00:03Z",
            ),
        ]
    )

    thinking_items = [item for item in items if item["kind"] == "thinking"]
    assert len(thinking_items) == 2
    assert thinking_items[0]["id"] != thinking_items[1]["id"]


def test_timeline_projection_hides_generic_reasoning_diagnostics() -> None:
    items = _project_timeline_events(
        [
            MessageEventResponse(
                seq=1,
                session_id="sess_a",
                type="reasoning",
                data={
                    "session_id": "sess_a",
                    "turn_id": "turn_1",
                    "message_id": "msg_1",
                    "block_id": "reason_1",
                    "summary": "The model considered alternatives.",
                    "source": "summary",
                },
                timestamp="2026-01-01T00:00:01Z",
            ),
            MessageEventResponse(
                seq=2,
                session_id="sess_a",
                type="assistant_message",
                data={
                    "session_id": "sess_a",
                    "turn_id": "turn_1",
                    "message_id": "msg_1",
                    "content": "Final answer",
                },
                timestamp="2026-01-01T00:00:02Z",
            ),
        ]
    )

    assert [item["kind"] for item in items] == ["message"]
    assert items[0]["content"] == "Final answer"


def test_timeline_projection_hides_internal_audit_events() -> None:
    items = _project_timeline_events(
        [
            MessageEventResponse(
                seq=1,
                session_id="sess_a",
                type="developer_message",
                data={"content": "Internal context only"},
                timestamp="2026-01-01T00:00:01Z",
            ),
            MessageEventResponse(
                seq=2,
                session_id="sess_a",
                type="lifecycle",
                data={"event": "intention_updated", "intention": "debug issue"},
                timestamp="2026-01-01T00:00:02Z",
            ),
            MessageEventResponse(
                seq=3,
                session_id="sess_a",
                type="lifecycle",
                data={"event": "session_status_changed", "status": "completed"},
                timestamp="2026-01-01T00:00:03Z",
            ),
            MessageEventResponse(
                seq=4,
                session_id="sess_a",
                type="lifecycle",
                data={"event": "session_created", "session_id": "sess_child"},
                timestamp="2026-01-01T00:00:04Z",
            ),
            MessageEventResponse(
                seq=5,
                session_id="sess_a",
                type="context_snapshot",
                data={"event": "context_snapshot", "summary": "internal context"},
                timestamp="2026-01-01T00:00:05Z",
            ),
            MessageEventResponse(
                seq=6,
                session_id="sess_a",
                type="evaluation",
                data={},
                timestamp="2026-01-01T00:00:06Z",
            ),
            MessageEventResponse(
                seq=7,
                session_id="sess_a",
                type="lifecycle",
                data={"event": "tool_discovery", "session_id": "sess_a"},
                timestamp="2026-01-01T00:00:07Z",
            ),
        ]
    )

    assert items == []


def test_timeline_projection_closes_assistant_phase_before_lifecycle_notice() -> None:
    items = _project_timeline_events(
        [
            MessageEventResponse(
                seq=1,
                type="assistant_message",
                data={
                    "session_id": "sess_a",
                    "turn_id": "turn_1",
                    "message_id": "msg_1",
                    "content": "Before",
                },
                timestamp="2026-01-01T00:00:01Z",
            ),
            MessageEventResponse(
                seq=2,
                type="lifecycle",
                data={
                    "session_id": "sess_a",
                    "event": "system_notice",
                    "message": "Notice",
                },
                timestamp="2026-01-01T00:00:02Z",
            ),
            MessageEventResponse(
                seq=3,
                type="assistant_message",
                data={
                    "session_id": "sess_a",
                    "turn_id": "turn_1",
                    "message_id": "msg_1",
                    "content": "After",
                },
                timestamp="2026-01-01T00:00:03Z",
            ),
        ]
    )

    assert [item["kind"] for item in items] == ["message", "system_message", "message"]
    assert items[0]["content"] == "Before"
    assert items[2]["content"] == "After"


def test_timeline_projection_closes_assistant_phase_before_session_recovery() -> None:
    items = _project_timeline_events(
        [
            MessageEventResponse(
                seq=1,
                type="assistant_message",
                data={
                    "session_id": "sess_a",
                    "turn_id": "turn_1",
                    "message_id": "msg_1",
                    "content": "Before",
                },
                timestamp="2026-01-01T00:00:01Z",
            ),
            MessageEventResponse(
                seq=2,
                type="session_recovered",
                data={"session_id": "sess_a"},
                timestamp="2026-01-01T00:00:02Z",
            ),
            MessageEventResponse(
                seq=3,
                type="assistant_message",
                data={
                    "session_id": "sess_a",
                    "turn_id": "turn_1",
                    "message_id": "msg_1",
                    "content": "After",
                },
                timestamp="2026-01-01T00:00:03Z",
            ),
        ]
    )

    assert [item["kind"] for item in items] == ["message", "system_message", "message"]
    assert items[0]["content"] == "Before"
    assert items[2]["content"] == "After"


def test_timeline_projection_projects_delegation_without_fallback() -> None:
    items = _project_timeline_events(
        [
            MessageEventResponse(
                seq=1,
                type="user_message",
                data={"session_id": "sess_a", "content": "Hi"},
                timestamp="2026-01-01T00:00:01Z",
            ),
            MessageEventResponse(
                seq=2,
                type="delegation",
                data={"session_id": "sess_a", "child_session_id": "sess_child"},
                timestamp="2026-01-01T00:00:02Z",
            ),
        ]
    )

    assert [item["kind"] for item in items] == ["message", "delegation"]
    assert _strip_order_key(items[1]) == {
        "id": "delegation:sess_child",
        "kind": "delegation",
        "taskId": "sess_child",
        "taskLabel": "Sub-session",
        "status": "started",
        "timestamp": "2026-01-01T00:00:02Z",
    }


def test_timeline_projection_uses_persisted_delegation_title() -> None:
    items = _project_timeline_events(
        [
            MessageEventResponse(
                seq=1,
                type="delegation",
                data={
                    "session_id": "sess_a",
                    "child_session_id": "sess_child",
                    "title": "Explore project",
                    "status": "completed",
                    "result_summary": "done",
                },
                timestamp="2026-01-01T00:00:01Z",
            ),
        ]
    )

    assert items[0]["taskLabel"] == "Explore project"


def test_timeline_projection_preserves_terminal_delegation_state() -> None:
    items = _project_timeline_events(
        [
            MessageEventResponse(
                seq=1,
                type="delegation",
                data={
                    "session_id": "sess_a",
                    "child_session_id": "sess_child",
                    "title": "Explore project",
                    "status": "completed",
                    "result_summary": "done",
                },
                timestamp="2026-01-01T00:00:01Z",
            ),
            MessageEventResponse(
                seq=2,
                type="delegation",
                data={
                    "session_id": "sess_a",
                    "child_session_id": "sess_child",
                    "status": "running",
                    "todos": [{"content": "stale", "status": "pending"}],
                },
                timestamp="2026-01-01T00:00:02Z",
            ),
        ]
    )

    assert len(items) == 1
    assert items[0]["taskLabel"] == "Explore project"
    assert items[0]["status"] == "completed"
    assert items[0]["result"] == "done"
    assert items[0]["timestamp"] == "2026-01-01T00:00:01Z"
    assert "todos" not in items[0]


def test_timeline_projection_projects_lifecycle_workflow_composed() -> None:
    items = _project_timeline_events(
        [
            MessageEventResponse(
                seq=1,
                type="lifecycle",
                data={
                    "session_id": "sess_a",
                    "event": "workflow_composed",
                    "workflow_id": "wf_1",
                    "workflow_name": "Workflow",
                    "steps": ["plan", "build"],
                },
                timestamp="2026-01-01T00:00:01Z",
            ),
        ]
    )

    assert [_strip_order_key(item) for item in items] == [
        {
            "id": "workflow-composed:sess_a:1",
            "kind": "workflow_composed",
            "workflowId": "wf_1",
            "workflowName": "Workflow",
            "lifecycle": "ephemeral",
            "steps": ["plan", "build"],
            "timestamp": "2026-01-01T00:00:01Z",
        }
    ]


def test_timeline_projection_uses_lifecycle_task_title() -> None:
    items = _project_timeline_events(
        [
            MessageEventResponse(
                seq=1,
                type="lifecycle",
                data={
                    "session_id": "sess_a",
                    "event": "task_result",
                    "task_id": "task_1",
                    "task_title": "Build projection",
                    "result_summary": "done",
                },
                timestamp="2026-01-01T00:00:01Z",
            ),
        ]
    )

    assert items[0]["kind"] == "delegation"
    assert items[0]["taskLabel"] == "Build projection"
    assert items[0]["status"] == "completed"


def test_timeline_projection_projects_unknown_events_as_notices() -> None:
    items = _project_timeline_events(
        [
            MessageEventResponse(
                seq=1,
                type="future_event",
                data={"session_id": "sess_a", "value": 1},
                timestamp="2026-01-01T00:00:01Z",
            ),
        ]
    )

    assert [_strip_order_key(item) for item in items] == [
        {
            "id": "event-notice:sess_a:1",
            "kind": "notice",
            "title": "Conversation event",
            "description": "Unsupported persisted event: future_event",
            "tone": "info",
            "timestamp": "2026-01-01T00:00:01Z",
        }
    ]


def test_viewer_cannot_create_task(monkeypatch: object, tmp_path: Path) -> None:
    with _create_test_client(monkeypatch, tmp_path) as client:
        response = client.post(
            "/api/v1/tasks",
            headers=_auth_headers(client.app, email="viewer@example.com", role="viewer"),
            json={"agent_id": "agent-1", "title": "Do work"},
        )
        assert response.status_code == 403


def test_managed_conversation_queue_mutations_are_read_only(
    monkeypatch: object,
    tmp_path: Path,
) -> None:
    with _create_test_client(monkeypatch, tmp_path) as client:

        async def _seed_managed_conversation() -> str:
            async with client.app.state.session_factory() as session:
                await create_user(
                    session,
                    email="owner@example.com",
                    name="Owner",
                    password_hash=client.app.state.password_hasher.hash("password123"),
                    role="user",
                )
                await create_agent(
                    session,
                    agent_id="controller-agent",
                    owner_email="owner@example.com",
                    name="Controller",
                    status="active",
                )
                await create_agent(
                    session,
                    agent_id="target-agent",
                    owner_email="owner@example.com",
                    name="Target",
                    status="active",
                )
                controller = await create_conversation(
                    session,
                    user_email="owner@example.com",
                    agent_id="controller-agent",
                    context_type="web",
                )
                target = await create_conversation(
                    session,
                    user_email="owner@example.com",
                    agent_id="target-agent",
                    context_type="agent_work",
                )
                await create_managed_conversation_link(
                    session,
                    user_email="owner@example.com",
                    controller_agent_id="controller-agent",
                    controller_conversation_id=controller.conversation_id,
                    controller_session_id="controller-session",
                    target_agent_id="target-agent",
                    target_conversation_id=target.conversation_id,
                    target_session_id="target-session",
                    title="Target",
                )
                await session.commit()
                return target.conversation_id

        conversation_id = asyncio.run(_seed_managed_conversation())
        turn_scheduler = SimpleNamespace(
            queued_messages=lambda _conversation_id: [],
            submit_turn=AsyncMock(),
            update_queued_message=AsyncMock(),
            cancel_queued_message=AsyncMock(),
        )
        client.app.state.turn_scheduler = turn_scheduler
        headers = _auth_headers(client.app, email="owner@example.com")

        get_response = client.get(f"/api/v1/conversations/{conversation_id}/queue", headers=headers)
        patch_response = client.patch(
            f"/api/v1/conversations/{conversation_id}/queue/q-1",
            headers=headers,
            json={"content": "edited"},
        )
        delete_response = client.delete(
            f"/api/v1/conversations/{conversation_id}/queue/q-1",
            headers=headers,
        )
        send_response = client.put(
            f"/api/v1/chat/v2/conversations/{conversation_id}/messages/txn-managed-send",
            headers=headers,
            json={"client_message_id": "cmsg-managed-send", "content": "direct target send"},
        )

        assert get_response.status_code == 200
        assert patch_response.status_code == 409
        assert patch_response.json()["error"]["code"] == "managed_conversation_read_only"
        assert delete_response.status_code == 409
        assert delete_response.json()["error"]["code"] == "managed_conversation_read_only"
        assert send_response.status_code == 409
        assert send_response.json()["error"]["code"] == "managed_conversation_read_only"
        turn_scheduler.submit_turn.assert_not_awaited()
        turn_scheduler.update_queued_message.assert_not_awaited()
        turn_scheduler.cancel_queued_message.assert_not_awaited()


async def _seed_api_managed_conversation(
    client: TestClient,
    *,
    turn_state: str,
    conversation_state: str = "open",
    active_turn_id: str | None = None,
) -> str:
    async with client.app.state.session_factory() as session:
        await create_user(
            session,
            email="owner@example.com",
            name="Owner",
            password_hash=client.app.state.password_hasher.hash("password123"),
            role="user",
        )
        await create_agent(
            session,
            agent_id="controller-agent",
            owner_email="owner@example.com",
            name="Controller",
            status="active",
        )
        await create_agent(
            session,
            agent_id="target-agent",
            owner_email="owner@example.com",
            name="Target",
            status="active",
        )
        controller = await create_conversation(
            session,
            user_email="owner@example.com",
            agent_id="controller-agent",
            context_type="web",
        )
        target = await create_conversation(
            session,
            user_email="owner@example.com",
            agent_id="target-agent",
            context_type="agent_work",
        )
        link = await create_managed_conversation_link(
            session,
            user_email="owner@example.com",
            controller_agent_id="controller-agent",
            controller_conversation_id=controller.conversation_id,
            controller_session_id="controller-session",
            target_agent_id="target-agent",
            target_conversation_id=target.conversation_id,
            target_session_id="target-session",
            title="Target",
            turn_state=turn_state,
        )
        if conversation_state != "open" or active_turn_id is not None:
            await update_managed_conversation_link(
                session,
                link.link_id,
                conversation_state=conversation_state,
                active_turn_id=active_turn_id,
            )
        await session.commit()
        return target.conversation_id


def test_managed_conversation_send_rejects_active_turn(
    monkeypatch: object,
    tmp_path: Path,
) -> None:
    with _create_test_client(monkeypatch, tmp_path) as client:
        conversation_id = asyncio.run(_seed_api_managed_conversation(client, turn_state="running"))
        submit_turn = AsyncMock()
        client.app.state.turn_scheduler = SimpleNamespace(
            has_active_turn=lambda _conversation_id: True,
            submit_turn=submit_turn,
        )
        headers = _auth_headers(client.app, email="owner@example.com")

        response = client.post(
            f"/api/v1/conversations/{conversation_id}/managed/send",
            headers=headers,
            json={"message": "manual instruction"},
        )

        assert response.status_code == 409
        assert response.json()["error"]["code"] == "active_turn_running"
        submit_turn.assert_not_awaited()


def test_managed_conversation_retry_rejects_closed_conversation(
    monkeypatch: object,
    tmp_path: Path,
) -> None:
    with _create_test_client(monkeypatch, tmp_path) as client:
        conversation_id = asyncio.run(
            _seed_api_managed_conversation(
                client,
                turn_state="interrupted",
                conversation_state="closed",
            )
        )
        submit_turn = AsyncMock()
        client.app.state.turn_scheduler = SimpleNamespace(
            has_active_turn=lambda _conversation_id: False,
            submit_turn=submit_turn,
        )
        headers = _auth_headers(client.app, email="owner@example.com")

        response = client.post(
            f"/api/v1/conversations/{conversation_id}/managed/retry",
            headers=headers,
            json={},
        )

        assert response.status_code == 409
        assert response.json()["error"]["code"] == "closed"
        submit_turn.assert_not_awaited()


def test_managed_conversation_retry_rejects_active_turn(
    monkeypatch: object,
    tmp_path: Path,
) -> None:
    with _create_test_client(monkeypatch, tmp_path) as client:
        conversation_id = asyncio.run(
            _seed_api_managed_conversation(
                client,
                turn_state="failed",
                active_turn_id="turn-active",
            )
        )
        submit_turn = AsyncMock()
        client.app.state.turn_scheduler = SimpleNamespace(
            has_active_turn=lambda _conversation_id: True,
            submit_turn=submit_turn,
        )
        headers = _auth_headers(client.app, email="owner@example.com")

        response = client.post(
            f"/api/v1/conversations/{conversation_id}/managed/retry",
            headers=headers,
            json={},
        )

        assert response.status_code == 409
        assert response.json()["error"]["code"] == "active_turn_running"
        submit_turn.assert_not_awaited()


def test_managed_conversation_retry_preserves_one_shot_mode(
    monkeypatch: object,
    tmp_path: Path,
) -> None:
    with _create_test_client(monkeypatch, tmp_path) as client:
        conversation_id = asyncio.run(_seed_api_managed_conversation(client, turn_state="failed"))
        submit_turn = AsyncMock(return_value=None)
        client.app.state.turn_scheduler = SimpleNamespace(
            has_active_turn=lambda _conversation_id: False,
            running_turn_state=lambda _conversation_id: None,
            active_turn_id=lambda _conversation_id: "turn-retry",
            submit_turn=submit_turn,
        )
        client.app.state.session_cache = SimpleNamespace(
            get_events_since_compaction=lambda *_args, **_kwargs: []
        )
        client.app.state.providers.guardrails.read_events = AsyncMock(
            return_value=EventReadResult(
                events=[
                    {
                        "type": "user_message",
                        "data": {
                            "content": "retry this in build mode",
                            "chat_mode": "build",
                            "chat_mode_source": "one_shot",
                        },
                    }
                ],
                last_seq=1,
                has_more=False,
            )
        )
        headers = _auth_headers(client.app, email="owner@example.com")

        response = client.post(
            f"/api/v1/conversations/{conversation_id}/managed/retry",
            headers=headers,
            json={},
        )

        assert response.status_code == 200
        submit_turn.assert_awaited_once_with(
            conversation_id,
            "retry this in build mode",
            user_email="owner@example.com",
            one_shot_chat_mode="build",
        )


def test_managed_conversation_stop_uses_stop_dispatcher_and_marks_manual_cancel(
    monkeypatch: object,
    tmp_path: Path,
) -> None:
    with _create_test_client(monkeypatch, tmp_path) as client:

        async def _seed_managed_conversation() -> str:
            async with client.app.state.session_factory() as session:
                await create_user(
                    session,
                    email="owner@example.com",
                    name="Owner",
                    password_hash=client.app.state.password_hasher.hash("password123"),
                    role="user",
                )
                await create_agent(
                    session,
                    agent_id="controller-agent",
                    owner_email="owner@example.com",
                    name="Controller",
                    status="active",
                )
                await create_agent(
                    session,
                    agent_id="target-agent",
                    owner_email="owner@example.com",
                    name="Target",
                    status="active",
                )
                controller = await create_conversation(
                    session,
                    user_email="owner@example.com",
                    agent_id="controller-agent",
                    context_type="web",
                )
                target = await create_conversation(
                    session,
                    user_email="owner@example.com",
                    agent_id="target-agent",
                    context_type="agent_work",
                )
                await create_managed_conversation_link(
                    session,
                    user_email="owner@example.com",
                    controller_agent_id="controller-agent",
                    controller_conversation_id=controller.conversation_id,
                    controller_session_id="controller-session",
                    target_agent_id="target-agent",
                    target_conversation_id=target.conversation_id,
                    target_session_id="target-session",
                    title="Target",
                    turn_state="running",
                )
                await session.commit()
                return target.conversation_id

        conversation_id = asyncio.run(_seed_managed_conversation())
        stop_conversation = AsyncMock(return_value=True)
        client.app.state.command_dispatcher = SimpleNamespace(stop_conversation=stop_conversation)
        client.app.state.turn_scheduler = SimpleNamespace(
            running_turn_state=lambda _conversation_id: None,
        )
        headers = _auth_headers(client.app, email="owner@example.com")

        response = client.post(
            f"/api/v1/conversations/{conversation_id}/managed/stop",
            headers=headers,
            json={"reason": "manual stop"},
        )

        assert response.status_code == 200
        assert response.json()["status"] == "stopped"
        stop_conversation.assert_awaited_once_with(
            conversation_id,
            user_email="owner@example.com",
        )

        async def _load_metadata() -> dict[str, object]:
            async with client.app.state.session_factory() as session:
                link = await get_managed_conversation_link_for_target(
                    session,
                    conversation_id,
                    user_email="owner@example.com",
                )
                assert link is not None
                return link.control_metadata or {}

        control_metadata = asyncio.run(_load_metadata())
        assert control_metadata["cancelled_by_user"] is True
        assert control_metadata["cancel_source"] == "managed_ui"

        async def _mark_idle() -> None:
            async with client.app.state.session_factory() as session:
                link = await get_managed_conversation_link_for_target(
                    session,
                    conversation_id,
                    user_email="owner@example.com",
                )
                assert link is not None
                await update_managed_conversation_link(
                    session,
                    link.link_id,
                    turn_state="idle",
                )
                await session.commit()

        asyncio.run(_mark_idle())
        client.app.state.turn_scheduler = SimpleNamespace(
            has_active_turn=lambda _conversation_id: False,
            running_turn_state=lambda _conversation_id: None,
            active_turn_id=lambda _conversation_id: None,
            submit_turn=AsyncMock(return_value=None),
        )

        send_response = client.post(
            f"/api/v1/conversations/{conversation_id}/managed/send",
            headers=headers,
            json={"message": "continue"},
        )

        assert send_response.status_code == 200
        control_metadata = asyncio.run(_load_metadata())
        assert "cancelled_by_user" not in control_metadata
        assert "cancel_source" not in control_metadata
        assert "cancelled_at" not in control_metadata


def test_managed_conversation_take_control_creates_normal_fork_and_closes_link(
    monkeypatch: object,
    tmp_path: Path,
) -> None:
    with _create_test_client(monkeypatch, tmp_path) as client:

        async def _seed_managed_conversation() -> str:
            async with client.app.state.session_factory() as session:
                await create_user(
                    session,
                    email="owner@example.com",
                    name="Owner",
                    password_hash=client.app.state.password_hasher.hash("password123"),
                    role="user",
                )
                await create_agent(
                    session,
                    agent_id="controller-agent",
                    owner_email="owner@example.com",
                    name="Controller",
                    status="active",
                )
                await create_agent(
                    session,
                    agent_id="target-agent",
                    owner_email="owner@example.com",
                    name="Target",
                    status="active",
                )
                controller = await create_conversation(
                    session,
                    user_email="owner@example.com",
                    agent_id="controller-agent",
                    context_type="web",
                )
                target = await create_conversation(
                    session,
                    user_email="owner@example.com",
                    agent_id="target-agent",
                    context_type="agent_work",
                    context_data={"kind": "agent_work", "target_agent_id": "target-agent"},
                    title="Managed target",
                )
                target_session = await create_session(
                    session,
                    target.conversation_id,
                    "owner@example.com",
                    "target-agent",
                    session_id="target-session",
                    intaris_session_id="target-session",
                )
                await update_conversation_active_session(
                    session,
                    target.conversation_id,
                    target_session.session_id,
                )
                await create_managed_conversation_link(
                    session,
                    user_email="owner@example.com",
                    controller_agent_id="controller-agent",
                    controller_conversation_id=controller.conversation_id,
                    controller_session_id="controller-session",
                    target_agent_id="target-agent",
                    target_conversation_id=target.conversation_id,
                    target_session_id=target_session.session_id,
                    title="Managed target",
                )
                await session.commit()
                return target.conversation_id

        conversation_id = asyncio.run(_seed_managed_conversation())

        async def _fork_into_normal_conversation(**_kwargs: object) -> tuple[object, object, bool]:
            from cognis.core.session import _to_conversation_model, _to_session_model

            async with client.app.state.session_factory() as session:
                follow_up = await create_conversation(
                    session,
                    user_email="owner@example.com",
                    agent_id="target-agent",
                    context_type="web",
                    title="Follow-up: Managed target",
                )
                follow_up_session = await create_session(
                    session,
                    follow_up.conversation_id,
                    "owner@example.com",
                    "target-agent",
                    session_id="follow-up-session",
                    intaris_session_id="follow-up-session",
                )
                await update_conversation_active_session(
                    session,
                    follow_up.conversation_id,
                    follow_up_session.session_id,
                )
                await session.commit()
                return (
                    _to_conversation_model(follow_up),
                    _to_session_model(follow_up_session),
                    True,
                )

        client.app.state.session_manager.fork_into_new_conversation = AsyncMock(
            side_effect=_fork_into_normal_conversation
        )
        client.app.state.providers.guardrails.record_events = AsyncMock(
            return_value=SimpleNamespace()
        )
        client.app.state.session_cache.append_recorded_events = AsyncMock()
        client.app.state.turn_scheduler = SimpleNamespace(
            has_active_turn=lambda _conversation_id: False,
            running_turn_state=lambda _conversation_id: None,
        )
        headers = _auth_headers(client.app, email="owner@example.com")

        response = client.post(
            f"/api/v1/conversations/{conversation_id}/managed/take-control",
            headers=headers,
            json={},
        )

        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "taken_over"
        follow_up_conversation_id = body["result"]["conversation_id"]
        assert follow_up_conversation_id

        async def _load_state() -> tuple[str, dict[str, object], str]:
            async with client.app.state.session_factory() as session:
                link = await get_managed_conversation_link_for_target(
                    session,
                    conversation_id,
                    user_email="owner@example.com",
                )
                follow_up = await get_conversation(session, follow_up_conversation_id)
                assert link is not None
                assert follow_up is not None
                return link.conversation_state, link.control_metadata or {}, follow_up.context_type

        state, control_metadata, follow_up_context_type = asyncio.run(_load_state())
        assert state == "closed"
        assert control_metadata["follow_up_conversation_id"] == follow_up_conversation_id
        assert control_metadata["closed_reason"] == "taken_over_by_user"
        assert follow_up_context_type == "web"


def test_timeline_projection_includes_managed_takeover_follow_up_link() -> None:
    event = MessageEventResponse(
        type="system_message",
        seq=1,
        timestamp="2026-01-01T00:00:00+00:00",
        data={
            "session_id": "sess_1",
            "content": "User took control in a follow-up conversation.",
            "kind": "managed_takeover",
            "notice_id": "managed_takeover:mconv_1",
            "follow_up_conversation_id": "conv_followup",
            "follow_up_session_id": "sess_followup",
        },
    )

    items = _project_timeline_events([event])

    assert [_strip_order_key(item) for item in items] == [
        {
            "id": "system:managed_takeover:mconv_1",
            "kind": "system_message",
            "text": "User took control in a follow-up conversation.",
            "noticeId": "managed_takeover:mconv_1",
            "noticeKind": "managed_takeover",
            "followUpConversationId": "conv_followup",
            "followUpSessionId": "sess_followup",
            "timestamp": "2026-01-01T00:00:00+00:00",
        }
    ]


def test_session_intaris_detail_prefers_intaris_summary(
    monkeypatch: object,
    tmp_path: Path,
) -> None:
    with _create_test_client(monkeypatch, tmp_path) as client:
        app = client.app

        async def _seed() -> str:
            async with app.state.session_factory() as session:
                await create_user(
                    session,
                    email="user@example.com",
                    name="User",
                    password_hash=app.state.password_hasher.hash("password123"),
                    role="user",
                )
                await create_agent(
                    session,
                    agent_id="agent-1",
                    owner_email="user@example.com",
                    name="Agent 1",
                    status="active",
                )
                conversation = await create_conversation(
                    session,
                    user_email="user@example.com",
                    agent_id="agent-1",
                    context_type="web",
                )
                session_row = await create_session(
                    session,
                    conversation_id=conversation.conversation_id,
                    user_email="user@example.com",
                    agent_id="agent-1",
                )
                await set_session_intaris_session_id(
                    session,
                    session_row.session_id,
                    "intaris-session-1",
                )
                await session.commit()
                return session_row.session_id

        session_id = asyncio.run(_seed())

        original_guardrails = app.state.providers.guardrails
        original_client = original_guardrails.client

        class _Guardrails:
            client = original_client

            async def get_session(self, session_id: str) -> IntarisSession:
                assert session_id == "intaris-session-1"
                return IntarisSession(
                    session_id=session_id,
                    user_id="user@example.com",
                    agent_id="agent-1",
                    title="Intaris title",
                    intention="Intaris intention",
                    status="active",
                    created_at="2026-01-01T00:00:00Z",
                    updated_at="2026-01-01T00:01:00Z",
                )

            async def get_session_summaries(self, session_id: str) -> IntarisSessionSummaries:
                assert session_id == "intaris-session-1"
                return IntarisSessionSummaries(
                    intaris_summaries=[
                        IntarisSessionSummaryRecord(
                            id="summary-1",
                            session_id=session_id,
                            window_start="2026-01-01T00:00:00Z",
                            window_end="2026-01-01T00:01:00Z",
                            trigger="manual",
                            summary="Latest Intaris summary",
                            intent_alignment="aligned",
                            call_count=3,
                            created_at="2026-01-01T00:01:00Z",
                        )
                    ],
                    agent_summaries=[
                        IntarisAgentSummaryRecord(
                            id="agent-summary-1",
                            session_id=session_id,
                            summary="Agent summary",
                            created_at="2026-01-01T00:00:30Z",
                        )
                    ],
                )

        app.state.providers.guardrails = _Guardrails()
        response = client.get(
            f"/api/v1/sessions/{session_id}/intaris",
            headers=_auth_headers(app, email="user@example.com"),
        )

        assert response.status_code == 200
        body = response.json()
        assert body["intaris_session_id"] == "intaris-session-1"
        assert body["intention"] == "Intaris intention"
        assert body["summary"] == "Latest Intaris summary"


def test_session_intaris_detail_falls_back_without_summary(
    monkeypatch: object,
    tmp_path: Path,
) -> None:
    with _create_test_client(monkeypatch, tmp_path) as client:
        app = client.app

        async def _seed() -> str:
            async with app.state.session_factory() as session:
                await create_user(
                    session,
                    email="user@example.com",
                    name="User",
                    password_hash=app.state.password_hasher.hash("password123"),
                    role="user",
                )
                await create_agent(
                    session,
                    agent_id="agent-1",
                    owner_email="user@example.com",
                    name="Agent 1",
                    status="active",
                )
                conversation = await create_conversation(
                    session,
                    user_email="user@example.com",
                    agent_id="agent-1",
                    context_type="web",
                )
                session_row = await create_session(
                    session,
                    conversation_id=conversation.conversation_id,
                    user_email="user@example.com",
                    agent_id="agent-1",
                )
                await session.commit()
                return session_row.session_id

        session_id = asyncio.run(_seed())

        original_guardrails = app.state.providers.guardrails
        original_client = original_guardrails.client

        class _Guardrails:
            client = original_client

            async def get_session(self, session_id: str) -> IntarisSession:
                return IntarisSession(
                    session_id=session_id,
                    user_id="user@example.com",
                    agent_id="agent-1",
                    title=None,
                    intention="Fallback intention",
                    status="active",
                    created_at="2026-01-01T00:00:00Z",
                    updated_at="2026-01-01T00:01:00Z",
                )

            async def get_session_summaries(self, session_id: str) -> IntarisSessionSummaries:
                raise RuntimeError("summary endpoint unavailable")

        app.state.providers.guardrails = _Guardrails()
        response = client.get(
            f"/api/v1/sessions/{session_id}/intaris",
            headers=_auth_headers(app, email="user@example.com"),
        )

        assert response.status_code == 200
        body = response.json()
        assert body["intention"] == "Fallback intention"
        assert body["summary"] is None


def test_batch_submit_returns_per_item_results(monkeypatch: object, tmp_path: Path) -> None:
    with _create_test_client(monkeypatch, tmp_path) as client:
        app = client.app

        async def _seed() -> tuple[str, str]:
            async with app.state.session_factory() as session:
                await create_user(
                    session,
                    email="user@example.com",
                    name="User",
                    password_hash=app.state.password_hasher.hash("password123"),
                    role="user",
                )
                await create_agent(
                    session,
                    agent_id="agent-1",
                    owner_email="user@example.com",
                    name="Agent 1",
                    status="active",
                )
                task_one = await create_task(
                    session,
                    created_by="user@example.com",
                    agent_id="agent-1",
                    title="Draft one",
                    status="draft",
                )
                task_two = await create_task(
                    session,
                    created_by="user@example.com",
                    agent_id="agent-1",
                    title="Draft two",
                    status="draft",
                )
                await session.commit()
                return task_one.task_id, task_two.task_id

        task_one, task_two = asyncio.run(_seed())
        response = client.post(
            "/api/v1/tasks/batch-submit",
            headers=_auth_headers(app, email="user@example.com"),
            json={"task_ids": [task_one, task_two, "missing-task"]},
        )
        assert response.status_code == 200
        body = response.json()
        assert body["succeeded"] == 2
        assert body["failed"] == 1
        assert any(
            item["task_id"] == "missing-task" and item["status"] == "error"
            for item in body["results"]
        )


def test_task_list_uses_keyset_cursor(monkeypatch: object, tmp_path: Path) -> None:
    with _create_test_client(monkeypatch, tmp_path) as client:
        app = client.app

        async def _seed() -> None:
            async with app.state.session_factory() as session:
                await create_user(
                    session,
                    email="user@example.com",
                    name="User",
                    password_hash=app.state.password_hasher.hash("password123"),
                    role="user",
                )
                await create_agent(
                    session,
                    agent_id="agent-1",
                    owner_email="user@example.com",
                    name="Agent 1",
                    status="active",
                )
                now = datetime.now(UTC)
                for index in range(3):
                    task = await create_task(
                        session,
                        created_by="user@example.com",
                        agent_id="agent-1",
                        title=f"Task {index}",
                        status="queued",
                    )
                    task.task_id = f"cursor-task-{index}"
                    task.updated_at = now - timedelta(minutes=index)
                await session.commit()

        asyncio.run(_seed())

        first = client.get(
            "/api/v1/tasks?limit=2",
            headers=_auth_headers(app, email="user@example.com"),
        )
        assert first.status_code == 200
        first_body = first.json()
        assert [item["task_id"] for item in first_body["items"]] == [
            "cursor-task-0",
            "cursor-task-1",
        ]
        assert first_body["has_more"] is True
        assert first_body["cursor"]

        second = client.get(
            f"/api/v1/tasks?limit=2&cursor={first_body['cursor']}",
            headers=_auth_headers(app, email="user@example.com"),
        )
        assert second.status_code == 200
        second_body = second.json()
        assert [item["task_id"] for item in second_body["items"]] == ["cursor-task-2"]
        assert second_body["has_more"] is False


def test_task_board_limits_columns_and_pages_independently(
    monkeypatch: object,
    tmp_path: Path,
) -> None:
    with _create_test_client(monkeypatch, tmp_path) as client:
        app = client.app

        async def _seed() -> None:
            async with app.state.session_factory() as session:
                await create_user(
                    session,
                    email="user@example.com",
                    name="User",
                    password_hash=app.state.password_hasher.hash("password123"),
                    role="user",
                )
                await create_agent(
                    session,
                    agent_id="agent-1",
                    owner_email="user@example.com",
                    name="Agent 1",
                    status="active",
                )
                now = datetime.now(UTC)
                for index in range(3):
                    task = await create_task(
                        session,
                        created_by="user@example.com",
                        agent_id="agent-1",
                        title=f"Queued {index}",
                        status="queued",
                    )
                    task.task_id = f"board-queued-{index}"
                    task.updated_at = now - timedelta(minutes=index)
                done = await create_task(
                    session,
                    created_by="user@example.com",
                    agent_id="agent-1",
                    title="Done",
                    status="completed",
                )
                done.task_id = "board-done-0"
                done.updated_at = now - timedelta(hours=1)
                done_older_same_group = await create_task(
                    session,
                    created_by="user@example.com",
                    agent_id="agent-1",
                    title="Done",
                    status="completed",
                )
                done_older_same_group.task_id = "board-done-1"
                done_older_same_group.updated_at = now - timedelta(hours=2)
                await session.commit()

        asyncio.run(_seed())

        response = client.get(
            "/api/v1/tasks/board?limit=2",
            headers=_auth_headers(app, email="user@example.com"),
        )
        assert response.status_code == 200
        body = response.json()

        queued = body["columns"]["queued"]
        assert [item["task_id"] for item in queued["items"]] == [
            "board-queued-0",
            "board-queued-1",
        ]
        assert queued["has_more"] is True
        assert queued["total_count"] == 3
        assert queued["cursor"]

        done = body["columns"]["done"]
        assert [item["task_id"] for item in done["items"]] == ["board-done-0"]
        assert [group["latest"]["task_id"] for group in done["groups"]] == ["board-done-0"]
        assert done["groups"][0]["task_count"] == 2
        assert done["total_count"] == 1
        group_key = quote(done["groups"][0]["key"], safe="")
        group_history = client.get(
            f"/api/v1/tasks/board/done/groups/{group_key}/tasks?limit=1",
            headers=_auth_headers(app, email="user@example.com"),
        )
        assert group_history.status_code == 200
        group_history_body = group_history.json()
        assert [item["task_id"] for item in group_history_body["items"]] == ["board-done-0"]
        assert group_history_body["has_more"] is True
        assert group_history_body["cursor"]

        next_group_history = client.get(
            f"/api/v1/tasks/board/done/groups/{group_key}/tasks?limit=1&cursor={group_history_body['cursor']}",
            headers=_auth_headers(app, email="user@example.com"),
        )
        assert next_group_history.status_code == 200
        assert [item["task_id"] for item in next_group_history.json()["items"]] == ["board-done-1"]

        next_page = client.get(
            f"/api/v1/tasks/board/queued?limit=2&cursor={queued['cursor']}",
            headers=_auth_headers(app, email="user@example.com"),
        )
        assert next_page.status_code == 200
        assert [item["task_id"] for item in next_page.json()["items"]] == ["board-queued-2"]


def test_task_detail_projection_endpoints_omit_heavy_step_payloads(
    monkeypatch: object,
    tmp_path: Path,
) -> None:
    with _create_test_client(monkeypatch, tmp_path) as client:
        app = client.app

        async def _seed() -> str:
            async with app.state.session_factory() as session:
                await create_user(
                    session,
                    email="user@example.com",
                    name="User",
                    password_hash=app.state.password_hasher.hash("password123"),
                    role="user",
                )
                await create_agent(
                    session,
                    agent_id="agent-1",
                    owner_email="user@example.com",
                    name="Agent 1",
                    status="active",
                )
                task = await create_task(
                    session,
                    created_by="user@example.com",
                    agent_id="agent-1",
                    title="Projected task",
                    status="running",
                )
                first = await create_step_run(
                    session,
                    task_id=task.task_id,
                    step_run_id="projection-step-1",
                    step_name="plan",
                    step_type="run",
                    agent_id="agent-1",
                    attempt=1,
                    attempt_number=1,
                    status="approved",
                    runtime_info={"executor_id": "executor-1", "large": "x" * 1000},
                    completed_at=datetime.now(UTC) - timedelta(minutes=10),
                )
                first.output = {"summary": "first", "content": "heavy first content"}
                second = await create_step_run(
                    session,
                    task_id=task.task_id,
                    step_run_id="projection-step-2",
                    step_name="plan",
                    step_type="run",
                    agent_id="agent-1",
                    attempt=2,
                    attempt_number=2,
                    status="approved",
                    runtime_info={"executor_id": "executor-1", "large": "y" * 1000},
                    completed_at=datetime.now(UTC),
                    deliverable_id="projection-deliverable",
                )
                second.output = {"summary": "second", "content": "heavy second content"}
                await create_deliverable(
                    session,
                    step_run_id=second.step_run_id,
                    deliverable_id="projection-deliverable",
                    title="Projection deliverable",
                    content="heavy deliverable content",
                )
                await session.commit()
                return task.task_id

        task_id = asyncio.run(_seed())
        headers = _auth_headers(app, email="user@example.com")

        summary = client.get(f"/api/v1/tasks/{task_id}/summary", headers=headers)
        assert summary.status_code == 200
        assert summary.json()["step_runs"] == []

        steps = client.get(f"/api/v1/tasks/{task_id}/steps/summary", headers=headers)
        assert steps.status_code == 200
        body = steps.json()
        assert [item["step_run_id"] for item in body["items"]] == ["projection-step-2"]
        projected = body["items"][0]
        assert projected["output"] is None
        assert projected["runtime_info"] is None
        assert projected["deliverables"] == []
        assert projected["deliverable_id"] == "projection-deliverable"
        assert projected["is_projection"] is True

        history = client.get(
            f"/api/v1/tasks/{task_id}/steps/plan/summary?limit=1",
            headers=headers,
        )
        assert history.status_code == 200
        history_body = history.json()
        assert [item["step_run_id"] for item in history_body["items"]] == ["projection-step-1"]
        assert history_body["has_more"] is True

        full_step = client.get("/api/v1/step-runs/projection-step-2", headers=headers)
        assert full_step.status_code == 200
        full_body = full_step.json()
        assert full_body["output"]["content"] == "heavy second content"
        assert full_body["runtime_info"]["large"].startswith("y")
        assert full_body["deliverables"][0]["content"] == "heavy deliverable content"
        assert full_body["is_projection"] is False


def test_gate_response_conflict_when_already_resolved(monkeypatch: object, tmp_path: Path) -> None:
    with _create_test_client(monkeypatch, tmp_path) as client:
        app = client.app

        async def _seed() -> str:
            async with app.state.session_factory() as session:
                await create_user(
                    session,
                    email="user@example.com",
                    name="User",
                    password_hash=app.state.password_hasher.hash("password123"),
                    role="user",
                )
                await create_agent(
                    session,
                    agent_id="agent-1",
                    owner_email="user@example.com",
                    name="Agent 1",
                    status="active",
                )
                task = await create_task(
                    session,
                    created_by="user@example.com",
                    agent_id="agent-1",
                    title="Paused task",
                    status="paused",
                )
                await session.commit()
                return task.task_id

        task_id = asyncio.run(_seed())
        app.state.pause_waiter.register(
            PendingPause(
                pause_id="gate_1",
                pause_type="gate",
                task_id=task_id,
                step_name="review",
                question="Approve?",
                options=[{"label": "Continue", "action": "continue"}],
            )
        )

        first = client.post(
            f"/api/v1/tasks/{task_id}/gate-response",
            headers=_auth_headers(app, email="user@example.com"),
            json={"step_name": "review", "action": "continue"},
        )
        second = client.post(
            f"/api/v1/tasks/{task_id}/gate-response",
            headers=_auth_headers(app, email="user@example.com"),
            json={"step_name": "review", "action": "continue"},
        )
        assert first.status_code == 200
        assert second.status_code == 409


def test_task_mutation_rejects_non_owner(monkeypatch: object, tmp_path: Path) -> None:
    with _create_test_client(monkeypatch, tmp_path) as client:
        app = client.app

        async def _seed() -> str:
            async with app.state.session_factory() as session:
                await create_user(
                    session,
                    email="owner@example.com",
                    name="Owner",
                    password_hash=app.state.password_hasher.hash("password123"),
                    role="user",
                )
                await create_agent(
                    session,
                    agent_id="agent-1",
                    owner_email="owner@example.com",
                    name="Agent 1",
                    status="active",
                )
                task = await create_task(
                    session,
                    created_by="owner@example.com",
                    agent_id="agent-1",
                    title="Private task",
                    status="draft",
                )
                await session.commit()
                return task.task_id

        task_id = asyncio.run(_seed())
        response = client.post(
            f"/api/v1/tasks/{task_id}/cancel",
            headers=_auth_headers(app, email="attacker@example.com"),
        )
        assert response.status_code == 404


def test_task_rerun_returns_new_task_target(monkeypatch: object, tmp_path: Path) -> None:
    with _create_test_client(monkeypatch, tmp_path) as client:
        app = client.app

        async def _seed() -> str:
            async with app.state.session_factory() as session:
                await create_user(
                    session,
                    email="user@example.com",
                    name="User",
                    password_hash=app.state.password_hasher.hash("password123"),
                    role="user",
                )
                await create_agent(
                    session,
                    agent_id="agent-1",
                    owner_email="user@example.com",
                    name="Agent 1",
                    status="active",
                )
                task = await create_task(
                    session,
                    created_by="user@example.com",
                    agent_id="agent-1",
                    title="Completed task",
                    status="completed",
                )
                await session.commit()
                return task.task_id

        task_id = asyncio.run(_seed())

        async def _fake_rerun(task_id: str) -> TaskRerunResult:
            return TaskRerunResult(
                source_task_id=task_id,
                task=TaskModel(
                    task_id="task_clone",
                    title="Completed task",
                    description="",
                    status=TaskStatus.QUEUED,
                    priority=0,
                    created_by="user@example.com",
                    agent_id="agent-1",
                    source_type="api",
                    source_ref=None,
                    delivery=TaskDelivery(),
                    workflow_id=None,
                    workflow_state=WorkflowState(),
                ),
                created_new=True,
            )

        app.state.task_queue.rerun_task = _fake_rerun

        response = client.post(
            f"/api/v1/tasks/{task_id}/rerun",
            headers=_auth_headers(app, email="user@example.com"),
        )

        assert response.status_code == 200
        assert response.json() == {
            "ok": True,
            "source_task_id": task_id,
            "task_id": "task_clone",
            "status": "queued",
            "created_new": True,
        }


def test_gate_response_returns_conflict_for_unsupported_action(
    monkeypatch: object, tmp_path: Path
) -> None:
    with _create_test_client(monkeypatch, tmp_path) as client:
        app = client.app

        async def _seed() -> str:
            async with app.state.session_factory() as session:  # type: ignore[attr-defined]
                await create_user(
                    session,
                    email="user@example.com",
                    name="User",
                    password_hash=app.state.password_hasher.hash("password123"),
                    role="user",
                )
                agent = await create_agent(
                    session,
                    owner_email="user@example.com",
                    agent_id="agent-unsupported-gate",
                    name="Unsupported Gate Agent",
                )
                task = await create_task(
                    session,
                    created_by="user@example.com",
                    agent_id=agent.agent_id,
                    title="Paused task",
                    status="paused",
                )
                await session.commit()
                return task.task_id

        task_id = asyncio.run(_seed())
        app.state.pause_waiter.register(
            PendingPause(
                pause_id="gate_conflict",
                pause_type="gate",
                task_id=task_id,
                step_name="review",
                options=[{"label": "Continue", "action": "continue"}],
            )
        )

        response = client.post(
            f"/api/v1/tasks/{task_id}/gate-response",
            headers=_auth_headers(app, email="user@example.com"),
            json={"step_name": "review", "action": "cancel"},
        )

        assert response.status_code == 409


def test_task_create_allows_non_chat_source_refs(monkeypatch: object, tmp_path: Path) -> None:
    with _create_test_client(monkeypatch, tmp_path) as client:
        app = client.app

        async def _seed() -> None:
            async with app.state.session_factory() as session:
                await create_user(
                    session,
                    email="user@example.com",
                    name="User",
                    password_hash=app.state.password_hasher.hash("password123"),
                    role="user",
                )
                await create_agent(
                    session,
                    agent_id="agent-1",
                    owner_email="user@example.com",
                    name="Agent 1",
                    status="active",
                )
                await session.commit()

        asyncio.run(_seed())
        response = client.post(
            "/api/v1/tasks",
            headers=_auth_headers(app, email="user@example.com"),
            json={
                "agent_id": "agent-1",
                "title": "Scheduled task",
                "source_type": "scheduler",
                "source_ref": "sched_daily_review",
            },
        )
        assert response.status_code == 200
        body = response.json()
        assert body["source_type"] == "scheduler"
        assert body["source_ref"] == "sched_daily_review"
        assert body["delivery"]["mode"] == "preferred_channel"


def test_task_create_rejects_explicit_creator_agent_without_side_effects() -> None:
    async def _fail(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("task_create should reject before opening a task transaction")

    request = SimpleNamespace(
        state=SimpleNamespace(
            session_factory=_fail,
            user=AuthenticatedUser(email="user@example.com", role="user"),
        )
    )
    payload = TaskCreateRequest(
        agent_id="agent-1",
        created_by_agent_id="agent-1",
        title="Invalid explicit creator",
    )

    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(task_create(request, payload))

    assert exc_info.value.status_code == 400


def test_task_create_rejects_same_conversation_outside_chat(
    monkeypatch: object, tmp_path: Path
) -> None:
    with _create_test_client(monkeypatch, tmp_path) as client:
        app = client.app

        async def _seed() -> None:
            async with app.state.session_factory() as session:
                await create_user(
                    session,
                    email="user@example.com",
                    name="User",
                    password_hash=app.state.password_hasher.hash("password123"),
                    role="user",
                )
                await create_agent(
                    session,
                    agent_id="agent-1",
                    owner_email="user@example.com",
                    name="Agent 1",
                    status="active",
                )
                await session.commit()

        asyncio.run(_seed())
        response = client.post(
            "/api/v1/tasks",
            headers=_auth_headers(app, email="user@example.com"),
            json={
                "agent_id": "agent-1",
                "title": "Board task",
                "delivery_mode": "same_conversation",
            },
        )
        assert response.status_code == 400


def test_task_create_rejects_unknown_delivery_mode(monkeypatch: object, tmp_path: Path) -> None:
    with _create_test_client(monkeypatch, tmp_path) as client:
        app = client.app

        async def _seed() -> None:
            async with app.state.session_factory() as session:
                await create_user(
                    session,
                    email="user@example.com",
                    name="User",
                    password_hash=app.state.password_hasher.hash("password123"),
                    role="user",
                )
                await create_agent(
                    session,
                    agent_id="agent-1",
                    owner_email="user@example.com",
                    name="Agent 1",
                    status="active",
                )
                await session.commit()

        asyncio.run(_seed())
        response = client.post(
            "/api/v1/tasks",
            headers=_auth_headers(app, email="user@example.com"),
            json={
                "agent_id": "agent-1",
                "title": "Board task",
                "delivery_mode": "typo",
            },
        )
        assert response.status_code == 400


def test_task_create_rejects_chat_without_source_ref(monkeypatch: object, tmp_path: Path) -> None:
    with _create_test_client(monkeypatch, tmp_path) as client:
        app = client.app

        async def _seed() -> None:
            async with app.state.session_factory() as session:
                await create_user(
                    session,
                    email="user@example.com",
                    name="User",
                    password_hash=app.state.password_hasher.hash("password123"),
                    role="user",
                )
                await create_agent(
                    session,
                    agent_id="agent-1",
                    owner_email="user@example.com",
                    name="Agent 1",
                    status="active",
                )
                await session.commit()

        asyncio.run(_seed())
        response = client.post(
            "/api/v1/tasks",
            headers=_auth_headers(app, email="user@example.com"),
            json={
                "agent_id": "agent-1",
                "title": "Delegated task",
                "source_type": "chat",
            },
        )
        assert response.status_code == 400


def test_task_update_validates_specific_conversation_target(
    monkeypatch: object, tmp_path: Path
) -> None:
    with _create_test_client(monkeypatch, tmp_path) as client:
        app = client.app

        async def _seed() -> tuple[str, str]:
            async with app.state.session_factory() as session:
                await create_user(
                    session,
                    email="owner@example.com",
                    name="Owner",
                    password_hash=app.state.password_hasher.hash("password123"),
                    role="user",
                )
                await create_user(
                    session,
                    email="other@example.com",
                    name="Other",
                    password_hash=app.state.password_hasher.hash("password123"),
                    role="user",
                )
                await create_agent(
                    session,
                    agent_id="agent-1",
                    owner_email="owner@example.com",
                    name="Agent 1",
                    status="active",
                )
                foreign_conversation = await create_conversation(
                    session,
                    user_email="other@example.com",
                    agent_id="agent-1",
                    context_type="web",
                    title="Foreign",
                )
                task = await create_task(
                    session,
                    created_by="owner@example.com",
                    agent_id="agent-1",
                    title="Specific delivery",
                    status="draft",
                    delivery_mode="specific_conversation",
                )
                await session.commit()
                return task.task_id, foreign_conversation.conversation_id

        task_id, foreign_conversation_id = asyncio.run(_seed())
        response = client.patch(
            f"/api/v1/tasks/{task_id}",
            headers=_auth_headers(app, email="owner@example.com"),
            json={"delivery_target": foreign_conversation_id},
        )
        assert response.status_code == 403


def test_task_update_rejects_specific_conversation_without_target(
    monkeypatch: object, tmp_path: Path
) -> None:
    with _create_test_client(monkeypatch, tmp_path) as client:
        app = client.app

        async def _seed() -> str:
            async with app.state.session_factory() as session:
                await create_user(
                    session,
                    email="owner@example.com",
                    name="Owner",
                    password_hash=app.state.password_hasher.hash("password123"),
                    role="user",
                )
                await create_agent(
                    session,
                    agent_id="agent-1",
                    owner_email="owner@example.com",
                    name="Agent 1",
                    status="active",
                )
                task = await create_task(
                    session,
                    created_by="owner@example.com",
                    agent_id="agent-1",
                    title="Missing target",
                    status="draft",
                )
                await session.commit()
                return task.task_id

        task_id = asyncio.run(_seed())
        response = client.patch(
            f"/api/v1/tasks/{task_id}",
            headers=_auth_headers(app, email="owner@example.com"),
            json={"delivery_mode": "specific_conversation"},
        )
        assert response.status_code == 400


def test_step_response_resumes_recovered_step_input(monkeypatch: object, tmp_path: Path) -> None:
    with _create_test_client(monkeypatch, tmp_path) as client:
        app = client.app

        async def _seed() -> str:
            async with app.state.session_factory() as session:
                await create_user(
                    session,
                    email="user@example.com",
                    name="User",
                    password_hash=app.state.password_hasher.hash("password123"),
                    role="user",
                )
                await create_agent(
                    session,
                    agent_id="agent-1",
                    owner_email="user@example.com",
                    name="Agent 1",
                    status="active",
                )
                task = await create_task(
                    session,
                    created_by="user@example.com",
                    agent_id="agent-1",
                    title="Paused question",
                    status="paused",
                    workflow_state={
                        "current_step_index": 0,
                        "status": "paused",
                        "pending_pause_type": "step_input",
                        "pending_pause_payload": {
                            "pause_id": "input_recovered",
                            "step_name": "plan",
                            "questions": [
                                {
                                    "id": "q1",
                                    "question": "Need input",
                                    "options": [
                                        {"id": "A", "label": "A"},
                                        {"id": "B", "label": "B"},
                                    ],
                                    "multiple": False,
                                    "allow_custom": True,
                                    "required": True,
                                }
                            ],
                        },
                    },
                )
                await session.commit()
                return task.task_id

        task_id = asyncio.run(_seed())
        asyncio.run(app.state.task_queue.recover_paused_tasks())

        called: dict[str, bool] = {"resume": False}

        async def _fake_resume(task_id: str) -> TaskModel:
            called["resume"] = True
            return TaskModel(
                task_id=task_id,
                title="Paused question",
                description="",
                status=TaskStatus.RUNNING,
                priority=0,
                created_by="user@example.com",
                agent_id="agent-1",
                source_type="api",
                source_ref=None,
                delivery=TaskDelivery(),
                workflow_id=None,
                workflow_state=WorkflowState(),
            )

        app.state.task_queue.resume_task = _fake_resume

        response = client.post(
            f"/api/v1/tasks/{task_id}/step-response",
            headers=_auth_headers(app, email="user@example.com"),
            json={
                "step_name": "plan",
                "mode": "structured",
                "answers": [
                    {
                        "question_id": "q1",
                        "selected_option_ids": ["A"],
                        "custom_answer": None,
                    }
                ],
            },
        )
        assert response.status_code == 200
        assert called["resume"] is True


def test_websocket_step_response_surfaces_resume_conflict(
    monkeypatch: object, tmp_path: Path
) -> None:
    with _create_test_client(monkeypatch, tmp_path) as client:
        app = client.app

        async def _seed() -> str:
            async with app.state.session_factory() as session:
                await create_user(
                    session,
                    email="user@example.com",
                    name="User",
                    password_hash=app.state.password_hasher.hash("password123"),
                    role="user",
                )
                await create_agent(
                    session,
                    agent_id="agent-1",
                    owner_email="user@example.com",
                    name="Agent 1",
                    status="active",
                )
                task = await create_task(
                    session,
                    created_by="user@example.com",
                    agent_id="agent-1",
                    title="Paused question",
                    status="paused",
                    workflow_state={
                        "current_step_index": 0,
                        "status": "paused",
                        "pending_pause_type": "step_input",
                        "pending_pause_payload": {
                            "pause_id": "input_ws_conflict",
                            "step_name": "plan",
                            "questions": [
                                {
                                    "id": "q1",
                                    "question": "Need input",
                                    "options": [
                                        {"id": "A", "label": "A"},
                                        {"id": "B", "label": "B"},
                                    ],
                                    "multiple": False,
                                    "allow_custom": True,
                                    "required": True,
                                }
                            ],
                        },
                    },
                )
                await session.commit()
                return task.task_id

        task_id = asyncio.run(_seed())
        asyncio.run(app.state.task_queue.recover_paused_tasks())

        async def _resume_conflict(task_id: str) -> TaskModel:
            raise ValueError("No execution capacity available to resume the task")

        app.state.task_queue.resume_task = _resume_conflict

        with client.websocket_connect("/api/ws") as ws:
            ws.send_json(
                {
                    "type": "auth",
                    "token": _auth_headers(app, email="user@example.com")[
                        "Authorization"
                    ].removeprefix("Bearer "),
                }
            )
            assert ws.receive_json()["type"] == "authenticated"
            ws.send_json(
                {
                    "type": "step_response",
                    "task_id": task_id,
                    "step_name": "plan",
                    "mode": "structured",
                    "answers": [
                        {
                            "question_id": "q1",
                            "selected_option_ids": ["A"],
                            "custom_answer": None,
                        }
                    ],
                }
            )
            payload = ws.receive_json()
            assert payload["type"] == "error"
            assert payload["code"] == "conflict"


def test_websocket_direct_chat_step_response_resolves_notification(
    monkeypatch: object, tmp_path: Path
) -> None:
    with _create_test_client(monkeypatch, tmp_path) as client:
        app = client.app

        async def _seed() -> tuple[str, str]:
            async with app.state.session_factory() as session:
                await create_user(
                    session,
                    email="user@example.com",
                    name="User",
                    password_hash=app.state.password_hasher.hash("password123"),
                    role="user",
                )
                await create_agent(
                    session,
                    agent_id="agent-1",
                    owner_email="user@example.com",
                    name="Agent 1",
                    status="active",
                )
                conversation = await create_conversation(
                    session,
                    user_email="user@example.com",
                    agent_id="agent-1",
                    context_type="web",
                    title="Conversation",
                )
                session_row = await create_session(
                    session,
                    conversation_id=conversation.conversation_id,
                    user_email="user@example.com",
                    agent_id="agent-1",
                )
                await session.commit()
                return conversation.conversation_id, session_row.session_id

        conversation_id, session_id = asyncio.run(_seed())
        notification = asyncio.run(
            app.state.notification_service.create(
                notification_type="step_question",
                user_email="user@example.com",
                conversation_id=conversation_id,
                session_id=session_id,
                notification_id="notif_direct_ok",
                payload={
                    "questions": [
                        {
                            "id": "q1",
                            "question": "Need input",
                            "options": [
                                {"id": "A", "label": "A"},
                                {"id": "B", "label": "B"},
                            ],
                            "multiple": False,
                            "allow_custom": True,
                            "required": True,
                        }
                    ]
                },
            )
        )

        class _Manager:
            def __init__(self) -> None:
                self.errors: list[dict[str, object]] = []

            async def send_error(self, _: object, **kwargs: object) -> None:
                self.errors.append(kwargs)

        manager = _Manager()
        connection = AuthenticatedWebSocket(
            connection_id="conn-1",
            websocket=object(),
            user_email="user@example.com",
            role="user",
        )

        asyncio.run(
            _handle_step_response(
                app,
                manager,
                connection,
                {
                    "type": "step_response",
                    "notification_id": notification.notification_id,
                    "mode": "structured",
                    "answers": [
                        {
                            "question_id": "q1",
                            "selected_option_ids": ["A"],
                            "custom_answer": None,
                        }
                    ],
                },
            )
        )
        asyncio.run(
            _handle_step_response(
                app,
                manager,
                connection,
                {
                    "type": "step_response",
                    "notification_id": notification.notification_id,
                    "mode": "structured",
                    "answers": [
                        {
                            "question_id": "q1",
                            "selected_option_ids": ["B"],
                            "custom_answer": None,
                        }
                    ],
                },
            )
        )
        assert manager.errors[-1]["code"] == "conflict"

        resolved = asyncio.run(app.state.notification_service.get(notification.notification_id))
        assert resolved is not None
        assert resolved.status == "resolved"
        assert resolved.resolution == {
            "decision": "continue",
            "answers": [
                {
                    "question_id": "q1",
                    "selected_option_ids": ["A"],
                    "custom_answer": None,
                }
            ],
            "mode": "structured",
            "state": "resolved",
        }


def test_websocket_direct_chat_step_response_resolves_auth_challenge(
    monkeypatch: object, tmp_path: Path
) -> None:
    with _create_test_client(monkeypatch, tmp_path) as client:
        app = client.app

        async def _seed() -> tuple[str, str]:
            async with app.state.session_factory() as session:
                await create_user(
                    session,
                    email="user@example.com",
                    name="User",
                    password_hash=app.state.password_hasher.hash("password123"),
                    role="user",
                )
                await create_agent(
                    session,
                    agent_id="agent-1",
                    owner_email="user@example.com",
                    name="Agent 1",
                    status="active",
                )
                conversation = await create_conversation(
                    session,
                    user_email="user@example.com",
                    agent_id="agent-1",
                    context_type="web",
                    title="Conversation",
                )
                session_row = await create_session(
                    session,
                    conversation_id=conversation.conversation_id,
                    user_email="user@example.com",
                    agent_id="agent-1",
                )
                await session.commit()
                return conversation.conversation_id, session_row.session_id

        conversation_id, session_id = asyncio.run(_seed())
        notification = asyncio.run(
            app.state.notification_service.create(
                notification_type="auth_challenge",
                user_email="user@example.com",
                conversation_id=conversation_id,
                session_id=session_id,
                notification_id="auth_direct_ok",
                payload={"kind": "otp_code", "required_fields": ["code"], "message": "OTP"},
            )
        )

        class _Manager:
            def __init__(self) -> None:
                self.errors: list[dict[str, object]] = []

            async def send_error(self, _: object, **kwargs: object) -> None:
                self.errors.append(kwargs)

        manager = _Manager()
        connection = AuthenticatedWebSocket(
            connection_id="conn-1",
            websocket=object(),
            user_email="user@example.com",
            role="user",
        )

        asyncio.run(
            _handle_step_response(
                app,
                manager,
                connection,
                {
                    "type": "step_response",
                    "notification_id": notification.notification_id,
                    "response": "123456",
                },
            )
        )

        assert manager.errors == []
        resolved = asyncio.run(app.state.notification_service.get(notification.notification_id))
        assert resolved is not None
        assert resolved.status == "resolved"
        assert resolved.resolution is not None
        assert resolved.resolution["decision"] == "continue"
        assert resolved.resolution["challenge_completed"] is True
        assert str(resolved.resolution["response_ref"]).startswith(
            "$credential:challenge_auth_direct_ok"
        )


def test_websocket_escalation_resolve_tolerates_same_decision_duplicate() -> None:
    class _NotificationService:
        async def resolve(
            self,
            notification_id: str,
            decision: str,
            data: dict[str, object],
            *,
            user_email: str | None = None,
        ) -> bool:
            assert notification_id == "call-1"
            assert decision == "approve"
            assert data == {"note": ""}
            assert user_email == "user@example.com"
            return False

        async def get(self, notification_id: str) -> object:
            assert notification_id == "call-1"
            return SimpleNamespace(
                status="resolved",
                resolution={"decision": "approve", "state": "resolved_remote"},
            )

    class _Manager:
        def __init__(self) -> None:
            self.errors: list[dict[str, object]] = []
            self.messages: list[tuple[str, dict[str, object]]] = []

        async def send_error(self, _: object, **kwargs: object) -> None:
            self.errors.append(kwargs)

        async def send_to_conversation(
            self, conversation_id: str, payload: dict[str, object]
        ) -> None:
            self.messages.append((conversation_id, payload))

    app = SimpleNamespace(
        state=SimpleNamespace(
            pause_waiter=SimpleNamespace(find_pending=lambda **_: None),
            notification_service=_NotificationService(),
        )
    )
    manager = _Manager()
    connection = AuthenticatedWebSocket(
        connection_id="conn-1",
        websocket=object(),
        user_email="user@example.com",
        role="user",
    )

    asyncio.run(
        _handle_resolve_escalation(
            app,
            manager,  # type: ignore[arg-type]
            connection,
            {"type": "resolve_escalation", "call_id": "call-1", "decision": "approve"},
        )
    )

    assert manager.errors == []
    assert manager.messages == []


def test_websocket_direct_chat_step_response_conflicts_without_live_pause(
    monkeypatch: object, tmp_path: Path
) -> None:
    with _create_test_client(monkeypatch, tmp_path) as client:
        app = client.app

        async def _seed() -> tuple[str, str]:
            async with app.state.session_factory() as session:
                await create_user(
                    session,
                    email="user@example.com",
                    name="User",
                    password_hash=app.state.password_hasher.hash("password123"),
                    role="user",
                )
                await create_agent(
                    session,
                    agent_id="agent-1",
                    owner_email="user@example.com",
                    name="Agent 1",
                    status="active",
                )
                conversation = await create_conversation(
                    session,
                    user_email="user@example.com",
                    agent_id="agent-1",
                    context_type="web",
                    title="Conversation",
                )
                session_row = await create_session(
                    session,
                    conversation_id=conversation.conversation_id,
                    user_email="user@example.com",
                    agent_id="agent-1",
                )
                await session.commit()
                return conversation.conversation_id, session_row.session_id

        conversation_id, session_id = asyncio.run(_seed())
        notification = asyncio.run(
            app.state.notification_service.create(
                notification_type="step_question",
                user_email="user@example.com",
                conversation_id=conversation_id,
                session_id=session_id,
                notification_id="notif_direct_orphan",
                payload={
                    "questions": [
                        {
                            "id": "q1",
                            "question": "Need input",
                            "options": [{"id": "A", "label": "A"}],
                            "multiple": False,
                            "allow_custom": True,
                            "required": True,
                        }
                    ]
                },
            )
        )
        app.state.pause_waiter.clear(notification.notification_id)

        class _Manager:
            def __init__(self) -> None:
                self.errors: list[dict[str, object]] = []

            async def send_error(self, _: object, **kwargs: object) -> None:
                self.errors.append(kwargs)

        manager = _Manager()
        connection = AuthenticatedWebSocket(
            connection_id="conn-1",
            websocket=object(),
            user_email="user@example.com",
            role="user",
        )

        asyncio.run(
            _handle_step_response(
                app,
                manager,
                connection,
                {
                    "type": "step_response",
                    "notification_id": notification.notification_id,
                    "mode": "structured",
                    "answers": [
                        {
                            "question_id": "q1",
                            "selected_option_ids": ["A"],
                            "custom_answer": None,
                        }
                    ],
                },
            )
        )
        assert manager.errors[-1]["code"] == "conflict"


def test_websocket_step_response_rejects_mismatched_task_and_notification(
    monkeypatch: object, tmp_path: Path
) -> None:
    with _create_test_client(monkeypatch, tmp_path) as client:
        app = client.app

        async def _seed() -> tuple[str, str, str]:
            async with app.state.session_factory() as session:
                await create_user(
                    session,
                    email="user@example.com",
                    name="User",
                    password_hash=app.state.password_hasher.hash("password123"),
                    role="user",
                )
                await create_agent(
                    session,
                    agent_id="agent-1",
                    owner_email="user@example.com",
                    name="Agent 1",
                    status="active",
                )
                conversation = await create_conversation(
                    session,
                    user_email="user@example.com",
                    agent_id="agent-1",
                    context_type="web",
                    title="Conversation",
                )
                session_row = await create_session(
                    session,
                    conversation_id=conversation.conversation_id,
                    user_email="user@example.com",
                    agent_id="agent-1",
                )
                task = await create_task(
                    session,
                    created_by="user@example.com",
                    agent_id="agent-1",
                    title="Task question",
                    status="paused",
                )
                await session.commit()
                return conversation.conversation_id, session_row.session_id, task.task_id

        conversation_id, session_id, task_id = asyncio.run(_seed())
        notification = asyncio.run(
            app.state.notification_service.create(
                notification_type="step_question",
                user_email="user@example.com",
                conversation_id=conversation_id,
                task_id=task_id,
                session_id=session_id,
                notification_id="notif_task_match",
                payload={"question": "Need input"},
            )
        )

        class _Manager:
            def __init__(self) -> None:
                self.errors: list[dict[str, object]] = []

            async def send_error(self, _: object, **kwargs: object) -> None:
                self.errors.append(kwargs)

        manager = _Manager()
        connection = AuthenticatedWebSocket(
            connection_id="conn-1",
            websocket=object(),
            user_email="user@example.com",
            role="user",
        )

        asyncio.run(
            _handle_step_response(
                app,
                manager,
                connection,
                {
                    "type": "step_response",
                    "notification_id": notification.notification_id,
                    "task_id": "task-other",
                    "response": "A",
                },
            )
        )
        assert manager.errors[-1]["code"] == "conflict"


def test_session_events_are_proxied(monkeypatch: object, tmp_path: Path) -> None:
    with _create_test_client(monkeypatch, tmp_path) as client:
        app = client.app

        async def _seed() -> str:
            async with app.state.session_factory() as session:
                await create_user(
                    session,
                    email="user@example.com",
                    name="User",
                    password_hash=app.state.password_hasher.hash("password123"),
                    role="user",
                )
                await create_agent(
                    session,
                    agent_id="agent-1",
                    owner_email="user@example.com",
                    name="Agent 1",
                    status="active",
                )
                conversation = await create_conversation(
                    session,
                    user_email="user@example.com",
                    agent_id="agent-1",
                    context_type="web",
                    title="Conversation",
                )
                session_row = await create_session(
                    session,
                    conversation_id=conversation.conversation_id,
                    user_email="user@example.com",
                    agent_id="agent-1",
                )
                await set_session_intaris_session_id(
                    session, session_row.session_id, session_row.session_id
                )
                await update_conversation_active_session(
                    session, conversation.conversation_id, session_row.session_id
                )
                await session.commit()
                return session_row.session_id

        session_id = asyncio.run(_seed())

        async def _fake_read_events(
            session_id: str, after_seq: int = 0, limit: int = 0, **_: object
        ) -> EventReadResult:
            assert after_seq == 0
            assert limit == 50
            return EventReadResult(
                events=[
                    {
                        "seq": 1,
                        "type": "assistant_message",
                        "data": {"content": "hello"},
                        "ts": "2026-03-28T00:00:00Z",
                    }
                ],
                last_seq=1,
                has_more=False,
            )

        app.state.providers.guardrails.read_events = _fake_read_events

        response = client.get(
            f"/api/v1/sessions/{session_id}/events",
            headers=_auth_headers(app, email="user@example.com"),
        )
        assert response.status_code == 200
        body = response.json()
        assert body["session_id"] == session_id
        assert body["items"][0]["type"] == "assistant_message"


def test_conversation_list_filters_by_agent(monkeypatch: object, tmp_path: Path) -> None:
    with _create_test_client(monkeypatch, tmp_path) as client:
        app = client.app

        async def _seed() -> tuple[str, str]:
            async with app.state.session_factory() as session:
                await create_user(
                    session,
                    email="user@example.com",
                    name="User",
                    password_hash=app.state.password_hasher.hash("password123"),
                    role="user",
                )
                await create_agent(
                    session,
                    agent_id="agent-1",
                    owner_email="user@example.com",
                    name="Agent 1",
                    status="active",
                )
                await create_agent(
                    session,
                    agent_id="agent-2",
                    owner_email="user@example.com",
                    name="Agent 2",
                    status="active",
                )
                first = await create_conversation(
                    session,
                    user_email="user@example.com",
                    agent_id="agent-1",
                    context_type="web",
                    title="Agent one",
                )
                second = await create_conversation(
                    session,
                    user_email="user@example.com",
                    agent_id="agent-2",
                    context_type="web",
                    title="Agent two",
                )
                await session.commit()
                return first.conversation_id, second.conversation_id

        first_id, second_id = asyncio.run(_seed())

        response = client.get(
            "/api/v1/conversations?agent_id=agent-2",
            headers=_auth_headers(app, email="user@example.com"),
        )

        assert response.status_code == 200
        body = response.json()
        assert [item["conversation_id"] for item in body["items"]] == [second_id]
        assert body["items"][0]["agent_id"] == "agent-2"
        assert first_id != second_id


def test_conversation_list_filters_by_multiple_agents_and_channels(
    monkeypatch: object, tmp_path: Path
) -> None:
    with _create_test_client(monkeypatch, tmp_path) as client:
        app = client.app

        async def _seed() -> dict[str, str]:
            async with app.state.session_factory() as session:
                await create_user(
                    session,
                    email="user@example.com",
                    name="User",
                    password_hash=app.state.password_hasher.hash("password123"),
                    role="user",
                )
                for agent_id in ("agent-1", "agent-2", "agent-3"):
                    await create_agent(
                        session,
                        agent_id=agent_id,
                        owner_email="user@example.com",
                        name=agent_id,
                        status="active",
                    )
                rows = {
                    "web_agent_1": await create_conversation(
                        session,
                        user_email="user@example.com",
                        agent_id="agent-1",
                        context_type="web",
                        title="Web agent 1",
                    ),
                    "agent_work_agent_2": await create_conversation(
                        session,
                        user_email="user@example.com",
                        agent_id="agent-2",
                        context_type="agent_work",
                        title="Agent work agent 2",
                    ),
                    "slack_agent_3": await create_conversation(
                        session,
                        user_email="user@example.com",
                        agent_id="agent-3",
                        context_type="slack",
                        title="Slack agent 3",
                    ),
                }
                await session.commit()
                return {key: row.conversation_id for key, row in rows.items()}

        ids = asyncio.run(_seed())

        response = client.get(
            "/api/v1/conversations?agent_ids=agent-1&agent_ids=agent-2"
            "&context_types=web&context_types=agent_work",
            headers=_auth_headers(app, email="user@example.com"),
        )

        assert response.status_code == 200
        body = response.json()
        assert {item["conversation_id"] for item in body["items"]} == {
            ids["web_agent_1"],
            ids["agent_work_agent_2"],
        }
        assert ids["slack_agent_3"] not in {item["conversation_id"] for item in body["items"]}


def test_conversation_search_fans_out_for_multiple_agent_filters(
    monkeypatch: object, tmp_path: Path
) -> None:
    with _create_test_client(monkeypatch, tmp_path) as client:
        app = client.app

        async def _seed() -> dict[str, str]:
            async with app.state.session_factory() as session:
                await create_user(
                    session,
                    email="user@example.com",
                    name="User",
                    password_hash=app.state.password_hasher.hash("password123"),
                    role="user",
                )
                session_ids: dict[str, str] = {}
                for agent_id in ("agent-1", "agent-2"):
                    await create_agent(
                        session,
                        agent_id=agent_id,
                        owner_email="user@example.com",
                        name=agent_id,
                        status="active",
                    )
                    conversation = await create_conversation(
                        session,
                        user_email="user@example.com",
                        agent_id=agent_id,
                        context_type="web",
                        title=agent_id,
                    )
                    session_row = await create_session(
                        session,
                        conversation_id=conversation.conversation_id,
                        user_email="user@example.com",
                        agent_id=agent_id,
                    )
                    intaris_session_id = f"intaris-{agent_id}"
                    await set_session_intaris_session_id(
                        session,
                        session_row.session_id,
                        intaris_session_id,
                    )
                    session_ids[agent_id] = intaris_session_id
                await session.commit()
                return session_ids

        session_ids = asyncio.run(_seed())
        requested_agent_ids: list[str | None] = []
        original_guardrails = app.state.providers.guardrails

        async def _search_sessions(payload: object, *, user_email: str) -> SearchSessionsResponse:
            assert user_email == "user@example.com"
            filters = payload.filters  # type: ignore[attr-defined]
            agent_id = filters.agent_id
            requested_agent_ids.append(agent_id)
            assert agent_id is not None
            intaris_session_id = session_ids[agent_id]
            return SearchSessionsResponse(
                sessions=[
                    SearchSessionMatch(
                        session_id=intaris_session_id,
                        match_count=1,
                        top_match=SearchMatch(
                            session_id=intaris_session_id,
                            kind="summary",
                            snippet=f"match for {agent_id}",
                            score=0.9,
                        ),
                    )
                ],
                total_estimated=1,
            )

        app.state.providers.guardrails = SimpleNamespace(
            client=original_guardrails.client,
            search_sessions=_search_sessions,
        )

        response = client.post(
            "/api/v1/search/conversations",
            json={
                "q": "match",
                "filters": {
                    "agent_id": "agent-1",
                    "agent_ids": ["agent-2"],
                    "context_types": ["web"],
                },
                "kinds": ["summary"],
                "limit": 10,
            },
            headers=_auth_headers(app, email="user@example.com"),
        )

        assert response.status_code == 200
        body = response.json()
        assert requested_agent_ids == ["agent-1", "agent-2"]
        assert {item["agent_id"] for item in body["matches"]} == {"agent-1", "agent-2"}


def test_conversation_search_rejects_too_many_agent_filters(
    monkeypatch: object, tmp_path: Path
) -> None:
    with _create_test_client(monkeypatch, tmp_path) as client:
        app = client.app

        async def _seed() -> None:
            async with app.state.session_factory() as session:
                await create_user(
                    session,
                    email="user@example.com",
                    name="User",
                    password_hash=app.state.password_hasher.hash("password123"),
                    role="user",
                )
                await session.commit()

        asyncio.run(_seed())

        response = client.post(
            "/api/v1/search/conversations",
            json={
                "q": "match",
                "filters": {"agent_ids": [f"agent-{index}" for index in range(26)]},
                "kinds": ["summary"],
                "limit": 10,
            },
            headers=_auth_headers(app, email="user@example.com"),
        )

        assert response.status_code == 400
        assert response.json()["error"]["code"] == "too_many_agent_filters"


def test_conversation_list_paginates_before_attention_hydration(
    monkeypatch: object, tmp_path: Path
) -> None:
    with _create_test_client(monkeypatch, tmp_path) as client:
        app = client.app

        async def _seed() -> list[str]:
            async with app.state.session_factory() as session:
                await create_user(
                    session,
                    email="user@example.com",
                    name="User",
                    password_hash=app.state.password_hasher.hash("password123"),
                    role="user",
                )
                await create_agent(
                    session,
                    agent_id="agent-1",
                    owner_email="user@example.com",
                    name="Agent 1",
                    status="active",
                )
                base = datetime(2026, 5, 7, 12, 0, tzinfo=UTC)
                conversations = []
                for index in range(3):
                    conversation = await create_conversation(
                        session,
                        user_email="user@example.com",
                        agent_id="agent-1",
                        context_type="web",
                        title=f"Conversation {index}",
                    )
                    conversation.last_message_at = base + timedelta(minutes=index)
                    conversations.append(conversation)
                await session.commit()
                return [conversation.conversation_id for conversation in conversations]

        conversation_ids = asyncio.run(_seed())
        hydration_batch_sizes: list[int] = []

        async def _fake_attention_context(
            _session: object,
            conversations: list[object],
            _user_email: str,
        ) -> tuple[dict[str, object], dict[str, list[str]]]:
            hydration_batch_sizes.append(len(conversations))
            return {}, {}

        from cognis.api.routes import conversations as conversation_routes

        monkeypatch.setattr(
            conversation_routes,
            "_conversation_attention_context",
            _fake_attention_context,
        )

        first_response = client.get(
            "/api/v1/conversations?limit=2",
            headers=_auth_headers(app, email="user@example.com"),
        )
        assert first_response.status_code == 200
        first_body = first_response.json()
        assert [item["conversation_id"] for item in first_body["items"]] == [
            conversation_ids[2],
            conversation_ids[1],
        ]
        assert first_body["has_more"] is True
        assert first_body["cursor"]

        second_response = client.get(
            f"/api/v1/conversations?limit=2&cursor={first_body['cursor']}",
            headers=_auth_headers(app, email="user@example.com"),
        )
        assert second_response.status_code == 200
        second_body = second_response.json()
        assert [item["conversation_id"] for item in second_body["items"]] == [
            conversation_ids[0],
        ]
        assert second_body["has_more"] is False
        assert hydration_batch_sizes == [2, 1]


def test_conversation_list_ignores_metadata_updated_at_for_activity_ordering(
    monkeypatch: object, tmp_path: Path
) -> None:
    with _create_test_client(monkeypatch, tmp_path) as client:
        app = client.app

        async def _seed() -> tuple[str, str]:
            async with app.state.session_factory() as session:
                await create_user(
                    session,
                    email="user@example.com",
                    name="User",
                    password_hash=app.state.password_hasher.hash("password123"),
                    role="user",
                )
                await create_agent(
                    session,
                    agent_id="agent-1",
                    owner_email="user@example.com",
                    name="Agent 1",
                    status="active",
                )
                base = datetime(2026, 5, 7, 12, 0, tzinfo=UTC)
                updated_activity = await create_conversation(
                    session,
                    user_email="user@example.com",
                    agent_id="agent-1",
                    context_type="web",
                    title="Updated activity",
                )
                updated_activity.last_message_at = base + timedelta(minutes=1)
                updated_activity.updated_at = base + timedelta(minutes=10)

                newer_message = await create_conversation(
                    session,
                    user_email="user@example.com",
                    agent_id="agent-1",
                    context_type="web",
                    title="Newer message",
                )
                newer_message.last_message_at = base + timedelta(minutes=5)
                newer_message.updated_at = base + timedelta(minutes=5)
                await session.commit()
                return updated_activity.conversation_id, newer_message.conversation_id

        metadata_updated_id, message_id = asyncio.run(_seed())

        response = client.get(
            "/api/v1/conversations",
            headers=_auth_headers(app, email="user@example.com"),
        )

        assert response.status_code == 200
        assert [item["conversation_id"] for item in response.json()["items"][:2]] == [
            message_id,
            metadata_updated_id,
        ]


def test_conversation_context_types_projection(monkeypatch: object, tmp_path: Path) -> None:
    with _create_test_client(monkeypatch, tmp_path) as client:
        app = client.app

        async def _seed() -> None:
            async with app.state.session_factory() as session:
                await create_user(
                    session,
                    email="user@example.com",
                    name="User",
                    password_hash=app.state.password_hasher.hash("password123"),
                    role="user",
                )
                await create_agent(
                    session,
                    agent_id="agent-1",
                    owner_email="user@example.com",
                    name="Agent 1",
                    status="active",
                )
                await create_conversation(
                    session,
                    user_email="user@example.com",
                    agent_id="agent-1",
                    context_type="web",
                    title="Web",
                )
                await create_conversation(
                    session,
                    user_email="user@example.com",
                    agent_id="agent-1",
                    context_type="signal",
                    title="Signal",
                )
                archived = await create_conversation(
                    session,
                    user_email="user@example.com",
                    agent_id="agent-1",
                    context_type="slack",
                    title="Archived Slack",
                )
                archived.status = "archived"
                await session.commit()

        asyncio.run(_seed())

        active_response = client.get(
            "/api/v1/conversations/context-types",
            headers=_auth_headers(app, email="user@example.com"),
        )
        assert active_response.status_code == 200
        assert active_response.json() == ["signal", "web"]

        archived_response = client.get(
            "/api/v1/conversations/context-types?status=archived",
            headers=_auth_headers(app, email="user@example.com"),
        )
        assert archived_response.status_code == 200
        assert archived_response.json() == ["slack"]


def test_conversation_open_prefers_valid_selected_agent_candidate(
    monkeypatch: object, tmp_path: Path
) -> None:
    with _create_test_client(monkeypatch, tmp_path) as client:
        app = client.app

        async def _seed() -> tuple[str, str]:
            async with app.state.session_factory() as session:
                await create_user(
                    session,
                    email="user@example.com",
                    name="User",
                    password_hash=app.state.password_hasher.hash("password123"),
                    role="user",
                )
                await create_agent(
                    session,
                    agent_id="agent-1",
                    owner_email="user@example.com",
                    name="Agent 1",
                    status="active",
                )
                await create_agent(
                    session,
                    agent_id="agent-2",
                    owner_email="user@example.com",
                    name="Agent 2",
                    status="active",
                )
                selected = await create_conversation(
                    session,
                    user_email="user@example.com",
                    agent_id="agent-1",
                    context_type="web",
                    title="Selected Agent",
                )
                other_agent = await create_conversation(
                    session,
                    user_email="user@example.com",
                    agent_id="agent-2",
                    context_type="web",
                    title="Other Agent",
                )
                await session.commit()
                return selected.conversation_id, other_agent.conversation_id

        selected_id, other_agent_id = asyncio.run(_seed())

        response = client.post(
            "/api/v1/conversations/open",
            headers=_auth_headers(app, email="user@example.com"),
            json={
                "agent_id": "agent-1",
                "context_type": "web",
                "candidate_conversation_ids": [other_agent_id, selected_id],
            },
        )

        assert response.status_code == 200
        assert response.json()["conversation_id"] == selected_id


def test_conversation_open_allows_viewer_to_open_existing_candidate(
    monkeypatch: object, tmp_path: Path
) -> None:
    with _create_test_client(monkeypatch, tmp_path) as client:
        app = client.app

        async def _seed() -> str:
            async with app.state.session_factory() as session:
                await create_user(
                    session,
                    email="viewer@example.com",
                    name="Viewer",
                    password_hash=app.state.password_hasher.hash("password123"),
                    role="viewer",
                )
                await create_agent(
                    session,
                    agent_id="agent-1",
                    owner_email="viewer@example.com",
                    name="Agent 1",
                    status="active",
                )
                conversation = await create_conversation(
                    session,
                    user_email="viewer@example.com",
                    agent_id="agent-1",
                    context_type="web",
                    title="Viewer Conversation",
                )
                await session.commit()
                return conversation.conversation_id

        conversation_id = asyncio.run(_seed())

        response = client.post(
            "/api/v1/conversations/open",
            headers=_auth_headers(app, email="viewer@example.com", role="viewer"),
            json={
                "agent_id": "agent-1",
                "context_type": "web",
                "candidate_conversation_ids": [conversation_id],
            },
        )

        assert response.status_code == 200
        assert response.json()["conversation_id"] == conversation_id

        async def _state() -> dict[str, object] | None:
            async with app.state.session_factory() as session:
                return await get_user_ui_state_value(
                    session,
                    "viewer@example.com",
                    "chat.last_opened:viewer-agent\x1f\x1fweb",
                )

        assert asyncio.run(_state()) is None


def test_conversation_open_honors_requested_agent_profile(
    monkeypatch: object, tmp_path: Path
) -> None:
    with _create_test_client(monkeypatch, tmp_path) as client:
        app = client.app

        async def _seed() -> tuple[str, str]:
            async with app.state.session_factory() as session:
                await create_user(
                    session,
                    email="user@example.com",
                    name="User",
                    password_hash=app.state.password_hasher.hash("password123"),
                    role="user",
                )
                await create_agent(
                    session,
                    agent_id="agent-1",
                    owner_email="user@example.com",
                    name="Agent 1",
                    status="active",
                    agent_profiles={
                        "fast": {"profile_id": "fast", "description": "Fast"},
                        "quality": {"profile_id": "quality", "description": "Quality"},
                    },
                    default_agent_profile_id="fast",
                )
                fast = await create_conversation(
                    session,
                    user_email="user@example.com",
                    agent_id="agent-1",
                    context_type="web",
                    title="Fast",
                )
                fast.agent_profile_id = "fast"
                quality = await create_conversation(
                    session,
                    user_email="user@example.com",
                    agent_id="agent-1",
                    context_type="web",
                    title="Quality",
                )
                quality.agent_profile_id = "quality"
                await session.commit()
                return fast.conversation_id, quality.conversation_id

        fast_id, quality_id = asyncio.run(_seed())

        response = client.post(
            "/api/v1/conversations/open",
            headers=_auth_headers(app, email="user@example.com"),
            json={
                "agent_id": "agent-1",
                "agent_profile_id": "quality",
                "context_type": "web",
                "candidate_conversation_ids": [fast_id, quality_id],
            },
        )

        assert response.status_code == 200
        assert response.json()["conversation_id"] == quality_id


def test_conversation_open_fallback_finds_latest_matching_profile(
    monkeypatch: object, tmp_path: Path
) -> None:
    with _create_test_client(monkeypatch, tmp_path) as client:
        app = client.app

        async def _seed() -> tuple[str, str]:
            async with app.state.session_factory() as session:
                await create_user(
                    session,
                    email="user@example.com",
                    name="User",
                    password_hash=app.state.password_hasher.hash("password123"),
                    role="user",
                )
                await create_agent(
                    session,
                    agent_id="agent-1",
                    owner_email="user@example.com",
                    name="Agent 1",
                    status="active",
                    agent_profiles={
                        "fast": {"profile_id": "fast", "description": "Fast"},
                        "quality": {"profile_id": "quality", "description": "Quality"},
                    },
                    default_agent_profile_id="fast",
                )
                base = datetime(2026, 5, 7, 12, 0, tzinfo=UTC)
                quality = await create_conversation(
                    session,
                    user_email="user@example.com",
                    agent_id="agent-1",
                    context_type="web",
                    title="Quality",
                )
                quality.agent_profile_id = "quality"
                quality.last_message_at = base
                fast = await create_conversation(
                    session,
                    user_email="user@example.com",
                    agent_id="agent-1",
                    context_type="web",
                    title="Fast",
                )
                fast.agent_profile_id = "fast"
                fast.last_message_at = base + timedelta(minutes=1)
                await session.commit()
                return quality.conversation_id, fast.conversation_id

        quality_id, fast_id = asyncio.run(_seed())

        response = client.post(
            "/api/v1/conversations/open",
            headers=_auth_headers(app, email="user@example.com"),
            json={
                "agent_id": "agent-1",
                "agent_profile_id": "quality",
                "context_type": "web",
                "candidate_conversation_ids": [],
            },
        )

        assert response.status_code == 200
        assert response.json()["conversation_id"] == quality_id
        assert response.json()["conversation_id"] != fast_id


def test_conversation_open_uses_server_persisted_last_opened(
    monkeypatch: object, tmp_path: Path
) -> None:
    with _create_test_client(monkeypatch, tmp_path) as client:
        app = client.app

        async def _seed() -> tuple[str, str]:
            async with app.state.session_factory() as session:
                await create_user(
                    session,
                    email="user@example.com",
                    name="User",
                    password_hash=app.state.password_hasher.hash("password123"),
                    role="user",
                )
                await create_agent(
                    session,
                    agent_id="agent-1",
                    owner_email="user@example.com",
                    name="Agent 1",
                    status="active",
                )
                base = datetime(2026, 5, 7, 12, 0, tzinfo=UTC)
                older = await create_conversation(
                    session,
                    user_email="user@example.com",
                    agent_id="agent-1",
                    context_type="web",
                    title="Older",
                )
                older.last_message_at = base
                newer = await create_conversation(
                    session,
                    user_email="user@example.com",
                    agent_id="agent-1",
                    context_type="web",
                    title="Newer",
                )
                newer.last_message_at = base + timedelta(minutes=1)
                await session.commit()
                return older.conversation_id, newer.conversation_id

        older_id, newer_id = asyncio.run(_seed())
        headers = _auth_headers(app, email="user@example.com")

        first = client.post(
            "/api/v1/conversations/open",
            headers=headers,
            json={
                "agent_id": "agent-1",
                "context_type": "web",
                "candidate_conversation_ids": [older_id],
            },
        )
        assert first.status_code == 200
        assert first.json()["conversation_id"] == older_id

        second = client.post(
            "/api/v1/conversations/open",
            headers=headers,
            json={
                "agent_id": "agent-1",
                "context_type": "web",
                "candidate_conversation_ids": [],
            },
        )
        assert second.status_code == 200
        assert second.json()["conversation_id"] == older_id
        assert second.json()["conversation_id"] != newer_id


def test_conversation_open_prefers_fresher_client_candidate_over_server_state(
    monkeypatch: object, tmp_path: Path
) -> None:
    with _create_test_client(monkeypatch, tmp_path) as client:
        app = client.app

        async def _seed() -> tuple[str, str]:
            async with app.state.session_factory() as session:
                await create_user(
                    session,
                    email="user@example.com",
                    name="User",
                    password_hash=app.state.password_hasher.hash("password123"),
                    role="user",
                )
                await create_agent(
                    session,
                    agent_id="agent-1",
                    owner_email="user@example.com",
                    name="Agent 1",
                    status="active",
                )
                older = await create_conversation(
                    session,
                    user_email="user@example.com",
                    agent_id="agent-1",
                    context_type="web",
                    title="Server persisted",
                )
                newer = await create_conversation(
                    session,
                    user_email="user@example.com",
                    agent_id="agent-1",
                    context_type="web",
                    title="Client fresher",
                )
                await session.commit()
                return older.conversation_id, newer.conversation_id

        older_id, newer_id = asyncio.run(_seed())
        headers = _auth_headers(app, email="user@example.com")

        first = client.post(
            "/api/v1/conversations/open",
            headers=headers,
            json={
                "agent_id": "agent-1",
                "context_type": "web",
                "candidate_conversation_ids": [older_id],
            },
        )
        assert first.status_code == 200
        assert first.json()["conversation_id"] == older_id

        second = client.post(
            "/api/v1/conversations/open",
            headers=headers,
            json={
                "agent_id": "agent-1",
                "context_type": "web",
                "candidate_conversations": [
                    {
                        "conversation_id": newer_id,
                        "opened_at": "2999-01-01T00:00:00",
                    }
                ],
                "candidate_conversation_ids": [newer_id],
            },
        )

        assert second.status_code == 200
        assert second.json()["conversation_id"] == newer_id
        assert second.json()["conversation_id"] != older_id

        invalid_timestamp = client.post(
            "/api/v1/conversations/open",
            headers=headers,
            json={
                "agent_id": "agent-1",
                "context_type": "web",
                "candidate_conversations": [
                    {
                        "conversation_id": newer_id,
                        "opened_at": "not-a-date",
                    }
                ],
                "candidate_conversation_ids": [newer_id],
            },
        )

        assert invalid_timestamp.status_code == 200
        assert invalid_timestamp.json()["conversation_id"] == newer_id


def test_conversation_opened_endpoint_persists_direct_open(
    monkeypatch: object, tmp_path: Path
) -> None:
    with _create_test_client(monkeypatch, tmp_path) as client:
        app = client.app

        async def _seed() -> tuple[str, str]:
            async with app.state.session_factory() as session:
                await create_user(
                    session,
                    email="user@example.com",
                    name="User",
                    password_hash=app.state.password_hasher.hash("password123"),
                    role="user",
                )
                await create_agent(
                    session,
                    agent_id="agent-1",
                    owner_email="user@example.com",
                    name="Agent 1",
                    status="active",
                )
                base = datetime(2026, 5, 7, 12, 0, tzinfo=UTC)
                opened = await create_conversation(
                    session,
                    user_email="user@example.com",
                    agent_id="agent-1",
                    context_type="web",
                    title="Opened",
                )
                opened.last_message_at = base
                latest = await create_conversation(
                    session,
                    user_email="user@example.com",
                    agent_id="agent-1",
                    context_type="web",
                    title="Latest",
                )
                latest.last_message_at = base + timedelta(minutes=1)
                await session.commit()
                return opened.conversation_id, latest.conversation_id

        opened_id, latest_id = asyncio.run(_seed())
        headers = _auth_headers(app, email="user@example.com")

        opened = client.post(f"/api/v1/conversations/{opened_id}/opened", headers=headers)
        assert opened.status_code == 200
        assert opened.json()["conversation_id"] == opened_id

        resolved = client.post(
            "/api/v1/conversations/open",
            headers=headers,
            json={
                "agent_id": "agent-1",
                "context_type": "web",
                "candidate_conversation_ids": [],
            },
        )
        assert resolved.status_code == 200
        assert resolved.json()["conversation_id"] == opened_id
        assert resolved.json()["conversation_id"] != latest_id


def test_conversation_sidebar_projection_returns_shaped_sidebar_payload(
    monkeypatch: object, tmp_path: Path
) -> None:
    with _create_test_client(monkeypatch, tmp_path) as client:
        app = client.app

        async def _seed() -> tuple[str, str]:
            async with app.state.session_factory() as session:
                await create_user(
                    session,
                    email="user@example.com",
                    name="User",
                    password_hash=app.state.password_hasher.hash("password123"),
                    role="user",
                )
                await create_agent(
                    session,
                    agent_id="agent-1",
                    owner_email="user@example.com",
                    name="Agent 1",
                    status="active",
                )
                await create_agent(
                    session,
                    agent_id="agent-2",
                    owner_email="user@example.com",
                    name="Agent 2",
                    status="active",
                )
                web = await create_conversation(
                    session,
                    user_email="user@example.com",
                    agent_id="agent-1",
                    context_type="web",
                    title="Web",
                )
                await create_conversation(
                    session,
                    user_email="user@example.com",
                    agent_id="agent-1",
                    context_type="signal",
                    title="Signal",
                )
                direct = await create_conversation(
                    session,
                    user_email="user@example.com",
                    agent_id="agent-1",
                    context_type="web",
                    context_ref=agent_direct_context_ref("user@example.com", "agent-1"),
                    context_data={"kind": AGENT_DIRECT_KIND},
                    title="Agent direct",
                    title_source="agent_direct",
                )
                await session.commit()
                return web.conversation_id, direct.conversation_id

        web_id, direct_id = asyncio.run(_seed())

        response = client.get(
            "/api/v1/conversations/sidebar?context_type=web&agent_id=agent-1&limit=10",
            headers=_auth_headers(app, email="user@example.com"),
        )
        assert response.status_code == 200
        body = response.json()
        assert {agent["agent_id"] for agent in body["agents"]} >= {"agent-1", "agent-2"}
        assert body["context_types"] == ["signal", "web"]
        assert [item["conversation_id"] for item in body["conversations"]["items"]] == [web_id]
        assert body["conversations"]["has_more"] is False
        assert [item["conversation"]["conversation_id"] for item in body["agent_direct_chats"]] == [
            direct_id
        ]

        signal_response = client.get(
            "/api/v1/conversations/sidebar?context_type=signal&agent_id=agent-1&limit=10",
            headers=_auth_headers(app, email="user@example.com"),
        )
        assert signal_response.status_code == 200
        assert signal_response.json()["agent_direct_chats"] == []


def test_conversation_list_includes_attention_status(monkeypatch: object, tmp_path: Path) -> None:
    with _create_test_client(monkeypatch, tmp_path) as client:
        app = client.app

        async def _seed() -> str:
            async with app.state.session_factory() as session:
                await create_user(
                    session,
                    email="user@example.com",
                    name="User",
                    password_hash=app.state.password_hasher.hash("password123"),
                    role="user",
                )
                await create_agent(
                    session,
                    agent_id="agent-1",
                    owner_email="user@example.com",
                    name="Agent 1",
                    status="active",
                )
                conversation = await create_conversation(
                    session,
                    user_email="user@example.com",
                    agent_id="agent-1",
                    context_type="web",
                    title="Blocked conversation",
                )
                session_row = await create_session(
                    session,
                    conversation.conversation_id,
                    "user@example.com",
                    "agent-1",
                    status="suspended",
                )
                session_row.completion_reason = "safety_escalation"
                conversation.active_session_id = session_row.session_id
                session.add(
                    NotificationRow(
                        notification_id="notif_attention",
                        notification_type="gate",
                        user_email="user@example.com",
                        conversation_id=conversation.conversation_id,
                        session_id=session_row.session_id,
                        status="pending",
                        payload={},
                    )
                )
                await session.commit()
                return conversation.conversation_id

        conversation_id = asyncio.run(_seed())

        response = client.get(
            "/api/v1/conversations",
            headers=_auth_headers(app, email="user@example.com"),
        )

        assert response.status_code == 200
        body = response.json()
        item = next(item for item in body["items"] if item["conversation_id"] == conversation_id)
        assert item["active_session_status"] == "suspended"
        assert item["active_session_completion_reason"] == "safety_escalation"
        assert item["active_turn_chat_mode"] is None
        assert item["active_turn_chat_mode_source"] is None
        assert item["pending_notification_types"] == ["gate"]


def test_conversation_list_defaults_to_active_and_supports_starred_and_archived_filters(
    monkeypatch: object, tmp_path: Path
) -> None:
    with _create_test_client(monkeypatch, tmp_path) as client:
        app = client.app

        async def _seed() -> tuple[str, str, str, str]:
            async with app.state.session_factory() as session:
                await create_user(
                    session,
                    email="user@example.com",
                    name="User",
                    password_hash=app.state.password_hasher.hash("password123"),
                    role="user",
                )
                await create_agent(
                    session,
                    agent_id="agent-1",
                    owner_email="user@example.com",
                    name="Agent 1",
                    status="active",
                )
                active = await create_conversation(
                    session,
                    user_email="user@example.com",
                    agent_id="agent-1",
                    context_type="web",
                    title="Active",
                )
                archived = await create_conversation(
                    session,
                    user_email="user@example.com",
                    agent_id="agent-1",
                    context_type="web",
                    title="Archived",
                )
                archived.status = "archived"
                starred = await create_conversation(
                    session,
                    user_email="user@example.com",
                    agent_id="agent-1",
                    context_type="web",
                    title="Starred",
                )
                starred.starred_at = datetime(2026, 5, 7, 12, 0, tzinfo=UTC)
                deleted = await create_conversation(
                    session,
                    user_email="user@example.com",
                    agent_id="agent-1",
                    context_type="web",
                    title="Deleted",
                )
                deleted.status = "deleted"
                await session.commit()
                return (
                    active.conversation_id,
                    archived.conversation_id,
                    starred.conversation_id,
                    deleted.conversation_id,
                )

        active_id, archived_id, starred_id, deleted_id = asyncio.run(_seed())

        active_response = client.get(
            "/api/v1/conversations",
            headers=_auth_headers(app, email="user@example.com"),
        )
        assert active_response.status_code == 200
        assert [item["conversation_id"] for item in active_response.json()["items"]] == [
            starred_id,
            active_id,
        ]

        starred_response = client.get(
            "/api/v1/conversations?status=starred",
            headers=_auth_headers(app, email="user@example.com"),
        )
        assert starred_response.status_code == 200
        starred_items = starred_response.json()["items"]
        assert [item["conversation_id"] for item in starred_items] == [starred_id]
        assert starred_items[0]["starred_at"] is not None

        archived_response = client.get(
            "/api/v1/conversations?status=archived",
            headers=_auth_headers(app, email="user@example.com"),
        )
        assert archived_response.status_code == 200
        assert [item["conversation_id"] for item in archived_response.json()["items"]] == [
            archived_id
        ]
        assert deleted_id not in [
            item["conversation_id"] for item in archived_response.json()["items"]
        ]


def test_conversation_update_sets_and_clears_starred_at(
    monkeypatch: object, tmp_path: Path
) -> None:
    with _create_test_client(monkeypatch, tmp_path) as client:
        app = client.app

        async def _seed() -> str:
            async with app.state.session_factory() as session:
                await create_user(
                    session,
                    email="user@example.com",
                    name="User",
                    password_hash=app.state.password_hasher.hash("password123"),
                    role="user",
                )
                await create_agent(
                    session,
                    agent_id="agent-1",
                    owner_email="user@example.com",
                    name="Agent 1",
                    status="active",
                )
                conversation = await create_conversation(
                    session,
                    user_email="user@example.com",
                    agent_id="agent-1",
                    context_type="web",
                    title="Important",
                )
                await session.commit()
                return conversation.conversation_id

        conversation_id = asyncio.run(_seed())
        headers = _auth_headers(app, email="user@example.com")

        star_response = client.patch(
            f"/api/v1/conversations/{conversation_id}",
            headers=headers,
            json={"starred_at": "2026-05-07T12:00:00Z"},
        )
        assert star_response.status_code == 200
        assert star_response.json()["starred_at"] is not None

        detail_response = client.get(f"/api/v1/conversations/{conversation_id}", headers=headers)
        assert detail_response.status_code == 200
        assert detail_response.json()["starred_at"] is not None

        unstar_response = client.patch(
            f"/api/v1/conversations/{conversation_id}",
            headers=headers,
            json={"starred_at": None},
        )
        assert unstar_response.status_code == 200
        assert unstar_response.json()["starred_at"] is None


def test_conversation_detail_uses_scheduler_active_turn_state(
    monkeypatch: object, tmp_path: Path
) -> None:
    with _create_test_client(monkeypatch, tmp_path) as client:
        app = client.app

        async def _seed() -> tuple[str, str]:
            async with app.state.session_factory() as session:
                await create_user(
                    session,
                    email="user@example.com",
                    name="User",
                    password_hash=app.state.password_hasher.hash("password123"),
                    role="user",
                )
                await create_agent(
                    session,
                    agent_id="agent-1",
                    owner_email="user@example.com",
                    name="Agent 1",
                    status="active",
                )
                conversation = await create_conversation(
                    session,
                    user_email="user@example.com",
                    agent_id="agent-1",
                    context_type="web",
                    title="Active",
                )
                session_row = await create_session(
                    session,
                    conversation.conversation_id,
                    "user@example.com",
                    "agent-1",
                )
                await set_session_status(
                    session,
                    session_row.session_id,
                    "completed",
                    completion_reason="finished",
                )
                await update_conversation_active_session(
                    session,
                    conversation.conversation_id,
                    session_row.session_id,
                )
                await session.commit()
                return conversation.conversation_id, session_row.session_id

        conversation_id, session_id = asyncio.run(_seed())
        app.state.turn_scheduler.running_turn_state = lambda _conversation_id: None

        response = client.get(
            f"/api/v1/conversations/{conversation_id}",
            headers=_auth_headers(app, email="user@example.com"),
        )

        assert response.status_code == 200
        body = response.json()
        assert body["active_session_id"] == session_id
        assert body["active_session_status"] == "completed"
        assert body["has_active_turn"] is False
        assert body["active_turn_chat_mode"] is None
        assert body["active_turn_chat_mode_source"] is None


def test_conversation_list_orders_by_latest_activity_even_without_messages(
    monkeypatch: object, tmp_path: Path
) -> None:
    with _create_test_client(monkeypatch, tmp_path) as client:
        app = client.app

        async def _seed() -> tuple[str, str]:
            async with app.state.session_factory() as session:
                await create_user(
                    session,
                    email="user@example.com",
                    name="User",
                    password_hash=app.state.password_hasher.hash("password123"),
                    role="user",
                )
                await create_agent(
                    session,
                    agent_id="agent-1",
                    owner_email="user@example.com",
                    name="Agent 1",
                    status="active",
                )
                older = await create_conversation(
                    session,
                    user_email="user@example.com",
                    agent_id="agent-1",
                    context_type="web",
                    title="Older active conversation",
                )
                older.starred_at = datetime.now(UTC)
                await touch_conversation(
                    session,
                    older.conversation_id,
                    datetime.now(UTC) - timedelta(days=1),
                )
                newer = await create_conversation(
                    session,
                    user_email="user@example.com",
                    agent_id="agent-1",
                    context_type="web",
                    title="Brand new conversation",
                )
                await session.commit()
                return older.conversation_id, newer.conversation_id

        older_id, newer_id = asyncio.run(_seed())

        response = client.get(
            "/api/v1/conversations",
            headers=_auth_headers(app, email="user@example.com"),
        )

        assert response.status_code == 200
        assert [item["conversation_id"] for item in response.json()["items"]] == [
            newer_id,
            older_id,
        ]


def test_conversation_list_ignores_update_time_when_conversation_has_no_messages(
    monkeypatch: object, tmp_path: Path
) -> None:
    with _create_test_client(monkeypatch, tmp_path) as client:
        app = client.app

        async def _seed() -> tuple[str, str]:
            async with app.state.session_factory() as session:
                await create_user(
                    session,
                    email="user@example.com",
                    name="User",
                    password_hash=app.state.password_hasher.hash("password123"),
                    role="user",
                )
                await create_agent(
                    session,
                    agent_id="agent-1",
                    owner_email="user@example.com",
                    name="Agent 1",
                    status="active",
                )
                finished_later = await create_conversation(
                    session,
                    user_email="user@example.com",
                    agent_id="agent-1",
                    context_type="web",
                    title="Finished later",
                )
                await touch_conversation(
                    session,
                    finished_later.conversation_id,
                    datetime(2026, 5, 7, 12, 0, tzinfo=UTC),
                )
                no_messages = await create_conversation(
                    session,
                    user_email="user@example.com",
                    agent_id="agent-1",
                    context_type="web",
                    title="No messages",
                )
                no_messages.last_message_at = None
                no_messages.created_at = datetime(2026, 5, 7, 11, 0, tzinfo=UTC)
                no_messages.updated_at = datetime(2026, 5, 8, 13, 0, tzinfo=UTC)
                await session.commit()
                return finished_later.conversation_id, no_messages.conversation_id

        finished_later_id, no_messages_id = asyncio.run(_seed())

        response = client.get(
            "/api/v1/conversations",
            headers=_auth_headers(app, email="user@example.com"),
        )

        assert response.status_code == 200
        assert [item["conversation_id"] for item in response.json()["items"]] == [
            finished_later_id,
            no_messages_id,
        ]


def test_deleted_conversation_is_hidden_from_detail(monkeypatch: object, tmp_path: Path) -> None:
    with _create_test_client(monkeypatch, tmp_path) as client:
        app = client.app

        async def _seed() -> str:
            async with app.state.session_factory() as session:
                await create_user(
                    session,
                    email="user@example.com",
                    name="User",
                    password_hash=app.state.password_hasher.hash("password123"),
                    role="user",
                )
                await create_agent(
                    session,
                    agent_id="agent-1",
                    owner_email="user@example.com",
                    name="Agent 1",
                    status="active",
                )
                conversation = await create_conversation(
                    session,
                    user_email="user@example.com",
                    agent_id="agent-1",
                    context_type="web",
                    title="Hidden",
                )
                conversation.status = "deleted"
                await session.commit()
                return conversation.conversation_id

        conversation_id = asyncio.run(_seed())
        headers = _auth_headers(app, email="user@example.com")

        detail_response = client.get(f"/api/v1/conversations/{conversation_id}", headers=headers)

        assert detail_response.status_code == 404


def test_websocket_replay_skips_missing_active_session_error(
    monkeypatch: object, tmp_path: Path
) -> None:
    with _create_test_client(monkeypatch, tmp_path) as client:
        app = client.app

        async def _seed() -> str:
            async with app.state.session_factory() as session:
                await create_user(
                    session,
                    email="user@example.com",
                    name="User",
                    password_hash=app.state.password_hasher.hash("password123"),
                    role="user",
                )
                await create_agent(
                    session,
                    agent_id="agent-1",
                    owner_email="user@example.com",
                    name="Agent 1",
                    status="active",
                )
                conversation = await create_conversation(
                    session,
                    user_email="user@example.com",
                    agent_id="agent-1",
                    context_type="web",
                    title="Replay",
                )
                conversation.active_session_id = "sess-missing"
                await session.commit()
                return conversation.conversation_id

        conversation_id = asyncio.run(_seed())

        class _Socket:
            def __init__(self) -> None:
                self.sent: list[dict[str, object]] = []

            async def send_json(self, payload: dict[str, object]) -> None:
                self.sent.append(payload)

        manager = WebSocketConnectionManager(app)
        socket = _Socket()
        connection = AuthenticatedWebSocket(
            connection_id="conn-1",
            websocket=socket,
            user_email="user@example.com",
            role="user",
        )

        asyncio.run(manager.replay(connection, conversation_id=conversation_id, last_seq=0))

        assert [payload["type"] for payload in socket.sent] == [
            "queued_messages_updated",
            "conversation_state_snapshot",
        ]
        state_payload = socket.sent[-1]
        assert state_payload["conversation_id"] == conversation_id
        state = state_payload["state"]
        assert isinstance(state, dict)
        assert state["conversation_id"] == conversation_id
        assert state["conversation_kind"] == "normal"
        assert state["task"] is None
        assert conversation_id in connection.subscriptions


def test_websocket_replay_includes_user_messages(monkeypatch: object, tmp_path: Path) -> None:
    with _create_test_client(monkeypatch, tmp_path) as client:
        app = client.app

        async def _seed() -> tuple[str, str]:
            async with app.state.session_factory() as session:
                await create_user(
                    session,
                    email="user@example.com",
                    name="User",
                    password_hash=app.state.password_hasher.hash("password123"),
                    role="user",
                )
                await create_agent(
                    session,
                    agent_id="agent-1",
                    owner_email="user@example.com",
                    name="Agent 1",
                    status="active",
                )
                conversation = await create_conversation(
                    session,
                    user_email="user@example.com",
                    agent_id="agent-1",
                    context_type="web",
                    title="Replay",
                )
                session_row = await create_session(
                    session,
                    conversation_id=conversation.conversation_id,
                    user_email="user@example.com",
                    agent_id="agent-1",
                )
                await set_session_intaris_session_id(
                    session, session_row.session_id, session_row.session_id
                )
                await update_conversation_active_session(
                    session, conversation.conversation_id, session_row.session_id
                )
                await session.commit()
                return conversation.conversation_id, session_row.session_id

        conversation_id, session_id = asyncio.run(_seed())

        async def _fake_read_events(
            session_id: str,
            after_seq: int = 0,
            limit: int = 0,
            allow_missing_stream: bool = False,
            **_: object,
        ) -> EventReadResult:
            assert session_id
            assert after_seq == 3
            assert limit > 0
            assert allow_missing_stream is True
            return EventReadResult(
                events=[
                    {
                        "seq": 4,
                        "type": "user_message",
                        "timestamp": "2026-03-28T00:00:00Z",
                        "data": {
                            "session_id": session_id,
                            "event_id": "client:cmsg_1",
                            "message_id": "client:cmsg_1",
                            "content": "hello",
                            "client_message_id": "cmsg_1",
                            "turn_id": "turn_1",
                            "attachments": [],
                        },
                    }
                ],
                last_seq=4,
                has_more=False,
            )

        app.state.providers.guardrails.read_events = _fake_read_events

        class _Socket:
            def __init__(self) -> None:
                self.sent: list[dict[str, object]] = []

            async def send_json(self, payload: dict[str, object]) -> None:
                self.sent.append(payload)

        manager = WebSocketConnectionManager(app)
        socket = _Socket()
        connection = AuthenticatedWebSocket(
            connection_id="conn-1",
            websocket=socket,
            user_email="user@example.com",
            role="user",
        )

        asyncio.run(manager.replay(connection, conversation_id=conversation_id, last_seq=3))

        assert not any(payload.get("type") == "timeline_patch" for payload in socket.sent)
        assert conversation_id in connection.subscriptions


def test_conversation_session_events_skip_malformed_rows(
    monkeypatch: object, tmp_path: Path
) -> None:
    with _create_test_client(monkeypatch, tmp_path) as client:
        app = client.app

        async def _seed() -> tuple[str, str]:
            async with app.state.session_factory() as session:
                await create_user(
                    session,
                    email="user@example.com",
                    name="User",
                    password_hash=app.state.password_hasher.hash("password123"),
                    role="user",
                )
                await create_agent(
                    session,
                    agent_id="agent-1",
                    owner_email="user@example.com",
                    name="Agent 1",
                    status="active",
                )
                conversation = await create_conversation(
                    session,
                    user_email="user@example.com",
                    agent_id="agent-1",
                    context_type="web",
                    title="Conversation",
                )
                session_row = await create_session(
                    session,
                    conversation_id=conversation.conversation_id,
                    user_email="user@example.com",
                    agent_id="agent-1",
                )
                await set_session_intaris_session_id(
                    session, session_row.session_id, session_row.session_id
                )
                await update_conversation_active_session(
                    session, conversation.conversation_id, session_row.session_id
                )
                await session.commit()
                return conversation.conversation_id, session_row.session_id

        conversation_id, session_id = asyncio.run(_seed())

        async def _fake_read_events(
            session_id: str,
            after_seq: int = 0,
            limit: int = 0,
            allow_missing_stream: bool = False,
            **_: object,
        ) -> object:
            assert session_id
            assert allow_missing_stream is True
            return type(
                "EventRead",
                (),
                {
                    "events": [
                        {
                            "seq": 1,
                            "type": "assistant_message",
                            "data": {"content": "hello"},
                            "ts": "2026-03-28T00:00:00Z",
                        },
                        ["broken"],
                    ],
                    "last_seq": 1,
                    "has_more": False,
                    "missing_stream_fallback_used": False,
                },
            )()

        app.state.providers.guardrails.read_events = _fake_read_events

        response = client.get(
            f"/api/v1/conversations/{conversation_id}/sessions/{session_id}/events",
            headers=_auth_headers(app, email="user@example.com"),
        )
        assert response.status_code == 200
        body = response.json()
        assert body["session_id"] == session_id
        assert len(body["items"]) == 1
        assert body["items"][0]["type"] == "assistant_message"


def test_conversation_session_events_return_empty_when_stream_missing(
    monkeypatch: object, tmp_path: Path
) -> None:
    with _create_test_client(monkeypatch, tmp_path) as client:
        app = client.app

        async def _seed() -> tuple[str, str]:
            async with app.state.session_factory() as session:
                await create_user(
                    session,
                    email="user@example.com",
                    name="User",
                    password_hash=app.state.password_hasher.hash("password123"),
                    role="user",
                )
                await create_agent(
                    session,
                    agent_id="agent-1",
                    owner_email="user@example.com",
                    name="Agent 1",
                    status="active",
                )
                conversation = await create_conversation(
                    session,
                    user_email="user@example.com",
                    agent_id="agent-1",
                    context_type="web",
                    title="Conversation",
                )
                session_row = await create_session(
                    session,
                    conversation_id=conversation.conversation_id,
                    user_email="user@example.com",
                    agent_id="agent-1",
                )
                await set_session_intaris_session_id(
                    session, session_row.session_id, session_row.session_id
                )
                await update_conversation_active_session(
                    session, conversation.conversation_id, session_row.session_id
                )
                await session.commit()
                return conversation.conversation_id, session_row.session_id

        conversation_id, session_id = asyncio.run(_seed())

        async def _fake_read_events(
            session_id: str,
            after_seq: int = 0,
            limit: int = 0,
            allow_missing_stream: bool = False,
            **_: object,
        ) -> EventReadResult:
            assert session_id
            assert after_seq == 3
            assert limit == 17
            assert allow_missing_stream is True
            return EventReadResult(
                events=[],
                last_seq=0,
                has_more=False,
                missing_stream_fallback_used=True,
            )

        app.state.providers.guardrails.read_events = _fake_read_events

        response = client.get(
            f"/api/v1/conversations/{conversation_id}/sessions/{session_id}/events?after_seq=3&limit=17",
            headers=_auth_headers(app, email="user@example.com"),
        )
        assert response.status_code == 200
        assert response.json() == {
            "session_id": session_id,
            "items": [],
            "timeline_items": [],
            "last_seq": 0,
            "has_more": False,
            "active_thinking": [],
        }


def test_session_events_route_returns_empty_when_stream_missing(
    monkeypatch: object, tmp_path: Path
) -> None:
    with _create_test_client(monkeypatch, tmp_path) as client:
        app = client.app

        async def _seed() -> str:
            async with app.state.session_factory() as session:
                await create_user(
                    session,
                    email="user@example.com",
                    name="User",
                    password_hash=app.state.password_hasher.hash("password123"),
                    role="user",
                )
                await create_agent(
                    session,
                    agent_id="agent-1",
                    owner_email="user@example.com",
                    name="Agent 1",
                    status="active",
                )
                conversation = await create_conversation(
                    session,
                    user_email="user@example.com",
                    agent_id="agent-1",
                    context_type="web",
                    title="Conversation",
                )
                session_row = await create_session(
                    session,
                    conversation_id=conversation.conversation_id,
                    user_email="user@example.com",
                    agent_id="agent-1",
                )
                await set_session_intaris_session_id(
                    session, session_row.session_id, session_row.session_id
                )
                await update_conversation_active_session(
                    session, conversation.conversation_id, session_row.session_id
                )
                await session.commit()
                return session_row.session_id

        session_id = asyncio.run(_seed())

        async def _fake_read_events(
            session_id: str,
            after_seq: int = 0,
            limit: int = 0,
            allow_missing_stream: bool = False,
            **_: object,
        ) -> EventReadResult:
            assert session_id
            assert after_seq == 9
            assert limit == 11
            assert allow_missing_stream is True
            return EventReadResult(
                events=[],
                last_seq=0,
                has_more=False,
                missing_stream_fallback_used=True,
            )

        app.state.providers.guardrails.read_events = _fake_read_events

        response = client.get(
            f"/api/v1/sessions/{session_id}/events?after_seq=9&limit=11",
            headers=_auth_headers(app, email="user@example.com"),
        )
        assert response.status_code == 200
        assert response.json() == {
            "session_id": session_id,
            "items": [],
            "timeline_items": [],
            "last_seq": 0,
            "has_more": False,
            "active_thinking": [],
        }


def test_signed_artifact_route_serves_skill_assets_without_artifact_record(
    monkeypatch: object, tmp_path: Path
) -> None:
    with _create_test_client(monkeypatch, tmp_path) as client:
        app = client.app
        artifact_store = app.state.artifact_store

        async def _seed() -> str:
            await artifact_store.async_save(
                "skills",
                "ska_script",
                "assets/tool.py",
                b"print('hi')\n",
                "text/x-python",
                owner_email="user@example.com",
            )
            async with app.state.session_factory() as session:
                await create_user(
                    session,
                    email="user@example.com",
                    name="User",
                    password_hash=app.state.password_hasher.hash("password123"),
                    role="user",
                )
                skill = await create_skill(
                    session,
                    skill_id="skill_asset_test",
                    name="Asset Test",
                    instructions="Use an asset.",
                    owner_email="user@example.com",
                )
                version = await create_skill_version(
                    session,
                    skill_id=skill.skill_id,
                    version_number=1,
                    content_hash="hash",
                    instructions=skill.instructions,
                )
                await create_skill_asset(
                    session,
                    asset_id="sa_script",
                    skill_version_id=version.version_id,
                    filename="assets/tool.py",
                    artifact_namespace="skills",
                    artifact_object_id="ska_script",
                    content_hash="content-hash",
                    size_bytes=12,
                    content_type="text/x-python",
                )
                await session.commit()
            return await artifact_store.async_get_public_url(
                "skills", "ska_script", "assets/tool.py"
            )

        signed_url = asyncio.run(_seed())
        response = client.get(signed_url)

        assert response.status_code == 200
        assert response.content == b"print('hi')\n"
        assert response.headers["content-type"].startswith("text/x-python")


def test_signed_artifact_view_route_serves_html_inline_only_with_view_signature(
    monkeypatch: object, tmp_path: Path
) -> None:
    with _create_test_client(monkeypatch, tmp_path) as client:
        app = client.app
        artifact_store = app.state.artifact_store

        async def _seed() -> tuple[str, str]:
            await artifact_store.async_save(
                "reports",
                "html_report",
                "report.html",
                b"<!doctype html><title>Report</title><script>window.ok=true</script>",
                "text/html; charset=utf-8",
                owner_email="user@example.com",
            )
            async with app.state.session_factory() as session:
                await create_user(
                    session,
                    email="user@example.com",
                    name="User",
                    password_hash=app.state.password_hasher.hash("password123"),
                    role="user",
                )
                await create_artifact_record(
                    session,
                    artifact_id="html_report",
                    namespace="reports",
                    object_id="html_report",
                    filename="report.html",
                    owner_email="user@example.com",
                    purpose="tool_output",
                    kind="file",
                    mime_type="text/html; charset=utf-8",
                    size_bytes=64,
                    status="attached",
                )
                await session.commit()
            download_url = await artifact_store.async_get_public_url(
                "reports", "html_report", "report.html"
            )
            view_url = await artifact_store.async_get_public_url(
                "reports", "html_report", "report.html", mode="view"
            )
            return download_url, view_url

        download_url, view_url = asyncio.run(_seed())

        download_response = client.get(download_url)
        view_response = client.get(view_url)
        forged_view_response = client.get(download_url.replace("/content/", "/view/"))

        assert download_response.status_code == 200
        assert download_response.headers["content-disposition"].startswith("attachment;")
        assert view_response.status_code == 200
        assert view_response.headers["content-type"].startswith("text/html")
        assert view_response.headers["content-disposition"].startswith("inline;")
        csp = view_response.headers["content-security-policy"]
        assert csp.startswith("sandbox allow-scripts")
        assert "connect-src 'none'" in csp
        assert "https:" not in csp
        assert forged_view_response.status_code == 403


def test_signed_artifact_view_route_rejects_non_html(monkeypatch: object, tmp_path: Path) -> None:
    with _create_test_client(monkeypatch, tmp_path) as client:
        app = client.app
        artifact_store = app.state.artifact_store

        async def _seed() -> str:
            await artifact_store.async_save(
                "reports",
                "plain_report",
                "report.txt",
                b"plain report",
                "text/plain",
                owner_email="user@example.com",
            )
            async with app.state.session_factory() as session:
                await create_user(
                    session,
                    email="user@example.com",
                    name="User",
                    password_hash=app.state.password_hasher.hash("password123"),
                    role="user",
                )
                await create_artifact_record(
                    session,
                    artifact_id="plain_report",
                    namespace="reports",
                    object_id="plain_report",
                    filename="report.txt",
                    owner_email="user@example.com",
                    purpose="tool_output",
                    kind="file",
                    mime_type="text/plain",
                    size_bytes=12,
                    status="attached",
                )
                await session.commit()
            return await artifact_store.async_get_public_url(
                "reports", "plain_report", "report.txt", mode="view"
            )

        response = client.get(asyncio.run(_seed()))

        assert response.status_code == 415


def test_artifact_signed_url_api_returns_view_url_for_html_artifact(
    monkeypatch: object, tmp_path: Path
) -> None:
    with _create_test_client(monkeypatch, tmp_path) as client:
        app = client.app

        async def _seed() -> None:
            async with app.state.session_factory() as session:
                await create_user(
                    session,
                    email="user@example.com",
                    name="User",
                    password_hash=app.state.password_hasher.hash("password123"),
                    role="user",
                )
                await create_artifact_record(
                    session,
                    artifact_id="html_api_report",
                    namespace="reports",
                    object_id="html_api_report",
                    filename="report.html",
                    owner_email="user@example.com",
                    purpose="tool_output",
                    kind="file",
                    mime_type="text/html",
                    size_bytes=64,
                    status="attached",
                )
                await session.commit()

        asyncio.run(_seed())

        response = client.get(
            "/api/v1/artifacts/html_api_report/signed-url?ttl_seconds=604800&mode=view",
            headers=_auth_headers(app, email="user@example.com"),
        )

        assert response.status_code == 200
        body = response.json()
        assert body["mode"] == "view"
        assert "/api/v1/artifacts/view/reports/html_api_report/report.html" in body["url"]


def test_artifact_signed_url_api_rejects_view_url_for_non_html_artifact(
    monkeypatch: object, tmp_path: Path
) -> None:
    with _create_test_client(monkeypatch, tmp_path) as client:
        app = client.app

        async def _seed() -> None:
            async with app.state.session_factory() as session:
                await create_user(
                    session,
                    email="user@example.com",
                    name="User",
                    password_hash=app.state.password_hasher.hash("password123"),
                    role="user",
                )
                await create_artifact_record(
                    session,
                    artifact_id="plain_api_report",
                    namespace="reports",
                    object_id="plain_api_report",
                    filename="report.txt",
                    owner_email="user@example.com",
                    purpose="tool_output",
                    kind="file",
                    mime_type="text/plain",
                    size_bytes=64,
                    status="attached",
                )
                await session.commit()

        asyncio.run(_seed())

        response = client.get(
            "/api/v1/artifacts/plain_api_report/signed-url?mode=view",
            headers=_auth_headers(app, email="user@example.com"),
        )

        assert response.status_code == 415


def test_signed_virtual_deliverable_route_serves_exact_content(
    monkeypatch: object, tmp_path: Path
) -> None:
    with _create_test_client(monkeypatch, tmp_path) as client:
        app = client.app

        async def _seed() -> str:
            async with app.state.session_factory() as session:
                await create_user(
                    session,
                    email="user@example.com",
                    name="User",
                    password_hash=app.state.password_hasher.hash("password123"),
                    role="user",
                )
                await create_agent(
                    session,
                    agent_id="agent-1",
                    owner_email="user@example.com",
                    name="Agent",
                )
                task = await create_task(
                    session,
                    task_id="task-virtual-url",
                    created_by="user@example.com",
                    agent_id="agent-1",
                    title="Task",
                )
                step_run = await create_step_run(
                    session,
                    step_run_id="sr-virtual-url",
                    task_id=task.task_id,
                    step_name="execute",
                    step_type="direct",
                    agent_id="agent-1",
                    status="completed",
                )
                await create_deliverable(
                    session,
                    deliverable_id="dlv_virtual_url",
                    step_run_id=step_run.step_run_id,
                    title="Virtual URL",
                    content="# Virtual\n\nExact content.",
                    format="markdown",
                )
                await session.commit()

                from cognis.core.content_refs import (
                    build_deliverable_public_url,
                    get_accessible_deliverable_ref,
                )

                async with app.state.session_factory() as session:
                    ref = await get_accessible_deliverable_ref(
                        session, "dlv_virtual_url", "user@example.com"
                    )
                assert ref is not None
                return build_deliverable_public_url(
                    app.state.artifact_store,
                    ref,
                    ttl_seconds=3600,
                )

        signed_url = client.portal.call(_seed)
        response = client.get(signed_url)

        assert response.status_code == 200
        assert response.content == b"# Virtual\n\nExact content."
        assert response.headers["content-type"].startswith("text/markdown")
        assert "Virtual-URL.md" in response.headers["content-disposition"]


def test_websocket_queues_second_message_while_turn_active(
    monkeypatch: object, tmp_path: Path
) -> None:
    with _create_test_client(monkeypatch, tmp_path) as client:
        app = client.app

        async def _seed() -> str:
            async with app.state.session_factory() as session:
                await create_user(
                    session,
                    email="user@example.com",
                    name="User",
                    password_hash=app.state.password_hasher.hash("password123"),
                    role="user",
                )
                agent = await create_agent(
                    session,
                    agent_id="agent-1",
                    owner_email="user@example.com",
                    name="Agent 1",
                    status="active",
                )
                conversation = await create_conversation(
                    session,
                    user_email="user@example.com",
                    agent_id=agent.agent_id,
                    context_type="web",
                    title="Conversation",
                )
                session_row = await create_session(
                    session,
                    conversation_id=conversation.conversation_id,
                    user_email="user@example.com",
                    agent_id=agent.agent_id,
                )
                await set_session_intaris_session_id(
                    session, session_row.session_id, session_row.session_id
                )
                await update_conversation_active_session(
                    session, conversation.conversation_id, session_row.session_id
                )
                await session.commit()
                return conversation.conversation_id

        conversation_id = asyncio.run(_seed())

        captured_direct_turn_kwargs: dict[str, object] = {}

        async def _fake_direct_turn(**kwargs: object) -> None:
            captured_direct_turn_kwargs.update(kwargs)
            await asyncio.sleep(0.2)
            return None

        async def _fake_decide(**_: object) -> DecisionResult:
            return DecisionResult(
                decision="inline",
                reason="test",
                confidence=1.0,
                predicted_tool_intensity="low",
            )

        app.state.workflow_engine.run_direct_turn = _fake_direct_turn
        app.state.decision_engine.decide = _fake_decide

        class _Entry:
            last_event_seq = 1

        async def _fake_refresh(session: object) -> object:
            return _Entry()

        app.state.session_cache.refresh = _fake_refresh

        with client.websocket_connect("/api/ws") as ws:
            ws.send_json(
                {
                    "type": "auth",
                    "token": _auth_headers(app, email="user@example.com")[
                        "Authorization"
                    ].removeprefix("Bearer "),
                }
            )
            assert ws.receive_json()["type"] == "authenticated"
            ws.send_json(
                {"type": "message", "conversation_id": conversation_id, "content": "First"}
            )
            ws.send_json(
                {"type": "message", "conversation_id": conversation_id, "content": "Second"}
            )

            seen_types: set[str] = set()
            for _ in range(5):
                payload = ws.receive_json()
                seen_types.add(payload["type"])
                if payload["type"] in {"queued", "queued_messages_updated"}:
                    break

            assert seen_types & {"queued", "queued_messages_updated"}
            for _ in range(5):
                payload = ws.receive_json()
                if payload["type"] == "message_complete":
                    assert captured_direct_turn_kwargs["bootstrap_wait_for_intention"] is False
                    break
