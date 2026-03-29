"""Executor-native web tool: web_fetch."""

from __future__ import annotations

from typing import Any

import httpx

from cognis.models.tool import ToolResult
from cognis.tools.registry import ToolExecutionContext

_DEFAULT_TIMEOUT = 30
_MAX_TIMEOUT = 120
_MAX_RESPONSE_SIZE = 500_000


async def handle_web_fetch(arguments: dict[str, Any], context: ToolExecutionContext) -> ToolResult:
    """Fetch content from a URL and return it as text or markdown."""
    url = arguments.get("url", "")
    output_format = arguments.get("format", "markdown")
    timeout = min(int(arguments.get("timeout", _DEFAULT_TIMEOUT)), _MAX_TIMEOUT)

    if not url:
        return ToolResult(output="No URL provided.", is_error=True)

    # Ensure HTTPS
    if url.startswith("http://"):
        url = "https://" + url[7:]

    try:
        async with httpx.AsyncClient(
            timeout=timeout, follow_redirects=True, max_redirects=5
        ) as client:
            response = await client.get(url, headers={"User-Agent": "Cognis/1.0"})
            response.raise_for_status()
    except httpx.TimeoutException:
        return ToolResult(output=f"Request timed out after {timeout}s.", is_error=True)
    except httpx.HTTPStatusError as exc:
        return ToolResult(
            output=f"HTTP {exc.response.status_code}: {exc.response.reason_phrase}",
            is_error=True,
        )
    except httpx.RequestError as exc:
        return ToolResult(output=f"Request failed: {exc}", is_error=True)

    content_type = response.headers.get("content-type", "")
    raw_text = response.text

    if len(raw_text) > _MAX_RESPONSE_SIZE:
        raw_text = raw_text[:_MAX_RESPONSE_SIZE]
        raw_text += f"\n[truncated: response exceeded {_MAX_RESPONSE_SIZE} chars]"

    if output_format == "html" or "text/html" not in content_type:
        return ToolResult(output=raw_text)

    if output_format == "text":
        return ToolResult(output=_html_to_text(raw_text))

    # Default: markdown
    return ToolResult(output=_html_to_markdown(raw_text))


def _html_to_text(html: str) -> str:
    """Simple HTML to plain text conversion."""
    import re

    text = re.sub(r"<script[^>]*>.*?</script>", "", html, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<style[^>]*>.*?</style>", "", text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def _html_to_markdown(html: str) -> str:
    """Convert HTML to markdown using markdownify if available, else plain text."""
    try:
        from markdownify import markdownify  # type: ignore[import-not-found]

        result: str = markdownify(html, heading_style="ATX", strip=["script", "style"])
        return result
    except ImportError:
        return _html_to_text(html)
