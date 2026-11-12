"""Artifact revisions, project-scoped documents, and project-aware discovery."""

from __future__ import annotations

import json
import os
import uuid
from io import BytesIO
from pathlib import Path
from typing import Any

import pytest
from PIL import Image
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import create_async_engine

from cognis.artifacts.store import ArtifactStore, ArtifactStoreConfig
from cognis.bootstrap import run_schema_bootstrap
from cognis.core.artifact_revisions import ContentActor, write_artifact_document
from cognis.core.content_projects import content_project_ids
from cognis.models.deliverable import ArtifactRevisionError
from cognis.store.content_revision_schema import ensure_content_revision_schema
from cognis.store.database import create_engine, create_session_factory
from cognis.store.models import ArtifactRecordRow
from cognis.store.project_queries import upsert_project_agent
from cognis.store.queries import (
    create_agent,
    create_conversation,
    create_project,
    create_project_grant,
    create_user,
)
from cognis.tools.builtin.artifact_tools import handle_artifact_tool
from cognis.tools.builtin.workflow import WRITE_DELIVERABLE_TOOL
from cognis.tools.introspection import validate_available_tool_call_with_context
from cognis.tools.native_validation import NativeValidationContext

OWNER = "owner@example.com"
WIFE = "wife@example.com"
VIEWER = "viewer@example.com"
OUTSIDER = "outsider@example.com"


async def _seed(session: Any) -> None:
    for email in (OWNER, WIFE, VIEWER, OUTSIDER):
        await create_user(session, email=email, name=email.split("@")[0], password_hash="x")
    for agent_id, owner in (
        ("butler", OWNER),
        ("private-bot", OWNER),
        ("outsider-bot", OUTSIDER),
    ):
        await create_agent(
            session, agent_id=agent_id, owner_email=owner, name=agent_id, status="active"
        )
    await create_project(session, owner_email=OWNER, name="Home", project_id="proj_home")
    await create_project(session, owner_email=OWNER, name="Work", project_id="proj_work")
    for grantee, permission in ((WIFE, "member"), (VIEWER, "viewer")):
        await create_project_grant(
            session,
            project_id="proj_home",
            granted_by=OWNER,
            grantee_user_email=grantee,
            permission=permission,
        )
    await upsert_project_agent(
        session, project_id="proj_home", agent_id="butler", added_by=OWNER, is_default=True
    )
    await upsert_project_agent(
        session, project_id="proj_work", agent_id="butler", added_by=OWNER, is_default=False
    )
    conversations = (
        ("conv_shared", OWNER, "butler", "proj_home", "project"),
        ("conv_private", OWNER, "butler", "proj_home", "private"),
        ("conv_wife", WIFE, "butler", "proj_home", "project"),
        ("conv_work", OWNER, "butler", "proj_work", "project"),
        ("conv_plain", OWNER, "butler", None, "private"),
        ("conv_bot", OWNER, "private-bot", None, "private"),
        ("conv_outsider", OUTSIDER, "outsider-bot", None, "private"),
    )
    for conversation_id, user, agent, project_id, visibility in conversations:
        await create_conversation(
            session,
            user_email=user,
            agent_id=agent,
            context_type="web",
            conversation_id=conversation_id,
            project_id=project_id,
            visibility=visibility,
        )


@pytest.fixture
async def env(tmp_path: Path):
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path}/revisions.db")
    await run_schema_bootstrap(engine)
    factory = create_session_factory(engine)
    store = ArtifactStore(ArtifactStoreConfig(path=str(tmp_path / "artifacts")))
    async with factory() as session:
        await _seed(session)
        await session.commit()
    try:
        yield factory, store
    finally:
        await engine.dispose()


def _actor(user: str, agent: str, conversation: str) -> ContentActor:
    return ContentActor(
        user_email=user, agent_id=agent, conversation_id=conversation, session_id=None
    )


async def _tool(factory, store, name: str, args: dict, *, user: str, agent: str, conv: str):
    return await handle_artifact_tool(
        name,
        args,
        llm=None,
        artifact_store=store,
        session_factory=factory,
        user_email=user,
        runtime_metadata={"conversation_id": conv, "agent_id": agent},
    )


async def _write(factory, store, *, user=OWNER, agent="butler", conv="conv_plain", **kw):
    result = await _tool(factory, store, "artifact_write", kw, user=user, agent=agent, conv=conv)
    return result, (json.loads(result.output) if result.output.startswith("{") else None)


async def _artifact_count(factory) -> int:
    async with factory() as session:
        return int(await session.scalar(select(func.count()).select_from(ArtifactRecordRow)))


def _stored_objects(store_root: Path) -> list[Path]:
    return [p for p in store_root.rglob("*") if p.is_file()]


async def test_write_read_revise_and_history(env) -> None:
    factory, store = env
    result, first = await _write(factory, store, filename="checkpoint.json", content='{"seen": []}')
    assert not result.is_error, result.output
    assert result.attachments in (None, [])  # silent: never presented
    assert (first["revision"], first["lineage_id"]) == (1, first["artifact_id"])

    _, second = await _write(
        factory,
        store,
        filename="checkpoint.json",
        content='{"seen": ["a"]}',
        supersedes=first["artifact_id"],
    )
    assert (second["revision"], second["lineage_id"]) == (2, first["artifact_id"])

    # Exact reference keeps its content; latest follows the stable ID.
    exact = await _tool(
        factory,
        store,
        "artifact_get_content",
        {"artifact_id": first["artifact_id"]},
        user=OWNER,
        agent="butler",
        conv="conv_plain",
    )
    exact_payload = json.loads(exact.output)
    assert exact_payload["content"] == {"seen": []}
    assert exact_payload["is_latest"] is False
    assert exact_payload["latest_artifact_id"] == second["artifact_id"]

    latest = await _tool(
        factory,
        store,
        "artifact_get_content",
        {"artifact_id": first["artifact_id"], "latest": True},
        user=OWNER,
        agent="butler",
        conv="conv_plain",
    )
    latest_payload = json.loads(latest.output)
    assert latest_payload["artifact_id"] == second["artifact_id"]
    assert latest_payload["content"] == {"seen": ["a"]}
    assert latest_payload["is_latest"] is True

    metadata = await _tool(
        factory,
        store,
        "artifact_get_metadata",
        {"artifact_id": second["artifact_id"]},
        user=OWNER,
        agent="butler",
        conv="conv_plain",
    )
    revisions = metadata.metadata["revisions"]
    assert [item["revision"] for item in revisions] == [2, 1]

    # Discovery returns only the latest revision.
    listed = await _tool(
        factory, store, "artifact_list_recent", {}, user=OWNER, agent="butler", conv="conv_plain"
    )
    ids = [item["artifact_id"] for item in listed.metadata["items"]]
    assert second["artifact_id"] in ids and first["artifact_id"] not in ids


async def test_project_member_cannot_change_other_owners_lineage_or_sharing(env) -> None:
    factory, store = env
    _, first = await _write(factory, store, filename="state.json", content="{}", conv="conv_shared")
    before = await _artifact_count(factory)
    with pytest.raises(ArtifactRevisionError) as denied:
        await write_artifact_document(
            session_factory=factory,
            artifact_store=store,
            actor=ContentActor(
                user_email=WIFE, agent_id=None, conversation_id=None, session_id=None, human=True
            ),
            content=b'{"changed": true}',
            filename="state.json",
            mime_type="application/json",
            kind="file",
            purpose="artifact_write",
            supersedes=first["artifact_id"],
            agent_ids=["butler"],
        )
    assert denied.value.code == "supersedes_not_found"
    assert await _artifact_count(factory) == before


async def test_stale_update_conflicts_without_partial_state(env, tmp_path) -> None:
    factory, store = env
    _, first = await _write(factory, store, filename="c.json", content="{}")
    _, second = await _write(
        factory, store, filename="c.json", content='{"a": 1}', supersedes=first["artifact_id"]
    )
    before_rows = await _artifact_count(factory)
    before_files = len(_stored_objects(tmp_path / "artifacts"))

    stale, _ = await _write(
        factory, store, filename="c.json", content='{"b": 2}', supersedes=first["artifact_id"]
    )
    assert stale.is_error
    payload = json.loads(stale.output)
    assert payload["reason"] == "supersedes_not_latest"
    assert payload["latest_artifact_id"] == second["artifact_id"]

    invalid, _ = await _write(
        factory, store, filename="c.json", content="{not json", supersedes=second["artifact_id"]
    )
    assert invalid.is_error and json.loads(invalid.output)["reason"] == "invalid_json"

    foreign, _ = await _write(
        factory,
        store,
        user=OUTSIDER,
        agent="outsider-bot",
        conv="conv_outsider",
        filename="c.json",
        content="{}",
        supersedes=second["artifact_id"],
    )
    assert foreign.is_error and json.loads(foreign.output)["reason"] == "supersedes_not_found"

    assert await _artifact_count(factory) == before_rows
    assert len(_stored_objects(tmp_path / "artifacts")) == before_files


async def test_concurrent_loser_learns_head(env, monkeypatch) -> None:
    factory, store = env
    _, first = await _write(factory, store, filename="c.json", content="{}")
    original = store.async_save
    winner = None

    async def racing_save(*args, **kwargs):
        nonlocal winner
        result = await original(*args, **kwargs)
        if winner is None:
            # Storage is outside the metadata transaction.
            winner = {}
            _, winner = await _write(
                factory,
                store,
                filename="c.json",
                content='{"w": 1}',
                supersedes=first["artifact_id"],
            )
        return result

    monkeypatch.setattr(store, "async_save", racing_save)
    with pytest.raises(ArtifactRevisionError) as caught:
        await write_artifact_document(
            session_factory=factory,
            artifact_store=store,
            actor=_actor(OWNER, "butler", "conv_plain"),
            content=b"{}",
            filename="c.json",
            mime_type="application/json",
            kind="file",
            purpose="artifact_write",
            supersedes=first["artifact_id"],
        )
    assert caught.value.code == "supersedes_not_latest"
    assert caught.value.latest_artifact_id == winner["artifact_id"]


async def test_unassigned_publisher_reuses_own_project_content_across_chats(env) -> None:
    from cognis.store.queries import create_deliverable

    factory, store = env
    async with factory() as session:
        for conv, project in (("conv_bot_home", "proj_home"), ("conv_bot_work", "proj_work")):
            await create_conversation(
                session,
                conversation_id=conv,
                user_email=OWNER,
                agent_id="private-bot",
                context_type="web",
                project_id=project,
                visibility="project",
            )
        await session.commit()
    image = BytesIO()
    Image.new("RGB", (2, 2), "blue").save(image, format="PNG")
    photo, _ = await write_artifact_document(
        session_factory=factory,
        artifact_store=store,
        actor=_actor(OWNER, "private-bot", "conv_bot_home"),
        content=image.getvalue(),
        filename="photo.png",
        mime_type="image/png",
        kind="image",
        purpose="artifact_publish",
    )
    payload = {
        "title": "Publication regression",
        "blocks": [{"type": "figure", "media": {"ref": photo.artifact_id, "alt": "Photo"}}],
    }
    document, _ = await write_artifact_document(
        session_factory=factory,
        artifact_store=store,
        actor=_actor(OWNER, "private-bot", "conv_bot_home"),
        content=json.dumps(payload).encode(),
        filename="report.json",
        mime_type="application/json",
        kind="file",
        purpose="artifact_publish",
    )
    assert photo.project_id == document.project_id == "proj_home"
    for conv in ("conv_bot_home", "conv_bot", "conv_bot_work"):
        for source in (
            {"payload": payload},
            {"payload_artifact": {"artifact_id": document.artifact_id}},
        ):
            validated = await validate_available_tool_call_with_context(
                [WRITE_DELIVERABLE_TOOL],
                "write_deliverable",
                {"action": "rich", **source},
                NativeValidationContext(
                    session_factory=factory,
                    artifact_store=store,
                    actor_email=OWNER,
                    conversation_id=conv,
                    conversation_agent_id="private-bot",
                ),
            )
            assert validated["valid"], validated

    # Exercise persistence as well as preflight, including media retention.
    async with factory() as session:
        report = await create_deliverable(
            session,
            conversation_id="conv_bot_home",
            turn_id="publication-regression",
            content="Report",
            format="rich",
            rich=payload,
            artifact_store=store,
            project_id="proj_home",
        )
        assert report.project_id == "proj_home"
        await session.commit()
        report_id = report.deliverable_id
    for ref in (document.artifact_id, photo.artifact_id, report_id):
        for conv in ("conv_bot", "conv_bot_work"):
            result = await _tool(
                factory,
                store,
                "artifact_get_metadata",
                {"artifact_id": ref},
                user=OWNER,
                agent="private-bot",
                conv=conv,
            )
            assert not result.is_error, result.output
        denied = await _tool(
            factory,
            store,
            "artifact_get_metadata",
            {"artifact_id": ref},
            user=OUTSIDER,
            agent="outsider-bot",
            conv="conv_outsider",
        )
        assert denied.is_error
    listed = await _tool(
        factory, store, "artifact_list_recent", {}, user=OWNER, agent="private-bot", conv="conv_bot"
    )
    assert {document.artifact_id, photo.artifact_id} <= {
        item["artifact_id"] for item in listed.metadata["items"]
    }
    # Another unassigned agent of the same person cannot read these outputs.
    async with factory() as session:
        await create_agent(
            session,
            agent_id="unassigned-reader",
            owner_email=OWNER,
            name="Reader",
            status="active",
        )
        await create_conversation(
            session,
            conversation_id="conv_reader",
            user_email=OWNER,
            agent_id="unassigned-reader",
            context_type="web",
        )
        await session.commit()
    denied = await _tool(
        factory,
        store,
        "artifact_get_content",
        {"artifact_id": document.artifact_id},
        user=OWNER,
        agent="unassigned-reader",
        conv="conv_reader",
    )
    assert denied.is_error


async def test_owned_project_reads_require_current_human_access(env) -> None:
    from cognis.store.models import ProjectGrantRow
    from cognis.store.queries import create_deliverable

    factory, store = env
    async with factory() as session:
        await create_agent(
            session, agent_id="member-bot", owner_email=WIFE, name="Member", status="active"
        )
        await create_conversation(
            session,
            conversation_id="conv_member_bot",
            user_email=WIFE,
            agent_id="member-bot",
            context_type="web",
            project_id="proj_home",
            visibility="project",
        )
        await session.commit()
    result, doc = await _write(
        factory,
        store,
        user=WIFE,
        agent="member-bot",
        conv="conv_member_bot",
        filename="member.json",
        content="{}",
    )
    assert not result.is_error, result.output
    async with factory() as session:
        report = await create_deliverable(
            session,
            conversation_id="conv_member_bot",
            turn_id="member-report",
            content="Report",
            artifact_store=store,
            project_id="proj_home",
        )
        await session.commit()
        report_id = report.deliverable_id
    for ref in (doc["artifact_id"], report_id):
        result = await _tool(
            factory,
            store,
            "artifact_get_metadata",
            {"artifact_id": ref},
            user=WIFE,
            agent="member-bot",
            conv="conv_member_bot",
        )
        assert not result.is_error, result.output
    async with factory() as session:
        from datetime import UTC, datetime

        grant = await session.scalar(
            select(ProjectGrantRow).where(ProjectGrantRow.grantee_user_email == WIFE)
        )
        grant.revoked_at = datetime.now(UTC)
        await session.commit()
    for ref in (doc["artifact_id"], report_id):
        denied = await _tool(
            factory,
            store,
            "artifact_get_metadata",
            {"artifact_id": ref},
            user=WIFE,
            agent="member-bot",
            conv="conv_member_bot",
        )
        assert denied.is_error


async def test_project_document_access_matrix(env) -> None:
    factory, store = env
    # Created in shared project work: joins the project automatically.
    _, doc = await _write(factory, store, conv="conv_shared", filename="cfg.json", content="{}")
    assert doc["project_id"] == "proj_home"
    # Created in a private chat inside the project: stays private.
    _, private = await _write(factory, store, conv="conv_private", filename="p.json", content="{}")
    assert private["project_id"] is None

    async def read(user: str, agent: str, conv: str) -> bool:
        result = await _tool(
            factory,
            store,
            "artifact_get_content",
            {"artifact_id": doc["artifact_id"]},
            user=user,
            agent=agent,
            conv=conv,
        )
        return not result.is_error

    assert await read(WIFE, "butler", "conv_wife")  # member, assigned agent, right project
    assert not await read(OWNER, "private-bot", "conv_bot")  # unassigned agent
    assert not await read(OUTSIDER, "outsider-bot", "conv_outsider")  # non-member

    # A member's agent can revise the project document; the revision keeps its project.
    _, by_wife = await _write(
        factory,
        store,
        user=WIFE,
        conv="conv_wife",
        filename="cfg.json",
        content='{"v": 2}',
        supersedes=doc["artifact_id"],
    )
    assert by_wife["project_id"] == "proj_home" and by_wife["revision"] == 2
    # Stable ownership retains the original owner's cross-chat read access.
    other_project_read = await _tool(
        factory,
        store,
        "artifact_get_content",
        {"artifact_id": by_wife["artifact_id"]},
        user=OWNER,
        agent="butler",
        conv="conv_work",
    )
    assert not other_project_read.is_error
    denied, _ = await _write(
        factory,
        store,
        conv="conv_work",
        filename="cfg.json",
        content="{}",
        supersedes=by_wife["artifact_id"],
    )
    assert denied.is_error
    # A private document of the owner is not reachable by the member.
    assert (
        await _tool(
            factory,
            store,
            "artifact_get_content",
            {"artifact_id": private["artifact_id"]},
            user=WIFE,
            agent="butler",
            conv="conv_wife",
        )
    ).is_error

    # Project discovery: the member finds the owner's document by project filter.
    found = await _tool(
        factory,
        store,
        "artifact_search",
        {"project_id": "current"},
        user=WIFE,
        agent="butler",
        conv="conv_wife",
    )
    found_ids = [item["artifact_id"] for item in found.metadata["items"]]
    assert by_wife["artifact_id"] in found_ids and doc["artifact_id"] not in found_ids
    # A non-readable project yields nothing, never existence.
    hidden = await _tool(
        factory,
        store,
        "artifact_search",
        {"project_id": "proj_home"},
        user=OUTSIDER,
        agent="outsider-bot",
        conv="conv_outsider",
    )
    assert hidden.metadata["items"] == []


async def test_explicit_project_requires_write_access(env) -> None:
    factory, store = env
    async with factory() as session:
        writable = await content_project_ids(
            session,
            user_email=VIEWER,
            agent_id="butler",
            conversation_id=None,
            session_id=None,
            access="write",
        )
    assert writable == []
    ok, created = await _write(
        factory, store, conv="conv_plain", filename="x.json", content="{}", project_id="proj_home"
    )
    assert not ok.is_error and created["project_id"] == "proj_home"
    denied, _ = await _write(
        factory,
        store,
        agent="private-bot",
        conv="conv_bot",
        filename="x.json",
        content="{}",
        project_id="proj_home",
    )
    assert denied.is_error and json.loads(denied.output)["reason"] == "project_not_writable"


async def test_deterministic_tool_step_read_update(env) -> None:
    """A workflow tool_call step reads JSON and writes the next revision without an LLM."""

    factory, store = env
    _, checkpoint = await _write(
        factory,
        store,
        conv="conv_shared",
        filename="checkpoint.json",
        content=json.dumps({"reported": {}}),
    )
    # Run 1 and run 2 come from different sessions/executors: state lives in Cognis.
    for run, offer in enumerate(("offer-a", "offer-b"), start=1):
        read = await _tool(
            factory,
            store,
            "artifact_get_content",
            {"artifact_id": checkpoint["lineage_id"], "latest": True},
            user=OWNER,
            agent="butler",
            conv="conv_shared",
        )
        state = json.loads(read.output)
        reported = dict(state["content"]["reported"])
        reported[offer] = 1000 + run
        _, written = await _write(
            factory,
            store,
            conv="conv_shared",
            filename="checkpoint.json",
            content=json.dumps({"reported": reported}),
            supersedes=state["artifact_id"],
        )
        assert written["revision"] == run + 1
    final = await _tool(
        factory,
        store,
        "artifact_get_content",
        {"artifact_id": checkpoint["artifact_id"], "latest": True},
        user=WIFE,
        agent="butler",
        conv="conv_wife",
    )
    assert json.loads(final.output)["content"]["reported"] == {"offer-a": 1001, "offer-b": 1002}


@pytest.mark.parametrize("backend", ["sqlite", "postgresql"])
async def test_schema_upgrade_backfills_and_guards(tmp_path, backend) -> None:
    url = os.environ.get("COGNIS_TEST_POSTGRES_URL")
    if backend == "postgresql" and not url:
        pytest.skip("needs COGNIS_TEST_POSTGRES_URL")
    schema = f"art_rev_{uuid.uuid4().hex}"
    admin = None
    if backend == "postgresql":
        admin = create_async_engine(url)
        async with admin.begin() as conn:
            await conn.execute(text(f'CREATE SCHEMA "{schema}"'))
        engine = create_async_engine(url, connect_args={"server_settings": {"search_path": schema}})
    else:
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/schema.db")

    def exercise(conn) -> None:
        conn.execute(
            text(
                "CREATE TABLE artifacts (artifact_id VARCHAR PRIMARY KEY, "
                "status VARCHAR NOT NULL, purpose VARCHAR, conversation_id VARCHAR)"
            )
        )
        conn.execute(
            text(
                "CREATE TABLE deliverables (deliverable_id VARCHAR PRIMARY KEY, "
                "step_run_id VARCHAR, conversation_id VARCHAR)"
            )
        )
        conn.execute(
            text(
                "CREATE TABLE tasks (task_id VARCHAR PRIMARY KEY, project_id VARCHAR, "
                "visibility VARCHAR)"
            )
        )
        conn.execute(
            text("CREATE TABLE step_runs (step_run_id VARCHAR PRIMARY KEY, task_id VARCHAR)")
        )
        conn.execute(
            text(
                "CREATE TABLE conversations (conversation_id VARCHAR PRIMARY KEY, "
                "project_id VARCHAR, visibility VARCHAR)"
            )
        )
        conn.execute(
            text(
                "INSERT INTO conversations VALUES ('c_shared','p1','project'), "
                "('c_private','p1','private')"
            )
        )
        conn.execute(text("INSERT INTO tasks VALUES ('t1','p1','project')"))
        conn.execute(text("INSERT INTO step_runs VALUES ('s1','t1')"))
        conn.execute(
            text(
                "INSERT INTO deliverables VALUES ('d_task','s1',NULL), "
                "('d_shared',NULL,'c_shared'), ('d_private',NULL,'c_private')"
            )
        )
        conn.execute(
            text(
                "INSERT INTO artifacts VALUES ('a_pub','attached','artifact_publish',"
                "'c_shared'), ('a_upload','attached','user_upload','c_shared'), "
                "('d_shared','attached','conversation_deliverable','c_shared')"
            )
        )
        ensure_content_revision_schema(conn)
        # A later private row in a project-visible chat stays private on re-run.
        conn.execute(
            text(
                "INSERT INTO deliverables (deliverable_id, conversation_id) "
                "VALUES ('d_later', 'c_shared')"
            )
        )
        ensure_content_revision_schema(conn)
        assert (
            conn.execute(
                text("SELECT project_id FROM deliverables WHERE deliverable_id='d_later'")
            ).scalar()
            is None
        )
        conn.execute(text("DELETE FROM deliverables WHERE deliverable_id='d_later'"))
        rows = dict(conn.execute(text("SELECT deliverable_id, project_id FROM deliverables")).all())
        assert rows == {"d_task": "p1", "d_shared": "p1", "d_private": None}
        arts = dict(conn.execute(text("SELECT artifact_id, project_id FROM artifacts")).all())
        assert arts == {"a_pub": "p1", "a_upload": None, "d_shared": "p1"}
        lineage = conn.execute(
            text("SELECT lineage_id, revision FROM artifacts WHERE artifact_id='a_pub'")
        ).one()
        assert tuple(lineage) == ("a_pub", 1)
        conn.execute(
            text(
                "INSERT INTO artifacts (artifact_id, status, supersedes_artifact_id) "
                "VALUES ('r_deleted','deleted','a_pub'), ('r_live','attached','a_pub')"
            )
        )
        with pytest.raises(IntegrityError), conn.begin_nested():
            conn.execute(
                text(
                    "INSERT INTO artifacts (artifact_id, status, "
                    "supersedes_artifact_id) VALUES ('r_dup','attached','a_pub')"
                )
            )

    try:
        async with engine.begin() as conn:
            await conn.run_sync(exercise)
    finally:
        await engine.dispose()
        if admin is not None:
            async with admin.begin() as conn:
                await conn.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
            await admin.dispose()


async def test_owner_agent_reads_but_cannot_revise_from_other_project(env) -> None:
    factory, store = env
    _, doc = await _write(factory, store, conv="conv_shared", filename="cfg.json", content="{}")
    read = await _tool(
        factory,
        store,
        "artifact_get_content",
        {"artifact_id": doc["artifact_id"]},
        user=OWNER,
        agent="butler",
        conv="conv_work",
    )
    assert not read.is_error
    revised, _ = await _write(
        factory,
        store,
        conv="conv_work",
        filename="cfg.json",
        content="{}",
        supersedes=doc["artifact_id"],
    )
    assert revised.is_error
    found = await _tool(
        factory, store, "artifact_list_recent", {}, user=OWNER, agent="butler", conv="conv_work"
    )
    assert doc["artifact_id"] in [item["artifact_id"] for item in found.metadata["items"]]
    # Outside any project context the owner's assigned agent still reaches it.
    plain = await _tool(
        factory,
        store,
        "artifact_get_content",
        {"artifact_id": doc["artifact_id"]},
        user=OWNER,
        agent="butler",
        conv="conv_plain",
    )
    assert not plain.is_error


async def test_private_lineage_stays_private_when_revised_in_project_work(env) -> None:
    factory, store = env
    _, private = await _write(factory, store, conv="conv_private", filename="p.json", content="{}")
    _, revised = await _write(
        factory,
        store,
        conv="conv_shared",
        filename="p.json",
        content='{"v": 2}',
        supersedes=private["artifact_id"],
    )
    assert revised["project_id"] is None
    async with factory() as session:
        await session.run_sync(lambda s: ensure_content_revision_schema(s.connection()))
        await session.commit()
    member = await _tool(
        factory,
        store,
        "artifact_get_content",
        {"artifact_id": revised["artifact_id"]},
        user=WIFE,
        agent="butler",
        conv="conv_wife",
    )
    assert member.is_error


async def test_deleted_intermediate_revision_does_not_reopen_older_revision(env) -> None:
    factory, store = env
    _, r1 = await _write(factory, store, filename="c.json", content="{}")
    _, r2 = await _write(
        factory, store, filename="c.json", content="{}", supersedes=r1["artifact_id"]
    )
    _, r3 = await _write(
        factory, store, filename="c.json", content="{}", supersedes=r2["artifact_id"]
    )
    async with factory() as session:
        row = await session.get(ArtifactRecordRow, r2["artifact_id"])
        row.status = "deleted"
        await session.commit()
    before = await _artifact_count(factory)
    forked, _ = await _write(
        factory, store, filename="c.json", content="{}", supersedes=r1["artifact_id"]
    )
    payload = json.loads(forked.output)
    assert payload["reason"] == "supersedes_not_latest"
    assert payload["latest_artifact_id"] == r3["artifact_id"]
    assert await _artifact_count(factory) == before


# --- Chats in a project are shared by default; their documents follow them ---


async def _change_project(factory, conversation_id: str, project_id: str | None) -> None:
    from cognis.core.project_access import change_conversation_project
    from cognis.store.queries import get_conversation

    async with factory() as session:
        row = await get_conversation(session, conversation_id)
        await change_conversation_project(session, row, project_id=project_id, user_email=OWNER)
        await session.commit()


async def _change_visibility(factory, conversation_id: str, visibility: str) -> None:
    from cognis.core.project_access import change_conversation_visibility
    from cognis.store.queries import get_conversation

    async with factory() as session:
        row = await get_conversation(session, conversation_id)
        await change_conversation_visibility(session, row, visibility=visibility)
        await session.commit()


async def _projects(factory) -> dict[str, str | None]:
    from cognis.store.models import DeliverableRow

    async with factory() as session:
        rows = dict(
            (
                await session.execute(
                    select(ArtifactRecordRow.artifact_id, ArtifactRecordRow.project_id)
                )
            ).all()
        )
        rows.update(
            {
                f"row:{k}": v
                for k, v in (
                    await session.execute(
                        select(DeliverableRow.deliverable_id, DeliverableRow.project_id)
                    )
                ).all()
            }
        )
        return rows


async def test_chat_created_in_project_is_shared_by_default(env) -> None:
    factory, _store = env
    async with factory() as session:
        shared = await create_conversation(
            session,
            user_email=OWNER,
            agent_id="butler",
            context_type="web",
            conversation_id="conv_new_in_project",
            project_id="proj_home",
        )
        plain = await create_conversation(
            session,
            user_email=OWNER,
            agent_id="butler",
            context_type="web",
            conversation_id="conv_new_plain",
        )
        await session.commit()
    assert (shared.visibility, plain.visibility) == ("project", "private")


async def test_documents_follow_chat_project_and_visibility(env) -> None:
    from cognis.store.queries import create_deliverable, get_conversation

    factory, store = env
    # Written while the chat has no project: private.
    _, config = await _write(factory, store, conv="conv_plain", filename="cfg.json", content="{}")
    _, config_v2 = await _write(
        factory,
        store,
        conv="conv_plain",
        filename="cfg.json",
        content='{"v":2}',
        supersedes=config["artifact_id"],
    )
    # Explicitly placed in another project: never follows the chat.
    _, placed = await _write(
        factory, store, conv="conv_plain", filename="x.json", content="{}", project_id="proj_work"
    )
    # Started in another chat, revised here: belongs to its origin.
    _, foreign = await _write(
        factory, store, conv="conv_bot", agent="private-bot", filename="f.json", content="{}"
    )
    async with factory() as session:
        report = await create_deliverable(
            session,
            deliverable_id=f"dlv_{uuid.uuid4().hex}",
            conversation_id="conv_plain",
            turn_id="t1",
            content="# Report",
            artifact_store=store,
            published_owner_email=OWNER,
        )
        await session.commit()

    await _change_project(factory, "conv_plain", "proj_home")
    async with factory() as session:
        assert (await get_conversation(session, "conv_plain")).visibility == "project"
    scopes = await _projects(factory)
    assert scopes[config["artifact_id"]] == scopes[config_v2["artifact_id"]] == "proj_home"
    assert scopes[report.deliverable_id] == scopes[f"row:{report.deliverable_id}"] == "proj_home"
    assert scopes[placed["artifact_id"]] == "proj_work"
    assert scopes[foreign["artifact_id"]] is None
    member_read = await _tool(
        factory,
        store,
        "artifact_get_content",
        {"artifact_id": config["lineage_id"], "latest": True},
        user=WIFE,
        agent="butler",
        conv="conv_wife",
    )
    assert json.loads(member_read.output)["content"] == {"v": 2}

    # Making the chat private takes its documents out of the project.
    await _change_visibility(factory, "conv_plain", "private")
    scopes = await _projects(factory)
    assert scopes[config["artifact_id"]] is None and scopes[config_v2["artifact_id"]] is None
    assert scopes[report.deliverable_id] is None and scopes[f"row:{report.deliverable_id}"] is None
    assert scopes[placed["artifact_id"]] == "proj_work"
    assert (
        await _tool(
            factory,
            store,
            "artifact_get_content",
            {"artifact_id": config_v2["artifact_id"]},
            user=WIFE,
            agent="butler",
            conv="conv_wife",
        )
    ).is_error

    # Sharing again, then moving to another project, carries them along.
    await _change_visibility(factory, "conv_plain", "project")
    await _change_project(factory, "conv_plain", "proj_work")
    scopes = await _projects(factory)
    assert scopes[config_v2["artifact_id"]] == "proj_work"
    assert scopes[report.deliverable_id] == "proj_work"

    # Removing the project makes them private again.
    await _change_project(factory, "conv_plain", None)
    scopes = await _projects(factory)
    assert scopes[config_v2["artifact_id"]] is None


async def test_private_chat_assigned_elsewhere_keeps_explicit_privacy(env) -> None:
    """Moving a chat the user made private to another project keeps it private."""

    from cognis.store.queries import get_conversation

    factory, store = env
    _, doc = await _write(factory, store, conv="conv_private", filename="p.json", content="{}")
    await _change_project(factory, "conv_private", "proj_work")
    async with factory() as session:
        assert (await get_conversation(session, "conv_private")).visibility == "private"
    assert (await _projects(factory))[doc["artifact_id"]] is None


async def test_combined_project_and_visibility_change_moves_documents_once(env) -> None:
    """Project B plus private in one change: an explicitly B-placed document stays in B."""

    from cognis.core.project_access import change_conversation_project
    from cognis.store.queries import get_conversation

    factory, store = env
    # Placed explicitly in proj_work while the chat had no project...
    _, placed = await _write(
        factory, store, conv="conv_plain", filename="b.json", content="{}", project_id="proj_work"
    )
    # ...then the chat joins proj_home and writes a following document.
    await _change_project(factory, "conv_plain", "proj_home")
    _, following = await _write(factory, store, conv="conv_plain", filename="f.json", content="{}")
    assert following["project_id"] == "proj_home"
    async with factory() as session:
        row = await get_conversation(session, "conv_plain")
        await change_conversation_project(
            session, row, project_id="proj_work", user_email=OWNER, visibility="private"
        )
        await session.commit()
    scopes = await _projects(factory)
    assert scopes[following["artifact_id"]] is None
    assert scopes[placed["artifact_id"]] == "proj_work"


@pytest.mark.skipif(
    not os.environ.get("COGNIS_TEST_POSTGRES_URL"), reason="needs COGNIS_TEST_POSTGRES_URL"
)
async def test_privatizing_during_artifact_revision_leaves_no_shared_revision(
    tmp_path, monkeypatch
) -> None:
    import asyncio

    url = os.environ["COGNIS_TEST_POSTGRES_URL"]
    schema = f"share_race_{uuid.uuid4().hex}"
    admin = create_async_engine(url)
    async with admin.begin() as conn:
        await conn.execute(text(f'CREATE SCHEMA "{schema}"'))
    engine = create_async_engine(url, connect_args={"server_settings": {"search_path": schema}})
    try:
        await run_schema_bootstrap(engine)
        factory = create_session_factory(engine)
        store = ArtifactStore(ArtifactStoreConfig(path=str(tmp_path / "artifacts")))
        async with factory() as session:
            await _seed(session)
            await session.commit()
        _, doc = await _write(factory, store, conv="conv_shared", filename="c.json", content="{}")
        assert doc["project_id"] == "proj_home"

        pause, resume = asyncio.Event(), asyncio.Event()
        original_save = store.async_save

        async def paused_save(*args, **kwargs):
            if b'"race"' in args[3]:
                pause.set()
                await resume.wait()
            return await original_save(*args, **kwargs)

        monkeypatch.setattr(store, "async_save", paused_save)
        # The member revises the project document from her own project chat.
        writer = asyncio.create_task(
            _write(
                factory,
                store,
                user=WIFE,
                conv="conv_wife",
                filename="c.json",
                content='{"race": 1}',
                supersedes=doc["artifact_id"],
            )
        )
        await asyncio.wait_for(pause.wait(), 10)
        await _change_visibility(factory, "conv_shared", "private")
        resume.set()
        result, payload = await asyncio.wait_for(writer, 20)

        # Either the member lost access and nothing was written, or the revision
        # followed the now-private lineage. Never a shared revision.
        async with factory() as session:
            rows = (
                await session.execute(
                    select(ArtifactRecordRow.artifact_id, ArtifactRecordRow.project_id).where(
                        ArtifactRecordRow.lineage_id == doc["artifact_id"]
                    )
                )
            ).all()
        assert all(project is None for _id, project in rows), rows
        assert result.is_error or payload["project_id"] is None
    finally:
        await engine.dispose()
        async with admin.begin() as conn:
            await conn.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        await admin.dispose()


@pytest.mark.skipif(
    not os.environ.get("COGNIS_TEST_POSTGRES_URL"), reason="needs COGNIS_TEST_POSTGRES_URL"
)
async def test_concurrent_move_and_privatize_leave_no_shared_documents(tmp_path) -> None:
    """A stale privatize request must not leave the chat's documents in the new project."""

    from cognis.core.project_access import (
        change_conversation_project,
        change_conversation_visibility,
    )
    from cognis.store.queries import get_conversation

    url = os.environ["COGNIS_TEST_POSTGRES_URL"]
    schema = f"share_move_{uuid.uuid4().hex}"
    admin = create_async_engine(url)
    async with admin.begin() as conn:
        await conn.execute(text(f'CREATE SCHEMA "{schema}"'))
    engine = create_async_engine(url, connect_args={"server_settings": {"search_path": schema}})
    try:
        await run_schema_bootstrap(engine)
        factory = create_session_factory(engine)
        store = ArtifactStore(ArtifactStoreConfig(path=str(tmp_path / "artifacts")))
        async with factory() as session:
            await _seed(session)
            await session.commit()
        _, doc = await _write(factory, store, conv="conv_shared", filename="c.json", content="{}")
        assert doc["project_id"] == "proj_home"

        # The privatize request loads the chat (shared, proj_home) and stalls...
        privatize_session = factory()
        stale = await get_conversation(privatize_session, "conv_shared")
        assert (stale.project_id, stale.visibility) == ("proj_home", "project")
        # ...while another request moves the chat and its documents to proj_work.
        async with factory() as session:
            row = await get_conversation(session, "conv_shared")
            await change_conversation_project(
                session, row, project_id="proj_work", user_email=OWNER
            )
            await session.commit()
        await change_conversation_visibility(privatize_session, stale, visibility="private")
        await privatize_session.commit()
        await privatize_session.close()

        async with factory() as session:
            final = await get_conversation(session, "conv_shared")
            projects = (
                await session.scalars(
                    select(ArtifactRecordRow.project_id).where(
                        ArtifactRecordRow.lineage_id == doc["artifact_id"]
                    )
                )
            ).all()
        assert (final.project_id, final.visibility) == ("proj_work", "private")
        assert projects == [None]
    finally:
        await engine.dispose()
        async with admin.begin() as conn:
            await conn.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        await admin.dispose()
