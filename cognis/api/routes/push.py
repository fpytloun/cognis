"""Web Push subscription endpoints for PWA notifications."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select

from cognis.api.common import require_current_user
from cognis.core.web_push import WebPushService
from cognis.store.models import PushSubscriptionRow

router = APIRouter(prefix="/api/v1/push", tags=["push"])


class VapidPublicKeyResponse(BaseModel):
    """Current Web Push availability and browser application key."""

    enabled: bool
    public_key: str | None = None
    reason: str | None = None


class PushSubscriptionKeys(BaseModel):
    """Browser PushSubscription key material."""

    p256dh: str
    auth: str


class PushSubscriptionRequest(BaseModel):
    """Register or refresh a browser PushSubscription."""

    model_config = ConfigDict(populate_by_name=True)

    endpoint: str
    expiration_time: int | None = Field(default=None, alias="expirationTime")
    keys: PushSubscriptionKeys
    platform: str | None = None


class PushUnsubscribeRequest(BaseModel):
    """Disable a browser PushSubscription by endpoint."""

    endpoint: str


class PushSubscriptionResponse(BaseModel):
    """Registered browser PushSubscription metadata."""

    subscription_id: str
    enabled: bool


class PushSubscriptionStatusResponse(BaseModel):
    """Current user's browser push delivery status."""

    configured: bool
    enabled_subscriptions: int
    last_error: str | None = None


def _get_service(request: Request) -> WebPushService:
    service: WebPushService | None = getattr(request.app.state, "web_push_service", None)
    if service is None:
        raise HTTPException(status_code=503, detail="Web Push service not available")
    return service


@router.get("/vapid-public-key", response_model=VapidPublicKeyResponse)
async def get_vapid_public_key(request: Request) -> VapidPublicKeyResponse:
    """Return the public VAPID key needed by PushManager.subscribe()."""

    require_current_user(request)
    service = _get_service(request)
    return VapidPublicKeyResponse(
        enabled=service.enabled,
        public_key=service.public_key if service.enabled else None,
        reason=service.disabled_reason,
    )


@router.post("/subscriptions", response_model=PushSubscriptionResponse)
async def register_subscription(
    request: Request,
    payload: PushSubscriptionRequest,
) -> PushSubscriptionResponse:
    """Register or refresh the current browser's Web Push subscription."""

    user = require_current_user(request)
    service = _get_service(request)
    if not service.enabled:
        raise HTTPException(status_code=503, detail=service.disabled_reason or "Web Push disabled")
    user_agent = request.headers.get("user-agent")
    subscription = await service.upsert_subscription(
        user_email=user.email,
        endpoint=payload.endpoint,
        p256dh=payload.keys.p256dh,
        auth=payload.keys.auth,
        user_agent=user_agent,
        platform=payload.platform,
    )
    return PushSubscriptionResponse(
        subscription_id=subscription.subscription_id,
        enabled=subscription.enabled,
    )


@router.get("/subscriptions/status", response_model=PushSubscriptionStatusResponse)
async def subscription_status(request: Request) -> PushSubscriptionStatusResponse:
    """Return log-safe Web Push status for the current user."""

    user = require_current_user(request)
    service = _get_service(request)
    async with request.app.state.session_factory() as session:
        count_result = await session.execute(
            select(func.count()).select_from(PushSubscriptionRow).where(
                PushSubscriptionRow.user_email == user.email,
                PushSubscriptionRow.enabled.is_(True),
            )
        )
        error_result = await session.execute(
            select(PushSubscriptionRow.last_error)
            .where(
                PushSubscriptionRow.user_email == user.email,
                PushSubscriptionRow.last_error.is_not(None),
            )
            .order_by(PushSubscriptionRow.updated_at.desc())
            .limit(1)
        )
    return PushSubscriptionStatusResponse(
        configured=service.enabled,
        enabled_subscriptions=int(count_result.scalar_one() or 0),
        last_error=error_result.scalar_one_or_none(),
    )


@router.post("/subscriptions/unsubscribe", response_model=dict[str, Any])
async def unregister_subscription(
    request: Request,
    payload: PushUnsubscribeRequest,
) -> dict[str, Any]:
    """Disable the current browser's Web Push subscription."""

    user = require_current_user(request)
    service = _get_service(request)
    removed = await service.unsubscribe(user_email=user.email, endpoint=payload.endpoint)
    return {"ok": True, "removed": removed}
