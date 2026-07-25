"""Golden parity coverage for canonical rich chart renderers."""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest
from bs4 import BeautifulSoup

from cognis.rendering.rich_visuals import chart_trend_text, normalize_chart, render_chart_svg

_FIXTURE_PATH = Path(__file__).parents[1] / "fixtures" / "chart_parity.json"
_GOLDEN = json.loads(_FIXTURE_PATH.read_text(encoding="utf-8"))


def _axis_semantics(axis: Any) -> dict[str, Any]:
    return {
        "type": axis.type,
        "label": axis.label,
        "unit": axis.unit,
        "min": axis.min,
        "max": axis.max,
    }


def _series_values(model: Any, ordered_x: list[str]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for series in model.series:
        values_by_x = {
            str(point.x): list(point.y) if isinstance(point.y, tuple) else point.y
            for point in series.points
        }
        result.append(
            {
                "id": series.id,
                "label": series.label,
                "stack": series.stack,
                "values": [values_by_x.get(x) for x in ordered_x],
            }
        )
    return result


@pytest.mark.parametrize("case", _GOLDEN["cases"], ids=lambda case: case["name"])
def test_python_svg_and_trend_match_canonical_chart_golden(
    case: Mapping[str, Any],
) -> None:
    spec = case["spec"]
    expected = case["expected"]
    model = normalize_chart(spec)

    assert model is not None
    assert model.spec_version == _GOLDEN["spec_version"]
    assert model.chart_type == expected["chart_type"]
    assert list(model.labels) == expected["ordered_x"]
    assert _series_values(model, expected["ordered_x"]) == expected["series"]
    assert _axis_semantics(model.x_axis) == expected["x_axis"]
    assert _axis_semantics(model.y_axis) == expected["y_axis"]
    assert model.stack is expected["stack"]
    assert model.legend_position == expected["legend_position"]
    assert model.palette_token == expected["palette_token"]
    assert chart_trend_text(model) == expected["trend_text"]

    svg = BeautifulSoup(render_chart_svg(model), "html.parser")
    root = svg.select_one("svg")
    assert root is not None
    assert f"chart-{expected['chart_type']}" in root.get("class", [])
    assert f"palette-{expected['palette_token']}" in root.get("class", [])
    assert len(svg.select(expected["svg_selector"])) == expected["svg_element_count"]
    if point_palette := expected.get("python_point_palette"):
        assert [
            element.get("fill") for element in svg.select(expected["svg_selector"])
        ] == point_palette

    legend = svg.select_one(f".chart-legend-{expected['legend_position']}")
    assert legend is not None
    if expected["chart_type"] == "donut":
        assert [item.get("data-point") for item in legend.select(".chart-legend-item")] == expected[
            "ordered_x"
        ]
    else:
        assert [item.get("data-series") for item in legend.select(".chart-legend-item")] == [
            series["id"] for series in expected["series"]
        ]
    assert [stop.get("stop-color") for stop in svg.select('stop[offset="0%"]')][
        : len(expected["python_palette"])
    ] == expected["python_palette"]

    rendered_series = {
        element.get("data-series")
        for element in svg.select("[data-series]")
        if element.get("data-series") is not None
    }
    assert rendered_series == {series["id"] for series in expected["series"]}
