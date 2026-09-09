"""Lookup boundary for future managed-channel bindings."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from cognis.store.models import ManagedChannelBinding, ManagedConversationLink


@dataclass(frozen=True, slots=True)
class ActiveManagedChannelBinding:
    """Compact metadata for an active managed conversation bound to a target."""

    conversation_id: str
    agent_id: str
    title: str | None
    status: str
    owner_epoch: int | None = None
    expires_at: datetime | None = None
    outcome_uncertain: bool = False
    recovery_eligible: bool = False


class ManagedChannelBindingLookup(Protocol):
    """Resolve an active managed conversation for an observed channel target."""

    async def find_active_binding(
        self,
        *,
        user_email: str,
        account_id: str,
        chat_id: str,
        thread_id: str | None = None,
    ) -> ActiveManagedChannelBinding | None: ...


class NoManagedChannelBindingLookup:
    """No-op implementation for tests and runtimes without managed bindings."""

    async def find_active_binding(
        self,
        *,
        user_email: str,
        account_id: str,
        chat_id: str,
        thread_id: str | None = None,
    ) -> ActiveManagedChannelBinding | None:
        del user_email, account_id, chat_id, thread_id
        return None


class DatabaseManagedChannelBindingLookup:
    """Resolve active bindings from the managed-channel foundation tables."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def find_active_binding(
        self,
        *,
        user_email: str,
        account_id: str,
        chat_id: str,
        thread_id: str | None = None,
    ) -> ActiveManagedChannelBinding | None:
        async with self._session_factory() as session:
            result = await session.execute(
                select(ManagedChannelBinding, ManagedConversationLink)
                .join(
                    ManagedConversationLink,
                    ManagedConversationLink.link_id == ManagedChannelBinding.link_id,
                )
                .where(
                    ManagedChannelBinding.user_email == user_email,
                    ManagedChannelBinding.account_id == account_id,
                    ManagedChannelBinding.chat_id == chat_id,
                    ManagedChannelBinding.thread_key == (thread_id or ""),
                    ManagedChannelBinding.active_route_key.is_not(None),
                    ManagedConversationLink.user_email == user_email,
                    ManagedConversationLink.kind == "channel",
                    ManagedConversationLink.conversation_state == "open",
                )
                .order_by(
                    ManagedChannelBinding.updated_at.desc(),
                    ManagedChannelBinding.binding_id.asc(),
                )
                .limit(1)
            )
            row = result.one_or_none()
            from cognis.channels.delivery_state import managed_delivery_outcome_uncertain

            outcome_uncertain = await managed_delivery_outcome_uncertain(
                session, row[0] if row is not None else None
            )
        if row is None:
            return None
        binding, link = row
        now = datetime.now(UTC)
        expires_at = (
            binding.expires_at.replace(tzinfo=UTC)
            if binding.expires_at.tzinfo is None
            else binding.expires_at.astimezone(UTC)
        )
        lease_expires_at = (
            (
                binding.delivery_lease_expires_at.replace(tzinfo=UTC)
                if binding.delivery_lease_expires_at.tzinfo is None
                else binding.delivery_lease_expires_at.astimezone(UTC)
            )
            if binding.delivery_lease_expires_at is not None
            else None
        )
        return ActiveManagedChannelBinding(
            conversation_id=link.target_conversation_id,
            agent_id=link.target_agent_id,
            title=link.title,
            status=binding.state,
            owner_epoch=link.owner_epoch,
            expires_at=expires_at,
            outcome_uncertain=outcome_uncertain,
            recovery_eligible=(
                binding.state == "delivery_failed"
                and binding.active_route_key is not None
                and expires_at <= now
                and not (
                    binding.delivery_lease_token
                    and lease_expires_at is not None
                    and lease_expires_at > now
                )
            ),
        )
