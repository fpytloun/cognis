"""Built-in owner-scoped app channels, with no external adapter lifecycle."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import Connection, exists, inspect, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.dml import Update

from cognis.store.models import Agent, ChannelAccountRow


def app_channel_id(user_email: str, agent_id: str) -> str:
    """Return the stable identity of a real user/agent app channel."""
    return f"ch_web_{uuid.uuid5(uuid.NAMESPACE_URL, f'{user_email}:{agent_id}').hex}"


def app_channel_preference_update() -> Update:
    """Select the built-in channel when its agent has no external accounts."""
    other = ChannelAccountRow.__table__.alias("other_channels")
    return (
        update(ChannelAccountRow)
        .where(
            ChannelAccountRow.channel_type == "web",
            ChannelAccountRow.preferred_for_task_delivery.is_(False),
            ~exists(
                select(other.c.account_id).where(
                    other.c.user_email == ChannelAccountRow.user_email,
                    other.c.agent_id == ChannelAccountRow.agent_id,
                    other.c.channel_type != "web",
                )
            ),
        )
        .values(preferred_for_task_delivery=True)
    )


async def ensure_app_channel_preference(
    session: AsyncSession, *, user_email: str, agent_id: str
) -> None:
    """Reconcile the sole-channel preference inside the caller's transaction."""
    await session.execute(
        app_channel_preference_update().where(
            ChannelAccountRow.user_email == user_email,
            ChannelAccountRow.agent_id == agent_id,
        )
    )


def ensure_app_channels(connection: Connection) -> None:
    """Backfill built-in channels without replacing existing preferences."""
    if not {"agents", "channel_accounts"}.issubset(inspect(connection).get_table_names()):
        return
    dialect_insert = pg_insert if connection.dialect.name == "postgresql" else sqlite_insert
    for agent in connection.execute(
        select(Agent.agent_id, Agent.owner_email).where(
            Agent.agent_type == "primary", Agent.is_system.is_(False)
        )
    ):
        account_id = app_channel_id(agent.owner_email, agent.agent_id)
        connection.execute(
            dialect_insert(ChannelAccountRow)
            .values(
                account_id=account_id,
                channel_type="web",
                display_name="Cognis app",
                user_email=agent.owner_email,
                agent_id=agent.agent_id,
                enabled=True,
                preferred_for_task_delivery=False,
                created_at=datetime.now(UTC),
                updated_at=datetime.now(UTC),
            )
            .on_conflict_do_nothing(index_elements=["account_id"])
        )
    connection.execute(app_channel_preference_update())
