from __future__ import annotations

import asyncio
from pathlib import Path

from fastapi.testclient import TestClient

from cognis.api.app import create_app
from cognis.store.queries import (
    create_agent,
    create_skill,
    create_skill_version,
    create_user,
    set_current_version,
)


def _create_test_client(monkeypatch: object, tmp_path: Path) -> TestClient:
    monkeypatch.setenv("COGNIS_DATA_DIR", str(tmp_path))  # type: ignore[attr-defined]
    monkeypatch.setenv("COGNIS_HOST", "127.0.0.1")  # type: ignore[attr-defined]
    return TestClient(create_app())


def _auth_headers(app: object, *, email: str, role: str = "user") -> dict[str, str]:
    token = app.state.auth_provider.sign_access_token(email, email.split("@")[0].title(), role)  # type: ignore[attr-defined]
    return {"Authorization": f"Bearer {token}"}


async def _seed_skill_workflow_source(app: object) -> str:
    async with app.state.session_factory() as session:  # type: ignore[attr-defined]
        await create_user(
            session,
            email="user@example.com",
            name="User",
            password_hash=app.state.password_hasher.hash("password123"),  # type: ignore[attr-defined]
            role="user",
        )
        await create_agent(
            session,
            agent_id="agent-1",
            owner_email="user@example.com",
            name="Agent 1",
            status="active",
            skills={"items": [{"skill_id": "skill_release", "enabled": True}]},
        )
        await create_agent(
            session,
            agent_id="agent-2",
            owner_email="user@example.com",
            name="Agent 2",
            status="active",
        )
        skill = await create_skill(
            session,
            skill_id="skill_release",
            name="Release Helper",
            description="Coordinates release tasks.",
            instructions="Gather release notes and publish the release.",
            tags=["release"],
            owner_email="user@example.com",
        )
        version = await create_skill_version(
            session,
            skill_id=skill.skill_id,
            version_number=1,
            content_hash="a" * 64,
            instructions="Gather release notes and publish the release.",
            steps=[
                {
                    "name": "gather_notes",
                    "type": "run",
                    "prompt": "Gather the release notes.",
                    "require_deliverable": False,
                },
                {
                    "name": "publish_release",
                    "type": "run",
                    "prompt": "Publish the release.",
                    "require_deliverable": True,
                },
            ],
            decomposition_source_hash="b" * 64,
        )
        await set_current_version(session, skill.skill_id, version.version_id)
        await session.commit()
        return skill.skill_id


def test_task_create_accepts_decomposed_skill_source(monkeypatch: object, tmp_path: Path) -> None:
    with _create_test_client(monkeypatch, tmp_path) as client:
        asyncio.run(_seed_skill_workflow_source(client.app))
        headers = _auth_headers(client.app, email="user@example.com")

        response = client.post(
            "/api/v1/tasks",
            headers=headers,
            json={
                "agent_id": "agent-1",
                "title": "Ship release",
                "description": "Use the saved release skill workflow.",
                "skill_id": "skill_release",
            },
        )

        assert response.status_code == 200
        body = response.json()
        assert body["workflow_id"] is not None

        workflow = client.get(f"/api/v1/workflows/{body['workflow_id']}", headers=headers)
        assert workflow.status_code == 200
        workflow_body = workflow.json()
        assert workflow_body["lifecycle"] == "ephemeral"
        assert workflow_body["lineage"]["source_skill_ids"] == ["skill_release"]


def test_schedule_create_accepts_decomposed_skill_source(monkeypatch: object, tmp_path: Path) -> None:
    with _create_test_client(monkeypatch, tmp_path) as client:
        asyncio.run(_seed_skill_workflow_source(client.app))
        headers = _auth_headers(client.app, email="user@example.com")

        response = client.post(
            "/api/v1/schedules",
            headers=headers,
            json={
                "name": "Release schedule",
                "schedule_type": "interval",
                "interval_seconds": 1800,
                "timezone": "UTC",
                "agent_id": "agent-1",
                "skill_id": "skill_release",
                "task_template": {"title": "Scheduled release"},
            },
        )

        assert response.status_code == 201
        body = response.json()
        assert body["workflow_id"] is not None

        workflow = client.get(f"/api/v1/workflows/{body['workflow_id']}", headers=headers)
        assert workflow.status_code == 200
        workflow_body = workflow.json()
        assert workflow_body["lifecycle"] == "persistent"
        assert workflow_body["lineage"]["source_skill_ids"] == ["skill_release"]


def test_task_create_rejects_unattached_skill_source(monkeypatch: object, tmp_path: Path) -> None:
    with _create_test_client(monkeypatch, tmp_path) as client:
        asyncio.run(_seed_skill_workflow_source(client.app))
        headers = _auth_headers(client.app, email="user@example.com")

        response = client.post(
            "/api/v1/tasks",
            headers=headers,
            json={
                "agent_id": "agent-2",
                "title": "Ship release",
                "skill_id": "skill_release",
            },
        )

        assert response.status_code == 400
        assert response.json()["error"]["message"] == "Skill is not attached to the selected agent"
