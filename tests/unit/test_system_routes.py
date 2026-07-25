from __future__ import annotations

import asyncio
import threading
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from cognis.api.app import create_app
from cognis.api.routes.system import livez, pwa_reset, readyz


@pytest.mark.asyncio
async def test_pwa_reset_page_is_uncached_browser_recovery() -> None:
    response = await pwa_reset()

    assert response.headers["cache-control"] == "no-store"
    assert response.headers["pragma"] == "no-cache"
    body = bytes(response.body).decode("utf-8")
    assert "navigator.serviceWorker.getRegistrations()" in body
    assert "registration.unregister()" in body
    assert "name.startsWith('cognis-')" in body
    assert "location.replace('/?pwa-reset='" in body


@pytest.mark.asyncio
async def test_livez_is_static_process_liveness() -> None:
    assert await livez() == {"status": "alive"}


@pytest.mark.asyncio
async def test_readyz_is_static_traffic_readiness() -> None:
    assert await readyz() == {"status": "ready"}


def test_pwa_reset_route_is_public_and_uncached(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("COGNIS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("COGNIS_HOST", "127.0.0.1")

    with TestClient(create_app()) as client:
        response = client.get("/api/v1/pwa-reset")

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["pragma"] == "no-cache"
    assert "text/html" in response.headers["content-type"]
    assert "navigator.serviceWorker.getRegistrations()" in response.text


def test_probe_routes_are_public_and_cheap(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("COGNIS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("COGNIS_HOST", "127.0.0.1")

    with TestClient(create_app()) as client:
        live_response = client.get("/api/livez")
        ready_response = client.get("/api/readyz")

    assert live_response.status_code == 200
    assert live_response.json() == {"status": "alive"}
    assert ready_response.status_code == 200
    assert ready_response.json() == {"status": "ready"}


def test_tool_output_maintenance_does_not_block_startup(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("COGNIS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("COGNIS_HOST", "127.0.0.1")
    started = threading.Event()
    release = threading.Event()

    async def _slow_cleanup(_: Any) -> int:
        started.set()
        await asyncio.to_thread(release.wait)
        return 0

    monkeypatch.setattr(
        "cognis.core.tool_output_store.ToolOutputStore.cleanup_expired",
        _slow_cleanup,
    )

    with TestClient(create_app()) as client:
        maintenance = client.app.state.tool_output_maintenance  # type: ignore[union-attr]
        try:
            assert started.wait(timeout=1.0)
            response = client.get("/api/livez")
            assert response.status_code == 200
        finally:
            release.set()

    assert maintenance._task is None
