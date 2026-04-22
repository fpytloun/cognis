"""Skill resolution and runtime integration.

Resolves discoverable skills for an agent from DB records, returning versioned
instruction blocks, tool definitions, prompt templates, and asset refs.

Backward-compatible: legacy inline ``agent.skills.items[*].tool_names``
entries are still supported as an additive fallback.
"""

from __future__ import annotations

import hashlib
import re
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
from cognis.models.tool import ToolDefinition, ToolSource, stable_tool_id
from cognis.store.models import SkillRow, SkillVersionRow
from cognis.store.queries import list_skill_assets

logger = get_logger(__name__)

_SCOPED_SYSTEM_SKILL_IDS = frozenset({"cognis-orchestrator"})

_SAFE_SKILL_TOOL_SEGMENT = re.compile(r"[^a-zA-Z0-9_-]+")
_MAX_SKILL_TOOL_NAME_LENGTH = 64


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
    include_scoped_system_skills: bool = False,
) -> ResolvedSkillSet:
    """Resolve discoverable skills for an agent from DB.

    Returns a ``ResolvedSkillSet`` with versioned skill data suitable
    for both effective-tools preview and runtime context assembly.

    Resolution order:
    1. Agent-attached skill refs (in order)
    2. Skills attached to all agents (alphabetical by name)
    3. Other visible skills (alphabetical by name)

    All visible skills remain discoverable via prompt metadata and ``skill_load``.
    Attached skills are marked so they can be highlighted in the prompt and
    exposed by default through the deferred tool-loading path.
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
    all_skills = {
        row.skill_id: row
        for row in result.scalars().all()
        if include_scoped_system_skills or row.skill_id not in _SCOPED_SYSTEM_SKILL_IDS
    }

    # Build ordered list: attached first, then globally attached, then discoverable.
    seen: set[str] = set()
    ordered_skill_ids: list[str] = []

    for skill_id in enabled_ids:
        if skill_id in all_skills and skill_id not in seen:
            ordered_skill_ids.append(skill_id)
            seen.add(skill_id)

    globally_attached = sorted(
        (
            row
            for row in all_skills.values()
            if row.auto_load and row.skill_id not in _SCOPED_SYSTEM_SKILL_IDS
        ),
        key=lambda row: (row.name.lower(), row.skill_id),
    )
    for row in globally_attached:
        skill_id = row.skill_id
        if skill_id not in seen:
            ordered_skill_ids.append(skill_id)
            seen.add(skill_id)

    discoverable = sorted(
        (row for row in all_skills.values() if row.skill_id not in seen),
        key=lambda row: (row.name.lower(), row.skill_id),
    )
    for row in discoverable:
        ordered_skill_ids.append(row.skill_id)
        seen.add(row.skill_id)

    # Resolve versions
    resolved: list[ResolvedSkill] = []
    version_snapshot: dict[str, str] = {}

    for skill_id in ordered_skill_ids:
        skill_row = all_skills[skill_id]
        attached = skill_id in enabled_ids or bool(
            skill_row.auto_load and skill_id not in _SCOPED_SYSTEM_SKILL_IDS
        )
        version_id = skill_row.current_version_id

        if version_id:
            # Fetch versioned content
            ver_result = await session.execute(
                select(SkillVersionRow).where(SkillVersionRow.version_id == version_id)
            )
            version_row = ver_result.scalar_one_or_none()
            if version_row:
                tools = _parse_tool_specs_from_version(version_row.tools)
                asset_rows = await list_skill_assets(session, version_row.version_id)
                asset_manifest = (
                    [
                        SkillAssetRef(
                            filename=asset.filename,
                            asset_id=asset.asset_id,
                            artifact_namespace=asset.artifact_namespace,
                            artifact_object_id=asset.artifact_object_id,
                            content_hash=asset.content_hash,
                            size_bytes=asset.size_bytes,
                            content_type=asset.content_type,
                        )
                        for asset in asset_rows
                    ]
                    if asset_rows
                    else _parse_asset_manifest(version_row.asset_manifest)
                )
                resolved.append(
                    ResolvedSkill(
                        skill_id=skill_id,
                        name=skill_row.name,
                        description=skill_row.description,
                        version_id=version_row.version_id,
                        version_number=version_row.version_number,
                        content_hash=version_row.content_hash,
                        instructions=version_row.instructions,
                        tools=tools,
                        prompt_templates=version_row.prompt_templates or {},
                        secret_placeholders=version_row.secret_placeholders or [],
                        steps=[
                            item
                            for item in (getattr(version_row, "steps", None) or [])
                            if isinstance(item, dict)
                        ],
                        decomposition_source_hash=getattr(
                            version_row, "decomposition_source_hash", None
                        ),
                        asset_manifest=asset_manifest,
                        auto_load=skill_row.auto_load,
                        attached=attached,
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
                description=skill_row.description,
                version_id="",
                version_number=0,
                content_hash="",
                instructions=skill_row.instructions,
                tools=tools,
                prompt_templates=skill_row.prompt_templates or {},
                steps=[
                    item
                    for item in (getattr(skill_row, "steps", None) or [])
                    if isinstance(item, dict)
                ],
                auto_load=skill_row.auto_load,
                attached=attached,
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
# Compact prompt metadata for <available_skills>
# ---------------------------------------------------------------------------


def build_available_skills_metadata(resolved: ResolvedSkillSet) -> str:
    """Build compact XML metadata for the immutable prompt prefix.

    This is token-efficient: only stable identifiers and summaries are
    included.  Full instructions are loaded on demand via ``skill_load``.
    Version ids and content hashes are intentionally excluded to keep
    the immutable prefix stable for prompt caching.
    """
    if not resolved.skills:
        return ""

    lines: list[str] = []
    ordered_skills = sorted(
        resolved.skills,
        key=lambda skill: (
            0 if skill.attached else 1,
            0 if skill.auto_load else 1,
            skill.name.lower(),
            skill.skill_id,
        ),
    )
    for skill in ordered_skills:
        tool_names = ", ".join(t.name for t in skill.tools) if skill.tools else ""
        lines.append("  <skill>")
        lines.append(f"    <name>{skill.name}</name>")
        lines.append(f"    <skill_id>{skill.skill_id}</skill_id>")
        # Use the real skill description so the model can decide which
        # skills are relevant.  Fall back to a truncated instruction hint
        # if no description is set.
        if skill.description:
            desc = skill.description
        elif skill.instructions:
            desc = skill.instructions[:150].replace("\n", " ").strip()
            if len(skill.instructions) > 150:
                desc += "..."
        else:
            desc = f"Skill {skill.name}. Use skill_load for details."
        lines.append(f"    <description>{desc}</description>")
        if tool_names:
            lines.append(f"    <tools>{tool_names}</tools>")
        if skill.attached:
            lines.append("    <attached>true</attached>")
        if skill.auto_load:
            lines.append("    <attach_to_all_agents>true</attach_to_all_agents>")
        lines.append("  </skill>")

    return "<available_skills>\n" + "\n".join(lines) + "\n</available_skills>"


# ---------------------------------------------------------------------------
# Convert resolved skills to ToolDefinitions
# ---------------------------------------------------------------------------


def skill_tools_to_definitions(
    resolved: ResolvedSkillSet,
    *,
    include_unattached: bool = False,
) -> list[ToolDefinition]:
    """Convert resolved skill tool specs into ToolDefinition objects.

    These are registered in the runtime tool registry alongside
    builtin/executor/MCP tools.  Execution metadata (recipe, assets,
    secret placeholders) is carried forward for executor handlers.
    """
    definitions: list[ToolDefinition] = []
    for skill in resolved.skills:
        if not include_unattached and not skill.attached:
            continue
        for tool_spec in skill.tools:
            # Build execution metadata for the executor handler
            exec_meta: dict[str, Any] = {}
            if tool_spec.recipe:
                exec_meta["recipe"] = tool_spec.recipe.model_dump(mode="json")
            if skill.asset_manifest:
                exec_meta["asset_manifest"] = [
                    a.model_dump(mode="json") for a in skill.asset_manifest
                ]
            if skill.secret_placeholders:
                exec_meta["secret_placeholders"] = skill.secret_placeholders

            definition = ToolDefinition(
                name=_qualified_skill_tool_name(skill.skill_id, tool_spec.name),
                description=tool_spec.description,
                parameters=tool_spec.parameters,
                source=ToolSource(
                    type="skill",
                    skill_id=skill.skill_id,
                    skill_version_id=skill.version_id or None,
                    skill_content_hash=skill.content_hash or None,
                    raw_tool_name=tool_spec.name,
                ),
                category="skill",
                read_only=tool_spec.read_only,
                non_bypassable=tool_spec.non_bypassable,
                timeout_seconds=tool_spec.timeout_seconds,
                max_result_size=tool_spec.max_result_size,
                execution_metadata=exec_meta if exec_meta else None,
            )
            definitions.append(definition)
    return definitions


def discoverable_skill_tools_to_definitions(resolved: ResolvedSkillSet) -> list[ToolDefinition]:
    """Convert all discoverable skill tools into executable definitions."""

    return skill_tools_to_definitions(resolved, include_unattached=True)


def raw_skill_tools_to_definitions(
    *,
    skill_id: str,
    version_id: str | None,
    content_hash: str | None,
    tools: list[dict[str, Any]] | None,
) -> list[ToolDefinition]:
    """Convert persisted raw skill tool specs to tool definitions."""

    definitions: list[ToolDefinition] = []
    for raw_tool in tools or []:
        if not isinstance(raw_tool, dict):
            continue
        tool_spec = SkillToolSpec.model_validate(raw_tool)
        definitions.append(
            ToolDefinition(
                name=_qualified_skill_tool_name(skill_id, tool_spec.name),
                description=tool_spec.description,
                parameters=tool_spec.parameters,
                source=ToolSource(
                    type="skill",
                    skill_id=skill_id,
                    skill_version_id=version_id,
                    skill_content_hash=content_hash,
                    raw_tool_name=tool_spec.name,
                ),
                category="skill",
                read_only=tool_spec.read_only,
                non_bypassable=tool_spec.non_bypassable,
                timeout_seconds=tool_spec.timeout_seconds,
                max_result_size=tool_spec.max_result_size,
            )
        )
    return definitions


def _qualified_skill_tool_name(skill_id: str, tool_name: str) -> str:
    """Return a deterministic registry-safe internal name for a skill tool."""

    safe_skill = _SAFE_SKILL_TOOL_SEGMENT.sub("_", skill_id).strip("_") or "skill"
    safe_tool = _SAFE_SKILL_TOOL_SEGMENT.sub("_", tool_name).strip("_") or "tool"
    base_name = f"skill_{safe_skill}__{safe_tool}"
    if len(base_name) <= _MAX_SKILL_TOOL_NAME_LENGTH:
        return base_name
    suffix = hashlib.sha1(f"{skill_id}:{tool_name}".encode()).hexdigest()[:8]
    trimmed = base_name[: _MAX_SKILL_TOOL_NAME_LENGTH - len(suffix) - 1].rstrip("_")
    return f"{trimmed}_{suffix}"


def attached_skill_tool_ids(resolved: ResolvedSkillSet) -> set[str]:
    """Return stable tool IDs for skills attached to this agent context."""

    return {
        stable_tool_id(tool)
        for tool in skill_tools_to_definitions(
            ResolvedSkillSet(skills=[skill for skill in resolved.skills if skill.attached])
        )
    }
