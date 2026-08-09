from __future__ import annotations

from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from cognis.channels.delivery import ChannelDeliveryService
from cognis.channels.rich_markdown import (
    RICH_MARKDOWN_MAX_CHARS,
    ProjectionContext,
    register_presentation_projector,
    render_rich_markdown,
    render_text_markdown,
    rich_media_manifest,
)
from cognis.core.deliverable_links import DeliverableViewLink
from cognis.core.events import EventBus
from cognis.models.channel import ChannelCapabilities, OutboundMessage

VIEW_URL = "https://cognis.example.com/d/pulse"
VIEW_LINK = DeliverableViewLink(url=VIEW_URL, public=True)


def _project(
    payload: dict[str, object],
    *,
    markdown: bool = True,
    unicode: bool = True,
    limit: int = 320,
    title: str = "Daily report",
) -> list[str]:
    del markdown, unicode, limit
    return [
        render_rich_markdown(
            payload,
            title=title,
            full_view_link=VIEW_LINK,
            deliverable_id="dlv_projection",
            fallback_text="Fallback must not replace the canonical document.",
        )
    ]


def test_signal_like_markdown_projection_preserves_full_document_and_links() -> None:
    chunks = _project(
        {
            "blocks": [
                {"type": "hero", "title": "Daily report", "subtitle": "Verified briefing"},
                {
                    "type": "section",
                    "title": "Summary",
                    "content": "Read the [primary source](https://source.example/report).",
                },
                {
                    "type": "table",
                    "title": "Metrics",
                    "rows": [
                        {"Metric": "Latency", "Value": "42 ms"},
                        {"Metric": "Errors", "Value": "0"},
                    ],
                },
                {"type": "code", "title": "Check", "language": "sh", "content": "curl /health"},
            ],
            "sources": [
                {
                    "id": "primary",
                    "title": "Primary source",
                    "url": "https://source.example/report",
                }
            ],
        },
        limit=220,
    )

    rendered = "\n\n".join(chunks)
    assert rendered.count("# Daily report") == 1
    assert "## Summary" in rendered
    assert "[primary source](https://source.example/report)" in rendered
    assert "| Metric | Value |" in rendered
    assert "curl /health" in rendered
    assert "## Sources" not in rendered
    assert rendered.count("https://source.example/report") == 1
    assert rendered.endswith(f"[Open full version]({VIEW_URL})")
    assert "_Rich deliverable preview_" not in rendered
    assert '{"blocks"' not in rendered
    assert "Fallback must not replace" not in rendered


def test_canonical_markdown_preserves_link_targets_and_markup() -> None:
    chunks = _project(
        {
            "blocks": [
                {
                    "type": "markdown",
                    "content": "## Finding\n\nUse [the runbook](https://ops.example/runbook).",
                },
                {
                    "type": "figure",
                    "caption": "Service topology",
                    "src": "https://assets.example/topology.png",
                    "source": "Architecture guide",
                    "source_url": "https://docs.example/architecture",
                },
            ]
        },
        markdown=False,
        limit=180,
    )

    rendered = "\n\n".join(chunks)
    assert "[the runbook](https://ops.example/runbook)" in rendered
    assert "[Image](https://assets.example/topology.png)" in rendered
    assert "[Architecture guide](https://docs.example/architecture)" in rendered
    assert rendered.endswith(f"[Open full version]({VIEW_URL})")


def test_canonical_markdown_is_independent_of_channel_message_limits() -> None:
    chunks = _project(
        {
            "blocks": [
                {"type": "section", "title": "Alpha", "content": "~" * 90},
                {"type": "section", "title": "Beta", "content": "^" * 90},
                {"type": "section", "title": "Gamma", "content": "=" * 90},
            ]
        },
        limit=64,
    )

    rendered = "\n".join(chunks)
    assert len(chunks) == 1
    assert rendered.index("## Alpha") < rendered.index("## Beta") < rendered.index("## Gamma")
    assert rendered.count("~") == 90
    assert rendered.count("^") == 90
    assert rendered.count("=") == 90
    assert rendered.endswith(f"[Open full version]({VIEW_URL})")


def test_normal_text_deliverable_is_not_truncated_to_one_preview_message() -> None:
    content = "\n\n".join(f"## Section {index}\n\nBody {index} " + ("x" * 80) for index in range(8))
    chunks = [
        render_text_markdown(
            content,
            title="Long normal document",
            format_name="markdown",
            full_view_link=VIEW_LINK,
            deliverable_id="dlv_text",
        )
    ]

    rendered = "\n\n".join(chunks)
    assert "Section 0" in rendered
    assert "Section 7" in rendered
    assert "truncated" not in rendered
    assert rendered.endswith(f"[Open full version]({VIEW_URL})")


def test_text_and_rich_projection_deduplicate_equivalent_leading_h1() -> None:
    text_chunks = [
        render_text_markdown(
            "# Daily report\n\nBody",
            title="Daily report",
            format_name="markdown",
            full_view_link=VIEW_LINK,
            deliverable_id="dlv-text-title",
        )
    ]
    rich_chunks = _project(
        {
            "blocks": [
                {"type": "markdown", "content": "# Daily report\n\nCanonical body"},
            ]
        },
        title="Daily report",
        limit=200,
    )

    assert "\n".join(text_chunks).count("# Daily report") == 1
    assert "\n".join(rich_chunks).count("# Daily report") == 1


def test_day_agenda_converts_utc_timestamps_to_declared_timezone_across_dst() -> None:
    chunks = _project(
        {
            "blocks": [
                {
                    "type": "day_agenda",
                    "title": "Agenda",
                    "timezone": "Europe/Prague",
                    "items": [
                        {
                            "start": "2026-07-13T07:00:00Z",
                            "end": "2026-07-13T08:00:00Z",
                            "title": "Summer",
                        },
                        {
                            "start": "2026-01-13T08:00:00Z",
                            "end": "2026-01-13T09:00:00Z",
                            "title": "Winter",
                        },
                    ],
                }
            ]
        },
        limit=400,
    )
    rendered = "\n".join(chunks)

    assert "09:00–10:00 — Summer" in rendered
    assert "09:00–10:00 — Winter" in rendered


def test_canonical_markdown_preserves_full_view_url_without_channel_policy() -> None:
    long_url = "https://cognis.example.com/d/view?signature=" + ("x" * 500)
    rendered = render_text_markdown(
        "Short body",
        title="Report",
        format_name="markdown",
        full_view_link=DeliverableViewLink(
            url=long_url,
            public=True,
            stable_url="https://cognis.example.com/d/dlv-long-link",
        ),
        deliverable_id="dlv-long-link",
    )

    assert long_url in rendered


def test_markdown_chunking_preserves_links_code_fences_and_table_structure() -> None:
    chunks = _project(
        {
            "blocks": [
                {
                    "type": "markdown",
                    "content": (
                        "Read [the complete runbook](https://ops.example/runbook) "
                        "before proceeding."
                    ),
                },
                {
                    "type": "code",
                    "language": "python",
                    "content": "\n".join(f"print({index})" for index in range(20)),
                },
                {
                    "type": "table",
                    "rows": [
                        {"Name": f"service-{index}", "State": "healthy"} for index in range(10)
                    ],
                },
            ]
        },
        limit=96,
    )

    rendered = "\n\n".join(chunks)
    assert "[the complete runbook](https://ops.example/runbook)" in rendered
    assert all(chunk.count("```") % 2 == 0 for chunk in chunks)
    table_chunks = [chunk for chunk in chunks if "| Name | State |" in chunk]
    assert len(table_chunks) == 1
    assert all("| --- | --- |" in chunk for chunk in table_chunks)


def test_canonical_markdown_preserves_unicode() -> None:
    chunks = _project(
        {
            "blocks": [
                {
                    "type": "section",
                    "title": "Příliš žluťoučký kůň",
                    "content": "Teplota 18 °C — stabilní ✓",
                }
            ]
        },
        unicode=False,
        limit=300,
    )
    rendered = "\n".join(chunks)

    assert "Příliš žluťoučký kůň" in rendered
    assert "18 °C — stabilní ✓" in rendered


def test_projection_rejects_unsafe_urls_and_escapes_markdown_labels_and_html() -> None:
    chunks = _project(
        {
            "blocks": [
                {
                    "type": "link",
                    "title": "Runbook ](https://attacker.example)",
                    "url": "javascript:alert(1)",
                    "description": "<script>alert(1)</script>",
                }
            ],
            "sources": [
                {
                    "title": "Source [spoof]",
                    "url": "data:text/html,malicious",
                }
            ],
        },
        limit=400,
    )
    rendered = "\n".join(chunks)

    assert "javascript:" not in rendered
    assert "data:text" not in rendered
    assert "\\]" in rendered
    assert "&lt;script>" in rendered
    assert "https://attacker.example" not in rendered


def test_pulse_projection_has_editorial_sections_sources_and_restrained_unicode() -> None:
    chunks = _project(
        {
            "metadata": {"presentation": "pulse"},
            "blocks": [
                {
                    "type": "hero",
                    "eyebrow": "Personal intelligence · 07:10",
                    "title": "Daily Pulse",
                    "subtitle": "Monday · Prague",
                },
                {
                    "type": "grid",
                    "blocks": [
                        {"type": "metric", "label": "Weather", "value": "18 °C"},
                        {"type": "metric", "label": "Markets", "value": "Stable"},
                    ],
                },
                {
                    "type": "columns",
                    "blocks": [
                        {
                            "type": "section",
                            "title": "Main story",
                            "content": "A concrete lead with [evidence](https://news.example/lead).",
                        },
                        {
                            "type": "stack",
                            "title": "Do today",
                            "blocks": [
                                {"type": "card", "title": "Ship review", "content": "Before noon."}
                            ],
                        },
                    ],
                },
                {
                    "type": "day_agenda",
                    "title": "Monday",
                    "timezone": "Europe/Prague",
                    "items": [
                        {"all_day": True, "title": "Release window"},
                        {
                            "start": "2026-07-13T09:30:00+02:00",
                            "end": "2026-07-13T10:00:00+02:00",
                            "title": "Stand-up",
                        },
                    ],
                },
                {
                    "type": "section",
                    "title": "News to know",
                    "blocks": [
                        {"type": "card", "title": "Platform update", "content": "Impact summary."}
                    ],
                },
                {
                    "type": "section",
                    "title": "Watch",
                    "blocks": [
                        {
                            "type": "chart",
                            "title": "USD/CZK",
                            "rows": [{"Day": "Mon", "Value": "21.5"}],
                            "source": "Market data",
                            "source_url": "https://markets.example/data",
                        }
                    ],
                },
                {"type": "callout", "title": "Today's course", "content": "Protect 09:00–12:00."},
                {"type": "source_list", "title": "Sources"},
            ],
            "sources": [
                {"id": "lead", "title": "Lead source", "url": "https://news.example/lead"},
                {"id": "market", "title": "Market data", "url": "https://markets.example/data"},
            ],
        },
        title="Daily Pulse",
        limit=260,
    )

    rendered = "\n\n".join(chunks)
    assert rendered.count("# Daily Pulse") == 1
    assert len(chunks) == 1
    assert f"[Open full version]({VIEW_URL})" in rendered
    assert rendered.endswith(f"[Open full version]({VIEW_URL})")


def test_source_list_resolves_structured_document_source_references() -> None:
    chunks = _project(
        {
            "blocks": [
                {
                    "type": "source_list",
                    "title": "Further reading",
                    "sources": [
                        {
                            "source_id": " sweet ",
                            "label": "Why cats do not taste sweetness",
                        }
                    ],
                },
                {
                    "type": "source_list",
                    "title": "More",
                    "source_ids": [" citation-key "],
                },
                {
                    "type": "source_list",
                    "title": "Alternate URL",
                    "sources": [" https://example.test/alternate "],
                },
            ],
            "sources": [
                {
                    "id": "sweet",
                    "title": "Pseudogenization of a Sweet-Receptor Gene",
                    "url": "https://doi.org/10.1371/journal.pgen.0010003",
                },
                {
                    "citation_id": "citation-key",
                    "title": "Citation-key source",
                    "url": "https://example.test/citation",
                },
                {
                    "id": "dual-url",
                    "title": "Dual URL source",
                    "url": "https://example.test/primary",
                    "href": "https://example.test/alternate",
                },
            ],
        },
        limit=500,
    )

    rendered = "\n".join(chunks)
    assert (
        "[Why cats do not taste sweetness](https://doi.org/10.1371/journal.pgen.0010003)"
    ) in rendered
    assert "[Citation\\-key source](https://example.test/citation)" in rendered
    assert "[Dual URL source](https://example.test/primary)" in rendered
    assert "Source 1" not in rendered


def test_pulse_uses_the_same_canonical_markdown_renderer() -> None:
    chunks = _project(
        {
            "metadata": {"presentation": "pulse"},
            "blocks": [
                {"type": "hero", "title": "Daily Pulse"},
                {
                    "type": "grid",
                    "blocks": [{"type": "metric", "label": "Weather", "value": "18 C"}],
                },
            ],
        },
        title="Daily Pulse",
        markdown=False,
        unicode=False,
    )
    rendered = "\n".join(chunks)
    assert "Weather: 18 C" in rendered
    assert "Signals" not in rendered


@pytest.mark.parametrize(
    ("block", "expected"),
    [
        (
            {"type": "tabs", "items": [{"type": "card", "title": "Tab item", "content": "tab"}]},
            "Tab item",
        ),
        (
            {"type": "accordion", "items": [{"type": "card", "title": "Fold", "content": "fold"}]},
            "Fold",
        ),
        (
            {"type": "modal", "items": [{"type": "card", "title": "Dialog", "content": "dialog"}]},
            "Dialog",
        ),
        (
            {"type": "gallery", "items": [{"type": "figure", "caption": "Gallery image"}]},
            "Gallery image",
        ),
        ({"type": "kv", "items": [{"label": "Region", "value": "EU"}]}, "Region: EU"),
        ({"type": "timeline", "items": [{"time": "09:00", "title": "Started"}]}, "09:00 — Started"),
        (
            {
                "type": "incident_checklist",
                "title": "Incident",
                "actions": [{"title": "Mitigate", "done": True}],
            },
            "[x] Mitigate",
        ),
        ({"type": "quote", "content": "Quoted evidence", "author": "Operator"}, "Quoted evidence"),
        ({"type": "mermaid", "source": "graph TD; A-->B"}, "graph TD; A-->B"),
        ({"type": "mermaid", "code": "flowchart LR; A-->B"}, "flowchart LR; A-->B"),
        (
            {"type": "link_preview", "title": "Runbook", "url": "https://ops.example"},
            "[Runbook](https://ops.example)",
        ),
        (
            {
                "type": "research_answer",
                "paragraphs": [{"text": "Research finding", "citations": ["source-1"]}],
            },
            "Research finding",
        ),
        (
            {
                "type": "evidence_report",
                "claims": [{"title": "Supported claim", "confidence": 0.9}],
            },
            "Confidence: 90%",
        ),
    ],
)
def test_generic_renderer_covers_specialized_block_families(
    block: dict[str, object],
    expected: str,
) -> None:
    chunks = _project(
        {
            "blocks": [block],
            "sources": [
                {
                    "id": "source-1",
                    "title": "Evidence source",
                    "url": "https://evidence.example",
                }
            ],
        },
        limit=500,
    )
    assert expected in "\n".join(chunks)


def test_oversized_document_uses_only_total_safety_limit_with_explicit_notice() -> None:
    payload = {
        "blocks": [
            {"type": "markdown", "title": f"Chapter {index}", "content": str(index) * 15_000}
            for index in range(10)
        ]
    }
    chunks = _project(payload, limit=1000)
    rendered = "\n\n".join(chunks)

    assert "Chapter 0" in rendered
    assert "Chapter 9" not in rendered
    assert "Some sections were omitted because this document exceeds" in rendered
    assert rendered.endswith(f"[Open full version]({VIEW_URL})")
    assert sum(len(chunk) for chunk in chunks) <= RICH_MARKDOWN_MAX_CHARS + len(VIEW_URL) + 200


def test_presentation_registry_can_override_projection_without_delivery_changes() -> None:
    class _CustomProjector:
        def project(
            self,
            payload: dict[str, object],
            *,
            title: str,
            context: ProjectionContext,
        ) -> list[str]:
            del payload, context
            return [f"Custom projection: {title}"]

    register_presentation_projector("test-presentation", _CustomProjector())
    chunks = _project(
        {"metadata": {"presentation": "test-presentation"}, "blocks": []},
        title="Registry seam",
    )
    assert chunks[0].startswith("Custom projection: Registry seam")


@pytest.mark.asyncio
async def test_delivery_sends_each_projected_chunk_exactly_once_without_duplicate_title(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _Adapter:
        capabilities = ChannelCapabilities(supports_markdown=True, max_message_length=90)

        def __init__(self) -> None:
            self.sent: list[OutboundMessage] = []

        async def send_message(self, message: OutboundMessage) -> str:
            self.sent.append(message)
            return f"message-{len(self.sent)}"

    adapter = _Adapter()

    class _Manager:
        _artifact_store = object()

        def find_adapter_for_channel(
            self, channel_type: str, account_id: str
        ) -> tuple[object, object]:
            return adapter, object()

    @asynccontextmanager
    async def session_factory() -> object:
        yield object()

    row = SimpleNamespace(
        deliverable_id="dlv_once",
        title="Exact once report",
        format="rich",
        content="fallback",
        rich_payload=None,
    )

    async def hydrate(target: object, artifact_store: object) -> object:
        del artifact_store
        target.rich_payload = {
            "blocks": [
                {"type": "hero", "title": "Exact once report"},
                {"type": "section", "title": "One", "content": "a" * 100},
                {"type": "section", "title": "Two", "content": "b" * 100},
            ]
        }
        return target

    monkeypatch.setattr("cognis.store.queries.get_deliverable", AsyncMock(return_value=row))
    monkeypatch.setattr("cognis.store.deliverable_storage.hydrate_deliverable_payload", hydrate)
    service = ChannelDeliveryService(
        session_factory=session_factory,
        event_bus=EventBus(),
        channel_manager_ref=lambda: _Manager(),
        public_base_url="https://cognis.example.com",
    )
    monkeypatch.setattr(
        service,
        "_deliverable_view_link",
        lambda artifact_store, deliverable_id: VIEW_LINK,
    )

    status = await service._send_to_route(  # noqa: SLF001
        channel_type="matrix",
        account_id="account",
        chat_id="room",
        thread_id=None,
        content="Notification fallback",
        deliverable_id="dlv_once",
    )

    rendered = "\n\n".join(message.content for message in adapter.sent)
    assert status == "sent"
    assert len(adapter.sent) == len({message.content for message in adapter.sent})
    assert all(len(message.content) <= 90 for message in adapter.sent)
    assert rendered.count("# Exact once report") == 1
    assert rendered.count("## One") == 1
    assert rendered.count("## Two") == 1
    assert "a" * 100 in rendered.replace("\n\n", "")
    assert "b" * 100 in rendered.replace("\n\n", "")
    assert rendered.count("[Open full version]") == 1
    assert "Notification fallback" not in rendered
    assert all(
        message.platform_data.get("canonical_rich_markdown") is True for message in adapter.sent
    )


def test_channel_chart_uses_concise_trend_instead_of_full_table() -> None:
    rendered = "\n".join(
        _project(
            {
                "blocks": [
                    {
                        "type": "chart",
                        "title": "Availability",
                        "description": "Three day availability.",
                        "spec_version": "cognis.chart.v1",
                        "chart_type": "line",
                        "series": [
                            {
                                "id": "availability",
                                "label": "Availability",
                                "points": [
                                    {"x": "Mon", "y": 98},
                                    {"x": "Tue", "y": 99},
                                    {"x": "Wed", "y": 99.5},
                                ],
                            }
                        ],
                        "x_axis": {"type": "category"},
                        "y_axis": {"type": "linear"},
                        "source": "SLO export",
                        "observed_at": "2026-07-14T08:00:00Z",
                    }
                ]
            },
            limit=500,
        )
    )

    assert "Trend: Availability: 98 → 99.5 (up 1.5)" in rendered
    assert "| Label |" not in rendered
    assert "SLO export" in rendered
    assert "2026-07-14T08:00:00Z" in rendered


def test_channel_media_manifest_does_not_render_attachment_status_text() -> None:
    payload = {
        "media_manifest": {
            "station": {
                "artifact_ref": "att_station",
                "filename": "station.png",
                "mime_type": "image/png",
            }
        },
        "assets": [
            {
                "id": "station",
                "artifact_id": "att_station",
                "mime_type": "image/png",
                "filename": "station.png",
            }
        ],
        "blocks": [
            {
                "type": "figure",
                "asset_id": "station",
                "alt": "Station overview",
                "caption": "Latest station view.",
            }
        ],
    }

    assert rich_media_manifest(payload) == [
        {
            "artifact_id": "att_station",
            "media_key": "station",
            "filename": "station.png",
            "mime_type": "image/png",
            "alt": "Station overview",
            "media_ref": "station",
            "safe_image_only": True,
        }
    ]
    unmaterialized = "\n".join(_project(payload, limit=500))
    materialized = render_rich_markdown(
        payload,
        title="Daily report",
        full_view_link=VIEW_LINK,
        deliverable_id="dlv_projection",
        fallback_text="fallback",
    )

    assert "<!--cognis-rich-media:station:Station overview-->" in unmaterialized
    assert "<!--cognis-rich-media:station:Station overview-->" in materialized
    assert "Image: Station overview" not in materialized
    assert "Attached image" not in materialized
    assert "Artifact ref" not in materialized


def test_rich_media_manifest_normalizes_persisted_asset_only_payloads() -> None:
    payload = {
        "assets": [
            {
                "id": "station",
                "artifact_id": "att_station",
                "mime_type": "image/png",
                "filename": "station.png",
            }
        ],
        "blocks": [{"type": "figure", "asset_id": "station", "alt": "Station overview"}],
    }

    assert rich_media_manifest(payload) == [
        {
            "artifact_id": "att_station",
            "filename": "station.png",
            "mime_type": "image/png",
            "alt": "Station overview",
            "media_ref": "station",
            "safe_image_only": True,
        }
    ]


def test_channel_cards_project_dek_icon_href_tone_and_source_ids_accessibly() -> None:
    rendered = "\n".join(
        _project(
            {
                "sources": [
                    {"id": "story", "title": "Primary report", "url": "https://news.example/story"}
                ],
                "blocks": [
                    {
                        "type": "card",
                        "variant": "editorial",
                        "tone": "positive",
                        "emoji": "🛰️",
                        "title": "Satellite update",
                        "dek": "The concise standfirst.",
                        "content": "Operational impact.",
                        "href": "https://news.example/story",
                        "source_ids": ["story"],
                    }
                ],
            },
            limit=500,
        )
    )

    assert "🛰️" in rendered
    assert "The concise standfirst." in rendered
    assert "[Read more](https://news.example/story)" in rendered
    assert rendered.count("https://news.example/story") == 1
    assert "Sources:" not in rendered


def test_generic_editorial_cards_deduplicate_links_sources_and_keep_media_anchors() -> None:
    first_url = "https://news.example/first"
    second_url = "https://news.example/second"
    payload = {
        "sources": [
            {"id": "first", "title": "First source", "url": first_url},
            {"id": "second", "title": "Second source", "url": second_url},
        ],
        "assets": [
            {"id": "first-image", "artifact_id": "img_first", "mime_type": "image/jpeg"},
            {"id": "second-image", "artifact_id": "img_second", "mime_type": "image/jpeg"},
        ],
        "blocks": [
            {
                "type": "card",
                "title": "First article",
                "content": f"First summary. [Read article]({first_url})",
                "href": first_url,
                "source_ids": ["first"],
                "media": {"ref": "first-image", "alt": "First photo"},
            },
            {
                "type": "card",
                "title": "Second article",
                "content": "Second summary.",
                "href": second_url,
                "source_ids": ["second"],
                "media": {"ref": "second-image", "alt": "Second photo"},
            },
            {"type": "source_list"},
        ],
    }

    rendered = render_rich_markdown(
        payload,
        title="Editorial report",
        full_view_link=VIEW_LINK,
        deliverable_id="dlv_editorial",
        fallback_text="fallback",
    )

    assert rendered.count(first_url) == 1
    assert rendered.count(second_url) == 1
    assert "Sources:" not in rendered
    assert "## Sources" not in rendered
    first_marker = "<!--cognis-rich-media:first-image:First photo-->"
    second_marker = "<!--cognis-rich-media:second-image:Second photo-->"
    assert rendered.index("First summary.") < rendered.index(first_marker)
    assert rendered.index(first_marker) < rendered.index("Second article")
    assert rendered.index("Second summary.") < rendered.index(second_marker)


def test_source_list_before_card_does_not_duplicate_the_card_primary_link() -> None:
    url = "https://news.example/story"
    rendered = render_rich_markdown(
        {
            "sources": [{"id": "story", "title": "Story source", "url": url}],
            "blocks": [
                {"type": "source_list"},
                {
                    "type": "card",
                    "title": "Story",
                    "content": "Summary.",
                    "href": url,
                    "source_ids": ["story"],
                },
            ],
        },
        title="Report",
        full_view_link=VIEW_LINK,
        deliverable_id="dlv_order",
        fallback_text="fallback",
    )

    assert rendered.count(url) == 1
    assert "[Read more]" in rendered
    assert "## Sources" not in rendered


def test_rich_media_marker_normalizes_multiline_alt_text() -> None:
    rendered = render_rich_markdown(
        {
            "assets": [{"id": "image", "artifact_id": "img_1", "mime_type": "image/png"}],
            "blocks": [
                {
                    "type": "figure",
                    "media": {"ref": "image", "alt": "First line\nSecond line"},
                }
            ],
        },
        title="Report",
        full_view_link=VIEW_LINK,
        deliverable_id="dlv_alt",
        fallback_text="fallback",
    )

    assert "<!--cognis-rich-media:image:First line Second line-->" in rendered


def test_channel_chart_preserves_canonical_multi_series() -> None:
    rendered = "\n".join(
        _project(
            {
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
                                "points": [{"x": "Mon", "y": 3}, {"x": "Tue", "y": 1}],
                            },
                        ],
                        "x_axis": {"type": "category"},
                        "y_axis": {"type": "linear"},
                    }
                ]
            },
            limit=500,
        )
    )

    assert "Requests: 10 → 20 (up 10)" in rendered
    assert "Errors: 3 → 1 (down 2)" in rendered
    assert "| Label |" not in rendered


@pytest.mark.parametrize(
    ("axis_type", "points"),
    [
        (
            "time",
            [
                {"x": "2026-07-15T12:00:00Z", "y": 30},
                {"x": "2026-07-13T12:00:00Z", "y": 10},
                {"x": "2026-07-14T12:00:00Z", "y": 20},
            ],
        ),
        (
            "linear",
            [
                {"x": 3, "y": 30},
                {"x": 1, "y": 10},
                {"x": 2, "y": 20},
            ],
        ),
        (
            "category",
            [
                {"x": "Wed", "y": 30},
                {"x": "Mon", "y": 10},
                {"x": "Tue", "y": 20},
            ],
        ),
    ],
)
def test_channel_chart_trend_uses_canonical_axis_ordering(
    axis_type: str,
    points: list[dict[str, object]],
) -> None:
    rendered = "\n".join(
        _project(
            {
                "blocks": [
                    {
                        "type": "chart",
                        "spec_version": "cognis.chart.v1",
                        "chart_type": "line",
                        "series": [{"id": "requests", "label": "Requests", "points": points}],
                        "x_axis": {"type": axis_type},
                        "y_axis": {"type": "linear"},
                    }
                ]
            },
            limit=500,
        )
    )

    expected = (
        "Requests: 30 → 20 (down 10)" if axis_type == "category" else "Requests: 10 → 30 (up 20)"
    )
    assert f"Trend: {expected}" in rendered
    assert "| Label |" not in rendered


@pytest.mark.parametrize(
    ("end_value", "expected"),
    [
        ([8, 18], "down 2–2"),
        ([10, 20], "flat 0–0"),
        ([8, 22], "mixed 2–2"),
    ],
)
def test_channel_chart_trend_describes_range_changes_conservatively(
    end_value: list[int],
    expected: str,
) -> None:
    rendered = "\n".join(
        _project(
            {
                "blocks": [
                    {
                        "type": "chart",
                        "spec_version": "cognis.chart.v1",
                        "chart_type": "range",
                        "series": [
                            {
                                "id": "forecast",
                                "label": "Forecast",
                                "points": [
                                    {"x": "Mon", "y": [10, 20]},
                                    {"x": "Tue", "y": end_value},
                                ],
                            }
                        ],
                        "x_axis": {"type": "category"},
                        "y_axis": {"type": "linear"},
                    }
                ]
            },
            limit=500,
        )
    )

    assert f"Trend: Forecast: 10–20 → {end_value[0]}–{end_value[1]} ({expected})" in rendered


def test_channel_chart_trend_caps_series_and_preserves_range_values() -> None:
    rendered = "\n".join(
        _project(
            {
                "blocks": [
                    {
                        "type": "chart",
                        "spec_version": "cognis.chart.v1",
                        "chart_type": "range",
                        "series": [
                            {
                                "id": f"series-{index}",
                                "label": f"Series {index}",
                                "points": [
                                    {"x": "Mon", "y": [index, index + 10]},
                                    {"x": "Tue", "y": [index + 2, index + 12]},
                                ],
                            }
                            for index in range(4)
                        ],
                        "x_axis": {"type": "category"},
                        "y_axis": {"type": "linear"},
                    }
                ]
            },
            limit=500,
        )
    )

    assert "Series 0: 0–10 → 2–12 (up 2–2)" in rendered
    assert "Series 2: 2–12 → 4–14 (up 2–2)" in rendered
    assert "Series 3" not in rendered
    assert "| Label |" not in rendered


def test_channel_chart_trend_marks_degraded_single_point_series_without_direction() -> None:
    rendered = "\n".join(
        _project(
            {
                "blocks": [
                    {
                        "type": "chart",
                        "spec_version": "cognis.chart.v1",
                        "chart_type": "line",
                        "series": [
                            {
                                "id": "latency",
                                "label": "Latency",
                                "points": [
                                    {"x": "invalid", "y": "not-a-number"},
                                    {"x": "only-valid-point", "y": 42},
                                ],
                            }
                        ],
                        "x_axis": {"type": "category"},
                        "y_axis": {"type": "linear"},
                    }
                ]
            },
            limit=500,
        )
    )

    assert "Trend: Latency: 42 (single point)" in rendered
    assert "up" not in rendered
    assert "down" not in rendered
    assert "| Label |" not in rendered
