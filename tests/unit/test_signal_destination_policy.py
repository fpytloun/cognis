import asyncio
import os
import uuid
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from cognis.channels.signal_failures import SignalDeliveryFailure, sanitize_signal_failure
from cognis.channels.signal_policy import SignalDestinationPolicy, SignalPolicyBlocked
from cognis.store.database import create_session_factory
from cognis.store.models import Base, SignalDestinationPolicyRow
from tests.unit.test_managed_channel_foundation import _seed_channel_link


@pytest_asyncio.fixture(params=["sqlite", "postgresql"])
async def policy_db(tmp_path, request):
    admin = None
    if request.param == "postgresql":
        url = os.getenv("COGNIS_TEST_POSTGRES_URL")
        if not url:
            pytest.skip("Isolated PostgreSQL URL not configured")
        schema = "signal_policy_" + uuid.uuid4().hex
        admin = create_async_engine(url)
        async with admin.begin() as connection:
            await connection.execute(text(f'CREATE SCHEMA "{schema}"'))
        engine = create_async_engine(url, connect_args={"server_settings": {"search_path": schema}})
    else:
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'policy.db'}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = create_session_factory(engine)
    async with factory() as session:
        await _seed_channel_link(session)
        await session.commit()
    yield factory
    await engine.dispose()
    if admin is not None:
        async with admin.begin() as connection:
            await connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        await admin.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("delay", [None, 86400])
async def test_terminal_rate_limit_blocks_only_destination_and_can_clear(policy_db, delay):
    policy = SignalDestinationPolicy(policy_db)
    failure = SignalDeliveryFailure(
        sanitize_signal_failure(
            rpc_code=-5,
            error_data={
                "results": [
                    {
                        "type": "RATE_LIMIT_FAILURE",
                        "retryAfterSeconds": delay,
                        "token": "secret-not-for-storage",
                    }
                ]
            },
        )
    )
    send = AsyncMock(side_effect=failure)
    with pytest.raises(SignalDeliveryFailure):
        await policy.send("owner@example.com", "account-1", "chat-1", send)
    policy = SignalDestinationPolicy(policy_db)  # durable across service recreation
    info = await policy.inspect("owner@example.com", "account-1", "chat-1")
    assert info["state"] == ("cooldown" if delay else "manual_action_required")
    assert info["platform_scope"] == "unknown"
    assert "secret-not-for-storage" not in str(info)
    with pytest.raises(SignalPolicyBlocked):
        await policy.send("owner@example.com", "account-1", "chat-1", send)
    assert send.await_count == 1
    assert (
        await policy.send(
            "owner@example.com", "account-1", "other-chat", AsyncMock(return_value="receipt")
        )
        == "receipt"
    )
    with pytest.raises(ValueError, match="not found"):
        await policy.clear("other-owner", "account-1", "chat-1", evidence="Checked", actor="owner")
    await policy.clear(
        "owner@example.com",
        "account-1",
        "chat-1",
        evidence="Reconciled externally",
        actor="controller",
    )
    info = await policy.inspect("owner@example.com", "account-1", "chat-1")
    assert info["state"] == "available"
    assert info["last_failure"]["challenge"]
    assert send.await_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [TimeoutError(), ConnectionError(), None])
async def test_unresolved_transport_is_never_clearable(policy_db, failure):
    policy = SignalDestinationPolicy(policy_db)
    send = AsyncMock(side_effect=failure, return_value="")
    with pytest.raises((TimeoutError, ConnectionError, RuntimeError)):
        await policy.send("owner@example.com", "account-1", "chat-1", send)
    async with policy_db() as session:
        row = await session.get(SignalDestinationPolicyRow, ("account-1", "chat-1"))
        state = dict(row.state_json)
        state["admission"] = {**state["admission"], "deadline": "2000-01-01T00:00:00+00:00"}
        row.state_json = state
        await session.commit()
    info = await policy.inspect("owner@example.com", "account-1", "chat-1")
    assert info["state"] == "transport_outcome_unresolved"
    with pytest.raises(ValueError, match="cannot be cleared"):
        await policy.clear(
            "owner@example.com",
            "account-1",
            "chat-1",
            evidence="Recipient saw nothing",
            actor="owner",
        )
    with pytest.raises(SignalPolicyBlocked):
        await policy.send("owner@example.com", "account-1", "chat-1", send)
    assert send.await_count == 1


@pytest.mark.asyncio
async def test_concurrent_controllers_and_cancellation(policy_db):
    policy = SignalDestinationPolicy(policy_db)
    entered = asyncio.Event()
    wait = asyncio.Event()

    async def send():
        entered.set()
        await wait.wait()
        return "receipt"

    task = asyncio.create_task(policy.send("owner@example.com", "account-1", "chat-1", send))
    await asyncio.wait_for(entered.wait(), 5)
    with pytest.raises(SignalPolicyBlocked):
        await SignalDestinationPolicy(policy_db).send(
            "owner@example.com", "account-1", "chat-1", AsyncMock(return_value="duplicate")
        )
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert (await policy.inspect("owner@example.com", "account-1", "chat-1"))[
        "state"
    ] == "transport_outcome_unresolved"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "obstacle",
    ["none", "other_controller", "stale_epoch", "transport", "send", "lease", "no_evidence"],
)
async def test_early_route_release_preserves_delivery_and_policy(policy_db, obstacle):
    from datetime import UTC, datetime, timedelta

    from sqlalchemy import select

    from cognis.channels.managed import ManagedChannelService
    from cognis.core.turn_scheduler import TurnResult, TurnScheduler
    from cognis.store.models import (
        ChannelDeliveryOutboxRow,
        ManagedChannelBinding,
        ManagedConversationLink,
    )

    async with policy_db() as session:
        link = (await session.execute(select(ManagedConversationLink))).scalar_one()
    scheduler = object.__new__(TurnScheduler)
    scheduler._session_factory = policy_db
    await scheduler._notify_managed_turn_result(
        TurnResult(
            conversation_id=link.target_conversation_id,
            session_id="target-session",
            message_id="assistant-final",
            turn_id="turn-final",
            final_content="Public final",
        )
    )
    async with policy_db() as session:
        binding = (await session.execute(select(ManagedChannelBinding))).scalar_one()
        row = (await session.execute(select(ChannelDeliveryOutboxRow))).scalar_one()
        binding.state = "delivery_failed"
        binding.last_error = "external_send_outcome_uncertain"
        binding.expires_at = datetime.now(UTC) + timedelta(days=30)
        row.status = "sending" if obstacle == "send" else "uncertain"
        if obstacle == "lease":
            binding.delivery_lease_token = "active-lease"
            binding.delivery_lease_expires_at = datetime.now(UTC) + timedelta(minutes=2)
        state = {"manual_action_required": True}
        if obstacle == "transport":
            state["admission"] = {"unresolved": True}
        session.add(
            SignalDestinationPolicyRow(
                account_id="account-1",
                destination="chat-1",
                user_email=link.user_email,
                state_json=state,
            )
        )
        await session.commit()
    result = await ManagedChannelService(policy_db).recover_expired_delivery_failure(
        target_conversation_id=link.target_conversation_id,
        user_email=link.user_email,
        expected_owner_epoch=link.owner_epoch + (obstacle == "stale_epoch"),
        actor_agent_id=link.controller_agent_id,
        actor_conversation_id=(
            "different-controller"
            if obstacle == "other_controller"
            else link.controller_conversation_id
        ),
        actor_session_id=link.controller_session_id,
        reason="External reconciliation",
        reconciliation_evidence=None
        if obstacle == "no_evidence"
        else "Recipient confirmed no arrival",
    )
    assert result.status == (
        "released"
        if obstacle == "none"
        else "not_found"
        if obstacle == "other_controller"
        else "conflict"
        if obstacle == "stale_epoch"
        else "not_eligible"
    )
    async with policy_db() as session:
        binding = (await session.execute(select(ManagedChannelBinding))).scalar_one()
        row = (await session.execute(select(ChannelDeliveryOutboxRow))).scalar_one()
        policy = await session.get(SignalDestinationPolicyRow, ("account-1", "chat-1"))
        assert policy.state_json == state
        assert row.status == ("sending" if obstacle == "send" else "uncertain")
        assert (binding.active_route_key is None) == (obstacle == "none")
        if obstacle == "none":
            assert result.audit["delivery_retried"] is False
            assert result.audit["held_messages_replayed"] is False
            assert result.audit["outcome_uncertain"] is True


@pytest.mark.asyncio
async def test_recipient_route_releases_database_lock_before_signal_admission(policy_db):
    from types import SimpleNamespace

    from sqlalchemy import update

    from cognis.channels.delivery import ChannelDeliveryService, ChannelDeliveryStatus
    from cognis.channels.manager import ChannelManager
    from cognis.core.events import EventBus
    from cognis.models.channel import ChannelAccountConfig, ChannelCapabilities
    from cognis.store.models import ChannelAccountRow

    async with policy_db() as session:
        await session.execute(update(ChannelAccountRow).values(allow_new_conversations=True))
        await session.commit()
    config = ChannelAccountConfig(
        account_id="account-1",
        channel_type="signal",
        user_email="owner@example.com",
        agent_id="target",
        display_name="Test",
    )
    manager = ChannelManager(
        session_factory=policy_db,
        inbound_pipeline=AsyncMock(),
        secrets_provider=AsyncMock(),
        artifact_store=None,
        event_bus=EventBus(),
    )
    manager._configs["account-1"] = config
    transport = SimpleNamespace(
        channel_type="signal", send_message=AsyncMock(return_value="receipt")
    )

    async def send(message):
        # Actual manager admission after the recipient route check.
        return await manager._invoke_adapter_operation(
            "account-1", transport, transport.send_message, (message,), {}
        )

    guarded = SimpleNamespace(send_message=send, capabilities=ChannelCapabilities())
    service = ChannelDeliveryService(
        session_factory=policy_db,
        event_bus=EventBus(),
        channel_manager_ref=lambda: SimpleNamespace(
            find_adapter_for_channel=lambda *_: (guarded, config)
        ),
    )
    async with asyncio.timeout(5):
        result = await service._send_to_route(
            channel_type="signal",
            account_id="account-1",
            chat_id="recipient-chat",
            thread_id=None,
            content="Test",
            delivery_owner_email="owner@example.com",
            reject_active_managed_binding=True,
        )
    assert result == ChannelDeliveryStatus.SENT
    assert transport.send_message.await_count == 1


@pytest.mark.asyncio
async def test_migration_matches_bootstrap_schema(policy_db):
    import importlib

    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    from sqlalchemy import inspect

    migration = importlib.import_module(
        "cognis.store.migrations.versions.149_signal_destination_policy"
    )

    def check(connection):
        def shape():
            inspector = inspect(connection)
            return (
                [
                    (c["name"], str(c["type"]), c["nullable"])
                    for c in inspector.get_columns("signal_destination_policies")
                ],
                inspector.get_pk_constraint("signal_destination_policies")["constrained_columns"],
            )

        baseline = shape()
        with Operations.context(MigrationContext.configure(connection)):
            migration.downgrade()
            migration.upgrade()
            migration.upgrade()
        assert shape() == baseline

    async with policy_db() as session:
        connection = await session.connection()
        await connection.run_sync(check)
        await session.commit()


def test_reconciliation_descriptor_alternatives():
    from jsonschema import Draft202012Validator

    from cognis.tools.builtin.channels import channel_tools
    from cognis.tools.builtin.orchestration import AGENT_CONVERSATION_RECOVER_CHANNEL_TOOL

    validator = Draft202012Validator(AGENT_CONVERSATION_RECOVER_CHANNEL_TOOL.parameters)
    managed = {
        "conversation_id": "managed-target",
        "expected_owner_epoch": 1,
        "reason": "Reconciled",
    }
    validator.validate(managed)
    validator.validate({**managed, "reconciliation_evidence": "External confirmation"})
    assert list(
        validator.iter_errors(
            {
                "delivery_id": "one-shot",
                "reason": "Reconciled",
                "reconciliation_evidence": "Not supported for this alternative",
            }
        )
    )
    assert list(validator.iter_errors({**managed, "reconciliation_evidence": ""}))
    assert any(tool.name == "reconcile_signal_destination_policy" for tool in channel_tools())
