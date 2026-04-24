from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from cognis.api.app import create_app
from cognis.api.runtime_support import select_static_tools
from cognis.models.tool import (
    MCP_SERVER_IDS_KEY,
    MCPServerConfig,
    ToolSource,
    sanitize_mcp_tool_name,
)
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
    MCPServerConfig(
        name="sse",
        transport="sse",
        url="http://localhost/sse",
        headers={"Authorization": "$secret:demo"},
    )

    with pytest.raises(ValueError, match="command is required"):
        MCPServerConfig(name="broken", transport="stdio")
    with pytest.raises(ValueError, match="url is required"):
        MCPServerConfig(name="broken", transport="sse")
    with pytest.raises(ValueError, match="headers are not allowed"):
        MCPServerConfig(
            name="broken",
            transport="stdio",
            command="/bin/echo",
            headers={"Authorization": "Bearer demo"},
        )
    with pytest.raises(ValueError, match="env is not allowed"):
        MCPServerConfig(
            name="broken",
            transport="streamable_http",
            url="http://localhost/mcp",
            env={"API_TOKEN": "$secret:demo"},
        )


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


def test_regular_user_can_create_private_mcp_server(monkeypatch: object, tmp_path: Path) -> None:
    with _create_test_client(monkeypatch, tmp_path) as client:
        import asyncio

        async def _seed_user() -> None:
            async with client.app.state.session_factory() as session:
                await create_user(
                    session,
                    email="user@example.com",
                    name="User",
                    password_hash=client.app.state.password_hasher.hash("password123"),
                    role="user",
                )
                await session.commit()

        asyncio.run(_seed_user())
        response = client.post(
            "/api/v1/mcp-servers",
            headers=_auth_headers(client.app, email="user@example.com", role="user"),
            json={"name": "demo", "transport": "stdio", "command": "/bin/echo"},
        )
        assert response.status_code == 200


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
        assert created["env"]["API_TOKEN"] == "$secret:DEMO_TOKEN"

        list_response = client.get("/api/v1/mcp-servers", headers=headers)
        assert list_response.status_code == 200
        listed = list_response.json()
        assert listed[0]["server_id"] == created["server_id"]


def test_admin_can_create_http_mcp_server_with_headers(monkeypatch: object, tmp_path: Path) -> None:
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
                "name": "remote-demo",
                "transport": "sse",
                "url": "http://localhost:3000/sse",
                "headers": {"authorization": "$secret:DEMO_TOKEN"},
            },
        )
        assert create_response.status_code == 200
        created = create_response.json()
        assert created["headers"]["Authorization"] == "$secret:DEMO_TOKEN"
        assert created["env"] == {}


def test_http_mcp_server_rejects_env_payload(monkeypatch: object, tmp_path: Path) -> None:
    with _create_test_client(monkeypatch, tmp_path) as client:
        response = client.post(
            "/api/v1/mcp-servers",
            headers=_auth_headers(client.app, email="admin@example.com", role="admin"),
            json={
                "name": "broken-http",
                "transport": "streamable_http",
                "url": "http://localhost:3000/mcp",
                "env": {"API_TOKEN": "$secret:DEMO_TOKEN"},
            },
        )
        assert response.status_code == 422


def test_mcp_servers_are_user_scoped(monkeypatch: object, tmp_path: Path) -> None:
    with _create_test_client(monkeypatch, tmp_path) as client:
        import asyncio

        async def _seed() -> None:
            async with client.app.state.session_factory() as session:
                for email in ("alice@example.com", "bob@example.com"):
                    await create_user(
                        session,
                        email=email,
                        name=email.split("@")[0].title(),
                        password_hash=client.app.state.password_hasher.hash("password123"),
                        role="user",
                    )
                await create_mcp_server(
                    session,
                    server_id="mcp_alice",
                    name="alice-server",
                    transport="stdio",
                    command="/bin/echo",
                    owner_email="alice@example.com",
                )
                await session.commit()

        asyncio.run(_seed())

        alice = client.get(
            "/api/v1/mcp-servers",
            headers=_auth_headers(client.app, email="alice@example.com", role="user"),
        )
        bob = client.get(
            "/api/v1/mcp-servers",
            headers=_auth_headers(client.app, email="bob@example.com", role="user"),
        )
        assert len(alice.json()) == 1
        assert bob.json() == []


def test_regular_user_can_list_but_not_mutate_shared_mcp_server(
    monkeypatch: object, tmp_path: Path
) -> None:
    with _create_test_client(monkeypatch, tmp_path) as client:
        import asyncio

        async def _seed() -> None:
            async with client.app.state.session_factory() as session:
                await create_user(
                    session,
                    email="user@example.com",
                    name="User",
                    password_hash=client.app.state.password_hasher.hash("password123"),
                    role="user",
                )
                await create_mcp_server(
                    session,
                    server_id="shared_mcp",
                    name="shared-server",
                    transport="stdio",
                    command="/bin/echo",
                    owner_email="admin@example.com",
                    shared=True,
                )
                await session.commit()

        asyncio.run(_seed())

        headers = _auth_headers(client.app, email="user@example.com", role="user")
        list_response = client.get("/api/v1/mcp-servers", headers=headers)
        update_response = client.put(
            "/api/v1/mcp-servers/shared_mcp",
            headers=headers,
            json={"name": "changed"},
        )
        delete_response = client.delete("/api/v1/mcp-servers/shared_mcp", headers=headers)

        assert list_response.status_code == 200
        assert list_response.json()[0]["shared"] is True
        assert update_response.status_code == 403
        assert delete_response.status_code == 403


def test_effective_tools_preview_respects_executor_owner_scope(
    monkeypatch: object, tmp_path: Path
) -> None:
    with _create_test_client(monkeypatch, tmp_path) as client:
        import asyncio

        async def _seed() -> None:
            async with client.app.state.session_factory() as session:
                for email in ("alice@example.com", "bob@example.com"):
                    await create_user(
                        session,
                        email=email,
                        name=email.split("@")[0].title(),
                        password_hash=client.app.state.password_hasher.hash("password123"),
                        role="user",
                    )
                executor = await create_executor(
                    session,
                    executor_id="bob_exec",
                    name="Bob exec",
                    executor_type="websocket",
                    labels={"tier": "shared"},
                    owner_email="bob@example.com",
                )
                executor.observed_tools = [
                    {
                        "name": "read",
                        "description": "Read file",
                        "parameters": {},
                        "source": ToolSource(type="executor").model_dump(mode="json"),
                        "category": "filesystem",
                        "read_only": True,
                        "timeout_seconds": 30,
                        "non_bypassable": False,
                    }
                ]
                executor.runtime_state = "active"
                await session.commit()

        asyncio.run(_seed())

        response = client.post(
            "/api/v1/agents/effective-tools/preview",
            headers=_auth_headers(client.app, email="alice@example.com", role="user"),
            json={"execution": {"executor_selector": {"tier": "shared"}}},
        )
        assert response.status_code == 200
        payload = response.json()
        assert payload["executor"]["executor_id"] is None
        assert payload["warnings"]


def test_effective_tools_live_state_includes_merged_intaris_tools_for_websocket(
    monkeypatch: object, tmp_path: Path
) -> None:
    with _create_test_client(monkeypatch, tmp_path) as client:
        import asyncio

        async def _seed() -> None:
            async with client.app.state.session_factory() as session:
                await create_user(
                    session,
                    email="alice@example.com",
                    name="Alice",
                    password_hash=client.app.state.password_hasher.hash("password123"),
                    role="user",
                )
                executor = await create_executor(
                    session,
                    executor_id="alice_exec",
                    name="Alice exec",
                    executor_type="websocket",
                    owner_email="alice@example.com",
                )
                executor.runtime_state = "active"
                await session.commit()

        asyncio.run(_seed())

        class _Conn:
            async def list_tools(self) -> list[dict[str, object]]:
                return [
                    {
                        "name": "read",
                        "description": "Read file",
                        "parameters": {},
                        "source": ToolSource(type="executor").model_dump(mode="json"),
                        "category": "filesystem",
                        "read_only": True,
                        "timeout_seconds": 30,
                        "non_bypassable": False,
                    }
                ]

        client.app.state.providers.executor.websocket.get_connection = lambda executor_id: (  # type: ignore[method-assign]
            _Conn() if executor_id == "alice_exec" else None
        )

        async def _list_mcp_tools() -> list[dict[str, object]]:
            return [
                {
                    "server": "github",
                    "tool": "search/issues",
                    "description": "Search issues",
                    "inputSchema": {"type": "object", "properties": {}},
                }
            ]

        client.app.state.providers.guardrails.list_mcp_tools = _list_mcp_tools  # type: ignore[method-assign]

        response = client.post(
            "/api/v1/agents/effective-tools/preview",
            headers=_auth_headers(client.app, email="alice@example.com", role="user"),
            json={
                "execution": {"executor_id": "alice_exec"},
                "tools": {"intaris_mcp_servers": ["github"]},
            },
        )
        assert response.status_code == 200
        payload = response.json()
        live_names = {tool["name"] for tool in payload["live_state"]["tools"]}
        assert "read" in live_names
        assert sanitize_mcp_tool_name("github", "search/issues") in live_names


def test_effective_tools_preview_discovers_http_mcp_for_in_process_executor(
    monkeypatch: object, tmp_path: Path
) -> None:
    with _create_test_client(monkeypatch, tmp_path) as client:
        app = client.app

        import asyncio

        async def _seed() -> None:
            async with app.state.session_factory() as session:
                await create_user(
                    session,
                    email="alice@example.com",
                    name="Alice",
                    password_hash=app.state.password_hasher.hash("password123"),
                    role="user",
                )
                await create_mcp_server(
                    session,
                    server_id="mcp_http_preview",
                    name="http-preview",
                    transport="sse",
                    url="http://localhost:3000/sse",
                    headers={"Authorization": "$secret:DEMO_TOKEN"},
                    owner_email="alice@example.com",
                )
                await create_executor(
                    session,
                    executor_id="alice_exec",
                    name="Alice exec",
                    executor_type="in_process",
                    config={MCP_SERVER_IDS_KEY: ["mcp_http_preview"]},
                    owner_email="alice@example.com",
                )
                await session.commit()

        asyncio.run(_seed())

        class _FakeClient:
            def __init__(self) -> None:
                self.connected = False

            async def connect(self) -> None:
                self.connected = True

            async def list_tools(self) -> list[dict[str, object]]:
                return [
                    {
                        "name": "inspect",
                        "description": "Inspect remote resource",
                        "inputSchema": {"type": "object", "properties": {}},
                    }
                ]

            async def call_tool(self, tool_name: str, arguments: dict[str, object]) -> str:
                del tool_name, arguments
                return "ok"

            async def close(self) -> None:
                self.connected = False

        monkeypatch.setattr(
            "cognis.api.routes.tools.build_mcp_client",
            lambda server, secrets: _FakeClient(),
        )

        response = client.post(
            "/api/v1/agents/effective-tools/preview",
            headers=_auth_headers(client.app, email="alice@example.com", role="user"),
            json={"execution": {"executor_id": "alice_exec"}},
        )
        assert response.status_code == 200
        payload = response.json()
        configured_names = {tool["name"] for tool in payload["configured_state"]["tools"]}
        assert sanitize_mcp_tool_name("http-preview", "inspect") in configured_names


def test_effective_tools_live_state_skips_stale_websocket_executor(
    monkeypatch: object, tmp_path: Path
) -> None:
    with _create_test_client(monkeypatch, tmp_path) as client:
        import asyncio

        async def _seed() -> None:
            async with client.app.state.session_factory() as session:
                await create_user(
                    session,
                    email="alice@example.com",
                    name="Alice",
                    password_hash=client.app.state.password_hasher.hash("password123"),
                    role="user",
                )
                executor = await create_executor(
                    session,
                    executor_id="alice_exec",
                    name="Alice exec",
                    executor_type="websocket",
                    owner_email="alice@example.com",
                )
                executor.runtime_state = "active"
                executor.desired_config_version = 2
                executor.applied_config_version = 1
                await session.commit()

        asyncio.run(_seed())

        class _Conn:
            async def list_tools(self) -> list[dict[str, object]]:
                return []

        client.app.state.providers.executor.websocket.get_connection = lambda executor_id: (  # type: ignore[method-assign]
            _Conn() if executor_id == "alice_exec" else None
        )

        response = client.post(
            "/api/v1/agents/effective-tools/preview",
            headers=_auth_headers(client.app, email="alice@example.com", role="user"),
            json={"execution": {"executor_id": "alice_exec"}},
        )

        assert response.status_code == 200
        payload = response.json()
        assert payload["live_state"]["connected"] is False
        assert any("offline or not ready" in warning for warning in payload["warnings"])


def test_list_intaris_mcp_tools_returns_normalized_rows(
    monkeypatch: object, tmp_path: Path
) -> None:
    with _create_test_client(monkeypatch, tmp_path) as client:
        import asyncio

        async def _seed() -> None:
            async with client.app.state.session_factory() as session:
                await create_user(
                    session,
                    email="alice@example.com",
                    name="Alice",
                    password_hash=client.app.state.password_hasher.hash("password123"),
                    role="user",
                )
                await session.commit()

        asyncio.run(_seed())

        async def _list_mcp_tools() -> list[dict[str, object]]:
            return [
                {
                    "server": "github",
                    "tool": "search/issues",
                    "description": "Search issues",
                    "inputSchema": {"type": "object", "properties": {"query": {"type": "string"}}},
                },
                {
                    "server": "github",
                    "description": "Malformed row",
                },
            ]

        async def _list_mcp_servers(enabled_only: bool = True) -> list[dict[str, object]]:
            assert enabled_only is True
            return [
                {
                    "name": "github",
                    "tools_cache": [
                        {
                            "name": "search/issues",
                            "description": "Cached duplicate",
                            "inputSchema": {"type": "object", "properties": {}},
                        }
                    ],
                }
            ]

        client.app.state.providers.guardrails.list_mcp_tools = _list_mcp_tools  # type: ignore[method-assign]
        client.app.state.providers.guardrails.list_mcp_servers = _list_mcp_servers  # type: ignore[method-assign]

        response = client.get(
            "/api/v1/intaris/mcp/tools",
            headers=_auth_headers(client.app, email="alice@example.com", role="user"),
        )

        assert response.status_code == 200
        payload = response.json()
        assert len(payload) == 1
        assert payload[0]["name"] == sanitize_mcp_tool_name("github", "search/issues")
        assert payload[0]["source"]["type"] == "intaris_mcp"
        assert payload[0]["source"]["server_name"] == "github"


def test_list_observed_local_mcp_tools_dedupes_and_filters_scope(
    monkeypatch: object, tmp_path: Path
) -> None:
    with _create_test_client(monkeypatch, tmp_path) as client:
        app = client.app

        import asyncio

        async def _seed() -> None:
            async with app.state.session_factory() as session:
                for email in ("alice@example.com", "bob@example.com"):
                    await create_user(
                        session,
                        email=email,
                        name=email.split("@")[0].title(),
                        password_hash=app.state.password_hasher.hash("password123"),
                        role="user",
                    )

                alice_one = await create_executor(
                    session,
                    executor_id="alice_exec_1",
                    name="Alice Exec 1",
                    executor_type="websocket",
                    owner_email="alice@example.com",
                )
                alice_two = await create_executor(
                    session,
                    executor_id="alice_exec_2",
                    name="Alice Exec 2",
                    executor_type="websocket",
                    owner_email="alice@example.com",
                )
                bob_exec = await create_executor(
                    session,
                    executor_id="bob_exec",
                    name="Bob Exec",
                    executor_type="websocket",
                    owner_email="bob@example.com",
                )

                observed_local = {
                    "name": sanitize_mcp_tool_name("github", "search/issues"),
                    "description": "Search issues",
                    "parameters": {"type": "object", "properties": {}},
                    "source": ToolSource(
                        type="local_mcp",
                        server_id="srv_github",
                        server_name="github",
                        raw_tool_name="search/issues",
                    ).model_dump(mode="json"),
                    "category": "mcp",
                    "read_only": True,
                    "timeout_seconds": 30,
                    "non_bypassable": False,
                }
                alice_one.observed_tools = [
                    observed_local,
                    {
                        "name": "read",
                        "description": "Read file",
                        "parameters": {},
                        "source": ToolSource(type="executor").model_dump(mode="json"),
                        "category": "filesystem",
                        "read_only": True,
                        "timeout_seconds": 30,
                        "non_bypassable": False,
                    },
                    {"bad": "row"},
                ]
                alice_two.observed_tools = [observed_local]
                bob_exec.observed_tools = [
                    {
                        **observed_local,
                        "source": ToolSource(
                            type="local_mcp",
                            server_id="srv_bob",
                            server_name="bob-github",
                            raw_tool_name="search/issues",
                        ).model_dump(mode="json"),
                    }
                ]
                await session.commit()

        asyncio.run(_seed())

        response = client.get(
            "/api/v1/tools/local-mcp/observed",
            headers=_auth_headers(client.app, email="alice@example.com", role="user"),
        )

        assert response.status_code == 200
        payload = response.json()
        assert len(payload) == 1
        assert payload[0]["source"]["type"] == "local_mcp"
        assert payload[0]["source"]["server_id"] == "srv_github"


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


def test_invalid_http_mcp_server_is_flagged_in_list(monkeypatch: object, tmp_path: Path) -> None:
    with _create_test_client(monkeypatch, tmp_path) as client:
        app = client.app

        import asyncio

        async def _seed() -> None:
            async with app.state.session_factory() as session:
                await create_user(
                    session,
                    email="admin@example.com",
                    name="Admin",
                    password_hash=app.state.password_hasher.hash("password123"),
                    role="admin",
                )
                await create_mcp_server(
                    session,
                    server_id="mcp_invalid_http",
                    name="invalid-http",
                    transport="sse",
                    url="http://localhost:3000/sse",
                    env={"API_TOKEN": "$secret:DEMO_TOKEN"},
                    owner_email="admin@example.com",
                )
                await session.commit()

        asyncio.run(_seed())

        response = client.get(
            "/api/v1/mcp-servers",
            headers=_auth_headers(client.app, email="admin@example.com", role="admin"),
        )
        assert response.status_code == 200
        payload = response.json()
        assert payload[0]["invalid_reason"] is not None


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
