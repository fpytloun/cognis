"""Recovery-handle extraction and injection for compaction summaries."""

from __future__ import annotations

import json
import re
from typing import Any

from prometheus_client import Counter

from cognis.core.compaction.input_format import tool_result_recovery_hint

COMPACTION_HANDLES_CAPPED = Counter(
    "cognis_compaction_recoverable_handles_capped_total",
    "Times the recoverable-handles block was capped to max_entries",
)

# Older conversation context remains searchable. Keep only a small index for
# raw tool evidence that normal conversation recovery cannot reproduce.
_MAX_RECOVERABLE_HANDLES = 5
RECOVERY_USAGE_HINT = (
    "Use read_tool_output for the full output or search_tool_output for a known term."
)
_MARKDOWN_RECOVERY_SECTION_RE = re.compile(
    r"\n*^## Recoverable Tool (?:Evidence|Outputs)\s*$.*?(?=^## |\Z)",
    re.MULTILINE | re.DOTALL,
)
_LEGACY_RECOVERY_SECTION_RE = re.compile(
    r"\n*^Recoverable tool outputs before compaction:\s*$.*\Z",
    re.MULTILINE | re.DOTALL,
)


def recoverable_tool_output_lines(
    events: list[Any], *, max_entries: int = _MAX_RECOVERABLE_HANDLES
) -> list[str]:
    """Return a small, contextual index of recoverable raw tool evidence."""
    calls: dict[str, Any] = {}
    for event in events:
        if event.type != "tool_call":
            continue
        call_id = event.data.get("call_id")
        if isinstance(call_id, str) and call_id:
            calls[call_id] = event

    candidates: list[tuple[bool, bool, int, str]] = []
    for event in events:
        if event.type != "tool_result":
            continue
        hint = tool_result_recovery_hint(event.data)
        if not hint:
            continue
        name = event.data.get("name") or "tool"
        call_id = _recovery_call_id(event.data)
        if call_id is None:
            continue
        context = _tool_call_context(calls.get(call_id))
        context_text = f" — {context.rstrip('.!?')}" if context else ""
        line = f"- [{event.seq}] {name}{context_text}. call_id={call_id!r}"
        candidates.append(
            (
                event.data.get("is_error") is True,
                name == "bash",
                int(event.seq),
                line,
            )
        )

    # Errors are most useful for resumption, followed by bash operations and
    # then the most recent remaining evidence.
    candidates.sort(key=lambda candidate: candidate[:3], reverse=True)

    lines = [candidate[3] for candidate in candidates[:max_entries]]
    if len(candidates) > max_entries:
        COMPACTION_HANDLES_CAPPED.inc()
        lines.append(
            f"[{len(candidates) - max_entries} additional recoverable outputs omitted "
            "from this summary]"
        )
    return lines


def append_recoverable_tool_output_handles(
    summary: str,
    events: list[Any],
    *,
    max_entries: int = _MAX_RECOVERABLE_HANDLES,
) -> str:
    """Ensure LLM compaction cannot drop saved tool-output recovery handles."""
    summary = remove_recoverable_tool_output_sections(summary)
    lines = recoverable_tool_output_lines(events, max_entries=max_entries)
    if not lines:
        return summary
    block_lines = ["## Recoverable Tool Evidence"]
    block_lines.extend(lines)
    block_lines.append(RECOVERY_USAGE_HINT)
    return summary.rstrip() + "\n\n" + "\n".join(block_lines)


def remove_recoverable_tool_output_sections(summary: str) -> str:
    """Remove managed recovery sections before regenerating the current index."""
    summary = _MARKDOWN_RECOVERY_SECTION_RE.sub("", summary)
    summary = _LEGACY_RECOVERY_SECTION_RE.sub("", summary)
    return summary.strip()


def _recovery_call_id(data: dict[str, Any]) -> str | None:
    value = data.get("recovery_call_id")
    if not isinstance(value, str) or not value.strip():
        value = data.get("call_id") if data.get("has_full_output") is True else None
    return value if isinstance(value, str) and value.strip() else None


def _tool_call_context(event: Any | None) -> str | None:
    if event is None:
        return None
    arguments = event.data.get("arguments")
    if isinstance(arguments, str):
        try:
            arguments = json.loads(arguments)
        except (TypeError, ValueError):
            return None
    if not isinstance(arguments, dict):
        return None

    for key in ("description", "command", "file_path", "path", "pattern", "query", "url", "title"):
        value = arguments.get(key)
        if not isinstance(value, str) or not value.strip():
            continue
        compact = " ".join(value.split())
        if len(compact) > 220:
            compact = compact[:217].rstrip() + "..."
        return compact
    return None
