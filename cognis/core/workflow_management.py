"""Shared workflow management helpers for API routes and agent tools."""

from __future__ import annotations

import uuid
from typing import Any

from cognis.core.management import (
    count_active_task_references_for_workflow,
    validate_workflow_definition,
)
from cognis.store.queries import create_workflow, delete_workflow, get_workflow, update_workflow


async def list_workflows_for_user(*, workflow_registry: Any, owner_email: str) -> list[Any]:
    """List workflows visible to the user."""

    return await workflow_registry.list_all(owner_email=owner_email, include_disabled=True)


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
    }
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
) -> Any:
    """Update a user-owned workflow after validation and active-run checks."""

    async with session_factory() as session:
        row = await get_workflow(session, workflow_id)
        if row is None:
            raise ValueError("Workflow not found")
        if row.is_system:
            raise ValueError("System workflows are read-only")
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
        )
        await session.commit()
        await session.refresh(row)
        return row
