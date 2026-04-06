"""Skill resolution and runtime integration.

Resolves active skills for an agent from DB records, returning versioned
instruction blocks, tool definitions, prompt templates, and asset refs.

Backward-compatible: legacy inline ``agent.skills.items[*].tool_names``
entries are still supported as an additive fallback.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from cognis.logging import get_logger
from cognis.models.agent import AgentDefinition
from cognis.models.skill import (
    AgentSkillRef,
    ResolvedSkill,
    ResolvedSkillSet,
    SkillAssetRef,
    SkillToolSpec,
)
from cognis.models.tool import ToolDefinition, ToolSource
from cognis.store.models import SkillRow, SkillVersionRow

logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# Legacy MVP loader (backward compatibility)
# ---------------------------------------------------------------------------


def load_skill_tool_names(agent: AgentDefinition | None) -> set[str]:
    """Return builtin/static tool names referenced by inline skill items.

    This is the legacy MVP behavior.  New agents should use DB skill refs
    instead of inline tool_names.
    """

    if agent is None or not isinstance(agent.skills, dict):
        return set()

    raw_items = agent.skills.get("items")
    if not isinstance(raw_items, list):
        return set()

    tool_names: set[str] = set()
    for index, item in enumerate(raw_items):
        if not isinstance(item, dict):
            logger.warning(
                "Skipping malformed skill entry",
                extra={"extra_data": {"agent_id": agent.agent_id, "index": index}},
            )
            continue
        skill_id = item.get("skill_id")
        skill_name = item.get("name")
        if not isinstance(skill_id, str) or not isinstance(skill_name, str):
            # Could be a new-style ref with just skill_id + enabled
            continue
        raw_tool_names = item.get("tool_names")
        if not isinstance(raw_tool_names, list):
            continue
        for tool_name in raw_tool_names:
            if isinstance(tool_name, str) and tool_name.strip():
                tool_names.add(tool_name)
    return tool_names


# ---------------------------------------------------------------------------
# Agent skill ref extraction
# ---------------------------------------------------------------------------


def extract_agent_skill_refs(agent: AgentDefinition | None) -> list[AgentSkillRef]:
    """Extract DB skill references from agent configuration.

    Supports both new-style refs (skill_id + enabled) and ignores
    legacy inline entries (which have tool_names instead).
    """
    if agent is None or not isinstance(agent.skills, dict):
        return []

    raw_items = agent.skills.get("items")
    if not isinstance(raw_items, list):
        return []

    refs: list[AgentSkillRef] = []
    for item in raw_items:
        if not isinstance(item, dict):
            continue
        skill_id = item.get("skill_id")
        if not isinstance(skill_id, str):
            continue
        # Skip legacy entries that have tool_names (handled by load_skill_tool_names)
        if "tool_names" in item:
            continue
        enabled = item.get("enabled", True)
        refs.append(AgentSkillRef(skill_id=skill_id, enabled=bool(enabled)))
    return refs


# ---------------------------------------------------------------------------
# DB-backed skill resolution
# ---------------------------------------------------------------------------


async def resolve_skills_for_agent(
    session: AsyncSession,
    agent: AgentDefinition,
    *,
    owner_email: str | None = None,
) -> ResolvedSkillSet:
    """Resolve active skills for an agent from DB.

    Returns a ``ResolvedSkillSet`` with versioned skill data suitable
    for both effective-tools preview and runtime context assembly.

    Resolution order:
    1. Agent-specified skill refs (in order)
    2. Auto-load skills visible to the owner (alphabetical by skill_id)

    Deduplication: a skill appearing in both lists is included only once,
    in the agent-specified position.
    """
    import sqlalchemy as sa
    from sqlalchemy import select

    # Get agent-specified skill refs
    agent_refs = extract_agent_skill_refs(agent)
    enabled_ids = [ref.skill_id for ref in agent_refs if ref.enabled]

    # Fetch all visible skills for this owner
    stmt = select(SkillRow).order_by(SkillRow.name)
    if owner_email is not None:
        stmt = stmt.where(
            sa.or_(SkillRow.owner_email == owner_email, SkillRow.owner_email.is_(None))
        )
    result = await session.execute(stmt)
    all_skills = {row.skill_id: row for row in result.scalars().all()}

    # Build ordered list: agent-specified first, then auto_load
    seen: set[str] = set()
    ordered_skill_ids: list[str] = []

    for skill_id in enabled_ids:
        if skill_id in all_skills and skill_id not in seen:
            ordered_skill_ids.append(skill_id)
            seen.add(skill_id)

    for skill_id, row in sorted(all_skills.items()):
        if row.auto_load and skill_id not in seen:
            ordered_skill_ids.append(skill_id)
            seen.add(skill_id)

    # Resolve versions
    resolved: list[ResolvedSkill] = []
    version_snapshot: dict[str, str] = {}

    for skill_id in ordered_skill_ids:
        skill_row = all_skills[skill_id]
        version_id = skill_row.current_version_id

        if version_id:
            # Fetch versioned content
            ver_result = await session.execute(
                select(SkillVersionRow).where(SkillVersionRow.version_id == version_id)
            )
            version_row = ver_result.scalar_one_or_none()
            if version_row:
                tools = _parse_tool_specs_from_version(version_row.tools)
                asset_manifest = _parse_asset_manifest(version_row.asset_manifest)
                resolved.append(
                    ResolvedSkill(
                        skill_id=skill_id,
                        name=skill_row.name,
                        version_id=version_row.version_id,
                        version_number=version_row.version_number,
                        content_hash=version_row.content_hash,
                        instructions=version_row.instructions,
                        tools=tools,
                        prompt_templates=version_row.prompt_templates or {},
                        secret_placeholders=version_row.secret_placeholders or [],
                        asset_manifest=asset_manifest,
                        auto_load=skill_row.auto_load,
                    )
                )
                version_snapshot[skill_id] = version_row.version_id
                continue

        # Fallback to legacy skill row content (no version)
        tools = _parse_tool_specs_from_version(skill_row.tools)
        resolved.append(
            ResolvedSkill(
                skill_id=skill_id,
                name=skill_row.name,
                version_id="",
                version_number=0,
                content_hash="",
                instructions=skill_row.instructions,
                tools=tools,
                prompt_templates=skill_row.prompt_templates or {},
                auto_load=skill_row.auto_load,
            )
        )

    return ResolvedSkillSet(skills=resolved, version_snapshot=version_snapshot)


def _parse_tool_specs_from_version(
    raw_tools: list[dict[str, Any]] | dict[str, Any] | None,
) -> list[SkillToolSpec]:
    """Parse tool specs from version row data."""
    if not raw_tools:
        return []
    if isinstance(raw_tools, dict):
        raw_tools = list(raw_tools.values()) if raw_tools else []
    if not isinstance(raw_tools, list):
        return []

    specs: list[SkillToolSpec] = []
    for raw in raw_tools:
        if not isinstance(raw, dict):
            continue
        try:
            specs.append(SkillToolSpec.model_validate(raw))
        except Exception:
            logger.warning("Skipping invalid tool spec in skill version", exc_info=True)
    return specs


def _parse_asset_manifest(
    raw_manifest: list[dict[str, Any]] | None,
) -> list[SkillAssetRef]:
    """Parse asset manifest from version row data."""
    if not raw_manifest or not isinstance(raw_manifest, list):
        return []
    refs: list[SkillAssetRef] = []
    for raw in raw_manifest:
        if isinstance(raw, dict):
            try:
                refs.append(SkillAssetRef.model_validate(raw))
            except Exception:
                logger.warning("Skipping invalid asset ref in skill version")
    return refs


# ---------------------------------------------------------------------------
# Convert resolved skills to ToolDefinitions
# ---------------------------------------------------------------------------


def skill_tools_to_definitions(
    resolved: ResolvedSkillSet,
) -> list[ToolDefinition]:
    """Convert resolved skill tool specs into ToolDefinition objects.

    These are registered in the runtime tool registry alongside
    builtin/executor/MCP tools.
    """
    definitions: list[ToolDefinition] = []
    for skill in resolved.skills:
        for tool_spec in skill.tools:
            definition = ToolDefinition(
                name=tool_spec.name,
                description=tool_spec.description,
                parameters=tool_spec.parameters,
                source=ToolSource(
                    type="skill",
                    skill_id=skill.skill_id,
                ),
                category="skill",
                read_only=tool_spec.read_only,
                non_bypassable=tool_spec.non_bypassable,
                timeout_seconds=tool_spec.timeout_seconds,
                max_result_size=tool_spec.max_result_size,
            )
            definitions.append(definition)
    return definitions
