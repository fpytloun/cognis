"""Tests for workflow task comments."""

from __future__ import annotations

import pytest

from cognis.store.database import create_engine, create_session_factory
from cognis.store.models import Base
from cognis.store.queries import (
    claim_pending_context_task_comments,
    create_agent,
    create_task,
    create_task_comment,
    create_user,
    list_task_comments,
)


@pytest.mark.asyncio
async def test_claim_pending_context_task_comments_claims_only_matching_context(
    tmp_path,
) -> None:
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path}/task-comments.db")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    session_factory = create_session_factory(engine)

    try:
        async with session_factory() as session:
            await create_user(session, "user@example.com", "User", "hash")
            await create_agent(
                session,
                agent_id="agent-1",
                owner_email="user@example.com",
                name="Agent",
            )
            await create_task(
                session,
                task_id="task-1",
                created_by="user@example.com",
                agent_id="agent-1",
                title="Task",
                status="running",
            )
            first = await create_task_comment(
                session,
                task_id="task-1",
                author_email="user@example.com",
                body="General context",
                intent="context_only",
                target_step=None,
                attempt_number=2,
            )
            targeted = await create_task_comment(
                session,
                task_id="task-1",
                author_email="user@example.com",
                body="Build context",
                intent="context_only",
                target_step="build",
                attempt_number=2,
            )
            await create_task_comment(
                session,
                task_id="task-1",
                author_email="user@example.com",
                body="Plan context",
                intent="context_only",
                target_step="plan",
                attempt_number=2,
            )
            await create_task_comment(
                session,
                task_id="task-1",
                author_email="user@example.com",
                body="Record only",
                intent="record_only",
                attempt_number=2,
            )
            await create_task_comment(
                session,
                task_id="task-1",
                author_email="user@example.com",
                body="Old attempt",
                intent="context_only",
                attempt_number=1,
            )
            await session.commit()

        async with session_factory() as session:
            claimed = await claim_pending_context_task_comments(
                session,
                task_id="task-1",
                step_name="build",
                attempt_number=2,
                step_run_id="sr-1",
                reason="after_tool_cycle",
            )
            await session.commit()

        assert [row.comment_id for row in claimed] == [first.comment_id, targeted.comment_id]

        async with session_factory() as session:
            comments = await list_task_comments(session, "task-1")

        by_body = {row.body: row for row in comments}
        assert by_body["General context"].applied is True
        general_metadata = dict(by_body["General context"].metadata_json or {})
        assert general_metadata["applied_step"] == "build"
        assert general_metadata["applied_step_run_id"] == "sr-1"
        assert general_metadata["applied_reason"] == "after_tool_cycle"
        assert by_body["Build context"].applied is True
        assert by_body["Plan context"].applied is False
        assert by_body["Record only"].applied is False
        assert by_body["Old attempt"].applied is False

        async with session_factory() as session:
            claimed_again = await claim_pending_context_task_comments(
                session,
                task_id="task-1",
                step_name="build",
                attempt_number=2,
                step_run_id="sr-1",
                reason="after_tool_cycle",
            )
            await session.commit()

        assert claimed_again == []
    finally:
        await engine.dispose()
