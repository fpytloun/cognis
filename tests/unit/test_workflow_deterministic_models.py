from __future__ import annotations

import pytest
from pydantic import ValidationError

from cognis.core.management import validate_workflow_definition
from cognis.models.workflow import StepDefinition, Workflow
from cognis.tools.builtin.orchestration import CREATE_WORKFLOW_TOOL, UPDATE_WORKFLOW_TOOL


def _workflow(steps: list[dict[str, object]]) -> dict[str, object]:
    return {"workflow_id": "wf_test", "name": "Test", "steps": steps}


def test_deterministic_steps_parse_and_serialize_with_external_aliases() -> None:
    definition = _workflow(
        [
            {
                "name": "fetch",
                "type": "tool_call",
                "when": "{{ vars.enabled }}",
                "tool_call": {"tool": "web_fetch", "args": {"url": "{{ vars.url }}"}},
            },
            {
                "name": "branch",
                "type": "condition",
                "condition": {
                    "if": "{{ steps.fetch.outputs.count > 0 }}",
                    "then": "respond",
                    "else": "done",
                },
            },
            {"name": "respond", "type": "run", "prompt": "Respond"},
            {
                "name": "done",
                "type": "complete",
                "complete": {
                    "summary": "Nothing to do",
                    "delivery_mode_override": "silent",
                },
            },
        ]
    )

    normalized = validate_workflow_definition(definition)

    assert normalized["steps"][1]["condition"]["if"] == "{{ steps.fetch.outputs.count > 0 }}"
    assert normalized["steps"][1]["condition"]["else"] == "done"
    assert "if_" not in normalized["steps"][1]["condition"]
    reloaded = Workflow.model_validate(normalized)
    assert reloaded.steps[0].tool_call is not None
    assert reloaded.steps[1].condition is not None


def test_condition_loop_budget_round_trips() -> None:
    normalized = validate_workflow_definition(
        _workflow(
            [
                {"name": "implement", "type": "run"},
                {
                    "name": "route",
                    "type": "condition",
                    "condition": {
                        "if": "true",
                        "then": "implement",
                        "max_loop_iterations": 5,
                        "on_exhausted": "gate",
                    },
                },
            ]
        )
    )

    assert normalized["steps"][1]["condition"]["max_loop_iterations"] == 5
    assert normalized["steps"][1]["condition"]["on_exhausted"] == "gate"


def test_revision_source_must_reference_an_earlier_step() -> None:
    with pytest.raises(ValueError, match="revision_source must reference an earlier step"):
        validate_workflow_definition(
            _workflow(
                [
                    {
                        "name": "route",
                        "type": "condition",
                        "condition": {
                            "if": "true",
                            "then": "done",
                            "revision_source": "done",
                        },
                    },
                    {"name": "done", "type": "run"},
                ]
            )
        )


@pytest.mark.parametrize(
    "step",
    [
        {"name": "fetch", "type": "tool_call"},
        {
            "name": "fetch",
            "type": "tool_call",
            "tool_call": {"tool": "read"},
            "condition": {"if": "true"},
        },
        {
            "name": "branch",
            "type": "condition",
            "condition": {"if": "true"},
            "input": {"type": "last", "source": "fetch"},
        },
        {
            "name": "done",
            "type": "complete",
            "complete": {"summary": "Done"},
            "next": "other",
        },
        {
            "name": "fetch",
            "type": "tool_call",
            "tool_call": {"tool": "read", "unknown": True},
        },
    ],
)
def test_invalid_mixed_or_missing_deterministic_configs_fail(step: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        StepDefinition.model_validate(step)


def test_unknown_target_and_self_jump_fail() -> None:
    with pytest.raises(ValueError, match="unknown deterministic target"):
        validate_workflow_definition(
            _workflow(
                [
                    {
                        "name": "branch",
                        "type": "condition",
                        "condition": {"if": "true", "then": "missing"},
                    }
                ]
            )
        )
    with pytest.raises(ValueError, match="cannot jump to itself"):
        validate_workflow_definition(
            _workflow(
                [
                    {
                        "name": "branch",
                        "type": "condition",
                        "condition": {"if": "true", "then": "branch"},
                    }
                ]
            )
        )


def test_deterministic_backward_cycle_is_runtime_bounded() -> None:
    normalized = validate_workflow_definition(
        _workflow(
            [
                {
                    "name": "first",
                    "type": "condition",
                    "condition": {"if": "true", "then": "second"},
                },
                {
                    "name": "second",
                    "type": "condition",
                    "condition": {"if": "true", "then": "first"},
                },
            ]
        )
    )
    assert normalized["steps"][1]["condition"]["then"] == "first"


def test_backward_jump_with_implicit_fallthrough_is_valid() -> None:
    normalized = validate_workflow_definition(
        _workflow(
            [
                {
                    "name": "first",
                    "type": "condition",
                    "condition": {"if": "true"},
                },
                {
                    "name": "second",
                    "type": "tool_call",
                    "tool_call": {"tool": "read"},
                    "next": "first",
                },
            ]
        )
    )
    assert normalized["steps"][1]["next"] == "first"


def test_backward_jump_through_run_step_is_valid() -> None:
    normalized = validate_workflow_definition(
        _workflow(
            [
                {"name": "work", "type": "run", "prompt": "Work"},
                {
                    "name": "again",
                    "type": "tool_call",
                    "tool_call": {"tool": "read"},
                    "next": "work",
                },
            ]
        )
    )
    assert normalized["steps"][1]["next"] == "work"


@pytest.mark.parametrize(
    "first",
    [
        {
            "name": "first",
            "type": "condition",
            "when": "{{ vars.enabled }}",
            "condition": {"if": "true", "then": "done", "else": "done"},
        },
        {
            "name": "first",
            "type": "complete",
            "when": "{{ vars.enabled }}",
            "complete": {"summary": "Done"},
        },
    ],
)
def test_when_skip_fallthrough_may_participate_in_runtime_bounded_cycle(
    first: dict[str, object],
) -> None:
    normalized = validate_workflow_definition(
        _workflow(
            [
                first,
                {
                    "name": "again",
                    "type": "tool_call",
                    "tool_call": {"tool": "read"},
                    "next": "first",
                },
                {
                    "name": "done",
                    "type": "complete",
                    "complete": {"summary": "Done"},
                },
            ]
        )
    )
    assert normalized["steps"][1]["next"] == "first"


def test_legacy_run_workflow_serialization_omits_new_fields() -> None:
    raw = _workflow([{"name": "run", "type": "run", "prompt": "Work"}])
    workflow = Workflow.model_validate(raw)

    dumped = workflow.model_dump(mode="json", exclude_none=True)

    step = dumped["steps"][0]
    assert step["name"] == "run"
    assert not {"when", "on_skip", "on_error", "next", "tool_call", "condition", "complete"} & set(
        step
    )


def test_workflow_authoring_tools_expose_deterministic_step_schema() -> None:
    for tool in (CREATE_WORKFLOW_TOOL, UPDATE_WORKFLOW_TOOL):
        item_schema = tool.parameters["properties"]["steps"]["items"]
        assert item_schema["properties"]["type"]["enum"] == [
            "run",
            "gate",
            "tool_call",
            "condition",
            "complete",
        ]
        assert {"when", "on_skip", "on_error", "next"} <= set(item_schema["properties"])
        assert {"objective", "responsibilities", "defer_to"} <= set(item_schema["properties"])
        condition_schema = item_schema["properties"]["condition"]
        assert condition_schema["required"] == ["if"]
        assert {
            "revision_source",
            "max_loop_iterations",
            "on_exhausted",
        } <= set(condition_schema["properties"])
