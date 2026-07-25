"""Canonical, controller-owned tool-output anchor contract."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, Literal

AnchorFormat = Literal[
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
]
RecoveryOperation = Literal[
    "read_lines",
    "read_json",
    "read_rows",
    "read_artifact_part",
    "materialize_artifact",
]

_FORMATS = {
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
_RECOVERY_OPERATIONS = {
    "read_lines",
    "read_json",
    "read_rows",
    "read_artifact_part",
    "materialize_artifact",
}


@dataclass(frozen=True, slots=True)
class OutputAnchorV1:
    """Public projection of one persisted output section."""

    anchor_id: str
    key: str
    kind: str
    format: AnchorFormat
    label: str
    summary: str | None
    locator: dict[str, Any]
    recovery_op: RecoveryOperation
    priority: int
    promote: bool
    lazy_artifact_ref: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "anchor_id": self.anchor_id,
            "anchor": self.key,
            "key": self.key,
            "kind": self.kind,
            "format": self.format,
            "label": self.label,
            "summary": self.summary,
            "locator": self.locator,
            "recovery_op": self.recovery_op,
            "priority": self.priority,
            "promote": self.promote,
            "lazy_artifact_ref": self.lazy_artifact_ref,
            # Compatibility for the existing line-oriented recovery tool.
            "start_line": self.locator.get("start_line"),
            "end_line": self.locator.get("end_line"),
        }


@dataclass(frozen=True, slots=True)
class AnchorManifestV1:
    """Versioned persisted anchor collection."""

    adapter_id: str
    anchors: tuple[OutputAnchorV1, ...]
    schema_version: int = 1

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "adapter_id": self.adapter_id,
            "anchors": [anchor.to_dict() for anchor in self.anchors],
        }


def stable_anchor_id(
    call_id: str,
    adapter_id: str,
    *,
    format_name: str,
    key: str,
    locator: dict[str, Any],
) -> str:
    """Build a deterministic identity without exposing recovery candidates."""

    identity = json.dumps(
        [call_id, adapter_id, format_name, key, locator],
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    )
    return "anc_" + hashlib.sha256(identity.encode()).hexdigest()[:24]


def validate_anchor(anchor: OutputAnchorV1) -> bool:
    """Validate the closed controller-owned parts of the public contract."""

    return (
        anchor.format in _FORMATS
        and anchor.recovery_op in _RECOVERY_OPERATIONS
        and bool(anchor.anchor_id and anchor.key and anchor.kind and anchor.label)
        and 0 <= anchor.priority <= 100
        and isinstance(anchor.locator, dict)
    )
