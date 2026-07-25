"""Runtime ↔ canonical item ID equivalence contract.

The single largest source of streaming-vs-reload divergence is an item whose
runtime overlay id differs from its canonical projection id: the two never
merge on the client, so the streamed item duplicates (or vanishes) after a
refresh. Every item type the runtime overlay can mint MUST produce a
byte-identical id in the canonical projector for the same logical event.
"""

from __future__ import annotations

from cognis.api.chat_v2.event_store import RawSessionEvent
from cognis.api.chat_v2.normalizer import normalize_session_events
from cognis.api.chat_v2.projector import project_timeline
from cognis.api.chat_v2.realtime import (
    assistant_completion_runtime_item,
    assistant_stream_runtime_item,
    compaction_runtime_item,
    delegation_runtime_item,
    thinking_runtime_items,
    tool_call_runtime_item,
    tool_result_runtime_item,
)

TS = "2026-01-01T00:00:00Z"


def _canonical_ids(raw_events: list[RawSessionEvent]) -> dict[str, str]:
    projection = project_timeline(normalize_session_events(raw_events).events)
    return {item.id: item.kind for item in projection.timeline.items}


def test_compaction_ids_match_from_running_state_through_canonical_projection() -> None:
    running = compaction_runtime_item(
        {
            "session_id": "sess_old",
            "trigger": "idle_checkpoint",
            "reason": "long_lived_chat_idle",
        }
    )
    completed = compaction_runtime_item(
        {
            "session_id": "sess_new",
            "previous_session_id": "sess_old",
            "summary_preview": "Compacted history",
            "method": "llm",
            "turns_compacted": 5,
        },
        status="compacted",
    )
    assert running is not None
    assert completed is not None

    canonical = _canonical_ids(
        [
            RawSessionEvent(
                store_id="intaris",
                session_id="sess_new",
                seq=2,
                type="compaction_summary",
                data={
                    "summary": "Compacted history",
                    "session_id": "sess_new",
                    "source_session_id": "sess_old",
                    "method": "llm",
                    "turns_compacted": 5,
                    "timeline_visible": True,
                },
            )
        ]
    )

    assert running.id == completed.id == "compaction:sess_old"
    assert completed.id in canonical


def test_assistant_message_ids_match_across_runtime_and_canonical() -> None:
    turn_id = "turn_1"
    phase = 2

    stream_item = assistant_stream_runtime_item(
        {
            "content": "streaming text",
            "message_id": turn_id,
            "turn_id": turn_id,
            "assistant_phase_index": phase,
            "session_id": "sess_1",
            "updated_at": TS,
        },
        local=0,
    )
    assert stream_item is not None
    completion_item = assistant_completion_runtime_item(
        message_id=turn_id,
        turn_id=turn_id,
        session_id="sess_1",
        phase=phase,
        content="final text",
        timestamp=TS,
        partial=False,
    )

    canonical = _canonical_ids(
        [
            RawSessionEvent(
                store_id="intaris",
                session_id="sess_1",
                seq=10,
                type="assistant_message",
                data={
                    "content": "final text",
                    "message_id": turn_id,
                    "turn_id": turn_id,
                    "assistant_phase_index": phase,
                },
            )
        ]
    )

    assert stream_item.id in canonical
    assert completion_item.id in canonical
    assert stream_item.id == completion_item.id


def test_thinking_block_ids_match_across_runtime_and_canonical() -> None:
    turn_id = "turn_1"
    phase = 1
    block_id = "thk_req_1"

    runtime_items = thinking_runtime_items(
        {
            "session_id": "sess_1",
            "message_id": turn_id,
            "turn_id": turn_id,
            "assistant_phase_index": phase,
            "updated_at": TS,
            "blocks": [
                {
                    "block_id": block_id,
                    "title": "Thinking",
                    "content": "reasoning",
                    "source": "summary",
                    "complete": False,
                }
            ],
        },
        local_start=0,
    )
    assert len(runtime_items) == 1

    canonical = _canonical_ids(
        [
            RawSessionEvent(
                store_id="intaris",
                session_id="sess_1",
                seq=11,
                type="assistant_thinking",
                data={
                    "message_id": turn_id,
                    "turn_id": turn_id,
                    "block_id": block_id,
                    "content": "reasoning",
                    "assistant_phase_index": phase,
                },
            )
        ]
    )

    assert runtime_items[0].id in canonical


def test_thinking_block_ids_match_when_provider_omits_block_id() -> None:
    turn_id = "turn_1"
    phase = 1

    runtime_items = thinking_runtime_items(
        {
            "session_id": "sess_1",
            "message_id": turn_id,
            "turn_id": turn_id,
            "assistant_phase_index": phase,
            "source_seq": 11,
            "updated_at": TS,
            "blocks": [
                {
                    "title": "Thinking",
                    "content": "reasoning without provider block id",
                    "source": "summary",
                    "complete": False,
                }
            ],
        },
        local_start=0,
    )
    assert len(runtime_items) == 1

    canonical = _canonical_ids(
        [
            RawSessionEvent(
                store_id="intaris",
                session_id="sess_1",
                seq=11,
                type="assistant_thinking",
                data={
                    "message_id": turn_id,
                    "turn_id": turn_id,
                    "content": "reasoning without provider block id",
                    "assistant_phase_index": phase,
                },
            )
        ]
    )

    assert runtime_items[0].blocks[0].id == "seq-11"
    assert runtime_items[0].id in canonical


def test_tool_call_ids_match_across_runtime_and_canonical() -> None:
    call_id = "call_equiv"

    runtime_call = tool_call_runtime_item(
        session_id="sess_1",
        call_id=call_id,
        tool_name="bash",
        arguments={"command": "ls"},
        turn_id="turn_1",
        assistant_phase_index=0,
        turn_cycle_index=0,
        timestamp=TS,
    )
    runtime_result = tool_result_runtime_item(
        session_id="sess_1",
        call_id=call_id,
        tool_name="bash",
        result="ok",
        is_error=False,
        duration_ms=10,
        evaluation=None,
        attachments=None,
        file_diffs=None,
        turn_id="turn_1",
        assistant_phase_index=0,
        turn_cycle_index=0,
        timestamp=TS,
    )

    canonical = _canonical_ids(
        [
            RawSessionEvent(
                store_id="intaris",
                session_id="sess_1",
                seq=12,
                type="tool_call",
                data={
                    "call_id": call_id,
                    "name": "bash",
                    "arguments": {"command": "ls"},
                    "turn_id": "turn_1",
                    "assistant_phase_index": 0,
                },
            ),
            RawSessionEvent(
                store_id="intaris",
                session_id="sess_1",
                seq=13,
                type="tool_result",
                data={
                    "call_id": call_id,
                    "name": "bash",
                    "result": "ok",
                    "assistant_phase_index": 0,
                },
            ),
        ]
    )

    assert runtime_call.id in canonical
    assert runtime_result.id == runtime_call.id


def test_delegation_overlay_targets_the_canonical_delegate_tool_item() -> None:
    call_id = "call_deleg"

    overlay = delegation_runtime_item(
        {
            "call_id": call_id,
            "mode": "delegate",
            "status": "completed",
            "child_session_id": "child_1",
            "parent_session_id": "sess_1",
            "assistant_phase_index": 1,
        },
        timestamp=TS,
    )
    assert overlay is not None

    canonical = _canonical_ids(
        [
            RawSessionEvent(
                store_id="intaris",
                session_id="sess_1",
                seq=14,
                type="tool_call",
                data={
                    "call_id": call_id,
                    "name": "delegate",
                    "assistant_phase_index": 1,
                },
            ),
            RawSessionEvent(
                store_id="intaris",
                session_id="sess_1",
                seq=15,
                type="delegation",
                data={
                    "call_id": call_id,
                    "mode": "delegate",
                    "status": "completed",
                    "child_session_id": "child_1",
                    "assistant_phase_index": 1,
                },
            ),
        ]
    )

    # The delegation folds onto the tool item canonically; the runtime overlay
    # must target the same id so the client merges instead of duplicating.
    assert overlay.id in canonical
    assert canonical[overlay.id] == "tool_call"


def test_delegation_overlay_with_phase_uses_active_band_not_late_band() -> None:
    overlay = delegation_runtime_item(
        {
            "call_id": "call_x",
            "mode": "delegate",
            "status": "running",
            "child_session_id": "child_x",
            "parent_session_id": "sess_1",
            "assistant_phase_index": 3,
        },
        timestamp=TS,
    )
    assert overlay is not None
    # Phase-anchored key (9998 band), not the bottom-of-timeline 9999 band
    # that made delegation cards jump to the tail during streaming.
    assert overlay.sort_key.startswith("9998:")
    assert ":000003:" in overlay.sort_key


def test_user_message_ids_match_for_client_message_id() -> None:
    canonical = _canonical_ids(
        [
            RawSessionEvent(
                store_id="intaris",
                session_id="sess_1",
                seq=16,
                type="user_message",
                data={
                    "role": "user",
                    "content": "queued while streaming",
                    "client_message_id": "cmsg_abc",
                    "queue_id": "qmsg_1",
                    "message_id": "cmsg_abc",
                },
            )
        ]
    )
    # The optimistic bubble and the live WS event both use
    # user:{client_message_id}; the canonical projection must match.
    assert "user:cmsg_abc" in canonical


def test_lifecycle_system_notice_ids_match_command_notice_id() -> None:
    notice_id = "command:profile:abc123"

    canonical = _canonical_ids(
        [
            RawSessionEvent(
                store_id="intaris",
                session_id="sess_1",
                seq=17,
                type="lifecycle",
                data={
                    "event": "system_notice",
                    "message": "Agent profile switched to: fast",
                    "notice_id": notice_id,
                    "kind": "command_result",
                    "scope": "session",
                    "command": "/profile",
                },
            )
        ]
    )

    assert f"system:{notice_id}" in canonical
    assert canonical[f"system:{notice_id}"] == "message"
