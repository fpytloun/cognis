"""OAuth 2.1 helpers for HTTP MCP servers.

This module deliberately keeps OAuth tokens controller-side. Executors only
receive short-lived bearer access tokens injected into MCP HTTP headers.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import socket
import uuid
from collections.abc import Awaitable, Callable
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
    get_executor_row,
    get_mcp_oauth_token,
    get_mcp_oauth_token_for_server,
    get_mcp_oauth_transaction,
    get_mcp_server,
    list_executors,
    list_pending_mcp_oauth_transactions,
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
_DEVICE_GRANT_TYPE = "urn:ietf:params:oauth:grant-type:device_code"
_DEVICE_DCR_REDIRECT_URI = "http://127.0.0.1/oauth/callback"
_EXECUTOR_LOOPBACK_CALLBACK_PATH = "/oauth/callback"
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

    def __init__(
        self,
        message: str,
        *,
        reason: str | None = None,
        authorization_required: bool = False,
        retryable: bool = False,
    ) -> None:
        super().__init__(message)
        self.reason = reason
        self.authorization_required = authorization_required
        self.retryable = retryable


@dataclass(frozen=True)
class AuthorizationStart:
    authorization_url: str
    transaction_id: str
    expires_at: datetime
    issuer: str
    authorization_server: str
    scopes: list[str]
    flow: str = "authorization_code"
    verification_uri: str | None = None
    verification_uri_complete: str | None = None
    user_code: str | None = None
    interval: int | None = None
    callback_mode: str = "controller_public"
    oauth_executor_id: str | None = None
    oauth_executor_name: str | None = None
    redirect_uri: str | None = None
    instructions: str | None = None


@dataclass(frozen=True)
class OAuthClientRegistration:
    client_id: str
    client_secret: str | None = None
    grant_types: tuple[str, ...] = ()


@dataclass(frozen=True)
class TokenInjectionResult:
    headers: dict[str, str]
    authorization_required: bool = False
    reason: str | None = None
    authorization_url: str | None = None
    transaction_id: str | None = None
    authorization_expires_at: datetime | None = None
    flow: str | None = None
    verification_uri: str | None = None
    verification_uri_complete: str | None = None
    user_code: str | None = None
    callback_mode: str | None = None
    oauth_executor_id: str | None = None
    oauth_executor_name: str | None = None
    redirect_uri: str | None = None
    instructions: str | None = None


def oauth_required_mcp_status(
    *,
    server_id: object,
    server_name: str,
    reason: str | None,
    transaction_id: str | None = None,
    authorization_url: str | None = None,
    flow: str | None = None,
    verification_uri: str | None = None,
    verification_uri_complete: str | None = None,
    user_code: str | None = None,
    callback_mode: str | None = None,
    oauth_executor_id: str | None = None,
    oauth_executor_name: str | None = None,
    redirect_uri: str | None = None,
    instructions: str | None = None,
) -> dict[str, Any]:
    """Return safe runtime metadata for an MCP server awaiting OAuth authorization."""

    return {
        "server_id": str(server_id),
        "name": server_name,
        "phase": "authorization",
        "status": "authorization_required",
        "authorization_required": True,
        "reason": reason or "authorization_required",
        "transaction_id": transaction_id,
        "authorization_url": authorization_url,
        "authorization_url_available": bool(authorization_url),
        "flow": flow,
        "verification_uri": verification_uri,
        "verification_uri_complete": verification_uri_complete,
        "user_code": user_code,
        "callback_mode": callback_mode,
        "oauth_executor_id": oauth_executor_id,
        "oauth_executor_name": oauth_executor_name,
        "redirect_uri": redirect_uri,
        "instructions": instructions,
    }


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


def _safe_provider_error(response: httpx.Response, *, operation: str) -> str:
    """Build a sanitized provider-error message without credential payloads."""

    parts = [f"{operation} failed (HTTP {response.status_code})"]
    try:
        payload = response.json()
    except ValueError:
        payload = None
    if isinstance(payload, dict):
        error = payload.get("error")
        description = payload.get("error_description")
        if isinstance(error, str) and error:
            parts.append(error[:120])
        if isinstance(description, str) and description:
            sanitized = re.sub(
                r"(?i)\b(access_token|refresh_token|client_secret|code|device_code|password|authorization)=([^,\s&]+)",
                r"\1=[redacted]",
                description,
            )
            parts.append(sanitized[:500])
    return ": ".join(parts)


def _parse_oauth_json_response(response: httpx.Response, *, operation: str) -> dict[str, Any]:
    try:
        payload = response.json()
    except ValueError as exc:
        content_type = response.headers.get("content-type")
        suffix = f" ({content_type})" if content_type else ""
        raise MCPOAuthError(f"{operation} response was not valid JSON{suffix}") from exc
    if not isinstance(payload, dict):
        raise MCPOAuthError(f"{operation} response was not a JSON object")
    return payload


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
        on_authorization_completed: Callable[[str], Awaitable[None]] | None = None,
        executor_provider: Any | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._public_base_url = public_base_url.rstrip("/")
        self._notification_service = notification_service
        self._on_authorization_completed = on_authorization_completed
        self._executor_provider = executor_provider
        self._refresh_locks: dict[tuple[str, str, str, str], asyncio.Lock] = {}
        self._device_poll_tasks: dict[str, asyncio.Task[None]] = {}
        with open(key_path, "rb") as key_file:
            self._key = base64.urlsafe_b64decode(key_file.read())

    def _refresh_lock(
        self,
        *,
        user_email: str,
        server_id: str,
        issuer: str,
        resource: str | None,
    ) -> asyncio.Lock:
        key = (user_email, server_id, issuer, mcp_oauth_resource_key(resource))
        lock = self._refresh_locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            self._refresh_locks[key] = lock
        return lock

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

    def _callback_mode(self, auth_config: Any) -> str:
        mode = str(getattr(auth_config, "callback_mode", "auto") or "auto")
        if mode == "auto" and getattr(auth_config, "oauth_executor_id", None):
            return "executor_loopback"
        if mode == "auto":
            return "controller_public"
        return mode

    async def _start_executor_loopback_listener(
        self,
        *,
        executor_id: str,
        state: str,
    ) -> dict[str, Any]:
        if self._executor_provider is None:
            raise MCPOAuthError("OAuth executor callback is unavailable")
        get_connection = getattr(self._executor_provider, "get_connection", None)
        if get_connection is None:
            raise MCPOAuthError("OAuth executor callback is unavailable")
        conn = get_connection(executor_id)
        if conn is None:
            raise MCPOAuthError(f"OAuth executor {executor_id} is not connected")
        start_listener = getattr(conn, "oauth_loopback_start", None)
        if start_listener is None:
            raise MCPOAuthError(f"OAuth executor {executor_id} does not support loopback OAuth")
        result = await start_listener(
            state=state,
            ttl_seconds=_STATE_TTL_SECONDS,
            callback_path=_EXECUTOR_LOOPBACK_CALLBACK_PATH,
        )
        listener_id = result.get("listener_id")
        redirect_uri = result.get("redirect_uri")
        if not isinstance(listener_id, str) or not listener_id:
            raise MCPOAuthError("OAuth executor did not return a listener ID")
        if not isinstance(redirect_uri, str) or not redirect_uri:
            raise MCPOAuthError("OAuth executor did not return a redirect URI")
        parsed = urlsplit(redirect_uri)
        if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost"}:
            raise MCPOAuthError("OAuth executor returned an invalid loopback redirect URI")
        if parsed.path != _EXECUTOR_LOOPBACK_CALLBACK_PATH:
            raise MCPOAuthError("OAuth executor returned an invalid callback path")
        return result

    async def _stop_executor_loopback_listener(
        self,
        *,
        executor_id: str | None,
        listener_id: str | None,
    ) -> None:
        if not executor_id or not listener_id or self._executor_provider is None:
            return
        get_connection = getattr(self._executor_provider, "get_connection", None)
        if get_connection is None:
            return
        conn = get_connection(executor_id)
        if conn is None:
            return
        stop_listener = getattr(conn, "oauth_loopback_stop", None)
        if stop_listener is None:
            return
        try:
            await stop_listener(listener_id=listener_id)
        except Exception:
            logger.debug(
                "mcp oauth: failed to stop executor loopback listener",
                extra={
                    "extra_data": {
                        "executor_id": executor_id,
                        "listener_id": listener_id,
                    }
                },
                exc_info=True,
            )

    async def discover_metadata(self, server: MCPServerRow) -> dict[str, Any]:
        auth_config = effective_mcp_auth_config(server.auth_config, server.headers)
        issuer = auth_config.issuer or auth_config.authorization_server
        if not issuer:
            if not server.url:
                raise MCPOAuthError("MCP OAuth server URL is required for discovery")
            issuer = await self._discover_issuer_from_resource(server.url)
        issuer = _safe_url(issuer.rstrip("/"))
        metadata = await self._fetch_json(
            f"{issuer}/.well-known/oauth-authorization-server",
            missing_ok=True,
        )
        if not metadata:
            metadata = await self._fetch_json(
                f"{issuer}/.well-known/openid-configuration",
                missing_ok=True,
            )
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
            urljoin(base, "/.well-known/oauth-protected-resource"),
            missing_ok=True,
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
        resource_metadata_url = challenge.get("resource_metadata")
        if resource_metadata_url:
            resource_metadata = await self._fetch_json(
                resource_metadata_url,
                missing_ok=True,
            )
            if resource_metadata:
                auth_servers = resource_metadata.get("authorization_servers")
                if isinstance(auth_servers, list) and auth_servers:
                    return str(auth_servers[0])
        issuer = challenge.get("authorization_uri") or challenge.get("issuer")
        if not issuer:
            raise MCPOAuthError("OAuth authorization server could not be discovered")
        return issuer

    async def _fetch_json(self, url: str, *, missing_ok: bool = False) -> dict[str, Any] | None:
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
        try:
            data = response.json()
        except json.JSONDecodeError as exc:
            if missing_ok:
                return None
            raise MCPOAuthError("OAuth metadata response is not valid JSON") from exc
        return data if isinstance(data, dict) else None

    async def _resolve_client_registration(
        self,
        *,
        metadata: dict[str, Any],
        configured_client_id: str | None,
        configured_client_secret: str | None,
        redirect_uri: str | None,
        scopes: list[str],
        client_metadata_document_url: str | None,
        flow: str = "authorization_code",
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
            flow=flow,
        )

    async def _register_dynamic_client(
        self,
        *,
        registration_endpoint: str,
        redirect_uri: str | None,
        scopes: list[str],
        client_metadata_document_url: str | None,
        flow: str = "authorization_code",
    ) -> OAuthClientRegistration:
        payload: dict[str, Any] = {
            "client_name": _DYNAMIC_CLIENT_NAME,
            "token_endpoint_auth_method": "none",
        }
        if flow == "device_code":
            payload["redirect_uris"] = [_DEVICE_DCR_REDIRECT_URI]
            payload["grant_types"] = [_DEVICE_GRANT_TYPE, "refresh_token"]
        else:
            if not redirect_uri:
                raise MCPOAuthError("OAuth redirect URI is required for authorization-code flow")
            payload["redirect_uris"] = [redirect_uri]
            payload["grant_types"] = ["authorization_code", "refresh_token"]
            payload["response_types"] = ["code"]
        if scopes:
            payload["scope"] = " ".join(scopes)
        if client_metadata_document_url:
            payload["client_uri"] = client_metadata_document_url
        device_fallback_payloads: list[dict[str, Any]] = []
        if flow == "device_code":
            empty_redirects_payload = dict(payload)
            empty_redirects_payload["redirect_uris"] = []
            device_fallback_payloads.append(empty_redirects_payload)
            omitted_redirects_payload = dict(payload)
            omitted_redirects_payload.pop("redirect_uris", None)
            device_fallback_payloads.append(omitted_redirects_payload)
        async with httpx.AsyncClient(timeout=_TOKEN_TIMEOUT, follow_redirects=False) as client:
            response = await client.post(
                _safe_url(registration_endpoint),
                json=payload,
                headers={"Accept": "application/json"},
            )
            for fallback_payload in device_fallback_payloads:
                if response.status_code != 400:
                    break
                response = await client.post(
                    _safe_url(registration_endpoint),
                    json=fallback_payload,
                    headers={"Accept": "application/json"},
                )
        if response.status_code >= 400:
            message = _safe_provider_error(
                response,
                operation="OAuth dynamic client registration",
            )
            logger.warning(
                "mcp oauth: dynamic client registration failed",
                extra={
                    "extra_data": {
                        "status_code": response.status_code,
                        "provider_error": message,
                    }
                },
            )
            raise MCPOAuthError(message)
        data = _parse_oauth_json_response(
            response,
            operation="OAuth dynamic client registration",
        )
        if not isinstance(data, dict) or not isinstance(data.get("client_id"), str):
            raise MCPOAuthError("OAuth dynamic client registration response missing client_id")
        registered_grant_types = data.get("grant_types")
        grant_types = (
            tuple(str(grant_type) for grant_type in registered_grant_types)
            if isinstance(registered_grant_types, list)
            else ()
        )
        if flow == "device_code" and _DEVICE_GRANT_TYPE not in grant_types:
            raise MCPOAuthError(
                "OAuth dynamic client registration did not register a device-code client"
            )
        client_secret = data.get("client_secret")
        return OAuthClientRegistration(
            client_id=str(data["client_id"]),
            client_secret=str(client_secret) if client_secret else None,
            grant_types=grant_types,
        )

    def _select_flow(
        self, *, requested_flow: str, auth_config: Any, metadata: dict[str, Any]
    ) -> str:
        if requested_flow == "authorization_code":
            return "authorization_code"
        device_endpoint = metadata.get("device_authorization_endpoint")
        if requested_flow == "device_code":
            if not isinstance(device_endpoint, str) or not device_endpoint:
                raise MCPOAuthError(
                    "OAuth device-code flow is not available because the authorization server "
                    "does not advertise device_authorization_endpoint"
                )
            return "device_code"
        if getattr(auth_config, "oauth_executor_id", None):
            return "authorization_code"
        if auth_config.client_id or auth_config.redirect_uri:
            return "authorization_code"
        if isinstance(device_endpoint, str) and device_endpoint:
            return "device_code"
        return "authorization_code"

    async def _start_device_authorization(
        self,
        *,
        session: Any,
        server: MCPServerRow,
        metadata: dict[str, Any],
        user_email: str,
        conversation_id: str | None,
        task_id: str | None,
        step_name: str | None,
        step_run_id: str | None,
        session_id: str | None,
        delivery_mode: str | None,
    ) -> AuthorizationStart:
        auth_config = effective_mcp_auth_config(server.auth_config, server.headers)
        issuer = str(metadata["issuer"]).rstrip("/")
        device_endpoint = _safe_url(str(metadata.get("device_authorization_endpoint") or ""))
        token_endpoint = _safe_url(str(metadata.get("token_endpoint") or ""))
        scopes = list(dict.fromkeys(auth_config.scopes))
        client = await self._resolve_client_registration(
            metadata=metadata,
            configured_client_id=auth_config.client_id,
            configured_client_secret=None,
            redirect_uri=None,
            scopes=scopes,
            client_metadata_document_url=auth_config.client_metadata_document_url,
            flow="device_code",
        )
        device_response = await self._request_device_authorization(
            device_endpoint=device_endpoint,
            client_id=client.client_id,
            client_secret=client.client_secret,
            scopes=scopes,
            resource=auth_config.resource or server.url,
        )
        device_code = device_response.get("device_code")
        user_code = device_response.get("user_code")
        verification_uri = device_response.get("verification_uri") or device_response.get(
            "verification_url"
        )
        if not (
            isinstance(device_code, str)
            and device_code
            and isinstance(user_code, str)
            and user_code
            and isinstance(verification_uri, str)
            and verification_uri
        ):
            raise MCPOAuthError("OAuth device authorization response missing required fields")
        verification_uri_complete = device_response.get("verification_uri_complete")
        if not isinstance(verification_uri_complete, str):
            verification_uri_complete = None
        expires_in = device_response.get("expires_in")
        expires_at = _utcnow() + timedelta(
            seconds=int(expires_in) if isinstance(expires_in, int) and expires_in > 0 else 900
        )
        interval_value = device_response.get("interval")
        interval = (
            int(interval_value) if isinstance(interval_value, int) and interval_value > 0 else 5
        )
        transaction_id = f"mcpoauth_{uuid.uuid4().hex[:16]}"
        resource = auth_config.resource or server.url
        row = await create_mcp_oauth_transaction(
            session,
            transaction_id=transaction_id,
            user_email=user_email,
            mcp_server_id=server.server_id,
            issuer=issuer,
            authorization_server=issuer,
            resource=resource,
            scopes=scopes,
            redirect_uri="",
            client_id=client.client_id,
            code_challenge="",
            state_hash="",
            encrypted_payload=self._encrypt(
                {
                    "flow": "device_code",
                    "device_code": device_code,
                    "client_secret": client.client_secret,
                    "token_endpoint": token_endpoint,
                    "verification_uri": verification_uri,
                    "verification_uri_complete": verification_uri_complete,
                    "user_code": user_code,
                    "interval": interval,
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
        authorization_url = verification_uri_complete or verification_uri
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
                    "message": "Authorize this MCP server using the provider verification page.",
                    "required_fields": [],
                    "metadata": {
                        "flow": "device_code",
                        "authorization_url": authorization_url,
                        "verification_uri": verification_uri,
                        "verification_uri_complete": verification_uri_complete,
                        "user_code": user_code,
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
        self._ensure_device_poll_task(transaction_id)
        return AuthorizationStart(
            authorization_url=authorization_url,
            transaction_id=transaction_id,
            expires_at=expires_at,
            issuer=issuer,
            authorization_server=issuer,
            scopes=scopes,
            flow="device_code",
            verification_uri=verification_uri,
            verification_uri_complete=verification_uri_complete,
            user_code=user_code,
            interval=interval,
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
            flow = self._select_flow(
                requested_flow=auth_config.flow,
                auth_config=auth_config,
                metadata=metadata,
            )
            if flow == "device_code":
                return await self._start_device_authorization(
                    session=session,
                    server=server,
                    metadata=metadata,
                    user_email=user_email,
                    conversation_id=conversation_id,
                    task_id=task_id,
                    step_name=step_name,
                    step_run_id=step_run_id,
                    session_id=session_id,
                    delivery_mode=delivery_mode,
                )
            authorization_endpoint = _safe_url(str(metadata.get("authorization_endpoint") or ""))
            issuer = str(metadata["issuer"]).rstrip("/")
            verifier = _b64url(secrets.token_bytes(48))
            challenge = _b64url(hashlib.sha256(verifier.encode()).digest())
            transaction_id = f"mcpoauth_{uuid.uuid4().hex[:16]}"
            raw_state = _b64url(secrets.token_bytes(32))
            state_payload = {"t": transaction_id, "s": raw_state}
            state = _b64url(json.dumps(state_payload, separators=(",", ":")).encode())
            state_hash = hashlib.sha256(state.encode()).hexdigest()
            expires_at = _utcnow() + timedelta(seconds=_STATE_TTL_SECONDS)
            scopes = list(dict.fromkeys(auth_config.scopes))
            callback_mode = self._callback_mode(auth_config)
            oauth_executor_id = None
            oauth_executor_name = None
            loopback_listener_id = None
            loopback_started = False
            instructions = None
            if callback_mode == "executor_loopback":
                oauth_executor_id = str(auth_config.oauth_executor_id or "").strip()
                if not oauth_executor_id:
                    executor_rows = await list_executors(
                        session,
                        owner_email=user_email,
                        include_shared=True,
                    )
                    websocket_rows = [
                        row
                        for row in executor_rows
                        if getattr(row, "executor_type", None) == "websocket"
                    ]
                    executor_row = next(
                        (row for row in websocket_rows if getattr(row, "is_default", False)), None
                    ) or (websocket_rows[0] if websocket_rows else None)
                    if executor_row is None:
                        raise MCPOAuthError(
                            "A connected websocket OAuth executor is required for executor loopback callback"
                        )
                    oauth_executor_id = executor_row.executor_id
                else:
                    executor_row = await get_executor_row(
                        session,
                        oauth_executor_id,
                        owner_email=user_email,
                        include_shared=True,
                    )
                if executor_row is None:
                    raise MCPOAuthError(f"OAuth executor {oauth_executor_id} was not found")
                oauth_executor_name = executor_row.name
                loopback = await self._start_executor_loopback_listener(
                    executor_id=oauth_executor_id,
                    state=state,
                )
                redirect_uri = str(loopback["redirect_uri"])
                loopback_listener_id = str(loopback["listener_id"])
                loopback_started = True
                instructions = (
                    f"Open this authorization URL in a browser running on executor "
                    f"{oauth_executor_name or oauth_executor_id}. If you are not on that "
                    "executor, use a remote browser or tunnel so the loopback callback "
                    "resolves on that executor."
                )
            else:
                redirect_uri = auth_config.redirect_uri or self.redirect_uri()
                callback_mode = "controller_public"
            try:
                client = await self._resolve_client_registration(
                    metadata=metadata,
                    configured_client_id=auth_config.client_id,
                    configured_client_secret=None,
                    redirect_uri=redirect_uri,
                    scopes=auth_config.scopes,
                    client_metadata_document_url=auth_config.client_metadata_document_url,
                    flow="authorization_code",
                )
            except Exception:
                if loopback_started:
                    await self._stop_executor_loopback_listener(
                        executor_id=oauth_executor_id,
                        listener_id=loopback_listener_id,
                    )
                raise
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
                        "callback_mode": callback_mode,
                        "oauth_executor_id": oauth_executor_id,
                        "oauth_executor_name": oauth_executor_name,
                        "loopback_listener_id": loopback_listener_id,
                        "redirect_uri": redirect_uri,
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
                        "message": instructions
                        or "Open the authorization link to connect this MCP server.",
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
                            "callback_mode": callback_mode,
                            "oauth_executor_id": oauth_executor_id,
                            "oauth_executor_name": oauth_executor_name,
                            "redirect_uri": redirect_uri,
                            "instructions": instructions,
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
            flow="authorization_code",
            callback_mode=callback_mode,
            oauth_executor_id=oauth_executor_id,
            oauth_executor_name=oauth_executor_name,
            redirect_uri=redirect_uri,
            instructions=instructions,
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
            authorization_expires_at=authorization.expires_at if authorization else None,
            flow=getattr(authorization, "flow", None) if authorization else None,
            verification_uri=getattr(authorization, "verification_uri", None)
            if authorization
            else None,
            verification_uri_complete=getattr(authorization, "verification_uri_complete", None)
            if authorization
            else None,
            user_code=getattr(authorization, "user_code", None) if authorization else None,
            callback_mode=getattr(authorization, "callback_mode", None) if authorization else None,
            oauth_executor_id=getattr(authorization, "oauth_executor_id", None)
            if authorization
            else None,
            oauth_executor_name=getattr(authorization, "oauth_executor_name", None)
            if authorization
            else None,
            redirect_uri=getattr(authorization, "redirect_uri", None) if authorization else None,
            instructions=getattr(authorization, "instructions", None) if authorization else None,
        )

    async def mark_token_invalid_for_server(
        self,
        *,
        user_email: str,
        server_id: str,
        reason: str = "authorization_failed",
    ) -> bool:
        """Ensure a server has no active token after a resource-server auth failure.

        The boolean return value means the caller should reconfigure stale
        executors. Multiple executors can observe the same rejected shared token;
        the first one marks it invalid, while later ones still need a reconfigure
        to drop the now-unauthorized MCP server from their applied config.
        """

        if not user_email or not server_id:
            return False
        async with self._session_factory() as session:
            server = await get_mcp_server(
                session,
                server_id,
                owner_email=user_email,
                include_shared=True,
            )
            if server is None:
                return False
            auth_config = effective_mcp_auth_config(
                getattr(server, "auth_config", None),
                getattr(server, "headers", None),
            )
            if auth_config.type != "oauth2":
                return False
            row = await get_mcp_oauth_token_for_server(
                session,
                user_email=user_email,
                mcp_server_id=server_id,
            )
            if row is None or row.status != "active":
                logger.info(
                    "mcp oauth: authorization failure observed after token was already unavailable",
                    extra={
                        "extra_data": {
                            "server_id": server_id,
                            "user_email": user_email,
                            "reason": reason,
                            "token_status": getattr(row, "status", None)
                            if row is not None
                            else None,
                        }
                    },
                )
                return True
            await mark_mcp_oauth_token_status(session, token_id=row.token_id, status="invalid")
            await session.commit()
        logger.warning(
            "mcp oauth: token marked invalid after MCP resource authorization failure",
            extra={
                "extra_data": {
                    "server_id": server_id,
                    "user_email": user_email,
                    "reason": reason,
                }
            },
        )
        return True

    async def complete_loopback_callback(
        self,
        *,
        executor_id: str,
        listener_id: str,
        redirect_uri: str,
        state: str,
        code: str | None,
        error: str | None = None,
        error_description: str | None = None,
    ) -> str:
        if error:
            await self._mark_callback_error(
                state=state,
                error_code="provider_error",
                error_description=error_description or error,
                source_executor_id=executor_id,
                listener_id=listener_id,
                callback_redirect_uri=redirect_uri,
            )
            raise MCPOAuthError("OAuth provider returned an error")
        if not code:
            raise MCPOAuthError("OAuth callback is missing authorization code")
        return await self.complete_callback(
            state=state,
            code=code,
            source_executor_id=executor_id,
            listener_id=listener_id,
            callback_redirect_uri=redirect_uri,
        )

    async def _mark_callback_error(
        self,
        *,
        state: str,
        error_code: str,
        error_description: str,
        source_executor_id: str | None = None,
        listener_id: str | None = None,
        callback_redirect_uri: str | None = None,
    ) -> str:
        try:
            state_payload = json.loads(base64.urlsafe_b64decode(state + "==="))
            transaction_id = str(state_payload["t"])
        except Exception as exc:
            raise MCPOAuthError("Invalid OAuth state") from exc
        state_hash = hashlib.sha256(state.encode()).hexdigest()
        async with self._session_factory() as session:
            row = await get_mcp_oauth_transaction(session, transaction_id)
            if row is None or not hmac.compare_digest(row.state_hash, state_hash):
                raise MCPOAuthError("Invalid OAuth transaction")
            if row.status != "pending" or row.used_at is not None or row.expires_at < _utcnow():
                raise MCPOAuthError("OAuth transaction is expired or already used")
            payload = self._decrypt(row.encrypted_payload)
            self._validate_callback_source(
                row=row,
                payload=payload,
                source_executor_id=source_executor_id,
                listener_id=listener_id,
                callback_redirect_uri=callback_redirect_uri,
            )
            row.status = "failed"
            row.error_code = error_code
            row.error_description = error_description[:500]
            await session.commit()
            if row.notification_id and self._notification_service is not None:
                await self._notification_service.resolve_internal(
                    row.notification_id,
                    "failed",
                    {"transaction_id": transaction_id, "provider": "mcp"},
                )
        return transaction_id

    def _validate_callback_source(
        self,
        *,
        row: Any,
        payload: dict[str, Any],
        source_executor_id: str | None,
        listener_id: str | None,
        callback_redirect_uri: str | None,
    ) -> None:
        callback_mode = str(payload.get("callback_mode") or "controller_public")
        if callback_mode == "executor_loopback":
            if not source_executor_id:
                raise MCPOAuthError("OAuth callback must be completed by the selected executor")
            if source_executor_id != payload.get("oauth_executor_id"):
                raise MCPOAuthError("OAuth callback executor mismatch")
            if listener_id != payload.get("loopback_listener_id"):
                raise MCPOAuthError("OAuth callback listener mismatch")
            if callback_redirect_uri != row.redirect_uri:
                raise MCPOAuthError("OAuth callback redirect URI mismatch")
        elif source_executor_id:
            raise MCPOAuthError("Executor loopback callback is not valid for this transaction")

    async def complete_callback(
        self,
        *,
        state: str,
        code: str,
        source_executor_id: str | None = None,
        listener_id: str | None = None,
        callback_redirect_uri: str | None = None,
    ) -> str:
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

            payload = self._decrypt(row.encrypted_payload)
            if payload.get("flow") == "device_code":
                raise MCPOAuthError("OAuth callback is not valid for device-code transactions")
            self._validate_callback_source(
                row=row,
                payload=payload,
                source_executor_id=source_executor_id,
                listener_id=listener_id,
                callback_redirect_uri=callback_redirect_uri,
            )
            row.status = "exchanging"
            row.used_at = now
            await session.commit()

            try:
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
                token_response["token_endpoint"] = token_endpoint
                if payload.get("client_secret"):
                    token_response["client_secret"] = payload.get("client_secret")
                expires_at = None
                if isinstance(token_response.get("expires_in"), int):
                    expires_at = now + timedelta(seconds=int(token_response["expires_in"]))
                token_response = _record_absolute_refresh_token_expiry(token_response, now)
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
        if self._on_authorization_completed is not None:
            await self._on_authorization_completed(transaction_id)
        return transaction_id

    async def _request_device_authorization(
        self,
        *,
        device_endpoint: str,
        client_id: str,
        client_secret: str | None,
        scopes: list[str],
        resource: str | None,
    ) -> dict[str, Any]:
        data = {"client_id": client_id}
        if client_secret:
            data["client_secret"] = client_secret
        if scopes:
            data["scope"] = " ".join(scopes)
        if resource:
            data["resource"] = resource
        async with httpx.AsyncClient(timeout=_TOKEN_TIMEOUT, follow_redirects=False) as client:
            response = await client.post(device_endpoint, data=data)
        if response.status_code >= 400:
            raise MCPOAuthError(
                _safe_provider_error(response, operation="OAuth device authorization")
            )
        payload = _parse_oauth_json_response(
            response,
            operation="OAuth device authorization",
        )
        return payload

    async def _exchange_device_code(
        self,
        *,
        token_endpoint: str,
        device_code: str,
        client_id: str,
        client_secret: str | None,
        resource: str | None,
    ) -> dict[str, Any]:
        data = {
            "grant_type": _DEVICE_GRANT_TYPE,
            "device_code": device_code,
            "client_id": client_id,
        }
        if client_secret:
            data["client_secret"] = client_secret
        if resource:
            data["resource"] = resource
        async with httpx.AsyncClient(timeout=_TOKEN_TIMEOUT, follow_redirects=False) as client:
            response = await client.post(token_endpoint, data=data)
        if response.status_code >= 400:
            error_value = None
            try:
                error_payload = response.json()
            except ValueError:
                error_payload = None
            if isinstance(error_payload, dict) and isinstance(error_payload.get("error"), str):
                error_value = str(error_payload["error"])
            if error_value in {
                "authorization_pending",
                "slow_down",
                "access_denied",
                "expired_token",
                "temporarily_unavailable",
            }:
                raise MCPOAuthError(error_value, reason=error_value)
            if response.status_code >= 500:
                raise MCPOAuthError(
                    _safe_provider_error(response, operation="OAuth device token exchange"),
                    reason="transient_provider_error",
                )
            raise MCPOAuthError(
                _safe_provider_error(response, operation="OAuth device token exchange")
            )
        payload = _parse_oauth_json_response(
            response,
            operation="OAuth device token exchange",
        )
        if not isinstance(payload, dict) or not payload.get("access_token"):
            raise MCPOAuthError("OAuth token response missing access token")
        return payload

    def _ensure_device_poll_task(self, transaction_id: str) -> None:
        task = self._device_poll_tasks.get(transaction_id)
        if task is not None and not task.done():
            return
        task = asyncio.create_task(self._poll_device_authorization(transaction_id))
        self._device_poll_tasks[transaction_id] = task
        task.add_done_callback(lambda _: self._device_poll_tasks.pop(transaction_id, None))

    async def recover_pending_device_authorizations(self) -> None:
        async with self._session_factory() as session:
            rows = await list_pending_mcp_oauth_transactions(session)
            for row in rows:
                try:
                    payload = self._decrypt(row.encrypted_payload)
                except Exception:
                    continue
                if payload.get("flow") == "device_code":
                    self._ensure_device_poll_task(row.transaction_id)

    async def shutdown(self) -> None:
        tasks = list(self._device_poll_tasks.values())
        self._device_poll_tasks.clear()
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _poll_device_authorization(self, transaction_id: str) -> None:
        interval = 5
        while True:
            async with self._session_factory() as session:
                row = await get_mcp_oauth_transaction(session, transaction_id)
                if row is None or row.status != "pending":
                    return
                now = _utcnow()
                if row.expires_at <= now:
                    row.status = "expired"
                    row.error_code = "expired_token"
                    row.error_description = "OAuth device authorization expired"
                    await session.commit()
                    if row.notification_id and self._notification_service is not None:
                        await self._notification_service.resolve_internal(
                            row.notification_id,
                            "failed",
                            {"transaction_id": transaction_id, "provider": "mcp"},
                        )
                    return
                payload = self._decrypt(row.encrypted_payload)
                if payload.get("flow") != "device_code":
                    return
                interval = int(payload.get("interval") or interval)
                sleep_for = min(max(interval, 1), max((row.expires_at - now).total_seconds(), 0))
            await asyncio.sleep(sleep_for)
            async with self._session_factory() as session:
                row = await get_mcp_oauth_transaction(session, transaction_id)
                if row is None or row.status != "pending":
                    return
                now = _utcnow()
                if row.expires_at <= now:
                    row.status = "expired"
                    row.error_code = "expired_token"
                    row.error_description = "OAuth device authorization expired"
                    await session.commit()
                    if row.notification_id and self._notification_service is not None:
                        await self._notification_service.resolve_internal(
                            row.notification_id,
                            "failed",
                            {"transaction_id": transaction_id, "provider": "mcp"},
                        )
                    return
                payload = self._decrypt(row.encrypted_payload)
                device_code = payload.get("device_code")
                token_endpoint = payload.get("token_endpoint")
                if not isinstance(device_code, str) or not isinstance(token_endpoint, str):
                    row.status = "failed"
                    row.error_code = "invalid_device_transaction"
                    row.error_description = "OAuth device transaction payload is invalid"
                    await session.commit()
                    return
                try:
                    token_response = await self._exchange_device_code(
                        token_endpoint=_safe_url(token_endpoint),
                        device_code=device_code,
                        client_id=row.client_id,
                        client_secret=payload.get("client_secret")
                        if isinstance(payload.get("client_secret"), str)
                        else None,
                        resource=row.resource,
                    )
                except MCPOAuthError as exc:
                    if exc.reason == "authorization_pending":
                        continue
                    if exc.reason == "slow_down":
                        interval += 5
                        payload["interval"] = interval
                        row.encrypted_payload = self._encrypt(payload)
                        await session.commit()
                        continue
                    if exc.reason in {"temporarily_unavailable", "transient_provider_error"}:
                        logger.warning(
                            "mcp oauth: transient device token provider failure",
                            extra={"extra_data": {"transaction_id": transaction_id}},
                        )
                        continue
                    row.status = "failed"
                    row.error_code = exc.reason or "token_exchange_failed"
                    row.error_description = str(exc)[:500]
                    await session.commit()
                    if row.notification_id and self._notification_service is not None:
                        await self._notification_service.resolve_internal(
                            row.notification_id,
                            "failed",
                            {"transaction_id": transaction_id, "provider": "mcp"},
                        )
                    return
                except httpx.HTTPError:
                    logger.warning(
                        "mcp oauth: transient device token polling failure",
                        extra={"extra_data": {"transaction_id": transaction_id}},
                        exc_info=True,
                    )
                    continue
                token_response["token_endpoint"] = token_endpoint
                if payload.get("client_secret"):
                    token_response["client_secret"] = payload.get("client_secret")
                expires_at = None
                if isinstance(token_response.get("expires_in"), int):
                    expires_at = now + timedelta(seconds=int(token_response["expires_in"]))
                token_response = _record_absolute_refresh_token_expiry(token_response, now)
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
                row.used_at = now
                await session.commit()
                if row.notification_id and self._notification_service is not None:
                    await self._notification_service.resolve_internal(
                        row.notification_id,
                        "completed",
                        {"transaction_id": transaction_id, "provider": "mcp"},
                    )
                if self._on_authorization_completed is not None:
                    await self._on_authorization_completed(transaction_id)
                return

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
        payload = _parse_oauth_json_response(response, operation="OAuth token exchange")
        if not isinstance(payload, dict) or not payload.get("access_token"):
            raise MCPOAuthError("OAuth token response missing access token")
        return payload

    async def _refresh_token(
        self,
        *,
        token_endpoint: str,
        client_id: str,
        client_secret: str | None = None,
        refresh_token: str,
        resource: str | None,
    ) -> dict[str, Any]:
        data = {
            "grant_type": "refresh_token",
            "client_id": client_id,
            "refresh_token": refresh_token,
        }
        if client_secret:
            data["client_secret"] = client_secret
        if resource:
            data["resource"] = resource
        try:
            async with httpx.AsyncClient(timeout=_TOKEN_TIMEOUT, follow_redirects=False) as client:
                response = await client.post(token_endpoint, data=data)
        except httpx.HTTPError as exc:
            raise MCPOAuthError(
                "OAuth token refresh backend is unavailable",
                reason="refresh_backend_unavailable",
                retryable=True,
            ) from exc
        if response.status_code >= 400:
            error_value = None
            try:
                error_payload = response.json()
            except ValueError:
                error_payload = None
            if isinstance(error_payload, dict):
                raw_error = error_payload.get("error")
                if isinstance(raw_error, str):
                    error_value = raw_error
            if error_value == "invalid_grant":
                raise MCPOAuthError(
                    "OAuth refresh token is invalid or expired",
                    reason="invalid_grant",
                    authorization_required=True,
                )
            raise MCPOAuthError(
                "OAuth token refresh failed",
                reason="refresh_backend_failed",
                retryable=response.status_code >= 500,
            )
        try:
            payload = response.json()
        except ValueError as exc:
            raise MCPOAuthError(
                "OAuth refresh response was not valid JSON",
                reason="refresh_backend_failed",
            ) from exc
        if not isinstance(payload, dict) or not payload.get("access_token"):
            raise MCPOAuthError(
                "OAuth refresh response missing access token",
                reason="refresh_backend_failed",
            )
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
        resource = auth_config.resource or server.url
        async with self._session_factory() as session:
            row = None
            if issuer:
                row = await get_mcp_oauth_token(
                    session,
                    user_email=user_email,
                    mcp_server_id=server.server_id,
                    issuer=issuer,
                    resource=resource,
                )
            else:
                row = await get_mcp_oauth_token_for_server(
                    session,
                    user_email=user_email,
                    mcp_server_id=server.server_id,
                )
                if row is not None:
                    issuer = str(row.issuer or "").rstrip("/")
                    resource = auth_config.resource or row.resource or server.url
                else:
                    metadata = await self.discover_metadata(server)
                    issuer = str(metadata["issuer"]).rstrip("/")
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
                    authorization_expires_at=authorization.expires_at if authorization else None,
                    flow=getattr(authorization, "flow", None) if authorization else None,
                    verification_uri=getattr(authorization, "verification_uri", None)
                    if authorization
                    else None,
                    verification_uri_complete=getattr(
                        authorization, "verification_uri_complete", None
                    )
                    if authorization
                    else None,
                    user_code=getattr(authorization, "user_code", None) if authorization else None,
                    callback_mode=getattr(authorization, "callback_mode", None)
                    if authorization
                    else None,
                    oauth_executor_id=getattr(authorization, "oauth_executor_id", None)
                    if authorization
                    else None,
                    oauth_executor_name=getattr(authorization, "oauth_executor_name", None)
                    if authorization
                    else None,
                    redirect_uri=getattr(authorization, "redirect_uri", None)
                    if authorization
                    else None,
                    instructions=getattr(authorization, "instructions", None)
                    if authorization
                    else None,
                )
            payload = self._decrypt(row.encrypted_payload)
            now = _utcnow()
            should_refresh = row.expires_at is not None and row.expires_at <= now + timedelta(
                seconds=_REFRESH_SKEW_SECONDS
            )
            if should_refresh:
                async with self._refresh_lock(
                    user_email=user_email,
                    server_id=server.server_id,
                    issuer=issuer,
                    resource=resource,
                ):
                    refresh_row = getattr(session, "refresh", None)
                    if callable(refresh_row):
                        await refresh_row(row)
                    if row.status != "active":
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
                            authorization_url=authorization.authorization_url
                            if authorization
                            else None,
                            transaction_id=authorization.transaction_id if authorization else None,
                            authorization_expires_at=authorization.expires_at
                            if authorization
                            else None,
                            flow=getattr(authorization, "flow", None) if authorization else None,
                            verification_uri=getattr(authorization, "verification_uri", None)
                            if authorization
                            else None,
                            verification_uri_complete=getattr(
                                authorization, "verification_uri_complete", None
                            )
                            if authorization
                            else None,
                            user_code=getattr(authorization, "user_code", None)
                            if authorization
                            else None,
                            callback_mode=getattr(authorization, "callback_mode", None)
                            if authorization
                            else None,
                            oauth_executor_id=getattr(authorization, "oauth_executor_id", None)
                            if authorization
                            else None,
                            oauth_executor_name=getattr(authorization, "oauth_executor_name", None)
                            if authorization
                            else None,
                            redirect_uri=getattr(authorization, "redirect_uri", None)
                            if authorization
                            else None,
                            instructions=getattr(authorization, "instructions", None)
                            if authorization
                            else None,
                        )
                    payload = self._decrypt(row.encrypted_payload)
                    now = _utcnow()
                    should_refresh = (
                        row.expires_at is not None
                        and row.expires_at <= now + timedelta(seconds=_REFRESH_SKEW_SECONDS)
                    )
                    if should_refresh:
                        refresh_token = payload.get("refresh_token")
                        if not isinstance(refresh_token, str) or not refresh_token:
                            return await self._mark_token_invalid_and_start_authorization(
                                session,
                                token_id=row.token_id,
                                headers=headers,
                                context=challenge_context,
                                reason="refresh_token_missing",
                            )
                        try:
                            token_endpoint = payload.get("token_endpoint")
                            if not isinstance(token_endpoint, str) or not token_endpoint:
                                metadata = await self.discover_metadata(server)
                                token_endpoint = str(metadata.get("token_endpoint") or "")
                            refresh_kwargs = {
                                "token_endpoint": _safe_url(token_endpoint),
                                "client_id": row.client_id
                                or auth_config.client_id
                                or f"cognis-mcp-{server.server_id}",
                                "refresh_token": refresh_token,
                                "resource": resource,
                            }
                            if isinstance(payload.get("client_secret"), str):
                                refresh_kwargs["client_secret"] = payload["client_secret"]
                            refreshed = await self._refresh_token(**refresh_kwargs)
                        except MCPOAuthError as exc:
                            if not exc.authorization_required:
                                raise
                            logger.warning(
                                "mcp oauth: refresh token invalid; token marked invalid",
                                extra={
                                    "extra_data": {
                                        "server_id": server.server_id,
                                        "token_id": row.token_id,
                                        "reason": exc.reason or "refresh_failed",
                                    }
                                },
                            )
                            return await self._mark_token_invalid_and_start_authorization(
                                session,
                                token_id=row.token_id,
                                reason=exc.reason or "refresh_failed",
                                headers=headers,
                                context=challenge_context,
                            )
                        if not refreshed.get("refresh_token"):
                            refreshed["refresh_token"] = refresh_token
                        if isinstance(payload.get("client_secret"), str) and not refreshed.get(
                            "client_secret"
                        ):
                            refreshed["client_secret"] = payload["client_secret"]
                        if not _has_refresh_token_expiry_metadata(refreshed):
                            for key in (
                                "refresh_token_expires_at",
                                "refresh_expires_at",
                                "authorization_expires_at",
                            ):
                                if key in payload:
                                    refreshed[key] = payload[key]
                        refreshed["token_endpoint"] = token_endpoint
                        refreshed = _record_absolute_refresh_token_expiry(refreshed, now)
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
                            token_type=str(
                                refreshed.get("token_type") or row.token_type or "Bearer"
                            ),
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


def _safe_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed


def _has_refresh_token_expiry_metadata(token_payload: dict[str, Any]) -> bool:
    return any(
        key in token_payload
        for key in (
            "refresh_token_expires_at",
            "refresh_expires_at",
            "authorization_expires_at",
            "refresh_token_expires_in",
            "refresh_expires_in",
        )
    )


def _record_absolute_refresh_token_expiry(
    token_payload: dict[str, Any], now: datetime
) -> dict[str, Any]:
    if any(
        token_payload.get(key)
        for key in ("refresh_token_expires_at", "refresh_expires_at", "authorization_expires_at")
    ):
        return token_payload
    for key in ("refresh_token_expires_in", "refresh_expires_in"):
        value = token_payload.get(key)
        if isinstance(value, int) and value > 0:
            token_payload["refresh_token_expires_at"] = (now + timedelta(seconds=value)).isoformat()
            return token_payload
    return token_payload


def _future_or_unbounded(value: datetime | None) -> bool:
    if value is None:
        return True
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value > _utcnow()


def _refresh_token_expires_at(row: Any, token_payload: dict[str, Any]) -> datetime | None:
    for key in ("refresh_token_expires_at", "refresh_expires_at", "authorization_expires_at"):
        parsed = _safe_datetime(token_payload.get(key))
        if parsed is not None:
            return parsed
    return None


def oauth_status_payload(
    row: Any | None, token_payload: dict[str, Any] | None = None
) -> dict[str, Any]:
    if row is None:
        return {"connected": False}
    access_token_expires_at = row.expires_at.isoformat() if row.expires_at else None
    refreshable = None
    authorization_expires_at = None
    if token_payload is not None:
        refreshable = bool(token_payload.get("refresh_token"))
        refresh_token_expires_at = _refresh_token_expires_at(row, token_payload)
        authorization_expires_at = (
            refresh_token_expires_at.isoformat() if refresh_token_expires_at else None
        )
    if refreshable is None:
        connected = row.status == "active"
    elif refreshable:
        connected = row.status == "active" and _future_or_unbounded(refresh_token_expires_at)
    else:
        connected = row.status == "active" and _future_or_unbounded(row.expires_at)
    return {
        "connected": connected,
        "issuer": row.issuer,
        "resource": row.resource if row.resource_key != mcp_oauth_resource_key(None) else None,
        "scopes": row.scopes or [],
        "expires_at": access_token_expires_at,
        "access_token_expires_at": access_token_expires_at,
        "authorization_expires_at": authorization_expires_at,
        "refreshable": refreshable if refreshable is not None else True,
        "status": row.status,
    }
