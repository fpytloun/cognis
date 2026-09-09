"""Markdown normalization and channel-native rendering helpers.

Outbound assistant text and canonical Rich Deliverable Markdown enter this
module unchanged.  Adapters select the smallest native representation their
platform supports.
"""

from __future__ import annotations

import html
import re
from urllib.parse import urlparse

import markdown as markdown_lib  # type: ignore[import-untyped]
from bs4 import BeautifulSoup, Tag
from bs4.element import NavigableString

_MARKDOWN_EXTENSIONS = ("fenced_code", "tables", "sane_lists", "nl2br")
_SAFE_LINK_SCHEMES = {"http", "https", "mailto"}
_TASK_RE = re.compile(r"^(\s*)([-+*])\s+\[([ xX])\]\s+")
_FENCE_OR_CODE_RE = re.compile(r"(```.*?```|`[^`\n]+`)", re.DOTALL)
_STRIKE_RE = re.compile(r"~~(?=\S)(.+?)(?<=\S)~~")


def normalize_gfm(markdown: str) -> str:
    """Normalize common GFM constructs for Python-Markdown."""

    normalized = normalize_portable_markdown(markdown)
    parts = _FENCE_OR_CODE_RE.split(normalized)
    return "".join(
        part if index % 2 else _STRIKE_RE.sub(r"<del>\1</del>", part)
        for index, part in enumerate(parts)
    )


def normalize_portable_markdown(markdown: str) -> str:
    """Normalize list/task/table syntax without changing inline Markdown."""

    normalized_lines: list[str] = []
    in_fence = False
    for line in markdown.splitlines():
        if line.lstrip().startswith("```"):
            in_fence = not in_fence
            normalized_lines.append(line)
            continue
        if in_fence:
            normalized_lines.append(line)
            continue
        match = _TASK_RE.match(line)
        if match:
            indent, marker, state = match.groups()
            checkbox = "☑" if state.lower() == "x" else "☐"
            line = _TASK_RE.sub(f"{indent}{marker} {checkbox} ", line, count=1)
        normalized_lines.append(_normalize_list_indent(line))
    return _linearize_markdown_tables("\n".join(normalized_lines))


def markdown_to_plain_text(markdown: str) -> str:
    """Render Markdown as structured, readable plain text."""

    soup = _markdown_soup(markdown)
    _linearize_tables(soup)
    return _render_nodes(soup, flavor="plain").strip()


def markdown_to_slack_mrkdwn(markdown: str) -> str:
    """Render canonical Markdown as Slack mrkdwn."""

    soup = _markdown_soup(markdown)
    _linearize_tables(soup)
    return _render_nodes(soup, flavor="slack").strip()


def markdown_to_telegram_html(markdown: str) -> str:
    """Render canonical Markdown as Telegram's supported HTML subset."""

    soup = _markdown_soup(markdown)
    _linearize_tables(soup)
    return _render_nodes(soup, flavor="telegram").strip()


def markdown_to_chat_text(markdown: str) -> str:
    """Render Markdown for chat flavors using *, _, ~, and backticks."""

    soup = _markdown_soup(markdown)
    _linearize_tables(soup)
    return _render_nodes(soup, flavor="chat").strip()


def markdown_to_discord_markdown(markdown: str) -> str:
    """Render canonical Markdown as Discord's native Markdown dialect."""

    soup = _markdown_soup(markdown)
    _linearize_tables(soup)
    return _render_nodes(soup, flavor="discord").strip()


def _markdown_soup(markdown: str) -> BeautifulSoup:
    rendered = markdown_lib.markdown(
        normalize_gfm(markdown),
        extensions=list(_MARKDOWN_EXTENSIONS),
        output_format="html",
    )
    return BeautifulSoup(rendered, "html.parser")


def _normalize_list_indent(line: str) -> str:
    match = re.match(r"^( +)(?=(?:[-+*]|\d+[.)])\s+)", line)
    if not match:
        return line
    width = len(match.group(1))
    if width % 4 == 0:
        return line
    normalized = " " * (4 * max(1, round(width / 2)))
    return normalized + line[width:]


def _linearize_markdown_tables(markdown: str) -> str:
    lines = markdown.splitlines()
    result: list[str] = []
    index = 0
    in_fence = False
    while index < len(lines):
        if lines[index].lstrip().startswith("```"):
            in_fence = not in_fence
            result.append(lines[index])
            index += 1
            continue
        if (
            not in_fence
            and index + 1 < len(lines)
            and "|" in lines[index]
            and re.match(r"^\s*\|?\s*:?-{3,}", lines[index + 1])
        ):
            headers = _table_cells(lines[index])
            index += 2
            while index < len(lines) and "|" in lines[index] and lines[index].strip():
                values = _table_cells(lines[index])
                result.append(
                    "- "
                    + " · ".join(
                        f"**{header}:** {value}" if header else value
                        for header, value in zip(headers, values, strict=False)
                        if value
                    )
                )
                index += 1
            continue
        result.append(lines[index])
        index += 1
    return "\n".join(result)


def _table_cells(line: str) -> list[str]:
    return [cell.strip() for cell in line.strip().strip("|").split("|")]


def _linearize_tables(soup: BeautifulSoup) -> None:
    for table in list(soup.find_all("table")):
        rows = table.find_all("tr")
        if not rows:
            table.decompose()
            continue
        headers = [cell.get_text(" ", strip=True) for cell in rows[0].find_all(["th", "td"])]
        replacement = soup.new_tag("p")
        for row_index, row in enumerate(rows[1:] or rows):
            values = [cell.get_text(" ", strip=True) for cell in row.find_all(["th", "td"])]
            if row_index:
                replacement.append(soup.new_tag("br"))
            rendered = " · ".join(
                f"{header}: {value}" if header else value
                for header, value in zip(headers, values, strict=False)
                if value
            )
            replacement.append(NavigableString(rendered))
        table.replace_with(replacement)


def _render_nodes(soup: BeautifulSoup, *, flavor: str) -> str:
    return "".join(_render_node(node, flavor=flavor, depth=0) for node in soup.contents)


def _render_node(node: object, *, flavor: str, depth: int) -> str:
    if isinstance(node, NavigableString):
        return html.escape(str(node), quote=False) if flavor in {"slack", "telegram"} else str(node)
    if not isinstance(node, Tag):
        return ""
    name = node.name
    children = "".join(_render_node(child, flavor=flavor, depth=depth) for child in node.children)
    if name in {"script", "style", "img"}:
        return ""
    if name in {"strong", "b"}:
        return _wrap(children, flavor, strong=True)
    if name in {"em", "i"}:
        return _wrap(children, flavor, italic=True)
    if name in {"del", "s", "strike"}:
        return _wrap(children, flavor, strike=True)
    if name == "code" and node.parent and node.parent.name == "pre":
        return children
    if name == "code":
        if flavor == "telegram":
            return f"<code>{children}</code>"
        return children if flavor == "plain" else f"`{children}`"
    if name == "pre":
        language = ""
        code = node.find("code")
        if code:
            raw_classes = code.get("class")
            classes = list(raw_classes) if isinstance(raw_classes, list) else []
            language = next(
                (str(value)[9:] for value in classes if str(value).startswith("language-")),
                "",
            )
        if flavor == "telegram":
            return f"<pre><code>{children}</code></pre>\n"
        return f"```{language}\n{children.rstrip()}\n```\n"
    if name == "a":
        href = _safe_url(str(node.get("href") or ""))
        label = children or href
        if not href:
            return label
        if flavor == "slack":
            return f"<{href}|{label}>"
        if flavor == "telegram":
            return f'<a href="{html.escape(href, quote=True)}">{label}</a>'
        if flavor == "discord":
            return f"[{label}]({href})"
        return f"{label} ({href})" if label != href else href
    if name in {"h1", "h2", "h3", "h4", "h5", "h6"}:
        heading = _wrap(children, flavor, strong=True)
        return f"{heading}\n"
    if name == "blockquote":
        lines = children.strip().splitlines() or [children.strip()]
        if flavor == "telegram":
            return f"<blockquote>{'<br>'.join(lines)}</blockquote>\n"
        return "\n".join(f"> {line}" for line in lines) + "\n"
    if name in {"ul", "ol"}:
        rendered = "".join(
            _render_list_item(child, flavor=flavor, depth=depth, ordered=name == "ol", index=index)
            for index, child in enumerate(node.find_all("li", recursive=False), start=1)
        )
        return rendered
    if name == "li":
        return children
    if name == "br":
        return "<br>" if flavor == "telegram" else "\n"
    if name == "hr":
        return "—\n"
    if name in {"p", "div"}:
        suffix = "<br>" if flavor == "telegram" else "\n"
        return f"{children.strip()}{suffix}"
    return children


def _render_list_item(
    item: Tag,
    *,
    flavor: str,
    depth: int,
    ordered: bool,
    index: int,
) -> str:
    nested = item.find(["ul", "ol"], recursive=False)
    if nested:
        nested.extract()
    body = "".join(_render_node(child, flavor=flavor, depth=depth + 1) for child in item.children)
    marker = f"{index}." if ordered else "•"
    indent = "  " * depth
    line_break = "<br>" if flavor == "telegram" else "\n"
    rendered = f"{indent}{marker} {body.strip()}{line_break}"
    if nested:
        rendered += _render_node(nested, flavor=flavor, depth=depth + 1)
    return rendered


def _wrap(
    value: str,
    flavor: str,
    *,
    strong: bool = False,
    italic: bool = False,
    strike: bool = False,
) -> str:
    if flavor == "plain":
        return value
    if flavor == "telegram":
        tag = "b" if strong else "i" if italic else "s"
        return f"<{tag}>{value}</{tag}>"
    if flavor == "discord":
        marker = "**" if strong else "*" if italic else "~~"
    else:
        marker = "*" if strong else "_" if italic else "~"
    return f"{marker}{value}{marker}"


def _safe_url(value: str) -> str:
    if any(ord(character) < 32 or character in "<>|" for character in value):
        return ""
    parsed = urlparse(value)
    return value if parsed.scheme.lower() in _SAFE_LINK_SCHEMES else ""
