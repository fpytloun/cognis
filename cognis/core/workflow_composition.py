"""Workflow composition and skill decomposition helpers."""

from __future__ import annotations

import asyncio
from typing import Any, Literal

from pydantic import BaseModel, Field

from cognis.core.json_utils import extract_json_object, extract_text_from_response
from cognis.logging import get_logger
from cognis.models.workflow import Workflow

logger = get_logger(__name__)


class SkillMaterial(BaseModel):
    """Resolved skill material used during workflow composition."""

    skill_id: str
    name: str
    description: str | None = None
    instructions: str
    tools: list[dict[str, Any]] = Field(default_factory=list)
    prompt_templates: dict[str, Any] = Field(default_factory=dict)
    steps: list[dict[str, Any]] = Field(default_factory=list)
    decomposition_source_hash: str | None = None
    current_source_hash: str | None = None


class ComposeAndRunWorkflowArgs(BaseModel):
    """Arguments for the compose_and_run_workflow controller tool."""

    intent: str
    context: str | None = None
    title: str | None = None
    expected_output: str | None = None
    skill_hints: list[str] = Field(default_factory=list)
    template_hints: list[str] = Field(default_factory=list)
    base_workflow_id: str | None = None
    decompose_skills: Literal["auto", "always", "never"] = "auto"
    schedule: dict[str, Any] | None = None
    delivery: dict[str, Any] | None = None
    persist: bool = False
    agent_id: str | None = None
    priority: int | None = None


class SkillDecompositionResult(BaseModel):
    """Structured result returned by the skill decomposer."""

    rationale: str = ""
    steps: list[dict[str, Any]] = Field(default_factory=list)


class WorkflowComposerOutput(BaseModel):
    """Structured result returned by the workflow composer."""

    action: Literal["reuse_existing", "create_derived"]
    workflow_id: str | None = None
    workflow: dict[str, Any] | None = None
    rationale: str = ""
    title: str | None = None
    expected_output: str | None = None


async def decompose_skill_material(
    *,
    llm: Any,
    skill_id: str,
    name: str,
    description: str | None,
    instructions: str,
    tools: list[dict[str, Any]],
    prompt_templates: dict[str, Any],
    timeout_seconds: float = 30.0,
) -> SkillDecompositionResult:
    """Decompose a skill into reusable workflow steps."""

    from cognis.core.agent_registry import SYSTEM_AGENTS
    from cognis.models.workflow import StepDefinition

    decomposer_agent = SYSTEM_AGENTS.get("system:skill_decomposer")
    system_prompt = (
        decomposer_agent.system_prompt
        if decomposer_agent and decomposer_agent.system_prompt
        else "You decompose skills into workflow step fragments. Respond with JSON only."
    )
    messages = [
        {"role": "system", "content": system_prompt},
        {
            "role": "user",
            "content": (
                f"Skill ID: {skill_id}\n"
                f"Name: {name}\n"
                f"Description: {description or ''}\n"
                f"Instructions:\n{instructions}\n\n"
                f"Tools:\n{tools}\n\n"
                f"Prompt templates:\n{prompt_templates}\n\n"
                "Return JSON with keys 'rationale' and 'steps'. Each step must be a valid "
                "Cognis StepDefinition object with name, type='run' or 'gate', prompt, and "
                "any relevant completion/input/deliverable fields. Use require_deliverable=false "
                "for obvious gather/inspect steps and true for synthesis/report/final steps."
            ),
        },
    ]
    response = await asyncio.wait_for(
        llm.generate(
            messages,
            task_type="classifier",
            temperature=0,
            response_format={"type": "json_object"},
        ),
        timeout=timeout_seconds,
    )
    content = extract_text_from_response(response)
    payload = extract_json_object(content, label="skill_decomposer")
    result = SkillDecompositionResult.model_validate(payload)
    normalized_steps = [
        StepDefinition.model_validate(step).model_dump(mode="json") for step in result.steps
    ]
    return SkillDecompositionResult(rationale=result.rationale, steps=normalized_steps)


async def compose_workflow_plan(
    *,
    llm: Any,
    intent: str,
    context: str | None,
    available_workflows: list[Workflow],
    template_hints: list[str],
    base_workflow: Workflow | None,
    skill_materials: list[SkillMaterial],
    persist: bool,
    schedule_requested: bool,
    validator_feedback: str | None = None,
    timeout_seconds: float = 45.0,
) -> WorkflowComposerOutput:
    """Compose a reusable workflow or select an existing one."""

    from cognis.core.agent_registry import SYSTEM_AGENTS

    composer_agent = SYSTEM_AGENTS.get("system:workflow_composer")
    system_prompt = (
        composer_agent.system_prompt
        if composer_agent and composer_agent.system_prompt
        else "You compose workflows. Respond with JSON only."
    )
    workflow_summaries = []
    for workflow in available_workflows:
        workflow_summaries.append(
            {
                "workflow_id": workflow.workflow_id,
                "name": workflow.name,
                "criteria": workflow.criteria,
                "lifecycle": str(workflow.lifecycle),
                "steps": [step.name for step in workflow.steps],
            }
        )
    skill_payload = [skill.model_dump(mode="json") for skill in skill_materials]
    messages = [
        {"role": "system", "content": system_prompt},
        {
            "role": "user",
            "content": (
                f"Intent: {intent}\n"
                f"Context: {context or ''}\n"
                f"Persist requested: {str(persist).lower()}\n"
                f"Schedule requested: {str(schedule_requested).lower()}\n"
                f"Template hints: {template_hints}\n"
                f"Base workflow: {base_workflow.model_dump(mode='json') if base_workflow else None}\n"
                f"Available workflows: {workflow_summaries}\n"
                f"Skill materials: {skill_payload}\n\n"
                f"Validator feedback from a previous invalid attempt: {validator_feedback or ''}\n\n"
                "Choose action='reuse_existing' only when an available workflow already fits without "
                "modification. Otherwise return action='create_derived' with a full workflow object. "
                "Derived workflows must be valid Cognis Workflow objects except that workflow_id, "
                "owner_email, is_system, lifecycle, archived_at, and lineage may be omitted because the "
                "controller fills them in. Prefer smaller workflows over copying large templates when only "
                "part of them is needed."
            ),
        },
    ]
    response = await asyncio.wait_for(
        llm.generate(
            messages,
            task_type="classifier",
            temperature=0,
            response_format={"type": "json_object"},
        ),
        timeout=timeout_seconds,
    )
    content = extract_text_from_response(response)
    payload = extract_json_object(content, label="workflow_composer")
    return WorkflowComposerOutput.model_validate(payload)


def validate_composed_workflow(payload: dict[str, Any]) -> Workflow:
    """Validate a composed workflow definition."""

    from cognis.core.workflow_registry import _validate_workflow

    workflow = Workflow.model_validate(payload)
    _validate_workflow(workflow)
    return workflow


def workflow_preview_payload(workflow: Workflow) -> dict[str, Any]:
    """Build a compact workflow preview payload for tool results and events."""

    return {
        "name": workflow.name,
        "lifecycle": str(workflow.lifecycle),
        "steps": [step.name for step in workflow.steps],
        "lineage": workflow.lineage.model_dump(mode="json") if workflow.lineage else None,
    }
