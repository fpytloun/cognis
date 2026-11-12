"""Recipient-owned preferred channel destinations and in-app fallback."""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from cognis.store.queries import (
    get_agent_direct_conversation,
    get_conversation,
    get_conversation_channel_route,
    get_latest_active_conversation_for_channel_account,
    get_preferred_channel_account_for_agent,
)


async def preferred_channel_target(
    session: AsyncSession, *, user_email: str, agent_id: str
) -> str | None:
    """Resolve this user's active preferred route, never a result execution chat."""
    account = await get_preferred_channel_account_for_agent(
        session, user_email=user_email, agent_id=agent_id
    )
    if account is not None and account.channel_type != "web":
        default = (
            await get_conversation(session, account.default_conversation_id)
            if account.default_conversation_id
            else None
        )
        latest = await get_latest_active_conversation_for_channel_account(
            session,
            user_email=user_email,
            agent_id=agent_id,
            account_id=account.account_id,
            prefer_unthreaded=True,
        )
        for candidate in (default, latest):
            if (
                candidate is None
                or candidate.status != "active"
                or candidate.user_email != user_email
                or candidate.agent_id != agent_id
                or (candidate.context_data or {}).get("kind") == "project_task_results"
            ):
                continue
            route = await get_conversation_channel_route(session, candidate.conversation_id)
            if route is not None and route[1] == account.account_id and route[4] == user_email:
                return candidate.conversation_id
    direct = await get_agent_direct_conversation(session, user_email, agent_id)
    return direct.conversation_id if direct is not None else None
