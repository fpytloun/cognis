from __future__ import annotations

import asyncio
from pathlib import Path

from fastapi.testclient import TestClient

from cognis.api.app import create_app
from cognis.store.queries import create_user, create_workflow


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
        assert "system:general-task" in workflow_ids
        assert "system:research" in workflow_ids


def test_agent_list_includes_system_agents(monkeypatch: object, tmp_path: Path) -> None:
    with _create_test_client(monkeypatch, tmp_path) as client:
        asyncio.run(_seed_user(client.app))

        response = client.get(
            "/api/v1/agents",
            headers=_auth_headers(client.app, email="user@example.com"),
        )

        assert response.status_code == 200
        agent_ids = {item["agent_id"] for item in response.json()["items"]}
        assert "system:explore" in agent_ids
        assert "system:research" in agent_ids
        assert "system:implement" in agent_ids


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


def test_workflow_create_accepts_null_workflow_id(monkeypatch: object, tmp_path: Path) -> None:
    with _create_test_client(monkeypatch, tmp_path) as client:
        asyncio.run(_seed_user(client.app))

        response = client.post(
            "/api/v1/workflows",
            headers=_auth_headers(client.app, email="user@example.com"),
            json={
                "workflow_id": None,
                "name": "Daily Brief",
                "steps": [{"name": "collect", "type": "run", "prompt": "Collect inputs."}],
            },
        )

        assert response.status_code == 200
        assert response.json()["workflow_id"].startswith("wf_")


def test_workflow_duplicate_allows_admin_for_user_owned_workflow(
    monkeypatch: object, tmp_path: Path
) -> None:
    with _create_test_client(monkeypatch, tmp_path) as client:
        asyncio.run(_seed_user(client.app, email="owner@example.com"))
        asyncio.run(_seed_user(client.app, email="admin@example.com"))

        async def _seed_workflow() -> None:
            async with client.app.state.session_factory() as session:  # type: ignore[attr-defined]
                await create_workflow(
                    session,
                    workflow_id="wf_owner_private",
                    name="Owner Workflow",
                    description="desc",
                    definition={
                        "workflow_id": "wf_owner_private",
                        "name": "Owner Workflow",
                        "description": "desc",
                        "version": 1,
                        "criteria": "",
                        "tags": [],
                        "interaction": {},
                        "defaults": {},
                        "steps": [{"name": "plan", "type": "run"}],
                        "is_system": False,
                        "owner_email": "owner@example.com",
                    },
                    is_system=False,
                    owner_email="owner@example.com",
                )
                await session.commit()

        asyncio.run(_seed_workflow())

        response = client.post(
            "/api/v1/workflows/wf_owner_private/duplicate",
            headers=_auth_headers(client.app, email="admin@example.com", role="admin"),
        )

        assert response.status_code == 200
        body = response.json()
        assert body["name"] == "Owner Workflow Copy"
        assert body["owner_email"] == "admin@example.com"
