"""Executor-local Playwright browser manager."""

from __future__ import annotations

import asyncio
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
from cognis.tools.executor.browser.install import ensure_playwright_browser

logger = get_logger(__name__)

BROWSER_MANAGER_KEY = "browser_manager"
BROWSER_DEFAULT_IDLE_TIMEOUT_SECONDS = 600
BROWSER_DEFAULT_MAX_SESSIONS = 4
BROWSER_DEFAULT_PROFILE_MODE = "persistent_local"
BROWSER_DEFAULT_VIEWPORT_WIDTH = 1365
BROWSER_DEFAULT_VIEWPORT_HEIGHT = 900
BROWSER_DIAGNOSTIC_EVENT_LIMIT = 200


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
    runtime_generation: int = 0


class BrowserManager:
    """Lazy Playwright runtime for executor browser tools."""

    def __init__(
        self,
        *,
        enabled: bool = True,
        auto_install: bool = False,
        engine: str = "chromium",
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
    ) -> None:
        self.enabled = enabled
        self.auto_install = auto_install
        self.engine = engine
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
        self._browser: Any | None = None
        self._playwright: Any | None = None
        self._sessions: dict[str, BrowserSession] = {}
        self._launch_headless = True
        self._lock = asyncio.Lock()
        self._xvfb_process: asyncio.subprocess.Process | None = None
        self._xvfb_display: str | None = None
        self._claimed_displays: set[str] = set()
        self._reserved_profile_ids: set[str] = set()
        self._runtime_generation = 0
        self._playwright_display: str | None = None
        self._headed_open_in_flight = 0
        self._open_in_flight = 0

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
            if self._browser is not None and self._launch_headless == headless:
                return
            if self._browser is not None:
                if self._has_live_sessions_for_generation_locked(self._runtime_generation):
                    raise RuntimeError(
                        "Browser runtime already initialized with a different headless mode"
                    )
                await self._close_shared_browser_locked()
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
    ) -> BrowserSession:
        await self._cleanup_idle_sessions()
        async with self._lock:
            session = self._sessions.get(session_id)
            if session is not None and not self._session_is_idle(session):
                session.last_used_at = datetime.now(UTC)
        if session is not None and not self._session_is_idle(session):
            await session.page.goto(url)
            return session
        resolved_mode, resolved_profile_id = self._resolve_profile_settings(
            profile_mode=profile_mode,
            profile_id=profile_id,
            url=url,
        )
        async with self._lock:
            if len(self._sessions) + self._open_in_flight >= self.max_sessions:
                raise RuntimeError("Browser session limit exceeded")
            self._open_in_flight += 1
            if not headless:
                self._headed_open_in_flight += 1
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
                    await page.goto(url)
                except Exception:
                    await self.close_session(session_id)
                    raise
            else:
                await self.ensure_runtime(headless=headless)
                browser = self._browser
                runtime_generation = self._runtime_generation
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
                    await page.goto(url)
                except Exception:
                    await self.close_session(session_id)
                    raise
        finally:
            try:
                async with self._lock:
                    if resolved_mode == "persistent_local":
                        self._reserved_profile_ids.discard(str(resolved_profile_id))
                    self._open_in_flight -= 1
                    if (
                        self._open_in_flight == 0
                        and not self._sessions
                        and self._browser is not None
                    ):
                        await self._close_shared_browser_locked()
                    if not headless:
                        self._headed_open_in_flight -= 1
                        if (
                            self._headed_open_in_flight == 0
                            and not self._has_live_headed_sessions_locked()
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
            if not self._sessions and self._open_in_flight == 0 and self._browser is not None:
                await self._close_shared_browser_locked()
            if self._headed_open_in_flight == 0 and not self._has_live_headed_sessions_locked():
                await self._stop_virtual_display_locked()

    async def cleanup(self) -> None:
        for session_id in list(self._sessions):
            await self.close_session(session_id)
        async with self._lock:
            await self._stop_playwright_locked()
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

    async def _reserve_profile_id(self, profile_id: str) -> None:
        async with self._lock:
            if profile_id in self._reserved_profile_ids or any(
                session.profile_mode == "persistent_local" and session.profile_id == profile_id
                for session in self._sessions.values()
            ):
                raise RuntimeError(f"Browser profile already in use: {profile_id}")
            self._reserved_profile_ids.add(profile_id)

    def _session_is_idle(self, session: BrowserSession | Any) -> bool:
        if self.idle_timeout_seconds <= 0:
            return False
        last_used_at = getattr(session, "last_used_at", None)
        if last_used_at is None:
            return False
        cutoff = datetime.now(UTC).timestamp() - self.idle_timeout_seconds
        return last_used_at.timestamp() < cutoff

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
        ok, reason = await ensure_playwright_browser(
            auto_install=self.auto_install, engine=self.engine
        )
        if not ok:
            raise RuntimeError(f"Browser runtime unavailable: {reason}")
        if self._needs_virtual_display(headless=headless):
            await self._ensure_virtual_display_locked()
        display = self._active_display(headless=headless)
        if self._playwright is not None and self._playwright_display != display:
            if self._has_live_sessions_for_generation_locked(self._runtime_generation):
                raise RuntimeError("Browser runtime already active for a different display")
            await self._stop_playwright_locked()
        if self._playwright is None:
            from playwright.async_api import async_playwright

            self._playwright = await async_playwright().start()
            self._playwright_display = display
            self._runtime_generation += 1
            self._log_lifecycle(
                "playwright_start",
                outcome="success",
                display=display,
            )
        return display

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
        return kwargs

    def _context_kwargs(self, *, auth_state: dict[str, Any] | None = None) -> dict[str, Any]:
        kwargs: dict[str, Any] = {
            "viewport": {"width": self.viewport_width, "height": self.viewport_height},
            "locale": self.locale,
        }
        if self.timezone_id:
            kwargs["timezone_id"] = self.timezone_id
        if auth_state is not None:
            kwargs["storage_state"] = auth_state
        return kwargs

    async def _apply_context_defaults(self, context: Any) -> None:
        if not self.realistic_launch:
            return
        await context.add_init_script(
            """
            Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
            """
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
                browser_launcher = getattr(self._playwright, self.engine)
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
                        retry_count=retry_count,
                    )
        await self._apply_context_defaults(context)
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

    def _has_live_sessions_for_generation_locked(self, generation: int) -> bool:
        return any(session.runtime_generation == generation for session in self._sessions.values())

    async def _close_shared_browser_locked(self) -> None:
        if self._browser is None:
            return
        await self._browser.close()
        self._browser = None

    async def _stop_playwright_locked(self) -> None:
        if self._browser is not None:
            await self._close_shared_browser_locked()
        if self._playwright is not None:
            await self._playwright.stop()
            self._playwright = None
            self._log_lifecycle("playwright_stop", outcome="success")
        self._playwright_display = None

    async def _launch_shared_browser_locked(self, *, headless: bool) -> None:
        retry_count = 0
        while True:
            display = await self._ensure_playwright_ready_locked(headless=headless)
            browser_launcher = getattr(self._playwright, self.engine)
            try:
                self._browser = await browser_launcher.launch(
                    **self._launch_kwargs(headless=headless, display=display)
                )
                self._launch_headless = headless
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
                    retry_count=retry_count,
                )

    async def _recover_retryable_launch_failure_locked(
        self,
        *,
        failure_category: str,
        retry_count: int,
    ) -> None:
        if self._has_live_sessions_for_generation_locked(self._runtime_generation):
            raise RuntimeError("Cannot recover browser launch while live sessions exist")
        if self._open_in_flight > 1:
            raise RuntimeError("Cannot recover browser launch while live sessions exist")
        self._log_lifecycle(
            "browser_recover",
            outcome="retry",
            failure_category=failure_category,
            retry_count=retry_count,
        )
        await self._stop_playwright_locked()
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
            "xvfb_managed": self._xvfb_process is not None,
            "active_session_count": len(self._sessions),
            "headed_session_count": sum(1 for item in self._sessions.values() if not item.headless),
            "engine": self.engine,
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
