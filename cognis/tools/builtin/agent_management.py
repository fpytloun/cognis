"""Built-in controller tools for owner-scoped agent management."""

from __future__ import annotations

from typing import Any

from cognis.core.agent_management import (
    GUARDRAIL_SETTING_FIELDS,
    TOOL_ASSIGNMENT_FIELDS,
    AgentManagementDependencies,
    AgentManagementError,
    handle_agent_management_action,
    result_to_json,
)
from cognis.core.agent_registry import SYSTEM_AGENTS
from cognis.models.agent import AgentCapabilities, AutoProfileSelectionSettings
from cognis.models.hooks import HookAssignment, HookDefinition
from cognis.models.tool import (
    MCPServerConfig,
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

_GUARDRAIL_SCHEMA = {
    field: AgentCapabilities.model_json_schema()["properties"][field]
    for field in GUARDRAIL_SETTING_FIELDS
}

_SETTINGS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "minProperties": 1,
    "additionalProperties": False,
    "properties": {
        **_GUARDRAIL_SCHEMA,
        "auto_profile_selection": {
            **AutoProfileSelectionSettings.model_json_schema(),
            "type": ["object", "null"],
            "description": (
                "Startup profile selection settings. Enabled by default; min_confidence "
                "defaults to 0.85. Supplied fields patch this group; omitted fields are "
                "preserved. Null restores defaults. Only enabled, agent-switchable "
                "profiles are eligible. Low confidence keeps the configured default."
            ),
        },
        "executor_id": {
            "type": ["string", "null"],
            "minLength": 1,
            "description": (
                "Owner-visible or shared executor ID. Setting an ID clears the selector; "
                "null clears only this binding. Omission preserves it."
            ),
        },
        "executor_selector": {
            "type": ["object", "null"],
            "minProperties": 1,
            "propertyNames": {"type": "string", "minLength": 1},
            "additionalProperties": {"type": "string", "minLength": 1},
            "description": (
                "Non-empty label selector matching an owner-visible or shared executor. "
                "Setting a selector clears the ID; null clears only this binding. "
                "Do not set both executor fields to non-null values."
            ),
        },
        "provider_id": {"type": ["string", "null"]},
        "model": {"type": ["string", "null"]},
        "temperature": {"type": ["number", "null"]},
        "max_tokens": {"type": ["integer", "null"]},
        "reasoning_effort": {"type": ["string", "null"]},
        "voice": {"type": ["string", "null"]},
        "voices": {
            "type": "object",
            "additionalProperties": {"type": "string"},
            "description": (
                "Provider-scoped fallback voices when no explicit TTS binding/voice is set. "
                "Replaces the map; {} clears it. Omission preserves it."
            ),
        },
        "tts_provider_id": {
            "type": ["string", "null"],
            "description": (
                "Agent-only TTS provider, independent of chat and global routes. "
                "Use an existing provider ID and tts_model; null clears the provider override. "
                "Clear both provider and model to inherit the system TTS route."
            ),
        },
        "tts_model": {
            "type": ["string", "null"],
            "description": (
                "Agent-only synthesis model. Null clears the model override; also clear "
                "tts_provider_id to inherit the system TTS route."
            ),
        },
        "tts_voice": {
            "type": ["string", "null"],
            "description": (
                "Voice ID for the selected TTS provider/model. Null uses existing voice "
                "inheritance/provider defaults. Unavailable voices fall back at synthesis "
                "with a warning, as in the UI."
            ),
        },
        "memory_backend": {"type": "string"},
        "memory_backend_options": {"type": "object"},
        "analytics_enabled": {
            "type": "boolean",
            "description": "Enable session analytics independently of guardrails. Omission preserves it.",
        },
        "escalation_timeout_seconds": {
            "type": ["integer", "null"],
            "minimum": 1,
            "maximum": 86400,
            "description": "Seconds to wait for an agent escalation; null inherits the global timeout.",
        },
        "delegation": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "enabled": {"type": "boolean"},
                "allowed_agent_ids": {
                    "type": ["array", "null"],
                    "items": {"type": "string"},
                },
                "primary": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "enabled": {"type": "boolean"},
                        "max_managed_depth": {"type": "integer", "minimum": 0, "maximum": 2},
                        "allowed_controller_agent_ids": {
                            "type": ["array", "null"],
                            "items": {"type": "string"},
                        },
                    },
                },
                "system": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {"enabled": {"type": "boolean"}},
                },
            },
        },
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
        "quick_switch": {
            "type": "boolean",
            "description": "Show this enabled profile in the chat quick switcher, independently of agent_switchable.",
        },
        "quick_switch_index": {
            "type": "integer",
            "minimum": 0,
            "description": "Position in the chat quick switcher, ascending; equal indexes sort by profile ID.",
        },
        "quick_switch_color": {
            "type": "string",
            "enum": ["neutral", "blue", "teal", "amber", "violet", "rose"],
            "description": "Accent color of this profile in the chat quick switcher. Defaults to neutral.",
        },
    },
}

_DESCRIPTION = (
    "Owner-scoped management for Cognis agents. Use this to list, inspect, create, update, "
    "archive, activate, suspend, sync personality, manage secondary bindings, remove/generate "
    "avatars, manage credential assignments without exposing secrets, and manage sharing grants "
    "for primary and secondary agents owned by the current user. Shipped system:* agents are "
    "managed through per-user overrides with the system_overrides_get, "
    "system_overrides_update, system_overrides_reset, system_disable, and system_enable "
    "actions. This tool is "
    "only available to root primary agents owned by the caller. The list action includes each "
    "agent's available runtime profile IDs and descriptions; inspect it before passing an "
    "agent_profile_id to orchestration tools. Deleting means archive-only. Call describe_tool "
    "before unfamiliar or complex mutations."
)

_SYSTEM_OVERRIDES_SCHEMA: dict[str, Any] = {
    "type": "object",
    "minProperties": 1,
    "additionalProperties": False,
    "description": (
        "Per-user override patch for a shipped system agent. Omitted fields keep the stored "
        "override; null clears that override so the shipped value applies. Supplied objects "
        "replace the whole field. Inspect system_overrides_get first."
    ),
    "properties": {
        "llm_config": {
            "type": ["object", "null"],
            "additionalProperties": False,
            "properties": {
                "provider_id": {"type": ["string", "null"]},
                "model": {"type": ["string", "null"]},
                "temperature": {"type": ["number", "null"]},
                "top_p": {"type": ["number", "null"]},
                "max_tokens": {"type": ["integer", "null"]},
                "reasoning_effort": {"type": ["string", "null"]},
                "fast_mode": {"type": ["boolean", "null"]},
                "tts_provider_id": _SETTINGS_SCHEMA["properties"]["tts_provider_id"],
                "tts_model": _SETTINGS_SCHEMA["properties"]["tts_model"],
                "tts_voice": _SETTINGS_SCHEMA["properties"]["tts_voice"],
            },
        },
        "skills": {
            "type": ["object", "null"],
            "additionalProperties": False,
            "required": ["items"],
            "properties": {
                "items": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "additionalProperties": False,
                        "properties": {
                            "skill_id": {"type": "string", "minLength": 1},
                            "enabled": {"type": "boolean"},
                            "auto_load_instructions": {"type": "boolean"},
                        },
                        "required": ["skill_id"],
                    },
                }
            },
            "description": "Complete skill assignment list for the system agent.",
        },
        "tools": {
            "type": ["object", "null"],
            "additionalProperties": False,
            "required": ["builtin_tools"],
            "properties": {
                field: {"type": "array", "items": {"type": "string"}}
                for field in (
                    "builtin_tools",
                    "allow_tools",
                    "deny_tools",
                    "tool_groups",
                    "disabled_tools",
                    "disabled_categories",
                    "disabled_mcp_servers",
                )
            },
            "description": (
                "Complete tool config; builtin_tools is the explicit enabled builtin list. "
                "Tools beyond the shipped set must be in the caller-effective assignable "
                "inventory. Management tools are never allowed."
            ),
        },
        "permissions": {"type": ["object", "null"]},
        "agent_profiles": {
            "type": ["object", "null"],
            "additionalProperties": {"type": "object"},
            "description": "Complete runtime profile map keyed by profile ID.",
        },
        "default_agent_profile_id": {"type": ["string", "null"]},
    },
}

_BASE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "action": {
            "type": "string",
            "enum": [
                "list",
                "get",
                "credentials_get",
                "credentials_grant",
                "credentials_revoke",
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
                "skills_get",
                "skills_set",
                "skills_add",
                "skills_update",
                "skills_remove",
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
                "system_overrides_get",
                "system_overrides_update",
                "system_overrides_reset",
                "system_disable",
                "system_enable",
            ],
        },
        "agent_id": {"type": "string", "description": "Target agent ID."},
        "credential_id": {
            "type": "string",
            "minLength": 1,
            "description": "Exact existing credential ID owned by the caller; never a secret value.",
        },
        "name": {"type": "string"},
        "display_name": {"type": "string"},
        "description": {"type": "string"},
        "system_prompt": {"type": "string"},
        "personality": {"type": "object"},
        "skills": {"type": "object"},
        "tools": {"type": "object"},
        "permissions": {"type": "object"},
        "delegation": _SETTINGS_SCHEMA["properties"]["delegation"],
        "llm_config": {"type": "object"},
        "execution": {"type": "object"},
        "auto_profile_selection": _SETTINGS_SCHEMA["properties"]["auto_profile_selection"],
        **_GUARDRAIL_SCHEMA,
        **{
            field: {"type": "array", "items": {"type": "string"}}
            for field in ("disabled_tools", "disabled_mcp_servers", "intaris_mcp_servers")
        },
        "mcp_servers": {
            "type": "array",
            "items": MCPServerConfig.model_json_schema(),
            "description": (
                "Legacy agent-local MCP configuration. Runtime connections are managed on executors; "
                "this does not provision or start servers."
            ),
        },
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
            "description": (
                "Optional ISO-8601 agent revision from a prior read for optimistic concurrency. "
                "For system_* actions use override_updated_at from system_overrides_get; an "
                "empty string asserts that no override exists yet."
            ),
        },
        "overrides": _SYSTEM_OVERRIDES_SCHEMA,
        "tool_groups": {"type": "array", "items": {"type": "string"}},
        "allow_tools": {"type": "array", "items": {"type": "string"}},
        "deny_tools": {"type": "array", "items": {"type": "string"}},
        "knowledgebase_ids": {"type": "array", "items": {"type": "string"}},
        "skill_ids": {"type": "array", "items": {"type": "string"}},
        "skill_assignments": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "skill_id": {"type": "string", "minLength": 1},
                    "enabled": {"type": "boolean"},
                    "auto_load_instructions": {"type": "boolean"},
                },
                "required": ["skill_id"],
            },
        },
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
    field_schemas: dict[str, dict[str, Any]] | None = None,
) -> NativeToolOperation:
    properties = {**_BASE_SCHEMA["properties"], **(field_schemas or {})}
    schema: dict[str, Any] = {
        "type": "object",
        "properties": {
            "action": {"type": "string", "const": name},
            **{field: properties[field] for field in fields},
        },
        "required": ["action", *required],
        "additionalProperties": False,
    }
    if name.startswith("hooks_") or name.startswith("hook_assignments_"):
        schema["$defs"] = HookDefinition.model_json_schema().get("$defs", {})
    if "mcp_servers" in fields:
        schema["$defs"] = MCPServerConfig.model_json_schema().get("$defs", {})
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
_SYSTEM_AGENT_ID_SCHEMAS: dict[str, dict[str, Any]] = {
    "agent_id": {
        "type": "string",
        "enum": sorted(
            agent.agent_id for agent in SYSTEM_AGENTS.values() if agent.allow_user_override
        ),
        "description": "Shipped system agent ID that accepts per-user overrides.",
    }
}
_SYSTEM_REVISION_SEMANTICS = ToolValueSemantics(
    omitted="agent_id is required; expected_updated_at is optional",
    null="not accepted",
    arrays="not accepted",
    concurrency=(
        "optional compare-and-swap on the override revision from system_overrides_get; "
        "re-read after conflict"
    ),
)
_TOOL_FIELDS = ("agent_id", *TOOL_ASSIGNMENT_FIELDS)
_SKILL_FIELDS = ("agent_id", "skill_assignments")
_KNOWLEDGEBASE_FIELDS = ("agent_id", "knowledgebase_ids")
_PROFILE_ID_FIELDS = ("agent_id", "profile_id")
_PROFILE_MUTATION_FIELDS = ("agent_id", "profile_id", "profile", "expected_updated_at")
_UPDATE_FIELDS = (
    "agent_id",
    "auto_profile_selection",
    "name",
    "display_name",
    "description",
    "system_prompt",
    "personality",
    "avatar_image_id",
    "status",
    "delegation",
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
        *[
            _operation(
                name,
                kind=ToolMutationKind.READ
                if name
                in {
                    "hooks_list",
                    "hooks_get",
                    "hook_assignments_get",
                    "hook_runs_list",
                }
                else ToolMutationKind.UPDATE,
                fields=fields,
                required=required,
                example={
                    "action": name,
                    **{
                        field: {
                            "agent_id": "my-agent",
                            "hook_id": "hook_working_reminder",
                            "expected_revision": 1,
                            "definition": {
                                "name": "Working reminder",
                                "trigger": "user_message",
                                "prompt": "Verify results before claiming success.",
                            },
                            "assignment": {
                                "hook_id": "system:working-reminder",
                                "enabled": True,
                            },
                        }[field]
                        for field in required
                    },
                },
                field_schemas={
                    "hook_id": {"type": "string"},
                    "definition": HookDefinition.model_json_schema(),
                    "assignment": HookAssignment.model_json_schema(),
                    "expected_revision": {"type": "integer", "minimum": 1},
                },
            )
            for name, fields, required in [
                ("hooks_list", (), ()),
                ("hooks_get", ("hook_id",), ("hook_id",)),
                ("hooks_create", ("definition",), ("definition",)),
                (
                    "hooks_update",
                    ("hook_id", "definition", "expected_revision"),
                    ("hook_id", "definition", "expected_revision"),
                ),
                (
                    "hooks_delete",
                    ("hook_id", "expected_revision"),
                    ("hook_id", "expected_revision"),
                ),
                ("hook_assignments_get", ("agent_id",), ("agent_id",)),
                ("hook_assignments_put", ("agent_id", "assignment"), ("agent_id", "assignment")),
                ("hook_assignments_remove", ("agent_id", "hook_id"), ("agent_id", "hook_id")),
                ("hook_runs_list", ("agent_id", "hook_id"), ()),
            ]
        ],
        _operation(
            "credentials_get",
            kind=ToolMutationKind.READ,
            fields=("agent_id", "credential_id"),
            required=("agent_id", "credential_id"),
            example={
                "action": "credentials_get",
                "agent_id": "travel",
                "credential_id": "travel-account",
            },
        ),
        *[
            _operation(
                action,
                kind=ToolMutationKind.UPDATE,
                fields=("agent_id", "credential_id", "expected_updated_at"),
                required=("agent_id", "credential_id", "expected_updated_at"),
                example={
                    "action": action,
                    "agent_id": "travel",
                    "credential_id": "travel-account",
                    "expected_updated_at": "2030-01-01T00:00:00+00:00",
                },
                semantics=ToolValueSemantics(
                    omitted="all fields are required; first read credentials_get",
                    null="not accepted",
                    arrays="not accepted; changes exactly one credential assignment",
                    concurrency=(
                        "compare-and-swap on expected_updated_at; re-read after conflict; "
                        "idempotent desired state, including retries with a fresh revision"
                    ),
                ),
            )
            for action in ("credentials_grant", "credentials_revoke")
        ],
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
                "settings": {"temperature": 0.2},
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
            "skills_get",
            kind=ToolMutationKind.READ,
            fields=_AGENT_ID,
            required=_AGENT_ID,
            example={"action": "skills_get", "agent_id": "current-agent"},
        ),
        *[
            _operation(
                action,
                kind=ToolMutationKind.UPDATE,
                fields=_SKILL_FIELDS,
                required=_SKILL_FIELDS,
                example={
                    "action": action,
                    "agent_id": "managed-agent",
                    "skill_assignments": [
                        {
                            "skill_id": "cognis-coding",
                            "enabled": True,
                            "auto_load_instructions": True,
                        }
                    ],
                },
                semantics=(_MUTATION_SEMANTICS if action == "skills_set" else _PATCH_SEMANTICS),
                dynamic_options=[
                    ToolDynamicOption(
                        path="$.skill_assignments[*].skill_id",
                        source="agent_management.available_skills",
                    )
                ],
            )
            for action in ("skills_set", "skills_add", "skills_update")
        ],
        _operation(
            "skills_remove",
            kind=ToolMutationKind.UPDATE,
            fields=("agent_id", "skill_ids"),
            required=("agent_id", "skill_ids"),
            example={
                "action": "skills_remove",
                "agent_id": "managed-agent",
                "skill_ids": ["cognis-coding"],
            },
            semantics=_PATCH_SEMANTICS,
            dynamic_options=[
                ToolDynamicOption(
                    path="$.skill_ids",
                    source="agent_management.available_skills",
                )
            ],
        ),
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
            + ("agent_type", "assigned_knowledgebases", *GUARDRAIL_SETTING_FIELDS),
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
        _operation(
            "system_overrides_get",
            kind=ToolMutationKind.READ,
            fields=_AGENT_ID,
            required=_AGENT_ID,
            example={"action": "system_overrides_get", "agent_id": "system:explore"},
            field_schemas={
                "agent_id": {
                    "type": "string",
                    "description": "Shipped system agent ID (system:*).",
                }
            },
        ),
        _operation(
            "system_overrides_update",
            kind=ToolMutationKind.UPDATE,
            fields=("agent_id", "overrides", "expected_updated_at"),
            required=("agent_id", "overrides"),
            example={
                "action": "system_overrides_update",
                "agent_id": "system:explore",
                "overrides": {"llm_config": {"reasoning_effort": "medium"}},
            },
            semantics=ToolValueSemantics(
                omitted="preserve every stored override field not supplied",
                null="clears that override field so the shipped value applies",
                arrays="supplied override objects replace the stored override field",
                concurrency=_SYSTEM_REVISION_SEMANTICS.concurrency,
            ),
            dynamic_options=[
                ToolDynamicOption(
                    path="$.overrides.llm_config.provider_id",
                    source="agent_management.llm_providers",
                ),
                ToolDynamicOption(
                    path="$.overrides.skills.items[*].skill_id",
                    source="agent_management.available_skills",
                ),
                ToolDynamicOption(
                    path="$.overrides.tools.builtin_tools",
                    source="agent_management.assignable_tools",
                ),
            ],
            field_schemas=_SYSTEM_AGENT_ID_SCHEMAS,
        ),
        *[
            _operation(
                action,
                kind=kind,
                fields=("agent_id", "expected_updated_at"),
                required=_AGENT_ID,
                example={"action": action, "agent_id": "system:explore"},
                semantics=_SYSTEM_REVISION_SEMANTICS,
                field_schemas=_SYSTEM_AGENT_ID_SCHEMAS,
            )
            for action, kind in (
                ("system_overrides_reset", ToolMutationKind.DELETE),
                ("system_disable", ToolMutationKind.UPDATE),
                ("system_enable", ToolMutationKind.UPDATE),
            )
        ],
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
    self_mutation_approved: bool = False,
) -> ToolResult:
    """Execute an agent-management tool call."""
    from cognis.core.intaris_policy import profile_mutation_error

    policy_arguments = dict(arguments)
    if arguments.get("action") == "create":
        for field in GUARDRAIL_SETTING_FIELDS:
            policy_arguments.pop(field, None)
    elif arguments.get("action") == "settings_update" and isinstance(
        arguments.get("settings"), dict
    ):
        policy_arguments["settings"] = {
            field: value
            for field, value in arguments["settings"].items()
            if field not in GUARDRAIL_SETTING_FIELDS
        }
    profile_error = profile_mutation_error(policy_arguments)
    if profile_error is not None:
        return profile_error

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
    assert runtime_access is not None
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
            self_mutation_approved=self_mutation_approved,
        )
    except AgentManagementError as exc:
        return ToolResult(
            output=str(exc), is_error=True, metadata={"code": "agent_management_error"}
        )
    return ToolResult(output=result_to_json(result))
