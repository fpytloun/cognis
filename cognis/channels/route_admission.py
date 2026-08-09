"""Shared route serialization for one-shot and managed-channel admission."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from cognis.channels.constants import (
    ACTIVE_CHANNEL_TOOL_DELIVERY_STATES,
    EXPLICIT_CHANNEL_DELIVERY_SOURCES,
)
from cognis.store.models import (
    ChannelAccountRow,
    ChannelDeliveryOutboxRow,
    ManagedChannelBinding,
)


async def lock_channel_route(session: AsyncSession, account_id: str) -> ChannelAccountRow:
    """Serialize all route admission decisions through the account row."""

    account = await session.get(ChannelAccountRow, account_id, with_for_update=True)
    if account is None:
        raise ValueError("Channel account is no longer available")
    return account


async def active_managed_binding_id(
    session: AsyncSession,
    *,
    user_email: str,
    account_id: str,
    chat_id: str,
    thread_id: str | None,
) -> str | None:
    return (
        await session.execute(
            select(ManagedChannelBinding.binding_id).where(
                ManagedChannelBinding.user_email == user_email,
                ManagedChannelBinding.account_id == account_id,
                ManagedChannelBinding.chat_id == chat_id,
                ManagedChannelBinding.thread_key == (thread_id or ""),
                ManagedChannelBinding.active_route_key.is_not(None),
            )
        )
    ).scalar_one_or_none()


async def active_channel_tool_delivery_id(
    session: AsyncSession,
    *,
    user_email: str,
    account_id: str,
    chat_id: str,
    thread_id: str | None,
) -> str | None:
    return (
        await session.execute(
            select(ChannelDeliveryOutboxRow.delivery_id).where(
                ChannelDeliveryOutboxRow.user_email == user_email,
                ChannelDeliveryOutboxRow.account_id == account_id,
                ChannelDeliveryOutboxRow.chat_id == chat_id,
                ChannelDeliveryOutboxRow.thread_id == thread_id,
                ChannelDeliveryOutboxRow.source_type.in_(EXPLICIT_CHANNEL_DELIVERY_SOURCES),
                ChannelDeliveryOutboxRow.status.in_(ACTIVE_CHANNEL_TOOL_DELIVERY_STATES),
            )
        )
    ).scalar_one_or_none()
