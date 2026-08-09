from __future__ import annotations

import asyncio
import os
from time import monotonic
from typing import Any, cast
from uuid import uuid4

import pytest

from cognis.api.chat_v2.cached_event_store import EventCachePolicy
from cognis.api.chat_v2.shared_snapshot_cache import SharedChatSnapshotCache
from cognis.api.chat_v2.snapshot_warmer import ChatSnapshotActiveReconciler
from cognis.api.chat_v2.sync import ConversationSessionRef, clear_chat_v2_read_caches
from cognis.core.redis_service import RedisService
from cognis.providers.guardrails.events import EventAppendNotification, EventStoreAuthority
from tests.unit.api.chat_v2.test_cached_event_store import (
    AUTHORITY,
    Delegate,
    build_test_snapshot,
    make_cache,
)

pytestmark = pytest.mark.skipif(
    not os.getenv("COGNIS_TEST_REDIS_URL"),
    reason="COGNIS_TEST_REDIS_URL is required for real Redis qualification",
)


def _snapshot_cache(
    event_store,
    redis: RedisService,
    policy: EventCachePolicy,
    *,
    lease: int = 15,
    deadline: float = 120,
) -> SharedChatSnapshotCache:
    return SharedChatSnapshotCache(
        event_store=event_store,
        redis_service=redis,
        policy=policy,
        clock=monotonic,
        lock_lease_seconds=lease,
        build_deadline_seconds=deadline,
    )


def _refs(bound) -> list[ConversationSessionRef]:
    return [
        ConversationSessionRef(
            session_id="session-a",
            event_store_session_id="session-a",
            ordinal=0,
            reader=bound,
            authority_token=bound.authority_token,
        )
    ]


@pytest.mark.anyio
async def test_real_redis_cross_controller_contention_sliding_expiry_and_outage() -> None:
    clear_chat_v2_read_caches()
    url = os.environ["COGNIS_TEST_REDIS_URL"]
    redis = RedisService(url, operation_timeout_seconds=0.2)
    assert await redis.start()
    await redis._client.flushdb()  # noqa: SLF001 - isolated test database
    policy = EventCachePolicy(ttl_seconds=2, sliding_expiration=True)
    delegate = Delegate()
    stores = [
        make_cache(delegate, cast(Any, redis), cast(Any, monotonic), policy=policy)
        for _ in range(8)
    ]
    bounds = [store.bind(AUTHORITY) for store in stores]
    scope_key = f"conversation:{uuid4().hex}"
    builds = 0

    async def build():
        nonlocal builds
        builds += 1
        await asyncio.sleep(1.3)
        return await build_test_snapshot(bounds[0], scope_key=scope_key)

    snapshots = [_snapshot_cache(store, redis, policy, lease=1, deadline=4) for store in stores]
    await asyncio.gather(
        *[
            cache.get_or_build(
                authority_token=bound.authority_token,
                scope_key=scope_key,
                session_refs=_refs(bound),
                cursor_secret="cursor-secret",
                build=build,
            )
            for cache, bound in zip(snapshots, bounds, strict=True)
        ]
    )
    assert builds == 1
    assert delegate.page_calls == 1
    reads_before_hit = (delegate.page_calls, delegate.watermark_calls)
    hit_started = monotonic()
    await snapshots[-1].get_or_build(
        authority_token=bounds[-1].authority_token,
        scope_key=scope_key,
        session_refs=_refs(bounds[-1]),
        cursor_secret="cursor-secret",
        build=build,
    )
    assert monotonic() - hit_started < 0.2
    assert (delegate.page_calls, delegate.watermark_calls) == reads_before_hit

    identity = await snapshots[0]._identity(  # noqa: SLF001 - integration evidence
        authority_token=bounds[0].authority_token,
        scope_key=scope_key,
        session_refs=_refs(bounds[0]),
    )
    assert identity is not None
    await asyncio.sleep(1.1)
    await snapshots[1].get_or_build(
        authority_token=bounds[1].authority_token,
        scope_key=scope_key,
        session_refs=_refs(bounds[1]),
        cursor_secret="cursor-secret",
        build=build,
    )
    assert await redis._client.ttl(identity.value_key) >= 1  # noqa: SLF001

    dead = RedisService(
        "redis://127.0.0.1:1/15",
        connect_timeout_seconds=0.05,
        operation_timeout_seconds=0.05,
    )
    assert not await dead.start()
    dead_store = make_cache(Delegate(), cast(Any, dead), cast(Any, monotonic))
    dead_bound = dead_store.bind(AUTHORITY)
    started = monotonic()
    await _snapshot_cache(dead_store, dead, EventCachePolicy()).get_or_build(
        authority_token=dead_bound.authority_token,
        scope_key=f"conversation:{uuid4().hex}",
        session_refs=_refs(dead_bound),
        cursor_secret="cursor-secret",
        build=lambda: build_test_snapshot(dead_bound),
    )
    assert monotonic() - started < 0.2
    await dead.aclose()
    await redis.aclose()


@pytest.mark.anyio
async def test_real_redis_abandoned_lock_recovers_after_lease_expiry() -> None:
    url = os.environ["COGNIS_TEST_REDIS_URL"]
    redis = RedisService(url, operation_timeout_seconds=0.2)
    assert await redis.start()
    policy = EventCachePolicy()
    delegate = Delegate()
    store = make_cache(delegate, cast(Any, redis), cast(Any, monotonic), policy=policy)
    bound = store.bind(AUTHORITY)
    cache = _snapshot_cache(store, redis, policy, lease=1, deadline=4)
    scope_key = f"conversation:{uuid4().hex}"
    identity = await cache._identity(  # noqa: SLF001 - integration evidence
        authority_token=bound.authority_token,
        scope_key=scope_key,
        session_refs=_refs(bound),
    )
    assert identity is not None
    assert await redis.set_if_absent(identity.lock_key, b"abandoned", ttl_seconds=1)
    started = monotonic()
    await cache.get_or_build(
        authority_token=bound.authority_token,
        scope_key=scope_key,
        session_refs=_refs(bound),
        cursor_secret="cursor-secret",
        build=lambda: build_test_snapshot(bound, scope_key=scope_key),
    )
    assert 0.8 <= monotonic() - started < 2.0
    assert delegate.page_calls == 1
    await redis.aclose()


@pytest.mark.anyio
async def test_real_redis_append_churn_is_constant_and_quiet_active_stays_warm() -> None:
    url = os.environ["COGNIS_TEST_REDIS_URL"]
    redis = RedisService(url, operation_timeout_seconds=0.2)
    assert await redis.start()
    await redis._client.flushdb()  # noqa: SLF001 - isolated test database
    policy = EventCachePolicy(ttl_seconds=2, sliding_expiration=True)
    delegate = Delegate()
    store = make_cache(delegate, cast(Any, redis), cast(Any, monotonic), policy=policy)
    bound = store.bind(AUTHORITY)
    cache = _snapshot_cache(store, redis, policy)
    scope_key = f"conversation:{uuid4().hex}"
    refs = _refs(bound)
    identity_keys: set[str] = set()
    for seq in range(1, 10_001):
        await store.handle_append(EventAppendNotification(AUTHORITY, "session-a", seq, seq, 1))
        identity = await cache._identity(  # noqa: SLF001 - cardinality evidence
            authority_token=bound.authority_token,
            scope_key=scope_key,
            session_refs=refs,
        )
        assert identity is not None
        identity_keys.add(identity.value_key)
        assert await store.generation_fenced_write(
            identity.value_key,
            b"bounded",
            identity.fence,
            ttl_seconds=policy.ttl_seconds,
        )
    assert len(identity_keys) == 1
    snapshot_keys = await redis._client.keys(  # noqa: SLF001 - integration evidence
        "cognis:chat-event-cache:*:snapshot:*"
    )
    assert len(snapshot_keys) == 1

    await redis._client.flushdb()  # noqa: SLF001 - isolated test database
    clear_chat_v2_read_caches()
    await cache.get_or_build(
        authority_token=bound.authority_token,
        scope_key=scope_key,
        session_refs=refs,
        cursor_secret="cursor-secret",
        build=lambda: build_test_snapshot(bound, scope_key=scope_key),
    )
    reads = (delegate.page_calls, delegate.watermark_calls)
    tasks: set[asyncio.Task[Any]] = set()

    async def discover():
        yield scope_key

    def enqueue(_conversation_id: str) -> bool:
        task = asyncio.create_task(
            cache.get_or_build(
                authority_token=bound.authority_token,
                scope_key=scope_key,
                session_refs=refs,
                cursor_secret="cursor-secret",
                build=lambda: build_test_snapshot(bound, scope_key=scope_key),
                fail_open=False,
            )
        )
        tasks.add(task)
        task.add_done_callback(tasks.discard)
        return True

    reconciler = ChatSnapshotActiveReconciler(
        discover,
        enqueue,
        interval_seconds=policy.ttl_seconds / 2,
    )
    await reconciler.start()
    await asyncio.sleep(policy.ttl_seconds + 0.4)
    await reconciler.stop()
    if tasks:
        await asyncio.gather(*tasks)
    await cache.get_or_build(
        authority_token=bound.authority_token,
        scope_key=scope_key,
        session_refs=refs,
        cursor_secret="cursor-secret",
        build=lambda: build_test_snapshot(bound, scope_key=scope_key),
    )
    assert (delegate.page_calls, delegate.watermark_calls) == reads
    await redis.aclose()


@pytest.mark.anyio
async def test_real_redis_lineage_growth_overwrites_one_snapshot_value() -> None:
    url = os.environ["COGNIS_TEST_REDIS_URL"]
    redis = RedisService(url, operation_timeout_seconds=0.2)
    assert await redis.start()
    await redis._client.flushdb()  # noqa: SLF001 - isolated test database
    policy = EventCachePolicy()
    delegate = Delegate()
    store = make_cache(delegate, cast(Any, redis), cast(Any, monotonic), policy=policy)
    first = store.bind(AUTHORITY)
    second = store.bind(
        EventStoreAuthority(
            user_email=AUTHORITY.user_email,
            agent_id="agent-b",
            agent_owner_email=AUTHORITY.agent_owner_email,
        )
    )
    cache = _snapshot_cache(store, redis, policy)
    stable_authority = store.derived_key_digest(
        "snapshot-conversation-authority",
        AUTHORITY.user_email,
    )
    first_ref = _refs(first)[0]
    second_ref = ConversationSessionRef(
        session_id="session-b",
        event_store_session_id="session-b",
        ordinal=1,
        reader=second,
        authority_token=second.authority_token,
    )
    old_identity = await cache._identity(
        authority_token=stable_authority,
        scope_key="conversation:lineage-growth",
        session_refs=[first_ref],
    )
    new_identity = await cache._identity(
        authority_token=stable_authority,
        scope_key="conversation:lineage-growth",
        session_refs=[first_ref, second_ref],
    )
    assert old_identity is not None and new_identity is not None
    assert old_identity.value_key == new_identity.value_key
    assert old_identity.local_key != new_identity.local_key
    assert await store.generation_fenced_write(
        old_identity.value_key,
        b"old-lineage",
        old_identity.fence,
        ttl_seconds=policy.ttl_seconds,
    )
    assert await store.generation_fenced_write(
        new_identity.value_key,
        b"new-lineage",
        new_identity.fence,
        ttl_seconds=policy.ttl_seconds,
    )
    keys = await redis._client.keys(  # noqa: SLF001 - cardinality evidence
        "cognis:chat-event-cache:*:snapshot:*"
    )
    assert keys == [old_identity.value_key.encode()]
    assert await redis.get(old_identity.value_key) == b"new-lineage"
    await redis.aclose()
