"""Agent routes."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

from fastapi import APIRouter, Query, Request
from pydantic import BaseModel, Field

from cognis.api.common import (
    api_exception,
    forbid_mutation_for_viewer,
    paginate_items,
    require_current_user,
    require_owner_or_admin,
)
from cognis.api.error_sanitizer import sanitize_client_error_detail
from cognis.api.models import (
    AgentCardResponse,
    AgentCreateRequest,
    AgentResponse,
    AgentUpdateRequest,
    CursorPage,
)
from cognis.api.serializers import agent_to_response
from cognis.core.agent_registry import SYSTEM_AGENTS, validate_agent_id
from cognis.logging import get_logger
from cognis.models.agent import AgentDefinition
from cognis.store.queries import (
    add_secondary_binding,
    create_agent,
    get_agent,
    list_agents,
    list_secondary_bindings,
    remove_secondary_binding,
    set_agent_status,
    set_secondary_bindings,
    update_agent,
)

logger = get_logger(__name__)

router = APIRouter(prefix="/api/v1/agents", tags=["agents"])


def _sync_metadata(synced: bool, error_detail: str | None = None) -> dict[str, object]:
    return {
        "personality_synced": synced,
        "personality_sync_error": error_detail,
        "personality_sync_checked_at": datetime.now(UTC).isoformat(),
    }


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
) -> CursorPage[AgentResponse]:
    user = require_current_user(request)

    # Start with system agents (if requested)
    items: list[AgentResponse] = []
    if include_system:
        for agent in SYSTEM_AGENTS.values():
            if not include_hidden and agent.hidden:
                continue
            if agent_type is not None and agent.agent_type != agent_type:
                continue
            items.append(_system_agent_to_response(agent))

    # Add DB agents
    async with request.app.state.session_factory() as session:
        rows = await list_agents(session, owner_email=user.email)
    for row in rows:
        resp = agent_to_response(row)
        if not include_hidden and getattr(row, "hidden", False):
            continue
        if agent_type is not None and getattr(row, "agent_type", "primary") != agent_type:
            continue
        items.append(resp)

    page_items, next_cursor, has_more = paginate_items(
        items,
        limit=limit,
        cursor=cursor,
        get_item_id=lambda item: item.agent_id,
    )
    return CursorPage(items=page_items, cursor=next_cursor, has_more=has_more)


def _system_agent_to_response(agent: AgentDefinition) -> AgentResponse:
    """Convert a system agent definition to an API response."""
    return AgentResponse(
        agent_id=agent.agent_id,
        owner_email=agent.owner_email,
        name=agent.name,
        display_name=agent.name,
        description=agent.description,
        system_prompt=agent.system_prompt,
        personality=agent.personality,
        skills=agent.skills,
        tools=agent.tools,
        permissions=None,
        llm_config=None,
        execution=agent.execution,
        avatar_url=agent.avatar_url,
        avatar_image_id=getattr(agent, "avatar_image_id", None),
        agent_type=agent.agent_type,
        is_system=agent.is_system,
        hidden=agent.hidden,
        status=agent.status,
        sync_metadata=None,
        created_at=None,
        updated_at=None,
    )


@router.post("", response_model=AgentResponse)
async def create_agent_route(request: Request, payload: AgentCreateRequest) -> AgentResponse:
    forbid_mutation_for_viewer(request)
    user = require_current_user(request)

    # Auto-generate agent_id from name if not provided
    agent_id = payload.agent_id
    if not agent_id:
        from cognis.api.common import slugify

        agent_id = slugify(payload.name)

    # Validate agent_id — system: prefix is reserved
    try:
        validate_agent_id(agent_id)
    except ValueError as exc:
        raise api_exception(400, "validation_error", str(exc)) from exc

    # Use display_name as alias for name (backward compat)
    name = payload.name or payload.display_name or agent_id

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
            execution=payload.execution,
            avatar_image_id=payload.avatar_image_id,
            agent_type=payload.agent_type,
            status=payload.status,
        )
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
            "Mnemory personality bootstrap failed for agent (retry via sync-personality)",
            extra={"extra_data": {"agent_id": agent_id}},
            exc_info=True,
        )
        await _persist_sync_metadata(request, agent_id, False, safe_detail)
        row.sync_metadata = _sync_metadata(False, safe_detail)
    return agent_to_response(row)


@router.get("/{agent_id}", response_model=AgentResponse)
async def agent_detail(request: Request, agent_id: str) -> AgentResponse:
    # Check system agents first
    if agent_id in SYSTEM_AGENTS:
        return _system_agent_to_response(SYSTEM_AGENTS[agent_id])

    async with request.app.state.session_factory() as session:
        row = await get_agent(session, agent_id)
    if row is None:
        raise api_exception(404, "not_found", "Agent not found")
    require_owner_or_admin(request, row.owner_email)
    return agent_to_response(row)


@router.put("/{agent_id}", response_model=AgentResponse)
async def update_agent_route(
    request: Request,
    agent_id: str,
    payload: AgentUpdateRequest,
) -> AgentResponse:
    forbid_mutation_for_viewer(request)
    if agent_id in SYSTEM_AGENTS:
        raise api_exception(403, "forbidden", "System agents are read-only")
    async with request.app.state.session_factory() as session:
        row = await get_agent(session, agent_id)
        if row is None:
            raise api_exception(404, "not_found", "Agent not found")
        require_owner_or_admin(request, row.owner_email)
        previous_definition = AgentDefinition.model_validate(agent_to_response(row).model_dump())
        updates = payload.model_dump(exclude_unset=True)
        profile_fields = {"name", "display_name", "avatar_image_id"}
        identity_fields = {"system_prompt", "personality"}
        profile_changed = bool(profile_fields & updates.keys())
        identity_changed = any(
            field in updates and getattr(row, field) != updates[field] for field in identity_fields
        )
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


@router.delete("/{agent_id}", response_model=dict)
async def archive_agent(request: Request, agent_id: str) -> dict[str, bool]:
    forbid_mutation_for_viewer(request)
    if agent_id in SYSTEM_AGENTS:
        raise api_exception(403, "forbidden", "System agents are read-only")
    async with request.app.state.session_factory() as session:
        row = await get_agent(session, agent_id)
        if row is None:
            raise api_exception(404, "not_found", "Agent not found")
        require_owner_or_admin(request, row.owner_email)
        ok = await set_agent_status(session, agent_id, "archived")
        await session.commit()
    return {"ok": ok}


@router.post("/{agent_id}/activate", response_model=AgentResponse)
async def activate_agent(request: Request, agent_id: str) -> AgentResponse:
    forbid_mutation_for_viewer(request)
    async with request.app.state.session_factory() as session:
        row = await get_agent(session, agent_id)
        if row is None:
            raise api_exception(404, "not_found", "Agent not found")
        require_owner_or_admin(request, row.owner_email)
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
        require_owner_or_admin(request, row.owner_email)
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
    require_owner_or_admin(request, row.owner_email)
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
        require_owner_or_admin(request, row.owner_email)
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
        temperature = 0.6
    else:
        user_msg = f"Generate the '{field}' field for this agent.\n\nAgent context:\n{ctx_summary}"
        system_msg = (
            f"You are helping configure an AI agent. {field_instruction} "
            f"Output ONLY the field value, nothing else. No quotes, no field name prefix, "
            f"no explanation."
        )
        temperature = 0.8

    try:
        response = await llm.generate(
            messages=[
                {"role": "system", "content": system_msg},
                {"role": "user", "content": user_msg},
            ],
            task_type="default",
            temperature=temperature,
            max_tokens=500,
        )
        choices = response.get("choices", [])
        if choices:
            value = choices[0].get("message", {}).get("content", "").strip()
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
    require_owner_or_admin(request, row.owner_email)
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
        require_owner_or_admin(request, row.owner_email)
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
        require_owner_or_admin(request, row.owner_email)
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
        require_owner_or_admin(request, row.owner_email)
        ok = await remove_secondary_binding(session, agent_id, secondary_agent_id)
        await session.commit()
    return {"ok": ok}


@router.get("/{agent_id}/card", response_model=AgentCardResponse)
async def agent_card(request: Request, agent_id: str) -> AgentCardResponse:
    async with request.app.state.session_factory() as session:
        row = await get_agent(session, agent_id)
    if row is None:
        raise api_exception(404, "not_found", "Agent not found")
    require_owner_or_admin(request, row.owner_email)
    raise api_exception(
        501,
        "not_implemented",
        "Agent cards require additional public discovery metadata and are deferred in MVP.",
    )
