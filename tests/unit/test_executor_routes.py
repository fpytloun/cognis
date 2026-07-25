from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path

from fastapi.testclient import TestClient

from cognis.api.app import create_app
from cognis.api.routes import executors as executors_routes
from cognis.store.queries import (
    bump_executor_reconfigure_generation,
    create_executor,
    create_mcp_server,
    create_user,
    get_executor_row,
    normalize_executor_desired_config_version,
)


def _create_test_client(monkeypatch: object, tmp_path: Path) -> TestClient:
    monkeypatch.setenv("COGNIS_DATA_DIR", str(tmp_path))  # type: ignore[attr-defined]
    monkeypatch.setenv("COGNIS_HOST", "127.0.0.1")  # type: ignore[attr-defined]
    return TestClient(create_app())


def _auth_headers(app: object, *, email: str, role: str = "user") -> dict[str, str]:
    token = app.state.auth_provider.sign_access_token(email, email.split("@")[0].title(), role)  # type: ignore[attr-defined]
    return {"Authorization": f"Bearer {token}"}


def test_executor_update_atomically_bumps_normalized_legacy_generation(
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
                executor.desired_config_version = 0
                executor.applied_config_version = 0
                executor.runtime_state = "active"
                await session.flush()
                normalized = await normalize_executor_desired_config_version(
                    session,
                    "exec-1",
                )
                assert normalized is True
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
        assert payload["applied_config_version"] == 0
        assert payload["runtime_state"] == "stale"


def test_legacy_normalization_and_atomic_bump_are_order_safe(
    monkeypatch: object,
    tmp_path: Path,
) -> None:
    with _create_test_client(monkeypatch, tmp_path) as client:

        async def _exercise_both_orders() -> None:
            async with client.app.state.session_factory() as session:
                await create_user(
                    session,
                    email="user@example.com",
                    name="User",
                    password_hash=client.app.state.password_hasher.hash("password123"),
                    role="user",
                )
                for executor_id in ("normalize-first", "bump-first"):
                    await create_executor(
                        session,
                        executor_id=executor_id,
                        name=executor_id,
                        executor_type="websocket",
                        owner_email="user@example.com",
                    )
                await session.commit()

            async with client.app.state.session_factory() as session:
                stale = await get_executor_row(session, "normalize-first")
                assert stale is not None
                assert stale.desired_config_version == 0

            async with client.app.state.session_factory() as session:
                assert (
                    await normalize_executor_desired_config_version(
                        session,
                        "normalize-first",
                    )
                    is True
                )
                await session.commit()
            async with client.app.state.session_factory() as session:
                assert (
                    await bump_executor_reconfigure_generation(
                        session,
                        "normalize-first",
                        runtime_state="reconfiguring",
                    )
                    is True
                )
                await session.commit()
                row = await get_executor_row(session, "normalize-first")
                assert row is not None
                assert row.desired_config_version == 2

            async with client.app.state.session_factory() as session:
                assert (
                    await bump_executor_reconfigure_generation(
                        session,
                        "bump-first",
                        runtime_state="reconfiguring",
                    )
                    is True
                )
                await session.commit()
            async with client.app.state.session_factory() as session:
                assert (
                    await normalize_executor_desired_config_version(
                        session,
                        "bump-first",
                    )
                    is False
                )
                await session.commit()
                row = await get_executor_row(session, "bump-first")
                assert row is not None
                assert row.desired_config_version == 1

        asyncio.run(_exercise_both_orders())


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


def test_executor_resource_snapshot_is_typed_fresh_and_owner_scoped(
    monkeypatch: object,
    tmp_path: Path,
) -> None:
    with _create_test_client(monkeypatch, tmp_path) as client:
        observed_at = datetime.now(UTC) - timedelta(minutes=3)
        received_at = datetime.now(UTC) - timedelta(seconds=15)

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
                    executor_id="alice-exec",
                    name="Alice executor",
                    executor_type="websocket",
                    owner_email="alice@example.com",
                )
                executor.runtime_metadata = {
                    "resource_snapshot": {
                        "observed_at": observed_at.isoformat(),
                        "os": "darwin",
                        "arch": "arm64",
                        "memory": {
                            "total_bytes": 64 * 1024**3,
                            "unified": True,
                        },
                        "private_path": "/Users/alice/.ollama/models",
                    },
                    "resource_snapshot_received_at": received_at.isoformat(),
                }
                await session.commit()

        asyncio.run(_seed())

        owner_response = client.get(
            "/api/v1/executors/alice-exec",
            headers=_auth_headers(client.app, email="alice@example.com"),
        )
        other_response = client.get(
            "/api/v1/executors/alice-exec",
            headers=_auth_headers(client.app, email="bob@example.com"),
        )

        assert owner_response.status_code == 200
        snapshot = owner_response.json()["resource_snapshot"]
        assert snapshot["os"] == "darwin"
        assert snapshot["memory"]["unified"] is True
        assert 0 <= snapshot["freshness"]["age_seconds"] <= 30
        assert snapshot["freshness"]["stale"] is False
        assert "private_path" not in snapshot
        assert "resource_snapshot" not in owner_response.json()["runtime_metadata"]
        assert "resource_snapshot_received_at" not in owner_response.json()["runtime_metadata"]
        assert other_response.status_code == 404


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


def test_executor_local_inference_config_validation_and_defaults(
    monkeypatch: object,
    tmp_path: Path,
) -> None:
    audit_events: list[dict[str, object]] = []
    monkeypatch.setattr(  # type: ignore[attr-defined]
        executors_routes._logger,
        "info",
        lambda _message, **kwargs: audit_events.append(kwargs["extra"]["extra_data"]),
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
                await session.commit()

        asyncio.run(_seed())
        headers = _auth_headers(client.app, email="user@example.com")

        legacy = client.post(
            "/api/v1/executors",
            headers=headers,
            json={"executor_id": "legacy", "name": "Legacy", "executor_type": "websocket"},
        )
        disabled = client.post(
            "/api/v1/executors",
            headers=headers,
            json={
                "executor_id": "disabled",
                "name": "Disabled",
                "executor_type": "websocket",
                "config": {"local_inference_enabled": False},
            },
        )
        invalid_configs = [
            {"local_inference_enabled": "false"},
            {"ollama_runtime": {"port": "22434"}},
            {"ollama_runtime": {"port": 0}},
            {"ollama_runtime": {"port": 65536}},
            {"ollama_runtime": {"endpoint": "http://localhost:11434"}},
            {"ollama_runtime": {"endpoint": "http://127.0.0.1:22434"}},
        ]
        invalid_responses = [
            client.post(
                "/api/v1/executors",
                headers=headers,
                json={
                    "name": f"Invalid {index}",
                    "executor_type": "websocket",
                    "config": config,
                },
            )
            for index, config in enumerate(invalid_configs)
        ]
        updated = client.put(
            "/api/v1/executors/disabled",
            headers=headers,
            json={
                "config": {
                    "local_inference_enabled": True,
                    "ollama_runtime": {
                        "port": 22434,
                        "management_enabled": False,
                    },
                }
            },
        )
        stale_update = client.put(
            "/api/v1/executors/disabled",
            headers=headers,
            json={
                "expected_config_version": disabled.json()["desired_config_version"],
                "config": {"local_inference_enabled": False},
            },
        )

        assert legacy.status_code == 201
        assert legacy.json()["local_inference_enabled"] is True
        assert legacy.json()["ollama_management_enabled"] is True
        assert legacy.json()["ollama_port"] == 11434
        assert legacy.json()["ollama_endpoint"] == "http://127.0.0.1:11434"
        assert legacy.json()["config"]["ollama_runtime"]["port"] == 11434
        assert "endpoint" not in legacy.json()["config"]["ollama_runtime"]
        assert disabled.status_code == 201
        assert disabled.json()["local_inference_enabled"] is False
        assert disabled.json()["ollama_management_enabled"] is False
        assert all(response.status_code == 400 for response in invalid_responses)
        assert updated.status_code == 200
        assert updated.json()["ollama_port"] == 22434
        assert updated.json()["ollama_endpoint"] == "http://127.0.0.1:22434"
        assert updated.json()["local_inference_config_status"] == "applying"
        assert stale_update.status_code == 409
        assert stale_update.json()["error"]["code"] == "executor_config_conflict"
        audit = next(
            event
            for event in audit_events
            if event.get("action") == "executor.local_inference.update"
        )
        assert audit["actor_email"] == "user@example.com"
        assert audit["executor_id"] == "disabled"
        assert audit["previous"]["local_inference_enabled"] is False
        assert audit["current"] == {
            "local_inference_enabled": True,
            "ollama_management_enabled": False,
        }


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
