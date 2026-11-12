"""Real SQLite/PostgreSQL usage endpoints; only isolated synthetic schemas."""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from hashlib import sha256
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import delete, event, insert, select, text, update
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from cognis.api.authentication import AuthenticatedUser
from cognis.api.common import require_current_user
from cognis.api.routes.usage import create_usage_router
from cognis.core.usage.access import UsageAccessError
from cognis.core.usage.dimensions import (
    UsageDimensions,
    encode_usage_dimensions,
    request_metric_values,
)
from cognis.core.usage.query_adapters import server_event
from cognis.core.usage.query_models import UsageAttemptQuery, UsageScope, UsageSummaryRequest
from cognis.core.usage.query_populations import MEASURES
from cognis.core.usage.query_service import UsageQueryService, create_usage_query_service
from cognis.core.usage.query_service_errors import UsageQueryError
from cognis.core.usage.tool_handler import build_usage_tool_handlers
from cognis.models.usage import UsageAttemptFinalized, UsageRequestStats
from cognis.runtime_context import RuntimeAccessContext
from cognis.store.models import (
    Base,
    Conversation,
    ManagedConversationLink,
    Schedule,
    ScheduleFireRow,
    Session,
    StepRun,
    Task,
)
from cognis.store.queries import (
    create_agent,
    create_conversation,
    create_project,
    create_project_grant,
    create_session,
    create_task,
    create_user,
)
from cognis.store.usage.models import (
    UsageAttemptRow,
    UsageDailyTotalRow,
    UsageHourlyTotalRow,
    UsagePriceRevisionRow,
    UsagePricingStateRow,
    UsageProjectionStateRow,
    UsageRequestLogRow,
    UsageRequestStatsRow,
    UsageSessionTotalRow,
    UsageValuationRow,
)
from cognis.store.usage_queries import subject_key

ACTOR = "actor@usage.test"
OTHER = "other@usage.test"
START = datetime(2026, 1, 1, tzinfo=UTC)
END = START + timedelta(days=1)
pytestmark = pytest.mark.asyncio


@pytest.fixture(params=["sqlite", "postgres"])
async def db(request, tmp_path):
    schema = f"usage_api_{uuid4().hex}"
    admin = None
    if request.param == "postgres":
        url = os.environ.get("COGNIS_TEST_POSTGRES_URL")
        if not url:
            pytest.skip(
                "COGNIS_TEST_POSTGRES_URL is required for isolated PostgreSQL qualification"
            )
        admin = create_async_engine(url)
        async with admin.begin() as conn:
            await conn.execute(text(f'CREATE SCHEMA "{schema}"'))
        engine = create_async_engine(url, connect_args={"server_settings": {"search_path": schema}})
    else:
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'usage.db'}")
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory() as session:
            await create_user(session, ACTOR, "Actor", "", role="admin")
            await create_user(session, OTHER, "Other", "")
            await create_agent(
                session, agent_id="agent_usage", owner_email=ACTOR, name="Usage", status="active"
            )
            await create_project(
                session, project_id="project_usage", owner_email=OTHER, name="Usage"
            )
            await create_project_grant(
                session,
                project_id="project_usage",
                granted_by=OTHER,
                grantee_user_email=ACTOR,
                permission="viewer",
            )
            for index, (owner, visibility, context) in enumerate(
                [
                    (ACTOR, "project", "web"),
                    (OTHER, "project", "web"),
                    (ACTOR, "private", "web"),
                    (OTHER, "project", "agent_work"),
                ]
            ):
                await create_conversation(
                    session,
                    user_email=owner,
                    agent_id="agent_usage",
                    context_type=context,
                    conversation_id=f"conv_usage_{index}",
                    project_id="project_usage",
                    visibility=visibility,
                )
                await create_session(
                    session,
                    conversation_id=f"conv_usage_{index}",
                    user_email=owner,
                    agent_id="agent_usage",
                    session_id=f"session_usage_{index}",
                )
                await _seed(session, index, owner)
            await session.commit()
        service = await create_usage_query_service(
            factory,
            cursor_secret=b"usage-test-cursor-signing-key-32-bytes",
        )
        calls = []
        event.listen(
            engine.sync_engine,
            "before_cursor_execute",
            lambda c, a, statement, p, x, m: calls.append(statement),
        )
        yield SimpleNamespace(
            factory=factory, service=service, engine=engine, calls=calls, dialect=request.param
        )
    finally:
        await engine.dispose()
        if admin is not None:
            async with admin.begin() as conn:
                await conn.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
            await admin.dispose()


async def _seed(session, index: int, owner: str) -> None:
    owner_key = f"accounting_bucket_{index}"
    key = subject_key(owner)
    session.add(
        UsageProjectionStateRow(
            owner_key=owner_key,
            bucket_day=START.date(),
            active_generation=1,
            ready=True,
            capture_only=False,
            updated_at=START,
        )
    )
    common = dict(
        owner_key=owner_key,
        bucket_day=START.date(),
        usage_subject_key=key,
        provider_id="provider_usage",
        effective_model="model_usage",
        purpose="recap",
        generation=1,
        operation="chat",
        streaming=False,
        source="provider",
        updated_at=START,
        currency="USD",
        basis="configured_estimate",
        finalized_count=1,
        success_count=1,
        reported_count=1,
        input_tokens=10,
        input_known_count=1,
        output_tokens=5,
        output_known_count=1,
        historical_cost=Decimal("0.125"),
        historical_priced_count=1,
    )
    resources = dict(
        project_id="project_usage",
        conversation_id=f"conv_usage_{index}",
        session_id=f"session_usage_{index}",
        agent_id="agent_usage",
    )
    common = {**{name: 0 for name in MEASURES}, **common}
    dimensions = UsageDimensions.model_validate(
        {
            "record_kind": "attempt",
            **{
                name: value
                for name, value in (common | resources).items()
                if name in UsageDimensions.model_fields
            },
        }
    )
    for model, grain, extra in (
        (UsageDailyTotalRow, "daily", {}),
        (UsageHourlyTotalRow, "hourly", {"bucket_hour": 0}),
        (UsageSessionTotalRow, "session", {}),
    ):
        encoded = encode_usage_dimensions(dimensions, grain=grain)
        values = common | {
            name: value
            for name, value in encoded.dimensions.model_dump(mode="json").items()
            if name in model.__table__.c
        }
        values.update(
            dimensions=encoded.dimensions.model_dump(mode="json"),
            dimension_key=encoded.dimension_key,
        )
        session.add(model(**values, **extra))
    terminal = UsageAttemptFinalized.model_validate(
        {
            "observation_id": f"terminal_usage_{index}",
            "context": {
                "trace": {
                    "attempt_id": f"attempt_usage_{index}",
                    "request_id": f"request_usage_{index}",
                    "cycle_id": f"cycle_usage_{index}",
                    "attempt_ordinal": 1,
                },
                "attribution": {
                    "identity": {
                        "principal_subject": owner,
                        "principal_type": "user",
                        "usage_subject_kind": "user",
                        "usage_subject_id": owner,
                        "routing_owner": ACTOR,
                        "provider_payer": ACTOR,
                        "key_record_id": f"key_usage_{index}",
                        "client_id": f"client_usage_{index}",
                    },
                    "purpose": "recap",
                    "resources": {"provenance": "controller", **resources},
                },
                "selection": {
                    "provider_id": "provider_usage",
                    "effective_model": "model_usage",
                    "streaming": False,
                    "source": "provider",
                    "operation": "chat",
                },
                "started_at": START + timedelta(seconds=index),
                "controller_owner_id": "controller_usage",
                "coverage_epoch_id": "epoch_usage",
            },
            "outcome": "success",
            "dispatch_status": "provider_accepted",
            "finalized_at": START + timedelta(minutes=1),
            "performance": {"source": "provider"},
            "usage": {
                "semantic_profile": "openai",
                "semantic_version": 1,
                "cache_read_semantics": "included_in_input",
                "cache_write_semantics": "unknown",
                "reasoning_semantics": "included_in_output",
                "completeness": "reported",
                "input_tokens": {"status": "reported", "value": 10, "source": "provider"},
                "output_tokens": {"status": "reported", "value": 5, "source": "provider"},
                "cache_read_tokens": {"status": "not_applicable"},
                "cache_write_tokens": {"status": "not_applicable"},
                "reasoning_tokens": {"status": "not_applicable"},
            },
        }
    )
    session.add(
        UsageAttemptRow(
            attempt_id=f"attempt_usage_{index}",
            request_id=f"request_usage_{index}",
            attempt_ordinal=1,
            cycle_id=f"cycle_usage_{index}",
            owner_key=owner_key,
            bucket_day=START.date(),
            started_at_observed=START + timedelta(seconds=index),
            controller_owner_id="controller_usage",
            coverage_epoch_id="epoch_usage",
            usage_subject_key=key,
            usage_subject_kind="user",
            usage_subject_id=owner,
            principal_subject=owner,
            principal_type="user",
            routing_owner=ACTOR,
            provider_payer=ACTOR,
            provider_id="provider_usage",
            operation="chat",
            effective_model="model_usage",
            purpose="recap",
            binding_provenance="controller",
            **resources,
            outcome="success",
            dispatch_status="provider_accepted",
            finalized_at=START + timedelta(minutes=1),
            ended_at=START + timedelta(minutes=1),
            usage_completeness="reported",
            input_tokens=10,
            input_status="reported",
            output_tokens=5,
            output_status="reported",
            fact_version=1,
            key_record_id=f"key_usage_{index}",
            client_id=f"client_usage_{index}",
            provider_request_id=f"provider_request_usage_{index}",
            streaming=False,
            source="provider",
            requested_parameters={},
            effective_parameters={},
            registered_at=START,
            context_digest=terminal.context.observation_digest(),
            context_payload=terminal.context.model_dump(mode="json"),
            start_observed=True,
            terminal_digest=terminal.observation_digest(),
            terminal_payload=terminal.model_dump(mode="json", exclude={"context"}),
        )
    )
    stats = UsageRequestStats(
        request_id=f"request_usage_{index}",
        observation_id=f"stats_usage_{index}",
        request_started_at=START + timedelta(seconds=index),
        observed_at=END,
        attribution=terminal.context.attribution,
        controller_owner_id="controller_usage",
        coverage_epoch_id="epoch_usage",
        stats_scope="server",
        logical_outcome="success",
        server={"elapsed_ms": 1000},
    )
    session.add(
        UsageRequestStatsRow(
            request_id=f"request_usage_{index}",
            owner_key=owner_key,
            bucket_day=START.date(),
            request_started_at=START + timedelta(seconds=index),
            usage_subject_key=key,
            attribution=stats.attribution.model_dump(mode="json"),
            attempt_id=f"attempt_usage_{index}",
            server_observation_id=f"stats_usage_{index}",
            server_payload=stats.model_dump(mode="json", exclude={"attribution"}),
            logical_outcome="success",
            controller_owner_id="controller_usage",
            coverage_epoch_id="epoch_usage",
            fact_version=1,
            updated_at=START,
        )
    )


def body(**kwargs):
    return {"start": START.isoformat(), "end": END.isoformat(), **kwargs}


async def _resource_tasks(db):
    async with db.factory() as session:
        session.add(
            Schedule(
                schedule_id="schedule_usage",
                created_by=ACTOR,
                agent_id="agent_usage",
                name="Daily diagnostics",
                cron_expr="0 8 * * *",
                task_template={},
            )
        )
        await session.flush()
        for index, owner in enumerate([ACTOR, OTHER, ACTOR, OTHER]):
            task = await create_task(
                session,
                task_id=f"task_usage_{index}",
                created_by=owner,
                agent_id="agent_usage",
                title=f"Task {index}",
                project_id="project_usage",
                source_type="scheduler" if index == 0 else "api",
            )
            task.visibility = "project"
            await session.execute(
                update(UsageHourlyTotalRow)
                .where(UsageHourlyTotalRow.session_id == f"session_usage_{index}")
                .values(task_id=task.task_id)
            )
            await session.execute(
                update(Conversation)
                .where(Conversation.conversation_id == f"conv_usage_{index}")
                .values(title=f"Conversation {index}")
            )
        # Multiple fires for one task must not multiply spend.
        for index in range(2):
            session.add(
                ScheduleFireRow(
                    fire_id=f"fire_usage_{index}",
                    schedule_id="schedule_usage",
                    task_id="task_usage_0",
                    scheduled_fire_at=START + timedelta(hours=index),
                )
            )
        await session.commit()


@pytest.mark.parametrize(
    "dimension", ["task", "schedule", "conversation", "session", "agent", "project"]
)
async def test_resource_rankings_have_current_authorized_labels(db, client, dimension):
    await _resource_tasks(db)
    response = await client.post(
        "/api/v1/usage/query",
        json=body(
            view="ranking",
            group_by=dimension,
            metric="cost",
        ),
    )
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["ranking_measure"] == "known_priced_subtotal"
    assert sum(Decimal(g["metrics"]["costs"][0]["known_amount"]) for g in data["groups"]) == (
        Decimal("0.125") if dimension == "schedule" else Decimal("0.250")
    )
    expected = {
        "task": {"Task 0", "Task 2"},
        "schedule": {"Daily diagnostics"},
        "conversation": {"Conversation 0", "Conversation 2"},
        "session": {"Conversation 0", "Conversation 2"},
        "agent": {"Usage"},
        "project": {"Usage"},
    }
    assert {g["label"] for g in data["groups"]} == expected[dimension]


async def test_cost_breakdown_agents_use_current_agent_names(db, client):
    await _resource_tasks(db)
    response = await client.post("/api/v1/usage/query", json=body(view="cost_breakdowns"))
    assert response.status_code == 200, response.text
    agents = next(
        item
        for item in response.json()["cost_breakdowns"]["dimensions"]
        if item["dimension"] == "agent"
    )
    assert {(g["key"], g["label"]) for g in agents["groups"]} == {("agent_usage", "Usage")}


async def test_system_agent_scope_selects_only_usage_without_an_agent(db, client):
    await _resource_tasks(db)
    async with db.factory() as session:
        await session.execute(
            update(UsageHourlyTotalRow)
            .where(UsageHourlyTotalRow.session_id == "session_usage_2")
            .values(agent_id=None, purpose="compaction")
        )
        await session.commit()
    everything = await client.post(
        "/api/v1/usage/query",
        json=body(view="ranking", bucket="hour", group_by="session", metric="attempts"),
    )
    system = await client.post(
        "/api/v1/usage/query",
        # Hourly totals are the rows this fixture rewrites.
        json=body(
            view="ranking",
            bucket="hour",
            group_by="purpose",
            metric="attempts",
            filters={"agent_scope": "system"},
        ),
    )
    assert everything.status_code == 200 and system.status_code == 200, system.text
    sessions = {g["key"]: g["metrics"]["finalized_attempts"] for g in everything.json()["groups"]}
    assert [(g["key"], g["metrics"]["finalized_attempts"]) for g in system.json()["groups"]] == [
        ("compaction", sessions["session_usage_2"])
    ]


async def test_task_origin_and_schedule_filters_partition_tasks(db, client):
    await _resource_tasks(db)
    for origin, expected in [("scheduled", ["task_usage_0"]), ("one_off", ["task_usage_2"])]:
        response = await client.post(
            "/api/v1/usage/query",
            json=body(
                view="ranking",
                group_by="task",
                filters={"task_origin": origin},
            ),
        )
        assert response.status_code == 200, response.text
        assert [g["key"] for g in response.json()["groups"]] == expected
    response = await client.post(
        "/api/v1/usage/query",
        json=body(
            view="ranking",
            group_by="task",
            filters={"schedule_ids": ["schedule_usage"]},
        ),
    )
    assert response.status_code == 200, response.text
    assert [g["key"] for g in response.json()["groups"]] == ["task_usage_0"]
    async with db.factory() as session:
        await session.execute(delete(ScheduleFireRow))
        await session.commit()
    response = await client.post(
        "/api/v1/usage/query",
        json=body(
            view="ranking",
            group_by="task",
            filters={"task_origin": "one_off"},
        ),
    )
    assert [g["key"] for g in response.json()["groups"]] == ["task_usage_2"]


@pytest.mark.parametrize("pricing", ["partial", "unknown", "mixed", "mixed_basis"])
async def test_cost_ranking_partial_zero_unknown_and_mixed_strata(db, client, pricing):
    await _resource_tasks(db)
    async with db.factory() as session:
        await session.execute(
            update(UsageHourlyTotalRow)
            .where(UsageHourlyTotalRow.session_id == "session_usage_0")
            .values(historical_cost=Decimal(0))
        )
        await session.execute(
            update(UsageHourlyTotalRow)
            .where(UsageHourlyTotalRow.session_id == "session_usage_2")
            .values(
                **(
                    {
                        "historical_priced_count": 0,
                        "historical_partial_count": 1,
                        "historical_cost": Decimal("0.3"),
                    }
                    if pricing == "partial"
                    else {
                        "currency": None,
                        "basis": None,
                        "historical_priced_count": 0,
                        "historical_cost": Decimal(0),
                    }
                    if pricing == "unknown"
                    else {"currency": "EUR"}
                    if pricing == "mixed"
                    else {"basis": "api_equivalent"}
                )
            )
        )
        await session.commit()
    response = await client.post(
        "/api/v1/usage/query",
        json=body(
            view="ranking",
            group_by="task",
            metric="cost",
        ),
    )
    if pricing in {"mixed", "mixed_basis"}:
        assert response.status_code == 422, response.text
        assert response.json()["detail"]["code"] == "mixed_cost_ranking"
        return
    assert response.status_code == 200, response.text
    groups = response.json()["groups"]
    assert [g["key"] for g in groups] == (
        ["task_usage_2", "task_usage_0"]
        if pricing == "partial"
        else ["task_usage_0", "task_usage_2"]
    )
    affected = next(g["metrics"] for g in groups if g["key"] == "task_usage_2")
    assert affected["unpriced_attempts"] == 1
    assert affected["partially_priced_attempts"] == (1 if pricing == "partial" else 0)
    if pricing == "partial":
        assert affected["costs"][0]["quality"] == "partial"


async def test_usage_admin_cannot_read_private_resource_titles(db, client):
    await _resource_tasks(db)
    async with db.factory() as session:
        await session.execute(
            update(Conversation)
            .where(Conversation.user_email == OTHER)
            .values(visibility="private")
        )
        await session.execute(
            update(Task).where(Task.created_by == OTHER).values(visibility="private")
        )
        await session.execute(update(Schedule).values(created_by=OTHER))
        await session.commit()
    for dimension in ["task", "conversation", "session", "schedule"]:
        response = await client.post(
            "/api/v1/usage/query",
            json=body(
                scope={"mode": "admin"},
                view="ranking",
                group_by=dimension,
            ),
        )
        assert response.status_code == 200, response.text
        private = [
            g
            for g in response.json()["groups"]
            if dimension == "schedule" or g["key"].endswith(("_1", "_3"))
        ]
        assert private
        assert all(g["label"] is None for g in private)
    response = await client.post("/api/v1/usage/sessions", json=body(scope={"mode": "admin"}))
    assert response.status_code == 200, response.text
    labels = {item["session_id"]: item["session_label"] for item in response.json()["items"]}
    assert labels["session_usage_0"] == "Conversation 0"
    assert labels["session_usage_1"] is None
    assert labels["session_usage_3"] is None


async def test_logical_request_labels_use_current_resource_authority(db, client):
    await _resource_tasks(db)
    async with db.factory() as session:
        for index in (0, 1):
            fact = await session.scalar(
                select(UsageAttemptRow).where(
                    UsageAttemptRow.attempt_id == f"attempt_usage_{index}"
                )
            )
            session.add(
                UsageRequestLogRow(
                    request_id=fact.request_id,
                    owner_key=fact.owner_key,
                    usage_subject_key=fact.usage_subject_key,
                    bucket_day=START.date(),
                    started_at=START,
                    registered_at=START,
                    attribution=fact.context_payload["attribution"],
                    session_id=fact.session_id,
                    conversation_id=fact.conversation_id,
                    logical_outcome="success",
                )
            )
        await session.execute(
            update(Conversation)
            .where(Conversation.user_email == OTHER)
            .values(visibility="private")
        )
        await session.commit()
    response = await client.post(
        "/api/v1/usage/requests",
        json=body(
            scope={"mode": "admin"},
            pagination_unit="request",
        ),
    )
    assert response.status_code == 200, response.text
    items = {item["request_id"]: item for item in response.json()["items"]}
    assert items["request_usage_0"]["session_label"] == "Conversation 0"
    assert items["request_usage_1"]["session_label"] is None
    for item in items.values():
        detail = await client.post(
            "/api/v1/usage/requests/detail",
            json=body(
                scope={"mode": "admin"},
                selection_ref=item["selection_ref"],
            ),
        )
        assert detail.status_code == 200, detail.text
        assert detail.json()["request"]["session_label"] == item["session_label"]


async def test_resource_day_ranking_works_beyond_ninety_days(db, client):
    await _resource_tasks(db)
    response = await client.post(
        "/api/v1/usage/query",
        json=body(
            view="ranking",
            group_by="schedule",
            bucket="day",
            end=(START + timedelta(days=180)).isoformat(),
        ),
    )
    assert response.status_code == 200, response.text
    assert response.json()["groups"][0]["key"] == "schedule_usage"
    response = await client.post(
        "/api/v1/usage/query",
        json=body(
            view="ranking",
            group_by="task",
            bucket="hour",
            end=(START + timedelta(days=180)).isoformat(),
        ),
    )
    assert response.status_code == 422


async def test_project_step_lineage_is_narrow_and_recovers_delegate_binding(db, client):
    from cognis.store.usage_queries import accounting_parent_step

    await _resource_tasks(db)
    async with db.factory() as session:
        child = await create_session(
            session,
            conversation_id="conv_usage_0",
            user_email=ACTOR,
            agent_id="agent_usage",
            session_id="session_usage_child",
            parent_session_id="session_usage_0",
        )
        rotated = await create_session(
            session,
            conversation_id="conv_usage_0",
            user_email=ACTOR,
            agent_id="agent_usage",
            session_id="session_usage_rotated",
            previous_session_id=child.session_id,
        )
        for suffix, owner, conversation in [
            ("owner", OTHER, "conv_usage_0"),
            ("conversation", ACTOR, "conv_usage_2"),
        ]:
            await create_session(
                session,
                conversation_id=conversation,
                user_email=owner,
                agent_id="agent_usage",
                session_id=f"session_usage_cross_{suffix}",
                parent_session_id="session_usage_0",
            )
        session.add(
            StepRun(
                step_run_id="step_usage",
                task_id="task_usage_0",
                step_name="run",
                step_type="run",
                agent_id="agent_usage",
                session_id="session_usage_0",
                conversation_id="conv_usage_0",
            )
        )
        await session.flush()
        await session.execute(
            update(UsageHourlyTotalRow)
            .where(UsageHourlyTotalRow.session_id == "session_usage_0")
            .values(session_id=rotated.session_id, step_run_id="step_usage")
        )
        assert await accounting_parent_step(
            session,
            "step_usage",
            child.session_id,
            ACTOR,
            "conv_usage_0",
        ) == ("task_usage_0", "step_usage")
        assert await accounting_parent_step(
            session,
            "step_usage",
            rotated.session_id,
            ACTOR,
            "conv_usage_0",
        ) == ("task_usage_0", "step_usage")
        for suffix, owner, conversation in [
            ("owner", OTHER, "conv_usage_0"),
            ("conversation", ACTOR, "conv_usage_2"),
        ]:
            assert (
                await accounting_parent_step(
                    session,
                    "step_usage",
                    f"session_usage_cross_{suffix}",
                    owner,
                    conversation,
                )
                is None
            )
        assert (
            await accounting_parent_step(
                session,
                "step_usage",
                "session_usage_2",
                ACTOR,
                "conv_usage_2",
            )
            is None
        )
        await session.commit()
    query = body(
        scope={"mode": "project", "project_id": "project_usage"},
        view="ranking",
        group_by="task",
    )
    response = await client.post("/api/v1/usage/query", json=query)
    assert response.status_code == 200, response.text
    assert "task_usage_0" in {g["key"] for g in response.json()["groups"]}
    async with db.factory() as session:
        current = await create_session(
            session,
            conversation_id="conv_usage_0",
            user_email=ACTOR,
            agent_id="agent_usage",
            session_id="session_usage_root_rotated",
            previous_session_id="session_usage_0",
        )
        # Workflow resume can move the mutable step pointer to a later rotation.
        await session.execute(
            update(StepRun)
            .where(StepRun.step_run_id == "step_usage")
            .values(session_id=current.session_id)
        )
        await session.commit()
    response = await client.post("/api/v1/usage/query", json=query)
    assert response.status_code == 200, response.text
    assert "task_usage_0" in {g["key"] for g in response.json()["groups"]}
    async with db.factory() as session:
        await session.execute(
            update(Session)
            .where(Session.session_id == "session_usage_child")
            .values(parent_session_id=None)
        )
        await session.commit()
    response = await client.post("/api/v1/usage/query", json=query)
    assert response.status_code == 200, response.text
    assert "task_usage_0" not in {g["key"] for g in response.json()["groups"]}


async def test_request_cursor_uses_numeric_tie_breaker(db, client):
    async with db.factory() as session:
        await session.execute(
            update(UsageAttemptRow)
            .where(UsageAttemptRow.usage_subject_key == subject_key(ACTOR))
            .values(started_at_observed=START)
        )
        await session.commit()
    first = await client.post("/api/v1/usage/requests", json=body(limit=1))
    assert first.status_code == 200, first.text
    second = await client.post(
        "/api/v1/usage/requests", json=body(limit=1, cursor=first.json()["next_cursor"])
    )
    assert second.status_code == 200, second.text
    ids = [item["attempt_id"] for response in (first, second) for item in response.json()["items"]]
    assert set(ids) == {"attempt_usage_0", "attempt_usage_2"}
    assert len(ids) == 2
    assert second.json()["next_cursor"] is None


@pytest.mark.parametrize("visible", [False, True])
async def test_capture_only_owner_day_cannot_hide_behind_ready_summary(db, client, visible):
    async with db.factory() as session:
        if visible:
            await create_conversation(
                session,
                user_email=ACTOR,
                agent_id="agent_usage",
                context_type="web",
                conversation_id="conv_usage_4",
                project_id="project_usage",
                visibility="project",
            )
            await create_session(
                session,
                conversation_id="conv_usage_4",
                user_email=ACTOR,
                agent_id="agent_usage",
                session_id="session_usage_4",
            )
        await _seed(session, 4, ACTOR)
        await session.flush()
        for model in (UsageDailyTotalRow, UsageHourlyTotalRow, UsageSessionTotalRow):
            await session.execute(delete(model).where(model.owner_key == "accounting_bucket_4"))
        await session.execute(
            update(UsageProjectionStateRow)
            .where(UsageProjectionStateRow.owner_key == "accounting_bucket_4")
            .values(ready=False, capture_only=True)
        )
        await session.commit()
    personal = await client.post("/api/v1/usage/query", json=body())
    assert personal.status_code == 200, personal.text
    assert personal.json()["total"] is None
    assert personal.json()["meta"]["accounting_degraded"] is True
    team = await client.post(
        "/api/v1/usage/query",
        json=body(scope={"mode": "project", "project_id": "project_usage"}),
    )
    assert team.status_code == 200, team.text
    if visible:
        assert team.json()["total"] is None
    else:
        assert team.json()["total"]["finalized_attempts"] == 2


async def test_cost_ranking_accepts_null_currency_unpriced_population(db, client):
    async with db.factory() as session:
        for model, grain in ((UsageDailyTotalRow, "daily"), (UsageHourlyTotalRow, "hourly")):
            row = await session.scalar(
                select(model).where(model.owner_key == "accounting_bucket_2")
            )
            dimensions = UsageDimensions.model_validate(row.dimensions).model_copy(
                update={"currency": None, "basis": None}
            )
            encoded = encode_usage_dimensions(dimensions, grain=grain)
            row.dimensions = encoded.dimensions.model_dump(mode="json")
            row.dimension_key = encoded.dimension_key
            row.currency = row.basis = None
            row.historical_cost = Decimal(0)
            row.historical_priced_count = 0
        await session.commit()
    response = await client.post(
        "/api/v1/usage/query", json=body(view="ranking", group_by="provider", metric="cost")
    )
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["ranking_measure"] == "known_priced_subtotal"
    assert data["groups"][0]["metrics"]["unpriced_attempts"] == 1
    assert Decimal(data["groups"][0]["metrics"]["costs"][0]["known_amount"]) == Decimal("0.125")


@pytest.mark.parametrize("generation", [2, 3, None])
async def test_current_detail_requires_matching_price_generation(db, client, generation):
    async with db.factory() as session:
        fact = await session.scalar(
            select(UsageAttemptRow).where(UsageAttemptRow.attempt_id == "attempt_usage_0")
        )
        session.add(
            UsagePriceRevisionRow(
                revision_id="price_usage_current",
                provider_id="provider_usage",
                model="model_usage",
                effective_at=START,
                recorded_at=START,
                currency="EUR",
                basis="configured_estimate",
                provenance="configured",
                source_revision=None,
                rule_version=1,
                content_digest=sha256(b"{}").hexdigest(),
                definition={},
            )
        )
        if generation is not None:
            session.add(
                UsagePricingStateRow(
                    provider_id="provider_usage",
                    model="model_usage",
                    generation=generation,
                    current_revision_id="price_usage_current",
                    updated_at=START,
                )
            )
        session.add(
            UsageValuationRow(
                owner_key=fact.owner_key,
                bucket_day=fact.bucket_day,
                generation=1,
                source_kind="attempt",
                source_id=fact.attempt_id,
                fact_version=fact.fact_version,
                calculator_version=1,
                pricing_generation=2,
                current_revision_id="price_usage_current",
                current_cost=Decimal("0.5"),
                historical_cost=Decimal("0.125"),
                historical_status="complete",
                current_status="complete",
                currency="USD",
                basis="configured_estimate",
                contribution_digest=sha256(b"{}").hexdigest(),
                contribution={},
                updated_at=START,
            )
        )
        await session.commit()
    response = await client.post(
        "/api/v1/usage/attempts/search", json=body(pricing_basis="current")
    )
    assert response.status_code == 200, response.text
    item = next(row for row in response.json()["items"] if row["attempt_id"] == "attempt_usage_0")
    if generation == 2:
        assert item["costs"][0]["currency"] == "EUR"
        assert Decimal(item["costs"][0]["known_amount"]) == Decimal("0.5")
        assert item["costs"][0]["quality"] == "complete"
        assert item["costs"][0]["priced_attempts"] == 1
    else:
        assert item["costs"] == []
    missing = next(
        row for row in response.json()["items"] if row["attempt_id"] == "attempt_usage_2"
    )
    assert missing["costs"] == []


@asynccontextmanager
async def usage_client(service: UsageQueryService) -> AsyncIterator[httpx.AsyncClient]:
    """Router-only harness with fixture authentication, not full-app auth evidence."""
    app = FastAPI()
    app.include_router(create_usage_router(service), prefix="/api/v1")
    app.dependency_overrides[require_current_user] = lambda: AuthenticatedUser(
        email=ACTOR, role="admin"
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://usage.test"
    ) as value:
        yield value


@pytest.fixture
async def client(db):
    async with usage_client(db.service) as value:
        yield value


async def test_personal_summary_uses_consumer_not_provider_owner(db, client):
    response = await client.post("/api/v1/usage/query", json=body())
    assert response.status_code == 200, response.text
    assert response.json()["total"]["finalized_attempts"] == 2
    assert response.json()["total"]["tokens"]["input"] == 20
    assert Decimal(response.json()["total"]["costs"][0]["known_amount"]) == Decimal("0.25")
    assert len(db.calls) <= (4 if db.dialect == "sqlite" else 5)


async def test_project_private_author_and_managed_excluded(db, client):
    scope = {"mode": "project", "project_id": "project_usage"}
    response = await client.post(
        "/api/v1/usage/attempts/search",
        json=body(scope=scope),
    )
    assert response.status_code == 200, response.text
    values = response.json()["items"]
    # Verified resource pairs distinguish the exact allowed facts without exposing
    # their redacted attempt IDs; private 2 and managed 3 must remain excluded.
    expected = {
        ("session_usage_0", "conv_usage_0"),
        ("session_usage_1", "conv_usage_1"),
    }
    assert len(values) == 2
    assert {(item["session_id"], item["conversation_id"]) for item in values} == expected
    refs = {}
    for item in values:
        assert item["attempt_id"] is None and item["logical_request_id"] is None
        assert "key_id" not in item and "client_id" not in item
        assert "provider_request_id" not in item
        assert item["selection_ref"].startswith("v2.")
        refs[item["session_id"]] = item["selection_ref"]
    assert len(set(refs.values())) == 2
    assert len(db.calls) <= 8
    for session_id, ref in refs.items():
        drill = await client.post(
            "/api/v1/usage/attempts/search",
            json=body(scope=scope, selection_ref=ref),
        )
        assert drill.status_code == 200, drill.text
        selected = drill.json()["items"]
        assert len(selected) == 1
        assert (selected[0]["session_id"], selected[0]["conversation_id"]) == (
            session_id,
            session_id.replace("session_", "conv_", 1),
        )
        assert selected[0]["attempt_id"] is None
        assert selected[0]["logical_request_id"] is None
    async with db.factory() as session:
        from cognis.store.models import Conversation

        conversation = await session.get(Conversation, "conv_usage_1")
        conversation.visibility = "private"
        await session.commit()
    # Previously issued refs narrow fresh authority, never preserve old visibility.
    for session_id, ref in refs.items():
        drill = await client.post(
            "/api/v1/usage/attempts/search",
            json=body(scope=scope, selection_ref=ref),
        )
        assert drill.status_code == 200, drill.text
        assert {item["session_id"] for item in drill.json()["items"]} == (
            {"session_usage_0"} if session_id == "session_usage_0" else set()
        )
    async with db.factory() as session:
        from cognis.store.models import ProjectGrantRow

        await session.execute(
            delete(ProjectGrantRow).where(
                ProjectGrantRow.project_id == "project_usage",
                ProjectGrantRow.grantee_user_email == ACTOR,
            )
        )
        await session.commit()
    for ref in refs.values():
        revoked = await client.post(
            "/api/v1/usage/attempts/search", json=body(scope=scope, selection_ref=ref)
        )
        assert revoked.status_code == 403, revoked.text


async def test_cursor_binds_filters_and_revalidates_visibility(db, client):
    response = await client.post("/api/v1/usage/attempts/search", json=body(limit=1))
    assert response.status_code == 200, response.text
    token = response.json()["next_cursor"]
    assert token
    altered = await client.post(
        "/api/v1/usage/attempts/search",
        json=body(
            limit=1,
            cursor=token,
            filters={"provider_ids": ["another_provider"]},
        ),
    )
    assert altered.status_code == 422
    next_page = await client.post("/api/v1/usage/attempts/search", json=body(limit=1, cursor=token))
    assert next_page.status_code == 200, next_page.text
    assert next_page.json()["items"][0]["attempt_id"] != response.json()["items"][0]["attempt_id"]


async def test_unconfigured_global_index_is_not_a_scan(db):
    # Remove only this isolated fixture's index; production never gets a scan fallback.
    async with db.engine.begin() as connection:
        await connection.execute(text("DROP INDEX ix_usage_attempt_time"))
    unindexed = await create_usage_query_service(
        db.factory, cursor_secret=b"usage-test-cursor-signing-key-32-bytes"
    )
    with pytest.raises(UsageQueryError, match="time-leading"):
        await unindexed.attempts(
            ACTOR,
            UsageAttemptQuery(
                start=START,
                end=END,
                scope=UsageScope(mode="admin"),
            ),
        )


async def test_sidebar_service_returns_valid_contract(db):
    response = await db.service.session_summaries(
        ACTOR,
        UsageSummaryRequest.model_validate(
            {
                "scope": {"mode": "project", "project_id": "project_usage"},
                "items": [
                    {"kind": "conversation", "id": f"conv_usage_{index}"} for index in range(4)
                ],
            }
        ),
    )
    assert [item.available for item in response.items] == [True, True, False, False]


@pytest.mark.parametrize("inclusion,count", [("direct", 1), ("inclusive", 2)])
async def test_conversation_tree_matches_sidebar_and_labels_children(db, client, inclusion, count):
    await _resource_tasks(db)
    async with db.factory() as session:
        await session.execute(
            update(Session)
            .where(Session.session_id == "session_usage_2")
            .values(
                conversation_id="conv_usage_0",
                parent_session_id="session_usage_0",
                delegation_mode="delegate",
            )
        )
        for model in (UsageHourlyTotalRow, UsageSessionTotalRow):
            await session.execute(
                update(model)
                .where(model.session_id == "session_usage_2")
                .values(conversation_id="conv_usage_0")
            )
        await session.commit()
    summary = await db.service.session_summaries(
        ACTOR,
        UsageSummaryRequest.model_validate(
            {
                "items": [{"kind": "conversation", "id": "conv_usage_0"}],
                "inclusion": inclusion,
            }
        ),
    )
    for view, dimension in (
        ("overview", None),
        ("trend", None),
        ("ranking", "session"),
        ("ranking", "conversation"),
    ):
        response = await client.post(
            "/api/v1/usage/query",
            json=body(
                view=view,
                group_by=dimension,
                bucket="hour",
                filters={"conversation_tree": {"id": "conv_usage_0", "inclusion": inclusion}},
            ),
        )
        assert response.status_code == 200, response.text
        data = response.json()
        assert data["total"]["finalized_attempts"] == count
        assert data["total"]["costs"] == summary.items[0].metrics.model_dump(mode="json")["costs"]
        if view == "trend":
            assert data["buckets"][0]["metrics"]["finalized_attempts"] == count
        if dimension == "session":
            assert {g["label"] for g in data["groups"]} == (
                {"Conversation 0"} if count == 1 else {"Conversation 0", "Conversation 0 · Usage"}
            )
    sessions = await client.post("/api/v1/usage/sessions", json=body())
    assert sessions.status_code == 200, sessions.text
    assert {i["session_label"] for i in sessions.json()["items"]} == {
        "Conversation 0",
        "Conversation 0 · Usage",
    }


async def test_conversation_tree_excludes_unauthorized_and_ambiguous_membership(db, client):
    for identifier in ("conv_usage_3", "conv_usage_0"):
        if identifier == "conv_usage_0":
            async with db.factory() as session:
                await session.execute(
                    update(Session)
                    .where(Session.session_id == "session_usage_0")
                    .values(previous_session_id="session_usage_0")
                )
                await session.commit()
        response = await client.post(
            "/api/v1/usage/query",
            json=body(
                bucket="hour",
                filters={"conversation_tree": {"id": identifier, "inclusion": "inclusive"}},
            ),
        )
        assert response.status_code == 200, response.text
        data = response.json()
        if identifier == "conv_usage_0":
            assert data["total"] is None
            assert data["meta"]["projection"]["state"] == "unavailable"
        else:
            assert data["total"]["finalized_attempts"] == 0


@pytest.mark.parametrize(
    "conversation,parent,expected",
    [
        ("conv_usage_2", "session_usage_0", "Conversation 2"),
        ("conv_usage_0", "session_usage_0", "Conversation 0 · Usage"),
        ("conv_usage_1", "session_usage_1", None),
    ],
)
async def test_session_label_distinguishes_own_conversation_and_readable_parent(
    db, client, conversation, parent, expected
):
    await _resource_tasks(db)
    async with db.factory() as session:
        await session.execute(
            update(Session)
            .where(Session.session_id == "session_usage_2")
            .values(
                conversation_id=conversation, parent_session_id=parent, delegation_mode="delegate"
            )
        )
        await session.execute(
            update(Conversation)
            .where(Conversation.user_email == OTHER)
            .values(visibility="private")
        )
        await session.commit()
    response = await client.post(
        "/api/v1/usage/query",
        json=body(
            view="ranking",
            group_by="session",
        ),
    )
    assert response.status_code == 200, response.text
    child = next(g for g in response.json()["groups"] if g["key"] == "session_usage_2")
    assert child["label"] == expected


@pytest.mark.parametrize("inclusion", ["self", "direct", "inclusive"])
async def test_sidebar_member_without_usage_does_not_hide_conversation(db, inclusion):
    """A new/idle session in the conversation has no totals; it is not unready."""
    async with db.factory() as session:
        await create_session(
            session,
            conversation_id="conv_usage_0",
            user_email=ACTOR,
            agent_id="agent_usage",
            session_id="session_usage_0_idle",
        )
        await session.commit()
    response = await db.service.session_summaries(
        ACTOR,
        UsageSummaryRequest.model_validate(
            {"items": [{"kind": "conversation", "id": "conv_usage_0"}], "inclusion": inclusion}
        ),
    )
    item = response.items[0]
    assert item.available
    assert item.metrics is not None and item.metrics.finalized_attempts == 1
    assert response.meta.projection.state == "ready"


async def test_sidebar_one_fetch_and_no_private_author(db, client):
    response = await client.post(
        "/api/v1/usage/session-summaries",
        json={
            "scope": {"mode": "project", "project_id": "project_usage"},
            "items": [{"kind": "conversation", "id": f"conv_usage_{index}"} for index in range(4)],
        },
    )
    assert response.status_code == 200, response.text
    assert [item["available"] for item in response.json()["items"]] == [True, True, False, False]
    summary_fetches = [
        sql for sql in db.calls if "usage_selectors" in sql and sql.lstrip().startswith("WITH")
    ]
    assert len(summary_fetches) == 1
    assert len(db.calls) <= (4 if db.dialect == "sqlite" else 5)


@pytest.mark.parametrize("child_status,available", [("active", True), ("deleted", False)])
async def test_sidebar_direct_metadata_without_usage_preserves_visibility(
    db: SimpleNamespace, child_status: str, available: bool
) -> None:
    async with db.factory() as session:
        conversation = await create_conversation(
            session,
            user_email=ACTOR,
            agent_id="agent_usage",
            conversation_id="conv_direct_child",
            context_type="agent_work",
        )
        conversation.status = child_status
        session.add(
            ManagedConversationLink(
                link_id="link_direct_child",
                root_link_id="link_direct_child",
                user_email=ACTOR,
                controller_agent_id="agent_usage",
                controller_conversation_id="conv_usage_0",
                target_agent_id="agent_usage",
                target_conversation_id="conv_direct_child",
                depth=1,
                conversation_state="closed",
                turn_state="completed",
            )
        )
        await session.commit()
    result = await db.service.session_summaries(
        ACTOR,
        UsageSummaryRequest(
            items=[{"kind": "conversation", "id": "conv_usage_0"}], inclusion="inclusive"
        ),
    )
    assert result.items[0].available is available
    if available:
        assert result.items[0].physical_sessions == 1
        assert result.items[0].metrics is not None
    else:
        assert result.items[0].physical_sessions is None
        assert result.items[0].metrics is None


async def test_sidebar_direct_membership_budget_applies_without_usage(db: SimpleNamespace) -> None:
    async with db.factory() as session:
        await create_conversation(
            session,
            user_email=ACTOR,
            agent_id="agent_usage",
            conversation_id="conv_direct_root",
            context_type="web",
        )
        await session.execute(
            insert(Conversation),
            [
                {
                    "conversation_id": f"conv_direct_limit_{index:05d}",
                    "user_email": ACTOR,
                    "agent_id": "agent_usage",
                    "context_type": "agent_work",
                }
                for index in range(10000)
            ],
        )
        await session.execute(
            insert(ManagedConversationLink),
            [
                {
                    "link_id": f"link_direct_limit_{index:05d}",
                    "root_link_id": f"link_direct_limit_{index:05d}",
                    "user_email": ACTOR,
                    "controller_agent_id": "agent_usage",
                    "controller_conversation_id": "conv_direct_root",
                    "target_agent_id": "agent_usage",
                    "target_conversation_id": f"conv_direct_limit_{index:05d}",
                    "depth": 1,
                    "conversation_state": "closed",
                    "turn_state": "completed",
                }
                for index in range(10000)
            ],
        )
        await session.commit()
    with pytest.raises(UsageQueryError) as refused:
        await db.service.session_summaries(
            ACTOR,
            UsageSummaryRequest(
                items=[{"kind": "conversation", "id": "conv_direct_root"}], inclusion="inclusive"
            ),
        )
    assert refused.value.code == "query_budget_exceeded" and refused.value.status == 422


async def test_requests_drill_to_actual_attempt_with_same_filters(db, client):
    response = await client.post(
        "/api/v1/usage/requests",
        json=body(
            filters={"session_ids": ["session_usage_0"]},
        ),
    )
    assert response.status_code == 200, response.text
    item = response.json()["items"][0]
    detail = await client.post(
        "/api/v1/usage/attempts/search", json=body(filters=item["drill_filters"])
    )
    assert detail.status_code == 200, detail.text
    assert [row["attempt_id"] for row in detail.json()["items"]] == ["attempt_usage_0"]


async def test_server_population_is_once_only_and_provider_allocation_stays_null(db, client):
    async with db.factory() as session:
        row = await session.get(UsageRequestStatsRow, "request_usage_0")
        server = server_event(row)
        assert server is not None
        dimensions = UsageDimensions(
            record_kind="request",
            usage_subject_key=row.usage_subject_key,
            purpose=server.attribution.purpose,
            operation="request",
            **{
                name: value
                for name, value in server.attribution.resources.model_dump().items()
                if name in UsageDimensions.model_fields
            },
        )
        for model, grain, extra in (
            (UsageDailyTotalRow, "daily", {}),
            (UsageHourlyTotalRow, "hourly", {"bucket_hour": 0}),
        ):
            encoded = encode_usage_dimensions(dimensions, grain=grain)
            values = {
                **dict.fromkeys(MEASURES, 0),
                **request_metric_values(server=server, client=None).model_dump(),
                **{
                    name: value
                    for name, value in encoded.dimensions.model_dump(mode="json").items()
                    if name in model.__table__.c
                },
                "owner_key": row.owner_key,
                "bucket_day": START.date(),
                "generation": 1,
                "dimension_key": encoded.dimension_key,
                "dimensions": encoded.dimensions.model_dump(mode="json"),
                "updated_at": END,
            }
            session.add(model(**values, **extra))
        await session.commit()
    response = await client.post("/api/v1/usage/query", json=body(group_by="provider"))
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["total"]["finalized_attempts"] == 2
    assert result["total"]["tokens"]["input"] == 20
    assert result["total"]["performance"]["logical_completed_requests"] == 1
    assert result["total"]["logical_performance"]["server_boundary"]["seconds_mean"] == 1
    unallocated = next(group for group in result["groups"] if group["key"] is None)
    assert unallocated["metrics"]["finalized_attempts"] == 0
    assert unallocated["metrics"]["performance"]["logical_completed_requests"] == 1
    allocated = next(group for group in result["groups"] if group["key"] == "provider_usage")
    assert allocated["metrics"]["performance"]["logical_completed_requests"] is None
    ranked = await client.post(
        "/api/v1/usage/query", json=body(view="ranking", metric="cost", group_by="provider")
    )
    assert ranked.status_code == 200, ranked.text
    assert [group["key"] for group in ranked.json()["groups"]] == ["provider_usage"]
    assert ranked.json()["total"]["performance"]["logical_completed_requests"] == 1


async def test_requests_remain_visible_without_optional_stats(db, client):
    async with db.factory() as session:
        await session.execute(
            delete(UsageRequestStatsRow).where(UsageRequestStatsRow.request_id == "request_usage_0")
        )
        await session.commit()
    response = await client.post(
        "/api/v1/usage/requests",
        json=body(filters={"session_ids": ["session_usage_0"]}),
    )
    assert response.status_code == 200, response.text
    assert response.json()["pagination_unit"] == "attempt"
    item = response.json()["items"][0]
    assert item["attempt_id"] == "attempt_usage_0"
    assert item["state"] == "succeeded"
    assert item["logical_outcome"] is None
    assert item["logical_request_started_at"] is None
    assert item["timing"] is None


async def test_global_summary_uses_committed_time_indexes(db, client):
    response = await client.post("/api/v1/usage/query", json=body(scope={"mode": "admin"}))
    assert response.status_code == 200, response.text
    assert response.json()["total"]["finalized_attempts"] == 4


async def test_unknown_dimension_version_is_unavailable_not_a_raw_fallback(db, client):
    async with db.factory() as session:
        rows = await session.scalars(
            select(UsageDailyTotalRow).where(
                UsageDailyTotalRow.usage_subject_key == subject_key(ACTOR)
            )
        )
        for row in rows:
            row.dimensions = dict(row.dimensions, schema_version=3)
        await session.commit()
    response = await client.post("/api/v1/usage/query", json=body())
    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "usage_source_incompatible"


async def test_tool_cannot_inherit_admin_or_accept_forged_context(db):
    handler = build_usage_tool_handlers(
        db.service,
        RuntimeAccessContext(
            user_email=ACTOR,
            agent_id="agent_usage",
            agent_owner_email=ACTOR,
        ),
    )["query_usage"]
    result = await handler(
        {
            "request": {
                "operation": "query",
                "query": body(
                    scope={"mode": "admin"},
                ),
            }
        },
        SimpleNamespace(runtime_metadata={"user_email": OTHER}),
    )
    assert result["error"] == "usage_access_denied"
    result = await handler({"request": {"operation": "attempts", "query": body()}}, None)
    assert len(result["items"]) == 2


async def test_degraded_projection_is_not_zero(db, client):
    async with db.factory() as session:
        await session.execute(update(UsageProjectionStateRow).values(ready=False))
        await session.commit()
    response = await client.post("/api/v1/usage/query", json=body())
    assert response.status_code == 200, response.text
    assert response.json()["total"] is None
    assert response.json()["meta"]["projection"]["state"] == "building"


async def test_quota_history_http_query_normalization_and_rejection() -> None:
    """Exercise the shipping query shape through the real HTTP adapter."""
    calls = []

    async def history(actor, provider, query, *, actor_role):
        calls.append((actor, provider, query, actor_role))
        if query.scope.mode == "admin":
            raise UsageAccessError("Forbidden")
        return {"provider_id": provider, "samples": [], "limitations": []}

    app = FastAPI()
    app.include_router(
        create_usage_router(SimpleNamespace(provider_history=history)), prefix="/api/v1"
    )
    app.dependency_overrides[require_current_user] = lambda: AuthenticatedUser(
        email=ACTOR, role="admin"
    )
    url = "/api/v1/usage/providers/provider_usage/history"
    base = {"start": START.isoformat(), "end": END.isoformat()}
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://usage.test"
    ) as client:
        response = await client.get(
            url, params={**base, "scope": '{"mode":"personal"}', "limit": "100"}
        )
        assert response.status_code == 200
        assert calls[-1][0:2] == (ACTOR, "provider_usage")
        assert calls[-1][2].scope == UsageScope(mode="personal")
        assert calls[-1][2].limit == 100
        assert calls[-1][3] == "admin"

        response = await client.get(url, params=base)
        assert response.status_code == 200
        assert calls[-1][2].scope == UsageScope()
        assert calls[-1][2].limit == 100

        response = await client.get(url, params={**base, "scope": '{"mode":"admin"}'})
        assert response.status_code == 403
        assert calls[-1][2].scope.mode == "admin"

        invalid = [
            {"scope": "{"},
            {"scope": '{"mode":"personal","extra":true}'},
            {"scope": '{"mode":"project"}'},
            {"limit": "1.5"},
            {"limit": "0"},
            {"limit": "501"},
            {"unexpected": "value"},
        ]
        previous = len(calls)
        for case in invalid:
            assert (await client.get(url, params={**base, **case})).status_code == 422
        duplicate = list(base.items()) + [("limit", "1"), ("limit", "2")]
        assert (await client.get(url, params=duplicate)).status_code == 422
        assert len(calls) == previous
