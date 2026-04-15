from __future__ import annotations

import asyncio
from pathlib import Path

from fastapi.testclient import TestClient

from cognis.api.app import create_app
from cognis.store.queries import create_user


def _create_test_client(monkeypatch: object, tmp_path: Path) -> TestClient:
    monkeypatch.setenv("COGNIS_DATA_DIR", str(tmp_path))  # type: ignore[attr-defined]
    monkeypatch.setenv("COGNIS_HOST", "127.0.0.1")  # type: ignore[attr-defined]
    return TestClient(create_app())


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


def test_system_agent_detail_includes_default_skills(monkeypatch: object, tmp_path: Path) -> None:
    with _create_test_client(monkeypatch, tmp_path) as client:
        asyncio.run(_seed_user(client.app))
        headers = _auth_headers(client.app, email="user@example.com")

        response = client.get("/api/v1/agents/system:implement", headers=headers)

        assert response.status_code == 200
        body = response.json()
        assert body["skills"] == {"items": [{"skill_id": "cognis-coding", "enabled": True}]}
        assert "skills" in body["editable_fields"]


def test_system_agent_update_accepts_skill_overrides(monkeypatch: object, tmp_path: Path) -> None:
    with _create_test_client(monkeypatch, tmp_path) as client:
        asyncio.run(_seed_user(client.app))
        headers = _auth_headers(client.app, email="user@example.com")

        update = client.put(
            "/api/v1/agents/system:implement",
            headers=headers,
            json={
                "skills": {
                    "items": [{"skill_id": "cognis-task-manager", "enabled": True}],
                }
            },
        )

        assert update.status_code == 200
        body = update.json()
        assert body["has_overrides"] is True
        assert body["skills"] == {"items": [{"skill_id": "cognis-task-manager", "enabled": True}]}

        detail = client.get("/api/v1/agents/system:implement", headers=headers)
        assert detail.status_code == 200
        assert detail.json()["skills"] == {
            "items": [{"skill_id": "cognis-task-manager", "enabled": True}]
        }


def test_reset_system_agent_overrides_restores_default_skills(
    monkeypatch: object, tmp_path: Path
) -> None:
    with _create_test_client(monkeypatch, tmp_path) as client:
        asyncio.run(_seed_user(client.app))
        headers = _auth_headers(client.app, email="user@example.com")

        update = client.put(
            "/api/v1/agents/system:implement",
            headers=headers,
            json={
                "skills": {
                    "items": [{"skill_id": "cognis-task-manager", "enabled": True}],
                }
            },
        )
        assert update.status_code == 200

        reset = client.post("/api/v1/agents/system:implement/reset-overrides", headers=headers)

        assert reset.status_code == 200
        assert reset.json() == {"ok": True}

        detail = client.get("/api/v1/agents/system:implement", headers=headers)
        assert detail.status_code == 200
        assert detail.json()["skills"] == {
            "items": [{"skill_id": "cognis-coding", "enabled": True}]
        }


def test_partial_system_agent_override_updates_preserve_existing_fields(
    monkeypatch: object, tmp_path: Path
) -> None:
    with _create_test_client(monkeypatch, tmp_path) as client:
        asyncio.run(_seed_user(client.app))
        headers = _auth_headers(client.app, email="user@example.com")

        first = client.put(
            "/api/v1/agents/system:implement",
            headers=headers,
            json={
                "skills": {
                    "items": [{"skill_id": "cognis-task-manager", "enabled": True}],
                }
            },
        )
        assert first.status_code == 200

        second = client.put(
            "/api/v1/agents/system:implement",
            headers=headers,
            json={"llm_config": {"reasoning_effort": "high"}},
        )

        assert second.status_code == 200
        body = second.json()
        assert body["skills"] == {"items": [{"skill_id": "cognis-task-manager", "enabled": True}]}
        assert body["llm_config"]["reasoning_effort"] == "high"
