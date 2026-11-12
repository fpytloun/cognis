from __future__ import annotations

import asyncio
import logging
import sys
import threading
from dataclasses import dataclass
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from cognis.core import resource_attribution as attribution
from cognis.core.session_cache import CachedEvent, CachedSessionState, SessionCache


def test_estimate_deduplicates_and_handles_cycles() -> None:
    shared = ["private payload"]
    root = [shared, shared]
    root.append(root)
    result = attribution.measure_owned_objects([root], budget_seconds=1)
    assert result["estimated_bytes"] == sum(
        sys.getsizeof(value) for value in (root, shared, shared[0])
    )
    assert result["visited_objects"] == 3
    assert result["truncated"] is False
    assert "private payload" not in str(result)


def test_estimate_dataclass_preserves_all_fields() -> None:
    @dataclass(slots=True)
    class Payload:
        first: str
        second: list[int]

    value = Payload("secret", [991])
    result = attribution.measure_owned_objects([value], budget_seconds=1)
    assert result["visited_objects"] == 4
    assert result["shallow_only_objects"] == 0


def test_estimate_limits_nodes_depth_and_time_without_invoking_custom_hooks() -> None:
    class Opaque:
        def __sizeof__(self) -> int:
            raise AssertionError("must not run arbitrary hooks")

    result = attribution.measure_owned_objects([Opaque()], budget_seconds=1)
    assert result["shallow_only_objects"] == 1
    result = attribution.measure_owned_objects([list(range(10000))], max_nodes=8, budget_seconds=1)
    assert result["visited_objects"] == 8
    assert result["truncated"] is True
    assert attribution.measure_owned_objects([[]], budget_seconds=0)["truncated"] is True
    root: list[object] = []
    for _ in range(100):
        root = [root]
    assert attribution.measure_owned_objects([root], budget_seconds=1)["truncated"] is True


def test_session_summary_has_no_identity_or_content_and_does_not_touch_entry() -> None:
    cache = SessionCache(Mock())
    entry = CachedSessionState(session_id="private-session", intaris_session_id="private-intaris")
    entry.events.append(CachedEvent(seq=1, type="user", data={"content": "private-prompt"}))
    cache._entries[entry.session_id] = entry
    touched = entry.touched_at
    summary = cache.resource_memory_summary()
    assert summary["sessions"] == 1
    assert summary["groups"]["events"]["estimated_bytes"] > 0
    assert "private" not in str(summary)
    assert entry.touched_at == touched
    assert len(entry.events) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("flag,pod", [("", ""), ("false", ""), ("true", "different")])
async def test_disabled_has_no_tasks_threads_or_measurement(monkeypatch, flag, pod) -> None:
    monkeypatch.setenv("COGNIS_RESOURCE_PROFILING", flag)
    monkeypatch.setenv("COGNIS_RESOURCE_PROFILE_POD", pod)
    monkeypatch.setattr("socket.gethostname", lambda: "selected")
    sampler = attribution.ResourceAttribution()
    summary = Mock(side_effect=AssertionError("disabled"))
    sampler.start(summary)
    assert sampler.task is None
    assert sampler._worker is None
    await sampler.stop()
    summary.assert_not_called()


@pytest.mark.asyncio
async def test_stop_during_warmup_and_no_restart(monkeypatch) -> None:
    monkeypatch.setenv("COGNIS_RESOURCE_PROFILING", "1")
    monkeypatch.delenv("COGNIS_RESOURCE_PROFILE_POD", raising=False)
    sampler = attribution.ResourceAttribution()
    summary = Mock()
    sampler.start(summary)
    await asyncio.sleep(0)
    await sampler.stop()
    sampler.start(summary)
    assert sampler.task is not None and sampler.task.done()
    summary.assert_not_called()


@pytest.mark.asyncio
async def test_capture_expires_and_shutdown_joins_worker(monkeypatch, caplog) -> None:
    monkeypatch.setenv("COGNIS_RESOURCE_PROFILING", "true")
    monkeypatch.delenv("COGNIS_RESOURCE_PROFILE_POD", raising=False)
    monkeypatch.setenv("COGNIS_RESOURCE_PROFILE_DELAY_SECONDS", "0")
    monkeypatch.setenv("COGNIS_RESOURCE_PROFILE_DURATION_SECONDS", "1")
    sampler = attribution.ResourceAttribution()
    summary = Mock(return_value={"sessions": 0})
    with caplog.at_level(logging.INFO, logger=attribution.__name__):
        sampler.start(summary)
        assert sampler.task is not None
        await asyncio.wait_for(asyncio.shield(sampler.task), timeout=5)
        await sampler.stop()
    assert summary.call_count == 2
    assert sampler._worker is None
    assert not any(t.name == "resource-attribution-sampler" for t in threading.enumerate())
    assert "resource_cpu" in caplog.text
    assert "resource_memory" in caplog.text


def test_thread_stat_parsing_and_unavailable_proc(monkeypatch) -> None:
    # comm can include spaces and closing parentheses.
    fields = ["0"] * 20
    fields[11], fields[12], fields[19] = "11", "17", "99"
    monkeypatch.setattr(
        attribution.Path, "read_text", lambda _: "1 (worker ) name) " + " ".join(fields)
    )
    assert attribution._thread_ticks(1) == (28, 99)
    monkeypatch.setattr(attribution.Path, "read_text", Mock(side_effect=OSError))
    assert attribution._thread_ticks(1) is None


def test_invalid_and_excessive_settings_are_bounded(monkeypatch) -> None:
    monkeypatch.setenv("COGNIS_RESOURCE_PROFILE_DELAY_SECONDS", "bad")
    monkeypatch.setenv("COGNIS_RESOURCE_PROFILE_DURATION_SECONDS", "99999")
    monkeypatch.setenv("COGNIS_RESOURCE_PROFILE_SAMPLE_MS", "0")
    sampler = attribution.ResourceAttribution()
    assert (sampler.delay, sampler.duration, sampler.interval_ms) == (300, 600, 50)


def test_cpu_delta_is_endpoint_attribution_and_tid_reuse_is_not_counted(monkeypatch) -> None:
    sampler = attribution.ResourceAttribution()
    thread = SimpleNamespace(ident=-1, native_id=123)
    frame = SimpleNamespace(
        f_code=SimpleNamespace(co_filename="module.py", co_name="work"),
        f_lineno=42,
        f_back=None,
    )
    monkeypatch.setattr(attribution.threading, "enumerate", lambda: [thread])
    monkeypatch.setattr(attribution.sys, "_current_frames", lambda: {-1: frame})
    monkeypatch.setattr(attribution.os, "sysconf", lambda _: 100)
    ticks = iter([(10, 1), (20, 1), (999, 2)])
    monkeypatch.setattr(attribution, "_thread_ticks", lambda _: next(ticks))
    waits = 0

    def wait(_):
        nonlocal waits
        waits += 1
        if waits == 3:
            sampler._stop.set()

    monkeypatch.setattr(sampler._stop, "wait", wait)
    log = Mock()
    monkeypatch.setattr(attribution.logger, "info", log)
    sampler._sample_stacks()
    args = log.call_args.args
    assert args[3] == 0.1  # Observed CPU delta, not three samples * sample interval.
    assert args[-1] == [(("module.py:42:work",), 3, 0.1)]


def test_stack_metadata_is_detached_before_thread_stat_io(monkeypatch) -> None:
    sampler = attribution.ResourceAttribution()
    parent = SimpleNamespace(
        f_code=SimpleNamespace(co_filename="module.py", co_name="parent"),
        f_lineno=10,
        f_back=None,
    )
    child = SimpleNamespace(
        f_code=SimpleNamespace(co_filename="module.py", co_name="child"),
        f_lineno=20,
        f_back=parent,
    )
    monkeypatch.setattr(
        attribution.threading, "enumerate", lambda: [SimpleNamespace(ident=-1, native_id=1)]
    )
    monkeypatch.setattr(attribution.sys, "_current_frames", lambda: {-1: child})

    def stat(_):
        child.f_back = None
        sampler._stop.set()
        return (0, 1)

    monkeypatch.setattr(attribution, "_thread_ticks", stat)
    log = Mock()
    monkeypatch.setattr(attribution.logger, "info", log)
    sampler._sample_stacks()
    assert log.call_args.args[-1][0][0] == ("module.py:20:child", "module.py:10:parent")


@pytest.mark.asyncio
async def test_loop_delay_is_measured_without_claiming_cpu_time(monkeypatch) -> None:
    sampler = attribution.ResourceAttribution(configured=True, duration=1)
    clock = [0.0]
    monkeypatch.setattr(attribution.time, "monotonic", lambda: clock[0])
    calls = 0

    async def delayed_wait(awaitable, *, timeout):
        nonlocal calls
        awaitable.close()
        clock[0] += timeout + 0.020
        calls += 1
        if calls == 3:
            sampler._stop.set()
        raise TimeoutError

    monkeypatch.setattr(attribution.asyncio, "wait_for", delayed_wait)
    await sampler._observe_loop_delay()
    result = sampler.report["event_loop"]
    assert result["kind"] == "scheduling_delay_not_cpu_time"
    assert result["samples"] == 2
    assert result["max_delay_seconds"] == pytest.approx(0.020)
    assert sum(result["bucket_counts"]) == 2


@pytest.mark.asyncio
async def test_active_stop_joins_and_measurement_failure_is_fail_open(monkeypatch) -> None:
    monkeypatch.setenv("COGNIS_RESOURCE_PROFILING", "true")
    monkeypatch.delenv("COGNIS_RESOURCE_PROFILE_POD", raising=False)
    sampler = attribution.ResourceAttribution()
    sampler.delay = 0
    measured = asyncio.Event()

    def summary():
        measured.set()
        raise RuntimeError("private error detail")

    sampler.start(summary)
    await asyncio.wait_for(measured.wait(), timeout=2)
    await sampler.stop()
    assert sampler.task is not None and sampler.task.done()
    assert sampler._worker is None


@pytest.mark.asyncio
async def test_cancelled_stop_waits_for_worker_cleanup(monkeypatch) -> None:
    monkeypatch.setenv("COGNIS_RESOURCE_PROFILING", "true")
    monkeypatch.delenv("COGNIS_RESOURCE_PROFILE_POD", raising=False)
    sampler = attribution.ResourceAttribution()
    sampler.delay = 0
    started = asyncio.Event()
    finished = threading.Event()
    release = threading.Event()
    loop = asyncio.get_running_loop()

    def worker():
        loop.call_soon_threadsafe(started.set)
        release.wait(timeout=5)
        finished.set()

    monkeypatch.setattr(sampler, "_sample_stacks", worker)
    sampler.start(lambda: {})
    await asyncio.wait_for(started.wait(), timeout=2)
    stopping = asyncio.create_task(sampler.stop())
    try:
        await asyncio.sleep(0)
        stopping.cancel()
        await asyncio.sleep(0)
        assert not stopping.done()
    finally:
        release.set()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(stopping, timeout=2)
    assert finished.is_set()
    assert sampler._worker is None
