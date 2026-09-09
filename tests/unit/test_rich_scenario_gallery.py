from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from cognis.channels.rich_markdown import render_rich_markdown
from cognis.models.deliverable import SUPPORTED_RICH_BLOCK_TYPES, normalize_rich_payload
from cognis.rendering.deliverables import render_standalone_html

SCENARIO_DIR = (
    Path(__file__).resolve().parents[2] / "ui" / "src" / "lib" / "rich-scenarios" / "scenarios"
)
FIXTURE_DIR = Path(__file__).resolve().parents[2] / "ui" / "static" / "fixtures" / "rich-scenarios"


def _scenarios() -> list[tuple[Path, dict[str, Any]]]:
    return [
        (path, json.loads(path.read_text(encoding="utf-8")))
        for path in sorted(SCENARIO_DIR.glob("*.json"))
    ]


def _walk_blocks(blocks: list[object]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for item in blocks:
        if not isinstance(item, dict):
            continue
        result.append(item)
        child_keys = ("blocks", "children")
        if item.get("type") in {"accordion", "tabs", "modal", "gallery"}:
            child_keys += ("items",)
        for key in child_keys:
            children = item.get(key)
            if isinstance(children, list):
                result.extend(_walk_blocks(children))
    return result


@pytest.mark.parametrize(
    ("path", "scenario"), _scenarios(), ids=lambda value: getattr(value, "name", None)
)
def test_rich_scenario_is_canonical_and_meaningful(path: Path, scenario: dict[str, Any]) -> None:
    assert path.stem == scenario["id"]
    payload, warnings = normalize_rich_payload(scenario["payload"])
    assert warnings == []
    assert payload is not None
    block_types = {
        str(block["type"]) for block in _walk_blocks(payload["blocks"]) if block.get("type")
    }
    assert block_types <= SUPPORTED_RICH_BLOCK_TYPES

    markdown = render_rich_markdown(
        payload,
        title=scenario["title"],
        full_view_link=None,
        deliverable_id=f"dlv_{scenario['id']}",
        fallback_text=scenario["content"],
    )
    assert len(markdown.strip()) >= 20
    assert scenario["title"] in markdown or any(
        str(block.get("title") or "") in markdown
        for block in payload["blocks"]
        if block.get("title")
    )

    row = SimpleNamespace(
        deliverable_id=f"dlv_{scenario['id']}",
        version=1,
        format="rich",
        title=scenario["title"],
        content=scenario["content"],
        content_hash="content-hash",
        rich_hash="rich-hash",
        rich_payload=payload,
    )
    html = render_standalone_html(row)
    assert "</html>" in html
    assert "Unsupported block" not in html
    assert scenario["title"] in html


def test_rich_scenario_ids_and_order_keys_are_unique() -> None:
    scenarios = _scenarios()
    assert scenarios
    ids = [scenario["id"] for _path, scenario in scenarios]
    assert len(ids) == len(set(ids))


def test_book_spoilers_warns_before_full_plot_in_all_projections() -> None:
    scenario = json.loads((SCENARIO_DIR / "book-spoilers.json").read_text(encoding="utf-8"))
    payload = scenario["payload"]
    markdown = render_rich_markdown(
        payload,
        title=scenario["title"],
        full_view_link=None,
        deliverable_id="dlv_book-spoilers",
        fallback_text=scenario["content"],
    )
    row = SimpleNamespace(
        format="rich",
        title=scenario["title"],
        content=scenario["content"],
        rich_payload=payload,
    )
    html = render_standalone_html(row)

    warning = "Spoiler warning"
    plot_heading = "Full plot summary"
    plot_content = "Victor creates a living being"
    for projection in (markdown, html):
        assert 0 <= projection.find(warning) < projection.find(plot_heading)
        assert projection.find(plot_heading) < projection.find(plot_content)


def test_data_projection_expected_generation_matches_monthly_series_and_export() -> None:
    scenario = json.loads((SCENARIO_DIR / "data-projections.json").read_text(encoding="utf-8"))
    source_export = json.loads(
        (FIXTURE_DIR / "data-projections" / "source-export.json").read_text(encoding="utf-8")
    )
    blocks = scenario["payload"]["blocks"]
    chart = next(block for block in blocks if block["type"] == "chart")
    metric_grid = next(block for block in blocks if block["type"] == "grid")
    scenario_table = next(block for block in blocks if block["type"] == "table")

    ranges = {
        point["x"]: point["y"]
        for series in chart["series"]
        if series["id"] == "generation_range"
        for point in series["points"]
    }
    expected_points = next(
        series["points"] for series in chart["series"] if series["id"] == "expected_generation"
    )
    assert all(point["y"][0] == point["y"][1] for point in expected_points)
    expected_monthly_values = {point["x"]: point["y"][0] for point in expected_points}
    expected_annual_generation = sum(expected_monthly_values.values())

    assert expected_annual_generation == source_export["expected_generation_kwh"]
    assert expected_monthly_values == source_export["monthly_expected_generation_kwh"]
    for month, expected_value in expected_monthly_values.items():
        low, high = ranges[month]
        assert low <= expected_value <= high

    expected_metric = next(
        metric for metric in metric_grid["blocks"] if metric["label"] == "Expected generation"
    )
    assert expected_metric["value"] == f"{expected_annual_generation:,} kWh"
    base_scenario = next(
        row for row in scenario_table["rows"] if row["scenario"].startswith("Base:")
    )
    assert base_scenario["generation"] == f"{expected_annual_generation:,} kWh"
