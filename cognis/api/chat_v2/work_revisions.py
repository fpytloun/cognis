"""Durable monotonic revisions for rebuildable Intaris-derived Work views."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from cognis.api.chat_v2.schemas import TimelineScope
from cognis.api.chat_v2.work_graph import AuthorizedWorkGraph
from cognis.store.models import WorkScopeState, WorkScopeStream


@dataclass(frozen=True, slots=True)
class WorkRevisionSnapshot:
    scope_key: str
    work_revision: int
    graph_revision: int


@dataclass(frozen=True, slots=True)
class WorkInvalidation:
    scope_key: str
    scope_kind: str
    root_id: str
    user_email: str
    work_revision: int
    graph_revision: int


def _root_id(scope: TimelineScope) -> str:
    if scope.kind == "conversation" and scope.conversation_id:
        return str(scope.conversation_id)
    if scope.kind == "session" and scope.session_id:
        return str(scope.session_id)
    if scope.kind == "task_step" and scope.step_run_id:
        return str(scope.step_run_id)
    raise ValueError("Unsupported Work revision root scope")


async def reconcile_work_scope_revision(
    db: AsyncSession,
    *,
    user_email: str,
    scope: TimelineScope,
    graph: AuthorizedWorkGraph,
    stream_watermarks: Mapping[str, int],
) -> WorkRevisionSnapshot:
    """Reconcile one rebuildable scope against authoritative graph and watermarks.

    The operation stores no event content. A missed append notification is
    repaired by the next read because Intaris high-watermarks only advance.
    """

    state = await db.scalar(
        select(WorkScopeState).where(WorkScopeState.scope_key == scope.key).with_for_update()
    )
    if state is None:
        candidate = WorkScopeState(
            scope_key=scope.key,
            user_email=user_email,
            scope_kind=scope.kind,
            root_id=_root_id(scope),
        )
        try:
            async with db.begin_nested():
                db.add(candidate)
                await db.flush()
        except IntegrityError:
            pass
        state = await db.scalar(
            select(WorkScopeState).where(WorkScopeState.scope_key == scope.key).with_for_update()
        )
    if state is None or state.user_email != user_email:
        raise ValueError("Authorized Work revision scope was not found")

    existing = {
        row.session_id: row
        for row in (
            await db.scalars(
                select(WorkScopeStream)
                .where(WorkScopeStream.scope_key == scope.key)
                .with_for_update()
            )
        ).all()
    }
    expected_session_ids = {row.session_id for row in graph.session_rows}
    graph_changed = (
        state.graph_fingerprint != graph.fingerprint or set(existing) != expected_session_ids
    )
    revision_delta = 1 if graph_changed else 0
    now = datetime.now(UTC)

    node_by_session = {node.session_id: node for node in graph.nodes}
    for row in graph.session_rows:
        node = node_by_session[row.session_id]
        source_last_seq = max(0, int(stream_watermarks.get(node.event_store_session_id, 0)))
        membership = existing.get(row.session_id)
        if membership is None:
            db.add(
                WorkScopeStream(
                    scope_key=scope.key,
                    session_id=row.session_id,
                    event_store_id="intaris",
                    event_store_session_id=node.event_store_session_id,
                    last_seq=source_last_seq,
                )
            )
            revision_delta += source_last_seq
            continue
        if (
            membership.event_store_id != "intaris"
            or membership.event_store_session_id != node.event_store_session_id
        ):
            membership.event_store_id = "intaris"
            membership.event_store_session_id = node.event_store_session_id
            membership.last_seq = source_last_seq
            membership.updated_at = now
            revision_delta += max(1, source_last_seq)
            continue
        if source_last_seq > membership.last_seq:
            revision_delta += source_last_seq - membership.last_seq
            membership.last_seq = source_last_seq
            membership.updated_at = now

    stale_session_ids = set(existing) - expected_session_ids
    if stale_session_ids:
        await db.execute(
            delete(WorkScopeStream).where(
                WorkScopeStream.scope_key == scope.key,
                WorkScopeStream.session_id.in_(stale_session_ids),
            )
        )

    if graph_changed:
        state.graph_fingerprint = graph.fingerprint
        state.graph_revision += 1
    if revision_delta:
        state.work_revision += revision_delta
    state.scope_kind = scope.kind
    state.root_id = _root_id(scope)
    state.updated_at = now
    await db.flush()
    return WorkRevisionSnapshot(
        scope_key=scope.key,
        work_revision=state.work_revision,
        graph_revision=state.graph_revision,
    )


async def advance_work_revisions_for_stream(
    db: AsyncSession,
    *,
    user_email: str,
    event_store_id: str,
    event_store_session_id: str,
    last_seq: int,
    include_current: bool = False,
) -> list[WorkInvalidation]:
    """Advance all materialized root scopes that contain one Intaris stream."""

    if last_seq < 0:
        raise ValueError("last_seq must be nonnegative")
    scope_keys = (
        await db.scalars(
            select(WorkScopeStream.scope_key)
            .join(
                WorkScopeState,
                WorkScopeState.scope_key == WorkScopeStream.scope_key,
            )
            .where(
                WorkScopeState.user_email == user_email,
                WorkScopeStream.event_store_id == event_store_id,
                WorkScopeStream.event_store_session_id == event_store_session_id,
            )
            .order_by(WorkScopeStream.scope_key)
        )
    ).all()
    if not scope_keys:
        return []

    states = {
        state.scope_key: state
        for state in (
            await db.scalars(
                select(WorkScopeState)
                .where(
                    WorkScopeState.user_email == user_email,
                    WorkScopeState.scope_key.in_(scope_keys),
                )
                .order_by(WorkScopeState.scope_key)
                .with_for_update()
            )
        ).all()
    }
    rows = (
        await db.scalars(
            select(WorkScopeStream)
            .where(
                WorkScopeStream.scope_key.in_(states),
                WorkScopeStream.event_store_id == event_store_id,
                WorkScopeStream.event_store_session_id == event_store_session_id,
            )
            .order_by(WorkScopeStream.scope_key)
            .with_for_update()
        )
    ).all()
    invalidations: list[WorkInvalidation] = []
    advanced_scope_keys: set[str] = set()
    now = datetime.now(UTC)
    for row in rows:
        if last_seq <= row.last_seq:
            continue
        state = states.get(row.scope_key)
        if state is None:
            continue
        state.work_revision += last_seq - row.last_seq
        state.updated_at = now
        row.last_seq = last_seq
        row.updated_at = now
        advanced_scope_keys.add(row.scope_key)

    await db.flush()
    for state in states.values():
        if not include_current and state.scope_key not in advanced_scope_keys:
            continue
        invalidations.append(
            WorkInvalidation(
                scope_key=state.scope_key,
                scope_kind=state.scope_kind,
                root_id=state.root_id,
                user_email=state.user_email,
                work_revision=state.work_revision,
                graph_revision=state.graph_revision,
            )
        )
    return invalidations


__all__ = [
    "WorkInvalidation",
    "WorkRevisionSnapshot",
    "advance_work_revisions_for_stream",
    "reconcile_work_scope_revision",
]
