from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from starlette.requests import Request

from cognis.api.routes.conversations import _agent_direct_chat_projection
from cognis.api.routes.conversations import router as conversations_router
from cognis.channels.managed import managed_route_key
from cognis.core.turn_scheduler import TurnError
from cognis.store import queries
from cognis.store.direct_turns import DirectTurnStore
from cognis.store.models import (
    ChannelDeliveryOutboxRow,
    Conversation,
    ManagedChannelBinding,
    ManagedConversationSignal,
)
from tests.unit.test_app_contact import OWNER, RECIPIENT, enqueue
from tests.unit.test_app_contact import contact_env as contact_env
from tests.unit.test_app_contact import recipient_app as recipient_app


@pytest.fixture
def owner_app(contact_env, recipient_app):
    env = contact_env
    env.scheduler.has_active_turn = lambda _: False
    env.scheduler.running_turn_state = lambda _: None
    recipient_app.state.providers = SimpleNamespace(
        managed_channel_service=env.managed, session_cache=None
    )
    recipient_app.state.event_bus = env.bus
    recipient_app.state.agent_registry = SimpleNamespace(list_all=AsyncMock(return_value=[]))
    recipient_app.state.turn_scheduler = env.scheduler
    recipient_app.include_router(conversations_router)
    return recipient_app


async def ready_contact(env):
    outbox, binding = await enqueue(env, managed=True)
    await env.service.deliver(outbox.delivery_id)
    async with env.factory() as db:
        row = await db.get(ManagedChannelBinding, binding.binding_id)
        row.state = "waiting_external"
        row.active_route_key = managed_route_key(OWNER, row.account_id, row.chat_id, row.thread_key)
        delivery = await db.get(ChannelDeliveryOutboxRow, outbox.delivery_id)
        delivery.status = "sent"
        link = await queries.get_managed_conversation_link(db, binding.link_id)
        link.turn_state = "idle"
        target = await db.get(Conversation, link.target_conversation_id)
        target.context_data = {"kind": "agent_work"}
        await db.commit()
    return binding, link


async def admit_scheduler_turn(env, conversation_id, message, kwargs):
    result = await DirectTurnStore(env.factory).admit(
        conversation_id=conversation_id,
        session_id=None,
        user_id=OWNER,
        agent_id=env.source.agent_id,
        payload={
            "schema_version": 1,
            "content": message,
            "attachments": [],
            "metadata": {"user_message_metadata": kwargs["user_message_metadata"]},
        },
        idempotency_scope="managed-owner-test",
        idempotency_key=kwargs["durable_request_id"],
        request_id=kwargs["durable_request_id"],
        turn_id=kwargs["turn_id"],
        admission_guard=kwargs["durable_admission_guard"],
        transaction_participant=kwargs["admission_transaction_participant"],
    )
    await kwargs["admission_observer"](result.request.turn_id, result.queued_behind_predecessor)
    return result


async def stage_controller_wait(env, binding, link):
    async with env.factory() as db:
        current = await queries.get_managed_conversation_link(db, link.link_id)
        current.turn_state = "running"
        current.active_turn_id = "controller-wait-turn"
        row = await db.get(ManagedChannelBinding, binding.binding_id)
        row.state = "processing"
        signal = await queries.create_managed_conversation_signal(
            db,
            link_id=link.link_id,
            owner_epoch=link.owner_epoch,
            message="PRIVATE question",
            wait=True,
            source_turn_id="controller-wait-turn",
        )
        await db.commit()
    return signal


@pytest.mark.parametrize("action", ["wait", "interrupt", "stop", "retry", "fork", "take-control"])
async def test_channel_owner_cannot_use_ordinary_managed_actions(contact_env, owner_app, action):
    env = contact_env
    _, link = await ready_contact(env)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=owner_app), base_url="http://test"
    ) as client:
        response = await client.post(
            f"/api/v1/conversations/{link.target_conversation_id}/managed/{action}",
            headers={"X-User": OWNER},
            json={},
        )
    assert response.status_code == 403, response.text
    env.scheduler.submit_turn.assert_not_awaited()
    env.scheduler.cancel_turn.assert_not_awaited()


async def test_owner_instruction_is_private_and_uses_the_channel_delivery_observer(
    contact_env, owner_app
):
    env = contact_env
    binding, link = await ready_contact(env)

    async def submit(conversation_id, message, **kwargs):
        await admit_scheduler_turn(env, conversation_id, message, kwargs)

    env.scheduler.submit_turn.side_effect = submit
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=owner_app), base_url="http://test"
    ) as client:
        response = await client.post(
            f"/api/v1/conversations/{link.target_conversation_id}/managed/send",
            headers={"X-User": OWNER},
            json={"message": "PRIVATE owner instruction"},
        )
        snapshot = await client.get(
            f"/api/v1/conversations/{link.target_conversation_id}?include_state=false",
            headers={"X-User": OWNER},
        )
    assert response.status_code == 200, response.text
    kwargs = env.scheduler.submit_turn.await_args.kwargs
    assert kwargs["user_email"] == OWNER
    assert kwargs["allow_queue"] is False
    assert kwargs["user_message_metadata"]["private_controller_instruction"] is True
    assert any(
        type(observer).__name__ == "ManagedChannelFinalObserver"
        for observer in kwargs["turn_observers"]
    )
    public = await env.service.view(binding.binding_id, RECIPIENT)
    assert "PRIVATE owner instruction" not in public.model_dump_json()
    assert public.conversation_id is not None
    assert public.conversation_id != link.target_conversation_id
    async with env.factory() as db:
        assert (await db.get(Conversation, public.conversation_id)).user_email == RECIPIENT
    assert snapshot.status_code == 200, snapshot.text
    assert snapshot.json()["managed_agent"]["kind"] == "channel"


async def test_owner_termination_fences_before_cancellation_and_keeps_recipient_history(
    contact_env, owner_app
):
    env = contact_env
    binding, link = await ready_contact(env)
    before = await env.service.view(binding.binding_id, RECIPIENT)
    assert before.messages

    async def cancel(conversation_id):
        assert conversation_id == link.target_conversation_id
        async with env.factory() as db:
            current = await db.get(ManagedChannelBinding, binding.binding_id)
            assert current.state == "cancelled"
            assert current.active_route_key is None
            current_link = await queries.get_managed_conversation_link(db, link.link_id)
            assert current_link.conversation_state == "cancelled"

    env.scheduler.cancel_turn.side_effect = cancel
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=owner_app), base_url="http://test"
    ) as client:
        url = f"/api/v1/conversations/{link.target_conversation_id}/managed/close"
        response = await client.post(url, headers={"X-User": OWNER}, json={})
        assert response.status_code == 200, response.text
        again = await client.post(url, headers={"X-User": OWNER}, json={})
        assert again.status_code == 200, again.text
    assert env.scheduler.cancel_turn.await_count == 2
    public = await env.service.view(binding.binding_id, RECIPIENT)
    assert public.messages == before.messages
    assert public.can_reply is False


@pytest.mark.parametrize("fails", [False, True])
async def test_private_instruction_resumes_controller_wait_or_restores_it_on_failure(
    contact_env, fails
):
    env = contact_env
    env.scheduler.has_active_turn = lambda _: False
    binding, link = await ready_contact(env)
    signal = await stage_controller_wait(env, binding, link)

    async def submit(conversation_id, message, **kwargs):
        if fails:
            raise RuntimeError("admission unavailable")
        await admit_scheduler_turn(env, conversation_id, message, kwargs)

    env.scheduler.submit_turn.side_effect = submit
    operation = env.managed.submit_controller_instruction(
        link=link, message="PRIVATE answer", turn_id="owner-resume-turn", wait=False
    )
    if fails:
        with pytest.raises(RuntimeError, match="admission unavailable"):
            await operation
    else:
        assert await operation is None
    async with env.factory() as db:
        current = await queries.get_managed_conversation_link(db, link.link_id)
        assert current.turn_state == ("waiting_controller" if fails else "running")
        current_signal = await db.get(ManagedConversationSignal, signal.signal_id)
        assert current_signal.state == ("waiting_controller" if fails else "consumed")
        assert await db.get(Conversation, link.target_conversation_id) is not None


async def test_reading_contact_preserves_discovery_and_the_existing_projection(
    contact_env, owner_app
):
    env = contact_env
    binding, _ = await ready_contact(env)
    public = await env.service.view(binding.binding_id, RECIPIENT)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=owner_app), base_url="http://test"
    ) as client:
        response = await client.post(
            f"/api/v1/conversations/{public.conversation_id}/read",
            headers={"X-User": RECIPIENT},
        )
        assert response.status_code == 200, response.text
    request = Request({"type": "http", "app": owner_app})
    projection = await _agent_direct_chat_projection(request, user_email=RECIPIENT)
    assert [item.conversation.conversation_id for item in projection] == [public.conversation_id]
    assert projection[0].conversation.has_unread is False
    assert (await env.service.view(binding.binding_id, RECIPIENT)).messages == public.messages


@pytest.mark.parametrize("waiting", [False, True])
async def test_termination_between_preparation_and_admission_cannot_reopen_the_route(
    contact_env, waiting
):
    env = contact_env
    binding, link = await ready_contact(env)
    env.scheduler.has_active_turn = lambda _: False
    signal = await stage_controller_wait(env, binding, link) if waiting else None
    rejected = TurnError(code="conversation_closed", message="Route closed", recoverable=False)

    async def submit(*args, **kwargs):
        await env.managed.terminate_controller_conversation(
            link_id=link.link_id, owner_epoch=link.owner_epoch, reason="Owner ended the exchange"
        )
        async with env.factory() as db:
            assert await kwargs["durable_admission_guard"](db) is False
        return rejected

    env.scheduler.submit_turn.side_effect = submit
    assert (
        await env.managed.submit_controller_instruction(
            link=link, message="Private instruction", turn_id="fenced-owner-turn", wait=False
        )
        is rejected
    )
    async with env.factory() as db:
        row = await db.get(ManagedChannelBinding, binding.binding_id)
        assert row.state == "cancelled"
        assert row.active_route_key is None
        current = await queries.get_managed_conversation_link(db, link.link_id)
        assert current.conversation_state == "cancelled"
        assert current.active_turn_id is None
        assert (
            await queries.get_managed_controller_turn_receipt(
                db, link=current, turn_id="fenced-owner-turn"
            )
            is None
        )
        if signal is not None:
            # Route closure preserves the reservation identity for durable reconciliation.
            reservation = await db.get(ManagedConversationSignal, signal.signal_id)
            assert reservation.state == "resuming"
            assert reservation.resume_turn_id == "fenced-owner-turn"


@pytest.mark.parametrize("waiting", [False, True])
async def test_post_commit_admission_exception_does_not_compensate_durable_acceptance(
    contact_env, waiting
):
    env = contact_env
    binding, link = await ready_contact(env)
    env.scheduler.has_active_turn = lambda _: False
    signal = await stage_controller_wait(env, binding, link) if waiting else None

    async def submit(conversation_id, message, **kwargs):
        await admit_scheduler_turn(env, conversation_id, message, kwargs)
        raise RuntimeError("post-commit wakeup failed")

    env.scheduler.submit_turn.side_effect = submit
    with pytest.raises(RuntimeError, match="post-commit wakeup failed"):
        await env.managed.submit_controller_instruction(
            link=link,
            message="PRIVATE durable instruction",
            turn_id="accepted-owner-turn",
            wait=False,
        )
    async with env.factory() as db:
        current = await queries.get_managed_conversation_link(db, link.link_id)
        receipt = await queries.get_managed_controller_turn_receipt(
            db, link=current, turn_id="accepted-owner-turn"
        )
        assert receipt is not None
        assert current.turn_state == "running"
        assert current.active_turn_id == "accepted-owner-turn"
        assert (await db.get(ManagedChannelBinding, binding.binding_id)).state == "processing"
        if signal is not None:
            assert (await db.get(ManagedConversationSignal, signal.signal_id)).state == "consumed"


async def test_termination_retry_finishes_cancellation_after_caller_disconnect(contact_env):
    env = contact_env
    binding, link = await ready_contact(env)
    env.scheduler.cancel_turn.side_effect = [asyncio.CancelledError(), None]
    with pytest.raises(asyncio.CancelledError):
        await env.managed.terminate_controller_conversation(
            link_id=link.link_id, owner_epoch=link.owner_epoch, reason="Owner ended the exchange"
        )
    await env.managed.terminate_controller_conversation(
        link_id=link.link_id, owner_epoch=link.owner_epoch, reason="Owner ended the exchange"
    )
    assert env.scheduler.cancel_turn.await_count == 2
    async with env.factory() as db:
        assert (await db.get(ManagedChannelBinding, binding.binding_id)).active_route_key is None


@pytest.mark.parametrize("kind", ["channel", "agent"])
@pytest.mark.parametrize("operation", ["admission", "launch"])
async def test_canonical_admission_cannot_reopen_a_terminated_channel(contact_env, kind, operation):
    env = contact_env
    _, link = await ready_contact(env)
    await env.managed.terminate_controller_conversation(
        link_id=link.link_id, owner_epoch=link.owner_epoch, reason="Owner ended the exchange"
    )
    async with env.factory() as db:
        current = await queries.get_managed_conversation_link(db, link.link_id)
        current.kind = kind
        await db.commit()
    async with env.factory() as db:
        if operation == "admission":
            changed = await queries.admit_managed_conversation_turn(
                db,
                link.link_id,
                turn_id="late-parent-turn",
                turn_state="running",
                notify_on_completion=False,
                control_metadata=None,
            )
        else:
            target_session = await queries.create_session(
                db,
                link.target_conversation_id,
                OWNER,
                link.target_agent_id,
            )
            changed = await queries.mark_managed_conversation_turn_running(
                db,
                link.target_conversation_id,
                turn_id="late-parent-turn",
                target_session_id=target_session.session_id,
            )
        assert bool(changed) is (kind == "agent")
        await db.commit()
    async with env.factory() as db:
        current = await queries.get_managed_conversation_link(db, link.link_id)
        assert current.conversation_state == ("open" if kind == "agent" else "cancelled")
