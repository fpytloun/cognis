"""Bounded worker-thread offload for CPU-bound controller work.

Context assembly, prompt projection, and exact token counting are synchronous
CPU work. Running them inline starves the event loop long enough to miss
liveness probes and lease renewals on large sessions, so the call sites hand
them to ``run_cpu_bound`` instead.

Capacity accounting is tied to the worker thread, not to the awaiting
coroutine: a cancelled awaiter stops waiting, but the slot stays taken until
the thread actually returns, so repeated cancellations can never exceed the
configured concurrency. Callers therefore pass isolated inputs (copies) and
publish results only after their own cancellation and fence checks.
"""

from __future__ import annotations

import asyncio
import contextlib
import functools
import os
import time
import weakref
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from prometheus_client import Counter, Gauge, Histogram

DEFAULT_WORKERS = 2
DEFAULT_QUEUE = 16

OFFLOAD_WAIT_SECONDS = Histogram(
    "cognis_cpu_offload_wait_seconds",
    "Time an offloaded CPU task waited for a worker slot",
    buckets=(0.001, 0.01, 0.05, 0.1, 0.5, 1.0, 2.0, 5.0, 10.0, 30.0),
)
OFFLOAD_RUN_SECONDS = Histogram(
    "cognis_cpu_offload_run_seconds",
    "Wall time of an offloaded CPU task on its worker thread",
    labelnames=("task",),
    buckets=(0.01, 0.05, 0.1, 0.5, 1.0, 2.0, 5.0, 10.0, 30.0, 60.0),
)
OFFLOAD_INFLIGHT = Gauge(
    "cognis_cpu_offload_inflight",
    "Offloaded CPU tasks currently admitted (queued or running)",
)
OFFLOAD_SATURATED_TOTAL = Counter(
    "cognis_cpu_offload_saturated_total",
    "Offloaded CPU tasks rejected because the admission bound was reached",
)


class CpuOffloadSaturated(RuntimeError):
    """Raised when more work is admitted than the bounded queue allows."""


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return value if value > 0 else default


@dataclass
class _LoopState:
    workers: asyncio.Semaphore
    max_admitted: int
    admitted: int = 0
    tasks: set[asyncio.Task[Any]] = field(default_factory=set)


_STATES: weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, _LoopState] = (
    weakref.WeakKeyDictionary()
)


def _state_for_running_loop() -> _LoopState:
    loop = asyncio.get_running_loop()
    state = _STATES.get(loop)
    if state is None:
        state = _LoopState(
            workers=asyncio.Semaphore(_env_int("COGNIS_CPU_OFFLOAD_WORKERS", DEFAULT_WORKERS)),
            max_admitted=_env_int("COGNIS_CPU_OFFLOAD_QUEUE", DEFAULT_QUEUE),
        )
        _STATES[loop] = state
    return state


def inflight_count() -> int:
    """Return admitted (queued + running) tasks for the running loop."""

    with contextlib.suppress(RuntimeError):
        state = _STATES.get(asyncio.get_running_loop())
        if state is not None:
            return state.admitted
    return 0


async def run_cpu_bound[**P, T](
    fn: Callable[P, T],
    /,
    *args: P.args,
    **kwargs: P.kwargs,
) -> T:
    """Run ``fn(*args, **kwargs)`` on a worker thread under a bounded slot.

    The worker slot is released when the thread finishes, even if the awaiting
    coroutine is cancelled first; the abandoned result is discarded. Callers
    must not hand the worker state that loop code keeps using.
    """

    state = _state_for_running_loop()
    if state.admitted >= state.max_admitted:
        OFFLOAD_SATURATED_TOTAL.inc()
        raise CpuOffloadSaturated(f"cpu offload queue is full ({state.max_admitted} admitted)")
    state.admitted += 1
    OFFLOAD_INFLIGHT.set(state.admitted)
    waited_from = time.perf_counter()
    try:
        await state.workers.acquire()
    except BaseException:
        state.admitted -= 1
        OFFLOAD_INFLIGHT.set(state.admitted)
        raise
    queue_seconds = time.perf_counter() - waited_from
    OFFLOAD_WAIT_SECONDS.observe(queue_seconds)

    # asyncio.to_thread copies the caller's contextvars into the worker.
    call = functools.partial(fn, *args, **kwargs)
    task_label = getattr(fn, "__name__", type(fn).__name__)

    async def run() -> T:
        started = time.perf_counter()
        try:
            return await asyncio.to_thread(call)
        finally:
            run_seconds = time.perf_counter() - started
            OFFLOAD_RUN_SECONDS.labels(task=task_label).observe(run_seconds)
            state.workers.release()
            state.admitted -= 1
            OFFLOAD_INFLIGHT.set(state.admitted)
            from cognis.core.turn_timing import logger, timing_identity

            logger.info(
                "turn: CPU preparation completed",
                extra={
                    "extra_data": {
                        **timing_identity(),
                        "stage": task_label,
                        "queue_seconds": round(queue_seconds, 6),
                        "duration_seconds": round(run_seconds, 6),
                    }
                },
            )

    worker = run()
    try:
        task = asyncio.create_task(worker, name=f"cpu-offload:{task_label}")
    except BaseException:
        worker.close()
        state.workers.release()
        state.admitted -= 1
        OFFLOAD_INFLIGHT.set(state.admitted)
        raise
    state.tasks.add(task)

    def _discard(completed: asyncio.Task[Any]) -> None:
        state.tasks.discard(completed)
        # A cancelled awaiter leaves nobody to retrieve the outcome; consume it
        # so asyncio does not log "exception was never retrieved".
        with contextlib.suppress(BaseException):
            completed.exception()

    task.add_done_callback(_discard)
    return await asyncio.shield(task)
