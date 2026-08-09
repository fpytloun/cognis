from __future__ import annotations

import asyncio

import pytest

from cognis.api.chat_v2.cached_event_store import AppendInvalidation
from cognis.core import event_append_invalidation as module
from cognis.core.event_append_invalidation import EventAppendInvalidationDispatcher


def _work(token: str, revision: int) -> AppendInvalidation:
    return AppendInvalidation(
        session_token=token,
        authority_token=f"authority-{revision}",
        last_seq=revision,
        has_events=True,
        local_revision=revision,
    )


def test_dispatcher_metrics_use_only_bounded_identity_free_labels() -> None:
    assert module.APPEND_INVALIDATION_PENDING._labelnames == ()  # noqa: SLF001
    assert module.APPEND_INVALIDATION_ENQUEUED._labelnames == ()  # noqa: SLF001
    assert module.APPEND_INVALIDATION_COALESCED._labelnames == ()  # noqa: SLF001
    assert module.APPEND_INVALIDATION_PROCESSED._labelnames == ()  # noqa: SLF001
    assert module.APPEND_INVALIDATION_RETRIED._labelnames == ()  # noqa: SLF001
    assert module.APPEND_INVALIDATION_ERRORS._labelnames == ("stage",)  # noqa: SLF001
    assert module.APPEND_INVALIDATION_DROPPED._labelnames == ("reason",)  # noqa: SLF001


@pytest.mark.asyncio
async def test_dispatcher_coalesces_latest_watermark_and_bounds_pending_sessions() -> None:
    entered = asyncio.Event()
    release = asyncio.Event()
    processed: list[AppendInvalidation] = []
    abandoned: list[AppendInvalidation] = []
    published: list[tuple[str, int]] = []

    class Store:
        async def process_append_invalidation(self, work: AppendInvalidation) -> bool:
            if work.session_token == "a":
                entered.set()
                await release.wait()
            processed.append(work)
            return True

        def abandon_append_invalidation(self, work: AppendInvalidation) -> None:
            abandoned.append(work)

    async def publish(session_token: str, revision: int) -> bool:
        published.append((session_token, revision))
        return True

    dispatcher = EventAppendInvalidationDispatcher(
        event_store=Store(),
        publish_invalidation=publish,
        max_pending_sessions=1,
        backoff_initial_seconds=0.001,
        backoff_max_seconds=0.002,
    )
    await dispatcher.start()
    dispatcher.enqueue(_work("a", 1))
    await asyncio.wait_for(entered.wait(), timeout=1)

    dispatcher.enqueue(_work("b", 2))
    dispatcher.enqueue(_work("b", 3))
    dispatcher.enqueue(_work("c", 4))

    assert [(item.session_token, item.last_seq) for item in abandoned] == [("b", 3)]
    assert dispatcher.pending_count == 1

    release.set()
    await dispatcher.stop(drain_timeout_seconds=1)

    assert [(item.session_token, item.last_seq) for item in processed] == [
        ("a", 1),
        ("c", 4),
    ]
    assert published == [("a", 1), ("c", 4)]
    assert dispatcher.pending_count == 0


@pytest.mark.asyncio
async def test_dispatcher_retries_failed_stages_without_repeating_completed_signal() -> None:
    cache_attempts = 0
    signal_attempts = 0

    class Store:
        async def process_append_invalidation(self, _: AppendInvalidation) -> bool:
            nonlocal cache_attempts
            cache_attempts += 1
            return cache_attempts >= 2

        def abandon_append_invalidation(self, _: AppendInvalidation) -> None:
            raise AssertionError("successful retry must not enter fallback")

    async def publish(_: str, __: int) -> bool:
        nonlocal signal_attempts
        signal_attempts += 1
        return True

    dispatcher = EventAppendInvalidationDispatcher(
        event_store=Store(),
        publish_invalidation=publish,
        backoff_initial_seconds=0.001,
        backoff_max_seconds=0.002,
    )
    await dispatcher.start()
    dispatcher.enqueue(_work("a", 1))
    await dispatcher.stop(drain_timeout_seconds=1)

    assert cache_attempts == 2
    assert signal_attempts == 1


@pytest.mark.asyncio
async def test_dispatcher_retries_signal_without_repeating_completed_cache_work() -> None:
    cache_attempts = 0
    signal_attempts = 0

    class Store:
        async def process_append_invalidation(self, _: AppendInvalidation) -> bool:
            nonlocal cache_attempts
            cache_attempts += 1
            return True

        def abandon_append_invalidation(self, _: AppendInvalidation) -> None:
            raise AssertionError("successful retry must not enter fallback")

    async def publish(_: str, __: int) -> bool:
        nonlocal signal_attempts
        signal_attempts += 1
        return signal_attempts >= 2

    dispatcher = EventAppendInvalidationDispatcher(
        event_store=Store(),
        publish_invalidation=publish,
        backoff_initial_seconds=0.001,
        backoff_max_seconds=0.002,
    )
    await dispatcher.start()
    dispatcher.enqueue(_work("a", 1))
    await dispatcher.stop(drain_timeout_seconds=1)

    assert cache_attempts == 1
    assert signal_attempts == 2
