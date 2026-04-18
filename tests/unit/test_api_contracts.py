"""Contract tests for API response models.

Stage 20+ refactors occasionally drifted the shape between the DB
producers and the API response models (e.g. ``StepRunResponse.todos``
was typed ``dict`` while every producer wrote ``list[dict]``).

These tests pin the canonical shapes so future changes either update the
tests or immediately fail in CI.
"""

from __future__ import annotations

import types

import pytest
from pydantic import ValidationError

from cognis.api.models import (
    PendingPauseResponse,
    StepRunResponse,
    TaskResponse,
)
from cognis.api.serializers import step_run_to_response
from cognis.core.management import _normalize_pause_context, _normalize_pause_options


class _FakeRow(types.SimpleNamespace):
    """Flexible stand-in for a SQLAlchemy ORM row in serializer tests."""


def _step_run_row(**overrides: object) -> _FakeRow:
    defaults = {
        "step_run_id": "sr-1",
        "task_id": "task-1",
        "step_name": "execute",
        "step_type": "run",
        "status": "completed",
        "attempt": 1,
        "agent_id": "agent-1",
        "workspace_root": None,
        "working_directory": None,
        "conversation_id": None,
        "session_id": None,
        "intaris_session_id": None,
        "output": None,
        "evaluation": None,
        "todos": None,
        "started_at": None,
        "completed_at": None,
        "updated_at": None,
    }
    defaults.update(overrides)
    return _FakeRow(**defaults)


class TestStepRunTodosContract:
    """StepRunResponse.todos must always be list[dict]."""

    def test_empty_list_round_trip(self) -> None:
        response = step_run_to_response(_step_run_row(todos=[]))
        assert response.todos == []

    def test_populated_list_round_trip(self) -> None:
        payload = [
            {"content": "Ship release", "status": "in_progress"},
            {"content": "Write changelog", "status": "pending"},
        ]
        response = step_run_to_response(_step_run_row(todos=payload))
        assert response.todos == payload

    def test_none_is_normalized_to_empty_list(self) -> None:
        response = step_run_to_response(_step_run_row(todos=None))
        assert response.todos == []

    def test_unexpected_dict_is_coerced_to_empty_list(self) -> None:
        # Historical shape drift — StepRunResponse must not raise when a
        # legacy row stored a dict under ``todos``.
        response = step_run_to_response(_step_run_row(todos={"stray": "value"}))
        assert response.todos == []

    def test_non_dict_items_are_filtered(self) -> None:
        response = step_run_to_response(_step_run_row(todos=[{"ok": True}, "garbage"]))
        assert response.todos == [{"ok": True}]

    def test_response_rejects_non_list_when_constructed_directly(self) -> None:
        with pytest.raises(ValidationError):
            StepRunResponse(
                step_run_id="sr",
                task_id="task",
                step_name="s",
                step_type="run",
                status="running",
                agent_id="agent",
                todos={"wrong": "shape"},  # type: ignore[arg-type]
            )


class TestPendingPauseShapeContract:
    """PendingPauseResponse must accept canonical shapes and
    normalization helpers must cover legacy shapes."""

    def test_options_as_list_of_dicts(self) -> None:
        response = PendingPauseResponse(
            pause_id="p-1",
            pause_type="step_input",
            options=[{"label": "Approve", "action": "approve"}],
        )
        assert response.options == [{"label": "Approve", "action": "approve"}]

    def test_options_none_is_allowed(self) -> None:
        response = PendingPauseResponse(pause_id="p-1", pause_type="step_input")
        assert response.options is None

    def test_normalize_options_from_list_of_strings(self) -> None:
        normalized = _normalize_pause_options(["Yes", "No"])
        assert normalized == [
            {"label": "Yes", "action": "Yes"},
            {"label": "No", "action": "No"},
        ]

    def test_normalize_options_from_mixed_shape_drops_junk(self) -> None:
        normalized = _normalize_pause_options([{"label": "A"}, 42, "B"])
        assert normalized == [{"label": "A"}, {"label": "B", "action": "B"}]

    def test_normalize_options_returns_none_for_empty_or_invalid(self) -> None:
        assert _normalize_pause_options(None) is None
        assert _normalize_pause_options("not a list") is None
        assert _normalize_pause_options([]) is None

    def test_normalize_context_accepts_dict(self) -> None:
        assert _normalize_pause_context({"key": "value"}) == {"key": "value"}

    def test_normalize_context_wraps_string(self) -> None:
        assert _normalize_pause_context("background") == {"note": "background"}

    def test_normalize_context_none_and_empty_return_none(self) -> None:
        assert _normalize_pause_context(None) is None
        assert _normalize_pause_context("") is None


class TestTaskResponseRoundTrip:
    """TaskResponse round trip preserves delivery default and status."""

    def test_defaults(self) -> None:
        response = TaskResponse(
            task_id="task-1",
            title="Task",
            status="running",
            created_by="user@example.com",
            agent_id="agent-1",
            source_type="api",
        )
        assert response.description == ""
        assert response.workflow_state is None
        assert response.completion_mode_family == "default"
