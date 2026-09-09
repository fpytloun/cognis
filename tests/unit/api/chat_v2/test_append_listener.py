from __future__ import annotations

import asyncio
from time import perf_counter
from types import SimpleNamespace

import pytest

from cognis.api.chat_v2.append_listener import EventAppendListenerFastPath
from cognis.api.chat_v2.cached_event_store import AppendInvalidation
from cognis.models.session import SessionEvent
from cognis.providers.guardrails.events import EventAppendNotification, EventStoreAuthority

AUTHORITY = EventStoreAuthority(
    user_email="user@example.com",
    agent_id="agent-1",
    agent_owner_email="owner@example.com",
)


def _notification() -> EventAppendNotification:
    return EventAppendNotification(
        authority=AUTHORITY,
        session_id="session-1",
        first_seq=1,
        last_seq=1,
        event_count=1,
        events=(SessionEvent(type="user_message", data={"content": "hello"}),),
    )


@pytest.mark.asyncio
async def test_listener_fast_path_orders_local_work_before_background_work() -> None:
    calls: list[str] = []
    work = AppendInvalidation("token", "authority", 1, True, 1)
    db_started = asyncio.Event()
    db_release = asyncio.Event()
    db_tasks: list[asyncio.Task[None]] = []

    class EventStore:
        def invalidate_append_local(self, _notification):
            calls.append("invalidate")
            return work

    class Dispatcher:
        def enqueue(self, admitted):
            assert admitted is work
            calls.append("dispatcher")
            return True

    class Materializer:
        def enqueue_append(self, _notification):
            calls.append("work")
            db_tasks.append(asyncio.create_task(self._blocked_db()))
            return True

        async def _blocked_db(self) -> None:
            db_started.set()
            await db_release.wait()

    listener = EventAppendListenerFastPath(
        event_store=EventStore(),
        invalidation_dispatcher=Dispatcher(),
        work_materializer=Materializer(),
    )

    started = perf_counter()
    await listener(_notification())

    assert perf_counter() - started < 0.1
    assert calls == ["invalidate", "dispatcher", "work"]
    await db_started.wait()
    db_release.set()
    await asyncio.gather(*db_tasks)


@pytest.mark.asyncio
async def test_work_admission_failure_cannot_suppress_listener_invalidation() -> None:
    state = SimpleNamespace(invalidated=False, dispatched=False)
    work = AppendInvalidation("token", "authority", 1, True, 1)

    class EventStore:
        def invalidate_append_local(self, _notification):
            state.invalidated = True
            return work

    class Dispatcher:
        def enqueue(self, _work):
            state.dispatched = True
            return True

    class Materializer:
        def enqueue_append(self, _notification):
            raise RuntimeError("Work database admission failed")

    listener = EventAppendListenerFastPath(
        event_store=EventStore(),
        invalidation_dispatcher=Dispatcher(),
        work_materializer=Materializer(),
    )

    await listener(_notification())

    assert state.invalidated is True
    assert state.dispatched is True


@pytest.mark.asyncio
async def test_listener_records_shutdown_rejection(
    caplog: pytest.LogCaptureFixture,
) -> None:
    work = AppendInvalidation("token", "authority", 1, True, 1)

    class EventStore:
        def invalidate_append_local(self, _notification):
            return work

    class Dispatcher:
        def enqueue(self, _work):
            return True

    class Materializer:
        def enqueue_append(self, _notification):
            return False

    listener = EventAppendListenerFastPath(
        event_store=EventStore(),
        invalidation_dispatcher=Dispatcher(),
        work_materializer=Materializer(),
    )

    with caplog.at_level("WARNING"):
        await listener(_notification())

    assert "Work append rejected during shutdown" in caplog.text
