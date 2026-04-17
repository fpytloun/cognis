"""Tests for the internal event bus."""

from __future__ import annotations

import pytest

from cognis.core.events import Event, EventBus, EventType


@pytest.mark.asyncio
async def test_event_bus_delivers_to_matching_handler() -> None:
    bus = EventBus()
    received: list[Event] = []

    async def handler(event: Event) -> None:
        received.append(event)

    bus.subscribe(EventType.TASK_CREATED, handler)
    await bus.publish(Event(type=EventType.TASK_CREATED, data={"task_id": "t1"}))

    assert len(received) == 1
    assert received[0].data["task_id"] == "t1"


@pytest.mark.asyncio
async def test_event_bus_does_not_deliver_to_wrong_type() -> None:
    bus = EventBus()
    received: list[Event] = []

    async def handler(event: Event) -> None:
        received.append(event)

    bus.subscribe(EventType.TASK_CREATED, handler)
    await bus.publish(Event(type=EventType.TASK_COMPLETED, data={}))

    assert len(received) == 0


@pytest.mark.asyncio
async def test_event_bus_global_handler_receives_all() -> None:
    bus = EventBus()
    received: list[Event] = []

    async def handler(event: Event) -> None:
        received.append(event)

    bus.subscribe_all(handler)
    await bus.publish(Event(type=EventType.TASK_CREATED, data={}))
    await bus.publish(Event(type=EventType.STEP_COMPLETED, data={}))

    assert len(received) == 2


@pytest.mark.asyncio
async def test_event_bus_handler_error_does_not_propagate() -> None:
    bus = EventBus()
    good_received: list[Event] = []

    async def bad_handler(event: Event) -> None:
        raise RuntimeError("handler error")

    async def good_handler(event: Event) -> None:
        good_received.append(event)

    bus.subscribe(EventType.TASK_CREATED, bad_handler)
    bus.subscribe(EventType.TASK_CREATED, good_handler)

    # Should not raise
    await bus.publish(Event(type=EventType.TASK_CREATED, data={}))
    assert len(good_received) == 1


@pytest.mark.asyncio
async def test_event_bus_unsubscribe() -> None:
    bus = EventBus()
    received: list[Event] = []

    async def handler(event: Event) -> None:
        received.append(event)

    bus.subscribe(EventType.TASK_CREATED, handler)
    bus.unsubscribe(EventType.TASK_CREATED, handler)
    await bus.publish(Event(type=EventType.TASK_CREATED, data={}))

    assert len(received) == 0


def test_event_bus_handler_count() -> None:
    bus = EventBus()

    async def h1(event: Event) -> None:
        pass

    async def h2(event: Event) -> None:
        pass

    bus.subscribe(EventType.TASK_CREATED, h1)
    bus.subscribe(EventType.TASK_COMPLETED, h2)
    bus.subscribe_all(h1)

    assert bus.handler_count(EventType.TASK_CREATED) == 2  # h1 + global h1
    assert bus.handler_count() == 3  # h1 + h2 + global h1


@pytest.mark.asyncio
async def test_event_bus_auto_removes_repeatedly_failing_handler() -> None:
    bus = EventBus()
    good_received: list[Event] = []

    async def bad_handler(event: Event) -> None:
        raise RuntimeError(f"boom {event.type}")

    async def good_handler(event: Event) -> None:
        good_received.append(event)

    bus.subscribe(EventType.TASK_CREATED, bad_handler)
    bus.subscribe(EventType.TASK_CREATED, good_handler)

    for _ in range(6):
        await bus.publish(Event(type=EventType.TASK_CREATED, data={}))

    assert len(good_received) == 6
    assert bus.handler_count(EventType.TASK_CREATED) == 1
