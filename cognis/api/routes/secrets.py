"""Secret metadata routes."""

from __future__ import annotations

from fastapi import APIRouter, Request

from cognis.api.common import api_exception, forbid_mutation_for_viewer, require_current_user
from cognis.api.models import SecretResponse, SecretUpsertRequest
from cognis.api.serializers import secret_to_response

router = APIRouter(prefix="/api/v1/secrets", tags=["secrets"])


@router.get("", response_model=list[SecretResponse])
async def secret_list(request: Request) -> list[SecretResponse]:
    user = require_current_user(request)
    rows = await request.app.state.providers.secrets.list_secrets(user.email)
    return [secret_to_response(row) for row in rows]


@router.post("", response_model=dict)
async def secret_upsert(request: Request, payload: SecretUpsertRequest) -> dict[str, bool]:
    forbid_mutation_for_viewer(request)
    user = require_current_user(request)
    await request.app.state.providers.secrets.set_secret(
        payload.name,
        payload.value,
        user.email,
        scope=payload.scope,
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
    ok = await request.app.state.providers.secrets.delete_secret(
        name,
        user.email,
        scope=scope,
        agent_id=agent_id,
    )
    if not ok:
        raise api_exception(404, "not_found", "Secret not found")
    return {"ok": True}
