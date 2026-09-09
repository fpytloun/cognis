"""Tests for controller-side executor package availability."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

import cognis.core.executor_availability as availability
from cognis.api.app import create_app
from cognis.core.executor_policy import (
    ExecutorPolicy,
    is_executor_row_usable,
    is_executor_type_allowed,
)
from cognis.models.tool import ExecutorConfig
from cognis.providers import registry
from cognis.providers.executor.composite import CompositeExecutorProvider
from cognis.store.queries import create_executor, create_user


@pytest.fixture(autouse=True)
def _reset_executor_package_availability_cache():
    availability.is_executor_package_available.cache_clear()
    yield
    availability.is_executor_package_available.cache_clear()


def test_distribution_presence_is_the_only_availability_signal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(availability, "distribution", lambda name: object())
    availability.is_executor_package_available.cache_clear()
    assert availability.is_executor_package_available() is True
    assert availability.is_executor_type_available("in_process") is True

    def package_missing(name: str) -> object:
        raise availability.PackageNotFoundError(name)

    monkeypatch.setattr(availability, "distribution", package_missing)
    availability.is_executor_package_available.cache_clear()
    assert availability.is_executor_package_available() is False
    assert availability.is_executor_type_available("in_process") is False
    assert availability.is_executor_type_available("websocket") is True


def test_policy_marks_local_types_unusable_when_package_is_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        availability,
        "is_executor_package_available",
        lambda: False,
    )
    policy = ExecutorPolicy()
    assert is_executor_type_allowed("in_process", policy) is False
    assert is_executor_type_allowed("subprocess", policy) is False
    assert is_executor_type_allowed("websocket", policy) is True

    row = MagicMock(status="active", executor_type="in_process", owner_email="owner@example.com")
    assert is_executor_row_usable(row, policy, owner_email="owner@example.com") is False


@pytest.mark.asyncio
async def test_composite_does_not_route_or_construct_local_provider_when_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        availability,
        "is_executor_package_available",
        lambda: False,
    )
    local = MagicMock()
    local.list_active = AsyncMock(return_value=[])
    subprocess = MagicMock()
    subprocess.list_active = AsyncMock(return_value=[])
    remote = MagicMock()
    remote.list_active = AsyncMock(return_value=[])
    composite = CompositeExecutorProvider(local, remote, subprocess)

    with pytest.raises(ValueError, match="cognis-executor package is not installed"):
        await composite.spawn(
            ExecutorConfig(
                executor_id="persisted-local-executor",
                metadata={"executor_type": "in_process"},
            )
        )
    await composite.list_active()
    local.list_active.assert_not_awaited()
    subprocess.list_active.assert_not_awaited()
    remote.list_active.assert_awaited_once()


def test_api_rejects_new_local_executor_but_keeps_persisted_row_visible(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.delitem(sys.modules, "cognis.providers.executor.in_process", raising=False)
    monkeypatch.delitem(sys.modules, "cognis.providers.executor.subprocess", raising=False)
    monkeypatch.setattr(availability, "is_executor_package_available", lambda: False)
    monkeypatch.setattr(registry, "is_executor_package_available", lambda: False)
    monkeypatch.setenv("COGNIS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("COGNIS_HOST", "127.0.0.1")

    with TestClient(create_app()) as client:
        assert "cognis.providers.executor.in_process" not in sys.modules
        assert "cognis.providers.executor.subprocess" not in sys.modules

        async def seed() -> None:
            async with client.app.state.session_factory() as session:
                await create_user(
                    session,
                    email="admin@example.com",
                    name="Admin",
                    password_hash=client.app.state.password_hasher.hash("password123"),
                    role="admin",
                )
                await create_executor(
                    session,
                    executor_id="persisted-local-executor",
                    name="Persisted local executor",
                    executor_type="in_process",
                    owner_email="admin@example.com",
                )
                await session.commit()

        asyncio.run(seed())
        token = client.app.state.auth_provider.sign_access_token(
            "admin@example.com",
            "Admin",
            "admin",
        )
        headers = {"Authorization": f"Bearer {token}"}

        listed = client.get("/api/v1/executors", headers=headers)
        executor_status = client.get("/api/v1/executor/status", headers=headers)
        created = client.post(
            "/api/v1/executors",
            headers=headers,
            json={"name": "New local executor", "executor_type": "in_process"},
        )

    assert listed.status_code == 200
    assert all(row["executor_id"] != "default_inprocess" for row in listed.json())
    persisted = next(
        row for row in listed.json() if row["executor_id"] == "persisted-local-executor"
    )
    assert persisted["available"] is False
    assert "cognis-executor package is not installed" in persisted["unavailable_reason"]
    assert persisted["executor_type"] == "in_process"
    assert persisted["status"] == "active"
    assert executor_status.status_code == 200
    assert executor_status.json()["available_executor_types"] == ["websocket"]
    assert created.status_code == 400
    assert "cognis-executor package is not installed" in created.json()["error"]["message"]
