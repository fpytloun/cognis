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
from cognis.store.models import (
    Conversation,
    ConversationTodo,
    NotificationRow,
    Session,
    StepRun,
    Task,
)
from cognis.store.queries import (
    get_conversation,
    get_step_run,
    get_task,
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
    questions = payload.get("questions") if isinstance(payload.get("questions"), list) else []
    context = payload.get("context") if isinstance(payload.get("context"), dict) else None
    return ConversationPendingSummary(
        notification_id=row.notification_id,
        notification_type=row.notification_type,
        call_id=payload.get("call_id") if isinstance(payload.get("call_id"), str) else None,
        session_id=row.session_id,
        tool_call_id=payload.get("tool_call_id")
        if isinstance(payload.get("tool_call_id"), str)
        else None,
        tool_name=payload.get("tool_name") if isinstance(payload.get("tool_name"), str) else None,
        arguments_display=payload.get("arguments_display"),
        risk=payload.get("risk") if isinstance(payload.get("risk"), str) else None,
        reasoning=payload.get("reasoning") if isinstance(payload.get("reasoning"), str) else None,
        timeout_seconds=payload.get("timeout_seconds")
        if isinstance(payload.get("timeout_seconds"), int)
        else None,
        task_id=row.task_id,
        step_name=row.step_name,
        step_run_id=row.step_run_id,
        question=payload.get("question") if isinstance(payload.get("question"), str) else None,
        label=payload.get("label") if isinstance(payload.get("label"), str) else None,
        message=payload.get("message") if isinstance(payload.get("message"), str) else None,
        options=options,
        questions=questions,
        context=context,
        metadata=metadata,
        managed_conversation_title=payload.get("managed_conversation_title")
        if isinstance(payload.get("managed_conversation_title"), str)
        else None,
        managed_target_agent_id=payload.get("managed_target_agent_id")
        if isinstance(payload.get("managed_target_agent_id"), str)
        else None,
        managed_origin_conversation_id=payload.get("managed_origin_conversation_id")
        if isinstance(payload.get("managed_origin_conversation_id"), str)
        else None,
        created_at=row.created_at,
    )


def _notification_visible_in_conversation(
    row: NotificationRow,
    conversation_id: str,
) -> bool:
    if row.conversation_id == conversation_id:
        return True
    payload = row.payload if isinstance(row.payload, dict) else {}
    return payload.get("managed_origin_conversation_id") == conversation_id


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
        .where(NotificationRow.status.in_(("pending", "resolving")))
        .order_by(NotificationRow.created_at.asc(), NotificationRow.notification_id.asc())
    )
    result = await session.execute(stmt)
    pending = ConversationPendingState()
    seen_types: set[str] = set()
    for row in result.scalars().all():
        if not (
            _notification_visible_in_conversation(row, conversation_id)
            or (task_id is not None and row.task_id == task_id)
        ):
            continue
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


def _fallback_task_reference(
    conversation: Conversation,
) -> tuple[ConversationKind, str, str | None] | None:
    data = conversation.context_data if isinstance(conversation.context_data, dict) else {}
    forked_from = data.get("forked_from")
    if forked_from == "task":
        kind: ConversationKind = "task"
        task_id = data.get("task_id")
        step_run_id = data.get("source_step_run_id")
    elif forked_from == "task_step":
        kind = "task_step"
        task_id = data.get("task_id")
        step_run_id = data.get("step_run_id")
    elif conversation.context_type == "task":
        kind = "task"
        task_id = conversation.context_ref
        step_run_id = None
    else:
        return None
    if not isinstance(task_id, str) or not task_id:
        return None
    return kind, task_id, step_run_id if isinstance(step_run_id, str) and step_run_id else None


def _pending_state_from_rows(rows: list[NotificationRow]) -> ConversationPendingState:
    pending = ConversationPendingState()
    seen_types: set[str] = set()
    for row in rows:
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


def _build_snapshot(
    *,
    conversation: Conversation,
    active_session: Session | None,
    running_turn_state: dict[str, Any] | None,
    linked: LinkedConversationContext | None,
    current_step: StepRun | None,
    conversation_todos: list[dict[str, Any]],
    pending_rows: list[NotificationRow],
) -> ConversationStateEnvelope:
    relevant_step = linked.step_run if linked else None
    task_state: ConversationTaskState | None = None
    task_id: str | None = None
    if linked is not None:
        task_id = linked.task.task_id
        relevant_step = relevant_step or current_step
        task_state = ConversationTaskState(
            task_id=task_id,
            title=linked.task.title,
            status=linked.task.status,
            current_step=_step_state(current_step),
            relevant_step=_step_state(relevant_step),
        )
    return ConversationStateEnvelope(
        conversation_id=conversation.conversation_id,
        conversation_kind=(
            linked.conversation_kind if linked else _conversation_kind_for_unlinked(conversation)
        ),
        linked_task_id=task_id,
        linked_step_run_id=relevant_step.step_run_id if relevant_step else None,
        snapshot_generated_at=datetime.now(UTC),
        capabilities=[
            "conversation_state_snapshot",
            "conversation_state_delta",
            "replace_subtree_deltas",
        ],
        offsets=ConversationStateOffsets(
            active_session_id=conversation.active_session_id,
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
            todos=_normalize_todos(conversation_todos),
        ),
        task=task_state,
        pending=_pending_state_from_rows(pending_rows),
    )


async def snapshots_for_conversations(
    session: AsyncSession,
    *,
    user_email: str,
    conversations: list[Conversation],
    running_turn_states: dict[str, dict[str, Any] | None],
    active_sessions: dict[str, Session] | None = None,
    conversation_todos: dict[str, list[dict[str, Any]]] | None = None,
) -> dict[str, ConversationStateEnvelope]:
    """Build conversation snapshots with a bounded number of database queries."""

    visible = [
        row for row in conversations if row.user_email == user_email and row.status != "deleted"
    ]
    if not visible:
        return {}
    conversation_ids = [row.conversation_id for row in visible]

    linked_by_conversation: dict[str, LinkedConversationContext] = {}
    result = await session.execute(
        select(StepRun, Task)
        .join(Task, Task.task_id == StepRun.task_id)
        .where(StepRun.conversation_id.in_(conversation_ids), Task.created_by == user_email)
        .order_by(
            StepRun.conversation_id,
            StepRun.started_at.desc().nullslast(),
            StepRun.updated_at.desc(),
            StepRun.step_run_id.desc(),
        )
    )
    for step_run, task in result.all():
        linked_by_conversation.setdefault(
            step_run.conversation_id,
            LinkedConversationContext("task_step", task, step_run),
        )

    fallback_refs: dict[str, tuple[ConversationKind, str, str | None]] = {}
    for row in visible:
        if row.conversation_id not in linked_by_conversation:
            reference = _fallback_task_reference(row)
            if reference:
                fallback_refs[row.conversation_id] = reference
    task_ids = sorted({reference[1] for reference in fallback_refs.values()})
    tasks: dict[str, Task] = {}
    if task_ids:
        result = await session.execute(
            select(Task).where(Task.task_id.in_(task_ids), Task.created_by == user_email)
        )
        tasks = {row.task_id: row for row in result.scalars().all()}
    step_ids = sorted({reference[2] for reference in fallback_refs.values() if reference[2]})
    steps: dict[str, StepRun] = {}
    if step_ids:
        result = await session.execute(select(StepRun).where(StepRun.step_run_id.in_(step_ids)))
        steps = {row.step_run_id: row for row in result.scalars().all()}
    for conversation_id, (kind, task_id, step_id) in fallback_refs.items():
        task = tasks.get(task_id)
        step = steps.get(step_id) if step_id else None
        if task is None or (step_id and (step is None or step.task_id != task_id)):
            continue
        linked_by_conversation[conversation_id] = LinkedConversationContext(kind, task, step)

    linked_task_ids = sorted({linked.task.task_id for linked in linked_by_conversation.values()})
    current_steps: dict[str, StepRun] = {}
    if linked_task_ids:
        result = await session.execute(
            select(StepRun)
            .where(StepRun.task_id.in_(linked_task_ids))
            .order_by(
                StepRun.task_id,
                StepRun.started_at.desc().nullslast(),
                StepRun.updated_at.desc(),
                StepRun.step_run_id.desc(),
            )
        )
        for row in result.scalars().all():
            current_steps.setdefault(row.task_id, row)

    result = await session.execute(
        select(NotificationRow)
        .where(
            NotificationRow.user_email == user_email,
            NotificationRow.status == "pending",
        )
        .order_by(NotificationRow.created_at, NotificationRow.notification_id)
    )
    pending_by_conversation: dict[str, list[NotificationRow]] = {}
    pending_by_task: dict[str, list[NotificationRow]] = {}
    for row in result.scalars().all():
        if row.conversation_id in conversation_ids:
            pending_by_conversation.setdefault(row.conversation_id, []).append(row)
        payload = row.payload if isinstance(row.payload, dict) else {}
        origin_conversation_id = payload.get("managed_origin_conversation_id")
        if (
            isinstance(origin_conversation_id, str)
            and origin_conversation_id in conversation_ids
            and origin_conversation_id != row.conversation_id
        ):
            pending_by_conversation.setdefault(origin_conversation_id, []).append(row)
        if row.task_id in linked_task_ids:
            pending_by_task.setdefault(row.task_id, []).append(row)

    if active_sessions is None:
        session_ids = sorted({row.active_session_id for row in visible if row.active_session_id})
        active_sessions = {}
        if session_ids:
            result = await session.execute(
                select(Session).where(Session.session_id.in_(session_ids))
            )
            active_sessions = {row.session_id: row for row in result.scalars().all()}
    if conversation_todos is None:
        conversation_todos = {}
        result = await session.execute(
            select(ConversationTodo)
            .where(ConversationTodo.conversation_id.in_(conversation_ids))
            .order_by(ConversationTodo.conversation_id, ConversationTodo.position)
        )
        for row in result.scalars().all():
            todo: dict[str, Any] = {"content": row.content, "status": row.status}
            if row.priority:
                todo["priority"] = row.priority
            conversation_todos.setdefault(row.conversation_id, []).append(todo)

    snapshots: dict[str, ConversationStateEnvelope] = {}
    for row in visible:
        linked = linked_by_conversation.get(row.conversation_id)
        pending_rows = list(pending_by_conversation.get(row.conversation_id, []))
        if linked:
            seen = {item.notification_id for item in pending_rows}
            pending_rows.extend(
                item
                for item in pending_by_task.get(linked.task.task_id, [])
                if item.notification_id not in seen
            )
            pending_rows.sort(key=lambda item: (item.created_at, item.notification_id))
        snapshots[row.conversation_id] = _build_snapshot(
            conversation=row,
            active_session=(active_sessions or {}).get(row.active_session_id or ""),
            running_turn_state=running_turn_states.get(row.conversation_id),
            linked=linked,
            current_step=current_steps.get(linked.task.task_id) if linked else None,
            conversation_todos=conversation_todos.get(row.conversation_id, []),
            pending_rows=pending_rows,
        )
    return snapshots


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
    durable_running = (
        getattr(turn_scheduler, "durable_running_turn_state", None)
        if turn_scheduler is not None
        else None
    )
    running_turn_state = (
        await durable_running(conversation_id, session=session)
        if callable(durable_running)
        else turn_scheduler.running_turn_state(conversation_id)
        if turn_scheduler is not None and hasattr(turn_scheduler, "running_turn_state")
        else None
    )

    snapshots = await snapshots_for_conversations(
        session,
        user_email=user_email,
        conversations=[conversation],
        running_turn_states={conversation_id: running_turn_state},
        conversation_todos=(
            {conversation_id: conversation_todos} if conversation_todos is not None else None
        ),
    )
    snapshot = snapshots.get(conversation_id)
    if snapshot is not None and active_session_last_seq is not None:
        snapshot.offsets.active_session_last_seq = active_session_last_seq
    return snapshot


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
