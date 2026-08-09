"""Golden composition tests for isolated workflow-step prompts."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from cognis.core.agent_loop import WORKFLOW_POLICY, AgentLoop, StepContext
from cognis.core.workflow_prompt import (
    WorkflowPromptBlockKind,
    WorkflowPromptLifetime,
    compose_workflow_prompt,
    render_context_comment,
)
from cognis.core.workflow_registry import (
    GENERAL_TASK_WORKFLOW,
    RESEARCH_WORKFLOW,
    SOFTWARE_DEVELOPMENT_WORKFLOW,
)
from cognis.models.agent import AgentDefinition
from cognis.models.workflow import (
    CompletionDeliveryPolicy,
    StepDefinition,
    StepOutput,
    WorkflowState,
)


def _compose_first_step(workflow: object):
    step = workflow.steps[0]
    return compose_workflow_prompt(
        workflow_id=workflow.workflow_id,
        workflow_name=workflow.name,
        task_title="TASK_TITLE_SENTINEL",
        task_description="TASK_DESCRIPTION_SENTINEL",
        task_expected_output="TASK_OUTPUT_SENTINEL",
        task_source_type="api",
        task_source_ref="SOURCE_REF_SENTINEL",
        attachment_refs=["att_prompt_sentinel (input.txt)"],
        project_context="PROJECT_CONTEXT_SENTINEL",
        step=step,
        step_prompt=step.prompt,
        prior_output_text="",
        todos=[],
        reviewer_feedback=None,
        revision_context=None,
        operator_instruction=None,
        completion_delivery=CompletionDeliveryPolicy(),
        require_step_complete=True,
        deliverable_owned=True,
    )


@pytest.mark.parametrize(
    "workflow",
    [GENERAL_TASK_WORKFLOW, RESEARCH_WORKFLOW, SOFTWARE_DEVELOPMENT_WORKFLOW],
    ids=["general", "research", "software-development"],
)
def test_first_step_prompt_categories_are_golden(workflow: object) -> None:
    composed = _compose_first_step(workflow)

    assert [block.kind for block in composed.blocks] == [
        WorkflowPromptBlockKind.WORKFLOW_CONTRACT,
        WorkflowPromptBlockKind.USER_TASK_CONTRACT,
        WorkflowPromptBlockKind.PROJECT_CONTEXT,
        WorkflowPromptBlockKind.ACTIVE_STEP_DIRECTIVE,
        WorkflowPromptBlockKind.RESPONSIBILITY_GUARD,
        WorkflowPromptBlockKind.COMPLETION_CONTRACT,
    ]
    assert [block.lifetime for block in composed.blocks] == [
        WorkflowPromptLifetime.WORKFLOW,
        WorkflowPromptLifetime.WORKFLOW,
        WorkflowPromptLifetime.WORKFLOW,
        WorkflowPromptLifetime.STEP,
        WorkflowPromptLifetime.STEP,
        WorkflowPromptLifetime.STEP,
    ]
    assert all(block.role == "user" for block in composed.blocks[1:3])
    assert all(block.role == "system" for block in (composed.blocks[0], *composed.blocks[3:]))

    transcript = f"{composed.user_message}\n{composed.controller_message}"
    for sentinel in (
        "TASK_TITLE_SENTINEL",
        "TASK_DESCRIPTION_SENTINEL",
        "TASK_OUTPUT_SENTINEL",
        "PROJECT_CONTEXT_SENTINEL",
        "att_prompt_sentinel",
    ):
        assert transcript.count(sentinel) == 1

    category_sizes = composed.category_characters()
    assert set(category_sizes) == {str(block.kind) for block in composed.blocks}
    assert all(0 < size < 12_000 for size in category_sizes.values())
    assert len(transcript) < 20_000


def test_isolated_reviewer_handoff_keeps_prior_deliverable_once() -> None:
    step = next(
        item for item in SOFTWARE_DEVELOPMENT_WORKFLOW.steps if item.name == "architect_review"
    )
    composed = compose_workflow_prompt(
        workflow_id=SOFTWARE_DEVELOPMENT_WORKFLOW.workflow_id,
        workflow_name=SOFTWARE_DEVELOPMENT_WORKFLOW.name,
        task_title="Review implementation plan",
        task_description="Review the prepared plan.",
        task_expected_output=None,
        task_source_type="api",
        task_source_ref=None,
        attachment_refs=[],
        project_context=None,
        step=step,
        step_prompt=step.prompt,
        prior_output_text=(
            '<step_output source="plan">\nDeliverable:\nPRIOR_DELIVERABLE_SENTINEL\n</step_output>'
        ),
        todos=[],
        reviewer_feedback=None,
        revision_context=None,
        operator_instruction=None,
        completion_delivery=CompletionDeliveryPolicy(),
        require_step_complete=True,
        deliverable_owned=True,
    )

    transcript = f"{composed.user_message}\n{composed.controller_message}"
    assert transcript.count("PRIOR_DELIVERABLE_SENTINEL") == 1
    input_block = next(
        block for block in composed.blocks if block.kind is WorkflowPromptBlockKind.INPUT_REFERENCES
    )
    assert input_block.role == "user"
    assert input_block.trust == "untrusted"
    assert "Deferred downstream:" in composed.controller_message
    assert "- implement" in composed.controller_message


def test_reused_step_prompt_is_compact_and_references_existing_source() -> None:
    step = StepDefinition(
        name="implement",
        type="run",
        objective="Implement the approved plan.",
        input={
            "type": "last",
            "source": ["plan", "architect_review"],
            "reuse_session_from": "plan",
        },
    )
    state = WorkflowState(
        step_outputs={
            "plan": StepOutput(
                summary="Plan",
                content="PLAN_DELIVERABLE_SENTINEL",
                deliverable_id="dlv_plan",
                session_id="sess-plan",
            ).model_dump(mode="json"),
            "architect_review": StepOutput(
                summary="Approved with one constraint.",
                content="REVIEWER_OUTPUT_SENTINEL",
                deliverable_id="dlv_review",
                session_id="sess-review",
            ).model_dump(mode="json"),
        }
    )
    loop = object.__new__(AgentLoop)
    ctx = StepContext(
        step_definition=step,
        session=SimpleNamespace(session_id="sess-plan", user_email="user@example.com"),
        conversation=SimpleNamespace(conversation_id="conv-plan"),
        agent=AgentDefinition(
            agent_id="primary",
            owner_email="user@example.com",
            name="Primary",
        ),
        policy=WORKFLOW_POLICY,
        task_title="TASK_TITLE_SENTINEL",
        task_description="TASK_DESCRIPTION_SENTINEL",
        project_context="PROJECT_CONTEXT_SENTINEL",
        workflow_id="wf:reuse",
        workflow_name="Reuse",
        workflow_state=state,
        workflow_steps=[
            StepDefinition(name="plan", type="run"),
            StepDefinition(name="architect_review", type="run"),
            step,
        ],
        step_index=2,
        session_reuse_source_step="plan",
        input_source_step_run_ids={"plan": "sr_plan", "architect_review": "sr_review"},
    )

    composed = loop._compose_step_prompt(ctx)
    transcript = f"{composed.user_message}\n{composed.controller_message}"

    assert "TASK_TITLE_SENTINEL" not in transcript
    assert "TASK_DESCRIPTION_SENTINEL" not in transcript
    assert "PROJECT_CONTEXT_SENTINEL" not in transcript
    assert "PLAN_DELIVERABLE_SENTINEL" not in transcript
    assert "step_run_id=sr_plan" in transcript
    assert transcript.count("REVIEWER_OUTPUT_SENTINEL") == 1


def test_step_instructions_remain_controller_owned_without_user_interpolation() -> None:
    step = StepDefinition(
        name="execute",
        type="run",
        objective="Complete the user request.",
        prompt="CONTROLLER_DETAIL_SENTINEL for {user_message}",
    )
    composed = compose_workflow_prompt(
        workflow_id="system:direct",
        workflow_name="Direct",
        task_title="Task",
        task_description="USER_TEXT_SENTINEL",
        task_expected_output=None,
        task_source_type="chat",
        task_source_ref="conv",
        attachment_refs=[],
        project_context=None,
        step=step,
        step_prompt=step.prompt,
        prior_output_text="",
        todos=[],
        reviewer_feedback=None,
        revision_context=None,
        operator_instruction=None,
        completion_delivery=CompletionDeliveryPolicy(),
        require_step_complete=True,
        deliverable_owned=False,
    )

    assert composed.controller_message.count("CONTROLLER_DETAIL_SENTINEL") == 1
    assert "Step instructions:" in composed.controller_message
    assert "the request in the user task contract" in composed.controller_message
    assert "{user_message}" not in composed.controller_message
    assert "USER_TEXT_SENTINEL" not in composed.controller_message
    assert composed.user_message.count("USER_TEXT_SENTINEL") == 1


def test_software_workflow_prompts_are_compact_and_boundary_scoped() -> None:
    prompts = {
        step.name: step.prompt for step in SOFTWARE_DEVELOPMENT_WORKFLOW.steps if step.type == "run"
    }

    assert all(len(prompt) < 500 for prompt in prompts.values())
    assert "managed implementation workstreams" in prompts["implement"]
    assert "Do not apply fixes" in prompts["code_review"]
    assert "Do not push" in prompts["commit"]
    assert "Do not perform more implementation work" in prompts["final_summary"]
    implement = next(
        step for step in SOFTWARE_DEVELOPMENT_WORKFLOW.steps if step.name == "implement"
    )
    assert "Update documentation" not in implement.responsibilities
    assert implement.defer_to == [
        "update_docs",
        "code_review",
        "commit",
        "remember",
        "final_summary",
    ]


def test_retry_and_routed_revision_do_not_replay_task_or_deliverable() -> None:
    step = next(item for item in SOFTWARE_DEVELOPMENT_WORKFLOW.steps if item.name == "implement")
    state = WorkflowState(
        last_retry_reason="routed_revision",
        last_evaluation_feedback="EVALUATOR_FEEDBACK_SENTINEL",
        last_revision_context="ROUTED_REVISION_SENTINEL",
        last_operator_instruction="HUMAN_INSTRUCTION_SENTINEL",
        step_outputs={
            "plan": StepOutput(
                summary="Plan",
                content="PRIOR_DELIVERABLE_SENTINEL",
                deliverable_id="dlv_plan",
            ).model_dump(mode="json")
        },
    )
    loop = object.__new__(AgentLoop)
    ctx = StepContext(
        step_definition=step,
        session=SimpleNamespace(session_id="sess", user_email="user@example.com"),
        conversation=SimpleNamespace(conversation_id="conv"),
        agent=AgentDefinition(
            agent_id="system:implement",
            owner_email="system@example.com",
            name="Implement",
            agent_type="secondary",
        ),
        policy=WORKFLOW_POLICY,
        is_retry=True,
        task_title="TASK_TITLE_SENTINEL",
        task_description="TASK_DESCRIPTION_SENTINEL",
        task_expected_output="TASK_OUTPUT_SENTINEL",
        workflow_id=SOFTWARE_DEVELOPMENT_WORKFLOW.workflow_id,
        workflow_name=SOFTWARE_DEVELOPMENT_WORKFLOW.name,
        step_run_id="sr_implement",
        workflow_state=state,
        workflow_steps=SOFTWARE_DEVELOPMENT_WORKFLOW.steps,
        step_index=SOFTWARE_DEVELOPMENT_WORKFLOW.steps.index(step),
    )

    composed = loop._compose_step_prompt(ctx)
    retry_message = loop._build_retry_step_prompt(ctx, composed)

    assert "TASK_TITLE_SENTINEL" not in retry_message
    assert "TASK_DESCRIPTION_SENTINEL" not in retry_message
    assert "TASK_OUTPUT_SENTINEL" not in retry_message
    assert "PRIOR_DELIVERABLE_SENTINEL" not in retry_message
    assert retry_message.count("EVALUATOR_FEEDBACK_SENTINEL") == 1
    assert retry_message.count("ROUTED_REVISION_SENTINEL") == 1
    assert retry_message.count("HUMAN_INSTRUCTION_SENTINEL") == 1
    assert state.last_revision_context is None


def test_full_software_chain_references_each_shared_session_source_without_replay() -> None:
    final_step = next(
        step for step in SOFTWARE_DEVELOPMENT_WORKFLOW.steps if step.name == "final_summary"
    )
    source_names = [
        "plan",
        "implement",
        "update_docs",
        "code_review",
        "commit",
        "remember",
    ]
    step_outputs = {
        name: StepOutput(
            summary=f"{name} summary",
            content=f"{name.upper()}_PAYLOAD_SENTINEL",
            deliverable_id=f"dlv_{name}",
            session_id="sess-primary" if name != "code_review" else "sess-review",
        ).model_dump(mode="json")
        for name in source_names
    }
    source_run_ids = {name: f"sr_{name}" for name in source_names}
    loop = object.__new__(AgentLoop)
    ctx = StepContext(
        step_definition=final_step,
        session=SimpleNamespace(session_id="sess-primary", user_email="user@example.com"),
        conversation=SimpleNamespace(conversation_id="conv-primary"),
        agent=AgentDefinition(
            agent_id="primary",
            owner_email="user@example.com",
            name="Primary",
        ),
        policy=WORKFLOW_POLICY,
        workflow_id=SOFTWARE_DEVELOPMENT_WORKFLOW.workflow_id,
        workflow_name=SOFTWARE_DEVELOPMENT_WORKFLOW.name,
        workflow_state=WorkflowState(step_outputs=step_outputs),
        workflow_steps=SOFTWARE_DEVELOPMENT_WORKFLOW.steps,
        step_index=SOFTWARE_DEVELOPMENT_WORKFLOW.steps.index(final_step),
        session_reuse_source_step="remember",
        input_source_step_run_ids=source_run_ids,
        input_source_session_ids={
            name: "sess-primary" if name != "code_review" else "sess-review"
            for name in source_names
        },
        previously_injected_source_step_run_ids=source_run_ids,
    )

    transcript = loop._build_step_prompt(ctx)

    for name in source_names:
        assert transcript.count(f"{name.upper()}_PAYLOAD_SENTINEL") <= 1
        assert f"step_run_id=sr_{name}" in transcript
    assert "CODE_REVIEW_PAYLOAD_SENTINEL" not in transcript


def test_human_context_comment_preserves_identity_and_provenance() -> None:
    rendered = render_context_comment(
        comment_id="tcmt_prompt",
        author_email="author@example.com",
        body="HUMAN_CONTEXT_SENTINEL",
        target_step="research",
    )

    assert 'trust="untrusted"' in rendered
    assert 'comment_id="tcmt_prompt"' in rendered
    assert 'author="author@example.com"' in rendered
    assert 'target="research"' in rendered
    assert rendered.count("HUMAN_CONTEXT_SENTINEL") == 1


def test_completion_guidance_has_one_static_owner() -> None:
    composed = _compose_first_step(GENERAL_TASK_WORKFLOW)

    assert composed.controller_message.count("write_deliverable") == 1
    assert composed.controller_message.count("step_complete") == 1
    completion_block = next(
        block
        for block in composed.blocks
        if block.kind is WorkflowPromptBlockKind.COMPLETION_CONTRACT
    )
    assert "write_deliverable" not in "".join(
        block.content for block in composed.blocks if block is not completion_block
    )
