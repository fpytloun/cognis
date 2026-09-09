"""PostgreSQL-only reads for the durable Work projection."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any, Literal, cast

from prometheus_client import Histogram
from sqlalchemy import and_, case, func, literal, literal_column, or_, select, union_all
from sqlalchemy.ext.asyncio import AsyncSession

from cognis.api.chat_v2.schemas import (
    ActivityOverviewDetail,
    ActivityOverviewResponse,
    ActivityRecentItem,
    ActivityRecentWork,
    ArtifactTimelineItem,
    FileDiffRef,
    FilePreviewOmissionReason,
    TimelineItem,
    TimelineScope,
    TodoProgress,
    ToolCallTimelineItem,
    WorkCategory,
    WorkMaterialization,
    WorkstreamRef,
    WorkSummary,
)
from cognis.api.chat_v2.sync import current_projection_version
from cognis.api.chat_v2.work_graph import ExecutionState, derive_work_execution_state
from cognis.api.chat_v2.work_materializer import (
    WORK_MATERIALIZER_VERSION,
    _decode_persisted_work_item,
)
from cognis.api.chat_v2.work_projection import build_work_projection
from cognis.core.agent_registry import SYSTEM_AGENTS
from cognis.core.managed_conversations import project_managed_conversation_state
from cognis.models.tool import ToolDefinition
from cognis.providers.llm.reasoning import normalize_reasoning_effort
from cognis.store.models import (
    Agent,
    ArtifactRecordRow,
    ConversationTodo,
    DirectTurnRequestRow,
    ManagedConversationLink,
    Session,
    SessionTodo,
    StepRun,
    Task,
    WorkCurrentFileRow,
    WorkRecordFileRow,
    WorkRecordRow,
    WorkSessionProjectionRow,
)


def _preview_omission_reason(payload: Mapping[str, Any]) -> FilePreviewOmissionReason | None:
    value = payload.get("preview_omission_reason")
    if value in {"not_persisted", "retention_expired", "projection_budget", "sensitive"}:
        return cast(FilePreviewOmissionReason, value)
    return "not_persisted" if payload.get("preview_omitted") else None


ACTIVITY_OVERVIEW_COMMAND_PREVIEW_MAX_BYTES = 4 * 1024
WORK_FILES_SOURCE_RECORD_BATCH_SIZE = 500

# Files is a complete initial-tree projection: it must never be row-limited or
# paginated into a partial set. These cardinality metrics are the operational
# safety contract; DB and response work may grow with unique path identities
# (including rename-chain members), never with mutation-history length.
WORK_FILES_UNIQUE_PATHS = Histogram(
    "cognis_work_files_unique_paths",
    "Unique paths returned by a complete Work Files projection",
    buckets=(0, 10, 50, 100, 500, 1000, 5000, 10_000, 25_000, 50_000),
)
WORK_FILES_SOURCE_RECORDS = Histogram(
    "cognis_work_files_source_records",
    "Deduplicated source records hydrated by a Work Files projection",
    buckets=(0, 10, 50, 100, 500, 1000, 5000, 10_000, 25_000, 50_000),
)


class WorkCursorError(ValueError):
    """Raised when a Work cursor is invalid for the current authorized graph."""


@dataclass(frozen=True)
class WorkDatabasePage:
    scope: TimelineScope
    projection_version: str
    items: list[TimelineItem]
    removed_call_ids: list[str]
    materialization: WorkMaterialization
    has_more_before: bool
    before_cursor: str | None
    server_time: str
    detail: ActivityOverviewDetail = "full"
    summary: WorkSummary | None = None
    category: WorkCategory | None = None


@dataclass(frozen=True)
class _LogicalProjection:
    workstreams: list[WorkstreamRef]
    physical_to_logical: dict[str, str]
    members_by_logical: dict[str, tuple[str, ...]]
    ambiguous: bool


@dataclass(frozen=True)
class WorkLifecycleProjection:
    workstreams: list[WorkstreamRef]
    direct_turn_rows: list[Any]


def _sign(payload: dict[str, Any], secret: str) -> str:
    body = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    signature = hmac.new(secret.encode(), body, hashlib.sha256).digest()
    return base64.urlsafe_b64encode(body + signature).decode().rstrip("=")


def _unsign(value: str, secret: str) -> dict[str, Any]:
    try:
        raw = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
        body, signature = raw[:-32], raw[-32:]
        expected = hmac.new(secret.encode(), body, hashlib.sha256).digest()
        if not hmac.compare_digest(signature, expected):
            raise WorkCursorError("invalid Work cursor signature")
        payload = json.loads(body)
    except WorkCursorError:
        raise
    except Exception as exc:
        raise WorkCursorError("invalid Work cursor") from exc
    if not isinstance(payload, dict):
        raise WorkCursorError("invalid Work cursor")
    return payload


async def read_work_projection_states(
    db: AsyncSession, *, rows: list[Session]
) -> list[WorkSessionProjectionRow]:
    owner_email = rows[0].user_email if rows else ""
    return list(
        (
            await db.scalars(
                select(WorkSessionProjectionRow)
                .where(
                    WorkSessionProjectionRow.owner_email == owner_email,
                    WorkSessionProjectionRow.session_id.in_(
                        [item.session_id for item in rows] or [""]
                    ),
                    WorkSessionProjectionRow.materializer_version == WORK_MATERIALIZER_VERSION,
                )
                .order_by(WorkSessionProjectionRow.session_id)
            )
        ).all()
    )


def _materialization(
    states: list[WorkSessionProjectionRow],
    *,
    total: int | None = None,
) -> WorkMaterialization:
    total = len(states) if total is None else total
    completed = sum(
        state.state == "caught_up" and state.covered_through_seq >= state.target_seq
        for state in states
    )
    failed = sum(state.state == "failed" for state in states)
    missing = max(0, total - len(states))
    status: Literal["live", "catching_up", "partial", "failed"]
    if failed == total and total:
        status = "failed"
    elif failed or missing or any(state.state == "source_unavailable" for state in states):
        status = "partial"
    elif completed == total:
        status = "live"
    else:
        status = "catching_up"
    retry_dates = [state.next_retry_at for state in states if state.next_retry_at is not None]
    retry_after_ms = None
    if retry_dates:
        retry_at = min(retry_dates)
        if retry_at.tzinfo is None:
            retry_at = retry_at.replace(tzinfo=UTC)
        retry_after_ms = max(0, int((retry_at - datetime.now(UTC)).total_seconds() * 1000))
    return WorkMaterialization(
        state=status,
        completed_streams=completed,
        total_streams=total,
        covered_events=sum(state.covered_through_seq for state in states),
        target_events=sum(state.target_seq for state in states),
        failed_streams=failed + missing,
        retry_after_ms=retry_after_ms,
    )


def _projection_summaries(
    states: Sequence[WorkSessionProjectionRow],
    *,
    session_ids: Sequence[str],
    path_ids_by_session: Mapping[str, set[str]],
) -> dict[str, WorkSummary]:
    """Build lightweight summaries from the durable per-session counters."""

    states_by_session = {state.session_id: state for state in states}
    summaries: dict[str, WorkSummary] = {}
    for session_id in session_ids:
        state = states_by_session.get(session_id)
        if state is None:
            summaries[session_id] = _empty_summary()
            continue
        summaries[session_id] = WorkSummary(
            mutations=int(state.mutation_count),
            commands=int(state.command_count),
            changed_files=len(path_ids_by_session.get(session_id, set())),
            artifacts=int(state.artifact_count),
            deliverables=int(state.deliverable_count),
            additions=int(state.additions),
            deletions=int(state.deletions),
            omitted_files=int(state.omitted_file_count),
        )
    return summaries


async def _current_file_path_ids(
    db: AsyncSession,
    *,
    owner_email: str,
    session_ids: Sequence[str],
) -> dict[str, set[str]]:
    """Read distinct current file paths for all physical sessions in one query."""

    rows = (
        await db.execute(
            select(WorkCurrentFileRow.session_id, WorkCurrentFileRow.path_id)
            .where(
                WorkCurrentFileRow.owner_email == owner_email,
                WorkCurrentFileRow.session_id.in_(list(session_ids) or [""]),
                WorkCurrentFileRow.materializer_version == WORK_MATERIALIZER_VERSION,
                WorkCurrentFileRow.state.not_in(["delete", "deleted", "removed"]),
            )
            .group_by(WorkCurrentFileRow.session_id, WorkCurrentFileRow.path_id)
        )
    ).all()
    result: dict[str, set[str]] = {}
    for session_id, path_id in rows:
        result.setdefault(str(session_id), set()).add(str(path_id))
    return result


def _current_execution_status(statuses_newest_first: Sequence[str]) -> ExecutionState | None:
    """Return the oldest admitted durable execution that remains nonterminal."""

    for status in reversed(statuses_newest_first):
        normalized = derive_work_execution_state(direct_turn_status=status)
        if normalized in {"running", "recovering", "waiting", "queued"}:
            return normalized
    return None


async def enrich_workstream_lifecycle(
    db: AsyncSession,
    *,
    owner_email: str,
    session_rows: Sequence[Session],
    workstreams: Sequence[WorkstreamRef],
) -> WorkLifecycleProjection:
    """Project identity-scoped durable lifecycle evidence onto logical Work nodes."""

    if not session_rows or not workstreams:
        return WorkLifecycleProjection(workstreams=list(workstreams), direct_turn_rows=[])
    session_ids = {row.session_id for row in session_rows}
    conversation_ids = {row.conversation_id for row in session_rows}
    ranked_direct = (
        select(
            DirectTurnRequestRow.session_id,
            DirectTurnRequestRow.status,
            DirectTurnRequestRow.payload,
            DirectTurnRequestRow.admission_order,
            DirectTurnRequestRow.turn_id,
            DirectTurnRequestRow.conversation_id,
            func.row_number()
            .over(
                partition_by=(
                    DirectTurnRequestRow.conversation_id,
                    DirectTurnRequestRow.session_id,
                ),
                order_by=DirectTurnRequestRow.admission_order.desc(),
            )
            .label("ordinal"),
        )
        .where(
            DirectTurnRequestRow.user_id == owner_email,
            DirectTurnRequestRow.conversation_id.in_(conversation_ids),
            or_(
                DirectTurnRequestRow.session_id.in_(session_ids),
                DirectTurnRequestRow.session_id.is_(None),
            ),
        )
        .subquery("ranked_work_lifecycle_turns")
    )
    direct_rows = list(
        (
            await db.execute(
                select(
                    ranked_direct.c.session_id,
                    ranked_direct.c.status,
                    ranked_direct.c.payload,
                    ranked_direct.c.admission_order,
                    ranked_direct.c.turn_id,
                    ranked_direct.c.conversation_id,
                )
                .where(
                    or_(
                        ranked_direct.c.ordinal == 1,
                        ranked_direct.c.status.in_(
                            ["queued", "claimed", "running", "absorbing", "recoverable"]
                        ),
                    ),
                )
                .order_by(ranked_direct.c.admission_order.desc())
            )
        ).all()
    )
    direct_by_turn = {str(row.turn_id): row for row in direct_rows}
    managed_rows = list(
        (
            await db.scalars(
                select(ManagedConversationLink)
                .where(
                    ManagedConversationLink.user_email == owner_email,
                    ManagedConversationLink.target_conversation_id.in_(conversation_ids),
                )
                .order_by(ManagedConversationLink.updated_at.desc())
            )
        ).all()
    )
    step_rows = list(
        (
            await db.execute(
                select(StepRun.session_id, StepRun.status, Task.status)
                .join(Task, Task.task_id == StepRun.task_id)
                .where(StepRun.session_id.in_(session_ids))
                .order_by(
                    StepRun.updated_at.desc(),
                    StepRun.attempt_number.desc(),
                    StepRun.step_run_id.desc(),
                )
            )
        ).all()
    )
    rows_by_session = {row.session_id: row for row in session_rows}
    enriched: list[WorkstreamRef] = []
    for node in workstreams:
        backing_ids = set(node.backing_session_ids) or {node.session_id}
        representative = rows_by_session[node.session_id]
        node_direct_rows = [
            row
            for row in direct_rows
            if row.conversation_id == representative.conversation_id
            and (row.session_id in backing_ids or (row.session_id is None and node.current))
        ]
        direct_active = _current_execution_status([str(row.status) for row in node_direct_rows])
        active_direct_row = next(
            (
                row
                for row in reversed(node_direct_rows)
                if derive_work_execution_state(direct_turn_status=str(row.status))
                in {"running", "recovering", "waiting", "queued"}
            ),
            None,
        )
        direct_latest = (
            derive_work_execution_state(direct_turn_status=str(node_direct_rows[0].status))
            if node_direct_rows
            else None
        )
        managed = next(
            (
                link
                for link in managed_rows
                if link.target_conversation_id == representative.conversation_id
                and (
                    link.target_session_id in backing_ids
                    or (link.target_session_id is None and node.current)
                )
            ),
            None,
        )
        managed_state: ExecutionState | None = None
        if managed is not None:
            projection = project_managed_conversation_state(managed)
            active_turn = (
                direct_by_turn.get(str(projection.active_turn_id))
                if projection.active_turn_id
                else None
            )
            active_turn_status = (
                str(active_turn.status)
                if active_turn is not None
                and active_turn.conversation_id == representative.conversation_id
                and (
                    active_turn.session_id in backing_ids
                    or (active_turn.session_id is None and node.current)
                )
                else None
            )
            if projection.conversation_state == "open":
                if active_turn_status is not None:
                    managed_state = derive_work_execution_state(
                        direct_turn_status=active_turn_status
                    )
                else:
                    managed_outcome = derive_work_execution_state(
                        managed_turn_state=projection.turn_state
                    )
                    managed_state = (
                        managed_outcome
                        if managed_outcome in {"waiting", "failed", "cancelled"}
                        else "idle"
                    )
            else:
                managed_state = derive_work_execution_state(
                    managed_conversation_state=projection.conversation_state
                )
        node_steps = [
            (str(step_status), str(task_status))
            for session_id, step_status, task_status in step_rows
            if session_id in backing_ids
        ]
        step_active = _current_execution_status([status for status, _task in node_steps])
        step_latest = (
            derive_work_execution_state(
                step_status=node_steps[0][0],
                task_status=node_steps[0][1],
            )
            if node_steps
            else None
        )
        terminal_session = derive_work_execution_state(session_status=representative.status)
        if terminal_session not in {"completed", "failed", "cancelled"}:
            terminal_session = None
        execution_state: ExecutionState | None
        if terminal_session is not None:
            execution_state = terminal_session
        elif direct_active is not None:
            execution_state = direct_active
        elif managed_state is not None:
            execution_state = managed_state
        elif step_active is not None:
            execution_state = step_active
        elif direct_latest is not None:
            execution_state = direct_latest
        elif step_latest is not None:
            execution_state = step_latest
        else:
            execution_state = derive_work_execution_state(
                session_status=representative.status,
                delegated_session=(
                    representative.parent_session_id is not None
                    and representative.delegation_mode is not None
                ),
            )
        ongoing = execution_state in {"queued", "running", "waiting", "recovering"}
        execution_runtime = (
            representative.delegation_metadata.get("execution_runtime")
            if isinstance(representative.delegation_metadata, dict)
            else None
        )
        valid_execution_runtime = (
            execution_runtime
            if isinstance(execution_runtime, dict)
            and execution_runtime.get("session_id") == representative.session_id
            else None
        )
        enriched.append(
            node.model_copy(
                update={
                    "execution_state": execution_state,
                    "active_turn_id": (
                        str(active_direct_row.turn_id) if active_direct_row is not None else None
                    ),
                    "execution_turn_id": (
                        _nonempty_string(valid_execution_runtime.get("turn_id"))
                        if valid_execution_runtime is not None
                        else None
                    ),
                    "runtime_selection_revision": representative.runtime_override_revision,
                    "runtime_recorded_at": (
                        _nonempty_string(valid_execution_runtime.get("recorded_at"))
                        if valid_execution_runtime is not None
                        else None
                    ),
                    "activity_state": _activity_state(
                        session_status=representative.status,
                        managed_conversation_state=(
                            str(managed.conversation_state) if managed is not None else None
                        ),
                        ongoing=ongoing,
                    ),
                    "activity_scope_id": representative.activity_scope_id,
                    "completion_reason": representative.completion_reason,
                    "completed_at": (
                        representative.completed_at.isoformat()
                        if representative.completed_at
                        else None
                    ),
                }
            )
        )
    return WorkLifecycleProjection(workstreams=enriched, direct_turn_rows=direct_rows)


async def read_activity_overview(
    db: AsyncSession,
    *,
    owner_email: str,
    scope: TimelineScope,
    session_rows: list[Session],
    workstreams: list[WorkstreamRef],
    graph_fingerprint: str,
    graph_truncated: bool,
    work_revision: int = 0,
    graph_revision: int = 0,
    tool_definitions: Mapping[str, ToolDefinition] | None = None,
    session_cache: Any | None = None,
    detail: ActivityOverviewDetail = "lightweight",
) -> ActivityOverviewResponse:
    """Read a bounded PostgreSQL activity overview for one authorized Work graph."""

    session_ids = [row.session_id for row in session_rows]
    base = select(WorkRecordRow).where(
        WorkRecordRow.owner_email == owner_email,
        WorkRecordRow.session_id.in_(session_ids or [""]),
        WorkRecordRow.materializer_version == WORK_MATERIALIZER_VERSION,
        WorkRecordRow.is_evidence.is_(True),
    )
    state_rows = await read_work_projection_states(db, rows=session_rows)
    materialization = _materialization(state_rows, total=len(session_rows))
    if not session_rows:
        empty = _empty_summary()
        overview_revision = _overview_revision(
            graph_fingerprint=graph_fingerprint,
            graph_truncated=graph_truncated,
            materialization=materialization,
            summary=empty,
            workstreams=[],
            logical_membership={},
            recent_records=[],
        )
        return ActivityOverviewResponse(
            detail=detail,
            projection_version=current_projection_version(),
            scope=scope,
            summary=empty,
            materialization=materialization,
            workstreams=[],
            recent={},
            recent_work=ActivityRecentWork(
                commands=[],
                mutations=[],
                files=[],
                artifacts=[],
                deliverables=[],
            ),
            graph_fingerprint=graph_fingerprint,
            work_revision=work_revision,
            graph_revision=graph_revision,
            overview_revision=overview_revision,
            graph_truncated=graph_truncated,
            server_time=datetime.now(UTC).isoformat(),
        )
    path_ids_by_session: dict[str, set[str]] = {}
    if detail == "lightweight":
        path_ids_by_session = await _current_file_path_ids(
            db,
            owner_email=owner_email,
            session_ids=session_ids,
        )
        summaries = _projection_summaries(
            state_rows,
            session_ids=session_ids,
            path_ids_by_session=path_ids_by_session,
        )
    else:
        summaries = await _session_summaries(
            db,
            owner_email=owner_email,
            statement=base,
            session_ids=session_ids,
        )
    lifecycle = await enrich_workstream_lifecycle(
        db,
        owner_email=owner_email,
        session_rows=session_rows,
        workstreams=workstreams,
    )
    workstreams = lifecycle.workstreams
    runtime_by_session = await _session_runtime_metadata(
        db,
        owner_email=owner_email,
        session_rows=session_rows,
        turn_rows=[row[:4] for row in lifecycle.direct_turn_rows],
        session_cache=session_cache,
    )
    enriched: list[WorkstreamRef] = []
    for node in workstreams:
        enriched.append(
            node.model_copy(
                update={
                    "summary": summaries.get(node.session_id, _empty_summary()),
                    "agent_profile_id": runtime_by_session.get(
                        node.session_id, (None, None, node.agent_profile_id, None, None)
                    )[2],
                    "model": runtime_by_session.get(
                        node.session_id, (None, None, None, None, None)
                    )[0],
                    "reasoning_effort": runtime_by_session.get(
                        node.session_id, (None, None, None, None, None)
                    )[1],
                    "agent_display_name": runtime_by_session.get(
                        node.session_id, (None, None, None, None, None)
                    )[3],
                    "agent_avatar_url": runtime_by_session.get(
                        node.session_id, (None, None, None, None, None)
                    )[4],
                }
            )
        )
    logical = _collapse_activity_workstreams(
        enriched,
        session_rows=session_rows,
        physical_root_key=next(
            (node.root_key for node in enriched if node.key == node.root_key),
            enriched[0].root_key if enriched else "",
        ),
    )
    entity_counts: Any = ()
    if detail == "lightweight":
        logical_by_session = {
            physical.removeprefix("session:"): logical_key
            for physical, logical_key in logical.physical_to_logical.items()
        }
        _, entity_statement = _logical_summary_statements(
            statement=base,
            logical_by_session=logical_by_session,
            owner_email=owner_email,
        )
        entity_counts = (await db.execute(entity_statement)).all()
        logical_summaries = _aggregate_cached_logical_summaries(
            summaries,
            logical,
            path_ids_by_session=path_ids_by_session,
            entity_counts=entity_counts,
        )
    else:
        logical_summaries = await _logical_summaries(
            db,
            owner_email=owner_email,
            statement=base,
            physical_summaries=summaries,
            logical=logical,
        )
    logical_workstreams = [
        node.model_copy(update={"summary": logical_summaries.get(node.key, _empty_summary())})
        for node in logical.workstreams
    ]
    todo_progress = await _logical_todo_progress(
        db,
        owner_email=owner_email,
        session_rows=session_rows,
        logical=logical,
    )
    logical_workstreams = [
        node.model_copy(update={"todo_progress": todo_progress.get(node.key)})
        for node in logical_workstreams
    ]
    recent, recent_records, recent_item_overrides = await _recent_activity(
        db,
        owner_email=owner_email,
        session_ids=session_ids,
        statement=base,
    )
    recent_items = await _hydrate_file_items(
        db,
        records=recent_records,
        items=[
            recent_item_overrides.get(record.work_record_id)
            or _decode_persisted_work_item(record.timeline_item)
            for record in recent_records
        ],
    )
    recent_projection = build_work_projection(
        scope=scope,
        projection_version=current_projection_version(),
        items=recent_items,
        tool_definitions=tool_definitions or {},
        has_more_before=False,
        before_cursor=None,
        workstreams=enriched,
        server_time=datetime.now(UTC).isoformat(),
        newest_first=True,
        preserve_input_order=True,
    )
    category_by_item = {record.source_item_id: record.category for record in recent_records}
    recent_work = ActivityRecentWork(
        commands=recent_projection.commands[:10],
        mutations=[
            item
            for item in recent_projection.mutations
            if category_by_item.get(item.id) == "mutations"
        ][:10],
        files=[
            item for item in recent_projection.mutations if category_by_item.get(item.id) == "files"
        ][:10],
        artifacts=recent_projection.artifacts[:10],
        deliverables=recent_projection.deliverables[:10],
    )
    if detail == "lightweight":
        recent_work = _lightweight_recent_work(recent_work)
    if detail == "lightweight":
        aggregate = _sum_summaries(summaries.values()).model_copy(
            update={
                "changed_files": len(
                    {path_id for path_ids in path_ids_by_session.values() for path_id in path_ids}
                ),
                "artifacts": sum(summary.artifacts for summary in logical_summaries.values()),
                "deliverables": sum(summary.deliverables for summary in logical_summaries.values()),
            }
        )
    else:
        aggregate = await _category_summary(db, statement=base, owner_email=owner_email)
    overview_revision = _overview_revision(
        graph_fingerprint=graph_fingerprint,
        graph_truncated=graph_truncated or logical.ambiguous,
        materialization=materialization,
        summary=aggregate,
        workstreams=logical_workstreams,
        logical_membership=logical.members_by_logical,
        recent_records=recent_records,
    )
    return ActivityOverviewResponse(
        detail=detail,
        projection_version=current_projection_version(),
        scope=scope,
        summary=aggregate,
        materialization=materialization,
        workstreams=logical_workstreams,
        recent=recent,
        recent_work=recent_work,
        graph_fingerprint=graph_fingerprint,
        work_revision=work_revision,
        graph_revision=graph_revision,
        overview_revision=overview_revision,
        graph_truncated=graph_truncated or logical.ambiguous,
        server_time=datetime.now(UTC).isoformat(),
    )


def _sum_summaries(values: Any) -> WorkSummary:
    rows = list(values)
    return WorkSummary(
        mutations=sum(row.mutations for row in rows),
        commands=sum(row.commands for row in rows),
        changed_files=sum(row.changed_files for row in rows),
        artifacts=sum(row.artifacts for row in rows),
        deliverables=sum(row.deliverables for row in rows),
        additions=sum(row.additions for row in rows),
        deletions=sum(row.deletions for row in rows),
        omitted_files=sum(row.omitted_files for row in rows),
    )


def _aggregate_cached_logical_summaries(
    summaries: dict[str, WorkSummary],
    logical: _LogicalProjection,
    *,
    path_ids_by_session: dict[str, set[str]],
    entity_counts: Any = (),
) -> dict[str, WorkSummary]:
    artifacts = {
        str(logical_key): int(count)
        for logical_key, category, count in entity_counts
        if category == "artifacts"
    }
    deliverables = {
        str(logical_key): int(count)
        for logical_key, category, count in entity_counts
        if category == "deliverables"
    }
    result: dict[str, WorkSummary] = {}
    for key, members in logical.members_by_logical.items():
        summary = _sum_summaries(
            summaries.get(session_id, _empty_summary()) for session_id in members
        )
        summary = summary.model_copy(
            update={
                "changed_files": len(
                    {
                        path_id
                        for session_id in members
                        for path_id in path_ids_by_session.get(session_id, set())
                    }
                ),
                "artifacts": artifacts.get(key, 0),
                "deliverables": deliverables.get(key, 0),
            }
        )
        result[key] = summary
    return result


def _activity_state(
    *,
    session_status: str,
    managed_conversation_state: str | None,
    ongoing: bool,
) -> str:
    terminal = session_status in {"completed", "failed", "cancelled", "terminated"} or (
        managed_conversation_state in {"closed", "completed", "failed", "cancelled", "terminated"}
    )
    return "closed" if terminal else ("ongoing" if ongoing else "active")


def _is_direct_delegate_ongoing(*, row: Session, node: WorkstreamRef) -> bool:
    """Identify a live direct delegate from its authoritative Session row."""

    return bool(
        row.status == "active"
        and row.parent_session_id
        and (row.delegation_mode or row.delegation_task)
        and (
            (node.kind == "delegate" and node.edge_kind == "delegate")
            or (
                node.kind == "rotation" and node.edge_kind == "rotation" and row.previous_session_id
            )
        )
    )


def _overview_revision(
    *,
    graph_fingerprint: str,
    graph_truncated: bool,
    materialization: WorkMaterialization,
    summary: WorkSummary,
    workstreams: list[WorkstreamRef],
    logical_membership: dict[str, tuple[str, ...]],
    recent_records: list[WorkRecordRow],
) -> str:
    return hashlib.sha256(
        json.dumps(
            {
                "graph": graph_fingerprint,
                "graph_truncated": graph_truncated,
                "materialization": materialization.model_dump(mode="json"),
                "summary": summary.model_dump(mode="json"),
                "workstreams": [node.model_dump(mode="json") for node in workstreams],
                "logical_membership": logical_membership,
                "recent": [
                    (record.work_record_id, record.materialized_at.isoformat())
                    for record in recent_records
                ],
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()


def _bounded_utf8(value: str | None, *, max_bytes: int) -> tuple[str | None, bool]:
    if value is None:
        return None, False
    encoded = value.encode("utf-8")
    if len(encoded) <= max_bytes:
        return value, False
    return encoded[:max_bytes].decode("utf-8", errors="ignore"), True


def _lightweight_recent_work(recent_work: ActivityRecentWork) -> ActivityRecentWork:
    """Remove drill-in bodies and cap each retained command output preview at 4 KiB."""

    commands = []
    for command in recent_work.commands[:10]:
        error, error_truncated = _bounded_utf8(
            command.error,
            max_bytes=ACTIVITY_OVERVIEW_COMMAND_PREVIEW_MAX_BYTES,
        )
        remaining_preview_bytes = max(
            0,
            ACTIVITY_OVERVIEW_COMMAND_PREVIEW_MAX_BYTES - len((error or "").encode("utf-8")),
        )
        preview, preview_truncated = _bounded_utf8(
            command.preview,
            max_bytes=remaining_preview_bytes,
        )
        commands.append(
            command.model_copy(
                update={
                    "arguments": {},
                    "evaluation": None,
                    "error": error,
                    "preview": preview,
                    "preview_truncated": command.preview_truncated
                    or error_truncated
                    or preview_truncated,
                }
            )
        )

    def lightweight_mutation(mutation: Any) -> Any:
        return mutation.model_copy(
            update={
                "arguments": {},
                "result_preview": None,
                "streamed_output": None,
                "evaluation": None,
                "error": None,
                "file_diffs": [
                    diff.model_copy(update={"diff": "", "content_truncated": True})
                    for diff in mutation.file_diffs
                ],
            }
        )

    deliverables = [
        deliverable.model_copy(
            update={
                "content": None,
                "content_preview_truncated": deliverable.content is not None
                or deliverable.content_preview_truncated,
                "render_metadata": None,
                "export_metadata": None,
            }
        )
        for deliverable in recent_work.deliverables
    ]
    return ActivityRecentWork(
        commands=commands,
        mutations=[lightweight_mutation(item) for item in recent_work.mutations],
        files=[lightweight_mutation(item) for item in recent_work.files],
        artifacts=recent_work.artifacts,
        deliverables=deliverables,
    )


async def read_work_page(
    db: AsyncSession,
    *,
    owner_email: str,
    scope: TimelineScope,
    session_rows: list[Session],
    graph_fingerprint: str,
    cursor_secret: str,
    before: str | None,
    limit: int,
    category: WorkCategory | None = None,
    from_time: datetime | None = None,
    to_time: datetime | None = None,
    tool_definitions: Mapping[str, ToolDefinition] | None = None,
    exact_session_id: str | None = None,
    detail: ActivityOverviewDetail = "full",
) -> WorkDatabasePage:
    authorized_session_ids = {row.session_id for row in session_rows}
    if exact_session_id is not None and exact_session_id not in authorized_session_ids:
        raise WorkCursorError("Requested Work session was not found")
    if exact_session_id is not None:
        session_rows = [row for row in session_rows if row.session_id == exact_session_id]
    session_ids = [row.session_id for row in session_rows]
    states = await read_work_projection_states(db, rows=session_rows)
    status = _materialization(states, total=len(session_rows))
    complete = status.state == "live"
    now = datetime.now(UTC)
    snapshot_at = now
    cutoff = now
    older_key: list[Any] | None = None
    cursor_summary: WorkSummary | None = None
    if before is not None:
        if not complete:
            raise WorkCursorError("Work history is not fully materialized")
        payload = _unsign(before, cursor_secret)
        expected = {
            "owner": owner_email,
            "graph": graph_fingerprint,
            "version": WORK_MATERIALIZER_VERSION,
            "category": category,
            "from": from_time.isoformat() if from_time else None,
            "to": to_time.isoformat() if to_time else None,
            "session_id": exact_session_id,
        }
        if any(payload.get(key) != value for key, value in expected.items()):
            raise WorkCursorError("Work cursor does not match the authorized graph")
        cutoff = datetime.fromisoformat(str(payload["cutoff"]))
        older_key = list(payload["older"])
        try:
            snapshot_at = datetime.fromisoformat(str(payload["snapshot_at"]))
        except (KeyError, TypeError, ValueError) as exc:
            raise WorkCursorError("invalid Work cursor snapshot") from exc
        if category is not None:
            raw_cursor_summary = payload.get("summary")
            if raw_cursor_summary is not None:
                try:
                    cursor_summary = WorkSummary.model_validate(raw_cursor_summary)
                except Exception as exc:
                    raise WorkCursorError("invalid Work cursor summary") from exc
    if category == "files":
        if before is not None:
            raise WorkCursorError("Files projection does not support pagination")
        current_statement = select(WorkCurrentFileRow).where(
            WorkCurrentFileRow.owner_email == owner_email,
            WorkCurrentFileRow.session_id.in_(session_ids or [""]),
            WorkCurrentFileRow.materializer_version == WORK_MATERIALIZER_VERSION,
        )
        if exact_session_id is not None:
            current_statement = current_statement.where(
                WorkCurrentFileRow.session_id == exact_session_id
            )
        if from_time is not None or to_time is not None:
            current_statement = current_statement.join(
                WorkRecordRow,
                and_(
                    WorkRecordRow.owner_email == WorkCurrentFileRow.owner_email,
                    WorkRecordRow.session_id == WorkCurrentFileRow.session_id,
                    WorkRecordRow.materializer_version == WorkCurrentFileRow.materializer_version,
                    WorkRecordRow.source_seq == WorkCurrentFileRow.source_seq,
                ),
            )
            if from_time is not None:
                current_statement = current_statement.where(WorkRecordRow.occurred_at >= from_time)
            if to_time is not None:
                current_statement = current_statement.where(WorkRecordRow.occurred_at < to_time)
        physical_order = {row.session_id: ordinal for ordinal, row in enumerate(session_rows)}
        current_by_path: dict[str, WorkCurrentFileRow] = {}
        for row in (await db.scalars(current_statement)).all():
            previous = current_by_path.get(row.path_id)
            ordering = (
                physical_order.get(row.session_id, -1),
                row.source_seq,
                row.recreate_ordinal,
                row.rename_ordinal,
            )
            if previous is None or ordering > (
                physical_order.get(previous.session_id, -1),
                previous.source_seq,
                previous.recreate_ordinal,
                previous.rename_ordinal,
            ):
                current_by_path[row.path_id] = row
        current_rows = [row for row in current_by_path.values() if row.state != "deleted"]
        occurrence_by_source = {
            (session_id, source_seq): occurred_at.isoformat()
            for session_id, source_seq, occurred_at in (
                await db.execute(
                    select(
                        WorkRecordRow.session_id,
                        WorkRecordRow.source_seq,
                        func.max(WorkRecordRow.occurred_at),
                    )
                    .where(
                        WorkRecordRow.owner_email == owner_email,
                        WorkRecordRow.session_id.in_(session_ids or [""]),
                        WorkRecordRow.materializer_version == WORK_MATERIALIZER_VERSION,
                        WorkRecordRow.source_seq.in_(
                            {row.source_seq for row in current_rows} or {0}
                        ),
                    )
                    .group_by(WorkRecordRow.session_id, WorkRecordRow.source_seq)
                )
            ).all()
        }
        file_rows = [
            SimpleNamespace(
                session_id=row.session_id,
                path_generation_id=row.path_generation_id,
                payload={
                    "source_item_id": row.source_item_id,
                    "source_seq": row.source_seq,
                    "occurred_at": occurrence_by_source.get((row.session_id, row.source_seq)),
                    "path": row.path,
                    "path_id": row.path_id,
                    "relative_path": row.relative_path,
                    "root_id": row.root_id,
                    "root_label": row.root_label,
                    "previous_path": row.previous_path,
                    "state": row.state,
                    "binary": row.binary,
                    "generated": row.generated,
                    "additions": row.additions,
                    "deletions": row.deletions,
                    "preview": row.preview,
                    "preview_truncated": row.preview_truncated,
                    "preview_omitted": row.preview_omitted or row.preview is None,
                    "updated_at": row.updated_at.isoformat(),
                    "created_at": row.created_at.isoformat(),
                },
            )
            for row in current_rows
        ]
        selected = _cached_current_file_items(file_rows)
        return WorkDatabasePage(
            scope=scope,
            projection_version=current_projection_version(),
            items=selected,
            removed_call_ids=[],
            materialization=status,
            has_more_before=False,
            before_cursor=None,
            server_time=now.isoformat(),
            summary=WorkSummary(
                mutations=sum(state.mutation_count for state in states),
                commands=sum(state.command_count for state in states),
                changed_files=len({row.path_id for row in current_rows}),
                artifacts=sum(state.artifact_count for state in states),
                deliverables=sum(state.deliverable_count for state in states),
                additions=sum(row.additions for row in current_rows),
                deletions=sum(row.deletions for row in current_rows),
                omitted_files=sum(row.preview_omitted for row in current_rows),
            ),
            category=category,
        )
    statement = select(WorkRecordRow).where(
        WorkRecordRow.owner_email == owner_email,
        WorkRecordRow.session_id.in_(session_ids or [""]),
        WorkRecordRow.materializer_version == WORK_MATERIALIZER_VERSION,
        WorkRecordRow.is_evidence.is_(True),
        WorkRecordRow.occurred_at <= cutoff,
        WorkRecordRow.materialized_at <= snapshot_at,
    )
    if from_time is not None:
        statement = statement.where(WorkRecordRow.occurred_at >= from_time)
    if to_time is not None:
        statement = statement.where(WorkRecordRow.occurred_at < to_time)
    category_statement = statement
    if older_key is not None:
        occurred_at = datetime.fromisoformat(str(older_key[0]))
        key = (
            occurred_at,
            str(older_key[1]),
            int(older_key[2]),
            int(older_key[3]),
            str(older_key[4]),
        )
        statement = statement.where(
            or_(
                WorkRecordRow.occurred_at < key[0],
                and_(
                    WorkRecordRow.occurred_at == key[0],
                    WorkRecordRow.session_id < key[1],
                ),
                and_(
                    WorkRecordRow.occurred_at == key[0],
                    WorkRecordRow.session_id == key[1],
                    WorkRecordRow.source_seq < key[2],
                ),
                and_(
                    WorkRecordRow.occurred_at == key[0],
                    WorkRecordRow.session_id == key[1],
                    WorkRecordRow.source_seq == key[2],
                    WorkRecordRow.item_ordinal < key[3],
                ),
                and_(
                    WorkRecordRow.occurred_at == key[0],
                    WorkRecordRow.session_id == key[1],
                    WorkRecordRow.source_seq == key[2],
                    WorkRecordRow.item_ordinal == key[3],
                    WorkRecordRow.work_record_id < key[4],
                ),
            )
        )
    if category is not None:
        return await _read_category_page(
            db,
            statement=category_statement,
            owner_email=owner_email,
            scope=scope,
            session_ids=session_ids,
            graph_fingerprint=graph_fingerprint,
            cursor_secret=cursor_secret,
            snapshot_at=snapshot_at,
            cutoff=cutoff,
            category=category,
            from_time=from_time,
            to_time=to_time,
            limit=limit,
            complete=complete,
            materialization=status,
            now=now,
            tool_definitions=tool_definitions or {},
            older_key=older_key,
            cursor_summary=cursor_summary,
            exact_session_id=exact_session_id,
            detail=detail,
            lightweight_summary=_sum_summaries(
                _projection_summaries(
                    states,
                    session_ids=session_ids,
                    path_ids_by_session={},
                ).values()
            ),
        )

    records = (
        await db.scalars(
            statement.order_by(
                WorkRecordRow.occurred_at.desc(),
                WorkRecordRow.session_id.desc(),
                WorkRecordRow.source_seq.desc(),
                WorkRecordRow.item_ordinal.desc(),
                WorkRecordRow.work_record_id.desc(),
            ).limit(limit + 1)
        )
    ).all()
    page_records = list(records[:limit])
    removed_call_ids = (
        await _removed_call_ids(
            db,
            owner_email=owner_email,
            session_ids=session_ids,
        )
        if before is None
        else []
    )
    hydrated_items = await _hydrate_file_items(
        db,
        records=page_records,
        items=[_decode_persisted_work_item(record.timeline_item) for record in page_records],
    )
    by_item: dict[str, TimelineItem] = {}
    for item in reversed(hydrated_items):
        by_item[item.id] = item
    artifact_ids = [
        item.artifact_id for item in by_item.values() if isinstance(item, ArtifactTimelineItem)
    ]
    if artifact_ids:
        artifacts = {
            row.artifact_id: row
            for row in (
                await db.scalars(
                    select(ArtifactRecordRow).where(
                        ArtifactRecordRow.artifact_id.in_(artifact_ids),
                        ArtifactRecordRow.owner_email == owner_email,
                        ArtifactRecordRow.deleted_at.is_(None),
                    )
                )
            ).all()
        }
        for item_id, item in list(by_item.items()):
            if not isinstance(item, ArtifactTimelineItem):
                continue
            artifact = artifacts.get(item.artifact_id)
            if artifact is None:
                by_item.pop(item_id, None)
                continue
            by_item[item_id] = item.model_copy(
                update={
                    "filename": artifact.filename,
                    "mime_type": artifact.mime_type,
                    "size_bytes": artifact.size_bytes,
                }
            )
    has_more = complete and len(records) > limit
    cursor = None
    if has_more and page_records:
        oldest = page_records[-1]
        cursor_payload = {
            "owner": owner_email,
            "graph": graph_fingerprint,
            "version": WORK_MATERIALIZER_VERSION,
            "snapshot_at": snapshot_at.isoformat(),
            "category": None,
            "from": from_time.isoformat() if from_time else None,
            "to": to_time.isoformat() if to_time else None,
            "session_id": exact_session_id,
            "cutoff": cutoff.isoformat(),
            "older": [
                oldest.occurred_at.isoformat(),
                oldest.session_id,
                oldest.source_seq,
                oldest.item_ordinal,
                oldest.work_record_id,
            ],
        }
        cursor = _sign(cursor_payload, cursor_secret)
    return WorkDatabasePage(
        scope=scope,
        projection_version=current_projection_version(),
        items=list(by_item.values()),
        removed_call_ids=removed_call_ids,
        materialization=status,
        has_more_before=has_more,
        before_cursor=cursor,
        server_time=now.isoformat(),
    )


def _cached_current_file_items(
    rows: Sequence[Any],
) -> list[TimelineItem]:
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for row in rows:
        payload = dict(row.payload)
        payload["_path_generation_id"] = row.path_generation_id
        grouped.setdefault(
            (row.session_id, str(payload["source_item_id"])),
            [],
        ).append(payload)
    items: list[TimelineItem] = []
    for (session_id, source_item_id), payloads in grouped.items():
        payloads.sort(key=lambda payload: (str(payload["path"]), str(payload["path_id"])))
        newest = max(payloads, key=lambda payload: int(payload["source_seq"]))
        created_at = newest.get("updated_at") or newest.get("created_at")
        items.append(
            ToolCallTimelineItem(
                id=source_item_id,
                sort_key=(
                    f"{created_at or ''}:{session_id}:"
                    f"{int(newest['source_seq']):020d}:{source_item_id}"
                ),
                created_at=str(created_at) if created_at else None,
                call_id=source_item_id.removeprefix("tool:"),
                tool_name="files",
                display_name="Current files",
                status="complete",
                file_diffs=[
                    FileDiffRef(
                        path=str(payload["path"]),
                        occurred_at=(
                            str(payload["occurred_at"]) if payload.get("occurred_at") else None
                        ),
                        diff=str(payload.get("preview") or ""),
                        status=str(payload.get("state") or "modified"),
                        old_path=(
                            str(payload["previous_path"]) if payload.get("previous_path") else None
                        ),
                        binary=bool(payload.get("binary")),
                        generated=bool(payload.get("generated")),
                        truncated=bool(payload.get("preview_truncated")),
                        preview_omitted=bool(payload.get("preview_omitted")),
                        preview_omission_reason=_preview_omission_reason(payload),
                        path_id=str(payload["path_id"]),
                        path_generation_id=str(payload["_path_generation_id"]),
                        relative_path=(
                            str(payload["relative_path"]) if payload.get("relative_path") else None
                        ),
                        root_label=(
                            str(payload["root_label"]) if payload.get("root_label") else None
                        ),
                        root_id=str(payload["root_id"]) if payload.get("root_id") else None,
                        additions=int(payload.get("additions") or 0),
                        deletions=int(payload.get("deletions") or 0),
                        content_truncated=bool(
                            payload.get("preview_truncated") or payload.get("preview_omitted")
                        ),
                    )
                    for payload in payloads
                ],
            )
        )
    return sorted(items, key=lambda item: item.sort_key, reverse=True)


async def _removed_call_ids(
    db: AsyncSession,
    *,
    owner_email: str,
    session_ids: list[str],
) -> list[str]:
    statement = select(WorkRecordRow.call_id, WorkRecordRow.is_evidence).where(
        WorkRecordRow.owner_email == owner_email,
        WorkRecordRow.session_id.in_(session_ids or [""]),
        WorkRecordRow.materializer_version == WORK_MATERIALIZER_VERSION,
        WorkRecordRow.call_id.is_not(None),
    )
    rows = (
        await db.execute(statement.order_by(WorkRecordRow.materialized_at.desc()).limit(1000))
    ).all()
    latest_by_call: dict[str, bool] = {}
    for call_id, is_evidence in rows:
        if call_id:
            latest_by_call.setdefault(str(call_id), bool(is_evidence))
    return [call_id for call_id, is_evidence in latest_by_call.items() if not is_evidence][:500]


async def _read_category_page(
    db: AsyncSession,
    *,
    statement: Any,
    owner_email: str,
    scope: TimelineScope,
    session_ids: list[str],
    graph_fingerprint: str,
    cursor_secret: str,
    snapshot_at: datetime,
    cutoff: datetime,
    category: WorkCategory,
    from_time: datetime | None,
    to_time: datetime | None,
    limit: int,
    complete: bool,
    materialization: WorkMaterialization,
    now: datetime,
    tool_definitions: Mapping[str, ToolDefinition],
    older_key: list[Any] | None,
    cursor_summary: WorkSummary | None,
    exact_session_id: str | None,
    detail: ActivityOverviewDetail,
    lightweight_summary: WorkSummary,
) -> WorkDatabasePage:
    del tool_definitions
    summary = cursor_summary or (
        await _category_summary(db, statement=statement, owner_email=owner_email)
        if detail == "full"
        else lightweight_summary
    )
    page_statement = (
        statement if category is None else statement.where(WorkRecordRow.category == category)
    )
    if category in {"artifacts", "deliverables"}:
        ranked = page_statement.with_only_columns(
            WorkRecordRow.work_record_id.label("work_record_id"),
            func.row_number()
            .over(
                partition_by=WorkRecordRow.entity_id,
                order_by=(
                    WorkRecordRow.occurred_at.desc(),
                    WorkRecordRow.session_id.desc(),
                    WorkRecordRow.source_seq.desc(),
                    WorkRecordRow.item_ordinal.desc(),
                    WorkRecordRow.work_record_id.desc(),
                ),
            )
            .label("entity_rank"),
        ).subquery()
        page_statement = select(WorkRecordRow).where(
            WorkRecordRow.work_record_id.in_(
                select(ranked.c.work_record_id).where(ranked.c.entity_rank == 1)
            )
        )
    if older_key is not None:
        page_statement = page_statement.where(_older_than_predicate(older_key))
    if category == "artifacts":
        page_statement = page_statement.join(
            ArtifactRecordRow,
            and_(
                ArtifactRecordRow.artifact_id == WorkRecordRow.entity_id,
                ArtifactRecordRow.owner_email == owner_email,
                ArtifactRecordRow.deleted_at.is_(None),
            ),
        )
    if category == "files":
        selected = await _read_file_projection(db, statement=page_statement)
        canonical_path_ids = {
            diff.path_id
            for item in selected
            if isinstance(item, ToolCallTimelineItem)
            for diff in item.file_diffs
            if diff.path_id
        }
        summary = summary.model_copy(update={"changed_files": len(canonical_path_ids)})
        records: list[WorkRecordRow] = []
    else:
        ordered = page_statement.order_by(
            WorkRecordRow.occurred_at.desc(),
            WorkRecordRow.session_id.desc(),
            WorkRecordRow.source_seq.desc(),
            WorkRecordRow.item_ordinal.desc(),
            WorkRecordRow.work_record_id.desc(),
        )
        records = list((await db.scalars(ordered.limit(limit + 1))).all())
        selected = await _hydrate_file_items(
            db,
            records=records[:limit],
            items=[_decode_persisted_work_item(record.timeline_item) for record in records[:limit]],
        )
    page_records = records[:limit]
    has_more = category != "files" and len(records) > limit
    if category == "artifacts":
        artifact_ids = [
            item.artifact_id for item in selected if isinstance(item, ArtifactTimelineItem)
        ]
        artifacts = {
            row.artifact_id: row
            for row in (
                await db.scalars(
                    select(ArtifactRecordRow).where(
                        ArtifactRecordRow.artifact_id.in_(artifact_ids or [""]),
                        ArtifactRecordRow.owner_email == owner_email,
                        ArtifactRecordRow.deleted_at.is_(None),
                    )
                )
            ).all()
        }
        selected = [
            item.model_copy(
                update={
                    "filename": artifacts[item.artifact_id].filename,
                    "mime_type": artifacts[item.artifact_id].mime_type,
                    "size_bytes": artifacts[item.artifact_id].size_bytes,
                }
            )
            if isinstance(item, ArtifactTimelineItem)
            else item
            for item in selected
        ]
    cursor = None
    if category != "files" and complete and has_more and page_records:
        matching_record = page_records[-1]
        if matching_record is not None:
            cursor_payload = {
                "owner": owner_email,
                "graph": graph_fingerprint,
                "version": WORK_MATERIALIZER_VERSION,
                "snapshot_at": snapshot_at.isoformat(),
                "category": category,
                "from": from_time.isoformat() if from_time else None,
                "to": to_time.isoformat() if to_time else None,
                "session_id": exact_session_id,
                "summary": summary.model_dump(mode="json") if detail == "full" else None,
                "cutoff": cutoff.isoformat(),
                "older": [
                    matching_record.occurred_at.isoformat(),
                    matching_record.session_id,
                    matching_record.source_seq,
                    matching_record.item_ordinal,
                    matching_record.work_record_id,
                ],
            }
            cursor = _sign(cursor_payload, cursor_secret)
    removed_call_ids = (
        await _removed_call_ids(
            db,
            owner_email=owner_email,
            session_ids=session_ids,
        )
        if category == "commands" and older_key is None
        else []
    )
    return WorkDatabasePage(
        scope=scope,
        projection_version=current_projection_version(),
        items=selected,
        removed_call_ids=removed_call_ids,
        materialization=materialization,
        has_more_before=bool(cursor),
        before_cursor=cursor,
        server_time=now.isoformat(),
        detail=detail,
        summary=summary,
        category=category,
    )


def _latest_file_projection(statement: Any) -> Any:
    """Return one latest row per chronological file-path generation."""

    events = (
        statement.with_only_columns(
            WorkRecordRow.work_record_id.label("work_record_id"),
            WorkRecordRow.occurred_at.label("occurred_at"),
            WorkRecordRow.session_id.label("session_id"),
            WorkRecordRow.source_seq.label("source_seq"),
            WorkRecordRow.item_ordinal.label("item_ordinal"),
            WorkRecordFileRow.file_ordinal.label("file_ordinal"),
            WorkRecordFileRow.path.label("path"),
            WorkRecordFileRow.path_id.label("path_id"),
            WorkRecordFileRow.status.label("status"),
            WorkRecordFileRow.old_path.label("old_path"),
            WorkRecordFileRow.old_path_id.label("old_path_id"),
            WorkRecordFileRow.binary.label("binary"),
            WorkRecordFileRow.generated.label("generated"),
            WorkRecordFileRow.truncated.label("truncated"),
            WorkRecordFileRow.preview_omitted.label("preview_omitted"),
            WorkRecordFileRow.additions.label("additions"),
            WorkRecordFileRow.deletions.label("deletions"),
        )
        .join(
            WorkRecordFileRow,
            WorkRecordFileRow.work_record_id == WorkRecordRow.work_record_id,
        )
        .where(WorkRecordRow.category == "files")
        .subquery()
    )
    generation = _file_generation(events, path_id=events.c.path_id)
    old_generation = case(
        (events.c.old_path_id.is_not(None), _file_generation(events, path_id=events.c.old_path_id)),
        else_=None,
    )
    enriched = select(
        events,
        generation.label("generation"),
        old_generation.label("old_generation"),
    ).subquery()
    latest_order = (
        enriched.c.occurred_at.desc(),
        enriched.c.session_id.desc(),
        enriched.c.source_seq.desc(),
        enriched.c.item_ordinal.desc(),
        enriched.c.work_record_id.desc(),
        enriched.c.file_ordinal.desc(),
    )
    rename_order = (
        case((enriched.c.old_path_id.is_not(None), 0), else_=1),
        *latest_order,
    )
    ranked = select(
        enriched.c.work_record_id,
        enriched.c.occurred_at,
        enriched.c.session_id,
        enriched.c.source_seq,
        enriched.c.item_ordinal,
        enriched.c.file_ordinal,
        enriched.c.path,
        enriched.c.path_id,
        enriched.c.generation,
        enriched.c.status,
        enriched.c.old_path,
        enriched.c.old_path_id,
        func.first_value(enriched.c.old_path)
        .over(
            partition_by=(enriched.c.path_id, enriched.c.generation),
            order_by=rename_order,
        )
        .label("rename_old_path"),
        func.first_value(enriched.c.old_path_id)
        .over(
            partition_by=(enriched.c.path_id, enriched.c.generation),
            order_by=rename_order,
        )
        .label("rename_old_path_id"),
        func.first_value(enriched.c.old_generation)
        .over(
            partition_by=(enriched.c.path_id, enriched.c.generation),
            order_by=rename_order,
        )
        .label("rename_old_generation"),
        enriched.c.binary,
        enriched.c.generated,
        enriched.c.truncated,
        enriched.c.preview_omitted,
        func.sum(enriched.c.additions)
        .over(partition_by=(enriched.c.path_id, enriched.c.generation))
        .label("additions"),
        func.sum(enriched.c.deletions)
        .over(partition_by=(enriched.c.path_id, enriched.c.generation))
        .label("deletions"),
        func.row_number()
        .over(
            partition_by=(enriched.c.path_id, enriched.c.generation),
            order_by=latest_order,
        )
        .label("path_rank"),
    ).subquery()
    return select(ranked).where(ranked.c.path_rank == 1).subquery()


def _file_generation(events: Any, *, path_id: Any) -> Any:
    outgoing = events.alias()
    return (
        select(func.count())
        .where(
            outgoing.c.old_path_id == path_id,
            _file_event_is_before(outgoing.c, events.c),
        )
        .correlate(events)
        .scalar_subquery()
    )


def _file_event_is_before(left: Any, right: Any) -> Any:
    columns = (
        "occurred_at",
        "session_id",
        "source_seq",
        "item_ordinal",
        "work_record_id",
        "file_ordinal",
    )
    earlier: list[Any] = []
    equal: list[Any] = []
    for column in columns:
        earlier.append(and_(*equal, left[column] < right[column]))
        equal.append(left[column] == right[column])
    return or_(*earlier)


async def _read_file_projection(db: AsyncSession, *, statement: Any) -> list[TimelineItem]:
    """Hydrate every unique path without re-running its history aggregation."""

    latest = _latest_file_projection(statement)
    order = (
        latest.c.occurred_at.desc(),
        latest.c.session_id.desc(),
        latest.c.source_seq.desc(),
        latest.c.item_ordinal.desc(),
        latest.c.work_record_id.desc(),
        latest.c.file_ordinal,
    )
    projected_rows = (await db.execute(select(latest).order_by(*order))).mappings().all()
    file_rows = _canonicalize_renamed_file_rows([dict(row) for row in projected_rows])
    WORK_FILES_UNIQUE_PATHS.observe(len(file_rows))
    if not file_rows:
        WORK_FILES_SOURCE_RECORDS.observe(0)
        return []
    record_ids = list(dict.fromkeys(str(row["work_record_id"]) for row in file_rows))
    WORK_FILES_SOURCE_RECORDS.observe(len(record_ids))
    records = await _load_file_source_records(db, record_ids=record_ids)
    rows_by_record: dict[str, list[Any]] = {}
    for row in file_rows:
        rows_by_record.setdefault(str(row["work_record_id"]), []).append(row)

    items: list[TimelineItem] = []
    for record_id, rows in rows_by_record.items():
        record = records.get(record_id)
        if record is None:
            continue
        item = _decode_persisted_work_item(record.timeline_item)
        if not isinstance(item, ToolCallTimelineItem):
            continue
        file_diffs: list[FileDiffRef] = []
        for row in rows:
            ordinal = int(row["file_ordinal"])
            parent = item.file_diffs[ordinal] if ordinal < len(item.file_diffs) else None
            path = str(row["path"])
            path_id = str(row["path_id"])
            additions = int(row["additions"] or 0)
            deletions = int(row["deletions"] or 0)
            update: dict[str, Any] = {
                "path": path,
                "path_id": path_id,
                "additions": additions,
                "deletions": deletions,
            }
            if parent is not None:
                if row["is_canonical"] and row["old_path"] and not parent.old_path:
                    update["old_path"] = str(row["old_path"])
                file_diffs.append(parent.model_copy(update=update))
            else:
                file_diffs.append(
                    FileDiffRef(
                        path=path,
                        path_id=path_id,
                        additions=additions,
                        deletions=deletions,
                        diff="",
                        status=str(row["status"]) if row["status"] is not None else None,
                        old_path=str(row["old_path"]) if row["old_path"] is not None else None,
                        binary=bool(row["binary"]),
                        generated=bool(row["generated"]),
                        truncated=bool(row["truncated"]),
                        preview_omitted=bool(row["preview_omitted"]),
                        preview_omission_reason=(
                            "not_persisted" if row["preview_omitted"] else None
                        ),
                        content_truncated=True,
                    )
                )
        items.append(item.model_copy(update={"file_diffs": file_diffs}))
    return items


def _canonicalize_renamed_file_rows(
    rows: list[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Map rename-chain members to one final path while retaining their totals."""

    normalized = [dict(row) for row in rows]

    def node_key(row: Mapping[str, Any]) -> tuple[str, int]:
        return (str(row["path_id"]), int(row["generation"]))

    by_node = {node_key(row): row for row in normalized}
    forward: dict[tuple[str, int], tuple[str, int]] = {}
    for row in normalized:
        old_path_id = row.get("rename_old_path_id")
        old_generation = row.get("rename_old_generation")
        current = node_key(row)
        previous = (
            (str(old_path_id), int(old_generation))
            if old_path_id is not None and old_generation is not None
            else None
        )
        if previous is not None and previous != current and previous in by_node:
            forward.setdefault(previous, current)

    def row_order(key: tuple[str, int]) -> tuple[Any, ...]:
        row = by_node[key]
        return (
            row["occurred_at"],
            str(row["session_id"]),
            int(row["source_seq"]),
            int(row["item_ordinal"]),
            str(row["work_record_id"]),
        )

    def canonical_node(key: tuple[str, int]) -> tuple[str, int]:
        seen: list[tuple[str, int]] = []
        current = key
        while current in forward:
            if current in seen:
                cycle = seen[seen.index(current) :]
                return max(cycle, key=row_order)
            seen.append(current)
            current = forward[current]
        return current

    for row in normalized:
        original = node_key(row)
        canonical_key = canonical_node(original)
        canonical = by_node[canonical_key]
        row["path"] = canonical["path"]
        row["path_id"] = canonical_key[0]
        row["is_canonical"] = original == canonical_key
        if row["is_canonical"] and not row.get("old_path"):
            row["old_path"] = row.get("rename_old_path")
    return normalized


async def _load_file_source_records(
    db: AsyncSession,
    *,
    record_ids: list[str],
) -> dict[str, WorkRecordRow]:
    """Load every selected parent without exceeding common SQL bind limits."""

    records: dict[str, WorkRecordRow] = {}
    for start in range(0, len(record_ids), WORK_FILES_SOURCE_RECORD_BATCH_SIZE):
        batch = record_ids[start : start + WORK_FILES_SOURCE_RECORD_BATCH_SIZE]
        for record in (
            await db.scalars(select(WorkRecordRow).where(WorkRecordRow.work_record_id.in_(batch)))
        ).all():
            records[record.work_record_id] = record
    return records


def _older_than_predicate(older_key: list[Any]) -> Any:
    key = (
        datetime.fromisoformat(str(older_key[0])),
        str(older_key[1]),
        int(older_key[2]),
        int(older_key[3]),
        str(older_key[4]),
    )
    return or_(
        WorkRecordRow.occurred_at < key[0],
        and_(WorkRecordRow.occurred_at == key[0], WorkRecordRow.session_id < key[1]),
        and_(
            WorkRecordRow.occurred_at == key[0],
            WorkRecordRow.session_id == key[1],
            WorkRecordRow.source_seq < key[2],
        ),
        and_(
            WorkRecordRow.occurred_at == key[0],
            WorkRecordRow.session_id == key[1],
            WorkRecordRow.source_seq == key[2],
            WorkRecordRow.item_ordinal < key[3],
        ),
        and_(
            WorkRecordRow.occurred_at == key[0],
            WorkRecordRow.session_id == key[1],
            WorkRecordRow.source_seq == key[2],
            WorkRecordRow.item_ordinal == key[3],
            WorkRecordRow.work_record_id < key[4],
        ),
    )


async def _category_summary(
    db: AsyncSession,
    *,
    statement: Any,
    owner_email: str,
) -> WorkSummary:
    base = statement.with_only_columns(
        WorkRecordRow.work_record_id,
        WorkRecordRow.category,
        WorkRecordRow.entity_id,
    ).cte("work_summary_records")
    summary_source = base.outerjoin(
        WorkRecordFileRow,
        and_(
            base.c.category == "files",
            WorkRecordFileRow.work_record_id == base.c.work_record_id,
        ),
    ).outerjoin(
        ArtifactRecordRow,
        and_(
            base.c.category == "artifacts",
            ArtifactRecordRow.artifact_id == base.c.entity_id,
            ArtifactRecordRow.owner_email == owner_email,
            ArtifactRecordRow.deleted_at.is_(None),
        ),
    )
    totals = (
        await db.execute(
            select(
                func.count().filter(base.c.category == "commands").label("commands"),
                func.count().filter(base.c.category == "mutations").label("mutations"),
                func.count(func.distinct(base.c.entity_id))
                .filter(
                    base.c.category == "deliverables",
                    base.c.entity_id.is_not(None),
                )
                .label("deliverables"),
                func.count(func.distinct(base.c.entity_id))
                .filter(
                    base.c.category == "artifacts",
                    ArtifactRecordRow.artifact_id.is_not(None),
                )
                .label("artifacts"),
                func.count(func.distinct(WorkRecordFileRow.path_id))
                .filter(base.c.category == "files")
                .label("changed_files"),
                func.coalesce(
                    func.sum(WorkRecordFileRow.additions).filter(base.c.category == "files"),
                    0,
                ).label("additions"),
                func.coalesce(
                    func.sum(WorkRecordFileRow.deletions).filter(base.c.category == "files"),
                    0,
                ).label("deletions"),
            ).select_from(summary_source)
        )
    ).one()
    return WorkSummary(
        mutations=int(totals.mutations),
        commands=int(totals.commands),
        changed_files=int(totals.changed_files),
        artifacts=int(totals.artifacts),
        deliverables=int(totals.deliverables),
        additions=int(totals.additions),
        deletions=int(totals.deletions),
        omitted_files=0,
    )


def _empty_summary() -> WorkSummary:
    return WorkSummary(
        mutations=0,
        commands=0,
        changed_files=0,
        artifacts=0,
        deliverables=0,
    )


def _node_time(node: WorkstreamRef) -> tuple[str, str]:
    return (node.updated_at or node.created_at or "", node.session_id)


def _collapse_activity_workstreams(
    workstreams: list[WorkstreamRef],
    *,
    session_rows: list[Session],
    physical_root_key: str,
) -> _LogicalProjection:
    """Collapse authorized same-scope physical rotations for Activity Overview only."""

    if workstreams and all(node.backing_session_ids for node in workstreams):
        physical_to_logical = {
            f"session:{session_id}": node.key
            for node in workstreams
            for session_id in node.backing_session_ids
        }
        canonical_members_by_logical = {
            node.key: tuple(sorted(node.backing_session_ids)) for node in workstreams
        }
        physical_root_session_id = physical_root_key.removeprefix("session:")
        logical_root_key = (
            physical_root_key
            if physical_root_key in canonical_members_by_logical
            else physical_to_logical.get(
                physical_root_key,
                physical_to_logical.get(physical_root_session_id),
            )
        )
        canonical_logical_nodes = sorted(
            workstreams,
            key=lambda node: (
                0 if node.key == logical_root_key else 1,
                node.ordinal,
                node.key,
            ),
        )
        return _LogicalProjection(
            workstreams=[
                node.model_copy(update={"ordinal": ordinal})
                for ordinal, node in enumerate(canonical_logical_nodes)
            ],
            physical_to_logical=physical_to_logical,
            members_by_logical=canonical_members_by_logical,
            ambiguous=logical_root_key is None,
        )

    nodes = {node.key: node for node in workstreams}
    rows = {f"session:{row.session_id}": row for row in session_rows}
    parent = {key: key for key in nodes}

    def find(key: str) -> str:
        while parent[key] != key:
            parent[key] = parent[parent[key]]
            key = parent[key]
        return key

    def union(left: str, right: str) -> None:
        left_root = find(left)
        right_root = find(right)
        if left_root == right_root:
            return
        first, second = sorted((left_root, right_root))
        parent[second] = first

    for key, row in rows.items():
        previous_key = f"session:{row.previous_session_id}" if row.previous_session_id else None
        previous = rows.get(previous_key or "")
        if (
            previous_key in nodes
            and previous is not None
            and previous.conversation_id == row.conversation_id
            and previous.activity_scope_id == row.activity_scope_id
        ):
            union(key, previous_key)

    component_keys: dict[str, list[str]] = {}
    for key in nodes:
        component_keys.setdefault(find(key), []).append(key)

    canonical_by_member: dict[str, str] = {}
    members_by_logical: dict[str, tuple[str, ...]] = {}
    canonical_nodes: dict[str, WorkstreamRef] = {}
    structural_identity_by_logical: dict[str, WorkstreamRef] = {}
    for members in component_keys.values():
        member_set = set(members)
        referenced_parents = {
            nodes[key].parent_key for key in members if nodes[key].parent_key in member_set
        }
        heads = [key for key in members if key not in referenced_parents]
        candidates = [key for key in members if nodes[key].current]
        if not candidates:
            candidates = heads or members
        canonical_key = max(candidates, key=lambda key: _node_time(nodes[key]))
        member_ids = tuple(sorted(nodes[key].session_id for key in members))
        members_by_logical[canonical_key] = member_ids
        for key in members:
            canonical_by_member[key] = canonical_key
        canonical = nodes[canonical_key]
        structural_identity = next(
            (nodes[key] for key in members if nodes[key].kind not in {"root", "rotation"}),
            canonical,
        )
        structural_identity_by_logical[canonical_key] = structural_identity
        states = {nodes[key].activity_state for key in members}
        activity_state = (
            "ongoing" if "ongoing" in states else ("closed" if states == {"closed"} else "active")
        )
        canonical_nodes[canonical_key] = canonical.model_copy(
            update={
                "activity_state": activity_state,
                "current": any(nodes[key].current for key in members),
                "backing_session_count": len(members),
                "backing_session_ids": list(member_ids),
            }
        )

    root_logical_key = canonical_by_member.get(physical_root_key, physical_root_key)
    ambiguous = False
    logical_nodes: list[WorkstreamRef] = []
    for canonical_key, canonical in canonical_nodes.items():
        structural_identity = structural_identity_by_logical[canonical_key]
        members = [
            key for key, logical_key in canonical_by_member.items() if logical_key == canonical_key
        ]
        external: list[tuple[str, str]] = []
        for key in members:
            parent_key = nodes[key].parent_key
            if not parent_key:
                continue
            if parent_key not in nodes:
                ambiguous = True
                continue
            candidate_parent = canonical_by_member.get(parent_key, parent_key)
            if candidate_parent != canonical_key:
                external.append((key, candidate_parent))
        canonical_external = [parent_key for key, parent_key in external if key == canonical_key]
        distinct_parents = sorted({parent_key for _key, parent_key in external})
        if len(distinct_parents) > 1:
            ambiguous = True
        logical_parent: str | None
        if canonical_external:
            logical_parent = canonical_external[0]
        elif external:
            logical_parent = max(
                external,
                key=lambda item: (_node_time(nodes[item[0]]), item[1]),
            )[1]
        else:
            logical_parent = None
        is_root = canonical_key == root_logical_key
        logical_nodes.append(
            canonical.model_copy(
                update={
                    "kind": "root" if is_root else structural_identity.kind,
                    "edge_kind": "root" if is_root else structural_identity.edge_kind,
                    "parent_key": None if is_root else logical_parent,
                    "root_key": root_logical_key,
                }
            )
        )
    logical_nodes.sort(
        key=lambda node: (
            0 if node.key == root_logical_key else 1,
            node.ordinal,
            node.key,
        )
    )
    logical_nodes = [
        node.model_copy(update={"ordinal": ordinal}) for ordinal, node in enumerate(logical_nodes)
    ]
    return _LogicalProjection(
        workstreams=logical_nodes,
        physical_to_logical=canonical_by_member,
        members_by_logical=members_by_logical,
        ambiguous=ambiguous,
    )


async def _logical_summaries(
    db: AsyncSession,
    *,
    owner_email: str,
    statement: Any,
    physical_summaries: dict[str, WorkSummary],
    logical: _LogicalProjection,
) -> dict[str, WorkSummary]:
    logical_by_session = {
        physical.removeprefix("session:"): logical_key
        for physical, logical_key in logical.physical_to_logical.items()
    }
    file_statement, entity_statement = _logical_summary_statements(
        statement=statement,
        logical_by_session=logical_by_session,
        owner_email=owner_email,
    )
    file_counts = (await db.execute(file_statement)).all()
    entity_counts = (await db.execute(entity_statement)).all()
    return _aggregate_logical_summaries(
        physical_summaries=physical_summaries,
        logical=logical,
        file_counts=file_counts,
        entity_counts=entity_counts,
    )


async def _logical_todo_progress(
    db: AsyncSession,
    *,
    owner_email: str,
    session_rows: list[Session],
    logical: _LogicalProjection,
) -> dict[str, TodoProgress]:
    """Aggregate TODO progress in two graph-wide queries without cross-node leakage."""

    del owner_email  # session_rows already passed owner-authorized graph resolution
    session_ids = [row.session_id for row in session_rows]
    conversation_ids = sorted({row.conversation_id for row in session_rows})
    todo_rows = (
        await db.execute(
            union_all(
                select(
                    literal("session").label("kind"),
                    SessionTodo.session_id.label("identifier"),
                    SessionTodo.position,
                    SessionTodo.status,
                ).where(SessionTodo.session_id.in_(session_ids or [""])),
                select(
                    literal("conversation").label("kind"),
                    ConversationTodo.conversation_id.label("identifier"),
                    ConversationTodo.position,
                    ConversationTodo.status,
                ).where(ConversationTodo.conversation_id.in_(conversation_ids or [""])),
            )
        )
    ).all()
    conversation_rows: dict[str, dict[int, str]] = {}
    for kind, identifier, position, status in todo_rows:
        if kind == "conversation":
            conversation_rows.setdefault(str(identifier), {})[int(position)] = str(status)
    session_rows_by_id = {row.session_id: row for row in session_rows}
    session_todo_rows: dict[str, dict[int, str]] = {}
    for kind, identifier, position, status in todo_rows:
        if kind == "session":
            session_todo_rows.setdefault(str(identifier), {})[int(position)] = str(status)
    result: dict[str, TodoProgress] = {}
    for logical_key, member_ids in logical.members_by_logical.items():
        conversations = {
            session_rows_by_id[session_id].conversation_id
            for session_id in member_ids
            if session_id in session_rows_by_id
        }
        items: dict[tuple[str, int], str] = {}
        for conversation_id in sorted(conversations):
            authoritative = conversation_rows.get(conversation_id)
            if authoritative:
                items.update(
                    {
                        (f"conversation:{conversation_id}", position): status
                        for position, status in authoritative.items()
                    }
                )
                continue
            for session_id in member_ids:
                row = session_rows_by_id.get(session_id)
                if row is None or row.conversation_id != conversation_id:
                    continue
                items.update(
                    {
                        (f"session:{session_id}", position): status
                        for position, status in session_todo_rows.get(session_id, {}).items()
                    }
                )
        statuses = [status for status in items.values() if status != "cancelled"]
        if not statuses:
            continue
        result[logical_key] = TodoProgress(
            total=len(statuses),
            completed=sum(status == "completed" for status in statuses),
            in_progress=sum(status == "in_progress" for status in statuses),
        )
    return result


def _logical_summary_statements(
    *,
    statement: Any,
    logical_by_session: dict[str, str],
    owner_email: str,
) -> tuple[Any, Any]:
    logical_key = case(
        logical_by_session,
        value=WorkRecordRow.session_id,
        else_=None,
    )
    file_statement = (
        statement.with_only_columns(
            logical_key.label("logical_key"),
            func.count(func.distinct(WorkRecordFileRow.path_id)),
        )
        .join(
            WorkRecordFileRow,
            WorkRecordFileRow.work_record_id == WorkRecordRow.work_record_id,
        )
        .where(WorkRecordRow.category == "files")
        .group_by(logical_key)
    )
    entity_statement = (
        statement.with_only_columns(
            logical_key.label("logical_key"),
            WorkRecordRow.category,
            func.count(func.distinct(WorkRecordRow.entity_id)),
        )
        .outerjoin(
            ArtifactRecordRow,
            and_(
                WorkRecordRow.category == "artifacts",
                ArtifactRecordRow.artifact_id == WorkRecordRow.entity_id,
                ArtifactRecordRow.owner_email == owner_email,
            ),
        )
        .where(
            WorkRecordRow.category.in_(["artifacts", "deliverables"]),
            WorkRecordRow.entity_id.is_not(None),
            or_(
                WorkRecordRow.category == "deliverables",
                and_(
                    ArtifactRecordRow.artifact_id.is_not(None),
                    ArtifactRecordRow.deleted_at.is_(None),
                ),
            ),
        )
        .group_by(logical_key, WorkRecordRow.category)
    )
    return file_statement, entity_statement


def _aggregate_logical_summaries(
    *,
    physical_summaries: dict[str, WorkSummary],
    logical: _LogicalProjection,
    file_counts: Any,
    entity_counts: Any,
) -> dict[str, WorkSummary]:
    files = {str(logical_key): int(count) for logical_key, count in file_counts}
    artifacts = {
        str(logical_key): int(count)
        for logical_key, category, count in entity_counts
        if category == "artifacts"
    }
    deliverables = {
        str(logical_key): int(count)
        for logical_key, category, count in entity_counts
        if category == "deliverables"
    }
    result: dict[str, WorkSummary] = {}
    for logical_key, member_ids in logical.members_by_logical.items():
        member_summaries = [
            physical_summaries.get(session_id, _empty_summary()) for session_id in member_ids
        ]
        result[logical_key] = WorkSummary(
            mutations=sum(item.mutations for item in member_summaries),
            commands=sum(item.commands for item in member_summaries),
            changed_files=files.get(logical_key, 0),
            artifacts=artifacts.get(logical_key, 0),
            deliverables=deliverables.get(logical_key, 0),
            additions=sum(item.additions for item in member_summaries),
            deletions=sum(item.deletions for item in member_summaries),
            omitted_files=sum(item.omitted_files for item in member_summaries),
        )
    return result


async def _session_summaries(
    db: AsyncSession,
    *,
    owner_email: str,
    statement: Any,
    session_ids: list[str],
) -> dict[str, WorkSummary]:
    summaries = {session_id: _empty_summary() for session_id in session_ids}
    base = statement.with_only_columns(
        WorkRecordRow.work_record_id,
        WorkRecordRow.session_id,
        WorkRecordRow.category,
        WorkRecordRow.entity_id,
    ).subquery()
    category_rows = (
        await db.execute(
            select(base.c.session_id, base.c.category, func.count())
            .where(base.c.category.in_(["commands", "mutations"]))
            .group_by(base.c.session_id, base.c.category)
        )
    ).all()
    entity_rows = (
        await db.execute(
            select(
                base.c.session_id,
                base.c.category,
                func.count(func.distinct(base.c.entity_id)),
            )
            .where(
                base.c.category == "deliverables",
                base.c.entity_id.is_not(None),
            )
            .group_by(base.c.session_id, base.c.category)
        )
    ).all()
    artifact_rows = (
        await db.execute(
            select(
                base.c.session_id,
                func.count(func.distinct(base.c.entity_id)),
            )
            .select_from(
                base.join(
                    ArtifactRecordRow,
                    and_(
                        ArtifactRecordRow.artifact_id == base.c.entity_id,
                        ArtifactRecordRow.owner_email == owner_email,
                        ArtifactRecordRow.deleted_at.is_(None),
                    ),
                )
            )
            .where(base.c.category == "artifacts")
            .group_by(base.c.session_id)
        )
    ).all()
    file_rows = (
        await db.execute(
            select(
                base.c.session_id,
                func.count(func.distinct(WorkRecordFileRow.path_id)),
                func.coalesce(func.sum(WorkRecordFileRow.additions), 0),
                func.coalesce(func.sum(WorkRecordFileRow.deletions), 0),
            )
            .select_from(
                base.join(
                    WorkRecordFileRow,
                    WorkRecordFileRow.work_record_id == base.c.work_record_id,
                )
            )
            .where(base.c.category == "files")
            .group_by(base.c.session_id)
        )
    ).all()
    for session_id, category, count in [*category_rows, *entity_rows]:
        current = summaries[str(session_id)]
        field = {
            "commands": "commands",
            "mutations": "mutations",
            "artifacts": "artifacts",
            "deliverables": "deliverables",
        }[str(category)]
        summaries[str(session_id)] = current.model_copy(update={field: int(count)})
    for session_id, files, additions, deletions in file_rows:
        current = summaries[str(session_id)]
        summaries[str(session_id)] = current.model_copy(
            update={
                "changed_files": int(files),
                "additions": int(additions),
                "deletions": int(deletions),
            }
        )
    for session_id, artifacts in artifact_rows:
        current = summaries[str(session_id)]
        summaries[str(session_id)] = current.model_copy(update={"artifacts": int(artifacts)})
    return summaries


async def _recent_activity(
    db: AsyncSession,
    *,
    owner_email: str,
    session_ids: list[str],
    statement: Any,
) -> tuple[
    dict[WorkCategory, list[ActivityRecentItem]],
    list[WorkRecordRow],
    dict[str, TimelineItem],
]:
    common_columns = (
        WorkRecordRow.work_record_id,
        WorkRecordRow.source_item_id,
        WorkRecordRow.category,
        WorkRecordRow.session_id,
        WorkRecordRow.occurred_at,
        WorkRecordRow.call_id,
        WorkRecordRow.entity_id,
        WorkRecordRow.source_seq,
        WorkRecordRow.item_ordinal,
    )
    rows: list[Any] = []
    artifact_metadata: dict[str, ArtifactRecordRow] = {}
    for category in ("files", "commands", "mutations", "artifacts", "deliverables"):
        metadata_columns = (
            literal(None, type_=ArtifactRecordRow.filename.type).label("artifact_filename"),
            literal(None, type_=ArtifactRecordRow.mime_type.type).label("artifact_mime_type"),
            literal(None, type_=ArtifactRecordRow.size_bytes.type).label("artifact_size_bytes"),
        )
        recent_statement = (
            select(
                *common_columns,
                *metadata_columns,
            )
            .where(
                WorkRecordRow.owner_email == owner_email,
                WorkRecordRow.session_id.in_(session_ids or [""]),
                WorkRecordRow.materializer_version == WORK_MATERIALIZER_VERSION,
                WorkRecordRow.is_evidence.is_(True),
                WorkRecordRow.category
                >= literal_column(f"'{category}'", type_=WorkRecordRow.category.type),
                WorkRecordRow.category
                <= literal_column(f"'{category}'", type_=WorkRecordRow.category.type),
            )
            .order_by(
                WorkRecordRow.category.desc(),
                WorkRecordRow.occurred_at.desc(),
                WorkRecordRow.session_id.desc(),
                WorkRecordRow.source_seq.desc(),
                WorkRecordRow.item_ordinal.desc(),
                WorkRecordRow.work_record_id.desc(),
            )
        )
        seen: set[str] = set()
        frontier: list[Any] | None = None
        while len(seen) < 10:
            page_statement = recent_statement
            if frontier is not None:
                page_statement = page_statement.where(_older_than_predicate(frontier))
            stream = await db.stream(
                page_statement.limit(4096).execution_options(
                    yield_per=32,
                    stream_results=True,
                )
            )
            last: Any | None = None
            try:
                async for partition in stream.partitions(32):
                    last = partition[-1]
                    if category == "artifacts":
                        artifacts_by_id = {
                            artifact.artifact_id: artifact
                            for artifact in (
                                await db.scalars(
                                    select(ArtifactRecordRow).where(
                                        ArtifactRecordRow.owner_email == owner_email,
                                        ArtifactRecordRow.artifact_id.in_(
                                            {
                                                str(row.entity_id)
                                                for row in partition
                                                if row.entity_id is not None
                                            }
                                            or {""}
                                        ),
                                        ArtifactRecordRow.deleted_at.is_(None),
                                    )
                                )
                            ).all()
                        }
                    else:
                        artifacts_by_id = {}
                    for row in partition:
                        identity = str(row.entity_id or row.source_item_id)
                        artifact = artifacts_by_id.get(identity)
                        if identity in seen or (category == "artifacts" and artifact is None):
                            continue
                        seen.add(identity)
                        rows.append(row)
                        if artifact is not None:
                            artifact_metadata[str(row.work_record_id)] = artifact
                        if len(seen) == 10:
                            break
                    if len(seen) == 10:
                        break
            finally:
                await stream.close()
            if last is None or len(seen) == 10:
                break
            frontier = [
                last.occurred_at.isoformat(),
                last.session_id,
                last.source_seq,
                last.item_ordinal,
                last.work_record_id,
            ]
    selected_ids = [str(row.work_record_id) for row in rows]
    records_by_id = {
        record.work_record_id: record
        for record in (
            await db.scalars(
                select(WorkRecordRow).where(WorkRecordRow.work_record_id.in_(selected_ids or [""]))
            )
        ).all()
    }
    records = [
        records_by_id[identifier] for identifier in selected_ids if identifier in records_by_id
    ]
    authorized_records: list[WorkRecordRow] = []
    item_overrides: dict[str, TimelineItem] = {}
    for record in records:
        if record.category == "artifacts":
            artifact = artifact_metadata.get(record.work_record_id)
            if artifact is None:
                continue
            item = _decode_persisted_work_item(record.timeline_item)
            if isinstance(item, ArtifactTimelineItem):
                item_overrides[record.work_record_id] = item.model_copy(
                    update={
                        "filename": artifact.filename,
                        "mime_type": artifact.mime_type,
                        "size_bytes": artifact.size_bytes,
                    }
                )
        authorized_records.append(record)
    recent: dict[WorkCategory, list[ActivityRecentItem]] = {}
    authorized_ids = {record.work_record_id for record in authorized_records}
    for row in rows:
        if str(row.work_record_id) not in authorized_ids:
            continue
        raw_category = str(row.category)
        if raw_category not in {"files", "commands", "mutations", "artifacts", "deliverables"}:
            continue
        recent_category: WorkCategory = cast(WorkCategory, raw_category)
        recent_item = ActivityRecentItem(
            id=str(row.source_item_id),
            category=recent_category,
            session_id=str(row.session_id),
            occurred_at=row.occurred_at.isoformat(),
            title=str(row.call_id or row.entity_id or recent_category),
        )
        recent.setdefault(recent_category, []).append(recent_item)
    for items in recent.values():
        items.sort(key=lambda item: item.occurred_at, reverse=True)
    authorized_records.sort(
        key=lambda record: (
            record.occurred_at,
            record.session_id,
            record.source_seq,
            record.item_ordinal,
            record.work_record_id,
        ),
        reverse=True,
    )
    return recent, authorized_records, item_overrides


async def _session_runtime_metadata(
    db: AsyncSession,
    *,
    owner_email: str,
    session_rows: list[Session],
    turn_rows: Sequence[Any] | None = None,
    session_cache: Any | None = None,
) -> dict[
    str,
    tuple[str | None, str | None, str | None, str | None, str | None],
]:
    session_ids = [row.session_id for row in session_rows]
    if turn_rows is None:
        turn_rows = (
            await db.execute(
                select(
                    DirectTurnRequestRow.session_id,
                    DirectTurnRequestRow.payload,
                    DirectTurnRequestRow.admission_order,
                )
                .where(
                    DirectTurnRequestRow.user_id == owner_email,
                    DirectTurnRequestRow.session_id.in_(session_ids or [""]),
                    DirectTurnRequestRow.status.in_(
                        ["queued", "claimed", "running", "absorbing", "recoverable"]
                    ),
                )
                .order_by(DirectTurnRequestRow.admission_order.desc())
            )
        ).all()
    resolved: dict[
        str,
        tuple[str | None, str | None, str | None, str | None, str | None],
    ] = {}
    for row in sorted(turn_rows, key=lambda item: int(item[-1]), reverse=True):
        if len(row) == 4:
            session_id, _status, payload, _order = row
        else:
            session_id, payload, _order = row
        if not session_id or str(session_id) in resolved or not isinstance(payload, dict):
            continue
        runtime_info = payload.get("metadata")
        runtime_info = runtime_info if isinstance(runtime_info, dict) else {}
        profile_id = _nonempty_string(runtime_info.get("channel_default_agent_profile_id"))
        if profile_id is not None:
            resolved[str(session_id)] = (None, None, profile_id, None, None)
    agents = {
        row.agent_id: row
        for row in (
            await db.scalars(
                select(Agent).where(
                    Agent.owner_email == owner_email,
                    Agent.agent_id.in_({row.agent_id for row in session_rows} or {""}),
                )
            )
        ).all()
    }
    for row in session_rows:
        metadata = row.delegation_metadata if isinstance(row.delegation_metadata, dict) else {}
        model, effort, profile_id, display_name, avatar_url = resolved.get(
            row.session_id, (None, None, None, None, None)
        )
        runtime_info = (
            session_cache.get_tool_runtime_info(row.session_id)
            if session_cache is not None and hasattr(session_cache, "get_tool_runtime_info")
            else None
        )
        runtime_info = runtime_info if isinstance(runtime_info, dict) else {}
        model = _nonempty_string(getattr(row, "model_override", None)) or model
        persisted_effort = normalize_reasoning_effort(
            getattr(row, "reasoning_effort_override", None)
        )
        effort = persisted_effort if persisted_effort is not None else effort
        model = model or _nonempty_string(runtime_info.get("resolved_model"))
        effort = effort or normalize_reasoning_effort(runtime_info.get("current_reasoning_effort"))
        profile_id = profile_id or _nonempty_string(runtime_info.get("agent_profile_id"))
        model = model or _nonempty_string(metadata.get("resolved_model") or metadata.get("model"))
        effort = effort or normalize_reasoning_effort(metadata.get("reasoning_effort"))
        recorded_model, recorded_effort = model, effort
        agent = agents.get(row.agent_id)
        profile_id = (
            row.agent_profile_id
            or profile_id
            or (agent.default_agent_profile_id if agent is not None else None)
        )
        if agent is not None and profile_id:
            profiles = agent.agent_profiles if isinstance(agent.agent_profiles, dict) else {}
            profile = profiles.get(profile_id)
            if isinstance(profile, dict):
                model = model or _nonempty_string(profile.get("model"))
                effort = effort or normalize_reasoning_effort(profile.get("reasoning_effort"))
        if agent is not None and isinstance(agent.llm_config, dict):
            model = model or _nonempty_string(agent.llm_config.get("model"))
            effort = effort or normalize_reasoning_effort(agent.llm_config.get("reasoning_effort"))
        if agent is not None:
            display_name = agent.display_name or agent.name
            avatar_url = agent.avatar_url
        else:
            system_agent = SYSTEM_AGENTS.get(row.agent_id)
            if system_agent is not None:
                display_name = system_agent.name
                if system_agent.llm_config is not None:
                    model = model or _nonempty_string(system_agent.llm_config.model)
                    effort = effort or normalize_reasoning_effort(
                        system_agent.llm_config.reasoning_effort
                    )
        execution_runtime = metadata.get("execution_runtime")
        if row.delegation_mode is not None or row.status != "active":
            # Mutable agent defaults do not establish historical execution.
            model, effort = recorded_model, recorded_effort
        if (
            isinstance(execution_runtime, dict)
            and execution_runtime.get("session_id") == row.session_id
        ):
            # Resolved execution identity outranks cache and mutable defaults.
            model = _nonempty_string(execution_runtime.get("model"))
            effort = normalize_reasoning_effort(execution_runtime.get("reasoning_effort"))
            profile_id = _nonempty_string(execution_runtime.get("profile_id"))
        else:
            # A prospective selection or copied metadata is not execution evidence.
            model = None
            effort = None
            profile_id = None
        if any(
            value is not None for value in (model, effort, profile_id, display_name, avatar_url)
        ):
            resolved[row.session_id] = (
                model,
                effort,
                profile_id,
                display_name,
                avatar_url,
            )
    return resolved


def _nonempty_string(value: Any) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


async def _hydrate_file_items(
    db: AsyncSession,
    *,
    records: list[WorkRecordRow],
    items: list[TimelineItem],
) -> list[TimelineItem]:
    record_ids = [record.work_record_id for record in records]
    if not record_ids:
        return items
    rows = (
        await db.scalars(
            select(WorkRecordFileRow)
            .where(WorkRecordFileRow.work_record_id.in_(record_ids))
            .order_by(
                WorkRecordFileRow.work_record_id,
                WorkRecordFileRow.file_ordinal,
            )
        )
    ).all()
    by_record: dict[str, list[WorkRecordFileRow]] = {}
    for row in rows:
        by_record.setdefault(row.work_record_id, []).append(row)
    hydrated: list[TimelineItem] = []
    for record, item in zip(records, items, strict=True):
        if not isinstance(item, ToolCallTimelineItem):
            hydrated.append(item)
            continue
        parent_diffs = item.file_diffs
        file_diffs: list[FileDiffRef] = []
        for row in by_record.get(record.work_record_id, []):
            parent = (
                parent_diffs[row.file_ordinal] if row.file_ordinal < len(parent_diffs) else None
            )
            if parent is not None:
                file_diffs.append(
                    parent.model_copy(
                        update={
                            "path": row.path,
                            "path_id": row.path_id,
                            "path_generation_id": row.path_generation_id,
                            "additions": row.additions,
                            "deletions": row.deletions,
                            "status": row.status,
                            "old_path": row.old_path,
                            "binary": row.binary,
                            "generated": row.generated,
                            "truncated": row.truncated,
                            "preview_omitted": row.preview_omitted,
                        }
                    )
                )
            else:
                file_diffs.append(
                    FileDiffRef(
                        path=row.path,
                        path_id=row.path_id,
                        path_generation_id=row.path_generation_id,
                        diff="",
                        additions=row.additions,
                        deletions=row.deletions,
                        status=row.status,
                        old_path=row.old_path,
                        binary=row.binary,
                        generated=row.generated,
                        truncated=row.truncated,
                        content_truncated=True,
                        preview_omitted=True,
                        preview_omission_reason="not_persisted",
                    )
                )
        hydrated.append(item.model_copy(update={"file_diffs": file_diffs}))
    return hydrated


def _record_is_older_than(record: WorkRecordRow, older_key: list[Any]) -> bool:
    occurred_at = record.occurred_at
    if occurred_at.tzinfo is None:
        occurred_at = occurred_at.replace(tzinfo=UTC)
    boundary_time = datetime.fromisoformat(str(older_key[0]))
    if boundary_time.tzinfo is None:
        boundary_time = boundary_time.replace(tzinfo=UTC)
    boundary = (
        boundary_time,
        str(older_key[1]),
        int(older_key[2]),
        int(older_key[3]),
        str(older_key[4]),
    )
    value = (
        occurred_at,
        record.session_id,
        record.source_seq,
        record.item_ordinal,
        record.work_record_id,
    )
    return value < boundary


__all__ = [
    "WorkLifecycleProjection",
    "WorkCursorError",
    "WorkDatabasePage",
    "enrich_workstream_lifecycle",
    "read_activity_overview",
    "read_work_projection_states",
    "read_work_page",
]
