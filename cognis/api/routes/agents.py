"""Agent routes."""

from __future__ import annotations

import asyncio
import contextlib
import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, Query, Request
from pydantic import BaseModel, Field, ValidationError
from sqlalchemy import select

from cognis.api.common import (
    api_exception,
    apply_agent_access_metadata,
    check_agent_access,
    forbid_mutation_for_viewer,
    paginate_items,
    require_current_user,
    require_resource_owner,
    slugify,
)
from cognis.api.error_sanitizer import sanitize_client_error_detail
from cognis.api.models import (
    AgentCardResponse,
    AgentCreateRequest,
    AgentGrantCreateRequest,
    AgentGrantOverrideUpdateRequest,
    AgentGrantResponse,
    AgentGrantUpdateRequest,
    AgentResponse,
    AgentUpdateRequest,
    CursorPage,
)
from cognis.api.serializers import agent_to_response
from cognis.core.agent_profiles import resolve_agent_profile
from cognis.core.agent_registry import SYSTEM_AGENTS, validate_agent_id
from cognis.core.json_utils import extract_text_from_response
from cognis.logging import get_logger
from cognis.models.agent import AgentDefinition, AgentPermissions
from cognis.ownership import normalize_executor_scope
from cognis.store.models import Schedule, Task
from cognis.store.queries import (
    add_secondary_binding,
    create_agent,
    create_agent_grant,
    delete_system_agent_override,
    get_agent,
    get_agent_grant,
    get_agent_grant_for_user,
    get_executor_row,
    get_system_agent_override,
    get_user,
    list_agent_grants,
    list_secondary_bindings,
    remove_secondary_binding,
    revoke_agent_grant,
    set_agent_status,
    set_secondary_bindings,
    update_agent,
    update_agent_grant,
    upsert_system_agent_override,
)

logger = get_logger(__name__)

router = APIRouter(prefix="/api/v1/agents", tags=["agents"])

_SYSTEM_AGENT_LLM_FIELDS = {
    "provider_id",
    "model",
    "temperature",
    "top_p",
    "max_tokens",
    "reasoning_effort",
}


def _sync_metadata(synced: bool, error_detail: str | None = None) -> dict[str, object]:
    return {
        "personality_synced": synced,
        "personality_sync_error": error_detail,
        "personality_sync_checked_at": datetime.now(UTC).isoformat(),
    }


def _validate_agent_definition_payload(payload: dict[str, object]) -> AgentDefinition:
    try:
        definition = AgentDefinition.model_validate(payload)
        resolve_agent_profile(definition, None, source="agent_default")
    except (ValidationError, ValueError) as exc:
        raise api_exception(400, "validation_error", str(exc)) from exc
    return definition


def _grant_to_response(row: object, *, include_overrides: bool = False) -> AgentGrantResponse:
    return AgentGrantResponse(
        grant_id=row.grant_id,  # type: ignore[attr-defined]
        agent_id=row.agent_id,  # type: ignore[attr-defined]
        grantee_type=row.grantee_type,  # type: ignore[attr-defined]
        grantee_user_email=getattr(row, "grantee_user_email", None),
        grantee_group_id=getattr(row, "grantee_group_id", None),
        permission=row.permission,  # type: ignore[attr-defined]
        executor_scope=normalize_executor_scope(str(row.executor_scope)),  # type: ignore[attr-defined]
        granted_by=row.granted_by,  # type: ignore[attr-defined]
        granted_at=getattr(row, "granted_at", None),
        revoked_at=getattr(row, "revoked_at", None),
        note=getattr(row, "note", None),
        grantee_overrides=getattr(row, "grantee_overrides", None) if include_overrides else None,
    )


def _validate_agent_execution(execution: object) -> None:
    """Validate ``execution`` field on agent create/update payloads.

    Stage 36: validates ``additional_executors`` if present. Each entry must
    be a dict with exactly one of ``executor_id`` (non-empty string) or
    ``executor_selector`` (non-empty mapping). Optional ``description`` (str).
    """

    if execution is None:
        return
    if not isinstance(execution, dict):
        raise api_exception(400, "validation_error", "execution must be an object")

    primary_id = execution.get("executor_id")
    if primary_id is not None and not (isinstance(primary_id, str) and primary_id.strip()):
        raise api_exception(
            400, "validation_error", "execution.executor_id must be a non-empty string"
        )
    primary_selector = execution.get("executor_selector")
    if primary_selector is not None and (
        not isinstance(primary_selector, dict) or not primary_selector
    ):
        raise api_exception(
            400,
            "validation_error",
            "execution.executor_selector must be a non-empty object",
        )

    raw_additional = execution.get("additional_executors")
    if raw_additional is None:
        return
    if not isinstance(raw_additional, list):
        raise api_exception(
            400,
            "validation_error",
            "execution.additional_executors must be a list",
        )
    seen_ids: set[str] = set()
    if isinstance(primary_id, str) and primary_id.strip():
        seen_ids.add(primary_id.strip())
    for index, entry in enumerate(raw_additional):
        path = f"execution.additional_executors[{index}]"
        if not isinstance(entry, dict):
            raise api_exception(400, "validation_error", f"{path} must be an object")
        entry_id = entry.get("executor_id")
        entry_selector = entry.get("executor_selector")
        has_id = bool(isinstance(entry_id, str) and entry_id.strip())
        has_selector = bool(isinstance(entry_selector, dict) and bool(entry_selector))
        if has_id == has_selector:
            raise api_exception(
                400,
                "validation_error",
                f"{path} must specify exactly one of executor_id or executor_selector",
            )
        if has_id:
            normalized_id = entry_id.strip()
            if normalized_id in seen_ids:
                raise api_exception(
                    400,
                    "validation_error",
                    f"{path} duplicates executor_id '{normalized_id}' "
                    "(also a primary or earlier additional binding)",
                )
            seen_ids.add(normalized_id)
        if has_selector:
            for k, v in entry_selector.items():
                if not isinstance(k, str) or not k.strip():
                    raise api_exception(
                        400,
                        "validation_error",
                        f"{path}.executor_selector keys must be non-empty strings",
                    )
                if not isinstance(v, (str, int, bool)):
                    raise api_exception(
                        400,
                        "validation_error",
                        f"{path}.executor_selector values must be strings, ints, or bools",
                    )
        description = entry.get("description")
        if description is not None and not isinstance(description, str):
            raise api_exception(400, "validation_error", f"{path}.description must be a string")


def _normalized_grantee_execution(payload: dict[str, object] | None) -> dict[str, object]:
    if not isinstance(payload, dict):
        return {}
    execution: dict[str, object] = {}
    executor_id = payload.get("executor_id")
    if isinstance(executor_id, str) and executor_id.strip():
        execution["executor_id"] = executor_id.strip()
        return execution
    selector = payload.get("executor_selector")
    if isinstance(selector, dict):
        normalized = {
            str(key): str(value)
            for key, value in selector.items()
            if str(key).strip() and str(value).strip()
        }
        if normalized:
            execution["executor_selector"] = normalized
    return execution


async def _persist_sync_metadata(
    request: Request, agent_id: str, synced: bool, error_detail: str | None = None
) -> None:
    async with request.app.state.session_factory() as session:
        row = await get_agent(session, agent_id)
        if row is None:
            return
        row.sync_metadata = _sync_metadata(synced, error_detail)
        await session.commit()


@router.get("", response_model=CursorPage[AgentResponse])
async def agent_list(
    request: Request,
    cursor: str | None = None,
    limit: int = Query(default=20, ge=1, le=100),
    agent_type: str | None = Query(default=None),
    include_hidden: bool = Query(default=False),
    include_system: bool = Query(default=True),
    include_disabled: bool = Query(default=False),
) -> CursorPage[AgentResponse]:
    user = require_current_user(request)
    agents = await request.app.state.agent_registry.list_all(
        owner_email=user.email,
        agent_type=agent_type,
        include_hidden=include_hidden,
        include_system=include_system,
        include_disabled=include_disabled,
    )
    items = [agent_to_response(agent) for agent in agents]

    page_items, next_cursor, has_more = paginate_items(
        items,
        limit=limit,
        cursor=cursor,
        get_item_id=lambda item: item.agent_id,
    )
    return CursorPage(items=page_items, cursor=next_cursor, has_more=has_more)


@router.post("", response_model=AgentResponse)
async def create_agent_route(request: Request, payload: AgentCreateRequest) -> AgentResponse:
    forbid_mutation_for_viewer(request)
    user = require_current_user(request)

    # Auto-generate agent_id from name if not provided
    agent_id = payload.agent_id
    if not agent_id:
        agent_id = slugify(payload.name)

    # Validate agent_id — system: prefix is reserved
    try:
        validate_agent_id(agent_id)
    except ValueError as exc:
        raise api_exception(400, "validation_error", str(exc)) from exc

    # Stage 36: validate execution.additional_executors structure (if any)
    _validate_agent_execution(payload.execution)

    # Use display_name as alias for name (backward compat)
    name = payload.name or payload.display_name or agent_id
    definition_payload = {
        "agent_id": agent_id,
        "owner_email": user.email,
        "name": name,
        "display_name": name,
        "description": payload.description,
        "system_prompt": payload.system_prompt,
        "personality": payload.personality,
        "skills": payload.skills,
        "tools": payload.tools,
        "permissions": payload.permissions,
        "llm_config": payload.llm_config,
        "capabilities": payload.capabilities,
        "agent_profiles": payload.agent_profiles or {},
        "default_agent_profile_id": payload.default_agent_profile_id,
        "execution": payload.execution,
        "avatar_image_id": payload.avatar_image_id,
        "agent_type": payload.agent_type,
        "status": payload.status,
    }
    definition = _validate_agent_definition_payload(definition_payload)

    async with request.app.state.session_factory() as session:
        existing = await get_agent(session, agent_id)
        if existing is not None:
            raise api_exception(409, "conflict", "Agent already exists")
        row = await create_agent(
            session,
            agent_id=agent_id,
            owner_email=user.email,
            name=name,
            display_name=name,  # keep display_name = name for backward compat
            description=payload.description,
            system_prompt=payload.system_prompt,
            personality=payload.personality,
            skills=payload.skills,
            tools=payload.tools,
            permissions=payload.permissions,
            llm_config=payload.llm_config,
            capabilities=definition.capabilities.model_dump(mode="json"),
            agent_profiles=payload.agent_profiles,
            default_agent_profile_id=payload.default_agent_profile_id,
            execution=payload.execution,
            avatar_image_id=payload.avatar_image_id,
            agent_type=payload.agent_type,
            status=payload.status,
        )
        await session.commit()
        await session.refresh(row)

    try:
        replace_identity = getattr(
            request.app.state.providers.memory,
            "replace_bootstrap_identity",
            None,
        )
        if callable(replace_identity):
            await asyncio.wait_for(
                replace_identity(definition, previous_content=None, allow_legacy_cleanup=True),
                timeout=60.0,
            )
        else:
            await asyncio.wait_for(
                request.app.state.providers.memory.bootstrap_agent(definition),
                timeout=60.0,
            )
        await _persist_sync_metadata(request, agent_id, True)
        row.sync_metadata = _sync_metadata(True)
    except Exception as exc:
        safe_detail = sanitize_client_error_detail(exc, fallback="Mnemory bootstrap failed")
        logger.warning(
            "Mnemory personality bootstrap failed for agent (retry via sync-personality)",
            extra={"extra_data": {"agent_id": agent_id}},
            exc_info=True,
        )
        await _persist_sync_metadata(request, agent_id, False, safe_detail)
        row.sync_metadata = _sync_metadata(False, safe_detail)
    return agent_to_response(row)


@router.get("/{agent_id}", response_model=AgentResponse)
async def agent_detail(request: Request, agent_id: str) -> AgentResponse:
    user = require_current_user(request)
    if agent_id in SYSTEM_AGENTS:
        agent = await request.app.state.agent_registry.get(
            agent_id, owner_email=user.email, include_disabled=True
        )
        if agent is None:
            raise api_exception(404, "not_found", "Agent not found")
        return agent_to_response(agent)

    async with request.app.state.session_factory() as session:
        row = await get_agent(session, agent_id)
    if row is None:
        raise api_exception(404, "not_found", "Agent not found")
    access = await check_agent_access(request, row, required="view")
    apply_agent_access_metadata(row, access)
    return agent_to_response(row)


@router.put("/{agent_id}", response_model=AgentResponse)
async def update_agent_route(
    request: Request,
    agent_id: str,
    payload: AgentUpdateRequest,
) -> AgentResponse:
    forbid_mutation_for_viewer(request)
    if agent_id in SYSTEM_AGENTS:
        return await _update_system_agent_route(request, agent_id, payload)
    # Stage 36: validate execution.additional_executors structure (if any)
    if "execution" in payload.model_fields_set:
        _validate_agent_execution(payload.execution)
    async with request.app.state.session_factory() as session:
        row = await get_agent(session, agent_id)
        if row is None:
            raise api_exception(404, "not_found", "Agent not found")
        await check_agent_access(request, row, required="edit")
        previous_definition = AgentDefinition.model_validate(agent_to_response(row).model_dump())
        updates = payload.model_dump(exclude_unset=True)
        profile_fields = {"name", "display_name", "avatar_image_id"}
        identity_fields = {"system_prompt", "personality"}
        profile_changed = bool(profile_fields & updates.keys())
        identity_changed = any(
            field in updates and getattr(row, field) != updates[field] for field in identity_fields
        )
        if {
            "agent_profiles",
            "default_agent_profile_id",
            "llm_config",
            "capabilities",
        } & updates.keys():
            candidate = agent_to_response(row).model_dump()
            candidate.update(updates)
            candidate_definition = _validate_agent_definition_payload(candidate)
            if "capabilities" in updates:
                updates["capabilities"] = candidate_definition.capabilities.model_dump(mode="json")
        ok = await update_agent(
            session,
            agent_id,
            updates=updates,
        )
        if not ok:
            raise api_exception(400, "validation_error", "Agent update failed")
        await session.commit()
        await session.refresh(row)

    if identity_changed:
        definition = AgentDefinition.model_validate(agent_to_response(row).model_dump())
        previous_content = (
            previous_definition.compose_personality() or previous_definition.system_prompt
        )
        try:
            replace_identity = getattr(
                request.app.state.providers.memory, "replace_bootstrap_identity", None
            )
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
                await asyncio.wait_for(
                    request.app.state.providers.memory.bootstrap_agent(definition),
                    timeout=60.0,
                )
            await _persist_sync_metadata(request, agent_id, True)
            row.sync_metadata = _sync_metadata(True)
        except Exception as exc:
            safe_detail = sanitize_client_error_detail(exc, fallback="Mnemory bootstrap failed")
            logger.warning(
                "Mnemory personality bootstrap failed during agent update",
                extra={"extra_data": {"agent_id": agent_id, "error_detail": safe_detail}},
            )
            await _persist_sync_metadata(request, agent_id, False, safe_detail)
            row.sync_metadata = _sync_metadata(False, safe_detail)

    if profile_changed:
        from cognis.core.events import Event, EventType

        event_bus = getattr(request.app.state, "event_bus", None)
        if event_bus is not None:
            await event_bus.publish(
                Event(
                    type=EventType.AGENT_PROFILE_UPDATED,
                    data={"agent_id": agent_id},
                )
            )

    return agent_to_response(row)


async def _update_system_agent_route(
    request: Request,
    agent_id: str,
    payload: AgentUpdateRequest,
) -> AgentResponse:
    user = require_current_user(request)
    base = request.app.state.agent_registry.get_system_agent(agent_id)
    if base is None:
        raise api_exception(404, "not_found", "Agent not found")
    if not base.allow_user_override:
        raise api_exception(403, "forbidden", "This system agent cannot be overridden")

    updates = payload.model_dump(exclude_unset=True)
    allowed_top_level = {"llm_config", "skills", "tools", "permissions"}
    forbidden = sorted(key for key in updates if key not in allowed_top_level)
    if forbidden:
        raise api_exception(
            403,
            "forbidden",
            f"System agent overrides only allow runtime tuning fields: {', '.join(forbidden)}",
        )

    raw_llm = updates.get("llm_config")
    llm_override = raw_llm if isinstance(raw_llm, dict) else {}
    invalid_llm = sorted(key for key in llm_override if key not in _SYSTEM_AGENT_LLM_FIELDS)
    if invalid_llm:
        raise api_exception(
            403,
            "forbidden",
            f"Unsupported system agent override fields: {', '.join(invalid_llm)}",
        )

    raw_skills = updates.get("skills")
    if raw_skills is not None and not isinstance(raw_skills, dict):
        raise api_exception(403, "forbidden", "System agent skill overrides must be an object")
    raw_tools = updates.get("tools")
    if raw_tools is not None and not isinstance(raw_tools, dict):
        raise api_exception(403, "forbidden", "System agent tool overrides must be an object")
    raw_permissions = updates.get("permissions")
    if raw_permissions is not None and not isinstance(raw_permissions, dict):
        raise api_exception(403, "forbidden", "System agent permission overrides must be an object")
    if isinstance(raw_permissions, dict):
        try:
            AgentPermissions.model_validate(raw_permissions)
        except ValidationError as exc:
            raise api_exception(400, "validation_error", str(exc)) from exc

    async with request.app.state.session_factory() as session:
        existing = await get_system_agent_override(
            session, owner_email=user.email, agent_id=agent_id
        )
        llm_override_to_store = (
            (llm_override or None)
            if "llm_config" in updates
            else (existing.llm_config_override if existing else None)
        )
        skills_override_to_store = (
            (raw_skills if isinstance(raw_skills, dict) else None)
            if "skills" in updates
            else (existing.skills_override if existing else None)
        )
        tools_override_to_store = (
            (raw_tools if isinstance(raw_tools, dict) else None)
            if "tools" in updates
            else (existing.tools_override if existing else None)
        )
        permissions_override_to_store = (
            (raw_permissions if isinstance(raw_permissions, dict) else None)
            if "permissions" in updates
            else (existing.permissions_override if existing else None)
        )
        await upsert_system_agent_override(
            session,
            owner_email=user.email,
            agent_id=agent_id,
            llm_config_override=llm_override_to_store,
            skills_override=skills_override_to_store,
            tools_override=tools_override_to_store,
            permissions_override=permissions_override_to_store,
            execution_override=None,
        )
        await session.commit()

    agent = await request.app.state.agent_registry.get(
        agent_id, owner_email=user.email, include_disabled=True
    )
    assert agent is not None
    return agent_to_response(agent)


@router.delete("/{agent_id}", response_model=dict)
async def archive_agent(request: Request, agent_id: str) -> dict[str, bool]:
    forbid_mutation_for_viewer(request)
    if agent_id in SYSTEM_AGENTS:
        raise api_exception(403, "forbidden", "System agents are read-only")
    async with request.app.state.session_factory() as session:
        row = await get_agent(session, agent_id)
        if row is None:
            raise api_exception(404, "not_found", "Agent not found")
        await check_agent_access(request, row, required="delete")
        ok = await set_agent_status(session, agent_id, "archived")
        await session.commit()
    return {"ok": ok}


@router.post("/{agent_id}/duplicate", response_model=AgentResponse)
async def duplicate_agent_route(request: Request, agent_id: str) -> AgentResponse:
    forbid_mutation_for_viewer(request)
    user = require_current_user(request)

    if agent_id in SYSTEM_AGENTS:
        base = request.app.state.agent_registry.get_system_agent(agent_id)
        if base is None or base.hidden:
            raise api_exception(403, "forbidden", "This system agent cannot be duplicated")
        definition = await request.app.state.agent_registry.get(
            agent_id, owner_email=user.email, include_disabled=True
        )
        assert definition is not None
        new_agent_id = f"{slugify(definition.name)}-{uuid.uuid4().hex[:6]}"
        async with request.app.state.session_factory() as session:
            row = await create_agent(
                session,
                agent_id=new_agent_id,
                owner_email=user.email,
                name=f"{definition.name} Copy",
                display_name=f"{definition.name} Copy",
                description=definition.description,
                system_prompt=definition.system_prompt,
                personality=definition.personality,
                skills=definition.skills,
                tools=definition.tools,
                permissions=definition.permissions.model_dump(mode="json")
                if definition.permissions
                else None,
                llm_config=definition.llm_config.model_dump(mode="json", exclude_none=True)
                if definition.llm_config
                else None,
                capabilities=definition.capabilities.model_dump(mode="json"),
                agent_profiles={
                    profile_id: profile.model_dump(mode="json", exclude_none=True)
                    for profile_id, profile in definition.agent_profiles.items()
                },
                default_agent_profile_id=definition.default_agent_profile_id,
                execution=definition.execution,
                avatar_image_id=definition.avatar_image_id,
                agent_type=definition.agent_type,
                status="draft",
            )
            await session.commit()
            await session.refresh(row)
        return agent_to_response(row)

    async with request.app.state.session_factory() as session:
        row = await get_agent(session, agent_id)
        if row is None:
            raise api_exception(404, "not_found", "Agent not found")
        await check_agent_access(request, row, required="edit")
        new_agent_id = f"{slugify(row.display_name or row.name)}-{uuid.uuid4().hex[:6]}"
        new_row = await create_agent(
            session,
            agent_id=new_agent_id,
            owner_email=user.email,
            name=f"{row.display_name or row.name} Copy",
            display_name=f"{row.display_name or row.name} Copy",
            description=row.description,
            system_prompt=row.system_prompt,
            personality=row.personality,
            skills=row.skills,
            tools=row.tools,
            permissions=row.permissions,
            llm_config=row.llm_config,
            agent_profiles=row.agent_profiles,
            default_agent_profile_id=row.default_agent_profile_id,
            execution=row.execution,
            avatar_image_id=row.avatar_image_id,
            agent_type=row.agent_type,
            status="draft",
        )
        await session.commit()
        await session.refresh(new_row)
    return agent_to_response(new_row)


@router.post("/{agent_id}/reset-overrides", response_model=dict)
async def reset_system_agent_overrides(request: Request, agent_id: str) -> dict[str, bool]:
    forbid_mutation_for_viewer(request)
    user = require_current_user(request)
    base = request.app.state.agent_registry.get_system_agent(agent_id)
    if base is None:
        raise api_exception(404, "not_found", "Agent not found")
    async with request.app.state.session_factory() as session:
        ok = await delete_system_agent_override(session, owner_email=user.email, agent_id=agent_id)
        await session.commit()
    return {"ok": ok}


@router.post("/{agent_id}/disable", response_model=AgentResponse)
async def disable_system_agent(request: Request, agent_id: str) -> AgentResponse:
    forbid_mutation_for_viewer(request)
    user = require_current_user(request)
    base = request.app.state.agent_registry.get_system_agent(agent_id)
    if base is None:
        raise api_exception(404, "not_found", "Agent not found")
    if not base.allow_user_disable:
        raise api_exception(403, "forbidden", "This system agent cannot be disabled")
    async with request.app.state.session_factory() as session:
        existing = await get_system_agent_override(
            session, owner_email=user.email, agent_id=agent_id
        )
        await upsert_system_agent_override(
            session,
            owner_email=user.email,
            agent_id=agent_id,
            disabled=True,
            llm_config_override=(existing.llm_config_override if existing else None),
            skills_override=(existing.skills_override if existing else None),
            tools_override=(existing.tools_override if existing else None),
            permissions_override=(existing.permissions_override if existing else None),
            execution_override=(existing.execution_override if existing else None),
        )
        await session.commit()
    agent = await request.app.state.agent_registry.get(
        agent_id, owner_email=user.email, include_disabled=True
    )
    assert agent is not None
    return agent_to_response(agent)


@router.post("/{agent_id}/enable", response_model=AgentResponse)
async def enable_system_agent(request: Request, agent_id: str) -> AgentResponse:
    forbid_mutation_for_viewer(request)
    user = require_current_user(request)
    base = request.app.state.agent_registry.get_system_agent(agent_id)
    if base is None:
        raise api_exception(404, "not_found", "Agent not found")
    async with request.app.state.session_factory() as session:
        existing = await get_system_agent_override(
            session, owner_email=user.email, agent_id=agent_id
        )
        await upsert_system_agent_override(
            session,
            owner_email=user.email,
            agent_id=agent_id,
            disabled=False,
            llm_config_override=(existing.llm_config_override if existing else None),
            skills_override=(existing.skills_override if existing else None),
            tools_override=(existing.tools_override if existing else None),
            permissions_override=(existing.permissions_override if existing else None),
            execution_override=(existing.execution_override if existing else None),
        )
        await session.commit()
    agent = await request.app.state.agent_registry.get(
        agent_id, owner_email=user.email, include_disabled=True
    )
    assert agent is not None
    return agent_to_response(agent)


@router.post("/{agent_id}/activate", response_model=AgentResponse)
async def activate_agent(request: Request, agent_id: str) -> AgentResponse:
    forbid_mutation_for_viewer(request)
    async with request.app.state.session_factory() as session:
        row = await get_agent(session, agent_id)
        if row is None:
            raise api_exception(404, "not_found", "Agent not found")
        await check_agent_access(request, row, required="edit")
        await set_agent_status(session, agent_id, "active")
        await session.commit()
        await session.refresh(row)
    definition = AgentDefinition.model_validate(agent_to_response(row).model_dump())
    try:
        await asyncio.wait_for(
            request.app.state.providers.memory.bootstrap_agent(definition),
            timeout=60.0,
        )
        await _persist_sync_metadata(request, agent_id, True)
        row.sync_metadata = _sync_metadata(True)
    except Exception as exc:
        safe_detail = sanitize_client_error_detail(exc, fallback="Mnemory bootstrap failed")
        logger.warning(
            "Mnemory personality bootstrap failed on activation (retry via sync-personality)",
            extra={"extra_data": {"agent_id": agent_id}},
            exc_info=True,
        )
        await _persist_sync_metadata(request, agent_id, False, safe_detail)
        row.sync_metadata = _sync_metadata(False, safe_detail)
    return agent_to_response(row)


@router.post("/{agent_id}/suspend", response_model=AgentResponse)
async def suspend_agent(request: Request, agent_id: str) -> AgentResponse:
    forbid_mutation_for_viewer(request)
    async with request.app.state.session_factory() as session:
        row = await get_agent(session, agent_id)
        if row is None:
            raise api_exception(404, "not_found", "Agent not found")
        await check_agent_access(request, row, required="edit")
        await set_agent_status(session, agent_id, "suspended")
        await session.commit()
        await session.refresh(row)
    return agent_to_response(row)


@router.post("/{agent_id}/sync-personality", response_model=dict)
async def sync_personality(request: Request, agent_id: str) -> dict[str, bool]:
    forbid_mutation_for_viewer(request)
    async with request.app.state.session_factory() as session:
        row = await get_agent(session, agent_id)
    if row is None:
        raise api_exception(404, "not_found", "Agent not found")
    await check_agent_access(request, row, required="edit")
    definition = AgentDefinition.model_validate(agent_to_response(row).model_dump())
    current_content = definition.compose_personality() or definition.system_prompt
    try:
        replace_identity = getattr(
            request.app.state.providers.memory, "replace_bootstrap_identity", None
        )
        if callable(replace_identity):
            await replace_identity(
                definition,
                previous_content=current_content,
                allow_legacy_cleanup=True,
            )
        else:
            await request.app.state.providers.memory.bootstrap_agent(definition)
    except Exception as exc:
        safe_detail = sanitize_client_error_detail(exc, fallback="Mnemory bootstrap failed")
        await _persist_sync_metadata(request, agent_id, False, safe_detail)
        raise api_exception(
            502, "provider_error", "Personality sync failed", details={"error_detail": safe_detail}
        ) from exc
    await _persist_sync_metadata(request, agent_id, True)
    return {"ok": True}


@router.delete("/{agent_id}/avatar", response_model=dict)
async def delete_agent_avatar(request: Request, agent_id: str) -> dict[str, bool]:
    """Remove an agent's avatar image."""
    forbid_mutation_for_viewer(request)
    if agent_id in SYSTEM_AGENTS:
        raise api_exception(403, "forbidden", "System agents are read-only")
    async with request.app.state.session_factory() as session:
        row = await get_agent(session, agent_id)
        if row is None:
            raise api_exception(404, "not_found", "Agent not found")
        await check_agent_access(request, row, required="edit")
        old_image_id = row.avatar_image_id
        ok = await update_agent(session, agent_id, updates={"avatar_image_id": None})
        await session.commit()
    # Clean up old artifact
    if old_image_id and hasattr(request.app.state, "artifact_store"):
        try:
            await request.app.state.artifact_store.async_delete_object("avatars", old_image_id)
        except Exception:
            logger.warning("Failed to delete avatar artifact", exc_info=True)
    return {"ok": ok}


_GENERATABLE_FIELDS = {"description", "tone", "temperament", "purpose", "behavioral_rules"}

_FIELD_INSTRUCTIONS: dict[str, str] = {
    "description": (
        "Write a concise 1-3 sentence description of this AI agent. "
        "Describe what it does and what makes it unique."
    ),
    "tone": (
        "Suggest a communication tone — how the agent speaks and writes. "
        "Output a short comma-separated list of 2-4 adjectives describing voice, "
        "formality, and style (e.g. 'formal, precise', 'casual, witty, warm'). "
        "Do NOT include behavioral traits like patience or caution — those belong in temperament."
    ),
    "temperament": (
        "Suggest a temperament — how the agent behaves and reacts. "
        "Output a short comma-separated list of 1-3 adjectives describing disposition, "
        "decision-making style, and emotional tendencies (e.g. 'patient, methodical', "
        "'bold, decisive'). Do NOT include communication style traits like formality "
        "or wit — those belong in tone."
    ),
    "purpose": (
        "Write a concise purpose statement for this agent in 3-8 words "
        "(e.g. 'research specialist', 'code review and refactoring assistant'). "
        "Describe the agent's primary role."
    ),
    "behavioral_rules": (
        "Write 3-7 clear behavioral rules for this agent, one per line. "
        "Each rule should be a concrete, actionable instruction "
        "(e.g. 'Always cite sources when making claims'). "
        "Match the agent's purpose and personality."
    ),
}


class GenerateFieldRequest(BaseModel):
    """Request body for agent field generation."""

    field: str
    current_value: str = Field(default="", max_length=2000)
    context: dict[str, str] = Field(default_factory=dict)


@router.post("/generate-field")
async def generate_agent_field(request: Request, payload: GenerateFieldRequest) -> dict[str, str]:
    """Generate or expand an agent field value using the LLM.

    Accepts the field name, its current value (if any), and the full
    context of all other agent fields. If current_value is non-empty,
    the LLM expands/refines it rather than generating from scratch.
    """
    forbid_mutation_for_viewer(request)
    require_current_user(request)
    llm = request.app.state.providers.llm

    field = payload.field
    current_value = payload.current_value.strip()
    context = {k: v[:2000] for k, v in payload.context.items()}

    if field not in _GENERATABLE_FIELDS:
        raise api_exception(400, "validation_error", f"Field '{field}' is not generatable")

    # Build context summary from all agent fields
    ctx_parts: list[str] = []
    if context.get("name"):
        ctx_parts.append(f"Agent name: {context['name']}")
    if context.get("description") and field != "description":
        ctx_parts.append(f"Description: {context['description']}")
    if context.get("tone") and field != "tone":
        ctx_parts.append(f"Tone: {context['tone']}")
    if context.get("temperament") and field != "temperament":
        ctx_parts.append(f"Temperament: {context['temperament']}")
    if context.get("purpose") and field != "purpose":
        ctx_parts.append(f"Purpose: {context['purpose']}")
    if context.get("behavioral_rules") and field != "behavioral_rules":
        ctx_parts.append(f"Behavioral rules:\n{context['behavioral_rules']}")
    if context.get("system_prompt"):
        ctx_parts.append(f"System prompt:\n{context['system_prompt']}")
    ctx_summary = "\n".join(ctx_parts) if ctx_parts else "No other fields set yet."

    field_instruction = _FIELD_INSTRUCTIONS[field]

    if current_value:
        user_msg = (
            f"The agent's '{field}' field currently contains:\n\n"
            f"{current_value}\n\n"
            f"The user wants this expanded into a complete, well-written version. "
            f"Use their text as the starting point and core idea, then elaborate it "
            f"into a proper, professional value for this field. "
            f"Keep their core meaning and any specific terms they used, "
            f"but feel free to rephrase and expand significantly.\n\n"
            f"Agent context:\n{ctx_summary}"
        )
        system_msg = (
            f"You are helping configure an AI agent. The user provided brief input "
            f"for the '{field}' field. Expand it into a complete, polished version. "
            f"Preserve the user's core idea and any specific terminology, but produce "
            f"a substantially more complete result. "
            f"{field_instruction} "
            f"Output ONLY the expanded field value, nothing else."
        )
    else:
        user_msg = f"Generate the '{field}' field for this agent.\n\nAgent context:\n{ctx_summary}"
        system_msg = (
            f"You are helping configure an AI agent. {field_instruction} "
            f"Output ONLY the field value, nothing else. No quotes, no field name prefix, "
            f"no explanation."
        )

    try:
        messages = [
            {"role": "system", "content": system_msg},
            {"role": "user", "content": user_msg},
        ]
        response = await llm.generate(messages=messages, task_type="default")
        value = extract_text_from_response(response).strip()
        if value:
            return {"value": value}
    except Exception:
        logger.warning("Agent field generation failed", exc_info=True)

    raise api_exception(502, "provider_error", "Failed to generate field value")


@router.get("/{agent_id}/bindings", response_model=list[str])
async def list_agent_bindings(request: Request, agent_id: str) -> list[str]:
    """List secondary agent IDs bound to a primary agent."""
    if agent_id in SYSTEM_AGENTS:
        return []  # System agents don't have bindings
    async with request.app.state.session_factory() as session:
        row = await get_agent(session, agent_id)
    if row is None:
        raise api_exception(404, "not_found", "Agent not found")
    await check_agent_access(request, row, required="view")
    async with request.app.state.session_factory() as session:
        return await list_secondary_bindings(session, agent_id)


@router.put("/{agent_id}/bindings", response_model=dict)
async def replace_agent_bindings(
    request: Request, agent_id: str, payload: list[str]
) -> dict[str, bool]:
    """Replace all secondary agent bindings for a primary agent."""
    forbid_mutation_for_viewer(request)
    if agent_id in SYSTEM_AGENTS:
        raise api_exception(403, "forbidden", "System agents are read-only")
    async with request.app.state.session_factory() as session:
        row = await get_agent(session, agent_id)
        if row is None:
            raise api_exception(404, "not_found", "Agent not found")
        await check_agent_access(request, row, required="edit")
        await set_secondary_bindings(session, agent_id, payload)
        await session.commit()
    return {"ok": True}


@router.post("/{agent_id}/bindings/{secondary_agent_id}", response_model=dict)
async def add_agent_binding(
    request: Request, agent_id: str, secondary_agent_id: str
) -> dict[str, bool]:
    """Add a single secondary agent binding."""
    forbid_mutation_for_viewer(request)
    if agent_id in SYSTEM_AGENTS:
        raise api_exception(403, "forbidden", "System agents are read-only")
    async with request.app.state.session_factory() as session:
        row = await get_agent(session, agent_id)
        if row is None:
            raise api_exception(404, "not_found", "Agent not found")
        await check_agent_access(request, row, required="edit")
        ok = await add_secondary_binding(session, agent_id, secondary_agent_id)
        await session.commit()
    return {"ok": ok}


@router.delete("/{agent_id}/bindings/{secondary_agent_id}", response_model=dict)
async def remove_agent_binding(
    request: Request, agent_id: str, secondary_agent_id: str
) -> dict[str, bool]:
    """Remove a single secondary agent binding."""
    forbid_mutation_for_viewer(request)
    if agent_id in SYSTEM_AGENTS:
        raise api_exception(403, "forbidden", "System agents are read-only")
    async with request.app.state.session_factory() as session:
        row = await get_agent(session, agent_id)
        if row is None:
            raise api_exception(404, "not_found", "Agent not found")
        await check_agent_access(request, row, required="edit")
        ok = await remove_secondary_binding(session, agent_id, secondary_agent_id)
        await session.commit()
    return {"ok": ok}


@router.get("/{agent_id}/card", response_model=AgentCardResponse)
async def agent_card(request: Request, agent_id: str) -> AgentCardResponse:
    async with request.app.state.session_factory() as session:
        row = await get_agent(session, agent_id)
    if row is None:
        raise api_exception(404, "not_found", "Agent not found")
    await check_agent_access(request, row, required="view")
    raise api_exception(
        501,
        "not_implemented",
        "Agent cards require additional public discovery metadata and are deferred in MVP.",
    )


@router.get("/{agent_id}/shares", response_model=list[AgentGrantResponse])
async def list_agent_shares(request: Request, agent_id: str) -> list[AgentGrantResponse]:
    async with request.app.state.session_factory() as session:
        row = await get_agent(session, agent_id)
        if row is None:
            raise api_exception(404, "not_found", "Agent not found")
        require_resource_owner(request, row.owner_email)
        grants = await list_agent_grants(session, agent_id)
    return [_grant_to_response(grant) for grant in grants]


@router.get("/{agent_id}/my-share", response_model=AgentGrantResponse)
async def get_my_agent_share(request: Request, agent_id: str) -> AgentGrantResponse:
    user = require_current_user(request)
    async with request.app.state.session_factory() as session:
        row = await get_agent(session, agent_id)
        if row is None:
            raise api_exception(404, "not_found", "Agent not found")
        grant = await get_agent_grant_for_user(session, agent_id, user.email)
        if grant is None or grant.revoked_at is not None:
            raise api_exception(404, "not_found", "Grant not found")
    return _grant_to_response(grant, include_overrides=True)


@router.patch("/{agent_id}/my-share", response_model=AgentGrantResponse)
async def update_my_agent_share(
    request: Request,
    agent_id: str,
    payload: AgentGrantOverrideUpdateRequest,
) -> AgentGrantResponse:
    forbid_mutation_for_viewer(request)
    user = require_current_user(request)
    execution = _normalized_grantee_execution(payload.execution)
    async with request.app.state.session_factory() as session:
        row = await get_agent(session, agent_id)
        if row is None:
            raise api_exception(404, "not_found", "Agent not found")
        grant = await get_agent_grant_for_user(session, agent_id, user.email)
        if grant is None or grant.revoked_at is not None:
            raise api_exception(404, "not_found", "Grant not found")
        if normalize_executor_scope(str(grant.executor_scope)) != "grantee_executor":
            raise api_exception(
                400,
                "validation_error",
                "Executor overrides are only available for grantee-executor shares",
            )
        if execution.get("executor_id"):
            executor = await get_executor_row(
                session,
                str(execution["executor_id"]),
                owner_email=user.email,
                include_shared=True,
            )
            if executor is None:
                raise api_exception(400, "validation_error", "Executor is not available")
        overrides = {"execution": execution} if execution else {}
        updated = await update_agent_grant(
            session,
            grant.grant_id,
            grantee_overrides=overrides,
        )
        assert updated is not None
        await session.commit()
    return _grant_to_response(updated, include_overrides=True)


@router.post("/{agent_id}/shares", response_model=AgentGrantResponse, status_code=201)
async def create_agent_share(
    request: Request,
    agent_id: str,
    payload: AgentGrantCreateRequest,
) -> AgentGrantResponse:
    forbid_mutation_for_viewer(request)
    user = require_current_user(request)
    async with request.app.state.session_factory() as session:
        agent = await get_agent(session, agent_id)
        if agent is None:
            raise api_exception(404, "not_found", "Agent not found")
        require_resource_owner(request, agent.owner_email)
        if payload.grantee_email == agent.owner_email:
            raise api_exception(
                400, "validation_error", "Agent owner cannot be granted their own agent"
            )
        grantee = await get_user(session, str(payload.grantee_email))
        if grantee is None:
            raise api_exception(404, "not_found", "Grantee user not found")
        existing = await get_agent_grant_for_user(session, agent_id, str(payload.grantee_email))
        if existing is not None:
            row = await update_agent_grant(
                session,
                existing.grant_id,
                executor_scope=payload.executor_scope,
                note=payload.note,
                grantee_overrides=None
                if payload.executor_scope == "owner_executor"
                else existing.grantee_overrides,
                granted_at=datetime.now(UTC),
                granted_by=user.email,
                revoked_at=None,
            )
            assert row is not None
        else:
            row = await create_agent_grant(
                session,
                agent_id=agent_id,
                grantee_user_email=str(payload.grantee_email),
                executor_scope=payload.executor_scope,
                granted_by=user.email,
                note=payload.note,
            )
        await session.commit()
    return _grant_to_response(row)


@router.patch("/{agent_id}/shares/{grant_id}", response_model=AgentGrantResponse)
async def update_agent_share(
    request: Request,
    agent_id: str,
    grant_id: str,
    payload: AgentGrantUpdateRequest,
) -> AgentGrantResponse:
    forbid_mutation_for_viewer(request)
    async with request.app.state.session_factory() as session:
        agent = await get_agent(session, agent_id)
        if agent is None:
            raise api_exception(404, "not_found", "Agent not found")
        require_resource_owner(request, agent.owner_email)
        row = await get_agent_grant(session, grant_id)
        if row is None or row.agent_id != agent_id or row.revoked_at is not None:
            raise api_exception(404, "not_found", "Grant not found")
        updated = await update_agent_grant(
            session,
            grant_id,
            executor_scope=payload.executor_scope,
            note=payload.note,
            grantee_overrides=(
                None if payload.executor_scope == "owner_executor" else row.grantee_overrides
            ),
        )
        assert updated is not None
        await session.commit()
    return _grant_to_response(updated)


@router.delete("/{agent_id}/shares/{grant_id}", response_model=dict)
async def revoke_agent_share(request: Request, agent_id: str, grant_id: str) -> dict[str, bool]:
    forbid_mutation_for_viewer(request)
    async with request.app.state.session_factory() as session:
        agent = await get_agent(session, agent_id)
        if agent is None:
            raise api_exception(404, "not_found", "Agent not found")
        require_resource_owner(request, agent.owner_email)
        row = await get_agent_grant(session, grant_id)
        if row is None or row.agent_id != agent_id or row.revoked_at is not None:
            raise api_exception(404, "not_found", "Grant not found")
        revoked = await revoke_agent_grant(session, grant_id)
        assert revoked is not None

        grantee_email = revoked.grantee_user_email
        running_task_ids: list[str] = []
        if grantee_email:
            schedules_result = await session.execute(
                select(Schedule).where(
                    Schedule.agent_id == agent_id,
                    Schedule.created_by == grantee_email,
                    Schedule.enabled.is_(True),
                )
            )
            for schedule in schedules_result.scalars().all():
                schedule.enabled = False
                schedule.disabled_reason = "access_revoked"

            task_result = await session.execute(
                select(Task).where(
                    Task.agent_id == agent_id,
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
    task_queue = getattr(request.app.state, "task_queue", None)
    if task_queue is not None:
        for task_id in running_task_ids:
            with contextlib.suppress(Exception):
                await task_queue.pause_task(task_id)
    return {"ok": True}
