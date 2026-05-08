"""Tests for the workflow registry."""

from __future__ import annotations

import pytest

from cognis.core.workflow_registry import (
    CREATIVE_WORKFLOW,
    DIRECT_WORKFLOW,
    GENERAL_TASK_WORKFLOW,
    RESEARCH_WORKFLOW,
    SOFTWARE_DEVELOPMENT_WORKFLOW,
    SYSTEM_WORKFLOWS,
    WorkflowRegistry,
    _validate_workflow,
)
from cognis.models.workflow import (
    OnRejectConfig,
    OutcomeRoute,
    StepDefinition,
    Workflow,
)
from cognis.store.database import create_engine, create_session_factory
from cognis.store.models import Base, User
from cognis.store.queries import attach_project_workflow, create_project


def test_system_workflows_are_registered() -> None:
    assert "system:direct" in SYSTEM_WORKFLOWS
    assert "system:general-task" in SYSTEM_WORKFLOWS
    assert "system:research" in SYSTEM_WORKFLOWS
    assert "system:software-development" in SYSTEM_WORKFLOWS
    assert "system:creative" in SYSTEM_WORKFLOWS


def test_direct_workflow_has_single_step_no_evaluation() -> None:
    w = DIRECT_WORKFLOW
    assert len(w.steps) == 1
    assert w.steps[0].name == "execute"
    assert w.steps[0].completion is not None
    assert w.steps[0].completion.evaluate is False


def test_general_task_workflow_has_single_step_with_evaluation() -> None:
    w = GENERAL_TASK_WORKFLOW
    assert len(w.steps) == 1
    assert w.steps[0].name == "execute"
    assert w.interaction.mode == "step_requests"
    assert w.steps[0].allow_questions is True
    assert w.steps[0].reasoning_effort == "low"
    assert w.steps[0].completion is not None
    assert w.steps[0].completion.evaluate is True
    assert "smallest correct change" in w.steps[0].prompt
    assert w.steps[0].outcome_routes == [OutcomeRoute(status="failed", action="gate")]


def test_research_and_creative_workflows_gate_failed_outcomes() -> None:
    for step in RESEARCH_WORKFLOW.steps:
        assert step.outcome_routes == [OutcomeRoute(status="failed", action="gate")]

    assert CREATIVE_WORKFLOW.steps[0].outcome_routes == [
        OutcomeRoute(status="failed", action="gate")
    ]


def test_software_development_workflow_uses_implement_specialist() -> None:
    implement_step = next(
        step for step in SOFTWARE_DEVELOPMENT_WORKFLOW.steps if step.name == "implement"
    )
    update_docs_step = next(
        step for step in SOFTWARE_DEVELOPMENT_WORKFLOW.steps if step.name == "update_docs"
    )

    assert implement_step.agent_override == "system:implement"
    assert implement_step.reasoning_effort == "medium"
    assert implement_step.input is not None
    assert implement_step.input.type == "summary"
    assert "smallest correct change" in implement_step.prompt
    assert "worktree, and branch setup" in implement_step.prompt
    assert "fix the issue and rerun" in implement_step.prompt
    assert "unrelated pre-existing reason" in implement_step.prompt
    assert implement_step.completion is not None
    assert implement_step.completion.evaluate is False
    assert update_docs_step.agent_override == "system:implement"
    assert update_docs_step.input is not None
    assert update_docs_step.input.type == "summary"
    assert "no documentation updates are needed" in update_docs_step.prompt


def test_software_development_review_steps_use_outcome_routes() -> None:
    architect_step = next(
        step for step in SOFTWARE_DEVELOPMENT_WORKFLOW.steps if step.name == "architect_review"
    )
    code_review_step = next(
        step for step in SOFTWARE_DEVELOPMENT_WORKFLOW.steps if step.name == "code_review"
    )
    commit_step = next(
        step for step in SOFTWARE_DEVELOPMENT_WORKFLOW.steps if step.name == "commit"
    )

    assert architect_step.outcome_routes == [
        OutcomeRoute(
            status="rejected",
            action="revise(plan)",
            max_loop_iterations=3,
            on_exhausted="gate",
        ),
        OutcomeRoute(status="failed", action="gate"),
    ]
    assert architect_step.input is not None
    assert architect_step.input.type == "full"
    assert "do not block on nitpicks" in architect_step.prompt
    assert "worktree/branch/PR handling" in architect_step.prompt
    assert (
        "plan is sound and ready, complete the step normally with success" in architect_step.prompt
    )
    assert "Put the outcome only in step_complete" in architect_step.prompt
    assert architect_step.completion is not None
    assert architect_step.completion.evaluate is False
    assert code_review_step.outcome_routes == [
        OutcomeRoute(
            status="rejected",
            action="revise(implement)",
            max_loop_iterations=3,
            on_exhausted="gate",
        ),
        OutcomeRoute(status="failed", action="gate"),
    ]
    assert code_review_step.input is not None
    assert code_review_step.input.type == "summary"
    assert (
        "changes are acceptable, complete the step normally with success" in code_review_step.prompt
    )
    assert "Put the outcome only in step_complete" in code_review_step.prompt
    assert code_review_step.completion is not None
    assert code_review_step.completion.evaluate is False
    assert commit_step.outcome_routes == [OutcomeRoute(status="failed", action="gate")]
    assert commit_step.completion is not None
    assert commit_step.completion.evaluate is False
    assert "Push and open a pull request only" in commit_step.prompt
    assert "publishing was not requested" in commit_step.prompt

    for step_name in ("plan", "implement", "update_docs", "remember"):
        step = next(step for step in SOFTWARE_DEVELOPMENT_WORKFLOW.steps if step.name == step_name)
        assert step.outcome_routes == [OutcomeRoute(status="failed", action="gate")]


def test_research_plan_step_uses_generic_evaluator_prompt() -> None:
    plan_step = next(step for step in RESEARCH_WORKFLOW.steps if step.name == "plan")
    research_step = next(step for step in RESEARCH_WORKFLOW.steps if step.name == "research")
    synthesize_step = next(step for step in RESEARCH_WORKFLOW.steps if step.name == "synthesize")

    assert RESEARCH_WORKFLOW.interaction.mode == "step_requests"
    assert plan_step.allow_questions is True
    assert plan_step.completion is not None
    assert plan_step.completion.evaluator_prompt is None
    assert "Expected deliverables and format" in plan_step.prompt
    assert "Appropriate depth: light, standard, or deep" in plan_step.prompt
    assert "Media/artifact strategy" in plan_step.prompt
    assert plan_step.metadata_contract is not None
    metadata_fields = {field.name for field in plan_step.metadata_contract.fields}
    assert "research_depth" in metadata_fields
    assert "media_strategy" in metadata_fields
    # Push the model toward delegation for non-trivial exploration so plan
    # steps don't burn the parent context on broad reads/greps.
    assert "delegate" in plan_step.prompt
    assert "system:explore" in plan_step.prompt
    assert research_step.reasoning_effort == "medium"
    assert "do not stop after the first useful result" in research_step.prompt
    assert "media/artifact references" in research_step.prompt
    assert "inline diagrams" in synthesize_step.prompt
    assert "Mermaid" in synthesize_step.prompt


def test_software_development_plan_step_uses_generic_evaluator_prompt() -> None:
    plan_step = next(step for step in SOFTWARE_DEVELOPMENT_WORKFLOW.steps if step.name == "plan")

    assert SOFTWARE_DEVELOPMENT_WORKFLOW.interaction.mode == "step_requests"
    assert plan_step.allow_questions is True
    for step in SOFTWARE_DEVELOPMENT_WORKFLOW.steps:
        if step.name != "plan":
            assert step.allow_questions is False
    assert plan_step.completion is not None
    assert plan_step.completion.evaluator_prompt is None
    assert plan_step.completion.evaluate is False
    assert "Files to create/modify (with rationale)" in plan_step.prompt
    assert "Environment and workspace setup" in plan_step.prompt
    assert "Worktree, branch, and repository strategy" in plan_step.prompt
    assert "Commit, push, and pull request strategy" in plan_step.prompt
    assert "read-only planning step" in plan_step.prompt
    assert "do not edit files" in plan_step.prompt
    assert "Later workflow steps handle implementation" in plan_step.prompt
    assert plan_step.metadata_contract is not None
    metadata_fields = {field.name for field in plan_step.metadata_contract.fields}
    assert "lifecycle_strategy" in metadata_fields
    assert "delegate" in plan_step.prompt
    assert "system:explore" in plan_step.prompt


def test_software_development_workflow_uses_review_steps_instead_of_evaluator() -> None:
    evaluated_by_review = {
        "plan",
        "architect_review",
        "implement",
        "code_review",
        "final_summary",
    }

    for step_name in evaluated_by_review:
        step = next(step for step in SOFTWARE_DEVELOPMENT_WORKFLOW.steps if step.name == step_name)
        assert step.completion is not None
        assert step.completion.evaluate is False


def test_creative_workflow_can_ask_brief_clarifications() -> None:
    step = CREATIVE_WORKFLOW.steps[0]

    assert CREATIVE_WORKFLOW.interaction.mode == "step_requests"
    assert step.allow_questions is True
    assert "audience, tone, format" in step.prompt


def test_validate_workflow_accepts_valid_definition() -> None:
    workflow = Workflow(
        workflow_id="test:valid",
        name="Valid",
        steps=[
            StepDefinition(name="plan", type="run", prompt="Plan"),
            StepDefinition(name="implement", type="run", prompt="Implement", input=["plan"]),
        ],
    )
    # Should not raise
    _validate_workflow(workflow)


def test_validate_workflow_accepts_all_input_source() -> None:
    workflow = Workflow(
        workflow_id="test:all-input",
        name="All Input",
        steps=[
            StepDefinition(name="setup", type="run", prompt="Setup"),
            StepDefinition(name="collect", type="run", prompt="Collect"),
            StepDefinition(
                name="synthesize",
                type="run",
                prompt="Synthesize",
                input={"type": "last", "source": "all"},
            ),
        ],
    )

    _validate_workflow(workflow)


def test_validate_workflow_rejects_duplicate_step_names() -> None:
    workflow = Workflow(
        workflow_id="test:dup",
        name="Duplicate",
        steps=[
            StepDefinition(name="plan", type="run"),
            StepDefinition(name="plan", type="run"),
        ],
    )
    with pytest.raises(ValueError, match="Duplicate step name"):
        _validate_workflow(workflow)


def test_validate_workflow_rejects_unknown_input_reference() -> None:
    workflow = Workflow(
        workflow_id="test:bad-input",
        name="Bad Input",
        steps=[
            StepDefinition(name="implement", type="run", input=["nonexistent"]),
        ],
    )
    with pytest.raises(ValueError, match="unknown/later input"):
        _validate_workflow(workflow)


def test_validate_workflow_rejects_forward_on_reject_target() -> None:
    workflow = Workflow(
        workflow_id="test:forward-reject",
        name="Forward Reject",
        steps=[
            StepDefinition(
                name="plan",
                type="run",
                on_reject=OnRejectConfig(target="implement"),
            ),
            StepDefinition(name="implement", type="run"),
        ],
    )
    with pytest.raises(ValueError, match="must reference an earlier step"):
        _validate_workflow(workflow)


def test_validate_workflow_rejects_unknown_on_reject_target() -> None:
    workflow = Workflow(
        workflow_id="test:unknown-reject",
        name="Unknown Reject",
        steps=[
            StepDefinition(
                name="plan",
                type="run",
                on_reject=OnRejectConfig(target="nonexistent"),
            ),
        ],
    )
    with pytest.raises(ValueError, match="unknown step"):
        _validate_workflow(workflow)


def test_validate_workflow_rejects_gate_without_config() -> None:
    workflow = Workflow(
        workflow_id="test:gate-no-config",
        name="Gate No Config",
        steps=[
            StepDefinition(name="approve", type="gate"),
        ],
    )
    with pytest.raises(ValueError, match="must have gate configuration"):
        _validate_workflow(workflow)


def test_step_definition_rejects_invalid_reasoning_effort() -> None:
    with pytest.raises(ValueError, match="reasoning_effort must be one of"):
        StepDefinition(name="plan", type="run", reasoning_effort="medimum")


def test_step_definition_normalizes_legacy_minimal_reasoning_effort() -> None:
    step = StepDefinition(name="plan", type="run", reasoning_effort="minimal")

    assert step.reasoning_effort == "low"


def test_validate_workflow_rejects_unknown_outcome_route_target() -> None:
    workflow = Workflow(
        workflow_id="test:bad-outcome-route",
        name="Bad Outcome Route",
        steps=[
            StepDefinition(
                name="review",
                type="run",
                outcome_routes=[OutcomeRoute(status="rejected", action="revise(plan)")],
            ),
        ],
    )
    with pytest.raises(ValueError, match="outcome route references unknown step"):
        _validate_workflow(workflow)


def test_validate_workflow_rejects_unsupported_outcome_route_action() -> None:
    workflow = Workflow(
        workflow_id="test:bad-outcome-action",
        name="Bad Outcome Action",
        steps=[
            StepDefinition(name="plan", type="run"),
            StepDefinition(
                name="review",
                type="run",
                outcome_routes=[OutcomeRoute(status="rejected", action="plan")],
            ),
        ],
    )
    with pytest.raises(ValueError, match="unsupported outcome route action"):
        _validate_workflow(workflow)


def test_validate_all_system_workflows() -> None:
    """Validate that all bundled system workflows pass validation."""
    for wf in SYSTEM_WORKFLOWS.values():
        _validate_workflow(wf)


@pytest.mark.asyncio
async def test_project_bound_workflows_require_matching_project(tmp_path: object) -> None:
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path}/cognis.db")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = create_session_factory(engine)

    try:
        async with factory() as session:
            session.add(User(email="owner@example.com", name="Owner", role="user"))
            await session.flush()
            project = await create_project(
                session,
                project_id="project-1",
                owner_email="owner@example.com",
                name="Project",
            )
            await attach_project_workflow(session, project.project_id, "system:research")
            await session.commit()

        registry = WorkflowRegistry(factory)

        assert await registry.get("system:research", project_id=None) is None
        assert await registry.get("system:research", project_id="project-1") is not None

        generic_ids = {workflow.workflow_id for workflow in await registry.list_all()}
        project_ids = {
            workflow.workflow_id for workflow in await registry.list_all(project_id="project-1")
        }
        assert "system:research" not in generic_ids
        assert "system:research" in project_ids
    finally:
        await engine.dispose()
