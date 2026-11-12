"""Native app transport for sender-owned, bounded agent contact.

Managed bindings and the channel outbox remain the lifecycle and retry owners.
Recipient APIs project only published transport messages, never execution
history. A delivery pins its optional recipient-main-chat session before I/O;
retries append the same idempotent event even after session rotation.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, Field
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from cognis.core.events import Event, EventType
from cognis.core.inbox import publish_notification, stage_notification
from cognis.core.message_envelope import message_metadata
from cognis.models.channel import InboundMessage
from cognis.models.session import SessionEvent, with_session_events_turn_id
from cognis.runtime_context import RuntimeAccessContext, scoped_runtime_context
from cognis.store import queries
from cognis.store.models import (
    AppChannelDeliveryRow,
    AppContactBlockRow,
    ChannelAccountRow,
    ChannelDeliveryOutboxRow,
    ChannelInboundLedgerRow,
    Conversation,
    InboxNotificationRow,
    ManagedChannelBinding,
    ManagedConversationLink,
    User,
)

if TYPE_CHECKING:
    from cognis.channels.managed import ManagedChannelService
    from cognis.core.events import EventBus
    from cognis.core.session import SessionManager
    from cognis.core.session_cache import SessionCache
    from cognis.core.turn_scheduler import TurnScheduler
    from cognis.providers.registry import ProviderRegistry


class AppContactAttachment(BaseModel):
    """A delivered attachment, without sender-side grant metadata."""

    artifact_id: str
    filename: str
    href: str


async def _managed_contact_title(
    db: AsyncSession, binding: ManagedChannelBinding, agent_name: str
) -> str:
    link = await db.get(ManagedConversationLink, binding.link_id)
    source = await db.get(Conversation, link.target_conversation_id) if link else None
    return (
        (source.title if source else None)
        or (link.title if link else None)
        or f"Chat with {agent_name}"
    )[:200]


class AppContactMessage(BaseModel):
    """One participant-visible transport message."""

    id: str
    direction: Literal["incoming", "outgoing"]
    content: str
    occurred_at: datetime
    attachments: list[AppContactAttachment] = Field(default_factory=list)


class AppContactView(BaseModel):
    """Recipient-only view; no controller, session, tool, or resource grants."""

    id: str
    conversation_id: str | None = None
    agent_name: str
    sender_email: str
    title: str
    state: str
    expires_at: datetime | None = None
    can_reply: bool = False
    blocked: bool = False
    main_conversation_id: str | None = None
    messages: list[AppContactMessage] = Field(default_factory=list)


class AppContactService:
    """Own local publication and recipient authorization, not agent execution."""

    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        session_manager: SessionManager,
        providers: ProviderRegistry,
        event_bus: EventBus,
        managed_channels: ManagedChannelService,
        turn_scheduler: TurnScheduler,
        session_cache: SessionCache | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._session_manager = session_manager
        self._providers = providers
        self._event_bus = event_bus
        self._managed_channels = managed_channels
        self._turn_scheduler = turn_scheduler
        self._session_cache = session_cache

    async def deliver(self, delivery_id: str) -> str:
        """Publish an existing web outbox item exactly once to its recipient."""
        async with self._session_factory() as db:
            outbox = await db.get(ChannelDeliveryOutboxRow, delivery_id)
            if outbox is None or outbox.channel_type != "web":
                raise ValueError("App delivery not found")
            account = await queries.get_channel_account(db, outbox.account_id)
            recipient = await db.get(User, outbox.chat_id)
            sender = await db.get(User, outbox.user_email)
            agent = await queries.get_agent(db, account.agent_id) if account else None
            if (
                account is None
                or not account.enabled
                or account.channel_type != "web"
                or account.user_email != outbox.user_email
                or agent is None
                or agent.status != "active"
                or recipient is None
                or not recipient.is_active
                or sender is None
                or not sender.is_active
                or sender.role == "viewer"
            ):
                raise ValueError("App destination unavailable")
            if await db.get(AppContactBlockRow, (recipient.email, sender.email)):
                raise ValueError("App contact is blocked by the recipient")
            projection = await db.get(AppChannelDeliveryRow, delivery_id)
            already_published = projection is not None and projection.published_at is not None
            binding = (
                await db.get(ManagedChannelBinding, outbox.managed_binding_id)
                if outbox.managed_binding_id
                else None
            )
            link = await db.get(ManagedConversationLink, binding.link_id) if binding else None
            if outbox.managed_binding_id and (
                binding is None
                or binding.channel_type != "web"
                or binding.chat_id != recipient.email
                or binding.sender_id != recipient.email
                or binding.user_email != sender.email
                or binding.account_id != account.account_id
                or link is None
                or link.target_agent_id != agent.agent_id
            ):
                raise ValueError("App managed route mismatch")
            shared = (
                agent.owner_email == recipient.email
                or (await queries.get_active_agent_grant(db, agent.agent_id, recipient.email))
                is not None
            )
            recipient_email = recipient.email
            sender_email = sender.email
            agent_id = agent.agent_id
            agent_owner = agent.owner_email
            agent_name = agent.display_name or agent.name
            text = outbox.fallback_text or ""
            attachments = list(outbox.attachments_json or [])
            if not already_published:
                from cognis.core.artifact_inputs import outbound_artifact_grant_is_valid

                for attachment in attachments:
                    artifact = await queries.get_artifact_record(
                        db, attachment.get("artifact_id", "")
                    )
                    if not await outbound_artifact_grant_is_valid(
                        db, attachment=attachment, artifact=artifact, owner_email=sender_email
                    ):
                        raise ValueError("App attachment is no longer available")
            needs_main = projection is None and binding is None and shared

        destination_conversation_id = None
        destination_session_id = None
        if needs_main:
            # Do not inherit the initiating turn's policy or executor identity.
            with scoped_runtime_context(
                user_email=recipient_email,
                agent_id=agent_id,
                agent_owner_email=agent_owner,
                access_context=RuntimeAccessContext(
                    user_email=recipient_email, agent_owner_email=agent_owner
                ),
            ):
                conversation = await self._session_manager.get_or_create_agent_direct_conversation(
                    user_email=recipient_email,
                    agent_id=agent_id,
                )
                if conversation.active_session_id:
                    destination_session_id = conversation.active_session_id
                else:
                    root = await self._session_manager.ensure_root_session(
                        conversation_id=conversation.conversation_id,
                        user_email=recipient_email,
                        agent_id=agent_id,
                        intention="Receive messages in the main conversation.",
                    )
                    destination_session_id = root.session_id
                destination_conversation_id = conversation.conversation_id

        async with self._session_factory() as db:
            # The channel outbox lease serializes publishers for this delivery.
            projection = await db.get(AppChannelDeliveryRow, delivery_id)
            if projection is None:
                if binding is not None:
                    # This is a recipient-facing index, not another agent runtime.
                    # Lock the binding only over metadata work, never external I/O.
                    await db.get(ManagedChannelBinding, binding.binding_id, with_for_update=True)
                    contact = (
                        await db.execute(
                            select(Conversation).where(
                                Conversation.user_email == recipient_email,
                                Conversation.context_type == "web",
                                Conversation.context_ref == binding.binding_id,
                            )
                        )
                    ).scalar_one_or_none()
                    if contact is None:
                        contact = await queries.create_conversation(
                            db,
                            recipient_email,
                            agent_id,
                            "web",
                            context_ref=binding.binding_id,
                            title=await _managed_contact_title(db, binding, agent_name),
                            title_source="app_contact",
                            context_data={
                                "sender_email": sender_email,
                                "agent_name": agent_name,
                                "app_contact_binding_id": binding.binding_id,
                            },
                        )
                    destination_conversation_id = contact.conversation_id
                projection = AppChannelDeliveryRow(
                    delivery_id=delivery_id,
                    recipient_email=recipient_email,
                    sender_agent_name=agent_name[:200],
                    binding_id=outbox.managed_binding_id,
                    destination_conversation_id=destination_conversation_id,
                    destination_session_id=destination_session_id,
                )
                db.add(projection)
                await db.commit()
            destination_conversation_id = projection.destination_conversation_id
            destination_session_id = projection.destination_session_id
            agent_name = projection.sender_agent_name

        if destination_session_id and not already_published:
            with scoped_runtime_context(
                user_email=recipient_email,
                agent_id=agent_id,
                agent_owner_email=agent_owner,
                access_context=RuntimeAccessContext(
                    user_email=recipient_email, agent_owner_email=agent_owner
                ),
            ):
                async with self._session_factory() as db:
                    destination = await queries.get_session_row(db, destination_session_id)
                if destination is None:
                    raise ValueError("App destination session unavailable")
                if not destination.intaris_session_id:
                    raise RuntimeError("Recipient event store session unavailable")
                appended = await self._providers.guardrails.record_events(
                    session_id=destination.intaris_session_id,
                    events=with_session_events_turn_id(
                        [
                            SessionEvent(
                                type="assistant_message",
                                timestamp=_utc(outbox.created_at),
                                data={
                                    "role": "assistant",
                                    "content": f"Message from {agent_name} on behalf of {sender_email}:\n\n{text}"
                                    + (
                                        f"\n\n[View attachments](/contacts/{delivery_id})"
                                        if attachments
                                        else ""
                                    ),
                                    "source": "app_contact",
                                    "channel": "web",
                                    "sender": sender_email,
                                    "untrusted": True,
                                    "message_metadata": message_metadata(
                                        ts=_utc(outbox.created_at),
                                        channel="web",
                                        sender=sender_email,
                                        untrusted=True,
                                    ),
                                    "app_delivery_id": delivery_id,
                                    "attachments": [],
                                },
                            )
                        ],
                        None,
                    ),
                    idempotency_key=f"{destination.intaris_session_id}:app_contact:{delivery_id}",
                    user_email=recipient_email,
                    agent_id=agent_id,
                    agent_owner_email=sender_email,
                )
                if not appended.ok:
                    raise RuntimeError("Recipient message was not persisted")
            if self._session_cache is not None:
                await self._session_cache.invalidate_canonical(destination_session_id)

        now = datetime.now(UTC)
        contact_id = outbox.managed_binding_id or delivery_id
        href = (
            f"/chat/{destination_conversation_id}"
            if destination_conversation_id
            else f"/contacts/{contact_id}"
        )
        async with self._session_factory() as db:
            projection = await db.get(AppChannelDeliveryRow, delivery_id)
            assert projection is not None
            projection.published_at = projection.published_at or now
            inbox_id = f"inbox_app_{uuid.uuid5(uuid.NAMESPACE_URL, delivery_id).hex}"
            notification = None
            if not outbox.managed_binding_id and not destination_conversation_id:
                notification = await db.get(InboxNotificationRow, inbox_id)
            if (
                notification is None
                and not outbox.managed_binding_id
                and not destination_conversation_id
            ):
                notification = stage_notification(
                    db,
                    user_email=recipient_email,
                    kind="agent_contact",
                    title=f"Message from {agent_name}",
                    body=f"On behalf of {sender_email}. " + f"No return route.\n\n{text}",
                    href=href,
                )
                notification.notification_id = inbox_id
            if destination_conversation_id and not already_published:
                await queries.mark_conversation_unread(db, destination_conversation_id)
                contact = await db.get(Conversation, destination_conversation_id)
                if contact is not None:
                    contact.last_message_at = now
                    contact.updated_at = now
                if outbox.managed_binding_id and not await db.scalar(
                    select(AppChannelDeliveryRow.delivery_id)
                    .where(
                        AppChannelDeliveryRow.binding_id == outbox.managed_binding_id,
                        AppChannelDeliveryRow.delivery_id != delivery_id,
                        AppChannelDeliveryRow.published_at.is_not(None),
                    )
                    .limit(1)
                ):
                    await queries.touch_sidebar_metadata_revision(db, recipient_email)
            await db.commit()
        await publish_notification(self._event_bus, notification)
        if destination_conversation_id:
            await self._event_bus.publish(
                Event(
                    type=EventType.CONVERSATION_UPDATED,
                    data={
                        "conversation_id": destination_conversation_id,
                        "user_email": recipient_email,
                    },
                )
            )
        return delivery_id

    async def view(self, contact_id: str, recipient_email: str) -> AppContactView:
        """Authorize an exact recipient and project delivered traffic only."""
        async with self._session_factory() as db:
            publications = list(
                (
                    await db.execute(
                        select(AppChannelDeliveryRow, ChannelDeliveryOutboxRow)
                        .join(ChannelDeliveryOutboxRow)
                        .where(
                            AppChannelDeliveryRow.recipient_email == recipient_email,
                            AppChannelDeliveryRow.published_at.is_not(None),
                            (
                                or_(
                                    AppChannelDeliveryRow.binding_id == contact_id,
                                    AppChannelDeliveryRow.delivery_id == contact_id,
                                )
                            ),
                        )
                        .order_by(
                            AppChannelDeliveryRow.published_at, AppChannelDeliveryRow.delivery_id
                        )
                    )
                ).all()
            )
            if not publications:
                raise LookupError("Contact not found")
            first, first_outbox = publications[0]
            account = await queries.get_channel_account(db, first_outbox.account_id)
            agent = await queries.get_agent(db, account.agent_id) if account else None
            if account is None or agent is None:
                raise LookupError("Contact not found")
            binding = (
                await db.get(ManagedChannelBinding, first.binding_id) if first.binding_id else None
            )
            blocked = await db.get(AppContactBlockRow, (recipient_email, first_outbox.user_email))
            recipient_conversation = (
                await db.get(Conversation, first.destination_conversation_id)
                if first.destination_conversation_id
                else None
            )
            title = (
                recipient_conversation.title
                if recipient_conversation and recipient_conversation.title
                else await _managed_contact_title(db, binding, first.sender_agent_name)
                if binding
                else "Message"
            )
            active = (
                binding is not None
                and binding.chat_id == recipient_email
                and binding.sender_id == recipient_email
                and binding.active_route_key is not None
                and _utc(binding.expires_at) > datetime.now(UTC)
                and account.enabled
                and agent.status == "active"
                and not blocked
            )
            messages = [
                AppContactMessage(
                    id=outbound.delivery_id,
                    direction="incoming",
                    content=outbound.fallback_text or "",
                    occurred_at=publication.published_at,
                    attachments=[
                        AppContactAttachment(
                            artifact_id=raw["artifact_id"],
                            filename=raw.get("filename") or raw["artifact_id"],
                            href=f"/api/v1/app-contacts/messages/{outbound.delivery_id}/attachments/{raw['artifact_id']}",
                        )
                        for raw in (outbound.attachments_json or [])
                        if isinstance(raw, dict) and isinstance(raw.get("artifact_id"), str)
                    ],
                )
                for publication, outbound in publications
            ]
            if binding is not None:
                inbound = (
                    (
                        await db.execute(
                            select(ChannelInboundLedgerRow).where(
                                ChannelInboundLedgerRow.binding_id == binding.binding_id,
                                ChannelInboundLedgerRow.sender_id == recipient_email,
                            )
                        )
                    )
                    .scalars()
                    .all()
                )
                messages.extend(
                    AppContactMessage(
                        id=row.inbound_id,
                        direction="outgoing",
                        content=row.content,
                        occurred_at=row.occurred_at,
                    )
                    for row in inbound
                )
            messages.sort(key=lambda message: (_utc(message.occurred_at), message.id))
            return AppContactView(
                id=first.binding_id or first.delivery_id,
                conversation_id=first.destination_conversation_id,
                agent_name=agent.display_name or agent.name,
                sender_email=first_outbox.user_email,
                title=title,
                state=binding.state if binding else "one_shot",
                expires_at=binding.expires_at if binding else None,
                can_reply=bool(active),
                blocked=bool(blocked),
                main_conversation_id=first.destination_conversation_id if not binding else None,
                messages=messages,
            )

    async def reply(
        self, contact_id: str, recipient_email: str, content: str, client_message_id: str
    ) -> str:
        """Route an authenticated participant reply through managed admission."""
        view = await self.view(contact_id, recipient_email)
        if not view.can_reply:
            raise ValueError("This contact no longer accepts replies")
        async with self._session_factory() as db:
            binding = await db.get(ManagedChannelBinding, contact_id)
            assert binding is not None
            recipient = await db.get(User, recipient_email)
            assert recipient is not None
            message = InboundMessage(
                channel_type="web",
                account_id=binding.account_id,
                sender_id=recipient_email,
                sender_name=recipient.name or recipient_email,
                chat_id=recipient_email,
                chat_type="direct",
                thread_id=binding.thread_key or None,
                message_id=f"app_reply_{uuid.uuid5(uuid.NAMESPACE_URL, contact_id + ':' + client_message_id).hex}",
                content=content,
                timestamp=datetime.now(UTC),
                platform_data={"app_binding_id": contact_id},
            )
            owner_email = binding.user_email
            link = await db.get(ManagedConversationLink, binding.link_id)
            assert link is not None
            agent_id = link.target_agent_id
        # The authenticated recipient is a participant, not the runtime owner.
        with scoped_runtime_context(
            user_email=owner_email,
            agent_id=agent_id,
            access_context=RuntimeAccessContext(
                user_email=owner_email,
                agent_owner_email=owner_email,
            ),
        ):
            admission = await self._managed_channels.admit_inbound(message, user_email=owner_email)
            if admission is None:
                raise ValueError("This contact no longer accepts replies")
            if not isinstance(admission, bool):
                error = await self._managed_channels.submit_participant_admission(
                    message, admission
                )
                if error is not None:
                    raise ValueError(error.message)
            return "accepted"

    async def end(self, contact_id: str, recipient_email: str, *, block: bool) -> None:
        """Refuse this exchange, optionally refusing future sender contact."""
        view = await self.view(contact_id, recipient_email)
        async with self._session_factory() as db:
            if block and not await db.get(AppContactBlockRow, (recipient_email, view.sender_email)):
                db.add(
                    AppContactBlockRow(
                        recipient_email=recipient_email, sender_email=view.sender_email
                    )
                )
            await db.commit()
        if view.expires_at is not None:
            await self._managed_channels.end_app_contact(
                binding_id=contact_id, recipient_email=recipient_email
            )


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


async def observe_app_recipient(
    db: AsyncSession, *, accounts: list[ChannelAccountRow], user_email: str, query: str
) -> None:
    """Resolve an exact active user without exposing a browsable user directory."""
    from sqlalchemy import func

    if "@" not in query:
        return
    recipient = (
        await db.execute(
            select(User).where(
                func.lower(User.email) == query.strip().lower(), User.is_active.is_(True)
            )
        )
    ).scalar_one_or_none()
    if recipient is None:
        return
    if await db.get(AppContactBlockRow, (recipient.email, user_email)) is not None:
        return
    for account in accounts:
        if account.channel_type != "web" or not account.enabled:
            continue
        await queries.upsert_channel_observed_target(
            db,
            user_email=user_email,
            account_id=account.account_id,
            channel_type="web",
            chat_id=recipient.email,
            sender_id=recipient.email,
            chat_kind="direct",
            display_name=recipient.name or recipient.email,
        )
    await db.commit()
