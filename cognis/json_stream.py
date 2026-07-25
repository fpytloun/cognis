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

    Replacement is only applied when the incoming complete object is at least
    as long as the existing accumulation. A large in-progress accumulation for
    a big tool call (e.g. ``write_deliverable`` with substantial content) is
    normally still-unparseable simply because it isn't finished yet, not
    because it is corrupt. If a later, unrelated, *smaller* fragment happens to
    parse as a complete JSON object on its own, it must never be allowed to
    discard a longer legitimate accumulation — that would silently truncate the
    tool call to a fraction of its intended arguments. Genuine "invalid partial
    then complete corrected replay" recoveries are, in practice, at least as
    long as what came before, so this preserves that behavior while closing the
    truncation gap.
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
    if (
        incoming_object is not None
        and existing_object is None
        and len(incoming) >= len(existing)
    ):
        return IncrementalJsonMergeResult(merged=incoming, emitted=incoming, replaced=True)
    if (
        incoming_object is not None
        and existing_object is not None
        and existing_object != incoming_object
    ):
        # Both the accumulation and the incoming fragment are complete,
        # divergent, top-level JSON objects. Tool-call arguments are always a
        # single top-level object, so two distinct complete objects on the same
        # stream index are a provider double-feed (the same logical call resent
        # with corrected/divergent arguments), never two intended parallel
        # calls (those arrive on separate stream indexes). Concatenating them
        # would corrupt the arguments and, downstream, fabricate a second tool
        # call. Prefer the later complete object as the corrected replay even
        # when it is shorter. The length guard applies only while the existing
        # accumulation is incomplete; once both values are complete objects,
        # appending either one is always invalid.
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
