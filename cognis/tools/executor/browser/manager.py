"""Executor-local Playwright browser manager."""

from __future__ import annotations

import asyncio
import os
import re
import shutil
import sys
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


@dataclass
class BrowserSession:
    session_id: str
    context: Any
    page: Any
    auth_origin: str | None = None
    profile_mode: str = "ephemeral"
    profile_id: str | None = None
    user_data_dir: str | None = None
    ref_map: dict[str, str] = field(default_factory=dict)
    last_used_at: datetime = field(default_factory=lambda: datetime.now(UTC))


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
        self._previous_display: str | None = None

    @property
    def active_session_count(self) -> int:
        return len(self._sessions)

    async def ensure_runtime(self, *, headless: bool = True) -> None:
        if not self.enabled:
            raise RuntimeError("Browser runtime disabled on this executor")
        if not headless and not self.headed_allowed:
            raise RuntimeError("Headed browser mode is not enabled on this executor")
        async with self._lock:
            if self._browser is not None:
                if self._launch_headless != headless:
                    raise RuntimeError(
                        "Browser runtime already initialized with a different headless mode"
                    )
                return
            await self._ensure_playwright_ready(headless=headless)
            browser_launcher = getattr(self._playwright, self.engine)
            self._browser = await browser_launcher.launch(**self._launch_kwargs(headless=headless))
            self._launch_headless = headless

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
        if session_id in self._sessions:
            session = self._sessions[session_id]
            await session.page.goto(url)
            session.last_used_at = datetime.now(UTC)
            return session
        if len(self._sessions) >= self.max_sessions:
            raise RuntimeError("Browser session limit exceeded")
        resolved_mode, resolved_profile_id = self._resolve_profile_settings(
            profile_mode=profile_mode,
            profile_id=profile_id,
            url=url,
        )
        if resolved_mode == "persistent_local":
            context, page, user_data_dir = await self._open_persistent_context(
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
            )
        else:
            await self.ensure_runtime(headless=headless)
            context = await self._browser.new_context(**self._context_kwargs(auth_state=auth_state))
            await self._apply_context_defaults(context)
            page = await context.new_page()
            await page.goto(url)
            session = BrowserSession(
                session_id=session_id,
                context=context,
                page=page,
                auth_origin=url,
                profile_mode=resolved_mode,
                profile_id=resolved_profile_id,
            )
        self._sessions[session_id] = session
        return session

    def get_session(self, session_id: str) -> BrowserSession:
        session = self._sessions.get(session_id)
        if session is None:
            raise KeyError(f"Unknown browser session: {session_id}")
        session.last_used_at = datetime.now(UTC)
        return session

    async def close_session(self, session_id: str) -> None:
        session = self._sessions.pop(session_id, None)
        if session is None:
            return
        await session.context.close()

    async def cleanup(self) -> None:
        for session_id in list(self._sessions):
            await self.close_session(session_id)
        if self._browser is not None:
            await self._browser.close()
            self._browser = None
        if self._playwright is not None:
            await self._playwright.stop()
            self._playwright = None
        await self._stop_virtual_display()

    async def storage_state(self, session_id: str) -> dict[str, Any]:
        session = self.get_session(session_id)
        return await session.context.storage_state()

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
        ok, reason = await ensure_playwright_browser(
            auto_install=self.auto_install, engine=self.engine
        )
        if not ok:
            raise RuntimeError(f"Browser runtime unavailable: {reason}")
        if self._needs_virtual_display(headless=headless):
            await self._ensure_virtual_display()
        if self._playwright is None:
            from playwright.async_api import async_playwright

            self._playwright = await async_playwright().start()

    def _needs_virtual_display(self, *, headless: bool) -> bool:
        return (
            not headless
            and self.xvfb_auto
            and sys.platform.startswith("linux")
            and not os.environ.get("DISPLAY")
        )

    async def _ensure_virtual_display(self) -> None:
        if self._xvfb_process is not None:
            return
        xvfb_binary = shutil.which("Xvfb")
        if xvfb_binary is None:
            raise RuntimeError(
                "Headed browser mode on Linux without DISPLAY requires Xvfb to be installed"
            )
        display = self._allocate_display()
        self._previous_display = os.environ.get("DISPLAY")
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
        await asyncio.sleep(0.2)
        if proc.returncode is not None:
            raise RuntimeError("Failed to start Xvfb for headed browser mode")
        os.environ["DISPLAY"] = display
        self._xvfb_process = proc
        self._xvfb_display = display
        logger.info("browser: started xvfb display", extra={"extra_data": {"display": display}})

    async def _stop_virtual_display(self) -> None:
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
            if self._previous_display is not None:
                os.environ["DISPLAY"] = self._previous_display
            else:
                os.environ.pop("DISPLAY", None)
            self._xvfb_display = None
            self._previous_display = None

    def _allocate_display(self) -> str:
        x11_dir = Path("/tmp/.X11-unix")
        for candidate in range(99, 120):
            if not (x11_dir / f"X{candidate}").exists():
                return f":{candidate}"
        return ":120"

    def _launch_kwargs(self, *, headless: bool) -> dict[str, Any]:
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
        kwargs: dict[str, Any] = {"headless": headless}
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
    ) -> tuple[Any, Any, Path]:
        if any(
            session.profile_mode == "persistent_local" and session.profile_id == profile_id
            for session in self._sessions.values()
        ):
            raise RuntimeError(f"Browser profile already in use: {profile_id}")
        await self._ensure_playwright_ready(headless=headless)
        browser_launcher = getattr(self._playwright, self.engine)
        user_data_dir = self.profile_base_dir / str(profile_id)
        user_data_dir.mkdir(parents=True, exist_ok=True)
        context = await browser_launcher.launch_persistent_context(
            str(user_data_dir),
            **self._launch_kwargs(headless=headless),
            **self._context_kwargs(),
        )
        await self._apply_context_defaults(context)
        if auth_state and isinstance(auth_state.get("cookies"), list):
            await context.add_cookies(auth_state["cookies"])
        page = context.pages[0] if context.pages else await context.new_page()
        await page.goto(url)
        return context, page, user_data_dir
