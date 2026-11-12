"""Usage route failures expose safe, request-linked diagnostics."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.exc import OperationalError

from cognis.api.common import require_current_user
from cognis.api.routes.usage import _response, create_usage_router
from cognis.core.usage.query_service_errors import UsageQueryError


def client_for(error: Exception) -> TestClient:
    service = SimpleNamespace(
        query=AsyncMock(side_effect=error),
        requests=AsyncMock(side_effect=error),
        request_detail=AsyncMock(side_effect=error),
    )
    app = FastAPI()
    app.dependency_overrides[require_current_user] = lambda: SimpleNamespace(
        email="owner@test.local", role="user"
    )
    app.include_router(create_usage_router(service), prefix="/api")
    return TestClient(app)


def test_http_failure_has_operation_status_target_and_ids_without_content(
    caplog: pytest.LogCaptureFixture,
) -> None:
    request = httpx.Request("GET", "https://usage.test.local/query?token=private")
    response = httpx.Response(
        503,
        request=request,
        headers={"x-request-id": "upstream-request-17"},
        json={"message": "secret prompt and Bearer private"},
    )
    upstream = httpx.HTTPStatusError("secret payload", request=request, response=response)
    error = UsageQueryError("usage_provider_unavailable", "secret query", status=503)
    error.__cause__ = upstream
    with caplog.at_level("WARNING"):
        result = client_for(error).post(
            "/api/usage/query",
            json={"start": "2026-10-01T00:00:00Z", "end": "2026-10-02T00:00:00Z"},
        )
    assert result.status_code == 503
    detail = result.json()["detail"]
    assert detail["details"]["operation"] == "Load usage"
    assert len(detail["details"]["correlation_id"]) == 32
    data = next(
        record.extra_data for record in caplog.records if record.msg == "Usage operation failed"
    )
    assert data["operation"] == "Load usage"
    assert data["target"] == "usage.test.local"
    assert data["status"] == data["upstream_status"] == 503
    assert data["upstream_error"] == "Service Unavailable"
    assert data["upstream_request_id"] == "upstream-request-17"
    assert data["correlation_id"] == detail["details"]["correlation_id"]
    assert "secret" not in str(data) + str(detail)
    assert "private" not in str(data) + str(detail)


def test_database_failure_does_not_log_sql_or_parameters(caplog: pytest.LogCaptureFixture) -> None:
    error = OperationalError("SELECT secret", {"token": "private"}, RuntimeError("secret"))
    with caplog.at_level("WARNING"):
        result = client_for(error).post(
            "/api/usage/query",
            json={"start": "2026-10-01T00:00:00Z", "end": "2026-10-02T00:00:00Z"},
        )
    assert result.status_code == 500
    assert "database" in result.json()["detail"]["message"]
    data = next(
        record.extra_data for record in caplog.records if record.msg == "Usage operation failed"
    )
    assert data["target"] == "usage-store"
    assert "secret" not in str(data)
    assert "private" not in str(data)


@pytest.mark.asyncio
async def test_usage_response_preserves_cancellation() -> None:
    async def cancelled() -> None:
        raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await _response(cancelled(), SimpleNamespace())


@pytest.mark.parametrize(
    ("path", "operation", "extra"),
    [
        ("/api/usage/requests", "Load request log", {}),
        (
            "/api/usage/requests/detail",
            "Load request details",
            {"selection_ref": "signed-fixture-request"},
        ),
    ],
)
def test_request_log_failures_keep_safe_diagnostics(
    path: str, operation: str, extra: dict[str, str], caplog: pytest.LogCaptureFixture
) -> None:
    error = UsageQueryError("resource_detail_expired", "PRIVATE SQL parameters", status=422)
    with caplog.at_level("WARNING"):
        response = client_for(error).post(
            path, json={"start": "2026-11-01T00:00:00Z", "end": "2026-11-02T00:00:00Z", **extra}
        )
    assert response.status_code == 422
    detail = response.json()["detail"]
    assert detail["details"]["operation"] == operation
    assert len(detail["details"]["correlation_id"]) == 32
    assert "expired" in detail["message"]
    assert "PRIVATE" not in str(detail)
    record = next(
        record.extra_data for record in caplog.records if record.msg == "Usage operation failed"
    )
    assert record["correlation_id"] == detail["details"]["correlation_id"]
    assert "PRIVATE" not in str(record)
