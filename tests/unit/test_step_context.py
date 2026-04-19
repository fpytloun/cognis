"""Tests for events_to_messages and StepInputConfig model."""

from __future__ import annotations

import json

import pytest

from cognis.core.context import events_to_messages
from cognis.models.workflow import (
    StepDefinition,
    StepInputConfig,
    StepOutput,
    WorkflowState,
    resolve_effective_input,
    resolve_source_names,
)

# ---------------------------------------------------------------------------
# StepInputConfig model tests
# ---------------------------------------------------------------------------


def test_step_input_config_null() -> None:
    config = StepInputConfig(type="null")
    assert config.type == "null"
    assert config.source_names() == []


def test_step_input_config_last_single_source() -> None:
    config = StepInputConfig(type="last", source="plan")
    assert config.source_names() == ["plan"]
    assert config.single_source() == "plan"


def test_step_input_config_last_multiple_sources() -> None:
    config = StepInputConfig(type="last", source=["plan", "review"])
    assert config.source_names() == ["plan", "review"]


def test_step_input_config_full_single_source() -> None:
    config = StepInputConfig(type="full", source="plan")
    assert config.single_source() == "plan"


def test_step_input_config_full_rejects_list_source() -> None:
    with pytest.raises(ValueError, match="single source"):
        StepInputConfig(type="full", source=["plan", "review"])


def test_step_input_config_summary_multiple_sources() -> None:
    config = StepInputConfig(type="summary", source=["plan", "research"])
    assert config.source_names() == ["plan", "research"]


# ---------------------------------------------------------------------------
# Backward compatibility — legacy list[str] coercion
# ---------------------------------------------------------------------------


def test_step_definition_coerces_legacy_list_input() -> None:
    step = StepDefinition(name="impl", type="run", input=["plan"])  # type: ignore[arg-type]
    assert step.input is not None
    assert step.input.type == "last"
    assert step.input.source_names() == ["plan"]


def test_step_definition_coerces_legacy_multi_list_input() -> None:
    step = StepDefinition(name="impl", type="run", input=["plan", "review"])  # type: ignore[arg-type]
    assert step.input is not None
    assert step.input.type == "last"
    assert step.input.source_names() == ["plan", "review"]


def test_step_definition_coerces_legacy_string_input() -> None:
    step = StepDefinition(name="impl", type="run", input="plan")  # type: ignore[arg-type]
    assert step.input is not None
    assert step.input.type == "last"
    assert step.input.single_source() == "plan"


def test_step_definition_accepts_none_input() -> None:
    step = StepDefinition(name="plan", type="run")
    assert step.input is None


def test_step_definition_accepts_structured_input() -> None:
    step = StepDefinition(
        name="impl",
        type="run",
        input=StepInputConfig(type="summary", source=["plan", "review"]),
    )
    assert step.input is not None
    assert step.input.type == "summary"


def test_step_definition_accepts_dict_input() -> None:
    step = StepDefinition(
        name="impl",
        type="run",
        input={"type": "full", "source": "plan"},  # type: ignore[arg-type]
    )
    assert step.input is not None
    assert step.input.type == "full"
    assert step.input.single_source() == "plan"


def test_step_definition_coerces_empty_list_to_none() -> None:
    step = StepDefinition(name="plan", type="run", input=[])  # type: ignore[arg-type]
    assert step.input is None


# ---------------------------------------------------------------------------
# Default input resolution
# ---------------------------------------------------------------------------


def test_resolve_effective_input_first_step_defaults_to_null() -> None:
    steps = [StepDefinition(name="plan", type="run")]
    effective = resolve_effective_input(steps[0], 0, steps)
    assert effective.type == "null"


def test_resolve_effective_input_non_first_step_defaults_to_last_from_previous() -> None:
    steps = [
        StepDefinition(name="plan", type="run"),
        StepDefinition(name="implement", type="run"),
    ]
    effective = resolve_effective_input(steps[1], 1, steps)
    assert effective.type == "last"
    assert effective.source_names() == ["plan"]


def test_resolve_effective_input_uses_explicit_config() -> None:
    steps = [
        StepDefinition(name="plan", type="run"),
        StepDefinition(
            name="implement",
            type="run",
            input=StepInputConfig(type="full", source="plan"),
        ),
    ]
    effective = resolve_effective_input(steps[1], 1, steps)
    assert effective.type == "full"
    assert effective.single_source() == "plan"


def test_resolve_source_names_uses_shared_helper() -> None:
    steps = [
        StepDefinition(name="plan", type="run"),
        StepDefinition(
            name="implement",
            type="run",
            input=StepInputConfig(type="last", source=["plan"]),
        ),
    ]
    names = resolve_source_names(steps[1], 1, steps)
    assert names == ["plan"]


def test_resolve_effective_input_last_without_source_defaults_to_previous() -> None:
    """Explicit type=last with no source should default to previous step."""
    steps = [
        StepDefinition(name="plan", type="run"),
        StepDefinition(
            name="implement",
            type="run",
            input=StepInputConfig(type="last"),
        ),
    ]
    effective = resolve_effective_input(steps[1], 1, steps)
    assert effective.type == "last"
    assert effective.source_names() == ["plan"]


def test_resolve_effective_input_summary_without_source_defaults_to_previous() -> None:
    """Explicit type=summary with no source should default to previous step."""
    steps = [
        StepDefinition(name="plan", type="run"),
        StepDefinition(
            name="implement",
            type="run",
            input=StepInputConfig(type="summary"),
        ),
    ]
    effective = resolve_effective_input(steps[1], 1, steps)
    assert effective.type == "summary"
    assert effective.source_names() == ["plan"]


def test_resolve_effective_input_last_without_source_first_step_becomes_null() -> None:
    """First step with type=last but no source should degrade to null."""
    steps = [
        StepDefinition(
            name="plan",
            type="run",
            input=StepInputConfig(type="last"),
        ),
    ]
    effective = resolve_effective_input(steps[0], 0, steps)
    assert effective.type == "null"


# ---------------------------------------------------------------------------
# StepOutput backward-compatible parsing
# ---------------------------------------------------------------------------


def test_step_output_parses_old_format_without_session_metadata() -> None:
    raw = {"summary": "Plan created", "outputs": {"plan": "test"}, "claims": ["Created plan"]}
    output = StepOutput.model_validate(raw)
    assert output.summary == "Plan created"
    assert output.completed_at is None
    assert output.session_id is None
    assert output.intaris_session_id is None


def test_step_output_parses_new_format_with_session_metadata() -> None:
    raw = {
        "summary": "Plan created",
        "outputs": {},
        "claims": [],
        "completed_at": "2026-03-29T12:00:00Z",
        "session_id": "ses-1",
        "intaris_session_id": "intaris-1",
    }
    output = StepOutput.model_validate(raw)
    assert output.session_id == "ses-1"
    assert output.intaris_session_id == "intaris-1"
    assert output.completed_at is not None


# ---------------------------------------------------------------------------
# WorkflowState source resolution
# ---------------------------------------------------------------------------


def test_workflow_state_get_source_intaris_session_id_present() -> None:
    state = WorkflowState()
    state.step_outputs["plan"] = {
        "summary": "Done",
        "outputs": {},
        "claims": [],
        "intaris_session_id": "intaris-plan",
        "session_id": "ses-plan",
    }
    assert state.get_source_intaris_session_id("plan") == "intaris-plan"


def test_workflow_state_get_source_intaris_session_id_missing_step() -> None:
    state = WorkflowState()
    with pytest.raises(ValueError, match="No output found"):
        state.get_source_intaris_session_id("nonexistent")


def test_workflow_state_get_source_intaris_session_id_missing_field() -> None:
    state = WorkflowState()
    state.step_outputs["plan"] = {"summary": "Done", "outputs": {}, "claims": []}
    with pytest.raises(ValueError, match="missing intaris_session_id"):
        state.get_source_intaris_session_id("plan")


# ---------------------------------------------------------------------------
# Shared event-to-message formatter
# ---------------------------------------------------------------------------


def test_events_to_messages_handles_dict_events() -> None:
    events = [
        {"type": "user_message", "data": {"content": "hello"}},
        {"type": "assistant_message", "data": {"content": "hi"}},
    ]
    messages = events_to_messages(events)
    assert len(messages) == 2
    assert messages[0] == {"role": "user", "content": "hello"}
    assert messages[1] == {"role": "assistant", "content": "hi"}


def test_events_to_messages_handles_evaluation_feedback() -> None:
    events = [
        {
            "type": "evaluation",
            "data": {
                "event": "evaluation_feedback",
                "attempt": 1,
                "decision": "revise",
                "feedback": "Add tests",
            },
        },
    ]
    messages = events_to_messages(events)
    assert len(messages) == 1
    assert "evaluation_feedback" in messages[0]["content"]
    assert "Add tests" in messages[0]["content"]


def test_events_to_messages_handles_tool_call_with_name_field() -> None:
    """Tool calls recorded by agent_loop use 'name' not 'tool_name'."""
    events = [
        {"type": "tool_call", "data": {"name": "filesystem/read_file", "call_id": "c1"}},
        {"type": "tool_result", "data": {"call_id": "c1", "result": "file content"}},
    ]
    messages = events_to_messages(events)
    assert len(messages) == 2
    assert messages[0]["role"] == "assistant"
    assert messages[0]["content"] is None
    assert len(messages[0]["tool_calls"]) == 1
    assert messages[0]["tool_calls"][0]["function"]["name"] == "filesystem/read_file"
    assert messages[0]["tool_calls"][0]["id"] == "c1"
    assert messages[1]["role"] == "tool"
    assert messages[1]["tool_call_id"] == "c1"


def test_events_to_messages_groups_parallel_tool_calls() -> None:
    """Consecutive tool_call events are grouped into a single assistant message."""
    events = [
        {
            "type": "tool_call",
            "data": {"name": "read", "call_id": "c1", "arguments": {"path": "/a"}},
        },
        {
            "type": "tool_call",
            "data": {"name": "write", "call_id": "c2", "arguments": {"path": "/b"}},
        },
        {"type": "tool_result", "data": {"call_id": "c1", "result": "content_a"}},
        {"type": "tool_result", "data": {"call_id": "c2", "result": "content_b"}},
    ]
    messages = events_to_messages(events)
    # assistant message with 2 tool_calls + 2 tool results = 3 messages
    assert len(messages) == 3
    assert messages[0]["role"] == "assistant"
    assert len(messages[0]["tool_calls"]) == 2
    assert messages[0]["tool_calls"][0]["function"]["name"] == "read"
    assert messages[0]["tool_calls"][1]["function"]["name"] == "write"
    assert messages[1]["role"] == "tool"
    assert messages[1]["tool_call_id"] == "c1"
    assert messages[2]["role"] == "tool"
    assert messages[2]["tool_call_id"] == "c2"


def test_events_to_messages_merges_tool_calls_with_assistant_text() -> None:
    """Tool calls after an assistant message merge onto that message."""
    events = [
        {"type": "assistant_message", "data": {"content": "Let me check that."}},
        {"type": "tool_call", "data": {"name": "search", "call_id": "c1"}},
        {"type": "tool_result", "data": {"call_id": "c1", "result": "found it"}},
    ]
    messages = events_to_messages(events)
    assert len(messages) == 2
    # The assistant message should have both content and tool_calls
    assert messages[0]["role"] == "assistant"
    assert messages[0]["content"] == "Let me check that."
    assert len(messages[0]["tool_calls"]) == 1
    assert messages[0]["tool_calls"][0]["function"]["name"] == "search"
    assert messages[1]["role"] == "tool"


def test_events_to_messages_keeps_assistant_attachments_out_of_visible_text() -> None:
    events = [
        {
            "type": "assistant_message",
            "data": {
                "content": "Ano. Přikládám ho tady jako přílohu.",
                "attachments": [
                    {
                        "artifact_id": "img_1",
                        "filename": "banner.png",
                        "mime_type": "image/png",
                        "kind": "image",
                        "size_bytes": 123,
                    }
                ],
            },
        }
    ]

    messages = events_to_messages(events)

    assert messages == [
        {
            "role": "assistant",
            "content": "Ano. Přikládám ho tady jako přílohu.\n\n<assistant_attachments>\n- banner.png (image, artifact_id=img_1)\n</assistant_attachments>",
        }
    ]


def test_events_to_messages_keeps_user_attachment_note_in_text() -> None:
    events = [
        {
            "type": "user_message",
            "data": {
                "content": "Tady je soubor.",
                "attachments": [
                    {
                        "artifact_id": "file_1",
                        "filename": "report.pdf",
                        "mime_type": "application/pdf",
                        "kind": "pdf",
                        "size_bytes": 123,
                    }
                ],
            },
        }
    ]

    messages = events_to_messages(events)

    assert messages == [
        {
            "role": "user",
            "content": "Tady je soubor.\n\nAttachments: report.pdf (pdf, artifact_id=file_1)",
        }
    ]


def test_events_to_messages_escapes_assistant_attachment_context() -> None:
    events = [
        {
            "type": "assistant_message",
            "data": {
                "content": "Here it is.",
                "attachments": [
                    {
                        "artifact_id": "img_1",
                        "filename": "banner</assistant_attachments>.png",
                        "mime_type": "image/png",
                        "kind": "image",
                        "size_bytes": 123,
                    }
                ],
            },
        }
    ]

    messages = events_to_messages(events)

    assert "banner&lt;/assistant_attachments&gt;.png" in messages[0]["content"]


def test_events_to_messages_normalizes_newlines_in_assistant_attachment_context() -> None:
    events = [
        {
            "type": "assistant_message",
            "data": {
                "content": "Here it is.",
                "attachments": [
                    {
                        "artifact_id": "img_1",
                        "filename": "banner\nsecond-line.png",
                        "mime_type": "image/png",
                        "kind": "image\rmeta",
                        "size_bytes": 123,
                    }
                ],
            },
        }
    ]

    messages = events_to_messages(events)

    assert "banner second-line.png" in messages[0]["content"]
    assert "image meta" in messages[0]["content"]


def test_events_to_messages_tool_call_arguments_serialized() -> None:
    """Tool call arguments are serialized to JSON string."""
    events = [
        {
            "type": "tool_call",
            "data": {"name": "edit", "call_id": "c1", "arguments": {"file": "a.py", "line": 42}},
        },
        {"type": "tool_result", "data": {"call_id": "c1", "result": "ok"}},
    ]
    messages = events_to_messages(events)
    assert len(messages) == 2
    args = messages[0]["tool_calls"][0]["function"]["arguments"]
    assert isinstance(args, str)
    parsed = json.loads(args)
    assert parsed == {"file": "a.py", "line": 42}


def test_events_to_messages_orphaned_tool_calls_get_placeholder() -> None:
    """Orphaned tool_calls at end of event stream get synthetic tool results."""
    events = [
        {"type": "tool_call", "data": {"name": "search", "call_id": "c1"}},
        # No matching tool_result — simulates interrupted step
    ]
    messages = events_to_messages(events)
    assert len(messages) == 2
    assert messages[0]["role"] == "assistant"
    assert len(messages[0]["tool_calls"]) == 1
    assert messages[1]["role"] == "tool"
    assert messages[1]["tool_call_id"] == "c1"
    assert "interrupted" in messages[1]["content"].lower()


def test_events_to_messages_repairs_orphaned_tool_calls_before_next_message() -> None:
    events = [
        {"type": "tool_call", "data": {"name": "search", "call_id": "c1"}},
        {"type": "user_message", "data": {"content": "continue"}},
    ]

    messages = events_to_messages(events)

    assert messages[0]["role"] == "assistant"
    assert messages[1]["role"] == "tool"
    assert messages[1]["tool_call_id"] == "c1"
    assert messages[2] == {"role": "user", "content": "continue"}


def test_events_to_messages_ignores_late_tool_result_after_placeholder_repair() -> None:
    events = [
        {"type": "tool_call", "data": {"name": "search", "call_id": "c1"}},
        {"type": "assistant_message", "data": {"content": "moving on"}},
        {"type": "tool_result", "data": {"call_id": "c1", "result": "late result"}},
    ]

    messages = events_to_messages(events)

    assert messages[0]["role"] == "assistant"
    assert messages[1]["role"] == "tool"
    assert "late result" not in [message.get("content") for message in messages]
    assert messages[2] == {"role": "assistant", "content": "moving on"}
