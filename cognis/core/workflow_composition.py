"""Workflow composition and skill decomposition helpers."""

from __future__ import annotations

import asyncio
import difflib
import json
from typing import Any, Literal

from pydantic import BaseModel, Field

from cognis.core.json_utils import (
    extract_json_object,
    extract_text_from_response,
    maybe_fallback_to_plain_json_response,
)
from cognis.core.step_profiles import list_step_profile_definitions
from cognis.logging import get_logger
from cognis.models.skill import ResolvedSkill, ResolvedSkillSet, SkillToolSpec
from cognis.models.tool import stable_tool_id
from cognis.models.workflow import Workflow
from cognis.tools.skill_service import normalize_linked_tool_ids
from cognis.tools.skills import skill_tools_to_definitions

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

    # Keep a small reserve for a plain-text retry, but avoid forcing larger
    # classifier models through an artificial early timeout.
    plain_timeout = max(2.0, min(10.0, total_timeout * 0.2))
    structured_timeout = max(4.0, total_timeout - plain_timeout)
    return structured_timeout, plain_timeout


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
    linked_tool_ids: list[str] = Field(default_factory=list)
    prompt_templates: dict[str, Any] = Field(default_factory=dict)
    secret_placeholders: list[str] = Field(default_factory=list)
    asset_manifest: list[dict[str, Any]] = Field(default_factory=list)
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


def _resolved_skill_tool_identifiers(
    *,
    skill_id: str,
    name: str,
    description: str | None,
    instructions: str,
    tools: list[dict[str, Any]],
    linked_tool_ids: list[str],
) -> list[str]:
    """Return stable tool identifiers surfaced by one skill."""

    parsed_tools: list[SkillToolSpec] = []
    for raw_tool in tools:
        if not isinstance(raw_tool, dict):
            continue
        parsed_tools.append(SkillToolSpec.model_validate(raw_tool))

    resolved = ResolvedSkillSet(
        skills=[
            ResolvedSkill(
                skill_id=skill_id,
                name=name,
                description=description,
                linked_tool_ids=normalize_linked_tool_ids(linked_tool_ids) or [],
                version_id="",
                version_number=0,
                content_hash="",
                instructions=instructions,
                tools=parsed_tools,
                attached=True,
            )
        ]
    )
    identifiers = {stable_tool_id(tool) for tool in skill_tools_to_definitions(resolved)}
    identifiers.update(normalize_linked_tool_ids(linked_tool_ids) or [])
    return sorted(identifiers)


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
    skill_tool_identifiers: list[str],
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

            if skill_tool_identifiers:
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
                for tool_identifier in skill_tool_identifiers:
                    if tool_identifier not in include:
                        include.append(tool_identifier)
                overrides["include"] = include
                inline_profile["tool_overrides"] = overrides
                normalized["step_profile"] = inline_profile

        normalized_steps.append(normalized)
    return normalized_steps


def _instruction_diff_summary(previous: str, current: str, *, limit: int = 80) -> str:
    """Return a compact unified diff for skill instructions."""

    diff_lines = list(
        difflib.unified_diff(
            previous.splitlines(),
            current.splitlines(),
            fromfile="previous",
            tofile="current",
            lineterm="",
            n=2,
        )
    )
    if not diff_lines:
        return ""
    trimmed = diff_lines[:limit]
    if len(diff_lines) > limit:
        trimmed.append("... diff truncated ...")
    return "\n".join(trimmed)


def _json_change_summary(previous: Any, current: Any) -> dict[str, Any]:
    """Summarize old/new JSON-ish values without generating a large raw diff."""

    return {
        "previous": previous,
        "current": current,
    }


async def decompose_skill_material(
    *,
    llm: Any,
    skill_id: str,
    name: str,
    description: str | None,
    instructions: str,
    tools: list[dict[str, Any]],
    linked_tool_ids: list[str] | None = None,
    prompt_templates: dict[str, Any],
    secret_placeholders: list[str] | None = None,
    asset_manifest: list[dict[str, Any]] | None = None,
    existing_steps: list[dict[str, Any]] | None = None,
    previous_instructions: str | None = None,
    previous_tools: list[dict[str, Any]] | None = None,
    previous_prompt_templates: dict[str, Any] | None = None,
    previous_secret_placeholders: list[str] | None = None,
    previous_asset_manifest: list[dict[str, Any]] | None = None,
    timeout_seconds: float = 60.0,
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
    skill_tool_identifiers = _resolved_skill_tool_identifiers(
        skill_id=skill_id,
        name=name,
        description=description,
        instructions=instructions,
        tools=tools,
        linked_tool_ids=linked_tool_ids or [],
    )
    refresh_existing = existing_steps is not None
    refresh_sections: list[str] = []
    if refresh_existing:
        refresh_sections.append(
            "Existing decomposition steps:\n"
            + json.dumps(existing_steps or [], indent=2, default=str)
        )
        if previous_instructions is not None and previous_instructions != instructions:
            diff_text = _instruction_diff_summary(previous_instructions, instructions)
            if diff_text:
                refresh_sections.append(f"Instruction diff:\n{diff_text}")
        if previous_tools is not None and previous_tools != tools:
            refresh_sections.append(
                "Tool changes:\n"
                + json.dumps(_json_change_summary(previous_tools, tools), indent=2, default=str)
            )
        if previous_prompt_templates is not None and previous_prompt_templates != prompt_templates:
            refresh_sections.append(
                "Prompt template changes:\n"
                + json.dumps(
                    _json_change_summary(previous_prompt_templates, prompt_templates),
                    indent=2,
                    default=str,
                )
            )
        if previous_secret_placeholders is not None and previous_secret_placeholders != (
            secret_placeholders or []
        ):
            refresh_sections.append(
                "Secret placeholder changes:\n"
                + json.dumps(
                    _json_change_summary(previous_secret_placeholders, secret_placeholders or []),
                    indent=2,
                    default=str,
                )
            )
        if previous_asset_manifest is not None and previous_asset_manifest != (asset_manifest or []):
            refresh_sections.append(
                "Asset manifest changes:\n"
                + json.dumps(
                    _json_change_summary(previous_asset_manifest, asset_manifest or []),
                    indent=2,
                    default=str,
                )
            )
    refresh_block = ""
    if refresh_sections:
        refresh_block = (
            "Refresh this skill's existing decomposition selectively when possible. "
            "Preserve unaffected step names, explicit input wiring, and intent unless the changes make them incorrect. "
            "Return the full updated step list, not a patch.\n\n"
            + "\n\n".join(refresh_sections)
            + "\n\n"
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
                f"Linked runtime tool ids:\n{linked_tool_ids or []}\n\n"
                f"Prompt templates:\n{prompt_templates}\n\n"
                f"Secret placeholders:\n{secret_placeholders or []}\n\n"
                f"Asset manifest:\n{asset_manifest or []}\n\n"
                + refresh_block
                + f"{_step_profile_catalog_text()}\n\n"
                + "Return JSON with keys 'rationale' and 'steps'. Keep the rationale short and return at most 8 steps. Each step must be a valid "
                + "Cognis StepDefinition object with name, type='run' or 'gate', prompt, and "
                + "any relevant completion/input/deliverable fields. Use require_deliverable=false "
                + "for obvious gather/inspect steps and true for synthesis/report/final steps. "
                + "Important input semantics: when input is omitted, Cognis defaults to the immediately preceding run step. "
                + "If multiple gather/collector steps depend on one earlier setup step, you must set input explicitly to that step instead of relying on defaults. "
                + "For final synthesis that should consume every prior run step, you may use input={type:'last', source:'all'} or input={type:'summary', source:'all'}. "
                + "Never use source='all' with type='full'. "
                + "Every run step should include step_profile_id. If this skill exposes linked or bundled tools, prefer step_profile_mode='hard' plus inline step_profile.tool_overrides.include for the exact required tool identifiers."
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
            skill_tool_identifiers=skill_tool_identifiers,
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
        skill_tool_identifiers: list[str] = []
        for material in skill_materials or []:
            for tool_identifier in _resolved_skill_tool_identifiers(
                skill_id=material.skill_id,
                name=material.name,
                description=material.description,
                instructions=material.instructions,
                tools=material.tools,
                linked_tool_ids=material.linked_tool_ids,
            ):
                if tool_identifier not in skill_tool_identifiers:
                    skill_tool_identifiers.append(tool_identifier)
        normalized_payload["steps"] = _normalize_generated_steps(
            raw_steps,
            default_profile_id="system:general-task",
            skill_tool_identifiers=skill_tool_identifiers,
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
