"""Fresh usage-domain authorization without transcript or credential authority."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from cognis.core.project_access import (
    resolve_agent_use_grant,
    resolve_project_role_by_id,
    role_satisfies,
)
from cognis.core.usage.query_models import UsageFilters, UsageMode, UsageScope
from cognis.models.usage import UsageIdentity
from cognis.store.models import User
from cognis.store.project_queries import get_project_agent_use_assignment
from cognis.store.queries import get_agent


class UsageAccessError(ValueError):
    """A usage query cannot be served under current authority."""


@dataclass(frozen=True, slots=True)
class UsageInvocation:
    """Server-selected tool scope; never deserialize this from tool arguments.

    Absence denotes an authenticated HTTP invocation. Tool invocations must pass
    this object even when the invoking user is an administrator.
    """

    agent_id: str
    agent_owner_email: str
    project_id: str | None = None
    allowed_modes: frozenset[UsageMode] = frozenset({"personal"})
    conversation_id: str | None = None
    task_id: str | None = None
    allow_account_history: bool = False

    def runtime_access(self) -> dict[str, Any]:
        """Adapt to the existing current project-agent authorization owner."""
        return {
            "agent_id": self.agent_id,
            "agent_owner_email": self.agent_owner_email,
            "project_id": self.project_id,
        }


@dataclass(frozen=True, slots=True)
class AuthorizedUsageScope:
    """Request-local authority. Never cache this across requests or cursors."""

    actor: str
    mode: UsageMode
    project_id: str | None
    consumer: str | None
    invocation: UsageInvocation | None

    @property
    def sensitive_provenance(self) -> bool:
        """Only own-personal/admin reads may reveal safe client/key provenance."""
        return self.mode != "project"

    def check_filters(self, filters: UsageFilters) -> None:
        """Reject scope expansion before constructing any analytical statement."""
        if self.mode == "personal" and filters.subject_emails not in (None, [self.actor]):
            raise UsageAccessError("Personal usage is limited to the authenticated consumer")
        if self.mode == "personal" and filters.subject_keys:
            consumer = UsageIdentity(
                principal_type="user", usage_subject_kind="user", usage_subject_id=self.actor
            ).usage_subject_key
            if filters.subject_keys != [consumer]:
                raise UsageAccessError("Personal usage is limited to the authenticated consumer")
        if self.mode != "admin" and filters.provider_owner_emails:
            raise UsageAccessError("Provider-owner filtering requires usage-admin authority")
        if self.mode == "project" and (filters.key_ids or filters.client_ids):
            raise UsageAccessError("Client/key filters are unavailable in project usage")
        if self.project_id and filters.project_ids not in (None, [self.project_id]):
            raise UsageAccessError("Project filter exceeds the applied scope")
        invocation = self.invocation
        if invocation is not None:
            if filters.agent_ids not in (None, [invocation.agent_id]):
                raise UsageAccessError("Agent filter exceeds the trusted invocation scope")
            for field, bound in (
                ("conversation_ids", invocation.conversation_id),
                ("task_ids", invocation.task_id),
            ):
                if bound and getattr(filters, field) not in (None, [bound]):
                    raise UsageAccessError("Resource filter exceeds the trusted invocation scope")


async def authorize_usage(
    session: AsyncSession,
    *,
    actor: str,
    requested: UsageScope,
    invocation: UsageInvocation | None = None,
    actor_role: str | None = None,
) -> AuthorizedUsageScope:
    """Resolve authority from current DB state on every response/continuation.

    This operation grants usage visibility only. Project queries must additionally
    join current project-visible resources before grouping, including for authors.
    """
    user = (
        await session.execute(
            select(
                User.email,
                User.role,
                User.is_active,
                User.disabled_at,
            ).where(User.email == actor)
        )
    ).one_or_none()
    if user is None or not user.is_active or user.disabled_at is not None:
        raise UsageAccessError("Usage access is unavailable")
    if invocation is not None:
        if requested.mode not in invocation.allowed_modes:
            raise UsageAccessError("Usage mode exceeds the trusted invocation scope")
        if requested.project_id != invocation.project_id:
            raise UsageAccessError("Usage project exceeds the trusted invocation scope")
        agent = await get_agent(session, invocation.agent_id)
        if (
            agent is None
            or agent.status != "active"
            or agent.owner_email != invocation.agent_owner_email
        ):
            raise UsageAccessError("Agent access is unavailable")
        if invocation.project_id:
            if not await get_project_agent_use_assignment(
                session,
                agent_id=invocation.agent_id,
                project_id=invocation.project_id,
                user_email=actor,
            ):
                raise UsageAccessError("Project agent access is unavailable")
        elif agent.owner_email != actor and not await resolve_agent_use_grant(
            session, agent_id=agent.agent_id, user_email=actor, project_id=None
        ):
            raise UsageAccessError("Agent access is unavailable")
    if requested.mode == "admin" and (
        user.role != "admin" or (actor_role is not None and actor_role != "admin")
    ):
        raise UsageAccessError("Usage-admin authority is required")
    if requested.project_id and invocation is None and requested.mode != "admin":
        _project, resolution = await resolve_project_role_by_id(
            session, requested.project_id, actor
        )
        if not role_satisfies(resolution.role, "view"):
            raise UsageAccessError("Project usage is unavailable")
    return AuthorizedUsageScope(
        actor=actor,
        mode=requested.mode,
        project_id=requested.project_id,
        consumer=actor if requested.mode == "personal" else None,
        invocation=invocation,
    )


def require_quota_history(scope: AuthorizedUsageScope, *, provider_owner_email: str) -> None:
    """Sharing a provider or project never grants account-wide history."""
    if scope.mode == "project":
        raise UsageAccessError("Account history is unavailable in project usage")
    if scope.invocation is not None and not scope.invocation.allow_account_history:
        raise UsageAccessError("Account history exceeds the trusted invocation scope")
    if scope.mode != "admin" and provider_owner_email != scope.actor:
        raise UsageAccessError("Account history requires provider ownership")
