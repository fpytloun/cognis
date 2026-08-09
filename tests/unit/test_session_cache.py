from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from cognis.core.immutable_prefix import ImmutablePrefixEntry
from cognis.core.project_context import (
    ProjectContextEntry,
    ProjectMetadataEntry,
    normalize_project_path,
    project_context_event_data,
)
from cognis.core.redis_service import RedisService
from cognis.core.session_cache import SessionCache
from cognis.models.session import EventAppendResult, SessionEvent, SessionModel


@pytest.mark.asyncio
async def test_session_cache_closes_only_owned_redis_service(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    injected = RedisService("")
    injected_close = AsyncMock()
    monkeypatch.setattr(injected, "aclose", injected_close)
    injected_cache = SessionCache(_Guardrails(), redis_service=injected)

    await injected_cache.aclose()

    injected_close.assert_not_awaited()

    owned_close = AsyncMock()
    monkeypatch.setattr("cognis.core.session_cache.RedisService.aclose", owned_close)
    owned_cache = SessionCache(_Guardrails(), redis_url="redis://localhost")

    await owned_cache.aclose()

    owned_close.assert_awaited_once()


@pytest.mark.asyncio
async def test_session_cache_uses_byte_payloads_with_injected_service(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = RedisService("")
    redis_set = AsyncMock(return_value=True)
    monkeypatch.setattr(service, "set", redis_set)
    cache = SessionCache(_Guardrails(), redis_service=service, redis_ttl_seconds=45)

    await cache.refresh(_session("session-bytes"))

    key, payload = redis_set.await_args.args
    assert key == "cognis:session-cache:v2:session-bytes"
    assert isinstance(payload, bytes)
    assert redis_set.await_args.kwargs == {"ttl_seconds": 45}


@pytest.mark.asyncio
async def test_session_cache_classifies_redis_read_failure_as_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = RedisService("")
    service.configured = True
    redis_get = AsyncMock(return_value=None)
    monkeypatch.setattr(service, "get", redis_get)
    error_inc = Mock()
    miss_inc = Mock()
    monkeypatch.setattr(
        "cognis.core.session_cache.REDIS_ERRORS",
        SimpleNamespace(inc=error_inc),
    )
    monkeypatch.setattr(
        "cognis.core.session_cache.REDIS_MISSES",
        SimpleNamespace(inc=miss_inc),
    )
    cache = SessionCache(_Guardrails(), redis_service=service)

    assert await cache._redis_get("session-failed") is None  # noqa: SLF001

    error_inc.assert_called_once_with()
    miss_inc.assert_not_called()
    redis_get.assert_awaited_once_with("cognis:session-cache:v2:session-failed")


@pytest.mark.asyncio
async def test_local_eviction_waits_for_locked_entry_without_touching_redis() -> None:
    cache = SessionCache(_Guardrails(), max_entries=10)
    session = _session("session-locked")
    entry = await cache.refresh(session)
    redis_delete = AsyncMock()
    cache._redis_delete = redis_delete  # type: ignore[method-assign]  # noqa: SLF001

    await entry.lock.acquire()
    try:
        assert not await cache.evict_local(session.session_id)
        assert cache.get_entry(session.session_id) is entry
        eviction_task = cache._local_eviction_tasks[session.session_id]  # noqa: SLF001
    finally:
        entry.lock.release()

    await asyncio.wait_for(eviction_task, timeout=1)
    assert cache.get_entry(session.session_id) is None
    redis_delete.assert_not_awaited()
    await cache.aclose()


@pytest.mark.asyncio
async def test_canonical_invalidation_preserves_ephemeral_session_overrides() -> None:
    cache = SessionCache(_Guardrails(), max_entries=10)
    session = _session("session-overrides")
    entry = await cache.refresh(session)
    entry.model_override = "openai/test-model"
    entry.model_override_provider_id = "provider-1"
    entry.reasoning_effort_override = "high"
    entry.loaded_skill_ids = {"skill-1"}
    entry.activated_skill_ids = {"skill-1"}
    entry.last_tool_runtime_info = {"executor_id": "executor-1"}
    entry.discovered_tool_handles = {"tool-1": object()}  # type: ignore[dict-item]
    project_metadata = ProjectMetadataEntry(
        project_id="project-1",
        project_name="Project One",
        content="Metadata",
        content_hash="metadata-hash",
        seq=12,
    )
    project_context = ProjectContextEntry(
        project_root="/workspace/project-1",
        source_path="/workspace/project-1/AGENTS.md",
        content="Instructions",
        content_hash="instructions-hash",
        seq=13,
    )
    entry.project_metadata_contexts = {"project-1": project_metadata}
    entry.project_contexts = {project_context.project_root: project_context}

    assert await cache.invalidate_canonical(session.session_id)
    assert entry.canonical_stale
    assert entry.events == []
    assert entry.last_event_seq == 0
    assert entry.discovered_tool_handles == {}
    assert entry.project_metadata_contexts == {"project-1": project_metadata}
    assert entry.project_contexts == {project_context.project_root: project_context}
    refreshed = await cache.refresh(session)

    assert refreshed is entry
    assert not refreshed.canonical_stale
    assert refreshed.model_override == "openai/test-model"
    assert refreshed.model_override_provider_id == "provider-1"
    assert refreshed.reasoning_effort_override == "high"
    assert refreshed.loaded_skill_ids == {"skill-1"}
    assert refreshed.activated_skill_ids == {"skill-1"}
    assert refreshed.last_tool_runtime_info == {"executor_id": "executor-1"}
    assert refreshed.project_metadata_contexts == {"project-1": project_metadata}
    assert refreshed.project_contexts == {project_context.project_root: project_context}
    await cache.aclose()


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
                        {
                            "seq": 2,
                            "type": "assistant_message",
                            "data": {
                                "content": "older",
                                "responses_output_items": [
                                    {
                                        "type": "reasoning",
                                        "id": "rs_cached",
                                        "encrypted_content": "opaque",
                                    }
                                ],
                            },
                        },
                        {
                            "seq": 3,
                            "type": "compaction_summary",
                            "data": {"summary": "compact"},
                        },
                        {
                            "seq": 4,
                            "type": "assistant_message",
                            "data": {
                                "content": "recent",
                                "responses_output_items": [
                                    {
                                        "type": "reasoning",
                                        "id": "rs_tail",
                                        "encrypted_content": "opaque-tail",
                                    }
                                ],
                            },
                        },
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
                                "extras": {
                                    "memory_policy_fingerprint": "policy-v1",
                                    "memory_mode": "full_auto",
                                },
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


class _RestartGuardrails:
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
                        {
                            "seq": 1,
                            "type": "developer_message",
                            "data": {
                                "role": "developer",
                                "source": "core_memories",
                                "content": "Existing identity.",
                            },
                        },
                        {
                            "seq": 2,
                            "type": "context_snapshot",
                            "data": {
                                "source": "bootstrap",
                                "entries": [
                                    {
                                        "role": "developer",
                                        "source": "core_memories",
                                        "seq": 1,
                                    }
                                ],
                            },
                        },
                        {"seq": 3, "type": "user_message", "data": {"content": "before"}},
                        {
                            "seq": 4,
                            "type": "assistant_message",
                            "data": {"content": "previous reply"},
                        },
                        {
                            "seq": 5,
                            "type": "user_message",
                            "data": {"content": "continued after restart"},
                        },
                    ],
                    "last_seq": 5,
                    "has_more": False,
                },
            )()
        return type(
            "EventRead",
            (),
            {"events": [], "last_seq": after_seq, "has_more": False},
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
    assert entry.events[0].data["responses_output_items"][0]["encrypted_content"] == "opaque-tail"
    assert [item.source for item in cache.get_prefix_entries("session-1")] == [
        "memory_instructions",
        "core_memories",
    ]
    assert entry.memory_policy_fingerprint == "policy-v1"
    assert entry.memory_policy_mode == "full_auto"

    warm_entry = await cache.refresh(_session())

    assert warm_entry is entry
    assert guardrails.calls == [0, 12]
    assert [event.seq for event in warm_entry.events] == [4, 5]


@pytest.mark.asyncio
async def test_session_cache_warm_refresh_rebuilds_prefix_when_initialized_entry_is_missing_it() -> (
    None
):
    guardrails = _Guardrails()
    cache = SessionCache(guardrails, max_entries=10)
    session = _session()

    entry = await cache.refresh(session)
    guardrails.calls.clear()

    entry.prefix_entries = []
    entry.context_snapshot_seq = 0
    entry.context_snapshot_source = None

    warm_entry = await cache.refresh(session)

    assert warm_entry is entry
    assert guardrails.calls == [12, 0]
    assert [item.source for item in cache.get_prefix_entries(session.session_id)] == [
        "memory_instructions",
        "core_memories",
    ]


@pytest.mark.asyncio
async def test_session_cache_hydrates_full_history_after_restart_append() -> None:
    guardrails = _RestartGuardrails()
    cache = SessionCache(guardrails, max_entries=10)
    session = _session()

    entry = await cache.append_recorded_events(
        session,
        [SessionEvent(type="user_message", data={"content": "continued after restart"})],
        EventAppendResult(ok=True, count=1, first_seq=5, last_seq=5),
    )

    assert entry.initialized is False
    assert [event.seq for event in cache.get_events_since_compaction(session.session_id)] == [5]

    refreshed = await cache.refresh(session)

    assert refreshed.initialized is True
    assert guardrails.calls == [0]
    assert [event.seq for event in cache.get_events_since_compaction(session.session_id)] == [
        3,
        4,
        5,
    ]
    assert [
        event.data["content"] for event in cache.get_events_since_compaction(session.session_id)
    ] == [
        "before",
        "previous reply",
        "continued after restart",
    ]
    assert [item.source for item in cache.get_prefix_entries(session.session_id)] == [
        "core_memories"
    ]


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
async def test_get_events_since_compaction_returns_memoized_reference_until_append() -> None:
    cache = SessionCache(_Guardrails(), max_entries=10)
    session = _session()
    await cache.refresh(session)

    first = cache.get_events_since_compaction(session.session_id)
    second = cache.get_events_since_compaction(session.session_id)
    filtered_first = cache.get_events_since_compaction(
        session.session_id,
        ["user_message", "assistant_message"],
    )
    filtered_second = cache.get_events_since_compaction(
        session.session_id,
        ["user_message", "assistant_message"],
    )

    assert second is first
    assert filtered_second is filtered_first
    assert filtered_first is not first

    await cache.append_recorded_events(
        session,
        [SessionEvent(type="assistant_message", data={"content": "new"})],
        EventAppendResult(ok=True, count=1, first_seq=13, last_seq=13),
    )

    after_append = cache.get_events_since_compaction(session.session_id)
    assert after_append is not first
    assert [event.seq for event in after_append][-1] == 13


class _GapGuardrails:
    """Simulates a direct write (seq 6) recorded by another writer.

    Cold load returns seqs 1-4 (last_seq 4). The warm refresh after the
    gap-deferred append returns the externally written seq 6 plus the
    already-cached seqs 7-8.
    """

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
                        {
                            "seq": 1,
                            "type": "developer_message",
                            "data": {
                                "role": "developer",
                                "source": "core_memories",
                                "content": "Identity.",
                            },
                        },
                        {
                            "seq": 2,
                            "type": "context_snapshot",
                            "data": {
                                "source": "bootstrap",
                                "entries": [
                                    {
                                        "role": "developer",
                                        "source": "core_memories",
                                        "seq": 1,
                                    }
                                ],
                            },
                        },
                        {"seq": 3, "type": "user_message", "data": {"content": "delegate this"}},
                        {"seq": 4, "type": "tool_call", "data": {"name": "delegate"}},
                    ],
                    "last_seq": 4,
                    "has_more": False,
                },
            )()
        return type(
            "EventRead",
            (),
            {
                "events": [
                    {
                        "seq": 6,
                        "type": "delegation",
                        "data": {"status": "completed", "child_session_id": "child-1"},
                    },
                    {"seq": 7, "type": "tool_result", "data": {"name": "delegate"}},
                    {"seq": 8, "type": "assistant_message", "data": {"content": "done"}},
                ],
                "last_seq": 8,
                "has_more": False,
            },
        )()


@pytest.mark.asyncio
async def test_session_cache_gap_append_backfills_missing_seqs_before_publish() -> None:
    """A seq gap on append must repair the cache before another replica reads it.

    Scenario: a delegation-completed event is written directly to Intaris
    (seq 6, bypassing the cache) while the parent turn's next flush lands at
    seqs 7-8. Advancing the watermark to 8 would make a warm refresh
    (after_seq=8) skip seq 6 forever. Deferring repair until a later request
    exposes a partial timeline to another controller replica.
    """
    guardrails = _GapGuardrails()
    cache = SessionCache(guardrails, max_entries=10)
    session = _session()
    await cache.refresh(session)  # cold load, watermark=4

    # Parent flush lands at seqs 7-8 — seq 6 was written by another writer.
    entry = await cache.append_recorded_events(
        session,
        [
            SessionEvent(type="tool_result", data={"name": "delegate"}),
            SessionEvent(type="assistant_message", data={"content": "done"}),
        ],
        EventAppendResult(ok=True, count=2, first_seq=7, last_seq=8),
    )

    # The immediate repair reads after the contiguous watermark (4), not 8.
    assert guardrails.calls == [0, 4]
    seqs = [event.seq for event in cache.get_events_since_compaction(session.session_id)]
    assert seqs == [3, 4, 6, 7, 8]
    assert entry.last_event_seq == 8
    # No duplicates from the re-fetched seqs 7-8.
    assert len(seqs) == len(set(seqs))


@pytest.mark.asyncio
async def test_session_cache_failed_gap_backfill_marks_canonical_state_stale() -> None:
    guardrails = _GapGuardrails()
    cache = SessionCache(guardrails, max_entries=10)
    session = _session()
    await cache.refresh(session)

    async def failed_refresh(_session):
        raise RuntimeError("Intaris unavailable")

    cache.refresh = failed_refresh
    entry = await cache.append_recorded_events(
        session,
        [SessionEvent(type="assistant_message", data={"content": "later"})],
        EventAppendResult(ok=True, count=1, first_seq=7, last_seq=7),
    )

    assert entry.canonical_stale is True


@pytest.mark.asyncio
async def test_session_cache_skips_duplicate_seqs_on_idempotent_replay() -> None:
    """Re-appending a batch whose seqs are already cached must be a no-op.

    Intaris idempotency-key dedup returns the FIRST batch's seqs when a
    byte-identical batch is replayed; re-appending would duplicate the
    events out of position at the end of the cached list.
    """
    cache = SessionCache(_Guardrails(), max_entries=10)
    session = _session()
    await cache.refresh(session)

    events = [SessionEvent(type="assistant_message", data={"content": "notice"})]
    result = EventAppendResult(ok=True, count=1, first_seq=6, last_seq=6)
    await cache.append_recorded_events(session, events, result)
    await cache.append_recorded_events(session, events, result)

    seqs = [event.seq for event in cache.get_events_since_compaction(session.session_id)]
    assert seqs.count(6) == 1


@pytest.mark.asyncio
async def test_session_cache_orders_out_of_order_appends_by_seq() -> None:
    """Cache appends landing out of seq order must be consumed in seq order."""
    cache = SessionCache(_Guardrails(), max_entries=10)
    session = _session()
    await cache.refresh(session)

    entry = await cache.append_recorded_events(
        session,
        [SessionEvent(type="assistant_message", data={"content": "later"})],
        EventAppendResult(ok=True, count=1, first_seq=8, last_seq=8),
    )
    entry = await cache.append_recorded_events(
        session,
        [SessionEvent(type="delegation", data={"status": "completed"})],
        EventAppendResult(ok=True, count=1, first_seq=6, last_seq=6),
    )

    seqs = [event.seq for event in entry.events]
    assert seqs == sorted(seqs)


@pytest.mark.asyncio
async def test_session_cache_keeps_replayable_developer_context_in_history() -> None:
    cache = SessionCache(_Guardrails(), max_entries=10)
    session = _session("session-memory")

    await cache.append_recorded_events(
        session,
        [
            SessionEvent(
                type="developer_message",
                data={
                    "role": "developer",
                    "source": "memory_search",
                    "content": '<memory_context trust="untrusted">\n'
                    "Recalled memories:\n- Uses pytest\n</memory_context>",
                    "context_injection": True,
                    "replayable": True,
                    "replay_scope": "same_session",
                    "visibility": "agent_context",
                    "model_role": "system",
                },
            )
        ],
        EventAppendResult(ok=True, count=1, first_seq=1, last_seq=1),
    )

    events = cache.get_events_since_compaction(session.session_id)
    assert [event.type for event in events] == ["developer_message"]
    assert events[0].data["source"] == "memory_search"
    assert cache.get_prefix_entries(session.session_id) == []


@pytest.mark.asyncio
async def test_legacy_daily_brief_loaded_skill_event_preserves_v12_contract() -> None:
    cache = SessionCache(_Guardrails(), max_entries=10)
    session = _session("session-daily-v12")

    await cache.append_recorded_events(
        session,
        [
            SessionEvent(
                type="developer_message",
                data={
                    "kind": "loaded_skill",
                    "skill_id": "skill_daily",
                    "skill_name": "daily-brief",
                    "content_hash": "hash-v12",
                    "content": "<loaded_skill>Use daily_brief_v12.</loaded_skill>",
                },
            )
        ],
        EventAppendResult(ok=True, count=1, first_seq=1, last_seq=1),
    )

    snapshot = cache.get_loaded_skill_snapshots(session.session_id)["skill_daily"]
    assert snapshot["contract_version"] == 12
    assert snapshot["content_hash"] == "hash-v12"


@pytest.mark.asyncio
async def test_session_cache_tracks_discovered_tool_handles_from_lifecycle_events() -> None:
    cache = SessionCache(_Guardrails(), max_entries=10)
    session = _session("session-discovery")
    handle = {
        "tool_id": "mcp:googleworkspace:get_events",
        "name": "mcp_googleworkspace__get_events",
        "callable_name": "mcp_googleworkspace__get_events",
        "scope": "session",
        "category": "mcp",
        "profile_group": "office",
        "source": {
            "type": "intaris_mcp",
            "server_name": "googleworkspace",
            "raw_tool_name": "get_events",
        },
        "capabilities": ["read"],
        "read_only": True,
        "permission_scope": "current_session_effective_inventory",
        "confidence": 12.5,
        "discovered_at": "2026-04-28T10:00:00+00:00",
    }

    await cache.append_recorded_events(
        session,
        [
            SessionEvent(
                type="lifecycle",
                data={"event": "tool_discovery", "handles": [handle]},
            )
        ],
        EventAppendResult(ok=True, count=1, first_seq=1, last_seq=1),
    )

    assert cache.get_discovered_tool_ids(session.session_id) == {"mcp:googleworkspace:get_events"}
    cached = cache.get_discovered_tool_handles(session.session_id)
    assert cached["mcp:googleworkspace:get_events"]["source"]["server_name"] == "googleworkspace"

    await cache.append_recorded_events(
        session,
        [
            SessionEvent(
                type="tool_result",
                data={
                    "tool_id": "mcp:googleworkspace:get_events",
                    "name": "mcp_googleworkspace__get_events",
                    "is_error": False,
                },
            )
        ],
        EventAppendResult(ok=True, count=1, first_seq=2, last_seq=2),
    )

    assert "mcp:googleworkspace:get_events" in cache.get_discovered_tool_ids(session.session_id)


@pytest.mark.asyncio
async def test_session_cache_resets_transient_tools_at_step_boundary() -> None:
    cache = SessionCache(_Guardrails(), max_entries=10)
    session = _session("session-step-boundary")
    entry = await cache.refresh(session)
    entry.activated_skill_ids = {"planning"}
    entry.activated_skill_tool_ids_by_skill = {"planning": {"tool:planning"}}
    entry.activated_skill_tool_ids = {"tool:planning"}
    entry.discovered_tool_handles = {"tool:planning": {"name": "planning"}}
    entry.classified_inventory = {"tool:planning": "dynamic"}
    entry.skill_tool_classifications = {"planning": {"tool:planning": "dynamic"}}
    entry.last_tool_runtime_info = {"executor_id": "executor-1"}

    assert await cache.reset_step_tool_state(session.session_id)

    assert entry.activated_skill_ids == set()
    assert entry.activated_skill_tool_ids == set()
    assert entry.discovered_tool_handles == {}
    assert entry.classified_inventory == {}
    assert entry.skill_tool_classifications == {}
    assert entry.last_tool_runtime_info == {}


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
        memory_policy_fingerprint="policy-v2",
        memory_policy_mode="on_demand",
    )

    assert [item.content for item in cache.get_prefix_entries(session.session_id)] == [
        "Instructions v2",
        "Core v2",
    ]
    assert cache.needs_prefix_repair(session.session_id) is False
    entry = cache.get_entry(session.session_id)
    assert entry is not None
    assert entry.memory_policy_fingerprint == "policy-v2"
    assert entry.memory_policy_mode == "on_demand"


@pytest.mark.asyncio
async def test_session_cache_restores_project_context_from_recorded_events() -> None:
    cache = SessionCache(_Guardrails(), max_entries=10)
    session = _session()
    await cache.refresh(session)

    project_context = ProjectContextEntry(
        project_root="/tmp/example",
        source_path="/tmp/example/AGENTS.md",
        content="Instructions for project at /tmp/example loaded from /tmp/example/AGENTS.md.",
        content_hash="hash",
        working_directory="/tmp/example",
    )
    append_result = EventAppendResult(ok=True, count=1, first_seq=20, last_seq=20)
    await cache.append_recorded_events(
        session,
        [
            SessionEvent(
                type="developer_message",
                data=project_context_event_data(project_context, turn_id="turn-1"),
            )
        ],
        append_result,
    )

    loaded = cache.get_project_contexts(session.session_id)

    assert len(loaded) == 1
    assert loaded[0].project_root == normalize_project_path("/tmp/example")
    assert loaded[0].seq == 20


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


@pytest.mark.asyncio
async def test_session_cache_exposes_last_llm_usage_in_context_snapshot() -> None:
    cache = SessionCache(_Guardrails(), max_entries=10)
    session = _session()
    await cache.refresh(session)

    cache.update_context_usage(
        session,
        prompt_tokens=2_000,
        max_context_tokens=8_000,
        max_input_tokens=6_000,
        available_prompt_tokens=6_000,
        model="gpt-5.4",
        provider_id="proxy",
        reasoning_effort="high",
        agent_id="agent-1",
        agent_profile_id="build",
        requested_agent_profile_id="build",
        agent_profile_source="conversation",
        agent_profile_synthetic=False,
        reserve_output_tokens=1_000,
        effective_reserve_output_tokens=1_000,
        compaction_threshold=0.85,
        projection_policy={
            "phase": "within_turn",
            "pressure_mode": "normal",
            "steady_target_tokens": 5_000,
        },
    )
    cache.update_last_llm_usage(
        session.session_id,
        {
            "prompt_tokens": 1_500,
            "completion_tokens": 200,
            "total_tokens": 1_700,
            "cached_tokens": 1_024,
            "cache_read_input_tokens": 900,
            "cache_creation_input_tokens": 124,
        },
    )

    usage = cache.get_context_usage(session.session_id)

    assert usage is not None
    assert usage["provider_id"] == "proxy"
    assert usage["reasoning_effort"] == "high"
    assert usage["agent_id"] == "agent-1"
    assert usage["agent_profile_id"] == "build"
    assert usage["requested_agent_profile_id"] == "build"
    assert usage["agent_profile_source"] == "conversation"
    assert usage["agent_profile_synthetic"] is False
    assert usage["max_input_tokens"] == 6_000
    assert usage["available_prompt_tokens"] == 6_000
    assert usage["effective_prompt_budget"] == 6_000
    assert usage["compaction_threshold"] == 0.85
    assert usage["projection_policy"] == {
        "phase": "within_turn",
        "pressure_mode": "normal",
        "steady_target_tokens": 5_000,
    }
    assert usage["last_llm_usage"] == {
        "prompt_tokens": 1_500,
        "completion_tokens": 200,
        "total_tokens": 1_700,
        "cached_tokens": 1_024,
        "cache_read_input_tokens": 900,
        "cache_creation_input_tokens": 124,
    }

    cache.update_last_generation_performance(
        session.session_id,
        {
            "is_local": True,
            "model": "qwen3:8b",
            "runtime": "Ollama",
            "generation_tokens_per_second": 25,
            "measured_at": "2026-07-13T12:00:00Z",
        },
    )
    performance = cache.get_last_generation_performance(session.session_id)
    assert performance is not None
    assert performance["model"] == "qwen3:8b"
    assert performance["generation_tokens_per_second"] == 25

    cache.update_last_generation_performance(session.session_id, None)
    assert cache.get_last_generation_performance(session.session_id) is None

    cache.update_context_usage(
        session,
        prompt_tokens=1_000,
        max_context_tokens=8_000,
        model="gpt-5.4",
        provider_id="proxy",
    )
    cleared_usage = cache.get_context_usage(session.session_id)

    assert cleared_usage is not None
    assert cleared_usage["reasoning_effort"] is None
    assert cleared_usage["agent_id"] is None
    assert cleared_usage["agent_profile_id"] is None
    assert cleared_usage["requested_agent_profile_id"] is None
    assert cleared_usage["agent_profile_source"] is None
    assert cleared_usage["agent_profile_synthetic"] is None


@pytest.mark.asyncio
async def test_classified_inventory_memo_round_trip() -> None:
    cache = SessionCache(_Guardrails(), max_entries=10)
    session = _session("session-memo")
    await cache.refresh(session)

    assert cache.get_classified_inventory(session.session_id, "fp1") is None

    cache.set_classified_inventory(session.session_id, "fp1", ["a", "b"])
    assert cache.get_classified_inventory(session.session_id, "fp1") == ["a", "b"]

    # Different fingerprint must miss; the memo bounds itself to the
    # latest fingerprint to prevent unbounded growth from rotating
    # inventories within the same session.
    cache.set_classified_inventory(session.session_id, "fp2", ["c"])
    assert cache.get_classified_inventory(session.session_id, "fp1") is None
    assert cache.get_classified_inventory(session.session_id, "fp2") == ["c"]


@pytest.mark.asyncio
async def test_invalidate_classified_inventory_clears_memo() -> None:
    cache = SessionCache(_Guardrails(), max_entries=10)
    session = _session("session-memo")
    await cache.refresh(session)

    cache.set_classified_inventory(session.session_id, "fp1", ["a"])
    cache.invalidate_classified_inventory(session.session_id)

    assert cache.get_classified_inventory(session.session_id, "fp1") is None


# ---------------------------------------------------------------------------
# active_thinking lifecycle — reconnect re-injection bug regression tests
# ---------------------------------------------------------------------------


def test_clear_active_thinking_removes_state() -> None:
    """clear_active_thinking removes all blocks for a session."""
    cache = SessionCache(_Guardrails(), max_entries=10)
    session_id = "sess-thinking-clear"

    # Seed an incomplete thinking block (simulates a cancelled turn)
    cache.update_active_thinking(
        session_id,
        message_id="msg1",
        turn_id="turn1",
        block_id="blk1",
        delta="partial content",
        title="Thinking",
        complete=False,
    )
    assert cache.active_thinking_snapshots(session_id) != []

    # clear_active_thinking must remove it
    cache.clear_active_thinking(session_id)
    assert cache.active_thinking_snapshots(session_id) == []


def test_discard_incomplete_active_thinking_drops_failed_attempt_blocks() -> None:
    """Mid-stream retry: the failed attempt's incomplete blocks are dropped,
    but the retried attempt's deltas are NOT blocked (unlike clear)."""
    cache = SessionCache(_Guardrails(), max_entries=10)
    session_id = "sess-thinking-retry"

    cache.update_active_thinking(
        session_id,
        message_id="msg1",
        turn_id="turn1",
        block_id="thk_attempt1_1",
        delta="partial reasoning",
        title="Thinking",
        complete=False,
    )
    assert cache.active_thinking_snapshots(session_id) != []

    cache.discard_incomplete_active_thinking(session_id)
    assert cache.active_thinking_snapshots(session_id) == []

    # The retried attempt's new blocks must flow (turn NOT marked cleared).
    cache.update_active_thinking(
        session_id,
        message_id="msg1",
        turn_id="turn1",
        block_id="thk_attempt2_1",
        delta="fresh reasoning",
        title="Thinking",
        complete=False,
    )
    snapshots = cache.active_thinking_snapshots(session_id)
    assert len(snapshots) == 1
    assert [block["block_id"] for block in snapshots[0]["blocks"]] == ["thk_attempt2_1"]


def test_discard_incomplete_active_thinking_is_idempotent() -> None:
    cache = SessionCache(_Guardrails(), max_entries=10)
    cache.discard_incomplete_active_thinking("sess-nonexistent")
    assert cache.active_thinking_snapshots("sess-nonexistent") == []


def test_clear_active_thinking_is_idempotent() -> None:
    """clear_active_thinking on an already-empty session is a no-op."""
    cache = SessionCache(_Guardrails(), max_entries=10)
    # Should not raise even if session has no active thinking
    cache.clear_active_thinking("sess-nonexistent")
    assert cache.active_thinking_snapshots("sess-nonexistent") == []


def test_clear_active_thinking_does_not_affect_other_sessions() -> None:
    """clear_active_thinking only clears the specified session."""
    cache = SessionCache(_Guardrails(), max_entries=10)

    cache.update_active_thinking(
        "sess-a",
        message_id="msg1",
        turn_id="turn1",
        block_id="blk1",
        delta="content a",
        title="Thinking A",
        complete=False,
    )
    cache.update_active_thinking(
        "sess-b",
        message_id="msg2",
        turn_id="turn2",
        block_id="blk2",
        delta="content b",
        title="Thinking B",
        complete=False,
    )

    cache.clear_active_thinking("sess-a")

    assert cache.active_thinking_snapshots("sess-a") == []
    assert cache.active_thinking_snapshots("sess-b") != []


def test_active_thinking_cleared_by_complete_true() -> None:
    """Normal drain path: complete=True removes the block (existing behaviour)."""
    cache = SessionCache(_Guardrails(), max_entries=10)
    session_id = "sess-drain"

    cache.update_active_thinking(
        session_id,
        message_id="msg1",
        turn_id="turn1",
        block_id="blk1",
        delta="content",
        title="Thinking",
        complete=False,
    )
    assert cache.active_thinking_snapshots(session_id) != []

    # Drain with complete=True (normal finalize_thinking path)
    cache.update_active_thinking(
        session_id,
        message_id="msg1",
        turn_id="turn1",
        block_id="blk1",
        delta="",
        title="Thinking",
        complete=True,
    )
    assert cache.active_thinking_snapshots(session_id) == []


def test_active_thinking_snapshots_empty_after_clear_then_new_turn() -> None:
    """After clear, a new turn can populate active_thinking normally."""
    cache = SessionCache(_Guardrails(), max_entries=10)
    session_id = "sess-new-turn"

    # Old turn leaves stale state
    cache.update_active_thinking(
        session_id,
        message_id="old-msg",
        turn_id="old-turn",
        block_id="blk-old",
        delta="stale content",
        title="Old Thinking",
        complete=False,
    )
    cache.clear_active_thinking(session_id)

    # New turn starts
    cache.update_active_thinking(
        session_id,
        message_id="new-msg",
        turn_id="new-turn",
        block_id="blk-new",
        delta="fresh content",
        title="New Thinking",
        complete=False,
    )
    snapshots = cache.active_thinking_snapshots(session_id)
    assert len(snapshots) == 1
    assert snapshots[0]["message_id"] == "new-msg"
    assert snapshots[0]["blocks"][0]["block_id"] == "blk-new"


# ---------------------------------------------------------------------------
# first_block_id anchor stability (the fix for duplicate thinking blocks)
# ---------------------------------------------------------------------------


def test_first_block_id_anchor_set_on_first_block() -> None:
    """The anchor is set to the first block_id ever added to the state."""
    cache = SessionCache(_Guardrails(), max_entries=10)
    session_id = "sess-anchor"

    cache.update_active_thinking(
        session_id,
        message_id="msg1",
        turn_id="turn1",
        block_id="blk_first",
        delta="content",
        title="Thinking",
        complete=False,
    )
    meta = cache.get_active_thinking_metadata(session_id)
    assert meta is not None
    assert meta["first_block_id"] == "blk_first"


def test_first_block_id_anchor_stable_after_second_block_added() -> None:
    """The anchor does not change when a second block is added."""
    cache = SessionCache(_Guardrails(), max_entries=10)
    session_id = "sess-anchor-stable"

    cache.update_active_thinking(
        session_id,
        message_id="msg1",
        turn_id="turn1",
        block_id="blk_first",
        delta="block 1 content",
        title="T",
        complete=False,
    )
    cache.update_active_thinking(
        session_id,
        message_id="msg1",
        turn_id="turn1",
        block_id="blk_second",
        delta="block 2 content",
        title="T",
        complete=False,
    )
    meta = cache.get_active_thinking_metadata(session_id)
    assert meta is not None
    assert meta["first_block_id"] == "blk_first"  # anchor unchanged


def test_first_block_id_anchor_stable_after_first_block_completes_and_is_popped() -> None:
    """THE KEY TEST: anchor stays blk_first even after blk_first is popped.

    This is the exact scenario that caused duplicate thinking blocks:
    - blk_first streams, then completes → popped from state.blocks
    - blk_second becomes blocks[0]
    - Without the anchor, the runtime projector would use blk_second as
      first_block_id → id changes → orphan (stuck) + duplicate.
    - With the anchor, first_block_id stays blk_first throughout.
    """
    cache = SessionCache(_Guardrails(), max_entries=10)
    session_id = "sess-pop-stable"

    # blk_first streams
    cache.update_active_thinking(
        session_id,
        message_id="msg1",
        turn_id="turn1",
        block_id="blk_first",
        delta="block 1",
        title="T",
        complete=False,
    )
    # blk_second starts streaming
    cache.update_active_thinking(
        session_id,
        message_id="msg1",
        turn_id="turn1",
        block_id="blk_second",
        delta="block 2",
        title="T",
        complete=False,
    )
    # blk_first completes → popped from state.blocks
    cache.update_active_thinking(
        session_id,
        message_id="msg1",
        turn_id="turn1",
        block_id="blk_first",
        delta="",
        title="T",
        complete=True,
    )

    # State still exists (not removed when blocks empty — retained for anchor)
    meta = cache.get_active_thinking_metadata(session_id)
    assert meta is not None, "State must be retained after first block pops"
    # Anchor must still be blk_first, not blk_second
    assert meta["first_block_id"] == "blk_first", (
        f"Anchor shifted to {meta['first_block_id']!r} after pop — "
        "this would cause duplicate thinking blocks"
    )
    # Phase must be preserved
    assert meta["assistant_phase_index"] == 0

    # active_thinking_snapshots still returns blk_second (the live block)
    snapshots = cache.active_thinking_snapshots(session_id)
    assert len(snapshots) == 1
    assert snapshots[0]["blocks"][0]["block_id"] == "blk_second"
    # And the snapshot carries the stable anchor
    assert snapshots[0]["first_block_id"] == "blk_first"


def test_first_block_id_anchor_reset_on_new_message_id() -> None:
    """Anchor resets when a new message_id/turn starts (new segment)."""
    cache = SessionCache(_Guardrails(), max_entries=10)
    session_id = "sess-anchor-reset"

    cache.update_active_thinking(
        session_id,
        message_id="msg1",
        turn_id="turn1",
        block_id="blk_old",
        delta="old",
        title="T",
        complete=False,
    )
    # New message_id → state recreated → anchor reset
    cache.update_active_thinking(
        session_id,
        message_id="msg2",
        turn_id="turn2",
        block_id="blk_new",
        delta="new",
        title="T",
        complete=False,
    )
    meta = cache.get_active_thinking_metadata(session_id)
    assert meta is not None
    assert meta["first_block_id"] == "blk_new"
    assert meta["message_id"] == "msg2"


def test_get_active_thinking_metadata_returns_none_when_no_state() -> None:
    """Returns None when no active thinking state exists."""
    cache = SessionCache(_Guardrails(), max_entries=10)
    assert cache.get_active_thinking_metadata("sess-nonexistent") is None


def test_get_active_thinking_metadata_returns_phase_after_all_blocks_popped() -> None:
    """Metadata (phase + anchor) is accessible even after all blocks are popped.

    This is the PATH B fix: the on_thinking finalize handler reads the metadata
    to emit the correct thinking item id even when active_thinking_snapshots
    returns [] (all blocks completed and popped).
    """
    cache = SessionCache(_Guardrails(), max_entries=10)
    session_id = "sess-pathb"

    cache.update_active_thinking(
        session_id,
        message_id="msg1",
        turn_id="turn1",
        block_id="blk_only",
        delta="content",
        title="T",
        complete=False,
        assistant_phase_index=2,
    )
    # Block completes → popped
    cache.update_active_thinking(
        session_id,
        message_id="msg1",
        turn_id="turn1",
        block_id="blk_only",
        delta="",
        title="T",
        complete=True,
        assistant_phase_index=2,
    )

    # active_thinking_snapshots returns [] (no live blocks)
    assert cache.active_thinking_snapshots(session_id) == []

    # But metadata is still accessible for PATH B
    meta = cache.get_active_thinking_metadata(session_id)
    assert meta is not None
    assert meta["assistant_phase_index"] == 2
    assert meta["first_block_id"] == "blk_only"
    assert meta["message_id"] == "msg1"


# ---------------------------------------------------------------------------
# Cancel teardown guards (Issue B)
# ---------------------------------------------------------------------------


def test_update_active_thinking_blocked_after_clear_for_same_turn() -> None:
    """A late thinking delta must not re-create state for a torn-down turn.

    This is the cancel-teardown race: clear_active_thinking runs (turn cancelled),
    then a late thinking delta arrives (from the coalesce buffer). Without the
    guard, update_active_thinking would re-create the state, causing the thinking
    block to stream in after cancel and the runtime snapshot to re-emit it.
    """
    cache = SessionCache(_Guardrails(), max_entries=10)
    session_id = "sess-cancel-guard"

    # Turn starts, thinking streams
    cache.update_active_thinking(
        session_id,
        message_id="msg1",
        turn_id="turn1",
        block_id="blk1",
        delta="thinking...",
        title="T",
        complete=False,
    )
    assert cache.active_thinking_snapshots(session_id) != []

    # Turn cancelled → clear_active_thinking records the turn
    cache.clear_active_thinking(session_id)
    assert cache.active_thinking_snapshots(session_id) == []

    # Late thinking delta arrives after cancel (the coalesce race)
    cache.update_active_thinking(
        session_id,
        message_id="msg1",
        turn_id="turn1",
        block_id="blk1",
        delta="late delta",
        title="T",
        complete=False,
    )
    # Must remain empty — the guard blocked re-creation
    assert cache.active_thinking_snapshots(session_id) == [], (
        "Late thinking delta re-created state after cancel — "
        "this would cause thinking to stream in after cancel"
    )


def test_update_active_thinking_allowed_for_new_turn_after_cancel() -> None:
    """A new turn can start normally after a previous turn was cancelled."""
    cache = SessionCache(_Guardrails(), max_entries=10)
    session_id = "sess-new-turn-after-cancel"

    # Old turn cancelled
    cache.update_active_thinking(
        session_id,
        message_id="msg1",
        turn_id="turn1",
        block_id="blk1",
        delta="old",
        title="T",
        complete=False,
    )
    cache.clear_active_thinking(session_id)

    # New turn starts (different turn_id)
    cache.update_active_thinking(
        session_id,
        message_id="msg2",
        turn_id="turn2",
        block_id="blk2",
        delta="new thinking",
        title="T",
        complete=False,
    )
    snapshots = cache.active_thinking_snapshots(session_id)
    assert len(snapshots) == 1
    assert snapshots[0]["message_id"] == "msg2"


def test_clear_active_thinking_records_cleared_turn_id() -> None:
    """clear_active_thinking records the (session_id, turn_id) for the guard."""
    cache = SessionCache(_Guardrails(), max_entries=10)
    session_id = "sess-record-turn"

    cache.update_active_thinking(
        session_id,
        message_id="msg1",
        turn_id="turn_abc",
        block_id="blk1",
        delta="content",
        title="T",
        complete=False,
    )
    cache.clear_active_thinking(session_id)

    # The cleared turn is recorded
    assert (session_id, "turn_abc") in cache._cleared_thinking_turns
