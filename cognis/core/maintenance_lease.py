"""Single-owner periodic maintenance using the existing database lease store."""

from __future__ import annotations

import asyncio
import contextlib
import uuid
from collections.abc import Awaitable, Callable

from cognis.core.cancellation import join_owned_task
from cognis.logging import get_logger
from cognis.store.coordination import DatabaseLeaseStore, Lease

logger = get_logger(__name__)
_TTL_SECONDS = 30.0


async def run_periodic_maintenance(
    operation: Callable[[], Awaitable[object]],
    *,
    stop: asyncio.Event,
    interval_seconds: float,
    resource_key: str,
    lease_store: DatabaseLeaseStore | None,
) -> None:
    """Hold ownership across passes, cancel work on renewal failure, release fenced.

    A lease cannot fence an already-issued storage request. Cleanup operations
    must remain idempotent and must never delete objects still attachable.
    """
    owner_id = uuid.uuid4().hex

    async def passes() -> None:
        while not stop.is_set():
            try:
                await operation()
            except Exception:
                logger.warning(
                    "Maintenance pass failed", extra={"resource_key": resource_key}, exc_info=True
                )
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=interval_seconds)

    if lease_store is None:
        await passes()
        return

    async def settle_owned_work(work: asyncio.Task[None] | None, lease: Lease | None) -> None:
        try:
            if work is not None:
                work.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await work
        finally:
            if lease is not None:
                with contextlib.suppress(Exception):
                    async with asyncio.timeout(_TTL_SECONDS / 3):
                        await lease_store.release(lease)

    while not stop.is_set():
        lease: Lease | None = None
        work: asyncio.Task[None] | None = None
        try:
            lease = await lease_store.acquire(resource_key, owner_id, ttl_seconds=_TTL_SECONDS)
            if lease is not None:
                work = asyncio.create_task(passes(), name=resource_key)
                while not work.done():
                    done, _ = await asyncio.wait({work}, timeout=_TTL_SECONDS / 3)
                    if done:
                        await work
                        break
                    async with asyncio.timeout(_TTL_SECONDS / 3):
                        renewed = await lease_store.renew(lease, ttl_seconds=_TTL_SECONDS)
                    if renewed is None:
                        break
                    lease = renewed
        except Exception:
            logger.warning(
                "Maintenance ownership failed", extra={"resource_key": resource_key}, exc_info=True
            )
        finally:
            # A shutdown cancellation must not interrupt work already closing
            # its database session after lease loss.
            await join_owned_task(
                asyncio.create_task(settle_owned_work(work, lease), name=f"{resource_key}:cleanup")
            )
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=min(interval_seconds, _TTL_SECONDS / 3))
