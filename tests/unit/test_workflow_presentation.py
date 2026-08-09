"""Compatibility and validation coverage for workflow presentation snapshots."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from cognis.api.models import (
    PublicWorkflowState,
    TaskResponse,
    WorkflowRequest,
    WorkflowRunResponse,
)
from cognis.api.serializers import workflow_to_response
from cognis.core.management import validate_workflow_definition
from cognis.core.task_queue import TaskQueue
from cognis.core.workflow_registry import GENERAL_TASK_WORKFLOW
from cognis.models.task import TaskModel
from cognis.models.workflow import (
    StepDefinition,
    Workflow,
    WorkflowState,
    canonical_workflow_digest,
    pin_effective_workflow,
)


def _workflow(*, presentation: dict[str, object] | None = None) -> Workflow:
    return Workflow.model_validate(
        {
            "workflow_id": "user:phased",
            "name": "Phased",
            "steps": [
                {"name": "collect", "type": "run"},
                {"name": "review", "type": "gate"},
                {"name": "finish", "type": "run"},
            ],
            **({"presentation": presentation} if presentation is not None else {}),
        }
    )


def test_legacy_workflow_round_trip_omits_presentation() -> None:
    raw = {
        "workflow_id": "user:legacy",
        "name": "Legacy",
        "steps": [{"name": "execute", "type": "run"}],
    }

    dumped = Workflow.model_validate(raw).model_dump(mode="json", exclude_none=True)

    assert "presentation" not in dumped
    assert Workflow.model_validate(dumped).steps == [StepDefinition(name="execute", type="run")]
    assert "presentation" not in validate_workflow_definition(raw)
    assert "presentation" not in workflow_to_response(Workflow.model_validate(raw)).model_dump(
        mode="json"
    )


def test_phased_workflow_round_trips_through_public_contract() -> None:
    presentation = {
        "phases": [
            {
                "id": "all",
                "title": "All steps",
                "step_names": ["collect", "review", "finish"],
            }
        ]
    }
    request = WorkflowRequest(
        name="Phased",
        steps=[
            {"name": "collect", "type": "run"},
            {"name": "review", "type": "gate"},
            {"name": "finish", "type": "run"},
        ],
        presentation=presentation,
    )
    workflow = _workflow(presentation=presentation)

    assert request.model_dump(mode="json")["presentation"] == presentation
    response_presentation = workflow_to_response(workflow).model_dump(mode="json")["presentation"]
    assert response_presentation["phases"][0] == {
        **presentation["phases"][0],
        "description": "",
    }


def test_general_task_compatibility_golden() -> None:
    workflow = GENERAL_TASK_WORKFLOW

    assert [step.type for step in workflow.steps] == ["run"]
    assert workflow.steps[0].name == "execute"
    assert workflow.steps[0].completion is not None
    assert workflow.steps[0].completion.evaluate is False
    assert workflow.steps[0].metadata_contract is not None
    assert workflow.presentation is not None
    assert workflow.presentation.phases[0].step_names == ["execute"]


@pytest.mark.parametrize(
    ("presentation", "message"),
    [
        ({"phases": []}, "at least one phase"),
        (
            {"phases": [{"id": "", "title": "Collect", "step_names": ["collect"]}]},
            "must not be empty",
        ),
        (
            {
                "phases": [
                    {"id": "same", "title": "Collect", "step_names": ["collect"]},
                    {"id": "same", "title": "Rest", "step_names": ["review", "finish"]},
                ]
            },
            "phase ids must be unique",
        ),
        (
            {"phases": [{"id": "all", "title": "", "step_names": ["collect"]}]},
            "must not be empty",
        ),
        (
            {"phases": [{"id": "empty", "title": "Empty", "step_names": []}]},
            "at least one step",
        ),
        (
            {
                "phases": [
                    {
                        "id": "all",
                        "title": "All",
                        "step_names": ["collect", "unknown", "review", "finish"],
                    }
                ]
            },
            "unknown steps",
        ),
        (
            {
                "phases": [
                    {
                        "id": "all",
                        "title": "All",
                        "step_names": ["collect", "review", "review", "finish"],
                    }
                ]
            },
            "duplicates",
        ),
        (
            {
                "phases": [
                    {"id": "first", "title": "First", "step_names": ["collect"]},
                    {"id": "last", "title": "Last", "step_names": ["finish"]},
                ]
            },
            "missing workflow steps",
        ),
        (
            {
                "phases": [
                    {
                        "id": "mixed",
                        "title": "Mixed",
                        "step_names": ["collect", "finish"],
                    },
                    {"id": "middle", "title": "Middle", "step_names": ["review"]},
                ]
            },
            "canonical workflow step order",
        ),
        (
            {
                "phases": [
                    {"id": "later", "title": "Later", "step_names": ["review", "finish"]},
                    {"id": "first", "title": "First", "step_names": ["collect"]},
                ]
            },
            "canonical workflow step order",
        ),
    ],
)
def test_invalid_phase_metadata_is_rejected(presentation: dict[str, object], message: str) -> None:
    with pytest.raises(ValidationError, match=message):
        _workflow(presentation=presentation)


def test_valid_phases_are_complete_contiguous_and_ordered() -> None:
    workflow = _workflow(
        presentation={
            "phases": [
                {"id": "prepare", "title": "Prepare", "step_names": ["collect"]},
                {
                    "id": "conclude",
                    "title": "Conclude",
                    "step_names": ["review", "finish"],
                },
            ]
        }
    )

    assert workflow.presentation is not None
    assert [phase.id for phase in workflow.presentation.phases] == ["prepare", "conclude"]


def test_digest_is_canonical_and_public_state_redacts_definition() -> None:
    workflow = _workflow()
    definition = workflow.model_dump(mode="json", exclude_none=True)
    reordered = dict(reversed(list(definition.items())))
    digest = canonical_workflow_digest(definition)
    state = PublicWorkflowState(
        effective_workflow_version=workflow.version,
        effective_workflow_digest=digest,
        effective_workflow_definition=definition,
    )

    assert canonical_workflow_digest(reordered) == digest
    dumped = state.model_dump(mode="json")
    assert dumped["effective_workflow_version"] == workflow.version
    assert dumped["effective_workflow_digest"] == digest
    assert "effective_workflow_definition" not in dumped


def test_public_task_and_workflow_run_responses_never_expose_definition() -> None:
    definition = _workflow().model_dump(mode="json", exclude_none=True)
    state = WorkflowState(
        effective_workflow_version=1,
        effective_workflow_digest=canonical_workflow_digest(definition),
        effective_workflow_definition=definition,
    )
    task_response = TaskResponse(
        task_id="task-public",
        title="Public",
        status="running",
        created_by="owner@example.com",
        agent_id="agent",
        source_type="user",
        workflow_state=state,
    )
    run_response = WorkflowRunResponse(task_id="task-public", workflow_state=state)

    assert (
        "effective_workflow_definition"
        not in task_response.model_dump(mode="json")["workflow_state"]
    )
    assert (
        "effective_workflow_definition"
        not in run_response.model_dump(mode="json")["workflow_state"]
    )


def test_pinned_definition_wins_after_source_workflow_edit() -> None:
    original = _workflow()
    state = WorkflowState(
        effective_workflow_version=original.version,
        effective_workflow_digest=canonical_workflow_digest(original),
        effective_workflow_definition=original.model_dump(mode="json", exclude_none=True),
    )
    task = TaskModel(
        task_id="task-pinned",
        title="Pinned",
        created_by="owner@example.com",
        agent_id="agent",
        workflow_id=original.workflow_id,
        workflow_state=state,
    )
    edited = original.model_copy(update={"steps": [StepDefinition(name="replacement", type="run")]})

    resumed = TaskQueue._pinned_workflow(task)

    assert edited.steps[0].name == "replacement"
    assert resumed is not None
    assert [step.name for step in resumed.steps] == ["collect", "review", "finish"]


def test_effective_definition_is_pinned_exactly_once() -> None:
    original = _workflow()
    edited = original.model_copy(
        update={
            "version": 2,
            "steps": [StepDefinition(name="replacement", type="run")],
        }
    )
    state = pin_effective_workflow(WorkflowState(), original)
    original_digest = state.effective_workflow_digest

    pin_effective_workflow(state, edited)

    assert state.effective_workflow_version == 1
    assert state.effective_workflow_digest == original_digest
    assert state.effective_workflow_definition is not None
    assert state.effective_workflow_definition["steps"][0]["name"] == "collect"
