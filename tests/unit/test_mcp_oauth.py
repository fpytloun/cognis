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
from cognis.api.runtime_support import _resolve_executor_mcp_servers
from cognis.core.mcp_oauth import MCPOAuthError, MCPOAuthService, _safe_url, parse_www_authenticate
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
            lambda *args, **kwargs: [
                (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("203.0.113.10", 443))
            ],
        )
        with pytest.raises(MCPOAuthError, match="private|https"):
            await service._fetch_json(
                "https://issuer.example/.well-known/oauth-authorization-server"
            )


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
async def test_refresh_failure_is_isolated_and_starts_reauthorization(
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
        )

    monkeypatch.setattr("cognis.core.mcp_oauth.get_mcp_oauth_token", fake_get_token)
    monkeypatch.setattr("cognis.core.mcp_oauth.mark_mcp_oauth_token_status", fake_mark_status)
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
        AsyncMock(side_effect=MCPOAuthError("OAuth token refresh failed")),
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
    assert result.reason == "refresh_failed"
    assert result.headers == {"X-Tenant": "demo"}
    assert token.status == "invalid"
    assert result.transaction_id == "mcpoauth_tx"


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

    oauth_service = SimpleNamespace(inject_authorization_header=AsyncMock())
    oauth_service.inject_authorization_header.return_value = SimpleNamespace(
        headers={"Authorization": "Bearer access"},
        authorization_required=False,
        reason=None,
        transaction_id=None,
    )

    monkeypatch.setattr("cognis.store.queries.get_mcp_server", fake_get_mcp_server)

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
