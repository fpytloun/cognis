"""Shared bounded presentation contract for tool outputs."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

from cognis.core.truncation import middle_truncate


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
) -> str:
    """Return deterministic recovery calls for model-facing compact placeholders."""

    quoted = repr(call_id)
    calls = [
        f"read_tool_output(call_id={quoted})",
        f"search_tool_output(call_id={quoted}, pattern='error|timeout|keyword')",
    ]
    if anchors_available and (anchor_count is None or anchor_count > 0):
        calls.append(f"list_tool_output_anchors(call_id={quoted})")
        calls.append(f"read_tool_output_anchor(call_id={quoted}, anchor='<anchor>')")
    return f"Recover with call_id {quoted}: " + ", ".join(calls) + "."


def present_tool_output(
    text: str,
    max_chars: int,
    *,
    recovery_call_id: str | None = None,
    has_full_output: bool = False,
    tool_output_artifact_id: str | None = None,
    anchors: Sequence[str] | None = None,
    token_counter: Callable[[str], int] | None = None,
    max_tokens: int | None = None,
) -> ToolOutputPresentation:
    anchor_names = [name for name in (anchors or []) if isinstance(name, str) and name.strip()]
    call_id = recovery_call_id if has_full_output and recovery_call_id else None
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
) -> ToolOutputPresentation:
    meta = metadata or {}
    call_id = recovery_call_id or _str_or_none(meta.get("recovery_call_id"))
    full = has_full_output or bool(meta.get("has_full_output"))
    artifact_id = tool_output_artifact_id or _str_or_none(meta.get("tool_output_artifact_id"))
    anchor_names = [name for name in (anchors or []) if isinstance(name, str) and name.strip()]
    if not anchor_names and isinstance(meta.get("anchor_count"), int) and meta["anchor_count"] > 0:
        anchors_available = True
        anchor_count = int(meta["anchor_count"])
    else:
        anchors_available = bool(anchor_names) or bool(meta.get("anchors_available"))
        anchor_count = len(anchor_names) if anchor_names else int(meta.get("anchor_count") or 0)
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
        transport_truncated=truncated,
    )


def _str_or_none(value: object) -> str | None:
    return value if isinstance(value, str) and value.strip() else None
