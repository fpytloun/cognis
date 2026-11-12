"""Document writes through the real artifact tool and deliverable publication boundary."""

from __future__ import annotations

import asyncio
import importlib
import os
import uuid
from types import SimpleNamespace

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from cognis.artifacts.store import ArtifactStore, ArtifactStoreConfig
from cognis.core.agent_loop import (
    CONTROLLER_TOOL_SURFACE_DIRECT_CHAT,
    AgentLoop,
    PauseWaiter,
    SessionLock,
    StepContext,
)
from cognis.core.artifact_authoring import save_human_artifact
from cognis.core.artifact_sharing import manage_document_shares
from cognis.core.deliverable_authoring import resolve_deliverable_authoring
from cognis.core.document_sharing import replace_explicit_shares
from cognis.core.events import EventBus
from cognis.models.agent import AgentDefinition
from cognis.models.deliverable import DeliverableRevisionError
from cognis.models.session import ConversationContext, ConversationModel, SessionModel
from cognis.models.workflow import StepDefinition
from cognis.store.artifact_access_schema import ensure_artifact_access_schema
from cognis.store.document_write_schema import rollback_document_write_schema
from cognis.store.models import (
    ArtifactGrantRow,
    ArtifactRecordRow,
    Base,
    DeliverableRow,
)
from cognis.store.project_queries import upsert_project_agent
from cognis.store.queries import (
    create_agent,
    create_conversation,
    create_project,
    create_project_grant,
    create_session,
    create_user,
)
from cognis.tools.builtin.artifact_tools import handle_artifact_tool

OWNER = "lineage@cognis.test"


@pytest.fixture(params=["sqlite", "postgres"])
async def lineage_env(tmp_path, request):
    url = os.environ.get("COGNIS_TEST_POSTGRES_URL")
    if request.param == "postgres" and not url:
        pytest.skip("requires disposable COGNIS_TEST_POSTGRES_URL")
    admin = None
    schema = f"lineage_write_{uuid.uuid4().hex}"
    if request.param == "postgres":
        admin = create_async_engine(url)
        async with admin.begin() as connection:
            await connection.execute(sa.text(f'CREATE SCHEMA "{schema}"'))
        engine = create_async_engine(url, connect_args={"server_settings": {"search_path": schema}})
    else:
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/lineage.db")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    store = ArtifactStore(ArtifactStoreConfig(path=str(tmp_path / "payloads")))
    contexts = {}
    try:
        async with factory() as db:
            await create_user(db, OWNER, name="Owner", password_hash="test")
            for agent in ("author", "writer", "reader"):
                await create_agent(
                    db, agent_id=agent, owner_email=OWNER, name=agent, status="active"
                )
                conversation = await create_conversation(
                    db, OWNER, agent, "agent_direct", conversation_id=f"chat-{agent}"
                )
                session = await create_session(db, conversation.conversation_id, OWNER, agent)
                contexts[agent] = StepContext(
                    agent=AgentDefinition(
                        agent_id=agent, owner_email=OWNER, name=agent, status="active"
                    ),
                    conversation=ConversationModel(
                        conversation_id=conversation.conversation_id,
                        user_email=OWNER,
                        agent_id=agent,
                        context=ConversationContext(type=conversation.context_type),
                    ),
                    session=SessionModel.model_validate(session, from_attributes=True),
                    step_definition=StepDefinition(name="write", type="run", prompt=""),
                    controller_tool_surface=CONTROLLER_TOOL_SURFACE_DIRECT_CHAT,
                    turn_id=uuid.uuid4().hex,
                )
            project = await create_project(db, owner_email=OWNER, name="State")
            await upsert_project_agent(
                db, project_id=project.project_id, agent_id="author", added_by=OWNER
            )
            await db.commit()
        loop = AgentLoop(
            providers=SimpleNamespace(),
            session_manager=SimpleNamespace(session_factory=factory),
            session_cache=SimpleNamespace(),
            context_assembler=SimpleNamespace(),
            compaction_strategy=SimpleNamespace(),
            tool_router=SimpleNamespace(artifact_store=store),
            remember_queue=SimpleNamespace(),
            event_bus=EventBus(),
            session_lock=SessionLock(),
            pause_waiter=PauseWaiter(),
            session_factory=factory,
        )
        yield SimpleNamespace(
            factory=factory,
            engine=engine,
            store=store,
            contexts=contexts,
            loop=loop,
            project_id=project.project_id,
        )
    finally:
        await engine.dispose()
        if admin:
            async with admin.begin() as connection:
                await connection.execute(sa.text(f'DROP SCHEMA "{schema}" CASCADE'))
            await admin.dispose()


async def publish(env, kind, agent, supersedes=None, project_id=None):
    ctx = env.contexts[agent]
    arguments = {"content": f"written by {agent}"}
    if supersedes:
        arguments["supersedes"] = supersedes
    if project_id:
        arguments["project_id"] = project_id
    if kind == "artifact":
        result = await handle_artifact_tool(
            "artifact_write",
            {**arguments, "filename": "state.txt"},
            llm=None,
            artifact_store=env.store,
            session_factory=env.factory,
            user_email=ctx.session.user_email,
            runtime_metadata={
                "agent_id": agent,
                "conversation_id": ctx.conversation.conversation_id,
                "session_id": ctx.session.session_id,
            },
        )
        if result.is_error:
            import json

            error = json.loads(result.output)
            raise DeliverableRevisionError(
                error["reason"],
                error["message"],
                latest_deliverable_id=error.get("latest_artifact_id"),
            )
        return result.metadata["artifact_id"]
    authoring = await resolve_deliverable_authoring(
        arguments,
        session_factory=env.factory,
        artifact_store=env.store,
        owner_email=ctx.session.user_email,
        conversation_id=ctx.conversation.conversation_id,
        agent_id=agent,
    )
    return (await env.loop._write_step_deliverable(ctx, authoring=authoring)).deliverable_id


async def share(env, kind, ref, readers, writers=()):
    async with env.factory() as db:
        model = ArtifactRecordRow if kind == "artifact" else DeliverableRow
        await replace_explicit_shares(
            db, await db.get(model, ref), OWNER, list(readers), list(writers)
        )
        await db.commit()


@pytest.mark.parametrize("kind", ["artifact", "deliverable"])
@pytest.mark.parametrize("project", [False, True])
async def test_explicit_write_is_lineage_scoped_and_revoked_live(lineage_env, kind, project):
    env = lineage_env
    first = await publish(env, kind, "author", project_id=env.project_id if project else None)
    await share(env, kind, first, ["writer", "reader"], ["writer"])
    with pytest.raises(DeliverableRevisionError):
        await publish(env, kind, "reader", first)
    second = await publish(env, kind, "writer", first)
    async with env.factory() as db:
        model = ArtifactRecordRow if kind == "artifact" else DeliverableRow
        head = await db.get(model, second)
        assert (head.owner_email, head.owner_agent_id, head.author_agent_id) == (
            OWNER,
            "author",
            "writer",
        )
        assert head.author_email == OWNER
        assert (head.lineage_id, head.revision, head.project_id) == (
            first,
            2,
            env.project_id if project else None,
        )
        for actor in ("author", "writer"):
            with pytest.raises(DeliverableRevisionError) as stale:
                await publish(env, kind, actor, first)
            assert stale.value.latest_deliverable_id == second
        # The write recipient is not allowed to reshare the revision it authored.
        with pytest.raises(ValueError):
            await manage_document_shares(
                db,
                content_ref=second,
                owner_email=OWNER,
                owner_agent_id="writer",
                action="grant",
                agent_id="reader",
                permission="write",
            )
    third = await publish(env, kind, "writer", second)
    # Revoke at an old revision; the same already-running session must lose write.
    await share(env, kind, first, ["writer", "reader"])
    with pytest.raises(DeliverableRevisionError):
        await publish(env, kind, "writer", third)
    async with env.factory() as db:
        permissions = set(
            await db.scalars(
                sa.select(ArtifactGrantRow.permission).where(
                    ArtifactGrantRow.agent_id == "writer", ArtifactGrantRow.source == "explicit"
                )
            )
        )
        assert permissions == {"read"}
    latest = await publish(env, kind, "author", third)
    assert latest != third


@pytest.mark.parametrize("kind", ["artifact", "deliverable"])
async def test_writing_null_owned_document_does_not_grant_ownership(lineage_env, kind):
    env = lineage_env
    if kind == "artifact":
        first = await save_human_artifact(
            env.factory,
            env.store,
            OWNER,
            content=b"human-owned state",
            filename="state.txt",
            mime_type="text/plain",
        )
    else:
        first = await publish(env, kind, "author")
        async with env.factory() as db:
            original = await db.get(DeliverableRow, first)
            mirror = await db.get(ArtifactRecordRow, first)
            original.owner_agent_id = mirror.owner_agent_id = None
            await db.commit()
    await share(env, kind, first, ["writer"], ["writer"])
    second = await publish(env, kind, "writer", first)
    model = ArtifactRecordRow if kind == "artifact" else DeliverableRow
    async with env.factory() as db:
        assert (await db.get(model, second)).owner_agent_id is None
    async with env.engine.begin() as connection:
        await connection.run_sync(ensure_artifact_access_schema)
        await connection.run_sync(ensure_artifact_access_schema)
    async with env.factory() as db:
        head = await db.get(model, second)
        assert (head.owner_email, head.owner_agent_id, head.author_agent_id) == (
            OWNER,
            None,
            "writer",
        )
        assert (await db.get(model, first)).owner_agent_id is None
        assert (await db.get(ArtifactRecordRow, second)).owner_agent_id is None
        with pytest.raises(ValueError):
            await manage_document_shares(
                db,
                content_ref=second,
                owner_email=OWNER,
                owner_agent_id="writer",
                action="grant",
                agent_id="reader",
                permission="write",
            )
    await share(env, kind, first, ["writer"])
    with pytest.raises(DeliverableRevisionError) as denied:
        await publish(env, kind, "writer", second)
    assert denied.value.code == "supersedes_not_found"


@pytest.mark.parametrize("kind", ["artifact", "deliverable"])
@pytest.mark.parametrize("same_session", [False, True])
async def test_same_head_has_only_one_successor(lineage_env, kind, same_session):
    env = lineage_env
    first = await publish(env, kind, "author")
    await share(env, kind, first, ["writer"], ["writer"])
    results = await asyncio.gather(
        publish(env, kind, "writer" if same_session else "author", first),
        publish(env, kind, "writer", first),
        return_exceptions=True,
    )
    winners = [value for value in results if isinstance(value, str)]
    losers = [value for value in results if isinstance(value, DeliverableRevisionError)]
    assert len(winners) == len(losers) == 1, results
    assert losers[0].latest_deliverable_id == winners[0]


async def test_artifact_revoke_during_storage_blocks_publication(lineage_env, monkeypatch):
    env = lineage_env
    first = await publish(env, "artifact", "author")
    await share(env, "artifact", first, ["writer"], ["writer"])
    save = env.store.async_save

    async def revoke_after_storage(*args, **kwargs):
        saved = await save(*args, **kwargs)
        await share(env, "artifact", first, ["writer"])
        return saved

    monkeypatch.setattr(env.store, "async_save", revoke_after_storage)
    with pytest.raises(DeliverableRevisionError):
        await publish(env, "artifact", "writer", first)
    async with env.factory() as db:
        rows = list(
            await db.scalars(
                sa.select(ArtifactRecordRow).where(ArtifactRecordRow.lineage_id == first)
            )
        )
        assert [row.artifact_id for row in rows] == [first]


@pytest.mark.parametrize("kind", ["artifact", "deliverable"])
async def test_project_grants_write_without_explicit_grant(lineage_env, kind):
    env = lineage_env
    first = await publish(env, kind, "author", project_id=env.project_id)
    async with env.factory() as db:
        await upsert_project_agent(db, project_id=env.project_id, agent_id="writer", added_by=OWNER)
        await db.commit()
    second = await publish(env, kind, "writer", first)
    async with env.factory() as db:
        assert not await db.scalar(
            sa.select(ArtifactGrantRow.agent_id).where(
                ArtifactGrantRow.agent_id == "writer", ArtifactGrantRow.source == "explicit"
            )
        )
    assert second != first


@pytest.mark.parametrize("kind", ["artifact", "deliverable"])
async def test_cross_user_project_author_keeps_original_lineage_grants(lineage_env, kind):
    env = lineage_env
    first = await publish(env, kind, "author", project_id=env.project_id)
    await share(env, kind, first, ["writer"], ["writer"])
    member = "member@cognis.test"
    async with env.factory() as db:
        await create_user(db, member, name="Member", password_hash="test")
        await create_agent(
            db, agent_id="member-agent", owner_email=member, name="Member", status="active"
        )
        await upsert_project_agent(
            db, project_id=env.project_id, agent_id="member-agent", added_by=OWNER
        )
        await create_project_grant(
            db,
            project_id=env.project_id,
            granted_by=OWNER,
            grantee_user_email=member,
            permission="member",
        )
        conversation = await create_conversation(
            db,
            member,
            "member-agent",
            "agent_direct",
            project_id=env.project_id,
            visibility="project",
        )
        session = await create_session(db, conversation.conversation_id, member, "member-agent")
        await db.commit()
        env.contexts["member-agent"] = StepContext(
            agent=AgentDefinition(agent_id="member-agent", owner_email=member, name="Member"),
            conversation=ConversationModel(
                conversation_id=conversation.conversation_id,
                user_email=member,
                agent_id="member-agent",
                project_id=env.project_id,
                visibility="project",
                context=ConversationContext(type=conversation.context_type),
            ),
            session=SessionModel.model_validate(session, from_attributes=True),
            step_definition=StepDefinition(name="write", type="run", prompt=""),
            controller_tool_surface=CONTROLLER_TOOL_SURFACE_DIRECT_CHAT,
            turn_id=uuid.uuid4().hex,
        )
    second = await publish(env, kind, "member-agent", first)
    async with env.factory() as db:
        model = ArtifactRecordRow if kind == "artifact" else DeliverableRow
        row = await db.get(model, second)
        assert (row.owner_email, row.owner_agent_id, row.author_email, row.author_agent_id) == (
            OWNER,
            "author",
            member,
            "member-agent",
        )
        grant = await db.scalar(
            sa.select(ArtifactGrantRow).where(
                ArtifactGrantRow.owner_email == OWNER,
                ArtifactGrantRow.content_ref == second,
                ArtifactGrantRow.agent_id == "writer",
                ArtifactGrantRow.source == "explicit",
            )
        )
        assert grant is not None and grant.permission == "write"
    assert await publish(env, kind, "writer", second) != second


async def test_populated_write_schema_bootstrap_and_migration_converge(lineage_env):
    env = lineage_env
    artifact = await publish(env, "artifact", "author")
    deliverable = await publish(env, "deliverable", "author")
    await share(env, "artifact", artifact, ["reader"])
    await share(env, "deliverable", deliverable, ["reader"])
    async with env.engine.begin() as connection:
        await connection.run_sync(rollback_document_write_schema)

        def upgrade(sync):
            migration = importlib.import_module(
                "cognis.store.migrations.versions.190_document_write_access"
            )
            ensure_artifact_access_schema(sync)
            ensure_artifact_access_schema(sync)
            with Operations.context(MigrationContext.configure(sync)):
                migration.upgrade()
                migration.upgrade()
            for table in ("artifacts", "deliverables"):
                actual = {column["name"]: column for column in sa.inspect(sync).get_columns(table)}
                assert actual["author_email"]["nullable"] is True
                assert actual["author_agent_id"]["nullable"] is True
            actual = {
                column["name"]: column for column in sa.inspect(sync).get_columns("artifact_grants")
            }
            assert actual["permission"]["nullable"] is False

        await connection.run_sync(upgrade)
    async with env.factory() as db:
        for model, ref in ((ArtifactRecordRow, artifact), (DeliverableRow, deliverable)):
            row = await db.get(model, ref)
            assert (row.owner_agent_id, row.author_email) == ("author", OWNER)
        grants = list(
            await db.scalars(
                sa.select(ArtifactGrantRow).where(
                    ArtifactGrantRow.agent_id == "reader", ArtifactGrantRow.source == "explicit"
                )
            )
        )
        assert {row.content_ref for row in grants} == {artifact, deliverable}
        assert {row.permission for row in grants} == {"read"}
