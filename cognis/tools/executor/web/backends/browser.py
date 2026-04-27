"""Browser-based fetch backend.

When the direct backend fails on Cloudflare-protected, JS-required, or
otherwise hostile sites, the browser backend uses the executor's existing
:class:`~cognis.tools.executor.browser.manager.BrowserManager` to render
the page properly. The Stage A-C stealth stack (patchright runtime,
playwright-stealth evasions, autoconsent, humanizer, fingerprint
hardening) automatically applies to fetches routed through this backend.

This backend is *not* a stand-in for the explicit ``browser_open`` tool.
It opens a short-lived ephemeral session, navigates once, extracts the
page text/HTML, and closes the session. Concurrent fetches share the
same per-executor browser session pool via the ``wait_for_slot=True``
opt-in on ``BrowserManager.open_session``.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

from cognis.models.tool import ToolResult
from cognis.tools.executor.browser.manager import BrowserManager
from cognis.tools.executor.web.headers import (
    clamp_timeout,
    sanitise_url,
    truncate_content,
)

logger = logging.getLogger(__name__)


class BrowserFetchBackend:
    """Implements ``WebFetchBackend`` using the executor BrowserManager."""

    def __init__(
        self,
        manager: BrowserManager,
        *,
        wait_timeout_seconds: float = 30.0,
        session_idle_seconds: float = 60.0,
        headed: bool = False,
    ) -> None:
        self._manager = manager
        self._wait_timeout_seconds = wait_timeout_seconds
        self._session_idle_seconds = session_idle_seconds
        self._headed = bool(headed)

    @property
    def manager(self) -> BrowserManager:
        return self._manager

    @property
    def headed(self) -> bool:
        return self._headed

    @property
    def mode(self) -> str:
        return "headed" if self._headed else "headless"

    async def fetch(
        self,
        url: str,
        *,
        output_format: str = "markdown",
        timeout: int = 30,
        options: dict[str, Any] | None = None,
    ) -> ToolResult:
        options = options or {}
        timeout = clamp_timeout(timeout)
        try:
            url = sanitise_url(url)
        except ValueError as exc:
            return ToolResult(output=str(exc), is_error=True)

        session_id = f"web-fetch-{uuid.uuid4().hex[:12]}"
        try:
            session = await self._manager.open_session(
                session_id=session_id,
                url=url,
                headless=not self._headed,
                profile_mode="ephemeral",
                wait_for_slot=True,
                wait_timeout_seconds=self._wait_timeout_seconds,
            )
        except TimeoutError:
            return ToolResult(
                output=(
                    "Browser fetch timed out waiting for a session slot. "
                    f"All {self._manager.max_sessions} browser sessions are busy. "
                    "Retry shortly or raise web.concurrency.browser_cap."
                ),
                is_error=True,
                metadata={
                    "browser_pool_timeout": True,
                    "browser_fetch_mode": self.mode,
                },
            )
        except RuntimeError as exc:
            return ToolResult(
                output=f"Browser runtime unavailable ({self.mode}): {exc}",
                is_error=True,
                metadata={"browser_fetch_mode": self.mode},
            )
        except Exception as exc:
            logger.warning(
                "web: browser fetch open_session failed (%s, mode=%s)",
                type(exc).__name__,
                self.mode,
            )
            return ToolResult(
                output=f"Browser fetch failed to open session ({self.mode}): {exc}",
                is_error=True,
                metadata={"browser_fetch_mode": self.mode},
            )

        try:
            try:
                final_url = str(getattr(session.page, "url", "") or url)
                content, metadata = await self._extract(
                    session,
                    output_format=output_format,
                    url=final_url,
                    options=options,
                )
            except Exception as exc:
                logger.warning(
                    "web: browser fetch extraction failed (%s, mode=%s)",
                    type(exc).__name__,
                    self.mode,
                )
                return ToolResult(
                    output=f"Browser fetch failed ({self.mode}): {exc}",
                    is_error=True,
                    metadata={"browser_fetch_mode": self.mode},
                )
        finally:
            try:
                await self._manager.close_session(session_id)
            except Exception:  # pragma: no cover - defensive
                logger.debug("web: browser session cleanup failed", exc_info=True)

        return ToolResult(
            output=content,
            metadata={"browser_fetch": True, "browser_fetch_mode": self.mode, **metadata},
        )

    async def _extract(
        self,
        session: Any,
        *,
        output_format: str,
        url: str,
        options: dict[str, Any],
    ) -> tuple[str, dict[str, Any]]:
        page = session.page
        # Always ask for the rendered HTML — browser fetch's value is JS
        # execution. We can downgrade to text/markdown via the existing
        # extractor (trafilatura) which produces cleaner output than
        # ``innerText`` for content extraction.
        html = await page.content()
        html = truncate_content(html)
        from cognis.tools.executor.web.extraction import extract_document

        document = extract_document(
            html,
            url=url,
            output_format=output_format,
            options=options,
        )
        return document.content, {"extracted_document": document.as_dict()}
