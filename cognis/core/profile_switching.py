"""Shared persistence for user- and agent-initiated runtime profile switches."""

from __future__ import annotations

from typing import Any

from cognis.core.runtime_selection import RuntimeSelectionPlan


async def persist_agent_profile_switch(
    *,
    session_factory: Any,
    session_cache: Any,
    conversation: Any,
    session: Any,
    profile_id: str,
    persist_conversation: bool,
    runtime_plan: RuntimeSelectionPlan | None = None,
) -> None:
    """Persist a validated profile selection and clear inference overrides."""

    from cognis.core.runtime_selection import persist_runtime_selection

    await persist_runtime_selection(
        session_factory=session_factory,
        session_cache=session_cache,
        conversation=conversation,
        session=session,
        profile_id=profile_id,
        clear_overrides=True,
        persist_conversation_profile=persist_conversation,
        require_active_session=persist_conversation,
        runtime_plan=runtime_plan,
    )
    if runtime_plan is not None:
        return
    update_tool_runtime_info = getattr(session_cache, "update_tool_runtime_info", None)
    if callable(update_tool_runtime_info):
        update_tool_runtime_info(session.session_id, None)
