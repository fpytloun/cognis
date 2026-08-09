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
    StepInputConfig,
    Workflow,
)
from cognis.store.database import create_engine, create_session_factory
from cognis.store.models import Base, User
from cognis.store.queries import (
    attach_project_workflow,
    create_project,
    upsert_system_workflow_override,
)


def test_system_workflows_are_registered() -> None:
    assert "system:direct" in SYSTEM_WORKFLOWS
    assert "system:general-task" in SYSTEM_WORKFLOWS
    assert "system:research" in SYSTEM_WORKFLOWS
    assert "system:software-development" in SYSTEM_WORKFLOWS
    assert "system:creative" in SYSTEM_WORKFLOWS


def test_workflow_accepts_input_scoped_session_reuse() -> None:
    workflow = Workflow(
        workflow_id="wf:reuse",
        name="Reuse",
        steps=[
            StepDefinition(name="plan", type="run"),
            StepDefinition(name="architect_review", type="run", agent_override="system:architect"),
            StepDefinition(
                name="implement",
                type="run",
                input=StepInputConfig(
                    type="last",
                    source=["plan", "architect_review"],
                    reuse_session_from="plan",
                ),
            ),
        ],
    )

    _validate_workflow(workflow)


@pytest.mark.parametrize(
    ("reuse_source", "source", "match"),
    [
        ("missing", ["plan"], "unknown step"),
        ("implement", ["plan"], "own session"),
        ("review", ["plan"], "input source"),
    ],
)
def test_workflow_rejects_invalid_session_reuse(
    reuse_source: str,
    source: list[str],
    match: str,
) -> None:
    workflow = Workflow(
        workflow_id="wf:invalid-reuse",
        name="Invalid reuse",
        steps=[
            StepDefinition(name="plan", type="run"),
            StepDefinition(name="review", type="run"),
            StepDefinition(
                name="implement",
                type="run",
                input=StepInputConfig(
                    type="last",
                    source=source,
                    reuse_session_from=reuse_source,
                ),
            ),
        ],
    )

    with pytest.raises(ValueError, match=match):
        _validate_workflow(workflow)


def test_workflow_rejects_explicit_reuse_profile_switch() -> None:
    workflow = Workflow(
        workflow_id="wf:profile-switch",
        name="Profile switch",
        steps=[
            StepDefinition(name="plan", type="run", agent_profile_id="architect"),
            StepDefinition(
                name="implement",
                type="run",
                agent_profile_id="developer",
                input=StepInputConfig(
                    type="last",
                    source="plan",
                    reuse_session_from="plan",
                ),
            ),
        ],
    )

    with pytest.raises(ValueError, match="runtime profiles"):
        _validate_workflow(workflow)


def test_system_workflow_phase_membership_is_golden() -> None:
    expected = {
        "system:direct": [("Execute", ["execute"])],
        "system:general-task": [("Execute", ["execute"])],
        "system:research": [
            ("Plan", ["plan", "pre_research_gate"]),
            ("Investigate", ["research"]),
            ("Deliver", ["synthesize"]),
        ],
        "system:software-development": [
            (
                "Plan",
                ["plan", "architect_review", "architect_review_route", "pre_implement_gate"],
            ),
            ("Build", ["implement", "update_docs"]),
            ("Verify", ["code_review", "code_review_route", "post_review_gate"]),
            ("Deliver", ["commit", "remember", "final_summary"]),
        ],
        "system:creative": [("Create", ["generate"])],
    }

    for workflow_id, phases in expected.items():
        workflow = SYSTEM_WORKFLOWS[workflow_id]
        assert workflow.presentation is not None
        assert [(phase.title, phase.step_names) for phase in workflow.presentation.phases] == phases
        assert [
            step_name for phase in workflow.presentation.phases for step_name in phase.step_names
        ] == [step.name for step in workflow.steps]


def test_direct_workflow_has_single_step_no_evaluation() -> None:
    w = DIRECT_WORKFLOW
    assert len(w.steps) == 1
    assert w.steps[0].name == "execute"
    assert w.steps[0].completion is not None
    assert w.steps[0].completion.evaluate is False


def test_general_task_workflow_has_deterministic_completion_contract() -> None:
    w = GENERAL_TASK_WORKFLOW
    assert len(w.steps) == 1
    assert w.steps[0].name == "execute"
    assert w.interaction.mode == "step_requests"
    assert w.steps[0].allow_questions is True
    assert w.steps[0].reasoning_effort == "low"
    assert w.steps[0].completion is not None
    assert w.steps[0].completion.evaluate is False
    assert w.steps[0].metadata_contract is not None
    assert {field.name for field in w.steps[0].metadata_contract.fields} == {
        "result_status",
        "verification",
    }
    assert w.steps[0].outcome_routes == [OutcomeRoute(status="failed", action="gate")]
    assert w.steps[0].objective
    assert w.steps[0].responsibilities


def test_research_and_creative_workflows_gate_failed_outcomes() -> None:
    for step in RESEARCH_WORKFLOW.steps:
        assert step.outcome_routes == [OutcomeRoute(status="failed", action="gate")]

    assert CREATIVE_WORKFLOW.steps[0].outcome_routes == [
        OutcomeRoute(status="failed", action="gate")
    ]


def test_software_development_primary_steps_preserve_session_continuity() -> None:
    implement_step = next(
        step for step in SOFTWARE_DEVELOPMENT_WORKFLOW.steps if step.name == "implement"
    )
    update_docs_step = next(
        step for step in SOFTWARE_DEVELOPMENT_WORKFLOW.steps if step.name == "update_docs"
    )

    assert implement_step.agent_override is None
    assert implement_step.objective == "Implement and verify the approved scope contract."
    assert "update_docs" in implement_step.defer_to
    assert "commit" in implement_step.defer_to
    assert implement_step.reasoning_effort == "medium"
    assert implement_step.input is not None
    assert implement_step.input.type == "last"
    assert implement_step.input.reuse_session_from == "plan"
    assert "managed implementation workstreams" in implement_step.prompt
    assert {"update_docs", "code_review", "commit", "remember", "final_summary"} <= set(
        implement_step.defer_to
    )
    assert implement_step.completion is not None
    assert implement_step.completion.evaluate is False
    assert implement_step.completion.evaluator_prompt is None
    assert implement_step.metadata_contract is not None
    implement_metadata = {field.name for field in implement_step.metadata_contract.fields}
    assert "scope_status" in implement_metadata
    assert "incomplete_required_scope_count" in implement_metadata
    assert "validation_summary" in implement_metadata
    assert update_docs_step.agent_override is None
    assert update_docs_step.input is not None
    assert update_docs_step.input.type == "last"
    assert update_docs_step.input.reuse_session_from == "implement"
    assert update_docs_step.metadata_contract is not None

    continuity = {
        "implement": "plan",
        "update_docs": "implement",
        "commit": "update_docs",
        "remember": "commit",
        "final_summary": "remember",
    }
    for step_name, source_name in continuity.items():
        step = next(step for step in SOFTWARE_DEVELOPMENT_WORKFLOW.steps if step.name == step_name)
        assert step.agent_override is None
        assert step.input is not None
        assert step.input.reuse_session_from == source_name


def test_software_development_review_steps_are_isolated_and_route_deterministically() -> None:
    architect_step = next(
        step for step in SOFTWARE_DEVELOPMENT_WORKFLOW.steps if step.name == "architect_review"
    )
    code_review_step = next(
        step for step in SOFTWARE_DEVELOPMENT_WORKFLOW.steps if step.name == "code_review"
    )
    architect_route = next(
        step
        for step in SOFTWARE_DEVELOPMENT_WORKFLOW.steps
        if step.name == "architect_review_route"
    )
    code_review_route = next(
        step for step in SOFTWARE_DEVELOPMENT_WORKFLOW.steps if step.name == "code_review_route"
    )
    post_review_gate = next(
        step for step in SOFTWARE_DEVELOPMENT_WORKFLOW.steps if step.name == "post_review_gate"
    )
    commit_step = next(
        step for step in SOFTWARE_DEVELOPMENT_WORKFLOW.steps if step.name == "commit"
    )

    assert architect_step.agent_override == "system:architect"
    assert code_review_step.agent_override == "system:code-review"
    assert architect_step.outcome_routes == [OutcomeRoute(status="failed", action="gate")]
    assert architect_step.input is not None
    assert architect_step.input.type == "full"
    assert architect_step.metadata_contract is not None
    assert {field.name for field in architect_step.metadata_contract.fields} == {
        "decision",
        "must_fix_count",
    }
    assert architect_step.completion is not None
    assert architect_step.completion.evaluate is False
    assert code_review_step.outcome_routes == [OutcomeRoute(status="failed", action="gate")]
    assert code_review_step.input is not None
    assert code_review_step.input.type == "last"
    assert code_review_step.completion is not None
    assert code_review_step.completion.evaluate is False
    assert code_review_step.completion.evaluator_prompt is None
    assert code_review_step.metadata_contract is not None
    review_metadata = {field.name for field in code_review_step.metadata_contract.fields}
    assert review_metadata == {
        "decision",
        "required_scope_complete",
        "missing_scope_count",
        "must_fix_count",
        "should_fix_count",
    }
    assert architect_route.condition is not None
    assert architect_route.condition.then == "plan"
    assert architect_route.condition.else_ == "pre_implement_gate"
    assert architect_route.condition.max_loop_iterations == 5
    assert code_review_route.condition is not None
    assert code_review_route.condition.then == "implement"
    assert code_review_route.condition.else_ == "post_review_gate"
    assert code_review_route.condition.max_loop_iterations == 5
    assert post_review_gate.gate is not None
    assert post_review_gate.gate.options[0].action == "revise(implement)"
    assert "metadata.code_review.should_fix_count > 0" in (
        post_review_gate.gate.conditions[0].expression
    )
    assert commit_step.outcome_routes == [OutcomeRoute(status="failed", action="gate")]
    assert commit_step.completion is not None
    assert commit_step.completion.evaluate is False
    assert "Do not push" in commit_step.prompt

    for step_name in ("plan", "implement", "update_docs", "remember"):
        step = next(step for step in SOFTWARE_DEVELOPMENT_WORKFLOW.steps if step.name == step_name)
        assert step.outcome_routes == [OutcomeRoute(status="failed", action="gate")]


def test_research_steps_share_primary_session_and_have_no_evaluators() -> None:
    plan_step = next(step for step in RESEARCH_WORKFLOW.steps if step.name == "plan")
    research_step = next(step for step in RESEARCH_WORKFLOW.steps if step.name == "research")
    synthesize_step = next(step for step in RESEARCH_WORKFLOW.steps if step.name == "synthesize")

    assert RESEARCH_WORKFLOW.interaction.mode == "step_requests"
    assert plan_step.allow_questions is True
    assert plan_step.completion is not None
    assert plan_step.completion.evaluator_prompt is None
    assert plan_step.completion.evaluate is False
    assert plan_step.metadata_contract is not None
    metadata_fields = {field.name for field in plan_step.metadata_contract.fields}
    assert "research_depth" in metadata_fields
    assert "media_strategy" in metadata_fields
    assert plan_step.agent_override is None
    assert research_step.agent_override is None
    assert synthesize_step.agent_override is None
    assert research_step.input is not None
    assert research_step.input.reuse_session_from == "plan"
    assert synthesize_step.input is not None
    assert synthesize_step.input.reuse_session_from == "research"
    assert research_step.completion is not None
    assert research_step.completion.evaluate is False
    assert synthesize_step.completion is not None
    assert synthesize_step.completion.evaluate is False
    assert research_step.reasoning_effort == "medium"


def test_software_development_run_steps_have_no_semantic_evaluators() -> None:
    for step in SOFTWARE_DEVELOPMENT_WORKFLOW.steps:
        if step.type != "run":
            continue
        assert step.completion is not None
        assert step.completion.evaluate is False
        assert step.completion.evaluator_prompt is None


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
            StepDefinition(
                name="implement",
                type="run",
                prompt="Implement",
                input=StepInputConfig(type="last", source=["plan"]),
            ),
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
                input=StepInputConfig(type="last", source="all"),
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
            StepDefinition(
                name="implement",
                type="run",
                input=StepInputConfig(type="last", source=["nonexistent"]),
            ),
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


@pytest.mark.parametrize(
    ("defer_to", "error"),
    [
        (["missing"], "defer_to references unknown step"),
        (["first"], "defer_to cannot reference itself"),
    ],
)
def test_validate_workflow_rejects_invalid_defer_targets(
    defer_to: list[str],
    error: str,
) -> None:
    workflow = Workflow(
        workflow_id="test:invalid-defer",
        name="Invalid defer",
        steps=[
            StepDefinition(name="first", type="run", defer_to=defer_to),
            StepDefinition(name="later", type="run"),
        ],
    )

    with pytest.raises(ValueError, match=error):
        _validate_workflow(workflow)


def test_validate_workflow_rejects_earlier_defer_target() -> None:
    workflow = Workflow(
        workflow_id="test:earlier-defer",
        name="Earlier defer",
        steps=[
            StepDefinition(name="first", type="run"),
            StepDefinition(name="later", type="run", defer_to=["first"]),
        ],
    )

    with pytest.raises(ValueError, match="defer_to cannot reference an earlier step"):
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
async def test_effective_system_override_preserves_shipped_phase_presentation(
    tmp_path: object,
) -> None:
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path}/override.db")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = create_session_factory(engine)
    try:
        async with factory() as session:
            session.add(User(email="owner@example.com", name="Owner", role="user"))
            await session.flush()
            await upsert_system_workflow_override(
                session,
                owner_email="owner@example.com",
                workflow_id="system:research",
                step_overrides={"research": {"reasoning_effort": "high"}},
            )
            await session.commit()

        workflow = await WorkflowRegistry(factory).get(
            "system:research",
            owner_email="owner@example.com",
        )

        assert workflow is not None
        assert workflow.steps[2].reasoning_effort == "high"
        assert workflow.presentation is not None
        assert [(phase.title, phase.step_names) for phase in workflow.presentation.phases] == [
            ("Plan", ["plan", "pre_research_gate"]),
            ("Investigate", ["research"]),
            ("Deliver", ["synthesize"]),
        ]
    finally:
        await engine.dispose()


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
