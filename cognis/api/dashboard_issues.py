"""Bounded user-scoped projection of actionable dashboard issues."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta

import sqlalchemy as sa
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from cognis.api.models import (
    DashboardIssue,
    DashboardIssueResource,
    DashboardIssuesResponse,
    DashboardIssuesSummary,
)
from cognis.models.tool import effective_mcp_auth_config
from cognis.ownership import SYSTEM_USER_EMAIL
from cognis.store.models import (
    ExecutorRow,
    MCPOAuthTokenRow,
    MCPServerRow,
    NotificationRow,
    Schedule,
)
from cognis.store.queries import mcp_oauth_resource_key

ISSUE_LIMIT = 50
CANDIDATE_LIMIT = 500
TOOL_OBSERVATION_STALE_AFTER = timedelta(minutes=10)
EXECUTOR_OFFLINE_GRACE = timedelta(seconds=30)
_SEVERITY_ORDER = {"critical": 0, "warning": 1, "info": 2}
_UNHEALTHY_RUNTIME_STATES = {"offline", "stale", "blocked", "error", "unavailable"}


def schedule_incident_token(
    schedule_id: str,
    task_id: str | None,
    last_fired_at: datetime | None,
) -> str:
    durable_run = (
        f"task:{task_id}"
        if task_id
        else f"fire:{_as_utc(last_fired_at).isoformat() if last_fired_at else 'unknown'}"
    )
    correlation = f"{schedule_id}\0{durable_run}"
    return hashlib.sha256(correlation.encode()).hexdigest()


async def collect_dashboard_issues(
    session: AsyncSession,
    *,
    user_email: str,
    now: datetime | None = None,
) -> DashboardIssuesResponse:
    generated_at = _as_utc(now or datetime.now(UTC))
    executor_rows = list(
        (
            await session.scalars(
                select(ExecutorRow)
                .where(
                    sa.or_(
                        ExecutorRow.owner_email == user_email,
                        ExecutorRow.owner_email == SYSTEM_USER_EMAIL,
                        ExecutorRow.owner_email.is_(None),
                    )
                )
                .order_by(ExecutorRow.executor_id)
                .limit(CANDIDATE_LIMIT)
            )
        ).all()
    )
    server_rows = list(
        (
            await session.scalars(
                select(MCPServerRow)
                .where(
                    sa.or_(
                        MCPServerRow.owner_email == user_email,
                        MCPServerRow.owner_email == SYSTEM_USER_EMAIL,
                    )
                )
                .order_by(MCPServerRow.server_id)
                .limit(CANDIDATE_LIMIT)
            )
        ).all()
    )
    server_by_id = {row.server_id: row for row in server_rows}
    oauth_rows = list(
        (
            await session.scalars(
                select(MCPOAuthTokenRow)
                .join(MCPServerRow, MCPServerRow.server_id == MCPOAuthTokenRow.mcp_server_id)
                .where(
                    MCPOAuthTokenRow.user_email == user_email,
                    sa.or_(
                        MCPServerRow.owner_email == user_email,
                        MCPServerRow.owner_email == SYSTEM_USER_EMAIL,
                    ),
                )
                .order_by(MCPOAuthTokenRow.token_id)
                .limit(CANDIDATE_LIMIT)
            )
        ).all()
    )
    schedule_rows = list(
        (
            await session.scalars(
                select(Schedule)
                .outerjoin(
                    NotificationRow,
                    NotificationRow.notification_id
                    == sa.literal("notif_schedule_") + Schedule.schedule_id,
                )
                .where(
                    Schedule.created_by == user_email,
                    sa.or_(
                        NotificationRow.notification_id.is_(None),
                        NotificationRow.status == "pending",
                    ),
                    sa.or_(
                        sa.and_(
                            Schedule.last_run_status == "failed",
                            Schedule.enabled.is_(True),
                        ),
                        Schedule.disabled_reason.like("auto_consecutive_failures:%"),
                    ),
                )
                .order_by(Schedule.updated_at.desc(), Schedule.schedule_id)
                .limit(CANDIDATE_LIMIT)
            )
        ).all()
    )

    issues: list[DashboardIssue] = []
    for row in executor_rows:
        issues.extend(_executor_issues(row, server_by_id=server_by_id, now=generated_at))
    oauth_by_server: dict[str, list[MCPOAuthTokenRow]] = {}
    for oauth_row in oauth_rows:
        oauth_by_server.setdefault(oauth_row.mcp_server_id, []).append(oauth_row)
    for server_id, rows in oauth_by_server.items():
        server = server_by_id.get(server_id)
        if server is not None:
            issue = _oauth_issue(rows, server)
            if issue is not None:
                issues.append(issue)
    for schedule in schedule_rows:
        issues.append(_schedule_issue(schedule))

    issues.sort(key=_issue_sort_key)
    total = len(issues)
    selected = issues[:ISSUE_LIMIT]
    return DashboardIssuesResponse(
        generated_at=generated_at,
        issues=selected,
        summary=DashboardIssuesSummary(
            total=total,
            critical=sum(issue.severity == "critical" for issue in issues),
            warning=sum(issue.severity == "warning" for issue in issues),
            info=sum(issue.severity == "info" for issue in issues),
            truncated=total > ISSUE_LIMIT
            or len(executor_rows) == CANDIDATE_LIMIT
            or len(server_rows) == CANDIDATE_LIMIT
            or len(oauth_rows) == CANDIDATE_LIMIT
            or len(schedule_rows) == CANDIDATE_LIMIT,
        ),
    )


def _executor_issues(
    row: ExecutorRow,
    *,
    server_by_id: dict[str, MCPServerRow],
    now: datetime,
) -> list[DashboardIssue]:
    if row.status != "active":
        return []
    observed_at = _optional_utc(row.last_observed_at)
    resource = DashboardIssueResource(type="executor", id=row.executor_id, label=row.name)
    action_url = "/settings?tab=executors"
    issues: list[DashboardIssue] = []
    runtime_state = str(row.runtime_state or "offline")
    offline_in_grace = (
        runtime_state == "offline"
        and observed_at is not None
        and now - observed_at < EXECUTOR_OFFLINE_GRACE
    )
    if (
        row.executor_type == "websocket"
        and runtime_state in _UNHEALTHY_RUNTIME_STATES
        and not offline_in_grace
    ):
        issues.append(
            _issue(
                kind="executor_unavailable",
                severity="critical",
                resource=resource,
                title=f"{row.name} is unavailable",
                detail=f"The executor runtime state is {runtime_state}.",
                observed_at=observed_at,
                action_url=action_url,
            )
        )
    elif row.executor_type == "websocket" and runtime_state == "degraded":
        issues.append(
            _issue(
                kind="executor_degraded",
                severity="warning",
                resource=resource,
                title=f"{row.name} is degraded",
                detail="The executor reported a degraded runtime state.",
                observed_at=observed_at,
                action_url=action_url,
            )
        )
    desired = int(row.desired_config_version or 0)
    applied = int(row.applied_config_version or 0)
    if desired != applied:
        issues.append(
            _issue(
                kind="executor_config_not_converged",
                severity="warning",
                resource=resource,
                title=f"{row.name} configuration is not applied",
                detail=f"The executor applied version {applied}, but version {desired} is required.",
                observed_at=observed_at,
                action_url=action_url,
            )
        )
    if row.executor_type == "websocket" and runtime_state in {"active", "degraded"}:
        if observed_at is None or row.observed_tools is None:
            issues.append(
                _issue(
                    kind="tool_observation_missing",
                    severity="warning",
                    resource=resource,
                    title=f"{row.name} has no tool observation",
                    detail="The executor has not reported its available tools.",
                    observed_at=observed_at,
                    action_url=action_url,
                )
            )
        elif now - observed_at > TOOL_OBSERVATION_STALE_AFTER:
            issues.append(
                _issue(
                    kind="tool_observation_stale",
                    severity="warning",
                    resource=resource,
                    title=f"{row.name} tool observation is stale",
                    detail="The executor tool observation is older than 10 minutes.",
                    observed_at=observed_at,
                    action_url=action_url,
                )
            )
    metadata = row.runtime_metadata if isinstance(row.runtime_metadata, dict) else {}
    for item in metadata.get("mcp_servers", []):
        if not isinstance(item, dict) or item.get("status") in {None, "ready", "disabled"}:
            continue
        server_id = item.get("server_id")
        if not isinstance(server_id, str):
            continue
        server = server_by_id.get(server_id)
        if server is None or server.status != "active":
            continue
        auth_fault = item.get("authorization_required") is True
        mcp_resource = DashboardIssueResource(
            type="mcp_server", id=server.server_id, label=server.name
        )
        issues.append(
            _issue(
                kind="mcp_auth_fault" if auth_fault else "mcp_runtime_fault",
                severity="critical" if auth_fault else "warning",
                resource=mcp_resource,
                title=(
                    f"{server.name} needs authorization"
                    if auth_fault
                    else f"{server.name} runtime is unavailable"
                ),
                detail=(
                    f"The MCP server failed on executor {row.name} because authorization is required."
                    if auth_fault
                    else f"The MCP server reported status {item.get('status')} on executor {row.name}."
                ),
                observed_at=observed_at,
                action_url="/settings?tab=tools",
                discriminator=row.executor_id,
            )
        )
    return issues


def _oauth_issue(
    rows: list[MCPOAuthTokenRow],
    server: MCPServerRow,
) -> DashboardIssue | None:
    if server.status != "active":
        return None
    auth_config = effective_mcp_auth_config(server.auth_config, server.headers)
    if auth_config.type != "oauth2":
        return None
    issuer = (auth_config.issuer or auth_config.authorization_server or "").rstrip("/")
    resource_key = mcp_oauth_resource_key(auth_config.resource or server.url)
    current_rows = [
        row for row in rows if row.issuer.rstrip("/") == issuer and row.resource_key == resource_key
    ]
    if not current_rows or any(row.status == "active" for row in current_rows):
        return None
    row = max(
        current_rows,
        key=lambda item: (
            _optional_utc(item.updated_at) or datetime.min.replace(tzinfo=UTC),
            item.token_id,
        ),
    )
    return _issue(
        kind="mcp_auth_fault",
        severity="critical",
        resource=DashboardIssueResource(type="mcp_server", id=server.server_id, label=server.name),
        title=f"{server.name} needs authorization",
        detail=f"The MCP authorization state is {row.status}.",
        observed_at=_optional_utc(row.last_refresh_error_at or row.updated_at),
        action_url="/settings?tab=tools",
        discriminator="oauth",
    )


def _schedule_issue(
    row: Schedule,
) -> DashboardIssue:
    auto_disabled = not row.enabled and str(row.disabled_reason or "").startswith(
        "auto_consecutive_failures:"
    )
    return _issue(
        kind="schedule_auto_disabled" if auto_disabled else "schedule_failed",
        severity="critical" if auto_disabled else "warning",
        resource=DashboardIssueResource(type="schedule", id=row.schedule_id, label=row.name),
        title=(f"{row.name} was automatically disabled" if auto_disabled else f"{row.name} failed"),
        detail=f"{int(row.consecutive_errors or 0)} consecutive failures.",
        observed_at=_optional_utc(row.updated_at),
        action_url=f"/schedules/{row.schedule_id}",
        action_label="View schedule",
        dismiss_token=schedule_incident_token(
            row.schedule_id,
            row.last_terminal_task_id,
            row.last_fired_at,
        ),
    )


def _issue(
    *,
    kind: str,
    severity: str,
    resource: DashboardIssueResource,
    title: str,
    detail: str,
    observed_at: datetime | None,
    action_url: str,
    action_label: str = "View",
    dismiss_token: str | None = None,
    discriminator: str = "",
) -> DashboardIssue:
    stable_key = "\0".join((kind, resource.type, resource.id, discriminator))
    issue_id = f"issue_{hashlib.sha256(stable_key.encode()).hexdigest()[:20]}"
    return DashboardIssue(
        id=issue_id,
        severity=severity,
        kind=kind,
        title=title,
        detail=detail,
        resource=resource,
        observed_at=observed_at,
        action_url=action_url,
        action_label=action_label,
        dismiss_token=dismiss_token,
    )


def _issue_sort_key(issue: DashboardIssue) -> tuple[int, float, str]:
    observed = issue.observed_at.timestamp() if issue.observed_at is not None else float("-inf")
    return (_SEVERITY_ORDER[issue.severity], -observed, issue.id)


def _optional_utc(value: datetime | None) -> datetime | None:
    return _as_utc(value) if value is not None else None


def _as_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
