"""Reusable async query helpers for Cognis DB."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

import sqlalchemy as sa
from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from cognis.store.models import (
    Agent,
    ApiKey,
    Conversation,
    LLMProvider,
    Schedule,
    Secret,
    Session,
    Setting,
    StepRun,
    Task,
    TaskDependency,
    User,
    WorkflowRow,
)


def _utcnow() -> datetime:
    return datetime.now(UTC)


# --- Users ---


async def get_user(session: AsyncSession, email: str) -> User | None:
    """Get a user by email."""
    result = await session.execute(select(User).where(User.email == email))
    return result.scalar_one_or_none()


async def create_user(
    session: AsyncSession,
    email: str,
    name: str | None,
    password_hash: str,
    role: str = "user",
) -> User:
    """Create a new user."""
    user = User(email=email, name=name, password_hash=password_hash, role=role)
    session.add(user)
    await session.flush()
    return user


async def count_users(session: AsyncSession) -> int:
    """Count total users."""
    result = await session.execute(select(User))
    return len(result.scalars().all())


async def update_user_password(session: AsyncSession, email: str, password_hash: str) -> bool:
    """Update a user's password hash. Returns True if user found."""
    user = await get_user(session, email)
    if user is None:
        return False
    user.password_hash = password_hash
    await session.flush()
    return True


# --- API Keys ---


async def get_api_key(session: AsyncSession, key_id: str) -> ApiKey | None:
    """Get an API key by key_id."""
    result = await session.execute(select(ApiKey).where(ApiKey.key_id == key_id))
    return result.scalar_one_or_none()


async def create_api_key(
    session: AsyncSession,
    user_email: str,
    key_hash: str,
    name: str,
    scopes: dict[str, object] | None = None,
    key_id: str | None = None,
) -> ApiKey:
    """Create a new API key record."""
    key_id = key_id or f"ck{uuid.uuid4().hex[:16]}"
    api_key = ApiKey(
        key_id=key_id,
        user_email=user_email,
        key_hash=key_hash,
        name=name,
        scopes=scopes,
    )
    session.add(api_key)
    await session.flush()
    return api_key


async def list_api_keys(session: AsyncSession, user_email: str) -> list[ApiKey]:
    """List API keys for a user (metadata only)."""
    result = await session.execute(select(ApiKey).where(ApiKey.user_email == user_email))
    return list(result.scalars().all())


# --- Settings ---


async def get_setting(session: AsyncSession, key: str) -> Setting | None:
    """Get a setting by key."""
    result = await session.execute(select(Setting).where(Setting.key == key))
    return result.scalar_one_or_none()


async def get_setting_value(session: AsyncSession, key: str, default: object = None) -> object:
    """Get a setting's value, returning default if not found."""
    setting = await get_setting(session, key)
    if setting is None:
        return default
    return setting.value


async def upsert_setting(
    session: AsyncSession,
    key: str,
    value: object,
    category: str,
    updated_by: str | None = None,
) -> Setting:
    """Create or update a setting."""
    existing = await get_setting(session, key)
    if existing is not None:
        existing.value = value
        existing.updated_by = updated_by
        existing.updated_at = datetime.now(UTC)
        await session.flush()
        return existing
    setting = Setting(key=key, value=value, category=category, updated_by=updated_by)
    session.add(setting)
    await session.flush()
    return setting


async def list_settings(session: AsyncSession) -> list[Setting]:
    """List all settings."""
    result = await session.execute(select(Setting))
    return list(result.scalars().all())


# --- LLM Providers ---


async def list_llm_providers(session: AsyncSession) -> list[LLMProvider]:
    """List all LLM provider configurations."""
    result = await session.execute(select(LLMProvider).where(LLMProvider.status == "active"))
    return list(result.scalars().all())


# --- Agents ---


async def list_active_agents_summary(
    session: AsyncSession, owner_email: str
) -> list[dict[str, str | None]]:
    """List safe agent metadata for tool responses."""

    query = (
        select(Agent.agent_id, Agent.name, Agent.description, Agent.status)
        .where(Agent.status == "active")
        .where(Agent.owner_email == owner_email)
        .order_by(Agent.agent_id)
    )
    result = await session.execute(query)
    return [
        {
            "agent_id": agent_id,
            "name": name,
            "description": description,
            "status": status,
        }
        for agent_id, name, description, status in result.all()
    ]


async def get_agent(session: AsyncSession, agent_id: str) -> Agent | None:
    """Get an agent by ID."""

    result = await session.execute(select(Agent).where(Agent.agent_id == agent_id))
    return result.scalar_one_or_none()


async def get_llm_provider(session: AsyncSession, provider_id: str) -> LLMProvider | None:
    """Get an LLM provider by ID."""
    result = await session.execute(
        select(LLMProvider).where(LLMProvider.provider_id == provider_id)
    )
    return result.scalar_one_or_none()


# --- Conversations ---


async def create_conversation(
    session: AsyncSession,
    user_email: str,
    agent_id: str,
    context_type: str,
    *,
    title: str | None = None,
    context_ref: str | None = None,
    context_data: dict[str, object] | None = None,
    memory_labels: dict[str, object] | None = None,
    conversation_id: str | None = None,
) -> Conversation:
    """Create a new conversation row."""

    conversation = Conversation(
        conversation_id=conversation_id or f"conv_{uuid.uuid4().hex}",
        user_email=user_email,
        agent_id=agent_id,
        title=title,
        context_type=context_type,
        context_ref=context_ref,
        context_data=context_data,
        memory_labels=memory_labels,
    )
    session.add(conversation)
    await session.flush()
    return conversation


async def get_conversation(session: AsyncSession, conversation_id: str) -> Conversation | None:
    """Get a conversation by ID."""

    result = await session.execute(
        select(Conversation).where(Conversation.conversation_id == conversation_id)
    )
    return result.scalar_one_or_none()


async def update_conversation_root_session(
    session: AsyncSession, conversation_id: str, root_session_id: str
) -> bool:
    """Set the root session ID for a conversation."""

    conversation = await get_conversation(session, conversation_id)
    if conversation is None:
        return False
    conversation.root_session_id = root_session_id
    conversation.updated_at = datetime.now(UTC)
    await session.flush()
    return True


async def touch_conversation(
    session: AsyncSession, conversation_id: str, when: datetime | None = None
) -> bool:
    """Update conversation timestamps after activity."""

    conversation = await get_conversation(session, conversation_id)
    if conversation is None:
        return False
    timestamp = when or datetime.now(UTC)
    conversation.last_message_at = timestamp
    conversation.updated_at = timestamp
    await session.flush()
    return True


async def set_conversation_status(session: AsyncSession, conversation_id: str, status: str) -> bool:
    """Set conversation lifecycle status."""

    conversation = await get_conversation(session, conversation_id)
    if conversation is None:
        return False
    conversation.status = status
    conversation.updated_at = datetime.now(UTC)
    await session.flush()
    return True


async def list_conversation_sessions(session: AsyncSession, conversation_id: str) -> list[Session]:
    """List all sessions for a conversation."""

    result = await session.execute(
        select(Session)
        .where(Session.conversation_id == conversation_id)
        .order_by(Session.started_at, Session.session_id)
    )
    return list(result.scalars().all())


async def delete_conversation(session: AsyncSession, conversation_id: str) -> int:
    """Hard-delete a conversation row."""

    result = await session.execute(
        delete(Conversation).where(Conversation.conversation_id == conversation_id)
    )
    await session.flush()
    return int(getattr(result, "rowcount", 0) or 0)


# --- Sessions ---


async def create_session(
    session: AsyncSession,
    conversation_id: str,
    user_email: str,
    agent_id: str,
    *,
    parent_session_id: str | None = None,
    delegation_mode: str | None = None,
    delegation_task: str | None = None,
    status: str = "active",
    intaris_session_id: str | None = None,
    mnemory_session_id: str | None = None,
    session_id: str | None = None,
) -> Session:
    """Create a new session row."""

    session_row = Session(
        session_id=session_id or f"sess_{uuid.uuid4().hex}",
        conversation_id=conversation_id,
        parent_session_id=parent_session_id,
        user_email=user_email,
        agent_id=agent_id,
        delegation_mode=delegation_mode,
        delegation_task=delegation_task,
        status=status,
        intaris_session_id=intaris_session_id,
        mnemory_session_id=mnemory_session_id,
    )
    session.add(session_row)
    await session.flush()
    return session_row


async def get_session_row(session: AsyncSession, session_id: str) -> Session | None:
    """Get a session row by ID."""

    result = await session.execute(select(Session).where(Session.session_id == session_id))
    return result.scalar_one_or_none()


async def set_session_intaris_session_id(
    session: AsyncSession, session_id: str, intaris_session_id: str
) -> bool:
    """Persist Intaris correlation ID for a session."""

    session_row = await get_session_row(session, session_id)
    if session_row is None:
        return False
    session_row.intaris_session_id = intaris_session_id
    session_row.updated_at = datetime.now(UTC)
    await session.flush()
    return True


async def set_session_mnemory_session_id(
    session: AsyncSession, session_id: str, mnemory_session_id: str
) -> bool:
    """Persist Mnemory correlation ID for a session."""

    session_row = await get_session_row(session, session_id)
    if session_row is None:
        return False
    session_row.mnemory_session_id = mnemory_session_id
    session_row.updated_at = datetime.now(UTC)
    await session.flush()
    return True


async def set_session_status(
    session: AsyncSession,
    session_id: str,
    status: str,
    *,
    idle_since: datetime | None = None,
    completed_at: datetime | None = None,
    result_summary: str | None = None,
) -> bool:
    """Update session lifecycle state and timestamps."""

    session_row = await get_session_row(session, session_id)
    if session_row is None:
        return False
    session_row.status = status
    session_row.idle_since = idle_since
    session_row.completed_at = completed_at
    if result_summary is not None:
        session_row.result_summary = result_summary
    session_row.updated_at = datetime.now(UTC)
    await session.flush()
    return True


async def set_session_idle(
    session: AsyncSession, session_id: str, idle_since: datetime | None = None
) -> bool:
    """Mark a session idle."""

    return await set_session_status(
        session,
        session_id,
        "idle",
        idle_since=idle_since or datetime.now(UTC),
    )


async def list_child_sessions(session: AsyncSession, parent_session_id: str) -> list[Session]:
    """List all direct child sessions for a parent session."""

    result = await session.execute(
        select(Session)
        .where(Session.parent_session_id == parent_session_id)
        .order_by(Session.started_at, Session.session_id)
    )
    return list(result.scalars().all())


async def list_stale_active_sessions(
    session: AsyncSession, updated_before: datetime
) -> list[Session]:
    """List active sessions that have not been updated recently."""

    result = await session.execute(
        select(Session)
        .where(Session.status == "active")
        .where(Session.updated_at < updated_before)
        .order_by(Session.updated_at, Session.session_id)
    )
    return list(result.scalars().all())


async def delete_sessions_for_conversation(session: AsyncSession, conversation_id: str) -> int:
    """Hard-delete all sessions for a conversation."""

    result = await session.execute(
        delete(Session).where(Session.conversation_id == conversation_id)
    )
    await session.flush()
    return int(getattr(result, "rowcount", 0) or 0)


# --- Secrets ---


async def get_secret(
    session: AsyncSession,
    user_email: str,
    name: str,
    scope: str = "user",
    agent_id: str | None = None,
) -> Secret | None:
    """Get a secret by user, name, scope, and optional agent_id."""
    query = select(Secret).where(
        Secret.user_email == user_email,
        Secret.name == name,
        Secret.scope == scope,
    )
    if agent_id is not None:
        query = query.where(Secret.agent_id == agent_id)
    else:
        query = query.where(Secret.agent_id.is_(None))
    result = await session.execute(query)
    return result.scalar_one_or_none()


async def list_secrets(session: AsyncSession, user_email: str) -> list[Secret]:
    """List secrets for a user (metadata only, no decrypted values)."""
    result = await session.execute(select(Secret).where(Secret.user_email == user_email))
    return list(result.scalars().all())


# ---------------------------------------------------------------------------
# Tasks
# ---------------------------------------------------------------------------


async def create_task(
    session: AsyncSession,
    *,
    created_by: str,
    agent_id: str,
    title: str,
    description: str = "",
    status: str = "draft",
    priority: int = 0,
    source_type: str = "api",
    source_ref: str | None = None,
    delivery_mode: str = "same_conversation",
    delivery_target: str | None = None,
    workflow_id: str | None = None,
    workflow_state: dict[str, object] | None = None,
    queue_name: str = "default",
    scheduled_for: datetime | None = None,
    task_id: str | None = None,
) -> Task:
    """Create a new task."""
    row = Task(
        task_id=task_id or f"task_{uuid.uuid4().hex}",
        title=title,
        description=description,
        status=status,
        priority=priority,
        created_by=created_by,
        agent_id=agent_id,
        source_type=source_type,
        source_ref=source_ref,
        delivery_mode=delivery_mode,
        delivery_target=delivery_target,
        workflow_id=workflow_id,
        workflow_state=workflow_state,
        queue_name=queue_name,
        scheduled_for=scheduled_for,
    )
    session.add(row)
    await session.flush()
    return row


async def get_task(session: AsyncSession, task_id: str) -> Task | None:
    """Get a task by ID."""
    result = await session.execute(select(Task).where(Task.task_id == task_id))
    return result.scalar_one_or_none()


async def update_task_status(
    session: AsyncSession,
    task_id: str,
    status: str,
    *,
    started_at: datetime | None = None,
    completed_at: datetime | None = None,
    result_summary: str | None = None,
    result_data: dict[str, object] | None = None,
) -> bool:
    """Update task status and optional lifecycle fields.

    Uses compare-and-swap: only updates if current status allows transition.
    """
    valid_transitions: dict[str, set[str]] = {
        "draft": {"queued", "cancelled"},
        "queued": {"ready", "cancelled"},
        "ready": {"running", "cancelled"},
        "running": {"paused", "completed", "failed", "cancelled"},
        "paused": {"running", "cancelled"},
    }
    allowed_from = [k for k, v in valid_transitions.items() if status in v]
    if not allowed_from:
        return False

    values: dict[str, object] = {"status": status}
    if started_at is not None:
        values["started_at"] = started_at
    if completed_at is not None:
        values["completed_at"] = completed_at
    if result_summary is not None:
        values["result_summary"] = result_summary
    if result_data is not None:
        values["result_data"] = result_data

    stmt = (
        update(Task).where(Task.task_id == task_id, Task.status.in_(allowed_from)).values(**values)
    )
    result = await session.execute(stmt)
    return int(getattr(result, "rowcount", 0) or 0) > 0


async def update_task_workflow_state(
    session: AsyncSession,
    task_id: str,
    workflow_state: dict[str, object],
) -> bool:
    """Persist workflow state after a step transition."""
    stmt = update(Task).where(Task.task_id == task_id).values(workflow_state=workflow_state)
    result = await session.execute(stmt)
    return int(getattr(result, "rowcount", 0) or 0) > 0


async def list_tasks_by_status(
    session: AsyncSession,
    statuses: list[str],
    *,
    agent_id: str | None = None,
    queue_name: str | None = None,
    limit: int = 100,
) -> list[Task]:
    """List tasks matching the given statuses, ordered by priority DESC, created_at ASC."""
    query = (
        select(Task)
        .where(Task.status.in_(statuses))
        .order_by(Task.priority.desc(), Task.created_at.asc())
        .limit(limit)
    )
    if agent_id is not None:
        query = query.where(Task.agent_id == agent_id)
    if queue_name is not None:
        query = query.where(Task.queue_name == queue_name)
    result = await session.execute(query)
    return list(result.scalars().all())


async def pick_ready_task(
    session: AsyncSession,
    *,
    queue_name: str = "default",
) -> Task | None:
    """Atomically pick the highest priority ready task using compare-and-swap.

    Returns the task if successfully transitioned to 'running', None otherwise.
    """
    now = _utcnow()

    # Find the best candidate
    query = (
        select(Task.task_id)
        .where(
            Task.status == "ready",
            Task.queue_name == queue_name,
            sa.or_(Task.scheduled_for.is_(None), Task.scheduled_for <= now),
        )
        .order_by(Task.priority.desc(), Task.created_at.asc())
        .limit(1)
    )
    result = await session.execute(query)
    candidate_id = result.scalar_one_or_none()
    if candidate_id is None:
        return None

    # CAS: only transition if still ready
    stmt = (
        update(Task)
        .where(Task.task_id == candidate_id, Task.status == "ready")
        .values(status="running", started_at=now)
    )
    cas_result = await session.execute(stmt)
    if int(getattr(cas_result, "rowcount", 0) or 0) == 0:
        return None  # Another coroutine picked it

    # Fetch the updated row
    fetch_result = await session.execute(select(Task).where(Task.task_id == candidate_id))
    return fetch_result.scalar_one_or_none()


async def count_active_steps(
    session: AsyncSession,
    *,
    agent_id: str | None = None,
) -> int:
    """Count running step_runs, optionally filtered by agent."""
    query = select(sa.func.count()).select_from(StepRun).where(StepRun.status == "running")
    if agent_id is not None:
        query = query.where(StepRun.agent_id == agent_id)
    result = await session.execute(query)
    return result.scalar_one()


async def list_stale_running_tasks(
    session: AsyncSession,
    updated_before: datetime,
) -> list[Task]:
    """Find tasks stuck in running state for recovery."""
    result = await session.execute(
        select(Task).where(Task.status == "running", Task.updated_at < updated_before)
    )
    return list(result.scalars().all())


# ---------------------------------------------------------------------------
# Task Dependencies
# ---------------------------------------------------------------------------


async def add_task_dependency(
    session: AsyncSession,
    task_id: str,
    depends_on: str,
    *,
    required: bool = True,
) -> TaskDependency:
    """Add a dependency edge. Raises ValueError on cycle detection."""
    if task_id == depends_on:
        raise ValueError("Task cannot depend on itself")

    # DFS cycle detection: check if depends_on can reach task_id
    if await _would_create_cycle(session, task_id=depends_on, target=task_id):
        raise ValueError(f"Adding dependency {task_id} -> {depends_on} would create a cycle")

    row = TaskDependency(task_id=task_id, depends_on=depends_on, required=required)
    session.add(row)
    await session.flush()
    return row


async def _would_create_cycle(
    session: AsyncSession,
    *,
    task_id: str,
    target: str,
) -> bool:
    """DFS check: can task_id reach target through existing dependencies?"""
    visited: set[str] = set()
    stack = [task_id]
    while stack:
        current = stack.pop()
        if current == target:
            return True
        if current in visited:
            continue
        visited.add(current)
        result = await session.execute(
            select(TaskDependency.depends_on).where(TaskDependency.task_id == current)
        )
        for (dep_id,) in result:
            stack.append(dep_id)
    return False


async def remove_task_dependency(
    session: AsyncSession,
    task_id: str,
    depends_on: str,
) -> bool:
    """Remove a dependency edge."""
    stmt = delete(TaskDependency).where(
        TaskDependency.task_id == task_id,
        TaskDependency.depends_on == depends_on,
    )
    result = await session.execute(stmt)
    return int(getattr(result, "rowcount", 0) or 0) > 0


async def get_task_dependencies(
    session: AsyncSession,
    task_id: str,
) -> list[TaskDependency]:
    """Get all dependencies for a task."""
    result = await session.execute(select(TaskDependency).where(TaskDependency.task_id == task_id))
    return list(result.scalars().all())


async def get_unmet_dependencies(
    session: AsyncSession,
    task_id: str,
    *,
    required_only: bool = True,
) -> list[TaskDependency]:
    """Get dependencies where the depended-on task is NOT completed.

    When required_only=True (default), only required dependencies block
    the task from becoming ready. Optional dependencies are ignored.
    """
    query = (
        select(TaskDependency)
        .join(Task, Task.task_id == TaskDependency.depends_on)
        .where(TaskDependency.task_id == task_id, Task.status != "completed")
    )
    if required_only:
        query = query.where(TaskDependency.required.is_(True))
    result = await session.execute(query)
    return list(result.scalars().all())


async def get_dependent_tasks(
    session: AsyncSession,
    completed_task_id: str,
) -> list[str]:
    """Get task IDs that depend on the given task."""
    result = await session.execute(
        select(TaskDependency.task_id).where(TaskDependency.depends_on == completed_task_id)
    )
    return [row[0] for row in result]


# ---------------------------------------------------------------------------
# Step Runs
# ---------------------------------------------------------------------------


async def create_step_run(
    session: AsyncSession,
    *,
    task_id: str,
    step_name: str,
    step_type: str,
    agent_id: str,
    attempt: int = 1,
    step_run_id: str | None = None,
) -> StepRun:
    """Create a new step run record."""
    row = StepRun(
        step_run_id=step_run_id or f"sr_{uuid.uuid4().hex}",
        task_id=task_id,
        step_name=step_name,
        step_type=step_type,
        agent_id=agent_id,
        attempt=attempt,
    )
    session.add(row)
    await session.flush()
    return row


async def update_step_run(
    session: AsyncSession,
    step_run_id: str,
    *,
    status: str | None = None,
    session_id: str | None = None,
    intaris_session_id: str | None = None,
    output: dict[str, object] | None = None,
    evaluation: dict[str, object] | None = None,
    todos: dict[str, object] | None = None,
    started_at: datetime | None = None,
    completed_at: datetime | None = None,
) -> bool:
    """Update step run fields."""
    values: dict[str, object] = {}
    if status is not None:
        values["status"] = status
    if session_id is not None:
        values["session_id"] = session_id
    if intaris_session_id is not None:
        values["intaris_session_id"] = intaris_session_id
    if output is not None:
        values["output"] = output
    if evaluation is not None:
        values["evaluation"] = evaluation
    if todos is not None:
        values["todos"] = todos
    if started_at is not None:
        values["started_at"] = started_at
    if completed_at is not None:
        values["completed_at"] = completed_at
    if not values:
        return False
    stmt = update(StepRun).where(StepRun.step_run_id == step_run_id).values(**values)
    result = await session.execute(stmt)
    return int(getattr(result, "rowcount", 0) or 0) > 0


async def list_step_runs_for_task(
    session: AsyncSession,
    task_id: str,
) -> list[StepRun]:
    """List all step runs for a task, ordered by started_at."""
    result = await session.execute(
        select(StepRun)
        .where(StepRun.task_id == task_id)
        .order_by(StepRun.started_at.asc(), StepRun.step_run_id.asc())
    )
    return list(result.scalars().all())


async def fail_running_step_runs_for_task(
    session: AsyncSession,
    task_id: str,
    completed_at: datetime,
) -> int:
    """Fail all running step runs for a task (used during recovery)."""
    stmt = (
        update(StepRun)
        .where(StepRun.task_id == task_id, StepRun.status == "running")
        .values(status="failed", completed_at=completed_at)
    )
    result = await session.execute(stmt)
    return int(getattr(result, "rowcount", 0) or 0)


# ---------------------------------------------------------------------------
# Workflows
# ---------------------------------------------------------------------------


async def create_workflow(
    session: AsyncSession,
    *,
    workflow_id: str,
    name: str,
    definition: dict[str, object],
    description: str = "",
    version: int = 1,
    is_system: bool = False,
    owner_email: str | None = None,
) -> WorkflowRow:
    """Create a workflow record."""
    row = WorkflowRow(
        workflow_id=workflow_id,
        name=name,
        description=description,
        version=version,
        definition=definition,
        is_system=is_system,
        owner_email=owner_email,
    )
    session.add(row)
    await session.flush()
    return row


async def get_workflow(session: AsyncSession, workflow_id: str) -> WorkflowRow | None:
    """Get a workflow by ID."""
    result = await session.execute(
        select(WorkflowRow).where(WorkflowRow.workflow_id == workflow_id)
    )
    return result.scalar_one_or_none()


async def list_workflows(
    session: AsyncSession,
    *,
    owner_email: str | None = None,
    include_system: bool = True,
) -> list[WorkflowRow]:
    """List workflows, optionally filtered by owner."""
    query = select(WorkflowRow).order_by(WorkflowRow.name.asc())
    conditions: list[Any] = []
    if include_system:
        conditions.append(WorkflowRow.is_system.is_(True))
    if owner_email is not None:
        conditions.append(WorkflowRow.owner_email == owner_email)
    if conditions:
        query = query.where(sa.or_(*conditions))
    result = await session.execute(query)
    return list(result.scalars().all())


# ---------------------------------------------------------------------------
# Schedules
# ---------------------------------------------------------------------------


async def create_schedule(
    session: AsyncSession,
    *,
    schedule_id: str | None = None,
    name: str,
    cron_expr: str,
    agent_id: str,
    task_template: dict[str, object],
    created_by: str,
    workflow_id: str | None = None,
    enabled: bool = True,
) -> Schedule:
    """Create a schedule record."""
    row = Schedule(
        schedule_id=schedule_id or f"sched_{uuid.uuid4().hex}",
        name=name,
        cron_expr=cron_expr,
        agent_id=agent_id,
        workflow_id=workflow_id,
        task_template=task_template,
        enabled=enabled,
        created_by=created_by,
    )
    session.add(row)
    await session.flush()
    return row


async def get_schedule(session: AsyncSession, schedule_id: str) -> Schedule | None:
    """Get a schedule by ID."""
    result = await session.execute(select(Schedule).where(Schedule.schedule_id == schedule_id))
    return result.scalar_one_or_none()
