"""Effective, bounded ordinary-tool plans for controller-governed programs."""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any

from cognis.models.program import ProgramToolDescriptor, ProgramToolPlan
from cognis.models.tool import ToolDefinition, stable_tool_id, tool_input_schema
from cognis.tools.registry import ToolRegistry

_NEVER_NESTED_CATEGORIES = frozenset(
    {"program", "orchestration", "agent_management", "system_management", "schedule"}
)
_NEVER_NESTED_NAMES = frozenset(
    {
        "python_execute",
        "python_kernel_status",
        "python_kernel_reset",
        "call_tool",
        "delegate_task",
        "create_task",
        "cancel_task",
    }
)
_MODEL_INVOKING_NAMES = frozenset(
    {
        "web_research",
        "image_generate",
        "video_generate",
        "speech_synthesize",
        "speech_transcribe",
    }
)
_NESTED_SOURCE_TYPES = frozenset({"builtin", "executor", "local_mcp", "intaris_mcp"})


def _canonical_digest(value: Any) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _tool_schema_hash(definition: ToolDefinition) -> str:
    descriptor = getattr(definition, "descriptor", None)
    schema_hash = getattr(descriptor, "schema_hash", None)
    if isinstance(schema_hash, str) and schema_hash:
        return schema_hash
    return _canonical_digest(
        {
            "tool_id": stable_tool_id(definition),
            "input_schema": tool_input_schema(definition),
            "output_schema": (
                definition.descriptor.output_schema
                if definition.descriptor is not None
                else definition.output_schema
            ),
        }
    )


def _tool_alias(definition: ToolDefinition, *, collision: bool) -> str:
    base = re.sub(r"\W+", "_", definition.name).strip("_")
    if not base or base[0].isdigit():
        base = f"cognis_tool_{_canonical_digest(stable_tool_id(definition))[:8]}"
    if collision:
        base = f"{base}_{_canonical_digest(stable_tool_id(definition))[:8]}"
    return base


def program_tool_exclusion_reason(
    definition: ToolDefinition,
    *,
    read_only_required: bool = False,
    exclude_model_invoking: bool = False,
    workflow: bool = False,
) -> str | None:
    """Return why a tool is unavailable to a program in the given policy."""
    if definition.name in _NEVER_NESTED_NAMES:
        return "recursive, orchestration, or kernel-management tools are unavailable"
    if definition.category in _NEVER_NESTED_CATEGORIES:
        return "management tools are unavailable"
    if definition.source.type not in _NESTED_SOURCE_TYPES:
        return "tool source is not controller-governable"
    if workflow and definition.category in {"memory", "knowledgebase"}:
        return "model-backed tools are not available to workflow programs"
    if read_only_required and definition.read_only is not True:
        return "tool is not marked read-only"
    if exclude_model_invoking and (
        definition.name in _MODEL_INVOKING_NAMES
        or definition.category in {"image", "video", "speech", "inference"}
    ):
        return "model-invoking tools are unavailable"
    return None


def build_effective_program_plan(
    registry: ToolRegistry,
    *,
    read_only_required: bool = False,
    exclude_model_invoking: bool = False,
    allowed_tool_ids: list[str] | tuple[str, ...] | set[str] | None = None,
) -> ProgramToolPlan:
    """Freeze the current assignable plan without granting new authority.

    The source registry already reflects the agent's tool assignment and current
    runtime inventory. The program-specific exclusions prevent recursive
    orchestration and kernel-management calls; ordinary calls still pass through
    ToolRouter and fresh policy evaluation.
    """
    allowed = set(allowed_tool_ids) if allowed_tool_ids is not None else None
    candidates: list[ToolDefinition] = []
    for registered in registry.items():
        definition = registered.definition
        if allowed is not None and (
            definition.name not in allowed and stable_tool_id(definition) not in allowed
        ):
            continue
        if program_tool_exclusion_reason(
            definition,
            read_only_required=read_only_required,
            exclude_model_invoking=exclude_model_invoking,
        ):
            continue
        candidates.append(definition)
    candidates.sort(key=lambda item: stable_tool_id(item))
    name_counts: dict[str, int] = {}
    for definition in candidates:
        name_counts[definition.name] = name_counts.get(definition.name, 0) + 1
    tools: list[dict[str, Any]] = []
    for definition in candidates:
        tool_id = stable_tool_id(definition)
        tools.append(
            {
                "tool_id": tool_id,
                "name": definition.name,
                "alias": _tool_alias(definition, collision=name_counts[definition.name] > 1),
                "schema_hash": _tool_schema_hash(definition),
                "description": definition.description,
                "input_schema": tool_input_schema(definition),
                "read_only": definition.read_only,
                "output_schema": (
                    definition.descriptor.output_schema
                    if definition.descriptor is not None
                    else definition.output_schema
                ),
            }
        )
    return ProgramToolPlan(
        plan_hash=_canonical_digest(tools),
        tools=[ProgramToolDescriptor.model_validate(item) for item in tools],
        read_only_required=read_only_required,
        exclude_model_invoking=exclude_model_invoking,
        allowed_tool_ids=sorted(allowed) if allowed is not None else None,
    )


def descriptor_is_in_plan(
    plan: ProgramToolPlan,
    *,
    tool_id: str,
    schema_hash: str,
) -> dict[str, Any] | None:
    """Return one exact descriptor from a frozen plan."""
    for descriptor in plan.tools:
        if descriptor.tool_id == tool_id and descriptor.schema_hash == schema_hash:
            return descriptor.model_dump(mode="json")
    return None


__all__ = [
    "build_effective_program_plan",
    "descriptor_is_in_plan",
    "program_tool_exclusion_reason",
]
