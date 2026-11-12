"""Deliverable revision lineage: write rules, access, discovery, API, and Work grouping."""

from __future__ import annotations

import importlib
import os
import uuid
from types import SimpleNamespace

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from fastapi import FastAPI
from sqlalchemy import inspect, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import create_async_engine
from starlette.testclient import TestClient

from cognis.api.authentication import AuthenticatedUser
from cognis.api.chat_v2.schemas import AssistantDeliverableTimelineItem, TimelineScope
from cognis.api.chat_v2.work_projection import build_work_projection
from cognis.api.routes import deliverables as deliverable_routes
from cognis.bootstrap import _ensure_deliverable_revision_columns
from cognis.core.content_refs import authorize_supersedes_target
from cognis.core.deliverable_authoring import (
    DeliverableAuthoringError,
    resolve_deliverable_authoring,
)
from cognis.models.deliverable import DeliverableRevisionError
from cognis.store.deliverable_storage import hydrate_deliverable_payload
from cognis.store.models import ArtifactRecordRow, DeliverableRow
from cognis.store.queries import (
    _is_live_supersedes_conflict,
    create_conversation,
    create_deliverable,
    get_deliverable,
    get_deliverable_lineage_summary,
    list_recent_artifact_records,
    update_deliverable_status,
)
from cognis.tools.builtin.workflow import WRITE_DELIVERABLE_TOOL

OWNER = "owner@example.com"


def _dlv() -> str:
    return f"dlv_{uuid.uuid4().hex}"


async def _chat_conversation(factory, conversation_id: str = "conv-chat") -> None:
    async with factory() as session:
        await create_conversation(
            session, OWNER, "agent-owner", "agent_direct", conversation_id=conversation_id
        )
        await session.commit()


async def _write_chat(
    factory,
    *,
    content: str,
    title: str | None = "Report",
    supersedes: str | None = None,
    turn_id: str | None = None,
    conversation_id: str = "conv-chat",
) -> DeliverableRow:
    async with factory() as session:
        if supersedes is not None:
            await authorize_supersedes_target(
                session,
                supersedes,
                user_email=OWNER,
                accessor_conversation_id=conversation_id,
                accessor_agent_id="agent-owner",
            )
        row = await create_deliverable(
            session,
            deliverable_id=_dlv(),
            conversation_id=conversation_id,
            turn_id=turn_id or uuid.uuid4().hex,
            content=content,
            title=title,
            artifact_store=factory.artifact_store,
            published_owner_email=OWNER,
            supersedes_deliverable_id=supersedes,
        )
        await session.commit()
        return row


async def test_direct_chat_revision_keeps_history_and_hides_old_from_discovery(
    task_continuation_db,
) -> None:
    factory = task_continuation_db
    await _chat_conversation(factory)
    first = await _write_chat(factory, content="# Draft one")
    second = await _write_chat(
        factory, content="# Draft two", title=None, supersedes=first.deliverable_id
    )

    assert first.lineage_id == first.deliverable_id
    assert (first.revision, second.revision) == (1, 2)
    assert second.lineage_id == first.deliverable_id
    assert second.supersedes_deliverable_id == first.deliverable_id
    assert second.title == "Report"

    async with factory() as session:
        old = await get_deliverable(session, first.deliverable_id)
        assert old is not None and old.status == "buffered"
        await hydrate_deliverable_payload(old, factory.artifact_store)
        assert old.content == "# Draft one"
        old_record = await session.get(ArtifactRecordRow, first.deliverable_id)
        assert old_record is not None and old_record.status != "deleted"

        listed = await list_recent_artifact_records(
            session, owner_email=OWNER, purpose="conversation_deliverable", limit=20
        )
        listed_ids = {record.artifact_id for record in listed}
        assert second.deliverable_id in listed_ids
        assert first.deliverable_id not in listed_ids

        summary = await get_deliverable_lineage_summary(session, old)
        assert summary.latest_deliverable_id == second.deliverable_id
        assert (summary.latest_revision, summary.revision_count) == (2, 2)


async def test_superseding_old_revision_reports_latest(task_continuation_db) -> None:
    factory = task_continuation_db
    await _chat_conversation(factory)
    first = await _write_chat(factory, content="one")
    second = await _write_chat(factory, content="two", supersedes=first.deliverable_id)

    with pytest.raises(DeliverableRevisionError) as caught:
        await _write_chat(factory, content="three", supersedes=first.deliverable_id)

    assert caught.value.code == "supersedes_not_latest"
    assert caught.value.latest_deliverable_id == second.deliverable_id
    assert caught.value.latest_revision == 2
    assert caught.value.to_tool_result()["path"] == "$.supersedes"


async def test_rejected_revision_releases_previous_head(task_continuation_db) -> None:
    factory = task_continuation_db
    await _chat_conversation(factory)
    first = await _write_chat(factory, content="one")
    second = await _write_chat(factory, content="two", supersedes=first.deliverable_id)
    async with factory() as session:
        await update_deliverable_status(session, second.deliverable_id, status="rejected")
        await session.commit()

    with pytest.raises(DeliverableRevisionError) as caught:
        await _write_chat(factory, content="again", supersedes=second.deliverable_id)
    assert caught.value.code == "supersedes_not_active"
    assert caught.value.latest_deliverable_id == first.deliverable_id

    third = await _write_chat(factory, content="three", supersedes=first.deliverable_id)
    assert third.revision == 2


async def test_live_supersedes_index_rejects_concurrent_successor(task_continuation_db) -> None:
    factory = task_continuation_db
    await _chat_conversation(factory)
    first = await _write_chat(factory, content="one")
    await _write_chat(factory, content="two", supersedes=first.deliverable_id)

    async with factory() as session:
        source = await get_deliverable(session, first.deliverable_id)
        assert source is not None
        duplicate = DeliverableRow(
            deliverable_id=_dlv(),
            conversation_id="conv-chat",
            turn_id="racing-turn",
            version=1,
            lineage_id=first.deliverable_id,
            revision=2,
            supersedes_deliverable_id=first.deliverable_id,
            storage_object_id="racing",
            content_mime="text/markdown",
            content_hash="hash",
            status="buffered",
        )
        session.add(duplicate)
        with pytest.raises(IntegrityError) as caught:
            await session.flush()
    assert _is_live_supersedes_conflict(caught.value)


async def test_step_drafts_inherit_revision_and_target_payload_survives(
    task_continuation_db,
) -> None:
    factory = task_continuation_db
    await _chat_conversation(factory)
    chat = await _write_chat(factory, content="chat original")

    async with factory() as session:
        draft_one = await create_deliverable(
            session,
            step_run_id="sr-owner",
            content="step revision draft",
            artifact_store=factory.artifact_store,
            supersedes_deliverable_id=chat.deliverable_id,
        )
        await session.commit()
    assert (draft_one.lineage_id, draft_one.revision) == (chat.deliverable_id, 2)

    async with factory() as session:
        # The same step has no exemption from the explicit head contract.
        with pytest.raises(DeliverableRevisionError) as missing:
            await create_deliverable(
                session,
                step_run_id="sr-owner",
                content="without a base revision",
                artifact_store=factory.artifact_store,
            )
        assert missing.value.code == "supersedes_required"
        assert missing.value.latest_deliverable_id == draft_one.deliverable_id
        with pytest.raises(DeliverableRevisionError) as stale:
            await create_deliverable(
                session,
                step_run_id="sr-owner",
                content="stale",
                artifact_store=factory.artifact_store,
                supersedes_deliverable_id=chat.deliverable_id,
            )
        assert stale.value.latest_deliverable_id == draft_one.deliverable_id
        await session.rollback()
    async with factory() as session:
        draft_two = await create_deliverable(
            session,
            step_run_id="sr-owner",
            content="step revision final",
            artifact_store=factory.artifact_store,
            supersedes_deliverable_id=draft_one.deliverable_id,
        )
        draft_three = await create_deliverable(
            session,
            step_run_id="sr-owner",
            content="step revision final 2",
            artifact_store=factory.artifact_store,
            supersedes_deliverable_id=draft_two.deliverable_id,
        )
        await session.commit()

    assert (draft_two.revision, draft_three.revision) == (3, 4)
    assert draft_three.lineage_id == chat.deliverable_id
    assert draft_three.supersedes_deliverable_id == draft_two.deliverable_id
    async with factory() as session:
        replaced = await get_deliverable(session, draft_one.deliverable_id)
        original = await get_deliverable(session, chat.deliverable_id)
        assert replaced is not None and replaced.status == "buffered"
        assert original is not None and original.status == "buffered"
        await hydrate_deliverable_payload(original, factory.artifact_store)
        assert original.content == "chat original"
        summary = await get_deliverable_lineage_summary(session, original)
    assert summary.latest_deliverable_id == draft_three.deliverable_id
    assert summary.revision_count == 4


async def test_step_draft_without_supersedes_starts_and_keeps_own_lineage(
    task_continuation_db,
) -> None:
    factory = task_continuation_db
    async with factory() as session:
        first = await get_deliverable(session, "dlv_owner")
        replacement = await create_deliverable(
            session,
            step_run_id="sr-owner",
            content="rewrite",
            artifact_store=factory.artifact_store,
        )
        await session.commit()
    assert first is not None
    assert replacement.lineage_id == first.lineage_id == "dlv_owner"
    assert replacement.revision == 1
    assert replacement.supersedes_deliverable_id is None


async def test_supersedes_target_authorization(task_continuation_db) -> None:
    factory = task_continuation_db
    await _chat_conversation(factory)
    async with factory() as session:
        await create_conversation(
            session, OWNER, "agent-owner", "agent_direct", conversation_id="conv-unpublished"
        )
        unpublished = await create_deliverable(
            session,
            deliverable_id=_dlv(),
            conversation_id="conv-unpublished",
            turn_id="turn",
            content="private",
            artifact_store=factory.artifact_store,
        )
        await session.commit()

    async with factory() as session:
        task_target = await authorize_supersedes_target(
            session,
            "dlv_owner",
            user_email=OWNER,
            accessor_conversation_id="conv-chat",
            accessor_agent_id="agent-owner",
        )
        assert task_target.deliverable_id == "dlv_owner"
        # Agent ownership intentionally spans conversations, including unpublished outputs.
        own_target = await authorize_supersedes_target(
            session,
            unpublished.deliverable_id,
            user_email=OWNER,
            accessor_conversation_id="conv-chat",
            accessor_agent_id="agent-owner",
        )
        assert own_target.deliverable_id == unpublished.deliverable_id
        for target in ("dlv_other", _dlv()):
            with pytest.raises(DeliverableRevisionError) as caught:
                await authorize_supersedes_target(
                    session,
                    target,
                    user_email=OWNER,
                    accessor_conversation_id="conv-chat",
                    accessor_agent_id="agent-owner",
                )
            assert caught.value.code == "supersedes_not_found"


async def test_chat_write_can_supersede_task_output(task_continuation_db) -> None:
    factory = task_continuation_db
    await _chat_conversation(factory)
    revision = await _write_chat(factory, content="chat edit", supersedes="dlv_owner")
    assert (revision.lineage_id, revision.revision) == ("dlv_owner", 2)
    assert revision.title == "Report"


async def test_authoring_carries_validated_supersedes() -> None:
    target = _dlv()
    resolved = await resolve_deliverable_authoring(
        {"content": "body", "supersedes": target},
        session_factory=None,
        artifact_store=None,
        owner_email=OWNER,
        conversation_id="conv",
        agent_id="agent",
    )
    assert resolved.supersedes == target
    with pytest.raises(DeliverableAuthoringError) as caught:
        await resolve_deliverable_authoring(
            {"content": "body", "supersedes": "art_123"},
            session_factory=None,
            artifact_store=None,
            owner_email=OWNER,
            conversation_id="conv",
            agent_id="agent",
        )
    assert caught.value.path == "$.supersedes"


def test_tool_schema_exposes_supersedes_on_every_operation() -> None:
    assert "supersedes" in WRITE_DELIVERABLE_TOOL.provider_exposure_schema["properties"]
    for operation in WRITE_DELIVERABLE_TOOL.native_operations:
        assert "supersedes" in operation.input_schema["properties"], operation.operation


def _client(factory, monkeypatch: pytest.MonkeyPatch, *, email: str) -> TestClient:
    app = FastAPI()
    app.include_router(deliverable_routes.router)
    app.state.session_factory = factory
    app.state.artifact_store = factory.artifact_store
    app.state.config = SimpleNamespace(deliverable_share_link_ttl_seconds=604800)
    user = AuthenticatedUser(email=email, role="user")
    monkeypatch.setattr(deliverable_routes, "require_current_user", lambda _request: user)
    return TestClient(app)


async def test_detail_and_revisions_api(task_continuation_db, monkeypatch) -> None:
    factory = task_continuation_db
    await _chat_conversation(factory)
    first = await _write_chat(factory, content="one")
    second = await _write_chat(factory, content="two", supersedes=first.deliverable_id)

    with _client(factory, monkeypatch, email=OWNER) as client:
        old = client.get(f"/api/v1/deliverables/{first.deliverable_id}").json()
        latest = client.get(f"/api/v1/deliverables/{second.deliverable_id}").json()
        revisions = client.get(f"/api/v1/deliverables/{first.deliverable_id}/revisions").json()

    assert old["is_latest"] is False
    assert old["latest_deliverable_id"] == second.deliverable_id
    assert (old["revision"], old["revision_count"], old["latest_revision"]) == (1, 2, 2)
    assert latest["is_latest"] is True
    assert revisions["latest_deliverable_id"] == second.deliverable_id
    assert [item["deliverable_id"] for item in revisions["revisions"]] == [
        second.deliverable_id,
        first.deliverable_id,
    ]
    assert [item["is_latest"] for item in revisions["revisions"]] == [True, False]

    with _client(factory, monkeypatch, email="other@example.com") as client:
        denied = client.get(f"/api/v1/deliverables/{first.deliverable_id}/revisions")
    assert denied.status_code == 404


def _deliverable_item(deliverable_id: str, lineage_id: str, revision: int, seq: int):
    return AssistantDeliverableTimelineItem(
        id=f"assistant-deliverable:{deliverable_id}",
        sort_key=f"{seq:08d}",
        deliverable_id=deliverable_id,
        lineage_id=lineage_id,
        revision=revision,
        format="markdown",
        title="Report",
        content=f"revision {revision}",
    )


@pytest.mark.parametrize("newest_first", [False, True])
def test_work_projection_shows_latest_revision_only(newest_first: bool) -> None:
    items = [
        _deliverable_item("dlv_a1", "dlv_a1", 1, 1),
        _deliverable_item("dlv_b1", "dlv_b1", 1, 2),
        _deliverable_item("dlv_a2", "dlv_a1", 2, 3),
        # Legacy event without lineage metadata stays its own deliverable.
        AssistantDeliverableTimelineItem(
            id="assistant-deliverable:dlv_legacy",
            sort_key="00000004",
            deliverable_id="dlv_legacy",
            format="markdown",
        ),
    ]
    projection = build_work_projection(
        scope=TimelineScope(key="session:s", kind="session", session_id="s"),
        projection_version="v",
        items=items,
        tool_definitions={},
        has_more_before=False,
        before_cursor=None,
        server_time="2026-10-01T00:00:00Z",
        newest_first=newest_first,
    )
    ids = sorted(item.deliverable_id for item in projection.deliverables)
    assert ids == ["dlv_a2", "dlv_b1", "dlv_legacy"]
    latest = next(item for item in projection.deliverables if item.deliverable_id == "dlv_a2")
    assert (latest.lineage_id, latest.revision) == ("dlv_a1", 2)


@pytest.mark.parametrize("upgrade_path", ["bootstrap", "alembic"])
async def test_deliverable_revision_schema_upgrade(tmp_path, upgrade_path) -> None:
    url = os.environ.get("COGNIS_TEST_POSTGRES_URL")
    schema = f"dlv_revisions_{uuid.uuid4().hex}"
    admin = None
    if url:
        admin = create_async_engine(url)
        async with admin.begin() as conn:
            await conn.execute(text(f'CREATE SCHEMA "{schema}"'))
        engine = create_async_engine(url, connect_args={"server_settings": {"search_path": schema}})
    else:
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/schema.db")

    def exercise(conn) -> None:
        conn.execute(
            text(
                "CREATE TABLE deliverables (deliverable_id VARCHAR PRIMARY KEY, "
                "status VARCHAR NOT NULL)"
            )
        )
        conn.execute(text("INSERT INTO deliverables VALUES ('dlv_legacy', 'approved')"))
        migration = importlib.import_module(
            "cognis.store.migrations.versions.154_deliverable_revisions"
        )
        if upgrade_path == "bootstrap":
            _ensure_deliverable_revision_columns(conn)
            _ensure_deliverable_revision_columns(conn)
        else:
            with Operations.context(MigrationContext.configure(conn)):
                migration.upgrade()
                migration.upgrade()
        columns = {column["name"] for column in inspect(conn).get_columns("deliverables")}
        assert {"lineage_id", "revision", "supersedes_deliverable_id"} <= columns
        assert conn.execute(
            text("SELECT lineage_id, revision FROM deliverables WHERE deliverable_id='dlv_legacy'")
        ).one() == ("dlv_legacy", 1)
        indexes = {index["name"] for index in inspect(conn).get_indexes("deliverables")}
        assert {"ix_deliverables_lineage", "uq_deliverables_live_supersedes"} <= indexes
        conn.execute(
            text(
                "INSERT INTO deliverables (deliverable_id, status, supersedes_deliverable_id) "
                "VALUES ('dlv_r1', 'superseded', 'dlv_legacy'), "
                "('dlv_r2', 'rejected', 'dlv_legacy'), ('dlv_r3', 'buffered', 'dlv_legacy')"
            )
        )
        with pytest.raises(IntegrityError), conn.begin_nested():
            conn.execute(
                text(
                    "INSERT INTO deliverables (deliverable_id, status, "
                    "supersedes_deliverable_id) VALUES ('dlv_r4', 'approved', 'dlv_legacy')"
                )
            )
        if upgrade_path == "alembic":
            with Operations.context(MigrationContext.configure(conn)):
                migration.downgrade()
            columns = {column["name"] for column in inspect(conn).get_columns("deliverables")}
            assert columns == {"deliverable_id", "status"}

    try:
        async with engine.begin() as conn:
            await conn.run_sync(exercise)
    finally:
        await engine.dispose()
        if admin is not None:
            async with admin.begin() as conn:
                await conn.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
            await admin.dispose()


async def test_lineage_rows_are_queryable_by_head(task_continuation_db) -> None:
    factory = task_continuation_db
    await _chat_conversation(factory)
    first = await _write_chat(factory, content="one")
    second = await _write_chat(factory, content="two", supersedes=first.deliverable_id)
    third = await _write_chat(factory, content="three", supersedes=second.deliverable_id)
    async with factory() as session:
        revisions = list(
            (
                await session.scalars(
                    select(DeliverableRow.revision)
                    .where(DeliverableRow.lineage_id == first.deliverable_id)
                    .order_by(DeliverableRow.revision)
                )
            ).all()
        )
    assert revisions == [1, 2, 3]
    assert third.supersedes_deliverable_id == second.deliverable_id


async def test_step_rewrite_cannot_destroy_draft_revised_elsewhere(task_continuation_db) -> None:
    factory = task_continuation_db
    await _chat_conversation(factory)
    # The step's buffered draft dlv_owner is revised from chat.
    chat_revision = await _write_chat(factory, content="chat edit", supersedes="dlv_owner")

    async with factory() as session:
        with pytest.raises(DeliverableRevisionError) as caught:
            await create_deliverable(
                session,
                step_run_id="sr-owner",
                content="step rewrite",
                artifact_store=factory.artifact_store,
            )
        await session.rollback()
    assert caught.value.code == "supersedes_not_latest"
    assert caught.value.latest_deliverable_id == chat_revision.deliverable_id

    async with factory() as session:
        step_revision = await create_deliverable(
            session,
            step_run_id="sr-owner",
            content="step revision 3",
            artifact_store=factory.artifact_store,
            supersedes_deliverable_id=chat_revision.deliverable_id,
        )
        await session.commit()
    assert (step_revision.lineage_id, step_revision.revision) == ("dlv_owner", 3)

    async with factory() as session:
        ancestor = await get_deliverable(session, "dlv_owner")
        assert ancestor is not None and ancestor.status == "buffered"
        await hydrate_deliverable_payload(ancestor, factory.artifact_store)
        assert ancestor.content.startswith("# Full report")


async def test_concurrent_loser_learns_latest_revision(task_continuation_db, monkeypatch) -> None:
    from cognis.store import queries

    factory = task_continuation_db
    await _chat_conversation(factory)
    first = await _write_chat(factory, content="one")
    original_resolve = queries._resolve_new_deliverable_lineage
    async with factory() as session:
        stale = await original_resolve(
            session,
            deliverable_id=_dlv(),
            supersedes_deliverable_id=first.deliverable_id,
            replaced_drafts=[],
        )
    winner = await _write_chat(factory, content="winner", supersedes=first.deliverable_id)

    async def stale_resolve(*_args, **_kwargs):
        # Simulate a writer that passed the pre-check before the winner committed.
        return stale

    monkeypatch.setattr(queries, "_resolve_new_deliverable_lineage", stale_resolve)
    async with factory() as session:
        with pytest.raises(DeliverableRevisionError) as caught:
            await create_deliverable(
                session,
                deliverable_id=_dlv(),
                conversation_id="conv-chat",
                turn_id="loser",
                content="loser",
                artifact_store=factory.artifact_store,
                published_owner_email=OWNER,
                supersedes_deliverable_id=first.deliverable_id,
            )
        assert caught.value.code == "supersedes_not_latest"
        completed = await queries.complete_revision_conflict(session, caught.value)
    assert completed.latest_deliverable_id == winner.deliverable_id
    assert completed.latest_revision == 2


@pytest.mark.skipif(
    not os.environ.get("COGNIS_TEST_POSTGRES_URL"), reason="needs COGNIS_TEST_POSTGRES_URL"
)
@pytest.mark.parametrize("first_writer", ["step_rewrite", "chat_revision"])
async def test_concurrent_draft_rewrite_and_revision_serialize_on_postgres(
    tmp_path, monkeypatch, first_writer: str
) -> None:
    import asyncio

    from cognis.artifacts.store import ArtifactStore, ArtifactStoreConfig
    from cognis.store import queries
    from cognis.store.database import create_session_factory
    from cognis.store.models import Agent, Base, User

    url = os.environ["COGNIS_TEST_POSTGRES_URL"]
    schema = f"dlv_race_{uuid.uuid4().hex}"
    admin = create_async_engine(url)
    async with admin.begin() as conn:
        await conn.execute(text(f'CREATE SCHEMA "{schema}"'))
    engine = create_async_engine(url, connect_args={"server_settings": {"search_path": schema}})
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        factory = create_session_factory(engine)
        store = ArtifactStore(
            ArtifactStoreConfig(
                path=str(tmp_path / "artifacts"), base_url="http://t", signing_secret="s"
            )
        )
        async with factory() as session:
            session.add(User(email=OWNER, name="Owner", role="user"))
            await session.flush()
            session.add(Agent(agent_id="agent-owner", owner_email=OWNER, name="Agent"))
            await session.flush()
            await queries.create_task(
                session, task_id="task", created_by=OWNER, agent_id="agent-owner", title="T"
            )
            await queries.create_step_run(
                session,
                step_run_id="sr",
                task_id="task",
                step_name="execute",
                step_type="direct",
                agent_id="agent-owner",
            )
            await create_conversation(
                session, OWNER, "agent-owner", "agent_direct", conversation_id="conv"
            )
            draft = await create_deliverable(
                session, step_run_id="sr", content="draft", artifact_store=store
            )
            await session.commit()

        pause, resume = asyncio.Event(), asyncio.Event()
        original_store = queries.store_deliverable_payload

        async def paused_store(*args, **kwargs):
            if kwargs.get("content") == "first":
                pause.set()
                await resume.wait()
            return await original_store(*args, **kwargs)

        monkeypatch.setattr(queries, "store_deliverable_payload", paused_store)

        async def write(kind: str, content: str):
            async with factory() as session:
                try:
                    if kind == "step_rewrite":
                        row = await create_deliverable(
                            session, step_run_id="sr", content=content, artifact_store=store
                        )
                    else:
                        row = await create_deliverable(
                            session,
                            conversation_id="conv",
                            turn_id=uuid.uuid4().hex,
                            content=content,
                            artifact_store=store,
                            supersedes_deliverable_id=draft.deliverable_id,
                        )
                    await session.commit()
                    return row
                except DeliverableRevisionError as exc:
                    return await queries.complete_revision_conflict(session, exc)

        second_writer = "chat_revision" if first_writer == "step_rewrite" else "step_rewrite"
        first = asyncio.create_task(write(first_writer, "first"))
        await asyncio.wait_for(pause.wait(), 10)
        second = asyncio.create_task(write(second_writer, "second"))
        await asyncio.sleep(0.5)
        # The second writer waits on the row lock held by the paused first writer.
        assert not second.done()
        resume.set()
        first_result, second_result = await asyncio.wait_for(asyncio.gather(first, second), 20)

        assert isinstance(first_result, DeliverableRow)
        assert isinstance(second_result, DeliverableRevisionError)
        async with factory() as session:
            rows = {
                row.deliverable_id: row
                for row in (await session.scalars(select(DeliverableRow))).all()
            }
        if first_writer == "chat_revision":
            # The revised draft keeps its status and payload.
            assert rows[draft.deliverable_id].status == "buffered"
            await store.async_load("deliverables", draft.deliverable_id, "content.md")
            assert second_result.latest_deliverable_id == first_result.deliverable_id
        else:
            assert rows[draft.deliverable_id].status == "superseded"
            assert second_result.code == "supersedes_not_active"
            assert second_result.latest_deliverable_id == first_result.deliverable_id
    finally:
        await engine.dispose()
        async with admin.begin() as conn:
            await conn.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        await admin.dispose()


async def test_rejected_intermediate_revision_does_not_reopen_older_revision(
    task_continuation_db,
) -> None:
    factory = task_continuation_db
    await _chat_conversation(factory)
    first = await _write_chat(factory, content="one")
    second = await _write_chat(factory, content="two", supersedes=first.deliverable_id)
    third = await _write_chat(factory, content="three", supersedes=second.deliverable_id)
    async with factory() as session:
        await update_deliverable_status(session, second.deliverable_id, status="rejected")
        await session.commit()
    with pytest.raises(DeliverableRevisionError) as caught:
        await _write_chat(factory, content="fork", supersedes=first.deliverable_id)
    assert caught.value.code == "supersedes_not_latest"
    assert caught.value.latest_deliverable_id == third.deliverable_id


async def test_public_link_always_serves_the_latest_revision(
    task_continuation_db, monkeypatch
) -> None:
    """Outside viewers never see an older revision, even through a link made from one."""
    from cognis.core import deliverable_links

    factory = task_continuation_db
    await _chat_conversation(factory)
    first = await _write_chat(factory, content="# Outdated body")
    share = deliverable_links.signed_deliverable_view_link(
        factory.artifact_store, first.deliverable_id, base_url="http://testserver", ttl_seconds=60
    )
    token = share.url.rstrip("/").split("/")[-1]
    second = await _write_chat(factory, content="# Current body", supersedes=first.deliverable_id)

    with _client(factory, monkeypatch, email="stranger@example.com") as client:
        markdown = client.get(f"/api/v1/deliverables/share/{token}/download.md")
    assert markdown.status_code == 200
    assert "Current body" in markdown.text
    assert "Outdated body" not in markdown.text

    # A rejected newest revision is not published; the link falls back to the live head.
    async with factory() as session:
        await update_deliverable_status(session, second.deliverable_id, status="rejected")
        await session.commit()
    with _client(factory, monkeypatch, email="stranger@example.com") as client:
        markdown = client.get(f"/api/v1/deliverables/share/{token}/download.md")
    assert markdown.status_code == 200
    assert "Outdated body" in markdown.text
