"""Cross-controller coalescing for paid speech work; cache bytes stay in artifacts."""

from __future__ import annotations

import asyncio
import time
import uuid
from collections.abc import Awaitable, Callable

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from cognis.store.coordination import DatabaseLeaseStore, Lease, database_now_expression
from cognis.store.models import CoordinationLeaseRow

LEASE_SECONDS = 60


class SpeechGenerationInterrupted(ValueError):
    """An accepted generation cannot safely be replayed automatically."""


class SpeechGenerationClaim:
    """Fence publication and cancel only this claim's work if renewal fails."""

    def __init__(self, store: DatabaseLeaseStore, lease: Lease) -> None:
        self.store = store
        self.lease = lease
        self.lost = False

    async def check(self) -> None:
        """Renew and verify ownership before an external side effect/publication."""
        renewed = await self.store.renew(self.lease, ttl_seconds=LEASE_SECONDS)
        if renewed is None:
            self.lost = True
            raise SpeechGenerationInterrupted(
                "Speech generation lost its claim. Retry explicitly; previous work may have been billed."
            )
        self.lease = renewed


async def shared_speech_generation[T](
    sessions: async_sessionmaker[AsyncSession],
    key: str,
    load: Callable[[], Awaitable[T | None]],
    generate: Callable[[SpeechGenerationClaim], Awaitable[T]],
) -> T:
    """Join one shared result without automatically retrying another paid attempt."""
    if (cached := await load()) is not None:
        return cached
    store = DatabaseLeaseStore(sessions)
    resource = f"speech-generation:{key}"
    lease = await store.acquire(resource, uuid.uuid4().hex, ttl_seconds=LEASE_SECONDS)
    if lease is None:
        deadline = time.monotonic() + 600
        while time.monotonic() < deadline:
            if (cached := await load()) is not None:
                return cached
            async with sessions() as session:
                active = await session.scalar(
                    select(CoordinationLeaseRow.resource_key).where(
                        CoordinationLeaseRow.resource_key == resource,
                        CoordinationLeaseRow.lease_expires_at > database_now_expression(session),
                    )
                )
            if active is None:
                if (cached := await load()) is not None:
                    return cached
                raise SpeechGenerationInterrupted(
                    "The shared speech attempt was interrupted. Retry explicitly; it may have been billed."
                )
            await asyncio.sleep(0.5)
        raise SpeechGenerationInterrupted(
            "Speech is still being generated. Try playing it again later."
        )

    claim = SpeechGenerationClaim(store, lease)
    work: asyncio.Task[T] | None = None
    renewal: asyncio.Task[None] | None = None

    async def produce() -> T:
        return await generate(claim)

    async def renew() -> None:
        try:
            while True:
                await asyncio.sleep(LEASE_SECONDS / 4)
                await claim.check()
        except asyncio.CancelledError:
            raise
        except Exception:
            claim.lost = True
            if work is not None:
                work.cancel()

    try:
        # A previous owner may have published between our first read and acquire.
        if (cached := await load()) is not None:
            return cached
        work = asyncio.create_task(produce())
        renewal = asyncio.create_task(renew())
        try:
            return await work
        except asyncio.CancelledError:
            if claim.lost:
                raise SpeechGenerationInterrupted(
                    "Speech generation was interrupted. Retry explicitly; it may have been billed."
                ) from None
            raise
    finally:
        tasks = [task for task in (renewal, work) if task is not None]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await store.release(claim.lease)
