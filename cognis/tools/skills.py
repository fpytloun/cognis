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
from cognis.store.models import Agent, SkillAssetRow, SkillRow, SkillVersionRow
from cognis.store.queries import list_skill_assets_for_versions, list_skills
from cognis.store.skill_usage import (
    list_agent_skill_usage,
    skill_usage_payload,
    skill_usage_tracking_started,
)
from cognis.tools.builtin.tool_search import bm25_scores, tokenize_search_text

logger = get_logger(__name__)

_SCOPED_SYSTEM_SKILL_IDS = frozenset({"cognis-orchestrator"})

_SAFE_SKILL_TOOL_SEGMENT = re.compile(r"[^a-zA-Z0-9_-]+")
_MAX_SKILL_TOOL_NAME_LENGTH = 64


def is_agent_assignable_skill(skill_id: str) -> bool:
    """Return whether a visible skill may be explicitly assigned to an agent."""

    return skill_id not in _SCOPED_SYSTEM_SKILL_IDS


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


def extract_agent_skill_refs(agent: AgentDefinition | Agent | None) -> list[AgentSkillRef]:
    """Extract DB skill references from agent configuration.

    Supports both new-style refs (skill_id + enabled) and ignores
    legacy inline entries (which have tool_names instead).
    """
    return _skill_refs(agent.skills if agent is not None else None)


def _skill_refs(skills: Any) -> list[AgentSkillRef]:
    if not isinstance(skills, dict):
        return []
    raw_items = skills.get("items")
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
        refs.append(
            AgentSkillRef(
                skill_id=skill_id,
                enabled=bool(enabled),
                auto_load_instructions=bool(item.get("auto_load_instructions", False)),
            )
        )
    return refs


def agent_skill_assignment(
    agent_skills: dict[str, Any] | None, skill_id: str, *, attach_to_all_agents: bool
) -> str:
    """Classify how a skill reaches an agent: global, auto_loaded, attached, or unattached."""

    if attach_to_all_agents and skill_id not in _SCOPED_SYSTEM_SKILL_IDS:
        return "global"
    for ref in _skill_refs(agent_skills):
        if ref.skill_id == skill_id and ref.enabled:
            return "auto_loaded" if ref.auto_load_instructions else "attached"
    return "unattached"


async def agent_skill_usage_report(
    session: AsyncSession, *, agent: Agent, owner_email: str, skill_id: str | None = None
) -> list[dict[str, Any]]:
    """Return usage and curation suggestions for skills attached to or used by an agent.

    With ``skill_id``, return only that visible skill, even when it is unused.
    """

    usage = await list_agent_skill_usage(session, agent.agent_id)
    tracking_started = await skill_usage_tracking_started(session)
    report: list[dict[str, Any]] = []
    for row in await list_skills(session, owner_email=owner_email):
        if not is_agent_assignable_skill(row.skill_id) or skill_id not in {None, row.skill_id}:
            continue
        assignment = agent_skill_assignment(
            agent.skills, row.skill_id, attach_to_all_agents=row.auto_load
        )
        if skill_id is None and assignment == "unattached" and row.skill_id not in usage:
            continue
        payload = skill_usage_payload(
            usage.get(row.skill_id),
            assignment=assignment,
            agent_updated_at=agent.updated_at,
            tracking_started=tracking_started,
        )
        report.append({"skill_id": row.skill_id, "name": row.name, **payload})
    return report


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

    All visible skills remain loadable with ``skill_load``. Only attached
    skills are advertised in the prompt and exposed by default through the
    deferred tool-loading path; the rest are found with ``skill_search``.
    """
    import sqlalchemy as sa
    from sqlalchemy import select

    from cognis.core.skill_access import list_readable_skills

    # Get agent-specified skill refs
    agent_refs = extract_agent_skill_refs(agent)
    enabled_ids = [ref.skill_id for ref in agent_refs if ref.enabled]
    auto_load_instruction_ids = {
        ref.skill_id for ref in agent_refs if ref.enabled and ref.auto_load_instructions
    }

    # Fetch all visible skills for this owner
    stmt = select(SkillRow).order_by(SkillRow.name)
    if owner_email is not None and agent.owner_email != owner_email:
        rows = await list_readable_skills(session, actor_email=owner_email, agent_id=agent.agent_id)
    else:
        if owner_email is not None:
            stmt = stmt.where(
                sa.or_(SkillRow.owner_email == owner_email, SkillRow.owner_email.is_(None))
            )
        rows = list((await session.scalars(stmt)).all())
    all_skills = {
        row.skill_id: row
        for row in rows
        if include_scoped_system_skills or is_agent_assignable_skill(row.skill_id)
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
    version_ids = [row.current_version_id for row in all_skills.values() if row.current_version_id]
    versions: dict[str, SkillVersionRow] = {}
    assets_by_version: dict[str, list[SkillAssetRow]] = {}
    if version_ids:
        version_result = await session.execute(
            select(SkillVersionRow).where(SkillVersionRow.version_id.in_(version_ids))
        )
        versions = {row.version_id: row for row in version_result.scalars().all()}
        for asset in await list_skill_assets_for_versions(session, list(versions)):
            assets_by_version.setdefault(asset.skill_version_id, []).append(asset)

    for skill_id in ordered_skill_ids:
        skill_row = all_skills[skill_id]
        attached = skill_id in enabled_ids or bool(
            skill_row.auto_load and skill_id not in _SCOPED_SYSTEM_SKILL_IDS
        )
        version_id = skill_row.current_version_id

        if version_id:
            version_row = versions.get(version_id)
            if version_row:
                tools = _parse_tool_specs_from_version(version_row.tools)
                asset_rows = assets_by_version.get(version_row.version_id, [])
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
                        tags=skill_row.tags or [],
                        linked_tool_ids=skill_row.linked_tool_ids or [],
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
                        auto_load_instructions=skill_id in auto_load_instruction_ids,
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
                tags=skill_row.tags or [],
                linked_tool_ids=skill_row.linked_tool_ids or [],
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
                auto_load_instructions=skill_id in auto_load_instruction_ids,
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


MAX_SKILL_DESCRIPTION_CHARS = 1024
_INSTRUCTION_EXCERPT_CHARS = 150


def skill_selection_description(description: str | None, instructions: str | None) -> str:
    """Return the bounded text an agent uses to decide whether to load a skill."""

    text = (description or "").strip()
    if not text and instructions:
        text = " ".join(instructions[:_INSTRUCTION_EXCERPT_CHARS].split())
        if len(instructions) > _INSTRUCTION_EXCERPT_CHARS:
            text += "..."
    if len(text) > MAX_SKILL_DESCRIPTION_CHARS:
        text = text[: MAX_SKILL_DESCRIPTION_CHARS - 3].rstrip() + "..."
    return text


def build_available_skills_metadata(resolved: ResolvedSkillSet) -> str:
    """Build the prompt catalog of skills attached to this agent.

    Only attached skills are listed, with id, name, and description. Skills
    whose instructions are already auto-loaded are omitted; other visible
    skills are only counted and reachable through ``skill_search``. Version
    ids and hashes are excluded to keep the immutable prefix cache-stable.
    """
    listed = sorted(
        (skill for skill in resolved.skills if skill.attached and not skill.auto_load_instructions),
        key=lambda skill: (skill.auto_load, skill.name.lower(), skill.skill_id),
    )
    searchable_count = sum(1 for skill in resolved.skills if not skill.attached)
    if not listed and not searchable_count:
        return ""

    lines: list[str] = []
    for skill in listed:
        lines.append("  <skill>")
        lines.append(f"    <skill_id>{skill.skill_id}</skill_id>")
        lines.append(f"    <name>{skill.name}</name>")
        description = skill_selection_description(skill.description, skill.instructions)
        if description:
            lines.append(f"    <description>{description}</description>")
        lines.append("  </skill>")
    if searchable_count:
        lines.append(
            f"  {searchable_count} more skills are available; find them with skill_search."
        )
    return "<available_skills>\n" + "\n".join(lines) + "\n</available_skills>"


def search_skill_rows(rows: list[SkillRow], query: str, *, limit: int = 10) -> list[dict[str, str]]:
    """Rank visible skills by name, description, and tags for ``skill_search``."""

    normalized_query = query.strip().lower()
    query_terms = tokenize_search_text(normalized_query)
    candidates = [row for row in rows if is_agent_assignable_skill(row.skill_id)]
    if not query_terms or not candidates:
        return []
    documents = [
        f"{row.skill_id} {row.name} {row.description or ''} {' '.join(row.tags or [])}"
        for row in candidates
    ]
    scored: list[tuple[float, SkillRow]] = []
    for row, score in zip(candidates, bm25_scores(documents, query_terms), strict=True):
        if normalized_query in row.name.lower() or normalized_query == row.skill_id.lower():
            score += 50.0
        if score > 0:
            scored.append((score, row))
    scored.sort(key=lambda item: (-item[0], item[1].name.lower(), item[1].skill_id))
    return [
        {
            "skill_id": row.skill_id,
            "name": row.name,
            "description": skill_selection_description(row.description, row.instructions),
        }
        for _, row in scored[: max(1, min(limit, 20))]
    ]


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
    """Return runtime tool IDs surfaced immediately by attached skills."""

    attached_by_skill = attached_skill_tool_ids_by_skill(resolved)
    return {tool_id for tool_ids in attached_by_skill.values() for tool_id in tool_ids}


def attached_skill_tool_ids_by_skill(resolved: ResolvedSkillSet) -> dict[str, list[str]]:
    """Return runtime tool IDs keyed by attached skill id."""

    attached_skills = [skill for skill in resolved.skills if skill.attached]
    attached_tool_ids_by_skill: dict[str, set[str]] = {
        skill.skill_id: set(skill.linked_tool_ids) for skill in attached_skills
    }
    for tool in skill_tools_to_definitions(ResolvedSkillSet(skills=attached_skills)):
        skill_id = tool.source.skill_id
        if not skill_id:
            continue
        attached_tool_ids_by_skill.setdefault(skill_id, set()).add(stable_tool_id(tool))

    return {skill_id: sorted(tool_ids) for skill_id, tool_ids in attached_tool_ids_by_skill.items()}
