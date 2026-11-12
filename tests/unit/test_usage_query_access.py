"""Fresh usage authorization against real, task-local SQLite metadata."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime

import pytest
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from cognis.core.usage.access import (
    UsageAccessError,
    UsageInvocation,
    authorize_usage,
    require_quota_history,
)
from cognis.core.usage.query_models import UsageFilters, UsageScope
from cognis.store.models import Base, ProjectGrantRow, User
from cognis.store.queries import (
    create_agent,
    create_agent_grant,
    create_project,
    create_project_grant,
    create_user,
    revoke_agent_grant,
)

ACTOR = "actor@usage.test"
OWNER = "owner@usage.test"


@pytest.fixture
async def factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        await create_user(session, ACTOR, "Actor", "", role="admin")
        await create_user(session, OWNER, "Owner", "")
        await create_project(session, project_id="proj_usage", owner_email=OWNER, name="Usage")
        await create_project_grant(
            session,
            project_id="proj_usage",
            granted_by=OWNER,
            grantee_user_email=ACTOR,
            permission="viewer",
        )
        await create_agent(
            session, agent_id="agent_usage", owner_email=ACTOR, name="Usage", status="active"
        )
        await session.commit()
    yield factory
    await engine.dispose()


async def test_personal_never_broadens_to_provider_owner(factory) -> None:
    async with factory() as session:
        scope = await authorize_usage(session, actor=ACTOR, requested=UsageScope())
        assert scope.consumer == ACTOR
        with pytest.raises(UsageAccessError):
            scope.check_filters(UsageFilters(subject_emails=[OWNER]))
        with pytest.raises(UsageAccessError):
            scope.check_filters(UsageFilters(provider_owner_emails=[OWNER]))
        with pytest.raises(UsageAccessError):
            require_quota_history(scope, provider_owner_email=OWNER)
        require_quota_history(scope, provider_owner_email=ACTOR)


async def test_admin_demotion_revalidated(factory) -> None:
    async with factory() as session:
        scope = await authorize_usage(session, actor=ACTOR, requested=UsageScope(mode="admin"))
        assert scope.consumer is None
        require_quota_history(scope, provider_owner_email=OWNER)
    async with factory() as session:
        await session.execute(update(User).where(User.email == ACTOR).values(role="user"))
        await session.commit()
    async with factory() as session:
        with pytest.raises(UsageAccessError):
            await authorize_usage(session, actor=ACTOR, requested=UsageScope(mode="admin"))


async def test_project_revocation_and_sensitive_filters(factory) -> None:
    requested = UsageScope(mode="project", project_id="proj_usage")
    async with factory() as session:
        scope = await authorize_usage(session, actor=ACTOR, requested=requested)
        assert not scope.sensitive_provenance
        with pytest.raises(UsageAccessError):
            scope.check_filters(UsageFilters(key_ids=["key_usage"]))
        with pytest.raises(UsageAccessError):
            require_quota_history(scope, provider_owner_email=ACTOR)
    async with factory() as session:
        await session.execute(update(ProjectGrantRow).values(revoked_at=datetime.now(UTC)))
        await session.commit()
    async with factory() as session:
        with pytest.raises(UsageAccessError):
            await authorize_usage(session, actor=ACTOR, requested=requested)


async def test_admin_caller_does_not_elevate_agent(factory) -> None:
    invocation = UsageInvocation(agent_id="agent_usage", agent_owner_email=ACTOR)
    async with factory() as session:
        with pytest.raises(UsageAccessError, match="trusted invocation"):
            await authorize_usage(
                session, actor=ACTOR, requested=UsageScope(mode="admin"), invocation=invocation
            )
        scope = await authorize_usage(
            session, actor=ACTOR, requested=UsageScope(), invocation=invocation
        )
        with pytest.raises(UsageAccessError):
            scope.check_filters(UsageFilters(agent_ids=["other_agent"]))
        with pytest.raises(UsageAccessError):
            require_quota_history(scope, provider_owner_email=ACTOR)


async def test_disabled_user_is_denied(factory) -> None:
    async with factory() as session:
        await session.execute(update(User).where(User.email == ACTOR).values(is_active=False))
        await session.commit()
    async with factory() as session:
        with pytest.raises(UsageAccessError):
            await authorize_usage(session, actor=ACTOR, requested=UsageScope())


async def test_shared_agent_revocation_rechecked_even_for_admin_caller(factory) -> None:
    async with factory() as session:
        await create_agent(
            session, agent_id="shared_usage", owner_email=OWNER, name="Shared", status="active"
        )
        grant = await create_agent_grant(
            session,
            agent_id="shared_usage",
            grantee_user_email=ACTOR,
            executor_scope="owner_executor",
            granted_by=OWNER,
        )
        grant_id = grant.grant_id
        await session.commit()
    invocation = UsageInvocation(agent_id="shared_usage", agent_owner_email=OWNER)
    async with factory() as session:
        scope = await authorize_usage(
            session, actor=ACTOR, requested=UsageScope(), invocation=invocation
        )
        assert scope.consumer == ACTOR  # routing owner is not the accounting subject
    async with factory() as session:
        await revoke_agent_grant(session, grant_id)
        await session.commit()
    async with factory() as session:
        with pytest.raises(UsageAccessError):
            await authorize_usage(
                session, actor=ACTOR, requested=UsageScope(), invocation=invocation
            )
