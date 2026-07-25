"""Canonical GitHub-Flavored Markdown rendering for Rich Deliverables."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from html import unescape
from typing import Any, Protocol
from urllib.parse import quote, urlsplit, urlunsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from cognis.core.deliverable_links import DeliverableViewLink
from cognis.rendering.rich_visuals import (
    chart_trend_text,
    icon_symbol,
    media_reference,
    normalize_chart,
)

RICH_MARKDOWN_MAX_CHARS = 120_000

_CONTAINER_TYPES = {
    "section",
    "stack",
    "columns",
    "grid",
    "card_grid",
    "dashboard",
    "status_grid",
}
_ITEM_CONTAINER_TYPES = {"tabs", "accordion", "modal", "gallery"}
_TABLE_TYPES = {"table", "comparison_matrix", "decision_matrix"}
_INCIDENT_TYPES = {"incident_timeline", "incident_checklist", "checklist"}
_CONTENT_KEYS = ("content", "text", "body")
_TITLE_KEYS = ("title", "label", "name")


@dataclass(frozen=True, slots=True)
class ProjectionContext:
    sources: list[dict[str, Any]]
    full_view_link: DeliverableViewLink | None = None
    assets: list[dict[str, Any]] | None = None


class PresentationProjector(Protocol):
    """Presentation-specific rich projection seam."""

    def project(
        self,
        payload: dict[str, Any],
        *,
        title: str,
        context: ProjectionContext,
    ) -> list[str]: ...


_PROJECTORS: dict[str, PresentationProjector] = {}


def rich_media_manifest(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Return the canonical sidecar manifest for referenced rich media."""

    assets = _objects(payload.get("assets"))
    raw_manifest = payload.get("media_manifest")
    controller_manifest = raw_manifest if isinstance(raw_manifest, dict) else {}
    result: list[dict[str, Any]] = []
    seen: set[str] = set()

    def visit(blocks: list[dict[str, Any]]) -> None:
        for block in blocks:
            if block.get("type") in {"figure", "card"}:
                reference = media_reference(block, assets)
                manifest_item = (
                    controller_manifest.get(reference.ref_id) if reference is not None else None
                )
                asset = next(
                    (
                        item
                        for item in assets
                        if reference is not None
                        and reference.ref_id
                        in {
                            _text(item.get("id")),
                            _text(item.get("asset_id")),
                            _text(item.get("media_id")),
                            _text(item.get("artifact_id")),
                            _text(item.get("ref")),
                        }
                    ),
                    {},
                )
                artifact_id = (
                    manifest_item.get("artifact_ref")
                    if isinstance(manifest_item, dict)
                    else reference.artifact_id
                    if reference is not None
                    else None
                )
                if (
                    reference
                    and isinstance(artifact_id, str)
                    and artifact_id
                    and reference.ref_id not in seen
                ):
                    seen.add(reference.ref_id)
                    result.append(
                        {
                            "artifact_id": artifact_id,
                            **(
                                {"media_key": reference.ref_id}
                                if isinstance(manifest_item, dict)
                                else {}
                            ),
                            "filename": (
                                _text(manifest_item.get("filename"))
                                if isinstance(manifest_item, dict)
                                else _text(asset.get("filename"))
                            )
                            or None,
                            "mime_type": (
                                _text(manifest_item.get("mime_type"))
                                if isinstance(manifest_item, dict)
                                else reference.mime_type
                            ),
                            "alt": reference.alt,
                            "media_ref": reference.ref_id,
                            "safe_image_only": True,
                        }
                    )
            visit(_child_blocks(block))
            if block.get("type") in _ITEM_CONTAINER_TYPES:
                visit(_objects(block.get("items") or block.get("data") or block.get("entries")))

    visit(_objects(payload.get("blocks")))
    return result


def register_presentation_projector(
    presentation: str,
    projector: PresentationProjector,
) -> None:
    """Register a projector for ``metadata.presentation``."""

    normalized = presentation.strip().lower()
    if not normalized:
        raise ValueError("presentation must not be empty")
    _PROJECTORS[normalized] = projector


def render_rich_markdown(
    payload: dict[str, Any],
    *,
    title: str,
    full_view_link: DeliverableViewLink | None,
    deliverable_id: str,
    fallback_text: str,
) -> str:
    """Render a canonical rich payload as channel-independent GFM Markdown."""

    sources = _objects(payload.get("sources"))
    context = ProjectionContext(
        sources=sources,
        full_view_link=full_view_link,
        assets=_objects(payload.get("assets")),
    )
    metadata = payload.get("metadata")
    presentation = (
        str(metadata.get("presentation") or "").strip().lower()
        if isinstance(metadata, dict)
        else ""
    )
    projector = _PROJECTORS.get(presentation, GenericRichProjector())
    sections = projector.project(payload, title=title, context=context)
    if not sections:
        safe_fallback = _markdown_or_plain(fallback_text, context)
        sections = [_heading(title or "Deliverable", 1, context), safe_fallback]
    link_section = _full_view_section(
        full_view_link,
        deliverable_id=deliverable_id,
        context=context,
    )
    bounded = _apply_total_safety_limit(sections, context=context)
    if link_section:
        bounded.append(link_section)
    return "\n\n".join(section.strip() for section in bounded if section.strip())


def render_text_markdown(
    content: str,
    *,
    title: str,
    format_name: str,
    full_view_link: DeliverableViewLink | None,
    deliverable_id: str,
) -> str:
    """Render a non-rich deliverable as canonical Markdown."""

    context = ProjectionContext(
        sources=[],
        full_view_link=full_view_link,
    )
    safe_content = content.strip()
    if format_name == "html":
        safe_content = unescape(re.sub(r"<[^>]+>", " ", safe_content))
        safe_content = re.sub(r"[ \t\r\f\v]+", " ", safe_content).strip()
    else:
        safe_content = _markdown_or_plain(safe_content, context)
    sections = []
    if not _starts_with_equivalent_h1(safe_content, title, markdown=True):
        sections.append(_heading(title or "Deliverable", 1, context))
    sections.append(safe_content)
    bounded = _apply_total_safety_limit(
        [section for section in sections if section],
        context=context,
    )
    link_section = _full_view_section(
        full_view_link,
        deliverable_id=deliverable_id,
        context=context,
    )
    if link_section:
        bounded.append(link_section)
    return "\n\n".join(section.strip() for section in bounded if section.strip())


class GenericRichProjector:
    """Loss-minimizing renderer for every canonical rich block type."""

    def project(
        self,
        payload: dict[str, Any],
        *,
        title: str,
        context: ProjectionContext,
    ) -> list[str]:
        blocks = _objects(payload.get("blocks"))
        sections: list[str] = []
        if title and not _first_block_has_equivalent_title(blocks, title):
            sections.append(_heading(title, 1, context))
        sections.extend(_render_block(block, context=context, depth=2) for block in blocks)
        sections = [section for section in sections if section.strip()]
        if context.sources and not _contains_source_list(blocks):
            sections.append(_render_sources(context.sources, title="Sources", context=context))
        return sections


class PulseProjector:
    """Editorial projection for ``metadata.presentation='pulse'`` briefs."""

    def project(
        self,
        payload: dict[str, Any],
        *,
        title: str,
        context: ProjectionContext,
    ) -> list[str]:
        blocks = _objects(payload.get("blocks"))
        sections: list[str] = []
        if blocks and blocks[0].get("type") == "hero":
            sections.append(_render_block(blocks.pop(0), context=context, depth=1))
        elif title:
            sections.append(_heading(title, 1, context))

        deferred_sources: list[dict[str, Any]] = []
        for block in blocks:
            block_type = str(block.get("type") or "")
            children = _child_blocks(block)
            if (
                block_type in {"grid", "status_grid"}
                and children
                and all(child.get("type") == "metric" for child in children)
            ):
                sections.append(_pulse_group("Signals", children, context=context, emoji="◈"))
                continue
            if block_type == "columns" and children:
                labels = ("Lead", "Actions")
                emojis = ("●", "✓")
                for index, child in enumerate(children):
                    label = labels[index] if index < len(labels) else _title(child) or "Brief"
                    emoji = emojis[index] if index < len(emojis) else ""
                    sections.append(_pulse_group(label, [child], context=context, emoji=emoji))
                continue
            if block_type == "day_agenda":
                sections.append(_pulse_group("Agenda", [block], context=context, emoji="◷"))
                continue
            if block_type == "source_list":
                deferred_sources.append(block)
                continue

            normalized_title = _title(block).casefold()
            if block_type == "section" and _matches_any(
                normalized_title, "news", "vědět", "vedet", "know"
            ):
                sections.append(_pulse_group("News", [block], context=context))
            elif block_type == "section" and _matches_any(normalized_title, "watch", "sledovat"):
                sections.append(_pulse_group("Watch", [block], context=context, emoji="◎"))
            elif block_type == "callout" and _matches_any(normalized_title, "course", "kurz"):
                sections.append(_pulse_group("Course", [block], context=context))
            else:
                sections.append(_render_block(block, context=context, depth=2))

        if deferred_sources:
            for block in deferred_sources:
                sections.append(_render_block(block, context=context, depth=2))
        elif context.sources:
            sections.append(_render_sources(context.sources, title="Sources", context=context))
        return [section for section in sections if section.strip()]


def _pulse_group(
    label: str,
    blocks: list[dict[str, Any]],
    *,
    context: ProjectionContext,
    emoji: str = "",
) -> str:
    prefix = f"{emoji} " if emoji else ""
    parts = [_heading(f"{prefix}{label}", 2, context)]
    parts.extend(_render_block(block, context=context, depth=3) for block in blocks)
    return "\n\n".join(part for part in parts if part)


def _render_block(
    block: dict[str, Any],
    *,
    context: ProjectionContext,
    depth: int,
) -> str:
    block_type = str(block.get("type") or "section")
    title = _title(block)
    content = _content(block)
    children = _child_blocks(block)
    heading = _heading(title, min(depth, 6), context) if title else ""
    parts: list[str] = []

    if block_type == "hero":
        eyebrow = _text(block.get("eyebrow"))
        subtitle = _text(block.get("subtitle"))
        badges = _string_list(block.get("badges") or block.get("tags"))
        if eyebrow:
            parts.append(f"_{eyebrow}_")
        if title:
            parts.append(_heading(title, 1, context))
        if subtitle:
            parts.append(subtitle)
        if badges:
            parts.append(" · ".join(badges))
        if content:
            parts.append(_markdown_or_plain(content, context))
    elif block_type == "markdown":
        if heading:
            parts.append(heading)
        parts.append(_markdown_or_plain(content, context))
    elif block_type in _CONTAINER_TYPES:
        eyebrow = _text(block.get("eyebrow"))
        subtitle = _text(block.get("subtitle") or block.get("description"))
        if eyebrow:
            parts.append(f"_{eyebrow}_")
        if heading:
            parts.append(heading)
        if subtitle:
            parts.append(subtitle)
        if content:
            parts.append(_markdown_or_plain(content, context))
        if block_type in {"dashboard", "status_grid"}:
            parts.extend(_render_named_items(block, context=context))
    elif block_type in _ITEM_CONTAINER_TYPES:
        if heading:
            parts.append(heading)
        if content:
            parts.append(_markdown_or_plain(content, context))
        items = _objects(block.get("items") or block.get("data") or block.get("entries"))
        for index, item in enumerate(items, start=1):
            item = dict(item)
            item.setdefault("type", "figure" if block_type == "gallery" else "section")
            item.setdefault("title", f"Item {index}")
            parts.append(_render_block(item, context=context, depth=depth + 1))
    elif block_type in {"card", "callout", "status"}:
        icon = icon_symbol(block.get("icon") or block.get("emoji"))
        dek = _text(block.get("dek"))
        href = _text(block.get("href") or block.get("url"))
        if heading:
            parts.append(heading)
        if icon:
            parts.append(icon)
        if dek:
            parts.append(dek)
        status = _scalar(block.get("status"))
        description = content or _text(block.get("description") or block.get("subtitle"))
        if status:
            parts.append(f"Status: {status}")
        if description:
            parts.append(_markdown_or_plain(description, context))
        if href:
            parts.append(_link("Read more", href, context))
        refs = _render_source_refs(
            block.get("citations") or block.get("source_ids") or block.get("sources"),
            context=context,
        )
        if refs:
            parts.append(refs)
        if block_type == "card":
            reference = media_reference(block, context.assets or [])
            if reference:
                parts.append(f"Image: {reference.alt or reference.ref_id}")
    elif block_type == "metric":
        label = _text(block.get("label")) or title
        value = _scalar(block.get("value")) or content
        unit = _scalar(block.get("unit"))
        delta = _scalar(block.get("delta") or block.get("trend"))
        description = _text(block.get("description"))
        metric = f"{label}: " if label else ""
        metric += value
        if unit:
            metric += f" {unit}"
        if delta:
            metric += f" — {delta}"
        parts.append(f"**{metric}**")
        if description:
            parts.append(description)
    elif block_type in {"kv", "key_value"}:
        if heading:
            parts.append(heading)
        parts.extend(_render_named_items(block, context=context))
    elif block_type in {"timeline", "steps"}:
        if heading:
            parts.append(heading)
        parts.extend(_render_timeline(block, context=context))
    elif block_type == "day_agenda":
        if heading:
            parts.append(heading)
        parts.extend(_render_agenda(block, context=context))
    elif block_type in _INCIDENT_TYPES:
        if heading:
            parts.append(heading)
        if content or block.get("description"):
            parts.append(_markdown_or_plain(content or _text(block.get("description")), context))
        parts.extend(_render_incident(block, context=context))
    elif block_type == "quote":
        quote = _text(
            block.get("quote") or block.get("content") or block.get("text") or block.get("body")
        )
        byline = _text(block.get("byline") or block.get("author"))
        if heading:
            parts.append(heading)
        if quote:
            parts.append(f"> {quote}")
        if byline:
            parts.append(f"— {byline}")
    elif block_type == "divider":
        parts.append("---")
    elif block_type == "figure":
        if heading:
            parts.append(heading)
        alt = _text(block.get("alt"))
        caption = _text(block.get("caption") or block.get("description"))
        image_url = _text(block.get("src") or block.get("url"))
        reference = media_reference(block, context.assets or [])
        if caption or alt:
            parts.append(caption or alt)
        if reference:
            parts.append(f"Image: {reference.alt or reference.ref_id}")
        elif image_url:
            parts.append(_link("Image", image_url, context))
        parts.extend(_source_metadata(block, context=context))
    elif block_type in _TABLE_TYPES:
        if heading:
            parts.append(heading)
        caption = _text(block.get("caption") or block.get("description"))
        if caption:
            parts.append(caption)
        parts.append(_render_table(block, context=context))
    elif block_type == "chart":
        if heading:
            parts.append(heading)
        description = _text(block.get("description"))
        if description:
            parts.append(description)
        model = normalize_chart(block)
        if model is not None:
            trend = chart_trend_text(model)
            if trend:
                parts.append(f"Trend: {trend}")
        else:
            parts.append("Chart data is unavailable.")
        parts.extend(_source_metadata(block, context=context))
    elif block_type in {"mermaid", "code"}:
        if heading:
            parts.append(heading)
        language = (
            "mermaid"
            if block_type == "mermaid"
            else _text(block.get("language") or block.get("lang"))
        )
        source = (
            _text(block.get("source") or block.get("code")) if block_type == "mermaid" else content
        )
        source = source or content
        if source:
            parts.append(f"```{language}\n{source}\n```")
    elif block_type in {"link", "link_preview"}:
        url = _text(block.get("href") or block.get("url"))
        label = title or url or "Link"
        if url:
            parts.append(_link(label, url, context))
        elif heading:
            parts.append(heading)
        description = _text(block.get("description")) or content
        if description:
            parts.append(_markdown_or_plain(description, context))
    elif block_type == "source_list":
        source_refs = next(
            (block[key] for key in ("sources", "source_ids", "citations") if key in block),
            None,
        )
        block_sources = (
            context.sources
            if source_refs is None
            else _resolve_sources(source_refs, context.sources)
        )
        parts.append(
            _render_sources(
                block_sources,
                title=title or "Sources",
                context=context,
            )
        )
    elif block_type == "research_answer":
        if heading:
            parts.append(heading)
        description = _text(block.get("description"))
        if description:
            parts.append(description)
        paragraphs = _objects(block.get("paragraphs") or block.get("items"))
        if paragraphs:
            for paragraph in paragraphs:
                text = _text(paragraph.get("text") or paragraph.get("content"))
                refs = _render_source_refs(
                    paragraph.get("citations")
                    or paragraph.get("sources")
                    or paragraph.get("source_ids"),
                    context=context,
                )
                parts.append(" ".join(part for part in (text, refs) if part))
        elif content or block.get("answer"):
            parts.append(_markdown_or_plain(_text(block.get("answer")) or content, context))
        key_points = _string_list(block.get("key_points") or block.get("highlights"))
        if key_points:
            parts.append(_list(key_points, context=context))
    elif block_type in {"evidence_report", "claim_cards"}:
        if heading:
            parts.append(heading)
        description = _text(block.get("description"))
        if description:
            parts.append(description)
        parts.extend(_render_claims(block, context=context, depth=depth + 1))
    else:
        if heading:
            parts.append(heading)
        if content:
            parts.append(_markdown_or_plain(content, context))
        else:
            parts.extend(_render_scalar_fields(block, context=context))

    if block_type not in _ITEM_CONTAINER_TYPES:
        parts.extend(_render_block(child, context=context, depth=depth + 1) for child in children)
    return "\n\n".join(part.strip() for part in parts if part and part.strip())


def _render_named_items(
    block: dict[str, Any],
    *,
    context: ProjectionContext,
) -> list[str]:
    items = block.get("items") or block.get("data") or block.get("steps")
    if isinstance(items, dict):
        return [
            _list(
                [f"{_humanize(key)}: {_scalar(value)}" for key, value in items.items()],
                context=context,
            )
        ]
    rendered: list[str] = []
    if isinstance(items, list):
        values = []
        for item in items:
            if isinstance(item, dict):
                label = _text(item.get("label") or item.get("key") or item.get("name"))
                value = _scalar(item.get("value") or item.get("text") or item.get("content"))
                if label or value:
                    values.append(": ".join(part for part in (label, value) if part))
            elif _scalar(item):
                values.append(_scalar(item))
        if values:
            rendered.append(_list(values, context=context))
    if not rendered:
        rendered.extend(_render_scalar_fields(block, context=context))
    return rendered


def _render_scalar_fields(
    block: dict[str, Any],
    *,
    context: ProjectionContext,
) -> list[str]:
    ignored = {
        "type",
        *_TITLE_KEYS,
        *_CONTENT_KEYS,
        "subtitle",
        "description",
        "eyebrow",
        "badges",
        "tags",
        "blocks",
        "children",
        "items",
        "data",
        "rows",
        "sources",
    }
    values = [
        f"{_humanize(key)}: {_scalar(value)}"
        for key, value in block.items()
        if key not in ignored and not isinstance(value, (dict, list)) and _scalar(value)
    ]
    return [_list(values, context=context)] if values else []


def _render_timeline(
    block: dict[str, Any],
    *,
    context: ProjectionContext,
) -> list[str]:
    items = block.get("items") or block.get("data") or block.get("steps")
    values: list[str] = []
    if isinstance(items, list):
        for index, item in enumerate(items, start=1):
            if not isinstance(item, dict):
                continue
            marker = _scalar(item.get("time") or item.get("timestamp") or item.get("step"))
            title = _title(item) or f"Item {index}"
            description = _text(item.get("content") or item.get("description"))
            status = _scalar(item.get("status") or item.get("tone"))
            prefix = f"{marker} — " if marker else ""
            suffix = f" ({status})" if status else ""
            values.append(f"{prefix}{title}{suffix}{f': {description}' if description else ''}")
    return [_ordered_list(values)] if values else []


def _render_agenda(
    block: dict[str, Any],
    *,
    context: ProjectionContext,
) -> list[str]:
    values: list[str] = []
    timezone = _text(block.get("timezone"))
    if timezone:
        values.append(f"Timezone: {timezone}")
    raw_items = block.get("items") or block.get("events")
    if isinstance(raw_items, list):
        for item in raw_items:
            if not isinstance(item, dict):
                continue
            title = _title(item)
            if not title:
                continue
            if item.get("all_day") is True or item.get("allDay") is True:
                values.append(f"All day — {title}")
                continue
            start = _agenda_time(
                item.get("start") or item.get("start_iso") or item.get("start_time"),
                timezone=timezone,
            )
            end = _agenda_time(
                item.get("end") or item.get("end_iso") or item.get("end_time"),
                timezone=timezone,
            )
            if end == start:
                end = ""
            time_range = start + (f"–{end}" if end else "")
            kind = "Free" if item.get("kind") == "free" else ""
            location = _text(item.get("location"))
            details = " · ".join(value for value in (kind, location) if value)
            next_label = "Next: " if item.get("next") is True else ""
            values.append(
                f"{next_label}{time_range} — {title}{f' ({details})' if details else ''}".strip(
                    " —"
                )
            )
    tasks = _objects(block.get("tasks"))
    if tasks:
        values.append("Tasks:")
        values.extend(f"- {_title(task)}" for task in tasks if _title(task))
    if not values:
        values.append("Nothing is scheduled today.")
    values.extend(_source_metadata(block, context=context))
    return values


def _render_incident(
    block: dict[str, Any],
    *,
    context: ProjectionContext,
) -> list[str]:
    parts = _render_timeline(block, context=context)
    checklist = block.get("checklist") or block.get("remediation") or block.get("actions")
    if isinstance(checklist, list):
        values: list[str] = []
        for item in checklist:
            if not isinstance(item, dict):
                continue
            title = _title(item) or _text(item.get("action"))
            done = (
                item.get("done") is True
                or item.get("checked") is True
                or item.get("status") == "done"
            )
            values.append(f"[{'x' if done else ' '}] {title}")
        if values:
            parts.append("\n".join(f"- {value}" for value in values))
    parts.extend(_render_scalar_fields(block, context=context))
    return parts


def _render_table(block: dict[str, Any], *, context: ProjectionContext) -> str:
    rows = block.get("rows") or block.get("data")
    if not isinstance(rows, list) or not rows:
        scalar = _render_scalar_fields(block, context=context)
        return "\n".join(scalar)
    headers = block.get("columns")
    keys: list[str] = []
    labels: list[str] = []
    if isinstance(headers, list):
        for header in headers:
            if isinstance(header, dict):
                key = _text(header.get("key") or header.get("id") or header.get("label"))
                label = _text(
                    header.get("label")
                    or header.get("title")
                    or header.get("key")
                    or header.get("id")
                )
            else:
                key = _scalar(header)
                label = _humanize(key)
            if key:
                keys.append(key)
                labels.append(label or _humanize(key))
    elif isinstance(rows[0], dict):
        keys = [str(key) for key in rows[0]]
        labels = [_humanize(key) for key in keys]

    normalized_rows: list[list[str]] = []
    for row in rows:
        if isinstance(row, dict):
            normalized_rows.append([_scalar(row.get(key)) for key in keys])
        elif isinstance(row, list):
            normalized_rows.append([_scalar(value) for value in row])
        else:
            normalized_rows.append([_scalar(row)])
    if not labels:
        width = max((len(row) for row in normalized_rows), default=1)
        labels = [f"Value {index}" for index in range(1, width + 1)]

    escaped_labels = [_escape_table_cell(value) for value in labels]
    lines = [
        "| " + " | ".join(escaped_labels) + " |",
        "| " + " | ".join("---" for _ in escaped_labels) + " |",
    ]
    lines.extend(
        "| "
        + " | ".join(
            _escape_table_cell(row[index] if index < len(row) else "")
            for index in range(len(labels))
        )
        + " |"
        for row in normalized_rows
    )
    return "\n".join(lines)


def _render_claims(
    block: dict[str, Any],
    *,
    context: ProjectionContext,
    depth: int,
) -> list[str]:
    claims = _objects(block.get("claims") or block.get("items") or block.get("data"))
    parts: list[str] = []
    for index, claim in enumerate(claims, start=1):
        title = _text(claim.get("title") or claim.get("claim")) or f"Claim {index}"
        label = _text(claim.get("label") or claim.get("category"))
        summary = _text(claim.get("content") or claim.get("summary"))
        confidence = _confidence(claim.get("confidence", claim.get("score")))
        claim_parts = [_heading(title, min(depth, 6), context)]
        if label:
            claim_parts.append(f"_{label}_")
        if summary:
            claim_parts.append(_markdown_or_plain(summary, context))
        if confidence:
            claim_parts.append(f"Confidence: {confidence}")
        for evidence in _objects(claim.get("evidence") or claim.get("snippets")):
            quote_text = _text(
                evidence.get("text") or evidence.get("quote") or evidence.get("content")
            )
            source = _scalar(evidence.get("source") or evidence.get("url"))
            if quote_text:
                claim_parts.append(f"> {quote_text}" + (f" — {source}" if source else ""))
        refs = _render_source_refs(
            claim.get("sources") or claim.get("citations") or claim.get("source_ids"),
            context=context,
        )
        if refs:
            claim_parts.append(refs)
        parts.append("\n\n".join(claim_parts))
    for label, key in (("Caveats", "caveats"), ("Contradictions", "contradictions")):
        values = _string_list(block.get(key))
        if values:
            parts.append(
                f"{_heading(label, min(depth, 6), context)}\n\n{_list(values, context=context)}"
            )
    return parts


def _render_sources(
    sources: list[dict[str, Any]],
    *,
    title: str,
    context: ProjectionContext,
) -> str:
    parts = [_heading(title, 2, context)]
    values: list[str] = []
    for index, source in enumerate(_deduplicate_sources(sources), start=1):
        label = _title(source) or _text(source.get("site")) or f"Source {index}"
        url = _text(source.get("url") or source.get("href"))
        timestamp = _text(
            source.get("timestamp") or source.get("updated_at") or source.get("refreshed_at")
        )
        rendered = _link(label, url, context) if url else label
        if timestamp:
            rendered += f" — {timestamp}"
        values.append(rendered)
    if values:
        parts.append(_ordered_list(values))
    else:
        parts.append("No sources were provided.")
    return "\n\n".join(parts)


def _source_metadata(
    block: dict[str, Any],
    *,
    context: ProjectionContext,
) -> list[str]:
    source = block.get("source")
    if isinstance(source, dict):
        label = _title(source) or _text(source.get("id"))
        url = _text(source.get("url") or source.get("href"))
        timestamp = _text(source.get("refreshed_at") or source.get("timestamp"))
    else:
        label = _text(source or block.get("source_label"))
        url = _text(block.get("source_url"))
        timestamp = _text(
            block.get("observed_at") or block.get("timestamp") or block.get("freshness")
        )
    if not (label or url or timestamp):
        return []
    rendered = _link(label or url or "Source", url, context) if url else label
    if timestamp:
        rendered += f" — {timestamp}"
    return [f"Source: {rendered}"]


def _render_source_refs(value: Any, *, context: ProjectionContext) -> str:
    sources = _resolve_sources(value, context.sources)
    if not sources:
        return ""
    rendered = []
    for source in sources:
        label = _title(source) or _text(source.get("id")) or "Source"
        url = _text(source.get("url") or source.get("href"))
        rendered.append(_link(label, url, context) if url else label)
    return "Sources: " + ", ".join(rendered)


def _resolve_sources(value: Any, available: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if value is None:
        return []
    raw_values = value if isinstance(value, list) else [value]
    resolved: list[dict[str, Any]] = []
    for raw in raw_values:
        if isinstance(raw, dict):
            reference = _text(raw.get("source_id") or raw.get("sourceId") or raw.get("ref"))
            if reference:
                match = next(
                    (
                        source
                        for source in available
                        if reference
                        in {
                            _text(source.get("id")),
                            _text(source.get("key")),
                            _text(source.get("citation_id")),
                            _text(source.get("url")),
                            _text(source.get("href")),
                            _title(source),
                        }
                    ),
                    None,
                )
                if match is not None:
                    resolved_source = dict(match)
                    label = _text(raw.get("label"))
                    if label:
                        resolved_source["title"] = label
                    resolved.append(resolved_source)
                    continue
            resolved.append(raw)
            continue
        token = _scalar(raw)
        match = next(
            (
                source
                for source in available
                if token
                in {
                    _text(source.get("id")),
                    _text(source.get("key")),
                    _text(source.get("citation_id")),
                    _text(source.get("url")),
                    _text(source.get("href")),
                    _title(source),
                }
            ),
            None,
        )
        resolved.append(match or {"title": token})
    return _deduplicate_sources(resolved)


def _apply_total_safety_limit(
    sections: list[str],
    *,
    context: ProjectionContext,
) -> list[str]:
    total = sum(len(section) for section in sections)
    if total <= RICH_MARKDOWN_MAX_CHARS:
        return sections
    notice = "_Some sections were omitted because this document exceeds the safety limit._"
    budget = RICH_MARKDOWN_MAX_CHARS - len(notice) - 2
    kept: list[str] = []
    used = 0
    for section in sections:
        separator = 2 if kept else 0
        if used + separator + len(section) <= budget:
            kept.append(section)
            used += separator + len(section)
            continue
        remaining = budget - used - separator
        if remaining > 200:
            safe_prefix = split_markdown(section, max_length=remaining)
            if safe_prefix:
                kept.append(safe_prefix[0])
        break
    kept.append(notice)
    return kept


def split_markdown(section: str, *, max_length: int) -> list[str]:
    """Split GFM without leaving links, fences, or tables malformed."""

    lines = section.splitlines()
    blocks: list[tuple[str, list[str]]] = []
    index = 0
    while index < len(lines):
        if lines[index].startswith("```"):
            fence = [lines[index]]
            index += 1
            while index < len(lines):
                fence.append(lines[index])
                closed = lines[index].startswith("```")
                index += 1
                if closed:
                    break
            blocks.append(("code", fence))
            continue
        if lines[index].lstrip().startswith("|"):
            table: list[str] = []
            while index < len(lines) and lines[index].lstrip().startswith("|"):
                table.append(lines[index])
                index += 1
            blocks.append(("table", table))
            continue
        text: list[str] = []
        while (
            index < len(lines)
            and not lines[index].startswith("```")
            and not lines[index].lstrip().startswith("|")
        ):
            text.append(lines[index])
            index += 1
        blocks.append(("text", text))

    result: list[str] = []
    for kind, block_lines in blocks:
        if kind == "code":
            result.extend(_split_code_fence(block_lines, max_length=max_length))
        elif kind == "table":
            result.extend(_split_markdown_table(block_lines, max_length=max_length))
        else:
            result.extend(_split_markdown_text("\n".join(block_lines), max_length=max_length))
    return [part for part in result if part]


def _split_code_fence(lines: list[str], *, max_length: int) -> list[str]:
    opener = lines[0] if lines else "```"
    body = lines[1:]
    if body and body[-1].startswith("```"):
        body = body[:-1]
    overhead = len(opener) + len("\n\n```")
    if overhead >= max_length:
        return _split_plain_text("\n".join(body), max_length=max_length)
    body_parts = _split_plain_text("\n".join(body), max_length=max_length - overhead)
    return [f"{opener}\n{part}\n```" for part in body_parts]


def _split_markdown_table(lines: list[str], *, max_length: int) -> list[str]:
    if len(lines) < 2:
        return _split_markdown_text("\n".join(lines), max_length=max_length)
    header = "\n".join(lines[:2])
    rows = lines[2:]
    if len(header) > max_length or any(len(header) + len(row) + 1 > max_length for row in rows):
        plain = "\n".join(_table_row_to_plain(line) for line in lines if line.strip())
        return _split_plain_text(plain, max_length=max_length)
    chunks: list[str] = []
    current = header
    for row in rows:
        candidate = f"{current}\n{row}"
        if len(candidate) <= max_length:
            current = candidate
        else:
            chunks.append(current)
            current = f"{header}\n{row}"
    chunks.append(current)
    return chunks


def _table_row_to_plain(line: str) -> str:
    return " | ".join(cell.strip() for cell in line.strip().strip("|").split("|"))


_MARKDOWN_LINK_TOKEN = re.compile(r"!?\[((?:\\.|[^\]])*)\]\(([^)\s]+)\)")


def _split_markdown_text(text: str, *, max_length: int) -> list[str]:
    tokens: list[str] = []
    position = 0
    for match in _MARKDOWN_LINK_TOKEN.finditer(text):
        tokens.extend(re.findall(r"\s+|[^\s]+", text[position : match.start()]))
        tokens.append(match.group(0))
        position = match.end()
    tokens.extend(re.findall(r"\s+|[^\s]+", text[position:]))

    chunks: list[str] = []
    current = ""
    for token in tokens:
        if len(token) > max_length and (link_match := _MARKDOWN_LINK_TOKEN.fullmatch(token)):
            label, url = link_match.groups()
            safe_url = _safe_url(url)
            token_parts = (
                [safe_url]
                if safe_url and len(safe_url) <= max_length
                else _split_plain_text(
                    f"{label or 'Link'} (link omitted: too long)",
                    max_length=max_length,
                )
            )
        elif len(token) > max_length:
            token_parts = [token[i : i + max_length] for i in range(0, len(token), max_length)]
        else:
            token_parts = [token]
        for part in token_parts:
            candidate = current + part
            if len(candidate) <= max_length:
                current = candidate
                continue
            if current.strip():
                chunks.append(current.strip())
            current = part.lstrip()
    if current.strip():
        chunks.append(current.strip())
    return chunks


def _split_plain_text(text: str, *, max_length: int) -> list[str]:
    chunks: list[str] = []
    remaining = text.strip()
    while remaining:
        if len(remaining) <= max_length:
            chunks.append(remaining)
            break
        boundary = max(
            remaining.rfind("\n", 0, max_length + 1),
            remaining.rfind(" ", 0, max_length + 1),
        )
        if boundary <= 0:
            boundary = max_length
        chunks.append(remaining[:boundary].rstrip())
        remaining = remaining[boundary:].lstrip()
    return chunks


def _full_view_section(
    link: DeliverableViewLink | None,
    *,
    deliverable_id: str,
    context: ProjectionContext,
) -> str:
    if link is None or not (safe_url := _safe_url(link.url)):
        return f"_Open the full version in Cognis ({deliverable_id})._"
    expiry = " (signed link expires)" if link.public and link.expires_at else ""
    return f"[Open full version]({safe_url}){expiry}"


def _heading(title: str, level: int, context: ProjectionContext) -> str:
    if not title:
        return ""
    return f"{'#' * max(1, min(level, 6))} {_escape_markdown_inline(title)}"


def _markdown_or_plain(value: str, context: ProjectionContext) -> str:
    del context
    sanitized = _sanitize_markdown(value)
    return sanitized.strip()


def _sanitize_markdown(value: str) -> str:
    """Keep supported Markdown while removing unsafe links and raw HTML."""

    def replace_link(match: re.Match[str]) -> str:
        prefix = "!" if match.group(0).startswith("!") else ""
        label, url = match.groups()
        safe_url = _safe_url(url)
        if not safe_url:
            return _escape_markdown_inline(label or "Link")
        return f"{prefix}[{_escape_markdown_inline(label)}]({safe_url})"

    sanitized = _MARKDOWN_LINK_TOKEN.sub(replace_link, value)
    return re.sub(r"<(?=[A-Za-z/!])", "&lt;", sanitized)


def _safe_url(value: str) -> str | None:
    text = _safe_plain_text(value).strip()
    if not text or any(character.isspace() for character in text):
        return None
    try:
        parsed = urlsplit(text)
    except ValueError:
        return None
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.netloc:
        return None
    try:
        parsed.netloc.encode("ascii")
    except UnicodeEncodeError:
        return None
    path = quote(parsed.path, safe="/:@-._~!$&'*+,;=%")
    query = quote(parsed.query, safe="=&:@-._~!$'*+,;/?%")
    fragment = quote(parsed.fragment, safe="-._~!$&'*+,;=:@/?%")
    return urlunsplit((parsed.scheme.lower(), parsed.netloc, path, query, fragment))


def _safe_plain_text(value: str) -> str:
    return "".join(
        character for character in value if character in {"\n", "\t"} or ord(character) >= 32
    )


def _escape_markdown_inline(value: str) -> str:
    safe = _safe_plain_text(value).replace("\\", "\\\\")
    return re.sub(r"([`*_[\]()<>#+.!|>-])", r"\\\1", safe)


def _first_block_has_equivalent_title(
    blocks: list[dict[str, Any]],
    title: str,
) -> bool:
    if not blocks:
        return False
    first = blocks[0]
    if _equivalent_title(_title(first), title):
        return True
    if first.get("type") == "markdown":
        return _starts_with_equivalent_h1(_content(first), title, markdown=True)
    return False


def _starts_with_equivalent_h1(content: str, title: str, *, markdown: bool) -> bool:
    first_line = next((line.strip() for line in content.splitlines() if line.strip()), "")
    if markdown:
        match = re.fullmatch(r"#\s+(.+?)\s*#*", first_line)
        return bool(match and _equivalent_title(match.group(1), title))
    return _equivalent_title(first_line, title)


def _equivalent_title(left: str, right: str) -> bool:
    def normalize(value: str) -> str:
        return re.sub(r"\s+", " ", value).strip().casefold()

    return bool(left and right and normalize(left) == normalize(right))


def _link(label: str, url: str, context: ProjectionContext) -> str:
    del context
    safe_label = _escape_markdown_inline(label)
    safe_url = _safe_url(url)
    if not safe_url:
        return safe_label
    return f"[{safe_label}]({safe_url})"


def _list(values: list[str], *, context: ProjectionContext) -> str:
    del context
    return "\n".join(f"- {value}" for value in values)


def _ordered_list(values: list[str]) -> str:
    return "\n".join(f"{index}. {value}" for index, value in enumerate(values, start=1))


def _agenda_time(value: Any, *, timezone: str) -> str:
    text = _scalar(value)
    if not text:
        return ""
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if timezone:
            target = ZoneInfo(timezone)
            parsed = (
                parsed.replace(tzinfo=target)
                if parsed.tzinfo is None
                else parsed.astimezone(target)
            )
        return parsed.strftime("%H:%M")
    except (ValueError, ZoneInfoNotFoundError):
        return text


def _confidence(value: Any) -> str:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        percent = value * 100 if 0 <= value <= 1 else value
        return f"{percent:g}%"
    return _scalar(value)


def _escape_table_cell(value: str) -> str:
    return value.replace("\\", "\\\\").replace("|", "\\|").replace("\n", " ")


def _contains_source_list(blocks: list[dict[str, Any]]) -> bool:
    for block in blocks:
        if block.get("type") == "source_list" or _contains_source_list(_child_blocks(block)):
            return True
    return False


def _child_blocks(block: dict[str, Any]) -> list[dict[str, Any]]:
    return _objects(block.get("blocks") or block.get("children"))


def _deduplicate_sources(sources: list[dict[str, Any]]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for source in sources:
        identity = _text(source.get("id") or source.get("url") or source.get("href")) or _title(
            source
        )
        if identity in seen:
            continue
        seen.add(identity)
        result.append(source)
    return result


def _objects(value: Any) -> list[dict[str, Any]]:
    return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []


def _string_list(value: Any) -> list[str]:
    return [text for item in value if (text := _scalar(item))] if isinstance(value, list) else []


def _title(value: dict[str, Any]) -> str:
    return next((_text(value.get(key)) for key in _TITLE_KEYS if _text(value.get(key))), "")


def _content(value: dict[str, Any]) -> str:
    return next((_text(value.get(key)) for key in _CONTENT_KEYS if _text(value.get(key))), "")


def _text(value: Any) -> str:
    return (
        str(value).strip()
        if isinstance(value, (str, int, float)) and not isinstance(value, bool)
        else ""
    )


def _scalar(value: Any) -> str:
    if isinstance(value, bool):
        return "Yes" if value else "No"
    return _text(value)


def _humanize(value: str) -> str:
    return value.replace("_", " ").strip().capitalize()


def _matches_any(value: str, *tokens: str) -> bool:
    return any(token in value for token in tokens)


register_presentation_projector("pulse", PulseProjector())
