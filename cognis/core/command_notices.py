"""Durable timeline notices for slash-command feedback."""

from __future__ import annotations

import uuid
from typing import Any

from cognis.logging import get_logger
from cognis.models.session import SessionEvent

logger = get_logger(__name__)

PERSISTED_COMMAND_NOTICE_COMMANDS = frozenset({"/profile", "/model", "/thinking", "/fast"})


async def persist_command_system_notice(
    *,
    conversation_id: str,
    result: Any,
    providers: Any,
    session_cache: Any,
    session: Any,
    agent: Any,
    user_email: str,
) -> bool:
    """Persist visible runtime-setting command feedback in the session event stream."""

    text = result.text if isinstance(result.text, str) else ""
    command = result.data.get("command") if isinstance(result.data, dict) else None
    if (
        not text
        or command not in PERSISTED_COMMAND_NOTICE_COMMANDS
        or session is None
        or agent is None
        or not user_email
    ):
        return True

    notice_id = result.data.get("notice_id")
    if not isinstance(notice_id, str) or not notice_id:
        notice_id = f"command:{str(command).lstrip('/')}:{uuid.uuid4().hex}"
        result.data["notice_id"] = notice_id

    event = SessionEvent(
        type="lifecycle",
        data={
            **result.data,
            "event": "system_notice",
            "message": text,
            "content": text,
            "text": text,
            "notice_id": notice_id,
            "kind": "command_result",
            "scope": "session",
            "session_id": getattr(session, "session_id", None),
            "command": command,
        },
    )
    intaris_session_id = getattr(session, "intaris_session_id", None) or getattr(
        session, "session_id", None
    )
    if not intaris_session_id:
        return False

    try:
        append_result = await providers.guardrails.record_events(
            session_id=intaris_session_id,
            events=[event],
            source="cognis",
            idempotency_key=f"{intaris_session_id}:command_system_notice:{notice_id}",
            user_email=user_email,
            agent_id=agent.agent_id,
            agent_owner_email=getattr(agent, "owner_email", user_email),
        )
        if not append_result.ok:
            raise RuntimeError("Intaris did not persist command system notice")
        if append_result.count <= 0:
            return False
        if session_cache is not None and hasattr(session_cache, "append_recorded_events"):
            await session_cache.append_recorded_events(session, [event], append_result)
        return True
    except Exception:
        logger.warning(
            "failed to persist command system notice",
            extra={
                "extra_data": {
                    "conversation_id": conversation_id,
                    "session_id": getattr(session, "session_id", None),
                    "command": command,
                    "notice_id": notice_id,
                }
            },
            exc_info=True,
        )
        return False
