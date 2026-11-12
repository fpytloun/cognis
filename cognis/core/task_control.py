"""Task-control authority and bounded live context."""

from __future__ import annotations

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from cognis import json_codec as json
from cognis.api.task_projection import build_task_progress_projection
from cognis.core.orchestration_policy import is_managed_agent_conversation_context
from cognis.core.project_access import (
    agent_usable_in_project,
    resolve_project_role_by_id,
    role_satisfies,
)
from cognis.models.session import ConversationContext
from cognis.runtime_context import RuntimeAccessContext
from cognis.store.models import Task
from cognis.store.queries import (
    get_agent,
    get_conversation,
    get_session_row,
    get_task,
    list_step_runs_for_task_projection,
    list_task_comments,
)

TASK_CONTROL_INSTRUCTION = """You are operating a persistent Task Control Chat for exactly one task.
Discuss scope and ideas freely, but mutate only the owning task and only with the dedicated task tools.
Use add_task_context for agreed guidance to update the active primary session at its next safe boundary.
Do not replay the original step prompt when you add context.
Use request_task_revision with target_step="plan" for substantive scope or acceptance changes.
Use stronger pause/resume/rerun/cancel actions only when clearly requested.
Never execute code, edit files, use shell/process tools, mutate credentials or memory, create tasks/workflows,
or create/manage implementation conversations. Read heavy details through the allowed task/output tools."""
_MAX_TASK_CONTROL_CONTEXT_CHARS = 16_000


async def task_control_access_error(
    session: AsyncSession,
    *,
    access: RuntimeAccessContext,
    task: Task,
    control_task_id: str | None,
) -> str | None:
    """Revalidate primary-owner chat authority without treating a UI marker as a grant."""
    if (
        not access.is_root_owner_primary_chat
        or access.task_id
        or access.step_run_id
        or not access.session_id
        or not access.conversation_id
    ):
        return "task_control_owner_primary_chat_required"
    agent = await get_agent(session, str(access.agent_id))
    if (
        agent is None
        or agent.owner_email != access.user_email
        or agent.agent_type != "primary"
        or agent.is_system
        or agent.status != "active"
    ):
        return "task_control_agent_access_denied"
    chat = await get_conversation(session, access.conversation_id)
    current_session = await get_session_row(session, access.session_id)
    # Root rotation preserves the historical session agent. Interactive agent
    # authority belongs to the current conversation binding, not that provenance.
    if (
        chat is None
        or chat.user_email != access.user_email
        or chat.agent_id != access.agent_id
        or chat.status != "active"
        or chat.active_session_id != access.session_id
        or chat.context_type == "task"
        or is_managed_agent_conversation_context(
            ConversationContext(type=chat.context_type, platform_data=chat.context_data or {})
        )
        or current_session is None
        or current_session.conversation_id != chat.conversation_id
        or current_session.user_email != access.user_email
        or current_session.status != "active"
        or current_session.parent_session_id
        or current_session.delegation_mode
    ):
        return "task_control_session_access_denied"
    if task.created_by != access.user_email:
        return "task_control_task_access_denied"
    if (
        control_task_id is not None or (chat.context_data or {}).get("kind") == "task_control"
    ) and (
        control_task_id != task.task_id
        or task.control_conversation_id != chat.conversation_id
        or (chat.context_data or {}).get("kind") != "task_control"
        or (chat.context_data or {}).get("task_id") != task.task_id
    ):
        return "task_control_binding_revoked"
    for project_id in {chat.project_id, task.project_id} - {None}:
        project, role = await resolve_project_role_by_id(
            session, str(project_id), str(access.user_email)
        )
        if project is None or project.status != "active" or not role_satisfies(role.role, "use"):
            return "task_control_project_access_denied"
    task_agent = await get_agent(session, task.agent_id)
    if task_agent is None or not await agent_usable_in_project(
        session, agent=task_agent, user_email=str(access.user_email), project_id=task.project_id
    ):
        return "task_control_agent_access_denied"
    return None


def _render_task_control_context(payload: dict[str, Any]) -> str:
    prefix = (
        "<task_control>\n"
        f"{TASK_CONTROL_INSTRUCTION}\n\n"
        "This snapshot is refreshed for the current turn and is not transcript history.\n"
    )
    suffix = "\n</task_control>"
    serialized = json.dumps(payload, default=str, ensure_ascii=False, separators=(",", ":"))
    if len(prefix) + len(serialized) + len(suffix) <= _MAX_TASK_CONTROL_CONTEXT_CHARS:
        return f"{prefix}{serialized}{suffix}"

    excerpt_limit = max(0, _MAX_TASK_CONTROL_CONTEXT_CHARS - len(prefix) - len(suffix) - 256)
    while True:
        envelope = json.dumps(
            {
                "truncated": True,
                "snapshot_excerpt": serialized[:excerpt_limit],
                "guidance": "Use the allowed task/output tools for omitted details.",
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )
        rendered = f"{prefix}{envelope}{suffix}"
        if len(rendered) <= _MAX_TASK_CONTROL_CONTEXT_CHARS:
            return rendered
        excerpt_limit = max(0, excerpt_limit - (len(rendered) - _MAX_TASK_CONTROL_CONTEXT_CHARS))


async def build_task_control_turn_context(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    task_id: str,
    conversation_id: str,
) -> str:
    """Build a compact, fresh task snapshot for one control-chat turn."""

    async with session_factory() as session:
        task = await get_task(session, task_id)
        if task is None:
            raise ValueError("Task control task no longer exists")
        if task.control_conversation_id != conversation_id:
            raise PermissionError("Conversation no longer controls this task")
        step_rows = await list_step_runs_for_task_projection(session, task_id)
        comments = await list_task_comments(session, task_id)
        progress = await build_task_progress_projection(
            session,
            owner_email=task.created_by,
            step_runs=step_rows,
        )

    state = dict(task.workflow_state or {})
    recent_comments = comments[-8:]
    latest_steps: dict[str, Any] = {}
    for row in step_rows:
        if row.status == "superseded" or row.superseded_by_step_run_id is not None:
            continue
        previous = latest_steps.get(row.step_name)
        ordering_time = row.started_at or row.updated_at
        if previous is None or (row.attempt_number, row.attempt, ordering_time) >= (
            previous["attempt_number"],
            previous["attempt"],
            previous["ordering_time"],
        ):
            latest_steps[row.step_name] = {
                "step_run_id": row.step_run_id,
                "step_name": row.step_name,
                "status": row.status,
                "attempt": row.attempt,
                "attempt_number": row.attempt_number,
                "conversation_id": row.conversation_id,
                "session_id": row.session_id,
                "deliverable_id": row.deliverable_id,
                "started_at": row.started_at,
                "ordering_time": ordering_time,
            }
    for step in latest_steps.values():
        step.pop("ordering_time", None)
    payload = {
        "task": {
            "task_id": task.task_id,
            "title": task.title,
            "description": task.description,
            "expected_output": task.expected_output,
            "status": task.status,
            "attempt_number": task.attempt_number,
            "agent_id": task.agent_id,
            "agent_profile_id": task.agent_profile_id,
            "workflow_id": task.workflow_id,
            "project_id": task.project_id,
            "priority": task.priority,
        },
        "workflow": {
            "status": state.get("status"),
            "current_step_index": state.get("current_step_index"),
            "current_step_status": state.get("current_step_status"),
            "pending_pause_type": state.get("pending_pause_type"),
            "pending_pause_payload": state.get("pending_pause_payload"),
            "last_operator_instruction": state.get("last_operator_instruction"),
            "last_revision_context": state.get("last_revision_context"),
        },
        "latest_steps": list(latest_steps.values()),
        "progress": (
            progress.model_dump(mode="json") if hasattr(progress, "model_dump") else progress
        ),
        "recent_comments": [
            {
                "comment_id": row.comment_id,
                "intent": row.intent,
                "body": row.body,
                "target_step": row.target_step,
                "attempt_number": row.attempt_number,
                "applied": row.applied,
                "created_at": row.created_at,
            }
            for row in recent_comments
        ],
        "result": {
            "summary": task.result_summary,
            "data": task.result_data,
        },
        "links": {
            "cockpit": f"/tasks/{task_id}",
            "control_chat": f"/chat/{conversation_id}",
            "work_view": f"/chat/{conversation_id}?view=work",
        },
    }
    return _render_task_control_context(payload)
