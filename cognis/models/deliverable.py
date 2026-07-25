"""Deliverable domain models."""

from __future__ import annotations

import html
import json
import math
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any, Literal, NoReturn
from urllib.parse import unquote, urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, field_validator

from cognis.rendering.rich_visuals import normalize_chart


class DeliverableFormat(StrEnum):
    """Supported deliverable render formats."""

    MARKDOWN = "markdown"
    PLAIN = "plain"
    HTML = "html"
    RICH = "rich"


RICH_DELIVERABLE_MAX_BYTES = 256_000
RICH_DELIVERABLE_PROJECTION_MAX_BYTES = 64_000
RICH_DELIVERABLE_MAX_BLOCKS = 64
RICH_DELIVERABLE_MAX_DATASET_ROWS = 2_000
RICH_DELIVERABLE_MAX_STRING_LENGTH = 16_384
CANONICAL_CHART_TYPES = {
    "line",
    "area",
    "bar",
    "grouped_bar",
    "stacked_bar",
    "sparkline",
    "progress",
    "range",
    "donut",
}
CHART_AXIS_TYPES = ("time", "category", "linear")
CHART_LEGEND_POSITIONS = ("top", "right", "bottom", "none")
CHART_PALETTE_TOKENS = ("default", "cool", "warm", "categorical")
LEGACY_CHART_FIELDS = ("data", "x_key", "y_key", "series_key")

_CHART_AXIS_SCHEMA: dict[str, Any] = {
    "oneOf": [
        {"type": "null"},
        {
            "type": "object",
            "properties": {
                "type": {"type": "string", "enum": list(CHART_AXIS_TYPES)},
                "label": {"type": ["string", "null"]},
                "unit": {"type": ["string", "null"]},
                "min": {"type": ["number", "null"]},
                "max": {"type": ["number", "null"]},
            },
            "additionalProperties": False,
        },
    ]
}

CANONICAL_CHART_BLOCK_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "type": {"const": "chart"},
        "title": {"type": "string"},
        "description": {"type": ["string", "null"]},
        "spec_version": {"const": "cognis.chart.v1"},
        "chart_type": {"type": "string", "enum": sorted(CANONICAL_CHART_TYPES)},
        "series": {
            "type": "array",
            "minItems": 1,
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": ["string", "null"]},
                    "label": {"type": ["string", "null"]},
                    "stack": {"type": ["string", "null"]},
                    "points": {
                        "type": "array",
                        "minItems": 1,
                        "items": {
                            "type": "object",
                            "properties": {
                                "x": {"type": ["string", "number"]},
                                "y": {
                                    "oneOf": [
                                        {"type": "number"},
                                        {
                                            "type": "array",
                                            "items": {"type": "number"},
                                            "minItems": 2,
                                            "maxItems": 2,
                                        },
                                    ]
                                },
                                "label": {"type": ["string", "null"]},
                            },
                            "required": ["x", "y"],
                            "additionalProperties": False,
                        },
                    },
                },
                "required": ["points"],
                "additionalProperties": False,
            },
        },
        "x_axis": _CHART_AXIS_SCHEMA,
        "y_axis": _CHART_AXIS_SCHEMA,
        "stack": {"type": "boolean"},
        "legend_position": {
            "type": "string",
            "enum": list(CHART_LEGEND_POSITIONS),
        },
        "palette_token": {
            "type": "string",
            "enum": list(CHART_PALETTE_TOKENS),
        },
        "source_ids": {
            "type": "array",
            "items": {"type": "string", "minLength": 1},
        },
        "source": {"type": ["string", "null"]},
        "source_url": {"type": ["string", "null"]},
        "observed_at": {"type": ["string", "null"]},
    },
    "required": ["type", "spec_version", "chart_type", "series"],
    "not": {"anyOf": [{"required": [field]} for field in LEGACY_CHART_FIELDS]},
    "allOf": [
        {
            "if": {"properties": {"chart_type": {"const": "range"}}},
            "then": {
                "properties": {
                    "series": {
                        "items": {
                            "properties": {
                                "points": {
                                    "items": {
                                        "properties": {
                                            "y": {
                                                "type": "array",
                                                "items": {"type": "number"},
                                                "minItems": 2,
                                                "maxItems": 2,
                                            }
                                        }
                                    }
                                }
                            }
                        }
                    }
                }
            },
            "else": {
                "properties": {
                    "series": {
                        "items": {
                            "properties": {
                                "points": {"items": {"properties": {"y": {"type": "number"}}}}
                            }
                        }
                    }
                }
            },
        }
    ],
    "additionalProperties": False,
}

PULSE_RICH_BLOCK_INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "type": {"type": "string"},
        "blocks": {
            "type": "array",
            "items": {"$ref": "#/definitions/pulseRichBlock"},
        },
        "children": {
            "type": "array",
            "items": {"$ref": "#/definitions/pulseRichBlock"},
        },
    },
    "required": ["type"],
    "if": {"properties": {"type": {"const": "chart"}}, "required": ["type"]},
    "then": CANONICAL_CHART_BLOCK_SCHEMA,
    "allOf": [
        {
            "if": {"properties": {"type": {"const": "markdown"}}, "required": ["type"]},
            "then": {
                "properties": {"content": {"type": "string", "minLength": 1, "pattern": "\\S"}},
                "required": ["content"],
            },
        },
        {
            "if": {"properties": {"type": {"const": "mermaid"}}, "required": ["type"]},
            "then": {
                "properties": {
                    "source": {"type": "string", "minLength": 1, "pattern": "\\S"},
                    "code": {"type": "string", "minLength": 1, "pattern": "\\S"},
                    "content": {"type": "string", "minLength": 1, "pattern": "\\S"},
                },
                "anyOf": [
                    {"required": ["source"]},
                    {"required": ["code"]},
                    {"required": ["content"]},
                ],
            },
        },
        {
            "if": {"properties": {"type": {"enum": ["accordion", "gallery", "modal", "tabs"]}}},
            "then": {
                "properties": {
                    "items": {
                        "type": "array",
                        "items": {
                            "anyOf": [
                                {"$ref": "#/definitions/pulseRichBlock"},
                                {
                                    "type": "object",
                                    "not": {"required": ["type"]},
                                },
                            ]
                        },
                    }
                }
            },
        },
    ],
    "additionalProperties": True,
}

RICH_DELIVERABLE_VALID_EXAMPLE: dict[str, Any] = {
    "blocks": [{"type": "markdown", "content": "## Summary\n\nDeliverable body."}],
    "assets": [],
    "sources": [],
    "datasets": [],
    "exports": [],
    "metadata": {},
}

PULSE_V1_DAILY_SKELETON: dict[str, Any] = {
    "blocks": [
        {"type": "hero", "title": "Daily pulse", "subtitle": "Current context"},
        {
            "type": "grid",
            "blocks": [
                {"type": "metric", "label": "Signal 1", "value": "Value"},
                {"type": "metric", "label": "Signal 2", "value": "Value"},
                {"type": "metric", "label": "Signal 3", "value": "Value"},
            ],
        },
        {
            "type": "day_agenda",
            "title": "Today",
            "date": "2026-01-01",
            "timezone": "UTC",
            "now": "2026-01-01T08:00:00+00:00",
            "items": [],
            "tasks": [],
        },
        {
            "type": "columns",
            "blocks": [
                {
                    "type": "section",
                    "title": "Primary context",
                    "content": "Concise contextual synthesis.",
                },
                {
                    "type": "stack",
                    "title": "Actions",
                    "blocks": [
                        {"type": "card", "title": "Action", "content": "Concrete next step."}
                    ],
                },
            ],
        },
        {
            "type": "section",
            "title": "Know",
            "blocks": [
                {
                    "type": "card_grid",
                    "blocks": [{"type": "card", "title": "Item", "content": "Bounded summary."}],
                }
            ],
        },
        {
            "type": "section",
            "title": "Monitor",
            "blocks": [
                {
                    "type": "chart",
                    "title": "Signal trend",
                    "spec_version": "cognis.chart.v1",
                    "chart_type": "line",
                    "series": [
                        {
                            "id": "signal",
                            "label": "Signal",
                            "points": [{"x": "Now", "y": 1}],
                        }
                    ],
                    "x_axis": {"type": "category"},
                    "y_axis": {"type": "linear"},
                }
            ],
        },
        {"type": "callout", "title": "Closing signal", "content": "Decision-oriented close."},
        {"type": "source_list", "title": "Sources"},
    ],
    "assets": [],
    "sources": [{"id": "source-1", "title": "Source", "url": "https://source.invalid"}],
    "datasets": [],
    "exports": [],
    "metadata": {"presentation": "pulse", "pulse_variant": "daily"},
}

PULSE_DAILY_SKELETON: dict[str, Any] = {
    "blocks": [
        {
            "type": "hero",
            "eyebrow": "Decision brief",
            "title": "Daily pulse",
            "subtitle": "Current context and the decisions that matter",
        },
        {
            "type": "dashboard",
            "blocks": [
                {"type": "metric", "icon": "calendar", "label": "Agenda", "value": "2"},
                {"type": "metric", "icon": "info", "label": "Conditions", "value": "18 °C"},
                {"type": "metric", "icon": "activity", "label": "Market", "value": "Stable"},
                {"type": "metric", "icon": "check", "label": "Priority", "value": "Focus"},
            ],
        },
        {
            "type": "day_agenda",
            "title": "Today",
            "date": "2026-01-01",
            "timezone": "UTC",
            "now": "2026-01-01T08:00:00+00:00",
            "items": [],
            "tasks": [],
            "compact": True,
        },
        {
            "type": "columns",
            "blocks": [
                {
                    "type": "research_answer",
                    "eyebrow": "Editorial focus",
                    "title": "The decision to make first",
                    "answer": "A concise evidence-based synthesis.",
                    "source_ids": ["source-1"],
                },
                {
                    "type": "stack",
                    "title": "Actions",
                    "blocks": [
                        {
                            "type": "card",
                            "variant": "action",
                            "icon": "check",
                            "title": "Act now",
                            "content": "One concrete next step.",
                        },
                        {
                            "type": "card",
                            "variant": "status",
                            "tone": "neutral",
                            "title": "Prepare",
                            "content": "One bounded follow-up.",
                        },
                    ],
                },
            ],
        },
        {
            "type": "section",
            "title": "News and AI",
            "blocks": [
                {
                    "type": "accordion",
                    "title": "News",
                    "items": [
                        {
                            "type": "card",
                            "variant": "feature",
                            "icon": "info",
                            "title": "Decision-relevant story",
                            "content": (
                                "Concise impact and why it matters. "
                                "[Source](https://news.example.org/story)."
                            ),
                            "source_id": "source-2",
                            "citations": ["source-2"],
                            "url": "https://news.example.org/story",
                        },
                        {
                            "type": "card",
                            "variant": "editorial",
                            "title": "Second decision-relevant story",
                            "content": (
                                "Concise impact and why it matters. "
                                "[Source](https://news.example.org/second-story)."
                            ),
                            "source_id": "source-3",
                            "citations": ["source-3"],
                            "url": "https://news.example.org/second-story",
                        },
                    ],
                },
                {
                    "type": "accordion",
                    "title": "AI",
                    "items": [
                        {
                            "type": "card",
                            "variant": "editorial",
                            "icon": "activity",
                            "title": "Relevant AI change",
                            "content": (
                                "Operational impact in one paragraph. "
                                "[Source](https://ai.example.org/change)."
                            ),
                            "source_id": "source-4",
                            "citations": ["source-4"],
                            "url": "https://ai.example.org/change",
                        }
                    ],
                },
            ],
        },
        {
            "type": "section",
            "title": "Monitor",
            "blocks": [
                {
                    "type": "chart",
                    "title": "Meaningful trend",
                    "description": "A sourced series with enough points to show direction.",
                    "spec_version": "cognis.chart.v1",
                    "chart_type": "line",
                    "series": [
                        {
                            "id": "signal",
                            "label": "Signal",
                            "stack": "signals",
                            "points": [
                                {"x": "T-2", "y": 16},
                                {"x": "T-1", "y": 17},
                                {"x": "Now", "y": 18},
                            ],
                        }
                    ],
                    "x_axis": {"type": "category"},
                    "y_axis": {"type": "linear", "label": "Signal", "unit": "units"},
                    "stack": False,
                    "legend_position": "bottom",
                    "palette_token": "default",
                    "source_ids": ["source-5"],
                    "source": "Monitoring source",
                    "source_url": "https://monitor.example.org/series",
                    "observed_at": "2026-01-01T08:00:00+00:00",
                }
            ],
        },
        {
            "type": "callout",
            "title": "Closing signal",
            "content": "A decision-oriented close.",
        },
        {"type": "source_list", "title": "Sources", "numbered": True},
    ],
    "assets": [],
    "sources": [
        {
            "id": "source-1",
            "number": 1,
            "title": "Primary context",
            "url": "https://context.example.org/source",
        },
        {
            "id": "source-2",
            "number": 2,
            "title": "News source",
            "url": "https://news.example.org/story",
        },
        {
            "id": "source-3",
            "number": 3,
            "title": "Second news source",
            "url": "https://news.example.org/second-story",
        },
        {
            "id": "source-4",
            "number": 4,
            "title": "AI source",
            "url": "https://ai.example.org/change",
        },
        {
            "id": "source-5",
            "number": 5,
            "title": "Monitoring source",
            "url": "https://monitor.example.org/series",
        },
    ],
    "datasets": [],
    "exports": [],
    "metadata": {
        "presentation": "pulse",
        "pulse_variant": "daily",
        "pulse_version": 2,
    },
}

PULSE_PRESENTATION_DESCRIPTOR: dict[str, Any] = {
    "schema_version": "cognis.rich.pulse.v2",
    "presentation": "pulse",
    "summary": (
        "Decision-oriented Pulse v2 composition for new writes. Persisted Pulse v1 "
        "payloads remain renderable and valid under the legacy grammar."
    ),
    "selector": "$.rich.metadata.presentation == 'pulse'",
    "versions": {
        "1": {
            "status": "legacy_rendering",
            "selector": "metadata.pulse_version omitted or equal to 1",
        },
        "2": {
            "status": "authoritative_for_new_writes",
            "selector": "metadata.pulse_version == 2",
        },
    },
    "variants": {
        "daily": {
            "required_slots": [
                "hero",
                "icon_signal_dashboard",
                "compact_day_agenda",
                "visual_editorial_feature_and_actions",
                "cited_news_and_ai_accordions",
                "visual_monitoring",
                "closing_callout",
                "numbered_source_list",
            ]
        }
    },
    "quality_gate": {
        "non_agenda_visual_min": 1,
        "line_chart_points_min": 3,
        "chart_requires": ["source", "observed_at"],
        "all_story_items_linked_and_cited": True,
        "all_images_require": ["alt", "provenance"],
        "visual_editorial_cards": {
            "allowed": True,
            "requires": ["media.alt", "media.provenance"],
            "fallback": "feature card when an appropriate image is unavailable",
        },
        "progressive_disclosure_required_for_multiple_stories": True,
        "unavailable_signal_max": 1,
        "trivial_sample_or_count_charts_allowed": False,
        "metadata_counts": [
            "visual_count",
            "meaningful_chart_count",
            "cited_story_count",
            "uncited_story_count",
            "media_alt_count",
            "media_source_count",
            "collapsible_count",
            "unavailable_count",
        ],
    },
    "retry_guidance": (
        "Copy the valid skeleton, replace its placeholder values without changing the required "
        "slot order, and retry. To fall back explicitly, author a new generic rich payload "
        "with neither metadata.presentation nor metadata.pulse_variant."
    ),
    "errors": [
        {"reason": "unknown_rich_presentation", "path": "$.metadata.presentation"},
        {"reason": "invalid_pulse_composition", "path": "$.blocks"},
        {"reason": "invalid_pulse_block", "path": "$.blocks[*]"},
        {"reason": "pulse_content_too_large", "path": "$.blocks[*]"},
        {"reason": "invalid_pulse_markdown", "path": "$.blocks[*].content"},
    ],
    "valid_skeleton": PULSE_DAILY_SKELETON,
}

PULSE_WRITE_DELIVERABLE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "definitions": {"pulseRichBlock": PULSE_RICH_BLOCK_INPUT_SCHEMA},
    "properties": {
        "content": {
            "type": "string",
            "minLength": 1,
            "pattern": "\\S",
            "description": "Required accessible and channel-safe fallback content.",
        },
        "format": {"const": "rich"},
        "rich": {
            "type": "object",
            "properties": {
                "blocks": {
                    "type": "array",
                    "minItems": 7,
                    "maxItems": 10,
                    "items": PULSE_RICH_BLOCK_INPUT_SCHEMA,
                },
                "assets": {"type": "array", "items": {"type": "object"}},
                "sources": {"type": "array", "items": {"type": "object"}},
                "datasets": {"type": "array", "items": {"type": "object"}},
                "exports": {"type": "array", "items": {"type": "object"}},
                "metadata": {
                    "type": "object",
                    "properties": {
                        "presentation": {"const": "pulse"},
                        "pulse_variant": {"type": "string", "enum": ["daily"]},
                        "pulse_version": {"const": 2},
                        "toc": {"type": "boolean", "enum": [False]},
                        "show_toc": {"type": "boolean", "enum": [False]},
                    },
                    "required": ["presentation", "pulse_version"],
                },
            },
            "required": ["blocks", "metadata"],
        },
        "title": {"type": "string"},
        "target": {"type": "string", "enum": ["channel", "none"]},
        "outputs": {"type": "object"},
    },
    "required": ["content", "format", "rich"],
}

SUPPORTED_RICH_BLOCK_TYPES = {
    "hero",
    "section",
    "stack",
    "columns",
    "grid",
    "tabs",
    "accordion",
    "modal",
    "markdown",
    "callout",
    "card",
    "card_grid",
    "dashboard",
    "status",
    "status_grid",
    "action",
    "metric",
    "kv",
    "key_value",
    "timeline",
    "steps",
    "day_agenda",
    "incident_timeline",
    "incident_checklist",
    "checklist",
    "quote",
    "divider",
    "figure",
    "gallery",
    "table",
    "comparison_matrix",
    "decision_matrix",
    "research_answer",
    "evidence_report",
    "claim_cards",
    "chart",
    "mermaid",
    "link",
    "link_preview",
    "source_list",
    "code",
}

_CHILD_BLOCK_KEYS = ("blocks", "children")
_ITEM_BACKED_BLOCK_TYPES = {"accordion", "gallery", "modal", "tabs"}
_ARRAY_OBJECT_KEYS = {"assets", "sources", "datasets", "exports"}
_GENERIC_RICH_STRING_FIELDS = {"variant", "dek", "summary", "href", "tone"}
_FORBIDDEN_RICH_EMBED_FIELDS = {"html", "svg", "css", "style"}


def _json_size_bytes(value: Any) -> int:
    return len(json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))


class RichPayloadValidationError(ValueError):
    """Strict rich deliverable validation failure with model-retry guidance."""

    def __init__(
        self,
        *,
        reason: str,
        path: str,
        expected: str,
        received: Any | None = None,
        issues: list[dict[str, str]] | None = None,
        descriptor: dict[str, Any] | None = None,
    ) -> None:
        self.issues = issues or [{"reason": reason, "path": path, "expected": expected}]
        first = self.issues[0]
        reason = first["reason"]
        path = first["path"]
        expected = first["expected"]
        self.reason = reason
        self.path = path
        self.expected = expected
        self.received_type = type(received).__name__ if received is not None else None
        self.descriptor = descriptor
        super().__init__(f"{reason} at {path}: expected {expected}")

    def to_tool_result(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "status": "rejected",
            "reason": self.reason,
            "path": self.path,
            "expected": self.expected,
            "issues": self.issues,
            "retry_guidance": (
                "Reissue write_deliverable(format='rich') with a renderer-neutral "
                "object matching the expected shape. Keep the required fallback "
                "content string in the top-level content argument."
            ),
            "valid_example": RICH_DELIVERABLE_VALID_EXAMPLE,
        }
        if self.descriptor is not None:
            result["retry_guidance"] = self.descriptor["retry_guidance"]
            result["valid_skeleton"] = self.descriptor["valid_skeleton"]
            result["presentation_descriptor"] = self.descriptor
        if self.received_type is not None:
            result["received_type"] = self.received_type
        return result


def _json_path(parent: str, child: str | int) -> str:
    if isinstance(child, int):
        return f"{parent}[{child}]"
    return f"{parent}.{child}" if parent else f"$.{child}"


@dataclass(frozen=True, slots=True)
class RichPresentationContract:
    """One registered presentation grammar applied after generic normalization."""

    name: str
    descriptor: dict[str, Any]
    validator: Callable[[dict[str, Any]], None]


_RICH_PRESENTATIONS: dict[str, RichPresentationContract] = {}


def register_rich_presentation(contract: RichPresentationContract) -> None:
    """Register one authoritative rich presentation contract."""

    if contract.name in _RICH_PRESENTATIONS:
        raise ValueError(f"rich presentation already registered: {contract.name}")
    _RICH_PRESENTATIONS[contract.name] = contract


def rich_presentation_descriptor(name: str) -> dict[str, Any] | None:
    contract = _RICH_PRESENTATIONS.get(name)
    return contract.descriptor if contract is not None else None


def _pulse_issue(
    path: str, expected: str, reason: str = "invalid_pulse_composition"
) -> dict[str, str]:
    return {"reason": reason, "path": path, "expected": expected}


def _rendered_nested_blocks(block: dict[str, Any]) -> list[dict[str, Any]]:
    """Return every child block the renderer will dispatch for this block."""

    raw_blocks = block.get("blocks")
    children = raw_blocks if isinstance(raw_blocks, list) else block.get("children")
    nested = (
        [child for child in children if isinstance(child, dict)]
        if isinstance(children, list)
        else []
    )
    items = block.get("items")
    if block.get("type") in _ITEM_BACKED_BLOCK_TYPES and isinstance(items, list):
        nested.extend(item for item in items if isinstance(item, dict))
    return nested


def _count_block_types(block: dict[str, Any], block_types: set[str]) -> int:
    return int(block.get("type") in block_types) + sum(
        _count_block_types(child, block_types) for child in _rendered_nested_blocks(block)
    )


def _contains_block_type(block: dict[str, Any], block_types: set[str]) -> bool:
    return _count_block_types(block, block_types) > 0


def _pulse_markdown_issues(value: Any, path: str = "$") -> list[dict[str, str]]:
    issues: list[dict[str, str]] = []
    if isinstance(value, dict):
        if value.get("type") == "markdown":
            issues.append(
                _pulse_issue(
                    f"{path}.type",
                    "structured Pulse block instead of a markdown block",
                    "invalid_pulse_markdown",
                )
            )
        for key in ("content", "text", "body"):
            content = value.get(key)
            if not isinstance(content, str):
                continue
            if len(content) > 2_400:
                issues.append(
                    _pulse_issue(
                        f"{path}.{key}",
                        "concise content <= 2400 characters",
                        "pulse_content_too_large",
                    )
                )
            if re.search(r"(?im)^#{1,3}\s+(?:contents|table of contents)\s*$", content):
                issues.append(
                    _pulse_issue(
                        f"{path}.{key}",
                        "no table of contents",
                        "invalid_pulse_markdown",
                    )
                )
            if re.search(r"(?m)^#{1,3}\s+\d+(?:\.\d+)*[.)]?\s+", content):
                issues.append(
                    _pulse_issue(
                        f"{path}.{key}",
                        "no academic-numbered headings",
                        "invalid_pulse_markdown",
                    )
                )
        for key, child in value.items():
            if isinstance(child, (dict, list)):
                issues.extend(_pulse_markdown_issues(child, _json_path(path, str(key))))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            issues.extend(_pulse_markdown_issues(child, _json_path(path, index)))
    return issues


def _walk_rendered_blocks(
    blocks: list[dict[str, Any]],
    *,
    parent_path: str = "$.blocks",
    inside_agenda: bool = False,
) -> list[tuple[dict[str, Any], str, bool]]:
    walked: list[tuple[dict[str, Any], str, bool]] = []
    for index, block in enumerate(blocks):
        path = f"{parent_path}[{index}]"
        block_inside_agenda = inside_agenda or block.get("type") == "day_agenda"
        walked.append((block, path, block_inside_agenda))
        children = _rendered_nested_blocks(block)
        if children:
            child_key = (
                "items"
                if block.get("type") in _ITEM_BACKED_BLOCK_TYPES
                and isinstance(block.get("items"), list)
                else "blocks"
            )
            walked.extend(
                _walk_rendered_blocks(
                    children,
                    parent_path=f"{path}.{child_key}",
                    inside_agenda=block_inside_agenda,
                )
            )
    return walked


def _nonempty_text(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _chart_data(block: dict[str, Any]) -> list[dict[str, Any]]:
    """Return points from the canonical chart series shape."""

    chart = normalize_chart(block)
    if chart is None:
        return []
    return [
        {
            "x": point.x,
            "y": list(point.y) if isinstance(point.y, tuple) else point.y,
        }
        for series in chart.series
        for point in series.points
    ]


def _usable_chart_data(block: dict[str, Any]) -> list[dict[str, Any]]:
    usable: list[dict[str, Any]] = []
    for item in _chart_data(block):
        x = item.get("x")
        value = item.get("y")
        if (
            not (
                _nonempty_text(x)
                or (isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x))
            )
            or isinstance(value, bool)
            or not isinstance(value, (int, float, list))
        ):
            continue
        values = value if isinstance(value, list) and len(value) == 2 else [value]
        if not all(
            isinstance(candidate, (int, float))
            and not isinstance(candidate, bool)
            and math.isfinite(candidate)
            for candidate in values
        ):
            continue
        usable.append(item)
    return usable


def _iso_timestamp_with_offset(value: Any) -> bool:
    if not _nonempty_text(value):
        return False
    try:
        parsed = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
    except ValueError:
        return False
    return parsed.tzinfo is not None


def _renderable_figure_source(block: dict[str, Any]) -> bool:
    source = block.get("src") if _nonempty_text(block.get("src")) else block.get("url")
    if not _nonempty_text(source):
        return False
    normalized = str(source).strip()
    if re.match(r"^data:image/(?:png|jpeg|gif|webp);base64,[a-z0-9+/=\s]+$", normalized, re.I):
        return True
    if not normalized.lower().startswith("data:image/svg+xml,"):
        return False
    svg = unquote(normalized.split(",", 1)[1]).strip()
    return bool(
        svg.lower().startswith("<svg")
        and len(svg) <= 100_000
        and re.search(
            r"<(?:script|foreignobject|image|use)\b|"
            r"\bon[a-z]+\s*=|\b(?:href|xlink:href)\s*=|url\s*\(",
            svg,
            re.I,
        )
        is None
    )


_TRIVIAL_CHART_RE = re.compile(
    r"\b(?:sample|collector|source|story|item|result|unavailable)\s+(?:count|total)s?\b",
    re.IGNORECASE,
)


def _renderable_media(block: dict[str, Any]) -> bool:
    media = block.get("media")
    if not isinstance(media, dict):
        return False
    if any(_nonempty_text(media.get(key)) for key in ("ref", "artifact_id", "content_ref", "key")):
        return True
    source = next(
        (
            str(media[key]).strip()
            for key in ("src", "url", "href")
            if _nonempty_text(media.get(key))
        ),
        "",
    )
    return bool(re.match(r"^https://[^\s]+$", source, re.IGNORECASE))


def _chart_quality(block: dict[str, Any]) -> tuple[bool, list[str]]:
    data = _usable_chart_data(block)
    issues: list[str] = []
    chart_type = str(block.get("chart_type") or "").strip().lower()
    if chart_type == "line" and (
        len(data) < 3 or len({str(item["x"]).strip() for item in data}) < 3
    ):
        issues.append("line chart with at least 3 usable observations")
    elif chart_type != "line" and len(data) < 2:
        issues.append("chart with at least 2 usable observations")
    values = [
        candidate
        for item in data
        for candidate in (item["y"] if isinstance(item["y"], list) else [item["y"]])
    ]
    if not any(value != 0 for value in values):
        issues.append("chart with non-trivial values")
    if not (
        _nonempty_text(block.get("source"))
        or _nonempty_text(block.get("source_id"))
        or _nonempty_text(block.get("source_url"))
        or (
            isinstance(block.get("source_ids"), list)
            and any(_nonempty_text(source_id) for source_id in block["source_ids"])
        )
    ):
        issues.append("chart source")
    if not _iso_timestamp_with_offset(block.get("observed_at")):
        issues.append("chart ISO-8601 timestamp with explicit offset")
    chart_text = " ".join(str(block.get(key) or "") for key in ("title", "description", "label"))
    if _TRIVIAL_CHART_RE.search(chart_text):
        issues.append("decision-relevant chart instead of a sample/count chart")
    return not issues, issues


def _story_source_refs(block: dict[str, Any]) -> set[str]:
    refs: set[str] = set()
    source_id = block.get("source_id")
    if _nonempty_text(source_id):
        refs.add(str(source_id).strip())
    source_ids = block.get("source_ids")
    if isinstance(source_ids, list):
        refs.update(str(item).strip() for item in source_ids if _nonempty_text(item))
    citations = block.get("citations")
    if isinstance(citations, list):
        for citation in citations:
            if _nonempty_text(citation):
                refs.add(str(citation).strip())
            elif isinstance(citation, dict) and _nonempty_text(citation.get("source_id")):
                refs.add(str(citation["source_id"]).strip())
    return refs


def _absolute_url(value: Any) -> bool:
    return _nonempty_text(value) and re.match(r"^https?://[^\s]+$", str(value).strip()) is not None


def _story_has_link(block: dict[str, Any], allowed_urls: set[str]) -> bool:
    destinations: set[str] = set()
    from cognis.rendering.deliverables import _markdown_to_html

    for key in ("content", "answer", "text", "body"):
        value = block.get(key)
        if not isinstance(value, str):
            continue
        destinations.update(
            html.unescape(match.group(1))
            for match in re.finditer(
                r"<a\s+[^>]*href=[\"'](https?://[^\"']+)[\"']",
                _markdown_to_html(value),
                re.IGNORECASE,
            )
        )
    return bool(destinations & allowed_urls)


def _media_quality_items(payload: dict[str, Any]) -> list[dict[str, Any]]:
    rendered: list[dict[str, Any]] = []
    for block, _path, _inside_agenda in _walk_rendered_blocks(payload.get("blocks", [])):
        media = block.get("media")
        if isinstance(media, dict):
            rendered.append({"type": block.get("type"), **media})
        elif block.get("type") == "figure":
            rendered.append(block)
    assets = [
        asset
        for asset in payload.get("assets", [])
        if isinstance(asset, dict)
        and str(asset.get("type") or asset.get("kind") or "").lower() in {"image", "photo"}
    ]
    return [*rendered, *assets]


def pulse_quality_metadata(payload: dict[str, Any]) -> dict[str, Any]:
    """Return structural, measurable Pulse quality evidence for evaluators."""

    walked = _walk_rendered_blocks(payload.get("blocks", []))
    charts = [block for block, _path, _agenda in walked if block.get("type") == "chart"]
    meaningful_charts = [block for block in charts if _chart_quality(block)[0]]
    rendered_media = [
        block
        for block, _path, inside_agenda in walked
        if not inside_agenda
        and (
            (block.get("type") == "figure" and _renderable_figure_source(block))
            or _renderable_media(block)
        )
    ]
    accordions = [block for block, _path, _agenda in walked if block.get("type") == "accordion"]
    story_items = [
        item
        for accordion in accordions
        for item in _rendered_nested_blocks(accordion)
        if item.get("type") in {"card", "section", "research_answer"}
    ]
    source_urls = {
        str(source["id"]).strip(): str(source["url"]).strip()
        for source in payload.get("sources", [])
        if isinstance(source, dict)
        and _nonempty_text(source.get("id"))
        and _absolute_url(source.get("url"))
    }
    cited_stories = [
        story
        for story in story_items
        if (refs := _story_source_refs(story) & set(source_urls))
        and _story_has_link(
            story,
            {
                *(
                    str(story[key]).strip()
                    for key in ("url", "source_url")
                    if _absolute_url(story.get(key))
                ),
                *(source_urls[ref] for ref in refs),
            },
        )
    ]
    media_items = _media_quality_items(payload)
    unavailable_blocks = [
        block
        for block, _path, _agenda in walked
        if str(block.get("status") or "").lower() == "unavailable"
        or block.get("degraded_data") is True
    ]
    return {
        "visual_count": len(rendered_media) + len(meaningful_charts),
        "meaningful_chart_count": len(meaningful_charts),
        "cited_story_count": len(cited_stories),
        "uncited_story_count": len(story_items) - len(cited_stories),
        "article_count": len(story_items),
        "article_media_count": sum(_renderable_media(story) for story in story_items),
        "article_citation_count": len(cited_stories),
        "media_count": len(media_items),
        "media_alt_count": sum(_nonempty_text(item.get("alt")) for item in media_items),
        "media_source_count": sum(
            _nonempty_text(item.get("provenance"))
            or _nonempty_text(item.get("source"))
            or _nonempty_text(item.get("source_url"))
            for item in media_items
        ),
        "source_count": len(payload.get("sources", [])),
        "collapsible_count": len(accordions),
        "unavailable_count": len(unavailable_blocks),
    }


def _validate_pulse_v2(payload: dict[str, Any]) -> None:
    blocks = payload["blocks"]
    metadata = payload["metadata"]
    issues: list[dict[str, str]] = []
    expected_types = [
        "hero",
        "dashboard",
        "day_agenda",
        "columns",
        "section",
        "section",
        "callout",
        "source_list",
    ]
    types = [str(block.get("type") or "") for block in blocks]
    if metadata.get("pulse_variant") != "daily":
        issues.append(_pulse_issue("$.metadata.pulse_variant", "'daily' for Pulse v2"))
    if types != expected_types:
        issues.append(
            _pulse_issue(
                "$.blocks",
                "exact Pulse v2 daily slot sequence from the returned valid_skeleton",
            )
        )
    if sum(_count_block_types(block, {"hero"}) for block in blocks) != 1:
        issues.append(_pulse_issue("$.blocks[0]", "exactly one hero in the first slot"))

    if len(blocks) > 1:
        signal_children = _rendered_nested_blocks(blocks[1])
        metrics = [child for child in signal_children if child.get("type") == "metric"]
        if len(metrics) not in {3, 4} or len(metrics) != len(signal_children):
            issues.append(
                _pulse_issue(
                    "$.blocks[1].blocks",
                    "exactly 3-4 metric blocks in the icon signal dashboard",
                    "invalid_pulse_block",
                )
            )
        for index, metric in enumerate(metrics):
            if not _nonempty_text(metric.get("icon")):
                issues.append(
                    _pulse_issue(
                        f"$.blocks[1].blocks[{index}].icon",
                        "non-empty icon identifier",
                        "invalid_pulse_block",
                    )
                )

    if len(blocks) > 2:
        agenda = blocks[2]
        if agenda.get("compact") is not True:
            issues.append(_pulse_issue("$.blocks[2].compact", "true"))
        if len(agenda.get("items", [])) > 6 or len(agenda.get("tasks", [])) > 4:
            issues.append(
                _pulse_issue(
                    "$.blocks[2]",
                    "compact agenda with at most 6 items and 4 tasks",
                    "pulse_content_too_large",
                )
            )

    if len(blocks) > 3:
        feature_children = _rendered_nested_blocks(blocks[3])
        if (
            len(feature_children) != 2
            or feature_children[0].get("type") not in {"card", "research_answer"}
            or feature_children[1].get("type") != "stack"
        ):
            issues.append(
                _pulse_issue(
                    "$.blocks[3].blocks",
                    "feature card/research_answer followed by an action stack",
                    "invalid_pulse_block",
                )
            )
        elif not 1 <= len(_rendered_nested_blocks(feature_children[1])) <= 3:
            issues.append(
                _pulse_issue(
                    "$.blocks[3].blocks[1].blocks",
                    "1-3 bounded action cards",
                    "invalid_pulse_block",
                )
            )
        else:
            for index, action in enumerate(_rendered_nested_blocks(feature_children[1])):
                if action.get("type") != "card" or _rendered_nested_blocks(action):
                    issues.append(
                        _pulse_issue(
                            f"$.blocks[3].blocks[1].blocks[{index}]",
                            "leaf action card",
                            "invalid_pulse_block",
                        )
                    )

    accordions: list[dict[str, Any]] = []
    if len(blocks) > 4:
        news_children = _rendered_nested_blocks(blocks[4])
        accordions = [child for child in news_children if child.get("type") == "accordion"]
        if len(accordions) != 2 or len(news_children) != len(accordions):
            issues.append(
                _pulse_issue(
                    "$.blocks[4].blocks",
                    "exactly two cited News and AI accordion groups with no direct story cards",
                    "invalid_pulse_block",
                )
            )

    sources = payload.get("sources", [])
    source_urls = {
        str(source["id"]).strip(): str(source["url"]).strip()
        for source in sources
        if isinstance(source, dict)
        and _nonempty_text(source.get("id"))
        and _absolute_url(source.get("url"))
    }
    for accordion_index, accordion in enumerate(accordions):
        stories = _rendered_nested_blocks(accordion)
        if not stories:
            issues.append(
                _pulse_issue(
                    f"$.blocks[4].blocks[{accordion_index}].items",
                    "at least one linked and cited story",
                )
            )
        for story_index, story in enumerate(stories):
            path = f"$.blocks[4].blocks[{accordion_index}].items[{story_index}]"
            if story.get("type") not in {"card", "section", "research_answer"}:
                issues.append(
                    _pulse_issue(
                        f"{path}.type",
                        "card, section, or research_answer story",
                        "invalid_pulse_block",
                    )
                )
            if _rendered_nested_blocks(story):
                issues.append(
                    _pulse_issue(
                        f"{path}.blocks",
                        "leaf story block without nested rendered stories",
                        "invalid_pulse_block",
                    )
                )
            refs = _story_source_refs(story) & set(source_urls)
            if not refs:
                issues.append(_pulse_issue(f"{path}.source_id", "citation to a declared source"))
            story_urls = {
                str(story[key]).strip()
                for key in ("url", "source_url")
                if _absolute_url(story.get(key))
            }
            if not story_urls:
                issues.append(_pulse_issue(f"{path}.url", "absolute story URL"))
            if not _story_has_link(
                story,
                {*story_urls, *(source_urls[ref] for ref in refs)},
            ):
                issues.append(
                    _pulse_issue(
                        f"{path}.content",
                        "inline absolute Markdown link rendered in generic fallback",
                    )
                )

    walked = _walk_rendered_blocks(blocks)
    for block, path, _inside_agenda in walked:
        if block.get("type") != "card" or str(block.get("variant") or "").lower() != "visual":
            continue
        media = block.get("media")
        if not _renderable_media(block):
            issues.append(
                _pulse_issue(
                    f"{path}.media",
                    "renderer-safe image media for visual editorial card",
                    "invalid_pulse_visual",
                )
            )
            continue
        if not isinstance(media, dict) or not _nonempty_text(media.get("alt")):
            issues.append(_pulse_issue(f"{path}.media.alt", "specific non-empty image alt text"))
        if not isinstance(media, dict) or not (
            _nonempty_text(media.get("provenance"))
            or _nonempty_text(media.get("source"))
            or _nonempty_text(media.get("source_url"))
        ):
            issues.append(_pulse_issue(f"{path}.media.provenance", "image provenance/source"))
    for block, path, inside_agenda in walked:
        if block.get("type") != "chart" or inside_agenda:
            continue
        _meaningful, chart_issues = _chart_quality(block)
        for expected in chart_issues:
            issues.append(_pulse_issue(path, expected, "invalid_pulse_visual"))
    if len(blocks) > 5:
        monitor_quality = pulse_quality_metadata(
            {
                **payload,
                "blocks": [blocks[5]],
                "assets": [],
            }
        )
        if monitor_quality["visual_count"] < 1:
            issues.append(
                _pulse_issue(
                    "$.blocks[5]",
                    "monitoring section containing a non-agenda figure or meaningful chart",
                    "invalid_pulse_visual",
                )
            )

    media_items = _media_quality_items(payload)
    for index, media in enumerate(media_items):
        if (
            media.get("type") == "figure"
            and not _renderable_figure_source(media)
            and not any(
                _nonempty_text(media.get(key))
                for key in ("ref", "artifact_id", "content_ref", "key")
            )
        ):
            issues.append(
                _pulse_issue(
                    f"$.media[{index}].src",
                    "renderer-supported figure src or url",
                    "invalid_pulse_visual",
                )
            )
        if not _nonempty_text(media.get("alt")):
            issues.append(_pulse_issue(f"$.media[{index}].alt", "non-empty image alt text"))
        if not (
            _nonempty_text(media.get("provenance"))
            or _nonempty_text(media.get("source"))
            or _nonempty_text(media.get("source_url"))
        ):
            issues.append(
                _pulse_issue(
                    f"$.media[{index}].provenance",
                    "image provenance/source",
                )
            )

    quality = pulse_quality_metadata(payload)
    if quality["visual_count"] < 1:
        issues.append(
            _pulse_issue(
                "$.blocks[5]",
                "at least one non-agenda figure/artifact media or meaningful chart",
                "invalid_pulse_visual",
            )
        )
    if quality["uncited_story_count"] > 0:
        issues.append(_pulse_issue("$.blocks[4]", "all News and AI stories linked and cited"))
    if quality["unavailable_count"] > 1:
        issues.append(
            _pulse_issue(
                "$.blocks",
                "at most one compact unavailable/degraded-data signal",
                "pulse_content_too_large",
            )
        )

    if blocks and blocks[-1].get("numbered") is not True:
        issues.append(_pulse_issue("$.blocks[7].numbered", "true"))
    expected_numbers = list(range(1, len(sources) + 1))
    received_numbers = [
        source.get("number") if isinstance(source, dict) else None for source in sources
    ]
    if not sources or received_numbers != expected_numbers:
        issues.append(
            _pulse_issue(
                "$.sources",
                "non-empty sources numbered sequentially from 1",
            )
        )
    for index, source in enumerate(sources):
        if not isinstance(source, dict) or not (
            _nonempty_text(source.get("id"))
            and _nonempty_text(source.get("title"))
            and _absolute_url(source.get("url"))
        ):
            issues.append(
                _pulse_issue(
                    f"$.sources[{index}]",
                    "numbered source with non-empty id/title and absolute URL",
                )
            )

    toc = metadata.get("toc")
    if (toc is not None and toc is not False) or metadata.get("show_toc") is True:
        issues.append(_pulse_issue("$.metadata.toc", "false or omitted", "invalid_pulse_markdown"))
    issues.extend(_pulse_markdown_issues(blocks, "$.blocks"))
    if issues:
        raise RichPayloadValidationError(
            reason=issues[0]["reason"],
            path=issues[0]["path"],
            expected=issues[0]["expected"],
            issues=issues[:20],
            descriptor=PULSE_PRESENTATION_DESCRIPTOR,
        )


def _validate_pulse(payload: dict[str, Any]) -> None:
    blocks = payload["blocks"]
    metadata = payload["metadata"]
    version = metadata.get("pulse_version", 1)
    if version == 2:
        _validate_pulse_v2(payload)
        return
    if version != 1:
        raise RichPayloadValidationError(
            reason="unsupported_pulse_version",
            path="$.metadata.pulse_version",
            expected="2 for new writes, or 1/omitted for persisted Pulse v1",
            received=version,
            descriptor=PULSE_PRESENTATION_DESCRIPTOR,
        )
    issues: list[dict[str, str]] = []
    if not 7 <= len(blocks) <= 10:
        issues.append(_pulse_issue("$.blocks", "7-10 bounded top-level Pulse blocks"))
    types = [str(block.get("type") or "") for block in blocks]
    hero_count = sum(_count_block_types(block, {"hero"}) for block in blocks)
    if hero_count != 1 or not types or types[0] != "hero":
        issues.append(_pulse_issue("$.blocks[0]", "exactly one hero in the first slot"))
    if len(blocks) > 1:
        signal = blocks[1]
        signal_type = signal.get("type")
        signal_children = _rendered_nested_blocks(signal)
        metric_count = sum(child.get("type") == "metric" for child in signal_children)
        if signal_type not in {"grid", "dashboard"} or metric_count not in {3, 4}:
            issues.append(
                _pulse_issue(
                    "$.blocks[1]",
                    "one grid/dashboard containing exactly 3-4 metric blocks",
                    "invalid_pulse_block",
                )
            )
        if len(signal_children) != metric_count:
            issues.append(
                _pulse_issue(
                    "$.blocks[1].blocks",
                    "metric blocks only",
                    "invalid_pulse_block",
                )
            )
    if len(blocks) >= 2:
        if blocks[-2].get("type") != "callout":
            issues.append(_pulse_issue(f"$.blocks[{len(blocks) - 2}]", "closing callout"))
        if blocks[-1].get("type") != "source_list":
            issues.append(
                _pulse_issue(f"$.blocks[{len(blocks) - 1}]", "source_list as final block")
            )

    middle = blocks[2:-2]
    allowed_middle = {"day_agenda", "columns", "section", "stack", "card_grid", "dashboard"}
    for index, block in enumerate(middle, start=2):
        if block.get("type") not in allowed_middle:
            issues.append(
                _pulse_issue(
                    f"$.blocks[{index}].type",
                    f"one of {sorted(allowed_middle)}",
                    "invalid_pulse_block",
                )
            )
        children = _rendered_nested_blocks(block)
        if len(children) > 6:
            issues.append(
                _pulse_issue(
                    f"$.blocks[{index}].blocks",
                    "at most 6 child blocks",
                    "pulse_content_too_large",
                )
            )
        if block.get("type") == "columns" and not 2 <= len(children) <= 3:
            issues.append(
                _pulse_issue(
                    f"$.blocks[{index}].blocks",
                    "2-3 column children",
                    "invalid_pulse_block",
                )
            )

    monitor_types = {"figure", "chart", "metric"}
    if not any(_contains_block_type(block, monitor_types) for block in middle):
        issues.append(
            _pulse_issue(
                "$.blocks",
                "at least one monitoring figure, chart, or metric in the content slots",
            )
        )

    toc = metadata.get("toc")
    if (toc is not None and toc is not False) or metadata.get("show_toc") is True:
        issues.append(_pulse_issue("$.metadata.toc", "false or omitted", "invalid_pulse_markdown"))
    publication = metadata.get("publication")
    if publication is True or (
        isinstance(publication, dict)
        and any(
            publication.get(key) is True for key in ("numbering", "number_figures", "number_tables")
        )
    ):
        issues.append(
            _pulse_issue(
                "$.metadata.publication",
                "academic numbering disabled",
                "invalid_pulse_markdown",
            )
        )
    issues.extend(_pulse_markdown_issues(blocks, "$.blocks"))

    variant = metadata.get("pulse_variant")
    if variant is not None and variant != "daily":
        issues.append(_pulse_issue("$.metadata.pulse_variant", "'daily' or omitted"))
    if variant == "daily":
        daily_types: list[str | tuple[str, ...]] = [
            "hero",
            ("grid", "dashboard"),
            "day_agenda",
            "columns",
            "section",
            "section",
            "callout",
            "source_list",
        ]
        if len(types) != len(daily_types):
            issues.append(
                _pulse_issue(
                    "$.blocks",
                    "exact daily slot sequence from the returned valid_skeleton",
                )
            )
        else:
            for index, expected in enumerate(daily_types):
                accepted: set[str] = set(expected) if isinstance(expected, tuple) else {expected}
                if types[index] not in accepted:
                    issues.append(
                        _pulse_issue(
                            f"$.blocks[{index}].type",
                            f"daily slot {index}: one of {sorted(accepted)}",
                        )
                    )
        if types.count("day_agenda") != 1:
            issues.append(_pulse_issue("$.blocks[2]", "exactly one day_agenda in the daily slot"))
        if len(blocks) > 5 and not _contains_block_type(blocks[5], monitor_types):
            issues.append(
                _pulse_issue(
                    "$.blocks[5]",
                    "daily monitoring section containing a figure, chart, or metric",
                )
            )

    if issues:
        raise RichPayloadValidationError(
            reason=issues[0]["reason"],
            path=issues[0]["path"],
            expected=issues[0]["expected"],
            issues=issues[:12],
            descriptor=PULSE_PRESENTATION_DESCRIPTOR,
        )


register_rich_presentation(
    RichPresentationContract(
        name="pulse",
        descriptor=PULSE_PRESENTATION_DESCRIPTOR,
        validator=_validate_pulse,
    )
)


def _validate_string_caps(value: Any, path: str) -> None:
    if isinstance(value, str):
        if len(value) > RICH_DELIVERABLE_MAX_STRING_LENGTH:
            raise RichPayloadValidationError(
                reason="rich_string_too_long",
                path=path,
                expected=f"string length <= {RICH_DELIVERABLE_MAX_STRING_LENGTH} characters",
                received=value,
            )
        return
    if isinstance(value, list):
        for index, item in enumerate(value):
            _validate_string_caps(item, _json_path(path, index))
        return
    if isinstance(value, dict):
        for key, item in value.items():
            _validate_string_caps(item, _json_path(path, str(key)))


def _validate_array_of_objects(value: Any, path: str) -> list[dict[str, Any]]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise RichPayloadValidationError(
            reason="invalid_rich_container",
            path=path,
            expected="array of objects",
            received=value,
        )
    normalized: list[dict[str, Any]] = []
    for index, item in enumerate(value):
        item_path = _json_path(path, index)
        if not isinstance(item, dict):
            raise RichPayloadValidationError(
                reason="invalid_rich_container_item",
                path=item_path,
                expected="object",
                received=item,
            )
        _validate_string_caps(item, item_path)
        normalized.append(dict(item))
    return normalized


def _dataset_row_count(datasets: list[dict[str, Any]]) -> int:
    total = 0
    for dataset in datasets:
        rows = dataset.get("rows")
        data = dataset.get("data")
        if isinstance(rows, list):
            total += len(rows)
        elif isinstance(data, list):
            total += len(data)
    return total


def _agenda_text(value: Any) -> str | None:
    if not isinstance(value, (str, int, float)) or isinstance(value, bool):
        return None
    text = str(value).strip()
    return text or None


_AGENDA_TIMESTAMP_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}"
    r"(?::\d{2}(?:\.\d{1,9})?)?(?:Z|[+-]\d{2}:\d{2})$",
    re.IGNORECASE,
)


def _agenda_timestamp(value: Any) -> str | None:
    """Accept a full ISO datetime with ``T`` and an explicit ``Z``/numeric offset."""
    text = _agenda_text(value)
    if text is None or _AGENDA_TIMESTAMP_RE.fullmatch(text) is None:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.isoformat() if parsed.tzinfo is not None else None


def _canonical_value(value: dict[str, Any], canonical: str, *aliases: str) -> Any:
    if canonical in value:
        return value[canonical]
    return next((value[alias] for alias in aliases if alias in value), None)


_AGENDA_TIMEZONE_RE = re.compile(r"^[A-Za-z0-9_+-]+(?:/[A-Za-z0-9_+-]+)*$")


def _agenda_timezone(value: Any) -> str | None:
    timezone = _agenda_text(value)
    if timezone is None or _AGENDA_TIMEZONE_RE.fullmatch(timezone) is None:
        return None
    try:
        ZoneInfo(timezone)
    except (ZoneInfoNotFoundError, ValueError, OSError):
        return None
    return timezone


def _normalize_day_agenda(block: dict[str, Any]) -> dict[str, Any]:
    normalized = dict(block)
    normalized["timezone"] = _agenda_timezone(block.get("timezone"))
    date = _agenda_text(block.get("date"))
    if date:
        try:
            date = datetime.strptime(date, "%Y-%m-%d").date().isoformat()
        except ValueError:
            date = None
    normalized["date"] = date
    normalized["now"] = _agenda_timestamp(_canonical_value(block, "now", "now_iso"))
    normalized.pop("now_iso", None)

    raw_items = _canonical_value(block, "items", "events")
    items: list[dict[str, Any]] = []
    if isinstance(raw_items, list):
        for raw in raw_items:
            if not isinstance(raw, dict):
                continue
            title = _agenda_text(_canonical_value(raw, "title", "label"))
            if title is None:
                continue
            all_day = _canonical_value(raw, "all_day", "allDay") is True
            start = _agenda_timestamp(_canonical_value(raw, "start", "start_iso", "start_time"))
            end = _agenda_timestamp(_canonical_value(raw, "end", "end_iso", "end_time"))
            if not all_day and start is None:
                continue
            if (
                start
                and end
                and datetime.fromisoformat(end).timestamp()
                < datetime.fromisoformat(start).timestamp()
            ):
                end = None
            item = {
                "title": title,
                "all_day": all_day,
                "start": start,
                "end": end,
                "location": _agenda_text(raw.get("location")),
                "description": _agenda_text(raw.get("description")),
                "kind": "free" if raw.get("kind") == "free" else "event",
                "source_id": _agenda_text(_canonical_value(raw, "source_id", "source")),
            }
            items.append({key: value for key, value in item.items() if value is not None})
    normalized["items"] = items
    normalized.pop("events", None)

    tasks: list[dict[str, Any]] = []
    if isinstance(block.get("tasks"), list):
        for raw in block["tasks"]:
            if not isinstance(raw, dict):
                continue
            title = _agenda_text(_canonical_value(raw, "title", "content"))
            if title is None:
                continue
            task = {
                "title": title,
                "due": _agenda_timestamp(_canonical_value(raw, "due", "due_at")),
                "priority": _agenda_text(raw.get("priority")),
                "source_id": _agenda_text(_canonical_value(raw, "source_id", "source")),
            }
            tasks.append({key: value for key, value in task.items() if value is not None})
    normalized["tasks"] = tasks
    source = block.get("source")
    if isinstance(source, dict):
        normalized["source"] = {
            key: text
            for key in ("id", "label", "url")
            if (text := _agenda_text(source.get(key))) is not None
        }
        refreshed_at = _agenda_timestamp(
            _canonical_value(source, "refreshed_at", "refreshed_at_iso")
        )
        if refreshed_at:
            normalized["source"]["refreshed_at"] = refreshed_at
    else:
        source_id = _agenda_text(source)
        normalized["source"] = {"id": source_id} if source_id else {}
    return normalized


def _chart_validation_error(
    *,
    reason: str,
    path: str,
    expected: str,
    received: Any,
) -> NoReturn:
    raise RichPayloadValidationError(
        reason=reason,
        path=path,
        expected=expected,
        received=received,
    )


def _is_finite_chart_number(value: Any) -> bool:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return False
    try:
        return math.isfinite(float(value))
    except OverflowError:
        return False


def _validate_chart_optional_string(block: dict[str, Any], path: str, key: str) -> None:
    value = block.get(key)
    if key in block and value is not None and not isinstance(value, str):
        _chart_validation_error(
            reason="invalid_chart_field",
            path=_json_path(path, key),
            expected="string, null, or omitted",
            received=value,
        )


def _validate_chart_axis(value: Any, path: str, *, default_type: str) -> str:
    if value is None:
        return default_type
    if not isinstance(value, dict):
        _chart_validation_error(
            reason="invalid_chart_axis",
            path=path,
            expected="axis object, null, or omitted",
            received=value,
        )
    unknown = set(value) - {"type", "label", "unit", "min", "max"}
    if unknown:
        key = sorted(unknown)[0]
        _chart_validation_error(
            reason="invalid_chart_axis_field",
            path=_json_path(path, key),
            expected="one of type, label, unit, min, max",
            received=value[key],
        )
    axis_type = value.get("type", default_type)
    if axis_type not in CHART_AXIS_TYPES:
        _chart_validation_error(
            reason="invalid_chart_axis_type",
            path=_json_path(path, "type"),
            expected=f"one of {list(CHART_AXIS_TYPES)}",
            received=axis_type,
        )
    for key in ("label", "unit"):
        field = value.get(key)
        if key in value and field is not None and not isinstance(field, str):
            _chart_validation_error(
                reason="invalid_chart_axis_field",
                path=_json_path(path, key),
                expected="string, null, or omitted",
                received=field,
            )
    for key in ("min", "max"):
        field = value.get(key)
        if key in value and field is not None and not _is_finite_chart_number(field):
            _chart_validation_error(
                reason="invalid_chart_axis_field",
                path=_json_path(path, key),
                expected="finite number, null, or omitted",
                received=field,
            )
    return str(axis_type)


def _validate_chart_x(value: Any, path: str, axis_type: str) -> None:
    if axis_type == "linear":
        valid = _is_finite_chart_number(value)
        expected = "finite number for a linear x-axis"
    elif axis_type == "time":
        valid = (
            isinstance(value, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}(?:T.*)?", value) is not None
        )
        if valid:
            try:
                datetime.fromisoformat(value.replace("Z", "+00:00"))
            except ValueError:
                valid = False
        expected = "ISO 8601 date or datetime string for a time x-axis"
    else:
        valid = (isinstance(value, str) and bool(value.strip())) or _is_finite_chart_number(value)
        expected = "non-empty string or finite number for a category x-axis"
    if not valid:
        _chart_validation_error(
            reason="invalid_chart_point_x",
            path=path,
            expected=expected,
            received=value,
        )


def _validate_chart_y(value: Any, path: str, chart_type: str) -> None:
    if chart_type == "range":
        valid = (
            isinstance(value, list)
            and len(value) == 2
            and all(_is_finite_chart_number(item) for item in value)
        )
        expected = "two-item array of finite numbers for chart_type='range'"
    else:
        valid = _is_finite_chart_number(value)
        expected = "finite number; two-item y ranges are only valid for chart_type='range'"
    if not valid:
        _chart_validation_error(
            reason="invalid_chart_point_y",
            path=path,
            expected=expected,
            received=value,
        )


def _validate_canonical_chart(block: dict[str, Any], path: str) -> None:
    for key in LEGACY_CHART_FIELDS:
        if key in block:
            _chart_validation_error(
                reason="legacy_chart_field",
                path=_json_path(path, key),
                expected=(
                    "remove this legacy field and migrate to cognis.chart.v1 using "
                    "spec_version, chart_type, series[].points[].x/y, x_axis, and y_axis"
                ),
                received=block[key],
            )
    unknown = set(block) - set(CANONICAL_CHART_BLOCK_SCHEMA["properties"])
    if unknown:
        key = sorted(unknown)[0]
        _chart_validation_error(
            reason="invalid_chart_field",
            path=_json_path(path, key),
            expected=(
                "canonical cognis.chart.v1 field; remove unknown chart fields "
                f"(allowed: {sorted(CANONICAL_CHART_BLOCK_SCHEMA['properties'])})"
            ),
            received=block[key],
        )
    spec_version = block.get("spec_version")
    if spec_version != "cognis.chart.v1":
        _chart_validation_error(
            reason="invalid_chart_spec_version",
            path=_json_path(path, "spec_version"),
            expected="literal 'cognis.chart.v1'",
            received=spec_version,
        )
    chart_type_value = block.get("chart_type")
    if not isinstance(chart_type_value, str) or chart_type_value not in CANONICAL_CHART_TYPES:
        _chart_validation_error(
            reason="invalid_chart_type",
            path=_json_path(path, "chart_type"),
            expected=f"one of {sorted(CANONICAL_CHART_TYPES)}",
            received=chart_type_value,
        )
    chart_type = str(chart_type_value)
    x_axis_type = _validate_chart_axis(
        block.get("x_axis"),
        _json_path(path, "x_axis"),
        default_type="category",
    )
    _validate_chart_axis(
        block.get("y_axis"),
        _json_path(path, "y_axis"),
        default_type="linear",
    )
    series = block.get("series")
    if not isinstance(series, list) or not series:
        _chart_validation_error(
            reason="invalid_chart_series",
            path=_json_path(path, "series"),
            expected="non-empty array of canonical chart series objects",
            received=series,
        )
    for series_index, item in enumerate(series):
        series_path = _json_path(_json_path(path, "series"), series_index)
        if not isinstance(item, dict):
            _chart_validation_error(
                reason="invalid_chart_series",
                path=series_path,
                expected="chart series object",
                received=item,
            )
        unknown = set(item) - {"id", "label", "points", "stack"}
        if unknown:
            key = sorted(unknown)[0]
            _chart_validation_error(
                reason="invalid_chart_series_field",
                path=_json_path(series_path, key),
                expected="one of id, label, points, stack",
                received=item[key],
            )
        for key in ("id", "label", "stack"):
            _validate_chart_optional_string(item, series_path, key)
        points = item.get("points")
        if not isinstance(points, list) or not points:
            _chart_validation_error(
                reason="invalid_chart_points",
                path=_json_path(series_path, "points"),
                expected="non-empty array of canonical {x, y, label?} point objects",
                received=points,
            )
        for point_index, point in enumerate(points):
            point_path = _json_path(_json_path(series_path, "points"), point_index)
            if not isinstance(point, dict):
                _chart_validation_error(
                    reason="invalid_chart_point",
                    path=point_path,
                    expected="chart point object with x and y",
                    received=point,
                )
            unknown = set(point) - {"x", "y", "label"}
            if unknown:
                key = sorted(unknown)[0]
                _chart_validation_error(
                    reason="invalid_chart_point_field",
                    path=_json_path(point_path, key),
                    expected="one of x, y, label",
                    received=point[key],
                )
            _validate_chart_x(point.get("x"), _json_path(point_path, "x"), x_axis_type)
            _validate_chart_y(point.get("y"), _json_path(point_path, "y"), chart_type)
            _validate_chart_optional_string(point, point_path, "label")
    if "stack" in block and not isinstance(block["stack"], bool):
        _chart_validation_error(
            reason="invalid_chart_stack",
            path=_json_path(path, "stack"),
            expected="boolean or omitted",
            received=block["stack"],
        )
    for key, values in (
        ("legend_position", CHART_LEGEND_POSITIONS),
        ("palette_token", CHART_PALETTE_TOKENS),
    ):
        value = block.get(key)
        if key in block and value not in values:
            _chart_validation_error(
                reason=f"invalid_chart_{key}",
                path=_json_path(path, key),
                expected=f"one of {list(values)}",
                received=value,
            )
    if "source_ids" in block and not (
        isinstance(block["source_ids"], list)
        and all(isinstance(item, str) and item.strip() for item in block["source_ids"])
    ):
        _chart_validation_error(
            reason="invalid_chart_source_ids",
            path=_json_path(path, "source_ids"),
            expected="array of non-empty source id strings or omitted",
            received=block["source_ids"],
        )
    title = block.get("title")
    if "title" in block and not isinstance(title, str):
        _chart_validation_error(
            reason="invalid_chart_field",
            path=_json_path(path, "title"),
            expected="string or omitted",
            received=title,
        )
    for key in ("description", "source", "source_url", "observed_at"):
        _validate_chart_optional_string(block, path, key)


def _normalize_block(block: Any, path: str, block_count: list[int]) -> dict[str, Any]:
    if not isinstance(block, dict):
        raise RichPayloadValidationError(
            reason="invalid_rich_block",
            path=path,
            expected="block object with a supported string type",
            received=block,
        )
    raw_type = block.get("type")
    if not isinstance(raw_type, str) or not raw_type.strip():
        raise RichPayloadValidationError(
            reason="invalid_rich_block_type",
            path=_json_path(path, "type"),
            expected=f"one of {sorted(SUPPORTED_RICH_BLOCK_TYPES)}",
            received=raw_type,
        )
    block_type = raw_type.strip()
    if block_type not in SUPPORTED_RICH_BLOCK_TYPES:
        raise RichPayloadValidationError(
            reason="unsupported_rich_block_type",
            path=_json_path(path, "type"),
            expected=f"one of {sorted(SUPPORTED_RICH_BLOCK_TYPES)}",
            received=raw_type,
        )
    block_count[0] += 1
    if block_count[0] > RICH_DELIVERABLE_MAX_BLOCKS:
        raise RichPayloadValidationError(
            reason="rich_block_count_exceeded",
            path=path,
            expected=f"at most {RICH_DELIVERABLE_MAX_BLOCKS} blocks including nested blocks",
            received=block,
        )

    normalized = dict(block)
    normalized["type"] = block_type
    if block_type == "chart":
        _validate_canonical_chart(normalized, path)
    if block_type == "markdown":
        content = normalized.get("content")
        if not isinstance(content, str) or not content.strip():
            raise RichPayloadValidationError(
                reason="missing_markdown_content",
                path=_json_path(path, "content"),
                expected="non-empty Markdown content string",
                received=content,
            )
    if block_type == "mermaid":
        source_keys = [key for key in ("source", "code", "content") if key in normalized]
        for key in source_keys:
            candidate = normalized[key]
            if not isinstance(candidate, str) or not candidate.strip():
                raise RichPayloadValidationError(
                    reason="invalid_mermaid_source",
                    path=_json_path(path, key),
                    expected="non-empty Mermaid source string",
                    received=candidate,
                )
        if not source_keys:
            raise RichPayloadValidationError(
                reason="missing_mermaid_source",
                path=_json_path(path, "source"),
                expected="non-empty Mermaid source string in source, code, or content",
                received=None,
            )
        normalized["source"] = normalized[source_keys[0]]
        normalized.pop("code", None)
    for key in _FORBIDDEN_RICH_EMBED_FIELDS & normalized.keys():
        raise RichPayloadValidationError(
            reason="unsafe_rich_embed",
            path=_json_path(path, key),
            expected="renderer-neutral fields; arbitrary HTML, SVG, CSS, and style are forbidden",
            received=normalized[key],
        )
    for key in _GENERIC_RICH_STRING_FIELDS:
        value = normalized.get(key)
        if value is not None and not isinstance(value, str):
            raise RichPayloadValidationError(
                reason="invalid_rich_block_field",
                path=_json_path(path, key),
                expected="string or omitted",
                received=value,
            )
    href = normalized.get("href")
    if isinstance(href, str) and not _safe_rich_url(href, allow_relative=True):
        raise RichPayloadValidationError(
            reason="unsafe_rich_url",
            path=_json_path(path, "href"),
            expected="http(s), mailto, absolute-path, fragment URL, or omitted",
            received=href,
        )
    icon = normalized.get("icon")
    if icon is not None and not isinstance(icon, (str, int, float, bool)):
        raise RichPayloadValidationError(
            reason="invalid_rich_icon",
            path=_json_path(path, "icon"),
            expected="JSON scalar (Unicode/emoji or renderer icon name) or omitted",
            received=icon,
        )
    for key in ("source_ids", "citations"):
        value = normalized.get(key)
        if value is not None and not (
            isinstance(value, str)
            or (
                isinstance(value, list)
                and all(isinstance(item, str) and item.strip() for item in value)
            )
        ):
            raise RichPayloadValidationError(
                reason="invalid_rich_citations",
                path=_json_path(path, key),
                expected="source id string, array of non-empty source id strings, or omitted",
                received=value,
            )
    media = normalized.get("media")
    if media is not None:
        media_path = _json_path(path, "media")
        if not isinstance(media, dict):
            raise RichPayloadValidationError(
                reason="invalid_rich_media",
                path=media_path,
                expected="object containing ref and optional alt/credit/source_url/role/aspect_ratio/focal_point",
                received=media,
            )
        if not any(
            isinstance(media.get(key), str) and media[key].strip()
            for key in ("ref", "artifact_id", "content_ref", "key")
        ):
            raise RichPayloadValidationError(
                reason="missing_rich_media_ref",
                path=_json_path(media_path, "ref"),
                expected="artifact-compatible ref (or controller-owned local key)",
                received=media,
            )
        for key in ("alt", "credit", "source_url", "role", "aspect_ratio"):
            value = media.get(key)
            if value is not None and not isinstance(value, str):
                raise RichPayloadValidationError(
                    reason="invalid_rich_media_field",
                    path=_json_path(media_path, key),
                    expected="string or omitted",
                    received=value,
                )
        source_url = media.get("source_url")
        if isinstance(source_url, str) and not _safe_rich_url(source_url, allow_relative=False):
            raise RichPayloadValidationError(
                reason="unsafe_rich_url",
                path=_json_path(media_path, "source_url"),
                expected="http(s) URL or omitted",
                received=source_url,
            )
        focal_point = media.get("focal_point")
        focal_valid = isinstance(focal_point, str) or (
            isinstance(focal_point, dict)
            and set(focal_point) <= {"x", "y"}
            and set(focal_point) == {"x", "y"}
            and all(
                isinstance(focal_point[axis], (int, float))
                and not isinstance(focal_point[axis], bool)
                and 0 <= focal_point[axis] <= 1
                for axis in ("x", "y")
            )
        )
        if focal_point is not None and not focal_valid:
            raise RichPayloadValidationError(
                reason="invalid_rich_media_focal_point",
                path=_json_path(media_path, "focal_point"),
                expected="string or object with numeric x/y coordinates in range 0..1",
                received=focal_point,
            )
    if block_type == "day_agenda":
        normalized = _normalize_day_agenda(normalized)
    _validate_string_caps(normalized, path)
    child_keys = list(_CHILD_BLOCK_KEYS)
    if block_type in _ITEM_BACKED_BLOCK_TYPES:
        child_keys.append("items")
    for key in child_keys:
        children = normalized.get(key)
        if children is None:
            continue
        child_path = _json_path(path, key)
        if not isinstance(children, list):
            raise RichPayloadValidationError(
                reason="invalid_rich_block_children",
                path=child_path,
                expected="array of supported block objects",
                received=children,
            )
        normalized_children = []
        for index, child in enumerate(children):
            if key == "items" and isinstance(child, dict) and "type" not in child:
                child = {
                    **child,
                    "type": "figure" if block_type == "gallery" else "section",
                }
            normalized_children.append(
                _normalize_block(child, _json_path(child_path, index), block_count)
            )
        normalized[key] = normalized_children
    return normalized


def _safe_rich_url(value: str, *, allow_relative: bool) -> bool:
    candidate = value.strip()
    if not candidate or any(ord(character) < 32 for character in candidate):
        return False
    if allow_relative and (candidate.startswith("/") or candidate.startswith("#")):
        return not candidate.startswith("//")
    parsed = urlsplit(candidate)
    allowed_schemes = {"http", "https", "mailto"} if allow_relative else {"http", "https"}
    scheme = parsed.scheme.lower()
    if scheme not in allowed_schemes:
        return False
    if scheme in {"http", "https"}:
        return bool(parsed.netloc)
    return bool(parsed.path)


def _rich_source_tokens(source: dict[str, Any]) -> set[str]:
    return {
        value.strip()
        for key in ("id", "key", "citation_id", "url", "href", "title", "name")
        if isinstance((value := source.get(key)), str) and value.strip()
    }


def _validate_source_list_references(
    blocks: list[dict[str, Any]],
    sources: list[dict[str, Any]],
    path: str = "$.blocks",
) -> None:
    available = (
        set().union(*(_rich_source_tokens(source) for source in sources)) if sources else set()
    )
    for block_index, block in enumerate(blocks):
        block_path = _json_path(path, block_index)
        if block.get("type") == "source_list":
            ref_key = next(
                (key for key in ("sources", "source_ids", "citations") if key in block),
                None,
            )
            refs = block.get(ref_key) if ref_key is not None else None
            if refs is not None:
                values = refs if isinstance(refs, list) else [refs]
                for ref_index, ref in enumerate(values):
                    ref_path = _json_path(_json_path(block_path, ref_key), ref_index)
                    if isinstance(ref, str):
                        token = ref.strip()
                    elif isinstance(ref, dict):
                        raw_token = ref.get("source_id") or ref.get("sourceId") or ref.get("ref")
                        token = raw_token.strip() if isinstance(raw_token, str) else ""
                        if not token and _rich_source_tokens(ref):
                            continue
                    else:
                        token = ""
                    if not token or token not in available:
                        raise RichPayloadValidationError(
                            reason="invalid_rich_source_reference",
                            path=ref_path,
                            expected=(
                                "document-level source id/title/URL or inline source record "
                                "with title/name/URL"
                            ),
                            received=ref,
                        )
        child_keys = list(_CHILD_BLOCK_KEYS)
        if block.get("type") in _ITEM_BACKED_BLOCK_TYPES:
            child_keys.append("items")
        for child_key in child_keys:
            children = block.get(child_key)
            if isinstance(children, list):
                _validate_source_list_references(
                    children,
                    sources,
                    _json_path(block_path, child_key),
                )


def normalize_rich_payload(value: Any) -> tuple[dict[str, Any] | None, list[str]]:
    """Strictly validate and normalize the renderer-neutral rich payload.

    Rich payloads must be renderable before persistence. Invalid or unknown block
    shapes raise :class:`RichPayloadValidationError` with retry guidance.
    """

    warnings: list[str] = []
    if value is None:
        return None, warnings
    if not isinstance(value, dict):
        raise RichPayloadValidationError(
            reason="invalid_rich_payload",
            path="$",
            expected="object with blocks/assets/sources/datasets/exports/metadata",
            received=value,
        )
    size_bytes = _json_size_bytes(value)
    if size_bytes > RICH_DELIVERABLE_MAX_BYTES:
        raise RichPayloadValidationError(
            reason="rich_payload_too_large",
            path="$",
            expected=f"JSON payload <= {RICH_DELIVERABLE_MAX_BYTES} bytes",
            received=value,
        )

    normalized = dict(value)
    if "media_manifest" in normalized:
        raise RichPayloadValidationError(
            reason="reserved_rich_media_manifest",
            path="$.media_manifest",
            expected="omitted; this field is controller-owned",
            received=normalized["media_manifest"],
        )
    blocks = normalized.get("blocks")
    if not isinstance(blocks, list):
        raise RichPayloadValidationError(
            reason="invalid_rich_blocks",
            path="$.blocks",
            expected="array of supported block objects",
            received=blocks,
        )
    block_count = [0]
    normalized["blocks"] = [
        _normalize_block(block, f"$.blocks[{index}]", block_count)
        for index, block in enumerate(blocks)
    ]

    for key in _ARRAY_OBJECT_KEYS:
        normalized[key] = _validate_array_of_objects(normalized.get(key), f"$.{key}")
    _validate_source_list_references(normalized["blocks"], normalized["sources"])
    datasets = normalized.get("datasets", [])
    if _dataset_row_count(datasets) > RICH_DELIVERABLE_MAX_DATASET_ROWS:
        raise RichPayloadValidationError(
            reason="rich_dataset_rows_exceeded",
            path="$.datasets",
            expected=f"total dataset rows <= {RICH_DELIVERABLE_MAX_DATASET_ROWS}",
            received=datasets,
        )

    raw_metadata = normalized.get("metadata")
    if raw_metadata is None:
        normalized["metadata"] = {}
    elif not isinstance(raw_metadata, dict):
        raise RichPayloadValidationError(
            reason="invalid_rich_metadata",
            path="$.metadata",
            expected="object",
            received=raw_metadata,
        )
    else:
        _validate_string_caps(raw_metadata, "$.metadata")
        normalized["metadata"] = dict(raw_metadata)

    for key in set(normalized) - {"blocks", "assets", "sources", "datasets", "exports", "metadata"}:
        raw = normalized.get(key)
        _validate_string_caps(raw, f"$.{key}")
    metadata = normalized["metadata"]
    presentation_present = "presentation" in metadata
    presentation = metadata.get("presentation")
    pulse_variant_present = "pulse_variant" in metadata
    if pulse_variant_present and presentation != "pulse":
        raise RichPayloadValidationError(
            reason="pulse_variant_requires_pulse_presentation",
            path="$.metadata.presentation",
            expected="'pulse' when metadata.pulse_variant is present",
            received=presentation,
        )
    if not presentation_present:
        pass
    elif (
        not isinstance(presentation, str)
        or not presentation.strip()
        or presentation not in _RICH_PRESENTATIONS
    ):
        raise RichPayloadValidationError(
            reason="unknown_rich_presentation",
            path="$.metadata.presentation",
            expected=f"one of {sorted(_RICH_PRESENTATIONS)} or omit for generic rich",
            received=presentation,
            issues=[
                {
                    "reason": "unknown_rich_presentation",
                    "path": "$.metadata.presentation",
                    "expected": f"one of {sorted(_RICH_PRESENTATIONS)} or omit for generic rich",
                }
            ],
        )
    else:
        _RICH_PRESENTATIONS[presentation].validator(normalized)
    return normalized, warnings


def normalize_required_rich_payload(value: Any) -> tuple[dict[str, Any], list[str]]:
    """Normalize a rich payload and reject absence exactly as persistence does."""

    payload, warnings = normalize_rich_payload(value)
    if payload is None:
        raise RichPayloadValidationError(
            reason="missing_rich_payload",
            path="$.rich",
            expected="rich payload object with a blocks array for format='rich'",
            received=value,
        )
    return payload, warnings


def rich_render_metadata(
    payload: dict[str, Any] | None, warnings: list[str] | None = None
) -> dict[str, Any]:
    blocks = payload.get("blocks", []) if isinstance(payload, dict) else []
    metadata = payload.get("metadata", {}) if isinstance(payload, dict) else {}
    media_manifest = payload.get("media_manifest", {}) if isinstance(payload, dict) else {}
    presentation = metadata.get("presentation") if isinstance(metadata, dict) else None
    pulse_version = (
        metadata.get("pulse_version", 1)
        if presentation == "pulse" and isinstance(metadata, dict)
        else None
    )
    result = {
        "schema": "cognis.rich_deliverable.v1",
        "renderer": "block-composed",
        "block_count": len(blocks) if isinstance(blocks, list) else 0,
        "media_count": len(media_manifest) if isinstance(media_manifest, dict) else 0,
        "presentation": presentation,
        "pulse_schema": (
            f"cognis.rich.pulse.v{pulse_version}" if pulse_version is not None else None
        ),
        "pulse_version": pulse_version,
        "pulse_variant": (
            metadata.get("pulse_variant")
            if presentation == "pulse" and isinstance(metadata, dict)
            else None
        ),
        "pulse_valid": presentation == "pulse",
        "projection_max_bytes": RICH_DELIVERABLE_PROJECTION_MAX_BYTES,
        "warnings": warnings or [],
    }
    if presentation == "pulse" and pulse_version == 2 and isinstance(payload, dict):
        quality = pulse_quality_metadata(payload)
        result["pulse_quality"] = {
            **quality,
            "quality_gate_passed": (
                quality["visual_count"] >= 1
                and quality["uncited_story_count"] == 0
                and quality["unavailable_count"] <= 1
                and quality["media_alt_count"] == quality["media_count"]
                and quality["media_source_count"] == quality["media_count"]
            ),
        }
    return result


def rich_export_metadata(payload: dict[str, Any] | None = None) -> dict[str, Any]:
    exports = payload.get("exports", []) if isinstance(payload, dict) else []
    return {
        "available": [
            "copy",
            "open_full_view",
            "standalone_html",
            "download_pdf",
            "share_link",
        ],
        "declared_exports": exports if isinstance(exports, list) else [],
        "standalone": {
            "html": {"available": True, "cached": True},
            "pdf": {"available": True, "cached": True},
            "share_link": {"available": True, "signed": True},
        },
    }


def rich_payload_for_projection(payload: dict[str, Any] | None) -> dict[str, Any] | None:
    """Return a lightweight rich payload for timeline/list projections.

    Full payloads are returned only when they fit the projection budget. Larger
    payloads keep fallback content and metadata in the projection and can be
    fetched through full deliverable retrieval surfaces.
    """

    if payload is None:
        return None
    if _json_size_bytes(payload) <= RICH_DELIVERABLE_PROJECTION_MAX_BYTES:
        return payload
    blocks = payload.get("blocks", [])
    return {
        "blocks": [],
        "assets": [],
        "sources": [],
        "datasets": [],
        "exports": payload.get("exports", []) if isinstance(payload.get("exports"), list) else [],
        "metadata": {
            "projection_truncated": True,
            "full_payload_required": True,
            "block_count": len(blocks) if isinstance(blocks, list) else 0,
        },
    }


class DeliverableStatus(StrEnum):
    """Lifecycle states for a step deliverable."""

    BUFFERED = "buffered"
    APPROVED = "approved"
    DELIVERED = "delivered"
    SUPERSEDED = "superseded"
    REJECTED = "rejected"


class Deliverable(BaseModel):
    """Typed, versioned artifact authored by a workflow step."""

    deliverable_id: str
    step_run_id: str | None = None
    conversation_id: str | None = None
    session_id: str | None = None
    turn_id: str | None = None
    version: int
    attempt_number: int = 1
    content: str
    format: Literal["markdown", "plain", "html", "rich"] = "markdown"
    title: str | None = None
    target: Literal["channel", "none"] | None = None
    outputs: dict[str, Any] = {}
    rich: dict[str, Any] | None = None
    rich_payload: dict[str, Any] | None = None
    validation_warnings: list[str] = []
    render_metadata: dict[str, Any] = {}
    export_metadata: dict[str, Any] = {}
    status: DeliverableStatus = DeliverableStatus.BUFFERED
    evaluator_feedback: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None

    @field_validator("content")
    @classmethod
    def _validate_content(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("content must not be empty")
        return value
