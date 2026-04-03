from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from cognis.channels.pairing import PairingService
from cognis.models.channel import ChannelAccountConfig, InboundMessage
from cognis.store.database import create_engine, create_session_factory
from cognis.store.queries import (
    create_agent,
    create_channel_account,
    create_channel_contact,
    create_pairing_request,
    create_user,
    get_channel_contact,
    get_pairing_request_by_code,
    list_pairing_requests,
)


class _Adapter:
    def __init__(self) -> None:
        self.messages: list[dict[str, str]] = []

    async def send_message(self, message) -> None:
        self.messages.append({"chat_id": message.chat_id, "content": message.content})


class _Manager:
    def __init__(self, config: ChannelAccountConfig) -> None:
        self._config = config
        self.adapter = _Adapter()

    def get_adapter(self, account_id: str) -> _Adapter | None:
        if account_id == self._config.account_id:
            return self.adapter
        return None

    def get_config(self, account_id: str) -> ChannelAccountConfig | None:
        if account_id == self._config.account_id:
            return self._config
        return None


async def _setup(tmp_path):
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path / 'cognis.db'}")
    session_factory = create_session_factory(engine)
    from cognis.bootstrap import run_schema_bootstrap

    await run_schema_bootstrap(engine)
    async with session_factory() as session:
        await create_user(
            session,
            email="owner@example.com",
            name="Owner",
            password_hash="x",
            role="user",
        )
        await create_agent(
            session,
            agent_id="agent-1",
            owner_email="owner@example.com",
            name="Agent 1",
            status="active",
        )
        row = await create_channel_account(
            session,
            account_id="ch_signal",
            channel_type="signal",
            display_name="Signal Bot",
            agent_id="agent-1",
            user_email="owner@example.com",
            dm_policy="pairing",
            group_policy="pairing",
        )
        await session.commit()

    config = ChannelAccountConfig(
        account_id=row.account_id,
        channel_type=row.channel_type,
        display_name=row.display_name,
        credential_refs=row.credential_refs or {},
        agent_id=row.agent_id,
        user_email=row.user_email,
        settings=row.config or {},
        default_conversation_id=row.default_conversation_id,
        allow_new_conversations=row.allow_new_conversations,
        allowed_senders=row.allowed_senders or [],
        dm_policy=row.dm_policy,
        group_policy=row.group_policy,
        webhook_secret=row.webhook_secret,
    )
    manager = _Manager(config)
    service = PairingService(session_factory=session_factory, channel_manager_ref=lambda: manager)
    return engine, session_factory, config, manager, service


def _message(*, sender_id: str = "+420111222333", sender_name: str = "Filip") -> InboundMessage:
    return InboundMessage(
        channel_type="signal",
        account_id="ch_signal",
        message_id="msg-1",
        sender_id=sender_id,
        sender_name=sender_name,
        chat_id=sender_id,
        chat_type="direct",
        content="Hello",
        timestamp=datetime.now(UTC),
    )


@pytest.mark.asyncio
async def test_new_sender_gets_pairing_challenge(tmp_path) -> None:
    engine, session_factory, config, manager, service = await _setup(tmp_path)
    try:
        resolved = await service.ensure_verified_sender(message=_message(), config=config)
        assert resolved is None
        assert len(manager.adapter.messages) == 1
        assert "I don't recognize your account yet" in manager.adapter.messages[0]["content"]

        async with session_factory() as session:
            rows = await list_pairing_requests(session, owner_email="owner@example.com")
            assert len(rows) == 1
            assert rows[0].status == "pending"
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_verified_contact_bypasses_pairing(tmp_path) -> None:
    engine, session_factory, config, manager, service = await _setup(tmp_path)
    try:
        async with session_factory() as session:
            await create_channel_contact(
                session,
                channel_type="signal",
                sender_id="+420111222333",
                user_email="owner@example.com",
                display_name="Filip",
                verified=True,
            )
            await session.commit()

        resolved = await service.ensure_verified_sender(message=_message(), config=config)
        assert resolved == "owner@example.com"
        assert manager.adapter.messages == []
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_subsequent_message_resends_existing_code(tmp_path) -> None:
    engine, session_factory, config, manager, service = await _setup(tmp_path)
    try:
        first = _message()
        await service.ensure_verified_sender(message=first, config=config)
        await service.ensure_verified_sender(message=first, config=config)

        assert len(manager.adapter.messages) == 2
        async with session_factory() as session:
            rows = await list_pairing_requests(session, owner_email="owner@example.com")
            assert len(rows) == 1
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_rate_limit_after_three_requests(tmp_path) -> None:
    engine, session_factory, config, manager, service = await _setup(tmp_path)
    try:
        async with session_factory() as session:
            now = datetime.now(UTC)
            for index in range(3):
                await create_pairing_request(
                    session,
                    owner_email="owner@example.com",
                    account_id="ch_signal",
                    channel_type="signal",
                    sender_id="+420111222333",
                    sender_name="Filip",
                    chat_id="+420111222333",
                    chat_name=None,
                    code=f"ABC12{index}",
                    expires_at=now - timedelta(minutes=1),
                )
            await session.commit()

        resolved = await service.ensure_verified_sender(message=_message(), config=config)
        assert resolved is None
        assert "Too many pairing attempts" in manager.adapter.messages[-1]["content"]
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_redeem_code_creates_verified_contact(tmp_path) -> None:
    engine, session_factory, config, manager, service = await _setup(tmp_path)
    try:
        await service.ensure_verified_sender(message=_message(), config=config)
        async with session_factory() as session:
            row = (await list_pairing_requests(session, owner_email="owner@example.com"))[0]
            code = row.code

        redeemed = await service.redeem_code(owner_email="owner@example.com", code=code)
        assert redeemed.status == "completed"

        async with session_factory() as session:
            contact = await get_channel_contact(session, "signal", "+420111222333")
            assert contact is not None
            assert contact.verified is True
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_redeem_expired_code_raises(tmp_path) -> None:
    engine, session_factory, _, _, service = await _setup(tmp_path)
    try:
        async with session_factory() as session:
            await create_pairing_request(
                session,
                owner_email="owner@example.com",
                account_id="ch_signal",
                channel_type="signal",
                sender_id="+420111222333",
                sender_name="Filip",
                chat_id="+420111222333",
                chat_name=None,
                code="ABC123",
                expires_at=datetime.now(UTC) - timedelta(minutes=1),
            )
            await session.commit()

        with pytest.raises(ValueError, match="expired"):
            await service.redeem_code(owner_email="owner@example.com", code="ABC-123")
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_redeem_max_attempts_rejects(tmp_path) -> None:
    engine, session_factory, _, _, service = await _setup(tmp_path)
    try:
        async with session_factory() as session:
            row = await create_pairing_request(
                session,
                owner_email="owner@example.com",
                account_id="ch_signal",
                channel_type="signal",
                sender_id="+420111222333",
                sender_name="Filip",
                chat_id="+420111222333",
                chat_name=None,
                code="ABC123",
                expires_at=datetime.now(UTC) + timedelta(minutes=15),
            )
            row.attempts = 4
            await session.commit()

        with pytest.raises(ValueError, match="Too many"):
            await service.redeem_code(owner_email="owner@example.com", code="ABC123")
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_redeem_increments_attempts(tmp_path) -> None:
    engine, session_factory, _, _, service = await _setup(tmp_path)
    try:
        async with session_factory() as session:
            await create_pairing_request(
                session,
                owner_email="owner@example.com",
                account_id="ch_signal",
                channel_type="signal",
                sender_id="+420111222333",
                sender_name="Filip",
                chat_id="+420111222333",
                chat_name=None,
                code="ABC123",
                expires_at=datetime.now(UTC) + timedelta(minutes=15),
            )
            await session.commit()

        await service.redeem_code(owner_email="owner@example.com", code="abc-123")

        async with session_factory() as session:
            row = await get_pairing_request_by_code(
                session,
                owner_email="owner@example.com",
                code="ABC123",
            )
            assert row is not None
            assert row.attempts == 1
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_reject_pending_request_succeeds(tmp_path) -> None:
    engine, session_factory, _, _, service = await _setup(tmp_path)
    try:
        async with session_factory() as session:
            row = await create_pairing_request(
                session,
                owner_email="owner@example.com",
                account_id="ch_signal",
                channel_type="signal",
                sender_id="+420111222333",
                sender_name="Filip",
                chat_id="+420111222333",
                chat_name=None,
                code="ABC123",
                expires_at=datetime.now(UTC) + timedelta(minutes=15),
            )
            await session.commit()

        rejected = await service.reject_request(
            owner_email="owner@example.com", request_id=row.request_id
        )
        assert rejected is True

        rows = await service.list_pending_requests(owner_email="owner@example.com")
        assert rows == []
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_reject_completed_request_returns_false(tmp_path) -> None:
    engine, session_factory, _, _, service = await _setup(tmp_path)
    try:
        async with session_factory() as session:
            row = await create_pairing_request(
                session,
                owner_email="owner@example.com",
                account_id="ch_signal",
                channel_type="signal",
                sender_id="+420111222333",
                sender_name="Filip",
                chat_id="+420111222333",
                chat_name=None,
                code="ABC123",
                expires_at=datetime.now(UTC) + timedelta(minutes=15),
            )
            row.status = "completed"
            await session.commit()

        rejected = await service.reject_request(
            owner_email="owner@example.com", request_id=row.request_id
        )
        assert rejected is False
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_list_pending_requests_returns_only_pending(tmp_path) -> None:
    engine, session_factory, _, _, service = await _setup(tmp_path)
    try:
        async with session_factory() as session:
            pending = await create_pairing_request(
                session,
                owner_email="owner@example.com",
                account_id="ch_signal",
                channel_type="signal",
                sender_id="+420111222333",
                sender_name="Filip",
                chat_id="+420111222333",
                chat_name=None,
                code="ABC123",
                expires_at=datetime.now(UTC) + timedelta(minutes=15),
            )
            completed = await create_pairing_request(
                session,
                owner_email="owner@example.com",
                account_id="ch_signal",
                channel_type="signal",
                sender_id="+420999888777",
                sender_name="Other",
                chat_id="+420999888777",
                chat_name=None,
                code="XYZ789",
                expires_at=datetime.now(UTC) + timedelta(minutes=15),
            )
            completed.status = "completed"
            await session.commit()

        rows = await service.list_pending_requests(owner_email="owner@example.com")
        assert [row.request_id for row in rows] == [pending.request_id]
    finally:
        await engine.dispose()
