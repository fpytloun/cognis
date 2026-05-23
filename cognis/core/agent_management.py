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

from sqlalchemy import select

from cognis.api.error_sanitizer import sanitize_client_error_detail
from cognis.api.serializers import agent_to_response
from cognis.core.agent_registry import SYSTEM_AGENTS, validate_agent_id
from cognis.core.events import Event, EventType
from cognis.logging import get_logger
from cognis.models.agent import AgentDefinition
from cognis.models.tool import stable_tool_id
from cognis.store.models import AuditLog, Schedule, Task
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
    list_llm_providers,
    list_secondary_bindings,
    list_skills,
    list_workflows,
    revoke_agent_grant,
    set_agent_status,
    set_secondary_bindings,
    update_agent,
    update_agent_grant,
)
from cognis.tools.builtin.image import _image_bytes

logger = get_logger(__name__)


class AgentManagementError(ValueError):
    """Expected user-facing agent-management failure."""


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
        if action == "settings_schema":
            return await _settings_schema(deps, actor_email, current_agent_id, arguments)
        if action == "settings_update":
            return await _settings_update(deps, actor_email, current_agent_id, arguments)
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
    agent["settings"] = _agent_settings_payload(row)
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
        "settings": _agent_settings_payload(row),
    }


async def _settings_schema(
    deps: AgentManagementDependencies,
    actor_email: str,
    current_agent_id: str,
    arguments: dict[str, Any],
) -> dict[str, Any]:
    row = await _require_owned_target(deps, actor_email, current_agent_id, arguments)
    return {
        "status": "ok",
        "agent_id": row.agent_id,
        "fields": await _agent_settings_schema(deps, actor_email, row),
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
        "settings": _agent_settings_payload(row),
        "agent": agent_to_response(row).model_dump(mode="json"),
    }


async def _create_agent(
    deps: AgentManagementDependencies, actor_email: str, arguments: dict[str, Any]
) -> dict[str, Any]:
    name = str(arguments.get("name") or "").strip()
    if not name:
        raise AgentManagementError("name is required for create")
    agent_id = str(arguments.get("agent_id") or slugify_agent_id(name)).strip()
    try:
        validate_agent_id(agent_id)
    except ValueError as exc:
        raise AgentManagementError(str(exc)) from exc

    avatar_image_id = arguments.get("avatar_image_id")
    if arguments.get("generate_avatar"):
        avatar_image_id = await _generate_avatar(deps, actor_email, arguments)

    async with deps.session_factory() as session:
        if await get_agent(session, agent_id) is not None:
            raise AgentManagementError("Agent already exists")
        row = await create_agent(
            session,
            agent_id=agent_id,
            owner_email=actor_email,
            name=name,
            display_name=_optional_string(arguments.get("display_name")) or name,
            description=_optional_string(arguments.get("description")),
            system_prompt=_optional_string(arguments.get("system_prompt")),
            personality=_optional_dict(arguments.get("personality")),
            skills=_optional_dict(arguments.get("skills")),
            tools=_optional_dict(arguments.get("tools")),
            permissions=_optional_dict(arguments.get("permissions")),
            llm_config=_optional_dict(arguments.get("llm_config")),
            execution=_optional_dict(arguments.get("execution")),
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
        "skills",
        "tools",
        "permissions",
        "llm_config",
        "execution",
        "avatar_image_id",
        "status",
    }
    return {key: arguments[key] for key in allowed if key in arguments}


def _agent_settings_payload(row: Any) -> dict[str, Any]:
    tools = row.tools if isinstance(row.tools, dict) else None
    skills = row.skills if isinstance(row.skills, dict) else None
    permissions = row.permissions if isinstance(row.permissions, dict) else None
    llm_config = row.llm_config if isinstance(row.llm_config, dict) else None
    execution = row.execution if isinstance(row.execution, dict) else None
    return {
        "tools": tools,
        "tools_state": _tools_state(tools, raw_value=row.tools),
        "skills": skills,
        "enabled_skills": _enabled_skill_ids(skills),
        "permissions": permissions,
        "llm_config": llm_config,
        "execution": execution,
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
        "delegation_tools": tools.get("delegation_tools", True) if tools else True,
        "intaris_mcp_servers": _string_list(tools.get("intaris_mcp_servers")) if tools else [],
        "inline_mcp_servers": _inline_mcp_server_names(tools.get("mcp_servers")) if tools else [],
    }


async def _agent_settings_schema(
    deps: AgentManagementDependencies, actor_email: str, row: Any
) -> dict[str, Any]:
    from cognis.api.runtime_support import static_tool_definitions

    async with deps.session_factory() as session:
        workflows = await list_workflows(session, owner_email=actor_email)
        skills = await list_skills(session, owner_email=actor_email)
        executors = await list_executors(session, owner_email=actor_email, include_shared=True)
        providers = await list_llm_providers(session)

    tool_options = [
        {
            "id": stable_tool_id(tool),
            "name": tool.name,
            "label": tool.name,
            "category": tool.category,
            "source": tool.source.model_dump(mode="json"),
        }
        for tool in static_tool_definitions()
    ]
    fields: dict[str, Any] = {
        "available_workflow_ids": {
            "type": "multi_select",
            "storage": "execution.available_workflow_ids",
            "options": [
                {"id": item.workflow_id, "label": item.name, "is_system": item.is_system}
                for item in workflows
            ],
        },
        "default_workflow_id": {
            "type": "enum",
            "nullable": True,
            "storage": "execution.default_workflow_id",
            "options": [
                {"id": item.workflow_id, "label": item.name, "is_system": item.is_system}
                for item in workflows
            ],
        },
        "workflow_selection_mode": {
            "type": "enum",
            "storage": "execution.workflow_selection_mode",
            "options": [
                {"id": "automatic", "label": "automatic"},
                {"id": "always_ask", "label": "always_ask"},
                {"id": "use_default", "label": "use_default"},
            ],
        },
        "step_agent_overrides": {"type": "object", "storage": "execution.step_agent_overrides"},
        "executor_id": {
            "type": "enum",
            "nullable": True,
            "storage": "execution.executor_id",
            "options": [
                {
                    "id": item.executor_id,
                    "label": item.name,
                    "executor_type": item.executor_type,
                    "is_default": item.is_default,
                    "labels": item.labels or {},
                }
                for item in executors
            ],
        },
        "executor_selector": {"type": "object", "storage": "execution.executor_selector"},
        "additional_executors": {"type": "list", "storage": "execution.additional_executors"},
        "provider_id": {
            "type": "enum",
            "nullable": True,
            "storage": "llm_config.provider_id",
            "options": [
                {"id": item.provider_id, "label": item.display_name, "location": item.location}
                for item in providers
            ],
        },
        "model": {"type": "string", "nullable": True, "storage": "llm_config.model"},
        "temperature": {"type": "number", "nullable": True, "storage": "llm_config.temperature"},
        "max_tokens": {"type": "integer", "nullable": True, "storage": "llm_config.max_tokens"},
        "reasoning_effort": {
            "type": "string",
            "nullable": True,
            "storage": "llm_config.reasoning_effort",
        },
        "voice": {"type": "string", "nullable": True, "storage": "llm_config.voice"},
        "builtin_tools": {
            "type": "multi_select",
            "nullable": True,
            "storage": "tools.builtin_tools",
            "options": [{"id": "*", "label": "All non-default-off built-in tools"}, *tool_options],
        },
        "opt_in_builtin_tools": {
            "type": "multi_select",
            "storage": "tools.opt_in_builtin_tools",
            "options": [
                option for option in tool_options if option["id"] == "builtin:manage_agents"
            ],
        },
        "disabled_categories": {
            "type": "multi_select",
            "storage": "tools.disabled_categories",
            "options": [
                {"id": category, "label": category}
                for category in sorted({str(option["category"]) for option in tool_options})
            ],
        },
        "disabled_tools": {
            "type": "multi_select",
            "storage": "tools.disabled_tools",
            "options": tool_options,
        },
        "intaris_mcp_servers": {
            "type": "multi_select",
            "storage": "tools.intaris_mcp_servers",
            "options": await _intaris_mcp_server_options(deps),
        },
        "mcp_servers": {"type": "list", "storage": "tools.mcp_servers"},
        "enabled_skills": {
            "type": "multi_select",
            "storage": "skills.items",
            "options": [
                {
                    "id": item.skill_id,
                    "label": item.name,
                    "is_system": item.is_system,
                    "attach_to_all_agents": item.auto_load,
                }
                for item in skills
            ],
        },
        "tool_permissions": {"type": "object", "storage": "permissions.tool_permissions"},
        "allowed_secrets": {"type": "multi_select", "storage": "permissions.allowed_secrets"},
        "allowed_credentials": {
            "type": "multi_select",
            "storage": "permissions.allowed_credentials",
        },
        "can_delegate": {"type": "boolean", "storage": "permissions.can_delegate"},
        "max_delegation_depth": {"type": "integer", "storage": "permissions.max_delegation_depth"},
        "tools": {"type": "object", "storage": "tools", "advanced": True},
        "skills": {"type": "object", "storage": "skills", "advanced": True},
        "permissions": {"type": "object", "storage": "permissions", "advanced": True},
        "llm_config": {"type": "object", "storage": "llm_config", "advanced": True},
        "execution": {"type": "object", "storage": "execution", "advanced": True},
    }
    if getattr(row, "agent_type", "primary") == "secondary":
        fields["opt_in_builtin_tools"]["readonly_reason"] = "secondary agents cannot manage agents"
    return fields


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
            + ". Call settings_schema to discover valid fields."
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
        provider_ids = {provider.provider_id for provider in await list_llm_providers(session)}

    tools = dict(row.tools) if isinstance(row.tools, dict) else {}
    skills = dict(row.skills) if isinstance(row.skills, dict) else {}
    permissions = dict(row.permissions) if isinstance(row.permissions, dict) else {}
    llm_config = dict(row.llm_config) if isinstance(row.llm_config, dict) else {}
    execution = dict(row.execution) if isinstance(row.execution, dict) else {}

    updates: dict[str, Any] = {}
    raw_updates: dict[str, Any] = {}
    for field, value in settings.items():
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
            _apply_llm_setting(llm_config, field, value, provider_ids)
            updates["llm_config"] = llm_config
        elif field in {
            "builtin_tools",
            "opt_in_builtin_tools",
            "disabled_categories",
            "disabled_tools",
            "intaris_mcp_servers",
            "mcp_servers",
        }:
            _apply_tools_setting(tools, field, value, row)
            updates["tools"] = tools
        elif field in {
            "tool_permissions",
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
        "step_agent_overrides",
        "executor_id",
        "executor_selector",
        "additional_executors",
        "provider_id",
        "model",
        "temperature",
        "max_tokens",
        "reasoning_effort",
        "voice",
        "builtin_tools",
        "opt_in_builtin_tools",
        "disabled_categories",
        "disabled_tools",
        "intaris_mcp_servers",
        "mcp_servers",
        "enabled_skills",
        "tool_permissions",
        "allowed_secrets",
        "allowed_credentials",
        "can_delegate",
        "max_delegation_depth",
        "tools",
        "skills",
        "permissions",
        "llm_config",
        "execution",
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
    llm_config: dict[str, Any], field: str, value: Any, provider_ids: set[str]
) -> None:
    if field == "provider_id":
        llm_config[field] = _nullable_enum(value, field, valid=provider_ids)
    elif field in {"model", "reasoning_effort", "voice"}:
        llm_config[field] = _nullable_string(value, field)
    elif field == "temperature":
        if value is not None and not isinstance(value, (int, float)):
            raise AgentManagementError("temperature must be a number")
        llm_config[field] = value
    elif field == "max_tokens":
        if value is not None and not isinstance(value, int):
            raise AgentManagementError("max_tokens must be an integer")
        llm_config[field] = value


def _apply_tools_setting(tools: dict[str, Any], field: str, value: Any, row: Any) -> None:
    if field == "builtin_tools":
        tools[field] = None if value is None else _validated_string_list(value, field)
    elif field == "opt_in_builtin_tools":
        selected = _validated_string_list(
            value, field, valid={"manage_agents", "builtin:manage_agents"}
        )
        if selected and getattr(row, "agent_type", "primary") == "secondary":
            raise AgentManagementError("secondary agents cannot opt into manage_agents")
        tools[field] = [
            "manage_agents" if item == "builtin:manage_agents" else item for item in selected
        ]
    elif field in {"disabled_categories", "disabled_tools", "intaris_mcp_servers"}:
        tools[field] = _validated_string_list(value, field)
    elif field == "mcp_servers":
        if not isinstance(value, list):
            raise AgentManagementError("mcp_servers must be a list")
        tools[field] = value


def _apply_permissions_setting(permissions: dict[str, Any], field: str, value: Any) -> None:
    if field == "tool_permissions":
        permissions[field] = _object(value, field)
    elif field in {"allowed_secrets", "allowed_credentials"}:
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
