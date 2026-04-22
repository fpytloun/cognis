"""Workflow composition and skill decomposition helpers."""

from __future__ import annotations

import asyncio
from typing import Any, Literal

from pydantic import BaseModel, Field

from cognis.core.json_utils import (
    extract_json_object,
    extract_text_from_response,
    maybe_fallback_to_plain_json_response,
)
from cognis.core.step_profiles import list_step_profile_definitions
from cognis.logging import get_logger
from cognis.models.workflow import Workflow

logger = get_logger(__name__)

_AGENT_OVERRIDE_PROFILE_IDS: dict[str, str] = {
    "system:research": "system:research",
    "system:implement": "system:coding",
    "system:committer": "system:coding",
    "system:code-review": "system:review",
    "system:architect": "system:review",
}
_STEP_PROFILE_PROMPT_HINTS: dict[str, str] = {
    "system:direct-default": "lightweight direct execution with a small default-visible tool surface",
    "system:general-task": "general execution, including shell, communication, office, and personal tools",
    "system:research": "read-heavy research and synthesis with a conservative default-visible tool surface",
    "system:coding": "implementation, debugging, build, and browser-assisted engineering work",
    "system:review": "read-focused review, critique, and analysis work",
}


def _split_json_generation_timeout(total_timeout: float) -> tuple[float, float]:
    """Split a JSON task timeout between structured and plain fallbacks."""

    if total_timeout <= 6.0:
        structured_timeout = max(1.5, total_timeout * 0.5)
        return structured_timeout, max(1.5, total_timeout - structured_timeout)
    structured_timeout = min(12.0, max(4.0, total_timeout * 0.4))
    return structured_timeout, max(2.0, total_timeout - structured_timeout)


async def _generate_json_response(
    *,
    llm: Any,
    messages: list[dict[str, Any]],
    task_type: str,
    label: str,
    timeout_seconds: float,
    logger_obj: Any,
    warning_context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Generate JSON with a structured-first attempt and plain fallback."""

    structured_timeout, plain_timeout = _split_json_generation_timeout(timeout_seconds)

    async def _generate(generate_kwargs: dict[str, Any], *, call_timeout: float) -> dict[str, Any]:
        return await asyncio.wait_for(
            llm.generate(
                messages,
                task_type=task_type,
                temperature=0,
                max_retries=1,
                **generate_kwargs,
            ),
            timeout=call_timeout,
        )

    try:
        response = await _generate(
            {"response_format": {"type": "json_object"}},
            call_timeout=structured_timeout,
        )
    except TimeoutError:
        extra_data = {"label": label, "reason": "structured_timeout"}
        if warning_context:
            extra_data.update(warning_context)
        logger_obj.warning(
            "Structured JSON generation timed out, retrying plain-text JSON fallback",
            extra={"extra_data": extra_data},
        )
        return await _generate({}, call_timeout=plain_timeout)

    return await maybe_fallback_to_plain_json_response(
        response,
        generate_response=lambda generate_kwargs: _generate(
            generate_kwargs,
            call_timeout=plain_timeout,
        ),
        label=label,
        logger_obj=logger_obj,
        warning_context=warning_context,
    )


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


def _declared_skill_tool_names(tools: list[dict[str, Any]]) -> list[str]:
    names: list[str] = []
    for raw_tool in tools:
        if not isinstance(raw_tool, dict):
            continue
        name = str(raw_tool.get("name") or "").strip()
        if name and name not in names:
            names.append(name)
    return names


def _step_profile_catalog_text() -> str:
    lines = ["Available seeded step profiles:"]
    for definition in list_step_profile_definitions():
        hint = _STEP_PROFILE_PROMPT_HINTS.get(
            definition.profile_id,
            "use this profile when its default tool surface matches the step",
        )
        lines.append(f"- {definition.profile_id}: {hint}")
    lines.extend(
        [
            "Every run step should set step_profile_id.",
            "Use step_profile_mode='hard' only when the step needs a constrained, explicit tool surface.",
            "When a step must expose specific skill-defined tools, add them via inline step_profile.tool_overrides.include.",
            "Keep allow_tool_search=true unless the step should forbid discovery of additional eligible tools.",
        ]
    )
    return "\n".join(lines)


def _normalize_generated_steps(
    steps: list[Any],
    *,
    default_profile_id: str,
    skill_tool_names: list[str],
) -> list[Any]:
    normalized_steps: list[Any] = []
    for step in steps:
        if not isinstance(step, dict):
            normalized_steps.append(step)
            continue
        if step.get("type") != "run":
            normalized_steps.append(step)
            continue

        normalized = dict(step)
        has_profile_fields = bool(normalized.get("step_profile_id")) or (
            normalized.get("step_profile") is not None
        )
        if not has_profile_fields:
            agent_override = str(normalized.get("agent_override") or "").strip()
            normalized["step_profile_id"] = _AGENT_OVERRIDE_PROFILE_IDS.get(
                agent_override,
                default_profile_id,
            )

            if skill_tool_names:
                if not normalized.get("step_profile_mode"):
                    normalized["step_profile_mode"] = "hard"
                raw_inline_profile = normalized.get("step_profile")
                inline_profile = (
                    dict(raw_inline_profile) if isinstance(raw_inline_profile, dict) else {}
                )
                raw_overrides = inline_profile.get("tool_overrides")
                overrides = dict(raw_overrides) if isinstance(raw_overrides, dict) else {}
                include = [
                    str(item).strip() for item in overrides.get("include", []) if str(item).strip()
                ]
                for tool_name in skill_tool_names:
                    if tool_name not in include:
                        include.append(tool_name)
                overrides["include"] = include
                inline_profile["tool_overrides"] = overrides
                normalized["step_profile"] = inline_profile

        normalized_steps.append(normalized)
    return normalized_steps


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
    skill_tool_names = _declared_skill_tool_names(tools)
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
                f"{_step_profile_catalog_text()}\n\n"
                "Return JSON with keys 'rationale' and 'steps'. Keep the rationale short and return at most 8 steps. Each step must be a valid "
                "Cognis StepDefinition object with name, type='run' or 'gate', prompt, and "
                "any relevant completion/input/deliverable fields. Use require_deliverable=false "
                "for obvious gather/inspect steps and true for synthesis/report/final steps. "
                "Every run step should include step_profile_id. If this skill declares executable skill tools, prefer step_profile_mode='hard' plus inline step_profile.tool_overrides.include for those tool names."
            ),
        },
    ]

    response = await _generate_json_response(
        llm=llm,
        messages=messages,
        task_type="classifier",
        label="skill_decomposer",
        logger_obj=logger,
        warning_context={"skill_id": skill_id},
        timeout_seconds=timeout_seconds,
    )
    content = extract_text_from_response(response)
    payload = extract_json_object(content, label="skill_decomposer")
    result = SkillDecompositionResult.model_validate(payload)
    normalized_steps = [
        StepDefinition.model_validate(step).model_dump(mode="json")
        for step in _normalize_generated_steps(
            result.steps,
            default_profile_id="system:general-task",
            skill_tool_names=skill_tool_names,
        )
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
                f"{_step_profile_catalog_text()}\n\n"
                f"Validator feedback from a previous invalid attempt: {validator_feedback or ''}\n\n"
                "Choose action='reuse_existing' only when an available workflow already fits without "
                "modification. Otherwise return action='create_derived' with a full workflow object. "
                "Derived workflows must be valid Cognis Workflow objects except that workflow_id, "
                "owner_email, is_system, lifecycle, archived_at, and lineage may be omitted because the "
                "controller fills them in. Prefer smaller workflows over copying large templates when only "
                "part of them is needed. Every run step should include step_profile_id, and when skill-defined tools are required you should use inline step_profile.tool_overrides.include for those tool names."
            ),
        },
    ]

    response = await _generate_json_response(
        llm=llm,
        messages=messages,
        task_type="classifier",
        label="workflow_composer",
        logger_obj=logger,
        warning_context={"intent_preview": intent[:80]},
        timeout_seconds=timeout_seconds,
    )
    content = extract_text_from_response(response)
    payload = extract_json_object(content, label="workflow_composer")
    return WorkflowComposerOutput.model_validate(payload)


def validate_composed_workflow(
    payload: dict[str, Any],
    *,
    skill_materials: list[SkillMaterial] | None = None,
) -> Workflow:
    """Validate a composed workflow definition."""

    from cognis.core.workflow_registry import _validate_workflow

    normalized_payload = dict(payload)
    raw_steps = payload.get("steps")
    if isinstance(raw_steps, list):
        skill_tool_names: list[str] = []
        for material in skill_materials or []:
            for tool_name in _declared_skill_tool_names(material.tools):
                if tool_name not in skill_tool_names:
                    skill_tool_names.append(tool_name)
        normalized_payload["steps"] = _normalize_generated_steps(
            raw_steps,
            default_profile_id="system:general-task",
            skill_tool_names=skill_tool_names,
        )

    workflow = Workflow.model_validate(normalized_payload)
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
