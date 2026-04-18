"""Tests for controller tool argument validation."""

from __future__ import annotations

from cognis.core.tool_arguments import ToolArgumentError, validate_tool_arguments


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
