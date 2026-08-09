from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import delete, event, select

import cognis.channels.managed as managed_channel_handlers
import cognis.channels.route_admission as route_admission
import cognis.tools.builtin.channels as channel_tool_handlers
from cognis.bootstrap import run_schema_bootstrap
from cognis.channels.bindings import (
    ActiveManagedChannelBinding,
    DatabaseManagedChannelBindingLookup,
)
from cognis.channels.observed_targets import DatabaseObservedTargetRecorder
from cognis.channels.target_refs import ChannelTargetRef, ChannelTargetRefCodec
from cognis.core.agent_loop import AgentLoop
from cognis.core.tool_output_store import FilesystemToolOutputBackend, ToolOutputStore
from cognis.models.channel import ChannelAccountConfig, InboundMessage
from cognis.models.tool import ExecutorHandle
from cognis.store.database import create_engine, create_session_factory
from cognis.store.models import (
    ArtifactRecordRow,
    ChannelAccountRow,
    ChannelDeliveryOutboxRow,
    ChannelDeliveryReceiptRow,
    ChannelInboundLedgerRow,
    ChannelObservedTargetRow,
    Conversation,
    ManagedChannelBinding,
    ManagedConversationLink,
)
from cognis.store.queries import (
    complete_managed_channel_conversation,
    create_agent,
    create_channel_account,
    create_channel_delivery_outbox,
    create_conversation,
    create_managed_conversation_link,
    create_user,
    get_channel_delivery_outbox,
    upsert_channel_observed_target,
)
from cognis.tools.builtin.channels import build_channel_tool_handlers
from cognis.tools.builtin.tool_output import handle_tool_output_tool
from cognis.tools.introspection import audit_tool_descriptors
from cognis.tools.registry import ToolExecutionContext


def test_public_channel_handlers_share_route_race_serialization_primitive() -> None:
    assert managed_channel_handlers.lock_channel_route is route_admission.lock_channel_route
    assert channel_tool_handlers.lock_channel_route is route_admission.lock_channel_route
    assert (
        managed_channel_handlers.active_channel_tool_delivery_id
        is route_admission.active_channel_tool_delivery_id
    )
    assert (
        channel_tool_handlers.active_managed_binding_id is route_admission.active_managed_binding_id
    )


def test_channel_tools_have_consistent_native_descriptors() -> None:
    assert audit_tool_descriptors(channel_tool_handlers.channel_tools()) == []


class _BindingLookup:
    def __init__(self, binding: ActiveManagedChannelBinding | None = None) -> None:
        self.binding = binding

    async def find_active_binding(
        self,
        *,
        user_email: str,
        account_id: str,
        chat_id: str,
        thread_id: str | None = None,
    ) -> ActiveManagedChannelBinding | None:
        assert user_email == "owner@example.com"
        assert account_id == "account-owner"
        assert chat_id == "chat-direct"
        assert thread_id is None
        return self.binding


def _context(
    user_email: str = "owner@example.com",
    *,
    conversation_id: str | None = None,
) -> ToolExecutionContext:
    return ToolExecutionContext(
        executor_handle=ExecutorHandle(executor_id="test", executor_type="in_process"),
        runtime_metadata={
            "user_email": "spoofed@example.com",
            "runtime_access": {
                "user_email": user_email,
                "conversation_id": (
                    conversation_id
                    or ("conv-other" if user_email == "other@example.com" else "conv-owner")
                ),
                "agent_id": ("agent-other" if user_email == "other@example.com" else "agent-owner"),
            },
        },
    )


async def _setup(
    tmp_path,
    *,
    binding: ActiveManagedChannelBinding | None = None,
):
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path / 'cognis.db'}")
    factory = create_session_factory(engine)
    await run_schema_bootstrap(engine)
    async with factory() as session:
        for email in ("owner@example.com", "other@example.com"):
            await create_user(session, email=email, name=email, password_hash="x", role="user")
            await create_agent(
                session,
                agent_id=f"agent-{email.split('@', 1)[0]}",
                owner_email=email,
                name=email,
                status="active",
            )
        await create_channel_account(
            session,
            account_id="account-owner",
            channel_type="signal",
            display_name="Owner Signal",
            agent_id="agent-owner",
            user_email="owner@example.com",
        )
        await create_channel_account(
            session,
            account_id="account-other",
            channel_type="signal",
            display_name="Other Signal",
            agent_id="agent-other",
            user_email="other@example.com",
        )
        await create_conversation(
            session,
            "owner@example.com",
            "agent-owner",
            "signal",
            title="Filip",
            context_ref="signal:account-owner:chat-direct",
            context_data={
                "account_id": "account-owner",
                "chat_id": "chat-direct",
                "chat_type": "direct",
                "chat_name": "Filip",
            },
            conversation_id="conv-owner",
        )
        await upsert_channel_observed_target(
            session,
            user_email="owner@example.com",
            account_id="account-owner",
            channel_type="signal",
            chat_id="chat-direct",
            chat_kind="direct",
            display_name="Filip",
        )
        await upsert_channel_observed_target(
            session,
            user_email="owner@example.com",
            account_id="account-owner",
            channel_type="signal",
            chat_id="chat-group",
            chat_kind="group",
            display_name="Ops Group",
        )
        await upsert_channel_observed_target(
            session,
            user_email="other@example.com",
            account_id="account-other",
            channel_type="signal",
            chat_id="chat-private",
            chat_kind="direct",
            display_name="Private Other",
        )
        await create_conversation(
            session,
            "owner@example.com",
            "agent-owner",
            "signal",
            title="Ops",
            context_ref="signal:account-owner:chat-group",
            context_data={
                "account_id": "account-owner",
                "chat_id": "chat-group",
                "chat_type": "group",
                "chat_name": "Ops Group",
            },
        )
        await create_conversation(
            session,
            "other@example.com",
            "agent-other",
            "signal",
            title="Private",
            context_ref="signal:account-other:chat-private",
            context_data={
                "account_id": "account-other",
                "chat_id": "chat-private",
                "chat_type": "direct",
                "chat_name": "Private Other",
            },
            conversation_id="conv-other",
        )
        await session.commit()
    handlers = build_channel_tool_handlers(
        factory,
        application_secret="stable-application-secret",
        binding_lookup=_BindingLookup(binding),
    )
    return engine, factory, handlers


async def _seed_artifact(
    factory,
    artifact_id: str,
    *,
    owner_email: str = "owner@example.com",
    mime_type: str = "image/png",
    size_bytes: int = 123,
    conversation_id: str | None = None,
) -> None:
    async with factory() as session:
        session.add(
            ArtifactRecordRow(
                artifact_id=artifact_id,
                namespace="test",
                object_id=f"objects/{artifact_id}",
                filename="photo.png",
                owner_email=owner_email,
                conversation_id=conversation_id,
                purpose="chat_input",
                kind="image",
                mime_type=mime_type,
                size_bytes=size_bytes,
                status="attached",
            )
        )
        await session.commit()


@pytest.mark.asyncio
async def test_account_and_observed_target_search_are_owner_scoped(tmp_path) -> None:
    engine, _factory, handlers = await _setup(tmp_path)
    try:
        accounts = await handlers["list_channel_accounts"]({}, _context())
        assert len(accounts) == 1
        assert accounts[0]["display_name"] == "Owner Signal"
        assert accounts[0]["target_discovery"] == {
            "observed_chats": True,
            "provider_directory": False,
        }
        assert accounts[0]["transport_identity"] == {
            "scope": "channel_account",
            "account_display_name": "Owner Signal",
            "configured_agent_id": "agent-owner",
            "per_message_agent_identity": False,
            "detail": (
                "The provider displays the shared channel account identity. "
                "It does not follow the agent that initiated this message."
            ),
        }
        assert "account-owner" not in str(accounts)

        result = await handlers["search_channel_targets"](
            {"query": "ops", "kinds": ["group"]}, _context()
        )
        assert result["capabilities"]["provider_directory"] is False
        assert result["capabilities"]["observation_status"] == "observed_targets_available"
        assert [target["display_name"] for target in result["targets"]] == ["Ops Group"]
        assert result["targets"][0]["transport_identity"]["per_message_agent_identity"] is False
        assert "chat-group" not in str(result)
        assert "Private Other" not in str(result)
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_database_binding_lookup_blocks_one_shot_delivery(tmp_path) -> None:
    engine, factory, _handlers = await _setup(tmp_path)
    try:
        async with factory() as session:
            controller = await create_conversation(
                session,
                "owner@example.com",
                "agent-owner",
                "web",
                title="Controller",
            )
            target = await create_conversation(
                session,
                "owner@example.com",
                "agent-owner",
                "agent_work",
                title="Managed support",
            )
            link = await create_managed_conversation_link(
                session,
                user_email="owner@example.com",
                controller_agent_id="agent-owner",
                controller_conversation_id=controller.conversation_id,
                controller_session_id="controller-session",
                target_agent_id="agent-owner",
                target_conversation_id=target.conversation_id,
                target_session_id="target-session",
                title="Managed support",
                kind="channel",
                completion_policy="explicit",
            )
            session.add(
                ManagedChannelBinding(
                    binding_id="binding-active",
                    link_id=link.link_id,
                    user_email="owner@example.com",
                    account_id="account-owner",
                    chat_id="chat-direct",
                    thread_key="",
                    sender_id="sender-owner",
                    active_route_key="owner:account-owner:chat-direct:",
                    state="waiting_external",
                    version=1,
                    expires_at=datetime.now(UTC) + timedelta(hours=1),
                    objective="Handle this conversation.",
                    safety_guidance="Do not disclose private data.",
                    explicit_tool_allowlist=[],
                )
            )
            await session.commit()

        lookup = DatabaseManagedChannelBindingLookup(factory)
        handlers = build_channel_tool_handlers(
            factory,
            application_secret="stable-application-secret",
            binding_lookup=lookup,
        )
        targets = await handlers["search_channel_targets"]({"query": "Filip"}, _context())
        result = await handlers["send_channel_message"](
            {
                "target_ref": targets["targets"][0]["target_ref"],
                "content": "Do not send this.",
                "idempotency_key": "blocked-by-binding",
            },
            _context(),
        )
        assert result.is_error is True
        refusal = json.loads(result.output)
        assert refusal["status"] == "active_binding"
        assert refusal["code"] == "channel_route_managed"
        assert refusal["delivery_id"] is None
        assert refusal["content_submitted"] is False
        assert refusal["externally_delivered"] is False
        assert refusal["active_binding"]["conversation_id"] == target.conversation_id
        assert refusal["transport_identity"]["per_message_agent_identity"] is False
        assert (
            await lookup.find_active_binding(
                user_email="other@example.com",
                account_id="account-owner",
                chat_id="chat-direct",
            )
            is None
        )

        async with factory() as session:
            stored_binding = await session.get(ManagedChannelBinding, "binding-active")
            assert stored_binding is not None
            stored_binding.expires_at = datetime.now(UTC) - timedelta(seconds=1)
            await session.commit()
        expired_route = await lookup.find_active_binding(
            user_email="owner@example.com",
            account_id="account-owner",
            chat_id="chat-direct",
        )
        assert expired_route is not None
        assert expired_route.conversation_id == target.conversation_id

        async with factory() as session:
            stored_binding = await session.get(ManagedChannelBinding, "binding-active")
            assert stored_binding is not None
            stored_binding.expires_at = datetime.now(UTC) + timedelta(hours=1)
            stored_binding.state = "completed"
            await session.commit()
        inconsistent_terminal_route = await lookup.find_active_binding(
            user_email="owner@example.com",
            account_id="account-owner",
            chat_id="chat-direct",
        )
        assert inconsistent_terminal_route is not None
        assert inconsistent_terminal_route.status == "completed"

        async with factory() as session:
            stored_binding = await session.get(ManagedChannelBinding, "binding-active")
            assert stored_binding is not None
            stored_binding.state = "waiting_external"
            stored_binding.active_route_key = None
            await session.commit()
        assert (
            await lookup.find_active_binding(
                user_email="owner@example.com",
                account_id="account-owner",
                chat_id="chat-direct",
            )
            is None
        )

        async with factory() as session:
            stored_binding = await session.get(ManagedChannelBinding, "binding-active")
            stored_link = await session.get(ManagedConversationLink, link.link_id)
            assert stored_binding is not None
            assert stored_link is not None
            stored_binding.active_route_key = "owner:account-owner:chat-direct:"
            stored_link.conversation_state = "closed"
            await session.commit()
        assert (
            await lookup.find_active_binding(
                user_email="owner@example.com",
                account_id="account-owner",
                chat_id="chat-direct",
            )
            is None
        )
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_target_ref_cannot_cross_users_or_be_forged(tmp_path) -> None:
    engine, _factory, handlers = await _setup(tmp_path)
    try:
        result = await handlers["search_channel_targets"]({"query": "Filip"}, _context())
        target_ref = result["targets"][0]["target_ref"]
        with pytest.raises(ValueError):
            await handlers["send_channel_message"](
                {
                    "target_ref": target_ref,
                    "content": "Hello",
                    "idempotency_key": "request-1",
                },
                _context("other@example.com"),
            )
        with pytest.raises(ValueError):
            await handlers["send_channel_message"](
                {
                    "target_ref": (
                        target_ref[: len(target_ref) // 2]
                        + ("B" if target_ref[len(target_ref) // 2] == "A" else "A")
                        + target_ref[len(target_ref) // 2 + 1 :]
                    ),
                    "content": "Hello",
                    "idempotency_key": "request-1",
                },
                _context(),
            )
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_managed_child_transcript_ref_is_route_and_conversation_scoped(tmp_path) -> None:
    engine, factory, handlers = await _setup(tmp_path)
    try:
        async with factory() as session:
            await create_conversation(
                session,
                "owner@example.com",
                "agent-owner",
                "agent_work",
                context_data={"managed_conversation_kind": "channel"},
                conversation_id="conv-channel-child",
            )
            await create_conversation(
                session,
                "owner@example.com",
                "agent-owner",
                "agent_work",
                context_data={"managed_conversation_kind": "channel"},
                conversation_id="conv-channel-child-other",
            )
            await session.commit()
        codec = ChannelTargetRefCodec("stable-application-secret")
        transcript_ref = codec.encode(
            ChannelTargetRef(
                kind="transcript",
                user_email="owner@example.com",
                account_id="account-owner",
                channel_type="signal",
                chat_id="chat-direct",
                chat_kind="direct",
                scope_conversation_id="conv-channel-child",
            )
        )
        result = await handlers["read_channel_messages"](
            {"target_ref": transcript_ref},
            _context(conversation_id="conv-channel-child"),
        )
        assert json.loads(result.output)["coverage"] == "cognis_observed"

        with pytest.raises(ValueError, match="not available"):
            await handlers["read_channel_messages"](
                {"target_ref": transcript_ref},
                ToolExecutionContext(
                    executor_handle=ExecutorHandle(
                        executor_id="test",
                        executor_type="in_process",
                    ),
                    runtime_metadata={
                        "runtime_access": {
                            "user_email": "owner@example.com",
                            "conversation_id": "conv-channel-child-other",
                            "agent_id": "agent-owner",
                        }
                    },
                ),
            )

        ordinary_target_ref = (
            await handlers["search_channel_targets"]({"query": "Filip"}, _context())
        )["targets"][0]["target_ref"]
        with pytest.raises(ValueError, match="wrong kind"):
            await handlers["read_channel_messages"](
                {"target_ref": ordinary_target_ref},
                _context(conversation_id="conv-channel-child"),
            )
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_search_targets_reports_no_observed_traffic(tmp_path) -> None:
    engine, factory, handlers = await _setup(tmp_path)
    try:
        async with factory() as session:
            await session.execute(
                delete(ChannelObservedTargetRow).where(
                    ChannelObservedTargetRow.user_email == "owner@example.com"
                )
            )
            await session.commit()
        result = await handlers["search_channel_targets"]({}, _context())
        assert result["targets"] == []
        assert result["capabilities"]["account_count"] == 1
        assert result["capabilities"]["observed_target_count"] == 0
        assert result["capabilities"]["observation_status"] == "no_observed_traffic"
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_search_targets_distinguishes_no_enabled_accounts(tmp_path) -> None:
    engine, factory, handlers = await _setup(tmp_path)
    try:
        async with factory() as session:
            account = await session.get(ChannelAccountRow, "account-owner")
            assert account is not None
            account.enabled = False
            await session.commit()
        result = await handlers["search_channel_targets"]({}, _context())
        assert result["targets"] == []
        assert result["capabilities"]["account_count"] == 0
        assert result["capabilities"]["observed_target_count"] == 0
        assert result["capabilities"]["observation_status"] == "no_enabled_accounts"
        assert "No enabled channel accounts" in result["capabilities"]["detail"]
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_channel_shaped_conversation_does_not_create_observed_target(tmp_path) -> None:
    engine, factory, handlers = await _setup(tmp_path)
    try:
        async with factory() as session:
            await create_conversation(
                session,
                "owner@example.com",
                "agent-owner",
                "signal",
                title="Forged",
                context_ref="signal:account-owner:chat-forged",
                context_data={
                    "account_id": "account-owner",
                    "chat_id": "chat-forged",
                    "chat_type": "direct",
                    "chat_name": "Forged",
                },
            )
            await session.commit()
        result = await handlers["search_channel_targets"]({"query": "Forged"}, _context())
        assert result["targets"] == []
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_inbound_recorder_creates_searchable_target(tmp_path) -> None:
    engine, factory, handlers = await _setup(tmp_path)
    try:
        recorder = DatabaseObservedTargetRecorder(factory)
        await recorder.record(
            InboundMessage(
                channel_type="signal",
                account_id="account-owner",
                message_id="message-new",
                sender_id="sender-new",
                sender_name="New Contact",
                chat_id="chat-new",
                chat_type="direct",
                content="Hello",
                timestamp=datetime.now(UTC),
            ),
            ChannelAccountConfig(
                account_id="account-owner",
                channel_type="signal",
                display_name="Owner Signal",
                agent_id="agent-owner",
                user_email="owner@example.com",
            ),
        )
        result = await handlers["search_channel_targets"]({"query": "new contact"}, _context())
        assert [target["display_name"] for target in result["targets"]] == ["New Contact"]
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_send_enqueues_idempotently_without_creating_conversation(tmp_path) -> None:
    engine, factory, handlers = await _setup(tmp_path)
    try:
        targets = await handlers["search_channel_targets"]({"query": "Filip"}, _context())
        arguments = {
            "target_ref": targets["targets"][0]["target_ref"],
            "content": "Hello",
            "idempotency_key": "request-1",
        }
        first = await handlers["send_channel_message"](arguments, _context())
        second = await handlers["send_channel_message"](arguments, _context())
        assert first["delivery_id"] == second["delivery_id"]
        assert first["status"] == "pending"
        assert first["created"] is True
        assert second["created"] is False

        async with factory() as session:
            row = await get_channel_delivery_outbox(session, first["delivery_id"])
            assert row is not None
            assert row.source_type == "channel_tool_message"
            assert row.fallback_text == "Hello"
            assert row.conversation_id.startswith("__channel_tool_delivery__:")
            assert await session.get(Conversation, row.conversation_id) is None

        with pytest.raises(ValueError, match="conflicts"):
            await handlers["send_channel_message"](
                {**arguments, "content": "Different"}, _context()
            )
        async with factory() as session:
            with pytest.raises(ValueError, match="reserved internal namespace"):
                await create_conversation(
                    session,
                    "owner@example.com",
                    "agent-owner",
                    "web",
                    conversation_id="__channel_tool_delivery__:collision",
                )
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_delivery_lookup_reports_status_failure_and_owner_scope(tmp_path) -> None:
    engine, factory, handlers = await _setup(tmp_path)
    try:
        targets = await handlers["search_channel_targets"]({"query": "Filip"}, _context())
        queued = await handlers["send_channel_message"](
            {
                "target_ref": targets["targets"][0]["target_ref"],
                "content": "Hello",
                "idempotency_key": "request-status",
            },
            _context(),
        )
        async with factory() as session:
            row = await session.get(ChannelDeliveryOutboxRow, queued["delivery_id"])
            assert row is not None
            row.status = "failed"
            row.attempt_count = 2
            row.last_error = "adapter unavailable"
            row.updated_at = datetime.now(UTC)
            await session.commit()

        status = await handlers["get_channel_delivery"](
            {"delivery_id": queued["delivery_id"]}, _context()
        )
        assert status["status"] == "failed"
        assert status["attempt_count"] == 2
        assert status["last_error"] == "adapter unavailable"
        assert "account-owner" not in str(status)
        with pytest.raises(ValueError, match="not found"):
            await handlers["get_channel_delivery"](
                {"delivery_id": queued["delivery_id"]}, _context("other@example.com")
            )
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_one_shot_picture_only_artifact_and_idempotency(tmp_path) -> None:
    engine, factory, handlers = await _setup(tmp_path)
    try:
        await _seed_artifact(factory, "img-owned")
        await _seed_artifact(factory, "img-other")
        target = (await handlers["search_channel_targets"]({"query": "Filip"}, _context()))[
            "targets"
        ][0]["target_ref"]
        first = await handlers["send_channel_message"](
            {
                "target_ref": target,
                "artifact_ids": ["img-owned"],
                "idempotency_key": "picture-only",
            },
            _context(),
        )
        assert first["created"] is True
        assert first["attachments"] == [
            {
                "artifact_id": "img-owned",
                "kind": "image",
                "mime_type": "image/png",
                "filename": "photo.png",
                "size_bytes": 123,
            }
        ]
        async with factory() as session:
            row = await session.get(ChannelDeliveryOutboxRow, first["delivery_id"])
            assert row is not None
            assert row.attachments_json[0]["_delivery_authorization"]["scope"] == "owner_global"
            stored = await session.get(ArtifactRecordRow, "img-owned")
            assert stored is not None
            stored.status = "deleted"
            stored.deleted_at = datetime.now(UTC)
            await session.commit()
        replay = await handlers["send_channel_message"](
            {
                "target_ref": target,
                "artifact_ids": ["img-owned"],
                "idempotency_key": "picture-only",
            },
            _context(),
        )
        assert replay["created"] is False
        with pytest.raises(ValueError, match="idempotency"):
            await handlers["send_channel_message"](
                {
                    "target_ref": target,
                    "artifact_ids": ["img-other"],
                    "idempotency_key": "picture-only",
                },
                _context(),
            )
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_one_shot_rejects_missing_and_cross_user_artifacts(tmp_path) -> None:
    engine, factory, handlers = await _setup(tmp_path)
    try:
        await _seed_artifact(factory, "img-private", owner_email="other@example.com")
        target = (await handlers["search_channel_targets"]({"query": "Filip"}, _context()))[
            "targets"
        ][0]["target_ref"]
        with pytest.raises(ValueError, match="unavailable"):
            await handlers["send_channel_message"](
                {
                    "target_ref": target,
                    "artifact_ids": ["img-private"],
                    "idempotency_key": "private",
                },
                _context(),
            )
        with pytest.raises(ValueError, match="unavailable"):
            await handlers["send_channel_message"](
                {
                    "target_ref": target,
                    "artifact_ids": ["missing"],
                    "idempotency_key": "missing",
                },
                _context(),
            )
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_one_shot_enforces_mime_and_byte_limits_before_enqueue(tmp_path) -> None:
    engine, factory, handlers = await _setup(tmp_path)
    try:
        await _seed_artifact(
            factory,
            "unsupported",
            mime_type="application/octet-stream",
        )
        await _seed_artifact(factory, "too-large", size_bytes=25 * 1024 * 1024 + 1)
        for index in range(3):
            await _seed_artifact(factory, f"aggregate-{index}", size_bytes=20 * 1024 * 1024)
        target = (await handlers["search_channel_targets"]({"query": "Filip"}, _context()))[
            "targets"
        ][0]["target_ref"]
        for artifact_ids in (
            ["unsupported"],
            ["too-large"],
            ["aggregate-0", "aggregate-1", "aggregate-2"],
        ):
            with pytest.raises(ValueError):
                await handlers["send_channel_message"](
                    {
                        "target_ref": target,
                        "artifact_ids": artifact_ids,
                        "idempotency_key": "-".join(artifact_ids),
                    },
                    _context(),
                )
        async with factory() as session:
            assert (await session.execute(select(ChannelDeliveryOutboxRow))).scalars().all() == []
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_read_channel_messages_paginates_and_emits_safe_output_anchors(
    tmp_path,
) -> None:
    engine, factory, handlers = await _setup(tmp_path)
    try:
        targets = await handlers["search_channel_targets"]({"query": "Filip"}, _context())
        target_ref = targets["targets"][0]["target_ref"]
        base = datetime(2026, 1, 1, tzinfo=UTC)
        async with factory() as session:
            session.add_all(
                [
                    ChannelInboundLedgerRow(
                        inbound_id=f"chin-{index}",
                        user_email="owner@example.com",
                        account_id="account-owner",
                        binding_id=None,
                        channel_type="signal",
                        chat_id="chat-direct",
                        thread_key="",
                        message_id=f"message-{index}",
                        sender_id="participant",
                        sender_name=("Filip\nOwner" if index == 1 else "Filip"),
                        occurred_at=base + timedelta(seconds=index),
                        observed_at=base,
                        ordering_key=f"{index}",
                        ordering_source="observed",
                        content=(
                            ("first line\n" + ("x" * 5000)) if index == 1 else f"message {index}"
                        ),
                        is_bot_output=False,
                        is_primary_input=index == 0,
                        disposition="admitted",
                        platform_data=(
                            {
                                "safe_attachments": [
                                    {
                                        "artifact_id": "img-inbound-history",
                                        "kind": "image",
                                        "mime_type": "image/png",
                                        "filename": "inbound.png",
                                        "size_bytes": 77,
                                    }
                                ]
                            }
                            if index == 2
                            else {}
                        ),
                    )
                    for index in range(3)
                ]
            )
            session.add(
                ChannelInboundLedgerRow(
                    inbound_id="chin-expired",
                    user_email="owner@example.com",
                    account_id="account-owner",
                    binding_id=None,
                    channel_type="signal",
                    chat_id="chat-direct",
                    thread_key="",
                    message_id="message-expired",
                    sender_id="participant",
                    sender_name="Filip",
                    occurred_at=base - timedelta(seconds=1),
                    observed_at=base,
                    ordering_key="expired",
                    ordering_source="observed",
                    retain_until=base,
                    content="expired private context",
                    is_bot_output=False,
                    is_primary_input=False,
                    disposition="context",
                    platform_data={},
                )
            )
            outbound = await create_channel_delivery_outbox(
                session,
                delivery_id="cdel-direct-history",
                user_email="owner@example.com",
                conversation_id="conv-direct-history",
                session_id="sess-direct-history",
                source_type="direct_turn_result",
                source_id="turn-direct-history",
                channel_type="signal",
                account_id="account-owner",
                chat_id="chat-direct",
                thread_id=None,
                fallback_text="assistant direct",
                attachments=[
                    {
                        "artifact_id": "img-history",
                        "kind": "image",
                        "mime_type": "image/png",
                        "filename": "history.png",
                        "size_bytes": 456,
                    }
                ],
                next_attempt_at=base,
            )
            outbound.created_at = base + timedelta(seconds=3)
            outbound.status = "sent"
            outbound.completed_chunk_count = 1
            outbound.projected_chunk_count = 1
            outbound.first_delivered_at = base + timedelta(seconds=3)
            outbound.last_delivered_at = base + timedelta(seconds=3)
            outbound.delivery_receipts_json = [
                {
                    "chunk_index": 0,
                    "content": "assistant direct",
                    "sent_at": (base + timedelta(seconds=3)).isoformat(),
                    "external_message_id": "external-direct",
                    "attachments_delivered": True,
                    "attachments": outbound.attachments_json,
                }
            ]
            session.add(
                ChannelDeliveryReceiptRow(
                    delivery_id=outbound.delivery_id,
                    chunk_index=0,
                    sent_at=base + timedelta(seconds=3),
                    content="assistant direct",
                    external_message_id="external-direct",
                    attachments_json=outbound.attachments_json,
                )
            )
            await session.commit()
        first = await handlers["read_channel_messages"](
            {
                "target_ref": target_ref,
                "limit": 2,
                "anchor": {"kind": "from_start"},
                "since": base.isoformat(),
                "until": (base + timedelta(seconds=3)).isoformat(),
            },
            _context(),
        )
        preview = json.loads(first.output)
        assert [item["message_ref"] for item in preview["messages"]] == [
            "inbound:chin-0",
            "inbound:chin-1",
        ]
        assert preview["messages"][1]["content_truncation"]["truncated"] is True
        assert preview["page"]["next_cursor"]
        assert len(first.metadata["output_anchors"]) == 2
        anchor = first.metadata["output_anchors"][1]
        stored_lines = first.metadata["stored_output"].splitlines()
        section = "\n".join(stored_lines[anchor["start_line"] - 1 : anchor["end_line"]])
        assert "inbound:chin-1" in section
        assert "inbound:chin-0" not in section
        assert "\\nOwner" in section
        assert "first line\\n" in section

        output_store = ToolOutputStore(
            FilesystemToolOutputBackend(tmp_path / "tool-output"),
            ttl_hours=1,
            max_size_mb=1,
        )
        agent_loop = object.__new__(AgentLoop)
        agent_loop.tool_output_store = output_store
        persisted, persisted_anchors = await agent_loop._save_tool_output_if_available(
            "call_channel_history",
            first,
            "read_channel_messages",
        )
        assert persisted is True
        persisted_anchor_names = {item["anchor"] for item in persisted_anchors}
        assert {
            "message:inbound:chin-0",
            "message:inbound:chin-1",
        } <= persisted_anchor_names
        listed = await handle_tool_output_tool(
            "list_tool_output_anchors",
            {"call_id": "call_channel_history"},
            output_store,
        )
        assert "message:inbound:chin-1" in listed.output
        recovered = await handle_tool_output_tool(
            "read_tool_output_anchor",
            {
                "call_id": "call_channel_history",
                "anchor": "message:inbound:chin-1",
                "before_lines": 0,
                "after_lines": 0,
            },
            output_store,
        )
        assert "inbound:chin-1" in recovered.output
        assert "inbound:chin-0" not in recovered.output
        assert '"attachments":' not in recovered.output
        assert "Attachments: []" in recovered.output
        assert "first line\\n" in recovered.output
        second = await handlers["read_channel_messages"](
            {
                "target_ref": target_ref,
                "limit": 2,
                "cursor": preview["page"]["next_cursor"],
                "since": base.isoformat(),
                "until": (base + timedelta(seconds=3)).isoformat(),
            },
            _context(),
        )
        second_messages = json.loads(second.output)["messages"]
        assert [item["message_ref"] for item in second_messages] == [
            "inbound:chin-2",
            "delivery:cdel-direct-history:chunk:0",
        ]
        assert second_messages[1]["attachments"] == [
            {
                "artifact_id": "img-history",
                "kind": "image",
                "mime_type": "image/png",
                "filename": "history.png",
                "size_bytes": 456,
            }
        ]
        assert second_messages[0]["attachments"][0]["artifact_id"] == "img-inbound-history"
        persisted, _ = await agent_loop._save_tool_output_if_available(
            "call_channel_history_next",
            second,
            "read_channel_messages",
        )
        assert persisted is True
        recovered_delivery = await handle_tool_output_tool(
            "read_tool_output_anchor",
            {
                "call_id": "call_channel_history_next",
                "anchor": "message:delivery:cdel-direct-history:chunk:0",
                "before_lines": 0,
                "after_lines": 0,
            },
            output_store,
        )
        assert "assistant direct" in recovered_delivery.output
        assert '"artifact_id": "img-history"' in recovered_delivery.output
        assert "inbound:chin-2" not in recovered_delivery.output
        with pytest.raises(ValueError, match="cursor"):
            await handlers["read_channel_messages"](
                {
                    "target_ref": targets["targets"][0]["target_ref"],
                    "cursor": preview["page"]["next_cursor"],
                    "direction": "outbound",
                },
                _context(),
            )
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_history_excludes_undelivered_attempts_and_represents_partial_sends(
    tmp_path,
) -> None:
    engine, factory, handlers = await _setup(tmp_path)
    try:
        target_ref = (await handlers["search_channel_targets"]({"query": "Filip"}, _context()))[
            "targets"
        ][0]["target_ref"]
        base = datetime(2026, 2, 1, tzinfo=UTC)
        async with factory() as session:
            session.add(
                ChannelInboundLedgerRow(
                    inbound_id="chin-interleaved",
                    user_email="owner@example.com",
                    account_id="account-owner",
                    binding_id=None,
                    channel_type="signal",
                    chat_id="chat-direct",
                    thread_key="",
                    message_id="inbound-interleaved",
                    sender_id="participant",
                    sender_name="Filip",
                    occurred_at=base + timedelta(seconds=2),
                    observed_at=base,
                    ordering_key="2",
                    ordering_source="provider",
                    content="participant observed",
                    is_bot_output=False,
                    is_primary_input=True,
                    disposition="admitted",
                    platform_data={},
                )
            )
            for index, (name, status, receipts) in enumerate(
                [
                    ("pending", "pending", []),
                    ("failed", "failed", []),
                    ("suppressed", "suppressed", []),
                    ("stale", "suppressed", []),
                    ("partial", "failed", ["delivered first chunk"]),
                    ("delayed", "sent", ["delayed delivered"]),
                ],
                start=1,
            ):
                delivered_at = base + timedelta(seconds=index + 2)
                row = await create_channel_delivery_outbox(
                    session,
                    delivery_id=f"cdel-{name}",
                    user_email="owner@example.com",
                    conversation_id="conv-owner",
                    session_id=None,
                    source_type="direct_turn_result",
                    source_id=f"turn-{name}",
                    channel_type="signal",
                    account_id="account-owner",
                    chat_id="chat-direct",
                    thread_id=None,
                    fallback_text=f"intended {name}",
                    attachments=None,
                    next_attempt_at=base,
                )
                row.status = status
                row.updated_at = delivered_at
                row.last_error = "stale_fence" if name == "stale" else None
                if receipts:
                    row.completed_chunk_count = 1
                    row.projected_chunk_count = 2 if name == "partial" else 1
                    row.first_delivered_at = delivered_at
                    row.last_delivered_at = delivered_at
                    row.sent_at = delivered_at if status == "sent" else None
                    row.delivery_receipts_json = [
                        {
                            "chunk_index": 0,
                            "content": receipts[0],
                            "sent_at": delivered_at.isoformat(),
                            "external_message_id": f"external-{name}",
                            "attachments_delivered": False,
                        }
                    ]
                    session.add(
                        ChannelDeliveryReceiptRow(
                            delivery_id=row.delivery_id,
                            chunk_index=0,
                            sent_at=delivered_at,
                            content=receipts[0],
                            external_message_id=f"external-{name}",
                            attachments_json=None,
                        )
                    )
            await session.commit()

        default = await handlers["read_channel_messages"](
            {
                "target_ref": target_ref,
                "anchor": {"kind": "from_start"},
                "since": base.isoformat(),
                "until": (base + timedelta(seconds=20)).isoformat(),
                "limit": 20,
            },
            _context(),
        )
        messages = json.loads(default.output)["messages"]
        assert [item["message_ref"] for item in messages] == [
            "inbound:chin-interleaved",
            "delivery:cdel-partial:chunk:0",
            "delivery:cdel-delayed:chunk:0",
        ]
        assert messages[1]["partial_delivery"] is True
        pending = await handlers["read_channel_messages"](
            {
                "target_ref": target_ref,
                "anchor": {"kind": "from_start"},
                "status": "pending",
                "limit": 20,
            },
            _context(),
        )
        attempts = json.loads(pending.output)["messages"]
        assert [item["message_ref"] for item in attempts] == ["attempt:cdel-pending"]
        assert attempts[0]["record_type"] == "delivery_attempt"
        assert attempts[0]["externally_observed"] is False
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_read_channel_messages_bounds_large_transcript_queries(tmp_path) -> None:
    engine, factory, handlers = await _setup(tmp_path)
    try:
        target_ref = (await handlers["search_channel_targets"]({"query": "Filip"}, _context()))[
            "targets"
        ][0]["target_ref"]
        base = datetime(2026, 3, 1, tzinfo=UTC)
        async with factory() as session:
            session.add_all(
                [
                    ChannelInboundLedgerRow(
                        inbound_id=f"chin-large-{index:04d}",
                        user_email="owner@example.com",
                        account_id="account-owner",
                        binding_id=None,
                        channel_type="signal",
                        chat_id="chat-direct",
                        thread_key="",
                        message_id=f"large-{index:04d}",
                        sender_id="participant",
                        sender_name="Filip",
                        occurred_at=base + timedelta(seconds=index),
                        observed_at=base,
                        ordering_key=str(index),
                        ordering_source="provider",
                        content=f"message {index}",
                        is_bot_output=False,
                        is_primary_input=True,
                        disposition="admitted",
                        platform_data={},
                    )
                    for index in range(500)
                ]
            )
            await session.commit()
        transcript_selects: list[str] = []

        def _record_statement(
            _connection,
            _cursor,
            statement,
            _parameters,
            _context,
            _executemany,
        ) -> None:
            lowered = statement.casefold()
            if "channel_inbound_ledger" in lowered or "channel_delivery_outbox" in lowered:
                transcript_selects.append(lowered)

        event.listen(engine.sync_engine, "before_cursor_execute", _record_statement)
        try:
            result = await handlers["read_channel_messages"](
                {
                    "target_ref": target_ref,
                    "anchor": {"kind": "from_start"},
                    "limit": 5,
                },
                _context(),
            )
        finally:
            event.remove(engine.sync_engine, "before_cursor_execute", _record_statement)
        assert len(json.loads(result.output)["messages"]) == 5
        assert len(transcript_selects) == 2
        assert all(" limit " in statement for statement in transcript_selects)
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_read_channel_messages_keyset_covers_equal_timestamps(tmp_path) -> None:
    engine, factory, handlers = await _setup(tmp_path)
    try:
        target_ref = (await handlers["search_channel_targets"]({"query": "Filip"}, _context()))[
            "targets"
        ][0]["target_ref"]
        timestamp = datetime(2026, 3, 2, tzinfo=UTC)
        async with factory() as session:
            session.add_all(
                [
                    ChannelInboundLedgerRow(
                        inbound_id=f"chin-equal-{index:04d}",
                        user_email="owner@example.com",
                        account_id="account-owner",
                        binding_id=None,
                        channel_type="signal",
                        chat_id="chat-direct",
                        thread_key="",
                        message_id=f"equal-{index:04d}",
                        sender_id="participant",
                        sender_name="Filip",
                        occurred_at=timestamp,
                        observed_at=timestamp,
                        ordering_key=str(index),
                        ordering_source="provider",
                        content=f"equal {index}",
                        is_bot_output=False,
                        is_primary_input=True,
                        disposition="admitted",
                        platform_data={},
                    )
                    for index in range(500)
                ]
            )
            await session.commit()
        cursor = None
        refs: list[str] = []
        while True:
            arguments: dict[str, object] = {
                "target_ref": target_ref,
                "limit": 100,
                "direction": "inbound",
            }
            if cursor is None:
                arguments["anchor"] = {"kind": "from_start"}
            else:
                arguments["cursor"] = cursor
            result = await handlers["read_channel_messages"](arguments, _context())
            payload = json.loads(result.output)
            refs.extend(
                item["anchor"].removeprefix("message:")
                for item in result.metadata["output_anchors"]
                if item["kind"] == "channel_message"
            )
            cursor = payload["page"]["next_cursor"]
            if cursor is None:
                break
        assert len(refs) == 500
        assert len(set(refs)) == 500
        assert refs == sorted(refs)
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_read_channel_messages_filters_each_delivered_chunk_time(tmp_path) -> None:
    engine, factory, handlers = await _setup(tmp_path)
    try:
        target_ref = (await handlers["search_channel_targets"]({"query": "Filip"}, _context()))[
            "targets"
        ][0]["target_ref"]
        base = datetime(2026, 3, 3, tzinfo=UTC)
        async with factory() as session:
            row = await create_channel_delivery_outbox(
                session,
                delivery_id="cdel-multipart-time",
                user_email="owner@example.com",
                conversation_id="conv-owner",
                session_id=None,
                source_type="direct_turn_result",
                source_id="turn-multipart-time",
                channel_type="signal",
                account_id="account-owner",
                chat_id="chat-direct",
                thread_id=None,
                fallback_text="first\nsecond",
                attachments=None,
                next_attempt_at=base,
            )
            row.status = "sent"
            row.completed_chunk_count = 2
            row.projected_chunk_count = 2
            row.first_delivered_at = base + timedelta(seconds=10)
            row.last_delivered_at = base + timedelta(seconds=20)
            row.sent_at = base + timedelta(seconds=20)
            for index, seconds in enumerate((10, 20)):
                session.add(
                    ChannelDeliveryReceiptRow(
                        delivery_id=row.delivery_id,
                        chunk_index=index,
                        sent_at=base + timedelta(seconds=seconds),
                        content=f"chunk {index}",
                        external_message_id=f"external-{index}",
                        attachments_json=None,
                    )
                )
            await session.commit()
        result = await handlers["read_channel_messages"](
            {
                "target_ref": target_ref,
                "direction": "outbound",
                "anchor": {"kind": "from_start"},
                "since": (base + timedelta(seconds=5)).isoformat(),
                "until": (base + timedelta(seconds=15)).isoformat(),
                "limit": 10,
            },
            _context(),
        )
        assert [item["content"] for item in json.loads(result.output)["messages"]] == ["chunk 0"]
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_active_binding_prevents_outbox_enqueue(tmp_path) -> None:
    binding = ActiveManagedChannelBinding(
        conversation_id="conv-managed",
        agent_id="agent-owner",
        title="Managed",
        status="active",
    )
    engine, factory, handlers = await _setup(tmp_path, binding=binding)
    try:
        targets = await handlers["search_channel_targets"]({"query": "Filip"}, _context())
        result = await handlers["send_channel_message"](
            {
                "target_ref": targets["targets"][0]["target_ref"],
                "content": "Hello",
                "idempotency_key": "request-binding",
            },
            _context(),
        )
        assert result.is_error is True
        refusal = json.loads(result.output)
        assert refusal["status"] == "active_binding"
        assert refusal["code"] == "channel_route_managed"
        assert refusal["active_binding"]["conversation_id"] == "conv-managed"
        async with factory() as session:
            rows = (await session.execute(select(ChannelDeliveryOutboxRow))).scalars().all()
            assert rows == []
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_one_shot_refuses_active_binding_then_sends_after_close(tmp_path) -> None:
    engine, factory, _handlers = await _setup(tmp_path)
    try:
        async with factory() as session:
            controller = await create_conversation(
                session,
                "owner@example.com",
                "agent-owner",
                "web",
            )
            target = await create_conversation(
                session,
                "owner@example.com",
                "agent-owner",
                "agent_work",
            )
            link = await create_managed_conversation_link(
                session,
                user_email="owner@example.com",
                controller_agent_id="agent-owner",
                controller_conversation_id=controller.conversation_id,
                controller_session_id="controller-session",
                target_agent_id="agent-owner",
                target_conversation_id=target.conversation_id,
                target_session_id="target-session",
                title="Managed route",
                kind="channel",
                completion_policy="explicit",
            )
            session.add(
                ManagedChannelBinding(
                    binding_id="binding-close-send",
                    link_id=link.link_id,
                    user_email="owner@example.com",
                    account_id="account-owner",
                    channel_type="signal",
                    chat_id="chat-direct",
                    thread_key="",
                    sender_id="sender-owner",
                    active_route_key="route-close-send",
                    state="delivery_sent",
                    version=2,
                    expires_at=datetime.now(UTC) + timedelta(hours=1),
                    objective="Handle this route.",
                    safety_guidance="Keep controller context private.",
                    explicit_tool_allowlist=[],
                )
            )
            await session.commit()

        handlers = build_channel_tool_handlers(
            factory,
            application_secret="stable-application-secret",
            binding_lookup=DatabaseManagedChannelBindingLookup(factory),
        )
        target_ref = (await handlers["search_channel_targets"]({"query": "Filip"}, _context()))[
            "targets"
        ][0]["target_ref"]
        refused = await handlers["send_channel_message"](
            {
                "target_ref": target_ref,
                "content": "Blocked",
                "idempotency_key": "before-close",
            },
            _context(),
        )
        assert refused.is_error is True
        refusal = json.loads(refused.output)
        assert refusal["code"] == "channel_route_managed"
        assert refusal["active_binding"]["conversation_id"] == target.conversation_id

        async with factory() as session:
            closed = await complete_managed_channel_conversation(
                session,
                link_id=link.link_id,
                owner_epoch=link.owner_epoch,
                status="cancelled",
                summary="Close route",
            )
            await session.commit()
            assert closed is not None

        sent = await handlers["send_channel_message"](
            {
                "target_ref": target_ref,
                "content": "Independent one-shot",
                "idempotency_key": "after-close",
            },
            _context(),
        )
        assert isinstance(sent, dict)
        assert sent["created"] is True
        assert sent["delivery_id"].startswith("cdel_")
        assert sent["active_binding"] is None
    finally:
        await engine.dispose()
