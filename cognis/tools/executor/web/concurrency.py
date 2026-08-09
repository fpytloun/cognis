"""Concurrency + rate-limit controller for web tool backends.

When multiple agents (or one agent with parallel tool calls) hit the web
backends concurrently we need to bound:

* total concurrent web ops on this executor (avoid CPU/file-descriptor
  starvation),
* per-backend concurrency (Brave free-tier needs a tighter cap than direct),
* per-host concurrency (don't DDoS one site and trip our own circuit
  breaker), and
* per-backend qps for backends with strict quotas.

This module exposes :class:`WebConcurrencyController` (a single per-process
instance, lazily wired into ``runtime_metadata``) which composes
``asyncio.Semaphore`` slots with token-bucket rate limiters from
``aiolimiter``.

All knobs are configurable via ``web.concurrency.*`` and ``web.rate_limit.*``
settings; defaults are tuned for typical free-tier quotas and an executor
running a small fleet of agents.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import TYPE_CHECKING
from urllib.parse import urlparse

from cognis.logging import get_logger

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

logger = get_logger(__name__)

WEB_CONCURRENCY_KEY = "web_concurrency_controller"

# Defaults — see docs/guide/configuring-providers.md for tuning guidance.
DEFAULT_GLOBAL_CAP = 16
DEFAULT_PER_HOST_CAP = 4
DEFAULT_BACKEND_CAPS: dict[str, int] = {
    # Direct web operations use synchronous libraries in worker threads.
    "direct": 4,
    # DDG is an unofficial scraped endpoint and needs a tighter lane than
    # ordinary direct HTTP fetches.
    "direct_search": 2,
    "tavily": 8,
    "brave": 2,
    "searxng": 4,
    "browser": 4,
}
DEFAULT_RATE_LIMITS_QPS: dict[str, float] = {
    # 0.0 means unlimited (no rate-limit token bucket created).
    "direct": 0.0,
    "direct_search": 1.0,
    "tavily": 5.0,
    "brave": 1.0,
    "searxng": 5.0,
    "browser": 0.0,
}


@dataclass
class WebConcurrencySettings:
    """Runtime-tunable concurrency configuration."""

    global_cap: int = DEFAULT_GLOBAL_CAP
    per_host_cap: int = DEFAULT_PER_HOST_CAP
    backend_caps: dict[str, int] = field(default_factory=lambda: dict(DEFAULT_BACKEND_CAPS))
    rate_limits_qps: dict[str, float] = field(default_factory=lambda: dict(DEFAULT_RATE_LIMITS_QPS))

    def cap_for(self, backend: str) -> int:
        return max(1, int(self.backend_caps.get(backend, self.global_cap)))

    def qps_for(self, backend: str) -> float:
        return max(0.0, float(self.rate_limits_qps.get(backend, 0.0)))


def host_for(url: str) -> str | None:
    """Extract the lowercase hostname for per-host rate limiting."""
    try:
        parsed = urlparse(url)
    except (ValueError, TypeError):
        return None
    host = (parsed.hostname or "").strip().lower()
    return host or None


class WebConcurrencyController:
    """Composes global / per-backend / per-host semaphores with rate limits."""

    def __init__(self, settings: WebConcurrencySettings | None = None) -> None:
        self._settings = settings or WebConcurrencySettings()
        self._global = asyncio.Semaphore(self._settings.global_cap)
        self._backend_locks: dict[str, asyncio.Semaphore] = {
            name: asyncio.Semaphore(cap) for name, cap in self._settings.backend_caps.items()
        }
        # Per-host semaphores are created lazily — agents may hit thousands
        # of distinct hosts and we don't want to pre-allocate that surface.
        self._host_locks: dict[tuple[str, str], asyncio.Semaphore] = {}
        self._host_lock_factory = asyncio.Lock()
        self._rate_limiters: dict[str, tuple[asyncio.AbstractEventLoop, object]] = {}
        self._rate_lock = asyncio.Lock()

    @property
    def settings(self) -> WebConcurrencySettings:
        return self._settings

    def _get_backend_lock(self, backend: str) -> asyncio.Semaphore:
        lock = self._backend_locks.get(backend)
        if lock is None:
            cap = self._settings.cap_for(backend)
            lock = asyncio.Semaphore(cap)
            self._backend_locks[backend] = lock
        return lock

    async def _get_host_lock(self, backend: str, host: str) -> asyncio.Semaphore:
        key = (backend, host)
        lock = self._host_locks.get(key)
        if lock is not None:
            return lock
        async with self._host_lock_factory:
            lock = self._host_locks.get(key)
            if lock is None:
                lock = asyncio.Semaphore(self._settings.per_host_cap)
                self._host_locks[key] = lock
        return lock

    async def _get_rate_limiter(self, backend: str) -> object | None:
        qps = self._settings.qps_for(backend)
        if qps <= 0:
            return None
        loop = asyncio.get_running_loop()
        existing = self._rate_limiters.get(backend)
        if existing is not None and existing[0] is loop:
            return existing[1]
        async with self._rate_lock:
            existing = self._rate_limiters.get(backend)
            if existing is not None and existing[0] is loop:
                return existing[1]
            try:
                from aiolimiter import AsyncLimiter
            except Exception as exc:  # pragma: no cover - defensive
                logger.warning(
                    "web: aiolimiter unavailable; rate limiting disabled (%s)",
                    type(exc).__name__,
                )
                return None
            # AsyncLimiter cannot acquire its default one-token amount when
            # max_rate is below one. Represent fractional QPS as one request
            # per inverse-QPS period instead.
            limiter: object
            if qps < 1:
                limiter = AsyncLimiter(max_rate=1, time_period=1 / qps)
            else:
                limiter = AsyncLimiter(max_rate=qps, time_period=1.0)
            self._rate_limiters[backend] = (loop, limiter)
            return limiter

    @asynccontextmanager
    async def acquire(
        self,
        *,
        backend: str,
        host: str | None = None,
        op: str = "fetch",
    ) -> AsyncIterator[None]:
        """Reserve global + per-backend (+ per-host) slots for one operation.

        The token-bucket rate limit (if configured) is awaited inside the
        slot, so a backend at its qps cap holds its slot until the bucket
        refills. This keeps the upstream service from seeing more than
        ``qps`` requests per second from this executor regardless of how
        many concurrent callers are queued.
        """
        del op  # currently informational only; reserved for metrics
        backend_lock = self._get_backend_lock(backend)
        async with self._global, backend_lock:
            host_cm: asyncio.Semaphore | None = None
            if host:
                host_cm = await self._get_host_lock(backend, host)
            if host_cm is None:
                await self._await_rate_limit(backend)
                yield
                return
            async with host_cm:
                await self._await_rate_limit(backend)
                yield

    async def _await_rate_limit(self, backend: str) -> None:
        limiter = await self._get_rate_limiter(backend)
        if limiter is None:
            return
        # AsyncLimiter is an async context manager; entering blocks until a
        # token is available.
        async with limiter:  # type: ignore[attr-defined]
            return


def build_settings_from_metadata(metadata: dict[str, object]) -> WebConcurrencySettings:
    """Construct settings from ``runtime_metadata['web_concurrency']`` dict."""
    raw = metadata.get("web_concurrency")
    if not isinstance(raw, dict):
        return WebConcurrencySettings()

    global_cap = int(raw.get("global_cap", DEFAULT_GLOBAL_CAP) or DEFAULT_GLOBAL_CAP)
    per_host_cap = int(raw.get("per_host_cap", DEFAULT_PER_HOST_CAP) or DEFAULT_PER_HOST_CAP)
    backend_caps_raw = raw.get("backend_caps")
    backend_caps = dict(DEFAULT_BACKEND_CAPS)
    if isinstance(backend_caps_raw, dict):
        for name, value in backend_caps_raw.items():
            try:
                backend_caps[str(name)] = max(1, int(value))
            except (TypeError, ValueError):
                continue

    rate_limits_raw = raw.get("rate_limits_qps")
    rate_limits = dict(DEFAULT_RATE_LIMITS_QPS)
    if isinstance(rate_limits_raw, dict):
        for name, value in rate_limits_raw.items():
            try:
                rate_limits[str(name)] = max(0.0, float(value))
            except (TypeError, ValueError):
                continue

    return WebConcurrencySettings(
        global_cap=max(1, global_cap),
        per_host_cap=max(1, per_host_cap),
        backend_caps=backend_caps,
        rate_limits_qps=rate_limits,
    )


def get_or_create_controller(
    metadata: dict[str, object],
) -> WebConcurrencyController:
    """Lazily construct the per-executor controller."""
    existing = metadata.get(WEB_CONCURRENCY_KEY)
    if isinstance(existing, WebConcurrencyController):
        return existing
    controller = WebConcurrencyController(build_settings_from_metadata(metadata))
    metadata[WEB_CONCURRENCY_KEY] = controller
    return controller
