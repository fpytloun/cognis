"""Matrix-safe rich text formatting.

Matrix clients render the ``formatted_body`` field using a restricted HTML
subset.  This module converts assistant Markdown into that subset and strips
anything that should not be sent to a Matrix room.
"""

from __future__ import annotations

from urllib.parse import urlparse

import markdown as markdown_lib  # type: ignore[import-untyped]
from bs4 import BeautifulSoup, Tag
from bs4.element import NavigableString

from cognis.channels.markdown_rendering import normalize_gfm

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


def markdown_to_matrix_html(value: str, *, compact: bool = False) -> str:
    """Render Markdown to sanitized Matrix-compatible HTML."""

    if not value:
        return ""
    rendered = markdown_lib.markdown(
        normalize_gfm(value),
        extensions=list(_MARKDOWN_EXTENSIONS),
        output_format="html",
    )
    sanitized = _sanitize_matrix_html(rendered)
    return _compact_matrix_html(sanitized) if compact else sanitized


def _sanitize_matrix_html(rendered: str) -> str:
    soup = BeautifulSoup(rendered, "html.parser")
    _linearize_tables(soup)
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


def _linearize_tables(soup: BeautifulSoup) -> None:
    """Prefer readable labelled rows over inconsistently rendered Matrix tables."""

    for table in list(soup.find_all("table")):
        rows = table.find_all("tr")
        headers = (
            [cell.get_text(" ", strip=True) for cell in rows[0].find_all(["th", "td"])]
            if rows
            else []
        )
        replacement = soup.new_tag("p")
        for row_index, row in enumerate(rows[1:] or rows):
            values = [cell.get_text(" ", strip=True) for cell in row.find_all(["th", "td"])]
            if row_index:
                replacement.append(soup.new_tag("br"))
            replacement.append(
                NavigableString(
                    " · ".join(
                        f"{header}: {value}" if header else value
                        for header, value in zip(headers, values, strict=False)
                        if value
                    )
                )
            )
        table.replace_with(replacement)


def _compact_matrix_html(rendered: str) -> str:
    """Remove client-defined paragraph/list margins from rich document messages."""

    soup = BeautifulSoup(rendered, "html.parser")
    for paragraph in list(soup.find_all("p")):
        paragraph.append(soup.new_tag("br"))
        paragraph.unwrap()
    for list_item in list(soup.find_all("li")):
        list_item.insert(0, NavigableString("• "))
        list_item.append(soup.new_tag("br"))
        list_item.unwrap()
    for container in list(soup.find_all(["ul", "ol"])):
        container.unwrap()
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
