"""Privileged controller tool for owner-scoped MCP management."""

from __future__ import annotations

import json
from typing import Any

from cognis.core.mcp_management import (
    MCPManagementDependencies,
    MCPManagementError,
    handle_mcp_management_action,
)
from cognis.models.tool import (
    MCPAuthConfig,
    NativeToolOperation,
    ToolCapability,
    ToolDefinition,
    ToolMutationKind,
    ToolResult,
    ToolSource,
    ToolValueSemantics,
)
from cognis.runtime_context import RuntimeAccessContext, current_runtime_access_context

_DESCRIPTION = (
    "Manage private Cognis MCP servers, OAuth authorization, and owned WebSocket executor "
    "assignments. This privileged tool is available only in root owner primary chats. "
    "Call describe_tool before mutations or OAuth actions to inspect the authoritative "
    "operation schema, semantics, side effects, and examples."
)
_FIELDS: dict[str, Any] = {
    "server_id": {"type": "string"},
    "executor_id": {"type": "string"},
    "server_ids": {"type": "array", "items": {"type": "string"}},
    "name": {"type": "string"},
    "description": {"type": ["string", "null"]},
    "transport": {"type": "string", "enum": ["stdio", "sse", "streamable_http"]},
    "command": {"type": ["string", "null"]},
    "url": {"type": ["string", "null"]},
    "args": {"type": "array", "items": {"type": "string"}},
    "env": {"type": "object", "additionalProperties": {"type": "string"}},
    "headers": {"type": "object", "additionalProperties": {"type": "string"}},
    "auth_config": {
        "anyOf": [
            MCPAuthConfig.model_json_schema(),
            {"type": "null"},
        ]
    },
    "timeout_seconds": {"type": "integer", "minimum": 1},
    "expected_updated_at": {"type": "string"},
    "expected_config_version": {"type": "integer", "minimum": 0},
}
_READ = ToolValueSemantics(
    omitted="leave optional filters absent",
    null="accepted only where the schema explicitly permits null",
    arrays="returned as current committed collections",
    concurrency="reads observe current committed state",
)
_MUTATE = ToolValueSemantics(
    omitted="preserve fields not supplied",
    null="clear only nullable fields",
    arrays="assignment set replaces; add/remove apply a deduplicated delta",
    concurrency="use the revision returned by a prior read; stale revisions fail",
)


def _operation(
    action: str,
    kind: ToolMutationKind,
    fields: tuple[str, ...],
    required: tuple[str, ...],
    example: dict[str, Any],
) -> NativeToolOperation:
    return NativeToolOperation(
        operation=action,
        summary=f"{action}: {_DESCRIPTION.split('.', 1)[0]}",
        mutation_kind=kind,
        input_schema={
            "type": "object",
            "properties": {
                "action": {"type": "string", "const": action},
                **{field: _FIELDS[field] for field in fields},
            },
            "required": ["action", *required],
            "additionalProperties": False,
        },
        semantics=_READ if kind is ToolMutationKind.READ else _MUTATE,
        examples=[example],
        side_effects=[]
        if kind is ToolMutationKind.READ
        else [
            "May persist MCP, OAuth, or executor configuration and trigger runtime reconfiguration."
        ],
    )


_SERVER_FIELDS = (
    "server_id",
    "name",
    "description",
    "transport",
    "command",
    "url",
    "args",
    "env",
    "headers",
    "auth_config",
    "timeout_seconds",
)
MANAGE_MCP_TOOL = ToolDefinition(
    name="manage_mcp",
    description=_DESCRIPTION,
    parameters={},
    native_operations=[
        _operation("servers_list", ToolMutationKind.READ, (), (), {"action": "servers_list"}),
        _operation(
            "servers_get",
            ToolMutationKind.READ,
            ("server_id",),
            ("server_id",),
            {"action": "servers_get", "server_id": "mcp_oura"},
        ),
        _operation("executors_list", ToolMutationKind.READ, (), (), {"action": "executors_list"}),
        _operation(
            "servers_create",
            ToolMutationKind.CREATE,
            _SERVER_FIELDS,
            ("name", "transport"),
            {
                "action": "servers_create",
                "name": "Oura",
                "transport": "streamable_http",
                "url": "https://mcp.example.test",
            },
        ),
        _operation(
            "servers_update",
            ToolMutationKind.UPDATE,
            _SERVER_FIELDS + ("expected_updated_at",),
            ("server_id", "expected_updated_at"),
            {
                "action": "servers_update",
                "server_id": "mcp_oura",
                "expected_updated_at": "2030-01-01T00:00:00+00:00",
                "name": "Oura",
            },
        ),
        _operation(
            "servers_delete",
            ToolMutationKind.DELETE,
            ("server_id", "expected_updated_at"),
            ("server_id", "expected_updated_at"),
            {
                "action": "servers_delete",
                "server_id": "mcp_oura",
                "expected_updated_at": "2030-01-01T00:00:00+00:00",
            },
        ),
        *[
            _operation(
                action,
                ToolMutationKind.READ if action == "assignments_get" else ToolMutationKind.UPDATE,
                ("executor_id", "server_ids", "expected_config_version"),
                ("executor_id",)
                if action == "assignments_get"
                else ("executor_id", "server_ids", "expected_config_version"),
                {
                    "action": action,
                    "executor_id": "executor_main",
                    **(
                        {}
                        if action == "assignments_get"
                        else {
                            "server_ids": ["mcp_oura"],
                            "expected_config_version": 1,
                        }
                    ),
                },
            )
            for action in (
                "assignments_get",
                "assignments_set",
                "assignments_add",
                "assignments_remove",
            )
        ],
        _operation(
            "oauth_authorize",
            ToolMutationKind.EXECUTE,
            ("server_id",),
            ("server_id",),
            {"action": "oauth_authorize", "server_id": "mcp_oura"},
        ),
        _operation(
            "oauth_status",
            ToolMutationKind.READ,
            ("server_id",),
            ("server_id",),
            {"action": "oauth_status", "server_id": "mcp_oura"},
        ),
        _operation(
            "oauth_disconnect",
            ToolMutationKind.DELETE,
            ("server_id",),
            ("server_id",),
            {"action": "oauth_disconnect", "server_id": "mcp_oura"},
        ),
    ],
    source=ToolSource(type="builtin"),
    category="mcp_management",
    profile_group="system",
    read_only=False,
    capabilities=[ToolCapability.PRIVILEGED, ToolCapability.WRITE],
    non_bypassable=True,
    timeout_seconds=120,
    max_result_size=100_000,
)


def mcp_management_tools() -> list[ToolDefinition]:
    return [MANAGE_MCP_TOOL]


def is_mcp_management_tool(name: str) -> bool:
    return name == "manage_mcp"


async def handle_mcp_management_tool(
    *,
    arguments: dict[str, Any],
    deps: MCPManagementDependencies,
    user_email: str | None,
    runtime_access: RuntimeAccessContext | None = None,
) -> ToolResult:
    access = runtime_access or current_runtime_access_context.get()
    if not access or not access.is_root_owner_primary_chat:
        return ToolResult(
            output="MCP management is only available in root owner primary conversations.",
            is_error=True,
            metadata={"code": "mcp_management_context_denied"},
        )
    actor = user_email or access.user_email
    if not actor:
        return ToolResult(output="MCP management runtime context is incomplete.", is_error=True)
    try:
        result = await handle_mcp_management_action(
            deps=deps, actor_email=actor, arguments=arguments
        )
    except MCPManagementError as exc:
        return ToolResult(output=str(exc), is_error=True, metadata={"code": "mcp_management_error"})
    return ToolResult(output=json.dumps(result, sort_keys=True, default=str))
