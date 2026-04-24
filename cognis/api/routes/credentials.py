"""Credential CRUD routes."""

from __future__ import annotations

from fastapi import APIRouter, Request

from cognis.api.common import (
    api_exception,
    check_agent_access,
    forbid_mutation_for_viewer,
    require_current_user,
)
from cognis.api.models import CredentialResponse, CredentialUpsertRequest
from cognis.api.serializers import credential_to_response
from cognis.store.queries import get_agent

router = APIRouter(prefix="/api/v1/credentials", tags=["credentials"])


@router.get("", response_model=list[CredentialResponse])
async def list_credentials_route(request: Request) -> list[CredentialResponse]:
    user = require_current_user(request)
    rows = await request.app.state.providers.credentials.list_credentials(user.email)
    return [credential_to_response(row) for row in rows]


@router.get("/{credential_id}", response_model=CredentialResponse)
async def get_credential_route(request: Request, credential_id: str) -> CredentialResponse:
    user = require_current_user(request)
    row = await request.app.state.providers.credentials.get_credential(credential_id, user.email)
    if row is None:
        raise api_exception(404, "not_found", "Credential not found")
    return credential_to_response(row)


@router.post("", response_model=CredentialResponse)
async def upsert_credential_route(
    request: Request, payload: CredentialUpsertRequest
) -> CredentialResponse:
    forbid_mutation_for_viewer(request)
    user = require_current_user(request)
    if payload.agent_id is not None:
        async with request.app.state.session_factory() as session:
            agent = await get_agent(session, payload.agent_id)
        if agent is None:
            raise api_exception(404, "not_found", "Agent not found")
        await check_agent_access(request, agent, required="use")
    row = await request.app.state.providers.credentials.upsert_credential(
        credential_id=payload.credential_id,
        user_email=user.email,
        kind=payload.kind,
        label=payload.label,
        payload=payload.payload,
        scope=payload.scope,
        agent_id=payload.agent_id,
        description=payload.description,
        metadata=payload.metadata,
        expires_at=payload.expires_at,
    )
    return credential_to_response(row)


@router.post("/{credential_id}/revoke", response_model=dict)
async def revoke_credential_route(request: Request, credential_id: str) -> dict[str, bool]:
    forbid_mutation_for_viewer(request)
    user = require_current_user(request)
    ok = await request.app.state.providers.credentials.revoke_credential(credential_id, user.email)
    if not ok:
        raise api_exception(404, "not_found", "Credential not found")
    return {"ok": True}


@router.delete("/{credential_id}", response_model=dict)
async def delete_credential_route(request: Request, credential_id: str) -> dict[str, bool]:
    forbid_mutation_for_viewer(request)
    user = require_current_user(request)
    ok = await request.app.state.providers.credentials.delete_credential(credential_id, user.email)
    if not ok:
        raise api_exception(404, "not_found", "Credential not found")
    return {"ok": True}
