"""Bounded usage SQL; no event-store reads or reconstruction on read paths."""

from __future__ import annotations

from typing import Any

from sqlalchemy import and_, case, exists, func, literal, or_, select, true, union_all
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased
from sqlalchemy.sql.elements import ColumnElement

from cognis.core.project_access import _MANAGED_CONTEXT_TYPES, PROJECT_GRANT_ROLES
from cognis.core.usage.access import AuthorizedUsageScope
from cognis.core.usage.query_service_errors import UsageQueryError
from cognis.models.usage import UsageIdentity
from cognis.store.models import (
    Conversation,
    ProjectGrantRow,
    ProjectRow,
    ScheduleFireRow,
    Session,
    StepRun,
    Task,
    User,
)
from cognis.store.usage.models import UsageAttemptRow


def subject_key(email: str) -> str:
    """Use the common subject codec, never decode the opaque accounting owner."""
    return UsageIdentity(
        principal_type="user",
        principal_subject=email,
        usage_subject_kind="user",
        usage_subject_id=email,
    ).usage_subject_key


def step_session_lineage(*, project_id: str | None = None, step_run_id: str | None = None) -> Any:
    """Follow same-owner/conversation rotations and descendants, never parent edges."""
    root, child = aliased(Session), aliased(Session)
    seed = select(
        StepRun.step_run_id,
        StepRun.task_id,
        root.session_id,
        root.conversation_id,
        root.user_email,
        root.previous_session_id,
    ).join(root, root.session_id == StepRun.session_id)
    if project_id is not None:
        seed = seed.join(Task, Task.task_id == StepRun.task_id).where(Task.project_id == project_id)
    if step_run_id is not None:
        seed = seed.where(StepRun.step_run_id == step_run_id)
    lineage = seed.cte(recursive=True)
    return lineage.union(
        select(
            lineage.c.step_run_id,
            lineage.c.task_id,
            child.session_id,
            child.conversation_id,
            child.user_email,
            child.previous_session_id,
        ).join(
            child,
            and_(
                or_(
                    child.parent_session_id == lineage.c.session_id,
                    child.previous_session_id == lineage.c.session_id,
                    child.session_id == lineage.c.previous_session_id,
                ),
                child.conversation_id == lineage.c.conversation_id,
                child.user_email == lineage.c.user_email,
            ),
        )
    )


async def accounting_parent_step(
    session: AsyncSession, step_run_id: str, session_id: str, actor: str, conversation_id: str
) -> tuple[str, str] | None:
    """Recover a delegate's accounting binding without changing its permissions."""
    lineage = step_session_lineage(step_run_id=step_run_id)
    row = (
        await session.execute(
            select(lineage.c.task_id, lineage.c.step_run_id)
            .join(Task, Task.task_id == lineage.c.task_id)
            .where(
                lineage.c.session_id == session_id,
                lineage.c.user_email == actor,
                lineage.c.conversation_id == conversation_id,
                Task.created_by == actor,
                Task.status != "deleted",
            )
        )
    ).first()
    return (row[0], row[1]) if row is not None else None


def project_resources(source: Any, project_id: str) -> Any:
    """Current explicit visibility, including private-author and child fences."""
    conversation = aliased(Conversation)
    physical = aliased(Session)
    task = aliased(Task)

    def visible_conversation(identifier: Any) -> Any:
        return exists(
            select(1).where(
                conversation.conversation_id == identifier,
                conversation.project_id == project_id,
                conversation.visibility == "project",
                conversation.status != "deleted",
                conversation.context_type.not_in(_MANAGED_CONTEXT_TYPES),
                conversation.context_data["app_contact_binding_id"].as_string().is_(None),
            )
        )

    physical_visible = exists(
        select(1).where(
            physical.session_id == source.session_id,
            or_(
                source.conversation_id.is_(None), physical.conversation_id == source.conversation_id
            ),
            or_(source.agent_id.is_(None), physical.agent_id == source.agent_id),
            visible_conversation(physical.conversation_id),
        )
    )
    task_visible = exists(
        select(1).where(
            task.task_id == source.task_id,
            task.project_id == project_id,
            task.visibility == "project",
            task.status != "deleted",
        )
    )
    bindings = [
        source.project_id == project_id,
        or_(source.conversation_id.is_(None), visible_conversation(source.conversation_id)),
        or_(source.session_id.is_(None), physical_visible),
        or_(source.task_id.is_(None), task_visible),
        or_(
            source.conversation_id.is_not(None),
            source.session_id.is_not(None),
            source.task_id.is_not(None),
        ),
    ]
    if hasattr(source, "step_run_id"):
        step = aliased(StepRun)
        lineage = step_session_lineage(project_id=project_id)
        bindings.append(
            or_(
                source.step_run_id.is_(None),
                exists(
                    select(1).where(
                        step.step_run_id == source.step_run_id,
                        step.task_id == source.task_id,
                        or_(
                            step.session_id.is_(None),
                            step.session_id == source.session_id,
                            exists(
                                select(1).where(
                                    lineage.c.step_run_id == step.step_run_id,
                                    lineage.c.task_id == source.task_id,
                                    lineage.c.session_id == source.session_id,
                                    lineage.c.conversation_id == source.conversation_id,
                                )
                            ),
                        ),
                        or_(
                            step.conversation_id.is_(None),
                            step.conversation_id == source.conversation_id,
                        ),
                    )
                ),
            )
        )
    if hasattr(source, "binding_provenance"):
        bindings.append(source.binding_provenance.in_(["controller", "authorized_binding"]))
    return and_(*bindings)


def scope_predicate(source: Any, scope: AuthorizedUsageScope) -> Any:
    """Apply the consumer fence independently of provider/payer ownership."""
    predicates = []
    predicates.append(
        exists(
            select(1).where(
                User.email == scope.actor,
                User.is_active.is_(True),
                User.disabled_at.is_(None),
                User.role == "admin" if scope.mode == "admin" else true(),
            )
        )
    )
    if scope.project_id is not None and scope.mode != "admin":
        predicates.append(
            exists(
                select(1).where(
                    ProjectRow.project_id == scope.project_id,
                    ProjectRow.status != "deleted",
                    or_(
                        ProjectRow.owner_email == scope.actor,
                        exists(
                            select(1).where(
                                ProjectGrantRow.project_id == ProjectRow.project_id,
                                ProjectGrantRow.grantee_type == "user",
                                ProjectGrantRow.grantee_user_email == scope.actor,
                                ProjectGrantRow.permission.in_([*PROJECT_GRANT_ROLES, "use"]),
                                ProjectGrantRow.revoked_at.is_(None),
                            )
                        ),
                    ),
                )
            )
        )
    if scope.consumer is not None:
        predicates.append(source.usage_subject_key == subject_key(scope.consumer))
    if scope.project_id is not None:
        predicates.append(
            project_resources(source, scope.project_id)
            if scope.mode == "project"
            else source.project_id == scope.project_id
        )
    if scope.invocation is not None:
        predicates.append(source.agent_id == scope.invocation.agent_id)
        for name in ("conversation_id", "task_id"):
            value = getattr(scope.invocation, name)
            if value is not None:
                predicates.append(getattr(source, name) == value)
    return and_(*predicates) if predicates else true()


FILTER_COLUMNS = {
    "subject_keys": "usage_subject_key",
    "provider_ids": "provider_id",
    "model_ids": "effective_model",
    "project_ids": "project_id",
    "agent_ids": "agent_id",
    "profile_ids": "agent_profile_id",
    "efforts": "effective_effort",
    "purposes": "purpose",
    "hook_ids": "hook_id",
    "service_ids": "service_id",
    "operations": "service_operation",
    "conversation_ids": "conversation_id",
    "session_ids": "session_id",
    "task_ids": "task_id",
    "step_run_ids": "step_run_id",
    "turn_ids": "turn_id",
    "cycle_ids": "cycle_id",
    "key_ids": "traffic_key_id",
    "client_ids": "traffic_client_id",
    "traffic_origins": "traffic_origin",
    "auth_types": "traffic_auth_type",
    "logical_request_ids": "request_id",
    "attempt_ids": "attempt_id",
}


def attempt_state() -> Any:
    """Start observation is not proof of dispatch or an admitted durable request."""
    return case(
        (UsageAttemptRow.outcome == "success", "succeeded"),
        (UsageAttemptRow.outcome == "error", "failed"),
        (UsageAttemptRow.outcome == "cancelled", "cancelled"),
        (UsageAttemptRow.finalized_at.is_not(None), "unknown"),
        (
            UsageAttemptRow.dispatch_status.in_(["dispatch_observed", "provider_accepted"]),
            "inflight",
        ),
        else_="observed_start",
    )


def filter_predicates(source: Any, filters: Any, scope: AuthorizedUsageScope) -> list[Any]:
    """Closed catalog only. Unsupported strata never fall through to a scan."""
    predicates = []
    for name, values in filters.model_dump(exclude_none=True).items():
        if not values:
            continue
        if name == "subject_emails":
            predicates.append(
                source.usage_subject_key.in_([subject_key(value) for value in values])
            )
        elif name == "provider_owner_emails":
            if scope.mode != "admin" or not hasattr(source, "provider_payer"):
                raise UsageQueryError(
                    "unsupported_usage_filter", "Provider owner requires admin detail"
                )
            predicates.append(source.provider_payer.in_(values))
        elif name == "schedule_ids":
            predicates.append(task_schedule(source).in_(values))
        elif name == "agent_scope":
            predicates.append(source.agent_id.is_(None))
        elif name == "task_origin":
            scheduled = exists(select(1).where(ScheduleFireRow.task_id == source.task_id))
            if values == "scheduled":
                predicates.append(scheduled)
            else:
                predicates.extend(
                    [
                        ~scheduled,
                        exists(
                            select(1).where(
                                Task.task_id == source.task_id,
                                or_(Task.source_type.is_(None), Task.source_type != "scheduler"),
                            )
                        ),
                    ]
                )
        elif name == "states" and source is UsageAttemptRow:
            predicates.append(attempt_state().in_(values))
        elif name == "error_categories" and source is UsageAttemptRow:
            error = case(
                (source.error_category == "transport", "connection"),
                (source.error_category == "provider_unavailable", "provider"),
                (source.error_category == "permission", "authentication"),
                (source.error_category == "context_limit", "invalid_request"),
                else_=source.error_category,
            )
            predicates.append(error.in_(values))
        elif name in FILTER_COLUMNS and hasattr(source, FILTER_COLUMNS[name]):
            predicates.append(getattr(source, FILTER_COLUMNS[name]).in_(values))
        else:
            raise UsageQueryError(
                "unsupported_usage_filter", "Filter is not supported by this source"
            )
    return predicates


def task_schedule(source: Any) -> Any:
    """A task has one schedule bucket; conflicting links never multiply spend."""
    return (
        select(func.min(ScheduleFireRow.schedule_id))
        .where(ScheduleFireRow.task_id == source.task_id)
        .having(func.count(func.distinct(ScheduleFireRow.schedule_id)) == 1)
        .correlate(source)
        .scalar_subquery()
    )


def public_group(source: Any, dimension: str, *, scope: AuthorizedUsageScope | None = None) -> Any:
    """Group values are safe catalog columns, never JSON or arbitrary SQL input."""
    if dimension == "schedule":
        return task_schedule(source)
    columns = {
        "subject": "usage_subject_key",
        "agent": "agent_id",
        "profile": "agent_profile_id",
        "provider": "provider_id",
        "model": "effective_model",
        "effort": "effective_effort",
        "purpose": "purpose",
        "hook": "hook_id",
        "service": "service_id",
        "operation": "service_operation",
        "project": "project_id",
        "conversation": "conversation_id",
        "session": "session_id",
        "traffic_origin": "traffic_origin",
        "auth_type": "traffic_auth_type",
        "client": "traffic_client_id",
        "key": "traffic_key_id",
        "task": "task_id",
    }
    column = getattr(source, columns[dimension])
    if dimension == "subject" and scope is not None and scope.mode == "project":
        return case((column.like("user:%"), column), else_=literal(None))
    return column


def session_membership(selectors: list[Any], inclusion: str) -> Any:
    """One recursive SQL membership relation, deduplicated across rotations/forks."""
    physical = Session.__table__
    seeds = []
    for item in selectors:
        if item.kind == "conversation":
            predicate = physical.c.conversation_id == item.id
        elif item.kind == "activity":
            predicate = or_(
                physical.c.session_id == item.id, physical.c.activity_scope_id == item.id
            )
        else:
            predicate = physical.c.session_id == item.id
        seeds.append(
            select(
                literal(item.kind).label("selector_kind"),
                literal(item.id).label("selector_id"),
                physical.c.session_id,
            ).where(predicate)
        )
    seed_rows = union_all(*seeds).subquery("usage_seeds")
    members = select(seed_rows).distinct().cte("usage_members", recursive=True)
    if inclusion == "inclusive":
        members = members.union(
            select(members.c.selector_kind, members.c.selector_id, physical.c.session_id).join(
                members, physical.c.parent_session_id == members.c.session_id
            )
        )
    return members


def authorized_selector(selector_kind: Any, selector_id: Any, scope: AuthorizedUsageScope) -> Any:
    """Selector authority, not an empty aggregate pretending to be authorized."""
    physical = aliased(Session)
    conversation = aliased(Conversation)
    owner: ColumnElement[bool]
    if scope.mode == "admin":
        owner = true()
    elif scope.mode == "personal":
        owner = or_(
            conversation.user_email == scope.actor,
            and_(
                conversation.visibility == "project",
                conversation.context_type.not_in(_MANAGED_CONTEXT_TYPES),
                conversation.context_data["app_contact_binding_id"].as_string().is_(None),
                exists(
                    select(1).where(
                        ProjectRow.project_id == conversation.project_id,
                        ProjectRow.status != "deleted",
                        or_(
                            ProjectRow.owner_email == scope.actor,
                            exists(
                                select(1).where(
                                    ProjectGrantRow.project_id == ProjectRow.project_id,
                                    ProjectGrantRow.grantee_type == "user",
                                    ProjectGrantRow.grantee_user_email == scope.actor,
                                    ProjectGrantRow.permission.in_([*PROJECT_GRANT_ROLES, "use"]),
                                    ProjectGrantRow.revoked_at.is_(None),
                                )
                            ),
                        ),
                    )
                ),
            ),
        )
    else:
        owner = and_(
            conversation.project_id == scope.project_id,
            conversation.visibility == "project",
            conversation.context_type.not_in(_MANAGED_CONTEXT_TYPES),
            conversation.context_data["app_contact_binding_id"].as_string().is_(None),
        )
    if scope.invocation is not None:
        owner = and_(owner, conversation.agent_id == scope.invocation.agent_id)
    return or_(
        and_(
            selector_kind == "conversation",
            exists(
                select(1).where(
                    conversation.conversation_id == selector_id,
                    conversation.status != "deleted",
                    owner,
                )
            ),
        ),
        and_(
            selector_kind.in_(["session", "activity", "execution"]),
            exists(
                select(1)
                .select_from(physical)
                .join(conversation, conversation.conversation_id == physical.conversation_id)
                .where(
                    or_(
                        physical.session_id == selector_id,
                        and_(
                            selector_kind == "activity", physical.activity_scope_id == selector_id
                        ),
                    ),
                    conversation.status != "deleted",
                    owner,
                )
            ),
        ),
    )
