"""Durable provenance for channel targets observed by the inbound pipeline."""

from __future__ import annotations

from typing import Any, Protocol

from sqlalchemy.ext.asyncio import async_sessionmaker

from cognis.models.channel import ChannelAccountConfig, InboundMessage


class ObservedTargetRecorder(Protocol):
    async def record(self, message: InboundMessage, config: ChannelAccountConfig) -> None: ...


class DatabaseObservedTargetRecorder:
    """Record provider routing data from trusted adapter ingress."""

    def __init__(self, session_factory: async_sessionmaker[Any]) -> None:
        self._session_factory = session_factory

    async def record(self, message: InboundMessage, config: ChannelAccountConfig) -> None:
        from cognis.store.queries import upsert_channel_observed_target

        chat_kind = "group" if message.chat_type in {"group", "channel"} else "direct"
        async with self._session_factory() as session:
            await upsert_channel_observed_target(
                session,
                user_email=config.user_email,
                account_id=config.account_id,
                channel_type=config.channel_type,
                chat_id=message.chat_id,
                thread_id=message.thread_id,
                sender_id=message.sender_id,
                chat_kind=chat_kind,
                display_name=message.chat_name or message.sender_name,
            )
            await session.commit()
