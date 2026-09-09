"""Authoritative projections of persisted managed-channel delivery outcomes."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from cognis.store.models import ChannelDeliveryOutboxRow, ManagedChannelBinding


async def managed_delivery_outcome_uncertain(
    session: AsyncSession, binding: ManagedChannelBinding | None
) -> bool:
    """Read the latest managed final, including failures retained after expiry."""

    if binding is None:
        return False
    status = await session.scalar(
        select(ChannelDeliveryOutboxRow.status)
        .where(
            ChannelDeliveryOutboxRow.managed_binding_id == binding.binding_id,
            ChannelDeliveryOutboxRow.user_email == binding.user_email,
            ChannelDeliveryOutboxRow.source_type == "managed_channel_final",
        )
        .order_by(
            ChannelDeliveryOutboxRow.managed_binding_version.desc(),
            ChannelDeliveryOutboxRow.created_at.desc(),
            ChannelDeliveryOutboxRow.delivery_id.desc(),
        )
        .limit(1)
    )
    if status is not None:
        return status == "uncertain"
    # Historical bindings can outlive their outbox row. Preserve only explicit
    # uncertain failure markers in that case; a persisted outcome always wins.
    return binding.state in {"delivery_failed", "expired"} and binding.last_error in {
        "external_send_outcome_uncertain",
        "stale_non_idempotent_send",
    }
