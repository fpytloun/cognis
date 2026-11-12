"""Shared agent-management operations for API routes and controller tools."""

from __future__ import annotations

import asyncio
import contextlib
import re
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, cast

from sqlalchemy import select, update

from cognis import json_codec as json
from cognis.api.error_sanitizer import sanitize_client_error_detail
from cognis.api.serializers import agent_to_response
from cognis.core.agent_management_catalog import assignable_knowledgebases
from cognis.core.agent_profiles import (
    agent_profile_options,
    resolve_agent_profile,
    validate_agent_profile_configuration,
)
from cognis.core.agent_registry import SYSTEM_AGENTS, AgentRegistry, validate_agent_id
from cognis.core.credential_grants import (
    CredentialGrantError,
    credential_assignment_metadata,
    update_credential_assignment,
)
from cognis.core.events import Event, EventType
from cognis.core.executor_resolution import labels_match
from cognis.core.system_agent_overrides import (
    SYSTEM_AGENT_OVERRIDE_FIELDS,
    SystemAgentOverrideError,
    require_system_agent,
    reset_system_agent_override,
    set_system_agent_disabled,
    system_override_payload,
    update_system_agent_override,
)
from cognis.logging import get_logger
from cognis.models.agent import (
    AgentCapabilities,
    AgentDefinition,
    AgentDelegationPolicy,
    AgentRuntimeProfile,
    auto_profile_selection_settings,
    strip_retired_execution_settings,
)
from cognis.models.skill import AgentSkillRef
from cognis.models.tool import MCPServerConfig, stable_tool_id
from cognis.providers.backends import get_backend
from cognis.providers.base import CredentialsProvider
from cognis.providers.memory.policy import memory_backend_descriptors
from cognis.store.models import (
    AuditLog,
    ChannelAccountRow,
    Conversation,
    Schedule,
    Session,
    Task,
)
from cognis.store.queries import (
    create_agent,
    create_agent_grant,
    get_agent,
    get_agent_grant,
    get_agent_grant_for_user,
    get_system_agent_override,
    get_user,
    list_agent_grants,
    list_agents,
    list_executors,
    list_llm_providers,
    list_secondary_bindings,
    list_skills,
    revoke_agent_grant,
    set_agent_status,
    set_secondary_bindings,
    update_agent,
    update_agent_grant,
    update_agent_if_updated_at,
)
from cognis.tools.builtin.image import _image_bytes
from cognis.tools.builtin.knowledgebase import knowledgebase_tools
from cognis.tools.builtin.projects import PROJECT_MANAGE_TOOL_IDS, PROJECT_READ_TOOL_IDS
from cognis.tools.builtin.system_management import SYSTEM_MANAGEMENT_TOOLS
from cognis.tools.skills import agent_skill_usage_report, is_agent_assignable_skill

logger = get_logger(__name__)


KNOWLEDGEBASE_READ_TOOL_IDS = (
    "builtin:knowledgebase_list",
    "builtin:knowledgebase_get",
    "builtin:knowledgebase_list_artifacts",
    "builtin:knowledgebase_list_jobs",
    "builtin:knowledgebase_status",
    "builtin:knowledgebase_diagnostics",
    "builtin:knowledgebase_search",
    "builtin:knowledgebase_read_source_context",
)


GUARDRAIL_SETTING_FIELDS = (
    "guardrails_backend",
    "intaris_evaluation",
    "approval_mode",
    "minimum_outcome",
    "maximum_outcome",
)

TOOL_ASSIGNMENT_FIELDS = (
    "tool_groups",
    "allow_tools",
    "deny_tools",
    "disabled_tools",
    "disabled_mcp_servers",
    "intaris_mcp_servers",
    "mcp_servers",
)


class AgentManagementError(ValueError):
    """Expected user-facing agent-management failure."""


@dataclass(frozen=True, slots=True)
class ToolGroupDefinition:
    """Curated agent tool assignment preset."""

    group_id: str
    name: str
    description: str
    tool_ids: tuple[str, ...]
    risk_level: str
    mutating: bool = False
    requires_executor: bool = False


TOOL_GROUP_DEFINITIONS: tuple[ToolGroupDefinition, ...] = (
    ToolGroupDefinition(
        group_id="knowledgebase_read",
        name="Knowledgebase read/search",
        description="Read, inspect, search, and cite assigned knowledgebases without mutation.",
        tool_ids=KNOWLEDGEBASE_READ_TOOL_IDS,
        risk_level="low",
    ),
    ToolGroupDefinition(
        group_id="knowledgebase_manage",
        name="Knowledgebase management",
        description="Full built-in knowledgebase management, including artifact attachment, indexing, and deletion.",
        tool_ids=tuple(stable_tool_id(tool) for tool in knowledgebase_tools()),
        risk_level="high",
        mutating=True,
    ),
    ToolGroupDefinition(
        group_id="office",
        name="Office documents",
        description="Create, inspect, validate, render, and modify Office documents using executor-native OfficeCLI tools.",
        tool_ids=(
            "builtin:office_read",
            "builtin:office_get",
            "builtin:office_query",
            "builtin:office_validate",
            "builtin:office_render",
            "builtin:office_create",
            "builtin:office_patch",
        ),
        risk_level="medium",
        mutating=True,
        requires_executor=True,
    ),
    ToolGroupDefinition(
        group_id="projects_read",
        name="Projects read",
        description="List and inspect owned and shared projects.",
        tool_ids=PROJECT_READ_TOOL_IDS,
        risk_level="low",
    ),
    ToolGroupDefinition(
        group_id="projects_manage",
        name="Projects management",
        description=(
            "Create, edit, and archive projects; manage instructions, sources, workflows, "
            "agent assignments, and the current conversation's project."
        ),
        tool_ids=PROJECT_MANAGE_TOOL_IDS,
        risk_level="high",
        mutating=True,
    ),
)


@dataclass(slots=True)
class AgentManagementDependencies:
    """External dependencies needed for agent-management side effects."""

    session_factory: Any
    memory: Any | None = None
    event_bus: Any | None = None
    artifact_store: Any | None = None
    image_generation_provider: Any | None = None
    llm: Any | None = None
    task_queue: Any | None = None
    guardrails: Any | None = None
    # Caller-effective, persistable tools. Mutations fail closed when absent.
    assignable_tools: list[Any] | None = None
    assignable_knowledgebase_ids: set[str] | None = None
    caller_agent_id: str | None = None
    credentials: CredentialsProvider | None = None


def slugify_agent_id(text: str) -> str:
    slug = text.lower().strip()
    slug = re.sub(r"[^\w\s-]", "", slug)
    slug = re.sub(r"[\s_]+", "-", slug)
    slug = re.sub(r"-+", "-", slug)
    return slug.strip("-")[:64] or "unnamed"


def sync_metadata(synced: bool, error_detail: str | None = None) -> dict[str, object]:
    return {
        "personality_synced": synced,
        "personality_sync_error": error_detail,
        "personality_sync_checked_at": datetime.now(UTC).isoformat(),
    }


async def handle_agent_management_action(
    *,
    deps: AgentManagementDependencies,
    actor_email: str,
    current_agent_id: str,
    arguments: dict[str, Any],
    self_mutation_approved: bool = False,
) -> dict[str, Any]:
    """Execute a single owner-scoped agent-management action."""

    action = str(arguments.get("action") or "").strip()
    if str(arguments.get("agent_id") or "").strip() == current_agent_id:
        from cognis.core.chat_modes import is_plan_mutating_tool_call
        from cognis.tools.builtin.agent_management import MANAGE_AGENTS_TOOL

        if is_plan_mutating_tool_call(MANAGE_AGENTS_TOOL, arguments) and not self_mutation_approved:
            raise AgentManagementError("Self mutation requires explicit user approval.")
    if not action:
        raise AgentManagementError("action is required")

    try:
        if action.startswith(("hooks_", "hook_assignments_")) or action == "hook_runs_list":
            from cognis.core.hooks import (
                HookError,
                hook_affects_agent,
                manage_hooks,
                notify_hook_change,
            )

            async with deps.session_factory() as db:
                hook_id = str(arguments.get("hook_id") or "")
                if (
                    action in {"hooks_update", "hooks_delete"}
                    and (await hook_affects_agent(db, hook_id, current_agent_id))
                    and not self_mutation_approved
                ):
                    raise AgentManagementError("Self mutation requires explicit user approval.")
                try:
                    result = await manage_hooks(
                        db,
                        owner=actor_email,
                        action=action,
                        **{
                            key: value
                            for key, value in arguments.items()
                            if key
                            in {
                                "hook_id",
                                "definition",
                                "agent_id",
                                "assignment",
                                "expected_revision",
                            }
                        },
                    )
                except HookError as exc:
                    raise AgentManagementError(str(exc)) from exc
                await db.commit()
            await notify_hook_change(deps.event_bus, action, arguments)
            return result
        if action == "list":
            return await _list_agents(deps, actor_email, current_agent_id)
        if action == "get":
            if str(arguments.get("agent_id") or "").strip() in SYSTEM_AGENTS:
                return await _system_agent_action(
                    deps, actor_email, {**arguments, "action": "system_overrides_get"}
                )
            return await _get_agent(deps, actor_email, current_agent_id, arguments)
        if action in _SYSTEM_AGENT_ACTIONS:
            return await _system_agent_action(deps, actor_email, arguments)
        if action in {"credentials_get", "credentials_grant", "credentials_revoke"}:
            return await _credentials_action(deps, actor_email, current_agent_id, arguments)
        if action == "settings_get":
            return await _settings_get(deps, actor_email, current_agent_id, arguments)
        if action == "settings_update":
            return await _settings_update(deps, actor_email, current_agent_id, arguments)
        if action == "runtime_profiles_list":
            return await _runtime_profiles_list(deps, actor_email, current_agent_id, arguments)
        if action == "runtime_profiles_get":
            return await _runtime_profiles_get(deps, actor_email, current_agent_id, arguments)
        if action in {"runtime_profiles_create", "runtime_profiles_update"}:
            return await _runtime_profiles_write(
                deps,
                actor_email,
                current_agent_id,
                arguments,
                create=action == "runtime_profiles_create",
            )
        if action == "runtime_profiles_delete":
            return await _runtime_profiles_delete(deps, actor_email, current_agent_id, arguments)
        if action == "runtime_profiles_default_set":
            return await _runtime_profiles_default_set(
                deps, actor_email, current_agent_id, arguments
            )
        if action == "tools_get":
            return await _tools_get(deps, actor_email, current_agent_id, arguments)
        if action in {"tools_set", "tools_add", "tools_remove"}:
            return await _tools_update(
                deps, actor_email, current_agent_id, arguments, mode=action.removeprefix("tools_")
            )
        if action == "skills_get":
            return await _skills_get(deps, actor_email, current_agent_id, arguments)
        if action in {"skills_set", "skills_add", "skills_update", "skills_remove"}:
            return await _skills_update(
                deps, actor_email, current_agent_id, arguments, mode=action.removeprefix("skills_")
            )
        if action == "knowledgebases_get":
            return await _knowledgebases_get(deps, actor_email, current_agent_id, arguments)
        if action in {"knowledgebases_set", "knowledgebases_add", "knowledgebases_remove"}:
            return await _knowledgebases_update(
                deps,
                actor_email,
                current_agent_id,
                arguments,
                mode=action.removeprefix("knowledgebases_"),
            )
        if action == "create":
            return await _create_agent(deps, actor_email, arguments)
        if action == "update":
            return await _update_agent(deps, actor_email, current_agent_id, arguments)
        if action == "archive":
            return await _set_status(deps, actor_email, current_agent_id, arguments, "archived")
        if action == "activate":
            return await _activate_agent(deps, actor_email, current_agent_id, arguments)
        if action == "suspend":
            return await _set_status(deps, actor_email, current_agent_id, arguments, "suspended")
        if action == "sync_personality":
            return await _sync_personality_action(deps, actor_email, current_agent_id, arguments)
        if action == "bindings_get":
            return await _bindings_get(deps, actor_email, current_agent_id, arguments)
        if action == "bindings_set":
            return await _bindings_set(deps, actor_email, current_agent_id, arguments)
        if action == "avatar_remove":
            return await _avatar_remove(deps, actor_email, current_agent_id, arguments)
        if action == "shares_list":
            return await _shares_list(deps, actor_email, current_agent_id, arguments)
        if action == "share_create":
            return await _share_create(deps, actor_email, current_agent_id, arguments)
        if action == "share_update":
            return await _share_update(deps, actor_email, current_agent_id, arguments)
        if action == "share_revoke":
            return await _share_revoke(deps, actor_email, current_agent_id, arguments)
    except AgentManagementError:
        await _audit(deps, actor_email, current_agent_id, action, "denied", arguments)
        raise

    raise AgentManagementError(f"Unknown action: {action}")


async def _credentials_action(
    deps: AgentManagementDependencies,
    actor_email: str,
    current_agent_id: str,
    arguments: dict[str, Any],
) -> dict[str, Any]:
    row = await _require_owned_target(deps, actor_email, current_agent_id, arguments)
    credential_id = _optional_string(arguments.get("credential_id"))
    if not credential_id:
        raise AgentManagementError("credential_id is required")
    if deps.credentials is None:
        raise AgentManagementError("Credential management is unavailable")
    credential = await deps.credentials.get_credential(credential_id, actor_email)
    action = arguments["action"]
    try:
        metadata = credential_assignment_metadata(credential, owner_email=actor_email)
        if action == "credentials_get":
            permissions = row.permissions or {}
            return {
                "agent_id": row.agent_id,
                "credential": metadata,
                "assigned": credential_id in (permissions.get("allowed_credentials") or []),
                "updated_at": row.updated_at.isoformat(),
            }
        expected_updated_at = arguments.get("expected_updated_at")
        if not isinstance(expected_updated_at, str):
            raise CredentialGrantError("expected_updated_at must be an ISO-8601 timestamp")
        async with deps.session_factory() as session:
            result = await update_credential_assignment(
                session,
                agent_id=row.agent_id,
                owner_email=actor_email,
                credential=credential,
                grant=action == "credentials_grant",
                expected_updated_at=expected_updated_at,
            )
            await session.commit()
    except CredentialGrantError as exc:
        raise AgentManagementError(str(exc)) from exc
    await _audit(deps, actor_email, row.agent_id, action, "success", arguments)
    return result


async def _list_agents(
    deps: AgentManagementDependencies, actor_email: str, current_agent_id: str
) -> dict[str, Any]:
    async with deps.session_factory() as session:
        rows = await list_agents(session, owner_email=actor_email)
    agent_definitions = {
        row.agent_id: AgentDefinition.model_validate(agent_to_response(row).model_dump())
        for row in rows
        if not getattr(row, "is_system", False)
    }
    return {
        "status": "ok",
        "agents": [
            {
                "agent_id": row.agent_id,
                "name": row.name,
                "description": row.description,
                "agent_type": row.agent_type,
                "status": row.status,
                "is_system": False,
                "manageable": not row.is_system,
                "management_mode": "full",
                "default_agent_profile_id": resolve_agent_profile(
                    agent_definitions[row.agent_id]
                ).profile_id,
                "agent_profiles": agent_profile_options(agent_definitions[row.agent_id]),
            }
            for row in rows
            if not getattr(row, "is_system", False)
        ],
        "system_agents": await _system_agent_list_entries(deps, actor_email),
    }


async def _get_agent(
    deps: AgentManagementDependencies,
    actor_email: str,
    current_agent_id: str,
    arguments: dict[str, Any],
) -> dict[str, Any]:
    row = await _require_owned_target(deps, actor_email, current_agent_id, arguments)
    agent = agent_to_response(row).model_dump(mode="json")
    agent["settings"] = _agent_settings_payload(row, deps)
    return {"status": "ok", "agent": agent}


async def _settings_get(
    deps: AgentManagementDependencies,
    actor_email: str,
    current_agent_id: str,
    arguments: dict[str, Any],
) -> dict[str, Any]:
    row = await _require_owned_target(deps, actor_email, current_agent_id, arguments)
    return {
        "status": "ok",
        "agent_id": row.agent_id,
        "settings": _agent_settings_payload(row, deps),
    }


async def _settings_update(
    deps: AgentManagementDependencies,
    actor_email: str,
    current_agent_id: str,
    arguments: dict[str, Any],
) -> dict[str, Any]:
    row = await _require_owned_target(deps, actor_email, current_agent_id, arguments)
    raw_settings = arguments.get("settings")
    if not isinstance(raw_settings, dict):
        raise AgentManagementError("settings must be an object")
    updates = await _settings_updates(deps, actor_email, row, raw_settings)
    if not updates:
        raise AgentManagementError("No settings update fields provided")

    async with deps.session_factory() as session:
        ok = await update_agent(session, row.agent_id, updates=updates)
        if not ok:
            raise AgentManagementError("Agent settings update failed")
        await session.commit()
        row = await get_agent(session, row.agent_id)
        assert row is not None

    await _audit(deps, actor_email, row.agent_id, "settings_update", "success", arguments)
    return {
        "status": "updated",
        "agent_id": row.agent_id,
        "settings": _agent_settings_payload(row, deps),
        "agent": agent_to_response(row).model_dump(mode="json"),
    }


def _runtime_profiles_payload(row: Any) -> dict[str, Any]:
    definition = AgentDefinition.model_validate(agent_to_response(row).model_dump())
    profiles = {
        profile_id: profile.model_dump(mode="json")
        for profile_id, profile in definition.agent_profiles.items()
    }
    return {
        "agent_id": row.agent_id,
        "profiles": profiles,
        "configured_default_profile_id": definition.default_agent_profile_id,
        "effective_default_profile_id": resolve_agent_profile(definition).profile_id,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
    }


async def _runtime_profiles_list(
    deps: AgentManagementDependencies,
    actor_email: str,
    current_agent_id: str,
    arguments: dict[str, Any],
) -> dict[str, Any]:
    row = await _require_owned_target(deps, actor_email, current_agent_id, arguments)
    return {"status": "ok", **_runtime_profiles_payload(row)}


async def _runtime_profiles_get(
    deps: AgentManagementDependencies,
    actor_email: str,
    current_agent_id: str,
    arguments: dict[str, Any],
) -> dict[str, Any]:
    row = await _require_owned_target(deps, actor_email, current_agent_id, arguments)
    profile_id = _required_profile_id(arguments)
    payload = _runtime_profiles_payload(row)
    profile = payload["profiles"].get(profile_id)
    if profile is None:
        raise AgentManagementError("Runtime profile not found")
    return {"status": "ok", **payload, "profile_id": profile_id, "profile": profile}


async def _runtime_profiles_write(
    deps: AgentManagementDependencies,
    actor_email: str,
    current_agent_id: str,
    arguments: dict[str, Any],
    *,
    create: bool,
) -> dict[str, Any]:
    await _require_owned_target(deps, actor_email, current_agent_id, arguments)
    profile_id = _required_profile_id(arguments)
    raw_profile = arguments.get("profile")
    if not isinstance(raw_profile, dict):
        raise AgentManagementError("profile must be an object")

    async with deps.session_factory() as session:
        row = await get_agent(session, str(arguments["agent_id"]))
        assert row is not None
        _check_expected_updated_at(row, arguments)
        profiles = _profile_map(row)
        existing = profiles.get(profile_id)
        if create and existing is not None:
            raise AgentManagementError("Runtime profile already exists")
        if not create and existing is None:
            raise AgentManagementError("Runtime profile not found")
        if "profile_id" in raw_profile and raw_profile["profile_id"] not in {None, profile_id}:
            raise AgentManagementError(
                "profile.profile_id must match profile_id and cannot be changed"
            )
        candidate = dict(existing or {})
        candidate.update(raw_profile)
        candidate["profile_id"] = profile_id
        if (
            existing is not None
            and bool(existing.get("enabled", True))
            and not bool(candidate.get("enabled", True))
        ):
            references = await live_agent_profile_references(session, row.agent_id, profile_id)
            if references:
                details = ", ".join(f"{kind}={count}" for kind, count in sorted(references.items()))
                raise AgentManagementError(f"Runtime profile is in use: {details}")
        profiles[profile_id] = await _validated_runtime_profile(
            session, row, profile_id, candidate, profiles
        )
        await _persist_runtime_profile_updates(
            session, row, arguments, {"agent_profiles": profiles}
        )
        await session.commit()
        row = await get_agent(session, row.agent_id)
        assert row is not None

    action = "runtime_profiles_create" if create else "runtime_profiles_update"
    await _audit(deps, actor_email, row.agent_id, action, "success", arguments)
    payload = _runtime_profiles_payload(row)
    return {
        "status": "created" if create else "updated",
        **payload,
        "profile_id": profile_id,
        "profile": payload["profiles"][profile_id],
    }


async def _runtime_profiles_default_set(
    deps: AgentManagementDependencies,
    actor_email: str,
    current_agent_id: str,
    arguments: dict[str, Any],
) -> dict[str, Any]:
    await _require_owned_target(deps, actor_email, current_agent_id, arguments)
    requested = arguments.get("default_profile_id")
    if requested is not None and (not isinstance(requested, str) or not requested.strip()):
        raise AgentManagementError("default_profile_id must be a non-empty string or null")
    profile_id = requested.strip() if isinstance(requested, str) else None
    async with deps.session_factory() as session:
        row = await get_agent(session, str(arguments["agent_id"]))
        assert row is not None
        _check_expected_updated_at(row, arguments)
        profiles = _profile_map(row)
        if profile_id is not None:
            profile = profiles.get(profile_id)
            if profile is None:
                raise AgentManagementError("Runtime profile not found")
            if not bool(profile.get("enabled", True)):
                raise AgentManagementError("Default runtime profile must be enabled")
        await _validate_runtime_profile_map(row, profiles, profile_id)
        await _persist_runtime_profile_updates(
            session, row, arguments, {"default_agent_profile_id": profile_id}
        )
        await session.commit()
        row = await get_agent(session, row.agent_id)
        assert row is not None
    await _audit(
        deps, actor_email, row.agent_id, "runtime_profiles_default_set", "success", arguments
    )
    return {"status": "updated", **_runtime_profiles_payload(row)}


async def _runtime_profiles_delete(
    deps: AgentManagementDependencies,
    actor_email: str,
    current_agent_id: str,
    arguments: dict[str, Any],
) -> dict[str, Any]:
    await _require_owned_target(deps, actor_email, current_agent_id, arguments)
    profile_id = _required_profile_id(arguments)
    replacement_profile_id = _optional_replacement_profile_id(arguments)
    migrated_references: dict[str, int] = {}
    async with deps.session_factory() as session:
        row = await get_agent(session, str(arguments["agent_id"]))
        assert row is not None
        _check_expected_updated_at(row, arguments)
        profiles = _profile_map(row)
        if profile_id not in profiles:
            raise AgentManagementError("Runtime profile not found")
        if row.default_agent_profile_id == profile_id:
            raise AgentManagementError("Cannot delete the configured default runtime profile")
        if replacement_profile_id == profile_id:
            raise AgentManagementError("Replacement runtime profile must be different")
        if replacement_profile_id is not None:
            replacement = profiles.get(replacement_profile_id)
            if replacement is None:
                raise AgentManagementError(
                    "Replacement runtime profile must exist on the same agent"
                )
            if not bool(replacement.get("enabled", True)):
                raise AgentManagementError("Replacement runtime profile must be enabled")
        references = await live_agent_profile_references(session, row.agent_id, profile_id)
        if references and replacement_profile_id is None:
            details = ", ".join(f"{kind}={count}" for kind, count in sorted(references.items()))
            raise AgentManagementError(f"Runtime profile is in use: {details}")
        if replacement_profile_id is not None:
            migrated_references = await _replace_live_agent_profile_references(
                session,
                row.agent_id,
                profile_id,
                replacement_profile_id,
            )
        del profiles[profile_id]
        await _validate_runtime_profile_map(row, profiles, row.default_agent_profile_id)
        await _persist_runtime_profile_updates(
            session, row, arguments, {"agent_profiles": profiles}
        )
        await session.commit()
        row = await get_agent(session, row.agent_id)
        assert row is not None
    audit_arguments = {
        **arguments,
        "replacement_profile_id": replacement_profile_id,
        "migrated_references": migrated_references,
    }
    await _audit(
        deps,
        actor_email,
        row.agent_id,
        "runtime_profiles_delete",
        "success",
        audit_arguments,
    )
    return {
        "status": "deleted",
        **_runtime_profiles_payload(row),
        "profile_id": profile_id,
        "replacement_profile_id": replacement_profile_id,
        "migrated_references": migrated_references,
    }


def _required_profile_id(arguments: dict[str, Any]) -> str:
    profile_id = arguments.get("profile_id")
    if not isinstance(profile_id, str) or not profile_id.strip() or "/" in profile_id:
        raise AgentManagementError("profile_id must be a non-empty string without '/'")
    return profile_id.strip()


def _optional_replacement_profile_id(arguments: dict[str, Any]) -> str | None:
    replacement_profile_id = arguments.get("replacement_profile_id")
    if replacement_profile_id is None:
        return None
    if (
        not isinstance(replacement_profile_id, str)
        or not replacement_profile_id.strip()
        or "/" in replacement_profile_id
    ):
        raise AgentManagementError("replacement_profile_id must be a non-empty string without '/'")
    return replacement_profile_id.strip()


def _profile_map(row: Any) -> dict[str, dict[str, Any]]:
    definition = AgentDefinition.model_validate(agent_to_response(row).model_dump())
    return {
        profile_id: profile.model_dump(mode="json")
        for profile_id, profile in definition.agent_profiles.items()
    }


def _check_expected_updated_at(row: Any, arguments: dict[str, Any]) -> None:
    expected = arguments.get("expected_updated_at")
    if expected is None:
        return
    if not isinstance(expected, str):
        raise AgentManagementError("expected_updated_at must be an ISO-8601 string")
    try:
        expected_at = datetime.fromisoformat(expected)
    except ValueError as exc:
        raise AgentManagementError("expected_updated_at must be an ISO-8601 string") from exc
    if expected_at.tzinfo is None:
        expected_at = expected_at.replace(tzinfo=UTC)
    actual = row.updated_at
    if actual is None:
        raise AgentManagementError("Runtime profile update conflict; re-read the agent and retry")
    if actual.tzinfo is None:
        actual = actual.replace(tzinfo=UTC)
    if expected_at.astimezone(UTC) != actual.astimezone(UTC):
        raise AgentManagementError("Runtime profile update conflict; re-read the agent and retry")


async def _validated_runtime_profile(
    session: Any,
    row: Any,
    profile_id: str,
    candidate: dict[str, Any],
    profiles: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    try:
        profile = AgentRuntimeProfile.model_validate(candidate)
    except ValueError as exc:
        raise AgentManagementError(f"Invalid runtime profile: {exc}") from exc
    profiles[profile_id] = profile.model_dump(mode="json")
    await _validate_runtime_profile_map(row, profiles, row.default_agent_profile_id)

    providers = await list_llm_providers(session)
    provider_ids = {provider.provider_id for provider in providers}
    effective_provider = profile.provider_id
    if effective_provider is None and isinstance(row.llm_config, dict):
        effective_provider = _optional_string(row.llm_config.get("provider_id"))
    if profile.provider_id is not None and profile.provider_id not in provider_ids:
        raise AgentManagementError(f"Invalid provider_id: {profile.provider_id}")
    if profile.model is not None and effective_provider is not None:
        configured_models = {
            provider.provider_id: _configured_provider_models(provider) for provider in providers
        }
        allowed_models = configured_models.get(effective_provider, set())
        if allowed_models and profile.model not in allowed_models:
            raise AgentManagementError(
                f"Invalid model for provider {effective_provider}: {profile.model}"
            )
    return profiles[profile_id]


async def _validate_runtime_profile_map(
    row: Any,
    profiles: dict[str, dict[str, Any]],
    default_profile_id: str | None,
) -> None:
    _validate_unavailable_profile_memory_options(row, profiles)
    candidate = agent_to_response(row).model_dump()
    candidate["agent_profiles"] = profiles
    candidate["default_agent_profile_id"] = default_profile_id
    try:
        definition = AgentDefinition.model_validate(candidate)
        validate_agent_profile_configuration(definition)
    except ValueError as exc:
        raise AgentManagementError(f"Invalid runtime profile configuration: {exc}") from exc
    if default_profile_id is not None:
        default = definition.agent_profiles.get(default_profile_id)
        if default is None:
            raise AgentManagementError("Default runtime profile must exist")
        if not default.enabled:
            raise AgentManagementError("Default runtime profile must be enabled")


def _validate_unavailable_profile_memory_options(
    row: Any,
    candidate_profiles: dict[str, dict[str, Any]],
) -> None:
    capabilities = row.capabilities if isinstance(row.capabilities, dict) else {}
    backend_id = capabilities.get("memory_backend", "mnemory")
    if not isinstance(backend_id, str):
        raise AgentManagementError("Invalid memory_backend configuration")
    try:
        get_backend("memory", backend_id)
        return
    except ValueError:
        pass

    current_profiles = row.agent_profiles if isinstance(row.agent_profiles, dict) else {}

    def _options_snapshot(profiles: dict[str, Any]) -> dict[str, dict[str, Any]]:
        snapshot: dict[str, dict[str, Any]] = {}
        for profile_id, profile in profiles.items():
            if not isinstance(profile_id, str) or not isinstance(profile, dict):
                continue
            options = profile.get("memory_backend_options")
            if isinstance(options, dict) and options:
                snapshot[profile_id] = dict(options)
        return snapshot

    if _options_snapshot(candidate_profiles) != _options_snapshot(current_profiles):
        raise AgentManagementError(
            f"Memory profile options cannot change while backend {backend_id!r} is unavailable"
        )


async def live_agent_profile_references(
    session: Any,
    agent_id: str,
    profile_id: str,
) -> dict[str, int]:
    """Return persisted runtime-profile references that block removal or disabling."""

    checks = {
        "conversations": select(Conversation.conversation_id).where(
            Conversation.agent_id == agent_id,
            Conversation.agent_profile_id == profile_id,
        ),
        "sessions": select(Session.session_id).where(
            Session.agent_id == agent_id,
            Session.agent_profile_id == profile_id,
        ),
        "tasks": select(Task.task_id).where(
            Task.agent_id == agent_id,
            Task.agent_profile_id == profile_id,
        ),
        "schedules": select(Schedule.schedule_id).where(
            Schedule.agent_id == agent_id,
            Schedule.agent_profile_id == profile_id,
        ),
        "channel_accounts": select(ChannelAccountRow.account_id).where(
            ChannelAccountRow.agent_id == agent_id,
            ChannelAccountRow.default_agent_profile_id == profile_id,
        ),
    }
    references: dict[str, int] = {}
    for name, query in checks.items():
        count = len((await session.execute(query)).scalars().all())
        if count:
            references[name] = count
    return references


async def _replace_live_agent_profile_references(
    session: Any,
    agent_id: str,
    source_profile_id: str,
    replacement_profile_id: str,
) -> dict[str, int]:
    """Replace exact live profile references for one agent and return affected counts."""

    replacements = {
        "conversations": (
            Conversation,
            Conversation.agent_profile_id,
        ),
        "sessions": (
            Session,
            Session.agent_profile_id,
        ),
        "tasks": (
            Task,
            Task.agent_profile_id,
        ),
        "schedules": (
            Schedule,
            Schedule.agent_profile_id,
        ),
        "channel_accounts": (
            ChannelAccountRow,
            ChannelAccountRow.default_agent_profile_id,
        ),
    }
    migrated: dict[str, int] = {}
    for name, (model, profile_column) in replacements.items():
        result = await session.execute(
            update(model)
            .where(
                cast(Any, model).agent_id == agent_id,
                profile_column == source_profile_id,
            )
            .values({profile_column.key: replacement_profile_id})
        )
        migrated[name] = result.rowcount
    return migrated


async def _persist_runtime_profile_updates(
    session: Any,
    row: Any,
    arguments: dict[str, Any],
    updates: dict[str, Any],
) -> None:
    if arguments.get("expected_updated_at") is None:
        ok = await update_agent(session, row.agent_id, updates=updates)
    else:
        ok = await update_agent_if_updated_at(
            session,
            row.agent_id,
            expected_updated_at=row.updated_at,
            updates=updates,
        )
    if not ok:
        raise AgentManagementError("Runtime profile update conflict; re-read the agent and retry")


async def _tools_get(
    deps: AgentManagementDependencies,
    actor_email: str,
    current_agent_id: str,
    arguments: dict[str, Any],
) -> dict[str, Any]:
    row = await _require_owned_target(deps, actor_email, current_agent_id, arguments)
    return {
        "status": "ok",
        "agent_id": row.agent_id,
        "tools": _agent_tool_assignment_payload(row, deps),
    }


async def _tools_update(
    deps: AgentManagementDependencies,
    actor_email: str,
    current_agent_id: str,
    arguments: dict[str, Any],
    *,
    mode: str,
) -> dict[str, Any]:
    row = await _require_owned_target(deps, actor_email, current_agent_id, arguments)
    current = _configured_tool_assignment(row)
    if mode == "set":
        proposed = _tool_assignment_from_arguments(arguments, row)
    else:
        delta = _tool_assignment_from_arguments(arguments, row, default_empty=True)
        proposed = {
            key: _apply_assignment_delta(
                current.get(key, []), delta.get(key, []), remove=mode == "remove"
            )
            for key in TOOL_ASSIGNMENT_FIELDS
        }
    validation = _validate_tool_assignment(row, proposed, deps.assignable_tools)
    if not validation["valid"]:
        return {
            "status": "invalid",
            "agent_id": row.agent_id,
            **validation,
            "effective_tools": _effective_assignment_tools(
                proposed, _available_tool_map(deps.assignable_tools)
            ),
        }

    tools = dict(row.tools) if isinstance(row.tools, dict) else {}
    tools.update(proposed)
    async with deps.session_factory() as session:
        ok = await update_agent(session, row.agent_id, updates={"tools": tools})
        if not ok:
            raise AgentManagementError("Agent tools update failed")
        await session.commit()
        row = await get_agent(session, row.agent_id)
        assert row is not None
    await _audit(deps, actor_email, row.agent_id, f"tools_{mode}", "success", arguments)
    return {
        "status": "updated",
        "agent_id": row.agent_id,
        "tools": _agent_tool_assignment_payload(row, deps),
    }


async def _knowledgebases_get(
    deps: AgentManagementDependencies,
    actor_email: str,
    current_agent_id: str,
    arguments: dict[str, Any],
) -> dict[str, Any]:
    row = await _require_owned_target(deps, actor_email, current_agent_id, arguments)
    assigned = _assigned_knowledgebase_ids(row)
    return {
        "status": "ok",
        "agent_id": row.agent_id,
        "assigned_knowledgebases": assigned,
        "available_knowledgebases": await _available_knowledgebase_options(deps, actor_email),
    }


async def _skills_get(
    deps: AgentManagementDependencies,
    actor_email: str,
    current_agent_id: str,
    arguments: dict[str, Any],
) -> dict[str, Any]:
    row = await _require_owned_target(deps, actor_email, current_agent_id, arguments)
    async with deps.session_factory() as session:
        skill_usage = await agent_skill_usage_report(session, agent=row, owner_email=actor_email)
    return {
        "status": "ok",
        "agent_id": row.agent_id,
        "skill_assignments": _configured_skill_assignments(row),
        "available_skills": await _available_skill_options(deps, actor_email),
        "skill_usage": skill_usage,
    }


async def _skills_update(
    deps: AgentManagementDependencies,
    actor_email: str,
    current_agent_id: str,
    arguments: dict[str, Any],
    *,
    mode: str,
) -> dict[str, Any]:
    row = await _require_owned_target(deps, actor_email, current_agent_id, arguments)
    available = {
        item["skill_id"]: item for item in await _available_skill_options(deps, actor_email)
    }
    current = _configured_skill_assignments(row)
    current_by_id = {item["skill_id"]: item for item in current}

    if mode == "remove":
        selected_ids = _validated_string_list(arguments.get("skill_ids"), "skill_ids")
        invalid = sorted(set(selected_ids) - (set(available) | set(current_by_id)))
        if invalid:
            raise AgentManagementError(f"Invalid skill_ids: {', '.join(invalid)}")
        selected = [item for item in current if item["skill_id"] not in set(selected_ids)]
    else:
        raw_assignments = arguments.get("skill_assignments")
        if mode == "update":
            if not isinstance(raw_assignments, list):
                raise AgentManagementError("skill_assignments must be a list")
            requested_ids = {
                str(item.get("skill_id") or "").strip()
                for item in raw_assignments
                if isinstance(item, dict)
            }
            missing = sorted(requested_ids - set(current_by_id))
            if missing:
                raise AgentManagementError(
                    f"Cannot update unassigned skill_ids: {', '.join(missing)}"
                )
            raw_assignments = [
                {**current_by_id.get(str(item.get("skill_id") or "").strip(), {}), **item}
                if isinstance(item, dict)
                else item
                for item in raw_assignments
            ]
        incoming = _validated_skill_assignments(raw_assignments, available)
        incoming_by_id = {item["skill_id"]: item for item in incoming}
        if mode == "set":
            selected = incoming
        elif mode == "add":
            selected = [
                *current,
                *[item for item in incoming if item["skill_id"] not in current_by_id],
            ]
        else:
            selected = [
                (
                    {**item, **incoming_by_id[item["skill_id"]]}
                    if item["skill_id"] in incoming_by_id
                    else item
                )
                for item in current
            ]

    skills = dict(row.skills) if isinstance(row.skills, dict) else {}
    legacy_items = [
        item for item in skills.get("items", []) if isinstance(item, dict) and "tool_names" in item
    ]
    skills["items"] = [*legacy_items, *selected]
    async with deps.session_factory() as session:
        ok = await update_agent(session, row.agent_id, updates={"skills": skills})
        if not ok:
            raise AgentManagementError("Agent skill assignment update failed")
        await session.commit()
        row = await get_agent(session, row.agent_id)
        assert row is not None
    await _audit(deps, actor_email, row.agent_id, f"skills_{mode}", "success", arguments)
    return {
        "status": "updated",
        "agent_id": row.agent_id,
        "skill_assignments": _configured_skill_assignments(row),
    }


def _configured_skill_assignments(row: Any) -> list[dict[str, Any]]:
    skills = row.skills if isinstance(row.skills, dict) else {}
    items = skills.get("items")
    if not isinstance(items, list):
        return []
    assignments: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in items:
        if not isinstance(item, dict) or "tool_names" in item:
            continue
        try:
            ref = AgentSkillRef.model_validate(item)
        except ValueError:
            continue
        if ref.skill_id in seen:
            continue
        seen.add(ref.skill_id)
        assignments.append(ref.model_dump(mode="json"))
    return assignments


def _validated_skill_assignments(
    value: Any, available: dict[str, dict[str, Any]]
) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        raise AgentManagementError("skill_assignments must be a list")
    assignments: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in value:
        if not isinstance(raw, dict):
            raise AgentManagementError("skill_assignments entries must be objects")
        unknown = sorted(set(raw) - {"skill_id", "enabled", "auto_load_instructions"})
        if unknown:
            raise AgentManagementError(
                f"Unsupported skill assignment field(s): {', '.join(unknown)}"
            )
        try:
            ref = AgentSkillRef.model_validate(raw)
        except ValueError as exc:
            raise AgentManagementError(f"Invalid skill assignment: {exc}") from exc
        if not ref.skill_id.strip():
            raise AgentManagementError("skill_id must be a non-empty string")
        ref.skill_id = ref.skill_id.strip()
        if ref.skill_id not in available:
            raise AgentManagementError(f"Invalid skill_id: {ref.skill_id}")
        if ref.skill_id in seen:
            raise AgentManagementError(f"Duplicate skill_id: {ref.skill_id}")
        seen.add(ref.skill_id)
        assignments.append(ref.model_dump(mode="json"))
    return assignments


async def _available_skill_options(
    deps: AgentManagementDependencies, actor_email: str
) -> list[dict[str, Any]]:
    async with deps.session_factory() as session:
        rows = await list_skills(session, owner_email=actor_email)
    return [
        {
            "skill_id": row.skill_id,
            "name": row.name,
            "description": row.description,
            "attach_to_all_agents": bool(row.auto_load),
            "is_system": bool(getattr(row, "is_system", False)),
        }
        for row in rows
        if is_agent_assignable_skill(row.skill_id)
    ]


async def _knowledgebases_update(
    deps: AgentManagementDependencies,
    actor_email: str,
    current_agent_id: str,
    arguments: dict[str, Any],
    *,
    mode: str,
) -> dict[str, Any]:
    row = await _require_owned_target(deps, actor_email, current_agent_id, arguments)
    raw_ids = arguments.get("knowledgebase_ids", arguments.get("assigned_knowledgebases"))
    selected = _validated_string_list(raw_ids, "knowledgebase_ids")
    valid_ids = {item["id"] for item in await _available_knowledgebase_options(deps, actor_email)}
    invalid = sorted(item for item in selected if item not in valid_ids)
    if invalid:
        raise AgentManagementError(f"Invalid knowledgebase_ids: {', '.join(invalid)}")
    current = _assigned_knowledgebase_ids(row)
    if mode == "set":
        assigned = selected
    else:
        assigned = _apply_assignment_delta(current, selected, remove=mode == "remove")
    permissions = dict(row.permissions) if isinstance(row.permissions, dict) else {}
    permissions["allowed_knowledgebases"] = assigned
    async with deps.session_factory() as session:
        ok = await update_agent(session, row.agent_id, updates={"permissions": permissions})
        if not ok:
            raise AgentManagementError("Agent knowledgebase assignment update failed")
        await session.commit()
        row = await get_agent(session, row.agent_id)
        assert row is not None
    await _audit(deps, actor_email, row.agent_id, f"knowledgebases_{mode}", "success", arguments)
    return {
        "status": "updated",
        "agent_id": row.agent_id,
        "assigned_knowledgebases": _assigned_knowledgebase_ids(row),
    }


async def _create_agent(
    deps: AgentManagementDependencies, actor_email: str, arguments: dict[str, Any]
) -> dict[str, Any]:
    name = str(arguments.get("name") or "").strip()
    if not name:
        raise AgentManagementError("name is required for create")
    if deps.assignable_tools is None:
        raise AgentManagementError(
            "Caller-effective assignable tool inventory is required for create"
        )
    from cognis.api.runtime_support import static_tool_definitions

    assignable_ids = {stable_tool_id(tool) for tool in deps.assignable_tools}
    denied_static_ids = sorted(
        stable_tool_id(tool)
        for tool in static_tool_definitions(knowledgebase_enabled=True)
        if stable_tool_id(tool) not in assignable_ids
    )
    agent_id = str(arguments.get("agent_id") or slugify_agent_id(name)).strip()
    try:
        validate_agent_id(agent_id)
    except ValueError as exc:
        raise AgentManagementError(str(exc)) from exc
    agent_type = str(arguments.get("agent_type") or "primary")
    if agent_type not in {"primary", "secondary"}:
        raise AgentManagementError("agent_type must be primary or secondary")
    if agent_type == "primary" and arguments.get("status") == "active":
        from cognis.core.runtime import ExecutorConfigurationError, require_explicit_executor

        try:
            require_explicit_executor(None)
        except ExecutorConfigurationError as exc:
            raise AgentManagementError(str(exc)) from exc
    # Assignability is not an opt-in: new agents need an explicit management grant.
    default_tools = [
        tool for tool in deps.assignable_tools if tool.name not in _MANAGEMENT_TOOL_NAMES
    ]

    avatar_image_id = arguments.get("avatar_image_id")
    if arguments.get("generate_avatar"):
        avatar_image_id = await _generate_avatar(deps, actor_email, arguments)
    assigned_knowledgebases = _validated_string_list(
        arguments.get("assigned_knowledgebases", []),
        "assigned_knowledgebases",
    )
    permissions: dict[str, Any] = {}
    if assigned_knowledgebases:
        permissions["allowed_knowledgebases"] = assigned_knowledgebases
    if "delegation" in arguments:
        permissions["delegation"] = AgentDelegationPolicy.model_validate(
            _object(arguments["delegation"], "delegation")
        ).model_dump(mode="json")

    async with deps.session_factory() as session:
        if await get_agent(session, agent_id) is not None:
            raise AgentManagementError("Agent already exists")
        available_knowledgebases = await assignable_knowledgebases(
            session,
            actor_email=actor_email,
            caller_agent_id=deps.caller_agent_id,
            allowed_ids=deps.assignable_knowledgebase_ids,
        )
        available_knowledgebase_ids = {
            row.knowledgebase_id for row in available_knowledgebases if row.status == "active"
        }
        invalid_knowledgebases = sorted(set(assigned_knowledgebases) - available_knowledgebase_ids)
        if invalid_knowledgebases:
            raise AgentManagementError(
                "Invalid assigned_knowledgebases: " + ", ".join(invalid_knowledgebases)
            )
        row = await create_agent(
            session,
            agent_id=agent_id,
            owner_email=actor_email,
            name=name,
            display_name=_optional_string(arguments.get("display_name")) or name,
            description=_optional_string(arguments.get("description")),
            system_prompt=_optional_string(arguments.get("system_prompt")),
            personality=_optional_dict(arguments.get("personality")),
            skills=None,
            tools={
                "tool_groups": [],
                "builtin_tools": sorted(
                    stable_tool_id(tool) for tool in default_tools if tool.source.type == "builtin"
                ),
                "allow_tools": sorted(stable_tool_id(tool) for tool in default_tools),
                "deny_tools": denied_static_ids,
            },
            permissions=permissions or None,
            capabilities=_validated_guardrail_settings({}, arguments),
            llm_config=None,
            execution=(
                {
                    "auto_profile_selection": auto_profile_selection_settings(
                        {"auto_profile_selection": arguments["auto_profile_selection"]}
                    ).model_dump()
                }
                if "auto_profile_selection" in arguments
                else None
            ),
            avatar_image_id=_optional_string(avatar_image_id),
            agent_type=agent_type,
            status=str(arguments.get("status") or "draft"),
        )
        await session.commit()
        await session.refresh(row)

    await _sync_identity(deps, row, previous_content=None)
    await _audit(deps, actor_email, agent_id, "create", "success", arguments)
    return {"status": "created", "agent": agent_to_response(row).model_dump(mode="json")}


async def _update_agent(
    deps: AgentManagementDependencies,
    actor_email: str,
    current_agent_id: str,
    arguments: dict[str, Any],
) -> dict[str, Any]:
    row = await _require_owned_target(deps, actor_email, current_agent_id, arguments)
    previous_definition = AgentDefinition.model_validate(agent_to_response(row).model_dump())
    updates = _agent_updates(arguments)
    if "auto_profile_selection" in arguments:
        execution = dict(row.execution or {})
        _apply_execution_setting(
            execution, "auto_profile_selection", arguments["auto_profile_selection"], set()
        )
        updates["execution"] = execution
    if row.agent_type == "primary" and updates.get("status") == "active":
        from cognis.core.runtime import ExecutorConfigurationError, require_explicit_executor

        try:
            require_explicit_executor(updates.get("execution", row.execution))
        except ExecutorConfigurationError as exc:
            raise AgentManagementError(str(exc)) from exc
    if "delegation" in arguments:
        permissions = dict(row.permissions) if isinstance(row.permissions, dict) else {}
        permissions["delegation"] = AgentDelegationPolicy.model_validate(
            _object(arguments["delegation"], "delegation")
        ).model_dump(mode="json")
        updates["permissions"] = permissions
    if arguments.get("generate_avatar"):
        updates["avatar_image_id"] = await _generate_avatar(deps, actor_email, arguments)
    if not updates:
        raise AgentManagementError("No update fields provided")

    profile_changed = bool({"name", "display_name", "avatar_image_id"} & updates.keys())
    identity_changed = any(
        field in updates and getattr(row, field) != updates[field]
        for field in ("system_prompt", "personality")
    )

    async with deps.session_factory() as session:
        ok = await update_agent(session, row.agent_id, updates=updates)
        if not ok:
            raise AgentManagementError("Agent update failed")
        await session.commit()
        row = await get_agent(session, row.agent_id)
        assert row is not None

    if identity_changed:
        previous_content = (
            previous_definition.compose_personality() or previous_definition.system_prompt
        )
        await _sync_identity(deps, row, previous_content=previous_content)
    if profile_changed:
        await _publish_profile_updated(deps, row.agent_id)
    await _audit(deps, actor_email, row.agent_id, "update", "success", arguments)
    return {"status": "updated", "agent": agent_to_response(row).model_dump(mode="json")}


async def _activate_agent(
    deps: AgentManagementDependencies,
    actor_email: str,
    current_agent_id: str,
    arguments: dict[str, Any],
) -> dict[str, Any]:
    result = await _set_status(deps, actor_email, current_agent_id, arguments, "active")
    async with deps.session_factory() as session:
        row = await get_agent(session, str(arguments.get("agent_id")))
    assert row is not None
    await _sync_identity(deps, row, previous_content=None)
    return result


async def _set_status(
    deps: AgentManagementDependencies,
    actor_email: str,
    current_agent_id: str,
    arguments: dict[str, Any],
    status: str,
) -> dict[str, Any]:
    row = await _require_owned_target(deps, actor_email, current_agent_id, arguments)
    if status == "active" and row.agent_type == "primary":
        from cognis.core.runtime import ExecutorConfigurationError, require_explicit_executor

        try:
            require_explicit_executor(row.execution)
        except ExecutorConfigurationError as exc:
            raise AgentManagementError(str(exc)) from exc
    async with deps.session_factory() as session:
        ok = await set_agent_status(session, row.agent_id, status)
        await session.commit()
        row = await get_agent(session, row.agent_id)
        assert row is not None
    await _audit(deps, actor_email, row.agent_id, status, "success", arguments)
    return {"status": status, "ok": ok, "agent": agent_to_response(row).model_dump(mode="json")}


async def _sync_personality_action(
    deps: AgentManagementDependencies,
    actor_email: str,
    current_agent_id: str,
    arguments: dict[str, Any],
) -> dict[str, Any]:
    row = await _require_owned_target(deps, actor_email, current_agent_id, arguments)
    definition = AgentDefinition.model_validate(agent_to_response(row).model_dump())
    await _sync_identity(
        deps, row, previous_content=definition.compose_personality() or definition.system_prompt
    )
    await _audit(deps, actor_email, row.agent_id, "sync_personality", "success", arguments)
    return {"status": "synced", "ok": True}


async def _bindings_get(
    deps: AgentManagementDependencies,
    actor_email: str,
    current_agent_id: str,
    arguments: dict[str, Any],
) -> dict[str, Any]:
    row = await _require_owned_target(deps, actor_email, current_agent_id, arguments)
    async with deps.session_factory() as session:
        bindings = await list_secondary_bindings(session, row.agent_id)
    return {"status": "ok", "agent_id": row.agent_id, "secondary_agent_ids": bindings}


async def _bindings_set(
    deps: AgentManagementDependencies,
    actor_email: str,
    current_agent_id: str,
    arguments: dict[str, Any],
) -> dict[str, Any]:
    row = await _require_owned_target(deps, actor_email, current_agent_id, arguments)
    if getattr(row, "agent_type", "primary") != "primary":
        raise AgentManagementError("Secondary bindings can only be set on primary agents")
    raw_ids = arguments.get("secondary_agent_ids")
    if not isinstance(raw_ids, list):
        raise AgentManagementError("secondary_agent_ids must be a list")
    ids = list(
        dict.fromkeys(item.strip() for item in raw_ids if isinstance(item, str) and item.strip())
    )
    async with deps.session_factory() as session:
        for secondary_id in ids:
            if secondary_id in SYSTEM_AGENTS:
                raise AgentManagementError(
                    f"{secondary_id} is a system agent and is available without a binding"
                )
            secondary = await get_agent(session, secondary_id)
            if secondary is None or secondary.owner_email != actor_email:
                raise AgentManagementError(
                    f"Secondary agent not found or not owned: {secondary_id}"
                )
            if getattr(secondary, "agent_type", "primary") != "secondary":
                raise AgentManagementError(f"Agent is not a secondary agent: {secondary_id}")
        await set_secondary_bindings(session, row.agent_id, ids)
        await session.commit()
    await _audit(deps, actor_email, row.agent_id, "bindings_set", "success", arguments)
    return {"status": "updated", "agent_id": row.agent_id, "secondary_agent_ids": ids}


async def _avatar_remove(
    deps: AgentManagementDependencies,
    actor_email: str,
    current_agent_id: str,
    arguments: dict[str, Any],
) -> dict[str, Any]:
    row = await _require_owned_target(deps, actor_email, current_agent_id, arguments)
    old_image_id = row.avatar_image_id
    async with deps.session_factory() as session:
        ok = await update_agent(session, row.agent_id, updates={"avatar_image_id": None})
        await session.commit()
    if old_image_id and deps.artifact_store is not None:
        with contextlib.suppress(Exception):
            await deps.artifact_store.async_delete_object("avatars", old_image_id)
    await _publish_profile_updated(deps, row.agent_id)
    await _audit(deps, actor_email, row.agent_id, "avatar_remove", "success", arguments)
    return {"status": "updated", "ok": ok, "agent_id": row.agent_id}


async def _shares_list(
    deps: AgentManagementDependencies,
    actor_email: str,
    current_agent_id: str,
    arguments: dict[str, Any],
) -> dict[str, Any]:
    row = await _require_owned_target(deps, actor_email, current_agent_id, arguments)
    async with deps.session_factory() as session:
        grants = await list_agent_grants(session, row.agent_id)
    return {"status": "ok", "shares": [_grant_payload(grant) for grant in grants]}


async def _share_create(
    deps: AgentManagementDependencies,
    actor_email: str,
    current_agent_id: str,
    arguments: dict[str, Any],
) -> dict[str, Any]:
    from typing import Literal

    from cognis.core.inbox import publish_notification, stage_share_notification

    row = await _require_owned_target(deps, actor_email, current_agent_id, arguments)
    grantee_email = str(arguments.get("grantee_email") or "").strip()
    if not grantee_email:
        raise AgentManagementError("grantee_email is required")
    if grantee_email == actor_email:
        raise AgentManagementError("Agent owner cannot be granted their own agent")
    executor_scope = str(arguments.get("executor_scope") or "owner_executor")
    if executor_scope not in {"owner_executor", "grantee_executor"}:
        raise AgentManagementError("executor_scope must be owner_executor or grantee_executor")
    if executor_scope == "owner_executor":
        from cognis.core.runtime import ExecutorConfigurationError, require_explicit_executor

        try:
            require_explicit_executor(row.execution)
        except ExecutorConfigurationError as exc:
            raise AgentManagementError(str(exc)) from exc
    async with deps.session_factory() as session:
        if await get_user(session, grantee_email) is None:
            raise AgentManagementError("Grantee user not found")
        from cognis.store.queries import lock_resource_share_transition

        await lock_resource_share_transition(
            session, resource_type="agent", resource_id=row.agent_id
        )
        existing = await get_agent_grant_for_user(session, row.agent_id, grantee_email)
        change: Literal["shared", "updated"] | None = (
            "shared"
            if existing is None or existing.revoked_at is not None
            else "updated"
            if existing.executor_scope != executor_scope
            else None
        )
        if existing is not None:
            grant = await update_agent_grant(
                session,
                existing.grant_id,
                executor_scope=executor_scope,
                note=_optional_string(arguments.get("note")),
                grantee_overrides=None
                if executor_scope == "owner_executor"
                else existing.grantee_overrides,
                granted_at=datetime.now(UTC),
                granted_by=actor_email,
                revoked_at=None,
            )
            assert grant is not None
        else:
            grant = await create_agent_grant(
                session,
                agent_id=row.agent_id,
                grantee_user_email=grantee_email,
                executor_scope=executor_scope,
                granted_by=actor_email,
                note=_optional_string(arguments.get("note")),
            )
        notice = (
            stage_share_notification(
                session,
                recipient=grantee_email,
                actor=actor_email,
                resource_type="agent",
                resource_id=row.agent_id,
                resource_name=row.name,
                change=change,
            )
            if change
            else None
        )
        await session.commit()
    await publish_notification(deps.event_bus, notice)
    await _audit(deps, actor_email, row.agent_id, "share_create", "success", arguments)
    return {"status": "shared", "share": _grant_payload(grant)}


async def _share_update(
    deps: AgentManagementDependencies,
    actor_email: str,
    current_agent_id: str,
    arguments: dict[str, Any],
) -> dict[str, Any]:
    from cognis.core.inbox import publish_notification, stage_share_notification

    row = await _require_owned_target(deps, actor_email, current_agent_id, arguments)
    grant_id = str(arguments.get("grant_id") or "").strip()
    if not grant_id:
        raise AgentManagementError("grant_id is required")
    executor_scope = str(arguments.get("executor_scope") or "owner_executor")
    if executor_scope not in {"owner_executor", "grantee_executor"}:
        raise AgentManagementError("executor_scope must be owner_executor or grantee_executor")
    async with deps.session_factory() as session:
        from cognis.store.queries import lock_resource_share_transition

        await lock_resource_share_transition(
            session, resource_type="agent", resource_id=row.agent_id
        )
        grant = await get_agent_grant(session, grant_id)
        if grant is None or grant.agent_id != row.agent_id or grant.revoked_at is not None:
            raise AgentManagementError("Grant not found")
        changed = grant.executor_scope != executor_scope
        updated = await update_agent_grant(
            session,
            grant_id,
            executor_scope=executor_scope,
            note=_optional_string(arguments.get("note")),
            grantee_overrides=None
            if executor_scope == "owner_executor"
            else grant.grantee_overrides,
        )
        assert updated is not None
        notice = (
            stage_share_notification(
                session,
                recipient=grant.grantee_user_email,
                actor=actor_email,
                resource_type="agent",
                resource_id=row.agent_id,
                resource_name=row.name,
                change="updated",
            )
            if changed
            else None
        )
        await session.commit()
    await publish_notification(deps.event_bus, notice)
    await _audit(deps, actor_email, row.agent_id, "share_update", "success", arguments)
    return {"status": "updated", "share": _grant_payload(updated)}


async def _share_revoke(
    deps: AgentManagementDependencies,
    actor_email: str,
    current_agent_id: str,
    arguments: dict[str, Any],
) -> dict[str, Any]:
    from cognis.core.inbox import publish_notification, stage_share_notification

    row = await _require_owned_target(deps, actor_email, current_agent_id, arguments)
    grant_id = str(arguments.get("grant_id") or "").strip()
    if not grant_id:
        raise AgentManagementError("grant_id is required")
    running_task_ids: list[str] = []
    async with deps.session_factory() as session:
        from cognis.store.queries import lock_resource_share_transition

        await lock_resource_share_transition(
            session, resource_type="agent", resource_id=row.agent_id
        )
        grant = await get_agent_grant(session, grant_id)
        if grant is None or grant.agent_id != row.agent_id or grant.revoked_at is not None:
            raise AgentManagementError("Grant not found")
        revoked = await revoke_agent_grant(session, grant_id)
        assert revoked is not None
        grantee_email = revoked.grantee_user_email
        if grantee_email:
            schedules_result = await session.execute(
                select(Schedule).where(
                    Schedule.agent_id == row.agent_id,
                    Schedule.created_by == grantee_email,
                    Schedule.enabled.is_(True),
                )
            )
            for schedule in schedules_result.scalars().all():
                schedule.enabled = False
                schedule.disabled_reason = "access_revoked"

            task_result = await session.execute(
                select(Task).where(
                    Task.agent_id == row.agent_id,
                    Task.created_by == grantee_email,
                    Task.status.in_(["draft", "queued", "ready", "running", "paused"]),
                )
            )
            for task in task_result.scalars().all():
                if task.status == "running":
                    running_task_ids.append(task.task_id)
                else:
                    task.status = "paused"
                task.updated_at = datetime.now(UTC)
                if not task.result_summary:
                    task.result_summary = "Access to the shared agent was revoked."
        notice = stage_share_notification(
            session,
            recipient=grantee_email,
            actor=actor_email,
            resource_type="agent",
            resource_id=row.agent_id,
            resource_name=row.name,
            change="revoked",
        )
        await session.commit()
    await publish_notification(deps.event_bus, notice)

    if deps.task_queue is not None:
        for task_id in running_task_ids:
            with contextlib.suppress(Exception):
                await deps.task_queue.pause_task(task_id)
    await _audit(deps, actor_email, row.agent_id, "share_revoke", "success", arguments)
    return {"status": "revoked", "ok": True, "grant_id": grant_id}


_SYSTEM_AGENT_ACTIONS = frozenset(
    {
        "system_overrides_get",
        "system_overrides_update",
        "system_overrides_reset",
        "system_disable",
        "system_enable",
    }
)
_MANAGEMENT_TOOL_NAMES = frozenset({"manage_agents", "manage_mcp"}) | SYSTEM_MANAGEMENT_TOOLS
_SYSTEM_TOOL_OVERRIDE_KEYS = frozenset(
    {
        "builtin_tools",
        "allow_tools",
        "deny_tools",
        "tool_groups",
        "disabled_tools",
        "disabled_categories",
        "disabled_mcp_servers",
    }
)


def _required_system_agent(arguments: dict[str, Any]) -> AgentDefinition:
    agent_id = str(arguments.get("agent_id") or "").strip()
    if not agent_id:
        raise AgentManagementError("agent_id is required")
    try:
        return require_system_agent(agent_id)
    except SystemAgentOverrideError as exc:
        raise AgentManagementError(
            f"System agent not found: {agent_id}. Use list to discover system agents."
        ) from exc


def _system_management_mode(base: AgentDefinition) -> str:
    return "overrides" if base.allow_user_override else "read_only"


async def _system_agent_payload(
    deps: AgentManagementDependencies, actor_email: str, base: AgentDefinition
) -> dict[str, Any]:
    async with deps.session_factory() as session:
        override = await get_system_agent_override(
            session, owner_email=actor_email, agent_id=base.agent_id
        )
    effective = await AgentRegistry(deps.session_factory).get(
        base.agent_id, owner_email=actor_email, include_disabled=True
    )
    assert effective is not None
    override_payload = system_override_payload(override)
    return {
        "agent_id": base.agent_id,
        "is_system": True,
        "management_mode": _system_management_mode(base),
        "allow_user_override": base.allow_user_override,
        "allow_user_disable": base.allow_user_disable,
        "editable_fields": list(base.editable_fields),
        "override_fields": sorted(SYSTEM_AGENT_OVERRIDE_FIELDS),
        "disabled": bool(effective.disabled),
        "has_overrides": override is not None,
        "override": override_payload,
        "override_updated_at": override_payload["updated_at"] if override_payload else "",
        "shipped": {
            "llm_config": (
                base.llm_config.model_dump(mode="json", exclude_none=True)
                if base.llm_config
                else None
            ),
            "skills": base.skills,
            "tools": base.tools,
            "permissions": base.permissions.model_dump(mode="json") if base.permissions else None,
            "agent_profiles": {
                profile_id: profile.model_dump(mode="json", exclude_none=True)
                for profile_id, profile in base.agent_profiles.items()
            },
            "default_agent_profile_id": base.default_agent_profile_id,
        },
        "effective": agent_to_response(effective).model_dump(mode="json"),
        "default_agent_profile_id": resolve_agent_profile(effective).profile_id,
        "agent_profiles": agent_profile_options(effective),
    }


async def _system_agent_action(
    deps: AgentManagementDependencies,
    actor_email: str,
    arguments: dict[str, Any],
) -> dict[str, Any]:
    action = str(arguments["action"])
    base = _required_system_agent(arguments)
    if action == "system_overrides_get":
        return {
            "status": "ok",
            "system_agent": await _system_agent_payload(deps, actor_email, base),
        }
    if not base.allow_user_override:
        raise AgentManagementError(f"{base.agent_id} is an internal system agent and is read-only")
    expected = arguments.get("expected_updated_at")
    if expected is not None and not isinstance(expected, str):
        raise AgentManagementError("expected_updated_at must be a string")
    overrides: dict[str, Any] = {}
    if action == "system_overrides_update":
        raw_overrides = arguments.get("overrides")
        if not isinstance(raw_overrides, dict) or not raw_overrides:
            raise AgentManagementError("overrides must be a non-empty object")
        # Caller-scoped validation runs before the DB session; it may call providers.
        overrides = await _validated_system_overrides(deps, actor_email, base, raw_overrides)
    try:
        async with deps.session_factory() as session:
            if action == "system_overrides_update":
                await update_system_agent_override(
                    session,
                    base=base,
                    owner_email=actor_email,
                    updates=overrides,
                    expected_updated_at=expected,
                )
                status = "updated"
            elif action == "system_overrides_reset":
                deleted = await reset_system_agent_override(
                    session, base=base, owner_email=actor_email, expected_updated_at=expected
                )
                status = "reset" if deleted else "unchanged"
            else:
                disabled = action == "system_disable"
                await set_system_agent_disabled(
                    session,
                    base=base,
                    owner_email=actor_email,
                    disabled=disabled,
                    expected_updated_at=expected,
                )
                status = "disabled" if disabled else "enabled"
            await session.commit()
    except SystemAgentOverrideError as exc:
        raise AgentManagementError(str(exc)) from exc
    await _audit(deps, actor_email, base.agent_id, action, "success", arguments)
    return {
        "status": status,
        "system_agent": await _system_agent_payload(deps, actor_email, base),
    }


async def _validated_system_overrides(
    deps: AgentManagementDependencies,
    actor_email: str,
    base: AgentDefinition,
    overrides: dict[str, Any],
) -> dict[str, Any]:
    """Apply caller-scoped validation that the shared override owner cannot know.

    The core override module validates shapes and persistence invariants. Tool
    callers may additionally only reference providers, tools, skills,
    knowledgebases, and credentials that exist and are available to them.
    """

    validated = dict(overrides)
    catalog: dict[str, set[str]] | None = None

    async def _provider_catalog() -> dict[str, set[str]]:
        nonlocal catalog
        if catalog is None:
            async with deps.session_factory() as session:
                rows = await list_llm_providers(session)
            catalog = {row.provider_id: _configured_provider_models(row) for row in rows}
        return catalog

    async def _check_provider_model(provider_id: Any, model: Any, field: str) -> None:
        providers = await _provider_catalog()
        if provider_id is not None and provider_id not in providers:
            raise AgentManagementError(f"Invalid {field}.provider_id: {provider_id}")
        effective_provider = provider_id
        if effective_provider is None and base.llm_config is not None:
            effective_provider = base.llm_config.provider_id
        allowed = providers.get(effective_provider, set()) if effective_provider else set()
        if model is not None and allowed and model not in allowed:
            raise AgentManagementError(
                f"Invalid {field}.model for provider {effective_provider}: {model}"
            )

    llm_config = overrides.get("llm_config")
    if isinstance(llm_config, dict):
        await _check_provider_model(
            llm_config.get("provider_id"), llm_config.get("model"), "llm_config"
        )
        if {"tts_provider_id", "tts_model"} & llm_config.keys():
            shipped = base.llm_config.model_dump() if base.llm_config else {}
            _validate_tts_binding({**shipped, **llm_config}, await _provider_catalog())
    profiles = overrides.get("agent_profiles")
    if isinstance(profiles, dict):
        for profile_id, profile in profiles.items():
            if isinstance(profile, dict):
                await _check_provider_model(
                    profile.get("provider_id"),
                    profile.get("model"),
                    f"agent_profiles.{profile_id}",
                )

    tools = overrides.get("tools")
    if isinstance(tools, dict):
        _validate_system_tool_override(deps, base, tools)

    skills = overrides.get("skills")
    if isinstance(skills, dict):
        validated["skills"] = await _validated_system_skill_override(deps, actor_email, skills)

    permissions = overrides.get("permissions")
    if isinstance(permissions, dict):
        await _validate_system_permission_override(deps, actor_email, base, permissions)
    return validated


def _validate_system_tool_override(
    deps: AgentManagementDependencies, base: AgentDefinition, tools: dict[str, Any]
) -> None:
    # Only restrictive or inventory-checked keys are accepted. Omitting
    # builtin_tools would enable every default-on builtin, and inline/Intaris
    # MCP servers, opt-ins, or delegation tools would bypass caller inventory.
    unsupported = sorted(set(tools) - _SYSTEM_TOOL_OVERRIDE_KEYS)
    if unsupported:
        raise AgentManagementError(
            f"Unsupported system agent tools override keys: {', '.join(unsupported)}"
        )
    if not isinstance(tools.get("builtin_tools"), list):
        raise AgentManagementError(
            "tools.builtin_tools must be an explicit list; start from the effective tools"
        )
    for field in _SYSTEM_TOOL_OVERRIDE_KEYS:
        if tools.get(field) is not None:
            _validated_string_list(tools[field], f"tools.{field}")
    shipped = base.tools if isinstance(base.tools, dict) else {}
    shipped_ids = {
        item for key in ("builtin_tools", "allow_tools") for item in _string_list(shipped.get(key))
    }
    available = _available_tool_map(deps.assignable_tools)
    for group_id in _string_list(tools.get("tool_groups")):
        group = _tool_group_map().get(group_id)
        if group is None:
            raise AgentManagementError(f"Invalid tools.tool_groups: {group_id}")
        unavailable = sorted(tool_id for tool_id in group.tool_ids if tool_id not in available)
        if unavailable:
            raise AgentManagementError(
                f"Tool group {group_id} exceeds caller-effective assignable inventory: "
                + ", ".join(unavailable)
            )
    for field in ("builtin_tools", "allow_tools"):
        for tool_id in _string_list(tools.get(field)):
            if tool_id.removeprefix("builtin:") in _MANAGEMENT_TOOL_NAMES:
                raise AgentManagementError(f"System agents cannot be granted {tool_id}")
            if tool_id in shipped_ids:
                continue
            if deps.assignable_tools is None:
                raise AgentManagementError(
                    "Caller-effective assignable tool inventory is required to grant tools "
                    "beyond the shipped system agent tools"
                )
            if tool_id not in available:
                raise AgentManagementError(
                    f"Invalid tools.{field}: {tool_id} is not in the caller-effective "
                    "assignable inventory"
                )


async def _validated_system_skill_override(
    deps: AgentManagementDependencies, actor_email: str, skills: dict[str, Any]
) -> dict[str, Any]:
    # Other keys are runtime-injected state, and legacy inline items carry
    # tool_names grants that bypass caller inventory checks.
    unsupported = sorted(set(skills) - {"items"})
    if unsupported:
        raise AgentManagementError(
            f"Unsupported system agent skills override keys: {', '.join(unsupported)}"
        )
    raw_items = skills.get("items", [])
    if not isinstance(raw_items, list):
        raise AgentManagementError("skills.items must be a list")
    available = {
        item["skill_id"]: item for item in await _available_skill_options(deps, actor_email)
    }
    return {"items": _validated_skill_assignments(raw_items, available)}


async def _validate_system_permission_override(
    deps: AgentManagementDependencies,
    actor_email: str,
    base: AgentDefinition,
    permissions: dict[str, Any],
) -> None:
    knowledgebases = permissions.get("allowed_knowledgebases")
    if knowledgebases is not None:
        selected = _validated_string_list(knowledgebases, "permissions.allowed_knowledgebases")
        valid = {item["id"] for item in await _available_knowledgebase_options(deps, actor_email)}
        invalid = sorted(set(selected) - valid)
        if invalid:
            raise AgentManagementError(
                f"Invalid permissions.allowed_knowledgebases: {', '.join(invalid)}"
            )
    credentials = permissions.get("allowed_credentials")
    if credentials is not None:
        shipped = set(base.permissions.allowed_credentials) if base.permissions else set()
        for credential_id in _validated_string_list(credentials, "permissions.allowed_credentials"):
            if credential_id in shipped:
                continue
            if deps.credentials is None:
                raise AgentManagementError("Credential management is unavailable")
            credential = await deps.credentials.get_credential(credential_id, actor_email)
            try:
                credential_assignment_metadata(credential, owner_email=actor_email)
            except CredentialGrantError as exc:
                raise AgentManagementError(f"{exc}: {credential_id}") from exc


async def _system_agent_list_entries(
    deps: AgentManagementDependencies, actor_email: str
) -> list[dict[str, Any]]:
    registry = AgentRegistry(deps.session_factory)
    entries: list[dict[str, Any]] = []
    for base in SYSTEM_AGENTS.values():
        if base.hidden:
            continue
        effective = await registry.get(
            base.agent_id, owner_email=actor_email, include_disabled=True
        )
        assert effective is not None
        entries.append(
            {
                "agent_id": base.agent_id,
                "name": effective.name,
                "description": effective.description,
                "agent_type": effective.agent_type,
                "status": effective.status,
                "is_system": True,
                "manageable": base.allow_user_override,
                "management_mode": _system_management_mode(base),
                "has_overrides": bool(effective.has_overrides),
                "disabled": bool(effective.disabled),
                "default_agent_profile_id": resolve_agent_profile(effective).profile_id,
                "agent_profiles": agent_profile_options(effective),
            }
        )
    return entries


async def _require_owned_target(
    deps: AgentManagementDependencies,
    actor_email: str,
    current_agent_id: str,
    arguments: dict[str, Any],
) -> Any:
    agent_id = str(arguments.get("agent_id") or "").strip()
    if not agent_id:
        raise AgentManagementError("agent_id is required")
    if agent_id in SYSTEM_AGENTS:
        raise AgentManagementError(
            f"{agent_id} is a shipped system agent. Manage it with system_overrides_get, "
            "system_overrides_update, system_overrides_reset, system_disable, or system_enable."
        )
    async with deps.session_factory() as session:
        row = await get_agent(session, agent_id)
    if row is None:
        raise AgentManagementError("Agent not found")
    if row.owner_email != actor_email:
        raise AgentManagementError("Resource access denied")
    if getattr(row, "is_system", False):
        raise AgentManagementError("System agents are read-only")
    return row


def _agent_updates(arguments: dict[str, Any]) -> dict[str, Any]:
    allowed = {
        "name",
        "display_name",
        "description",
        "system_prompt",
        "personality",
        "avatar_image_id",
        "status",
    }
    return {key: arguments[key] for key in allowed if key in arguments}


def _agent_settings_payload(
    row: Any, deps: AgentManagementDependencies | None = None
) -> dict[str, Any]:
    tools = row.tools if isinstance(row.tools, dict) else None
    skills = row.skills if isinstance(row.skills, dict) else None
    permissions = row.permissions if isinstance(row.permissions, dict) else None
    llm_config = row.llm_config if isinstance(row.llm_config, dict) else None
    execution = strip_retired_execution_settings(
        row.execution if isinstance(row.execution, dict) else None
    )
    capabilities = AgentCapabilities.model_validate(row.capabilities or {}).model_dump(mode="json")
    return {
        "tools": tools,
        "tools_state": _tools_state(tools, raw_value=row.tools),
        "tool_assignment": _agent_tool_assignment_payload(row, deps),
        "skills": skills,
        "enabled_skills": _enabled_skill_ids(skills),
        "permissions": permissions,
        "llm_config": llm_config,
        "execution": execution,
        "auto_profile_selection": auto_profile_selection_settings(execution).model_dump(),
        "capabilities": capabilities,
        **{field: capabilities[field] for field in GUARDRAIL_SETTING_FIELDS},
        "escalation_timeout_seconds": capabilities.get("escalation_timeout_seconds"),
        "analytics_enabled": capabilities.get("analytics_enabled", True),
        "memory": {
            "backend": capabilities.get("memory_backend", "mnemory"),
            "options": capabilities.get("memory_backend_options", {}),
            "backends": memory_backend_descriptors(),
        },
        "workflow": {
            "step_agent_overrides": execution.get("step_agent_overrides", {}) if execution else {},
        },
        "executor": {
            "executor_id": execution.get("executor_id") if execution else None,
            "executor_selector": execution.get("executor_selector") if execution else None,
            "additional_executors": execution.get("additional_executors", []) if execution else [],
        },
    }


def _tools_state(tools: dict[str, Any] | None, *, raw_value: Any) -> dict[str, Any]:
    if raw_value is None:
        config_state = "default_inherited"
    elif isinstance(raw_value, dict):
        config_state = "explicit_config"
    else:
        config_state = "unavailable_invalid"
    builtin_tools = tools.get("builtin_tools") if tools else None
    if not isinstance(builtin_tools, list):
        builtin_mode = "default_all_except_default_off"
        configured_builtin_tools: list[str] | None = None
    else:
        configured_builtin_tools = _string_list(builtin_tools)
        if not configured_builtin_tools:
            builtin_mode = "explicit_none"
        elif "*" in configured_builtin_tools:
            builtin_mode = "explicit_all_except_default_off"
        else:
            builtin_mode = "explicit_allowlist"
    return {
        "config_state": config_state,
        "builtin_tools": {"mode": builtin_mode, "configured": configured_builtin_tools},
        "opt_in_builtin_tools": _string_list(tools.get("opt_in_builtin_tools")) if tools else [],
        "disabled_categories": _string_list(tools.get("disabled_categories")) if tools else [],
        "disabled_tools": _string_list(tools.get("disabled_tools")) if tools else [],
        "disabled_mcp_servers": _string_list(tools.get("disabled_mcp_servers")) if tools else [],
        "tool_groups": _string_list(tools.get("tool_groups")) if tools else [],
        "allow_tools": _string_list(tools.get("allow_tools")) if tools else [],
        "deny_tools": _string_list(tools.get("deny_tools")) if tools else [],
        "delegation_tools": tools.get("delegation_tools", True) if tools else True,
        "intaris_mcp_servers": _string_list(tools.get("intaris_mcp_servers")) if tools else [],
        "inline_mcp_servers": _inline_mcp_server_names(tools.get("mcp_servers")) if tools else [],
    }


def _validated_memory_capabilities(
    capabilities: dict[str, Any],
    settings: dict[str, Any],
) -> dict[str, Any]:
    """Apply backend/options atomically and return normalized capabilities."""

    candidate = dict(capabilities)
    if "memory_backend" in settings:
        candidate["memory_backend"] = settings["memory_backend"]
    if "memory_backend_options" in settings:
        candidate["memory_backend_options"] = settings["memory_backend_options"]
    elif candidate.get("memory_backend") == "none":
        candidate["memory_backend_options"] = {}
    try:
        validated = AgentCapabilities.model_validate(candidate).model_dump(mode="json")
    except ValueError as exc:
        raise AgentManagementError(str(exc)) from exc
    backend_id = validated["memory_backend"]
    try:
        get_backend("memory", backend_id)
    except ValueError:
        current_backend = capabilities.get("memory_backend")
        current_options = capabilities.get("memory_backend_options", {})
        if backend_id != current_backend or validated["memory_backend_options"] != current_options:
            raise AgentManagementError(f"Unknown memory_backend {backend_id!r}") from None
    return validated


async def _settings_updates(
    deps: AgentManagementDependencies,
    actor_email: str,
    row: Any,
    settings: dict[str, Any],
) -> dict[str, Any]:
    unknown = sorted(set(settings) - _settings_field_names())
    if unknown:
        raise AgentManagementError(
            "Unsupported settings field(s): "
            + ", ".join(unknown)
            + ". Call describe_tool for manage_agents to inspect supported operations."
        )

    async with deps.session_factory() as session:
        skill_ids = {
            skill.skill_id for skill in await list_skills(session, owner_email=actor_email)
        }
        executors = await list_executors(
            session,
            owner_email=actor_email,
            include_shared=True,
            defer_observed_tools=True,
        )
        executor_ids = {executor.executor_id for executor in executors}
        providers = await list_llm_providers(session)
        provider_ids = {provider.provider_id for provider in providers}
        provider_models = {
            provider.provider_id: _configured_provider_models(provider) for provider in providers
        }

    if settings.get("executor_id") is not None and settings.get("executor_selector") is not None:
        raise AgentManagementError("Set either executor_id or executor_selector, not both")
    selector = settings.get("executor_selector")
    if selector is not None:
        if (
            not isinstance(selector, dict)
            or not selector
            or any(
                not isinstance(key, str)
                or not key.strip()
                or not isinstance(value, str)
                or not value.strip()
                for key, value in selector.items()
            )
        ):
            raise AgentManagementError(
                "executor_selector must be a non-empty object of non-empty string labels"
            )
        if not any(labels_match(executor.labels, selector) for executor in executors):
            raise AgentManagementError("executor_selector does not match an available executor")

    tools = dict(row.tools) if isinstance(row.tools, dict) else {}
    skills = dict(row.skills) if isinstance(row.skills, dict) else {}
    permissions = dict(row.permissions) if isinstance(row.permissions, dict) else {}
    llm_config = dict(row.llm_config) if isinstance(row.llm_config, dict) else {}
    execution = dict(row.execution) if isinstance(row.execution, dict) else {}
    capabilities = dict(row.capabilities) if isinstance(row.capabilities, dict) else {}
    updates: dict[str, Any] = {}
    raw_updates: dict[str, Any] = {}
    memory_fields = {"memory_backend", "memory_backend_options"}
    if memory_fields.intersection(settings):
        capabilities = _validated_memory_capabilities(capabilities, settings)
        updates["capabilities"] = capabilities
    if set(GUARDRAIL_SETTING_FIELDS).intersection(settings):
        capabilities = _validated_guardrail_settings(capabilities, settings)
        updates["capabilities"] = capabilities
    for capability_field in ("escalation_timeout_seconds", "analytics_enabled"):
        if capability_field not in settings:
            continue
        candidate = {
            **capabilities,
            capability_field: settings[capability_field],
        }
        try:
            capabilities = AgentCapabilities.model_validate(candidate).model_dump(mode="json")
        except ValueError as exc:
            raise AgentManagementError(str(exc)) from exc
        updates["capabilities"] = capabilities

    ordered_settings = sorted(
        settings.items(),
        key=lambda item: 0 if item[0] == "provider_id" else 1 if item[0] == "model" else 2,
    )
    for field, value in ordered_settings:
        if field in {"tools", "skills", "permissions", "llm_config", "execution"}:
            raw_updates[field] = _nullable_object(value, field)
        elif field == "enabled_skills":
            selected = _validated_string_list(value, field, valid=skill_ids)
            skills["items"] = [{"skill_id": skill_id, "enabled": True} for skill_id in selected]
            updates["skills"] = skills
        elif field in {
            "auto_profile_selection",
            "step_agent_overrides",
            "executor_id",
            "executor_selector",
            "additional_executors",
        }:
            _apply_execution_setting(execution, field, value, executor_ids)
            updates["execution"] = execution
        elif field in {
            "provider_id",
            "model",
            "temperature",
            "max_tokens",
            "reasoning_effort",
            "voice",
            "voices",
            "tts_provider_id",
            "tts_model",
            "tts_voice",
        }:
            _apply_llm_setting(
                llm_config,
                field,
                value,
                provider_ids,
                provider_models,
            )
            updates["llm_config"] = llm_config
        elif field in {
            "memory_backend",
            "memory_backend_options",
            "escalation_timeout_seconds",
            "analytics_enabled",
            *GUARDRAIL_SETTING_FIELDS,
        }:
            # Validated atomically above so backend/options transitions do not
            # depend on input field order.
            continue
        elif field in {
            "builtin_tools",
            "opt_in_builtin_tools",
            "disabled_categories",
            "disabled_tools",
            "disabled_mcp_servers",
            "tool_groups",
            "allow_tools",
            "deny_tools",
            "intaris_mcp_servers",
            "mcp_servers",
        }:
            _apply_tools_setting(tools, field, value, row, deps.assignable_tools)
            updates["tools"] = tools
        elif field in {
            "tool_permissions",
            "allowed_knowledgebases",
            "allowed_secrets",
            "allowed_credentials",
            "delegation",
            "can_delegate",
            "max_delegation_depth",
        }:
            _apply_permissions_setting(permissions, field, value)
            updates["permissions"] = permissions
    if {"tts_provider_id", "tts_model"} & settings.keys():
        _validate_tts_binding(llm_config, provider_models)
    return {**updates, **raw_updates}


def _validate_tts_binding(llm_config: dict[str, Any], provider_models: dict[str, set[str]]) -> None:
    provider_id = _nullable_string(llm_config.get("tts_provider_id"), "tts_provider_id")
    model = _nullable_string(llm_config.get("tts_model"), "tts_model")
    if provider_id and provider_id not in provider_models:
        raise AgentManagementError(f"Invalid tts_provider_id: {provider_id}")
    if provider_id and not model:
        raise AgentManagementError("Choose a speech model for the selected TTS provider.")
    allowed_models = provider_models.get(provider_id, set()) if provider_id else set()
    if model is not None and allowed_models and model not in allowed_models:
        raise AgentManagementError(f"Invalid tts_model for provider {provider_id}: {model}")


def _settings_field_names() -> set[str]:
    return {
        *GUARDRAIL_SETTING_FIELDS,
        "auto_profile_selection",
        "executor_id",
        "executor_selector",
        "provider_id",
        "model",
        "temperature",
        "max_tokens",
        "reasoning_effort",
        "voice",
        "voices",
        "tts_provider_id",
        "tts_model",
        "tts_voice",
        "memory_backend",
        "memory_backend_options",
        "escalation_timeout_seconds",
        "analytics_enabled",
        "delegation",
    }


def _validated_guardrail_settings(
    capabilities: dict[str, Any], settings: dict[str, Any]
) -> dict[str, Any]:
    candidate = {
        **capabilities,
        **{field: settings[field] for field in GUARDRAIL_SETTING_FIELDS if field in settings},
    }
    try:
        return AgentCapabilities.model_validate(candidate).model_dump(mode="json")
    except ValueError as exc:
        raise AgentManagementError(str(exc)) from exc


def _apply_execution_setting(
    execution: dict[str, Any],
    field: str,
    value: Any,
    executor_ids: set[str],
) -> None:
    if field == "auto_profile_selection":
        if value is None:
            execution.pop(field, None)
        else:
            previous = auto_profile_selection_settings(execution).model_dump()
            merged = {**previous, **_object(value, field)}
            execution[field] = auto_profile_selection_settings({field: merged}).model_dump()
    elif field == "step_agent_overrides":
        execution[field] = _object(value, field)
    elif field == "executor_id":
        execution[field] = None if value is None else _enum(value, field, valid=executor_ids)
        if execution[field]:
            execution.pop("executor_selector", None)
    elif field == "executor_selector":
        execution[field] = _nullable_object(value, field)
        if execution[field]:
            execution.pop("executor_id", None)
    elif field == "additional_executors":
        if not isinstance(value, list):
            raise AgentManagementError("additional_executors must be a list")
        execution[field] = value


def _apply_llm_setting(
    llm_config: dict[str, Any],
    field: str,
    value: Any,
    provider_ids: set[str],
    provider_models: dict[str, set[str]],
) -> None:
    if field in {"provider_id", "tts_provider_id"}:
        llm_config[field] = _nullable_enum(value, field, valid=provider_ids)
    elif field == "model":
        model = _nullable_string(value, field)
        provider_id = llm_config.get("provider_id")
        allowed_models = (
            provider_models.get(provider_id, set()) if isinstance(provider_id, str) else set()
        )
        if model is not None and allowed_models and model not in allowed_models:
            raise AgentManagementError(f"Invalid model for provider {provider_id}: {model}")
        llm_config[field] = model
    elif field in {"reasoning_effort", "voice", "tts_model", "tts_voice"}:
        llm_config[field] = _nullable_string(value, field)
    elif field == "voices":
        voices = _object(value, field)
        if any(not isinstance(voice, str) for voice in voices.values()):
            raise AgentManagementError("voices values must be strings")
        llm_config[field] = dict(voices)
    elif field == "temperature":
        if value is not None and not isinstance(value, (int, float)):
            raise AgentManagementError("temperature must be a number")
        llm_config[field] = value
    elif field == "max_tokens":
        if value is not None and not isinstance(value, int):
            raise AgentManagementError("max_tokens must be an integer")
        llm_config[field] = value


def _configured_provider_models(provider: Any) -> set[str]:
    config = provider.config if isinstance(provider.config, dict) else {}
    raw_models = config.get("models")
    if not isinstance(raw_models, list):
        return set()
    values = {str(item) for item in raw_models if isinstance(item, str) and item}
    values.update(
        str(item.get("model_id") or item.get("id") or item.get("model"))
        for item in raw_models
        if isinstance(item, dict) and (item.get("model_id") or item.get("id") or item.get("model"))
    )
    return values


def _apply_tools_setting(
    tools: dict[str, Any],
    field: str,
    value: Any,
    row: Any,
    assignable_tools: list[Any] | None,
) -> None:
    if field == "builtin_tools":
        tools[field] = None if value is None else _validated_string_list(value, field)
    elif field == "opt_in_builtin_tools":
        selected = _validated_string_list(
            value,
            field,
            valid=set(_MANAGEMENT_TOOL_NAMES)
            | {f"builtin:{name}" for name in _MANAGEMENT_TOOL_NAMES},
        )
        if selected and getattr(row, "agent_type", "primary") == "secondary":
            raise AgentManagementError("secondary agents cannot opt into management tools")
        available = _available_tool_map(assignable_tools)
        if any(f"builtin:{item.removeprefix('builtin:')}" not in available for item in selected):
            raise AgentManagementError("Management opt-in exceeds caller-assignable inventory")
        tools[field] = [item.removeprefix("builtin:") for item in selected]
    elif field in {
        "disabled_categories",
        "disabled_tools",
        "disabled_mcp_servers",
        "intaris_mcp_servers",
    }:
        tools[field] = _validated_string_list(value, field)
    elif field == "tool_groups":
        tools[field] = _validated_string_list(value, field, valid=set(_tool_group_map()))
    elif field in {"allow_tools", "deny_tools"}:
        valid_tools = set(_available_tool_map(assignable_tools))
        selected = _validated_string_list(value, field, valid=valid_tools)
        if (
            field == "allow_tools"
            and getattr(row, "agent_type", "primary") == "secondary"
            and any(item.removeprefix("builtin:") in _MANAGEMENT_TOOL_NAMES for item in selected)
        ):
            raise AgentManagementError("secondary agents cannot hold management tools")
        tools[field] = selected
    elif field == "mcp_servers":
        if not isinstance(value, list):
            raise AgentManagementError("mcp_servers must be a list")
        tools[field] = value


def _apply_permissions_setting(permissions: dict[str, Any], field: str, value: Any) -> None:
    if field == "tool_permissions":
        permissions[field] = _object(value, field)
    elif field == "delegation":
        permissions[field] = AgentDelegationPolicy.model_validate(_object(value, field)).model_dump(
            mode="json"
        )
    elif field in {"allowed_secrets", "allowed_credentials", "allowed_knowledgebases"}:
        permissions[field] = _validated_string_list(value, field)
    elif field == "can_delegate":
        if not isinstance(value, bool):
            raise AgentManagementError("can_delegate must be a boolean")
        permissions[field] = value
    elif field == "max_delegation_depth":
        if not isinstance(value, int):
            raise AgentManagementError("max_delegation_depth must be an integer")
        permissions[field] = value


async def _intaris_mcp_server_options(deps: AgentManagementDependencies) -> list[dict[str, Any]]:
    if deps.guardrails is None:
        return []
    try:
        servers = await deps.guardrails.list_mcp_servers(enabled_only=True)
    except Exception:
        logger.debug("Unable to list Intaris MCP servers for agent settings schema", exc_info=True)
        return []
    return [
        {
            "id": item.get("name"),
            "label": item.get("name"),
            "transport": item.get("transport"),
            "enabled": item.get("enabled", True),
        }
        for item in servers
        if isinstance(item, dict) and item.get("name")
    ]


def _enabled_skill_ids(skills: dict[str, Any] | None) -> list[str]:
    if not skills:
        return []
    items = skills.get("items")
    if not isinstance(items, list):
        return []
    return [
        str(item["skill_id"])
        for item in items
        if isinstance(item, dict)
        and isinstance(item.get("skill_id"), str)
        and item.get("enabled") is not False
    ]


def _inline_mcp_server_names(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item.get("name")) for item in value if isinstance(item, dict) and item.get("name")]


def _validated_string_list(value: Any, field: str, *, valid: set[str] | None = None) -> list[str]:
    if not isinstance(value, list):
        raise AgentManagementError(f"{field} must be a list of strings")
    items = _string_list(value)
    if valid is not None:
        invalid = sorted(item for item in items if item not in valid)
        if invalid:
            raise AgentManagementError(f"Invalid {field}: {', '.join(invalid)}")
    return items


def _configured_tool_assignment(row: Any) -> dict[str, Any]:
    tools = row.tools if isinstance(row.tools, dict) else {}
    return {
        key: list(tools.get(key) or []) if key == "mcp_servers" else _string_list(tools.get(key))
        for key in TOOL_ASSIGNMENT_FIELDS
    }


def _tool_assignment_from_arguments(
    arguments: dict[str, Any], row: Any, *, default_empty: bool = False
) -> dict[str, Any]:
    current: dict[str, Any] = {key: [] for key in TOOL_ASSIGNMENT_FIELDS}
    if not default_empty:
        current = _configured_tool_assignment(row)
    raw_tools = arguments.get("tools")
    if raw_tools is not None:
        if not isinstance(raw_tools, dict):
            raise AgentManagementError("tools must be an object")
        source = raw_tools
    else:
        source = arguments
    for key in TOOL_ASSIGNMENT_FIELDS:
        if key not in source:
            continue
        if key == "mcp_servers":
            if not isinstance(source[key], list):
                raise AgentManagementError("mcp_servers must be a list")
            try:
                current[key] = [
                    MCPServerConfig.model_validate(item).model_dump(mode="json")
                    for item in source[key]
                ]
            except ValueError as exc:
                raise AgentManagementError(str(exc)) from exc
        else:
            current[key] = _validated_string_list(source[key], key)
    return current


def _agent_tool_assignment_payload(
    row: Any, deps: AgentManagementDependencies | None = None
) -> dict[str, Any]:
    configured = _configured_tool_assignment(row)
    assignable_tools = deps.assignable_tools if deps is not None else None
    validation = _validate_tool_assignment(row, configured, assignable_tools)
    tool_map = _available_tool_map(assignable_tools)
    return {
        "configured": configured,
        "effective_tools": _effective_assignment_tools(configured, tool_map),
        "validation": validation,
    }


def _available_tool_map(assignable_tools: list[Any] | None) -> dict[str, Any]:
    tools = assignable_tools or []
    mapping: dict[str, Any] = {}
    for tool in tools:
        mapping[stable_tool_id(tool)] = tool
        mapping.setdefault(tool.name, tool)
    return mapping


def _tool_group_map() -> dict[str, ToolGroupDefinition]:
    return {group.group_id: group for group in TOOL_GROUP_DEFINITIONS}


def _validate_tool_assignment(
    row: Any,
    configured: dict[str, list[str]],
    assignable_tools: list[Any] | None,
) -> dict[str, Any]:
    errors: list[dict[str, str]] = []
    warnings: list[dict[str, str]] = []
    tools = _available_tool_map(assignable_tools)
    if assignable_tools is None:
        return {
            "valid": False,
            "errors": [
                {
                    "field": "tools",
                    "id": "",
                    "reason": "Caller-effective assignable tool inventory is unavailable",
                }
            ],
            "warnings": [],
        }
    groups = _tool_group_map()
    for group_id in configured["tool_groups"]:
        if group_id not in groups:
            errors.append({"field": "tool_groups", "id": group_id, "reason": "Unknown tool group"})
            continue
        unavailable = sorted(
            tool_id for tool_id in groups[group_id].tool_ids if tool_id not in tools
        )
        if unavailable:
            errors.append(
                {
                    "field": "tool_groups",
                    "id": group_id,
                    "reason": (
                        "Tool group exceeds caller-effective assignable inventory: "
                        + ", ".join(unavailable)
                    ),
                }
            )
    from cognis.api.runtime_support import static_tool_definitions

    deny_ids = {
        stable_tool_id(tool) for tool in static_tool_definitions(knowledgebase_enabled=True)
    }
    for field in ("allow_tools", "deny_tools"):
        for tool_id in configured[field]:
            if tool_id not in tools and not (field == "deny_tools" and tool_id in deny_ids):
                errors.append({"field": field, "id": tool_id, "reason": "Unknown tool"})
    overlap = sorted(
        set(_normalize_tool_identifier(item, tools) for item in configured["allow_tools"])
        & set(_normalize_tool_identifier(item, tools) for item in configured["deny_tools"])
    )
    for tool_id in overlap:
        errors.append(
            {"field": "tools", "id": tool_id, "reason": "Tool cannot be both allowed and denied"}
        )
    if getattr(row, "agent_type", "primary") == "secondary":
        for tool_id in _effective_assignment_tools(configured, tools):
            tool = tools.get(tool_id)
            if tool and tool.name in _MANAGEMENT_TOOL_NAMES:
                errors.append(
                    {
                        "field": "allow_tools",
                        "id": tool_id,
                        "reason": "secondary agents cannot hold management tools",
                    }
                )
    if any(
        tool_id.startswith("builtin:knowledgebase_")
        for tool_id in _effective_assignment_tools(configured, tools)
    ) and not _assigned_knowledgebase_ids(row):
        warnings.append(
            {
                "field": "allowed_knowledgebases",
                "reason": "Knowledgebase tools are assigned but no knowledgebases are assigned",
            }
        )
    return {"valid": not errors, "errors": errors, "warnings": warnings}


def _effective_assignment_tools(
    configured: dict[str, list[str]], tools: dict[str, Any]
) -> list[str]:
    from cognis.api.runtime_support import tool_disabled_by_agent_config

    groups = _tool_group_map()
    selected: list[str] = []
    for group_id in configured["tool_groups"]:
        group = groups.get(group_id)
        if group:
            selected.extend(group.tool_ids)
    selected.extend(configured["allow_tools"])
    denied = set(_normalize_tool_identifier(item, tools) for item in configured["deny_tools"])
    normalized: list[str] = []
    for tool_id in selected:
        stable_id = _normalize_tool_identifier(tool_id, tools)
        tool = tools.get(tool_id)
        if tool is not None and tool_disabled_by_agent_config(
            tool,
            disabled_categories=set(),
            disabled_tools=set(configured.get("disabled_tools", [])),
            disabled_mcp_servers=set(configured.get("disabled_mcp_servers", [])),
        ):
            continue
        if stable_id not in denied and stable_id not in normalized:
            normalized.append(stable_id)
    return normalized


def _normalize_tool_identifier(identifier: str, tools: dict[str, Any]) -> str:
    tool = tools.get(identifier)
    return stable_tool_id(tool) if tool else identifier


def _apply_assignment_delta(current: list[Any], delta: list[Any], *, remove: bool) -> list[Any]:
    if remove:
        return [item for item in current if item not in delta]
    result = list(current)
    for item in delta:
        if item not in result:
            result.append(item)
    return result


def _assigned_knowledgebase_ids(row: Any) -> list[str]:
    permissions = row.permissions if isinstance(row.permissions, dict) else {}
    return _string_list(permissions.get("allowed_knowledgebases"))


async def _available_knowledgebase_options(
    deps: AgentManagementDependencies, actor_email: str
) -> list[dict[str, Any]]:
    async with deps.session_factory() as session:
        rows = await assignable_knowledgebases(
            session,
            actor_email=actor_email,
            caller_agent_id=deps.caller_agent_id,
            allowed_ids=deps.assignable_knowledgebase_ids,
        )
    return [{"id": row.knowledgebase_id, "name": row.name, "status": row.status} for row in rows]


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item) for item in value if isinstance(item, str) and item.strip()]


def _enum(value: Any, field: str, *, valid: set[str]) -> str:
    if not isinstance(value, str) or value not in valid:
        if isinstance(value, str):
            raise AgentManagementError(f"Invalid {field}: {value}")
        raise AgentManagementError(f"{field} must be one of: {', '.join(sorted(valid))}")
    return value


def _nullable_enum(value: Any, field: str, *, valid: set[str]) -> str | None:
    if value is None or value == "":
        return None
    return _enum(value, field, valid=valid)


def _nullable_string(value: Any, field: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise AgentManagementError(f"{field} must be a string")
    return value


def _object(value: Any, field: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise AgentManagementError(f"{field} must be an object")
    return value


def _nullable_object(value: Any, field: str) -> dict[str, Any] | None:
    if value is None:
        return None
    return _object(value, field)


async def _sync_identity(
    deps: AgentManagementDependencies, row: Any, *, previous_content: str | None
) -> None:
    if deps.memory is None:
        return
    definition = AgentDefinition.model_validate(agent_to_response(row).model_dump())
    try:
        replace_identity = getattr(deps.memory, "replace_bootstrap_identity", None)
        if callable(replace_identity):
            await asyncio.wait_for(
                replace_identity(
                    definition,
                    previous_content=previous_content,
                    allow_legacy_cleanup=True,
                ),
                timeout=60.0,
            )
        else:
            await asyncio.wait_for(deps.memory.bootstrap_agent(definition), timeout=60.0)
        await _persist_sync_metadata(deps, row.agent_id, True)
    except Exception as exc:
        safe_detail = sanitize_client_error_detail(exc, fallback="Mnemory bootstrap failed")
        logger.warning(
            "Mnemory personality sync failed during agent management",
            extra={"extra_data": {"agent_id": row.agent_id}},
            exc_info=True,
        )
        await _persist_sync_metadata(deps, row.agent_id, False, safe_detail)


async def _persist_sync_metadata(
    deps: AgentManagementDependencies, agent_id: str, synced: bool, error_detail: str | None = None
) -> None:
    async with deps.session_factory() as session:
        row = await get_agent(session, agent_id)
        if row is None:
            return
        row.sync_metadata = sync_metadata(synced, error_detail)
        await session.commit()


async def _publish_profile_updated(deps: AgentManagementDependencies, agent_id: str) -> None:
    if deps.event_bus is None:
        return
    await deps.event_bus.publish(
        Event(type=EventType.AGENT_PROFILE_UPDATED, data={"agent_id": agent_id})
    )


async def _generate_avatar(
    deps: AgentManagementDependencies, actor_email: str, arguments: dict[str, Any]
) -> str:
    if deps.image_generation_provider is None or deps.artifact_store is None:
        raise AgentManagementError("Image generation is not available")
    prompt = _optional_string(arguments.get("avatar_prompt"))
    if not prompt:
        prompt = await _generate_avatar_prompt(deps, arguments)
    result = await deps.image_generation_provider.image_generate(
        prompt=prompt,
        task_type="image_generation",
        n=1,
        size=str(arguments.get("avatar_size") or "1024x1024"),
        quality=_optional_string(arguments.get("avatar_quality")),
    )
    if not result.images:
        raise AgentManagementError("No avatar image returned by the model")
    image_id = deps.artifact_store.generate_id("img")
    image = result.images[0]
    image_bytes = await _image_bytes(image)
    await deps.artifact_store.async_save(
        "avatars",
        image_id,
        "image",
        image_bytes,
        image.content_type,
        owner_email=actor_email,
    )
    return str(image_id)


async def _generate_avatar_prompt(
    deps: AgentManagementDependencies, arguments: dict[str, Any]
) -> str:
    name = str(arguments.get("name") or "AI assistant")
    description = _optional_string(arguments.get("description")) or ""
    personality = _optional_dict(arguments.get("personality")) or {}
    if deps.llm is None:
        return f"A professional, modern avatar for an AI assistant named '{name}'."
    context_parts = [f"Agent name: {name}"]
    if description:
        context_parts.append(f"Description: {description}")
    for key in ("purpose", "tone"):
        value = personality.get(key)
        if isinstance(value, str) and value.strip():
            context_parts.append(f"{key.title()}: {value.strip()}")
    try:
        response = await deps.llm.generate(
            messages=[
                {
                    "role": "system",
                    "content": (
                        "Generate one concise professional avatar image prompt for an AI agent. "
                        "Output only the prompt. Do not include text or words in the image."
                    ),
                },
                {"role": "user", "content": "\n".join(context_parts)},
            ],
            task_type="default",
            temperature=1.0,
        )
        choices = response.get("choices", [])
        content = choices[0].get("message", {}).get("content") if choices else None
        if isinstance(content, str) and content.strip():
            return content.strip()
    except Exception:
        logger.warning("Agent avatar prompt generation failed", exc_info=True)
    return f"A professional, modern avatar for an AI assistant named '{name}'."


async def _audit(
    deps: AgentManagementDependencies,
    actor_email: str,
    agent_id: str | None,
    operation: str,
    outcome: str,
    arguments: dict[str, Any],
) -> None:
    safe_details = {
        "operation": operation,
        "outcome": outcome,
        "target_agent_id": arguments.get("agent_id"),
        "profile_id": arguments.get("profile_id"),
        "replacement_profile_id": arguments.get("replacement_profile_id"),
        "migrated_references": arguments.get("migrated_references"),
        "grant_id": arguments.get("grant_id"),
        "credential_id": arguments.get("credential_id"),
        "grantee_email": arguments.get("grantee_email"),
    }
    async with deps.session_factory() as session:
        session.add(
            AuditLog(
                log_id=f"audit_{uuid.uuid4().hex[:12]}",
                event_type="agent_management_tool",
                user_email=actor_email,
                agent_id=agent_id,
                details=safe_details,
            )
        )
        await session.commit()


def _grant_payload(row: Any) -> dict[str, Any]:
    return {
        "grant_id": row.grant_id,
        "agent_id": row.agent_id,
        "grantee_type": row.grantee_type,
        "grantee_user_email": row.grantee_user_email,
        "grantee_group_id": row.grantee_group_id,
        "permission": row.permission,
        "executor_scope": row.executor_scope,
        "granted_by": row.granted_by,
        "granted_at": row.granted_at.isoformat() if row.granted_at else None,
        "revoked_at": row.revoked_at.isoformat() if row.revoked_at else None,
        "note": row.note,
    }


def _optional_string(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    value = value.strip()
    return value or None


def _optional_dict(value: Any) -> dict[str, Any] | None:
    return value if isinstance(value, dict) else None


def result_to_json(data: dict[str, Any]) -> str:
    return json.dumps(data, indent=2, sort_keys=True, default=str)
