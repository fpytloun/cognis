"""Built-in LLM tools for skill management.

These tools allow agents to list, inspect, create, update, delete,
import, and export skills through the normal tool call flow.
All mutation operations are non-bypassable (Intaris evaluates them).
"""

from __future__ import annotations

import base64
from typing import Any

from cognis.logging import get_logger
from cognis.models.skill import (
    ImportProvenance,
    ResolvedSkill,
    ResolvedSkillSet,
    SkillAssetRef,
    SkillToolSpec,
)
from cognis.models.tool import NativeToolDefinition as ToolDefinition
from cognis.models.tool import ToolResult, ToolSource, stable_tool_id
from cognis.tools.skill_service import (
    apply_mapping_patch,
    apply_named_list_patch,
    asset_refs_to_inputs,
    cleanup_unpublished_skill_assets,
    compute_decomposition_source_hash,
    create_skill_version_with_assets,
    export_cognis_package,
    load_export_assets,
    load_skill_asset_refs,
    normalize_linked_tool_ids,
    normalize_prompt_templates,
    normalize_secret_placeholders,
    normalize_skill_steps,
    normalize_skill_tools,
    resolve_current_skill_version,
)
from cognis.tools.skills import _qualified_skill_tool_name, skill_tools_to_definitions

logger = get_logger(__name__)

_SKILL_SOURCE = ToolSource(type="builtin")


# ---------------------------------------------------------------------------
# Tool definitions
# ---------------------------------------------------------------------------


def skill_management_tools() -> list[ToolDefinition]:
    """Return all skill management tool definitions."""
    return [
        SKILL_LIST_TOOL,
        SKILL_LOAD_TOOL,
        SKILL_GET_TOOL,
        SKILL_VERSIONS_TOOL,
        SKILL_WRITE_TOOL,
        SKILL_PATCH_TOOL,
        SKILL_ASSET_WRITE_TOOL,
        SKILL_ASSET_DELETE_TOOL,
        SKILL_DELETE_TOOL,
        SKILL_IMPORT_URL_TOOL,
        SKILL_RESTORE_VERSION_TOOL,
        SKILL_EXPORT_TOOL,
    ]


SKILL_LIST_TOOL = ToolDefinition(
    name="skill_list",
    description=(
        "List all available skills. Usually prefer the available_skills metadata in the "
        "system prompt and load a specific skill with skill_load instead of browsing here."
    ),
    parameters={"type": "object", "properties": {}},
    source=_SKILL_SOURCE,
    category="skill",
    read_only=True,
    timeout_seconds=15,
)

SKILL_LOAD_TOOL = ToolDefinition(
    name="skill_load",
    description=(
        "Load a skill's full instructions, prompt templates, and tool summaries. "
        "Use this when you need to follow a skill's guidance. Always returns the "
        "latest published version. This is the primary way to access skill content "
        "— the available_skills metadata in the system prompt only contains summaries. "
        "Do not read filesystem SKILL.md files as a substitute for loading visible "
        "Cognis skills. "
        "Loading a skill also makes its deferred skill tools available for subsequent model calls."
    ),
    parameters={
        "type": "object",
        "properties": {
            "skill_id": {"type": "string", "description": "The skill ID to load"},
        },
        "required": ["skill_id"],
    },
    source=_SKILL_SOURCE,
    category="skill",
    read_only=True,
    timeout_seconds=15,
)

SKILL_GET_TOOL = ToolDefinition(
    name="skill_get",
    description="Get detailed information about a skill including version history, provenance, and asset manifest.",
    parameters={
        "type": "object",
        "properties": {
            "skill_id": {"type": "string", "description": "The skill ID to retrieve"},
        },
        "required": ["skill_id"],
    },
    source=_SKILL_SOURCE,
    category="skill",
    read_only=True,
    timeout_seconds=15,
)

SKILL_VERSIONS_TOOL = ToolDefinition(
    name="skill_versions",
    description="List immutable versions for a skill, newest first.",
    parameters={
        "type": "object",
        "properties": {
            "skill_id": {"type": "string", "description": "The skill ID to inspect"},
        },
        "required": ["skill_id"],
    },
    source=_SKILL_SOURCE,
    category="skill",
    read_only=True,
    timeout_seconds=15,
)

SKILL_WRITE_TOOL = ToolDefinition(
    name="skill_write",
    description=(
        "Create or update a skill. Provide skill_id to update an existing skill, "
        "or omit it to create a new one. Creates a new immutable version on each write. "
        "This is the authoritative path for skill creation and updates; do not "
        "create or edit filesystem SKILL.md files instead. Use proactively when a "
        "task reveals durable reusable procedure, tool guidance, safety boundaries, "
        "or user-specific workflow/style expectations. Prefer updating an existing "
        "relevant skill over creating a new one; create new skills only for recurring "
        "class-level workflows, not one-off progress notes, transient failures, or "
        "narrow bug fixes."
    ),
    parameters={
        "type": "object",
        "properties": {
            "skill_id": {
                "type": "string",
                "description": "Skill ID to update (omit to create new)",
            },
            "name": {"type": "string", "description": "Skill name"},
            "description": {"type": "string", "description": "Short description"},
            "instructions": {
                "type": "string",
                "description": "Skill instructions (markdown)",
            },
            "tags": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Tags for categorization",
            },
            "tools": {
                "type": "array",
                "items": {"type": "object"},
                "description": "Bundled executable skill tool definitions (optional)",
            },
            "linked_tool_ids": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Existing runtime tool ids to expose when this skill is attached or loaded",
            },
            "prompt_templates": {
                "type": "object",
                "description": "Prompt templates (optional)",
            },
            "secret_placeholders": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Environment variable placeholders required by this skill",
            },
            "steps": {
                "type": "array",
                "items": {"type": "object"},
                "description": (
                    "Optional workflow decomposition for this skill. Each item must be a workflow step "
                    "object and may include step_profile_id, step_profile_mode, and inline step_profile."
                ),
            },
            "decomposition_source_hash": {
                "type": "string",
                "description": (
                    "Optional hash proving the steps were generated from the current instructions. "
                    "When omitted, Cognis will compute one automatically if steps are provided."
                ),
            },
            "attach_to_all_agents": {
                "type": "boolean",
                "description": (
                    "Globally attach this skill to every agent by default. "
                    "Use rarely for universal platform skills only. This does not "
                    "preload the skill's full instructions; configure per-agent "
                    "auto_load_instructions for that behavior."
                ),
            },
        },
        "required": ["name", "instructions"],
    },
    source=_SKILL_SOURCE,
    category="skill",
    read_only=False,
    non_bypassable=True,
    timeout_seconds=30,
)

SKILL_PATCH_TOOL = ToolDefinition(
    name="skill_patch",
    description=(
        "Partially update an existing skill. Omitted fields remain unchanged; nested "
        "prompt_templates use set/remove and tools, steps, and assets use upsert/remove. "
        "Creates an immutable version only when versioned content changes. Use skill_write "
        "for creation or complete replacement."
    ),
    parameters={
        "type": "object",
        "properties": {
            "skill_id": {"type": "string"},
            "expected_current_version_id": {"type": "string"},
            "expected_content_hash": {"type": "string"},
            "name": {"type": "string"},
            "description": {"type": "string"},
            "instructions": {"type": "string"},
            "tags": {"type": "array", "items": {"type": "string"}},
            "attach_to_all_agents": {"type": "boolean"},
            "linked_tool_ids": {"type": "array", "items": {"type": "string"}},
            "secret_placeholders": {"type": "array", "items": {"type": "string"}},
            "prompt_templates": {"type": "object"},
            "tools": {"type": "object"},
            "steps": {"type": "object"},
            "assets": {"type": "object"},
        },
        "required": ["skill_id"],
    },
    source=_SKILL_SOURCE,
    category="skill",
    read_only=False,
    non_bypassable=True,
    timeout_seconds=30,
)

SKILL_ASSET_WRITE_TOOL = ToolDefinition(
    name="skill_asset_write",
    description=(
        "Add or replace a skill asset by filename. Agents may attach text/script content directly "
        "or reuse an existing published artifact via source_artifact_id. Use this Cognis tool for "
        "skill assets instead of writing files into a filesystem skill directory."
    ),
    parameters={
        "type": "object",
        "properties": {
            "skill_id": {"type": "string", "description": "Target skill ID"},
            "filename": {"type": "string", "description": "Asset filename or relative path"},
            "content": {"type": "string", "description": "UTF-8 text content for the asset"},
            "source_artifact_id": {
                "type": "string",
                "description": "Existing artifact ID to attach instead of inline content",
            },
            "content_type": {
                "type": "string",
                "description": "Optional MIME type override",
            },
        },
        "required": ["skill_id", "filename"],
    },
    source=_SKILL_SOURCE,
    category="skill",
    read_only=False,
    non_bypassable=True,
    timeout_seconds=30,
)

SKILL_ASSET_DELETE_TOOL = ToolDefinition(
    name="skill_asset_delete",
    description=(
        "Remove an asset from a skill by filename, creating a new skill version. "
        "Use this Cognis tool instead of deleting files from a filesystem skill directory."
    ),
    parameters={
        "type": "object",
        "properties": {
            "skill_id": {"type": "string", "description": "Target skill ID"},
            "filename": {"type": "string", "description": "Asset filename or relative path"},
        },
        "required": ["skill_id", "filename"],
    },
    source=_SKILL_SOURCE,
    category="skill",
    read_only=False,
    non_bypassable=True,
    timeout_seconds=15,
)

SKILL_DELETE_TOOL = ToolDefinition(
    name="skill_delete",
    description=(
        "Delete a skill by ID. This removes the skill and all its versions. "
        "Use this Cognis tool instead of removing filesystem skill manifests."
    ),
    parameters={
        "type": "object",
        "properties": {
            "skill_id": {"type": "string", "description": "The skill ID to delete"},
        },
        "required": ["skill_id"],
    },
    source=_SKILL_SOURCE,
    category="skill",
    read_only=False,
    non_bypassable=True,
    timeout_seconds=15,
)

SKILL_IMPORT_URL_TOOL = ToolDefinition(
    name="skill_import_url",
    description=(
        "Import a skill from a URL. Supports SKILL.md files (Claude Code / Agent Skills format), "
        "GitHub blob/raw/folder URLs, and Cognis YAML format. "
        "The imported skill is created as a new DB-managed skill with provenance tracking; "
        "do not manually create filesystem SKILL.md files for skill management."
    ),
    parameters={
        "type": "object",
        "properties": {
            "url": {"type": "string", "description": "URL to import from"},
            "name": {
                "type": "string",
                "description": "Override the imported skill name (optional)",
            },
            "tags": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Tags to apply (optional)",
            },
            "linked_tool_ids": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Existing runtime tool ids to expose when this imported skill is attached or loaded",
            },
            "attach_to_all_agents": {
                "type": "boolean",
                "description": "Attach this skill to all agents by default (default false)",
            },
        },
        "required": ["url"],
    },
    source=_SKILL_SOURCE,
    category="skill",
    read_only=False,
    non_bypassable=True,
    timeout_seconds=60,
)

SKILL_EXPORT_TOOL = ToolDefinition(
    name="skill_export",
    description=(
        "Export a Cognis-managed skill as SKILL.md, Cognis YAML, or a full Cognis package. "
        "Exports are generated through Cognis; do not manage skills by editing exported SKILL.md files."
    ),
    parameters={
        "type": "object",
        "properties": {
            "skill_id": {"type": "string", "description": "The skill ID to export"},
            "format": {
                "type": "string",
                "enum": ["skill_md", "cognis_yaml", "cognis_package"],
                "description": "Export format (default: skill_md)",
            },
        },
        "required": ["skill_id"],
    },
    source=_SKILL_SOURCE,
    category="skill",
    read_only=True,
    timeout_seconds=15,
)

SKILL_RESTORE_VERSION_TOOL = ToolDefinition(
    name="skill_restore_version",
    description="Restore a skill to one of its previous immutable versions.",
    parameters={
        "type": "object",
        "properties": {
            "skill_id": {"type": "string", "description": "Target skill ID"},
            "version_id": {"type": "string", "description": "Version ID to restore"},
        },
        "required": ["skill_id", "version_id"],
    },
    source=_SKILL_SOURCE,
    category="skill",
    read_only=False,
    non_bypassable=True,
    timeout_seconds=15,
)


# ---------------------------------------------------------------------------
# Tool identification
# ---------------------------------------------------------------------------

_SKILL_TOOL_NAMES = {
    "skill_list",
    "skill_load",
    "skill_get",
    "skill_versions",
    "skill_write",
    "skill_patch",
    "skill_asset_write",
    "skill_asset_delete",
    "skill_delete",
    "skill_import_url",
    "skill_restore_version",
    "skill_export",
}


def is_skill_management_tool(tool_name: str) -> bool:
    """Check if a tool name is a skill management tool."""
    return tool_name in _SKILL_TOOL_NAMES


# ---------------------------------------------------------------------------
# Auto-bind helper
# ---------------------------------------------------------------------------


async def _auto_bind_skill_to_agent(
    session_factory: Any, skill_id: str, *, agent_id: str | None = None
) -> None:
    """Add a skill to the current agent's selected skills if not already present.

    Uses ``current_agent_id`` from the runtime context.  Best-effort:
    silently returns if no agent context is available or on any error.
    """
    from cognis.runtime_context import current_agent_id
    from cognis.store.queries import get_agent, update_agent

    target_agent_id = agent_id or current_agent_id.get()
    if not target_agent_id:
        return

    try:
        async with session_factory() as session:
            agent_row = await get_agent(session, target_agent_id)
            if agent_row is None:
                return

            skills_json = dict(agent_row.skills or {})
            items = skills_json.get("items", [])
            if not isinstance(items, list):
                items = []

            # Check if already bound
            for item in items:
                if isinstance(item, dict) and item.get("skill_id") == skill_id:
                    return  # already bound

            # Add the skill ref
            items.append({"skill_id": skill_id, "enabled": True})
            skills_json["items"] = items

            await update_agent(session, target_agent_id, updates={"skills": skills_json})
            await session.commit()
    except Exception:
        logger.warning(
            "Failed to auto-bind skill to agent",
            extra={"extra_data": {"skill_id": skill_id, "agent_id": target_agent_id}},
            exc_info=True,
        )


def _resolve_attach_to_all_agents(arguments: dict[str, Any]) -> bool:
    """Resolve the user-facing global attachment flag.

    ``attach_to_all_agents`` is the preferred name. ``auto_load`` remains an
    accepted legacy alias for backward compatibility.
    """

    if "attach_to_all_agents" in arguments:
        return bool(arguments.get("attach_to_all_agents"))
    return bool(arguments.get("auto_load", False))


def _resolved_skill_tool_ids(
    skill_id: str,
    name: str,
    description: str | None,
    attach_to_all_agents: bool,
    instructions: str,
    tools: list[dict[str, Any]] | dict[str, Any] | None,
    linked_tool_ids: list[str] | None = None,
) -> set[str]:
    """Return stable tool ids for one resolved skill payload."""

    parsed_tools: list[SkillToolSpec] = []
    raw_tools: list[Any] = []
    if isinstance(tools, dict):
        raw_tools = list(tools.values())
    elif isinstance(tools, list):
        raw_tools = tools
    for raw in raw_tools:
        if not isinstance(raw, dict):
            continue
        try:
            parsed_tools.append(SkillToolSpec.model_validate(raw))
        except Exception:
            logger.warning("Skipping invalid skill tool while resolving loaded skill tool ids")

    tool_defs = skill_tools_to_definitions(
        ResolvedSkillSet(
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
                    auto_load=attach_to_all_agents,
                    attached=True,
                )
            ]
        )
    )
    resolved_tool_ids = {stable_tool_id(tool) for tool in tool_defs}
    resolved_tool_ids.update(normalize_linked_tool_ids(linked_tool_ids) or [])
    return resolved_tool_ids


def materialize_loaded_skill_context(
    *,
    skill_id: str,
    name: str,
    description: str | None,
    instructions: str,
    tools: list[dict[str, Any]] | dict[str, Any] | None,
    templates: dict[str, Any] | None,
    asset_refs: list[SkillAssetRef],
    steps: list[dict[str, Any]] | None,
    tags: list[str] | None,
    linked_tool_ids: list[str] | None,
    attach_to_all_agents: bool = False,
    version_id: str | None = None,
    version_number: int | None = None,
    content_hash: str | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Build the protected context and activation metadata for a loaded skill."""

    import json

    asset_manifest = _skill_asset_llm_manifest(asset_refs)
    available_skill_tools = _skill_tool_runtime_summaries(skill_id, tools)

    protected_context_parts = [
        "<loaded_skill>",
        f"<skill_id>{skill_id}</skill_id>",
        f"<name>{name}</name>",
    ]
    if description:
        protected_context_parts.append(f"<description>{description}</description>")
    if isinstance(instructions, str) and instructions.strip():
        protected_context_parts.append(f"<instructions>\n{instructions}\n</instructions>")
    if tools:
        protected_context_parts.append(
            "<tool_summaries>\n" + json.dumps(tools, indent=2, default=str) + "\n</tool_summaries>"
        )
    if available_skill_tools:
        protected_context_parts.append(
            "<available_skill_tools>\n"
            + json.dumps(available_skill_tools, indent=2, default=str)
            + "\n</available_skill_tools>"
        )
    if asset_manifest:
        protected_context_parts.append(
            "<asset_manifest>\n"
            + json.dumps(asset_manifest, indent=2, default=str)
            + "\n</asset_manifest>"
        )
        protected_context_parts.append(
            "<asset_guidance>Prefer available_skill_tools for runnable skill behavior. "
            "Use skill_asset_materialize only when you need an asset-only script or "
            "need to inspect an asset as an executor-local file.</asset_guidance>"
        )
    if templates:
        protected_context_parts.append(
            "<prompt_templates>\n"
            + json.dumps(templates, indent=2, default=str)
            + "\n</prompt_templates>"
        )
    if steps:
        protected_context_parts.append(
            "<workflow_steps>\n" + json.dumps(steps, indent=2, default=str) + "\n</workflow_steps>"
        )
    protected_context_parts.append("</loaded_skill>")

    declared_tool_ids = sorted(
        _resolved_skill_tool_ids(
            skill_id,
            name,
            description,
            attach_to_all_agents,
            instructions,
            tools,
            linked_tool_ids,
        )
    )

    result = {
        "skill_id": skill_id,
        "name": name,
        "description": description,
        "loaded": True,
        "tool_count": len(tools or []),
        "step_count": len(steps or []),
        "template_count": len(templates or {}),
        "asset_count": len(asset_refs),
        "available_skill_tools": available_skill_tools,
        "asset_manifest": asset_manifest,
        "asset_guidance": (
            "Prefer available_skill_tools for runnable behavior. Use "
            "skill_asset_materialize(skill_id, asset_id) only for asset-only scripts "
            "or asset inspection."
            if asset_manifest
            else None
        ),
        "message": "Skill loaded into working context for this turn.",
        "tags": tags or [],
        "linked_tool_ids": linked_tool_ids or [],
        "version_id": version_id,
        "version_number": version_number,
        "content_hash": content_hash or "",
    }
    from cognis.core.daily_brief_contract import daily_brief_contract_version

    contract_version = daily_brief_contract_version(
        name=name,
        prompt_templates=templates,
        instructions=instructions,
    )
    result["contract_version"] = contract_version
    metadata = {
        "protected_context": "\n".join(protected_context_parts),
        "discovered_tool_ids": declared_tool_ids,
        "legacy_skill_tool_fallback": linked_tool_ids is None and not declared_tool_ids,
        "skill_activation": {
            "skill_id": skill_id,
            "name": name,
            "description": description,
            "instructions": instructions,
            "tags": tags or [],
            "version_id": version_id,
            "version_number": version_number,
            "content_hash": content_hash or "",
            "contract_version": contract_version,
        },
    }
    return result, metadata


# ---------------------------------------------------------------------------
# Tool handlers
# ---------------------------------------------------------------------------


async def handle_skill_management_tool(
    tool_name: str,
    arguments: dict[str, Any],
    session_factory: Any,
    user_email: str,
    llm: Any | None = None,
    artifact_store: Any | None = None,
    current_agent_id: str | None = None,
) -> ToolResult:
    """Handle a skill management tool call."""
    try:
        if tool_name == "skill_list":
            return await _handle_skill_list(session_factory, user_email)
        if tool_name == "skill_load":
            return await _handle_skill_load(session_factory, user_email, arguments)
        if tool_name == "skill_get":
            return await _handle_skill_get(session_factory, user_email, arguments)
        if tool_name == "skill_versions":
            return await _handle_skill_versions(session_factory, user_email, arguments)
        if tool_name == "skill_write":
            return await _handle_skill_write(
                session_factory,
                user_email,
                arguments,
                llm=llm,
                artifact_store=artifact_store,
                current_agent_id=current_agent_id,
            )
        if tool_name == "skill_patch":
            return await _handle_skill_patch(
                session_factory, user_email, arguments, artifact_store=artifact_store
            )
        if tool_name == "skill_asset_write":
            return await _handle_skill_asset_write(
                session_factory, user_email, arguments, llm=llm, artifact_store=artifact_store
            )
        if tool_name == "skill_asset_delete":
            return await _handle_skill_asset_delete(
                session_factory, user_email, arguments, llm=llm, artifact_store=artifact_store
            )
        if tool_name == "skill_delete":
            return await _handle_skill_delete(session_factory, user_email, arguments)
        if tool_name == "skill_import_url":
            return await _handle_skill_import_url(
                session_factory, user_email, arguments, artifact_store=artifact_store
            )
        if tool_name == "skill_restore_version":
            return await _handle_skill_restore_version(session_factory, user_email, arguments)
        if tool_name == "skill_export":
            return await _handle_skill_export(
                session_factory, user_email, arguments, artifact_store=artifact_store
            )
        return ToolResult(output=f"Unknown skill tool: {tool_name}", is_error=True)
    except Exception as exc:
        logger.warning(
            "Skill management tool failed",
            extra={"extra_data": {"tool_name": tool_name}},
            exc_info=True,
        )
        return ToolResult(output=f"Skill operation failed: {str(exc)[:500]}", is_error=True)


def _canonical_asset_inputs(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    canonical: list[dict[str, Any]] = []
    for item in items:
        payload = dict(item)
        if payload.get("existing_asset_id"):
            payload.pop("content_type", None)
        canonical.append(payload)
    return canonical


def _decomposition_inputs_changed(
    *,
    instructions: str,
    tools: list[dict[str, Any]] | None,
    linked_tool_ids: list[str] | None,
    prompt_templates: dict[str, Any] | None,
    secret_placeholders: list[str] | None,
    asset_inputs: list[dict[str, Any]] | None,
    current_instructions: str,
    current_tools: list[dict[str, Any]] | None,
    current_linked_tool_ids: list[str] | None,
    current_templates: dict[str, Any] | None,
    current_placeholders: list[str] | None,
    current_asset_inputs: list[dict[str, Any]] | None,
) -> bool:
    """Return whether skill content that drives decomposition changed."""

    return any(
        [
            instructions != current_instructions,
            (tools or []) != (current_tools or []),
            (linked_tool_ids or []) != (current_linked_tool_ids or []),
            (prompt_templates or {}) != (current_templates or {}),
            (secret_placeholders or []) != (current_placeholders or []),
            (asset_inputs or []) != (current_asset_inputs or []),
        ]
    )


async def _refresh_skill_steps_if_needed(
    *,
    llm: Any | None,
    skill_id: str,
    skill_name: str,
    description: str | None,
    instructions: str,
    tools: list[dict[str, Any]] | None,
    linked_tool_ids: list[str] | None,
    prompt_templates: dict[str, Any] | None,
    secret_placeholders: list[str] | None,
    asset_inputs: list[dict[str, Any]] | None,
    current_instructions: str,
    current_tools: list[dict[str, Any]] | None,
    current_linked_tool_ids: list[str] | None,
    current_templates: dict[str, Any] | None,
    current_placeholders: list[str] | None,
    current_steps: list[dict[str, Any]] | None,
    current_asset_inputs: list[dict[str, Any]] | None,
) -> tuple[list[dict[str, Any]] | None, str | None]:
    """Refresh saved skill decomposition when omitted steps became stale."""

    if current_steps is None:
        return None, None
    comparable_asset_inputs = _canonical_asset_inputs(asset_inputs or [])
    if not _decomposition_inputs_changed(
        instructions=instructions,
        tools=tools,
        linked_tool_ids=linked_tool_ids,
        prompt_templates=prompt_templates,
        secret_placeholders=secret_placeholders,
        asset_inputs=comparable_asset_inputs,
        current_instructions=current_instructions,
        current_tools=current_tools,
        current_linked_tool_ids=current_linked_tool_ids,
        current_templates=current_templates,
        current_placeholders=current_placeholders,
        current_asset_inputs=_canonical_asset_inputs(current_asset_inputs or []),
    ):
        return current_steps, None
    if llm is None:
        return None, "LLM provider is not available for decomposition refresh"

    from cognis.core.workflow_composition import decompose_skill_material

    try:
        decomposition = await decompose_skill_material(
            llm=llm,
            skill_id=skill_id,
            name=skill_name,
            description=description,
            instructions=instructions,
            tools=tools or [],
            linked_tool_ids=linked_tool_ids or [],
            prompt_templates=prompt_templates or {},
            secret_placeholders=secret_placeholders or [],
            asset_manifest=comparable_asset_inputs,
            existing_steps=current_steps,
            previous_instructions=current_instructions,
            previous_tools=current_tools or [],
            previous_prompt_templates=current_templates or {},
            previous_secret_placeholders=current_placeholders or [],
            previous_asset_manifest=_canonical_asset_inputs(current_asset_inputs or []),
        )
    except TimeoutError:
        return None, "Timed out while refreshing the saved skill decomposition"
    except ValueError as exc:
        return None, f"Skill decomposition refresh returned invalid output: {exc}"
    except Exception as exc:
        return None, f"Failed to refresh the saved skill decomposition: {exc}"
    return decomposition.steps, None


async def _handle_skill_list(session_factory: Any, user_email: str) -> ToolResult:
    import json

    from cognis.store.queries import list_skills

    async with session_factory() as session:
        rows = await list_skills(session, owner_email=user_email)

    skills = []
    for row in rows:
        skills.append(
            {
                "skill_id": row.skill_id,
                "name": row.name,
                "description": row.description,
                "tags": row.tags or [],
                "linked_tool_ids": row.linked_tool_ids or [],
                "attach_to_all_agents": row.auto_load,
                "auto_load": row.auto_load,
                "source": row.source,
                "current_version_id": row.current_version_id,
            }
        )
    return ToolResult(output=json.dumps(skills, indent=2))


async def _handle_skill_load(
    session_factory: Any, user_email: str, arguments: dict[str, Any]
) -> ToolResult:
    """Load a skill's full instructions, templates, and tool summaries.

    This is the primary way the model accesses skill content.  Always
    returns the latest published version.
    """
    import json

    from cognis.store.queries import get_skill_scoped, list_skills

    skill_id = str(arguments.get("skill_id") or arguments.get("skill") or "").strip()
    if not skill_id:
        return ToolResult(output="skill_id is required", is_error=True)

    async with session_factory() as session:
        row = await get_skill_scoped(session, skill_id, owner_email=user_email)
        if row is None and not skill_id.startswith("skill_"):
            candidates = [
                skill
                for skill in await list_skills(session, owner_email=user_email)
                if skill.name == skill_id or skill.skill_id == skill_id
            ]
            if len(candidates) == 1:
                row = candidates[0]
        if row is None:
            return ToolResult(output=f"Skill '{skill_id}' not found", is_error=True)

        version_row = await resolve_current_skill_version(session, row)
        instructions = version_row.instructions if version_row is not None else row.instructions
        tools = version_row.tools if version_row is not None else row.tools
        templates = (
            version_row.prompt_templates if version_row is not None else row.prompt_templates
        )
        asset_refs = (
            await load_skill_asset_refs(session, version_row) if version_row is not None else []
        )
        steps = getattr(version_row, "steps", None) if version_row is not None else None

    result, metadata = materialize_loaded_skill_context(
        skill_id=row.skill_id,
        name=row.name,
        description=row.description,
        instructions=instructions,
        tools=tools,
        templates=templates,
        asset_refs=asset_refs,
        steps=steps,
        tags=row.tags or [],
        linked_tool_ids=row.linked_tool_ids,
        attach_to_all_agents=row.auto_load,
        version_id=version_row.version_id if version_row is not None else None,
        version_number=version_row.version_number if version_row is not None else None,
        content_hash=version_row.content_hash if version_row is not None else None,
    )
    declared_tool_ids = metadata.get("discovered_tool_ids", [])
    skill_activation = metadata.get("skill_activation")
    if isinstance(skill_activation, dict):
        skill_activation["content_hash"] = (
            version_row.content_hash
            if version_row is not None and isinstance(version_row.content_hash, str)
            else ""
        )
    logger.info(
        "skill loaded",
        extra={
            "extra_data": {
                "skill_id": row.skill_id,
                "name": row.name,
                "declared_tool_count": len(declared_tool_ids),
                "declared_tool_ids": declared_tool_ids,
                "requires_classifier": not declared_tool_ids,
                "step_count": len(steps or []),
                "template_count": len(templates or {}),
                "asset_count": len(asset_refs),
                "content_hash": (
                    version_row.content_hash
                    if version_row is not None and isinstance(version_row.content_hash, str)
                    else ""
                )[:16],
            }
        },
    )

    return ToolResult(
        output=json.dumps(result, indent=2, default=str),
        metadata=metadata,
    )


def _skill_tool_runtime_summaries(
    skill_id: str,
    tools: list[dict[str, Any]] | dict[str, Any] | None,
) -> list[dict[str, Any]]:
    """Return LLM-facing runtime names for skill-defined tools."""

    if not tools:
        return []
    raw_items = list(tools.values()) if isinstance(tools, dict) else tools
    if not isinstance(raw_items, list):
        return []
    summaries: list[dict[str, Any]] = []
    for raw in raw_items:
        if not isinstance(raw, dict):
            continue
        raw_name = str(raw.get("name") or "").strip()
        if not raw_name:
            continue
        recipe = raw.get("recipe") if isinstance(raw.get("recipe"), dict) else {}
        summary: dict[str, Any] = {
            "name": raw_name,
            "callable_name": _qualified_skill_tool_name(skill_id, raw_name),
            "stable_tool_id": f"skill:{skill_id}:{raw_name}",
            "description": raw.get("description") or "",
            "parameters": raw.get("parameters") or {"type": "object", "properties": {}},
        }
        if recipe:
            summary["recipe"] = {
                "mode": recipe.get("mode"),
                "entry": recipe.get("entry"),
                "required_assets": recipe.get("required_assets") or [],
            }
        summaries.append(summary)
    return summaries


def _skill_asset_llm_manifest(asset_refs: list[Any]) -> list[dict[str, Any]]:
    """Return asset metadata safe to expose to the LLM."""

    manifest: list[dict[str, Any]] = []
    for ref in asset_refs:
        raw = ref.model_dump(mode="json", exclude_none=True) if hasattr(ref, "model_dump") else ref
        if not isinstance(raw, dict):
            continue
        manifest.append(
            {
                "filename": raw.get("filename"),
                "asset_id": raw.get("asset_id"),
                "content_hash": raw.get("content_hash"),
                "size_bytes": raw.get("size_bytes", 0),
                "content_type": raw.get("content_type", "application/octet-stream"),
            }
        )
    return manifest


async def _handle_skill_get(
    session_factory: Any, user_email: str, arguments: dict[str, Any]
) -> ToolResult:
    import json

    from cognis.store.queries import get_skill_scoped, list_skill_versions

    skill_id = str(arguments.get("skill_id", "")).strip()
    if not skill_id:
        return ToolResult(output="skill_id is required", is_error=True)

    async with session_factory() as session:
        row = await get_skill_scoped(session, skill_id, owner_email=user_email)
        if row is None:
            return ToolResult(output=f"Skill '{skill_id}' not found", is_error=True)

        version_data = None
        current_version = await resolve_current_skill_version(session, row)
        if current_version is not None:
            asset_refs = await load_skill_asset_refs(session, current_version)
            version_data = {
                "version_id": current_version.version_id,
                "version_number": current_version.version_number,
                "content_hash": current_version.content_hash,
                "instructions": current_version.instructions,
                "tools": current_version.tools,
                "linked_tool_ids": getattr(current_version, "linked_tool_ids", None) or [],
                "steps": getattr(current_version, "steps", None),
                "prompt_templates": current_version.prompt_templates,
                "secret_placeholders": current_version.secret_placeholders,
                "decomposition_source_hash": getattr(
                    current_version, "decomposition_source_hash", None
                ),
                "source_url": current_version.source_url,
                "resolved_url": current_version.resolved_url,
                "import_checksum": current_version.import_checksum,
                "import_format": current_version.import_format,
                "asset_manifest": [ref.model_dump(mode="json") for ref in asset_refs],
            }
        versions = await list_skill_versions(session, skill_id)

    result = {
        "skill_id": row.skill_id,
        "name": row.name,
        "description": row.description,
        "tags": row.tags or [],
        "attach_to_all_agents": row.auto_load,
        "auto_load": row.auto_load,
        "linked_tool_ids": row.linked_tool_ids or [],
        "source": row.source,
        "current_version": version_data,
        "version_count": len(versions),
    }
    return ToolResult(output=json.dumps(result, indent=2, default=str))


async def _handle_skill_versions(
    session_factory: Any, user_email: str, arguments: dict[str, Any]
) -> ToolResult:
    import json

    from cognis.store.queries import get_skill_scoped, list_skill_versions

    skill_id = str(arguments.get("skill_id", "")).strip()
    if not skill_id:
        return ToolResult(output="skill_id is required", is_error=True)

    async with session_factory() as session:
        row = await get_skill_scoped(session, skill_id, owner_email=user_email)
        if row is None:
            return ToolResult(output=f"Skill '{skill_id}' not found", is_error=True)
        versions = await list_skill_versions(session, skill_id)
        payload = []
        for version in versions:
            refs = await load_skill_asset_refs(session, version)
            payload.append(
                {
                    "version_id": version.version_id,
                    "version_number": version.version_number,
                    "content_hash": version.content_hash,
                    "created_at": version.created_at,
                    "import_format": version.import_format,
                    "source_url": version.source_url,
                    "asset_count": len(refs),
                }
            )
    return ToolResult(output=json.dumps(payload, indent=2, default=str))


async def _handle_skill_write(
    session_factory: Any,
    user_email: str,
    arguments: dict[str, Any],
    *,
    llm: Any | None,
    artifact_store: Any | None,
    current_agent_id: str | None = None,
) -> ToolResult:
    import json

    from cognis.store.queries import (
        create_skill,
        get_next_version_number,
        get_skill_scoped,
        set_current_version,
        update_skill,
    )

    if artifact_store is None:
        return ToolResult(output="Skill management requires artifact support.", is_error=True)

    skill_id = arguments.get("skill_id")
    name = str(arguments.get("name", "")).strip()
    instructions = str(arguments.get("instructions", "")).strip()

    if not name:
        return ToolResult(output="name is required", is_error=True)
    if not instructions:
        return ToolResult(output="instructions is required", is_error=True)

    try:
        tools = normalize_skill_tools(arguments.get("tools"))
        linked_tool_ids = normalize_linked_tool_ids(arguments.get("linked_tool_ids"))
        templates = normalize_prompt_templates(arguments.get("prompt_templates"))
        secret_placeholders = normalize_secret_placeholders(arguments.get("secret_placeholders"))
        steps = normalize_skill_steps(arguments.get("steps"))
    except ValueError as exc:
        return ToolResult(output=str(exc), is_error=True)
    tags = arguments.get("tags")
    attach_to_all_agents = _resolve_attach_to_all_agents(arguments)
    description = arguments.get("description")
    decomposition_source_hash = arguments.get("decomposition_source_hash")
    if decomposition_source_hash is not None:
        decomposition_source_hash = str(decomposition_source_hash).strip() or None

    async with session_factory() as session:
        created_new_skill = False
        assets = None
        current_version = None
        if skill_id:
            # Update existing
            row = await get_skill_scoped(session, skill_id, owner_email=user_email)
            if row is None:
                return ToolResult(output=f"Skill '{skill_id}' not found", is_error=True)
            if linked_tool_ids is None:
                linked_tool_ids = row.linked_tool_ids or []
            current_version = await resolve_current_skill_version(session, row)
            current_assets = (
                await load_skill_asset_refs(session, current_version)
                if current_version is not None
                else []
            )
            assets = asset_refs_to_inputs(current_assets)
            current_asset_inputs = _canonical_asset_inputs(list(assets))
            current_source_hash = compute_decomposition_source_hash(
                instructions,
                tools=tools,
                linked_tool_ids=linked_tool_ids,
                prompt_templates=templates,
                secret_placeholders=secret_placeholders,
                asset_manifest=getattr(current_version, "asset_manifest", None) or [],
            )
            if (
                steps is not None
                and decomposition_source_hash is not None
                and decomposition_source_hash != current_source_hash
            ):
                return ToolResult(
                    output=(
                        "steps do not match the current skill content; decomposition_source_hash is stale. "
                        "Refresh the decomposition before saving."
                    ),
                    is_error=True,
                )
            await update_skill(
                session,
                skill_id,
                owner_email=user_email,
                name=name,
                description=description,
                instructions=instructions,
                tools=tools,
                linked_tool_ids=linked_tool_ids,
                prompt_templates=templates,
                tags=tags,
                auto_load=attach_to_all_agents,
            )
            next_num = await get_next_version_number(session, skill_id)
            if steps is None:
                steps, refresh_error = await _refresh_skill_steps_if_needed(
                    llm=llm,
                    skill_id=skill_id,
                    skill_name=name,
                    description=description if isinstance(description, str) else row.description,
                    instructions=instructions,
                    tools=tools,
                    linked_tool_ids=linked_tool_ids,
                    prompt_templates=templates,
                    secret_placeholders=secret_placeholders,
                    asset_inputs=current_asset_inputs,
                    current_instructions=current_version.instructions
                    if current_version is not None
                    else row.instructions,
                    current_tools=current_version.tools
                    if current_version is not None
                    else row.tools,
                    current_linked_tool_ids=(
                        getattr(current_version, "linked_tool_ids", None)
                        if current_version is not None
                        else row.linked_tool_ids or []
                    ),
                    current_templates=(
                        current_version.prompt_templates
                        if current_version is not None
                        else row.prompt_templates
                    ),
                    current_placeholders=(
                        current_version.secret_placeholders if current_version is not None else None
                    ),
                    current_steps=getattr(current_version, "steps", None)
                    if current_version is not None
                    else None,
                    current_asset_inputs=current_asset_inputs,
                )
                if refresh_error is not None:
                    return ToolResult(output=refresh_error, is_error=True)
        else:
            # Create new
            created_new_skill = True
            linked_tool_ids = linked_tool_ids or []
            row = await create_skill(
                session,
                name=name,
                description=description,
                instructions=instructions,
                tools=tools,
                linked_tool_ids=linked_tool_ids,
                prompt_templates=templates,
                tags=tags,
                auto_load=attach_to_all_agents,
                owner_email=user_email,
            )
            skill_id = row.skill_id
            next_num = 1
            assets = []
            current_source_hash = compute_decomposition_source_hash(
                instructions,
                tools=tools,
                linked_tool_ids=linked_tool_ids,
                prompt_templates=templates,
                secret_placeholders=secret_placeholders,
                asset_manifest=[],
            )
            if (
                steps is not None
                and decomposition_source_hash is not None
                and decomposition_source_hash != current_source_hash
            ):
                return ToolResult(
                    output=(
                        "steps do not match the current skill content; decomposition_source_hash is stale. "
                        "Refresh the decomposition before saving."
                    ),
                    is_error=True,
                )

        try:
            version_row = await create_skill_version_with_assets(
                session,
                artifact_store,
                skill_id=skill_id,
                version_number=next_num,
                owner_email=user_email,
                instructions=instructions,
                tools=tools,
                linked_tool_ids=linked_tool_ids,
                prompt_templates=templates,
                secret_placeholders=secret_placeholders,
                steps=steps
                if steps is not None
                else (
                    getattr(current_version, "steps", None) if current_version is not None else None
                ),
                assets=assets,
                allow_binary_assets=False,
                source_url=current_version.source_url if current_version is not None else None,
                resolved_url=current_version.resolved_url if current_version is not None else None,
                commit_sha=current_version.commit_sha if current_version is not None else None,
                import_checksum=current_version.import_checksum
                if current_version is not None
                else None,
                imported_at=current_version.imported_at if current_version is not None else None,
                import_format=current_version.import_format
                if current_version is not None
                else None,
            )
        except ValueError as exc:
            return ToolResult(output=str(exc), is_error=True)
        await set_current_version(session, skill_id, version_row.version_id)
        await session.commit()

    metadata: dict[str, Any] = {}
    if current_agent_id:
        # Keep created and updated skills attached to the agent that performed the mutation.
        await _auto_bind_skill_to_agent(session_factory, skill_id, agent_id=current_agent_id)
        metadata["attached_skill_id"] = skill_id

    if created_new_skill or current_agent_id:
        metadata["attached_skill_id"] = skill_id
        metadata["discovered_tool_ids"] = sorted(
            _resolved_skill_tool_ids(
                skill_id,
                name,
                description if isinstance(description, str) else None,
                attach_to_all_agents,
                instructions,
                tools,
                linked_tool_ids,
            )
        )

    result = {
        "skill_id": skill_id,
        "name": name,
        "version_id": version_row.version_id,
        "version_number": next_num,
        "content_hash": version_row.content_hash,
        "step_count": len(
            steps if steps is not None else getattr(version_row, "steps", None) or []
        ),
    }
    return ToolResult(output=json.dumps(result, indent=2), metadata=metadata or None)


async def _handle_skill_patch(
    session_factory: Any,
    user_email: str,
    arguments: dict[str, Any],
    *,
    artifact_store: Any | None,
) -> ToolResult:
    """Apply an explicit partial patch to the current immutable skill version."""

    import json

    from cognis.store.queries import (
        get_next_version_number,
        get_skill_scoped,
        set_current_version_if_matches,
        update_skill,
    )

    if artifact_store is None:
        return ToolResult(output="Skill management requires artifact support.", is_error=True)
    skill_id = str(arguments.get("skill_id", "")).strip()
    if not skill_id:
        return ToolResult(output="skill_id is required", is_error=True)
    mutation_keys = set(arguments) - {
        "skill_id",
        "expected_current_version_id",
        "expected_content_hash",
    }
    if not mutation_keys:
        return ToolResult(output="No patch operations provided", is_error=True)

    async with session_factory() as session:
        row = await get_skill_scoped(session, skill_id, owner_email=user_email)
        if row is None:
            return ToolResult(output=f"Skill '{skill_id}' not found", is_error=True)
        if row.is_system:
            return ToolResult(output="Cannot modify system skills directly", is_error=True)
        current = await resolve_current_skill_version(session, row)
        if current is None:
            return ToolResult(
                output="version_conflict: skill has no current version", is_error=True
            )
        if (
            arguments.get("expected_current_version_id") is not None
            and arguments["expected_current_version_id"] != current.version_id
        ) or (
            arguments.get("expected_content_hash") is not None
            and arguments["expected_content_hash"] != current.content_hash
        ):
            return ToolResult(
                output="version_conflict: skill changed since it was read; reload and retry",
                is_error=True,
            )

        current_assets = await load_skill_asset_refs(session, current)
        try:
            instructions = (
                str(arguments["instructions"])
                if "instructions" in arguments
                else current.instructions
            )
            tools = current.tools
            if "tools" in arguments:
                tools = normalize_skill_tools(
                    apply_named_list_patch(tools, arguments["tools"] or {}, key="name")
                )
            templates = current.prompt_templates or {}
            if "prompt_templates" in arguments:
                templates = normalize_prompt_templates(
                    apply_mapping_patch(templates, arguments["prompt_templates"] or {})
                )
            steps = list(current.steps or [])
            if "steps" in arguments:
                steps = (
                    normalize_skill_steps(
                        apply_named_list_patch(steps, arguments["steps"] or {}, key="name")
                    )
                    or []
                )
            linked_tool_ids = (
                normalize_linked_tool_ids(arguments.get("linked_tool_ids"))
                if "linked_tool_ids" in arguments
                else current.linked_tool_ids or []
            )
            placeholders = (
                normalize_secret_placeholders(arguments.get("secret_placeholders"))
                if "secret_placeholders" in arguments
                else current.secret_placeholders
            )
            asset_inputs = asset_refs_to_inputs(current_assets)
            if "assets" in arguments:
                asset_inputs = apply_named_list_patch(
                    asset_inputs, arguments["assets"] or {}, key="filename"
                )
        except (TypeError, ValueError) as exc:
            return ToolResult(output=str(exc), is_error=True)

        metadata_updates: dict[str, Any] = {}
        for key in ("name", "description", "tags"):
            if key in arguments:
                metadata_updates[key] = arguments[key]
        if "attach_to_all_agents" in arguments:
            metadata_updates["auto_load"] = bool(arguments["attach_to_all_agents"])
        if "linked_tool_ids" in arguments:
            metadata_updates["linked_tool_ids"] = linked_tool_ids
        content_changed = any(
            [
                instructions != current.instructions,
                (tools or []) != (current.tools or []),
                (templates or {}) != (current.prompt_templates or {}),
                (steps or []) != (current.steps or []),
                (linked_tool_ids or []) != (current.linked_tool_ids or []),
                (placeholders or []) != (current.secret_placeholders or []),
                _canonical_asset_inputs(asset_inputs)
                != _canonical_asset_inputs(asset_refs_to_inputs(current_assets)),
            ]
        )
        metadata_changed = any(
            getattr(row, key) != value for key, value in metadata_updates.items()
        )
        if not content_changed and not metadata_changed:
            return ToolResult(output="no_op_patch: patch does not change the skill", is_error=True)
        if content_changed:
            metadata_updates.update(
                instructions=instructions, tools=tools, prompt_templates=templates
            )
        try:
            updated = await update_skill(
                session, skill_id, owner_email=user_email, **metadata_updates
            )
            assert updated is not None
            version = current
            if content_changed:
                version = await create_skill_version_with_assets(
                    session,
                    artifact_store,
                    skill_id=skill_id,
                    version_number=await get_next_version_number(session, skill_id),
                    owner_email=user_email,
                    instructions=instructions,
                    tools=tools,
                    linked_tool_ids=linked_tool_ids,
                    prompt_templates=templates,
                    secret_placeholders=placeholders,
                    steps=steps,
                    assets=asset_inputs,
                    allow_binary_assets=False,
                    source_url=current.source_url,
                    resolved_url=current.resolved_url,
                    commit_sha=current.commit_sha,
                    import_checksum=current.import_checksum,
                    imported_at=current.imported_at,
                    import_format=current.import_format,
                    decomposition_source_hash=(
                        None if "steps" in arguments else current.decomposition_source_hash
                    ),
                )
                if not await set_current_version_if_matches(
                    session,
                    skill_id,
                    expected_version_id=current.version_id,
                    version_id=version.version_id,
                ):
                    await session.rollback()
                    await cleanup_unpublished_skill_assets(
                        artifact_store,
                        version,
                    )
                    return ToolResult(
                        output="version_conflict: skill changed while patching; reload and retry",
                        is_error=True,
                    )
            elif not await set_current_version_if_matches(
                session,
                skill_id,
                expected_version_id=current.version_id,
                version_id=current.version_id,
            ):
                await session.rollback()
                return ToolResult(
                    output="version_conflict: skill changed while patching; reload and retry",
                    is_error=True,
                )
            await session.commit()
        except ValueError as exc:
            await session.rollback()
            return ToolResult(output=str(exc), is_error=True)

    return ToolResult(
        output=json.dumps(
            {
                "skill_id": skill_id,
                "version_id": version.version_id,
                "version_number": version.version_number,
                "content_hash": version.content_hash,
                "version_created": content_changed,
            },
            indent=2,
        )
    )


async def _handle_skill_asset_write(
    session_factory: Any,
    user_email: str,
    arguments: dict[str, Any],
    *,
    llm: Any | None,
    artifact_store: Any | None,
) -> ToolResult:
    import json

    from cognis.store.queries import (
        get_next_version_number,
        get_skill_scoped,
        set_current_version,
        update_skill,
    )

    if artifact_store is None:
        return ToolResult(output="Skill management requires artifact support.", is_error=True)

    skill_id = str(arguments.get("skill_id", "")).strip()
    filename = str(arguments.get("filename", "")).strip()
    if not skill_id or not filename:
        return ToolResult(output="skill_id and filename are required", is_error=True)

    asset_payload = {
        "filename": filename,
        "content": arguments.get("content"),
        "source_artifact_id": arguments.get("source_artifact_id"),
        "content_type": arguments.get("content_type"),
    }

    async with session_factory() as session:
        row = await get_skill_scoped(session, skill_id, owner_email=user_email)
        if row is None:
            return ToolResult(output=f"Skill '{skill_id}' not found", is_error=True)
        current_version = await resolve_current_skill_version(session, row)
        current_assets = (
            await load_skill_asset_refs(session, current_version)
            if current_version is not None
            else []
        )
        current_asset_inputs = _canonical_asset_inputs(asset_refs_to_inputs(current_assets))
        retained_assets = [item for item in current_assets if item.filename != filename]
        asset_inputs = [*asset_refs_to_inputs(retained_assets), asset_payload]
        instructions = (
            current_version.instructions if current_version is not None else row.instructions
        )
        tools = current_version.tools if current_version is not None else row.tools
        templates = (
            current_version.prompt_templates
            if current_version is not None
            else row.prompt_templates
        )
        placeholders = current_version.secret_placeholders if current_version is not None else None
        steps = getattr(current_version, "steps", None) if current_version is not None else None
        if steps is not None:
            steps, refresh_error = await _refresh_skill_steps_if_needed(
                llm=llm,
                skill_id=skill_id,
                skill_name=row.name,
                description=row.description,
                instructions=instructions,
                tools=tools,
                linked_tool_ids=row.linked_tool_ids or [],
                prompt_templates=templates,
                secret_placeholders=placeholders,
                asset_inputs=_canonical_asset_inputs(asset_inputs),
                current_instructions=instructions,
                current_tools=tools,
                current_linked_tool_ids=(
                    getattr(current_version, "linked_tool_ids", None)
                    if current_version is not None
                    else row.linked_tool_ids or []
                ),
                current_templates=templates,
                current_placeholders=placeholders,
                current_steps=steps,
                current_asset_inputs=current_asset_inputs,
            )
            if refresh_error is not None:
                return ToolResult(output=refresh_error, is_error=True)
        try:
            version_row = await create_skill_version_with_assets(
                session,
                artifact_store,
                skill_id=skill_id,
                version_number=await get_next_version_number(session, skill_id),
                owner_email=user_email,
                instructions=instructions,
                tools=tools,
                linked_tool_ids=row.linked_tool_ids or [],
                prompt_templates=templates,
                secret_placeholders=placeholders,
                steps=steps,
                assets=asset_inputs,
                allow_binary_assets=False,
                source_url=current_version.source_url if current_version is not None else None,
                resolved_url=current_version.resolved_url if current_version is not None else None,
                commit_sha=current_version.commit_sha if current_version is not None else None,
                import_checksum=current_version.import_checksum
                if current_version is not None
                else None,
                imported_at=current_version.imported_at if current_version is not None else None,
                import_format=current_version.import_format
                if current_version is not None
                else None,
            )
        except ValueError as exc:
            return ToolResult(output=str(exc), is_error=True)
        await update_skill(
            session,
            skill_id,
            owner_email=user_email,
            instructions=instructions,
            tools=tools,
            linked_tool_ids=row.linked_tool_ids or [],
            prompt_templates=templates,
        )
        await set_current_version(session, skill_id, version_row.version_id)
        await session.commit()

    return ToolResult(
        output=json.dumps(
            {
                "skill_id": skill_id,
                "filename": filename,
                "version_id": version_row.version_id,
                "version_number": version_row.version_number,
                "content_hash": version_row.content_hash,
            },
            indent=2,
        )
    )


async def _handle_skill_asset_delete(
    session_factory: Any,
    user_email: str,
    arguments: dict[str, Any],
    *,
    llm: Any | None,
    artifact_store: Any | None,
) -> ToolResult:
    import json

    from cognis.store.queries import (
        get_next_version_number,
        get_skill_scoped,
        set_current_version,
        update_skill,
    )

    if artifact_store is None:
        return ToolResult(output="Skill management requires artifact support.", is_error=True)

    skill_id = str(arguments.get("skill_id", "")).strip()
    filename = str(arguments.get("filename", "")).strip()
    if not skill_id or not filename:
        return ToolResult(output="skill_id and filename are required", is_error=True)

    async with session_factory() as session:
        row = await get_skill_scoped(session, skill_id, owner_email=user_email)
        if row is None:
            return ToolResult(output=f"Skill '{skill_id}' not found", is_error=True)
        current_version = await resolve_current_skill_version(session, row)
        current_assets = (
            await load_skill_asset_refs(session, current_version)
            if current_version is not None
            else []
        )
        current_asset_inputs = _canonical_asset_inputs(asset_refs_to_inputs(current_assets))
        retained_assets = [item for item in current_assets if item.filename != filename]
        if len(retained_assets) == len(current_assets):
            return ToolResult(
                output=f"Asset '{filename}' not found on skill '{skill_id}'", is_error=True
            )
        instructions = (
            current_version.instructions if current_version is not None else row.instructions
        )
        tools = current_version.tools if current_version is not None else row.tools
        templates = (
            current_version.prompt_templates
            if current_version is not None
            else row.prompt_templates
        )
        placeholders = current_version.secret_placeholders if current_version is not None else None
        steps = getattr(current_version, "steps", None) if current_version is not None else None
        if steps is not None:
            retained_asset_inputs = _canonical_asset_inputs(asset_refs_to_inputs(retained_assets))
            steps, refresh_error = await _refresh_skill_steps_if_needed(
                llm=llm,
                skill_id=skill_id,
                skill_name=row.name,
                description=row.description,
                instructions=instructions,
                tools=tools,
                linked_tool_ids=row.linked_tool_ids or [],
                prompt_templates=templates,
                secret_placeholders=placeholders,
                asset_inputs=retained_asset_inputs,
                current_instructions=instructions,
                current_tools=tools,
                current_linked_tool_ids=(
                    getattr(current_version, "linked_tool_ids", None)
                    if current_version is not None
                    else row.linked_tool_ids or []
                ),
                current_templates=templates,
                current_placeholders=placeholders,
                current_steps=steps,
                current_asset_inputs=current_asset_inputs,
            )
            if refresh_error is not None:
                return ToolResult(output=refresh_error, is_error=True)
        version_row = await create_skill_version_with_assets(
            session,
            artifact_store,
            skill_id=skill_id,
            version_number=await get_next_version_number(session, skill_id),
            owner_email=user_email,
            instructions=instructions,
            tools=tools,
            prompt_templates=templates,
            secret_placeholders=placeholders,
            steps=steps,
            assets=asset_refs_to_inputs(retained_assets),
            allow_binary_assets=False,
            source_url=current_version.source_url if current_version is not None else None,
            resolved_url=current_version.resolved_url if current_version is not None else None,
            commit_sha=current_version.commit_sha if current_version is not None else None,
            import_checksum=current_version.import_checksum
            if current_version is not None
            else None,
            imported_at=current_version.imported_at if current_version is not None else None,
            import_format=current_version.import_format if current_version is not None else None,
        )
        await update_skill(
            session,
            skill_id,
            owner_email=user_email,
            instructions=instructions,
            tools=tools,
            prompt_templates=templates,
        )
        await set_current_version(session, skill_id, version_row.version_id)
        await session.commit()

    return ToolResult(
        output=json.dumps(
            {
                "skill_id": skill_id,
                "removed_filename": filename,
                "version_id": version_row.version_id,
                "version_number": version_row.version_number,
                "content_hash": version_row.content_hash,
            },
            indent=2,
        )
    )


async def _handle_skill_delete(
    session_factory: Any, user_email: str, arguments: dict[str, Any]
) -> ToolResult:
    from cognis.store.queries import delete_skill, get_skill_scoped, get_skill_version

    skill_id = str(arguments.get("skill_id", "")).strip()
    if not skill_id:
        return ToolResult(output="skill_id is required", is_error=True)

    async with session_factory() as session:
        row = await get_skill_scoped(session, skill_id, owner_email=user_email)
        if row is None:
            return ToolResult(output=f"Skill '{skill_id}' not found", is_error=True)
        instructions = row.instructions
        tools = row.tools
        if row.current_version_id:
            version_row = await get_skill_version(session, row.current_version_id)
            if version_row is not None:
                instructions = version_row.instructions
                tools = version_row.tools
        removed_tool_ids = sorted(
            _resolved_skill_tool_ids(
                row.skill_id,
                row.name,
                row.description,
                row.auto_load,
                instructions,
                tools,
                row.linked_tool_ids,
            )
        )
        try:
            deleted = await delete_skill(session, skill_id, owner_email=user_email)
        except ValueError as exc:
            return ToolResult(output=str(exc), is_error=True)
        if not deleted:
            return ToolResult(output=f"Skill '{skill_id}' not found", is_error=True)
        await session.commit()

    return ToolResult(
        output=f"Skill '{skill_id}' deleted successfully.",
        metadata={"deleted_skill_id": skill_id, "removed_tool_ids": removed_tool_ids},
    )


async def _handle_skill_import_url(
    session_factory: Any,
    user_email: str,
    arguments: dict[str, Any],
    *,
    artifact_store: Any | None,
) -> ToolResult:
    import json

    from cognis.store.queries import create_skill, set_current_version
    from cognis.tools.skill_import import import_skill_from_url

    if artifact_store is None:
        return ToolResult(output="Skill management requires artifact support.", is_error=True)

    url = str(arguments.get("url", "")).strip()
    if not url:
        return ToolResult(output="url is required", is_error=True)

    try:
        skill_data, provenance = await import_skill_from_url(url)
    except ValueError as exc:
        return ToolResult(output=f"Import failed: {exc}", is_error=True)

    try:
        name = arguments.get("name") or skill_data.get("name") or "Imported Skill"
        instructions = str(skill_data.get("instructions") or "")
        tools = normalize_skill_tools(skill_data.get("tools"))
        linked_tool_ids = (
            normalize_linked_tool_ids(
                arguments.get("linked_tool_ids")
                if arguments.get("linked_tool_ids") is not None
                else skill_data.get("linked_tool_ids")
            )
            or []
        )
        templates = normalize_prompt_templates(skill_data.get("prompt_templates"))
        placeholders = normalize_secret_placeholders(skill_data.get("secret_placeholders"))
        steps = skill_data.get("steps") if isinstance(skill_data.get("steps"), list) else None
        tags = arguments.get("tags") or skill_data.get("tags") or []
        attach_to_all_agents = _resolve_attach_to_all_agents(arguments)
    except ValueError as exc:
        return ToolResult(output=str(exc), is_error=True)

    async with session_factory() as session:
        row = await create_skill(
            session,
            name=name,
            description=skill_data.get("description"),
            instructions=instructions,
            tools=tools,
            linked_tool_ids=linked_tool_ids,
            prompt_templates=templates,
            tags=tags,
            auto_load=attach_to_all_agents,
            source="imported",
            owner_email=user_email,
        )
        try:
            version_row = await create_skill_version_with_assets(
                session,
                artifact_store,
                skill_id=row.skill_id,
                version_number=1,
                owner_email=user_email,
                instructions=instructions,
                tools=tools,
                linked_tool_ids=linked_tool_ids,
                prompt_templates=templates,
                secret_placeholders=placeholders,
                steps=steps,
                assets=skill_data.get("assets")
                if isinstance(skill_data.get("assets"), list)
                else None,
                allow_binary_assets=False,
                source_url=provenance.source_url,
                resolved_url=provenance.resolved_url,
                commit_sha=provenance.commit_sha,
                import_checksum=provenance.import_checksum,
                imported_at=provenance.imported_at,
                import_format=provenance.import_format,
            )
        except ValueError as exc:
            return ToolResult(output=str(exc), is_error=True)
        await set_current_version(session, row.skill_id, version_row.version_id)
        await session.commit()

    # Auto-bind the imported skill to the current agent
    await _auto_bind_skill_to_agent(session_factory, row.skill_id)

    result = {
        "skill_id": row.skill_id,
        "name": name,
        "version_id": version_row.version_id,
        "source_url": provenance.source_url,
        "import_format": provenance.import_format,
    }
    return ToolResult(
        output=json.dumps(result, indent=2),
        metadata={
            "attached_skill_id": row.skill_id,
            "discovered_tool_ids": sorted(
                _resolved_skill_tool_ids(
                    row.skill_id,
                    str(name),
                    skill_data.get("description"),
                    attach_to_all_agents,
                    str(instructions),
                    tools,
                    linked_tool_ids,
                )
            ),
        },
    )


async def _handle_skill_export(
    session_factory: Any,
    user_email: str,
    arguments: dict[str, Any],
    *,
    artifact_store: Any | None,
) -> ToolResult:
    from cognis.models.skill import SkillExportData
    from cognis.store.queries import get_skill_scoped
    from cognis.tools.skill_parser import export_cognis_yaml, export_skill_md

    if artifact_store is None:
        return ToolResult(output="Skill management requires artifact support.", is_error=True)

    skill_id = str(arguments.get("skill_id", "")).strip()
    if not skill_id:
        return ToolResult(output="skill_id is required", is_error=True)

    fmt = str(arguments.get("format", "skill_md")).strip()
    if fmt not in {"skill_md", "cognis_yaml", "cognis_package"}:
        return ToolResult(output="Unsupported export format", is_error=True)

    async with session_factory() as session:
        row = await get_skill_scoped(session, skill_id, owner_email=user_email)
        if row is None:
            return ToolResult(output=f"Skill '{skill_id}' not found", is_error=True)

        version_row = await resolve_current_skill_version(session, row)
        asset_manifest = []
        asset_bytes: dict[str, bytes] = {}
        if version_row is not None:
            asset_manifest, asset_bytes = await load_export_assets(
                session, artifact_store, version_row
            )

    provenance = None
    if version_row and version_row.source_url:
        provenance = ImportProvenance(
            source_url=version_row.source_url,
            resolved_url=version_row.resolved_url,
            commit_sha=version_row.commit_sha,
            import_checksum=version_row.import_checksum,
            imported_at=version_row.imported_at,
            import_format=version_row.import_format,
        )

    export_data = SkillExportData(
        name=row.name,
        description=row.description,
        tags=row.tags or [],
        linked_tool_ids=row.linked_tool_ids or [],
        auto_load=row.auto_load,
        instructions=version_row.instructions if version_row else row.instructions,
        tools=version_row.tools or [] if version_row else row.tools or [],
        prompt_templates=version_row.prompt_templates or {}
        if version_row
        else row.prompt_templates or {},
        secret_placeholders=version_row.secret_placeholders or [] if version_row else [],
        provenance=provenance,
        asset_manifest=asset_manifest,
    )

    if fmt == "cognis_package":
        payload = export_cognis_package(export_data, asset_bytes)
        return ToolResult(output=base64.b64encode(payload).decode("ascii"))
    if fmt == "cognis_yaml":
        content = export_cognis_yaml(export_data)
    else:
        content = export_skill_md(export_data)

    return ToolResult(output=content)


async def _handle_skill_restore_version(
    session_factory: Any, user_email: str, arguments: dict[str, Any]
) -> ToolResult:
    import json

    from cognis.store.queries import (
        get_skill_scoped,
        list_skill_versions,
        set_current_version,
        update_skill,
    )

    skill_id = str(arguments.get("skill_id", "")).strip()
    version_id = str(arguments.get("version_id", "")).strip()
    if not skill_id or not version_id:
        return ToolResult(output="skill_id and version_id are required", is_error=True)

    async with session_factory() as session:
        row = await get_skill_scoped(session, skill_id, owner_email=user_email)
        if row is None:
            return ToolResult(output=f"Skill '{skill_id}' not found", is_error=True)
        if row.is_system:
            return ToolResult(output="System skills cannot be restored", is_error=True)
        versions = {item.version_id: item for item in await list_skill_versions(session, skill_id)}
        version_row = versions.get(version_id)
        if version_row is None:
            return ToolResult(output=f"Version '{version_id}' not found", is_error=True)
        await update_skill(
            session,
            skill_id,
            owner_email=user_email,
            instructions=version_row.instructions,
            tools=version_row.tools,
            prompt_templates=version_row.prompt_templates,
        )
        await set_current_version(session, skill_id, version_id)
        await session.commit()

    return ToolResult(
        output=json.dumps(
            {
                "skill_id": skill_id,
                "version_id": version_id,
                "restored": True,
            },
            indent=2,
        )
    )
