from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from cognis.api.chat_v2.event_store import RawSessionEvent
from cognis.api.chat_v2.normalizer import normalize_session_events
from cognis.api.chat_v2.projector import project_timeline
from cognis.api.chat_v2.schemas import (
    ArtifactTimelineItem,
    AuthChallengeTimelineItem,
    CompactionTimelineItem,
    CredentialRequestTimelineItem,
    DelegationTimelineItem,
    FileDiffTimelineItem,
    ManagedConversationTimelineItem,
    MessageTimelineItem,
    NoticeTimelineItem,
    QuestionSetTimelineItem,
    TaskTimelineItem,
    ThinkingTimelineItem,
    TimelineItem,
    TodoStateTimelineItem,
    ToolCallTimelineItem,
)

FIXTURES_DIR = Path(__file__).parent / "fixtures"


@pytest.mark.parametrize(
    "fixture_name",
    [
        "messages_tools_delegation.json",
        "cards_filtering_unknown.json",
        "dedupe_lineage.json",
    ],
)
def test_projector_golden_fixtures(fixture_name: str) -> None:
    fixture = _load_fixture(fixture_name)
    raw_events = [RawSessionEvent.model_validate(item) for item in fixture["raw_events"]]

    normalization = normalize_session_events(
        raw_events,
        lineage_ordinal=fixture.get("lineage_ordinal", 0),
    )
    projection = project_timeline(normalization.events)

    expected = fixture["expected"]
    assert normalization.skipped_count == expected["skipped_count"]
    assert normalization.skipped_event_types == expected.get("skipped_event_types", {})
    assert [warning.code for warning in projection.warnings] == expected["warning_codes"]
    assert [_item_summary(item) for item in projection.timeline.items] == expected["items"]
    assert all(item.stable for item in projection.timeline.items)


def test_side_lane_can_be_included_when_requested() -> None:
    raw_events = [
        RawSessionEvent(
            store_id="intaris",
            session_id="sess_side",
            seq=1,
            type="user_message",
            lane="side",
            data={"content": "side lane", "client_message_id": "side_1"},
        )
    ]

    normalization = normalize_session_events(raw_events, visible_lanes=frozenset({None, "side"}))
    projection = project_timeline(normalization.events)

    assert normalization.skipped_count == 0
    assert [item.id for item in projection.timeline.items] == ["user:side_1"]


def test_audit_only_prompt_visibility_is_hidden() -> None:
    raw_events = [
        RawSessionEvent(
            store_id="intaris",
            session_id="sess_hidden",
            seq=1,
            type="user_message",
            prompt_visibility="audit_only",
            data={"content": "must not render", "client_message_id": "hidden_1"},
        )
    ]

    normalization = normalize_session_events(raw_events)
    projection = project_timeline(normalization.events)

    assert normalization.skipped_count == 1
    assert normalization.skipped_event_types == {"user_message": 1}
    assert projection.timeline.items == []


def test_lifecycle_and_system_events_are_hidden_from_chat_timeline() -> None:
    raw_events = [
        RawSessionEvent(
            store_id="intaris",
            session_id="sess_hidden",
            seq=1,
            type="lifecycle",
            data={"event": "session_started"},
        ),
        RawSessionEvent(
            store_id="intaris",
            session_id="sess_hidden",
            seq=2,
            type="system_message",
            data={"content": "system prompt"},
        ),
        RawSessionEvent(
            store_id="intaris",
            session_id="sess_hidden",
            seq=3,
            type="message",
            data={"role": "system", "content": "system context"},
        ),
        RawSessionEvent(
            store_id="intaris",
            session_id="sess_hidden",
            seq=4,
            type="system_message",
            data={
                "notice_id": "notice_1",
                "kind": "managed_takeover",
                "scope": "conversation",
                "follow_up_conversation_id": "conv_follow",
                "follow_up_session_id": "sess_follow",
                "content": "visible notice",
            },
        ),
        RawSessionEvent(
            store_id="intaris",
            session_id="sess_hidden",
            seq=5,
            type="system_message",
            data={"notice_id": "notice_hidden_prompt", "content": "hidden prompt notice"},
            prompt_visibility="audit_only",
        ),
        RawSessionEvent(
            store_id="intaris",
            session_id="sess_hidden",
            seq=6,
            type="system_message",
            data={"notice_id": "notice_hidden_lane", "content": "hidden lane notice"},
            lane="side",
        ),
        RawSessionEvent(
            store_id="intaris",
            session_id="sess_hidden",
            seq=7,
            type="assistant_message",
            data={"content": "visible", "message_id": "assistant_1"},
        ),
    ]

    normalization = normalize_session_events(raw_events)
    projection = project_timeline(normalization.events)

    assert normalization.skipped_count == 5
    assert normalization.skipped_event_types == {
        "lifecycle": 1,
        "message": 1,
        "system_message": 3,
    }
    assert [
        (item.kind, getattr(item, "role", None), getattr(item, "content", None))
        for item in projection.timeline.items
    ] == [
        ("message", "system", "visible notice"),
        ("message", "assistant", "visible"),
    ]
    system_item = projection.timeline.items[0]
    assert isinstance(system_item, MessageTimelineItem)
    assert system_item.notice_id == "notice_1"
    assert system_item.notice_kind == "managed_takeover"
    assert system_item.notice_scope == "conversation"
    assert system_item.follow_up_conversation_id == "conv_follow"
    assert system_item.follow_up_session_id == "sess_follow"


def test_lifecycle_compaction_start_notice_is_hidden() -> None:
    raw_events = [
        RawSessionEvent(
            store_id="intaris",
            session_id="sess_compact",
            seq=1,
            type="lifecycle",
            data={
                "event": "system_notice",
                "kind": "compaction_start",
                "message": (
                    "Automatic compaction is starting before this turn continues because the "
                    "session context is over the compaction threshold."
                ),
            },
        ),
    ]

    normalization = normalize_session_events(raw_events)
    projection = project_timeline(normalization.events)

    assert normalization.skipped_count == 1
    assert normalization.skipped_event_types == {"lifecycle": 1}
    assert projection.timeline.items == []


def test_lifecycle_system_notice_projects_to_system_message() -> None:
    raw_events = [
        RawSessionEvent(
            store_id="intaris",
            session_id="sess_notice",
            seq=1,
            type="lifecycle",
            data={
                "event": "system_notice",
                "message": (
                    "A model error occurred while generating the response. "
                    "Your tool results have been saved. Please try sending your message again."
                ),
                "turn_id": "turn_1",
                "notice_id": "model_error:turn_1",
                "kind": "model_error",
            },
        ),
    ]

    normalization = normalize_session_events(raw_events)
    projection = project_timeline(normalization.events)

    assert normalization.skipped_count == 0
    assert len(projection.timeline.items) == 1
    item = projection.timeline.items[0]
    assert isinstance(item, MessageTimelineItem)
    assert not isinstance(item, NoticeTimelineItem)
    assert item.id == "system:model_error:turn_1"
    assert item.role == "system"
    assert item.content == (
        "A model error occurred while generating the response. "
        "Your tool results have been saved. Please try sending your message again."
    )
    assert item.turn_id == "turn_1"
    assert item.notice_id == "model_error:turn_1"
    assert item.notice_kind == "model_error"


def test_lifecycle_system_notice_dedupes_by_notice_id() -> None:
    raw_events = [
        RawSessionEvent(
            store_id="intaris",
            session_id="sess_notice",
            seq=1,
            type="lifecycle",
            data={
                "event": "system_notice",
                "message": "A model error occurred while generating the response.",
                "turn_id": "turn_1",
                "notice_id": "model_error:turn_1",
            },
        ),
        RawSessionEvent(
            store_id="intaris",
            session_id="sess_notice",
            seq=2,
            type="lifecycle",
            data={
                "event": "system_notice",
                "message": "A model error occurred while generating the response.",
                "turn_id": "turn_1",
                "notice_id": "model_error:turn_1",
            },
        ),
    ]

    normalization = normalize_session_events(raw_events)
    projection = project_timeline(normalization.events)

    assert normalization.skipped_count == 0
    assert len(projection.timeline.items) == 1
    item = projection.timeline.items[0]
    assert isinstance(item, MessageTimelineItem)
    assert item.id == "system:model_error:turn_1"
    assert len(item.source_refs) == 2


def test_lifecycle_system_notice_dedupes_by_kind_and_turn_id() -> None:
    raw_events = [
        RawSessionEvent(
            store_id="intaris",
            session_id="sess_notice",
            seq=1,
            type="lifecycle",
            data={
                "event": "system_notice",
                "message": "Step timed out after 60s",
                "turn_id": "turn_1",
                "kind": "step_timeout",
            },
        ),
        RawSessionEvent(
            store_id="intaris",
            session_id="sess_notice",
            seq=2,
            type="lifecycle",
            data={
                "event": "system_notice",
                "message": "Step timed out after 60s",
                "turn_id": "turn_1",
                "kind": "step_timeout",
            },
        ),
    ]

    normalization = normalize_session_events(raw_events)
    projection = project_timeline(normalization.events)

    assert normalization.skipped_count == 0
    assert len(projection.timeline.items) == 1
    item = projection.timeline.items[0]
    assert isinstance(item, MessageTimelineItem)
    assert item.id == "system:step_timeout:turn_1"
    assert len(item.source_refs) == 2


def test_compaction_summary_projects_to_compaction_card() -> None:
    raw_events = [
        RawSessionEvent(
            store_id="intaris",
            session_id="sess_new",
            seq=1,
            type="compaction_summary",
            data={
                "session_id": "sess_new",
                "source_session_id": "sess_old",
                "summary": "Compacted history summary",
                "method": "rotation",
                "turns_compacted": 7,
                "trigger": "pressure",
                "reason": "context_pressure",
                "previous_usage_percentage": 86.1,
                "effective_usage_percentage": 72.5,
                "hard_pressure_exceeded": True,
                "used_timeout_fallback": False,
            },
        ),
    ]

    normalization = normalize_session_events(raw_events)
    projection = project_timeline(normalization.events)

    assert len(projection.timeline.items) == 1
    item = projection.timeline.items[0]
    assert isinstance(item, CompactionTimelineItem)
    assert item.kind == "compaction"
    assert item.id == "compaction:sess_old:sess_new"
    assert item.status == "compacted"
    assert item.session_id == "sess_new"
    assert item.previous_session_id == "sess_old"
    assert item.summary_preview == "Compacted history summary"
    assert item.method == "rotation"
    assert item.turns_compacted == 7
    assert item.previous_usage_percentage == 86.1
    assert item.effective_usage_percentage == 72.5
    assert item.hard_pressure_exceeded is True


def test_evaluation_events_attach_to_tool_call_cards() -> None:
    raw_events = [
        RawSessionEvent(
            store_id="intaris",
            session_id="sess_eval",
            seq=1,
            type="evaluation",
            data={
                "call_id": "call_1",
                "decision": "approve",
                "reasoning": "Safe to run",
                "risk": "low",
            },
        ),
        RawSessionEvent(
            store_id="intaris",
            session_id="sess_eval",
            seq=2,
            type="tool_call",
            data={
                "call_id": "call_1",
                "tool_name": "bash",
                "arguments": {"command": "true"},
            },
        ),
    ]

    normalization = normalize_session_events(raw_events)
    projection = project_timeline(normalization.events)

    assert normalization.skipped_count == 0
    assert [warning.code for warning in projection.warnings] == []
    assert len(projection.timeline.items) == 1
    item = projection.timeline.items[0]
    assert isinstance(item, ToolCallTimelineItem)
    assert item.call_id == "call_1"
    assert item.tool_name == "bash"
    assert item.evaluation == {
        "decision": "approve",
        "reasoning": "Safe to run",
        "risk": "low",
    }


def test_assistant_messages_project_chat_mode_metadata() -> None:
    raw_events = [
        RawSessionEvent(
            store_id="intaris",
            session_id="sess_mode",
            seq=1,
            type="assistant_message",
            data={
                "message_id": "assistant_plan",
                "content": "Plan response",
                "chat_mode": "plan",
                "chat_mode_source": "directive",
            },
        )
    ]

    projection = project_timeline(normalize_session_events(raw_events).events)

    assert len(projection.timeline.items) == 1
    item = projection.timeline.items[0]
    assert isinstance(item, MessageTimelineItem)
    assert item.chat_mode == "plan"
    assert item.chat_mode_source == "directive"


def test_turn_cycle_index_projects_to_message_thinking_and_tool_items() -> None:
    raw_events = [
        RawSessionEvent(
            store_id="intaris",
            session_id="sess_cycle",
            seq=1,
            type="assistant_message",
            data={
                "message_id": "assistant_1",
                "content": "I will inspect it.",
                "turn_id": "turn_1",
                "assistant_phase_index": 0,
                "turn_cycle_index": 0,
            },
        ),
        RawSessionEvent(
            store_id="intaris",
            session_id="sess_cycle",
            seq=2,
            type="assistant_thinking",
            data={
                "message_id": "assistant_1",
                "block_id": "think_1",
                "content": "Considering next step",
                "turn_id": "turn_1",
                "assistant_phase_index": 1,
                "turn_cycle_index": 1,
            },
        ),
        RawSessionEvent(
            store_id="intaris",
            session_id="sess_cycle",
            seq=3,
            type="tool_call",
            data={
                "call_id": "call_1",
                "name": "read",
                "arguments": {"file_path": "README.md"},
                "turn_id": "turn_1",
                "assistant_phase_index": 1,
                "turn_cycle_index": 1,
            },
        ),
    ]

    projection = project_timeline(normalize_session_events(raw_events).events)

    message = next(item for item in projection.timeline.items if item.kind == "message")
    thinking = next(item for item in projection.timeline.items if item.kind == "thinking")
    tool_call = next(item for item in projection.timeline.items if item.kind == "tool_call")

    assert message.turn_cycle_index == 0
    assert thinking.turn_cycle_index == 1
    assert tool_call.turn_cycle_index == 1
    assert [
        (state.turn_id, state.turn_cycle_index, state.lifecycle_status, state.has_tool_activity)
        for state in projection.timeline.cycle_states
    ] == [
        ("turn_1", 0, "complete", False),
        ("turn_1", 1, "open", True),
    ]


def test_tool_result_preserves_cycle_from_tool_call_event() -> None:
    raw_events = [
        RawSessionEvent(
            store_id="intaris",
            session_id="sess_cycle",
            seq=1,
            type="tool_call",
            data={
                "call_id": "call_1",
                "name": "read",
                "arguments": {"file_path": "README.md"},
                "turn_id": "turn_1",
                "assistant_phase_index": 2,
                "turn_cycle_index": 2,
            },
        ),
        RawSessionEvent(
            store_id="intaris",
            session_id="sess_cycle",
            seq=2,
            type="tool_result",
            data={
                "call_id": "call_1",
                "name": "read",
                "result": "contents",
                "is_error": False,
                "turn_id": "turn_1",
                "assistant_phase_index": 3,
                "turn_cycle_index": 3,
            },
        ),
    ]

    projection = project_timeline(normalize_session_events(raw_events).events)
    tool_call = next(item for item in projection.timeline.items if item.kind == "tool_call")

    assert tool_call.assistant_phase_index == 2
    assert tool_call.turn_cycle_index == 2


def test_assistant_missing_cycle_is_repaired_from_following_same_turn_tool_cycle() -> None:
    raw_events = [
        RawSessionEvent(
            store_id="intaris",
            session_id="sess_cycle_repair",
            seq=1,
            type="assistant_message",
            data={
                "message_id": "assistant_1",
                "content": "The patch failed; I will read the exact files.",
                "turn_id": "turn_1",
                "assistant_phase_index": 3,
            },
        ),
        RawSessionEvent(
            store_id="intaris",
            session_id="sess_cycle_repair",
            seq=2,
            type="tool_call",
            data={
                "call_id": "call_1",
                "name": "read",
                "arguments": {"file_path": "cognis/api/chat_v2/projector.py"},
                "turn_id": "turn_1",
                "assistant_phase_index": 4,
                "turn_cycle_index": 3,
            },
        ),
    ]

    projection = project_timeline(normalize_session_events(raw_events).events)

    message = next(item for item in projection.timeline.items if item.kind == "message")
    tool_call = next(item for item in projection.timeline.items if item.kind == "tool_call")

    assert message.turn_cycle_index == 3
    assert tool_call.turn_cycle_index == 3


def test_assistant_only_missing_cycle_remains_standalone() -> None:
    raw_events = [
        RawSessionEvent(
            store_id="intaris",
            session_id="sess_cycle_repair",
            seq=1,
            type="assistant_message",
            data={
                "message_id": "assistant_1",
                "content": "Done.",
                "turn_id": "turn_1",
                "assistant_phase_index": 3,
            },
        ),
        RawSessionEvent(
            store_id="intaris",
            session_id="sess_cycle_repair",
            seq=2,
            type="assistant_message",
            data={
                "message_id": "assistant_2",
                "content": "Next standalone message.",
                "turn_id": "turn_1",
                "assistant_phase_index": 4,
            },
        ),
    ]

    projection = project_timeline(normalize_session_events(raw_events).events)

    messages = [item for item in projection.timeline.items if item.kind == "message"]
    assert [message.turn_cycle_index for message in messages] == [None, None]


def test_delegation_projection_preserves_turn_metadata() -> None:
    raw_events = [
        RawSessionEvent(
            store_id="intaris",
            session_id="sess_delegation",
            seq=1,
            type="delegation",
            data={
                "status": "running",
                "mode": "delegate",
                "call_id": "call_delegation",
                "child_session_id": "child_1",
                "turn_id": "turn_1",
                "assistant_phase_index": 3,
                "turn_cycle_index": 2,
            },
        )
    ]

    projection = project_timeline(normalize_session_events(raw_events).events)

    item = projection.timeline.items[0]
    assert isinstance(item, DelegationTimelineItem)
    assert item.turn_id == "turn_1"
    assert item.assistant_phase_index == 3
    assert item.turn_cycle_index == 2


def test_folded_delegation_payload_preserves_turn_metadata() -> None:
    raw_events = [
        RawSessionEvent(
            store_id="intaris",
            session_id="sess_delegation",
            seq=1,
            type="tool_call",
            data={
                "call_id": "call_delegation",
                "name": "delegate",
                "turn_id": "turn_1",
                "assistant_phase_index": 3,
                "turn_cycle_index": 2,
            },
        ),
        RawSessionEvent(
            store_id="intaris",
            session_id="sess_delegation",
            seq=2,
            type="delegation",
            data={
                "status": "completed",
                "mode": "delegate",
                "call_id": "call_delegation",
                "child_session_id": "child_1",
                "turn_id": "turn_1",
                "assistant_phase_index": 3,
                "turn_cycle_index": 2,
            },
        ),
    ]

    projection = project_timeline(normalize_session_events(raw_events).events)

    item = projection.timeline.items[0]
    assert isinstance(item, ToolCallTimelineItem)
    assert item.turn_id == "turn_1"
    assert item.turn_cycle_index == 2
    assert item.delegation is not None
    assert item.delegation["turn_id"] == "turn_1"
    assert item.delegation["assistant_phase_index"] == 3
    assert item.delegation["turn_cycle_index"] == 2


def test_evaluation_events_use_tool_call_id_as_sidecar_target() -> None:
    raw_events = [
        RawSessionEvent(
            store_id="intaris",
            session_id="sess_eval",
            seq=1,
            type="evaluation",
            data={
                "call_id": "eval_1",
                "tool_call_id": "call_1",
                "decision": "escalate",
                "reasoning": "Needs approval",
                "risk": "medium",
            },
        ),
        RawSessionEvent(
            store_id="intaris",
            session_id="sess_eval",
            seq=2,
            type="tool_call",
            data={
                "call_id": "call_1",
                "tool_name": "bash",
                "arguments": {"command": "true"},
            },
        ),
    ]

    projection = project_timeline(normalize_session_events(raw_events).events)

    assert len(projection.timeline.items) == 1
    item = projection.timeline.items[0]
    assert isinstance(item, ToolCallTimelineItem)
    assert item.id == "tool:call_1"
    assert item.call_id == "call_1"
    assert item.evaluation == {
        "decision": "escalate",
        "reasoning": "Needs approval",
        "risk": "medium",
    }


def test_uncorrelated_evaluation_does_not_emit_tool_item() -> None:
    raw_events = [
        RawSessionEvent(
            store_id="intaris",
            session_id="sess_eval",
            seq=1,
            type="evaluation",
            data={
                "call_id": "eval_only",
                "decision": "approve",
                "reasoning": "No matching tool call",
            },
        ),
    ]

    projection = project_timeline(normalize_session_events(raw_events).events)

    assert [warning.code for warning in projection.warnings] == []
    assert projection.timeline.items == []


def test_canonical_internal_events_do_not_project_as_unsupported() -> None:
    raw_events = [
        RawSessionEvent(
            store_id="intaris",
            session_id="sess_internal",
            seq=index,
            type=event_type,
            data={"event": event_type, "message": f"{event_type} message"},
        )
        for index, event_type in enumerate(
            [
                "checkpoint",
                "context_snapshot",
                "developer_message",
                "part",
                "reasoning",
                "transcript",
            ],
            start=1,
        )
    ]

    normalization = normalize_session_events(raw_events)
    projection = project_timeline(normalization.events)

    assert normalization.skipped_count == len(raw_events)
    assert projection.warnings == []
    assert projection.timeline.items == []


def test_assistant_message_phases_with_same_message_id_stay_separate_items() -> None:
    """A multi-phase turn persists one assistant_message per phase, all sharing
    message_id == turn_id but with distinct assistant_phase_index. The projector
    must keep each phase as its own timeline item (phase-aware id) — collapsing
    them onto one id dropped mid-turn assistant text after reload."""
    raw_events = [
        RawSessionEvent(
            store_id="intaris",
            session_id="sess_assistant",
            seq=1,
            type="assistant_message",
            data={
                "message_id": "turn_1",
                "turn_id": "turn_1",
                "assistant_phase_index": 0,
                "content": "first segment",
            },
        ),
        RawSessionEvent(
            store_id="intaris",
            session_id="sess_assistant",
            seq=2,
            type="assistant_message",
            data={
                "message_id": "turn_1",
                "turn_id": "turn_1",
                "assistant_phase_index": 1,
                "content": "final segment",
            },
        ),
    ]

    projection = project_timeline(normalize_session_events(raw_events).events)

    assert [warning.code for warning in projection.warnings] == []
    items = [i for i in projection.timeline.items if isinstance(i, MessageTimelineItem)]
    assert len(items) == 2, "both assistant phase segments must be preserved"
    # Distinct, phase-aware ids; ordered by phase.
    assert [i.id for i in items] == ["message:turn_1:phase:0", "message:turn_1:phase:1"]
    assert [i.content for i in items] == ["first segment", "final segment"]
    assert [i.assistant_phase_index for i in items] == [0, 1]


def test_assistant_message_without_phase_uses_unphased_id() -> None:
    """When no assistant_phase_index is present, the id falls back to the
    unphased form (byte-identical to the runtime/legacy fallback)."""
    raw_events = [
        RawSessionEvent(
            store_id="intaris",
            session_id="sess_assistant",
            seq=1,
            type="assistant_message",
            data={"message_id": "turn_1", "turn_id": "turn_1", "content": "only"},
        ),
    ]
    projection = project_timeline(normalize_session_events(raw_events).events)
    items = [i for i in projection.timeline.items if isinstance(i, MessageTimelineItem)]
    assert len(items) == 1
    assert items[0].id == "message:turn_1"


def test_canonical_assistant_id_matches_runtime_and_legacy_id() -> None:
    """The canonical projector, the runtime overlay, and the legacy/completion
    builder must produce byte-identical assistant ids for the same (message_id,
    phase) so live and canonical items merge 1:1. Guards against regressing to
    the divergent `message:{id}` (canonical) vs `message:{id}:phase:{p}`
    (runtime) shapes that dropped mid-turn assistant messages."""
    from cognis.api.chat_v2.realtime import assistant_stream_runtime_item
    from cognis.api.routes.conversations import _stable_assistant_timeline_id

    for phase in (0, 1, 7):
        raw_events = [
            RawSessionEvent(
                store_id="intaris",
                session_id="sess_a",
                seq=1,
                type="assistant_message",
                data={
                    "message_id": "turn_x",
                    "turn_id": "turn_x",
                    "assistant_phase_index": phase,
                    "content": "segment",
                },
            )
        ]
        canonical = project_timeline(normalize_session_events(raw_events).events)
        canonical_item = next(
            i for i in canonical.timeline.items if isinstance(i, MessageTimelineItem)
        )
        runtime_item = assistant_stream_runtime_item(
            {
                "content": "segment",
                "message_id": "turn_x",
                "turn_id": "turn_x",
                "assistant_phase_index": phase,
                "session_id": "sess_a",
            },
            local=0,
        )
        legacy_id = _stable_assistant_timeline_id("turn_x", phase, "turn_x")
        assert canonical_item.id == legacy_id
        assert runtime_item is not None
        assert runtime_item.id == legacy_id


def test_multiple_thinking_blocks_remain_separate_timeline_items() -> None:
    raw_events = [
        RawSessionEvent(
            store_id="intaris",
            session_id="sess_thinking",
            seq=1,
            type="assistant_thinking",
            data={
                "message_id": "msg_assistant",
                "turn_id": "turn_1",
                "block_id": "block_1",
                "content": "First block",
                "assistant_phase_index": 0,
            },
        ),
        RawSessionEvent(
            store_id="intaris",
            session_id="sess_thinking",
            seq=2,
            type="assistant_thinking",
            data={
                "message_id": "msg_assistant",
                "turn_id": "turn_1",
                "block_id": "block_2",
                "content": "Second block",
                "assistant_phase_index": 0,
            },
        ),
    ]

    normalization = normalize_session_events(raw_events)
    projection = project_timeline(normalization.events)

    assert [_item_summary(item) for item in projection.timeline.items] == [
        {
            "id": "thinking:msg_assistant:phase:0:block_1",
            "kind": "thinking",
            "sort_key": "0000:000000000000001:000000:01:000000000",
            "source_ref_count": 1,
            "status": "complete",
            "message_id": "msg_assistant",
            "block_ids": ["block_1"],
        },
        {
            "id": "thinking:msg_assistant:phase:0:block_2",
            "kind": "thinking",
            "sort_key": "0000:000000000000002:000000:01:000000001",
            "source_ref_count": 1,
            "status": "complete",
            "message_id": "msg_assistant",
            "block_ids": ["block_2"],
        },
    ]


def test_tool_call_projects_structured_arguments() -> None:
    for raw_arguments in ({"path": "/tmp/x", "limit": 50}, '{"path": "/tmp/x", "limit": 50}'):
        raw_events = [
            RawSessionEvent(
                store_id="intaris",
                session_id="sess_tool",
                seq=1,
                type="tool_call",
                data={"call_id": "call_1", "name": "read", "arguments": raw_arguments},
            )
        ]
        projection = project_timeline(normalize_session_events(raw_events).events)
        item = projection.timeline.items[0]
        assert isinstance(item, ToolCallTimelineItem)
        assert item.arguments == {"path": "/tmp/x", "limit": 50}
        # When structured arguments are available the raw preview is omitted so
        # a stale repr(dict) string can never leak into the UI on a merge that
        # drops the structured dict.
        assert item.arguments_preview is None


def test_tool_call_preview_fallback_without_structured_arguments() -> None:
    """Arguments that cannot be structured (unparseable string) keep a preview
    so the tool card still has something to show."""
    raw_events = [
        RawSessionEvent(
            store_id="intaris",
            session_id="sess_tool",
            seq=1,
            type="tool_call",
            data={"call_id": "call_1", "name": "read", "arguments": "not json"},
        )
    ]
    projection = project_timeline(normalize_session_events(raw_events).events)
    item = projection.timeline.items[0]
    assert isinstance(item, ToolCallTimelineItem)
    assert item.arguments is None
    assert item.arguments_preview == "not json"


def test_thinking_block_projects_timing() -> None:
    raw_events = [
        RawSessionEvent(
            store_id="intaris",
            session_id="sess_think",
            seq=1,
            type="assistant_thinking",
            data={
                "message_id": "msg_1",
                "block_id": "blk_1",
                "content": "reasoning",
                "assistant_phase_index": 0,
                "started_at": "2026-01-01T00:00:00+00:00",
                "completed_at": "2026-01-01T00:00:02+00:00",
                "duration_ms": 2000,
            },
        )
    ]
    projection = project_timeline(normalize_session_events(raw_events).events)
    item = projection.timeline.items[0]
    assert isinstance(item, ThinkingTimelineItem)
    block = item.blocks[0]
    assert block.started_at == "2026-01-01T00:00:00+00:00"
    assert block.completed_at == "2026-01-01T00:00:02+00:00"
    assert block.duration_ms == 2000


def test_delegation_folds_onto_delegate_tool_call() -> None:
    raw_events = [
        RawSessionEvent(
            store_id="intaris",
            session_id="sess_deleg",
            seq=1,
            type="tool_call",
            data={
                "call_id": "call_d",
                "name": "delegate",
                "arguments": {"title": "Investigate X", "agent_id": "laforge"},
            },
        ),
        RawSessionEvent(
            store_id="intaris",
            session_id="sess_deleg",
            seq=2,
            type="delegation",
            data={
                "call_id": "call_d",
                "child_session_id": "sess_child",
                "status": "running",
                "title": "Investigate X",
                "used_agent_id": "laforge",
                "tool_call_count": 3,
                "max_tool_calls": 20,
                "last_tool": "grep",
                "todos": [{"content": "step a", "status": "in_progress"}],
            },
        ),
    ]
    projection = project_timeline(normalize_session_events(raw_events).events)
    # Only the delegate tool call remains; no separate delegation card.
    assert [item.kind for item in projection.timeline.items] == ["tool_call"]
    item = projection.timeline.items[0]
    assert isinstance(item, ToolCallTimelineItem)
    assert item.tool_name == "delegate"
    assert item.delegation is not None
    assert item.delegation["title"] == "Investigate X"
    assert item.delegation["used_agent_id"] == "laforge"
    assert item.delegation["tool_call_count"] == 3
    assert item.delegation["max_tool_calls"] == 20
    assert item.delegation["last_tool"] == "grep"
    assert item.delegation["todos"] == [{"content": "step a", "status": "in_progress"}]


def test_delegation_completion_without_call_id_folds_via_child_session() -> None:
    # The started event carries call_id; completion is keyed only by
    # child_session_id. Both must fold onto the same delegate tool call.
    raw_events = [
        RawSessionEvent(
            store_id="intaris",
            session_id="sess_deleg",
            seq=1,
            type="tool_call",
            data={"call_id": "call_d", "name": "delegate", "arguments": {"title": "X"}},
        ),
        RawSessionEvent(
            store_id="intaris",
            session_id="sess_deleg",
            seq=2,
            type="delegation",
            data={
                "call_id": "call_d",
                "child_session_id": "sess_child",
                "mode": "delegate",
                "status": "started",
                "title": "X",
            },
        ),
        RawSessionEvent(
            store_id="intaris",
            session_id="sess_deleg",
            seq=3,
            type="delegation",
            data={
                "child_session_id": "sess_child",
                "mode": "delegate",
                "status": "completed",
                "result_summary": "done",
                "result_content": "### Summary\nDone",
                "result_source": "assistant_messages",
                "result_truncated": False,
                "started_at": "2026-01-01T00:00:00+00:00",
                "duration_ms": 1234,
            },
        ),
    ]
    projection = project_timeline(normalize_session_events(raw_events).events)
    assert [item.kind for item in projection.timeline.items] == ["tool_call"]
    item = projection.timeline.items[0]
    assert isinstance(item, ToolCallTimelineItem)
    assert item.delegation is not None
    assert item.delegation["status"] == "completed"
    assert item.delegation["result_summary"] == "done"
    assert item.delegation["result_content"] == "### Summary\nDone"
    assert item.delegation["result_source"] == "assistant_messages"
    assert item.delegation["result_truncated"] is False
    assert item.delegation["started_at"] == "2026-01-01T00:00:00+00:00"
    assert item.delegation["duration_ms"] == 1234


def test_task_mode_delegation_keeps_standalone_card() -> None:
    # Async task delegations carry a call_id but must NOT fold onto a tool call.
    raw_events = [
        RawSessionEvent(
            store_id="intaris",
            session_id="sess_deleg",
            seq=1,
            type="delegation",
            data={
                "call_id": "call_t",
                "child_session_id": "task_abc",
                "mode": "task",
                "status": "started",
                "title": "Async task",
            },
        )
    ]
    projection = project_timeline(normalize_session_events(raw_events).events)
    assert [item.kind for item in projection.timeline.items] == ["delegation"]


def test_delegation_without_matching_tool_call_emits_standalone_card() -> None:
    raw_events = [
        RawSessionEvent(
            store_id="intaris",
            session_id="sess_deleg",
            seq=1,
            type="delegation",
            data={"child_session_id": "sess_child", "status": "running", "title": "Spawned"},
        )
    ]
    projection = project_timeline(normalize_session_events(raw_events).events)
    assert [item.kind for item in projection.timeline.items] == ["delegation"]


def test_delegation_projected_before_tool_call_folds_and_suppresses_card() -> None:
    """A delegation event ordered BEFORE its tool_call in the same window folds.

    Concurrent writers can invert seq order (the delegation completion lands
    at a lower seq than the tool_call batch). The later tool_call must attach
    the recorded payload and remove the provisional standalone card.
    """
    raw_events = [
        RawSessionEvent(
            store_id="intaris",
            session_id="sess_deleg",
            seq=1,
            type="delegation",
            data={
                "call_id": "call_d",
                "child_session_id": "sess_child",
                "mode": "delegate",
                "status": "completed",
                "result_summary": "done",
            },
        ),
        RawSessionEvent(
            store_id="intaris",
            session_id="sess_deleg",
            seq=2,
            type="tool_call",
            data={"call_id": "call_d", "name": "delegate", "arguments": {"title": "X"}},
        ),
    ]
    projection = project_timeline(normalize_session_events(raw_events).events)
    assert [item.kind for item in projection.timeline.items] == ["tool_call"]
    item = projection.timeline.items[0]
    assert isinstance(item, ToolCallTimelineItem)
    assert item.delegation is not None
    assert item.delegation["status"] == "completed"
    assert item.delegation["result_summary"] == "done"


def test_evaluation_feedback_without_tool_anchor_projects_notice() -> None:
    raw_events = [
        RawSessionEvent(
            store_id="intaris",
            session_id="sess_eval",
            seq=7,
            type="evaluation",
            data={
                "event": "evaluation_feedback",
                "attempt": 2,
                "decision": "revise",
                "feedback": "Add tests for the edge case.",
            },
        )
    ]
    projection = project_timeline(normalize_session_events(raw_events).events)
    assert len(projection.timeline.items) == 1
    item = projection.timeline.items[0]
    assert item.kind == "notice"
    assert "attempt 2" in getattr(item, "title", "")
    assert "revise" in getattr(item, "title", "")
    assert getattr(item, "message", None) == "Add tests for the edge case."


def test_tool_result_without_call_creates_recoverable_tool_item() -> None:
    raw_events = [
        RawSessionEvent(
            store_id="intaris",
            session_id="sess_tool",
            seq=5,
            type="tool_result",
            data={"call_id": "orphan_call", "result": "done"},
        )
    ]

    normalization = normalize_session_events(raw_events)
    projection = project_timeline(normalization.events)

    assert [_item_summary(item) for item in projection.timeline.items] == [
        {
            "id": "tool:orphan_call",
            "kind": "tool_call",
            "sort_key": "0000:000000000000005:000000:03:000000000",
            "source_ref_count": 1,
            "status": "complete",
            "call_id": "orphan_call",
            "tool_name": "tool",
            "result_preview": "done",
            "duration_ms": None,
            "has_full_output": False,
            "tool_output_artifact_id": None,
        }
    ]


def _load_fixture(fixture_name: str) -> dict[str, Any]:
    loaded = json.loads((FIXTURES_DIR / fixture_name).read_text())
    assert isinstance(loaded, dict)
    return loaded


def _item_summary(item: TimelineItem) -> dict[str, Any]:
    base: dict[str, Any] = {
        "id": item.id,
        "kind": item.kind,
        "sort_key": item.sort_key,
        "source_ref_count": len(item.source_refs),
    }
    if item.status is not None:
        base["status"] = item.status

    if isinstance(item, MessageTimelineItem):
        base.update(
            {
                "role": item.role,
                "content": item.content,
                "message_id": item.message_id,
            }
        )
        if item.client_message_id is not None:
            base["client_message_id"] = item.client_message_id
        if item.client_txn_id is not None:
            base["client_txn_id"] = item.client_txn_id
    elif isinstance(item, ThinkingTimelineItem):
        base.update(
            {
                "message_id": item.message_id,
                "block_ids": [block.id for block in item.blocks],
            }
        )
    elif isinstance(item, ToolCallTimelineItem):
        base.update(
            {
                "call_id": item.call_id,
                "tool_name": item.tool_name,
                "result_preview": item.result_preview,
                "duration_ms": item.duration_ms,
                "has_full_output": item.has_full_output,
                "tool_output_artifact_id": item.tool_output_artifact_id,
            }
        )
    elif isinstance(item, DelegationTimelineItem):
        base.update(
            {
                "child_session_id": item.child_session_id,
                "agent_id": item.agent_id,
                "result_summary": item.result_summary,
            }
        )
    elif isinstance(item, ManagedConversationTimelineItem):
        base.update(
            {
                "managed_conversation_id": item.managed_conversation_id,
                "agent_id": item.agent_id,
                "result_summary": item.result_summary,
            }
        )
    elif isinstance(item, TaskTimelineItem):
        base.update(
            {
                "task_id": item.task_id,
                "result_summary": item.result_summary,
            }
        )
    elif isinstance(item, QuestionSetTimelineItem):
        base["question_count"] = len(item.questions)
    elif isinstance(item, AuthChallengeTimelineItem):
        base.update(
            {
                "challenge_kind": item.challenge_kind,
                "required_fields": item.required_fields,
            }
        )
    elif isinstance(item, CredentialRequestTimelineItem):
        base.update(
            {
                "credential_id": item.credential_id,
                "credential_kind": item.credential_kind,
            }
        )
    elif isinstance(item, TodoStateTimelineItem):
        base["todo_count"] = len(item.todos)
    elif isinstance(item, ArtifactTimelineItem):
        base.update(
            {
                "artifact_id": item.artifact_id,
                "filename": item.filename,
            }
        )
    elif isinstance(item, FileDiffTimelineItem):
        base["file_diff_count"] = len(item.file_diffs)
    elif isinstance(item, NoticeTimelineItem):
        base.update(
            {
                "level": item.level,
                "title": item.title,
            }
        )
    return base
