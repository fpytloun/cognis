from __future__ import annotations

import asyncio
from pathlib import Path

from fastapi.testclient import TestClient

from cognis.api.app import create_app
from cognis.store.queries import create_user


def _create_test_client(monkeypatch: object, tmp_path: Path) -> TestClient:
    monkeypatch.setenv("COGNIS_DATA_DIR", str(tmp_path))  # type: ignore[attr-defined]
    monkeypatch.setenv("COGNIS_HOST", "127.0.0.1")  # type: ignore[attr-defined]
    app = create_app()
    return TestClient(app)


def _auth_headers(app: object, *, email: str, role: str = "user") -> dict[str, str]:
    token = app.state.auth_provider.sign_access_token(email, email.split("@")[0].title(), role)  # type: ignore[attr-defined]
    return {"Authorization": f"Bearer {token}"}


async def _seed_user(app: object, email: str = "user@example.com") -> None:
    async with app.state.session_factory() as session:  # type: ignore[attr-defined]
        await create_user(
            session,
            email=email,
            name="User",
            password_hash=app.state.password_hasher.hash("password123"),  # type: ignore[attr-defined]
            role="user",
        )
        await session.commit()


def test_list_skills_marks_system_skills(monkeypatch: object, tmp_path: Path) -> None:
    with _create_test_client(monkeypatch, tmp_path) as client:
        asyncio.run(_seed_user(client.app))

        response = client.get(
            "/api/v1/skills", headers=_auth_headers(client.app, email="user@example.com")
        )

        assert response.status_code == 200
        skills = {item["skill_id"]: item for item in response.json()}
        assert skills["cognis-task-manager"]["is_system"] is True
        assert skills["cognis-workflow-manager"]["is_system"] is True
        assert "attach_to_all_agents" in skills["cognis-task-manager"]


def test_skill_create_accepts_attach_to_all_agents(monkeypatch: object, tmp_path: Path) -> None:
    with _create_test_client(monkeypatch, tmp_path) as client:
        asyncio.run(_seed_user(client.app))
        headers = _auth_headers(client.app, email="user@example.com")

        response = client.post(
            "/api/v1/skills",
            headers=headers,
            json={
                "name": "Custom",
                "instructions": "hello",
                "attach_to_all_agents": True,
            },
        )

        assert response.status_code == 201
        body = response.json()
        assert body["attach_to_all_agents"] is True
        assert body["auto_load"] is True


def test_system_skill_delete_is_forbidden(monkeypatch: object, tmp_path: Path) -> None:
    with _create_test_client(monkeypatch, tmp_path) as client:
        asyncio.run(_seed_user(client.app))

        response = client.delete(
            "/api/v1/skills/cognis-task-manager",
            headers=_auth_headers(client.app, email="user@example.com"),
        )

        assert response.status_code == 403


def test_system_skill_reset_restores_default(monkeypatch: object, tmp_path: Path) -> None:
    with _create_test_client(monkeypatch, tmp_path) as client:
        asyncio.run(_seed_user(client.app, email="admin@example.com"))
        headers = _auth_headers(client.app, email="admin@example.com", role="admin")

        update = client.put(
            "/api/v1/skills/cognis-task-manager",
            headers=headers,
            json={"instructions": "custom"},
        )
        assert update.status_code == 403

        reset = client.post(
            "/api/v1/skills/cognis-task-manager/reset",
            headers=headers,
        )

        assert reset.status_code == 200
        body = reset.json()
        assert body["is_system"] is True
        assert body["instructions"].startswith("# Purpose")
        assert body["name"] == "Cognis Task Manager"
        assert body["tags"] == ["cognis", "management", "tasks"]
        assert body["current_version"] is not None


def test_non_system_skill_reset_is_forbidden(monkeypatch: object, tmp_path: Path) -> None:
    with _create_test_client(monkeypatch, tmp_path) as client:
        asyncio.run(_seed_user(client.app))
        headers = _auth_headers(client.app, email="user@example.com")

        created = client.post(
            "/api/v1/skills",
            headers=headers,
            json={"name": "Custom", "instructions": "hello"},
        )
        assert created.status_code == 201

        response = client.post(
            f"/api/v1/skills/{created.json()['skill_id']}/reset",
            headers=headers,
        )

        assert response.status_code == 403


def test_non_admin_cannot_reset_system_skill(monkeypatch: object, tmp_path: Path) -> None:
    with _create_test_client(monkeypatch, tmp_path) as client:
        asyncio.run(_seed_user(client.app))

        response = client.post(
            "/api/v1/skills/cognis-task-manager/reset",
            headers=_auth_headers(client.app, email="user@example.com"),
        )

        assert response.status_code == 403


def test_reset_system_skill_is_idempotent_when_already_default(
    monkeypatch: object, tmp_path: Path
) -> None:
    with _create_test_client(monkeypatch, tmp_path) as client:
        asyncio.run(_seed_user(client.app, email="admin@example.com"))
        headers = _auth_headers(client.app, email="admin@example.com", role="admin")

        first = client.get("/api/v1/skills/cognis-task-manager", headers=headers)
        assert first.status_code == 200
        original_version = first.json()["current_version_id"]

        reset = client.post("/api/v1/skills/cognis-task-manager/reset", headers=headers)

        assert reset.status_code == 200
        assert reset.json()["current_version_id"] == original_version
