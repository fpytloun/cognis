from __future__ import annotations

import asyncio
import re
import time
from contextlib import asynccontextmanager
from dataclasses import fields
from datetime import datetime
from io import BytesIO
from types import SimpleNamespace
from typing import Any
from unittest.mock import ANY, AsyncMock

import pytest
from bs4 import BeautifulSoup
from fastapi import FastAPI
from pypdf import PdfReader
from starlette.testclient import TestClient

from cognis.api.middleware import AuthenticationMiddleware
from cognis.api.routes import deliverables as deliverable_routes
from cognis.core import deliverable_links
from cognis.models.deliverable import CANONICAL_CHART_TYPES as MODEL_CHART_TYPES
from cognis.rendering.deliverables import (
    DeliverableRenderError,
    PublicationContext,
    _emoji_spans,
    _markdown_headings,
    _substitute_emoji,
    deliverable_cache_key,
    render_pdf_bytes,
    render_standalone_html,
)
from cognis.rendering.rich_visuals import (
    CANONICAL_CHART_TYPES,
    CHART_SPEC_VERSION,
    ChartAxis,
    ChartModel,
    ChartSeries,
    MediaReference,
    ResolvedMedia,
    chart_rows,
    chart_trend_text,
    normalize_chart,
    render_chart_svg,
)
from cognis.security import RequestRateLimiter


def _share_token(url: str) -> str:
    parts = url.rstrip("/").split("/")
    return parts[-1] if parts[-2] == "s" else parts[-2]


def _row(**overrides: object) -> SimpleNamespace:
    values = {
        "deliverable_id": "dlv_render",
        "version": 1,
        "format": "rich",
        "title": "Rich report",
        "content": "Fallback",
        "content_hash": "content-hash",
        "rich_hash": "rich-hash",
        "rich_payload": {
            "blocks": [
                {"type": "card", "title": "Finding", "content": "**Body**"},
                {"type": "table", "title": "Data", "rows": [{"Name": "alpha", "Value": 1}]},
            ]
        },
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _client(factory: object, monkeypatch: pytest.MonkeyPatch, *, email: str) -> TestClient:
    app = FastAPI()
    app.include_router(deliverable_routes.router)
    app.state.session_factory = factory
    app.state.artifact_store = factory.artifact_store  # type: ignore[attr-defined]
    app.state.config = SimpleNamespace(deliverable_share_link_ttl_seconds=604800)
    monkeypatch.setattr(
        deliverable_routes,
        "require_current_user",
        lambda _request: SimpleNamespace(email=email),
    )
    return TestClient(app)


def _middleware_client(
    factory: object,
    *,
    public_share_rate_limiter: RequestRateLimiter | None = None,
    public_share_client_rate_limiter: RequestRateLimiter | None = None,
    api_rate_limiter: RequestRateLimiter | None = None,
    client_address: tuple[str, int] = ("testclient", 50000),
    trusted_proxy_cidrs: tuple[str, ...] = (),
) -> TestClient:
    app = FastAPI()
    app.include_router(deliverable_routes.router)
    app.add_middleware(AuthenticationMiddleware)
    app.state.session_factory = factory
    app.state.artifact_store = factory.artifact_store  # type: ignore[attr-defined]
    app.state.auth_provider = object()
    app.state.password_hasher = object()
    app.state.config = SimpleNamespace(
        deliverable_share_link_ttl_seconds=604800,
        trusted_proxy_cidrs=trusted_proxy_cidrs,
    )
    if public_share_rate_limiter is not None:
        app.state.public_share_rate_limiter = public_share_rate_limiter
    if public_share_client_rate_limiter is not None:
        app.state.public_share_client_rate_limiter = public_share_client_rate_limiter
    if api_rate_limiter is not None:
        app.state.api_rate_limiter = api_rate_limiter
    return TestClient(app, client=client_address)


def test_standalone_resolves_mermaid_and_source_list_compatibility_aliases() -> None:
    rendered = render_standalone_html(
        _row(
            rich_payload={
                "blocks": [
                    {
                        "type": "mermaid",
                        "title": "How a cat maps a doorway",
                        "code": "flowchart LR\n  Air --> Whiskers",
                    },
                    {
                        "type": "source_list",
                        "sources": [
                            {
                                "source_id": " sweet ",
                                "label": "Why cats do not taste sweetness",
                            }
                        ],
                    },
                ],
                "sources": [
                    {
                        "id": "sweet",
                        "title": "Pseudogenization of a Sweet-Receptor Gene",
                        "url": "https://doi.org/10.1371/journal.pgen.0010003",
                    }
                ],
            }
        )
    )

    assert "Air --&gt; Whiskers" in rendered
    soup = BeautifulSoup(rendered, "html.parser")
    source = soup.find("a", string="Why cats do not taste sweetness")
    assert source is not None
    assert source.get("href") == "https://doi.org/10.1371/journal.pgen.0010003"
    assert "Source 1" not in rendered


def test_publication_toc_policy_overrides_depth_and_duplicate_heading_ids() -> None:
    blocks = [
        {"type": "section", "title": "Overview"},
        {
            "type": "section",
            "title": "Overview",
            "blocks": [{"type": "section", "title": "Technical detail"}],
        },
        {"type": "section", "title": "Results"},
        {"type": "section", "title": "Appendix"},
    ]
    rendered = render_standalone_html(
        _row(rich_payload={"metadata": {"toc": {"enabled": True, "depth": 3}}, "blocks": blocks})
    )

    assert 'aria-label="Table of contents"' in rendered
    assert 'href="#overview"' in rendered
    assert 'href="#overview-2"' in rendered
    assert 'id="overview"' in rendered
    assert 'id="overview-2"' in rendered
    assert 'data-level="3"' in rendered
    soup = BeautifulSoup(rendered, "html.parser")
    assert (
        soup.select_one('.document-toc li[data-level="2"] > ol li[data-level="3"]').get_text(
            strip=True
        )
        == "Technical detail"
    )
    assert soup.select(".document-toc [class*=badge], .document-toc [class*=card]") == []

    forced_off = render_standalone_html(
        _row(rich_payload={"metadata": {"toc": False}, "blocks": blocks})
    )
    assert 'aria-label="Table of contents"' not in forced_off


def test_markdown_headings_skips_fenced_code_like_the_web_renderer() -> None:
    """`_markdown_headings` feeds a TOC-enablement heading-count threshold
    (see `_wrapped_standalone_rich_payload` in
    cognis/api/routes/deliverables.py); it must mirror the web renderer's
    fence-aware `extractMarkdownHeadings` (ui/src/lib/markdown.ts) so a `#`
    line inside a fenced code sample (a shell/Python comment, for example)
    is never miscounted as a real heading by only one of the two
    renderers."""

    content = "\n".join(
        [
            "# Real heading",
            "",
            "```python",
            "# Not a heading, just a Python comment",
            "## Also not a heading",
            "```",
            "",
            "## Another real heading",
        ]
    )

    headings = _markdown_headings(content)

    assert headings == [(1, "Real heading"), (2, "Another real heading")]


def test_markdown_headings_handles_unclosed_fence_by_skipping_to_end() -> None:
    content = "\n".join(["# Real heading", "```", "# Inside unclosed fence"])

    assert _markdown_headings(content) == [(1, "Real heading")]


def test_canonical_toc_model_omits_hero_and_preserves_skipped_levels_and_ids() -> None:
    payload = {
        "metadata": {"toc": {"enabled": True, "depth": 4}},
        "blocks": [
            {"type": "hero", "title": "Document title"},
            {"type": "markdown", "content": "# Overview\n\nBody\n\n### Edge cases\n\nMore"},
            {
                "type": "section",
                "title": "Implementation",
                "children": [
                    {
                        "type": "section",
                        "title": "Validation",
                        "children": [{"type": "section", "title": "Mobile"}],
                    }
                ],
            },
            {"type": "section", "title": "Overview"},
        ],
    }
    context = PublicationContext(payload)

    assert [(entry.anchor, entry.title, entry.level) for entry in context.headings] == [
        ("overview", "Overview", 2),
        ("edge-cases", "Edge cases", 4),
        ("implementation", "Implementation", 2),
        ("validation", "Validation", 3),
        ("mobile", "Mobile", 4),
        ("overview-2", "Overview", 2),
    ]

    rendered = render_standalone_html(_row(title="Document title", rich_payload=payload))
    soup = BeautifulSoup(rendered, "html.parser")
    toc_labels = [link.get_text(strip=True) for link in soup.select(".document-toc a")]
    assert toc_labels == [
        "Overview",
        "Edge cases",
        "Implementation",
        "Validation",
        "Mobile",
        "Overview",
    ]
    assert len(soup.select("h1")) == 1
    assert soup.select_one("h1").get_text(strip=True) == "Document title"
    assert soup.select(".document-toc [class*=badge], .document-toc [class*=card]") == []


def test_standalone_uses_one_document_identity_for_hero_metadata_and_fallback_titles() -> None:
    hero = render_standalone_html(
        _row(
            title="Morning Pulse",
            rich_payload={
                "metadata": {"title": "Morning pulse", "subtitle": "Metadata subtitle"},
                "blocks": [{"type": "hero", "title": "Morning Pulse", "subtitle": "Hero subtitle"}],
            },
        )
    )
    metadata = render_standalone_html(
        _row(
            title="Export record",
            rich_payload={
                "metadata": {"title": "Editorial brief", "subtitle": "Distinct metadata"},
                "blocks": [{"type": "card", "title": "Finding", "content": "Body"}],
            },
        )
    )
    fallback = render_standalone_html(
        _row(title="Actual deliverable title", rich_payload={"blocks": [{"type": "card"}]})
    )

    assert (
        BeautifulSoup(hero, "html.parser").select_one("h1").get_text(strip=True) == "Morning Pulse"
    )
    assert len(BeautifulSoup(hero, "html.parser").select("h1")) == 1
    assert "Metadata subtitle" not in hero
    assert "Hero subtitle" in hero
    assert (
        BeautifulSoup(metadata, "html.parser").select_one("h1").get_text(strip=True)
        == "Editorial brief"
    )
    assert "Distinct metadata" in metadata
    assert BeautifulSoup(fallback, "html.parser").select_one("h1").get_text(strip=True) == (
        "Actual deliverable title"
    )


@pytest.mark.parametrize("alias", ["title", "label", "name"])
def test_standalone_hero_title_alias_is_the_single_document_identity(alias: str) -> None:
    rendered = render_standalone_html(
        _row(title="Fallback", rich_payload={"blocks": [{"type": "hero", alias: "Identity"}]})
    )
    headings = BeautifulSoup(rendered, "html.parser").select("h1")
    assert len(headings) == 1
    assert headings[0].get_text(strip=True) == "Identity"


def test_standalone_defaults_to_dark_and_responsive_toc_is_single_instance() -> None:
    rendered = render_standalone_html(
        _row(
            rich_payload={
                "metadata": {"toc": True},
                "blocks": [
                    {"type": "section", "title": f"Section {index}", "content": "Body"}
                    for index in range(4)
                ],
            }
        ),
        download_pdf_url="/api/v1/deliverables/dlv_render/download.pdf",
    )
    soup = BeautifulSoup(rendered, "html.parser")

    assert len(soup.select(".document-toc")) == 1
    # There is no per-deliverable theme toggle: deliverables default to
    # dark regardless of OS preference until Cognis ships app-wide
    # theming (see _STANDALONE_THEME_BOOTSTRAP).
    assert len(soup.select("[data-theme-toggle]")) == 0
    assert len(soup.select("[data-download-pdf]")) == 1
    assert ".document-layout" in rendered
    assert len(soup.select('[data-toc-toggle][aria-controls="document-toc"]')) == 1
    assert len(soup.select("[data-toc-backdrop]")) == 1
    assert "@media print" in rendered
    bootstrap = soup.select_one('script[data-cognis-runtime="theme-bootstrap"]').get_text()
    assert "root.dataset.resolvedTheme = 'dark'" in bootstrap
    assert "root.style.colorScheme = 'dark'" in bootstrap
    assert "matchMedia" not in bootstrap
    interactions = soup.select_one('script[data-cognis-runtime="interactions"]').get_text()
    assert "cognis-deliverable-theme" not in interactions
    assert "target.scrollIntoView" in interactions
    assert "target.focus({ preventScroll: true })" in interactions
    assert "tocClose?.focus({ preventScroll: true })" in interactions
    assert "tocRestoreFocus.focus({ preventScroll: true })" in interactions
    assert 'tabindex="-1"' in rendered
    assert "response.headers.get('Content-Disposition')" in interactions
    assert "filename\\*" in interactions
    assert "sanitizeFilename(document.title)" in interactions


def test_standalone_generated_css_uses_semantic_colors_across_theme_sensitive_content() -> None:
    rendered = render_standalone_html(
        _row(
            rich_payload={
                "metadata": {"toc": True, "presentation": "pulse"},
                "blocks": [
                    {"type": "hero", "title": "Pulse", "eyebrow": "Morning", "badges": ["Live"]},
                    {"type": "quote", "title": "Quote", "content": "Readable evidence"},
                    {"type": "metric", "title": "Metric", "value": "42", "description": "Context"},
                    {
                        "type": "day_agenda",
                        "title": "Agenda",
                        "timezone": "UTC",
                        "now": "2026-07-12T07:10:00Z",
                        "items": [{"title": "Review", "start": "2026-07-12T08:00:00Z"}],
                        "source": {"title": "Calendar", "url": "https://example.test/calendar"},
                    },
                    {
                        "type": "chart",
                        "title": "Chart",
                        "description": "Trend",
                        "source": "Dataset",
                        "rows": [{"label": "Current", "value": 42}],
                    },
                    {"type": "code", "title": "Code", "content": "print('dark')"},
                    {"type": "source_list", "title": "Sources"},
                ],
                "sources": [{"title": "Source", "url": "https://example.test/source"}],
            }
        )
    )
    stylesheet = BeautifulSoup(rendered, "html.parser").select_one("style").get_text()

    for variable in (
        "--eyebrow",
        "--label",
        "--quote",
        "--subtle",
        "--muted",
        "--row-line",
        "--active-bg",
        "--code-text",
        "--focus",
    ):
        assert f"var({variable})" in stylesheet
    for selector in (
        ".eyebrow",
        ".badges span",
        ".block-quote",
        ".metric-label",
        ".dashboard-meta",
        ".agenda-time time",
        ".block-day-agenda footer",
        ".block-figure figcaption",
        ".source-list",
        "table",
        "pre",
        "a:hover",
        "a:active",
        "a:focus-visible",
        ".document-toc a:hover",
        ".action.error",
    ):
        assert selector in stylesheet
    screen_css = stylesheet.split("@page", 1)[0]
    for obsolete_color in (
        "#47647d",
        "#405162",
        "#344657",
        "#667585",
        "#526170",
        "#687787",
        "#174f7a",
        "#ecebe6",
    ):
        assert screen_css.count(obsolete_color) <= 1


@pytest.mark.asyncio
async def test_managed_deliverable_has_no_owner_global_browser_or_public_fallback(
    task_continuation_db: object,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tests.unit.test_artifact_virtual_deliverable_refs import _seed_managed_deliverables

    await _seed_managed_deliverables(task_continuation_db)
    share = deliverable_links.signed_deliverable_view_link(
        task_continuation_db.artifact_store,
        "dlv_child",
        base_url="http://testserver",
        ttl_seconds=60,
    )
    token = _share_token(share.url)

    with _client(task_continuation_db, monkeypatch, email="owner@example.com") as client:
        detail = client.get("/api/v1/deliverables/dlv_child")
        view = client.get("/api/v1/deliverables/dlv_child/view")
        pdf = client.get("/api/v1/deliverables/dlv_child/download.pdf")
        share_link = client.post("/api/v1/deliverables/dlv_child/share-link")
        public_view = client.get(f"/api/v1/deliverables/share/{token}/view")
        public_pdf = client.get(f"/api/v1/deliverables/share/{token}/download.pdf")

    assert detail.status_code == 404
    assert view.status_code == 404
    assert pdf.status_code == 404
    assert share_link.status_code == 404
    assert public_view.status_code == 404
    assert public_pdf.status_code == 404


def test_day_agenda_renders_canonical_source_and_sanitizes_url_with_compatibility_fallback() -> (
    None
):
    canonical = render_standalone_html(
        _row(
            rich_payload={
                "blocks": [
                    {
                        "type": "day_agenda",
                        "freshness": "legacy must not leak",
                        "source": {
                            "label": "Google Calendar",
                            "url": "javascript:alert(1)",
                            "refreshed_at": "07:10 CEST",
                        },
                    }
                ]
            }
        )
    )
    fallback = render_standalone_html(
        _row(rich_payload={"blocks": [{"type": "day_agenda", "freshness": "07:05 CEST"}]})
    )

    footer = BeautifulSoup(canonical, "html.parser").select_one(".block-day-agenda footer")
    assert footer.get_text(" ", strip=True) == "Google Calendar · updated 07:10 CEST"
    assert footer.select_one("a") is None
    assert "legacy must not leak" not in canonical
    assert "Calendar and tasks · updated 07:05 CEST" in fallback


def test_pulse_presentation_suppresses_toc_and_numbering_with_editorial_chrome() -> None:
    blocks = [
        {"type": "hero", "title": "Ranní pulse", "subtitle": "Static fixture"},
        *[
            {"type": "section", "title": f"News {index}", "content": "Brief. " * 150}
            for index in range(6)
        ],
        {
            "type": "day_agenda",
            "title": "Sunday",
            "now": "2026-07-12T07:10:00+02:00",
            "timezone": "Europe/Prague",
            "freshness": "07:08 CEST",
            "items": [
                {"all_day": True, "title": "Family day"},
                {
                    "start": "2026-07-12T09:30:00+02:00",
                    "end": "2026-07-12T10:00:00+02:00",
                    "title": "Coordination",
                    "next": True,
                },
            ],
            "tasks": [{"title": "Confirm priority"}],
        },
        {
            "type": "figure",
            "caption": "Lovosice morning window.",
            "alt": "Lovosice horizon",
            "source": "Fixture source",
            "source_url": "https://example.org/source",
            "timestamp": "07:00 CEST",
        },
        {
            "type": "chart",
            "title": "Markets",
            "description": "Five-day direction.",
            "data": [{"day": "Mon", "value": 100}, {"day": "Tue", "value": 101}],
            "source": "Fixture market source",
            "source_url": "https://example.org/markets",
            "timestamp": "Friday close",
        },
    ]
    rendered = render_standalone_html(
        _row(
            title="Outer title",
            rich_payload={
                "metadata": {
                    "presentation": "pulse",
                    "toc": True,
                    "publication": True,
                    "number_figures": True,
                    "number_tables": True,
                },
                "blocks": blocks,
            },
        )
    )

    assert '<body class="presentation-pulse" data-rich-density="airy">' in rendered
    assert rendered.count("<h1>") == 1
    assert "<h1>Ranní pulse</h1>" in rendered
    assert 'aria-label="Table of contents"' not in rendered
    assert "Figure 1." not in rendered
    assert "Table 1." not in rendered
    assert "Lovosice morning window." in rendered
    assert "Fixture source" in rendered
    assert 'class="block block-day-agenda"' in rendered
    assert 'datetime="2026-07-12T07:10:00+02:00"' in rendered
    assert "Family day" in rendered
    assert "Coordination" in rendered
    assert "Confirm priority" in rendered
    assert "Five-day direction." in rendered
    assert "Fixture market source" in rendered


def test_day_agenda_renderer_orders_events_and_places_current_marker_chronologically() -> None:
    rendered = render_standalone_html(
        _row(
            rich_payload={
                "blocks": [
                    {
                        "type": "day_agenda",
                        "title": "DST day",
                        "timezone": "Europe/Prague",
                        "now": "2026-10-25T02:15:00+02:00",
                        "items": [
                            None,
                            "bad",
                            {
                                "title": "Later",
                                "start": "2026-10-25T03:00:00+01:00",
                            },
                            {
                                "title": "Current overlap",
                                "start": "2026-10-25T02:00:00+02:00",
                                "end": "2026-10-25T02:30:00+02:00",
                            },
                            {
                                "title": "Past",
                                "start": "2026-10-24T23:30:00+02:00",
                                "end": "2026-10-25T00:30:00+02:00",
                            },
                            {"title": "All day", "all_day": True},
                            {"title": "Invalid", "start": "09:00"},
                        ],
                    }
                ]
            }
        )
    )
    marker = '<li class="agenda-current-marker"'
    assert rendered.index("All day") < rendered.index("Past")
    assert rendered.index("Past") < rendered.index("Current overlap")
    assert rendered.index("Current overlap") < rendered.index(marker)
    assert rendered.index(marker) < rendered.index("Later")
    assert 'class="next current"' in rendered
    assert "Invalid" not in rendered


def test_day_agenda_renderer_converts_utc_to_prague_and_handles_cross_midnight_dst() -> None:
    rendered = render_standalone_html(
        _row(
            rich_payload={
                "blocks": [
                    {
                        "type": "day_agenda",
                        "timezone": "Europe/Prague",
                        "now": "2026-03-29T00:45:00Z",
                        "items": [
                            {
                                "title": "After jump",
                                "start": "2026-03-29T01:15:00Z",
                            },
                            {
                                "title": "Cross midnight",
                                "start": "2026-03-28T23:30:00Z",
                                "end": "2026-03-29T01:30:00Z",
                            },
                        ],
                    }
                ]
            }
        )
    )
    assert rendered.index("Cross midnight") < rendered.index("After jump")
    soup = BeautifulSoup(rendered, "html.parser")
    cross_midnight = next(
        item for item in soup.select(".agenda-timeline > li") if "Cross midnight" in item.get_text()
    )
    assert [time.get_text(strip=True) for time in cross_midnight.select("time")] == [
        "00:30",
        "03:30",
    ]
    assert [time["datetime"] for time in cross_midnight.select("time")] == [
        "2026-03-29T00:30:00+01:00",
        "2026-03-29T03:30:00+02:00",
    ]
    assert 'datetime="2026-03-29T01:45:00+01:00">01:45</time>' in rendered
    assert 'datetime="2026-03-29T03:15:00+02:00">03:15</time>' in rendered
    assert 'class="next current"' in rendered


def test_day_agenda_without_valid_now_makes_no_relative_claims() -> None:
    rendered = render_standalone_html(
        _row(
            rich_payload={
                "blocks": [
                    {
                        "type": "day_agenda",
                        "now": "not-an-instant",
                        "items": [{"title": "Event", "start": "2026-07-12T09:00:00Z"}],
                    }
                ]
            }
        )
    )
    soup = BeautifulSoup(rendered, "html.parser")
    agenda = soup.select_one(".block-day-agenda")
    assert agenda is not None
    assert agenda.select_one(".agenda-now") is None
    assert agenda.select_one(".agenda-current-marker") is None
    assert agenda.select_one(".next") is None
    assert agenda.select_one(".current") is None
    assert "Next" not in agenda.get_text(" ", strip=True)
    assert "Current" not in agenda.get_text(" ", strip=True)


def test_day_agenda_renderer_uses_absolute_instants_across_repeated_hour() -> None:
    rendered = render_standalone_html(
        _row(
            rich_payload={
                "blocks": [
                    {
                        "type": "day_agenda",
                        "timezone": "Europe/Prague",
                        "now": "2026-10-25T02:15:00+01:00",
                        "items": [
                            {
                                "title": "First fold",
                                "start": "2026-10-25T02:30:00+02:00",
                                "end": "2026-10-25T02:45:00+02:00",
                            },
                            {
                                "title": "Second fold current",
                                "start": "2026-10-25T02:00:00+01:00",
                                "end": "2026-10-25T02:30:00+01:00",
                            },
                        ],
                    }
                ]
            }
        )
    )
    assert rendered.index("First fold") < rendered.index("Second fold current")
    assert 'class="next current"' in rendered
    assert rendered.index("First fold") < rendered.index('<li class="agenda-current-marker"')


@pytest.mark.parametrize(
    "timezone",
    ["/usr/share/zoneinfo/UTC", "../UTC", "Europe\x00/Prague", r"Europe\Prague", "Unknown/Zone"],
)
def test_day_agenda_renderer_safely_falls_back_for_invalid_timezone(timezone: str) -> None:
    rendered = render_standalone_html(
        _row(
            rich_payload={
                "blocks": [
                    {
                        "type": "day_agenda",
                        "timezone": timezone,
                        "now": "2026-07-12T09:00:00Z",
                        "items": [],
                    }
                ]
            }
        )
    )
    assert "<small>UTC</small>" in rendered


def test_day_agenda_renderer_omits_marker_and_keeps_empty_message_with_zero_items() -> None:
    # `marker_index == len(timed_items)` is also true (0 == 0) when there
    # are no timed items at all. Without an explicit `timed_items` guard,
    # this rendered a second, redundant "now" line under the header's own
    # current-time display, and (since the marker made `timed` non-empty)
    # silently hid the "no events" message that should show instead.
    rendered = render_standalone_html(
        _row(
            rich_payload={
                "blocks": [
                    {
                        "type": "day_agenda",
                        "timezone": "Europe/Prague",
                        "now": "2026-07-17T08:00:00+02:00",
                        "items": [],
                    }
                ]
            }
        )
    )
    assert rendered.count('<time datetime="2026-07-17T08:00:00+02:00">') == 1
    assert '<li class="agenda-current-marker"' not in rendered
    assert "Nothing is scheduled today." in rendered


def test_day_agenda_renderer_canonical_presence_suppresses_aliases() -> None:
    rendered = render_standalone_html(
        _row(
            rich_payload={
                "blocks": [
                    {
                        "type": "day_agenda",
                        "now": None,
                        "now_iso": "2026-07-12T09:00:00Z",
                        "items": None,
                        "events": [{"title": "Hidden event", "all_day": True}],
                    }
                ]
            }
        )
    )
    assert "Hidden event" not in rendered
    assert BeautifulSoup(rendered, "html.parser").select_one(".agenda-now") is None


def test_standalone_figure_omits_external_image_but_keeps_link_caption_and_timestamp() -> None:
    rendered = render_standalone_html(
        _row(
            rich_payload={
                "blocks": [
                    {
                        "type": "figure",
                        "src": "https://example.org/external.png",
                        "caption": "Remote figure caption.",
                        "timestamp": "07:00 CEST",
                    }
                ]
            }
        )
    )
    assert '<img src="https://example.org/external.png"' not in rendered
    assert '<a href="https://example.org/external.png">Open figure</a>' in rendered
    assert "Remote figure caption." in rendered
    assert "07:00 CEST" in rendered
    assert "Source: " not in rendered
    assert "Updated: 07:00 CEST" in rendered


@pytest.mark.asyncio
async def test_pulse_pdf_renders_agenda_chart_and_safe_figure_parity() -> None:
    rendered = render_standalone_html(
        _row(
            rich_payload={
                "metadata": {"presentation": "pulse"},
                "blocks": [
                    {"type": "hero", "title": "Pulse PDF parity"},
                    {
                        "type": "day_agenda",
                        "title": "Agenda",
                        "timezone": "Europe/Prague",
                        "now": "2026-07-12T07:15:00Z",
                        "items": [
                            {"title": "Later", "start": "2026-07-12T08:30:00Z"},
                            {
                                "title": "Current meeting",
                                "start": "2026-07-12T07:00:00Z",
                                "end": "2026-07-12T07:30:00Z",
                            },
                            {
                                "title": "Past meeting",
                                "start": "2026-07-12T06:00:00Z",
                                "end": "2026-07-12T06:30:00Z",
                            },
                        ],
                    },
                    {
                        "type": "chart",
                        "title": "Unavailable chart",
                        "description": "Fallback chart description.",
                        "timestamp": "09:00 CEST",
                    },
                    {
                        "type": "figure",
                        "src": "https://example.org/remote.png",
                        "caption": "Remote provenance figure.",
                        "timestamp": "09:05 CEST",
                    },
                ],
            }
        )
    )
    assert (
        rendered.index("Past meeting") < rendered.index("Current meeting") < rendered.index("Later")
    )
    assert 'datetime="2026-07-12T09:15:00+02:00">09:15</time>' in rendered
    assert 'class="next current"' in rendered
    assert ">Current<" in rendered
    assert ">Next<" not in rendered
    assert "Chart data is unavailable" in rendered
    assert "Updated: 09:05 CEST" in rendered
    assert 'src="https://example.org/remote.png"' not in rendered

    pdf = await render_pdf_bytes(rendered)
    reader = PdfReader(BytesIO(pdf.content))
    text = "\n".join(page.extract_text() or "" for page in reader.pages)
    for expected in (
        "Pulse",
        "PDF",
        "parity",
        "Past meeting",
        "Current meeting",
        "Later",
        "09:15",
        "Current",
        "Chart data is unavailable",
        "Fallback chart description.",
        "Remote provenance figure.",
        "Updated: 09:05 CEST",
    ):
        assert expected in text


def test_publication_contract_supports_depth_only_toc_markdown_and_granular_numbering() -> None:
    blocks = [
        {"type": "markdown", "content": "## Markdown overview\n\nBody"},
        {
            "type": "section",
            "title": "Parent",
            "children": [{"type": "section", "title": "Nested"}],
        },
        {"type": "figure", "caption": "Not numbered"},
        {"type": "table", "caption": "Numbered", "rows": [{"value": 1}]},
    ]
    rendered = render_standalone_html(
        _row(
            rich_payload={
                "metadata": {
                    "toc": {"depth": 3},
                    "show_toc": True,
                    "publication": True,
                    "number_figures": False,
                    "number_tables": True,
                },
                "blocks": blocks,
            }
        )
    )

    assert 'href="#markdown-overview"' in rendered
    assert '<h2 id="markdown-overview" tabindex="-1">Markdown overview</h2>' in rendered
    assert '<h2 id="parent" tabindex="-1">Parent</h2>' in rendered
    assert '<h3 id="nested" tabindex="-1">Nested</h3>' in rendered
    assert "Figure 1." not in rendered
    assert "<strong>Table 1. </strong>Numbered" in rendered


def test_short_report_does_not_get_automatic_toc() -> None:
    rendered = render_standalone_html(
        _row(
            rich_payload={
                "blocks": [
                    {"type": "section", "title": "Summary"},
                    {"type": "section", "title": "Decision"},
                ]
            }
        )
    )
    assert 'aria-label="Table of contents"' not in rendered


def test_many_short_headings_omit_toc_but_substantial_report_gets_one() -> None:
    brief_blocks = [
        {"type": "markdown", "title": f"Brief {index}", "content": "One short update."}
        for index in range(12)
    ]
    long_blocks = [
        {
            "type": "markdown",
            "title": f"Chapter {index}",
            "content": (f"Detailed evidence for chapter {index}. " * 24),
        }
        for index in range(10)
    ]

    brief = render_standalone_html(_row(rich_payload={"blocks": brief_blocks}))
    report = render_standalone_html(_row(rich_payload={"blocks": long_blocks}))
    explicit = render_standalone_html(
        _row(rich_payload={"metadata": {"toc": True}, "blocks": brief_blocks})
    )

    assert 'class="document-toc"' not in brief
    assert brief.count("<h2") == 12
    assert report.count('class="document-toc"') == 1
    assert explicit.count('class="document-toc"') == 1


def test_publication_toc_excludes_item_backed_content_and_normalizes_markdown_levels() -> None:
    rendered = render_standalone_html(
        _row(
            rich_payload={
                "metadata": {"toc": {"enabled": True, "depth": 3}},
                "blocks": [
                    {
                        "type": "tabs",
                        "title": "Tabbed analysis",
                        "items": [{"type": "section", "title": "Synthetic tab item"}],
                    },
                    {
                        "type": "accordion",
                        "title": "Questions",
                        "items": [{"type": "section", "title": "Collapsed answer"}],
                    },
                    {
                        "type": "gallery",
                        "title": "Figures",
                        "items": [{"type": "figure", "title": "Gallery image"}],
                    },
                    {"type": "markdown", "content": "# Primary Markdown\n\nBody"},
                    {"type": "markdown", "content": "### Tertiary Markdown\n\nBody"},
                    {
                        "type": "markdown",
                        "title": "Summary",
                        "content": "Paragraph only",
                    },
                    {
                        "type": "section",
                        "title": "Canonical parent",
                        "children": [{"type": "markdown", "content": "# Nested Markdown\n\nBody"}],
                    },
                ],
            }
        )
    )

    soup = BeautifulSoup(rendered, "html.parser")
    toc = soup.select_one(".document-toc")
    assert toc is not None
    toc_text = toc.get_text(" ", strip=True)
    assert "Tabbed analysis" in toc_text
    assert "Questions" in toc_text
    assert "Figures" in toc_text
    assert "Synthetic tab item" not in toc_text
    assert "Collapsed answer" not in toc_text
    assert "Gallery image" not in toc_text
    assert soup.select_one("h2#primary-markdown").get_text(strip=True) == "Primary Markdown"
    assert soup.select_one("h2#tertiary-markdown").get_text(strip=True) == "Tertiary Markdown"
    assert soup.select_one("h3#nested-markdown").get_text(strip=True) == "Nested Markdown"
    summary = soup.select_one("section.block-markdown > h2#summary")
    assert summary is not None
    assert summary.get_text(strip=True) == "Summary"
    assert len(soup.select("#summary")) == 1


@pytest.mark.asyncio
async def test_publication_allocates_one_global_id_namespace_and_titled_markdown_hierarchy() -> (
    None
):
    rendered = render_standalone_html(
        _row(
            rich_payload={
                "metadata": {"toc": {"enabled": True, "depth": 3}},
                "sources": [{"id": "source", "title": "Source"}],
                "blocks": [
                    {"type": "section", "id": "rich-section-0", "title": "Legacy collision"},
                    {"type": "section", "id": "reference-1", "title": "Reference collision"},
                    {
                        "type": "section",
                        "id": "references-heading",
                        "title": "Bibliography collision",
                    },
                    {"type": "section", "id": "cite-1-1", "title": "Citation collision"},
                    {"type": "section", "id": "citation-1", "title": "Citation namespace"},
                    {"type": "section", "id": "toc", "title": "TOC collision"},
                    {"type": "figure", "id": "figure-1", "title": "Figure collision"},
                    {"type": "table", "id": "table-1", "title": "Table collision"},
                    {"type": "mermaid", "id": "mermaid-0", "title": "Mermaid collision"},
                    {"type": "section", "id": "duplicate", "title": "Duplicate one"},
                    {"type": "section", "id": "duplicate", "title": "Duplicate two"},
                    {
                        "type": "markdown",
                        "id": "summary",
                        "title": "Summary",
                        "content": "# Content heading\n\nParagraph\n\n### Detail heading\n\nMore.",
                    },
                    {
                        "type": "research_answer",
                        "title": "Evidence",
                        "paragraphs": [{"text": "Claim", "citations": ["source"]}],
                    },
                ],
            }
        )
    )
    soup = BeautifulSoup(rendered, "html.parser")
    ids = [str(tag["id"]) for tag in soup.select("[id]")]

    assert len(ids) == len(set(ids))
    assert {
        "section-rich-section-0",
        "section-reference-1",
        "section-references-heading",
        "section-cite-1-1",
        "section-citation-1",
        "section-toc",
        "section-figure-1",
        "section-table-1",
        "section-mermaid-0",
        "duplicate",
        "duplicate-2",
    } <= set(ids)
    assert soup.select_one("h2#summary").get_text(strip=True) == "Summary"
    assert soup.select_one("h3#content-heading").get_text(strip=True) == "Content heading"
    assert soup.select_one("h4#detail-heading").get_text(strip=True) == "Detail heading"
    assert soup.select_one('a.citation[href="#reference-1"]') is not None
    assert soup.select_one('#reference-1 a[href="#cite-1-1"]') is not None
    for link in soup.select('.document-toc a[href^="#"], .citation[href^="#"], .citation-backref'):
        assert soup.select_one(link["href"]) is not None

    reader = PdfReader(BytesIO((await render_pdf_bytes(rendered)).content))
    outline_titles: list[str] = []

    def collect_titles(items: list[object]) -> None:
        for item in items:
            if isinstance(item, list):
                collect_titles(item)
            elif isinstance(item, dict) and item.get("/Title"):
                outline_titles.append(str(item["/Title"]))

    collect_titles(reader.outline)
    assert "Summary" in outline_titles
    assert "Content heading" in outline_titles
    assert "Detail heading" in outline_titles


def test_ieee_citations_are_first_use_ordered_deduplicated_and_sanitized() -> None:
    rendered = render_standalone_html(
        _row(
            rich_payload={
                "metadata": {"toc": False},
                "sources": [
                    {
                        "id": "b",
                        "authors": ["B. Author"],
                        "title": "Second in payload, first cited",
                        "publication": "Journal",
                        "year": 2025,
                        "doi": "10.1000/test",
                        "accessed": "2026-07-11",
                    },
                    {"id": "a", "title": "Fallback metadata", "url": "javascript:alert(1)"},
                    {"id": "b", "title": "Duplicate"},
                ],
                "blocks": [
                    {
                        "type": "research_answer",
                        "title": "Findings",
                        "paragraphs": [
                            {"text": "First.", "citations": ["b", "a"]},
                            {"text": "Again.", "citations": ["b"]},
                        ],
                    }
                ],
            }
        )
    )

    assert 'href="#reference-1"' in rendered
    assert 'href="#reference-2"' in rendered
    assert rendered.count('id="reference-1"') == 1
    assert rendered.count('id="reference-2"') == 1
    assert rendered.index("Second in payload, first cited") < rendered.index("Fallback metadata")
    assert "B. Author" in rendered
    assert "Journal" in rendered
    assert "2025" in rendered
    assert "Accessed: 2026-07-11" in rendered
    assert "https://doi.org/10.1000/test" in rendered
    assert "javascript:" not in rendered
    assert 'class="citation-backref"' in rendered
    soup = BeautifulSoup(rendered, "html.parser")
    paragraphs = soup.select(".block-research_answer > p")
    assert paragraphs[0].contents[-1].get("class") == ["citation-links"]
    assert paragraphs[1].contents[-1].get("class") == ["citation-links"]
    backrefs = soup.select("#reference-1 .citation-backrefs .citation-backref")
    assert [link.get_text(strip=True) for link in backrefs] == ["1", "2"]
    assert [link["aria-label"] for link in backrefs] == [
        "Return to citation 1",
        "Return to citation 2",
    ]
    assert "↩" in soup.select_one("#reference-1 .citation-backrefs").get_text()


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("authors", ['A. Author</span><script id="authors-injection">x()</script>']),
        ("author", 'A. Author</span><img src=x onerror="author-injection">'),
        ("title", 'Title</span><script id="title-injection">x()</script>'),
        ("publication", 'Journal</em><script id="publication-injection">x()</script>'),
        ("publisher", 'Publisher</em><img src=x onerror="publisher-injection">'),
        ("site", 'Site</em><script id="site-injection">x()</script>'),
        ("year", '2026</span><script id="year-injection">x()</script>'),
        ("date", '2026</span><img src=x onerror="date-injection">'),
        ("published_at", '2026</span><script id="published-injection">x()</script>'),
        ("accessed", 'today</span><script id="accessed-injection">x()</script>'),
        ("accessed_at", 'today</span><img src=x onerror="accessed-at-injection">'),
        ("doi", '10.1/test" onclick="doi-injection'),
        ("url", 'https://example.test/" onclick="url-injection'),
    ],
)
def test_bibliography_escapes_every_metadata_field(field: str, value: object) -> None:
    source = {"id": "source", "title": "Safe title", field: value}
    rendered = render_standalone_html(
        _row(
            rich_payload={
                "sources": [source],
                "blocks": [
                    {
                        "type": "research_answer",
                        "paragraphs": [{"text": "Claim", "citations": "source"}],
                    }
                ],
            }
        )
    )

    parsed = BeautifulSoup(rendered, "html.parser")
    assert parsed.select("script:not([data-cognis-runtime])") == []
    assert parsed.find("img") is None
    assert not any(
        tag.has_attr("onerror") or tag.has_attr("onclick") for tag in parsed.find_all(True)
    )


def test_citations_are_document_wide_accept_aliases_and_dedupe_each_group() -> None:
    rendered = render_standalone_html(
        _row(
            rich_payload={
                "sources": [
                    {"id": "a", "title": "A"},
                    {"citation_id": "b", "title": "B"},
                ],
                "blocks": [
                    {
                        "type": "research_answer",
                        "title": "First",
                        "paragraphs": [{"text": "One", "citations": ["b", "b", "a"]}],
                    },
                    {
                        "type": "research_answer",
                        "title": "Second",
                        "paragraphs": [
                            {"text": "Two", "source_ids": "a"},
                            {
                                "text": "Inline",
                                "sources": {"id": "c", "title": "C"},
                            },
                        ],
                    },
                ],
            }
        )
    )

    assert rendered.count('href="#reference-1"') == 1
    assert rendered.count('href="#reference-2"') == 2
    assert rendered.count('href="#reference-3"') == 1
    assert rendered.index('id="reference-1"') < rendered.index('id="reference-2"')
    assert rendered.count('id="reference-1"') == 1
    assert rendered.count('id="reference-2"') == 1
    assert rendered.count('id="reference-3"') == 1


@pytest.mark.asyncio
async def test_nested_citations_follow_visible_parent_before_child_order_with_backrefs() -> None:
    rendered = render_standalone_html(
        _row(
            rich_payload={
                "sources": [
                    {"id": "parent", "title": "Parent source"},
                    {"id": "child", "title": "Child source"},
                ],
                "blocks": [
                    {
                        "type": "research_answer",
                        "title": "Parent finding",
                        "paragraphs": [{"text": "Parent claim.", "citations": ["parent"]}],
                        "children": [
                            {
                                "type": "research_answer",
                                "title": "Child finding",
                                "paragraphs": [
                                    {"text": "Child claim.", "citations": ["child", "parent"]}
                                ],
                            }
                        ],
                    }
                ],
            }
        )
    )
    soup = BeautifulSoup(rendered, "html.parser")
    paragraphs = soup.select(".block-research_answer > p")
    parent_claim = next(tag for tag in paragraphs if "Parent claim." in tag.get_text())
    child_claim = next(tag for tag in paragraphs if "Child claim." in tag.get_text())

    assert parent_claim.select_one('a[href="#reference-1"]') is not None
    assert child_claim.select_one('a[href="#reference-2"]') is not None
    assert child_claim.select_one('a[href="#reference-1"]') is not None
    assert rendered.index("Parent claim.") < rendered.index("Child claim.")
    assert rendered.index("Parent source") < rendered.index("Child source")
    assert soup.select_one('#reference-1 a[href="#cite-1-1"]') is not None
    assert soup.select_one('#reference-1 a[href="#cite-1-2"]') is not None
    assert soup.select_one('#reference-2 a[href="#cite-2-1"]') is not None

    reader = PdfReader(BytesIO((await render_pdf_bytes(rendered)).content))
    destinations = {
        destination
        for page in reader.pages
        for annotation in page.get("/Annots", [])
        if isinstance(
            (destination := annotation.get_object().get("/Dest")),
            str,
        )
    }
    assert {
        "reference-1",
        "reference-2",
        "cite-1-1",
        "cite-1-2",
        "cite-2-1",
    } <= destinations


def test_doi_normalization_is_case_insensitive_and_deduplicates_equivalents() -> None:
    rendered = render_standalone_html(
        _row(
            rich_payload={
                "sources": [
                    {"doi": "HTTPS://DOI.ORG/10.1000/Test", "title": "First"},
                    {"doi": "doi:10.1000/test", "title": "Duplicate"},
                ],
                "blocks": [
                    {
                        "type": "research_answer",
                        "paragraphs": [
                            {
                                "text": "Claim",
                                "citations": [
                                    {"doi": "DOI: 10.1000/TEST", "title": "Inline"},
                                    {"doi": "https://doi.org/10.1000/test", "title": "Again"},
                                ],
                            }
                        ],
                    }
                ],
            }
        )
    )

    assert rendered.count('id="reference-1"') == 1
    assert 'id="reference-2"' not in rendered
    assert "https://doi.org/10.1000/Test" in rendered


def test_publication_uses_locale_independent_unicode_anchor_and_identity_normalization() -> None:
    rendered = render_standalone_html(
        _row(
            rich_payload={
                "metadata": {"toc": True},
                "sources": [
                    {"id": "İD", "title": "First"},
                    {"id": "i̇d", "title": "Second"},
                ],
                "blocks": [
                    {"type": "section", "title": "İSTANBUL Résumé"},
                    {"type": "section", "title": "istanbul resume"},
                    {"type": "section", "title": "Third"},
                    {
                        "type": "research_answer",
                        "title": "Fourth",
                        "paragraphs": [{"text": "Claim.", "citations": ["İD", "i̇d"]}],
                    },
                ],
            }
        )
    )

    assert 'id="istanbul-resume"' in rendered
    assert 'id="istanbul-resume-2"' in rendered
    assert rendered.count('id="reference-1"') == 1
    assert 'id="reference-2"' not in rendered


def test_publication_numbers_figures_and_tables_only_when_enabled() -> None:
    blocks = [
        {"type": "figure", "caption": "Architecture."},
        {"type": "table", "caption": "Measurements.", "rows": [{"value": 1}]},
    ]
    numbered = render_standalone_html(
        _row(rich_payload={"metadata": {"publication": True}, "blocks": blocks})
    )
    simple = render_standalone_html(_row(rich_payload={"blocks": blocks}))

    assert "<strong>Figure 1.</strong> Architecture." in numbered
    assert "<strong>Table 1. </strong>Measurements." in numbered
    assert "Figure 1." not in simple
    assert "Table 1." not in simple


@pytest.mark.asyncio
async def test_short_bibliography_flows_with_content_unless_dedicated_page_is_requested() -> None:
    sources = [{"id": f"source-{index}", "title": f"Short source {index}"} for index in range(1, 4)]
    block = {
        "type": "research_answer",
        "title": "Finding",
        "paragraphs": [
            {"text": "Compact claim.", "citations": [source["id"] for source in sources]}
        ],
    }
    compact_html = render_standalone_html(
        _row(rich_payload={"sources": sources, "blocks": [block]})
    )
    dedicated_html = render_standalone_html(
        _row(
            rich_payload={
                "metadata": {"references": {"dedicated_page": True}},
                "sources": sources,
                "blocks": [block],
            }
        )
    )
    compact_reader = PdfReader(BytesIO((await render_pdf_bytes(compact_html)).content))
    dedicated_reader = PdfReader(BytesIO((await render_pdf_bytes(dedicated_html)).content))

    assert "bibliography bibliography-compact" in compact_html
    assert "bibliography bibliography-dedicated" in dedicated_html
    assert len(compact_reader.pages) == 1
    assert len(dedicated_reader.pages) == 2
    assert "References" in compact_reader.pages[0].extract_text()
    assert "References" in dedicated_reader.pages[1].extract_text()


@pytest.mark.asyncio
async def test_many_bibliography_backreferences_wrap_as_individual_compact_links() -> None:
    paragraphs = [
        {"text": f"Frequently cited claim {index}.", "citations": ["source"]}
        for index in range(1, 25)
    ]
    rendered = render_standalone_html(
        _row(
            rich_payload={
                "metadata": {"references": {"dedicated_page": True}},
                "sources": [{"id": "source", "title": "Frequently cited source"}],
                "blocks": [
                    {
                        "type": "research_answer",
                        "title": "Repeated evidence",
                        "paragraphs": paragraphs,
                    }
                ],
            }
        )
    )
    soup = BeautifulSoup(rendered, "html.parser")
    suffix = soup.select_one("#reference-1 .citation-backrefs")
    items = suffix.select(".citation-backref-item")
    links = suffix.select(".citation-backref")

    assert len(items) == len(links) == 24
    assert [link.get_text(strip=True) for link in links] == [str(index) for index in range(1, 25)]
    assert "flex-wrap: wrap" in rendered
    assert ".citation-backref-item { display: inline-flex; white-space: nowrap; }" in rendered

    reader = PdfReader(BytesIO((await render_pdf_bytes(rendered)).content))
    return_annotations = []
    for page in reader.pages:
        page_width = float(page.mediabox.width)
        for annotation_ref in page.get("/Annots", []):
            annotation = annotation_ref.get_object()
            destination = annotation.get("/Dest")
            if not isinstance(destination, str) or not destination.startswith("cite-1-"):
                continue
            rect = [float(value) for value in annotation["/Rect"]]
            assert 0 <= rect[0] <= rect[2] <= page_width
            return_annotations.append(destination)
    assert set(return_annotations) == {f"cite-1-{index}" for index in range(1, 25)}


@pytest.mark.asyncio
async def test_safe_inline_svg_figure_renders_with_alt_number_and_caption() -> None:
    safe_svg = (
        "data:image/svg+xml,%3Csvg xmlns=%22http://www.w3.org/2000/svg%22 "
        "viewBox=%220 0 320 100%22%3E%3Crect width=%22320%22 height=%22100%22 "
        "fill=%22%23eeeeee%22/%3E%3Ctext x=%2220%22 y=%2260%22%3ERenderer "
        "pipeline%3C/text%3E%3C/svg%3E"
    )
    rendered = render_standalone_html(
        _row(
            rich_payload={
                "metadata": {"publication": True},
                "blocks": [
                    {
                        "type": "figure",
                        "title": "Pipeline",
                        "src": safe_svg,
                        "alt": "Three-stage renderer pipeline",
                        "caption": "Canonical payload reaches chat and PDF.",
                    }
                ],
            }
        )
    )
    soup = BeautifulSoup(rendered, "html.parser")
    image = soup.select_one("figure.block-figure img")
    pdf = await render_pdf_bytes(rendered)

    assert image is not None
    assert image["src"].startswith("data:image/svg+xml,")
    assert image["alt"] == "Three-stage renderer pipeline"
    figure_caption = soup.select_one("figcaption")
    assert figure_caption is not None
    assert figure_caption.get_text() == "Figure 1. Canonical payload reaches chat and PDF."
    assert figure_caption.strong is not None
    assert figure_caption.strong.get_text() == "Figure 1."
    assert pdf.content.startswith(b"%PDF")
    assert len(pdf.content) > 3_000


def test_unsafe_inline_svg_figure_is_rejected() -> None:
    rendered = render_standalone_html(
        _row(
            rich_payload={
                "blocks": [
                    {
                        "type": "figure",
                        "src": "data:image/svg+xml,%3Csvg%3E%3Cscript%3Ealert(1)%3C/script%3E%3C/svg%3E",
                        "alt": "Unsafe",
                    }
                ]
            }
        )
    )
    assert "alert(1)" not in rendered
    assert '<img src="data:image/svg+xml' not in rendered


@pytest.mark.asyncio
async def test_publication_pdf_has_bytes_links_and_heading_bookmarks() -> None:
    rendered = render_standalone_html(
        _row(
            title="Publication",
            rich_payload={
                "metadata": {"toc": {"enabled": True, "depth": 3}},
                "sources": [{"id": "source", "title": "Source", "url": "https://example.com"}],
                "blocks": [
                    {
                        "type": "section",
                        "title": "One",
                        "blocks": [{"type": "section", "title": "Nested"}],
                    },
                    {"type": "section", "title": "Two"},
                    {"type": "section", "title": "Three"},
                    {
                        "type": "research_answer",
                        "title": "Four",
                        "paragraphs": [{"text": "Claim", "citations": ["source"]}],
                    },
                ],
            },
        )
    )
    pdf = await render_pdf_bytes(rendered)
    reader = PdfReader(BytesIO(pdf.content))

    assert pdf.content.startswith(b"%PDF")
    assert len(pdf.content) > 1_000
    assert reader.pages
    assert reader.outline[0]["/Title"] == "Publication"
    document_outline = reader.outline[1]
    assert document_outline[0]["/Title"] == "One"
    assert document_outline[1][0]["/Title"] == "Nested"
    assert any(
        item.get("/Title") == "References" for item in document_outline if isinstance(item, dict)
    )
    assert reader.get_destination_page_number(document_outline[0]) == 0
    assert reader.get_destination_page_number(document_outline[1][0]) == 0

    toc_text = reader.pages[0].extract_text()
    pdf_text = "\n".join(page.extract_text() or "" for page in reader.pages)
    assert pdf_text.count("Publication") == 1
    assert pdf_text.count("Contents") == 1
    assert "Download PDF" not in pdf_text
    assert "Close table of contents" not in pdf_text
    assert re.search(r"One\s+\.{3,}\s+1", toc_text)
    assert re.search(r"Nested\s+\.{3,}\s+1", toc_text)

    destinations: set[str] = set()
    for page in reader.pages:
        for annotation in page.get("/Annots", []):
            destination = annotation.get_object().get("/Dest")
            if isinstance(destination, str):
                destinations.add(destination)
    assert {"one", "nested", "two", "three", "four"} <= destinations
    assert {"reference-1", "cite-1-1"} <= destinations


def test_standalone_html_renders_sanitized_static_rich_payload() -> None:
    html = render_standalone_html(
        _row(
            rich_payload={
                "blocks": [
                    {"type": "card", "title": "Finding", "content": "<script>x()</script>Body"},
                    {"type": "table", "title": "Data", "rows": [{"Name": "alpha"}]},
                ]
            }
        ),
        download_pdf_url="/download.pdf",
    )

    assert '<meta name="viewport"' in html
    assert "<script>x()</script>" not in html.lower()
    assert len(BeautifulSoup(html, "html.parser").select("script[data-cognis-runtime]")) == 2
    assert "Finding" in html
    assert "Body" in html
    assert "<table" in html
    assert "Download PDF" in html
    assert "http://169.254.169.254" not in render_standalone_html(
        _row(format="markdown", content="![x](http://169.254.169.254/latest/meta-data/)")
    )


def test_standalone_html_consumes_semantic_hero_without_duplicate_title_or_schema_leak() -> None:
    rendered = render_standalone_html(
        _row(
            title="Editorial report",
            rich_payload={
                "metadata": {"subtitle": "Metadata subtitle must stay semantic"},
                "blocks": [
                    {
                        "type": "hero",
                        "eyebrow": "Research brief",
                        "title": "Editorial report",
                        "subtitle": "Evidence-led recommendation",
                        "badges": ["Reviewed"],
                    },
                    {
                        "type": "kv",
                        "title": "Scope",
                        "items": [{"label": "Audience", "value": "Engineering"}],
                    },
                ],
            },
        )
    )

    assert rendered.count("<h1>Editorial report</h1>") == 1
    assert "Evidence-led recommendation" in rendered
    assert "Metadata subtitle must stay semantic" not in rendered
    assert ">subtitle<" not in rendered
    assert "<dt>Audience</dt><dd>Engineering</dd>" in rendered
    assert "block-hero" not in rendered


def test_standalone_html_preserves_leading_hero_children_exactly_once() -> None:
    rendered = render_standalone_html(
        _row(
            title="Nested report",
            rich_payload={
                "blocks": [
                    {
                        "type": "hero",
                        "title": "Nested report",
                        "children": [
                            {
                                "type": "callout",
                                "title": "Executive finding",
                                "content": "Preserved nested evidence.",
                            }
                        ],
                    }
                ]
            },
        )
    )

    assert rendered.count("<h1>Nested report</h1>") == 1
    assert rendered.count("Executive finding") == 1
    assert rendered.count("Preserved nested evidence.") == 1


def test_standalone_html_renders_non_leading_hero_and_its_children_once() -> None:
    rendered = render_standalone_html(
        _row(
            title="Document title",
            rich_payload={
                "blocks": [
                    {"type": "markdown", "content": "Introduction"},
                    {
                        "type": "hero",
                        "title": "Chapter title",
                        "subtitle": "Chapter subtitle",
                        "blocks": [{"type": "markdown", "content": "Nested chapter content"}],
                    },
                ]
            },
        )
    )

    assert rendered.count("<h1>Document title</h1>") == 1
    assert rendered.count("Chapter title") == 1
    assert rendered.count("Chapter subtitle") == 1
    assert rendered.count("Nested chapter content") == 1


def test_standalone_html_renders_hero_media_banner() -> None:
    # Mirrors the web renderer's hero media support: a hero can carry a
    # banner image (e.g. an agent-generated cover for a published article)
    # via the same media object shape as figure/card, resolved through
    # `_render_card_media`. Remote https URLs are deliberately rejected by
    # `_safe_image_src` (standalone HTML/PDF must never embed a remote
    # image reference), so authorized media always goes through the
    # `media_resolver` callback like every other media-bearing block.
    resolved = ResolvedMedia(
        src="data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAAB",
        mime_type="image/png",
        filename="banner.png",
    )
    rendered = render_standalone_html(
        _row(
            title="Document title",
            rich_payload={
                "blocks": [
                    {"type": "markdown", "content": "Introduction"},
                    {
                        "type": "hero",
                        "title": "Chapter title",
                        "media": {"ref": "hero-banner", "alt": "Cover banner"},
                    },
                ]
            },
        ),
        media_resolver=lambda _reference, _target: resolved,
        render_target="pdf",
    )
    image = BeautifulSoup(rendered, "html.parser").select_one(".card-media img")

    assert image is not None
    assert image["src"] == resolved.src
    assert image["alt"] == "Cover banner"


def test_standalone_html_omits_hero_media_figure_without_a_media_reference() -> None:
    rendered = render_standalone_html(
        _row(
            title="Document title",
            rich_payload={
                "blocks": [
                    {"type": "markdown", "content": "Introduction"},
                    {"type": "hero", "title": "Chapter without a banner"},
                ]
            },
        )
    )

    assert BeautifulSoup(rendered, "html.parser").select_one(".card-media") is None


@pytest.mark.parametrize("tag_field", ["tags", "badges"])
def test_standalone_html_renders_non_leading_hero_metadata(tag_field: str) -> None:
    rendered = render_standalone_html(
        _row(
            title="Document title",
            rich_payload={
                "blocks": [
                    {"type": "markdown", "content": "Introduction"},
                    {
                        "type": "hero",
                        "eyebrow": "Architecture chapter",
                        "title": "Control plane",
                        "subtitle": "Policy and execution boundaries",
                        "content": "Chapter overview.",
                        tag_field: ["Governance", "Audit"],
                    },
                ]
            },
        )
    )

    assert rendered.count("Architecture chapter") == 1
    assert rendered.count("Control plane") == 1
    assert rendered.count("Policy and execution boundaries") == 1
    assert rendered.count("Chapter overview.") == 1
    assert rendered.count("Governance") == 1
    assert rendered.count("Audit") == 1


@pytest.mark.parametrize(("value", "expected"), [(42, "42"), (False, "false"), (True, "true")])
def test_standalone_html_renders_scalar_metric_values(value: object, expected: str) -> None:
    rendered = render_standalone_html(
        _row(rich_payload={"blocks": [{"type": "metric", "label": "Availability", "value": value}]})
    )

    assert f'<p class="metric-value">{expected}</p>' in rendered


def test_standalone_html_renders_metric_context() -> None:
    rendered = render_standalone_html(
        _row(
            rich_payload={
                "blocks": [
                    {
                        "type": "metric",
                        "label": "Latency",
                        "value": 4.2,
                        "unit": "s",
                        "trend": "-0.6 s",
                        "description": "Improved after queue backpressure tuning.",
                    }
                ]
            }
        )
    )

    assert '<p class="metric-value">4.2<span class="metric-unit">s</span></p>' in rendered
    assert '<p class="metric-delta">-0.6 s</p>' in rendered
    assert "Improved after queue backpressure tuning." in rendered


def test_standalone_html_metric_uses_content_fallback_and_value_precedence() -> None:
    content_only = render_standalone_html(
        _row(
            rich_payload={
                "blocks": [
                    {
                        "type": "metric",
                        "label": "Content fallback",
                        "content": "Fallback value",
                        "description": "Fallback metric context.",
                    }
                ]
            }
        )
    )
    explicit_value = render_standalone_html(
        _row(
            rich_payload={
                "blocks": [
                    {
                        "type": "metric",
                        "label": "Explicit value",
                        "value": 0,
                        "content": "Must not render",
                        "unit": "ms",
                        "trend": "stable",
                    }
                ]
            }
        )
    )

    assert '<p class="metric-value">Fallback value</p>' in content_only
    assert "Fallback metric context." in content_only
    assert '<p class="metric-value">0<span class="metric-unit">ms</span></p>' in explicit_value
    assert '<p class="metric-delta">stable</p>' in explicit_value
    assert "Must not render" not in explicit_value


@pytest.mark.parametrize(
    ("block_type", "selector"),
    [
        ("grid", ".block-grid, .block-card_grid { display: grid;"),
        ("columns", ".block-columns { display: grid;"),
        ("card_grid", ".block-grid, .block-card_grid { display: grid;"),
    ],
)
def test_standalone_html_grid_blocks_get_a_generic_display_grid_rule(
    block_type: str, selector: str
) -> None:
    """Regression test: only the pulse presentation had a `display: grid`
    CSS rule for grid/columns/card_grid blocks. A generic (non-pulse)
    multi-item grid -- e.g. a row of metrics -- rendered as a plain vertical
    stack in the static/PDF export, with no way to lay siblings out
    side by side."""

    rendered = render_standalone_html(
        _row(
            rich_payload={
                "blocks": [
                    {
                        "type": block_type,
                        "blocks": [
                            {"type": "metric", "label": "A", "value": 1},
                            {"type": "metric", "label": "B", "value": 2},
                        ],
                    }
                ]
            }
        )
    )

    assert selector in rendered
    assert f'class="block block-{block_type}"' in rendered


def test_standalone_html_grid_block_respects_explicit_column_count() -> None:
    rendered = render_standalone_html(
        _row(
            rich_payload={
                "blocks": [
                    {
                        "type": "grid",
                        "columns": 3,
                        "blocks": [
                            {"type": "metric", "label": "A", "value": 1},
                            {"type": "metric", "label": "B", "value": 2},
                        ],
                    }
                ]
            }
        )
    )

    assert 'style="grid-template-columns: repeat(3, minmax(0, 1fr))"' in rendered


def test_standalone_html_grid_block_without_explicit_columns_has_no_inline_override() -> None:
    """Without an explicit column count, no inline `grid-template-columns`
    style should be emitted on the block -- the CSS auto-fit default must be
    free to engage."""

    rendered = render_standalone_html(
        _row(
            rich_payload={
                "blocks": [
                    {
                        "type": "grid",
                        "blocks": [{"type": "metric", "label": "A", "value": 1}],
                    }
                ]
            }
        )
    )

    assert 'style="grid-template-columns' not in rendered


async def test_pdf_grid_block_with_explicit_columns_renders_without_error() -> None:
    """Regression test for a WeasyPrint incompatibility: an earlier version
    of the generic grid fix used `repeat(var(--rich-columns, auto-fit),
    minmax(...))`, which crashes WeasyPrint's grid layout engine with
    `TypeError: 'FunctionBlock' object is not subscriptable`. Explicit
    columns must use a plain inline `grid-template-columns` override
    instead. Also covers a plain grid block (auto-fit, no explicit count)
    through the real PDF pipeline."""

    for block in (
        {
            "type": "grid",
            "columns": 3,
            "blocks": [
                {"type": "metric", "label": "A", "value": 1},
                {"type": "metric", "label": "B", "value": 2},
            ],
        },
        {
            "type": "grid",
            "blocks": [
                {"type": "metric", "label": "A", "value": 1},
                {"type": "metric", "label": "B", "value": 2},
            ],
        },
    ):
        rendered = render_standalone_html(_row(rich_payload={"blocks": [block]}))
        pdf = await render_pdf_bytes(rendered)
        assert pdf.content.startswith(b"%PDF")


@pytest.mark.parametrize("data_field", ["metrics", "items", "cards"])
def test_standalone_html_renders_status_dashboard_data_and_children_once(
    data_field: str,
) -> None:
    rendered = render_standalone_html(
        _row(
            rich_payload={
                "blocks": [
                    {
                        "type": "status",
                        "eyebrow": "Operations",
                        "title": "Service health",
                        "description": "Current production posture.",
                        "status": "Healthy",
                        data_field: [
                            {
                                "label": "Availability",
                                "value": 99.96,
                                "unit": "%",
                                "delta": "+0.03 pp",
                                "status": "Good",
                                "explanation": "Above the weekly target.",
                                "drilldown": ["API: 99.98%", "Runtime: 99.94%"],
                            }
                        ],
                        "children": [
                            {
                                "type": "callout",
                                "title": "Operator note",
                                "content": "No intervention required.",
                            }
                        ],
                    }
                ]
            }
        )
    )

    assert rendered.count("Operations") == 1
    assert rendered.count("Service health") == 1
    assert rendered.count("Current production posture.") == 1
    assert rendered.count("Healthy") == 1
    assert rendered.count("Availability") == 1
    assert rendered.count("99.96") == 1
    assert rendered.count("+0.03 pp") == 1
    assert rendered.count("Good") == 1
    assert rendered.count("Above the weekly target.") == 1
    assert rendered.count("API: 99.98%") == 1
    assert rendered.count("Operator note") == 1
    assert rendered.count("No intervention required.") == 1


@pytest.mark.parametrize("field", ["quote", "content", "text", "body"])
def test_standalone_html_renders_all_quote_text_aliases(field: str) -> None:
    rendered = render_standalone_html(
        _row(rich_payload={"blocks": [{"type": "quote", field: "Canonical quotation"}]})
    )

    assert "<p>Canonical quotation</p>" in rendered


@pytest.mark.parametrize("field", ["content", "summary"])
def test_standalone_html_callout_body_accepts_summary_alias_and_never_renders_empty(
    field: str,
) -> None:
    """Regression test: callout authored with `summary` (a common LLM habit)
    instead of `content` must not silently render an empty body."""

    rendered = render_standalone_html(
        _row(
            rich_payload={
                "blocks": [
                    {"type": "callout", "title": "Important caveat", field: "Read this carefully."}
                ]
            }
        )
    )

    assert "Important caveat" in rendered
    assert "Read this carefully." in rendered


def test_standalone_html_metric_description_accepts_summary_and_dek_aliases() -> None:
    rendered = render_standalone_html(
        _row(
            rich_payload={
                "blocks": [
                    {
                        "type": "metric",
                        "label": "Error budget",
                        "value": "12%",
                        "summary": "Consumed faster than the weekly pace.",
                    }
                ]
            }
        )
    )

    assert "Consumed faster than the weekly pace." in rendered


def test_standalone_html_dashboard_item_description_accepts_summary_and_dek_aliases() -> None:
    rendered = render_standalone_html(
        _row(
            rich_payload={
                "blocks": [
                    {
                        "type": "dashboard",
                        "title": "Fleet health",
                        "metrics": [
                            {
                                "label": "Availability",
                                "value": 99.9,
                                "summary": "Stable across all regions.",
                            }
                        ],
                    }
                ]
            }
        )
    )

    assert "Stable across all regions." in rendered


def test_standalone_html_action_block_renders_as_action_card_and_does_not_fall_back() -> None:
    """Regression test: `action` is a supported standalone block type (see
    SUPPORTED_RICH_BLOCK_TYPES) but previously had no dedicated renderer and
    silently fell through to the generic markdown/key-value fallback path."""

    rendered = render_standalone_html(
        _row(
            rich_payload={
                "blocks": [
                    {
                        "type": "action",
                        "icon": "check",
                        "title": "Rotate the credential",
                        "content": "Do this before the next deploy.",
                    }
                ]
            }
        )
    )

    assert "Rotate the credential" in rendered
    assert "Do this before the next deploy." in rendered
    assert "block-card card-variant-action" in rendered
    assert "Unsupported" not in rendered


def test_standalone_html_action_block_respects_explicit_variant_override() -> None:
    rendered = render_standalone_html(
        _row(
            rich_payload={
                "blocks": [
                    {
                        "type": "action",
                        "variant": "status",
                        "title": "Follow up",
                        "content": "Track this next week.",
                    }
                ]
            }
        )
    )

    # Check the space-joined class attribute token pair, not just substring
    # presence, since the static stylesheet always embeds the dot-joined CSS
    # selector `.block-card.card-variant-action` regardless of which blocks
    # are actually used on the page.
    assert "block-card card-variant-status" in rendered
    assert "block-card card-variant-action" not in rendered


def test_standalone_html_accordion_item_summary_is_shown_once_as_inline_label() -> None:
    """Regression guard: extending the shared content fallback to include
    `summary` must not cause accordion/tabs items (which already show
    `summary`/`dek` as an inline label) to also render it a second time as
    the item body when no separate `content` is authored."""

    rendered = render_standalone_html(
        _row(
            rich_payload={
                "blocks": [
                    {
                        "type": "accordion",
                        "title": "FAQ",
                        "items": [
                            {
                                "title": "Why did this happen",
                                "summary": "Short inline summary only.",
                            }
                        ],
                    }
                ]
            }
        )
    )

    assert rendered.count("Short inline summary only.") == 1


def test_standalone_html_accordion_item_with_both_summary_and_content_shows_each_once() -> None:
    rendered = render_standalone_html(
        _row(
            rich_payload={
                "blocks": [
                    {
                        "type": "accordion",
                        "title": "FAQ",
                        "items": [
                            {
                                "title": "Why did this happen",
                                "summary": "Short inline summary.",
                                "content": "Full explanation body.",
                            }
                        ],
                    }
                ]
            }
        )
    )

    assert rendered.count("Short inline summary.") == 1
    assert rendered.count("Full explanation body.") == 1


def test_standalone_html_renders_payload_and_block_sources_with_safe_clickable_links() -> None:
    rendered = render_standalone_html(
        _row(
            rich_payload={
                "sources": [
                    {
                        "id": "payload",
                        "title": "Payload source",
                        "url": "https://example.com/report?x=1&y=2",
                        "citation": "Primary evidence",
                    },
                    {
                        "id": "unsafe",
                        "title": "<Unsafe title>",
                        "url": "javascript:alert(1)",
                    },
                ],
                "blocks": [
                    {"type": "source_list", "title": "Payload references"},
                    {
                        "type": "source_list",
                        "title": "Selected references",
                        "sources": [
                            "payload",
                            {
                                "title": "Block source",
                                "url": "https://example.org/source",
                                "snippet": "Block-level citation",
                            },
                        ],
                    },
                ],
            }
        )
    )

    assert rendered.count(">Payload source</a>") == 2
    assert 'href="https://example.com/report?x=1&amp;y=2"' in rendered
    assert "Primary evidence" in rendered
    assert 'href="https://example.org/source"' in rendered
    assert "Block-level citation" in rendered
    assert "&lt;Unsafe title&gt;" in rendered
    assert "javascript:" not in rendered


@pytest.mark.parametrize(
    ("block_type", "block_data"),
    [
        ("quote", {"quote": ""}),
        ("quote", {"quote": "Parent quotation"}),
        ("kv", {"items": [{"key": "Environment", "value": "Production"}]}),
        ("key_value", {"items": [{"key": "Environment", "value": "Production"}]}),
        ("timeline", {"items": [{"title": "Parent event"}]}),
        ("steps", {"items": [{"title": "Parent step"}]}),
        ("incident_timeline", {"items": [{"title": "Parent incident"}]}),
        ("checklist", {"items": [{"title": "Parent check"}]}),
        ("incident_checklist", {"items": [{"title": "Parent incident check"}]}),
        ("source_list", {"sources": []}),
        (
            "source_list",
            {"sources": [{"title": "Parent source", "url": "https://example.com/parent"}]},
        ),
        (
            "chart",
            {
                "labels": ["A"],
                "datasets": [{"label": "Parent series", "data": [1]}],
            },
        ),
        ("mermaid", {"content": "graph TD; A-->B"}),
        ("divider", {}),
    ],
)
def test_standalone_html_specialized_blocks_render_nested_children_exactly_once(
    block_type: str,
    block_data: dict[str, object],
) -> None:
    rendered = render_standalone_html(
        _row(
            rich_payload={
                "blocks": [
                    {
                        "type": block_type,
                        **block_data,
                        "children": [
                            {
                                "type": "callout",
                                "title": "Nested parity marker",
                                "content": "Nested parity content.",
                            }
                        ],
                    }
                ]
            }
        )
    )

    assert rendered.count("Nested parity marker") == 1
    assert rendered.count("Nested parity content.") == 1


@pytest.mark.parametrize("paragraph_field", ["paragraphs", "items"])
@pytest.mark.parametrize("points_field", ["key_points", "highlights"])
def test_standalone_html_renders_research_semantics_aliases_and_children(
    paragraph_field: str,
    points_field: str,
) -> None:
    rendered = render_standalone_html(
        _row(
            rich_payload={
                "sources": [
                    {
                        "id": "s1",
                        "title": "Safe research source",
                        "url": "https://example.com/research",
                    }
                ],
                "blocks": [
                    {
                        "type": "research_answer",
                        "title": "Research finding",
                        "description": "Evidence-led answer.",
                        paragraph_field: [
                            {
                                "text": "Supported paragraph.",
                                "source_ids": ["s1"],
                            }
                        ],
                        points_field: ["Durable key point"],
                        "children": [
                            {
                                "type": "callout",
                                "title": "Nested research note",
                                "content": "Nested research content.",
                            }
                        ],
                    }
                ],
            }
        )
    )

    for expected in (
        "Research finding",
        "Evidence-led answer.",
        "Supported paragraph.",
        "Safe research source",
        "Durable key point",
    ):
        assert expected in rendered
    assert 'href="https://example.com/research"' in rendered
    assert rendered.count("Nested research note") == 1
    assert rendered.count("Nested research content.") == 1


def test_standalone_html_renders_block_level_research_answer_citations() -> None:
    rendered = render_standalone_html(
        _row(
            rich_payload={
                "sources": [
                    {
                        "id": "s1",
                        "title": "Calendar source",
                        "url": "https://calendar.example.com/",
                    },
                    {
                        "id": "s2",
                        "title": "Task source",
                        "url": "https://tasks.example.com/",
                    },
                ],
                "blocks": [
                    {
                        "type": "research_answer",
                        "title": "Protect the morning focus",
                        "answer": "The day has no fixed commitments.",
                        "source_ids": ["s1", "s2"],
                    }
                ],
            }
        )
    )

    assert "Protect the morning focus" in rendered
    assert "The day has no fixed commitments." in rendered
    assert rendered.count('class="citation"') == 2
    assert "Calendar source" in rendered
    assert "Task source" in rendered


def test_standalone_html_does_not_cite_research_answer_source_scopes() -> None:
    rendered = render_standalone_html(
        _row(
            rich_payload={
                "sources": [
                    {"id": "scope-only", "title": "Scope only"},
                    {"id": "cited", "title": "Cited source"},
                ],
                "blocks": [
                    {
                        "type": "research_answer",
                        "sources": ["scope-only", "cited"],
                        "answer": "Only this source supports the answer.",
                        "source_ids": ["cited"],
                    }
                ],
            }
        )
    )

    assert "Cited source" in rendered
    assert "Scope only" not in rendered
    assert rendered.count('class="citation"') == 1


def test_standalone_html_renders_claim_body_when_title_is_present() -> None:
    rendered = render_standalone_html(
        _row(
            rich_payload={
                "blocks": [
                    {
                        "type": "evidence_report",
                        "claims": [
                            {
                                "title": "Pick the verified option",
                                "claim": "It has the strongest evidence for the stated requirements.",
                            }
                        ],
                    }
                ]
            }
        )
    )

    assert "Pick the verified option" in rendered
    assert "It has the strongest evidence for the stated requirements." in rendered


def test_standalone_html_renders_comparison_matrix_row_evidence_and_citations() -> None:
    rendered = render_standalone_html(
        _row(
            rich_payload={
                "sources": [
                    {
                        "id": "manufacturer",
                        "title": "Manufacturer specification",
                        "url": "https://example.org/specification",
                    }
                ],
                "blocks": [
                    {
                        "type": "comparison_matrix",
                        "columns": ["name", "price"],
                        "rows": [
                            {
                                "name": "Product A",
                                "price": "100",
                                "recommended": True,
                                "evidence": [
                                    {"title": "Price check", "text": "Lowest verified price."}
                                ],
                                "source_ids": ["manufacturer"],
                            }
                        ],
                    }
                ],
            }
        )
    )

    assert "<th>Evidence</th>" in rendered
    assert "Price check" in rendered
    assert "Lowest verified price." in rendered
    assert 'class="citation"' in rendered
    assert "Manufacturer specification" in rendered
    assert 'class="recommendation">Recommended</strong>' in rendered


@pytest.mark.parametrize("claims_field", ["claims", "items", "data"])
@pytest.mark.parametrize("block_type", ["evidence_report", "claim_cards"])
def test_standalone_html_renders_evidence_semantics_aliases_and_children(
    claims_field: str,
    block_type: str,
) -> None:
    rendered = render_standalone_html(
        _row(
            rich_payload={
                "sources": [
                    {
                        "key": "evidence-source",
                        "title": "Evidence source",
                        "url": "https://example.org/evidence",
                    }
                ],
                "blocks": [
                    {
                        "type": block_type,
                        "title": "Claims review",
                        claims_field: [
                            {
                                "category": "Verified",
                                "claim": "The control is effective.",
                                "summary": "Observed across representative runs.",
                                "score": 0.92,
                                "snippets": [
                                    {
                                        "quote": "No unauthorized calls.",
                                        "source": "Audit log",
                                    }
                                ],
                                "citations": ["evidence-source"],
                            }
                        ],
                        "caveats": ["Limited historical window."],
                        "contradictions": ["One stale test fixture."],
                        "children": [
                            {
                                "type": "callout",
                                "title": "Nested evidence note",
                                "content": "Nested evidence content.",
                            }
                        ],
                    }
                ],
            }
        )
    )

    for expected in (
        "Verified",
        "The control is effective.",
        "Observed across representative runs.",
        "92%",
        "No unauthorized calls.",
        "Audit log",
        "Evidence source",
        "Limited historical window.",
        "One stale test fixture.",
    ):
        assert expected in rendered
    assert 'href="https://example.org/evidence"' in rendered
    assert rendered.count("Nested evidence note") == 1
    assert rendered.count("Nested evidence content.") == 1


@pytest.mark.parametrize("entries_field", ["items", "entries", "timeline", "data"])
@pytest.mark.parametrize("checklist_field", ["checklist", "remediation", "actions"])
def test_standalone_html_renders_incident_aliases_metadata_and_children(
    entries_field: str,
    checklist_field: str,
) -> None:
    rendered = render_standalone_html(
        _row(
            rich_payload={
                "blocks": [
                    {
                        "type": "incident_timeline",
                        "eyebrow": "Incident 42",
                        "title": "API latency",
                        "description": "Production degradation.",
                        "severity": "P1",
                        "status": "Resolved",
                        "owner": "Runtime",
                        entries_field: [
                            {
                                "timestamp": "10:42",
                                "label": "Mitigation applied",
                                "description": "Queue limits reduced.",
                                "status": "complete",
                                "owner": "SRE",
                                "duration": "8m",
                            }
                        ],
                        checklist_field: [
                            {
                                "action": "Backfill dashboards",
                                "checked": True,
                                "owner": "Observability",
                                "status": "done",
                            }
                        ],
                        "children": [
                            {
                                "type": "callout",
                                "title": "Nested incident note",
                                "content": "Nested incident content.",
                            }
                        ],
                    }
                ]
            }
        )
    )

    for expected in (
        "Incident 42",
        "API latency",
        "Production degradation.",
        "Severity: P1",
        "Status: Resolved",
        "Owner: Runtime",
        "10:42",
        "Mitigation applied",
        "Queue limits reduced.",
        "Status: complete",
        "Owner: SRE",
        "Duration: 8m",
        "Backfill dashboards",
        "Owner: Observability",
    ):
        assert expected in rendered
    assert rendered.count("Nested incident note") == 1
    assert rendered.count("Nested incident content.") == 1


@pytest.mark.parametrize("block_type", ["tabs", "accordion"])
def test_standalone_html_renders_item_backed_disclosures_once(block_type: str) -> None:
    rendered = render_standalone_html(
        _row(
            rich_payload={
                "blocks": [
                    {
                        "type": block_type,
                        "title": "Operational details",
                        "items": [
                            {
                                "type": "section",
                                "title": "Capacity tab",
                                "content": "Capacity item content.",
                            }
                        ],
                        "children": [
                            {
                                "type": "callout",
                                "title": "Disclosure child",
                                "content": "Disclosure child content.",
                            }
                        ],
                    }
                ]
            }
        )
    )

    assert rendered.count("Capacity item content.") == 1
    assert rendered.count("<h3>Capacity tab</h3>") == (0 if block_type == "accordion" else 1)
    assert rendered.count('<h3 id="disclosure-child" tabindex="-1">Disclosure child</h3>') == 1
    assert rendered.count("<p>Disclosure child content.</p>") == 1


@pytest.mark.parametrize("block_type", ["modal", "gallery"])
def test_standalone_html_renders_static_item_containers_and_children_once(
    block_type: str,
) -> None:
    item = (
        {
            "title": "Gallery figure",
            "url": "https://example.com/figure",
            "caption": "Figure caption.",
        }
        if block_type == "gallery"
        else {
            "type": "markdown",
            "title": "Modal detail",
            "content": "Modal item content.",
        }
    )
    rendered = render_standalone_html(
        _row(
            rich_payload={
                "blocks": [
                    {
                        "type": block_type,
                        "title": "Container",
                        "items": [item],
                        "children": [
                            {
                                "type": "callout",
                                "title": "Container child",
                                "content": "Container child content.",
                            }
                        ],
                    }
                ]
            }
        )
    )

    assert rendered.count('<h3 id="container-child" tabindex="-1">Container child</h3>') == 1
    assert rendered.count("<p>Container child content.</p>") == 1
    assert "Modal item content." in rendered or "Figure caption." in rendered


def test_standalone_html_uses_mermaid_source_safely_and_renders_children_once() -> None:
    rendered = render_standalone_html(
        _row(
            rich_payload={
                "blocks": [
                    {
                        "type": "mermaid",
                        "title": "Flow",
                        "source": 'graph TD; A["<unsafe>"]-->B',
                        "children": [
                            {
                                "type": "callout",
                                "title": "Diagram note",
                                "content": "Diagram child content.",
                            }
                        ],
                    }
                ]
            }
        )
    )

    assert "A[&quot;&lt;unsafe&gt;&quot;]" in rendered
    assert "<unsafe>" not in rendered
    assert rendered.count("Diagram note") == 1
    assert rendered.count("Diagram child content.") == 1


def test_standalone_html_renders_media_link_code_and_table_aliases_safely() -> None:
    rendered = render_standalone_html(
        _row(
            rich_payload={
                "blocks": [
                    {
                        "type": "figure",
                        "title": "Architecture figure",
                        "url": "https://example.com/figure",
                        "caption": "External figure caption.",
                    },
                    {
                        "type": "link_preview",
                        "label": "Safe reference",
                        "href": "https://example.com/reference?x=1&y=2",
                        "domain": "example.com",
                        "description": "Reference context.",
                    },
                    {
                        "type": "code",
                        "title": "Escaped code",
                        "language": "html",
                        "content": "<script>alert(1)</script>",
                    },
                    {
                        "type": "table",
                        "columns": [{"id": "build_state", "title": "Build state"}],
                        "rows": [{"build_state": "passing"}],
                    },
                ]
            }
        )
    )

    assert 'href="https://example.com/figure"' in rendered
    assert "External figure caption." in rendered
    assert 'href="https://example.com/reference?x=1&amp;y=2"' in rendered
    assert "Reference context." in rendered
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in rendered
    assert "<script>alert(1)</script>" not in rendered
    assert "<th>Build state</th>" in rendered
    assert "<td>passing</td>" in rendered


def test_standalone_html_uses_column_labels_and_print_report_grammar() -> None:
    rendered = render_standalone_html(
        _row(
            rich_payload={
                "blocks": [
                    {
                        "type": "table",
                        "title": "Options",
                        "columns": [
                            {"key": "internal_name", "label": "Option"},
                            {"key": "score", "label": "Score"},
                        ],
                        "rows": [{"internal_name": "Alpha", "score": 9}],
                    }
                ]
            }
        )
    )

    assert "<th>Option</th><th>Score</th>" in rendered
    assert "@page" in rendered
    assert "counter(page)" in rendered
    assert "thead { display: table-header-group; }" in rendered
    assert ".document { padding: 0; box-shadow: none; }" in rendered


@pytest.mark.asyncio
async def test_pdf_render_smoke_and_input_cap() -> None:
    pdf = await render_pdf_bytes(render_standalone_html(_row()))
    assert pdf.content.startswith(b"%PDF")
    assert len(pdf.content) > 100

    with pytest.raises(DeliverableRenderError, match="render_input_too_large"):
        await render_pdf_bytes("x" * (20 * 1024 * 1024 + 1))


@pytest.mark.asyncio
async def test_pdf_export_is_multi_page_a4_with_repeated_headers_and_clickable_links() -> None:
    rows = [
        {"item": f"Row {index:03d}", "detail": "A deterministic long-table value"}
        for index in range(180)
    ]
    document = render_standalone_html(
        _row(
            title="Long research report",
            rich_payload={
                "blocks": [
                    {
                        "type": "hero",
                        "title": "Long research report",
                        "subtitle": "A4 pagination fixture",
                    },
                    {
                        "type": "markdown",
                        "title": "Evidence",
                        "content": "Read the [primary source](https://example.com/report).",
                    },
                    {
                        "type": "table",
                        "title": "Dataset",
                        "columns": [
                            {"key": "item", "label": "Item"},
                            {"key": "detail", "label": "Detail"},
                        ],
                        "rows": rows,
                    },
                ]
            },
        )
    )
    pdf = await render_pdf_bytes(document)
    reader = PdfReader(BytesIO(pdf.content))

    assert len(reader.pages) >= 4
    assert all(abs(float(page.mediabox.width) - 595.28) < 2 for page in reader.pages)
    assert all(abs(float(page.mediabox.height) - 841.89) < 2 for page in reader.pages)
    table_pages = [
        page.extract_text() or "" for page in reader.pages if "Row " in (page.extract_text() or "")
    ]
    assert len(table_pages) >= 2
    assert all("ITEM" in text and "DETAIL" in text for text in table_pages)
    assert any("/Annots" in page for page in reader.pages)


def test_cache_key_changes_with_version() -> None:
    assert deliverable_cache_key(_row(version=1)) != deliverable_cache_key(_row(version=2))


class _SingleFlightStore:
    def __init__(self) -> None:
        self.objects: dict[tuple[str, str, str], bytes] = {}

    async def async_exists(self, namespace: str, object_id: str, filename: str) -> bool:
        return (namespace, object_id, filename) in self.objects

    async def async_load(self, namespace: str, object_id: str, filename: str) -> tuple[bytes, str]:
        return self.objects[(namespace, object_id, filename)], "application/pdf"

    async def async_save(
        self, namespace: str, object_id: str, filename: str, content: bytes, _mime: str
    ) -> None:
        self.objects[(namespace, object_id, filename)] = content


def _single_flight_request(store: _SingleFlightStore) -> SimpleNamespace:
    return SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(artifact_store=store)))


@pytest.mark.asyncio
async def test_pdf_cache_single_flight_fans_out_one_render_and_documents_stay_independent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _SingleFlightStore()
    request = _single_flight_request(store)
    rows = [
        _row(
            deliverable_id=f"dlv_{index}",
            content_hash=f"content-{index}",
            pdf_cache_key=None,
            storage_namespace="deliverables",
            storage_object_id=f"dlv_{index}",
        )
        for index in range(2)
    ]
    started = asyncio.Event()
    release = asyncio.Event()
    calls = 0

    async def render_once(_html: str) -> SimpleNamespace:
        nonlocal calls
        calls += 1
        if calls == 2:
            started.set()
        await release.wait()
        return SimpleNamespace(content=b"%PDF-single-flight")

    async def update_cache(*_args: object, **_kwargs: object) -> None:
        return None

    monkeypatch.setattr(deliverable_routes, "render_pdf_bytes", render_once)
    monkeypatch.setattr(deliverable_routes, "_update_cache_key", update_cache)
    tasks = [
        *[
            asyncio.create_task(
                deliverable_routes._cached_pdf(request, rows[0], access_scope="owner@example.com")
            )
            for _ in range(8)
        ],
        asyncio.create_task(
            deliverable_routes._cached_pdf(request, rows[1], access_scope="owner@example.com")
        ),
    ]
    await started.wait()
    assert calls == 2
    release.set()

    assert await asyncio.gather(*tasks) == [b"%PDF-single-flight"] * 9
    await asyncio.sleep(0)
    assert calls == 2
    assert deliverable_routes._pdf_render_flights == {}


@pytest.mark.asyncio
async def test_pdf_cache_single_flight_failure_cleans_up_and_allows_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _SingleFlightStore()
    request = _single_flight_request(store)
    row = _row(pdf_cache_key=None, storage_namespace="deliverables", storage_object_id="dlv_retry")
    calls = 0

    async def render(_html: str) -> SimpleNamespace:
        nonlocal calls
        calls += 1
        if calls == 1:
            await asyncio.sleep(0)
            raise DeliverableRenderError("render_timeout")
        return SimpleNamespace(content=b"%PDF-retry")

    async def update_cache(*_args: object, **_kwargs: object) -> None:
        return None

    monkeypatch.setattr(deliverable_routes, "render_pdf_bytes", render)
    monkeypatch.setattr(deliverable_routes, "_update_cache_key", update_cache)
    failures = await asyncio.gather(
        *[
            deliverable_routes._cached_pdf(request, row, access_scope="owner@example.com")
            for _ in range(4)
        ],
        return_exceptions=True,
    )
    assert all(getattr(error, "status_code", None) == 503 for error in failures)
    await asyncio.sleep(0)
    assert deliverable_routes._pdf_render_flights == {}
    assert (
        await deliverable_routes._cached_pdf(request, row, access_scope="owner@example.com")
        == b"%PDF-retry"
    )
    assert calls == 2


@pytest.mark.asyncio
async def test_authenticated_view_authz_and_cache(
    task_continuation_db: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = 0

    def fake_render(row: object, *, download_pdf_url: str | None = None) -> str:
        nonlocal calls
        calls += 1
        link = '<a href="/download.pdf">Download PDF</a>' if download_pdf_url else ""
        return f"<!doctype html><html><body>{getattr(row, 'title', '')}{link}</body></html>"

    monkeypatch.setattr(deliverable_routes, "render_standalone_html", fake_render)
    with _client(task_continuation_db, monkeypatch, email="owner@example.com") as client:
        first = client.get("/api/v1/deliverables/dlv_rich/view")
        second = client.get("/api/v1/deliverables/dlv_rich/view")
    assert first.status_code == 200
    assert second.status_code == 200
    assert calls == 1
    assert "Download PDF" in first.text
    assert "script-src 'unsafe-inline'" in first.headers["content-security-policy"]

    with _client(task_continuation_db, monkeypatch, email="other@example.com") as client:
        denied = client.get("/api/v1/deliverables/dlv_rich/view")
    assert denied.status_code == 404


@pytest.mark.asyncio
async def test_share_token_valid_tampered_and_expired(
    task_continuation_db: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _client(task_continuation_db, monkeypatch, email="owner@example.com") as client:
        created = client.post("/api/v1/deliverables/dlv_rich/share-link")
        assert created.status_code == 200
        url = created.json()["url"]
        token = url.rsplit("/", 1)[-1]
        short = client.get(url)
        assert short.status_code == 200
        viewed = client.get(f"/api/v1/deliverables/share/{token}/view")
        assert viewed.status_code == 200
        assert "Rich report" in viewed.text
        assert "Download PDF" in viewed.text

        tampered = client.get(f"/api/v1/deliverables/share/{token}x/view")
        assert tampered.status_code == 404

        expires_at = datetime.fromisoformat(created.json()["expires_at"])
        monkeypatch.setattr(deliverable_links.time, "time", lambda: expires_at.timestamp() + 1)
        expired_response = client.get(f"/api/v1/deliverables/share/{token}/view")
        assert expired_response.status_code == 404


@pytest.mark.asyncio
async def test_public_share_routes_bypass_session_auth_but_verify_token(
    task_continuation_db: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def fake_pdf(_request: object, _row: object, *, access_scope: str) -> bytes:
        assert access_scope.startswith("share:")
        return b"%PDF-public"

    monkeypatch.setattr(deliverable_routes, "_cached_pdf", fake_pdf)
    link = deliverable_links.signed_deliverable_view_link(
        task_continuation_db.artifact_store,  # type: ignore[attr-defined]
        "dlv_rich",
        base_url="https://cognis.example.test",
    )
    token = _share_token(link.url)

    with _middleware_client(task_continuation_db) as client:
        viewed = client.get(f"/api/v1/deliverables/share/{token}/view")
        downloaded = client.get(f"/api/v1/deliverables/share/{token}/download.pdf")
        tampered_view = client.get(f"/api/v1/deliverables/share/{token}x/view")
        tampered_pdf = client.get(f"/api/v1/deliverables/share/{token}x/download.pdf")

        assert viewed.status_code == 200
        assert "Rich report" in viewed.text
        assert viewed.headers["cache-control"] == "no-store"
        assert downloaded.status_code == 200
        assert downloaded.content == b"%PDF-public"
        assert downloaded.headers["content-type"] == "application/pdf"
        assert downloaded.headers["cache-control"] == "no-store"
        assert tampered_view.status_code == 404
        assert tampered_pdf.status_code == 404

        assert link.expires_at is not None
        monkeypatch.setattr(
            deliverable_links.time,
            "time",
            lambda: link.expires_at.timestamp() + 1,
        )
        expired_view = client.get(f"/api/v1/deliverables/share/{token}/view")
        expired_pdf = client.get(f"/api/v1/deliverables/share/{token}/download.pdf")
        assert expired_view.status_code == 404
        assert expired_pdf.status_code == 404


@pytest.mark.asyncio
async def test_public_share_auth_exemption_rejects_method_and_path_boundaries(
    task_continuation_db: object,
) -> None:
    link = deliverable_links.signed_deliverable_view_link(
        task_continuation_db.artifact_store,  # type: ignore[attr-defined]
        "dlv_rich",
        base_url="https://cognis.example.test",
    )
    token = _share_token(link.url)

    with _middleware_client(task_continuation_db) as client:
        responses = [
            client.post(f"/api/v1/deliverables/share/{token}/view"),
            client.get("/api/v1/deliverables/share//view"),
            client.get(f"/api/v1/deliverables/share/{token}/view/extra"),
            client.get(f"/api/v1/deliverables/share/{token}/download.pdf/extra"),
            client.get("/api/v1/deliverables/dlv_rich/view"),
            client.get("/api/v1/deliverables/dlv_rich/download.pdf"),
        ]

    assert [response.status_code for response in responses] == [401] * len(responses)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "token_boundary",
    [
        "%2F",
        "%252F",
        "%25252F",
        "%2e%2e",
        "%252e%252e",
        "/",
        "..",
    ],
)
async def test_public_share_auth_exemption_rejects_encoded_and_normalized_boundaries(
    task_continuation_db: object,
    token_boundary: str,
) -> None:
    with _middleware_client(task_continuation_db) as client:
        response = client.get(f"/api/v1/deliverables/share/{token_boundary}/view")

    assert response.status_code == 401


@pytest.mark.asyncio
async def test_public_share_rate_limit_scopes_token_and_client_and_covers_view_pdf(
    task_continuation_db: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def fake_pdf(_request: object, _row: object, *, access_scope: str) -> bytes:
        return b"%PDF-public"

    monkeypatch.setattr(deliverable_routes, "_cached_pdf", fake_pdf)
    store = task_continuation_db.artifact_store  # type: ignore[attr-defined]
    rich_link = deliverable_links.signed_deliverable_view_link(
        store,
        "dlv_rich",
        base_url="https://cognis.example.test",
    )
    owner_link = deliverable_links.signed_deliverable_view_link(
        store,
        "dlv_owner",
        base_url="https://cognis.example.test",
    )
    rich_token = _share_token(rich_link.url)
    owner_token = _share_token(owner_link.url)
    limiter = RequestRateLimiter(read_requests_per_minute=2)

    with _middleware_client(
        task_continuation_db,
        public_share_rate_limiter=limiter,
        api_rate_limiter=RequestRateLimiter(read_requests_per_minute=0),
        client_address=("192.0.2.10", 50000),
    ) as first_client:
        assert first_client.get(f"/api/v1/deliverables/share/{rich_token}/view").status_code == 200
        assert (
            first_client.get(f"/api/v1/deliverables/share/{rich_token}/download.pdf").status_code
            == 200
        )
        limited = first_client.get(f"/api/v1/deliverables/share/{rich_token}/view")
        assert limited.status_code == 429
        assert limited.json()["error"]["code"] == "rate_limited"
        assert first_client.get(f"/api/v1/deliverables/share/{owner_token}/view").status_code == 200

    with _middleware_client(
        task_continuation_db,
        public_share_rate_limiter=limiter,
        client_address=("192.0.2.11", 50000),
    ) as second_client:
        assert second_client.get(f"/api/v1/deliverables/share/{rich_token}/view").status_code == 200


@pytest.mark.asyncio
async def test_public_share_aggregate_limit_bounds_bogus_token_state(
    task_continuation_db: object,
) -> None:
    store = task_continuation_db.artifact_store  # type: ignore[attr-defined]
    link = deliverable_links.signed_deliverable_view_link(
        store,
        "dlv_rich",
        base_url="https://cognis.example.test",
    )
    token = _share_token(link.url)
    token_limiter = RequestRateLimiter(
        read_requests_per_minute=2,
        max_state_entries=2,
    )
    client_limiter = RequestRateLimiter(read_requests_per_minute=2)

    with _middleware_client(
        task_continuation_db,
        public_share_rate_limiter=token_limiter,
        public_share_client_rate_limiter=client_limiter,
        client_address=("192.0.2.11", 50000),
    ) as legitimate_client:
        valid_path = f"/api/v1/deliverables/share/{token}/view"
        assert legitimate_client.get(valid_path).status_code == 200

    with _middleware_client(
        task_continuation_db,
        public_share_rate_limiter=token_limiter,
        public_share_client_rate_limiter=client_limiter,
        client_address=("192.0.2.10", 50000),
    ) as attacker:
        assert attacker.get("/api/v1/deliverables/share/bogus-a/view").status_code == 404
        admitted_keys = frozenset(token_limiter._state)
        assert attacker.get("/api/v1/deliverables/share/bogus-b/view").status_code == 429
        assert attacker.get("/api/v1/deliverables/share/bogus-c/view").status_code == 429
        for index in range(100):
            assert attacker.get(f"/api/v1/deliverables/share/bogus-{index}/view").status_code == 429

    assert len(token_limiter._state) == 2
    assert frozenset(token_limiter._state) == admitted_keys
    with _middleware_client(
        task_continuation_db,
        public_share_rate_limiter=token_limiter,
        public_share_client_rate_limiter=client_limiter,
        client_address=("192.0.2.11", 50000),
    ) as legitimate_client:
        assert legitimate_client.get(valid_path).status_code == 200
        assert legitimate_client.get(valid_path).status_code == 429
    assert len(token_limiter._state) == 2
    assert frozenset(token_limiter._state) == admitted_keys


@pytest.mark.asyncio
async def test_public_share_client_ip_ignores_untrusted_forwarded_header(
    task_continuation_db: object,
) -> None:
    store = task_continuation_db.artifact_store  # type: ignore[attr-defined]
    link = deliverable_links.signed_deliverable_view_link(
        store,
        "dlv_rich",
        base_url="https://cognis.example.test",
    )
    token = _share_token(link.url)
    limiter = RequestRateLimiter(read_requests_per_minute=1)

    with _middleware_client(
        task_continuation_db,
        public_share_rate_limiter=limiter,
        client_address=("198.51.100.10", 50000),
    ) as client:
        path = f"/api/v1/deliverables/share/{token}/view"
        assert client.get(path, headers={"x-forwarded-for": "192.0.2.1"}).status_code == 200
        assert client.get(path, headers={"x-forwarded-for": "192.0.2.2"}).status_code == 429


@pytest.mark.asyncio
async def test_public_share_client_ip_uses_trusted_proxy_chain(
    task_continuation_db: object,
) -> None:
    store = task_continuation_db.artifact_store  # type: ignore[attr-defined]
    link = deliverable_links.signed_deliverable_view_link(
        store,
        "dlv_rich",
        base_url="https://cognis.example.test",
    )
    token = _share_token(link.url)
    limiter = RequestRateLimiter(read_requests_per_minute=1)

    with _middleware_client(
        task_continuation_db,
        public_share_rate_limiter=limiter,
        client_address=("10.0.0.2", 50000),
        trusted_proxy_cidrs=("10.0.0.0/8",),
    ) as client:
        path = f"/api/v1/deliverables/share/{token}/view"
        first = {"x-forwarded-for": "203.0.113.9, 198.51.100.1, 10.1.1.1"}
        spoofed_left = {"x-forwarded-for": "192.0.2.99, 198.51.100.1, 10.1.1.1"}
        other_client = {"x-forwarded-for": "198.51.100.2, 10.1.1.1"}
        assert client.get(path, headers=first).status_code == 200
        assert client.get(path, headers=spoofed_left).status_code == 429
        assert client.get(path, headers=other_client).status_code == 200


@pytest.mark.asyncio
async def test_pdf_endpoint_reports_renderer_failure(
    task_continuation_db: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def fail_pdf(_html: str) -> object:
        raise DeliverableRenderError("render_timeout")

    monkeypatch.setattr(deliverable_routes, "render_pdf_bytes", fail_pdf)
    with _client(task_continuation_db, monkeypatch, email="owner@example.com") as client:
        response = client.get("/api/v1/deliverables/dlv_rich/download.pdf")
    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "render_unavailable"
    assert "render_timeout" in response.json()["detail"]["message"]


def test_standalone_html_has_neutral_identity_and_offline_emoji_images() -> None:
    rendered = render_standalone_html(
        _row(
            title="Ranní přehled",
            rich_payload={
                "metadata": {"toc": True},
                "blocks": [
                    {"type": "markdown", "title": "Kalendář 📅", "content": "🇨🇿 Počasí ⛅"},
                    {"type": "markdown", "title": "Trhy 📈", "content": "Palivo ⛽ 1️⃣"},
                    {"type": "markdown", "title": "Další část", "content": "Rodina 👨‍👩‍👧"},
                ],
            },
        ),
        download_pdf_url="/download.pdf",
    )

    assert rendered.count("<h1>Ranní přehled</h1>") == 1
    assert "Cognis report" not in rendered
    assert "Cognis deliverable" not in rendered
    assert "COGNIS DELIVERABLE" not in rendered
    assert rendered.count('class="document-toc"') == 1
    assert 'aria-label="Download PDF"' in rendered
    assert "@font-face" in rendered
    assert "data:font/ttf;base64," in rendered
    assert 'class="emoji-glyph"' in rendered
    assert 'class="emoji-text"' not in rendered
    assert "https://" not in rendered
    assert '@bottom-center { content: counter(page) " / " counter(pages)' in rendered


def test_emoji_tokenizer_consumes_complete_standardized_sequences() -> None:
    source = "©️ ™️ ↗️ ©︎ 🏴󠁧󠁢󠁥󠁮󠁧󠁿 👍🏽 👨‍👩‍👧 A️"
    spans = [(source[start:end], text) for start, end, text in _emoji_spans(source)]

    assert spans == [
        ("©️", False),
        ("™️", False),
        ("↗️", False),
        ("©︎", True),
        ("🏴󠁧󠁢󠁥󠁮󠁧󠁿", False),
        ("👍🏽", False),
        ("👨‍👩‍👧", False),
        ("A️", False),
    ]


def test_emoji_substitution_is_exact_safe_and_preserves_text_presentation() -> None:
    source = "<p>&lt;script&gt;alert(1)&lt;/script&gt; ©️ ©︎ 🏴󠁧󠁢󠁥󠁮󠁧󠁿 A️</p><pre>©️ 🏴</pre><code>™️</code>"
    first = _substitute_emoji(source)
    second = _substitute_emoji(source)
    soup = BeautifulSoup(first, "html.parser")
    emoji = soup.select("span.emoji")

    assert first == second
    assert "<script>" not in first
    assert soup.p is not None
    assert soup.p.get_text(" ", strip=True) == "<script>alert(1)</script> ©️ ©︎ 🏴󠁧󠁢󠁥󠁮󠁧󠁿 A️"
    assert [node["aria-label"] for node in emoji] == ["©️", "🏴󠁧󠁢󠁥󠁮󠁧󠁿", "A️"]
    assert soup.pre is not None and soup.pre.get_text() == "©️ 🏴"
    assert soup.code is not None and soup.code.get_text() == "™️"
    assert str(emoji[0]) == (
        '<span aria-label="©️" class="emoji" role="img">'
        '<span aria-hidden="true" class="emoji-glyph">©️</span></span>'
    )


@pytest.mark.asyncio
async def test_pdf_emoji_rendering_is_self_contained_and_deterministic() -> None:
    rendered = render_standalone_html(
        _row(
            title="Offline emoji",
            rich_payload={
                "blocks": [
                    {"type": "markdown", "title": "Přehled 📅", "content": "🇨🇿 ⛅ 📈 ⛽ 1️⃣ 👨‍👩‍👧"}
                ]
            },
        )
    )

    first = await render_pdf_bytes(rendered)
    second = await render_pdf_bytes(rendered)
    assert first.content.startswith(b"%PDF-")
    assert second.content.startswith(b"%PDF-")
    assert len(first.content) > 1_000
    assert abs(len(first.content) - len(second.content)) < 128
    reader = PdfReader(BytesIO(first.content))
    extracted = "\n".join(page.extract_text() or "" for page in reader.pages)
    assert "�" not in extracted
    for sequence in ("📅", "🇨🇿", "⛅", "📈", "⛽", "1️⃣", "👨‍👩‍👧"):
        assert sequence in extracted
    font_names = {
        str(font.get_object().get("/BaseFont", ""))
        for page in reader.pages
        for font in page["/Resources"].get("/Font", {}).values()
    }
    assert any("Noto-Color-Emoji" in name for name in font_names)


def test_server_cards_render_variants_tones_icons_deks_links_and_citations() -> None:
    rendered = render_standalone_html(
        _row(
            rich_payload={
                "sources": [
                    {"id": "one", "title": "Primary", "url": "https://news.example/one"},
                    {"id": "two", "title": "Secondary", "url": "https://news.example/two"},
                ],
                "blocks": [
                    {
                        "type": "card",
                        "variant": "feature",
                        "tone": "positive",
                        "emoji": "🛰️",
                        "title": "Clickable headline",
                        "dek": "A concise editorial standfirst.",
                        "content": "Feature body.",
                        "source_ids": ["one"],
                    },
                    {
                        "type": "card",
                        "variant": "action",
                        "tone": "warning",
                        "icon": "!",
                        "title": "Act now",
                        "citations": ["one", "two"],
                    },
                ],
            }
        )
    )
    soup = BeautifulSoup(rendered, "html.parser")

    feature = soup.select_one(".block-card.card-variant-feature.tone-positive")
    assert feature is not None
    assert feature.select_one(".card-icon").get_text(strip=True) == "🛰️"
    assert feature.select_one(".card-dek").get_text(strip=True) == (
        "A concise editorial standfirst."
    )
    assert feature.select_one('h2 a[href="https://news.example/one"]').get_text(strip=True) == (
        "Clickable headline"
    )
    assert [link.get_text(strip=True) for link in feature.select("a.citation")] == ["[1]"]
    assert [
        link.get_text(strip=True) for link in soup.select(".card-variant-action a.citation")
    ] == [
        "[1]",
        "[2]",
    ]
    assert len(soup.select(".bibliography li")) == 2


@pytest.mark.parametrize(
    ("chart_type", "structural_selector"),
    [
        ("line", "polyline.chart-line"),
        ("area", "polygon.chart-area"),
        ("bar", "rect.chart-bar"),
        ("grouped_bar", "rect.chart-bar"),
        ("stacked_bar", "rect.chart-stack-segment[data-stack-start][data-stack-end]"),
        ("sparkline", "polyline.chart-line"),
        ("progress", "rect.chart-progress-fill"),
        ("range", "g.chart-range-mark"),
        ("donut", "path.chart-donut-segment"),
    ],
)
def test_server_chart_renderer_owns_deterministic_accessible_svg_and_table(
    chart_type: str,
    structural_selector: str,
) -> None:
    payload = {
        "blocks": [
            {
                "type": "chart",
                "spec_version": "cognis.chart.v1",
                "chart_type": chart_type,
                "title": f"{chart_type.title()} trend",
                "description": "Three point service trend.",
                "series": [
                    {
                        "id": "healthy",
                        "label": "Healthy",
                        "points": [
                            {"x": "Mon", "y": [1, 3] if chart_type == "range" else 2},
                            {"x": "Tue", "y": [3, 5] if chart_type == "range" else 4},
                            {"x": "Wed", "y": [5, 7] if chart_type == "range" else 6},
                        ],
                    },
                    {
                        "id": "degraded",
                        "label": "Degraded",
                        "points": [
                            {"x": "Mon", "y": [0, 2] if chart_type == "range" else 1},
                            {"x": "Tue", "y": [1, 3] if chart_type == "range" else 2},
                            {"x": "Wed", "y": [0, 2] if chart_type == "range" else 1},
                        ],
                    },
                ],
                "x_axis": {"type": "category", "label": "Weekday", "unit": "local"},
                "y_axis": {"type": "linear", "label": "Services", "unit": "%", "min": 0, "max": 10},
                "stack": chart_type == "stacked_bar",
                "legend_position": "bottom",
                "palette_token": "default",
                "source": "Operations data",
                "source_url": "https://metrics.example/service",
                "observed_at": "2026-07-14T08:00:00Z",
            }
        ]
    }
    first = render_standalone_html(_row(rich_payload=payload))
    second = render_standalone_html(_row(rich_payload=payload))
    soup = BeautifulSoup(first, "html.parser")

    assert first == second
    svg = soup.select_one(f"svg.chart-{chart_type}")
    assert svg is not None
    assert svg["role"] == "img"
    assert svg["aria-label"] == "Three point service trend."
    # WeasyPrint has been observed to lay out <text> axis labels using a
    # different (or no) coordinate transform than <line>/<path> geometry
    # when the <svg> has no explicit width/height (only viewBox) -- labels
    # then spill out as loose sequential text above the chart instead of
    # sitting at their intended x/y position. Explicit width/height (in
    # addition to viewBox) give WeasyPrint a consistent scale to compute,
    # fixing the leak; `.chart-svg { width: 100% }` in CSS still overrides
    # these attributes for on-screen/responsive sizing in every renderer.
    # BeautifulSoup's html.parser lowercases attribute names, so the
    # source's `viewBox` is read back as `viewbox` here.
    assert svg["width"] == svg["viewbox"].split()[2]
    assert svg["height"] == svg["viewbox"].split()[3]
    assert svg.select_one("title").get_text(strip=True) == "Three point service trend."
    assert svg.select_one("defs linearGradient") is not None
    assert svg.select_one(structural_selector) is not None
    assert svg.select_one("script, filter, image, use") is None
    assert "http://" not in str(svg)
    assert "https://" not in str(svg)
    if chart_type not in {"progress", "donut", "sparkline"}:
        axis_titles = [title.get_text(strip=True) for title in svg.select(".chart-axis-title")]
        assert axis_titles == ["Weekday (local)", "Services (%)"]
        tick_labels = [label.get_text(strip=True) for label in svg.select(".chart-axis-label")]
        assert "0" in tick_labels
        assert "10" in tick_labels
    if chart_type == "area":
        assert svg.select_one("polygon.chart-area")["fill"].startswith("url(#chart-")
    if chart_type == "stacked_bar":
        assert svg.select_one('[data-series="degraded"][data-stack-start="2"]') is not None
    if chart_type == "donut":
        assert " A " in svg.select_one("path.chart-donut-segment")["d"]
        assert svg.select_one("circle.chart-donut-empty") is None
    legend = svg.select_one(".chart-legend-bottom")
    assert legend is not None
    assert len(legend.select(".chart-legend-item")) == (3 if chart_type == "donut" else 2)
    assert soup.select_one(".chart-data summary").get_text(strip=True) == "View data table"
    assert [
        cell.get_text(strip=True) for cell in soup.select(".chart-data tbody tr")[-1].select("td")
    ] == (["Wed", "5–7", "0–2"] if chart_type == "range" else ["Wed", "6", "1"])
    assert "Operations data" in first
    assert "2026-07-14T08:00:00Z" in first


@pytest.mark.parametrize("position", ["top", "right", "bottom", "none"])
def test_chart_legend_flows_inside_viewbox_without_overlapping_plot(position: str) -> None:
    model = normalize_chart(
        _canonical_chart(
            x_axis={"type": "category"},
            y_axis={"type": "linear"},
            legend_position=position,
            series=[
                {
                    "id": f"series-{index}",
                    "label": f"Long renderer-owned series {index}",
                    "points": [{"x": "A", "y": index + 1}, {"x": "B", "y": index + 2}],
                }
                for index in range(9)
            ],
        )
    )
    assert model is not None

    svg = BeautifulSoup(render_chart_svg(model), "html.parser").select_one("svg")
    legend = svg.select_one(".chart-legend")
    if position == "none":
        assert legend is None
        return

    assert legend is not None
    assert legend["data-position"] == position
    assert len(legend.select(".chart-legend-item")) == 9
    viewbox = [float(value) for value in svg["viewbox"].split()]
    for item in legend.select(".chart-legend-item"):
        marker = item.select_one("rect")
        label = item.select_one("text")
        assert 0 <= float(marker["x"]) < viewbox[2]
        assert 0 <= float(marker["y"]) < viewbox[3]
        assert float(label["x"]) + len(label.get_text()) * 7 <= viewbox[2]
    if position == "right":
        items_by_row: dict[str, list[Any]] = {}
        for item in legend.select(".chart-legend-item"):
            items_by_row.setdefault(item.select_one("rect")["y"], []).append(item)
        for items in items_by_row.values():
            items.sort(key=lambda item: float(item.select_one("rect")["x"]))
            for current, following in zip(items, items[1:], strict=False):
                label = current.select_one("text")
                label_end = float(label["x"]) + len(label.get_text()) * 7
                assert label_end <= float(following.select_one("rect")["x"])
    if position in {"top", "bottom"}:
        assert (
            len({item.select_one("rect")["y"] for item in legend.select(".chart-legend-item")}) > 1
        )


@pytest.mark.parametrize(
    ("palette_token", "expected_color"),
    [
        ("default", "#0e7490"),
        ("cool", "#0369a1"),
        ("warm", "#c2410c"),
        ("categorical", "#2563eb"),
    ],
)
def test_chart_palette_token_selects_renderer_owned_palette(
    palette_token: str,
    expected_color: str,
) -> None:
    model = normalize_chart(
        _canonical_chart(
            palette_token=palette_token,
            x_axis={"type": "category"},
            series=[{"id": "value", "label": "Value", "points": [{"x": "A", "y": 4}]}],
        )
    )
    assert model is not None

    svg = render_chart_svg(model)
    assert f"palette-{palette_token}" in svg
    assert f'stop-color="{expected_color}"' in svg


@pytest.mark.parametrize(
    ("axis", "expected_endpoint"),
    [
        ({"type": "linear", "min": 10}, "10"),
        ({"type": "linear", "max": -10}, "-10"),
    ],
)
def test_chart_preserves_incompatible_one_sided_y_bound(
    axis: dict[str, Any],
    expected_endpoint: str,
) -> None:
    model = normalize_chart(
        _canonical_chart(
            x_axis={"type": "category"},
            y_axis=axis,
            series=[
                {
                    "id": "value",
                    "label": "Value",
                    "points": [{"x": "A", "y": 1}, {"x": "B", "y": 2}],
                }
            ],
        )
    )
    assert model is not None

    svg = BeautifulSoup(render_chart_svg(model), "html.parser")
    ticks = [label.get_text(strip=True) for label in svg.select(".chart-axis-label")]
    assert expected_endpoint in ticks


def test_naive_time_axis_geometry_is_host_timezone_independent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    model = normalize_chart(
        _canonical_chart(
            x_axis={"type": "time"},
            y_axis={"type": "linear"},
            series=[
                {
                    "id": "value",
                    "label": "Value",
                    "points": [
                        {"x": "2026-03-29T00:30:00", "y": 1},
                        {"x": "2026-03-29T01:30:00", "y": 2},
                        {"x": "2026-03-29T03:30:00", "y": 3},
                    ],
                }
            ],
        )
    )
    assert model is not None

    def render_in_timezone(zone: str) -> str:
        try:
            with monkeypatch.context() as context:
                context.setenv("TZ", zone)
                time.tzset()
                return render_chart_svg(model)
        finally:
            time.tzset()

    assert render_in_timezone("UTC") == render_in_timezone("Europe/Prague")


@pytest.mark.asyncio
async def test_single_segment_donut_uses_non_degenerate_full_circle_arcs_in_pdf() -> None:
    block = _canonical_chart(
        chart_type="donut",
        x_axis={"type": "category"},
        y_axis={"type": "linear", "unit": "%"},
        legend_position="none",
        series=[
            {
                "id": "complete",
                "label": "Complete",
                "points": [{"x": "All", "y": 100}],
            }
        ],
    )
    model = normalize_chart(block)
    assert model is not None

    svg = BeautifulSoup(render_chart_svg(model), "html.parser")
    path = svg.select_one("path.chart-donut-segment")
    assert path is not None
    assert path["d"].count(" A ") == 4

    rendered = render_standalone_html(_row(rich_payload={"blocks": [block]}))
    pdf = await render_pdf_bytes(rendered)
    assert pdf.content.startswith(b"%PDF")
    assert len(PdfReader(BytesIO(pdf.content)).pages) == 1


@pytest.mark.asyncio
async def test_all_canonical_svg_charts_survive_weasyprint_pdf_smoke() -> None:
    blocks = []
    for chart_type in CANONICAL_CHART_TYPES:
        blocks.append(
            {
                "type": "chart",
                "spec_version": CHART_SPEC_VERSION,
                "chart_type": chart_type,
                "title": chart_type.replace("_", " ").title(),
                "description": f"{chart_type} PDF smoke.",
                "series": [
                    {
                        "id": "primary",
                        "label": "Primary",
                        "points": [
                            {"x": "A", "y": [1, 3] if chart_type == "range" else 3},
                            {"x": "B", "y": [2, 5] if chart_type == "range" else 5},
                        ],
                    },
                    {
                        "id": "secondary",
                        "label": "Secondary",
                        "points": [
                            {"x": "A", "y": [0, 2] if chart_type == "range" else 2},
                            {"x": "B", "y": [1, 4] if chart_type == "range" else 4},
                        ],
                    },
                ],
                "x_axis": {"type": "category", "label": "Category"},
                "y_axis": {"type": "linear", "label": "Value", "unit": "ms", "min": 0, "max": 12},
                "stack": chart_type == "stacked_bar",
                "legend_position": "right",
                "palette_token": "warm",
            }
        )

    rendered = render_standalone_html(_row(rich_payload={"blocks": blocks}))
    pdf = await render_pdf_bytes(rendered)
    reader = PdfReader(BytesIO(pdf.content))

    assert pdf.content.startswith(b"%PDF")
    assert len(reader.pages) >= 1


def test_server_media_uses_proxy_callback_and_preserves_missing_alt_credit() -> None:
    calls: list[tuple[MediaReference, str]] = []

    def resolver(reference: MediaReference, target: str) -> ResolvedMedia | None:
        calls.append((reference, target))
        if reference.ref_id != "hero-image":
            return None
        return ResolvedMedia(
            src="/api/v1/deliverables/dlv_render/media/hero-image",
            mime_type="image/png",
            filename="hero.png",
        )

    rendered = render_standalone_html(
        _row(
            rich_payload={
                "assets": [
                    {
                        "id": "hero-image",
                        "artifact_id": "att_owned",
                        "mime_type": "image/png",
                    },
                    {
                        "id": "missing-image",
                        "artifact_id": "att_missing",
                        "mime_type": "image/png",
                    },
                ],
                "blocks": [
                    {
                        "type": "figure",
                        "asset_id": "hero-image",
                        "alt": "A monitored station",
                        "credit": "Operations",
                    },
                    {
                        "type": "figure",
                        "asset_id": "missing-image",
                        "alt": "Unavailable station image",
                        "credit": "Field team",
                        "caption": "Image intentionally unavailable.",
                    },
                ],
            }
        ),
        media_resolver=resolver,
    )
    soup = BeautifulSoup(rendered, "html.parser")

    image = soup.select_one('img[src^="/api/v1/deliverables/"]')
    assert image["alt"] == "A monitored station"
    assert "Source: Operations" in soup.get_text(" ", strip=True)
    assert "Image intentionally unavailable." in soup.get_text(" ", strip=True)
    assert "Source: Field team" in soup.get_text(" ", strip=True)
    assert len(soup.select("img")) == 1
    assert [target for _reference, target in calls] == ["html", "html"]


def test_one_event_agenda_is_point_event_with_one_current_time_and_clean_label() -> None:
    rendered = render_standalone_html(
        _row(
            rich_payload={
                "blocks": [
                    {
                        "type": "day_agenda",
                        "variant": "compact",
                        "title": "Today",
                        "timezone": "UTC",
                        "now": "2026-07-14T08:30:00Z",
                        "items": [
                            {
                                "title": "Point event",
                                "start": "2026-07-14T09:00:00Z",
                                "end": "2026-07-14T09:00:00Z",
                            }
                        ],
                    }
                ]
            }
        )
    )
    soup = BeautifulSoup(rendered, "html.parser")

    agenda = soup.select_one('.block-day-agenda[data-variant="compact"]')
    assert agenda is not None
    assert agenda.get_text(" ", strip=True).count("08:30") == 1
    assert agenda.get_text(" ", strip=True).count("09:00") == 1
    assert "Next" in agenda.get_text(" ", strip=True)
    assert "09:00–09:00" not in agenda.get_text("", strip=True)


def test_accordion_is_interactive_html_and_print_expands_content() -> None:
    rendered = render_standalone_html(
        _row(
            rich_payload={
                "blocks": [
                    {
                        "type": "accordion",
                        "title": "Stories",
                        "items": [
                            {
                                "type": "card",
                                "title": "Cited story",
                                "summary": "One-line impact summary.",
                                "content": "Expanded in print.",
                            }
                        ],
                    }
                ]
            }
        )
    )
    soup = BeautifulSoup(rendered, "html.parser")

    details = soup.select_one("details.accordion-item")
    assert details is not None
    assert not details.has_attr("open")
    assert details.select_one("summary span").get_text(strip=True) == "Cited story"
    assert details.select_one("summary small").get_text(strip=True) == "One-line impact summary."
    assert details.select_one("h3") is None
    assert "details.accordion-item > * { display: block !important; }" in rendered


@pytest.mark.asyncio
async def test_pdf_uses_internal_media_bytes_and_prints_chart_and_accordion() -> None:
    tiny_png = (
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk"
        "YAAAAAYAAjCB0C8AAAAASUVORK5CYII="
    )

    def resolver(reference: MediaReference, target: str) -> ResolvedMedia | None:
        assert target == "pdf"
        return ResolvedMedia(
            src=f"data:image/png;base64,{tiny_png}",
            mime_type="image/png",
            filename=f"{reference.ref_id}.png",
        )

    rendered = render_standalone_html(
        _row(
            rich_payload={
                "assets": [
                    {
                        "id": "embedded",
                        "artifact_id": "att_embedded",
                        "mime_type": "image/png",
                    }
                ],
                "blocks": [
                    {
                        "type": "figure",
                        "asset_id": "embedded",
                        "alt": "Embedded pixel",
                        "caption": "Internally resolved image.",
                    },
                    {
                        "type": "chart",
                        "title": "Real chart",
                        "description": "Three point trend.",
                        "rows": [
                            {"label": "A", "value": 1},
                            {"label": "B", "value": 2},
                            {"label": "C", "value": 3},
                        ],
                    },
                    {
                        "type": "accordion",
                        "title": "Collapsed stories",
                        "items": [
                            {
                                "type": "card",
                                "title": "Expanded story",
                                "content": "Visible in the PDF.",
                            }
                        ],
                    },
                ],
            }
        ),
        media_resolver=resolver,
        render_target="pdf",
    )
    assert 'src="data:image/png;base64,' in rendered
    pdf = await render_pdf_bytes(rendered)
    reader = PdfReader(BytesIO(pdf.content))
    text = "\n".join(page.extract_text() or "" for page in reader.pages)

    assert pdf.content.startswith(b"%PDF")
    assert "Internally resolved image." in text
    assert "Real chart" in text
    assert "Expanded story" in text
    assert "Visible in the PDF" in text


def test_server_chart_renders_canonical_multi_series() -> None:
    rendered = render_standalone_html(
        _row(
            rich_payload={
                "blocks": [
                    {
                        "type": "chart",
                        "spec_version": "cognis.chart.v1",
                        "chart_type": "line",
                        "series": [
                            {
                                "id": "requests",
                                "label": "Requests",
                                "points": [{"x": "Mon", "y": 10}, {"x": "Tue", "y": 20}],
                            },
                            {
                                "id": "errors",
                                "label": "Errors",
                                "points": [{"x": "Mon", "y": 2}, {"x": "Tue", "y": 1}],
                            },
                        ],
                        "x_axis": {"type": "category"},
                        "y_axis": {"type": "linear"},
                    }
                ]
            }
        )
    )
    soup = BeautifulSoup(rendered, "html.parser")

    assert len(soup.select("svg.chart-line polyline")) == 2
    assert [cell.get_text(strip=True) for cell in soup.select("thead th")] == [
        "Label",
        "Requests",
        "Errors",
    ]
    assert [cell.get_text(strip=True) for cell in soup.select("tbody tr")[-1].select("td")] == [
        "Tue",
        "20",
        "1",
    ]


@pytest.mark.asyncio
async def test_pdf_media_resolver_uses_only_controller_manifest_keys(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    @asynccontextmanager
    async def session_factory() -> object:
        yield object()

    resolved_media = AsyncMock(
        return_value=(
            b"png-bytes",
            {"mime_type": "image/png", "filename": "editorial.png"},
            SimpleNamespace(),
        )
    )
    monkeypatch.setattr(deliverable_routes, "resolve_deliverable_media", resolved_media)
    artifact_store = SimpleNamespace()
    request = SimpleNamespace(
        app=SimpleNamespace(
            state=SimpleNamespace(
                session_factory=session_factory,
                artifact_store=artifact_store,
            )
        )
    )
    row = _row(
        rich_payload={
            "media_manifest": {
                "media_0123456789abcdef01234567": {
                    "artifact_ref": "art_0123456789abcdef",
                    "mime_type": "image/png",
                    "filename": "editorial.png",
                }
            },
            "blocks": [
                {
                    "type": "card",
                    "media": {"key": "media_0123456789abcdef01234567", "alt": "Editorial"},
                }
            ],
        },
    )

    resolver = await deliverable_routes._embedded_media_resolver(request, row)  # noqa: SLF001
    resolved = resolver(
        MediaReference(
            ref_id="media_0123456789abcdef01234567",
            artifact_id=None,
            alt="Editorial",
            credit=None,
            mime_type=None,
        ),
        "pdf",
    )

    assert resolved is not None
    assert resolved.src == "data:image/png;base64,cG5nLWJ5dGVz"
    resolved_media.assert_awaited_once_with(
        ANY,
        artifact_store,
        row,
        "media_0123456789abcdef01234567",
    )


def test_generic_deliverable_headings_use_sans_not_serif() -> None:
    """Renderer-convergence regression test: generic (non-pulse) headings
    previously hardcoded Georgia serif unconditionally, while the web
    renderer's generic default is sans-serif (matching the app shell). Serif
    display type is a pulse-only editorial choice, not a universal default."""

    rendered = render_standalone_html(
        _row(rich_payload={"blocks": [{"type": "hero", "title": "Heading"}]})
    )
    generic_css = rendered.split(".presentation-pulse", 1)[0]
    assert "h1 { max-width: 48rem; margin: 0; font-family: inherit;" in generic_css
    assert "Georgia" not in generic_css


def test_pulse_deliverable_headings_still_use_serif() -> None:
    """Pulse's editorial serif heading treatment must be preserved exactly
    (not regressed by making the generic default sans-serif)."""

    rendered = render_standalone_html(
        _row(
            rich_payload={
                "metadata": {"presentation": "pulse", "pulse_variant": "daily"},
                "blocks": [{"type": "hero", "title": "Heading"}],
            }
        )
    )
    assert (
        ".presentation-pulse h1,\n.presentation-pulse .block > h2,\n"
        ".presentation-pulse .block-quote p,\n.presentation-pulse .claim-card h3,\n"
        '.presentation-pulse .bibliography > h2 { font-family: Georgia, "Times New Roman", serif; }'
    ) in rendered


@pytest.mark.asyncio
async def test_pulse_pdf_metrics_have_semantic_grid_spacing_and_separate_text() -> None:
    rendered = render_standalone_html(
        _row(
            rich_payload={
                "metadata": {"presentation": "pulse", "pulse_variant": "daily"},
                "blocks": [
                    {
                        "type": "dashboard",
                        "blocks": [
                            {"type": "metric", "label": "Agenda", "value": "3"},
                            {"type": "metric", "label": "Conditions", "value": "18 °C"},
                            {"type": "metric", "label": "Market", "value": "Stable"},
                            {"type": "metric", "label": "Priority", "value": "Focus"},
                        ],
                    }
                ],
            }
        )
    )

    assert '<div class="dashboard-blocks">' in rendered
    assert "repeat(4, minmax(28mm, 1fr))" in rendered
    assert "gap: 3mm" in rendered
    reader = PdfReader(BytesIO((await render_pdf_bytes(rendered)).content))
    text = "\n".join(page.extract_text() or "" for page in reader.pages)
    for token in ("Agenda", "Conditions", "Market", "Priority", "Stable", "Focus"):
        assert token in text
    assert "AgendaConditionsMarket" not in text
    assert "StableFocus" not in text


def test_named_icon_tokens_never_leak_and_unicode_remains_supported() -> None:
    rendered = render_standalone_html(
        _row(
            rich_payload={
                "blocks": [
                    {"type": "card", "title": "Unknown", "icon": "newspaper"},
                    {"type": "card", "title": "Known", "icon": "check"},
                    {"type": "card", "title": "Unicode", "icon": "★"},
                ]
            }
        )
    )
    soup = BeautifulSoup(rendered, "html.parser")
    assert "newspaper" not in soup.get_text(" ", strip=True)
    assert [item.get_text(strip=True) for item in soup.select(".card-icon")] == ["✓", "★"]


def test_card_media_has_visible_dimensions_credit_and_internal_source() -> None:
    resolved = ResolvedMedia(
        src="data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAAB",
        mime_type="image/png",
        filename="feature.png",
    )
    rendered = render_standalone_html(
        _row(
            rich_payload={
                "blocks": [
                    {
                        "type": "card",
                        "variant": "feature",
                        "title": "Feature",
                        "media": {
                            "ref": "feature-media",
                            "alt": "Editorial landscape",
                            "credit": "Cognis fixture",
                            "width": 1600,
                            "height": 900,
                        },
                    }
                ]
            }
        ),
        media_resolver=lambda _reference, _target: resolved,
        render_target="pdf",
    )
    image = BeautifulSoup(rendered, "html.parser").select_one(".card-media img")
    assert image is not None
    assert image["src"].startswith("data:image/png;base64,")
    assert image["alt"] == "Editorial landscape"
    assert image["width"] == "1600"
    assert image["height"] == "900"
    assert image.find_parent("figure").find("figcaption").get_text(strip=True) == "Cognis fixture"


def test_pulse_uses_one_compact_reference_section() -> None:
    rendered = render_standalone_html(
        _row(
            rich_payload={
                "metadata": {"presentation": "pulse", "references": {"dedicated_page": False}},
                "sources": [{"id": "one", "title": "One source", "url": "https://example.org/one"}],
                "blocks": [
                    {"type": "card", "title": "Cited", "content": "Body", "citations": ["one"]},
                    {"type": "source_list", "title": "Sources", "numbered": True},
                ],
            }
        )
    )
    soup = BeautifulSoup(rendered, "html.parser")
    assert soup.select_one(".block-sources") is None
    assert len(soup.select(".bibliography")) == 1
    assert soup.select_one(".bibliography h2").get_text(strip=True) == "References"
    assert "bibliography bibliography-compact" in rendered


@pytest.mark.asyncio
async def test_pulse_compact_references_and_agenda_location_fit_two_pages() -> None:
    sources = [
        {
            "id": f"source-{index}",
            "title": f"Compact source {index}",
            "url": f"https://example.org/{index}",
        }
        for index in range(1, 5)
    ]
    stories = [
        {
            "type": "card",
            "variant": "editorial",
            "title": f"Decision-relevant story {index}",
            "summary": "One-line impact summary.",
            "content": "Useful expanded detail for print.",
            "citations": [f"source-{index}"],
        }
        for index in range(1, 5)
    ]
    rendered = render_standalone_html(
        _row(
            rich_payload={
                "metadata": {
                    "presentation": "pulse",
                    "references": {"dedicated_page": True},
                },
                "sources": sources,
                "blocks": [
                    {
                        "type": "dashboard",
                        "blocks": [
                            {"type": "metric", "label": "Agenda", "value": "3"},
                            {"type": "metric", "label": "Conditions", "value": "18 °C"},
                            {"type": "metric", "label": "Market", "value": "Stable"},
                            {"type": "metric", "label": "Priority", "value": "Focus"},
                        ],
                    },
                    {
                        "type": "day_agenda",
                        "title": "Tuesday",
                        "now": "2026-07-14T08:20:00+02:00",
                        "items": [
                            {
                                "title": "Errands",
                                "start": "2026-07-14T10:30:00+02:00",
                                "end": "2026-07-14T11:15:00+02:00",
                                "location": "Lovosice",
                            }
                        ],
                    },
                    {"type": "accordion", "title": "Know", "items": stories},
                    {
                        "type": "chart",
                        "title": "Meaningful trend",
                        "chart_type": "line",
                        "data": [
                            {"label": "07", "value": 17},
                            {"label": "12", "value": 23},
                            {"label": "18", "value": 22},
                        ],
                        "source": "Compact source 1",
                        "source_url": "https://example.org/1",
                    },
                    {"type": "source_list", "title": "References", "numbered": True},
                ],
            }
        )
    )
    bibliography = BeautifulSoup(rendered, "html.parser").select_one(".bibliography")
    assert bibliography is not None
    assert "bibliography-compact" in bibliography.get("class", [])
    assert "bibliography-dedicated" not in bibliography.get("class", [])
    assert 'class="agenda-location"> · Lovosice</span>' in rendered

    reader = PdfReader(BytesIO((await render_pdf_bytes(rendered)).content))
    text = "\n".join(page.extract_text() or "" for page in reader.pages)
    assert len(reader.pages) <= 2
    assert "Errands · Lovosice" in text
    assert "ErrandsLovosice" not in text


def _canonical_chart(**overrides: Any) -> dict[str, Any]:
    chart: dict[str, Any] = {
        "type": "chart",
        "spec_version": CHART_SPEC_VERSION,
        "chart_type": "line",
        "series": [
            {
                "id": "requests",
                "label": "Requests",
                "points": [{"x": "2026-07-15T09:00:00Z", "y": 12, "label": "Current"}],
                "stack": "traffic",
            }
        ],
        "x_axis": {"type": "time", "label": "Time", "unit": "UTC", "min": None, "max": None},
        "y_axis": {
            "type": "linear",
            "label": "Requests",
            "unit": "req/s",
            "min": 0,
            "max": 100,
        },
        "stack": False,
        "legend_position": "bottom",
        "palette_token": "cool",
        "source_ids": ["source-1"],
        "source": "Metrics",
        "source_url": "https://metrics.example.test",
        "observed_at": "2026-07-15T09:05:00Z",
        "description": "Request volume.",
    }
    chart.update(overrides)
    return chart


@pytest.mark.parametrize("chart_type", CANONICAL_CHART_TYPES)
def test_normalize_chart_supports_every_canonical_chart_type(chart_type: str) -> None:
    series = (
        [{"id": "band", "label": "Band", "points": [{"x": "A", "y": [8, 2]}]}]
        if chart_type == "range"
        else [{"id": "value", "label": "Value", "points": [{"x": "A", "y": 4}]}]
    )
    model = normalize_chart(
        _canonical_chart(chart_type=chart_type, x_axis={"type": "category"}, series=series)
    )

    assert model is not None
    assert model.chart_type == chart_type
    assert chart_type in MODEL_CHART_TYPES
    assert f"chart-{chart_type}" in render_chart_svg(model)


def test_normalize_chart_has_frozen_fields_and_orders_temporal_multi_series() -> None:
    model = normalize_chart(
        _canonical_chart(
            series=[
                {
                    "id": "requests",
                    "label": "Requests",
                    "stack": "traffic",
                    "points": [
                        {"x": "2026-07-15T10:00:00Z", "y": 20},
                        {"x": "2026-07-15T08:00:00Z", "y": 10},
                    ],
                },
                {
                    "id": "errors",
                    "label": "Errors",
                    "points": [
                        {"x": "2026-07-15T09:00:00Z", "y": 2},
                        {"x": "2026-07-15T08:00:00Z", "y": 1},
                    ],
                },
            ]
        )
    )

    assert model is not None
    assert [field.name for field in fields(ChartModel)] == [
        "type",
        "spec_version",
        "chart_type",
        "series",
        "x_axis",
        "y_axis",
        "stack",
        "legend_position",
        "palette_token",
        "source_ids",
        "source",
        "source_url",
        "observed_at",
        "description",
    ]
    assert [field.name for field in fields(ChartAxis)] == ["type", "label", "unit", "min", "max"]
    assert [field.name for field in fields(ChartSeries)] == ["id", "label", "points", "stack"]
    assert model.x_axis == ChartAxis(type="time", label="Time", unit="UTC", min=None, max=None)
    assert model.y_axis == ChartAxis(
        type="linear",
        label="Requests",
        unit="req/s",
        min=0.0,
        max=100.0,
    )
    assert model.series[0].stack == "traffic"
    assert model.labels == (
        "2026-07-15T08:00:00Z",
        "2026-07-15T09:00:00Z",
        "2026-07-15T10:00:00Z",
    )
    assert chart_rows(model) == (
        ["Label", "Requests", "Errors"],
        [
            ["2026-07-15T08:00:00Z", "10", "1"],
            ["2026-07-15T09:00:00Z", "", "2"],
            ["2026-07-15T10:00:00Z", "20", ""],
        ],
    )


def test_normalize_chart_defaults_explicit_null_axes() -> None:
    model = normalize_chart(
        _canonical_chart(
            x_axis=None,
            y_axis=None,
            series=[{"id": "value", "points": [{"x": "A", "y": 1}]}],
        )
    )

    assert model is not None
    assert model.x_axis == ChartAxis(
        type="category",
        label=None,
        unit=None,
        min=None,
        max=None,
    )
    assert model.y_axis == ChartAxis(
        type="linear",
        label=None,
        unit=None,
        min=None,
        max=None,
    )
    assert model.series[0].stack is None


def test_normalize_chart_preserves_ranges_for_rows_and_trends() -> None:
    model = normalize_chart(
        _canonical_chart(
            chart_type="range",
            x_axis={"type": "category"},
            series=[
                {
                    "id": "latency",
                    "label": "Latency",
                    "points": [{"x": "p50", "y": [20, 10]}, {"x": "p95", "y": [30, 50]}],
                }
            ],
        )
    )

    assert model is not None
    assert model.series[0].points[0].y == (10.0, 20.0)
    assert chart_rows(model)[1] == [["p50", "10–20"], ["p95", "30–50"]]
    assert chart_trend_text(model) == "Latency: 10–20 → 30–50 (up 20–30)"


@pytest.mark.parametrize(("chart_type", "selector"), [("progress", "rect"), ("donut", "path")])
def test_specialized_svg_charts_render_every_canonical_point(
    chart_type: str,
    selector: str,
) -> None:
    model = normalize_chart(
        _canonical_chart(
            chart_type=chart_type,
            x_axis={"type": "category"},
            series=[
                {
                    "id": "status",
                    "label": "Status",
                    "points": [
                        {"x": "Complete", "y": 60},
                        {"x": "Remaining", "y": 30},
                        {"x": "Blocked", "y": 10},
                    ],
                }
            ],
        )
    )

    assert model is not None
    svg = BeautifulSoup(render_chart_svg(model), "html.parser")
    assert (
        len(
            svg.select(
                f"{selector}.chart-{chart_type}-{'fill' if chart_type == 'progress' else 'segment'}"
            )
        )
        == 3
    )
    assert {
        element["data-point"]
        for element in svg.select(
            f"{selector}.chart-{chart_type}-{'fill' if chart_type == 'progress' else 'segment'}"
        )
    } == {"Complete", "Remaining", "Blocked"}


def test_donut_svg_uses_magnitude_and_category_color_for_each_ring() -> None:
    model = normalize_chart(
        _canonical_chart(
            chart_type="donut",
            x_axis={"type": "category"},
            series=[
                {
                    "id": "planned",
                    "label": "Planned",
                    "points": [{"x": "A", "y": -2}, {"x": "B", "y": 3}],
                },
                {
                    "id": "actual",
                    "label": "Actual",
                    "points": [{"x": "A", "y": 4}, {"x": "B", "y": 1}],
                },
            ],
        )
    )

    assert model is not None
    svg = BeautifulSoup(render_chart_svg(model), "html.parser")
    assert len(svg.select("path.chart-donut-segment")) == 4
    for series_index, series_id in enumerate(("planned", "actual"), start=1):
        segments = svg.select(f'path.chart-series-{series_index}[data-series="{series_id}"]')
        assert len(segments) == 2
        assert [segment["fill"] for segment in segments] == ["#0369a1", "#0891b2"]


def test_progress_svg_expands_for_high_cardinality_payloads() -> None:
    model = normalize_chart(
        _canonical_chart(
            chart_type="progress",
            x_axis={"type": "category"},
            series=[
                {
                    "id": "steps",
                    "label": "Steps",
                    "points": [{"x": f"Step {index}", "y": index} for index in range(20)],
                }
            ],
        )
    )

    assert model is not None
    svg = BeautifulSoup(render_chart_svg(model), "html.parser").select_one("svg")
    assert svg is not None
    assert float(str(svg["viewbox"]).split()[-1]) >= 640
    fills = svg.select("rect.chart-progress-fill")
    assert len(fills) == 20
    assert len({fill["y"] for fill in fills}) == 20


def test_normalize_chart_deduplicates_points_and_drops_non_string_metadata() -> None:
    model = normalize_chart(
        _canonical_chart(
            x_axis={"type": "category"},
            source_ids=["source-1", 2],
            source=3,
            series=[
                {
                    "id": 4,
                    "label": 5,
                    "points": [{"x": "A", "y": 1}, {"x": "A", "y": 2}],
                }
            ],
        )
    )

    assert model is not None
    assert model.source_ids == ("source-1",)
    assert model.source is None
    assert model.series[0].id == "series-1"
    assert model.series[0].label == "series-1"
    assert [(point.x, point.y) for point in model.series[0].points] == [("A", 2.0)]


def test_normalize_chart_matches_linear_and_strict_time_x_semantics() -> None:
    linear = normalize_chart(
        _canonical_chart(
            x_axis={"type": "linear"},
            series=[
                {
                    "id": "value",
                    "label": "Value",
                    "points": [
                        {"x": 2, "y": 2},
                        {"x": 1, "y": 1},
                        {"x": 1.23452, "y": 4},
                        {"x": 1.23451, "y": 3},
                        {"x": 0.000001, "y": 0},
                    ],
                }
            ],
        )
    )

    assert linear is not None
    assert linear.labels == ("1e-6", "1", "1.23451e0", "1.23452e0", "2")
    assert [point.x for point in linear.series[0].points] == [
        0.000001,
        1.0,
        1.23451,
        1.23452,
        2.0,
    ]
    assert (
        normalize_chart(_canonical_chart(series=[{"id": "bad", "points": [{"x": "2026", "y": 1}]}]))
        is None
    )
    category = normalize_chart(
        _canonical_chart(
            x_axis={"type": "category"},
            series=[
                {
                    "id": "value",
                    "points": [
                        {"x": 1, "y": 1},
                        {"x": 1.0, "y": 2},
                        {"x": 0.000001, "y": 3},
                        {"x": "0.000001", "y": 4},
                    ],
                }
            ],
        )
    )
    assert category is not None
    assert category.labels == ("1", "1e-6", "0.000001")
    assert category.series[0].points[0].y == 2


@pytest.mark.parametrize(
    "block",
    [
        {"type": "chart", "chart_type": "line", "series": []},
        _canonical_chart(spec_version="future"),
        _canonical_chart(chart_type="comparison"),
        _canonical_chart(series=[]),
        _canonical_chart(series=[{"id": "empty", "label": "Empty", "points": []}]),
        _canonical_chart(
            series=[{"id": "bad", "label": "Bad", "points": [{"x": "bad", "y": float("nan")}]}]
        ),
        _canonical_chart(
            x_axis={"type": "time"},
            series=[{"id": "bad", "points": [{"x": "not-time", "y": 1}]}],
        ),
        _canonical_chart(series=[{"id": "bad", "points": [{"x": "20260715", "y": 1}]}]),
        _canonical_chart(series=[{"id": "bad", "points": [{"x": "2026-W29-3", "y": 1}]}]),
    ],
)
def test_normalize_chart_degrades_invalid_or_empty_input(block: dict[str, Any]) -> None:
    assert normalize_chart(block) is None
