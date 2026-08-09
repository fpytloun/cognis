from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from cognis.channels.group_context import (
    GROUP_CONTEXT_MAX_BYTES,
    GroupContextPolicy,
    GroupContextSettingsError,
    group_context_policy,
)
from cognis.channels.managed import (
    GroupContextReservationConflict,
    ManagedChannelService,
)
from cognis.models.artifact import AttachmentRef
from cognis.models.channel import InboundMessage
from cognis.store.database import create_engine, create_session_factory
from cognis.store.direct_turns import DirectTurnStore
from cognis.store.models import (
    Base,
    ChannelContextConsumptionRow,
    ChannelInboundLedgerRow,
    DirectTurnRequestRow,
)
from cognis.store.queries import (
    create_agent,
    create_channel_account,
    create_conversation,
    create_user,
)


async def _harness(
    tmp_path: Path,
) -> tuple[
    AsyncEngine,
    async_sessionmaker[AsyncSession],
    ManagedChannelService,
]:
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path / 'group-context.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = create_session_factory(engine)
    async with factory() as session:
        await create_user(
            session,
            email="owner@example.com",
            name="Owner",
            password_hash="hash",
        )
        await create_agent(
            session,
            agent_id="agent-1",
            owner_email="owner@example.com",
            name="Agent",
            status="active",
        )
        for conversation_id in ("conv-1", "conv-2"):
            await create_conversation(
                session,
                conversation_id=conversation_id,
                user_email="owner@example.com",
                agent_id="agent-1",
                context_type="signal",
            )
        await create_channel_account(
            session,
            account_id="account-1",
            channel_type="signal",
            display_name="Signal",
            agent_id="agent-1",
            user_email="owner@example.com",
            group_policy="mention",
        )
        await session.commit()
    return engine, factory, ManagedChannelService(factory)


def _message(
    message_id: str,
    content: str,
    *,
    order: int,
    timestamp: datetime,
    mentioned: bool = False,
    bot: bool = False,
    ordering_source: str = "provider",
) -> InboundMessage:
    return InboundMessage(
        channel_type="signal",
        account_id="account-1",
        message_id=message_id,
        sender_id=f"sender-{message_id}",
        sender_name=f"Sender {message_id}",
        chat_id="group-1",
        chat_type="group",
        content=content,
        was_mentioned=mentioned,
        is_bot_output=bot,
        timestamp=timestamp,
        platform_data={
            "_cognis_ordering_key": f"{order:020d}",
            "_cognis_ordering_source": ordering_source,
        },
    )


@pytest.mark.asyncio
async def test_media_only_group_message_preserves_safe_attachment_metadata(
    tmp_path: Path,
) -> None:
    engine, factory, service = await _harness(tmp_path)
    try:
        row = await service.capture_group_message(
            _message("media-only", "", order=1, timestamp=datetime.now(UTC)),
            user_email="owner@example.com",
            policy=GroupContextPolicy(enabled=True),
            attachments=[
                AttachmentRef(
                    artifact_id="img-group",
                    kind="image",
                    mime_type="image/png",
                    filename="group.png",
                    size_bytes=17,
                    url="https://provider.invalid/private",
                )
            ],
        )
        assert row is not None
        async with factory() as session:
            stored = await session.get(ChannelInboundLedgerRow, row.inbound_id)
            assert stored is not None
            assert stored.platform_data["safe_attachments"] == [
                {
                    "artifact_id": "img-group",
                    "kind": "image",
                    "mime_type": "image/png",
                    "filename": "group.png",
                    "size_bytes": 17,
                }
            ]
    finally:
        await engine.dispose()


@pytest.mark.parametrize(
    ("settings", "message"),
    [
        ({"group_context_enabled": "true"}, "group_context_enabled must be a boolean"),
        ({"group_context_max_messages": True}, "group_context_max_messages must be an integer"),
        ({"group_context_max_bytes": "8192"}, "group_context_max_bytes must be an integer"),
        ({"group_context_max_age_seconds": 901}, "group_context_max_age_seconds"),
        ({"group_context_retention_seconds": 86401}, "group_context_retention_seconds"),
    ],
)
def test_group_context_settings_reject_coercion_and_hard_maxima(
    settings: dict[str, object],
    message: str,
) -> None:
    with pytest.raises(GroupContextSettingsError, match=message):
        group_context_policy(settings)


def test_group_context_defaults_to_disabled() -> None:
    assert group_context_policy({}).enabled is False


@pytest.mark.asyncio
async def test_newest_window_is_chronological_and_only_new_rows_repeat(
    tmp_path: Path,
) -> None:
    engine, _factory, service = await _harness(tmp_path)
    policy = GroupContextPolicy(enabled=True)
    now = datetime.now(UTC)
    try:
        for index in range(12):
            await service.capture_group_message(
                _message(
                    f"m-{index:02d}",
                    f"body-{index:02d}",
                    order=index,
                    timestamp=now + timedelta(seconds=index),
                ),
                user_email="owner@example.com",
                policy=policy,
            )
        trigger = await service.capture_group_message(
            _message(
                "trigger-1",
                "@agent first",
                order=20,
                timestamp=now + timedelta(seconds=20),
                mentioned=True,
            ),
            user_email="owner@example.com",
            policy=policy,
        )
        assert trigger is not None
        first = await service.reserve_group_context(
            trigger_inbound_id=trigger.inbound_id,
            conversation_id="conv-1",
            turn_id="turn-1",
            policy=policy,
        )
        assert [item["content"] for item in first.contextual_messages] == [
            f"body-{index:02d}" for index in range(2, 12)
        ]
        assert all(item["intention_eligible"] is False for item in first.contextual_messages)
        assert all(
            item["message_metadata"]["untrusted"] is True for item in first.contextual_messages
        )
        assert first.token is not None
        await service.settle_group_context(first.token, turn_id="turn-1", succeeded=True)

        await service.capture_group_message(
            _message(
                "m-new",
                "new chatter",
                order=21,
                timestamp=now + timedelta(seconds=21),
            ),
            user_email="owner@example.com",
            policy=policy,
        )
        trigger_2 = await service.capture_group_message(
            _message(
                "trigger-2",
                "@agent second",
                order=22,
                timestamp=now + timedelta(seconds=22),
                mentioned=True,
            ),
            user_email="owner@example.com",
            policy=policy,
        )
        assert trigger_2 is not None
        second = await service.reserve_group_context(
            trigger_inbound_id=trigger_2.inbound_id,
            conversation_id="conv-1",
            turn_id="turn-2",
            policy=policy,
        )
        assert [item["content"] for item in second.contextual_messages] == ["new chatter"]
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_consumption_is_per_conversation_and_duplicate_primary_is_idempotent(
    tmp_path: Path,
) -> None:
    engine, _factory, service = await _harness(tmp_path)
    policy = GroupContextPolicy(enabled=True)
    now = datetime.now(UTC)
    try:
        prior = await service.capture_group_message(
            _message("prior", "shared", order=1, timestamp=now),
            user_email="owner@example.com",
            policy=policy,
        )
        trigger = await service.capture_group_message(
            _message(
                "trigger",
                "@agent",
                order=2,
                timestamp=now + timedelta(seconds=1),
                mentioned=True,
            ),
            user_email="owner@example.com",
            policy=policy,
        )
        assert prior is not None and trigger is not None
        for conversation_id, turn_id in (("conv-1", "turn-1"), ("conv-2", "turn-2")):
            reservation = await service.reserve_group_context(
                trigger_inbound_id=trigger.inbound_id,
                conversation_id=conversation_id,
                turn_id=turn_id,
                policy=policy,
            )
            assert [item["content"] for item in reservation.contextual_messages] == ["shared"]
            assert reservation.token is not None
            await service.settle_group_context(
                reservation.token,
                turn_id=turn_id,
                succeeded=True,
            )
        duplicate = await service.reserve_group_context(
            trigger_inbound_id=trigger.inbound_id,
            conversation_id="conv-1",
            turn_id="turn-replay",
            policy=policy,
        )
        assert duplicate.duplicate_primary is True
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_concurrent_triggers_do_not_overwrite_reservations(tmp_path: Path) -> None:
    engine, factory, service = await _harness(tmp_path)
    policy = GroupContextPolicy(enabled=True)
    now = datetime.now(UTC)
    try:
        await service.capture_group_message(
            _message("prior", "shared", order=1, timestamp=now),
            user_email="owner@example.com",
            policy=policy,
        )
        triggers = []
        for index in (2, 3):
            trigger = await service.capture_group_message(
                _message(
                    f"trigger-{index}",
                    "@agent",
                    order=index,
                    timestamp=now + timedelta(seconds=index),
                    mentioned=True,
                ),
                user_email="owner@example.com",
                policy=policy,
            )
            assert trigger is not None
            triggers.append(trigger)

        first, second = await asyncio.gather(
            service.reserve_group_context(
                trigger_inbound_id=triggers[0].inbound_id,
                conversation_id="conv-1",
                turn_id="turn-2",
                policy=policy,
            ),
            service.reserve_group_context(
                trigger_inbound_id=triggers[1].inbound_id,
                conversation_id="conv-1",
                turn_id="turn-3",
                policy=policy,
            ),
        )
        assert first.token is not None
        assert second.token is not None
        assert first.token != second.token
        async with factory() as session:
            rows = list(
                (
                    await session.execute(
                        select(
                            ChannelContextConsumptionRow.inbound_id,
                            ChannelContextConsumptionRow.reservation_token,
                        ).where(ChannelContextConsumptionRow.consumer_conversation_id == "conv-1")
                    )
                ).all()
            )
        assert len(rows) == len({row.inbound_id for row in rows})
        assert {row.reservation_token for row in rows} == {first.token, second.token}
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_bounds_bot_oversize_age_bytes_and_order_ties(tmp_path: Path) -> None:
    engine, _factory, service = await _harness(tmp_path)
    now = datetime.now(UTC)
    policy = GroupContextPolicy(
        enabled=True,
        max_messages=10,
        max_bytes=7,
        max_age_seconds=60,
    )
    try:
        assert (
            await service.capture_group_message(
                _message("bot", "assistant", order=1, timestamp=now, bot=True),
                user_email="owner@example.com",
                policy=policy,
            )
            is None
        )
        assert (
            await service.capture_group_message(
                _message(
                    "large",
                    "x" * (GROUP_CONTEXT_MAX_BYTES + 1),
                    order=2,
                    timestamp=now,
                ),
                user_email="owner@example.com",
                policy=policy,
            )
            is None
        )
        for message_id, content, order, age in (
            ("old", "old", 3, 120),
            ("tie-a", "aaa", 4, 3),
            ("tie-b", "bbbb", 4, 2),
            ("newest", "cc", 5, 1),
        ):
            await service.capture_group_message(
                _message(
                    message_id,
                    content,
                    order=order,
                    timestamp=now - timedelta(seconds=age),
                ),
                user_email="owner@example.com",
                policy=policy,
            )
        trigger = await service.capture_group_message(
            _message("trigger", "@agent", order=6, timestamp=now, mentioned=True),
            user_email="owner@example.com",
            policy=policy,
        )
        assert trigger is not None
        reservation = await service.reserve_group_context(
            trigger_inbound_id=trigger.inbound_id,
            conversation_id="conv-1",
            turn_id="turn-bounds",
            policy=policy,
        )
        # The newest contiguous window stops before the next body exceeds 7 bytes.
        assert [item["content"] for item in reservation.contextual_messages] == [
            "bbbb",
            "cc",
        ]
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_observed_order_does_not_infer_provider_timestamp_order(tmp_path: Path) -> None:
    engine, _factory, service = await _harness(tmp_path)
    policy = GroupContextPolicy(enabled=True)
    now = datetime.now(UTC)
    try:
        await service.capture_group_message(
            _message(
                "observed-first",
                "received first",
                order=1,
                timestamp=now + timedelta(hours=1),
                ordering_source="observed",
            ),
            user_email="owner@example.com",
            policy=policy,
        )
        trigger = await service.capture_group_message(
            _message(
                "observed-trigger",
                "@agent",
                order=2,
                timestamp=now - timedelta(hours=1),
                mentioned=True,
                ordering_source="observed",
            ),
            user_email="owner@example.com",
            policy=policy,
        )
        assert trigger is not None
        reservation = await service.reserve_group_context(
            trigger_inbound_id=trigger.inbound_id,
            conversation_id="conv-1",
            turn_id="turn-observed",
            policy=policy,
        )
        assert [item["content"] for item in reservation.contextual_messages] == ["received first"]
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_stale_recovery_and_retention_are_deterministic(tmp_path: Path) -> None:
    engine, factory, service = await _harness(tmp_path)
    now = datetime.now(UTC)
    policy = GroupContextPolicy(enabled=True, retention_seconds=1)
    try:
        trigger = await service.capture_group_message(
            _message("trigger", "@agent", order=1, timestamp=now, mentioned=True),
            user_email="owner@example.com",
            policy=policy,
        )
        assert trigger is not None
        reservation = await service.reserve_group_context(
            trigger_inbound_id=trigger.inbound_id,
            conversation_id="conv-1",
            turn_id="turn-missing",
            policy=policy,
        )
        assert reservation.token is not None
        assert await service.purge_expired_group_context(now=now + timedelta(seconds=2)) == 0
        async with factory() as session:
            await session.execute(
                ChannelContextConsumptionRow.__table__.update().values(
                    reserved_until=now - timedelta(seconds=1)
                )
            )
            await session.commit()
        await service.recover_stale_reservations(now=now)
        async with factory() as session:
            states = list(
                (await session.execute(select(ChannelContextConsumptionRow.state))).scalars()
            )
            assert states == ["released"]

        async def _settle_released(
            session: AsyncSession,
            request: DirectTurnRequestRow,
            _created: bool,
        ) -> None:
            await service.settle_group_context_in_session(
                session,
                token=reservation.token or "",
                turn_id=request.turn_id,
                require_valid=True,
            )

        with pytest.raises(GroupContextReservationConflict):
            await DirectTurnStore(factory).admit(
                conversation_id="conv-1",
                session_id=None,
                agent_id="agent-1",
                user_id="owner@example.com",
                idempotency_scope="signal:conv-1",
                idempotency_key="trigger-released",
                payload={"schema_version": 1, "content": "@agent", "attachments": []},
                turn_id="turn-missing",
                transaction_participant=_settle_released,
            )
        async with factory() as session:
            assert (
                await session.execute(
                    select(func.count())
                    .select_from(DirectTurnRequestRow)
                    .where(DirectTurnRequestRow.turn_id == "turn-missing")
                )
            ).scalar_one() == 0

        retry = await service.reserve_group_context(
            trigger_inbound_id=trigger.inbound_id,
            conversation_id="conv-1",
            turn_id="turn-retry",
            policy=policy,
        )
        assert retry.token is not None

        async with factory() as session:
            session.add(
                DirectTurnRequestRow(
                    request_id="request-admitted",
                    turn_id="turn-admitted",
                    conversation_id="conv-1",
                    agent_id="agent-1",
                    user_id="owner@example.com",
                    idempotency_scope="test",
                    idempotency_key="admitted",
                    admission_hash="hash",
                    payload_hash="payload",
                    payload_version=1,
                    payload={"schema_version": 1, "content": "", "attachments": []},
                )
            )
            consumption = (await session.execute(select(ChannelContextConsumptionRow))).scalar_one()
            consumption.state = "reserved"
            consumption.admitted_turn_id = "turn-admitted"
            consumption.reserved_until = now - timedelta(seconds=1)
            await session.commit()
        await service.recover_stale_reservations(now=now)
        async with factory() as session:
            state = (await session.execute(select(ChannelContextConsumptionRow.state))).scalar_one()
            assert state == "committed"

        assert await service.purge_expired_group_context(now=now + timedelta(seconds=2)) == 1
        async with factory() as session:
            assert (
                await session.execute(select(func.count()).select_from(ChannelInboundLedgerRow))
            ).scalar_one() == 0
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_direct_turn_admission_settles_context_in_the_same_transaction(
    tmp_path: Path,
) -> None:
    engine, factory, service = await _harness(tmp_path)
    store = DirectTurnStore(factory)
    now = datetime.now(UTC)
    policy = GroupContextPolicy(enabled=True)
    try:
        trigger = await service.capture_group_message(
            _message("trigger", "@agent", order=1, timestamp=now, mentioned=True),
            user_email="owner@example.com",
            policy=policy,
        )
        assert trigger is not None
        reservation = await service.reserve_group_context(
            trigger_inbound_id=trigger.inbound_id,
            conversation_id="conv-1",
            turn_id="turn-atomic",
            policy=policy,
        )
        assert reservation.token is not None
        admission_order: list[str] = []

        async def _guard(_session: AsyncSession) -> bool:
            admission_order.append("guard")
            return True

        async def _participant(
            session: AsyncSession,
            request: DirectTurnRequestRow,
            _created: bool,
        ) -> None:
            admission_order.append("participant")
            await service.settle_group_context_in_session(
                session,
                token=reservation.token or "",
                turn_id=request.turn_id,
                require_valid=True,
            )

        admission = await store.admit(
            conversation_id="conv-1",
            session_id=None,
            agent_id="agent-1",
            user_id="owner@example.com",
            idempotency_scope="signal:conv-1",
            idempotency_key="trigger",
            payload={"schema_version": 1, "content": "@agent", "attachments": []},
            turn_id="turn-atomic",
            admission_guard=_guard,
            transaction_participant=_participant,
        )
        assert admission.created is True
        assert admission_order == ["guard", "participant"]
        admission_order.clear()
        replay = await store.admit(
            conversation_id="conv-1",
            session_id=None,
            agent_id="agent-1",
            user_id="owner@example.com",
            idempotency_scope="signal:conv-1",
            idempotency_key="trigger",
            payload={"schema_version": 1, "content": "@agent", "attachments": []},
            turn_id="turn-atomic",
            admission_guard=_guard,
            transaction_participant=_participant,
        )
        assert replay.created is False
        assert admission_order == ["guard", "participant"]
        async with factory() as session:
            assert (
                await session.execute(select(ChannelContextConsumptionRow.state))
            ).scalar_one() == "committed"
            assert (
                await session.execute(select(ChannelInboundLedgerRow.disposition))
            ).scalar_one() == "context"

        trigger_2 = await service.capture_group_message(
            _message(
                "trigger-rollback",
                "@agent fail",
                order=2,
                timestamp=now + timedelta(seconds=1),
                mentioned=True,
            ),
            user_email="owner@example.com",
            policy=policy,
        )
        assert trigger_2 is not None
        reservation_2 = await service.reserve_group_context(
            trigger_inbound_id=trigger_2.inbound_id,
            conversation_id="conv-1",
            turn_id="turn-rollback",
            policy=policy,
        )
        assert reservation_2.token is not None

        async def _rollback(
            session: AsyncSession,
            request: DirectTurnRequestRow,
            _created: bool,
        ) -> None:
            await service.settle_group_context_in_session(
                session,
                token=reservation_2.token or "",
                turn_id=request.turn_id,
                require_valid=True,
            )
            raise RuntimeError("injected admission failure")

        with pytest.raises(RuntimeError, match="injected admission failure"):
            await store.admit(
                conversation_id="conv-1",
                session_id=None,
                agent_id="agent-1",
                user_id="owner@example.com",
                idempotency_scope="signal:conv-1",
                idempotency_key="trigger-rollback",
                payload={
                    "schema_version": 1,
                    "content": "@agent fail",
                    "attachments": [],
                },
                turn_id="turn-rollback",
                transaction_participant=_rollback,
            )
        async with factory() as session:
            assert (
                await session.execute(
                    select(func.count())
                    .select_from(DirectTurnRequestRow)
                    .where(DirectTurnRequestRow.turn_id == "turn-rollback")
                )
            ).scalar_one() == 0
            assert (
                await session.execute(
                    select(ChannelContextConsumptionRow.state).where(
                        ChannelContextConsumptionRow.admitted_turn_id == "turn-rollback"
                    )
                )
            ).scalar_one() == "reserved"
    finally:
        await engine.dispose()
