# ruff: noqa: F811
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from cognis.core.hook_evidence import idle_evidence, turn_evidence
from cognis.store.models import AgentHookRunRow, Session
from tests.unit.test_agent_hooks import (  # noqa: F401
    assign,
    conversation,
    factory,
    runtime,
    session,
)


def events():
    return [
        {"seq": 1, "type": "user_message", "data": {"turn_id": "work", "content": "Implement"}},
        {
            "seq": 2,
            "type": "tool_result",
            "data": {
                "turn_id": "work",
                "call_id": "write-file",
                "status": "success",
                "file_diffs": [{"path": "src/main.py"}],
            },
        },
        {"seq": 3, "type": "user_message", "data": {"turn_id": "question", "content": "Explain"}},
        {
            "seq": 4,
            "type": "assistant_message",
            "data": {"turn_id": "question", "content": "Explanation"},
        },
    ]


def test_idle_aggregates_work_but_messages_remain_last_turn():
    result = idle_evidence(events(), "question")
    assert result["work"]["has_work"]
    assert result["work"]["successful_tool_calls"] == 1
    assert result["last_user_message"] == "Explain"
    assert not turn_evidence(events(), "question")["work"]["has_work"]


def test_automated_turn_work_is_not_foreground_evidence():
    automated = events()
    automated[0]["data"]["system_initiated"] = True
    assert not idle_evidence(automated, "question")["work"]["has_work"]


async def match(service, occurrence):
    async with service.session_factory() as db:
        source = await db.get(Session, "session-hooks")
    service._idle_origin = AsyncMock(
        return_value=(occurrence, datetime.now(UTC), datetime.now(UTC))
    )
    await service.match(
        conversation=conversation(),
        session=source,
        triggers=["conversation_idle"],
        occurrence=occurrence,
        context={"turn": {"id": "question"}, "idle": {"seconds": 9999}},
    )


@pytest.mark.asyncio
async def test_window_is_per_hook_and_failed_runs_do_not_consume_work(factory):
    definition = {
        "name": "Learn",
        "trigger": "conversation_idle",
        "mode": "fork",
        "idle_seconds": 60,
        "condition": "{{ work.has_work }}",
        "prompt": "{{ work.file_count }}",
    }
    first = await assign(factory, definition)
    second = await assign(factory, {**definition, "name": "Other learner"})
    service = runtime(factory)
    service.providers.guardrails.streams["session-hooks"] = events()
    await match(service, "idle:first")
    async with factory() as db:
        rows = list((await db.scalars(select(AgentHookRunRow))).all())
        assert len(rows) == 2
        for row in rows:
            row.status = "completed" if row.hook_id == first else "failed"
            row.finished_at = datetime.now(UTC)
        await db.commit()
    # Failed runs retain old work, but a successful run consumes only its own window.
    await match(service, "idle:second")
    async with factory() as db:
        rows = list(
            (
                await db.scalars(
                    select(AgentHookRunRow).where(AgentHookRunRow.occurrence == "idle:second")
                )
            ).all()
        )
        assert [row.hook_id for row in rows] == [second]


@pytest.mark.asyncio
async def test_injected_revision_without_snapshot_does_not_block_idle_revision(factory):
    hook_id = await assign(
        factory,
        {
            "name": "Learn",
            "trigger": "conversation_idle",
            "mode": "fresh",
            "idle_seconds": 60,
            "condition": "{{ work.has_work }}",
            "prompt": "Learn",
        },
    )
    async with factory() as db:
        db.add(
            AgentHookRunRow(
                run_id="injected-revision",
                hook_id=hook_id,
                hook_revision=1,
                owner_email="owner@hooks.test",
                agent_id="agent-hooks",
                conversation_id="conv-hooks",
                source_session_id="session-hooks",
                occurrence="message:old",
                configuration={"mode": "inject"},
                status="completed",
            )
        )
        await db.commit()
    service = runtime(factory)
    service.providers.guardrails.streams["session-hooks"] = events()
    await match(service, "idle:converted")
    async with factory() as db:
        assert (
            await db.scalar(
                select(AgentHookRunRow).where(AgentHookRunRow.occurrence == "idle:converted")
            )
            is not None
        )


@pytest.mark.asyncio
async def test_snapshot_not_completion_time_and_same_occurrence_cache_invalidation(factory):
    hook_id = await assign(
        factory,
        {
            "name": "Learn",
            "trigger": "conversation_idle",
            "mode": "fork",
            "idle_seconds": 60,
            "condition": "{{ work.successful_tool_calls == 1 }}",
            "prompt": "Learn",
        },
    )
    service = runtime(factory)
    stream = events()
    # Two successful writes initially cause the condition to fail and cache that window.
    stream += [
        {
            "seq": 5,
            "type": "lifecycle",
            "data": {
                "event": "hook_snapshot",
                "hook_run_id": "previous-success",
            },
        },
        {"seq": 6, "type": "user_message", "data": {"turn_id": "later", "content": "Continue"}},
        {
            "seq": 7,
            "type": "tool_result",
            "data": {
                "turn_id": "later",
                "call_id": "later-write",
                "status": "success",
                "file_diffs": [{"path": "later.py"}],
            },
        },
    ]
    service.providers.guardrails.streams["session-hooks"] = stream
    await match(service, "idle:same")
    async with factory() as db:
        assert await db.scalar(select(AgentHookRunRow)) is None
        db.add(
            AgentHookRunRow(
                run_id="previous-success",
                hook_id=hook_id,
                hook_revision=1,
                owner_email="owner@hooks.test",
                agent_id="agent-hooks",
                conversation_id="conv-hooks",
                source_session_id="session-hooks",
                occurrence="idle:previous",
                configuration={"mode": "fork", "action": "prompt"},
                status="completed",
                handoff={"snapshot": {"session_id": "session-hooks", "seq": 5}},
                finished_at=datetime.now(UTC),
            )
        )
        await db.commit()
    await match(service, "idle:same")
    async with factory() as db:
        assert (
            await db.scalar(
                select(AgentHookRunRow).where(AgentHookRunRow.occurrence == "idle:same")
            )
            is not None
        )
    assert any(
        call.kwargs.get("after_seq") == 5
        for call in service.providers.guardrails.read_events.await_args_list
    )


@pytest.mark.asyncio
async def test_rollover_reads_old_root_work_not_child_execution(factory):
    await assign(
        factory,
        {
            "name": "Learn",
            "trigger": "conversation_idle",
            "mode": "fork",
            "condition": "{{ work.file_count == 1 }}",
            "idle_seconds": 60,
            "prompt": "Learn",
        },
    )
    service = runtime(factory)
    async with factory() as db:
        source = await db.get(Session, "session-hooks")
        source.started_at = datetime.now(UTC) - timedelta(minutes=1)
        db.add(
            Session(
                session_id="old-root",
                conversation_id="conv-hooks",
                agent_id="agent-hooks",
                user_email="owner@hooks.test",
                started_at=source.started_at - timedelta(minutes=1),
            )
        )
        db.add(
            Session(
                session_id="hook-child",
                conversation_id="conv-hooks",
                agent_id="agent-hooks",
                user_email="owner@hooks.test",
                parent_session_id="old-root",
                started_at=source.started_at - timedelta(seconds=1),
            )
        )
        await db.commit()
    service.providers.guardrails.streams["old-root"] = events()[:2]
    service.providers.guardrails.streams["session-hooks"] = events()[2:]
    service.providers.guardrails.streams["hook-child"] = [
        *events()[:1],
        {
            "seq": 2,
            "type": "tool_result",
            "data": {
                "turn_id": "work",
                "status": "success",
                "file_diffs": [{"path": "ignore.py"}],
            },
        },
    ]
    await match(service, "idle:rollover")
    async with factory() as db:
        assert await db.scalar(select(AgentHookRunRow)) is not None
