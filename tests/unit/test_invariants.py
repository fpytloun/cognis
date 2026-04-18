"""Tests for cross-subsystem invariant checkers and reconcilers."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from cognis.core.invariants import check_invariants, reconcile_invariants
from cognis.store.database import create_engine, create_session_factory
from cognis.store.models import Agent, Base, User
from cognis.store.queries import create_step_run, create_task, update_step_run


async def _bootstrap_db(tmp_path: object) -> tuple[object, object]:
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path}/cognis.db")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = create_session_factory(engine)

    async with factory() as session:
        session.add(User(email="user@test.com", name="Test", role="admin"))
        await session.flush()
        session.add(
            Agent(
                agent_id="agent-1",
                owner_email="user@test.com",
                name="Agent",
            )
        )
        await session.commit()

    return engine, factory


@pytest.mark.asyncio
async def test_check_invariants_reports_zero_on_clean_db(tmp_path: object) -> None:
    engine, factory = await _bootstrap_db(tmp_path)
    try:
        async with factory() as session:
            reports = await check_invariants(session)
        assert all(report.current_count == 0 for report in reports)
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_reconcile_invariants_is_idempotent(tmp_path: object) -> None:
    engine, factory = await _bootstrap_db(tmp_path)
    try:
        async with factory() as session:
            # Seed a terminal task with a leaked running step_run
            await create_task(
                session,
                created_by="user@test.com",
                agent_id="agent-1",
                title="Terminal failed task",
                status="failed",
                task_id="task_bad",
            )
            await create_step_run(
                session,
                task_id="task_bad",
                step_name="execute",
                step_type="run",
                agent_id="agent-1",
                step_run_id="sr_leaked",
            )
            await update_step_run(session, "sr_leaked", status="running")
            await session.commit()

        async with factory() as session:
            reports_first = await reconcile_invariants(session)
        assert any(r.reconciled_count > 0 for r in reports_first)

        async with factory() as session:
            reports_second = await reconcile_invariants(session)
        # Idempotent — second pass reconciles nothing and reports zero remaining.
        assert all(r.reconciled_count == 0 for r in reports_second)
        assert all(r.current_count == 0 for r in reports_second)
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_reconcile_invariants_preserves_cancelled_semantics(tmp_path: object) -> None:
    engine, factory = await _bootstrap_db(tmp_path)
    try:
        async with factory() as session:
            await create_task(
                session,
                created_by="user@test.com",
                agent_id="agent-1",
                title="Cancelled task",
                status="cancelled",
                task_id="task_cancel",
            )
            await create_step_run(
                session,
                task_id="task_cancel",
                step_name="execute",
                step_type="run",
                agent_id="agent-1",
                step_run_id="sr_orphan",
            )
            await update_step_run(session, "sr_orphan", status="running")
            await session.commit()

        async with factory() as session:
            await reconcile_invariants(session)

        # Step run under a cancelled parent must be marked cancelled,
        # not failed.
        from cognis.store.queries import get_step_run

        async with factory() as session:
            row = await get_step_run(session, "sr_orphan")
            assert row is not None and row.status == "cancelled"
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_reconcile_invariants_covers_paused_and_evaluating_step_runs(
    tmp_path: object,
) -> None:
    engine, factory = await _bootstrap_db(tmp_path)
    try:
        async with factory() as session:
            await create_task(
                session,
                created_by="user@test.com",
                agent_id="agent-1",
                title="Dead task",
                status="failed",
                task_id="task_dead",
            )
            await create_step_run(
                session,
                task_id="task_dead",
                step_name="a",
                step_type="run",
                agent_id="agent-1",
                step_run_id="sr_paused",
            )
            await update_step_run(session, "sr_paused", status="running")
            await update_step_run(session, "sr_paused", status="paused")
            await create_step_run(
                session,
                task_id="task_dead",
                step_name="b",
                step_type="run",
                agent_id="agent-1",
                step_run_id="sr_evaluating",
            )
            await update_step_run(session, "sr_evaluating", status="running")
            await update_step_run(session, "sr_evaluating", status="evaluating")
            await session.commit()

        async with factory() as session:
            reports = await reconcile_invariants(session)
        orphans = next(
            r for r in reports if r.category == "non_terminal_step_runs_under_terminal_task"
        )
        assert orphans.reconciled_count == 2
        assert orphans.current_count == 0
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_reconcile_invariants_clears_active_session_pointer_to_terminal_session(
    tmp_path: object,
) -> None:
    engine, factory = await _bootstrap_db(tmp_path)
    try:
        from cognis.store.models import Conversation, Session as SessionRow

        async with factory() as session:
            conv = Conversation(
                conversation_id="conv-1",
                user_email="user@test.com",
                agent_id="agent-1",
                title="Test",
                active_session_id="sess-1",
                context_type="chat",
                context_ref=None,
                context_data={},
            )
            sess = SessionRow(
                session_id="sess-1",
                conversation_id="conv-1",
                user_email="user@test.com",
                agent_id="agent-1",
                status="completed",
                started_at=datetime.now(UTC),
            )
            session.add(conv)
            session.add(sess)
            await session.commit()

        async with factory() as session:
            reports = await reconcile_invariants(session)
        cleared = next(
            r for r in reports if r.category == "conversations_with_terminal_active_session"
        )
        assert cleared.reconciled_count == 1

        async with factory() as session:
            from sqlalchemy import select

            row = (
                await session.execute(
                    select(Conversation).where(Conversation.conversation_id == "conv-1")
                )
            ).scalar_one()
            assert row.active_session_id is None
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_reconcile_invariants_clears_active_session_pointer_to_missing_session(
    tmp_path: object,
) -> None:
    engine, factory = await _bootstrap_db(tmp_path)
    try:
        from cognis.store.models import Conversation

        async with factory() as session:
            conv = Conversation(
                conversation_id="conv-missing",
                user_email="user@test.com",
                agent_id="agent-1",
                title="Missing session",
                active_session_id="sess-missing",
                context_type="chat",
                context_ref=None,
                context_data={},
            )
            session.add(conv)
            await session.commit()

        async with factory() as session:
            reports = await reconcile_invariants(session)
        cleared = next(
            r for r in reports if r.category == "conversations_with_missing_active_session"
        )
        assert cleared.reconciled_count == 1

        async with factory() as session:
            from sqlalchemy import select

            row = (
                await session.execute(
                    select(Conversation).where(Conversation.conversation_id == "conv-missing")
                )
            ).scalar_one()
            assert row.active_session_id is None
    finally:
        await engine.dispose()
