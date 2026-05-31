"""Context compaction package.

Public API — all callers import from here:

    from cognis.core.compaction import CompactionStrategy, CompactionResult
    from cognis.core.compaction import ROTATION_TOTAL  # for turn_scheduler / agent_loop

Internal submodules:
    banding      — three-band token-budgeted input assembly
    input_format — event-to-text formatting helpers
    recovery     — recoverable-handle extraction and injection
    fallback     — sliding-window mechanical summary
    strategy     — orchestration, retry logic, settings loading
"""

from __future__ import annotations

from cognis.core.compaction.fallback import build_sliding_window_summary as _mechanical_summary

# Legacy helpers re-exported so existing tests that import them directly
# continue to work without modification.
from cognis.core.compaction.input_format import (
    format_events_for_compaction as _format_events_for_compaction,
)
from cognis.core.compaction.recovery import COMPACTION_HANDLES_CAPPED
from cognis.core.compaction.strategy import (
    COMPACTION_DEFERRED_TAIL_SEEDED,
    COMPACTION_FALLBACK_USED,
    COMPACTION_TOTAL,
    LONG_LIVED_CHAT_COMPACTION_ADDENDUM,
    ROTATION_TOTAL,
    CompactionModelContext,
    CompactionResult,
    CompactionStrategy,
)

__all__ = [
    "CompactionStrategy",
    "CompactionResult",
    "CompactionModelContext",
    "LONG_LIVED_CHAT_COMPACTION_ADDENDUM",
    "COMPACTION_TOTAL",
    "COMPACTION_FALLBACK_USED",
    "COMPACTION_HANDLES_CAPPED",
    "COMPACTION_DEFERRED_TAIL_SEEDED",
    "ROTATION_TOTAL",
    # Legacy names kept for backward compat (tests + any external callers).
    "_format_events_for_compaction",
    "_mechanical_summary",
]
