from __future__ import annotations

import json
import re
from collections.abc import Callable
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from bs4 import BeautifulSoup
from sqlalchemy import func, select

from cognis.artifacts.store import ArtifactStore, ArtifactStoreConfig
from cognis.core.system_skills import get_system_skill_default
from cognis.models.deliverable import (
    PULSE_DAILY_SKELETON,
    PULSE_PRESENTATION_DESCRIPTOR,
    PULSE_V1_DAILY_SKELETON,
    RICH_DELIVERABLE_MAX_BLOCKS,
    RichPayloadValidationError,
    normalize_required_rich_payload,
    normalize_rich_payload,
    pulse_quality_metadata,
    rich_render_metadata,
)
from cognis.rendering.deliverables import render_pdf_bytes, render_standalone_html
from cognis.store.database import create_engine, create_session_factory
from cognis.store.models import Agent, Base, Conversation, DeliverableRow, User
from cognis.store.queries import create_deliverable
from cognis.tools.builtin.workflow import WRITE_DELIVERABLE_TOOL
from cognis.tools.introspection import (
    audit_native_tool_domains,
    describe_available_tool,
    validate_available_tool_call,
    validate_available_tool_call_with_context,
)
from cognis.tools.native_validation import NativeValidationContext

_FIXTURES = Path(__file__).parents[1] / "fixtures" / "pulse"


def _fixture(name: str) -> dict[str, object]:
    return json.loads((_FIXTURES / name).read_text())


def _item_chain(container_type: str, count: int) -> dict[str, object]:
    root: dict[str, object] = {"type": container_type, "items": []}
    current = root
    for _ in range(count - 1):
        child: dict[str, object] = {"type": container_type, "items": []}
        current["items"] = [child]
        current = child
    return root


class _ReplayAuthoringAgent:
    """Deterministic model stand-in that authors from live contract + collector JSON."""

    def author(
        self,
        *,
        collectors: list[dict[str, Any]],
        descriptor: dict[str, Any],
        system_skill: dict[str, object],
    ) -> dict[str, Any]:
        instructions = str(system_skill["instructions"])
        assert "metadata.pulse_version=2" in instructions
        payload = deepcopy(descriptor["valid_skeleton"])
        by_name = {collector["collector"]: collector for collector in collectors}
        agenda_items = by_name["agenda"]["items"]
        valid_agenda = [
            item for item in agenda_items if item["timestamp"] != item["metadata"].get("end")
        ]
        payload["blocks"][2]["date"] = "2026-07-14"
        payload["blocks"][2]["timezone"] = "Europe/Prague"
        payload["blocks"][2]["now"] = "2026-07-14T08:00:00+02:00"
        payload["blocks"][2]["items"] = [
            {
                "title": item["title"],
                "start": item["timestamp"],
                "end": item["metadata"]["end"],
                "source_id": item["source_id"],
            }
            for item in valid_agenda
        ]

        actionable_mail = [
            item for item in by_name["gmail"]["items"] if item["metadata"].get("actionable") is True
        ]
        payload["blocks"][3]["blocks"][0].update(
            {
                "title": actionable_mail[0]["title"],
                "answer": actionable_mail[0]["summary"],
                "source_ids": [actionable_mail[0]["source_id"]],
            }
        )
        payload["blocks"][3]["blocks"][1]["blocks"][0]["content"] = (
            "Review the OAuth client inventory before opening lower-value inbox items."
        )
        infrastructure_errors = [
            error
            for collector in collectors
            for error in collector["errors"]
            if error["kind"] == "infrastructure_error"
        ]
        if infrastructure_errors:
            payload["blocks"][3]["blocks"][1]["blocks"][1]["content"] = (
                "Unavailable: Todoist was omitted because the executor transport failed."
            )
            payload["blocks"][3]["blocks"][1]["blocks"][1]["status"] = "unavailable"

        news_ai = by_name["news-ai"]["items"]
        news = [item for item in news_ai if item["kind"] == "news"]
        ai = [item for item in news_ai if item["kind"] == "ai"]
        for accordion, items in zip(payload["blocks"][4]["blocks"], (news, ai), strict=True):
            accordion["items"] = [
                {
                    "type": "card",
                    "title": item["title"],
                    "content": f"{item['summary']} [Zdroj]({item['source_url']}).",
                    "source_id": item["source_id"],
                    "url": item["source_url"],
                }
                for item in items
            ]

        weather = by_name["weather"]
        chart = payload["blocks"][5]["blocks"][0]
        chart.update(
            {
                "title": "Lovosice temperature",
                "series": [
                    {
                        "id": "temperature",
                        "label": "Temperature",
                        "points": [
                            {"x": item["title"], "y": item["value"]} for item in weather["items"]
                        ],
                    }
                ],
                "source_ids": [weather["sources"][0]["id"]],
                "source": weather["sources"][0]["title"],
                "source_url": weather["sources"][0]["url"],
                "observed_at": weather["observed_at"],
            }
        )

        selected_source_ids = {
            valid_agenda[0]["source_id"],
            actionable_mail[0]["source_id"],
            *(item["source_id"] for item in news_ai),
            weather["sources"][0]["id"],
        }
        all_sources = {
            source["id"]: source for collector in collectors for source in collector["sources"]
        }
        payload["sources"] = [
            {**all_sources[source_id], "number": number}
            for number, source_id in enumerate(sorted(selected_source_ids), start=1)
        ]
        return {
            "action": "rich:pulse",
            "content": (
                "Priorita: prověřit nepoužívané OAuth klienty. "
                "Todoist byl vynechán kvůli výpadku executor infrastruktury."
            ),
            "format": "rich",
            "title": "Ranní pulse",
            "rich": payload,
        }


def test_collector_json_acceptance_fixture_produces_valid_daily_payload() -> None:
    collectors = _fixture("daily_collectors.json")["collectors"]
    arguments = _fixture("daily_write_arguments.json")

    assert isinstance(collectors, list)
    for collector in collectors:
        assert set(collector) == {
            "collector",
            "status",
            "observed_at",
            "items",
            "sources",
            "errors",
        }
        assert collector["status"] in {"ok", "partial", "unavailable"}
        assert isinstance(collector["items"], list)
        assert isinstance(collector["sources"], list)
        assert isinstance(collector["errors"], list)

    collector_source_ids = {
        source["id"] for collector in collectors for source in collector["sources"]
    }
    payload_source_ids = {source["id"] for source in arguments["rich"]["sources"]}
    assert payload_source_ids == collector_source_ids
    assert arguments["rich"]["blocks"][2]["items"][0]["title"] == collectors[0]["items"][0]["title"]

    payload, warnings = normalize_rich_payload(arguments["rich"])
    assert warnings == []
    assert payload is not None
    assert payload["metadata"] == {"presentation": "pulse", "pulse_variant": "daily"}
    assert rich_render_metadata(payload)["pulse_valid"] is True
    assert rich_render_metadata(payload)["pulse_variant"] == "daily"


@pytest.mark.asyncio
async def test_agent_authored_pulse_v2_accepts_production_collector_replay() -> None:
    replay = _fixture("production_collector_replay.json")
    collectors = replay["collectors"]
    assert len(next(item for item in collectors if item["collector"] == "gmail")["items"]) == 15
    descriptor = describe_available_tool([WRITE_DELIVERABLE_TOOL], "write_deliverable")[
        "descriptor"
    ]["extensions"]["presentation_contracts"]["rich:pulse"]
    system_skill = get_system_skill_default("cognis-pulse-deliverable")
    assert system_skill is not None

    arguments = _ReplayAuthoringAgent().author(
        collectors=collectors,
        descriptor=descriptor,
        system_skill=system_skill,
    )
    validated = await validate_available_tool_call_with_context(
        [WRITE_DELIVERABLE_TOOL],
        "write_deliverable",
        arguments,
        None,
    )
    payload, warnings = normalize_rich_payload(arguments["rich"])

    assert validated["valid"] is True, validated["errors"]
    assert warnings == []
    assert payload is not None
    assert payload["metadata"]["pulse_version"] == 2
    agenda_titles = {item["title"] for item in payload["blocks"][2]["items"]}
    assert "Malformed zero-duration candidate" not in agenda_titles
    weather = next(item for item in collectors if item["collector"] == "weather")
    chart = payload["blocks"][5]["blocks"][0]
    assert chart["series"] == [
        {
            "id": "temperature",
            "label": "Temperature",
            "points": [{"x": item["title"], "y": item["value"]} for item in weather["items"]],
        }
    ]
    assert chart["source_ids"] == [weather["sources"][0]["id"]]
    assert chart["observed_at"] == weather["observed_at"]
    serialized = json.dumps(payload)
    assert "Review unused OAuth clients" in serialized
    assert "Summer promotion" not in serialized
    assert serialized.count("Unavailable") == 1
    quality = rich_render_metadata(payload)["pulse_quality"]
    assert quality["quality_gate_passed"] is True
    assert quality["visual_count"] >= 1
    assert quality["meaningful_chart_count"] == 1
    assert quality["uncited_story_count"] == 0
    assert quality["collapsible_count"] == 2
    assert quality["unavailable_count"] == 1


def test_agent_authored_acceptance_validates_explicit_generic_fallback() -> None:
    arguments = {
        "action": "write_deliverable",
        "content": "Zdrojová data nestačila pro Pulse; zde je přístupný souhrn.",
        "format": "rich",
        "rich": {
            "blocks": [
                {
                    "type": "research_answer",
                    "title": "Degraded daily brief",
                    "answer": "Only verified source data is included.",
                },
                {
                    "type": "source_list",
                    "title": "Sources",
                },
            ],
            "sources": [],
            "metadata": {},
        },
    }

    validated = validate_available_tool_call(
        [WRITE_DELIVERABLE_TOOL],
        "write_deliverable",
        arguments,
    )
    payload, warnings = normalize_rich_payload(arguments["rich"])

    assert validated["valid"] is True
    assert validated["operation"] == "write_deliverable"
    assert warnings == []
    assert payload is not None
    assert payload["metadata"] == {}


def test_descriptor_example_and_live_validator_use_the_same_pulse_contract() -> None:
    described = describe_available_tool([WRITE_DELIVERABLE_TOOL], "write_deliverable")
    operation = next(
        item for item in described["descriptor"]["operations"] if item["operation"] == "rich:pulse"
    )
    example = operation["examples"][0]

    assert (
        described["descriptor"]["extensions"]["presentation_contracts"]["rich:pulse"]
        == PULSE_PRESENTATION_DESCRIPTOR
    )
    assert example["rich"] == PULSE_DAILY_SKELETON
    validated = validate_available_tool_call([WRITE_DELIVERABLE_TOOL], "write_deliverable", example)
    assert validated["valid"] is True
    assert validated["operation"] == "rich:pulse"
    payload, _warnings = normalize_rich_payload(example["rich"])
    assert payload is not None
    metadata = rich_render_metadata(payload)
    assert metadata["pulse_schema"] == "cognis.rich.pulse.v2"
    assert metadata["pulse_quality"]["quality_gate_passed"] is True
    assert metadata["pulse_quality"] == {
        **pulse_quality_metadata(payload),
        "quality_gate_passed": True,
    }


def test_pulse_v2_counts_a_canonical_multiseries_chart_once() -> None:
    payload = deepcopy(PULSE_DAILY_SKELETON)
    chart = payload["blocks"][5]["blocks"][0]
    chart["series"] = [
        {
            "id": "requests",
            "label": "Requests",
            "points": [{"x": "T-2", "y": 10}, {"x": "T-1", "y": 12}],
        },
        {
            "id": "errors",
            "label": "Errors",
            "points": [{"x": "T-1", "y": 2}, {"x": "Now", "y": 1}],
        },
    ]

    normalized, warnings = normalize_rich_payload(payload)

    assert warnings == []
    assert normalized is not None
    quality = rich_render_metadata(normalized)["pulse_quality"]
    assert quality["quality_gate_passed"] is True
    assert quality["meaningful_chart_count"] == 1


def test_pulse_v2_allows_one_degraded_signal_and_reports_it() -> None:
    payload = deepcopy(PULSE_DAILY_SKELETON)
    payload["blocks"][3]["blocks"][1]["blocks"][1]["degraded_data"] = True

    normalized, warnings = normalize_rich_payload(payload)

    assert warnings == []
    assert normalized is not None
    quality = rich_render_metadata(normalized)["pulse_quality"]
    assert quality["quality_gate_passed"] is True
    assert quality["unavailable_count"] == 1


def test_pulse_v2_rejects_renderer_invalid_canonical_range_points() -> None:
    payload = deepcopy(PULSE_DAILY_SKELETON)
    payload["blocks"][5]["blocks"][0]["chart_type"] = "range"

    with pytest.raises(RichPayloadValidationError) as exc_info:
        normalize_rich_payload(payload)

    assert any(issue["reason"] == "invalid_chart_point_y" for issue in exc_info.value.issues)


def test_persisted_pulse_v1_remains_valid_and_reports_legacy_schema() -> None:
    payload, warnings = normalize_rich_payload(deepcopy(PULSE_V1_DAILY_SKELETON))

    assert warnings == []
    assert payload is not None
    metadata = rich_render_metadata(payload)
    assert metadata["pulse_valid"] is True
    assert metadata["pulse_schema"] == "cognis.rich.pulse.v1"
    assert metadata["pulse_version"] == 1
    assert "pulse_quality" not in metadata


def test_pulse_v2_generic_standalone_fallback_keeps_story_links_and_source_citations() -> None:
    payload, _warnings = normalize_rich_payload(deepcopy(PULSE_DAILY_SKELETON))
    assert payload is not None
    rendered = render_standalone_html(
        SimpleNamespace(
            title="Pulse v2",
            format="rich",
            content="Accessible fallback.",
            rich_payload=payload,
        )
    )

    soup = BeautifulSoup(rendered, "html.parser")
    assert len(soup.select(".block-accordion")) == 2
    assert soup.find("a", href="https://news.example.org/story") is not None
    assert soup.find("a", href="https://ai.example.org/change") is not None
    assert "News source" in soup.get_text()
    assert "AI source" in soup.get_text()


@pytest.mark.asyncio
async def test_registered_write_deliverable_examples_pass_domain_audit() -> None:
    assert (
        await audit_native_tool_domains(
            [WRITE_DELIVERABLE_TOOL],
            NativeValidationContext(),
        )
        == []
    )


def test_generic_operation_cannot_claim_pulse_presentation() -> None:
    arguments = _fixture("daily_write_arguments.json")
    arguments["action"] = "write_deliverable"

    validated = validate_available_tool_call(
        [WRITE_DELIVERABLE_TOOL],
        "write_deliverable",
        arguments,
    )

    assert validated["valid"] is False
    assert validated["operation"] == "write_deliverable"


@pytest.mark.asyncio
async def test_validate_tool_call_reports_the_same_pulse_issue_paths_as_write() -> None:
    arguments = {
        "action": "rich:pulse",
        "content": "Accessible fallback.",
        "format": "rich",
        "rich": deepcopy(PULSE_DAILY_SKELETON),
    }
    arguments["rich"]["blocks"].pop()

    validated = await validate_available_tool_call_with_context(
        [WRITE_DELIVERABLE_TOOL], "write_deliverable", arguments, None
    )
    with pytest.raises(RichPayloadValidationError) as exc_info:
        normalize_rich_payload(arguments["rich"])

    assert validated["valid"] is False
    assert validated["operation"] == "rich:pulse"
    assert {(error["code"], error["path"]) for error in validated["errors"]} == {
        (issue["reason"], f"$.rich{issue['path'][1:]}") for issue in exc_info.value.issues
    }


@pytest.mark.asyncio
async def test_daily_brief_v12_is_not_silently_upgraded_to_v13_contract() -> None:
    arguments = {
        "action": "rich:pulse",
        "content": "Accessible fallback.",
        "format": "rich",
        "rich": deepcopy(PULSE_DAILY_SKELETON),
    }
    v12 = await validate_available_tool_call_with_context(
        [WRITE_DELIVERABLE_TOOL],
        "write_deliverable",
        arguments,
        NativeValidationContext(task_description="Use daily_brief_v12."),
    )
    v13 = await validate_available_tool_call_with_context(
        [WRITE_DELIVERABLE_TOOL],
        "write_deliverable",
        arguments,
        NativeValidationContext(task_description="Use daily_brief_v13."),
    )

    assert v12["valid"] is True
    assert v13["valid"] is False
    assert any(error["code"] == "invalid_daily_brief" for error in v13["errors"])


def test_standard_pulse_allows_optional_context_without_daily_slots() -> None:
    payload = deepcopy(PULSE_V1_DAILY_SKELETON)
    payload["metadata"].pop("pulse_variant")
    payload["blocks"].pop(2)

    normalized, warnings = normalize_rich_payload(payload)

    assert warnings == []
    assert normalized["metadata"] == {"presentation": "pulse"}
    assert all(block["type"] != "day_agenda" for block in normalized["blocks"])


def test_conflicting_children_cannot_hide_missing_rendered_metrics() -> None:
    payload = deepcopy(PULSE_V1_DAILY_SKELETON)
    payload["blocks"][1]["children"] = payload["blocks"][1]["blocks"]
    payload["blocks"][1]["blocks"] = []

    with pytest.raises(RichPayloadValidationError) as exc_info:
        normalize_rich_payload(payload)

    assert any(issue["path"] == "$.blocks[1]" for issue in exc_info.value.issues)


def test_nested_second_hero_is_rejected() -> None:
    payload = deepcopy(PULSE_DAILY_SKELETON)
    payload["blocks"][4]["blocks"].append({"type": "hero", "title": "Nested hero"})

    with pytest.raises(RichPayloadValidationError) as exc_info:
        normalize_rich_payload(payload)

    assert any(issue["path"] == "$.blocks[0]" for issue in exc_info.value.issues)


def test_nested_second_hero_inside_primary_hero_is_rejected() -> None:
    payload = deepcopy(PULSE_DAILY_SKELETON)
    payload["blocks"][0]["blocks"] = [{"type": "hero", "title": "Nested hero"}]

    with pytest.raises(RichPayloadValidationError) as exc_info:
        normalize_rich_payload(payload)

    assert any(issue["path"] == "$.blocks[0]" for issue in exc_info.value.issues)


@pytest.mark.parametrize("container_type", ["tabs", "accordion", "modal", "gallery"])
def test_item_container_cannot_hide_nested_second_hero(container_type: str) -> None:
    payload = deepcopy(PULSE_DAILY_SKELETON)
    payload["blocks"][4]["blocks"].append(
        {
            "type": container_type,
            "items": [{"type": "hero", "title": "Nested item hero"}],
        }
    )

    with pytest.raises(RichPayloadValidationError) as exc_info:
        normalize_rich_payload(payload)

    assert any(issue["path"] == "$.blocks[0]" for issue in exc_info.value.issues)


def test_null_blocks_uses_renderer_children_precedence() -> None:
    payload = deepcopy(PULSE_DAILY_SKELETON)
    metrics = payload["blocks"][1]["blocks"]
    payload["blocks"][1]["blocks"] = None
    payload["blocks"][1]["children"] = metrics

    normalized, warnings = normalize_rich_payload(payload)

    assert warnings == []
    assert normalized["metadata"]["presentation"] == "pulse"


def test_daily_monitoring_visual_must_be_in_monitor_slot() -> None:
    payload = deepcopy(PULSE_DAILY_SKELETON)
    chart = payload["blocks"][5]["blocks"].pop()
    payload["blocks"][4]["blocks"].append(chart)
    payload["blocks"][5]["blocks"].append(
        {"type": "card", "title": "Not monitoring", "content": "Summary"}
    )

    with pytest.raises(RichPayloadValidationError) as exc_info:
        normalize_rich_payload(payload)

    assert any(issue["path"] == "$.blocks[5]" for issue in exc_info.value.issues)


@pytest.mark.parametrize(
    ("mutate", "reason"),
    [
        (
            lambda payload: payload["blocks"][5]["blocks"][0]["series"][0]["points"].pop(),
            "invalid_pulse_visual",
        ),
        (
            lambda payload: payload["blocks"][5]["blocks"][0].__setitem__(
                "series", [{"id": "invalid", "label": "Invalid", "points": [{}, {}, {}]}]
            ),
            "invalid_chart_point_x",
        ),
        (
            lambda payload: payload["blocks"][5]["blocks"][0].pop("observed_at"),
            "invalid_pulse_visual",
        ),
        (
            lambda payload: (
                payload["blocks"][5]["blocks"][0].__setitem__(
                    "timestamp", "2026-01-01T08:00:00+00:00"
                ),
                payload["blocks"][5]["blocks"][0].pop("observed_at"),
            ),
            "invalid_chart_field",
        ),
        (
            lambda payload: payload["blocks"][5]["blocks"][0]["series"][0].__setitem__(
                "points",
                [{"x": "T-2", "y": 0}, {"x": "T-1", "y": 0}, {"x": "Now", "y": 0}],
            ),
            "invalid_pulse_visual",
        ),
        (
            lambda payload: payload["blocks"][5]["blocks"][0].__setitem__("title", "Source count"),
            "invalid_pulse_visual",
        ),
        (
            lambda payload: (
                payload["blocks"][4]["blocks"][0]["items"][0].pop("source_id"),
                payload["blocks"][4]["blocks"][0]["items"][0].pop("citations"),
            ),
            "invalid_pulse_composition",
        ),
        (
            lambda payload: payload["blocks"][4]["blocks"][0]["items"][0].__setitem__(
                "content", "Story without a rendered link."
            ),
            "invalid_pulse_composition",
        ),
        (
            lambda payload: payload["blocks"][4]["blocks"][0]["items"][0].__setitem__(
                "content", "Bare URL https://news.example.org/story"
            ),
            "invalid_pulse_composition",
        ),
        (
            lambda payload: payload["blocks"][4]["blocks"][0]["items"][0].__setitem__(
                "content", "[Unrelated](https://unrelated.example.org/story)"
            ),
            "invalid_pulse_composition",
        ),
        (
            lambda payload: payload["blocks"][4]["blocks"][0]["items"][0].__setitem__(
                "content", "![Image](https://news.example.org/story)"
            ),
            "invalid_pulse_composition",
        ),
        (
            lambda payload: payload["blocks"][4]["blocks"][0]["items"][0].__setitem__(
                "content", "`[Code](https://news.example.org/story)`"
            ),
            "invalid_pulse_composition",
        ),
        (
            lambda payload: payload["blocks"][4]["blocks"][0]["items"][0].pop("url"),
            "invalid_pulse_composition",
        ),
        (
            lambda payload: payload["blocks"][1]["blocks"][0].pop("icon"),
            "invalid_pulse_block",
        ),
        (
            lambda payload: payload["blocks"][3]["blocks"][1]["blocks"].__setitem__(
                0, {"type": "chart", "data": []}
            ),
            "legacy_chart_field",
        ),
    ],
)
def test_pulse_v2_quality_gate_rejects_structural_only_success(
    mutate: Callable[[dict[str, Any]], None],
    reason: str,
) -> None:
    payload = deepcopy(PULSE_DAILY_SKELETON)
    mutate(payload)

    with pytest.raises(RichPayloadValidationError) as exc_info:
        normalize_rich_payload(payload)

    assert any(issue["reason"] == reason for issue in exc_info.value.issues)


def test_pulse_v2_requires_image_alt_and_provenance() -> None:
    payload = deepcopy(PULSE_DAILY_SKELETON)
    payload["blocks"][5]["blocks"] = [
        {
            "type": "figure",
            "src": "artifact://generated-figure",
            "caption": "Decision-relevant monitoring figure.",
        }
    ]

    with pytest.raises(RichPayloadValidationError) as exc_info:
        normalize_rich_payload(payload)

    paths = {issue["path"] for issue in exc_info.value.issues}
    assert "$.media[0].src" in paths
    assert "$.media[0].alt" in paths
    assert "$.media[0].provenance" in paths


@pytest.mark.parametrize(
    "src",
    [
        "https://cdn.example.org/remote.png",
        "/unresolved/local.png",
        "data:image/svg+xml,<svg><script>alert(1)</script></svg>",
    ],
)
def test_pulse_v2_does_not_count_renderer_rejected_figure_sources(src: str) -> None:
    payload = deepcopy(PULSE_DAILY_SKELETON)
    payload["blocks"][5]["blocks"] = [
        {
            "type": "figure",
            "src": src,
            "alt": "Monitoring visual",
            "source": "Collector",
            "provenance": "Collector replay",
        }
    ]

    with pytest.raises(RichPayloadValidationError) as exc_info:
        normalize_rich_payload(payload)

    assert any(
        issue["reason"] == "invalid_pulse_visual"
        and issue["expected"] == "renderer-supported figure src or url"
        for issue in exc_info.value.issues
    )


def test_pulse_v2_rejects_nested_uncited_story_descendants() -> None:
    payload = deepcopy(PULSE_DAILY_SKELETON)
    payload["blocks"][4]["blocks"][0]["items"][0]["blocks"] = [
        {
            "type": "card",
            "title": "Nested uncited story",
            "content": "This rendered descendant must not bypass validation.",
        }
    ]

    with pytest.raises(RichPayloadValidationError) as exc_info:
        normalize_rich_payload(payload)

    assert any(
        issue["expected"] == "leaf story block without nested rendered stories"
        for issue in exc_info.value.issues
    )


def test_pulse_v2_rejects_story_cards_outside_accordions() -> None:
    payload = deepcopy(PULSE_DAILY_SKELETON)
    payload["blocks"][4]["blocks"].append(
        {
            "type": "card",
            "title": "Uncited direct story",
            "content": "This must not bypass citation counts.",
        }
    )

    with pytest.raises(RichPayloadValidationError) as exc_info:
        normalize_rich_payload(payload)

    assert any(
        issue["path"] == "$.blocks[4].blocks" and issue["reason"] == "invalid_pulse_block"
        for issue in exc_info.value.issues
    )


def test_pulse_v2_allows_only_one_compact_unavailable_signal() -> None:
    payload = deepcopy(PULSE_DAILY_SKELETON)
    payload["blocks"][3]["blocks"][1]["blocks"][0]["status"] = "unavailable"
    payload["blocks"][3]["blocks"][1]["blocks"][1]["degraded_data"] = True

    with pytest.raises(RichPayloadValidationError) as exc_info:
        normalize_rich_payload(payload)

    assert any(
        issue["expected"] == "at most one compact unavailable/degraded-data signal"
        for issue in exc_info.value.issues
    )


@pytest.mark.parametrize(
    ("path", "value"),
    [
        ("presentation", {}),
        ("toc", []),
    ],
)
def test_non_scalar_pulse_metadata_returns_structured_rejection(path: str, value: object) -> None:
    payload = deepcopy(PULSE_DAILY_SKELETON)
    payload["metadata"][path] = value

    with pytest.raises(RichPayloadValidationError):
        normalize_rich_payload(payload)


def test_generic_descriptor_rejects_whitespace_only_fallback_before_write() -> None:
    arguments = {
        "action": "write_deliverable",
        "content": "  \n",
    }

    validated = validate_available_tool_call(
        [WRITE_DELIVERABLE_TOOL], "write_deliverable", arguments
    )

    assert validated["valid"] is False
    assert any(error.startswith("content:") for error in validated["errors"])


@pytest.mark.asyncio
async def test_validate_tool_call_rejects_missing_rich_payload_like_persistence() -> None:
    arguments = {
        "action": "write_deliverable",
        "content": "Fallback",
        "format": "rich",
    }

    validated = await validate_available_tool_call_with_context(
        [WRITE_DELIVERABLE_TOOL],
        "write_deliverable",
        arguments,
        None,
    )
    with pytest.raises(RichPayloadValidationError) as exc_info:
        normalize_required_rich_payload(None)

    assert validated["valid"] is False
    assert exc_info.value.reason == "missing_rich_payload"
    assert validated["errors"] == [
        {
            "code": exc_info.value.reason,
            "path": "$.rich",
            "message": (
                f"At $.rich, expected {exc_info.value.expected}. Correct that path and retry "
                "write_deliverable with format='rich'."
            ),
        }
    ]


def test_generic_and_pulse_write_schemas_accept_canonical_chart_examples() -> None:
    generic, pulse = WRITE_DELIVERABLE_TOOL.native_operations

    assert (
        validate_available_tool_call(
            [WRITE_DELIVERABLE_TOOL],
            "write_deliverable",
            generic.examples[0],
        )["valid"]
        is True
    )
    assert (
        validate_available_tool_call(
            [WRITE_DELIVERABLE_TOOL],
            "write_deliverable",
            pulse.examples[0],
        )["valid"]
        is True
    )
    generic_block_schema = generic.input_schema["definitions"]["genericRichBlock"]
    pulse_block_schema = pulse.input_schema["definitions"]["pulseRichBlock"]
    assert generic_block_schema["then"]["properties"]["spec_version"] == {
        "const": "cognis.chart.v1"
    }
    assert pulse_block_schema["then"]["properties"]["chart_type"]["enum"]
    assert generic_block_schema["properties"]["blocks"]["items"] == {
        "$ref": "#/definitions/genericRichBlock"
    }
    assert pulse_block_schema["properties"]["blocks"]["items"] == {
        "$ref": "#/definitions/pulseRichBlock"
    }
    assert set(WRITE_DELIVERABLE_TOOL.parameters["definitions"]) == {
        "genericRichBlock",
        "pulseRichBlock",
    }


@pytest.mark.parametrize(
    ("chart_type", "y"),
    [
        ("range", 12),
        ("line", [10, 20]),
    ],
)
def test_generic_write_schema_enforces_range_y_semantics(
    chart_type: str,
    y: object,
) -> None:
    operation = next(
        item
        for item in WRITE_DELIVERABLE_TOOL.native_operations
        if item.operation == "write_deliverable"
    )
    arguments = deepcopy(operation.examples[0])
    chart = arguments["rich"]["blocks"][0]
    chart["chart_type"] = chart_type
    chart["series"][0]["points"][0]["y"] = y

    validated = validate_available_tool_call(
        [WRITE_DELIVERABLE_TOOL],
        "write_deliverable",
        arguments,
    )

    assert validated["valid"] is False


@pytest.mark.parametrize("legacy_field", ["data", "x_key", "y_key", "series_key"])
@pytest.mark.asyncio
async def test_native_chart_validation_returns_structured_migration_retry_issue(
    legacy_field: str,
) -> None:
    operation = next(
        item
        for item in WRITE_DELIVERABLE_TOOL.native_operations
        if item.operation == "write_deliverable"
    )
    chart = deepcopy(operation.examples[0]["rich"]["blocks"][0])
    chart[legacy_field] = [] if legacy_field == "data" else "legacy"
    arguments = deepcopy(operation.examples[0])
    arguments["rich"]["blocks"] = [chart]

    validated = await validate_available_tool_call_with_context(
        [WRITE_DELIVERABLE_TOOL],
        "write_deliverable",
        arguments,
        None,
    )

    assert validated["valid"] is False
    assert validated["errors"] == [
        {
            "code": "legacy_chart_field",
            "path": f"$.rich.blocks[0].{legacy_field}",
            "message": (
                f"At $.rich.blocks[0].{legacy_field}, expected remove this legacy field and "
                "migrate to cognis.chart.v1 using spec_version, chart_type, "
                "series[].points[].x/y, x_axis, and y_axis. Correct that path and retry "
                "write_deliverable with format='rich'."
            ),
        }
    ]


@pytest.mark.asyncio
async def test_native_chart_validation_reports_deeply_nested_retry_path() -> None:
    operation = next(
        item
        for item in WRITE_DELIVERABLE_TOOL.native_operations
        if item.operation == "write_deliverable"
    )
    chart = deepcopy(operation.examples[0]["rich"]["blocks"][0])
    chart["x_key"] = "label"
    arguments = deepcopy(operation.examples[0])
    arguments["rich"]["blocks"] = [
        {
            "type": "section",
            "blocks": [
                {
                    "type": "stack",
                    "children": [{"type": "section", "blocks": [chart]}],
                }
            ],
        }
    ]

    validated = await validate_available_tool_call_with_context(
        [WRITE_DELIVERABLE_TOOL],
        "write_deliverable",
        arguments,
        None,
    )

    assert validated["valid"] is False
    assert validated["errors"][0]["path"] == (
        "$.rich.blocks[0].blocks[0].children[0].blocks[0].x_key"
    )


@pytest.mark.asyncio
async def test_native_chart_retry_preserves_unrelated_schema_errors() -> None:
    operation = next(
        item
        for item in WRITE_DELIVERABLE_TOOL.native_operations
        if item.operation == "write_deliverable"
    )
    arguments = deepcopy(operation.examples[0])
    arguments.pop("content")
    arguments["rich"]["blocks"][0]["data"] = []

    validated = await validate_available_tool_call_with_context(
        [WRITE_DELIVERABLE_TOOL],
        "write_deliverable",
        arguments,
        None,
    )

    assert validated["valid"] is False
    assert validated["errors"][0]["code"] == "legacy_chart_field"
    assert any("content" in error for error in validated["schema_errors"])


def test_generic_and_pulse_schemas_preserve_typeless_gallery_item_shorthand() -> None:
    generic, pulse = WRITE_DELIVERABLE_TOOL.native_operations
    generic_arguments = deepcopy(generic.examples[0])
    generic_arguments["rich"]["blocks"] = [
        {
            "type": "gallery",
            "items": [{"url": "https://images.example.org/chart.png", "caption": "Chart"}],
        }
    ]
    pulse_arguments = deepcopy(pulse.examples[0])
    pulse_arguments["rich"]["blocks"][4]["blocks"].append(
        {
            "type": "gallery",
            "items": [{"url": "https://images.example.org/news.png", "caption": "News"}],
        }
    )

    assert validate_available_tool_call(
        [WRITE_DELIVERABLE_TOOL], "write_deliverable", generic_arguments
    )["valid"]
    assert validate_available_tool_call(
        [WRITE_DELIVERABLE_TOOL], "write_deliverable", pulse_arguments
    )["valid"]


def test_schema_invalid_pulse_rejects_with_live_repair_contract() -> None:
    arguments = _fixture("daily_write_arguments.json")
    arguments.pop("rich")

    validated = validate_available_tool_call(
        [WRITE_DELIVERABLE_TOOL],
        "write_deliverable",
        arguments,
    )
    contract = describe_available_tool(
        [WRITE_DELIVERABLE_TOOL],
        "write_deliverable",
    )["descriptor"]["extensions"]["presentation_contracts"]["rich:pulse"]

    assert validated["valid"] is False
    assert validated["operation"] == "rich:pulse"
    assert contract["retry_guidance"]
    assert contract["valid_skeleton"] == PULSE_DAILY_SKELETON


def test_daily_brief_v10_document_contains_api_compatible_exact_fields() -> None:
    document = (
        Path(__file__).parents[2] / "docs" / "migrations" / "daily-brief-v10.md"
    ).read_text()
    templates_section = document.split("### `prompt_templates`", 1)[1].split("### `steps`", 1)[0]
    fenced_json = re.search(r"```json\n(.+?)\n```", templates_section, re.DOTALL)

    assert fenced_json is not None
    prompt_templates = json.loads(fenced_json.group(1))
    assert isinstance(prompt_templates, dict)
    assert set(prompt_templates) == {"daily_brief_v10"}
    assert isinstance(prompt_templates["daily_brief_v10"], str)
    descriptor_path = 'descriptor.extensions.presentation_contracts["rich:pulse"].valid_skeleton'
    assert descriptor_path in document
    assert descriptor_path in prompt_templates["daily_brief_v10"]
    assert "`steps` to an empty array" in document


def test_daily_brief_v11_document_contains_pulse_v2_collector_contract() -> None:
    document = (
        Path(__file__).parents[2] / "docs" / "migrations" / "daily-brief-v11.md"
    ).read_text()
    templates_section = document.split("### `prompt_templates`", 1)[1].split("### `steps`", 1)[0]
    fenced_json = re.search(r"```json\n(.+?)\n```", templates_section, re.DOTALL)

    assert fenced_json is not None
    prompt_templates = json.loads(fenced_json.group(1))
    assert set(prompt_templates) == {"daily_brief_v11"}
    template = prompt_templates["daily_brief_v11"]
    assert "cognis-pulse-deliverable" in template
    assert "infrastructure_error" in template
    assert "source_unavailable" in template
    assert "15-result Gmail" in template
    assert "valid=true" in template
    assert '"media": [' in document
    assert "`steps` to an empty array" in document


@pytest.mark.parametrize(
    ("mutate", "expected_path"),
    [
        (lambda payload: payload["blocks"].__setitem__(0, {"type": "section"}), "$.blocks[0]"),
        (
            lambda payload: payload["blocks"][1]["blocks"].append(
                {"type": "card", "title": "Not a metric"}
            ),
            "$.blocks[1].blocks",
        ),
        (
            lambda payload: payload["blocks"][3].__setitem__("content", "x" * 2_401),
            "$.blocks[3].content",
        ),
        (
            lambda payload: payload["blocks"][4]["blocks"].append(
                {"type": "markdown", "content": "# 1. Academic section"}
            ),
            "$.blocks[4].blocks[1].type",
        ),
        (
            lambda payload: payload["blocks"].__setitem__(
                -1, {"type": "section", "title": "Not sources"}
            ),
            "$.blocks[7]",
        ),
    ],
)
def test_invalid_pulse_returns_json_path_issues_and_retry_skeleton(
    mutate: Callable[[dict[str, Any]], None],
    expected_path: str,
) -> None:
    payload = deepcopy(PULSE_V1_DAILY_SKELETON)
    mutate(payload)

    with pytest.raises(RichPayloadValidationError) as exc_info:
        normalize_rich_payload(payload)

    result = exc_info.value.to_tool_result()
    assert any(issue["path"] == expected_path for issue in result["issues"])
    assert result["valid_skeleton"] == PULSE_DAILY_SKELETON
    assert "new generic rich payload" in result["retry_guidance"]


def test_unknown_presentation_is_rejected_and_generic_fallback_is_explicit() -> None:
    unknown = deepcopy(PULSE_DAILY_SKELETON)
    unknown["metadata"]["presentation"] = "newsletter"
    with pytest.raises(RichPayloadValidationError) as exc_info:
        normalize_rich_payload(unknown)
    assert exc_info.value.path == "$.metadata.presentation"

    generic = {"blocks": [{"type": "markdown", "content": "# Generic report"}], "metadata": {}}
    normalized, warnings = normalize_rich_payload(generic)
    assert warnings == []
    assert normalized["metadata"] == {}


@pytest.mark.parametrize("presentation", [None, "", "  ", "generic"])
def test_pulse_variant_requires_explicit_pulse_presentation(presentation: object) -> None:
    payload = {
        "blocks": [{"type": "markdown", "content": "# Generic report"}],
        "metadata": {"pulse_variant": "daily"},
    }
    if presentation is not None:
        payload["metadata"]["presentation"] = presentation

    with pytest.raises(RichPayloadValidationError) as exc_info:
        normalize_rich_payload(payload)

    assert exc_info.value.reason == "pulse_variant_requires_pulse_presentation"
    assert exc_info.value.path == "$.metadata.presentation"


def test_null_presentation_with_pulse_variant_is_rejected() -> None:
    payload = {
        "blocks": [{"type": "markdown", "content": "# Generic report"}],
        "metadata": {"presentation": None, "pulse_variant": "daily"},
    }

    with pytest.raises(RichPayloadValidationError) as exc_info:
        normalize_rich_payload(payload)

    assert exc_info.value.reason == "pulse_variant_requires_pulse_presentation"


@pytest.mark.parametrize("presentation", [None, "", "  ", "generic"])
def test_invalid_presentation_label_is_not_a_generic_fallback(
    presentation: object,
) -> None:
    payload = {
        "blocks": [{"type": "markdown", "content": "# Generic report"}],
        "metadata": {"presentation": presentation},
    }

    with pytest.raises(RichPayloadValidationError) as exc_info:
        normalize_rich_payload(payload)

    assert exc_info.value.reason == "unknown_rich_presentation"
    assert exc_info.value.path == "$.metadata.presentation"


@pytest.mark.parametrize("container_type", ["tabs", "accordion", "modal", "gallery"])
def test_generic_item_containers_enforce_nested_block_type(container_type: str) -> None:
    payload = {
        "blocks": [
            {
                "type": container_type,
                "items": [{"type": "unsupported", "content": "Hidden invalid block"}],
            }
        ]
    }

    with pytest.raises(RichPayloadValidationError) as exc_info:
        normalize_rich_payload(payload)

    assert exc_info.value.reason == "unsupported_rich_block_type"
    assert exc_info.value.path == "$.blocks[0].items[0].type"


@pytest.mark.parametrize("container_type", ["modal", "gallery"])
def test_generic_item_containers_enforce_nested_string_size(container_type: str) -> None:
    payload = {
        "blocks": [
            {
                "type": container_type,
                "items": [{"type": "section", "content": "x" * 16_385}],
            }
        ]
    }

    with pytest.raises(RichPayloadValidationError) as exc_info:
        normalize_rich_payload(payload)

    assert exc_info.value.reason == "rich_string_too_long"
    assert exc_info.value.path == "$.blocks[0].items[0].content"


@pytest.mark.parametrize("container_type", ["tabs", "accordion", "modal", "gallery"])
def test_generic_item_containers_enforce_64_rendered_block_limit(
    container_type: str,
) -> None:
    accepted, warnings = normalize_rich_payload(
        {"blocks": [_item_chain(container_type, RICH_DELIVERABLE_MAX_BLOCKS)]}
    )
    assert warnings == []
    assert accepted is not None
    row = SimpleNamespace(
        title="Nested limit",
        format="rich",
        content="Nested item container limit.",
        rich_payload=accepted,
    )
    rendered = render_standalone_html(row)
    assert rendered.count(f"block-{container_type}") == RICH_DELIVERABLE_MAX_BLOCKS

    with pytest.raises(RichPayloadValidationError) as exc_info:
        normalize_rich_payload(
            {"blocks": [_item_chain(container_type, RICH_DELIVERABLE_MAX_BLOCKS + 1)]}
        )
    assert exc_info.value.reason == "rich_block_count_exceeded"


@pytest.mark.asyncio
async def test_invalid_pulse_is_never_persisted(tmp_path: Path) -> None:
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path}/pulse.db")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = create_session_factory(engine)
    store = ArtifactStore(
        ArtifactStoreConfig(
            path=str(tmp_path / "artifacts"),
            base_url="http://testserver",
            signing_secret="pulse-test-secret",
        )
    )
    invalid = deepcopy(PULSE_DAILY_SKELETON)
    invalid["blocks"].pop()

    async with factory() as session:
        session.add(User(email="owner@example.org", name="Owner", role="user"))
        await session.flush()
        session.add(Agent(agent_id="pulse-agent", owner_email="owner@example.org", name="Pulse"))
        await session.flush()
        session.add(
            Conversation(
                conversation_id="pulse-conversation",
                user_email="owner@example.org",
                agent_id="pulse-agent",
                context_type="direct",
            )
        )
        await session.flush()

        with pytest.raises(RichPayloadValidationError):
            await create_deliverable(
                session,
                conversation_id="pulse-conversation",
                content="Fallback",
                format="rich",
                rich=invalid,
                artifact_store=store,
            )
        assert await session.scalar(select(func.count()).select_from(DeliverableRow)) == 0

    await engine.dispose()


@pytest.mark.asyncio
async def test_direct_rich_persistence_requires_payload_and_persists_nothing(
    tmp_path: Path,
) -> None:
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path}/missing-rich.db")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = create_session_factory(engine)
    store = ArtifactStore(
        ArtifactStoreConfig(
            path=str(tmp_path / "artifacts"),
            base_url="http://testserver",
            signing_secret="pulse-test-secret",
        )
    )

    async with factory() as session:
        session.add(User(email="owner@example.org", name="Owner", role="user"))
        await session.flush()
        session.add(Agent(agent_id="pulse-agent", owner_email="owner@example.org", name="Pulse"))
        await session.flush()
        session.add(
            Conversation(
                conversation_id="pulse-conversation",
                user_email="owner@example.org",
                agent_id="pulse-agent",
                context_type="direct",
            )
        )
        await session.flush()

        with pytest.raises(RichPayloadValidationError) as exc_info:
            await create_deliverable(
                session,
                conversation_id="pulse-conversation",
                content="Fallback",
                format="rich",
                rich=None,
                artifact_store=store,
            )

        assert exc_info.value.reason == "missing_rich_payload"
        assert await session.scalar(select(func.count()).select_from(DeliverableRow)) == 0

    await engine.dispose()


@pytest.mark.asyncio
async def test_acceptance_fixture_renders_standalone_and_pdf() -> None:
    arguments = _fixture("daily_write_arguments.json")
    payload, _ = normalize_rich_payload(arguments["rich"])
    row = SimpleNamespace(
        title=arguments["title"],
        format="rich",
        content=arguments["content"],
        rich_payload=payload,
    )

    rendered = render_standalone_html(row)
    soup = BeautifulSoup(rendered, "html.parser")
    pdf = await render_pdf_bytes(rendered)

    assert soup.body["class"] == ["presentation-pulse"]
    assert len(soup.select("h1")) == 1
    assert soup.select_one(".block-day-agenda") is not None
    assert soup.select_one(".block-chart") is not None
    assert soup.select_one(".block-sources") is not None
    assert soup.select_one(".document-toc") is None
    assert pdf.content.startswith(b"%PDF")
    assert len(pdf.content) > 1_000


@pytest.mark.asyncio
async def test_item_backed_containers_render_in_standalone_and_pdf() -> None:
    container_types = ("tabs", "accordion", "modal", "gallery")
    payload, _ = normalize_rich_payload(
        {
            "blocks": [
                {
                    "type": block_type,
                    "blocks": [{"type": "markdown", "content": f"{block_type} block marker"}],
                    "items": [
                        {
                            "type": "figure" if block_type == "gallery" else "markdown",
                            "title": (
                                f"{block_type} item marker"
                                if block_type == "gallery"
                                else f"{block_type} item"
                            ),
                            "content": f"{block_type} item marker",
                            **(
                                {"url": "https://example.org/gallery.png"}
                                if block_type == "gallery"
                                else {}
                            ),
                        }
                    ],
                }
                for block_type in container_types
            ]
        }
    )
    row = SimpleNamespace(
        title="Item-backed containers",
        format="rich",
        content="Fallback",
        rich_payload=payload,
    )

    rendered = render_standalone_html(row)
    pdf = await render_pdf_bytes(rendered)

    for block_type in container_types:
        assert f"{block_type} block marker" in rendered
        assert f"{block_type} item marker" in rendered
    assert pdf.content.startswith(b"%PDF")
    assert len(pdf.content) > 1_000
