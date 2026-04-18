"""Helpers for replay-safe streamed JSON argument assembly."""

from __future__ import annotations


def merge_incremental_json_fragment(existing: str, incoming: str) -> str:
    """Merge a streamed JSON fragment into an accumulated string.

    Providers sometimes replay a full prefix or restart from an overlapping
    point after a mid-stream retry. This helper preserves the longest known
    accumulation and appends only the unseen suffix when possible.
    """

    if not incoming:
        return existing
    if not existing:
        return incoming
    if existing.startswith(incoming):
        return existing
    if incoming.startswith(existing):
        return incoming

    max_overlap = min(len(existing), len(incoming))
    for overlap in range(max_overlap, 0, -1):
        if existing.endswith(incoming[:overlap]):
            return existing + incoming[overlap:]
    return existing + incoming
