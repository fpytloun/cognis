"""PostgreSQL row-lock coverage for one-shot versus managed route admission."""

from __future__ import annotations

import asyncio
import os
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import create_async_engine

from cognis.channels.constants import (
    CHANNEL_RECIPIENT_MESSAGE_SOURCE,
    CHANNEL_TOOL_MESSAGE_SOURCE,
)
from cognis.channels.route_admission import (
    active_channel_tool_delivery_id,
    active_managed_binding_id,
    lock_channel_route,
)
from cognis.store import queries
from cognis.store.database import create_session_factory
from cognis.store.models import (
    Base,
    ChannelDeliveryOutboxRow,
    ManagedChannelBinding,
)


@pytest.mark.asyncio
@pytest.mark.parametrize("first", ["binding", "one_shot"])
@pytest.mark.parametrize(
    "source_type",
    [CHANNEL_TOOL_MESSAGE_SOURCE, CHANNEL_RECIPIENT_MESSAGE_SOURCE],
)
async def test_route_admission_has_exactly_one_winner(first: str, source_type: str) -> None:
    url = os.getenv("COGNIS_TEST_POSTGRES_URL")
    if not url:
        pytest.skip("COGNIS_TEST_POSTGRES_URL is not configured")
    schema = f"managed_route_{uuid.uuid4().hex[:12]}"
    admin = create_async_engine(url)
    async with admin.begin() as connection:
        await connection.execute(text(f'CREATE SCHEMA "{schema}"'))
    engine = create_async_engine(
        url,
        connect_args={"server_settings": {"search_path": schema, "timezone": "UTC"}},
    )
    factory = create_session_factory(engine)
    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        async with factory() as session:
            await queries.create_user(session, "owner@example.com", "Owner", "hash")
            await queries.create_agent(
                session,
                agent_id="agent",
                owner_email="owner@example.com",
                name="Agent",
            )
            account = await queries.create_channel_account(
                session,
                account_id="account",
                channel_type="signal",
                display_name="Signal",
                agent_id="agent",
                user_email="owner@example.com",
            )
            controller = await queries.create_conversation(
                session, "owner@example.com", "agent", "web", title="Controller"
            )
            target = await queries.create_conversation(
                session, "owner@example.com", "agent", "agent_work", title="Child"
            )
            link = await queries.create_managed_conversation_link(
                session,
                user_email="owner@example.com",
                controller_agent_id="agent",
                controller_conversation_id=controller.conversation_id,
                controller_session_id="controller-session",
                target_agent_id="agent",
                target_conversation_id=target.conversation_id,
                target_session_id="target-session",
                title="Child",
                kind="channel",
                completion_policy="explicit",
            )
            await session.commit()

        locked = asyncio.Event()
        release = asyncio.Event()

        async def binding_admission() -> str:
            async with factory() as session, session.begin():
                await lock_channel_route(session, account.account_id)
                locked.set()
                if first == "binding":
                    await release.wait()
                conflict = await active_channel_tool_delivery_id(
                    session,
                    user_email="owner@example.com",
                    account_id="account",
                    chat_id="chat",
                    thread_id=None,
                )
                if conflict:
                    return "one_shot"
                session.add(
                    ManagedChannelBinding(
                        binding_id="binding",
                        link_id=link.link_id,
                        user_email="owner@example.com",
                        account_id="account",
                        channel_type="signal",
                        chat_id="chat",
                        thread_key="",
                        sender_id="sender",
                        active_route_key="route",
                        state="waiting_external",
                        version=1,
                        expires_at=datetime.now(UTC) + timedelta(hours=1),
                        objective="Objective",
                        safety_guidance="Safety",
                        explicit_tool_allowlist=[],
                    )
                )
                return "binding"

        async def one_shot_admission() -> str:
            async with factory() as session, session.begin():
                await lock_channel_route(session, account.account_id)
                locked.set()
                if first == "one_shot":
                    await release.wait()
                conflict = await active_managed_binding_id(
                    session,
                    user_email="owner@example.com",
                    account_id="account",
                    chat_id="chat",
                    thread_id=None,
                )
                if conflict:
                    return "binding"
                session.add(
                    ChannelDeliveryOutboxRow(
                        delivery_id="delivery",
                        user_email="owner@example.com",
                        conversation_id=controller.conversation_id,
                        source_type=source_type,
                        source_id="request",
                        channel_type="signal",
                        account_id="account",
                        chat_id="chat",
                        fallback_text="Hello",
                        status="pending",
                    )
                )
                return "one_shot"

        winner_task = asyncio.create_task(
            binding_admission() if first == "binding" else one_shot_admission()
        )
        await locked.wait()
        loser_task = asyncio.create_task(
            one_shot_admission() if first == "binding" else binding_admission()
        )
        await asyncio.sleep(0.05)
        release.set()
        winner, loser = await asyncio.gather(winner_task, loser_task)
        assert winner == first
        assert loser == first

        async with factory() as session:
            binding_count = await session.scalar(
                select(func.count(ManagedChannelBinding.binding_id))
            )
            delivery_count = await session.scalar(
                select(func.count(ChannelDeliveryOutboxRow.delivery_id))
            )
        assert (binding_count, delivery_count) == ((1, 0) if first == "binding" else (0, 1))
    finally:
        await engine.dispose()
        async with admin.begin() as connection:
            await connection.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        await admin.dispose()
