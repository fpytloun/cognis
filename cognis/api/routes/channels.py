"""Channel management API routes.

Provides CRUD for channel accounts, status monitoring, start/stop
controls, and webhook endpoints for inbound messages.
"""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, Request, Response
from fastapi.responses import JSONResponse

from cognis.api.common import error_response, require_current_user
from cognis.api.models import ChannelPairingRequestResponse
from cognis.channels.registry import get_channel_meta, list_channel_types
from cognis.logging import get_logger

logger = get_logger(__name__)

router = APIRouter(prefix="/api/v1/channels", tags=["channels"])


async def _validate_signal_account(request: Request, body: dict[str, Any]) -> Any | None:
    """Validate Signal-specific account configuration.

    Returns an error response if validation fails, or ``None`` if valid.
    """
    settings = body.get("settings", {})
    transport = settings.get("transport", "rest_api")
    credential_refs = body.get("credential_refs", {})
    adapter_location = body.get("adapter_location", "controller")
    executor_id = body.get("executor_id")

    # The channel UI stores non-secret credential fields in settings/config,
    # while secret fields go through credential_refs. Support both so
    # validation matches the actual runtime credential resolution path.
    account_number = credential_refs.get("account_number") or settings.get("account_number", "")
    if not account_number:
        return error_response(400, "validation_error", "Signal requires account_number credential")

    if transport == "direct_jsonrpc":
        if adapter_location != "executor":
            return error_response(
                400,
                "validation_error",
                "Signal direct_jsonrpc transport requires adapter_location='executor'",
            )
        if not executor_id:
            return error_response(
                400,
                "validation_error",
                "Signal direct_jsonrpc transport requires an explicit executor_id",
            )
        # Verify executor exists for the same owner and has direct Signal enabled
        from cognis.store.queries import get_executor_row

        user_email = require_current_user(request).email
        async with request.app.state.session_factory() as session:
            executor_row = await get_executor_row(session, executor_id, owner_email=user_email)
        if executor_row is None:
            return error_response(
                400,
                "validation_error",
                f"Executor '{executor_id}' not found or not owned by current user",
            )
        executor_config = executor_row.config or {}
        signal_config = executor_config.get("signal", {})
        if not signal_config.get("direct_enabled", False):
            return error_response(
                400,
                "validation_error",
                (
                    f"Executor '{executor_id}' does not have Signal direct mode enabled. "
                    "Set config.signal.direct_enabled=true on the executor."
                ),
            )

    elif transport == "rest_api":
        api_url = credential_refs.get("api_url") or settings.get("api_url", "")
        if not api_url:
            return error_response(
                400,
                "validation_error",
                "Signal REST API transport requires api_url credential",
            )
    else:
        return error_response(
            400,
            "validation_error",
            f"Unknown Signal transport: {transport}. Use 'rest_api' or 'direct_jsonrpc'.",
        )

    return None


async def _validate_bluebubbles_account(request: Request, body: dict[str, Any]) -> Any | None:
    """Validate BlueBubbles-specific account configuration.

    Returns an error response if validation fails, or ``None`` if valid.
    """
    credential_refs = body.get("credential_refs", {})

    server_url = credential_refs.get("server_url", "")
    if not server_url:
        return error_response(400, "validation_error", "BlueBubbles requires server_url credential")

    password = credential_refs.get("password", "")
    if not password:
        return error_response(400, "validation_error", "BlueBubbles requires password credential")

    return None


async def _pairing_response(
    request: Request,
    row: Any,
) -> ChannelPairingRequestResponse:
    session_factory = request.app.state.session_factory

    from cognis.store.queries import get_agent, get_channel_account

    account_display_name: str | None = None
    agent_id: str | None = None
    agent_name: str | None = None

    async with session_factory() as session:
        account = await get_channel_account(session, row.account_id)
        if account is not None:
            account_display_name = account.display_name
            agent_id = account.agent_id
            agent = await get_agent(session, account.agent_id)
            if agent is not None:
                agent_name = agent.display_name or agent.name

    return ChannelPairingRequestResponse(
        request_id=row.request_id,
        owner_email=row.owner_email,
        account_id=row.account_id,
        account_display_name=account_display_name,
        agent_id=agent_id,
        agent_name=agent_name,
        channel_type=row.channel_type,
        sender_id=row.sender_id,
        sender_name=row.sender_name,
        chat_id=row.chat_id,
        chat_name=row.chat_name,
        code=row.code,
        status=str(row.status),
        attempts=row.attempts,
        expires_at=row.expires_at,
        created_at=row.created_at,
        completed_at=row.completed_at,
    )


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
    user_email = require_current_user(request).email

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
            "adapter_location": getattr(row, "adapter_location", "controller"),
            "executor_id": getattr(row, "executor_id", None),
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
    user_email = require_current_user(request).email

    channel_type = body.get("channel_type")
    if not channel_type:
        return error_response(400, "validation_error", "channel_type is required")

    meta = get_channel_meta(channel_type)
    if meta is None:
        return error_response(400, "validation_error", f"Unknown channel type: {channel_type}")

    agent_id = body.get("agent_id")
    if not agent_id:
        return error_response(400, "validation_error", "agent_id is required")

    from cognis.store.queries import get_agent

    async with session_factory() as session:
        agent_row = await get_agent(session, agent_id)
    if agent_row is None:
        return error_response(404, "not_found", "Agent not found")
    if getattr(agent_row, "agent_type", "primary") != "primary":
        return error_response(
            400,
            "validation_error",
            "Channel accounts support primary agents only",
        )

    display_name = body.get("display_name", f"{meta.label} Account")

    # --- Channel-specific validation ---
    if channel_type == "signal":
        err = await _validate_signal_account(request, body)
        if err is not None:
            return err
    elif channel_type == "bluebubbles":
        err = await _validate_bluebubbles_account(request, body)
        if err is not None:
            return err

    # Generate webhook secret for webhook-based channels
    webhook_secret = None
    if meta.connection_mode == "webhook":
        if channel_type == "bluebubbles":
            # BlueBubbles uses the API password for webhook auth;
            # sync the webhook secret to the configured password.
            webhook_secret = body.get("credential_refs", {}).get("password", uuid.uuid4().hex)
        else:
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
            adapter_location=body.get("adapter_location", "controller"),
            executor_id=body.get("executor_id"),
            dm_policy=body.get("dm_policy", "pairing"),
            group_policy=body.get("group_policy", "pairing"),
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
        "adapter_location": getattr(row, "adapter_location", "controller"),
        "executor_id": getattr(row, "executor_id", None),
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

    # --- Signal-specific validation on update ---
    from cognis.store.queries import get_channel_account as _get_account
    from cognis.store.queries import update_channel_account

    async with session_factory() as session:
        existing_row = await _get_account(session, account_id)
    if existing_row is None:
        return error_response(404, "not_found", "Channel account not found")

    agent_id = body.get("agent_id")
    if agent_id:
        from cognis.store.queries import get_agent

        async with session_factory() as session:
            agent_row = await get_agent(session, agent_id)
        if agent_row is None:
            return error_response(404, "not_found", "Agent not found")
        if getattr(agent_row, "agent_type", "primary") != "primary":
            return error_response(
                400,
                "validation_error",
                "Channel accounts support primary agents only",
            )

    if existing_row.channel_type == "signal":
        # Merge existing settings and credentials with incoming for validation
        merged = dict(body)
        merged.setdefault(
            "adapter_location", getattr(existing_row, "adapter_location", "controller")
        )
        merged.setdefault("executor_id", getattr(existing_row, "executor_id", None))
        existing_settings = existing_row.config or {}
        merged_settings = {**existing_settings, **(body.get("config", {}))}
        merged["settings"] = merged_settings
        # Merge credential_refs: existing values + incoming overrides
        existing_creds = existing_row.credential_refs or {}
        incoming_creds = body.get("credential_refs", {})
        merged["credential_refs"] = {**existing_creds, **incoming_creds}
        err = await _validate_signal_account(request, merged)
        if err is not None:
            return err

    if existing_row.channel_type == "bluebubbles":
        merged = dict(body)
        existing_creds = existing_row.credential_refs or {}
        incoming_creds = body.get("credential_refs", {})
        merged["credential_refs"] = {**existing_creds, **incoming_creds}
        err = await _validate_bluebubbles_account(request, merged)
        if err is not None:
            return err
        # Auto-sync webhook_secret when BlueBubbles password changes
        new_password = incoming_creds.get("password")
        if new_password:
            body["webhook_secret"] = new_password

    # Only allow updating specific fields
    allowed_fields = {
        "display_name",
        "enabled",
        "agent_id",
        "config",
        "credential_refs",
        "default_conversation_id",
        "allow_new_conversations",
        "adapter_location",
        "executor_id",
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

    # Include query parameters so adapters that authenticate via query
    # string (e.g., BlueBubbles ?password=...) can verify them.
    for key, value in request.query_params.items():
        qp_key = f"x-query-{key}"
        if qp_key not in headers:
            headers[qp_key] = value

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
# Channel pairing
# ---------------------------------------------------------------------------


@router.get("/pairing-requests", response_model=list[ChannelPairingRequestResponse])
async def list_pairing_requests(request: Request) -> list[ChannelPairingRequestResponse]:
    """List pending pairing requests for the authenticated user."""
    pairing_service = getattr(request.app.state, "pairing_service", None)
    if pairing_service is None:
        return []

    rows = await pairing_service.list_pending_requests(
        owner_email=require_current_user(request).email
    )
    return [await _pairing_response(request, row) for row in rows]


@router.post("/pair", response_model=ChannelPairingRequestResponse)
async def redeem_pairing_code(request: Request) -> Any:
    """Redeem a sender-initiated pairing code from the web UI."""
    pairing_service = getattr(request.app.state, "pairing_service", None)
    if pairing_service is None:
        return error_response(503, "unavailable", "Pairing service not initialized")

    body = await request.json()
    code = body.get("code", "")
    if not isinstance(code, str) or not code.strip():
        return error_response(400, "validation_error", "code is required")

    try:
        row = await pairing_service.redeem_code(
            owner_email=require_current_user(request).email, code=code
        )
    except ValueError as exc:
        return error_response(400, "validation_error", str(exc))
    return await _pairing_response(request, row)


@router.post("/pairing-requests/{request_id}/reject", response_model=None)
async def reject_pairing_request(request: Request, request_id: str) -> Any:
    """Reject a pending pairing request."""
    pairing_service = getattr(request.app.state, "pairing_service", None)
    if pairing_service is None:
        return error_response(503, "unavailable", "Pairing service not initialized")

    rejected = await pairing_service.reject_request(
        owner_email=require_current_user(request).email,
        request_id=request_id,
    )
    if not rejected:
        return error_response(404, "not_found", "Pairing request not found")
    return {"rejected": True}


# ---------------------------------------------------------------------------
# Channel contacts
# ---------------------------------------------------------------------------


@router.get("/contacts")
async def list_contacts(request: Request) -> list[dict[str, Any]]:
    """List channel contact mappings."""
    session_factory = request.app.state.session_factory
    user_email = require_current_user(request).email

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
    user_email = require_current_user(request).email

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
            verified=body.get("verified", True),
        )
        await session.commit()

        return {
            "contact_id": contact.contact_id,
            "channel_type": contact.channel_type,
            "sender_id": contact.sender_id,
            "user_email": contact.user_email,
        }
