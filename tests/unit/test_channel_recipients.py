from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import inspect, select, text

from cognis.bootstrap import run_schema_bootstrap
from cognis.channels.delivery import ChannelDeliveryService
from cognis.channels.recipients import (
    RecipientNormalizationError,
    RecipientResolutionService,
    normalize_recipient,
)
from cognis.channels.target_refs import ChannelTargetRef, ChannelTargetRefCodec
from cognis.core.events import EventBus
from cognis.models.channel import (
    ChannelCapabilities,
    ChannelRecipient,
    ChannelRecipientCapabilities,
    ChannelRecipientResult,
    ResolvedChannelTarget,
)
from cognis.models.tool import ExecutorHandle
from cognis.store.database import create_engine, create_session_factory
from cognis.store.models import ChannelDeliveryOutboxRow, ChannelRecipientIntentRow
from cognis.store.queries import (
    claim_channel_recipient_intent,
    create_agent,
    create_artifact_record,
    create_channel_account,
    create_conversation,
    create_user,
    get_channel_observed_target,
    promote_channel_recipient_target_on_receipt,
)
from cognis.tools.builtin.channels import SEND_CHANNEL_MESSAGE_TOOL, build_channel_tool_handlers
from cognis.tools.registry import ToolExecutionContext


def _context() -> ToolExecutionContext:
    return ToolExecutionContext(
        executor_handle=ExecutorHandle(executor_id="test", executor_type="in_process"),
        runtime_metadata={
            "runtime_access": {
                "user_email": "owner@example.com",
                "conversation_id": "conv-owner",
                "agent_id": "agent-owner",
            }
        },
    )


def test_recipient_normalization_is_strict_and_channel_specific() -> None:
    recipient = normalize_recipient(
        ChannelRecipient(channel_type="Signal", address=" +420123456789 ")
    )
    assert recipient.channel_type == "signal"
    assert recipient.address == "+420123456789"
    assert recipient.address_kind == "signal_e164"
    assert recipient.chat_kind == "direct"

    signal_uuid = normalize_recipient(
        ChannelRecipient(
            channel_type="signal",
            address="A8098C1A-F86E-11DA-BD1A-00112444BE1E",
            address_kind="signal_uuid",
        )
    )
    assert signal_uuid.address == "a8098c1a-f86e-11da-bd1a-00112444be1e"

    signal_group = normalize_recipient(
        ChannelRecipient(
            channel_type="signal",
            address="A" * 43 + "=",
            address_kind="signal_group_id",
            chat_kind="group",
        )
    )
    assert signal_group.chat_kind == "group"

    workspace_user = normalize_recipient(
        ChannelRecipient(
            channel_type="google_chat",
            address="users/123456789",
            address_kind="google_workspace_user",
        )
    )
    assert workspace_user.address == "users/123456789"
    with pytest.raises(RecipientNormalizationError):
        normalize_recipient(
            ChannelRecipient(
                channel_type="google_chat",
                address="users/person@example.com",
                address_kind="google_workspace_user",
            )
        )

    for address in ("+420123456789\n", "tel:+420123456789", "not-an-id"):
        with pytest.raises(RecipientNormalizationError):
            normalize_recipient(
                ChannelRecipient(
                    channel_type="signal",
                    address=address,
                    address_kind="signal_e164",
                )
            )

    with pytest.raises(RecipientNormalizationError, match="chat kind"):
        normalize_recipient(
            ChannelRecipient(
                channel_type="signal",
                address="+420123456789",
                address_kind="signal_e164",
                chat_kind="group",
            )
        )


def test_send_tool_exposes_exact_recipient_union() -> None:
    one_of = SEND_CHANNEL_MESSAGE_TOOL.parameters["oneOf"]
    assert len(one_of) == 2
    assert one_of[0]["required"] == ["target_ref"]
    assert one_of[1]["required"] == ["recipient"]


def test_resolved_target_is_rpc_serializable_but_tool_result_is_safe() -> None:
    target = ResolvedChannelTarget(
        channel_type="signal",
        account_id="owned-account",
        chat_id="+420123456789",
        chat_kind="direct",
        thread_id="thread-1",
    )
    payload = target.model_dump(mode="json")
    assert payload["account_id"] == "owned-account"
    assert payload["chat_id"] == "+420123456789"
    assert payload["thread_id"] == "thread-1"

    safe = ChannelRecipientResult(status="queued", delivery_id="delivery-1")
    assert "chat_id" not in safe.model_dump(mode="json")
    assert "+420123456789" not in json.dumps(safe.model_dump(mode="json"))


def test_provisional_route_key_is_keyed_and_domain_separated() -> None:
    from cognis.channels.recipients import _provisional_route_key

    recipient = ChannelRecipient(
        channel_type="signal",
        address="+420123456789",
        address_kind="signal_e164",
        chat_kind="direct",
    )
    first = _provisional_route_key(
        ChannelTargetRefCodec("route-secret-a"),
        user_email="owner@example.com",
        account_id="account-1",
        recipient=recipient,
    )
    second = _provisional_route_key(
        ChannelTargetRefCodec("route-secret-b"),
        user_email="owner@example.com",
        account_id="account-1",
        recipient=recipient,
    )
    assert first != second
    assert "+420123456789" not in first


@pytest.mark.asyncio
async def test_account_selection_is_owned_and_signal_recipient_is_not_bot_account(tmp_path) -> None:
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path / 'account-selection.db'}")
    factory = create_session_factory(engine)
    await run_schema_bootstrap(engine)
    try:
        async with factory() as session:
            await create_user(session, "owner@example.com", "Owner", "hash")
            await create_agent(
                session,
                agent_id="agent-owner",
                owner_email="owner@example.com",
                name="Owner",
                status="active",
            )
            await create_channel_account(
                session,
                account_id="signal-one",
                channel_type="signal",
                display_name="Signal One",
                agent_id="agent-owner",
                user_email="owner@example.com",
                config={"account_number": "+420999888777"},
            )
            await session.commit()
        service = RecipientResolutionService(
            factory,
            codec=ChannelTargetRefCodec("selection-secret"),
        )
        result = await service.send(
            user_email="owner@example.com",
            recipient=ChannelRecipient(
                channel_type="signal",
                address="+420123456789",
            ),
            content="Hello",
            artifact_metadata=[],
            idempotency_key="different-recipient",
            conversation_id="conversation-1",
        )
        assert result.status == "queued"
        async with factory() as session:
            await create_channel_account(
                session,
                account_id="signal-two",
                channel_type="signal",
                display_name="Signal Two",
                agent_id="agent-owner",
                user_email="owner@example.com",
                config={"account_number": "+420111222333"},
            )
            await session.commit()
        ambiguous = await service.send(
            user_email="owner@example.com",
            recipient=ChannelRecipient(
                channel_type="signal",
                address="+420123456789",
            ),
            content="Hello",
            artifact_metadata=[],
            idempotency_key="ambiguous-recipient",
            conversation_id="conversation-1",
        )
        assert ambiguous.error is not None
        assert ambiguous.error.code == "account_ambiguous"
        replay = await service.send(
            user_email="owner@example.com",
            recipient=ChannelRecipient(
                channel_type="signal",
                address="+420123456789",
            ),
            content="Hello",
            artifact_metadata=[],
            idempotency_key="different-recipient",
            conversation_id="conversation-1",
        )
        assert replay.status == "queued"
        async with factory() as session:
            intent = await session.get(ChannelRecipientIntentRow, replay.intent_id)
            assert intent is not None
            assert intent.account_id == "signal-one"

        changed_gate = await service.send(
            user_email="owner@example.com",
            recipient=ChannelRecipient(
                channel_type="signal",
                address="+420123456789",
                allow_resolution=True,
            ),
            content="Hello",
            artifact_metadata=[],
            idempotency_key="different-recipient",
            conversation_id="conversation-1",
        )
        assert changed_gate.status == "conflict"
        scoped = await service.send(
            user_email="owner@example.com",
            recipient=ChannelRecipient(
                channel_type="signal",
                address="+420123456789",
                account_ref=ChannelTargetRefCodec("selection-secret").encode(
                    ChannelTargetRef(
                        kind="account",
                        user_email="owner@example.com",
                        account_id="signal-one",
                        channel_type="signal",
                    )
                ),
            ),
            content="Hello",
            artifact_metadata=[],
            idempotency_key="different-recipient",
            conversation_id="conversation-1",
            idempotency_scope="another-scope",
        )
        assert scoped.status == "queued"
        assert scoped.intent_id != replay.intent_id
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_canonical_signal_recipient_does_not_require_local_adapter(tmp_path) -> None:
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path / 'signal-nonowner.db'}")
    factory = create_session_factory(engine)
    await run_schema_bootstrap(engine)

    class _NonOwnerManager:
        def get_adapter(self, _account_id: str) -> None:
            return None

    try:
        async with factory() as session:
            await create_user(session, "owner@example.com", "Owner", "hash")
            await create_agent(
                session,
                agent_id="agent-owner",
                owner_email="owner@example.com",
                name="Owner",
                status="active",
            )
            await create_channel_account(
                session,
                account_id="signal-one",
                channel_type="signal",
                display_name="Signal One",
                agent_id="agent-owner",
                user_email="owner@example.com",
                config={"account_number": "+420999888777"},
            )
            await session.commit()
        service = RecipientResolutionService(
            factory,
            codec=ChannelTargetRefCodec("nonowner-secret"),
            channel_manager_ref=lambda: _NonOwnerManager(),
        )

        result = await service.send(
            user_email="owner@example.com",
            recipient=ChannelRecipient(
                channel_type="signal",
                address="+420728720962",
                address_kind="signal_e164",
                chat_kind="direct",
                allow_resolution=True,
                allow_creation=True,
            ),
            content="Hello",
            artifact_metadata=[],
            idempotency_key="signal-nonowner",
            conversation_id="conversation-1",
        )

        assert result.status == "queued"
        assert result.intent_id is not None
        assert result.delivery_id is not None
    finally:
        await engine.dispose()


class _ResolutionAdapter:
    def __init__(self, capabilities: ChannelCapabilities) -> None:
        self.capabilities = capabilities
        self.calls: list[ChannelRecipient] = []

    async def resolve_recipient(
        self, recipient: ChannelRecipient, *, resolution_key: str
    ) -> ResolvedChannelTarget:
        self.calls.append(recipient)
        return ResolvedChannelTarget(
            channel_type=recipient.channel_type,
            account_id="telegram-account",
            chat_id="-100123456789",
            chat_kind="group",
        )


class _ResolutionManager:
    def __init__(self, adapter: _ResolutionAdapter) -> None:
        self.adapter = adapter

    def get_adapter(self, account_id: str) -> _ResolutionAdapter:
        assert account_id == "telegram-account"
        return self.adapter


@pytest.mark.asyncio
async def test_adapter_resolution_creation_capability_and_restart_recovery(tmp_path) -> None:
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path / 'recovery.db'}")
    factory = create_session_factory(engine)
    await run_schema_bootstrap(engine)
    try:
        async with factory() as session:
            await create_user(session, "owner@example.com", "Owner", "hash")
            await create_agent(
                session,
                agent_id="agent-owner",
                owner_email="owner@example.com",
                name="Owner",
                status="active",
            )
            await create_channel_account(
                session,
                account_id="telegram-account",
                channel_type="telegram",
                display_name="Telegram",
                agent_id="agent-owner",
                user_email="owner@example.com",
            )
            await session.commit()
        capabilities = ChannelCapabilities(
            recipient_capabilities=ChannelRecipientCapabilities(
                address_kinds=["telegram_public_username"],
                chat_kinds=["group"],
                supports_resolution=True,
                supports_creation=True,
            )
        )
        adapter = _ResolutionAdapter(capabilities)
        service = RecipientResolutionService(
            factory,
            codec=ChannelTargetRefCodec("recovery-secret"),
            channel_manager_ref=lambda: _ResolutionManager(adapter),
        )
        result = await service.send(
            user_email="owner@example.com",
            recipient=ChannelRecipient(
                channel_type="telegram",
                address="@public_group",
                address_kind="telegram_public_username",
                chat_kind="group",
                allow_creation=True,
            ),
            content="Recover me",
            artifact_metadata=[],
            idempotency_key="recovery-1",
            conversation_id="conversation-1",
        )
        assert result.status == "queued"
        assert len(adapter.calls) == 1
        async with factory() as session:
            intent = await session.get(ChannelRecipientIntentRow, result.intent_id)
            assert intent is not None
            assert intent.content == "Recover me"
            assert intent.conversation_id == "conversation-1"
            assert intent.payload_json["idempotency_key"] == "recovery-1"
            outbox = await session.get(ChannelDeliveryOutboxRow, result.delivery_id)
            assert outbox is not None
            await session.delete(outbox)
            await session.commit()
        assert await service.recover_pending() == 1
        assert len(adapter.calls) == 1

        async with factory() as session:
            intent = await session.get(ChannelRecipientIntentRow, result.intent_id)
            assert intent is not None
            intent.resolution_state = "pending"
            intent.resolution_lease_token = None
            intent.resolution_lease_expires_at = None
            await session.commit()
        async with factory() as first, factory() as second:
            first_claim = await claim_channel_recipient_intent(
                first,
                intent_id=result.intent_id,
                lease_token="lease-one",
                lease_expires_at=datetime.now(UTC) + timedelta(minutes=2),
                side_effect_certainty="none",
            )
            await first.commit()
            second_claim = await claim_channel_recipient_intent(
                second,
                intent_id=result.intent_id,
                lease_token="lease-two",
                lease_expires_at=datetime.now(UTC) + timedelta(minutes=2),
                side_effect_certainty="none",
            )
            assert first_claim is not None
            assert second_claim is None
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_signal_recipient_persists_intent_without_exposing_address(tmp_path) -> None:
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path / 'recipients.db'}")
    factory = create_session_factory(engine)
    await run_schema_bootstrap(engine)
    try:
        async with factory() as session:
            await create_user(session, "owner@example.com", "Owner", "hash")
            await create_agent(
                session,
                agent_id="agent-owner",
                owner_email="owner@example.com",
                name="Owner",
                status="active",
            )
            await create_channel_account(
                session,
                account_id="signal-owner",
                channel_type="signal",
                display_name="Signal",
                agent_id="agent-owner",
                user_email="owner@example.com",
                config={"account_number": "+420999888777"},
            )
            await session.commit()

        handlers = build_channel_tool_handlers(
            factory,
            application_secret="stable-application-secret",
        )
        result = await handlers["send_channel_message"](
            {
                "recipient": {
                    "channel_type": "signal",
                    "address": "+420123456789",
                },
                "content": "Hello",
                "idempotency_key": "recipient-1",
            },
            _context(),
        )
        assert result["status"] == "queued"
        assert result["target_ref"] is None
        encoded = json.dumps(result)
        assert "+420123456789" not in encoded

        async with factory() as session:
            intent = await session.get(ChannelRecipientIntentRow, result["intent_id"])
            assert intent is not None
            assert intent.normalized_address == "+420123456789"
        assert intent.provisional_route_key not in encoded
        replay = await handlers["send_channel_message"](
            {
                "recipient": {
                    "channel_type": "signal",
                    "address": "+420123456789",
                },
                "content": "Hello",
                "idempotency_key": "recipient-1",
            },
            _context(),
        )
        assert replay["delivery_id"] == result["delivery_id"]
        conflict = await handlers["send_channel_message"](
            {
                "recipient": {
                    "channel_type": "signal",
                    "address": "+420123456789",
                },
                "content": "Changed",
                "idempotency_key": "recipient-1",
            },
            _context(),
        )
        assert conflict.is_error is True
        async with engine.begin() as connection:
            tables = await connection.run_sync(
                lambda sync_conn: set(inspect(sync_conn).get_table_names())
            )
        assert "channel_recipient_intents" in tables

        async with factory() as session:
            outbox = await session.get(ChannelDeliveryOutboxRow, result["delivery_id"])
            assert outbox is not None
            assert (
                await promote_channel_recipient_target_on_receipt(
                    session, delivery_id=outbox.delivery_id
                )
                is None
            )
            outbox.completed_chunk_count = 1
            target = await promote_channel_recipient_target_on_receipt(
                session, delivery_id=outbox.delivery_id
            )
            assert target is not None
            await session.commit()

        async with factory() as session:
            observed = await get_channel_observed_target(
                session,
                user_email="owner@example.com",
                account_id="signal-owner",
                chat_id="+420123456789",
            )
            assert observed is not None
            assert observed.sender_id == "+420123456789"
        delivery = await handlers["get_channel_delivery"](
            {"delivery_id": result["delivery_id"]}, _context()
        )
        assert isinstance(delivery["target_ref"], str)
        assert "+420123456789" not in json.dumps(delivery)
        decoded = ChannelTargetRefCodec("stable-application-secret").decode(
            delivery["target_ref"],
            user_email="owner@example.com",
            expected_kind="target",
        )
        assert decoded.sender_id == "+420123456789"
        discovered = await handlers["search_channel_targets"](
            {"query": "+420123456789", "kinds": ["direct"], "limit": 10},
            _context(),
        )
        assert len(discovered["targets"]) == 1
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_recipient_artifact_delivery_survives_restart_grant_and_promotes_on_receipt(
    tmp_path,
) -> None:
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path / 'recipient-artifact.db'}")
    factory = create_session_factory(engine)
    await run_schema_bootstrap(engine)
    try:
        async with factory() as session:
            await create_user(session, "owner@example.com", "Owner", "hash")
            await create_agent(
                session,
                agent_id="agent-owner",
                owner_email="owner@example.com",
                name="Owner",
                status="active",
            )
            await create_conversation(
                session,
                user_email="owner@example.com",
                agent_id="agent-owner",
                context_type="web",
                context_ref="web:owner@example.com:default",
                context_data={},
                title="Owner",
                conversation_id="conv-owner",
            )
            await create_channel_account(
                session,
                account_id="signal-owner",
                channel_type="signal",
                display_name="Signal",
                agent_id="agent-owner",
                user_email="owner@example.com",
            )
            await create_artifact_record(
                session,
                artifact_id="artifact-report",
                namespace="artifacts",
                object_id="artifact-report",
                filename="report.txt",
                owner_email="owner@example.com",
                purpose="chat_input",
                kind="file",
                mime_type="text/plain",
                size_bytes=6,
                status="attached",
                conversation_id="conv-owner",
            )
            await session.commit()

        handlers = build_channel_tool_handlers(
            factory,
            application_secret="artifact-recipient-secret",
        )
        result = await handlers["send_channel_message"](
            {
                "recipient": {
                    "channel_type": "signal",
                    "address": "+420123456789",
                },
                "content": "First\n\nSecond",
                "artifact_ids": ["artifact-report"],
                "idempotency_key": "artifact-recipient-1",
            },
            _context(),
        )
        assert result["status"] == "queued"

        async with factory() as session:
            intent = await session.get(ChannelRecipientIntentRow, result["intent_id"])
            assert intent is not None
            assert intent.authorized_artifacts_json
            assert intent.authorized_artifacts_json[0]["_delivery_authorization"]["scope"] == (
                "conversation"
            )
            assert (
                intent.authorized_artifacts_json[0]["_delivery_authorization"][
                    "accessor_conversation_id"
                ]
                == "conv-owner"
            )
            outbox = await session.get(ChannelDeliveryOutboxRow, result["delivery_id"])
            assert outbox is not None
            assert outbox.attachments_json == intent.authorized_artifacts_json
            assert (
                await get_channel_observed_target(
                    session,
                    user_email="owner@example.com",
                    account_id="signal-owner",
                    chat_id="+420123456789",
                )
                is None
            )
            await session.delete(outbox)
            await session.commit()

        restarted_recipient_service = RecipientResolutionService(
            factory,
            codec=ChannelTargetRefCodec("artifact-recipient-secret"),
        )
        assert await restarted_recipient_service.recover_pending() == 1

        class _ArtifactStore:
            async def async_load(self, *_args: object) -> tuple[bytes, str]:
                return b"report", "text/plain"

        class _Adapter:
            capabilities = ChannelCapabilities(
                supports_idempotent_send=True,
                max_message_length=10,
            )

            def __init__(self) -> None:
                self.calls: list[object] = []

            async def send_message(self, message: object) -> str:
                self.calls.append(message)
                return "signal-message-1"

        adapter = _Adapter()

        class _Manager:
            _artifact_store = _ArtifactStore()

            def find_adapter_for_channel(
                self, _channel_type: str, _account_id: str
            ) -> tuple[_Adapter, object]:
                return adapter, object()

        delivery_service = ChannelDeliveryService(
            session_factory=factory,
            event_bus=EventBus(),
            channel_manager_ref=lambda: _Manager(),
        )
        async with factory() as session:
            outbox = (
                await session.execute(
                    select(ChannelDeliveryOutboxRow).where(
                        ChannelDeliveryOutboxRow.source_type == "channel_recipient"
                    )
                )
            ).scalar_one()
            delivery_id = outbox.delivery_id
        await delivery_service._deliver_outbox(  # noqa: SLF001
            delivery_id=delivery_id,
            final_content=None,
            fallback_text=None,
            ignore_next_attempt=True,
        )

        assert len(adapter.calls) == 2
        async with factory() as session:
            delivered = await session.get(ChannelDeliveryOutboxRow, delivery_id)
            assert delivered is not None
            assert delivered.status == "sent"
            assert delivered.completed_chunk_count == 2
            observed = await get_channel_observed_target(
                session,
                user_email="owner@example.com",
                account_id="signal-owner",
                chat_id="+420123456789",
            )
            assert observed is not None
        delivery = await handlers["get_channel_delivery"]({"delivery_id": delivery_id}, _context())
        assert isinstance(delivery["target_ref"], str)
        assert "_delivery_authorization" not in json.dumps(delivery)
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_bootstrap_adds_new_intent_payload_columns_twice(tmp_path) -> None:
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path / 'legacy-intents.db'}")
    try:
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    """
                    CREATE TABLE channel_recipient_intents (
                        intent_id VARCHAR PRIMARY KEY,
                        user_email VARCHAR NOT NULL,
                        account_id VARCHAR NOT NULL,
                        channel_type VARCHAR NOT NULL,
                        address_kind VARCHAR NOT NULL,
                        normalized_address VARCHAR NOT NULL,
                        chat_kind VARCHAR NOT NULL,
                        allow_resolution BOOLEAN NOT NULL,
                        allow_creation BOOLEAN NOT NULL,
                        provisional_route_key VARCHAR NOT NULL,
                        fingerprint VARCHAR NOT NULL,
                        authorized_artifacts_json JSON,
                        resolution_lease_token VARCHAR,
                        resolution_lease_expires_at TIMESTAMP,
                        resolution_state VARCHAR NOT NULL,
                        attempt_count INTEGER NOT NULL,
                        side_effect_certainty VARCHAR NOT NULL,
                        resolved_route_json JSON,
                        safe_error_json JSON,
                        created_at TIMESTAMP NOT NULL,
                        updated_at TIMESTAMP NOT NULL
                    )
                    """
                )
            )
        await run_schema_bootstrap(engine)
        await run_schema_bootstrap(engine)
        async with engine.begin() as connection:
            columns = await connection.run_sync(
                lambda sync_conn: {
                    item["name"]
                    for item in inspect(sync_conn).get_columns("channel_recipient_intents")
                }
            )
        assert {
            "content",
            "conversation_id",
            "idempotency_key",
            "idempotency_scope",
            "payload_json",
        } <= columns
    finally:
        await engine.dispose()
