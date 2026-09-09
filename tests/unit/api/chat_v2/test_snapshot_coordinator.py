from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from cognis.api.chat_v2 import snapshot_coordinator as coordinator
from cognis.api.chat_v2 import work_graph, work_materializer, work_repository
from cognis.api.chat_v2.event_store import RawSessionEvent
from cognis.api.chat_v2.event_store_refs import session_read_refs
from cognis.api.chat_v2.schemas import (
    ConversationStateView,
    ConversationSummary,
    QueueState,
    TimelineScope,
)
from cognis.api.chat_v2.shared_snapshot_cache import (
    SharedChatSnapshotCache,
    SnapshotCacheResult,
    SnapshotRequestTrace,
)
from cognis.api.chat_v2.snapshot_coordinator import ConversationSnapshotContext
from cognis.api.chat_v2.snapshot_warmer import ChatSnapshotWarmer
from cognis.api.chat_v2.sync import ConversationSessionRef, RuntimeOverlayInput
from cognis.bootstrap import run_schema_bootstrap
from cognis.providers.guardrails.events import EventStoreAuthority
from cognis.store.database import create_engine, create_session_factory
from cognis.store.models import Agent, Conversation, Session, User
from tests.unit.api.chat_v2.test_cached_event_store import (
    AUTHORITY,
    Delegate,
    FakeClock,
    FakeRedis,
    build_test_snapshot,
    make_cache,
    make_snapshot_cache,
)


def _context(bound, session_id: str) -> ConversationSnapshotContext:
    conversation_id = "conversation-a"

    async def postprocess(events):
        return events

    return ConversationSnapshotContext(
        scope=TimelineScope(
            key=f"conversation:{conversation_id}",
            kind="conversation",
            conversation_id=conversation_id,
            session_id=session_id,
        ),
        conversation=ConversationSummary(
            conversation_id=conversation_id,
            agent_id="agent-a",
            active_session_id=session_id,
        ),
        session_refs=[
            ConversationSessionRef(
                session_id=session_id,
                event_store_session_id=session_id,
                ordinal=0,
                reader=bound,
                authority_token=bound.authority_token,
            )
        ],
        event_store=None,
        cursor_secret="cursor-secret",
        queue=QueueState(messages=[], queued_count=0),
        state=ConversationStateView(
            state_version=1,
            snapshot_generated_at="2026-01-01T00:00:00+00:00",
        ),
        runtime_input=RuntimeOverlayInput(runtime_epoch="runtime", runtime_revision=1),
        session_cache=None,
        event_post_processor=postprocess,
        owner_email="user@example.com",
        conversation_id=conversation_id,
    )


def _forbid_work_calls(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        work_graph,
        "resolve_authorized_work_graph",
        AsyncMock(side_effect=AssertionError("snapshot resolved Work graph")),
    )
    monkeypatch.setattr(
        work_repository,
        "read_activity_overview",
        AsyncMock(side_effect=AssertionError("snapshot read Work overview")),
    )
    monkeypatch.setattr(
        work_repository,
        "read_work_projection_states",
        AsyncMock(side_effect=AssertionError("snapshot created Work projection state")),
    )
    monkeypatch.setattr(
        work_materializer.WorkMaterializer,
        "prioritize_sessions",
        AsyncMock(side_effect=AssertionError("snapshot prioritized Work materialization")),
    )
    original_session_read_refs = coordinator.session_read_refs

    async def guarded_session_read_refs(*args, role, **kwargs):
        if role == "work":
            raise AssertionError("snapshot resolved Work event-store watermarks")
        return await original_session_read_refs(*args, role=role, **kwargs)

    monkeypatch.setattr(coordinator, "session_read_refs", guarded_session_read_refs)


@pytest.mark.anyio
async def test_snapshot_overlay_always_removes_embedded_activity_overview(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = FakeClock()
    bound = make_cache(Delegate(), FakeRedis(clock), clock).bind(AUTHORITY)
    context = _context(bound, "session-a")
    base = (await build_test_snapshot(bound)).model_copy(
        update={"activity_overview": "stale-overview"}
    )
    monkeypatch.setattr(
        coordinator,
        "_hydrate_snapshot_attachments",
        AsyncMock(side_effect=lambda _app, snapshot, **_kwargs: snapshot),
    )
    app = SimpleNamespace(state=SimpleNamespace())

    snapshot = await coordinator._apply_mutable_snapshot_overlay(  # noqa: SLF001
        app,
        base,
        context,
    )

    assert snapshot.activity_overview is None


@pytest.mark.anyio
async def test_session_read_refs_batches_database_authority_resolution() -> None:
    active_sessions = 0
    session_opens = 0

    class Result:
        def all(self):
            return [("agent-a", "owner-a@example.com"), ("agent-b", "owner-b@example.com")]

    class Session:
        async def execute(self, _statement):
            assert active_sessions == 1
            return Result()

    class SessionContext:
        async def __aenter__(self):
            nonlocal active_sessions, session_opens
            active_sessions += 1
            session_opens += 1
            return Session()

        async def __aexit__(self, *_args):
            nonlocal active_sessions
            active_sessions -= 1

    class Registry:
        get = AsyncMock(side_effect=AssertionError("per-agent lookup used"))

        def get_system_agent(self, _agent_id):
            return None

    class Store:
        def bind(self, authority):
            assert active_sessions == 0
            return SimpleNamespace(authority_token=f"token-{authority.agent_id}")

    rows = [
        SimpleNamespace(
            session_id=f"session-{suffix}",
            intaris_session_id=None,
            user_email="user@example.com",
            agent_id=f"agent-{suffix}",
            status="active",
            completion_reason=None,
        )
        for suffix in ("a", "b")
    ]
    app = SimpleNamespace(
        state=SimpleNamespace(
            session_factory=lambda: SessionContext(),
            agent_registry=Registry(),
            cached_event_store=Store(),
        )
    )

    refs = await session_read_refs(
        app,
        rows,
        user_email="user@example.com",
        role="work",
    )

    assert session_opens == 1
    assert active_sessions == 0
    assert [ref.ordinal for ref in refs] == [0, 1]


@pytest.mark.anyio
async def test_cold_snapshot_performs_no_work_queries_or_computation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = FakeClock()
    bound = make_cache(Delegate(), FakeRedis(clock), clock).bind(AUTHORITY)
    context = _context(bound, "session-a")

    monkeypatch.setattr(
        coordinator,
        "load_conversation_snapshot_context",
        AsyncMock(return_value=context),
    )
    _forbid_work_calls(monkeypatch)
    monkeypatch.setattr(
        coordinator,
        "_hydrate_snapshot_attachments",
        AsyncMock(side_effect=lambda _app, snapshot, **_kwargs: snapshot),
    )
    app = SimpleNamespace(
        state=SimpleNamespace(
            shared_chat_snapshot_cache=None,
            cached_event_store=bound._cache,
        )
    )

    snapshot = await coordinator.build_chat_snapshot_coordinated(app, context)

    assert len(snapshot.timeline.items) == 1
    assert snapshot.activity_overview is None


@pytest.mark.anyio
async def test_context_loading_and_cold_build_perform_no_work_calls(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path / 'snapshot-context.db'}")
    session_factory = create_session_factory(engine)
    await run_schema_bootstrap(engine)
    async with session_factory() as session:
        session.add(User(email="user@example.com", name="User", role="user"))
        await session.flush()
        session.add(Agent(agent_id="agent-a", owner_email="user@example.com", name="Agent"))
        await session.flush()
        session.add(
            Conversation(
                conversation_id="conversation-a",
                user_email="user@example.com",
                agent_id="agent-a",
                context_type="web",
                title_source="unset",
                active_session_id="session-a",
            )
        )
        await session.flush()
        session.add(
            Session(
                session_id="session-a",
                conversation_id="conversation-a",
                user_email="user@example.com",
                agent_id="agent-a",
                intaris_session_id="session-a",
                delegation_metadata={},
            )
        )
        await session.commit()

    clock = FakeClock()
    events = make_cache(Delegate(), FakeRedis(clock), clock)
    _forbid_work_calls(monkeypatch)
    monkeypatch.setattr(
        coordinator,
        "_hydrate_snapshot_attachments",
        AsyncMock(side_effect=lambda _app, snapshot, **_kwargs: snapshot),
    )
    app = SimpleNamespace(
        state=SimpleNamespace(
            session_factory=session_factory,
            agent_registry=SimpleNamespace(get_system_agent=lambda _agent_id: None),
            cached_event_store=events,
            shared_chat_snapshot_cache=None,
            chat_v2_cursor_secret="cursor-secret",
            turn_scheduler=None,
            session_cache=None,
            artifact_store=None,
        )
    )
    try:
        context = await coordinator.load_conversation_snapshot_context(
            app,
            user_email="user@example.com",
            conversation_id="conversation-a",
        )
        snapshot = await coordinator.build_chat_snapshot_coordinated(app, context)
    finally:
        await engine.dispose()

    assert snapshot.activity_overview is None


@pytest.mark.anyio
async def test_cache_hit_uses_one_context_load_and_rehydrates_attachments(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = FakeClock()
    bound = make_cache(Delegate(), FakeRedis(clock), clock).bind(AUTHORITY)
    context = _context(bound, "session-a")
    base = await build_test_snapshot(bound)
    hydrated = 0
    trace = SnapshotRequestTrace()
    _forbid_work_calls(monkeypatch)

    class Cache:
        async def get_or_build_result(self, *, request_trace, **_kwargs):
            request_trace.select("l1")
            return SnapshotCacheResult(base, "l1")

    async def unexpected_reload(*_args, **_kwargs):
        raise AssertionError("cache hit must not reload conversation context")

    async def hydrate(_app, snapshot, **_kwargs):
        nonlocal hydrated
        hydrated += 1
        return snapshot

    monkeypatch.setattr(coordinator, "load_conversation_snapshot_context", unexpected_reload)
    monkeypatch.setattr(coordinator, "_hydrate_snapshot_attachments", hydrate)
    app = SimpleNamespace(
        state=SimpleNamespace(
            shared_chat_snapshot_cache=Cache(),
            cached_event_store=bound._cache,
        )
    )

    await coordinator.build_chat_snapshot_coordinated(app, context, request_trace=trace)

    assert hydrated == 1
    assert trace.tier == "l1"


@pytest.mark.anyio
@pytest.mark.parametrize("entry_state", ["hit", "incompatible"])
async def test_shared_cache_paths_perform_no_work_calls(
    monkeypatch: pytest.MonkeyPatch,
    entry_state: str,
) -> None:
    clock = FakeClock()
    redis = FakeRedis(clock)
    events = make_cache(Delegate(), redis, clock)
    bound = events.bind(AUTHORITY)
    cache = make_snapshot_cache(events, redis, clock)
    context = _context(bound, "session-a")
    app = SimpleNamespace(
        state=SimpleNamespace(
            shared_chat_snapshot_cache=cache,
            cached_event_store=events,
        )
    )
    _forbid_work_calls(monkeypatch)
    monkeypatch.setattr(
        coordinator,
        "load_conversation_snapshot_context",
        AsyncMock(return_value=context),
    )
    monkeypatch.setattr(
        coordinator,
        "_hydrate_snapshot_attachments",
        AsyncMock(side_effect=lambda _app, snapshot, **_kwargs: snapshot),
    )

    warm_outcome, _failure = await coordinator.warm_chat_snapshot_coordinated(app, context)
    assert warm_outcome == "succeeded"
    identity = await cache._identity(  # noqa: SLF001
        authority_token=coordinator._conversation_authority_token(app, context),  # noqa: SLF001
        scope_key=context.scope.key,
        session_refs=context.session_refs,
    )
    assert identity is not None
    if entry_state == "incompatible":
        cache._l1.clear()  # noqa: SLF001
        redis.values[identity.value_key] = (b"incompatible", clock() + 60)

    snapshot = await coordinator.build_chat_snapshot_coordinated(app, context)

    assert snapshot.activity_overview is None
    if entry_state == "incompatible":
        assert identity.value_key in redis.deleted


@pytest.mark.anyio
async def test_cache_only_hit_applies_fresh_mutable_overlay(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = FakeClock()
    bound = make_cache(Delegate(), FakeRedis(clock), clock).bind(AUTHORITY)
    context = _context(bound, "session-a")
    base = (await build_test_snapshot(bound)).model_copy(
        update={"activity_overview": SimpleNamespace()}
    )
    _forbid_work_calls(monkeypatch)

    class Cache:
        async def get_cached_result(self, **_kwargs):
            return SimpleNamespace(snapshot=base, status="hit_redis")

    async def hydrate(_app, snapshot, **_kwargs):
        return snapshot

    monkeypatch.setattr(coordinator, "_hydrate_snapshot_attachments", hydrate)
    app = SimpleNamespace(
        state=SimpleNamespace(
            shared_chat_snapshot_cache=Cache(),
            cached_event_store=bound._cache,
        )
    )

    snapshot, outcome = await coordinator.get_cached_chat_snapshot_coordinated(app, context)

    assert outcome == "hit_redis"
    assert snapshot is not None
    assert snapshot.queue == context.queue
    assert snapshot.state == context.state
    assert snapshot.runtime.runtime_revision == 1
    assert snapshot.activity_overview is None


@pytest.mark.anyio
async def test_cache_only_hit_accepts_snapshot_without_activity_overview() -> None:
    clock = FakeClock()
    bound = make_cache(Delegate(), FakeRedis(clock), clock).bind(AUTHORITY)
    context = _context(bound, "session-a")
    base = await build_test_snapshot(bound)

    class Cache:
        async def get_cached_result(self, **_kwargs):
            return SimpleNamespace(snapshot=base, status="hit_redis")

    app = SimpleNamespace(
        state=SimpleNamespace(
            shared_chat_snapshot_cache=Cache(),
            cached_event_store=bound._cache,
        )
    )

    snapshot, outcome = await coordinator.get_cached_chat_snapshot_coordinated(app, context)

    assert snapshot is not None
    assert snapshot.activity_overview is None
    assert outcome == "hit_redis"


@pytest.mark.anyio
async def test_build_failure_preserves_selected_tier() -> None:
    clock = FakeClock()
    bound = make_cache(Delegate(), FakeRedis(clock), clock).bind(AUTHORITY)
    trace = SnapshotRequestTrace()

    class Cache:
        async def get_or_build_result(self, *, request_trace, **_kwargs):
            request_trace.select("build")
            raise RuntimeError("projection failed")

    app = SimpleNamespace(
        state=SimpleNamespace(shared_chat_snapshot_cache=Cache(), cached_event_store=bound._cache)
    )

    with pytest.raises(RuntimeError, match="projection failed"):
        await coordinator.build_chat_snapshot_coordinated(
            app,
            _context(bound, "session-a"),
            request_trace=trace,
        )

    assert trace.tier == "build"


@pytest.mark.anyio
async def test_lineage_change_during_build_restarts_with_fresh_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = FakeClock()
    bound = make_cache(Delegate(), FakeRedis(clock), clock).bind(AUTHORITY)
    initial = _context(bound, "session-a")
    fresh = _context(bound, "session-b")
    base = await build_test_snapshot(bound)
    builds: list[str] = []
    hydrated_session: list[str] = []

    class Cache:
        async def get_or_build_result(self, *, build, **_kwargs):
            return SnapshotCacheResult(await build(), "build")

    async def build_immutable(context):
        builds.append(context.session_refs[0].session_id)
        return base

    async def reload(*_args, **_kwargs):
        return fresh

    async def hydrate(_app, snapshot, *, session_refs, **_kwargs):
        hydrated_session.append(session_refs[0].session_id)
        return snapshot

    monkeypatch.setattr(coordinator, "_build_immutable_snapshot", build_immutable)
    monkeypatch.setattr(coordinator, "load_conversation_snapshot_context", reload)
    monkeypatch.setattr(coordinator, "_hydrate_snapshot_attachments", hydrate)
    app = SimpleNamespace(
        state=SimpleNamespace(
            shared_chat_snapshot_cache=Cache(),
            cached_event_store=bound._cache,
        )
    )

    await coordinator.build_chat_snapshot_coordinated(app, initial)

    assert builds == ["session-a", "session-b"]
    assert hydrated_session == ["session-b"]


@pytest.mark.anyio
async def test_snapshot_warmer_stores_null_activity_overview(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = FakeClock()
    bound = make_cache(Delegate(), FakeRedis(clock), clock).bind(AUTHORITY)
    context = _context(bound, "session-a")
    stored = None

    class Cache:
        warming_configured = True

        async def get_or_build_result(self, *, build, **_kwargs):
            nonlocal stored
            stored = await build()
            return SimpleNamespace(warm_failure=None)

        def warm_outcome(self, _scope_key):
            return "succeeded"

    _forbid_work_calls(monkeypatch)
    app = SimpleNamespace(
        state=SimpleNamespace(
            shared_chat_snapshot_cache=Cache(),
            cached_event_store=bound._cache,
        )
    )

    outcome, failure = await coordinator.warm_chat_snapshot_coordinated(app, context)

    assert outcome == "succeeded"
    assert failure is None
    assert stored is not None
    assert stored.activity_overview is None


@pytest.mark.anyio
async def test_non_admitted_warm_is_skipped_without_requeue(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = FakeClock()
    redis = FakeRedis(clock)
    events = make_cache(Delegate(), redis, clock)
    bound = events.bind(AUTHORITY)
    cache = SharedChatSnapshotCache(
        event_store=events,
        redis_service=redis,
        policy=events._policy,
        clock=clock,
    )
    context = _context(bound, "session-a")
    calls = 0

    async def reject_encoding(**_kwargs):
        return None

    monkeypatch.setattr(cache, "_serialize", reject_encoding)
    app = SimpleNamespace(
        state=SimpleNamespace(
            shared_chat_snapshot_cache=cache,
            cached_event_store=events,
        )
    )

    async def warm(_conversation_id: str):
        nonlocal calls
        calls += 1
        return await coordinator.warm_chat_snapshot_coordinated(app, context)

    warmer = ChatSnapshotWarmer(warm, worker_count=1, retry_seconds=0.01)
    await warmer.start()
    warmer.enqueue("conversation-a")
    for _ in range(100):
        if calls:
            break
        await asyncio.sleep(0.01)
    await asyncio.sleep(0.05)
    await warmer.stop()

    assert calls == 1
    assert cache.warm_outcome(context.scope.key) == "skipped"
    await cache.aclose()


@pytest.mark.anyio
async def test_attachment_post_processor_refreshes_each_sync_and_backfill_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    revisions = iter(("url-v1", "url-v2"))

    async def hydrate(_session, _store, attachments, **_kwargs):
        return [{**attachments[0], "url": next(revisions)}]

    monkeypatch.setattr(coordinator, "hydrate_attachment_refs", hydrate)

    class SessionContext:
        async def __aenter__(self):
            return object()

        async def __aexit__(self, *_args):
            return None

    app = SimpleNamespace(
        state=SimpleNamespace(
            session_factory=lambda: SessionContext(),
            artifact_store=object(),
        )
    )
    processor = coordinator.event_attachment_post_processor(
        app,
        owner_email="user@example.com",
        conversation_id="conversation-a",
    )
    event = RawSessionEvent(
        store_id="intaris",
        session_id="event-session",
        seq=1,
        type="assistant_message",
        data={
            "cognis_session_id": "session-a",
            "attachments": [{"artifact_id": "art-a"}],
        },
    )

    first = await processor([event])
    second = await processor([event])

    assert first[0].data["attachments"][0]["url"] == "url-v1"
    assert second[0].data["attachments"][0]["url"] == "url-v2"
    assert event.data["attachments"] == [{"artifact_id": "art-a"}]


def test_conversation_authority_is_stable_across_session_agent_lineage() -> None:
    clock = FakeClock()
    store = make_cache(Delegate(), FakeRedis(clock), clock)
    first = store.bind(AUTHORITY)
    second = store.bind(
        EventStoreAuthority(
            user_email=AUTHORITY.user_email,
            agent_id="agent-b",
            agent_owner_email=AUTHORITY.agent_owner_email,
        )
    )
    first_context = _context(first, "session-a")
    second_context = _context(second, "session-b")
    app = SimpleNamespace(state=SimpleNamespace(cached_event_store=store))

    assert coordinator._conversation_authority_token(
        app, first_context
    ) == coordinator._conversation_authority_token(app, second_context)
