from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from cognis.core.command_notices import persist_command_system_notice
from cognis.core.commands import CommandResult


@pytest.mark.asyncio
async def test_persist_command_system_notice_records_profile_switch() -> None:
    append_result = SimpleNamespace(ok=True, count=1)
    guardrails = SimpleNamespace(record_events=AsyncMock(return_value=append_result))
    session_cache = SimpleNamespace(append_recorded_events=AsyncMock())
    result = CommandResult(
        type="system_message",
        text="Agent profile switched to: developer-senior",
        data={"command": "/profile", "resolved_agent_profile_id": "developer-senior"},
    )
    session = SimpleNamespace(session_id="sess-1", intaris_session_id="intaris-1")
    agent = SimpleNamespace(agent_id="agent-1", owner_email="owner@example.com")

    persisted = await persist_command_system_notice(
        conversation_id="conv-1",
        result=result,
        providers=SimpleNamespace(guardrails=guardrails),
        session_cache=session_cache,
        session=session,
        agent=agent,
        user_email="user@example.com",
    )

    notice_id = result.data["notice_id"]
    assert persisted is True
    assert isinstance(notice_id, str)
    assert notice_id.startswith("command:profile:")
    guardrails.record_events.assert_awaited_once()
    kwargs = guardrails.record_events.await_args.kwargs
    assert kwargs["session_id"] == "intaris-1"
    assert kwargs["idempotency_key"] == f"intaris-1:command_system_notice:{notice_id}"
    event = kwargs["events"][0]
    assert event.data == {
        "command": "/profile",
        "resolved_agent_profile_id": "developer-senior",
        "notice_id": notice_id,
        "event": "system_notice",
        "message": "Agent profile switched to: developer-senior",
        "content": "Agent profile switched to: developer-senior",
        "text": "Agent profile switched to: developer-senior",
        "kind": "command_result",
        "scope": "session",
        "session_id": "sess-1",
    }
    session_cache.append_recorded_events.assert_awaited_once_with(session, [event], append_result)


@pytest.mark.asyncio
async def test_persist_command_system_notice_skips_non_runtime_commands() -> None:
    guardrails = SimpleNamespace(record_events=AsyncMock())
    result = CommandResult(
        type="system_message", text="Available skills", data={"command": "/skill"}
    )

    persisted = await persist_command_system_notice(
        conversation_id="conv-1",
        result=result,
        providers=SimpleNamespace(guardrails=guardrails),
        session_cache=None,
        session=SimpleNamespace(session_id="sess-1", intaris_session_id="intaris-1"),
        agent=SimpleNamespace(agent_id="agent-1", owner_email="owner@example.com"),
        user_email="user@example.com",
    )

    assert "notice_id" not in result.data
    assert persisted is True
    guardrails.record_events.assert_not_awaited()


@pytest.mark.asyncio
async def test_persist_command_system_notice_returns_false_when_recording_fails() -> None:
    guardrails = SimpleNamespace(record_events=AsyncMock(side_effect=RuntimeError("unavailable")))

    persisted = await persist_command_system_notice(
        conversation_id="conv-1",
        result=CommandResult(
            type="system_message",
            text="Agent profile switched to: developer-senior",
            data={"command": "/profile"},
        ),
        providers=SimpleNamespace(guardrails=guardrails),
        session_cache=None,
        session=SimpleNamespace(session_id="sess-1", intaris_session_id="intaris-1"),
        agent=SimpleNamespace(agent_id="agent-1", owner_email="owner@example.com"),
        user_email="user@example.com",
    )

    assert persisted is False
