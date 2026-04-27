"""Browser-like HTTP headers and retry logic for web tools."""

from __future__ import annotations

import asyncio
import logging

import httpx

from cognis.models.tool import ToolResult

logger = logging.getLogger(__name__)

# Chrome 131 on Windows 10 — update periodically.
_CHROME_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)

BROWSER_HEADERS: dict[str, str] = {
    "User-Agent": _CHROME_UA,
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;q=0.9,"
        "image/avif,image/webp,image/apng,*/*;q=0.8"
    ),
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "Upgrade-Insecure-Requests": "1",
    "Connection": "keep-alive",
}

_DEFAULT_TIMEOUT = 30
_MAX_TIMEOUT = 120
_MAX_RESPONSE_SIZE = 500_000
_MAX_RETRIES = 3
_RETRY_BACKOFF_BASE = 1.0  # seconds


def sanitise_url(url: str) -> str:
    """Ensure URL uses HTTPS and is well-formed.

    Only http:// and https:// schemes are supported. Other schemes
    (ftp://, etc.) are rejected.
    """
    if url.startswith("http://"):
        url = "https://" + url[7:]
    elif url.startswith("https://"):
        pass  # already fine
    elif "://" in url:
        raise ValueError(f"Unsupported URL scheme: {url.split('://')[0]}")
    else:
        url = "https://" + url
    return url


def clamp_timeout(timeout: int | None) -> int:
    """Clamp timeout to allowed range."""
    if timeout is None:
        return _DEFAULT_TIMEOUT
    return max(1, min(int(timeout), _MAX_TIMEOUT))


async def fetch_with_retry(
    url: str,
    *,
    timeout: int = _DEFAULT_TIMEOUT,
    headers: dict[str, str] | None = None,
    max_retries: int = _MAX_RETRIES,
) -> httpx.Response | ToolResult:
    """Fetch a URL with browser-like headers and retry on 429.

    Returns an ``httpx.Response`` on success, or a ``ToolResult`` with
    ``is_error=True`` on user-actionable failure (Cloudflare, bad URL).

    Raises network-level exceptions (``httpx.RequestError``,
    ``httpx.TimeoutException``) so that a wrapping circuit breaker can
    count failures.
    """
    request_headers = dict(BROWSER_HEADERS)
    if headers:
        request_headers.update(headers)

    try:
        url = sanitise_url(url)
    except ValueError as exc:
        return ToolResult(output=str(exc), is_error=True)

    async with httpx.AsyncClient(
        timeout=timeout,
        follow_redirects=True,
        max_redirects=5,
    ) as client:
        for attempt in range(max_retries):
            response = await client.get(url, headers=request_headers)

            if response.status_code == 429:
                retry_after = _parse_retry_after(response)
                wait = retry_after or (_RETRY_BACKOFF_BASE * (2**attempt))
                if attempt < max_retries - 1:
                    logger.info(
                        "web: 429 rate limited, retrying",
                        extra={"extra_data": {"attempt": attempt + 1, "wait_s": wait}},
                    )
                    await asyncio.sleep(wait)
                    continue
                return ToolResult(
                    output=f"Rate limited (HTTP 429) after {max_retries} attempts.",
                    is_error=True,
                )

            if response.status_code == 403:
                cf_mitigated = response.headers.get("cf-mitigated", "")
                if cf_mitigated:
                    return ToolResult(
                        output=(
                            "Direct HTTP fetch was blocked by Cloudflare browser "
                            "verification. The controller may attempt headless "
                            "(and optionally headed) browser fallback."
                        ),
                        is_error=True,
                        metadata={
                            "cloudflare_blocked": True,
                            "direct_fetch_blocked": True,
                        },
                    )

            if response.status_code >= 500 and attempt < max_retries - 1:
                wait = _RETRY_BACKOFF_BASE * (2**attempt)
                logger.info(
                    "web: server error, retrying",
                    extra={
                        "extra_data": {
                            "status": response.status_code,
                            "attempt": attempt + 1,
                            "wait_s": wait,
                        }
                    },
                )
                await asyncio.sleep(wait)
                continue

            response.raise_for_status()
            return response

    # Unreachable in practice — the loop always returns or raises.
    return ToolResult(output="Request failed.", is_error=True)  # pragma: no cover


def _parse_retry_after(response: httpx.Response) -> float | None:
    """Parse Retry-After header (seconds only; HTTP-date is ignored)."""
    value = response.headers.get("retry-after")
    if not value:
        return None
    try:
        return float(value)
    except ValueError:
        return None


def truncate_content(text: str, max_size: int = _MAX_RESPONSE_SIZE) -> str:
    """Truncate content with a notice if it exceeds the limit."""
    if len(text) <= max_size:
        return text
    return text[:max_size] + f"\n[truncated: response exceeded {max_size} chars]"


def html_to_text(html: str) -> str:
    """Simple HTML to plain text conversion."""
    import re

    text = re.sub(r"<script[^>]*>.*?</script>", "", html, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<style[^>]*>.*?</style>", "", text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def html_to_markdown(html: str, *, url: str | None = None) -> str:
    """Convert HTML to markdown.

    Prefers ``trafilatura.extract`` for boilerplate-free output (strips
    nav/sidebar/ads/footer the way Reader View does). Falls back to
    ``markdownify`` if trafilatura returns empty (it sometimes does on
    JSON-rendered or unusual pages) so the agent always gets *something*.
    """
    try:
        import trafilatura

        extracted = trafilatura.extract(
            html,
            output_format="markdown",
            include_links=True,
            include_tables=True,
            include_comments=False,
            include_images=False,
            url=url,
            favor_recall=True,
        )
        if extracted and extracted.strip():
            return str(extracted)
    except Exception as exc:  # pragma: no cover - defensive
        logger.debug(
            "web: trafilatura extraction failed (%s); falling back to markdownify",
            type(exc).__name__,
        )

    from markdownify import markdownify

    result: str = markdownify(html, heading_style="ATX", strip=["script", "style"])
    return result


def convert_html(html: str, output_format: str, *, url: str | None = None) -> str:
    """Convert HTML to the requested format."""
    if output_format == "html":
        return html
    if output_format == "text":
        return html_to_text(html)
    return html_to_markdown(html, url=url)


def format_response(response: httpx.Response, output_format: str) -> str:
    """Extract and format response content."""
    content_type = response.headers.get("content-type", "")
    raw_text = response.text
    raw_text = truncate_content(raw_text)

    if "text/html" in content_type:
        return convert_html(raw_text, output_format, url=str(response.url))
    return raw_text


def format_response_result(
    response: httpx.Response,
    output_format: str,
    *,
    source_url: str | None = None,
    options: dict[str, object] | None = None,
) -> ToolResult:
    """Extract response content and attach structured document metadata."""

    content_type = response.headers.get("content-type", "")
    raw_text = truncate_content(response.text)
    if "text/html" not in content_type:
        return ToolResult(output=raw_text)

    from cognis.tools.executor.web.extraction import extract_document

    document = extract_document(
        raw_text,
        url=source_url or str(response.url),
        output_format=output_format,
        options=options,
    )
    return ToolResult(
        output=document.content,
        metadata={"extracted_document": document.as_dict()},
    )
