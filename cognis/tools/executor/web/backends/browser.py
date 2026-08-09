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

import asyncio
import hashlib
import ipaddress
import logging
import uuid
import weakref
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qsl, urlparse

from cognis.models.tool import ToolResult
from cognis.tools.executor.browser.manager import BrowserManager, BrowserSessionOwner
from cognis.tools.executor.web.headers import (
    clamp_timeout,
    sanitise_url,
    truncate_content,
)
from cognis.tools.executor.web.quality import classify_provider_error_page

logger = logging.getLogger(__name__)
_MAX_RENDERED_HTML_CHARS = 64 * 1024 * 1024


@dataclass
class _ProfileLockEntry:
    lock: asyncio.Lock
    users: int = 0


_PROFILE_LOCKS_BY_MANAGER: weakref.WeakKeyDictionary[
    BrowserManager, dict[str, _ProfileLockEntry]
] = weakref.WeakKeyDictionary()


@asynccontextmanager
async def _acquire_profile_lock(manager: BrowserManager, profile_id: str) -> Any:
    profile_locks = _PROFILE_LOCKS_BY_MANAGER.setdefault(manager, {})
    entry = profile_locks.get(profile_id)
    if entry is None:
        entry = _ProfileLockEntry(lock=asyncio.Lock())
        profile_locks[profile_id] = entry
    entry.users += 1
    try:
        async with entry.lock:
            yield
    finally:
        entry.users -= 1
        if entry.users == 0 and profile_locks.get(profile_id) is entry:
            profile_locks.pop(profile_id, None)


_CONSENT_ACCEPT_NAMES = {
    "accept",
    "accept all",
    "agree",
    "allow all",
    "continue",
    "einverstanden",
    "i agree",
    "ok",
    "pokracovat",
    "prijmout vse",
    "rozumim",
    "rozumim a souhlasim",
    "souhlasim",
}
_CONSENT_REJECT_NAMES = {
    "decline",
    "ablehnen",
    "disagree",
    "only necessary",
    "reject",
    "reject all",
    "odmitnout",
    "odmitnout vse",
    "pouze nezbytne",
}


class BrowserFetchBackend:
    """Implements ``WebFetchBackend`` using the executor BrowserManager."""

    def __init__(
        self,
        manager: BrowserManager,
        *,
        wait_timeout_seconds: float = 30.0,
        session_idle_seconds: float = 60.0,
        navigation_timeout_seconds: float = 60.0,
        wait_until: str = "domcontentloaded",
        network_idle_after_dom_seconds: float = 3.0,
        headed: bool = False,
    ) -> None:
        self._manager = manager
        self._wait_timeout_seconds = wait_timeout_seconds
        self._session_idle_seconds = session_idle_seconds
        self._navigation_timeout_seconds = navigation_timeout_seconds
        self._wait_until = wait_until
        self._network_idle_after_dom_seconds = network_idle_after_dom_seconds
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

        profile_id, owner = self._persistent_profile(url, options)
        try:
            async with asyncio.timeout(timeout):
                async with _acquire_profile_lock(self._manager, profile_id):
                    return await self._fetch_in_profile(
                        url,
                        output_format=output_format,
                        timeout=timeout,
                        options=options,
                        profile_id=profile_id,
                        owner=owner,
                    )
        except TimeoutError:
            return ToolResult(
                output=f"Browser fetch timed out after {timeout}s ({self.mode}).",
                is_error=True,
                metadata={
                    "browser_fetch_mode": self.mode,
                    "browser_failure_category": "browser_timeout",
                    "browser_profile_id": profile_id,
                },
            )

    def _persistent_profile(
        self,
        url: str,
        options: dict[str, Any],
    ) -> tuple[str, BrowserSessionOwner]:
        raw_owner = options.get("_browser_profile_owner")
        owner_data = raw_owner if isinstance(raw_owner, dict) else {}
        user_email = str(owner_data.get("user_email") or "anonymous").strip().lower()
        execution_scope_id = str(owner_data.get("execution_scope_id") or "web-fetch").strip()
        hostname = (urlparse(url).hostname or "unknown").lower()
        digest = hashlib.sha256(f"{user_email}\0{hostname}".encode()).hexdigest()[:32]
        return (
            f"web-fetch-{digest}",
            BrowserSessionOwner(
                execution_scope_id=execution_scope_id,
                user_email=user_email,
            ),
        )

    async def _fetch_in_profile(
        self,
        url: str,
        *,
        output_format: str,
        timeout: int,
        options: dict[str, Any],
        profile_id: str,
        owner: BrowserSessionOwner,
    ) -> ToolResult:
        session_id = f"web-fetch-{uuid.uuid4().hex[:12]}"
        try:
            session = await self._manager.open_session(
                session_id=session_id,
                url=url,
                headless=not self._headed,
                profile_mode="persistent_local",
                profile_id=profile_id,
                wait_for_slot=True,
                wait_timeout_seconds=self._wait_timeout_seconds,
                lifecycle="ephemeral",
                session_idle_seconds=self._session_idle_seconds,
                navigation_timeout_seconds=self._navigation_timeout_seconds,
                wait_until=self._wait_until,
                network_idle_after_dom_seconds=self._network_idle_after_dom_seconds,
                # Browser fetch owns consent handling so it can persist and verify
                # the result. The generic init bundle can misidentify policy links.
                browser_settings={"auto_consent": "off"},
                owner=owner,
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
            logger.warning(
                "web: browser runtime unavailable (%s, mode=%s)",
                type(exc).__name__,
                self.mode,
            )
            return ToolResult(
                output=f"Browser runtime unavailable ({self.mode}).",
                is_error=True,
                metadata={
                    "browser_fetch_mode": self.mode,
                    "browser_failure_category": "browser_runtime_unavailable",
                },
            )
        except Exception as exc:
            logger.warning(
                "web: browser fetch open_session failed (%s, mode=%s)",
                type(exc).__name__,
                self.mode,
            )
            return ToolResult(
                output=f"Browser fetch failed to open session ({self.mode}).",
                is_error=True,
                metadata={
                    "browser_fetch_mode": self.mode,
                    "browser_failure_category": "browser_initialization_failed",
                },
            )

        try:
            try:
                initial_consent_redirect = _looks_like_consent_redirect(
                    requested_url=url,
                    final_url=str(getattr(session.page, "url", "") or ""),
                )
                recovered_consent_redirect = await self._recover_consent_redirect(
                    session,
                    requested_url=url,
                )
                if initial_consent_redirect and not recovered_consent_redirect:
                    return ToolResult(
                        output="Browser consent redirect could not return to the requested page.",
                        is_error=True,
                        metadata={
                            "browser_fetch_mode": self.mode,
                            "browser_failure_category": "consent_redirect",
                            "browser_profile_persistent": True,
                            "browser_profile_id": profile_id,
                            "requested_url": url,
                            "final_url": str(getattr(session.page, "url", "") or ""),
                        },
                    )
                recovered_inline_consent = False
                if not recovered_consent_redirect:
                    recovered_inline_consent = await self._recover_inline_consent(session.page)
                if recovered_consent_redirect:
                    await self._wait_for_browser_unblock(session.page, requested_url=url)
                challenge_attempted, challenge_resolved = await self._resolve_browser_challenge(
                    session,
                    requested_url=url,
                )
                navigation_error = (
                    None
                    if challenge_resolved
                    else self._navigation_http_error(session, requested_url=url)
                )
                if navigation_error is not None:
                    navigation_error.metadata = {
                        **(navigation_error.metadata or {}),
                        "browser_profile_persistent": True,
                        "browser_profile_id": profile_id,
                        "browser_challenge_attempted": challenge_attempted,
                        "browser_challenge_resolved": challenge_resolved,
                    }
                    return navigation_error
                await self._wait_for_rendered_body(session.page)
                final_url = str(getattr(session.page, "url", "") or url)
                content, metadata = await self._extract(
                    session,
                    output_format=output_format,
                    url=final_url,
                    requested_url=url,
                    options=options,
                )
                if recovered_consent_redirect:
                    extracted = metadata.get("extracted_document")
                    if isinstance(extracted, dict):
                        extracted["browser_consent_redirect_recovered"] = True
                        extracted["requested_url"] = url
                if recovered_inline_consent:
                    extracted = metadata.get("extracted_document")
                    if isinstance(extracted, dict):
                        extracted["browser_inline_consent_recovered"] = True
                metadata["browser_profile_persistent"] = True
                metadata["browser_profile_id"] = profile_id
                metadata["browser_challenge_attempted"] = challenge_attempted
                metadata["browser_challenge_resolved"] = challenge_resolved
                extracted = metadata.get("extracted_document")
                block_signal = (
                    extracted.get("browser_block_signal") if isinstance(extracted, dict) else None
                )
                if isinstance(block_signal, str) and block_signal:
                    return ToolResult(
                        output=(
                            "Browser fetch loaded a blocked or provider-generated error page "
                            f"({block_signal}, {self.mode})."
                        ),
                        is_error=True,
                        metadata={
                            "browser_fetch": True,
                            "browser_fetch_mode": self.mode,
                            "browser_block_signal": block_signal,
                            **metadata,
                        },
                    )
            except Exception as exc:
                logger.warning(
                    "web: browser fetch extraction failed (%s, mode=%s)",
                    type(exc).__name__,
                    self.mode,
                )
                return ToolResult(
                    output=f"Browser fetch failed during extraction ({self.mode}).",
                    is_error=True,
                    metadata={
                        "browser_fetch_mode": self.mode,
                        "browser_failure_category": "browser_extraction_failed",
                    },
                )
        finally:
            try:
                await self._manager.close_session(session_id, owner=owner)
            except Exception:  # pragma: no cover - defensive
                logger.debug("web: browser session cleanup failed", exc_info=True)

        return ToolResult(
            output=content,
            metadata={"browser_fetch": True, "browser_fetch_mode": self.mode, **metadata},
        )

    def _navigation_http_error(
        self,
        session: Any,
        *,
        requested_url: str,
    ) -> ToolResult | None:
        navigation_status = getattr(session, "navigation_status", None)
        if not isinstance(navigation_status, int) or navigation_status < 400:
            return None
        navigation_url = str(getattr(session, "navigation_url", None) or requested_url)
        if _is_internal_browser_url(navigation_url):
            navigation_url = requested_url
        return ToolResult(
            output=(
                f"Browser fetch received HTTP {navigation_status} "
                f"for {navigation_url} "
                f"({self.mode})."
            ),
            is_error=True,
            metadata={
                "browser_fetch": True,
                "browser_fetch_mode": self.mode,
                "browser_block_signal": f"http_status_{navigation_status}",
                "browser_navigation_status": navigation_status,
                "browser_failure_category": "browser_navigation_failed",
            },
        )

    async def _extract(
        self,
        session: Any,
        *,
        output_format: str,
        url: str,
        requested_url: str,
        options: dict[str, Any],
    ) -> tuple[str, dict[str, Any]]:
        page = session.page
        # Always ask for the rendered HTML — browser fetch's value is JS
        # execution. We can downgrade to text/markdown via the existing
        # extractor (trafilatura) which produces cleaner output than
        # ``innerText`` for content extraction.
        html = await page.content()
        source_truncated = len(html) > _MAX_RENDERED_HTML_CHARS
        html = truncate_content(html)
        from cognis.tools.executor.web.extraction_process import extract_document_in_process

        extraction_payload = await extract_document_in_process(
            html,
            url=url,
            output_format=output_format,
            options={**options, "requested_url": requested_url},
            timeout=30.0,
        )
        document_data = extraction_payload.get("document") or {}
        content = str(extraction_payload.get("content") or "")
        document_data["source_truncated"] = source_truncated
        if source_truncated:
            quality = document_data.get("semantic_quality")
            if isinstance(quality, dict) and quality.get("status") == "complete":
                quality["status"] = "partial"
                quality["label"] = "partial"
                quality["rank"] = 3
                signals = quality.get("signals")
                quality["signals"] = [
                    *(signals if isinstance(signals, list) else []),
                    "source_truncated",
                ]
        document_data["browser_fetch_mode"] = self.mode
        block_signal = _classify_browser_extract_quality(
            document_data,
            content,
            requested_url=requested_url,
            final_url=url,
        )
        if block_signal:
            document_data["browser_block_signal"] = block_signal
            document_data["extraction_status"] = f"blocked_or_empty:{block_signal}"
        else:
            document_data["extraction_status"] = "ok"
        return content, {"extracted_document": document_data}

    async def _wait_for_rendered_body(self, page: Any) -> None:
        """Give client-rendered shells a short chance to hydrate before extraction."""
        try:
            await page.wait_for_function(
                """
                () => {
                  const body = document.body;
                  if (!body) return false;
                  const text = (body.innerText || body.textContent || '').trim();
                  const html = body.innerHTML || '';
                  const main = document.querySelector(
                    'article, main, [role="main"], [data-component-name*="article"], ' +
                    '[data-testid*="article"], [class*="article-body"], [class*="story-body"]'
                  );
                  const listingCards = document.querySelectorAll(
                    '[itemtype*="schema.org/Product"], [itemtype*="schema.org/Vehicle"], ' +
                    '[data-testid*="product-card"], [data-testid*="listing-card"], ' +
                    '[class*="product-card"], [class*="listing-card"], [class*="vehicle-card"], ' +
                    '[class~="c-product"], [class*="listing-card__container"]'
                  );
                  const appState = document.querySelector('#__NEXT_DATA__, #__NUXT_DATA__');
                  const structuredProduct = Array.from(
                    document.querySelectorAll('script[type="application/ld+json"]')
                  ).some(node => /"(?:Product|Vehicle|Car|ItemList|Offer)"/i.test(node.textContent || ''));
                  const mainText = main ? (main.innerText || main.textContent || '').trim() : '';
                  return listingCards.length >= 2 || structuredProduct || !!appState ||
                    mainText.length >= 500 || text.length >= 1500 || html.length >= 10000;
                }
                """,
                timeout=10000,
            )
        except Exception:
            # Some pages are genuinely thin or block wait_for_function; extraction
            # quality classification handles the remaining failure mode.
            return

    async def _resolve_browser_challenge(
        self,
        session: Any,
        *,
        requested_url: str,
    ) -> tuple[bool, bool]:
        signal = await self._page_block_signal(session.page, requested_url=requested_url)
        navigation_status = getattr(session, "navigation_status", None)
        challenge_status = navigation_status in {401, 403, 429}
        if signal != "interstitial" and not challenge_status:
            return False, False
        if await self._wait_for_browser_unblock(
            session.page,
            requested_url=requested_url,
            session=session,
            require_success_status=challenge_status,
        ):
            return True, True
        try:
            response = await session.page.goto(
                requested_url,
                timeout=int(self._navigation_timeout_seconds * 1000),
                wait_until=self._wait_until,
            )
            if response is not None:
                self._manager.record_navigation_response(
                    session,
                    response,
                    requested_url=requested_url,
                )
            elif challenge_status:
                session.navigation_status = navigation_status
        except Exception:
            return True, False
        return (
            True,
            await self._wait_for_browser_unblock(
                session.page,
                requested_url=requested_url,
                session=session,
                require_success_status=challenge_status,
            ),
        )

    async def _wait_for_browser_unblock(
        self,
        page: Any,
        *,
        requested_url: str,
        session: Any | None = None,
        require_success_status: bool = False,
        timeout_seconds: float = 10.0,
    ) -> bool:
        deadline = asyncio.get_running_loop().time() + timeout_seconds
        while asyncio.get_running_loop().time() < deadline:
            signal = await self._page_block_signal(page, requested_url=requested_url)
            status = getattr(session, "navigation_status", None) if session is not None else None
            status_ready = (
                isinstance(status, int) and status < 400
                if require_success_status
                else status not in {401, 403, 429}
            )
            if not signal and status_ready:
                return True
            await asyncio.sleep(0.5)
        signal = await self._page_block_signal(page, requested_url=requested_url)
        status = getattr(session, "navigation_status", None) if session is not None else None
        status_ready = (
            isinstance(status, int) and status < 400
            if require_success_status
            else status not in {401, 403, 429}
        )
        return not signal and status_ready

    async def _page_block_signal(self, page: Any, *, requested_url: str) -> str | None:
        try:
            snapshot = await page.evaluate(
                """
                () => ({
                  title: document.title || '',
                  text: (document.body?.innerText || document.body?.textContent || '').slice(0, 50000),
                  html: (document.documentElement?.outerHTML || '').slice(0, 100000)
                })
                """
            )
        except Exception:
            return None
        if not isinstance(snapshot, dict):
            return None
        content = str(snapshot.get("text") or "")
        document = {
            "title": str(snapshot.get("title") or ""),
            "url": str(getattr(page, "url", "") or requested_url),
            "extractor": "browser_probe",
            "score": 0,
        }
        return _classify_browser_extract_quality(
            document,
            content,
            requested_url=requested_url,
            final_url=document["url"],
        )

    async def _recover_consent_redirect(self, session: Any, *, requested_url: str) -> bool:
        page = session.page
        final_url = str(getattr(page, "url", "") or "")
        if not _looks_like_consent_redirect(requested_url=requested_url, final_url=final_url):
            return False
        if self._consent_disabled(requested_url) or self._consent_disabled(final_url):
            return False
        for _attempt in range(2):
            current_url = str(getattr(page, "url", "") or "")
            if _looks_like_consent_redirect(
                requested_url=requested_url,
                final_url=current_url,
            ):
                clicked = await self._click_consent_action(page, allow_accessibility=True)
                if not clicked:
                    return False
                with suppress(Exception):
                    await page.wait_for_timeout(1000)
                    await page.wait_for_load_state("networkidle", timeout=5000)
                current_url = str(getattr(page, "url", "") or "")
            if _consent_navigation_matches(requested_url, current_url):
                return True
            try:
                response = await page.goto(
                    requested_url,
                    timeout=int(self._navigation_timeout_seconds * 1000),
                    wait_until=self._wait_until,
                )
                self._manager.record_navigation_response(
                    session,
                    response,
                    requested_url=requested_url,
                )
                if self._network_idle_after_dom_seconds > 0 and self._wait_until != "networkidle":
                    with suppress(Exception):
                        await page.wait_for_load_state(
                            "networkidle",
                            timeout=int(self._network_idle_after_dom_seconds * 1000),
                        )
            except Exception:
                return False
            if _consent_navigation_matches(
                requested_url,
                str(getattr(page, "url", "") or ""),
            ):
                return True
        return False

    async def _recover_inline_consent(self, page: Any) -> bool:
        if self._consent_disabled(str(getattr(page, "url", "") or "")):
            return False
        if not await self._page_has_consent_context(page):
            return False
        clicked = await self._click_consent_action(
            page,
            timeout_seconds=2.0,
            allow_accessibility=False,
        )
        if not clicked:
            return False
        with suppress(Exception):
            await page.wait_for_timeout(750)
            await page.wait_for_load_state("networkidle", timeout=5000)
        return True

    async def _page_has_consent_context(self, page: Any) -> bool:
        try:
            scope_texts = await page.evaluate(
                """
                () => {
                  const selector = [
                    'dialog', '[role="dialog"]', '[aria-modal="true"]',
                    '[id*="consent" i]', '[class*="consent" i]',
                    '[id*="cookie" i]', '[class*="cookie" i]',
                    '[id*="cmp" i]', '[class*="cmp" i]'
                  ].join(',');
                  return Array.from(document.querySelectorAll(selector))
                    .filter((scope) => {
                      const style = getComputedStyle(scope);
                      const rect = scope.getBoundingClientRect();
                      return style.display !== 'none' && style.visibility !== 'hidden'
                        && rect.width > 0 && rect.height > 0;
                    })
                    .map((scope) => ({
                      text: (scope.innerText || scope.textContent || '').slice(0, 10000),
                      strongScope: /(?:consent|cookie|cmp)/i.test(
                        [scope.id || '', scope.className || ''].join(' ')
                      )
                    }));
                }
                """
            )
        except Exception:
            return False
        if not isinstance(scope_texts, list):
            return False
        strong_context_markers = (
            "cookie",
            "cookies",
            "tracking",
            "personalized advertising",
            "personalised advertising",
            "personalisierte werbung",
        )
        action_markers = (
            *_CONSENT_ACCEPT_NAMES,
            *_CONSENT_REJECT_NAMES,
        )
        return any(
            (
                bool(scope.get("strongScope"))
                or any(marker in normalized for marker in strong_context_markers)
            )
            and any(marker in normalized for marker in action_markers)
            for scope, normalized in (
                (scope, _normalize_for_matching(str(scope.get("text") or "")))
                for scope in scope_texts
                if isinstance(scope, dict)
            )
        )

    async def _click_consent_action(
        self,
        page: Any,
        *,
        timeout_seconds: float = 8.0,
        allow_accessibility: bool,
    ) -> bool:
        action = str(getattr(self._manager, "auto_consent", "accept") or "accept")
        if action == "off":
            return False
        deadline = asyncio.get_running_loop().time() + timeout_seconds
        while asyncio.get_running_loop().time() < deadline:
            if action in {"accept", "reject"}:
                try:
                    if await page.evaluate(_CONSENT_REDIRECT_CLICK_SCRIPT, action):
                        return True
                except Exception:
                    pass
            if allow_accessibility and await self._click_accessible_consent(page):
                return True
            await asyncio.sleep(0.5)
        return False

    def _consent_disabled(self, url: str) -> bool:
        hostname = (urlparse(url).hostname or "").lower()
        disabled = getattr(self._manager, "auto_consent_disabled_domains", ())
        return any(
            hostname == str(domain).lower() or hostname.endswith(f".{str(domain).lower()}")
            for domain in disabled
            if str(domain).strip()
        )

    async def _click_accessible_consent(self, page: Any) -> bool:
        """Click consent controls hidden inside closed shadow roots via Chrome's AX tree."""
        context = getattr(page, "context", None)
        if context is None or not hasattr(context, "new_cdp_session"):
            return False
        client = None
        try:
            client = await context.new_cdp_session(page)
            tree = await client.send("Accessibility.getFullAXTree")
            nodes = tree.get("nodes") if isinstance(tree, dict) else None
            if not isinstance(nodes, list):
                return False
            action = str(getattr(self._manager, "auto_consent", "accept") or "accept")
            accepted_names = _CONSENT_REJECT_NAMES if action == "reject" else _CONSENT_ACCEPT_NAMES
            for node in nodes:
                if not isinstance(node, dict):
                    continue
                role = node.get("role")
                name = node.get("name")
                role_value = role.get("value") if isinstance(role, dict) else None
                name_value = name.get("value") if isinstance(name, dict) else None
                normalized = _normalize_for_matching(str(name_value or ""))
                if role_value not in {"button", "link"} or normalized not in accepted_names:
                    continue
                backend_node_id = node.get("backendDOMNodeId")
                if not isinstance(backend_node_id, int):
                    continue
                box = await client.send(
                    "DOM.getBoxModel",
                    {"backendNodeId": backend_node_id},
                )
                model = box.get("model") if isinstance(box, dict) else None
                quad = model.get("content") if isinstance(model, dict) else None
                if not isinstance(quad, list) or len(quad) < 8:
                    continue
                x = sum(float(quad[index]) for index in (0, 2, 4, 6)) / 4
                y = sum(float(quad[index]) for index in (1, 3, 5, 7)) / 4
                await page.mouse.move(x, y, steps=8)
                await page.mouse.click(x, y)
                return True
        except Exception:
            return False
        finally:
            if client is not None:
                with suppress(Exception):
                    await client.detach()
        return False


def _classify_browser_extract_quality(
    document: dict[str, Any],
    content: str,
    *,
    requested_url: str | None = None,
    final_url: str | None = None,
) -> str | None:
    """Classify browser-rendered pages that loaded but did not yield usable content."""
    signals = _collect_page_signals(
        document,
        content,
        requested_url=requested_url,
        final_url=final_url or str(document.get("url") or ""),
    )
    if signals.consent_redirect_score >= 2:
        return "consent_redirect"

    provider_error = classify_provider_error_page(document, content)
    if provider_error:
        return provider_error

    # Real page evidence wins over provider asset noise. Modern commerce pages
    # commonly include Turnstile/CMP scripts even when they are not blocking us.
    if signals.real_page_score >= 3 and signals.challenge_score < 3:
        return None

    if signals.challenge_score >= 3:
        return "interstitial"

    if signals.consent_score >= 3 and signals.real_page_score < 3:
        return "consent_interstitial"

    if signals.empty_score >= 2:
        return "empty_extraction"

    if signals.thin_block_score >= 2:
        return "thin_block_page"
    return None


def _is_internal_browser_url(value: str) -> bool:
    host = (urlparse(value).hostname or "").lower()
    if not host:
        return False
    try:
        return not ipaddress.ip_address(host).is_global
    except ValueError:
        return (
            host.endswith((".internal", ".local", ".localhost"))
            or host == "localhost"
            or "." not in host
        )


@dataclass(frozen=True)
class _PageSignals:
    real_page_score: int
    challenge_score: int
    consent_score: int
    consent_redirect_score: int
    empty_score: int
    thin_block_score: int


def _collect_page_signals(
    document: dict[str, Any],
    content: str,
    *,
    requested_url: str | None,
    final_url: str | None,
) -> _PageSignals:
    normalized = " ".join((content or "").lower().split())
    normalized_ascii = _normalize_for_matching(content)
    extractor = str(document.get("extractor") or "").lower()
    score = document.get("extraction_score")
    score_float = float(score) if isinstance(score, int | float) else 0.0
    title = str(document.get("title") or "").strip().lower()
    title_ascii = _normalize_for_matching(title)
    final_url_value = final_url or str(document.get("url") or "")

    real_page_score = _real_page_score(
        document,
        normalized=normalized,
        normalized_ascii=normalized_ascii,
        requested_url=requested_url,
        final_url=final_url_value,
        score_float=score_float,
        title=title,
    )
    challenge_score = _challenge_score(
        normalized=normalized,
        score_float=score_float,
        title_ascii=title_ascii,
        real_page_score=real_page_score,
    )
    consent_score = _consent_score(normalized_ascii)
    consent_redirect_score = _consent_redirect_score(
        requested_url=requested_url,
        final_url=final_url_value,
        content=content,
    )
    empty_score = int(extractor == "empty") + int(score_float <= 0) + int(len(normalized) < 80)
    thin_block_titles = {"reuters.com", "just a moment", "access denied"}
    thin_block_score = int(title_ascii in thin_block_titles) + int(len(normalized) < 500)
    return _PageSignals(
        real_page_score=real_page_score,
        challenge_score=challenge_score,
        consent_score=consent_score,
        consent_redirect_score=consent_redirect_score,
        empty_score=empty_score,
        thin_block_score=thin_block_score,
    )


def _real_page_score(
    document: dict[str, Any],
    *,
    normalized: str,
    normalized_ascii: str,
    requested_url: str | None,
    final_url: str | None,
    score_float: float,
    title: str,
) -> int:
    score = 0
    if title and title not in {"just a moment...", "access denied", "reuters.com"}:
        score += 1
    description = str(document.get("description") or "").strip()
    if len(description) >= 40:
        score += 1
    if _urls_match_materially(requested_url, final_url or str(document.get("url") or "")):
        score += 1
    if score_float >= 500:
        score += 1
    if len(normalized) >= 2000:
        score += 1
    if any(
        marker in normalized_ascii
        for marker in ("schema org", "type product", "type article", "type webpage")
    ):
        score += 2
    if any(marker in normalized for marker in ('"@type":"product"', '"@type": "product"')):
        score += 2
    if any(
        marker in normalized
        for marker in ('property="og:title"', "property='og:title'", 'property="og:description"')
    ):
        score += 1
    if any(marker in normalized for marker in ("<h1", "<article", "<main")):
        score += 1
    return score


def _challenge_score(
    *,
    normalized: str,
    score_float: float,
    title_ascii: str,
    real_page_score: int,
) -> int:
    score = 0
    if title_ascii in {"just a moment", "access denied"}:
        score += 3
    hard_markers = (
        "verify you are human",
        "checking your browser",
        "enable javascript and cookies",
        "access denied",
        "are you a robot",
        "captcha-delivery.com",
        "datadome captcha",
        "geo.captcha-delivery.com",
        "you've been blocked by network security",
        "you have been blocked by network security",
        "please wait for verification",
    )
    hard_marker_hits = {marker for marker in hard_markers if marker in normalized}
    denial_context_markers = (
        "your request was rejected",
        "request has been rejected",
        "disable vpn",
        "disable your vpn",
        "proxy and retry",
        "reference id",
        "incident id",
    )
    access_denied_is_blocking = "access denied" in hard_marker_hits and any(
        marker in normalized for marker in denial_context_markers
    )
    hard_marker_hit = bool(hard_marker_hits - {"access denied"}) or access_denied_is_blocking
    if hard_marker_hit:
        score += 3
    contextual_markers = (
        "cf-challenge",
        "cf challenge",
        "challenge-platform",
        "challenge platform",
        "checking if the site connection is secure",
        "needs to review the security of your connection",
        "complete the security check",
        "prove you are human",
        "ray id",
        "js_challenge=1",
    )
    if any(marker in normalized for marker in contextual_markers):
        score += 2
    provider_assets = (
        "cloudflare",
        "turnstile",
        "cf turnstile",
        "cf-turnstile",
        "challenges.cloudflare.com",
        "captcha",
    )
    if any(marker in normalized for marker in provider_assets):
        score += 1
    if score_float <= 0 and len(normalized) < 2000:
        score += 1
    if real_page_score >= 3 and not hard_marker_hit:
        score -= 2
    return max(score, 3 if hard_marker_hit else 0)


def _consent_score(normalized_ascii: str) -> int:
    consent_markers = (
        "cookie",
        "cookies",
        "consent",
        "gdpr",
        "privacy",
        "souhlas",
        "soubory cookie",
        "pouze nezbytn",
        "rozumim a souhlasim",
        "sukromi",
        "ciasteczka",
        "zgoda",
        "prywatnosc",
        "datenschutz",
        "einwilligung",
        "confidentialite",
        "donnees personnelles",
        "privacidad",
        "datos personales",
        "privacidade",
        "dados pessoais",
        "dati personali",
        "rifiuta tutto",
    )
    if len(normalized_ascii) < 500:
        return 0
    return sum(1 for marker in consent_markers if marker in normalized_ascii)


def _consent_redirect_score(
    *,
    requested_url: str | None,
    final_url: str | None,
    content: str | None,
) -> int:
    if not _looks_like_consent_redirect(
        requested_url=requested_url,
        final_url=final_url,
        content=content,
    ):
        return 0
    requested_host = _host(requested_url)
    final_host = _host(final_url)
    return 3 if requested_host and final_host and requested_host != final_host else 2


def _normalize_for_matching(value: str | None) -> str:
    import unicodedata

    normalized = unicodedata.normalize("NFD", value or "")
    ascii_text = "".join(ch for ch in normalized if unicodedata.category(ch) != "Mn")
    ascii_text = "".join(ch if ch.isalnum() else " " for ch in ascii_text)
    return " ".join(ascii_text.lower().split())


def _host(value: str | None) -> str:
    try:
        return urlparse(value or "").hostname or ""
    except Exception:
        return ""


def _urls_match_materially(requested_url: str | None, final_url: str | None) -> bool:
    try:
        requested = urlparse(requested_url or "")
        final = urlparse(final_url or "")
    except Exception:
        return False
    if not requested.hostname or not final.hostname:
        return False
    if requested.hostname != final.hostname:
        return False
    requested_path = requested.path.rstrip("/") or "/"
    final_path = final.path.rstrip("/") or "/"
    return requested_path == final_path


def _consent_navigation_matches(requested_url: str, final_url: str) -> bool:
    if not _urls_match_materially(requested_url, final_url):
        return False
    requested = urlparse(requested_url)
    final = urlparse(final_url)
    ignored_prefixes = ("utm_",)
    ignored_keys = {"fbclid", "gclid", "mc_cid", "mc_eid"}
    requested_query = {
        pair
        for pair in parse_qsl(requested.query, keep_blank_values=True)
        if pair[0].lower() not in ignored_keys and not pair[0].lower().startswith(ignored_prefixes)
    }
    if not requested_query:
        return True
    final_query = set(parse_qsl(final.query, keep_blank_values=True))
    return requested_query.issubset(final_query)


def _looks_like_consent_redirect(
    *,
    requested_url: str | None,
    final_url: str | None,
    content: str | None = None,
) -> bool:
    try:
        requested = urlparse(requested_url or "")
        final = urlparse(final_url or "")
    except Exception:
        return False
    if not requested.hostname or not final.hostname:
        return False
    requested_path = requested.path.rstrip("/") or "/"
    final_path = final.path.rstrip("/") or "/"
    if requested.hostname == final.hostname and requested_path == final_path:
        return False
    cross_origin = requested.hostname != final.hostname
    final_text = _normalize_for_matching(" ".join([final_url or "", content or ""]))
    same_origin_path_markers = (
        "cookie",
        "cookies",
        "pouzivani cookies",
        "cookie policy",
        "cookie notice",
    )
    cross_origin_path_markers = same_origin_path_markers + (
        "privacy",
        "policy",
        "consent",
        "souhlas",
        "nastaveni souhlasu",
        "gdpr",
        "terms",
        "ochrana osobnich udaju",
        "datenschutz",
    )
    path_markers = cross_origin_path_markers if cross_origin else same_origin_path_markers
    if any(marker in final_text for marker in path_markers):
        return True
    if not cross_origin:
        return False
    redirect_markers = (
        "ochrana osobnich udaju",
        "ochrana osobnych udajov",
        "privacy policy",
        "privacy notice",
        "data protection",
        "datenschutz",
        "confidentialite",
        "proteccion de datos",
        "informativa privacy",
        "podminky pouzivani",
        "terms of use",
        "consent",
        "gdpr",
    )
    return any(marker in final_text for marker in redirect_markers)


_CONSENT_REDIRECT_CLICK_SCRIPT = r"""
(action) => {
  const normalize = (value) => String(value || '')
    .normalize('NFD')
    .replace(/[\u0300-\u036f]/g, '')
    .replace(/\s+/g, ' ')
    .trim()
    .toLowerCase();
  const acceptLabels = [
    'accept all', 'accept', 'agree', 'i agree', 'ok', 'got it', 'allow all', 'continue',
    'rozumim a souhlasim', 'souhlasim', 'prijmout vse', 'prijmout vsechny',
    'povolit vse', 'pokracovat', 'rozumim',
    'suhlasim', 'prijat vsetko', 'prijat vsetky', 'povolit vsetko',
    'akceptuj wszystko', 'zaakceptuj wszystkie', 'zgadzam sie', 'zgoda',
    'alle akzeptieren', 'akzeptieren', 'zustimmen', 'ich stimme zu',
    'einverstanden',
    'tout accepter', 'accepter', 'j accepte',
    'aceptar todo', 'aceptar', 'estoy de acuerdo',
    'aceitar tudo', 'aceitar', 'concordo',
    'accetta tutto', 'accetta', 'acconsento',
    'alles accepteren', 'accepteren', 'ik ga akkoord'
  ];
  const rejectLabels = [
    'reject all', 'reject', 'decline', 'only necessary',
    'odmitnout vse', 'odmitnout', 'pouze nezbytne',
    'alle ablehnen', 'ablehnen', 'nur notwendige',
    'tout refuser', 'refuser', 'necessaires uniquement'
  ];
  const labels = action === 'reject' ? rejectLabels : acceptLabels;
  const strongConsentMarkers = [
    'cookie', 'cookies', 'tracking', 'personalized advertising',
    'personalised advertising', 'personalisierte werbung'
  ];
  const isVisible = (el) => {
    const style = window.getComputedStyle(el);
    const rect = el.getBoundingClientRect();
    return style.visibility !== 'hidden' && style.display !== 'none'
      && Number(style.opacity || 1) !== 0 && rect.width > 0 && rect.height > 0;
  };
  const textFor = (el) => normalize([
    el.innerText,
    el.textContent,
    el.getAttribute('aria-label'),
    el.getAttribute('title'),
    el.getAttribute('value'),
    el.getAttribute('data-testid')
  ].filter(Boolean).join(' '));
  const scopeSelector = [
    'dialog', '[role="dialog"]', '[aria-modal="true"]',
    '[id*="consent" i]', '[class*="consent" i]',
    '[id*="cookie" i]', '[class*="cookie" i]',
    '[id*="cmp" i]', '[class*="cmp" i]'
  ].join(',');
  const scopes = Array.from(document.querySelectorAll(scopeSelector)).filter((scope) => {
    if (!isVisible(scope)) return false;
    const scopeText = normalize(scope.innerText || scope.textContent || '');
    const identity = normalize([scope.id || '', scope.className || ''].join(' '));
    return /(?:consent|cookie|cmp)/.test(identity)
      || strongConsentMarkers.some((marker) => scopeText.includes(marker));
  });
  const candidates = scopes.flatMap((scope) => Array.from(
    scope.querySelectorAll('button, a, [role="button"], input[type="button"], input[type="submit"]')
  ));
  for (const el of candidates) {
    if (!isVisible(el)) continue;
    const text = textFor(el);
    if (!text || text.length > 120) continue;
    if (!labels.some((label) => text === label || (label.length >= 8 && text.includes(label)))) continue;
    el.click();
    return true;
  }
  return false;
}
"""
