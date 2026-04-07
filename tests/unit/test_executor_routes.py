from __future__ import annotations

import asyncio
from pathlib import Path

from fastapi.testclient import TestClient

from cognis.api.app import create_app
from cognis.api.routes import executors as executors_routes
from cognis.store.queries import create_executor, create_mcp_server, create_user


def _create_test_client(monkeypatch: object, tmp_path: Path) -> TestClient:
    monkeypatch.setenv("COGNIS_DATA_DIR", str(tmp_path))  # type: ignore[attr-defined]
    monkeypatch.setenv("COGNIS_HOST", "127.0.0.1")  # type: ignore[attr-defined]
    return TestClient(create_app())


def _auth_headers(app: object, *, email: str, role: str = "user") -> dict[str, str]:
    token = app.state.auth_provider.sign_access_token(email, email.split("@")[0].title(), role)  # type: ignore[attr-defined]
    return {"Authorization": f"Bearer {token}"}


def test_executor_update_bumps_desired_generation_and_marks_stale(
    monkeypatch: object, tmp_path: Path
) -> None:
    monkeypatch.setattr(
        executors_routes, "schedule_executor_reconfigure", lambda app, executor_id: None
    )
    with _create_test_client(monkeypatch, tmp_path) as client:

        async def _seed() -> None:
            async with client.app.state.session_factory() as session:
                await create_user(
                    session,
                    email="user@example.com",
                    name="User",
                    password_hash=client.app.state.password_hasher.hash("password123"),
                    role="user",
                )
                executor = await create_executor(
                    session,
                    executor_id="exec-1",
                    name="Exec",
                    executor_type="websocket",
                    owner_email="user@example.com",
                )
                await create_mcp_server(
                    session,
                    server_id="mcp-1",
                    name="Todoist",
                    transport="stdio",
                    command="npx",
                    owner_email="user@example.com",
                )
                executor.desired_config_version = 1
                executor.applied_config_version = 1
                executor.runtime_state = "active"
                await session.commit()

        asyncio.run(_seed())

        response = client.put(
            "/api/v1/executors/exec-1",
            headers=_auth_headers(client.app, email="user@example.com", role="user"),
            json={"config": {"mcp_server_ids": ["mcp-1"]}},
        )

        assert response.status_code == 200
        payload = response.json()
        assert payload["desired_config_version"] == 2
        assert payload["applied_config_version"] == 1
        assert payload["runtime_state"] == "stale"
