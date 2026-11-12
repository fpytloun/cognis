"""Proxy API namespaces, retained selections and private-token boundaries."""

from __future__ import annotations

import base64
from datetime import timedelta

import pytest
from sqlalchemy import delete, inspect, select, text

from cognis.core.usage.dimensions import UsageDimensions, encode_usage_dimensions
from cognis.core.usage.query_service import create_usage_query_service
from cognis.core.usage.sessions import session_correlation, session_dimension_key
from cognis.models.usage import UsageAttemptContext, UsageClientLabels, UsageResourceBinding
from cognis.store.queries import create_task
from cognis.store.usage.models import (
    UsageAttemptRow,
    UsageRequestStatsRow,
    UsageSessionTotalRow,
)
from tests.integration.test_usage_query_api import (
    ACTOR,
    OTHER,
    _seed,
    body,
    usage_client,
)
from tests.integration.test_usage_query_api import (
    client as client,
)
from tests.integration.test_usage_query_api import (
    db as db,
)

pytestmark = pytest.mark.asyncio


async def proxy(
    session,
    index,
    *,
    actor=ACTOR,
    client_id=None,
    label="shared-run",
    service_id=None,
    cognis=False,
    visible=False,
):
    await _seed(session, index, actor)
    if visible and not cognis:
        task = await create_task(
            session,
            task_id=f"task_proxy_{index}",
            created_by=actor,
            agent_id="agent_usage",
            title="Proxy task",
            project_id="project_usage",
            source_type="api",
        )
        task.visibility = "project"
    fact = await session.scalar(
        select(UsageAttemptRow).where(UsageAttemptRow.attempt_id == f"attempt_usage_{index}")
    )
    context = UsageAttemptContext.model_validate(fact.context_payload)
    identity = context.attribution.identity.model_copy(
        update={
            "client_id": client_id,
            "service_id": service_id,
        }
    )
    resources = (
        context.attribution.resources
        if cognis
        else UsageResourceBinding(
            project_id="project_usage",
            agent_id="agent_usage",
            task_id=f"task_proxy_{index}" if visible else None,
            provenance="controller",
        )
    )
    attribution = context.attribution.model_copy(
        update={
            "identity": identity,
            "resources": resources,
            "client_labels": UsageClientLabels(session_id=label),
        }
    )
    context = context.model_copy(update={"attribution": attribution})
    fact.context_payload = context.model_dump(mode="json")
    fact.context_digest = context.observation_digest()
    fact.client_id, fact.client_session_id, fact.service_id = client_id, label, service_id
    fact.session_id = resources.session_id
    fact.conversation_id = resources.conversation_id
    fact.task_id, fact.step_run_id = resources.task_id, resources.step_run_id
    previous = await session.scalar(
        select(UsageSessionTotalRow).where(UsageSessionTotalRow.owner_key == fact.owner_key)
    )
    values = {
        column.name: getattr(previous, column.name)
        for column in UsageSessionTotalRow.__table__.columns
    }
    await session.delete(previous)
    correlation = session_correlation(attribution)
    if correlation is not None:
        dimensions = UsageDimensions.model_validate(values["dimensions"]).model_copy(
            update={
                "session_id": resources.session_id,
                "conversation_id": resources.conversation_id,
                "task_id": resources.task_id,
                "step_run_id": resources.step_run_id,
            }
        )
        encoded = encode_usage_dimensions(dimensions, grain="session")
        values.update(
            session_kind=correlation.session_kind,
            session_id=correlation.session_id,
            client_id=correlation.client_id,
            client_session_id=correlation.client_session_id,
            conversation_id=resources.conversation_id,
            task_id=resources.task_id,
            step_run_id=resources.step_run_id,
            dimensions=encoded.dimensions.model_dump(mode="json"),
            dimension_key=session_dimension_key(dimensions, correlation),
        )
        session.add(UsageSessionTotalRow(**values))
    await session.execute(
        delete(UsageRequestStatsRow).where(UsageRequestStatsRow.request_id == fact.request_id)
    )
    await session.flush()
    return fact


async def test_proxy_namespaces_stats_loss_and_private_refs(db, client):
    async with db.factory() as session:
        for index, actor, trusted in (
            (4, ACTOR, None),
            (5, ACTOR, "trusted-a"),
            (6, ACTOR, "trusted-b"),
            (7, OTHER, "trusted-a"),
        ):
            await proxy(session, index, actor=actor, client_id=trusted)
        await proxy(session, 8, service_id="intaris")
        await proxy(session, 9, service_id="mnemory")
        await proxy(session, 10, cognis=True)
        await session.commit()
    response = await client.post("/api/v1/usage/sessions", json=body(session_type="proxy", limit=2))
    assert response.status_code == 200, response.text
    first = response.json()
    assert len(first["items"]) == 2
    second = await client.post(
        "/api/v1/usage/sessions",
        json=body(session_type="proxy", limit=2, cursor=first["next_cursor"]),
    )
    assert second.status_code == 200, second.text
    items = first["items"] + second.json()["items"]
    assert {item["client_id"] for item in items} == {None, "trusted-a", "trusted-b"}
    assert all(
        item["session_id"] is None and item["totals_scope"] == "retained_detail" for item in items
    )
    for item in items:
        ref = item["selection_ref"]
        wire = base64.urlsafe_b64decode(ref[3:] + "=" * (-len(ref[3:]) % 4))
        assert b"shared-run" not in wire and ACTOR.encode() not in wire
        requests = await client.post("/api/v1/usage/requests", json=body(selection_ref=ref))
        assert requests.status_code == 200, requests.text
        rows = requests.json()["items"]
        assert len(rows) == 1 and rows[0]["logical_outcome"] is None
        assert requests.json()["meta"]["availability"]["resolution"] == "retained"
        detail = await client.post(
            "/api/v1/usage/attempts/search", json=body(selection_ref=rows[0]["selection_ref"])
        )
        assert detail.status_code == 200, detail.text
        assert len(detail.json()["items"]) == 1


async def test_expired_selection_cannot_resurrect_reused_label(db, client):
    async with db.factory() as session:
        old = await proxy(session, 4)
        await session.commit()
        old_id = old.attempt_id
    result = await client.post("/api/v1/usage/sessions", json=body(session_type="proxy"))
    assert result.status_code == 200, result.text
    ref = result.json()["items"][0]["selection_ref"]
    async with db.factory() as session:
        await session.execute(delete(UsageAttemptRow).where(UsageAttemptRow.attempt_id == old_id))
        reused = await proxy(session, 5)
        # Simulate a later registration with an old, backdated start time.
        from datetime import UTC, datetime

        reused.registered_at = datetime.now(UTC) + timedelta(seconds=2)
        await session.commit()
    result = await client.post("/api/v1/usage/requests", json=body(selection_ref=ref))
    assert result.status_code == 200, result.text
    assert result.json()["items"] == []
    tampered = ref[:10] + ("A" if ref[10] != "A" else "B") + ref[11:]
    result = await client.post("/api/v1/usage/requests", json=body(selection_ref=tampered))
    assert result.status_code == 422, result.text


async def test_project_proxy_redaction_and_current_membership(db, client):
    async with db.factory() as session:
        await proxy(session, 4, actor=OTHER, client_id="private-client", visible=True)
        await session.commit()
    scope = {"mode": "project", "project_id": "project_usage"}
    result = await client.post(
        "/api/v1/usage/sessions", json=body(session_type="proxy", scope=scope)
    )
    assert result.status_code == 200, result.text
    item = result.json()["items"][0]
    assert item["client_id"] is None and item["client_session_id"] is None
    requests = await client.post(
        "/api/v1/usage/requests", json=body(scope=scope, selection_ref=item["selection_ref"])
    )
    assert requests.status_code == 200, requests.text
    request = requests.json()["items"][0]
    assert request["request_id"] is None and request["attempt_id"] is None
    assert not request["drill_filters"].get("logical_request_ids")
    detail = await client.post(
        "/api/v1/usage/attempts/search",
        json=body(scope=scope, selection_ref=request["selection_ref"]),
    )
    assert detail.status_code == 200, detail.text
    assert detail.json()["items"][0]["logical_request_id"] is None
    assert detail.json()["items"][0]["attempt_id"] is None
    async with db.factory() as session:
        from cognis.store.models import ProjectGrantRow

        await session.execute(
            delete(ProjectGrantRow).where(ProjectGrantRow.grantee_user_email == ACTOR)
        )
        await session.commit()
    revoked = await client.post(
        "/api/v1/usage/requests", json=body(scope=scope, selection_ref=item["selection_ref"])
    )
    assert revoked.status_code == 403, revoked.text


async def test_proxy_search_and_selection_current_visibility(db, client):
    async with db.factory() as session:
        await proxy(session, 4, actor=OTHER, client_id="private-client", visible=True)
        await session.commit()
    private = await client.post(
        "/api/v1/usage/sessions", json=body(session_type="proxy", search="shared-run")
    )
    assert private.status_code == 200 and private.json()["items"] == []
    scope = {"mode": "project", "project_id": "project_usage"}
    page = await client.post("/api/v1/usage/sessions", json=body(session_type="proxy", scope=scope))
    assert page.status_code == 200, page.text
    ref = page.json()["items"][0]["selection_ref"]
    forbidden_search = await client.post(
        "/api/v1/usage/sessions",
        json=body(session_type="proxy", scope=scope, search="shared-run"),
    )
    assert forbidden_search.status_code == 422, forbidden_search.text
    async with db.factory() as session:
        from cognis.store.models import Task

        task = await session.get(Task, "task_proxy_4")
        task.visibility = "private"
        await session.commit()
    hidden = await client.post("/api/v1/usage/requests", json=body(scope=scope, selection_ref=ref))
    assert hidden.status_code == 200 and hidden.json()["items"] == [], hidden.text


async def test_request_ref_keeps_indexed_retry_trail_without_stats(db, client):
    async with db.factory() as session:
        fact = await proxy(session, 4)
        values = {
            column.name: getattr(fact, column.name)
            for column in UsageAttemptRow.__table__.columns
            if column.name != "row_id"
        }
        values.update(attempt_id="attempt_usage_4_retry", attempt_ordinal=1, projected_version=0)
        session.add(UsageAttemptRow(**values))
        await session.commit()
    requests = await client.post("/api/v1/usage/requests", json=body())
    assert requests.status_code == 200, requests.text
    selected = next(
        item for item in requests.json()["items"] if item["request_id"] == fact.request_id
    )
    trail = await client.post(
        "/api/v1/usage/attempts/search", json=body(selection_ref=selected["selection_ref"])
    )
    assert trail.status_code == 200, trail.text
    assert {item["attempt_id"] for item in trail.json()["items"]} == {
        fact.attempt_id,
        "attempt_usage_4_retry",
    }


async def test_global_proxy_source_with_time_leading_index(db, client):
    async with db.engine.connect() as connection:
        indexes = await connection.run_sync(
            lambda conn: inspect(conn).get_indexes("usage_session_totals")
        )
    day_index = next(item for item in indexes if item["name"] == "ix_usage_session_day")
    assert day_index["column_names"] == ["bucket_day", "owner_key", "generation"]
    options = day_index.get("dialect_options", {})
    assert options.get("sqlite_where") is None
    assert options.get("postgresql_where") is None
    async with db.factory() as session:
        await proxy(session, 4, client_id="global-client-actor")
        await proxy(session, 5, actor=OTHER, client_id="global-client-other")
        await session.commit()
    result = await client.post(
        "/api/v1/usage/sessions",
        json=body(session_type="proxy", scope={"mode": "admin"}),
    )
    assert result.status_code == 200, result.text
    items = result.json()["items"]
    expected = {
        "global-client-actor": "attempt_usage_4",
        "global-client-other": "attempt_usage_5",
    }
    assert {item["client_id"] for item in items} == set(expected)
    assert len(items) == 2
    assert all(item["session_id"] is None and item["selection_ref"] for item in items)
    for item in items:
        drill = await client.post(
            "/api/v1/usage/requests",
            json=body(scope={"mode": "admin"}, selection_ref=item["selection_ref"]),
        )
        assert drill.status_code == 200, drill.text
        assert [row["attempt_id"] for row in drill.json()["items"]] == [expected[item["client_id"]]]


async def test_global_proxy_source_fails_closed_without_time_leading_index(db):
    # Only this fixture's UUID PostgreSQL schema or temporary SQLite DB is altered.
    async with db.engine.begin() as connection:
        await connection.execute(text("DROP INDEX ix_usage_session_day"))
        indexes = await connection.run_sync(
            lambda conn: inspect(conn).get_indexes("usage_session_totals")
        )
    assert not any(item["column_names"][:1] == ["bucket_day"] for item in indexes)
    unindexed = await create_usage_query_service(
        db.factory, cursor_secret=b"usage-test-cursor-signing-key-32-bytes"
    )
    async with usage_client(unindexed) as client:
        result = await client.post(
            "/api/v1/usage/sessions",
            json=body(session_type="proxy", scope={"mode": "admin"}),
        )
    assert result.status_code == 503, result.text
    assert result.json()["detail"]["code"] == "usage_index_unavailable"
