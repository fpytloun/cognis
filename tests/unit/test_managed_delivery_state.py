"""Managed uncertainty follows persisted outcomes rather than error wording."""

import json
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import create_async_engine

from cognis.channels.bindings import DatabaseManagedChannelBindingLookup
from cognis.channels.delivery import ChannelDeliveryService
from cognis.channels.delivery_state import managed_delivery_outcome_uncertain
from cognis.channels.managed import ManagedChannelService
from cognis.core.events import EventBus
from cognis.core.turn_scheduler import TurnResult, TurnScheduler
from cognis.models.tool import ToolCall
from cognis.store import queries
from cognis.store.database import create_session_factory
from cognis.store.models import Base, ChannelDeliveryOutboxRow
from cognis.tools.builtin.channels import build_channel_tool_handlers
from tests.unit.test_agent_loop import (
    _background_work_agent_loop,
    _background_work_ctx,
    _IdleWaitScheduler,
)
from tests.unit.test_channel_tools import _context
from tests.unit.test_managed_channel_foundation import _seed_channel_link


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["sending", "sent", "suppressed"])
async def test_recovered_outcome_owns_binding_and_recovery_projection(tmp_path, status):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'outcomes.db'}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = create_session_factory(engine)
    try:
        async with factory() as session:
            link, _, _, target = await _seed_channel_link(session)
            await queries.upsert_channel_observed_target(
                session,
                user_email=link.user_email,
                account_id="account-1",
                channel_type="signal",
                chat_id="chat-1",
                chat_kind="direct",
                display_name="Participant",
            )
            await session.commit()
        scheduler = object.__new__(TurnScheduler)
        scheduler._session_factory = factory
        await scheduler._notify_managed_turn_result(
            TurnResult(
                conversation_id=target.conversation_id,
                session_id="target-session",
                message_id="assistant-final",
                turn_id="turn-final",
                final_content="Public final",
            )
        )
        async with factory() as session:
            row = (await session.execute(select(ChannelDeliveryOutboxRow))).scalar_one()
            row.status = status
            row.last_error = "external_send_outcome_uncertain"
            if status == "sending":
                row.lease_token = "lease-expired"
                row.lease_expires_at = datetime.now(UTC) - timedelta(seconds=1)
                row.inflight_chunk_index = 0
                row.inflight_idempotent = False
                row.projected_chunk_count = 1
            binding = await queries.get_managed_channel_binding_for_link(session, link.link_id)
            binding.last_error = "external_send_outcome_uncertain"
            if status != "sending":
                binding.state = "delivery_failed"
            await session.commit()
        managed = ManagedChannelService(factory)
        delivery = ChannelDeliveryService(
            session_factory=factory, event_bus=EventBus(), channel_manager_ref=lambda: None
        )
        managed.set_delivery_service(delivery)
        await delivery.recover_pending_deliveries()
        expected = status == "sending"
        async with factory() as session:
            binding = await queries.get_managed_channel_binding_for_link(session, link.link_id)
            row = (await session.execute(select(ChannelDeliveryOutboxRow))).scalar_one()
            assert await managed_delivery_outcome_uncertain(session, binding) is expected
            if expected:
                assert row.status == "uncertain"
                assert binding.last_error == "stale_non_idempotent_send"
            binding.expires_at = datetime.now(UTC) - timedelta(seconds=1)
            await session.commit()
        projection = await DatabaseManagedChannelBindingLookup(factory).find_active_binding(
            user_email=link.user_email, account_id="account-1", chat_id="chat-1"
        )
        assert projection.outcome_uncertain is expected
        loop = _background_work_agent_loop(factory, _IdleWaitScheduler())
        ctx = _background_work_ctx(link.controller_conversation_id)
        ctx.session.user_email = link.user_email
        ctx.agent.agent_id = "controller"
        ctx.agent.owner_email = link.user_email
        listed = await loop._handle_managed_conversation_tool(
            ToolCall(
                call_id="list-managed-outcomes",
                name="agent_conversation_list",
                arguments={"kind": "channel"},
            ),
            ctx=ctx,
        )
        assert not listed.is_error
        payload = json.loads(listed.output)
        assert payload["conversations"][0]["channel"]["outcome_uncertain"] is expected
        handlers = build_channel_tool_handlers(
            factory, application_secret="managed-outcome-test-secret"
        )
        targets = await handlers["search_channel_targets"]({}, _context())
        assert targets["targets"][0]["active_managed_conversation"]["outcome_uncertain"] is expected
        recovery = await managed.recover_expired_delivery_failure(
            target_conversation_id=target.conversation_id,
            expected_owner_epoch=link.owner_epoch,
            user_email=link.user_email,
            actor_agent_id="controller",
            actor_conversation_id=link.controller_conversation_id,
            actor_session_id=link.controller_session_id,
            reason="Reconciled externally",
        )
        assert recovery.outcome_uncertain is expected
        async with factory() as session:
            binding = await queries.get_managed_channel_binding_for_link(session, link.link_id)
            assert await managed_delivery_outcome_uncertain(session, binding) is expected
            row = (await session.execute(select(ChannelDeliveryOutboxRow))).scalar_one()
            assert row.status == ("uncertain" if expected else status)
    finally:
        await engine.dispose()
