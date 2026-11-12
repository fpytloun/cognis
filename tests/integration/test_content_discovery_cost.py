"""Discovery must not traverse ancestry for rows admitted by direct grants."""

from __future__ import annotations

import hashlib
import json
from itertools import product
from typing import Any

import pytest
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from cognis.core.artifact_access import _prefer_direct_access, content_read_predicate
from cognis.store.models import (
    ArtifactGrantRow,
    ArtifactRecordRow,
    Conversation,
    DeliverableRow,
    ProjectRow,
    Session,
)
from tests.integration.test_read_paths import read_db as read_db

OWNER = "reads-owner@local.test"


async def test_access_fence_preserves_sql_three_valued_logic(read_db):
    async with read_db() as db:
        for left, right in product((True, False, None), repeat=2):
            direct = sa.literal(left, type_=sa.Boolean())
            fallback = sa.literal(right, type_=sa.Boolean())
            original, fenced = (
                await db.execute(
                    sa.select(sa.or_(fallback, direct), _prefer_direct_access(direct, fallback))
                )
            ).one()
            assert original is fenced


def _walk(plan: dict[str, Any]):
    yield plan
    for child in plan.get("Plans", []):
        yield from _walk(child)


async def _seed(factory, kind: str, basis: str, projected: bool) -> list[str]:
    model = ArtifactRecordRow if kind == "artifact" else DeliverableRow
    ids = [f"{'art' if kind == 'artifact' else 'dlv'}_cost_{index:04d}" for index in range(100)]
    async with factory() as db:
        await db.execute(
            sa.insert(Conversation),
            [
                {
                    "conversation_id": f"cost-conversation-{index}",
                    "user_email": OWNER,
                    "agent_id": "reads-owned",
                    "context_type": "agent_direct",
                }
                for index in range(3000)
            ],
        )
        await db.execute(
            sa.insert(Session),
            [
                {
                    "session_id": f"cost-session-{index}",
                    "conversation_id": f"cost-conversation-{index}",
                    "user_email": OWNER,
                    "agent_id": "reads-owned",
                }
                for index in range(3000)
            ],
        )
        if projected:
            db.add(ProjectRow(project_id="cost-project", owner_email=OWNER, name="Cost"))
            await db.flush()
        values = [
            {
                "owner_email": OWNER,
                "owner_agent_id": "reads-owned" if basis == "owner" else "reads-private",
                "session_id": "cost-session-0",
                "conversation_id": "cost-conversation-0",
                "project_id": "cost-project" if projected else None,
            }
            for _ in ids
        ]
        for ref, value in zip(ids, values, strict=True):
            if kind == "artifact":
                value.update(
                    artifact_id=ref,
                    namespace="content",
                    object_id=ref,
                    filename="cost.txt",
                    mime_type="text/plain",
                    status="stored",
                )
            else:
                value.update(
                    deliverable_id=ref,
                    storage_object_id=ref,
                    content_mime="text/markdown",
                    content_hash=hashlib.sha256(b"").hexdigest(),
                    title="Cost result",
                    format="markdown",
                )
        await db.execute(sa.insert(model), values)
        if basis == "output":
            await db.execute(
                sa.insert(ArtifactGrantRow),
                [
                    {
                        "grant_id": f"cost-grant-{index}",
                        "owner_email": OWNER,
                        "content_ref": ref,
                        "agent_id": "reads-owned",
                        "source": "output",
                    }
                    for index, ref in enumerate(ids)
                ],
            )
        await db.commit()
    return ids


@pytest.mark.parametrize("kind", ["artifact", "deliverable"])
@pytest.mark.parametrize("basis", ["owner", "output"])
@pytest.mark.parametrize("projected", [False, True])
@pytest.mark.parametrize("read_db", ["postgresql"], indirect=True)
async def test_direct_discovery_does_not_execute_provenance(
    read_db, kind, basis, projected, record_property
):
    factory = read_db
    engine = factory.kw["bind"]
    if engine.dialect.name != "postgresql":
        pytest.skip("requires PostgreSQL execution-plan counters")
    expected = await _seed(factory, kind, basis, projected)
    model = ArtifactRecordRow if kind == "artifact" else DeliverableRow
    column = model.artifact_id if kind == "artifact" else model.deliverable_id
    statements = []

    def capture(conn, cursor, statement, parameters, context, many):
        if statement.startswith("WITH RECURSIVE content_managed_lineage"):
            statements.append((statement, parameters))

    sa.event.listen(engine.sync_engine, "before_cursor_execute", capture)
    try:
        async with factory() as db:
            predicate = await content_read_predicate(
                db, owner_email=OWNER, agent_id="reads-owned", model=model
            )
            result = (
                await db.scalars(sa.select(column).where(predicate).order_by(column).limit(25))
            ).all()
    finally:
        sa.event.remove(engine.sync_engine, "before_cursor_execute", capture)
    assert list(result) == expected[:25]
    assert statements
    plans = []
    async with engine.connect() as connection:
        await connection.execute(sa.text("SET plan_cache_mode=force_generic_plan"))
        for statement, parameters in statements:
            await connection.execute(sa.text(f"PREPARE content_read_probe AS {statement}"))
            args = ",".join(
                str(
                    sa.literal(value).compile(
                        dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}
                    )
                )
                for value in parameters
            )
            value = await connection.scalar(
                sa.text(
                    "EXPLAIN (ANALYZE,BUFFERS,TIMING OFF,FORMAT JSON) "
                    f"EXECUTE content_read_probe({args})"
                )
            )
            plans.append(json.loads(value)[0] if isinstance(value, str) else value[0])
            await connection.execute(sa.text("DEALLOCATE content_read_probe"))
    ancestry = [
        node
        for plan in plans
        for node in _walk(plan["Plan"])
        if node.get("Subplan Name")
        in {"CTE content_work_scope", "CTE content_work_edges", "CTE content_managed_lineage"}
    ]
    assert ancestry
    record_property(
        "discovery_cost",
        {
            "kind": kind,
            "basis": basis,
            "projected": projected,
            "execution_ms": [plan["Execution Time"] for plan in plans],
            "recursive_loops": [node["Actual Loops"] for node in ancestry],
            "temp_blocks_written": [plan["Plan"].get("Temp Written Blocks", 0) for plan in plans],
        },
    )
    assert all(node["Actual Loops"] == 0 for node in ancestry)
