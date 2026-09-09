"""Tests for session-scoped Mnemory aliases."""

from __future__ import annotations

import pytest

from cognis.core.memory_aliases import (
    MemoryAliasError,
    MemoryAliasState,
    memory_revision_identity,
)
from cognis.core.session_cache import (
    CachedEvent,
    CachedSessionState,
    SessionCache,
    _deserialize_entry,
    _serialize_entry,
)

MEMORY_ID = "01234567-89ab-4def-8123-456789abcdef"


def _record(
    text: str = "Fact",
    *,
    memory_id: str = MEMORY_ID,
    metadata: dict[str, object] | None = None,
    score: float = 0.9,
) -> dict[str, object]:
    return {
        "id": memory_id,
        "memory": text,
        "metadata": metadata or {},
        "score": score,
    }


def test_native_revision_has_priority_over_snapshot() -> None:
    first = _record("old", metadata={"revision_id": "rev-4"})
    second = _record("new", metadata={"revision_id": "rev-4"})

    assert memory_revision_identity(first) == "native:metadata.revision_id:rev-4"
    assert memory_revision_identity(second) == memory_revision_identity(first)


def test_snapshot_revision_ignores_retrieval_telemetry() -> None:
    first = _record(metadata={"access_count": 1, "last_accessed_at": "before"}, score=0.1)
    second = _record(metadata={"access_count": 9, "last_accessed_at": "after"}, score=0.99)

    assert memory_revision_identity(first) == memory_revision_identity(second)


def test_replay_reuses_revision_and_invalidates_changed_revision() -> None:
    state = MemoryAliasState()
    first, delta = state.plan_records([_record()])
    assert first[0]["id"] == "m1"
    assert delta is not None
    assert state.apply(delta)

    repeated, repeated_delta = state.plan_records([_record(score=0.1)])
    assert repeated[0]["id"] == "m1"
    assert repeated_delta is None

    changed, changed_delta = state.plan_records([_record("Changed")])
    assert changed[0]["id"] == "m2"
    assert changed_delta == {
        "version": 1,
        "bindings": [
            {
                "alias": "m2",
                "memory_id": MEMORY_ID,
                "revision": memory_revision_identity(_record("Changed")),
            }
        ],
        "invalidated": ["m1"],
    }
    assert state.apply(changed_delta)
    with pytest.raises(MemoryAliasError, match="stale"):
        state.resolve("m1")
    assert state.resolve("m2") == MEMORY_ID


def test_unsettled_retry_plans_the_same_alias_and_delta() -> None:
    state = MemoryAliasState()

    first = state.plan_records([_record()])
    retried = state.plan_records([_record()])

    assert retried == first
    assert state.apply(first[1])
    assert state.apply(first[1])
    assert state.resolve("m1") == MEMORY_ID


def test_unknown_and_cross_session_aliases_fail_closed() -> None:
    first_session = MemoryAliasState()
    _, delta = first_session.plan_records([_record()])
    assert first_session.apply(delta)

    with pytest.raises(MemoryAliasError, match="unknown"):
        MemoryAliasState().resolve("m1")


def test_rotated_session_starts_with_an_independent_alias_map() -> None:
    previous = MemoryAliasState()
    _, delta = previous.plan_records([_record()])
    assert previous.apply(delta)

    successor = MemoryAliasState()
    projected, successor_delta = successor.plan_records([_record()])

    assert projected[0]["id"] == "m1"
    assert successor_delta is not None


def test_conflicting_replay_never_rebinds_alias() -> None:
    state = MemoryAliasState()
    _, delta = state.plan_records([_record()])
    assert state.apply(delta)
    conflict = {
        "version": 1,
        "bindings": [
            {
                "alias": "m1",
                "memory_id": "fedcba98-7654-4321-8123-456789abcdef",
                "revision": "native:revision:2",
            }
        ],
        "invalidated": [],
    }

    assert state.apply(conflict) is False
    assert state.resolve("m1") == MEMORY_ID


def test_same_delta_expected_revision_conflict_is_rejected_atomically() -> None:
    state = MemoryAliasState()
    delta = {
        "version": 1,
        "bindings": [
            {
                "alias": "m1",
                "memory_id": MEMORY_ID,
                "revision": "native:revision_id:revision-1",
                "expected_revision": "revision-1",
            },
            {
                "alias": "m1",
                "memory_id": MEMORY_ID,
                "revision": "native:revision_id:revision-1",
                "expected_revision": "revision-2",
            },
        ],
        "invalidated": [],
    }

    assert state.apply(delta) is False
    assert state.bindings == {}


def test_unsafe_native_revision_uses_snapshot_fallback() -> None:
    record = _record(metadata={"revision_id": "bad\u007fheader"})

    assert memory_revision_identity(record).startswith("snapshot-sha256:")


@pytest.mark.parametrize(
    ("revision", "expected_revision"),
    [
        ("snapshot-sha256:abc", "revision-1"),
        ("native:revision_id:revision-1", "revision-2"),
        ("native:revision_id:abc:def", "def"),
        ("native:revision_id:revision-1", "bad\u007fheader"),
        ("native:revision_id:revision-1", "x" * 513),
    ],
)
def test_replay_rejects_invalid_expected_revision(revision: str, expected_revision: str) -> None:
    state = MemoryAliasState()

    assert (
        state.apply(
            {
                "version": 1,
                "bindings": [
                    {
                        "alias": "m1",
                        "memory_id": MEMORY_ID,
                        "revision": revision,
                        "expected_revision": expected_revision,
                    }
                ],
                "invalidated": [],
            }
        )
        is False
    )
    assert state.bindings == {}


def test_canonical_uuid_and_legacy_canonical_ids_pass_through() -> None:
    state = MemoryAliasState()

    assert state.resolve(MEMORY_ID) == MEMORY_ID
    assert state.resolve("mem_legacy") == "mem_legacy"


def test_redis_snapshot_restores_alias_state() -> None:
    state = MemoryAliasState()
    _, delta = state.plan_records([_record()])
    assert state.apply(delta)

    restored = MemoryAliasState()
    assert restored.apply(state.snapshot())
    assert restored.resolve("m1") == MEMORY_ID
    assert restored.next_number == 2


def test_session_cache_replays_recall_and_tool_result_metadata() -> None:
    cache = SessionCache(guardrails=None)
    entry = CachedSessionState(session_id="session-a", intaris_session_id="session-a")
    first = MemoryAliasState()
    _, first_delta = first.plan_records([_record()])
    cache._apply_cached_event(  # noqa: SLF001
        entry,
        CachedEvent(
            seq=1,
            type="developer_message",
            data={"source": "memory_search", "memory_aliases": first_delta},
        ),
    )
    _, changed_delta = entry.memory_aliases.plan_records([_record("Changed")])
    cache._apply_cached_event(  # noqa: SLF001
        entry,
        CachedEvent(
            seq=2,
            type="tool_result",
            data={"name": "memory_update", "memory_aliases": changed_delta},
        ),
    )

    assert entry.memory_aliases.resolve("m2") == MEMORY_ID
    with pytest.raises(MemoryAliasError, match="stale"):
        entry.memory_aliases.resolve("m1")


def test_session_cache_redis_round_trip_is_acceleration_only() -> None:
    entry = CachedSessionState(session_id="session-a", intaris_session_id="session-a")
    _, delta = entry.memory_aliases.plan_records([_record()])
    assert entry.memory_aliases.apply(delta)

    restored = _deserialize_entry(_serialize_entry(entry))

    assert restored.memory_aliases.resolve("m1") == MEMORY_ID


def test_authoritative_replay_replaces_accelerated_alias_state() -> None:
    cache = SessionCache(guardrails=None)
    entry = CachedSessionState(session_id="session-a", intaris_session_id="session-a")
    _, delta = entry.memory_aliases.plan_records([_record()])
    assert entry.memory_aliases.apply(delta)

    cache._replace_from_intaris_events(entry, [])  # noqa: SLF001

    with pytest.raises(MemoryAliasError, match="unknown"):
        entry.memory_aliases.resolve("m1")
