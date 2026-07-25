"""Tests for controller tool argument validation."""

from __future__ import annotations

from cognis.core.tool_arguments import ToolArgumentError, validate_tool_arguments
from cognis.tools.builtin.workflow import (
    ATTACH_ARTIFACT_TOOL,
    REQUEST_CREDENTIAL_TOOL,
    STEP_COMPLETE_TOOL,
    WRITE_DELIVERABLE_TOOL,
)

_STEP_TODO_SCHEMA = {
    "type": "object",
    "properties": {
        "todos": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "content": {"type": "string"},
                    "status": {
                        "type": "string",
                        "enum": ["pending", "in_progress", "completed", "cancelled"],
                    },
                },
                "required": ["content", "status"],
            },
        },
    },
    "required": ["todos"],
}


def test_valid_arguments_return_none() -> None:
    error = validate_tool_arguments(
        "step_todo_write",
        {"todos": [{"content": "test", "status": "pending"}]},
        schema=_STEP_TODO_SCHEMA,
    )
    assert error is None


def test_unparseable_raw_wrapper_is_rejected() -> None:
    error = validate_tool_arguments(
        "step_todo_write",
        {"_raw": '{"todos": [broken'},
        schema=_STEP_TODO_SCHEMA,
    )
    assert isinstance(error, ToolArgumentError)
    assert error.reason == "unparseable_json"
    assert "JSON" in error.message


def test_missing_required_field_is_rejected() -> None:
    error = validate_tool_arguments(
        "step_todo_write",
        {},
        schema=_STEP_TODO_SCHEMA,
    )
    assert isinstance(error, ToolArgumentError)
    assert error.reason == "schema_violation"
    assert any("todos" in line for line in error.errors)


def test_wrong_item_shape_is_rejected() -> None:
    error = validate_tool_arguments(
        "step_todo_write",
        {"todos": [{"content": "missing status"}]},
        schema=_STEP_TODO_SCHEMA,
    )
    assert isinstance(error, ToolArgumentError)
    assert error.reason == "schema_violation"


def test_non_object_arguments_are_rejected() -> None:
    error = validate_tool_arguments(
        "step_todo_write",
        ["not", "an", "object"],
        schema=_STEP_TODO_SCHEMA,
    )
    assert isinstance(error, ToolArgumentError)
    assert error.reason == "not_object"


def test_as_tool_result_produces_structured_payload() -> None:
    error = validate_tool_arguments(
        "step_todo_write",
        {"todos": [{"content": "x"}]},
        schema=_STEP_TODO_SCHEMA,
    )
    assert error is not None
    payload = error.as_tool_result()
    assert payload["error"] == "invalid_tool_arguments"
    assert payload["tool"] == "step_todo_write"
    assert payload["reason"] == "schema_violation"
    assert isinstance(payload["errors"], list)


def test_schema_none_accepts_any_dict() -> None:
    assert validate_tool_arguments("something", {"anything": 1}, schema=None) is None


def test_attach_artifact_requires_only_a_content_reference() -> None:
    assert (
        validate_tool_arguments(
            "attach_artifact",
            {"content_ref": "art_123"},
            schema=ATTACH_ARTIFACT_TOOL.parameters,
        )
        is None
    )
    error = validate_tool_arguments(
        "attach_artifact",
        {"content_ref": "art_123", "caption": "not supported"},
        schema=ATTACH_ARTIFACT_TOOL.parameters,
    )
    assert isinstance(error, ToolArgumentError)
    assert error.reason == "schema_violation"
    assert "`doc_…`" in ATTACH_ARTIFACT_TOOL.description


def test_step_complete_summary_schema_rejects_blank_strings() -> None:
    error = validate_tool_arguments(
        "step_complete",
        {"summary": "   "},
        schema=STEP_COMPLETE_TOOL.parameters,
    )
    assert isinstance(error, ToolArgumentError)
    assert error.reason == "schema_violation"
    assert any("summary" in line for line in error.errors)


def test_write_deliverable_content_schema_rejects_non_string() -> None:
    error = validate_tool_arguments(
        "write_deliverable",
        {"content": ["plain text", False]},
        schema=WRITE_DELIVERABLE_TOOL.parameters,
    )

    assert isinstance(error, ToolArgumentError)
    assert error.reason == "schema_violation"
    assert any("content" in line and "string" in line for line in error.errors)


def test_generic_write_deliverable_missing_content_reports_only_generic_branch() -> None:
    """A generic write_deliverable call missing `content` must not also report
    the unrelated `rich:pulse` branch's requirements (regression test for the
    doubled oneOf error message bug)."""

    error = validate_tool_arguments(
        "write_deliverable",
        {"action": "write_deliverable"},
        schema=WRITE_DELIVERABLE_TOOL.parameters,
    )
    assert isinstance(error, ToolArgumentError)
    assert error.reason == "schema_violation"
    assert any("content" in line for line in error.errors)
    # Must not leak the pulse branch's distinct required fields into the
    # generic branch's error report.
    joined = " ".join(error.errors)
    assert "rich" not in joined.split(":")[0]  # no top-level "rich" path error


def test_generic_write_deliverable_complete_payload_is_valid() -> None:
    error = validate_tool_arguments(
        "write_deliverable",
        {
            "action": "write_deliverable",
            "content": "fallback text",
            "format": "rich",
            "rich": {"blocks": [{"type": "callout", "content": "hi"}]},
        },
        schema=WRITE_DELIVERABLE_TOOL.parameters,
    )
    assert error is None


def test_generic_write_deliverable_preserves_compatible_source_shapes() -> None:
    error = validate_tool_arguments(
        "write_deliverable",
        {
            "action": "write_deliverable",
            "content": "fallback text",
            "format": "rich",
            "rich": {
                "blocks": [
                    {
                        "type": "day_agenda",
                        "items": [],
                        "source": {
                            "title": "Calendar",
                            "url": "https://calendar.example.test",
                        },
                    },
                    {"type": "source_list", "sources": "calendar"},
                ]
            },
        },
        schema=WRITE_DELIVERABLE_TOOL.parameters,
    )

    assert error is None


def test_generic_write_deliverable_rejects_empty_markdown_block() -> None:
    error = validate_tool_arguments(
        "write_deliverable",
        {
            "action": "write_deliverable",
            "content": "fallback text",
            "format": "rich",
            "rich": {"blocks": [{"type": "markdown"}]},
        },
        schema=WRITE_DELIVERABLE_TOOL.parameters,
    )

    assert isinstance(error, ToolArgumentError)
    assert error.reason == "schema_violation"
    assert any("content" in line for line in error.errors)


def test_generic_write_deliverable_rejects_empty_mermaid_block() -> None:
    error = validate_tool_arguments(
        "write_deliverable",
        {
            "action": "write_deliverable",
            "content": "fallback text",
            "format": "rich",
            "rich": {"blocks": [{"type": "mermaid", "title": "Empty"}]},
        },
        schema=WRITE_DELIVERABLE_TOOL.parameters,
    )

    assert isinstance(error, ToolArgumentError)
    assert error.reason == "schema_violation"
    assert any("source" in line or "code" in line or "content" in line for line in error.errors)


def test_pulse_write_deliverable_rejects_empty_markdown_block() -> None:
    error = validate_tool_arguments(
        "write_deliverable",
        {
            "action": "rich:pulse",
            "content": "fallback text",
            "format": "rich",
            "rich": {
                "blocks": [
                    {"type": "markdown"},
                    *[{"type": "card", "content": "Body"} for _ in range(6)],
                ],
                "metadata": {"presentation": "pulse", "pulse_version": 2},
            },
        },
        schema=WRITE_DELIVERABLE_TOOL.parameters,
    )

    assert isinstance(error, ToolArgumentError)
    assert error.reason == "schema_violation"
    assert any("content" in line for line in error.errors)


def test_pulse_write_deliverable_accepts_every_mermaid_source_alias() -> None:
    for field in ("source", "code", "content"):
        error = validate_tool_arguments(
            "write_deliverable",
            {
                "action": "rich:pulse",
                "content": "fallback text",
                "format": "rich",
                "rich": {
                    "blocks": [
                        {"type": "mermaid", field: "flowchart LR; A-->B"},
                        *[{"type": "card", "content": "Body"} for _ in range(6)],
                    ],
                    "metadata": {"presentation": "pulse", "pulse_version": 2},
                },
            },
            schema=WRITE_DELIVERABLE_TOOL.parameters,
        )

        assert error is None, field


def test_pulse_write_deliverable_rejects_whitespace_mermaid_alias() -> None:
    error = validate_tool_arguments(
        "write_deliverable",
        {
            "action": "rich:pulse",
            "content": "fallback text",
            "format": "rich",
            "rich": {
                "blocks": [
                    {"type": "mermaid", "code": "   "},
                    *[{"type": "card", "content": "Body"} for _ in range(6)],
                ],
                "metadata": {"presentation": "pulse", "pulse_version": 2},
            },
        },
        schema=WRITE_DELIVERABLE_TOOL.parameters,
    )

    assert isinstance(error, ToolArgumentError)
    assert error.reason == "schema_violation"
    assert any("code" in line for line in error.errors)


def test_pulse_branch_is_narrowed_and_does_not_report_generic_only_errors() -> None:
    error = validate_tool_arguments(
        "write_deliverable",
        {"action": "rich:pulse"},
        schema=WRITE_DELIVERABLE_TOOL.parameters,
    )
    assert isinstance(error, ToolArgumentError)
    assert error.reason == "schema_violation"
    # Narrowed to the pulse branch: errors should reference pulse-required
    # fields, not the generic branch's unrelated `content` requirement being
    # duplicated alongside pulse-specific ones in a confusing way.
    assert error.errors


def test_unknown_action_falls_back_to_full_oneof_validation() -> None:
    error = validate_tool_arguments(
        "write_deliverable",
        {"action": "not_a_real_action", "content": "x"},
        schema=WRITE_DELIVERABLE_TOOL.parameters,
    )
    assert isinstance(error, ToolArgumentError)
    assert error.reason == "schema_violation"


def test_request_credential_schema_rejects_invalid_kind() -> None:
    error = validate_tool_arguments(
        "request_credential",
        {
            "credential_id": "site_login",
            "kind": "login",
            "label": "Site login",
        },
        schema=REQUEST_CREDENTIAL_TOOL.parameters,
    )
    assert isinstance(error, ToolArgumentError)
    assert error.reason == "schema_violation"
    assert any("kind" in line and "username_password" in line for line in error.errors)
