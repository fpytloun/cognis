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
from contextlib import suppress
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

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

        session_id = f"web-fetch-{uuid.uuid4().hex[:12]}"
        try:
            session = await self._manager.open_session(
                session_id=session_id,
                url=url,
                headless=not self._headed,
                profile_mode="ephemeral",
                wait_for_slot=True,
                wait_timeout_seconds=self._wait_timeout_seconds,
                lifecycle="ephemeral",
                session_idle_seconds=self._session_idle_seconds,
                navigation_timeout_seconds=self._navigation_timeout_seconds,
                wait_until=self._wait_until,
                network_idle_after_dom_seconds=self._network_idle_after_dom_seconds,
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
                await self._wait_for_rendered_body(session.page)
                recovered_consent_redirect = await self._recover_consent_redirect(
                    session,
                    requested_url=url,
                )
                if recovered_consent_redirect:
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
        requested_url: str,
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
        document_data = document.as_dict()
        block_signal = _classify_browser_extract_quality(
            document_data,
            document.content,
            requested_url=requested_url,
            final_url=url,
        )
        if block_signal:
            document_data["browser_block_signal"] = block_signal
            document_data["extraction_status"] = f"blocked_or_empty:{block_signal}"
        else:
            document_data["extraction_status"] = "ok"
        return document.content, {"extracted_document": document_data}

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
                  return text.length >= 80 || html.length >= 2000;
                }
                """,
                timeout=5000,
            )
        except Exception:
            # Some pages are genuinely thin or block wait_for_function; extraction
            # quality classification handles the remaining failure mode.
            return

    async def _recover_consent_redirect(self, session: Any, *, requested_url: str) -> bool:
        page = session.page
        final_url = str(getattr(page, "url", "") or "")
        if not _looks_like_consent_redirect(requested_url=requested_url, final_url=final_url):
            return False
        try:
            clicked = await page.evaluate(_CONSENT_REDIRECT_CLICK_SCRIPT)
        except Exception:
            clicked = False
        if clicked:
            with suppress(Exception):
                await page.wait_for_load_state("networkidle", timeout=3000)
        try:
            await page.goto(
                requested_url,
                timeout=int(self._navigation_timeout_seconds * 1000),
                wait_until=self._wait_until,
            )
            if self._network_idle_after_dom_seconds > 0 and self._wait_until != "networkidle":
                with suppress(Exception):
                    await page.wait_for_load_state(
                        "networkidle",
                        timeout=int(self._network_idle_after_dom_seconds * 1000),
                    )
        except Exception:
            return False
        return True


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
    if any(marker in normalized for marker in hard_markers):
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
    if real_page_score >= 3:
        score -= 2
    return max(score, 0)


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
() => {
  const normalize = (value) => String(value || '')
    .normalize('NFD')
    .replace(/[\u0300-\u036f]/g, '')
    .replace(/\s+/g, ' ')
    .trim()
    .toLowerCase();
  const labels = [
    'accept all', 'accept', 'agree', 'i agree', 'ok', 'got it', 'allow all', 'continue',
    'rozumim a souhlasim', 'souhlasim', 'prijmout vse', 'prijmout vsechny',
    'povolit vse', 'pokracovat', 'rozumim',
    'suhlasim', 'prijat vsetko', 'prijat vsetky', 'povolit vsetko',
    'akceptuj wszystko', 'zaakceptuj wszystkie', 'zgadzam sie', 'zgoda',
    'alle akzeptieren', 'akzeptieren', 'zustimmen', 'ich stimme zu',
    'tout accepter', 'accepter', 'j accepte',
    'aceptar todo', 'aceptar', 'estoy de acuerdo',
    'aceitar tudo', 'aceitar', 'concordo',
    'accetta tutto', 'accetta', 'acconsento',
    'alles accepteren', 'accepteren', 'ik ga akkoord'
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
  const candidates = Array.from(
    document.querySelectorAll('button, a, [role="button"], input[type="button"], input[type="submit"]')
  );
  for (const el of candidates) {
    if (!isVisible(el)) continue;
    const text = textFor(el);
    if (!text || text.length > 120) continue;
    if (!labels.some((label) => text === label || text.includes(label))) continue;
    el.click();
    return true;
  }
  return false;
}
"""
