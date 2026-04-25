"""Setup and auth routes."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Literal

from fastapi import APIRouter, HTTPException, Request
from starlette.responses import JSONResponse, Response

from cognis.api.common import api_exception, require_current_user, require_session_user
from cognis.api.models import (
    ApiKeyCreateRequest,
    ApiKeyCreateResponse,
    ApiKeyResponse,
    AuthSessionResponse,
    BootstrapStatusResponse,
    ExchangeTokenResponse,
    LoginRequest,
    LogoutRequest,
    PasswordChangeRequest,
    ProfileUpdateRequest,
    RefreshRequest,
    SetupRequest,
)
from cognis.security import generate_api_key_material
from cognis.store.queries import (
    count_users,
    create_api_key,
    create_browser_session,
    create_user,
    delete_api_key,
    get_browser_session_by_token,
    get_user,
    list_api_keys,
    revoke_browser_session,
    touch_browser_session,
    update_user,
    update_user_last_login,
    update_user_password,
)

router = APIRouter()

COOKIE_NAME = "cognis_session"


def _cookie_samesite(request: Request) -> str:
    raw = str(getattr(request.app.state.config, "session_cookie_samesite", "lax") or "lax").lower()
    if raw not in {"lax", "strict", "none"}:
        return "lax"
    return raw


def _cookie_secure(request: Request) -> bool:
    forwarded = request.headers.get("x-forwarded-proto", "").split(",", 1)[0].strip().lower()
    scheme = forwarded or request.url.scheme.lower()
    return scheme == "https"


def _set_session_cookie(response: Response, request: Request, token: str, max_age: int) -> None:
    """Set the opaque browser session cookie on a response."""

    cookie_domain = getattr(request.app.state.config, "session_cookie_domain", "") or None
    response.set_cookie(
        key=COOKIE_NAME,
        value=token,
        max_age=max_age,
        path="/",
        domain=cookie_domain,
        httponly=True,
        samesite=_cookie_samesite(request),
        secure=_cookie_secure(request),
    )


def _clear_session_cookie(response: Response, request: Request) -> None:
    """Clear the opaque browser session cookie."""

    cookie_domain = getattr(request.app.state.config, "session_cookie_domain", "") or None
    response.delete_cookie(
        key=COOKIE_NAME,
        path="/",
        domain=cookie_domain,
        samesite=_cookie_samesite(request),
        secure=_cookie_secure(request),
    )


def _browser_session_expiry(request: Request) -> datetime:
    ttl = int(getattr(request.app.state.config, "browser_session_ttl_seconds", 30 * 24 * 60 * 60))
    return datetime.now(UTC) + timedelta(seconds=max(60, ttl))


def _browser_session_max_age(request: Request) -> int:
    ttl = int(getattr(request.app.state.config, "browser_session_ttl_seconds", 30 * 24 * 60 * 60))
    return max(60, ttl)


def _legacy_token_ttl(request: Request) -> int:
    return int(getattr(request.app.state.auth_provider, "token_ttl_seconds", 3600) or 3600)


def _auth_session_body(
    request: Request,
    *,
    email: str,
    name: str | None,
    role: str,
    expires_at: datetime,
) -> AuthSessionResponse:
    access_token = request.app.state.auth_provider.sign_access_token(email, name, role)
    refresh_token = request.app.state.auth_provider.sign_refresh_token(email)
    return AuthSessionResponse(
        user={"email": email, "name": name, "role": role},
        expires_at=expires_at,
        token=access_token,
        refresh_token=refresh_token,
        expires_in=_legacy_token_ttl(request),
    )


def _api_key_prefix(key_id: str) -> str:
    return f"cognis_{key_id}_"


def _api_key_response(record: object) -> ApiKeyResponse:
    return ApiKeyResponse(
        key_id=record.key_id,  # type: ignore[attr-defined]
        name=record.name,  # type: ignore[attr-defined]
        prefix=_api_key_prefix(record.key_id),  # type: ignore[attr-defined]
        created_at=record.created_at,  # type: ignore[attr-defined]
        last_used_at=record.last_used_at,  # type: ignore[attr-defined]
        expires_at=record.expires_at,  # type: ignore[attr-defined]
    )


@router.get("/api/bootstrap-status", response_model=BootstrapStatusResponse)
async def bootstrap_status(request: Request) -> BootstrapStatusResponse:
    async with request.app.state.session_factory() as session:
        user_count = await count_users(session)
    return BootstrapStatusResponse(setup_available=user_count == 0, setup_complete=user_count > 0)


@router.post("/api/setup")
async def setup_admin(request: Request, payload: SetupRequest) -> dict[str, bool]:
    app_state = request.app.state
    async with app_state.session_factory() as session:
        if await count_users(session) > 0:
            raise HTTPException(status_code=404, detail="Setup no longer available")
        if not app_state.setup_token_manager.validate(payload.token):
            raise HTTPException(status_code=401, detail="Invalid or expired setup token")
        password_hash = app_state.password_hasher.hash(payload.password)
        await create_user(
            session,
            email=payload.email,
            name=payload.name,
            password_hash=password_hash,
            role="admin",
        )
        await session.commit()
    app_state.setup_token_manager.invalidate()
    return {"ok": True}


@router.post("/api/auth/login", response_model=AuthSessionResponse)
async def login(request: Request, payload: LoginRequest) -> AuthSessionResponse:
    app_state = request.app.state
    if app_state.login_rate_limiter.is_limited(payload.email):
        raise HTTPException(status_code=429, detail="Too many failed login attempts")
    async with app_state.session_factory() as session:
        user = await get_user(session, payload.email)
        if user is None or user.password_hash is None:
            app_state.login_rate_limiter.record_failure(payload.email)
            raise HTTPException(status_code=401, detail="Invalid credentials")
        if not user.is_active:
            raise HTTPException(status_code=403, detail="Account disabled")
        try:
            app_state.password_hasher.verify(user.password_hash, payload.password)
        except Exception:
            app_state.login_rate_limiter.record_failure(payload.email)
            raise HTTPException(status_code=401, detail="Invalid credentials") from None
        app_state.login_rate_limiter.clear(payload.email)
        await update_user_last_login(session, user.email)
        expires_at = _browser_session_expiry(request)
        browser_session, raw_token = await create_browser_session(
            session,
            user_email=user.email,
            expires_at=expires_at,
            user_agent=request.headers.get("user-agent"),
        )
        await session.commit()
        body = _auth_session_body(
            request,
            email=user.email,
            name=user.name,
            role=user.role,
            expires_at=browser_session.expires_at,
        )
        response = JSONResponse(content=body.model_dump(mode="json"))
        _set_session_cookie(response, request, raw_token, _browser_session_max_age(request))
        return response  # type: ignore[return-value]


@router.post("/api/auth/refresh", response_model=AuthSessionResponse)
async def refresh(request: Request, payload: RefreshRequest | None = None) -> AuthSessionResponse:
    raw_token = request.cookies.get(COOKIE_NAME)

    app_state = request.app.state
    async with app_state.session_factory() as session:
        user = None
        browser_session = None

        if raw_token:
            browser_session = await get_browser_session_by_token(session, raw_token)
            if browser_session is None or browser_session.revoked_at is not None:
                raise HTTPException(status_code=401, detail="Invalid browser session")
            if browser_session.expires_at <= datetime.now(UTC):
                raise HTTPException(status_code=401, detail="Browser session expired")
            user = await get_user(session, browser_session.user_email)
        elif payload and payload.refresh_token:
            try:
                claims = app_state.auth_provider.verify_jwt(payload.refresh_token, audience=["cognis"])
            except Exception as exc:
                raise HTTPException(status_code=401, detail="Invalid refresh token") from exc
            if claims.get("typ") != "refresh":
                raise HTTPException(status_code=401, detail="Invalid refresh token")
            user = await get_user(session, str(claims["sub"]))
            if user is not None:
                browser_session, raw_token = await create_browser_session(
                    session,
                    user_email=user.email,
                    expires_at=_browser_session_expiry(request),
                    user_agent=request.headers.get("user-agent"),
                )
        else:
            raise HTTPException(status_code=401, detail="No active browser session")

        if user is None:
            raise HTTPException(status_code=401, detail="Unknown user")
        if not user.is_active:
            raise HTTPException(status_code=403, detail="Account disabled")

        refreshed_expiry = _browser_session_expiry(request)
        if browser_session is None:
            raise HTTPException(status_code=401, detail="No active browser session")
        browser_session = await touch_browser_session(session, browser_session, expires_at=refreshed_expiry)
        await session.commit()
        body = _auth_session_body(
            request,
            email=user.email,
            name=user.name,
            role=user.role,
            expires_at=browser_session.expires_at,
        )
        response = JSONResponse(content=body.model_dump(mode="json"))
        _set_session_cookie(response, request, raw_token, _browser_session_max_age(request))
        return response  # type: ignore[return-value]


@router.post("/api/auth/logout")
async def logout(request: Request, payload: LogoutRequest | None = None) -> Response:
    claims = getattr(request.state, "claims", None)
    auth_provider = request.app.state.auth_provider
    if claims is not None and (jti := claims.get("jti")) is not None:
        auth_provider.revoke_token(str(jti))
    if payload and payload.refresh_token:
        try:
            refresh_claims = auth_provider.verify_jwt(payload.refresh_token, audience=["cognis"])
        except Exception as exc:
            raise HTTPException(status_code=401, detail="Invalid refresh token") from exc
        jti = refresh_claims.get("jti")
        if jti is not None:
            auth_provider.revoke_token(str(jti))

    raw_token = request.cookies.get(COOKIE_NAME)
    if raw_token:
        async with request.app.state.session_factory() as session:
            browser_session = await get_browser_session_by_token(session, raw_token)
            if browser_session is not None:
                await revoke_browser_session(session, browser_session.session_id)
                await session.commit()
    response = JSONResponse(content={"ok": True})
    _clear_session_cookie(response, request)
    return response


@router.get("/api/auth/me")
async def me(request: Request) -> dict[str, str | None]:
    user = request.state.user
    return {"email": user.email, "name": user.name, "role": user.role}


@router.patch("/api/auth/me")
async def update_profile(request: Request, payload: ProfileUpdateRequest) -> dict[str, str | None]:
    """Update the current user's profile (name only)."""
    current = require_current_user(request)
    async with request.app.state.session_factory() as session:
        user = await update_user(session, current.email, name=payload.name)
        if user is None:
            raise api_exception(404, "not_found", "User not found")
        await session.commit()
    return {"email": user.email, "name": user.name, "role": user.role}


@router.post("/api/auth/change-password", response_model=dict[str, bool])
async def change_password(request: Request, payload: PasswordChangeRequest) -> dict[str, bool]:
    user = require_session_user(request)
    app_state = request.app.state
    limiter_key = f"password-change:{user.email}"
    if app_state.login_rate_limiter.is_limited(limiter_key):
        raise api_exception(429, "rate_limited", "Too many failed password change attempts")

    async with app_state.session_factory() as session:
        row = await get_user(session, user.email)
        if row is None or row.password_hash is None:
            raise api_exception(404, "not_found", "User not found")
        try:
            app_state.password_hasher.verify(row.password_hash, payload.current_password)
        except Exception as exc:
            app_state.login_rate_limiter.record_failure(limiter_key)
            raise api_exception(401, "unauthorized", "Current password is incorrect") from exc

        password_hash = app_state.password_hasher.hash(payload.new_password)
        await update_user_password(session, user.email, password_hash)
        await session.commit()

    app_state.login_rate_limiter.clear(limiter_key)
    return {"ok": True}


@router.get("/api/v1/auth/api-keys", response_model=list[ApiKeyResponse])
async def api_key_list(request: Request) -> list[ApiKeyResponse]:
    user = require_session_user(request)
    async with request.app.state.session_factory() as session:
        records = await list_api_keys(session, user.email)
    return [_api_key_response(record) for record in records]


@router.post("/api/v1/auth/api-keys", response_model=ApiKeyCreateResponse)
async def api_key_create(request: Request, payload: ApiKeyCreateRequest) -> ApiKeyCreateResponse:
    user = require_session_user(request)
    app_state = request.app.state
    key_id, api_key = generate_api_key_material()
    expires_at = None
    if payload.expires_in_days is not None:
        expires_at = datetime.now(UTC) + timedelta(days=payload.expires_in_days)

    async with app_state.session_factory() as session:
        record = await create_api_key(
            session,
            user_email=user.email,
            key_hash=app_state.password_hasher.hash(api_key),
            name=payload.name,
            key_id=key_id,
        )
        record.expires_at = expires_at
        await session.commit()
        await session.refresh(record)

    metadata = _api_key_response(record)
    return ApiKeyCreateResponse(**metadata.model_dump(), api_key=api_key)


@router.delete("/api/v1/auth/api-keys/{key_id}", response_model=dict[str, bool])
async def api_key_delete(request: Request, key_id: str) -> dict[str, bool]:
    user = require_session_user(request)
    async with request.app.state.session_factory() as session:
        ok = await delete_api_key(session, key_id, user.email)
        await session.commit()
    if not ok:
        raise api_exception(404, "not_found", "API key not found")
    return {"ok": True}


@router.post("/api/v1/auth/exchange-token", response_model=ExchangeTokenResponse)
async def exchange_token(
    request: Request, target: Literal["intaris", "mnemory"] = "intaris"
) -> ExchangeTokenResponse:
    user = request.state.user
    token = request.app.state.auth_provider.sign_exchange_token(user.email, target)
    return ExchangeTokenResponse(token=token, target=target, expires_in=60)
