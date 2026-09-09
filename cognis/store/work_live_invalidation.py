"""Best-effort Work view invalidation after authoritative SQL mutations."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from weakref import WeakSet

from sqlalchemy import Table, event, select
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session as OrmSession

from cognis.store.models import (
    Agent,
    AgentGrantRow,
    Conversation,
    ConversationTodo,
    DirectTurnRequestRow,
    ManagedConversationLink,
    NotificationRow,
    ProjectRow,
    Session,
    SessionTodo,
    StepRun,
    SystemAgentOverride,
    Task,
    WorkLiveRevisionRow,
)

_WORK_WAKE_PENDING = "_work_live_invalidation_pending"
_WORK_OWNERS_PENDING = "_work_live_invalidation_owners"
_WORK_REVISIONS_COMMITTED = "_work_live_invalidation_revisions"
_wake_callbacks: WeakSet[Callable[[dict[str, int]], None]] = WeakSet()
_WATCHED_TYPES = (
    Agent,
    AgentGrantRow,
    Conversation,
    ConversationTodo,
    Session,
    SessionTodo,
    StepRun,
    Task,
    ManagedConversationLink,
    DirectTurnRequestRow,
    NotificationRow,
    ProjectRow,
    SystemAgentOverride,
)
_WATCHED_TABLES = {item.__tablename__ for item in _WATCHED_TYPES}


def register_live_work_waker(
    callback: Callable[[dict[str, int]], None],
) -> Callable[[], None]:
    """Register one process-local best-effort Work reload notifier."""

    _wake_callbacks.add(callback)

    def unregister() -> None:
        _wake_callbacks.discard(callback)

    return unregister


def _revision_upsert(db: OrmSession, owner_email: str) -> int:
    values = {
        "owner_email": owner_email,
        "revision": 1,
        "updated_at": datetime.now(UTC),
    }
    table = WorkLiveRevisionRow.__table__
    assert isinstance(table, Table)
    insert_statement = (
        postgresql_insert(table)
        if db.get_bind().dialect.name == "postgresql"
        else sqlite_insert(table)
    )
    statement = (
        insert_statement.values(**values)
        .on_conflict_do_update(
            index_elements=[table.c.owner_email],
            set_={
                "revision": table.c.revision + 1,
                "updated_at": values["updated_at"],
            },
        )
        .returning(table.c.revision)
    )
    revision = db.scalar(statement)
    if revision is None:
        raise RuntimeError("Work live revision bump did not return a revision")
    return int(revision)


async def bump_live_work_revision(db: AsyncSession, owner_email: str) -> int:
    """Atomically bump and return one owner's durable Work revision."""

    return await db.run_sync(lambda sync_db: _revision_upsert(sync_db, owner_email))


async def read_live_work_revision(db: AsyncSession, owner_email: str) -> int:
    """Read one owner's durable Work reconciliation watermark."""

    return int(
        await db.scalar(
            select(WorkLiveRevisionRow.revision).where(
                WorkLiveRevisionRow.owner_email == owner_email
            )
        )
        or 0
    )


def _mark(db: OrmSession, owners: set[str] | None = None) -> None:
    db.info[_WORK_WAKE_PENDING] = True
    if owners:
        db.info.setdefault(_WORK_OWNERS_PENDING, set()).update(owners)


@event.listens_for(OrmSession, "before_flush")
def invalidate_live_work_after_flush(
    db: OrmSession,
    _flush_context: object,
    _instances: object,
) -> None:
    watched = [row for row in (*db.new, *db.dirty, *db.deleted) if isinstance(row, _WATCHED_TYPES)]
    if watched:
        owners = {
            owner
            for row in watched
            for owner in (
                getattr(row, "user_email", None),
                getattr(row, "owner_email", None),
                getattr(row, "created_by", None),
                getattr(row, "user_id", None),
                getattr(row, "grantee_user_email", None),
                getattr(row, "granted_by", None),
            )
            if isinstance(owner, str) and owner
        }
        conversation_ids = {
            conversation_id
            for row in watched
            if isinstance(
                conversation_id := getattr(row, "conversation_id", None),
                str,
            )
            and conversation_id
            and not isinstance(row, Conversation)
        }
        if conversation_ids:
            owners.update(
                owner
                for owner in db.connection()
                .execute(
                    select(Conversation.user_email).where(
                        Conversation.conversation_id.in_(conversation_ids)
                    )
                )
                .scalars()
                if isinstance(owner, str) and owner
            )
        session_ids = {
            session_id
            for row in watched
            if isinstance(session_id := getattr(row, "session_id", None), str)
            and session_id
            and not isinstance(row, Session)
        }
        if session_ids:
            owners.update(
                owner
                for owner in db.connection()
                .execute(select(Session.user_email).where(Session.session_id.in_(session_ids)))
                .scalars()
                if isinstance(owner, str) and owner
            )
        agent_ids = {
            agent_id
            for row in watched
            if isinstance(row, (Agent, AgentGrantRow))
            and isinstance(agent_id := getattr(row, "agent_id", None), str)
            and agent_id
        }
        if agent_ids:
            owners.update(
                owner
                for owner in db.connection()
                .execute(
                    select(AgentGrantRow.grantee_user_email).where(
                        AgentGrantRow.agent_id.in_(agent_ids),
                        AgentGrantRow.grantee_user_email.is_not(None),
                    )
                )
                .scalars()
                if isinstance(owner, str) and owner
            )
        _mark(db, owners)


@event.listens_for(OrmSession, "do_orm_execute", retval=True)
def invalidate_live_work_after_bulk(execute_state: object) -> object:
    statement = execute_state.statement  # type: ignore[attr-defined]
    table = getattr(statement, "table", None)
    table_name = getattr(table, "name", None)
    watched_bulk = table_name in _WATCHED_TABLES and (
        getattr(execute_state, "is_update", False)
        or getattr(execute_state, "is_delete", False)
        or getattr(execute_state, "is_insert", False)
    )
    owners: set[str] = set()
    if watched_bulk:
        session = execute_state.session  # type: ignore[attr-defined]
        if table is None:
            return execute_state.invoke_statement()  # type: ignore[attr-defined]
        criteria = tuple(getattr(statement, "_where_criteria", ()))
        owner_column = next(
            (
                table.c[name]
                for name in (
                    "user_email",
                    "owner_email",
                    "created_by",
                    "user_id",
                    "grantee_user_email",
                    "granted_by",
                )
                if name in table.c
            ),
            None,
        )
        if owner_column is not None and criteria:
            owners.update(
                owner
                for owner in session.connection()
                .execute(select(owner_column).where(*criteria))
                .scalars()
                if isinstance(owner, str) and owner
            )
        elif table_name == StepRun.__tablename__ and criteria:
            step_table = StepRun.__table__
            task_table = Task.__table__
            owners.update(
                owner
                for owner in session.connection()
                .execute(
                    select(task_table.c.created_by)
                    .select_from(
                        step_table.join(
                            task_table,
                            step_table.c.task_id == task_table.c.task_id,
                        )
                    )
                    .where(*criteria)
                )
                .scalars()
                if isinstance(owner, str) and owner
            )
        elif "conversation_id" in table.c and criteria:
            conversation_ids = session.connection().execute(
                select(table.c.conversation_id).where(*criteria)
            )
            owners.update(
                owner
                for owner in session.connection()
                .execute(
                    select(Conversation.user_email).where(
                        Conversation.conversation_id.in_(conversation_ids.scalars().all())
                    )
                )
                .scalars()
                if isinstance(owner, str) and owner
            )
        elif "session_id" in table.c and criteria:
            session_ids = session.connection().execute(select(table.c.session_id).where(*criteria))
            owners.update(
                owner
                for owner in session.connection()
                .execute(
                    select(Session.user_email).where(
                        Session.session_id.in_(session_ids.scalars().all())
                    )
                )
                .scalars()
                if isinstance(owner, str) and owner
            )
        statement_values = getattr(statement, "_values", {}) or {}
        for key, bound_value in statement_values.items():
            name = getattr(key, "key", str(key))
            if name not in {
                "user_email",
                "owner_email",
                "created_by",
                "user_id",
                "grantee_user_email",
                "granted_by",
            }:
                continue
            owner = getattr(bound_value, "value", bound_value)
            if isinstance(owner, str) and owner:
                owners.add(owner)
        parameters = getattr(execute_state, "parameters", None)
        parameter_sets = parameters if isinstance(parameters, list) else [parameters]
        for values in parameter_sets:
            if not isinstance(values, dict):
                continue
            for name in (
                "user_email",
                "owner_email",
                "created_by",
                "user_id",
                "grantee_user_email",
                "granted_by",
            ):
                owner = values.get(name)
                if isinstance(owner, str) and owner:
                    owners.add(owner)
    result = execute_state.invoke_statement()  # type: ignore[attr-defined]
    if watched_bulk:
        _mark(execute_state.session, owners)  # type: ignore[attr-defined]
    return result


@event.listens_for(OrmSession, "after_commit")
def publish_live_work_invalidation(db: OrmSession) -> None:
    if not db.info.pop(_WORK_WAKE_PENDING, False):
        return
    revisions = dict(db.info.pop(_WORK_REVISIONS_COMMITTED, {}))
    db.info.pop(_WORK_OWNERS_PENDING, None)
    if not revisions:
        return
    for callback in tuple(_wake_callbacks):
        callback(revisions)


@event.listens_for(OrmSession, "before_commit")
def bump_live_work_revisions(db: OrmSession) -> None:
    if db.new or db.dirty or db.deleted:
        db.flush()
    if not db.info.get(_WORK_WAKE_PENDING):
        return
    owners = set(db.info.get(_WORK_OWNERS_PENDING, set()))
    if owners:
        db.info[_WORK_REVISIONS_COMMITTED] = {
            owner: _revision_upsert(db, owner) for owner in sorted(owners)
        }


@event.listens_for(OrmSession, "after_rollback")
def clear_live_work_invalidation(db: OrmSession) -> None:
    db.info.pop(_WORK_WAKE_PENDING, None)
    db.info.pop(_WORK_OWNERS_PENDING, None)
    db.info.pop(_WORK_REVISIONS_COMMITTED, None)


async def invalidate_live_work_explicit(
    db: AsyncSession,
    **_scope_hints: object,
) -> None:
    """Resolve affected owners and publish a live invalidation after commit."""

    owners: set[str] = set()

    def string_set(name: str) -> set[str]:
        value = _scope_hints.get(name)
        if not isinstance(value, (list, tuple, set, frozenset)):
            return set()
        return {item for item in value if isinstance(item, str)}

    session_ids = string_set("session_ids")
    conversation_ids = string_set("conversation_ids")
    task_ids = string_set("task_ids")
    step_run_ids = string_set("step_run_ids")
    if session_ids:
        owners.update(
            await db.scalars(select(Session.user_email).where(Session.session_id.in_(session_ids)))
        )
    if conversation_ids:
        owners.update(
            await db.scalars(
                select(Conversation.user_email).where(
                    Conversation.conversation_id.in_(conversation_ids)
                )
            )
        )
    if task_ids:
        owners.update(await db.scalars(select(Task.created_by).where(Task.task_id.in_(task_ids))))
    if step_run_ids:
        owners.update(
            await db.scalars(
                select(Task.created_by)
                .join(StepRun, StepRun.task_id == Task.task_id)
                .where(StepRun.step_run_id.in_(step_run_ids))
            )
        )
    _mark(db.sync_session, {owner for owner in owners if owner})
