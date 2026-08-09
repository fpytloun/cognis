"""Task-control persistence and hard inventory policy tests."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta

import pytest

from cognis.api.runtime_support import task_control_tool_allowed
from cognis.core.agent_loop import TASK_CONTROL_CONTROLLER_TOOL_NAMES
from cognis.core.task_control import (
    TASK_CONTROL_INSTRUCTION,
    _render_task_control_context,
    build_task_control_turn_context,
)
from cognis.models.tool import NativeToolDefinition, ToolSource
from cognis.store.database import create_engine, create_session_factory
from cognis.store.models import Agent, Base, Conversation, User
from cognis.store.queries import (
    assert_task_attempt_current,
    claim_task_control_conversation,
    clear_stale_task_control_conversation_claim,
    create_conversation,
    create_session,
    create_step_run,
    create_task,
    get_task,
    mark_task_control_conversation_ready,
    update_step_run,
)


def _tool(name: str, *, read_only: bool) -> NativeToolDefinition:
    return NativeToolDefinition(
        name=name,
        description=f"{name} test tool.",
        parameters={"type": "object", "properties": {}},
        source=ToolSource(type="executor"),
        category="filesystem",
        read_only=read_only,
    )


def test_task_control_inventory_is_a_hard_positive_allowlist() -> None:
    assert task_control_tool_allowed(_tool("read", read_only=True))
    assert task_control_tool_allowed(_tool("grep", read_only=True))
    assert task_control_tool_allowed(_tool("web_search", read_only=True))
    assert not task_control_tool_allowed(_tool("bash", read_only=False))
    assert not task_control_tool_allowed(_tool("write", read_only=False))
    assert not task_control_tool_allowed(_tool("apply_patch", read_only=False))
    assert not task_control_tool_allowed(_tool("search_tools", read_only=True))
    assert not task_control_tool_allowed(_tool("memory_add", read_only=False))


def test_task_control_guidance_separates_context_from_plan_revision() -> None:
    assert "update the active primary session at its next safe boundary" in TASK_CONTROL_INSTRUCTION
    assert "Do not replay the original step prompt" in TASK_CONTROL_INSTRUCTION
    assert 'target_step="plan"' in TASK_CONTROL_INSTRUCTION
    assert "delegate" not in TASK_CONTROL_CONTROLLER_TOOL_NAMES
    assert "switch_executor" not in TASK_CONTROL_CONTROLLER_TOOL_NAMES
    assert "create_task" not in TASK_CONTROL_CONTROLLER_TOOL_NAMES


@pytest.mark.asyncio
async def test_task_control_conversation_claim_is_concurrency_safe(tmp_path: object) -> None:
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path}/task-control.db")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = create_session_factory(engine)
    try:
        async with factory() as session:
            session.add(User(email="owner@example.com", name="Owner", role="admin"))
            await session.flush()
            session.add(
                Agent(
                    agent_id="agent-1",
                    owner_email="owner@example.com",
                    name="Agent",
                )
            )
            await session.flush()
            session.add_all(
                [
                    Conversation(
                        conversation_id="conv-control-a",
                        user_email="owner@example.com",
                        agent_id="agent-1",
                        context_type="web",
                    ),
                    Conversation(
                        conversation_id="conv-control-b",
                        user_email="owner@example.com",
                        agent_id="agent-1",
                        context_type="web",
                    ),
                ]
            )
            await session.flush()
            task = await create_task(
                session,
                created_by="owner@example.com",
                agent_id="agent-1",
                title="Controlled task",
                status="draft",
            )
            await session.commit()
            task_id = task.task_id

        async def claim(conversation_id: str) -> tuple[str, bool]:
            async with factory() as session:
                claimed = await claim_task_control_conversation(
                    session,
                    task_id,
                    conversation_id,
                )
                await session.commit()
                return conversation_id, claimed

        results = await asyncio.gather(claim("conv-control-a"), claim("conv-control-b"))
        winners = [conversation_id for conversation_id, claimed in results if claimed]
        assert len(winners) == 1
        async with factory() as session:
            persisted = await get_task(session, task_id)
            assert persisted is not None
            assert persisted.control_conversation_id == winners[0]
            assert await assert_task_attempt_current(session, task_id, 1)
            persisted.attempt_number = 2
            await session.commit()
        async with factory() as session:
            assert not await assert_task_attempt_current(session, task_id, 1)
            await session.rollback()
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_task_control_context_uses_persisted_step_projection_fields(tmp_path: object) -> None:
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path}/task-control-context.db")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = create_session_factory(engine)
    try:
        async with factory() as session:
            session.add(User(email="owner@example.com", name="Owner", role="admin"))
            await session.flush()
            session.add(
                Agent(
                    agent_id="agent-1",
                    owner_email="owner@example.com",
                    name="Agent",
                )
            )
            await session.flush()
            task = await create_task(
                session,
                created_by="owner@example.com",
                agent_id="agent-1",
                title="Persisted control task",
                description="Keep the control context bounded.",
                expected_output="A verified result.",
                status="running",
            )
            assert await claim_task_control_conversation(
                session,
                task.task_id,
                "conv-control",
            )
            conversation = await create_conversation(
                session,
                conversation_id="conv-control",
                user_email="owner@example.com",
                agent_id="agent-1",
                context_type="web",
                context_ref=f"web:task_control:{task.task_id}",
                context_data={"kind": "task_control", "task_id": task.task_id},
            )
            control_session = await create_session(
                session,
                conversation.conversation_id,
                "owner@example.com",
                "agent-1",
                session_id="sess-control",
            )
            running = await create_step_run(
                session,
                task_id=task.task_id,
                step_name="implement",
                step_type="run",
                agent_id="agent-1",
                conversation_id="conv-step-running",
                step_run_id="sr-running",
                status="running",
                started_at=datetime.now(UTC) - timedelta(minutes=2),
            )
            await update_step_run(
                session,
                running.step_run_id,
                session_id="sess-step-running",
            )
            await create_step_run(
                session,
                task_id=task.task_id,
                step_name="review",
                step_type="run",
                agent_id="agent-1",
                conversation_id="conv-step-completed",
                step_run_id="sr-completed",
                status="completed",
                started_at=datetime.now(UTC) - timedelta(minutes=1),
                completed_at=datetime.now(UTC),
            )
            assert await mark_task_control_conversation_ready(
                session,
                task.task_id,
                conversation.conversation_id,
            )
            await session.commit()
            assert control_session.session_id == "sess-control"

        context = await build_task_control_turn_context(
            factory,
            task_id=task.task_id,
            conversation_id="conv-control",
        )

        assert len(context) < 16_000
        serialized = context.split(
            "This snapshot is refreshed for the current turn and is not transcript history.\n",
            1,
        )[1].rsplit("\n</task_control>", 1)[0]
        payload = json.loads(serialized)
        assert payload["task"]["status"] == "running"
        assert payload["latest_steps"] == [
            {
                "step_run_id": "sr-running",
                "step_name": "implement",
                "status": "running",
                "attempt": 1,
                "conversation_id": "conv-step-running",
                "session_id": "sess-step-running",
                "deliverable_id": None,
                "started_at": payload["latest_steps"][0]["started_at"],
            },
            {
                "step_run_id": "sr-completed",
                "step_name": "review",
                "status": "completed",
                "attempt": 1,
                "conversation_id": "conv-step-completed",
                "session_id": None,
                "deliverable_id": None,
                "started_at": payload["latest_steps"][1]["started_at"],
            },
        ]
        assert payload["links"]["control_chat"] == "/chat/conv-control"

        async with factory() as session:
            assert not await claim_task_control_conversation(
                session,
                task.task_id,
                "conv-other",
            )
            await session.rollback()
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_delayed_task_control_creator_cannot_finalize_after_claim_replacement(
    tmp_path: object,
) -> None:
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path}/task-control-lease.db")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = create_session_factory(engine)
    try:
        async with factory() as session:
            session.add(User(email="owner@example.com", name="Owner", role="admin"))
            await session.flush()
            session.add(
                Agent(
                    agent_id="agent-1",
                    owner_email="owner@example.com",
                    name="Agent",
                )
            )
            await session.flush()
            task = await create_task(
                session,
                created_by="owner@example.com",
                agent_id="agent-1",
                title="Delayed control task",
                status="running",
            )
            assert await claim_task_control_conversation(session, task.task_id, "conv-delayed")
            task.control_conversation_claimed_at = datetime.now(UTC) - timedelta(seconds=31)
            await session.commit()

        async with factory() as session:
            assert await clear_stale_task_control_conversation_claim(
                session,
                task.task_id,
                "conv-delayed",
                claimed_before=datetime.now(UTC) - timedelta(seconds=30),
            )
            assert await claim_task_control_conversation(session, task.task_id, "conv-winner")
            await session.commit()

        async with factory() as session:
            assert not await mark_task_control_conversation_ready(
                session,
                task.task_id,
                "conv-delayed",
            )
            persisted = await get_task(session, task.task_id)
            assert persisted is not None
            assert persisted.control_conversation_id == "conv-winner"
            assert persisted.control_conversation_claimed_at is not None
            await session.rollback()
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_stale_clear_cannot_remove_claim_that_became_ready(tmp_path: object) -> None:
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path}/task-control-ready-race.db")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = create_session_factory(engine)
    try:
        async with factory() as session:
            session.add(User(email="owner@example.com", name="Owner", role="admin"))
            await session.flush()
            session.add(
                Agent(
                    agent_id="agent-1",
                    owner_email="owner@example.com",
                    name="Agent",
                )
            )
            await session.flush()
            task = await create_task(
                session,
                created_by="owner@example.com",
                agent_id="agent-1",
                title="Ready control task",
                status="running",
            )
            assert await claim_task_control_conversation(session, task.task_id, "conv-ready")
            task.control_conversation_claimed_at = datetime.now(UTC) - timedelta(seconds=31)
            await session.commit()

        stale_cutoff = datetime.now(UTC) - timedelta(seconds=30)
        async with factory() as session:
            assert await mark_task_control_conversation_ready(
                session,
                task.task_id,
                "conv-ready",
            )
            await session.commit()

        async with factory() as session:
            assert not await clear_stale_task_control_conversation_claim(
                session,
                task.task_id,
                "conv-ready",
                claimed_before=stale_cutoff,
            )
            persisted = await get_task(session, task.task_id)
            assert persisted is not None
            assert persisted.control_conversation_id == "conv-ready"
            assert persisted.control_conversation_claimed_at is None
    finally:
        await engine.dispose()


def test_task_control_context_bound_includes_escaped_envelope() -> None:
    context = _render_task_control_context(
        {"description": ('"quoted"\\line\n' * 4_000), "status": "running"}
    )

    assert len(context) <= 16_000
    assert '"truncated":true' in context
    assert "Use the allowed task/output tools" in context
