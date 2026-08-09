"""Pairing flow for external channel senders.

Unknown senders can be challenged with a short-lived pairing code that the
user redeems in the Cognis web UI. Once redeemed, the sender is stored as a
verified ``channel_contact`` and may interact with the agent.
"""

from __future__ import annotations

import contextlib
import secrets
import string
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy.ext.asyncio import async_sessionmaker

from cognis.logging import get_logger
from cognis.models.channel import (
    ChannelAccountConfig,
    InboundMessage,
    OutboundMessage,
    PairingRequest,
)

logger = get_logger(__name__)

_CODE_ALPHABET = string.ascii_uppercase + string.digits
_CODE_LENGTH = 6
_CODE_TTL_MINUTES = 15
_MAX_REQUESTS_PER_HOUR = 3
_MAX_REDEEM_ATTEMPTS = 5


def _normalize_code(code: str) -> str:
    return "".join(ch for ch in code.upper() if ch.isalnum())


def _format_code(code: str) -> str:
    normalized = _normalize_code(code)
    if len(normalized) <= 3:
        return normalized
    return f"{normalized[:3]}-{normalized[3:]}"


def _generate_code() -> str:
    return "".join(secrets.choice(_CODE_ALPHABET) for _ in range(_CODE_LENGTH))


def _ensure_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


class PairingService:
    """Creates, redeems, and rejects external channel pairing requests."""

    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[Any],
        channel_manager_ref: Any,
    ) -> None:
        self._session_factory = session_factory
        self._channel_manager_ref = channel_manager_ref

    async def ensure_verified_sender(
        self,
        *,
        message: InboundMessage,
        config: ChannelAccountConfig,
        executor_connection_owner: Any | None = None,
    ) -> str | None:
        """Return the paired user email or issue a pairing challenge.

        When no verified mapping exists, a short-lived pairing challenge is
        sent to the sender and ``None`` is returned.
        """
        from cognis.store.queries import (
            count_recent_pairing_requests,
            create_pairing_request,
            expire_stale_pairing_requests,
            get_channel_contact,
            get_pending_pairing_request_for_sender,
        )

        async with self._session_factory() as session:
            if executor_connection_owner is not None:
                from cognis.core.executor_connection_ownership import (
                    ExecutorConnectionOwnership,
                )

                if not await ExecutorConnectionOwnership.lock_current(
                    session,
                    executor_connection_owner,
                ):
                    await session.rollback()
                    return None
            await expire_stale_pairing_requests(session)

            contact = await get_channel_contact(session, message.channel_type, message.sender_id)
            if contact is not None and contact.verified:
                return contact.user_email

            pending = await get_pending_pairing_request_for_sender(
                session,
                account_id=config.account_id,
                channel_type=message.channel_type,
                sender_id=message.sender_id,
            )
            if pending is not None:
                await session.commit()
                await self._send_pairing_challenge(config.account_id, message.chat_id, pending.code)
                return None

            recent_count = await count_recent_pairing_requests(
                session,
                account_id=config.account_id,
                channel_type=message.channel_type,
                sender_id=message.sender_id,
                since=datetime.now(UTC) - timedelta(hours=1),
            )
            if recent_count >= _MAX_REQUESTS_PER_HOUR:
                await session.commit()
                await self._send_rate_limited_message(config.account_id, message.chat_id)
                return None

            code = await self._generate_unique_code(session)
            expires_at = datetime.now(UTC) + timedelta(minutes=_CODE_TTL_MINUTES)
            request = await create_pairing_request(
                session,
                owner_email=config.user_email,
                account_id=config.account_id,
                channel_type=message.channel_type,
                sender_id=message.sender_id,
                sender_name=message.sender_name,
                chat_id=message.chat_id,
                chat_name=message.chat_name,
                code=code,
                expires_at=expires_at,
            )
            await session.commit()

        await self._send_pairing_challenge(config.account_id, message.chat_id, request.code)
        return None

    async def redeem_code(self, *, owner_email: str, code: str) -> PairingRequest:
        """Redeem a pairing code and create a verified channel contact."""
        from cognis.store.queries import (
            complete_pairing_request,
            create_channel_contact,
            expire_stale_pairing_requests,
            get_channel_contact,
            get_pairing_request_by_code,
            increment_pairing_request_attempts,
        )

        normalized = _normalize_code(code)
        if len(normalized) != _CODE_LENGTH:
            raise ValueError("Invalid pairing code")

        async with self._session_factory() as session:
            await expire_stale_pairing_requests(session)
            request = await get_pairing_request_by_code(
                session, owner_email=owner_email, code=normalized
            )
            if request is None:
                raise ValueError("Pairing code not found")
            expires_at = _ensure_utc(request.expires_at)
            if request.status == "expired" or expires_at <= datetime.now(UTC):
                request.status = "expired"
                await session.commit()
                raise ValueError("Pairing code has expired")
            request = (
                await increment_pairing_request_attempts(session, request.request_id) or request
            )
            if request.status != "pending":
                raise ValueError("Pairing code is no longer active")
            if request.attempts >= _MAX_REDEEM_ATTEMPTS:
                request.status = "rejected"
                await session.commit()
                raise ValueError("Too many invalid pairing attempts")

            existing = await get_channel_contact(session, request.channel_type, request.sender_id)
            if existing is None:
                await create_channel_contact(
                    session,
                    channel_type=request.channel_type,
                    sender_id=request.sender_id,
                    user_email=owner_email,
                    display_name=request.sender_name,
                    verified=True,
                )
            else:
                existing.user_email = owner_email
                existing.display_name = request.sender_name or existing.display_name
                existing.verified = True
            await complete_pairing_request(session, request.request_id)
            await session.commit()

            return PairingRequest(
                request_id=request.request_id,
                owner_email=request.owner_email,
                account_id=request.account_id,
                channel_type=request.channel_type,
                sender_id=request.sender_id,
                sender_name=request.sender_name,
                chat_id=request.chat_id,
                chat_name=request.chat_name,
                code=_format_code(request.code),
                status="completed",
                attempts=request.attempts,
                expires_at=expires_at,
                created_at=request.created_at,
                completed_at=request.completed_at or datetime.now(UTC),
            )

    async def reject_request(self, *, owner_email: str, request_id: str) -> bool:
        """Reject a pending pairing request."""
        from cognis.store.queries import reject_pairing_request

        async with self._session_factory() as session:
            row = await reject_pairing_request(
                session, owner_email=owner_email, request_id=request_id
            )
            if row is None:
                return False
            await session.commit()
            return True

    async def list_pending_requests(self, *, owner_email: str) -> list[PairingRequest]:
        """List pending pairing requests for the current Cognis user."""
        from cognis.store.queries import expire_stale_pairing_requests, list_pairing_requests

        async with self._session_factory() as session:
            await expire_stale_pairing_requests(session)
            rows = await list_pairing_requests(
                session, owner_email=owner_email, statuses=["pending"]
            )
            await session.commit()

        return [
            PairingRequest(
                request_id=row.request_id,
                owner_email=row.owner_email,
                account_id=row.account_id,
                channel_type=row.channel_type,
                sender_id=row.sender_id,
                sender_name=row.sender_name,
                chat_id=row.chat_id,
                chat_name=row.chat_name,
                code=_format_code(row.code),
                status=row.status,
                attempts=row.attempts,
                expires_at=row.expires_at,
                created_at=row.created_at,
                completed_at=row.completed_at,
            )
            for row in rows
        ]

    async def _generate_unique_code(self, session: Any) -> str:
        from cognis.store.queries import get_pairing_request_by_code

        for _ in range(20):
            code = _generate_code()
            existing = await get_pairing_request_by_code(session, owner_email=None, code=code)
            if existing is None:
                return code
        raise ValueError("Failed to generate unique pairing code")

    async def _send_pairing_challenge(self, account_id: str, chat_id: str, code: str) -> None:
        manager = self._channel_manager_ref()
        if manager is None:
            return
        adapter = manager.get_adapter(account_id)
        config = manager.get_config(account_id)
        if adapter is None or config is None:
            return
        text = (
            "I don't recognize your account yet. To connect it, open Cognis and enter this code:\n\n"
            f"  {_format_code(code)}\n\n"
            f"This code expires in {_CODE_TTL_MINUTES} minutes."
        )
        with contextlib.suppress(Exception):
            await adapter.send_message(
                OutboundMessage(
                    channel_type=config.channel_type,
                    account_id=account_id,
                    chat_id=chat_id,
                    content=text,
                )
            )

    async def _send_rate_limited_message(self, account_id: str, chat_id: str) -> None:
        manager = self._channel_manager_ref()
        if manager is None:
            return
        adapter = manager.get_adapter(account_id)
        config = manager.get_config(account_id)
        if adapter is None or config is None:
            return
        with contextlib.suppress(Exception):
            await adapter.send_message(
                OutboundMessage(
                    channel_type=config.channel_type,
                    account_id=account_id,
                    chat_id=chat_id,
                    content="Too many pairing attempts were requested recently. Please try again in about an hour.",
                )
            )
