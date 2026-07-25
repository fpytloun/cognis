"""Built-in system tool definitions and handlers."""

from __future__ import annotations

from typing import Any, Protocol

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from cognis.models.tool import NativeToolDefinition as ToolDefinition
from cognis.models.tool import ToolSource
from cognis.store.queries import list_active_agents_summary
from cognis.tools.builtin.tool_search import (
    DESCRIBE_TOOL_TOOL,
    SEARCH_TOOLS_TOOL,
    VALIDATE_TOOL_CALL_TOOL,
)
from cognis.tools.registry import ToolExecutionContext


class StatusProvider(Protocol):
    """Returns safe runtime status for the get_status builtin."""

    async def __call__(self, context: ToolExecutionContext) -> dict[str, Any]: ...


LIST_AGENTS_TOOL = ToolDefinition(
    name="list_agents",
    description="List active agents with ids, display names, and status for delegation or routing decisions.",
    parameters={"type": "object", "properties": {}},
    source=ToolSource(type="builtin"),
    category="system",
    read_only=True,
)

GET_STATUS_TOOL = ToolDefinition(
    name="get_status",
    description="Return safe runtime status including active session, executor, and capability metadata.",
    parameters={"type": "object", "properties": {}},
    source=ToolSource(type="builtin"),
    category="system",
    read_only=True,
)


def system_tools() -> list[ToolDefinition]:
    """Return built-in system tool definitions."""

    return [
        LIST_AGENTS_TOOL,
        GET_STATUS_TOOL,
        SEARCH_TOOLS_TOOL,
        DESCRIBE_TOOL_TOOL,
        VALIDATE_TOOL_CALL_TOOL,
    ]


def build_system_tool_handlers(
    session_factory: async_sessionmaker[AsyncSession],
    status_provider: StatusProvider | None = None,
) -> dict[str, Any]:
    """Build runtime handlers for system tools."""

    async def list_agents_handler(
        arguments: dict[str, Any], context: ToolExecutionContext
    ) -> list[dict[str, str | None]]:
        del arguments
        owner_email = context.runtime_metadata.get("user_email")
        if not isinstance(owner_email, str):
            return []
        async with session_factory() as session:
            return await list_active_agents_summary(session, owner_email=owner_email)

    async def get_status_handler(
        arguments: dict[str, Any], context: ToolExecutionContext
    ) -> dict[str, Any]:
        del arguments
        extra_status = await status_provider(context) if status_provider is not None else {}
        return {
            "executor_id": context.executor_handle.executor_id,
            "executor_type": context.executor_handle.executor_type,
            "available_tools": context.executor_handle.capabilities.tools,
            "status": context.executor_handle.status,
            "details": extra_status,
        }

    return {
        LIST_AGENTS_TOOL.name: list_agents_handler,
        GET_STATUS_TOOL.name: get_status_handler,
    }
