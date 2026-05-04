from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

from fastapi.testclient import TestClient

from cognis.api.app import create_app
from cognis.models.tool import ToolDefinition, ToolSource
from cognis.security import generate_api_key_material
from cognis.store.queries import create_agent, create_api_key, create_llm_provider, create_user


def _create_test_client(monkeypatch: object, tmp_path: Path) -> TestClient:
    monkeypatch.setenv("COGNIS_DATA_DIR", str(tmp_path))  # type: ignore[attr-defined]
    monkeypatch.setenv("COGNIS_HOST", "127.0.0.1")  # type: ignore[attr-defined]
    app = create_app()
    return TestClient(app)


def _auth_headers(app: object, *, email: str, role: str = "user") -> dict[str, str]:
    token = app.state.auth_provider.sign_access_token(email, email.split("@")[0].title(), role)  # type: ignore[attr-defined]
    return {"Authorization": f"Bearer {token}"}


def test_bootstrap_status_public_endpoint(monkeypatch: object, tmp_path: Path) -> None:
    with _create_test_client(monkeypatch, tmp_path) as client:
        response = client.get("/api/bootstrap-status")
        assert response.status_code == 200
        assert response.json() == {"setup_available": True, "setup_complete": False}


def test_change_password_and_api_key_last_used(monkeypatch: object, tmp_path: Path) -> None:
    with _create_test_client(monkeypatch, tmp_path) as client:
        app = client.app

        async def _seed() -> None:
            async with app.state.session_factory() as session:
                await create_user(
                    session,
                    email="admin@example.com",
                    name="Admin",
                    password_hash=app.state.password_hasher.hash("password123"),
                    role="admin",
                )
                await session.commit()

        asyncio.run(_seed())

        headers = _auth_headers(app, email="admin@example.com", role="admin")
        change = client.post(
            "/api/auth/change-password",
            headers=headers,
            json={"current_password": "password123", "new_password": "new-password"},
        )
        assert change.status_code == 200
        login = client.post(
            "/api/auth/login",
            json={"email": "admin@example.com", "password": "new-password"},
        )
        assert login.status_code == 200

        created = client.post(
            "/api/v1/auth/api-keys",
            headers=headers,
            json={"name": "CLI key", "expires_in_days": 7},
        )
        assert created.status_code == 200
        api_key = created.json()["api_key"]

        me = client.get("/api/auth/me", headers={"X-API-Key": api_key})
        assert me.status_code == 200

        listed = client.get("/api/v1/auth/api-keys", headers=headers)
        assert listed.status_code == 200
        assert listed.json()[0]["last_used_at"] is not None


def test_expired_api_key_is_rejected(monkeypatch: object, tmp_path: Path) -> None:
    with _create_test_client(monkeypatch, tmp_path) as client:
        app = client.app

        async def _seed() -> str:
            key_id, api_key = generate_api_key_material()
            async with app.state.session_factory() as session:
                await create_user(
                    session,
                    email="user@example.com",
                    name="User",
                    password_hash=app.state.password_hasher.hash("password123"),
                    role="user",
                )
                record = await create_api_key(
                    session,
                    user_email="user@example.com",
                    key_hash=app.state.password_hasher.hash(api_key),
                    name="expired",
                    key_id=key_id,
                )
                record.expires_at = datetime.now(UTC) - timedelta(days=1)
                await session.commit()
            return api_key

        api_key = asyncio.run(_seed())
    response = client.get("/api/auth/me", headers={"X-API-Key": api_key})
    assert response.status_code == 401


def test_api_key_cannot_manage_api_keys(monkeypatch: object, tmp_path: Path) -> None:
    with _create_test_client(monkeypatch, tmp_path) as client:
        app = client.app

        async def _seed() -> str:
            async with app.state.session_factory() as session:
                await create_user(
                    session,
                    email="user@example.com",
                    name="User",
                    password_hash=app.state.password_hasher.hash("password123"),
                    role="user",
                )
                await session.commit()
            login = client.post(
                "/api/auth/login",
                json={"email": "user@example.com", "password": "password123"},
            )
            assert login.status_code == 200
            created = client.post(
                "/api/v1/auth/api-keys",
                json={"name": "primary-key"},
            )
            return created.json()["api_key"]

        api_key = asyncio.run(_seed())
        response = client.post(
            "/api/v1/auth/api-keys",
            headers={"X-API-Key": api_key},
            json={"name": "nested-key"},
        )
        assert response.status_code == 403


def test_diagnostics_requires_admin(monkeypatch: object, tmp_path: Path) -> None:
    with _create_test_client(monkeypatch, tmp_path) as client:
        app = client.app

        async def _seed() -> None:
            async with app.state.session_factory() as session:
                await create_user(
                    session,
                    email="user@example.com",
                    name="User",
                    password_hash=app.state.password_hasher.hash("password123"),
                    role="user",
                )
                await session.commit()

        asyncio.run(_seed())
        response = client.get(
            "/api/v1/system/diagnostics",
            headers=_auth_headers(app, email="user@example.com"),
        )
        assert response.status_code == 403


def test_admin_diagnostics_returns_readiness_summary(monkeypatch: object, tmp_path: Path) -> None:
    with _create_test_client(monkeypatch, tmp_path) as client:
        app = client.app

        async def _seed() -> None:
            async with app.state.session_factory() as session:
                await create_user(
                    session,
                    email="admin@example.com",
                    name="Admin",
                    password_hash=app.state.password_hasher.hash("password123"),
                    role="admin",
                )
                await create_agent(
                    session,
                    agent_id="agent-1",
                    owner_email="admin@example.com",
                    name="Agent",
                    status="active",
                )
                await create_llm_provider(
                    session,
                    provider_id="default",
                    display_name="OpenAI",
                    location="controller",
                    backend="litellm",
                    config={
                        "default_model": "gpt-4o-mini",
                        "models": [{"model_id": "gpt-4o-mini"}],
                    },
                    status="active",
                )
                await session.commit()

        asyncio.run(_seed())
        response = client.get(
            "/api/v1/system/diagnostics",
            headers=_auth_headers(app, email="admin@example.com", role="admin"),
        )
        assert response.status_code == 200
        payload = response.json()
        assert payload["readiness"]["llm_provider_configured"] is True
        assert payload["readiness"]["agent_created"] is True
        assert payload["key_fingerprint"] is not None


def test_provider_test_endpoint_enforces_cooldown(monkeypatch: object, tmp_path: Path) -> None:
    with _create_test_client(monkeypatch, tmp_path) as client:
        app = client.app

        async def _seed() -> None:
            async with app.state.session_factory() as session:
                await create_user(
                    session,
                    email="admin@example.com",
                    name="Admin",
                    password_hash=app.state.password_hasher.hash("password123"),
                    role="admin",
                )
                await create_llm_provider(
                    session,
                    provider_id="default",
                    display_name="OpenAI",
                    location="controller",
                    backend="litellm",
                    config={"default_model": "gpt-4o-mini"},
                    status="active",
                )
                await session.commit()

        asyncio.run(_seed())

        async def _fake_test_provider(
            provider_id: str, timeout_seconds: int = 15
        ) -> dict[str, object]:
            return {
                "ok": True,
                "model_resolved": "gpt-4o-mini",
                "latency_ms": 12,
                "error_type": None,
                "error_detail": None,
                "tested_at": datetime.now(UTC),
            }

        app.state.providers.llm.test_provider = _fake_test_provider
        headers = _auth_headers(app, email="admin@example.com", role="admin")
        first = client.post("/api/v1/llm-providers/default/test", headers=headers)
        second = client.post("/api/v1/llm-providers/default/test", headers=headers)
        assert first.status_code == 200
        assert second.status_code == 429


def test_mcp_routes_use_executor_discovery(monkeypatch: object, tmp_path: Path) -> None:
    with _create_test_client(monkeypatch, tmp_path) as client:
        app = client.app

        async def _seed() -> None:
            async with app.state.session_factory() as session:
                await create_user(
                    session,
                    email="admin@example.com",
                    name="Admin",
                    password_hash=app.state.password_hasher.hash("password123"),
                    role="admin",
                )
                await create_user(
                    session,
                    email="owner@example.com",
                    name="Owner",
                    password_hash=app.state.password_hasher.hash("password123"),
                    role="user",
                )
                await create_agent(
                    session,
                    agent_id="agent-mcp",
                    owner_email="owner@example.com",
                    name="MCP Agent",
                    status="active",
                    tools={
                        "mcp_servers": [
                            {
                                "name": "filesystem",
                                "command": "npx",
                                "args": ["@modelcontextprotocol/server-filesystem"],
                            }
                        ]
                    },
                )
                await session.commit()

        asyncio.run(_seed())

        class _FakeConnection:
            async def list_tools(self) -> list[ToolDefinition]:
                return [
                    ToolDefinition(
                        name="filesystem/read_file",
                        description="Read file",
                        source=ToolSource(type="local_mcp", server_name="filesystem"),
                        category="mcp",
                        read_only=True,
                        timeout_seconds=30,
                        non_bypassable=False,
                    )
                ]

        called = {"spawn": 0, "cancel": 0}

        async def _fake_spawn(config: object) -> object:
            called["spawn"] += 1
            return SimpleNamespace(executor_id="executor-1")

        async def _fake_get_executor(handle: object) -> _FakeConnection:
            return _FakeConnection()

        async def _fake_cancel(handle: object) -> None:
            called["cancel"] += 1

        async def _fake_resolve_for_execution(agent: object, user_id: str) -> dict[str, str]:
            assert user_id == "owner@example.com"
            return {}

        app.state.providers.executor.spawn = _fake_spawn
        app.state.providers.executor.get_executor = _fake_get_executor
        app.state.providers.executor.cancel = _fake_cancel
        app.state.providers.secrets.resolve_for_execution = _fake_resolve_for_execution

        headers = _auth_headers(app, email="owner@example.com", role="user")
        test_response = client.post("/api/v1/agents/agent-mcp/mcp/test", headers=headers)
        tools_response = client.get("/api/v1/agents/agent-mcp/tools", headers=headers)

        assert test_response.status_code == 200
        assert tools_response.status_code == 200
        assert any(tool["name"] == "filesystem/read_file" for tool in tools_response.json())
        assert called == {"spawn": 2, "cancel": 2}
