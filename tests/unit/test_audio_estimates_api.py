"""Estimate transport is authenticated, read-only, and never a generation request."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import FastAPI, Request

from cognis.api.authentication import AuthenticatedUser
from cognis.api.routes.audio import router
from cognis.core.audio_planning import podcast_length_estimates


@pytest.mark.parametrize("identity", [None, "user", "viewer", "denied"])
async def test_estimate_authentication_and_read_only_transport(identity):
    plans = podcast_length_estimates("Source facts. " * 200)
    estimates = AsyncMock(
        side_effect=PermissionError("Document not found") if identity == "denied" else None,
        return_value=plans,
    )
    app = FastAPI()
    app.state.audio_jobs = SimpleNamespace(estimates=estimates)
    app.include_router(router)

    @app.middleware("http")
    async def authentication(request: Request, call_next):
        if identity:
            request.state.user = AuthenticatedUser(
                email="reader@test.local", role="viewer" if identity == "viewer" else "user"
            )
        return await call_next(request)

    ref = "dlv_11111111111111111111111111111111"
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.get("/api/v1/audio/jobs/estimates", params={"content_ref": ref})
        assert response.status_code == (
            401 if identity is None else 404 if identity == "denied" else 200
        )
        if identity is None:
            estimates.assert_not_awaited()
        else:
            estimates.assert_awaited_once_with("reader@test.local", ref)
        if response.status_code == 200:
            assert response.json() == [plan.model_dump() for plan in plans]
        estimates.reset_mock()
        assert (await client.post("/api/v1/audio/jobs/estimates")).status_code == 405
        estimates.assert_not_awaited()
