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
    DeliverableResponse,
    EffectiveToolItemResponse,
    ModelRoutingEntry,
    ModelRoutingResponse,
    PendingPauseResponse,
    SkillResponse,
    SkillVersionResponse,
    StepProfileResponse,
    StepRunResponse,
    TaskResponse,
    ToolResponse,
    WorkflowResponse,
)
from cognis.api.serializers import llm_provider_to_response, step_run_to_response
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


def test_deliverable_response_round_trip() -> None:
    response = DeliverableResponse(
        deliverable_id="dlv-1",
        step_run_id="sr-1",
        version=2,
        content="# Result",
        format="markdown",
        title="Implementation summary",
        target="channel",
        outputs={"tests": "passed"},
        status="approved",
    )

    assert response.outputs == {"tests": "passed"}
    assert response.status == "approved"


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


class TestSkillResponseContracts:
    def test_skill_version_accepts_asset_manifest_entries(self) -> None:
        response = SkillVersionResponse(
            version_id="sv-1",
            skill_id="skill-1",
            version_number=1,
            content_hash="a" * 64,
            instructions="hello",
            asset_manifest=[
                {
                    "filename": "scripts/tool.py",
                    "asset_id": "sa-1",
                    "artifact_namespace": "skills",
                    "artifact_object_id": "ska-1",
                    "content_hash": "b" * 64,
                    "size_bytes": 10,
                    "content_type": "text/x-python",
                    "url": "https://example.test/tool.py",
                }
            ],
        )
        assert response.asset_manifest is not None
        assert response.asset_manifest[0].artifact_object_id == "ska-1"

    def test_skill_response_round_trips_current_version_shape(self) -> None:
        response = SkillResponse(
            skill_id="skill-1",
            name="Skill One",
            instructions="hello",
            attach_to_all_agents=False,
            current_version=SkillVersionResponse(
                version_id="sv-1",
                skill_id="skill-1",
                version_number=1,
                content_hash="a" * 64,
                instructions="hello",
            ),
        )
        assert response.current_version is not None
        assert response.current_version.version_id == "sv-1"

    def test_skill_version_round_trips_decomposition_fields(self) -> None:
        response = SkillVersionResponse(
            version_id="sv-1",
            skill_id="skill-1",
            version_number=2,
            content_hash="a" * 64,
            instructions="hello",
            steps=[{"name": "plan", "type": "run", "prompt": "Plan it"}],
            decomposition_source_hash="b" * 64,
            decomposition_stale=True,
        )

        assert response.steps is not None
        assert response.steps[0]["name"] == "plan"
        assert response.decomposition_stale is True

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


def test_workflow_response_round_trips_lifecycle_and_lineage() -> None:
    response = WorkflowResponse(
        workflow_id="wf-1",
        name="Workflow",
        lifecycle="ephemeral",
        lineage={"base_workflow_id": "system:software-development"},
    )

    assert response.lifecycle == "ephemeral"
    assert response.lineage == {"base_workflow_id": "system:software-development"}


def test_workflow_response_preserves_step_profile_shape() -> None:
    response = WorkflowResponse(
        workflow_id="wf-1",
        name="Workflow",
        steps=[
            {
                "name": "execute",
                "type": "run",
                "step_profile_id": "system:coding",
                "step_profile_mode": "hard",
                "step_profile": {
                    "matrix": {"filesystem": ["read", "write"]},
                    "tool_overrides": {"include": ["read"], "exclude": ["bash"]},
                    "allow_tool_search": False,
                },
            }
        ],
    )

    assert response.steps[0]["step_profile_id"] == "system:coding"
    assert response.steps[0]["step_profile_mode"] == "hard"
    assert response.steps[0]["step_profile"]["matrix"]["filesystem"] == ["read", "write"]


def test_tool_response_round_trips_classification_fields() -> None:
    response = ToolResponse(
        name="read",
        description="Read a file",
        category="filesystem",
        read_only=True,
        capabilities=["read"],
        classification_source="declared",
        classification_confidence=1.0,
    )

    assert response.capabilities == ["read"]
    assert response.classification_source == "declared"
    assert response.classification_confidence == 1.0


def test_effective_tool_item_round_trips_classification_fields() -> None:
    response = EffectiveToolItemResponse(
        tool_id="builtin:read",
        name="read",
        description="Read a file",
        category="filesystem",
        read_only=True,
        capabilities=["read"],
        classification_source="llm",
        classification_confidence=0.83,
        permission="allow",
    )

    assert response.capabilities == ["read"]
    assert response.classification_source == "llm"
    assert response.classification_confidence == pytest.approx(0.83)


def test_step_profile_response_round_trips_matrix_shape() -> None:
    response = StepProfileResponse(
        profile_id="system:coding",
        name="Coding",
        mode="soft",
        config={
            "matrix": {"filesystem": ["read", "write"], "shell": ["write", "privileged"]},
            "allow_tool_search": True,
        },
    )

    assert response.profile_id == "system:coding"
    assert response.config["matrix"]["filesystem"] == ["read", "write"]


class TestModelRoutingContracts:
    def test_model_routing_defaults_to_empty_route_entries(self) -> None:
        response = ModelRoutingResponse()

        assert response.default == ModelRoutingEntry(model=None, reasoning_effort=None)
        assert response.image_generation == ModelRoutingEntry(model=None, reasoning_effort=None)

    def test_model_routing_preserves_nested_entry_shape(self) -> None:
        response = ModelRoutingResponse(
            default={"model": "gpt-5.4", "reasoning_effort": "xhigh"},
            speech_to_text={"model": "gpt-4o-transcribe", "reasoning_effort": None},
        )

        assert response.default.model == "gpt-5.4"
        assert response.default.reasoning_effort == "xhigh"
        assert response.speech_to_text.model == "gpt-4o-transcribe"


class TestLLMProviderSerializer:
    """Provider list enriches stored models with derived capability fields."""

    def _provider_row(self, **config_overrides: object) -> _FakeRow:
        config = {
            "preset": "openai",
            "default_model": "gpt-5.4",
            "models": [
                {
                    "model_id": "gpt-5.4",
                    "supports_reasoning": True,
                },
                {
                    "model_id": "gpt-4o-mini",
                    "supports_reasoning": False,
                },
            ],
            **config_overrides,
        }
        return _FakeRow(
            provider_id="openai",
            display_name="OpenAI",
            location="controller",
            backend="litellm",
            config=config,
            is_default=True,
            status="active",
            created_at=None,
            updated_at=None,
            last_test=None,
        )

    def test_reasoning_model_gets_reasoning_efforts_populated(self) -> None:
        response = llm_provider_to_response(self._provider_row())

        reasoning_model = next(m for m in response.models if m["model_id"] == "gpt-5.4")
        assert reasoning_model["reasoning_efforts"] == [
            "default",
            "none",
            "low",
            "medium",
            "high",
            "xhigh",
        ]

    def test_non_reasoning_model_keeps_empty_reasoning_efforts(self) -> None:
        response = llm_provider_to_response(self._provider_row())

        standard_model = next(m for m in response.models if m["model_id"] == "gpt-4o-mini")
        assert standard_model.get("reasoning_efforts", []) == []

    def test_explicitly_configured_reasoning_efforts_are_preserved(self) -> None:
        response = llm_provider_to_response(
            self._provider_row(
                models=[
                    {
                        "model_id": "gpt-5.4",
                        "supports_reasoning": True,
                        "reasoning_efforts": ["default", "low", "high"],
                    }
                ]
            )
        )

        reasoning_model = next(m for m in response.models if m["model_id"] == "gpt-5.4")
        assert reasoning_model["reasoning_efforts"] == ["default", "low", "high"]
