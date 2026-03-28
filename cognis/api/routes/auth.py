"""Setup and auth routes."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, HTTPException, Request

from cognis.api.common import api_exception, require_jwt_user
from cognis.api.models import (
    ApiKeyCreateRequest,
    ApiKeyCreateResponse,
    ApiKeyResponse,
    BootstrapStatusResponse,
    ExchangeTokenResponse,
    LoginRequest,
    LogoutRequest,
    PasswordChangeRequest,
    RefreshRequest,
    SetupRequest,
    TokenResponse,
)
from cognis.security import generate_api_key_material
from cognis.store.queries import (
    count_users,
    create_api_key,
    create_user,
    delete_api_key,
    get_setting_value,
    get_user,
    list_api_keys,
    update_user_password,
)

router = APIRouter()


def _as_int(value: object, default: int) -> int:
    return value if isinstance(value, int) else default


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


@router.post("/api/auth/login", response_model=TokenResponse)
async def login(request: Request, payload: LoginRequest) -> TokenResponse:
    app_state = request.app.state
    if app_state.login_rate_limiter.is_limited(payload.email):
        raise HTTPException(status_code=429, detail="Too many failed login attempts")
    async with app_state.session_factory() as session:
        user = await get_user(session, payload.email)
        if user is None or user.password_hash is None:
            app_state.login_rate_limiter.record_failure(payload.email)
            raise HTTPException(status_code=401, detail="Invalid credentials")
        try:
            app_state.password_hasher.verify(user.password_hash, payload.password)
        except Exception:
            app_state.login_rate_limiter.record_failure(payload.email)
            raise HTTPException(status_code=401, detail="Invalid credentials") from None
        app_state.login_rate_limiter.clear(payload.email)
        ttl = _as_int(await get_setting_value(session, "security.token_ttl_seconds", 3600), 3600)
        token = app_state.auth_provider.sign_access_token(user.email, user.name, user.role)
        refresh_token = app_state.auth_provider.sign_refresh_token(user.email)
        return TokenResponse(
            token=token,
            refresh_token=refresh_token,
            expires_in=ttl,
            user={"email": user.email, "name": user.name, "role": user.role},
        )


@router.post("/api/auth/refresh", response_model=TokenResponse)
async def refresh(request: Request, payload: RefreshRequest) -> TokenResponse:
    app_state = request.app.state
    try:
        claims = app_state.auth_provider.verify_jwt(payload.refresh_token, audience=["cognis"])
    except Exception as exc:
        raise HTTPException(status_code=401, detail="Invalid refresh token") from exc
    if claims.get("typ") != "refresh":
        raise HTTPException(status_code=401, detail="Invalid refresh token")
    async with app_state.session_factory() as session:
        user = await get_user(session, str(claims["sub"]))
        if user is None:
            raise HTTPException(status_code=401, detail="Unknown user")
        ttl = _as_int(await get_setting_value(session, "security.token_ttl_seconds", 3600), 3600)
        token = app_state.auth_provider.sign_access_token(user.email, user.name, user.role)
        refresh_token = app_state.auth_provider.sign_refresh_token(user.email)
        return TokenResponse(
            token=token,
            refresh_token=refresh_token,
            expires_in=ttl,
            user={"email": user.email, "name": user.name, "role": user.role},
        )


@router.post("/api/auth/logout")
async def logout(request: Request, payload: LogoutRequest) -> dict[str, bool]:
    claims = getattr(request.state, "claims", None)
    auth_provider = request.app.state.auth_provider
    if claims is not None and (jti := claims.get("jti")) is not None:
        auth_provider.revoke_token(str(jti))
    if payload.refresh_token:
        try:
            refresh_claims = auth_provider.verify_jwt(payload.refresh_token, audience=["cognis"])
        except Exception as exc:
            raise HTTPException(status_code=401, detail="Invalid refresh token") from exc
        jti = refresh_claims.get("jti")
        if jti is not None:
            auth_provider.revoke_token(str(jti))
    return {"ok": True}


@router.get("/api/auth/me")
async def me(request: Request) -> dict[str, str | None]:
    user = request.state.user
    return {"email": user.email, "name": user.name, "role": user.role}


@router.post("/api/auth/change-password", response_model=dict[str, bool])
async def change_password(request: Request, payload: PasswordChangeRequest) -> dict[str, bool]:
    user = require_jwt_user(request)
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
    user = require_jwt_user(request)
    async with request.app.state.session_factory() as session:
        records = await list_api_keys(session, user.email)
    return [_api_key_response(record) for record in records]


@router.post("/api/v1/auth/api-keys", response_model=ApiKeyCreateResponse)
async def api_key_create(request: Request, payload: ApiKeyCreateRequest) -> ApiKeyCreateResponse:
    user = require_jwt_user(request)
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
    user = require_jwt_user(request)
    async with request.app.state.session_factory() as session:
        ok = await delete_api_key(session, key_id, user.email)
        await session.commit()
    if not ok:
        raise api_exception(404, "not_found", "API key not found")
    return {"ok": True}


@router.post("/api/v1/auth/exchange-token", response_model=ExchangeTokenResponse)
async def exchange_token(request: Request, target: str = "intaris") -> ExchangeTokenResponse:
    user = request.state.user
    token = request.app.state.auth_provider.sign_exchange_token(user.email, target)
    return ExchangeTokenResponse(token=token, target=target, expires_in=60)
