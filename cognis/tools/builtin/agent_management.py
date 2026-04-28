"""Built-in controller tools for owner-scoped agent management."""

from __future__ import annotations

from typing import Any

from cognis.core.agent_management import (
    AgentManagementDependencies,
    AgentManagementError,
    handle_agent_management_action,
    result_to_json,
)
from cognis.models.tool import ToolCapability, ToolDefinition, ToolResult, ToolSource
from cognis.runtime_context import RuntimeAccessContext, current_runtime_access_context

_SOURCE = ToolSource(type="builtin")

MANAGE_AGENTS_TOOL = ToolDefinition(
    name="manage_agents",
    description=(
        "Owner-scoped management for Cognis agents. Use this to list, inspect, create, update, "
        "archive, activate, suspend, sync personality, manage secondary bindings, remove/generate "
        "avatars, and manage sharing grants for agents owned by the current user. This tool is "
        "only available to root primary agents owned by the caller. Deleting means archive-only."
    ),
    parameters={
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": [
                    "list",
                    "get",
                    "create",
                    "update",
                    "archive",
                    "activate",
                    "suspend",
                    "sync_personality",
                    "bindings_get",
                    "bindings_set",
                    "avatar_remove",
                    "shares_list",
                    "share_create",
                    "share_update",
                    "share_revoke",
                ],
            },
            "agent_id": {"type": "string", "description": "Target agent ID."},
            "name": {"type": "string"},
            "display_name": {"type": "string"},
            "description": {"type": "string"},
            "system_prompt": {"type": "string"},
            "personality": {"type": "object"},
            "skills": {"type": "object"},
            "tools": {"type": "object"},
            "permissions": {"type": "object"},
            "llm_config": {"type": "object"},
            "execution": {"type": "object"},
            "avatar_image_id": {"type": "string"},
            "agent_type": {"type": "string", "enum": ["primary", "secondary"]},
            "status": {"type": "string"},
            "generate_avatar": {"type": "boolean"},
            "avatar_prompt": {"type": "string"},
            "avatar_size": {"type": "string"},
            "avatar_quality": {"type": "string"},
            "secondary_agent_ids": {"type": "array", "items": {"type": "string"}},
            "grantee_email": {"type": "string"},
            "grant_id": {"type": "string"},
            "executor_scope": {
                "type": "string",
                "enum": ["owner_executor", "grantee_executor"],
            },
            "note": {"type": "string"},
        },
        "required": ["action"],
    },
    source=_SOURCE,
    category="agent_management",
    profile_group="system",
    read_only=False,
    capabilities=[ToolCapability.PRIVILEGED, ToolCapability.WRITE],
    non_bypassable=True,
    timeout_seconds=120,
    max_result_size=100_000,
)

_TOOL_NAMES = {"manage_agents"}


def agent_management_tools() -> list[ToolDefinition]:
    """Return built-in agent-management tool definitions."""

    return [MANAGE_AGENTS_TOOL]


def is_agent_management_tool(name: str) -> bool:
    """Return whether a tool name belongs to agent management."""

    return name in _TOOL_NAMES


def agent_management_context_allowed(access_context: RuntimeAccessContext | None) -> bool:
    """Fail closed unless the current run is a root owner primary chat."""

    return bool(access_context and access_context.is_root_owner_primary_chat)


async def handle_agent_management_tool(
    *,
    tool_name: str,
    arguments: dict[str, Any],
    deps: AgentManagementDependencies,
    user_email: str | None,
    current_agent_id: str | None,
    runtime_access: RuntimeAccessContext | None = None,
) -> ToolResult:
    """Execute an agent-management tool call."""

    if not is_agent_management_tool(tool_name):
        return ToolResult(output=f"Unknown agent-management tool: {tool_name}", is_error=True)
    runtime_access = runtime_access or current_runtime_access_context.get()
    if not agent_management_context_allowed(runtime_access):
        return ToolResult(
            output=(
                "Agent management is only available in root conversations for primary agents "
                "owned by the current user."
            ),
            is_error=True,
            metadata={"code": "agent_management_context_denied"},
        )
    actor_email = user_email or runtime_access.user_email
    agent_id = current_agent_id or runtime_access.agent_id
    if not actor_email or not agent_id:
        return ToolResult(output="Agent management runtime context is incomplete.", is_error=True)
    try:
        result = await handle_agent_management_action(
            deps=deps,
            actor_email=actor_email,
            current_agent_id=agent_id,
            arguments=arguments,
        )
    except AgentManagementError as exc:
        return ToolResult(output=str(exc), is_error=True, metadata={"code": "agent_management_error"})
    return ToolResult(output=result_to_json(result))
