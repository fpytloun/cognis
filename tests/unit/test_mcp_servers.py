from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from cognis.api.app import create_app
from cognis.api.runtime_support import select_static_tools
from cognis.models.tool import MCP_SERVER_IDS_KEY, MCPServerConfig
from cognis.store.queries import (
    create_executor,
    create_mcp_server,
    create_user,
    delete_user_cascade,
)


def _create_test_client(monkeypatch: object, tmp_path: Path) -> TestClient:
    monkeypatch.setenv("COGNIS_DATA_DIR", str(tmp_path))  # type: ignore[attr-defined]
    monkeypatch.setenv("COGNIS_HOST", "127.0.0.1")  # type: ignore[attr-defined]
    app = create_app()
    return TestClient(app)


def _auth_headers(app: object, *, email: str, role: str = "user") -> dict[str, str]:
    token = app.state.auth_provider.sign_access_token(email, email.split("@")[0].title(), role)  # type: ignore[attr-defined]
    return {"Authorization": f"Bearer {token}"}


def test_mcp_server_config_validates_transport_fields() -> None:
    MCPServerConfig(name="stdio", transport="stdio", command="/bin/echo")
    MCPServerConfig(name="sse", transport="sse", url="http://localhost/sse")

    with pytest.raises(ValueError, match="command is required"):
        MCPServerConfig(name="broken", transport="stdio")
    with pytest.raises(ValueError, match="url is required"):
        MCPServerConfig(name="broken", transport="sse")


def test_select_static_tools_honors_disabled_categories_and_tools() -> None:
    agent = SimpleNamespace(
        tools={
            "delegation_tools": True,
            "disabled_categories": ["shell"],
            "disabled_tools": ["read"],
        },
        skills={},
    )
    selected = select_static_tools(agent)
    names = {tool.name for tool in selected}
    assert "bash" not in names
    assert "read" not in names


def test_non_admin_cannot_create_mcp_server(monkeypatch: object, tmp_path: Path) -> None:
    with _create_test_client(monkeypatch, tmp_path) as client:
        response = client.post(
            "/api/v1/mcp-servers",
            headers=_auth_headers(client.app, email="user@example.com", role="user"),
            json={"name": "demo", "transport": "stdio", "command": "/bin/echo"},
        )
        assert response.status_code == 403


def test_create_mcp_server_requires_command_for_stdio(monkeypatch: object, tmp_path: Path) -> None:
    with _create_test_client(monkeypatch, tmp_path) as client:
        response = client.post(
            "/api/v1/mcp-servers",
            headers=_auth_headers(client.app, email="admin@example.com", role="admin"),
            json={"name": "demo", "transport": "stdio"},
        )
        assert response.status_code == 422


def test_admin_can_create_and_list_mcp_servers(monkeypatch: object, tmp_path: Path) -> None:
    with _create_test_client(monkeypatch, tmp_path) as client:
        headers = _auth_headers(client.app, email="admin@example.com", role="admin")

        import asyncio

        async def _seed_user() -> None:
            async with client.app.state.session_factory() as session:
                await create_user(
                    session,
                    email="admin@example.com",
                    name="Admin",
                    password_hash=client.app.state.password_hasher.hash("password123"),
                    role="admin",
                )
                await session.commit()

        asyncio.run(_seed_user())

        create_response = client.post(
            "/api/v1/mcp-servers",
            headers=headers,
            json={
                "name": "demo",
                "transport": "stdio",
                "command": "/bin/echo",
                "env": {"API_TOKEN": "$secret:DEMO_TOKEN"},
            },
        )
        assert create_response.status_code == 200
        created = create_response.json()
        assert created["server_id"]
        assert created["name"] == "demo"
        assert created["env"]["API_TOKEN"] == "***"

        list_response = client.get("/api/v1/mcp-servers", headers=headers)
        assert list_response.status_code == 200
        listed = list_response.json()
        assert listed[0]["server_id"] == created["server_id"]


def test_delete_referenced_mcp_server_returns_409(monkeypatch: object, tmp_path: Path) -> None:
    with _create_test_client(monkeypatch, tmp_path) as client:
        app = client.app

        async def _seed() -> str:
            async with app.state.session_factory() as session:
                await create_user(
                    session,
                    email="admin@example.com",
                    name="Admin",
                    password_hash=app.state.password_hasher.hash("password123"),
                    role="admin",
                )
                server = await create_mcp_server(
                    session,
                    server_id="mcp_demo",
                    name="demo",
                    transport="stdio",
                    command="/bin/echo",
                    owner_email="admin@example.com",
                )
                await create_executor(
                    session,
                    executor_id="exec_demo",
                    name="Demo",
                    executor_type="in_process",
                    config={MCP_SERVER_IDS_KEY: [server.server_id]},
                    owner_email="admin@example.com",
                )
                await session.commit()
                return server.server_id

        import asyncio

        server_id = asyncio.run(_seed())
        response = client.delete(
            f"/api/v1/mcp-servers/{server_id}",
            headers=_auth_headers(client.app, email="admin@example.com", role="admin"),
        )
        assert response.status_code == 409


def test_update_mcp_server_revalidates_transport(monkeypatch: object, tmp_path: Path) -> None:
    with _create_test_client(monkeypatch, tmp_path) as client:
        app = client.app

        import asyncio

        async def _seed() -> str:
            async with app.state.session_factory() as session:
                await create_user(
                    session,
                    email="admin@example.com",
                    name="Admin",
                    password_hash=app.state.password_hasher.hash("password123"),
                    role="admin",
                )
                server = await create_mcp_server(
                    session,
                    server_id="mcp_update",
                    name="update-me",
                    transport="stdio",
                    command="/bin/echo",
                    owner_email="admin@example.com",
                )
                await session.commit()
                return server.server_id

        server_id = asyncio.run(_seed())
        response = client.put(
            f"/api/v1/mcp-servers/{server_id}",
            headers=_auth_headers(client.app, email="admin@example.com", role="admin"),
            json={"transport": "sse", "command": None},
        )
        assert response.status_code == 422


@pytest.mark.asyncio
async def test_delete_user_cascade_removes_owned_mcp_servers(tmp_path: Path) -> None:
    from cognis.store.database import create_engine, create_session_factory
    from cognis.store.models import Base

    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path}/cascade.db")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    session_factory = create_session_factory(engine)

    async with session_factory() as session:
        await create_user(
            session,
            email="owner@example.com",
            name="Owner",
            password_hash="hash",
            role="user",
        )
        await create_mcp_server(
            session,
            server_id="mcp_owned",
            name="owned",
            transport="stdio",
            command="/bin/echo",
            owner_email="owner@example.com",
        )
        await session.commit()

    async with session_factory() as session:
        deleted = await delete_user_cascade(session, "owner@example.com")
        await session.commit()
        assert deleted is True
        from cognis.store.queries import get_mcp_server

        assert await get_mcp_server(session, "mcp_owned") is None

    await engine.dispose()
