from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient

from cognis.api.app import create_app
from cognis.core.agent_loop import AgentLoop
from cognis.core.workflow_management import get_workflow_for_user
from cognis.models.tool import ToolCall
from cognis.store.queries import (
    attach_project_workflow,
    create_project,
    create_project_grant,
    create_user,
    create_workflow,
    get_workflow,
)


def _create_test_client(monkeypatch: object, tmp_path: Path) -> TestClient:
    monkeypatch.setenv("COGNIS_DATA_DIR", str(tmp_path))  # type: ignore[attr-defined]
    monkeypatch.setenv("COGNIS_HOST", "127.0.0.1")  # type: ignore[attr-defined]
    app = create_app()
    return TestClient(app)


def _auth_headers(app: object, *, email: str, role: str = "user") -> dict[str, str]:
    token = app.state.auth_provider.sign_access_token(email, email.split("@")[0].title(), role)  # type: ignore[attr-defined]
    return {"Authorization": f"Bearer {token}"}


async def _seed_user(app: object, email: str = "user@example.com", role: str = "user") -> None:
    async with app.state.session_factory() as session:  # type: ignore[attr-defined]
        await create_user(
            session,
            email=email,
            name="User",
            password_hash=app.state.password_hasher.hash("password123"),  # type: ignore[attr-defined]
            role=role,
        )
        await session.commit()


def test_workflow_list_includes_system_workflows(monkeypatch: object, tmp_path: Path) -> None:
    with _create_test_client(monkeypatch, tmp_path) as client:
        client.portal.call(_seed_user, client.app)

        response = client.get(
            "/api/v1/workflows",
            headers=_auth_headers(client.app, email="user@example.com"),
        )

        assert response.status_code == 200
        workflow_ids = {item["workflow_id"] for item in response.json()["items"]}
        assert "system:direct" in workflow_ids
        assert "system:general-task" in workflow_ids
        assert "system:research" in workflow_ids


async def _seed_bound_workflow(app: Any, permission: str | None = None) -> str:
    async with app.state.session_factory() as session:
        project = await create_project(session, owner_email="user@example.com", name="Bound")
        for workflow_id in ("wf_bound_owner", "wf_unbound_owner"):
            await create_workflow(
                session,
                workflow_id=workflow_id,
                name=workflow_id,
                owner_email="user@example.com",
                definition={
                    "workflow_id": workflow_id,
                    "name": workflow_id,
                    "owner_email": "user@example.com",
                    "is_system": False,
                    "steps": [{"name": "run", "type": "run", "prompt": "Work"}],
                },
            )
        await attach_project_workflow(session, project.project_id, "wf_bound_owner")
        if permission is not None:
            await create_project_grant(
                session,
                project_id=project.project_id,
                granted_by="user@example.com",
                grantee_user_email="other@example.com",
                permission=permission,
            )
        await session.commit()
        return str(project.project_id)


async def _native_workflow_read(
    app: Any, actor: str, name: str, project_id: str | None = None
) -> Any:
    loop = SimpleNamespace(
        session_manager=SimpleNamespace(session_factory=app.state.session_factory)
    )
    ctx = SimpleNamespace(
        session=SimpleNamespace(user_email=actor, project_id=project_id),
        agent=SimpleNamespace(manage_agents=True),
    )
    return await AgentLoop._handle_workflow_tool(
        loop,
        ToolCall(
            call_id="workflow-read",
            name=name,
            arguments={"workflow_id": "wf_bound_owner"} if name == "get_workflow" else {},
        ),
        ctx=ctx,
    )


def test_owner_administrative_reads_do_not_grant_execution(
    monkeypatch: object, tmp_path: Path
) -> None:
    with _create_test_client(monkeypatch, tmp_path) as client:
        client.portal.call(_seed_user, client.app)
        project_id = client.portal.call(_seed_bound_workflow, client.app)
        headers = _auth_headers(client.app, email="user@example.com")
        assert client.get("/api/v1/workflows/wf_bound_owner", headers=headers).status_code == 200
        listed = client.get("/api/v1/workflows", headers=headers).json()["items"]
        assert sum(item["workflow_id"] == "wf_bound_owner" for item in listed) == 1
        assert (
            client.put(
                "/api/v1/workflows/wf_bound_owner", headers=headers, json={"description": "Updated"}
            ).status_code
            == 200
        )

        async def check_native_and_execution() -> None:
            for context_project in (None, project_id):
                fetched = await _native_workflow_read(
                    client.app, "user@example.com", "get_workflow", context_project
                )
                assert not fetched.is_error
                assert json.loads(fetched.output)["description"] == "Updated"
                listed = await _native_workflow_read(
                    client.app, "user@example.com", "list_workflows", context_project
                )
                assert "wf_bound_owner" in {
                    item["workflow_id"] for item in json.loads(listed.output)["workflows"]
                }
            registry = client.app.state.workflow_registry
            assert await registry.get("wf_bound_owner", owner_email="user@example.com") is None
            assert (
                await get_workflow_for_user(
                    workflow_registry=registry,
                    workflow_id="wf_bound_owner",
                    owner_email="user@example.com",
                )
                is None
            )
            assert (
                await registry.get(
                    "wf_bound_owner", owner_email="user@example.com", project_id=project_id
                )
                is not None
            )
            assert "wf_bound_owner" not in {
                item.workflow_id for item in await registry.list_all(owner_email="user@example.com")
            }
            assert "wf_bound_owner" in {
                item.workflow_id
                for item in await registry.list_all(
                    owner_email="user@example.com", project_id=project_id
                )
            }

        client.portal.call(check_native_and_execution)


@pytest.mark.parametrize("permission", [None, "viewer", "use", "manager"])
@pytest.mark.parametrize("role", ["user", "admin"])
def test_non_owner_management_reads_preserve_authorization(
    monkeypatch: object, tmp_path: Path, permission: str | None, role: str
) -> None:
    with _create_test_client(monkeypatch, tmp_path) as client:
        client.portal.call(_seed_user, client.app)
        client.portal.call(lambda: _seed_user(client.app, email="other@example.com", role=role))
        project_id = client.portal.call(_seed_bound_workflow, client.app, permission)
        headers = _auth_headers(client.app, email="other@example.com", role=role)
        assert client.get("/api/v1/workflows/wf_bound_owner", headers=headers).status_code == 404
        # Existing unbound admin exception is preserved, not extended to bound definitions.
        assert client.get("/api/v1/workflows/wf_unbound_owner", headers=headers).status_code == (
            200 if role == "admin" else 403
        )
        listed = client.get("/api/v1/workflows", headers=headers).json()["items"]
        assert "wf_bound_owner" not in {item["workflow_id"] for item in listed}
        assert (
            client.put(
                "/api/v1/workflows/wf_bound_owner", headers=headers, json={"description": "Denied"}
            ).status_code
            == 403
        )

        async def check_native() -> None:
            for context_project in (None, project_id):
                fetched = await _native_workflow_read(
                    client.app, "other@example.com", "get_workflow", context_project
                )
                assert fetched.is_error
                listed = await _native_workflow_read(
                    client.app, "other@example.com", "list_workflows", context_project
                )
                assert "wf_bound_owner" not in {
                    item["workflow_id"] for item in json.loads(listed.output)["workflows"]
                }

        client.portal.call(check_native)
        scoped = client.get("/api/v1/workflows", headers=headers, params={"project_id": project_id})
        if permission in {"use", "manager"}:
            assert scoped.status_code == 200
            assert "wf_bound_owner" not in {item["workflow_id"] for item in scoped.json()["items"]}
        else:
            assert scoped.status_code == 403


def test_definition_owner_cannot_override_db_owner(monkeypatch: object, tmp_path: Path) -> None:
    with _create_test_client(monkeypatch, tmp_path) as client:
        client.portal.call(_seed_user, client.app)
        client.portal.call(lambda: _seed_user(client.app, email="other@example.com"))
        client.portal.call(_seed_bound_workflow, client.app)

        async def forge_definition_and_check_native() -> None:
            async with client.app.state.session_factory() as session:
                row = await get_workflow(session, "wf_bound_owner")
                assert row is not None
                row.definition = {**row.definition, "owner_email": "other@example.com"}
                await session.commit()
            fetched = await _native_workflow_read(client.app, "other@example.com", "get_workflow")
            assert fetched.is_error
            listed = await _native_workflow_read(client.app, "other@example.com", "list_workflows")
            assert "wf_bound_owner" not in {
                item["workflow_id"] for item in json.loads(listed.output)["workflows"]
            }

        client.portal.call(forge_definition_and_check_native)
        headers = _auth_headers(client.app, email="other@example.com")
        assert client.get("/api/v1/workflows/wf_bound_owner", headers=headers).status_code == 404
        assert "wf_bound_owner" not in {
            item["workflow_id"]
            for item in client.get("/api/v1/workflows", headers=headers).json()["items"]
        }


def test_agent_list_includes_system_agents(monkeypatch: object, tmp_path: Path) -> None:
    with _create_test_client(monkeypatch, tmp_path) as client:
        client.portal.call(_seed_user, client.app)

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
        client.portal.call(_seed_user, client.app)

        response = client.get(
            "/api/v1/workflows/system:software-development",
            headers=_auth_headers(client.app, email="user@example.com"),
        )

        assert response.status_code == 200
        body = response.json()
        assert body["workflow_id"] == "system:software-development"
        assert body["is_system"] is True
        assert body["owner_email"] is None


def test_workflow_presentation_create_round_trip_and_explicit_removal(
    monkeypatch: object, tmp_path: Path
) -> None:
    with _create_test_client(monkeypatch, tmp_path) as client:
        client.portal.call(_seed_user, client.app)
        headers = _auth_headers(client.app, email="user@example.com")
        payload = {
            "name": "Phased",
            "steps": [
                {
                    "name": "collect",
                    "type": "run",
                    "objective": "Collect the required evidence.",
                    "responsibilities": ["Read the sources", "Record provenance"],
                    "defer_to": ["finish"],
                },
                {"name": "finish", "type": "run"},
            ],
            "presentation": {
                "phases": [
                    {
                        "id": "all",
                        "title": "All",
                        "step_names": ["collect", "finish"],
                    }
                ]
            },
        }

        created = client.post("/api/v1/workflows", headers=headers, json=payload)

        assert created.status_code == 200
        workflow_id = created.json()["workflow_id"]
        assert created.json()["presentation"]["phases"][0]["id"] == "all"
        fetched = client.get(f"/api/v1/workflows/{workflow_id}", headers=headers)
        assert fetched.status_code == 200
        assert fetched.json()["presentation"]["phases"][0]["step_names"] == [
            "collect",
            "finish",
        ]
        assert fetched.json()["steps"][0]["objective"] == "Collect the required evidence."
        assert fetched.json()["steps"][0]["responsibilities"] == [
            "Read the sources",
            "Record provenance",
        ]
        assert fetched.json()["steps"][0]["defer_to"] == ["finish"]

        removed = client.put(
            f"/api/v1/workflows/{workflow_id}",
            headers=headers,
            json={"presentation": None},
        )

        assert removed.status_code == 200
        assert "presentation" not in removed.json()


def test_workflow_duplicate_supports_system_workflow(monkeypatch: object, tmp_path: Path) -> None:
    with _create_test_client(monkeypatch, tmp_path) as client:
        client.portal.call(_seed_user, client.app)

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
        assert [step["name"] for step in body["steps"]] == [
            "plan",
            "pre_research_gate",
            "research",
            "synthesize",
        ]


def test_workflow_create_accepts_null_workflow_id(monkeypatch: object, tmp_path: Path) -> None:
    with _create_test_client(monkeypatch, tmp_path) as client:
        client.portal.call(_seed_user, client.app)

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
        client.portal.call(lambda: _seed_user(client.app, email="owner@example.com"))
        client.portal.call(lambda: _seed_user(client.app, email="admin@example.com", role="admin"))

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

        client.portal.call(_seed_workflow)

        response = client.post(
            "/api/v1/workflows/wf_owner_private/duplicate",
            headers=_auth_headers(client.app, email="admin@example.com", role="admin"),
        )

        assert response.status_code == 200
        body = response.json()
    assert body["name"] == "Owner Workflow Copy"
    assert body["owner_email"] == "admin@example.com"


def test_project_workflow_binding_rejects_other_user_workflow(
    monkeypatch: object, tmp_path: Path
) -> None:
    with _create_test_client(monkeypatch, tmp_path) as client:
        client.portal.call(lambda: _seed_user(client.app, email="owner@example.com"))
        client.portal.call(lambda: _seed_user(client.app, email="other@example.com"))

        async def _seed_workflow() -> None:
            async with client.app.state.session_factory() as session:  # type: ignore[attr-defined]
                await create_workflow(
                    session,
                    workflow_id="wf_other_private",
                    name="Other Workflow",
                    definition={
                        "workflow_id": "wf_other_private",
                        "name": "Other Workflow",
                        "steps": [{"name": "plan", "type": "run"}],
                        "is_system": False,
                        "owner_email": "other@example.com",
                    },
                    is_system=False,
                    owner_email="other@example.com",
                )
                await session.commit()

        client.portal.call(_seed_workflow)
        headers = _auth_headers(client.app, email="owner@example.com")
        project = client.post("/api/v1/projects", headers=headers, json={"name": "Project"})
        assert project.status_code == 201
        project_id = project.json()["project_id"]

        response = client.post(
            f"/api/v1/projects/{project_id}/workflows/wf_other_private",
            headers=headers,
        )

        assert response.status_code == 404


def test_project_create_rejects_other_user_default_workflow(
    monkeypatch: object, tmp_path: Path
) -> None:
    with _create_test_client(monkeypatch, tmp_path) as client:
        client.portal.call(lambda: _seed_user(client.app, email="owner@example.com"))
        client.portal.call(lambda: _seed_user(client.app, email="other@example.com"))

        async def _seed_workflow() -> None:
            async with client.app.state.session_factory() as session:  # type: ignore[attr-defined]
                await create_workflow(
                    session,
                    workflow_id="wf_other_default",
                    name="Other Default",
                    definition={
                        "workflow_id": "wf_other_default",
                        "name": "Other Default",
                        "steps": [{"name": "plan", "type": "run"}],
                        "is_system": False,
                        "owner_email": "other@example.com",
                    },
                    is_system=False,
                    owner_email="other@example.com",
                )
                await session.commit()

        client.portal.call(_seed_workflow)
        response = client.post(
            "/api/v1/projects",
            headers=_auth_headers(client.app, email="owner@example.com"),
            json={"name": "Project", "default_workflow_id": "wf_other_default"},
        )

        assert response.status_code == 404
