from __future__ import annotations

import asyncio

import pytest

from cognis.api.chat_v2.activity_overview_computation import (
    ActivityOverviewComputationKey,
    ActivityOverviewComputationService,
    ActivityOverviewDisconnected,
)
from cognis.api.chat_v2.work_materializer import WORK_MATERIALIZER_VERSION


def _key(revision: int = 1) -> ActivityOverviewComputationKey:
    return ActivityOverviewComputationKey(
        owner_email="owner@example.com",
        scope_key="conversation:conversation-1",
        materializer_version=WORK_MATERIALIZER_VERSION,
        projection_version="chat-v2",
        graph_fingerprint="graph",
        graph_revision=1,
        work_revision=revision,
        required_watermarks=(("stream-1", revision),),
        detail="lightweight",
    )


@pytest.mark.asyncio
async def test_identical_waiters_share_one_copy_safe_computation() -> None:
    service = ActivityOverviewComputationService()
    release = asyncio.Event()
    calls = 0

    async def load() -> dict[str, list[int]]:
        nonlocal calls
        calls += 1
        await release.wait()
        return {"items": [1]}

    first = asyncio.create_task(service.get_or_compute(_key(), load))
    second = asyncio.create_task(service.get_or_compute(_key(), load))
    await asyncio.sleep(0)
    release.set()
    left, right = await asyncio.gather(first, second)
    left["items"].append(2)  # type: ignore[index]

    assert calls == 1
    assert right == {"items": [1]}
    assert await service.get_or_compute(_key(), load) == {"items": [1]}
    await service.stop()


@pytest.mark.asyncio
async def test_last_waiter_cancels_computation_and_new_revision_isolated() -> None:
    service = ActivityOverviewComputationService()
    cancelled = asyncio.Event()

    async def blocked() -> object:
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    waiter = asyncio.create_task(service.get_or_compute(_key(), blocked))
    await asyncio.sleep(0)
    waiter.cancel()
    await asyncio.gather(waiter, return_exceptions=True)
    await asyncio.wait_for(cancelled.wait(), timeout=0.2)

    value = await service.get_or_compute(_key(2), lambda: asyncio.sleep(0, result={"revision": 2}))
    assert value == {"revision": 2}
    assert service.cache_size == 1
    await service.stop()


@pytest.mark.asyncio
async def test_cancel_only_waiter_then_immediate_same_key_retry_succeeds() -> None:
    service = ActivityOverviewComputationService()
    started = asyncio.Event()

    async def blocked() -> object:
        started.set()
        await asyncio.Event().wait()

    waiter = asyncio.create_task(service.get_or_compute(_key(), blocked))
    await started.wait()
    waiter.cancel()
    await asyncio.gather(waiter, return_exceptions=True)

    result = await service.get_or_compute(
        _key(),
        lambda: asyncio.sleep(0, result={"retry": "ok"}),
    )
    assert result == {"retry": "ok"}
    await service.stop()


@pytest.mark.asyncio
async def test_disconnected_waiter_leaves_shared_computation_for_other_waiter() -> None:
    service = ActivityOverviewComputationService()
    release = asyncio.Event()
    disconnected = False

    async def load() -> object:
        await release.wait()
        return {"ok": True}

    keeper = asyncio.create_task(service.get_or_compute(_key(), load))
    leaving = asyncio.create_task(
        service.get_or_compute(
            _key(),
            load,
            disconnected=lambda: asyncio.sleep(0, result=disconnected),
            poll_seconds=0.01,
        )
    )
    await asyncio.sleep(0)
    disconnected = True
    with pytest.raises(ActivityOverviewDisconnected):
        await leaving
    release.set()
    assert await keeper == {"ok": True}
    await service.stop()


@pytest.mark.asyncio
async def test_ttl_lru_and_shutdown_are_bounded() -> None:
    now = 0.0
    service = ActivityOverviewComputationService(
        max_entries=2,
        ttl_seconds=2,
        clock=lambda: now,
    )
    for revision in (1, 2, 3):
        await service.get_or_compute(
            _key(revision),
            lambda revision=revision: asyncio.sleep(0, result={"revision": revision}),
        )
    assert service.cache_size == 2
    now = 3
    calls = 0

    async def reload() -> object:
        nonlocal calls
        calls += 1
        return {"revision": 2}

    await service.get_or_compute(_key(2), reload)
    assert calls == 1
    await service.stop()
    assert service.flight_count == 0
