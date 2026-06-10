"""Tests for task deliverable/step-run continuation tools."""

from __future__ import annotations

import pytest

from cognis.models.tool import ExecutorHandle
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
