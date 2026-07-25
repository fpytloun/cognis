"""Built-in controller tools for owner-scoped agent management."""

from __future__ import annotations

from typing import Any

from cognis.core.agent_management import (
    AgentManagementDependencies,
    AgentManagementError,
    handle_agent_management_action,
    result_to_json,
)
from cognis.models.tool import (
    NativeToolOperation,
    ToolCapability,
    ToolDefinition,
    ToolDynamicOption,
    ToolMutationKind,
    ToolResult,
    ToolSource,
    ToolValueSemantics,
)
from cognis.runtime_context import RuntimeAccessContext, current_runtime_access_context

_SOURCE = ToolSource(type="builtin")

_SETTINGS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "minProperties": 1,
    "additionalProperties": False,
    "properties": {
        "available_workflow_ids": {"type": "array", "items": {"type": "string"}},
        "default_workflow_id": {"type": ["string", "null"]},
        "workflow_selection_mode": {
            "type": "string",
            "enum": ["automatic", "always_ask", "use_default"],
        },
        "provider_id": {"type": ["string", "null"]},
        "model": {"type": ["string", "null"]},
        "temperature": {"type": ["number", "null"]},
        "max_tokens": {"type": ["integer", "null"]},
        "reasoning_effort": {"type": ["string", "null"]},
        "voice": {"type": ["string", "null"]},
        "memory_backend": {"type": "string"},
        "memory_backend_options": {"type": "object"},
    },
}

_RUNTIME_PROFILE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "description": {"type": "string"},
        "provider_id": {"type": ["string", "null"]},
        "model": {"type": ["string", "null"]},
        "reasoning_effort": {"type": ["string", "null"]},
        "system_prompt_extra": {"type": ["string", "null"]},
        "enabled": {"type": "boolean"},
        "agent_switchable": {"type": "boolean"},
    },
}

_DESCRIPTION = (
    "Owner-scoped management for Cognis agents. Use this to list, inspect, create, update, "
    "archive, activate, suspend, sync personality, manage secondary bindings, remove/generate "
    "avatars, and manage sharing grants for agents owned by the current user. This tool is "
    "only available to root primary agents owned by the caller. The list action includes each "
    "agent's available runtime profile IDs and descriptions; inspect it before passing an "
    "agent_profile_id to orchestration tools. Deleting means archive-only. Call describe_tool "
    "before unfamiliar or complex mutations."
)

_BASE_SCHEMA = {
    "type": "object",
    "properties": {
        "action": {
            "type": "string",
            "enum": [
                "list",
                "get",
                "settings_get",
                "settings_update",
                "runtime_profiles_list",
                "runtime_profiles_get",
                "runtime_profiles_create",
                "runtime_profiles_update",
                "runtime_profiles_delete",
                "runtime_profiles_default_set",
                "tools_get",
                "tools_set",
                "tools_add",
                "tools_remove",
                "knowledgebases_get",
                "knowledgebases_set",
                "knowledgebases_add",
                "knowledgebases_remove",
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
        "settings": {
            **_SETTINGS_SCHEMA,
            "description": (
                "UI-editable agent settings for settings_update. Call describe_tool on "
                "manage_agents to inspect operation semantics and dynamic option sources."
            ),
        },
        "profile_id": {"type": "string"},
        "replacement_profile_id": {
            "type": "string",
            "description": (
                "Optional enabled profile on the same agent. When supplied, exact live "
                "references to profile_id are atomically migrated before deletion."
            ),
        },
        "default_profile_id": {"type": ["string", "null"]},
        "profile": {
            **_RUNTIME_PROFILE_SCHEMA,
            "description": (
                "Runtime profile fields. profile_id is supplied separately and cannot be changed "
                "by update. Omitted update fields are preserved; null clears nullable fields."
            ),
        },
        "expected_updated_at": {
            "type": "string",
            "description": "Optional ISO-8601 agent revision from a prior read for optimistic concurrency.",
        },
        "tool_groups": {"type": "array", "items": {"type": "string"}},
        "allow_tools": {"type": "array", "items": {"type": "string"}},
        "deny_tools": {"type": "array", "items": {"type": "string"}},
        "knowledgebase_ids": {"type": "array", "items": {"type": "string"}},
        "assigned_knowledgebases": {"type": "array", "items": {"type": "string"}},
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
}

_READ_SEMANTICS = ToolValueSemantics(
    omitted="use the operation default or leave the value absent",
    null="accepted only where the operation schema explicitly permits null",
    arrays="arrays are filters or returned resource collections",
    concurrency="reads observe the current committed agent state",
)
_MUTATION_SEMANTICS = ToolValueSemantics(
    omitted="use the operation default or leave the value absent",
    null="accepted only where the operation schema explicitly permits null",
    arrays="replace supplied arrays unless this operation explicitly adds or removes entries",
    concurrency="validated against current agent state and committed atomically",
)
_PATCH_SEMANTICS = ToolValueSemantics(
    omitted="preserve every field not supplied by the caller",
    null="clear only fields whose operation schema explicitly permits null",
    arrays="replace supplied arrays",
    concurrency="validated against current agent state and committed atomically",
)


def _operation(
    name: str,
    *,
    kind: ToolMutationKind,
    fields: tuple[str, ...] = (),
    required: tuple[str, ...] = (),
    example: dict[str, Any],
    semantics: ToolValueSemantics | None = None,
    dynamic_options: list[ToolDynamicOption] | None = None,
    validator_ids: tuple[str, ...] = (),
    any_of: list[dict[str, Any]] | None = None,
) -> NativeToolOperation:
    properties = _BASE_SCHEMA["properties"]
    schema: dict[str, Any] = {
        "type": "object",
        "properties": {
            "action": {"type": "string", "const": name},
            **{field: properties[field] for field in fields},
        },
        "required": ["action", *required],
        "additionalProperties": False,
    }
    if any_of:
        schema["anyOf"] = any_of
    return NativeToolOperation(
        operation=name,
        summary=f"{name}: {_DESCRIPTION.split('.', 1)[0]}",
        mutation_kind=kind,
        input_schema=schema,
        semantics=semantics
        or (_READ_SEMANTICS if kind is ToolMutationKind.READ else _MUTATION_SEMANTICS),
        dynamic_options=dynamic_options or [],
        examples=[example],
        side_effects=[] if kind is ToolMutationKind.READ else ["May modify durable agent state."],
        validator_ids=list(validator_ids),
    )


_AGENT_ID = ("agent_id",)
_TOOL_FIELDS = ("agent_id", "tool_groups", "allow_tools", "deny_tools")
_KNOWLEDGEBASE_FIELDS = ("agent_id", "knowledgebase_ids")
_PROFILE_ID_FIELDS = ("agent_id", "profile_id")
_PROFILE_MUTATION_FIELDS = ("agent_id", "profile_id", "profile", "expected_updated_at")
_UPDATE_FIELDS = (
    "agent_id",
    "name",
    "display_name",
    "description",
    "system_prompt",
    "personality",
    "avatar_image_id",
    "status",
    "generate_avatar",
    "avatar_prompt",
    "avatar_size",
    "avatar_quality",
)

MANAGE_AGENTS_TOOL = ToolDefinition(
    name="manage_agents",
    description=_DESCRIPTION,
    parameters={},
    native_operations=[
        _operation("list", kind=ToolMutationKind.READ, example={"action": "list"}),
        _operation(
            "get",
            kind=ToolMutationKind.READ,
            fields=_AGENT_ID,
            required=_AGENT_ID,
            example={"action": "get", "agent_id": "current-agent"},
        ),
        _operation(
            "settings_get",
            kind=ToolMutationKind.READ,
            fields=_AGENT_ID,
            required=_AGENT_ID,
            example={"action": "settings_get", "agent_id": "current-agent"},
        ),
        _operation(
            "settings_update",
            kind=ToolMutationKind.UPDATE,
            fields=("agent_id", "settings"),
            required=("agent_id", "settings"),
            example={
                "action": "settings_update",
                "agent_id": "managed-agent",
                "settings": {"workflow_selection_mode": "automatic"},
            },
            semantics=_PATCH_SEMANTICS,
            dynamic_options=[
                ToolDynamicOption(
                    path="$.settings",
                    source="agent_management.settings_schema",
                )
            ],
            validator_ids=("manage_agents.settings_update",),
        ),
        _operation(
            "runtime_profiles_list",
            kind=ToolMutationKind.READ,
            fields=_AGENT_ID,
            required=_AGENT_ID,
            example={"action": "runtime_profiles_list", "agent_id": "managed-agent"},
            dynamic_options=[
                ToolDynamicOption(path="$.agent_id", source="agent_management.owned_agents")
            ],
        ),
        _operation(
            "runtime_profiles_get",
            kind=ToolMutationKind.READ,
            fields=_PROFILE_ID_FIELDS,
            required=_PROFILE_ID_FIELDS,
            example={
                "action": "runtime_profiles_get",
                "agent_id": "managed-agent",
                "profile_id": "fast",
            },
        ),
        _operation(
            "runtime_profiles_create",
            kind=ToolMutationKind.CREATE,
            fields=_PROFILE_MUTATION_FIELDS,
            required=("agent_id", "profile_id", "profile"),
            example={
                "action": "runtime_profiles_create",
                "agent_id": "managed-agent",
                "profile_id": "fast",
                "profile": {"description": "Fast low-cost tasks", "enabled": True},
            },
            dynamic_options=[
                ToolDynamicOption(
                    path="$.profile.provider_id", source="agent_management.llm_providers"
                ),
                ToolDynamicOption(
                    path="$.profile.model", source="agent_management.llm_provider_models"
                ),
            ],
        ),
        _operation(
            "runtime_profiles_update",
            kind=ToolMutationKind.UPDATE,
            fields=_PROFILE_MUTATION_FIELDS,
            required=("agent_id", "profile_id", "profile"),
            example={
                "action": "runtime_profiles_update",
                "agent_id": "managed-agent",
                "profile_id": "fast",
                "profile": {"reasoning_effort": "low"},
            },
            semantics=_PATCH_SEMANTICS,
            dynamic_options=[
                ToolDynamicOption(
                    path="$.profile.provider_id", source="agent_management.llm_providers"
                ),
                ToolDynamicOption(
                    path="$.profile.model", source="agent_management.llm_provider_models"
                ),
            ],
        ),
        _operation(
            "runtime_profiles_delete",
            kind=ToolMutationKind.DELETE,
            fields=(
                "agent_id",
                "profile_id",
                "replacement_profile_id",
                "expected_updated_at",
            ),
            required=_PROFILE_ID_FIELDS,
            example={
                "action": "runtime_profiles_delete",
                "agent_id": "managed-agent",
                "profile_id": "fast",
                "replacement_profile_id": "standard",
            },
        ),
        _operation(
            "runtime_profiles_default_set",
            kind=ToolMutationKind.UPDATE,
            fields=("agent_id", "default_profile_id", "expected_updated_at"),
            required=("agent_id", "default_profile_id"),
            example={
                "action": "runtime_profiles_default_set",
                "agent_id": "managed-agent",
                "default_profile_id": "fast",
            },
            semantics=_PATCH_SEMANTICS,
        ),
        _operation(
            "tools_get",
            kind=ToolMutationKind.READ,
            fields=_AGENT_ID,
            required=_AGENT_ID,
            example={"action": "tools_get", "agent_id": "current-agent"},
        ),
        *[
            _operation(
                action,
                kind=ToolMutationKind.UPDATE,
                fields=_TOOL_FIELDS,
                required=("agent_id",),
                example={"action": action, "agent_id": "managed-agent", "tool_groups": []},
                semantics=(_PATCH_SEMANTICS if action != "tools_set" else _MUTATION_SEMANTICS),
                dynamic_options=[
                    ToolDynamicOption(
                        path="$.tool_groups",
                        source="agent_management.tool_groups",
                    ),
                    ToolDynamicOption(
                        path="$.allow_tools",
                        source="agent_management.assignable_tools",
                    ),
                    ToolDynamicOption(
                        path="$.deny_tools",
                        source="agent_management.assignable_tools",
                    ),
                ],
                validator_ids=("manage_agents.tool_assignment",),
            )
            for action in ("tools_set", "tools_add", "tools_remove")
        ],
        _operation(
            "knowledgebases_get",
            kind=ToolMutationKind.READ,
            fields=_AGENT_ID,
            required=_AGENT_ID,
            example={"action": "knowledgebases_get", "agent_id": "current-agent"},
        ),
        *[
            _operation(
                action,
                kind=ToolMutationKind.UPDATE,
                fields=_KNOWLEDGEBASE_FIELDS,
                required=("agent_id", "knowledgebase_ids"),
                example={
                    "action": action,
                    "agent_id": "current-agent",
                    "knowledgebase_ids": [],
                },
                dynamic_options=[
                    ToolDynamicOption(
                        path="$.knowledgebase_ids",
                        source="agent_management.authorized_knowledgebases",
                    )
                ],
            )
            for action in (
                "knowledgebases_set",
                "knowledgebases_add",
                "knowledgebases_remove",
            )
        ],
        _operation(
            "create",
            kind=ToolMutationKind.CREATE,
            fields=tuple(field for field in _UPDATE_FIELDS if field not in {"agent_id", "status"})
            + ("agent_type", "assigned_knowledgebases"),
            required=("name",),
            example={"action": "create", "name": "New agent"},
            validator_ids=("manage_agents.create",),
        ),
        _operation(
            "update",
            kind=ToolMutationKind.UPDATE,
            fields=_UPDATE_FIELDS,
            required=("agent_id",),
            example={
                "action": "update",
                "agent_id": "current-agent",
                "description": "Updated description",
            },
            semantics=_PATCH_SEMANTICS,
            any_of=[
                {"required": [field]}
                for field in _UPDATE_FIELDS
                if field != "agent_id" and field != "generate_avatar"
            ]
            + [
                {
                    "properties": {"generate_avatar": {"const": True}},
                    "required": ["generate_avatar"],
                }
            ],
            validator_ids=("manage_agents.update",),
        ),
        *[
            _operation(
                action,
                kind=ToolMutationKind.UPDATE,
                fields=_AGENT_ID,
                required=_AGENT_ID,
                example={"action": action, "agent_id": "current-agent"},
            )
            for action in ("archive", "activate", "suspend", "sync_personality")
        ],
        _operation(
            "bindings_get",
            kind=ToolMutationKind.READ,
            fields=_AGENT_ID,
            required=_AGENT_ID,
            example={"action": "bindings_get", "agent_id": "current-agent"},
        ),
        _operation(
            "bindings_set",
            kind=ToolMutationKind.UPDATE,
            fields=("agent_id", "secondary_agent_ids"),
            required=("agent_id", "secondary_agent_ids"),
            example={
                "action": "bindings_set",
                "agent_id": "current-agent",
                "secondary_agent_ids": [],
            },
        ),
        _operation(
            "avatar_remove",
            kind=ToolMutationKind.DELETE,
            fields=_AGENT_ID,
            required=_AGENT_ID,
            example={"action": "avatar_remove", "agent_id": "current-agent"},
        ),
        _operation(
            "shares_list",
            kind=ToolMutationKind.READ,
            fields=_AGENT_ID,
            required=_AGENT_ID,
            example={"action": "shares_list", "agent_id": "current-agent"},
        ),
        _operation(
            "share_create",
            kind=ToolMutationKind.CREATE,
            fields=("agent_id", "grantee_email", "executor_scope", "note"),
            required=("agent_id", "grantee_email"),
            example={
                "action": "share_create",
                "agent_id": "current-agent",
                "grantee_email": "owner@example.com",
            },
        ),
        _operation(
            "share_update",
            kind=ToolMutationKind.UPDATE,
            fields=("agent_id", "grant_id", "executor_scope", "note"),
            required=("agent_id", "grant_id"),
            example={
                "action": "share_update",
                "agent_id": "current-agent",
                "grant_id": "grant-id",
            },
            semantics=_PATCH_SEMANTICS,
        ),
        _operation(
            "share_revoke",
            kind=ToolMutationKind.DELETE,
            fields=("agent_id", "grant_id"),
            required=("agent_id", "grant_id"),
            example={
                "action": "share_revoke",
                "agent_id": "current-agent",
                "grant_id": "grant-id",
            },
        ),
    ],
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
        return ToolResult(
            output=str(exc), is_error=True, metadata={"code": "agent_management_error"}
        )
    return ToolResult(output=result_to_json(result))
