"""OAuth 2.1 helpers for HTTP MCP servers.

This module deliberately keeps OAuth tokens controller-side. Executors only
receive short-lived bearer access tokens injected into MCP HTTP headers.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import socket
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from ipaddress import ip_address
from typing import Any
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit

import httpx
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from cognis.core.notifications import NotificationService, NotificationType
from cognis.logging import get_logger
from cognis.models.tool import effective_mcp_auth_config
from cognis.store.models import MCPServerRow
from cognis.store.queries import (
    create_mcp_oauth_transaction,
    get_mcp_oauth_token,
    get_mcp_oauth_transaction,
    get_mcp_server,
    mark_mcp_oauth_token_status,
    mcp_oauth_resource_key,
    upsert_mcp_oauth_token,
)

logger = get_logger(__name__)

_METADATA_TIMEOUT = 5.0
_TOKEN_TIMEOUT = 10.0
_STATE_TTL_SECONDS = 15 * 60
_MAX_METADATA_BYTES = 128 * 1024
_REFRESH_SKEW_SECONDS = 60
_DYNAMIC_CLIENT_NAME = "Cognis MCP"
_RESERVED_AUTHORIZATION_PARAMS = {
    "client_id",
    "code_challenge",
    "code_challenge_method",
    "redirect_uri",
    "response_type",
    "state",
}


class MCPOAuthError(RuntimeError):
    """Safe OAuth error for user-visible/API paths."""


@dataclass(frozen=True)
class AuthorizationStart:
    authorization_url: str
    transaction_id: str
    expires_at: datetime
    issuer: str
    authorization_server: str
    scopes: list[str]


@dataclass(frozen=True)
class OAuthClientRegistration:
    client_id: str
    client_secret: str | None = None


@dataclass(frozen=True)
class TokenInjectionResult:
    headers: dict[str, str]
    authorization_required: bool = False
    reason: str | None = None
    authorization_url: str | None = None
    transaction_id: str | None = None


@dataclass(frozen=True)
class AuthorizationChallengeContext:
    """Runtime routing context for an OAuth authorization challenge."""

    user_email: str
    server: MCPServerRow
    conversation_id: str | None = None
    task_id: str | None = None
    step_name: str | None = None
    step_run_id: str | None = None
    session_id: str | None = None
    delivery_mode: str | None = "silent"


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _safe_url(url: str, *, allow_http_localhost: bool = True) -> str:
    parsed = urlsplit(url)
    if parsed.scheme not in {"https", "http"}:
        raise MCPOAuthError("OAuth endpoints must use https")
    host = parsed.hostname
    if not host:
        raise MCPOAuthError("OAuth endpoint host is required")
    localhost = host in {"localhost", "127.0.0.1", "::1"}
    if parsed.scheme != "https" and not (allow_http_localhost and localhost):
        raise MCPOAuthError("OAuth endpoints must use https except localhost development URLs")
    resolved_ips = []
    try:
        ip = ip_address(host)
    except ValueError:
        ip = None
        if not localhost:
            try:
                resolved_ips = [
                    ip_address(info[4][0])
                    for info in socket.getaddrinfo(
                        host, parsed.port or 443, type=socket.SOCK_STREAM
                    )
                ]
            except OSError as exc:
                raise MCPOAuthError("OAuth endpoint host could not be resolved") from exc
    else:
        resolved_ips = [ip]
    if any(
        not candidate.is_loopback
        and (
            candidate.is_private
            or candidate.is_link_local
            or candidate.is_reserved
            or candidate.is_multicast
        )
        for candidate in resolved_ips
    ):
        raise MCPOAuthError("OAuth endpoints cannot target private or link-local addresses")
    if any(candidate.is_loopback for candidate in resolved_ips) and not localhost:
        raise MCPOAuthError("OAuth endpoints cannot target loopback aliases")
    return url


def parse_www_authenticate(value: str | None) -> dict[str, str]:
    """Parse a Bearer WWW-Authenticate challenge into lowercase parameters."""

    if not value:
        return {}
    challenge = value.strip()
    if challenge.lower().startswith("bearer "):
        challenge = challenge[7:]
    params: dict[str, str] = {}
    for item in parse_qsl(challenge.replace(",", "&"), keep_blank_values=True):
        params[item[0].strip().lower()] = item[1].strip().strip('"')
    return params


class MCPOAuthService:
    """Controller-side MCP OAuth service."""

    def __init__(
        self,
        *,
        session_factory: Any,
        key_path: str,
        public_base_url: str,
        notification_service: NotificationService | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._public_base_url = public_base_url.rstrip("/")
        self._notification_service = notification_service
        with open(key_path, "rb") as key_file:
            self._key = base64.urlsafe_b64decode(key_file.read())

    def _encrypt(self, payload: dict[str, Any]) -> bytes:
        nonce = os.urandom(12)
        plaintext = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
        return nonce + AESGCM(self._key).encrypt(nonce, plaintext, None)

    def _decrypt(self, data: bytes) -> dict[str, Any]:
        nonce, ciphertext = data[:12], data[12:]
        payload = json.loads(AESGCM(self._key).decrypt(nonce, ciphertext, None).decode())
        return payload if isinstance(payload, dict) else {}

    def redirect_uri(self) -> str:
        if not self._public_base_url:
            raise MCPOAuthError("COGNIS_PUBLIC_BASE_URL is required for MCP OAuth")
        return f"{self._public_base_url}/api/v1/mcp/oauth/callback"

    async def discover_metadata(self, server: MCPServerRow) -> dict[str, Any]:
        auth_config = effective_mcp_auth_config(server.auth_config, server.headers)
        issuer = auth_config.issuer or auth_config.authorization_server
        if not issuer:
            if not server.url:
                raise MCPOAuthError("MCP OAuth server URL is required for discovery")
            issuer = await self._discover_issuer_from_resource(server.url)
        issuer = _safe_url(issuer.rstrip("/"))
        metadata = await self._fetch_json(f"{issuer}/.well-known/oauth-authorization-server")
        if not metadata:
            metadata = await self._fetch_json(f"{issuer}/.well-known/openid-configuration")
        if not metadata:
            raise MCPOAuthError("OAuth authorization server metadata not found")
        metadata_issuer = str(metadata.get("issuer") or issuer).rstrip("/")
        if metadata_issuer != issuer.rstrip("/"):
            raise MCPOAuthError("OAuth issuer metadata mismatch")
        metadata["issuer"] = metadata_issuer
        return metadata

    async def _discover_issuer_from_resource(self, resource_url: str) -> str:
        parsed = urlsplit(_safe_url(resource_url))
        base = f"{parsed.scheme}://{parsed.netloc}"
        resource_metadata = await self._fetch_json(
            urljoin(base, "/.well-known/oauth-protected-resource")
        )
        if resource_metadata:
            auth_servers = resource_metadata.get("authorization_servers")
            if isinstance(auth_servers, list) and auth_servers:
                return str(auth_servers[0])
        async with httpx.AsyncClient(
            timeout=_METADATA_TIMEOUT, follow_redirects=False, max_redirects=2
        ) as client:
            response = await client.get(resource_url)
        challenge = parse_www_authenticate(response.headers.get("www-authenticate"))
        issuer = challenge.get("authorization_uri") or challenge.get("issuer")
        if not issuer:
            raise MCPOAuthError("OAuth authorization server could not be discovered")
        return issuer

    async def _fetch_json(self, url: str) -> dict[str, Any] | None:
        current_url = _safe_url(url)
        async with httpx.AsyncClient(timeout=_METADATA_TIMEOUT, follow_redirects=False) as client:
            for _ in range(3):
                response = await client.get(current_url)
                if response.status_code not in {301, 302, 303, 307, 308}:
                    break
                location = response.headers.get("location")
                if not location:
                    raise MCPOAuthError("OAuth metadata redirect missing location")
                current_url = _safe_url(urljoin(current_url, location))
            else:
                raise MCPOAuthError("OAuth metadata redirects exceeded limit")
        if response.status_code == 404:
            return None
        response.raise_for_status()
        if len(response.content) > _MAX_METADATA_BYTES:
            raise MCPOAuthError("OAuth metadata response too large")
        data = response.json()
        return data if isinstance(data, dict) else None

    async def _resolve_client_registration(
        self,
        *,
        metadata: dict[str, Any],
        configured_client_id: str | None,
        configured_client_secret: str | None,
        redirect_uri: str,
        scopes: list[str],
        client_metadata_document_url: str | None,
    ) -> OAuthClientRegistration:
        if configured_client_id:
            return OAuthClientRegistration(
                client_id=configured_client_id,
                client_secret=configured_client_secret,
            )
        registration_endpoint = metadata.get("registration_endpoint")
        if not isinstance(registration_endpoint, str) or not registration_endpoint:
            raise MCPOAuthError(
                "OAuth client_id is required because the authorization server "
                "does not advertise dynamic client registration"
            )
        return await self._register_dynamic_client(
            registration_endpoint=registration_endpoint,
            redirect_uri=redirect_uri,
            scopes=scopes,
            client_metadata_document_url=client_metadata_document_url,
        )

    async def _register_dynamic_client(
        self,
        *,
        registration_endpoint: str,
        redirect_uri: str,
        scopes: list[str],
        client_metadata_document_url: str | None,
    ) -> OAuthClientRegistration:
        payload: dict[str, Any] = {
            "client_name": _DYNAMIC_CLIENT_NAME,
            "redirect_uris": [redirect_uri],
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
            "token_endpoint_auth_method": "none",
        }
        if scopes:
            payload["scope"] = " ".join(scopes)
        if client_metadata_document_url:
            payload["client_uri"] = client_metadata_document_url
        async with httpx.AsyncClient(timeout=_TOKEN_TIMEOUT, follow_redirects=False) as client:
            response = await client.post(
                _safe_url(registration_endpoint),
                json=payload,
                headers={"Accept": "application/json"},
            )
        if response.status_code >= 400:
            raise MCPOAuthError("OAuth dynamic client registration failed")
        data = response.json()
        if not isinstance(data, dict) or not isinstance(data.get("client_id"), str):
            raise MCPOAuthError("OAuth dynamic client registration response missing client_id")
        client_secret = data.get("client_secret")
        return OAuthClientRegistration(
            client_id=str(data["client_id"]),
            client_secret=str(client_secret) if client_secret else None,
        )

    async def start_authorization(
        self,
        *,
        user_email: str,
        server_id: str,
        conversation_id: str | None = None,
        task_id: str | None = None,
        step_name: str | None = None,
        step_run_id: str | None = None,
        session_id: str | None = None,
        delivery_mode: str | None = None,
    ) -> AuthorizationStart:
        async with self._session_factory() as session:
            server = await get_mcp_server(
                session, server_id, owner_email=user_email, include_shared=True
            )
            if server is None:
                raise MCPOAuthError("MCP server not found")
            auth_config = effective_mcp_auth_config(server.auth_config, server.headers)
            if auth_config.type != "oauth2":
                raise MCPOAuthError("MCP server is not configured for OAuth")
            metadata = await self.discover_metadata(server)
            authorization_endpoint = _safe_url(str(metadata.get("authorization_endpoint") or ""))
            issuer = str(metadata["issuer"]).rstrip("/")
            redirect_uri = auth_config.redirect_uri or self.redirect_uri()
            client = await self._resolve_client_registration(
                metadata=metadata,
                configured_client_id=auth_config.client_id,
                configured_client_secret=None,
                redirect_uri=redirect_uri,
                scopes=auth_config.scopes,
                client_metadata_document_url=auth_config.client_metadata_document_url,
            )
            verifier = _b64url(secrets.token_bytes(48))
            challenge = _b64url(hashlib.sha256(verifier.encode()).digest())
            transaction_id = f"mcpoauth_{uuid.uuid4().hex[:16]}"
            raw_state = _b64url(secrets.token_bytes(32))
            state_payload = {"t": transaction_id, "s": raw_state}
            state = _b64url(json.dumps(state_payload, separators=(",", ":")).encode())
            state_hash = hashlib.sha256(state.encode()).hexdigest()
            expires_at = _utcnow() + timedelta(seconds=_STATE_TTL_SECONDS)
            scopes = list(dict.fromkeys(auth_config.scopes))
            params = {
                "response_type": "code",
                "client_id": client.client_id,
                "redirect_uri": redirect_uri,
                "code_challenge": challenge,
                "code_challenge_method": "S256",
                "state": state,
            }
            if scopes:
                params["scope"] = " ".join(scopes)
            resource = auth_config.resource or server.url
            if resource:
                params["resource"] = resource
            forbidden_params = _RESERVED_AUTHORIZATION_PARAMS.intersection(
                {key.lower() for key in auth_config.authorization_params}
            )
            if forbidden_params:
                raise MCPOAuthError(
                    "OAuth authorization_params cannot override reserved parameters: "
                    + ", ".join(sorted(forbidden_params))
                )
            params.update(auth_config.authorization_params)
            authorization_url = f"{authorization_endpoint}?{urlencode(params)}"
            row = await create_mcp_oauth_transaction(
                session,
                transaction_id=transaction_id,
                user_email=user_email,
                mcp_server_id=server.server_id,
                issuer=issuer,
                authorization_server=str(metadata.get("issuer") or issuer),
                resource=resource,
                scopes=scopes,
                redirect_uri=redirect_uri,
                client_id=client.client_id,
                code_challenge=challenge,
                state_hash=state_hash,
                encrypted_payload=self._encrypt(
                    {
                        "code_verifier": verifier,
                        "state": state,
                        "client_secret": client.client_secret,
                    }
                ),
                expires_at=expires_at,
                task_id=task_id,
                step_name=step_name,
                step_run_id=step_run_id,
                session_id=session_id,
                conversation_id=conversation_id,
            )
            notification_id = None
            if self._notification_service is not None:
                notification_conversation_id = conversation_id or f"mcp_oauth:{transaction_id}"
                notification = await self._notification_service.create(
                    notification_type=NotificationType.AUTH_CHALLENGE,
                    user_email=user_email,
                    conversation_id=notification_conversation_id,
                    task_id=task_id,
                    step_name=step_name,
                    step_run_id=step_run_id,
                    session_id=session_id,
                    payload={
                        "kind": "oauth_authorization",
                        "label": "Authorize MCP server",
                        "message": "Open the authorization link to connect this MCP server.",
                        "required_fields": [],
                        "metadata": {
                            "authorization_url": authorization_url,
                            "transaction_id": transaction_id,
                            "provider": "mcp",
                            "subject_id": server.server_id,
                            "scopes": scopes,
                            "authorization_server": issuer,
                            "expires_at": expires_at.isoformat(),
                            "callback_only": True,
                        },
                    },
                    suppress_event=delivery_mode == "silent",
                )
                notification_id = notification.notification_id
                row.notification_id = notification_id
            await session.commit()
        return AuthorizationStart(
            authorization_url=authorization_url,
            transaction_id=transaction_id,
            expires_at=expires_at,
            issuer=issuer,
            authorization_server=issuer,
            scopes=scopes,
        )

    async def start_authorization_for_server(
        self,
        *,
        user_email: str,
        server: MCPServerRow,
        conversation_id: str | None = None,
        task_id: str | None = None,
        step_name: str | None = None,
        step_run_id: str | None = None,
        session_id: str | None = None,
        delivery_mode: str | None = "silent",
    ) -> AuthorizationStart | None:
        """Best-effort authorization start for runtime configuration paths."""

        if not user_email:
            return None
        try:
            return await self.start_authorization(
                user_email=user_email,
                server_id=server.server_id,
                conversation_id=conversation_id,
                task_id=task_id,
                step_name=step_name,
                step_run_id=step_run_id,
                session_id=session_id,
                delivery_mode=delivery_mode,
            )
        except Exception:
            logger.warning(
                "mcp oauth: failed to start authorization challenge",
                extra={"extra_data": {"server_id": server.server_id}},
                exc_info=True,
            )
            return None

    async def _mark_token_invalid_and_start_authorization(
        self,
        session: Any,
        *,
        token_id: str,
        reason: str,
        headers: dict[str, str],
        context: AuthorizationChallengeContext,
    ) -> TokenInjectionResult:
        await mark_mcp_oauth_token_status(session, token_id=token_id, status="invalid")
        await session.commit()
        try:
            authorization = await self.start_authorization_for_server(
                user_email=context.user_email,
                server=context.server,
                conversation_id=context.conversation_id,
                task_id=context.task_id,
                step_name=context.step_name,
                step_run_id=context.step_run_id,
                session_id=context.session_id,
                delivery_mode=context.delivery_mode,
            )
        except Exception:
            logger.warning(
                "mcp oauth: failed to start reauthorization after token invalidation",
                extra={
                    "extra_data": {
                        "server_id": context.server.server_id,
                        "reason": reason,
                    }
                },
                exc_info=True,
            )
            authorization = None
        return TokenInjectionResult(
            headers=headers,
            authorization_required=True,
            reason=reason,
            authorization_url=authorization.authorization_url if authorization else None,
            transaction_id=authorization.transaction_id if authorization else None,
        )

    async def complete_callback(self, *, state: str, code: str) -> str:
        try:
            state_payload = json.loads(base64.urlsafe_b64decode(state + "==="))
            transaction_id = str(state_payload["t"])
        except Exception as exc:
            raise MCPOAuthError("Invalid OAuth state") from exc
        state_hash = hashlib.sha256(state.encode()).hexdigest()
        notification_id = None
        async with self._session_factory() as session:
            row = await get_mcp_oauth_transaction(session, transaction_id)
            if row is None or not hmac.compare_digest(row.state_hash, state_hash):
                raise MCPOAuthError("Invalid OAuth transaction")
            notification_id = row.notification_id
            now = _utcnow()
            if row.status != "pending" or row.used_at is not None or row.expires_at < now:
                raise MCPOAuthError("OAuth transaction is expired or already used")
            row.status = "exchanging"
            row.used_at = now
            await session.commit()

            try:
                payload = self._decrypt(row.encrypted_payload)
                server = await get_mcp_server(
                    session, row.mcp_server_id, owner_email=row.user_email, include_shared=True
                )
                if server is None:
                    raise MCPOAuthError("MCP server not found")
                metadata = await self.discover_metadata(server)
                token_endpoint = _safe_url(str(metadata.get("token_endpoint") or ""))
                token_response = await self._exchange_code(
                    token_endpoint=token_endpoint,
                    code=code,
                    redirect_uri=row.redirect_uri,
                    client_id=row.client_id,
                    client_secret=payload.get("client_secret")
                    if isinstance(payload.get("client_secret"), str)
                    else None,
                    code_verifier=str(payload["code_verifier"]),
                    resource=row.resource,
                )
                expires_at = None
                if isinstance(token_response.get("expires_in"), int):
                    expires_at = now + timedelta(seconds=int(token_response["expires_in"]))
                await upsert_mcp_oauth_token(
                    session,
                    user_email=row.user_email,
                    mcp_server_id=row.mcp_server_id,
                    issuer=row.issuer,
                    resource=row.resource,
                    client_id=row.client_id,
                    scopes=row.scopes or [],
                    token_type=str(token_response.get("token_type") or "Bearer"),
                    expires_at=expires_at,
                    encrypted_payload=self._encrypt(token_response),
                )
                row.status = "completed"
                await session.commit()
            except Exception as exc:
                row.status = "failed"
                row.error_code = "token_exchange_failed"
                row.error_description = "OAuth token exchange failed"
                await session.commit()
                if notification_id and self._notification_service is not None:
                    await self._notification_service.resolve_internal(
                        notification_id,
                        "failed",
                        {"transaction_id": transaction_id, "provider": "mcp"},
                    )
                if isinstance(exc, MCPOAuthError):
                    raise
                raise MCPOAuthError("OAuth token exchange failed") from exc
        if notification_id and self._notification_service is not None:
            await self._notification_service.resolve_internal(
                notification_id,
                "completed",
                {"transaction_id": transaction_id, "provider": "mcp"},
            )
        return transaction_id

    async def _exchange_code(
        self,
        *,
        token_endpoint: str,
        code: str,
        redirect_uri: str,
        client_id: str,
        client_secret: str | None,
        code_verifier: str,
        resource: str | None,
    ) -> dict[str, Any]:
        data = {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect_uri,
            "client_id": client_id,
            "code_verifier": code_verifier,
        }
        if client_secret:
            data["client_secret"] = client_secret
        if resource:
            data["resource"] = resource
        async with httpx.AsyncClient(timeout=_TOKEN_TIMEOUT, follow_redirects=False) as client:
            response = await client.post(token_endpoint, data=data)
        if response.status_code >= 400:
            raise MCPOAuthError("OAuth token exchange failed")
        payload = response.json()
        if not isinstance(payload, dict) or not payload.get("access_token"):
            raise MCPOAuthError("OAuth token response missing access token")
        return payload

    async def _refresh_token(
        self,
        *,
        token_endpoint: str,
        client_id: str,
        refresh_token: str,
        resource: str | None,
    ) -> dict[str, Any]:
        data = {
            "grant_type": "refresh_token",
            "client_id": client_id,
            "refresh_token": refresh_token,
        }
        if resource:
            data["resource"] = resource
        async with httpx.AsyncClient(timeout=_TOKEN_TIMEOUT, follow_redirects=False) as client:
            response = await client.post(token_endpoint, data=data)
        if response.status_code >= 400:
            raise MCPOAuthError("OAuth token refresh failed")
        payload = response.json()
        if not isinstance(payload, dict) or not payload.get("access_token"):
            raise MCPOAuthError("OAuth refresh response missing access token")
        return payload

    async def inject_authorization_header(
        self,
        *,
        user_email: str,
        server: MCPServerRow,
        headers: dict[str, str],
        conversation_id: str | None = None,
        task_id: str | None = None,
        step_name: str | None = None,
        step_run_id: str | None = None,
        session_id: str | None = None,
        delivery_mode: str | None = "silent",
    ) -> TokenInjectionResult:
        auth_config = effective_mcp_auth_config(server.auth_config, server.headers)
        if auth_config.type != "oauth2":
            return TokenInjectionResult(headers=headers)
        challenge_context = AuthorizationChallengeContext(
            user_email=user_email,
            server=server,
            conversation_id=conversation_id,
            task_id=task_id,
            step_name=step_name,
            step_run_id=step_run_id,
            session_id=session_id,
            delivery_mode=delivery_mode,
        )
        issuer = (auth_config.issuer or auth_config.authorization_server or "").rstrip("/")
        if not issuer:
            metadata = await self.discover_metadata(server)
            issuer = str(metadata["issuer"]).rstrip("/")
        resource = auth_config.resource or server.url
        async with self._session_factory() as session:
            row = await get_mcp_oauth_token(
                session,
                user_email=user_email,
                mcp_server_id=server.server_id,
                issuer=issuer,
                resource=resource,
            )
            if row is None or row.status != "active":
                authorization = await self.start_authorization_for_server(
                    user_email=user_email,
                    server=server,
                    conversation_id=conversation_id,
                    task_id=task_id,
                    step_name=step_name,
                    step_run_id=step_run_id,
                    session_id=session_id,
                    delivery_mode=delivery_mode,
                )
                return TokenInjectionResult(
                    headers=headers,
                    authorization_required=True,
                    reason="authorization_required",
                    authorization_url=authorization.authorization_url if authorization else None,
                    transaction_id=authorization.transaction_id if authorization else None,
                )
            payload = self._decrypt(row.encrypted_payload)
            now = _utcnow()
            if row.expires_at is not None and row.expires_at <= now + timedelta(
                seconds=_REFRESH_SKEW_SECONDS
            ):
                refresh_token = payload.get("refresh_token")
                if not isinstance(refresh_token, str) or not refresh_token:
                    await mark_mcp_oauth_token_status(
                        session, token_id=row.token_id, status="expired"
                    )
                    await session.commit()
                    authorization = await self.start_authorization_for_server(
                        user_email=user_email,
                        server=server,
                        conversation_id=conversation_id,
                        task_id=task_id,
                        step_name=step_name,
                        step_run_id=step_run_id,
                        session_id=session_id,
                        delivery_mode=delivery_mode,
                    )
                    return TokenInjectionResult(
                        headers=headers,
                        authorization_required=True,
                        reason="token_expired",
                        authorization_url=authorization.authorization_url
                        if authorization
                        else None,
                        transaction_id=authorization.transaction_id if authorization else None,
                    )
                try:
                    metadata = await self.discover_metadata(server)
                    refreshed = await self._refresh_token(
                        token_endpoint=_safe_url(str(metadata.get("token_endpoint") or "")),
                        client_id=row.client_id
                        or auth_config.client_id
                        or f"cognis-mcp-{server.server_id}",
                        refresh_token=refresh_token,
                        resource=resource,
                    )
                except Exception:
                    logger.warning(
                        "mcp oauth: token refresh failed; token marked invalid",
                        extra={
                            "extra_data": {
                                "server_id": server.server_id,
                                "token_id": row.token_id,
                            }
                        },
                        exc_info=True,
                    )
                    return await self._mark_token_invalid_and_start_authorization(
                        session,
                        token_id=row.token_id,
                        reason="refresh_failed",
                        headers=headers,
                        context=challenge_context,
                    )
                if not refreshed.get("refresh_token"):
                    refreshed["refresh_token"] = refresh_token
                expires_at = None
                if isinstance(refreshed.get("expires_in"), int):
                    expires_at = now + timedelta(seconds=int(refreshed["expires_in"]))
                await upsert_mcp_oauth_token(
                    session,
                    user_email=user_email,
                    mcp_server_id=server.server_id,
                    issuer=issuer,
                    resource=resource,
                    client_id=row.client_id or auth_config.client_id,
                    scopes=row.scopes or auth_config.scopes,
                    token_type=str(refreshed.get("token_type") or row.token_type or "Bearer"),
                    expires_at=expires_at,
                    encrypted_payload=self._encrypt(refreshed),
                )
                row.last_refresh_at = now
                await session.commit()
                payload = refreshed
        access_token = payload.get("access_token")
        if not isinstance(access_token, str) or not access_token:
            return TokenInjectionResult(
                headers=headers, authorization_required=True, reason="invalid_token"
            )
        injected = dict(headers)
        injected["Authorization"] = f"Bearer {access_token}"
        return TokenInjectionResult(headers=injected)


def oauth_status_payload(row: Any | None) -> dict[str, Any]:
    if row is None:
        return {"connected": False}
    return {
        "connected": row.status == "active",
        "issuer": row.issuer,
        "resource": row.resource if row.resource_key != mcp_oauth_resource_key(None) else None,
        "scopes": row.scopes or [],
        "expires_at": row.expires_at.isoformat() if row.expires_at else None,
        "refreshable": True,
        "status": row.status,
    }
