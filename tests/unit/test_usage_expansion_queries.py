"""No-service SQLite behavior and PostgreSQL compilation for accounting expansion."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from pydantic import ValidationError
from sqlalchemy import event, literal, select, union_all, update
from sqlalchemy.dialects import postgresql, sqlite
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import aliased
from sqlalchemy.sql import operators

from cognis.core.artifact_access import _work_edges
from cognis.core.usage.access import AuthorizedUsageScope, UsageInvocation
from cognis.core.usage.dimensions import UsageDimensions, encode_usage_dimensions
from cognis.core.usage.query_models import UsageQuery, UsageSummaryRequest
from cognis.core.usage.query_populations import MEASURES
from cognis.core.usage.query_service import create_usage_query_service
from cognis.core.usage.query_service_errors import UsageQueryError
from cognis.store.managed_lineage import managed_lineage
from cognis.store.models import Base, Conversation, ManagedConversationLink, Session
from cognis.store.queries import (
    create_agent,
    create_conversation,
    create_project,
    create_project_grant,
    create_session,
    create_user,
)
from cognis.store.usage.models import (
    UsageDailyTotalRow,
    UsageHourlyTotalRow,
    UsageProjectionStateRow,
    UsageSessionTotalRow,
)
from cognis.store.usage_membership import (
    invalid_memberships,
    materialized_membership,
    membership,
    same_lane,
)
from cognis.store.usage_queries import subject_key

ACTOR = "consumer@usage.test"
OTHER = "provider-owner@usage.test"
START = datetime(2026, 1, 1, tzinfo=UTC)


@pytest.fixture
async def accounting():
    """Task-owned in-memory DB only; no external fixture or service probing."""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as db:
        for email in (ACTOR, OTHER):
            await create_user(db, email, email, "", role="admin" if email == ACTOR else "user")
        for agent_id, owner in (("agent_root", ACTOR), ("agent_worker", OTHER)):
            await create_agent(
                db, agent_id=agent_id, owner_email=owner, name=agent_id, status="active"
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
        factory, cursor_secret=b"accounting-expansion-test-secret"
    )
    calls = []
    event.listen(
        engine.sync_engine, "before_cursor_execute", lambda c, a, sql, p, x, m: calls.append(sql)
    )
    value = SimpleNamespace(factory=factory, service=service, calls=calls)
    try:
        yield value
    finally:
        await engine.dispose()


async def add_session(
    accounting,
    identifier,
    *,
    conversation="conv_root",
    agent="agent_root",
    owner=ACTOR,
    currency="USD",
    basis="configured_estimate",
    amount="1",
    priced=1,
    partial=0,
    model="model_priced",
    project=None,
    **metadata,
):
    async with accounting.factory() as db:
        physical = await create_session(
            db,
            session_id=identifier,
            conversation_id=conversation,
            user_email=owner,
            agent_id=agent,
            **metadata,
        )
        db.add(
            UsageProjectionStateRow(
                owner_key=identifier,
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
            provider_id="provider_shared",
            effective_model=model,
            currency=currency,
            basis=basis,
        )
        for row, grain, extra in (
            (UsageDailyTotalRow, "daily", {}),
            (UsageHourlyTotalRow, "hourly", {"bucket_hour": 0}),
            (UsageSessionTotalRow, "session", {}),
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
                owner_key=identifier,
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
            db.add(row(**values, **extra))
        await db.commit()
    return identifier


async def add_managed(
    accounting,
    *,
    controller="conv_root",
    controller_session="root",
    target="conv_managed",
    parent=None,
    link="link_managed",
    kind="agent",
    owner=ACTOR,
):
    async with accounting.factory() as db:
        await create_conversation(
            db,
            conversation_id=target,
            user_email=owner,
            agent_id="agent_worker",
            context_type="agent_work",
        )
        source = await db.get(Conversation, controller)
        db.add(
            ManagedConversationLink(
                link_id=link,
                user_email=owner,
                controller_agent_id=source.agent_id,
                controller_conversation_id=controller,
                controller_session_id=controller_session,
                target_agent_id="agent_worker",
                target_conversation_id=target,
                root_link_id=parent or link,
                parent_link_id=parent,
                depth=2 if parent else 1,
                kind=kind,
                conversation_state="closed",
                turn_state="completed",
            )
        )
        await db.commit()
    return await add_session(
        accounting, target + "_root", conversation=target, agent="agent_worker", owner=owner
    )


async def summary(
    accounting, *, kind="conversation", identifier="conv_root", inclusion="direct", **kwargs
):
    return await accounting.service.session_summaries(
        ACTOR,
        UsageSummaryRequest(items=[{"kind": kind, "id": identifier}], inclusion=inclusion),
        **kwargs,
    )


@pytest.mark.asyncio
async def test_sessionless_conversation_costs_and_managed_inclusion(accounting):
    async def add_direct(identifier, conversation, amount):
        dims = UsageDimensions(
            record_kind="attempt",
            purpose="image_generation",
            operation="image_generate",
            usage_subject_key=subject_key(ACTOR),
            conversation_id=conversation,
            currency="USD",
            basis="configured_estimate",
        )
        async with accounting.factory() as db:
            db.add(
                UsageProjectionStateRow(
                    owner_key=identifier,
                    bucket_day=START.date(),
                    active_generation=1,
                    ready=True,
                    capture_only=False,
                    updated_at=START,
                )
            )
            for row, grain, extra in (
                (UsageSessionTotalRow, "session", {"session_kind": "direct"}),
                (UsageHourlyTotalRow, "hourly", {"bucket_hour": 0}),
            ):
                encoded = encode_usage_dimensions(dims, grain=grain)
                db.add(
                    row(
                        **{
                            name: value
                            for name, value in encoded.dimensions.model_dump(mode="json").items()
                            if name in row.__table__.c
                        },
                        **{
                            name: value
                            for name, value in {
                                **dict.fromkeys(MEASURES, 0),
                                "finalized_count": 1,
                                "historical_cost": Decimal(amount),
                                "historical_priced_count": 1,
                            }.items()
                        },
                        owner_key=identifier,
                        bucket_day=START.date(),
                        generation=1,
                        dimension_key=encoded.dimension_key,
                        dimensions=encoded.dimensions.model_dump(mode="json"),
                        **extra,
                        updated_at=START,
                    )
                )
            await db.commit()

    await add_direct("image-charge", "conv_root", "0.08")
    only = (await summary(accounting)).items[0]
    assert only.available and only.physical_sessions == 0
    assert Decimal(only.metrics.costs[0].known_amount) == Decimal("0.08")
    await add_session(accounting, "root")
    await add_managed(accounting)
    await add_direct("video-charge", "conv_managed", "0.12")
    direct = (await summary(accounting)).items[0]
    inclusive = (await summary(accounting, inclusion="inclusive")).items[0]
    assert direct.physical_sessions == 1 and inclusive.physical_sessions == 2
    assert Decimal(direct.metrics.costs[0].known_amount) == Decimal("1.08")
    assert Decimal(inclusive.metrics.costs[0].known_amount) == Decimal("2.20")
    for inclusion, expected in (("direct", "1.08"), ("inclusive", "2.20")):
        result = await accounting.service.query(
            ACTOR,
            UsageQuery(
                start=START,
                end=START + timedelta(days=1),
                filters={"conversation_tree": {"id": "conv_root", "inclusion": inclusion}},
            ),
        )
        assert Decimal(result.total.costs[0].known_amount) == Decimal(expected)


@pytest.mark.asyncio
async def test_lifetime_direct_keeps_resets_renew_undo_and_fork_origin(accounting):
    await add_session(accounting, "root")
    await add_session(accounting, "compact", previous_session_id="root", activity_scope_id="root")
    await add_session(accounting, "renew", previous_session_id="compact")
    await add_session(accounting, "reset", previous_session_id="renew")
    await add_session(accounting, "undo")
    await add_session(accounting, "fork_origin", source_session_id="root")
    await add_session(
        accounting,
        "delegate",
        parent_session_id="root",
        delegation_mode="sync",
        activity_scope_id="root",
        delegation_task="bounded work",
    )
    await add_session(
        accounting,
        "hook",
        parent_session_id="root",
        delegation_mode="hook",
        activity_scope_id="root",
    )
    # Active points to only the newest execution; direct must not use it as the population.
    async with accounting.factory() as db:
        await db.execute(
            update(Conversation)
            .where(Conversation.conversation_id == "conv_root")
            .values(active_session_id="reset")
        )
        await db.commit()
    direct = (await summary(accounting)).items[0]
    inclusive = (await summary(accounting, inclusion="inclusive")).items[0]
    legacy = (await summary(accounting, inclusion="self")).items[0]
    assert direct.available and direct.metrics.finalized_attempts == 6
    assert direct.physical_sessions == 6
    assert inclusive.metrics.finalized_attempts == legacy.metrics.finalized_attempts == 8


@pytest.mark.asyncio
async def test_execution_pins_rotations_without_parent_sibling_or_renew(accounting):
    await add_session(accounting, "root")
    await add_session(
        accounting,
        "child",
        parent_session_id="root",
        delegation_mode="sync",
        activity_scope_id="root",
        delegation_task="child work",
    )
    await add_session(
        accounting,
        "child_rotated",
        parent_session_id="root",
        delegation_mode="sync",
        activity_scope_id="root",
        delegation_task="child work",
        previous_session_id="child",
    )
    await add_session(
        accounting,
        "sibling",
        parent_session_id="root",
        delegation_mode="sync",
        activity_scope_id="root",
        delegation_task="child work",
    )
    await add_session(
        accounting,
        "child_renew",
        parent_session_id="root",
        delegation_mode="sync",
        delegation_task="child work",
        previous_session_id="child_rotated",
    )
    await add_session(
        accounting,
        "grandchild",
        parent_session_id="child",
        delegation_mode="sync",
        activity_scope_id="root",
    )
    direct = (await summary(accounting, kind="execution", identifier="child_rotated")).items[0]
    inclusive = (
        await summary(
            accounting, kind="execution", identifier="child_rotated", inclusion="inclusive"
        )
    ).items[0]
    assert direct.available and direct.metrics.finalized_attempts == 2
    assert inclusive.available and inclusive.metrics.finalized_attempts == 3


@pytest.mark.asyncio
async def test_closed_managed_descendants_and_rotations_are_counted_once(accounting):
    await add_session(accounting, "root")
    await add_managed(accounting)
    await add_session(
        accounting,
        "managed_rotation",
        conversation="conv_managed",
        agent="agent_worker",
        previous_session_id="conv_managed_root",
        activity_scope_id="conv_managed_root",
    )
    await add_managed(
        accounting,
        controller="conv_managed",
        controller_session="conv_managed_root",
        target="conv_grandchild",
        parent="link_managed",
        link="link_nested",
    )
    direct = (await summary(accounting)).items[0]
    inclusive = (await summary(accounting, inclusion="inclusive")).items[0]
    managed = (await summary(accounting, identifier="conv_managed")).items[0]
    assert direct.metrics.finalized_attempts == 1
    assert inclusive.metrics.finalized_attempts == inclusive.physical_sessions == 4
    assert managed.metrics.finalized_attempts == 2
    # No grant to use the provider-owned worker was created, nor is one required
    # for the consumer's historical HTTP accounting.
    assert (await summary(accounting, inclusion="inclusive")).items[0].available
    scope = AuthorizedUsageScope(ACTOR, "personal", None, ACTOR, None)
    for inclusion in ("direct", "inclusive"):
        request = UsageSummaryRequest(
            items=[
                {"kind": "conversation", "id": "conv_root"},
                {"kind": "conversation", "id": "conv_managed"},
            ],
            inclusion=inclusion,
        )
        async with accounting.factory() as db:
            recursive = set(
                (await db.execute(select(membership(request.items, inclusion, scope)))).all()
            )
            literal = await materialized_membership(db, request.items, inclusion, scope)
            materialized = set((await db.execute(select(literal))).all())
        # The frontier expansion is the same membership as the recursive CTE.
        assert materialized == recursive and recursive


@pytest.mark.asyncio
@pytest.mark.parametrize("corruption", ["root", "owner", "agent", "channel", "controller"])
async def test_invalid_or_external_managed_links_do_not_add_usage(accounting, corruption):
    await add_session(accounting, "root")
    await add_managed(accounting)
    async with accounting.factory() as db:
        row = await db.get(ManagedConversationLink, "link_managed")
        if corruption == "root":
            row.root_link_id = "unresolved-root"
        elif corruption == "owner":
            row.user_email = OTHER
        elif corruption == "agent":
            row.target_agent_id = "agent_root"
        elif corruption == "controller":
            row.controller_session_id = "unresolved-controller"
        else:
            row.kind = "channel"
        await db.commit()
    result = (await summary(accounting, inclusion="inclusive")).items[0]
    if corruption in {"root", "agent", "controller"}:
        assert not result.available and result.metrics is None
    else:
        assert result.available and result.metrics.finalized_attempts == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("corruption", ["missing", "identity", "branch", "cycle", "scope"])
async def test_malformed_execution_is_unavailable(accounting, corruption):
    await add_session(accounting, "root")
    await add_session(accounting, "rotation", previous_session_id="root", activity_scope_id="root")
    if corruption == "branch":
        await add_session(
            accounting, "branched", previous_session_id="root", activity_scope_id="root"
        )
    else:
        async with accounting.factory() as db:
            row = await db.get(Session, "root" if corruption == "cycle" else "rotation")
            if corruption == "missing":
                row.previous_session_id = "unresolved-previous"
            elif corruption == "identity":
                row.delegation_mode = "sync"
            elif corruption == "scope":
                row.activity_scope_id = ""
            else:
                row.previous_session_id = "rotation"
            await db.commit()
    result = (await summary(accounting, kind="execution", identifier="rotation")).items[0]
    assert not result.available and result.metrics is None and result.physical_sessions is None


@pytest.mark.asyncio
async def test_unknown_unauthorized_and_duplicate_selectors(accounting):
    await add_session(accounting, "root")
    await add_managed(accounting, target="conv_other", link="link_other", owner=OTHER)
    response = await accounting.service.session_summaries(
        ACTOR,
        UsageSummaryRequest(
            inclusion="direct",
            items=[
                {"kind": "conversation", "id": identifier}
                for identifier in (
                    "conv_root",
                    "conv_root",
                    "conv_other",
                    "unresolved-conversation",
                )
            ],
        ),
    )
    assert [item.available for item in response.items] == [True, True, False, False]
    assert [item.metrics.finalized_attempts for item in response.items[:2]] == [1, 1]
    assert response.items[2].model_dump(exclude={"selector"}) == response.items[3].model_dump(
        exclude={"selector"}
    )


@pytest.mark.asyncio
async def test_invoking_agent_summary_excludes_valid_managed_worker_usage(accounting):
    await add_session(accounting, "root")
    await add_managed(accounting)
    invocation = UsageInvocation(
        agent_id="agent_root", agent_owner_email=ACTOR, conversation_id="conv_root"
    )
    human = (await summary(accounting, inclusion="inclusive")).items[0]
    assert human.available and human.metrics.finalized_attempts == 2
    scoped = (await summary(accounting, inclusion="inclusive", invocation=invocation)).items[0]
    assert scoped.available and scoped.metrics.finalized_attempts == 1
    assert scoped.physical_sessions == 1
    outside = (
        await summary(
            accounting,
            kind="execution",
            identifier="conv_managed_root",
            inclusion="inclusive",
            invocation=invocation,
        )
    ).items[0]
    assert not outside.available and outside.metrics is None
    async with accounting.factory() as db:
        worker = await db.get(Conversation, "conv_managed")
        worker.status = "deleted"
        await db.commit()
    malformed = (await summary(accounting, inclusion="inclusive", invocation=invocation)).items[0]
    assert not malformed.available and malformed.metrics is None


@pytest.mark.asyncio
async def test_revoked_invoking_agent_still_denies(accounting):
    await add_session(accounting, "root")
    invocation = UsageInvocation(
        agent_id="agent_worker", agent_owner_email=OTHER, conversation_id="conv_root"
    )
    with pytest.raises(ValueError, match="Agent access"):
        await summary(accounting, inclusion="inclusive", invocation=invocation)


@pytest.mark.asyncio
async def test_known_cost_marginals_preserve_partial_unknown_bands_and_zero(accounting):
    await add_session(accounting, "complete", amount="2")
    await add_session(
        accounting, "partial", amount="0.5", priced=0, partial=1, model="model_partial"
    )
    await add_session(accounting, "unpriced", amount="0", priced=0, model=None)
    await add_session(accounting, "unallocated", amount="0", priced=0, currency=None, model=None)
    await add_session(accounting, "euro", amount="3", currency="EUR")
    await add_session(accounting, "zero", amount="0", basis="api_equivalent")
    accounting.calls.clear()
    response = await accounting.service.query(
        ACTOR,
        UsageQuery(
            view="cost_breakdowns",
            start=START,
            end=START + timedelta(days=1),
        ),
    )
    assert response.total.finalized_attempts == 6
    assert response.total.partially_priced_attempts == 1
    assert response.total.unpriced_attempts == 3
    assert response.total.unallocatable_cost_attempts == 1
    amounts = {(cost.currency, cost.basis): cost for cost in response.total.costs}
    assert Decimal(amounts["USD", "api_estimate"].known_amount) == Decimal("2.5")
    assert amounts["USD", "api_estimate"].partially_priced_attempts == 1
    assert amounts["USD", "configured_equivalent"].known_amount is not None
    assert Decimal(amounts["USD", "configured_equivalent"].known_amount) == 0
    assert Decimal(amounts["EUR", "api_estimate"].known_amount) == 3
    assert response.cost_breakdowns.measure == "known_priced_subtotal"
    assert [value.dimension for value in response.cost_breakdowns.dimensions] == [
        "subject",
        "agent",
        "provider",
        "model",
    ]
    for dimension in response.cost_breakdowns.dimensions:
        assert sum(group.metrics.finalized_attempts for group in dimension.groups) == 6
        assert sum(group.metrics.unallocatable_cost_attempts for group in dimension.groups) == 1
    models = response.cost_breakdowns.dimensions[3].groups
    assert next(group for group in models if group.key is None).metrics.finalized_attempts == 2
    data_reads = [sql for sql in accounting.calls if "usage_breakdown_source" in sql]
    assert len(data_reads) == 1 and "UNION ALL" in data_reads[0]
    with pytest.raises(UsageQueryError, match="currency|priced"):
        await accounting.service.query(
            ACTOR,
            UsageQuery(
                view="ranking",
                group_by="model",
                start=START,
                end=START + timedelta(days=1),
            ),
        )


@pytest.mark.parametrize("kind", ["conversation", "execution", "activity"])
def test_request_population_contract(kind):
    if kind == "activity":
        with pytest.raises(ValidationError):
            UsageSummaryRequest(items=[{"kind": kind, "id": "activity_target"}], inclusion="direct")
    else:
        query = UsageSummaryRequest(
            items=[{"kind": kind, "id": "session_target"}], inclusion="direct"
        )
        assert query.inclusion == "direct"
    with pytest.raises(ValidationError):
        UsageSummaryRequest(items=[{"kind": "execution", "id": "session_target"}])


@pytest.mark.parametrize("activity", [False, True])
def test_lane_owner_is_indexable_and_other_fields_are_null_safe(activity: bool) -> None:
    assert Session.__table__.c.user_email.nullable is False
    left, right = aliased(Session), aliased(Session)
    predicates = {
        predicate.left.key: predicate
        for predicate in same_lane(left, right, activity=activity).clauses
    }
    fields = {
        "user_email",
        "conversation_id",
        "agent_id",
        "parent_session_id",
        "delegation_mode",
        "delegation_task",
    }
    if activity:
        fields.add("activity_scope_id")
    assert set(predicates) == fields
    assert predicates["user_email"].operator is operators.eq
    assert all(
        predicates[name].operator is operators.is_not_distinct_from
        for name in fields - {"user_email"}
    )


@pytest.mark.parametrize("dialect", [sqlite.dialect(), postgresql.dialect()])
def test_recursive_sql_and_shared_content_lineage_compile(dialect):
    scope = AuthorizedUsageScope(ACTOR, "personal", None, ACTOR, None)
    request = UsageSummaryRequest(
        items=[{"kind": "conversation", "id": "conv_root"}], inclusion="inclusive"
    )
    sql = str(select(membership(request.items, request.inclusion, scope)).compile(dialect=dialect))
    assert "usage_managed_lineage" in sql and "UNION" in sql
    assert "source_session_id =" not in sql
    lineage = managed_lineage(owner_email=ACTOR, name="expected_lineage")
    content_sql = str(select(_work_edges(owner_email=ACTOR)).compile(dialect=dialect))
    assert "content_managed_lineage" in content_sql
    assert [column.name for column in lineage.c] == [
        "link_id",
        "parent_link_id",
        "root_link_id",
        "depth",
        "kind",
        "controller_conversation_id",
        "controller_agent_id",
        "controller_session_id",
        "target_conversation_id",
        "target_agent_id",
    ]


@pytest.mark.asyncio
async def test_budget_is_not_silently_truncated(accounting, monkeypatch):
    from cognis.core.usage.query_breakdowns import cost_breakdowns
    from cognis.core.usage.query_populations import UsagePopulationContract
    from cognis.store import usage_membership

    await add_session(accounting, "root")
    await add_session(accounting, "rotation", previous_session_id="root", activity_scope_id="root")
    monkeypatch.setattr(usage_membership, "MAX_MEMBERS", 1)
    with pytest.raises(UsageQueryError, match="membership"):
        await summary(accounting)
    fake_session = AsyncMock()
    fake_session.execute.return_value = Mock()
    fake_session.execute.return_value.mappings.return_value = [{}] * 1001
    with pytest.raises(UsageQueryError, match="1000 strata"):
        await cost_breakdowns(
            fake_session,
            scope=AuthorizedUsageScope(ACTOR, "personal", None, ACTOR, None),
            query=UsageQuery(view="cost_breakdowns", start=START, end=START + timedelta(days=1)),
            population=UsagePopulationContract(),
            meta=None,
            base=select(UsageDailyTotalRow),
        )


@pytest.mark.asyncio
async def test_project_fences_apply_to_sidebar_and_all_marginals(accounting):
    from cognis.core.usage.query_models import UsageScope
    from cognis.store.models import ProjectGrantRow

    async with accounting.factory() as db:
        await create_project(db, project_id="project_shared", owner_email=OTHER, name="Shared")
        await create_project_grant(
            db,
            project_id="project_shared",
            granted_by=OTHER,
            grantee_user_email=ACTOR,
            permission="viewer",
        )
        conversation = await db.get(Conversation, "conv_root")
        conversation.project_id = "project_shared"
        conversation.visibility = "project"
        await db.commit()
    await add_session(accounting, "root", project="project_shared")
    await add_managed(accounting)
    async with accounting.factory() as db:
        child = await db.get(Conversation, "conv_managed")
        child.project_id = "project_shared"
        child.visibility = "project"
        for row in (UsageHourlyTotalRow, UsageSessionTotalRow):
            await db.execute(
                update(row)
                .where(row.conversation_id == "conv_managed")
                .values(
                    project_id="project_shared",
                )
            )
        await db.commit()
    scope = UsageScope(mode="project", project_id="project_shared")
    request = UsageSummaryRequest(
        scope=scope,
        inclusion="inclusive",
        items=[
            {"kind": "conversation", "id": "conv_root"},
            {"kind": "conversation", "id": "conv_managed"},
        ],
    )
    result = await accounting.service.session_summaries(ACTOR, request)
    assert result.items[0].metrics.finalized_attempts == 1
    assert not result.items[1].available
    query = UsageQuery(
        view="cost_breakdowns",
        scope=scope,
        start=START,
        end=START + timedelta(days=1),
    )
    breakdowns = await accounting.service.query(ACTOR, query)
    assert breakdowns.total.finalized_attempts == 1
    for marginal in breakdowns.cost_breakdowns.dimensions:
        assert sum(group.metrics.finalized_attempts for group in marginal.groups) == 1
    async with accounting.factory() as db:
        conversation = await db.get(Conversation, "conv_root")
        conversation.visibility = "private"
        await db.commit()
    hidden = await accounting.service.session_summaries(ACTOR, request)
    assert all(not item.available for item in hidden.items)
    assert (await accounting.service.query(ACTOR, query)).total is None
    async with accounting.factory() as db:
        await db.execute(
            update(ProjectGrantRow)
            .where(
                ProjectGrantRow.project_id == scope.project_id,
                ProjectGrantRow.grantee_user_email == ACTOR,
            )
            .values(revoked_at=START)
        )
        await db.commit()
    with pytest.raises(ValueError, match="Project usage"):
        await accounting.service.session_summaries(ACTOR, request)


@pytest.mark.asyncio
async def test_filters_projection_and_pending_remain_explicit(accounting):
    from cognis.core.usage.query_models import UsageFilters

    await add_session(accounting, "priced", model="chosen")
    await add_session(accounting, "partial", model="excluded", priced=0, partial=1, amount="4")
    query = UsageQuery(
        view="cost_breakdowns",
        start=START,
        end=START + timedelta(days=1),
        filters=UsageFilters(model_ids=["chosen"]),
    )
    result = await accounting.service.query(ACTOR, query)
    assert result.total.finalized_attempts == 1 and result.total.partially_priced_attempts == 0
    assert len(result.cost_breakdowns.dimensions[3].groups) == 1
    assert not result.meta.complete and result.meta.capture.state == "unknown"
    assert result.total.pending_complete is None
    assert accounting.calls.count("BEGIN") == 1
    with pytest.raises(UsageQueryError, match="Pending"):
        await accounting.service.session_summaries(
            ACTOR,
            UsageSummaryRequest(
                items=[{"kind": "conversation", "id": "conv_root"}],
                inclusion="direct",
                include_pending=True,
            ),
        )
    async with accounting.factory() as db:
        await db.execute(
            update(UsageProjectionStateRow).values(
                building_generation=2,
                source_cut_row_id=1,
                build_job_id="projection_rebuild",
            )
        )
        await db.commit()
    result = await accounting.service.query(ACTOR, query)
    assert result.total is None and result.cost_breakdowns is None
    assert not (await summary(accounting)).items[0].available


@pytest.mark.asyncio
async def test_ambiguous_managed_anchor_and_cycle_are_unavailable(accounting):
    await add_session(accounting, "root")
    await add_managed(accounting, controller_session=None)
    assert (await summary(accounting, inclusion="inclusive")).items[0].available
    focused = await summary(accounting, kind="execution", identifier="root", inclusion="inclusive")
    assert not focused.items[0].available
    async with accounting.factory() as db:
        link = await db.get(ManagedConversationLink, "link_managed")
        link.controller_session_id = "root"
        # An owner-bound, otherwise valid self-cycle must not become a subtotal.
        link.target_conversation_id = "conv_root"
        link.target_agent_id = "agent_root"
        await db.commit()
    assert not (await summary(accounting, inclusion="inclusive")).items[0].available


@pytest.mark.asyncio
async def test_new_http_response_models_serialize_the_complete_contract(accounting):
    import httpx
    from fastapi import FastAPI

    from cognis.api.authentication import AuthenticatedUser
    from cognis.api.common import require_current_user
    from cognis.api.routes.usage import create_usage_router

    await add_session(accounting, "partial", amount="0.25", priced=0, partial=1)
    app = FastAPI()
    app.include_router(create_usage_router(accounting.service), prefix="/api/v1")
    app.dependency_overrides[require_current_user] = lambda: AuthenticatedUser(
        email=ACTOR,
        role="admin",
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://accounting.test",
    ) as client:
        response = await client.post(
            "/api/v1/usage/query",
            json={
                "view": "cost_breakdowns",
                "start": START.isoformat(),
                "end": (START + timedelta(days=1)).isoformat(),
            },
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["total"]["partially_priced_attempts"] == 1
        assert body["cost_breakdowns"]["complete_groups"] is True
        response = await client.post(
            "/api/v1/usage/session-summaries",
            json={
                "items": [{"kind": "execution", "id": "partial"}],
                "inclusion": "direct",
            },
        )
        assert response.status_code == 200, response.text
        assert response.json()["items"][0]["physical_sessions"] == 1


@pytest.mark.asyncio
async def test_shared_lineage_preserves_content_edges_and_owner_validation(accounting):
    await add_session(accounting, "root")
    await add_managed(accounting)
    await add_managed(
        accounting,
        controller="conv_managed",
        controller_session="conv_managed_root",
        target="conv_grandchild",
        parent="link_managed",
        link="link_nested",
    )
    edges = _work_edges(owner_email=ACTOR)
    async with accounting.factory() as db:
        pairs = set((await db.execute(select(edges))).all())
        assert {
            ("c:conv_root", "c:conv_managed"),
            ("s:root", "c:conv_managed"),
            ("c:conv_managed", "c:conv_grandchild"),
        } <= pairs
        foreign_edges = _work_edges(owner_email=OTHER)
        foreign = set((await db.execute(select(foreign_edges))).all())
        assert not any(child == "c:conv_managed" for _, child in foreign)
        # A broken root invalidates its nested link at the shared canonical owner.
        link = await db.get(ManagedConversationLink, "link_managed")
        link.root_link_id = "unresolved-root"
        await db.commit()
    async with accounting.factory() as db:
        pairs = set((await db.execute(select(edges))).all())
        assert ("c:conv_root", "c:conv_managed") not in pairs
        assert ("c:conv_managed", "c:conv_grandchild") not in pairs


@pytest.mark.asyncio
async def test_membership_preflight_refuses_before_metadata_proof(accounting, monkeypatch):
    from cognis.store import usage_membership

    await add_session(accounting, "root")
    await add_session(accounting, "other_root")
    monkeypatch.setattr(usage_membership, "MAX_MEMBERS", 1)
    accounting.calls.clear()
    with pytest.raises(UsageQueryError, match="Sidebar membership exceeds 10000") as refused:
        await summary(accounting)
    assert refused.value.code == "query_budget_exceeded" and refused.value.status == 422
    assert any("usage_seeds" in sql for sql in accounting.calls)
    assert not any("usage_proof_members" in sql for sql in accounting.calls)


def _preflight_members(*rows):
    return union_all(
        *(
            select(
                literal(kind).label("selector_kind"),
                literal(identifier).label("selector_id"),
                literal(session_id).label("session_id"),
            )
            for kind, identifier, session_id in rows
        )
    ).cte("usage_members")


@pytest.mark.asyncio
async def test_membership_preflight_authorizes_before_limit(accounting, monkeypatch):
    from cognis.store import usage_membership

    await add_session(accounting, "root")
    await add_session(accounting, "other_root")
    async with accounting.factory() as db:
        await create_conversation(
            db,
            conversation_id="private_other",
            user_email=OTHER,
            agent_id="agent_worker",
            context_type="web",
        )
        await db.commit()
    await add_session(
        accounting, "private_root", conversation="private_other", agent="agent_worker", owner=OTHER
    )
    await add_session(
        accounting,
        "private_other_root",
        conversation="private_other",
        agent="agent_worker",
        owner=OTHER,
    )
    monkeypatch.setattr(usage_membership, "MAX_MEMBERS", 1)
    scope = AuthorizedUsageScope(ACTOR, "personal", None, ACTOR, None)
    hidden = [
        ("conversation", "private_other", "private_root"),
        ("conversation", "private_other", "private_other_root"),
    ]
    async with accounting.factory() as db:
        members = _preflight_members(*hidden, ("conversation", "conv_root", "root"))
        accounting.calls.clear()
        assert await invalid_memberships(db, members, scope, inclusive=False) == set()
        assert any("usage_proof_members" in sql for sql in accounting.calls)

        members = _preflight_members(
            *hidden,
            ("conversation", "conv_root", "root"),
            ("conversation", "conv_root", "other_root"),
        )
        accounting.calls.clear()
        with pytest.raises(UsageQueryError, match="Sidebar membership exceeds 10000") as refused:
            await invalid_memberships(db, members, scope, inclusive=False)
        assert refused.value.code == "query_budget_exceeded" and refused.value.status == 422
        assert not any("usage_proof_members" in sql for sql in accounting.calls)


@pytest.mark.asyncio
async def test_membership_preflight_counts_per_selector_and_rechecks_authority(
    accounting, monkeypatch
):
    from cognis.store import usage_membership, usage_queries

    await add_session(accounting, "root")
    monkeypatch.setattr(usage_membership, "MAX_MEMBERS", 1)
    scope = AuthorizedUsageScope(ACTOR, "personal", None, ACTOR, None)
    async with accounting.factory() as db:
        members = _preflight_members(
            ("conversation", "conv_root", "root"), ("execution", "root", "root")
        )
        with pytest.raises(UsageQueryError, match="Sidebar membership exceeds 10000"):
            await invalid_memberships(db, members, scope, inclusive=False)

        authority = Mock(side_effect=[literal(True), literal(False)])
        monkeypatch.setattr(usage_queries, "authorized_selector", authority)
        members = _preflight_members(("conversation", "conv_root", "root"))
        accounting.calls.clear()
        assert await invalid_memberships(db, members, scope, inclusive=False) == set()
        assert authority.call_count == 2
        assert any("usage_proof_members" in sql for sql in accounting.calls)


@pytest.mark.asyncio
async def test_batch_metadata_reads_do_not_scale_with_selector_count(accounting):
    await add_session(accounting, "root")
    await add_managed(accounting)
    statements = []
    for count in (1, 4):
        accounting.calls.clear()
        await accounting.service.session_summaries(
            ACTOR,
            UsageSummaryRequest(
                inclusion="inclusive",
                items=[
                    {"kind": "conversation", "id": "conv_root"},
                    {"kind": "execution", "id": "root"},
                    {"kind": "conversation", "id": "conv_managed"},
                    {"kind": "execution", "id": "conv_managed_root"},
                ][:count],
            ),
        )
        # Membership resolves through lineage frontier reads (one per depth level),
        # then every consumer reads the same literal member set.
        statements.append(
            (
                len([sql for sql in accounting.calls if "usage_lineage_edges" in sql]),
                len([sql for sql in accounting.calls if "usage_members" in sql]),
            )
        )
        assert len([sql for sql in accounting.calls if "usage_proof_managed_lineage" in sql]) == 1
    # Frontier reads follow lineage depth (more seeds can only shorten it);
    # consumer statements are fixed. Neither grows with the selector count.
    assert statements[1][0] <= statements[0][0] and statements[0][1] == statements[1][1]


@pytest.mark.asyncio
async def test_continuation_budget_does_not_merge_or_truncate_lanes(accounting, monkeypatch):
    from cognis.store import usage_membership

    await add_session(accounting, "root")
    await add_session(accounting, "rotation", previous_session_id="root", activity_scope_id="root")
    monkeypatch.setattr(usage_membership, "MAX_LANE_MEMBERS", 1)
    assert (
        not (
            await summary(
                accounting,
                kind="execution",
                identifier="rotation",
            )
        )
        .items[0]
        .available
    )
