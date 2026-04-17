from __future__ import annotations

import pytest

from cognis.core.immutable_prefix import ImmutablePrefixEntry
from cognis.core.session_cache import SessionCache
from cognis.models.session import EventAppendResult, SessionEvent, SessionModel


class _Guardrails:
    def __init__(self) -> None:
        self.calls: list[int] = []

    async def read_events(
        self,
        session_id: str,
        after_seq: int = 0,
        limit: int = 0,
        types: list[str] | None = None,
        last_n: int | None = None,
        allow_missing_stream: bool = False,
    ) -> object:
        del session_id, allow_missing_stream, limit, types, last_n
        self.calls.append(after_seq)
        if after_seq == 0:
            return type(
                "EventRead",
                (),
                {
                    "events": [
                        {"seq": 1, "type": "user_message", "data": {"content": "old"}},
                        {"seq": 2, "type": "assistant_message", "data": {"content": "older"}},
                        {
                            "seq": 3,
                            "type": "compaction_summary",
                            "data": {"summary": "compact"},
                        },
                        {"seq": 4, "type": "user_message", "data": {"content": "recent"}},
                        {
                            "seq": 10,
                            "type": "developer_message",
                            "data": {
                                "role": "developer",
                                "source": "memory_instructions",
                                "content": "Use memory carefully.",
                            },
                        },
                        {
                            "seq": 11,
                            "type": "developer_message",
                            "data": {
                                "role": "developer",
                                "source": "core_memories",
                                "content": "Prefers Python.",
                            },
                        },
                        {
                            "seq": 12,
                            "type": "context_snapshot",
                            "data": {
                                "source": "bootstrap",
                                "entries": [
                                    {
                                        "role": "developer",
                                        "source": "memory_instructions",
                                        "seq": 10,
                                    },
                                    {
                                        "role": "developer",
                                        "source": "core_memories",
                                        "seq": 11,
                                    },
                                ],
                            },
                        },
                    ],
                    "last_seq": 12,
                    "has_more": False,
                },
            )()
        return type(
            "EventRead",
            (),
            {
                "events": [{"seq": 5, "type": "assistant_message", "data": {"content": "latest"}}],
                "last_seq": 5,
                "has_more": False,
            },
        )()


def _session(session_id: str = "session-1") -> SessionModel:
    return SessionModel(
        session_id=session_id,
        conversation_id="conv-1",
        user_email="user@example.com",
        agent_id="agent-1",
        intaris_session_id=session_id,
    )


@pytest.mark.asyncio
async def test_session_cache_cold_and_warm_paths() -> None:
    guardrails = _Guardrails()
    cache = SessionCache(guardrails, max_entries=10)

    entry = await cache.refresh(_session())

    assert guardrails.calls == [0]
    assert entry.last_compaction_seq == 3
    assert entry.last_compaction_summary == "compact"
    assert [event.seq for event in entry.events] == [4]
    assert [item.source for item in cache.get_prefix_entries("session-1")] == [
        "memory_instructions",
        "core_memories",
    ]

    warm_entry = await cache.refresh(_session())

    assert warm_entry is entry
    assert guardrails.calls == [0, 12]
    assert [event.seq for event in warm_entry.events] == [4, 5]


@pytest.mark.asyncio
async def test_session_cache_appends_recorded_events_and_applies_compaction() -> None:
    cache = SessionCache(_Guardrails(), max_entries=10)
    session = _session()
    await cache.refresh(session)

    append_result = EventAppendResult(ok=True, count=1, first_seq=6, last_seq=6)
    await cache.append_recorded_events(
        session,
        [SessionEvent(type="assistant_message", data={"content": "new"})],
        append_result,
    )
    assert [event.seq for event in cache.get_events_since_compaction(session.session_id)] == [4, 6]

    await cache.apply_compaction(session, summary="fresh summary", compaction_seq=6)
    assert cache.get_compaction_summary(session.session_id) == "fresh summary"
    assert cache.get_events_since_compaction(session.session_id) == []


@pytest.mark.asyncio
async def test_store_prefix_snapshot_replaces_active_prefix() -> None:
    cache = SessionCache(_Guardrails(), max_entries=10)
    session = _session()
    await cache.refresh(session)

    await cache.store_prefix_snapshot(
        session.session_id,
        [
            ImmutablePrefixEntry(
                role="developer",
                source="memory_instructions",
                content="Instructions v2",
                seq=20,
            ),
            ImmutablePrefixEntry(
                role="developer",
                source="core_memories",
                content="Core v2",
                seq=21,
            ),
        ],
        snapshot_seq=22,
        snapshot_source="repair",
    )

    assert [item.content for item in cache.get_prefix_entries(session.session_id)] == [
        "Instructions v2",
        "Core v2",
    ]
    assert cache.needs_prefix_repair(session.session_id) is False


@pytest.mark.asyncio
async def test_mark_prefix_repair_needed_sets_flag() -> None:
    cache = SessionCache(_Guardrails(), max_entries=10)
    session = _session()
    await cache.refresh(session)

    await cache.mark_prefix_repair_needed(session.session_id)

    assert cache.needs_prefix_repair(session.session_id) is True


@pytest.mark.asyncio
async def test_session_cache_evicts_oldest_unlocked_entry_when_full() -> None:
    guardrails = _Guardrails()
    cache = SessionCache(guardrails, max_entries=1)

    first = await cache.refresh(_session("session-1"))
    first.touched_at = 1.0

    await cache.refresh(_session("session-2"))

    assert cache.get_entry("session-1") is None
    assert cache.get_entry("session-2") is not None
