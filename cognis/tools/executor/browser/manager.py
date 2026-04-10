"""Executor-local Playwright browser manager."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from cognis.logging import get_logger
from cognis.tools.executor.browser.install import ensure_playwright_browser

logger = get_logger(__name__)

BROWSER_MANAGER_KEY = "browser_manager"
BROWSER_DEFAULT_IDLE_TIMEOUT_SECONDS = 600
BROWSER_DEFAULT_MAX_SESSIONS = 4


@dataclass
class BrowserSession:
    session_id: str
    context: Any
    page: Any
    auth_origin: str | None = None
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
    ) -> None:
        self.enabled = enabled
        self.auto_install = auto_install
        self.engine = engine
        self.headed_allowed = headed_allowed
        self.max_sessions = max_sessions
        self.idle_timeout_seconds = idle_timeout_seconds
        self._browser: Any | None = None
        self._playwright: Any | None = None
        self._sessions: dict[str, BrowserSession] = {}
        self._launch_headless = True
        self._lock = asyncio.Lock()

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
            ok, reason = await ensure_playwright_browser(
                auto_install=self.auto_install, engine=self.engine
            )
            if not ok:
                raise RuntimeError(f"Browser runtime unavailable: {reason}")
            from playwright.async_api import async_playwright

            self._playwright = await async_playwright().start()
            browser_launcher = getattr(self._playwright, self.engine)
            self._browser = await browser_launcher.launch(headless=headless)
            self._launch_headless = headless

    async def open_session(
        self,
        *,
        session_id: str,
        url: str,
        headless: bool = True,
        auth_state: dict[str, Any] | None = None,
    ) -> BrowserSession:
        await self.ensure_runtime(headless=headless)
        if session_id in self._sessions:
            session = self._sessions[session_id]
            await session.page.goto(url)
            session.last_used_at = datetime.now(UTC)
            return session
        if len(self._sessions) >= self.max_sessions:
            raise RuntimeError("Browser session limit exceeded")
        context = await self._browser.new_context(storage_state=auth_state)
        page = await context.new_page()
        await page.goto(url)
        session = BrowserSession(session_id=session_id, context=context, page=page, auth_origin=url)
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

    async def storage_state(self, session_id: str) -> dict[str, Any]:
        session = self.get_session(session_id)
        return await session.context.storage_state()
