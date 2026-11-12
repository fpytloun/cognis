"""Recipient-only native channel surfaces, separate from execution sessions."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal

from fastapi import APIRouter, Request
from pydantic import BaseModel, Field

from cognis.api.common import api_exception, require_current_user
from cognis.channels.app_contact import AppContactService, AppContactView
from cognis.store import queries
from cognis.store.models import AppChannelDeliveryRow, ChannelDeliveryOutboxRow

router = APIRouter(prefix="/api/v1/app-contacts", tags=["app-contacts"])


class AppContactReplyRequest(BaseModel):
    """An idempotent participant message, not an instruction to another user's agent."""

    content: str = Field(min_length=1, max_length=32000)
    client_message_id: str = Field(min_length=1, max_length=128)


class AppContactReplyResponse(BaseModel):
    """Admission acknowledgement; execution remains sender-owned."""

    status: Literal["accepted"]


class AppContactEndRequest(BaseModel):
    """End this exchange, optionally refusing subsequent contact from its sender."""

    block_sender: bool = False


class AppContactEndResponse(BaseModel):
    """Recipient refusal acknowledgement."""

    status: Literal["ended"] = "ended"


@router.get("/messages/{delivery_id}/attachments/{artifact_id}")
async def download_contact_attachment(
    request: Request, delivery_id: str, artifact_id: str
) -> dict[str, str]:
    """Authorize only an attachment explicitly published to the authenticated recipient."""
    user = require_current_user(request)
    async with request.app.state.session_factory() as db:
        publication = await db.get(AppChannelDeliveryRow, delivery_id)
        outbox = await db.get(ChannelDeliveryOutboxRow, delivery_id)
        if (
            publication is None
            or publication.recipient_email != user.email
            or publication.published_at is None
            or outbox is None
        ):
            raise api_exception(404, "not_found", "Attachment not found")
        attachment = next(
            (
                raw
                for raw in (outbox.attachments_json or [])
                if isinstance(raw, dict) and raw.get("artifact_id") == artifact_id
            ),
            None,
        )
        artifact = await queries.get_artifact_record(db, artifact_id) if attachment else None
        if artifact is None or artifact.status == "deleted" or artifact.deleted_at is not None:
            raise api_exception(404, "not_found", "Attachment not found")
        expires_at = artifact.expires_at
        if expires_at is not None:
            expires_at = expires_at.replace(tzinfo=UTC) if expires_at.tzinfo is None else expires_at
            if expires_at <= datetime.now(UTC):
                raise api_exception(404, "not_found", "Attachment not found")
    url = await request.app.state.artifact_store.async_get_public_url(
        artifact.namespace, artifact.object_id, artifact.filename, ttl_seconds=60, mode="download"
    )
    return {"url": url}


@router.get("/{contact_id}", response_model=AppContactView)
async def get_app_contact(request: Request, contact_id: str) -> AppContactView:
    """Read delivered contact traffic without exposing its managed execution."""
    user = require_current_user(request)
    service: AppContactService = request.app.state.app_contact_service
    try:
        return await service.view(contact_id, user.email)
    except LookupError as exc:
        raise api_exception(404, "not_found", "Contact not found") from exc


@router.post("/{contact_id}/replies", response_model=AppContactReplyResponse)
async def reply_app_contact(
    request: Request, contact_id: str, body: AppContactReplyRequest
) -> AppContactReplyResponse:
    """Reply only as the authenticated recipient of this managed contact."""
    user = require_current_user(request)
    if user.role == "viewer":
        raise api_exception(403, "forbidden", "Read-only users cannot send messages")
    if not body.content.strip():
        raise api_exception(422, "validation_error", "Reply must not be blank")
    try:
        await request.app.state.app_contact_service.reply(
            contact_id, user.email, body.content, body.client_message_id
        )
    except LookupError as exc:
        raise api_exception(404, "not_found", "Contact not found") from exc
    except ValueError as exc:
        raise api_exception(409, "contact_unavailable", str(exc)) from exc
    return AppContactReplyResponse(status="accepted")


@router.post("/{contact_id}/end", response_model=AppContactEndResponse)
async def end_app_contact(
    request: Request, contact_id: str, body: AppContactEndRequest
) -> AppContactEndResponse:
    """End or block contact, without granting controller authority."""
    user = require_current_user(request)
    try:
        await request.app.state.app_contact_service.end(
            contact_id, user.email, block=body.block_sender
        )
    except LookupError as exc:
        raise api_exception(404, "not_found", "Contact not found") from exc
    except ValueError as exc:
        raise api_exception(409, "conflict", str(exc)) from exc
    return AppContactEndResponse()
