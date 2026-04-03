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


def test_workflow_list_includes_system_workflows(monkeypatch: object, tmp_path: Path) -> None:
    with _create_test_client(monkeypatch, tmp_path) as client:
        asyncio.run(_seed_user(client.app))

        response = client.get(
            "/api/v1/workflows",
            headers=_auth_headers(client.app, email="user@example.com"),
        )

        assert response.status_code == 200
        workflow_ids = {item["workflow_id"] for item in response.json()["items"]}
        assert "system:direct" in workflow_ids
        assert "system:research" in workflow_ids


def test_workflow_detail_supports_system_workflow(monkeypatch: object, tmp_path: Path) -> None:
    with _create_test_client(monkeypatch, tmp_path) as client:
        asyncio.run(_seed_user(client.app))

        response = client.get(
            "/api/v1/workflows/system:software-development",
            headers=_auth_headers(client.app, email="user@example.com"),
        )

        assert response.status_code == 200
        body = response.json()
        assert body["workflow_id"] == "system:software-development"
        assert body["is_system"] is True
        assert body["owner_email"] is None


def test_workflow_duplicate_supports_system_workflow(monkeypatch: object, tmp_path: Path) -> None:
    with _create_test_client(monkeypatch, tmp_path) as client:
        asyncio.run(_seed_user(client.app))

        response = client.post(
            "/api/v1/workflows/system:research/duplicate",
            headers=_auth_headers(client.app, email="user@example.com"),
        )

        assert response.status_code == 200
        body = response.json()
        assert body["workflow_id"].startswith("wf_")
        assert body["name"] == "Research Copy"
        assert body["is_system"] is False
        assert body["owner_email"] == "user@example.com"
        assert len(body["steps"]) == 3
