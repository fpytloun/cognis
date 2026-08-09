from __future__ import annotations

import asyncio
from dataclasses import replace
from pathlib import Path

import pytest

from cognis.bootstrap import run_schema_bootstrap
from cognis.store.coordination import DatabaseLeaseStore
from cognis.store.database import create_engine, create_session_factory


@pytest.mark.asyncio
async def test_lease_fencing_and_compare_and_swap(tmp_path: Path) -> None:
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path / 'leases.db'}")
    await run_schema_bootstrap(engine)
    store = DatabaseLeaseStore(create_session_factory(engine))

    first = await store.acquire("scheduler", "controller-a", ttl_seconds=60)
    assert first is not None
    assert first.fencing_token == 1
    assert await store.acquire("scheduler", "controller-b", ttl_seconds=60) is None

    renewed = await store.renew(first, ttl_seconds=120)
    assert renewed is not None
    stale = replace(first, fencing_token=first.fencing_token + 1)
    assert await store.renew(stale, ttl_seconds=120) is None
    assert await store.release(stale) is False
    assert await store.release(renewed) is True

    second = await store.acquire("scheduler", "controller-b", ttl_seconds=60)
    assert second is not None
    assert second.fencing_token == 2
    await engine.dispose()


@pytest.mark.asyncio
async def test_concurrent_sqlite_connections_have_single_lease_winner(tmp_path: Path) -> None:
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path / 'contended-leases.db'}")
    await run_schema_bootstrap(engine)
    factory = create_session_factory(engine)
    first_store = DatabaseLeaseStore(factory)
    second_store = DatabaseLeaseStore(factory)

    for index in range(10):
        resource = f"resource-{index}"
        first, second = await asyncio.gather(
            first_store.acquire(resource, "controller-a", ttl_seconds=60),
            second_store.acquire(resource, "controller-b", ttl_seconds=60),
        )

        winners = [lease for lease in (first, second) if lease is not None]
        assert len(winners) == 1
        assert winners[0].fencing_token == 1

    await engine.dispose()


@pytest.mark.asyncio
async def test_expired_lease_cannot_be_renewed(tmp_path: Path) -> None:
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path / 'expired-lease.db'}")
    await run_schema_bootstrap(engine)
    store = DatabaseLeaseStore(create_session_factory(engine))

    expired = await store.acquire("scheduler", "controller-a", ttl_seconds=0)
    assert expired is not None
    assert await store.renew(expired, ttl_seconds=60) is None
    await engine.dispose()


@pytest.mark.asyncio
async def test_revoke_invalidates_current_owner_and_advances_fence(tmp_path: Path) -> None:
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path / 'revoked-lease.db'}")
    await run_schema_bootstrap(engine)
    store = DatabaseLeaseStore(create_session_factory(engine))

    first = await store.acquire("channel-account:a1", "controller-a", ttl_seconds=60)
    assert first is not None and await store.is_current(first)
    assert await store.revoke("channel-account:a1")
    assert not await store.is_current(first)
    assert await store.release(first) is False
    second = await store.acquire("channel-account:a1", "controller-b", ttl_seconds=60)
    assert second is not None
    assert second.fencing_token > first.fencing_token
    await engine.dispose()
