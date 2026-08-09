"""Authoritative chat-facing state snapshots for conversations."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from cognis.models.conversation_state import (
    ConversationActiveSessionState,
    ConversationActiveTurnState,
    ConversationKind,
    ConversationPendingState,
    ConversationPendingSummary,
    ConversationStateDelta,
    ConversationStateDeltaSource,
    ConversationStateEnvelope,
    ConversationStateOffsets,
    ConversationStepState,
    ConversationTaskState,
    ConversationTodoItem,
)
from cognis.store.models import Conversation, NotificationRow, Session, StepRun, Task
from cognis.store.queries import (
    get_conversation,
    get_session_row,
    get_step_run,
    get_task,
    list_conversation_todos,
)


@dataclass(frozen=True)
class LinkedConversationContext:
    conversation_kind: ConversationKind
    task: Task
    step_run: StepRun | None = None


def _iso_revision(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _normalize_todos(raw: Any) -> list[ConversationTodoItem]:
    if not isinstance(raw, list):
        return []
    todos: list[ConversationTodoItem] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        content = item.get("content")
        if not isinstance(content, str) or not content.strip():
            continue
        status = item.get("status")
        priority = item.get("priority")
        todos.append(
            ConversationTodoItem(
                content=content,
                status=status if isinstance(status, str) and status else "pending",
                priority=priority if isinstance(priority, str) and priority else None,
            )
        )
    return todos


def _step_state(row: StepRun | None) -> ConversationStepState | None:
    if row is None:
        return None
    return ConversationStepState(
        step_run_id=row.step_run_id,
        step_name=row.step_name,
        status=row.status,
        conversation_id=row.conversation_id,
        session_id=row.session_id,
        started_at=row.started_at,
        completed_at=row.completed_at,
        todos=_normalize_todos(row.todos),
    )


def _conversation_kind_for_unlinked(row: Conversation) -> ConversationKind:
    return (
        "external_channel" if (row.context_type or "").lower() not in {"web", "api"} else "normal"
    )


async def resolve_linked_task_context(
    session: AsyncSession,
    conversation: Conversation,
    user_email: str,
) -> LinkedConversationContext | None:
    """Resolve backend-owned task linkage, failing closed on invalid refs."""

    result = await session.execute(
        select(StepRun, Task)
        .join(Task, Task.task_id == StepRun.task_id)
        .where(StepRun.conversation_id == conversation.conversation_id)
        .where(Task.created_by == user_email)
        .order_by(StepRun.started_at.desc().nullslast(), StepRun.updated_at.desc())
    )
    actual = result.first()
    if actual is not None:
        actual_step_run, task = actual
        return LinkedConversationContext("task_step", task, actual_step_run)

    task_id: str | None = None
    step_run_id: str | None = None
    kind: ConversationKind = "task"
    context_data = conversation.context_data if isinstance(conversation.context_data, dict) else {}
    forked_from = context_data.get("forked_from")
    if forked_from == "task":
        raw_task_id = context_data.get("task_id")
        raw_step_run_id = context_data.get("source_step_run_id")
        task_id = raw_task_id if isinstance(raw_task_id, str) and raw_task_id else None
        step_run_id = (
            raw_step_run_id if isinstance(raw_step_run_id, str) and raw_step_run_id else None
        )
        kind = "task"
    elif forked_from == "task_step":
        raw_task_id = context_data.get("task_id")
        raw_step_run_id = context_data.get("step_run_id")
        task_id = raw_task_id if isinstance(raw_task_id, str) and raw_task_id else None
        step_run_id = (
            raw_step_run_id if isinstance(raw_step_run_id, str) and raw_step_run_id else None
        )
        kind = "task_step"
    elif conversation.context_type == "task":
        task_id = conversation.context_ref if isinstance(conversation.context_ref, str) else None
        kind = "task"

    if not task_id:
        return None
    task = await get_task(session, task_id)
    if task is None or task.created_by != user_email:
        return None

    step_run: StepRun | None = None
    if step_run_id:
        step_run = await get_step_run(session, step_run_id)
        if step_run is None or step_run.task_id != task.task_id:
            return None
    return LinkedConversationContext(kind, task, step_run)


async def _current_step_for_task(session: AsyncSession, task_id: str) -> StepRun | None:
    result = await session.execute(
        select(StepRun)
        .where(StepRun.task_id == task_id)
        .order_by(
            StepRun.started_at.desc().nullslast(),
            StepRun.updated_at.desc(),
            StepRun.step_run_id.desc(),
        )
    )
    return result.scalars().first()


def _pending_summary(row: NotificationRow) -> ConversationPendingSummary:
    payload = row.payload if isinstance(row.payload, dict) else {}
    metadata = payload.get("metadata") if isinstance(payload.get("metadata"), dict) else {}
    options = payload.get("options") if isinstance(payload.get("options"), list) else []
    return ConversationPendingSummary(
        notification_id=row.notification_id,
        notification_type=row.notification_type,
        task_id=row.task_id,
        step_name=row.step_name,
        step_run_id=row.step_run_id,
        question=payload.get("question") if isinstance(payload.get("question"), str) else None,
        label=payload.get("label") if isinstance(payload.get("label"), str) else None,
        message=payload.get("message") if isinstance(payload.get("message"), str) else None,
        options=options,
        metadata=metadata,
        created_at=row.created_at,
    )


async def _pending_state(
    session: AsyncSession,
    *,
    user_email: str,
    conversation_id: str,
    task_id: str | None,
) -> ConversationPendingState:
    stmt = (
        select(NotificationRow)
        .where(NotificationRow.user_email == user_email)
        .where(NotificationRow.status == "pending")
        .order_by(NotificationRow.created_at.asc(), NotificationRow.notification_id.asc())
    )
    stmt = stmt.where(
        or_(NotificationRow.conversation_id == conversation_id, NotificationRow.task_id == task_id)
        if task_id
        else NotificationRow.conversation_id == conversation_id
    )
    result = await session.execute(stmt)
    pending = ConversationPendingState()
    seen_types: set[str] = set()
    for row in result.scalars().all():
        if row.notification_type not in seen_types:
            pending.notification_types.append(row.notification_type)
            seen_types.add(row.notification_type)
        summary = _pending_summary(row)
        if row.notification_type in {"step_question", "gate"} and pending.pending_input is None:
            pending.pending_input = summary
        elif row.notification_type == "credential_request" and pending.credential_request is None:
            pending.credential_request = summary
        elif row.notification_type == "auth_challenge" and pending.auth_challenge is None:
            pending.auth_challenge = summary
        elif row.notification_type == "escalation" and pending.escalation is None:
            pending.escalation = summary
    return pending


async def snapshot_for_conversation(
    session: AsyncSession,
    *,
    user_email: str,
    conversation_id: str,
    turn_scheduler: Any | None = None,
    active_session_last_seq: int | None = None,
    conversation: Conversation | None = None,
    conversation_todos: list[dict[str, Any]] | None = None,
) -> ConversationStateEnvelope | None:
    if conversation is None:
        conversation = await get_conversation(session, conversation_id)
    if (
        conversation is None
        or conversation.user_email != user_email
        or conversation.status == "deleted"
        or conversation.conversation_id != conversation_id
    ):
        return None

    linked = await resolve_linked_task_context(session, conversation, user_email)
    active_session: Session | None = (
        await get_session_row(session, conversation.active_session_id)
        if conversation.active_session_id
        else None
    )
    durable_running = (
        getattr(turn_scheduler, "durable_running_turn_state", None)
        if turn_scheduler is not None
        else None
    )
    running_turn_state = (
        await durable_running(conversation_id)
        if callable(durable_running)
        else turn_scheduler.running_turn_state(conversation_id)
        if turn_scheduler is not None and hasattr(turn_scheduler, "running_turn_state")
        else None
    )

    task_state: ConversationTaskState | None = None
    relevant_step = linked.step_run if linked else None
    task_id: str | None = None
    if linked is not None:
        task_id = linked.task.task_id
        current_step = await _current_step_for_task(session, linked.task.task_id)
        relevant_step = relevant_step or current_step
        task_state = ConversationTaskState(
            task_id=linked.task.task_id,
            title=linked.task.title,
            status=linked.task.status,
            current_step=_step_state(current_step),
            relevant_step=_step_state(relevant_step),
        )

    return ConversationStateEnvelope(
        conversation_id=conversation_id,
        conversation_kind=linked.conversation_kind
        if linked
        else _conversation_kind_for_unlinked(conversation),
        linked_task_id=task_id,
        linked_step_run_id=relevant_step.step_run_id if relevant_step is not None else None,
        snapshot_generated_at=datetime.now(UTC),
        capabilities=[
            "conversation_state_snapshot",
            "conversation_state_delta",
            "replace_subtree_deltas",
        ],
        offsets=ConversationStateOffsets(
            active_session_id=conversation.active_session_id,
            active_session_last_seq=active_session_last_seq,
            task_state_revision=_iso_revision(linked.task.updated_at) if linked else None,
            step_state_revision=_iso_revision(relevant_step.updated_at) if relevant_step else None,
        ),
        active_turn=ConversationActiveTurnState(
            has_active_turn=running_turn_state is not None,
            chat_mode=(running_turn_state or {}).get("chat_mode"),
            chat_mode_source=(running_turn_state or {}).get("chat_mode_source"),
        ),
        active_session=ConversationActiveSessionState(
            session_id=getattr(active_session, "session_id", None),
            status=getattr(active_session, "status", None),
            completion_reason=getattr(active_session, "completion_reason", None),
            todos=_normalize_todos(
                conversation_todos
                if conversation_todos is not None
                else await list_conversation_todos(session, conversation.conversation_id)
            ),
        ),
        task=task_state,
        pending=await _pending_state(
            session,
            user_email=user_email,
            conversation_id=conversation_id,
            task_id=task_id,
        ),
    )


async def linked_conversation_ids_for_task(
    session: AsyncSession,
    *,
    user_email: str,
    task_id: str,
    step_run_id: str | None = None,
) -> list[str]:
    task = await get_task(session, task_id)
    if task is None or task.created_by != user_email:
        return []
    ids: set[str] = set()
    step_stmt = select(StepRun.conversation_id).where(StepRun.task_id == task_id)
    if step_run_id is not None:
        step_stmt = step_stmt.where(StepRun.step_run_id == step_run_id)
    step_result = await session.execute(step_stmt)
    ids.update(cid for cid in step_result.scalars().all() if cid)

    task_link_clause = and_(
        Conversation.context_data["forked_from"].as_string() == "task",
        Conversation.context_data["task_id"].as_string() == task_id,
    )
    step_link_clause = and_(
        Conversation.context_data["forked_from"].as_string() == "task_step",
        Conversation.context_data["task_id"].as_string() == task_id,
    )
    if step_run_id is not None:
        step_link_clause = and_(
            step_link_clause,
            Conversation.context_data["step_run_id"].as_string() == step_run_id,
        )

    conv_result = await session.execute(
        select(Conversation.conversation_id)
        .where(Conversation.user_email == user_email)
        .where(Conversation.status != "deleted")
        .where(
            or_(
                and_(Conversation.context_type == "task", Conversation.context_ref == task_id),
                task_link_clause,
                step_link_clause,
            )
        )
    )
    ids.update(cid for cid in conv_result.scalars().all() if cid)
    return sorted(ids)


def build_state_delta(
    *,
    conversation_id: str,
    source_kind: str,
    task_id: str | None = None,
    step_run_id: str | None = None,
    changed_paths: list[str] | None = None,
    replace: dict[str, Any] | None = None,
) -> ConversationStateDelta:
    stable_source = ":".join(part for part in [task_id, step_run_id, source_kind] if part)
    return ConversationStateDelta(
        conversation_id=conversation_id,
        delta_id=f"{stable_source}:{datetime.now(UTC).isoformat()}",
        changed_paths=changed_paths or [],
        replace=replace or {},
        source=ConversationStateDeltaSource(
            kind=source_kind,
            task_id=task_id,
            step_run_id=step_run_id,
        ),
    )
