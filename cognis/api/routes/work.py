"""Cache-authorized lazy Work file-history route."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any, Literal

from fastapi import APIRouter, Query, Request
from sqlalchemy import and_, case, func, or_, select, tuple_
from sqlalchemy.orm import aliased

from cognis.api.chat_v2.event_store import (
    SessionHistoryUnavailableError,
    require_session_history,
)
from cognis.api.chat_v2.normalizer import normalize_session_events
from cognis.api.chat_v2.projector import project_timeline
from cognis.api.chat_v2.schemas import (
    FileDiffRef,
    ToolCallTimelineItem,
    WorkActivityAgentRef,
    WorkActivityItem,
    WorkActivityListResponse,
    WorkActivityProjectRef,
    WorkActivityRootRef,
    WorkFileHistoryRequest,
    WorkFileHistoryResponse,
    WorkRefreshRequest,
    WorkRefreshResponse,
    WorkSummary,
)
from cognis.api.chat_v2.scope_projection import (
    project_session_scope,
    project_task_step_scope,
)
from cognis.api.chat_v2.work_materializer import WORK_MATERIALIZER_VERSION
from cognis.api.chat_v2.work_projection import build_work_projection
from cognis.api.common import api_exception, require_current_user
from cognis.providers.guardrails.events import EventStoreAuthority
from cognis.store.models import (
    Agent,
    Conversation,
    ProjectRow,
    Session,
    StepRun,
    Task,
    WorkCurrentFileRow,
    WorkRecordFileRow,
    WorkRecordRow,
    WorkSessionProjectionRow,
)

router = APIRouter(prefix="/api/v1/work", tags=["work"])


@router.post("/refresh", response_model=WorkRefreshResponse, status_code=202)
async def refresh_work(request: Request, payload: WorkRefreshRequest) -> WorkRefreshResponse:
    """Prioritize authorized SQL-visible Work sessions for background recovery."""

    user = require_current_user(request)
    graph = await request.app.state.work_graph_resolver.resolve(
        user_email=user.email,
        scope=payload.scope,
    )
    await request.app.state.work_materializer.prioritize_sessions(graph.session_rows)
    return WorkRefreshResponse(scope=payload.scope, session_count=len(graph.session_rows))


def _cursor(payload: dict[str, Any], secret: str) -> str:
    body = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    signature = hmac.new(secret.encode(), body, hashlib.sha256).digest()
    return base64.urlsafe_b64encode(body + signature).decode().rstrip("=")


def _decode_cursor(value: str, secret: str) -> dict[str, Any]:
    try:
        raw = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
        body, signature = raw[:-32], raw[-32:]
        if not hmac.compare_digest(
            signature, hmac.new(secret.encode(), body, hashlib.sha256).digest()
        ):
            raise ValueError
        payload = json.loads(body)
        if not isinstance(payload, dict):
            raise ValueError
        return payload
    except Exception as exc:
        raise api_exception(409, "cursor_invalid", "File-history cursor is invalid") from exc


def _decode_activity_cursor(value: str, secret: str, owner_email: str) -> tuple[datetime, str]:
    try:
        payload = _decode_cursor(value, secret)
        if set(payload) != {"v", "owner", "last_activity_at", "activity_scope_id"}:
            raise ValueError
        if payload["v"] != 1 or payload["owner"] != owner_email:
            raise ValueError
        timestamp = datetime.fromisoformat(payload["last_activity_at"])
        if timestamp.tzinfo is None:
            timestamp = timestamp.replace(tzinfo=UTC)
        activity_scope_id = payload["activity_scope_id"]
        if not isinstance(activity_scope_id, str) or not activity_scope_id:
            raise ValueError
        return timestamp, activity_scope_id
    except Exception as exc:
        raise api_exception(409, "cursor_invalid", "Activity-list cursor is invalid") from exc


def _activity_cursor(
    *,
    secret: str,
    owner_email: str,
    last_activity_at: datetime,
    activity_scope_id: str,
) -> str:
    return _cursor(
        {
            "v": 1,
            "owner": owner_email,
            "last_activity_at": last_activity_at.isoformat(),
            "activity_scope_id": activity_scope_id,
        },
        secret,
    )


@router.get("/activities", response_model=WorkActivityListResponse)
async def list_work_activities(
    request: Request,
    limit: int = Query(default=20, ge=1, le=100),
    cursor: str | None = Query(default=None, max_length=4096),
) -> WorkActivityListResponse:
    """List canonical owner activities without reading or recovering event history."""

    user = require_current_user(request)
    owner_email = user.email
    secret = request.app.state.chat_v2_cursor_secret
    before = _decode_activity_cursor(cursor, secret, owner_email) if cursor else None

    latest_rank = func.row_number().over(
        partition_by=Session.activity_scope_id,
        order_by=(Session.updated_at.desc(), Session.session_id.desc()),
    )
    member_rows = (
        select(
            Session.session_id.label("session_id"),
            Session.activity_scope_id.label("activity_scope_id"),
            Session.conversation_id.label("conversation_id"),
            Session.status.label("session_status"),
            Session.updated_at.label("updated_at"),
            func.max(Session.updated_at)
            .over(partition_by=Session.activity_scope_id)
            .label("last_activity_at"),
            latest_rank.label("latest_rank"),
        )
        .where(Session.user_email == owner_email)
        .cte("activity_members")
    )
    latest_members = (
        select(member_rows).where(member_rows.c.latest_rank == 1).cte("latest_activity_members")
    )

    task_activity_at = case(
        (Task.updated_at > StepRun.updated_at, Task.updated_at),
        else_=StepRun.updated_at,
    )
    task_rank = func.row_number().over(
        partition_by=Session.activity_scope_id,
        order_by=(
            task_activity_at.desc(),
            StepRun.attempt_number.desc(),
            Task.task_id.desc(),
            StepRun.step_run_id.desc(),
        ),
    )
    task_candidates = (
        select(
            Session.activity_scope_id.label("activity_scope_id"),
            StepRun.step_run_id.label("step_run_id"),
            StepRun.task_id.label("task_id"),
            StepRun.session_id.label("step_session_id"),
            StepRun.conversation_id.label("step_conversation_id"),
            StepRun.step_name.label("step_name"),
            StepRun.attempt_number.label("attempt_number"),
            StepRun.status.label("step_status"),
            Session.parent_session_id.label("step_parent_session_id"),
            Task.title.label("task_title"),
            Task.status.label("task_status"),
            Task.agent_id.label("task_agent_id"),
            Task.project_id.label("task_project_id"),
            Task.updated_at.label("task_updated_at"),
            StepRun.updated_at.label("step_updated_at"),
            task_rank.label("task_rank"),
        )
        .join(StepRun, StepRun.session_id == Session.session_id)
        .join(Task, Task.task_id == StepRun.task_id)
        .where(
            Session.user_email == owner_email,
            Task.created_by == owner_email,
            StepRun.superseded_by_step_run_id.is_(None),
            or_(
                StepRun.conversation_id.is_(None),
                StepRun.conversation_id == Session.conversation_id,
            ),
        )
        .cte("activity_task_candidates")
    )
    task_root = (
        select(task_candidates).where(task_candidates.c.task_rank == 1).cte("activity_task_root")
    )

    root_session = aliased(Session, name="root_session")
    task_agent = aliased(Agent, name="task_agent")
    continuation_rank = func.row_number().over(
        partition_by=(
            Session.activity_scope_id,
            Session.parent_session_id,
            Session.agent_id,
            Session.delegation_mode,
            Session.delegation_task,
        ),
        order_by=(Session.started_at.desc(), Session.session_id.desc()),
    )
    continuation_candidates = (
        select(
            Session.activity_scope_id.label("activity_scope_id"),
            Session.parent_session_id.label("parent_session_id"),
            Session.agent_id.label("agent_id"),
            Session.delegation_mode.label("delegation_mode"),
            Session.delegation_task.label("delegation_task"),
            Session.status.label("status"),
            continuation_rank.label("continuation_rank"),
        )
        .where(Session.user_email == owner_email)
        .cte("activity_continuation_candidates")
    )
    continuation_current = (
        select(continuation_candidates)
        .where(continuation_candidates.c.continuation_rank == 1)
        .cte("activity_continuation_current")
    )
    project_id = func.coalesce(task_root.c.task_project_id, Conversation.project_id)
    canonical_session_status = case(
        (root_session.parent_session_id.is_(None), root_session.status),
        else_=continuation_current.c.status,
    )
    step_activity = case(
        (
            task_root.c.step_updated_at.is_not(None)
            & (task_root.c.step_updated_at > latest_members.c.last_activity_at),
            task_root.c.step_updated_at,
        ),
        else_=latest_members.c.last_activity_at,
    )
    last_activity_at = case(
        (
            task_root.c.task_updated_at.is_not(None)
            & (task_root.c.task_updated_at > step_activity),
            task_root.c.task_updated_at,
        ),
        else_=step_activity,
    )
    statement = (
        select(
            latest_members.c.activity_scope_id,
            last_activity_at.label("last_activity_at"),
            canonical_session_status.label("canonical_session_status"),
            root_session.conversation_id.label("root_conversation_id"),
            root_session.parent_session_id.label("root_parent_session_id"),
            root_session.delegation_task.label("root_delegation_task"),
            root_session.agent_id.label("root_agent_id"),
            Conversation.title.label("conversation_title"),
            Agent.name.label("root_agent_name"),
            Agent.display_name.label("root_agent_display_name"),
            Agent.avatar_url.label("root_agent_avatar_url"),
            ProjectRow.project_id.label("project_id"),
            ProjectRow.name.label("project_name"),
            task_root.c.step_run_id,
            task_root.c.task_id,
            task_root.c.step_session_id,
            task_root.c.step_conversation_id,
            task_root.c.step_name,
            task_root.c.attempt_number,
            task_root.c.step_status,
            task_root.c.step_parent_session_id,
            task_root.c.task_title,
            task_root.c.task_status,
            task_root.c.task_agent_id,
            task_agent.name.label("task_agent_name"),
            task_agent.display_name.label("task_agent_display_name"),
            task_agent.avatar_url.label("task_agent_avatar_url"),
        )
        .join(
            root_session,
            and_(
                root_session.session_id == latest_members.c.activity_scope_id,
                root_session.activity_scope_id == latest_members.c.activity_scope_id,
                root_session.user_email == owner_email,
            ),
        )
        .join(
            Conversation,
            and_(
                Conversation.conversation_id == root_session.conversation_id,
                Conversation.user_email == owner_email,
                Conversation.status != "deleted",
            ),
        )
        .join(Agent, Agent.agent_id == root_session.agent_id)
        .outerjoin(
            continuation_current,
            and_(
                root_session.parent_session_id.is_not(None),
                continuation_current.c.activity_scope_id == latest_members.c.activity_scope_id,
                continuation_current.c.parent_session_id == root_session.parent_session_id,
                continuation_current.c.agent_id == root_session.agent_id,
                or_(
                    continuation_current.c.delegation_mode == root_session.delegation_mode,
                    and_(
                        continuation_current.c.delegation_mode.is_(None),
                        root_session.delegation_mode.is_(None),
                    ),
                ),
                or_(
                    continuation_current.c.delegation_task == root_session.delegation_task,
                    and_(
                        continuation_current.c.delegation_task.is_(None),
                        root_session.delegation_task.is_(None),
                    ),
                ),
            ),
        )
        .outerjoin(
            task_root,
            task_root.c.activity_scope_id == latest_members.c.activity_scope_id,
        )
        .outerjoin(task_agent, task_agent.agent_id == task_root.c.task_agent_id)
        .outerjoin(
            ProjectRow,
            and_(
                ProjectRow.project_id == project_id,
                ProjectRow.owner_email == owner_email,
            ),
        )
        .order_by(
            last_activity_at.desc(),
            latest_members.c.activity_scope_id.desc(),
        )
        .limit(limit + 1)
        .execution_options(cognis_work_activity_list=True)
    )
    if before is not None:
        statement = statement.where(
            tuple_(
                last_activity_at,
                latest_members.c.activity_scope_id,
            )
            < before
        )

    async with request.app.state.session_factory() as db:
        rows = list((await db.execute(statement)).all())
        selected = rows[:limit]
        activity_ids = [row.activity_scope_id for row in selected]
        projection_rows = (
            select(
                Session.activity_scope_id.label("activity_scope_id"),
                WorkSessionProjectionRow.session_id,
                WorkSessionProjectionRow.state,
                WorkSessionProjectionRow.covered_through_seq,
                WorkSessionProjectionRow.target_seq,
                WorkSessionProjectionRow.mutation_count,
                WorkSessionProjectionRow.command_count,
                WorkSessionProjectionRow.artifact_count,
                WorkSessionProjectionRow.deliverable_count,
                WorkSessionProjectionRow.additions,
                WorkSessionProjectionRow.deletions,
                WorkSessionProjectionRow.omitted_file_count,
            )
            .outerjoin(
                WorkSessionProjectionRow,
                and_(
                    WorkSessionProjectionRow.session_id == Session.session_id,
                    WorkSessionProjectionRow.owner_email == owner_email,
                    WorkSessionProjectionRow.materializer_version == WORK_MATERIALIZER_VERSION,
                ),
            )
            .where(
                Session.user_email == owner_email,
                Session.activity_scope_id.in_(activity_ids or [""]),
            )
            .subquery("activity_projection_rows")
        )
        projection_summary = (
            select(
                projection_rows.c.activity_scope_id,
                func.count().label("session_count"),
                func.count(projection_rows.c.session_id).label("projection_count"),
                func.sum(case((projection_rows.c.state == "failed", 1), else_=0)).label(
                    "failed_count"
                ),
                func.sum(
                    case(
                        (
                            and_(
                                projection_rows.c.state == "caught_up",
                                projection_rows.c.covered_through_seq
                                >= projection_rows.c.target_seq,
                            ),
                            1,
                        ),
                        else_=0,
                    )
                ).label("live_count"),
                func.coalesce(func.sum(projection_rows.c.mutation_count), 0).label("mutations"),
                func.coalesce(func.sum(projection_rows.c.command_count), 0).label("commands"),
                func.coalesce(func.sum(projection_rows.c.artifact_count), 0).label("artifacts"),
                func.coalesce(func.sum(projection_rows.c.deliverable_count), 0).label(
                    "deliverables"
                ),
                func.coalesce(func.sum(projection_rows.c.additions), 0).label("additions"),
                func.coalesce(func.sum(projection_rows.c.deletions), 0).label("deletions"),
                func.coalesce(func.sum(projection_rows.c.omitted_file_count), 0).label(
                    "omitted_files"
                ),
            )
            .group_by(projection_rows.c.activity_scope_id)
            .subquery("activity_projection_summary")
        )
        file_summary = (
            select(
                Session.activity_scope_id.label("activity_scope_id"),
                func.count(func.distinct(WorkCurrentFileRow.path_id)).label("changed_files"),
            )
            .join(WorkCurrentFileRow, WorkCurrentFileRow.session_id == Session.session_id)
            .where(
                Session.user_email == owner_email,
                Session.activity_scope_id.in_(activity_ids or [""]),
                WorkCurrentFileRow.owner_email == owner_email,
                WorkCurrentFileRow.materializer_version == WORK_MATERIALIZER_VERSION,
                WorkCurrentFileRow.state != "deleted",
            )
            .group_by(Session.activity_scope_id)
            .subquery("activity_file_summary")
        )
        summary_rows = (
            await db.execute(
                select(
                    projection_summary.c.activity_scope_id,
                    projection_summary.c.session_count,
                    projection_summary.c.projection_count,
                    projection_summary.c.failed_count,
                    projection_summary.c.live_count,
                    projection_summary.c.mutations,
                    projection_summary.c.commands,
                    func.coalesce(file_summary.c.changed_files, 0).label("changed_files"),
                    projection_summary.c.artifacts,
                    projection_summary.c.deliverables,
                    projection_summary.c.additions,
                    projection_summary.c.deletions,
                    projection_summary.c.omitted_files,
                )
                .outerjoin(
                    file_summary,
                    file_summary.c.activity_scope_id == projection_summary.c.activity_scope_id,
                )
                .execution_options(cognis_work_activity_summary=True)
            )
        ).all()
        summaries: dict[
            str, tuple[WorkSummary, Literal["live", "catching_up", "partial", "failed"]]
        ] = {}
        for summary_row in summary_rows:
            session_count = int(summary_row.session_count)
            projection_count = int(summary_row.projection_count)
            failed_count = int(summary_row.failed_count or 0)
            live_count = int(summary_row.live_count or 0)
            materialization: Literal["live", "catching_up", "partial", "failed"]
            if failed_count == session_count and session_count:
                materialization = "failed"
            elif projection_count < session_count or failed_count:
                materialization = "partial"
            elif live_count == session_count:
                materialization = "live"
            else:
                materialization = "catching_up"
            summaries[str(summary_row.activity_scope_id)] = (
                WorkSummary(
                    mutations=int(summary_row.mutations),
                    commands=int(summary_row.commands),
                    changed_files=int(summary_row.changed_files),
                    artifacts=int(summary_row.artifacts),
                    deliverables=int(summary_row.deliverables),
                    additions=int(summary_row.additions),
                    deletions=int(summary_row.deletions),
                    omitted_files=int(summary_row.omitted_files),
                ),
                materialization,
            )

    items: list[WorkActivityItem] = []
    for row in selected:
        if row.step_run_id is not None:
            status = str(row.task_status)
            scope = project_task_step_scope(
                step_run=SimpleNamespace(
                    step_run_id=row.step_run_id,
                    session_id=row.step_session_id,
                    task_id=row.task_id,
                    step_name=row.step_name,
                    attempt_number=row.attempt_number,
                    status=row.step_status,
                ),
                session_row=SimpleNamespace(
                    parent_session_id=row.step_parent_session_id,
                ),
                conversation_id=row.step_conversation_id or row.root_conversation_id,
            )
            root = WorkActivityRootRef(
                kind="task",
                conversation_id=scope.conversation_id,
                task_id=row.task_id,
                step_run_id=row.step_run_id,
                title=row.task_title,
            )
            agent_id = str(row.task_agent_id)
            agent_name = row.task_agent_display_name or row.task_agent_name or agent_id
            agent_avatar_url = row.task_agent_avatar_url
        else:
            status = str(row.canonical_session_status)
            scope = project_session_scope(
                root_session=SimpleNamespace(
                    session_id=row.activity_scope_id,
                    conversation_id=row.root_conversation_id,
                    parent_session_id=row.root_parent_session_id,
                    delegation_task=row.root_delegation_task,
                    agent_id=row.root_agent_id,
                ),
                current_session=SimpleNamespace(status=row.canonical_session_status),
            )
            root = WorkActivityRootRef(
                kind="conversation",
                conversation_id=row.root_conversation_id,
                title=row.conversation_title,
            )
            agent_id = row.root_agent_id
            agent_name = row.root_agent_display_name or row.root_agent_name
            agent_avatar_url = row.root_agent_avatar_url
        summary, materialization = summaries[row.activity_scope_id]
        items.append(
            WorkActivityItem(
                activity_scope_id=row.activity_scope_id,
                scope=scope,
                root=root,
                agent=WorkActivityAgentRef(
                    agent_id=agent_id,
                    display_name=agent_name,
                    avatar_url=agent_avatar_url,
                ),
                project=(
                    WorkActivityProjectRef(
                        project_id=row.project_id,
                        name=row.project_name,
                    )
                    if row.project_id is not None
                    else None
                ),
                status=status,
                last_activity_at=row.last_activity_at.isoformat(),
                summary=summary,
                materialization=materialization,
            )
        )

    has_more = len(rows) > limit
    next_cursor = None
    if has_more and selected:
        last = selected[-1]
        next_cursor = _activity_cursor(
            secret=secret,
            owner_email=owner_email,
            last_activity_at=last.last_activity_at,
            activity_scope_id=last.activity_scope_id,
        )
    return WorkActivityListResponse(
        items=items,
        next_cursor=next_cursor,
        has_more=has_more,
    )


def _project_exact_file_diff(
    *,
    items: list[Any],
    source_item_id: str,
    file_ordinal: int,
    fact: WorkRecordFileRow,
    scope: Any,
    projection_version: str,
) -> FileDiffRef | None:
    source_item = next(
        (
            item
            for item in items
            if isinstance(item, ToolCallTimelineItem) and item.id == source_item_id
        ),
        None,
    )
    if source_item is None or file_ordinal >= len(source_item.file_diffs):
        return None
    selected_item = source_item.model_copy(
        update={"file_diffs": [source_item.file_diffs[file_ordinal]]}
    )
    projection = build_work_projection(
        scope=scope,
        projection_version=projection_version,
        items=[selected_item],
        tool_definitions={},
        has_more_before=False,
        before_cursor=None,
        server_time="",
    )
    if not projection.mutations or not projection.mutations[0].file_diffs:
        return None
    return (
        projection.mutations[0]
        .file_diffs[0]
        .model_copy(
            update={
                "path": fact.path,
                "path_id": fact.path_id,
                "path_generation_id": fact.path_generation_id,
                "old_path": fact.old_path,
                "additions": fact.additions,
                "deletions": fact.deletions,
                "status": fact.status,
                "binary": fact.binary,
                "generated": fact.generated,
                "truncated": fact.truncated,
                "source_item_id": source_item_id,
            }
        )
    )


@router.post("/file-history", response_model=WorkFileHistoryResponse)
async def work_file_history(
    request: Request, payload: WorkFileHistoryRequest
) -> WorkFileHistoryResponse:
    user = require_current_user(request)
    secret = request.app.state.chat_v2_cursor_secret
    async with request.app.state.session_factory() as db:
        if payload.scope.kind == "conversation":
            root_exists = await db.scalar(
                select(Conversation.conversation_id).where(
                    Conversation.conversation_id == payload.scope.conversation_id,
                    Conversation.user_email == user.email,
                    Conversation.status != "deleted",
                )
            )
        elif payload.scope.kind == "session":
            root_exists = await db.scalar(
                select(Session.session_id).where(
                    Session.session_id == payload.scope.session_id,
                    Session.user_email == user.email,
                )
            )
        else:
            root_exists = await db.scalar(
                select(StepRun.step_run_id)
                .join(Task, Task.task_id == StepRun.task_id)
                .where(
                    StepRun.step_run_id == payload.scope.step_run_id,
                    StepRun.task_id == payload.scope.task_id,
                    Task.created_by == user.email,
                )
            )
        if root_exists is None:
            raise api_exception(404, "not_found", "Authorized Work root was not found")

    graph = await request.app.state.work_graph_resolver.resolve(
        user_email=user.email,
        scope=payload.scope,
    )
    session_ids = [row.session_id for row in graph.session_rows]
    session_ordinals = {session_id: ordinal for ordinal, session_id in enumerate(session_ids)}
    session_ordinal = case(session_ordinals, value=WorkRecordFileRow.session_id)
    before: list[Any] | None = None
    if payload.before:
        decoded = _decode_cursor(payload.before, secret)
        identity = (
            payload.scope.key,
            graph.fingerprint,
            payload.path_generation_id,
        )
        if tuple(decoded.get("identity", ())) != identity:
            raise api_exception(
                409, "cursor_invalid", "File-history cursor does not match the active cache"
            )
        raw_before = decoded.get("before")
        if not isinstance(raw_before, list) or len(raw_before) != 6:
            raise api_exception(409, "cursor_invalid", "File-history cursor is invalid")
        before = raw_before

    async with request.app.state.session_factory() as db:
        statement = (
            select(
                WorkRecordFileRow,
                WorkRecordRow,
                Session,
                Agent,
                session_ordinal.label("session_ordinal"),
            )
            .join(
                WorkRecordRow,
                WorkRecordRow.work_record_id == WorkRecordFileRow.work_record_id,
            )
            .join(Session, Session.session_id == WorkRecordFileRow.session_id)
            .join(Agent, Agent.agent_id == Session.agent_id)
            .join(
                WorkSessionProjectionRow,
                (WorkSessionProjectionRow.session_id == WorkRecordFileRow.session_id)
                & (WorkSessionProjectionRow.materializer_version == WORK_MATERIALIZER_VERSION),
            )
            .where(
                WorkRecordFileRow.owner_email == user.email,
                WorkRecordFileRow.materializer_version == WORK_MATERIALIZER_VERSION,
                WorkRecordFileRow.path_generation_id == payload.path_generation_id,
                WorkRecordFileRow.session_id.in_(session_ids or [""]),
                WorkRecordFileRow.source_seq <= WorkSessionProjectionRow.covered_through_seq,
                Session.user_email == user.email,
            )
            .order_by(
                session_ordinal.desc(),
                WorkRecordFileRow.session_id.desc(),
                WorkRecordFileRow.source_seq.desc(),
                WorkRecordFileRow.item_ordinal.desc(),
                WorkRecordFileRow.file_ordinal.desc(),
                WorkRecordFileRow.work_record_file_id.desc(),
            )
            .limit(payload.limit + 1)
        )
        if before is not None:
            statement = statement.where(
                tuple_(
                    session_ordinal,
                    WorkRecordFileRow.session_id,
                    WorkRecordFileRow.source_seq,
                    WorkRecordFileRow.item_ordinal,
                    WorkRecordFileRow.file_ordinal,
                    WorkRecordFileRow.work_record_file_id,
                )
                < tuple(before)
            )
        rows = list((await db.execute(statement)).all())
    if not rows:
        async with request.app.state.session_factory() as db:
            retained_path = await db.scalar(
                select(WorkCurrentFileRow.path_generation_id).where(
                    WorkCurrentFileRow.owner_email == user.email,
                    WorkCurrentFileRow.session_id.in_(session_ids or [""]),
                    WorkCurrentFileRow.path_generation_id == payload.path_generation_id,
                )
            )
        if retained_path is not None:
            raise api_exception(410, "history_unavailable", "File history is no longer retained")
        raise api_exception(404, "not_found", "File path generation was not found")
    has_more = len(rows) > payload.limit
    selected = rows[: payload.limit]
    diffs: list[FileDiffRef] = []
    for fact, record, session, agent, _ordinal in selected:
        reader = request.app.state.cached_event_store.bind(
            EventStoreAuthority(
                user_email=user.email,
                agent_id=session.agent_id,
                agent_owner_email=agent.owner_email,
            )
        )
        try:
            page = await reader.read_session_events(
                session_id=record.source_session_id,
                before_seq=fact.source_seq + 1,
                limit=1,
                direction="backward",
            )
            require_session_history(
                page.availability, from_seq=fact.source_seq, to_seq=fact.source_seq
            )
        except SessionHistoryUnavailableError as exc:
            raise api_exception(
                410, "history_unavailable", "File history is no longer retained"
            ) from exc
        event = next((item for item in page.events if item.seq == fact.source_seq), None)
        if event is None:
            raise api_exception(
                503, "event_store_unavailable", "File history is temporarily unavailable"
            )
        timeline = project_timeline(normalize_session_events([event]).events).timeline
        diff = _project_exact_file_diff(
            items=timeline.items,
            source_item_id=record.source_item_id,
            file_ordinal=fact.file_ordinal,
            fact=fact,
            scope=payload.scope,
            projection_version=WORK_MATERIALIZER_VERSION,
        )
        if diff is None:
            raise api_exception(
                503, "event_store_unavailable", "Normalized file history is inconsistent"
            )
        diffs.append(diff)
    last_fact, _record, _session, _agent, last_ordinal = selected[-1]
    next_cursor = (
        _cursor(
            {
                "identity": [
                    payload.scope.key,
                    graph.fingerprint,
                    payload.path_generation_id,
                ],
                "before": [
                    last_ordinal,
                    last_fact.session_id,
                    last_fact.source_seq,
                    last_fact.item_ordinal,
                    last_fact.file_ordinal,
                    last_fact.work_record_file_id,
                ],
            },
            secret,
        )
        if has_more
        else None
    )
    return WorkFileHistoryResponse(
        scope=payload.scope,
        path_generation_id=payload.path_generation_id,
        items=diffs,
        before_cursor=next_cursor,
        has_more_before=has_more,
    )
