from __future__ import annotations

import asyncio
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from cognis.api.chat_v2 import snapshot_coordinator as coordinator
from cognis.api.chat_v2.event_store import RawSessionEvent
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
from cognis.api.chat_v2.work_graph import AuthorizedWorkRootNotReadyError
from cognis.providers.guardrails.events import EventStoreAuthority
from tests.unit.api.chat_v2.test_cached_event_store import (
    AUTHORITY,
    Delegate,
    FakeClock,
    FakeRedis,
    build_test_snapshot,
    make_cache,
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


@pytest.mark.anyio
async def test_snapshot_overlay_atomically_replaces_cached_activity_overview(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = FakeClock()
    bound = make_cache(Delegate(), FakeRedis(clock), clock).bind(AUTHORITY)
    context = _context(bound, "session-a")
    base = (await build_test_snapshot(bound)).model_copy(
        update={"activity_overview": "stale-overview"}
    )
    fresh_overview = SimpleNamespace(overview_revision="revision-fresh")
    monkeypatch.setattr(
        coordinator,
        "_read_snapshot_activity_overview",
        AsyncMock(return_value=fresh_overview),
    )
    monkeypatch.setattr(
        coordinator,
        "_hydrate_snapshot_attachments",
        AsyncMock(side_effect=lambda _app, snapshot, **_kwargs: snapshot),
    )
    app = SimpleNamespace(state=SimpleNamespace(session_factory=object()))

    snapshot = await coordinator._apply_mutable_snapshot_overlay(  # noqa: SLF001
        app,
        base,
        context,
    )

    assert snapshot.activity_overview is fresh_overview


@pytest.mark.anyio
async def test_snapshot_overlay_keeps_cached_overview_when_refresh_times_out(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = FakeClock()
    bound = make_cache(Delegate(), FakeRedis(clock), clock).bind(AUTHORITY)
    context = _context(bound, "session-a")
    stale = SimpleNamespace(overview_revision="revision-stale")
    base = (await build_test_snapshot(bound)).model_copy(update={"activity_overview": stale})
    monkeypatch.setattr(
        coordinator,
        "_read_snapshot_activity_overview",
        AsyncMock(side_effect=TimeoutError),
    )
    monkeypatch.setattr(
        coordinator,
        "_hydrate_snapshot_attachments",
        AsyncMock(side_effect=lambda _app, snapshot, **_kwargs: snapshot),
    )
    app = SimpleNamespace(state=SimpleNamespace(session_factory=object()))

    snapshot = await coordinator._apply_mutable_snapshot_overlay(  # noqa: SLF001
        app,
        base,
        context,
    )

    assert snapshot.activity_overview is stale


@pytest.mark.anyio
async def test_snapshot_overview_uses_same_service_with_bounded_graph_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = FakeClock()
    bound = make_cache(Delegate(), FakeRedis(clock), clock).bind(AUTHORITY)
    context = _context(bound, "session-a")
    expected = SimpleNamespace(overview_revision="same-service")
    deadlines: list[float] = []

    class SessionFactory:
        async def __aenter__(self):
            return object()

        async def __aexit__(self, *_args):
            return None

    async def resolve_graph(_db, *, deadline, **_kwargs):
        deadlines.append(deadline)
        return SimpleNamespace(
            session_rows=[],
            nodes=[],
            fingerprint="graph",
            truncated=False,
        )

    read_overview = AsyncMock(return_value=expected)
    monkeypatch.setattr(coordinator, "resolve_authorized_work_graph", resolve_graph)
    monkeypatch.setattr(coordinator, "read_activity_overview", read_overview)
    app = SimpleNamespace(
        state=SimpleNamespace(
            session_factory=lambda: SessionFactory(),
            tool_registry=None,
        )
    )

    actual = await coordinator._read_snapshot_activity_overview(app, context)  # noqa: SLF001

    assert actual is expected
    assert deadlines
    assert read_overview.await_args.kwargs["scope"] == context.scope
    assert read_overview.await_args.kwargs["graph_fingerprint"] == "graph"
    assert read_overview.await_args.kwargs["detail"] == "lightweight"


@pytest.mark.anyio
async def test_cold_snapshot_succeeds_before_authorized_work_root_is_visible(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = FakeClock()
    bound = make_cache(Delegate(), FakeRedis(clock), clock).bind(AUTHORITY)
    context = _context(bound, "session-a")
    base = await build_test_snapshot(bound)

    class SessionFactory:
        async def __aenter__(self):
            return object()

        async def __aexit__(self, *_args):
            return None

    monkeypatch.setattr(coordinator, "_build_immutable_snapshot", AsyncMock(return_value=base))
    monkeypatch.setattr(
        coordinator,
        "load_conversation_snapshot_context",
        AsyncMock(return_value=context),
    )
    monkeypatch.setattr(
        coordinator,
        "resolve_authorized_work_graph",
        AsyncMock(
            side_effect=AuthorizedWorkRootNotReadyError(
                "Authorized Work conversation root was not found"
            )
        ),
    )
    monkeypatch.setattr(
        coordinator,
        "_hydrate_snapshot_attachments",
        AsyncMock(side_effect=lambda _app, snapshot, **_kwargs: snapshot),
    )
    app = SimpleNamespace(
        state=SimpleNamespace(
            shared_chat_snapshot_cache=None,
            cached_event_store=bound._cache,
            session_factory=lambda: SessionFactory(),
            tool_registry=None,
        )
    )

    snapshot = await coordinator.build_chat_snapshot_coordinated(app, context)

    assert snapshot.timeline == base.timeline
    assert snapshot.activity_overview is None


@pytest.mark.anyio
async def test_cold_snapshot_succeeds_before_conversation_has_an_active_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = FakeClock()
    bound = make_cache(Delegate(), FakeRedis(clock), clock).bind(AUTHORITY)
    initial = _context(bound, "session-a")
    context = replace(
        initial,
        scope=initial.scope.model_copy(update={"session_id": None}),
        conversation=initial.conversation.model_copy(update={"active_session_id": None}),
        session_refs=[],
    )
    base = await build_test_snapshot(bound)

    class SessionFactory:
        async def __aenter__(self):
            return object()

        async def __aexit__(self, *_args):
            return None

    monkeypatch.setattr(coordinator, "_build_immutable_snapshot", AsyncMock(return_value=base))
    monkeypatch.setattr(
        coordinator,
        "load_conversation_snapshot_context",
        AsyncMock(return_value=context),
    )
    monkeypatch.setattr(
        coordinator,
        "resolve_authorized_work_graph",
        AsyncMock(
            side_effect=AuthorizedWorkRootNotReadyError(
                "Authorized Work conversation root was not found"
            )
        ),
    )
    monkeypatch.setattr(
        coordinator,
        "_hydrate_snapshot_attachments",
        AsyncMock(side_effect=lambda _app, snapshot, **_kwargs: snapshot),
    )
    app = SimpleNamespace(
        state=SimpleNamespace(
            shared_chat_snapshot_cache=None,
            cached_event_store=bound._cache,
            session_factory=lambda: SessionFactory(),
            tool_registry=None,
        )
    )

    snapshot = await coordinator.build_chat_snapshot_coordinated(app, context)

    assert snapshot.timeline == base.timeline
    assert snapshot.activity_overview is None


@pytest.mark.anyio
async def test_snapshot_overview_recovers_when_first_activity_becomes_visible(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = FakeClock()
    bound = make_cache(Delegate(), FakeRedis(clock), clock).bind(AUTHORITY)
    context = _context(bound, "session-a")
    expected = SimpleNamespace(overview_revision="first-activity")
    read_overview = AsyncMock(
        side_effect=[
            AuthorizedWorkRootNotReadyError("Authorized Work active root session was not found"),
            expected,
        ]
    )
    monkeypatch.setattr(coordinator, "_read_snapshot_activity_overview", read_overview)
    app = SimpleNamespace(state=SimpleNamespace())

    first = await coordinator._read_snapshot_activity_overview_bounded(  # noqa: SLF001
        app,
        context,
        stale=SimpleNamespace(overview_revision="stale"),
    )
    second = await coordinator._read_snapshot_activity_overview_bounded(  # noqa: SLF001
        app,
        context,
    )

    assert first is None
    assert second is expected


@pytest.mark.anyio
async def test_snapshot_overview_does_not_mask_other_graph_failures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = FakeClock()
    bound = make_cache(Delegate(), FakeRedis(clock), clock).bind(AUTHORITY)
    context = _context(bound, "session-a")
    monkeypatch.setattr(
        coordinator,
        "_read_snapshot_activity_overview",
        AsyncMock(side_effect=ValueError("Authorized Work graph root session was not resolved")),
    )

    with pytest.raises(ValueError, match="graph root session"):
        await coordinator._read_snapshot_activity_overview_bounded(  # noqa: SLF001
            SimpleNamespace(state=SimpleNamespace()),
            context,
        )


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
async def test_cache_only_hit_applies_fresh_mutable_overlay(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = FakeClock()
    bound = make_cache(Delegate(), FakeRedis(clock), clock).bind(AUTHORITY)
    context = _context(bound, "session-a")
    base = (await build_test_snapshot(bound)).model_copy(
        update={"activity_overview": SimpleNamespace()}
    )

    class Cache:
        async def get_cached_result(self, **_kwargs):
            return SimpleNamespace(snapshot=base, status="hit_redis")

    async def hydrate(_app, snapshot, **_kwargs):
        return snapshot

    monkeypatch.setattr(coordinator, "_hydrate_snapshot_attachments", hydrate)
    monkeypatch.setattr(
        coordinator,
        "_read_snapshot_activity_overview_bounded",
        AsyncMock(side_effect=AssertionError("cache-only hit refreshed Work")),
    )
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


@pytest.mark.anyio
async def test_cache_only_hit_without_warmed_overview_returns_miss() -> None:
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

    assert snapshot is None
    assert outcome == "miss"


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
@pytest.mark.parametrize("timeout_stage", ["graph", "repository"])
async def test_cold_snapshot_overview_timeout_returns_canonical_snapshot(
    monkeypatch: pytest.MonkeyPatch,
    timeout_stage: str,
) -> None:
    clock = FakeClock()
    bound = make_cache(Delegate(), FakeRedis(clock), clock).bind(AUTHORITY)
    context = _context(bound, "session-a")
    base = await build_test_snapshot(bound)

    class SessionFactory:
        async def __aenter__(self):
            return object()

        async def __aexit__(self, *_args):
            return None

    async def resolve_graph(*_args, **_kwargs):
        if timeout_stage == "graph":
            raise TimeoutError
        return SimpleNamespace(
            session_rows=[],
            nodes=[],
            fingerprint="graph",
            truncated=False,
        )

    async def read_overview(*_args, **_kwargs):
        raise TimeoutError

    monkeypatch.setattr(coordinator, "_build_immutable_snapshot", AsyncMock(return_value=base))
    monkeypatch.setattr(
        coordinator,
        "load_conversation_snapshot_context",
        AsyncMock(return_value=context),
    )
    monkeypatch.setattr(coordinator, "resolve_authorized_work_graph", resolve_graph)
    monkeypatch.setattr(coordinator, "read_activity_overview", read_overview)
    monkeypatch.setattr(
        coordinator,
        "_hydrate_snapshot_attachments",
        AsyncMock(side_effect=lambda _app, snapshot, **_kwargs: snapshot),
    )
    app = SimpleNamespace(
        state=SimpleNamespace(
            shared_chat_snapshot_cache=None,
            cached_event_store=bound._cache,
            session_factory=lambda: SessionFactory(),
            tool_registry=None,
        )
    )

    snapshot = await coordinator.build_chat_snapshot_coordinated(app, context)

    assert snapshot.timeline == base.timeline
    assert snapshot.activity_overview is None


@pytest.mark.anyio
async def test_second_lineage_change_rebuilds_overview_for_final_lineage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = FakeClock()
    bound = make_cache(Delegate(), FakeRedis(clock), clock).bind(AUTHORITY)
    contexts = [
        _context(bound, "session-a"),
        _context(bound, "session-b"),
        _context(bound, "session-c"),
    ]
    base = await build_test_snapshot(bound)
    builds: list[str] = []
    overview_reads: list[str] = []
    reloads = iter(contexts[1:])

    class Cache:
        async def get_or_build_result(self, *, build, **_kwargs):
            return SnapshotCacheResult(await build(), "build")

    async def build_immutable(context):
        builds.append(context.session_refs[0].session_id)
        return base

    async def read_overview(_app, context, **_kwargs):
        session_id = context.session_refs[0].session_id
        overview_reads.append(session_id)
        return session_id

    monkeypatch.setattr(coordinator, "_build_immutable_snapshot", build_immutable)
    monkeypatch.setattr(
        coordinator,
        "load_conversation_snapshot_context",
        AsyncMock(side_effect=lambda *_args, **_kwargs: next(reloads)),
    )
    monkeypatch.setattr(
        coordinator,
        "_read_snapshot_activity_overview_bounded",
        read_overview,
    )
    monkeypatch.setattr(
        coordinator,
        "_hydrate_snapshot_attachments",
        AsyncMock(side_effect=lambda _app, snapshot, **_kwargs: snapshot),
    )
    app = SimpleNamespace(
        state=SimpleNamespace(
            shared_chat_snapshot_cache=Cache(),
            cached_event_store=bound._cache,
            session_factory=object(),
        )
    )

    snapshot = await coordinator.build_chat_snapshot_coordinated(app, contexts[0])

    assert builds == ["session-a", "session-b", "session-c"]
    assert overview_reads == ["session-a", "session-b", "session-c"]
    assert snapshot.activity_overview == "session-c"


@pytest.mark.anyio
@pytest.mark.parametrize(
    "overview_error",
    [
        TimeoutError(),
        AuthorizedWorkRootNotReadyError("Authorized Work conversation root was not found"),
    ],
    ids=["timeout", "root-not-ready"],
)
async def test_snapshot_warmer_overview_failure_stores_canonical_snapshot(
    monkeypatch: pytest.MonkeyPatch,
    overview_error: Exception,
) -> None:
    clock = FakeClock()
    bound = make_cache(Delegate(), FakeRedis(clock), clock).bind(AUTHORITY)
    context = _context(bound, "session-a")
    base = await build_test_snapshot(bound)
    stored = None

    class Cache:
        warming_configured = True

        async def get_or_build_result(self, *, build, **_kwargs):
            nonlocal stored
            stored = await build()
            return SimpleNamespace(warm_failure=None)

        def warm_outcome(self, _scope_key):
            return "succeeded"

    monkeypatch.setattr(coordinator, "_build_immutable_snapshot", AsyncMock(return_value=base))
    monkeypatch.setattr(
        coordinator,
        "_read_snapshot_activity_overview",
        AsyncMock(side_effect=overview_error),
    )
    app = SimpleNamespace(
        state=SimpleNamespace(
            shared_chat_snapshot_cache=Cache(),
            cached_event_store=bound._cache,
            session_factory=object(),
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
