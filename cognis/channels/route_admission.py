"""Shared route serialization for one-shot and managed-channel admission."""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import case, select
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


@dataclass(frozen=True, slots=True)
class ChannelRouteBlocker:
    """One durable route owner that prevents conflicting admission."""

    blocker_type: str
    blocker_id: str
    status: str
    recovery_tool: str | None = None
    recovery_guidance: str | None = None


async def channel_route_blockers(
    session: AsyncSession,
    *,
    user_email: str,
    account_id: str,
    chat_id: str,
    thread_id: str | None,
) -> tuple[ChannelRouteBlocker, ...]:
    """Read aggregate occupancy under the caller's account lock."""

    binding_id = await active_managed_binding_id(
        session, user_email=user_email, account_id=account_id, chat_id=chat_id, thread_id=thread_id
    )
    delivery = await active_channel_tool_delivery_blocker(
        session, user_email=user_email, account_id=account_id, chat_id=chat_id, thread_id=thread_id
    )
    blockers = []
    if binding_id is not None:
        blockers.append(
            ChannelRouteBlocker(
                "managed_binding",
                binding_id,
                "active",
                recovery_guidance=(
                    "Inspect the managed conversation. Only its controller can close it."
                ),
            )
        )
    if delivery is not None:
        blockers.append(delivery)
    return tuple(blockers)


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
    blocker = await active_channel_tool_delivery_blocker(
        session,
        user_email=user_email,
        account_id=account_id,
        chat_id=chat_id,
        thread_id=thread_id,
    )
    return blocker.blocker_id if blocker is not None else None


async def active_channel_tool_delivery_blocker(
    session: AsyncSession,
    *,
    user_email: str,
    account_id: str,
    chat_id: str,
    thread_id: str | None,
) -> ChannelRouteBlocker | None:
    row = (
        await session.execute(
            select(
                ChannelDeliveryOutboxRow.delivery_id,
                ChannelDeliveryOutboxRow.status,
            )
            .where(
                ChannelDeliveryOutboxRow.user_email == user_email,
                ChannelDeliveryOutboxRow.account_id == account_id,
                ChannelDeliveryOutboxRow.chat_id == chat_id,
                ChannelDeliveryOutboxRow.thread_id == thread_id,
                ChannelDeliveryOutboxRow.source_type.in_(EXPLICIT_CHANNEL_DELIVERY_SOURCES),
                ChannelDeliveryOutboxRow.status.in_(ACTIVE_CHANNEL_TOOL_DELIVERY_STATES),
                ChannelDeliveryOutboxRow.route_released_at.is_(None),
            )
            .order_by(
                case((ChannelDeliveryOutboxRow.status == "uncertain", 0), else_=1),
                ChannelDeliveryOutboxRow.created_at,
                ChannelDeliveryOutboxRow.delivery_id,
            )
            .limit(1)
        )
    ).one_or_none()
    if row is None:
        return None
    delivery_id, status = row
    return ChannelRouteBlocker(
        blocker_type="one_shot_delivery",
        blocker_id=delivery_id,
        status=status,
        recovery_tool=("agent_conversation_recover_channel" if status == "uncertain" else None),
        recovery_guidance=(
            "Reconcile externally, then release this delivery_id with an audit reason. "
            "Repeat discovery because another blocker can remain."
            if status == "uncertain"
            else "Wait for this delivery to settle."
        ),
    )
