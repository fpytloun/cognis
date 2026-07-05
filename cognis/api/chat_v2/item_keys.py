"""Canonical Chat v2 timeline item identity and ordering helpers."""

from __future__ import annotations

KIND_RANK: dict[str, int] = {
    "user_message": 0,
    "thinking": 1,
    "assistant_message": 2,
    "tool_call": 3,
    "tool_result": 3,
    "evaluation": 3,
    "delegation": 4,
    "managed_conversation": 4,
    "task": 5,
    "question_set": 6,
    "auth_challenge": 6,
    "credential_request": 6,
    "todo_state": 7,
    "artifact": 8,
    "file_diff": 8,
    "system_message": 9,
    "compaction": 10,
    "notice": 10,
    "error": 11,
    "unknown": 12,
}

ORDER_KEY_NO_LINEAGE = 9999
ORDER_KEY_NO_SEQ = 10**15 - 1
ORDER_KEY_ACTIVE_LINEAGE = 9998

assert 0 <= ORDER_KEY_ACTIVE_LINEAGE < ORDER_KEY_NO_LINEAGE <= 9999, (
    "Lineage sentinel ordering invariant violated: "
    f"active={ORDER_KEY_ACTIVE_LINEAGE}, no_lineage={ORDER_KEY_NO_LINEAGE}"
)


def encode_timeline_sort_key(
    *,
    lineage: int | None,
    seq: int | None,
    phase: int | None,
    kind_rank: int,
    local: int,
) -> str:
    """Encode the canonical lexicographically-sortable timeline order key."""

    li = lineage if isinstance(lineage, int) else ORDER_KEY_NO_LINEAGE
    s = seq if isinstance(seq, int) else ORDER_KEY_NO_SEQ
    p = phase if isinstance(phase, int) else 0
    return f"{li:04d}:{s:015d}:{p:06d}:{kind_rank:02d}:{local:09d}"


def runtime_timeline_sort_key(
    *,
    phase: int | None,
    kind_rank: int,
    local: int,
) -> str:
    """Canonical sort key for active-turn runtime overlay items."""

    return encode_timeline_sort_key(
        lineage=ORDER_KEY_ACTIVE_LINEAGE,
        seq=None,
        phase=phase,
        kind_rank=kind_rank,
        local=local,
    )


def late_runtime_timeline_sort_key(*, kind_rank: int, local: int) -> str:
    """Canonical high sort key for overlays that must not move an existing item earlier."""

    return encode_timeline_sort_key(
        lineage=ORDER_KEY_NO_LINEAGE,
        seq=None,
        phase=None,
        kind_rank=kind_rank,
        local=local,
    )


def assistant_message_item_id(*, message_id: str, phase: int | None) -> str:
    if isinstance(phase, int):
        return f"message:{message_id}:phase:{phase}"
    return f"message:{message_id}"


def thinking_item_id(*, message_id: str, phase: int | None, block_id: str) -> str:
    if isinstance(phase, int):
        return f"thinking:{message_id}:phase:{phase}:{block_id}"
    return f"thinking:{message_id}:{block_id}"
