"""Real PostgreSQL accounting regressions; isolated runtime admission is mandatory.

Collection does not open connections. These cases are not a PostgreSQL pass
until executed in the separately admitted, task-owned network namespace.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import uuid4

import pytest
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.engine import URL, make_url
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from cognis.core.usage.access import UsageAccessError, UsageInvocation
from cognis.core.usage.dimensions import UsageDimensions, encode_usage_dimensions
from cognis.core.usage.query_models import UsageFilters, UsageQuery, UsageScope, UsageSummaryRequest
from cognis.core.usage.query_populations import MEASURES
from cognis.core.usage.query_service import UsageQueryService, create_usage_query_service
from cognis.core.usage.query_service_errors import UsageQueryError
from cognis.store.models import (
    Agent,
    AgentGrantRow,
    AgentSecondaryBinding,
    ArtifactGrantRow,
    Base,
    ChannelAccountRow,
    Conversation,
    ManagedConversationLink,
    ProjectAgentRow,
    ProjectGrantRow,
    ProjectRow,
    Session,
    StepRun,
    Task,
    User,
    UserUiState,
    WorkLiveRevisionRow,
)
from cognis.store.queries import (
    create_agent,
    create_agent_grant,
    create_conversation,
    create_project,
    create_project_grant,
    create_session,
    create_user,
    revoke_agent_grant,
    revoke_project_grant,
)
from cognis.store.usage.models import (
    UsageDailyTotalRow,
    UsageHourlyTotalRow,
    UsageProjectionStateRow,
    UsageSessionTotalRow,
)
from cognis.store.usage.schema import ensure_usage_schema
from cognis.store.usage_queries import subject_key

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.environ.get("COGNIS_TEST_USAGE_PG_ISOLATED") != "1"
        or not os.environ.get("COGNIS_TEST_POSTGRES_URL"),
        reason="Task-owned isolated usage PostgreSQL runtime has not been admitted/configured",
    ),
]

ACTOR = "consumer@usage.test"
OTHER = "provider-owner@usage.test"
START = datetime(2026, 1, 1, tzinfo=UTC)
SUMMARY_ROWS = (UsageDailyTotalRow, UsageHourlyTotalRow, UsageSessionTotalRow)
REQUEST_SQL_SETTINGS = sa.text(
    "SELECT current_setting('jit'), current_setting('statement_timeout'),"
    " current_setting('lock_timeout'), pg_backend_pid()"
)


def isolated_postgres_url(value: str, *, admitted: bool) -> URL:
    """Reject inherited/published fixtures before creating even an engine."""
    if not admitted:
        raise ValueError("Isolated usage PostgreSQL admission marker is required")
    url = make_url(value)
    if url.drivername not in {"postgresql", "postgresql+asyncpg"}:
        raise ValueError("PostgreSQL with asyncpg is required")
    if (
        url.username != "usage_test"
        or url.database != "usage_expansion"
        or url.password is not None
        or set(url.query) - {"host"}
        or url.port not in {None, 5432}
    ):
        raise ValueError("Use the private ephemeral usage fixture, without inherited credentials")
    socket = url.query.get("host")
    if socket is not None:
        if socket != "/run/cognis-usage-pg" or url.host is not None:
            raise ValueError("Only the task-owned private PostgreSQL socket is admitted")
    elif url.host not in {"127.0.0.1", "::1"}:
        raise ValueError("Only namespace-local loopback or the private Unix socket is admitted")
    return url.set(drivername="postgresql+asyncpg")


def core_tables() -> list[sa.Table]:
    """Canonical ACL/persistence tables plus their actual FK dependencies."""
    models = (
        User,
        Agent,
        AgentGrantRow,
        AgentSecondaryBinding,
        ArtifactGrantRow,
        ChannelAccountRow,
        UserUiState,
        WorkLiveRevisionRow,
        ProjectRow,
        ProjectGrantRow,
        ProjectAgentRow,
        Conversation,
        Session,
        Task,
        StepRun,
        ManagedConversationLink,
    )
    tables = {model.__table__ for model in models}
    pending = list(tables)
    while pending:
        for foreign_key in pending.pop().foreign_keys:
            target = foreign_key.column.table
            if target not in tables:
                tables.add(target)
                pending.append(target)
    return [table for table in Base.metadata.sorted_tables if table in tables]


@dataclass
class PgAccounting:
    """Real persistence and public query owner, not a synthesized ORM response."""

    engine: AsyncEngine
    factory: async_sessionmaker[AsyncSession]
    service: UsageQueryService
    shared_grant_id: str

    async def conversation(
        self,
        identifier: str,
        *,
        owner: str = ACTOR,
        agent: str = "agent_root",
        context: str = "web",
        project: str | None = None,
        visibility: str | None = None,
    ) -> None:
        async with self.factory() as db:
            await create_conversation(
                db,
                conversation_id=identifier,
                user_email=owner,
                agent_id=agent,
                context_type=context,
                project_id=project,
                visibility=visibility,
            )
            await db.commit()

    async def observation(
        self,
        identifier: str,
        *,
        conversation: str = "conv_root",
        agent: str = "agent_root",
        owner: str = ACTOR,
        project: str | None = None,
        currency: str | None = "USD",
        basis: str | None = "configured_estimate",
        amount: str = "1",
        priced: int = 1,
        partial: int = 0,
        model: str | None = "model_priced",
        provider: str = "provider_shared",
        **metadata: Any,
    ) -> None:
        async with self.factory() as db:
            physical = await create_session(
                db,
                session_id=identifier,
                conversation_id=conversation,
                user_email=owner,
                agent_id=agent,
                **metadata,
            )
            owner_key = f"projected:{owner}"
            if await db.get(UsageProjectionStateRow, (owner_key, START.date())) is None:
                db.add(
                    UsageProjectionStateRow(
                        owner_key=owner_key,
                        bucket_day=START.date(),
                        active_generation=1,
                        ready=True,
                        capture_only=False,
                        updated_at=START,
                    )
                )
            dimensions = UsageDimensions(
                record_kind="attempt",
                purpose="chat",
                operation="chat",
                usage_subject_key=subject_key(owner),
                agent_id=agent,
                conversation_id=conversation,
                session_id=identifier,
                activity_scope_id=physical.activity_scope_id,
                project_id=project,
                provider_id=provider,
                effective_model=model,
                currency=currency,
                basis=basis,
            )
            for row, grain in (
                (UsageDailyTotalRow, "daily"),
                (UsageHourlyTotalRow, "hourly"),
                (UsageSessionTotalRow, "session"),
            ):
                encoded = encode_usage_dimensions(dimensions, grain=grain)
                values = {name: 0 for name in MEASURES}
                values.update(
                    {
                        name: value
                        for name, value in encoded.dimensions.model_dump(mode="json").items()
                        if name in row.__table__.c
                    }
                )
                values.update(
                    owner_key=owner_key,
                    bucket_day=START.date(),
                    generation=1,
                    dimension_key=encoded.dimension_key,
                    dimensions=encoded.dimensions.model_dump(mode="json"),
                    historical_cost=Decimal(amount),
                    historical_priced_count=priced,
                    historical_partial_count=partial,
                    finalized_count=1,
                    success_count=1,
                    input_tokens=10,
                    input_known_count=1,
                    updated_at=START,
                )
                if row is UsageHourlyTotalRow:
                    values["bucket_hour"] = 0
                insert = pg_insert(row).values(values)
                await db.execute(
                    insert.on_conflict_do_update(
                        index_elements=list(row.__table__.primary_key.columns),
                        set_={
                            **{
                                name: row.__table__.c[name] + insert.excluded[name]
                                for name in MEASURES
                            },
                            "updated_at": START,
                        },
                    )
                )
            await db.commit()

    async def managed(
        self,
        *,
        target: str = "conv_managed",
        link: str = "link_managed",
        controller: str = "conv_root",
        anchor: str | None = "root",
        parent: str | None = None,
        project: str | None = None,
    ) -> None:
        await self.conversation(
            target,
            agent="agent_worker",
            context="agent_work",
            project=project,
        )
        async with self.factory() as db:
            source = await db.get(Conversation, controller)
            assert source is not None
            db.add(
                ManagedConversationLink(
                    link_id=link,
                    user_email=ACTOR,
                    controller_agent_id=source.agent_id,
                    controller_conversation_id=controller,
                    controller_session_id=anchor,
                    target_agent_id="agent_worker",
                    target_conversation_id=target,
                    root_link_id=parent or link,
                    parent_link_id=parent,
                    depth=2 if parent else 1,
                    conversation_state="closed",
                    turn_state="completed",
                )
            )
            await db.commit()
        await self.observation(
            target + "_root",
            conversation=target,
            agent="agent_worker",
            project=project,
        )

    async def summary(
        self,
        *,
        kind: str = "conversation",
        identifier: str = "conv_root",
        inclusion: str = "direct",
        **kwargs: Any,
    ) -> Any:
        request = UsageSummaryRequest.model_validate(
            {
                "items": [{"kind": kind, "id": identifier}],
                "inclusion": inclusion,
            }
        )
        return await self.service.session_summaries(ACTOR, request, **kwargs)

    def cost_query(self, **kwargs: Any) -> UsageQuery:
        return UsageQuery(
            view="cost_breakdowns",
            start=START,
            end=START + timedelta(days=1),
            **kwargs,
        )


@pytest.fixture
async def pg_accounting() -> AsyncIterator[PgAccounting]:
    """One private schema; no public fallback and the owning dispose/drop cleanup."""
    url = isolated_postgres_url(
        os.environ["COGNIS_TEST_POSTGRES_URL"],
        admitted=os.environ.get("COGNIS_TEST_USAGE_PG_ISOLATED") == "1",
    )
    schema = f"cognis_usage_expansion_{uuid4().hex}"
    admin = create_async_engine(url, pool_size=1, max_overflow=0)
    created = False
    try:
        async with admin.begin() as connection:
            await connection.execute(sa.schema.CreateSchema(schema))
        created = True
        engine = create_async_engine(
            url,
            pool_size=2,
            max_overflow=0,
            pool_use_lifo=True,
            connect_args={
                "command_timeout": 5,
                "server_settings": {
                    "search_path": f'"{schema}"',
                    "timezone": "UTC",
                    "statement_timeout": "5000",
                    "lock_timeout": "1000",
                    "idle_in_transaction_session_timeout": "10000",
                },
            },
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(
                    lambda sync: Base.metadata.create_all(sync, tables=core_tables())
                )
                await connection.run_sync(ensure_usage_schema)
            factory = async_sessionmaker(engine, expire_on_commit=False)
            async with factory() as db:
                for owner in (ACTOR, OTHER):
                    await create_user(db, owner, owner, "", role="user")
                for identifier, owner in (
                    ("agent_root", ACTOR),
                    ("agent_peer", ACTOR),
                    ("agent_worker", OTHER),
                ):
                    await create_agent(
                        db,
                        agent_id=identifier,
                        owner_email=owner,
                        name=identifier,
                        status="active",
                    )
                grant = await create_agent_grant(
                    db,
                    agent_id="agent_worker",
                    grantee_user_email=ACTOR,
                    executor_scope="grantee",
                    granted_by=OTHER,
                )
                await create_conversation(
                    db,
                    conversation_id="conv_root",
                    user_email=ACTOR,
                    agent_id="agent_root",
                    context_type="web",
                )
                await db.commit()
            service = await create_usage_query_service(
                factory,
                cursor_secret=b"isolated-postgres-accounting-test-secret",
            )
            yield PgAccounting(engine, factory, service, grant.grant_id)
        finally:
            await engine.dispose()
    finally:
        try:
            if created:
                async with admin.begin() as connection:
                    await connection.execute(sa.schema.DropSchema(schema, cascade=True))
        finally:
            await admin.dispose()


class ObservedReadSession(AsyncSession):
    """Observe a genuine public-owner transaction after its first database read."""

    def __init__(
        self,
        *args: Any,
        after_first_read: Callable[[AsyncSession], Awaitable[None]],
        **kwargs: Any,
    ) -> None:
        super().__init__(*args, **kwargs)
        self.after_first_read = after_first_read
        self.observed = False

    async def execute(self, statement: Any, *args: Any, **kwargs: Any) -> Any:
        result = await super().execute(statement, *args, **kwargs)
        if not self.observed and getattr(statement, "is_select", False):
            self.observed = True
            await self.after_first_read(self)
        return result


@pytest.mark.asyncio
async def test_pg_conversation_lifetime_own_history_is_the_same_toggle_base(
    pg_accounting: PgAccounting,
) -> None:
    pg = pg_accounting
    await pg.observation("root")
    await pg.observation("root_rotation", previous_session_id="root", activity_scope_id="root")
    await pg.observation("root_renew", previous_session_id="root_rotation")
    await pg.observation("root_reset", previous_session_id="root_renew")
    await pg.observation("root_undo", source_session_id="root_reset")
    await pg.observation(
        "delegate",
        agent="agent_worker",
        parent_session_id="root",
        activity_scope_id="root",
        delegation_mode="sync",
        delegation_task="Delegate",
    )
    await pg.observation(
        "delegate_rotation",
        agent="agent_worker",
        parent_session_id="root",
        previous_session_id="delegate",
        activity_scope_id="root",
        delegation_mode="sync",
        delegation_task="Delegate",
    )
    await pg.observation(
        "hook",
        parent_session_id="root",
        activity_scope_id="root",
        delegation_mode="hook",
        delegation_task="Hook",
    )
    await pg.managed()
    # A stale pointer must neither narrow retained own history nor include a delegate as its root.
    async with pg.factory() as db:
        await db.execute(
            sa.update(Conversation)
            .where(
                Conversation.conversation_id == "conv_root",
            )
            .values(active_session_id="delegate")
        )
        await db.commit()
    direct = (await pg.summary()).items[0]
    inclusive = (await pg.summary(inclusion="inclusive")).items[0]
    legacy = (await pg.summary(inclusion="self")).items[0]
    assert direct.available and direct.physical_sessions == 5
    assert direct.metrics.finalized_attempts == 5
    assert inclusive.available and inclusive.physical_sessions == 9
    assert inclusive.metrics.finalized_attempts == 9
    assert legacy.available and legacy.physical_sessions == 8
    assert legacy.metrics.finalized_attempts == 8
    await pg.conversation("conv_fork")
    await pg.observation("fork_origin", conversation="conv_fork", source_session_id="root")
    fork = (await pg.summary(identifier="conv_fork")).items[0]
    assert fork.available and fork.physical_sessions == 1
    assert fork.metrics.finalized_attempts == 1


@pytest.mark.asyncio
async def test_pg_focused_execution_pins_compaction_lane_not_parent_or_siblings(
    pg_accounting: PgAccounting,
) -> None:
    pg = pg_accounting
    await pg.observation("root")
    child = {
        "agent": "agent_worker",
        "parent_session_id": "root",
        "activity_scope_id": "root",
        "delegation_mode": "sync",
        "delegation_task": "Child execution",
    }
    await pg.observation("child", **child)
    await pg.observation("child_rotation", previous_session_id="child", **child)
    await pg.observation("sibling", **child)
    await pg.observation(
        "grandchild",
        agent="agent_worker",
        parent_session_id="child",
        activity_scope_id="root",
        delegation_mode="sync",
        delegation_task="Nested execution",
    )
    await pg.observation(
        "child_renew",
        previous_session_id="child_rotation",
        agent="agent_worker",
        parent_session_id="root",
        delegation_mode="sync",
        delegation_task="Child execution",
    )
    for anchor in ("child", "child_rotation"):
        direct = (await pg.summary(kind="execution", identifier=anchor)).items[0]
        inclusive = (
            await pg.summary(
                kind="execution",
                identifier=anchor,
                inclusion="inclusive",
            )
        ).items[0]
        assert direct.available and direct.physical_sessions == 2
        assert direct.metrics.finalized_attempts == 2
        assert inclusive.available and inclusive.physical_sessions == 3
        assert inclusive.metrics.finalized_attempts == 3


@pytest.mark.asyncio
async def test_pg_owned_managed_descendants_are_deduplicated_after_control_and_grant_closure(
    pg_accounting: PgAccounting,
) -> None:
    pg = pg_accounting
    await pg.observation("root")
    await pg.managed()
    await pg.observation(
        "managed_rotation",
        conversation="conv_managed",
        agent="agent_worker",
        previous_session_id="conv_managed_root",
        activity_scope_id="conv_managed_root",
    )
    await pg.managed(
        target="conv_nested",
        link="link_nested",
        parent="link_managed",
        controller="conv_managed",
        anchor="conv_managed_root",
    )
    async with pg.factory() as db:
        await revoke_agent_grant(db, pg.shared_grant_id)
        await db.commit()
    response = await pg.service.session_summaries(
        ACTOR,
        UsageSummaryRequest(
            items=[{"kind": "conversation", "id": "conv_root"}] * 2,
            inclusion="inclusive",
        ),
    )
    assert len(response.items) == 2
    for item in response.items:
        assert item.available and item.physical_sessions == 4
        assert item.metrics.finalized_attempts == 4
        assert Decimal(item.metrics.costs[0].known_amount) == 4
    focused = (await pg.summary(identifier="conv_managed")).items[0]
    assert focused.available and focused.physical_sessions == 2
    assert focused.metrics.finalized_attempts == 2


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "corruption",
    ["missing_predecessor", "cross_lane", "cycle", "branch", "missing_scope"],
)
async def test_pg_lost_or_malformed_execution_lineage_is_unavailable(
    pg_accounting: PgAccounting,
    corruption: str,
) -> None:
    pg = pg_accounting
    await pg.observation("root")
    await pg.observation("rotation", previous_session_id="root", activity_scope_id="root")
    if corruption == "branch":
        await pg.observation("branch", previous_session_id="root", activity_scope_id="root")
    async with pg.factory() as db:
        root = await db.get(Session, "root")
        rotation = await db.get(Session, "rotation")
        assert root is not None and rotation is not None
        if corruption == "missing_predecessor":
            rotation.previous_session_id = "lost_rotation_metadata"
        elif corruption == "cross_lane":
            rotation.agent_id = "agent_peer"
        elif corruption == "cycle":
            root.previous_session_id = "rotation"
        elif corruption == "missing_scope":
            rotation.activity_scope_id = ""
        await db.commit()
    for inclusion in ("direct", "inclusive"):
        item = (
            await pg.summary(
                kind="execution",
                identifier="rotation",
                inclusion=inclusion,
            )
        ).items[0]
        assert not item.available
        assert item.metrics is None and item.physical_sessions is None


@pytest.mark.asyncio
@pytest.mark.parametrize("corruption", ["missing_root", "target_agent", "controller", "cycle"])
async def test_pg_owned_managed_lineage_failure_does_not_become_a_partial_or_broader_total(
    pg_accounting: PgAccounting,
    corruption: str,
) -> None:
    pg = pg_accounting
    await pg.observation("root")
    await pg.managed()
    async with pg.factory() as db:
        link = await db.get(ManagedConversationLink, "link_managed")
        assert link is not None
        if corruption == "missing_root":
            link.root_link_id = None
        elif corruption == "target_agent":
            link.target_agent_id = "agent_peer"
        elif corruption == "controller":
            link.controller_session_id = "lost_controller_metadata"
        elif corruption == "cycle":
            link.target_conversation_id = "conv_root"
            link.target_agent_id = "agent_root"
        await db.commit()
    item = (await pg.summary(inclusion="inclusive")).items[0]
    assert not item.available and item.metrics is None
    own = (await pg.summary()).items[0]
    assert own.available and own.metrics.finalized_attempts == 1


@pytest.mark.asyncio
async def test_pg_execution_cannot_assign_a_legacy_unanchored_managed_link(
    pg_accounting: PgAccounting,
) -> None:
    pg = pg_accounting
    await pg.observation("root")
    await pg.observation("rotation", previous_session_id="root", activity_scope_id="root")
    await pg.observation("renewed", previous_session_id="rotation")
    await pg.managed(anchor=None)
    focused = (
        await pg.summary(
            kind="execution",
            identifier="root",
            inclusion="inclusive",
        )
    ).items[0]
    assert not focused.available and focused.metrics is None
    conversation = (await pg.summary(inclusion="inclusive")).items[0]
    assert conversation.available and conversation.metrics.finalized_attempts == 4


@pytest.mark.asyncio
async def test_pg_consumer_authority_is_not_agent_provider_ownership(
    pg_accounting: PgAccounting,
) -> None:
    pg = pg_accounting
    await pg.observation("root")
    await pg.conversation("conv_other", owner=OTHER, agent="agent_worker")
    await pg.observation(
        "other",
        owner=OTHER,
        conversation="conv_other",
        agent="agent_worker",
        amount="100",
    )
    response = await pg.service.query(ACTOR, pg.cost_query())
    assert response.total.finalized_attempts == 1
    assert Decimal(response.total.costs[0].known_amount) == 1
    for dimension in response.cost_breakdowns.dimensions:
        assert sum(group.metrics.finalized_attempts for group in dimension.groups) == 1
    for identifier in ("conv_other", "unavailable_conversation"):
        item = (await pg.summary(identifier=identifier, inclusion="inclusive")).items[0]
        assert not item.available and item.metrics is None and item.physical_sessions is None
    with pytest.raises(UsageAccessError):
        await pg.service.query(ACTOR, pg.cost_query(scope=UsageScope(mode="admin")))
    async with pg.factory() as db:
        await db.execute(sa.update(User).where(User.email == ACTOR).values(is_active=False))
        await db.commit()
    with pytest.raises(UsageAccessError):
        await pg.service.query(ACTOR, pg.cost_query())


@pytest.mark.asyncio
async def test_pg_invoking_agent_fences_and_revocation_do_not_revoke_human_accounting(
    pg_accounting: PgAccounting,
) -> None:
    pg = pg_accounting
    await pg.observation("root")
    await pg.managed()
    invocation = UsageInvocation(
        agent_id="agent_root",
        agent_owner_email=ACTOR,
        conversation_id="conv_root",
    )
    result = await pg.service.query(ACTOR, pg.cost_query(), invocation=invocation)
    assert result.total.finalized_attempts == 1
    item = (await pg.summary(inclusion="inclusive", invocation=invocation)).items[0]
    assert item.available and item.metrics.finalized_attempts == 1
    outside = (
        await pg.summary(
            kind="execution",
            identifier="conv_managed_root",
            inclusion="inclusive",
            invocation=invocation,
        )
    ).items[0]
    assert not outside.available and outside.metrics is None
    with pytest.raises(UsageAccessError):
        await pg.service.query(
            ACTOR,
            pg.cost_query(filters=UsageFilters(agent_ids=["agent_worker"])),
            invocation=invocation,
        )
    shared = UsageInvocation(
        agent_id="agent_worker",
        agent_owner_email=OTHER,
        conversation_id="conv_managed",
    )
    before = await pg.service.query(ACTOR, pg.cost_query(), invocation=shared)
    assert before.total.finalized_attempts == 1
    async with pg.factory() as db:
        await revoke_agent_grant(db, pg.shared_grant_id)
        await db.commit()
    with pytest.raises(UsageAccessError):
        await pg.service.query(ACTOR, pg.cost_query(), invocation=shared)
    human = (await pg.summary(inclusion="inclusive")).items[0]
    assert human.available and human.metrics.finalized_attempts == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("visibility_change", ["private", "contact_bound", "deleted", "moved"])
async def test_pg_project_contributors_require_current_resource_and_membership_authority(
    pg_accounting: PgAccounting,
    visibility_change: str,
) -> None:
    pg = pg_accounting
    async with pg.factory() as db:
        await create_project(db, project_id="project_usage", owner_email=OTHER, name="Usage")
        await create_project(db, project_id="project_moved", owner_email=OTHER, name="Moved")
        grant = await create_project_grant(
            db,
            project_id="project_usage",
            grantee_type="user",
            grantee_user_email=ACTOR,
            permission="viewer",
            granted_by=OTHER,
        )
        await db.execute(
            sa.update(Conversation)
            .where(
                Conversation.conversation_id == "conv_root",
            )
            .values(project_id="project_usage", visibility="private")
        )
        await db.commit()
    # A private author's own contribution remains excluded from team accounting.
    await pg.observation("private_root", project="project_usage", amount="100")
    await pg.conversation("conv_visible", project="project_usage", visibility="project")
    await pg.observation("public_actor", conversation="conv_visible", project="project_usage")
    await pg.conversation(
        "conv_other",
        owner=OTHER,
        agent="agent_worker",
        project="project_usage",
        visibility="project",
    )
    await pg.observation(
        "public_other",
        conversation="conv_other",
        agent="agent_worker",
        owner=OTHER,
        project="project_usage",
        amount="20",
    )
    await pg.managed(
        controller="conv_visible",
        anchor="public_actor",
        project="project_usage",
    )
    async with pg.factory() as db:
        await db.execute(
            sa.update(Conversation)
            .where(
                Conversation.conversation_id == "conv_managed",
            )
            .values(visibility="project")
        )
        await db.commit()
    query = pg.cost_query(scope=UsageScope(mode="project", project_id="project_usage"))
    response = await pg.service.query(ACTOR, query)
    assert response.meta.private_resources_excluded
    assert response.total.finalized_attempts == 2
    assert Decimal(response.total.costs[0].known_amount) == 21
    assert {group.key for group in response.cost_breakdowns.dimensions[0].groups} == {
        subject_key(ACTOR),
        subject_key(OTHER),
    }
    items = await pg.service.session_summaries(
        ACTOR,
        UsageSummaryRequest(
            scope=query.scope,
            inclusion="inclusive",
            items=[
                {"kind": "conversation", "id": "conv_visible"},
                {"kind": "conversation", "id": "conv_root"},
                {"kind": "conversation", "id": "conv_managed"},
            ],
        ),
    )
    assert items.items[0].available and items.items[0].metrics.finalized_attempts == 1
    assert all(not item.available and item.metrics is None for item in items.items[1:])
    async with pg.factory() as db:
        conversation = await db.get(Conversation, "conv_other")
        assert conversation is not None
        if visibility_change == "private":
            conversation.visibility = "private"
        elif visibility_change == "contact_bound":
            conversation.context_data = {"app_contact_binding_id": "contact_binding_fixture"}
        elif visibility_change == "deleted":
            conversation.status = "deleted"
        elif visibility_change == "moved":
            conversation.project_id = "project_moved"
        await db.commit()
    changed = await pg.service.query(ACTOR, query)
    assert changed.total.finalized_attempts == 1
    assert Decimal(changed.total.costs[0].known_amount) == 1
    async with pg.factory() as db:
        await revoke_project_grant(db, grant.grant_id)
        await db.commit()
    with pytest.raises(UsageAccessError):
        await pg.service.query(ACTOR, query)


@pytest.mark.asyncio
@pytest.mark.parametrize("bucket", ["day", "hour"])
async def test_pg_fixed_cost_marginals_reconcile_only_within_currency_and_basis(
    pg_accounting: PgAccounting,
    bucket: str,
) -> None:
    pg = pg_accounting
    await pg.observation("complete", amount="2", provider="provider_priced")
    await pg.observation(
        "partial",
        amount="0.5",
        priced=0,
        partial=1,
        model="model_partial",
        provider="provider_priced",
    )
    await pg.observation("euro", amount="3", currency="EUR")
    await pg.observation("zero", amount="0", basis="api_equivalent")
    await pg.observation("unknown", amount="0", priced=0, model="model_unknown")
    await pg.observation(
        "unallocated",
        amount="0",
        priced=0,
        currency=None,
        basis=None,
        model=None,
    )
    response = await pg.service.query(ACTOR, pg.cost_query(bucket=bucket))
    assert response.cost_breakdowns.measure == "known_priced_subtotal"
    assert response.cost_breakdowns.complete_groups is True
    assert response.total.finalized_attempts == 6
    assert response.total.unpriced_attempts == 3
    assert response.total.partially_priced_attempts == 1
    assert response.total.unallocatable_cost_attempts == 1
    expected = {
        ("USD", "api_estimate"): Decimal("2.5"),
        ("EUR", "api_estimate"): Decimal(3),
        ("USD", "configured_equivalent"): Decimal(0),
    }
    assert {
        (cost.currency, cost.basis): Decimal(cost.known_amount) for cost in response.total.costs
    } == expected
    assert (
        next(
            cost
            for cost in response.total.costs
            if cost.currency == "USD" and cost.basis == "api_estimate"
        ).partially_priced_attempts
        == 1
    )
    assert [item.dimension for item in response.cost_breakdowns.dimensions] == [
        "subject",
        "agent",
        "provider",
        "model",
    ]
    for dimension in response.cost_breakdowns.dimensions:
        assert sum(item.metrics.finalized_attempts for item in dimension.groups) == 6
        assert sum(item.metrics.unpriced_attempts for item in dimension.groups) == 3
        assert sum(item.metrics.partially_priced_attempts for item in dimension.groups) == 1
        assert sum(item.metrics.unallocatable_cost_attempts for item in dimension.groups) == 1
        amounts: dict[tuple[str, str], Decimal] = {}
        for item in dimension.groups:
            for cost in item.metrics.costs:
                if cost.known_amount is not None:
                    band = (cost.currency, cost.basis)
                    amounts[band] = amounts.get(band, Decimal(0)) + Decimal(cost.known_amount)
        assert amounts == expected
    unknown_model = next(
        item for item in response.cost_breakdowns.dimensions[3].groups if item.key is None
    )
    assert unknown_model.metrics.unallocatable_cost_attempts == 1
    assert unknown_model.metrics.costs == []
    filtered = await pg.service.query(
        ACTOR,
        pg.cost_query(
            bucket=bucket,
            filters=UsageFilters(provider_ids=["provider_priced"]),
        ),
    )
    assert filtered.total.finalized_attempts == 2
    for dimension in filtered.cost_breakdowns.dimensions:
        assert sum(item.metrics.finalized_attempts for item in dimension.groups) == 2
        assert {item.metrics.costs[0].currency for item in dimension.groups} == {"USD"}
    with pytest.raises(UsageQueryError) as ranking:
        await pg.service.query(
            ACTOR,
            UsageQuery(
                view="ranking",
                metric="cost",
                group_by="model",
                start=START,
                end=START + timedelta(days=1),
            ),
        )
    assert ranking.value.code == "mixed_cost_ranking"


@pytest.mark.asyncio
async def test_pg_cost_ranking_preserves_single_band_partial_pricing(
    pg_accounting: PgAccounting,
) -> None:
    pg = pg_accounting
    await pg.observation("root")
    await pg.observation("partial", priced=0, partial=1, amount="0.25")
    ranking = await pg.service.query(
        ACTOR,
        UsageQuery(
            view="ranking",
            metric="cost",
            group_by="model",
            start=START,
            end=START + timedelta(days=1),
        ),
    )
    assert ranking.ranking_measure == "known_priced_subtotal"
    assert len(ranking.groups) == 1 and ranking.groups[0].key == "model_priced"
    metrics = ranking.groups[0].metrics
    assert metrics.finalized_attempts == 2
    assert metrics.unpriced_attempts == 1 and metrics.partially_priced_attempts == 1
    assert len(metrics.costs) == 1
    cost = metrics.costs[0]
    assert (cost.currency, cost.basis, cost.quality) == ("USD", "api_estimate", "partial")
    assert cost.known_amount is not None and Decimal(cost.known_amount) == Decimal("1.25")
    assert (cost.priced_attempts, cost.unpriced_attempts, cost.partially_priced_attempts) == (
        1,
        1,
        1,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["summary", "cost_breakdowns"])
async def test_pg_public_owner_uses_real_repeatable_read_only_snapshot(
    pg_accounting: PgAccounting,
    operation: str,
) -> None:
    pg = pg_accounting
    await pg.observation("root")
    async with pg.factory() as db:
        original_settings = tuple((await db.execute(REQUEST_SQL_SETTINGS)).one())
    observed: list[tuple[Any, ...]] = []

    async def after_first_read(session: AsyncSession) -> None:
        flags = (
            await session.execute(
                sa.text(
                    "SELECT current_setting('transaction_isolation'),"
                    " current_setting('transaction_read_only'), pg_backend_pid(),"
                    " pg_current_snapshot()::text"
                )
            )
        ).one()
        assert flags[0] == "repeatable read" and flags[1] == "on"
        settings = tuple((await session.execute(REQUEST_SQL_SETTINGS)).one())
        assert settings[:3] == ("off", "1s", original_settings[2])
        assert settings[3] == flags[2]
        with pytest.raises(DBAPIError) as write:
            async with session.begin_nested():
                await session.execute(
                    sa.update(User)
                    .where(
                        User.email == ACTOR,
                    )
                    .values(name="read_only_write_must_not_commit")
                )
        assert getattr(write.value.orig, "sqlstate", None) == "25006"
        # A second genuine connection commits new metadata and rollup values
        # after the public owner's first read established its snapshot.
        await pg.observation("concurrent_root")
        snapshot = await session.scalar(sa.text("SELECT pg_current_snapshot()::text"))
        assert snapshot == flags[3]
        assert await session.scalar(sa.select(User.name).where(User.email == ACTOR)) == ACTOR
        observed.append(tuple(flags))

    observer_factory = async_sessionmaker(
        pg.engine,
        expire_on_commit=False,
        class_=ObservedReadSession,
        after_first_read=after_first_read,
    )
    service = await create_usage_query_service(
        observer_factory,
        cursor_secret=b"isolated-postgres-snapshot-test-secret",
    )
    if operation == "summary":
        first = await service.session_summaries(
            ACTOR,
            UsageSummaryRequest(
                items=[{"kind": "conversation", "id": "conv_root"}],
                inclusion="inclusive",
            ),
        )
        assert first.items[0].available and first.items[0].physical_sessions == 1
        assert first.items[0].metrics.finalized_attempts == 1
        assert Decimal(first.items[0].metrics.costs[0].known_amount) == 1
    else:
        first_cost = await service.query(ACTOR, pg.cost_query())
        assert first_cost.total.finalized_attempts == 1
        assert Decimal(first_cost.total.costs[0].known_amount) == 1
        assert all(
            sum(item.metrics.finalized_attempts for item in dimension.groups) == 1
            for dimension in first_cost.cost_breakdowns.dimensions
        )
    assert len(observed) == 1
    # The reader was returned last. LIFO checkout proves the actual same
    # backend connection did not leak SET TRANSACTION's read-only isolation.
    async with pg.factory() as db:
        ordinary = (
            await db.execute(
                sa.text(
                    "SELECT current_setting('transaction_isolation'),"
                    " current_setting('transaction_read_only'), pg_backend_pid()"
                )
            )
        ).one()
        assert ordinary[0] == "read committed" and ordinary[1] == "off"
        assert ordinary[2] == observed[0][2]
        restored_settings = tuple((await db.execute(REQUEST_SQL_SETTINGS)).one())
        assert restored_settings == original_settings
    next_summary = (await pg.summary(inclusion="inclusive")).items[0]
    assert next_summary.available and next_summary.physical_sessions == 2
    assert next_summary.metrics.finalized_attempts == 2
    next_cost = await pg.service.query(ACTOR, pg.cost_query())
    assert next_cost.total.finalized_attempts == 2
    assert Decimal(next_cost.total.costs[0].known_amount) == 2


@pytest.mark.asyncio
async def test_pg_statement_budget_refuses_and_releases_failed_read_transaction(
    pg_accounting: PgAccounting,
) -> None:
    pg = pg_accounting
    await pg.observation("root")
    async with pg.factory() as db:
        original_settings = tuple((await db.execute(REQUEST_SQL_SETTINGS)).one())
    observed_pids: list[int] = []

    async def exceed_statement_budget(session: AsyncSession) -> None:
        settings = tuple((await session.execute(REQUEST_SQL_SETTINGS)).one())
        assert settings[:3] == ("off", "1s", original_settings[2])
        observed_pids.append(settings[3])
        # The public owner sets 1000ms; do not increase it or wait for an expiry fixture.
        assert (
            await session.scalar(
                sa.text(
                    "SELECT current_setting('statement_timeout')::interval = interval '1 second'"
                )
            )
            is True
        )
        await session.execute(sa.text("SELECT pg_sleep(2)"))

    observer_factory = async_sessionmaker(
        pg.engine,
        expire_on_commit=False,
        class_=ObservedReadSession,
        after_first_read=exceed_statement_budget,
    )
    service = await create_usage_query_service(
        observer_factory,
        cursor_secret=b"isolated-postgres-budget-test-secret",
    )
    with pytest.raises(UsageQueryError) as refused:
        await service.query(ACTOR, pg.cost_query())
    assert refused.value.code == "query_budget_exceeded" and refused.value.status == 503
    assert observed_pids == [original_settings[3]]
    async with pg.factory() as db:
        restored_settings = tuple((await db.execute(REQUEST_SQL_SETTINGS)).one())
        assert restored_settings == original_settings
    # The timeout must not poison the returned connection or fabricate a zero.
    recovered = await pg.service.query(ACTOR, pg.cost_query())
    assert recovered.total.finalized_attempts == 1
    assert Decimal(recovered.total.costs[0].known_amount) == 1


@pytest.mark.asyncio
async def test_pg_membership_budget_refuses_instead_of_truncating(
    pg_accounting: PgAccounting,
) -> None:
    pg = pg_accounting
    await pg.observation("root")
    async with pg.factory() as db:
        # This fixture is exactly the default 10,000-member sentinel boundary:
        # 10,000 additional genuine own roots plus the already projected root.
        await db.execute(
            sa.insert(Session),
            [
                {
                    "session_id": identifier,
                    "user_email": ACTOR,
                    "conversation_id": "conv_root",
                    "agent_id": "agent_root",
                    "activity_scope_id": identifier,
                    "artifact_scope_id": identifier,
                    "intaris_session_id": identifier,
                    "mnemory_session_id": identifier,
                }
                for identifier in (f"budget_root_{number:05d}" for number in range(10000))
            ],
        )
        await db.commit()
    with pytest.raises(UsageQueryError, match="10000") as refused:
        await pg.summary(inclusion="inclusive")
    assert refused.value.code == "query_budget_exceeded" and refused.value.status == 422


@pytest.mark.asyncio
async def test_pg_fixed_marginal_budget_refuses_instead_of_returning_top_groups(
    pg_accounting: PgAccounting,
) -> None:
    pg = pg_accounting
    await pg.observation("root")
    async with pg.factory() as db:
        original = (await db.execute(sa.select(UsageDailyTotalRow))).scalar_one()
        original_dimensions = UsageDimensions.model_validate(original.dimensions)
        values = []
        for number in range(999):
            encoded = encode_usage_dimensions(
                original_dimensions.model_copy(
                    update={"effective_model": f"budget_model_{number}"}
                ),
                grain="daily",
            )
            row = {
                column.name: getattr(original, column.name)
                for column in UsageDailyTotalRow.__table__.columns
            }
            row.update(
                dimension_key=encoded.dimension_key,
                dimensions=encoded.dimensions.model_dump(mode="json"),
                effective_model=encoded.dimensions.effective_model,
            )
            values.append(row)
        await db.execute(sa.insert(UsageDailyTotalRow), values)
        await db.commit()
    # 1000 model groups + subject/agent/provider/total = 1004 strata, one owner/day.
    with pytest.raises(UsageQueryError, match="1000 strata") as refused:
        await pg.service.query(ACTOR, pg.cost_query())
    assert refused.value.code == "query_budget_exceeded" and refused.value.status == 422
