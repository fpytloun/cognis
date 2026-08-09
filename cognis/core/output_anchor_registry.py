"""Small registry adapting producer metadata into canonical output anchors."""

from __future__ import annotations

import re
from collections.abc import Callable, Sequence
from dataclasses import replace
from typing import Any

from cognis.core.output_anchors import (
    AnchorFormat,
    AnchorManifestV1,
    OutputAnchorV1,
    RecoveryOperation,
    stable_anchor_id,
    validate_anchor,
)

_KEY_RE = re.compile(r"[^A-Za-z0-9._:-]+")
_NATURAL_PART_RE = re.compile(r"(\d+)")
_MAX_ANCHORS = 256


def _safe_key(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    value = _KEY_RE.sub("_", value.strip()).strip("_")
    return value[:120] or None


class AnchorAdapter:
    """Pure adapter for one declared output family."""

    def __init__(
        self,
        adapter_id: str,
        matches: Callable[[str, dict[str, Any]], bool],
        format_name: AnchorFormat,
        kind_prefix: str,
        recovery_op: RecoveryOperation = "read_lines",
    ) -> None:
        self.adapter_id = adapter_id
        self.matches = matches
        self.format_name = format_name
        self.kind_prefix = kind_prefix
        self.recovery_op = recovery_op

    def adapt(self, call_id: str, item: dict[str, Any]) -> OutputAnchorV1 | None:
        key = _safe_key(item.get("anchor") or item.get("key") or item.get("name"))
        kind = _safe_key(item.get("kind")) or "section"
        if key is None:
            return None
        locator, recovery_op = _locator(item, self.recovery_op)
        if locator is None:
            return None
        label = str(item.get("label") or item.get("summary") or key)[:160]
        summary = item.get("summary")
        declared_format = item.get("format")
        format_name = (
            declared_format
            if declared_format
            in {
                "text",
                "log",
                "json",
                "table",
                "csv",
                "search",
                "web",
                "document",
                "code",
                "diff",
                "test",
                "pdf",
                "spreadsheet",
                "presentation",
                "binary",
            }
            else self.format_name
        )
        anchor = OutputAnchorV1(
            anchor_id=stable_anchor_id(
                call_id,
                self.adapter_id,
                format_name=format_name,
                key=key,
                locator=locator,
            ),
            key=key,
            kind=f"{self.kind_prefix}.{kind}",
            format=format_name,
            label=label,
            summary=str(summary)[:500] if isinstance(summary, str) else None,
            locator=locator,
            recovery_op=recovery_op,
            priority=max(0, min(100, int(item.get("priority") or _default_priority(key, kind)))),
            promote=bool(item.get("promote")) or key.startswith(("result:", "media:", "failure:")),
        )
        return anchor if validate_anchor(anchor) else None


def _locator(
    item: dict[str, Any], default_operation: RecoveryOperation
) -> tuple[dict[str, Any] | None, RecoveryOperation]:
    pointer = item.get("json_pointer")
    if isinstance(pointer, str) and pointer.startswith("/"):
        return {"type": "stored_json", "pointer": pointer}, "read_json"
    start_row, end_row = item.get("start_row"), item.get("end_row")
    if isinstance(start_row, int) and isinstance(end_row, int) and 1 <= start_row <= end_row:
        locator: dict[str, Any] = {
            "type": "stored_rows",
            "start_row": start_row,
            "end_row": end_row,
        }
        columns = item.get("columns")
        if isinstance(columns, list):
            locator["columns"] = [str(column)[:120] for column in columns[:50]]
        return locator, "read_rows"
    artifact_part = item.get("artifact_part")
    start_line, end_line = item.get("start_line"), item.get("end_line")
    valid_lines = (
        isinstance(start_line, int) and isinstance(end_line, int) and 1 <= start_line <= end_line
    )
    if (
        item.get("format") == "pdf"
        and isinstance(artifact_part, dict)
        and isinstance(artifact_part.get("page"), int)
        and valid_lines
    ):
        return {
            "type": "stored_pdf_page",
            "format": "pdf",
            "page": artifact_part["page"],
            "start_line": start_line,
            "end_line": end_line,
        }, "read_lines"
    if isinstance(artifact_part, dict):
        safe_part = {
            key: value
            for key, value in artifact_part.items()
            if key in {"page", "sheet", "range", "slide", "attachment_index"}
            and isinstance(value, (str, int))
        }
        return {"type": "artifact_part", **safe_part}, "read_artifact_part"
    if valid_lines:
        return {
            "type": "stored_lines",
            "start_line": start_line,
            "end_line": end_line,
        }, default_operation
    return None, default_operation


def _natural_key(value: str) -> tuple[object, ...]:
    return tuple(
        int(part) if part.isdigit() else part.casefold()
        for part in _NATURAL_PART_RE.split(value)
        if part
    )


def _default_priority(key: str, kind: str) -> int:
    if key.startswith(("failure:", "error:")) or "failure" in kind or "error" in kind:
        return 100
    if key.startswith("media:"):
        return 90
    if key.startswith(("result:", "citation:")):
        return 80
    return 50


def _prefix(*prefixes: str) -> Callable[[str, dict[str, Any]], bool]:
    return lambda _tool, item: str(item.get("anchor") or item.get("kind") or "").startswith(
        prefixes
    )


_ADAPTERS: Sequence[AnchorAdapter] = (
    AnchorAdapter("search-v1", _prefix("answer", "result:", "citation:"), "search", "search"),
    AnchorAdapter(
        "web-v1",
        _prefix("page:", "media:", "heading:", "transcript:", "item:"),
        "web",
        "web",
    ),
    AnchorAdapter(
        "json-v1",
        lambda _tool, item: isinstance(item.get("json_pointer"), str),
        "json",
        "json",
        "read_json",
    ),
    AnchorAdapter(
        "csv-v1",
        lambda _tool, item: item.get("format") == "csv",
        "csv",
        "csv",
        "read_rows",
    ),
    AnchorAdapter(
        "table-v1",
        lambda _tool, item: isinstance(item.get("start_row"), int),
        "table",
        "table",
        "read_rows",
    ),
    AnchorAdapter("log-v1", _prefix("warning:", "error:", "tail:"), "log", "log"),
    AnchorAdapter("test-v1", _prefix("failure:", "test:"), "test", "test"),
    AnchorAdapter("diff-v1", _prefix("hunk:", "diff:"), "diff", "diff"),
    AnchorAdapter("code-v1", _prefix("file:", "symbol:"), "code", "code"),
    AnchorAdapter(
        "artifact-part-v1",
        lambda _tool, item: isinstance(item.get("artifact_part"), dict),
        "binary",
        "artifact",
        "read_artifact_part",
    ),
    AnchorAdapter(
        "legacy-lines-v1",
        lambda _tool, _item: True,
        "text",
        "text",
    ),
)


def build_anchor_manifest(
    call_id: str,
    tool_name: str,
    drafts: object,
) -> tuple[AnchorManifestV1, list[dict[str, Any]]]:
    """Adapt drafts and return public manifest plus store-private records."""

    if not isinstance(drafts, list):
        return AnchorManifestV1(adapter_id="none", anchors=()), []
    public: list[OutputAnchorV1] = []
    private: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    seen_keys: set[str] = set()
    adapter_ids: list[str] = []
    for item in drafts:
        if not isinstance(item, dict):
            continue
        adapter = next(
            (candidate for candidate in _ADAPTERS if candidate.matches(tool_name, item)), None
        )
        if adapter is None:
            continue
        try:
            anchor = adapter.adapt(call_id, item)
        except (TypeError, ValueError):
            continue
        if anchor is None or anchor.anchor_id in seen_ids:
            continue
        if anchor.key in seen_keys:
            key = f"{anchor.key}:{anchor.anchor_id[-8:]}"
            anchor = replace(
                anchor,
                key=key,
                anchor_id=stable_anchor_id(
                    call_id,
                    adapter.adapter_id,
                    format_name=anchor.format,
                    key=key,
                    locator=anchor.locator,
                ),
            )
        candidate = item.get("artifact_candidate")
        candidate_dict = candidate if isinstance(candidate, dict) else None
        candidate_allowed = candidate_dict is not None and (
            candidate_dict.get("source_type") == "artifact_id"
            or (
                candidate_dict.get("source_type") == "remote_url"
                and tool_name in {"web_search", "web_fetch", "web_crawl", "web_map"}
            )
        )
        if candidate_allowed:
            anchor = replace(anchor, recovery_op="materialize_artifact")
        record = anchor.to_dict()
        if candidate_allowed and candidate_dict is not None:
            record["artifact_candidate"] = dict(candidate_dict)
        public.append(anchor)
        private.append(record)
        seen_ids.add(anchor.anchor_id)
        seen_keys.add(anchor.key)
        adapter_ids.append(adapter.adapter_id)
        if len(public) >= _MAX_ANCHORS:
            break
    all_line_located = all(isinstance(anchor.locator.get("start_line"), int) for anchor in public)
    order = sorted(
        range(len(public)),
        key=(
            lambda index: (
                (
                    public[index].locator.get("start_line", 0),
                    public[index].locator.get("end_line", 0),
                    public[index].key,
                    public[index].anchor_id,
                )
                if all_line_located
                else (
                    -public[index].priority,
                    _natural_key(public[index].key),
                    public[index].anchor_id,
                )
            )
        ),
    )
    public = [public[index] for index in order]
    private = [private[index] for index in order]
    adapter_id = "+".join(dict.fromkeys(adapter_ids)) or "none"
    return AnchorManifestV1(adapter_id=adapter_id, anchors=tuple(public)), private
