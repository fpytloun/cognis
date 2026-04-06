"""Built-in LLM tools for skill management.

These tools allow agents to list, inspect, create, update, delete,
import, and export skills through the normal tool call flow.
All mutation operations are non-bypassable (Intaris evaluates them).
"""

from __future__ import annotations

from typing import Any

from cognis.logging import get_logger
from cognis.models.tool import ToolDefinition, ToolResult, ToolSource

logger = get_logger(__name__)

_SKILL_SOURCE = ToolSource(type="builtin")


# ---------------------------------------------------------------------------
# Tool definitions
# ---------------------------------------------------------------------------


def skill_management_tools() -> list[ToolDefinition]:
    """Return all skill management tool definitions."""
    return [
        SKILL_LIST_TOOL,
        SKILL_GET_TOOL,
        SKILL_WRITE_TOOL,
        SKILL_DELETE_TOOL,
        SKILL_IMPORT_URL_TOOL,
        SKILL_EXPORT_TOOL,
    ]


SKILL_LIST_TOOL = ToolDefinition(
    name="skill_list",
    description="List all available skills. Returns skill names, descriptions, tags, and version info.",
    parameters={"type": "object", "properties": {}},
    source=_SKILL_SOURCE,
    category="skill",
    read_only=True,
    timeout_seconds=15,
)

SKILL_GET_TOOL = ToolDefinition(
    name="skill_get",
    description="Get detailed information about a skill including instructions, tools, templates, and version history.",
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

SKILL_WRITE_TOOL = ToolDefinition(
    name="skill_write",
    description=(
        "Create or update a skill. Provide skill_id to update an existing skill, "
        "or omit it to create a new one. Creates a new immutable version on each write."
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
                "description": "Tool definitions (optional)",
            },
            "prompt_templates": {
                "type": "object",
                "description": "Prompt templates (optional)",
            },
            "auto_load": {
                "type": "boolean",
                "description": "Auto-load for all agents (default false)",
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

SKILL_DELETE_TOOL = ToolDefinition(
    name="skill_delete",
    description="Delete a skill by ID. This removes the skill and all its versions.",
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
        "The imported skill is created as a new DB-managed skill with provenance tracking."
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
            "auto_load": {
                "type": "boolean",
                "description": "Auto-load for all agents (default false)",
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
    description="Export a skill as SKILL.md or Cognis YAML format.",
    parameters={
        "type": "object",
        "properties": {
            "skill_id": {"type": "string", "description": "The skill ID to export"},
            "format": {
                "type": "string",
                "enum": ["skill_md", "cognis_yaml"],
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


# ---------------------------------------------------------------------------
# Tool identification
# ---------------------------------------------------------------------------

_SKILL_TOOL_NAMES = {
    "skill_list",
    "skill_get",
    "skill_write",
    "skill_delete",
    "skill_import_url",
    "skill_export",
}


def is_skill_management_tool(tool_name: str) -> bool:
    """Check if a tool name is a skill management tool."""
    return tool_name in _SKILL_TOOL_NAMES


# ---------------------------------------------------------------------------
# Tool handlers
# ---------------------------------------------------------------------------


async def handle_skill_management_tool(
    tool_name: str,
    arguments: dict[str, Any],
    session_factory: Any,
    user_email: str,
) -> ToolResult:
    """Handle a skill management tool call."""
    try:
        if tool_name == "skill_list":
            return await _handle_skill_list(session_factory, user_email)
        if tool_name == "skill_get":
            return await _handle_skill_get(session_factory, user_email, arguments)
        if tool_name == "skill_write":
            return await _handle_skill_write(session_factory, user_email, arguments)
        if tool_name == "skill_delete":
            return await _handle_skill_delete(session_factory, user_email, arguments)
        if tool_name == "skill_import_url":
            return await _handle_skill_import_url(session_factory, user_email, arguments)
        if tool_name == "skill_export":
            return await _handle_skill_export(session_factory, user_email, arguments)
        return ToolResult(output=f"Unknown skill tool: {tool_name}", is_error=True)
    except Exception as exc:
        logger.warning(
            "Skill management tool failed",
            extra={"extra_data": {"tool_name": tool_name}},
            exc_info=True,
        )
        return ToolResult(output=f"Skill operation failed: {str(exc)[:500]}", is_error=True)


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
                "auto_load": row.auto_load,
                "source": row.source,
                "current_version_id": row.current_version_id,
            }
        )
    return ToolResult(output=json.dumps(skills, indent=2))


async def _handle_skill_get(
    session_factory: Any, user_email: str, arguments: dict[str, Any]
) -> ToolResult:
    import json

    from cognis.store.queries import get_skill_scoped, get_skill_version, list_skill_versions

    skill_id = str(arguments.get("skill_id", "")).strip()
    if not skill_id:
        return ToolResult(output="skill_id is required", is_error=True)

    async with session_factory() as session:
        row = await get_skill_scoped(session, skill_id, owner_email=user_email)
        if row is None:
            return ToolResult(output=f"Skill '{skill_id}' not found", is_error=True)

        version_data = None
        if row.current_version_id:
            ver = await get_skill_version(session, row.current_version_id)
            if ver:
                version_data = {
                    "version_id": ver.version_id,
                    "version_number": ver.version_number,
                    "content_hash": ver.content_hash,
                    "instructions": ver.instructions,
                    "tools": ver.tools,
                    "prompt_templates": ver.prompt_templates,
                    "secret_placeholders": ver.secret_placeholders,
                    "source_url": ver.source_url,
                    "asset_manifest": ver.asset_manifest,
                }

        versions = await list_skill_versions(session, skill_id)

    result = {
        "skill_id": row.skill_id,
        "name": row.name,
        "description": row.description,
        "tags": row.tags or [],
        "auto_load": row.auto_load,
        "source": row.source,
        "current_version": version_data,
        "version_count": len(versions),
    }
    return ToolResult(output=json.dumps(result, indent=2, default=str))


async def _handle_skill_write(
    session_factory: Any, user_email: str, arguments: dict[str, Any]
) -> ToolResult:
    import json

    from cognis.store.queries import (
        create_skill,
        create_skill_version,
        get_next_version_number,
        get_skill_scoped,
        set_current_version,
        update_skill,
    )
    from cognis.tools.skill_parser import compute_content_hash

    skill_id = arguments.get("skill_id")
    name = str(arguments.get("name", "")).strip()
    instructions = str(arguments.get("instructions", "")).strip()

    if not name:
        return ToolResult(output="name is required", is_error=True)
    if not instructions:
        return ToolResult(output="instructions is required", is_error=True)

    tools = arguments.get("tools")
    templates = arguments.get("prompt_templates")
    tags = arguments.get("tags")
    auto_load = bool(arguments.get("auto_load", False))
    description = arguments.get("description")

    content_hash = compute_content_hash(instructions, tools, templates)

    async with session_factory() as session:
        if skill_id:
            # Update existing
            row = await get_skill_scoped(session, skill_id, owner_email=user_email)
            if row is None:
                return ToolResult(output=f"Skill '{skill_id}' not found", is_error=True)
            await update_skill(
                session,
                skill_id,
                owner_email=user_email,
                name=name,
                description=description,
                instructions=instructions,
                tools=tools,
                prompt_templates=templates,
                tags=tags,
                auto_load=auto_load,
            )
            next_num = await get_next_version_number(session, skill_id)
        else:
            # Create new
            row = await create_skill(
                session,
                name=name,
                description=description,
                instructions=instructions,
                tools=tools,
                prompt_templates=templates,
                tags=tags,
                auto_load=auto_load,
                owner_email=user_email,
            )
            skill_id = row.skill_id
            next_num = 1

        version_row = await create_skill_version(
            session,
            skill_id=skill_id,
            version_number=next_num,
            content_hash=content_hash,
            instructions=instructions,
            tools=tools,
            prompt_templates=templates,
        )
        await set_current_version(session, skill_id, version_row.version_id)
        await session.commit()

    result = {
        "skill_id": skill_id,
        "name": name,
        "version_id": version_row.version_id,
        "version_number": next_num,
        "content_hash": content_hash,
    }
    return ToolResult(output=json.dumps(result, indent=2))


async def _handle_skill_delete(
    session_factory: Any, user_email: str, arguments: dict[str, Any]
) -> ToolResult:
    from cognis.store.queries import delete_skill, get_skill_scoped

    skill_id = str(arguments.get("skill_id", "")).strip()
    if not skill_id:
        return ToolResult(output="skill_id is required", is_error=True)

    async with session_factory() as session:
        row = await get_skill_scoped(session, skill_id, owner_email=user_email)
        if row is None:
            return ToolResult(output=f"Skill '{skill_id}' not found", is_error=True)
        try:
            deleted = await delete_skill(session, skill_id, owner_email=user_email)
        except ValueError as exc:
            return ToolResult(output=str(exc), is_error=True)
        if not deleted:
            return ToolResult(output=f"Skill '{skill_id}' not found", is_error=True)
        await session.commit()

    return ToolResult(output=f"Skill '{skill_id}' deleted successfully.")


async def _handle_skill_import_url(
    session_factory: Any, user_email: str, arguments: dict[str, Any]
) -> ToolResult:
    import json

    from cognis.store.queries import create_skill, create_skill_version, set_current_version
    from cognis.tools.skill_import import import_skill_from_url
    from cognis.tools.skill_parser import compute_content_hash

    url = str(arguments.get("url", "")).strip()
    if not url:
        return ToolResult(output="url is required", is_error=True)

    try:
        skill_data, provenance = await import_skill_from_url(url)
    except ValueError as exc:
        return ToolResult(output=f"Import failed: {exc}", is_error=True)

    name = arguments.get("name") or skill_data.get("name") or "Imported Skill"
    instructions = skill_data.get("instructions", "")
    tools = skill_data.get("tools")
    templates = skill_data.get("prompt_templates")
    tags = arguments.get("tags") or skill_data.get("tags") or []
    auto_load = bool(arguments.get("auto_load", False))

    content_hash = compute_content_hash(instructions, tools, templates)

    async with session_factory() as session:
        row = await create_skill(
            session,
            name=name,
            description=skill_data.get("description"),
            instructions=instructions,
            tools=tools,
            prompt_templates=templates,
            tags=tags,
            auto_load=auto_load,
            source="imported",
            owner_email=user_email,
        )
        version_row = await create_skill_version(
            session,
            skill_id=row.skill_id,
            version_number=1,
            content_hash=content_hash,
            instructions=instructions,
            tools=tools,
            prompt_templates=templates,
            secret_placeholders=skill_data.get("secret_placeholders"),
            source_url=provenance.source_url,
            resolved_url=provenance.resolved_url,
            commit_sha=provenance.commit_sha,
            import_checksum=provenance.import_checksum,
            imported_at=provenance.imported_at,
            import_format=provenance.import_format,
        )
        await set_current_version(session, row.skill_id, version_row.version_id)
        await session.commit()

    result = {
        "skill_id": row.skill_id,
        "name": name,
        "version_id": version_row.version_id,
        "source_url": provenance.source_url,
        "import_format": provenance.import_format,
    }
    return ToolResult(output=json.dumps(result, indent=2))


async def _handle_skill_export(
    session_factory: Any, user_email: str, arguments: dict[str, Any]
) -> ToolResult:
    from cognis.models.skill import ImportProvenance, SkillAssetRef, SkillExportData
    from cognis.store.queries import get_skill_scoped, get_skill_version
    from cognis.tools.skill_parser import export_cognis_yaml, export_skill_md

    skill_id = str(arguments.get("skill_id", "")).strip()
    if not skill_id:
        return ToolResult(output="skill_id is required", is_error=True)

    fmt = str(arguments.get("format", "skill_md")).strip()

    async with session_factory() as session:
        row = await get_skill_scoped(session, skill_id, owner_email=user_email)
        if row is None:
            return ToolResult(output=f"Skill '{skill_id}' not found", is_error=True)

        version_row = None
        if row.current_version_id:
            version_row = await get_skill_version(session, row.current_version_id)

    provenance = None
    asset_manifest: list[SkillAssetRef] = []
    if version_row and version_row.source_url:
        provenance = ImportProvenance(
            source_url=version_row.source_url,
            resolved_url=version_row.resolved_url,
            commit_sha=version_row.commit_sha,
        )
    if version_row and version_row.asset_manifest:
        asset_manifest = [SkillAssetRef.model_validate(a) for a in version_row.asset_manifest]

    export_data = SkillExportData(
        name=row.name,
        description=row.description,
        tags=row.tags or [],
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

    if fmt == "cognis_yaml":
        content = export_cognis_yaml(export_data)
    else:
        content = export_skill_md(export_data)

    return ToolResult(output=content)
