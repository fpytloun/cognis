"""Browser-like HTTP headers and retry logic for web tools."""

from __future__ import annotations

import asyncio
import base64
import io
import logging
import mimetypes
import re
from urllib.parse import unquote, urlparse

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
_MAX_RESPONSE_SIZE = 64 * 1024 * 1024
_MAX_BINARY_ATTACHMENT_SIZE = 25 * 1024 * 1024
_MAX_PDF_PAGES = 2_000
_MAX_PDF_TEXT_CHARS = 16 * 1024 * 1024
_MAX_RETRIES = 3
_RETRY_BACKOFF_BASE = 1.0  # seconds

_TEXTUAL_MIME_PREFIXES = ("text/",)
_TEXTUAL_MIME_TYPES = {
    "application/json",
    "application/ld+json",
    "application/xml",
    "application/xhtml+xml",
    "application/rss+xml",
    "application/atom+xml",
    "application/csv",
    "application/x-ndjson",
}
_TEXTUAL_MIME_SUFFIXES = ("+json", "+xml")


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
            async with client.stream("GET", url, headers=request_headers) as response:
                content_length = _content_length(response)
                if content_length is not None and content_length > _MAX_RESPONSE_SIZE:
                    return _response_too_large(content_length)

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
                                "verification. The controller may attempt browser fallback."
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
                content = await _read_bounded_response(response)
                if isinstance(content, ToolResult):
                    return content
                response_headers = dict(response.headers)
                response_headers.pop("content-encoding", None)
                response_headers["content-length"] = str(len(content))
                return httpx.Response(
                    response.status_code,
                    headers=response_headers,
                    content=content,
                    request=response.request,
                    history=response.history,
                    extensions=response.extensions,
                )

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


def _content_length(response: httpx.Response) -> int | None:
    raw = response.headers.get("content-length")
    if not raw:
        return None
    try:
        return max(0, int(raw))
    except ValueError:
        return None


async def _read_bounded_response(response: httpx.Response) -> bytes | ToolResult:
    content = bytearray()
    async for chunk in response.aiter_bytes():
        if len(content) + len(chunk) > _MAX_RESPONSE_SIZE:
            return _response_too_large(len(content) + len(chunk))
        content.extend(chunk)
    return bytes(content)


def _response_too_large(observed_size: int) -> ToolResult:
    return ToolResult(
        output=(
            f"Web response exceeded the native fetch safety limit of {_MAX_RESPONSE_SIZE} bytes."
        ),
        is_error=True,
        metadata={
            "failure_category": "response_too_large",
            "source_limit_bytes": _MAX_RESPONSE_SIZE,
            "source_bytes_observed": observed_size,
        },
    )


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
    requested_url: str | None = None,
    source_url: str | None = None,
    options: dict[str, object] | None = None,
) -> ToolResult:
    """Extract response content and attach structured document metadata."""

    content_type = _normalize_content_type(response.headers.get("content-type", ""))
    raw_content = getattr(response, "content", b"")
    content = raw_content if isinstance(raw_content, bytes) else str(response.text).encode("utf-8")
    url = source_url or str(response.url)
    requested = requested_url or url
    filename = _filename_from_response(response, content_type=content_type)

    binary_kind = _binary_kind(content, content_type, filename)
    if binary_kind == "pdf":
        return _format_pdf_response(
            content,
            content_type=content_type,
            filename=filename,
            url=url,
            requested_url=requested,
        )
    if binary_kind == "binary":
        return _format_binary_response(
            content, content_type=content_type, filename=filename, url=url
        )

    source_truncated = False
    raw_text = response.text
    if "text/html" not in content_type:
        return ToolResult(
            output=raw_text,
            metadata={
                "source_url": url,
                "requested_url": requested,
                "fetched_url": url,
                "content_type": content_type or "text/plain",
                "filename": filename,
                "size_bytes": len(content),
                "source_limit_bytes": _MAX_RESPONSE_SIZE,
                "source_bytes_received": len(content),
                "source_truncated": source_truncated,
            },
        )

    from cognis.tools.executor.web.extraction import extract_document

    extraction_options = dict(options or {})
    extraction_options.setdefault("requested_url", requested)
    document = extract_document(
        raw_text,
        url=url,
        output_format=output_format,
        options=extraction_options,
    )
    document_data = document.as_dict()
    if _looks_like_landing_redirect(requested, url):
        quality = document_data.get("semantic_quality")
        if isinstance(quality, dict):
            quality_signals = quality.get("signals")
            normalized_signals = (
                [str(signal) for signal in quality_signals]
                if isinstance(quality_signals, list)
                else []
            )
            quality.update(
                {
                    "status": "navigation_only",
                    "label": "navigation_only",
                    "rank": 2,
                    "signals": [*normalized_signals, "redirected_to_landing_page"],
                }
            )
    if source_truncated:
        document_data["source_truncated"] = True
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
    from cognis.tools.executor.web.quality import classify_provider_error_page

    provider_error = classify_provider_error_page(document_data, document.content)
    if provider_error:
        return ToolResult(
            output=(
                "Web fetch loaded a provider-generated error page instead of the requested "
                f"content ({provider_error})."
            ),
            is_error=True,
            metadata={
                "extracted_document": document_data,
                "requested_url": requested,
                "fetched_url": url,
                "canonical_url": document_data.get("canonical_url"),
                "source_truncated": source_truncated,
                "direct_fetch_blocked": True,
                "direct_fetch_block_signal": provider_error,
            },
        )
    block_reason = _blocked_empty_extraction_reason(document_data)
    if block_reason:
        return ToolResult(
            output=(
                "Web fetch loaded the page but extraction produced no content "
                f"because the page appears blocked or requires verification ({block_reason})."
            ),
            is_error=True,
            metadata={
                "extracted_document": document_data,
                "requested_url": requested,
                "fetched_url": url,
                "canonical_url": document_data.get("canonical_url"),
                "source_truncated": source_truncated,
                "direct_fetch_blocked": True,
                "direct_fetch_block_signal": block_reason,
            },
        )
    return ToolResult(
        output=document.content,
        metadata={
            "extracted_document": document_data,
            "requested_url": requested,
            "fetched_url": url,
            "canonical_url": document_data.get("canonical_url"),
            "source_truncated": source_truncated,
            "source_limit_bytes": _MAX_RESPONSE_SIZE,
            "source_bytes_received": len(content),
        },
    )


def _blocked_empty_extraction_reason(document: dict[str, object]) -> str | None:
    content = str(document.get("content") or "").strip()
    if content:
        return None
    extractor = str(document.get("extractor") or "").lower()
    score = document.get("extraction_score")
    score_float = float(score) if isinstance(score, int | float) else 0.0
    if extractor != "empty" and score_float > 0:
        return None
    text = " ".join(
        str(document.get(key) or "") for key in ("title", "description", "url", "canonical_url")
    ).lower()
    markers = {
        "please wait for verification": "verification",
        "verify you are human": "human_verification",
        "access denied": "access_denied",
        "blocked": "blocked",
        "just a moment": "interstitial",
    }
    for marker, reason in markers.items():
        if marker in text:
            return reason
    return None


def _looks_like_landing_redirect(requested_url: str, fetched_url: str) -> bool:
    requested = urlparse(requested_url)
    fetched = urlparse(fetched_url)
    requested_host = (requested.hostname or "").lower().removeprefix("www.")
    fetched_host = (fetched.hostname or "").lower().removeprefix("www.")
    if requested_host != fetched_host:
        return False
    requested_parts = [part for part in requested.path.split("/") if part]
    fetched_parts = [part for part in fetched.path.split("/") if part]
    if len(requested_parts) < 3:
        return False
    return len(fetched_parts) <= 1 and fetched.path != requested.path


def _normalize_content_type(value: str) -> str:
    return value.split(";", 1)[0].strip().lower()


def _binary_kind(content: bytes, content_type: str, filename: str) -> str | None:
    if _is_pdf(content, content_type, filename):
        return "pdf"
    if _is_textual_content_type(content_type):
        return None
    if _looks_textual_bytes(content):
        return None
    return "binary"


def _is_pdf(content: bytes, content_type: str, filename: str) -> bool:
    return (
        content_type == "application/pdf"
        or filename.lower().endswith(".pdf")
        or content.startswith(b"%PDF-")
    )


def _is_textual_content_type(content_type: str) -> bool:
    if not content_type:
        return False
    return (
        content_type.startswith(_TEXTUAL_MIME_PREFIXES)
        or content_type in _TEXTUAL_MIME_TYPES
        or content_type.endswith(_TEXTUAL_MIME_SUFFIXES)
    )


def _looks_textual_bytes(content: bytes) -> bool:
    sample = content[:1024]
    if not sample:
        return True
    if b"\x00" in sample:
        return False
    try:
        sample.decode("utf-8")
    except UnicodeDecodeError:
        return False
    return True


def _format_pdf_response(
    content: bytes,
    *,
    content_type: str,
    filename: str,
    url: str,
    requested_url: str,
) -> ToolResult:
    pdf_content_type = (
        content_type
        if content_type and content_type != "application/octet-stream"
        else "application/pdf"
    )
    attachment = _binary_attachment(
        content,
        content_type=pdf_content_type,
        filename=filename or "document.pdf",
        url=url,
        purpose="web_fetch",
    )
    page_texts, pdf_metadata = _extract_pdf_text(content)
    pdf_text_characters = sum(len(page) for page in page_texts)
    pdf_text_truncated = (
        len(page_texts) >= _MAX_PDF_PAGES or pdf_text_characters >= _MAX_PDF_TEXT_CHARS
    )
    lines = [
        "[[metadata]]",
        f"URL: {url}",
        f"Filename: {filename}",
        f"Content type: {pdf_content_type}",
        f"Size bytes: {len(content)}",
        f"Pages: {len(page_texts)}",
    ]
    if attachment:
        lines.append("Original PDF: attached as artifact")
    else:
        lines.append(
            f"Original PDF: not attached because it exceeds {_MAX_BINARY_ATTACHMENT_SIZE} bytes"
        )
    title = pdf_metadata.get("title")
    author = pdf_metadata.get("author")
    if title:
        lines.append(f"Title: {title}")
    if author:
        lines.append(f"Author: {author}")
    lines.append("")
    if page_texts:
        for index, page_text in enumerate(page_texts, start=1):
            lines.append(f"[[page:{index}]]")
            lines.append(page_text.strip() or "[no extractable text]")
            lines.append("")
    else:
        lines.extend(["[[page:1]]", "[no extractable text]", ""])
    return ToolResult(
        output="\n".join(lines).strip(),
        metadata={
            "source_url": url,
            "requested_url": requested_url,
            "fetched_url": url,
            "canonical_url": url,
            "content_type": pdf_content_type,
            "filename": filename,
            "size_bytes": len(content),
            "binary_content": True,
            "binary_kind": "pdf",
            "attachment_created": bool(attachment),
            "pdf_page_count": len(page_texts),
            "pdf_page_limit": _MAX_PDF_PAGES,
            "pdf_text_characters": pdf_text_characters,
            "pdf_text_limit_characters": _MAX_PDF_TEXT_CHARS,
            "pdf_text_truncated": pdf_text_truncated,
            "pdf_metadata": pdf_metadata,
            "extracted_document": {
                "url": url,
                "canonical_url": url,
                "title": title,
                "author": author,
                "extractor": "pypdf",
                "semantic_quality": {
                    "status": (
                        "partial"
                        if pdf_text_truncated
                        else ("complete" if any(page.strip() for page in page_texts) else "empty")
                    ),
                    "label": (
                        "partial"
                        if pdf_text_truncated
                        else ("complete" if any(page.strip() for page in page_texts) else "empty")
                    ),
                    "score": min(100.0, sum(len(page) for page in page_texts) / 100),
                    "rank": (
                        3
                        if pdf_text_truncated
                        else (4 if any(page.strip() for page in page_texts) else 0)
                    ),
                    "signals": (
                        ["pdf_text_extracted", "pdf_text_truncated"]
                        if pdf_text_truncated
                        else (
                            ["pdf_text_extracted"]
                            if any(page.strip() for page in page_texts)
                            else ["no_extractable_pdf_text"]
                        )
                    ),
                },
            },
        },
        attachments=[attachment] if attachment else None,
    )


def _format_binary_response(
    content: bytes,
    *,
    content_type: str,
    filename: str,
    url: str,
) -> ToolResult:
    guessed_type = content_type or mimetypes.guess_type(filename)[0] or "application/octet-stream"
    kind = "image" if guessed_type.startswith("image/") else "binary"
    attachment = _binary_attachment(
        content,
        content_type=guessed_type,
        filename=filename,
        url=url,
        purpose="web_fetch",
    )
    lines = [
        "[[metadata]]",
        f"URL: {url}",
        f"Filename: {filename}",
        f"Content type: {guessed_type}",
        f"Size bytes: {len(content)}",
        f"Binary kind: {kind}",
    ]
    if attachment:
        lines.append("Binary content: attached as artifact")
    else:
        lines.append(
            f"Binary content: not attached because it exceeds {_MAX_BINARY_ATTACHMENT_SIZE} bytes"
        )
    if guessed_type.startswith("image/"):
        lines.append("Use artifact_read to analyze this image with a vision-capable model.")
    return ToolResult(
        output="\n".join(lines),
        metadata={
            "source_url": url,
            "content_type": guessed_type,
            "filename": filename,
            "size_bytes": len(content),
            "binary_content": True,
            "binary_kind": kind,
            "attachment_created": bool(attachment),
        },
        attachments=[attachment] if attachment else None,
    )


def _extract_pdf_text(content: bytes) -> tuple[list[str], dict[str, str]]:
    try:
        from pypdf import PdfReader

        reader = PdfReader(io.BytesIO(content))
        metadata_raw = getattr(reader, "metadata", None)
        metadata = _pdf_metadata(metadata_raw)
        pages: list[str] = []
        total_chars = 0
        for page in reader.pages[:_MAX_PDF_PAGES]:
            text = str(page.extract_text() or "")
            remaining = _MAX_PDF_TEXT_CHARS - total_chars
            if remaining <= 0:
                break
            pages.append(text[:remaining])
            total_chars += len(pages[-1])
        return pages, metadata
    except Exception as exc:  # pragma: no cover - defensive around malformed PDFs
        logger.debug("web: PDF text extraction failed (%s)", type(exc).__name__)
        return [], {}


def _pdf_metadata(metadata: object) -> dict[str, str]:
    if metadata is None:
        return {}
    result: dict[str, str] = {}
    for key, label in (("/Title", "title"), ("/Author", "author")):
        value = getattr(metadata, key.removeprefix("/"), None)
        if value is None and hasattr(metadata, "get"):
            try:
                value = metadata.get(key)
            except Exception:
                value = None
        if value:
            result[label] = str(value)
    return result


def _binary_attachment(
    content: bytes,
    *,
    content_type: str,
    filename: str,
    url: str,
    purpose: str,
) -> dict[str, object] | None:
    if len(content) > _MAX_BINARY_ATTACHMENT_SIZE:
        return None
    return {
        "filename": filename,
        "mime_type": content_type or "application/octet-stream",
        "size_bytes": len(content),
        "source_url": url,
        "purpose": purpose,
        "content_b64": base64.b64encode(content).decode("ascii"),
    }


def _filename_from_response(response: httpx.Response, *, content_type: str) -> str:
    disposition = response.headers.get("content-disposition", "")
    filename = _filename_from_content_disposition(disposition)
    if not filename:
        path = unquote(urlparse(str(response.url)).path)
        filename = path.rsplit("/", 1)[-1] if path else ""
    if not filename:
        filename = "download"
    if "." not in filename:
        extension = mimetypes.guess_extension(content_type or "") or ""
        filename = f"{filename}{extension}"
    return _safe_filename(filename)


def _filename_from_content_disposition(value: str) -> str | None:
    if not value:
        return None
    match = re.search(r"filename\*=UTF-8''([^;]+)", value, flags=re.IGNORECASE)
    if match:
        return unquote(match.group(1).strip().strip('"'))
    match = re.search(r'filename="?([^";]+)"?', value, flags=re.IGNORECASE)
    if match:
        return match.group(1).strip()
    return None


def _safe_filename(value: str) -> str:
    filename = value.rsplit("/", 1)[-1].rsplit("\\", 1)[-1].strip() or "download"
    filename = filename.replace("..", "_")
    filename = re.sub(r"[^A-Za-z0-9._ -]+", "_", filename)
    return filename[:180] or "download"
