from __future__ import annotations

import asyncio
import os

import pytest
from sqlalchemy import text

from cognis.core.turn_scheduler import TurnError
from cognis.store import queries
from cognis.store.direct_turns import DirectTurnAdmissionRejected, DirectTurnStore
from cognis.store.models import ManagedChannelBinding
from tests.unit.test_managed_channel_owner_api import (
    admit_scheduler_turn,
    ready_contact,
    stage_controller_wait,
)
from tests.unit.test_managed_channel_owner_api import (
    contact_env as contact_env,
)

pytestmark = pytest.mark.skipif(
    not os.environ.get("COGNIS_APP_CONTACT_TEST_POSTGRES_URL"),
    reason="Requires an isolated PostgreSQL test database",
)


async def wait_for_binding_lock(env) -> None:
    async def observe() -> None:
        while True:
            async with env.engine.connect() as connection:
                blocked = await connection.scalar(
                    text(
                        "SELECT EXISTS (SELECT 1 FROM pg_stat_activity "
                        "WHERE datname = current_database() AND pid <> pg_backend_pid() "
                        "AND wait_event_type = 'Lock' AND query LIKE :binding_query)"
                    ),
                    {"binding_query": f"%{ManagedChannelBinding.__tablename__}%"},
                )
            if blocked:
                return
            await asyncio.sleep(0.01)

    await asyncio.wait_for(observe(), timeout=10)


@pytest.mark.parametrize("waiting", [False, True])
@pytest.mark.parametrize("close_wins", [False, True])
async def test_close_and_durable_admission_are_serialized(
    contact_env, monkeypatch, waiting, close_wins
):
    env = contact_env
    binding, link = await ready_contact(env)
    env.scheduler.has_active_turn = lambda _: False
    if waiting:
        await stage_controller_wait(env, binding, link)
    store = DirectTurnStore(env.factory)

    async def cancel(conversation_id):
        await store.cancel_conversation(conversation_id, clear_queue=True)

    env.scheduler.cancel_turn.side_effect = cancel
    lock_held = asyncio.Event()
    release = asyncio.Event()
    tasks: list[asyncio.Task] = []
    original_complete = queries.complete_managed_channel_conversation

    async def gated_complete(*args, **kwargs):
        result = await original_complete(*args, **kwargs)
        lock_held.set()
        await release.wait()
        return result

    if close_wins:
        monkeypatch.setattr(queries, "complete_managed_channel_conversation", gated_complete)

    async def close():
        await env.managed.terminate_controller_conversation(
            link_id=link.link_id,
            owner_epoch=link.owner_epoch,
            reason="Owner ended the exchange",
        )

    async def submit(conversation_id, message, **kwargs):
        original_guard = kwargs["durable_admission_guard"]

        async def gated_guard(db):
            admitted = await original_guard(db)
            if not close_wins:
                assert admitted
                lock_held.set()
                await release.wait()
            return admitted

        kwargs["durable_admission_guard"] = gated_guard
        # Only this test holds a transaction at an explicit concurrency barrier.
        if close_wins:
            closing = asyncio.create_task(close())
            tasks.append(closing)
            await asyncio.wait_for(lock_held.wait(), timeout=10)
            admission = asyncio.create_task(
                admit_scheduler_turn(env, conversation_id, message, kwargs)
            )
            tasks.append(admission)
        else:
            admission = asyncio.create_task(
                admit_scheduler_turn(env, conversation_id, message, kwargs)
            )
            tasks.append(admission)
            await asyncio.wait_for(lock_held.wait(), timeout=10)
            closing = asyncio.create_task(close())
            tasks.append(closing)
        await wait_for_binding_lock(env)
        release.set()
        if close_wins:
            with pytest.raises(DirectTurnAdmissionRejected):
                await admission
            await closing
            return TurnError(
                code="conversation_closed",
                message="Conversation closed during admission",
                recoverable=False,
            )
        await admission
        await closing
        return None

    env.scheduler.submit_turn.side_effect = submit
    try:
        error = await env.managed.submit_controller_instruction(
            link=link,
            message="PRIVATE concurrent instruction",
            turn_id="concurrent-owner-turn",
            wait=False,
        )
        assert (error is not None) is close_wins
    finally:
        release.set()
        await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), timeout=10)
    async with env.factory() as db:
        current = await queries.get_managed_conversation_link(db, link.link_id)
        current_binding = await db.get(ManagedChannelBinding, binding.binding_id)
        receipt = await queries.get_managed_controller_turn_receipt(
            db, link=current, turn_id="concurrent-owner-turn"
        )
        assert current.conversation_state == "cancelled"
        assert current_binding.state == "cancelled"
        assert current_binding.active_route_key is None
        if close_wins:
            assert receipt is None
        else:
            assert receipt is not None
            assert receipt.status == "cancelled"
