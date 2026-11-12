from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from cognis.api.chat_v2.event_store import RawSessionEvent
from cognis.api.chat_v2.item_keys import assistant_message_item_id
from cognis.api.chat_v2.normalizer import normalize_session_events
from cognis.api.chat_v2.projector import project_timeline
from cognis.api.chat_v2.realtime import (
    compaction_runtime_item,
    tool_result_runtime_item,
)
from cognis.api.chat_v2.schemas import (
    ArtifactTimelineItem,
    AssistantDeliverableTimelineItem,
    AuthChallengeTimelineItem,
    CompactionTimelineItem,
    CredentialRequestTimelineItem,
    DelegationTimelineItem,
    FileDiffTimelineItem,
    HandoffTimelineItem,
    ManagedConversationTimelineItem,
    MessageTimelineItem,
    NoticeTimelineItem,
    QuestionSetTimelineItem,
    TaskTimelineItem,
    ThinkingTimelineItem,
    TimelineItem,
    TodoStateTimelineItem,
    ToolCallTimelineItem,
    UserInteractionTimelineItem,
)


@pytest.mark.parametrize("auto", [True, False])
def test_compaction_copied_recaps_are_hidden_but_original_recaps_remain(auto: bool) -> None:
    def recap(session_id: str, seq: int, *, copied: bool) -> RawSessionEvent:
        return RawSessionEvent(
            store_id="intaris",
            session_id=session_id,
            seq=seq,
            type="lifecycle",
            data={
                "event": "conversation_recap",
                "text": f"Summary {seq}",
                "auto": auto,
                "compaction_tail": copied,
                "source_session_id": "previous-session" if copied else session_id,
                "source_seq": seq,
            },
        )

    original = normalize_session_events([recap("previous-session", 1262, copied=False)])
    assert [item.kind for item in project_timeline(original.events).timeline.items] == ["recap"]
    copied = normalize_session_events(
        [recap("rotated-session", 70, copied=True), recap("rotated-session", 147, copied=True)]
    )
    assert copied.skipped_count == 2
    assert project_timeline(copied.events).timeline.items == []
    new = normalize_session_events([recap("rotated-session", 200, copied=False)])
    assert [item.kind for item in project_timeline(new.events).timeline.items] == ["recap"]


@pytest.mark.parametrize(
    ("metadata", "expected"),
    [
        (
            {"source": "controller_instruction", "private_controller_instruction": True},
            {"private_controller_instruction": True},
        ),
        (
            {
                "ts": "2026-10-28T20:00:00Z",
                "channel": "web",
                "sender": "Remote participant",
                "untrusted": True,
                "private_payload": "must not be projected",
            },
            {
                "ts": "2026-10-28T20:00:00Z",
                "channel": "web",
                "sender": "Remote participant",
                "untrusted": True,
            },
        ),
        (
            {
                "ts": "2026-10-28T20:00:00Z",
                "feedback": {
                    "submission_id": "fbsu_1",
                    "manifest_hash": "a" * 64,
                    "private_payload": "must not be projected",
                },
            },
            {
                "ts": "2026-10-28T20:00:00Z",
                "feedback": {"submission_id": "fbsu_1", "manifest_hash": "a" * 64},
            },
        ),
    ],
)
def test_message_provenance_survives_canonical_replay_without_arbitrary_metadata(
    metadata: dict[str, Any], expected: dict[str, Any]
) -> None:
    raw = [
        RawSessionEvent(
            store_id="intaris",
            session_id="sess-channel-provenance",
            seq=1,
            type="user_message",
            data={"content": "A message", "message_metadata": metadata},
        )
    ]
    first = project_timeline(normalize_session_events(raw).events).timeline
    replay = project_timeline(normalize_session_events(raw).events).timeline
    assert first == replay
    message = first.items[0]
    assert isinstance(message, MessageTimelineItem)
    assert message.message_metadata == expected


@pytest.mark.parametrize("attachment_index", [0, 1, 2])
def test_assistant_attachments_survive_same_phase_text_updates(attachment_index: int) -> None:
    raw = [
        RawSessionEvent(
            store_id="intaris",
            session_id="sess-attachments",
            seq=index + 1,
            type="assistant_message",
            data={
                "content": f"Text {index}",
                "turn_id": "turn-attachments",
                "message_id": "turn-attachments",
                "assistant_phase_index": 0,
                "attachments": (
                    [
                        {
                            "artifact_id": "art_report",
                            "filename": "report.csv",
                            "mime_type": "text/csv",
                            "kind": "file",
                            "size_bytes": 10,
                        }
                    ]
                    if index == attachment_index
                    else []
                ),
            },
        )
        for index in range(3)
    ]
    items = project_timeline(normalize_session_events(raw).events).timeline.items
    assert len(items) == 1
    assert isinstance(items[0], MessageTimelineItem)
    assert items[0].content == "Text 2"
    assert [attachment.artifact_id for attachment in items[0].attachments] == ["art_report"]


def test_output_continuation_segments_rebuild_one_complete_assistant_bubble() -> None:
    raw = [
        RawSessionEvent(
            store_id="intaris",
            session_id="sess-continuation",
            seq=index + 1,
            type="assistant_message",
            data={
                "content": content,
                "turn_id": "turn-continuation",
                "message_id": "turn-continuation",
                "assistant_phase_index": 0,
                "turn_cycle_index": index,
                "content_is_delta": index > 0,
            },
        )
        for index, content in enumerate(("First segment.", "Second segment.", "Final segment."))
    ]
    timeline = project_timeline(normalize_session_events(raw).events).timeline
    messages = [item for item in timeline.items if isinstance(item, MessageTimelineItem)]
    assert len(messages) == 1
    assert messages[0].content == "First segment.Second segment.Final segment."
    assert [ref.seq for ref in messages[0].source_refs] == [1, 2, 3]
    assert project_timeline(normalize_session_events(raw).events).timeline == timeline


@pytest.mark.parametrize("event_type", ["assistant_message", "message"])
def test_question_context_is_hidden_without_hiding_assistant_json(event_type: str) -> None:
    content = json.dumps([{"id": "scope", "question": "Which scope?"}])
    raw = [
        RawSessionEvent(
            store_id="intaris",
            session_id="sess-1",
            seq=1,
            type=event_type,
            data={
                "role": "assistant",
                "content": content,
                "source": "question_context",
                "question_id": "question-scope",
            },
        ),
        RawSessionEvent(
            store_id="intaris",
            session_id="sess-1",
            seq=2,
            type="assistant_message",
            data={"content": content},
        ),
    ]
    normalized = normalize_session_events(raw)
    assert normalized.skipped_count == 1
    items = project_timeline(normalized.events).timeline.items
    assert len(items) == 1
    assert isinstance(items[0], MessageTimelineItem)
    assert items[0].content == content
    assert items[0].source_refs[0].seq == 2


def test_handoff_lifecycle_restores_full_text_and_source_cutoff() -> None:
    raw = RawSessionEvent(
        store_id="intaris",
        session_id="sess-handoff-source",
        seq=12,
        type="lifecycle",
        data={
            "event": "conversation_handoff",
            "text": "# Continue\n\nAwait approval.",
            "focus": "Review",
            "source_session_id": "sess-handoff-source",
            "source_seq": 8,
        },
    )
    normalized = normalize_session_events([raw])
    result = project_timeline(normalized.events)
    item = result.timeline.items[0]
    assert isinstance(item, HandoffTimelineItem)
    assert item.text == raw.data["text"]
    assert item.focus == "Review"
    assert item.source_seq == 8
    assert item.source_session_id == "sess-handoff-source"
    assert item.source_refs[0].seq == 12
    assert project_timeline(normalize_session_events([raw]).events) == result


def test_handoff_progress_result_and_link_coalesce_and_keep_initial_position() -> None:
    base = {
        "event": "conversation_handoff",
        "request_id": "handoff-request",
        "source_session_id": "sess-handoff",
        "source_seq": 5,
        "focus": "Review",
    }
    running = RawSessionEvent(
        store_id="intaris",
        session_id="sess-handoff",
        seq=6,
        type="lifecycle",
        data={**base, "status": "running", "text": ""},
    )
    completed = RawSessionEvent(
        store_id="intaris",
        session_id="sess-handoff",
        seq=10,
        type="lifecycle",
        data={**base, "status": "completed", "text": "Verified brief."},
    )
    linked = RawSessionEvent(
        store_id="intaris",
        session_id="sess-handoff",
        seq=15,
        type="lifecycle",
        data={**completed.data, "recipient_conversation_id": "conv-recipient"},
    )
    initial = project_timeline(normalize_session_events([running]).events).timeline.items[0]
    result = project_timeline(
        normalize_session_events([running, completed, linked]).events
    ).timeline.items
    assert len(result) == 1
    assert isinstance(result[0], HandoffTimelineItem)
    assert result[0].id == initial.id
    assert result[0].sort_key == initial.sort_key
    assert result[0].status == "complete"
    assert result[0].text == "Verified brief."
    assert result[0].recipient_conversation_id == "conv-recipient"
    assert [ref.seq for ref in result[0].source_refs] == [6, 10, 15]
    replay = project_timeline(normalize_session_events([completed, linked, running]).events)
    assert replay.timeline.items[0].status == "complete"


def test_handoff_failure_and_legacy_recipient_link_preserve_card_identity() -> None:
    legacy = RawSessionEvent(
        store_id="intaris",
        session_id="sess-handoff",
        seq=8,
        type="lifecycle",
        data={"event": "conversation_handoff", "text": "Legacy brief."},
    )
    linked = RawSessionEvent(
        store_id="intaris",
        session_id="sess-handoff",
        seq=11,
        type="lifecycle",
        data={**legacy.data, "handoff_seq": 8, "recipient_conversation_id": "conv-recipient"},
    )
    items = project_timeline(normalize_session_events([legacy, linked]).events).timeline.items
    assert len(items) == 1
    assert items[0].id == "handoff:sess-handoff:8"
    failure = RawSessionEvent(
        store_id="intaris",
        session_id="sess-handoff",
        seq=12,
        type="lifecycle",
        data={
            "event": "conversation_handoff",
            "request_id": "failed-request",
            "status": "failed",
            "text": "",
            "error": "Handoff cancelled.",
        },
    )
    item = project_timeline(normalize_session_events([failure]).events).timeline.items[0]
    assert item.status == "failed"
    assert item.error == "Handoff cancelled."


@pytest.mark.parametrize(
    ("producer_status", "expected"),
    [
        ("started", "running"),
        ("running", "running"),
        ("completed", "complete"),
        ("complete", "complete"),
        ("failed", "failed"),
        ("cancelled", "cancelled"),
    ],
)
def test_delegation_history_preserves_producer_status(producer_status: str, expected: str) -> None:
    events = [
        RawSessionEvent(
            store_id="intaris",
            session_id="parent",
            seq=seq,
            type="delegation",
            data={
                "child_session_id": "child",
                "agent_id": "system:code-review",
                "status": status,
                "result_summary": "### Verdict\n**APPROVE**" if seq == 2 else None,
            },
        )
        for seq, status in [(1, "started"), (2, producer_status)]
    ]
    result = project_timeline(normalize_session_events(events).events)
    delegation = result.timeline.items[0]
    assert isinstance(delegation, DelegationTimelineItem)
    assert delegation.status == expected
    assert delegation.result_summary == "### Verdict\n**APPROVE**"
    assert project_timeline(normalize_session_events(events).events) == result


@pytest.mark.parametrize(
    ("metadata", "expected"),
    [
        ({}, "user:admitted-turn:turn-1"),
        ({"queue_id": "dtr-admission"}, "user:dtr-admission"),
        ({"client_message_id": "client-1", "queue_id": "dtr-admission"}, "user:client-1"),
        ({"client_txn_id": "txn-1"}, "user-txn:txn-1"),
        ({"source": "queued_input", "message_id": "queued-1"}, "user:queued-1"),
    ],
)
def test_scheduler_admission_identity(metadata: dict[str, Any], expected: str) -> None:
    events = normalize_session_events(
        [
            RawSessionEvent(
                store_id="intaris",
                session_id="sess-1",
                seq=887,
                type="user_message",
                data={
                    "content": "continue",
                    "source": "user_input",
                    "turn_id": "turn-1",
                    **metadata,
                },
            ),
            RawSessionEvent(
                store_id="intaris",
                session_id="sess-1",
                seq=889,
                type="user_message",
                data={"content": "continue", "turn_id": "turn-1", "message_id": "distinct-input"},
            ),
        ]
    )
    items = project_timeline(events.events).timeline.items
    assert [item.id for item in items] == [expected, "user:distinct-input"]


@pytest.mark.parametrize("status", ["running", "completed"])
@pytest.mark.parametrize("trigger", [None, "conversation_idle", "turn_completed"])
def test_hook_card_preserves_type_and_session(status: str, trigger: str | None) -> None:
    events = normalize_session_events(
        [
            RawSessionEvent(
                store_id="intaris",
                session_id="sess-1",
                seq=1,
                type="lifecycle",
                data={
                    "event": "agent_hook",
                    "hook_run_id": "run-card",
                    "hook_id": "hook-card",
                    "name": "Debrief",
                    "status": status,
                    "trigger": trigger,
                    "child_session_id": "sess-child",
                },
            )
        ]
    )
    item = project_timeline(events.events).timeline.items[0]
    assert isinstance(item, NoticeTimelineItem)
    assert item.hook is not None
    assert item.hook.trigger == trigger
    assert item.hook.child_session_id == "sess-child"


def test_terminal_compaction_lifecycle_projects_with_occurrence_identity() -> None:
    normalization = normalize_session_events(
        [
            RawSessionEvent(
                store_id="intaris",
                session_id="sess-1",
                seq=7,
                type="lifecycle",
                data={
                    "event": "session_compaction_finished",
                    "compaction_id": "compact-1",
                    "session_id": "sess-1",
                    "status": "skipped",
                    "trigger": "pre_turn_auto",
                    "reason": "context_compaction_threshold",
                    "fallback_reason": "no_compactable_history",
                },
            )
        ]
    )

    item = project_timeline(normalization.events).timeline.items[0]
    assert isinstance(item, CompactionTimelineItem)
    assert item.id == "compaction:compact-1"
    assert item.status == "skipped"
    assert item.summary_preview == "no_compactable_history"
    assert item.reason == "no_compactable_history"


def test_session_recovery_lifecycle_projects_as_notice() -> None:
    normalization = normalize_session_events(
        [
            RawSessionEvent(
                store_id="intaris",
                session_id="sess-1",
                seq=3,
                type="lifecycle",
                data={
                    "event": "session_recovered",
                    "notice_id": "session-recovered:sess-1:controller_restart",
                    "title": "Controller restarted",
                    "message": "Saved work is preserved.",
                },
            )
        ]
    )

    item = project_timeline(normalization.events).timeline.items[0]
    assert isinstance(item, NoticeTimelineItem)
    assert item.title == "Controller restarted"


def test_runtime_compaction_uses_canonical_id() -> None:
    compaction = compaction_runtime_item(
        {
            "session_id": "sess-1",
            "compaction_id": "compact-1",
            "status": "running",
        }
    )
    assert compaction is not None
    assert compaction.id == "compaction:compact-1"


FIXTURES_DIR = Path(__file__).parent / "fixtures"


def test_escalation_runtime_waiting_and_denial_have_distinct_statuses() -> None:
    waiting = tool_result_runtime_item(
        session_id="sess-1",
        call_id="tool-call-1",
        tool_name="bash",
        result="Waiting for user approval...",
        is_error=False,
        duration_ms=None,
        evaluation={"phase": "waiting_for_approval"},
        attachments=None,
        file_diffs=None,
        turn_id="turn-1",
        assistant_phase_index=0,
        turn_cycle_index=0,
        timestamp="2026-08-17T15:00:00Z",
    )
    denied = tool_result_runtime_item(
        session_id="sess-1",
        call_id="tool-call-1",
        tool_name="bash",
        result="User denied the tool call. Too risky",
        is_error=True,
        duration_ms=None,
        evaluation={"resolution": "deny", "note": "Too risky"},
        attachments=None,
        file_diffs=None,
        turn_id="turn-1",
        assistant_phase_index=0,
        turn_cycle_index=0,
        timestamp="2026-08-17T15:00:01Z",
    )

    assert waiting.id == denied.id == "tool:tool-call-1"
    assert waiting.status == "waiting"
    assert waiting.is_error is False
    assert denied.status == "denied"
    assert denied.is_error is True


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


def test_model_only_workflow_prompt_is_hidden_but_literal_user_xml_is_visible() -> None:
    wrapper = (
        '<workflow_prompt_block kind="user_task_contract" lifetime="workflow" '
        'source="task" trust="user">\nliteral\n</workflow_prompt_block>'
    )
    raw_events = [
        RawSessionEvent(
            store_id="intaris",
            session_id="sess",
            seq=1,
            type="user_message",
            prompt_visibility="model_only",
            data={
                "content": wrapper,
                "prompt_visibility": "model_only",
                "prompt_provenance": {
                    "kind": "internal_workflow_prompt",
                    "task_id": "task-1",
                },
            },
        ),
        RawSessionEvent(
            store_id="intaris",
            session_id="sess",
            seq=2,
            type="user_message",
            data={"content": wrapper, "client_message_id": "literal-user"},
        ),
    ]

    result = normalize_session_events(raw_events)

    assert result.skipped_count == 1
    assert [event.data["content"] for event in result.events] == [wrapper]


def test_model_only_workflow_prompt_exposes_only_its_user_visible_revision() -> None:
    raw_events = [
        RawSessionEvent(
            store_id="intaris",
            session_id="sess",
            seq=1,
            type="user_message",
            prompt_visibility="model_only",
            data={
                "content": "internal workflow prompt",
                "user_visible_content": "Keep the API compatible.",
                "prompt_provenance": {
                    "kind": "internal_workflow_prompt",
                    "task_id": "task-1",
                    "step_run_id": "step-2",
                },
            },
        )
    ]

    normalization = normalize_session_events(raw_events)
    projection = project_timeline(normalization.events)

    assert normalization.skipped_count == 0
    assert len(projection.timeline.items) == 1
    item = projection.timeline.items[0]
    assert isinstance(item, MessageTimelineItem)
    assert item.role == "user"
    assert item.content == "Keep the API compatible."


def test_legacy_model_only_delegation_input_is_visible() -> None:
    raw_events = [
        RawSessionEvent(
            store_id="intaris",
            session_id="sess_delegate",
            seq=1,
            type="user_message",
            prompt_visibility="model_only",
            data={
                "content": "Research replacement adapters",
                "source": "delegation_input",
                "prompt_visibility": "model_only",
                "prompt_provenance": {"kind": "internal_workflow_prompt"},
            },
        )
    ]

    normalization = normalize_session_events(raw_events)
    projection = project_timeline(normalization.events)

    assert normalization.skipped_count == 0
    assert len(projection.timeline.items) == 1
    item = projection.timeline.items[0]
    assert isinstance(item, MessageTimelineItem)
    assert item.role == "user"
    assert item.content == "Research replacement adapters"


def test_internal_workflow_envelope_stays_hidden_when_mislabeled_as_delegation_input() -> None:
    wrapper = (
        '<workflow_prompt_block kind="user_task_contract" lifetime="workflow" '
        'source="task" trust="user">\ninternal\n</workflow_prompt_block>'
    )
    raw_events = [
        RawSessionEvent(
            store_id="intaris",
            session_id="sess_delegate",
            seq=1,
            type="user_message",
            prompt_visibility="model_only",
            data={
                "content": wrapper,
                "source": "delegation_input",
                "prompt_visibility": "model_only",
                "prompt_provenance": {
                    "kind": "internal_workflow_prompt",
                    "task_id": "task-1",
                },
            },
        )
    ]

    normalization = normalize_session_events(raw_events)

    assert normalization.skipped_count == 1
    assert normalization.events == []


def test_legacy_attachment_without_kind_does_not_break_projection() -> None:
    raw_events = [
        RawSessionEvent(
            store_id="intaris",
            session_id="sess_legacy_attachment",
            seq=1,
            type="user_message",
            data={
                "content": "Legacy image",
                "client_message_id": "legacy_attachment",
                "attachments": [
                    {
                        "artifact_id": "img_legacy",
                        "mime_type": "image/png",
                        "filename": "legacy.png",
                        "size_bytes": 512,
                    }
                ],
            },
        )
    ]

    projection = project_timeline(normalize_session_events(raw_events).events)

    item = projection.timeline.items[0]
    assert isinstance(item, MessageTimelineItem)
    assert item.attachments[0].kind == "image"


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


def test_turn_error_lifecycle_projects_as_error() -> None:
    raw_events = [
        RawSessionEvent(
            store_id="intaris",
            session_id="sess_failed",
            seq=2,
            type="lifecycle",
            data={
                "event": "turn_error",
                "turn_id": "turn_failed",
                "message": "Executor unavailable",
                "error_detail": "Connection refused by executor.",
                "code": "executor_unavailable",
                "recoverable": True,
            },
        )
    ]

    normalization = normalize_session_events(raw_events)
    projection = project_timeline(normalization.events)

    assert normalization.skipped_count == 0
    assert len(projection.timeline.items) == 1
    assert projection.timeline.items[0].kind == "error"
    assert projection.timeline.items[0].error_code == "executor_unavailable"
    assert projection.timeline.items[0].error_detail == "Connection refused by executor."
    assert projection.timeline.items[0].recoverable is True


@pytest.mark.parametrize("event_type", ["user_message", "message"])
def test_user_interaction_lifecycle_projects_a_safe_answer_card(event_type: str) -> None:
    raw_events = [
        RawSessionEvent(
            store_id="intaris",
            session_id="sess_interaction",
            seq=2,
            type=event_type,
            data={
                "role": "user",
                "content": "Answer 1: Staging\nAnswer 2: Verify the migration",
                "source": "question_answer",
                "question_id": "notif_question",
            },
        ),
        RawSessionEvent(
            store_id="intaris",
            session_id="sess_interaction",
            seq=4,
            type="lifecycle",
            data={
                "event": "user_interaction_resolved",
                "interaction_id": "notif_question",
                "interaction_type": "step_question",
                "origin_call_id": "call_question",
                "title": "You answered questions",
                "answers": [
                    {"question": "Deployment target?", "answer": "Staging"},
                    {"question": "Why?", "answer": "Verify the migration"},
                ],
                "status": "complete",
            },
        ),
    ]

    projection = project_timeline(normalize_session_events(raw_events).events)

    assert len(projection.timeline.items) == 1
    item = projection.timeline.items[0]
    assert isinstance(item, UserInteractionTimelineItem)
    assert item.id == "user-interaction:notif_question"
    assert item.origin_call_id == "call_question"
    assert [(answer.question, answer.answer) for answer in item.answers] == [
        ("Deployment target?", "Staging"),
        ("Why?", "Verify the migration"),
    ]

    # Identical ordinary user prose must not be deduplicated by content.
    ordinary = raw_events[0].model_copy(update={"data": {"role": "user", "content": "Staging"}})
    assert len(project_timeline(normalize_session_events([ordinary]).events).timeline.items) == 1


def test_cancelled_turn_lifecycle_projects_as_system_message() -> None:
    raw_events = [
        RawSessionEvent(
            store_id="intaris",
            session_id="sess_cancelled",
            seq=2,
            type="lifecycle",
            data={
                "event": "turn_error",
                "turn_id": "turn_cancelled",
                "message": "The turn was cancelled.",
                "error_code": "turn_cancelled",
                "recoverable": True,
            },
        )
    ]

    normalization = normalize_session_events(raw_events)
    projection = project_timeline(normalization.events)

    assert normalization.skipped_count == 0
    assert len(projection.timeline.items) == 1
    item = projection.timeline.items[0]
    assert isinstance(item, MessageTimelineItem)
    assert item.kind == "message"
    assert item.role == "system"
    assert item.content == "The turn was cancelled."
    assert item.turn_id == "turn_cancelled"


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
                "retry_reason": "controller_restart",
                "retry_source_turn_id": "turn-source",
                "attempt": 2,
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
    assert system_item.retry_reason == "controller_restart"
    assert system_item.retry_source_turn_id == "turn-source"
    assert system_item.attempt == 2
    assert system_item.follow_up_conversation_id == "conv_follow"
    assert system_item.follow_up_session_id == "sess_follow"


def test_turn_initiated_system_notice_precedes_follow_up_activity_in_canonical_history() -> None:
    raw_events = [
        RawSessionEvent(
            store_id="intaris",
            session_id="sess_follow_up",
            seq=1,
            type="system_message",
            data={
                "notice_id": "turn-init:fup_1",
                "kind": "turn_initiated",
                "scope": "turn",
                "turn_id": "turn_fup_1",
                "content": "Turn initiated by other: Agent work finished.",
            },
        ),
        RawSessionEvent(
            store_id="intaris",
            session_id="sess_follow_up",
            seq=2,
            type="assistant_message",
            data={
                "message_id": "msg_1",
                "turn_id": "turn_fup_1",
                "content": "Current result.",
            },
        ),
    ]

    projection = project_timeline(normalize_session_events(raw_events).events)

    assert [
        (item.kind, getattr(item, "role", None), getattr(item, "content", None))
        for item in projection.timeline.items
    ] == [
        ("message", "system", "Turn initiated by other: Agent work finished."),
        ("message", "assistant", "Current result."),
    ]
    assert projection.timeline.items[0].id == "system:turn-init:fup_1"
    assert projection.timeline.items[0].sort_key < projection.timeline.items[1].sort_key


def test_turn_initiated_system_notice_precedes_follow_up_failure() -> None:
    raw_events = [
        RawSessionEvent(
            store_id="intaris",
            session_id="sess_follow_up",
            seq=1,
            type="system_message",
            data={
                "notice_id": "turn-init:fup_failed",
                "kind": "turn_initiated",
                "scope": "turn",
                "turn_id": "turn_failed",
                "content": "Turn initiated by task failure.",
            },
        ),
        RawSessionEvent(
            store_id="intaris",
            session_id="sess_follow_up",
            seq=2,
            type="lifecycle",
            data={
                "event": "turn_error",
                "turn_id": "turn_failed",
                "error_id": "follow-up-error",
                "title": "Follow-up turn failed",
                "message": "The follow-up turn did not complete.",
                "error_code": "turn_failed",
                "recoverable": False,
            },
        ),
    ]

    projection = project_timeline(normalize_session_events(raw_events).events)

    assert [item.id for item in projection.timeline.items] == [
        "system:turn-init:fup_failed",
        "error:follow-up-error",
    ]
    assert projection.timeline.items[0].sort_key < projection.timeline.items[1].sort_key


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
                "scope": "failed_turn",
                "reason_class": "rate_limit",
                "provider_id": "anthropic-demo",
                "model": "claude-fable-5",
                "retry_after_seconds": 23,
                "provider_retry_after_seconds": 23,
                "retry_at": "2026-07-09T13:28:00+00:00",
                "attempt": 1,
                "max_attempts": 3,
                "attempts": 3,
                "attempts_per_cycle": 1,
                "continuation_attempts": 2,
                "recoverable": True,
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
    assert item.notice_scope == "failed_turn"
    assert item.reason_class == "rate_limit"
    assert item.provider_id == "anthropic-demo"
    assert item.model == "claude-fable-5"
    assert item.retry_after_seconds == 23
    assert item.provider_retry_after_seconds == 23
    assert item.retry_at == "2026-07-09T13:28:00+00:00"
    assert item.attempt == 1
    assert item.max_attempts == 3
    assert item.attempts == 3
    assert item.attempts_per_cycle == 1
    assert item.continuation_attempts == 2
    assert item.recoverable is True


def test_turn_error_preserves_turn_identity() -> None:
    projection = project_timeline(
        normalize_session_events(
            [
                RawSessionEvent(
                    store_id="intaris",
                    session_id="sess_failure",
                    seq=1,
                    type="lifecycle",
                    data={
                        "event": "turn_error",
                        "turn_id": "turn_failure",
                        "error_id": "turn_failure",
                        "error_code": "step_failed",
                        "message": "Model stopped producing output.",
                    },
                )
            ]
        ).events
    )
    item = projection.timeline.items[0]
    assert item.kind == "error"
    assert item.turn_id == "turn_failure"
    assert item.model_dump()["turn_id"] == "turn_failure"


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
                "events_compacted": 140,
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
    assert item.id == "compaction:sess_old"
    assert item.status == "compacted"
    assert item.session_id == "sess_new"
    assert item.previous_session_id == "sess_old"
    assert item.summary_preview == "Compacted history summary"
    assert item.method == "rotation"
    assert item.turns_compacted == 7
    assert item.events_compacted == 140
    assert item.previous_usage_percentage == 86.1
    assert item.effective_usage_percentage == 72.5
    assert item.hard_pressure_exceeded is True


def test_legacy_and_rotated_compaction_markers_fold_to_one_card() -> None:
    raw_events = [
        RawSessionEvent(
            store_id="intaris",
            session_id="sess_old",
            seq=11,
            type="compaction_summary",
            data={
                "summary": "Legacy source-session checkpoint",
                "method": "llm",
                "turns_compacted": 7,
            },
        ),
        RawSessionEvent(
            store_id="intaris",
            session_id="sess_new",
            seq=2,
            type="compaction_summary",
            data={
                "session_id": "sess_new",
                "source_session_id": "sess_old",
                "summary": "Rotated-session marker",
                "method": "rotation",
                "turns_compacted": 7,
                "timeline_visible": True,
            },
        ),
    ]

    projection = project_timeline(normalize_session_events(raw_events).events)

    assert len(projection.timeline.items) == 1
    item = projection.timeline.items[0]
    assert isinstance(item, CompactionTimelineItem)
    assert item.id == "compaction:sess_old"
    assert item.session_id == "sess_new"
    assert item.summary == "Rotated-session marker"
    assert len(item.source_refs) == 2


def test_source_compaction_checkpoint_uses_runtime_stable_id() -> None:
    raw_events = [
        RawSessionEvent(
            store_id="intaris",
            session_id="sess_old",
            seq=11,
            type="compaction_summary",
            data={
                "summary": "Internal checkpoint",
                "timeline_visible": True,
            },
        ),
    ]

    projection = project_timeline(normalize_session_events(raw_events).events)

    assert len(projection.timeline.items) == 1
    item = projection.timeline.items[0]
    assert isinstance(item, CompactionTimelineItem)
    assert item.id == "compaction:sess_old"
    assert item.session_id == "sess_old"


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


def test_tool_result_backfills_cycle_when_tool_call_event_lacks_stamp() -> None:
    """The primary regular-tool path historically recorded tool_call events
    without cycle/phase while the paired tool_result carried them. The
    projector must backfill from the tool_result rather than freezing the
    tool_call's null, otherwise the grouping key is lost after reload."""

    raw_events = [
        RawSessionEvent(
            store_id="intaris",
            session_id="sess_cycle",
            seq=1,
            type="tool_call",
            data={
                "call_id": "call_1",
                "name": "web_fetch",
                "arguments": {"url": "https://example.com"},
                "turn_id": "turn_1",
                # No assistant_phase_index / turn_cycle_index (legacy batch path).
            },
        ),
        RawSessionEvent(
            store_id="intaris",
            session_id="sess_cycle",
            seq=2,
            type="tool_result",
            data={
                "call_id": "call_1",
                "name": "web_fetch",
                "result": "contents",
                "is_error": False,
                "turn_id": "turn_1",
                "assistant_phase_index": 2,
                "turn_cycle_index": 1,
            },
        ),
    ]

    projection = project_timeline(normalize_session_events(raw_events).events)
    tool_call = next(item for item in projection.timeline.items if item.kind == "tool_call")

    assert tool_call.turn_cycle_index == 1
    assert tool_call.assistant_phase_index == 2


def test_production_snapshot_shape_tools_missing_cycle_are_repaired() -> None:
    """Reproduces the 2026-07-08 production snapshot: thinking events carry an
    int cycle, but tool_call/tool_result events for the same turn were persisted
    without one. The positional repair must stamp the tool items with the
    surrounding cycle so they fold with the thinking segment instead of
    fragmenting into standalone cards."""

    raw_events = [
        RawSessionEvent(
            store_id="intaris",
            session_id="sess_prod",
            seq=1,
            type="user_message",
            data={"content": "Explain this.", "client_message_id": "cmsg_1", "turn_id": "turn_1"},
        ),
        RawSessionEvent(
            store_id="intaris",
            session_id="sess_prod",
            seq=2,
            type="tool_call",
            data={
                "call_id": "call_a",
                "name": "web_fetch",
                "arguments": {"url": "https://example.com/1"},
                "turn_id": "turn_1",
                # Batch path: no cycle/phase.
            },
        ),
        RawSessionEvent(
            store_id="intaris",
            session_id="sess_prod",
            seq=3,
            type="tool_result",
            data={
                "call_id": "call_a",
                "name": "web_fetch",
                "result": "first",
                "is_error": False,
                "turn_id": "turn_1",
                # Result recorded before result-side stamping existed either.
            },
        ),
        RawSessionEvent(
            store_id="intaris",
            session_id="sess_prod",
            seq=4,
            type="assistant_thinking",
            data={
                "message_id": "turn_1",
                "block_id": "think_1",
                "content": "Considering options",
                "turn_id": "turn_1",
                "assistant_phase_index": 4,
                "turn_cycle_index": 2,
            },
        ),
        RawSessionEvent(
            store_id="intaris",
            session_id="sess_prod",
            seq=5,
            type="tool_call",
            data={
                "call_id": "call_b",
                "name": "bash",
                "arguments": {"command": "curl -s https://example.com/2"},
                "turn_id": "turn_1",
            },
        ),
        RawSessionEvent(
            store_id="intaris",
            session_id="sess_prod",
            seq=6,
            type="tool_result",
            data={
                "call_id": "call_b",
                "name": "bash",
                "result": "second",
                "is_error": False,
                "turn_id": "turn_1",
            },
        ),
    ]

    projection = project_timeline(normalize_session_events(raw_events).events)
    tool_calls = [item for item in projection.timeline.items if item.kind == "tool_call"]

    # Both tools inherit a same-turn cycle so they carry the grouping key. The
    # first tool precedes the thinking cycle (2) and inherits it via the forward
    # scan; the bash tool follows the thinking event and also resolves to 2.
    assert all(isinstance(tc.turn_cycle_index, int) for tc in tool_calls)
    assert {tc.turn_cycle_index for tc in tool_calls} == {2}


def test_final_answer_after_tool_cycle_stays_standalone_while_tools_repaired() -> None:
    """A trailing assistant answer with no following tools must NOT inherit the
    prior cycle (it is the final answer and renders standalone), even though the
    preceding unstamped tools DO inherit it via the backward fallback."""

    raw_events = [
        RawSessionEvent(
            store_id="intaris",
            session_id="sess_final",
            seq=1,
            type="assistant_thinking",
            data={
                "message_id": "turn_1",
                "block_id": "think_1",
                "content": "Planning",
                "turn_id": "turn_1",
                "assistant_phase_index": 0,
                "turn_cycle_index": 0,
            },
        ),
        RawSessionEvent(
            store_id="intaris",
            session_id="sess_final",
            seq=2,
            type="tool_call",
            data={
                "call_id": "call_a",
                "name": "bash",
                "arguments": {"command": "ls"},
                "turn_id": "turn_1",
                # Unstamped batch path; must inherit cycle 0 backward.
            },
        ),
        RawSessionEvent(
            store_id="intaris",
            session_id="sess_final",
            seq=3,
            type="tool_result",
            data={
                "call_id": "call_a",
                "name": "bash",
                "result": "files",
                "is_error": False,
                "turn_id": "turn_1",
            },
        ),
        RawSessionEvent(
            store_id="intaris",
            session_id="sess_final",
            seq=4,
            type="assistant_message",
            data={
                "message_id": "turn_1",
                "content": "Here is the answer.",
                "turn_id": "turn_1",
                "assistant_phase_index": 1,
                # No cycle: this is the final answer, no following tools.
            },
        ),
    ]

    projection = project_timeline(normalize_session_events(raw_events).events)

    tool_call = next(item for item in projection.timeline.items if item.kind == "tool_call")
    answer = next(
        item
        for item in projection.timeline.items
        if item.kind == "message" and item.role == "assistant"
    )

    assert tool_call.turn_cycle_index == 0
    assert answer.turn_cycle_index is None


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
                "error_code": "delegate_agent_not_found",
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
    assert item.delegation["error_code"] == "delegate_agent_not_found"


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


def test_canonical_assistant_id_matches_runtime_id() -> None:
    """The projector and runtime overlay share the canonical assistant id."""
    from cognis.api.chat_v2.realtime import assistant_stream_runtime_item

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
        canonical_id = assistant_message_item_id(message_id="turn_x", phase=phase)
        assert canonical_item.id == canonical_id
        assert runtime_item is not None
        assert runtime_item.id == canonical_id


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


def test_deferred_bridge_projects_canonical_tool_identity_and_arguments() -> None:
    raw_events = [
        RawSessionEvent(
            store_id="intaris",
            session_id="sess_tool",
            seq=1,
            type="tool_call",
            data={
                "call_id": "call_1",
                "name": "get_task",
                "canonical_name": "get_task",
                "visible_name": "call_tool",
                "arguments": {"task_id": "task_sched_123"},
                "visible_arguments": {
                    "tool": "builtin:get_task",
                    "arguments": {"task_id": "task_sched_123"},
                },
            },
        ),
        RawSessionEvent(
            store_id="intaris",
            session_id="sess_tool",
            seq=2,
            type="tool_result",
            data={"call_id": "call_1", "result": '{"status":"completed"}'},
        ),
    ]

    projection = project_timeline(normalize_session_events(raw_events).events)
    item = projection.timeline.items[0]

    assert isinstance(item, ToolCallTimelineItem)
    assert item.tool_name == "get_task"
    assert item.display_name is None
    assert item.arguments == {"task_id": "task_sched_123"}


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


@pytest.mark.parametrize(
    "tool_name",
    ["delegate", "retry_subsession", "follow_up_subsession", "fork_subsession", "fork"],
)
def test_delegation_folds_onto_delegated_subsession_tool_call(tool_name: str) -> None:
    raw_events = [
        RawSessionEvent(
            store_id="intaris",
            session_id="sess_deleg",
            seq=1,
            type="tool_call",
            data={
                "call_id": "call_d",
                "name": tool_name,
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
    # Only the delegated sub-session tool call remains; no standalone card.
    assert [item.kind for item in projection.timeline.items] == ["tool_call"]
    item = projection.timeline.items[0]
    assert isinstance(item, ToolCallTimelineItem)
    assert item.tool_name == tool_name
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


def test_task_mode_delegation_is_hidden_from_conversation_timeline() -> None:
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
    assert projection.timeline.items == []


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


@pytest.mark.parametrize(
    "tool_name",
    ["delegate", "retry_subsession", "follow_up_subsession", "fork_subsession", "fork"],
)
def test_delegation_projected_before_tool_call_folds_and_suppresses_card(
    tool_name: str,
) -> None:
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
            data={"call_id": "call_d", "name": tool_name, "arguments": {"title": "X"}},
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


def test_assistant_deliverable_event_projects_reference_only_item() -> None:
    raw_events = [
        RawSessionEvent(
            store_id="intaris",
            session_id="sess_1",
            seq=1,
            event_id="evt-deliverable",
            type="lifecycle",
            data={
                "event": "assistant_deliverable",
                "deliverable_id": "dlv_rich",
                "turn_id": "report-turn",
                "format": "rich",
                "title": "Rich report",
                "render_metadata": {"presentation": "rich"},
            },
        )
    ]

    projection = project_timeline(normalize_session_events(raw_events).events)

    assert len(projection.timeline.items) == 1
    item = projection.timeline.items[0]
    assert isinstance(item, AssistantDeliverableTimelineItem)
    assert item.deliverable_id == "dlv_rich"
    assert item.turn_id == "report-turn"
    assert item.format == "rich"
    assert item.title == "Rich report"
    assert item.render_metadata == {"presentation": "rich"}
    assert not hasattr(item, "rich_payload")


def test_deferred_assistant_deliverable_projects_after_final_assistant_message() -> None:
    raw_events = [
        RawSessionEvent(
            store_id="intaris",
            session_id="sess_1",
            seq=1,
            event_id="evt-user",
            type="user_message",
            data={"content": "Build report", "client_message_id": "client-1"},
        ),
        RawSessionEvent(
            store_id="intaris",
            session_id="sess_1",
            seq=2,
            event_id="evt-tool",
            type="tool_call",
            data={
                "call_id": "call_write",
                "tool_name": "write_deliverable",
                "arguments": {"content": "# Draft"},
            },
        ),
        RawSessionEvent(
            store_id="intaris",
            session_id="sess_1",
            seq=3,
            event_id="evt-final",
            type="assistant_message",
            data={
                "content": "Completed validation. The final deliverable follows.",
                "message_id": "turn-1",
                "turn_id": "turn-1",
            },
        ),
        RawSessionEvent(
            store_id="intaris",
            session_id="sess_1",
            seq=4,
            event_id="evt-deliverable",
            type="lifecycle",
            data={
                "event": "assistant_deliverable",
                "deliverable_id": "dlv_final",
                "format": "markdown",
                "title": "Final report",
            },
        ),
    ]

    projection = project_timeline(normalize_session_events(raw_events).events)

    assistant_index = next(
        index
        for index, item in enumerate(projection.timeline.items)
        if isinstance(item, MessageTimelineItem) and item.role == "assistant"
    )
    deliverable_index = next(
        index
        for index, item in enumerate(projection.timeline.items)
        if item.id == "assistant-deliverable:dlv_final"
    )
    assert assistant_index < deliverable_index
    deliverable_item = projection.timeline.items[deliverable_index]
    assert isinstance(deliverable_item, AssistantDeliverableTimelineItem)
    assert deliverable_item.sort_key > projection.timeline.items[assistant_index].sort_key


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


def test_program_linkage_survives_call_result_coalescing_without_changing_identity_or_order() -> (
    None
):
    raw_events = [
        RawSessionEvent(
            store_id="intaris",
            session_id="sess_program",
            seq=10,
            type="tool_call",
            data={
                "call_id": "call-program",
                "name": "python_execute",
                "arguments": {"description": "Read and summarize the report", "code": "print(1)"},
                "turn_id": "turn-program",
                "assistant_phase_index": 2,
                "turn_cycle_index": 1,
                "program_id": "program-7",
                "kernel_generation": 4,
            },
        ),
        RawSessionEvent(
            store_id="intaris",
            session_id="sess_program",
            seq=11,
            type="tool_call",
            data={
                "call_id": "call-child",
                "name": "read",
                "arguments": {"file_path": "report.csv"},
                "turn_id": "turn-program",
                "assistant_phase_index": 2,
                "turn_cycle_index": 1,
                "parent_call_id": "call-program",
                "program_id": "program-7",
                "kernel_generation": 4,
            },
        ),
        RawSessionEvent(
            store_id="intaris",
            session_id="sess_program",
            seq=12,
            type="tool_result",
            data={
                "call_id": "call-child",
                "name": "read",
                "result": "report contents",
                "is_error": False,
                "turn_id": "turn-program",
                "assistant_phase_index": 2,
                "turn_cycle_index": 1,
                "parent_call_id": "call-program",
                "program_id": "program-7",
                "kernel_generation": 4,
            },
        ),
        RawSessionEvent(
            store_id="intaris",
            session_id="sess_program",
            seq=13,
            type="tool_result",
            data={
                "call_id": "call-program",
                "name": "python_execute",
                "result": '{"rows": 1}',
                "is_error": False,
                "turn_id": "turn-program",
                "assistant_phase_index": 2,
                "turn_cycle_index": 1,
                "program_id": "changed-program",
                "kernel_generation": 5,
            },
        ),
    ]

    timeline = project_timeline(normalize_session_events(raw_events).events).timeline
    parent = next(item for item in timeline.items if item.id == "tool:call-program")
    child = next(item for item in timeline.items if item.id == "tool:call-child")

    assert isinstance(parent, ToolCallTimelineItem)
    assert isinstance(child, ToolCallTimelineItem)
    assert parent.parent_call_id is None
    assert parent.program_id == "program-7"
    assert parent.kernel_generation == 4
    assert parent.sort_key.startswith("0000:000000000000010:")
    assert child.parent_call_id == "call-program"
    assert child.program_id == "program-7"
    assert child.kernel_generation == 4
    assert child.sort_key.startswith("0000:000000000000011:")
    assert [parent.id, child.id] == ["tool:call-program", "tool:call-child"]


def test_program_linkage_can_be_hydrated_from_a_result_only_window() -> None:
    raw_result = RawSessionEvent(
        store_id="intaris",
        session_id="sess_program",
        seq=21,
        type="tool_result",
        data={
            "call_id": "call-window-child",
            "name": "read",
            "result": "partial history",
            "is_error": False,
            "parent_call_id": "call-outside-window",
            "program_id": "program-window",
            "kernel_generation": "generation-a",
        },
    )

    items = project_timeline(normalize_session_events([raw_result]).events).timeline.items
    assert len(items) == 1
    item = items[0]
    assert isinstance(item, ToolCallTimelineItem)
    assert item.id == "tool:call-window-child"
    assert item.parent_call_id == "call-outside-window"
    assert item.program_id == "program-window"
    assert item.kernel_generation == "generation-a"
