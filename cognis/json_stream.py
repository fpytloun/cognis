"""Helpers for replay-safe streamed JSON argument assembly."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class IncrementalJsonMergeResult:
    """Describes how a streamed JSON fragment merged into the accumulator."""

    merged: str
    emitted: str
    replaced: bool = False


def _parse_json_object(raw: str) -> dict[str, Any] | None:
    raw = raw.strip()
    if not raw:
        return None
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


def recover_trailing_json_object(raw: str) -> dict[str, Any] | None:
    """Recover the last valid top-level JSON object that consumes the suffix.

    This handles provider/tool-call corruption where an invalid partial object is
    followed by a fully corrected JSON object for the same logical tool call.
    """

    raw = raw.strip()
    if not raw:
        return None
    decoder = json.JSONDecoder()
    recovered: dict[str, Any] | None = None
    for pos, char in enumerate(raw):
        if char != "{":
            continue
        try:
            parsed, end = decoder.raw_decode(raw, pos)
        except json.JSONDecodeError:
            continue
        if not isinstance(parsed, dict):
            continue
        if raw[end:].strip():
            continue
        recovered = parsed
    return recovered


def merge_incremental_json_fragment(existing: str, incoming: str) -> IncrementalJsonMergeResult:
    """Merge a streamed JSON fragment into an accumulated string.

    Providers sometimes replay a full prefix or restart from an overlapping
    point after a mid-stream retry. They can also emit an invalid partial JSON
    object before retrying with a complete corrected object. This helper
    preserves the longest known valid accumulation and prefers a later complete
    object over concatenating corruption into the final payload.
    """

    if not incoming:
        return IncrementalJsonMergeResult(merged=existing, emitted="")
    if not existing:
        return IncrementalJsonMergeResult(merged=incoming, emitted=incoming)
    if existing.startswith(incoming):
        return IncrementalJsonMergeResult(merged=existing, emitted="")
    if incoming.startswith(existing):
        return IncrementalJsonMergeResult(
            merged=incoming,
            emitted=incoming[len(existing) :],
        )

    existing_object = _parse_json_object(existing)
    incoming_object = _parse_json_object(incoming)
    if existing_object is None and incoming_object is not None:
        return IncrementalJsonMergeResult(merged=incoming, emitted=incoming, replaced=True)

    max_overlap = min(len(existing), len(incoming))
    for overlap in range(max_overlap, 0, -1):
        if existing.endswith(incoming[:overlap]):
            emitted = incoming[overlap:]
            return IncrementalJsonMergeResult(
                merged=existing + emitted,
                emitted=emitted,
            )
    return IncrementalJsonMergeResult(merged=existing + incoming, emitted=incoming)
