"""Secret metadata routes."""

from __future__ import annotations

from fastapi import APIRouter, Request

from cognis.api.common import (
    api_exception,
    check_agent_access,
    forbid_mutation_for_viewer,
    require_admin,
    require_current_user,
)
from cognis.api.models import SecretResponse, SecretUpsertRequest
from cognis.api.serializers import secret_to_response
from cognis.ownership import SYSTEM_USER_EMAIL
from cognis.store.queries import get_agent

router = APIRouter(prefix="/api/v1/secrets", tags=["secrets"])


@router.get("", response_model=list[SecretResponse])
async def secret_list(request: Request) -> list[SecretResponse]:
    user = require_current_user(request)
    rows = await request.app.state.providers.secrets.list_secrets(user.email)
    if user.role == "admin":
        rows.extend(await request.app.state.providers.secrets.list_secrets(SYSTEM_USER_EMAIL))
    return [secret_to_response(row) for row in rows]


@router.post("", response_model=dict)
async def secret_upsert(request: Request, payload: SecretUpsertRequest) -> dict[str, bool]:
    forbid_mutation_for_viewer(request)
    user = require_current_user(request)
    scope = "system" if payload.scope == "global" else payload.scope
    if scope == "system":
        require_admin(request)
    if payload.agent_id is not None:
        async with request.app.state.session_factory() as session:
            agent = await get_agent(session, payload.agent_id)
        if agent is None:
            raise api_exception(404, "not_found", "Agent not found")
        await check_agent_access(request, agent, required="use")
    await request.app.state.providers.secrets.set_secret(
        payload.name,
        payload.value,
        SYSTEM_USER_EMAIL if scope == "system" else user.email,
        scope=scope,
        agent_id=payload.agent_id,
        description=payload.description,
    )
    return {"ok": True}


@router.delete("/{name}", response_model=dict)
async def secret_delete(
    request: Request,
    name: str,
    scope: str = "user",
    agent_id: str | None = None,
) -> dict[str, bool]:
    forbid_mutation_for_viewer(request)
    user = require_current_user(request)
    normalized_scope = "system" if scope == "global" else scope
    if normalized_scope == "system":
        require_admin(request)
    ok = await request.app.state.providers.secrets.delete_secret(
        name,
        SYSTEM_USER_EMAIL if normalized_scope == "system" else user.email,
        scope=normalized_scope,
        agent_id=agent_id,
    )
    if not ok:
        raise api_exception(404, "not_found", "Secret not found")
    return {"ok": True}
