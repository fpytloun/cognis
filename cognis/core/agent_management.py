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
    list_secondary_bindings,
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
    return {"status": "ok", "agent": agent_to_response(row).model_dump(mode="json")}


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
        previous_content = previous_definition.compose_personality() or previous_definition.system_prompt
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
    await _sync_identity(deps, row, previous_content=definition.compose_personality() or definition.system_prompt)
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
                raise AgentManagementError(f"Secondary agent not found or not owned: {secondary_id}")
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
            grantee_overrides=None if executor_scope == "owner_executor" else grant.grantee_overrides,
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
    await deps.event_bus.publish(Event(type=EventType.AGENT_PROFILE_UPDATED, data={"agent_id": agent_id}))


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
