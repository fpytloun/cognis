"""Chat v2 sync contract package.

Chat v2 is a typed, cursor-checked projection/sync layer over Cognis session
events. The current session event-store backend is Intaris, but the public Chat
v2 contract is intentionally backend-agnostic.
"""

from __future__ import annotations

__all__ = [
    "cursors",
    "event_store",
    "normalizer",
    "projector",
    "realtime",
    "routes",
    "schemas",
    "sync",
]
