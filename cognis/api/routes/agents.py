"""Agent routes."""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, Query, Request

from cognis.api.common import (
    api_exception,
    forbid_mutation_for_viewer,
    paginate_items,
    require_current_user,
    require_owner_or_admin,
)
from cognis.api.models import (
    AgentCardResponse,
    AgentCreateRequest,
    AgentResponse,
    AgentUpdateRequest,
    CursorPage,
)
from cognis.api.serializers import agent_to_response
from cognis.logging import get_logger
from cognis.models.agent import AgentDefinition
from cognis.store.queries import (
    create_agent,
    get_agent,
    list_agents,
    set_agent_status,
    update_agent,
)

logger = get_logger(__name__)

router = APIRouter(prefix="/api/v1/agents", tags=["agents"])


@router.get("", response_model=CursorPage[AgentResponse])
async def agent_list(
    request: Request,
    cursor: str | None = None,
    limit: int = Query(default=20, ge=1, le=100),
) -> CursorPage[AgentResponse]:
    user = require_current_user(request)
    async with request.app.state.session_factory() as session:
        rows = await list_agents(session, owner_email=user.email)
    items = [agent_to_response(row) for row in rows]
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
    async with request.app.state.session_factory() as session:
        existing = await get_agent(session, payload.agent_id)
        if existing is not None:
            raise api_exception(409, "conflict", "Agent already exists")
        row = await create_agent(
            session,
            agent_id=payload.agent_id,
            owner_email=user.email,
            name=payload.name,
            display_name=payload.display_name,
            description=payload.description,
            system_prompt=payload.system_prompt,
            personality=payload.personality,
            skills=payload.skills,
            tools=payload.tools,
            permissions=payload.permissions,
            llm_config=payload.llm_config,
            execution=payload.execution,
            avatar_url=payload.avatar_url,
            status=payload.status,
        )
        await session.commit()
        await session.refresh(row)

    definition = AgentDefinition.model_validate(agent_to_response(row).model_dump())
    try:
        await asyncio.wait_for(
            request.app.state.providers.memory.bootstrap_agent(definition),
            timeout=5.0,
        )
    except Exception:
        logger.warning(
            "Mnemory personality bootstrap failed for agent (retry via sync-personality)",
            extra={"extra_data": {"agent_id": payload.agent_id}},
            exc_info=True,
        )
    return agent_to_response(row)


@router.get("/{agent_id}", response_model=AgentResponse)
async def agent_detail(request: Request, agent_id: str) -> AgentResponse:
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
    async with request.app.state.session_factory() as session:
        row = await get_agent(session, agent_id)
        if row is None:
            raise api_exception(404, "not_found", "Agent not found")
        require_owner_or_admin(request, row.owner_email)
        ok = await update_agent(
            session,
            agent_id,
            updates=payload.model_dump(exclude_none=True),
        )
        if not ok:
            raise api_exception(400, "validation_error", "Agent update failed")
        await session.commit()
        await session.refresh(row)
    return agent_to_response(row)


@router.delete("/{agent_id}", response_model=dict)
async def archive_agent(request: Request, agent_id: str) -> dict[str, bool]:
    forbid_mutation_for_viewer(request)
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
            timeout=5.0,
        )
    except Exception:
        logger.warning(
            "Mnemory personality bootstrap failed on activation (retry via sync-personality)",
            extra={"extra_data": {"agent_id": agent_id}},
            exc_info=True,
        )
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
    await request.app.state.providers.memory.bootstrap_agent(definition)
    return {"ok": True}


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
