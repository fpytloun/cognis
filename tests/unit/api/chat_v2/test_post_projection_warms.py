from __future__ import annotations

import asyncio

import pytest

from cognis.api.chat_v2.post_projection_warms import PostProjectionWarmRevisions
from cognis.api.chat_v2.snapshot_warmer import ChatSnapshotWarmer


async def _wait_until_idle(warmer: ChatSnapshotWarmer) -> None:
    for _ in range(100):
        if warmer.pending_count == 0 and not warmer._active:
            return
        await asyncio.sleep(0.01)
    raise AssertionError("snapshot warmer did not become idle")


@pytest.mark.asyncio
async def test_work_commit_during_initial_warm_forces_one_caught_up_warm() -> None:
    revisions = PostProjectionWarmRevisions(max_entries=8)
    first_started = asyncio.Event()
    finish_first = asyncio.Event()
    caught_up_published = asyncio.Event()
    callback_revisions: list[int | None] = []

    async def warm(conversation_id: str):
        revision = revisions.current(conversation_id)
        callback_revisions.append(revision)
        if len(callback_revisions) == 1:
            first_started.set()
            await finish_first.wait()
            result = ("skipped", "context_changed")
        else:
            caught_up_published.set()
            result = ("succeeded", None)
        if result[0] != "retry":
            revisions.complete(conversation_id, revision)
        return result

    warmer = ChatSnapshotWarmer(
        warm,
        worker_count=1,
        retry_seconds=0.01,
        max_pending=8,
    )
    await warmer.start()
    assert warmer.enqueue("conversation-1")
    await first_started.wait()

    assert revisions.admit("conversation-1", warmer.enqueue)
    committed_revision = revisions.current("conversation-1")
    assert committed_revision is not None
    finish_first.set()

    await asyncio.wait_for(caught_up_published.wait(), timeout=1)
    await _wait_until_idle(warmer)

    assert callback_revisions == [None, committed_revision]
    assert revisions.current("conversation-1") is None
    assert warmer.pending_count == 0
    assert warmer._active == set()
    await warmer.stop()


@pytest.mark.asyncio
async def test_capacity_rejection_preserves_oldest_accepted_forced_warm() -> None:
    revisions = PostProjectionWarmRevisions(max_entries=2)
    warmed: list[tuple[str, int | None]] = []
    blocker_started = asyncio.Event()
    release_blocker = asyncio.Event()

    async def warm(conversation_id: str):
        if conversation_id == "conversation-blocker":
            blocker_started.set()
            await release_blocker.wait()
            return "succeeded", None
        revision = revisions.current(conversation_id)
        warmed.append((conversation_id, revision))
        revisions.complete(conversation_id, revision)
        return "succeeded", None

    warmer = ChatSnapshotWarmer(warm, worker_count=1, max_pending=2)
    await warmer.start()
    assert warmer.enqueue("conversation-blocker")
    await blocker_started.wait()
    assert revisions.admit("conversation-oldest", warmer.enqueue)
    oldest_revision = revisions.current("conversation-oldest")
    assert revisions.admit("conversation-second", warmer.enqueue)

    assert revisions.admit("conversation-rejected", warmer.enqueue) is False
    assert revisions.current("conversation-oldest") == oldest_revision
    assert revisions.current("conversation-rejected") is None
    assert warmer.pending_count == 2

    release_blocker.set()
    await _wait_until_idle(warmer)

    assert warmed == [
        ("conversation-oldest", oldest_revision),
        ("conversation-second", 2),
    ]
    assert revisions.current("conversation-oldest") is None
    await warmer.stop()


def test_rejected_new_conversation_does_not_leak_a_marker() -> None:
    revisions = PostProjectionWarmRevisions(max_entries=1)
    enqueued: list[str] = []

    assert revisions.admit("conversation-accepted", lambda value: enqueued.append(value) or True)
    assert (
        revisions.admit("conversation-rejected", lambda value: enqueued.append(value) or True)
        is False
    )

    assert enqueued == ["conversation-accepted"]
    assert revisions.current("conversation-rejected") is None
    assert revisions.current("conversation-accepted") == 1


def test_rejected_coalesced_admission_restores_accepted_revision() -> None:
    revisions = PostProjectionWarmRevisions(max_entries=1)
    assert revisions.admit("conversation-accepted", lambda _value: True)
    accepted_revision = revisions.current("conversation-accepted")

    assert revisions.admit("conversation-accepted", lambda _value: False) is False

    assert revisions.current("conversation-accepted") == accepted_revision


@pytest.mark.asyncio
async def test_active_conversation_coalesces_new_revision_into_one_follow_up() -> None:
    revisions = PostProjectionWarmRevisions(max_entries=1)
    first_started = asyncio.Event()
    finish_first = asyncio.Event()
    observed: list[int | None] = []

    async def warm(conversation_id: str):
        revision = revisions.current(conversation_id)
        observed.append(revision)
        if len(observed) == 1:
            first_started.set()
            await finish_first.wait()
        revisions.complete(conversation_id, revision)
        return "succeeded", None

    warmer = ChatSnapshotWarmer(warm, worker_count=1, max_pending=1)
    await warmer.start()
    assert revisions.admit("conversation-1", warmer.enqueue)
    await first_started.wait()
    first_revision = revisions.current("conversation-1")

    assert revisions.admit("conversation-1", warmer.enqueue)
    second_revision = revisions.current("conversation-1")
    assert second_revision is not None and second_revision != first_revision
    finish_first.set()
    await _wait_until_idle(warmer)

    assert observed == [first_revision, second_revision]
    assert revisions.current("conversation-1") is None
    await warmer.stop()


def test_older_completion_cannot_clear_a_newer_revision() -> None:
    revisions = PostProjectionWarmRevisions(max_entries=8)
    first_reservation = revisions.reserve("conversation-1")
    second_reservation = revisions.reserve("conversation-1")
    assert first_reservation is not None and second_reservation is not None
    first = first_reservation.revision
    second = second_reservation.revision

    assert revisions.complete("conversation-1", first) is False
    assert revisions.current("conversation-1") == second
    assert revisions.complete("conversation-1", second) is True
    assert revisions.current("conversation-1") is None
