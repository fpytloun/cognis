"""Exact document reads must not repeatedly authorize the whole content catalog."""

from __future__ import annotations

import hashlib

import pytest
import sqlalchemy as sa

from cognis.artifacts.store import ArtifactStore, ArtifactStoreConfig
from cognis.core import artifact_access
from cognis.core.deliverable_reads import readable_deliverables
from cognis.store.models import Agent, ArtifactGrantRow, DeliverableRow, ProjectRow, User
from cognis.store.queries import create_deliverable
from tests.integration.test_read_paths import read_db as read_db
from tests.unit.test_artifact_virtual_deliverable_refs import _seed_managed_deliverables

OWNER = "owner@example.com"


@pytest.fixture
async def documents(read_db, tmp_path):
    read_db.artifact_store = ArtifactStore(
        ArtifactStoreConfig(
            path=str(tmp_path / "documents"),
            base_url="http://testserver",
            signing_secret="authorization-test-secret",
        )
    )
    async with read_db() as db:
        db.add(User(email=OWNER, name="Owner"))
        await db.flush()
        db.add(Agent(agent_id="agent-owner", owner_email=OWNER, name="Controller"))
        await db.commit()
    await _seed_managed_deliverables(read_db)
    ids = []
    async with read_db() as db:
        for index in range(10):
            row = await create_deliverable(
                db,
                conversation_id="conv-child",
                content=f"Result {index}",
                artifact_store=read_db.artifact_store,
            )
            ids.append(row.deliverable_id)
        await db.commit()
    return read_db, ids


async def _read(factory, ids):
    async with factory() as db:
        return await readable_deliverables(
            db,
            ids,
            OWNER,
            accessor_conversation_id="conv-controller",
        )


async def test_output_grants_batch_without_recursive_work(documents, monkeypatch):
    factory, ids = documents
    async with factory() as db:
        assert await db.scalar(
            sa.select(sa.func.count())
            .select_from(ArtifactGrantRow)
            .where(
                ArtifactGrantRow.owner_email == OWNER,
                ArtifactGrantRow.content_ref.in_(ids),
                ArtifactGrantRow.agent_id == "agent-owner",
                ArtifactGrantRow.source == "output",
            )
        ) == len(ids)

    def forbidden_graph(**kwargs):
        raise AssertionError("An exact output grant must not construct the work graph")

    monkeypatch.setattr(artifact_access, "_work_nodes", forbidden_graph)
    statements = []
    engine = factory.kw["bind"]

    def capture(conn, cursor, statement, parameters, context, many):
        statements.append(statement)

    sa.event.listen(engine.sync_engine, "before_cursor_execute", capture)
    try:
        assert set(await _read(factory, ids[:1])) == set(ids[:1])
        single_count = len(statements)
        statements.clear()
        assert set(await _read(factory, ids)) == set(ids)
        assert len(statements) == single_count
        assert not any("content_managed_lineage" in sql for sql in statements)
        candidates = [
            sql
            for sql in statements
            if "JOIN" in sql and "projects" in sql and "SELECT DISTINCT" in sql
        ]
        assert candidates
        assert all("deliverables.deliverable_id IN" in sql for sql in candidates)
    finally:
        sa.event.remove(engine.sync_engine, "before_cursor_execute", capture)


async def test_legacy_provenance_is_batched_and_matches_canonical_policy(documents, monkeypatch):
    factory, ids = documents
    requested = [*ids, "dlv_branch_child"]
    async with factory() as db:
        # Pre-grant legacy outputs must still be reachable through provenance.
        await db.execute(
            sa.delete(ArtifactGrantRow).where(
                ArtifactGrantRow.content_ref.in_(ids),
                ArtifactGrantRow.agent_id == "agent-owner",
            )
        )
        await db.commit()
        canonical = await artifact_access.content_read_predicate(
            db,
            owner_email=OWNER,
            agent_id="agent-owner",
            conversation_id="conv-controller",
            model=DeliverableRow,
        )
        expected = set(
            await db.scalars(
                sa.select(DeliverableRow.deliverable_id).where(
                    DeliverableRow.deliverable_id.in_(requested),
                    canonical,
                )
            )
        )
    original = artifact_access._work_nodes
    constructions = []

    def capture_graph(**kwargs):
        constructions.append(kwargs)
        return original(**kwargs)

    monkeypatch.setattr(artifact_access, "_work_nodes", capture_graph)
    assert set(await _read(factory, requested)) == expected == set(ids)
    assert len(constructions) == 1


async def test_exact_document_grant_revocation_is_fresh(documents):
    factory, _ids = documents
    ref = "dlv_branch_child"
    async with factory() as db:
        db.add(
            ArtifactGrantRow(
                owner_email=OWNER,
                content_ref=ref,
                agent_id="agent-owner",
                source="explicit",
            )
        )
        await db.commit()
    assert set(await _read(factory, [ref])) == {ref}
    async with factory() as db:
        await db.execute(
            sa.delete(ArtifactGrantRow).where(
                ArtifactGrantRow.owner_email == OWNER,
                ArtifactGrantRow.content_ref == ref,
                ArtifactGrantRow.agent_id == "agent-owner",
            )
        )
        await db.commit()
    assert not await _read(factory, [ref])


async def test_postgres_exact_candidate_plan_ignores_unrelated_catalog(documents, capsys):
    factory, ids = documents
    engine = factory.kw["bind"]
    if engine.dialect.name != "postgresql":
        pytest.skip("PostgreSQL plan and buffer qualification")
    async with factory() as db:
        db.add(ProjectRow(project_id="project-cost", owner_email=OWNER, name="Cost fixture"))
        await db.flush()
        await db.execute(
            sa.insert(DeliverableRow),
            [
                dict(
                    deliverable_id=f"dlv_cost_{index:08d}",
                    owner_email=OWNER,
                    owner_agent_id="agent-unrelated",
                    conversation_id="conv-unrelated",
                    project_id="project-cost",
                    storage_object_id=f"dlv_cost_{index:08d}",
                    content_mime="text/markdown",
                    content_hash=hashlib.sha256(b"").hexdigest(),
                )
                for index in range(10_000)
            ],
        )
        await db.execute(
            sa.insert(ArtifactGrantRow),
            [
                dict(
                    owner_email=OWNER,
                    content_ref=f"dlv_cost_{index:08d}",
                    agent_id="agent-owner",
                    source="output",
                )
                for index in range(10_000)
            ],
        )
        await db.commit()

    captured = []

    def capture(conn, cursor, statement, parameters, context, many):
        if "SELECT DISTINCT" in statement and "JOIN" in statement and "projects" in statement:
            captured.append((statement, parameters))

    sa.event.listen(engine.sync_engine, "before_cursor_execute", capture)
    try:
        async with factory() as db:
            # The public discovery predicate models the original unbounded
            # intermediate candidate query; compare it without changing policy.
            await artifact_access.content_read_predicate(
                db,
                owner_email=OWNER,
                agent_id="agent-owner",
                conversation_id="conv-controller",
                model=DeliverableRow,
            )
        baseline = captured[-1]
        captured.clear()
        assert set(await _read(factory, ids)) == set(ids)
        bounded = captured[-1]
    finally:
        sa.event.remove(engine.sync_engine, "before_cursor_execute", capture)

    plans = []
    async with engine.connect() as conn:
        await conn.exec_driver_sql("SET statement_timeout = '20s'")
        for sql, parameters in (baseline, bounded):
            result = await conn.exec_driver_sql(
                "EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) " + sql,
                parameters,
            )
            plans.append(result.scalar_one()[0])
    old, new = plans
    assert "content_managed_lineage" in baseline[0]
    assert "content_managed_lineage" not in bounded[0]
    assert new["Plan"]["Shared Hit Blocks"] < old["Plan"]["Shared Hit Blocks"]
    with capsys.disabled():
        print(
            {
                "catalog_documents": 10_000,
                "requested_documents": len(ids),
                "baseline_ms": old["Execution Time"],
                "bounded_ms": new["Execution Time"],
                "baseline_shared_hits": old["Plan"]["Shared Hit Blocks"],
                "bounded_shared_hits": new["Plan"]["Shared Hit Blocks"],
            }
        )
