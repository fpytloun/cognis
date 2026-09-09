"""Authorized transitive work graph resolution for the Work projection."""

from __future__ import annotations

import asyncio
import time
from collections import deque
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from hashlib import sha256
from typing import Any, Literal

from prometheus_client import Counter, Histogram
from sqlalchemy import and_, exists, or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import aliased

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
WORK_GRAPH_MAX_SECONDS = 5.0
# A reachable session can require several indexed outgoing-edge queries.
# Keep these budgets proportional to the node cap; the request deadline remains
# the primary latency guard.
WORK_GRAPH_MAX_QUERIES = WORK_GRAPH_MAX_NODES * 8
WORK_GRAPH_MAX_RESULTS = WORK_GRAPH_MAX_NODES * 8
WORK_GRAPH_FRONTIER_CHUNK_SIZE = 256
WORK_STRUCTURAL_EDGE_PRECEDENCE = {
    "rotation": 10,
    "delegate": 20,
    "task": 30,
    "retry": 40,
    "task_step": 50,
    "managed": 60,
    "root": 70,
}

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
WORK_GRAPH_SINGLEFLIGHT = Counter(
    "cognis_chat_work_graph_singleflight_total",
    "Authorized Work graph single-flight outcomes.",
    labelnames=("outcome",),
)


@dataclass(frozen=True)
class AuthorizedWorkGraph:
    nodes: tuple[WorkstreamRef, ...]
    session_rows: tuple[Session, ...]
    fingerprint: str
    truncated: bool


class AuthorizedWorkRootNotReadyError(ValueError):
    """The authorized conversation Work root is not visible yet."""


@dataclass(frozen=True, slots=True)
class _WorkGraphFlightKey:
    user_email: str
    scope_key: str
    max_nodes: int
    source_revision: int | None


@dataclass(slots=True)
class _WorkGraphFlight:
    task: asyncio.Task[AuthorizedWorkGraph]
    waiters: int = 0


class AuthorizedWorkGraphResolver:
    """Share concurrent identical Work graph reads within one controller."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        max_flights: int = 256,
        deadline_seconds: float = WORK_GRAPH_MAX_SECONDS,
    ) -> None:
        if max_flights < 1:
            raise ValueError("max_flights must be positive")
        if deadline_seconds <= 0:
            raise ValueError("deadline_seconds must be positive")
        self._session_factory = session_factory
        self._max_flights = max_flights
        self._deadline_seconds = deadline_seconds
        self._flights: dict[_WorkGraphFlightKey, _WorkGraphFlight] = {}
        self._bypass_tasks: set[asyncio.Task[AuthorizedWorkGraph]] = set()
        self._lock = asyncio.Lock()
        self._accepting = True

    @property
    def flight_count(self) -> int:
        return len(self._flights)

    @property
    def bypass_count(self) -> int:
        return len(self._bypass_tasks)

    async def resolve(
        self,
        *,
        user_email: str,
        scope: TimelineScope,
        max_nodes: int = WORK_GRAPH_MAX_NODES,
        source_revision: int | None = None,
    ) -> AuthorizedWorkGraph:
        key = _WorkGraphFlightKey(
            user_email=user_email,
            scope_key=scope.key,
            max_nodes=max_nodes,
            source_revision=source_revision,
        )
        bypass_task: asyncio.Task[AuthorizedWorkGraph] | None = None
        async with self._lock:
            if not self._accepting:
                raise RuntimeError("Work graph resolver is stopped")
            flight = self._flights.get(key)
            if flight is None:
                if len(self._flights) >= self._max_flights:
                    bypass_task = asyncio.create_task(
                        self._resolve(
                            user_email=user_email,
                            scope=scope.model_copy(deep=True),
                            max_nodes=max_nodes,
                        )
                    )
                    self._bypass_tasks.add(bypass_task)
                    bypass_task.add_done_callback(self._consume_bypass_result)
                    WORK_GRAPH_SINGLEFLIGHT.labels(outcome="bypassed").inc()
                else:
                    task = asyncio.create_task(
                        self._build(
                            key,
                            user_email=user_email,
                            scope=scope.model_copy(deep=True),
                            max_nodes=max_nodes,
                        )
                    )
                    task.add_done_callback(self._consume_task_result)
                    flight = _WorkGraphFlight(task=task, waiters=0)
                    self._flights[key] = flight
                    WORK_GRAPH_SINGLEFLIGHT.labels(outcome="created").inc()
            else:
                WORK_GRAPH_SINGLEFLIGHT.labels(outcome="joined").inc()
            if flight is not None:
                flight.waiters += 1

        if bypass_task is not None:
            return await bypass_task
        assert flight is not None

        try:
            return await asyncio.shield(flight.task)
        finally:
            async with self._lock:
                current = self._flights.get(key)
                if current is flight:
                    flight.waiters = max(0, flight.waiters - 1)
                    if flight.waiters == 0 and not flight.task.done():
                        self._flights.pop(key, None)
                        flight.task.cancel()
                        WORK_GRAPH_SINGLEFLIGHT.labels(outcome="cancelled").inc()

    async def stop(self) -> None:
        async with self._lock:
            self._accepting = False
            tasks = [
                *(flight.task for flight in self._flights.values()),
                *self._bypass_tasks,
            ]
            self._flights.clear()
            self._bypass_tasks.clear()
            for task in tasks:
                task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _build(
        self,
        key: _WorkGraphFlightKey,
        *,
        user_email: str,
        scope: TimelineScope,
        max_nodes: int,
    ) -> AuthorizedWorkGraph:
        try:
            result = await self._resolve(
                user_email=user_email,
                scope=scope,
                max_nodes=max_nodes,
            )
            WORK_GRAPH_SINGLEFLIGHT.labels(outcome="succeeded").inc()
            return result
        except asyncio.CancelledError:
            raise
        except Exception:
            WORK_GRAPH_SINGLEFLIGHT.labels(outcome="failed").inc()
            raise
        finally:
            async with self._lock:
                current = self._flights.get(key)
                if current is not None and current.task is asyncio.current_task():
                    self._flights.pop(key, None)

    async def _resolve(
        self,
        *,
        user_email: str,
        scope: TimelineScope,
        max_nodes: int,
    ) -> AuthorizedWorkGraph:
        async with self._session_factory() as db:
            return await resolve_authorized_work_graph(
                db,
                user_email=user_email,
                scope=scope,
                max_nodes=max_nodes,
                deadline=time.monotonic() + self._deadline_seconds,
            )

    @staticmethod
    def _consume_task_result(task: asyncio.Task[AuthorizedWorkGraph]) -> None:
        if task.cancelled():
            return
        try:
            task.exception()
        except Exception:
            return

    def _consume_bypass_result(self, task: asyncio.Task[AuthorizedWorkGraph]) -> None:
        self._bypass_tasks.discard(task)
        self._consume_task_result(task)


def canonical_work_structural_edge(
    existing: tuple[str | None, str] | None,
    candidate: tuple[str | None, str],
) -> tuple[str | None, str]:
    """Select one structural edge independently of traversal order."""

    candidate_parent, candidate_kind = candidate
    if candidate_kind == "root":
        candidate_parent = None
    candidate = (candidate_parent, candidate_kind)
    if existing is None:
        return candidate
    existing_parent, existing_kind = existing
    if existing_kind == "root":
        existing_parent = None
    existing = (existing_parent, existing_kind)
    existing_rank = (
        WORK_STRUCTURAL_EDGE_PRECEDENCE.get(existing_kind, 0),
        existing_kind,
        existing_parent or "",
    )
    candidate_rank = (
        WORK_STRUCTURAL_EDGE_PRECEDENCE.get(candidate_kind, 0),
        candidate_kind,
        candidate_parent or "",
    )
    return candidate if candidate_rank > existing_rank else existing


def merge_work_session_edge(
    session_parent: dict[str, tuple[str | None, str]],
    *,
    session_id: str,
    parent_session_id: str | None,
    edge_kind: str,
) -> None:
    """Merge one physical session edge through the canonical policy."""

    session_parent[session_id] = canonical_work_structural_edge(
        session_parent.get(session_id),
        (parent_session_id, edge_kind),
    )


def same_work_activity_scope(
    expected_scope_id: str | None,
    actual_scope_id: str | None,
) -> bool:
    """Compare scopes without conflating a missing map key with NULL."""

    return actual_scope_id == expected_scope_id


_LIVE_DESCENDANT_SESSION_STATUSES = frozenset({"active", "pending"})


def canonical_work_session_order(
    rows: Iterable[Session],
    *,
    session_parent: Mapping[str, tuple[str | None, str]],
    max_nodes: int | None = None,
    current_session_ids: frozenset[str] = frozenset(),
    root_session_id: str | None = None,
) -> list[Session]:
    """Apply stable public WorkstreamRef ordering and source-stream deduplication."""

    selected = sorted(
        rows,
        key=lambda row: (
            0 if row.session_id == root_session_id else 1,
            0 if session_parent.get(row.session_id) == (None, "root") else 1,
            0 if row.session_id in current_session_ids else 1,
            str(row.started_at or row.updated_at or ""),
            row.session_id,
        ),
    )
    unique_streams: dict[str, Session] = {}
    for row in selected:
        unique_streams.setdefault(row.intaris_session_id or row.session_id, row)
    deduplicated = list(unique_streams.values())
    if max_nodes is not None:
        by_id = {row.session_id: row for row in deduplicated}
        retained: dict[str, Session] = {}
        for row in deduplicated:
            chain: dict[str, Session] = {}
            candidate: Session | None = row
            while candidate is not None and candidate.session_id not in retained:
                if candidate.session_id in chain:
                    break
                chain[candidate.session_id] = candidate
                parent_id = session_parent.get(candidate.session_id, (None, "root"))[0]
                candidate = by_id.get(parent_id or "")
            if len(retained.keys() | chain.keys()) <= max_nodes:
                retained.update(reversed(list(chain.items())))
        deduplicated = [row for row in deduplicated if row.session_id in retained]
    return deduplicated


def canonical_work_graph_fingerprint(nodes: Iterable[WorkstreamRef]) -> str:
    """Fingerprint ordered canonical WorkstreamRef topology."""

    return sha256(
        repr(
            tuple(
                (
                    node.key,
                    node.parent_key,
                    node.edge_kind,
                    node.root_key,
                    node.event_store_session_id,
                    tuple(node.backing_session_ids),
                )
                for node in nodes
            )
        ).encode()
    ).hexdigest()


def _empty_authorized_work_graph() -> AuthorizedWorkGraph:
    """Return the stable empty graph for a conversation without sessions."""

    return AuthorizedWorkGraph(
        nodes=(),
        session_rows=(),
        fingerprint=canonical_work_graph_fingerprint(()),
        truncated=False,
    )


def _latest_session_per_conversation_statement(
    *,
    user_email: str,
    conversation_ids: Iterable[str],
) -> Any:
    """Select the newest authorized session for each requested conversation."""

    newer = aliased(Session)
    recency = or_(
        newer.updated_at > Session.updated_at,
        and_(
            newer.updated_at == Session.updated_at,
            newer.started_at > Session.started_at,
        ),
        and_(
            newer.updated_at == Session.updated_at,
            newer.started_at == Session.started_at,
            newer.session_id > Session.session_id,
        ),
    )
    return (
        select(Session)
        .where(
            Session.user_email == user_email,
            Session.conversation_id.in_(list(conversation_ids) or [""]),
            ~exists(
                select(1)
                .select_from(newer)
                .where(
                    newer.user_email == Session.user_email,
                    newer.conversation_id == Session.conversation_id,
                    recency,
                )
            ),
        )
        .order_by(
            Session.updated_at.desc(),
            Session.started_at.desc(),
            Session.session_id.desc(),
        )
    )


def canonical_work_topology_fingerprint(
    nodes: Iterable[tuple[str, str | None, str, str, str]],
) -> str:
    """Fingerprint ordered canonical Work topology fields."""

    return sha256(
        "|".join(
            f"{key}:{parent_key or ''}:{edge_kind}:{root_key}:{event_store_session_id}"
            for key, parent_key, edge_kind, root_key, event_store_session_id in nodes
        ).encode()
    ).hexdigest()


def canonical_workstream_refs(
    selected: Iterable[Session],
    *,
    session_parent: Mapping[str, tuple[str | None, str]],
    root_session_id: str,
    steps: Iterable[StepRun],
    tasks: Mapping[str, Task],
    links: Iterable[ManagedConversationLink],
    conversations: Mapping[str, Conversation],
) -> list[WorkstreamRef]:
    """Collapse physical rotations into deterministic logical WorkstreamRef nodes."""

    selected_rows = list(selected)
    selected_ids = {row.session_id for row in selected_rows}
    root_row = next(
        (row for row in selected_rows if row.session_id == root_session_id),
        selected_rows[0] if selected_rows else None,
    )
    if root_row is None:
        raise ValueError("Authorized Work graph root session was not resolved")
    rows_by_id = {row.session_id: row for row in selected_rows}

    def rotation_root_session_id(row: Session) -> str:
        """Return the stable root of one in-scope compaction lineage."""

        current = row
        seen = {row.session_id}
        while current.previous_session_id:
            predecessor = rows_by_id.get(current.previous_session_id)
            if (
                predecessor is None
                or predecessor.conversation_id != row.conversation_id
                or not same_work_activity_scope(
                    row.activity_scope_id,
                    predecessor.activity_scope_id,
                )
            ):
                break
            if predecessor.session_id in seen:
                return min(seen)
            seen.add(predecessor.session_id)
            current = predecessor
        return current.session_id

    def logical_group_key(row: Session) -> str:
        rotation_root = rows_by_id[rotation_root_session_id(row)]
        # A delegate inherits its controller's activity scope, but it is a
        # separate workstream. Keep its compaction successors together while
        # separating that lineage from the controller and sibling delegates.
        if getattr(rotation_root, "delegation_mode", None) is not None:
            return f"delegate:{rotation_root.session_id}"
        return row.activity_scope_id or f"session:{row.session_id}"

    group_key_by_session = {row.session_id: logical_group_key(row) for row in selected_rows}
    rows_by_group: dict[str, list[Session]] = {}
    physical_ordinal = {row.session_id: ordinal for ordinal, row in enumerate(selected_rows)}
    for row in selected_rows:
        rows_by_group.setdefault(group_key_by_session[row.session_id], []).append(row)
    root_key = group_key_by_session[root_row.session_id]

    step_for_session: dict[str, StepRun] = {}
    for step in sorted(
        (step for step in steps if step.session_id),
        key=lambda item: (
            int(getattr(item, "attempt_number", 0) or 0),
            item.step_run_id,
        ),
    ):
        step_for_session[str(step.session_id)] = step
    managed_for_conversation: dict[str, ManagedConversationLink] = {}
    for link in sorted(links, key=lambda item: item.link_id):
        managed_for_conversation[link.target_conversation_id] = link

    logical_edges: dict[str, tuple[str | None, str]] = {}
    for logical_key, rows in rows_by_group.items():
        edge: tuple[str | None, str] | None = None
        for row in rows:
            parent_session_id, edge_kind = session_parent.get(
                row.session_id,
                (None, "root"),
            )
            edge = canonical_work_structural_edge(
                edge,
                (parent_session_id, edge_kind),
            )
            edge_step = step_for_session.get(row.session_id)
            if edge_step is not None:
                edge_task = tasks.get(edge_step.task_id)
                edge = canonical_work_structural_edge(
                    edge,
                    (
                        (edge_task.source_session_id if edge_task is not None else None)
                        or parent_session_id,
                        "task_step",
                    ),
                )
            rotation_root = rows_by_id[rotation_root_session_id(row)]
            edge_link = managed_for_conversation.get(row.conversation_id)
            if edge_link is not None and getattr(rotation_root, "delegation_mode", None) is None:
                edge = canonical_work_structural_edge(
                    edge,
                    (edge_link.controller_session_id, "managed"),
                )
        if logical_key == root_key:
            logical_edges[logical_key] = (None, "root")
            continue
        parent_session_id, edge_kind = edge or (None, "root")
        parent_key = (
            group_key_by_session.get(parent_session_id)
            if parent_session_id in selected_ids
            else None
        )
        if parent_key == logical_key:
            parent_key = None
        logical_edges[logical_key] = (parent_key, edge_kind)

    def representative(rows: list[Session]) -> Session:
        current = [
            row
            for row in rows
            if (
                (conversation := conversations.get(row.conversation_id))
                and conversation.active_session_id == row.session_id
            )
        ]
        return max(
            current or rows,
            key=lambda row: (
                str(row.updated_at or row.started_at or ""),
                row.session_id,
            ),
        )

    ordered_groups = sorted(
        rows_by_group,
        key=lambda logical_key: (
            0 if logical_key == root_key else 1,
            min(physical_ordinal[row.session_id] for row in rows_by_group[logical_key]),
            logical_key,
        ),
    )
    nodes: list[WorkstreamRef] = []
    for ordinal, logical_key in enumerate(ordered_groups):
        rows = rows_by_group[logical_key]
        row = representative(rows)
        parent_key, edge_kind = logical_edges[logical_key]
        node_step = step_for_session.get(row.session_id)
        node_link = (
            managed_for_conversation.get(row.conversation_id) if edge_kind == "managed" else None
        )
        node_conversation = conversations.get(row.conversation_id)
        backing_session_ids = sorted(member.session_id for member in rows)
        nodes.append(
            WorkstreamRef(
                key=logical_key,
                kind=edge_kind,
                parent_key=parent_key,
                root_key=root_key,
                edge_kind=edge_kind,
                ordinal=ordinal,
                conversation_id=row.conversation_id,
                session_id=row.session_id,
                event_store_session_id=row.intaris_session_id or row.session_id,
                task_id=node_step.task_id if node_step else None,
                step_run_id=node_step.step_run_id if node_step else None,
                link_id=node_link.link_id if node_link else None,
                title=(
                    (node_link.title if node_link else None)
                    or (node_step.step_name if node_step else None)
                    or row.delegation_task
                    or (node_conversation.title if node_conversation else None)
                    or row.agent_id
                ),
                agent_id=row.agent_id,
                agent_profile_id=row.agent_profile_id,
                status=row.status,
                attempt=getattr(node_step, "attempt_number", None) if node_step else None,
                step_name=node_step.step_name if node_step else None,
                created_at=row.started_at.isoformat() if row.started_at else None,
                updated_at=row.updated_at.isoformat() if row.updated_at else None,
                current=bool(
                    node_conversation and node_conversation.active_session_id == row.session_id
                ),
                superseded=bool(
                    node_step and getattr(node_step, "superseded_by_step_run_id", None)
                ),
                activity_scope_id=row.activity_scope_id,
                backing_session_count=len(backing_session_ids),
                backing_session_ids=backing_session_ids,
                execution_state=None,
            )
        )
    _validate_logical_work_tree(nodes, root_key=root_key)
    return nodes


_TERMINAL_CANCELLED = {"cancelled", "canceled", "aborted"}
_TERMINAL_FAILED = {"failed", "error", "ambiguous", "interrupted"}
_TERMINAL_COMPLETED = {"completed", "complete", "succeeded", "closed", "absorbed"}
_WAITING = {
    "waiting",
    "waiting_controller",
    "blocked",
    "paused",
    "pending_input",
    "handoff",
}
_RUNNING = {"running", "claimed", "absorbing", "in_progress"}
_QUEUED = {"queued", "pending", "admitted"}
ExecutionState = Literal[
    "idle",
    "queued",
    "running",
    "waiting",
    "recovering",
    "completed",
    "failed",
    "cancelled",
]


def strongest_work_execution_status(values: Iterable[str | None]) -> ExecutionState | None:
    statuses = {value.lower() for value in values if value}
    result: ExecutionState
    for candidates, result in (
        (_WAITING, "waiting"),
        (_RUNNING, "running"),
        ({"recoverable", "recovering"}, "recovering"),
        (_QUEUED, "queued"),
        ({"idle", "active", "open"}, "idle"),
        (_TERMINAL_CANCELLED, "cancelled"),
        (_TERMINAL_FAILED, "failed"),
        (_TERMINAL_COMPLETED, "completed"),
    ):
        if statuses & candidates:
            return result
    return None


def _validate_logical_work_tree(nodes: Iterable[WorkstreamRef], *, root_key: str) -> None:
    parent_by_key = {node.key: node.parent_key for node in nodes}
    if root_key not in parent_by_key:
        raise ValueError("Authorized Work graph logical root was not resolved")
    if sum(parent is None for parent in parent_by_key.values()) != 1:
        raise ValueError("Authorized Work graph must contain exactly one logical root")
    if any(parent is not None and parent not in parent_by_key for parent in parent_by_key.values()):
        raise ValueError("Authorized Work graph contains a missing logical parent")
    for key in parent_by_key:
        seen: set[str] = set()
        current: str | None = key
        while current is not None:
            if current in seen:
                raise ValueError("Authorized Work graph contains a logical parent cycle")
            seen.add(current)
            current = parent_by_key.get(current)


def derive_work_execution_state(
    *,
    direct_turn_status: str | None = None,
    managed_turn_state: str | None = None,
    managed_conversation_state: str | None = None,
    step_status: str | None = None,
    task_status: str | None = None,
    session_status: str | None = None,
    delegated_session: bool = False,
    recovering: bool = False,
) -> ExecutionState | None:
    """Normalize one already identity-scoped durable lifecycle owner."""

    if recovering:
        return "recovering"
    normalized_session = strongest_work_execution_status([session_status])
    if delegated_session and session_status == "active":
        normalized_session = "running"
    if normalized_session in {"completed", "failed", "cancelled"}:
        return normalized_session
    normalized_direct = strongest_work_execution_status([direct_turn_status])
    if normalized_direct in {"queued", "running", "waiting", "recovering"}:
        return normalized_direct
    normalized_managed = strongest_work_execution_status([managed_turn_state])
    if normalized_managed in {"queued", "running", "waiting", "recovering"}:
        return normalized_managed
    normalized_step = strongest_work_execution_status([step_status])
    if normalized_step in {"queued", "running", "waiting", "recovering"}:
        return normalized_step
    if normalized_managed in {"completed", "failed", "cancelled"}:
        return normalized_managed
    if managed_conversation_state == "open":
        return "idle"
    if normalized_direct is not None:
        return normalized_direct
    normalized_managed_conversation = strongest_work_execution_status([managed_conversation_state])
    if normalized_managed_conversation is not None:
        return normalized_managed_conversation
    if normalized_step is not None:
        return normalized_step
    normalized_task = strongest_work_execution_status([task_status])
    if normalized_task is not None:
        return normalized_task
    if normalized_session is not None:
        return normalized_session
    return None


def _typed_lineage_ids(row: Conversation) -> dict[str, set[str]]:
    result: dict[str, set[str]] = {
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
        statement: Any,
        *,
        identity: Any,
        limit: int | None = None,
    ) -> list[Any]:
        nonlocal queries, truncated
        if queries >= WORK_GRAPH_MAX_QUERIES:
            truncated = True
            return []
        remaining = max(0, WORK_GRAPH_MAX_RESULTS - len(unique_results))
        if remaining == 0:
            truncated = True
            return []
        requested = min(limit or remaining, remaining)
        query = db.scalars(
            statement.order_by(identity(entity)).limit(
                requested if limit is not None else requested + 1
            )
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
        accepted: list[Any] = []
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
    cross_scope_active_ids: set[str] = set()
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
                if row.conversation_id == identifier
                and row.user_email == user_email
                and row.status != "deleted"
            ),
            None,
        )

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
            (row for row in rows if row.session_id == identifier and row.user_email == user_email),
            None,
        )

    if scope.kind == "conversation" and scope.conversation_id:
        root_conversation = await exact_conversation(scope.conversation_id)
        if root_conversation is None:
            raise AuthorizedWorkRootNotReadyError("Authorized Work conversation root was not found")
        conversations[root_conversation.conversation_id] = root_conversation
        root_session: Session | None = None
        if root_conversation.active_session_id:
            active_rows = await fetch(
                Session,
                select(Session).where(
                    Session.user_email == user_email,
                    Session.session_id == root_conversation.active_session_id,
                ),
                identity=session_id,
                limit=1,
            )
            candidate = active_rows[0] if active_rows else None
            if (
                candidate is not None
                and candidate.conversation_id == root_conversation.conversation_id
            ):
                root_session = candidate
        if root_session is None:
            candidates = await fetch(
                Session,
                select(Session)
                .where(
                    Session.user_email == user_email,
                    Session.conversation_id == root_conversation.conversation_id,
                )
                .order_by(
                    Session.updated_at.desc(),
                    Session.started_at.desc(),
                    Session.session_id.desc(),
                ),
                identity=session_id,
                limit=1,
            )
            root_session = candidates[0] if candidates else None
        if root_session is None:
            return _empty_authorized_work_graph()
        root_session_id = root_session.session_id
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
            merge_work_session_edge(
                session_parent,
                session_id=current.session_id,
                parent_session_id=None if current.session_id == root_session_id else previous_id,
                edge_kind="root" if current.session_id == root_session_id else "rotation",
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
        merge_work_session_edge(
            session_parent,
            session_id=root_session.session_id,
            parent_session_id=None,
            edge_kind="root",
        )
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
                Task.task_id == root_step.task_id,
            ),
            identity=task_id,
            limit=1,
        )
        if not root_tasks:
            raise ValueError("Authorized Work task root was not found")
        steps[root_step.step_run_id] = root_step
        tasks[root_tasks[0].task_id] = root_tasks[0]
        queue.append(_Frontier("step", root_step.step_run_id, None, "root"))
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
            merge_work_session_edge(
                session_parent,
                session_id=row.session_id,
                parent_session_id=frontier.parent_session_id,
                edge_kind=frontier.edge_kind,
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
                    if (edge_kind == "delegate" and child.parent_session_id != row.session_id) or (
                        edge_kind == "rotation" and child.previous_session_id != row.session_id
                    ):
                        continue
                    if (
                        row.session_id in cross_scope_active_ids
                        and child.status not in _LIVE_DESCENDANT_SESSION_STATUSES
                    ):
                        continue
                    queue.append(
                        _Frontier(
                            "session",
                            child.session_id,
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
                if link.user_email != user_email or link.controller_session_id != row.session_id:
                    continue
                links[link.link_id] = link
                queue.append(
                    _Frontier(
                        "conversation",
                        link.target_conversation_id,
                        row.session_id,
                        "managed",
                    )
                )
        elif frontier.kind == "conversation":
            conversation_row = conversations.get(frontier.identifier) or await exact_conversation(
                frontier.identifier
            )
            if conversation_row is None:
                continue
            conversations[conversation_row.conversation_id] = conversation_row
            active_session = (
                await exact_session(conversation_row.active_session_id)
                if conversation_row.active_session_id
                else None
            )
            if conversation_row.active_session_id:
                queue.append(
                    _Frontier(
                        "session",
                        conversation_row.active_session_id,
                        frontier.parent_session_id,
                        frontier.edge_kind,
                    )
                )
            if active_session is not None:
                predecessor_ids: set[str] = set()
                predecessor_id = active_session.previous_session_id
                for _ in range(WORK_GRAPH_MAX_DEPTH):
                    if not predecessor_id or predecessor_id in predecessor_ids:
                        break
                    predecessor_ids.add(predecessor_id)
                    predecessor = await exact_session(predecessor_id)
                    if (
                        predecessor is None
                        or predecessor.conversation_id != conversation_row.conversation_id
                    ):
                        break
                    predecessor_id = predecessor.previous_session_id
                else:
                    if predecessor_id:
                        truncated = True
                lineage_ids = {active_session.session_id, *predecessor_ids}
                active_candidates = await fetch(
                    Session,
                    select(Session).where(
                        Session.user_email == user_email,
                        Session.conversation_id == conversation_row.conversation_id,
                        Session.status.in_(_LIVE_DESCENDANT_SESSION_STATUSES),
                        Session.parent_session_id.in_(lineage_ids),
                    ),
                    identity=session_id,
                )
                cross_scope_active_ids.update(row.session_id for row in active_candidates)
                for child in active_candidates:
                    if not child.parent_session_id:
                        continue
                    queue.append(
                        _Frontier(
                            "session",
                            child.session_id,
                            active_session.session_id,
                            "delegate",
                        )
                    )
            for link in await fetch(
                ManagedConversationLink,
                select(ManagedConversationLink).where(
                    ManagedConversationLink.user_email == user_email,
                    ManagedConversationLink.controller_conversation_id
                    == conversation_row.conversation_id,
                ),
                identity=link_id,
            ):
                if (
                    link.user_email != user_email
                    or link.controller_conversation_id != conversation_row.conversation_id
                ):
                    continue
                links[link.link_id] = link
                queue.append(
                    _Frontier(
                        "conversation",
                        link.target_conversation_id,
                        getattr(link, "controller_session_id", None),
                        "managed",
                    )
                )
            for task in await fetch(
                Task,
                select(Task).where(
                    Task.created_by == user_email,
                    Task.source_ref == conversation_row.conversation_id,
                ),
                identity=task_id,
            ):
                if (
                    task.created_by != user_email
                    or task.source_ref != conversation_row.conversation_id
                ):
                    continue
                source_id = task.source_session_id
                source_session = await exact_session(source_id) if source_id else None
                if (
                    active_session is None
                    or source_session is None
                    or source_session.conversation_id != conversation_row.conversation_id
                    or source_session.activity_scope_id != active_session.activity_scope_id
                ):
                    continue
                queue.append(_Frontier("task", task.task_id, None, "task"))
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
                    if row.task_id == frontier.identifier and row.created_by == user_email
                ),
                None,
            )
            if task is None:
                continue
            tasks[task.task_id] = task
            if task.control_conversation_id:
                queue.append(_Frontier("conversation", task.control_conversation_id, None, "task"))
            for step in await fetch(
                StepRun,
                select(StepRun).where(StepRun.task_id == task.task_id),
                identity=step_id,
            ):
                if step.task_id != task.task_id:
                    continue
                queue.append(
                    _Frontier(
                        "step",
                        step.step_run_id,
                        task.source_session_id,
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
                (row for row in step_rows if row.step_run_id == frontier.identifier),
                None,
            )
            if step is None:
                continue
            task_rows = await fetch(
                Task,
                select(Task).where(
                    Task.created_by == user_email,
                    Task.task_id == step.task_id,
                ),
                identity=task_id,
                limit=1,
            )
            task = next(
                (
                    row
                    for row in task_rows
                    if row.task_id == step.task_id and row.created_by == user_email
                ),
                None,
            )
            if task is None:
                continue
            steps[step.step_run_id] = step
            tasks[task.task_id] = task
            if step.session_id:
                if root_session_id is None:
                    root_session_id = step.session_id
                queue.append(
                    _Frontier(
                        "session",
                        step.session_id,
                        frontier.parent_session_id,
                        "task_step",
                    )
                )
            if step.superseded_by_step_run_id:
                queue.append(
                    _Frontier(
                        "step",
                        step.superseded_by_step_run_id,
                        frontier.parent_session_id,
                        "retry",
                    )
                )

    if queue:
        truncated = True
    selected = canonical_work_session_order(
        sessions.values(), session_parent=session_parent, max_nodes=max_nodes
    )
    if root_session_id is None:
        raise ValueError("Authorized Work graph root session was not resolved")
    nodes = canonical_workstream_refs(
        selected,
        session_parent=session_parent,
        root_session_id=root_session_id,
        steps=steps.values(),
        tasks=tasks,
        links=links.values(),
        conversations=conversations,
    )
    fingerprint = canonical_work_graph_fingerprint(nodes)
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
        statement: Any,
        identity: Any,
        *,
        limit: int | None = None,
    ) -> list[Any]:
        nonlocal queries, truncated
        if queries >= WORK_GRAPH_MAX_QUERIES:
            truncated = True
            return []
        remaining = WORK_GRAPH_MAX_RESULTS - len(results_seen)
        if remaining <= 0:
            truncated = True
            return []
        requested = min(limit or remaining, remaining)
        awaitable = db.scalars(
            statement.order_by(identity).limit(requested if limit == 1 else requested + 1)
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
        if len(rows) > requested:
            truncated = True
            rows = rows[:requested]
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
                    limit=max_nodes if entity is Session and column is not identity else None,
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
    loaded_scope_conversations: set[str] = set()
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
        if root is None:
            raise AuthorizedWorkRootNotReadyError("Authorized Work conversation root was not found")
        conversations[root.conversation_id] = root
        active_rows: list[Session] = []
        if root.active_session_id:
            active_rows = await fetch(
                Session,
                select(Session).where(
                    Session.user_email == user_email,
                    Session.session_id == root.active_session_id,
                ),
                Session.session_id,
            )
            active_rows = [
                row for row in active_rows if row.conversation_id == root.conversation_id
            ]
        root_session = active_rows[0] if active_rows else None
        fallback_rows: list[Session] = []
        if root_session is None:
            fallback_rows = await fetch(
                Session,
                select(Session)
                .where(
                    Session.user_email == user_email,
                    Session.conversation_id == root.conversation_id,
                )
                .order_by(
                    Session.updated_at.desc(),
                    Session.started_at.desc(),
                    Session.session_id.desc(),
                ),
                Session.session_id,
                limit=1,
            )
            root_session = fallback_rows[0] if fallback_rows else None
        if root_session is None:
            return _empty_authorized_work_graph()
        root_session_id = root_session.session_id
        if not active_rows:
            active_rows = [root_session]
        active_scope_id = root_session.activity_scope_id
        scope_by_conversation[root.conversation_id] = active_scope_id
        rotation_rows = await fetch(
            Session,
            select(Session).where(
                Session.user_email == user_email,
                Session.conversation_id == root.conversation_id,
                (
                    Session.activity_scope_id == active_scope_id
                    if active_scope_id is not None
                    else Session.session_id.in_(
                        [row.session_id for row in [*active_rows, *fallback_rows]]
                    )
                ),
            ),
            Session.session_id,
        )
        rotation_by_id = {row.session_id: row for row in [*active_rows, *rotation_rows]}
        if active_scope_id is not None:
            loaded_scope_conversations.add(root.conversation_id)
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
            merge_work_session_edge(
                session_parent,
                session_id=row.session_id,
                parent_session_id=None
                if row.session_id == root_session_id
                else row.previous_session_id,
                edge_kind="root" if row.session_id == root_session_id else "rotation",
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
        merge_work_session_edge(
            session_parent,
            session_id=root.session_id,
            parent_session_id=None,
            edge_kind="root",
        )
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
            merge_work_session_edge(
                session_parent,
                session_id=current.session_id,
                parent_session_id=previous.session_id,
                edge_kind="rotation",
            )
            merge_work_session_edge(
                session_parent,
                session_id=previous.session_id,
                parent_session_id=previous.previous_session_id,
                edge_kind="rotation",
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
        # Resolve child conversations before spending the remaining read budget
        # on backing-history expansion. The output cap is applied with ancestors.
        history = (
            [item for item in layer if item.kind == "session" and item.edge_kind == "rotation"]
            if any(item.kind == "conversation" for item in layer)
            else []
        )
        layer = [item for item in layer if item not in history]
        frontier = history
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
                if row.conversation_id in scope_by_conversation and same_work_activity_scope(
                    scope_by_conversation[row.conversation_id],
                    row.activity_scope_id,
                ):
                    current_scope_session_ids.add(row.session_id)
                merge_work_session_edge(
                    session_parent,
                    session_id=row.session_id,
                    parent_session_id=item.parent_session_id,
                    edge_kind=item.edge_kind,
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
            newly_reached_scopes: dict[str, str] = {}
            for row in sessions.values():
                if (
                    row.session_id in session_ids
                    and row.activity_scope_id is not None
                    and row.conversation_id not in loaded_scope_conversations
                ):
                    canonical_scope_id = scope_by_conversation.setdefault(
                        row.conversation_id,
                        row.activity_scope_id,
                    )
                    newly_reached_scopes.setdefault(
                        row.conversation_id,
                        canonical_scope_id,
                    )
            if newly_reached_scopes:
                scoped_sessions = await fetch(
                    Session,
                    select(Session).where(
                        Session.user_email == user_email,
                        or_(
                            *[
                                and_(
                                    Session.conversation_id == conversation_id,
                                    Session.activity_scope_id == scope_id,
                                )
                                for conversation_id, scope_id in newly_reached_scopes.items()
                            ]
                        ),
                    ),
                    Session.session_id,
                    limit=max_nodes,
                )
                loaded_scope_conversations.update(newly_reached_scopes)
                current_scope_session_ids.update(row.session_id for row in scoped_sessions)
                for scoped in scoped_sessions:
                    frontier.append(
                        _Frontier(
                            "session",
                            scoped.session_id,
                            scoped.previous_session_id or scoped.parent_session_id,
                            "rotation" if scoped.previous_session_id else "delegate",
                        )
                    )
            rotation_session_ids = {
                row.session_id
                for row in sessions.values()
                if row.session_id in session_ids
                and row.conversation_id not in loaded_scope_conversations
            }
            for column, edge_kind, parent_ids in (
                (Session.parent_session_id, "delegate", session_ids),
                (Session.previous_session_id, "rotation", rotation_session_ids),
            ):
                if not parent_ids:
                    continue
                children = await fetch_chunks(
                    Session,
                    parent_ids,
                    column,
                    Session.session_id,
                    Session.user_email == user_email,
                )
                for child in children:
                    if (
                        child.conversation_id in scope_by_conversation
                        and not same_work_activity_scope(
                            scope_by_conversation[child.conversation_id],
                            child.activity_scope_id,
                        )
                    ):
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
            root_session_by_conversation: dict[str, Session] = {}
            for active in active_sessions:
                conversation = conversations.get(active.conversation_id)
                if (
                    conversation is None
                    or conversation.conversation_id not in conversation_ids
                    or conversation.active_session_id != active.session_id
                    or active.conversation_id != conversation.conversation_id
                ):
                    continue
                root_session_by_conversation[active.conversation_id] = active
            fallback_conversation_ids = conversation_ids - set(root_session_by_conversation)
            if fallback_conversation_ids:
                fallback_sessions = await fetch(
                    Session,
                    _latest_session_per_conversation_statement(
                        user_email=user_email,
                        conversation_ids=fallback_conversation_ids,
                    ),
                    Session.session_id,
                )
                for session in fallback_sessions:
                    root_session_by_conversation[session.conversation_id] = session
            root_ids = {row.session_id for row in root_session_by_conversation.values()}
            active_sessions = [
                *[
                    row
                    for row in active_sessions
                    if root_session_by_conversation.get(row.conversation_id) is row
                ],
                *[
                    row
                    for row in root_session_by_conversation.values()
                    if row.session_id not in active_ids
                ],
            ]
            active_ids = root_ids
            for active in active_sessions:
                scope_by_conversation[active.conversation_id] = active.activity_scope_id
            predecessor_ids_by_conversation: dict[str, set[str]] = {
                conversation_id: set() for conversation_id in conversation_ids
            }
            pending_predecessors = {
                row.previous_session_id: row.conversation_id
                for row in active_sessions
                if row.previous_session_id
            }
            for _ in range(WORK_GRAPH_MAX_DEPTH):
                if not pending_predecessors:
                    break
                predecessor_rows = await fetch_chunks(
                    Session,
                    set(pending_predecessors),
                    Session.session_id,
                    Session.session_id,
                    Session.user_email == user_email,
                )
                next_predecessors: dict[str, str] = {}
                for predecessor in predecessor_rows:
                    conversation_id = pending_predecessors.get(predecessor.session_id)
                    if conversation_id is None or predecessor.conversation_id != conversation_id:
                        continue
                    predecessor_ids_by_conversation[conversation_id].add(predecessor.session_id)
                    if (
                        predecessor.previous_session_id
                        and predecessor.previous_session_id
                        not in predecessor_ids_by_conversation[conversation_id]
                    ):
                        next_predecessors[predecessor.previous_session_id] = conversation_id
                pending_predecessors = next_predecessors
            if pending_predecessors:
                truncated = True
            cross_scope_parent_aliases: dict[str, str] = {}
            active_descendant_candidates: list[Session] = []
            root_by_conversation = {row.conversation_id: row for row in active_sessions}
            parent_conversation = {
                session_id: conversation_id
                for conversation_id, predecessor_ids in predecessor_ids_by_conversation.items()
                for session_id in predecessor_ids
            }
            parent_conversation.update(
                {row.session_id: row.conversation_id for row in active_sessions}
            )
            pending_live_parents = set(parent_conversation)
            visited_live_parents: set[str] = set()
            for _ in range(WORK_GRAPH_MAX_DEPTH):
                query_parents = pending_live_parents - visited_live_parents
                if not query_parents:
                    pending_live_parents = set()
                    break
                visited_live_parents.update(query_parents)
                children = await fetch_chunks(
                    Session,
                    query_parents,
                    Session.parent_session_id,
                    Session.session_id,
                    Session.user_email == user_email,
                    Session.status.in_(_LIVE_DESCENDANT_SESSION_STATUSES),
                )
                next_live_parents: set[str] = set()
                for child in children:
                    parent_id = child.parent_session_id
                    conversation_id = parent_conversation.get(parent_id or "")
                    active = root_by_conversation.get(conversation_id or "")
                    if active is None or child.conversation_id != conversation_id:
                        continue
                    parent_conversation[child.session_id] = child.conversation_id
                    if same_work_activity_scope(
                        active.activity_scope_id,
                        child.activity_scope_id,
                    ):
                        continue
                    next_live_parents.add(child.session_id)
                    lineage_ids = {
                        active.session_id,
                        *predecessor_ids_by_conversation.get(
                            active.conversation_id,
                            set(),
                        ),
                    }
                    cross_scope_parent_aliases[child.session_id] = (
                        active.session_id if parent_id in lineage_ids else parent_id
                    )
                    active_descendant_candidates.append(child)
                pending_live_parents = next_live_parents
            if pending_live_parents:
                truncated = True
            scope_predicates = [
                and_(
                    Session.conversation_id == conversation_id,
                    (
                        Session.activity_scope_id == scope_id
                        if scope_id is not None
                        else Session.session_id
                        == root_session_by_conversation[conversation_id].session_id
                    ),
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
                    limit=max_nodes,
                )
                if scope_predicates
                else []
            )
            scoped_by_id = {
                row.session_id: row
                for row in [
                    *active_sessions,
                    *scoped_sessions,
                    *active_descendant_candidates,
                ]
                if row.session_id in root_ids
                or (
                    scope_by_conversation.get(row.conversation_id) is not None
                    and same_work_activity_scope(
                        scope_by_conversation[row.conversation_id],
                        row.activity_scope_id,
                    )
                )
                or row.session_id in cross_scope_parent_aliases
            }
            scoped_sessions = list(scoped_by_id.values())
            scoped_ids = {row.session_id for row in scoped_sessions}
            loaded_scope_conversations.update(
                conversation_id
                for conversation_id in conversation_ids
                if scope_by_conversation.get(conversation_id) is not None
            )
            current_scope_session_ids.update(scoped_ids)
            for item in conversation_frontier:
                row = conversations.get(item.identifier)
                if row and row.conversation_id in root_session_by_conversation:
                    root_session = root_session_by_conversation[row.conversation_id]
                    for scoped in scoped_sessions:
                        if scoped.conversation_id != row.conversation_id:
                            continue
                        parent_id = cross_scope_parent_aliases.get(scoped.session_id) or (
                            item.parent_session_id
                            if scoped.session_id == root_session.session_id
                            else (scoped.previous_session_id or scoped.parent_session_id)
                        )
                        edge_kind = (
                            item.edge_kind
                            if scoped.session_id == root_session.session_id
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

    selected = canonical_work_session_order(
        sessions.values(),
        session_parent=session_parent,
        max_nodes=max_nodes,
        current_session_ids=frozenset(
            conversation.active_session_id
            for conversation in conversations.values()
            if conversation.active_session_id
        ),
        root_session_id=root_session_id,
    )
    if root_session_id is None:
        raise ValueError("Authorized Work graph root session was not resolved")
    selected_ids = {row.session_id for row in selected}
    omitted_current_conversations = {
        link.target_conversation_id
        for link in links.values()
        if (conversation := conversations.get(link.target_conversation_id))
        and conversation.active_session_id
        and conversation.active_session_id not in selected_ids
        and link.target_conversation_id != sessions[root_session_id].conversation_id
    }
    # Never publish only the settled predecessor of an omitted managed owner.
    # Drop its dependent subtree as well rather than inventing a new parent.
    removed_ids = {
        row.session_id for row in selected if row.conversation_id in omitted_current_conversations
    }
    while True:
        dependent_ids = {
            row.session_id
            for row in selected
            if session_parent.get(row.session_id, (None, "root"))[0] in removed_ids
        }
        if dependent_ids <= removed_ids:
            break
        removed_ids.update(dependent_ids)
    if removed_ids:
        truncated = True
        selected = [row for row in selected if row.session_id not in removed_ids]
    nodes = canonical_workstream_refs(
        selected,
        session_parent=session_parent,
        root_session_id=root_session_id,
        steps=steps.values(),
        tasks=tasks,
        links=links.values(),
        conversations=conversations,
    )
    fingerprint = canonical_work_graph_fingerprint(nodes)
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
    wanted: dict[str, set[str]] = {
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

    async def fetch(statement: Any) -> list[Any]:
        nonlocal queries, results_seen, truncated
        if queries >= WORK_GRAPH_MAX_QUERIES or results_seen >= WORK_GRAPH_MAX_RESULTS:
            truncated = True
            return []
        remaining = WORK_GRAPH_MAX_RESULTS - results_seen
        rows = list((await db.scalars(statement.limit(remaining + 1))).all())
        queries += 1
        if len(rows) > remaining:
            truncated = True
            rows = rows[:remaining]
        results_seen += len(rows)
        return rows

    async def fetch_exact(statement: Any) -> list[Any]:
        nonlocal queries, results_seen, truncated
        if queries >= WORK_GRAPH_MAX_QUERIES or results_seen >= WORK_GRAPH_MAX_RESULTS:
            truncated = True
            return []
        rows = list((await db.scalars(statement.limit(1))).all())
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
        conversation_rows[root_conversation.conversation_id] = root_conversation
        if root_conversation.active_session_id:
            root_sessions = await fetch_exact(
                select(Session).where(
                    Session.user_email == user_email,
                    Session.session_id == root_conversation.active_session_id,
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
            session_rows[root_session.session_id] = root_session
            wanted["session"].add(root_session.session_id)
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
        session_rows[root_session.session_id] = root_session
        wanted["conversation"].add(root_session.conversation_id)
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
                Task.task_id == root_step.task_id,
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
        step_rows[root_step.step_run_id] = root_step
        task_rows[root_task.task_id] = root_task
        if root_step.session_id:
            root_sessions = await fetch_exact(
                select(Session).where(
                    Session.user_email == user_email,
                    Session.session_id == root_step.session_id,
                )
            )
            if root_sessions:
                session_rows[root_sessions[0].session_id] = root_sessions[0]

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
                conversation_rows[row.conversation_id] = row
        for predicate in session_queries:
            for row in await fetch(
                select(Session).where(Session.user_email == user_email, predicate)
            ):
                session_rows[row.session_id] = row
        for predicate in link_queries:
            for row in await fetch(
                select(ManagedConversationLink).where(
                    ManagedConversationLink.user_email == user_email,
                    predicate,
                )
            ):
                link_rows[row.link_id] = row
        for predicate in task_queries:
            for row in await fetch(select(Task).where(Task.created_by == user_email, predicate)):
                task_rows[row.task_id] = row
        for predicate in step_queries:
            for row in await fetch(select(StepRun).where(predicate)):
                step_rows[row.step_run_id] = row
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
            current_task = task_by_id.get(identifier)
            if current_task is None or identifier in included_tasks:
                continue
            included_tasks.add(identifier)
            if current_task.control_conversation_id:
                queue.append(("conversation", current_task.control_conversation_id))
            for task_step in steps:
                if task_step.task_id == identifier:
                    queue.append(("step", task_step.step_run_id))
            for child_conversation in conversations:
                if getattr(child_conversation, "lineage_task_id", None) == identifier:
                    queue.append(("conversation", child_conversation.conversation_id))
        elif kind == "step":
            current_step = step_by_id.get(identifier)
            if current_step is None or identifier in included_steps:
                continue
            included_steps.add(identifier)
            if current_step.session_id:
                queue.append(("session", current_step.session_id))
            if current_step.superseded_by_step_run_id:
                queue.append(("step", current_step.superseded_by_step_run_id))
            for child_conversation in conversations:
                if getattr(child_conversation, "lineage_step_run_id", None) == identifier:
                    queue.append(("conversation", child_conversation.conversation_id))

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
        session_step = step_for_session.get(row.session_id)
        session_link = managed_for_conversation.get(row.conversation_id)
        session_conversation = conversation_by_id.get(row.conversation_id)
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
        elif session_link is not None:
            parent_key = keys.get(getattr(session_link, "controller_session_id", None) or "")
            edge_kind = "managed"
            kind = "managed"
        elif session_step is not None:
            session_task = task_by_id.get(session_step.task_id)
            if scope.kind == "conversation" and session_task is not None:
                parent_key = keys.get(session_task.source_session_id or "")
            edge_kind = "task_step"
            kind = "task_step"
        title = (
            (getattr(session_link, "title", None) if session_link else None)
            or (session_step.step_name if session_step else None)
            or row.delegation_task
            or (session_conversation.title if session_conversation else None)
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
                task_id=session_step.task_id if session_step else None,
                step_run_id=session_step.step_run_id if session_step else None,
                link_id=session_link.link_id if session_link else None,
                title=title,
                agent_id=row.agent_id,
                agent_profile_id=row.agent_profile_id,
                status=(session_step.status if session_step else row.status) or "unknown",
                attempt=session_step.attempt_number if session_step else None,
                step_name=session_step.step_name if session_step else None,
                created_at=row.started_at.isoformat() if row.started_at else None,
                updated_at=row.updated_at.isoformat() if row.updated_at else None,
                current=(
                    session_conversation.active_session_id == row.session_id
                    if session_conversation
                    else False
                ),
                superseded=bool(session_step and session_step.superseded_by_step_run_id),
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
