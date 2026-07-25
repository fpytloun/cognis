"""Standalone deliverable HTML/PDF rendering."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import html
import json
import re
import unicodedata
from dataclasses import dataclass
from datetime import datetime
from functools import cache
from pathlib import Path
from typing import Any, cast
from urllib.parse import unquote, unquote_to_bytes
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import markdown  # type: ignore[import-untyped]
from bs4 import BeautifulSoup, Comment

from cognis.rendering.rich_visuals import (
    MediaResolver,
    RenderTarget,
    ResolvedMedia,
    chart_rows,
    icon_symbol,
    media_reference,
    normalize_chart,
    render_chart_svg,
)
from cognis.ui_assets import resolve_standalone_manifest, standalone_asset_url

RENDERER_VERSION = "standalone-deliverable-v11"
HTML_CACHE_FILENAME = "render.html"
PDF_CACHE_FILENAME = "render.pdf"
PDF_INPUT_MAX_BYTES = 20 * 1024 * 1024
PDF_RENDER_TIMEOUT_SECONDS = 20.0

_ALLOWED_TAGS = {
    "a",
    "abbr",
    "aside",
    "b",
    "blockquote",
    "br",
    "caption",
    "code",
    "col",
    "colgroup",
    "dd",
    "del",
    "details",
    "div",
    "dl",
    "dt",
    "em",
    "figcaption",
    "figure",
    "h1",
    "h2",
    "h3",
    "h4",
    "h5",
    "h6",
    "hr",
    "i",
    "img",
    "ins",
    "kbd",
    "li",
    "mark",
    "ol",
    "p",
    "pre",
    "q",
    "s",
    "section",
    "small",
    "span",
    "strong",
    "sub",
    "summary",
    "sup",
    "svg",
    "table",
    "tbody",
    "td",
    "tfoot",
    "th",
    "thead",
    "tr",
    "u",
    "ul",
}
_ALLOWED_ATTRS = {
    "a": {"href", "title"},
    "img": {"alt", "src", "title"},
    "th": {"colspan", "rowspan"},
    "td": {"colspan", "rowspan"},
    "svg": {"viewbox", "role", "aria-label"},
    "*": {"class"},
}
_ALLOWED_HREF_PROTOCOLS = ("http://", "https://", "mailto:", "#")
_ALLOWED_SRC_PROTOCOLS = ("data:image/",)

_EMOJI_TEXT_SELECTOR = 0xFE0E
_EMOJI_SELECTOR = 0xFE0F
_EMOJI_ZWJ = 0x200D
_EMOJI_KEYCAP = 0x20E3
_EMOJI_CANCEL_TAG = 0xE007F
_EMOJI_FONT_PATH = Path(__file__).parent / "assets" / "noto-emoji" / "NotoColorEmoji.ttf"


class DeliverableRenderError(RuntimeError):
    """Client-safe deliverable render failure."""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


@dataclass(frozen=True)
class RenderedPdf:
    content: bytes


@dataclass(frozen=True)
class HeadingEntry:
    anchor: str
    title: str
    level: int


def _slug(value: str, *, fallback: str) -> str:
    normalized = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", "-", normalized.lower()).strip("-") or fallback


_MARKDOWN_FENCE_RE = re.compile(r"^[ \t]{0,3}(`{3,}|~{3,})")
_MARKDOWN_HEADING_RE = re.compile(r"^(#{1,4})\s+(.+?)\s*#*\s*$")


def _markdown_headings(value: str) -> list[tuple[int, str]]:
    """Extract `#`/`##`/`###`/`####` headings, skipping fenced code blocks.

    Mirrors the fence-tracking in the web renderer's
    `extractMarkdownHeadings` (ui/src/lib/markdown.ts) so a `#` line inside
    a ``` fenced code sample is never miscounted as a real heading by
    either renderer -- this matters here because the heading count feeds a
    TOC-enablement threshold (see `_wrapped_standalone_rich_payload` in
    cognis/api/routes/deliverables.py), and a code sample with a shell
    comment or Python `#` line could otherwise flip that threshold
    differently between the two renderers for the same content.
    """

    headings: list[tuple[int, str]] = []
    in_fence = False
    fence_char = ""
    fence_length = 0
    for line in value.splitlines():
        fence_match = _MARKDOWN_FENCE_RE.match(line)
        if fence_match:
            marker = fence_match.group(1)
            char, length = marker[0], len(marker)
            if not in_fence:
                in_fence, fence_char, fence_length = True, char, length
            elif char == fence_char and length >= fence_length:
                in_fence, fence_char, fence_length = False, "", 0
            continue
        if in_fence:
            continue
        heading_match = _MARKDOWN_HEADING_RE.match(line)
        if heading_match and heading_match.group(2).strip():
            headings.append((len(heading_match.group(1)), heading_match.group(2).strip()))
    return headings


_RESERVED_ID_PREFIXES = (
    "rich-section-",
    "cite-",
    "citation-",
    "rich-citation-",
    "reference-",
    "toc-",
    "figure-",
    "table-",
    "mermaid-",
)
_RESERVED_IDS = {"references-heading", "toc"}


class DocumentIdAllocator:
    """Allocate one collision-free HTML/PDF destination namespace."""

    def __init__(self) -> None:
        self._used: set[str] = set()

    def user(self, value: str, *, fallback: str = "section") -> str:
        base = _slug(value, fallback=fallback)
        if base in _RESERVED_IDS or base.startswith(_RESERVED_ID_PREFIXES):
            base = f"section-{base}"
        return self._allocate(base)

    def generated(self, value: str) -> str:
        return self._allocate(_slug(value, fallback="generated"))

    def _allocate(self, base: str) -> str:
        candidate = base
        suffix = 2
        while candidate in self._used:
            candidate = f"{base}-{suffix}"
            suffix += 1
        self._used.add(candidate)
        return candidate


class PublicationContext:
    """Deterministic document-wide navigation, numbering, and citation state."""

    def __init__(
        self,
        payload: dict[str, Any],
        *,
        media_resolver: MediaResolver | None = None,
        render_target: RenderTarget = "html",
    ) -> None:
        metadata = payload.get("metadata")
        self.metadata = metadata if isinstance(metadata, dict) else {}
        self.presentation = "pulse" if self.metadata.get("presentation") == "pulse" else "default"
        self.blocks = _object_list(payload.get("blocks"))
        self.assets = _object_list(payload.get("assets"))
        self.media_resolver = media_resolver
        self.render_target = render_target
        self.heading_ids: dict[int, str] = {}
        self.headings: list[HeadingEntry] = []
        self.markdown_headings: dict[int, list[tuple[int, HeadingEntry]]] = {}
        self.ids = DocumentIdAllocator()
        self.references_heading_id = self.ids.generated("references-heading")
        self.figure_numbers: dict[int, int] = {}
        self.table_numbers: dict[int, int] = {}
        self.sources = _deduplicate_sources(_object_list(payload.get("sources")))
        self.source_by_identity = {_source_identity(source): source for source in self.sources}
        self.citation_numbers: dict[str, int] = {}
        self.citation_backrefs: dict[str, list[str]] = {}
        self.citation_ids: dict[tuple[int, str], str] = {}
        self.reference_ids: dict[str, str] = {}
        self.toc_depth = _toc_depth(self.metadata)
        self.number_figures = _numbering_enabled(self.metadata, "figures")
        self.number_tables = _numbering_enabled(self.metadata, "tables")
        self._index_blocks(self.blocks)
        self._prepare_citations(self.blocks, self.sources)

    def _index_blocks(self, blocks: list[dict[str, Any]], *, level: int = 2) -> None:
        for block in blocks:
            title = _navigation_title(block)
            block_type = str(block.get("type") or "")
            if title and block_type not in {"hero", "divider"}:
                anchor = self.ids.user(
                    _text(block, "id") or _text(block, "anchor") or title,
                    fallback="section",
                )
                self.heading_ids[id(block)] = anchor
                self.headings.append(HeadingEntry(anchor=anchor, title=title, level=level))
                if block_type == "markdown":
                    markdown_headings = _markdown_headings(_block_content(block))
                    start = 0 if _block_title(block) else 1
                    nested = []
                    first_source_level = markdown_headings[0][0] if markdown_headings else 1
                    for index, (source_level, markdown_title) in enumerate(
                        markdown_headings[start:], start=start
                    ):
                        relative_level = (
                            source_level
                            if _block_title(block)
                            else max(1, source_level - first_source_level)
                        )
                        entry = HeadingEntry(
                            anchor=self.ids.user(markdown_title),
                            title=markdown_title,
                            level=min(4, level + relative_level),
                        )
                        nested.append((index, entry))
                        self.headings.append(entry)
                    self.markdown_headings[id(block)] = nested
            if block_type == "figure" and self.number_figures:
                self.figure_numbers[id(block)] = len(self.figure_numbers) + 1
            if (
                block_type in {"table", "comparison_matrix", "decision_matrix"}
                and self.number_tables
            ):
                self.table_numbers[id(block)] = len(self.table_numbers) + 1
            children = block.get("blocks") or block.get("children")
            if isinstance(children, list):
                self._index_blocks(_object_list(children), level=min(level + 1, 4))

    @property
    def show_toc(self) -> bool:
        if self.presentation == "pulse":
            return False
        override = _toc_override(self.metadata)
        top_level_headings = len([h for h in self.headings if h.level == 2])
        return (
            override
            if override is not None
            else _is_substantial_document(self.blocks, top_level_headings)
        )

    def heading(self, block: dict[str, Any], title: str) -> str:
        if not title:
            return ""
        anchor = self.heading_ids.get(id(block))
        id_attr = f' id="{html.escape(anchor, quote=True)}"' if anchor else ""
        level = self.heading_level(block)
        tabindex = ' tabindex="-1"' if anchor else ""
        return f"<h{level}{id_attr}{tabindex}>{html.escape(title)}</h{level}>"

    def heading_level(self, block: dict[str, Any]) -> int:
        anchor = self.heading_ids.get(id(block))
        return next((entry.level for entry in self.headings if entry.anchor == anchor), 2)

    def _prepare_citations(
        self,
        blocks: list[dict[str, Any]],
        inherited_sources: list[dict[str, Any]],
    ) -> None:
        for block in blocks:
            scoped_sources = _resolve_sources(block.get("sources"), inherited_sources)
            available = scoped_sources or inherited_sources
            block_type = str(block.get("type") or "")
            if block_type == "research_answer":
                self._plan_citation_group(
                    block,
                    _first_present(block, "source_ids", "citations"),
                    available,
                )
                items = _object_list(block.get("paragraphs") or block.get("items"))
            elif block_type in {"evidence_report", "claim_cards"}:
                items = _object_list(block.get("claims") or block.get("items") or block.get("data"))
            elif block_type in {"comparison_matrix", "decision_matrix"}:
                items = _object_list(block.get("rows") or block.get("data"))
            elif block_type in {"card", "callout", "status", "metric"}:
                items = [block]
            else:
                items = []
            for item in items:
                refs = _first_present(item, "source_ids", "citations", "sources")
                self._plan_citation_group(item, refs, available)
            if block_type in {"tabs", "accordion", "modal", "gallery"}:
                item_blocks = _object_list(
                    block.get("items") or block.get("data") or block.get("entries")
                )
                self._prepare_citations(item_blocks, available)
            children = block.get("blocks") or block.get("children")
            self._prepare_citations(_object_list(children), available)

    def _plan_citation_group(
        self,
        token: dict[str, Any],
        refs: object,
        available_sources: list[dict[str, Any]],
    ) -> None:
        seen: set[str] = set()
        for source in _resolve_sources(refs, available_sources):
            identity = _source_identity(source)
            if identity in seen:
                continue
            seen.add(identity)
            canonical = self.source_by_identity.setdefault(identity, source)
            if identity not in self.citation_numbers:
                self.citation_numbers[identity] = len(self.citation_numbers) + 1
                self.reference_ids[identity] = self.ids.generated(
                    f"reference-{self.citation_numbers[identity]}"
                )
            number = self.citation_numbers[identity]
            ref_id = self.ids.generated(
                f"cite-{number}-{len(self.citation_backrefs.get(identity, [])) + 1}"
            )
            self.citation_ids[(id(token), identity)] = ref_id
            self.citation_backrefs.setdefault(identity, []).append(ref_id)
            self.source_by_identity[identity] = canonical

    def cite(
        self,
        refs: object,
        available_sources: list[dict[str, Any]],
        *,
        token: dict[str, Any],
    ) -> str:
        resolved = _resolve_sources(refs, available_sources)
        missing = [
            source
            for source in resolved
            if _source_identity(source) not in self.citation_numbers
            or (id(token), _source_identity(source)) not in self.citation_ids
        ]
        if missing:
            self._plan_citation_group(token, refs, available_sources)
        links: list[str] = []
        seen: set[str] = set()
        for source in resolved:
            identity = _source_identity(source)
            if identity in seen:
                continue
            seen.add(identity)
            canonical = self.source_by_identity.setdefault(identity, source)
            number = self.citation_numbers[identity]
            ref_id = self.citation_ids[(id(token), identity)]
            links.append(
                f'<a class="citation" id="{ref_id}" href="#{self.reference_ids[identity]}" '
                f'aria-label="Reference {number}: {html.escape(_source_title(canonical), quote=True)}">'
                f"[{number}]</a>"
            )
        return f'<span class="citation-links">{"".join(links)}</span>' if links else ""


def deliverable_cache_key(row: Any) -> str:
    """Return the render cache key for a deliverable row."""

    payload = {
        "renderer": RENDERER_VERSION,
        "deliverable_id": getattr(row, "deliverable_id", ""),
        "version": int(getattr(row, "version", 0) or 0),
        "format": str(getattr(row, "format", "") or ""),
        "content_hash": str(getattr(row, "content_hash", "") or ""),
        "rich_hash": str(getattr(row, "rich_hash", "") or ""),
    }
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(raw).hexdigest()


def render_standalone_html(
    row: Any,
    *,
    download_pdf_url: str | None = None,
    media_resolver: MediaResolver | None = None,
    render_target: RenderTarget = "html",
) -> str:
    """Render a deliverable as a static, self-contained, sanitized HTML document."""

    title = str(getattr(row, "title", None) or "Deliverable")
    rich_payload = getattr(row, "rich_payload", None)
    metadata = (
        rich_payload.get("metadata")
        if isinstance(rich_payload, dict) and isinstance(rich_payload.get("metadata"), dict)
        else {}
    )
    presentation = "pulse" if metadata.get("presentation") == "pulse" else "default"
    raw_blocks = rich_payload.get("blocks") if isinstance(rich_payload, dict) else None
    density_blocks: list[dict[str, Any]] = (
        [block for block in raw_blocks if isinstance(block, dict)]
        if isinstance(raw_blocks, list)
        else []
    )
    density = _document_density(metadata, density_blocks)
    intro, body, has_toc = _render_document(
        row,
        media_resolver=media_resolver,
        render_target=render_target,
    )
    pdf_link = (
        f'<a class="action" data-download-pdf href="{html.escape(download_pdf_url, quote=True)}" '
        'aria-label="Download PDF" title="Download PDF">'
        '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 3v12m-4-4 4 4 4-4M5 20h14"/></svg></a>'
        if download_pdf_url
        else ""
    )
    toc_control = (
        '<button class="action toc-action" type="button" data-toc-toggle '
        'aria-label="Open table of contents" title="Open table of contents" '
        'aria-expanded="false" aria-controls="document-toc">'
        '<svg viewBox="0 0 24 24" aria-hidden="true">'
        '<path d="M4 6h2m3 0h11M4 12h2m3 0h11M4 18h2m3 0h11"/></svg></button>'
        if has_toc
        else ""
    )
    body = _substitute_emoji(body)
    intro = _substitute_emoji(intro)
    stylesheet = _CSS.replace(
        "__EMOJI_FONT_SOURCE__",
        (
            "url('cognis-asset:emoji-font') format('truetype'), "
            f"url({_emoji_font_data_url()}) format('truetype')"
            if 'class="emoji"' in body + intro
            else "local('sans-serif')"
        ),
    )
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{html.escape(title)}</title>
  <script data-cognis-runtime="theme-bootstrap">{_STANDALONE_THEME_BOOTSTRAP}</script>
  <style>{stylesheet}</style>
</head>
<body class="presentation-{presentation}" data-rich-density="{density}">
  <main class="page">
    <article class="document">
      <header class="document-header">
        <div>{intro}</div>
        <nav aria-label="Document actions">{toc_control}{pdf_link}</nav>
      </header>
      {body}
    </article>
  </main>
  <script data-cognis-runtime="interactions">{_STANDALONE_INTERACTIONS}</script>
</body>
</html>"""


def render_standalone_shell(
    row: Any,
    *,
    media_base: str,
    standalone_url: str,
    pdf_url: str,
    rich_payload_override: dict[str, Any] | None = None,
) -> str:
    """Render a client-mounted standalone shell backed by same-origin assets.

    `rich_payload_override`, when given, is used for the hydrated payload
    instead of `row.rich_payload` -- this is how non-rich (markdown/plain/
    html) deliverables get the same SvelteKit-hydrated RichDeliverable shell
    as rich payloads (see `_try_render_standalone_response` in
    `cognis/api/routes/deliverables.py`), by wrapping their content as a
    single block. The `<noscript>` fallback below still renders from `row`
    directly via the existing `_render_document`/Python renderer dispatch,
    which already handles every format on its own.
    """

    manifest = resolve_standalone_manifest()
    if manifest is None:
        raise DeliverableRenderError("standalone_assets_unavailable")

    title = str(getattr(row, "title", None) or "Deliverable")
    content = str(getattr(row, "content", "") or "")
    rich_payload = (
        rich_payload_override
        if rich_payload_override is not None
        else getattr(row, "rich_payload", None)
    )
    payload = rich_payload if isinstance(rich_payload, dict) else {}
    description = _standalone_description(payload, content)
    inert_payload = html.escape(
        json.dumps(
            {
                "content": content,
                "instanceId": str(getattr(row, "deliverable_id", "") or ""),
                "payload": payload,
                "title": title,
            },
            ensure_ascii=False,
            separators=(",", ":"),
        ),
        quote=False,
    )
    intro, body, _has_toc = _render_document(row)
    semantic_fallback = _substitute_emoji(f"{intro}{body}")
    styles = "\n".join(
        f'  <link rel="stylesheet" href="{html.escape(standalone_asset_url(path), quote=True)}">'
        for path in manifest.styles
    )
    script_url = standalone_asset_url(manifest.script)
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{html.escape(title)}</title>
  <meta name="description" content="{html.escape(description, quote=True)}">
  <meta property="og:type" content="article">
  <meta property="og:title" content="{html.escape(title, quote=True)}">
  <meta property="og:description" content="{html.escape(description, quote=True)}">
  <meta property="og:url" content="{html.escape(standalone_url, quote=True)}">
{styles}
</head>
<body>
  <template id="cognis-deliverable-payload"
    data-media-base="{html.escape(media_base, quote=True)}"
    data-pdf-url="{html.escape(pdf_url, quote=True)}"
    data-standalone-url="{html.escape(standalone_url, quote=True)}">{inert_payload}</template>
  <div id="cognis-deliverable-root"></div>
  <noscript><main class="cognis-noscript"><article>{semantic_fallback}</article></main></noscript>
  <script type="module" src="{html.escape(script_url, quote=True)}"></script>
</body>
</html>"""


def _standalone_description(payload: dict[str, Any], content: str) -> str:
    metadata = payload.get("metadata")
    if isinstance(metadata, dict):
        for key in ("description", "subtitle", "summary"):
            value = metadata.get(key)
            if isinstance(value, str) and value.strip():
                return _truncate_description(value)
    return _truncate_description(content or "Cognis rich deliverable")


def _truncate_description(value: str) -> str:
    normalized = " ".join(value.split())
    return normalized[:197].rstrip() + ("…" if len(normalized) > 197 else "")


async def render_pdf_bytes(html_document: str) -> RenderedPdf:
    """Render PDF bytes from standalone HTML in a worker thread with guardrails."""

    if len(html_document.encode("utf-8")) > PDF_INPUT_MAX_BYTES:
        raise DeliverableRenderError("render_input_too_large")

    try:
        content = await asyncio.wait_for(
            asyncio.to_thread(_render_pdf_sync, html_document),
            timeout=PDF_RENDER_TIMEOUT_SECONDS,
        )
    except TimeoutError as exc:
        raise DeliverableRenderError("render_timeout") from exc
    except DeliverableRenderError:
        raise
    except Exception as exc:
        raise DeliverableRenderError(f"pdf_render_failed: {exc.__class__.__name__}") from exc
    if not content.startswith(b"%PDF") or len(content) < 100:
        raise DeliverableRenderError("pdf_render_failed: invalid_pdf_output")
    return RenderedPdf(content=content)


def _render_pdf_sync(html_document: str) -> bytes:
    from weasyprint import HTML  # type: ignore[import-untyped]
    from weasyprint.text.fonts import FontConfiguration  # type: ignore[import-untyped]

    font_config = FontConfiguration()
    return cast(
        bytes,
        HTML(string=html_document, url_fetcher=_blocked_url_fetcher).write_pdf(
            font_config=font_config
        ),
    )


def _blocked_url_fetcher(url: str) -> dict[str, object]:
    if url == "cognis-asset:emoji-font":
        return {
            "string": _EMOJI_FONT_PATH.read_bytes(),
            "mime_type": "font/ttf",
        }
    font_match = re.match(r"^data:font/ttf;base64,(.+)$", url, re.I | re.S)
    if font_match:
        return {
            "string": base64.b64decode(font_match.group(1), validate=True),
            "mime_type": "font/ttf",
        }
    safe_source = _safe_image_src(url)
    if safe_source and safe_source.lower().startswith("data:image/svg+xml,"):
        return {
            "string": unquote_to_bytes(safe_source.split(",", 1)[1]),
            "mime_type": "image/svg+xml",
        }
    match = re.match(
        r"^data:(image/(?:png|jpeg|gif|webp));base64,(.+)$",
        safe_source,
        re.I | re.S,
    )
    if match:
        return {
            "string": base64.b64decode(match.group(2), validate=True),
            "mime_type": match.group(1).lower(),
        }
    raise ValueError(f"external resource loading is disabled: {url}")


_DENSITY_SIGNAL_TYPES = frozenset(
    {
        "dashboard",
        "status",
        "status_grid",
        "metric",
        "kv",
        "key_value",
        "table",
        "comparison_matrix",
        "decision_matrix",
        "chart",
        "incident_timeline",
        "incident_checklist",
        "checklist",
    }
)


def _document_density(metadata: object, blocks: list[dict[str, Any]]) -> str:
    """Mirror of the web renderer's richDensity() (rich-deliverable.ts):
    a spacing-rhythm heuristic, not an authoring requirement. An explicit
    metadata.density always wins; otherwise a composition dominated by
    dashboard/metric/table/status blocks reads "dense" (tighter rhythm)
    while a composition dominated by prose/research blocks stays "airy"
    (the default, more generous rhythm)."""
    meta = metadata if isinstance(metadata, dict) else {}
    explicit = meta.get("density")
    if explicit == "dense":
        return "dense"
    if explicit == "airy":
        return "airy"
    total = 0
    signals = 0

    def visit(block: dict[str, Any]) -> None:
        nonlocal total, signals
        total += 1
        if str(block.get("type") or "") in _DENSITY_SIGNAL_TYPES:
            signals += 1
        children = (
            block.get("blocks") if isinstance(block.get("blocks"), list) else block.get("children")
        )
        if not isinstance(children, list) and str(block.get("type") or "") in {
            "accordion",
            "gallery",
            "modal",
            "tabs",
        }:
            children = block.get("items")
        if isinstance(children, list):
            for child in children:
                if isinstance(child, dict):
                    visit(child)

    for block in blocks:
        if isinstance(block, dict):
            visit(block)
    if total == 0:
        return "airy"
    return "dense" if signals >= 3 and signals / total >= 0.35 else "airy"


def _render_document(
    row: Any,
    *,
    media_resolver: MediaResolver | None = None,
    render_target: RenderTarget = "html",
) -> tuple[str, str, bool]:
    format_name = str(getattr(row, "format", "") or "markdown").lower()
    content = str(getattr(row, "content", "") or "")
    title = str(getattr(row, "title", None) or "Deliverable")
    if format_name == "rich":
        payload = getattr(row, "rich_payload", None)
        if isinstance(payload, dict):
            blocks = payload.get("blocks")
            if isinstance(blocks, list) and blocks:
                valid_blocks = [block for block in blocks if isinstance(block, dict)]
                payload_sources = payload.get("sources")
                sources = (
                    [source for source in payload_sources if isinstance(source, dict)]
                    if isinstance(payload_sources, list)
                    else []
                )
                context = PublicationContext(
                    payload,
                    media_resolver=media_resolver,
                    render_target=render_target,
                )
                first = valid_blocks[0] if valid_blocks else None
                if first and str(first.get("type") or "") == "hero":
                    intro = _render_intro(first, fallback_title=title)
                    hero_content = (
                        _markdown_to_html(_block_content(first)) if _block_content(first) else ""
                    )
                    hero_children = hero_content + _render_children(
                        first, sources=sources, context=context
                    )
                    valid_blocks = valid_blocks[1:]
                else:
                    intro = _render_intro(
                        {}, fallback_title=title, metadata=payload.get("metadata")
                    )
                    hero_children = ""
                body = hero_children + "".join(
                    _render_block(block, sources=sources, context=context) for block in valid_blocks
                )
                toc = _render_toc(context) if context.show_toc else ""
                bibliography = _render_bibliography(context)
                return (
                    intro,
                    f'<div class="document-layout">{toc}'
                    f'<div class="document-content">{body}{bibliography}</div></div>',
                    context.show_toc,
                )
        return _render_intro({}, fallback_title=title), _markdown_to_html(content), False
    if format_name == "html":
        return _render_intro({}, fallback_title=title), sanitize_html(content), False
    if format_name == "plain":
        return _render_intro({}, fallback_title=title), f"<pre>{html.escape(content)}</pre>", False
    return _render_intro({}, fallback_title=title), _markdown_to_html(content), False


def _render_intro(
    block: dict[str, Any],
    *,
    fallback_title: str,
    metadata: object = None,
) -> str:
    meta = metadata if isinstance(metadata, dict) else {}
    title = _block_title(block) or _text(meta, "title") or fallback_title
    subtitle = _text(block, "subtitle") or _text(meta, "subtitle")
    eyebrow = _text(block, "eyebrow") or _text(meta, "eyebrow")
    badges = block.get("tags") or block.get("badges") or meta.get("badges")
    badge_html = ""
    if isinstance(badges, list):
        badge_html = (
            '<p class="badges">'
            + "".join(
                f"<span>{html.escape(str(value))}</span>" for value in badges if str(value).strip()
            )
            + "</p>"
        )
    subtitle_html = f'<p class="subtitle">{html.escape(subtitle)}</p>' if subtitle else ""
    eyebrow_html = f'<p class="eyebrow">{html.escape(eyebrow)}</p>' if eyebrow else ""
    return f"{eyebrow_html}<h1>{html.escape(title)}</h1>{subtitle_html}{badge_html}"


def _render_block(
    block: dict[str, Any],
    *,
    sources: list[dict[str, Any]] | None = None,
    context: PublicationContext | None = None,
) -> str:
    available_sources = sources or []
    block_type = str(block.get("type") or "section")
    title = _block_title(block)
    content = _block_content(block)
    child_html = _render_children(block, sources=available_sources, context=context)
    heading = context.heading(block, title) if context else _heading(title)
    if block_type == "markdown":
        if context:
            markdown_html = _markdown_with_publication_heading(content, block, context)
            if not title:
                heading = ""
        else:
            markdown_html = _markdown_to_html(content)
        return f'<section class="block block-{html.escape(block_type)}">{heading}{markdown_html}{child_html}</section>'
    if block_type == "research_answer":
        return _render_research_answer(
            block, sources=available_sources, child_html=child_html, context=context
        )
    if block_type in {"evidence_report", "claim_cards"}:
        return _render_evidence_report(
            block, sources=available_sources, child_html=child_html, context=context
        )
    if block_type in {"section", "stack", "columns", "grid", "card_grid"}:
        subtitle = _text(block, "subtitle") or _text(block, "description")
        columns_style = (
            _explicit_columns_style(block) if block_type in {"columns", "grid", "card_grid"} else ""
        )
        return (
            f'<section class="block block-{html.escape(block_type)}"{columns_style}>'
            f"{_eyebrow(block)}{heading}"
            f"{f'<p class="subtitle">{html.escape(subtitle)}</p>' if subtitle else ''}"
            f"{_markdown_to_html(content)}{child_html}</section>"
        )
    if block_type in {"tabs", "accordion", "modal", "gallery"}:
        return _render_item_container(
            block,
            block_type=block_type,
            sources=available_sources,
            child_html=child_html,
            context=context,
        )
    if block_type in {"card", "callout", "action"}:
        # "action" is a standalone block type with no dedicated visual
        # treatment of its own -- it shares the existing card "action"
        # variant (a compact, accent-bordered call-to-action card). Default
        # the variant only when the author didn't already pick one, and
        # render through the "card" wrapper class so the existing
        # `.block-card.card-variant-action` styling applies.
        if block_type == "action":
            if not isinstance(block.get("variant"), str):
                block = {**block, "variant": "action"}
            wrapper_type = "card"
        else:
            wrapper_type = block_type
        return _render_card(
            block,
            block_type=wrapper_type,
            heading=heading,
            content=content,
            child_html=child_html,
            available_sources=available_sources,
            context=context,
        )
    if block_type in {"dashboard", "status", "status_grid"}:
        description = _text(block, "description") or _text(block, "subtitle")
        status = _scalar_text(block.get("status"))
        status_html = f'<p class="status-value">{html.escape(status)}</p>' if status else ""
        return (
            f'<section class="block block-{html.escape(block_type)}">'
            f"{_eyebrow(block)}{heading}"
            f"{_markdown_to_html(content or description)}{status_html}"
            f"{_render_dashboard_items(block)}"
            f"{f'<div class="dashboard-blocks">{child_html}</div>' if child_html else ''}</section>"
        )
    if block_type == "metric":
        label = _text(block, "label") or title
        value = _scalar_text(block.get("value"))
        if not value:
            value = content
        unit = _scalar_text(block.get("unit"))
        delta = _scalar_text(block.get("delta")) or _scalar_text(block.get("trend"))
        description = _text(block, "description") or _text(block, "summary") or _text(block, "dek")
        unit_html = f'<span class="metric-unit">{html.escape(unit)}</span>' if unit else ""
        delta_html = f'<p class="metric-delta">{html.escape(delta)}</p>' if delta else ""
        description_html = (
            f'<p class="metric-description">{html.escape(description)}</p>' if description else ""
        )
        return (
            '<section class="block block-metric">'
            f'<p class="metric-label">{html.escape(label)}</p>'
            f'<p class="metric-value">{html.escape(value)}{unit_html}</p>'
            f"{delta_html}{description_html}{child_html}</section>"
        )
    if block_type == "quote":
        byline = _text(block, "byline") or _text(block, "author")
        quote = (
            _text(block, "quote")
            or _text(block, "content")
            or _text(block, "text")
            or _text(block, "body")
        )
        footer = f"<footer>— {html.escape(byline)}</footer>" if byline else ""
        return (
            '<blockquote class="block block-quote">'
            f"<p>{html.escape(quote)}</p>{footer}{child_html}</blockquote>"
        )
    if block_type == "hero":
        subtitle = _text(block, "subtitle")
        body = _markdown_to_html(content)
        # Mirrors the web renderer's hero media support (a hero can carry a
        # banner image, e.g. an agent-generated cover for a published
        # article, via the same media object shape as figure/card). The web
        # renderer overlays it as a full-bleed background behind the title
        # with a legibility gradient; this print/fallback path keeps it
        # simpler -- a plain banner figure above the title, using the same
        # `_render_card_media` resolution (authorized ref/artifact_id/
        # content_ref, placement, credit) already used by card blocks -- to
        # limit the added rendering complexity in a rarely-served path that
        # also feeds PDF export, where a light, print-conventional layout is
        # preferred over a dark overlay treatment.
        media_html = _render_card_media(block, context=context)
        return (
            '<section class="block block-section block-hero-section">'
            f"{media_html}{_eyebrow(block)}{_heading(title)}"
            f"{f'<p class="subtitle">{html.escape(subtitle)}</p>' if subtitle else ''}"
            f"{_render_badges(block.get('tags') or block.get('badges'))}"
            f"{body}{child_html}</section>"
        )
    if block_type in {"kv", "key_value"}:
        return (
            '<section class="block block-kv">'
            f"{heading}{_render_items_as_definitions(block)}{child_html}</section>"
        )
    if block_type in {"timeline", "steps"}:
        # `steps` shares the timeline renderer but adds a `block-steps`
        # modifier class so CSS can give it a numbered-circle marker instead
        # of a plain time/step label -- the same distinction the web
        # renderer's `.rich-steps` modifier makes on `.rich-timeline`.
        wrapper_class = (
            "block block-timeline block-steps" if block_type == "steps" else "block block-timeline"
        )
        return (
            f'<section class="{wrapper_class}">'
            f"{heading}{_render_timeline(block)}{child_html}</section>"
        )
    if block_type == "day_agenda":
        variant = _class_token(block.get("variant"), fallback="timeline")
        return (
            f'<section class="block block-day-agenda" data-variant="{variant}">'
            f"{heading}{_render_day_agenda(block)}{child_html}</section>"
        )
    if block_type in {"incident_timeline", "checklist", "incident_checklist"}:
        return _render_incident(block, child_html=child_html, heading=heading)
    if block_type in {"source_list"}:
        if context and context.presentation == "pulse" and context.citation_numbers:
            return ""
        source_key = next(
            (key for key in ("sources", "source_ids", "citations") if key in block),
            None,
        )
        block_sources = block.get(source_key) if source_key is not None else None
        resolved_sources = (
            _resolve_sources(block_sources, available_sources)
            if block_sources is not None
            else available_sources
        )
        return (
            '<section class="block block-sources">'
            f"{heading}{_render_sources(resolved_sources)}{child_html}</section>"
        )
    if block_type in {"comparison_matrix", "decision_matrix"}:
        number = context.table_numbers.get(id(block)) if context else None
        return (
            f'<section class="block block-{html.escape(block_type)}">{heading}'
            f"{_render_matrix(block, sources=available_sources, number=number, context=context)}{child_html}</section>"
        )
    if block_type == "table":
        number = context.table_numbers.get(id(block)) if context else None
        return (
            f'<section class="block block-{html.escape(block_type)}">{heading}'
            f"{_render_table(block, number=number)}{child_html}</section>"
        )
    if block_type == "chart":
        return (
            '<section class="block block-chart">'
            f"{heading}{_render_chart(block)}{child_html}</section>"
        )
    if block_type == "mermaid":
        source = _text(block, "source") or _text(block, "code") or content
        return (
            '<section class="block block-mermaid">'
            f"{heading}<pre>{html.escape(source)}</pre>{child_html}</section>"
        )
    if block_type == "code":
        language = _text(block, "language") or _text(block, "lang")
        language_html = f'<p class="code-language">{html.escape(language)}</p>' if language else ""
        return (
            '<section class="block block-code">'
            f"{heading}{language_html}<pre><code>{html.escape(content)}</code></pre>"
            f"{child_html}</section>"
        )
    if block_type in {"link", "link_preview"}:
        return _render_link(block, child_html=child_html, heading=heading)
    if block_type == "figure":
        number = context.figure_numbers.get(id(block)) if context else None
        return _render_figure(
            block,
            child_html=child_html,
            heading=heading,
            number=number,
            context=context,
        )
    if block_type == "divider":
        return f'<section class="block block-divider"><hr>{child_html}</section>'
    return f'<section class="block block-{html.escape(block_type)}">{heading}{_markdown_to_html(content) if content else _render_key_values(block)}{child_html}</section>'


def _heading(title: str) -> str:
    return f"<h2>{html.escape(title)}</h2>" if title else ""


def _text(mapping: dict[str, Any], key: str) -> str:
    value = mapping.get(key)
    return value if isinstance(value, str) else ""


def _first_present(mapping: dict[str, Any], *keys: str) -> object:
    for key in keys:
        if key in mapping and mapping[key] is not None:
            return mapping[key]
    return None


def _scalar_text(value: object) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (str, int, float)):
        return str(value)
    return ""


def _class_token(value: object, *, fallback: str) -> str:
    token = re.sub(r"[^a-z0-9_-]+", "-", _scalar_text(value).strip().lower()).strip("-")
    return token or fallback


def _explicit_columns(block: dict[str, Any]) -> int:
    """Return an author-specified column count (1-4), or 0 for auto-fit.

    Mirrors the web renderer's column-count behavior: an unset/invalid value
    must resolve to 0 (not clamped up to 1), so the caller can fall back to
    the CSS auto-fit default instead of forcing a single column.
    """

    layout = block.get("layout")
    layout_columns = layout.get("columns") if isinstance(layout, dict) else None
    raw = block.get("columns", layout_columns)
    try:
        value = int(raw)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0
    return max(1, min(4, value)) if value > 0 else 0


def _explicit_columns_style(block: dict[str, Any]) -> str:
    """Return an inline `style` attribute forcing an explicit column count.

    Deliberately NOT a CSS custom property: WeasyPrint's grid layout raises
    `TypeError: 'FunctionBlock' object is not subscriptable` on
    `repeat(var(--x, auto-fit), minmax(...))`. An inline
    `grid-template-columns` override (which always wins over the class rule
    regardless of specificity) avoids `var()` inside `repeat()`/`minmax()`
    entirely while still letting an explicit author column count take
    effect in both the static/web HTML and the PDF export.
    """

    columns = _explicit_columns(block)
    if not columns:
        return ""
    return f' style="grid-template-columns: repeat({columns}, minmax(0, 1fr))"'


def _render_card(
    block: dict[str, Any],
    *,
    block_type: str,
    heading: str,
    content: str,
    child_html: str,
    available_sources: list[dict[str, Any]],
    context: PublicationContext | None,
) -> str:
    variant = _class_token(block.get("variant"), fallback="compact")
    tone = _class_token(block.get("tone"), fallback="neutral")
    description = _text(block, "description") or _text(block, "summary") or _text(block, "subtitle")
    dek = _text(block, "dek")
    icon = icon_symbol(block.get("icon") or block.get("emoji"))
    refs = block.get("citations") or block.get("source_ids") or block.get("sources")
    resolved_sources = _resolve_sources(refs, available_sources)
    href = _safe_href(_text(block, "href") or _text(block, "url"))
    if not href and variant in {"editorial", "feature", "visual"} and len(resolved_sources) == 1:
        href = _safe_href(_text(resolved_sources[0], "url") or _text(resolved_sources[0], "href"))
    title = _block_title(block)
    if href and title and html.escape(title) in heading:
        linked_title = (
            f'<a class="card-headline-link" href="{html.escape(href, quote=True)}">'
            f"{html.escape(title)}</a>"
        )
        heading = heading.replace(html.escape(title), linked_title, 1)
    citation_token = (
        block["_citation_token"] if isinstance(block.get("_citation_token"), dict) else block
    )
    citations = (
        context.cite(refs, available_sources, token=citation_token)
        if context and refs is not None
        else _render_source_links(refs, available_sources)
    )
    icon_html = (
        f'<span class="card-icon" aria-hidden="true">{html.escape(icon)}</span>' if icon else ""
    )
    dek_html = f'<p class="card-dek">{html.escape(dek)}</p>' if dek else ""
    body = _markdown_to_html(content or description)
    media_html = _render_card_media(
        block,
        context=context,
        placement_override="background" if variant == "visual" else None,
    )
    rendered_variant = variant if variant != "visual" or media_html else "feature"
    return (
        f'<section class="block block-{html.escape(block_type)} '
        f'card-variant-{rendered_variant} tone-{tone}">'
        f"{media_html}{icon_html}{heading}{dek_html}{body}{citations}{child_html}</section>"
    )


def _render_card_media(
    block: dict[str, Any],
    *,
    context: PublicationContext | None,
    placement_override: str | None = None,
) -> str:
    raw_media = block.get("media")
    media = raw_media if isinstance(raw_media, dict) else {}
    reference = media_reference(block, context.assets) if context else None
    resolved = (
        context.media_resolver(reference, context.render_target)
        if context and context.media_resolver and reference
        else None
    )
    source = _safe_resolved_media(resolved) or _safe_image_src(
        _scalar_text(media.get("src") or media.get("url") or media.get("href"))
    )
    if not source:
        return ""
    alt = _scalar_text(media.get("alt")) or _block_title(block)
    credit = _scalar_text(media.get("credit"))
    width = _positive_dimension(media.get("width"))
    height = _positive_dimension(media.get("height"))
    dimensions = (
        f' width="{width}" height="{height}"' if width is not None and height is not None else ""
    )
    placement = placement_override or _class_token(media.get("placement"), fallback="top")
    if placement not in {"top", "background", "leading"}:
        placement = "top"
    caption = f"<figcaption>{html.escape(credit)}</figcaption>" if credit else ""
    return (
        f'<figure class="card-media media-placement-{placement}">'
        f'<img src="{html.escape(source, quote=True)}" alt="{html.escape(alt, quote=True)}"'
        f'{dimensions} loading="eager" decoding="sync">'
        f"{caption}</figure>"
    )


def _positive_dimension(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        dimension = int(value)
    except (TypeError, ValueError):
        return None
    return dimension if 0 < dimension <= 10000 else None


def _block_title(block: dict[str, Any]) -> str:
    return _text(block, "title") or _text(block, "label") or _text(block, "name")


def _navigation_title(block: dict[str, Any]) -> str:
    title = _block_title(block)
    if title or str(block.get("type") or "") != "markdown":
        return title
    match = re.search(r"^#{1,4}\s+(.+?)\s*#*\s*$", _block_content(block), re.MULTILINE)
    return match.group(1).strip() if match else ""


def _block_content(block: dict[str, Any]) -> str:
    # `summary` is accepted as a body-text alias for blocks with no separate
    # summary display slot (e.g. callout, action). Blocks that already show
    # `dek`/`description` separately (card, dashboard, metric) pop or read
    # those keys independently before/around this call, so this fallback
    # must not include them here or the same text would render twice.
    return (
        _text(block, "content")
        or _text(block, "text")
        or _text(block, "body")
        or _text(block, "summary")
    )


def _object_list(value: object) -> list[dict[str, Any]]:
    return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []


def _toc_override(metadata: dict[str, Any]) -> bool | None:
    value = metadata.get("toc")
    if isinstance(value, bool):
        return value
    if isinstance(value, dict) and isinstance(value.get("enabled"), bool):
        return bool(value["enabled"])
    show_toc = metadata.get("show_toc")
    return show_toc if isinstance(show_toc, bool) else None


def _document_substance(value: object) -> tuple[int, int]:
    if isinstance(value, str):
        return len(value.strip()), 0
    if isinstance(value, list):
        totals = [_document_substance(item) for item in value]
        return sum(item[0] for item in totals), sum(item[1] for item in totals)
    if not isinstance(value, dict):
        return 0, 0
    structure = int(
        str(value.get("type") or "")
        in {"table", "comparison_matrix", "decision_matrix", "figure", "chart", "code", "mermaid"}
    )
    characters = 0
    structures = structure
    for key, item in value.items():
        if key in {"id", "anchor", "type", "variant", "url", "src"}:
            continue
        nested_characters, nested_structures = _document_substance(item)
        characters += nested_characters
        structures += nested_structures
    return characters, structures


def _is_substantial_document(blocks: list[dict[str, Any]], top_level_headings: int) -> bool:
    if top_level_headings < 4:
        return False
    characters, structures = _document_substance(blocks)
    return (
        characters >= 4_000
        or (characters >= 2_400 and structures >= 2)
        or (characters >= 1_500 and structures >= 4)
        or (top_level_headings >= 10 and characters >= 3_000)
    )


def _toc_depth(metadata: dict[str, Any]) -> int:
    value = metadata.get("toc")
    raw_depth = value.get("depth") if isinstance(value, dict) else metadata.get("toc_depth")
    return 4 if raw_depth == 4 else 3 if raw_depth == 3 else 2


def _numbering_enabled(metadata: dict[str, Any], kind: str) -> bool:
    if metadata.get("presentation") == "pulse":
        return False
    publication = metadata.get("publication")
    if isinstance(publication, dict):
        value = publication.get(f"number_{kind}", publication.get("numbering"))
        if isinstance(value, bool):
            return value
    value = metadata.get(f"number_{kind}")
    if isinstance(value, bool):
        return value
    return publication if isinstance(publication, bool) else False


def _source_title(source: dict[str, Any]) -> str:
    return (
        _block_title(source) or _text(source, "url") or _text(source, "href") or "Untitled source"
    )


def _source_identity(source: dict[str, Any]) -> str:
    explicit = _scalar_text(source.get("id") or source.get("key") or source.get("citation_id"))
    if explicit:
        return f"id:{explicit.strip().lower()}"
    doi = _normalize_doi(_text(source, "doi"))
    if doi:
        return f"doi:{doi.lower()}"
    url = _safe_href(_text(source, "url") or _text(source, "href"))
    if url:
        return f"url:{url.rstrip('/').lower()}"
    authors = _source_authors(source)
    year = _scalar_text(source.get("year"))
    return f"meta:{authors.lower()}|{_source_title(source).lower()}|{year}"


def _deduplicate_sources(sources: list[dict[str, Any]]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for source in sources:
        identity = _source_identity(source)
        if identity not in seen:
            seen.add(identity)
            result.append(source)
    return result


def _source_authors(source: dict[str, Any]) -> str:
    value = source.get("authors", source.get("author"))
    if isinstance(value, list):
        return ", ".join(str(item) for item in value if str(item).strip())
    return _scalar_text(value)


def _normalize_doi(value: str) -> str:
    return re.sub(r"^(?:https?://doi\.org/|doi:\s*)", "", value.strip(), flags=re.IGNORECASE)


def _render_toc(context: PublicationContext) -> str:
    entries = [entry for entry in context.headings if entry.level <= context.toc_depth]
    roots: list[tuple[HeadingEntry, list[Any]]] = []
    stack: list[tuple[HeadingEntry, list[Any]]] = []
    for entry in entries:
        node: tuple[HeadingEntry, list[Any]] = (entry, [])
        while stack and stack[-1][0].level >= entry.level:
            stack.pop()
        target = stack[-1][1] if stack else roots
        target.append(node)
        stack.append(node)

    def render_nodes(nodes: list[tuple[HeadingEntry, list[Any]]]) -> str:
        items = []
        for entry, children in nodes:
            nested = render_nodes(children) if children else ""
            items.append(
                f'<li data-level="{entry.level}">'
                f'<a href="#{html.escape(entry.anchor, quote=True)}">'
                f"{html.escape(entry.title)}</a>{nested}</li>"
            )
        return f"<ol>{''.join(items)}</ol>"

    return (
        '<button class="toc-backdrop" type="button" data-toc-backdrop '
        'aria-label="Close table of contents"></button>'
        '<aside class="document-toc" id="document-toc" aria-label="Table of contents">'
        '<header class="toc-drawer-header"><strong>Contents</strong>'
        '<button class="action" type="button" data-toc-close '
        'aria-label="Close table of contents">'
        '<svg viewBox="0 0 24 24" aria-hidden="true">'
        '<path d="m6 6 12 12M18 6 6 18"/></svg></button></header>'
        '<nav aria-label="Table of contents"><h2 class="toc-title">Contents</h2>'
        f"{render_nodes(roots)}</nav></aside>"
    )


@cache
def _emoji_font_data_url() -> str:
    """Return the vendored OFL Noto Emoji font as an offline data URL."""

    encoded = base64.b64encode(_EMOJI_FONT_PATH.read_bytes()).decode("ascii")
    return f"data:font/ttf;base64,{encoded}"


def _emoji_markup(sequence: str) -> str:
    """Render one intact emoji grapheme with the vendored monochrome font."""

    accessible = html.escape(sequence, quote=True)
    escaped = html.escape(sequence)
    return (
        f'<span class="emoji" role="img" aria-label="{accessible}">'
        f'<span class="emoji-glyph" aria-hidden="true">{escaped}</span></span>'
    )


def _is_emoji_base(value: int) -> bool:
    return (
        0x1F000 <= value <= 0x1FAFF
        or 0x2300 <= value <= 0x23FF
        or 0x2600 <= value <= 0x27BF
        or value
        in {
            0x00A9,
            0x00AE,
            0x203C,
            0x2049,
            0x2122,
            0x2139,
            0x2194,
            0x2195,
            0x2196,
            0x2197,
            0x2198,
            0x2199,
            0x21A9,
            0x21AA,
            0x3030,
            0x303D,
            0x3297,
            0x3299,
        }
    )


def _is_default_emoji_presentation(value: int) -> bool:
    return 0x1F000 <= value <= 0x1FAFF or value in {
        0x231A,
        0x231B,
        0x23E9,
        0x23EA,
        0x23EB,
        0x23EC,
        0x23F0,
        0x23F3,
        0x25FD,
        0x25FE,
        0x2614,
        0x2615,
        0x2648,
        0x2649,
        0x264A,
        0x264B,
        0x264C,
        0x264D,
        0x264E,
        0x264F,
        0x2650,
        0x2651,
        0x2652,
        0x2653,
        0x267F,
        0x2693,
        0x26A1,
        0x26AA,
        0x26AB,
        0x26BD,
        0x26BE,
        0x26C4,
        0x26C5,
        0x26CE,
        0x26D4,
        0x26EA,
        0x26F2,
        0x26F3,
        0x26F5,
        0x26FA,
        0x26FD,
        0x2705,
        0x270A,
        0x270B,
        0x2728,
        0x274C,
        0x274E,
        0x2753,
        0x2754,
        0x2755,
        0x2757,
        0x2795,
        0x2796,
        0x2797,
        0x27B0,
        0x27BF,
        0x2B1B,
        0x2B1C,
        0x2B50,
        0x2B55,
    }


def _consume_emoji_component(source: str, start: int) -> int:
    if start >= len(source) or not _is_emoji_base(ord(source[start])):
        return start
    cursor = start + 1
    if cursor < len(source) and ord(source[cursor]) in {_EMOJI_TEXT_SELECTOR, _EMOJI_SELECTOR}:
        cursor += 1
    if cursor < len(source) and 0x1F3FB <= ord(source[cursor]) <= 0x1F3FF:
        cursor += 1
    if ord(source[start]) == 0x1F3F4:
        while cursor < len(source) and 0xE0020 <= ord(source[cursor]) <= 0xE007E:
            cursor += 1
        if cursor < len(source) and ord(source[cursor]) == _EMOJI_CANCEL_TAG:
            cursor += 1
    return cursor


def _emoji_spans(source: str) -> list[tuple[int, int, bool]]:
    """Return complete emoji grapheme spans and whether they request text presentation."""

    spans: list[tuple[int, int, bool]] = []
    cursor = 0
    while cursor < len(source):
        start = cursor
        value = ord(source[cursor])
        if source[cursor] in "0123456789#*" and cursor + 1 < len(source):
            end = cursor + 1
            if ord(source[end]) == _EMOJI_SELECTOR:
                end += 1
            if end < len(source) and ord(source[end]) == _EMOJI_KEYCAP:
                spans.append((start, end + 1, False))
                cursor = end + 1
                continue
        if 0x1F1E6 <= value <= 0x1F1FF:
            end = cursor + 1
            if end < len(source) and 0x1F1E6 <= ord(source[end]) <= 0x1F1FF:
                end += 1
            spans.append((start, end, False))
            cursor = end
            continue
        end = _consume_emoji_component(source, cursor)
        # Preserve any otherwise unsupported base plus a presentation selector
        # as one grapheme instead of leaking a detached VS15/VS16 character.
        if (
            end == cursor
            and cursor + 1 < len(source)
            and ord(source[cursor + 1])
            in {
                _EMOJI_TEXT_SELECTOR,
                _EMOJI_SELECTOR,
            }
        ):
            end = cursor + 2
        if end == cursor:
            cursor += 1
            continue
        while end < len(source) and ord(source[end]) == _EMOJI_ZWJ:
            component_end = _consume_emoji_component(source, end + 1)
            if component_end == end + 1:
                break
            end = component_end
        values = tuple(map(ord, source[start:end]))
        text_presentation = _EMOJI_TEXT_SELECTOR in values or (
            _EMOJI_SELECTOR not in values and not _is_default_emoji_presentation(ord(source[start]))
        )
        spans.append((start, end, text_presentation))
        cursor = end
    return spans


def _substitute_emoji(fragment: str) -> str:
    """Replace complete emoji graphemes with deterministic offline SVG."""

    if not any(
        _is_emoji_base(ord(char)) or ord(char) in {_EMOJI_SELECTOR, _EMOJI_KEYCAP}
        for char in fragment
    ):
        return fragment
    soup = BeautifulSoup(fragment, "html.parser")
    for node in list(soup.find_all(string=True)):
        if node.parent and node.parent.name in {"code", "pre", "style", "script"}:
            continue
        source = str(node)
        spans = _emoji_spans(source)
        if not spans:
            continue
        parts: list[object] = []
        cursor = 0
        for start, end, text_presentation in spans:
            if start > cursor:
                parts.append(source[cursor:start])
            sequence = source[start:end]
            if text_presentation:
                parts.append(sequence)
            else:
                emoji_fragment = BeautifulSoup(_emoji_markup(sequence), "html.parser")
                parts.extend(list(emoji_fragment.contents))
            cursor = end
        if cursor < len(source):
            parts.append(source[cursor:])
        node.replace_with(*parts)
    return str(soup)


def _render_bibliography(context: PublicationContext) -> str:
    if not context.citation_numbers:
        return ""
    ordered = sorted(context.citation_numbers.items(), key=lambda item: item[1])
    entries = []
    for identity, number in ordered:
        source = context.source_by_identity[identity]
        authors = html.escape(_source_authors(source))
        title = html.escape(_source_title(source))
        publication = (
            _text(source, "publication") or _text(source, "publisher") or _text(source, "site")
        )
        year = html.escape(
            _scalar_text(source.get("year") or source.get("date") or source.get("published_at"))
        )
        accessed = html.escape(_scalar_text(source.get("accessed") or source.get("accessed_at")))
        doi = _normalize_doi(_text(source, "doi"))
        url = _safe_href(_text(source, "url") or _text(source, "href"))
        parts = [
            f"{authors}," if authors else "",
            f"“{title},”",
            f"<em>{html.escape(publication)}</em>," if publication else "",
            f"{year}." if year else "",
        ]
        locator = ""
        if doi:
            safe_doi_text = html.escape(doi)
            safe_doi_href = html.escape(f"https://doi.org/{doi}", quote=True)
            locator = f' doi: <a href="{safe_doi_href}">{safe_doi_text}</a>.'
        elif url:
            locator = f' [Online]. Available: <a href="{html.escape(url, quote=True)}">{html.escape(url)}</a>.'
        accessed_html = f" Accessed: {accessed}." if accessed else ""
        backrefs = context.citation_backrefs.get(identity, [])
        backref_links = "".join(
            f'<span class="citation-backref-item">'
            f'<a class="citation-backref" href="#{ref_id}" aria-label="Return to citation {index + 1}">{index + 1}</a>'
            f"{',' if index < len(backrefs) - 1 else ''}"
            f"</span>"
            for index, ref_id in enumerate(backrefs)
        )
        backref_html = (
            f'<span class="citation-backrefs" aria-label="Return to citations">'
            f'<span aria-hidden="true">↩ </span>{backref_links}</span>'
            if backref_links
            else ""
        )
        entries.append(
            f'<li id="{context.reference_ids[identity]}"><span class="reference-number">[{number}]</span>'
            f'<span class="reference-content">{" ".join(part for part in parts if part)}'
            f"{locator}{accessed_html} {backref_html}</span></li>"
        )
    dedicated = _dedicated_references_page(context, ordered)
    pagination_class = "bibliography-dedicated" if dedicated else "bibliography-compact"
    return (
        f'<section class="bibliography {pagination_class}" '
        f'aria-labelledby="{context.references_heading_id}">'
        f'<h2 id="{context.references_heading_id}">References</h2>'
        f"<ol>{''.join(entries)}</ol></section>"
    )


def _dedicated_references_page(context: PublicationContext, ordered: list[tuple[str, int]]) -> bool:
    if context.presentation == "pulse":
        return False
    preference = context.metadata.get("references", context.metadata.get("bibliography"))
    if isinstance(preference, dict):
        explicit = preference.get("dedicated_page", preference.get("page"))
        if isinstance(explicit, bool):
            return explicit
    if isinstance(preference, bool):
        return preference
    estimated_length = sum(
        len(_source_authors(context.source_by_identity[identity]))
        + len(_source_title(context.source_by_identity[identity]))
        + len(_text(context.source_by_identity[identity], "url"))
        + len(_text(context.source_by_identity[identity], "doi"))
        for identity, _number in ordered
    )
    return len(ordered) >= 8 or estimated_length >= 2400


def _string_list(value: object) -> list[str]:
    return [str(item) for item in value if str(item).strip()] if isinstance(value, list) else []


def _safe_href(url: str) -> str:
    return url if url.strip().lower().startswith(_ALLOWED_HREF_PROTOCOLS) else ""


def _safe_image_src(value: str) -> str:
    source = value.strip()
    lowered = source.lower()
    if re.match(r"^data:image/(?:png|jpeg|gif|webp);base64,[a-z0-9+/=\s]+$", source, re.I):
        return source
    if lowered.startswith("data:image/svg+xml,"):
        svg = unquote(source.split(",", 1)[1]).strip()
        unsafe = (
            not svg.lower().startswith("<svg")
            or len(svg) > 100_000
            or re.search(
                r"<(?:script|foreignobject|image|use)\b|"
                r"\bon[a-z]+\s*=|\b(?:href|xlink:href)\s*=|url\s*\(",
                svg,
                re.I,
            )
        )
        return "" if unsafe else source
    # Standalone HTML is also the trusted PDF input. Never embed remote image
    # resources here: keep the safe external link/caption while preventing
    # browser viewers and PDF engines from issuing network requests.
    return ""


def _safe_resolved_media(media: ResolvedMedia | None) -> str:
    if media is None or not media.mime_type.lower().startswith("image/"):
        return ""
    source = media.src.strip()
    if source.startswith("/api/v1/deliverables/"):
        return source
    return _safe_image_src(source)


def _render_source_links(source_values: object, available_sources: list[dict[str, Any]]) -> str:
    resolved = _resolve_sources(source_values, available_sources)
    if not resolved:
        return ""
    links = []
    for index, source in enumerate(resolved, start=1):
        title = _block_title(source) or f"Source {index}"
        url = _safe_href(_text(source, "url") or _text(source, "href"))
        links.append(
            f'<a href="{html.escape(url, quote=True)}">{html.escape(title)}</a>'
            if url
            else f"<span>{html.escape(title)}</span>"
        )
    return f'<span class="citation-links">{" ".join(links)}</span>'


def _render_research_answer(
    block: dict[str, Any],
    *,
    sources: list[dict[str, Any]],
    child_html: str,
    context: PublicationContext | None,
) -> str:
    available = _resolve_sources(block.get("sources"), sources) or sources
    paragraphs = _object_list(block.get("paragraphs") or block.get("items"))
    paragraph_html = []
    for paragraph in paragraphs:
        text = _text(paragraph, "text") or _text(paragraph, "content")
        refs = _first_present(paragraph, "source_ids", "citations", "sources")
        citations = (
            context.cite(refs, available, token=paragraph)
            if context
            else _render_source_links(refs, available)
        )
        paragraph_html.append(f"<p>{html.escape(text)}{citations}</p>")
    answer = _text(block, "answer") or _block_content(block)
    answer_html = "".join(paragraph_html) if paragraph_html else _markdown_to_html(answer)
    key_points = _string_list(block.get("key_points") or block.get("highlights"))
    points_html = (
        "<h3>Key points</h3><ul>"
        + "".join(f"<li>{html.escape(point)}</li>" for point in key_points)
        + "</ul>"
        if key_points
        else ""
    )
    description = _text(block, "description")
    block_refs = _first_present(block, "source_ids", "citations")
    sources_html = (
        context.cite(block_refs, available, token=block)
        if context
        else _render_source_links(block_refs, available)
    )
    return (
        '<section class="block block-research_answer">'
        f"{context.heading(block, _block_title(block)) if context else _heading(_block_title(block))}"
        f"{f'<p class="block-description">{html.escape(description)}</p>' if description else ''}"
        f"{answer_html}{points_html}{sources_html}"
        f"{child_html}</section>"
    )


def _confidence_text(value: object) -> str:
    if isinstance(value, (int, float)):
        percent = value * 100 if 0 <= value <= 1 else value
        return f"{percent:g}%"
    return _scalar_text(value)


def _render_evidence_report(
    block: dict[str, Any],
    *,
    sources: list[dict[str, Any]],
    child_html: str,
    context: PublicationContext | None,
) -> str:
    available = _resolve_sources(block.get("sources"), sources) or sources
    claims = _object_list(block.get("claims") or block.get("items") or block.get("data"))
    cards = []
    for index, claim in enumerate(claims, start=1):
        label = _text(claim, "label") or _text(claim, "category") or "Claim"
        title = _text(claim, "title") or _text(claim, "claim") or f"Claim {index}"
        summary = (
            _text(claim, "content")
            or _text(claim, "summary")
            or (_text(claim, "claim") if _text(claim, "title") else "")
        )
        confidence = _confidence_text(claim.get("confidence", claim.get("score")))
        snippets = _object_list(claim.get("evidence") or claim.get("snippets"))
        evidence_html = "".join(
            "<blockquote>"
            f"{html.escape(_text(snippet, 'text') or _text(snippet, 'quote') or _text(snippet, 'content'))}"
            f"{f'<footer>{html.escape(_scalar_text(snippet.get("source") or snippet.get("url")))}</footer>' if snippet.get('source') or snippet.get('url') else ''}"
            "</blockquote>"
            for snippet in snippets
        )
        refs = _first_present(claim, "source_ids", "citations", "sources")
        cards.append(
            '<article class="claim-card">'
            f'<p class="eyebrow">{html.escape(label)}</p><h3>{html.escape(title)}</h3>'
            f"{f'<p>{html.escape(summary)}</p>' if summary else ''}"
            f"{f'<p class="confidence">Confidence: {html.escape(confidence)}</p>' if confidence else ''}"
            f"{evidence_html}"
            f"{context.cite(refs, available, token=claim) if context else _render_source_links(refs, available)}"
            "</article>"
        )
    caveats = _render_named_list("Caveats", block.get("caveats"))
    contradictions = _render_named_list("Contradictions", block.get("contradictions"))
    description = _text(block, "description")
    return (
        f'<section class="block block-{html.escape(str(block.get("type") or "evidence_report"))}">'
        f"{context.heading(block, _block_title(block)) if context else _heading(_block_title(block))}"
        f"{f'<p class="block-description">{html.escape(description)}</p>' if description else ''}"
        f'<div class="claim-grid">{"".join(cards)}</div>'
        f"{caveats}{contradictions}{child_html}</section>"
    )


def _render_named_list(title: str, value: object) -> str:
    items = _string_list(value)
    if not items:
        return ""
    return (
        f'<section class="semantic-list"><h3>{html.escape(title)}</h3><ul>'
        + "".join(f"<li>{html.escape(item)}</li>" for item in items)
        + "</ul></section>"
    )


def _render_incident(block: dict[str, Any], *, child_html: str, heading: str) -> str:
    entries = _object_list(
        block.get("items") or block.get("entries") or block.get("timeline") or block.get("data")
    )
    timeline = []
    for index, entry in enumerate(entries, start=1):
        marker = (
            _scalar_text(entry.get("time"))
            or _scalar_text(entry.get("timestamp"))
            or _scalar_text(entry.get("step"))
            or str(index)
        )
        title = _block_title(entry) or f"Entry {index}"
        content = _text(entry, "content") or _text(entry, "description")
        metadata = [
            f"{label}: {_scalar_text(entry.get(key))}"
            for label, key in (
                ("Status", "status"),
                ("Severity", "severity"),
                ("Owner", "owner"),
                ("Duration", "duration"),
            )
            if _scalar_text(entry.get(key))
        ]
        timeline.append(
            f"<li><span>{html.escape(marker)}</span><div><strong>{html.escape(title)}</strong>"
            f"{_markdown_to_html(content)}"
            f"{f'<p class="incident-meta">{html.escape(" · ".join(metadata))}</p>' if metadata else ''}"
            "</div></li>"
        )
    checklist = _object_list(
        block.get("checklist") or block.get("remediation") or block.get("actions")
    )
    checklist_items = []
    for index, item in enumerate(checklist, start=1):
        title = _block_title(item) or _text(item, "action") or f"Checklist item {index}"
        done = (
            item.get("done") is True or item.get("checked") is True or item.get("status") == "done"
        )
        metadata = [
            f"{label}: {_scalar_text(item.get(key))}"
            for label, key in (("Owner", "owner"), ("Status", "status"))
            if _scalar_text(item.get(key))
        ]
        checklist_items.append(
            f'<li><span class="checkmark">{"✓" if done else "○"}</span> {html.escape(title)}'
            f"{f'<small>{html.escape(" · ".join(metadata))}</small>' if metadata else ''}</li>"
        )
    pills = [
        f"{label}: {_scalar_text(block.get(key))}"
        for label, key in (("Severity", "severity"), ("Status", "status"), ("Owner", "owner"))
        if _scalar_text(block.get(key))
    ]
    description = _text(block, "description")
    return (
        '<section class="block block-incident">'
        f"{_eyebrow(block)}{heading}"
        f"{f'<p>{html.escape(description)}</p>' if description else ''}"
        f"{f'<p class="incident-pills">{html.escape(" · ".join(pills))}</p>' if pills else ''}"
        f"{f'<ol class="incident-timeline">{"".join(timeline)}</ol>' if timeline else ''}"
        f"{f'<section class="incident-checklist"><h3>{html.escape(_text(block, "checklist_title") or "Remediation checklist")}</h3><ul>{"".join(checklist_items)}</ul></section>' if checklist_items else ''}"
        f"{child_html}</section>"
    )


def _render_item_container(
    block: dict[str, Any],
    *,
    block_type: str,
    sources: list[dict[str, Any]],
    child_html: str,
    context: PublicationContext | None,
) -> str:
    items = _object_list(block.get("items"))
    rendered_items = []
    for index, item in enumerate(items, start=1):
        item_block = dict(item)
        item_block["_citation_token"] = item
        if block_type == "gallery":
            item_block.setdefault("type", "figure")
        else:
            item_block.setdefault("type", "section")
        label = _block_title(item_block) or f"Item {index}"
        summary = _text(item_block, "summary") or _text(item_block, "dek")
        for key in ("title", "label", "name", "summary", "dek"):
            item_block.pop(key, None)
        rendered = _render_block(item_block, sources=sources, context=context)
        if block_type == "accordion":
            open_attribute = " open" if context and context.render_target == "pdf" else ""
            rendered_items.append(
                f'<details class="container-item accordion-item"{open_attribute}>'
                f"<summary><span>{html.escape(label)}</span>"
                f"{f'<small>{html.escape(summary)}</small>' if summary else ''}</summary>"
                f"{rendered}</details>"
            )
        else:
            rendered_items.append(
                f'<section class="container-item"><h3>{html.escape(label)}</h3>{rendered}</section>'
            )
    return (
        f'<section class="block block-{html.escape(block_type)}">'
        f"{context.heading(block, _block_title(block)) if context else _heading(_block_title(block))}"
        f"{_markdown_to_html(_block_content(block))}"
        f"{''.join(rendered_items)}{child_html}</section>"
    )


def _render_link(block: dict[str, Any], *, child_html: str, heading: str) -> str:
    url = _safe_href(_text(block, "href") or _text(block, "url"))
    title = _block_title(block) or url or "Link"
    description = _text(block, "description") or _block_content(block)
    site = _text(block, "site") or _text(block, "domain")
    link_heading = (
        f'<a href="{html.escape(url, quote=True)}">{html.escape(title)}</a>'
        if url
        else html.escape(title)
    )
    return (
        '<section class="block block-link">'
        f"{heading if heading else f'<h2>{link_heading}</h2>'}"
        f"{f'<p class="link-target">{link_heading}</p>' if heading else ''}"
        f"{f'<p class="link-site">{html.escape(site)}</p>' if site else ''}"
        f"{_markdown_to_html(description)}{child_html}</section>"
    )


def _render_figure(
    block: dict[str, Any],
    *,
    child_html: str,
    heading: str,
    number: int | None,
    context: PublicationContext | None,
) -> str:
    source = _text(block, "src") or _text(block, "url")
    alt = _text(block, "alt") or _block_title(block)
    caption = _text(block, "caption") or _text(block, "description")
    source_label = _text(block, "source") or _text(block, "source_label")
    credit = _text(block, "credit")
    source_url = _safe_href(_text(block, "source_url"))
    timestamp = _text(block, "observed_at") or _text(block, "timestamp")
    caption_label = f"Figure {number}." if number is not None else ""
    caption_spacing = " " if caption_label and caption else ""
    reference = media_reference(block, context.assets) if context else None
    resolved = (
        context.media_resolver(reference, context.render_target)
        if context and context.media_resolver and reference
        else None
    )
    safe_source = _safe_resolved_media(resolved) or _safe_image_src(source)
    image = (
        f'<img src="{html.escape(safe_source, quote=True)}" alt="{html.escape(alt, quote=True)}">'
        if safe_source
        else ""
    )
    external_link = (
        f'<p><a href="{html.escape(_safe_href(source), quote=True)}">Open figure</a></p>'
        if _safe_href(source)
        else ""
    )
    source_text = html.escape(source_label or credit or source_url)
    source_html = (
        f'<a href="{html.escape(source_url, quote=True)}">{source_text}</a>'
        if source_url
        else source_text
    )
    if source_html:
        source_line = (
            '<p class="figure-source">Source: '
            f"{source_html}{f' · {html.escape(timestamp)}' if timestamp else ''}</p>"
        )
    elif timestamp:
        source_line = f'<p class="figure-source">Updated: {html.escape(timestamp)}</p>'
    else:
        source_line = ""
    return (
        '<figure class="block block-figure">'
        f"{heading}{image}{external_link}"
        f"{f'<figcaption><strong>{caption_label}</strong>{caption_spacing}{html.escape(caption)}</figcaption>' if caption or caption_label else ''}"
        f"{source_line}"
        f"{child_html}</figure>"
    )


def _eyebrow(block: dict[str, Any]) -> str:
    eyebrow = _text(block, "eyebrow")
    return f'<p class="eyebrow">{html.escape(eyebrow)}</p>' if eyebrow else ""


def _render_badges(values: object) -> str:
    if not isinstance(values, list):
        return ""
    badges = "".join(
        f"<span>{html.escape(str(value))}</span>" for value in values if str(value).strip()
    )
    return f'<p class="badges">{badges}</p>' if badges else ""


def _render_dashboard_items(block: dict[str, Any]) -> str:
    values = next(
        (
            block.get(key)
            for key in ("metrics", "items", "cards", "data")
            if isinstance(block.get(key), list)
        ),
        [],
    )
    cards = []
    for index, value in enumerate(values, start=1):
        if not isinstance(value, dict):
            continue
        label = (
            _text(value, "label")
            or _text(value, "title")
            or _text(value, "name")
            or f"Metric {index}"
        )
        metric_value = _scalar_text(value.get("value", value.get("current", value.get("count"))))
        unit = _scalar_text(value.get("unit"))
        delta = _scalar_text(value.get("delta")) or _scalar_text(value.get("trend"))
        status = _scalar_text(value.get("status"))
        description = (
            _text(value, "description")
            or _text(value, "explanation")
            or _text(value, "summary")
            or _text(value, "dek")
        )
        detail_parts = [part for part in (status, delta) if part]
        drilldown = value.get("drilldown")
        drilldown_html = ""
        if isinstance(drilldown, list):
            drilldown_items = "".join(
                f"<li>{html.escape(_scalar_text(item))}</li>"
                for item in drilldown
                if _scalar_text(item)
            )
            drilldown_html = f"<ul>{drilldown_items}</ul>" if drilldown_items else ""
        cards.append(
            '<article class="dashboard-item">'
            f"<h3>{html.escape(label)}</h3>"
            f'<p class="dashboard-value">{html.escape(metric_value)}'
            f"{f'<span>{html.escape(unit)}</span>' if unit else ''}</p>"
            f"{f'<p class="dashboard-meta">{html.escape(" · ".join(detail_parts))}</p>' if detail_parts else ''}"
            f"{f'<p>{html.escape(description)}</p>' if description else ''}"
            f"{drilldown_html}</article>"
        )
    return f'<div class="dashboard-grid">{"".join(cards)}</div>' if cards else ""


def _render_children(
    block: dict[str, Any],
    *,
    sources: list[dict[str, Any]],
    context: PublicationContext | None,
) -> str:
    children = (
        block.get("blocks") if isinstance(block.get("blocks"), list) else block.get("children")
    )
    if not isinstance(children, list):
        return ""
    return "".join(
        _render_block(child, sources=sources, context=context)
        for child in children
        if isinstance(child, dict)
    )


def _resolve_sources(
    source_values: object, available_sources: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    values = source_values if isinstance(source_values, list) else [source_values]
    if source_values is None:
        return []
    by_id: dict[str, dict[str, Any]] = {}
    for source in available_sources:
        for key in ("id", "key", "citation_id", "title", "name", "url", "href"):
            value = source.get(key)
            if value is not None:
                by_id[str(value).strip()] = source
    resolved: list[dict[str, Any]] = []
    seen: set[str] = set()
    for value in values:
        source: dict[str, Any] | None = None
        if isinstance(value, dict):
            reference = value.get("source_id") or value.get("sourceId") or value.get("ref")
            normalized_reference = str(reference).strip() if reference is not None else ""
            if normalized_reference in by_id:
                source = dict(by_id[normalized_reference])
                label = _text(value, "label")
                if label:
                    source["title"] = label
            else:
                source = value
        elif str(value).strip() in by_id:
            source = by_id[str(value).strip()]
        if source is not None:
            identity = _source_identity(source)
            if identity not in seen:
                seen.add(identity)
                resolved.append(source)
    return resolved


def _render_sources(sources: list[dict[str, Any]]) -> str:
    items = []
    for index, source in enumerate(sources, start=1):
        title = (
            _text(source, "title")
            or _text(source, "label")
            or _text(source, "name")
            or f"Source {index}"
        )
        url = _text(source, "url") or _text(source, "href")
        citation = (
            _text(source, "citation")
            or _text(source, "snippet")
            or _text(source, "description")
            or _text(source, "publisher")
        )
        safe_title = html.escape(title)
        if url.lower().startswith(_ALLOWED_HREF_PROTOCOLS):
            heading = f'<a href="{html.escape(url, quote=True)}">{safe_title}</a>'
        else:
            heading = safe_title
        citation_html = f"<p>{html.escape(citation)}</p>" if citation else ""
        items.append(f"<li><strong>{heading}</strong>{citation_html}</li>")
    return (
        f'<ol class="source-list">{"".join(items)}</ol>' if items else "<p>No sources provided.</p>"
    )


def _markdown_to_html(value: str) -> str:
    rendered = markdown.markdown(
        value,
        extensions=["extra", "sane_lists", "tables"],
        output_format="html5",
    )
    return sanitize_html(rendered)


def _markdown_with_publication_heading(
    value: str,
    block: dict[str, Any],
    context: PublicationContext,
) -> str:
    rendered = _markdown_to_html(value)
    anchor = context.heading_ids.get(id(block))
    if not anchor:
        return rendered
    soup = BeautifulSoup(rendered, "html.parser")
    headings = soup.find_all(re.compile(r"^h[1-4]$"))
    explicit_title = bool(_block_title(block))
    nested = dict(context.markdown_headings.get(id(block), []))
    for index, heading in enumerate(headings):
        if not explicit_title and index == 0:
            heading.name = f"h{context.heading_level(block)}"
            heading["id"] = anchor
            heading["tabindex"] = "-1"
            continue
        entry = nested.get(index)
        if entry is not None:
            heading.name = f"h{entry.level}"
            heading["id"] = entry.anchor
            heading["tabindex"] = "-1"
    return str(soup)


def sanitize_html(value: str) -> str:
    """Sanitize HTML using already-declared BeautifulSoup dependency."""

    soup = BeautifulSoup(value, "html.parser")
    for comment in soup.find_all(string=lambda text: isinstance(text, Comment)):
        comment.extract()
    for tag in list(soup.find_all(True)):
        if tag.name not in _ALLOWED_TAGS:
            tag.unwrap()
            continue
        allowed = set(_ALLOWED_ATTRS.get(tag.name, set())) | set(_ALLOWED_ATTRS.get("*", set()))
        for attr in list(tag.attrs):
            if attr.lower() not in allowed:
                del tag.attrs[attr]
                continue
            attr_value = tag.attrs.get(attr)
            attr_name = attr.lower()
            if attr_name == "href":
                text_value = str(attr_value or "").strip().lower()
                if not text_value.startswith(_ALLOWED_HREF_PROTOCOLS):
                    del tag.attrs[attr]
            elif attr_name == "src":
                text_value = str(attr_value or "").strip().lower()
                if not text_value.startswith(_ALLOWED_SRC_PROTOCOLS):
                    del tag.attrs[attr]
    return str(soup)


def _render_key_values(block: dict[str, Any]) -> str:
    ignored = {
        "type",
        "title",
        "subtitle",
        "description",
        "eyebrow",
        "badges",
        "blocks",
        "children",
        "items",
        "tone",
        "content",
        "text",
        "body",
    }
    rows = []
    for key, value in block.items():
        if key in ignored or isinstance(value, (dict, list)):
            continue
        rows.append(
            f"<tr><th>{html.escape(str(key).replace('_', ' '))}</th><td>{html.escape(str(value))}</td></tr>"
        )
    return f"<table>{''.join(rows)}</table>" if rows else ""


def _render_table(block: dict[str, Any], *, number: int | None = None) -> str:
    rows = block.get("rows") or block.get("data")
    if not isinstance(rows, list) or not rows:
        return _render_key_values(block)
    headers = block.get("columns")
    header_labels: list[str]
    if isinstance(headers, list):
        header_values = [
            str(item.get("key") or item.get("id") or item.get("label") or "")
            if isinstance(item, dict)
            else str(item)
            for item in headers
        ]
        header_labels = [
            str(item.get("label") or item.get("title") or item.get("key") or item.get("id") or "")
            if isinstance(item, dict)
            else str(item).replace("_", " ").title()
            for item in headers
        ]
    elif isinstance(rows[0], dict):
        header_values = [str(key) for key in rows[0]]
        header_labels = [value.replace("_", " ").title() for value in header_values]
    else:
        header_values = []
        header_labels = []
    head = "".join(f"<th>{html.escape(label)}</th>" for label in header_labels)
    body_rows = []
    for row in rows:
        if isinstance(row, dict):
            cells = [row.get(key, "") for key in header_values]
        elif isinstance(row, list):
            cells = row
        else:
            cells = [row]
        body_rows.append("".join(f"<td>{html.escape(str(cell))}</td>" for cell in cells))
    caption = _text(block, "caption") or _text(block, "description")
    label = f"Table {number}. " if number is not None else ""
    caption_html = (
        f"<caption><strong>{label}</strong>{html.escape(caption)}</caption>"
        if caption or label
        else ""
    )
    return f"<table>{caption_html}<thead><tr>{head}</tr></thead><tbody>{''.join(f'<tr>{row}</tr>' for row in body_rows)}</tbody></table>"


def _render_matrix(
    block: dict[str, Any],
    *,
    sources: list[dict[str, Any]],
    number: int | None,
    context: PublicationContext | None,
) -> str:
    rows = _object_list(block.get("rows") or block.get("data"))
    if not rows:
        return _render_key_values(block)
    headers = block.get("columns")
    if isinstance(headers, list):
        header_values = [
            str(item.get("key") or item.get("id") or item.get("label") or "")
            if isinstance(item, dict)
            else str(item)
            for item in headers
        ]
        header_labels = [
            str(item.get("label") or item.get("title") or item.get("key") or item.get("id") or "")
            if isinstance(item, dict)
            else str(item).replace("_", " ").title()
            for item in headers
        ]
    else:
        header_values = [str(key) for key in rows[0]]
        header_labels = [value.replace("_", " ").title() for value in header_values]
    available = _resolve_sources(block.get("sources"), sources) or sources
    row_refs = [_first_present(row, "source_ids", "citations", "sources") for row in rows]
    row_evidence = [_object_list(row.get("evidence") or row.get("rationale")) for row in rows]
    has_evidence = any(
        refs or evidence for refs, evidence in zip(row_refs, row_evidence, strict=True)
    )
    head = "".join(f"<th>{html.escape(label)}</th>" for label in header_labels)
    if has_evidence:
        head += "<th>Evidence</th>"
    body_rows = []
    for row, refs, evidence in zip(rows, row_refs, row_evidence, strict=True):
        recommended = row.get("recommended") in {True, "true", "yes", "recommended", "winner"}
        cells = ""
        for index, key in enumerate(header_values):
            value = html.escape(_scalar_text(row.get(key)))
            marker = (
                '<strong class="recommendation">Recommended</strong>'
                if recommended and (key in {"option", "name", "title"} or index == 0)
                else ""
            )
            cells += f"<td>{value}{marker}</td>"
        if has_evidence:
            evidence_text = ""
            for item in evidence:
                evidence_title = _text(item, "title") or _text(item, "label")
                evidence_body = (
                    _text(item, "text") or _text(item, "content") or _text(item, "quote")
                )
                if evidence_title:
                    evidence_text += f"<strong>{html.escape(evidence_title)}</strong>"
                if evidence_body:
                    evidence_text += f"<p>{html.escape(evidence_body)}</p>"
            citations = (
                context.cite(refs, available, token=row)
                if context
                else _render_source_links(refs, available)
            )
            cells += f"<td>{evidence_text}{citations or '—'}</td>"
        row_class = ' class="recommended"' if recommended else ""
        body_rows.append(f"<tr{row_class}>{cells}</tr>")
    caption = _text(block, "caption") or _text(block, "description")
    label = f"Table {number}. " if number is not None else ""
    caption_html = (
        f"<caption><strong>{label}</strong>{html.escape(caption)}</caption>"
        if caption or label
        else ""
    )
    return f"<table>{caption_html}<thead><tr>{head}</tr></thead><tbody>{''.join(body_rows)}</tbody></table>"


def _render_items_as_definitions(block: dict[str, Any]) -> str:
    items = block.get("items") or block.get("data") or block.get("steps")
    if not isinstance(items, list):
        return ""
    values = []
    for item in items:
        if not isinstance(item, dict):
            continue
        label = _text(item, "label") or _text(item, "key") or _text(item, "name")
        value = item.get("value", item.get("text", item.get("content", "")))
        values.append(f"<div><dt>{html.escape(label)}</dt><dd>{html.escape(str(value))}</dd></div>")
    return f"<dl>{''.join(values)}</dl>" if values else ""


def _render_timeline(block: dict[str, Any]) -> str:
    items = block.get("items") or block.get("data") or block.get("steps")
    if not isinstance(items, list):
        return ""
    rendered = []
    for index, item in enumerate(items, start=1):
        if not isinstance(item, dict):
            continue
        marker = _scalar_text(item.get("time")) or _scalar_text(item.get("step")) or str(index)
        title = _block_title(item) or f"Item {index}"
        content = _text(item, "content") or _text(item, "description")
        status = _scalar_text(item.get("status")) or _scalar_text(item.get("tone"))
        rendered.append(
            f"<li><span>{html.escape(marker)}</span><div><strong>{html.escape(title)}</strong>"
            f"{f'<small>{html.escape(status)}</small>' if status else ''}"
            f"{_markdown_to_html(content)}</div></li>"
        )
    return f"<ol>{''.join(rendered)}</ol>" if rendered else ""


def _render_day_agenda(block: dict[str, Any]) -> str:
    raw_items = block.get("items") if "items" in block else block.get("events")
    if not isinstance(raw_items, list):
        raw_items = []
    items = [item for item in raw_items if isinstance(item, dict)]
    tasks = (
        [task for task in block.get("tasks", []) if isinstance(task, dict)]
        if isinstance(block.get("tasks"), list)
        else []
    )
    zone, timezone = _agenda_timezone(block.get("timezone"))
    now_dt = _agenda_datetime(
        block["now"] if "now" in block else block.get("now_iso"),
        zone,
    )
    all_day: list[str] = []
    timed_items: list[tuple[datetime, datetime | None, str, dict[str, Any]]] = []
    for item in items:
        title = _scalar_text(item.get("title") if "title" in item else item.get("label"))
        if not title:
            continue
        if (item.get("all_day") if "all_day" in item else item.get("allDay")) is True:
            all_day.append(f"<span>{html.escape(title)}</span>")
            continue
        start_dt = _agenda_datetime(
            item["start"] if "start" in item else item.get("start_iso", item.get("start_time")),
            zone,
        )
        end_dt = _agenda_datetime(
            item["end"] if "end" in item else item.get("end_iso", item.get("end_time")),
            zone,
        )
        if start_dt is None:
            continue
        if end_dt is not None and end_dt.timestamp() <= start_dt.timestamp():
            end_dt = None
        timed_items.append((start_dt, end_dt, title, item))
    timed_items.sort(
        key=lambda entry: (
            entry[0].timestamp(),
            entry[1].timestamp() if entry[1] else float("inf"),
            entry[2],
        )
    )
    next_index = next(
        (
            index
            for index, (start, end, _title, item) in enumerate(timed_items)
            if item.get("kind") != "free"
            and now_dt is not None
            and (
                start.timestamp() > now_dt.timestamp()
                or (end is not None and start.timestamp() <= now_dt.timestamp() < end.timestamp())
            )
        ),
        None,
    )
    marker_index = (
        sum(
            1
            for start, _end, _title, _item in timed_items
            if start.timestamp() <= now_dt.timestamp()
        )
        if now_dt is not None
        else None
    )
    timed: list[str] = []
    for index, (start_dt, end_dt, title, item) in enumerate(timed_items):
        if marker_index == index and now_dt is not None:
            timed.append(_agenda_marker(now_dt))
        start = start_dt.strftime("%H:%M")
        end = end_dt.strftime("%H:%M") if end_dt else ""
        start_iso = start_dt.isoformat()
        end_iso = end_dt.isoformat() if end_dt else ""
        location = _scalar_text(item.get("location"))
        description = _scalar_text(item.get("description"))
        current = bool(
            now_dt
            and end_dt is not None
            and start_dt.timestamp() <= now_dt.timestamp() < end_dt.timestamp()
        )
        past = bool(
            now_dt
            and (
                (end_dt is not None and end_dt.timestamp() <= now_dt.timestamp())
                or (end_dt is None and start_dt.timestamp() <= now_dt.timestamp())
            )
        )
        classes = " next" if next_index == index else ""
        classes += " free" if item.get("kind") == "free" else ""
        classes += " current" if current else " past" if past else ""
        end_marker = (
            f'<span aria-hidden="true">–</span>'
            f'<time datetime="{html.escape(end_iso, quote=True)}">{html.escape(end)}</time>'
            if end
            else ""
        )
        marker = (
            '<span class="agenda-range">'
            f'<time datetime="{html.escape(start_iso, quote=True)}">{html.escape(start)}</time>'
            f"{end_marker}"
            "</span>"
        )
        timed.append(
            f'<li class="{classes.strip()}"><div class="agenda-time">{marker}</div><div>'
            f"{'<small>Current</small>' if current and next_index == index else '<small>Next</small>' if next_index == index else ''}"
            f"<strong>{html.escape(title)}</strong>"
            f"{f'<span class="agenda-location"> · {html.escape(location)}</span>' if location else ''}"
            f"{f'<p>{html.escape(description)}</p>' if description else ''}</div></li>"
        )
    # `marker_index == len(timed_items)` is also true (0 == 0) when there are
    # no timed items at all -- guard with `timed_items` so the "current
    # time" marker never renders standalone with nothing to anchor against.
    # Without this, an agenda with zero scheduled items showed a second,
    # redundant "now" line directly under the header's own current-time
    # display, and (Python renderer only) that marker-only list made
    # `if timed:` below true, silently hiding the "no events" message.
    if marker_index == len(timed_items) and now_dt is not None and timed_items:
        timed.append(_agenda_marker(now_dt))
    task_items = [
        f"<li>{html.escape(_block_title(task))}</li>"
        for task in tasks[:4]
        if isinstance(task, dict) and _block_title(task)
    ]
    now = now_dt.strftime("%H:%M") if now_dt else ""
    now_iso = now_dt.isoformat() if now_dt else ""
    source = block.get("source")
    source_record = source if isinstance(source, dict) else None
    source_label = _block_title(source_record) if source_record is not None else ""
    source_url = (
        _safe_href(_text(source_record, "url") or _text(source_record, "href"))
        if source_record is not None
        else ""
    )
    refreshed_at = (
        _text(source_record, "refreshed_at") or _text(source_record, "refreshedAt")
        if source_record is not None
        else _text(block, "freshness")
    )
    all_day_html = (
        f'<div class="agenda-all-day"><strong>All day</strong>{"".join(all_day)}</div>'
        if all_day
        else ""
    )
    has_schedule = bool(all_day or timed_items or task_items)
    variant = _class_token(block.get("variant"), fallback="timeline")
    timeline_html = (
        f'<ol class="agenda-timeline">{"".join(timed)}</ol>'
        if timed
        else (
            '<p class="agenda-empty">Nothing is scheduled today.</p>'
            if not has_schedule
            else '<p class="agenda-empty">No timed events are scheduled today.</p>'
        )
    )
    header_time = now_dt is not None and not timed_items
    return (
        (
            (
                '<p class="agenda-now">'
                + (
                    f'<time datetime="{html.escape(now_iso, quote=True)}">{html.escape(now)}</time>'
                    if header_time
                    else ""
                )
                + f"<small>{html.escape(timezone)}</small></p>"
                if now_dt is not None
                else ""
            )
            + all_day_html
            + timeline_html
        )
        + (
            f'<div class="agenda-tasks"><strong>Tasks · {len(tasks)}</strong><ul>{"".join(task_items)}</ul></div>'
            if task_items
            else ""
        )
        + (
            "<footer>"
            + (
                f'<a href="{html.escape(source_url, quote=True)}" rel="noreferrer">'
                f"{html.escape(source_label or source_url)}</a>"
                if source_url
                else html.escape(source_label or ("Calendar and tasks" if refreshed_at else ""))
            )
            + (f" · updated {html.escape(refreshed_at)}" if refreshed_at else "")
            + "</footer>"
            if source_label or source_url or refreshed_at
            else ""
        )
        + f'<span class="agenda-variant-label sr-only">Agenda view: {html.escape(variant)}</span>'
    )


_AGENDA_TIMESTAMP_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}"
    r"(?::\d{2}(?:\.\d{1,9})?)?(?:Z|[+-]\d{2}:\d{2})$",
    re.IGNORECASE,
)
_AGENDA_TIMEZONE_RE = re.compile(r"^[A-Za-z0-9_+-]+(?:/[A-Za-z0-9_+-]+)*$")


def _agenda_timezone(value: Any) -> tuple[ZoneInfo, str]:
    name = _scalar_text(value) or "UTC"
    if _AGENDA_TIMEZONE_RE.fullmatch(name) is None:
        return ZoneInfo("UTC"), "UTC"
    try:
        return ZoneInfo(name), name
    except (ZoneInfoNotFoundError, ValueError, OSError):
        return ZoneInfo("UTC"), "UTC"


def _agenda_datetime(value: Any, zone: ZoneInfo) -> datetime | None:
    text = _scalar_text(value)
    if not text or _AGENDA_TIMESTAMP_RE.fullmatch(text) is None:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.astimezone(zone) if parsed.tzinfo is not None else None


def _agenda_marker(now: datetime) -> str:
    return (
        '<li class="agenda-current-marker" aria-label="Current time"><span></span>'
        f'<time datetime="{html.escape(now.isoformat(), quote=True)}">'
        f"{html.escape(now.strftime('%H:%M'))}</time></li>"
    )


def _render_chart(block: dict[str, Any]) -> str:
    model = normalize_chart(block)
    description = _text(block, "description")
    source_label = _text(block, "source") or _text(block, "source_label")
    source_url = _safe_href(_text(block, "source_url"))
    timestamp = _text(block, "observed_at") or _text(block, "timestamp")
    description_html = (
        f'<p class="chart-description">{html.escape(description)}</p>' if description else ""
    )
    source_text = html.escape(source_label or source_url)
    source_html = (
        f'<a href="{html.escape(source_url, quote=True)}">{source_text}</a>'
        if source_url
        else source_text
    )
    source_line = (
        '<p class="data-source">'
        f"{f'Source: {source_html}' if source_html else ''}"
        f"{f' · Updated: {html.escape(timestamp)}' if timestamp else ''}</p>"
        if source_html or timestamp
        else ""
    )
    if model is not None:
        headers, rows = chart_rows(model)
        table = _render_table({"columns": headers, "rows": rows, "caption": "Chart data"})
        accessible = html.escape(model.description)
        description_id = (
            "chart-description-"
            + hashlib.sha256(
                json.dumps(block, ensure_ascii=False, sort_keys=True, default=str).encode()
            ).hexdigest()[:12]
        )
        return (
            description_html
            + f'<div class="chart-visual" aria-describedby="{description_id}">'
            + render_chart_svg(model)
            + "</div>"
            + f'<p class="sr-only" id="{description_id}">{accessible}</p>'
            + '<details class="chart-data"><summary>View data table</summary>'
            + table
            + "</details>"
            + source_line
        )
    return (
        description_html
        + "<p>Chart data is unavailable; see the source for current values.</p>"
        + source_line
    )


_STANDALONE_THEME_BOOTSTRAP = r"""
(() => {
  // Deliverables default to dark regardless of OS preference until Cognis
  // ships app-wide theming: there is no per-deliverable theme toggle, so
  // resolving `system`/a stored choice via prefers-color-scheme used to
  // leave this page stuck light on a light-OS machine (or stuck on a
  // stale stored choice) with no way back to dark.
  // `data-resolved-theme="light"` is still supported by the CSS for when
  // app-wide theming lands; this bootstrap just never sets it.
  const root = document.documentElement;
  root.dataset.resolvedTheme = 'dark';
  root.style.colorScheme = 'dark';
})();
"""

_STANDALONE_INTERACTIONS = r"""
(() => {
  let downloadStarted = false;
  document.querySelector('[data-download-pdf]')?.addEventListener('click', async (event) => {
    event.preventDefault();
    const link = event.currentTarget;
    if (downloadStarted) return;
    downloadStarted = true;
    link.classList.remove('error');
    link.classList.add('pending');
    link.setAttribute('aria-disabled', 'true');
    link.setAttribute('aria-label', 'Preparing PDF download');
    link.setAttribute('title', 'Preparing PDF download');
    try {
      const response = await fetch(link.href, { credentials: 'same-origin' });
      if (!response.ok) throw new Error(`PDF export failed (${response.status})`);
      const sanitizeFilename = (value) => {
        const leaf = String(value || '').replace(/\\/g, '/').split('/').pop() || '';
        const safe = leaf.replace(/[\u0000-\u001f\u007f"<>:|?*]/g, '-').replace(/^\.+/, '').trim();
        return safe && safe !== '.' && safe !== '..' ? safe.slice(0, 180) : '';
      };
      const dispositionFilename = (header) => {
        if (!header || /[\r\n]/.test(header)) return '';
        const extended = header.match(/(?:^|;)\s*filename\*\s*=\s*UTF-8''([^;]*)/i);
        if (extended) {
          try { return sanitizeFilename(decodeURIComponent(extended[1].trim().replace(/^"|"$/g, ''))); }
          catch {}
        }
        const plain = header.match(/(?:^|;)\s*filename\s*=\s*(?:"((?:\\.|[^"])*)"|([^;]*))/i);
        return sanitizeFilename((plain?.[1] || plain?.[2] || '').replace(/\\(.)/g, '$1'));
      };
      const filename = dispositionFilename(response.headers.get('Content-Disposition'))
        || `${sanitizeFilename(document.title) || 'deliverable'}.pdf`;
      const blobUrl = URL.createObjectURL(await response.blob());
      const download = document.createElement('a');
      download.href = blobUrl;
      download.download = filename.toLowerCase().endsWith('.pdf') ? filename : `${filename}.pdf`;
      document.body.append(download);
      download.click();
      download.remove();
      window.setTimeout(() => URL.revokeObjectURL(blobUrl), 1000);
    } catch {
      link.classList.add('error');
      link.setAttribute('aria-label', 'PDF download failed; retry');
      link.setAttribute('title', 'PDF download failed; retry');
    } finally {
      downloadStarted = false;
      link.classList.remove('pending');
      link.removeAttribute('aria-disabled');
      if (!link.classList.contains('error')) {
        link.setAttribute('aria-label', 'Download PDF');
        link.setAttribute('title', 'Download PDF');
      }
    }
  });

  const toc = document.querySelector('.document-toc');
  const tocToggle = document.querySelector('[data-toc-toggle]');
  const tocClose = document.querySelector('[data-toc-close]');
  const tocBackdrop = document.querySelector('[data-toc-backdrop]');
  const tocMedia = matchMedia('(max-width: 720px)');
  let tocRestoreFocus = null;
  const setTocOpen = (open, restoreFocus = true) => {
    if (!toc || !tocMedia.matches) return;
    toc.classList.toggle('open', open);
    tocBackdrop?.classList.toggle('open', open);
    document.body.classList.toggle('toc-open', open);
    tocToggle?.setAttribute('aria-expanded', String(open));
    if (open) {
      tocRestoreFocus = document.activeElement;
      toc.setAttribute('role', 'dialog');
      toc.setAttribute('aria-modal', 'true');
      requestAnimationFrame(() => {
        if (toc.classList.contains('open')) tocClose?.focus({ preventScroll: true });
      });
      window.setTimeout(() => {
        if (toc.classList.contains('open')) tocClose?.focus({ preventScroll: true });
      }, 200);
    } else {
      toc.removeAttribute('role');
      toc.removeAttribute('aria-modal');
      if (restoreFocus && tocRestoreFocus?.isConnected) {
        tocRestoreFocus.focus({ preventScroll: true });
      }
      tocRestoreFocus = null;
    }
  };
  tocToggle?.addEventListener('click', () => setTocOpen(true));
  tocClose?.addEventListener('click', () => setTocOpen(false));
  tocBackdrop?.addEventListener('click', () => setTocOpen(false));
  toc?.addEventListener('click', (event) => {
    const link = event.target.closest?.('a[href^="#"]');
    if (!link) return;
    const target = document.querySelector(link.getAttribute('href'));
    if (!target) return;
    event.preventDefault();
    if (tocMedia.matches) setTocOpen(false, false);
    target.scrollIntoView({ behavior: 'smooth', block: 'start' });
    target.focus({ preventScroll: true });
    history.replaceState(history.state, '', link.getAttribute('href'));
  });
  document.addEventListener('keydown', (event) => {
    if (event.key === 'Escape' && toc?.classList.contains('open')) {
      event.preventDefault();
      setTocOpen(false);
      return;
    }
    if (event.key !== 'Tab' || !toc?.classList.contains('open')) return;
    const focusable = [...toc.querySelectorAll('a[href], button:not([disabled])')]
      .filter((node) => node.getClientRects().length > 0);
    if (!focusable.length) return;
    const first = focusable[0];
    const last = focusable[focusable.length - 1];
    if (event.shiftKey && document.activeElement === first) {
      event.preventDefault();
      last.focus();
    } else if (!event.shiftKey && document.activeElement === last) {
      event.preventDefault();
      first.focus();
    }
  });
  document.addEventListener('focusin', (event) => {
    if (toc?.classList.contains('open') && !toc.contains(event.target)) {
      tocClose?.focus({ preventScroll: true });
    }
  });
  tocMedia.addEventListener?.('change', (event) => {
    if (!event.matches && toc?.classList.contains('open')) setTocOpen(false, false);
  });
  if (toc && 'IntersectionObserver' in window) {
    const links = [...toc.querySelectorAll('a[href^="#"]')];
    const observer = new IntersectionObserver((entries) => {
      const current = entries.filter((entry) => entry.isIntersecting)
        .sort((left, right) => left.boundingClientRect.top - right.boundingClientRect.top)[0];
      if (!current?.target.id) return;
      for (const link of links) {
        const active = link.getAttribute('href') === `#${current.target.id}`;
        link.classList.toggle('active', active);
        if (active) link.setAttribute('aria-current', 'location');
        else link.removeAttribute('aria-current');
      }
    }, { rootMargin: '-12% 0px -72% 0px' });
    for (const link of links) {
      const target = document.querySelector(link.getAttribute('href'));
      if (target) observer.observe(target);
    }
  }
})();
"""


_CSS = """
@font-face { font-family: "Noto Emoji Vendored"; src: __EMOJI_FONT_SOURCE__; font-style: normal; font-weight: 400; font-display: block; }
:root {
  color-scheme: light;
  --page-bg: #f5f7fa; --surface: #fff; --text: #18212f; --muted: #526170;
  --subtle: #5b6976; --line: #cfd6dc; --strong-line: #596b7a; --accent: #174f7a;
  --soft: #eef1f3; --code-bg: #17202c; --code-text: #edf2f7;
  --label: #405162; --eyebrow: #47647d; --heading-detail: #283b4c;
  --decorative: #557894; --quote: #344657; --badge-line: #aeb9c4;
  --row-line: #d7dde2; --pulse-bg: #ecebe6; --active-bg: #eef5f8;
  --shadow: rgb(24 33 47 / .07); --focus: #0e7490; --danger: #b42318;
  --surface-raised: #f8fafc; --surface-accent: #e8f4f8; --positive: #087f5b;
  --warning: #b45309; --chart-1: #0e7490; --chart-2: #7c3aed; --chart-3: #db2777; --chart-4: #15803d;
  --rich-surface: var(--surface); --rich-surface-raised: var(--surface-raised);
  --rich-text: var(--text); --rich-muted: var(--subtle); --rich-line: var(--line); --rich-accent: var(--accent);
  font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;

  /* Canonical scale, shared by name and value with the Svelte web renderer's
     --rich-fs-*/--rich-fw-*/--rich-lh-*/--rich-ls-*/--rich-space-*/
     --rich-radius-* tokens (ui/src/lib/components/rich/rich-blocks.css).
     This is the renderer-convergence half of the design-system polish pass:
     both renderers consume the same named steps instead of two independently
     drifting sets of hardcoded values. Only screen-context rules below use
     these; @media print and the physical mm/pt-based Pulse print rules keep
     their own page-physical units, which are a different concern. */
  --rich-fs-3xs: 0.68rem;
  --rich-fs-2xs: 0.72rem;
  --rich-fs-xs: 0.78rem;
  --rich-fs-sm: 0.85rem;
  --rich-fs-sm-plus: 0.92rem;
  --rich-fs-base: 1rem;
  --rich-fs-md: 1.08rem;
  --rich-fs-lg: 1.18rem;
  --rich-fs-xl: 1.3rem;
  --rich-fs-2xl: 1.45rem;
  --rich-fs-display: clamp(1.85rem, 4vw, 2.8rem);
  --rich-fw-medium: 500;
  --rich-fw-semibold: 700;
  --rich-fw-bold: 800;
  --rich-lh-tight: 1.06;
  --rich-lh-snug: 1.15;
  --rich-lh-normal: 1.4;
  --rich-lh-relaxed: 1.55;
  --rich-lh-loose: 1.7;
  --rich-ls-tighter: -0.04em;
  --rich-ls-tight: -0.035em;
  --rich-ls-snug: -0.025em;
  --rich-ls-normal: 0;
  --rich-ls-wide: 0.08em;
  --rich-ls-wider: 0.14em;
  --rich-space-1: 0.25rem;
  --rich-space-2: 0.5rem;
  --rich-space-3: 0.75rem;
  --rich-space-4: 1rem;
  --rich-space-5: 1.25rem;
  --rich-space-6: 1.5rem;
  --rich-radius-lg: 1.35rem;
  --rich-radius-md: 1.1rem;
  --rich-radius-sm-plus: 0.85rem;
  --rich-radius-sm: 0.65rem;
  --rich-radius-xs: 0.45rem;
  --rich-radius-pill: 999px;

  /* Density mode, mirroring the web renderer's data-rich-density attribute
     (set from the same block-mix heuristic, see _document_density()).
     "airy" is the default (research/publication/notes reading comfort);
     dashboards/RCA/ops archetypes get tighter block spacing and page
     padding via [data-rich-density="dense"] below. */
  --rich-density-block-gap: 1.65rem;
  --rich-density-page-pad: clamp(1.25rem, 3vw, 2.25rem);
}
body[data-rich-density="dense"] {
  --rich-density-block-gap: 1.15rem;
  --rich-density-page-pad: clamp(1rem, 2.4vw, 1.7rem);
}
:root[data-resolved-theme="dark"] {
  color-scheme: dark;
  --page-bg: #020617; --surface: #081525; --text: #e5f4ff; --muted: #a8bdd0;
  --subtle: #89a4bb; --line: #263d52; --strong-line: #4f6f87; --accent: #67e8f9;
  --soft: #102438; --code-bg: #010a14; --code-text: #dff8ff;
  --label: #c4d9e8; --eyebrow: #8eeeff; --heading-detail: #e5f4ff;
  --decorative: #6bbbd3; --quote: #d2e7f5; --badge-line: #55738a;
  --row-line: #29445a; --pulse-bg: #020617; --active-bg: #123047;
  --shadow: rgb(0 0 0 / .32); --focus: #a5f3fc; --danger: #fda4af;
  --surface-raised: #0d2032; --surface-accent: #103349; --positive: #5eead4;
  --warning: #fdba74; --chart-1: #67e8f9; --chart-2: #c4b5fd; --chart-3: #f9a8d4; --chart-4: #86efac;
  --rich-surface: var(--surface); --rich-surface-raised: var(--surface-raised);
  --rich-text: var(--text); --rich-muted: var(--subtle); --rich-line: var(--line); --rich-accent: var(--accent);
}
* { box-sizing: border-box; }
html, body { max-width: 100%; }
body { margin: 0; background: var(--page-bg); color: var(--text); overflow-wrap: anywhere; transition: background-color .18s ease, color .18s ease; }
.page { width: min(calc(100% - 2rem), 72rem); margin: 0 auto; padding: 1.25rem 0 4rem; }
.document-header { display: flex; gap: 1.5rem; align-items: flex-start; justify-content: space-between; margin: 0 0 1.5rem; padding: 0 0 1.1rem; border-bottom: 1px solid var(--strong-line); }
.document-header > div { min-width: 0; }
.document-header nav { display: flex; flex: none; gap: .5rem; }
.eyebrow { margin: 0 0 .6rem; color: var(--eyebrow); font-size: var(--rich-fs-2xs); font-weight: var(--rich-fw-bold); letter-spacing: var(--rich-ls-wider); text-transform: uppercase; }
h1 { max-width: 48rem; margin: 0; font-family: inherit; font-size: var(--rich-fs-display); line-height: var(--rich-lh-tight); letter-spacing: var(--rich-ls-tight); }
.subtitle { max-width: 48rem; margin: .8rem 0 0; color: var(--muted); font-size: var(--rich-fs-md); line-height: var(--rich-lh-relaxed); }
.badges { display: flex; flex-wrap: wrap; gap: .4rem; margin: .9rem 0 0; }
.badges span { border: 1px solid var(--badge-line); border-radius: var(--rich-radius-pill); padding: .22rem .5rem; color: var(--label); font-size: var(--rich-fs-2xs); font-weight: var(--rich-fw-semibold); }
.action { display: inline-grid; width: 2.75rem; height: 2.75rem; place-items: center; border: 1px solid var(--line); border-radius: var(--rich-radius-pill); color: var(--accent); background: var(--surface); text-decoration: none; cursor: pointer; transition: transform .14s ease, border-color .14s ease, background .14s ease; }
.action:hover { border-color: var(--accent); background: var(--soft); transform: translateY(-1px); }
.action:active { transform: translateY(1px); }
.action:focus-visible, a:focus-visible { outline: 3px solid var(--focus); outline-offset: 3px; }
.action.error { border-color: var(--danger); color: var(--danger); }
.action.pending { cursor: progress; opacity: .65; animation: action-pulse 1s ease-in-out infinite alternate; pointer-events: none; }
@keyframes action-pulse { to { opacity: 1; } }
.action svg { width: 1.2rem; height: 1.2rem; fill: none; stroke: currentColor; stroke-linecap: round; stroke-linejoin: round; stroke-width: 1.8; }
.document { background: var(--surface); padding: var(--rich-density-page-pad); box-shadow: 0 12px 36px var(--shadow); }
.document-layout { display: grid; grid-template-columns: minmax(10rem, 12rem) minmax(0, 1fr); gap: clamp(1rem, 2.5vw, 2rem); }
.document-layout:not(:has(.document-toc)) { display: block; }
.document-content { min-width: 0; }
.toc-action, .toc-backdrop, .toc-drawer-header { display: none; }
.document-toc { position: sticky; top: 1rem; align-self: start; min-width: 0; max-height: calc(100vh - 2rem); overflow: auto; margin: 0; border-left: 1px solid var(--line); padding: 0 0 0 .7rem; }
.toc-title { margin: 0 0 .5rem; color: var(--label); font-family: inherit; font-size: var(--rich-fs-xs); font-weight: var(--rich-fw-bold); letter-spacing: var(--rich-ls-wide); text-transform: uppercase; }
.document-toc ol { margin: 0; padding: 0; list-style: none; }
.document-toc li { margin: .08rem 0; }
.document-toc li > ol { margin-left: .45rem; border-left: 1px solid var(--line); padding-left: .55rem; }
.document-toc li[data-level="3"] { font-size: .92em; }
.document-toc li[data-level="4"] { font-size: .86em; }
.document-toc a { display: block; border-radius: .35rem; color: inherit; padding: .3rem .4rem; text-decoration: none; }
.document-toc a:hover, .document-toc a.active { background: var(--soft); color: var(--accent); }
.document-toc a::after { content: leader(".") target-counter(attr(href), page); color: var(--subtle); }
.block { margin: 0 0 var(--rich-density-block-gap); }
.block > h2 { margin: 0 0 .7rem; font-family: inherit; font-size: var(--rich-fs-2xl); line-height: var(--rich-lh-snug); }
.block-callout, .block-status { border-left: .25rem solid var(--decorative); background: var(--soft); padding: var(--rich-space-3) var(--rich-space-4); }
.block-card { position: relative; padding: .35rem 0 .8rem; }
.block-card.card-variant-editorial, .block-card.card-variant-feature { background: linear-gradient(145deg, var(--surface-accent), var(--surface-raised) 72%); }
.block-card.card-variant-editorial { border-bottom: 1px solid var(--row-line); background: transparent; }
.block-card.card-variant-feature { min-height: 11rem; border-radius: var(--rich-radius-sm-plus); padding: clamp(1.1rem, 3vw, 1.6rem); box-shadow: 0 8px 24px var(--shadow); }
.block-card.card-variant-visual { position: relative; isolation: isolate; min-height: 16rem; overflow: hidden; border-radius: var(--rich-radius-sm-plus); padding: clamp(1.2rem, 4vw, 2rem); color: #fff; background: #12243d; box-shadow: 0 12px 28px var(--shadow); }
.block-card.card-variant-visual::after { content: ""; position: absolute; z-index: -1; inset: 0; background: linear-gradient(15deg, rgba(8, 19, 34, .94), rgba(8, 19, 34, .2) 72%); }
.block-card.card-variant-visual .card-media { position: absolute; z-index: -2; inset: 0; margin: 0; }
.block-card.card-variant-visual .card-media img { width: 100%; height: 100%; object-fit: cover; }
.block-card.card-variant-visual h3, .block-card.card-variant-visual h4, .block-card.card-variant-visual p, .block-card.card-variant-visual .citation-links { color: inherit; }
.block-card.card-variant-action { border-left: .28rem solid var(--accent); background: var(--surface-raised); padding: var(--rich-space-3) var(--rich-space-4); }
.block-card.card-variant-status { border-radius: var(--rich-radius-sm); background: var(--surface-raised); padding: var(--rich-space-3) var(--rich-space-4); box-shadow: inset 0 .22rem 0 var(--decorative); }
.block-card.card-variant-metric { border-radius: var(--rich-radius-sm); background: linear-gradient(160deg, var(--surface-raised), var(--surface-accent)); padding: var(--rich-space-3) var(--rich-space-4); }
.block-card.card-variant-compact { border-radius: var(--rich-radius-xs); padding: var(--rich-space-3) .85rem; box-shadow: none; }
.block-card.tone-positive { --decorative: var(--positive); }
.block-card.tone-warning { --decorative: var(--warning); }
.block-card.tone-critical, .block-card.tone-danger { --decorative: var(--danger); }
.card-media { margin: 0 0 .85rem; overflow: hidden; border-radius: var(--rich-radius-xs); }
.card-variant-feature .card-media { margin: -1.6rem -1.6rem .95rem; border-radius: var(--rich-radius-sm-plus) var(--rich-radius-sm-plus) 0 0; }
.card-media img { display: block; width: 100%; aspect-ratio: 16 / 9; max-height: 18rem; object-fit: cover; }
.card-media figcaption { margin: 0; padding: .35rem .55rem; background: var(--soft); color: var(--subtle); font-size: var(--rich-fs-2xs); }
.card-media.media-placement-leading { float: left; width: min(9rem, 38%); margin: 0 .9rem .5rem 0; border-radius: var(--rich-radius-sm); }
.card-media.media-placement-background { opacity: .82; }
.card-icon { display: inline-grid; min-width: 2rem; min-height: 2rem; place-items: center; margin: 0 0 .55rem; border-radius: var(--rich-radius-xs); background: var(--soft); font-size: 1.2rem; }
.card-dek { margin: -.25rem 0 .7rem; color: var(--muted); font-size: var(--rich-fs-sm-plus); line-height: var(--rich-lh-snug); }
.card-headline-link { color: inherit; text-decoration: none; }
.card-headline-link:hover { color: var(--accent); text-decoration: underline; }
.block-quote { margin-left: 0; border-left: .2rem solid var(--decorative); padding: .2rem 0 .2rem 1.1rem; color: var(--quote); }
.block-quote p { font-family: inherit; font-size: var(--rich-fs-md); }
.block-quote footer { color: var(--muted); font-size: var(--rich-fs-sm); }
.block-metric { display: inline-block; min-width: 10rem; margin-right: 1rem; border-top: 2px solid var(--strong-line); padding-top: .55rem; vertical-align: top; }
.metric-label, .metric-delta { margin: 0; color: var(--subtle); font-size: var(--rich-fs-xs); }
.metric-value { margin: .18rem 0; font-size: 1.8rem; font-weight: var(--rich-fw-bold); letter-spacing: var(--rich-ls-tighter); }
.metric-unit, .dashboard-value span { margin-left: .25rem; font-size: .48em; font-weight: var(--rich-fw-semibold); letter-spacing: 0; }
.metric-description { max-width: 22rem; margin: .45rem 0 0; color: var(--muted); font-size: var(--rich-fs-sm); }
.status-value { display: inline-block; margin: 0 0 .8rem; border: 1px solid var(--badge-line); border-radius: var(--rich-radius-pill); padding: .2rem .55rem; color: var(--label); font-size: var(--rich-fs-2xs); font-weight: var(--rich-fw-semibold); }
.dashboard-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(10rem, 1fr)); gap: var(--rich-space-3) var(--rich-space-4); margin-top: var(--rich-space-3); }
.dashboard-blocks { display: grid; grid-template-columns: repeat(auto-fit, minmax(9rem, 1fr)); gap: var(--rich-space-3) var(--rich-space-4); margin-top: var(--rich-space-3); }
.dashboard-blocks > .block { min-width: 0; margin: 0; }
.dashboard-item { border-top: 1px solid var(--badge-line); padding-top: .5rem; }
.dashboard-item h3 { margin: 0; font-family: inherit; font-size: var(--rich-fs-xs); }
.dashboard-item p { margin: .25rem 0 0; }
.dashboard-value { font-size: 1.35rem; font-weight: var(--rich-fw-bold); }
.dashboard-meta { color: var(--subtle); font-size: var(--rich-fs-sm); }
.dashboard-item ul { margin: .35rem 0 0; padding-left: 1rem; font-size: var(--rich-fs-xs); }
.block-description { color: var(--muted); }
.research-sources { margin-top: 1rem; border-top: 1px solid var(--line); padding-top: .7rem; }
.research-sources h3, .semantic-list h3, .container-item > h3, .incident-checklist h3 { margin: 0 0 .45rem; font-size: var(--rich-fs-xs); letter-spacing: var(--rich-ls-wide); text-transform: uppercase; }
.citation-links { display: inline; margin-left: .22em; font-size: .78em; white-space: normal; }
.citation { display: inline-block; margin-right: .12em; font-weight: var(--rich-fw-semibold); text-decoration: none; }
.source-list { color: var(--text); }
.source-list p { margin: .2rem 0 0; color: var(--muted); }
.claim-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(14rem, 1fr)); gap: .9rem; }
.claim-card { border-top: 1px solid var(--badge-line); padding-top: .65rem; }
.claim-card h3 { margin: .15rem 0 .45rem; font-family: inherit; font-size: var(--rich-fs-md); }
.claim-card blockquote { margin: .6rem 0; border-left: 2px solid var(--badge-line); padding-left: .7rem; color: var(--quote); }
.claim-card blockquote footer { color: var(--muted); }
.confidence, .incident-meta, .incident-pills, .link-site, .code-language { color: var(--subtle); font-size: var(--rich-fs-xs); }
.semantic-list { margin-top: .9rem; }
.incident-timeline { margin: .8rem 0 0; padding: 0; list-style: none; }
.incident-timeline li { display: grid; grid-template-columns: 4rem 1fr; gap: .8rem; border-top: 1px solid var(--row-line); padding: .65rem 0; }
.incident-timeline li > span { color: var(--muted); font-size: var(--rich-fs-xs); font-weight: var(--rich-fw-bold); }
.incident-checklist { margin-top: .9rem; border-top: 1px solid var(--line); padding-top: .7rem; }
.incident-checklist ul { margin: 0; padding: 0; list-style: none; }
.incident-checklist li { display: grid; grid-template-columns: auto 1fr; gap: .4rem; padding: .25rem 0; }
.incident-checklist small { display: block; grid-column: 2; color: var(--subtle); }
.container-item { margin-top: .8rem; padding-top: .65rem; }
.container-item > .block { margin-bottom: 0; }
.accordion-item { border-bottom: 1px solid var(--row-line); padding: .5rem 0 .75rem; }
.accordion-item summary { cursor: pointer; color: var(--heading-detail); font-weight: var(--rich-fw-semibold); }
.accordion-item summary span, .accordion-item summary small { display: block; }
.accordion-item summary small { margin-top: .15rem; color: var(--subtle); font-size: var(--rich-fs-xs); font-weight: var(--rich-fw-medium); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.accordion-item[open] summary { margin-bottom: .55rem; }
.block-figure { margin-right: 0; margin-left: 0; }
.block-figure figcaption, caption { margin-top: .45rem; color: var(--muted); font-size: var(--rich-fs-sm); line-height: var(--rich-lh-relaxed); text-align: left; }
.block-figure figcaption strong, caption strong { color: var(--heading-detail); }
.figure-source, .data-source { margin: .35rem 0 0; color: var(--subtle); font-size: var(--rich-fs-2xs); line-height: var(--rich-lh-normal); }
.chart-description { color: var(--muted); font-size: var(--rich-fs-sm); }
.block-chart > p:not(.chart-description, .data-source) { color: var(--muted); }
.chart-visual { margin: .7rem 0; border-radius: var(--rich-radius-sm-plus); background: linear-gradient(180deg, var(--surface-raised), transparent); padding: .55rem; }
.chart-svg { display: block; width: 100%; max-height: 22rem; overflow: hidden; }
.chart-gridline { stroke: var(--row-line); stroke-width: 1; }
.chart-axis-label, .chart-axis-title, .chart-legend text, .chart-progress-value, .chart-donut-total { fill: var(--subtle); font: 12px Inter, sans-serif; }
.chart-axis-title { fill: var(--muted); font-weight: 700; }
.chart-line { stroke-width: 3; stroke-linecap: round; stroke-linejoin: round; }
.chart-area, .chart-bar, .chart-donut-segment { stroke: none; }
.chart-range-mark line { stroke-linecap: round; }
.chart-legend-item text { font-size: 11px; }
.chart-progress-track { fill: var(--soft); }
.chart-donut-total { font-size: 18px; font-weight: 800; }
.chart-data { margin-top: .5rem; color: var(--subtle); font-size: .78rem; }
.chart-data summary { cursor: pointer; font-weight: 700; }
.sr-only { position: absolute !important; width: 1px !important; height: 1px !important; overflow: hidden !important; clip: rect(0 0 0 0) !important; white-space: nowrap !important; }
.bibliography { margin-top: 2rem; border-top: 1.5px solid var(--strong-line); padding-top: 1rem; }
.bibliography > h2 { font-family: inherit; font-size: var(--rich-fs-2xl); }
.bibliography ol { margin: 0; padding: 0; list-style: none; }
.bibliography li { display: grid; grid-template-columns: 2.4rem minmax(0, 1fr); gap: .35rem; margin: 0 0 .65rem; font-size: var(--rich-fs-sm); }
.reference-number { font-weight: var(--rich-fw-semibold); }
.citation-backrefs { display: inline-flex; flex-wrap: wrap; align-items: baseline; gap: 0 .18em; margin-left: .35em; color: var(--subtle); font-size: .78em; white-space: normal; }
.citation-backref-item { display: inline-flex; white-space: nowrap; }
.citation-backref { text-decoration: none; }
dl { display: grid; grid-template-columns: repeat(auto-fit, minmax(10rem, 1fr)); gap: .8rem 1.25rem; margin: 0; }
dt { color: var(--subtle); font-size: var(--rich-fs-2xs); font-weight: var(--rich-fw-bold); letter-spacing: var(--rich-ls-wide); text-transform: uppercase; }
dd { margin: .18rem 0 0; font-weight: var(--rich-fw-semibold); }
.block-timeline ol { margin: 0; padding: 0; list-style: none; }
.block-timeline li { display: grid; grid-template-columns: 4rem 1fr; gap: 1rem; border-top: 1px solid var(--row-line); padding: .75rem 0; }
.block-timeline li > span { color: var(--muted); font-size: var(--rich-fs-sm); font-weight: var(--rich-fw-bold); }
/* `steps` (block-steps modifier): a numbered-circle marker instead of the
   plain time/step label a timeline entry uses, mirroring the web
   renderer's `.rich-steps` treatment. `align-items: start` is required
   (not just relying on the span's own explicit height) because
   WeasyPrint's grid implementation stretches grid items to the row's
   cross-axis size even when the item declares an explicit height, unlike
   real browsers -- without this the circle badge renders as a stretched
   capsule instead of a circle. The web renderer additionally draws a
   connecting line between circles via an absolutely positioned pseudo-
   element; WeasyPrint does not reliably size empty absolutely-positioned
   pseudo-elements (verified via rendered PDF: an explicit 1px/1rem box
   rendered as an oversized rectangle instead), so that connector is
   intentionally omitted here -- the numbered circles alone still convey
   the sequence clearly in print. */
.block-steps li { grid-template-columns: 2.35rem 1fr; align-items: start; border-top: 0; padding-top: 0; }
.block-steps li + li { margin-top: var(--rich-space-4); }
.block-steps li > span {
  display: grid;
  /* Longhand, not the `place-items` shorthand: WeasyPrint logs
     "Ignored `place-items: center`" and falls back to default start
     alignment (number rendered top-left of the circle instead of
     centered), while it does support the longhand align-items/
     justify-items. */
  align-items: center;
  justify-items: center;
  width: 2.35rem;
  height: 2.35rem;
  border: 1px solid var(--badge-line);
  border-radius: var(--rich-radius-pill);
  background: var(--soft);
  color: var(--accent);
  font-variant-numeric: tabular-nums;
}
.block-day-agenda { border-top: 2px solid var(--heading-detail); padding-top: 2mm; break-inside: auto; }
.agenda-now { float: right; margin: -8mm 0 0; font-weight: var(--rich-fw-bold); }
.agenda-now small { display: block; color: var(--subtle); font-size: var(--rich-fs-2xs); text-align: right; }
.agenda-all-day { display: flex; flex-wrap: wrap; gap: 2mm 4mm; margin: 2mm 0; padding: 2mm; background: var(--soft); font-size: var(--rich-fs-xs); }
.agenda-timeline { margin: 0; padding: 0; list-style: none; }
.agenda-timeline li { display: grid; grid-template-columns: 16mm 1fr; gap: 3mm; border-top: 1px solid var(--row-line); padding: 2mm 0; break-inside: avoid; }
.agenda-timeline li.next { border-left: 1mm solid var(--accent); padding-left: 2mm; }
.agenda-timeline li.current { background: var(--active-bg); }
.agenda-timeline li.past { color: var(--subtle); }
.agenda-timeline li.agenda-current-marker { display: grid; grid-template-columns: 1fr auto; align-items: center; gap: 2mm; border-top: 0; color: var(--accent); font-size: .65rem; font-weight: var(--rich-fw-bold); }
.agenda-current-marker > span { height: .25mm; background: var(--accent); }
.agenda-timeline li.free { color: var(--subtle); }
.agenda-time time, .agenda-timeline li > div > span { color: var(--subtle); font-size: var(--rich-fs-2xs); }
.agenda-location { display: inline; margin-left: .18rem; }
.agenda-range { display: inline-flex; gap: .18rem; align-items: baseline; white-space: nowrap; }
.block-day-agenda[data-variant="compact"] .agenda-timeline li { grid-template-columns: 13mm 1fr; padding: 1mm 0; }
.block-day-agenda[data-variant="list"] .agenda-timeline li { border-top: 0; border-bottom: 1px solid var(--row-line); }
.agenda-timeline li > div > small { display: block; margin-bottom: .12rem; color: var(--accent); font-size: .62rem; font-weight: var(--rich-fw-bold); letter-spacing: .04em; text-transform: uppercase; }
.agenda-timeline p { margin: .5mm 0 0; font-size: var(--rich-fs-2xs); }
.agenda-tasks { display: grid; grid-template-columns: 24mm 1fr; gap: 3mm; border-top: 1px solid var(--row-line); padding-top: 2mm; font-size: var(--rich-fs-2xs); }
.agenda-tasks ul { display: flex; flex-wrap: wrap; gap: 1mm 4mm; margin: 0; padding: 0; list-style: none; }
.block-day-agenda footer, .agenda-empty { color: var(--subtle); font-size: var(--rich-fs-3xs); }
p, li { line-height: var(--rich-lh-loose); }
a { color: var(--accent); text-decoration-thickness: .06em; text-underline-offset: .12em; }
a:hover { text-decoration-thickness: .12em; }
a:active { color: var(--text); }
pre { max-width: 100%; overflow: auto; white-space: pre-wrap; overflow-wrap: anywhere; border-radius: var(--rich-radius-xs); background: var(--code-bg); color: var(--code-text); padding: 1rem; font-size: var(--rich-fs-sm); line-height: var(--rich-lh-relaxed); }
code { font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; }
table { width: 100%; border-collapse: collapse; margin: .8rem 0; color: var(--text); font-size: var(--rich-fs-sm); line-height: var(--rich-lh-normal); }
th, td { border-bottom: 1px solid var(--line); padding: .45rem .5rem; text-align: left; vertical-align: top; overflow-wrap: anywhere; }
th { border-bottom: 1.5px solid var(--strong-line); background: var(--soft); font-size: var(--rich-fs-xs); font-weight: var(--rich-fw-bold); letter-spacing: .03em; text-transform: uppercase; }
img, svg { max-width: 100%; height: auto; }
.emoji { display: inline-block; min-width: 1.1em; height: 1.1em; vertical-align: -.16em; }
.emoji-glyph { display: inline-block; font-family: "Noto Emoji Vendored" !important; font-size: 1em; font-weight: 400; line-height: 1; white-space: nowrap; }
/* Generic (non-pulse) grid/columns/card_grid layout. Previously only the
   pulse presentation had a display:grid rule for these block types, so a
   generic multi-item grid (e.g. a metric row) rendered as a plain vertical
   stack in the static/PDF export -- the same class of bug fixed in the web
   renderer's GridBlock.svelte column-count default. An explicit author
   column count is applied as an inline `grid-template-columns` override
   (see _explicit_columns_style()), not a CSS custom property: WeasyPrint's
   grid layout crashes (`TypeError: 'FunctionBlock' object is not
   subscriptable`) on `repeat(var(...), minmax(...))`, so `var()` must never
   appear inside `repeat()`/`minmax()` here. */
.block-grid, .block-card_grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(11rem, 1fr)); gap: .75rem 1rem; }
.block-columns { display: grid; grid-template-columns: repeat(auto-fit, minmax(16rem, 1fr)); gap: .75rem 1rem; }
.block-grid > .block, .block-columns > .block, .block-card_grid > .block { min-width: 0; margin-bottom: 0; }
/* Generic (non-pulse) deliverables use sans-serif throughout (h1 { font-family:
   inherit } above), matching the app shell and the web renderer's generic
   default -- serif display type is reserved for the pulse presentation's
   editorial reading mode, not applied universally. Restore it here, scoped
   to pulse only. This rule is intentionally NOT inside a media query: its
   selector specificity (class + element) already beats the plain-element
   `h1` rule inside `@media print` below, so one declaration covers both the
   static/web view and the PDF export. */
.presentation-pulse h1,
.presentation-pulse .block > h2,
.presentation-pulse .block-quote p,
.presentation-pulse .claim-card h3,
.presentation-pulse .bibliography > h2 { font-family: Georgia, "Times New Roman", serif; }
.presentation-pulse { background: var(--pulse-bg); }
.presentation-pulse .page { width: min(calc(100% - 1.25rem), 74rem); padding-top: 1rem; }
.presentation-pulse .document-header { margin-bottom: 0; padding: .65rem 0 .75rem; border-bottom: 2px solid var(--text); }
.presentation-pulse .document-header h1 { font-size: clamp(1.85rem, 4vw, 2.85rem); }
.presentation-pulse .document-header .subtitle { margin-top: .4rem; font-size: .9rem; }
.presentation-pulse .document { padding: clamp(1rem, 3vw, 2rem); box-shadow: none; }
.presentation-pulse .block { margin-bottom: 1rem; }
.presentation-pulse .block > h2 { padding-top: .55rem; border-top: 1px solid var(--strong-line); font-size: 1.25rem; }
.presentation-pulse .block-grid, .presentation-pulse .block-columns, .presentation-pulse .block-card_grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(11rem, 1fr)); gap: 4mm 6mm; }
.presentation-pulse .block-columns { grid-template-columns: minmax(0, 1.65fr) minmax(12rem, .85fr); }
.presentation-pulse .block-grid > .block, .presentation-pulse .block-columns > .block, .presentation-pulse .block-card_grid > .block { min-width: 0; margin-bottom: 0; }
  .presentation-pulse .block-card { padding: .8rem; }
.presentation-pulse .block-card > h2 { border: 0; padding: 0; font-size: 1.06rem; }
.presentation-pulse .block-metric { min-width: 0; margin: 0; }
.presentation-pulse .metric-value { font-size: 1.5rem; }
.presentation-pulse .block-figure img { display: block; width: 100%; max-height: 88mm; object-fit: cover; }
.presentation-pulse .block-chart table { font-size: .72rem; }
.presentation-pulse .block-chart th, .presentation-pulse .block-chart td { padding: .3rem .35rem; }
.presentation-pulse .block-sources { margin-top: 1.5rem; border-top: 1.5px solid var(--strong-line); padding-top: .6rem; }
@media (max-width: 720px) {
  .page { width: 100%; padding: 0; }
  body.toc-open { overflow: hidden; }
  .document-header { margin: 0 0 1rem; padding: 0 0 .8rem; border-bottom-width: 1px; }
  .document-header nav { margin-left: auto; gap: .35rem; }
  .document { padding: 1rem; box-shadow: none; }
  .document-layout { display: block; }
  .toc-action { display: inline-grid; }
  .toc-backdrop { position: fixed; inset: 0; z-index: 19; border: 0; background: rgb(2 6 23 / .58); opacity: 0; visibility: hidden; transition: opacity .18s ease, visibility .18s; }
  .toc-backdrop.open { display: block; opacity: 1; visibility: visible; }
  .document-toc { position: fixed; top: 0; right: 0; bottom: 0; z-index: 20; width: min(22rem, calc(100% - 2.5rem)); max-height: none; overflow: auto; visibility: hidden; transform: translateX(102%); border: 0; border-left: 1px solid var(--line); background: var(--surface); padding: .75rem 1rem max(1rem, env(safe-area-inset-bottom)); box-shadow: -18px 0 44px rgb(2 6 23 / .4); transition: transform .18s ease, visibility .18s; }
  .document-toc.open { visibility: visible; transform: translateX(0); }
  .toc-drawer-header { position: sticky; top: -.75rem; z-index: 1; display: flex; min-height: 3.5rem; align-items: center; justify-content: space-between; background: var(--surface); padding: .25rem 0 .5rem; }
  .toc-title { display: none; }
  h1 { font-size: clamp(1.55rem, 7vw, 2rem); }
  .subtitle { font-size: .98rem; }
  .block-timeline li { grid-template-columns: 1fr; gap: .25rem; }
  .block-metric { display: block; margin-right: 0; }
  .claim-grid { grid-template-columns: 1fr; }
  .incident-timeline li { grid-template-columns: 1fr; gap: .25rem; }
  .bibliography li { grid-template-columns: 2rem minmax(0, 1fr); }
  table { display: block; width: 100%; max-width: 100%; overflow-x: auto; }
  .presentation-pulse .page { width: 100%; }
  .presentation-pulse .document-header { padding: 0 0 .8rem; }
  .presentation-pulse .block-columns, .presentation-pulse .block-grid, .presentation-pulse .block-card_grid { grid-template-columns: 1fr; }
  .block-card.card-variant-feature { min-height: 0; }
  /* auto-fit already collapses unconstrained grids responsively via
     minmax(min(100%, ...)); this override only matters when an author set
     an explicit --rich-columns count that would otherwise force multiple
     narrow columns on a small viewport. */
  .block-grid, .block-columns, .block-card_grid { grid-template-columns: 1fr; }
}
@page {
  size: A4;
  margin: 18mm 17mm 19mm;
  @bottom-center { content: counter(page) " / " counter(pages); color: #69747f; font-size: 8pt; }
}
@media print {
  :root { font-family: "DejaVu Sans", Arial, sans-serif; font-size: 9.5pt; }
  body { background: white; color: #111; overflow-wrap: normal; }
  .page { width: auto; margin: 0; padding: 0; }
  .document-header { margin-bottom: 5mm; padding: 0 0 4mm; border-bottom: 1.5pt solid #111; }
  .document-header nav, .action { display: none; }
  h1 { font-family: inherit; font-size: 25pt; line-height: 1.04; }
  .presentation-pulse h1 { font-family: "DejaVu Serif", Georgia, serif; }
  .subtitle { color: #333; font-size: 10.5pt; }
  .eyebrow, .badges span { color: #333; }
  .document { padding: 0; box-shadow: none; }
  .document-layout { display: block; }
  .toc-backdrop, .toc-drawer-header { display: none; }
  .document-toc { position: static; display: block; max-height: none; overflow: visible; margin: 0 0 6mm; border: .5pt solid #bbb; padding: 3mm 4mm; }
  .toc-title { display: block; margin: 0 0 2mm; color: #111; font-family: "DejaVu Serif", Georgia, serif; font-size: 13pt; letter-spacing: 0; text-transform: none; bookmark-level: none; }
  .document-toc li { line-height: 1.25; }
  .document-toc li > ol { border-left: .5pt solid #bbb; }
  h1 { bookmark-level: 1; }
  h2 { bookmark-level: 2; }
  h3 { bookmark-level: 3; }
  h4 { bookmark-level: 4; }
  .block { margin-bottom: 4.5mm; break-inside: auto; }
  .block > h2, h2, h3, h4 { break-after: avoid; page-break-after: avoid; orphans: 3; widows: 3; }
  .block-callout, .block-status { break-inside: avoid; border-left-color: #555; background: #f2f2f2; }
  .block-card { break-inside: avoid; }
  .block-code { break-inside: avoid; }
  .block-quote { break-inside: avoid; border-left-color: #666; }
  .claim-card, .container-item, .incident-timeline li, .incident-checklist li { break-inside: avoid; }
  details.accordion-item > * { display: block !important; }
  details.accordion-item summary { list-style: none; }
  details.accordion-item summary { margin-bottom: 1.5mm; }
  .presentation-pulse .accordion-item { margin-top: 1mm; padding: 1mm 0 2mm; break-inside: auto; }
  .presentation-pulse .accordion-item .block-card { min-height: 0; padding: 2mm 0; background: transparent; box-shadow: none; break-inside: auto; }
  .chart-data > * { display: block !important; }
  .chart-visual { break-inside: avoid; background: #f7f7f7; }
  .chart-gridline { stroke: #bbb; }
  /* The root (non-print) chart chrome rules above use var(--subtle) /
     var(--muted), which WeasyPrint does not reliably resolve for SVG fill.
     .chart-axis-title and .chart-donut-total are added here (alongside the
     pre-existing .chart-axis-label/.chart-legend/.chart-progress-value)
     since their root rules have the same var()-based fill and would
     otherwise depend on the SVG initial fill (black) matching intent only
     by coincidence. */
  .chart-axis-label, .chart-axis-title, .chart-legend, .chart-progress-value, .chart-donut-total { fill: #333; }
  /* Belt-and-braces only: the actual fix for the progress-track background
     is an inline fill="#e4e7ea" attribute set directly in
     rich_visuals.py's SVG generation. That was necessary because this CSS
     class rule alone was verified (via direct PDF rendering) to NOT be
     applied by WeasyPrint to the <rect> -- unlike .chart-gridline, which
     does render correctly from CSS alone. The inconsistency wasn't fully
     root-caused; this rule is kept in case a future WeasyPrint version
     handles it, but do not rely on it. */
  .chart-progress-track { fill: #e4e7ea; }
  p, li { orphans: 3; widows: 3; }
  pre { break-inside: auto; white-space: pre-wrap; overflow-wrap: anywhere; background: #f2f2f2; color: #111; border: .5pt solid #bbb; }
  table { font-size: 8pt; break-inside: auto; }
  thead { display: table-header-group; }
  tfoot { display: table-footer-group; }
  tr { break-inside: avoid; page-break-inside: avoid; }
  caption, figcaption { break-before: avoid; }
  .bibliography li { break-inside: avoid; font-size: 7.7pt; }
  .bibliography-dedicated { break-before: page; page-break-before: always; }
  .bibliography-compact { margin-top: 5mm; padding-top: 3mm; }
  .bibliography-compact ol { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); column-gap: 7mm; }
  th { background: #e6e6e6; color: #111; }
  a { color: #111; text-decoration: underline; }
  a[href^="http"]::after { content: ""; }
  .presentation-pulse .document-header { margin-bottom: 4mm; padding-bottom: 3mm; }
  .presentation-pulse .document-header h1 { font-size: 22pt; }
  .presentation-pulse { font-size: 8.5pt; }
  .presentation-pulse p, .presentation-pulse li { line-height: 1.4; }
  .presentation-pulse .block-columns { grid-template-columns: minmax(0, 1.55fr) minmax(42mm, .8fr); gap: 5mm; }
  .presentation-pulse .block-grid, .presentation-pulse .block-card_grid { grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 3mm 5mm; }
  /* Generic (non-pulse) equivalent of the pulse override above. WeasyPrint's
     grid layout does not implement `repeat(auto-fit, ...)` track sizing (it
     lays every item out in a single column regardless of available width),
     so the auto-fit rule earlier in this stylesheet only works for the
     static/web HTML view. An explicit author column count (inline style)
     still wins over this. */
  .block-grid, .block-card_grid { grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 3mm 5mm; }
  .block-columns { grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 3mm 5mm; }
  .presentation-pulse .dashboard-blocks { display: grid; grid-template-columns: repeat(4, minmax(28mm, 1fr)); gap: 3mm; }
  .presentation-pulse .dashboard-blocks > .block-metric { display: block; min-width: 0; width: auto; margin: 0; padding: 2mm 2.5mm; }
  .presentation-pulse .dashboard-blocks .metric-label, .presentation-pulse .dashboard-blocks .metric-value { display: block; }
  .presentation-pulse .dashboard-blocks .metric-value { overflow-wrap: normal; word-break: normal; }
  .presentation-pulse .block { margin-bottom: 2mm; }
  .presentation-pulse .block > h2 { font-size: 12.5pt; }
  .presentation-pulse .block-figure, .presentation-pulse .block-chart, .presentation-pulse .block-metric { break-inside: avoid; }
  .presentation-pulse .card-media img { width: 100%; height: auto; max-height: 32mm; aspect-ratio: 16 / 9; object-fit: cover; }
  .presentation-pulse .block-chart { break-inside: auto; }
  .presentation-pulse .block-chart .chart-svg { max-height: 30mm; }
  .presentation-pulse .block-chart table { font-size: 6.8pt; }
  .presentation-pulse .bibliography-compact { margin-top: 3mm; padding-top: 2mm; }
  .presentation-pulse .bibliography-compact > h2 { margin-bottom: 2mm; font-size: 11pt; }
  .presentation-pulse .bibliography-compact ol { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); column-gap: 6mm; }
  .presentation-pulse .bibliography-compact li { margin-bottom: 1.5mm; font-size: 6.8pt; line-height: 1.32; }
}
"""
