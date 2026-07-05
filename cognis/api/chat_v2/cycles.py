from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from .schemas import TimelineItem, TurnCycleState

_OPEN_ITEM_STATUSES = {"pending", "running", "waiting"}


@dataclass
class _CycleAccumulator:
    turn_id: str
    turn_cycle_index: int
    has_tool_activity: bool = False
    is_open: bool = False

    def to_state(self) -> TurnCycleState:
        return TurnCycleState(
            turn_id=self.turn_id,
            turn_cycle_index=self.turn_cycle_index,
            lifecycle_status="open" if self.is_open else "complete",
            has_tool_activity=self.has_tool_activity,
        )


def cycle_states_from_items(items: Iterable[TimelineItem]) -> list[TurnCycleState]:
    """Derive authoritative turn-cycle lifecycle metadata from timeline items."""

    states: dict[tuple[str, int], _CycleAccumulator] = {}
    for item in items:
        turn_id = getattr(item, "turn_id", None)
        turn_cycle_index = getattr(item, "turn_cycle_index", None)
        if not isinstance(turn_id, str) or not turn_id:
            continue
        if not isinstance(turn_cycle_index, int):
            continue
        key = (turn_id, turn_cycle_index)
        state = states.get(key)
        if state is None:
            state = _CycleAccumulator(turn_id=turn_id, turn_cycle_index=turn_cycle_index)
            states[key] = state
        if item.kind == "tool_call":
            state.has_tool_activity = True
        if getattr(item, "status", None) in _OPEN_ITEM_STATUSES:
            state.is_open = True

    return [
        state.to_state()
        for state in sorted(
            states.values(),
            key=lambda candidate: (candidate.turn_id, candidate.turn_cycle_index),
        )
    ]
