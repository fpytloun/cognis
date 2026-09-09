from types import SimpleNamespace

import pytest

import cognis.core.managed_conversations as managed_conversations_module
from cognis.core.managed_conversations import (
    ManagedConversationProgressObserver,
    _last_user_message_from_events,
    is_allowed_managed_conversation_target,
    last_managed_conversation_user_message_for_retry,
    managed_conversation_target_error,
)
from cognis.models.session import EventReadResult
from cognis.tools.builtin.orchestration import AGENT_CONVERSATION_CREATE_TOOL


def test_managed_conversation_allows_primary_agent_targets() -> None:
    assert is_allowed_managed_conversation_target("laforge")
    assert is_allowed_managed_conversation_target("lumi")


def test_managed_conversation_syntactic_check_does_not_infer_agent_type_from_id() -> None:
    assert is_allowed_managed_conversation_target("system:implement")
    assert is_allowed_managed_conversation_target("system:explore")
    assert not is_allowed_managed_conversation_target("")

    message = managed_conversation_target_error("system:implement")

    assert "primary, non-system agent" in message
    assert "delegate()" in message


def test_managed_conversation_create_tool_guidance_rejects_system_agents() -> None:
    description = AGENT_CONVERSATION_CREATE_TOOL.description

    assert "primary/user agents" in description
    assert "delegate()" in description
    assert "system:*" in description


def test_managed_conversation_retry_message_preserves_one_shot_mode() -> None:
    message = _last_user_message_from_events(
        [
            {
                "type": "user_message",
                "data": {
                    "content": "older",
                    "chat_mode": "plan",
                    "chat_mode_source": "one_shot",
                },
            },
            {
                "type": "user_message",
                "data": {
                    "content": "retry this",
                    "chat_mode": "build",
                    "chat_mode_source": "one_shot",
                },
            },
        ]
    )

    assert message is not None
    assert message.content == "retry this"
    assert message.one_shot_chat_mode == "build"


def test_managed_conversation_retry_message_does_not_stick_non_one_shot_mode() -> None:
    message = _last_user_message_from_events(
        [
            {
                "type": "user_message",
                "data": {
                    "content": "retry this",
                    "chat_mode": "build",
                    "chat_mode_source": "conversation_override",
                },
            },
        ]
    )

    assert message is not None
    assert message.content == "retry this"
    assert message.one_shot_chat_mode is None


@pytest.mark.asyncio
async def test_managed_retry_reads_long_history_without_using_stream_high_water(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _candidates(*_: object, **__: object) -> list[SimpleNamespace]:
        return [SimpleNamespace(session_id="session-1", intaris_session_id="intaris-1")]

    monkeypatch.setattr(managed_conversations_module, "_candidate_sessions", _candidates)

    class _Guardrails:
        cursors: list[int] = []

        async def read_events(self, **kwargs: object) -> EventReadResult:
            after_seq = int(kwargs["after_seq"])
            self.cursors.append(after_seq)
            if after_seq == 0:
                return EventReadResult(
                    events=[
                        {
                            "seq": 500,
                            "type": "user_message",
                            "data": {"content": "older"},
                        }
                    ],
                    last_seq=1201,
                    has_more=True,
                )
            assert after_seq == 500
            return EventReadResult(
                events=[
                    {
                        "seq": 1201,
                        "type": "user_message",
                        "data": {"content": "latest"},
                    }
                ],
                last_seq=1201,
                has_more=False,
            )

    guardrails = _Guardrails()
    message = await last_managed_conversation_user_message_for_retry(
        session_cache=SimpleNamespace(get_cached_events=lambda _: []),
        guardrails=guardrails,
        session_factory=lambda: None,
        link=SimpleNamespace(),
    )

    assert message is not None
    assert message.content == "latest"
    assert guardrails.cursors == [0, 500]


@pytest.mark.asyncio
async def test_managed_progress_observer_forwards_todos_and_tool_activity() -> None:
    progress: list[dict[str, object]] = []

    async def publish(**payload: object) -> None:
        progress.append(payload)

    observer = ManagedConversationProgressObserver(publish)
    await observer.on_tool_call("conv", "sess", "call-1", "read", {}, "turn-1")
    await observer.on_tool_result(
        "conv",
        "sess",
        "call-2",
        "step_todo_write",
        '{"todos":[{"content":"Implement fix","status":"completed"},{"content":"Run tests","status":"in_progress"}]}',
        False,
        1,
        None,
    )

    assert progress[0] == {"tool_call_count": 1, "last_tool": "read", "todos": []}
    assert progress[1] == {
        "tool_call_count": 2,
        "last_tool": "step_todo_write",
        "todos": [
            {"content": "Implement fix", "status": "completed"},
            {"content": "Run tests", "status": "in_progress"},
        ],
    }

    await observer.on_turn_complete(object())
    await observer.on_turn_error("conv", RuntimeError("failed"))

    assert progress[2]["last_tool"] is None
    assert progress[2]["tool_call_count"] == 2
    assert progress[2]["todos"] == progress[1]["todos"]
    assert progress[3] == progress[2]
