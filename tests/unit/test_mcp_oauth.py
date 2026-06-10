from __future__ import annotations

import base64
import json
import socket
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from cognis.api.models import MCPServerCreateRequest
from cognis.api.routes.mcp_oauth import _schedule_mcp_executor_reconfigure
from cognis.api.runtime_support import _resolve_executor_mcp_servers
from cognis.core.mcp_oauth import (
    MCPOAuthError,
    MCPOAuthService,
    _safe_url,
    oauth_status_payload,
    parse_www_authenticate,
)
from cognis.models.tool import MCPServerConfig
from cognis.runtime_context import RuntimeAccessContext


def test_parse_www_authenticate_bearer_challenge() -> None:
    parsed = parse_www_authenticate(
        'Bearer resource_metadata="https://mcp.example/.well-known/oauth-protected-resource", '
        'error="insufficient_scope", scope="tools.read tools.write"'
    )

    assert parsed["resource_metadata"] == "https://mcp.example/.well-known/oauth-protected-resource"
    assert parsed["error"] == "insufficient_scope"
    assert parsed["scope"] == "tools.read tools.write"


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


def test_oauth_status_remains_connected_when_refresh_is_possible_after_access_expiry() -> None:
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

    assert payload["connected"] is True
    assert payload["access_token_expires_at"] == access_expires_at.isoformat()
    assert payload["authorization_expires_at"] == refresh_expires_at.isoformat()
    assert payload["refreshable"] is True


def test_oauth_status_omits_unknown_authorization_expiry_for_refreshable_token() -> None:
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

    assert payload["connected"] is True
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
                json={"authorization_servers": ["https://issuer.example"]},
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


class _MemorySession:
    def __init__(self, token=None, server=None) -> None:
        self.token = token
        self.server = server
        self.commits = 0

    async def commit(self) -> None:
        self.commits += 1

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

    assert exc_info.value.reason == "refresh_backend_failed"
    assert exc_info.value.authorization_required is False


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
        refresh_token="refresh",
        resource="https://mcp.example/sse",
    )
    refreshed_payload = service._decrypt(memory_session.token.encrypted_payload)
    assert refreshed_payload["refresh_token"] == "refresh"
    assert refreshed_payload["token_endpoint"] == "https://issuer.example/token"


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
async def test_oauth_completion_schedules_assigned_executor_reconfigure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _Transaction:
        mcp_server_id = "mcp-1"

    class _Executor:
        executor_id = "olorin"
        desired_config_version = 4

    class _Session:
        committed = False

        async def __aenter__(self) -> _Session:
            return self

        async def __aexit__(self, *_args: object) -> None:
            return None

        async def commit(self) -> None:
            self.committed = True

    session = _Session()
    runtime_updates = []
    scheduled = []

    async def fake_get_transaction(_session: object, transaction_id: str) -> _Transaction:
        assert transaction_id == "txn-1"
        return _Transaction()

    async def fake_list_executors(_session: object, server_id: str) -> list[_Executor]:
        assert server_id == "mcp-1"
        return [_Executor()]

    async def fake_update_runtime(_session: object, executor_id: str, **kwargs: object) -> None:
        runtime_updates.append((executor_id, kwargs))

    monkeypatch.setattr(
        "cognis.api.routes.mcp_oauth.get_mcp_oauth_transaction", fake_get_transaction
    )
    monkeypatch.setattr(
        "cognis.api.routes.mcp_oauth.list_websocket_executors_for_mcp_server",
        fake_list_executors,
    )
    monkeypatch.setattr(
        "cognis.api.routes.mcp_oauth.update_executor_runtime_state", fake_update_runtime
    )
    monkeypatch.setattr(
        "cognis.api.routes.mcp_oauth.schedule_executor_reconfigure",
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
    request = SimpleNamespace(app=app)

    await _schedule_mcp_executor_reconfigure(request, transaction_id="txn-1")

    assert runtime_updates == [
        ("olorin", {"desired_config_version": 5, "runtime_state": "reconfiguring"})
    ]
    assert session.committed is True
    assert scheduled == ["olorin"]
