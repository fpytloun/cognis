"""Deterministic visual primitives for server-rendered rich deliverables."""

from __future__ import annotations

import hashlib
import html
import math
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal, Protocol

RenderTarget = Literal["html", "pdf", "channel"]
ChartType = Literal[
    "line",
    "area",
    "bar",
    "grouped_bar",
    "stacked_bar",
    "sparkline",
    "progress",
    "range",
    "donut",
]
ChartAxisType = Literal["time", "category", "linear"]
ChartLegendPosition = Literal["top", "right", "bottom", "none"]
ChartPaletteToken = Literal["default", "cool", "warm", "categorical"]
ChartYValue = float | tuple[float, float]
ChartUpgradeStatus = Literal["upgraded", "already_canonical", "unupgradable"]

CHART_SPEC_VERSION: Literal["cognis.chart.v1"] = "cognis.chart.v1"
CANONICAL_CHART_TYPES: tuple[ChartType, ...] = (
    "line",
    "area",
    "bar",
    "grouped_bar",
    "stacked_bar",
    "sparkline",
    "progress",
    "range",
    "donut",
)
CHART_AXIS_TYPES: tuple[ChartAxisType, ...] = ("time", "category", "linear")
CHART_LEGEND_POSITIONS: tuple[ChartLegendPosition, ...] = ("top", "right", "bottom", "none")
CHART_PALETTE_TOKENS: tuple[ChartPaletteToken, ...] = ("default", "cool", "warm", "categorical")
_TIME_X_RE = re.compile(r"^\d{4}-\d{2}-\d{2}(?:T.*)?$")
_LEGACY_CHART_TYPE_ALIASES: dict[str, ChartType] = {
    **{chart_type: chart_type for chart_type in CANONICAL_CHART_TYPES},
    "column": "bar",
    "comparison": "grouped_bar",
    "doughnut": "donut",
    "grouped": "grouped_bar",
    "horizontal_bar": "bar",
    "pie": "donut",
    "stacked": "stacked_bar",
}
_LEGACY_CHART_DATA_KEYS = {"data", "rows", "series_key", "x_key", "y_key", "variant"}
_CHART_CHILD_BLOCK_KEYS = ("blocks", "children")
_CHART_ITEM_BACKED_BLOCK_TYPES = {"accordion", "gallery", "modal", "tabs"}

_ICON_SYMBOLS = {
    "activity": "↗",
    "alert": "⚠",
    "calendar": "▣",
    "check": "✓",
    "clock": "◷",
    "external": "↗",
    "info": "ⓘ",
    "trend_down": "↓",
    "trend_up": "↑",
    "arrow_up_right": "↗",
}


def icon_symbol(value: Any) -> str:
    """Return a safe cross-renderer symbol for a named or authored Unicode icon."""

    text = _text(value).strip()
    normalized = text.lower().replace("-", "_").replace(" ", "_")
    if normalized in _ICON_SYMBOLS:
        return _ICON_SYMBOLS[normalized]
    return text if text and not text.replace("_", "").replace("-", "").isalnum() else ""


@dataclass(frozen=True, slots=True)
class MediaReference:
    """Renderer-neutral media request resolved by the owning integration layer."""

    ref_id: str
    artifact_id: str | None
    alt: str
    credit: str | None
    mime_type: str | None


@dataclass(frozen=True, slots=True)
class ResolvedMedia:
    """Authorized media made safe for one renderer target."""

    src: str
    mime_type: str
    filename: str | None = None


class MediaResolver(Protocol):
    """Resolve an authorized media reference for HTML, PDF, or channel output."""

    def __call__(
        self,
        reference: MediaReference,
        target: RenderTarget,
    ) -> ResolvedMedia | None: ...


def media_reference(
    block: Mapping[str, Any],
    assets: Sequence[Mapping[str, Any]],
) -> MediaReference | None:
    """Resolve a block media key against the canonical asset manifest."""

    raw_ref = next(
        (
            block.get(key)
            for key in ("asset_id", "media_id", "artifact_id", "ref")
            if isinstance(block.get(key), str) and str(block.get(key)).strip()
        ),
        None,
    )
    asset: Mapping[str, Any] | None = None
    if isinstance(raw_ref, str):
        asset = next(
            (
                candidate
                for candidate in assets
                if raw_ref
                in {
                    _text(candidate.get("id")),
                    _text(candidate.get("asset_id")),
                    _text(candidate.get("media_id")),
                    _text(candidate.get("artifact_id")),
                    _text(candidate.get("ref")),
                }
            ),
            None,
        )
    if asset is None and isinstance(block.get("media"), Mapping):
        asset = block["media"]
    if asset is None and raw_ref is None:
        return None
    source = asset or block
    ref_id = _text(
        source.get("key")
        or source.get("ref")
        or source.get("id")
        or source.get("asset_id")
        or source.get("media_id")
        or source.get("artifact_id")
        or raw_ref
    )
    if not ref_id:
        return None
    artifact_id = _optional_text(source.get("artifact_id"))
    alt = _text(block.get("alt") or source.get("alt") or block.get("title"))
    credit = _optional_text(
        block.get("credit")
        or source.get("credit")
        or block.get("source_label")
        or source.get("source_label")
    )
    mime_type = _optional_text(source.get("mime_type") or source.get("content_type"))
    return MediaReference(
        ref_id=ref_id,
        artifact_id=artifact_id,
        alt=alt,
        credit=credit,
        mime_type=mime_type,
    )


@dataclass(frozen=True, slots=True)
class ChartPoint:
    x: str | float
    y: ChartYValue
    label: str | None


@dataclass(frozen=True, slots=True)
class ChartAxis:
    type: ChartAxisType
    label: str | None
    unit: str | None
    min: float | None
    max: float | None


@dataclass(frozen=True, slots=True)
class ChartSeries:
    id: str
    label: str
    points: tuple[ChartPoint, ...]
    stack: str | None


@dataclass(frozen=True, slots=True)
class ChartModel:
    type: Literal["chart"]
    spec_version: Literal["cognis.chart.v1"]
    chart_type: ChartType
    series: tuple[ChartSeries, ...]
    x_axis: ChartAxis
    y_axis: ChartAxis
    stack: bool
    legend_position: ChartLegendPosition
    palette_token: ChartPaletteToken
    source_ids: tuple[str, ...]
    source: str | None
    source_url: str | None
    observed_at: str | None
    description: str

    @property
    def kind(self) -> ChartType:
        """Compatibility alias used by the existing server renderer."""

        return self.chart_type

    @property
    def labels(self) -> tuple[str, ...]:
        """Return renderer labels in the order defined by the x-axis type."""

        return tuple(_point_x_text(point.x) for point in _ordered_points(self))


@dataclass(frozen=True, slots=True)
class ChartUpgradeResult:
    """Result of attempting a one-time legacy chart conversion."""

    status: ChartUpgradeStatus
    block: dict[str, Any] | None
    reason: str | None = None


@dataclass(frozen=True, slots=True)
class ChartPayloadUpgradeResult:
    """Bounded result for recursively upgrading chart blocks in a rich payload."""

    payload: dict[str, Any]
    upgraded_blocks: int
    reason: str | None = None


def upgrade_legacy_chart_block(block: Mapping[str, Any]) -> ChartUpgradeResult:
    """Convert supported legacy chart rows into the canonical chart contract."""

    if block.get("type") != "chart":
        return ChartUpgradeResult("unupgradable", None, "not_chart_block")
    if block.get("spec_version") == CHART_SPEC_VERSION:
        if normalize_chart(block) is None:
            return ChartUpgradeResult("unupgradable", None, "invalid_canonical_chart")
        return ChartUpgradeResult("already_canonical", dict(block))

    chart_type = _legacy_chart_type(block)
    if chart_type is None:
        return ChartUpgradeResult("unupgradable", None, "unsupported_chart_type")
    if chart_type == "range":
        return ChartUpgradeResult("unupgradable", None, "legacy_range_values_are_ambiguous")

    key_fields = ("series_key", "x_key", "y_key")
    is_flat = (
        "data" in block and "rows" not in block and not any(key in block for key in key_fields)
    )
    is_long_form = (
        "rows" in block and "data" not in block and all(key in block for key in key_fields)
    )
    if not is_flat and not is_long_form:
        return ChartUpgradeResult("unupgradable", None, "unsupported_legacy_shape")

    raw_rows = block.get("rows" if is_long_form else "data")
    if not isinstance(raw_rows, list) or not raw_rows:
        return ChartUpgradeResult("unupgradable", None, "missing_legacy_rows")

    series = (
        _upgrade_long_form_chart_series(block, raw_rows)
        if is_long_form
        else _upgrade_flat_chart_series(raw_rows)
    )
    if series is None:
        shape = "long_form" if is_long_form else "flat"
        return ChartUpgradeResult("unupgradable", None, f"invalid_{shape}_rows")
    x_axis_type = _legacy_x_axis_type(series)

    upgraded = {key: value for key, value in block.items() if key not in _LEGACY_CHART_DATA_KEYS}
    upgraded.update(
        {
            "type": "chart",
            "spec_version": CHART_SPEC_VERSION,
            "chart_type": chart_type,
            "series": series,
            "x_axis": _upgraded_axis(block.get("x_axis"), default_type=x_axis_type),
            "y_axis": _upgraded_axis(block.get("y_axis"), default_type="linear"),
            "stack": block.get("stack") is True or chart_type == "stacked_bar",
        }
    )
    if normalize_chart(upgraded) is None:
        return ChartUpgradeResult("unupgradable", None, "canonical_validation_failed")
    return ChartUpgradeResult("upgraded", upgraded)


def upgrade_legacy_chart_payload(
    payload: Mapping[str, Any],
    *,
    max_nodes: int = 10_000,
    max_depth: int = 32,
) -> ChartPayloadUpgradeResult:
    """Upgrade nested chart blocks without partially rewriting oversized payloads."""

    nodes = 0
    upgraded_blocks = 0

    def visit_blocks(value: Any, depth: int) -> Any:
        nonlocal nodes, upgraded_blocks
        nodes += 1
        if nodes > max_nodes or depth > max_depth:
            raise ValueError("chart_payload_traversal_limit")
        if isinstance(value, list):
            return [visit_block(item, depth + 1) for item in value]
        return value

    def visit_block(value: Any, depth: int) -> Any:
        nonlocal nodes, upgraded_blocks
        nodes += 1
        if nodes > max_nodes or depth > max_depth:
            raise ValueError("chart_payload_traversal_limit")
        if not isinstance(value, Mapping):
            return value
        if value.get("type") == "chart":
            result = upgrade_legacy_chart_block(value)
            if result.status == "upgraded" and result.block is not None:
                upgraded_blocks += 1
                return result.block
            return dict(value)

        upgraded = dict(value)
        for key in _CHART_CHILD_BLOCK_KEYS:
            if key in value:
                upgraded[key] = visit_blocks(value[key], depth + 1)
        if value.get("type") in _CHART_ITEM_BACKED_BLOCK_TYPES and "items" in value:
            upgraded["items"] = visit_blocks(value["items"], depth + 1)
        return upgraded

    try:
        upgraded_payload = dict(payload)
        if "blocks" in payload:
            upgraded_payload["blocks"] = visit_blocks(payload["blocks"], 0)
    except ValueError:
        return ChartPayloadUpgradeResult(dict(payload), 0, "chart_payload_traversal_limit")
    if not isinstance(upgraded_payload, dict):
        return ChartPayloadUpgradeResult(dict(payload), 0, "invalid_rich_payload")
    return ChartPayloadUpgradeResult(upgraded_payload, upgraded_blocks)


def rich_payload_has_noncanonical_chart(
    payload: Mapping[str, Any],
    *,
    max_nodes: int = 10_000,
    max_depth: int = 32,
) -> bool:
    """Return whether a payload contains a chart unsafe for canonical-only rendering."""

    nodes = 0

    def visit_blocks(value: Any, depth: int) -> bool:
        nonlocal nodes
        nodes += 1
        if nodes > max_nodes or depth > max_depth:
            return True
        if isinstance(value, list):
            return any(visit_block(item, depth + 1) for item in value)
        return False

    def visit_block(value: Any, depth: int) -> bool:
        nonlocal nodes
        nodes += 1
        if nodes > max_nodes or depth > max_depth:
            return True
        if not isinstance(value, Mapping):
            return False
        if value.get("type") == "chart":
            return normalize_chart(value) is None
        if any(visit_blocks(value.get(key), depth + 1) for key in _CHART_CHILD_BLOCK_KEYS):
            return True
        return value.get("type") in _CHART_ITEM_BACKED_BLOCK_TYPES and visit_blocks(
            value.get("items"), depth + 1
        )

    return visit_blocks(payload.get("blocks"), 0)


def normalize_chart(block: Mapping[str, Any]) -> ChartModel | None:
    """Parse the canonical ``cognis.chart.v1`` chart shape."""

    if block.get("type") != "chart" or block.get("spec_version") != CHART_SPEC_VERSION:
        return None
    chart_type = block.get("chart_type")
    if chart_type not in CANONICAL_CHART_TYPES:
        return None
    x_axis = _normalize_axis(block.get("x_axis"), default_type="category")
    y_axis = _normalize_axis(block.get("y_axis"), default_type="linear")
    if x_axis is None or y_axis is None:
        return None
    raw_series = block.get("series")
    if not isinstance(raw_series, list) or not raw_series:
        return None
    series = tuple(
        normalized
        for index, raw in enumerate(raw_series, start=1)
        if (normalized := _normalize_series(raw, index, x_axis.type, chart_type)) is not None
    )
    if not series:
        return None

    legend_position = block.get("legend_position", "bottom")
    palette_token = block.get("palette_token", "default")
    if legend_position not in CHART_LEGEND_POSITIONS or palette_token not in CHART_PALETTE_TOKENS:
        return None
    return ChartModel(
        type="chart",
        spec_version=CHART_SPEC_VERSION,
        chart_type=chart_type,
        series=series,
        x_axis=x_axis,
        y_axis=y_axis,
        stack=block.get("stack") is True,
        legend_position=legend_position,
        palette_token=palette_token,
        source_ids=_text_tuple(block.get("source_ids")),
        source=_strict_optional_text(block.get("source")),
        source_url=_strict_optional_text(block.get("source_url")),
        observed_at=_strict_optional_text(block.get("observed_at")),
        description=_strict_optional_text(block.get("description")) or "Chart",
    )


def _legacy_chart_type(block: Mapping[str, Any]) -> ChartType | None:
    raw_type = block.get("chart_type", block.get("variant", "line"))
    if not isinstance(raw_type, str):
        return None
    return _LEGACY_CHART_TYPE_ALIASES.get(raw_type.strip().lower())


def _upgrade_flat_chart_series(raw_rows: list[Any]) -> list[dict[str, Any]] | None:
    points: list[dict[str, Any]] = []
    seen_x: set[str] = set()
    for row in raw_rows:
        if not isinstance(row, Mapping):
            return None
        x = _strict_optional_text(row.get("label"))
        y = _finite_number(row.get("value"))
        if x is None or y is None or x in seen_x:
            return None
        seen_x.add(x)
        points.append({"x": x, "y": y})
    return [{"id": "value", "label": "Value", "points": points}] if points else None


def _upgrade_long_form_chart_series(
    block: Mapping[str, Any],
    raw_rows: list[Any],
) -> list[dict[str, Any]] | None:
    series_key = _strict_optional_text(block.get("series_key")) or "series"
    x_key = _strict_optional_text(block.get("x_key")) or "date"
    y_key = _strict_optional_text(block.get("y_key")) or "value"
    grouped: dict[str, list[dict[str, Any]]] = {}
    seen_points: set[tuple[str, str]] = set()
    for row in raw_rows:
        if not isinstance(row, Mapping):
            return None
        series_label = _strict_optional_text(row.get(series_key))
        x = _strict_optional_text(row.get(x_key))
        y = _finite_number(row.get(y_key))
        if series_label is None or x is None or y is None:
            return None
        point_key = (series_label, x)
        if point_key in seen_points:
            return None
        seen_points.add(point_key)
        grouped.setdefault(series_label, []).append({"x": x, "y": y})
    if not grouped:
        return None
    used_ids: set[str] = set()
    return [
        {
            "id": _legacy_series_id(label, index, used_ids),
            "label": label,
            "points": points,
        }
        for index, (label, points) in enumerate(grouped.items(), start=1)
    ]


def _legacy_series_id(label: str, index: int, used_ids: set[str]) -> str:
    base = re.sub(r"[^a-z0-9]+", "-", label.lower()).strip("-") or f"series-{index}"
    candidate = base
    suffix = 2
    while candidate in used_ids:
        candidate = f"{base}-{suffix}"
        suffix += 1
    used_ids.add(candidate)
    return candidate


def _legacy_x_axis_type(series: list[dict[str, Any]]) -> ChartAxisType:
    values = [
        point.get("x")
        for item in series
        for point in item.get("points", [])
        if isinstance(point, Mapping)
    ]
    if values and all(isinstance(value, str) and _timestamp(value) is not None for value in values):
        return "time"
    return "category"


def _upgraded_axis(value: Any, *, default_type: ChartAxisType) -> dict[str, Any]:
    axis: dict[str, Any] = {"type": default_type}
    if not isinstance(value, Mapping):
        return axis
    for key in ("label", "unit"):
        if (text_value := _strict_optional_text(value.get(key))) is not None:
            axis[key] = text_value
    for key in ("min", "max"):
        if (number := _finite_number(value.get(key))) is not None:
            axis[key] = number
    return axis


def render_chart_svg(model: ChartModel) -> str:
    """Render canonical, deterministic SVG without browser-only dependencies."""

    if model.kind == "progress":
        return _render_progress_svg(model)
    if model.kind == "donut":
        return _render_donut_svg(model)

    layout = _chart_layout(model)
    colors = _PALETTES[model.palette_token]
    low, high = _chart_y_domain(model)
    x_values = _x_coordinates(model, layout)

    def y_at(value: float) -> float:
        return layout.top + layout.plot_height * (high - value) / (high - low)

    parts = _svg_start(model, layout.width, layout.height, colors)
    if model.kind != "sparkline":
        _append_axes(parts, model, layout, low, high, x_values, y_at)

    if model.kind in {"bar", "grouped_bar"}:
        _append_grouped_bars(parts, model, layout, x_values, y_at, low, high, colors)
    elif model.kind == "stacked_bar":
        _append_stacked_bars(parts, model, layout, x_values, y_at, low, high, colors)
    elif model.kind == "range":
        _append_ranges(parts, model, x_values, y_at, colors)
    else:
        _append_lines(parts, model, x_values, y_at, low, high, colors)

    _append_legend(parts, model, layout, colors)
    parts.append("</svg>")
    return "".join(parts)


_PALETTES: dict[ChartPaletteToken, tuple[str, ...]] = {
    "default": ("#0e7490", "#7c3aed", "#db2777", "#15803d", "#d97706", "#2563eb"),
    "cool": ("#0369a1", "#0891b2", "#4f46e5", "#7c3aed", "#0f766e", "#2563eb"),
    "warm": ("#c2410c", "#dc2626", "#db2777", "#d97706", "#9333ea", "#ca8a04"),
    "categorical": ("#2563eb", "#dc2626", "#16a34a", "#d97706", "#7c3aed", "#0891b2"),
}


@dataclass(frozen=True, slots=True)
class _ChartLayout:
    width: float
    height: float
    left: float
    right: float
    top: float
    bottom: float
    legend_rows: tuple[tuple[int, ...], ...]
    legend_column_width: float

    @property
    def plot_width(self) -> float:
        return self.width - self.left - self.right

    @property
    def plot_height(self) -> float:
        return self.height - self.top - self.bottom


@dataclass(frozen=True, slots=True)
class _LegendEntry:
    id: str
    label: str
    color_index: int
    point: bool = False


def _legend_entries(model: ChartModel) -> tuple[_LegendEntry, ...]:
    if model.kind == "donut":
        return tuple(
            _LegendEntry(label, label, index, point=True)
            for index, label in enumerate(model.labels)
        )
    return tuple(
        _LegendEntry(series.id, series.label, index) for index, series in enumerate(model.series)
    )


def _chart_layout(model: ChartModel) -> _ChartLayout:
    width = 720.0
    compact = model.kind in {"sparkline", "progress"}
    height = 180.0 if compact else 340.0
    left = 76.0 if not compact else 24.0
    right = 24.0
    top = 24.0
    bottom = 62.0 if not compact else 20.0
    rows: tuple[tuple[int, ...], ...] = ()
    column_width = 0.0
    legend_entries = _legend_entries(model)
    if model.legend_position in {"top", "bottom"}:
        rows = _legend_rows(legend_entries, width - left - right)
        legend_height = len(rows) * 24.0
        minimum_plot_height = 80.0 if compact else 150.0
        available_plot_height = height - top - bottom - legend_height - 8.0
        height += max(0.0, minimum_plot_height - available_plot_height)
        if model.legend_position == "top":
            top += legend_height + 8.0
        else:
            bottom += legend_height + 8.0
    elif model.legend_position == "right":
        default_rows = max(1, int((height - top - bottom) // 24))
        column_count = min(2, max(1, math.ceil(len(legend_entries) / default_rows)))
        max_rows = math.ceil(len(legend_entries) / column_count)
        height = max(height, top + bottom + max_rows * 24.0)
        rows = tuple(
            tuple(range(start, min(start + max_rows, len(legend_entries))))
            for start in range(0, len(legend_entries), max_rows)
        )
        column_width = min(
            max((_legend_item_width(entry.label) for entry in legend_entries), default=0.0),
            width / 3,
        )
        right += len(rows) * column_width + 12.0
    return _ChartLayout(width, height, left, right, top, bottom, rows, column_width)


def _legend_rows(
    entries: tuple[_LegendEntry, ...],
    available_width: float,
) -> tuple[tuple[int, ...], ...]:
    rows: list[list[int]] = [[]]
    used = 0.0
    for index, entry in enumerate(entries):
        item_width = _legend_item_width(entry.label)
        if rows[-1] and used + item_width > available_width:
            rows.append([])
            used = 0.0
        rows[-1].append(index)
        used += item_width
    return tuple(tuple(row) for row in rows if row)


def _legend_item_width(label: str) -> float:
    return 34.0 + min(24, len(label)) * 7.0


def _chart_digest(model: ChartModel) -> str:
    identity = f"{model.kind}:{model.palette_token}:{model.description}:" + "|".join(
        series.id for series in model.series
    )
    return hashlib.sha256(identity.encode()).hexdigest()[:10]


def _svg_start(
    model: ChartModel,
    width: float,
    height: float,
    colors: tuple[str, ...],
) -> list[str]:
    title = html.escape(model.description)
    digest = _chart_digest(model)
    parts = [
        f'<svg class="chart-svg chart-{model.kind} palette-{model.palette_token}" '
        # Explicit width/height (in addition to viewBox) are required for
        # WeasyPrint to compute a correct, consistent coordinate transform
        # for this SVG's PDF/print rendering -- without them, WeasyPrint
        # has been observed to render <text> axis labels using a different
        # (or no) scale/position transform than the <line>/<path> geometry,
        # so labels spill out as loose sequential text above the chart
        # instead of sitting at their intended x/y coordinates. `.chart-svg`
        # sets `width: 100%` in CSS, which takes precedence over these
        # attributes in every browser and in WeasyPrint alike, so this does
        # not change on-screen/web rendering at all.
        f'width="{width:g}" height="{height:g}" '
        f'viewBox="0 0 {width:g} {height:g}" role="img" '
        f'aria-label="{html.escape(model.description, quote=True)}">',
        f"<title>{title}</title><desc>{title}</desc><defs>",
    ]
    for index, color in enumerate(colors, start=1):
        parts.append(
            f'<linearGradient id="chart-{digest}-gradient-{index}" x1="0" y1="0" '
            'x2="0" y2="1" gradientUnits="objectBoundingBox">'
            f'<stop offset="0%" stop-color="{color}" stop-opacity="0.86"/>'
            f'<stop offset="100%" stop-color="{color}" stop-opacity="0.28"/>'
            "</linearGradient>"
        )
    parts.append("</defs>")
    return parts


def _gradient_id(model: ChartModel, index: int) -> str:
    palette_size = len(_PALETTES[model.palette_token])
    return f"chart-{_chart_digest(model)}-gradient-{index % palette_size + 1}"


def _chart_y_domain(model: ChartModel) -> tuple[float, float]:
    values = [
        value for series in model.series for point in series.points for value in _y_values(point.y)
    ]
    if model.kind == "stacked_bar":
        positive = {group: [0.0] * len(model.labels) for group in _stack_groups(model)}
        negative = {group: [0.0] * len(model.labels) for group in _stack_groups(model)}
        for series in model.series:
            group = series.stack or "default"
            for index, value in enumerate(_series_values(model, series)):
                if value is not None:
                    (positive if value >= 0 else negative)[group][index] += value
        values.extend(value for totals in positive.values() for value in totals)
        values.extend(value for totals in negative.values() for value in totals)
    low, high = min(values), max(values)
    if model.kind in {"bar", "grouped_bar", "stacked_bar", "area"}:
        low, high = min(0.0, low), max(0.0, high)
    if model.y_axis.min is not None:
        low = model.y_axis.min
    if model.y_axis.max is not None:
        high = model.y_axis.max
    if low >= high:
        if model.y_axis.min is not None and model.y_axis.max is None:
            high = low + (abs(low) * 0.1 or 1.0)
        elif model.y_axis.max is not None and model.y_axis.min is None:
            low = high - (abs(high) * 0.1 or 1.0)
        else:
            center = (low + high) / 2
            padding = abs(center) * 0.1 or 1.0
            low, high = center - padding, center + padding
    elif (
        model.y_axis.min is None
        and model.y_axis.max is None
        and model.kind
        in {
            "line",
            "sparkline",
            "range",
        }
    ):
        padding = (high - low) * 0.08
        low, high = low - padding, high + padding
    return low, high


def _x_coordinates(model: ChartModel, layout: _ChartLayout) -> tuple[float, ...]:
    points = _ordered_points(model)
    if not points:
        return ()
    inset = layout.plot_width / max(2.0, len(points) * 2.0)
    start = layout.left + inset
    end = layout.left + layout.plot_width - inset
    if model.x_axis.type == "linear":
        values = [float(point.x) for point in points]
    elif model.x_axis.type == "time":
        values = [_timestamp(str(point.x)) or 0.0 for point in points]
    else:
        values = [float(index) for index in range(len(points))]
    low = model.x_axis.min if model.x_axis.min is not None else min(values)
    high = model.x_axis.max if model.x_axis.max is not None else max(values)
    if low >= high:
        return tuple((start + end) / 2 for _ in values)
    return tuple(start + (end - start) * (value - low) / (high - low) for value in values)


def _axis_title(axis: ChartAxis) -> str:
    if axis.label and axis.unit:
        return f"{axis.label} ({axis.unit})"
    return axis.label or axis.unit or ""


def _append_axes(
    parts: list[str],
    model: ChartModel,
    layout: _ChartLayout,
    low: float,
    high: float,
    x_values: tuple[float, ...],
    y_at: Callable[[float], float],
) -> None:
    for tick in range(5):
        tick_value = low + (high - low) * tick / 4
        y = y_at(tick_value)
        parts.append(
            f'<line class="chart-gridline" x1="{layout.left:g}" y1="{y:.2f}" '
            f'x2="{layout.width - layout.right:g}" y2="{y:.2f}"/>'
            f'<text class="chart-axis-label" x="{layout.left - 9:g}" y="{y + 4:.2f}" '
            f'text-anchor="end">{html.escape(_number(tick_value))}</text>'
        )
    label_step = max(1, math.ceil(len(model.labels) / 8))
    for index, (label, x) in enumerate(zip(model.labels, x_values, strict=True)):
        if index % label_step == 0 or index == len(model.labels) - 1:
            parts.append(
                f'<text class="chart-axis-label" x="{x:.2f}" '
                f'y="{layout.top + layout.plot_height + 22:.2f}" text-anchor="middle">'
                f"{html.escape(label[:18])}</text>"
            )
    x_title = _axis_title(model.x_axis)
    if x_title:
        parts.append(
            f'<text class="chart-axis-title chart-axis-title-x" '
            f'x="{layout.left + layout.plot_width / 2:.2f}" '
            f'y="{layout.top + layout.plot_height + 46:.2f}" text-anchor="middle">'
            f"{html.escape(x_title)}</text>"
        )
    y_title = _axis_title(model.y_axis)
    if y_title:
        center = layout.top + layout.plot_height / 2
        parts.append(
            f'<text class="chart-axis-title chart-axis-title-y" x="16" y="{center:.2f}" '
            f'text-anchor="middle" transform="rotate(-90 16 {center:.2f})">'
            f"{html.escape(y_title)}</text>"
        )


def _append_grouped_bars(
    parts: list[str],
    model: ChartModel,
    layout: _ChartLayout,
    x_values: tuple[float, ...],
    y_at: Callable[[float], float],
    low: float,
    high: float,
    colors: tuple[str, ...],
) -> None:
    group_width = layout.plot_width / max(1, len(x_values)) * 0.68
    bar_width = group_width / max(1, len(model.series))
    baseline = y_at(min(high, max(low, 0.0)))
    for series_index, series in enumerate(model.series):
        for index, value in enumerate(_series_values(model, series)):
            if value is None:
                continue
            value_y = y_at(min(high, max(low, value)))
            x = x_values[index] - group_width / 2 + series_index * bar_width
            parts.append(
                f'<rect class="chart-bar chart-series-{series_index % 6 + 1}" '
                f'data-series="{html.escape(series.id, quote=True)}" x="{x + 1:.2f}" '
                f'y="{min(value_y, baseline):.2f}" width="{max(1.0, bar_width - 2):.2f}" '
                f'height="{max(0.75, abs(baseline - value_y)):.2f}" '
                f'fill="{colors[series_index % len(colors)]}"/>'
            )


def _append_stacked_bars(
    parts: list[str],
    model: ChartModel,
    layout: _ChartLayout,
    x_values: tuple[float, ...],
    y_at: Callable[[float], float],
    low: float,
    high: float,
    colors: tuple[str, ...],
) -> None:
    groups = _stack_groups(model)
    group_width = layout.plot_width / max(1, len(x_values)) * 0.68
    bar_width = group_width / len(groups)
    positive = {group: [0.0] * len(x_values) for group in groups}
    negative = {group: [0.0] * len(x_values) for group in groups}
    for series_index, series in enumerate(model.series):
        group = series.stack or "default"
        group_index = groups.index(group)
        for index, value in enumerate(_series_values(model, series)):
            if value is None:
                continue
            start = positive[group][index] if value >= 0 else negative[group][index]
            end = start + value
            if value >= 0:
                positive[group][index] = end
            else:
                negative[group][index] = end
            start_y = y_at(min(high, max(low, start)))
            end_y = y_at(min(high, max(low, end)))
            x = x_values[index] - group_width / 2 + group_index * bar_width
            parts.append(
                f'<rect class="chart-bar chart-stack-segment chart-series-{series_index % 6 + 1}" '
                f'data-series="{html.escape(series.id, quote=True)}" '
                f'data-stack="{html.escape(group, quote=True)}" '
                f'data-stack-start="{start:g}" data-stack-end="{end:g}" '
                f'x="{x + 1:.2f}" y="{min(start_y, end_y):.2f}" '
                f'width="{max(1.0, bar_width - 2):.2f}" '
                f'height="{max(0.75, abs(start_y - end_y)):.2f}" '
                f'fill="{colors[series_index % len(colors)]}"/>'
            )


def _stack_groups(model: ChartModel) -> tuple[str, ...]:
    return tuple(dict.fromkeys(series.stack or "default" for series in model.series))


def _append_lines(
    parts: list[str],
    model: ChartModel,
    x_values: tuple[float, ...],
    y_at: Callable[[float], float],
    low: float,
    high: float,
    colors: tuple[str, ...],
) -> None:
    baseline = y_at(min(high, max(low, 0.0)))
    for series_index, series in enumerate(model.series):
        segments: list[list[tuple[float, float]]] = [[]]
        for x, value in zip(x_values, _series_values(model, series), strict=True):
            if value is None:
                if segments[-1]:
                    segments.append([])
                continue
            segments[-1].append((x, y_at(min(high, max(low, value)))))
        for segment in (segment for segment in segments if segment):
            point_text = " ".join(f"{x:.2f},{y:.2f}" for x, y in segment)
            if model.kind == "area":
                area_points = (
                    f"{segment[0][0]:.2f},{baseline:.2f} {point_text} "
                    f"{segment[-1][0]:.2f},{baseline:.2f}"
                )
                parts.append(
                    f'<polygon class="chart-area chart-series-{series_index % 6 + 1}" '
                    f'points="{area_points}" fill="url(#{_gradient_id(model, series_index)})"/>'
                )
            if len(segment) == 1:
                x, y = segment[0]
                parts.append(
                    f'<circle class="chart-point chart-series-{series_index % 6 + 1}" '
                    f'cx="{x:.2f}" cy="{y:.2f}" r="4" '
                    f'fill="{colors[series_index % len(colors)]}"/>'
                )
            else:
                parts.append(
                    f'<polyline class="chart-line chart-series-{series_index % 6 + 1}" '
                    f'points="{point_text}" fill="none" '
                    f'stroke="{colors[series_index % len(colors)]}"/>'
                )


def _append_ranges(
    parts: list[str],
    model: ChartModel,
    x_values: tuple[float, ...],
    y_at: Callable[[float], float],
    colors: tuple[str, ...],
) -> None:
    offset_step = min(14.0, 540.0 / max(1, len(x_values) * len(model.series) * 2))
    for series_index, series in enumerate(model.series):
        offset = (series_index - (len(model.series) - 1) / 2) * offset_step
        for index, ordered in enumerate(_ordered_points(model)):
            point = _point_for_x(series, ordered.x)
            if point is None or not isinstance(point.y, tuple):
                continue
            low_y, high_y = y_at(point.y[0]), y_at(point.y[1])
            x = x_values[index] + offset
            color = colors[series_index % len(colors)]
            parts.append(
                f'<g class="chart-range-mark chart-series-{series_index % 6 + 1}" '
                f'data-series="{html.escape(series.id, quote=True)}">'
                f'<line x1="{x:.2f}" y1="{low_y:.2f}" x2="{x:.2f}" y2="{high_y:.2f}" '
                f'stroke="{color}" stroke-width="5"/>'
                f'<line x1="{x - 5:.2f}" y1="{low_y:.2f}" x2="{x + 5:.2f}" y2="{low_y:.2f}" '
                f'stroke="{color}" stroke-width="2"/>'
                f'<line x1="{x - 5:.2f}" y1="{high_y:.2f}" x2="{x + 5:.2f}" y2="{high_y:.2f}" '
                f'stroke="{color}" stroke-width="2"/></g>'
            )


def _append_legend(
    parts: list[str],
    model: ChartModel,
    layout: _ChartLayout,
    colors: tuple[str, ...],
) -> None:
    if model.legend_position == "none" or not layout.legend_rows:
        return
    entries = _legend_entries(model)
    parts.append(
        f'<g class="chart-legend chart-legend-{model.legend_position}" '
        f'data-position="{model.legend_position}">'
    )
    if model.legend_position in {"top", "bottom"}:
        base_y = (
            18.0
            if model.legend_position == "top"
            else layout.height - len(layout.legend_rows) * 24.0
        )
        for row_index, row in enumerate(layout.legend_rows):
            widths = [_legend_item_width(entries[index].label) for index in row]
            x = layout.left + max(0.0, (layout.plot_width - sum(widths)) / 2)
            for index, item_width in zip(row, widths, strict=True):
                _append_legend_item(parts, entries[index], x, base_y + row_index * 24, colors)
                x += item_width
    else:
        base_x = layout.width - layout.right + 18.0
        for column, row in enumerate(layout.legend_rows):
            for row_index, index in enumerate(row):
                _append_legend_item(
                    parts,
                    entries[index],
                    base_x + column * layout.legend_column_width,
                    layout.top + row_index * 24,
                    colors,
                )
    parts.append("</g>")


def _append_legend_item(
    parts: list[str],
    entry: _LegendEntry,
    x: float,
    y: float,
    colors: tuple[str, ...],
) -> None:
    color = colors[entry.color_index % len(colors)]
    data_attribute = "data-point" if entry.point else "data-series"
    parts.append(
        f'<g class="chart-legend-item" {data_attribute}="{html.escape(entry.id, quote=True)}">'
        f'<rect x="{x:.2f}" y="{y - 9:.2f}" width="18" height="8" rx="2" fill="{color}"/>'
        f'<text x="{x + 25:.2f}" y="{y:.2f}">{html.escape(entry.label[:24])}</text></g>'
    )


def chart_rows(model: ChartModel) -> tuple[list[str], list[list[str]]]:
    headers = ["Label", *(series.label for series in model.series)]
    rows = [
        [
            label,
            *(_format_point_y(_point_for_x(series, point.x)) for series in model.series),
        ]
        for point, label in zip(_ordered_points(model), model.labels, strict=True)
    ]
    return headers, rows


def _format_point_y(point: ChartPoint | None) -> str:
    return _format_y(point.y) if point is not None else ""


def chart_trend_text(model: ChartModel) -> str:
    """Return concise, deterministic trend text for channel projections."""

    summaries: list[str] = []
    for series in model.series[:3]:
        points = series.points
        if not points:
            continue
        start, end = points[0].y, points[-1].y
        if len(points) == 1:
            summaries.append(f"{series.label}: {_format_y(start)} (single point)")
            continue
        direction, delta = _trend_change(start, end)
        summaries.append(
            f"{series.label}: {_format_y(start)} → {_format_y(end)} ({direction} {delta})"
        )
    return "; ".join(summaries)


def _trend_change(start: ChartYValue, end: ChartYValue) -> tuple[str, str]:
    """Describe a scalar or range change without discarding range bounds."""

    if isinstance(start, tuple) and isinstance(end, tuple):
        low_delta = end[0] - start[0]
        high_delta = end[1] - start[1]
        if low_delta > 0 and high_delta > 0:
            direction = "up"
        elif low_delta < 0 and high_delta < 0:
            direction = "down"
        elif low_delta == 0 and high_delta == 0:
            direction = "flat"
        else:
            direction = "mixed"
        return direction, f"{_number(abs(low_delta))}–{_number(abs(high_delta))}"

    delta = _trend_value(end) - _trend_value(start)
    direction = "up" if delta > 0 else "down" if delta < 0 else "flat"
    return direction, _number(abs(delta))


def _normalize_axis(value: Any, *, default_type: ChartAxisType) -> ChartAxis | None:
    if value is None:
        return ChartAxis(type=default_type, label=None, unit=None, min=None, max=None)
    if not isinstance(value, Mapping):
        return None
    axis_type = value.get("type", default_type)
    if axis_type not in CHART_AXIS_TYPES:
        return None
    return ChartAxis(
        type=axis_type,
        label=_strict_optional_text(value.get("label")),
        unit=_strict_optional_text(value.get("unit")),
        min=_finite_number(value.get("min")),
        max=_finite_number(value.get("max")),
    )


def _normalize_series(
    value: Any,
    index: int,
    x_axis_type: ChartAxisType,
    chart_type: ChartType,
) -> ChartSeries | None:
    if not isinstance(value, Mapping):
        return None
    raw_points = value.get("points")
    if not isinstance(raw_points, list) or not raw_points:
        return None
    points_by_x: dict[str | float, ChartPoint] = {}
    for raw_point in raw_points:
        point = _normalize_point(raw_point, x_axis_type, chart_type)
        if point is not None:
            points_by_x[point.x] = point
    points = list(points_by_x.values())
    if not points:
        return None
    points.sort(key=lambda point: _x_sort_key(point.x, x_axis_type))
    series_id = _strict_optional_text(value.get("id")) or f"series-{index}"
    return ChartSeries(
        id=series_id,
        label=_strict_optional_text(value.get("label")) or series_id,
        points=tuple(points),
        stack=_strict_optional_text(value.get("stack")),
    )


def _normalize_point(
    value: Any,
    x_axis_type: ChartAxisType,
    chart_type: ChartType,
) -> ChartPoint | None:
    if not isinstance(value, Mapping):
        return None
    x = _normalize_x(value.get("x"), x_axis_type)
    if x is None:
        return None
    y = _normalize_y(value.get("y"), chart_type)
    if y is None:
        return None
    return ChartPoint(x=x, y=y, label=_strict_optional_text(value.get("label")))


def _normalize_x(value: Any, axis_type: ChartAxisType) -> str | float | None:
    if axis_type == "linear":
        return _finite_number(value)
    if axis_type == "category" and isinstance(value, (int, float)) and not isinstance(value, bool):
        number = _finite_number(value)
        return _number_x_text(number) if number is not None else None
    text = _text(value)
    if not text:
        return None
    if axis_type == "time" and _timestamp(text) is None:
        return None
    return text


def _normalize_y(value: Any, chart_type: ChartType) -> ChartYValue | None:
    if chart_type == "range":
        if not isinstance(value, (list, tuple)) or len(value) != 2:
            return None
        low = _finite_number(value[0])
        high = _finite_number(value[1])
        if low is None or high is None:
            return None
        return (min(low, high), max(low, high))
    return _finite_number(value)


def _timestamp(value: str) -> float | None:
    if _TIME_X_RE.fullmatch(value) is None:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        return parsed.timestamp()
    except ValueError:
        return None


def _x_sort_key(value: str | float, axis_type: ChartAxisType) -> tuple[int, float | str]:
    if axis_type == "time":
        return (0, _timestamp(str(value)) or 0.0)
    if axis_type == "linear":
        return (0, float(value))
    return (0, "")


def _ordered_points(model: ChartModel) -> tuple[ChartPoint, ...]:
    by_x: dict[str | float, ChartPoint] = {}
    for series in model.series:
        for point in series.points:
            by_x.setdefault(point.x, point)
    points = list(by_x.values())
    if model.x_axis.type != "category":
        points.sort(key=lambda point: _x_sort_key(point.x, model.x_axis.type))
    return tuple(points)


def _point_for_x(series: ChartSeries, x: str | float) -> ChartPoint | None:
    return next((point for point in series.points if point.x == x), None)


def _series_values(model: ChartModel, series: ChartSeries) -> tuple[float | None, ...]:
    return tuple(
        _trend_value(point.y) if (point := _point_for_x(series, ordered.x)) is not None else None
        for ordered in _ordered_points(model)
    )


def _y_values(value: ChartYValue) -> tuple[float, ...]:
    return value if isinstance(value, tuple) else (value,)


def _trend_value(value: ChartYValue) -> float:
    return sum(_y_values(value)) / len(_y_values(value))


def _format_y(value: ChartYValue) -> str:
    if isinstance(value, tuple):
        return f"{_number(value[0])}–{_number(value[1])}"
    return _number(value)


def _point_x_text(value: str | float) -> str:
    return _number_x_text(value) if isinstance(value, float) else value


def _number_x_text(value: float) -> str:
    if value.is_integer():
        return str(int(value))
    mantissa, exponent = f"{value:.15e}".split("e", 1)
    return f"{mantissa.rstrip('0').rstrip('.')}e{int(exponent)}"


def _text_tuple(value: Any) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    return tuple(text for item in value if (text := _strict_optional_text(item)))


def _render_progress_svg(model: ChartModel) -> str:
    points = [(series, point) for series in model.series for point in series.points]
    minimum = model.y_axis.min if model.y_axis.min is not None else 0.0
    maximum = model.y_axis.max if model.y_axis.max is not None else 100.0
    if minimum >= maximum:
        minimum, maximum = 0.0, 100.0
    colors = _PALETTES[model.palette_token]
    layout = _chart_layout(model)
    minimum_plot_height = len(points) * 32.0
    if layout.plot_height < minimum_plot_height:
        layout = _ChartLayout(
            layout.width,
            layout.top + layout.bottom + minimum_plot_height,
            layout.left,
            layout.right,
            layout.top,
            layout.bottom,
            layout.legend_rows,
            layout.legend_column_width,
        )
    parts = _svg_start(model, layout.width, layout.height, colors)
    row_height = layout.plot_height / max(1, len(points))
    track_height = min(24.0, max(8.0, row_height * 0.34))
    for index, (series, point) in enumerate(points):
        value = _trend_value(point.y)
        progress = min(1.0, max(0.0, (value - minimum) / (maximum - minimum)))
        center_y = layout.top + row_height * (index + 0.5)
        track_y = center_y - track_height / 2
        label = point.label or _point_x_text(point.x)
        parts.extend(
            [
                f'<text class="chart-axis-label" x="{layout.left:.2f}" '
                f'y="{track_y - 5:.2f}">{html.escape(label)}</text>',
                # fill is set inline (not via the .chart-progress-track CSS
                # class alone) because WeasyPrint's SVG support does not
                # reliably cascade page-level <style> rules onto inline SVG
                # shape elements in all cases; without an explicit fill the
                # unfilled track rectangle fell back to solid black instead
                # of a light track background.
                f'<rect class="chart-progress-track" x="{layout.left:.2f}" y="{track_y:.2f}" '
                f'width="{layout.plot_width:.2f}" height="{track_height:.2f}" '
                f'rx="{track_height / 2:.2f}" fill="#e4e7ea"/>',
                f'<rect class="chart-progress-fill chart-series-{index % 6 + 1}" '
                f'data-series="{html.escape(series.id, quote=True)}" '
                f'data-point="{html.escape(_point_x_text(point.x), quote=True)}" '
                f'x="{layout.left:.2f}" y="{track_y:.2f}" '
                f'width="{layout.plot_width * progress:.2f}" height="{track_height:.2f}" '
                f'rx="{track_height / 2:.2f}" fill="url(#{_gradient_id(model, index)})"/>',
                f'<text class="chart-progress-value" x="{layout.left + layout.plot_width:.2f}" '
                f'y="{track_y - 5:.2f}" text-anchor="end">'
                f"{html.escape(_number(value))}{html.escape(model.y_axis.unit or '')}</text>",
            ]
        )
    axis_title = _axis_title(model.y_axis)
    if axis_title:
        parts.append(
            f'<text class="chart-axis-title" x="{layout.left + layout.plot_width / 2:.2f}" '
            f'y="{layout.height - 8:.2f}" text-anchor="middle">'
            f"{html.escape(axis_title)}</text>"
        )
    _append_legend(parts, model, layout, colors)
    parts.append("</svg>")
    return "".join(parts)


def _render_donut_svg(model: ChartModel) -> str:
    colors = _PALETTES[model.palette_token]
    layout = _chart_layout(model)
    parts = _svg_start(model, layout.width, layout.height, colors)
    values = [[abs(_trend_value(point.y)) for point in series.points] for series in model.series]
    total = sum(sum(series_values) for series_values in values)
    cx = layout.left + layout.plot_width / 2
    cy = layout.top + layout.plot_height / 2
    radius = max(20.0, min(layout.plot_width, layout.plot_height) * 0.39)
    inner_radius = radius * 0.58
    if total <= 0:
        parts.append(
            f'<circle class="chart-donut-empty" cx="{cx:.2f}" cy="{cy:.2f}" '
            f'r="{radius:.2f}" fill="none" stroke="#cbd5e1" stroke-width="24"/>'
        )
    else:
        ring_width = (radius - inner_radius) / max(1, len(model.series))
        for series_index, (series, series_values) in enumerate(
            zip(model.series, values, strict=True)
        ):
            series_total = sum(series_values)
            if series_total <= 0:
                continue
            outer = radius - series_index * ring_width
            inner = outer - ring_width * 0.82
            angle = -math.pi / 2
            for point_index, ordered in enumerate(_ordered_points(model)):
                point = _point_for_x(series, ordered.x)
                if point is None:
                    continue
                value = abs(_trend_value(point.y))
                if value <= 0:
                    continue
                sweep = value / series_total * math.tau
                end = angle + sweep
                path = _donut_arc_path(cx, cy, outer, inner, angle, end)
                parts.append(
                    f'<path class="chart-donut-segment chart-series-{series_index % 6 + 1}" '
                    f'data-series="{html.escape(series.id, quote=True)}" '
                    f'data-point="{html.escape(_point_x_text(point.x), quote=True)}" '
                    f'd="{path}" fill="{colors[point_index % len(colors)]}"/>'
                )
                angle = end
    parts.append(
        f'<text class="chart-donut-total" x="{cx:.2f}" y="{cy + 5:.2f}" text-anchor="middle">'
        f"{html.escape(_number(total))}{html.escape(model.y_axis.unit or '')}</text>"
    )
    _append_legend(parts, model, layout, colors)
    parts.append("</svg>")
    return "".join(parts)


def _donut_arc_path(
    cx: float,
    cy: float,
    outer: float,
    inner: float,
    start: float,
    end: float,
) -> str:
    sweep = end - start
    if sweep >= math.tau - 1e-9:
        middle = start + math.pi
        outer_start = (cx + outer * math.cos(start), cy + outer * math.sin(start))
        outer_middle = (cx + outer * math.cos(middle), cy + outer * math.sin(middle))
        inner_start = (cx + inner * math.cos(start), cy + inner * math.sin(start))
        inner_middle = (cx + inner * math.cos(middle), cy + inner * math.sin(middle))
        return (
            f"M {outer_start[0]:.2f} {outer_start[1]:.2f} "
            f"A {outer:.2f} {outer:.2f} 0 0 1 {outer_middle[0]:.2f} {outer_middle[1]:.2f} "
            f"A {outer:.2f} {outer:.2f} 0 0 1 {outer_start[0]:.2f} {outer_start[1]:.2f} "
            f"L {inner_start[0]:.2f} {inner_start[1]:.2f} "
            f"A {inner:.2f} {inner:.2f} 0 0 0 {inner_middle[0]:.2f} {inner_middle[1]:.2f} "
            f"A {inner:.2f} {inner:.2f} 0 0 0 {inner_start[0]:.2f} {inner_start[1]:.2f} Z"
        )
    sweep = min(math.tau, sweep)
    adjusted_end = start + sweep
    large = 1 if sweep > math.pi else 0
    outer_start = (cx + outer * math.cos(start), cy + outer * math.sin(start))
    outer_end = (cx + outer * math.cos(adjusted_end), cy + outer * math.sin(adjusted_end))
    inner_end = (cx + inner * math.cos(adjusted_end), cy + inner * math.sin(adjusted_end))
    inner_start = (cx + inner * math.cos(start), cy + inner * math.sin(start))
    return (
        f"M {outer_start[0]:.2f} {outer_start[1]:.2f} "
        f"A {outer:.2f} {outer:.2f} 0 {large} 1 {outer_end[0]:.2f} {outer_end[1]:.2f} "
        f"L {inner_end[0]:.2f} {inner_end[1]:.2f} "
        f"A {inner:.2f} {inner:.2f} 0 {large} 0 {inner_start[0]:.2f} {inner_start[1]:.2f} Z"
    )


def _finite_number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _number(value: float | None) -> str:
    if value is None:
        return ""
    return f"{value:.4g}"


def _text(value: Any) -> str:
    return (
        str(value).strip()
        if isinstance(value, (str, int, float)) and not isinstance(value, bool)
        else ""
    )


def _optional_text(value: Any) -> str | None:
    text = _text(value)
    return text or None


def _strict_optional_text(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    return text or None


MediaProxyFactory = Callable[[MediaReference], str | None]
