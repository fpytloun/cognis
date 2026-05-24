"""Tests for task deliverable/step-run continuation tools."""

from __future__ import annotations

import pytest

from cognis.models.tool import ExecutorHandle
from cognis.store.database import create_engine, create_session_factory
from cognis.store.models import Agent, Base, User
from cognis.store.queries import create_deliverable, create_step_run, create_task
from cognis.tools.builtin.task_continuation import build_task_continuation_tool_handlers
from cognis.tools.registry import ToolExecutionContext


def _context(
    user_email: str,
    *,
    scope_task_id: str | None = None,
) -> ToolExecutionContext:
    runtime_metadata: dict[str, object] = {"user_email": user_email}
    if scope_task_id is not None:
        runtime_metadata["conversation_context"] = {
            "platform_data": {
                "forked_from": "task",
                "task_id": scope_task_id,
            }
        }
    return ToolExecutionContext(
        executor_handle=ExecutorHandle(executor_id="test", executor_type="in_process"),
        runtime_metadata=runtime_metadata,
    )


@pytest.fixture
async def task_continuation_db(tmp_path: object):
    """Create a small task DB with one user-owned deliverable and one foreign task."""

    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path}/task-continuation.db")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = create_session_factory(engine)

    async with factory() as session:
        session.add_all(
            [
                User(email="owner@example.com", name="Owner", role="user"),
                User(email="other@example.com", name="Other", role="user"),
            ]
        )
        await session.flush()
        session.add_all(
            [
                Agent(agent_id="agent-owner", owner_email="owner@example.com", name="Owner Agent"),
                Agent(agent_id="agent-other", owner_email="other@example.com", name="Other Agent"),
            ]
        )
        await session.flush()
        task = await create_task(
            session,
            task_id="task-owner",
            created_by="owner@example.com",
            agent_id="agent-owner",
            title="Owner task",
            status="completed",
        )
        step_run = await create_step_run(
            session,
            step_run_id="sr-owner",
            task_id=task.task_id,
            step_name="execute",
            step_type="direct",
            agent_id="agent-owner",
            conversation_id="conv-owner",
            status="approved",
            runtime_info={"source": "test"},
        )
        deliverable = await create_deliverable(
            session,
            deliverable_id="dlv_owner",
            step_run_id=step_run.step_run_id,
            content="# Full report\n\nComplete deliverable body.",
            format="markdown",
            title="Full report",
            outputs={"kind": "report"},
        )
        step_run.deliverable_id = deliverable.deliverable_id

        sibling_task = await create_task(
            session,
            task_id="task-sibling",
            created_by="owner@example.com",
            agent_id="agent-owner",
            title="Owner sibling task",
            status="completed",
        )
        sibling_step_run = await create_step_run(
            session,
            step_run_id="sr-sibling",
            task_id=sibling_task.task_id,
            step_name="execute",
            step_type="direct",
            agent_id="agent-owner",
            status="approved",
        )
        await create_deliverable(
            session,
            deliverable_id="dlv_sibling",
            step_run_id=sibling_step_run.step_run_id,
            content="Sibling task content",
            format="plain",
            title="Sibling notes",
        )

        foreign_task = await create_task(
            session,
            task_id="task-other",
            created_by="other@example.com",
            agent_id="agent-other",
            title="Other task",
            status="completed",
        )
        foreign_step_run = await create_step_run(
            session,
            step_run_id="sr-other",
            task_id=foreign_task.task_id,
            step_name="execute",
            step_type="direct",
            agent_id="agent-other",
            status="approved",
        )
        await create_deliverable(
            session,
            deliverable_id="dlv_other",
            step_run_id=foreign_step_run.step_run_id,
            content="Other user content",
        )
        await session.commit()

    try:
        yield factory
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_read_task_deliverable_allows_owned_deliverable_from_main_chat(
    task_continuation_db,
) -> None:
    handlers = build_task_continuation_tool_handlers(task_continuation_db)

    result = await handlers["read_task_deliverable"](
        {"deliverable_id": "dlv_owner"},
        _context("owner@example.com"),
    )

    assert result["ok"] is True
    assert result["task_id"] == "task-owner"
    assert result["deliverable_id"] == "dlv_owner"
    assert result["step_run_id"] == "sr-owner"
    assert result["content"] == "# Full report\n\nComplete deliverable body."
    assert result["outputs"] == {"kind": "report"}


@pytest.mark.asyncio
async def test_list_task_step_runs_allows_owned_task_from_main_chat(
    task_continuation_db,
) -> None:
    handlers = build_task_continuation_tool_handlers(task_continuation_db)

    result = await handlers["list_task_step_runs"](
        {"task_id": "task-owner"},
        _context("owner@example.com"),
    )

    assert result["ok"] is True
    assert result["task_id"] == "task-owner"
    assert result["step_runs"] == [
        {
            "step_run_id": "sr-owner",
            "step_name": "execute",
            "step_type": "direct",
            "status": "approved",
            "attempt": 1,
            "agent_id": "agent-owner",
            "conversation_id": "conv-owner",
            "session_id": None,
            "intaris_session_id": None,
            "deliverable_id": "dlv_owner",
            "runtime_info": {"source": "test"},
        }
    ]


@pytest.mark.asyncio
async def test_task_continuation_tools_keep_cross_user_access_denied(
    task_continuation_db,
) -> None:
    handlers = build_task_continuation_tool_handlers(task_continuation_db)

    deliverable_result = await handlers["read_task_deliverable"](
        {"deliverable_id": "dlv_owner"},
        _context("other@example.com"),
    )
    step_runs_result = await handlers["list_task_step_runs"](
        {"task_id": "task-owner"},
        _context("other@example.com"),
    )

    assert deliverable_result == {"ok": False, "error": "not_found"}
    assert step_runs_result == {"ok": False, "error": "not_found"}


@pytest.mark.asyncio
async def test_task_continuation_tools_keep_fork_scope_restriction(
    task_continuation_db,
) -> None:
    handlers = build_task_continuation_tool_handlers(task_continuation_db)

    deliverable_result = await handlers["read_task_deliverable"](
        {"deliverable_id": "dlv_owner"},
        _context("owner@example.com", scope_task_id="task-other"),
    )
    step_runs_result = await handlers["list_task_step_runs"](
        {"task_id": "task-owner"},
        _context("owner@example.com", scope_task_id="task-other"),
    )

    assert deliverable_result == {"ok": False, "error": "not_found"}
    assert step_runs_result == {"ok": False, "error": "outside_continuation_scope"}
