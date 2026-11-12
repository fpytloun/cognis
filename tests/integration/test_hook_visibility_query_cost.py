"""Legacy hook classification must not scan inline configuration history."""

from __future__ import annotations

import hashlib
import json
from typing import Any

import pytest
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from cognis.store.hook_visibility import is_internal_hook_conversation
from cognis.store.models import AgentHookRunRow, Conversation
from cognis.store.queries import list_conversations
from tests.integration.test_read_paths import read_db as read_db

OWNER = "reads-owner@local.test"


def _walk(plan: dict[str, Any]):
    yield plan
    for child in plan.get("Plans", []):
        yield from _walk(child)


async def _seed(factory, *, inline_count: int) -> set[str]:
    ids = {f"hook-cost-conv-{index:02d}" for index in range(60)}
    padding = hashlib.shake_256(b"synthetic-hook-configuration").hexdigest(4096)
    async with factory() as db:
        await db.execute(
            sa.insert(Conversation),
            [
                {
                    "conversation_id": ref,
                    "user_email": OWNER,
                    "agent_id": "reads-owned",
                    "context_type": "web",
                }
                for ref in sorted(ids)
            ],
        )
        runs = [
            {
                "run_id": f"hook-cost-inline-{index}",
                "hook_id": "hook-cost",
                "hook_revision": 1,
                "owner_email": OWNER,
                "agent_id": "reads-owned",
                "conversation_id": "hook-cost-conv-59",
                "source_session_id": "hook-cost-source",
                "occurrence": str(index),
                "status": "completed",
                "lifecycle_pending": False,
                "child_conversation_id": None,
                "configuration": {"action": "prompt", "mode": "inline", "prompt": padding},
            }
            for index in range(inline_count)
        ]
        for index in range(5):
            runs.append(
                {
                    "run_id": f"hook-cost-child-{index}",
                    "hook_id": "hook-cost",
                    "hook_revision": 1,
                    "owner_email": OWNER if index != 3 else "reads-other@local.test",
                    "agent_id": "reads-owned" if index != 4 else "reads-private",
                    "conversation_id": "hook-cost-conv-59",
                    "source_session_id": "hook-cost-source",
                    "occurrence": f"child-{index}",
                    "status": "completed",
                    "lifecycle_pending": False,
                    "child_conversation_id": f"hook-cost-conv-{index:02d}",
                    "configuration": {
                        "action": "prompt",
                        "mode": "inline" if index == 2 else "fork" if index == 0 else "fresh",
                    },
                }
            )
        await db.execute(sa.insert(AgentHookRunRow), runs)
        await db.commit()
    return ids


async def test_hook_visibility_preserves_mode_and_owner_agent_fences(read_db):
    ids = await _seed(read_db, inline_count=3)
    async with read_db() as db:
        for index in range(5):
            assert await is_internal_hook_conversation(db, f"hook-cost-conv-{index:02d}") is (
                index < 2
            )
        assert not await is_internal_hook_conversation(db, "hook-cost-conv-59")
        visible = await list_conversations(db, user_email=OWNER, limit=100)
    assert {row.conversation_id for row in visible} == ids - {
        "hook-cost-conv-00",
        "hook-cost-conv-01",
    }


async def test_listing_does_not_scan_inline_hook_configurations(read_db, record_property):
    factory = read_db
    engine = factory.kw["bind"]
    if engine.dialect.name != "postgresql":
        pytest.skip("requires PostgreSQL execution-plan counters")
    ids = await _seed(factory, inline_count=1530)
    schema = engine.get_execution_options()["schema_translate_map"][None]
    quoted = engine.dialect.identifier_preparer.quote_schema(schema)
    async with engine.begin() as conn:
        await conn.execute(sa.text(f"ANALYZE {quoted}.agent_hook_runs"))
        await conn.execute(sa.text(f"ANALYZE {quoted}.conversations"))
    statements = []

    def capture(conn, cursor, statement, parameters, context, many):
        if statement.startswith("SELECT ") and "agent_hook_runs" in statement:
            statements.append((statement, parameters))

    sa.event.listen(engine.sync_engine, "before_cursor_execute", capture)
    try:
        async with factory() as db:
            rows = await list_conversations(db, user_email=OWNER, limit=100)
    finally:
        sa.event.remove(engine.sync_engine, "before_cursor_execute", capture)
    assert {row.conversation_id for row in rows} == ids - {
        "hook-cost-conv-00",
        "hook-cost-conv-01",
    }
    assert len(statements) == 1
    statement, parameters = statements[0]
    async with engine.connect() as conn:
        await conn.execute(sa.text("SET plan_cache_mode=force_generic_plan"))
        await conn.execute(sa.text(f"PREPARE hook_visibility_probe AS {statement}"))
        args = ",".join(
            str(
                sa.literal(value).compile(
                    dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}
                )
            )
            for value in parameters
        )
        value = await conn.scalar(
            sa.text(
                "EXPLAIN (ANALYZE,BUFFERS,TIMING OFF,FORMAT JSON) "
                f"EXECUTE hook_visibility_probe({args})"
            )
        )
        plan = json.loads(value)[0] if isinstance(value, str) else value[0]
        await conn.execute(sa.text("DEALLOCATE hook_visibility_probe"))
    scans = [node for node in _walk(plan["Plan"]) if node.get("Relation Name") == "agent_hook_runs"]
    assert scans
    examined = sum(
        (node["Actual Rows"] + node.get("Rows Removed by Filter", 0)) * node["Actual Loops"]
        for node in scans
    )
    record_property(
        "hook_visibility_cost",
        {
            "execution_ms": plan["Execution Time"],
            "hook_rows_examined": examined,
            "shared_hit_blocks": plan["Plan"].get("Shared Hit Blocks", 0),
            "scan_types": [node["Node Type"] for node in scans],
        },
    )
    assert examined <= 5
