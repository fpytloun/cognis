"""Shared persistence for user- and agent-initiated runtime profile switches."""

from __future__ import annotations

from typing import Any


async def persist_agent_profile_switch(
    *,
    session_factory: Any,
    session_cache: Any,
    conversation: Any,
    session: Any,
    profile_id: str,
    persist_conversation: bool,
) -> None:
    """Persist a validated profile selection and clear inference overrides."""

    from cognis.store.queries import (
        set_conversation_agent_profile_id,
        set_session_agent_profile_id,
    )

    async with session_factory() as db_session:
        try:
            if persist_conversation:
                await set_conversation_agent_profile_id(
                    db_session,
                    conversation.conversation_id,
                    profile_id,
                )
            await set_session_agent_profile_id(
                db_session,
                session.session_id,
                profile_id,
            )
            await db_session.commit()
        except Exception:
            await db_session.rollback()
            raise

    if persist_conversation:
        conversation.agent_profile_id = profile_id
    session.agent_profile_id = profile_id
    session_cache.set_model_override(session.session_id, None)
    session_cache.set_reasoning_effort_override(session.session_id, None)
    set_fast_mode_override = getattr(session_cache, "set_fast_mode_override", None)
    if callable(set_fast_mode_override):
        set_fast_mode_override(session.session_id, None)
    update_tool_runtime_info = getattr(session_cache, "update_tool_runtime_info", None)
    if callable(update_tool_runtime_info):
        update_tool_runtime_info(session.session_id, None)
