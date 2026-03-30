"""HTTP authentication middleware."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime

from fastapi import Request
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import Response

from cognis.api.models import ErrorBody, ErrorResponse
from cognis.runtime_context import current_user_email
from cognis.security import parse_api_key, verify_api_key
from cognis.store.queries import get_api_key, get_user, touch_api_key_last_used

PUBLIC_ROUTES = {
    ("GET", "/api/bootstrap-status"),
    ("POST", "/api/setup"),
    ("GET", "/.well-known/jwks.json"),
    ("GET", "/api/health"),
    ("GET", "/api/health/providers"),
    ("GET", "/api/metrics"),
    ("POST", "/api/auth/login"),
    ("POST", "/api/auth/refresh"),
    ("GET", "/.well-known/agent.json"),
}


@dataclass
class AuthenticatedUser:
    email: str
    role: str
    name: str | None = None
    auth_type: str = "jwt"


class AuthenticationMiddleware(BaseHTTPMiddleware):
    """Authenticate all /api/* routes by default."""

    async def dispatch(
        self,
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        if not request.url.path.startswith("/api/"):
            return await call_next(request)
        if (request.method.upper(), request.url.path) in PUBLIC_ROUTES:
            return await call_next(request)

        app_state = request.app.state
        auth_provider = app_state.auth_provider
        password_hasher = app_state.password_hasher
        session_factory = app_state.session_factory
        api_rate_limiter = getattr(app_state, "api_rate_limiter", None)

        authorization = request.headers.get("Authorization")
        api_key_header = request.headers.get("X-API-Key")
        cookie_token = request.cookies.get("cognis_session")

        if authorization and authorization.startswith("Bearer "):
            token = authorization.removeprefix("Bearer ").strip()
            try:
                claims = auth_provider.verify_jwt(token, audience=["cognis"])
            except Exception:
                return JSONResponse(
                    status_code=401,
                    content=ErrorResponse(
                        error=ErrorBody(code="unauthorized", message="Invalid or expired token")
                    ).model_dump(),
                )
            context_token = current_user_email.set(str(claims["sub"]))
            request.state.user = AuthenticatedUser(
                email=str(claims["sub"]),
                role=str(claims.get("role", "user")),
                name=claims.get("name"),
                auth_type="jwt",
            )
            request.state.claims = claims
            if api_rate_limiter is not None and not await api_rate_limiter.allow(
                user_key=str(claims["sub"]),
                path=request.url.path,
                method=request.method,
            ):
                return JSONResponse(
                    status_code=429,
                    content=ErrorResponse(
                        error=ErrorBody(code="rate_limited", message="API rate limit exceeded")
                    ).model_dump(),
                )
            try:
                return await call_next(request)
            finally:
                current_user_email.reset(context_token)

        if api_key_header:
            parsed = parse_api_key(api_key_header)
            if parsed is None:
                return JSONResponse(
                    status_code=401,
                    content=ErrorResponse(
                        error=ErrorBody(code="unauthorized", message="Invalid API key")
                    ).model_dump(),
                )
            key_id, _ = parsed
            async with session_factory() as session:
                record = await get_api_key(session, key_id)
                if record is None or not verify_api_key(
                    password_hasher, api_key_header, record.key_hash
                ):
                    return JSONResponse(
                        status_code=401,
                        content=ErrorResponse(
                            error=ErrorBody(code="unauthorized", message="Invalid API key")
                        ).model_dump(),
                    )
                expires_at = record.expires_at
                if expires_at is not None and expires_at.tzinfo is None:
                    expires_at = expires_at.replace(tzinfo=UTC)
                if expires_at is not None and expires_at < datetime.now(UTC):
                    return JSONResponse(
                        status_code=401,
                        content=ErrorResponse(
                            error=ErrorBody(code="unauthorized", message="Expired API key")
                        ).model_dump(),
                    )
                user = await get_user(session, record.user_email)
                if user is None:
                    return JSONResponse(
                        status_code=401,
                        content=ErrorResponse(
                            error=ErrorBody(code="unauthorized", message="Unknown API key owner")
                        ).model_dump(),
                    )
                await touch_api_key_last_used(session, record.key_id)
                await session.commit()
                request.state.user = AuthenticatedUser(
                    email=user.email, role=user.role, name=user.name, auth_type="api_key"
                )
                context_token = current_user_email.set(user.email)
                if api_rate_limiter is not None and not await api_rate_limiter.allow(
                    user_key=user.email,
                    path=request.url.path,
                    method=request.method,
                ):
                    return JSONResponse(
                        status_code=429,
                        content=ErrorResponse(
                            error=ErrorBody(code="rate_limited", message="API rate limit exceeded")
                        ).model_dump(),
                    )
                try:
                    return await call_next(request)
                finally:
                    current_user_email.reset(context_token)

        # Fallback: check cognis_session cookie (cross-service SSO)
        if cookie_token:
            try:
                claims = auth_provider.verify_jwt(cookie_token, audience=["cognis"])
            except Exception:
                return JSONResponse(
                    status_code=401,
                    content=ErrorResponse(
                        error=ErrorBody(code="unauthorized", message="Invalid session cookie")
                    ).model_dump(),
                )
            context_token = current_user_email.set(str(claims["sub"]))
            request.state.user = AuthenticatedUser(
                email=str(claims["sub"]),
                role=str(claims.get("role", "user")),
                name=claims.get("name"),
                auth_type="cookie",
            )
            request.state.claims = claims
            if api_rate_limiter is not None and not await api_rate_limiter.allow(
                user_key=str(claims["sub"]),
                path=request.url.path,
                method=request.method,
            ):
                return JSONResponse(
                    status_code=429,
                    content=ErrorResponse(
                        error=ErrorBody(code="rate_limited", message="API rate limit exceeded")
                    ).model_dump(),
                )
            try:
                return await call_next(request)
            finally:
                current_user_email.reset(context_token)

        return JSONResponse(
            status_code=401,
            content=ErrorResponse(
                error=ErrorBody(code="unauthorized", message="Missing authentication credentials")
            ).model_dump(),
        )
