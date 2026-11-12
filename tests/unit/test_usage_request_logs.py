"""Real retained logical requests, including retries, private selectors and expiry."""

from datetime import timedelta
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import func, select

from cognis.api.common import require_current_user
from cognis.api.routes.usage import create_usage_router
from cognis.core.usage.access import UsageAccessError
from cognis.core.usage.maintenance import UsageMaintenanceScope
from cognis.core.usage.query_models import (
    UsageQuery,
    UsageRequestDetailQuery,
    UsageRequestQuery,
    UsageScope,
)
from cognis.core.usage.query_service import create_usage_query_service
from cognis.core.usage.query_service_errors import UsageQueryError
from cognis.models.usage import (
    UsageAttemptFinalized,
    UsageHTTPMetadata,
    UsageRequestStats,
    UsageServerSpan,
)
from cognis.store.models import ProjectRow, User
from cognis.store.queries import create_user
from cognis.store.usage.models import UsageArchivedTotalRow, UsageDailyTotalRow, UsageRequestLogRow
from tests.usage_storage_harness import usage_storage as usage_storage


async def seed(harness, *, retry_delay=None, legacy=False, sequence=1, media=False):
    started_at = harness.clock()
    event = harness.event(sequence)
    data = event.model_dump()
    if media:
        data["context"]["selection"].update(
            operation="image_generate", captured_price_revision_id="price_1"
        )
        data["usage"] = {
            "semantic_profile": "unknown",
            "semantic_version": 1,
            "cache_read_semantics": "unknown",
            "cache_write_semantics": "unknown",
            "reasoning_semantics": "unknown",
            "completeness": "reported",
            "extras": [
                {"name": "output_image_count", "unit": "images", "value": 2, "source": "provider"}
            ],
            "provider_cost": {"amount": "0.125", "currency": "USD"},
        }
    data["context"]["attribution"]["identity"].update(
        traffic_origin="unknown" if legacy else "api",
        auth_type="api_key",
        client_id="key-client-123",
        key_record_id=None if legacy else "key-client-123",
        resource_owner="usage+test@cognis.test",
    )
    if not legacy:
        data["context"]["request_started_at"] = started_at
    else:
        data["context"]["attribution"]["client_labels"] = {
            "session_id": "upgrade-client-session",
        }
    first = UsageAttemptFinalized.model_validate(data)
    retry = harness.event(sequence + 1).model_dump()
    retry_id = retry["context"]["trace"]["attempt_id"]
    retry["context"] = first.context.model_dump()
    retry["context"]["trace"].update(
        attempt_id=retry_id,
        attempt_ordinal=1,
        retry_of_attempt_id=first.context.trace.attempt_id,
    )
    if retry_delay:
        harness.clock.now += retry_delay
        retry["context"]["started_at"] = harness.clock()
        retry["finalized_at"] = harness.clock()
    if media:
        retry["usage"] = data["usage"]
    second = UsageAttemptFinalized.model_validate(retry)
    stats = UsageRequestStats(
        observed_at=harness.clock(),
        observation_id=f"server-log-final-{sequence}",
        stats_scope="server",
        request_id=first.context.trace.request_id,
        request_started_at=started_at,
        attribution=first.context.attribution,
        controller_owner_id="storage_test",
        coverage_epoch_id=first.context.coverage_epoch_id,
        logical_outcome="success",
        server=UsageServerSpan(
            received_at=started_at,
            completed_at=harness.clock() + timedelta(seconds=1),
            elapsed_ms=(harness.clock() - started_at).total_seconds() * 1000 + 1000,
            ttft_ms=100,
        ),
        http_metadata=None
        if legacy
        else UsageHTTPMetadata(
            request_path="/v1/chat/completions",
            streaming=True,
            http_status_code=200,
            user_agent="OpenAI/Python",
            app_name="test-client",
            finish_reason="stop",
        ),
    )
    harness.writer.try_emit(first)
    harness.writer.try_emit(second)
    harness.writer.try_emit(stats)
    await harness.pump()
    actor = first.context.attribution.identity.usage_subject_id
    async with harness.engine.begin() as connection:
        await connection.run_sync(lambda c: User.__table__.create(c, checkfirst=True))
        await connection.run_sync(lambda c: ProjectRow.__table__.create(c, checkfirst=True))
    async with harness.sessions.begin() as session:
        if await session.get(User, actor) is None:
            await create_user(session, actor, "Usage", "", role="admin")
            await create_user(session, "other@tests.local", "Other", "", role="user")
    service = await create_usage_query_service(
        harness.sessions, cursor_secret=b"retained-log-test-secret-32-bytes-long"
    )
    start = harness.clock().replace(hour=0, minute=0, second=0, microsecond=0)
    return actor, service, start, start + timedelta(days=1), first


@pytest.mark.asyncio
async def test_request_detail_pages_retries_outside_header_window(usage_storage):
    usage_storage.clock.now -= timedelta(seconds=2)
    actor, service, _, _, event = await seed(usage_storage, retry_delay=timedelta(seconds=2))
    start = event.context.started_at - timedelta(seconds=1)
    end = event.context.started_at + timedelta(seconds=1)
    page = await service.requests(
        actor, UsageRequestQuery(start=start, end=end, pagination_unit="request")
    )
    assert len(page.items) == 1
    selection = page.items[0].selection_ref
    detail_query = UsageRequestDetailQuery(start=start, end=end, selection_ref=selection, limit=1)
    first_page = await service.request_detail(actor, detail_query)
    assert first_page.request.attempt_count == 2
    assert len(first_page.attempts) == 1
    assert first_page.attempts[0].started_at >= end
    assert first_page.next_cursor is not None
    second_page = await service.request_detail(
        actor, detail_query.model_copy(update={"cursor": first_page.next_cursor})
    )
    assert len(second_page.attempts) == 1
    assert second_page.attempts[0].attempt_id == event.context.trace.attempt_id
    assert second_page.next_cursor is None


@pytest.mark.asyncio
async def test_unique_request_list_detail_and_summary_dimensions(usage_storage):
    actor, service, start, end, event = await seed(usage_storage)
    query = UsageRequestQuery(
        start=start,
        end=end,
        pagination_unit="request",
        filters={
            "traffic_origins": ["api"],
            "auth_types": ["api_key"],
            "key_ids": ["key-client-123"],
            "http_status_codes": [200],
        },
    )
    page = await service.requests(actor, query)
    assert page.pagination_unit == "request"
    assert len(page.items) == 1
    item = page.items[0]
    assert item.request_id == event.context.trace.request_id
    assert item.attempt_count == 2
    assert item.retry_count == 1
    assert item.tokens.input == 2000
    assert item.unpriced_attempts == 2
    assert item.user_agent == "OpenAI/Python"
    assert item.latency_seconds == 1
    assert item.finish_reason == "stop"
    assert item.state == "succeeded"
    detail = await service.request_detail(
        actor,
        UsageRequestDetailQuery(
            start=start,
            end=end,
            selection_ref=item.selection_ref,
            limit=1,
        ),
    )
    assert detail.request.attempt_count == 2
    assert len(detail.attempts) == 1
    assert detail.next_cursor is not None
    app = FastAPI()
    app.include_router(create_usage_router(service), prefix="/api/v1")
    app.dependency_overrides[require_current_user] = lambda: SimpleNamespace(
        email=actor, role="admin"
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://usage.tests"
    ) as client:
        response = await client.post("/api/v1/usage/requests", json=query.model_dump(mode="json"))
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["pagination_unit"] == "request"
        assert body["items"][0]["user_agent"] == "OpenAI/Python"
        response = await client.post(
            "/api/v1/usage/requests/detail",
            json={
                "start": start.isoformat(),
                "end": end.isoformat(),
                "selection_ref": body["items"][0]["selection_ref"],
                "limit": 1,
            },
        )
        assert response.status_code == 200, response.text
        assert response.json()["request"]["attempt_count"] == 2
    legacy = await service.requests(actor, UsageRequestQuery(start=start, end=end))
    assert legacy.pagination_unit == "attempt"
    assert len(legacy.items) == 2
    summary = await service.query(
        actor,
        UsageQuery(
            start=start,
            end=end,
            group_by="client",
            filters={"traffic_origins": ["api"]},
        ),
    )
    assert len(summary.groups) == 1
    assert summary.groups[0].key == "key-client-123"
    assert summary.total.finalized_attempts == 2
    other = await service.requests("other@tests.local", query)
    assert other.items == []
    with pytest.raises(UsageQueryError, match="selection|Cursor|cursor|Invalid"):
        await service.request_detail(
            "other@tests.local",
            UsageRequestDetailQuery(
                start=start,
                end=end,
                selection_ref=item.selection_ref,
            ),
        )
    with pytest.raises(UsageAccessError):
        await service.requests(
            actor,
            query.model_copy(
                update={"scope": UsageScope(mode="project", project_id="ungranted-project")}
            ),
        )


@pytest.mark.asyncio
async def test_expiry_removes_request_headers_and_private_summary_dimensions(usage_storage):
    harness = usage_storage
    harness.clock.now -= timedelta(days=61)
    actor, service, start, end, _ = await seed(harness)
    async with harness.sessions() as session:
        header = await session.scalar(select(UsageRequestLogRow))
        scope = UsageMaintenanceScope(owner_key=header.owner_key, bucket_day=header.bucket_day)
    await harness.maintenance.request_archive(scope)
    await harness.pump()
    async with harness.sessions() as session:
        assert await session.scalar(select(func.count()).select_from(UsageRequestLogRow)) == 0
        for model in (UsageDailyTotalRow, UsageArchivedTotalRow):
            rows = (await session.scalars(select(model))).all()
            assert rows
            assert all(row.traffic_client_id is None and row.traffic_key_id is None for row in rows)
            assert all(row.dimensions.get("traffic_client_id") is None for row in rows)


async def test_media_quantities_and_provider_cost_survive_request_queries(usage_storage):
    await usage_storage.maintenance.publish_price(usage_storage.price())
    await usage_storage.pump()
    actor, service, start, end, _ = await seed(usage_storage, media=True)
    page = await service.requests(
        actor, UsageRequestQuery(start=start, end=end, pagination_unit="request")
    )
    request = page.items[0]
    assert request.operations == ["image_generate"]
    assert [(item.name, item.value, item.unit) for item in request.media_usage] == [
        ("output_image_count", 4, "images")
    ]
    assert request.usage_quality == "complete"
    assert request.tokens.input is None
    assert [(cost.known_amount, cost.basis) for cost in request.costs] == [
        ("0.250000000000", "provider_reported")
    ]
    detail = await service.request_detail(
        actor,
        UsageRequestDetailQuery(start=start, end=end, selection_ref=request.selection_ref),
    )
    assert [attempt.media_usage[0].value for attempt in detail.attempts] == [2, 2]
    assert all(attempt.costs[0].basis == "provider_reported" for attempt in detail.attempts)
    overview = await service.query(actor, UsageQuery(start=start, end=end))
    assert overview.total.costs[0].basis == "provider_reported"
