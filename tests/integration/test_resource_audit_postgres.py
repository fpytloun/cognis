"""Opt-in isolated PostgreSQL coverage for resource-audit query contracts."""

from __future__ import annotations

import asyncio
import os
from datetime import UTC, datetime, timedelta
from time import perf_counter
from types import SimpleNamespace
from uuid import uuid4

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from cognis.api.dashboard_issues import collect_dashboard_issues
from cognis.core.artifact_access import _work_nodes
from cognis.core.artifact_entitlements import content_recipients, register_content_entitlements
from cognis.core.controller_directory import ControllerInstanceDirectory
from cognis.core.controller_runtime import ControllerRuntime
from cognis.core.executor_pin_notice_dispatch import ExecutorPinNoticeDispatcher
from cognis.core.observability import ObservabilityService
from cognis.store.models import (
    Agent,
    ArtifactGrantRow,
    ArtifactRecordRow,
    Base,
    ControllerInstanceRow,
    Conversation,
    DeliverableRow,
    ExecutorPinNoticeOutboxRow,
    ExecutorPinTransitionRow,
    ExecutorRow,
    ManagedConversationLink,
    Session,
    StepRun,
    Task,
    User,
)
from cognis.store.observability import collect_observability_snapshot


@pytest.fixture(params=["sqlite", "postgresql"])
async def resource_db(request, tmp_path):
    url = (
        f"sqlite+aiosqlite:///{tmp_path}/resource.db"
        if request.param == "sqlite"
        else os.environ.get("COGNIS_RESOURCE_TEST_DATABASE_URL")
    )
    if not url:
        pytest.skip("requires an isolated COGNIS_RESOURCE_TEST_DATABASE_URL")
    admin = None
    if request.param == "postgresql":
        schema = f"resource_test_{uuid4().hex}"
        admin = create_async_engine(url)
        try:
            async with admin.begin() as connection:
                await connection.execute(sa.text(f'CREATE SCHEMA "{schema}"'))
        except BaseException:
            await admin.dispose()
            raise
        engine = create_async_engine(url, connect_args={"server_settings": {"search_path": schema}})
    else:
        engine = create_async_engine(url)
    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        yield async_sessionmaker(engine, expire_on_commit=False)
    finally:
        await engine.dispose()
        if admin is not None:
            try:
                async with admin.begin() as connection:
                    await connection.execute(sa.text(f'DROP SCHEMA "{schema}" CASCADE'))
            finally:
                await admin.dispose()


async def test_inventory_postgresql(resource_db):
    async with resource_db() as session:
        snapshot = await collect_observability_snapshot(session)
    assert any(name == "cognis_turns_active" for name, *_ in snapshot.metric_rows)


async def test_inventory_lsp_uses_configured_label_fallback(resource_db):
    async with resource_db() as session:
        session.add(
            ExecutorRow(
                executor_id="audit-label",
                name="Builder (Linux)",
                executor_type="websocket",
                runtime_state="ready",
            )
        )
        await session.flush()
        snapshot = await collect_observability_snapshot(session)
        assert snapshot.executor_slugs["audit-label"] == ("audit-label", "shared")
        state_rows = next(
            rows for name, _, _, rows in snapshot.metric_rows if name == "cognis_executor_state"
        )
        assert any(
            labels["executor"] == "audit-label" and labels["user"] == "shared"
            for labels, _ in state_rows
        )

        class Provider:
            async def get_lsp_statuses(self):
                return [
                    SimpleNamespace(
                        executor_id="audit-label", executor_type="websocket", state="ready"
                    )
                ]

        service = ObservabilityService(
            resource_db, owner_id="audit:labels", lsp_provider=Provider()
        )
        rows = await service._collect_lsp(snapshot)
        assert [(row.executor, row.user) for row in rows] == [("audit-label", "shared")]
        await session.rollback()


async def test_postgresql_connections_have_component_attribution(resource_db):
    from cognis.store.database import create_engine

    url = resource_db.kw["bind"].url
    if url.get_backend_name() != "postgresql":
        return
    engine = create_engine(url.render_as_string(hide_password=False))
    try:
        async with engine.connect() as connection:
            name = await connection.scalar(sa.text("SHOW application_name"))
            assert name == f"cognis-controller:{os.getpid()}"
    finally:
        await engine.dispose()


async def test_inventory_refresh_and_partial_failure_do_not_publish_zero(resource_db, caplog):
    service = ObservabilityService(resource_db, owner_id="audit:inventory")
    assert await service.refresh_once()
    assert service.state.snapshot is not None
    lease = service._lease
    engine = resource_db.kw["bind"]

    def fail_family(conn, cursor, statement, parameters, context, executemany):
        if "FROM channel_accounts" in statement:
            raise RuntimeError("sensitive database payload must never be logged")

    sa.event.listen(engine.sync_engine, "before_cursor_execute", fail_family)
    try:
        assert not await service.refresh_once()
        assert service.state.snapshot is None
        assert service.state.owner is False
        assert not any(
            metric.name == "cognis_turns_active" for metric in service.collector.collect()
        )
        assert "sensitive database payload" not in caplog.text
        record = next(
            record
            for record in caplog.records
            if record.message == "observability inventory collection failed"
        )
        assert record.extra_data == {"stage": "cognis_channel_accounts", "category": "RuntimeError"}
    finally:
        sa.event.remove(engine.sync_engine, "before_cursor_execute", fail_family)
        await service._lease_store.release(lease)
        await service.stop()


async def test_dashboard_projects_presence_without_hydrating_inventory(resource_db):
    async with resource_db() as db:
        db.add(User(email="projection@example.com", name="Projection"))
        await db.flush()
        for name, tools in [
            ("sql-null", sa.null()),
            ("json-null", None),
            ("empty", []),
            ("large", [{"description": "x" * 1_000_000}]),
        ]:
            db.add(
                ExecutorRow(
                    executor_id=name,
                    name=name,
                    owner_email="projection@example.com",
                    executor_type="websocket",
                    runtime_state="active",
                    last_observed_at=datetime.now(UTC),
                    observed_tools=tools,
                )
            )
        await db.flush()
        db.expunge_all()
        statements = []
        connection = await db.connection()

        def capture(conn, cursor, statement, parameters, context, executemany):
            statements.append(statement)

        sa.event.listen(connection.sync_connection, "before_cursor_execute", capture)
        try:
            issues = await collect_dashboard_issues(db, user_email="projection@example.com")
        finally:
            sa.event.remove(connection.sync_connection, "before_cursor_execute", capture)
        assert {issue.resource.id for issue in issues.issues} == {"sql-null", "json-null"}
        assert not any(isinstance(item, ExecutorRow) for item in db.identity_map.values())
        executor_queries = [query for query in statements if "FROM executors" in query]
        assert len(executor_queries) == 1
        assert "AS has_observed_tools" in executor_queries[0]
        assert "executors.config" not in executor_queries[0]
        assert "CAST(executors.observed_tools" not in executor_queries[0]
        await db.rollback()


async def test_directory_batch_preserves_readiness_expiry_and_exact_owner(resource_db):
    owners = ["audit:ready", "audit:expired", "audit:draining"]
    now = datetime.now(UTC)
    async with resource_db() as db:
        for owner in owners:
            suffix = owner.split(":")[1]
            db.add(
                ControllerInstanceRow(
                    owner_id=owner,
                    controller_id="audit",
                    incarnation_id=suffix,
                    internal_url="http://127.0.0.1:8000",
                    lifecycle_state="draining" if suffix == "draining" else "ready",
                    heartbeat_at=now,
                    expires_at=now + timedelta(seconds=-60 if suffix == "expired" else 60),
                )
            )
        await db.commit()
    try:
        directory = ControllerInstanceDirectory(
            resource_db,
            ControllerRuntime(instance_id="audit", incarnation_id="local"),
            internal_url=None,
        )
        assert set(await directory.get_ready_many(owners + ["audit:old"])) == {"audit:ready"}
        assert await directory.get_ready("audit:old") is None
        assert await directory.get_ready_many([]) == {}
    finally:
        async with resource_db() as db:
            await db.execute(
                sa.delete(ControllerInstanceRow).where(ControllerInstanceRow.owner_id.in_(owners))
            )
            await db.commit()


@pytest.mark.parametrize("boundary", ["task_delivery", "knowledgebase"])
async def test_read_transaction_released_while_dependency_stalls(
    resource_db, monkeypatch, boundary
):
    from cognis.core.workflow_engine import WorkflowEngine
    from cognis.knowledgebase.service import KnowledgebaseService

    entered = asyncio.Event()
    checked_out = 0
    engine = resource_db.kw["bind"]

    def checkout(*args):
        nonlocal checked_out
        checked_out += 1

    def checkin(*args):
        nonlocal checked_out
        checked_out -= 1

    async def stalled(*args, **kwargs):
        entered.set()
        await asyncio.Event().wait()

    sa.event.listen(engine.sync_engine, "checkout", checkout)
    sa.event.listen(engine.sync_engine, "checkin", checkin)
    if boundary == "task_delivery":
        from cognis.models.task import TaskModel

        async def latest(session, *args):
            await session.execute(sa.select(1))
            return SimpleNamespace(session_id="audit", intaris_session_id="audit-intaris")

        monkeypatch.setattr(
            "cognis.store.queries.get_latest_active_session_for_conversation", latest
        )
        runtime = SimpleNamespace(
            _session_factory=resource_db,
            _providers=SimpleNamespace(guardrails=SimpleNamespace(record_events=stalled)),
        )
        task = TaskModel(
            status="completed",
            task_id="audit",
            title="Audit",
            created_by="audit@example.com",
            agent_id="audit-agent",
            result_summary="",
            result_data={},
        )
        operation = WorkflowEngine._deliver_task_result_default(runtime, task, "audit")
    else:

        async def resolve(session, **kwargs):
            await session.execute(sa.select(1))
            return object()

        async def chunk(*args, **kwargs):
            return SimpleNamespace(
                chunk_id="audit",
                artifact_id="audit",
                text="text",
                locator={"char_start": 0, "char_end": 4},
                artifact_hash=None,
            )

        async def artifact(*args):
            return SimpleNamespace(
                status="attached",
                content_hash=None,
                namespace="artifacts",
                object_id="audit",
                filename="audit.txt",
                mime_type="text/plain",
            )

        monkeypatch.setattr("cognis.knowledgebase.service.resolve_knowledgebase_access", resolve)
        monkeypatch.setattr("cognis.knowledgebase.service.get_knowledgebase_chunk", chunk)
        monkeypatch.setattr("cognis.knowledgebase.service.get_artifact_record", artifact)
        service = KnowledgebaseService(
            session_factory=resource_db,
            llm=None,
            vector_backend=None,
            enabled=True,
            artifact_store=SimpleNamespace(async_load=stalled),
        )
        operation = service.source_context(
            owner_email="audit@example.com", knowledgebase_id="audit", chunk_id="audit"
        )
    running = asyncio.create_task(operation)
    try:
        await asyncio.wait_for(entered.wait(), 5)
        assert checked_out == 0
        running.cancel()
        with pytest.raises(asyncio.CancelledError):
            await running
        assert checked_out == 0
    finally:
        if not running.done():
            running.cancel()
            await asyncio.gather(running, return_exceptions=True)
        sa.event.remove(engine.sync_engine, "checkout", checkout)
        sa.event.remove(engine.sync_engine, "checkin", checkin)


@pytest.mark.parametrize("failure", ["cancel", "commit", "concurrent"])
async def test_notice_split_transactions_retry_and_concurrency(resource_db, failure):
    async with resource_db() as db:
        db.add(User(email="notice@example.com", name="Notice"))
        await db.flush()
        db.add(
            Agent(agent_id="audit-notice-agent", owner_email="notice@example.com", name="Notice")
        )
        await db.flush()
        db.add(
            Conversation(
                conversation_id="audit",
                user_email="notice@example.com",
                agent_id="audit-notice-agent",
                context_type="agent_direct",
            )
        )
        await db.flush()
        db.add(
            ExecutorPinTransitionRow(
                transition_id="audit-transition",
                scope_type="conversation",
                scope_id="audit",
                generation=1,
                old_executor_id="old",
                new_executor_id="new",
                reason="unavailable",
                state_caveat="executor-local state is not migrated",
                notice_id="audit-notice",
            )
        )
        await db.flush()
        db.add(
            ExecutorPinNoticeOutboxRow(
                outbox_id="audit-outbox",
                transition_id="audit-transition",
                conversation_id="audit",
                intaris_session_id="audit-intaris",
                idempotency_key="audit-intaris:executor_failover:audit-notice",
                payload={},
            )
        )
        await db.commit()

    engine = resource_db.kw["bind"]
    entered, release = asyncio.Event(), asyncio.Event()
    keys = []
    canonical_appends = set()

    async def record_events(**kwargs):
        key = kwargs["idempotency_key"]
        keys.append(key)
        canonical_appends.add(key)
        if failure != "concurrent" or len(keys) == 2:
            entered.set()
        await release.wait()
        return SimpleNamespace(ok=True)

    dispatcher = ExecutorPinNoticeDispatcher(
        session_factory=resource_db, guardrails=SimpleNamespace(record_events=record_events)
    )
    running = []

    def fail_commit(connection):
        raise RuntimeError("marker commit interrupted")

    try:
        running.append(asyncio.create_task(dispatcher.dispatch_one("audit-outbox")))
        if failure == "concurrent":
            running.append(asyncio.create_task(dispatcher.dispatch_one("audit-outbox")))
        await asyncio.wait_for(entered.wait(), 5)
        async with resource_db() as db:
            assert (await db.get(ExecutorPinNoticeOutboxRow, "audit-outbox")).delivered_at is None
            assert (
                await db.get(ExecutorPinTransitionRow, "audit-transition")
            ).notice_appended_at is None
        if failure == "cancel":
            running[0].cancel()
            with pytest.raises(asyncio.CancelledError):
                await running[0]
            release.set()
        elif failure == "commit":
            sa.event.listen(sa.orm.Session, "before_commit", fail_commit)
            release.set()
            try:
                with pytest.raises(RuntimeError, match="marker commit interrupted"):
                    await running[0]
            finally:
                sa.event.remove(sa.orm.Session, "before_commit", fail_commit)
        else:
            release.set()
            assert await asyncio.wait_for(asyncio.gather(*running), 5) == [True, True]

        if failure != "concurrent":
            async with resource_db() as db:
                assert (
                    await db.get(ExecutorPinNoticeOutboxRow, "audit-outbox")
                ).delivered_at is None
                assert (
                    await db.get(ExecutorPinTransitionRow, "audit-transition")
                ).notice_appended_at is None
            assert await dispatcher.dispatch_one("audit-outbox")
        async with resource_db() as db:
            delivered = (await db.get(ExecutorPinNoticeOutboxRow, "audit-outbox")).delivered_at
            appended = (
                await db.get(ExecutorPinTransitionRow, "audit-transition")
            ).notice_appended_at
            assert delivered is not None and appended is not None
        assert keys == ["audit-intaris:executor_failover:audit-notice"] * 2
        assert len(canonical_appends) == 1
        assert await dispatcher.dispatch_one("audit-outbox")
        assert len(keys) == 2
        async with resource_db() as db:
            assert (
                await db.get(ExecutorPinNoticeOutboxRow, "audit-outbox")
            ).delivered_at == delivered
    finally:
        for task in running:
            if not task.done():
                task.cancel()
        await asyncio.gather(*running, return_exceptions=True)
        # Teardown uses Core so deleting the owner does not enqueue an ORM
        # live-work revision for that now-absent test user.
        async with engine.begin() as db:
            await db.execute(
                sa.delete(ExecutorPinNoticeOutboxRow).where(
                    ExecutorPinNoticeOutboxRow.outbox_id == "audit-outbox"
                )
            )
            await db.execute(
                sa.delete(ExecutorPinTransitionRow).where(
                    ExecutorPinTransitionRow.transition_id == "audit-transition"
                )
            )
            await db.execute(sa.delete(Conversation).where(Conversation.conversation_id == "audit"))
            await db.execute(sa.delete(Agent).where(Agent.agent_id == "audit-notice-agent"))
            await db.execute(sa.delete(User).where(User.email == "notice@example.com"))


async def seed_graph(db, *, agents=4, sessions=12):
    owner, foreign = "audit@example.com", "foreign@example.com"
    db.add_all([User(email=owner, name="Audit"), User(email=foreign, name="Foreign")])
    await db.flush()
    db.add_all(
        [Agent(agent_id=f"a{i}", owner_email=owner, name=f"Agent {i}") for i in range(agents)]
        + [Agent(agent_id="foreign", owner_email=foreign, name="Foreign")]
    )
    await db.flush()
    db.add_all(
        [
            Conversation(
                conversation_id=f"c{i}",
                user_email=owner,
                agent_id=f"a{i}",
                context_type="agent_direct",
            )
            for i in range(agents)
        ]
        + [
            Conversation(
                conversation_id="foreign",
                user_email=foreign,
                agent_id="foreign",
                context_type="agent_direct",
            )
        ]
    )
    await db.flush()
    for i in range(sessions):
        db.add(
            Session(
                session_id=f"s{i}",
                conversation_id=f"c{i % agents}",
                user_email=owner,
                agent_id=f"a{i % agents}",
                source_session_id=f"s{i - 1}" if i else None,
                previous_session_id=f"s{i - 2}" if i > 1 else None,
            )
        )
    await db.flush()
    # Corrupt cycle and cross-tenant ancestry must preserve the forward graph's deny rules.
    (await db.get(Session, "s0")).source_session_id = f"s{sessions - 1}"
    db.add(
        Session(
            session_id="foreign",
            conversation_id="foreign",
            user_email=foreign,
            agent_id="foreign",
            source_session_id="s0",
        )
    )
    db.add(
        Task(
            task_id="task",
            created_by=owner,
            agent_id="a1",
            created_by_agent_id="a2",
            title="Audit",
            source_session_id="s0",
        )
    )
    await db.flush()
    db.add(
        StepRun(
            step_run_id="step",
            task_id="task",
            step_name="execute",
            step_type="direct",
            agent_id="a1",
            session_id=f"s{sessions - 1}",
        )
    )
    db.add(
        ManagedConversationLink(
            link_id="root",
            root_link_id="root",
            depth=1,
            user_email=owner,
            controller_agent_id="a0",
            controller_conversation_id="c0",
            controller_session_id="s0",
            target_agent_id="a1",
            target_conversation_id="c1",
        )
    )
    await db.flush()
    db.add(
        ManagedConversationLink(
            link_id="child",
            parent_link_id="root",
            root_link_id="root",
            depth=2,
            user_email=owner,
            controller_agent_id="a1",
            controller_conversation_id="c1",
            target_agent_id="a2",
            target_conversation_id="c2",
        )
    )
    await db.flush()
    return owner


def forward_recipients(db, row):
    """Independent pre-change registration oracle using the read-side forward closure."""
    result = set()
    for agent in db.scalars(
        sa.select(Agent.agent_id).where(
            Agent.owner_email == row.owner_email, ~Agent.agent_id.startswith("system:")
        )
    ):
        nodes = _work_nodes(owner_email=row.owner_email, agent_id=agent, session_id=None)
        conditions = []
        if row.session_id is not None:
            conditions.append(sa.literal("s:" + row.session_id).in_(sa.select(nodes.c.node)))
        elif row.conversation_id is not None:
            conditions.append(sa.literal("c:" + row.conversation_id).in_(sa.select(nodes.c.node)))
        if isinstance(row, DeliverableRow) and row.step_run_id is not None:
            conditions.append(
                sa.literal(row.step_run_id).in_(
                    sa.select(StepRun.step_run_id).where(
                        (sa.literal("t:") + StepRun.task_id).in_(sa.select(nodes.c.node))
                    )
                )
            )
        if conditions and db.scalar(sa.select(sa.or_(*conditions))):
            result.add(agent)
    return result


@pytest.mark.parametrize("corruption", ["none", "depth", "agent", "tenant", "root", "cycle"])
async def test_recipient_differential(resource_db, corruption):
    async with resource_db() as db:
        owner = await seed_graph(db)
        if corruption != "cycle":
            await db.execute(
                sa.update(Session)
                .where(Session.user_email == owner)
                .values(source_session_id=None, previous_session_id=None)
            )
        link = await db.get(ManagedConversationLink, "child")
        if corruption == "depth":
            link.depth = 3
        elif corruption == "agent":
            link.controller_agent_id = "a3"
        elif corruption == "tenant":
            link.user_email = "foreign@example.com"
        elif corruption == "root":
            link.root_link_id = "child"
        await db.flush()
        for cls in (ArtifactRecordRow, DeliverableRow):
            for session_id, conversation_id in [
                ("s11", "c3"),
                (None, "c2"),
                ("foreign", "foreign"),
                (None, "foreign"),
                (None, None),
            ]:
                row = cls(owner_email=owner, session_id=session_id, conversation_id=conversation_id)
                if cls is DeliverableRow:
                    row.step_run_id = "step"
                expected = await db.run_sync(forward_recipients, row)
                actual = set(await db.run_sync(content_recipients, row))
                assert actual == expected
                assert "foreign" not in actual
                if cls is ArtifactRecordRow and session_id is None and conversation_id == "c2":
                    assert actual == (
                        {"a0", "a1", "a2", "a3"}
                        if corruption == "cycle"
                        else {"a0", "a1", "a2"}
                        if corruption == "none"
                        else {"a2"}
                    )
        await db.rollback()


async def test_registration_revision_retry_and_producer_deletion(resource_db):
    async with resource_db() as db:
        owner = await seed_graph(db)
        row = ArtifactRecordRow(
            artifact_id="art_audit",
            namespace="artifacts",
            object_id="audit",
            filename="audit.txt",
            mime_type="text/plain",
            owner_email=owner,
            owner_agent_id="a3",
            session_id="s11",
        )
        db.add(row)
        await db.flush()
        await db.run_sync(register_content_entitlements, row)
        await db.run_sync(register_content_entitlements, row)
        db.add(
            ArtifactGrantRow(
                owner_email=owner, content_ref=row.artifact_id, agent_id="a2", source="explicit"
            )
        )
        await db.flush()
        revision = ArtifactRecordRow(
            artifact_id="art_revision",
            namespace="artifacts",
            object_id="revision",
            filename="audit.txt",
            mime_type="text/plain",
            owner_email=owner,
            owner_agent_id="a3",
            session_id="s11",
            supersedes_artifact_id=row.artifact_id,
        )
        db.add(revision)
        await db.flush()
        await db.run_sync(register_content_entitlements, revision)
        await db.flush()
        grants = set(
            (
                await db.execute(
                    sa.select(ArtifactGrantRow.agent_id, ArtifactGrantRow.source).where(
                        ArtifactGrantRow.content_ref == revision.artifact_id
                    )
                )
            ).all()
        )
        assert ("a2", "explicit") in grants
        assert {(f"a{i}", "output") for i in range(4)} <= grants
        await db.delete(await db.get(Session, "s11"))
        await db.flush()
        assert (
            set(
                (
                    await db.execute(
                        sa.select(ArtifactGrantRow.agent_id, ArtifactGrantRow.source).where(
                            ArtifactGrantRow.content_ref == revision.artifact_id
                        )
                    )
                ).all()
            )
            == grants
        )
        await db.rollback()


@pytest.mark.parametrize(("agents", "sessions"), [(4, 40), (20, 40), (20, 200)])
async def test_recipient_query_benchmark(resource_db, agents, sessions):
    async with resource_db() as db:
        owner = await seed_graph(db, agents=agents, sessions=sessions)
        row = ArtifactRecordRow(owner_email=owner, session_id=f"s{sessions - 1}")
        start = perf_counter()
        before = await db.run_sync(forward_recipients, row)
        old_seconds = perf_counter() - start
        start = perf_counter()
        after = set(await db.run_sync(content_recipients, row))
        new_seconds = perf_counter() - start
        assert before == after
        print(
            f"recipient benchmark agents={agents} sessions={sessions} "
            f"forward={old_seconds:.6f}s reverse={new_seconds:.6f}s"
        )
        await db.rollback()
