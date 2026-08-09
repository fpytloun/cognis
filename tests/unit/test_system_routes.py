from __future__ import annotations

import asyncio
import threading
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from cognis import __version__
from cognis.api.app import _drain_turn_scheduler, create_app
from cognis.api.routes.system import _redis_diagnostics, livez, pwa_reset, readyz
from cognis.core.controller_runtime import ControllerLifecycleState, ControllerRuntime


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


def test_redis_diagnostics_are_safe_when_unwired_and_report_shared_cache() -> None:
    unwired = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace()))
    assert _redis_diagnostics(unwired) == {
        "configured": False,
        "available": False,
        "session_cache": False,
        "event_cache": False,
        "runtime_relay": False,
    }

    service = SimpleNamespace(configured=True, available=False)
    wired = SimpleNamespace(
        app=SimpleNamespace(
            state=SimpleNamespace(
                redis_service=service,
                session_cache=SimpleNamespace(_redis_service=service),
                cached_event_store=SimpleNamespace(_redis=service),
                chat_v2_runtime_relay=SimpleNamespace(redis_service=service),
            )
        )
    )
    assert _redis_diagnostics(wired) == {
        "configured": True,
        "available": False,
        "session_cache": True,
        "event_cache": True,
        "runtime_relay": True,
    }


@pytest.mark.asyncio
async def test_readyz_recovers_after_transient_database_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = ControllerRuntime(
        "controller-a",
        state=ControllerLifecycleState.READY,
        schema_compatible=True,
    )
    request = SimpleNamespace(
        app=SimpleNamespace(
            state=SimpleNamespace(
                controller_runtime=runtime,
                engine=object(),
                config=SimpleNamespace(schema_mode="validate"),
            )
        )
    )
    check = AsyncMock(side_effect=[False, True])
    monkeypatch.setattr("cognis.api.routes.system.check_connection", check)

    first = await readyz(request)
    second = await readyz(request)

    assert first.status_code == 503
    assert second.status_code == 200
    assert runtime.schema_compatible is True
    assert check.await_count == 2


@pytest.mark.asyncio
async def test_shutdown_drain_interrupts_and_settles_after_timeout() -> None:
    scheduler = SimpleNamespace(
        drain_active_turns=AsyncMock(return_value={"active": 1, "completed": 0, "timed_out": 1}),
        interrupt_active_turns_and_wait=AsyncMock(
            return_value={"requested": 1, "settled": 1, "abandoned": 0}
        ),
    )

    result = await _drain_turn_scheduler(
        scheduler,
        drain_timeout_seconds=5,
        cancel_timeout_seconds=2,
    )

    assert result["cancellation_settled"] == 1
    assert result["cancellation_abandoned"] == 0
    scheduler.drain_active_turns.assert_awaited_once_with(timeout_seconds=5)
    scheduler.interrupt_active_turns_and_wait.assert_awaited_once_with(
        reason="controller_restart", timeout_seconds=2
    )


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


def test_client_discovery_is_public_typed_and_uncached(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("COGNIS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("COGNIS_HOST", "127.0.0.1")
    monkeypatch.setenv("COGNIS_BUILD_ID", "private-image-tag")
    monkeypatch.setenv("COGNIS_BUILD_SHA", "private-deployment-sha")

    with TestClient(create_app()) as client:
        first_response = client.get("/.well-known/cognis-client.json")
        second_response = client.get("/.well-known/cognis-client.json")
        fingerprint = client.app.state.jwt_public_key_fingerprint  # type: ignore[union-attr]

    expected = {
        "schema_version": 1,
        "product": {"id": "cognis", "display_name": "Cognis"},
        "protocol": {"id": "cognis-client", "version": 1},
        "server": {
            "id": f"cognis:{fingerprint}",
            "version": __version__,
            "build_id": __version__,
        },
        "paths": {
            "api_v1": "/api/v1",
            "login": "/api/auth/login",
            "refresh": "/api/auth/refresh",
            "logout": "/api/auth/logout",
            "current_user": "/api/auth/me",
            "chat_v2": "/api/v1/chat/v2",
            "realtime": "/api/ws",
            "jwks": "/.well-known/jwks.json",
        },
        "capabilities": {
            "authentication": 1,
            "chat": 2,
            "realtime": 1,
        },
    }
    assert first_response.status_code == 200
    assert first_response.headers["cache-control"] == "no-store"
    assert first_response.json() == expected
    assert second_response.json() == expected
    assert "private-image-tag" not in first_response.text
    assert "private-deployment-sha" not in first_response.text


def test_client_discovery_openapi_uses_typed_response() -> None:
    operation = create_app().openapi()["paths"]["/.well-known/cognis-client.json"]["get"]

    schema = operation["responses"]["200"]["content"]["application/json"]["schema"]
    assert schema == {"$ref": "#/components/schemas/ClientDiscoveryResponse"}


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
