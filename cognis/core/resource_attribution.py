"""Bounded, opt-in attribution; never inspect frame locals or log object contents."""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import gc
import inspect
import itertools
import logging
import os
import socket
import sys
import threading
import time
from collections import Counter
from collections.abc import Awaitable, Callable, Iterable, Iterator
from pathlib import Path
from types import FrameType
from typing import Any

from pydantic import BaseModel

logger = logging.getLogger(__name__)

type MemorySummary = Callable[[], dict[str, Any] | Awaitable[dict[str, Any]]]


def _dataclass_children(value: object) -> Iterator[object]:
    for field in dataclasses.fields(value):  # type: ignore[arg-type]
        yield object.__getattribute__(value, field.name)


def _integer(name: str, default: int, minimum: int, maximum: int) -> int:
    try:
        return max(minimum, min(maximum, int(os.getenv(name, str(default)))))
    except ValueError:
        return default


def measure_owned_objects(
    roots: Iterable[object], *, max_nodes: int = 20_000, budget_seconds: float = 0.002
) -> dict[str, int | bool]:
    """Estimate reachable Python bytes, stopping before an unbounded graph walk.

    Called synchronously on the owner event loop. Shared objects count once per
    invocation. Unsupported objects are shallow-only; this is not an RSS census.
    Iterators avoid materializing wide containers or retaining graphs afterwards.
    """
    deadline = time.monotonic() + budget_seconds
    seen: set[int] = set()
    stack: list[Iterator[object]] = [iter(roots)]
    size = shallow_only = 0
    truncated = False
    while stack:
        if len(seen) >= max_nodes or time.monotonic() >= deadline:
            truncated = True
            break
        try:
            value = next(stack[-1])
        except StopIteration:
            stack.pop()
            continue
        identity = id(value)
        if identity in seen:
            continue
        seen.add(identity)
        kind = type(value)
        # Only known builtins/classes: never invoke arbitrary __sizeof__ hooks.
        if kind in (str, bytes, bytearray, int, float, bool, type(None)):
            size += sys.getsizeof(value)
            continue
        children: Iterator[object] | None = None
        if kind is dict:
            size += sys.getsizeof(value)
            children = itertools.chain.from_iterable(value.items())  # type: ignore[attr-defined]
        elif kind in (list, tuple, set, frozenset):
            size += sys.getsizeof(value)
            children = iter(value)  # type: ignore[call-overload]
        elif dataclasses.is_dataclass(value) and not isinstance(value, type):
            size += object.__sizeof__(value)
            children = _dataclass_children(value)
        elif isinstance(value, BaseModel):
            size += object.__sizeof__(value)
            children = iter((object.__getattribute__(value, "__dict__"),))
        else:
            shallow_only += 1
            size += object.__sizeof__(value)
        if children is not None:
            if len(stack) < 32:
                stack.append(children)
            else:
                truncated = True
    return {
        "estimated_bytes": size,
        "visited_objects": len(seen),
        "shallow_only_objects": shallow_only,
        "truncated": truncated,
    }


def _thread_ticks(tid: int) -> tuple[int, int] | None:
    """Return native CPU ticks and start time, guarding against TID reuse."""
    try:
        fields = Path(f"/proc/self/task/{tid}/stat").read_text().rsplit(")", 1)[1].split()
        return int(fields[11]) + int(fields[12]), int(fields[19])
    except (OSError, ValueError, IndexError):
        return None


def _process_memory() -> dict[str, int]:
    from cognis.core.allocator_memory import allocator_memory

    result = {}
    with contextlib.suppress(OSError, ValueError):
        for line in Path("/proc/self/status").read_text().splitlines():
            name, _, value = line.partition(":")
            if name in {"VmRSS", "RssAnon", "RssFile", "RssShmem"}:
                result[name] = int(value.split()[0]) * 1024
    result.update(allocator_memory())
    return result


def _capture_stacks() -> dict[int, tuple[str, ...]]:
    # Format every stack before procfs I/O releases the GIL: suspended generator
    # frames may lose their caller link while the sampler reads a thread's stat.
    frames = sys._current_frames()
    frame: FrameType | None = None
    stacks = {}
    try:
        for ident, frame in frames.items():
            if ident == threading.get_ident():
                continue
            locations = []
            for _ in range(24):
                if frame is None:
                    break
                code = frame.f_code
                locations.append(f"{code.co_filename[-240:]}:{frame.f_lineno}:{code.co_name[:80]}")
                frame = frame.f_back
            stacks[ident] = tuple(locations)
        return stacks
    finally:
        frames.clear()
        frame = None


class ResourceAttribution:
    """One delayed capture per process; disabled means no task/thread/profiler."""

    def __init__(
        self,
        *,
        configured: bool = False,
        duration: int = 120,
        interval_ms: int = 100,
        stacks: bool = True,
        log_details: bool = True,
    ) -> None:
        selected = os.getenv("COGNIS_RESOURCE_PROFILE_POD", "")
        self.enabled = os.getenv("COGNIS_RESOURCE_PROFILING", "").lower() in {"true", "1"}
        self.enabled &= not selected or selected == socket.gethostname()
        self.delay = _integer("COGNIS_RESOURCE_PROFILE_DELAY_SECONDS", 300, 0, 3600)
        self.duration = _integer("COGNIS_RESOURCE_PROFILE_DURATION_SECONDS", 120, 1, 600)
        self.interval_ms = _integer("COGNIS_RESOURCE_PROFILE_SAMPLE_MS", 100, 50, 1000)
        self.task: asyncio.Task[None] | None = None
        self._stop = threading.Event()
        self._wake = asyncio.Event()
        self._worker: threading.Thread | None = None
        self._owner_ident: int | None = None
        self.stacks = stacks
        self.log_details = log_details
        self.report: dict[str, Any] = {"memory": [], "cpu": None, "errors": []}
        if configured:
            self.enabled = True
            self.delay = 0
            self.duration = max(1, min(600, duration))
            self.interval_ms = max(50, min(1000, interval_ms))

    def start(self, memory_summary: MemorySummary) -> None:
        if self.enabled and self.task is None:
            self._owner_ident = threading.get_ident()
            self.task = asyncio.create_task(self._run(memory_summary), name="resource-attribution")

    async def _run(self, memory_summary: MemorySummary) -> None:
        loop_observer: asyncio.Task[None] | None = None
        try:
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._wake.wait(), timeout=self.delay)
            if self._stop.is_set():
                return
            from cognis import __version__

            self.report["schema_version"] = 2
            self.report["runtime"] = {
                "build": os.getenv("COGNIS_BUILD_SHA")
                or os.getenv("COGNIS_BUILD_ID")
                or __version__,
                "python": sys.version.split()[0],
                "build_source": (
                    "environment"
                    if os.getenv("COGNIS_BUILD_SHA") or os.getenv("COGNIS_BUILD_ID")
                    else "package_version"
                ),
                "pid": os.getpid(),
                "sampling_scope": "process_local",
                "owner_bytes_additive": False,
            }
            gc_start = gc.get_stats()
            wall_start = time.monotonic()
            cpu_start = time.process_time()
            if self.stacks:
                loop_observer = asyncio.create_task(
                    self._observe_loop_delay(), name="resource-event-loop-delay"
                )
                self._worker = threading.Thread(
                    target=self._sample_stacks, name="resource-attribution-sampler", daemon=True
                )
                self._worker.start()
            deadline = time.monotonic() + self.duration
            while not self._stop.is_set():
                started = time.monotonic()
                try:
                    summary = memory_summary()
                    if inspect.isawaitable(summary):
                        summary = await summary
                    measured = time.monotonic() - started
                    process = await asyncio.to_thread(_process_memory)
                    self.report["memory"].append(
                        {
                            "timestamp": time.time(),
                            "elapsed_seconds": self.duration - max(0, deadline - time.monotonic()),
                            "owners": summary,
                            "process": process,
                            "measurement_seconds": measured,
                            "runtime": {
                                "elapsed_seconds": time.monotonic() - wall_start,
                                "process_cpu_seconds": time.process_time() - cpu_start,
                                "python_threads": threading.active_count(),
                                "gc_delta": [
                                    {
                                        key: current[key] - baseline[key]
                                        for key in ("collections", "collected", "uncollectable")
                                    }
                                    for current, baseline in zip(
                                        gc.get_stats(), gc_start, strict=True
                                    )
                                ],
                            },
                        }
                    )
                    if self.log_details:
                        logger.info(
                            "resource_memory owners=%s process=%s measurement_seconds=%s",
                            summary,
                            process,
                            measured,
                        )
                except Exception:
                    if "memory_sample_failed" not in self.report["errors"]:
                        self.report["errors"].append("memory_sample_failed")
                    logger.warning("Resource memory attribution failed")
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(self._wake.wait(), timeout=min(30, remaining))
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning("Resource attribution failed")
        finally:
            self._stop.set()
            if loop_observer is not None:
                loop_observer.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await loop_observer
            if self._worker is not None:
                # Join before lifecycle teardown can release owned objects.
                await asyncio.to_thread(self._worker.join)
                self._worker = None

    async def _observe_loop_delay(self) -> None:
        """Observe owner-loop scheduling delay without callbacks or task contents."""
        bounds = (0.001, 0.005, 0.010, 0.025, 0.050, 0.100, 0.250, 0.500, 1.0)
        counts = [0] * (len(bounds) + 1)
        samples = 0
        maximum = 0.0
        deadline = time.monotonic() + self.duration
        try:
            while not self._stop.is_set():
                delay = min(0.100, max(0, deadline - time.monotonic()))
                if delay <= 0:
                    break
                expected = time.monotonic() + delay
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(self._wake.wait(), timeout=delay)
                if self._stop.is_set():
                    break
                lag = max(0, time.monotonic() - expected)
                counts[
                    next((i for i, bound in enumerate(bounds) if lag <= bound), len(bounds))
                ] += 1
                samples += 1
                maximum = max(maximum, lag)
        finally:
            self.report["event_loop"] = {
                "kind": "scheduling_delay_not_cpu_time",
                "interval_seconds": 0.100,
                "samples": samples,
                "max_delay_seconds": maximum,
                "bucket_upper_seconds": [*bounds, None],
                "bucket_counts": counts,
            }

    def _sample_stacks(self) -> None:
        counts: Counter[tuple[str, ...]] = Counter()
        cpu_by_stack: Counter[tuple[str, ...]] = Counter()
        cpu_by_role: Counter[str] = Counter()
        previous: dict[int, tuple[int, int]] = {}
        observed_ticks = unattributed_ticks = samples = dropped = 0
        started = time.monotonic()
        cpu_started = time.process_time()
        observer_started = time.thread_time()
        previous_sample = started
        max_interval = 0.0
        missing_stats = 0
        try:
            hz = os.sysconf("SC_CLK_TCK")
            while not self._stop.is_set() and time.monotonic() - started < self.duration:
                now = time.monotonic()
                max_interval = max(max_interval, now - previous_sample)
                previous_sample = now
                threads = {t.ident: t.native_id for t in threading.enumerate()}
                stacks = _capture_stacks()
                try:
                    current = {}
                    for ident, key in stacks.items():
                        tid = threads.get(ident)
                        ticks = _thread_ticks(tid) if tid is not None else None
                        missing_stats += int(ticks is None)
                        delta = 0
                        if tid is not None and ticks is not None:
                            prior = previous.get(tid)
                            if prior and ticks[1] == prior[1]:
                                delta = max(0, ticks[0] - prior[0])
                            current[tid] = ticks
                        observed_ticks += delta
                        cpu_by_role["owner_thread" if ident == self._owner_ident else "worker"] += (
                            delta
                        )
                        samples += 1
                        if key in counts or len(counts) < 256:
                            counts[key] += 1
                            cpu_by_stack[key] += delta
                        else:
                            dropped += 1
                            unattributed_ticks += delta
                    previous = current
                finally:
                    stacks.clear()
                self._stop.wait(self.interval_ms / 1000)
            top = sorted(counts, key=lambda key: (cpu_by_stack[key], counts[key]), reverse=True)[
                :20
            ]
            self.report["cpu"] = {
                "method": "python_stack_endpoints_with_thread_tick_deltas",
                "native_stacks": False,
                "thread_scope": "python_threads_only",
                "sampler_thread_cpu_seconds": time.thread_time() - observer_started,
                "max_sample_interval_seconds": max_interval,
                "missing_thread_stat_samples": missing_stats,
                "unattributed_process_cpu_seconds": max(
                    0, time.process_time() - cpu_started - observed_ticks / hz
                ),
                "elapsed_seconds": time.monotonic() - started,
                "process_cpu_seconds": time.process_time() - cpu_started,
                "observed_thread_cpu_seconds": observed_ticks / hz,
                "dropped_cpu_seconds": unattributed_ticks / hz,
                "thread_role_cpu_seconds": {
                    role: ticks / hz for role, ticks in cpu_by_role.items()
                },
                "stack_samples": samples,
                "dropped_samples": dropped,
                "top_endpoint_stacks": [(key, counts[key], cpu_by_stack[key] / hz) for key in top],
            }
            if self.log_details:
                logger.info(
                    "resource_cpu elapsed_seconds=%s process_cpu_seconds=%s "
                    "observed_thread_cpu_seconds=%s dropped_cpu_seconds=%s "
                    "thread_role_cpu_seconds=%s stack_samples=%s dropped_samples=%s "
                    "top_endpoint_stacks=%s",
                    time.monotonic() - started,
                    time.process_time() - cpu_started,
                    observed_ticks / hz,
                    unattributed_ticks / hz,
                    {role: ticks / hz for role, ticks in cpu_by_role.items()},
                    samples,
                    dropped,
                    [(key, counts[key], cpu_by_stack[key] / hz) for key in top],
                )
        except Exception:
            self.report["errors"].append("cpu_sample_failed")
            logger.warning("Resource CPU attribution failed")
        finally:
            counts.clear()
            cpu_by_stack.clear()

    async def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        if self.task is not None:
            cancelled = False
            while not self.task.done():
                try:
                    await asyncio.shield(self.task)
                except asyncio.CancelledError:
                    cancelled = True
            # Preserve cancellation, but only after the sampling worker is joined.
            if cancelled:
                raise asyncio.CancelledError
            self.task.result()
