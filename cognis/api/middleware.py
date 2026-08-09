"""HTTP authentication middleware."""

from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import json
import mmap
import re
import tempfile
import time
import weakref
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from email.message import Message as EmailMessage
from email.parser import BytesHeaderParser
from typing import Any

from argon2 import PasswordHasher
from fastapi import Request
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import Response
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from cognis.api.models import ErrorBody, ErrorResponse
from cognis.runtime_context import current_user_email
from cognis.security import parse_api_key, verify_api_key
from cognis.store.queries import (
    get_api_key,
    get_browser_session_by_token,
    get_user,
    register_auth_cache_invalidator,
    touch_api_key_last_used,
)


class _KnowledgebaseUploadRejected(RuntimeError):
    def __init__(self, status_code: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message


class KnowledgebaseDocumentUploadLimitMiddleware:
    """Bound document multipart bodies before Starlette parses or spools them."""

    _PATH = re.compile(r"^/api/v1/knowledgebases/[^/]+/documents$")
    _MAX_PART_HEADER_BYTES = 16 * 1024

    def __init__(
        self,
        app: ASGIApp,
        *,
        max_body_bytes: int,
        max_files: int = 25,
        max_parts: int = 52,
    ) -> None:
        self.app = app
        self.max_body_bytes = max_body_bytes
        self.max_files = max_files
        self.max_parts = max_parts

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if (
            scope["type"] != "http"
            or scope.get("method") != "POST"
            or self._PATH.fullmatch(str(scope.get("path") or "")) is None
        ):
            await self.app(scope, receive, send)
            return
        headers = {key.lower(): value for key, value in scope.get("headers", [])}
        if not headers.get(b"content-type", b"").lower().startswith(b"multipart/form-data"):
            await self.app(scope, receive, send)
            return
        raw_length = headers.get(b"content-length")
        if raw_length is not None:
            try:
                content_length = int(raw_length)
            except ValueError:
                await self._reject(send, 400, "validation_error", "Invalid Content-Length")
                return
            if content_length > self.max_body_bytes:
                await self._reject(
                    send,
                    413,
                    "content_too_large",
                    "Knowledgebase document upload body exceeds size limit",
                )
                return

        spool = tempfile.SpooledTemporaryFile(max_size=1024 * 1024)  # noqa: SIM115
        try:
            received = 0
            while True:
                message = await receive()
                if message["type"] != "http.request":
                    raise _KnowledgebaseUploadRejected(
                        400, "validation_error", "Incomplete multipart request body"
                    )
                body = message.get("body", b"")
                received += len(body)
                if received > self.max_body_bytes:
                    raise _KnowledgebaseUploadRejected(
                        413,
                        "content_too_large",
                        "Knowledgebase document upload body exceeds size limit",
                    )
                spool.write(body)
                if not message.get("more_body", False):
                    break
            self._validate_multipart(
                spool,
                content_type=headers[b"content-type"],
            )
            spool.seek(0)

            async def replay_receive() -> Message:
                body = spool.read(1024 * 1024)
                if body:
                    return {
                        "type": "http.request",
                        "body": body,
                        "more_body": spool.tell() < received,
                    }
                return {"type": "http.request", "body": b"", "more_body": False}

            await self.app(scope, replay_receive, send)
        except _KnowledgebaseUploadRejected as exc:
            await self._reject(send, exc.status_code, exc.code, exc.message)
        finally:
            spool.close()

    def _validate_multipart(self, spool: Any, *, content_type: bytes) -> None:
        message = EmailMessage()
        message["content-type"] = content_type.decode("latin-1")
        boundary_value = message.get_param("boundary", header="content-type")
        if not isinstance(boundary_value, str) or not boundary_value:
            raise _KnowledgebaseUploadRejected(
                400, "validation_error", "Multipart boundary is missing"
            )
        boundary = b"--" + boundary_value.encode("latin-1")
        final_boundary = boundary + b"--"
        parts = 0
        files = 0
        spool.flush()
        if spool.tell() == 0:
            raise _KnowledgebaseUploadRejected(
                400, "validation_error", "Multipart request body is empty"
            )
        with mmap.mmap(spool.fileno(), 0, access=mmap.ACCESS_READ) as body:
            position = 0
            while True:
                position = body.find(boundary, position)
                if position < 0:
                    break
                before_valid = position == 0 or body[position - 2 : position] == b"\r\n"
                after = position + len(boundary)
                if not before_valid or body[after : after + 2] not in {b"\r\n", b"--"}:
                    position = after
                    continue
                if body[position : position + len(final_boundary)] == final_boundary:
                    break
                header_start = after + 2
                header_end = body.find(b"\r\n\r\n", header_start)
                if header_end < 0 or header_end - header_start > self._MAX_PART_HEADER_BYTES:
                    raise _KnowledgebaseUploadRejected(
                        400, "validation_error", "Multipart part header is too large"
                    )
                headers = BytesHeaderParser().parsebytes(body[header_start:header_end])
                disposition = headers.get("content-disposition")
                if disposition is None:
                    raise _KnowledgebaseUploadRejected(
                        400,
                        "validation_error",
                        "Multipart part is missing disposition",
                    )
                parts += 1
                disposition_message = EmailMessage()
                disposition_message["content-disposition"] = disposition
                if (
                    disposition_message.get_param("name", header="content-disposition") == "files[]"
                    and disposition_message.get_param("filename", header="content-disposition")
                    is not None
                ):
                    files += 1
                if parts > self.max_parts or files > self.max_files:
                    raise _KnowledgebaseUploadRejected(
                        400,
                        "validation_error",
                        "Knowledgebase document multipart contains too many parts",
                    )
                position = header_end + 4

    @staticmethod
    async def _reject(send: Send, status_code: int, code: str, message: str) -> None:
        body = json.dumps(
            {"error": {"code": code, "message": message, "details": None}},
            separators=(",", ":"),
        ).encode()
        await send(
            {
                "type": "http.response.start",
                "status": status_code,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(body)).encode()),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})


PUBLIC_ROUTES = {
    ("GET", "/api/bootstrap-status"),
    ("POST", "/api/setup"),
    ("GET", "/.well-known/jwks.json"),
    ("GET", "/.well-known/cognis-client.json"),
    ("GET", "/api/health"),
    ("GET", "/api/health/providers"),
    ("GET", "/api/livez"),
    ("GET", "/api/readyz"),
    ("GET", "/api/metrics"),
    ("GET", "/api/v1/pwa-reset"),
    ("POST", "/api/auth/login"),
    ("POST", "/api/auth/refresh"),
    ("GET", "/api/v1/mcp/oauth/callback"),
    ("GET", "/.well-known/agent.json"),
}

AUTH_LOOKUP_CACHE_TTL_SECONDS = 45.0
API_KEY_TOUCH_DEBOUNCE_SECONDS = 60.0


@dataclass
class AuthenticatedUser:
    email: str
    role: str
    name: str | None = None
    auth_type: str = "jwt"


@dataclass(frozen=True)
class _CachedApiKeyIdentity:
    key_id: str
    user_email: str
    role: str
    name: str | None
    expires_at: datetime | None


class _AuthLookupCache:
    """Small in-process auth cache with bounded stale-positive windows."""

    def __init__(self, ttl_seconds: float = AUTH_LOOKUP_CACHE_TTL_SECONDS) -> None:
        self._ttl_seconds = ttl_seconds
        self._api_keys: dict[str, tuple[float, _CachedApiKeyIdentity]] = {}
        self._user_active: dict[str, tuple[float, bool | None]] = {}
        self._api_key_touches: dict[str, float] = {}

    def get_api_key_identity(self, key_hash: str, now: datetime) -> _CachedApiKeyIdentity | None:
        cached = self._api_keys.get(key_hash)
        monotonic_now = time.monotonic()
        if cached is None:
            return None
        expires_at, identity = cached
        if expires_at <= monotonic_now:
            self._api_keys.pop(key_hash, None)
            return None
        if _datetime_expired(identity.expires_at, now):
            self._api_keys.pop(key_hash, None)
            return None
        return identity

    def put_api_key_identity(self, key_hash: str, identity: _CachedApiKeyIdentity) -> None:
        self._api_keys[key_hash] = (time.monotonic() + self._ttl_seconds, identity)

    def get_user_active(self, email: str) -> tuple[bool, bool | None]:
        cached = self._user_active.get(email)
        monotonic_now = time.monotonic()
        if cached is None:
            return False, None
        expires_at, is_active = cached
        if expires_at <= monotonic_now:
            self._user_active.pop(email, None)
            return False, None
        return True, is_active

    def put_user_active(self, email: str, is_active: bool | None) -> None:
        self._user_active[email] = (time.monotonic() + self._ttl_seconds, is_active)

    def should_touch_api_key(self, key_id: str) -> bool:
        monotonic_now = time.monotonic()
        last_touch = self._api_key_touches.get(key_id)
        if last_touch is not None and (monotonic_now - last_touch < API_KEY_TOUCH_DEBOUNCE_SECONDS):
            return False
        self._api_key_touches[key_id] = monotonic_now
        return True

    def invalidate_user(self, email: str) -> None:
        self._user_active.pop(email, None)
        for key_hash, (_, identity) in list(self._api_keys.items()):
            if identity.user_email == email:
                self._api_keys.pop(key_hash, None)
                self._api_key_touches.pop(identity.key_id, None)

    def invalidate_api_key(self, key_id: str) -> None:
        for key_hash, (_, identity) in list(self._api_keys.items()):
            if identity.key_id == key_id:
                self._api_keys.pop(key_hash, None)
        self._api_key_touches.pop(key_id, None)


_AUTH_CACHES: weakref.WeakSet[_AuthLookupCache] = weakref.WeakSet()
_AUTH_CACHE_INVALIDATOR_REGISTERED = False
_PUBLIC_DELIVERABLE_SHARE_PATH = re.compile(
    r"/api/v1/deliverables/(?:s/[A-Za-z0-9_-]+|share/[A-Za-z0-9_-]+/"
    r"(?:view|download\.pdf|media/media_[0-9a-f]{24}))"
)
_PUBLIC_STANDALONE_ASSET_PATH = re.compile(
    r"/api/v1/deliverables/standalone-assets/assets/[A-Za-z0-9_./-]+"
)


def _hash_api_key(api_key: str) -> str:
    return hashlib.sha256(api_key.encode("utf-8")).hexdigest()


def _public_deliverable_share_token(path: str) -> str | None:
    if _PUBLIC_DELIVERABLE_SHARE_PATH.fullmatch(path) is None:
        return None
    if "/s/" in path:
        token = path.rsplit("/", 1)[-1]
    else:
        token = path.split("/share/", 1)[1].split("/", 1)[0]
    if token in {".", ".."} or ".." in token:
        return None
    return token


def _public_share_client_ip(request: Request) -> str:
    client_host = request.client.host if request.client is not None else "unknown"
    try:
        peer_ip = ipaddress.ip_address(client_host)
    except ValueError:
        return client_host
    config = getattr(request.app.state, "config", None)
    trusted_cidrs = getattr(config, "trusted_proxy_cidrs", ())
    trusted_networks = tuple(ipaddress.ip_network(cidr, strict=False) for cidr in trusted_cidrs)
    if not any(peer_ip in network for network in trusted_networks):
        return str(peer_ip)
    try:
        forwarded_ips = [
            ipaddress.ip_address(value.strip())
            for value in request.headers.get("x-forwarded-for", "").split(",")
            if value.strip()
        ]
    except ValueError:
        return str(peer_ip)
    if not forwarded_ips:
        return str(peer_ip)
    for candidate in reversed(forwarded_ips):
        if not any(candidate in network for network in trusted_networks):
            return str(candidate)
    return str(forwarded_ips[0])


def _public_share_rate_key(client_ip: str, token: str) -> str:
    token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
    return f"public-share:{client_ip}:{token_hash}"


def _public_share_rate_limited_response() -> JSONResponse:
    return JSONResponse(
        status_code=429,
        content=ErrorResponse(
            error=ErrorBody(
                code="rate_limited",
                message="Public deliverable share rate limit exceeded",
            )
        ).model_dump(),
    )


def _datetime_expired(value: datetime | None, now: datetime) -> bool:
    if value is None:
        return False
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value < now


async def _verify_api_key_async(
    password_hasher: PasswordHasher, api_key: str, key_hash: str
) -> bool:
    return await asyncio.to_thread(verify_api_key, password_hasher, api_key, key_hash)


def _invalidate_auth_caches(kind: str, identifier: str) -> None:
    for cache in list(_AUTH_CACHES):
        if kind == "user":
            cache.invalidate_user(identifier)
        elif kind == "api_key":
            cache.invalidate_api_key(identifier)


def _is_public_deliverable_share_route(request: Request) -> bool:
    """Match only canonical, unencoded signed deliverable GET paths."""

    if request.method.upper() != "GET":
        return False
    raw_path = request.scope.get("raw_path")
    if not isinstance(raw_path, bytes):
        return False
    try:
        raw_path_text = raw_path.decode("ascii")
    except UnicodeDecodeError:
        return False
    path = request.url.path
    return raw_path_text == path and _PUBLIC_DELIVERABLE_SHARE_PATH.fullmatch(path) is not None


class AuthenticationMiddleware(BaseHTTPMiddleware):
    """Authenticate all /api/* routes by default."""

    def __init__(self, app: ASGIApp) -> None:
        super().__init__(app)
        self._auth_cache = _AuthLookupCache()
        _AUTH_CACHES.add(self._auth_cache)
        global _AUTH_CACHE_INVALIDATOR_REGISTERED
        if not _AUTH_CACHE_INVALIDATOR_REGISTERED:
            register_auth_cache_invalidator(_invalidate_auth_caches)
            _AUTH_CACHE_INVALIDATOR_REGISTERED = True

    async def dispatch(
        self,
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        if not request.url.path.startswith("/api/"):
            return await call_next(request)
        if (request.method.upper(), request.url.path) in PUBLIC_ROUTES:
            return await call_next(request)
        if (
            request.method.upper() == "GET"
            and _PUBLIC_STANDALONE_ASSET_PATH.fullmatch(request.url.path) is not None
        ):
            return await call_next(request)
        public_share_token = (
            _public_deliverable_share_token(request.url.path)
            if _is_public_deliverable_share_route(request)
            else None
        )
        if public_share_token is not None:
            client_ip = _public_share_client_ip(request)
            public_share_client_rate_limiter = getattr(
                request.app.state, "public_share_client_rate_limiter", None
            )
            if (
                public_share_client_rate_limiter is not None
                and not await public_share_client_rate_limiter.allow(
                    user_key=f"public-share-client:{client_ip}",
                    path=request.url.path,
                    method=request.method,
                )
            ):
                return _public_share_rate_limited_response()
            public_share_rate_limiter = getattr(
                request.app.state, "public_share_rate_limiter", None
            )
            if public_share_rate_limiter is not None and not await public_share_rate_limiter.allow(
                user_key=_public_share_rate_key(client_ip, public_share_token),
                path=request.url.path,
                method=request.method,
            ):
                return _public_share_rate_limited_response()
            return await call_next(request)
        if request.method.upper() == "GET" and request.url.path.startswith(
            "/api/v1/artifacts/content/"
        ):
            return await call_next(request)
        if request.method.upper() == "GET" and request.url.path.startswith(
            "/api/v1/artifacts/view/"
        ):
            return await call_next(request)
        if request.method.upper() == "GET" and request.url.path.startswith(
            "/api/v1/artifacts/virtual/deliverables/"
        ):
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
            # Check if user is disabled (JWT may have been issued before disable).
            # Positive active-user cache entries are invalidated by in-process
            # query hooks; other processes rely on the short TTL bound.
            user_email = str(claims["sub"])
            cached_user_state, is_active = self._auth_cache.get_user_active(user_email)
            if not cached_user_state:
                async with session_factory() as session:
                    user_row = await get_user(session, user_email)
                    is_active = None if user_row is None else user_row.is_active
                    self._auth_cache.put_user_active(user_email, is_active)
            if is_active is False:
                return JSONResponse(
                    status_code=403,
                    content=ErrorResponse(
                        error=ErrorBody(code="account_disabled", message="Account disabled")
                    ).model_dump(),
                )
            context_token = current_user_email.set(user_email)
            request.state.user = AuthenticatedUser(
                email=user_email,
                role=str(claims.get("role", "user")),
                name=claims.get("name"),
                auth_type="jwt",
            )
            request.state.claims = claims
            if api_rate_limiter is not None and not await api_rate_limiter.allow(
                user_key=user_email,
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
            now = datetime.now(UTC)
            full_key_hash = _hash_api_key(api_key_header)
            identity = self._auth_cache.get_api_key_identity(full_key_hash, now)
            if identity is None:
                async with session_factory() as session:
                    record = await get_api_key(session, key_id)
                    if record is None or not await _verify_api_key_async(
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
                    if _datetime_expired(expires_at, now):
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
                                error=ErrorBody(
                                    code="unauthorized", message="Unknown API key owner"
                                )
                            ).model_dump(),
                        )
                    self._auth_cache.put_user_active(user.email, user.is_active)
                    if not user.is_active:
                        return JSONResponse(
                            status_code=403,
                            content=ErrorResponse(
                                error=ErrorBody(code="account_disabled", message="Account disabled")
                            ).model_dump(),
                        )
                    identity = _CachedApiKeyIdentity(
                        key_id=record.key_id,
                        user_email=user.email,
                        role=user.role,
                        name=user.name,
                        expires_at=expires_at,
                    )
                    self._auth_cache.put_api_key_identity(full_key_hash, identity)
                    if self._auth_cache.should_touch_api_key(record.key_id):
                        await touch_api_key_last_used(session, record.key_id)
                        await session.commit()
            elif self._auth_cache.should_touch_api_key(identity.key_id):
                async with session_factory() as session:
                    touched = await touch_api_key_last_used(session, identity.key_id)
                    if not touched:
                        self._auth_cache.invalidate_api_key(identity.key_id)
                        return JSONResponse(
                            status_code=401,
                            content=ErrorResponse(
                                error=ErrorBody(code="unauthorized", message="Invalid API key")
                            ).model_dump(),
                        )
                    await session.commit()
            request.state.user = AuthenticatedUser(
                email=identity.user_email,
                role=identity.role,
                name=identity.name,
                auth_type="api_key",
            )
            context_token = current_user_email.set(identity.user_email)
            if api_rate_limiter is not None and not await api_rate_limiter.allow(
                user_key=identity.user_email,
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

        # Fallback: check opaque browser session cookie.
        if cookie_token:
            async with session_factory() as session:
                browser_session = await get_browser_session_by_token(session, cookie_token)
                if browser_session is None or browser_session.revoked_at is not None:
                    return JSONResponse(
                        status_code=401,
                        content=ErrorResponse(
                            error=ErrorBody(code="unauthorized", message="Invalid session cookie")
                        ).model_dump(),
                    )
                expires_at = browser_session.expires_at
                if expires_at.tzinfo is None:
                    expires_at = expires_at.replace(tzinfo=UTC)
                if expires_at < datetime.now(UTC):
                    return JSONResponse(
                        status_code=401,
                        content=ErrorResponse(
                            error=ErrorBody(code="unauthorized", message="Session expired")
                        ).model_dump(),
                    )
                user_row = await get_user(session, browser_session.user_email)
                if user_row is not None and not user_row.is_active:
                    return JSONResponse(
                        status_code=403,
                        content=ErrorResponse(
                            error=ErrorBody(code="account_disabled", message="Account disabled")
                        ).model_dump(),
                    )
                if user_row is None:
                    return JSONResponse(
                        status_code=401,
                        content=ErrorResponse(
                            error=ErrorBody(code="unauthorized", message="Unknown session owner")
                        ).model_dump(),
                    )
            context_token = current_user_email.set(user_row.email)
            request.state.user = AuthenticatedUser(
                email=user_row.email,
                role=user_row.role,
                name=user_row.name,
                auth_type="session",
            )
            request.state.browser_session_id = browser_session.session_id
            if api_rate_limiter is not None and not await api_rate_limiter.allow(
                user_key=user_row.email,
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
