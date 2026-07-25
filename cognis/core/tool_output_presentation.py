"""Shared bounded presentation contract for tool outputs."""

from __future__ import annotations

import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

from cognis.core.truncation import middle_truncate

MAX_ANCHOR_NAME_LENGTH = 120
MAX_PRESENTED_ANCHORS = 32
_UNSAFE_ANCHOR_CHARS = re.compile(r"[^A-Za-z0-9._:-]+")
_LAZY_ARTIFACT_REF_RE = re.compile(
    r"^tool_artifact:[A-Za-z0-9][A-Za-z0-9._-]{0,199}:[A-Za-z0-9][A-Za-z0-9._:-]{0,119}$"
)


def safe_anchor_name(value: object) -> str | None:
    """Return a bounded, controller-safe anchor name."""

    if not isinstance(value, str):
        return None
    name = _UNSAFE_ANCHOR_CHARS.sub("-", value.strip()).strip("-")
    if not name:
        return None
    return name[:MAX_ANCHOR_NAME_LENGTH].rstrip("-") or None


def safe_anchor_names(values: Sequence[object] | None) -> list[str]:
    """Normalize and de-duplicate only concrete anchor names."""

    names: list[str] = []
    seen: set[str] = set()
    for value in values or []:
        name = safe_anchor_name(value)
        if name and name not in seen:
            names.append(name)
            seen.add(name)
            if len(names) >= MAX_PRESENTED_ANCHORS:
                break
    return names


def safe_output_anchors(value: object) -> list[dict[str, Any]]:
    """Bound persisted anchor metadata while keeping candidates store-private."""

    if not isinstance(value, list):
        return []
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in value:
        if not isinstance(item, dict):
            continue
        name = safe_anchor_name(item.get("anchor") or item.get("name"))
        if not name or name in seen:
            continue
        kind = item.get("kind")
        start_line = item.get("start_line")
        end_line = item.get("end_line")
        locator = item.get("locator")
        valid_lines = (
            isinstance(start_line, int)
            and isinstance(end_line, int)
            and start_line >= 1
            and end_line >= start_line
        )
        valid_locator = isinstance(locator, dict) and locator.get("type") in {
            "stored_lines",
            "stored_json",
            "stored_rows",
            "artifact_part",
        }
        if not isinstance(kind, str) or not kind.strip() or not (valid_lines or valid_locator):
            continue
        seen.add(name)
        anchor: dict[str, Any] = {
            "anchor": name,
            "kind": kind[:120],
        }
        if valid_lines:
            anchor["start_line"] = start_line
            anchor["end_line"] = end_line
        for key in (
            "anchor_id",
            "key",
            "format",
            "label",
            "summary",
            "locator",
            "recovery_op",
            "priority",
            "promote",
            "lazy_artifact_ref",
        ):
            if key in item:
                anchor[key] = item[key]
        if isinstance(anchor.get("label"), str):
            anchor["label"] = anchor["label"][:160]
        if isinstance(anchor.get("summary"), str):
            anchor["summary"] = anchor["summary"][:500]
        candidate = item.get("artifact_candidate")
        if isinstance(candidate, dict):
            anchor["artifact_candidate"] = dict(candidate)
        result.append(anchor)
    return result


def artifact_anchor_names(anchors: Sequence[dict[str, Any]] | None) -> list[str]:
    """Return persisted anchors that can be materialized as artifacts."""

    return safe_anchor_names(
        [
            item.get("anchor")
            for item in anchors or []
            if isinstance(item, dict) and isinstance(item.get("artifact_candidate"), dict)
        ]
    )


def public_anchor_projections(
    anchors: Sequence[dict[str, Any]] | None,
) -> list[dict[str, Any]]:
    """Return bounded canonical anchor metadata without private candidates."""

    allowed = {
        "anchor_id",
        "anchor",
        "key",
        "kind",
        "format",
        "label",
        "summary",
        "locator",
        "recovery_op",
        "priority",
        "promote",
        "lazy_artifact_ref",
        "start_line",
        "end_line",
    }
    return [
        {key: value for key, value in item.items() if key in allowed}
        for item in (anchors or [])[:MAX_PRESENTED_ANCHORS]
        if isinstance(item, dict)
    ]


def lazy_artifact_refs(call_id: str | None, anchors: Sequence[str] | None) -> list[str]:
    """Build bounded lazy refs from one real persisted call and its anchors."""

    if not isinstance(call_id, str) or not call_id.strip():
        return []
    return [f"tool_artifact:{call_id}:{name}" for name in safe_anchor_names(anchors)]


def is_safe_lazy_artifact_ref(value: object) -> bool:
    return isinstance(value, str) and _LAZY_ARTIFACT_REF_RE.fullmatch(value) is not None


@dataclass(frozen=True, slots=True)
class ToolOutputPresentation:
    result: str
    output_size: int
    truncated: bool = False
    agent_visible_truncated: bool = False
    has_full_output: bool = False
    recovery_call_id: str | None = None
    tool_output_artifact_id: str | None = None
    anchors_available: bool = False
    anchor_count: int = 0
    anchors: tuple[str, ...] = ()
    anchor_projections: tuple[dict[str, Any], ...] = ()
    lazy_artifact_refs: tuple[str, ...] = ()
    transport_truncated: bool = False

    def metadata(self) -> dict[str, Any]:
        return {
            "output_size": self.output_size,
            "truncated": self.truncated,
            "agent_visible_truncated": self.agent_visible_truncated,
            "has_full_output": self.has_full_output,
            "recovery_call_id": self.recovery_call_id,
            "tool_output_artifact_id": self.tool_output_artifact_id,
            "anchors_available": self.anchors_available,
            "anchor_count": self.anchor_count,
            "anchors": list(self.anchors),
            "anchor_projections": [dict(item) for item in self.anchor_projections],
            "lazy_artifact_refs": list(self.lazy_artifact_refs),
            "transport_truncated": self.transport_truncated,
        }

    def event_fields(self) -> dict[str, Any]:
        return {"tool_output_presentation": self.metadata(), **self.metadata()}


def output_size(value: str) -> int:
    """Return output size in Unicode characters for UI/model budgets."""

    return len(value)


def build_recovery_hint(
    call_id: str,
    *,
    anchors_available: bool = False,
    anchor_count: int | None = None,
    anchor_names: Sequence[str] | None = None,
) -> str:
    """Return deterministic recovery calls for model-facing compact placeholders."""

    quoted = repr(call_id)
    calls = [
        f"read_tool_output(call_id={quoted})",
        f"search_tool_output(call_id={quoted}, pattern='error|timeout|keyword')",
    ]
    names = safe_anchor_names(anchor_names)
    if names:
        calls.append(f"list_tool_output_anchors(call_id={quoted})")
        calls.append(f"read_tool_output_anchor(call_id={quoted}, anchor={names[0]!r})")
    elif anchors_available and isinstance(anchor_count, int) and anchor_count > 0:
        calls.append(f"list_tool_output_anchors(call_id={quoted})")
    return f"Recover with call_id {quoted}: " + ", ".join(calls) + "."


def present_tool_output(
    text: str,
    max_chars: int,
    *,
    recovery_call_id: str | None = None,
    has_full_output: bool = False,
    tool_output_artifact_id: str | None = None,
    anchors: Sequence[str] | None = None,
    lazy_artifact_anchors: Sequence[str] | None = None,
    anchor_projections: Sequence[dict[str, Any]] | None = None,
    token_counter: Callable[[str], int] | None = None,
    max_tokens: int | None = None,
) -> ToolOutputPresentation:
    anchor_names = safe_anchor_names(anchors)
    call_id = recovery_call_id if has_full_output and recovery_call_id else None
    lazy_refs = lazy_artifact_refs(call_id, lazy_artifact_anchors)
    result, truncated = middle_truncate(
        text,
        max_chars,
        call_id=call_id,
        token_counter=token_counter,
        max_tokens=max_tokens,
        anchors=anchor_names,
        anchors_available=bool(anchor_names),
    )
    return ToolOutputPresentation(
        result=result,
        output_size=output_size(text),
        truncated=truncated,
        agent_visible_truncated=truncated,
        has_full_output=has_full_output,
        recovery_call_id=call_id,
        tool_output_artifact_id=tool_output_artifact_id,
        anchors_available=bool(anchor_names),
        anchor_count=len(anchor_names),
        anchors=tuple(anchor_names),
        anchor_projections=tuple(public_anchor_projections(anchor_projections)),
        lazy_artifact_refs=tuple(lazy_refs),
    )


def build_transport_tool_output_preview(
    text: str,
    max_chars: int,
    *,
    metadata: dict[str, Any] | None = None,
    recovery_call_id: str | None = None,
    has_full_output: bool = False,
    tool_output_artifact_id: str | None = None,
    anchors: Sequence[str] | None = None,
    lazy_refs: Sequence[str] | None = None,
    anchor_projections: Sequence[dict[str, Any]] | None = None,
) -> ToolOutputPresentation:
    meta = metadata or {}
    call_id = recovery_call_id or _str_or_none(meta.get("recovery_call_id"))
    full = has_full_output or bool(meta.get("has_full_output"))
    artifact_id = tool_output_artifact_id or _str_or_none(meta.get("tool_output_artifact_id"))
    metadata_anchors = meta.get("anchors")
    anchor_names = safe_anchor_names(
        anchors
        if anchors is not None
        else metadata_anchors
        if isinstance(metadata_anchors, list)
        else []
    )
    anchors_available = bool(anchor_names)
    anchor_count = len(anchor_names)
    metadata_lazy_refs = meta.get("lazy_artifact_refs")
    candidate_lazy_refs = lazy_refs if lazy_refs is not None else metadata_lazy_refs
    allowed_lazy_refs = set(lazy_artifact_refs(call_id if full else None, anchor_names))
    safe_lazy_refs = [
        ref
        for ref in candidate_lazy_refs or []
        if isinstance(ref, str) and ref in allowed_lazy_refs
    ][:MAX_PRESENTED_ANCHORS]
    result, truncated = middle_truncate(
        text,
        max_chars,
        call_id=call_id if full and call_id else None,
        anchors=anchor_names,
        anchors_available=anchors_available,
    )
    return ToolOutputPresentation(
        result=result,
        output_size=int(meta.get("output_size") or output_size(text)),
        truncated=bool(meta.get("truncated")) or truncated,
        agent_visible_truncated=bool(meta.get("agent_visible_truncated")),
        has_full_output=full,
        recovery_call_id=call_id if full else None,
        tool_output_artifact_id=artifact_id,
        anchors_available=anchors_available,
        anchor_count=anchor_count,
        anchors=tuple(anchor_names),
        anchor_projections=tuple(
            public_anchor_projections(
                anchor_projections
                if anchor_projections is not None
                else meta.get("anchor_projections")
                if isinstance(meta.get("anchor_projections"), list)
                else []
            )
        ),
        lazy_artifact_refs=tuple(safe_lazy_refs),
        transport_truncated=truncated,
    )


def _str_or_none(value: object) -> str | None:
    return value if isinstance(value, str) and value.strip() else None
