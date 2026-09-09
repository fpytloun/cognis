from __future__ import annotations

import asyncio
import inspect
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from cognis.api.app import (
    _cancel_optional_snapshot_warming,
    _create_work_projection_runtime,
    _schedule_optional_snapshot_warming,
    _snapshot_warm_conversation_id,
    _start_optional_snapshot_warming,
    create_app,
)
from cognis.api.chat_v2 import snapshot_activity
from cognis.core.events import Event, EventType


def test_work_invalidation_does_not_target_full_snapshot_warming() -> None:
    event = Event(
        type=EventType.CLUSTER_SCOPE_INVALIDATED,
        data={
            "kind": "work_invalidated",
            "scope": {
                "conversation_id": "conversation-a",
                "work_scope_key": "conversation:conversation-a",
                "work_materialized": True,
            },
        },
    )

    assert _snapshot_warm_conversation_id(event) is None


def test_app_uses_snapshot_independent_work_projection_runtime_factory() -> None:
    parameters = inspect.signature(_create_work_projection_runtime).parameters

    assert "snapshot_warmer" not in parameters
    assert "snapshot_warmer_enqueue" not in parameters
    assert "cluster_signals" in parameters
    assert "_create_work_projection_runtime(" in inspect.getsource(create_app)


@pytest.mark.asyncio
async def test_optional_snapshot_warming_cannot_block_startup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def failed_conversations(_session_factory):
        if False:
            yield "unused"
        raise RuntimeError("database pool unavailable")

    monkeypatch.setattr(
        snapshot_activity,
        "iter_active_snapshot_conversation_ids",
        failed_conversations,
    )
    reconciler = SimpleNamespace(start=AsyncMock(side_effect=RuntimeError("redis unavailable")))
    warmer = SimpleNamespace(enqueue=AsyncMock())

    await _start_optional_snapshot_warming(object(), warmer, reconciler)

    warmer.enqueue.assert_not_called()
    reconciler.start.assert_awaited_once()


@pytest.mark.asyncio
async def test_blocking_snapshot_iterator_does_not_delay_startup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entered = asyncio.Event()
    blocked = asyncio.Event()

    async def blocking_conversations(_session_factory):
        entered.set()
        await blocked.wait()
        if False:
            yield "unused"

    monkeypatch.setattr(
        snapshot_activity,
        "iter_active_snapshot_conversation_ids",
        blocking_conversations,
    )
    reconciler = SimpleNamespace(start=AsyncMock())
    warmer = SimpleNamespace(enqueue=AsyncMock())
    startup_proceeded = asyncio.Event()

    async def startup_path() -> asyncio.Task[None]:
        task = _schedule_optional_snapshot_warming(object(), warmer, reconciler)
        startup_proceeded.set()
        return task

    task = await asyncio.wait_for(startup_path(), timeout=0.1)
    await asyncio.wait_for(entered.wait(), timeout=0.1)

    assert startup_proceeded.is_set()
    assert not task.done()
    reconciler.start.assert_not_awaited()

    await _cancel_optional_snapshot_warming(task)
    assert task.cancelled()
