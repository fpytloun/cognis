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


def test_executor_list_includes_shared_for_regular_users(
    monkeypatch: object, tmp_path: Path
) -> None:
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
                await create_executor(
                    session,
                    executor_id="shared-exec",
                    name="Shared",
                    executor_type="websocket",
                    owner_email="user@example.com",
                    shared=True,
                )
                await create_executor(
                    session,
                    executor_id="private-exec",
                    name="Private",
                    executor_type="websocket",
                    owner_email="user@example.com",
                )
                await session.commit()

        asyncio.run(_seed())

        response = client.get(
            "/api/v1/executors",
            headers=_auth_headers(client.app, email="user@example.com", role="user"),
        )

        assert response.status_code == 200
        payload = response.json()
        assert {row["executor_id"] for row in payload} == {
            "default_inprocess",
            "shared-exec",
            "private-exec",
        }
        assert next(row for row in payload if row["executor_id"] == "shared-exec")["shared"] is True


def test_regular_user_cannot_create_shared_or_local_executors(
    monkeypatch: object, tmp_path: Path
) -> None:
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
                await session.commit()

        asyncio.run(_seed())

        local_response = client.post(
            "/api/v1/executors",
            headers=_auth_headers(client.app, email="user@example.com", role="user"),
            json={"name": "Local", "executor_type": "subprocess"},
        )
        shared_response = client.post(
            "/api/v1/executors",
            headers=_auth_headers(client.app, email="user@example.com", role="user"),
            json={"name": "Shared", "executor_type": "websocket", "shared": True},
        )

        assert local_response.status_code == 403
        assert shared_response.status_code == 403


def test_regular_user_cannot_mutate_owned_local_executor(
    monkeypatch: object, tmp_path: Path
) -> None:
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
                await create_executor(
                    session,
                    executor_id="owned-local",
                    name="Owned Local",
                    executor_type="in_process",
                    owner_email="user@example.com",
                )
                await session.commit()

        asyncio.run(_seed())

        headers = _auth_headers(client.app, email="user@example.com", role="user")
        update_response = client.put(
            "/api/v1/executors/owned-local",
            headers=headers,
            json={"name": "Edited Local"},
        )
        token_response = client.post("/api/v1/executors/owned-local/token", headers=headers)

        assert update_response.status_code == 403
        assert token_response.status_code == 403


def test_executor_token_generation_rotates_token_version(
    monkeypatch: object, tmp_path: Path
) -> None:
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
                await create_executor(
                    session,
                    executor_id="remote-1",
                    name="Remote",
                    executor_type="websocket",
                    owner_email="user@example.com",
                )
                await session.commit()

        asyncio.run(_seed())

        headers = _auth_headers(client.app, email="user@example.com", role="user")
        first = client.post("/api/v1/executors/remote-1/token", headers=headers)
        second = client.post("/api/v1/executors/remote-1/token", headers=headers)

        assert first.status_code == 200
        assert second.status_code == 200
        first_payload = first.json()
        second_payload = second.json()
        assert first_payload["expires_in"] is None
        assert second_payload["expires_in"] is None

        first_claims = client.app.state.auth_provider.verify_executor_token(first_payload["token"])
        second_claims = client.app.state.auth_provider.verify_executor_token(
            second_payload["token"]
        )
        assert first_claims["etv"] == 1
        assert second_claims["etv"] == 2

        async def _load_token_version() -> int:
            async with client.app.state.session_factory() as session:
                row = await executors_routes.get_executor_row(session, "remote-1")
                assert row is not None
                return row.token_version

        assert asyncio.run(_load_token_version()) == 2


def test_persistent_token_generation_rejects_non_websocket_executors(
    monkeypatch: object, tmp_path: Path
) -> None:
    with _create_test_client(monkeypatch, tmp_path) as client:

        async def _seed() -> None:
            async with client.app.state.session_factory() as session:
                await create_user(
                    session,
                    email="admin@example.com",
                    name="Admin",
                    password_hash=client.app.state.password_hasher.hash("password123"),
                    role="admin",
                )
                await create_executor(
                    session,
                    executor_id="subprocess-1",
                    name="Subprocess",
                    executor_type="subprocess",
                    owner_email="admin@example.com",
                )
                await session.commit()

        asyncio.run(_seed())

        response = client.post(
            "/api/v1/executors/subprocess-1/token",
            headers=_auth_headers(client.app, email="admin@example.com", role="admin"),
        )

        assert response.status_code == 400
        assert response.json()["error"]["code"] == "validation_error"
