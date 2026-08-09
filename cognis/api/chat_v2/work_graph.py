"""Authorized transitive work graph resolution for the Work projection."""

from __future__ import annotations

import asyncio
import time
from collections import deque
from dataclasses import dataclass
from hashlib import sha256
from typing import Any

from prometheus_client import Counter, Histogram
from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from cognis.api.chat_v2.schemas import TimelineScope, WorkstreamRef
from cognis.api.chat_v2.sync import ChatV2SyncError
from cognis.logging import get_logger
from cognis.store.models import (
    Conversation,
    ManagedConversationLink,
    Session,
    StepRun,
    Task,
)

WORK_GRAPH_MAX_NODES = 256
WORK_GRAPH_MAX_DEPTH = 24
WORK_GRAPH_MAX_SECONDS = 2.0
# A reachable session can require several indexed outgoing-edge queries.
# Keep these budgets proportional to the node cap; the request deadline remains
# the primary latency guard.
WORK_GRAPH_MAX_QUERIES = WORK_GRAPH_MAX_NODES * 8
WORK_GRAPH_MAX_RESULTS = WORK_GRAPH_MAX_NODES * 8
WORK_GRAPH_FRONTIER_CHUNK_SIZE = 256

logger = get_logger(__name__)
WORK_GRAPH_REQUESTS = Counter(
    "cognis_chat_work_graph_requests_total",
    "Authorized Work graph resolutions.",
    labelnames=("outcome",),
)
WORK_GRAPH_LATENCY = Histogram(
    "cognis_chat_work_graph_latency_seconds",
    "Wall time of one authorized Work graph resolution.",
)
WORK_GRAPH_QUERIES = Histogram(
    "cognis_chat_work_graph_queries",
    "Database queries used by one authorized Work graph resolution.",
)


@dataclass(frozen=True)
class AuthorizedWorkGraph:
    nodes: tuple[WorkstreamRef, ...]
    session_rows: tuple[Session, ...]
    fingerprint: str
    truncated: bool


class AuthorizedWorkRootNotReadyError(ValueError):
    """The authorized conversation Work root is not visible yet."""


def _typed_lineage_ids(row: Conversation) -> dict[str, set[str]]:
    result = {
        "conversation": set(),
        "session": set(),
        "task": set(),
        "step": set(),
    }
    fork_conversation_id = getattr(row, "fork_source_conversation_id", None)
    fork_session_id = getattr(row, "fork_source_session_id", None)
    task_id = getattr(row, "lineage_task_id", None)
    step_id = getattr(row, "lineage_step_run_id", None)
    if fork_conversation_id:
        result["conversation"].add(fork_conversation_id)
    if fork_session_id:
        result["session"].add(fork_session_id)
    if task_id:
        result["task"].add(task_id)
    if step_id:
        result["step"].add(step_id)
    return result


@dataclass(frozen=True)
class _Frontier:
    kind: str
    identifier: str
    parent_session_id: str | None
    edge_kind: str


async def _resolve_authorized_work_graph_sequential(
    db: AsyncSession,
    *,
    user_email: str,
    scope: TimelineScope,
    max_nodes: int = WORK_GRAPH_MAX_NODES,
    deadline: float | None = None,
) -> AuthorizedWorkGraph:
    """Resolve only indexed, directed outgoing edges from an authorized root."""

    queries = 0
    unique_results: set[tuple[str, str]] = set()
    truncated = False

    async def fetch(
        entity: type[object],
        statement: object,
        *,
        identity: Any,
        limit: int | None = None,
    ) -> list[object]:
        nonlocal queries, truncated
        if queries >= WORK_GRAPH_MAX_QUERIES:
            truncated = True
            return []
        remaining = max(0, WORK_GRAPH_MAX_RESULTS - len(unique_results))
        if remaining == 0:
            truncated = True
            return []
        requested = min(limit or remaining, remaining)
        query = db.scalars(  # type: ignore[arg-type]
            statement.order_by(identity(entity)).limit(requested + 1)  # type: ignore[attr-defined]
        )
        try:
            result = (
                await asyncio.wait_for(
                    query,
                    timeout=max(0.0, deadline - time.monotonic()),
                )
                if deadline is not None
                else await query
            )
        except TimeoutError as exc:
            raise ChatV2SyncError(
                "work_scan_timeout",
                "Work graph resolution exceeded the request deadline",
            ) from exc
        rows = list(result.all())
        queries += 1
        if len(rows) > requested:
            truncated = True
            rows = rows[:requested]
        accepted: list[object] = []
        for row in rows:
            key = (entity.__name__, str(identity(row)))
            if key in unique_results:
                accepted.append(row)
                continue
            if len(unique_results) >= WORK_GRAPH_MAX_RESULTS:
                truncated = True
                break
            unique_results.add(key)
            accepted.append(row)
        return accepted

    def conversation_id(value: Any) -> object:
        return Conversation.conversation_id if value is Conversation else value.conversation_id

    def session_id(value: Any) -> object:
        return Session.session_id if value is Session else value.session_id

    def link_id(value: Any) -> object:
        return (
            ManagedConversationLink.link_id if value is ManagedConversationLink else value.link_id
        )

    def task_id(value: Any) -> object:
        return Task.task_id if value is Task else value.task_id

    def step_id(value: Any) -> object:
        return StepRun.step_run_id if value is StepRun else value.step_run_id

    conversations: dict[str, Conversation] = {}
    sessions: dict[str, Session] = {}
    links: dict[str, ManagedConversationLink] = {}
    tasks: dict[str, Task] = {}
    steps: dict[str, StepRun] = {}
    session_parent: dict[str, tuple[str | None, str]] = {}
    queue: deque[_Frontier] = deque()
    seen_frontier: set[tuple[str, str]] = set()
    root_session_id: str | None = None

    async def exact_conversation(identifier: str) -> Conversation | None:
        rows = await fetch(
            Conversation,
            select(Conversation).where(
                Conversation.user_email == user_email,
                Conversation.status != "deleted",
                Conversation.conversation_id == identifier,
            ),
            identity=conversation_id,
            limit=1,
        )
        return next(
            (
                row
                for row in rows
                if row.conversation_id == identifier  # type: ignore[attr-defined]
                and row.user_email == user_email  # type: ignore[attr-defined]
                and row.status != "deleted"  # type: ignore[attr-defined]
            ),
            None,
        )  # type: ignore[return-value]

    async def exact_session(identifier: str) -> Session | None:
        rows = await fetch(
            Session,
            select(Session).where(
                Session.user_email == user_email,
                Session.session_id == identifier,
            ),
            identity=session_id,
            limit=1,
        )
        return next(
            (
                row
                for row in rows
                if row.session_id == identifier  # type: ignore[attr-defined]
                and row.user_email == user_email  # type: ignore[attr-defined]
            ),
            None,
        )  # type: ignore[return-value]

    if scope.kind == "conversation" and scope.conversation_id:
        root_conversation = await exact_conversation(scope.conversation_id)
        if root_conversation is None or not root_conversation.active_session_id:
            raise AuthorizedWorkRootNotReadyError("Authorized Work conversation root was not found")
        conversations[root_conversation.conversation_id] = root_conversation
        root_session_id = root_conversation.active_session_id
        current_id: str | None = root_session_id
        rotation_seen: set[str] = set()
        while current_id and current_id not in rotation_seen and len(sessions) < max_nodes:
            rotation_seen.add(current_id)
            current = await exact_session(current_id)
            if current is None:
                if current_id == root_session_id:
                    raise AuthorizedWorkRootNotReadyError(
                        "Authorized Work active root session was not found"
                    )
                truncated = True
                break
            sessions[current.session_id] = current
            queue.append(_Frontier("session", current.session_id, None, "rotation"))
            previous_id = current.previous_session_id
            session_parent[current.session_id] = (
                previous_id,
                "rotation" if previous_id else "root",
            )
            current_id = previous_id
        if current_id:
            truncated = True
        queue.append(_Frontier("conversation", root_conversation.conversation_id, None, "root"))
    elif scope.kind == "session" and scope.session_id:
        root_session = await exact_session(scope.session_id)
        if root_session is None:
            raise ValueError("Authorized Work session root was not found")
        sessions[root_session.session_id] = root_session
        session_parent[root_session.session_id] = (None, "root")
        root_session_id = root_session.session_id
        queue.append(_Frontier("session", root_session.session_id, None, "root"))
    elif scope.kind == "task_step" and scope.step_run_id:
        root_steps = await fetch(
            StepRun,
            select(StepRun).where(StepRun.step_run_id == scope.step_run_id),
            identity=step_id,
            limit=1,
        )
        if not root_steps:
            raise ValueError("Authorized Work step root was not found")
        root_step = root_steps[0]
        root_tasks = await fetch(
            Task,
            select(Task).where(
                Task.created_by == user_email,
                Task.task_id == root_step.task_id,  # type: ignore[attr-defined]
            ),
            identity=task_id,
            limit=1,
        )
        if not root_tasks:
            raise ValueError("Authorized Work task root was not found")
        steps[root_step.step_run_id] = root_step  # type: ignore[attr-defined]
        tasks[root_tasks[0].task_id] = root_tasks[0]  # type: ignore[attr-defined]
        queue.append(_Frontier("step", root_step.step_run_id, None, "root"))  # type: ignore[attr-defined]
    else:
        raise ValueError("Unsupported Work graph root scope")

    while queue and len(sessions) < max_nodes:
        frontier = queue.popleft()
        frontier_key = (frontier.kind, frontier.identifier)
        if frontier_key in seen_frontier:
            continue
        seen_frontier.add(frontier_key)
        if len(seen_frontier) > WORK_GRAPH_MAX_DEPTH * max_nodes:
            truncated = True
            break

        if frontier.kind == "session":
            row = sessions.get(frontier.identifier) or await exact_session(frontier.identifier)
            if row is None:
                continue
            sessions[row.session_id] = row
            session_parent.setdefault(
                row.session_id,
                (frontier.parent_session_id, frontier.edge_kind),
            )
            conversation = conversations.get(row.conversation_id)
            if conversation is None:
                conversation = await exact_conversation(row.conversation_id)
                if conversation is not None:
                    conversations[conversation.conversation_id] = conversation
            child_queries = (
                (Session.parent_session_id == row.session_id, "delegate"),
                (Session.previous_session_id == row.session_id, "rotation"),
            )
            for predicate, edge_kind in child_queries:
                for child in await fetch(
                    Session,
                    select(Session).where(Session.user_email == user_email, predicate),
                    identity=session_id,
                ):
                    if (
                        edge_kind == "delegate" and child.parent_session_id != row.session_id  # type: ignore[attr-defined]
                    ) or (
                        edge_kind == "rotation" and child.previous_session_id != row.session_id  # type: ignore[attr-defined]
                    ):
                        continue
                    queue.append(
                        _Frontier(
                            "session",
                            child.session_id,  # type: ignore[attr-defined]
                            row.session_id,
                            edge_kind,
                        )
                    )
            for link in await fetch(
                ManagedConversationLink,
                select(ManagedConversationLink).where(
                    ManagedConversationLink.user_email == user_email,
                    ManagedConversationLink.controller_session_id == row.session_id,
                ),
                identity=link_id,
            ):
                if (
                    link.user_email != user_email  # type: ignore[attr-defined]
                    or link.controller_session_id != row.session_id  # type: ignore[attr-defined]
                ):
                    continue
                links[link.link_id] = link  # type: ignore[attr-defined]
                queue.append(
                    _Frontier(
                        "conversation",
                        link.target_conversation_id,  # type: ignore[attr-defined]
                        row.session_id,
                        "managed",
                    )
                )
        elif frontier.kind == "conversation":
            row = conversations.get(frontier.identifier) or await exact_conversation(
                frontier.identifier
            )
            if row is None:
                continue
            conversations[row.conversation_id] = row
            active_session = (
                await exact_session(row.active_session_id) if row.active_session_id else None
            )
            if row.active_session_id:
                queue.append(
                    _Frontier(
                        "session",
                        row.active_session_id,
                        frontier.parent_session_id,
                        frontier.edge_kind,
                    )
                )
            for link in await fetch(
                ManagedConversationLink,
                select(ManagedConversationLink).where(
                    ManagedConversationLink.user_email == user_email,
                    ManagedConversationLink.controller_conversation_id == row.conversation_id,
                ),
                identity=link_id,
            ):
                if (
                    link.user_email != user_email  # type: ignore[attr-defined]
                    or link.controller_conversation_id != row.conversation_id  # type: ignore[attr-defined]
                ):
                    continue
                links[link.link_id] = link  # type: ignore[attr-defined]
                queue.append(
                    _Frontier(
                        "conversation",
                        link.target_conversation_id,  # type: ignore[attr-defined]
                        getattr(link, "controller_session_id", None),
                        "managed",
                    )
                )
            for task in await fetch(
                Task,
                select(Task).where(
                    Task.created_by == user_email,
                    Task.source_ref == row.conversation_id,
                ),
                identity=task_id,
            ):
                if (
                    task.created_by != user_email  # type: ignore[attr-defined]
                    or task.source_ref != row.conversation_id  # type: ignore[attr-defined]
                ):
                    continue
                source_id = task.source_session_id  # type: ignore[attr-defined]
                source_session = await exact_session(source_id) if source_id else None
                if (
                    active_session is None
                    or source_session is None
                    or source_session.conversation_id != row.conversation_id
                    or source_session.activity_scope_id != active_session.activity_scope_id
                ):
                    continue
                queue.append(_Frontier("task", task.task_id, None, "task"))  # type: ignore[attr-defined]
        elif frontier.kind == "task":
            task_rows = await fetch(
                Task,
                select(Task).where(
                    Task.created_by == user_email,
                    Task.task_id == frontier.identifier,
                ),
                identity=task_id,
                limit=1,
            )
            if not task_rows:
                continue
            task = next(
                (
                    row
                    for row in task_rows
                    if row.task_id == frontier.identifier  # type: ignore[attr-defined]
                    and row.created_by == user_email  # type: ignore[attr-defined]
                ),
                None,
            )
            if task is None:
                continue
            tasks[task.task_id] = task  # type: ignore[attr-defined]
            if task.control_conversation_id:  # type: ignore[attr-defined]
                queue.append(
                    _Frontier("conversation", task.control_conversation_id, None, "task")  # type: ignore[attr-defined]
                )
            for step in await fetch(
                StepRun,
                select(StepRun).where(StepRun.task_id == task.task_id),  # type: ignore[attr-defined]
                identity=step_id,
            ):
                if step.task_id != task.task_id:  # type: ignore[attr-defined]
                    continue
                queue.append(
                    _Frontier(
                        "step",
                        step.step_run_id,  # type: ignore[attr-defined]
                        task.source_session_id,  # type: ignore[attr-defined]
                        "task_step",
                    )
                )
        elif frontier.kind == "step":
            step_rows = await fetch(
                StepRun,
                select(StepRun).where(StepRun.step_run_id == frontier.identifier),
                identity=step_id,
                limit=1,
            )
            if not step_rows:
                continue
            step = next(
                (
                    row
                    for row in step_rows
                    if row.step_run_id == frontier.identifier  # type: ignore[attr-defined]
                ),
                None,
            )
            if step is None:
                continue
            task_rows = await fetch(
                Task,
                select(Task).where(
                    Task.created_by == user_email,
                    Task.task_id == step.task_id,  # type: ignore[attr-defined]
                ),
                identity=task_id,
                limit=1,
            )
            task = next(
                (
                    row
                    for row in task_rows
                    if row.task_id == step.task_id  # type: ignore[attr-defined]
                    and row.created_by == user_email  # type: ignore[attr-defined]
                ),
                None,
            )
            if task is None:
                continue
            steps[step.step_run_id] = step  # type: ignore[attr-defined]
            tasks[task.task_id] = task  # type: ignore[attr-defined]
            if step.session_id:  # type: ignore[attr-defined]
                if root_session_id is None:
                    root_session_id = step.session_id  # type: ignore[attr-defined]
                queue.append(
                    _Frontier(
                        "session",
                        step.session_id,  # type: ignore[attr-defined]
                        frontier.parent_session_id,
                        "task_step",
                    )
                )
            if step.superseded_by_step_run_id:  # type: ignore[attr-defined]
                queue.append(
                    _Frontier(
                        "step",
                        step.superseded_by_step_run_id,  # type: ignore[attr-defined]
                        frontier.parent_session_id,
                        "retry",
                    )
                )

    if queue:
        truncated = True
    selected = sorted(
        sessions.values(),
        key=lambda row: (
            0 if session_parent.get(row.session_id) == (None, "root") else 1,
            str(row.started_at or row.updated_at or ""),
            row.session_id,
        ),
    )[:max_nodes]
    unique_streams: dict[str, Session] = {}
    for row in selected:
        unique_streams.setdefault(row.intaris_session_id or row.session_id, row)
    selected = list(unique_streams.values())
    root_row = next(
        (row for row in selected if row.session_id == root_session_id),
        selected[0] if selected else None,
    )
    if root_row is None:
        raise ValueError("Authorized Work graph root session was not resolved")
    root_key = f"session:{root_row.session_id}"
    step_for_session = {step.session_id: step for step in steps.values() if step.session_id}
    managed_for_conversation = {link.target_conversation_id: link for link in links.values()}
    nodes: list[WorkstreamRef] = []
    selected_ids = {row.session_id for row in selected}
    for ordinal, row in enumerate(selected):
        parent_session_id, edge_kind = session_parent.get(row.session_id, (None, "root"))
        if parent_session_id not in selected_ids:
            parent_session_id = None
        step = step_for_session.get(row.session_id)
        if step is not None:
            task = tasks.get(step.task_id)
            if task is not None and task.source_session_id in selected_ids:
                parent_session_id = task.source_session_id
            edge_kind = "task_step"
        link = managed_for_conversation.get(row.conversation_id)
        conversation = conversations.get(row.conversation_id)
        nodes.append(
            WorkstreamRef(
                key=f"session:{row.session_id}",
                kind=edge_kind,
                parent_key=f"session:{parent_session_id}" if parent_session_id else None,
                root_key=root_key,
                edge_kind=edge_kind,
                ordinal=ordinal,
                conversation_id=row.conversation_id,
                session_id=row.session_id,
                event_store_session_id=row.intaris_session_id or row.session_id,
                task_id=step.task_id if step else None,
                step_run_id=step.step_run_id if step else None,
                link_id=link.link_id if link else None,
                title=(
                    (link.title if link else None)
                    or (step.step_name if step else None)
                    or row.delegation_task
                    or (conversation.title if conversation else None)
                    or row.agent_id
                ),
                agent_id=row.agent_id,
                agent_profile_id=row.agent_profile_id,
                status=row.status,
                attempt=getattr(step, "attempt_number", None) if step else None,
                step_name=step.step_name if step else None,
                created_at=row.started_at.isoformat() if row.started_at else None,
                updated_at=row.updated_at.isoformat() if row.updated_at else None,
                current=bool(conversation and conversation.active_session_id == row.session_id),
                superseded=bool(step and getattr(step, "superseded_by_step_run_id", None)),
            )
        )
    fingerprint = sha256(
        "|".join(
            f"{node.key}:{node.parent_key or ''}:{node.edge_kind}:{node.root_key}:"
            f"{node.event_store_session_id}"
            for node in nodes
        ).encode()
    ).hexdigest()
    return AuthorizedWorkGraph(
        nodes=tuple(nodes),
        session_rows=tuple(selected),
        fingerprint=fingerprint,
        truncated=truncated,
    )


def _chunks(values: set[str], size: int = WORK_GRAPH_FRONTIER_CHUNK_SIZE) -> list[list[str]]:
    ordered = sorted(values)
    return [ordered[index : index + size] for index in range(0, len(ordered), size)]


async def resolve_authorized_work_graph(
    db: AsyncSession,
    *,
    user_email: str,
    scope: TimelineScope,
    max_nodes: int = WORK_GRAPH_MAX_NODES,
    deadline: float | None = None,
) -> AuthorizedWorkGraph:
    """Resolve directed outgoing Work edges in bounded, indexed frontier batches."""

    started = time.monotonic()
    queries = 0
    results_seen: set[tuple[str, str]] = set()
    truncated = False

    async def fetch(
        entity: type[object],
        statement: object,
        identity: Any,
    ) -> list[Any]:
        nonlocal queries, truncated
        if queries >= WORK_GRAPH_MAX_QUERIES:
            truncated = True
            return []
        remaining = WORK_GRAPH_MAX_RESULTS - len(results_seen)
        if remaining <= 0:
            truncated = True
            return []
        awaitable = db.scalars(  # type: ignore[arg-type]
            statement.order_by(identity).limit(remaining + 1)  # type: ignore[attr-defined]
        )
        try:
            result = (
                await asyncio.wait_for(
                    awaitable,
                    timeout=max(0.0, deadline - time.monotonic()),
                )
                if deadline is not None
                else await awaitable
            )
        except TimeoutError as exc:
            latency = time.monotonic() - started
            WORK_GRAPH_REQUESTS.labels(outcome="timeout").inc()
            WORK_GRAPH_LATENCY.observe(latency)
            WORK_GRAPH_QUERIES.observe(queries)
            logger.warning(
                "chat_v2: Work graph resolution timed out",
                extra={
                    "extra_data": {
                        "scope_key": scope.key,
                        "queries": queries,
                        "latency_seconds": latency,
                    }
                },
            )
            raise ChatV2SyncError(
                "work_graph_timeout",
                "Work graph resolution exceeded its stage deadline",
            ) from exc
        queries += 1
        rows = list(result.all())
        if len(rows) > remaining:
            truncated = True
            rows = rows[:remaining]
        accepted: list[Any] = []
        for row in rows:
            key = (entity.__name__, str(getattr(row, identity.name)))
            if key not in results_seen:
                results_seen.add(key)
            accepted.append(row)
        return accepted

    async def fetch_chunks(
        entity: type[object],
        values: set[str],
        column: Any,
        identity: Any,
        *owner_predicates: Any,
    ) -> list[Any]:
        rows: list[Any] = []
        for chunk in _chunks(values):
            rows.extend(
                await fetch(
                    entity,
                    select(entity).where(*owner_predicates, column.in_(chunk)),
                    identity,
                )
            )
        return rows

    conversations: dict[str, Conversation] = {}
    sessions: dict[str, Session] = {}
    links: dict[str, ManagedConversationLink] = {}
    tasks: dict[str, Task] = {}
    steps: dict[str, StepRun] = {}
    session_parent: dict[str, tuple[str | None, str]] = {}
    root_session_id: str | None = None
    active_scope_id: str | None = None
    scope_by_conversation: dict[str, str] = {}
    current_scope_session_ids: set[str] = set()
    frontier: list[_Frontier] = []
    seen: set[tuple[str, str]] = set()

    if scope.kind == "conversation" and scope.conversation_id:
        roots = await fetch(
            Conversation,
            select(Conversation).where(
                Conversation.user_email == user_email,
                Conversation.status != "deleted",
                Conversation.conversation_id == scope.conversation_id,
            ),
            Conversation.conversation_id,
        )
        root = roots[0] if roots else None
        if root is None or not root.active_session_id:
            raise AuthorizedWorkRootNotReadyError("Authorized Work conversation root was not found")
        conversations[root.conversation_id] = root
        root_session_id = root.active_session_id
        active_rows = await fetch(
            Session,
            select(Session).where(
                Session.user_email == user_email,
                Session.session_id == root_session_id,
            ),
            Session.session_id,
        )
        if not active_rows:
            raise AuthorizedWorkRootNotReadyError(
                "Authorized Work active root session was not found"
            )
        active_scope_id = active_rows[0].activity_scope_id
        scope_by_conversation[root.conversation_id] = active_scope_id
        rotation_rows = await fetch(
            Session,
            select(Session).where(
                Session.user_email == user_email,
                Session.conversation_id == root.conversation_id,
                Session.activity_scope_id == active_scope_id,
            ),
            Session.session_id,
        )
        rotation_by_id = {row.session_id: row for row in [*active_rows, *rotation_rows]}
        current_scope_session_ids.update(rotation_by_id)
        rotation_seen: set[str] = set()
        current_id: str | None = root_session_id
        while current_id and current_id not in rotation_seen and len(sessions) < max_nodes:
            rotation_seen.add(current_id)
            row = rotation_by_id.get(current_id)
            if row is None:
                fallback = await fetch(
                    Session,
                    select(Session).where(
                        Session.user_email == user_email,
                        Session.session_id == current_id,
                        Session.activity_scope_id == active_scope_id,
                    ),
                    Session.session_id,
                )
                row = fallback[0] if fallback else None
                if row is None:
                    truncated = True
                    break
            sessions[row.session_id] = row
            session_parent[row.session_id] = (
                row.previous_session_id,
                "rotation" if row.previous_session_id else "root",
            )
            frontier.append(_Frontier("session", row.session_id, None, "rotation"))
            current_id = row.previous_session_id
        if current_id:
            truncated = True
        frontier.append(_Frontier("conversation", root.conversation_id, None, "root"))
    elif scope.kind == "session" and scope.session_id:
        roots = await fetch(
            Session,
            select(Session).where(
                Session.user_email == user_email,
                Session.session_id == scope.session_id,
            ),
            Session.session_id,
        )
        root = roots[0] if roots else None
        if root is None:
            raise ValueError("Authorized Work session root was not found")
        sessions[root.session_id] = root
        session_parent[root.session_id] = (None, "root")
        root_session_id = root.session_id
        active_scope_id = root.activity_scope_id
        scope_by_conversation[root.conversation_id] = root.activity_scope_id
        current_scope_session_ids.add(root.session_id)
        frontier.append(_Frontier("session", root.session_id, None, "root"))
        current = root
        for _rotation_depth in range(WORK_GRAPH_MAX_DEPTH):
            previous_id = current.previous_session_id
            if not previous_id:
                break
            previous_rows = await fetch(
                Session,
                select(Session).where(
                    Session.user_email == user_email,
                    Session.session_id == previous_id,
                ),
                Session.session_id,
            )
            previous = previous_rows[0] if previous_rows else None
            if previous is None:
                truncated = True
                break
            if (
                previous.conversation_id != root.conversation_id
                or previous.activity_scope_id != root.activity_scope_id
            ):
                break
            sessions[previous.session_id] = previous
            current_scope_session_ids.add(previous.session_id)
            session_parent[current.session_id] = (previous.session_id, "rotation")
            session_parent.setdefault(
                previous.session_id,
                (previous.previous_session_id, "rotation"),
            )
            frontier.append(
                _Frontier(
                    "session",
                    previous.session_id,
                    previous.previous_session_id,
                    "rotation",
                )
            )
            current = previous
        else:
            truncated = True
    elif scope.kind == "task_step" and scope.step_run_id:
        root_steps = await fetch(
            StepRun,
            select(StepRun).where(StepRun.step_run_id == scope.step_run_id),
            StepRun.step_run_id,
        )
        root_step = root_steps[0] if root_steps else None
        if root_step is None:
            raise ValueError("Authorized Work step root was not found")
        root_tasks = await fetch(
            Task,
            select(Task).where(
                Task.created_by == user_email,
                Task.task_id == root_step.task_id,
            ),
            Task.task_id,
        )
        if not root_tasks:
            raise ValueError("Authorized Work task root was not found")
        steps[root_step.step_run_id] = root_step
        tasks[root_tasks[0].task_id] = root_tasks[0]
        if root_step.session_id:
            root_sessions = await fetch(
                Session,
                select(Session).where(
                    Session.user_email == user_email,
                    Session.session_id == root_step.session_id,
                ),
                Session.session_id,
            )
            root_session = root_sessions[0] if root_sessions else None
            if root_session is None:
                raise ValueError("Authorized Work step session root was not found")
            root_session_id = root_session.session_id
            active_scope_id = root_session.activity_scope_id
            scope_by_conversation[root_session.conversation_id] = root_session.activity_scope_id
            current_scope_session_ids.add(root_session.session_id)
        frontier.append(_Frontier("step", root_step.step_run_id, None, "root"))
    else:
        raise ValueError("Unsupported Work graph root scope")

    for _depth in range(WORK_GRAPH_MAX_DEPTH):
        layer = [item for item in frontier if (item.kind, item.identifier) not in seen]
        if not layer:
            frontier = []
            break
        if len(sessions) >= max_nodes:
            truncated = True
            break
        frontier = []
        for item in layer:
            seen.add((item.kind, item.identifier))
        by_kind = {
            kind: [item for item in layer if item.kind == kind]
            for kind in ("session", "conversation", "task", "step")
        }

        session_frontier = by_kind["session"]
        session_ids = {item.identifier for item in session_frontier}
        if session_ids:
            loaded = await fetch_chunks(
                Session,
                session_ids,
                Session.session_id,
                Session.session_id,
                Session.user_email == user_email,
            )
            loaded_by_id = {row.session_id: row for row in loaded}
            for item in session_frontier:
                row = sessions.get(item.identifier) or loaded_by_id.get(item.identifier)
                if row is None:
                    continue
                sessions[row.session_id] = row
                expected_scope = scope_by_conversation.get(row.conversation_id)
                if expected_scope is not None and row.activity_scope_id == expected_scope:
                    current_scope_session_ids.add(row.session_id)
                session_parent.setdefault(
                    row.session_id,
                    (item.parent_session_id, item.edge_kind),
                )
            conversation_ids = {
                row.conversation_id for row in sessions.values() if row.session_id in session_ids
            }
            for row in await fetch_chunks(
                Conversation,
                conversation_ids,
                Conversation.conversation_id,
                Conversation.conversation_id,
                Conversation.user_email == user_email,
                Conversation.status != "deleted",
            ):
                conversations[row.conversation_id] = row
            for column, edge_kind in (
                (Session.parent_session_id, "delegate"),
                (Session.previous_session_id, "rotation"),
            ):
                children = await fetch_chunks(
                    Session,
                    session_ids,
                    column,
                    Session.session_id,
                    Session.user_email == user_email,
                )
                for child in children:
                    expected_scope = scope_by_conversation.get(child.conversation_id)
                    if expected_scope is not None and child.activity_scope_id != expected_scope:
                        continue
                    parent_id = getattr(child, column.name)
                    frontier.append(_Frontier("session", child.session_id, parent_id, edge_kind))
            for link in await fetch_chunks(
                ManagedConversationLink,
                session_ids,
                ManagedConversationLink.controller_session_id,
                ManagedConversationLink.link_id,
                ManagedConversationLink.user_email == user_email,
            ):
                if link.controller_session_id not in current_scope_session_ids:
                    continue
                links[link.link_id] = link
                frontier.append(
                    _Frontier(
                        "conversation",
                        link.target_conversation_id,
                        link.controller_session_id,
                        "managed",
                    )
                )

        conversation_frontier = by_kind["conversation"]
        conversation_ids = {item.identifier for item in conversation_frontier}
        if conversation_ids:
            for row in await fetch_chunks(
                Conversation,
                conversation_ids,
                Conversation.conversation_id,
                Conversation.conversation_id,
                Conversation.user_email == user_email,
                Conversation.status != "deleted",
            ):
                conversations[row.conversation_id] = row
            active_ids = {
                row.active_session_id
                for row in conversations.values()
                if row.conversation_id in conversation_ids and row.active_session_id
            }
            active_sessions = await fetch_chunks(
                Session,
                active_ids,
                Session.session_id,
                Session.session_id,
                Session.user_email == user_email,
            )
            for active in active_sessions:
                if active.session_id not in active_ids:
                    continue
                scope_by_conversation[active.conversation_id] = active.activity_scope_id
            scope_predicates = [
                and_(
                    Session.conversation_id == conversation_id,
                    Session.activity_scope_id == scope_id,
                )
                for conversation_id, scope_id in scope_by_conversation.items()
                if conversation_id in conversation_ids
            ]
            scoped_sessions = (
                await fetch(
                    Session,
                    select(Session).where(
                        Session.user_email == user_email,
                        or_(*scope_predicates),
                    ),
                    Session.session_id,
                )
                if scope_predicates
                else []
            )
            scoped_by_id = {
                row.session_id: row
                for row in [*active_sessions, *scoped_sessions]
                if row.session_id in active_ids
                or scope_by_conversation.get(row.conversation_id) == row.activity_scope_id
            }
            scoped_sessions = list(scoped_by_id.values())
            scoped_ids = {row.session_id for row in scoped_sessions}
            current_scope_session_ids.update(scoped_ids)
            for item in conversation_frontier:
                row = conversations.get(item.identifier)
                if row and row.active_session_id:
                    for scoped in scoped_sessions:
                        if scoped.conversation_id != row.conversation_id:
                            continue
                        if (
                            scoped.session_id != row.active_session_id
                            and not scoped.previous_session_id
                            and not scoped.parent_session_id
                        ):
                            continue
                        parent_id = (
                            item.parent_session_id
                            if scoped.session_id == row.active_session_id
                            else (scoped.previous_session_id or scoped.parent_session_id)
                        )
                        edge_kind = (
                            item.edge_kind
                            if scoped.session_id == row.active_session_id
                            else ("rotation" if scoped.previous_session_id else "delegate")
                        )
                        frontier.append(
                            _Frontier("session", scoped.session_id, parent_id, edge_kind)
                        )
            for link in await fetch_chunks(
                ManagedConversationLink,
                conversation_ids,
                ManagedConversationLink.controller_conversation_id,
                ManagedConversationLink.link_id,
                ManagedConversationLink.user_email == user_email,
            ):
                if link.controller_session_id not in current_scope_session_ids:
                    continue
                links[link.link_id] = link
                frontier.append(
                    _Frontier(
                        "conversation",
                        link.target_conversation_id,
                        link.controller_session_id,
                        "managed",
                    )
                )
            authorized_source_sessions = {
                row.session_id: row for row in [*sessions.values(), *scoped_sessions]
            }
            for task in await fetch_chunks(
                Task,
                conversation_ids,
                Task.source_ref,
                Task.task_id,
                Task.created_by == user_email,
            ):
                source_session = authorized_source_sessions.get(task.source_session_id or "")
                if (
                    source_session is None
                    or source_session.session_id not in current_scope_session_ids
                    or source_session.conversation_id != task.source_ref
                    or scope_by_conversation.get(task.source_ref)
                    != source_session.activity_scope_id
                ):
                    continue
                frontier.append(_Frontier("task", task.task_id, None, "task"))

        task_frontier = by_kind["task"]
        task_ids = {item.identifier for item in task_frontier}
        if task_ids:
            for task in await fetch_chunks(
                Task,
                task_ids,
                Task.task_id,
                Task.task_id,
                Task.created_by == user_email,
            ):
                tasks[task.task_id] = task
                if task.control_conversation_id:
                    frontier.append(
                        _Frontier("conversation", task.control_conversation_id, None, "task")
                    )
            for step in await fetch_chunks(
                StepRun,
                task_ids,
                StepRun.task_id,
                StepRun.step_run_id,
            ):
                task = tasks.get(step.task_id)
                frontier.append(
                    _Frontier(
                        "step",
                        step.step_run_id,
                        task.source_session_id if task else None,
                        "task_step",
                    )
                )

        step_frontier = by_kind["step"]
        step_ids = {item.identifier for item in step_frontier}
        if step_ids:
            loaded_steps = await fetch_chunks(
                StepRun,
                step_ids,
                StepRun.step_run_id,
                StepRun.step_run_id,
            )
            candidate_task_ids = {step.task_id for step in loaded_steps}
            authorized_tasks = {
                task.task_id: task
                for task in await fetch_chunks(
                    Task,
                    candidate_task_ids,
                    Task.task_id,
                    Task.task_id,
                    Task.created_by == user_email,
                )
            }
            item_by_id = {item.identifier: item for item in step_frontier}
            for step in loaded_steps:
                if step.task_id not in authorized_tasks:
                    continue
                item = item_by_id[step.step_run_id]
                steps[step.step_run_id] = step
                tasks[step.task_id] = authorized_tasks[step.task_id]
                if step.session_id:
                    if root_session_id is None:
                        root_session_id = step.session_id
                    frontier.append(
                        _Frontier(
                            "session",
                            step.session_id,
                            item.parent_session_id,
                            "task_step",
                        )
                    )
                if step.superseded_by_step_run_id:
                    frontier.append(
                        _Frontier(
                            "step",
                            step.superseded_by_step_run_id,
                            item.parent_session_id,
                            "retry",
                        )
                    )
    else:
        truncated = True
    if (
        any((item.kind, item.identifier) not in seen for item in frontier)
        or len(sessions) > max_nodes
    ):
        truncated = True

    selected = sorted(
        sessions.values(),
        key=lambda row: (
            0 if session_parent.get(row.session_id) == (None, "root") else 1,
            str(row.started_at or row.updated_at or ""),
            row.session_id,
        ),
    )[:max_nodes]
    unique_streams: dict[str, Session] = {}
    for row in selected:
        unique_streams.setdefault(row.intaris_session_id or row.session_id, row)
    selected = list(unique_streams.values())
    root_row = next(
        (row for row in selected if row.session_id == root_session_id),
        selected[0] if selected else None,
    )
    if root_row is None:
        raise ValueError("Authorized Work graph root session was not resolved")
    root_key = f"session:{root_row.session_id}"
    step_for_session = {step.session_id: step for step in steps.values() if step.session_id}
    managed_for_conversation = {link.target_conversation_id: link for link in links.values()}
    selected_ids = {row.session_id for row in selected}
    nodes: list[WorkstreamRef] = []
    for ordinal, row in enumerate(selected):
        parent_session_id, edge_kind = session_parent.get(row.session_id, (None, "root"))
        if parent_session_id not in selected_ids:
            parent_session_id = None
        step = step_for_session.get(row.session_id)
        if step is not None:
            task = tasks.get(step.task_id)
            if task is not None and task.source_session_id in selected_ids:
                parent_session_id = task.source_session_id
            edge_kind = "task_step"
        link = managed_for_conversation.get(row.conversation_id)
        conversation = conversations.get(row.conversation_id)
        nodes.append(
            WorkstreamRef(
                key=f"session:{row.session_id}",
                kind=edge_kind,
                parent_key=f"session:{parent_session_id}" if parent_session_id else None,
                root_key=root_key,
                edge_kind=edge_kind,
                ordinal=ordinal,
                conversation_id=row.conversation_id,
                session_id=row.session_id,
                event_store_session_id=row.intaris_session_id or row.session_id,
                task_id=step.task_id if step else None,
                step_run_id=step.step_run_id if step else None,
                link_id=link.link_id if link else None,
                title=(
                    (link.title if link else None)
                    or (step.step_name if step else None)
                    or row.delegation_task
                    or (conversation.title if conversation else None)
                    or row.agent_id
                ),
                agent_id=row.agent_id,
                agent_profile_id=row.agent_profile_id,
                status=row.status,
                attempt=getattr(step, "attempt_number", None) if step else None,
                step_name=step.step_name if step else None,
                created_at=row.started_at.isoformat() if row.started_at else None,
                updated_at=row.updated_at.isoformat() if row.updated_at else None,
                current=bool(conversation and conversation.active_session_id == row.session_id),
                superseded=bool(step and getattr(step, "superseded_by_step_run_id", None)),
            )
        )
    fingerprint = sha256(
        "|".join(
            f"{node.key}:{node.parent_key or ''}:{node.edge_kind}:{node.root_key}:"
            f"{node.event_store_session_id}"
            for node in nodes
        ).encode()
    ).hexdigest()
    latency = time.monotonic() - started
    WORK_GRAPH_REQUESTS.labels(outcome="truncated" if truncated else "complete").inc()
    WORK_GRAPH_LATENCY.observe(latency)
    WORK_GRAPH_QUERIES.observe(queries)
    logger.info(
        "chat_v2: Work graph resolution completed",
        extra={
            "extra_data": {
                "scope_key": scope.key,
                "nodes": len(nodes),
                "queries": queries,
                "truncated": truncated,
                "latency_seconds": latency,
            }
        },
    )
    return AuthorizedWorkGraph(
        nodes=tuple(nodes),
        session_rows=tuple(selected),
        fingerprint=fingerprint,
        truncated=truncated,
    )


async def _resolve_authorized_work_graph_legacy(
    db: AsyncSession,
    *,
    user_email: str,
    scope: TimelineScope,
    max_nodes: int = WORK_GRAPH_MAX_NODES,
) -> AuthorizedWorkGraph:
    """Resolve all same-owner implementation streams reachable from one root."""
    wanted = {
        "conversation": set(),
        "session": set(),
        "task": set(),
        "step": set(),
        "link": set(),
    }
    if scope.kind == "conversation" and scope.conversation_id:
        wanted["conversation"].add(scope.conversation_id)
    elif scope.kind == "session" and scope.session_id:
        wanted["session"].add(scope.session_id)
    elif scope.kind == "task_step" and scope.step_run_id:
        wanted["step"].add(scope.step_run_id)
    else:
        raise ValueError("Unsupported Work graph root scope")

    conversation_rows: dict[str, Conversation] = {}
    session_rows: dict[str, Session] = {}
    link_rows: dict[str, ManagedConversationLink] = {}
    task_rows: dict[str, Task] = {}
    step_rows: dict[str, StepRun] = {}
    queries = 0
    results_seen = 0
    truncated = False

    async def fetch(statement: object) -> list[object]:
        nonlocal queries, results_seen, truncated
        if queries >= WORK_GRAPH_MAX_QUERIES or results_seen >= WORK_GRAPH_MAX_RESULTS:
            truncated = True
            return []
        remaining = WORK_GRAPH_MAX_RESULTS - results_seen
        rows = list((await db.scalars(statement.limit(remaining + 1))).all())  # type: ignore[attr-defined]
        queries += 1
        if len(rows) > remaining:
            truncated = True
            rows = rows[:remaining]
        results_seen += len(rows)
        return rows

    async def fetch_exact(statement: object) -> list[object]:
        nonlocal queries, results_seen, truncated
        if queries >= WORK_GRAPH_MAX_QUERIES or results_seen >= WORK_GRAPH_MAX_RESULTS:
            truncated = True
            return []
        rows = list((await db.scalars(statement.limit(1))).all())  # type: ignore[attr-defined]
        queries += 1
        selected = rows[:1]
        results_seen += len(selected)
        return selected

    # Reserve and authorize the explicit root before any bounded descendant query.
    if scope.kind == "conversation":
        roots = await fetch_exact(
            select(Conversation).where(
                Conversation.user_email == user_email,
                Conversation.status != "deleted",
                Conversation.conversation_id == scope.conversation_id,
            )
        )
        root_conversation = next(
            (
                row
                for row in roots
                if row.conversation_id == scope.conversation_id
                and row.user_email == user_email
                and row.status != "deleted"
            ),
            None,
        )
        if root_conversation is None:
            raise AuthorizedWorkRootNotReadyError("Authorized Work conversation root was not found")
        conversation_rows[root_conversation.conversation_id] = root_conversation  # type: ignore[attr-defined]
        if root_conversation.active_session_id:  # type: ignore[attr-defined]
            root_sessions = await fetch_exact(
                select(Session).where(
                    Session.user_email == user_email,
                    Session.session_id == root_conversation.active_session_id,  # type: ignore[attr-defined]
                )
            )
            root_session = next(
                (
                    row
                    for row in root_sessions
                    if row.session_id == root_conversation.active_session_id
                    and row.user_email == user_email
                ),
                None,
            )
            if root_session is None:
                raise AuthorizedWorkRootNotReadyError(
                    "Authorized Work active root session was not found"
                )
            session_rows[root_session.session_id] = root_session  # type: ignore[attr-defined]
            wanted["session"].add(root_session.session_id)  # type: ignore[attr-defined]
    elif scope.kind == "session":
        root_sessions = await fetch_exact(
            select(Session).where(
                Session.user_email == user_email,
                Session.session_id == scope.session_id,
            )
        )
        root_session = next(
            (
                row
                for row in root_sessions
                if row.session_id == scope.session_id and row.user_email == user_email
            ),
            None,
        )
        if root_session is None:
            raise ValueError("Authorized Work session root was not found")
        session_rows[root_session.session_id] = root_session  # type: ignore[attr-defined]
        wanted["conversation"].add(root_session.conversation_id)  # type: ignore[attr-defined]
    else:
        root_steps = await fetch_exact(
            select(StepRun).where(StepRun.step_run_id == scope.step_run_id)
        )
        root_step = next(
            (row for row in root_steps if row.step_run_id == scope.step_run_id),
            None,
        )
        if root_step is None:
            raise ValueError("Authorized Work step root was not found")
        root_tasks = await fetch_exact(
            select(Task).where(
                Task.created_by == user_email,
                Task.task_id == root_step.task_id,  # type: ignore[attr-defined]
            )
        )
        root_task = next(
            (
                row
                for row in root_tasks
                if row.task_id == root_step.task_id and row.created_by == user_email
            ),
            None,
        )
        if root_task is None:
            raise ValueError("Authorized Work task root was not found")
        step_rows[root_step.step_run_id] = root_step  # type: ignore[attr-defined]
        task_rows[root_task.task_id] = root_task  # type: ignore[attr-defined]
        if root_step.session_id:  # type: ignore[attr-defined]
            root_sessions = await fetch_exact(
                select(Session).where(
                    Session.user_email == user_email,
                    Session.session_id == root_step.session_id,  # type: ignore[attr-defined]
                )
            )
            if root_sessions:
                session_rows[root_sessions[0].session_id] = root_sessions[0]  # type: ignore[attr-defined]

    for _depth in range(WORK_GRAPH_MAX_DEPTH):
        before = tuple(
            len(rows)
            for rows in (
                conversation_rows,
                session_rows,
                link_rows,
                task_rows,
                step_rows,
            )
        )
        if queries >= WORK_GRAPH_MAX_QUERIES:
            truncated = True
            break
        conversation_ids = wanted["conversation"] | {
            row.conversation_id for row in session_rows.values()
        }
        session_ids = wanted["session"]
        task_ids = wanted["task"]
        step_ids = wanted["step"]
        link_ids = wanted["link"]
        conversation_queries = [
            Conversation.conversation_id.in_(conversation_ids or {""}),
            Conversation.fork_source_conversation_id.in_(conversation_ids or {""}),
            Conversation.fork_source_session_id.in_(session_ids or {""}),
            Conversation.lineage_task_id.in_(task_ids or {""}),
            Conversation.lineage_step_run_id.in_(step_ids or {""}),
        ]
        session_queries = [
            Session.session_id.in_(session_ids or {""}),
            Session.conversation_id.in_(conversation_ids or {""}),
            Session.parent_session_id.in_(session_ids or {""}),
            Session.previous_session_id.in_(session_ids or {""}),
            Session.source_session_id.in_(session_ids or {""}),
        ]
        link_queries = [
            ManagedConversationLink.link_id.in_(link_ids or {""}),
            ManagedConversationLink.parent_link_id.in_(link_ids or {""}),
            ManagedConversationLink.root_link_id.in_(link_ids or {""}),
            ManagedConversationLink.controller_conversation_id.in_(conversation_ids or {""}),
            ManagedConversationLink.target_conversation_id.in_(conversation_ids or {""}),
        ]
        task_queries = [
            Task.task_id.in_(task_ids or {""}),
            Task.source_ref.in_(conversation_ids or {""}),
            Task.control_conversation_id.in_(conversation_ids or {""}),
        ]
        step_queries = [
            StepRun.step_run_id.in_(step_ids or {""}),
            StepRun.task_id.in_(task_ids or {""}),
            StepRun.conversation_id.in_(conversation_ids or {""}),
            StepRun.session_id.in_(session_ids or {""}),
        ]
        for predicate in conversation_queries:
            for row in await fetch(
                select(Conversation).where(
                    Conversation.user_email == user_email,
                    Conversation.status != "deleted",
                    predicate,
                )
            ):
                conversation_rows[row.conversation_id] = row  # type: ignore[attr-defined]
        for predicate in session_queries:
            for row in await fetch(
                select(Session).where(Session.user_email == user_email, predicate)
            ):
                session_rows[row.session_id] = row  # type: ignore[attr-defined]
        for predicate in link_queries:
            for row in await fetch(
                select(ManagedConversationLink).where(
                    ManagedConversationLink.user_email == user_email,
                    predicate,
                )
            ):
                link_rows[row.link_id] = row  # type: ignore[attr-defined]
        for predicate in task_queries:
            for row in await fetch(select(Task).where(Task.created_by == user_email, predicate)):
                task_rows[row.task_id] = row  # type: ignore[attr-defined]
        for predicate in step_queries:
            for row in await fetch(select(StepRun).where(predicate)):
                step_rows[row.step_run_id] = row  # type: ignore[attr-defined]
        for row in conversation_rows.values():
            wanted["conversation"].add(row.conversation_id)
            if row.active_session_id:
                wanted["session"].add(row.active_session_id)
            for kind, values in _typed_lineage_ids(row).items():
                wanted[kind].update(values)
        for row in session_rows.values():
            wanted["conversation"].add(row.conversation_id)
            wanted["session"].add(row.session_id)
            if row.parent_session_id:
                wanted["session"].add(row.parent_session_id)
            if row.previous_session_id:
                wanted["session"].add(row.previous_session_id)
        for row in link_rows.values():
            wanted["link"].add(row.link_id)
            wanted["conversation"].update(
                {row.controller_conversation_id, row.target_conversation_id}
            )
            if row.parent_link_id:
                wanted["link"].add(row.parent_link_id)
            if row.root_link_id:
                wanted["link"].add(row.root_link_id)
        for row in task_rows.values():
            wanted["task"].add(row.task_id)
            if row.control_conversation_id:
                wanted["conversation"].add(row.control_conversation_id)
            if row.source_ref:
                wanted["conversation"].add(row.source_ref)
        for row in step_rows.values():
            wanted["step"].add(row.step_run_id)
            wanted["task"].add(row.task_id)
            if row.conversation_id:
                wanted["conversation"].add(row.conversation_id)
            if row.session_id:
                wanted["session"].add(row.session_id)
        if results_seen >= WORK_GRAPH_MAX_RESULTS:
            truncated = True
            break
        after = tuple(
            len(rows)
            for rows in (
                conversation_rows,
                session_rows,
                link_rows,
                task_rows,
                step_rows,
            )
        )
        if after == before:
            break
    else:
        truncated = True

    conversations = list(conversation_rows.values())
    sessions = list(session_rows.values())
    links = list(link_rows.values())
    tasks = list(task_rows.values())
    steps = list(step_rows.values())

    conversation_by_id = {row.conversation_id: row for row in conversations}
    session_by_id = {row.session_id: row for row in sessions}
    task_by_id = {row.task_id: row for row in tasks}
    step_by_id = {row.step_run_id: row for row in steps}

    included_conversations: set[str] = set()
    included_sessions: set[str] = set()
    included_tasks: set[str] = set()
    included_steps: set[str] = set()
    included_links: set[str] = set()
    queue: deque[tuple[str, str]] = deque()

    if scope.kind == "conversation":
        queue.append(("conversation", scope.conversation_id or ""))
    elif scope.kind == "session":
        queue.append(("session", scope.session_id or ""))
    else:
        queue.append(("step", scope.step_run_id or ""))
    while queue:
        kind, identifier = queue.popleft()
        if kind == "session" and len(included_sessions) >= max_nodes:
            truncated = True
            continue
        if kind == "conversation":
            if identifier in included_conversations or identifier not in conversation_by_id:
                continue
            included_conversations.add(identifier)
            conversation = conversation_by_id[identifier]
            if conversation.active_session_id:
                queue.append(("session", conversation.active_session_id))
            for link in links:
                if link.controller_conversation_id == identifier:
                    included_links.add(link.link_id)
                    queue.append(("conversation", link.target_conversation_id))
            for task in tasks:
                if task.source_ref == identifier:
                    queue.append(("task", task.task_id))
        elif kind == "session":
            row = session_by_id.get(identifier)
            if row is None or identifier in included_sessions:
                continue
            included_sessions.add(identifier)
            if row.previous_session_id:
                previous = session_by_id.get(row.previous_session_id)
                if (
                    previous is not None
                    and previous.conversation_id == row.conversation_id
                    and previous.activity_scope_id == row.activity_scope_id
                ):
                    queue.append(("session", previous.session_id))
            for child in sessions:
                if child.previous_session_id == identifier or child.parent_session_id == identifier:
                    queue.append(("session", child.session_id))
            for link in links:
                if link.controller_session_id == identifier:
                    included_links.add(link.link_id)
                    queue.append(("conversation", link.target_conversation_id))
        elif kind == "task":
            task = task_by_id.get(identifier)
            if task is None or identifier in included_tasks:
                continue
            included_tasks.add(identifier)
            if task.control_conversation_id:
                queue.append(("conversation", task.control_conversation_id))
            for step in steps:
                if step.task_id == identifier:
                    queue.append(("step", step.step_run_id))
            for child in conversations:
                if getattr(child, "lineage_task_id", None) == identifier:
                    queue.append(("conversation", child.conversation_id))
        elif kind == "step":
            step = step_by_id.get(identifier)
            if step is None or identifier in included_steps:
                continue
            included_steps.add(identifier)
            if step.session_id:
                queue.append(("session", step.session_id))
            if step.superseded_by_step_run_id:
                queue.append(("step", step.superseded_by_step_run_id))
            for child in conversations:
                if getattr(child, "lineage_step_run_id", None) == identifier:
                    queue.append(("conversation", child.conversation_id))

    selected = [row for row in sessions if row.session_id in included_sessions]
    step_for_session = {
        step.session_id: step
        for step in steps
        if step.session_id and step.step_run_id in included_steps
    }
    managed_for_conversation = {
        link.target_conversation_id: link for link in links if link.link_id in included_links
    }
    root_session_id = scope.session_id
    if scope.conversation_id and scope.conversation_id in conversation_by_id:
        root_session_id = conversation_by_id[scope.conversation_id].active_session_id
    if scope.step_run_id and scope.step_run_id in step_by_id:
        root_session_id = step_by_id[scope.step_run_id].session_id
    selected.sort(
        key=lambda row: (
            0 if row.session_id == root_session_id else 1,
            0 if row.previous_session_id else 1,
            str(row.started_at or row.updated_at or ""),
            row.conversation_id,
            row.session_id,
        )
    )
    unique_streams: dict[str, Session] = {}
    for row in selected:
        stream_id = row.intaris_session_id or row.session_id
        unique_streams.setdefault(stream_id, row)
    selected = list(unique_streams.values())
    keys = {row.session_id: f"session:{row.session_id}" for row in selected}
    root_key = keys.get(root_session_id or "")
    if root_key is None:
        raise ValueError("Authorized Work graph root session was not resolved")
    nodes: list[WorkstreamRef] = []
    for ordinal, row in enumerate(selected):
        step = step_for_session.get(row.session_id)
        link = managed_for_conversation.get(row.conversation_id)
        conversation = conversation_by_id.get(row.conversation_id)
        parent_key: str | None = None
        edge_kind = "root"
        kind = "root"
        if row.previous_session_id in keys:
            parent_key = keys[row.previous_session_id]
            edge_kind = "rotation"
            kind = "rotation"
        elif row.parent_session_id in keys:
            parent_key = keys[row.parent_session_id]
            edge_kind = "delegate"
            kind = "delegate"
        elif link is not None:
            parent_key = keys.get(getattr(link, "controller_session_id", None) or "")
            edge_kind = "managed"
            kind = "managed"
        elif step is not None:
            task = task_by_id.get(step.task_id)
            if scope.kind == "conversation" and task is not None:
                parent_key = keys.get(task.source_session_id or "")
            edge_kind = "task_step"
            kind = "task_step"
        title = (
            (getattr(link, "title", None) if link else None)
            or (step.step_name if step else None)
            or row.delegation_task
            or (conversation.title if conversation else None)
            or row.agent_id
        )
        nodes.append(
            WorkstreamRef(
                key=keys[row.session_id],
                kind=kind,
                parent_key=parent_key,
                root_key=root_key,
                edge_kind=edge_kind,
                ordinal=ordinal,
                conversation_id=row.conversation_id,
                session_id=row.session_id,
                event_store_session_id=row.intaris_session_id or row.session_id,
                task_id=step.task_id if step else None,
                step_run_id=step.step_run_id if step else None,
                link_id=link.link_id if link else None,
                title=title,
                agent_id=row.agent_id,
                agent_profile_id=row.agent_profile_id,
                status=(step.status if step else row.status) or "unknown",
                attempt=step.attempt_number if step else None,
                step_name=step.step_name if step else None,
                created_at=row.started_at.isoformat() if row.started_at else None,
                updated_at=row.updated_at.isoformat() if row.updated_at else None,
                current=(
                    conversation.active_session_id == row.session_id if conversation else False
                ),
                superseded=bool(step and step.superseded_by_step_run_id),
            )
        )
    fingerprint_source = "|".join(
        f"{node.ordinal}:intaris:{node.event_store_session_id}:{node.kind}" for node in nodes
    )
    fingerprint = sha256(fingerprint_source.encode()).hexdigest()
    return AuthorizedWorkGraph(
        nodes=tuple(nodes),
        session_rows=tuple(selected),
        fingerprint=fingerprint,
        truncated=truncated,
    )
