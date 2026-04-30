"""Built-in project management tools."""

from __future__ import annotations

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from cognis.api.serializers import project_source_to_response, project_to_response
from cognis.models.tool import ToolDefinition, ToolSource
from cognis.store.queries import (
    attach_project_workflow,
    create_project,
    create_project_source,
    delete_project_source,
    detach_project_workflow,
    get_active_project_grant,
    get_project,
    get_project_source,
    list_project_sources,
    list_project_workflow_ids,
    list_projects_for_user,
    update_project,
    update_project_source,
)
from cognis.tools.registry import ToolExecutionContext

_SOURCE = ToolSource(type="builtin")
_PROJECT_CAUTION = (
    "Do not invent projects. Do not assign a project to a task unless an exact match "
    "(project name alias, source path prefix, or remote URL) exists. Project assignment is optional."
)


def _tool(
    name: str, description: str, properties: dict[str, Any], required: list[str] | None = None
) -> ToolDefinition:
    return ToolDefinition(
        name=name,
        description=f"{description} {_PROJECT_CAUTION}",
        parameters={"type": "object", "properties": properties, "required": required or []},
        source=_SOURCE,
        category="projects",
        read_only=name.startswith("list_") or name.startswith("get_"),
    )


LIST_PROJECTS_TOOL = _tool(
    "list_projects",
    "List owned and shared projects.",
    {"query": {"type": "string"}, "status": {"type": "string"}},
)
GET_PROJECT_TOOL = _tool(
    "get_project", "Get project details.", {"project_id": {"type": "string"}}, ["project_id"]
)
CREATE_PROJECT_TOOL = _tool(
    "create_project",
    "Create a project owned by the current user.",
    {
        "name": {"type": "string"},
        "description": {"type": "string"},
        "instructions": {"type": "string"},
        "default_workflow_id": {"type": "string"},
    },
    ["name"],
)
UPDATE_PROJECT_TOOL = _tool(
    "update_project",
    "Update an owned project.",
    {
        "project_id": {"type": "string"},
        "name": {"type": "string"},
        "description": {"type": "string"},
        "instructions": {"type": "string"},
        "default_workflow_id": {"type": "string"},
    },
    ["project_id"],
)
DELETE_PROJECT_TOOL = _tool(
    "delete_project",
    "Archive an owned project.",
    {"project_id": {"type": "string"}},
    ["project_id"],
)
ADD_PROJECT_SOURCE_TOOL = _tool(
    "add_project_source",
    "Add a source hint to an owned project.",
    {
        "project_id": {"type": "string"},
        "name": {"type": "string"},
        "local_path": {"type": "string"},
        "remote_url": {"type": "string"},
        "default_branch": {"type": "string"},
        "credential_ref": {"type": "string"},
        "instructions": {"type": "string"},
    },
    ["project_id", "name"],
)
UPDATE_PROJECT_SOURCE_TOOL = _tool(
    "update_project_source",
    "Update a project source hint.",
    {
        "source_id": {"type": "string"},
        "name": {"type": "string"},
        "local_path": {"type": "string"},
        "remote_url": {"type": "string"},
        "default_branch": {"type": "string"},
        "credential_ref": {"type": "string"},
        "instructions": {"type": "string"},
    },
    ["source_id"],
)
REMOVE_PROJECT_SOURCE_TOOL = _tool(
    "remove_project_source",
    "Remove a project source hint.",
    {"source_id": {"type": "string"}},
    ["source_id"],
)
ATTACH_WORKFLOW_TO_PROJECT_TOOL = _tool(
    "attach_workflow_to_project",
    "Bind a workflow to an owned project.",
    {"project_id": {"type": "string"}, "workflow_id": {"type": "string"}},
    ["project_id", "workflow_id"],
)
DETACH_WORKFLOW_FROM_PROJECT_TOOL = _tool(
    "detach_workflow_from_project",
    "Remove a workflow binding from an owned project.",
    {"project_id": {"type": "string"}, "workflow_id": {"type": "string"}},
    ["project_id", "workflow_id"],
)


def project_tools() -> list[ToolDefinition]:
    return [
        LIST_PROJECTS_TOOL,
        GET_PROJECT_TOOL,
        CREATE_PROJECT_TOOL,
        UPDATE_PROJECT_TOOL,
        DELETE_PROJECT_TOOL,
        ADD_PROJECT_SOURCE_TOOL,
        UPDATE_PROJECT_SOURCE_TOOL,
        REMOVE_PROJECT_SOURCE_TOOL,
        ATTACH_WORKFLOW_TO_PROJECT_TOOL,
        DETACH_WORKFLOW_FROM_PROJECT_TOOL,
    ]


def build_project_tool_handlers(
    session_factory: async_sessionmaker[AsyncSession],
) -> dict[str, Any]:
    async def _require_owner(session: AsyncSession, project_id: str, user_email: str) -> Any:
        row = await get_project(session, project_id)
        if row is None or row.owner_email != user_email:
            raise ValueError("Project not found or not owned by caller")
        return row

    async def _require_visible(session: AsyncSession, project_id: str, user_email: str) -> Any:
        row = await get_project(session, project_id)
        if row is None:
            raise ValueError("Project not found")
        if (
            row.owner_email != user_email
            and await get_active_project_grant(session, project_id, user_email) is None
        ):
            raise ValueError("Project not found")
        return row

    def _user(context: ToolExecutionContext) -> str:
        user_email = context.runtime_metadata.get("user_email")
        if not isinstance(user_email, str):
            runtime_access = context.runtime_metadata.get("runtime_access")
            if isinstance(runtime_access, dict):
                user_email = runtime_access.get("user_email")
        if not isinstance(user_email, str) and isinstance(
            context.shared_runtime_metadata, dict
        ):
            user_email = context.shared_runtime_metadata.get("user_email")
        if not isinstance(user_email, str):
            raise ValueError("User context is unavailable")
        return user_email

    async def list_projects_handler(
        arguments: dict[str, Any], context: ToolExecutionContext
    ) -> list[dict[str, Any]]:
        async with session_factory() as session:
            rows = await list_projects_for_user(
                session,
                _user(context),
                status=arguments.get("status") or "active",
                query=arguments.get("query"),
            )
        return [project_to_response(row).model_dump(mode="json") for row in rows]

    async def get_project_handler(
        arguments: dict[str, Any], context: ToolExecutionContext
    ) -> dict[str, Any]:
        async with session_factory() as session:
            row = await _require_visible(session, str(arguments["project_id"]), _user(context))
            sources = await list_project_sources(session, row.project_id)
            workflow_ids = await list_project_workflow_ids(session, row.project_id)
        return project_to_response(row, sources=sources, workflow_ids=workflow_ids).model_dump(
            mode="json"
        )

    async def create_project_handler(
        arguments: dict[str, Any], context: ToolExecutionContext
    ) -> dict[str, Any]:
        async with session_factory() as session:
            row = await create_project(session, owner_email=_user(context), **arguments)
            await session.commit()
        return project_to_response(row).model_dump(mode="json")

    async def update_project_handler(
        arguments: dict[str, Any], context: ToolExecutionContext
    ) -> dict[str, Any]:
        project_id = str(arguments.pop("project_id"))
        async with session_factory() as session:
            await _require_owner(session, project_id, _user(context))
            row = await update_project(session, project_id, **arguments)
            await session.commit()
        return project_to_response(row).model_dump(mode="json")

    async def delete_project_handler(
        arguments: dict[str, Any], context: ToolExecutionContext
    ) -> dict[str, Any]:
        async with session_factory() as session:
            await _require_owner(session, str(arguments["project_id"]), _user(context))
            await update_project(session, str(arguments["project_id"]), status="archived")
            await session.commit()
        return {"ok": True}

    async def add_source_handler(
        arguments: dict[str, Any], context: ToolExecutionContext
    ) -> dict[str, Any]:
        async with session_factory() as session:
            await _require_owner(session, str(arguments["project_id"]), _user(context))
            row = await create_project_source(session, **arguments)
            await session.commit()
        return project_source_to_response(row).model_dump(mode="json")

    async def update_source_handler(
        arguments: dict[str, Any], context: ToolExecutionContext
    ) -> dict[str, Any]:
        source_id = str(arguments.pop("source_id"))
        async with session_factory() as session:
            source = await get_project_source(session, source_id)
            if source is None:
                raise ValueError("Project source not found")
            await _require_owner(session, source.project_id, _user(context))
            row = await update_project_source(session, source_id, **arguments)
            await session.commit()
        return project_source_to_response(row).model_dump(mode="json")

    async def remove_source_handler(
        arguments: dict[str, Any], context: ToolExecutionContext
    ) -> dict[str, Any]:
        async with session_factory() as session:
            source = await get_project_source(session, str(arguments["source_id"]))
            if source is None:
                raise ValueError("Project source not found")
            await _require_owner(session, source.project_id, _user(context))
            ok = await delete_project_source(session, str(arguments["source_id"]))
            await session.commit()
        return {"ok": ok}

    async def attach_workflow_handler(
        arguments: dict[str, Any], context: ToolExecutionContext
    ) -> dict[str, Any]:
        async with session_factory() as session:
            await _require_owner(session, str(arguments["project_id"]), _user(context))
            await attach_project_workflow(
                session, str(arguments["project_id"]), str(arguments["workflow_id"])
            )
            await session.commit()
        return {"ok": True}

    async def detach_workflow_handler(
        arguments: dict[str, Any], context: ToolExecutionContext
    ) -> dict[str, Any]:
        async with session_factory() as session:
            await _require_owner(session, str(arguments["project_id"]), _user(context))
            ok = await detach_project_workflow(
                session, str(arguments["project_id"]), str(arguments["workflow_id"])
            )
            await session.commit()
        return {"ok": ok}

    return {
        LIST_PROJECTS_TOOL.name: list_projects_handler,
        GET_PROJECT_TOOL.name: get_project_handler,
        CREATE_PROJECT_TOOL.name: create_project_handler,
        UPDATE_PROJECT_TOOL.name: update_project_handler,
        DELETE_PROJECT_TOOL.name: delete_project_handler,
        ADD_PROJECT_SOURCE_TOOL.name: add_source_handler,
        UPDATE_PROJECT_SOURCE_TOOL.name: update_source_handler,
        REMOVE_PROJECT_SOURCE_TOOL.name: remove_source_handler,
        ATTACH_WORKFLOW_TO_PROJECT_TOOL.name: attach_workflow_handler,
        DETACH_WORKFLOW_FROM_PROJECT_TOOL.name: detach_workflow_handler,
    }
