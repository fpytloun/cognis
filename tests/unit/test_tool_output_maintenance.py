"""Tests for periodic tool-output maintenance."""

from __future__ import annotations

import asyncio

import pytest

from cognis.core.tool_output_maintenance import ToolOutputMaintenanceService


class _FakeToolOutputStore:
    def __init__(
        self,
        *,
        cleanup_error: Exception | None = None,
        size_cap_error: Exception | None = None,
    ) -> None:
        self.cleanup_error = cleanup_error
        self.size_cap_error = size_cap_error
        self.cleanup_calls = 0
        self.size_cap_calls = 0
        self.pass_completed = asyncio.Event()

    async def cleanup_expired(self) -> int:
        self.cleanup_calls += 1
        if self.cleanup_error is not None:
            raise self.cleanup_error
        return 3

    async def enforce_size_cap(self) -> int:
        self.size_cap_calls += 1
        try:
            if self.size_cap_error is not None:
                raise self.size_cap_error
            return 2
        finally:
            self.pass_completed.set()


@pytest.mark.asyncio
async def test_run_once_enforces_ttl_and_size_cap() -> None:
    store = _FakeToolOutputStore()
    service = ToolOutputMaintenanceService(store)

    result = await service.run_once()

    assert result.expired_deleted == 3
    assert result.size_cap_deleted == 2
    assert result.cleanup_failed is False
    assert result.size_cap_failed is False
    assert store.cleanup_calls == 1
    assert store.size_cap_calls == 1


@pytest.mark.asyncio
async def test_run_once_isolates_cleanup_failure() -> None:
    store = _FakeToolOutputStore(cleanup_error=RuntimeError("cleanup failed"))
    service = ToolOutputMaintenanceService(store)

    result = await service.run_once()

    assert result.expired_deleted == 0
    assert result.size_cap_deleted == 2
    assert result.cleanup_failed is True
    assert result.size_cap_failed is False
    assert store.size_cap_calls == 1


@pytest.mark.asyncio
async def test_start_runs_immediately_and_stop_is_idempotent() -> None:
    store = _FakeToolOutputStore()
    service = ToolOutputMaintenanceService(store, interval_seconds=60)

    await service.start()
    await asyncio.wait_for(store.pass_completed.wait(), timeout=1.0)
    await service.start()

    assert store.cleanup_calls == 1
    assert store.size_cap_calls == 1

    await service.stop()
    await service.stop()
    assert service._task is None


@pytest.mark.asyncio
async def test_periodic_loop_continues_after_size_cap_failure() -> None:
    store = _FakeToolOutputStore(size_cap_error=RuntimeError("size cap failed"))
    service = ToolOutputMaintenanceService(store, interval_seconds=0.01)
    await service.start()
    await asyncio.wait_for(store.pass_completed.wait(), timeout=1.0)
    store.pass_completed.clear()
    store.size_cap_error = None

    await asyncio.wait_for(store.pass_completed.wait(), timeout=1.0)
    await service.stop()

    assert store.cleanup_calls >= 2
    assert store.size_cap_calls >= 2
