from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import event, select

from cognis.api.chat_v2 import routes
from cognis.api.chat_v2.schemas import TimelineScope, ToolCallTimelineItem
from cognis.api.chat_v2.work_graph import AuthorizedWorkGraphResolver
from cognis.api.chat_v2.work_materializer import WORK_MATERIALIZER_VERSION
from cognis.api.common import AuthenticatedUser
from cognis.api.routes.work import list_work_activities
from cognis.bootstrap import run_schema_bootstrap
from cognis.models.tool import ToolDefinition, ToolSource
from cognis.store import queries
from cognis.store.database import create_engine, create_session_factory
from cognis.store.models import (
    Agent,
    ConversationTodo,
    NotificationRow,
    Session,
    SessionTodo,
    SystemAgentOverride,
    User,
    WorkRecordRow,
    WorkSessionProjectionRow,
)
from cognis.store.work_live_invalidation import bump_live_work_revision, read_live_work_revision


async def _database(tmp_path: Path):
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path / 'live-work.db'}")
    factory = create_session_factory(engine)
    await run_schema_bootstrap(engine)
    async with factory() as db:
        db.add(User(email="owner@example.com", name="Owner", role="user"))
        await db.flush()
        db.add(
            Agent(
                agent_id="agent",
                owner_email="owner@example.com",
                name="Agent",
            )
        )
        await db.flush()
        conversation = await queries.create_conversation(
            db,
            conversation_id="conversation",
            user_email="owner@example.com",
            agent_id="agent",
            context_type="web",
        )
        session = await queries.create_session(
            db,
            session_id="session",
            conversation_id=conversation.conversation_id,
            user_email="owner@example.com",
            agent_id="agent",
            intaris_session_id="source",
        )
        await queries.update_conversation_active_session(
            db,
            conversation.conversation_id,
            session.session_id,
        )
        await db.commit()
    request = SimpleNamespace(
        state=SimpleNamespace(user=AuthenticatedUser(email="owner@example.com", role="user")),
        app=SimpleNamespace(
            state=SimpleNamespace(
                session_factory=factory,
                work_graph_resolver=AuthorizedWorkGraphResolver(factory),
                tool_registry=SimpleNamespace(
                    list_tools=lambda: [
                        ToolDefinition(
                            name="bash",
                            description="Run",
                            source=ToolSource(type="skill"),
                        )
                    ]
                ),
                artifact_store=None,
                work_materializer=None,
                chat_v2_cursor_secret="live-work-secret",
            )
        ),
    )
    context = {
        "scope": TimelineScope(
            key="conversation:conversation",
            kind="conversation",
            conversation_id="conversation",
        ),
        "cursor_secret": "live-work-secret",
    }
    return engine, factory, request, context


@pytest.mark.asyncio
async def test_activity_overview_retries_when_lifecycle_changes_after_graph_read(
    tmp_path: Path,
) -> None:
    engine, factory, request, context = await _database(tmp_path)
    resolver = request.app.state.work_graph_resolver
    resolve = resolver.resolve
    calls = 0

    async def resolve_with_terminal_race(**kwargs):
        nonlocal calls
        graph = await resolve(**kwargs)
        calls += 1
        if calls == 1:
            async with factory() as db:
                session = await db.get(Session, "session")
                assert session is not None
                session.status = "completed"
                await db.commit()
        return graph

    resolver.resolve = resolve_with_terminal_race
    try:
        overview = await routes._build_activity_overview(request, context)
    finally:
        await resolver.stop()
        await engine.dispose()

    assert calls == 2
    node = next(item for item in overview.workstreams if item.session_id == "session")
    assert node.execution_state == "completed"


@pytest.mark.asyncio
async def test_sidebar_visible_attention_and_todos_advance_owner_revision(
    tmp_path: Path,
) -> None:
    engine, factory, request, _context = await _database(tmp_path)
    try:
        async with factory() as db:
            baseline = await read_live_work_revision(db, "owner@example.com")
            db.add(
                NotificationRow(
                    notification_id="notification",
                    notification_type="gate",
                    user_email="owner@example.com",
                    conversation_id="conversation",
                    status="pending",
                )
            )
            await db.commit()
        async with factory() as db:
            after_notification = await read_live_work_revision(db, "owner@example.com")
            db.add(
                ConversationTodo(
                    conversation_id="conversation",
                    position=0,
                    content="Review lifecycle",
                    status="pending",
                )
            )
            await db.commit()
        async with factory() as db:
            after_todo = await read_live_work_revision(db, "owner@example.com")
            db.add(
                SessionTodo(
                    session_id="session",
                    position=0,
                    content="Inspect graph",
                    status="pending",
                )
            )
            await db.commit()
        async with factory() as db:
            after_session_todo = await read_live_work_revision(db, "owner@example.com")
            db.add(
                SystemAgentOverride(
                    override_id="override",
                    owner_email="owner@example.com",
                    agent_id="system:riker",
                    disabled=True,
                )
            )
            await db.commit()
        async with factory() as db:
            after_override = await read_live_work_revision(db, "owner@example.com")
    finally:
        await request.app.state.work_graph_resolver.stop()
        await engine.dispose()

    assert after_notification > baseline
    assert after_todo > after_notification
    assert after_session_todo > after_todo
    assert after_override > after_session_todo


@pytest.mark.asyncio
async def test_live_revision_bump_rolls_back_with_owning_transaction(tmp_path: Path) -> None:
    engine, factory, request, _context = await _database(tmp_path)
    try:
        async with factory() as db:
            baseline = await read_live_work_revision(db, "owner@example.com")
            assert await bump_live_work_revision(db, "owner@example.com") == baseline + 1
            await db.rollback()
        async with factory() as db:
            assert await read_live_work_revision(db, "owner@example.com") == baseline
    finally:
        await request.app.state.work_graph_resolver.stop()
        await engine.dispose()


@pytest.mark.asyncio
async def test_activity_list_uses_two_queries_and_live_projection_summary(
    tmp_path: Path,
) -> None:
    engine, factory, request, _context = await _database(tmp_path)
    queries_seen = 0

    def count_queries(*_args: object) -> None:
        nonlocal queries_seen
        queries_seen += 1

    event.listen(engine.sync_engine, "before_cursor_execute", count_queries)
    try:
        async with factory() as db:
            projection = await db.scalar(select(WorkSessionProjectionRow))
            assert projection is not None
            projection.command_count = 3
            projection.mutation_count = 2
            await db.commit()
        queries_seen = 0
        result = await list_work_activities(request, limit=20, cursor=None)
        assert queries_seen == 2
        assert len(result.items) == 1
        assert result.items[0].summary is not None
        assert result.items[0].summary.commands == 3
        assert result.items[0].summary.mutations == 2
        assert result.items[0].materialization == "live"
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", count_queries)
        await engine.dispose()


@pytest.mark.asyncio
async def test_new_session_is_live_and_first_projected_command_is_immediate(
    tmp_path: Path,
) -> None:
    engine, factory, request, context = await _database(tmp_path)
    try:
        empty_overview = await routes._build_activity_overview(request, context)
        empty_work = await routes._build_work_graph_projection(
            request,
            context,
            before=None,
            limit=50,
            category="commands",
            from_time=None,
            to_time=None,
            exact_session_id=None,
        )
        assert empty_overview.materialization.state == "live"
        assert empty_overview.summary.commands == 0
        assert len(empty_overview.workstreams) == 1
        assert empty_overview.workstreams[0].execution_state == "idle"
        assert empty_work.materialization.state == "live"
        assert empty_work.commands == []

        occurred_at = datetime.now(UTC)
        tool = ToolCallTimelineItem(
            id="tool:call",
            sort_key="0001:000000000001:tool",
            created_at=occurred_at.isoformat(),
            call_id="call",
            tool_name="bash",
            arguments={"command": "pwd"},
            status="complete",
        )
        async with factory() as db:
            projection = await db.scalar(
                select(WorkSessionProjectionRow).where(
                    WorkSessionProjectionRow.session_id == "session"
                )
            )
            assert projection is not None
            assert projection.next_head_check_at is not None
            projection.target_seq = 1
            projection.covered_through_seq = 1
            projection.state = "caught_up"
            db.add(
                WorkRecordRow(
                    work_record_id="record",
                    owner_email="owner@example.com",
                    session_id="session",
                    materializer_version=WORK_MATERIALIZER_VERSION,
                    source_store="intaris",
                    source_session_id="source",
                    source_seq=1,
                    source_item_id=tool.id,
                    item_ordinal=0,
                    occurred_at=occurred_at,
                    record_type="tool_pair",
                    category="commands",
                    entity_id="call",
                    call_id="call",
                    pairing_key="call",
                    timeline_item=tool.model_dump(mode="json"),
                )
            )
            await db.commit()

        live_work = await routes._build_work_graph_projection(
            request,
            context,
            before=None,
            limit=50,
            category="commands",
            from_time=None,
            to_time=None,
            exact_session_id=None,
        )
        assert live_work.materialization.state == "live"
        assert [command.call_id for command in live_work.commands] == ["call"], (
            live_work.model_dump()
        )
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_one_missing_projection_is_partial_without_blocking_live_sibling(
    tmp_path: Path,
) -> None:
    engine, factory, request, context = await _database(tmp_path)
    try:
        async with factory() as db:
            root = await queries.get_session_row(db, "session")
            assert root is not None
            root.activity_scope_id = "activity"
            await queries.create_session(
                db,
                session_id="child",
                conversation_id="conversation",
                user_email="owner@example.com",
                agent_id="agent",
                intaris_session_id="child-source",
                previous_session_id="session",
                activity_scope_id="activity",
            )
            await queries.update_conversation_active_session(
                db,
                "conversation",
                "child",
            )
            child_projection = await db.scalar(
                select(WorkSessionProjectionRow).where(
                    WorkSessionProjectionRow.session_id == "child"
                )
            )
            assert child_projection is not None
            await db.delete(child_projection)
            await db.commit()
        overview = await routes._build_activity_overview(request, context)
        assert overview.materialization.state == "partial"
        assert overview.materialization.completed_streams == 1
        assert overview.materialization.failed_streams == 1
        assert len(overview.workstreams) == 1
        assert overview.workstreams[0].backing_session_ids == ["child", "session"]
    finally:
        await engine.dispose()
