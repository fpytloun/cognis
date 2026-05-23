"""Executor-local Playwright browser manager."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import os
import re
import shutil
import sys
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from cognis.logging import get_logger
from cognis.tools.executor.browser.assets import load_asset
from cognis.tools.executor.browser.install import (
    RUNTIME_PATCHRIGHT,
    RUNTIME_PLAYWRIGHT,
    SUPPORTED_RUNTIMES,
    ensure_browser_runtime,
    ensure_playwright_browser,
)

logger = get_logger(__name__)

BROWSER_MANAGER_KEY = "browser_manager"
BROWSER_DEFAULT_IDLE_TIMEOUT_SECONDS = 1800
BROWSER_DEFAULT_EPHEMERAL_IDLE_TIMEOUT_SECONDS = 60
BROWSER_DEFAULT_MAX_SESSIONS = 8
BROWSER_DEFAULT_NAVIGATION_TIMEOUT_SECONDS = 60
BROWSER_DEFAULT_WAIT_UNTIL = "domcontentloaded"
BROWSER_DEFAULT_NETWORK_IDLE_AFTER_DOM_SECONDS = 3
BROWSER_DEFAULT_PROFILE_MODE = "persistent_local"
BROWSER_DEFAULT_VIEWPORT_WIDTH = 1365
BROWSER_DEFAULT_VIEWPORT_HEIGHT = 900
BROWSER_DIAGNOSTIC_EVENT_LIMIT = 200

# Pinned recent Chrome desktop UA used as the realistic-UA fallback when we
# cannot probe the running Chromium build at startup. Update periodically.
BROWSER_DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/127.0.0.0 Safari/537.36"
)
BROWSER_DEFAULT_ACCEPT_LANGUAGE = "en-US,en;q=0.9"
BROWSER_DEFAULT_TIMEZONE_ID = "UTC"

# Stage C defaults
BROWSER_DEFAULT_AUTO_CONSENT_DELAY_MS = 800
BROWSER_DEFAULT_HUMANIZE_INTENSITY = "low"
SUPPORTED_HUMANIZE_INTENSITIES: tuple[str, ...] = ("off", "low", "medium", "high")
SUPPORTED_AUTO_CONSENT_ACTIONS: tuple[str, ...] = ("accept", "reject", "off")

# Asset filenames (kept here so tests can monkey-patch the loader).
_AUTOCONSENT_ASSET = "autoconsent.bundle.js"
_FP_AUDIO_ASSET = "fingerprint_audio.js"
_FP_BATTERY_ASSET = "fingerprint_battery.js"
_FP_VIEWPORT_ASSET = "fingerprint_viewport_jitter.js"

# Per-evasion exclusion names usable in ``stealth_evasions`` to disable a
# specific Stage C hardening init script. Names are namespaced so they do
# not collide with playwright-stealth's evasion keys.
FP_EXCLUSION_KEYS: dict[str, str] = {
    "audio_context": _FP_AUDIO_ASSET,
    "battery_api": _FP_BATTERY_ASSET,
    "viewport_jitter": _FP_VIEWPORT_ASSET,
}


@dataclass
class BrowserSession:
    session_id: str
    context: Any
    page: Any
    auth_origin: str | None = None
    profile_mode: str = "ephemeral"
    profile_id: str | None = None
    user_data_dir: str | None = None
    headless: bool = True
    display: str | None = None
    ref_map: dict[str, str] = field(default_factory=dict)
    ref_metadata: dict[str, dict[str, Any]] = field(default_factory=dict)
    console_events: list[dict[str, Any]] = field(default_factory=list)
    network_events: list[dict[str, Any]] = field(default_factory=list)
    last_used_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    last_activity_bump_at: float = 0.0
    lifecycle: str = "explicit"
    idle_timeout_seconds: float | None = None
    runtime_generation: int = 0


class BrowserManager:
    """Lazy Playwright runtime for executor browser tools."""

    def __init__(
        self,
        *,
        enabled: bool = True,
        auto_install: bool = False,
        engine: str = "chromium",
        runtime: str = RUNTIME_PLAYWRIGHT,
        channel: str | None = None,
        headed_allowed: bool = False,
        max_sessions: int = BROWSER_DEFAULT_MAX_SESSIONS,
        idle_timeout_seconds: int = BROWSER_DEFAULT_IDLE_TIMEOUT_SECONDS,
        persistent_profiles_enabled: bool = True,
        profile_mode_default: str = BROWSER_DEFAULT_PROFILE_MODE,
        profile_base_dir: str | None = None,
        realistic_launch: bool = True,
        xvfb_auto: bool = True,
        locale: str = "en-US",
        timezone_id: str | None = None,
        viewport_width: int = BROWSER_DEFAULT_VIEWPORT_WIDTH,
        viewport_height: int = BROWSER_DEFAULT_VIEWPORT_HEIGHT,
        stealth_enabled: bool | None = None,
        stealth_evasions: list[str] | None = None,
        realistic_user_agent: bool = True,
        default_timezone_id: str | None = BROWSER_DEFAULT_TIMEZONE_ID,
        default_accept_language: str = BROWSER_DEFAULT_ACCEPT_LANGUAGE,
        auto_consent: str | None = None,
        auto_consent_disabled_domains: list[str] | None = None,
        auto_consent_delay_ms: int = BROWSER_DEFAULT_AUTO_CONSENT_DELAY_MS,
        humanize_input: bool | None = None,
        humanize_intensity: str = BROWSER_DEFAULT_HUMANIZE_INTENSITY,
        fingerprint_hardening: bool | None = None,
        navigation_timeout_seconds: int = BROWSER_DEFAULT_NAVIGATION_TIMEOUT_SECONDS,
        wait_until: str = BROWSER_DEFAULT_WAIT_UNTIL,
        network_idle_after_dom_seconds: int = BROWSER_DEFAULT_NETWORK_IDLE_AFTER_DOM_SECONDS,
    ) -> None:
        self.enabled = enabled
        self.auto_install = auto_install
        self.engine = engine
        runtime_normalized = (runtime or RUNTIME_PLAYWRIGHT).strip().lower()
        if runtime_normalized not in SUPPORTED_RUNTIMES:
            raise ValueError(
                f"Unsupported browser runtime: {runtime!r}. "
                f"Expected one of {', '.join(SUPPORTED_RUNTIMES)}."
            )
        self.runtime = runtime_normalized
        # Channel is user-explicit only.  We intentionally do NOT auto-pin
        # channel="chrome" for Patchright because installing system Chrome
        # requires root (sudo apt-get) which fails in headless/daemon setups.
        # Users who want the maximum-stealth real-Chrome path should install
        # Chrome system-wide themselves and then set channel="chrome" in
        # executor settings.
        self.channel = (channel or "").strip() or None
        self.headed_allowed = headed_allowed
        self.max_sessions = max_sessions
        self.idle_timeout_seconds = idle_timeout_seconds
        self.persistent_profiles_enabled = persistent_profiles_enabled
        self.profile_mode_default = profile_mode_default
        self.profile_base_dir = self._resolve_profile_base_dir(profile_base_dir)
        self.realistic_launch = realistic_launch
        self.xvfb_auto = xvfb_auto
        self.locale = locale
        self.timezone_id = timezone_id
        self.viewport_width = viewport_width
        self.viewport_height = viewport_height
        # Stealth defaults: ON for vanilla Playwright, OFF for Patchright (it
        # already covers the same evasions and stacking can introduce
        # inconsistencies). Explicit user setting always wins.
        if stealth_enabled is None:
            stealth_enabled = self.runtime != RUNTIME_PATCHRIGHT
        self.stealth_enabled = bool(stealth_enabled)
        self.stealth_evasions = list(stealth_evasions) if stealth_evasions else []
        self.realistic_user_agent = realistic_user_agent
        self.default_timezone_id = default_timezone_id
        self.default_accept_language = default_accept_language

        # Autoconsent is a content-unblocking feature, not a stealth evasion:
        # keep it on by default even for Patchright, where stealth stacking is
        # intentionally disabled.
        if auto_consent is None:
            auto_consent_value = "accept"
        else:
            auto_consent_value = str(auto_consent).strip().lower() or "off"
        if auto_consent_value not in SUPPORTED_AUTO_CONSENT_ACTIONS:
            raise ValueError(
                f"Unsupported auto_consent action: {auto_consent!r}. "
                f"Expected one of {', '.join(SUPPORTED_AUTO_CONSENT_ACTIONS)}."
            )
        self.auto_consent = auto_consent_value
        self.auto_consent_disabled_domains = [
            str(item).strip().lower()
            for item in (auto_consent_disabled_domains or [])
            if isinstance(item, str) and item.strip()
        ]
        self.auto_consent_delay_ms = max(0, int(auto_consent_delay_ms))

        if humanize_input is None:
            humanize_input = self.stealth_enabled
        self.humanize_input = bool(humanize_input)
        intensity_normalized = (humanize_intensity or "low").strip().lower()
        if intensity_normalized not in SUPPORTED_HUMANIZE_INTENSITIES:
            raise ValueError(
                f"Unsupported humanize_intensity: {humanize_intensity!r}. "
                f"Expected one of {', '.join(SUPPORTED_HUMANIZE_INTENSITIES)}."
            )
        self.humanize_intensity = intensity_normalized

        if fingerprint_hardening is None:
            fingerprint_hardening = self.stealth_enabled
        self.fingerprint_hardening = bool(fingerprint_hardening)
        self.navigation_timeout_seconds = max(1, int(navigation_timeout_seconds))
        wait_until_normalized = (wait_until or BROWSER_DEFAULT_WAIT_UNTIL).strip().lower()
        if wait_until_normalized not in {"commit", "domcontentloaded", "load", "networkidle"}:
            wait_until_normalized = BROWSER_DEFAULT_WAIT_UNTIL
        self.wait_until = wait_until_normalized
        self.network_idle_after_dom_seconds = max(0, int(network_idle_after_dom_seconds))

        self._stealth: Any | None = None  # Lazily instantiated
        # Stage B: per-mode browser instances so headless and headed can run
        # concurrently.  True=headless browser, False=headed browser.
        self._browsers: dict[bool, Any] = {}  # headless -> Browser object
        self._browser_generations: dict[bool, int] = {}  # headless -> generation
        # Back-compat alias kept for external readers (e.g. status/diagnostics).
        # Points at the most recently launched browser regardless of mode.
        self._browser: Any | None = None  # last-touched browser, for diagnostics
        self._playwrights: dict[bool, Any] = {}  # headless -> Playwright object
        self._playwright_displays: dict[bool, str | None] = {}  # headless -> display
        self._playwright: Any | None = None  # last-touched runtime, for diagnostics/tests
        self._sessions: dict[str, BrowserSession] = {}
        self._lock = asyncio.Lock()
        self._xvfb_process: asyncio.subprocess.Process | None = None
        self._xvfb_display: str | None = None
        self._claimed_displays: set[str] = set()
        self._reserved_profile_ids: set[str] = set()
        self._runtime_generation = 0
        self._playwright_display: str | None = None  # last-touched display, for diagnostics/tests
        self._headed_open_in_flight = 0
        self._open_in_flight = 0
        self._open_in_flight_by_mode: dict[bool, int] = {True: 0, False: 0}
        self._patchright_persistent_warning_emitted = False
        self._idle_reaper_task: asyncio.Task[None] | None = None
        self._reap_orphan_profile_locks()

    @property
    def active_session_count(self) -> int:
        return len(self._sessions)

    async def list_sessions(self) -> list[dict[str, Any]]:
        sessions: list[dict[str, Any]] = []
        for session in self._sessions.values():
            if self._session_is_idle(session):
                continue
            sessions.append(
                {
                    "session_id": session.session_id,
                    "url": getattr(session.page, "url", ""),
                    "profile_mode": session.profile_mode,
                    "profile_id": session.profile_id,
                    "headless": session.headless,
                    "display": session.display,
                    "last_used_at": session.last_used_at.isoformat(),
                    "lifecycle": getattr(session, "lifecycle", "explicit"),
                    "idle_deadline_at": self._session_idle_deadline_at(session),
                    "auth_origin": session.auth_origin,
                    "console_event_count": len(getattr(session, "console_events", [])),
                    "network_event_count": len(getattr(session, "network_events", [])),
                }
            )
        return sessions

    async def list_profiles(self) -> list[dict[str, Any]]:
        active_profile_ids = {
            session.profile_id
            for session in self._sessions.values()
            if session.profile_id and not self._session_is_idle(session)
        }
        profiles: list[dict[str, Any]] = []
        for entry in sorted(self.profile_base_dir.iterdir(), key=lambda item: item.name):
            if not entry.is_dir():
                continue
            stat = entry.stat()
            profiles.append(
                {
                    "profile_id": entry.name,
                    "currently_in_use": entry.name in active_profile_ids,
                    "last_used_at": datetime.fromtimestamp(stat.st_mtime, tz=UTC).isoformat(),
                }
            )
        return profiles

    async def ensure_runtime(self, *, headless: bool = True) -> None:
        if not self.enabled:
            raise RuntimeError("Browser runtime disabled on this executor")
        if not headless and not self.headed_allowed:
            raise RuntimeError("Headed browser mode is not enabled on this executor")
        async with self._lock:
            if self._browsers.get(headless) is not None:
                return
            await self._launch_shared_browser_locked(headless=headless)

    async def open_session(
        self,
        *,
        session_id: str,
        url: str,
        headless: bool = True,
        auth_state: dict[str, Any] | None = None,
        profile_mode: str = "default",
        profile_id: str | None = None,
        wait_for_slot: bool = False,
        wait_timeout_seconds: float = 30.0,
        lifecycle: str = "explicit",
        session_idle_seconds: float | None = None,
        navigation_timeout_seconds: float | None = None,
        wait_until: str | None = None,
        network_idle_after_dom_seconds: float | None = None,
    ) -> BrowserSession:
        self._ensure_idle_reaper()
        await self._cleanup_idle_sessions()
        async with self._lock:
            session = self._sessions.get(session_id)
            if session is not None and not self._session_is_idle(session):
                session.last_used_at = datetime.now(UTC)
        if session is not None and not self._session_is_idle(session):
            await self._goto(
                session.page,
                url,
                navigation_timeout_seconds=navigation_timeout_seconds,
                wait_until=wait_until,
                network_idle_after_dom_seconds=network_idle_after_dom_seconds,
            )
            return session
        resolved_mode, resolved_profile_id = self._resolve_profile_settings(
            profile_mode=profile_mode,
            profile_id=profile_id,
            url=url,
        )
        self._maybe_emit_patchright_persistent_warning(profile_mode=resolved_mode)
        await self._reserve_open_slot(
            headless=headless,
            wait_for_slot=wait_for_slot,
            wait_timeout_seconds=wait_timeout_seconds,
        )
        try:
            if resolved_mode == "persistent_local":
                await self._reserve_profile_id(str(resolved_profile_id))
                (
                    context,
                    page,
                    user_data_dir,
                    display,
                    runtime_generation,
                ) = await self._open_persistent_context(
                    url=url,
                    headless=headless,
                    auth_state=auth_state,
                    profile_id=resolved_profile_id,
                )
                session = BrowserSession(
                    session_id=session_id,
                    context=context,
                    page=page,
                    auth_origin=url,
                    profile_mode=resolved_mode,
                    profile_id=resolved_profile_id,
                    user_data_dir=str(user_data_dir),
                    headless=headless,
                    display=display,
                    lifecycle=self._normalize_lifecycle(lifecycle),
                    idle_timeout_seconds=session_idle_seconds,
                    runtime_generation=runtime_generation,
                )
                try:
                    async with self._lock:
                        self._register_session_locked(session)
                        self._reserved_profile_ids.discard(str(resolved_profile_id))
                except Exception:
                    await context.close()
                    raise
                self._log_lifecycle(
                    "browser_session_open",
                    outcome="success",
                    session=session,
                )
                try:
                    await self._attach_session_observers(session)
                    await self._goto(
                        page,
                        url,
                        navigation_timeout_seconds=navigation_timeout_seconds,
                        wait_until=wait_until,
                        network_idle_after_dom_seconds=network_idle_after_dom_seconds,
                    )
                except Exception:
                    await self.close_session(session_id)
                    raise
            else:
                await self.ensure_runtime(headless=headless)
                browser = self._browsers[headless]
                runtime_generation = self._browser_generations.get(
                    headless, self._runtime_generation
                )
                display = self._active_display(headless=headless)
                context = await browser.new_context(**self._context_kwargs(auth_state=auth_state))
                await self._apply_context_defaults(context)
                page = await context.new_page()
                session = BrowserSession(
                    session_id=session_id,
                    context=context,
                    page=page,
                    auth_origin=url,
                    profile_mode=resolved_mode,
                    profile_id=resolved_profile_id,
                    headless=headless,
                    display=display,
                    lifecycle=self._normalize_lifecycle(lifecycle),
                    idle_timeout_seconds=session_idle_seconds,
                    runtime_generation=runtime_generation,
                )
                try:
                    async with self._lock:
                        self._register_session_locked(session)
                    self._log_lifecycle(
                        "browser_session_open",
                        outcome="success",
                        session=session,
                    )
                except Exception:
                    await context.close()
                    raise
                try:
                    await self._attach_session_observers(session)
                    await self._goto(
                        page,
                        url,
                        navigation_timeout_seconds=navigation_timeout_seconds,
                        wait_until=wait_until,
                        network_idle_after_dom_seconds=network_idle_after_dom_seconds,
                    )
                except Exception:
                    await self.close_session(session_id)
                    raise
        finally:
            try:
                async with self._lock:
                    if resolved_mode == "persistent_local":
                        self._reserved_profile_ids.discard(str(resolved_profile_id))
                    self._open_in_flight -= 1
                    self._open_in_flight_by_mode[headless] = max(
                        0, self._open_in_flight_by_mode.get(headless, 0) - 1
                    )
                    if not headless:
                        self._headed_open_in_flight -= 1
                    # Tear down the per-mode runtime if no sessions of that
                    # mode remain and no concurrent open is in flight.
                    if (
                        self._open_in_flight_by_mode.get(headless, 0) == 0
                        and not self._has_live_sessions_for_mode_locked(headless)
                        and (
                            self._browsers.get(headless) is not None
                            or self._playwrights.get(headless) is not None
                        )
                    ):
                        await self._stop_playwright_locked(headless=headless)
                    if (
                        not headless
                        and self._headed_open_in_flight == 0
                        and not self._has_live_headed_sessions_locked()
                        and not self._has_live_headed_runtime_locked()
                    ):
                        await self._stop_virtual_display_locked()
            except RuntimeError:
                pass
        return session

    def get_session(self, session_id: str) -> BrowserSession:
        session = self._sessions.get(session_id)
        if session is None:
            raise KeyError(f"Unknown browser session: {session_id}")
        if self._session_is_idle(session):
            raise KeyError(f"Browser session expired due to idleness: {session_id}")
        session.last_used_at = datetime.now(UTC)
        return session

    async def get_live_session(self, session_id: str) -> BrowserSession:
        await self._cleanup_idle_sessions()
        return self.get_session(session_id)

    async def close_session(self, session_id: str) -> None:
        async with self._lock:
            session = self._sessions.pop(session_id, None)
            if session is None:
                return
        await session.context.close()
        self._log_lifecycle("browser_session_close", outcome="success", session=session)
        async with self._lock:
            # Tear down the per-mode browser once all sessions of that mode
            # are gone and no open is in flight for that mode.
            was_headless = session.headless
            live_same_mode = any(s.headless == was_headless for s in self._sessions.values())
            in_flight_same_mode = self._open_in_flight_by_mode.get(was_headless, 0) > 0
            if not live_same_mode and not in_flight_same_mode:
                await self._stop_playwright_locked(headless=was_headless)
            if (
                self._headed_open_in_flight == 0
                and not self._has_live_headed_sessions_locked()
                and not self._has_live_headed_runtime_locked()
            ):
                await self._stop_virtual_display_locked()

    async def cleanup(self) -> None:
        if self._idle_reaper_task is not None:
            self._idle_reaper_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._idle_reaper_task
            self._idle_reaper_task = None
        for session_id in list(self._sessions):
            await self.close_session(session_id)
        async with self._lock:
            await self._close_all_browsers_locked()
            await self._stop_all_playwright_locked()
            await self._stop_virtual_display_locked()

    async def storage_state(self, session_id: str) -> dict[str, Any]:
        session = await self.get_live_session(session_id)
        return await session.context.storage_state()

    async def get_console_events(
        self, session_id: str, *, level: str = "all", limit: int = 100
    ) -> list[dict[str, Any]]:
        session = await self.get_live_session(session_id)
        events = session.console_events
        if level != "all":
            normalized = level.lower()
            events = [
                event for event in events if str(event.get("level", "")).lower() == normalized
            ]
        return events[-max(1, limit) :]

    async def get_network_events(
        self,
        session_id: str,
        *,
        limit: int = 100,
        resource_types: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        session = await self.get_live_session(session_id)
        events = session.network_events
        if resource_types:
            allowed = {item.lower() for item in resource_types}
            events = [
                event for event in events if str(event.get("resource_type", "")).lower() in allowed
            ]
        return events[-max(1, limit) :]

    async def _cleanup_idle_sessions(self) -> None:
        if self.idle_timeout_seconds <= 0:
            return
        stale = [
            session_id
            for session_id, session in self._sessions.items()
            if self._session_is_idle(session)
        ]
        for session_id in stale:
            await self.close_session(session_id)

    async def _reserve_open_slot(
        self,
        *,
        headless: bool,
        wait_for_slot: bool,
        wait_timeout_seconds: float,
    ) -> None:
        """Reserve a session slot, optionally waiting until one is free.

        ``wait_for_slot=False`` (default) preserves the legacy fast-fail
        behaviour: if the pool is full the caller gets a ``RuntimeError``
        immediately. ``wait_for_slot=True`` makes the caller block until a
        slot becomes available or ``wait_timeout_seconds`` elapses, raising
        ``TimeoutError`` in the latter case.
        """
        if not wait_for_slot:
            async with self._lock:
                if len(self._sessions) + self._open_in_flight >= self.max_sessions:
                    raise RuntimeError("Browser session limit exceeded")
                self._open_in_flight += 1
                self._open_in_flight_by_mode[headless] = (
                    self._open_in_flight_by_mode.get(headless, 0) + 1
                )
                if not headless:
                    self._headed_open_in_flight += 1
            return

        deadline = asyncio.get_running_loop().time() + max(0.0, wait_timeout_seconds)
        while True:
            async with self._lock:
                if len(self._sessions) + self._open_in_flight < self.max_sessions:
                    self._open_in_flight += 1
                    self._open_in_flight_by_mode[headless] = (
                        self._open_in_flight_by_mode.get(headless, 0) + 1
                    )
                    if not headless:
                        self._headed_open_in_flight += 1
                    return
            now = asyncio.get_running_loop().time()
            if now >= deadline:
                raise TimeoutError(
                    f"Timed out waiting for browser session slot (max_sessions={self.max_sessions})"
                )
            # Tunable polling interval. ``asyncio.sleep`` cooperates with
            # other tasks releasing slots via ``close_session``.
            await asyncio.sleep(min(0.2, max(0.05, deadline - now)))

    async def _reserve_profile_id(self, profile_id: str) -> None:
        async with self._lock:
            if profile_id in self._reserved_profile_ids or any(
                session.profile_mode == "persistent_local" and session.profile_id == profile_id
                for session in self._sessions.values()
            ):
                raise RuntimeError(f"Browser profile already in use: {profile_id}")
            self._reserved_profile_ids.add(profile_id)

    def _session_is_idle(self, session: BrowserSession | Any) -> bool:
        timeout_seconds = self._session_idle_timeout_seconds(session)
        if timeout_seconds <= 0:
            return False
        last_used_at = getattr(session, "last_used_at", None)
        if last_used_at is None:
            return False
        cutoff = datetime.now(UTC).timestamp() - timeout_seconds
        return last_used_at.timestamp() < cutoff

    def _session_idle_timeout_seconds(self, session: BrowserSession | Any) -> float:
        configured = getattr(session, "idle_timeout_seconds", None)
        if isinstance(configured, int | float) and configured > 0:
            return float(configured)
        return float(self.idle_timeout_seconds)

    def _session_idle_deadline_at(self, session: BrowserSession | Any) -> str | None:
        timeout_seconds = self._session_idle_timeout_seconds(session)
        if timeout_seconds <= 0:
            return None
        last_used_at = getattr(session, "last_used_at", None)
        if last_used_at is None:
            return None
        return datetime.fromtimestamp(
            last_used_at.timestamp() + timeout_seconds, tz=UTC
        ).isoformat()

    def _ensure_idle_reaper(self) -> None:
        if self._idle_reaper_task is not None and not self._idle_reaper_task.done():
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        self._idle_reaper_task = loop.create_task(
            self._idle_reaper_loop(), name="browser-idle-reaper"
        )

    async def _idle_reaper_loop(self) -> None:
        while True:
            await asyncio.sleep(30)
            try:
                await self._cleanup_idle_sessions()
            except Exception:
                logger.debug("browser: idle session reaper failed", exc_info=True)

    def _normalize_lifecycle(self, lifecycle: str) -> str:
        normalized = (lifecycle or "explicit").strip().lower()
        return normalized if normalized in {"ephemeral", "explicit"} else "explicit"

    async def _goto(
        self,
        page: Any,
        url: str,
        *,
        navigation_timeout_seconds: float | None = None,
        wait_until: str | None = None,
        network_idle_after_dom_seconds: float | None = None,
    ) -> None:
        timeout_seconds = (
            float(navigation_timeout_seconds)
            if navigation_timeout_seconds is not None and navigation_timeout_seconds > 0
            else float(self.navigation_timeout_seconds)
        )
        wait_until_value = (
            (wait_until or self.wait_until or BROWSER_DEFAULT_WAIT_UNTIL).strip().lower()
        )
        if wait_until_value not in {"commit", "domcontentloaded", "load", "networkidle"}:
            wait_until_value = BROWSER_DEFAULT_WAIT_UNTIL
        await page.goto(url, timeout=int(timeout_seconds * 1000), wait_until=wait_until_value)
        soft_wait = (
            float(network_idle_after_dom_seconds)
            if network_idle_after_dom_seconds is not None
            else float(self.network_idle_after_dom_seconds)
        )
        if soft_wait > 0 and wait_until_value != "networkidle":
            with contextlib.suppress(Exception):
                await page.wait_for_load_state("networkidle", timeout=int(soft_wait * 1000))

    def _bump_session_activity(self, session: BrowserSession) -> None:
        now = time.monotonic()
        if now - session.last_activity_bump_at < 1.0:
            return
        session.last_activity_bump_at = now
        session.last_used_at = datetime.now(UTC)

    def _reap_orphan_profile_locks(self) -> None:
        if not self.profile_base_dir.exists():
            return
        for lock_path in self.profile_base_dir.glob("**/SingletonLock"):
            if not self._profile_lock_looks_orphaned(lock_path):
                continue
            try:
                lock_path.unlink()
                logger.warning(
                    "browser: removed orphaned profile lock",
                    extra={"extra_data": {"path": str(lock_path)}},
                )
            except OSError:
                logger.debug("browser: failed to remove orphaned profile lock", exc_info=True)

    def _profile_lock_looks_orphaned(self, lock_path: Path) -> bool:
        try:
            if lock_path.is_symlink():
                target = os.readlink(lock_path)
                pid_match = re.search(r"(?:^|[^0-9])(\d{2,})(?:[^0-9]|$)", target)
                if pid_match:
                    return not self._pid_is_alive(int(pid_match.group(1)))
                return True
            if not lock_path.exists():
                return False
            if time.time() - lock_path.stat().st_mtime < 24 * 60 * 60:
                return False
            content = lock_path.read_text(errors="ignore")[:200]
            pid_match = re.search(r"\b(\d{2,})\b", content)
            return not (pid_match and self._pid_is_alive(int(pid_match.group(1))))
        except OSError:
            return False

    def _pid_is_alive(self, pid: int) -> bool:
        if pid <= 0:
            return False
        try:
            os.kill(pid, 0)
            return True
        except ProcessLookupError:
            return False
        except PermissionError:
            return True

    def _resolve_profile_base_dir(self, configured: str | None) -> Path:
        if configured:
            path = Path(configured).expanduser()
        else:
            path = (
                Path(os.environ.get("COGNIS_DATA_DIR", os.path.expanduser("~/.cognis")))
                / "browser-profiles"
            )
        path.mkdir(parents=True, exist_ok=True)
        return path

    def _resolve_profile_settings(
        self, *, profile_mode: str, profile_id: str | None, url: str
    ) -> tuple[str, str | None]:
        normalized_mode = (profile_mode or "default").strip().lower()
        if normalized_mode == "default":
            normalized_mode = self.profile_mode_default
        if normalized_mode not in {"ephemeral", "persistent_local"}:
            raise ValueError(f"Unsupported profile_mode: {profile_mode}")
        normalized_profile_id = profile_id.strip() if isinstance(profile_id, str) else None
        if normalized_mode == "persistent_local":
            if not self.persistent_profiles_enabled:
                raise RuntimeError(
                    "Persistent local browser profiles are disabled on this executor"
                )
            if not normalized_profile_id:
                normalized_profile_id = self._derive_profile_id(url)
        else:
            normalized_profile_id = None
        return normalized_mode, normalized_profile_id

    def _derive_profile_id(self, url: str) -> str:
        host = urlparse(url).hostname or "default"
        return re.sub(r"[^a-z0-9]+", "-", host.lower()).strip("-") or "default"

    async def _ensure_playwright_ready(self, *, headless: bool) -> None:
        async with self._lock:
            await self._ensure_playwright_ready_locked(headless=headless)

    async def _ensure_playwright_ready_locked(self, *, headless: bool) -> str | None:
        # Use the back-compat shim when the manager is running vanilla
        # Playwright with no channel pin, so existing tests that monkeypatch
        # ``ensure_playwright_browser`` keep working unchanged.
        if self.runtime == RUNTIME_PLAYWRIGHT and not self.channel:
            ok, reason = await ensure_playwright_browser(
                auto_install=self.auto_install, engine=self.engine
            )
        else:
            ok, reason = await ensure_browser_runtime(
                runtime=self.runtime,
                engine=self.engine,
                channel=self.channel,
                auto_install=self.auto_install,
            )
        if not ok:
            raise RuntimeError(f"Browser runtime unavailable: {reason}")
        if self._needs_virtual_display(headless=headless):
            await self._ensure_virtual_display_locked()
        display = self._active_display(headless=headless)
        playwright = self._playwrights.get(headless)
        if playwright is not None and self._playwright_displays.get(headless) != display:
            if self._has_live_sessions_for_mode_locked(headless):
                raise RuntimeError("Browser runtime already active for a different display")
            await self._close_shared_browser_locked(headless=headless)
            await self._stop_playwright_locked(headless=headless)
            playwright = None
        if playwright is None:
            async_playwright = self._resolve_async_playwright()
            playwright = await async_playwright().start()
            self._playwrights[headless] = playwright
            self._playwright_displays[headless] = display
            self._playwright = playwright
            self._playwright_display = display
            self._log_lifecycle(
                "playwright_start",
                outcome="success",
                headless=headless,
                display=display,
            )
        else:
            self._playwright = playwright
            self._playwright_display = self._playwright_displays.get(headless)
        return display

    def _resolve_async_playwright(self) -> Any:
        """Import the ``async_playwright`` factory for the active runtime."""
        if self.runtime == RUNTIME_PATCHRIGHT:
            from patchright.async_api import async_playwright as patchright_async

            return patchright_async
        from playwright.async_api import async_playwright as playwright_async

        return playwright_async

    def _needs_virtual_display(self, *, headless: bool) -> bool:
        return (
            not headless
            and self.xvfb_auto
            and sys.platform.startswith("linux")
            and not os.environ.get("DISPLAY")
        )

    async def _ensure_virtual_display(self) -> None:
        async with self._lock:
            await self._ensure_virtual_display_locked()

    async def _ensure_virtual_display_locked(self) -> None:
        if self._xvfb_process is not None:
            return
        xvfb_binary = shutil.which("Xvfb")
        if xvfb_binary is None:
            raise RuntimeError(
                "Headed browser mode on Linux without DISPLAY requires Xvfb to be installed"
            )
        display = self._allocate_display()
        proc = await asyncio.create_subprocess_exec(
            xvfb_binary,
            display,
            "-screen",
            "0",
            f"{self.viewport_width}x{self.viewport_height}x24",
            "-nolisten",
            "tcp",
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await self._wait_for_virtual_display_ready(display=display, proc=proc)
        self._xvfb_process = proc
        self._xvfb_display = display
        self._claimed_displays.add(display)
        self._log_lifecycle("xvfb_start", outcome="success", display=display)

    async def _wait_for_virtual_display_ready(
        self,
        *,
        display: str,
        proc: asyncio.subprocess.Process,
        timeout_seconds: float = 5.0,
        poll_interval_seconds: float = 0.05,
    ) -> None:
        display_number = display.removeprefix(":").split(".", 1)[0]
        socket_path = Path("/tmp/.X11-unix") / f"X{display_number}"
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            if proc.returncode is not None:
                raise RuntimeError("Failed to start Xvfb for headed browser mode")
            if socket_path.exists():
                return
            await asyncio.sleep(poll_interval_seconds)
        raise RuntimeError(f"Timed out waiting for Xvfb display {display} to become ready")

    def _active_display(self, *, headless: bool) -> str | None:
        if headless:
            return None
        return self._xvfb_display or os.environ.get("DISPLAY")

    async def _stop_virtual_display(self) -> None:
        async with self._lock:
            await self._stop_virtual_display_locked()

    async def _stop_virtual_display_locked(self) -> None:
        proc = self._xvfb_process
        self._xvfb_process = None
        if proc is not None:
            proc.terminate()
            try:
                await asyncio.wait_for(proc.wait(), timeout=2)
            except TimeoutError:
                proc.kill()
                await proc.wait()
        if self._xvfb_display is not None:
            self._claimed_displays.discard(self._xvfb_display)
            self._log_lifecycle("xvfb_stop", outcome="success", display=self._xvfb_display)
            self._xvfb_display = None

    def _allocate_display(self) -> str:
        x11_dir = Path("/tmp/.X11-unix")
        for candidate in range(99, 200):
            display = f":{candidate}"
            if display in self._claimed_displays:
                continue
            if not (x11_dir / f"X{candidate}").exists():
                return display
        return f":{int(time.time()) % 1000 + 200}"

    def _launch_kwargs(self, *, headless: bool, display: str | None = None) -> dict[str, Any]:
        args: list[str] = []
        if self.realistic_launch:
            args.extend(
                [
                    "--disable-blink-features=AutomationControlled",
                    "--no-first-run",
                    "--no-default-browser-check",
                ]
            )
            if not headless:
                args.append("--start-maximized")
        kwargs: dict[str, Any] = {"headless": headless, "env": self._launch_env(display)}
        if args:
            kwargs["args"] = args
        if self.channel:
            kwargs["channel"] = self.channel
        return kwargs

    def _context_kwargs(self, *, auth_state: dict[str, Any] | None = None) -> dict[str, Any]:
        kwargs: dict[str, Any] = {
            "viewport": {"width": self.viewport_width, "height": self.viewport_height},
            "locale": self.locale,
        }
        timezone_id = self.timezone_id or (
            self.default_timezone_id if self.stealth_enabled else None
        )
        if timezone_id:
            kwargs["timezone_id"] = timezone_id
        if self.stealth_enabled:
            if self.realistic_user_agent:
                kwargs["user_agent"] = BROWSER_DEFAULT_USER_AGENT
            if self.default_accept_language:
                kwargs["extra_http_headers"] = {
                    "Accept-Language": self.default_accept_language,
                }
        if auth_state is not None:
            kwargs["storage_state"] = auth_state
        return kwargs

    def _build_stealth(self) -> Any | None:
        """Construct (and cache) the ``Stealth`` runner for this manager.

        Returns ``None`` when stealth is disabled or the optional dependency
        is not importable. Per-evasion exclusion list is applied by setting
        the matching ``Stealth`` boolean to ``False``.
        """
        if not self.stealth_enabled:
            return None
        if self._stealth is not None:
            return self._stealth
        try:
            from playwright_stealth import Stealth  # type: ignore[import-untyped]
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning(
                "browser: playwright_stealth unavailable; falling back to launch-arg only "
                "evasions (%s)",
                type(exc).__name__,
            )
            return None
        kwargs: dict[str, Any] = {}
        for evasion in self.stealth_evasions:
            normalized = (evasion or "").strip()
            if normalized:
                kwargs[normalized] = False
        # ``init_scripts_only`` is the safer default for Patchright since
        # Patchright already patches CDP-level leaks; layering stealth's
        # CDP-based evasions on top can introduce inconsistencies.
        if self.runtime == RUNTIME_PATCHRIGHT:
            kwargs.setdefault("init_scripts_only", True)
        try:
            self._stealth = Stealth(**kwargs)
        except TypeError as exc:
            logger.warning(
                "browser: playwright_stealth rejected configuration (%s); falling back to defaults",
                exc,
            )
            self._stealth = Stealth()
        return self._stealth

    def _maybe_emit_patchright_persistent_warning(self, *, profile_mode: str) -> None:
        if self._patchright_persistent_warning_emitted:
            return
        if self.runtime != RUNTIME_PATCHRIGHT:
            return
        if profile_mode == "persistent_local":
            return
        logger.warning(
            "browser: patchright runtime is most effective with persistent profiles "
            "and channel=chrome (current channel=%s, profile_mode=%s)",
            self.channel,
            profile_mode,
        )
        self._patchright_persistent_warning_emitted = True

    async def _apply_context_defaults(self, context: Any, *, profile_id: str | None = None) -> None:
        stealth = self._build_stealth()
        if stealth is not None:
            try:
                await stealth.apply_stealth_async(context)
            except Exception as exc:  # pragma: no cover - defensive
                logger.warning(
                    "browser: failed to apply stealth to context (%s); continuing without",
                    type(exc).__name__,
                )
        await self._apply_autoconsent_init_script(context, profile_id=profile_id)
        await self._apply_fingerprint_hardening_init_scripts(context, profile_id=profile_id)

    async def _apply_autoconsent_init_script(
        self, context: Any, *, profile_id: str | None = None
    ) -> None:
        if self.auto_consent == "off":
            return
        bundle = load_asset(_AUTOCONSENT_ASSET)
        if bundle is None:
            return
        config = {
            "action": self.auto_consent,
            "delayMs": self.auto_consent_delay_ms,
            "disabledHosts": self.auto_consent_disabled_domains,
        }
        prefix = f"window.__cognis_autoconsent = {json.dumps(config)};"
        try:
            await context.add_init_script(prefix + "\n" + bundle)
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning(
                "browser: failed to install autoconsent init script (%s); continuing without",
                type(exc).__name__,
            )

    async def _apply_fingerprint_hardening_init_scripts(
        self, context: Any, *, profile_id: str | None = None
    ) -> None:
        if not self.fingerprint_hardening:
            return
        # Per-profile deterministic seed so re-visits to the same site see a
        # consistent fingerprint. Persistent profile sessions take precedence;
        # ephemeral sessions get a per-runtime-generation seed instead.
        seed_source = profile_id or f"runtime-{self._runtime_generation}"
        seed_value = hashlib.sha256(seed_source.encode("utf-8")).hexdigest()[:32]
        seed_prefix = f"window.__cognis_fp_seed = {json.dumps(seed_value)};"

        excluded = {
            FP_EXCLUSION_KEYS[name] for name in self.stealth_evasions if name in FP_EXCLUSION_KEYS
        }
        # Viewport jitter is suppressed for persistent profiles too:
        # "viewport changed between visits" is a tell, and persistent profile
        # sessions are already stable identity-wise.
        if profile_id:
            excluded.add(_FP_VIEWPORT_ASSET)

        for asset_name in (_FP_AUDIO_ASSET, _FP_BATTERY_ASSET, _FP_VIEWPORT_ASSET):
            if asset_name in excluded:
                continue
            payload = load_asset(asset_name)
            if payload is None:
                continue
            try:
                await context.add_init_script(seed_prefix + "\n" + payload)
            except Exception as exc:  # pragma: no cover - defensive
                logger.warning(
                    "browser: failed to install fingerprint asset %s (%s); continuing",
                    asset_name,
                    type(exc).__name__,
                )

    async def _open_persistent_context(
        self,
        *,
        url: str,
        headless: bool,
        auth_state: dict[str, Any] | None,
        profile_id: str | None,
    ) -> tuple[Any, Any, Path, str | None, int]:
        user_data_dir = self.profile_base_dir / str(profile_id)
        user_data_dir.mkdir(parents=True, exist_ok=True)
        retry_count = 0
        while True:
            async with self._lock:
                display = await self._ensure_playwright_ready_locked(headless=headless)
                runtime_generation = self._runtime_generation
                browser_launcher = getattr(self._playwrights[headless], self.engine)
                launch_kwargs = self._launch_kwargs(headless=headless, display=display)
            try:
                context = await browser_launcher.launch_persistent_context(
                    str(user_data_dir),
                    **launch_kwargs,
                    **self._context_kwargs(),
                )
                break
            except Exception as exc:
                failure_category = self._classify_launch_failure(exc, phase="persistent_launch")
                if retry_count >= 1 or failure_category is None:
                    self._log_lifecycle(
                        "browser_launch",
                        outcome="failure",
                        display=display,
                        failure_category=failure_category or "non_retryable",
                        retry_count=retry_count,
                    )
                    raise
                retry_count += 1
                async with self._lock:
                    await self._recover_retryable_launch_failure_locked(
                        failure_category=failure_category,
                        headless=headless,
                        retry_count=retry_count,
                    )
        await self._apply_context_defaults(context, profile_id=profile_id)
        if auth_state and isinstance(auth_state.get("cookies"), list):
            await context.add_cookies(auth_state["cookies"])
        page = context.pages[0] if context.pages else await context.new_page()
        self._log_lifecycle(
            "browser_launch",
            outcome="success",
            display=display,
            retry_count=retry_count,
        )
        return context, page, user_data_dir, display, runtime_generation

    def _launch_env(self, display: str | None) -> dict[str, str]:
        env = dict(os.environ)
        if display is None:
            env.pop("DISPLAY", None)
        else:
            env["DISPLAY"] = display
        return env

    def _register_session_locked(self, session: BrowserSession) -> None:
        if session.session_id in self._sessions:
            raise RuntimeError(f"Browser session already exists: {session.session_id}")
        if len(self._sessions) >= self.max_sessions:
            raise RuntimeError("Browser session limit exceeded")
        self._sessions[session.session_id] = session

    def _has_live_headed_sessions_locked(self) -> bool:
        return any(not session.headless for session in self._sessions.values())

    def _has_live_headed_runtime_locked(self) -> bool:
        return self._browsers.get(False) is not None or self._playwrights.get(False) is not None

    def _has_live_sessions_for_generation_locked(self, generation: int) -> bool:
        return any(session.runtime_generation == generation for session in self._sessions.values())

    def _has_live_sessions_for_mode_locked(self, headless: bool) -> bool:
        return any(session.headless == headless for session in self._sessions.values())

    async def _close_shared_browser_locked(self, headless: bool | None = None) -> None:
        """Close the per-mode browser, or both if headless is None."""
        modes = [True, False] if headless is None else [headless]
        for mode in modes:
            browser = self._browsers.pop(mode, None)
            if browser is not None:
                with contextlib.suppress(Exception):
                    await browser.close()
        self._browser = next(iter(self._browsers.values()), None)  # diagnostic alias

    async def _close_all_browsers_locked(self) -> None:
        await self._close_shared_browser_locked(headless=None)

    async def _stop_playwright_locked(self, *, headless: bool) -> None:
        await self._close_shared_browser_locked(headless=headless)
        playwright = self._playwrights.get(headless)
        display = self._playwright_displays.get(headless)
        if playwright is not None:
            await playwright.stop()
            self._playwrights.pop(headless, None)
            self._playwright_displays.pop(headless, None)
            self._log_lifecycle(
                "playwright_stop",
                outcome="success",
                headless=headless,
                display=display,
            )
        else:
            self._playwright_displays.pop(headless, None)
        if self._playwright is playwright:
            replacement_mode = next(iter(self._playwrights), None)
            if replacement_mode is None:
                self._playwright = None
                self._playwright_display = None
            else:
                self._playwright = self._playwrights[replacement_mode]
                self._playwright_display = self._playwright_displays.get(replacement_mode)

    async def _stop_all_playwright_locked(self) -> None:
        for mode in list(self._playwrights):
            await self._stop_playwright_locked(headless=mode)

    async def _launch_shared_browser_locked(self, *, headless: bool) -> None:
        retry_count = 0
        while True:
            display = await self._ensure_playwright_ready_locked(headless=headless)
            browser_launcher = getattr(self._playwrights[headless], self.engine)
            try:
                browser = await browser_launcher.launch(
                    **self._launch_kwargs(headless=headless, display=display)
                )
                self._browsers[headless] = browser
                self._browser = browser  # diagnostic alias
                self._runtime_generation += 1
                self._browser_generations[headless] = self._runtime_generation
                self._log_lifecycle(
                    "browser_launch",
                    outcome="success",
                    headless=headless,
                    display=display,
                    retry_count=retry_count,
                )
                return
            except Exception as exc:
                failure_category = self._classify_launch_failure(exc, phase="browser_launch")
                if retry_count >= 1 or failure_category is None:
                    self._log_lifecycle(
                        "browser_launch",
                        outcome="failure",
                        headless=headless,
                        display=display,
                        failure_category=failure_category or "non_retryable",
                        retry_count=retry_count,
                    )
                    raise
                retry_count += 1
                await self._recover_retryable_launch_failure_locked(
                    failure_category=failure_category,
                    headless=headless,
                    retry_count=retry_count,
                )

    async def _recover_retryable_launch_failure_locked(
        self,
        *,
        failure_category: str,
        headless: bool | None = None,
        retry_count: int,
    ) -> None:
        if headless is None:
            if self._sessions:
                raise RuntimeError("Cannot recover browser launch while live sessions exist")
            if self._open_in_flight > 1:
                raise RuntimeError("Cannot recover browser launch while live sessions exist")
        else:
            if self._has_live_sessions_for_mode_locked(headless):
                raise RuntimeError("Cannot recover browser launch while live sessions exist")
            if self._open_in_flight_by_mode.get(headless, 0) > 1:
                raise RuntimeError("Cannot recover browser launch while live sessions exist")
        self._log_lifecycle(
            "browser_recover",
            outcome="retry",
            headless=headless,
            failure_category=failure_category,
            retry_count=retry_count,
        )
        if headless is None:
            # Legacy/manual path: reset everything only when no sessions are live.
            await self._close_all_browsers_locked()
            await self._stop_all_playwright_locked()
            await self._stop_virtual_display_locked()
            return
        # Reset only the failed mode so a headed retry does not disrupt live
        # headless sessions, or vice versa.
        await self._stop_playwright_locked(headless=headless)
        if not headless:
            await self._stop_virtual_display_locked()

    def _classify_launch_failure(self, exc: Exception, *, phase: str) -> str | None:
        if phase not in {"browser_launch", "persistent_launch"}:
            return None
        if type(exc).__name__ in {"AssertionError", "ValueError", "TypeError"}:
            return None
        return "display_bootstrap"

    def _log_lifecycle(
        self,
        action: str,
        *,
        outcome: str,
        session: BrowserSession | None = None,
        headless: bool | None = None,
        display: str | None = None,
        failure_category: str | None = None,
        retry_count: int | None = None,
    ) -> None:
        extra_data: dict[str, Any] = {
            "action": action,
            "outcome": outcome,
            "runtime_generation": self._runtime_generation,
            "playwright_display": self._playwright_display,
            "playwright_displays": {
                "headless": self._playwright_displays.get(True),
                "headed": self._playwright_displays.get(False),
            },
            "playwright_runtime_count": len(self._playwrights),
            "xvfb_managed": self._xvfb_process is not None,
            "active_session_count": len(self._sessions),
            "headed_session_count": sum(1 for item in self._sessions.values() if not item.headless),
            "engine": self.engine,
            "runtime": self.runtime,
            "channel": self.channel,
            "stealth_enabled": self.stealth_enabled,
            "auto_consent": self.auto_consent,
            "humanize_input": self.humanize_input,
            "humanize_intensity": self.humanize_intensity,
            "fingerprint_hardening": self.fingerprint_hardening,
        }
        if session is not None:
            extra_data.update(
                {
                    "session_id": session.session_id,
                    "profile_mode": session.profile_mode,
                    "headless": session.headless,
                    "display": session.display,
                }
            )
        else:
            if headless is not None:
                extra_data["headless"] = headless
            if display is not None:
                extra_data["display"] = display
        if failure_category is not None:
            extra_data["failure_category"] = failure_category
        if retry_count is not None:
            extra_data["retry_count"] = retry_count
        logger.info("browser: lifecycle", extra={"extra_data": extra_data})

    async def _attach_session_observers(self, session: BrowserSession) -> None:
        def _push_console(event: dict[str, Any]) -> None:
            session.console_events.append(event)
            del session.console_events[:-BROWSER_DIAGNOSTIC_EVENT_LIMIT]

        def _push_network(event: dict[str, Any]) -> None:
            session.network_events.append(event)
            del session.network_events[:-BROWSER_DIAGNOSTIC_EVENT_LIMIT]

        page = session.page

        def _attach_page_observers(observed_page: Any) -> None:
            if not hasattr(observed_page, "on"):
                return
            observed_page.on("console", _console_listener)
            observed_page.on("pageerror", _page_error_listener)
            observed_page.on("request", _request_listener)
            observed_page.on("response", _response_listener)
            observed_page.on("requestfailed", _request_failed_listener)

        def _console_listener(message: Any) -> None:
            _push_console(
                {
                    "type": "console",
                    "level": getattr(message, "type", "log"),
                    "text": getattr(message, "text", ""),
                    "timestamp": datetime.now(UTC).isoformat(),
                }
            )

        def _page_error_listener(error: Exception) -> None:
            _push_console(
                {
                    "type": "pageerror",
                    "level": "error",
                    "text": str(error),
                    "timestamp": datetime.now(UTC).isoformat(),
                }
            )

        def _request_listener(request: Any) -> None:
            _push_network(
                {
                    "phase": "request",
                    "url": request.url,
                    "method": request.method,
                    "resource_type": request.resource_type,
                    "timestamp": datetime.now(UTC).isoformat(),
                }
            )

        def _response_listener(response: Any) -> None:
            request = response.request
            self._bump_session_activity(session)
            _push_network(
                {
                    "phase": "response",
                    "url": response.url,
                    "method": request.method,
                    "resource_type": request.resource_type,
                    "status": response.status,
                    "ok": response.ok,
                    "timestamp": datetime.now(UTC).isoformat(),
                }
            )

        def _request_failed_listener(request: Any) -> None:
            failure = request.failure
            _push_network(
                {
                    "phase": "request_failed",
                    "url": request.url,
                    "method": request.method,
                    "resource_type": request.resource_type,
                    "failure": failure.get("errorText") if isinstance(failure, dict) else None,
                    "timestamp": datetime.now(UTC).isoformat(),
                }
            )

        _attach_page_observers(page)
        if hasattr(session.context, "on"):
            session.context.on("page", _attach_page_observers)
