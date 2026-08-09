from __future__ import annotations

from types import SimpleNamespace
from typing import cast

import pytest

from cognis.core.immutable_prefix import ImmutablePrefixEntry
from cognis.core.session_cache import CachedEvent
from cognis.core.session_fork import fork_session_events
from cognis.models.session import SessionEvent


class _SessionCache:
    def __init__(self) -> None:
        self.events: list[CachedEvent] = [
            CachedEvent(seq=1, type="system_message", data={"content": "old identity"}),
            CachedEvent(seq=2, type="user_message", data={"content": "source input"}),
        ]
        self.prefix_entries: list[ImmutablePrefixEntry] = [
            ImmutablePrefixEntry(role="system", source="identity", content="old identity", seq=1),
            ImmutablePrefixEntry(
                role="developer",
                source="core_memories",
                content="private owner memory",
                seq=2,
            ),
        ]
        self.seeded_events: list[CachedEvent] = []
        self.seed_last_seqs: list[int] = []
        self.stored_prefix: list[ImmutablePrefixEntry] = []

    def get_entry(self, session_id: str) -> object:
        del session_id
        return SimpleNamespace(initialized=True, events=self.events)

    def get_prefix_entries(self, session_id: str) -> list[ImmutablePrefixEntry]:
        del session_id
        return list(self.prefix_entries)

    async def seed_events(self, session: object, events: list[CachedEvent], last_seq: int) -> None:
        del session
        self.seeded_events.extend(events)
        self.seed_last_seqs.append(last_seq)

    async def append_recorded_events(
        self, session: object, events: list[object], result: object
    ) -> None:
        del session, events, result

    async def store_prefix_snapshot(
        self,
        session_id: str,
        entries: list[ImmutablePrefixEntry],
        *,
        snapshot_seq: int,
        snapshot_source: str,
    ) -> None:
        del session_id, snapshot_seq, snapshot_source
        self.stored_prefix = list(entries)


class _Guardrails:
    def __init__(self) -> None:
        self.recorded_events: list[SessionEvent] = []
        self.recorded_batches: list[list[SessionEvent]] = []
        self.read_kwargs: list[dict[str, object]] = []

    async def read_events(self, **kwargs: object) -> object:
        self.read_kwargs.append(dict(kwargs))
        return SimpleNamespace(events=[])

    async def record_events(self, **kwargs: object) -> object:
        events = cast(list[SessionEvent], kwargs.get("events", []))
        first_seq = len(self.recorded_events) + 1
        self.recorded_batches.append(events)
        self.recorded_events.extend(events)
        return SimpleNamespace(ok=True, first_seq=first_seq, last_seq=len(self.recorded_events))


@pytest.mark.asyncio
async def test_fork_session_events_can_skip_source_prefix() -> None:
    cache = _SessionCache()
    guardrails = _Guardrails()

    copied = await fork_session_events(
        providers=SimpleNamespace(guardrails=guardrails),
        session_cache=cache,
        source_cognis_session_id="source-session",
        source_intaris_session_id="source-intaris",
        target_session=SimpleNamespace(
            session_id="target-session", intaris_session_id="target-intaris"
        ),
        source_label="plan",
        copy_prefix=False,
    )

    assert copied is True
    assert [event.type for event in cache.seeded_events] == ["user_message"]
    assert cache.stored_prefix == []
    assert [event.type for event in guardrails.recorded_events] == ["user_message"]


@pytest.mark.asyncio
async def test_fork_session_events_preserves_copied_event_payloads() -> None:
    cache = _SessionCache()
    cache.events = [
        CachedEvent(seq=1, type="user_message", data={"content": "without turn"}),
        CachedEvent(
            seq=2,
            type="assistant_message",
            data={"content": "with turn", "turn_id": "turn-1"},
        ),
    ]
    guardrails = _Guardrails()

    copied = await fork_session_events(
        providers=SimpleNamespace(guardrails=guardrails),
        session_cache=cache,
        source_cognis_session_id="source-session",
        source_intaris_session_id="source-intaris",
        target_session=SimpleNamespace(
            session_id="target-session", intaris_session_id="target-intaris"
        ),
        source_label="undo",
        copy_prefix=False,
    )

    assert copied is True
    assert [event.data for event in guardrails.recorded_events] == [
        {"content": "without turn"},
        {"content": "with turn", "turn_id": "turn-1"},
    ]
    assert "turn_id" not in guardrails.recorded_events[0].model_dump()["data"]


@pytest.mark.asyncio
async def test_fork_session_events_can_require_durable_source_history() -> None:
    cache = _SessionCache()
    cache.events = [CachedEvent(seq=1, type="user_message", data={"content": "stale"})]

    class _DurableGuardrails(_Guardrails):
        async def read_events(self, **kwargs: object) -> object:
            self.read_kwargs.append(dict(kwargs))
            return SimpleNamespace(
                events=[
                    {"seq": 1, "type": "user_message", "data": {"content": "one"}},
                    {"seq": 2, "type": "assistant_message", "data": {"content": "two"}},
                ]
            )

    guardrails = _DurableGuardrails()

    copied = await fork_session_events(
        providers=SimpleNamespace(guardrails=guardrails),
        session_cache=cache,
        source_cognis_session_id="source-session",
        source_intaris_session_id="source-intaris",
        target_session=SimpleNamespace(
            session_id="target-session", intaris_session_id="target-intaris"
        ),
        source_label="reuse_recovery",
        copy_prefix=False,
        prefer_durable_source=True,
    )

    assert copied is True
    assert guardrails.read_kwargs == [
        {
            "session_id": "source-intaris",
            "after_seq": 0,
            "allow_missing_stream": False,
        }
    ]
    assert [event.data["content"] for event in guardrails.recorded_events] == ["one", "two"]


@pytest.mark.asyncio
async def test_fork_session_events_copies_source_events_in_intaris_sized_batches() -> None:
    cache = _SessionCache()
    cache.events = [
        CachedEvent(seq=index, type="user_message", data={"content": f"message {index}"})
        for index in range(1, 1003)
    ]
    guardrails = _Guardrails()

    copied = await fork_session_events(
        providers=SimpleNamespace(guardrails=guardrails),
        session_cache=cache,
        source_cognis_session_id="source-session",
        source_intaris_session_id="source-intaris",
        target_session=SimpleNamespace(
            session_id="target-session", intaris_session_id="target-intaris"
        ),
        source_label="conversation_fork",
        copy_prefix=False,
    )

    assert copied is True
    assert [len(batch) for batch in guardrails.recorded_batches] == [1000, 2]
    assert len(cache.seeded_events) == 1002
    assert [event.seq for event in cache.seeded_events] == list(range(1, 1003))
    assert cache.seed_last_seqs == [1000, 1002]
    assert guardrails.recorded_events[0].data == {"content": "message 1"}
    assert guardrails.recorded_events[-1].data == {"content": "message 1002"}


@pytest.mark.asyncio
async def test_fork_session_events_skips_non_appendable_source_events() -> None:
    cache = _SessionCache()
    cache.events = [
        CachedEvent(seq=1, type="user_message", data={"content": "copy me"}),
        CachedEvent(seq=2, type="tool_result_chunk", data={"content": "live only"}),
        CachedEvent(seq=3, type="assistant_message", data={"content": "copy me too"}),
    ]
    guardrails = _Guardrails()

    copied = await fork_session_events(
        providers=SimpleNamespace(guardrails=guardrails),
        session_cache=cache,
        source_cognis_session_id="source-session",
        source_intaris_session_id="source-intaris",
        target_session=SimpleNamespace(
            session_id="target-session", intaris_session_id="target-intaris"
        ),
        source_label="task_chat",
        copy_prefix=False,
    )

    assert copied is True
    assert [event.type for event in guardrails.recorded_events] == [
        "user_message",
        "assistant_message",
    ]
    assert [event.type for event in cache.seeded_events] == [
        "user_message",
        "assistant_message",
    ]


@pytest.mark.asyncio
async def test_fork_session_events_preserves_prefix_by_default() -> None:
    cache = _SessionCache()
    guardrails = _Guardrails()

    copied = await fork_session_events(
        providers=SimpleNamespace(guardrails=guardrails),
        session_cache=cache,
        source_cognis_session_id="source-session",
        source_intaris_session_id="source-intaris",
        target_session=SimpleNamespace(
            session_id="target-session", intaris_session_id="target-intaris"
        ),
        source_label="conversation_fork",
    )

    assert copied is True
    assert [entry.source for entry in cache.stored_prefix] == ["identity", "core_memories"]
    assert [event.type for event in guardrails.recorded_events] == [
        "user_message",
        "system_message",
        "developer_message",
        "context_snapshot",
    ]
    assert all(
        "turn_id" not in event.model_dump()["data"] for event in guardrails.recorded_events[1:]
    )


@pytest.mark.asyncio
async def test_fork_session_events_tolerates_missing_source_stream() -> None:
    cache = _SessionCache()
    cache.events = []
    cache.prefix_entries = []
    guardrails = _Guardrails()

    copied = await fork_session_events(
        providers=SimpleNamespace(guardrails=guardrails),
        session_cache=cache,
        source_cognis_session_id="source-session",
        source_intaris_session_id="source-intaris",
        target_session=SimpleNamespace(
            session_id="target-session", intaris_session_id="target-intaris"
        ),
        source_label="conversation_fork",
    )

    assert copied is False
    assert guardrails.read_kwargs == [
        {
            "session_id": "source-intaris",
            "after_seq": 0,
            "allow_missing_stream": True,
        }
    ]
    assert guardrails.recorded_events == []
