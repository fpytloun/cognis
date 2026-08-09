from __future__ import annotations

from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine as create_sync_engine
from sqlalchemy import inspect

from cognis.api.chat_v2.schemas import TimelineScope, WorkstreamRef
from cognis.api.chat_v2.work_graph import AuthorizedWorkGraph
from cognis.api.chat_v2.work_revisions import (
    advance_work_revisions_for_stream,
    reconcile_work_scope_revision,
)
from cognis.bootstrap import run_schema_bootstrap
from cognis.store.database import create_engine, create_session_factory
from cognis.store.models import Agent, Conversation, Session, User


def test_migration_119_upgrades_current_head_with_work_indexes(tmp_path: Path) -> None:
    database_path = tmp_path / "migration-119.db"
    config = Config("cognis/store/migrations/alembic.ini")
    config.set_main_option("sqlalchemy.url", f"sqlite:///{database_path}")
    config.config_file_name = None

    command.upgrade(config, "119_work_scope_revisions")

    engine = create_sync_engine(f"sqlite:///{database_path}")
    try:
        inspector = inspect(engine)
        assert {"work_scope_states", "work_scope_streams"} <= set(inspector.get_table_names())
        assert {
            "ix_sessions_owner_parent_session",
            "ix_sessions_owner_previous_session",
        } <= {index["name"] for index in inspector.get_indexes("sessions")}
        assert "ix_managed_conversation_links_owner_controller_session" in {
            index["name"] for index in inspector.get_indexes("managed_conversation_links")
        }
    finally:
        engine.dispose()


@pytest.mark.asyncio
async def test_revisions_are_monotonic_and_duplicate_append_is_idempotent(tmp_path: Path) -> None:
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path / 'work-revisions.db'}")
    factory = create_session_factory(engine)
    await run_schema_bootstrap(engine)
    async with factory() as db:
        db.add(User(email="owner@example.com", name="Owner", password_hash="x", role="user"))
        await db.flush()
        db.add(
            Agent(
                agent_id="agent-1",
                owner_email="owner@example.com",
                name="Agent",
                description="Agent",
            )
        )
        await db.flush()
        db.add(
            Conversation(
                conversation_id="conv-1",
                user_email="owner@example.com",
                agent_id="agent-1",
                context_type="web",
                active_session_id="session-1",
            )
        )
        await db.flush()
        db.add(
            Session(
                session_id="session-1",
                conversation_id="conv-1",
                user_email="owner@example.com",
                agent_id="agent-1",
                intaris_session_id="stream-1",
                delegation_metadata={},
            )
        )
        await db.commit()

    scope = TimelineScope(
        key="conversation:conv-1",
        kind="conversation",
        conversation_id="conv-1",
    )
    node = WorkstreamRef(
        key="session:session-1",
        kind="root",
        root_key="session:session-1",
        edge_kind="root",
        ordinal=0,
        conversation_id="conv-1",
        session_id="session-1",
        event_store_session_id="stream-1",
        title="Root",
        agent_id="agent-1",
        status="active",
    )
    async with factory() as db:
        row = await db.get(Session, "session-1")
        assert row is not None
        graph = AuthorizedWorkGraph(
            nodes=(node,),
            session_rows=(row,),
            fingerprint="graph-a",
            truncated=False,
        )
        initial = await reconcile_work_scope_revision(
            db,
            user_email="owner@example.com",
            scope=scope,
            graph=graph,
            stream_watermarks={"stream-1": 10},
        )
        await db.commit()
    assert initial.graph_revision == 1
    assert initial.work_revision == 11

    async with factory() as db:
        row = await db.get(Session, "session-1")
        assert row is not None
        reparented = await reconcile_work_scope_revision(
            db,
            user_email="owner@example.com",
            scope=scope,
            graph=AuthorizedWorkGraph(
                nodes=(
                    node.model_copy(
                        update={
                            "parent_key": "session:new-parent",
                            "edge_kind": "delegate",
                        }
                    ),
                ),
                session_rows=(row,),
                fingerprint="graph-b",
                truncated=False,
            ),
            stream_watermarks={"stream-1": 10},
        )
        await db.commit()
    assert reparented.graph_revision == 2
    assert reparented.work_revision == 12

    async with factory() as db:
        first = await advance_work_revisions_for_stream(
            db,
            user_email="owner@example.com",
            event_store_id="intaris",
            event_store_session_id="stream-1",
            last_seq=12,
        )
        await db.commit()
    assert first[0].work_revision == 14

    async with factory() as db:
        duplicate = await advance_work_revisions_for_stream(
            db,
            user_email="owner@example.com",
            event_store_id="intaris",
            event_store_session_id="stream-1",
            last_seq=12,
        )
        await db.commit()
    assert duplicate == []

    async with factory() as db:
        publication_retry = await advance_work_revisions_for_stream(
            db,
            user_email="owner@example.com",
            event_store_id="intaris",
            event_store_session_id="stream-1",
            last_seq=12,
            include_current=True,
        )
        await db.commit()
    assert publication_retry[0].work_revision == 14

    async with factory() as db:
        db.add(
            Session(
                session_id="session-old",
                conversation_id="conv-1",
                user_email="owner@example.com",
                agent_id="agent-1",
                intaris_session_id="stream-old",
                delegation_metadata={},
            )
        )
        await db.commit()
    older_node = node.model_copy(
        update={
            "key": "session:session-old",
            "kind": "rotation",
            "edge_kind": "rotation",
            "session_id": "session-old",
            "event_store_session_id": "stream-old",
            "current": False,
            "ordinal": 1,
        }
    )
    async with factory() as db:
        current_row = await db.get(Session, "session-1")
        older_row = await db.get(Session, "session-old")
        assert current_row is not None
        assert older_row is not None
        historical = await reconcile_work_scope_revision(
            db,
            user_email="owner@example.com",
            scope=scope,
            graph=AuthorizedWorkGraph(
                nodes=(node, older_node),
                session_rows=(current_row, older_row),
                fingerprint="graph-c",
                truncated=False,
            ),
            stream_watermarks={"stream-1": 12, "stream-old": 4},
        )
        await db.commit()
    assert historical.graph_revision == 3
    assert historical.work_revision == 19

    async with factory() as db:
        historical_append = await advance_work_revisions_for_stream(
            db,
            user_email="owner@example.com",
            event_store_id="intaris",
            event_store_session_id="stream-old",
            last_seq=6,
        )
        await db.commit()
    assert historical_append[0].work_revision == 21
