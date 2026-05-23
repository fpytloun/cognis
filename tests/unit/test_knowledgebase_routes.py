from __future__ import annotations

import anyio
from fastapi import FastAPI, Request, Response
from fastapi.testclient import TestClient

from cognis.api.middleware import AuthenticatedUser
from cognis.api.routes.knowledgebases import router
from cognis.knowledgebase.service import KnowledgebaseService
from cognis.models.agent import AgentPermissions
from cognis.store.database import create_engine, create_session_factory
from cognis.store.models import (
    Agent,
    ArtifactRecordRow,
    Base,
    KnowledgebaseArtifactRow,
    KnowledgebaseRow,
    User,
)
from cognis.store.queries import (
    assign_knowledgebase_to_agent,
    create_agent_grant,
)


class _VectorBackend:
    async def health(self) -> dict[str, object]:
        return {"ok": True}


def _client(tmp_path: object) -> TestClient:
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path}/kb-routes.db")
    factory = create_session_factory(engine)
    app = FastAPI()
    app.include_router(router)
    app.state.session_factory = factory
    app.state.knowledgebase_service = KnowledgebaseService(
        session_factory=factory,
        artifact_store=None,  # not used by these route tests
        llm=None,  # not used by these route tests
        vector_backend=_VectorBackend(),
        enabled=True,
    )

    @app.middleware("http")
    async def _fake_auth(request: Request, call_next) -> Response:  # type: ignore[no-untyped-def]
        email = request.headers.get("x-user", "owner@example.com")
        request.state.user = AuthenticatedUser(email=email, role="user", name=email)
        agent_id = request.headers.get("x-agent-id")
        agent_owner = request.headers.get("x-agent-owner")
        if agent_id:
            request.state.runtime_access = {
                "agent_id": agent_id,
                "agent_owner_email": agent_owner,
            }
        return await call_next(request)

    async def _init() -> None:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        async with factory() as session:
            session.add_all(
                [
                    User(email="owner@example.com", name="Owner", role="user"),
                    User(email="grantee@example.com", name="Grantee", role="user"),
                    User(email="other@example.com", name="Other", role="user"),
                ]
            )
            await session.flush()
            session.add_all(
                [
                    KnowledgebaseRow(
                        knowledgebase_id="kb_owner",
                        owner_email="owner@example.com",
                        name="Owner KB",
                    ),
                    ArtifactRecordRow(
                        artifact_id="artifact_1",
                        namespace="test",
                        object_id="artifact_1",
                        filename="source.txt",
                        owner_email="owner@example.com",
                        mime_type="text/plain",
                    ),
                ]
            )
            await session.flush()
            session.add_all(
                [
                    KnowledgebaseArtifactRow(
                        kb_artifact_id="kba_1",
                        knowledgebase_id="kb_owner",
                        artifact_id="artifact_1",
                        status="indexed",
                    ),
                    Agent(
                        agent_id="agent_owner",
                        owner_email="owner@example.com",
                        name="Owner Agent",
                        status="active",
                        permissions=AgentPermissions().model_dump(mode="json"),
                    ),
                ]
            )
            await session.commit()
            await assign_knowledgebase_to_agent(
                session,
                owner_email="owner@example.com",
                knowledgebase_id="kb_owner",
                agent_id="agent_owner",
            )
            await create_agent_grant(
                session,
                agent_id="agent_owner",
                grantee_user_email="grantee@example.com",
                executor_scope="shared_pool",
                granted_by="owner@example.com",
            )
            await session.commit()

    anyio.run(_init)
    return TestClient(app)


def test_owner_can_assign_and_list_agent_assignments(tmp_path: object) -> None:
    client = _client(tmp_path)

    response = client.get("/api/v1/knowledgebases/kb_owner/agents")
    assert response.status_code == 200
    assert response.json() == ["agent_owner"]

    response = client.post("/api/v1/knowledgebases/kb_owner/agents/agent_owner")
    assert response.status_code == 200
    assert response.json() == {"assigned": True}


def test_create_from_active_owner_agent_assigns_new_kb(tmp_path: object) -> None:
    client = _client(tmp_path)

    response = client.post(
        "/api/v1/knowledgebases/",
        json={"name": "Agent KB"},
        headers={"x-agent-id": "agent_owner", "x-agent-owner": "owner@example.com"},
    )

    assert response.status_code == 200
    knowledgebase_id = response.json()["knowledgebase_id"]
    assignments = client.get(f"/api/v1/knowledgebases/{knowledgebase_id}/agents")
    assert assignments.status_code == 200
    assert assignments.json() == ["agent_owner"]


def test_shared_grantee_can_read_available_kb_artifacts_jobs_and_status(
    tmp_path: object,
) -> None:
    client = _client(tmp_path)
    headers = {
        "x-user": "grantee@example.com",
        "x-agent-id": "agent_owner",
        "x-agent-owner": "owner@example.com",
    }

    assert client.get("/api/v1/knowledgebases/kb_owner", headers=headers).status_code == 200
    artifacts = client.get("/api/v1/knowledgebases/kb_owner/artifacts", headers=headers)
    assert artifacts.status_code == 200
    assert artifacts.json()[0]["artifact_id"] == "artifact_1"
    assert client.get("/api/v1/knowledgebases/kb_owner/jobs", headers=headers).status_code == 200
    assert client.get("/api/v1/knowledgebases/kb_owner/status", headers=headers).status_code == 200


def test_owner_can_bulk_attach_with_per_document_metadata(tmp_path: object) -> None:
    client = _client(tmp_path)

    response = client.post(
        "/api/v1/knowledgebases/kb_owner/artifacts/bulk",
        json={
            "items": [
                {
                    "artifact_id": "artifact_1",
                    "metadata": {
                        "lesson_no": 62,
                        "category": "mistnosti-domova",
                        "tags": ["ložnice", "kuchyň"],
                    },
                }
            ]
        },
    )

    assert response.status_code == 200
    assert response.json()[0]["metadata"] == {
        "lesson_no": 62,
        "category": "mistnosti-domova",
        "tags": ["ložnice", "kuchyň"],
    }


def test_owner_can_update_knowledgebase(tmp_path: object) -> None:
    client = _client(tmp_path)

    response = client.patch(
        "/api/v1/knowledgebases/kb_owner",
        json={"name": "Updated KB", "status": "archived"},
    )

    assert response.status_code == 200
    assert response.json()["name"] == "Updated KB"
    assert response.json()["status"] == "archived"


def test_owner_can_update_chunking_settings(tmp_path: object) -> None:
    client = _client(tmp_path)

    response = client.patch(
        "/api/v1/knowledgebases/kb_owner",
        json={
            "settings": {
                "chunking": {
                    "target_tokens": 512,
                    "overlap_tokens": 64,
                    "max_chunks_per_artifact": 250,
                }
            }
        },
    )

    assert response.status_code == 200
    assert response.json()["settings"]["chunking"] == {
        "target_tokens": 512,
        "overlap_tokens": 64,
        "max_chunks_per_artifact": 250,
    }


def test_owner_cannot_set_invalid_chunking_settings(tmp_path: object) -> None:
    client = _client(tmp_path)

    response = client.patch(
        "/api/v1/knowledgebases/kb_owner",
        json={"settings": {"chunking": {"target_tokens": 256, "overlap_tokens": 256}}},
    )

    assert response.status_code == 400
    assert "invalid_knowledgebase_settings" in str(response.json())


def test_owner_can_delete_knowledgebase(tmp_path: object) -> None:
    client = _client(tmp_path)

    response = client.delete("/api/v1/knowledgebases/kb_owner")

    assert response.status_code == 200
    assert response.json() == {"deleted": True}
    assert client.get("/api/v1/knowledgebases/kb_owner").status_code == 404


def test_shared_grantee_cannot_manage_owner_kb(tmp_path: object) -> None:
    client = _client(tmp_path)

    response = client.post(
        "/api/v1/knowledgebases/kb_owner/agents/agent_owner",
        headers={"x-user": "grantee@example.com"},
    )

    assert response.status_code == 404


def test_unrelated_user_cannot_read_or_assign_owner_kb(tmp_path: object) -> None:
    client = _client(tmp_path)

    assert (
        client.get(
            "/api/v1/knowledgebases/kb_owner",
            headers={"x-user": "other@example.com"},
        ).status_code
        == 404
    )
    assert (
        client.post(
            "/api/v1/knowledgebases/kb_owner/agents/agent_owner",
            headers={"x-user": "other@example.com"},
        ).status_code
        == 404
    )
