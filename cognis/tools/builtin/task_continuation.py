"""Built-in read-only tools for task continuation chats."""

from __future__ import annotations

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from cognis.models.tool import ToolDefinition, ToolSource
from cognis.store.queries import get_deliverable, get_task, list_step_runs_for_task
from cognis.tools.registry import ToolExecutionContext

_SOURCE = ToolSource(type="builtin")

READ_TASK_DELIVERABLE_TOOL = ToolDefinition(
    name="read_task_deliverable",
    description="Read a task workflow deliverable by deliverable ID. Only works for deliverables owned by the current user.",
    parameters={
        "type": "object",
        "properties": {
            "deliverable_id": {
                "type": "string",
                "description": "Deliverable ID to read.",
            }
        },
        "required": ["deliverable_id"],
    },
    source=_SOURCE,
    category="task_continuation",
    read_only=True,
)

LIST_TASK_STEP_RUNS_TOOL = ToolDefinition(
    name="list_task_step_runs",
    description="List step runs for one of the current user's tasks, including session and deliverable references for deeper inspection.",
    parameters={
        "type": "object",
        "properties": {
            "task_id": {
                "type": "string",
                "description": "Task ID to inspect.",
            }
        },
        "required": ["task_id"],
    },
    source=_SOURCE,
    category="task_continuation",
    read_only=True,
)


def task_continuation_tools() -> list[ToolDefinition]:
    """Return task continuation tool definitions."""

    return [READ_TASK_DELIVERABLE_TOOL, LIST_TASK_STEP_RUNS_TOOL]


def _user_email(context: ToolExecutionContext) -> str | None:
    runtime_access = context.runtime_metadata.get("runtime_access")
    if isinstance(runtime_access, dict) and isinstance(runtime_access.get("user_email"), str):
        return runtime_access["user_email"]
    if isinstance(context.runtime_metadata.get("user_email"), str):
        return context.runtime_metadata["user_email"]
    if isinstance(context.shared_runtime_metadata, dict) and isinstance(
        context.shared_runtime_metadata.get("user_email"), str
    ):
        return context.shared_runtime_metadata["user_email"]
    return None


def _continuation_task_id(context: ToolExecutionContext) -> str | None:
    conversation_context = context.runtime_metadata.get("conversation_context")
    if not isinstance(conversation_context, dict):
        return None
    platform_data = conversation_context.get("platform_data")
    if not isinstance(platform_data, dict):
        return None
    if platform_data.get("forked_from") not in {"task", "task_step"}:
        return None
    task_id = platform_data.get("task_id")
    return task_id if isinstance(task_id, str) and task_id else None


def build_task_continuation_tool_handlers(
    session_factory: async_sessionmaker[AsyncSession],
) -> dict[str, Any]:
    """Build runtime handlers for task continuation tools."""

    async def read_task_deliverable_handler(
        arguments: dict[str, Any], context: ToolExecutionContext
    ) -> dict[str, Any]:
        user_email = _user_email(context)
        scope_task_id = _continuation_task_id(context)
        deliverable_id = str(arguments.get("deliverable_id") or "").strip()
        if not user_email or not scope_task_id or not deliverable_id:
            return {"ok": False, "error": "missing_user_or_deliverable_id"}
        async with session_factory() as session:
            deliverable = await get_deliverable(session, deliverable_id)
            if deliverable is None:
                return {"ok": False, "error": "not_found"}
            task = await _task_for_step_run(session, deliverable.step_run_id)
            if task is None or task.created_by != user_email or task.task_id != scope_task_id:
                return {"ok": False, "error": "not_found"}
            return {
                "ok": True,
                "deliverable_id": deliverable.deliverable_id,
                "step_run_id": deliverable.step_run_id,
                "version": deliverable.version,
                "status": deliverable.status,
                "format": deliverable.format,
                "title": deliverable.title,
                "content": deliverable.content,
                "outputs": deliverable.outputs or {},
            }

    async def list_task_step_runs_handler(
        arguments: dict[str, Any], context: ToolExecutionContext
    ) -> dict[str, Any]:
        user_email = _user_email(context)
        scope_task_id = _continuation_task_id(context)
        task_id = str(arguments.get("task_id") or "").strip()
        if not user_email or not scope_task_id or not task_id:
            return {"ok": False, "error": "missing_user_or_task_id"}
        if task_id != scope_task_id:
            return {"ok": False, "error": "outside_continuation_scope"}
        async with session_factory() as session:
            task = await get_task(session, task_id)
            if task is None or task.created_by != user_email:
                return {"ok": False, "error": "not_found"}
            rows = await list_step_runs_for_task(session, task_id)
            return {
                "ok": True,
                "task_id": task_id,
                "step_runs": [
                    {
                        "step_run_id": row.step_run_id,
                        "step_name": row.step_name,
                        "step_type": row.step_type,
                        "status": row.status,
                        "attempt": row.attempt,
                        "agent_id": row.agent_id,
                        "conversation_id": row.conversation_id,
                        "session_id": row.session_id,
                        "intaris_session_id": row.intaris_session_id,
                        "deliverable_id": row.deliverable_id,
                        "runtime_info": row.runtime_info or {},
                    }
                    for row in rows
                ],
            }

    return {
        READ_TASK_DELIVERABLE_TOOL.name: read_task_deliverable_handler,
        LIST_TASK_STEP_RUNS_TOOL.name: list_task_step_runs_handler,
    }


async def _task_for_step_run(session: AsyncSession, step_run_id: str) -> Any | None:
    from sqlalchemy import select

    from cognis.store.models import StepRun

    result = await session.execute(
        select(StepRun.task_id).where(StepRun.step_run_id == step_run_id)
    )
    task_id = result.scalar_one_or_none()
    if not isinstance(task_id, str):
        return None
    return await get_task(session, task_id)
