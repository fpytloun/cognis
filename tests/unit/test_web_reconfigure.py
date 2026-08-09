from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

import cognis.api.web_reconfigure as web_reconfigure


class _Session:
    def __init__(self, events: list[str]) -> None:
        self.events = events

    async def __aenter__(self) -> _Session:
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None

    async def commit(self) -> None:
        self.events.append("commit")


class _SessionFactory:
    def __init__(self, events: list[str]) -> None:
        self.events = events

    def __call__(self) -> _Session:
        return _Session(self.events)


@pytest.mark.asyncio
async def test_web_reconfigure_bumps_all_active_websocket_executors_before_scheduling(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    executors = [
        SimpleNamespace(executor_id="connected"),
        SimpleNamespace(executor_id="disconnected"),
    ]
    connection = object()
    websocket = SimpleNamespace(
        get_connection=lambda executor_id: connection if executor_id == "connected" else None
    )
    app = SimpleNamespace(
        state=SimpleNamespace(
            session_factory=_SessionFactory(events),
            providers=SimpleNamespace(executor=SimpleNamespace(websocket=websocket)),
        )
    )

    async def _list(_session: object, *, for_update: bool) -> list[SimpleNamespace]:
        assert for_update is True
        return executors

    async def _bump(_session: object, executor_id: str, *, runtime_state: str) -> bool:
        events.append(f"bump:{executor_id}:{runtime_state}")
        return True

    def _schedule(_app: object, executor_id: str) -> None:
        events.append(f"schedule:{executor_id}")

    monkeypatch.setattr(web_reconfigure, "list_active_websocket_executors", _list)
    monkeypatch.setattr(web_reconfigure, "bump_executor_reconfigure_generation", _bump)
    monkeypatch.setattr(web_reconfigure, "schedule_executor_reconfigure", _schedule)

    scheduled = await web_reconfigure.schedule_web_executor_reconfigure_for_app(
        app,
        reason="test",
    )

    assert scheduled == ["connected", "disconnected"]
    assert events == [
        "bump:connected:reconfiguring",
        "bump:disconnected:stale",
        "commit",
        "schedule:connected",
        "schedule:disconnected",
    ]


@pytest.mark.asyncio
async def test_web_reconfigure_does_not_schedule_executor_when_generation_bump_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    executor = SimpleNamespace(executor_id="executor-1")
    websocket = SimpleNamespace(get_connection=lambda _executor_id: object())
    app = SimpleNamespace(
        state=SimpleNamespace(
            session_factory=_SessionFactory(events),
            providers=SimpleNamespace(executor=SimpleNamespace(websocket=websocket)),
        )
    )

    async def _list(_session: object, *, for_update: bool) -> list[SimpleNamespace]:
        assert for_update is True
        return [executor]

    async def _bump(_session: object, _executor_id: str, *, runtime_state: str) -> bool:
        assert runtime_state == "reconfiguring"
        return False

    def _schedule(_app: object, executor_id: str) -> None:
        events.append(f"schedule:{executor_id}")

    monkeypatch.setattr(web_reconfigure, "list_active_websocket_executors", _list)
    monkeypatch.setattr(web_reconfigure, "bump_executor_reconfigure_generation", _bump)
    monkeypatch.setattr(web_reconfigure, "schedule_executor_reconfigure", _schedule)

    scheduled = await web_reconfigure.schedule_web_executor_reconfigure_for_app(
        app,
        reason="test",
    )

    assert scheduled == []
    assert events == ["commit"]


@pytest.mark.asyncio
async def test_web_reconfigure_continues_after_one_scheduling_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    executors = [
        SimpleNamespace(executor_id="broken"),
        SimpleNamespace(executor_id="healthy"),
    ]
    websocket = SimpleNamespace(get_connection=lambda _executor_id: object())
    app = SimpleNamespace(
        state=SimpleNamespace(
            session_factory=_SessionFactory(events),
            providers=SimpleNamespace(executor=SimpleNamespace(websocket=websocket)),
        )
    )

    async def _list(_session: object, *, for_update: bool) -> list[SimpleNamespace]:
        assert for_update is True
        return executors

    async def _bump(_session: object, _executor_id: str, *, runtime_state: str) -> bool:
        assert runtime_state == "reconfiguring"
        return True

    def _schedule(_app: object, executor_id: str) -> None:
        events.append(f"schedule:{executor_id}")
        if executor_id == "broken":
            raise RuntimeError("cannot schedule")

    monkeypatch.setattr(web_reconfigure, "list_active_websocket_executors", _list)
    monkeypatch.setattr(web_reconfigure, "bump_executor_reconfigure_generation", _bump)
    monkeypatch.setattr(web_reconfigure, "schedule_executor_reconfigure", _schedule)

    scheduled = await web_reconfigure.schedule_web_executor_reconfigure_for_app(
        app,
        reason="test",
    )

    assert scheduled == ["broken", "healthy"]
    assert events == ["commit", "schedule:broken", "schedule:healthy"]


@pytest.mark.asyncio
async def test_web_reconfigure_finalizer_completes_after_caller_cancellation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    started = asyncio.Event()
    release = asyncio.Event()
    completed = False

    async def _schedule(_app: object, *, reason: str) -> list[str]:
        nonlocal completed
        assert reason == "test"
        started.set()
        await release.wait()
        completed = True
        return ["executor-1"]

    monkeypatch.setattr(
        web_reconfigure,
        "schedule_web_executor_reconfigure_for_app",
        _schedule,
    )

    task = asyncio.create_task(
        web_reconfigure.finalize_web_executor_reconfigure_for_app(object(), reason="test")
    )
    await started.wait()
    task.cancel()
    release.set()

    with pytest.raises(asyncio.CancelledError):
        await task
    assert completed is True
