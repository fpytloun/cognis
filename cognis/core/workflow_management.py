"""Shared workflow management helpers for API routes and agent tools."""

from __future__ import annotations

import re
import uuid
from typing import Any

from pydantic import BaseModel, Field

from cognis.core.management import (
    count_active_task_references_for_workflow,
    validate_workflow_definition,
)
from cognis.store.queries import get_skill_scoped
from cognis.store.queries import create_workflow, delete_workflow, get_workflow, update_workflow


class SkillWorkflowSource(BaseModel):
    """A skill with saved decomposition that can materialize a workflow."""

    skill_id: str
    name: str
    description: str | None = None
    instructions: str
    tags: list[str] = Field(default_factory=list)
    steps: list[dict[str, Any]] = Field(default_factory=list)


_SKILL_WORKFLOW_CANDIDATE_PREFIX = "skill:"


def encode_skill_workflow_candidate_id(skill_id: str) -> str:
    """Encode a skill candidate for workflow selection."""

    return f"{_SKILL_WORKFLOW_CANDIDATE_PREFIX}{skill_id}"


def decode_skill_workflow_candidate_id(candidate_id: str) -> str | None:
    """Decode a workflow-selection candidate back to a skill id."""

    if candidate_id.startswith(_SKILL_WORKFLOW_CANDIDATE_PREFIX):
        return candidate_id[len(_SKILL_WORKFLOW_CANDIDATE_PREFIX) :]
    return None


def _summarize_skill_instructions(instructions: str, *, limit: int = 180) -> str:
    """Extract a short plain-text summary from skill instructions."""

    cleaned = re.sub(r"\s+", " ", instructions.replace("#", " ")).strip()
    if len(cleaned) <= limit:
        return cleaned
    return cleaned[: limit - 1].rstrip() + "..."


def skill_workflow_criteria(source: SkillWorkflowSource) -> str:
    """Build classifier-facing criteria text for a decomposed skill."""

    tags = [tag for tag in (getattr(source, "tags", None) or []) if isinstance(tag, str)]
    parts = [f"Tasks that match the skill {source.name}."]
    if source.description:
        parts.append(source.description.strip())
    summary = _summarize_skill_instructions(source.instructions)
    if summary:
        parts.append(f"Instructions summary: {summary}")
    if tags:
        parts.append("Tags: " + ", ".join(tags))
    return " ".join(part for part in parts if part)


def build_skill_workflow_payload(
    *,
    source: SkillWorkflowSource,
    owner_email: str,
    lifecycle: str,
    composition_source: str,
    composition_intent: str | None = None,
) -> dict[str, Any]:
    """Build a workflow payload from a decomposed skill."""

    return {
        "name": f"{source.name} Workflow",
        "description": source.description or f"Derived from skill {source.name}",
        "criteria": skill_workflow_criteria(source),
        "tags": sorted({*source.tags, "skill"}),
        "interaction": {"mode": "explicit_gates"},
        "defaults": {
            "evaluate": True,
            "max_attempts": 3,
            "on_exhausted": "gate",
            "delivery": {
                "completion_mode_family": "default",
                "allow_silent_completion": False,
            },
        },
        "steps": source.steps,
        "is_system": False,
        "owner_email": owner_email,
        "lifecycle": lifecycle,
        "archived_at": None,
        "lineage": {
            "base_workflow_id": None,
            "source_skill_ids": [source.skill_id],
            "composition_source": composition_source,
            "composition_intent": composition_intent,
        },
    }


async def get_skill_workflow_source(
    *, session_factory: Any, owner_email: str, skill_id: str
) -> SkillWorkflowSource:
    """Load a decomposed skill visible to the user as a workflow source."""

    from cognis.tools.skill_service import resolve_current_skill_version

    async with session_factory() as session:
        row = await get_skill_scoped(session, skill_id, owner_email=owner_email)
        if row is None:
            raise ValueError("Skill not found")
        version_row = await resolve_current_skill_version(session, row)
        instructions = version_row.instructions if version_row is not None else row.instructions
        steps = [
            item
            for item in (
                ((getattr(version_row, "steps", None) if version_row is not None else None)
                or (getattr(row, "steps", None) or []))
            )
            if isinstance(item, dict)
        ]
        if not steps:
            raise ValueError("Skill has no saved workflow decomposition")
        return SkillWorkflowSource(
            skill_id=row.skill_id,
            name=row.name,
            description=row.description,
            instructions=instructions,
            tags=[tag for tag in (row.tags or []) if isinstance(tag, str)],
            steps=steps,
        )


async def get_attached_skill_workflow_source(
    *,
    session_factory: Any,
    owner_email: str,
    agent: Any,
    skill_id: str,
) -> SkillWorkflowSource:
    """Load a decomposed skill only if it is attached to the agent."""

    from cognis.tools.skills import resolve_skills_for_agent

    async with session_factory() as session:
        resolved_skills = await resolve_skills_for_agent(
            session,
            agent,
            owner_email=owner_email,
        )
    for skill in resolved_skills.skills:
        if skill.skill_id != skill_id:
            continue
        if not skill.attached:
            break
        if not skill.steps:
            raise ValueError("Skill has no saved workflow decomposition")
        return SkillWorkflowSource(
            skill_id=skill.skill_id,
            name=skill.name,
            description=skill.description,
            instructions=skill.instructions,
            tags=[tag for tag in (getattr(skill, "tags", None) or []) if isinstance(tag, str)],
            steps=skill.steps,
        )
    raise ValueError("Skill is not attached to the selected agent")


async def materialize_skill_workflow(
    *,
    session_factory: Any,
    owner_email: str,
    skill_id: str,
    lifecycle: str,
    composition_source: str,
    composition_intent: str | None = None,
    source: SkillWorkflowSource | None = None,
) -> Any:
    """Create a concrete workflow from a skill's saved decomposition."""

    source = source or await get_skill_workflow_source(
        session_factory=session_factory,
        owner_email=owner_email,
        skill_id=skill_id,
    )
    payload = build_skill_workflow_payload(
        source=source,
        owner_email=owner_email,
        lifecycle=lifecycle,
        composition_source=composition_source,
        composition_intent=composition_intent,
    )
    return await create_user_workflow(
        session_factory=session_factory,
        owner_email=owner_email,
        payload=payload,
        allow_ephemeral=lifecycle == "ephemeral",
    )


async def delete_materialized_workflow(*, session_factory: Any, workflow_id: str) -> None:
    """Delete a just-created workflow after downstream failures."""

    async with session_factory() as session:
        await delete_workflow(session, workflow_id)
        await session.commit()


async def list_workflows_for_user(
    *,
    workflow_registry: Any,
    owner_email: str,
    include_ephemeral: bool = False,
) -> list[Any]:
    """List workflows visible to the user."""

    return await workflow_registry.list_all(
        owner_email=owner_email,
        include_disabled=True,
        include_ephemeral=include_ephemeral,
    )


async def get_workflow_for_user(
    *, workflow_registry: Any, workflow_id: str, owner_email: str
) -> Any | None:
    """Return a visible workflow for the user."""

    workflow = await workflow_registry.get(
        workflow_id, owner_email=owner_email, include_disabled=True
    )
    if workflow is None:
        return None
    if workflow.is_system or workflow.owner_email in {owner_email, None}:
        return workflow
    return None


async def create_user_workflow(
    *,
    session_factory: Any,
    owner_email: str,
    payload: dict[str, Any],
    allow_ephemeral: bool = False,
) -> Any:
    """Create a user-owned workflow after validation."""

    workflow_id = payload.get("workflow_id") or f"wf_{uuid.uuid4().hex[:12]}"
    definition = {
        "workflow_id": workflow_id,
        "name": payload["name"],
        "description": payload.get("description", ""),
        "version": payload.get("version", 1),
        "criteria": payload.get("criteria", ""),
        "tags": payload.get("tags", []),
        "interaction": payload.get("interaction", {}),
        "defaults": payload.get("defaults", {}),
        "steps": payload["steps"],
        "is_system": False,
        "owner_email": owner_email,
        "lifecycle": payload.get("lifecycle", "persistent"),
        "lineage": payload.get("lineage"),
    }
    if definition["lifecycle"] == "ephemeral" and not allow_ephemeral:
        raise ValueError("Ephemeral lifecycle is reserved for composed workflows")
    definition = validate_workflow_definition(definition)

    async with session_factory() as session:
        row = await create_workflow(
            session,
            workflow_id=workflow_id,
            name=definition["name"],
            description=str(definition.get("description", "")),
            definition=definition,
            version=int(definition.get("version", 1)),
            is_system=False,
            owner_email=owner_email,
            lifecycle=str(definition.get("lifecycle", "persistent")),
        )
        await session.commit()
        await session.refresh(row)
        return row


async def update_user_workflow(
    *,
    session_factory: Any,
    workflow_id: str,
    owner_email: str,
    payload: dict[str, Any],
    allow_ephemeral: bool = False,
) -> Any:
    """Update a user-owned workflow after validation and active-run checks."""

    async with session_factory() as session:
        row = await get_workflow(session, workflow_id)
        if row is None:
            raise ValueError("Workflow not found")
        if row.is_system:
            raise ValueError("System workflows are read-only")
        if getattr(row, "lifecycle", "persistent") == "ephemeral":
            raise ValueError("Ephemeral workflows are read-only; promote or duplicate them first")
        if row.owner_email != owner_email:
            raise ValueError("Workflow access denied")

    if await count_active_task_references_for_workflow(session_factory, workflow_id) > 0:
        raise ValueError("Cannot modify a workflow referenced by active tasks")

    async with session_factory() as session:
        row = await get_workflow(session, workflow_id)
        assert row is not None
        definition = dict(row.definition or {})
        definition.update({key: value for key, value in payload.items() if value is not None})
        definition["workflow_id"] = workflow_id
        definition["is_system"] = False
        definition["owner_email"] = owner_email
        if definition.get("lifecycle") == "ephemeral" and not allow_ephemeral:
            raise ValueError("Ephemeral lifecycle is reserved for composed workflows")
        definition = validate_workflow_definition(definition)
        ok = await update_workflow(
            session,
            workflow_id,
            updates={
                **({"name": payload["name"]} if payload.get("name") is not None else {}),
                **(
                    {"description": payload["description"]}
                    if payload.get("description") is not None
                    else {}
                ),
                **({"version": payload["version"]} if payload.get("version") is not None else {}),
                **(
                    {"lifecycle": payload["lifecycle"]}
                    if payload.get("lifecycle") is not None
                    else {}
                ),
                "definition": definition,
            },
        )
        if not ok:
            raise ValueError("Workflow update failed")
        await session.commit()
        await session.refresh(row)
        return row


async def delete_user_workflow(
    *,
    session_factory: Any,
    workflow_id: str,
    owner_email: str,
) -> bool:
    """Delete a user-owned workflow if it is safe to do so."""

    async with session_factory() as session:
        row = await get_workflow(session, workflow_id)
        if row is None:
            raise ValueError("Workflow not found")
        if row.is_system:
            raise ValueError("System workflows are read-only")
        if getattr(row, "lifecycle", "persistent") == "ephemeral":
            raise ValueError("Ephemeral workflows are read-only; promote or duplicate them first")
        if row.owner_email != owner_email:
            raise ValueError("Workflow access denied")

    if await count_active_task_references_for_workflow(session_factory, workflow_id) > 0:
        raise ValueError("Cannot delete a workflow referenced by active tasks")

    async with session_factory() as session:
        ok = await delete_workflow(session, workflow_id)
        await session.commit()
        return ok


async def duplicate_visible_workflow(
    *,
    session_factory: Any,
    workflow_registry: Any,
    workflow_id: str,
    owner_email: str,
    allow_admin: bool = False,
) -> Any:
    """Duplicate a visible workflow into a new user-owned workflow."""

    workflow = await get_workflow_for_user(
        workflow_registry=workflow_registry,
        workflow_id=workflow_id,
        owner_email=owner_email,
    )
    if workflow is None and allow_admin:
        workflow = await workflow_registry.get(
            workflow_id, owner_email=owner_email, include_disabled=True
        )
    if workflow is None:
        raise ValueError("Workflow not found")

    new_workflow_id = f"wf_{uuid.uuid4().hex[:12]}"
    definition = workflow.model_dump(mode="json")
    definition["workflow_id"] = new_workflow_id
    definition["name"] = f"{workflow.name} Copy"
    definition["is_system"] = False
    definition["owner_email"] = owner_email
    definition["lifecycle"] = "persistent"
    definition["archived_at"] = None
    definition = validate_workflow_definition(definition)

    async with session_factory() as session:
        row = await create_workflow(
            session,
            workflow_id=new_workflow_id,
            name=definition["name"],
            description=str(definition.get("description", "")),
            definition=definition,
            version=int(definition.get("version", 1)),
            is_system=False,
            owner_email=owner_email,
            lifecycle="persistent",
        )
        await session.commit()
        await session.refresh(row)
        return row
