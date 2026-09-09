from __future__ import annotations

import asyncio
import os
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select
from sqlalchemy import schema as sa_schema
from sqlalchemy.ext.asyncio import create_async_engine

from cognis.api.chat_v2.work_materializer import (
    WORK_MATERIALIZER_VERSION,
    WORK_MAX_REPAIR_CONCURRENCY,
    WORK_VISIBLE_REFRESH_PRIORITY,
    WorkMaterializer,
)
from cognis.knowledgebase.indexer import KnowledgebaseIndexer
from cognis.store.database import create_session_factory
from cognis.store.models import (
    Agent,
    Base,
    Conversation,
    KnowledgebaseIndexJobRow,
    Session,
    User,
    WorkSessionProjectionRow,
)

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not os.environ.get("COGNIS_TEST_POSTGRES_URL"),
        reason="COGNIS_TEST_POSTGRES_URL is not configured",
    ),
]


def _url() -> str:
    url = os.environ["COGNIS_TEST_POSTGRES_URL"]
    return (
        url.replace("postgresql://", "postgresql+asyncpg://", 1)
        if url.startswith("postgresql://")
        else url
    )


def _indexer(factory, owner_id: str) -> KnowledgebaseIndexer:
    return KnowledgebaseIndexer(
        session_factory=factory,
        artifact_store=object(),
        llm=object(),
        vector_backend=object(),
        enabled=True,
        poll_interval_seconds=0.01,
        max_artifact_size_bytes=1024,
        max_chunks_per_artifact=10,
        chunk_target_tokens=100,
        chunk_overlap_tokens=10,
        embedding_batch_size=2,
        controller_owner_id=owner_id,
    )


@pytest.mark.asyncio
async def test_postgres_two_kb_indexers_claim_one_job() -> None:
    url = _url()
    schema_name = f"cognis_ha_workers_{uuid.uuid4().hex}"
    admin = create_async_engine(url)
    async with admin.begin() as connection:
        await connection.execute(sa_schema.CreateSchema(schema_name))
    engine = create_async_engine(
        url,
        connect_args={"server_settings": {"search_path": f'"{schema_name}"'}},
    )
    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        factory = create_session_factory(engine)
        async with factory() as session:
            session.add(
                KnowledgebaseIndexJobRow(
                    job_id="kbj-pg-1",
                    knowledgebase_id="kb-pg-1",
                    job_type="delete_artifact_index",
                    status="queued",
                )
            )
            await session.commit()

        results = await asyncio.gather(
            _indexer(factory, "controller-a").run_once(),
            _indexer(factory, "controller-b").run_once(),
        )
        assert sorted(results) == [False, True]
        async with factory() as session:
            row = await session.get(KnowledgebaseIndexJobRow, "kbj-pg-1")
            assert row is not None
            assert row.status == "succeeded"
            assert row.attempts == 1
    finally:
        await engine.dispose()
        async with admin.begin() as connection:
            await connection.execute(sa_schema.DropSchema(schema_name, cascade=True))
        await admin.dispose()


@pytest.mark.asyncio
async def test_postgres_two_materializers_share_one_urgent_repair_lane() -> None:
    url = _url()
    schema_name = f"cognis_ha_work_{uuid.uuid4().hex}"
    admin = create_async_engine(url)
    async with admin.begin() as connection:
        await connection.execute(sa_schema.CreateSchema(schema_name))
    engine = create_async_engine(
        url,
        connect_args={"server_settings": {"search_path": f'"{schema_name}"'}},
    )
    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        factory = create_session_factory(engine)
        first = WorkMaterializer(
            session_factory=factory,
            event_store=object(),  # type: ignore[arg-type]
            tool_definitions=lambda: {},
            worker_id="controller-a",
        )
        second = WorkMaterializer(
            session_factory=factory,
            event_store=object(),  # type: ignore[arg-type]
            tool_definitions=lambda: {},
            worker_id="controller-b",
        )
        urgent_projection_ids: list[str] = []
        async with factory() as session:
            session.add(User(email="owner@example.com", name="Owner", role="user"))
            await session.flush()
            session.add(
                Agent(
                    agent_id="agent-owner",
                    owner_email="owner@example.com",
                    name="Owner",
                )
            )
            await session.flush()
            session.add(
                Conversation(
                    conversation_id="conversation-work",
                    user_email="owner@example.com",
                    agent_id="agent-owner",
                    context_type="web",
                )
            )
            await session.flush()
            for index in range(4):
                session_id = f"session-{index}"
                projection_id = f"projection-{index}"
                session.add(
                    Session(
                        session_id=session_id,
                        conversation_id="conversation-work",
                        user_email="owner@example.com",
                        agent_id="agent-owner",
                        intaris_session_id=f"source-{index}",
                    )
                )
                session.add(
                    WorkSessionProjectionRow(
                        projection_id=projection_id,
                        owner_email="owner@example.com",
                        session_id=session_id,
                        source_session_id=f"source-{index}",
                        materializer_version=WORK_MATERIALIZER_VERSION,
                        state="repair" if index < 2 else "caught_up",
                        target_seq=1 if index < 2 else 0,
                        covered_through_seq=0,
                        priority=0,
                        next_head_check_at=(
                            None if index < 2 else datetime.now(UTC) + timedelta(hours=1)
                        ),
                    )
                )
                if index >= 2:
                    urgent_projection_ids.append(projection_id)
            await session.commit()

        assert len(await first._claim()) == 2
        async with factory() as session:
            urgent_rows = list(
                (
                    await session.scalars(
                        select(WorkSessionProjectionRow).where(
                            WorkSessionProjectionRow.projection_id.in_(urgent_projection_ids)
                        )
                    )
                ).all()
            )
            assert len(urgent_rows) == 2
            for row in urgent_rows:
                row.state = "repair"
                row.priority = WORK_VISIBLE_REFRESH_PRIORITY
                row.next_head_check_at = datetime.now(UTC)
            await session.commit()

        claims = await asyncio.gather(
            first._claim_urgent(urgent_projection_ids[0]),
            second._claim_urgent(urgent_projection_ids[1]),
        )

        assert sum(claim is not None for claim in claims) == 1
        async with factory() as session:
            active = int(
                await session.scalar(
                    select(func.count())
                    .select_from(WorkSessionProjectionRow)
                    .where(WorkSessionProjectionRow.lease_owner.is_not(None))
                )
                or 0
            )
            urgent_active = int(
                await session.scalar(
                    select(func.count())
                    .select_from(WorkSessionProjectionRow)
                    .where(WorkSessionProjectionRow.lease_owner.like("work-urgent:%"))
                )
                or 0
            )
        assert active == WORK_MAX_REPAIR_CONCURRENCY == 3
        assert urgent_active == 1
    finally:
        await engine.dispose()
        async with admin.begin() as connection:
            await connection.execute(sa_schema.DropSchema(schema_name, cascade=True))
        await admin.dispose()


@pytest.mark.asyncio
async def test_postgres_two_materializers_claim_stale_caught_up_lag_once() -> None:
    url = _url()
    schema_name = f"cognis_ha_stale_work_{uuid.uuid4().hex}"
    admin = create_async_engine(url)
    async with admin.begin() as connection:
        await connection.execute(sa_schema.CreateSchema(schema_name))
    engine = create_async_engine(
        url,
        connect_args={"server_settings": {"search_path": f'"{schema_name}"'}},
    )
    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        factory = create_session_factory(engine)
        workers = [
            WorkMaterializer(
                session_factory=factory,
                event_store=object(),  # type: ignore[arg-type]
                tool_definitions=lambda: {},
                worker_id=worker_id,
            )
            for worker_id in ("controller-a", "controller-b")
        ]
        async with factory() as session:
            session.add(User(email="owner@example.com", name="Owner", role="user"))
            await session.flush()
            session.add(
                Agent(
                    agent_id="agent-owner",
                    owner_email="owner@example.com",
                    name="Owner",
                )
            )
            await session.flush()
            session.add(
                Conversation(
                    conversation_id="conversation-stale-work",
                    user_email="owner@example.com",
                    agent_id="agent-owner",
                    context_type="web",
                )
            )
            await session.flush()
            session.add(
                Session(
                    session_id="session-stale-work",
                    conversation_id="conversation-stale-work",
                    user_email="owner@example.com",
                    agent_id="agent-owner",
                    intaris_session_id="source-stale-work",
                )
            )
            await session.flush()
            session.add(
                WorkSessionProjectionRow(
                    projection_id="projection-stale-work",
                    owner_email="owner@example.com",
                    session_id="session-stale-work",
                    source_session_id="source-stale-work",
                    materializer_version=WORK_MATERIALIZER_VERSION,
                    state="caught_up",
                    covered_through_seq=10,
                    target_seq=11,
                    priority=0,
                    next_retry_at=datetime.now(UTC) + timedelta(hours=1),
                    next_head_check_at=datetime.now(UTC) + timedelta(hours=1),
                )
            )
            await session.commit()

        claims = await asyncio.gather(*(worker._claim() for worker in workers))
        assert sum(len(worker_claims) for worker_claims in claims) == 1
        async with factory() as session:
            row = await session.get(WorkSessionProjectionRow, "projection-stale-work")
            assert row is not None
            assert row.state == "materializing"
            assert row.covered_through_seq == 10
            assert row.target_seq == 11
            assert row.lease_owner in {"controller-a", "controller-b"}
    finally:
        await engine.dispose()
        async with admin.begin() as connection:
            await connection.execute(sa_schema.DropSchema(schema_name, cascade=True))
        await admin.dispose()
