"""PostgreSQL-only reads for the durable Work projection."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from pydantic import TypeAdapter
from sqlalchemy import and_, case, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from cognis.api.chat_v2.schemas import (
    ActivityOverviewDetail,
    ActivityOverviewResponse,
    ActivityRecentItem,
    ActivityRecentWork,
    ArtifactTimelineItem,
    FileDiffRef,
    TimelineItem,
    TimelineScope,
    ToolCallTimelineItem,
    WorkCategory,
    WorkMaterialization,
    WorkstreamRef,
    WorkSummary,
)
from cognis.api.chat_v2.sync import current_projection_version
from cognis.api.chat_v2.work_materializer import (
    WORK_MATERIALIZER_VERSION,
    lock_work_projection_state,
)
from cognis.api.chat_v2.work_projection import build_work_projection
from cognis.models.tool import ToolDefinition
from cognis.providers.llm.reasoning import normalize_reasoning_effort
from cognis.store.models import (
    Agent,
    ArtifactRecordRow,
    DirectTurnRequestRow,
    ManagedConversationLink,
    Session,
    WorkRecordFileRow,
    WorkRecordRow,
    WorkSessionProjectionRow,
)

_TIMELINE_ADAPTER = TypeAdapter(TimelineItem)
ACTIVITY_OVERVIEW_COMMAND_PREVIEW_MAX_BYTES = 4 * 1024


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
    summary: WorkSummary | None = None
    category: WorkCategory | None = None


@dataclass(frozen=True)
class _ProjectionState:
    state: str
    covered_through_seq: int
    target_seq: int
    next_retry_at: datetime | None = None


@dataclass(frozen=True)
class _LogicalProjection:
    workstreams: list[WorkstreamRef]
    physical_to_logical: dict[str, str]
    members_by_logical: dict[str, tuple[str, ...]]
    ambiguous: bool


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


async def ensure_work_projection_states(
    db: AsyncSession, *, rows: list[Session]
) -> list[WorkSessionProjectionRow]:
    states: list[WorkSessionProjectionRow] = []
    for session_id in sorted({row.session_id for row in rows}):
        await lock_work_projection_state(db, session_id)
    existing = {
        row.session_id: row
        for row in (
            await db.scalars(
                select(WorkSessionProjectionRow).where(
                    WorkSessionProjectionRow.session_id.in_(
                        [item.session_id for item in rows] or [""]
                    ),
                    WorkSessionProjectionRow.materializer_version == WORK_MATERIALIZER_VERSION,
                )
            )
        ).all()
    }
    for row in rows:
        state = existing.get(row.session_id)
        if state is None:
            digest = hashlib.sha256(
                f"{row.session_id}:{WORK_MATERIALIZER_VERSION}".encode()
            ).hexdigest()[:40]
            state = WorkSessionProjectionRow(
                projection_id=f"wsp_{digest}",
                owner_email=row.user_email,
                session_id=row.session_id,
                source_session_id=row.intaris_session_id or row.session_id,
                materializer_version=WORK_MATERIALIZER_VERSION,
                state="pending",
                priority=100,
            )
            db.add(state)
        elif state.state != "caught_up":
            state.priority = max(state.priority, 100)
        states.append(state)
    await db.flush()
    return states


def _materialization(states: list[WorkSessionProjectionRow]) -> WorkMaterialization:
    total = len(states)
    completed = sum(
        state.state == "caught_up" and state.covered_through_seq >= state.target_seq
        for state in states
    )
    failed = sum(state.state == "failed" for state in states)
    repairs = sum(state.state == "repair" for state in states)
    if failed:
        status = "failed"
    elif repairs:
        status = "repair"
    elif completed == total:
        status = "caught_up"
    else:
        status = "materializing"
    retry_dates = [state.next_retry_at for state in states if state.next_retry_at is not None]
    retry_after_ms = None
    if retry_dates:
        retry_after_ms = max(0, int((min(retry_dates) - datetime.now(UTC)).total_seconds() * 1000))
    return WorkMaterialization(
        state=status,
        completed_streams=completed,
        total_streams=total,
        covered_events=sum(state.covered_through_seq for state in states),
        target_events=sum(state.target_seq for state in states),
        failed_streams=failed,
        retry_after_ms=retry_after_ms,
    )


async def read_activity_overview(
    db: AsyncSession,
    *,
    owner_email: str,
    scope: TimelineScope,
    session_rows: list[Session],
    workstreams: list[WorkstreamRef],
    graph_fingerprint: str,
    graph_truncated: bool,
    tool_definitions: Mapping[str, ToolDefinition] | None = None,
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
    state_rows = list(
        (
            await db.scalars(
                select(WorkSessionProjectionRow).where(
                    WorkSessionProjectionRow.session_id.in_(session_ids or [""]),
                    WorkSessionProjectionRow.materializer_version == WORK_MATERIALIZER_VERSION,
                )
            )
        ).all()
    )
    states_by_session = {row.session_id: row for row in state_rows}
    materialization_states = [
        states_by_session.get(row.session_id)
        or _ProjectionState(state="pending", covered_through_seq=0, target_seq=0)
        for row in session_rows
    ]
    materialization = _materialization(materialization_states)  # type: ignore[arg-type]
    summaries = await _session_summaries(
        db,
        owner_email=owner_email,
        statement=base,
        session_ids=session_ids,
    )
    direct_ongoing = set(
        (
            await db.scalars(
                select(DirectTurnRequestRow.session_id).where(
                    DirectTurnRequestRow.user_id == owner_email,
                    DirectTurnRequestRow.session_id.in_(session_ids or [""]),
                    DirectTurnRequestRow.status.in_(
                        ["queued", "claimed", "running", "absorbing", "recoverable"]
                    ),
                )
            )
        ).all()
    )
    managed_rows = (
        await db.execute(
            select(
                ManagedConversationLink.target_session_id,
                ManagedConversationLink.conversation_state,
                ManagedConversationLink.turn_state,
            ).where(
                ManagedConversationLink.user_email == owner_email,
                ManagedConversationLink.target_session_id.in_(session_ids or [""]),
            )
        )
    ).all()
    managed_by_session = {
        str(session_id): (str(conversation_state), str(turn_state))
        for session_id, conversation_state, turn_state in managed_rows
        if session_id
    }
    runtime_by_session = await _session_runtime_metadata(
        db,
        owner_email=owner_email,
        session_rows=session_rows,
    )
    rows_by_session = {row.session_id: row for row in session_rows}
    enriched: list[WorkstreamRef] = []
    for node in workstreams:
        row = rows_by_session[node.session_id]
        managed = managed_by_session.get(node.session_id)
        ongoing = node.session_id in direct_ongoing or bool(
            managed
            and managed[1] in {"queued", "claimed", "running", "absorbing", "recoverable", "active"}
        )
        terminal = row.status in {"completed", "failed", "cancelled"} or bool(
            managed and managed[0] in {"closed", "completed", "failed", "cancelled"}
        )
        activity_state = "closed" if terminal else ("ongoing" if ongoing else "active")
        enriched.append(
            node.model_copy(
                update={
                    "summary": summaries.get(node.session_id, _empty_summary()),
                    "activity_state": activity_state,
                    "activity_scope_id": row.activity_scope_id,
                    "completion_reason": row.completion_reason,
                    "completed_at": row.completed_at.isoformat() if row.completed_at else None,
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
    recent, recent_records, recent_item_overrides = await _recent_activity(
        db,
        owner_email=owner_email,
        statement=base,
    )
    recent_items = await _hydrate_file_items(
        db,
        records=recent_records,
        items=[
            recent_item_overrides.get(record.work_record_id)
            or _TIMELINE_ADAPTER.validate_python(record.timeline_item)
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
        overview_revision=overview_revision,
        graph_truncated=graph_truncated or logical.ambiguous,
        server_time=datetime.now(UTC).isoformat(),
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
) -> WorkDatabasePage:
    authorized_session_ids = {row.session_id for row in session_rows}
    if exact_session_id is not None and exact_session_id not in authorized_session_ids:
        raise WorkCursorError("Requested Work session was not found")
    if exact_session_id is not None:
        session_rows = [row for row in session_rows if row.session_id == exact_session_id]
    states = await ensure_work_projection_states(db, rows=session_rows)
    status = _materialization(states)
    snapshot = sorted(
        (state.session_id, state.covered_through_seq, state.target_seq) for state in states
    )
    snapshot_revision = hashlib.sha256(
        json.dumps(snapshot, separators=(",", ":")).encode()
    ).hexdigest()
    complete = status.state == "caught_up"
    now = datetime.now(UTC)
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
            "snapshot": snapshot_revision,
        }
        expected.update(
            {
                "category": category,
                "from": from_time.isoformat() if from_time else None,
                "to": to_time.isoformat() if to_time else None,
                "session_id": exact_session_id,
            }
        )
        if any(payload.get(key) != value for key, value in expected.items()):
            raise WorkCursorError("Work cursor does not match the authorized graph")
        cutoff = datetime.fromisoformat(str(payload["cutoff"]))
        older_key = list(payload["older"])
        if category is not None:
            try:
                cursor_summary = WorkSummary.model_validate(payload["summary"])
            except Exception as exc:
                raise WorkCursorError("invalid Work cursor summary") from exc
    session_ids = [row.session_id for row in session_rows]
    statement = select(WorkRecordRow).where(
        WorkRecordRow.owner_email == owner_email,
        WorkRecordRow.session_id.in_(session_ids or [""]),
        WorkRecordRow.materializer_version == WORK_MATERIALIZER_VERSION,
        WorkRecordRow.is_evidence.is_(True),
        WorkRecordRow.occurred_at <= cutoff,
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
            snapshot_revision=snapshot_revision,
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
    removed_call_ids: list[str] = []
    if before is None:
        recent_call_states = (
            await db.execute(
                select(WorkRecordRow.call_id, WorkRecordRow.is_evidence)
                .where(
                    WorkRecordRow.owner_email == owner_email,
                    WorkRecordRow.session_id.in_(session_ids or [""]),
                    WorkRecordRow.materializer_version == WORK_MATERIALIZER_VERSION,
                    WorkRecordRow.call_id.is_not(None),
                )
                .order_by(WorkRecordRow.materialized_at.desc())
                .limit(1000)
            )
        ).all()
        latest_by_call: dict[str, bool] = {}
        for call_id, is_evidence in recent_call_states:
            if call_id:
                latest_by_call.setdefault(str(call_id), bool(is_evidence))
        removed_call_ids = [
            call_id for call_id, is_evidence in latest_by_call.items() if not is_evidence
        ][:500]
    hydrated_items = await _hydrate_file_items(
        db,
        records=page_records,
        items=[_TIMELINE_ADAPTER.validate_python(record.timeline_item) for record in page_records],
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
        cursor = _sign(
            {
                "owner": owner_email,
                "graph": graph_fingerprint,
                "version": WORK_MATERIALIZER_VERSION,
                "snapshot": snapshot_revision,
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
            },
            cursor_secret,
        )
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


async def _read_category_page(
    db: AsyncSession,
    *,
    statement: Any,
    owner_email: str,
    scope: TimelineScope,
    session_ids: list[str],
    graph_fingerprint: str,
    cursor_secret: str,
    snapshot_revision: str,
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
) -> WorkDatabasePage:
    del tool_definitions
    summary = cursor_summary or await _category_summary(
        db, statement=statement, owner_email=owner_email
    )
    page_statement = statement.where(WorkRecordRow.category == category)
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
    ordered = page_statement.order_by(
        WorkRecordRow.occurred_at.desc(),
        WorkRecordRow.session_id.desc(),
        WorkRecordRow.source_seq.desc(),
        WorkRecordRow.item_ordinal.desc(),
        WorkRecordRow.work_record_id.desc(),
    )
    if category == "files":
        records = list((await db.scalars(ordered)).all())
    else:
        records = list((await db.scalars(ordered.limit(limit + 1))).all())
    page_records = records if category == "files" else records[:limit]
    has_more = category != "files" and len(records) > limit
    selected = await _hydrate_file_items(
        db,
        records=page_records,
        items=[_TIMELINE_ADAPTER.validate_python(record.timeline_item) for record in page_records],
    )
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
            cursor = _sign(
                {
                    "owner": owner_email,
                    "graph": graph_fingerprint,
                    "version": WORK_MATERIALIZER_VERSION,
                    "snapshot": snapshot_revision,
                    "category": category,
                    "from": from_time.isoformat() if from_time else None,
                    "to": to_time.isoformat() if to_time else None,
                    "session_id": exact_session_id,
                    "summary": summary.model_dump(mode="json"),
                    "cutoff": cutoff.isoformat(),
                    "older": [
                        matching_record.occurred_at.isoformat(),
                        matching_record.session_id,
                        matching_record.source_seq,
                        matching_record.item_ordinal,
                        matching_record.work_record_id,
                    ],
                },
                cursor_secret,
            )
    removed_call_ids: list[str] = []
    if category == "commands" and older_key is None:
        recent_call_states = (
            await db.execute(
                select(WorkRecordRow.call_id, WorkRecordRow.is_evidence)
                .where(
                    WorkRecordRow.owner_email == owner_email,
                    WorkRecordRow.session_id.in_(session_ids or [""]),
                    WorkRecordRow.materializer_version == WORK_MATERIALIZER_VERSION,
                    WorkRecordRow.call_id.is_not(None),
                )
                .order_by(WorkRecordRow.materialized_at.desc())
                .limit(1000)
            )
        ).all()
        latest_by_call: dict[str, bool] = {}
        for call_id, is_evidence in recent_call_states:
            if call_id:
                latest_by_call.setdefault(str(call_id), bool(is_evidence))
        removed_call_ids = [
            call_id for call_id, is_evidence in latest_by_call.items() if not is_evidence
        ][:500]
    return WorkDatabasePage(
        scope=scope,
        projection_version=current_projection_version(),
        items=selected,
        removed_call_ids=removed_call_ids,
        materialization=materialization,
        has_more_before=bool(cursor),
        before_cursor=cursor,
        server_time=now.isoformat(),
        summary=summary,
        category=category,
    )


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
        WorkRecordRow.category,
        WorkRecordRow.entity_id,
        WorkRecordRow.file_path_ids,
        WorkRecordRow.additions,
        WorkRecordRow.deletions,
    ).subquery()
    counts = dict(
        (
            await db.execute(
                select(base.c.category, func.count())
                .where(base.c.category.in_(["commands", "mutations"]))
                .group_by(base.c.category)
            )
        ).all()
    )
    deliverables = int(
        await db.scalar(
            select(func.count(func.distinct(base.c.entity_id))).where(
                base.c.category == "deliverables",
                base.c.entity_id.is_not(None),
            )
        )
        or 0
    )
    artifacts = int(
        await db.scalar(
            select(func.count(func.distinct(base.c.entity_id)))
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
        )
        or 0
    )
    file_record_ids = statement.with_only_columns(WorkRecordRow.work_record_id).where(
        WorkRecordRow.category == "files"
    )
    changed_files = int(
        await db.scalar(
            select(func.count(func.distinct(WorkRecordFileRow.path_id))).where(
                WorkRecordFileRow.work_record_id.in_(file_record_ids)
            )
        )
        or 0
    )
    file_totals = (
        await db.execute(
            select(
                func.coalesce(func.sum(WorkRecordFileRow.additions), 0),
                func.coalesce(func.sum(WorkRecordFileRow.deletions), 0),
            ).where(WorkRecordFileRow.work_record_id.in_(file_record_ids))
        )
    ).one()
    return WorkSummary(
        mutations=int(counts.get("mutations", 0)),
        commands=int(counts.get("commands", 0)),
        changed_files=changed_files,
        artifacts=artifacts,
        deliverables=deliverables,
        additions=int(file_totals[0]),
        deletions=int(file_totals[1]),
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
            logical_parent = canonical_by_member.get(parent_key, parent_key)
            if logical_parent != canonical_key:
                external.append((key, logical_parent))
        canonical_external = [parent_key for key, parent_key in external if key == canonical_key]
        distinct_parents = sorted({parent_key for _key, parent_key in external})
        if len(distinct_parents) > 1:
            ambiguous = True
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
                    "kind": "root" if is_root else canonical.kind,
                    "edge_kind": "root" if is_root else canonical.edge_kind,
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
    statement: Any,
) -> tuple[
    dict[WorkCategory, list[ActivityRecentItem]],
    list[WorkRecordRow],
    dict[str, TimelineItem],
]:
    deduplicated = (
        statement.with_only_columns(
            WorkRecordRow.work_record_id,
            WorkRecordRow.source_item_id,
            WorkRecordRow.category,
            WorkRecordRow.session_id,
            WorkRecordRow.occurred_at,
            WorkRecordRow.call_id,
            WorkRecordRow.entity_id,
            func.row_number()
            .over(
                partition_by=(
                    WorkRecordRow.category,
                    func.coalesce(WorkRecordRow.entity_id, WorkRecordRow.source_item_id),
                ),
                order_by=(
                    WorkRecordRow.occurred_at.desc(),
                    WorkRecordRow.session_id.desc(),
                    WorkRecordRow.source_seq.desc(),
                    WorkRecordRow.item_ordinal.desc(),
                    WorkRecordRow.work_record_id.desc(),
                ),
            )
            .label("entity_rank"),
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
            or_(
                WorkRecordRow.category != "artifacts",
                and_(
                    ArtifactRecordRow.artifact_id.is_not(None),
                    ArtifactRecordRow.deleted_at.is_(None),
                ),
            )
        )
        .subquery()
    )
    ranked = (
        select(
            deduplicated,
            func.row_number()
            .over(
                partition_by=deduplicated.c.category,
                order_by=(
                    deduplicated.c.occurred_at.desc(),
                    deduplicated.c.session_id.desc(),
                    deduplicated.c.source_item_id.desc(),
                ),
            )
            .label("category_rank"),
        )
        .where(deduplicated.c.entity_rank == 1)
        .subquery()
    )
    rows = (
        await db.execute(
            select(ranked).where(
                ranked.c.category.in_(
                    ["files", "commands", "mutations", "artifacts", "deliverables"]
                ),
                ranked.c.category_rank <= 10,
            )
        )
    ).all()
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
    artifact_ids = {
        record.entity_id
        for record in records
        if record.category == "artifacts" and record.entity_id
    }
    artifacts = {
        row.artifact_id: row
        for row in (
            await db.scalars(
                select(ArtifactRecordRow).where(
                    ArtifactRecordRow.owner_email == owner_email,
                    ArtifactRecordRow.artifact_id.in_(artifact_ids or {""}),
                    ArtifactRecordRow.deleted_at.is_(None),
                )
            )
        ).all()
    }
    authorized_records: list[WorkRecordRow] = []
    item_overrides: dict[str, TimelineItem] = {}
    for record in records:
        if record.category == "artifacts":
            artifact = artifacts.get(record.entity_id or "")
            if artifact is None:
                continue
            item = _TIMELINE_ADAPTER.validate_python(record.timeline_item)
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
        category = str(row.category)
        item = ActivityRecentItem(
            id=str(row.source_item_id),
            category=category,  # type: ignore[arg-type]
            session_id=str(row.session_id),
            occurred_at=row.occurred_at.isoformat(),
            title=str(row.call_id or row.entity_id or category),
        )
        recent.setdefault(category, []).append(item)  # type: ignore[arg-type]
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
) -> dict[
    str,
    tuple[str | None, str | None, str | None, str | None, str | None],
]:
    session_ids = [row.session_id for row in session_rows]
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
    for session_id, payload, _order in turn_rows:
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
        model = model or _nonempty_string(metadata.get("resolved_model") or metadata.get("model"))
        effort = effort or normalize_reasoning_effort(metadata.get("reasoning_effort"))
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
                            "additions": row.additions,
                            "deletions": row.deletions,
                        }
                    )
                )
            else:
                file_diffs.append(
                    FileDiffRef(
                        path=row.path,
                        path_id=row.path_id,
                        diff="",
                        additions=row.additions,
                        deletions=row.deletions,
                        content_truncated=True,
                        preview_omitted=True,
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
    "WorkCursorError",
    "WorkDatabasePage",
    "ensure_work_projection_states",
    "read_activity_overview",
    "read_work_page",
]
