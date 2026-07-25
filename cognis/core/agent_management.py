"""Shared agent-management operations for API routes and controller tools."""

from __future__ import annotations

import asyncio
import contextlib
import json
import re
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select, update

from cognis.api.error_sanitizer import sanitize_client_error_detail
from cognis.api.serializers import agent_to_response
from cognis.core.agent_profiles import (
    agent_profile_options,
    resolve_agent_profile,
    validate_agent_profile_configuration,
)
from cognis.core.agent_registry import SYSTEM_AGENTS, validate_agent_id
from cognis.core.events import Event, EventType
from cognis.logging import get_logger
from cognis.models.agent import AgentCapabilities, AgentDefinition, AgentRuntimeProfile
from cognis.models.tool import stable_tool_id
from cognis.providers.backends import get_backend
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
    get_user,
    list_agent_grants,
    list_agents,
    list_executors,
    list_knowledgebases,
    list_llm_providers,
    list_secondary_bindings,
    list_skills,
    list_workflows,
    revoke_agent_grant,
    set_agent_status,
    set_secondary_bindings,
    update_agent,
    update_agent_grant,
    update_agent_if_updated_at,
)
from cognis.tools.builtin.image import _image_bytes
from cognis.tools.builtin.knowledgebase import knowledgebase_tools

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
) -> dict[str, Any]:
    """Execute a single owner-scoped agent-management action."""

    action = str(arguments.get("action") or "").strip()
    if not action:
        raise AgentManagementError("action is required")

    try:
        if action == "list":
            return await _list_agents(deps, actor_email, current_agent_id)
        if action == "get":
            return await _get_agent(deps, actor_email, current_agent_id, arguments)
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
                "manageable": row.agent_id != current_agent_id and not row.is_system,
                "default_agent_profile_id": resolve_agent_profile(
                    agent_definitions[row.agent_id]
                ).profile_id,
                "agent_profiles": agent_profile_options(agent_definitions[row.agent_id]),
            }
            for row in rows
            if not getattr(row, "is_system", False)
        ],
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
    await _publish_profile_updated(deps, row.agent_id)
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
    await _publish_profile_updated(deps, row.agent_id)
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
    await _publish_profile_updated(deps, row.agent_id)
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
                model.agent_id == agent_id,
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
            for key in ("tool_groups", "allow_tools", "deny_tools")
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

    avatar_image_id = arguments.get("avatar_image_id")
    if arguments.get("generate_avatar"):
        avatar_image_id = await _generate_avatar(deps, actor_email, arguments)
    assigned_knowledgebases = _validated_string_list(
        arguments.get("assigned_knowledgebases", []),
        "assigned_knowledgebases",
    )

    async with deps.session_factory() as session:
        if await get_agent(session, agent_id) is not None:
            raise AgentManagementError("Agent already exists")
        available_knowledgebases = await list_knowledgebases(
            session,
            owner_email=actor_email,
        )
        available_knowledgebase_ids = {row.knowledgebase_id for row in available_knowledgebases}
        if deps.assignable_knowledgebase_ids is not None:
            available_knowledgebase_ids &= deps.assignable_knowledgebase_ids
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
                    stable_tool_id(tool)
                    for tool in deps.assignable_tools
                    if tool.source.type == "builtin"
                ),
                "allow_tools": sorted(assignable_ids),
                "deny_tools": denied_static_ids,
            },
            permissions=(
                {"allowed_knowledgebases": assigned_knowledgebases}
                if assigned_knowledgebases
                else None
            ),
            llm_config=None,
            execution=None,
            avatar_image_id=_optional_string(avatar_image_id),
            agent_type=str(arguments.get("agent_type") or "primary"),
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
    raw_ids = arguments.get("secondary_agent_ids")
    if not isinstance(raw_ids, list):
        raise AgentManagementError("secondary_agent_ids must be a list")
    ids = [str(item) for item in raw_ids if isinstance(item, str) and item.strip()]
    async with deps.session_factory() as session:
        for secondary_id in ids:
            secondary = await get_agent(session, secondary_id)
            if secondary is None or secondary.owner_email != actor_email:
                raise AgentManagementError(
                    f"Secondary agent not found or not owned: {secondary_id}"
                )
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
    row = await _require_owned_target(deps, actor_email, current_agent_id, arguments)
    grantee_email = str(arguments.get("grantee_email") or "").strip()
    if not grantee_email:
        raise AgentManagementError("grantee_email is required")
    if grantee_email == actor_email:
        raise AgentManagementError("Agent owner cannot be granted their own agent")
    executor_scope = str(arguments.get("executor_scope") or "owner_executor")
    if executor_scope not in {"owner_executor", "grantee_executor"}:
        raise AgentManagementError("executor_scope must be owner_executor or grantee_executor")
    async with deps.session_factory() as session:
        if await get_user(session, grantee_email) is None:
            raise AgentManagementError("Grantee user not found")
        existing = await get_agent_grant_for_user(session, row.agent_id, grantee_email)
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
        await session.commit()
    await _audit(deps, actor_email, row.agent_id, "share_create", "success", arguments)
    return {"status": "shared", "share": _grant_payload(grant)}


async def _share_update(
    deps: AgentManagementDependencies,
    actor_email: str,
    current_agent_id: str,
    arguments: dict[str, Any],
) -> dict[str, Any]:
    row = await _require_owned_target(deps, actor_email, current_agent_id, arguments)
    grant_id = str(arguments.get("grant_id") or "").strip()
    if not grant_id:
        raise AgentManagementError("grant_id is required")
    executor_scope = str(arguments.get("executor_scope") or "owner_executor")
    if executor_scope not in {"owner_executor", "grantee_executor"}:
        raise AgentManagementError("executor_scope must be owner_executor or grantee_executor")
    async with deps.session_factory() as session:
        grant = await get_agent_grant(session, grant_id)
        if grant is None or grant.agent_id != row.agent_id or grant.revoked_at is not None:
            raise AgentManagementError("Grant not found")
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
        await session.commit()
    await _audit(deps, actor_email, row.agent_id, "share_update", "success", arguments)
    return {"status": "updated", "share": _grant_payload(updated)}


async def _share_revoke(
    deps: AgentManagementDependencies,
    actor_email: str,
    current_agent_id: str,
    arguments: dict[str, Any],
) -> dict[str, Any]:
    row = await _require_owned_target(deps, actor_email, current_agent_id, arguments)
    grant_id = str(arguments.get("grant_id") or "").strip()
    if not grant_id:
        raise AgentManagementError("grant_id is required")
    running_task_ids: list[str] = []
    async with deps.session_factory() as session:
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
        await session.commit()

    if deps.task_queue is not None:
        for task_id in running_task_ids:
            with contextlib.suppress(Exception):
                await deps.task_queue.pause_task(task_id)
    await _audit(deps, actor_email, row.agent_id, "share_revoke", "success", arguments)
    return {"status": "revoked", "ok": True, "grant_id": grant_id}


async def _require_owned_target(
    deps: AgentManagementDependencies,
    actor_email: str,
    current_agent_id: str,
    arguments: dict[str, Any],
) -> Any:
    agent_id = str(arguments.get("agent_id") or "").strip()
    if not agent_id:
        raise AgentManagementError("agent_id is required")
    if agent_id == current_agent_id:
        raise AgentManagementError("An agent cannot manage itself")
    if agent_id in SYSTEM_AGENTS:
        raise AgentManagementError("System agents are read-only")
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
    execution = row.execution if isinstance(row.execution, dict) else None
    capabilities = row.capabilities if isinstance(row.capabilities, dict) else {}
    return {
        "tools": tools,
        "tools_state": _tools_state(tools, raw_value=row.tools),
        "tool_assignment": _agent_tool_assignment_payload(row, deps),
        "skills": skills,
        "enabled_skills": _enabled_skill_ids(skills),
        "permissions": permissions,
        "llm_config": llm_config,
        "execution": execution,
        "capabilities": capabilities,
        "memory": {
            "backend": capabilities.get("memory_backend", "mnemory"),
            "options": capabilities.get("memory_backend_options", {}),
            "backends": memory_backend_descriptors(),
        },
        "workflow": {
            "available_workflow_ids": _string_list(execution.get("available_workflow_ids"))
            if execution
            else [],
            "default_workflow_id": execution.get("default_workflow_id") if execution else None,
            "workflow_selection_mode": execution.get("workflow_selection_mode", "automatic")
            if execution
            else "automatic",
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
        workflow_ids = {
            workflow.workflow_id
            for workflow in await list_workflows(session, owner_email=actor_email)
        }
        skill_ids = {
            skill.skill_id for skill in await list_skills(session, owner_email=actor_email)
        }
        executor_ids = {
            executor.executor_id
            for executor in await list_executors(
                session, owner_email=actor_email, include_shared=True
            )
        }
        providers = await list_llm_providers(session)
        provider_ids = {provider.provider_id for provider in providers}
        provider_models = {
            provider.provider_id: _configured_provider_models(provider) for provider in providers
        }

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

    ordered_settings = sorted(
        settings.items(),
        key=lambda item: (
            0
            if item[0] in {"provider_id", "available_workflow_ids"}
            else 1
            if item[0] in {"model", "default_workflow_id"}
            else 2
        ),
    )
    for field, value in ordered_settings:
        if field in {"tools", "skills", "permissions", "llm_config", "execution"}:
            raw_updates[field] = _nullable_object(value, field)
        elif field == "enabled_skills":
            selected = _validated_string_list(value, field, valid=skill_ids)
            skills["items"] = [{"skill_id": skill_id, "enabled": True} for skill_id in selected]
            updates["skills"] = skills
        elif field in {
            "available_workflow_ids",
            "default_workflow_id",
            "workflow_selection_mode",
            "step_agent_overrides",
            "executor_id",
            "executor_selector",
            "additional_executors",
        }:
            _apply_execution_setting(execution, field, value, workflow_ids, executor_ids)
            updates["execution"] = execution
        elif field in {
            "provider_id",
            "model",
            "temperature",
            "max_tokens",
            "reasoning_effort",
            "voice",
        }:
            _apply_llm_setting(
                llm_config,
                field,
                value,
                provider_ids,
                provider_models,
            )
            updates["llm_config"] = llm_config
        elif field in {"memory_backend", "memory_backend_options"}:
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
            "can_delegate",
            "max_delegation_depth",
        }:
            _apply_permissions_setting(permissions, field, value)
            updates["permissions"] = permissions
    return {**updates, **raw_updates}


def _settings_field_names() -> set[str]:
    return {
        "available_workflow_ids",
        "default_workflow_id",
        "workflow_selection_mode",
        "provider_id",
        "model",
        "temperature",
        "max_tokens",
        "reasoning_effort",
        "voice",
        "memory_backend",
        "memory_backend_options",
    }


def _apply_execution_setting(
    execution: dict[str, Any],
    field: str,
    value: Any,
    workflow_ids: set[str],
    executor_ids: set[str],
) -> None:
    if field == "available_workflow_ids":
        execution[field] = _validated_string_list(value, field, valid=workflow_ids)
    elif field == "default_workflow_id":
        execution[field] = _nullable_enum(value, field, valid=workflow_ids)
    elif field == "workflow_selection_mode":
        execution[field] = _enum(value, field, valid={"automatic", "always_ask", "use_default"})
    elif field == "step_agent_overrides":
        execution[field] = _object(value, field)
    elif field == "executor_id":
        execution[field] = _nullable_enum(value, field, valid=executor_ids)
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
    if field == "provider_id":
        llm_config[field] = _nullable_enum(value, field, valid=provider_ids)
    elif field == "model":
        model = _nullable_string(value, field)
        provider_id = llm_config.get("provider_id")
        allowed_models = provider_models.get(provider_id, set())
        if model is not None and allowed_models and model not in allowed_models:
            raise AgentManagementError(f"Invalid model for provider {provider_id}: {model}")
        llm_config[field] = model
    elif field in {"reasoning_effort", "voice"}:
        llm_config[field] = _nullable_string(value, field)
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
            valid={
                "manage_agents",
                "builtin:manage_agents",
                "manage_mcp",
                "builtin:manage_mcp",
            },
        )
        if selected and getattr(row, "agent_type", "primary") == "secondary":
            raise AgentManagementError("secondary agents cannot opt into management tools")
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
        tools[field] = _validated_string_list(value, field, valid=valid_tools)
    elif field == "mcp_servers":
        if not isinstance(value, list):
            raise AgentManagementError("mcp_servers must be a list")
        tools[field] = value


def _apply_permissions_setting(permissions: dict[str, Any], field: str, value: Any) -> None:
    if field == "tool_permissions":
        permissions[field] = _object(value, field)
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


def _configured_tool_assignment(row: Any) -> dict[str, list[str]]:
    tools = row.tools if isinstance(row.tools, dict) else {}
    return {
        "tool_groups": _string_list(tools.get("tool_groups")),
        "allow_tools": _string_list(tools.get("allow_tools")),
        "deny_tools": _string_list(tools.get("deny_tools")),
    }


def _tool_assignment_from_arguments(
    arguments: dict[str, Any], row: Any, *, default_empty: bool = False
) -> dict[str, list[str]]:
    current = {"tool_groups": [], "allow_tools": [], "deny_tools": []}
    if not default_empty:
        current = _configured_tool_assignment(row)
    raw_tools = arguments.get("tools")
    if raw_tools is not None:
        if not isinstance(raw_tools, dict):
            raise AgentManagementError("tools must be an object")
        source = raw_tools
    else:
        source = arguments
    return {
        key: _validated_string_list(source[key], key) if key in source else list(current[key])
        for key in ("tool_groups", "allow_tools", "deny_tools")
    }


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
    for field in ("allow_tools", "deny_tools"):
        for tool_id in configured[field]:
            if tool_id not in tools:
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
            if tool and tool.name == "manage_agents":
                errors.append(
                    {
                        "field": "allow_tools",
                        "id": tool_id,
                        "reason": "secondary agents cannot manage agents",
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
        if stable_id not in denied and stable_id not in normalized:
            normalized.append(stable_id)
    return normalized


def _normalize_tool_identifier(identifier: str, tools: dict[str, Any]) -> str:
    tool = tools.get(identifier)
    return stable_tool_id(tool) if tool else identifier


def _apply_assignment_delta(current: list[str], delta: list[str], *, remove: bool) -> list[str]:
    if remove:
        removal = set(delta)
        return [item for item in current if item not in removal]
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
        rows = await list_knowledgebases(session, owner_email=actor_email)
    if deps.assignable_knowledgebase_ids is not None:
        rows = [row for row in rows if row.knowledgebase_id in deps.assignable_knowledgebase_ids]
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
    return image_id


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
