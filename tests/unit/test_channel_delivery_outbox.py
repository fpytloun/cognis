from __future__ import annotations

import asyncio
import hashlib
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine as create_sync_engine
from sqlalchemy import inspect

from cognis.channels import delivery as delivery_module
from cognis.channels.delivery import (
    ChannelDeliveryService,
    ChannelDeliveryStatus,
    ChannelProjection,
)
from cognis.core.events import Event, EventBus, EventType
from cognis.models.channel import ChannelCapabilities, MediaAttachment, OutboundMessage
from cognis.store.database import create_engine, create_session_factory
from cognis.store.models import Base
from cognis.store.queries import (
    claim_channel_delivery_outbox,
    create_agent,
    create_artifact_record,
    create_channel_delivery_outbox,
    create_conversation,
    create_managed_conversation_link,
    create_user,
    ensure_follow_up_result_delivery,
    get_channel_delivery_outbox,
    get_channel_delivery_outbox_for_source,
    list_channel_delivery_outbox_stale_sending,
    mark_channel_delivery_chunk_inflight,
    mark_channel_delivery_chunk_sent,
    mark_channel_delivery_sent,
    recover_stale_channel_delivery,
    renew_channel_delivery_lease,
)


class _Manager:
    def __init__(self, adapter: object) -> None:
        self.adapter = adapter
        self._artifact_store = None

    def find_adapter_for_channel(
        self,
        channel_type: str,
        account_id: str,
    ) -> tuple[object, object]:
        del channel_type, account_id
        return self.adapter, object()


async def _database(tmp_path: Path, name: str = "delivery.db") -> tuple[Any, Any]:
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path / name}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = create_session_factory(engine)
    async with factory() as session:
        await create_user(
            session,
            email="user@example.com",
            name="User",
            password_hash="hash",
            role="user",
        )
        await session.commit()
    return engine, factory


async def _outbox(factory: Any, delivery_id: str = "cdel-real") -> None:
    async with factory() as session:
        await create_channel_delivery_outbox(
            session,
            delivery_id=delivery_id,
            user_email="user@example.com",
            conversation_id="conv-real",
            session_id=None,
            source_type="task",
            source_id="task-real",
            channel_type="matrix",
            account_id="acct-real",
            chat_id="room-real",
            thread_id=None,
            fallback_text="fallback",
            next_attempt_at=datetime.now(UTC),
        )
        await session.commit()


async def _matrix_follow_up(factory: Any, *, delivery_id: str) -> str:
    async with factory() as session:
        await create_agent(
            session,
            agent_id="agent-follow-up",
            owner_email="user@example.com",
            name="Agent",
        )
        conversation = await create_conversation(
            session,
            user_email="user@example.com",
            agent_id="agent-follow-up",
            context_type="matrix",
            context_ref="matrix:acct-follow-up:room-follow-up",
            context_data={
                "channel_type": "matrix",
                "account_id": "acct-follow-up",
                "chat_id": "room-follow-up",
                "thread_id": "thread-follow-up",
            },
            title="Matrix follow-up",
        )
        await create_channel_delivery_outbox(
            session,
            delivery_id=delivery_id,
            user_email="user@example.com",
            conversation_id=conversation.conversation_id,
            session_id="sess-follow-up",
            source_type="follow_up",
            source_id="fup-real",
            channel_type="matrix",
            account_id="acct-follow-up",
            chat_id="room-follow-up",
            thread_id="thread-follow-up",
            fallback_text="Agent work finished. Open the conversation for details.",
            next_attempt_at=datetime.now(UTC) + timedelta(minutes=2),
        )
        await session.commit()
        return conversation.conversation_id


def _successful_route_sender(calls: list[dict[str, Any]]) -> Any:
    async def send(**kwargs: Any) -> ChannelDeliveryStatus:
        calls.append(kwargs)
        on_chunk_start = kwargs["on_chunk_start"]
        on_chunk_sent = kwargs["on_chunk_sent"]
        assert await on_chunk_start(0, 1, f"digest-{len(calls)}", True)
        assert await on_chunk_sent(1, 1, f"digest-{len(calls)}")
        return ChannelDeliveryStatus.SENT

    return send


def test_channel_delivery_migrations_apply_progress_and_inflight_columns(tmp_path: Path) -> None:
    database_path = tmp_path / "migrations.db"
    config = Config("cognis/store/migrations/alembic.ini")
    config.set_main_option("sqlalchemy.url", f"sqlite:///{database_path}")
    # Alembic's logging fileConfig disables existing application loggers by
    # default, which would leak across the rest of this pytest process.
    config.config_file_name = None

    command.upgrade(config, "head")
    revision = ScriptDirectory.from_config(config).get_revision("095_channel_delivery_dlv_id")
    assert revision is not None and len(revision.revision) <= 32

    columns = {
        column["name"]
        for column in inspect(create_sync_engine(f"sqlite:///{database_path}")).get_columns(
            "channel_delivery_outbox"
        )
    }
    assert {
        "completed_chunk_count",
        "projected_chunk_count",
        "projection_digest",
        "inflight_chunk_index",
        "inflight_idempotent",
        "attachments_json",
        "deliverable_id",
    } <= columns


@pytest.mark.asyncio
async def test_progress_is_monotonic_and_completion_requires_all_chunks(
    tmp_path: Path,
) -> None:
    engine, factory = await _database(tmp_path)
    try:
        await _outbox(factory)
        now = datetime.now(UTC)
        async with factory() as session:
            row = await claim_channel_delivery_outbox(
                session,
                delivery_id="cdel-real",
                lease_token="lease-real",
                lease_expires_at=now + timedelta(minutes=2),
            )
            assert row is not None
            assert await mark_channel_delivery_chunk_inflight(
                session,
                delivery_id="cdel-real",
                lease_token="lease-real",
                chunk_index=0,
                projected_chunk_count=2,
                projection_digest="stable",
                idempotent=True,
                lease_expires_at=now + timedelta(minutes=2),
            )
            assert await mark_channel_delivery_chunk_sent(
                session,
                delivery_id="cdel-real",
                lease_token="lease-real",
                completed_chunk_count=1,
                projected_chunk_count=2,
                projection_digest="stable",
                lease_expires_at=now + timedelta(minutes=2),
            )
            assert not await mark_channel_delivery_chunk_sent(
                session,
                delivery_id="cdel-real",
                lease_token="lease-real",
                completed_chunk_count=1,
                projected_chunk_count=2,
                projection_digest="stable",
                lease_expires_at=now + timedelta(minutes=2),
            )
            assert not await mark_channel_delivery_sent(
                session,
                delivery_id="cdel-real",
                lease_token="lease-real",
                require_complete_chunks=True,
            )
            assert await mark_channel_delivery_chunk_inflight(
                session,
                delivery_id="cdel-real",
                lease_token="lease-real",
                chunk_index=1,
                projected_chunk_count=2,
                projection_digest="stable",
                idempotent=True,
                lease_expires_at=now + timedelta(minutes=2),
            )
            assert await mark_channel_delivery_chunk_sent(
                session,
                delivery_id="cdel-real",
                lease_token="lease-real",
                completed_chunk_count=2,
                projected_chunk_count=2,
                projection_digest="stable",
                lease_expires_at=now + timedelta(minutes=2),
            )
            assert await mark_channel_delivery_sent(
                session,
                delivery_id="cdel-real",
                lease_token="lease-real",
                require_complete_chunks=True,
            )
            await session.commit()
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_lease_renewal_prevents_active_delivery_from_becoming_stale(
    tmp_path: Path,
) -> None:
    engine, factory = await _database(tmp_path)
    try:
        await _outbox(factory)
        now = datetime.now(UTC)
        async with factory() as session:
            await claim_channel_delivery_outbox(
                session,
                delivery_id="cdel-real",
                lease_token="lease-real",
                lease_expires_at=now - timedelta(seconds=1),
            )
            await session.commit()
        async with factory() as session:
            assert [
                row.delivery_id
                for row in await list_channel_delivery_outbox_stale_sending(session, now=now)
            ] == ["cdel-real"]
            assert await renew_channel_delivery_lease(
                session,
                delivery_id="cdel-real",
                lease_token="lease-real",
                lease_expires_at=now + timedelta(minutes=2),
            )
            await session.commit()
        async with factory() as session:
            assert await list_channel_delivery_outbox_stale_sending(session, now=now) == []
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_stale_recovery_loses_race_to_heartbeat_and_cannot_be_claimed(
    tmp_path: Path,
) -> None:
    engine, factory = await _database(tmp_path)
    try:
        await _outbox(factory)
        now = datetime.now(UTC)
        expired_at = now - timedelta(seconds=1)
        async with factory() as session:
            assert await claim_channel_delivery_outbox(
                session,
                delivery_id="cdel-real",
                lease_token="lease-real",
                lease_expires_at=expired_at,
            )
            await session.commit()
        async with factory() as session:
            observed = (await list_channel_delivery_outbox_stale_sending(session, now=now))[0]
            observed_expiry = observed.lease_expires_at
            assert observed_expiry is not None
        async with factory() as session:
            assert await renew_channel_delivery_lease(
                session,
                delivery_id="cdel-real",
                lease_token="lease-real",
                lease_expires_at=now + timedelta(minutes=2),
            )
            await session.commit()
        async with factory() as session:
            assert (
                await recover_stale_channel_delivery(
                    session,
                    delivery_id="cdel-real",
                    observed_lease_token="lease-real",
                    observed_lease_expires_at=observed_expiry,
                    observed_inflight_chunk_index=None,
                    observed_inflight_idempotent=None,
                    now=now,
                )
                is None
            )
            assert (
                await claim_channel_delivery_outbox(
                    session,
                    delivery_id="cdel-real",
                    lease_token="lease-second",
                    lease_expires_at=now + timedelta(minutes=2),
                    ignore_next_attempt=True,
                )
                is None
            )
            current = await get_channel_delivery_outbox(session, "cdel-real")
            assert current is not None
            assert current.status == "sending"
            assert current.lease_token == "lease-real"
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_stale_recovery_loses_race_to_inflight_persistence(
    tmp_path: Path,
) -> None:
    engine, factory = await _database(tmp_path)
    try:
        await _outbox(factory)
        now = datetime.now(UTC)
        expired_at = now - timedelta(seconds=1)
        async with factory() as session:
            assert await claim_channel_delivery_outbox(
                session,
                delivery_id="cdel-real",
                lease_token="lease-real",
                lease_expires_at=expired_at,
            )
            await session.commit()
        async with factory() as session:
            observed = (await list_channel_delivery_outbox_stale_sending(session, now=now))[0]
            observed_expiry = observed.lease_expires_at
            assert observed_expiry is not None
        async with factory() as session:
            assert await mark_channel_delivery_chunk_inflight(
                session,
                delivery_id="cdel-real",
                lease_token="lease-real",
                chunk_index=0,
                projected_chunk_count=1,
                projection_digest="stable",
                idempotent=True,
                lease_expires_at=now + timedelta(minutes=2),
            )
            await session.commit()
        async with factory() as session:
            assert (
                await recover_stale_channel_delivery(
                    session,
                    delivery_id="cdel-real",
                    observed_lease_token="lease-real",
                    observed_lease_expires_at=observed_expiry,
                    observed_inflight_chunk_index=None,
                    observed_inflight_idempotent=None,
                    now=now,
                )
                is None
            )
            current = await get_channel_delivery_outbox(session, "cdel-real")
            assert current is not None
            assert current.status == "sending"
            assert current.inflight_chunk_index == 0
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_multipart_send_heartbeat_renews_lease_while_adapter_is_active(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine, factory = await _database(tmp_path)
    try:
        await _outbox(factory)
        monkeypatch.setattr(
            delivery_module,
            "_DELIVERY_LEASE_DURATION",
            timedelta(milliseconds=30),
        )
        monkeypatch.setattr(
            delivery_module,
            "_DELIVERY_LEASE_HEARTBEAT_SECONDS",
            0.005,
        )
        adapter_started = asyncio.Event()

        class _SlowAdapter:
            capabilities = ChannelCapabilities(max_message_length=200)

            async def send_message(self, message: OutboundMessage) -> str:
                del message
                adapter_started.set()
                await asyncio.sleep(0.08)
                return "message-1"

        service = ChannelDeliveryService(
            session_factory=factory,
            event_bus=EventBus(),
            channel_manager_ref=lambda: _Manager(_SlowAdapter()),
        )
        sending = asyncio.create_task(
            service._deliver_outbox(  # noqa: SLF001
                delivery_id="cdel-real",
                final_content="slow message",
                fallback_text="fallback",
                ignore_next_attempt=True,
            )
        )
        await asyncio.wait_for(adapter_started.wait(), timeout=1)
        await asyncio.sleep(0.05)
        async with factory() as session:
            assert (
                await list_channel_delivery_outbox_stale_sending(
                    session,
                    now=datetime.now(UTC),
                )
                == []
            )
        await sending
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_signed_link_retry_uses_stable_projection_and_resumes_unsent_chunk(
    tmp_path: Path,
) -> None:
    engine, factory = await _database(tmp_path)
    try:
        await _outbox(factory)

        class _Adapter:
            capabilities = ChannelCapabilities(
                supports_markdown=True,
                supports_idempotent_send=True,
                max_message_length=200,
            )

            def __init__(self) -> None:
                self.calls: list[OutboundMessage] = []
                self.fail_link_once = True

            async def send_message(self, message: OutboundMessage) -> str:
                self.calls.append(message)
                if "token=first" in message.content and self.fail_link_once:
                    self.fail_link_once = False
                    raise RuntimeError("lost response")
                return f"event-{len(self.calls)}"

        adapter = _Adapter()
        service = ChannelDeliveryService(
            session_factory=factory,
            event_bus=EventBus(),
            channel_manager_ref=lambda: _Manager(adapter),
        )
        projection_attempt = 0

        async def projection(**kwargs: object) -> ChannelProjection:
            nonlocal projection_attempt
            del kwargs
            projection_attempt += 1
            token = "first" if projection_attempt == 1 else "second"
            return ChannelProjection(
                chunks=["body", f"https://cognis.example/d/dlv-real?token={token}"],
                identity="deliverable:dlv-real:1:content:rich",
            )

        service._deliverable_channel_projection = projection  # type: ignore[method-assign]
        await service._deliver_outbox(  # noqa: SLF001
            delivery_id="cdel-real",
            final_content="fallback",
            fallback_text="fallback",
            deliverable_id="dlv-real",
            ignore_next_attempt=True,
        )
        async with factory() as session:
            first = await get_channel_delivery_outbox(session, "cdel-real")
            assert first is not None
            assert first.status == "failed"
            assert first.completed_chunk_count == 1

        await service._deliver_outbox(  # noqa: SLF001
            delivery_id="cdel-real",
            final_content="fallback",
            fallback_text="fallback",
            deliverable_id="dlv-real",
            ignore_next_attempt=True,
        )

        async with factory() as session:
            final = await get_channel_delivery_outbox(session, "cdel-real")
            assert final is not None
            assert final.status == "sent"
            assert final.completed_chunk_count == 2
        assert [message.content for message in adapter.calls] == [
            "body",
            "https://cognis.example/d/dlv-real?token=first",
            "https://cognis.example/d/dlv-real?token=second",
        ]
        assert (
            adapter.calls[1].platform_data["idempotency_key"]
            == (adapter.calls[2].platform_data["idempotency_key"])
        )
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_non_idempotent_lost_response_becomes_uncertain_and_is_not_retried(
    tmp_path: Path,
) -> None:
    engine, factory = await _database(tmp_path)
    try:
        await _outbox(factory)

        class _Adapter:
            capabilities = ChannelCapabilities(max_message_length=200)

            def __init__(self) -> None:
                self.calls = 0
                self.messages: list[OutboundMessage] = []

            async def send_message(self, message: OutboundMessage) -> str:
                self.calls += 1
                self.messages.append(message)
                raise RuntimeError("server accepted but response was lost")

        adapter = _Adapter()
        service = ChannelDeliveryService(
            session_factory=factory,
            event_bus=EventBus(),
            channel_manager_ref=lambda: _Manager(adapter),
        )
        await service._deliver_outbox(  # noqa: SLF001
            delivery_id="cdel-real",
            final_content="one message",
            fallback_text="fallback",
            ignore_next_attempt=True,
        )
        await service.recover_pending_deliveries()

        async with factory() as session:
            row = await get_channel_delivery_outbox(session, "cdel-real")
            assert row is not None
            assert row.status == "uncertain"
            assert row.inflight_chunk_index == 0
        assert adapter.calls == 1
        assert adapter.messages[0].platform_data == {}
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_restart_recovers_committed_non_idempotent_inflight_as_uncertain(
    tmp_path: Path,
) -> None:
    engine, factory = await _database(tmp_path)
    try:
        await _outbox(factory)
        now = datetime.now(UTC)
        async with factory() as session:
            assert await claim_channel_delivery_outbox(
                session,
                delivery_id="cdel-real",
                lease_token="lease-crashed",
                lease_expires_at=now - timedelta(seconds=1),
            )
            assert await mark_channel_delivery_chunk_inflight(
                session,
                delivery_id="cdel-real",
                lease_token="lease-crashed",
                chunk_index=0,
                projected_chunk_count=1,
                projection_digest="stable",
                idempotent=False,
                lease_expires_at=now - timedelta(seconds=1),
            )
            await session.commit()

        class _Adapter:
            capabilities = ChannelCapabilities(max_message_length=200)

            def __init__(self) -> None:
                self.calls = 0

            async def send_message(self, message: OutboundMessage) -> str:
                del message
                self.calls += 1
                return "unexpected"

        adapter = _Adapter()
        restarted = ChannelDeliveryService(
            session_factory=factory,
            event_bus=EventBus(),
            channel_manager_ref=lambda: _Manager(adapter),
        )
        await restarted.recover_pending_deliveries()

        async with factory() as session:
            row = await get_channel_delivery_outbox(session, "cdel-real")
            assert row is not None
            assert row.status == "uncertain"
            assert row.last_error == "stale_non_idempotent_send"
        assert adapter.calls == 0
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_restart_retries_committed_idempotent_inflight_once(
    tmp_path: Path,
) -> None:
    engine, factory = await _database(tmp_path)
    try:
        await _outbox(factory)
        capabilities = ChannelCapabilities(
            supports_idempotent_send=True,
            max_message_length=200,
        )
        content = "fallback"
        stable_identity = f"text:{hashlib.sha256(content.encode()).hexdigest()}"
        projection_digest = hashlib.sha256(
            f"matrix:{capabilities.model_dump_json()}:{stable_identity}".encode()
        ).hexdigest()
        now = datetime.now(UTC)
        async with factory() as session:
            assert await claim_channel_delivery_outbox(
                session,
                delivery_id="cdel-real",
                lease_token="lease-crashed",
                lease_expires_at=now - timedelta(seconds=1),
            )
            assert await mark_channel_delivery_chunk_inflight(
                session,
                delivery_id="cdel-real",
                lease_token="lease-crashed",
                chunk_index=0,
                projected_chunk_count=1,
                projection_digest=projection_digest,
                idempotent=True,
                lease_expires_at=now - timedelta(seconds=1),
            )
            await session.commit()

        class _Adapter:
            def __init__(self) -> None:
                self.capabilities = capabilities
                self.calls: list[OutboundMessage] = []

            async def send_message(self, message: OutboundMessage) -> str:
                self.calls.append(message)
                return "event-replayed"

        adapter = _Adapter()
        restarted = ChannelDeliveryService(
            session_factory=factory,
            event_bus=EventBus(),
            channel_manager_ref=lambda: _Manager(adapter),
        )
        await restarted.recover_pending_deliveries()

        async with factory() as session:
            row = await get_channel_delivery_outbox(session, "cdel-real")
            assert row is not None
            assert row.status == "sent"
            assert row.completed_chunk_count == 1
        assert len(adapter.calls) == 1
        assert adapter.calls[0].platform_data["idempotency_key"]
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_direct_task_delivery_uses_outbox_and_resumes_partial_failure(
    tmp_path: Path,
) -> None:
    engine, factory = await _database(tmp_path)
    try:
        async with factory() as session:
            await create_agent(
                session,
                agent_id="agent-real",
                owner_email="user@example.com",
                name="Agent",
            )
            conversation = await create_conversation(
                session,
                user_email="user@example.com",
                agent_id="agent-real",
                context_type="matrix",
                context_ref="matrix:acct-real:room-real",
                context_data={
                    "channel_type": "matrix",
                    "account_id": "acct-real",
                    "chat_id": "room-real",
                },
                title="Matrix",
            )
            await session.commit()

        class _Adapter:
            capabilities = ChannelCapabilities(
                supports_idempotent_send=True,
                max_message_length=12,
            )

            def __init__(self) -> None:
                self.calls: list[str] = []
                self.fail_once = True

            async def send_message(self, message: OutboundMessage) -> str:
                self.calls.append(message.content)
                if len(self.calls) == 2 and self.fail_once:
                    self.fail_once = False
                    raise RuntimeError("retry")
                return f"event-{len(self.calls)}"

        adapter = _Adapter()
        service = ChannelDeliveryService(
            session_factory=factory,
            event_bus=EventBus(),
            channel_manager_ref=lambda: _Manager(adapter),
        )
        content = "first block\n\nsecond block\n\nthird block"
        attachments = [
            {
                "filename": "brief.txt",
                "mime_type": "text/plain",
                "content_b64": "YnJpZWY=",
            }
        ]
        first = await service.deliver_task_to_conversation(
            conversation.conversation_id,
            task_id="task-direct-real",
            content=content,
            attachments=attachments,
        )
        second = await service.deliver_task_to_conversation(
            conversation.conversation_id,
            task_id="task-direct-real",
            content=content,
            attachments=attachments,
        )

        assert first == ChannelDeliveryStatus.FAILED
        assert second == ChannelDeliveryStatus.SENT
        assert adapter.calls.count("first block") == 1
        async with factory() as session:
            row = await get_channel_delivery_outbox_for_source(
                session,
                conversation_id=conversation.conversation_id,
                source_type="task_final_result",
                source_id="task-direct-real",
            )
            assert row is not None
            assert row.status == "sent"
            assert row.completed_chunk_count == row.projected_chunk_count
            assert row.attachments_json == attachments
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_outbox_recovery_resumes_text_chunk_after_media_chunk_failure(
    tmp_path: Path,
) -> None:
    engine, factory = await _database(tmp_path)
    try:
        await _outbox(factory, delivery_id="cdel-media-retry")

        class _Adapter:
            capabilities = ChannelCapabilities(
                supports_idempotent_send=True,
                max_message_length=12,
            )

            def __init__(self) -> None:
                self.calls: list[OutboundMessage] = []
                self.fail_once = True

            async def send_message(self, message: OutboundMessage) -> str:
                self.calls.append(message)
                if len(self.calls) == 2 and self.fail_once:
                    self.fail_once = False
                    raise RuntimeError("later text chunk failed")
                return f"event-{len(self.calls)}"

        adapter = _Adapter()
        service = ChannelDeliveryService(
            session_factory=factory,
            event_bus=EventBus(),
            channel_manager_ref=lambda: _Manager(adapter),
        )
        service._prepare_media_attachments = AsyncMock(  # type: ignore[method-assign]
            return_value=([MediaAttachment(filename="report.pdf", content_b64="cGRm")], [], False)
        )
        content = "first block\n\nsecond block\n\nthird block"

        await service._deliver_outbox(  # noqa: SLF001
            delivery_id="cdel-media-retry",
            final_content=content,
            fallback_text=content,
            attachments=[{"artifact_id": "artifact-media"}],
            ignore_next_attempt=True,
        )
        async with factory() as session:
            partial = await get_channel_delivery_outbox(session, "cdel-media-retry")
            assert partial is not None
            assert partial.status == "failed"
            assert partial.completed_chunk_count == 1
            assert partial.inflight_chunk_index == 1
            assert partial.inflight_idempotent is True

        await service._deliver_outbox(  # noqa: SLF001
            delivery_id="cdel-media-retry",
            final_content=content,
            fallback_text=content,
            attachments=None,
            ignore_next_attempt=True,
        )

        async with factory() as session:
            final = await get_channel_delivery_outbox(session, "cdel-media-retry")
            assert final is not None
            assert final.status == "sent"
            assert final.completed_chunk_count == final.projected_chunk_count
        assert [message.content for message in adapter.calls].count("first block") == 1
        assert sum(bool(message.media) for message in adapter.calls) == 1
        assert adapter.calls[1].platform_data["idempotency_key"]
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_unauthorized_outbox_attachment_never_loads_or_sends(
    tmp_path: Path,
) -> None:
    engine, factory = await _database(tmp_path)
    try:
        await _outbox(factory, delivery_id="cdel-denied-artifact")
        async with factory() as session:
            await create_user(
                session,
                email="other@example.com",
                name="Other",
                password_hash="hash",
                role="user",
            )
            await create_artifact_record(
                session,
                artifact_id="artifact-private",
                namespace="artifacts",
                object_id="artifact-private",
                filename="private.pdf",
                owner_email="other@example.com",
                purpose="chat_input",
                kind="file",
                mime_type="application/pdf",
                size_bytes=7,
                status="attached",
            )
            await session.commit()

        class _Store:
            def __init__(self) -> None:
                self.loads = 0

            async def async_load(self, *_args: object) -> tuple[bytes, str]:
                self.loads += 1
                return b"private", "application/pdf"

        class _Adapter:
            capabilities = ChannelCapabilities(supports_idempotent_send=True)

            def __init__(self) -> None:
                self.calls: list[OutboundMessage] = []

            async def send_message(self, message: OutboundMessage) -> str:
                self.calls.append(message)
                return "unexpected"

        store = _Store()
        adapter = _Adapter()
        manager = _Manager(adapter)
        manager._artifact_store = store
        service = ChannelDeliveryService(
            session_factory=factory,
            event_bus=EventBus(),
            channel_manager_ref=lambda: manager,
        )

        await service._deliver_outbox(  # noqa: SLF001
            delivery_id="cdel-denied-artifact",
            final_content="private attachment",
            fallback_text="private attachment",
            attachments=[
                {
                    "artifact_id": "artifact-private",
                    "url": "https://example.invalid/private.pdf",
                    "filename": "private.pdf",
                }
            ],
            ignore_next_attempt=True,
        )

        async with factory() as session:
            row = await get_channel_delivery_outbox(session, "cdel-denied-artifact")
            assert row is not None
            assert row.status == "failed"
            assert row.last_error == "attachment_materialization_incomplete"
        assert store.loads == 0
        assert adapter.calls == []
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_attachment_materialization_allows_direct_and_managed_descendant_artifacts(
    tmp_path: Path,
) -> None:
    engine, factory = await _database(tmp_path)
    try:
        async with factory() as session:
            await create_agent(
                session,
                agent_id="agent-root",
                owner_email="user@example.com",
                name="Root",
            )
            await create_agent(
                session,
                agent_id="agent-child",
                owner_email="user@example.com",
                name="Child",
            )
            root = await create_conversation(
                session,
                user_email="user@example.com",
                agent_id="agent-root",
                context_type="matrix",
                context_ref="matrix:acct:root",
                context_data={},
                title="Root",
            )
            child = await create_conversation(
                session,
                user_email="user@example.com",
                agent_id="agent-child",
                context_type="agent_work",
                context_ref="managed:child",
                context_data={},
                title="Child",
            )
            await create_managed_conversation_link(
                session,
                link_id="mcl-direct-child",
                user_email="user@example.com",
                controller_agent_id="agent-root",
                controller_conversation_id=root.conversation_id,
                controller_session_id="sess-root",
                target_agent_id="agent-child",
                target_conversation_id=child.conversation_id,
                target_session_id="sess-child",
                title="Child",
            )
            for artifact_id, conversation_id in (
                ("artifact-direct", root.conversation_id),
                ("artifact-descendant", child.conversation_id),
            ):
                await create_artifact_record(
                    session,
                    artifact_id=artifact_id,
                    namespace="artifacts",
                    object_id=artifact_id,
                    filename=f"{artifact_id}.txt",
                    owner_email="user@example.com",
                    conversation_id=conversation_id,
                    purpose="chat_input",
                    kind="file",
                    mime_type="text/plain",
                    size_bytes=2,
                    status="attached",
                )
            await session.commit()

        class _Store:
            async def async_load(self, *_args: object) -> tuple[bytes, str]:
                return b"ok", "text/plain"

        manager = _Manager(object())
        manager._artifact_store = _Store()
        service = ChannelDeliveryService(
            session_factory=factory,
            event_bus=EventBus(),
            channel_manager_ref=lambda: manager,
        )
        for artifact_id in ("artifact-direct", "artifact-descendant"):
            media, _fallback, materialized = await service._materialize_media_attachment(  # noqa: SLF001
                {"artifact_id": artifact_id},
                owner_email="user@example.com",
                conversation_id=root.conversation_id,
            )
            assert materialized is True
            assert media is not None and media.content_b64 == "b2s="
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_direct_final_result_does_not_reuse_sent_gate_or_follow_up_row(
    tmp_path: Path,
) -> None:
    engine, factory = await _database(tmp_path)
    try:
        async with factory() as session:
            await create_agent(
                session,
                agent_id="agent-real",
                owner_email="user@example.com",
                name="Agent",
            )
            conversation = await create_conversation(
                session,
                user_email="user@example.com",
                agent_id="agent-real",
                context_type="matrix",
                context_ref="matrix:acct-real:room-real",
                context_data={
                    "channel_type": "matrix",
                    "account_id": "acct-real",
                    "chat_id": "room-real",
                },
                title="Matrix",
            )
            for delivery_id, source_type in (
                ("cdel-gate", "task_gate_follow_up"),
                ("cdel-follow-up", "task_result_follow_up"),
                ("cdel-legacy-gate", "task"),
            ):
                row = await create_channel_delivery_outbox(
                    session,
                    delivery_id=delivery_id,
                    user_email="user@example.com",
                    conversation_id=conversation.conversation_id,
                    session_id=None,
                    source_type=source_type,
                    source_id="task-shared",
                    channel_type="matrix",
                    account_id="acct-real",
                    chat_id="room-real",
                    thread_id=None,
                    fallback_text="old notification",
                    next_attempt_at=datetime.now(UTC),
                )
                row.status = "sent"
                row.sent_at = datetime.now(UTC)
            await session.commit()

        class _Adapter:
            capabilities = ChannelCapabilities(
                supports_idempotent_send=True,
                max_message_length=200,
            )

            def __init__(self) -> None:
                self.calls: list[str] = []

            async def send_message(self, message: OutboundMessage) -> str:
                self.calls.append(message.content)
                return "event-final"

        adapter = _Adapter()
        service = ChannelDeliveryService(
            session_factory=factory,
            event_bus=EventBus(),
            channel_manager_ref=lambda: _Manager(adapter),
        )
        status = await service.deliver_task_to_conversation(
            conversation.conversation_id,
            task_id="task-shared",
            content="final result",
        )

        assert status == ChannelDeliveryStatus.SENT
        assert adapter.calls == ["final result"]
        async with factory() as session:
            final = await get_channel_delivery_outbox_for_source(
                session,
                conversation_id=conversation.conversation_id,
                source_type="task_final_result",
                source_id="task-shared",
            )
            gate = await get_channel_delivery_outbox(session, "cdel-gate")
            follow_up = await get_channel_delivery_outbox(session, "cdel-follow-up")
            legacy_gate = await get_channel_delivery_outbox(session, "cdel-legacy-gate")
            assert final is not None
            assert final.status == "sent"
            assert final.delivery_id not in {
                "cdel-gate",
                "cdel-follow-up",
                "cdel-legacy-gate",
            }
            assert gate is not None and gate.status == "sent"
            assert follow_up is not None and follow_up.status == "sent"
            assert legacy_gate is not None and legacy_gate.status == "sent"
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_partial_attachment_materialization_retries_without_duplicate_text(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine, factory = await _database(tmp_path)
    try:
        await _outbox(factory, delivery_id="cdel-attachments")

        class _Adapter:
            capabilities = ChannelCapabilities(max_message_length=4000)

            def __init__(self) -> None:
                self.calls: list[OutboundMessage] = []

            async def send_message(self, message: OutboundMessage) -> str:
                self.calls.append(message)
                return f"message-{len(self.calls)}"

        adapter = _Adapter()
        service = ChannelDeliveryService(
            session_factory=factory,
            event_bus=EventBus(),
            channel_manager_ref=lambda: _Manager(adapter),
        )
        attachment_refs = [
            {"artifact_id": "artifact-ok", "filename": "ok.txt"},
            {"artifact_id": "artifact-later", "filename": "later.txt"},
        ]
        service._prepare_media_attachments = AsyncMock(  # type: ignore[method-assign]
            side_effect=[
                (
                    [MediaAttachment(filename="ok.txt", content_b64="b2s=")],
                    ["Attachment unavailable: later.txt"],
                    True,
                ),
                (
                    [
                        MediaAttachment(filename="ok.txt", content_b64="b2s="),
                        MediaAttachment(filename="later.txt", content_b64="bGF0ZXI="),
                    ],
                    [],
                    False,
                ),
            ]
        )
        update_status = AsyncMock(return_value=True)
        monkeypatch.setattr(
            "cognis.store.queries.update_deliverable_status",
            update_status,
        )

        await service._deliver_outbox(  # noqa: SLF001
            delivery_id="cdel-attachments",
            final_content="Final result",
            fallback_text="fallback",
            attachments=attachment_refs,
            deliverable_id="dlv-attachments",
            ignore_next_attempt=True,
        )
        async with factory() as session:
            first = await get_channel_delivery_outbox(session, "cdel-attachments")
            assert first is not None
            assert first.status == "failed"
            assert first.last_error == "attachment_materialization_incomplete"
            assert first.completed_chunk_count == 0
            assert first.attachments_json == attachment_refs
        assert adapter.calls == []
        update_status.assert_not_awaited()

        await service._deliver_outbox(  # noqa: SLF001
            delivery_id="cdel-attachments",
            final_content="Final result",
            fallback_text="fallback",
            attachments=None,
            deliverable_id="dlv-attachments",
            ignore_next_attempt=True,
        )
        async with factory() as session:
            final = await get_channel_delivery_outbox(session, "cdel-attachments")
            assert final is not None
            assert final.status == "sent"
            assert final.completed_chunk_count == 1
        assert len(adapter.calls) == 1
        assert "Final result" in adapter.calls[0].content
        assert len(adapter.calls[0].media) == 2
        update_status.assert_awaited_once()
        assert update_status.await_args.kwargs["status"] == "delivered"
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_follow_up_completion_before_grace_sends_only_detailed_result(
    tmp_path: Path,
) -> None:
    engine, factory = await _database(tmp_path)
    try:
        conversation_id = await _matrix_follow_up(factory, delivery_id="cdel-grace-before")
        service = ChannelDeliveryService(
            session_factory=factory,
            event_bus=EventBus(),
            channel_manager_ref=lambda: None,
        )
        calls: list[dict[str, Any]] = []
        service._send_to_route = AsyncMock(side_effect=_successful_route_sender(calls))  # type: ignore[method-assign]
        event = Event(
            type=EventType.TURN_COMPLETED,
            data={
                "delivery_id": "cdel-grace-before",
                "conversation_id": conversation_id,
                "session_id": "sess-follow-up",
                "turn_id": "turn-before",
                "channel_deliverable": True,
                "final_content": "Detailed result.",
                "delivery_fallback_text": "Generic grace notice.",
            },
        )

        await service._handle_turn_completed_event(event)

        service._send_to_route.assert_awaited_once()
        assert calls[0]["content"] == "Detailed result."
        async with factory() as session:
            grace = await get_channel_delivery_outbox(session, "cdel-grace-before")
            result = await get_channel_delivery_outbox_for_source(
                session,
                conversation_id=conversation_id,
                source_type="follow_up_result",
                source_id="turn-before",
            )
        assert grace is not None and grace.status == "suppressed"
        assert result is not None and result.status == "sent"
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_follow_up_completion_after_grace_sends_generic_then_detailed_once(
    tmp_path: Path,
) -> None:
    engine, factory = await _database(tmp_path)
    try:
        conversation_id = await _matrix_follow_up(factory, delivery_id="cdel-grace-after")
        service = ChannelDeliveryService(
            session_factory=factory,
            event_bus=EventBus(),
            channel_manager_ref=lambda: None,
        )
        calls: list[dict[str, Any]] = []
        service._send_to_route = AsyncMock(side_effect=_successful_route_sender(calls))  # type: ignore[method-assign]

        await service._deliver_outbox(
            delivery_id="cdel-grace-after",
            final_content=None,
            fallback_text="Generic grace notice.",
            ignore_next_attempt=True,
        )
        event = Event(
            type=EventType.TURN_COMPLETED,
            data={
                "delivery_id": "cdel-grace-after",
                "conversation_id": conversation_id,
                "session_id": "sess-follow-up",
                "turn_id": "turn-after",
                "channel_deliverable": True,
                "final_content": "Detailed late result.",
                "delivery_fallback_text": "Generic grace notice.",
            },
        )
        await service._handle_turn_completed_event(event)
        await service._handle_turn_completed_event(event)
        await service.recover_pending_deliveries()

        assert [call["content"] for call in calls] == [
            "Generic grace notice.",
            "Detailed late result.",
        ]
        async with factory() as session:
            grace = await get_channel_delivery_outbox(session, "cdel-grace-after")
            result = await get_channel_delivery_outbox_for_source(
                session,
                conversation_id=conversation_id,
                source_type="follow_up_result",
                source_id="turn-after",
            )
        assert grace is not None and grace.status == "sent"
        assert result is not None and result.status == "sent"
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_persisted_follow_up_result_survives_lost_event_and_restart(
    tmp_path: Path,
) -> None:
    engine, factory = await _database(tmp_path)
    try:
        conversation_id = await _matrix_follow_up(factory, delivery_id="cdel-restart")
        attachments = [{"artifact_id": "art-result", "filename": "result.txt"}]
        async with factory() as session:
            row = await ensure_follow_up_result_delivery(
                session,
                grace_delivery_id="cdel-restart",
                conversation_id=conversation_id,
                session_id="sess-follow-up",
                turn_id="turn-restart",
                final_content="Durable result.",
                attachments=attachments,
                deliverable_id="dlv-result",
            )
            await session.commit()
            assert row is not None

        restarted = ChannelDeliveryService(
            session_factory=factory,
            event_bus=EventBus(),
            channel_manager_ref=lambda: None,
        )
        calls: list[dict[str, Any]] = []
        restarted._send_to_route = AsyncMock(side_effect=_successful_route_sender(calls))  # type: ignore[method-assign]
        await restarted.recover_pending_deliveries()

        restarted._send_to_route.assert_awaited_once()
        kwargs = calls[0]
        assert kwargs["content"] == "Durable result."
        assert kwargs["media"] == attachments
        assert kwargs["deliverable_id"] == "dlv-result"
        assert kwargs["channel_type"] == "matrix"
        assert kwargs["account_id"] == "acct-follow-up"
        assert kwargs["chat_id"] == "room-follow-up"
        assert kwargs["thread_id"] == "thread-follow-up"
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_follow_up_deliverable_only_result_is_sent_after_restart(
    tmp_path: Path,
) -> None:
    engine, factory = await _database(tmp_path)
    try:
        conversation_id = await _matrix_follow_up(factory, delivery_id="cdel-deliverable-only")
        async with factory() as session:
            await ensure_follow_up_result_delivery(
                session,
                grace_delivery_id="cdel-deliverable-only",
                conversation_id=conversation_id,
                session_id="sess-follow-up",
                turn_id="turn-deliverable-only",
                final_content=None,
                attachments=None,
                deliverable_id="dlv-only",
            )
            await session.commit()

        restarted = ChannelDeliveryService(
            session_factory=factory,
            event_bus=EventBus(),
            channel_manager_ref=lambda: None,
        )
        calls: list[dict[str, Any]] = []
        restarted._send_to_route = AsyncMock(side_effect=_successful_route_sender(calls))  # type: ignore[method-assign]
        await restarted.recover_pending_deliveries()

        restarted._send_to_route.assert_awaited_once()
        assert calls[0]["deliverable_id"] == "dlv-only"
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_follow_up_result_rejects_conflicting_terminal_turn(
    tmp_path: Path,
) -> None:
    engine, factory = await _database(tmp_path)
    try:
        conversation_id = await _matrix_follow_up(factory, delivery_id="cdel-conflict")
        async with factory() as session:
            await ensure_follow_up_result_delivery(
                session,
                grace_delivery_id="cdel-conflict",
                conversation_id=conversation_id,
                session_id="sess-follow-up",
                turn_id="turn-first",
                final_content="First terminal result.",
                attachments=None,
                deliverable_id=None,
            )
            await session.commit()
        async with factory() as session:
            with pytest.raises(ValueError, match="stale or conflicting"):
                await ensure_follow_up_result_delivery(
                    session,
                    grace_delivery_id="cdel-conflict",
                    conversation_id=conversation_id,
                    session_id="sess-follow-up",
                    turn_id="turn-stale",
                    final_content="Stale terminal result.",
                    attachments=None,
                    deliverable_id=None,
                )
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_terminal_error_after_grace_does_not_duplicate_notification(
    tmp_path: Path,
) -> None:
    engine, factory = await _database(tmp_path)
    try:
        conversation_id = await _matrix_follow_up(factory, delivery_id="cdel-error")
        service = ChannelDeliveryService(
            session_factory=factory,
            event_bus=EventBus(),
            channel_manager_ref=lambda: None,
        )
        calls: list[dict[str, Any]] = []
        service._send_to_route = AsyncMock(side_effect=_successful_route_sender(calls))  # type: ignore[method-assign]
        await service._deliver_outbox(
            delivery_id="cdel-error",
            final_content=None,
            fallback_text="Generic grace notice.",
            ignore_next_attempt=True,
        )

        await service._handle_turn_error_event(
            Event(
                type=EventType.TURN_ERROR,
                data={
                    "delivery_id": "cdel-error",
                    "conversation_id": conversation_id,
                    "turn_id": "turn-error",
                    "channel_deliverable": True,
                    "delivery_fallback_text": "Generic grace notice.",
                },
            )
        )

        service._send_to_route.assert_awaited_once()
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_follow_up_result_ignores_non_deliverable_and_unbound_web_events(
    tmp_path: Path,
) -> None:
    engine, factory = await _database(tmp_path)
    try:
        async with factory() as session:
            await create_agent(
                session,
                agent_id="agent-web",
                owner_email="user@example.com",
                name="Web agent",
            )
            conversation = await create_conversation(
                session,
                user_email="user@example.com",
                agent_id="agent-web",
                context_type="web",
                context_ref=None,
                context_data={},
                title="Web",
            )
            await session.commit()
        service = ChannelDeliveryService(
            session_factory=factory,
            event_bus=EventBus(),
            channel_manager_ref=lambda: None,
        )
        service._send_to_route = AsyncMock(return_value=ChannelDeliveryStatus.SENT)  # type: ignore[method-assign]

        base = {
            "delivery_id": "cdel-web-missing",
            "conversation_id": conversation.conversation_id,
            "session_id": "sess-web",
            "turn_id": "turn-web",
            "final_content": "Web result.",
        }
        await service._handle_turn_completed_event(
            Event(type=EventType.TURN_COMPLETED, data={**base, "channel_deliverable": False})
        )
        await service._handle_turn_completed_event(
            Event(type=EventType.TURN_COMPLETED, data={**base, "channel_deliverable": True})
        )

        service._send_to_route.assert_not_awaited()
        async with factory() as session:
            assert (
                await get_channel_delivery_outbox_for_source(
                    session,
                    conversation_id=conversation.conversation_id,
                    source_type="follow_up_result",
                    source_id="turn-web",
                )
                is None
            )
    finally:
        await engine.dispose()
