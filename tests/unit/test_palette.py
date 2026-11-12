"""Real HTTP/SQL palette boundary: access, index lifecycle and keyset pagination."""

from __future__ import annotations

import asyncio
import importlib
import os
from datetime import UTC, datetime

import httpx
import pytest
import sqlalchemy as sa
from fastapi import FastAPI, Request

from cognis.api.authentication import AuthenticatedUser
from cognis.api.routes.palette import router
from cognis.artifacts.store import ArtifactStore, ArtifactStoreConfig
from cognis.core.palette import PaletteAdmission, name_matches
from cognis.store.database import create_engine, create_session_factory
from cognis.store.models import Agent, Base, Conversation, Schedule, Task, User
from cognis.store.palette_indexes import ensure_palette_indexes
from cognis.store.queries import (
    create_agent,
    create_artifact_record,
    create_conversation,
    create_deliverable,
    create_project,
    create_project_grant,
    create_user,
)

OWNER = "palette-owner@test.local"
READER = "palette-reader@test.local"


@pytest.fixture(params=["sqlite", "postgresql"])
async def palette_app(request, tmp_path):
    if request.param == "postgresql":
        url = os.getenv("COGNIS_TEST_POSTGRES_URL")
        if not url:
            pytest.skip("Requires task-owned COGNIS_TEST_POSTGRES_URL")
    else:
        url = f"sqlite+aiosqlite:///{tmp_path}/palette.db"
    engine = create_engine(url)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await conn.run_sync(ensure_palette_indexes)
        await conn.run_sync(ensure_palette_indexes)
    factory = create_session_factory(engine)
    async with factory() as session:
        for email in (OWNER, READER):
            await create_user(session, email=email, name=email, password_hash="test")
        await create_agent(
            session, agent_id="palette-agent", owner_email=OWNER, name="Palette agent"
        )
        await create_project(
            session, project_id="palette-project", owner_email=OWNER, name="Palette project"
        )
        await create_project_grant(
            session,
            project_id="palette-project",
            granted_by=OWNER,
            grantee_user_email=READER,
            permission="viewer",
        )
        for index in range(35):
            await create_conversation(
                session,
                conversation_id=f"palette-chat-{index:02}",
                user_email=OWNER,
                agent_id="palette-agent",
                context_type="web",
                title=f"Command palette {index:02}",
                project_id="palette-project",
                visibility="project" if index % 2 == 0 else "private",
            )
        session.add(
            Task(
                task_id="palette-task",
                title="Command palette task",
                agent_id="palette-agent",
                created_by=OWNER,
                status="pending",
                description="Build palette",
            )
        )
        session.add(
            Schedule(
                schedule_id="palette-schedule",
                name="Command palette schedule",
                agent_id="palette-agent",
                created_by=OWNER,
                task_template={},
            )
        )
        await create_artifact_record(
            session,
            artifact_id="palette-image",
            namespace="artifacts",
            object_id="palette-image",
            filename="Command palette.png",
            owner_email=OWNER,
            purpose="artifact_write",
            kind="file",
            mime_type="image/png",
            size_bytes=100,
            status="attached",
            project_id="palette-project",
        )
        doc = await create_deliverable(
            session,
            conversation_id="palette-chat-00",
            title="Command palette report",
            content="report",
            published_owner_email=OWNER,
            artifact_store=ArtifactStore(
                ArtifactStoreConfig(
                    path=str(tmp_path / "files"), signing_secret="palette-test-signing-secret"
                )
            ),
        )
        doc.status = "delivered"
        # Document access must not expose private source metadata.
        doc.project_id = "palette-project"
        await session.commit()
    app = FastAPI()
    app.state.session_factory = factory
    app.state.palette_admission = PaletteAdmission(per_user_rate=100, global_rate=100)
    app.include_router(router)

    @app.middleware("http")
    async def identity(request: Request, call_next):
        if email := request.headers.get("x-user"):
            request.state.user = AuthenticatedUser(email=email, role="admin")
        return await call_next(request)

    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://palette.test"
        ) as client:
            yield app, client, engine
    finally:
        async with engine.begin() as conn:
            from cognis.store.palette_indexes import drop_palette_indexes

            await conn.run_sync(drop_palette_indexes)
            await conn.run_sync(Base.metadata.drop_all)
        await engine.dispose()


async def test_metadata_http_access_ranking_and_pages(palette_app):
    app, client, _ = palette_app
    assert (await client.get("/api/v1/search/palette")).status_code == 401
    seen = []
    cursor = None
    while True:
        response = await client.get(
            "/api/v1/search/palette",
            headers={"x-user": OWNER},
            params={"q": "command pallete", "limit": 7, **({"cursor": cursor} if cursor else {})},
        )
        assert response.status_code == 200, response.text
        assert response.headers["cache-control"] == "no-store"
        body = response.json()
        assert len(body["items"]) <= 7
        seen.extend((row["kind"], row["id"]) for row in body["items"])
        cursor = body["next_cursor"]
        if not cursor:
            break
    assert len(seen) == len(set(seen)) == 39
    assert {kind for kind, _ in seen} == {
        "conversation",
        "task",
        "schedule",
        "artifact",
        "deliverable",
    }
    shared = await client.get(
        "/api/v1/search/palette", headers={"x-user": READER}, params={"q": "command", "limit": 40}
    )
    assert shared.status_code == 200, shared.text
    rows = shared.json()["items"]
    assert len(rows) == 20
    assert all(row["kind"] not in ("task", "schedule") for row in rows)
    assert all(
        int(row["id"].split("-")[-1]) % 2 == 0 for row in rows if row["kind"] == "conversation"
    )
    async with app.state.session_factory() as session:
        await session.execute(
            sa.text("UPDATE project_grants SET revoked_at = :now"), {"now": datetime.now(UTC)}
        )
        await session.commit()
    revoked = await client.get(
        "/api/v1/search/palette", headers={"x-user": READER}, params={"q": "command"}
    )
    assert revoked.json()["items"] == []


async def test_index_rename_delete_cursor_binding_and_backfill(palette_app, monkeypatch):
    app, client, engine = palette_app
    async with app.state.session_factory() as session:
        await session.execute(
            sa.update(Conversation)
            .where(Conversation.conversation_id == "palette-chat-00")
            .values(title="Precisely renamed conversation")
        )
        await session.commit()
    result = await client.get(
        "/api/v1/search/palette", headers={"x-user": OWNER}, params={"q": "precisely"}
    )
    assert [row["id"] for row in result.json()["items"]] == ["palette-chat-00"]
    page = await client.get(
        "/api/v1/search/palette", headers={"x-user": OWNER}, params={"q": "command", "limit": 1}
    )
    cursor = page.json()["next_cursor"]
    for user, query in ((READER, "command"), (OWNER, "renamed")):
        response = await client.get(
            "/api/v1/search/palette",
            headers={"x-user": user},
            params={"q": query, "cursor": cursor},
        )
        assert response.status_code == 400
        assert response.json()["detail"]["code"] == "invalid_cursor"
    for malformed in ("not-a-cursor", "W10="):
        response = await client.get(
            "/api/v1/search/palette",
            headers={"x-user": OWNER},
            params={"q": "command", "cursor": malformed},
        )
        assert response.status_code == 400
        assert response.json()["detail"]["code"] == "invalid_cursor"
    # Reversible migration then bootstrap recreates the index from existing names.
    migration = importlib.import_module("cognis.store.migrations.versions.168_palette_name_indexes")
    from alembic.migration import MigrationContext
    from alembic.operations import Operations

    def roundtrip(conn):
        context = MigrationContext.configure(conn)
        with Operations.context(context), context.begin_transaction():
            migration.downgrade()
            # Qualify startup from populated pre-palette tables independently
            # of Alembic, then qualify the formal upgrade from that same schema.
            ensure_palette_indexes(conn)
            ensure_palette_indexes(conn)
            migration.downgrade()
            migration.upgrade()
            ensure_palette_indexes(conn)

    async with engine.connect() as conn:
        await conn.run_sync(roundtrip)
        await conn.commit()

    result = await client.get(
        "/api/v1/search/palette", headers={"x-user": OWNER}, params={"q": "precisely"}
    )
    assert result.json()["items"][0]["id"] == "palette-chat-00"
    import cognis.core.palette as palette

    async def internal_error(*args, **kwargs):
        raise ValueError("Internal projection failure")

    monkeypatch.setattr(palette, "search", internal_error)
    with pytest.raises(ValueError, match="Internal projection failure"):
        await client.get("/api/v1/search/palette", headers={"x-user": OWNER})
    assert app.state.palette_admission.active == set()


async def test_destination_indexes_bootstrap_migration_and_display_name_updates(palette_app):
    from alembic.migration import MigrationContext
    from alembic.operations import Operations

    from cognis.store.palette_indexes import ensure_palette_destination_indexes, name_expression

    app, _, engine = palette_app
    migration = importlib.import_module("cognis.store.migrations.versions.170_palette_destinations")

    def roundtrip(conn):
        def installed(table):
            if conn.dialect.name == "postgresql":
                return f"ix_{table}_palette_name" in {
                    row["name"] for row in sa.inspect(conn).get_indexes(table)
                }
            return bool(
                conn.scalar(
                    sa.text("SELECT 1 FROM sqlite_master WHERE name=:name"),
                    {"name": f"palette_{table}"},
                )
            )

        context = MigrationContext.configure(conn)
        with Operations.context(context), context.begin_transaction():
            migration.downgrade()
            assert not installed("agents") and not installed("projects")
            ensure_palette_destination_indexes(conn)
            ensure_palette_destination_indexes(conn)
            assert installed("agents") and installed("projects")
            migration.downgrade()
            migration.upgrade()
            assert installed("agents") and installed("projects")

    async with engine.connect() as conn:
        await conn.run_sync(roundtrip)
        await conn.commit()
    async with app.state.session_factory() as session:

        async def matches():
            if engine.dialect.name == "postgresql":
                return list(
                    (
                        await session.execute(
                            sa.text(
                                f"SELECT agent_id FROM agents WHERE palette_name_vector("
                                f"{name_expression('agent', 'name')}) @@ to_tsquery('simple', 'coffee:*')"
                            )
                        )
                    ).scalars()
                )
            return list(
                (
                    await session.execute(
                        sa.text(
                            "SELECT agents.agent_id FROM agents JOIN palette_agents "
                            "ON agents.rowid=palette_agents.rowid WHERE palette_agents MATCH 'coffee*'"
                        )
                    )
                ).scalars()
            )

        await session.execute(sa.update(Agent).values(display_name="Coffee engineer"))
        assert await matches() == ["palette-agent"]
        await session.execute(sa.update(Agent).values(display_name="Tea engineer"))
        assert await matches() == []
        await session.commit()


async def test_admission_rejects_without_queue(palette_app):
    app, client, _ = palette_app
    app.state.palette_admission.active.add(OWNER)
    busy = await client.get("/api/v1/search/palette", headers={"x-user": OWNER})
    assert busy.status_code == 429
    app.state.palette_admission.active.clear()
    assert (
        await client.get("/api/v1/search/palette", headers={"x-user": OWNER}, params={"q": "!!!"})
    ).json()["items"] == []


@pytest.mark.parametrize(
    ("title", "query", "expected"),
    [
        ("Command palette", "command pallete", True),
        ("Náklady - přístavba.xlsx", "naklady pristavba", True),
        ("Budget", "password", False),
    ],
)
def test_bounded_name_refinement(title, query, expected):
    assert name_matches(title, query) is expected


async def test_complete_title_tokens_and_accented_http_search(palette_app):
    app, client, engine = palette_app
    async with app.state.session_factory() as session:
        await session.execute(
            sa.update(Conversation)
            .where(Conversation.conversation_id == "palette-chat-00")
            .values(title="Annual planning notes for the engineering team revised budget")
        )
        from cognis.store.models import ArtifactRecordRow

        await session.execute(
            sa.update(ArtifactRecordRow)
            .where(ArtifactRecordRow.artifact_id == "palette-image")
            .values(filename="Náklady - přístavba.xlsx")
        )
        await session.execute(
            sa.delete(Conversation).where(Conversation.conversation_id == "palette-chat-03")
        )
        await session.commit()
    for query, expected in (
        ("budget", "palette-chat-00"),
        ("naklady pristavba", "palette-image"),
        ("xlsx", "palette-image"),
    ):
        response = await client.get(
            "/api/v1/search/palette", headers={"x-user": OWNER}, params={"q": query}
        )
        assert response.status_code == 200, response.text
        assert expected in {row["id"] for row in response.json()["items"]}
    if engine.dialect.name == "sqlite":
        async with engine.begin() as conn:
            await conn.execute(
                sa.text(
                    "INSERT INTO palette_conversations(palette_conversations, rank) VALUES('integrity-check', 1)"
                )
            )


async def test_sparse_candidate_pages_advance_without_skipping_matches(palette_app):
    app, client, _ = palette_app
    async with app.state.session_factory() as session:
        for index in range(8):
            await create_conversation(
                session,
                conversation_id=f"palette-distractor-{index}",
                user_email=OWNER,
                agent_id="palette-agent",
                context_type="web",
                title=f"Command palladium {index}",
            )
        await session.commit()
    cursor = None
    matches = []
    empty_pages = 0
    for _ in range(30):
        response = await client.get(
            "/api/v1/search/palette",
            headers={"x-user": OWNER},
            params={"q": "command palette", "limit": 3, **({"cursor": cursor} if cursor else {})},
        )
        assert response.status_code == 200, response.text
        page = response.json()
        if not page["items"]:
            empty_pages += 1
        matches.extend(row["id"] for row in page["items"])
        cursor = page["next_cursor"]
        if not cursor:
            break
    assert empty_pages > 0
    assert len(set(matches)) == len(matches) == 39


async def test_rate_window_global_cap_and_cancel_cleanup(monkeypatch):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, MagicMock

    import cognis.core.palette as palette

    clock = [0.0]
    monkeypatch.setattr(palette, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    fake = MagicMock()
    fake.get_bind.return_value.dialect.name = "postgresql"
    fake.execute = AsyncMock()
    operation = AsyncMock(return_value=palette.PalettePage(items=[]))
    monkeypatch.setattr(palette, "search", operation)
    guard = PaletteAdmission(per_user_rate=2, global_rate=3)
    await guard.run(fake, OWNER, "")
    await guard.run(fake, OWNER, "")
    with pytest.raises(BlockingIOError):
        await guard.run(fake, OWNER, "")
    await guard.run(fake, READER, "")
    with pytest.raises(BlockingIOError):
        await guard.run(fake, "third-user@test.local", "")
    clock[0] = 1.01
    await guard.run(fake, OWNER, "")
    assert guard.active == set()
    guard.active.update(f"active-{index}" for index in range(4))
    with pytest.raises(BlockingIOError):
        await guard.run(fake, READER, "")
    guard.active.clear()
    entered = asyncio.Event()

    async def pending(*args, **kwargs):
        entered.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(palette, "search", pending)
    task = asyncio.create_task(guard.run(fake, READER, ""))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert guard.active == set()


@pytest.mark.parametrize("outer_timeout", [False, True])
async def test_real_database_deadline_and_connection_recovery(
    palette_app, monkeypatch, outer_timeout
):
    import time

    import cognis.core.palette as palette

    # Keep the cancellation/recovery experiment fast while testing both owners.
    db_budget = palette.DB_TIMEOUT_SECONDS
    operation_budget = palette.OPERATION_TIMEOUT_SECONDS
    monkeypatch.setattr(palette, "DB_TIMEOUT_SECONDS", 0.2)
    monkeypatch.setattr(palette, "OPERATION_TIMEOUT_SECONDS", 0.35)
    app, client, engine = palette_app
    original = palette.search
    invalidations = []
    executed = asyncio.Event()

    def invalidated(*args):
        invalidations.append(True)

    sa.event.listen(engine.sync_engine, "invalidate", invalidated)

    async def slow(session, *args, **kwargs):
        if outer_timeout:
            if engine.dialect.name == "sqlite":
                connection = await session.connection()
                raw = await connection.get_raw_connection()
                await raw.driver_connection.create_function(
                    "palette_pause", 0, lambda: time.sleep(0.5)
                )
            else:
                # Begin a real query just before the outer deadline, before the
                # per-statement deadline can fire.
                await asyncio.sleep(0.3)
        statement = (
            "SELECT pg_sleep(1)"
            if engine.dialect.name == "postgresql"
            else "SELECT palette_pause()"
            if outer_timeout
            else "WITH RECURSIVE n(x) AS (VALUES(0) UNION ALL SELECT x+1 FROM n WHERE x<100000000) SELECT sum(x) FROM n"
        )
        executed.set()
        await session.execute(sa.text(statement))
        return palette.PalettePage(items=[])

    monkeypatch.setattr(palette, "search", slow)
    response = await client.get(
        "/api/v1/search/palette", headers={"x-user": OWNER}, params={"q": "command"}
    )
    assert response.status_code == 503, response.text
    assert executed.is_set()
    if outer_timeout:
        assert invalidations, "Outer cancellation must retire the in-flight connection"
    assert app.state.palette_admission.active == set()
    monkeypatch.setattr(palette, "search", original)
    monkeypatch.setattr(palette, "DB_TIMEOUT_SECONDS", db_budget)
    monkeypatch.setattr(palette, "OPERATION_TIMEOUT_SECONDS", operation_budget)
    recovered = await client.get(
        "/api/v1/search/palette", headers={"x-user": OWNER}, params={"q": "command"}
    )
    assert recovered.status_code == 200, recovered.text
    assert recovered.json()["items"]


async def test_revision_lookup_preserves_legacy_lineage_and_expired_successors(palette_app):
    from datetime import timedelta

    from cognis.store.library_queries import discoverable_content_clause
    from cognis.store.models import ArtifactRecordRow, DeliverableRow

    app, client, engine = palette_app
    async with app.state.session_factory() as session:
        await session.execute(
            sa.update(ArtifactRecordRow)
            .where(ArtifactRecordRow.artifact_id == "palette-image")
            .values(lineage_id=None)
        )
        await create_artifact_record(
            session,
            artifact_id="palette-image-revision",
            namespace="artifacts",
            object_id="palette-image-revision",
            filename="Command palette image revised.png",
            owner_email=OWNER,
            owner_agent_id="palette-agent",
            purpose="artifact_write",
            kind="image",
            mime_type="image/png",
            size_bytes=2048,
            status="attached",
            lineage_id="palette-image",
            revision=2,
            supersedes_artifact_id="palette-image",
        )
        await session.commit()

    async def artifact_ids():
        response = await client.get(
            "/api/v1/search/palette",
            headers={"x-user": OWNER},
            params={"q": "command palette", "limit": 40},
        )
        assert response.status_code == 200, response.text
        return {row["id"] for row in response.json()["items"] if row["kind"] == "artifact"}

    assert await artifact_ids() == {"palette-image-revision"}
    async with app.state.session_factory() as session:
        await session.execute(
            sa.update(ArtifactRecordRow)
            .where(ArtifactRecordRow.artifact_id == "palette-image-revision")
            .values(expires_at=datetime.now(UTC) - timedelta(days=1))
        )
        await session.commit()
    assert await artifact_ids() == {"palette-image"}

    if engine.dialect.name == "sqlite":
        async with app.state.session_factory() as session:
            connection = await session.connection()
            for model, ref, expected_index in (
                (DeliverableRow, DeliverableRow.deliverable_id, "ix_deliverables_successor_status"),
                (ArtifactRecordRow, ArtifactRecordRow.artifact_id, "ix_artifacts_lineage"),
            ):
                statement = sa.select(ref).where(discoverable_content_clause(model))
                compiled = statement.compile(
                    dialect=engine.dialect, compile_kwargs={"render_postcompile": True}
                )
                parameters = tuple(compiled.params[key] for key in compiled.positiontup)
                plan = await connection.exec_driver_sql(
                    "EXPLAIN QUERY PLAN " + str(compiled), parameters
                )
                assert any(expected_index in row[3] for row in plan), (
                    "Revision discovery must use an indexed successor lookup, not a per-row scan"
                )


async def test_agent_metadata_is_authorized_and_every_result_has_update_time(palette_app):
    app, client, _ = palette_app
    async with app.state.session_factory() as session:
        await session.execute(
            sa.update(Agent)
            .where(Agent.agent_id == "palette-agent")
            .values(display_name="Palette engineer", avatar_image_id="palette-avatar")
        )
        await session.commit()
    response = await client.get(
        "/api/v1/search/palette", headers={"x-user": OWNER}, params={"q": "command", "limit": 40}
    )
    assert response.status_code == 200, response.text
    rows = response.json()["items"]
    assert all(row["updated_at"] for row in rows)
    for row in rows:
        if row["kind"] in ("conversation", "task"):
            assert row["agent_name"] == "Palette engineer"
            assert row["agent_avatar_url"] == "/api/v1/images/palette-avatar"
    shared = await client.get(
        "/api/v1/search/palette", headers={"x-user": READER}, params={"q": "command", "limit": 40}
    )
    assert shared.status_code == 200, shared.text
    assert all(row["agent_name"] is None for row in shared.json()["items"])
    assert all(row["agent_avatar_url"] is None for row in shared.json()["items"])


async def test_latest_scheduled_execution_is_selected_before_filtering_and_pagination(palette_app):
    from datetime import timedelta

    app, client, engine = palette_app
    start = datetime(2026, 1, 1, tzinfo=UTC)
    async with app.state.session_factory() as session:
        for index in range(15):
            session.add(
                Task(
                    task_id=f"daily-execution-{index:02}",
                    title="Historic Daily brief" if index < 14 else "Daily brief renamed",
                    agent_id="palette-agent",
                    created_by=OWNER,
                    source_type="scheduler",
                    source_ref="daily-schedule",
                    status="failed" if index == 14 else "completed",
                    created_at=start + timedelta(days=index),
                    # Historical updates must not make an old execution current.
                    updated_at=start + timedelta(days=50 - index),
                )
            )
        for task_id, source_type, source_ref in (
            ("daily-manual", "api", None),
            ("daily-distinct-schedule", "scheduler", "other-daily-schedule"),
        ):
            session.add(
                Task(
                    task_id=task_id,
                    title="Daily brief",
                    agent_id="palette-agent",
                    created_by=OWNER,
                    source_type=source_type,
                    source_ref=source_ref,
                    status="completed",
                )
            )
        await session.commit()
    ids = []
    cursor = None
    while True:
        response = await client.get(
            "/api/v1/search/palette",
            headers={"x-user": OWNER},
            params={
                "q": "daily",
                "types": "task",
                "limit": 1,
                **({"cursor": cursor} if cursor else {}),
            },
        )
        assert response.status_code == 200, response.text
        page = response.json()
        ids.extend(row["id"] for row in page["items"])
        cursor = page["next_cursor"]
        if not cursor:
            break
    assert set(ids) == {"daily-execution-14", "daily-manual", "daily-distinct-schedule"}
    assert len(ids) == 3
    # The title match must not resurrect an obsolete execution.
    response = await client.get(
        "/api/v1/search/palette",
        headers={"x-user": OWNER},
        params={"q": "historic", "types": "task", "limit": 40},
    )
    assert response.status_code == 200, response.text
    assert response.json()["items"] == []
    async with engine.connect() as connection:
        indexes = await connection.run_sync(lambda conn: sa.inspect(conn).get_indexes("tasks"))
    assert "ix_tasks_palette_latest_run" in {index["name"] for index in indexes}


async def test_latest_task_probe_does_not_use_inaccessible_execution(palette_app):
    from datetime import timedelta

    app, client, _ = palette_app
    start = datetime(2026, 1, 1, tzinfo=UTC)
    async with app.state.session_factory() as session:
        for index, visibility in enumerate(("project", "private")):
            session.add(
                Task(
                    task_id=f"shared-daily-{index}",
                    title="Shared daily briefing",
                    agent_id="palette-agent",
                    created_by=OWNER,
                    source_type="scheduler",
                    source_ref="shared-daily-schedule",
                    project_id="palette-project",
                    visibility=visibility,
                    created_at=start + timedelta(days=index),
                )
            )
        await session.commit()
    response = await client.get(
        "/api/v1/search/palette",
        headers={"x-user": READER},
        params={"q": "shared daily", "types": "task"},
    )
    assert response.status_code == 200, response.text
    assert [row["id"] for row in response.json()["items"]] == ["shared-daily-0"]


async def test_type_selection_is_applied_before_limit_and_bound_to_cursor(palette_app):
    _, client, _ = palette_app
    first = await client.get(
        "/api/v1/search/palette",
        headers={"x-user": OWNER},
        params={"q": "command", "types": "conversation", "limit": 1},
    )
    assert first.status_code == 200, first.text
    assert [row["kind"] for row in first.json()["items"]] == ["conversation"]
    assert first.json()["next_cursor"]
    changed = await client.get(
        "/api/v1/search/palette",
        headers={"x-user": OWNER},
        params={"q": "command", "types": "artifact", "cursor": first.json()["next_cursor"]},
    )
    assert changed.status_code == 400
    files = await client.get(
        "/api/v1/search/palette",
        headers={"x-user": OWNER},
        params={"q": "command", "types": ["artifact", "deliverable"], "limit": 40},
    )
    assert files.status_code == 200, files.text
    assert {row["kind"] for row in files.json()["items"]} == {"artifact", "deliverable"}


async def test_production_deadlines_allow_a_moderately_slow_read(palette_app, monkeypatch):
    import cognis.core.palette as palette

    app, client, engine = palette_app
    original = palette.search

    async def delayed(session, *args, **kwargs):
        if engine.dialect.name == "postgresql":
            await session.execute(sa.text("SELECT pg_sleep(3.1)"))
        else:
            await asyncio.sleep(3.1)
        return await original(session, *args, **kwargs)

    monkeypatch.setattr(palette, "search", delayed)
    response = await client.get(
        "/api/v1/search/palette", headers={"x-user": OWNER}, params={"q": "command"}
    )
    assert response.status_code == 200, response.text
    assert response.json()["items"]


async def test_artifact_producers_and_agent_priority_preserve_relevance(palette_app):
    from cognis.store.models import ArtifactRecordRow

    app, client, _ = palette_app
    async with app.state.session_factory() as session:
        await session.execute(
            sa.update(User).where(User.email == OWNER).values(name="Palette reviewer")
        )
        for artifact_id, filename, role, purpose, updated in (
            ("produced-agent", "Production report notes", "assistant", "artifact_publish", 1),
            ("uploaded-user", "Production report", "user", "chat_input", 2),
            ("ambiguous-source", "Production report unknown", None, "artifact_write", 3),
        ):
            await create_artifact_record(
                session,
                artifact_id=artifact_id,
                namespace="artifacts",
                object_id=artifact_id,
                filename=filename,
                owner_email=OWNER,
                owner_agent_id="palette-agent",
                purpose=purpose,
                message_role=role,
                kind="file",
                mime_type="text/plain",
                size_bytes=100,
                status="attached",
            )
            await session.execute(
                sa.update(ArtifactRecordRow)
                .where(ArtifactRecordRow.artifact_id == artifact_id)
                .values(updated_at=datetime(2026, 1, updated, tzinfo=UTC))
            )
        await session.commit()
    response = await client.get(
        "/api/v1/search/palette",
        headers={"x-user": OWNER},
        params={"q": "production", "types": "artifact", "limit": 1},
    )
    assert response.status_code == 200, response.text
    assert response.json()["items"][0]["id"] == "produced-agent"
    cursor = response.json()["next_cursor"]
    rows = response.json()["items"]
    while cursor:
        response = await client.get(
            "/api/v1/search/palette",
            headers={"x-user": OWNER},
            params={"q": "production", "types": "artifact", "limit": 1, "cursor": cursor},
        )
        assert response.status_code == 200, response.text
        rows.extend(response.json()["items"])
        cursor = response.json()["next_cursor"]
    by_id = {row["id"]: row for row in rows}
    assert len(rows) == len(by_id) == 3
    assert by_id["produced-agent"]["origin"] == "agent"
    assert by_id["produced-agent"]["agent_name"] == "Palette agent"
    assert by_id["uploaded-user"]["origin"] == "user"
    assert by_id["uploaded-user"]["producer_name"] == "Palette reviewer"
    assert by_id["uploaded-user"]["agent_name"] is None
    assert by_id["uploaded-user"]["agent_avatar_url"] is None
    assert by_id["ambiguous-source"]["origin"] == "other"
    assert by_id["ambiguous-source"]["agent_name"] is None
    assert all("owner_email" not in row for row in rows)
    exact = await client.get(
        "/api/v1/search/palette",
        headers={"x-user": OWNER},
        params={"q": "Production report", "types": "artifact"},
    )
    assert exact.status_code == 200, exact.text
    assert exact.json()["items"][0]["id"] == "uploaded-user"


async def test_latest_task_index_migration_and_bootstrap_are_reversible(palette_app):
    from alembic.migration import MigrationContext
    from alembic.operations import Operations

    from cognis.store.palette_indexes import ensure_palette_task_run_index

    _, _, engine = palette_app
    migration = importlib.import_module(
        "cognis.store.migrations.versions.169_palette_latest_task_run"
    )

    def roundtrip(conn):
        def names():
            return {index["name"] for index in sa.inspect(conn).get_indexes("tasks")}

        context = MigrationContext.configure(conn)
        with Operations.context(context), context.begin_transaction():
            migration.downgrade()
            assert "ix_tasks_palette_latest_run" not in names()
            ensure_palette_task_run_index(conn)
            ensure_palette_task_run_index(conn)
            assert "ix_tasks_palette_latest_run" in names()
            migration.downgrade()
            migration.upgrade()
            assert "ix_tasks_palette_latest_run" in names()
            assert conn.scalar(sa.select(sa.func.count()).select_from(Task)) == 1

    async with engine.connect() as conn:
        await conn.run_sync(roundtrip)
        await conn.commit()


async def test_coffee_task_survives_content_floods_and_anchored_typo_candidates(palette_app):
    from cognis.store.models import ArtifactRecordRow

    app, client, _ = palette_app
    async with app.state.session_factory() as session:
        session.add(
            Task(
                task_id="coffee-replenishment",
                title="Smart coffee replenishment",
                created_by=OWNER,
                agent_id="palette-agent",
                status="completed",
                source_type="scheduler",
                source_ref="coffee-automation",
                created_at=datetime(2020, 1, 1, tzinfo=UTC),
                updated_at=datetime(2020, 1, 1, tzinfo=UTC),
            )
        )
        session.add_all(
            [
                Task(
                    task_id=f"cofactor-{i}",
                    title=f"Cofactor maintenance {i}",
                    created_by=OWNER,
                    agent_id="palette-agent",
                    status="running",
                )
                for i in range(60)
            ]
        )
        session.add_all(
            [
                Task(
                    task_id=f"coffin-{i}",
                    title=f"Coffin maintenance {i}",
                    created_by=OWNER,
                    agent_id="palette-agent",
                    status="running",
                )
                for i in range(5)
            ]
        )
        session.add_all(
            [
                ArtifactRecordRow(
                    artifact_id=f"coffee-artifact-{i:03}",
                    namespace="artifacts",
                    object_id=f"coffee-artifact-{i:03}",
                    owner_email=OWNER,
                    owner_agent_id="palette-agent",
                    message_role="assistant",
                    filename="coffee" if i < 6 else f"coffee report {i}.md",
                    purpose="artifact_publish",
                    kind="file",
                    mime_type="text/markdown",
                    status="attached",
                    size_bytes=100,
                    updated_at=datetime(2026, 1, 1, tzinfo=UTC),
                )
                for i in range(120)
            ]
        )
        await session.commit()
    response = await client.get(
        "/api/v1/search/palette",
        headers={"x-user": OWNER},
        params={"q": "coffee", "limit": 6},
    )
    assert response.status_code == 200, response.text
    ids = [row["id"] for row in response.json()["items"]]
    assert ids.index("coffee-replenishment") <= 2
    assert all(not ref.startswith("cofactor-") for ref in ids)
    # The content-head boost is computed before seek, not a per-page cutoff:
    # every matching file must remain reachable without duplicates.
    seen = []
    cursor = None
    while True:
        response = await client.get(
            "/api/v1/search/palette",
            headers={"x-user": OWNER},
            params={"q": "coffee", "limit": 20, **({"cursor": cursor} if cursor else {})},
        )
        assert response.status_code == 200, response.text
        seen.extend(row["id"] for row in response.json()["items"])
        cursor = response.json()["next_cursor"]
        if not cursor:
            break
    assert len(seen) == len(set(seen)) == 126
    assert min(seen.index(f"coffin-{i}") for i in range(5)) > max(
        seen.index(f"coffee-artifact-{i:03}") for i in range(120)
    )
    files = await client.get(
        "/api/v1/search/palette",
        headers={"x-user": OWNER},
        params={"q": "coffee", "types": "artifact", "limit": 6},
    )
    assert files.status_code == 200, files.text
    assert len(files.json()["items"]) == 6
    assert all(row["kind"] == "artifact" for row in files.json()["items"])


async def test_blank_query_with_content_has_complete_duplicate_free_continuation(palette_app):
    app, client, _ = palette_app
    async with app.state.session_factory() as session:
        await session.execute(sa.update(Agent).values(status="active"))
        await session.commit()
    seen = []
    cursor = None
    for _ in range(10):
        params = {"limit": 8}
        if cursor:
            params["cursor"] = cursor
        response = await client.get(
            "/api/v1/search/palette", params=params, headers={"x-user": OWNER}
        )
        assert response.status_code == 200, response.text
        data = response.json()
        seen.extend((item["kind"], item["id"]) for item in data["items"])
        cursor = data["next_cursor"]
        if not cursor:
            break
    assert cursor is None
    assert len(seen) == len(set(seen)) == 41
    assert ("artifact", "palette-image") in seen
    assert sum(kind == "deliverable" for kind, _ in seen) == 1


@pytest.mark.parametrize("query", ["", "c", "co"])
async def test_blank_and_short_queries_list_recent_destinations_of_every_kind(palette_app, query):
    from cognis.store.models import ArtifactRecordRow, DeliverableRow, ProjectRow

    app, client, _ = palette_app
    old = datetime(2020, 1, 1, tzinfo=UTC)
    async with app.state.session_factory() as session:
        for model in (ProjectRow, Conversation, Task, Schedule):
            await session.execute(sa.update(model).values(updated_at=old))
        await session.execute(sa.update(Agent).values(status="active", updated_at=old))
        await session.execute(
            sa.update(ArtifactRecordRow).values(updated_at=datetime(2026, 1, 3, tzinfo=UTC))
        )
        await session.execute(
            sa.update(DeliverableRow).values(updated_at=datetime(2026, 1, 2, tzinfo=UTC))
        )
        await session.execute(
            sa.update(Conversation)
            .where(Conversation.conversation_id == "palette-chat-07")
            .values(updated_at=datetime(2026, 1, 1, tzinfo=UTC))
        )
        await session.commit()
    response = await client.get(
        "/api/v1/search/palette", headers={"x-user": OWNER}, params={"q": query, "limit": 3}
    )
    assert response.status_code == 200, response.text
    assert [(row["kind"], row["id"]) for row in response.json()["items"]][:3] == [
        ("artifact", "palette-image"),
        ("deliverable", response.json()["items"][1]["id"]),
        ("conversation", "palette-chat-07"),
    ]


async def test_destination_visibility_aliases_and_canonical_routes(palette_app):
    from cognis.core.project_identity import project_color
    from cognis.store.models import AgentGrantRow, ProjectGrantRow, ProjectRow
    from cognis.store.queries import create_agent_grant

    app, client, _ = palette_app
    async with app.state.session_factory() as session:
        await session.execute(
            sa.update(Agent)
            .where(Agent.agent_id == "palette-agent")
            .values(
                display_name="Coffee engineer",
                system_prompt="PRIVATE_PROMPT",
                status="active",
            )
        )
        for agent_id, values in (
            ("palette-hidden", {"hidden": True}),
            ("palette-disabled", {"status": "disabled"}),
            ("palette-secondary", {"agent_type": "secondary"}),
            ("palette-system", {"is_system": True}),
        ):
            await create_agent(
                session, agent_id=agent_id, owner_email=OWNER, name=agent_id, status="active"
            )
            await session.execute(
                sa.update(Agent).where(Agent.agent_id == agent_id).values(**values)
            )
        await create_agent(
            session,
            agent_id="palette-foreign",
            owner_email=READER,
            name="Palette foreign",
            status="active",
        )
        await create_project(
            session, project_id="palette-foreign", owner_email=READER, name="Palette foreign"
        )
        await session.execute(
            sa.update(ProjectRow)
            .where(ProjectRow.project_id == "palette-project")
            .values(
                instructions="PRIVATE_PROJECT_INSTRUCTIONS",
                avatar_image_id="palette/project avatar",
                avatar_url="https://example.org/project-avatar.png",
                metadata_json={
                    "icon": "cognis",
                    "color": "#FBBF24",
                    "private": "PRIVATE_PROJECT_METADATA",
                },
            )
        )
        await session.commit()

    async def destinations(user, query="palette"):
        response = await client.get(
            "/api/v1/search/palette",
            headers={"x-user": user},
            params=[("q", query), ("types", "agent"), ("types", "project"), ("limit", "40")],
        )
        assert response.status_code == 200, response.text
        assert "PRIVATE_PROMPT" not in response.text
        assert "PRIVATE_PROJECT_INSTRUCTIONS" not in response.text
        assert "PRIVATE_PROJECT_METADATA" not in response.text
        return {(row["kind"], row["id"]): row for row in response.json()["items"]}

    owned = await destinations(OWNER)
    assert set(owned) == {("agent", "palette-agent"), ("project", "palette-project")}
    assert owned["agent", "palette-agent"]["title"] == "Coffee engineer"
    assert (
        owned["agent", "palette-agent"]["href"] == "/chat?agent_id=palette-agent&scope=agent_direct"
    )
    assert owned["project", "palette-project"]["href"] == "/projects/palette-project?tab=overview"
    project = owned["project", "palette-project"]
    assert project["project_avatar_url"] == "/api/v1/images/palette%2Fproject%20avatar"
    assert project["project_icon"] == "cognis"
    assert project["project_color"] == "#fbbf24"
    assert all(
        owned["agent", "palette-agent"][field] is None
        for field in ("project_avatar_url", "project_icon", "project_color")
    )
    async with app.state.session_factory() as session:
        await session.execute(
            sa.update(ProjectRow)
            .where(ProjectRow.project_id == "palette-project")
            .values(
                avatar_image_id=None,
                metadata_json={"icon": {"private": "PRIVATE_PROJECT_METADATA"}, "color": "bad"},
            )
        )
        await session.commit()
    fallback = (await destinations(OWNER))["project", "palette-project"]
    assert fallback["project_avatar_url"] == "https://example.org/project-avatar.png"
    assert fallback["project_icon"] is None
    assert fallback["project_color"] == project_color("palette-project")
    assert ("agent", "palette-agent") in await destinations(OWNER, "coffee")
    # A project grant does not grant a private/direct main-agent conversation.
    shared = await destinations(READER)
    assert ("agent", "palette-agent") not in shared
    assert (
        shared["project", "palette-project"]["project_avatar_url"] == fallback["project_avatar_url"]
    )
    async with app.state.session_factory() as session:
        await create_agent_grant(
            session,
            agent_id="palette-agent",
            grantee_user_email=READER,
            granted_by=OWNER,
            executor_scope="owner_executor",
        )
        await session.commit()
    assert ("agent", "palette-agent") in await destinations(READER)
    async with app.state.session_factory() as session:
        await session.execute(sa.update(AgentGrantRow).values(revoked_at=datetime.now(UTC)))
        await session.execute(
            sa.update(ProjectGrantRow)
            .where(ProjectGrantRow.grantee_user_email == READER)
            .values(revoked_at=datetime.now(UTC))
        )
        await session.commit()
    assert set(await destinations(READER)) == {
        ("agent", "palette-foreign"),
        ("project", "palette-foreign"),
    }
