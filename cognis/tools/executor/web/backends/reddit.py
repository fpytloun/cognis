"""Reddit-specific direct fetch adapter."""

from __future__ import annotations

import html
import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse, urlunparse

import httpx

from cognis.models.tool import ToolResult
from cognis.tools.executor.web.extraction import ExtractedDocument
from cognis.tools.executor.web.headers import BROWSER_HEADERS, clamp_timeout, sanitise_url


@dataclass(frozen=True)
class RedditCommentUrl:
    subreddit: str
    post_id: str
    slug: str


def parse_reddit_comment_url(url: str) -> RedditCommentUrl | None:
    """Return Reddit comment URL parts for public thread URLs."""
    try:
        parsed = urlparse(sanitise_url(url))
    except ValueError:
        return None
    host = (parsed.hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    if host not in {"reddit.com", "old.reddit.com", "new.reddit.com"}:
        return None
    parts = [part for part in parsed.path.split("/") if part]
    if len(parts) < 4 or parts[0].lower() != "r" or parts[2].lower() != "comments":
        return None
    return RedditCommentUrl(
        subreddit=parts[1], post_id=parts[3], slug=parts[4] if len(parts) > 4 else ""
    )


def reddit_json_url(url: str) -> str | None:
    parsed = parse_reddit_comment_url(url)
    if parsed is None:
        return None
    path = f"/r/{parsed.subreddit}/comments/{parsed.post_id}"
    if parsed.slug:
        path += f"/{parsed.slug}"
    path += "/.json"
    return urlunparse(("https", "www.reddit.com", path, "", "raw_json=1", ""))


async def fetch_reddit_thread(
    url: str,
    *,
    output_format: str = "markdown",
    timeout: int = 30,
) -> ToolResult | None:
    """Fetch Reddit threads through the public JSON endpoint when applicable."""
    json_url = reddit_json_url(url)
    if json_url is None:
        return None

    request_headers = dict(BROWSER_HEADERS)
    request_headers["Accept"] = "application/json,text/plain;q=0.9,*/*;q=0.8"
    async with httpx.AsyncClient(
        timeout=clamp_timeout(timeout),
        follow_redirects=True,
        max_redirects=5,
    ) as client:
        response = await client.get(json_url, headers=request_headers)
        response.raise_for_status()
    data = response.json()
    document = _reddit_document(
        data, requested_url=url, json_url=json_url, output_format=output_format
    )
    if document is None:
        return None
    return ToolResult(
        output=document.content,
        metadata={
            "extracted_document": document.as_dict(),
            "reddit_adapter": True,
            "reddit_json_url": json_url,
        },
    )


def _reddit_document(
    data: Any,
    *,
    requested_url: str,
    json_url: str,
    output_format: str,
) -> ExtractedDocument | None:
    post = _first_post(data)
    if not post:
        return None
    title = str(post.get("title") or "").strip()
    author = str(post.get("author") or "").strip() or None
    subreddit = str(post.get("subreddit_name_prefixed") or post.get("subreddit") or "").strip()
    canonical_url = _canonical_reddit_url(post, requested_url=requested_url)
    published_at = _reddit_timestamp(post.get("created_utc"))
    body = str(post.get("selftext") or "").strip()
    lines: list[str] = []
    if title:
        lines.append(f"# {title}")
        lines.append("")
    details = []
    if subreddit:
        details.append(subreddit)
    if author:
        details.append(f"u/{author}")
    if published_at:
        details.append(published_at)
    if details:
        lines.append(" | ".join(details))
        lines.append("")
    if body:
        lines.append(body)
    comments = _top_comments(data)
    if comments:
        lines.extend(["", "## Comments", ""])
        for comment in comments:
            comment_author = str(comment.get("author") or "[deleted]")
            comment_body = str(comment.get("body") or "").strip()
            if not comment_body:
                continue
            lines.append(f"- u/{comment_author}: {_single_line(comment_body)}")
    content = "\n".join(lines).strip()
    if output_format == "text":
        content = _markdown_to_text(content)
    elif output_format == "html":
        content = _markdown_to_html(content)
    return ExtractedDocument(
        url=canonical_url or requested_url,
        canonical_url=canonical_url,
        title=title or None,
        description=_single_line(body)[:300] if body else None,
        site_name="reddit",
        author=author,
        published_at=published_at,
        language="en",
        output_format=output_format
        if output_format in {"markdown", "text", "html"}
        else "markdown",
        extractor="reddit_json",
        extraction_score=float(len(content)),
        content=content,
        structured_data={"reddit_json_url": json_url},
    )


def _first_post(data: Any) -> dict[str, Any] | None:
    try:
        post = data[0]["data"]["children"][0]["data"]
    except (IndexError, KeyError, TypeError):
        return None
    return post if isinstance(post, dict) else None


def _top_comments(data: Any, *, limit: int = 8) -> list[dict[str, Any]]:
    try:
        children = data[1]["data"]["children"]
    except (IndexError, KeyError, TypeError):
        return []
    comments: list[dict[str, Any]] = []
    for child in children:
        if not isinstance(child, dict) or child.get("kind") != "t1":
            continue
        payload = child.get("data")
        if isinstance(payload, dict):
            comments.append(payload)
        if len(comments) >= limit:
            break
    return comments


def _canonical_reddit_url(post: dict[str, Any], *, requested_url: str) -> str | None:
    permalink = str(post.get("permalink") or "").strip()
    if permalink.startswith("/"):
        return f"https://www.reddit.com{permalink}"
    return requested_url


def _reddit_timestamp(value: Any) -> str | None:
    if not isinstance(value, int | float):
        return None
    from datetime import UTC, datetime

    return datetime.fromtimestamp(float(value), tz=UTC).date().isoformat()


def _single_line(value: str) -> str:
    return " ".join(value.split())


def _markdown_to_text(value: str) -> str:
    text = re.sub(r"[`*_>#-]+", " ", value)
    return _single_line(text)


def _markdown_to_html(value: str) -> str:
    lines = []
    for line in value.splitlines():
        escaped = html.escape(line)
        if line.startswith("# "):
            lines.append(f"<h1>{html.escape(line[2:])}</h1>")
        elif line.startswith("## "):
            lines.append(f"<h2>{html.escape(line[3:])}</h2>")
        elif line:
            lines.append(f"<p>{escaped}</p>")
    return "\n".join(lines)
