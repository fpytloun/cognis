from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor

import anyio
import pytest
import sqlalchemy as sa
from fastapi import FastAPI, Request, Response
from fastapi.testclient import TestClient

from cognis.api.middleware import AuthenticatedUser
from cognis.api.routes.knowledgebases import router
from cognis.artifacts.store import ArtifactStore, ArtifactStoreConfig
from cognis.knowledgebase.service import KnowledgebaseService
from cognis.models.agent import AgentPermissions
from cognis.store.database import create_engine, create_session_factory
from cognis.store.models import (
    Agent,
    ArtifactRecordRow,
    Base,
    KnowledgebaseArtifactRow,
    KnowledgebaseIndexJobRow,
    KnowledgebaseRow,
    ModelRouting,
    User,
)
from cognis.store.queries import (
    assign_knowledgebase_to_agent,
    create_agent_grant,
    upsert_model_routing,
)


class _VectorBackend:
    async def health(self) -> dict[str, object]:
        return {"ok": True}


class _LLM:
    async def embed(self, texts: list[str], **kwargs: object) -> list[list[float]]:
        return [[1.0, 0.5] for _text in texts]


def _client(tmp_path: object) -> TestClient:
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path}/kb-routes.db")
    factory = create_session_factory(engine)
    app = FastAPI()
    app.include_router(router)
    artifact_store = ArtifactStore(
        ArtifactStoreConfig(
            path=f"{tmp_path}/artifacts",
            max_size_bytes=50 * 1024 * 1024,
        )
    )
    app.state.session_factory = factory
    app.state.artifact_store = artifact_store
    app.state.knowledgebase_service = KnowledgebaseService(
        session_factory=factory,
        artifact_store=artifact_store,
        llm=_LLM(),
        vector_backend=_VectorBackend(),
        enabled=True,
    )

    @app.middleware("http")
    async def _fake_auth(request: Request, call_next) -> Response:  # type: ignore[no-untyped-def]
        email = request.headers.get("x-user", "owner@example.com")
        request.state.user = AuthenticatedUser(
            email=email,
            role=request.headers.get("x-role", "user"),
            name=email,
        )
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
            await artifact_store.async_save(
                "test",
                "artifact_1",
                "source.txt",
                b"Existing source text",
                "text/plain",
                owner_email="owner@example.com",
            )
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
            await upsert_model_routing(
                session, task_type="embedding", provider_id=None, model="embed"
            )
            await upsert_model_routing(
                session, task_type="default", provider_id=None, model="answer"
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


def test_shared_grantee_cannot_enumerate_documents_jobs_or_diagnostics(
    tmp_path: object,
) -> None:
    client = _client(tmp_path)
    headers = {
        "x-user": "grantee@example.com",
        "x-agent-id": "agent_owner",
        "x-agent-owner": "owner@example.com",
    }

    assert client.get("/api/v1/knowledgebases/kb_owner", headers=headers).status_code == 200
    assert (
        client.get("/api/v1/knowledgebases/kb_owner/artifacts", headers=headers).status_code == 404
    )
    assert client.get("/api/v1/knowledgebases/kb_owner/jobs", headers=headers).status_code == 404
    assert client.get("/api/v1/knowledgebases/kb_owner/status", headers=headers).status_code == 404


def test_deleted_kb_exposes_only_redacted_cleanup_jobs_to_direct_owner(
    tmp_path: object,
) -> None:
    client = _client(tmp_path)

    async def _delete_and_seed_jobs() -> None:
        async with client.app.state.session_factory() as session:
            kb = await session.get(KnowledgebaseRow, "kb_owner")
            assert kb is not None
            kb.status = "deleted"
            session.add_all(
                [
                    KnowledgebaseIndexJobRow(
                        job_id="cleanup_failed",
                        knowledgebase_id="kb_owner",
                        kb_artifact_id="kba_1",
                        artifact_id="artifact_1",
                        generation=2,
                        job_type="delete_stale_vectors",
                        status="failed",
                        diagnostics={
                            "point_ids": ["vector-1", "vector-2"],
                            "backend": "qdrant",
                        },
                    ),
                    KnowledgebaseIndexJobRow(
                        job_id="index_failed",
                        knowledgebase_id="kb_owner",
                        kb_artifact_id="kba_1",
                        artifact_id="artifact_1",
                        generation=1,
                        job_type="reindex_artifact",
                        status="failed",
                        diagnostics={"internal": "hidden after delete"},
                    ),
                ]
            )
            await session.commit()

    anyio.run(_delete_and_seed_jobs)

    response = client.get("/api/v1/knowledgebases/kb_owner/jobs")
    assert response.status_code == 200
    assert [job["job_id"] for job in response.json()] == ["cleanup_failed"]
    assert response.json()[0]["diagnostics"] == {
        "backend": "qdrant",
        "point_count": 2,
    }
    assert "point_ids" not in response.text

    assert (
        client.get(
            "/api/v1/knowledgebases/kb_owner/jobs",
            headers={"x-user": "other@example.com"},
        ).status_code
        == 404
    )
    assert (
        client.get(
            "/api/v1/knowledgebases/kb_owner/jobs",
            headers={
                "x-user": "grantee@example.com",
                "x-agent-id": "agent_owner",
                "x-agent-owner": "owner@example.com",
            },
        ).status_code
        == 404
    )


def test_owner_can_bulk_attach_with_per_document_metadata(tmp_path: object) -> None:
    client = _client(tmp_path)

    response = client.post(
        "/api/v1/knowledgebases/kb_owner/artifacts/bulk",
        json={
            "items": [
                {
                    "artifact_id": "artifact_1",
                    "metadata": {
                        "category": "reference",
                        "tags": ["bedroom", "kitchen"],
                    },
                }
            ]
        },
    )

    assert response.status_code == 200
    assert response.json()[0]["metadata"] == {
        "category": "reference",
        "tags": ["bedroom", "kitchen"],
    }


def test_capabilities_is_always_available_and_reports_readiness(tmp_path: object) -> None:
    client = _client(tmp_path)

    response = client.get("/api/v1/knowledgebases/capabilities")

    assert response.status_code == 200
    payload = response.json()
    assert payload["enabled"] is True
    assert payload["backend_ready"] is True
    assert payload["embedding_ready"] is True
    assert payload["ask_ready"] is True
    assert ".pdf" in payload["supported_extensions"]
    assert payload["limits"]["max_batch_files"] == 25

    async def _routes() -> None:
        async with client.app.state.session_factory() as session:
            await upsert_model_routing(
                session, task_type="embedding", provider_id=None, model="embed"
            )
            await upsert_model_routing(
                session, task_type="default", provider_id=None, model="answer"
            )
            await session.commit()

    anyio.run(_routes)
    ready = client.get("/api/v1/knowledgebases/capabilities").json()
    assert ready["embedding_ready"] is True
    assert ready["indexer_ready"] is True
    assert ready["ask_ready"] is True

    class _UnhealthyVector:
        name = "qdrant"

        async def health(self) -> dict[str, object]:
            return {"ok": False, "reason": "unreachable"}

    client.app.state.knowledgebase_service._vector_backend = _UnhealthyVector()
    unhealthy = client.get("/api/v1/knowledgebases/capabilities").json()
    assert unhealthy["backend_ready"] is False
    assert unhealthy["indexer_ready"] is False

    client.app.state.knowledgebase_service = None
    response = client.get("/api/v1/knowledgebases/capabilities")
    assert response.status_code == 200
    assert response.json()["enabled"] is False


def test_ingestion_rejects_before_persistence_when_embedding_is_not_ready(
    tmp_path: object,
) -> None:
    client = _client(tmp_path)

    async def _remove_embedding_route() -> tuple[int, int]:
        async with client.app.state.session_factory() as session:
            await session.execute(
                sa.delete(ModelRouting).where(ModelRouting.task_type == "embedding")
            )
            await session.commit()
            return (
                await session.scalar(sa.select(sa.func.count(ArtifactRecordRow.artifact_id))),
                await session.scalar(sa.select(sa.func.count(KnowledgebaseIndexJobRow.job_id))),
            )

    before = anyio.run(_remove_embedding_route)
    response = client.post(
        "/api/v1/knowledgebases/kb_owner/documents",
        files=[("files[]", ("blocked.txt", b"blocked", "text/plain"))],
    )
    assert response.status_code == 503
    assert (
        client.post(
            "/api/v1/knowledgebases/kb_owner/search",
            json={"query": "blocked"},
        ).status_code
        == 503
    )
    assert (
        client.post(
            "/api/v1/knowledgebases/kb_owner/ask",
            json={"question": "blocked"},
        ).status_code
        == 503
    )
    assert client.get("/api/v1/knowledgebases/capabilities").status_code == 200
    assert client.get("/api/v1/knowledgebases/capabilities").json()["embedding_ready"] is False

    async def _counts() -> tuple[int, int]:
        async with client.app.state.session_factory() as session:
            return (
                await session.scalar(sa.select(sa.func.count(ArtifactRecordRow.artifact_id))),
                await session.scalar(sa.select(sa.func.count(KnowledgebaseIndexJobRow.job_id))),
            )

    assert anyio.run(_counts) == before


def test_document_ingestion_browse_content_update_and_conflicts(tmp_path: object) -> None:
    client = _client(tmp_path)
    upload = client.post(
        "/api/v1/knowledgebases/kb_owner/documents",
        files=[
            ("files[]", ("alpha.txt", b"Alpha source", "text/plain")),
            ("files[]", ("bad.bin", b"\x00\x01", "application/octet-stream")),
        ],
        data={
            "paths[]": ["docs/alpha.txt", "../bad.bin"],
            "metadata": '{"category":"reference"}',
            "conflict_policy": "replace",
        },
    )

    assert upload.status_code == 200
    outcomes = upload.json()["outcomes"]
    assert [item["status"] for item in outcomes] == ["created", "failed"]
    document_id = outcomes[0]["kb_artifact_id"]

    listing = client.get(
        "/api/v1/knowledgebases/kb_owner/documents",
        params={"path_prefix": "docs", "q": "alpha", "limit": 1},
    )
    assert listing.status_code == 200
    assert listing.json()["documents"][0]["source_path"] == "docs/alpha.txt"
    assert listing.json()["next_cursor"] is None

    second = client.post(
        "/api/v1/knowledgebases/kb_owner/documents",
        files=[("files[]", ("beta.txt", b"Beta source", "text/plain"))],
        data={"paths[]": "docs/beta.txt"},
    )
    assert second.status_code == 200
    first_page = client.get(
        "/api/v1/knowledgebases/kb_owner/documents",
        params={"path_prefix": "docs", "limit": 1},
    ).json()
    assert first_page["next_cursor"] is not None
    second_page = client.get(
        "/api/v1/knowledgebases/kb_owner/documents",
        params={
            "path_prefix": "docs",
            "limit": 1,
            "cursor": first_page["next_cursor"],
        },
    )
    assert second_page.status_code == 200
    assert second_page.json()["documents"][0]["source_path"] == "docs/beta.txt"
    mismatched_cursor = client.get(
        "/api/v1/knowledgebases/kb_owner/documents",
        params={"sort": "updated_at", "cursor": first_page["next_cursor"]},
    )
    assert mismatched_cursor.status_code == 400
    assert (
        client.get(
            "/api/v1/knowledgebases/kb_owner/documents",
            params={
                "path_prefix": "docs",
                "direction": "desc",
                "cursor": first_page["next_cursor"],
            },
        ).status_code
        == 400
    )
    assert (
        client.get(
            "/api/v1/knowledgebases/kb_owner/documents",
            params={
                "path_prefix": "manual",
                "cursor": first_page["next_cursor"],
            },
        ).status_code
        == 400
    )

    detail = client.get(f"/api/v1/knowledgebases/kb_owner/documents/{document_id}")
    assert detail.status_code == 200
    assert detail.json()["pending_artifact_id"] == outcomes[0]["artifact_id"]
    assert detail.json()["last_job"]["status"] == "queued"

    content = client.get(
        f"/api/v1/knowledgebases/kb_owner/documents/{document_id}/content",
        params={"content_mode": "source"},
    )
    assert content.status_code == 200
    assert content.json()["text"] == "Alpha source"
    extracted = client.get(f"/api/v1/knowledgebases/kb_owner/documents/{document_id}/content")
    assert extracted.status_code == 200
    assert extracted.json()["text"] == "Alpha source"

    update = client.patch(
        f"/api/v1/knowledgebases/kb_owner/documents/{document_id}",
        json={"source_path": "manual/alpha.txt", "metadata": {"tags": ["alpha"]}},
    )
    assert update.status_code == 200
    assert update.json()["source_path"] == "manual/alpha.txt"
    assert update.json()["metadata"] == {"tags": ["alpha"]}
    assert update.json()["desired_generation"] == 2

    unchanged = client.post(
        "/api/v1/knowledgebases/kb_owner/documents",
        files=[("files[]", ("alpha.txt", b"Alpha source", "text/plain"))],
        data={"paths[]": "manual/alpha.txt", "conflict_policy": "replace"},
    )
    assert unchanged.status_code == 200
    assert unchanged.json()["outcomes"][0]["status"] == "unchanged"

    metadata_changed = client.post(
        "/api/v1/knowledgebases/kb_owner/documents",
        files=[("files[]", ("alpha.txt", b"Alpha source", "text/plain"))],
        data={
            "paths[]": "manual/alpha.txt",
            "metadata": '{"category":"changed"}',
            "conflict_policy": "replace",
        },
    )
    assert metadata_changed.json()["outcomes"][0]["status"] == "updated"
    changed_detail = client.get(f"/api/v1/knowledgebases/kb_owner/documents/{document_id}").json()
    assert changed_detail["metadata"] == {"category": "changed"}
    changed_generation = changed_detail["desired_generation"]

    metadata_cleared = client.post(
        "/api/v1/knowledgebases/kb_owner/documents",
        files=[("files[]", ("alpha.txt", b"Alpha source", "text/plain"))],
        data={
            "paths[]": "manual/alpha.txt",
            "metadata": "{}",
            "conflict_policy": "replace",
        },
    )
    assert metadata_cleared.json()["outcomes"][0]["status"] == "updated"
    cleared_detail = client.get(f"/api/v1/knowledgebases/kb_owner/documents/{document_id}").json()
    assert cleared_detail["metadata"] == {}
    assert cleared_detail["desired_generation"] == changed_generation + 1

    skipped = client.post(
        "/api/v1/knowledgebases/kb_owner/documents",
        files=[("files[]", ("alpha.txt", b"Different", "text/plain"))],
        data={"paths[]": "manual/alpha.txt", "conflict_policy": "skip"},
    )
    assert skipped.json()["outcomes"][0]["status"] == "skipped"

    kept = client.post(
        "/api/v1/knowledgebases/kb_owner/documents",
        files=[("files[]", ("alpha.txt", b"Different", "text/plain"))],
        data={"paths[]": "manual/alpha.txt", "conflict_policy": "keep_both"},
    )
    assert kept.json()["outcomes"][0]["status"] == "created"
    assert kept.json()["outcomes"][0]["source_path"] == "manual/alpha (2).txt"

    cleared_path = client.patch(
        f"/api/v1/knowledgebases/kb_owner/documents/{document_id}",
        json={"source_path": None},
    )
    assert cleared_path.status_code == 200
    assert cleared_path.json()["source_path"] is None
    assert cleared_path.json()["desired_generation"] == cleared_detail["desired_generation"] + 1


def test_document_surface_is_owner_only_and_archived_read_only(tmp_path: object) -> None:
    client = _client(tmp_path)
    headers = {
        "x-user": "grantee@example.com",
        "x-agent-id": "agent_owner",
        "x-agent-owner": "owner@example.com",
    }

    assert (
        client.get("/api/v1/knowledgebases/kb_owner/documents", headers=headers).status_code == 404
    )
    assert (
        client.get(
            "/api/v1/knowledgebases/kb_owner/documents",
            headers={"x-user": "other@example.com"},
        ).status_code
        == 404
    )
    assert (
        client.get(
            "/api/v1/knowledgebases/kb_owner/documents",
            headers={"x-role": "viewer"},
        ).status_code
        == 200
    )
    assert (
        client.post(
            "/api/v1/knowledgebases/kb_owner/documents",
            files=[("files[]", ("readme.txt", b"text", "text/plain"))],
            headers={"x-role": "viewer"},
        ).status_code
        == 403
    )

    assert (
        client.patch("/api/v1/knowledgebases/kb_owner", json={"status": "archived"}).status_code
        == 200
    )
    assert client.get("/api/v1/knowledgebases/kb_owner/documents").status_code == 200
    archived_upload = client.post(
        "/api/v1/knowledgebases/kb_owner/documents",
        files=[("files[]", ("readme.txt", b"text", "text/plain"))],
    )
    assert archived_upload.status_code == 200
    assert archived_upload.json()["outcomes"][0]["status"] == "failed"


def test_document_batch_rejects_absolute_and_duplicate_paths_per_item(
    tmp_path: object,
) -> None:
    client = _client(tmp_path)

    response = client.post(
        "/api/v1/knowledgebases/kb_owner/documents",
        files=[
            ("files[]", ("one.txt", b"one", "text/plain")),
            ("files[]", ("two.txt", b"two", "text/plain")),
            ("files[]", ("three.txt", b"three", "text/plain")),
        ],
        data={
            "paths[]": ["docs/same.txt", "docs/same.txt", "/absolute.txt"],
            "conflict_policy": "replace",
        },
    )

    assert response.status_code == 200
    assert [item["status"] for item in response.json()["outcomes"]] == [
        "created",
        "failed",
        "failed",
    ]


def test_concurrent_document_conflict_policies_are_atomic(tmp_path: object) -> None:
    client = _client(tmp_path)

    def upload(content: bytes, policy: str, path: str) -> dict[str, object]:
        response = client.post(
            "/api/v1/knowledgebases/kb_owner/documents",
            files=[("files[]", ("source.txt", content, "text/plain"))],
            data={"paths[]": path, "conflict_policy": policy},
        )
        assert response.status_code == 200
        return response.json()["outcomes"][0]

    with ThreadPoolExecutor(max_workers=2) as executor:
        skipped = list(
            executor.map(
                lambda value: upload(value, "skip", "concurrent/skip.txt"),
                [b"one", b"two"],
            )
        )
    assert sorted(item["status"] for item in skipped) == ["created", "skipped"]

    with ThreadPoolExecutor(max_workers=2) as executor:
        kept = list(
            executor.map(
                lambda value: upload(value, "keep_both", "concurrent/keep.txt"),
                [b"one", b"two"],
            )
        )
    assert [item["status"] for item in kept] == ["created", "created"]
    assert {item["source_path"] for item in kept} == {
        "concurrent/keep.txt",
        "concurrent/keep (2).txt",
    }


def test_document_content_reader_enforces_size_bound(tmp_path: object) -> None:
    client = _client(tmp_path)

    async def _oversize() -> None:
        async with client.app.state.session_factory() as session:
            artifact = await session.get(ArtifactRecordRow, "artifact_1")
            assert artifact is not None
            artifact.size_bytes = 3 * 1024 * 1024
            await session.commit()

    anyio.run(_oversize)
    response = client.get("/api/v1/knowledgebases/kb_owner/documents/kba_1/content")
    assert response.status_code == 413


def test_document_detail_fetches_last_job_outside_newest_hundred(
    tmp_path: object,
) -> None:
    client = _client(tmp_path)

    async def _jobs() -> None:
        async with client.app.state.session_factory() as session:
            attachment = await session.get(KnowledgebaseArtifactRow, "kba_1")
            assert attachment is not None
            attachment.last_job_id = "older-last-job"
            session.add(
                KnowledgebaseIndexJobRow(
                    job_id="older-last-job",
                    knowledgebase_id="kb_owner",
                    kb_artifact_id="kba_1",
                    artifact_id="artifact_1",
                    job_type="reindex_artifact",
                    status="failed",
                )
            )
            session.add_all(
                [
                    KnowledgebaseIndexJobRow(
                        job_id=f"newer-job-{index}",
                        knowledgebase_id="kb_owner",
                        kb_artifact_id="kba_1",
                        artifact_id="artifact_1",
                        job_type="reindex_artifact",
                        status="succeeded",
                    )
                    for index in range(101)
                ]
            )
            await session.commit()

    anyio.run(_jobs)
    response = client.get("/api/v1/knowledgebases/kb_owner/documents/kba_1")
    assert response.status_code == 200
    assert response.json()["last_job"]["job_id"] == "older-last-job"


def test_document_upload_stream_enforces_configured_size_bound(tmp_path: object) -> None:
    client = _client(tmp_path)
    client.app.state.knowledgebase_service._max_artifact_size_bytes = 4

    response = client.post(
        "/api/v1/knowledgebases/kb_owner/documents",
        files=[("files[]", ("large.txt", b"12345", "text/plain"))],
    )

    assert response.status_code == 413


def test_document_upload_enforces_aggregate_and_form_field_budgets_before_persistence(
    tmp_path: object,
) -> None:
    client = _client(tmp_path)
    service = client.app.state.knowledgebase_service
    service._max_artifact_size_bytes = 5

    async def _counts() -> tuple[int, int]:
        async with client.app.state.session_factory() as session:
            return (
                await session.scalar(sa.select(sa.func.count(ArtifactRecordRow.artifact_id))),
                await session.scalar(sa.select(sa.func.count(KnowledgebaseIndexJobRow.job_id))),
            )

    before = anyio.run(_counts)
    aggregate = client.post(
        "/api/v1/knowledgebases/kb_owner/documents",
        files=[("files[]", (f"{index}.txt", b"12345", "text/plain")) for index in range(5)],
    )
    assert aggregate.status_code == 413
    assert anyio.run(_counts) == before

    service._max_artifact_size_bytes = 50 * 1024 * 1024
    metadata = client.post(
        "/api/v1/knowledgebases/kb_owner/documents",
        files=[("files[]", ("metadata.txt", b"text", "text/plain"))],
        data={"metadata": '{"long":"' + ("x" * (65 * 1024)) + '"}'},
    )
    assert metadata.status_code == 400
    assert anyio.run(_counts) == before

    path = client.post(
        "/api/v1/knowledgebases/kb_owner/documents",
        files=[("files[]", ("path.txt", b"text", "text/plain"))],
        data={"paths[]": "documents/" + ("x" * 1024) + ".txt"},
    )
    assert path.status_code == 400
    assert anyio.run(_counts) == before


def test_ingestion_save_failure_cleans_partially_written_artifact(
    tmp_path: object,
) -> None:
    client = _client(tmp_path)
    store = client.app.state.artifact_store
    original_save = store.async_save
    original_delete = store.async_delete
    deleted: list[tuple[str, str, str]] = []

    async def failing_save(*args: object, **kwargs: object) -> object:
        await original_save(*args, **kwargs)
        raise RuntimeError("sidecar failed")

    async def recording_delete(namespace: str, object_id: str, filename: str) -> None:
        deleted.append((namespace, object_id, filename))
        await original_delete(namespace, object_id, filename)

    store.async_save = failing_save
    store.async_delete = recording_delete
    response = client.post(
        "/api/v1/knowledgebases/kb_owner/documents",
        files=[("files[]", ("cleanup.txt", b"cleanup", "text/plain"))],
    )

    assert response.status_code == 200
    assert response.json()["outcomes"][0]["status"] == "failed"
    assert len(deleted) == 1


def test_uncertain_artifact_commit_never_deletes_canonical_bytes(
    tmp_path: object,
) -> None:
    client = _client(tmp_path)
    service = client.app.state.knowledgebase_service
    deleted: list[str] = []

    async def failed_lookup(artifact_id: str) -> bool:
        del artifact_id
        raise RuntimeError("database unavailable")

    async def record_delete(namespace: str, object_id: str, filename: str) -> None:
        del namespace, filename
        deleted.append(object_id)

    service._artifact_record_exists = failed_lookup
    client.app.state.artifact_store.async_delete = record_delete
    anyio.run(
        lambda: service._delete_artifact_if_uncommitted(
            artifact_id="possibly-committed",
            filename="source.txt",
        )
    )
    assert deleted == []


def test_cancelled_ingestion_save_cleans_definitely_uncommitted_blob(
    tmp_path: object,
) -> None:
    client = _client(tmp_path)

    async def scenario() -> None:
        service = client.app.state.knowledgebase_service
        store = client.app.state.artifact_store
        original_save = store.async_save
        original_delete = store.async_delete
        started = asyncio.Event()
        release = asyncio.Event()
        deleted: list[str] = []

        async def blocked_save(*args: object, **kwargs: object) -> object:
            started.set()
            await release.wait()
            return await original_save(*args, **kwargs)

        async def recording_delete(namespace: str, object_id: str, filename: str) -> None:
            deleted.append(object_id)
            await original_delete(namespace, object_id, filename)

        store.async_save = blocked_save
        store.async_delete = recording_delete
        task = asyncio.create_task(
            service.ingest_documents(
                owner_email="owner@example.com",
                knowledgebase_id="kb_owner",
                files=[("cancel.txt", b"cancel", "text/plain", "cancel.txt")],
                metadata=None,
                conflict_policy="replace",
            )
        )
        await started.wait()
        task.cancel()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert len(deleted) == 1
        async with client.app.state.session_factory() as session:
            assert (
                await session.scalar(
                    sa.select(ArtifactRecordRow).where(ArtifactRecordRow.filename == "cancel.txt")
                )
                is None
            )

    anyio.run(scenario)


@pytest.mark.parametrize("lookup", [True, RuntimeError("database unavailable")])
def test_cancelled_commit_cleanup_preserves_successful_or_uncertain_blob(
    tmp_path: object, lookup: bool | Exception
) -> None:
    client = _client(tmp_path)

    async def scenario() -> None:
        service = client.app.state.knowledgebase_service
        deleted: list[str] = []

        async def lookup_result(artifact_id: str) -> bool:
            del artifact_id
            if isinstance(lookup, Exception):
                raise lookup
            return lookup

        async def record_delete(namespace: str, object_id: str, filename: str) -> None:
            del namespace, filename
            deleted.append(object_id)

        service._artifact_record_exists = lookup_result
        client.app.state.artifact_store.async_delete = record_delete
        await service._delete_artifact_if_uncommitted(
            artifact_id="commit-outcome",
            filename="source.txt",
        )
        assert deleted == []

    anyio.run(scenario)


@pytest.mark.parametrize("commit_first", [False, True])
def test_cancelled_ingestion_during_commit_handles_definite_and_successful_outcome(
    tmp_path: object, monkeypatch: pytest.MonkeyPatch, commit_first: bool
) -> None:
    from sqlalchemy.ext.asyncio import AsyncSession

    client = _client(tmp_path)

    async def scenario() -> None:
        service = client.app.state.knowledgebase_service
        store = client.app.state.artifact_store
        original_commit = AsyncSession.commit
        original_delete = store.async_delete
        commit_reached = asyncio.Event()
        deleted: list[str] = []

        async def controlled_commit(session: AsyncSession) -> None:
            if commit_first:
                await original_commit(session)
            commit_reached.set()
            await asyncio.Event().wait()

        async def recording_delete(namespace: str, object_id: str, filename: str) -> None:
            deleted.append(object_id)
            await original_delete(namespace, object_id, filename)

        monkeypatch.setattr(AsyncSession, "commit", controlled_commit)
        store.async_delete = recording_delete
        task = asyncio.create_task(
            service.ingest_documents(
                owner_email="owner@example.com",
                knowledgebase_id="kb_owner",
                files=[("commit.txt", b"commit", "text/plain", "commit.txt")],
                metadata=None,
                conflict_policy="replace",
            )
        )
        await commit_reached.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        monkeypatch.setattr(AsyncSession, "commit", original_commit)
        async with client.app.state.session_factory() as session:
            persisted = await session.scalar(
                sa.select(ArtifactRecordRow).where(ArtifactRecordRow.filename == "commit.txt")
            )
        if commit_first:
            assert persisted is not None
            assert deleted == []
        else:
            assert persisted is None
            assert len(deleted) == 1

    anyio.run(scenario)


def test_queued_job_can_be_cancelled(tmp_path: object) -> None:
    client = _client(tmp_path)
    upload = client.post(
        "/api/v1/knowledgebases/kb_owner/documents",
        files=[("files[]", ("cancel.txt", b"cancel", "text/plain"))],
    )
    job_id = upload.json()["outcomes"][0]["job_id"]

    response = client.post(f"/api/v1/knowledgebases/kb_owner/jobs/{job_id}/cancel")

    assert response.status_code == 200
    assert response.json()["status"] == "cancelled"


def test_ask_route_skips_synthesis_without_matches_and_allows_shared_use(
    tmp_path: object,
) -> None:
    client = _client(tmp_path)

    owner = client.post("/api/v1/knowledgebases/kb_owner/ask", json={"question": "Question"})
    assert owner.status_code == 200
    assert owner.json()["status"] == "insufficient_evidence"

    shared = client.post(
        "/api/v1/knowledgebases/kb_owner/ask",
        json={"question": "Shared question"},
        headers={
            "x-user": "grantee@example.com",
            "x-agent-id": "agent_owner",
            "x-agent-owner": "owner@example.com",
        },
    )
    assert shared.status_code == 200
    assert shared.json()["status"] == "insufficient_evidence"

    unrelated = client.post(
        "/api/v1/knowledgebases/kb_owner/ask",
        json={"question": "Unrelated"},
        headers={"x-user": "other@example.com"},
    )
    assert unrelated.status_code == 404


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


def test_direct_user_share_read_query_revocation_and_owner_only_controls(
    tmp_path: object,
) -> None:
    client = _client(tmp_path)
    headers = {"x-user": "grantee@example.com"}
    candidates = client.get(
        "/api/v1/knowledgebases/kb_owner/shares/candidates", params={"q": "grant"}
    )
    assert "grantee@example.com" in {row["email"] for row in candidates.json()}
    granted = client.put(
        "/api/v1/knowledgebases/kb_owner/shares",
        json={"user_email": "grantee@example.com", "permission": "view"},
    )
    assert granted.status_code == 200
    duplicate = client.put(
        "/api/v1/knowledgebases/kb_owner/shares",
        json={"user_email": "grantee@example.com", "permission": "view"},
    )
    assert duplicate.json()["grant_id"] == granted.json()["grant_id"]
    assert len(client.get("/api/v1/knowledgebases/kb_owner/shares").json()) == 1
    listing = client.get("/api/v1/knowledgebases", headers=headers).json()
    assert listing[0]["access_level"] == "shared"
    assert listing[0]["owner_email"] == "owner@example.com"
    for path in (
        "/api/v1/knowledgebases/kb_owner",
        "/api/v1/knowledgebases/kb_owner/documents",
        "/api/v1/knowledgebases/kb_owner/documents/kba_1/content",
    ):
        assert client.get(path, headers=headers).status_code == 200
    assert (
        client.post(
            "/api/v1/knowledgebases/kb_owner/search",
            json={"query": "source"},
            headers=headers,
        ).status_code
        == 200
    )
    assert (
        client.post(
            "/api/v1/knowledgebases/kb_owner/ask",
            json={"question": "source?"},
            headers=headers,
        ).status_code
        == 200
    )
    assert (
        client.patch(
            "/api/v1/knowledgebases/kb_owner",
            json={"name": "forbidden"},
            headers=headers,
        ).status_code
        == 404
    )
    assert client.get("/api/v1/knowledgebases/kb_owner/shares", headers=headers).status_code == 404
    assert client.get("/api/v1/knowledgebases/kb_owner/jobs", headers=headers).status_code == 404
    assert (
        client.delete("/api/v1/knowledgebases/kb_owner/shares/grantee@example.com").status_code
        == 200
    )
    assert client.get("/api/v1/knowledgebases/kb_owner", headers=headers).status_code == 404
    assert client.get("/api/v1/knowledgebases", headers=headers).json() == []


def test_share_rejects_self_missing_disabled_and_archived(tmp_path: object) -> None:
    client = _client(tmp_path)
    assert (
        client.put(
            "/api/v1/knowledgebases/kb_owner/shares",
            json={"user_email": "owner@example.com"},
        ).status_code
        == 400
    )
    assert (
        client.put(
            "/api/v1/knowledgebases/kb_owner/shares",
            json={"user_email": "missing@example.com"},
        ).status_code
        == 400
    )

    async def _disable() -> None:
        async with client.app.state.session_factory() as session:
            user = await session.get(User, "grantee@example.com")
            assert user is not None
            user.is_active = False
            await session.commit()

    anyio.run(_disable)
    assert (
        client.put(
            "/api/v1/knowledgebases/kb_owner/shares",
            json={"user_email": "grantee@example.com"},
        ).status_code
        == 400
    )
    assert (
        client.put(
            "/api/v1/knowledgebases/kb_owner/shares",
            json={"user_email": "other@example.com"},
        ).status_code
        == 200
    )
    assert (
        client.patch("/api/v1/knowledgebases/kb_owner", json={"status": "archived"}).status_code
        == 200
    )
    assert (
        client.put(
            "/api/v1/knowledgebases/kb_owner/shares",
            json={"user_email": "other@example.com"},
        ).status_code
        == 404
    )
    archived_revoke = client.delete("/api/v1/knowledgebases/kb_owner/shares/other@example.com")
    assert archived_revoke.status_code == 404
    assert archived_revoke.json()["detail"]["code"] == "not_found"
    other_headers = {"x-user": "other@example.com"}
    assert client.get("/api/v1/knowledgebases/kb_owner", headers=other_headers).status_code == 200
    assert (
        client.get("/api/v1/knowledgebases/kb_owner/documents", headers=other_headers).status_code
        == 200
    )
    assert (
        client.post(
            "/api/v1/knowledgebases/kb_owner/agents/agent_owner",
            headers={"x-user": "other@example.com"},
        ).status_code
        == 404
    )


def test_share_candidates_require_bounded_owner_search(tmp_path: object) -> None:
    client = _client(tmp_path)
    endpoint = "/api/v1/knowledgebases/kb_owner/shares/candidates"

    for query in (None, "", " ", "x"):
        params = {} if query is None else {"q": query}
        response = client.get(endpoint, params=params)
        assert response.status_code == 400
        assert response.json()["detail"]["code"] == "invalid_share_candidate_query"

    assert (
        client.get(
            endpoint,
            params={"q": "grant"},
            headers={"x-user": "other@example.com"},
        ).status_code
        == 404
    )

    async def _add_candidates() -> None:
        async with client.app.state.session_factory() as session:
            session.add_all(
                [
                    User(
                        email=f"candidate-{index:02d}@example.com",
                        name=f"Candidate {index:02d}",
                        role="user",
                    )
                    for index in range(25)
                ]
                + [
                    User(
                        email="candidate-disabled@example.com",
                        name="Candidate Disabled",
                        role="user",
                        is_active=False,
                    ),
                    User(
                        email="candidate-system@example.com",
                        name="Candidate System",
                        role="system",
                    ),
                ]
            )
            await session.commit()

    anyio.run(_add_candidates)
    response = client.get(endpoint, params={"q": "candidate"})
    assert response.status_code == 200
    assert len(response.json()) == 20
    emails = {candidate["email"] for candidate in response.json()}
    assert "owner@example.com" not in emails
    assert "candidate-disabled@example.com" not in emails
    assert "candidate-system@example.com" not in emails


def test_direct_grant_does_not_bypass_unassigned_active_agent_context(
    tmp_path: object,
) -> None:
    client = _client(tmp_path)
    assert (
        client.put(
            "/api/v1/knowledgebases/kb_owner/shares",
            json={"user_email": "grantee@example.com"},
        ).status_code
        == 200
    )

    async def _add_agent() -> None:
        async with client.app.state.session_factory() as session:
            session.add(
                Agent(
                    agent_id="agent_unassigned",
                    owner_email="owner@example.com",
                    name="Unassigned Agent",
                    status="active",
                    permissions=AgentPermissions().model_dump(mode="json"),
                )
            )
            await session.commit()

    anyio.run(_add_agent)
    headers = {
        "x-user": "grantee@example.com",
        "x-agent-id": "agent_unassigned",
        "x-agent-owner": "owner@example.com",
    }
    assert client.get("/api/v1/knowledgebases", headers=headers).json() == []
    assert client.get("/api/v1/knowledgebases/kb_owner", headers=headers).status_code == 404
    assert (
        client.get("/api/v1/knowledgebases/kb_owner/documents", headers=headers).status_code == 404
    )
