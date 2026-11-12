"""Typed durability outcomes for a turn-owned assistant stream."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

ASSISTANT_OUTPUT_UNPERSISTED_MESSAGE = (
    "The response could not be saved. Keep a copy of the interrupted text."
)


@dataclass(frozen=True)
class AssistantStreamCheckpoint:
    """Distinguish an empty stream from a rejected or uncertain append."""

    status: Literal["empty", "persisted", "rejected", "uncertain"]
    content: str | None = None
    last_seq: int = 0
    turn_cycle_index: int | None = None
    assistant_phase_index: int | None = None
    chunk_count: int = 0

    @property
    def failed(self) -> bool:
        """Whether visible output remains unconfirmed by the durable owner."""
        return self.status in {"rejected", "uncertain"}

    def failure_detail(self) -> dict[str, str | int | bool | None]:
        """Return content-free metadata suitable for a lifecycle marker."""
        return {
            "output_unpersisted": True,
            "checkpoint_status": self.status,
            "assistant_phase_index": self.assistant_phase_index,
            "turn_cycle_index": self.turn_cycle_index,
            "stream_chunk_count": self.chunk_count,
        }
