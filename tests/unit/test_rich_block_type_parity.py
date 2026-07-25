"""Render-contract parity test for rich deliverable block types.

Two renderers must agree on which block types exist and both must
actually render every one of them without falling back to a raw/unknown
placeholder. Historically these have drifted independently -- e.g. `action`
was added to the Python `SUPPORTED_RICH_BLOCK_TYPES` set (and its JSON
schema enum, exposed to the LLM) without being added to the TypeScript
set, so the model could author a block type that the interactive web
renderer didn't even recognize as valid and would immediately reject as
"Unsupported block".

This module has two responsibilities:

1. Assert the Python and TypeScript `SUPPORTED_RICH_BLOCK_TYPES` sets are
   byte-identical (parsed directly from the TS source, no build step
   required).
2. Assert every supported type actually renders through the Python static/
   PDF renderer without hitting the generic key-value fallback path in a
   way that loses the block's authored content.

The TypeScript-side counterpart of responsibility 2 is
`RichDeliverable.render.test.ts` ("dispatches every supported block type
without using unsupported fallback").
"""

from __future__ import annotations

import re
from pathlib import Path
from types import SimpleNamespace

from cognis.models.deliverable import SUPPORTED_RICH_BLOCK_TYPES
from cognis.rendering.deliverables import render_standalone_html

_UI_RICH_DELIVERABLE_TS = (
    Path(__file__).resolve().parents[2] / "ui" / "src" / "lib" / "rich-deliverable.ts"
)


def _ts_supported_block_types() -> set[str]:
    source = _UI_RICH_DELIVERABLE_TS.read_text(encoding="utf-8")
    match = re.search(
        r"SUPPORTED_RICH_BLOCK_TYPES\s*=\s*new Set\(\[(.*?)\]\)", source, re.DOTALL
    )
    assert match is not None, "SUPPORTED_RICH_BLOCK_TYPES Set literal not found in rich-deliverable.ts"
    body = match.group(1)
    return set(re.findall(r"'([a-z_]+)'", body))


def test_python_and_typescript_supported_block_types_are_identical() -> None:
    ts_types = _ts_supported_block_types()
    assert ts_types, "failed to parse any block types from rich-deliverable.ts"
    assert ts_types == SUPPORTED_RICH_BLOCK_TYPES, (
        "SUPPORTED_RICH_BLOCK_TYPES drift between Python and TypeScript.\n"
        f"Python only: {sorted(SUPPORTED_RICH_BLOCK_TYPES - ts_types)}\n"
        f"TypeScript only: {sorted(ts_types - SUPPORTED_RICH_BLOCK_TYPES)}"
    )


def _row(**overrides: object) -> SimpleNamespace:
    values: dict[str, object] = {
        "deliverable_id": "dlv_contract",
        "version": 1,
        "format": "rich",
        "title": "Contract report",
        "content": "Fallback",
        "content_hash": "content-hash",
        "rich_hash": "rich-hash",
        "rich_payload": {"blocks": []},
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _block_for(block_type: str) -> dict[str, object]:
    base: dict[str, object] = {
        "type": block_type,
        "title": f"{block_type} title",
        "content": f"{block_type} content marker",
    }
    if block_type in {"section", "stack", "columns", "grid", "card_grid"}:
        return {**base, "blocks": [{"type": "markdown", "content": f"{block_type} child"}]}
    if block_type in {"tabs", "accordion", "modal"}:
        return {
            **base,
            "items": [{"type": "markdown", "title": f"{block_type} item", "content": f"{block_type} item"}],
        }
    if block_type == "gallery":
        return {**base, "items": [{"url": "https://example.com/image.png", "caption": "Gallery item"}]}
    if block_type == "callout":
        return {**base, "tone": "success"}
    if block_type == "action":
        return {**base, "icon": "check"}
    if block_type == "metric":
        return {**base, "value": 42, "delta": "+2%", "description": "metric description"}
    if block_type in {"kv", "key_value"}:
        return {**base, "items": [{"label": "Key", "value": "Value"}]}
    if block_type in {"timeline", "steps"}:
        return {**base, "items": [{"title": "Step", "content": "Done"}]}
    if block_type == "quote":
        return {**base, "quote": "quoted text", "byline": "Author"}
    if block_type == "figure":
        return {**base, "url": "https://example.com/image.png", "alt": "Example image", "caption": "Caption"}
    if block_type in {"table", "comparison_matrix", "decision_matrix"}:
        return {**base, "columns": ["name", "score"], "rows": [{"name": "A", "score": 1}]}
    if block_type == "research_answer":
        return {
            **base,
            "paragraphs": [{"text": "Answer", "citations": ["s1"]}],
            "sources": [{"id": "s1", "title": "Source"}],
        }
    if block_type in {"evidence_report", "claim_cards"}:
        return {**base, "claims": [{"title": "Claim", "confidence": "high", "evidence": [{"text": "Evidence"}]}]}
    if block_type == "chart":
        return {**base, "rows": [{"label": "A", "value": 1}]}
    if block_type == "mermaid":
        return {**base, "source": "graph TD; A-->B"}
    if block_type in {"link", "link_preview"}:
        return {**base, "url": "https://example.com", "site": "Example"}
    if block_type == "source_list":
        return {**base, "sources": [{"title": "Source", "url": "https://example.com"}]}
    if block_type == "day_agenda":
        return {**base, "date": "2026-01-01", "timezone": "UTC", "now": "2026-01-01T08:00:00+00:00", "items": []}
    if block_type in {"incident_timeline", "incident_checklist", "checklist"}:
        return {**base, "items": [{"title": "Step", "status": "done"}]}
    if block_type == "code":
        return {**base, "language": "python"}
    return base


_DELIVERABLES_MODULE = Path(__file__).resolve().parents[2] / "cognis" / "rendering" / "deliverables.py"


def _render_block_dispatch_source() -> str:
    """Return the source of `_render_block`'s dispatch ladder.

    A dynamic content-marker check can't reliably distinguish "hit a
    dedicated branch" from "fell through to the generic fallback", because
    the generic fallback also renders `content` verbatim when present (it
    only shows a raw key-value dump when content is absent). The real
    historical bug (`action` added to SUPPORTED_RICH_BLOCK_TYPES without a
    dispatch branch) is a *static* omission, so this is checked statically:
    every supported type must appear as a literal string within the
    `_render_block` function body.
    """

    source = _DELIVERABLES_MODULE.read_text(encoding="utf-8")
    match = re.search(r"\ndef _render_block\(.*?\n\ndef _heading\(", source, re.DOTALL)
    assert match is not None, "_render_block function body not found"
    body = match.group(0)
    # Strip full-line and trailing comments so a stray mention of a type name
    # in prose (e.g. explaining why a variant defaults a certain way) can
    # never mask a genuinely missing dispatch condition.
    code_lines = [re.sub(r"(?<!['\"])#.*$", "", line) for line in body.splitlines()]
    return "\n".join(code_lines)


def test_every_supported_block_type_has_an_explicit_render_block_dispatch_branch() -> None:
    dispatch_source = _render_block_dispatch_source()
    missing = [
        block_type
        for block_type in sorted(SUPPORTED_RICH_BLOCK_TYPES)
        if f'"{block_type}"' not in dispatch_source
    ]
    assert not missing, (
        "Block types missing an explicit dispatch branch in _render_block "
        f"(would silently fall through to the generic key-value fallback): {missing}"
    )


def test_every_supported_block_type_renders_without_raising() -> None:
    """Smoke test: every block type renders through the full standalone HTML
    pipeline without raising, and the deliverable's own title still appears
    exactly once (guards against a dispatch branch crashing or swallowing
    the rest of the document)."""

    blocks = [_block_for(block_type) for block_type in sorted(SUPPORTED_RICH_BLOCK_TYPES)]
    rendered = render_standalone_html(_row(rich_payload={"blocks": blocks}))

    assert rendered.count("<title>Contract report</title>") == 1
    assert "</html>" in rendered
