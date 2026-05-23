from __future__ import annotations

import asyncio
from pathlib import Path

from fastapi.testclient import TestClient

from cognis.api.app import create_app
from cognis.core.workflow_composition import SkillDecompositionResult
from cognis.store.queries import create_agent, create_user, get_agent


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


def test_skill_create_with_agent_id_binds_skill_to_agent(
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
                await create_agent(
                    session,
                    agent_id="agent-1",
                    owner_email="user@example.com",
                    name="Agent 1",
                    status="active",
                )
                await session.commit()

        asyncio.run(_seed())
        headers = _auth_headers(client.app, email="user@example.com")

        response = client.post(
            "/api/v1/skills",
            headers=headers,
            json={
                "name": "Agent Bound Skill",
                "instructions": "hello",
                "agent_id": "agent-1",
            },
        )

        assert response.status_code == 201
        skill_id = response.json()["skill_id"]

        async def _load_items() -> list[dict[str, object]]:
            async with client.app.state.session_factory() as session:
                agent = await get_agent(session, "agent-1")
                assert agent is not None
                return list(agent.skills["items"])

        assert asyncio.run(_load_items()) == [{"skill_id": skill_id, "enabled": True}]


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


def test_skill_update_can_attach_artifact_backed_asset(monkeypatch: object, tmp_path: Path) -> None:
    with _create_test_client(monkeypatch, tmp_path) as client:
        asyncio.run(_seed_user(client.app))
        headers = _auth_headers(client.app, email="user@example.com")

        created = client.post(
            "/api/v1/skills",
            headers=headers,
            json={"name": "Asset Skill", "instructions": "hello"},
        )
        assert created.status_code == 201
        skill_id = created.json()["skill_id"]

        uploaded = client.post(
            "/api/v1/artifacts/upload",
            headers=headers,
            data={"purpose": "skill_asset"},
            files={"file": ("tool.py", b"print('hi')\n", "text/x-python")},
        )
        assert uploaded.status_code == 200
        artifact_id = uploaded.json()["artifact_id"]

        updated = client.put(
            f"/api/v1/skills/{skill_id}",
            headers=headers,
            json={
                "assets": [
                    {
                        "filename": "scripts/tool.py",
                        "source_artifact_id": artifact_id,
                        "content_type": "text/x-python",
                    }
                ]
            },
        )
        assert updated.status_code == 200
        body = updated.json()
        assert body["current_version"]["asset_manifest"][0]["filename"] == "scripts/tool.py"
        assert body["current_version"]["asset_manifest"][0]["artifact_namespace"] == "skills"
        assert body["current_version"]["asset_manifest"][0]["url"] is not None


def test_skill_version_can_be_restored(monkeypatch: object, tmp_path: Path) -> None:
    with _create_test_client(monkeypatch, tmp_path) as client:
        asyncio.run(_seed_user(client.app))
        headers = _auth_headers(client.app, email="user@example.com")

        created = client.post(
            "/api/v1/skills",
            headers=headers,
            json={"name": "Versioned", "instructions": "one"},
        )
        assert created.status_code == 201
        skill_id = created.json()["skill_id"]
        first_version_id = created.json()["current_version_id"]

        updated = client.put(
            f"/api/v1/skills/{skill_id}",
            headers=headers,
            json={"instructions": "two"},
        )
        assert updated.status_code == 200
        assert updated.json()["current_version_id"] != first_version_id

        restored = client.post(
            f"/api/v1/skills/{skill_id}/versions/{first_version_id}/restore",
            headers=headers,
        )
        assert restored.status_code == 200
        assert restored.json()["current_version_id"] == first_version_id
        assert restored.json()["instructions"] == "one"


def test_skill_decompose_preview_returns_gateway_timeout_on_timeout(
    monkeypatch: object, tmp_path: Path
) -> None:
    async def _timeout(*args: object, **kwargs: object) -> SkillDecompositionResult:
        raise TimeoutError()

    monkeypatch.setattr(
        "cognis.core.workflow_composition.decompose_skill_material",
        _timeout,
    )

    with _create_test_client(monkeypatch, tmp_path) as client:
        asyncio.run(_seed_user(client.app))
        headers = _auth_headers(client.app, email="user@example.com")

        created = client.post(
            "/api/v1/skills",
            headers=headers,
            json={"name": "Timeout Skill", "instructions": "hello"},
        )
        assert created.status_code == 201

        response = client.post(
            f"/api/v1/skills/{created.json()['skill_id']}/decompose-preview",
            headers=headers,
        )

        assert response.status_code == 504
        assert response.json()["error"]["code"] == "timeout"


def test_skill_decompose_preview_returns_provider_error_on_invalid_output(
    monkeypatch: object, tmp_path: Path
) -> None:
    async def _invalid_output(*args: object, **kwargs: object) -> SkillDecompositionResult:
        raise ValueError("Empty content")

    monkeypatch.setattr(
        "cognis.core.workflow_composition.decompose_skill_material",
        _invalid_output,
    )

    with _create_test_client(monkeypatch, tmp_path) as client:
        asyncio.run(_seed_user(client.app))
        headers = _auth_headers(client.app, email="user@example.com")

        created = client.post(
            "/api/v1/skills",
            headers=headers,
            json={"name": "Invalid Skill", "instructions": "hello"},
        )
        assert created.status_code == 201

        response = client.post(
            f"/api/v1/skills/{created.json()['skill_id']}/decompose-preview",
            headers=headers,
        )

        assert response.status_code == 502
    assert response.json()["error"]["code"] == "provider_error"
    assert "invalid output" in response.json()["error"]["message"]


def test_skill_update_refreshes_saved_decomposition_when_inputs_change(
    monkeypatch: object, tmp_path: Path
) -> None:
    async def _refresh(*args: object, **kwargs: object) -> SkillDecompositionResult:
        return SkillDecompositionResult(
            rationale="Refresh only the affected synthesis step.",
            steps=[
                {
                    "name": "collect",
                    "type": "run",
                    "prompt": "Collect data.",
                    "require_deliverable": False,
                },
                {
                    "name": "synthesize",
                    "type": "run",
                    "prompt": "Write the refreshed brief.",
                    "input": {"type": "last", "source": "all"},
                    "require_deliverable": True,
                },
            ],
        )

    monkeypatch.setattr("cognis.core.workflow_composition.decompose_skill_material", _refresh)

    with _create_test_client(monkeypatch, tmp_path) as client:
        asyncio.run(_seed_user(client.app))
        headers = _auth_headers(client.app, email="user@example.com")

        created = client.post(
            "/api/v1/skills",
            headers=headers,
            json={
                "name": "Briefing",
                "instructions": "Write a briefing.",
                "steps": [
                    {
                        "name": "collect",
                        "type": "run",
                        "prompt": "Collect data.",
                        "require_deliverable": False,
                    },
                    {
                        "name": "synthesize",
                        "type": "run",
                        "prompt": "Write the brief.",
                        "require_deliverable": True,
                    },
                ],
            },
        )
        assert created.status_code == 201

        updated = client.put(
            f"/api/v1/skills/{created.json()['skill_id']}",
            headers=headers,
            json={"instructions": "Write a refreshed multi-section briefing."},
        )

        assert updated.status_code == 200
        assert updated.json()["current_version"]["steps"][1]["input"] == {
            "type": "last",
            "source": "all",
        }


def test_skill_update_fails_when_decomposition_refresh_fails(
    monkeypatch: object, tmp_path: Path
) -> None:
    async def _refresh(*args: object, **kwargs: object) -> SkillDecompositionResult:
        raise TimeoutError()

    monkeypatch.setattr("cognis.core.workflow_composition.decompose_skill_material", _refresh)

    with _create_test_client(monkeypatch, tmp_path) as client:
        asyncio.run(_seed_user(client.app))
        headers = _auth_headers(client.app, email="user@example.com")

        created = client.post(
            "/api/v1/skills",
            headers=headers,
            json={
                "name": "Briefing",
                "instructions": "Write a briefing.",
                "steps": [
                    {
                        "name": "collect",
                        "type": "run",
                        "prompt": "Collect data.",
                        "require_deliverable": False,
                    }
                ],
            },
        )
        assert created.status_code == 201
        current_version_id = created.json()["current_version_id"]

        updated = client.put(
            f"/api/v1/skills/{created.json()['skill_id']}",
            headers=headers,
            json={"instructions": "Write a refreshed multi-section briefing."},
        )

        assert updated.status_code == 504

        reloaded = client.get(f"/api/v1/skills/{created.json()['skill_id']}", headers=headers)
        assert reloaded.status_code == 200
        assert reloaded.json()["current_version_id"] == current_version_id


def test_skill_linked_tool_ids_are_versioned_and_restorable(
    monkeypatch: object, tmp_path: Path
) -> None:
    with _create_test_client(monkeypatch, tmp_path) as client:
        asyncio.run(_seed_user(client.app))
        headers = _auth_headers(client.app, email="user@example.com")

        created = client.post(
            "/api/v1/skills",
            headers=headers,
            json={
                "name": "Linked Skill",
                "instructions": "Use shell helpers.",
                "linked_tool_ids": ["builtin:bash"],
            },
        )
        assert created.status_code == 201
        skill_id = created.json()["skill_id"]
        first_version_id = created.json()["current_version_id"]
        assert created.json()["current_version"]["linked_tool_ids"] == ["builtin:bash"]
        assert created.json()["current_version"]["decomposition_stale"] is False

        updated = client.put(
            f"/api/v1/skills/{skill_id}",
            headers=headers,
            json={"linked_tool_ids": ["builtin:read"]},
        )
        assert updated.status_code == 200
        assert updated.json()["current_version_id"] != first_version_id
        assert updated.json()["current_version"]["linked_tool_ids"] == ["builtin:read"]
        assert updated.json()["current_version"]["decomposition_stale"] is False

        restored = client.post(
            f"/api/v1/skills/{skill_id}/versions/{first_version_id}/restore",
            headers=headers,
        )
        assert restored.status_code == 200
        assert restored.json()["current_version_id"] == first_version_id
        assert restored.json()["linked_tool_ids"] == ["builtin:bash"]
        assert restored.json()["current_version"]["linked_tool_ids"] == ["builtin:bash"]


def test_skill_update_with_identical_content_does_not_create_new_version(
    monkeypatch: object, tmp_path: Path
) -> None:
    with _create_test_client(monkeypatch, tmp_path) as client:
        asyncio.run(_seed_user(client.app))
        headers = _auth_headers(client.app, email="user@example.com")

        created = client.post(
            "/api/v1/skills",
            headers=headers,
            json={"name": "Stable", "instructions": "same"},
        )
        assert created.status_code == 201
        current_version_id = created.json()["current_version_id"]

        updated = client.put(
            f"/api/v1/skills/{created.json()['skill_id']}",
            headers=headers,
            json={"instructions": "same"},
        )
        assert updated.status_code == 200
        assert updated.json()["current_version_id"] == current_version_id


def test_skill_metadata_edit_with_existing_assets_does_not_create_new_version(
    monkeypatch: object, tmp_path: Path
) -> None:
    with _create_test_client(monkeypatch, tmp_path) as client:
        asyncio.run(_seed_user(client.app))
        headers = _auth_headers(client.app, email="user@example.com")

        created = client.post(
            "/api/v1/skills",
            headers=headers,
            json={"name": "Asset Stable", "instructions": "hello"},
        )
        assert created.status_code == 201
        skill_id = created.json()["skill_id"]

        uploaded = client.post(
            "/api/v1/artifacts/upload",
            headers=headers,
            data={"purpose": "skill_asset"},
            files={"file": ("tool.py", b"print('hi')\n", "text/x-python")},
        )
        assert uploaded.status_code == 200

        updated = client.put(
            f"/api/v1/skills/{skill_id}",
            headers=headers,
            json={
                "assets": [
                    {
                        "filename": "scripts/tool.py",
                        "source_artifact_id": uploaded.json()["artifact_id"],
                        "content_type": "text/x-python",
                    }
                ]
            },
        )
        assert updated.status_code == 200
        current_version_id = updated.json()["current_version_id"]
        asset_manifest = updated.json()["current_version"]["asset_manifest"]

        metadata_only = client.put(
            f"/api/v1/skills/{skill_id}",
            headers=headers,
            json={
                "name": "Asset Stable Renamed",
                "assets": [
                    {
                        "filename": asset_manifest[0]["filename"],
                        "existing_asset_id": asset_manifest[0]["asset_id"],
                        "content_type": asset_manifest[0]["content_type"],
                    }
                ],
            },
        )
        assert metadata_only.status_code == 200
        assert metadata_only.json()["current_version_id"] == current_version_id
