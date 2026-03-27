"""Setup and auth routes."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse

from cognis.api.models import (
    ExchangeTokenResponse,
    LoginRequest,
    LogoutRequest,
    RefreshRequest,
    SetupRequest,
    TokenResponse,
)
from cognis.store.queries import count_users, create_user, get_setting_value, get_user

router = APIRouter()


def _as_int(value: object, default: int) -> int:
    return value if isinstance(value, int) else default


@router.get("/setup", response_class=HTMLResponse)
async def setup_page() -> str:
    return """
    <html><body><h1>Cognis Setup</h1><p>Use POST /api/setup with the printed token to create the first admin.</p></body></html>
    """


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


@router.post("/api/v1/auth/exchange-token", response_model=ExchangeTokenResponse)
async def exchange_token(request: Request, target: str = "intaris") -> ExchangeTokenResponse:
    user = request.state.user
    token = request.app.state.auth_provider.sign_exchange_token(user.email, target)
    return ExchangeTokenResponse(token=token, target=target, expires_in=60)
