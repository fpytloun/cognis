"""Library browsing through its HTTP boundary, backed by a real metadata DB."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
import sqlalchemy as sa
from fastapi import FastAPI, Request

from cognis.api.authentication import AuthenticatedUser
from cognis.api.routes.deliverables import router as deliverables_router
from cognis.api.routes.library import router
from cognis.artifacts.store import ArtifactStore, ArtifactStoreConfig
from cognis.store.database import create_engine, create_session_factory
from cognis.store.models import (
    ArtifactRecordRow,
    Base,
    DeliverableRow,
    DocumentFavorite,
    ProjectGrantRow,
)
from cognis.store.queries import (
    create_agent,
    create_artifact_record,
    create_conversation,
    create_deliverable,
    create_project,
    create_project_grant,
    create_session,
    create_step_run,
    create_task,
    create_user,
    delete_conversation,
)

OWNER = "author@library.test"
READER = "reader@library.test"
OUTSIDER = "admin@library.test"


@pytest.fixture
async def library_app(tmp_path: Path):
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path}/library.db")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = create_session_factory(engine)
    store = ArtifactStore(
        ArtifactStoreConfig(
            path=str(tmp_path / "files"),
            base_url="http://library.test",
            signing_secret="library-test-signing-secret",
        )
    )
    async with factory() as session:
        for email in (OWNER, READER, OUTSIDER):
            await create_user(session, email=email, name=email, password_hash="test")
        await create_agent(
            session, agent_id="library-author", owner_email=OWNER, name="Library author"
        )
        await create_project(
            session, project_id="library-project", owner_email=OWNER, name="Library project"
        )
        await create_project_grant(
            session,
            project_id="library-project",
            granted_by=OWNER,
            grantee_user_email=READER,
            permission="viewer",
        )
        for ref, visibility in (("library-shared", "project"), ("library-private", "private")):
            await create_conversation(
                session,
                conversation_id=ref,
                user_email=OWNER,
                agent_id="library-author",
                context_type="web",
                project_id="library-project",
                visibility=visibility,
                title=f"{visibility} source",
            )
        first = await create_deliverable(
            session,
            conversation_id="library-shared",
            content="# Readable report",
            title="Release report",
            artifact_store=store,
            published_owner_email=OWNER,
        )
        first.status = "delivered"
        first.project_id = "library-project"
        second = await create_deliverable(
            session,
            conversation_id="library-private",
            content="# Secret report",
            title="Private salary report",
            artifact_store=store,
            published_owner_email=OWNER,
        )
        second.status = "delivered"
        await session.commit()
        ids = {"shared": first.deliverable_id, "private": second.deliverable_id}
    app = FastAPI()
    app.state.session_factory = factory
    app.state.artifact_store = store
    app.state.ids = ids
    app.include_router(router)
    app.include_router(deliverables_router)

    @app.middleware("http")
    async def identity(request: Request, call_next):
        # Authentication is isolated here; production uses the normal app middleware.
        if email := request.headers.get("x-library-user"):
            request.state.user = AuthenticatedUser(
                email=email, role="admin" if email == OUTSIDER else "user"
            )
        return await call_next(request)

    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://library.test"
        ) as client:
            yield app, client
    finally:
        await engine.dispose()


@pytest.mark.parametrize("kind", ["deliverable", "artifact"])
async def test_personal_favorites_follow_revisions_and_never_grant_access(library_app, kind):
    app, client = library_app
    if kind == "deliverable":
        ref = app.state.ids["shared"]
    else:
        ref = "art_favorite_root"
        async with app.state.session_factory() as session:
            await create_artifact_record(
                session,
                artifact_id=ref,
                namespace="artifacts",
                object_id=ref,
                filename="Favorite report.md",
                owner_email=OWNER,
                purpose="artifact_write",
                kind="file",
                mime_type="text/markdown",
                size_bytes=10,
                status="attached",
                conversation_id="library-shared",
                project_id="library-project",
            )
            await session.commit()
    path = f"/api/v1/library/items/{kind}/{ref}"
    owner, reader, outsider = ({"x-library-user": email} for email in (OWNER, READER, OUTSIDER))
    assert (await client.put(path + "/favorite", json={"favorite": True})).status_code == 401
    assert (
        await client.put(path + "/favorite", headers=outsider, json={"favorite": True})
    ).status_code == 404
    assert (
        await client.put(
            f"/api/v1/library/items/{kind}/missing/favorite", headers=owner, json={"favorite": True}
        )
    ).status_code == 404
    original = (await client.get(path, headers=reader)).json()
    assert original["is_favorite"] is False
    for _ in range(2):
        marked = await client.put(path + "/favorite", headers=reader, json={"favorite": True})
        assert marked.status_code == 200, marked.text
        assert marked.json()["is_favorite"] is True
        assert marked.json()["updated_at"] == original["updated_at"]
    assert (await client.get(path, headers=owner)).json()["is_favorite"] is False
    async with app.state.session_factory() as session:
        assert await session.scalar(sa.select(sa.func.count()).select_from(DocumentFavorite)) == 1
        if kind == "deliverable":
            previous = await session.get(DeliverableRow, ref)
            revision = await create_deliverable(
                session,
                conversation_id="library-shared",
                content="# New revision",
                title="New favorite report",
                artifact_store=app.state.artifact_store,
                published_owner_email=OWNER,
            )
            revision.status = "delivered"
            revision.project_id = "library-project"
            revision.lineage_id = previous.lineage_id or ref
            revision.revision = 2
            revision.supersedes_deliverable_id = ref
            latest = revision.deliverable_id
        else:
            latest = "art_favorite_revision"
            await create_artifact_record(
                session,
                artifact_id=latest,
                namespace="artifacts",
                object_id=latest,
                filename="New favorite report.md",
                owner_email=OWNER,
                purpose="artifact_write",
                kind="file",
                mime_type="text/markdown",
                size_bytes=10,
                status="attached",
                conversation_id="library-shared",
                project_id="library-project",
                lineage_id=ref,
                revision=2,
            )
        await session.commit()
    latest_path = f"/api/v1/library/items/{kind}/{latest}"
    assert (await client.get(latest_path, headers=reader)).json()["is_favorite"] is True
    filtered = await client.get(
        "/api/v1/library/items",
        params={"kind": kind, "favorites": "true", "limit": 1},
        headers=reader,
    )
    assert [item["id"] for item in filtered.json()["items"]] == [latest]
    assert filtered.json()["next_cursor"] is None
    for group_by in ("project", "conversation", "agent", "format"):
        grouped = await client.get(
            "/api/v1/library/groups",
            params={"kind": kind, "favorites": "true", "group_by": group_by},
            headers=reader,
        )
        assert grouped.status_code == 200, grouped.text
        assert sum(group["count"] for group in grouped.json()["groups"]) == 1
    for _ in range(2):
        unmarked = await client.put(
            latest_path + "/favorite", headers=reader, json={"favorite": False}
        )
        assert unmarked.status_code == 200
        assert unmarked.json()["is_favorite"] is False
    assert (await client.get(path, headers=reader)).json()["is_favorite"] is False
    await client.put(path + "/favorite", headers=reader, json={"favorite": True})
    async with app.state.session_factory() as session:
        await session.execute(
            sa.delete(ProjectGrantRow).where(ProjectGrantRow.grantee_user_email == READER)
        )
        await session.commit()
    assert (await client.get(path, headers=reader)).status_code == 404
    assert (
        await client.put(path + "/favorite", headers=reader, json={"favorite": False})
    ).status_code == 404
    assert (
        await client.get(
            "/api/v1/library/items", params={"kind": kind, "favorites": "true"}, headers=reader
        )
    ).json()["items"] == []
    assert (
        await client.get(
            "/api/v1/library/groups",
            params={"kind": kind, "favorites": "true", "group_by": "project"},
            headers=reader,
        )
    ).json()["groups"] == []


@pytest.mark.parametrize("kind", ["deliverable", "artifact"])
@pytest.mark.parametrize("group_by", ["project", "conversation", "agent", "format"])
async def test_groups_follow_selected_recency_with_stable_pagination(library_app, kind, group_by):
    app, client = library_app
    start = datetime(2026, 10, 1, tzinfo=UTC)
    formats = {"a": "markdown", "b": "plain", "c": "html"}
    async with app.state.session_factory() as session:
        for suffix in ("a", "b", "c"):
            await create_project(
                session, project_id=f"sort-project-{suffix}", owner_email=OWNER, name=suffix
            )
            await create_agent(
                session, agent_id=f"sort-agent-{suffix}", owner_email=OWNER, name=suffix
            )
            await create_conversation(
                session,
                conversation_id=f"sort-source-{suffix}",
                user_email=OWNER,
                agent_id=f"sort-agent-{suffix}",
                context_type="web",
                project_id=f"sort-project-{suffix}",
                title=suffix,
            )
        for index, suffix in enumerate(("a", "b", "c", "a", "b")):
            title = f"Sorting regression {index}" if index < 4 else "Excluded newer document"
            if kind == "deliverable":
                row = await create_deliverable(
                    session,
                    conversation_id=f"sort-source-{suffix}",
                    content="Content",
                    title=title,
                    format=formats[suffix],
                    artifact_store=app.state.artifact_store,
                    published_owner_email=OWNER,
                )
                row.status = "delivered"
            else:
                ref = f"art_sorting_{index}"
                await create_artifact_record(
                    session,
                    artifact_id=ref,
                    namespace="artifacts",
                    object_id=ref,
                    filename=title,
                    owner_email=OWNER,
                    purpose="artifact_write",
                    kind="file",
                    mime_type=formats[suffix],
                    size_bytes=10,
                    status="attached",
                    conversation_id=f"sort-source-{suffix}",
                )
                row = await session.get(ArtifactRecordRow, ref)
            row.owner_agent_id = f"sort-agent-{suffix}"
            row.project_id = f"sort-project-{suffix}"
            # A second old item must not lower its folder's newest-item timestamp.
            row.created_at = start + timedelta(days=[2, 6, 4, 0, 20][index])
            row.updated_at = start + timedelta(days=[5, 3, 5, 0, 20][index])
        await session.commit()
    headers = {"x-library-user": OWNER}
    keys = {
        suffix: formats[suffix]
        if group_by == "format"
        else f"sort-{group_by if group_by != 'conversation' else 'source'}-{suffix}"
        for suffix in ("a", "b", "c")
    }
    for sort, expected in (
        ("updated", sorted([keys["a"], keys["c"]], reverse=True) + [keys["b"]]),
        ("created", [keys["b"], keys["c"], keys["a"]]),
    ):
        params = {"kind": kind, "q": "Sorting regression", "sort": sort, "group_by": group_by}
        complete = await client.get("/api/v1/library/groups", params=params, headers=headers)
        assert complete.status_code == 200, complete.text
        assert [group["id"] for group in complete.json()["groups"]] == expected
        assert {group["id"]: group["count"] for group in complete.json()["groups"]} == {
            keys["a"]: 2,
            keys["b"]: 1,
            keys["c"]: 1,
        }
        for limit in (1, 2):
            seen = []
            cursor = None
            for _ in range(4):
                response = await client.get(
                    "/api/v1/library/groups",
                    params={**params, "limit": limit, **({"cursor": cursor} if cursor else {})},
                    headers=headers,
                )
                assert response.status_code == 200, response.text
                page = response.json()
                seen.extend(group["id"] for group in page["groups"])
                cursor = page["next_cursor"]
                if cursor is None:
                    break
                if len(seen) == limit:
                    wrong_sort = await client.get(
                        "/api/v1/library/groups",
                        params={
                            **params,
                            "sort": "created" if sort == "updated" else "updated",
                            "cursor": cursor,
                        },
                        headers=headers,
                    )
                    assert wrong_sort.status_code == 400
            assert cursor is None
            assert seen == expected
        # The same ordering applies at the inner level of a project/source tree.
        nested = await client.get(
            "/api/v1/library/groups",
            params={**params, "project_id": "sort-project-a"},
            headers=headers,
        )
        assert nested.json()["groups"] == [
            {"id": keys["a"], "label": formats["a"] if group_by == "format" else "a", "count": 2}
        ]


async def test_library_access_groups_and_revocation(library_app):
    app, client = library_app
    assert (await client.get("/api/v1/library/items")).status_code == 401
    owner = (await client.get("/api/v1/library/items", headers={"x-library-user": OWNER})).json()
    assert {row["id"] for row in owner["items"]} == set(app.state.ids.values())
    reader = {"x-library-user": READER}
    response = await client.get("/api/v1/library/items", headers=reader)
    assert response.status_code == 200, response.text
    assert [row["id"] for row in response.json()["items"]] == [app.state.ids["shared"]]
    assert response.json()["items"][0]["is_readonly"]
    groups = (await client.get("/api/v1/library/groups?group_by=project", headers=reader)).json()
    assert groups["groups"] == [{"id": "library-project", "label": "Library project", "count": 1}]
    assert not (
        await client.get("/api/v1/library/items", headers={"x-library-user": OUTSIDER})
    ).json()["items"]
    assert (
        await client.get(
            f"/api/v1/library/items/deliverable/{app.state.ids['private']}", headers=reader
        )
    ).status_code == 404
    async with app.state.session_factory() as session:
        await session.execute(sa.update(ProjectGrantRow).values(revoked_at=datetime.now(UTC)))
        await session.commit()
    assert not (await client.get("/api/v1/library/items", headers=reader)).json()["items"]
    assert not (await client.get("/api/v1/library/groups", headers=reader)).json()["groups"]
    assert (
        await client.get(
            f"/api/v1/library/deliverables/{app.state.ids['shared']}/content", headers=reader
        )
    ).status_code == 404


async def test_pagination_filters_and_literal_search(library_app):
    _, client = library_app
    headers = {"x-library-user": OWNER}
    page = (await client.get("/api/v1/library/items?limit=1", headers=headers)).json()
    assert len(page["items"]) == 1 and page["next_cursor"]
    next_page = (
        await client.get(
            "/api/v1/library/items",
            params={"limit": 1, "cursor": page["next_cursor"]},
            headers=headers,
        )
    ).json()
    assert len(next_page["items"]) == 1 and next_page["items"][0]["id"] != page["items"][0]["id"]
    assert next_page["next_cursor"] is None
    assert (
        await client.get(
            "/api/v1/library/items",
            params={"q": "other", "cursor": page["next_cursor"]},
            headers=headers,
        )
    ).status_code == 400
    assert (
        await client.get("/api/v1/library/items?cursor=broken", headers=headers)
    ).status_code == 400
    assert (await client.get("/api/v1/library/items?limit=0", headers=headers)).status_code == 422
    assert not (await client.get("/api/v1/library/items?q=%25", headers=headers)).json()["items"]
    assert (
        len((await client.get("/api/v1/library/items?q=release", headers=headers)).json()["items"])
        == 1
    )


async def test_detached_deliverable_content_and_private_provenance(library_app):
    app, client = library_app
    async with app.state.session_factory() as session:
        private = await session.get(DeliverableRow, app.state.ids["private"])
        await create_session(
            session,
            session_id="library-private-session",
            user_email=OWNER,
            agent_id="library-author",
            conversation_id="library-private",
        )
        private.session_id = "library-private-session"
        private.turn_id = "library-private-turn"
        private.project_id = "library-project"
        await delete_conversation(session, "library-shared")
        await session.commit()
    headers = {"x-library-user": OWNER}
    ref = app.state.ids["shared"]
    detail = await client.get(f"/api/v1/library/deliverables/{ref}/content", headers=headers)
    assert detail.status_code == 200, detail.text
    assert "# Readable report" in detail.json()["content"]
    shared = (
        await client.get(
            f"/api/v1/library/items/deliverable/{app.state.ids['private']}",
            headers={"x-library-user": READER},
        )
    ).json()
    assert shared["conversation_id"] is None and shared["conversation_title"] is None
    assert shared["project_name"] == "Library project"
    content = await client.get(
        f"/api/v1/library/deliverables/{app.state.ids['private']}/content",
        headers={"x-library-user": READER},
    )
    assert content.status_code == 200
    assert content.json()["conversation_id"] is None
    assert content.json()["session_id"] is None
    assert content.json()["turn_id"] is None
    history = await client.get(
        f"/api/v1/deliverables/{app.state.ids['private']}/revisions",
        headers={"x-library-user": READER},
    )
    assert history.status_code == 200
    assert history.json()["revisions"][0]["conversation_id"] is None


async def test_file_origin_latest_revisions_and_expiry(library_app):
    app, client = library_app
    now = datetime.now(UTC)
    async with app.state.session_factory() as session:
        for ref, purpose, role, status, expiry in (
            ("att_library_upload", "chat_input", "user", "attached", None),
            ("art_library_output", "artifact_publish", "assistant", "attached", None),
            ("art_library_expired", "chat_input", "user", "attached", now - timedelta(days=1)),
            ("att_library_temporary", "chat_input", None, "temporary", now + timedelta(days=1)),
        ):
            await create_artifact_record(
                session,
                artifact_id=ref,
                namespace="artifacts",
                object_id=ref,
                filename=f"{ref}.txt",
                owner_email=OWNER,
                purpose=purpose,
                kind="file",
                mime_type="text/plain",
                size_bytes=123,
                message_role=role,
                status=status,
                expires_at=expiry,
            )
        await session.commit()
    headers = {"x-library-user": OWNER}
    files = (await client.get("/api/v1/library/items?kind=artifact", headers=headers)).json()[
        "items"
    ]
    assert {row["origin"] for row in files} == {"user", "agent"}
    assert len(files) == 2  # No deliverable bridge duplicates, expired or temporary rows.
    uploads = (
        await client.get("/api/v1/library/items?kind=artifact&origin=user", headers=headers)
    ).json()["items"]
    assert [row["id"] for row in uploads] == ["att_library_upload"]
    async with app.state.session_factory() as session:
        old = await session.get(DeliverableRow, app.state.ids["shared"])
        new = DeliverableRow(
            deliverable_id="dlv_library_revision",
            owner_email=OWNER,
            title="Release report revised",
            format="markdown",
            storage_object_id="dlv_library_revision",
            content_mime="text/markdown",
            content_hash="stored",
            lineage_id=old.lineage_id or old.deliverable_id,
            revision=2,
            supersedes_deliverable_id=old.deliverable_id,
            status="delivered",
        )
        session.add(new)
        await session.commit()
    result = (await client.get("/api/v1/library/items", headers=headers)).json()["items"]
    assert app.state.ids["shared"] not in {row["id"] for row in result}
    assert "dlv_library_revision" in {row["id"] for row in result}


async def test_expired_or_deleted_successor_does_not_hide_live_file(library_app):
    app, client = library_app
    async with app.state.session_factory() as session:
        for index in range(1, 4):
            await create_artifact_record(
                session,
                artifact_id=f"art_revision_{index}",
                namespace="artifacts",
                object_id=f"art_revision_{index}",
                filename=f"report-{index}.txt",
                owner_email=OWNER,
                purpose="artifact_write",
                kind="file",
                mime_type="text/plain",
                size_bytes=12,
                status="attached",
            )
            row = await session.get(ArtifactRecordRow, f"art_revision_{index}")
            row.lineage_id = "art_revision_1"
            row.revision = index
            row.supersedes_artifact_id = f"art_revision_{index - 1}" if index > 1 else None
        last = await session.get(ArtifactRecordRow, "art_revision_3")
        last.expires_at = datetime.now(UTC) - timedelta(days=1)
        middle = await session.get(ArtifactRecordRow, "art_revision_2")
        middle.status = "deleted"
        await session.commit()
    headers = {"x-library-user": OWNER}
    files = (await client.get("/api/v1/library/items?kind=artifact", headers=headers)).json()
    assert [row["id"] for row in files["items"]] == ["art_revision_1"]
    groups = (
        await client.get("/api/v1/library/groups?kind=artifact&group_by=project", headers=headers)
    ).json()
    assert groups["groups"] == [{"id": "", "label": "No project", "count": 1}]
    nested = (
        await client.get(
            "/api/v1/library/groups?kind=artifact&project_id=&group_by=conversation",
            headers=headers,
        )
    ).json()
    assert nested["groups"] == [{"id": "", "label": "No conversation", "count": 1}]


async def test_task_outputs_and_group_pagination(library_app):
    app, client = library_app
    async with app.state.session_factory() as session:
        task = await create_task(
            session,
            created_by=OWNER,
            agent_id="library-author",
            title="Library task",
            project_id="library-project",
        )
        task.visibility = "private"
        step = await create_step_run(
            session,
            task_id=task.task_id,
            step_name="report",
            step_type="run",
            agent_id="library-author",
        )
        output = await create_deliverable(
            session,
            step_run_id=step.step_run_id,
            content="# Task report",
            title="Task output",
            artifact_store=app.state.artifact_store,
        )
        output.status = "approved"
        output.project_id = "library-project"
        output_id = output.deliverable_id
        # Equal timestamps exercise the ID tie-breaker.
        await session.execute(
            sa.update(DeliverableRow).values(
                created_at=datetime(2026, 10, 1, tzinfo=UTC),
                updated_at=datetime(2026, 10, 1, tzinfo=UTC),
            )
        )
        await session.commit()
    reader = {"x-library-user": READER}
    content = await client.get(f"/api/v1/library/deliverables/{output_id}/content", headers=reader)
    assert content.status_code == 200 and "Task report" in content.json()["content"]
    assert content.json()["step_run_id"] is None
    source = (
        await client.get(f"/api/v1/library/items/deliverable/{output_id}", headers=reader)
    ).json()
    assert source["task_id"] is None and source["task_title"] is None
    history = (
        await client.get(f"/api/v1/deliverables/{output_id}/revisions", headers=reader)
    ).json()
    assert history["revisions"][0]["step_run_id"] is None
    assert history["revisions"][0]["task_id"] is None
    owner = {"x-library-user": OWNER}
    seen = set()
    cursor = None
    while True:
        response = (
            await client.get(
                "/api/v1/library/groups",
                params={
                    "group_by": "conversation",
                    "limit": 1,
                    **({"cursor": cursor} if cursor else {}),
                },
                headers=owner,
            )
        ).json()
        seen.update(group["id"] for group in response["groups"])
        cursor = response["next_cursor"]
        if cursor is None:
            break
    assert seen == {f"task:{task.task_id}", "library-private", "library-shared"}
    detail = (
        await client.get(f"/api/v1/library/items/deliverable/{output_id}", headers=owner)
    ).json()
    assert detail["task_id"] == task.task_id and detail["task_title"] == "Library task"
    assert detail["source_id"] == f"task:{task.task_id}"
    assert detail["source_title"] == "Task: Library task"
    assert source["source_id"] is None and source["source_title"] is None
    selected = (
        await client.get(
            "/api/v1/library/items", params={"source_id": detail["source_id"]}, headers=owner
        )
    ).json()
    assert [row["id"] for row in selected["items"]] == [output_id]


async def test_task_files_group_direct_and_specialist_sessions_without_duplicates(library_app):
    app, client = library_app
    async with app.state.session_factory() as session:
        task = await create_task(
            session, created_by=OWNER, agent_id="library-author", title="File analysis"
        )
        task.visibility = "private"
        for session_id, parent in (
            ("library-task-session", None),
            ("library-worker-session", "library-task-session"),
        ):
            await create_session(
                session,
                session_id=session_id,
                conversation_id="library-private",
                agent_id="library-author",
                user_email=OWNER,
                parent_session_id=parent,
            )
        for name in ("analyze", "review"):
            step = await create_step_run(
                session,
                task_id=task.task_id,
                step_name=name,
                step_type="run",
                agent_id="library-author",
            )
            step.session_id = "library-task-session"
        for ref, producer in (
            ("art_task_direct", "library-task-session"),
            ("art_task_worker", "library-worker-session"),
        ):
            await create_artifact_record(
                session,
                artifact_id=ref,
                namespace="artifacts",
                object_id=ref,
                filename=f"{ref}.txt",
                owner_email=OWNER,
                session_id=producer,
                purpose="artifact_write",
                kind="file",
                mime_type="text/plain",
                size_bytes=12,
                status="attached",
                project_id="library-project",
            )
        await session.commit()
    owner = {"x-library-user": OWNER}
    params = {"kind": "artifact", "group_by": "conversation"}
    groups = (await client.get("/api/v1/library/groups", params=params, headers=owner)).json()
    assert groups["groups"] == [
        {"id": f"task:{task.task_id}", "label": "Task: File analysis", "count": 2}
    ]
    files = (
        await client.get(
            "/api/v1/library/items",
            params={"kind": "artifact", "source_id": f"task:{task.task_id}"},
            headers=owner,
        )
    ).json()["items"]
    assert {row["id"] for row in files} == {"art_task_direct", "art_task_worker"}
    assert all(row["task_id"] == task.task_id for row in files)
    reader = {"x-library-user": READER}
    shared = (await client.get("/api/v1/library/items?kind=artifact", headers=reader)).json()[
        "items"
    ]
    assert len(shared) == 2 and all(
        row["source_id"] is None and row["task_id"] is None for row in shared
    )
    hidden = (
        await client.get(
            "/api/v1/library/items",
            params={"kind": "artifact", "source_id": f"task:{task.task_id}"},
            headers=reader,
        )
    ).json()
    assert hidden["items"] == []


@pytest.mark.parametrize("kind", ["artifact", "deliverable"])
def test_exact_item_scopes_recursive_producer_lookup_to_one_document(kind: str) -> None:
    """A single-item read must not walk every readable document's session ancestry."""
    from cognis.store.library_queries import library_items

    compiled = (
        sa.select(library_items("reader@example.com", kind, exact=True, ref_id="doc_exact"))
        .compile(compile_kwargs={"literal_binds": True})
        .string
    )
    seed, _, rest = compiled.partition("UNION ALL")
    assert "library_session_ancestors" in seed
    assert "'doc_exact'" in seed
    assert "'doc_exact'" in rest
