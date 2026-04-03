"""Channel management API routes.

Provides CRUD for channel accounts, status monitoring, start/stop
controls, and webhook endpoints for inbound messages.
"""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, Request, Response
from fastapi.responses import JSONResponse

from cognis.api.common import error_response
from cognis.channels.registry import get_channel_meta, list_channel_types
from cognis.logging import get_logger

logger = get_logger(__name__)

router = APIRouter(prefix="/api/v1/channels", tags=["channels"])


# ---------------------------------------------------------------------------
# Channel type metadata
# ---------------------------------------------------------------------------


@router.get("/types")
async def list_types(request: Request) -> list[dict[str, Any]]:
    """List all supported channel types with metadata."""
    types = list_channel_types()
    return [t.model_dump() for t in types]


@router.get("/types/{channel_type}", response_model=None)
async def get_type(request: Request, channel_type: str) -> Any:
    """Get metadata for a specific channel type."""
    meta = get_channel_meta(channel_type)
    if meta is None:
        return error_response(404, "not_found", f"Unknown channel type: {channel_type}")
    return meta.model_dump()


# ---------------------------------------------------------------------------
# Channel account CRUD
# ---------------------------------------------------------------------------


@router.get("/accounts")
async def list_accounts(request: Request) -> list[dict[str, Any]]:
    """List all configured channel accounts."""
    session_factory = request.app.state.session_factory
    user_email = request.state.user_email

    from cognis.store.queries import list_channel_accounts

    async with session_factory() as session:
        rows = await list_channel_accounts(session, user_email=user_email)

    result = []
    for row in rows:
        account = {
            "account_id": row.account_id,
            "channel_type": row.channel_type,
            "display_name": row.display_name,
            "enabled": row.enabled,
            "agent_id": row.agent_id,
            "config": row.config or {},
            "credential_refs": row.credential_refs or {},
            "default_conversation_id": row.default_conversation_id,
            "allow_new_conversations": row.allow_new_conversations,
            "allowed_senders": row.allowed_senders or [],
            "dm_policy": row.dm_policy,
            "group_policy": row.group_policy,
            "created_at": row.created_at.isoformat() if row.created_at else None,
            "updated_at": row.updated_at.isoformat() if row.updated_at else None,
        }

        # Add runtime status if channel manager is available
        channel_manager = getattr(request.app.state, "channel_manager", None)
        if channel_manager:
            status = await channel_manager.get_account_status(row.account_id)
            if status:
                account["status"] = status.model_dump()
            else:
                account["status"] = {"status": "stopped"}
        else:
            account["status"] = {"status": "stopped"}

        result.append(account)

    return result


@router.post("/accounts", response_model=None)
async def create_account(request: Request) -> Any:
    """Create a new channel account."""
    body = await request.json()
    session_factory = request.app.state.session_factory
    user_email = request.state.user_email

    channel_type = body.get("channel_type")
    if not channel_type:
        return error_response(400, "validation_error", "channel_type is required")

    meta = get_channel_meta(channel_type)
    if meta is None:
        return error_response(400, "validation_error", f"Unknown channel type: {channel_type}")

    agent_id = body.get("agent_id")
    if not agent_id:
        return error_response(400, "validation_error", "agent_id is required")

    display_name = body.get("display_name", f"{meta.label} Account")

    # Generate webhook secret for webhook-based channels
    webhook_secret = None
    if meta.connection_mode == "webhook":
        webhook_secret = uuid.uuid4().hex

    from cognis.store.queries import create_channel_account

    async with session_factory() as session:
        row = await create_channel_account(
            session,
            channel_type=channel_type,
            display_name=display_name,
            agent_id=agent_id,
            user_email=user_email,
            config=body.get("settings", {}),
            credential_refs=body.get("credential_refs", {}),
            default_conversation_id=body.get("default_conversation_id"),
            allow_new_conversations=body.get("allow_new_conversations", True),
            allowed_senders=body.get("allowed_senders", []),
            dm_policy=body.get("dm_policy", "open"),
            group_policy=body.get("group_policy", "mention"),
            webhook_secret=webhook_secret,
        )
        await session.commit()

        return {
            "account_id": row.account_id,
            "channel_type": row.channel_type,
            "display_name": row.display_name,
            "webhook_secret": webhook_secret,
            "created_at": row.created_at.isoformat() if row.created_at else None,
        }


@router.get("/accounts/{account_id}", response_model=None)
async def get_account(request: Request, account_id: str) -> Any:
    """Get a channel account by ID."""
    session_factory = request.app.state.session_factory

    from cognis.store.queries import get_channel_account

    async with session_factory() as session:
        row = await get_channel_account(session, account_id)

    if row is None:
        return error_response(404, "not_found", "Channel account not found")

    result: dict[str, Any] = {
        "account_id": row.account_id,
        "channel_type": row.channel_type,
        "display_name": row.display_name,
        "enabled": row.enabled,
        "agent_id": row.agent_id,
        "config": row.config or {},
        "credential_refs": row.credential_refs or {},
        "default_conversation_id": row.default_conversation_id,
        "allow_new_conversations": row.allow_new_conversations,
        "allowed_senders": row.allowed_senders or [],
        "dm_policy": row.dm_policy,
        "group_policy": row.group_policy,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
    }

    channel_manager = getattr(request.app.state, "channel_manager", None)
    if channel_manager:
        status = await channel_manager.get_account_status(account_id)
        result["status"] = status.model_dump() if status else {"status": "stopped"}

    return result


@router.patch("/accounts/{account_id}", response_model=None)
async def update_account(request: Request, account_id: str) -> Any:
    """Update a channel account."""
    body = await request.json()
    session_factory = request.app.state.session_factory

    from cognis.store.queries import update_channel_account

    # Only allow updating specific fields
    allowed_fields = {
        "display_name",
        "enabled",
        "agent_id",
        "config",
        "credential_refs",
        "default_conversation_id",
        "allow_new_conversations",
        "allowed_senders",
        "dm_policy",
        "group_policy",
        "webhook_secret",
    }
    updates = {k: v for k, v in body.items() if k in allowed_fields}

    async with session_factory() as session:
        row = await update_channel_account(session, account_id, **updates)
        if row is None:
            return error_response(404, "not_found", "Channel account not found")
        await session.commit()

        return {
            "account_id": row.account_id,
            "channel_type": row.channel_type,
            "display_name": row.display_name,
            "enabled": row.enabled,
            "updated_at": row.updated_at.isoformat() if row.updated_at else None,
        }


@router.delete("/accounts/{account_id}", response_model=None)
async def delete_account(request: Request, account_id: str) -> Any:
    """Delete a channel account."""
    session_factory = request.app.state.session_factory

    # Stop the adapter first
    channel_manager = getattr(request.app.state, "channel_manager", None)
    if channel_manager:
        await channel_manager.stop_account(account_id)

    from cognis.store.queries import delete_channel_account

    async with session_factory() as session:
        deleted = await delete_channel_account(session, account_id)
        if not deleted:
            return error_response(404, "not_found", "Channel account not found")
        await session.commit()

    return {"deleted": True}


# ---------------------------------------------------------------------------
# Account lifecycle controls
# ---------------------------------------------------------------------------


@router.post("/accounts/{account_id}/start", response_model=None)
async def start_account(request: Request, account_id: str) -> Any:
    """Start a channel account."""
    channel_manager = getattr(request.app.state, "channel_manager", None)
    if channel_manager is None:
        return error_response(503, "unavailable", "Channel manager not initialized")

    try:
        await channel_manager.restart_account(account_id)
        status = await channel_manager.get_account_status(account_id)
        return {"status": status.model_dump() if status else "starting"}
    except Exception as exc:
        return error_response(500, "start_failed", str(exc))


@router.post("/accounts/{account_id}/stop", response_model=None)
async def stop_account(request: Request, account_id: str) -> Any:
    """Stop a channel account."""
    channel_manager = getattr(request.app.state, "channel_manager", None)
    if channel_manager is None:
        return error_response(503, "unavailable", "Channel manager not initialized")

    await channel_manager.stop_account(account_id)
    return {"status": "stopped"}


@router.get("/accounts/{account_id}/status", response_model=None)
async def get_account_status(request: Request, account_id: str) -> Any:
    """Get runtime status for a channel account."""
    channel_manager = getattr(request.app.state, "channel_manager", None)
    if channel_manager is None:
        return {"status": "stopped", "detail": "Channel manager not initialized"}

    status = await channel_manager.get_account_status(account_id)
    if status is None:
        return {"status": "stopped"}
    return status.model_dump()


# ---------------------------------------------------------------------------
# Webhook endpoint for inbound messages
# ---------------------------------------------------------------------------


@router.post("/webhook/{channel_type}/{account_id}")
async def handle_webhook(
    request: Request,
    channel_type: str,
    account_id: str,
) -> Response:
    """Handle inbound webhook from a messaging platform.

    This endpoint is unauthenticated (external platforms POST to it)
    but each request is verified using platform-specific signature
    verification.
    """
    channel_manager = getattr(request.app.state, "channel_manager", None)
    if channel_manager is None:
        return JSONResponse({"error": "Channel manager not available"}, status_code=503)

    body = await request.body()
    headers = dict(request.headers)

    result = await channel_manager.handle_webhook(
        channel_type=channel_type,
        account_id=account_id,
        headers=headers,
        body=body,
    )

    if result is None:
        return JSONResponse({"error": "Webhook rejected"}, status_code=403)

    return JSONResponse(result, status_code=200)


@router.get("/webhook/{channel_type}/{account_id}")
async def handle_webhook_verification(
    request: Request,
    channel_type: str,
    account_id: str,
) -> Response:
    """Handle webhook verification challenges (WhatsApp, Slack, etc.)."""
    # WhatsApp verification
    mode = request.query_params.get("hub.mode")
    if mode == "subscribe":
        verify_token = request.query_params.get("hub.verify_token", "")
        challenge = request.query_params.get("hub.challenge", "")

        session_factory = request.app.state.session_factory
        from cognis.store.queries import get_channel_account

        async with session_factory() as session:
            row = await get_channel_account(session, account_id)

        if row and row.webhook_secret == verify_token:
            return Response(content=challenge, media_type="text/plain")
        return Response(content="Verification failed", status_code=403)

    return Response(content="OK", status_code=200)


# ---------------------------------------------------------------------------
# Channel contacts
# ---------------------------------------------------------------------------


@router.get("/contacts")
async def list_contacts(request: Request) -> list[dict[str, Any]]:
    """List channel contact mappings."""
    session_factory = request.app.state.session_factory
    user_email = request.state.user_email

    from cognis.store.queries import list_channel_contacts

    async with session_factory() as session:
        contacts = await list_channel_contacts(session, user_email=user_email)

    return [
        {
            "contact_id": c.contact_id,
            "channel_type": c.channel_type,
            "sender_id": c.sender_id,
            "user_email": c.user_email,
            "display_name": c.display_name,
            "verified": c.verified,
            "created_at": c.created_at.isoformat() if c.created_at else None,
        }
        for c in contacts
    ]


@router.post("/contacts", response_model=None)
async def create_contact(request: Request) -> Any:
    """Create a channel contact mapping."""
    body = await request.json()
    session_factory = request.app.state.session_factory
    user_email = request.state.user_email

    channel_type = body.get("channel_type")
    sender_id = body.get("sender_id")
    if not channel_type or not sender_id:
        return error_response(400, "validation_error", "channel_type and sender_id are required")

    from cognis.store.queries import create_channel_contact

    async with session_factory() as session:
        contact = await create_channel_contact(
            session,
            channel_type=channel_type,
            sender_id=sender_id,
            user_email=body.get("user_email", user_email),
            display_name=body.get("display_name"),
            verified=body.get("verified", False),
        )
        await session.commit()

        return {
            "contact_id": contact.contact_id,
            "channel_type": contact.channel_type,
            "sender_id": contact.sender_id,
            "user_email": contact.user_email,
        }
