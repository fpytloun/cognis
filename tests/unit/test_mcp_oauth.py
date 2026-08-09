from __future__ import annotations

import asyncio
import base64
import hashlib
import inspect
import json
import socket
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from fastapi import HTTPException

from cognis import bootstrap as bootstrap_module
from cognis.api import app as app_module
from cognis.api.mcp_reconfigure import schedule_mcp_server_executor_reconfigure_for_app
from cognis.api.models import MCPServerCreateRequest
from cognis.api.routes.mcp_oauth import (
    mcp_oauth_callback,
    mcp_oauth_status,
    schedule_mcp_executor_reconfigure_for_app,
)
from cognis.api.routes.notifications import ResolveRequest, resolve_notification
from cognis.api.runtime_support import _resolve_executor_mcp_servers
from cognis.core.mcp_oauth import (
    _AUTHORIZATION_CHALLENGE_KEY,
    _PROTECTED_RESOURCE_METADATA_KEY,
    MCPOAuthError,
    MCPOAuthService,
    OAuthClientRegistration,
    _authorization_server_metadata_url,
    _effective_oauth_resource,
    _effective_oauth_scopes,
    _path_qualified_resource_metadata_url,
    _safe_url,
    _sanitize_provider_description,
    _validate_protected_resource_metadata,
    oauth_status_payload,
    parse_www_authenticate,
)
from cognis.models.tool import MCPServerConfig
from cognis.runtime_context import RuntimeAccessContext
from cognis.store.models import MCPOAuthTokenRow, MCPOAuthTransactionRow


def test_parse_www_authenticate_bearer_challenge() -> None:
    parsed = parse_www_authenticate(
        'Bearer resource_metadata="https://mcp.example/.well-known/oauth-protected-resource", '
        'error="insufficient_scope", scope="tools.read tools.write"'
    )

    assert parsed["resource_metadata"] == "https://mcp.example/.well-known/oauth-protected-resource"
    assert parsed["error"] == "insufficient_scope"
    assert parsed["scope"] == "tools.read tools.write"


def test_provider_description_redacts_quoted_tokens_bearer_and_jwt() -> None:
    sanitized = _sanitize_provider_description(
        'refresh_token="secret-refresh" client_secret: "secret-client" '
        "Authorization: Bearer secret-access "
        "token=eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJ1In0.signature"
    )

    assert "secret-refresh" not in sanitized
    assert "secret-client" not in sanitized
    assert "secret-access" not in sanitized
    assert "eyJhbGci" not in sanitized


def test_bootstrap_uses_timezone_aware_postgres_refresh_timestamps(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _Inspector:
        def get_columns(self, table_name: str) -> list[dict[str, str]]:
            if table_name == "mcp_servers":
                return [{"name": "auth_config"}]
            if table_name == "mcp_oauth_tokens":
                return [
                    {"name": "token_id"},
                    {"name": "refresh_failure_count"},
                    {"name": "last_refresh_error_code"},
                    {"name": "last_refresh_error_description"},
                ]
            return []

    class _Connection:
        dialect = SimpleNamespace(name="postgresql")

        def __init__(self) -> None:
            self.statements: list[str] = []

        def execute(self, statement: object) -> None:
            self.statements.append(str(statement))

    connection = _Connection()
    monkeypatch.setattr(bootstrap_module, "inspect", lambda _connection: _Inspector())
    monkeypatch.setattr(MCPOAuthTokenRow.__table__, "create", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(MCPOAuthTransactionRow.__table__, "create", lambda *_args, **_kwargs: None)
    for index in MCPOAuthTokenRow.__table__.indexes:
        monkeypatch.setattr(index, "create", lambda *_args, **_kwargs: None)
    for index in MCPOAuthTransactionRow.__table__.indexes:
        monkeypatch.setattr(index, "create", lambda *_args, **_kwargs: None)

    bootstrap_module._ensure_mcp_oauth_schema(connection)

    assert (
        "ALTER TABLE mcp_oauth_tokens ADD COLUMN next_refresh_attempt_at TIMESTAMP WITH TIME ZONE"
    ) in connection.statements
    assert (
        "ALTER TABLE mcp_oauth_tokens ADD COLUMN last_refresh_error_at TIMESTAMP WITH TIME ZONE"
    ) in connection.statements


def test_oauth_state_shape_is_base64_json() -> None:
    payload = {"t": "mcpoauth_abc", "s": "state"}
    state = base64.urlsafe_b64encode(json.dumps(payload, separators=(",", ":")).encode()).decode()

    decoded = json.loads(base64.urlsafe_b64decode(state + "==="))
    assert decoded == payload


def test_oauth_status_exposes_access_token_expiry_separately() -> None:
    access_expires_at = datetime.now(UTC) + timedelta(hours=1)
    row = SimpleNamespace(
        status="active",
        issuer="https://issuer.example",
        resource="https://mcp.example/sse",
        resource_key="https://mcp.example/sse",
        scopes=["tools.read"],
        expires_at=access_expires_at,
    )

    payload = oauth_status_payload(row, {"access_token": "redacted"})

    assert payload["connected"] is True
    assert payload["expires_at"] == access_expires_at.isoformat()
    assert payload["access_token_expires_at"] == access_expires_at.isoformat()
    assert payload["authorization_expires_at"] is None
    assert payload["refreshable"] is False


def test_oauth_status_is_authorized_but_not_connected_after_access_expiry() -> None:
    access_expires_at = datetime.now(UTC) - timedelta(minutes=5)
    refresh_expires_at = datetime.now(UTC) + timedelta(days=30)
    row = SimpleNamespace(
        status="active",
        issuer="https://issuer.example",
        resource=None,
        resource_key="",
        scopes=[],
        expires_at=access_expires_at,
    )

    payload = oauth_status_payload(
        row,
        {
            "access_token": "redacted",
            "refresh_token": "redacted-refresh",
            "refresh_token_expires_at": refresh_expires_at.isoformat(),
        },
    )

    assert payload["connected"] is False
    assert payload["authorized"] is True
    assert payload["refresh_state"] == "refresh_due"
    assert payload["access_token_expires_at"] == access_expires_at.isoformat()
    assert payload["authorization_expires_at"] == refresh_expires_at.isoformat()
    assert payload["refreshable"] is True


def test_oauth_status_omits_unknown_authorization_expiry_but_requires_runtime_refresh() -> None:
    access_expires_at = datetime.now(UTC) - timedelta(minutes=5)
    row = SimpleNamespace(
        status="active",
        issuer="https://issuer.example",
        resource=None,
        resource_key="",
        scopes=[],
        expires_at=access_expires_at,
    )

    payload = oauth_status_payload(
        row,
        {
            "access_token": "redacted",
            "refresh_token": "redacted-refresh",
        },
    )

    assert payload["connected"] is False
    assert payload["authorized"] is True
    assert payload["refresh_state"] == "refresh_due"
    assert payload["access_token_expires_at"] == access_expires_at.isoformat()
    assert payload["authorization_expires_at"] is None
    assert payload["refreshable"] is True


@pytest.mark.asyncio
async def test_fetch_json_rejects_redirect_to_private_ip(tmp_path) -> None:
    key_path = tmp_path / "key"
    key_path.write_bytes(base64.urlsafe_b64encode(b"0" * 32))
    service = MCPOAuthService(
        session_factory=AsyncMock(),
        key_path=str(key_path),
        public_base_url="https://cognis.example",
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"location": "http://169.254.169.254/latest"})

    transport = httpx.MockTransport(handler)
    original_client = httpx.AsyncClient

    def client_factory(*args, **kwargs):
        kwargs["transport"] = transport
        return original_client(*args, **kwargs)

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(httpx, "AsyncClient", client_factory)
        mp.setattr(
            socket,
            "getaddrinfo",
            lambda *args, **kwargs: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 443))],
        )
        with pytest.raises(MCPOAuthError, match="private|https"):
            await service._fetch_json(
                "https://issuer.example/.well-known/oauth-authorization-server"
            )


@pytest.mark.asyncio
async def test_fetch_json_wraps_invalid_json_metadata(tmp_path) -> None:
    key_path = tmp_path / "key"
    key_path.write_bytes(base64.urlsafe_b64encode(b"0" * 32))
    service = MCPOAuthService(
        session_factory=AsyncMock(),
        key_path=str(key_path),
        public_base_url="https://cognis.example",
    )

    transport = httpx.MockTransport(
        lambda request: httpx.Response(
            200,
            request=request,
            text="<html>not json</html>",
            headers={"content-type": "text/html"},
        )
    )
    original_client = httpx.AsyncClient

    def client_factory(*args, **kwargs):
        kwargs["transport"] = transport
        return original_client(*args, **kwargs)

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(httpx, "AsyncClient", client_factory)
        mp.setattr(
            socket,
            "getaddrinfo",
            lambda *args, **kwargs: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 443))],
        )
        with pytest.raises(MCPOAuthError, match="not valid JSON"):
            await service._fetch_json(
                "https://issuer.example/.well-known/oauth-authorization-server"
            )
        assert (
            await service._fetch_json(
                "https://issuer.example/.well-known/oauth-authorization-server",
                missing_ok=True,
            )
            is None
        )


@pytest.mark.asyncio
async def test_discover_metadata_falls_back_from_invalid_auth_server_metadata_to_oidc(
    tmp_path,
) -> None:
    key_path = tmp_path / "key"
    key_path.write_bytes(base64.urlsafe_b64encode(b"0" * 32))
    service = MCPOAuthService(
        session_factory=AsyncMock(),
        key_path=str(key_path),
        public_base_url="https://cognis.example",
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/.well-known/oauth-authorization-server":
            return httpx.Response(200, request=request, text="<html>not json</html>")
        if request.url.path == "/.well-known/openid-configuration":
            return httpx.Response(
                200,
                request=request,
                json={
                    "issuer": "https://issuer.example",
                    "authorization_endpoint": "https://issuer.example/auth",
                    "token_endpoint": "https://issuer.example/token",
                },
            )
        return httpx.Response(404, request=request)

    transport = httpx.MockTransport(handler)
    original_client = httpx.AsyncClient

    def client_factory(*args, **kwargs):
        kwargs["transport"] = transport
        return original_client(*args, **kwargs)

    server = SimpleNamespace(
        url="https://mcp.example/mcp/",
        headers={},
        auth_config={"type": "oauth2", "issuer": "https://issuer.example"},
    )

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(httpx, "AsyncClient", client_factory)
        mp.setattr(
            socket,
            "getaddrinfo",
            lambda *args, **kwargs: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 443))],
        )
        metadata = await service.discover_metadata(server)

    assert metadata["issuer"] == "https://issuer.example"
    assert metadata["token_endpoint"] == "https://issuer.example/token"


@pytest.mark.asyncio
async def test_discover_issuer_follows_resource_metadata_challenge(tmp_path) -> None:
    key_path = tmp_path / "key"
    key_path.write_bytes(base64.urlsafe_b64encode(b"0" * 32))
    service = MCPOAuthService(
        session_factory=AsyncMock(),
        key_path=str(key_path),
        public_base_url="https://cognis.example",
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/.well-known/oauth-protected-resource":
            return httpx.Response(200, request=request, text="<html>not json</html>")
        if request.url == "https://mcp.example/mcp/":
            return httpx.Response(
                401,
                request=request,
                headers={
                    "www-authenticate": (
                        'Bearer resource_metadata="https://mcp.example/custom-resource-metadata"'
                    )
                },
            )
        if request.url == "https://mcp.example/custom-resource-metadata":
            return httpx.Response(
                200,
                request=request,
                json={
                    "resource": "https://mcp.example/mcp/",
                    "authorization_servers": ["https://issuer.example"],
                },
            )
        return httpx.Response(404, request=request)

    transport = httpx.MockTransport(handler)
    original_client = httpx.AsyncClient

    def client_factory(*args, **kwargs):
        kwargs["transport"] = transport
        return original_client(*args, **kwargs)

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(httpx, "AsyncClient", client_factory)
        mp.setattr(
            socket,
            "getaddrinfo",
            lambda *args, **kwargs: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 443))],
        )
        issuer = await service._discover_issuer_from_resource("https://mcp.example/mcp/")

    assert issuer == "https://issuer.example"


def test_path_qualified_resource_metadata_url_preserves_resource_path() -> None:
    assert (
        _path_qualified_resource_metadata_url("https://mcp.example/mcp")
        == "https://mcp.example/.well-known/oauth-protected-resource/mcp"
    )
    assert (
        _path_qualified_resource_metadata_url("https://mcp.example/")
        == "https://mcp.example/.well-known/oauth-protected-resource"
    )
    assert (
        _authorization_server_metadata_url("https://issuer.example/tenant")
        == "https://issuer.example/.well-known/oauth-authorization-server/tenant"
    )


@pytest.mark.asyncio
async def test_discover_metadata_preserves_path_qualified_resource_metadata(tmp_path) -> None:
    key_path = tmp_path / "key"
    key_path.write_bytes(base64.urlsafe_b64encode(b"0" * 32))
    service = MCPOAuthService(
        session_factory=AsyncMock(),
        key_path=str(key_path),
        public_base_url="https://cognis.example",
    )
    requested_paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested_paths.append(request.url.path)
        if request.url.path == "/.well-known/oauth-protected-resource/mcp":
            return httpx.Response(
                200,
                request=request,
                json={
                    "resource": "https://mcp.example/mcp",
                    "authorization_servers": ["https://issuer.example"],
                    "scopes_supported": ["openid", "email", "roles", "email"],
                },
            )
        if request.url.path == "/.well-known/oauth-authorization-server":
            return httpx.Response(404, request=request)
        if request.url.path == "/.well-known/openid-configuration":
            return httpx.Response(
                200,
                request=request,
                json={
                    "issuer": "https://issuer.example",
                    "authorization_endpoint": "https://issuer.example/authorize",
                    "token_endpoint": "https://issuer.example/token",
                },
            )
        return httpx.Response(404, request=request)

    transport = httpx.MockTransport(handler)
    original_client = httpx.AsyncClient

    def client_factory(*args, **kwargs):
        kwargs["transport"] = transport
        return original_client(*args, **kwargs)

    server = SimpleNamespace(
        url="https://mcp.example/mcp",
        headers={},
        auth_config={"type": "oauth2"},
    )
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(httpx, "AsyncClient", client_factory)
        mp.setattr(
            socket,
            "getaddrinfo",
            lambda *args, **kwargs: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 443))],
        )
        metadata = await service.discover_metadata(server)

    protected_resource = metadata[_PROTECTED_RESOURCE_METADATA_KEY]
    assert protected_resource == {
        "resource": "https://mcp.example/mcp",
        "authorization_servers": ["https://issuer.example"],
        "scopes_supported": ["openid", "email", "roles"],
    }
    assert metadata[_AUTHORIZATION_CHALLENGE_KEY] == {}
    assert requested_paths[0] == "/.well-known/oauth-protected-resource/mcp"
    assert "/mcp" not in requested_paths
    assert "/.well-known/oauth-protected-resource" not in requested_paths


def test_effective_oauth_scope_precedence() -> None:
    metadata = {
        _AUTHORIZATION_CHALLENGE_KEY: {"scope": "challenge.write challenge.read"},
        _PROTECTED_RESOURCE_METADATA_KEY: {"scopes_supported": ["metadata.read", "metadata.write"]},
    }
    configured = SimpleNamespace(scopes=["configured.read"])

    assert _effective_oauth_scopes(configured, metadata) == [
        "configured.read",
        "challenge.write",
        "challenge.read",
    ]

    metadata[_AUTHORIZATION_CHALLENGE_KEY] = {}
    assert _effective_oauth_scopes(configured, metadata) == ["configured.read"]

    metadata[_AUTHORIZATION_CHALLENGE_KEY] = {"scope": " "}
    assert _effective_oauth_scopes(configured, metadata) == ["configured.read"]

    configured.scopes = []
    assert _effective_oauth_scopes(configured, metadata) == [
        "metadata.read",
        "metadata.write",
    ]

    metadata[_PROTECTED_RESOURCE_METADATA_KEY] = {"scopes_supported": []}
    assert _effective_oauth_scopes(configured, metadata) == []


def test_effective_oauth_resource_rejects_configured_metadata_mismatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *args, **kwargs: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 443))],
    )
    server = SimpleNamespace(url="https://mcp.example/mcp")
    metadata = {
        _PROTECTED_RESOURCE_METADATA_KEY: {
            "resource": "https://mcp.example/mcp",
        }
    }

    with pytest.raises(MCPOAuthError, match="Configured OAuth resource"):
        _effective_oauth_resource(
            server,
            SimpleNamespace(resource="https://mcp.example/other"),
            metadata,
        )


def test_protected_resource_metadata_rejects_fragment_identifier() -> None:
    with pytest.raises(MCPOAuthError, match="fragment"):
        _validate_protected_resource_metadata(
            {
                "resource": "https://mcp.example/mcp#fragment",
                "authorization_servers": [],
            },
            expected_resource="https://mcp.example/mcp",
        )


@pytest.mark.asyncio
async def test_discover_metadata_rejects_mismatched_protected_resource(tmp_path) -> None:
    key_path = tmp_path / "key"
    key_path.write_bytes(base64.urlsafe_b64encode(b"0" * 32))
    service = MCPOAuthService(
        session_factory=AsyncMock(),
        key_path=str(key_path),
        public_base_url="https://cognis.example",
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/.well-known/oauth-protected-resource/mcp":
            return httpx.Response(
                200,
                request=request,
                json={
                    "resource": "https://mcp.example",
                    "authorization_servers": ["https://issuer.example"],
                },
            )
        return httpx.Response(404, request=request)

    transport = httpx.MockTransport(handler)
    original_client = httpx.AsyncClient

    def client_factory(*args, **kwargs):
        kwargs["transport"] = transport
        return original_client(*args, **kwargs)

    server = SimpleNamespace(
        url="https://mcp.example/mcp",
        headers={},
        auth_config={"type": "oauth2"},
    )
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(httpx, "AsyncClient", client_factory)
        mp.setattr(
            socket,
            "getaddrinfo",
            lambda *args, **kwargs: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 443))],
        )
        with pytest.raises(MCPOAuthError, match="resource mismatch"):
            await service.discover_metadata(server)


@pytest.mark.asyncio
async def test_discover_metadata_rejects_configured_issuer_outside_resource_metadata(
    tmp_path,
) -> None:
    key_path = tmp_path / "key"
    key_path.write_bytes(base64.urlsafe_b64encode(b"0" * 32))
    service = MCPOAuthService(
        session_factory=AsyncMock(),
        key_path=str(key_path),
        public_base_url="https://cognis.example",
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/.well-known/oauth-protected-resource/mcp":
            return httpx.Response(
                200,
                request=request,
                json={
                    "resource": "https://mcp.example/mcp",
                    "authorization_servers": ["https://other-issuer.example"],
                },
            )
        return httpx.Response(404, request=request)

    transport = httpx.MockTransport(handler)
    original_client = httpx.AsyncClient

    def client_factory(*args, **kwargs):
        kwargs["transport"] = transport
        return original_client(*args, **kwargs)

    server = SimpleNamespace(
        server_id="mcp-1",
        url="https://mcp.example/mcp",
        headers={},
        auth_config={"type": "oauth2", "issuer": "https://issuer.example"},
    )
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(httpx, "AsyncClient", client_factory)
        mp.setattr(
            socket,
            "getaddrinfo",
            lambda *args, **kwargs: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 443))],
        )
        with pytest.raises(MCPOAuthError, match="does not match"):
            await service.discover_metadata(server)


@pytest.mark.asyncio
async def test_explicit_issuer_and_scopes_do_not_probe_private_mcp_resource(tmp_path) -> None:
    key_path = tmp_path / "key"
    key_path.write_bytes(base64.urlsafe_b64encode(b"0" * 32))
    service = MCPOAuthService(
        session_factory=AsyncMock(),
        key_path=str(key_path),
        public_base_url="https://cognis.example",
    )
    requested_urls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested_urls.append(str(request.url))
        if request.url.path == "/.well-known/oauth-authorization-server":
            return httpx.Response(
                200,
                request=request,
                json={
                    "issuer": "https://issuer.example",
                    "authorization_endpoint": "https://issuer.example/authorize",
                    "token_endpoint": "https://issuer.example/token",
                },
            )
        return httpx.Response(404, request=request)

    transport = httpx.MockTransport(handler)
    original_client = httpx.AsyncClient

    def client_factory(*args, **kwargs):
        kwargs["transport"] = transport
        return original_client(*args, **kwargs)

    server = SimpleNamespace(
        server_id="mcp-1",
        url="https://10.0.0.5/mcp",
        headers={},
        auth_config={
            "type": "oauth2",
            "issuer": "https://issuer.example",
            "scopes": ["tools.read"],
        },
    )
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(httpx, "AsyncClient", client_factory)
        mp.setattr(
            socket,
            "getaddrinfo",
            lambda *args, **kwargs: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 443))],
        )
        metadata = await service.discover_metadata(server)

    assert metadata["issuer"] == "https://issuer.example"
    assert requested_urls == ["https://issuer.example/.well-known/oauth-authorization-server"]


@pytest.mark.asyncio
async def test_runtime_scope_challenge_uses_explicit_issuer_for_private_mcp_resource(
    tmp_path,
) -> None:
    key_path = tmp_path / "key"
    key_path.write_bytes(base64.urlsafe_b64encode(b"0" * 32))
    service = MCPOAuthService(
        session_factory=AsyncMock(),
        key_path=str(key_path),
        public_base_url="https://cognis.example",
    )
    requested_urls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested_urls.append(str(request.url))
        if request.url.path == "/.well-known/oauth-authorization-server":
            return httpx.Response(
                200,
                request=request,
                json={
                    "issuer": "https://issuer.example",
                    "authorization_endpoint": "https://issuer.example/authorize",
                    "token_endpoint": "https://issuer.example/token",
                },
            )
        return httpx.Response(404, request=request)

    transport = httpx.MockTransport(handler)
    original_client = httpx.AsyncClient

    def client_factory(*args, **kwargs):
        kwargs["transport"] = transport
        return original_client(*args, **kwargs)

    server = SimpleNamespace(
        server_id="mcp-1",
        url="https://10.0.0.5/mcp",
        headers={},
        auth_config={
            "type": "oauth2",
            "issuer": "https://issuer.example",
            "scopes": ["configured.read"],
        },
    )
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(httpx, "AsyncClient", client_factory)
        mp.setattr(
            socket,
            "getaddrinfo",
            lambda *args, **kwargs: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 443))],
        )
        metadata = await service.discover_metadata(
            server,
            authorization_challenge={
                "error": "insufficient_scope",
                "scope": "challenge.write",
            },
        )

    assert _effective_oauth_scopes(
        SimpleNamespace(scopes=["configured.read"]),
        metadata,
    ) == ["configured.read", "challenge.write"]
    assert requested_urls == ["https://issuer.example/.well-known/oauth-authorization-server"]


def test_safe_url_rejects_hostname_resolving_to_private_ip(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_getaddrinfo(*args, **kwargs):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.5", 443))]

    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)

    with pytest.raises(MCPOAuthError, match="private|link-local"):
        _safe_url("https://issuer.example/.well-known/oauth-authorization-server")


def test_mcp_oauth_authorization_params_cannot_override_reserved_values() -> None:
    with pytest.raises(ValueError, match="reserved OAuth parameter"):
        MCPServerConfig(
            name="oauth",
            transport="sse",
            url="https://mcp.example/sse",
            auth_config={
                "type": "oauth2",
                "issuer": "https://issuer.example",
                "client_id": "client",
                "authorization_params": {"state": "attacker"},
            },
        )


def test_mcp_server_create_request_rejects_reserved_oauth_authorization_params() -> None:
    with pytest.raises(ValueError, match="reserved OAuth parameter"):
        MCPServerCreateRequest(
            name="oauth",
            transport="sse",
            url="https://mcp.example/sse",
            auth_config={
                "type": "oauth2",
                "issuer": "https://issuer.example",
                "client_id": "client",
                "authorization_params": {"redirect_uri": "https://attacker.example/cb"},
            },
        )


def test_mcp_oauth_config_accepts_executor_loopback_callback_mode() -> None:
    config = MCPServerConfig(
        name="oauth",
        transport="sse",
        url="https://mcp.example/sse",
        auth_config={
            "type": "oauth2",
            "flow": "auto",
            "callback_mode": "executor_loopback",
            "oauth_executor_id": "exec-1",
        },
    )

    assert config.auth_config is not None
    assert config.auth_config.callback_mode == "executor_loopback"
    assert config.auth_config.oauth_executor_id == "exec-1"


class _MemorySession:
    def __init__(self, token=None, server=None) -> None:
        self.token = token
        self.server = server
        self.commits = 0
        self.rollbacks = 0

    async def commit(self) -> None:
        self.commits += 1

    async def rollback(self) -> None:
        self.rollbacks += 1

    async def flush(self) -> None:
        return None

    async def __aenter__(self) -> _MemorySession:
        return self

    async def __aexit__(self, *args: object) -> None:
        return None


class _MemorySessionFactory:
    def __init__(self, session: _MemorySession) -> None:
        self.session = session

    def __call__(self) -> _MemorySession:
        return self.session


def _oauth_service_for_tests(tmp_path, memory_session: _MemorySession) -> MCPOAuthService:
    key_path = tmp_path / "key"
    key_path.write_bytes(base64.urlsafe_b64encode(b"9" * 32))
    return MCPOAuthService(
        session_factory=_MemorySessionFactory(memory_session),
        key_path=str(key_path),
        public_base_url="https://cognis.example",
    )


@pytest.mark.asyncio
async def test_mark_token_invalid_for_server_invalidates_active_oauth_token(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    server = SimpleNamespace(
        server_id="mcp-1",
        headers={},
        auth_config={"type": "oauth2", "issuer": "https://issuer.example"},
    )
    token = SimpleNamespace(token_id="tok-1", status="active")
    memory_session = _MemorySession(token=token, server=server)
    service = _oauth_service_for_tests(tmp_path, memory_session)

    async def fake_get_server(*_args, **_kwargs):
        return memory_session.server

    async def fake_get_token(*_args, **_kwargs):
        return memory_session.token

    async def fake_mark_status(session, *, token_id: str, status: str) -> None:
        assert token_id == "tok-1"
        session.token.status = status

    monkeypatch.setattr("cognis.core.mcp_oauth.get_mcp_server", fake_get_server)
    monkeypatch.setattr("cognis.core.mcp_oauth.get_mcp_oauth_token_for_server", fake_get_token)
    monkeypatch.setattr("cognis.core.mcp_oauth.mark_mcp_oauth_token_status", fake_mark_status)

    assert await service.mark_token_invalid_for_server(
        user_email="alice@example.com", server_id="mcp-1"
    )
    assert token.status == "invalid"
    assert memory_session.commits == 1


@pytest.mark.asyncio
async def test_mark_token_invalid_for_server_requests_retry_when_token_already_invalid(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    server = SimpleNamespace(
        server_id="mcp-1",
        headers={},
        auth_config={"type": "oauth2", "issuer": "https://issuer.example"},
    )
    token = SimpleNamespace(token_id="tok-1", status="invalid")
    memory_session = _MemorySession(token=token, server=server)
    service = _oauth_service_for_tests(tmp_path, memory_session)

    async def fake_get_server(*_args, **_kwargs):
        return memory_session.server

    async def fake_get_token(*_args, **_kwargs):
        return memory_session.token

    mark_status = AsyncMock()
    monkeypatch.setattr("cognis.core.mcp_oauth.get_mcp_server", fake_get_server)
    monkeypatch.setattr("cognis.core.mcp_oauth.get_mcp_oauth_token_for_server", fake_get_token)
    monkeypatch.setattr("cognis.core.mcp_oauth.mark_mcp_oauth_token_status", mark_status)

    assert await service.mark_token_invalid_for_server(
        user_email="alice@example.com", server_id="mcp-1"
    )
    assert token.status == "invalid"
    assert memory_session.commits == 0
    mark_status.assert_not_awaited()


@pytest.mark.asyncio
async def test_mark_token_invalid_for_server_ignores_non_oauth_servers(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    server = SimpleNamespace(
        server_id="mcp-1",
        headers={},
        auth_config={"type": "none"},
    )
    token = SimpleNamespace(token_id="tok-1", status="active")
    memory_session = _MemorySession(token=token, server=server)
    service = _oauth_service_for_tests(tmp_path, memory_session)

    async def fake_get_server(*_args, **_kwargs):
        return memory_session.server

    get_token = AsyncMock()
    monkeypatch.setattr("cognis.core.mcp_oauth.get_mcp_server", fake_get_server)
    monkeypatch.setattr("cognis.core.mcp_oauth.get_mcp_oauth_token_for_server", get_token)

    assert not await service.mark_token_invalid_for_server(
        user_email="alice@example.com", server_id="mcp-1"
    )
    get_token.assert_not_awaited()
    assert token.status == "active"


@pytest.mark.asyncio
async def test_start_authorization_uses_dynamic_client_registration_when_client_id_missing(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    key_path = tmp_path / "key"
    key_path.write_bytes(base64.urlsafe_b64encode(b"2" * 32))
    server = SimpleNamespace(
        server_id="mcp-1",
        name="OAuth MCP",
        transport="sse",
        command=None,
        url="https://mcp.example/sse",
        args=[],
        env={},
        headers={},
        auth_config={"type": "oauth2", "resource": "https://mcp.example/sse"},
        timeout_seconds=30,
    )
    memory_session = _MemorySession(server=server)
    service = MCPOAuthService(
        session_factory=_MemorySessionFactory(memory_session),
        key_path=str(key_path),
        public_base_url="https://cognis.example",
    )
    transaction_rows: list[SimpleNamespace] = []

    async def fake_get_mcp_server(*args, **kwargs):
        return server

    async def fake_get_setting_value(*args, **kwargs):
        return args[2] if len(args) > 2 else kwargs.get("default")

    async def fake_create_transaction(session, **kwargs):
        row = SimpleNamespace(notification_id=None, **kwargs)
        transaction_rows.append(row)
        return row

    monkeypatch.setattr("cognis.core.mcp_oauth.get_mcp_server", fake_get_mcp_server)
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *args, **kwargs: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 443))],
    )
    monkeypatch.setattr(
        "cognis.core.mcp_oauth.create_mcp_oauth_transaction",
        fake_create_transaction,
    )
    monkeypatch.setattr(
        service,
        "discover_metadata",
        AsyncMock(
            return_value={
                "issuer": "https://issuer.example",
                "authorization_endpoint": "https://issuer.example/authorize",
                "token_endpoint": "https://issuer.example/token",
                "registration_endpoint": "https://issuer.example/register",
            }
        ),
    )
    register_dynamic_client = AsyncMock(
        return_value=SimpleNamespace(client_id="registered-client", client_secret=None)
    )
    monkeypatch.setattr(service, "_register_dynamic_client", register_dynamic_client)

    result = await service.start_authorization(
        user_email="alice@example.com",
        server_id="mcp-1",
    )

    assert "client_id=registered-client" in result.authorization_url
    assert "cognis-mcp-" not in result.authorization_url
    assert transaction_rows[0].client_id == "registered-client"
    register_dynamic_client.assert_awaited_once()


@pytest.mark.asyncio
async def test_start_authorization_uses_metadata_scopes_in_url_and_transaction(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    key_path = tmp_path / "key"
    key_path.write_bytes(base64.urlsafe_b64encode(b"2" * 32))
    server = SimpleNamespace(
        server_id="mcp-1",
        name="OAuth MCP",
        transport="streamable_http",
        command=None,
        url="https://mcp.example/mcp",
        args=[],
        env={},
        headers={},
        auth_config={
            "type": "oauth2",
            "issuer": "https://issuer.example",
            "client_id": "configured-client",
            "scopes": [],
        },
        timeout_seconds=30,
    )
    memory_session = _MemorySession(server=server)
    service = MCPOAuthService(
        session_factory=_MemorySessionFactory(memory_session),
        key_path=str(key_path),
        public_base_url="https://cognis.example",
    )
    transaction_rows: list[SimpleNamespace] = []

    async def fake_get_mcp_server(*args, **kwargs):
        return server

    async def fake_create_transaction(session, **kwargs):
        row = SimpleNamespace(notification_id=None, **kwargs)
        transaction_rows.append(row)
        return row

    async def fake_resolve_client_registration(**kwargs):
        assert kwargs["scopes"] == ["openid", "email", "roles"]
        return OAuthClientRegistration(client_id="configured-client")

    monkeypatch.setattr("cognis.core.mcp_oauth.get_mcp_server", fake_get_mcp_server)
    monkeypatch.setattr(
        "cognis.core.mcp_oauth.create_mcp_oauth_transaction",
        fake_create_transaction,
    )
    monkeypatch.setattr(
        service,
        "discover_metadata",
        AsyncMock(
            return_value={
                "issuer": "https://issuer.example",
                "authorization_endpoint": "https://issuer.example/authorize",
                "token_endpoint": "https://issuer.example/token",
                _AUTHORIZATION_CHALLENGE_KEY: {},
                _PROTECTED_RESOURCE_METADATA_KEY: {
                    "resource": "https://mcp.example/mcp",
                    "authorization_servers": ["https://issuer.example"],
                    "scopes_supported": ["openid", "email", "roles"],
                },
            }
        ),
    )
    monkeypatch.setattr(
        service,
        "_resolve_client_registration",
        fake_resolve_client_registration,
    )
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *args, **kwargs: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 443))],
    )

    result = await service.start_authorization(
        user_email="alice@example.com",
        server_id="mcp-1",
    )

    query = parse_qs(urlsplit(result.authorization_url).query)
    assert query["scope"] == ["openid email roles"]
    assert query["resource"] == ["https://mcp.example/mcp"]
    assert query["code_challenge_method"] == ["S256"]
    assert result.scopes == ["openid", "email", "roles"]
    assert result.resource == "https://mcp.example/mcp"
    assert transaction_rows[0].scopes == ["openid", "email", "roles"]
    assert transaction_rows[0].resource == "https://mcp.example/mcp"
    assert (
        service._decrypt(transaction_rows[0].encrypted_payload)["token_endpoint"]
        == "https://issuer.example/token"
    )


@pytest.mark.asyncio
async def test_transaction_token_endpoint_uses_persisted_value_with_legacy_fallback(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    key_path = tmp_path / "key"
    key_path.write_bytes(base64.urlsafe_b64encode(b"2" * 32))
    service = MCPOAuthService(
        session_factory=AsyncMock(),
        key_path=str(key_path),
        public_base_url="https://cognis.example",
    )
    server = SimpleNamespace(url="https://mcp.example/mcp")
    discover_metadata = AsyncMock(
        return_value={"token_endpoint": "https://legacy-issuer.example/token"}
    )
    monkeypatch.setattr(service, "discover_metadata", discover_metadata)
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *args, **kwargs: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 443))],
    )

    assert (
        await service._transaction_token_endpoint(
            server=server,
            transaction_payload={"token_endpoint": "https://issuer.example/token"},
        )
        == "https://issuer.example/token"
    )
    discover_metadata.assert_not_awaited()

    assert (
        await service._transaction_token_endpoint(
            server=server,
            transaction_payload={},
        )
        == "https://legacy-issuer.example/token"
    )
    discover_metadata.assert_awaited_once_with(server)


@pytest.mark.asyncio
async def test_start_authorization_uses_executor_loopback_redirect_uri(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    key_path = tmp_path / "key"
    key_path.write_bytes(base64.urlsafe_b64encode(b"2" * 32))
    server = SimpleNamespace(
        server_id="mcp-1",
        name="OAuth MCP",
        transport="streamable_http",
        command=None,
        url="https://mcp.example/mcp",
        args=[],
        env={},
        headers={},
        auth_config={
            "type": "oauth2",
            "resource": "https://mcp.example/mcp",
            "oauth_executor_id": "exec-1",
            "dynamic_client_registration": True,
        },
        timeout_seconds=30,
    )
    memory_session = _MemorySession(server=server)
    started_states: list[str] = []

    class FakeExecutorConnection:
        async def oauth_loopback_start(
            self, *, state: str, ttl_seconds: int, callback_path: str
        ) -> dict[str, str]:
            started_states.append(state)
            assert ttl_seconds == 900
            assert callback_path == "/oauth/callback"
            return {
                "listener_id": "listener-1",
                "redirect_uri": "http://127.0.0.1:4567/oauth/callback",
                "expires_at": "2026-01-01T00:00:00+00:00",
            }

    class FakeExecutorProvider:
        def get_connection(self, executor_id: str) -> FakeExecutorConnection | None:
            assert executor_id == "exec-1"
            return FakeExecutorConnection()

    service = MCPOAuthService(
        session_factory=_MemorySessionFactory(memory_session),
        key_path=str(key_path),
        public_base_url="https://cognis.example",
        executor_provider=FakeExecutorProvider(),
    )
    transaction_rows: list[SimpleNamespace] = []

    async def fake_get_mcp_server(*args, **kwargs):
        return server

    async def fake_get_executor_row(*args, **kwargs):
        return SimpleNamespace(executor_id="exec-1", name="Olorin")

    async def fake_create_transaction(session, **kwargs):
        row = SimpleNamespace(notification_id=None, **kwargs)
        transaction_rows.append(row)
        return row

    async def fake_resolve_client_registration(**kwargs):
        assert kwargs["redirect_uri"] == "http://127.0.0.1:4567/oauth/callback"
        return OAuthClientRegistration(client_id="registered-client")

    monkeypatch.setattr("cognis.core.mcp_oauth.get_mcp_server", fake_get_mcp_server)
    monkeypatch.setattr("cognis.core.mcp_oauth.get_executor_row", fake_get_executor_row)
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *args, **kwargs: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 443))],
    )
    monkeypatch.setattr(
        "cognis.core.mcp_oauth.create_mcp_oauth_transaction",
        fake_create_transaction,
    )
    monkeypatch.setattr(
        service,
        "discover_metadata",
        AsyncMock(
            return_value={
                "issuer": "https://issuer.example",
                "authorization_endpoint": "https://issuer.example/authorize",
                "registration_endpoint": "https://issuer.example/register",
                "device_authorization_endpoint": "https://issuer.example/device",
            }
        ),
    )
    monkeypatch.setattr(service, "_resolve_client_registration", fake_resolve_client_registration)

    result = await service.start_authorization(
        user_email="alice@example.com",
        server_id="mcp-1",
    )

    query = parse_qs(urlsplit(result.authorization_url).query)
    assert query["redirect_uri"] == ["http://127.0.0.1:4567/oauth/callback"]
    assert result.callback_mode == "executor_loopback"
    assert result.oauth_executor_id == "exec-1"
    assert result.oauth_executor_name == "Olorin"
    assert result.redirect_uri == "http://127.0.0.1:4567/oauth/callback"
    assert started_states == [query["state"][0]]
    assert transaction_rows[0].redirect_uri == "http://127.0.0.1:4567/oauth/callback"
    payload = service._decrypt(transaction_rows[0].encrypted_payload)
    assert payload["callback_mode"] == "executor_loopback"
    assert payload["oauth_executor_id"] == "exec-1"
    assert payload["loopback_listener_id"] == "listener-1"


@pytest.mark.asyncio
async def test_executor_loopback_uses_default_executor_when_not_configured(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    key_path = tmp_path / "key"
    key_path.write_bytes(base64.urlsafe_b64encode(b"2" * 32))
    server = SimpleNamespace(
        server_id="mcp-1",
        name="OAuth MCP",
        transport="streamable_http",
        command=None,
        url="https://mcp.example/mcp",
        args=[],
        env={},
        headers={},
        auth_config={
            "type": "oauth2",
            "resource": "https://mcp.example/mcp",
            "callback_mode": "executor_loopback",
            "client_id": "static-client",
        },
        timeout_seconds=30,
    )
    memory_session = _MemorySession(server=server)

    class FakeExecutorConnection:
        async def oauth_loopback_start(
            self, *, state: str, ttl_seconds: int, callback_path: str
        ) -> dict[str, str]:
            assert state
            assert ttl_seconds == 900
            assert callback_path == "/oauth/callback"
            return {
                "listener_id": "listener-default",
                "redirect_uri": "http://127.0.0.1:5678/oauth/callback",
            }

    class FakeExecutorProvider:
        def get_connection(self, executor_id: str) -> FakeExecutorConnection | None:
            assert executor_id == "default-exec"
            return FakeExecutorConnection()

    service = MCPOAuthService(
        session_factory=_MemorySessionFactory(memory_session),
        key_path=str(key_path),
        public_base_url="https://cognis.example",
        executor_provider=FakeExecutorProvider(),
    )

    async def fake_get_mcp_server(*args, **kwargs):
        return server

    async def fake_list_executors(*args, **kwargs):
        assert kwargs == {"owner_email": "alice@example.com", "include_shared": True}
        return [
            SimpleNamespace(
                executor_id="default-inprocess",
                name="In-process",
                executor_type="in_process",
                is_default=True,
            ),
            SimpleNamespace(
                executor_id="other-exec",
                name="Other Workstation",
                executor_type="websocket",
                is_default=False,
            ),
            SimpleNamespace(
                executor_id="default-exec",
                name="Default Laptop",
                executor_type="websocket",
                is_default=True,
            ),
        ]

    async def fake_create_transaction(session, **kwargs):
        return SimpleNamespace(notification_id=None, **kwargs)

    monkeypatch.setattr("cognis.core.mcp_oauth.get_mcp_server", fake_get_mcp_server)
    monkeypatch.setattr("cognis.core.mcp_oauth.list_executors", fake_list_executors)
    monkeypatch.setattr(
        "cognis.core.mcp_oauth.create_mcp_oauth_transaction", fake_create_transaction
    )
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *args, **kwargs: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 443))],
    )
    monkeypatch.setattr(
        service,
        "discover_metadata",
        AsyncMock(
            return_value={
                "issuer": "https://issuer.example",
                "authorization_endpoint": "https://issuer.example/authorize",
            }
        ),
    )

    result = await service.start_authorization(
        user_email="alice@example.com",
        server_id="mcp-1",
        delivery_mode="silent",
    )

    assert result.callback_mode == "executor_loopback"
    assert result.oauth_executor_id == "default-exec"
    assert result.oauth_executor_name == "Default Laptop"
    assert result.redirect_uri == "http://127.0.0.1:5678/oauth/callback"


@pytest.mark.asyncio
async def test_executor_loopback_callback_rejects_executor_mismatch(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    key_path = tmp_path / "key"
    key_path.write_bytes(base64.urlsafe_b64encode(b"2" * 32))
    memory_session = _MemorySession()
    service = MCPOAuthService(
        session_factory=_MemorySessionFactory(memory_session),
        key_path=str(key_path),
        public_base_url="https://cognis.example",
    )
    state = base64.urlsafe_b64encode(
        json.dumps({"t": "txn-1", "s": "nonce"}, separators=(",", ":")).encode()
    ).decode()
    row = SimpleNamespace(
        transaction_id="txn-1",
        state_hash=hashlib.sha256(state.encode()).hexdigest(),
        notification_id=None,
        status="pending",
        used_at=None,
        expires_at=datetime.now(UTC) + timedelta(minutes=5),
        encrypted_payload=service._encrypt(
            {
                "code_verifier": "verifier",
                "state": state,
                "callback_mode": "executor_loopback",
                "oauth_executor_id": "exec-1",
                "loopback_listener_id": "listener-1",
                "redirect_uri": "http://127.0.0.1:4567/oauth/callback",
            }
        ),
        mcp_server_id="mcp-1",
        user_email="alice@example.com",
        redirect_uri="http://127.0.0.1:4567/oauth/callback",
        client_id="client",
        issuer="https://issuer.example",
        resource=None,
        scopes=[],
    )
    server = SimpleNamespace(
        server_id="mcp-1",
        name="OAuth MCP",
        transport="streamable_http",
        url="https://mcp.example/mcp",
        headers={},
        auth_config={"type": "oauth2", "issuer": "https://issuer.example"},
    )

    async def fake_get_transaction(*args, **kwargs):
        return row

    async def fake_get_mcp_server(*args, **kwargs):
        return server

    monkeypatch.setattr(
        "cognis.core.mcp_oauth.get_mcp_oauth_transaction",
        fake_get_transaction,
    )
    monkeypatch.setattr("cognis.core.mcp_oauth.get_mcp_server", fake_get_mcp_server)
    monkeypatch.setattr(
        service,
        "discover_metadata",
        AsyncMock(
            return_value={
                "issuer": "https://issuer.example",
                "token_endpoint": "http://127.0.0.1/token",
            }
        ),
    )

    with pytest.raises(MCPOAuthError, match="executor mismatch"):
        await service.complete_loopback_callback(
            executor_id="other-exec",
            listener_id="listener-1",
            redirect_uri="http://127.0.0.1:4567/oauth/callback",
            state=state,
            code="code",
        )
    assert row.status == "pending"
    assert row.used_at is None

    row.status = "pending"
    with pytest.raises(MCPOAuthError, match="executor mismatch"):
        await service.complete_loopback_callback(
            executor_id="other-exec",
            listener_id="listener-1",
            redirect_uri="http://127.0.0.1:4567/oauth/callback",
            state=state,
            code=None,
            error="access_denied",
        )
    assert row.status == "pending"

    row.status = "completed"
    with pytest.raises(MCPOAuthError, match="expired or already used"):
        await service.complete_loopback_callback(
            executor_id="exec-1",
            listener_id="listener-1",
            redirect_uri="http://127.0.0.1:4567/oauth/callback",
            state=state,
            code=None,
            error="access_denied",
        )
    assert row.status == "completed"

    row.status = "pending"
    row.used_at = None
    exchange_started = asyncio.Event()

    async def blocked_exchange(**_kwargs):
        exchange_started.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(service, "_exchange_code", blocked_exchange)
    callback = asyncio.create_task(
        service.complete_loopback_callback(
            executor_id="exec-1",
            listener_id="listener-1",
            redirect_uri="http://127.0.0.1:4567/oauth/callback",
            state=state,
            code="code",
        )
    )
    exchange_waiter = asyncio.create_task(exchange_started.wait())
    done, _pending = await asyncio.wait(
        {callback, exchange_waiter},
        timeout=1.0,
        return_when=asyncio.FIRST_COMPLETED,
    )
    if callback in done:
        await callback
    assert exchange_waiter in done
    assert row.status == "exchanging"
    callback.cancel()
    with pytest.raises(asyncio.CancelledError):
        await callback
    assert row.status == "failed"
    assert row.error_code == "callback_cancelled"

    row.status = "pending"
    row.used_at = None
    admission_commit_started = asyncio.Event()
    original_commit = memory_session.commit
    block_commit_once = True

    async def blocked_admission_commit() -> None:
        nonlocal block_commit_once
        if block_commit_once:
            block_commit_once = False
            admission_commit_started.set()
            await asyncio.Event().wait()
        await original_commit()

    monkeypatch.setattr(memory_session, "commit", blocked_admission_commit)
    callback = asyncio.create_task(
        service.complete_loopback_callback(
            executor_id="exec-1",
            listener_id="listener-1",
            redirect_uri="http://127.0.0.1:4567/oauth/callback",
            state=state,
            code="code",
        )
    )
    await asyncio.wait_for(admission_commit_started.wait(), timeout=1.0)
    callback.cancel()
    with pytest.raises(asyncio.CancelledError):
        await callback
    assert row.status != "exchanging"

    row.status = "pending"
    row.used_at = None
    monkeypatch.setattr(
        service,
        "_exchange_code",
        AsyncMock(
            return_value={
                "access_token": "stale-access-token",
                "token_type": "Bearer",
                "expires_in": 900,
            }
        ),
    )
    ownership_check = AsyncMock(side_effect=[True, False, False])
    monkeypatch.setattr(
        "cognis.core.executor_connection_ownership.ExecutorConnectionOwnership.lock_current",
        ownership_check,
    )
    upsert = AsyncMock()
    monkeypatch.setattr("cognis.core.mcp_oauth.upsert_mcp_oauth_token", upsert)

    with pytest.raises(MCPOAuthError, match="ownership changed"):
        await service.complete_loopback_callback(
            connection_owner=object(),
            executor_id="exec-1",
            listener_id="listener-1",
            redirect_uri="http://127.0.0.1:4567/oauth/callback",
            state=state,
            code="code",
        )

    upsert.assert_not_awaited()
    assert row.status != "completed"

    notification_mutations: list[str] = []

    async def guarded_notification_resolve(
        _notification_id,
        decision,
        _data,
        *,
        admission_guard=None,
    ):
        if admission_guard is not None and not await admission_guard(memory_session):
            return False
        notification_mutations.append(decision)
        return True

    service._notification_service = SimpleNamespace(  # noqa: SLF001
        resolve_internal=guarded_notification_resolve
    )
    row.notification_id = "notif-1"
    row.status = "pending"
    row.used_at = None
    row.error_code = None
    row.error_description = None
    exchange_started = asyncio.Event()

    async def blocked_owned_exchange(**_kwargs):
        exchange_started.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(service, "_exchange_code", blocked_owned_exchange)
    cancellation_owner_checks = AsyncMock(side_effect=[True, False])
    monkeypatch.setattr(
        "cognis.core.executor_connection_ownership.ExecutorConnectionOwnership.lock_current",
        cancellation_owner_checks,
    )
    upsert.reset_mock()
    callback = asyncio.create_task(
        service.complete_loopback_callback(
            connection_owner=object(),
            executor_id="exec-1",
            listener_id="listener-1",
            redirect_uri="http://127.0.0.1:4567/oauth/callback",
            state=state,
            code="code",
        )
    )
    await asyncio.wait_for(exchange_started.wait(), timeout=1.0)
    callback.cancel()
    with pytest.raises(asyncio.CancelledError):
        await callback

    upsert.assert_not_awaited()
    assert row.status == "exchanging"
    assert row.error_code is None
    assert notification_mutations == []

    row.status = "pending"
    row.used_at = None
    row.error_code = None
    row.error_description = None
    final_upsert_started = asyncio.Event()

    async def blocked_final_upsert(*_args, **_kwargs):
        final_upsert_started.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(
        service,
        "_exchange_code",
        AsyncMock(
            return_value={
                "access_token": "current-access-token",
                "token_type": "Bearer",
                "expires_in": 900,
            }
        ),
    )
    monkeypatch.setattr(
        "cognis.core.executor_connection_ownership.ExecutorConnectionOwnership.lock_current",
        AsyncMock(return_value=True),
    )
    monkeypatch.setattr(
        "cognis.core.mcp_oauth.upsert_mcp_oauth_token",
        blocked_final_upsert,
    )
    rollback_count = memory_session.rollbacks
    callback = asyncio.create_task(
        service.complete_loopback_callback(
            connection_owner=object(),
            executor_id="exec-1",
            listener_id="listener-1",
            redirect_uri="http://127.0.0.1:4567/oauth/callback",
            state=state,
            code="code",
        )
    )
    await asyncio.wait_for(final_upsert_started.wait(), timeout=1.0)
    callback.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(callback, timeout=1.0)

    assert memory_session.rollbacks > rollback_count
    assert row.status == "failed"
    assert row.error_code == "callback_cancelled"
    assert notification_mutations == []

    row.status = "pending"
    row.used_at = None
    upsert = AsyncMock()
    monkeypatch.setattr("cognis.core.mcp_oauth.upsert_mcp_oauth_token", upsert)
    monkeypatch.setattr(
        service,
        "_exchange_code",
        AsyncMock(
            return_value={
                "access_token": "current-access-token",
                "token_type": "Bearer",
                "expires_in": 900,
            }
        ),
    )
    monkeypatch.setattr(
        "cognis.core.executor_connection_ownership.ExecutorConnectionOwnership.lock_current",
        AsyncMock(side_effect=[True, True, False, False]),
    )
    upsert.reset_mock()

    await service.complete_loopback_callback(
        connection_owner=object(),
        executor_id="exec-1",
        listener_id="listener-1",
        redirect_uri="http://127.0.0.1:4567/oauth/callback",
        state=state,
        code="code",
    )

    upsert.assert_awaited_once()
    assert row.status == "completed"
    assert notification_mutations == []

    row.status = "pending"
    row.used_at = None
    row.error_code = None
    row.error_description = None
    monkeypatch.setattr(
        "cognis.core.executor_connection_ownership.ExecutorConnectionOwnership.lock_current",
        AsyncMock(return_value=False),
    )
    with pytest.raises(MCPOAuthError, match="ownership changed"):
        await service.complete_loopback_callback(
            connection_owner=object(),
            executor_id="exec-1",
            listener_id="listener-1",
            redirect_uri="http://127.0.0.1:4567/oauth/callback",
            state=state,
            code=None,
            error="access_denied",
        )
    assert row.status == "pending"
    assert row.error_code is None
    assert notification_mutations == []


@pytest.mark.parametrize("terminal_status", ["completed", "failed"])
@pytest.mark.asyncio
async def test_terminal_callback_cleanup_recovers_once_for_current_owner(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
    terminal_status: str,
) -> None:
    key_path = tmp_path / "key"
    key_path.write_bytes(base64.urlsafe_b64encode(b"7" * 32))
    session = _MemorySession()
    owner = object()
    notification_resolutions: list[str] = []
    generation_bumps: list[str] = []
    notification_service = SimpleNamespace()

    async def resolve_internal(
        _notification_id,
        decision,
        _data,
        *,
        admission_guard=None,
    ):
        assert admission_guard is not None
        assert await admission_guard(session)
        notification_resolutions.append(decision)
        return True

    notification_service.resolve_internal = resolve_internal
    service = MCPOAuthService(
        session_factory=_MemorySessionFactory(session),
        key_path=str(key_path),
        public_base_url="https://cognis.example",
        notification_service=notification_service,
        executor_provider=SimpleNamespace(
            get_connection=lambda _executor_id: SimpleNamespace(connection_owner=owner)
        ),
    )
    row = SimpleNamespace(
        transaction_id=f"txn-{terminal_status}",
        status=terminal_status,
        terminal_cleanup_required=True,
        notification_id=f"notif-{terminal_status}",
        terminal_notification_resolved_at=None,
        terminal_reconfigure_applied_at=None,
        terminal_reconfigure_completed_at=None,
        encrypted_payload=service._encrypt(
            {
                "callback_mode": "executor_loopback",
                "oauth_executor_id": "exec-1",
            }
        ),
        updated_at=datetime.now(UTC),
    )

    async def fake_list_cleanup(_session):
        return [row]

    async def fake_get_transaction(_session, transaction_id):
        assert transaction_id == row.transaction_id
        return row

    async def apply_generation(
        transaction_id,
        *,
        admission_guard=None,
        terminal_cleanup=False,
    ):
        assert transaction_id == row.transaction_id
        assert admission_guard is not None
        assert terminal_cleanup is True
        assert await admission_guard(session)
        if row.terminal_reconfigure_applied_at is None:
            generation_bumps.append(transaction_id)
            row.terminal_reconfigure_applied_at = datetime.now(UTC)
        row.terminal_reconfigure_completed_at = datetime.now(UTC)

    service._on_authorization_completed = apply_generation
    monkeypatch.setattr(
        "cognis.core.mcp_oauth.list_mcp_oauth_transactions_pending_terminal_cleanup",
        fake_list_cleanup,
    )
    monkeypatch.setattr(
        "cognis.core.mcp_oauth.get_mcp_oauth_transaction",
        fake_get_transaction,
    )
    monkeypatch.setattr(
        "cognis.core.executor_connection_ownership.ExecutorConnectionOwnership.lock_current",
        AsyncMock(return_value=True),
    )

    first = await service.recover_terminal_callback_cleanup()
    second = await service.recover_terminal_callback_cleanup()

    assert first == 1
    assert second == 1
    assert notification_resolutions == ["completed" if terminal_status == "completed" else "failed"]
    assert generation_bumps == [row.transaction_id]
    assert row.terminal_notification_resolved_at is not None
    assert row.terminal_reconfigure_applied_at is not None
    assert row.terminal_reconfigure_completed_at is not None


@pytest.mark.asyncio
async def test_auto_flow_preserves_pkce_when_client_id_configured_even_with_device_metadata(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    key_path = tmp_path / "key"
    key_path.write_bytes(base64.urlsafe_b64encode(b"2" * 32))
    server = SimpleNamespace(
        server_id="mcp-1",
        name="OAuth MCP",
        transport="streamable_http",
        command=None,
        url="https://mcp.example/mcp",
        args=[],
        env={},
        headers={},
        auth_config={
            "type": "oauth2",
            "client_id": "configured-client",
            "resource": "https://mcp.example/mcp",
        },
        timeout_seconds=30,
    )
    memory_session = _MemorySession(server=server)
    notification_service = SimpleNamespace(
        create=AsyncMock(return_value=SimpleNamespace(notification_id="notif-auth-code"))
    )
    service = MCPOAuthService(
        session_factory=_MemorySessionFactory(memory_session),
        key_path=str(key_path),
        public_base_url="https://cognis.example",
        notification_service=notification_service,
    )
    transaction_rows: list[SimpleNamespace] = []

    async def fake_get_mcp_server(*args, **kwargs):
        return server

    async def fake_create_transaction(session, **kwargs):
        row = SimpleNamespace(notification_id=None, **kwargs)
        transaction_rows.append(row)
        return row

    monkeypatch.setattr("cognis.core.mcp_oauth.get_mcp_server", fake_get_mcp_server)
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *args, **kwargs: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 443))],
    )
    monkeypatch.setattr(
        "cognis.core.mcp_oauth.create_mcp_oauth_transaction",
        fake_create_transaction,
    )
    monkeypatch.setattr(
        service,
        "discover_metadata",
        AsyncMock(
            return_value={
                "issuer": "https://issuer.example",
                "authorization_endpoint": "https://issuer.example/authorize",
                "token_endpoint": "https://issuer.example/token",
                "device_authorization_endpoint": "https://issuer.example/device",
                "registration_endpoint": "https://issuer.example/register",
            }
        ),
    )
    request_device_authorization = AsyncMock()
    monkeypatch.setattr(service, "_request_device_authorization", request_device_authorization)

    result = await service.start_authorization(
        user_email="alice@example.com",
        server_id="mcp-1",
        delivery_mode="silent",
    )

    assert result.flow == "authorization_code"
    assert "response_type=code" in result.authorization_url
    assert "client_id=configured-client" in result.authorization_url
    assert "code_challenge=" in result.authorization_url
    assert transaction_rows[0].redirect_uri == "https://cognis.example/api/v1/mcp/oauth/callback"
    assert transaction_rows[0].code_challenge
    request_device_authorization.assert_not_awaited()
    notification_service.create.assert_awaited_once()
    assert "suppress_event" not in notification_service.create.await_args.kwargs


def test_oauth_flow_selection_supports_forced_modes(tmp_path) -> None:
    key_path = tmp_path / "key"
    key_path.write_bytes(base64.urlsafe_b64encode(b"2" * 32))
    service = MCPOAuthService(
        session_factory=AsyncMock(),
        key_path=str(key_path),
        public_base_url="https://cognis.example",
    )
    auth_config = SimpleNamespace(client_id=None, redirect_uri=None)

    assert (
        service._select_flow(
            requested_flow="authorization_code",
            auth_config=auth_config,
            metadata={"device_authorization_endpoint": "https://issuer.example/device"},
        )
        == "authorization_code"
    )
    assert (
        service._select_flow(
            requested_flow="device_code",
            auth_config=auth_config,
            metadata={"device_authorization_endpoint": "https://issuer.example/device"},
        )
        == "device_code"
    )
    with pytest.raises(MCPOAuthError, match="device-code flow is not available"):
        service._select_flow(
            requested_flow="device_code",
            auth_config=auth_config,
            metadata={},
        )


def test_app_lifespan_wires_mcp_oauth_recovery_and_shutdown() -> None:
    source = inspect.getsource(app_module.create_app)

    assert "on_authorization_completed=_on_mcp_oauth_completed" in source
    assert "await mcp_oauth_service.recover_pending_device_authorizations()" in source
    assert "await mcp_oauth_service.recover_terminal_callback_cleanup()" in source
    assert "await mcp_oauth_service.shutdown()" in source


@pytest.mark.asyncio
async def test_oauth_authorization_notification_replies_do_not_complete(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    notification = SimpleNamespace(
        notification_id="notif-1",
        user_email="alice@example.com",
        notification_type="auth_challenge",
        payload={"kind": "oauth_authorization", "flow": "device_code"},
    )
    service = SimpleNamespace(get=AsyncMock(return_value=notification))
    request = SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace(notification_service=service)),
        state=SimpleNamespace(user=SimpleNamespace(email="alice@example.com")),
    )

    monkeypatch.setattr(
        "cognis.api.routes.notifications.require_current_user",
        lambda request: SimpleNamespace(email="alice@example.com", role="user"),
    )
    monkeypatch.setattr(
        "cognis.api.common.require_current_user",
        lambda request: SimpleNamespace(email="alice@example.com", role="user"),
    )

    with pytest.raises(HTTPException) as exc:
        await resolve_notification(
            request,
            "notif-1",
            ResolveRequest(decision="approve", response="done"),
        )

    assert exc.value.status_code == 400
    assert "provider authorization flow" in exc.value.detail
    service.get.assert_awaited_once_with("notif-1")


@pytest.mark.asyncio
async def test_dynamic_client_registration_surfaces_sanitized_provider_error(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    key_path = tmp_path / "key"
    key_path.write_bytes(base64.urlsafe_b64encode(b"2" * 32))
    service = MCPOAuthService(
        session_factory=AsyncMock(),
        key_path=str(key_path),
        public_base_url="https://cognis.example",
    )
    secret = "super-secret-sentinel"
    transport = httpx.MockTransport(
        lambda request: httpx.Response(
            400,
            request=request,
            json={
                "error": "invalid_request",
                "error_description": (
                    "Redirect URI is not allowed for dynamic client registration. "
                    "Got: https://cognis.example/api/v1/mcp/oauth/callback "
                    f"client_secret={secret}"
                ),
            },
        )
    )
    original_client = httpx.AsyncClient

    def client_factory(*args, **kwargs):
        kwargs["transport"] = transport
        return original_client(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", client_factory)
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *args, **kwargs: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 443))],
    )

    with pytest.raises(MCPOAuthError) as exc_info:
        await service._register_dynamic_client(
            registration_endpoint="https://issuer.example/register",
            redirect_uri="https://cognis.example/api/v1/mcp/oauth/callback",
            scopes=["tools.read"],
            client_metadata_document_url=None,
        )

    message = str(exc_info.value)
    assert "HTTP 400" in message
    assert "invalid_request" in message
    assert "Redirect URI is not allowed" in message
    assert secret not in message


@pytest.mark.asyncio
async def test_start_authorization_uses_device_code_without_redirect_uri(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    key_path = tmp_path / "key"
    key_path.write_bytes(base64.urlsafe_b64encode(b"2" * 32))
    server = SimpleNamespace(
        server_id="mcp-1",
        name="Rohlik MCP",
        transport="streamable_http",
        command=None,
        url="https://mcp.rohlik.cz/mcp",
        args=[],
        env={},
        headers={},
        auth_config={"type": "oauth2", "resource": "https://mcp.rohlik.cz/mcp"},
        timeout_seconds=30,
    )
    memory_session = _MemorySession(server=server)
    notification_service = SimpleNamespace(
        create=AsyncMock(return_value=SimpleNamespace(notification_id="notif-device-code"))
    )
    service = MCPOAuthService(
        session_factory=_MemorySessionFactory(memory_session),
        key_path=str(key_path),
        public_base_url="https://cognis.example",
        notification_service=notification_service,
    )
    transaction_rows: list[SimpleNamespace] = []
    posted: list[tuple[str, dict[str, str]]] = []

    async def fake_get_mcp_server(*args, **kwargs):
        return server

    async def fake_create_transaction(session, **kwargs):
        row = SimpleNamespace(notification_id=None, **kwargs)
        transaction_rows.append(row)
        return row

    async def fake_request_device_authorization(**kwargs):
        posted.append(("device", kwargs))
        return {
            "device_code": "device-secret",
            "user_code": "ABCD-EFGH",
            "verification_uri": "https://identity.rohlik.cz/device",
            "verification_uri_complete": "https://identity.rohlik.cz/device?user_code=ABCD-EFGH",
            "expires_in": 600,
            "interval": 5,
        }

    register_dynamic_client = AsyncMock(
        return_value=SimpleNamespace(client_id="device-client", client_secret=None)
    )
    monkeypatch.setattr("cognis.core.mcp_oauth.get_mcp_server", fake_get_mcp_server)
    monkeypatch.setattr(
        "cognis.core.mcp_oauth.create_mcp_oauth_transaction", fake_create_transaction
    )
    monkeypatch.setattr(
        service,
        "discover_metadata",
        AsyncMock(
            return_value={
                "issuer": "https://identity.rohlik.cz",
                "token_endpoint": "https://identity.rohlik.cz/connect/token",
                "registration_endpoint": "https://identity.rohlik.cz/connect/register",
                "device_authorization_endpoint": "https://identity.rohlik.cz/connect/device",
                _AUTHORIZATION_CHALLENGE_KEY: {},
                _PROTECTED_RESOURCE_METADATA_KEY: {
                    "resource": "https://mcp.rohlik.cz/mcp",
                    "authorization_servers": ["https://identity.rohlik.cz"],
                    "scopes_supported": ["openid", "email", "roles"],
                },
            }
        ),
    )
    monkeypatch.setattr(service, "_register_dynamic_client", register_dynamic_client)
    monkeypatch.setattr(service, "_request_device_authorization", fake_request_device_authorization)
    monkeypatch.setattr(service, "_ensure_device_poll_task", lambda transaction_id: None)

    result = await service.start_authorization(
        user_email="alice@example.com",
        server_id="mcp-1",
        delivery_mode="silent",
    )

    assert result.flow == "device_code"
    assert result.authorization_url == "https://identity.rohlik.cz/device?user_code=ABCD-EFGH"
    assert result.user_code == "ABCD-EFGH"
    assert transaction_rows[0].redirect_uri == ""
    assert transaction_rows[0].code_challenge == ""
    assert transaction_rows[0].state_hash == ""
    assert "device-secret" not in result.authorization_url
    register_dynamic_client.assert_awaited_once()
    assert register_dynamic_client.await_args.kwargs["redirect_uri"] is None
    assert register_dynamic_client.await_args.kwargs["flow"] == "device_code"
    assert register_dynamic_client.await_args.kwargs["scopes"] == ["openid", "email", "roles"]
    assert posted[0][1]["client_id"] == "device-client"
    assert posted[0][1]["scopes"] == ["openid", "email", "roles"]
    assert posted[0][1]["resource"] == "https://mcp.rohlik.cz/mcp"
    assert transaction_rows[0].scopes == ["openid", "email", "roles"]
    assert result.scopes == ["openid", "email", "roles"]
    assert result.resource == "https://mcp.rohlik.cz/mcp"
    notification_service.create.assert_awaited_once()
    assert "suppress_event" not in notification_service.create.await_args.kwargs


@pytest.mark.asyncio
async def test_oauth_status_exposes_safe_pending_device_authorization(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    key_path = tmp_path / "key"
    key_path.write_bytes(base64.urlsafe_b64encode(b"2" * 32))
    service = MCPOAuthService(
        session_factory=AsyncMock(),
        key_path=str(key_path),
        public_base_url="https://cognis.example",
    )
    server = SimpleNamespace(
        server_id="mcp-1",
        url="https://mcp.example/mcp",
        headers={},
        auth_config={"type": "oauth2", "issuer": "https://issuer.example"},
    )
    pending = SimpleNamespace(
        transaction_id="device-tx",
        expires_at=datetime.now(UTC) + timedelta(minutes=5),
        encrypted_payload=service._encrypt(
            {
                "flow": "device_code",
                "verification_uri": "https://issuer.example/verify",
                "verification_uri_complete": "https://issuer.example/verify?user_code=ABCD",
                "user_code": "ABCD",
                "interval": 5,
                "device_code": "device-secret",
                "access_token": "access-secret",
                "refresh_token": "refresh-secret",
                "client_secret": "client-secret",
            }
        ),
    )
    request = SimpleNamespace(
        app=SimpleNamespace(
            state=SimpleNamespace(
                session_factory=_MemorySessionFactory(_MemorySession(server=server)),
                mcp_oauth_service=service,
            )
        )
    )

    monkeypatch.setattr(
        "cognis.api.routes.mcp_oauth.require_current_user",
        lambda request: SimpleNamespace(email="alice@example.com"),
    )
    monkeypatch.setattr(
        "cognis.api.routes.mcp_oauth.get_mcp_server",
        AsyncMock(return_value=server),
    )
    monkeypatch.setattr(
        "cognis.api.routes.mcp_oauth.get_mcp_oauth_token",
        AsyncMock(return_value=None),
    )
    monkeypatch.setattr(
        "cognis.api.routes.mcp_oauth.list_pending_mcp_oauth_transactions",
        AsyncMock(return_value=[pending]),
    )
    monkeypatch.setattr(
        "cognis.api.routes.mcp_oauth.list_websocket_executors_for_mcp_server",
        AsyncMock(return_value=[]),
    )

    payload = await mcp_oauth_status(request, "mcp-1")

    assert payload["pending_authorization"] == {
        "flow": "device_code",
        "transaction_id": "device-tx",
        "verification_uri": "https://issuer.example/verify",
        "verification_uri_complete": "https://issuer.example/verify?user_code=ABCD",
        "user_code": "ABCD",
        "expires_at": pending.expires_at.isoformat(),
        "interval": 5,
    }
    serialized = json.dumps(payload)
    assert "device-secret" not in serialized
    assert "access-secret" not in serialized
    assert "refresh-secret" not in serialized
    assert "client-secret" not in serialized
    assert '"device_code":' not in serialized


@pytest.mark.asyncio
async def test_device_dynamic_client_registration_sends_loopback_redirect_uri(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    key_path = tmp_path / "key"
    key_path.write_bytes(base64.urlsafe_b64encode(b"2" * 32))
    service = MCPOAuthService(
        session_factory=AsyncMock(),
        key_path=str(key_path),
        public_base_url="https://cognis.example",
    )
    captured_payloads: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured_payloads.append(json.loads(request.content.decode()))
        return httpx.Response(
            201,
            request=request,
            json={
                "client_id": "device-client",
                "grant_types": ["urn:ietf:params:oauth:grant-type:device_code"],
            },
        )

    transport = httpx.MockTransport(handler)
    original_client = httpx.AsyncClient

    def client_factory(*args, **kwargs):
        kwargs["transport"] = transport
        return original_client(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", client_factory)
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *args, **kwargs: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 443))],
    )

    client = await service._register_dynamic_client(
        registration_endpoint="https://issuer.example/register",
        redirect_uri=None,
        scopes=["tools.read"],
        client_metadata_document_url=None,
        flow="device_code",
    )

    assert client.client_id == "device-client"
    assert len(captured_payloads) == 1
    assert captured_payloads[0]["redirect_uris"] == ["http://127.0.0.1/oauth/callback"]
    assert "https://cognis.example/api/v1/mcp/oauth/callback" not in json.dumps(
        captured_payloads[0]
    )
    assert captured_payloads[0]["grant_types"] == [
        "urn:ietf:params:oauth:grant-type:device_code",
        "refresh_token",
    ]


@pytest.mark.asyncio
async def test_device_dynamic_client_registration_retries_empty_then_omitted_redirect_uris(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    key_path = tmp_path / "key"
    key_path.write_bytes(base64.urlsafe_b64encode(b"2" * 32))
    service = MCPOAuthService(
        session_factory=AsyncMock(),
        key_path=str(key_path),
        public_base_url="https://cognis.example",
    )
    captured_payloads: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured_payloads.append(json.loads(request.content.decode()))
        if len(captured_payloads) < 3:
            return httpx.Response(
                400,
                request=request,
                json={
                    "error": "invalid_client_metadata",
                    "error_description": "redirect_uris not accepted",
                },
            )
        return httpx.Response(
            201,
            request=request,
            json={
                "client_id": "device-client",
                "grant_types": ["urn:ietf:params:oauth:grant-type:device_code"],
            },
        )

    transport = httpx.MockTransport(handler)
    original_client = httpx.AsyncClient

    def client_factory(*args, **kwargs):
        kwargs["transport"] = transport
        return original_client(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", client_factory)
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *args, **kwargs: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 443))],
    )

    client = await service._register_dynamic_client(
        registration_endpoint="https://issuer.example/register",
        redirect_uri=None,
        scopes=["tools.read"],
        client_metadata_document_url=None,
        flow="device_code",
    )

    assert client.client_id == "device-client"
    assert captured_payloads[0]["redirect_uris"] == ["http://127.0.0.1/oauth/callback"]
    assert captured_payloads[1]["redirect_uris"] == []
    assert "redirect_uris" not in captured_payloads[2]
    assert captured_payloads[2]["grant_types"] == [
        "urn:ietf:params:oauth:grant-type:device_code",
        "refresh_token",
    ]


@pytest.mark.asyncio
async def test_dynamic_client_registration_wraps_non_json_success(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    key_path = tmp_path / "key"
    key_path.write_bytes(base64.urlsafe_b64encode(b"2" * 32))
    service = MCPOAuthService(
        session_factory=AsyncMock(),
        key_path=str(key_path),
        public_base_url="https://cognis.example",
    )
    transport = httpx.MockTransport(
        lambda request: httpx.Response(
            201,
            request=request,
            text="",
            headers={"content-type": "text/html"},
        )
    )
    original_client = httpx.AsyncClient

    def client_factory(*args, **kwargs):
        kwargs["transport"] = transport
        return original_client(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", client_factory)
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *args, **kwargs: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 443))],
    )

    with pytest.raises(
        MCPOAuthError, match="dynamic client registration response was not valid JSON"
    ):
        await service._register_dynamic_client(
            registration_endpoint="https://issuer.example/register",
            redirect_uri=None,
            scopes=[],
            client_metadata_document_url=None,
            flow="device_code",
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "response_payload",
    [
        {
            "client_id": "auth-code-client",
            "grant_types": ["refresh_token", "authorization_code"],
            "redirect_uris": ["http://127.0.0.1/oauth/callback"],
        },
        {"client_id": "unknown-grants-client"},
    ],
)
async def test_device_dynamic_client_registration_rejects_auth_code_only_client(
    tmp_path, monkeypatch: pytest.MonkeyPatch, response_payload: dict[str, object]
) -> None:
    key_path = tmp_path / "key"
    key_path.write_bytes(base64.urlsafe_b64encode(b"2" * 32))
    service = MCPOAuthService(
        session_factory=AsyncMock(),
        key_path=str(key_path),
        public_base_url="https://cognis.example",
    )
    transport = httpx.MockTransport(
        lambda request: httpx.Response(
            201,
            request=request,
            json=response_payload,
        )
    )
    original_client = httpx.AsyncClient

    def client_factory(*args, **kwargs):
        kwargs["transport"] = transport
        return original_client(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", client_factory)
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *args, **kwargs: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 443))],
    )

    with pytest.raises(
        MCPOAuthError,
        match="did not register a device-code client",
    ):
        await service._register_dynamic_client(
            registration_endpoint="https://issuer.example/register",
            redirect_uri=None,
            scopes=[],
            client_metadata_document_url=None,
            flow="device_code",
        )


@pytest.mark.asyncio
async def test_device_authorization_wraps_non_json_success(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    key_path = tmp_path / "key"
    key_path.write_bytes(base64.urlsafe_b64encode(b"2" * 32))
    service = MCPOAuthService(
        session_factory=AsyncMock(),
        key_path=str(key_path),
        public_base_url="https://cognis.example",
    )
    transport = httpx.MockTransport(
        lambda request: httpx.Response(
            200,
            request=request,
            text="",
            headers={"content-type": "text/plain"},
        )
    )
    original_client = httpx.AsyncClient

    def client_factory(*args, **kwargs):
        kwargs["transport"] = transport
        return original_client(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", client_factory)

    with pytest.raises(MCPOAuthError, match="device authorization response was not valid JSON"):
        await service._request_device_authorization(
            device_endpoint="https://issuer.example/device",
            client_id="device-client",
            client_secret=None,
            scopes=[],
            resource=None,
        )


@pytest.mark.asyncio
async def test_device_token_exchange_marks_http_5xx_retryable(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    key_path = tmp_path / "key"
    key_path.write_bytes(base64.urlsafe_b64encode(b"2" * 32))
    service = MCPOAuthService(
        session_factory=AsyncMock(),
        key_path=str(key_path),
        public_base_url="https://cognis.example",
    )
    transport = httpx.MockTransport(
        lambda request: httpx.Response(
            503,
            request=request,
            json={"error": "server_error", "error_description": "try later"},
        )
    )
    original_client = httpx.AsyncClient

    def client_factory(*args, **kwargs):
        kwargs["transport"] = transport
        return original_client(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", client_factory)

    with pytest.raises(MCPOAuthError) as exc_info:
        await service._exchange_device_code(
            token_endpoint="https://issuer.example/token",
            device_code="device-secret",
            client_id="device-client",
            client_secret=None,
            resource="https://mcp.example/mcp",
        )

    assert exc_info.value.reason == "transient_provider_error"
    assert "HTTP 503" in str(exc_info.value)


@pytest.mark.asyncio
async def test_device_code_polling_handles_pending_and_stores_token(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    key_path = tmp_path / "key"
    key_path.write_bytes(base64.urlsafe_b64encode(b"2" * 32))
    completed: list[str] = []
    service = MCPOAuthService(
        session_factory=AsyncMock(),
        key_path=str(key_path),
        public_base_url="https://cognis.example",
        on_authorization_completed=lambda tx: completed.append(tx) or asyncio.sleep(0),
    )
    row = SimpleNamespace(
        transaction_id="mcpoauth_device",
        user_email="alice@example.com",
        mcp_server_id="mcp-1",
        issuer="https://issuer.example",
        resource="https://mcp.example/mcp",
        scopes=["tools.read"],
        client_id="device-client",
        status="pending",
        expires_at=datetime.now(UTC) + timedelta(minutes=5),
        notification_id=None,
        encrypted_payload=service._encrypt(
            {
                "flow": "device_code",
                "device_code": "device-secret",
                "client_secret": "stored-client-secret",
                "token_endpoint": "https://issuer.example/token",
                "interval": 1,
            }
        ),
        used_at=None,
        error_code=None,
        error_description=None,
    )
    memory_session = _MemorySession()
    service._session_factory = _MemorySessionFactory(memory_session)
    token_payloads: list[dict[str, object]] = []

    async def fake_get_transaction(session, transaction_id):
        assert transaction_id == row.transaction_id
        return row

    exchange = AsyncMock(
        side_effect=[
            MCPOAuthError("authorization_pending", reason="authorization_pending"),
            MCPOAuthError("slow_down", reason="slow_down"),
            httpx.ConnectError("temporary outage"),
            MCPOAuthError(
                "OAuth device token exchange failed: HTTP 503", reason="transient_provider_error"
            ),
            {
                "access_token": "access-token",
                "refresh_token": "refresh-token",
                "token_type": "Bearer",
                "expires_in": 3600,
            },
        ]
    )

    async def fake_upsert(session, **kwargs):
        token_payloads.append(service._decrypt(kwargs["encrypted_payload"]))
        return SimpleNamespace(**kwargs)

    monkeypatch.setattr("cognis.core.mcp_oauth.get_mcp_oauth_transaction", fake_get_transaction)
    monkeypatch.setattr("cognis.core.mcp_oauth.upsert_mcp_oauth_token", fake_upsert)
    monkeypatch.setattr(service, "_exchange_device_code", exchange)
    monkeypatch.setattr(asyncio, "sleep", AsyncMock())
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *args, **kwargs: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 443))],
    )

    await service._poll_device_authorization(row.transaction_id)

    assert row.status == "completed"
    assert token_payloads[0]["access_token"] == "access-token"
    assert token_payloads[0]["refresh_token"] == "refresh-token"
    assert token_payloads[0]["client_secret"] == "stored-client-secret"
    assert completed == [row.transaction_id]
    assert exchange.await_count == 5
    assert service._decrypt(row.encrypted_payload)["interval"] == 6
    for call in exchange.await_args_list:
        assert call.kwargs["client_secret"] == "stored-client-secret"


@pytest.mark.asyncio
async def test_device_code_polling_resolves_notification_when_expired_after_sleep(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    key_path = tmp_path / "key"
    key_path.write_bytes(base64.urlsafe_b64encode(b"2" * 32))
    notification_service = SimpleNamespace(resolve_internal=AsyncMock())
    service = MCPOAuthService(
        session_factory=AsyncMock(),
        key_path=str(key_path),
        public_base_url="https://cognis.example",
        notification_service=notification_service,
    )
    row = SimpleNamespace(
        transaction_id="mcpoauth_expiring",
        user_email="alice@example.com",
        mcp_server_id="mcp-1",
        issuer="https://issuer.example",
        resource="https://mcp.example/mcp",
        scopes=["tools.read"],
        client_id="device-client",
        status="pending",
        expires_at=datetime.now(UTC) + timedelta(seconds=30),
        notification_id="notif-1",
        encrypted_payload=service._encrypt(
            {
                "flow": "device_code",
                "device_code": "device-secret",
                "token_endpoint": "https://issuer.example/token",
                "interval": 1,
            }
        ),
        used_at=None,
        error_code=None,
        error_description=None,
    )
    memory_session = _MemorySession()
    service._session_factory = _MemorySessionFactory(memory_session)

    async def fake_get_transaction(session, transaction_id):
        assert transaction_id == row.transaction_id
        return row

    async def fake_sleep(delay):
        row.expires_at = datetime.now(UTC) - timedelta(seconds=1)

    monkeypatch.setattr("cognis.core.mcp_oauth.get_mcp_oauth_transaction", fake_get_transaction)
    monkeypatch.setattr(asyncio, "sleep", fake_sleep)

    await service._poll_device_authorization(row.transaction_id)

    assert row.status == "expired"
    assert row.error_code == "expired_token"
    notification_service.resolve_internal.assert_awaited_once_with(
        "notif-1",
        "failed",
        {"transaction_id": row.transaction_id, "provider": "mcp"},
    )


@pytest.mark.asyncio
async def test_recover_pending_device_authorizations_filters_device_transactions(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    key_path = tmp_path / "key"
    key_path.write_bytes(base64.urlsafe_b64encode(b"2" * 32))
    service = MCPOAuthService(
        session_factory=_MemorySessionFactory(_MemorySession()),
        key_path=str(key_path),
        public_base_url="https://cognis.example",
    )
    rows = [
        SimpleNamespace(
            transaction_id="device-tx",
            encrypted_payload=service._encrypt({"flow": "device_code"}),
        ),
        SimpleNamespace(
            transaction_id="code-tx",
            encrypted_payload=service._encrypt({"flow": "authorization_code"}),
        ),
    ]
    started: list[str] = []

    async def fake_list_pending(session, **kwargs):
        return rows

    monkeypatch.setattr(
        "cognis.core.mcp_oauth.list_pending_mcp_oauth_transactions",
        fake_list_pending,
    )
    monkeypatch.setattr(service, "_ensure_device_poll_task", started.append)

    await service.recover_pending_device_authorizations()

    assert started == ["device-tx"]


@pytest.mark.asyncio
async def test_device_poll_task_duplicate_prevention_and_shutdown(tmp_path) -> None:
    key_path = tmp_path / "key"
    key_path.write_bytes(base64.urlsafe_b64encode(b"2" * 32))
    service = MCPOAuthService(
        session_factory=AsyncMock(),
        key_path=str(key_path),
        public_base_url="https://cognis.example",
    )
    poll_started = asyncio.Event()

    async def wait_forever(transaction_id: str) -> None:
        poll_started.set()
        await asyncio.sleep(3600)

    service._poll_device_authorization = wait_forever

    service._ensure_device_poll_task("device-tx")
    first_task = service._device_poll_tasks["device-tx"]
    service._ensure_device_poll_task("device-tx")

    assert service._device_poll_tasks["device-tx"] is first_task
    await asyncio.wait_for(poll_started.wait(), timeout=1)
    await service.shutdown()
    assert first_task.cancelled()
    assert service._device_poll_tasks == {}


@pytest.mark.asyncio
async def test_invalid_grant_refresh_marks_token_invalid_and_starts_reauthorization(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    key_path = tmp_path / "key"
    key_path.write_bytes(base64.urlsafe_b64encode(b"1" * 32))
    server = SimpleNamespace(
        server_id="mcp-1",
        name="OAuth MCP",
        transport="sse",
        command=None,
        url="https://mcp.example/sse",
        args=[],
        env={},
        headers={},
        auth_config={
            "type": "oauth2",
            "issuer": "https://issuer.example",
            "client_id": "client",
        },
        timeout_seconds=30,
    )
    service = MCPOAuthService(
        session_factory=_MemorySessionFactory(_MemorySession(server=server)),
        key_path=str(key_path),
        public_base_url="https://cognis.example",
    )
    token = SimpleNamespace(
        token_id="tok-1",
        status="active",
        encrypted_payload=service._encrypt({"access_token": "old", "refresh_token": "revoked"}),
        expires_at=datetime.now(UTC) - timedelta(seconds=30),
        client_id="client",
        scopes=["tools.read"],
        token_type="Bearer",
    )
    memory_session = _MemorySession(token=token, server=server)
    service._session_factory = _MemorySessionFactory(memory_session)

    async def fake_get_token(*args, **kwargs):
        return memory_session.token

    async def fake_mark_status(session, *, token_id: str, status: str) -> None:
        assert token_id == "tok-1"
        session.token.status = status

    async def fake_start_authorization_for_server(**kwargs):
        assert kwargs["task_id"] == "task-1"
        assert kwargs["step_name"] == "build"
        assert kwargs["step_run_id"] == "sr-1"
        assert kwargs["session_id"] == "sess-1"
        return SimpleNamespace(
            authorization_url="https://issuer.example/authorize",
            transaction_id="mcpoauth_tx",
            expires_at=datetime.now(UTC) + timedelta(minutes=15),
        )

    monkeypatch.setattr("cognis.core.mcp_oauth.get_mcp_oauth_token", fake_get_token)
    monkeypatch.setattr("cognis.core.mcp_oauth.mark_mcp_oauth_token_status", fake_mark_status)
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *args, **kwargs: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 443))],
    )
    monkeypatch.setattr(
        service,
        "discover_metadata",
        AsyncMock(
            return_value={
                "issuer": "https://issuer.example",
                "token_endpoint": "https://issuer.example/token",
            }
        ),
    )
    monkeypatch.setattr(
        service,
        "_refresh_token",
        AsyncMock(
            side_effect=MCPOAuthError(
                "OAuth refresh token is invalid or expired",
                reason="invalid_grant",
                authorization_required=True,
            )
        ),
    )
    monkeypatch.setattr(
        service,
        "start_authorization_for_server",
        AsyncMock(side_effect=fake_start_authorization_for_server),
    )

    result = await service.inject_authorization_header(
        user_email="alice@example.com",
        server=server,
        headers={"X-Tenant": "demo"},
        conversation_id="conv-1",
        task_id="task-1",
        step_name="build",
        step_run_id="sr-1",
        session_id="sess-1",
        delivery_mode="default",
    )

    assert result.authorization_required is True
    assert result.reason == "invalid_grant"
    assert result.headers == {"X-Tenant": "demo"}
    assert token.status == "invalid"
    assert result.transaction_id == "mcpoauth_tx"
    assert result.authorization_url == "https://issuer.example/authorize"


@pytest.mark.asyncio
async def test_refresh_backend_failure_raises_setup_error_without_reauthorization(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    key_path = tmp_path / "key"
    key_path.write_bytes(base64.urlsafe_b64encode(b"1" * 32))
    server = SimpleNamespace(
        server_id="mcp-1",
        name="OAuth MCP",
        transport="sse",
        command=None,
        url="https://mcp.example/sse",
        args=[],
        env={},
        headers={},
        auth_config={
            "type": "oauth2",
            "issuer": "https://issuer.example",
            "client_id": "client",
        },
        timeout_seconds=30,
    )
    service = MCPOAuthService(
        session_factory=_MemorySessionFactory(_MemorySession(server=server)),
        key_path=str(key_path),
        public_base_url="https://cognis.example",
    )
    token = SimpleNamespace(
        token_id="tok-1",
        status="active",
        encrypted_payload=service._encrypt(
            {
                "access_token": "old",
                "refresh_token": "refresh",
                "token_endpoint": "https://issuer.example/token",
                "client_secret": "stored-client-secret",
            }
        ),
        expires_at=datetime.now(UTC) - timedelta(seconds=30),
        client_id="client",
        scopes=["tools.read"],
        token_type="Bearer",
    )
    memory_session = _MemorySession(token=token, server=server)
    service._session_factory = _MemorySessionFactory(memory_session)

    async def fake_get_token(*args, **kwargs):
        return memory_session.token

    start_authorization = AsyncMock()

    monkeypatch.setattr("cognis.core.mcp_oauth.get_mcp_oauth_token", fake_get_token)
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *args, **kwargs: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 443))],
    )
    monkeypatch.setattr(
        service,
        "_refresh_token",
        AsyncMock(
            side_effect=MCPOAuthError(
                "OAuth token refresh backend is unavailable",
                reason="refresh_backend_unavailable",
                retryable=True,
            )
        ),
    )
    monkeypatch.setattr(service, "start_authorization_for_server", start_authorization)

    with pytest.raises(MCPOAuthError) as exc_info:
        await service.inject_authorization_header(
            user_email="alice@example.com",
            server=server,
            headers={"X-Tenant": "demo"},
        )

    assert str(exc_info.value) == "OAuth token refresh backend is unavailable"
    assert exc_info.value.reason == "refresh_backend_unavailable"
    assert exc_info.value.authorization_required is False
    assert exc_info.value.retryable is True
    assert token.status == "active"
    assert token.refresh_failure_count == 1
    assert token.next_refresh_attempt_at > datetime.now(UTC)
    with pytest.raises(MCPOAuthError) as backoff_error:
        await service.inject_authorization_header(
            user_email="alice@example.com",
            server=server,
            headers={"X-Tenant": "demo"},
        )
    assert backoff_error.value.retryable is True
    with pytest.raises(MCPOAuthError) as forced_backoff:
        await service.refresh_token_for_server(
            user_email="alice@example.com",
            server=server,
            force=True,
            reason="mcp_tool_401",
        )
    assert forced_backoff.value.retryable is True
    assert service._refresh_token.await_count == 1
    start_authorization.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("response", "message"),
    [
        (httpx.Response(200, text="<html>not json</html>"), "not valid JSON"),
        (httpx.Response(200, json={"token_type": "Bearer"}), "missing access token"),
    ],
)
async def test_refresh_token_rejects_malformed_success_response(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
    response: httpx.Response,
    message: str,
) -> None:
    key_path = tmp_path / "key"
    key_path.write_bytes(base64.urlsafe_b64encode(b"1" * 32))
    service = MCPOAuthService(
        session_factory=AsyncMock(),
        key_path=str(key_path),
        public_base_url="https://cognis.example",
    )

    transport = httpx.MockTransport(lambda request: response)
    original_client = httpx.AsyncClient

    def client_factory(*args, **kwargs):
        kwargs["transport"] = transport
        return original_client(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", client_factory)

    with pytest.raises(MCPOAuthError, match=message) as exc_info:
        await service._refresh_token(
            token_endpoint="https://issuer.example/token",
            client_id="client",
            refresh_token="refresh",
            resource="https://mcp.example/sse",
        )

    assert exc_info.value.reason == "refresh_outcome_unknown"
    assert exc_info.value.outcome_unknown is True
    assert exc_info.value.authorization_required is True


@pytest.mark.asyncio
async def test_expired_token_without_refresh_token_starts_reauthorization(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    key_path = tmp_path / "key"
    key_path.write_bytes(base64.urlsafe_b64encode(b"1" * 32))
    server = SimpleNamespace(
        server_id="mcp-1",
        name="OAuth MCP",
        transport="sse",
        command=None,
        url="https://mcp.example/sse",
        args=[],
        env={},
        headers={},
        auth_config={
            "type": "oauth2",
            "issuer": "https://issuer.example",
            "client_id": "client",
        },
        timeout_seconds=30,
    )
    service = MCPOAuthService(
        session_factory=_MemorySessionFactory(_MemorySession(server=server)),
        key_path=str(key_path),
        public_base_url="https://cognis.example",
    )
    token = SimpleNamespace(
        token_id="tok-1",
        status="active",
        encrypted_payload=service._encrypt({"access_token": "old"}),
        expires_at=datetime.now(UTC) - timedelta(seconds=30),
        client_id="client",
        scopes=["tools.read"],
        token_type="Bearer",
    )
    memory_session = _MemorySession(token=token, server=server)
    service._session_factory = _MemorySessionFactory(memory_session)

    async def fake_get_token(*args, **kwargs):
        return memory_session.token

    async def fake_mark_status(session, *, token_id: str, status: str) -> None:
        assert token_id == "tok-1"
        session.token.status = status

    async def fake_start_authorization_for_server(**kwargs):
        assert kwargs["delivery_mode"] == "same_conversation"
        return SimpleNamespace(
            authorization_url="https://issuer.example/authorize",
            transaction_id="mcpoauth_tx",
            expires_at=datetime.now(UTC) + timedelta(minutes=15),
        )

    refresh_token = AsyncMock(side_effect=AssertionError("refresh should not be attempted"))

    monkeypatch.setattr("cognis.core.mcp_oauth.get_mcp_oauth_token", fake_get_token)
    monkeypatch.setattr("cognis.core.mcp_oauth.mark_mcp_oauth_token_status", fake_mark_status)
    monkeypatch.setattr(service, "_refresh_token", refresh_token)
    monkeypatch.setattr(
        service,
        "start_authorization_for_server",
        AsyncMock(side_effect=fake_start_authorization_for_server),
    )

    result = await service.inject_authorization_header(
        user_email="alice@example.com",
        server=server,
        headers={"X-Tenant": "demo"},
        delivery_mode="same_conversation",
    )

    assert result.authorization_required is True
    assert result.reason == "refresh_token_missing"
    assert result.headers == {"X-Tenant": "demo"}
    assert token.status == "invalid"
    assert result.authorization_url == "https://issuer.example/authorize"
    assert result.transaction_id == "mcpoauth_tx"
    refresh_token.assert_not_awaited()


@pytest.mark.asyncio
async def test_expired_token_refresh_uses_stored_token_endpoint(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    key_path = tmp_path / "key"
    key_path.write_bytes(base64.urlsafe_b64encode(b"3" * 32))
    server = SimpleNamespace(
        server_id="mcp-1",
        name="OAuth MCP",
        transport="sse",
        command=None,
        url="https://mcp.example/sse",
        args=[],
        env={},
        headers={},
        auth_config={
            "type": "oauth2",
            "issuer": "https://issuer.example",
            "client_id": "client",
        },
        timeout_seconds=30,
    )
    service = MCPOAuthService(
        session_factory=_MemorySessionFactory(_MemorySession(server=server)),
        key_path=str(key_path),
        public_base_url="https://cognis.example",
    )
    token = SimpleNamespace(
        token_id="tok-1",
        status="active",
        encrypted_payload=service._encrypt(
            {
                "access_token": "old",
                "refresh_token": "refresh",
                "token_endpoint": "https://issuer.example/token",
                "client_secret": "stored-client-secret",
            }
        ),
        expires_at=datetime.now(UTC) - timedelta(seconds=30),
        client_id="client",
        scopes=["tools.read"],
        token_type="Bearer",
    )
    memory_session = _MemorySession(token=token, server=server)
    service._session_factory = _MemorySessionFactory(memory_session)

    async def fake_get_token(*args, **kwargs):
        return memory_session.token

    async def fake_upsert_token(session, **kwargs):
        session.token.encrypted_payload = kwargs["encrypted_payload"]
        session.token.expires_at = kwargs["expires_at"]
        session.token.status = "active"
        return session.token

    refresh_token = AsyncMock(
        return_value={
            "access_token": "new",
            "refresh_token": "rotated-refresh",
            "expires_in": 3600,
            "token_type": "Bearer",
        }
    )

    monkeypatch.setattr("cognis.core.mcp_oauth.get_mcp_oauth_token", fake_get_token)
    monkeypatch.setattr("cognis.core.mcp_oauth.upsert_mcp_oauth_token", fake_upsert_token)
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *args, **kwargs: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 443))],
    )
    monkeypatch.setattr(
        service,
        "discover_metadata",
        AsyncMock(side_effect=AssertionError("metadata discovery should not be used")),
    )
    monkeypatch.setattr(service, "_refresh_token", refresh_token)

    result = await service.inject_authorization_header(
        user_email="alice@example.com",
        server=server,
        headers={},
    )

    assert result.authorization_required is False
    assert result.headers == {"Authorization": "Bearer new"}
    refresh_token.assert_awaited_once_with(
        token_endpoint="https://issuer.example/token",
        client_id="client",
        client_secret="stored-client-secret",
        refresh_token="refresh",
        resource="https://mcp.example/sse",
    )
    refreshed_payload = service._decrypt(memory_session.token.encrypted_payload)
    assert refreshed_payload["access_token"] == "new"
    assert refreshed_payload["refresh_token"] == "rotated-refresh"
    assert refreshed_payload["token_endpoint"] == "https://issuer.example/token"
    assert refreshed_payload["client_secret"] == "stored-client-secret"


@pytest.mark.asyncio
async def test_parallel_expired_token_injections_share_single_refresh(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    key_path = tmp_path / "key"
    key_path.write_bytes(base64.urlsafe_b64encode(b"3" * 32))
    server = SimpleNamespace(
        server_id="mcp-1",
        name="OAuth MCP",
        transport="sse",
        command=None,
        url="https://mcp.example/sse",
        args=[],
        env={},
        headers={},
        auth_config={
            "type": "oauth2",
            "issuer": "https://issuer.example",
            "client_id": "client",
        },
        timeout_seconds=30,
    )
    shared_token = SimpleNamespace(
        token_id="tok-1",
        status="active",
        encrypted_payload=b"",
        expires_at=datetime.now(UTC) - timedelta(seconds=30),
        client_id="client",
        scopes=["tools.read"],
        token_type="Bearer",
    )

    def copy_shared_token() -> SimpleNamespace:
        return SimpleNamespace(
            token_id=shared_token.token_id,
            status=shared_token.status,
            encrypted_payload=shared_token.encrypted_payload,
            expires_at=shared_token.expires_at,
            client_id=shared_token.client_id,
            scopes=list(shared_token.scopes),
            token_type=shared_token.token_type,
        )

    class _IsolatedSession(_MemorySession):
        def __init__(self) -> None:
            super().__init__(token=copy_shared_token(), server=server)

        async def refresh(self, row) -> None:
            row.status = shared_token.status
            row.encrypted_payload = shared_token.encrypted_payload
            row.expires_at = shared_token.expires_at
            row.client_id = shared_token.client_id
            row.scopes = list(shared_token.scopes)
            row.token_type = shared_token.token_type

    class _IsolatedSessionFactory:
        def __call__(self) -> _IsolatedSession:
            return _IsolatedSession()

    service = MCPOAuthService(
        session_factory=_IsolatedSessionFactory(),
        key_path=str(key_path),
        public_base_url="https://cognis.example",
    )
    shared_token.encrypted_payload = service._encrypt(
        {
            "access_token": "old",
            "refresh_token": "refresh",
            "token_endpoint": "https://issuer.example/token",
        }
    )

    async def fake_get_token(session, **kwargs):
        return session.token

    async def fake_upsert_token(session, **kwargs):
        shared_token.encrypted_payload = kwargs["encrypted_payload"]
        shared_token.expires_at = kwargs["expires_at"]
        shared_token.status = "active"
        session.token.encrypted_payload = kwargs["encrypted_payload"]
        session.token.expires_at = kwargs["expires_at"]
        session.token.status = "active"
        return session.token

    refresh_calls = 0

    async def fake_refresh_token(**kwargs):
        nonlocal refresh_calls
        refresh_calls += 1
        await asyncio.sleep(0.01)
        return {
            "access_token": "new",
            "refresh_token": "rotated",
            "expires_in": 3600,
            "token_type": "Bearer",
        }

    monkeypatch.setattr("cognis.core.mcp_oauth.get_mcp_oauth_token", fake_get_token)
    monkeypatch.setattr("cognis.core.mcp_oauth.upsert_mcp_oauth_token", fake_upsert_token)
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *args, **kwargs: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 443))],
    )
    monkeypatch.setattr(
        service,
        "discover_metadata",
        AsyncMock(side_effect=AssertionError("metadata discovery should not be used")),
    )
    monkeypatch.setattr(service, "_refresh_token", fake_refresh_token)

    results = await asyncio.gather(
        *(
            service.inject_authorization_header(
                user_email="alice@example.com",
                server=server,
                headers={},
            )
            for _ in range(4)
        )
    )

    assert refresh_calls == 1
    assert [result.headers for result in results] == [
        {"Authorization": "Bearer new"},
        {"Authorization": "Bearer new"},
        {"Authorization": "Bearer new"},
        {"Authorization": "Bearer new"},
    ]
    refreshed_payload = service._decrypt(shared_token.encrypted_payload)
    assert refreshed_payload["refresh_token"] == "rotated"


@pytest.mark.asyncio
async def test_existing_token_is_used_when_issuer_config_requires_discovery(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    key_path = tmp_path / "key"
    key_path.write_bytes(base64.urlsafe_b64encode(b"4" * 32))
    server = SimpleNamespace(
        server_id="mcp-1",
        name="OAuth MCP",
        transport="sse",
        command=None,
        url="https://mcp.example/sse",
        args=[],
        env={},
        headers={},
        auth_config={"type": "oauth2"},
        timeout_seconds=30,
    )
    service = MCPOAuthService(
        session_factory=_MemorySessionFactory(_MemorySession(server=server)),
        key_path=str(key_path),
        public_base_url="https://cognis.example",
    )
    token = SimpleNamespace(
        token_id="tok-1",
        status="active",
        issuer="https://issuer.example",
        resource="https://mcp.example/sse",
        encrypted_payload=service._encrypt({"access_token": "current"}),
        expires_at=datetime.now(UTC) + timedelta(hours=1),
        client_id="client",
        scopes=["tools.read"],
        token_type="Bearer",
    )
    memory_session = _MemorySession(token=token, server=server)
    service._session_factory = _MemorySessionFactory(memory_session)

    async def fake_get_token_for_server(*args, **kwargs):
        return memory_session.token

    monkeypatch.setattr(
        "cognis.core.mcp_oauth.get_mcp_oauth_token_for_server", fake_get_token_for_server
    )
    monkeypatch.setattr(
        service,
        "discover_metadata",
        AsyncMock(side_effect=AssertionError("metadata discovery should not be used")),
    )

    result = await service.inject_authorization_header(
        user_email="alice@example.com",
        server=server,
        headers={},
    )

    assert result.authorization_required is False
    assert result.headers == {"Authorization": "Bearer current"}


@pytest.mark.asyncio
async def test_runtime_mcp_resolution_passes_workflow_step_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    server = SimpleNamespace(
        server_id="mcp-1",
        name="OAuth MCP",
        transport="sse",
        command=None,
        url="https://mcp.example/sse",
        args=[],
        env={},
        headers={},
        auth_config={"type": "oauth2", "issuer": "https://issuer.example"},
        timeout_seconds=30,
        status="active",
    )
    session = _MemorySession(server=server)

    async def fake_get_mcp_server(*args, **kwargs):
        return server

    async def fake_get_setting_value(*args, **kwargs):
        return args[2] if len(args) > 2 else kwargs.get("default")

    oauth_service = SimpleNamespace(inject_authorization_header=AsyncMock())
    oauth_service.inject_authorization_header.return_value = SimpleNamespace(
        headers={"Authorization": "Bearer access"},
        authorization_required=False,
        reason=None,
        transaction_id=None,
    )

    monkeypatch.setattr("cognis.store.queries.get_mcp_server", fake_get_mcp_server)
    monkeypatch.setattr("cognis.store.queries.get_setting_value", fake_get_setting_value)

    servers = await _resolve_executor_mcp_servers(
        {"config": {"mcp_server_ids": ["mcp-1"]}, "executor_owner_email": "owner@example.com"},
        _MemorySessionFactory(session),
        providers=SimpleNamespace(mcp_oauth_service=oauth_service),
        user_email="alice@example.com",
        conversation_id="conv-1",
        task_id="task-1",
        step_name="build",
        step_run_id="sr-1",
        session_id="sess-1",
        delivery_mode="default",
    )

    assert len(servers) == 1
    oauth_service.inject_authorization_header.assert_awaited_once()
    kwargs = oauth_service.inject_authorization_header.await_args.kwargs
    assert kwargs["task_id"] == "task-1"
    assert kwargs["step_name"] == "build"
    assert kwargs["step_run_id"] == "sr-1"
    assert kwargs["session_id"] == "sess-1"


@pytest.mark.asyncio
async def test_runtime_mcp_resolution_skips_unresolved_oauth_server(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    oauth_server = SimpleNamespace(
        server_id="mcp-oauth",
        name="OAuth MCP",
        transport="streamable_http",
        command=None,
        url="https://mcp.example/mcp",
        args=[],
        env={},
        headers={},
        auth_config={"type": "oauth2"},
        timeout_seconds=30,
        status="active",
    )
    stdio_server = SimpleNamespace(
        server_id="mcp-stdio",
        name="Stdio MCP",
        transport="stdio",
        command="stdio-server",
        url=None,
        args=[],
        env={},
        headers={},
        auth_config=None,
        timeout_seconds=30,
        status="active",
    )
    session = _MemorySession()

    async def fake_get_mcp_server(*args, **kwargs):
        server_id = args[1]
        return {"mcp-oauth": oauth_server, "mcp-stdio": stdio_server}[server_id]

    async def fake_get_setting_value(*args, **kwargs):
        return args[2] if len(args) > 2 else kwargs.get("default")

    oauth_service = SimpleNamespace(inject_authorization_header=AsyncMock())
    oauth_service.inject_authorization_header.return_value = SimpleNamespace(
        headers={},
        authorization_required=True,
        reason="authorization_required",
        transaction_id="txn-1",
        authorization_url="https://issuer.example/authorize",
    )

    monkeypatch.setattr("cognis.store.queries.get_mcp_server", fake_get_mcp_server)
    monkeypatch.setattr("cognis.store.queries.get_setting_value", fake_get_setting_value)

    diagnostics: dict[str, object] = {}
    servers = await _resolve_executor_mcp_servers(
        {
            "config": {"mcp_server_ids": ["mcp-oauth", "mcp-stdio"]},
            "executor_owner_email": "owner@example.com",
        },
        _MemorySessionFactory(session),
        providers=SimpleNamespace(mcp_oauth_service=oauth_service),
        user_email="alice@example.com",
        diagnostics=diagnostics,
    )

    assert [server.server_id for server in servers] == ["mcp-stdio"]
    assert diagnostics["mcp_servers"][0]["server_id"] == "mcp-oauth"
    assert diagnostics["mcp_servers"][0]["status"] == "authorization_required"
    assert diagnostics["mcp_servers"][0]["transaction_id"] == "txn-1"
    assert diagnostics["warnings"] == [
        "MCP server OAuth MCP requires OAuth authorization before tools can be discovered."
    ]


def test_runtime_access_context_preserves_step_identity() -> None:
    ctx = RuntimeAccessContext(
        user_email="alice@example.com",
        conversation_id="conv-1",
        task_id="task-1",
        step_name="build",
        step_run_id="sr-1",
    )

    assert ctx.task_id == "task-1"
    assert ctx.step_name == "build"
    assert ctx.step_run_id == "sr-1"


@pytest.mark.asyncio
async def test_mcp_server_reconfigure_helper_schedules_assigned_executor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _Executor:
        executor_id = "olorin"
        desired_config_version = 4
        applied_config_version = 4

    class _Session:
        committed = False

        async def __aenter__(self) -> _Session:
            return self

        async def __aexit__(self, *_args: object) -> None:
            return None

        async def commit(self) -> None:
            self.committed = True

        async def rollback(self) -> None:
            return None

        async def scalar(self, _statement: object) -> object:
            return cleanup_transaction

    session = _Session()
    cleanup_transaction = SimpleNamespace(
        status="completed",
        terminal_cleanup_required=True,
        terminal_reconfigure_applied_at=None,
        terminal_reconfigure_completed_at=None,
    )
    executor = _Executor()
    runtime_updates = []
    scheduled = []
    inject_late_generation = False

    async def fake_list_executors(
        _session: object,
        server_id: str,
        *,
        for_update: bool = False,
    ) -> list[_Executor]:
        nonlocal inject_late_generation
        if for_update and inject_late_generation:
            inject_late_generation = False
            executor.desired_config_version += 1
        assert server_id == "mcp-1"
        return [executor]

    async def fake_bump_runtime(_session: object, executor_id: str, *, runtime_state: str) -> bool:
        runtime_updates.append((executor_id, runtime_state))
        executor.desired_config_version += 1
        return True

    monkeypatch.setattr(
        "cognis.api.mcp_reconfigure.list_websocket_executors_for_mcp_server", fake_list_executors
    )
    monkeypatch.setattr(
        "cognis.api.mcp_reconfigure.bump_executor_reconfigure_generation", fake_bump_runtime
    )
    monkeypatch.setattr(
        "cognis.api.mcp_reconfigure.schedule_executor_reconfigure",
        lambda _app, executor_id: scheduled.append(executor_id),
    )

    app = SimpleNamespace(
        state=SimpleNamespace(
            session_factory=lambda: session,
            providers=SimpleNamespace(
                executor=SimpleNamespace(
                    websocket=SimpleNamespace(get_connection=lambda executor_id: object())
                )
            ),
        )
    )
    result = await schedule_mcp_server_executor_reconfigure_for_app(
        app,
        server_id="mcp-1",
        reason="test",
    )

    assert runtime_updates == [("olorin", "reconfiguring")]
    assert session.committed is True
    assert scheduled == ["olorin"]
    assert result == ["olorin"]

    runtime_updates.clear()
    scheduled.clear()
    result = await schedule_mcp_server_executor_reconfigure_for_app(
        app,
        server_id="mcp-1",
        reason="stale-callback",
        admission_guard=AsyncMock(return_value=False),
    )

    assert result is None
    assert runtime_updates == []
    assert scheduled == []

    result = await schedule_mcp_server_executor_reconfigure_for_app(
        app,
        server_id="mcp-1",
        reason="terminal-cleanup",
        terminal_cleanup_transaction_id="txn-1",
    )
    assert cleanup_transaction.terminal_reconfigure_completed_at is None
    executor.applied_config_version = executor.desired_config_version
    repeated = await schedule_mcp_server_executor_reconfigure_for_app(
        app,
        server_id="mcp-1",
        reason="terminal-cleanup-retry",
        terminal_cleanup_transaction_id="txn-1",
    )

    assert result == ["olorin"]
    assert repeated == []
    assert runtime_updates == [("olorin", "reconfiguring")]
    assert scheduled == ["olorin"]
    assert cleanup_transaction.terminal_reconfigure_applied_at is not None
    assert cleanup_transaction.terminal_reconfigure_completed_at is not None

    cleanup_transaction.terminal_reconfigure_applied_at = None
    cleanup_transaction.terminal_reconfigure_completed_at = None
    executor.desired_config_version = 4
    executor.applied_config_version = 4
    runtime_updates.clear()
    scheduled.clear()

    def crash_before_schedule(_app: object, _executor_id: str) -> None:
        raise RuntimeError("controller crashed before dispatch")

    monkeypatch.setattr(
        "cognis.api.mcp_reconfigure.schedule_executor_reconfigure",
        crash_before_schedule,
    )
    with pytest.raises(RuntimeError, match="crashed before dispatch"):
        await schedule_mcp_server_executor_reconfigure_for_app(
            app,
            server_id="mcp-1",
            reason="terminal-cleanup-crash",
            terminal_cleanup_transaction_id="txn-1",
        )

    assert cleanup_transaction.terminal_reconfigure_applied_at is not None
    assert cleanup_transaction.terminal_reconfigure_completed_at is None
    assert executor.desired_config_version == 5
    assert runtime_updates == [("olorin", "reconfiguring")]

    monkeypatch.setattr(
        "cognis.api.mcp_reconfigure.schedule_executor_reconfigure",
        lambda _app, executor_id: scheduled.append(executor_id),
    )
    recovered = await schedule_mcp_server_executor_reconfigure_for_app(
        app,
        server_id="mcp-1",
        reason="terminal-cleanup-recovery",
        terminal_cleanup_transaction_id="txn-1",
    )
    assert cleanup_transaction.terminal_reconfigure_completed_at is None
    executor.applied_config_version = executor.desired_config_version
    inject_late_generation = True
    late_recovery = await schedule_mcp_server_executor_reconfigure_for_app(
        app,
        server_id="mcp-1",
        reason="terminal-cleanup-late-generation",
        terminal_cleanup_transaction_id="txn-1",
    )
    assert cleanup_transaction.terminal_reconfigure_completed_at is None
    executor.applied_config_version = executor.desired_config_version
    completed = await schedule_mcp_server_executor_reconfigure_for_app(
        app,
        server_id="mcp-1",
        reason="terminal-cleanup-reconcile-evidence",
        terminal_cleanup_transaction_id="txn-1",
    )
    repeated = await schedule_mcp_server_executor_reconfigure_for_app(
        app,
        server_id="mcp-1",
        reason="terminal-cleanup-recovery-retry",
        terminal_cleanup_transaction_id="txn-1",
    )

    assert recovered == ["olorin"]
    assert late_recovery == ["olorin"]
    assert completed == []
    assert repeated == []
    assert runtime_updates == [("olorin", "reconfiguring")]
    assert scheduled == ["olorin", "olorin"]
    assert cleanup_transaction.terminal_reconfigure_completed_at is not None


@pytest.mark.asyncio
async def test_oauth_completion_schedules_reconfigure_and_emits_status(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _Transaction:
        mcp_server_id = "mcp-1"
        user_email = "alice@example.com"

    class _Session:
        async def __aenter__(self) -> _Session:
            return self

        async def __aexit__(self, *_args: object) -> None:
            return None

    scheduled = []
    emitted_status = []

    async def fake_get_transaction(_session: object, transaction_id: str) -> _Transaction:
        assert transaction_id == "txn-1"
        return _Transaction()

    async def fake_schedule(_app: object, **kwargs: object) -> list[str]:
        scheduled.append(kwargs)
        return ["olorin"]

    monkeypatch.setattr(
        "cognis.api.routes.mcp_oauth.get_mcp_oauth_transaction", fake_get_transaction
    )
    monkeypatch.setattr(
        "cognis.api.routes.mcp_oauth.schedule_mcp_server_executor_reconfigure_for_app",
        fake_schedule,
    )
    monkeypatch.setattr(
        "cognis.api.routes.mcp_oauth._emit_mcp_oauth_status_changed",
        AsyncMock(side_effect=lambda _app, **kwargs: emitted_status.append(kwargs)),
    )

    app = SimpleNamespace(state=SimpleNamespace(session_factory=lambda: _Session()))

    await schedule_mcp_executor_reconfigure_for_app(app, transaction_id="txn-1")

    assert scheduled == [
        {
            "server_id": "mcp-1",
            "reason": "mcp_oauth_authorization",
            "log_context": {"transaction_id": "txn-1"},
        }
    ]
    assert emitted_status == [{"user_email": "alice@example.com", "server_id": "mcp-1"}]

    scheduled.clear()
    emitted_status.clear()

    async def reject_stale_owner(_app: object, **kwargs: object) -> None:
        scheduled.append(kwargs)
        return None

    monkeypatch.setattr(
        "cognis.api.routes.mcp_oauth.schedule_mcp_server_executor_reconfigure_for_app",
        reject_stale_owner,
    )
    await schedule_mcp_executor_reconfigure_for_app(
        app,
        transaction_id="txn-1",
        admission_guard=AsyncMock(return_value=False),
        terminal_cleanup=True,
    )

    assert len(scheduled) == 1
    assert emitted_status == []


@pytest.mark.asyncio
async def test_callback_route_does_not_schedule_duplicate_reconfigure_when_service_callback_exists(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = SimpleNamespace(
        _on_authorization_completed=object(),
        complete_callback=AsyncMock(return_value="txn-1"),
    )
    app = SimpleNamespace(state=SimpleNamespace(mcp_oauth_service=service))
    request = SimpleNamespace(app=app)

    schedule = AsyncMock(side_effect=AssertionError("duplicate schedule"))
    monkeypatch.setattr("cognis.api.routes.mcp_oauth._schedule_mcp_executor_reconfigure", schedule)

    response = await mcp_oauth_callback(request, state="state", code="code")

    service.complete_callback.assert_awaited_once_with(state="state", code="code")
    schedule.assert_not_awaited()
    assert response.status_code == 200


@pytest.mark.asyncio
async def test_refresh_succeeds_beyond_legacy_ten_second_timeout(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    key_path = tmp_path / "key"
    key_path.write_bytes(base64.urlsafe_b64encode(b"7" * 32))
    service = MCPOAuthService(
        session_factory=AsyncMock(),
        key_path=str(key_path),
        public_base_url="https://cognis.example",
        refresh_timeout_seconds=11,
    )

    class _SlowClient:
        def __init__(self, **_kwargs: object) -> None:
            pass

        async def __aenter__(self) -> _SlowClient:
            return self

        async def __aexit__(self, *_args: object) -> None:
            return None

        async def post(self, url: str, *, data: dict[str, str]) -> httpx.Response:
            assert data["refresh_token"] == "old-refresh"
            await asyncio.sleep(10.05)
            return httpx.Response(
                200,
                json={
                    "access_token": "new-access",
                    "refresh_token": "rotated-refresh",
                    "expires_in": 900,
                },
                request=httpx.Request("POST", url),
            )

    monkeypatch.setattr("cognis.core.mcp_oauth.httpx.AsyncClient", _SlowClient)

    payload = await service._refresh_token(
        token_endpoint="https://issuer.example/token",
        client_id="client",
        refresh_token="old-refresh",
        resource="https://mcp.example/mcp",
    )

    assert payload["access_token"] == "new-access"
    assert payload["refresh_token"] == "rotated-refresh"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("payload", "description"),
    [
        ({"error": "invalid_grant", "error_description": "expired"}, "expired"),
        (
            {"detail": {"error": "invalid_grant", "error_description": "rotated token reused"}},
            "rotated token reused",
        ),
    ],
)
async def test_refresh_classifies_top_level_and_nested_invalid_grant(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
    payload: dict[str, object],
    description: str,
) -> None:
    key_path = tmp_path / "key"
    key_path.write_bytes(base64.urlsafe_b64encode(b"6" * 32))
    service = MCPOAuthService(
        session_factory=AsyncMock(),
        key_path=str(key_path),
        public_base_url="https://cognis.example",
    )

    class _Client:
        def __init__(self, **_kwargs: object) -> None:
            pass

        async def __aenter__(self) -> _Client:
            return self

        async def __aexit__(self, *_args: object) -> None:
            return None

        async def post(self, url: str, *, data: dict[str, str]) -> httpx.Response:
            return httpx.Response(
                400,
                json=payload,
                request=httpx.Request("POST", url),
            )

    monkeypatch.setattr("cognis.core.mcp_oauth.httpx.AsyncClient", _Client)

    with pytest.raises(MCPOAuthError) as exc_info:
        await service._refresh_token(
            token_endpoint="https://issuer.example/token",
            client_id="client",
            refresh_token="old-refresh",
            resource=None,
        )

    assert exc_info.value.reason == "invalid_grant"
    assert exc_info.value.authorization_required is True
    assert exc_info.value.retryable is False
    assert description in str(exc_info.value)


@pytest.mark.asyncio
async def test_refresh_normalizes_unknown_provider_error_code(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    key_path = tmp_path / "key"
    key_path.write_bytes(base64.urlsafe_b64encode(b"5" * 32))
    service = MCPOAuthService(
        session_factory=AsyncMock(),
        key_path=str(key_path),
        public_base_url="https://cognis.example",
    )

    class _Client:
        def __init__(self, **_kwargs: object) -> None:
            pass

        async def __aenter__(self) -> _Client:
            return self

        async def __aexit__(self, *_args: object) -> None:
            return None

        async def post(self, url: str, *, data: dict[str, str]) -> httpx.Response:
            return httpx.Response(
                400,
                json={"error": "refresh_token=provider-secret"},
                request=httpx.Request("POST", url),
            )

    monkeypatch.setattr("cognis.core.mcp_oauth.httpx.AsyncClient", _Client)

    with pytest.raises(MCPOAuthError) as exc_info:
        await service._refresh_token(
            token_endpoint="https://issuer.example/token",
            client_id="client",
            refresh_token="old-refresh",
            resource=None,
        )

    assert exc_info.value.reason == "refresh_rejected"
    assert "provider-secret" not in str(exc_info.value)


@pytest.mark.asyncio
async def test_indeterminate_refresh_is_terminal_and_never_reuses_old_refresh_token(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    server = SimpleNamespace(
        server_id="mcp-1",
        name="OAuth MCP",
        url="https://mcp.example/mcp",
        headers={},
        auth_config={
            "type": "oauth2",
            "issuer": "https://issuer.example",
            "resource": "https://mcp.example/mcp",
        },
    )
    session = _MemorySession(server=server)
    service = _oauth_service_for_tests(tmp_path, session)
    token = SimpleNamespace(
        token_id="tok-1",
        user_email="alice@example.com",
        mcp_server_id="mcp-1",
        issuer="https://issuer.example",
        resource="https://mcp.example/mcp",
        client_id="client",
        scopes=[],
        token_type="Bearer",
        expires_at=datetime.now(UTC) - timedelta(seconds=1),
        status="active",
        version=1,
        refresh_failure_count=0,
        encrypted_payload=service._encrypt(
            {
                "access_token": "old-access",
                "refresh_token": "old-refresh",
                "token_endpoint": "https://issuer.example/token",
            }
        ),
    )
    session.token = token

    async def fake_get_token(*_args: object, **_kwargs: object) -> object:
        return session.token

    refresh = AsyncMock(
        side_effect=MCPOAuthError(
            "refresh outcome unknown",
            reason="refresh_outcome_unknown",
            authorization_required=True,
            outcome_unknown=True,
        )
    )
    monkeypatch.setattr("cognis.core.mcp_oauth.get_mcp_oauth_token", fake_get_token)
    monkeypatch.setattr(
        "cognis.core.mcp_oauth.get_mcp_oauth_token_for_server",
        fake_get_token,
    )
    monkeypatch.setattr(service, "_refresh_token", refresh)
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *_args, **_kwargs: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 443))],
    )

    with pytest.raises(MCPOAuthError, match="outcome unknown"):
        await service.refresh_token_for_server(
            user_email="alice@example.com",
            server=server,
        )
    with pytest.raises(MCPOAuthError) as second:
        await service.refresh_token_for_server(
            user_email="alice@example.com",
            server=server,
        )

    assert token.status == "refresh_outcome_unknown"
    assert token.last_refresh_error_code == "refresh_outcome_unknown"
    assert token.next_refresh_attempt_at is None
    assert second.value.authorization_required is True
    refresh.assert_awaited_once()


def test_oauth_status_never_reports_connected_for_expired_failed_refresh() -> None:
    now = datetime.now(UTC)
    row = SimpleNamespace(
        status="active",
        issuer="https://issuer.example",
        resource="https://mcp.example/mcp",
        resource_key="https://mcp.example/mcp",
        scopes=[],
        token_type="Bearer",
        expires_at=now - timedelta(seconds=1),
        last_refresh_at=now - timedelta(minutes=10),
        last_verified_at=now - timedelta(minutes=10),
        refresh_failure_count=3,
        next_refresh_attempt_at=now + timedelta(seconds=20),
        last_refresh_error_code="refresh_backend_unavailable",
        last_refresh_error_description="temporarily unavailable",
        last_refresh_error_at=now,
    )

    payload = oauth_status_payload(
        row,
        {"access_token": "expired", "refresh_token": "refresh"},
    )

    assert payload["authorized"] is True
    assert payload["connected"] is False
    assert payload["refresh_state"] == "retry_backoff"
    assert payload["authorization_required"] is False


def test_oauth_status_never_reports_connected_when_token_cannot_be_decrypted() -> None:
    now = datetime.now(UTC)
    row = SimpleNamespace(
        status="active",
        issuer="https://issuer.example",
        resource="https://mcp.example/mcp",
        resource_key="https://mcp.example/mcp",
        scopes=[],
        expires_at=now + timedelta(minutes=10),
        refresh_failure_count=0,
        next_refresh_attempt_at=None,
        last_refresh_error_code=None,
        last_refresh_error_description=None,
        last_refresh_error_at=None,
    )

    payload = oauth_status_payload(row, None)

    assert payload["connected"] is False
    assert payload["authorized"] is False
    assert payload["authorization_required"] is True
    assert payload["refreshable"] is False


@pytest.mark.asyncio
async def test_proactive_maintenance_refreshes_due_long_lived_connection(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = _oauth_service_for_tests(tmp_path, _MemorySession())
    due = SimpleNamespace(
        user_email="alice@example.com",
        mcp_server_id="mcp-1",
        token_id="tok-1",
    )
    monkeypatch.setattr(
        "cognis.core.mcp_oauth.list_due_mcp_oauth_tokens",
        AsyncMock(return_value=[due]),
    )
    refresh = AsyncMock(return_value=True)
    monkeypatch.setattr(service, "refresh_token_for_server_id", refresh)

    assert await service.run_refresh_maintenance_once() == 1
    refresh.assert_awaited_once_with(
        user_email="alice@example.com",
        server_id="mcp-1",
        token_id="tok-1",
        reason="proactive_refresh",
    )


@pytest.mark.asyncio
async def test_token_specific_maintenance_uses_selected_rows_issuer_and_resource(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    server = SimpleNamespace(
        server_id="mcp-1",
        name="OAuth MCP",
        url="https://new-resource.example/mcp",
        headers={},
        auth_config={
            "type": "oauth2",
            "issuer": "https://new-issuer.example",
            "resource": "https://new-resource.example/mcp",
        },
    )
    session = _MemorySession(server=server)
    service = _oauth_service_for_tests(tmp_path, session)
    token = SimpleNamespace(
        token_id="tok-old",
        user_email="alice@example.com",
        mcp_server_id="mcp-1",
        issuer="https://old-issuer.example",
        resource="https://old-resource.example/mcp",
        client_id="client",
        scopes=["tools.read"],
        token_type="Bearer",
        expires_at=datetime.now(UTC) - timedelta(seconds=1),
        status="active",
        version=4,
        refresh_failure_count=0,
        next_refresh_attempt_at=None,
        encrypted_payload=service._encrypt(
            {
                "access_token": "old-access",
                "refresh_token": "old-refresh",
                "token_endpoint": "https://old-issuer.example/token",
            }
        ),
    )
    session.token = token
    refresh = AsyncMock(
        return_value={
            "access_token": "new-access",
            "refresh_token": "rotated-refresh",
            "expires_in": 900,
        }
    )
    upsert = AsyncMock(return_value=token)
    monkeypatch.setattr(service, "_refresh_token", refresh)
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *_args, **_kwargs: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 443))],
    )
    monkeypatch.setattr(
        "cognis.core.mcp_oauth.upsert_mcp_oauth_token",
        upsert,
    )

    changed = await service.refresh_token_for_server(
        user_email="alice@example.com",
        server=server,
        token_id="tok-old",
        reason="proactive_refresh",
    )

    assert changed is True
    refresh.assert_awaited_once_with(
        token_endpoint="https://old-issuer.example/token",
        client_id="client",
        refresh_token="old-refresh",
        resource="https://old-resource.example/mcp",
    )
    assert upsert.await_args.kwargs["issuer"] == "https://old-issuer.example"
    assert upsert.await_args.kwargs["resource"] == "https://old-resource.example/mcp"


@pytest.mark.asyncio
async def test_reauthorization_unions_existing_and_challenge_scopes(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    token = SimpleNamespace(scopes=["configured.read", "existing.inspect"])
    session = _MemorySession()
    service = _oauth_service_for_tests(tmp_path, session)
    monkeypatch.setattr(
        "cognis.core.mcp_oauth.get_mcp_oauth_token_for_server",
        AsyncMock(return_value=token),
    )
    monkeypatch.setattr(
        service,
        "mark_token_invalid_for_server",
        AsyncMock(return_value=True),
    )
    start = AsyncMock(return_value=None)
    monkeypatch.setattr(service, "start_authorization", start)

    await service.require_reauthorization_for_server(
        user_email="alice@example.com",
        server_id="mcp-1",
        reason="insufficient_scope",
        authorization_challenge={"scope": "existing.inspect challenge.write"},
    )

    assert start.await_args.kwargs["authorization_challenge"]["scope"] == (
        "configured.read existing.inspect challenge.write"
    )
