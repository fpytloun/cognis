from __future__ import annotations

import asyncio
import os
import uuid

import pytest
from sqlalchemy import schema as sa_schema
from sqlalchemy.ext.asyncio import create_async_engine

from cognis.knowledgebase.indexer import KnowledgebaseIndexer
from cognis.store.database import create_session_factory
from cognis.store.models import Base, KnowledgebaseIndexJobRow

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
