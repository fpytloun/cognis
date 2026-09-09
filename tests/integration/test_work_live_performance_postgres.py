from __future__ import annotations

import os
import statistics
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, Mock

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncEngine

from cognis.api.app import create_app
from cognis.api.chat_v2.event_store import (
    RawSessionEvent,
    SessionEventPage,
    SessionHistoryAvailability,
)
from cognis.api.chat_v2.work_materializer import WorkMaterializer, _PendingWorkAppend
from cognis.models.session import SessionEvent
from cognis.providers.guardrails.events import EventStoreAuthority
from cognis.store import queries
from cognis.store.models import (
    Agent,
    ManagedConversationLink,
    User,
    WorkSessionProjectionRow,
)

POSTGRES_URL = os.environ.get("COGNIS_TEST_POSTGRES_URL", "")
pytestmark = pytest.mark.skipif(not POSTGRES_URL, reason="PostgreSQL URL required")

OWNER = "work-perf@example.com"
AGENT = "work-perf-agent"
MAX_ROOT = "work-perf-max"
NORMAL_ROOT = "work-perf-normal"
ONE_ROOT = "work-perf-one"
MAX_PHYSICAL_SESSIONS = 224


def _p95(values: list[float]) -> float:
    ordered = sorted(values)
    return ordered[max(0, int(len(ordered) * 0.95) - 1)]


def _headers(client: TestClient) -> dict[str, str]:
    token = client.app.state.auth_provider.sign_access_token(OWNER, "Work Perf", "user")
    return {"Authorization": f"Bearer {token}"}


async def _seed_topology(client: TestClient) -> None:
    async with client.app.state.session_factory() as db:
        db.add(User(email=OWNER, name="Work Perf", role="user"))
        await db.flush()
        db.add(Agent(agent_id=AGENT, owner_email=OWNER, name="Work Perf"))
        await db.flush()
        for root, physical in (
            (ONE_ROOT, 1),
            (NORMAL_ROOT, 10),
            (MAX_ROOT, MAX_PHYSICAL_SESSIONS),
        ):
            await queries.create_conversation(
                db,
                conversation_id=root,
                user_email=OWNER,
                agent_id=AGENT,
                context_type="web",
            )
            root_session = f"{root}-session-000"
            previous: dict[int, str] = {}
            child_conversations: dict[int, str] = {}
            for index in range(physical):
                scope = (0 if index == 0 else ((index - 1) % 11) + 1) if root == MAX_ROOT else index
                conversation_id = root
                if index:
                    conversation_id = child_conversations.get(scope, f"{root}-child-{scope:03d}")
                    if scope not in child_conversations:
                        await queries.create_conversation(
                            db,
                            conversation_id=conversation_id,
                            user_email=OWNER,
                            agent_id=AGENT,
                            context_type="agent_work",
                        )
                        child_conversations[scope] = conversation_id
                session_id = f"{root}-session-{index:03d}"
                prior = previous.get(scope)
                row = await queries.create_session(
                    db,
                    session_id=session_id,
                    conversation_id=conversation_id,
                    user_email=OWNER,
                    agent_id=AGENT,
                    intaris_session_id=f"source-{session_id}",
                    parent_session_id=(root_session if index and prior is None else None),
                    previous_session_id=prior,
                    source_session_id=(root_session if index else None),
                    delegation_mode=("agent" if index else None),
                    delegation_task=(f"Performance child {index}" if index else None),
                    activity_scope_id=f"{root}-scope-{scope:03d}",
                )
                previous[scope] = row.session_id
                if index:
                    await queries.update_conversation_active_session(
                        db, conversation_id, row.session_id
                    )
            await queries.update_conversation_active_session(db, root, root_session)
        await db.commit()


async def _seed_rows(client: TestClient) -> None:
    async with client.app.state.engine.begin() as connection:
        await connection.execute(
            sa.text(
                """
                UPDATE work_session_projections
                SET state = CASE
                        WHEN right(session_id, 3)::integer % 29 = 0 THEN 'failed'
                        WHEN right(session_id, 3)::integer % 17 = 0 THEN 'repair'
                        ELSE 'caught_up'
                    END,
                    target_seq = 400,
                    covered_through_seq = CASE
                        WHEN right(session_id, 3)::integer % 17 = 0 THEN 300
                        ELSE 400
                    END,
                    record_count = 400, evidence_record_count = 400,
                    command_count = 250, mutation_count = 100,
                    artifact_count = 25, deliverable_count = 25
                WHERE session_id LIKE 'work-perf-max-session-%'
                """
            )
        )
        await connection.execute(
            sa.text(
                """
                INSERT INTO work_records (
                    work_record_id, owner_email, session_id, materializer_version,
                    source_store, source_session_id, source_seq, source_item_id,
                    item_ordinal, occurred_at, record_type, category, entity_id,
                    file_path_ids, additions, deletions, is_evidence, pairing_key,
                    call_id, timeline_item, materialized_at
                )
                SELECT 'perf-record-' || g, :owner,
                    'work-perf-max-session-' ||
                        lpad(((g - 1) % :physical)::text, 3, '0'),
                    'work-v8', 'intaris',
                    'source-work-perf-max-session-' ||
                        lpad(((g - 1) % :physical)::text, 3, '0'),
                    ((g - 1) / :physical) + 1,
                    CASE
                        WHEN g % 20 NOT IN (0, 1) AND g % 5 != 0
                            THEN 'tool:command-' || (g % 500)
                        ELSE 'tool:perf-' || g
                    END,
                    0,
                    now() - ((100000 - g) * interval '1 millisecond'),
                    'tool_pair',
                    CASE WHEN g % 20 = 0 THEN 'artifacts'
                         WHEN g % 20 = 1 THEN 'deliverables'
                         WHEN g % 5 = 0 THEN 'mutations' ELSE 'commands' END,
                    CASE
                        WHEN g % 20 = 0 THEN 'perf-artifact-' || g
                        WHEN g % 20 = 1 THEN 'perf-deliverable-' || (g % 200)
                        WHEN g % 5 = 0 THEN 'perf-mutation-' || (g % 500)
                        ELSE NULL
                    END,
                    '[]'::jsonb,
                    CASE WHEN g % 5 = 0 THEN 2 ELSE 0 END,
                    CASE WHEN g % 5 = 0 THEN 1 ELSE 0 END,
                    true, 'perf-' || g,
                    CASE WHEN g % 20 = 0 THEN NULL ELSE 'perf-' || g END,
                    CASE
                        WHEN g % 20 = 0 THEN jsonb_build_object(
                            'kind', 'artifact',
                            'id', 'artifact-item:' || g,
                            'sort_key', lpad(g::text, 12, '0'),
                            'artifact_id', 'perf-artifact-' || g,
                            'filename', 'artifact-' || g || '.txt'
                        )
                        ELSE jsonb_build_object(
                            'kind', 'tool_call', 'id',
                            CASE
                                WHEN g % 20 NOT IN (0, 1) AND g % 5 != 0
                                    THEN 'tool:command-' || (g % 500)
                                ELSE 'tool:perf-' || g
                            END,
                            'sort_key', lpad(g::text, 12, '0'),
                            'call_id', 'perf-' || g, 'tool_name', 'bash',
                            'arguments', jsonb_build_object('command', 'true'),
                            'status', 'complete'
                        )
                    END,
                    now() - ((100000 - g) * interval '1 millisecond')
                FROM generate_series(1, 100000) AS g
                """
            ),
            {"owner": OWNER, "physical": MAX_PHYSICAL_SESSIONS},
        )
        await connection.execute(
            sa.text(
                """
                INSERT INTO artifacts (
                    artifact_id, namespace, object_id, filename, owner_email,
                    purpose, kind, mime_type, size_bytes, status,
                    created_at, updated_at, deleted_at
                )
                SELECT
                    'perf-artifact-' || g,
                    'work-perf',
                    'object-' || g,
                    'artifact-' || g || '.txt',
                    CASE WHEN g >= 99960 THEN 'foreign@example.com' ELSE :owner END,
                    'tool_output',
                    'file',
                    'text/plain',
                    128,
                    'ready',
                    now(),
                    now(),
                    CASE
                        WHEN g IN (99940, 99920) THEN now()
                        ELSE NULL
                    END
                FROM unnest(
                    ARRAY[
                        100000, 99980, 99960, 99940, 99920,
                        99900, 99880, 99860, 99840, 99820,
                        99800, 99780, 99760, 99740, 99720
                    ]
                ) AS g
                """
            ),
            {"owner": OWNER},
        )
        await connection.execute(
            sa.text(
                """
                INSERT INTO work_current_files (
                    current_file_id, owner_email, session_id, materializer_version,
                    file_projector_version, path_generation_id, source_session_id,
                    source_seq, source_item_id, root_id, path, path_id, state,
                    additions, deletions, preview_omitted, created_at, updated_at
                )
                SELECT 'perf-current-' || g, :owner,
                    'work-perf-max-session-' ||
                        lpad(((g - 1) % :physical)::text, 3, '0'),
                    'work-v8', 'work-files-v3', 'perf-generation-' || g,
                    'source-work-perf-max-session-' ||
                        lpad(((g - 1) % :physical)::text, 3, '0'),
                    400, 'tool:perf-' || g, 'repo',
                    'src/file-' || g || '.py', 'repo:src/file-' || g || '.py',
                    'modified', 2, 1, true, now(), now()
                FROM generate_series(1, 2500) AS g
                """
            ),
            {"owner": OWNER, "physical": MAX_PHYSICAL_SESSIONS},
        )
        await connection.execute(
            sa.text(
                """
                INSERT INTO work_record_files (
                    work_record_file_id, owner_email, session_id,
                    materializer_version, work_record_id, file_ordinal, path,
                    path_id, path_generation_id, source_seq, item_ordinal,
                    additions, deletions, status
                )
                SELECT 'perf-file-' || g, :owner,
                    'work-perf-max-session-' ||
                        lpad(((g - 1) % :physical)::text, 3, '0'),
                    'work-v8', 'perf-record-' || g, 0,
                    'src/file-' || g || '.py',
                    'repo:src/file-' || g || '.py',
                    'perf-generation-' || g,
                    ((g - 1) / :physical) + 1,
                    0, 2, 1, 'modified'
                FROM generate_series(1, 80000) AS g
                """
            ),
            {"owner": OWNER, "physical": MAX_PHYSICAL_SESSIONS},
        )
        await connection.execute(
            sa.text(
                """
                INSERT INTO work_record_files (
                    work_record_file_id, owner_email, session_id,
                    materializer_version, work_record_id, file_ordinal, path,
                    path_id, path_generation_id, source_seq, item_ordinal,
                    additions, deletions, status
                ) VALUES (
                    'perf-history-file', :owner, 'work-perf-max-session-095',
                    'work-v8', 'perf-record-100000', 0, 'src/history.py',
                    'repo:src/history.py', 'perf-history-generation',
                    400, 0, 2, 1, 'modified'
                )
                """
            ),
            {"owner": OWNER},
        )
        await connection.execute(
            sa.text(
                """
                INSERT INTO direct_turn_requests (
                    request_id, turn_id, conversation_id, session_id, agent_id,
                    user_id, idempotency_scope, idempotency_key, admission_hash,
                    payload_hash, payload_version, payload, status, attempt_count,
                    created_at, updated_at
                )
                SELECT 'perf-request-' || g, 'perf-turn-' || g, :conversation,
                    'work-perf-max-session-' ||
                        lpad(((g - 1) % :physical)::text, 3, '0'),
                    :agent, :owner, 'perf', 'perf-key-' || g,
                    'admission-' || g, 'payload-' || g, 1, '{}'::jsonb,
                    'completed', 1,
                    now() - ((5000 - g) * interval '1 second'),
                    now() - ((5000 - g) * interval '1 second')
                FROM generate_series(1, 5000) AS g
                """
            ),
            {
                "conversation": MAX_ROOT,
                "agent": AGENT,
                "owner": OWNER,
                "physical": MAX_PHYSICAL_SESSIONS,
            },
        )
        await connection.execute(
            sa.text("ANALYZE work_records, work_record_files, artifacts, direct_turn_requests")
        )
    async with client.app.state.session_factory() as db:
        db.add(
            ManagedConversationLink(
                link_id="perf-link",
                user_email=OWNER,
                controller_agent_id=AGENT,
                controller_conversation_id=MAX_ROOT,
                controller_session_id="work-perf-max-session-000",
                target_agent_id=AGENT,
                target_conversation_id=f"{MAX_ROOT}-child-010",
                target_session_id="work-perf-max-session-020",
                title="Managed evidence",
                turn_state="completed",
            )
        )
        for index in range(20):
            task = await queries.create_task(
                db,
                created_by=OWNER,
                agent_id=AGENT,
                title=f"Perf task {index}",
                status="completed",
                source_session_id=f"work-perf-max-session-{index:03d}",
            )
            step = await queries.create_step_run(
                db,
                task_id=task.task_id,
                step_name="run",
                step_type="run",
                agent_id=AGENT,
                conversation_id=MAX_ROOT,
                status="completed",
            )
            step.session_id = f"work-perf-max-session-{index:03d}"
        await db.commit()


def _samples(
    client: TestClient, path: str, repeats: int = 7
) -> tuple[
    list[float],
    list[int],
    tuple[str, Any, float],
    dict[str, tuple[str, Any]],
]:
    statements: list[str] = []
    started_queries: dict[int, float] = {}
    slowest: tuple[str, Any, float] = ("", (), 0.0)
    recent_queries: dict[str, tuple[str, Any]] = {}

    def capture(
        _connection: object,
        _cursor: object,
        statement: str,
        parameters: Any,
        _context: object,
        _executemany: object,
    ) -> None:
        statements.append(statement)
        started_queries[id(_context)] = time.perf_counter()
        if (
            "work_records.category >=" in statement
            and "work_records.occurred_at DESC" in statement
            and "row_number" not in statement.lower()
        ):
            for category in ("commands", "artifacts"):
                if category not in recent_queries and f"'{category}'" in statement:
                    recent_queries[category] = (statement, parameters)
        if "work_summary_records" in statement and "summary" not in recent_queries:
            recent_queries["summary"] = (statement, parameters)
        if (
            "work_records.call_id IS NOT NULL" in statement
            and "ORDER BY work_records.materialized_at DESC" in statement
            and "call_state" not in recent_queries
        ):
            recent_queries["call_state"] = (statement, parameters)

    def captured(
        _connection: object,
        _cursor: object,
        statement: str,
        parameters: Any,
        context: object,
        _executemany: object,
    ) -> None:
        nonlocal slowest
        started = started_queries.pop(id(context), None)
        if started is None:
            return
        elapsed = (time.perf_counter() - started) * 1000
        if elapsed > slowest[2] and statement.lstrip().upper().startswith("SELECT"):
            slowest = (statement, parameters, elapsed)

    timings: list[float] = []
    counts: list[int] = []
    sa.event.listen(client.app.state.engine.sync_engine, "before_cursor_execute", capture)
    sa.event.listen(client.app.state.engine.sync_engine, "after_cursor_execute", captured)
    try:
        for _ in range(2):
            response = client.get(path, headers=_headers(client))
            assert response.status_code == 200, response.text
        for _ in range(repeats):
            statements.clear()
            started = time.perf_counter()
            response = client.get(path, headers=_headers(client))
            timings.append((time.perf_counter() - started) * 1000)
            counts.append(len(statements))
            assert response.status_code == 200, response.text
    finally:
        sa.event.remove(client.app.state.engine.sync_engine, "before_cursor_execute", capture)
        sa.event.remove(client.app.state.engine.sync_engine, "after_cursor_execute", captured)
    return timings, counts, slowest, recent_queries


async def _direct_plan(engine: AsyncEngine) -> str:
    session_ids = [f"work-perf-max-session-{index:03d}" for index in range(MAX_PHYSICAL_SESSIONS)]
    async with engine.connect() as connection:
        await connection.execute(sa.text("SET LOCAL enable_seqscan = off"))
        rows = await connection.execute(
            sa.text(
                """
                EXPLAIN (ANALYZE, BUFFERS, COSTS OFF)
                SELECT session_id, status FROM (
                    SELECT session_id, status,
                        row_number() OVER (
                            PARTITION BY session_id ORDER BY admission_order DESC
                        ) AS ordinal
                    FROM direct_turn_requests
                    WHERE user_id = :owner
                        AND session_id = ANY(CAST(:sessions AS text[]))
                ) ranked WHERE ordinal = 1
                """
            ),
            {"owner": OWNER, "sessions": session_ids},
        )
        return "\n".join(str(row[0]) for row in rows)


async def _statement_plan(engine: AsyncEngine, statement: str, parameters: Any) -> str:
    async with engine.connect() as connection:
        rows = await connection.exec_driver_sql(
            f"EXPLAIN (ANALYZE, BUFFERS, COSTS OFF) {statement}",
            parameters,
        )
        return "\n".join(str(row[0]) for row in rows)


async def _query_plan(engine: AsyncEngine, statement: str, parameters: Any) -> str:
    async with engine.connect() as connection:
        rows = await connection.exec_driver_sql(
            f"EXPLAIN (COSTS OFF) {statement}",
            parameters,
        )
        return "\n".join(str(row[0]) for row in rows)


async def _append_fresh_command(
    materializer: WorkMaterializer,
    sample: int,
) -> None:
    first_seq = (sample * 2) + 1
    call_id = f"fresh-command-{sample}"
    await materializer._process_append(
        _PendingWorkAppend(
            authority=EventStoreAuthority(
                user_email=OWNER,
                agent_id=AGENT,
                agent_owner_email=OWNER,
            ),
            session_id=f"source-{ONE_ROOT}-session-000",
            first_seq=first_seq,
            last_seq=first_seq + 1,
            target_seq=first_seq + 1,
            events=(
                SessionEvent(
                    type="tool_call",
                    data={
                        "call_id": call_id,
                        "name": "bash",
                        "arguments": {"command": f"printf {sample}"},
                    },
                ),
                SessionEvent(
                    type="tool_result",
                    data={
                        "call_id": call_id,
                        "name": "bash",
                        "status": "complete",
                        "result": str(sample),
                    },
                ),
            ),
            payload_bytes=128,
        )
    )


async def _fresh_projection_state(client: TestClient) -> tuple[str, int, int, str | None]:
    async with client.app.state.session_factory() as db:
        state = await db.scalar(
            sa.select(WorkSessionProjectionRow).where(
                WorkSessionProjectionRow.session_id == f"{ONE_ROOT}-session-000"
            )
        )
        assert state is not None
        return (
            state.state,
            state.covered_through_seq,
            state.target_seq,
            state.last_error,
        )


def test_public_work_postgres_production_shape(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("COGNIS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv(
        "DATABASE_URL", POSTGRES_URL.replace("postgresql://", "postgresql+asyncpg://", 1)
    )
    monkeypatch.setenv("COGNIS_RUNTIME_MODE", "simple")
    monkeypatch.setenv("COGNIS_INTARIS_URL", "http://127.0.0.1:1")
    monkeypatch.setenv("COGNIS_MNEMORY_URL", "http://127.0.0.1:1")
    with TestClient(create_app()) as client:
        assert client.portal is not None
        client.portal.call(client.app.state.work_materializer.stop)
        client.portal.call(client.app.state.task_queue.stop)
        client.portal.call(client.app.state.scheduler.stop)
        guardrails_read = AsyncMock(
            side_effect=AssertionError("foreground Overview/Work called Intaris")
        )
        client.app.state.intaris_event_store._guardrails.read_events = guardrails_read  # noqa: SLF001
        client.portal.call(_seed_topology, client)
        client.portal.call(_seed_rows, client)

        paths = {
            "one_overview": f"/api/v1/chat/v2/conversations/{ONE_ROOT}/activity-overview",
            "normal_overview": f"/api/v1/chat/v2/conversations/{NORMAL_ROOT}/activity-overview",
            "max_overview": f"/api/v1/chat/v2/conversations/{MAX_ROOT}/activity-overview",
            "max_work": f"/api/v1/chat/v2/conversations/{MAX_ROOT}/work",
            "one_commands": (f"/api/v1/chat/v2/conversations/{ONE_ROOT}/work?category=commands"),
            "max_commands": (
                f"/api/v1/chat/v2/conversations/{MAX_ROOT}/work"
                "?category=commands&detail=lightweight"
            ),
            "max_commands_exact": (
                f"/api/v1/chat/v2/conversations/{MAX_ROOT}/work?category=commands&detail=full"
            ),
            "max_files": f"/api/v1/chat/v2/conversations/{MAX_ROOT}/work?category=files",
        }
        metrics: dict[str, dict[str, float | int]] = {}
        counts: dict[str, int] = {}
        slowest: dict[str, tuple[str, Any, float]] = {}
        sampled_queries: dict[str, tuple[str, Any]] = {}
        for name, path in paths.items():
            timings, query_counts, slow_query, sampled_recent = _samples(client, path)
            metrics[name] = {
                "median_ms": round(statistics.median(timings), 2),
                "p95_ms": round(_p95(timings), 2),
                "queries": max(query_counts),
            }
            counts[name] = max(query_counts)
            slowest[name] = slow_query
            if name in {"max_overview", "max_work", "max_commands", "max_commands_exact"}:
                sampled_queries.update(sampled_recent)

        overview_payload = client.get(
            paths["max_overview"],
            headers=_headers(client),
        ).json()
        recent_commands = overview_payload["recent"]["commands"]
        assert len(recent_commands) == 10
        assert len({item["id"] for item in recent_commands}) == 10
        expected_command_ids = [
            f"tool:command-{value % 500}"
            for value in range(99_999, 0, -1)
            if value % 20 not in (0, 1) and value % 5 != 0
        ][:10]
        assert [item["id"] for item in recent_commands] == expected_command_ids
        assert recent_commands[0]["session_id"] == "work-perf-max-session-094"
        assert [item["title"] for item in overview_payload["recent"]["artifacts"]] == [
            f"perf-artifact-{value}"
            for value in (99900, 99880, 99860, 99840, 99820, 99800, 99780, 99760, 99740, 99720)
        ]

        guardrails_read.assert_not_awaited()
        assert counts["normal_overview"] <= counts["one_overview"] + 25
        assert counts["max_overview"] <= 125
        assert metrics["max_work"]["p95_ms"] <= 1_000
        assert metrics["max_commands"]["p95_ms"] <= 500
        assert counts["max_commands"] <= 80
        exact_commands = client.get(
            paths["max_commands_exact"],
            headers=_headers(client),
        )
        assert exact_commands.status_code == 200
        exact_payload = exact_commands.json()
        assert exact_payload["detail"] == "full"
        assert exact_payload["summary"]["commands"] == 75_000

        headers = _headers(client)
        counter_queries: dict[str, tuple[str, Any]] = {}

        def capture_counter_query(
            _connection: object,
            _cursor: object,
            statement: str,
            parameters: Any,
            _context: object,
            _executemany: object,
        ) -> None:
            normalized = statement.lower()
            if (
                normalized.lstrip().startswith("select")
                and "from work_records" in normalized
                and "filter (where" in normalized
            ):
                counter_queries["records"] = (statement, parameters)
            if (
                normalized.lstrip().startswith("select")
                and "from work_record_files" in normalized
                and "count(*)" in normalized
                and "work_record_files.owner_email" in normalized
                and "work_record_files.session_id" in normalized
                and "work_record_files.materializer_version" in normalized
                and "join" not in normalized
            ):
                counter_queries["files"] = (statement, parameters)

        append_times: list[float] = []
        fresh_read_times: list[float] = []
        fresh_end_to_end_times: list[float] = []
        fresh_path = f"/api/v1/chat/v2/conversations/{ONE_ROOT}/work?category=commands"
        sa.event.listen(
            client.app.state.engine.sync_engine,
            "before_cursor_execute",
            capture_counter_query,
        )
        try:
            for sample in range(12):
                started = time.perf_counter()
                client.portal.call(
                    _append_fresh_command,
                    client.app.state.work_materializer,
                    sample,
                )
                append_times.append((time.perf_counter() - started) * 1000)
                read_started = time.perf_counter()
                fresh_response = client.get(fresh_path, headers=headers)
                fresh_read_times.append((time.perf_counter() - read_started) * 1000)
                fresh_end_to_end_times.append((time.perf_counter() - started) * 1000)
                assert fresh_response.status_code == 200, fresh_response.text
                assert fresh_response.json()["commands"][0]["call_id"] == (
                    f"fresh-command-{sample}"
                )
        finally:
            sa.event.remove(
                client.app.state.engine.sync_engine,
                "before_cursor_execute",
                capture_counter_query,
            )

        fresh_state = client.portal.call(_fresh_projection_state, client)
        assert fresh_state == ("caught_up", 24, 24, None)
        warm_append = append_times[2:]
        warm_read = fresh_read_times[2:]
        warm_end_to_end = fresh_end_to_end_times[2:]
        assert _p95(warm_append) <= 1_000
        assert _p95(warm_end_to_end) <= 1_000
        assert set(counter_queries) == {"records", "files"}
        counter_plans = {
            name: client.portal.call(
                _query_plan,
                client.app.state.engine,
                query[0],
                query[1],
            )
            for name, query in counter_queries.items()
        }
        assert "Index" in counter_plans["records"]
        assert "Seq Scan on work_records" not in counter_plans["records"]
        assert "Index" in counter_plans["files"]
        assert "Seq Scan on work_record_files" not in counter_plans["files"]

        def concurrent_read() -> tuple[int, float]:
            request_started = time.perf_counter()
            response = client.get(paths["max_overview"], headers=headers)
            return response.status_code, (time.perf_counter() - request_started) * 1000

        started = time.perf_counter()
        with ThreadPoolExecutor(max_workers=10) as pool:
            concurrent_results = [
                future.result(timeout=10)
                for future in [pool.submit(concurrent_read) for _ in range(10)]
            ]
        concurrency_ms = (time.perf_counter() - started) * 1000
        assert all(status == 200 for status, _elapsed in concurrent_results)
        assert max(elapsed for _status, elapsed in concurrent_results) < 10_000
        assert concurrency_ms < 10_000

        history_page = SessionEventPage(
            store_id="intaris",
            session_id="source-work-perf-max-session-095",
            events=[
                RawSessionEvent(
                    store_id="intaris",
                    session_id="source-work-perf-max-session-095",
                    seq=400,
                    type="tool_result",
                    data={
                        "call_id": "perf-100000",
                        "name": "write",
                        "status": "complete",
                        "file_diffs": [
                            {
                                "path": "src/history.py",
                                "diff": "+line",
                                "additions": 2,
                                "deletions": 1,
                            }
                        ],
                    },
                )
            ],
            first_seq=400,
            last_seq=400,
            availability=SessionHistoryAvailability(durable_last_seq=400, first_available_seq=1),
        )
        client.app.state.cached_event_store.bind = Mock(
            return_value=SimpleNamespace(read_session_events=AsyncMock(return_value=history_page))
        )
        history_times: list[float] = []
        for _ in range(7):
            started = time.perf_counter()
            response = client.post(
                "/api/v1/work/file-history",
                headers=headers,
                json={
                    "scope": {
                        "key": f"conversation:{MAX_ROOT}",
                        "kind": "conversation",
                        "conversation_id": MAX_ROOT,
                    },
                    "path_generation_id": "perf-history-generation",
                    "limit": 20,
                },
            )
            history_times.append((time.perf_counter() - started) * 1000)
            assert response.status_code == 200, response.text
        metrics["file_history"] = {
            "median_ms": round(statistics.median(history_times[2:]), 2),
            "p95_ms": round(_p95(history_times[2:]), 2),
            "queries": -1,
        }

        plan = client.portal.call(_direct_plan, client.app.state.engine)
        assert "ix_direct_turn_requests_session_latest" in plan
        overview_plan = client.portal.call(
            _statement_plan,
            client.app.state.engine,
            slowest["max_overview"][0],
            slowest["max_overview"][1],
        )
        assert "Execution Time:" in overview_plan
        recent_plans = {
            category: client.portal.call(
                _query_plan,
                client.app.state.engine,
                query[0],
                query[1],
            )
            for category, query in sampled_queries.items()
            if category in {"commands", "artifacts"}
        }
        assert set(recent_plans) == {"commands", "artifacts"}
        assert "ix_work_records_owner_category_order" in recent_plans["commands"]
        assert "WindowAgg" not in recent_plans["commands"]
        assert "Sort" not in recent_plans["commands"]
        assert "WindowAgg" not in recent_plans["artifacts"]
        assert "external" not in recent_plans["artifacts"].lower()
        summary_plan = client.portal.call(
            _statement_plan,
            client.app.state.engine,
            sampled_queries["summary"][0],
            sampled_queries["summary"][1],
        )
        call_state_plan = client.portal.call(
            _statement_plan,
            client.app.state.engine,
            sampled_queries["call_state"][0],
            sampled_queries["call_state"][1],
        )
        assert "Execution Time:" in summary_plan
        assert "Execution Time:" in call_state_plan
        assert "ix_work_records_owner_version_call_state" in call_state_plan
        print(
            {
                "rows": {
                    "logical_workstreams": 200,
                    "physical_sessions": MAX_PHYSICAL_SESSIONS,
                    "work_records": 100_000,
                    "work_record_files": 80_001,
                    "current_files": 2_500,
                    "direct_turns": 5_000,
                    "tasks": 20,
                    "managed_links": 1,
                },
                "metrics": metrics,
                "fresh_live_append": {
                    "samples": len(warm_append),
                    "append_p95_ms": round(_p95(warm_append), 2),
                    "read_p95_ms": round(_p95(warm_read), 2),
                    "end_to_end_p95_ms": round(_p95(warm_end_to_end), 2),
                    "record_counter_plan": counter_plans["records"],
                    "file_counter_plan": counter_plans["files"],
                },
                "concurrent_overview_10_total_ms": round(concurrency_ms, 2),
                "concurrent_overview_10_p95_ms": round(
                    _p95([elapsed for _status, elapsed in concurrent_results]), 2
                ),
                "direct_turn_plan": plan,
                "slowest_overview_sql_ms": round(slowest["max_overview"][2], 2),
                "slowest_overview_plan": overview_plan,
                "recent_stream_plans": recent_plans,
                "summary_plan": summary_plan,
                "call_state_plan": call_state_plan,
            }
        )
