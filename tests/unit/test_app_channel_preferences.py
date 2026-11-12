from __future__ import annotations

import os

import pytest

from cognis.store.app_channels import app_channel_id
from cognis.store.database import create_engine, create_session_factory
from cognis.store.models import Base
from cognis.store.queries import (
    create_agent,
    create_channel_account,
    create_user,
    delete_channel_account,
    get_preferred_channel_account_for_agent,
    update_channel_account,
)


@pytest.mark.asyncio
@pytest.mark.parametrize("dialect", ["sqlite", "postgresql"])
async def test_preference_follows_channel_assignment_and_restores_app(tmp_path, dialect):
    url = (
        f"sqlite+aiosqlite:///{tmp_path / 'preferences.db'}"
        if dialect == "sqlite"
        else os.environ.get("COGNIS_TASK_OUTCOME_TEST_POSTGRES_URL")
    )
    if not url:
        pytest.skip("Set COGNIS_TASK_OUTCOME_TEST_POSTGRES_URL to a disposable PostgreSQL database")
    engine = create_engine(url)
    factory = create_session_factory(engine)
    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        owner = "channel-owner@example.com"
        async with factory() as session:
            await create_user(session, email=owner, name="Owner", role="user", password_hash="")
            for agent_id in ("channel-agent-a", "channel-agent-b"):
                await create_agent(session, agent_id=agent_id, name=agent_id, owner_email=owner)
            await create_channel_account(
                session,
                account_id="preferred-telegram",
                channel_type="telegram",
                display_name="Telegram",
                agent_id="channel-agent-a",
                user_email=owner,
                preferred_for_task_delivery=True,
            )
            await session.commit()
            await update_channel_account(session, "preferred-telegram", agent_id="channel-agent-b")
            await session.commit()
        async with factory() as session:
            first = await get_preferred_channel_account_for_agent(
                session, user_email=owner, agent_id="channel-agent-a"
            )
            second = await get_preferred_channel_account_for_agent(
                session, user_email=owner, agent_id="channel-agent-b"
            )
            assert first.account_id == app_channel_id(owner, "channel-agent-a")
            assert second.account_id == "preferred-telegram"
            await delete_channel_account(session, "preferred-telegram")
            await session.commit()
        async with factory() as session:
            second = await get_preferred_channel_account_for_agent(
                session, user_email=owner, agent_id="channel-agent-b"
            )
            assert second.account_id == app_channel_id(owner, "channel-agent-b")
    finally:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.drop_all)
        await engine.dispose()
