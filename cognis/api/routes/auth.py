"""Setup and auth routes."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any, Literal, cast

import pyotp
from fastapi import APIRouter, HTTPException, Request
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.responses import JSONResponse, Response

from cognis.api.common import api_exception, require_current_user, require_session_user
from cognis.api.models import (
    ApiKeyCreateRequest,
    ApiKeyCreateResponse,
    ApiKeyResponse,
    BootstrapStatusResponse,
    BrowserSessionResponse,
    ExchangeTokenResponse,
    LoginRequest,
    LogoutRequest,
    MfaChallengeRequest,
    MfaChallengeResponse,
    MfaCodeRequest,
    MfaEnabledResponse,
    MfaEnrollmentStartRequest,
    MfaManageRequest,
    MfaRecoveryCodesResponse,
    MfaSetupResponse,
    MfaStatusResponse,
    NativeTokenResponse,
    PasswordChangeRequest,
    ProfileUpdateRequest,
    RefreshRequest,
    SetupRequest,
)
from cognis.api.proxy import trusted_request_scheme
from cognis.mfa import (
    CHALLENGE_TTL,
    MFA_POLICY_KEY,
    MfaSecretCipher,
    activate_totp_factor,
    clear_user_mfa_attempts,
    consume_challenge,
    create_mfa_challenge,
    get_active_totp_factor,
    get_live_challenge,
    invalidate_other_setup_challenges,
    matching_totp_counter,
    mfa_attempts_limited,
    replace_recovery_codes,
    reserve_challenge_attempt,
    reserve_user_mfa_attempt,
    revoke_user_auth_sessions,
    verify_totp_or_recovery,
)
from cognis.security import generate_api_key_material
from cognis.store.models import MfaChallenge, MfaRecoveryCode, UserTotpFactor
from cognis.store.queries import (
    count_users,
    create_api_key,
    create_browser_session,
    create_native_session,
    create_user,
    delete_api_key,
    get_browser_session_by_token,
    get_setting_value,
    get_user,
    list_api_keys,
    revoke_browser_session,
    revoke_native_session_by_token,
    rotate_native_session,
    touch_browser_session,
    update_user,
    update_user_last_login,
    update_user_password,
)

router = APIRouter()

COOKIE_NAME = "cognis_session"
MFA_PASSWORD_LIMITER_PREFIX = "mfa-management"


def _cookie_samesite(request: Request) -> Literal["lax", "strict", "none"]:
    raw = str(getattr(request.app.state.config, "session_cookie_samesite", "lax") or "lax").lower()
    if raw not in {"lax", "strict", "none"}:
        return "lax"
    return cast(Literal["lax", "strict", "none"], raw)


def _cookie_secure(request: Request) -> bool:
    return trusted_request_scheme(request) == "https"


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


def _access_token_ttl(request: Request) -> int:
    return int(getattr(request.app.state.auth_provider, "token_ttl_seconds", 3600) or 3600)


def _native_refresh_expiry(request: Request) -> datetime:
    ttl = int(getattr(request.app.state.auth_provider, "refresh_ttl_seconds", 7 * 24 * 60 * 60))
    return datetime.now(UTC) + timedelta(seconds=max(60, ttl))


def _native_token_body(
    request: Request,
    *,
    email: str,
    name: str | None,
    role: str,
    refresh_token: str,
    auth_version: int,
) -> NativeTokenResponse:
    access_token = request.app.state.auth_provider.sign_access_token(
        email,
        name,
        role,
        auth_version=auth_version,
    )
    return NativeTokenResponse(
        user={"email": email, "name": name, "role": role},
        token=access_token,
        refresh_token=refresh_token,
        expires_in=_access_token_ttl(request),
    )


async def _mfa_policy(session: AsyncSession) -> Literal["optional", "required"]:
    value = await get_setting_value(session, MFA_POLICY_KEY, "optional")
    return "required" if value == "required" else "optional"


def _verify_mfa_current_password(request: Request, user: Any, password: str) -> None:
    """Verify and rate-limit current-password proofs for MFA management."""

    limiter = request.app.state.login_rate_limiter
    limiter_key = f"{MFA_PASSWORD_LIMITER_PREFIX}:{user.email}"
    if limiter.is_limited(limiter_key):
        raise HTTPException(status_code=429, detail="Too many failed password attempts")
    try:
        request.app.state.password_hasher.verify(user.password_hash, password)
    except Exception:
        limiter.record_failure(limiter_key)
        raise HTTPException(status_code=401, detail="Invalid credentials") from None
    limiter.clear(limiter_key)


async def _disconnect_user_websockets(request: Request, user_email: str) -> None:
    manager = getattr(request.app.state, "ws_manager", None)
    if manager is not None:
        await manager.disconnect_user(user_email)


async def _issue_authenticated_session(
    request: Request,
    session: AsyncSession,
    *,
    user: Any,
    mode: Literal["browser", "native"],
    recovery_codes: list[str] | None = None,
) -> BrowserSessionResponse | NativeTokenResponse | Response:
    await update_user_last_login(session, user.email)
    if mode == "native":
        _, refresh_token = await create_native_session(
            session,
            user_email=user.email,
            expires_at=_native_refresh_expiry(request),
            auth_version=user.auth_version,
        )
        await session.commit()
        return _native_token_body(
            request,
            email=user.email,
            name=user.name,
            role=user.role,
            refresh_token=refresh_token,
            auth_version=user.auth_version,
        ).model_copy(update={"recovery_codes": recovery_codes})
    browser_session, raw_token = await create_browser_session(
        session,
        user_email=user.email,
        expires_at=_browser_session_expiry(request),
        auth_version=user.auth_version,
        user_agent=request.headers.get("user-agent"),
    )
    await session.commit()
    body = BrowserSessionResponse(
        user={"email": user.email, "name": user.name, "role": user.role},
        expires_at=browser_session.expires_at,
        recovery_codes=recovery_codes,
    )
    response = JSONResponse(content=body.model_dump(mode="json"))
    _set_session_cookie(response, request, raw_token, _browser_session_max_age(request))
    return response


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


@router.post(
    "/api/auth/login",
    response_model=BrowserSessionResponse | NativeTokenResponse | MfaChallengeResponse,
)
async def login(
    request: Request, payload: LoginRequest
) -> BrowserSessionResponse | NativeTokenResponse | MfaChallengeResponse | Response:
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
        factor = await get_active_totp_factor(session, user.email)
        policy = await _mfa_policy(session)
        if factor is not None or policy == "required":
            if await mfa_attempts_limited(session, user_email=user.email):
                raise HTTPException(status_code=429, detail="Too many MFA attempts")
            purpose: Literal["login_verify", "login_setup"] = (
                "login_verify" if factor is not None else "login_setup"
            )
            challenge = await create_mfa_challenge(
                session,
                user_email=user.email,
                purpose=purpose,
                login_mode=payload.mode,
                auth_version=user.auth_version,
                pending_secret=(
                    app_state.mfa_cipher.encrypt(pyotp.random_base32())
                    if purpose == "login_setup"
                    else None
                ),
            )
            await session.commit()
            return MfaChallengeResponse(
                status="mfa_required" if factor is not None else "mfa_setup_required",
                challenge_token=challenge.token,
                expires_in=int(CHALLENGE_TTL.total_seconds()),
            )
        return await _issue_authenticated_session(
            request,
            session,
            user=user,
            mode=payload.mode,
        )


@router.post("/api/auth/mfa/setup/start", response_model=MfaSetupResponse)
async def start_login_mfa_setup(request: Request, payload: MfaChallengeRequest) -> MfaSetupResponse:
    async with request.app.state.session_factory() as session:
        challenge = await get_live_challenge(
            session, payload.challenge_token, purposes={"login_setup"}
        )
        if challenge is None:
            raise HTTPException(status_code=401, detail="Invalid or expired MFA challenge")
        cipher: MfaSecretCipher = request.app.state.mfa_cipher
        if challenge.pending_secret is None:
            raise HTTPException(status_code=409, detail="MFA setup is not initialized")
        secret = cipher.decrypt(challenge.pending_secret)
        uri = pyotp.TOTP(secret).provisioning_uri(
            name=challenge.user_email,
            issuer_name="Cognis",
        )
        return MfaSetupResponse(
            status="mfa_setup",
            challenge_token=payload.challenge_token,
            secret=secret,
            provisioning_uri=uri,
        )


@router.post(
    "/api/auth/mfa/setup/confirm",
    response_model=BrowserSessionResponse | NativeTokenResponse,
)
async def confirm_mfa_setup(
    request: Request, payload: MfaCodeRequest
) -> BrowserSessionResponse | NativeTokenResponse | Response:
    result = await _confirm_mfa_setup(
        request,
        payload,
        purpose="login_setup",
        bound_user_email=None,
    )
    if isinstance(result, MfaEnabledResponse):
        raise RuntimeError("Login MFA setup returned an account response")
    return result


@router.post("/api/auth/mfa/enroll/confirm", response_model=MfaEnabledResponse)
async def confirm_account_mfa_setup(
    request: Request,
    payload: MfaCodeRequest,
) -> MfaEnabledResponse:
    current = require_session_user(request)
    result = await _confirm_mfa_setup(
        request,
        payload,
        purpose="account_setup",
        bound_user_email=current.email,
    )
    if not isinstance(result, MfaEnabledResponse):
        raise RuntimeError("Account MFA setup returned a login session")
    return result


async def _confirm_mfa_setup(
    request: Request,
    payload: MfaCodeRequest,
    *,
    purpose: Literal["login_setup", "account_setup"],
    bound_user_email: str | None,
) -> BrowserSessionResponse | NativeTokenResponse | MfaEnabledResponse | Response:
    now = datetime.now(UTC)
    async with request.app.state.session_factory() as session:
        challenge = await get_live_challenge(
            session,
            payload.challenge_token,
            purposes={purpose},
            now=now,
        )
        if challenge is None or challenge.pending_secret is None:
            raise HTTPException(status_code=401, detail="Invalid or expired MFA challenge")
        if bound_user_email is not None and challenge.user_email != bound_user_email:
            raise HTTPException(status_code=403, detail="MFA challenge belongs to another user")
        if not await reserve_challenge_attempt(session, challenge.challenge_id, now=now):
            await session.commit()
            raise HTTPException(status_code=401, detail="MFA challenge attempts exhausted")
        if not await reserve_user_mfa_attempt(session, user_email=challenge.user_email, now=now):
            await session.commit()
            raise HTTPException(status_code=429, detail="Too many MFA attempts")
        cipher: MfaSecretCipher = request.app.state.mfa_cipher
        counter = matching_totp_counter(
            cipher.decrypt(challenge.pending_secret), payload.code, now=now
        )
        if counter is None:
            await session.commit()
            raise HTTPException(status_code=401, detail="Invalid MFA code")
        if not await consume_challenge(session, challenge.challenge_id, now=now):
            await session.rollback()
            raise HTTPException(status_code=401, detail="MFA challenge already used")
        recovery_codes = await activate_totp_factor(
            session,
            user_email=challenge.user_email,
            encrypted_secret=challenge.pending_secret,
            accepted_counter=counter,
            now=now,
        )
        if recovery_codes is None:
            await session.rollback()
            raise HTTPException(status_code=409, detail="TOTP is already enabled")
        await invalidate_other_setup_challenges(
            session,
            user_email=challenge.user_email,
            keep_challenge_id=challenge.challenge_id,
            now=now,
        )
        await clear_user_mfa_attempts(session, user_email=challenge.user_email)
        await revoke_user_auth_sessions(session, user_email=challenge.user_email)
        user = await get_user(session, challenge.user_email)
        if user is None or not user.is_active:
            await session.rollback()
            raise HTTPException(status_code=401, detail="Unknown or disabled user")
        if user.auth_version != challenge.auth_version + 1:
            await session.rollback()
            raise HTTPException(status_code=401, detail="MFA challenge was revoked")
        if challenge.purpose == "account_setup":
            await session.commit()
            await _disconnect_user_websockets(request, user.email)
            return MfaEnabledResponse(recovery_codes=recovery_codes)
        mode = cast(Literal["browser", "native"], challenge.login_mode)
        result = await _issue_authenticated_session(
            request,
            session,
            user=user,
            mode=mode,
            recovery_codes=recovery_codes,
        )
        await _disconnect_user_websockets(request, user.email)
        return result


@router.post(
    "/api/auth/mfa/verify",
    response_model=BrowserSessionResponse | NativeTokenResponse,
)
async def verify_login_mfa(
    request: Request, payload: MfaCodeRequest
) -> BrowserSessionResponse | NativeTokenResponse | Response:
    now = datetime.now(UTC)
    async with request.app.state.session_factory() as session:
        challenge = await get_live_challenge(
            session, payload.challenge_token, purposes={"login_verify"}, now=now
        )
        if challenge is None:
            raise HTTPException(status_code=401, detail="Invalid or expired MFA challenge")
        if not await reserve_challenge_attempt(session, challenge.challenge_id, now=now):
            await session.commit()
            raise HTTPException(status_code=401, detail="MFA challenge attempts exhausted")
        if not await reserve_user_mfa_attempt(session, user_email=challenge.user_email, now=now):
            await session.commit()
            raise HTTPException(status_code=429, detail="Too many MFA attempts")
        verified = await verify_totp_or_recovery(
            session,
            user_email=challenge.user_email,
            code=payload.code,
            cipher=request.app.state.mfa_cipher,
            now=now,
        )
        if not verified:
            await session.commit()
            raise HTTPException(status_code=401, detail="Invalid MFA code")
        if not await consume_challenge(session, challenge.challenge_id, now=now):
            await session.rollback()
            raise HTTPException(status_code=401, detail="MFA challenge already used")
        user = await get_user(session, challenge.user_email)
        if user is None or not user.is_active:
            await session.rollback()
            raise HTTPException(status_code=401, detail="Unknown or disabled user")
        if user.auth_version != challenge.auth_version:
            await session.rollback()
            raise HTTPException(status_code=401, detail="MFA challenge was revoked")
        await clear_user_mfa_attempts(session, user_email=challenge.user_email)
        mode = cast(Literal["browser", "native"], challenge.login_mode)
        return await _issue_authenticated_session(
            request,
            session,
            user=user,
            mode=mode,
        )


@router.post(
    "/api/auth/refresh",
    response_model=BrowserSessionResponse | NativeTokenResponse,
)
async def refresh(
    request: Request, payload: RefreshRequest | None = None
) -> BrowserSessionResponse | NativeTokenResponse:
    raw_token = request.cookies.get(COOKIE_NAME)

    app_state = request.app.state
    async with app_state.session_factory() as session:
        if payload and payload.mode == "native":
            if not payload.refresh_token:
                raise HTTPException(status_code=401, detail="Invalid refresh token")
            rotated = await rotate_native_session(
                session,
                refresh_token=payload.refresh_token,
                expires_at=_native_refresh_expiry(request),
            )
            if rotated.status in {"replay", "stale"}:
                await session.commit()
                raise HTTPException(status_code=401, detail="Invalid refresh token")
            if rotated.status == "invalid":
                raise HTTPException(status_code=401, detail="Invalid refresh token")
            if rotated.session is None or rotated.refresh_token is None:
                raise RuntimeError("Native session rotation returned no replacement")
            user = await get_user(session, rotated.session.user_email)
            if user is None:
                raise HTTPException(status_code=401, detail="Unknown user")
            if not user.is_active:
                raise HTTPException(status_code=403, detail="Account disabled")
            if rotated.session.auth_version != user.auth_version:
                await revoke_native_session_by_token(session, rotated.refresh_token)
                await session.commit()
                raise HTTPException(status_code=401, detail="Invalid refresh token")
            await session.commit()
            return _native_token_body(
                request,
                email=user.email,
                name=user.name,
                role=user.role,
                refresh_token=rotated.refresh_token,
                auth_version=user.auth_version,
            )
        if payload and payload.refresh_token:
            raise HTTPException(status_code=400, detail="Native refresh mode is required")

        if raw_token:
            browser_session = await get_browser_session_by_token(session, raw_token)
            if browser_session is None or browser_session.revoked_at is not None:
                raise HTTPException(status_code=401, detail="Invalid browser session")
            if browser_session.expires_at <= datetime.now(UTC):
                raise HTTPException(status_code=401, detail="Browser session expired")
            user = await get_user(session, browser_session.user_email)
        else:
            raise HTTPException(status_code=401, detail="No active browser session")

        if user is None:
            raise HTTPException(status_code=401, detail="Unknown user")
        if not user.is_active:
            raise HTTPException(status_code=403, detail="Account disabled")
        if browser_session.auth_version != user.auth_version:
            raise HTTPException(status_code=401, detail="Invalid browser session")

        refreshed_expiry = _browser_session_expiry(request)
        browser_session = await touch_browser_session(
            session, browser_session, expires_at=refreshed_expiry
        )
        await session.commit()
        body = BrowserSessionResponse(
            user={"email": user.email, "name": user.name, "role": user.role},
            expires_at=browser_session.expires_at,
        )
        response = JSONResponse(content=body.model_dump(mode="json"))
        _set_session_cookie(response, request, raw_token, _browser_session_max_age(request))
        return response  # type: ignore[return-value]


@router.post("/api/auth/logout")
async def logout(request: Request, payload: LogoutRequest | None = None) -> Response:
    claims = getattr(request.state, "claims", None)
    if claims is not None and (jti := claims.get("jti")) is not None:
        request.app.state.auth_provider.revoke_token(str(jti))
    if payload and payload.refresh_token:
        async with request.app.state.session_factory() as session:
            revoked = await revoke_native_session_by_token(session, payload.refresh_token)
            if not revoked:
                raise HTTPException(status_code=401, detail="Invalid refresh token")
            await session.commit()

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


@router.get("/api/auth/mfa", response_model=MfaStatusResponse)
async def mfa_status(request: Request) -> MfaStatusResponse:
    current = require_current_user(request)
    async with request.app.state.session_factory() as session:
        factor = await get_active_totp_factor(session, current.email)
        policy = await _mfa_policy(session)
        remaining = (
            await session.execute(
                select(func.count())
                .select_from(MfaRecoveryCode)
                .where(
                    MfaRecoveryCode.user_email == current.email,
                    MfaRecoveryCode.used_at.is_(None),
                )
            )
        ).scalar_one()
    return MfaStatusResponse(
        enabled=factor is not None,
        policy=policy,
        recovery_codes_remaining=int(remaining),
    )


@router.post("/api/auth/mfa/enroll/start", response_model=MfaSetupResponse)
async def start_account_mfa_setup(
    request: Request,
    payload: MfaEnrollmentStartRequest,
) -> MfaSetupResponse:
    current = require_session_user(request)
    cipher: MfaSecretCipher = request.app.state.mfa_cipher
    async with request.app.state.session_factory() as session:
        user = await get_user(session, current.email)
        if user is None or user.password_hash is None or not user.is_active:
            raise HTTPException(status_code=401, detail="Invalid credentials")
        _verify_mfa_current_password(request, user, payload.current_password)
        if await get_active_totp_factor(session, current.email) is not None:
            raise HTTPException(status_code=409, detail="TOTP is already enabled")
        secret = pyotp.random_base32()
        challenge = await create_mfa_challenge(
            session,
            user_email=current.email,
            purpose="account_setup",
            login_mode="browser",
            auth_version=user.auth_version,
            pending_secret=cipher.encrypt(secret),
        )
        await session.commit()
    return MfaSetupResponse(
        status="mfa_setup",
        challenge_token=challenge.token,
        secret=secret,
        provisioning_uri=pyotp.TOTP(secret).provisioning_uri(
            name=current.email,
            issuer_name="Cognis",
        ),
    )


async def _verify_mfa_management(
    request: Request,
    session: AsyncSession,
    payload: MfaManageRequest,
) -> Any:
    current = require_session_user(request)
    user = await get_user(session, current.email)
    if user is None or user.password_hash is None or not user.is_active:
        raise HTTPException(status_code=401, detail="Invalid credentials")
    _verify_mfa_current_password(request, user, payload.current_password)
    if not await reserve_user_mfa_attempt(session, user_email=user.email):
        await session.commit()
        raise HTTPException(status_code=429, detail="Too many MFA attempts")
    if not await verify_totp_or_recovery(
        session,
        user_email=user.email,
        code=payload.code,
        cipher=request.app.state.mfa_cipher,
    ):
        await session.commit()
        raise HTTPException(status_code=401, detail="Invalid MFA code")
    await clear_user_mfa_attempts(session, user_email=user.email)
    return user


def _revoke_presented_access_token(request: Request) -> None:
    claims = getattr(request.state, "claims", None)
    if claims is not None and (jti := claims.get("jti")) is not None:
        request.app.state.auth_provider.revoke_token(str(jti))


@router.post("/api/auth/mfa/disable")
async def disable_mfa(request: Request, payload: MfaManageRequest) -> dict[str, bool]:
    async with request.app.state.session_factory() as session:
        if await _mfa_policy(session) == "required":
            raise HTTPException(status_code=409, detail="MFA is required by system policy")
        user = await _verify_mfa_management(request, session, payload)
        await session.execute(delete(MfaChallenge).where(MfaChallenge.user_email == user.email))
        await session.execute(
            delete(MfaRecoveryCode).where(MfaRecoveryCode.user_email == user.email)
        )
        await session.execute(delete(UserTotpFactor).where(UserTotpFactor.user_email == user.email))
        await revoke_user_auth_sessions(session, user_email=user.email)
        await session.commit()
    await _disconnect_user_websockets(request, user.email)
    _revoke_presented_access_token(request)
    return {"ok": True}


@router.post(
    "/api/auth/mfa/recovery-codes/regenerate",
    response_model=MfaRecoveryCodesResponse,
)
async def regenerate_mfa_recovery_codes(
    request: Request, payload: MfaManageRequest
) -> MfaRecoveryCodesResponse:
    async with request.app.state.session_factory() as session:
        user = await _verify_mfa_management(request, session, payload)
        codes = await replace_recovery_codes(session, user_email=user.email)
        await revoke_user_auth_sessions(session, user_email=user.email)
        await session.commit()
    await _disconnect_user_websockets(request, user.email)
    _revoke_presented_access_token(request)
    return MfaRecoveryCodesResponse(recovery_codes=codes)


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
        await revoke_user_auth_sessions(session, user_email=user.email)
        await session.commit()

    await _disconnect_user_websockets(request, user.email)
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
    config = request.app.state.config
    ui_url = config.public_intaris_ui_url if target == "intaris" else config.public_mnemory_ui_url
    return ExchangeTokenResponse(token=token, target=target, expires_in=60, ui_url=ui_url)
