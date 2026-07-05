"""Matrix-safe rich text formatting.

Matrix clients render the ``formatted_body`` field using a restricted HTML
subset.  This module converts assistant Markdown into that subset and strips
anything that should not be sent to a Matrix room.
"""

from __future__ import annotations

from urllib.parse import urlparse

import markdown as markdown_lib
from bs4 import BeautifulSoup, Tag

_MARKDOWN_EXTENSIONS = ("fenced_code", "tables", "sane_lists", "nl2br")

_ALLOWED_TAGS = {
    "a",
    "blockquote",
    "br",
    "code",
    "del",
    "em",
    "h1",
    "h2",
    "h3",
    "h4",
    "h5",
    "h6",
    "hr",
    "li",
    "ol",
    "p",
    "pre",
    "strong",
    "table",
    "tbody",
    "td",
    "th",
    "thead",
    "tr",
    "ul",
}

_SAFE_LINK_SCHEMES = {"http", "https", "matrix", "mailto"}


def markdown_to_matrix_html(value: str) -> str:
    """Render Markdown to sanitized Matrix-compatible HTML."""

    if not value:
        return ""
    normalized = _normalize_task_list_markers(value)
    rendered = markdown_lib.markdown(
        normalized,
        extensions=list(_MARKDOWN_EXTENSIONS),
        output_format="html",
    )
    return _sanitize_matrix_html(rendered)


def _normalize_task_list_markers(value: str) -> str:
    """Translate GitHub-style task list markers to portable text markers."""

    lines: list[str] = []
    for line in value.splitlines():
        stripped = line.lstrip()
        prefix = line[: len(line) - len(stripped)]
        lower = stripped.lower()
        if lower.startswith("- [x] "):
            lines.append(f"{prefix}- ☑ {stripped[6:]}")
        elif lower.startswith("- [ ] "):
            lines.append(f"{prefix}- ☐ {stripped[6:]}")
        else:
            lines.append(line)
    return "\n".join(lines)


def _sanitize_matrix_html(rendered: str) -> str:
    soup = BeautifulSoup(rendered, "html.parser")
    while True:
        disallowed = next(
            (tag for tag in soup.find_all(True) if tag.name not in _ALLOWED_TAGS),
            None,
        )
        if disallowed is None:
            break
        disallowed.unwrap()
    for tag in soup.find_all(True):
        _sanitize_tag(tag)
    return str(soup)


def _sanitize_tag(tag: Tag) -> None:
    if tag.name == "a":
        href = str(tag.get("href") or "")
        parsed = urlparse(href)
        if parsed.scheme.lower() not in _SAFE_LINK_SCHEMES:
            tag.unwrap()
            return
        tag.attrs = {"href": href}
        return
    tag.attrs = {}
