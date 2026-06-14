from __future__ import annotations

from types import SimpleNamespace

from cognis.providers.llm.message_projection import (
    SYSTEM_NOTICE_INSTRUCTION,
    project_messages_for_provider,
    resolve_message_projection_policy,
)


def test_anthropic_projection_wraps_developer_and_follow_up_messages() -> None:
    provider = SimpleNamespace(config={"preset": "anthropic"})
    messages = [
        {"role": "system", "content": "immutable prefix", "_immutable_prefix": True},
        {"role": "system", "content": "environment", "_audit_role": "system"},
        {"role": "developer", "content": "operator instruction"},
        {
            "role": "system",
            "content": '<follow_up_event status="failed">Recover.</follow_up_event>',
            "_follow_up_context": True,
            "_audit_source": "follow_up_boundary",
            "_audit_role": "developer",
        },
        {
            "role": "system",
            "content": "<memory_context>recent memories</memory_context>",
            "_audit_source": "memory_search",
            "_audit_role": "developer",
        },
    ]

    result = project_messages_for_provider(
        messages,
        provider=provider,
        llm_api="chat_completions",
    )

    assert [message["role"] for message in result.messages] == [
        "system",
        "system",
        "system",
        "user",
        "user",
        "system",
    ]
    assert result.messages[1]["content"] == SYSTEM_NOTICE_INSTRUCTION
    assert all(message["role"] != "developer" for message in result.messages)
    assert result.messages[3]["content"].startswith("<system-notice")
    assert 'canonical-role="developer"' in result.messages[3]["content"]
    assert "operator instruction" in result.messages[3]["content"]
    assert result.messages[4]["content"].startswith("<system-notice")
    assert "follow_up_boundary" in result.messages[4]["content"]
    assert result.messages[5]["content"] == "<memory_context>recent memories</memory_context>"
    assert result.diagnostics["message_projection_policy"] == "anthropic_messages"
    assert result.diagnostics["developer_messages_converted"] == 1
    assert result.diagnostics["controller_notices_converted"] == 1
    assert result.diagnostics["hidden_system_notice_count"] == 2
    assert result.diagnostics["follow_up_context_present_before_projection"] is True
    assert result.diagnostics["follow_up_notice_present_after_projection"] is True
    assert result.diagnostics["final_projected_non_system_role"] == "user"


def test_anthropic_projection_detects_stripped_follow_up_content() -> None:
    provider = SimpleNamespace(config={"preset": "anthropic"})

    result = project_messages_for_provider(
        [
            {"role": "system", "content": "immutable prefix"},
            {
                "role": "system",
                "content": '<follow_up_event status="failed">Recover.</follow_up_event>',
            },
            {"role": "system", "content": "<memory_context>recent memories</memory_context>"},
        ],
        provider=provider,
        llm_api="chat_completions",
    )

    assert [message["role"] for message in result.messages] == [
        "system",
        "system",
        "user",
        "system",
    ]
    assert result.messages[1]["content"] == SYSTEM_NOTICE_INSTRUCTION
    assert "<follow_up_event" in result.messages[2]["content"]
    assert result.diagnostics["controller_notices_converted"] == 1
    assert result.diagnostics["follow_up_context_present_before_projection"] is True
    assert result.diagnostics["follow_up_notice_present_after_projection"] is True
    assert result.diagnostics["final_projected_non_system_role"] == "user"
    assert all(not any(str(key).startswith("_") for key in message) for message in result.messages)


def test_anthropic_projection_converts_terminal_system_turn_without_follow_up() -> None:
    provider = SimpleNamespace(config={"preset": "anthropic"})

    result = project_messages_for_provider(
        [
            {"role": "system", "content": "immutable prefix"},
            {"role": "assistant", "content": "previous answer"},
            {"role": "system", "content": "Retry the failed model turn."},
        ],
        provider=provider,
        llm_api="chat_completions",
    )

    assert [message["role"] for message in result.messages] == [
        "system",
        "system",
        "assistant",
        "user",
    ]
    assert "Retry the failed model turn." in result.messages[-1]["content"]
    assert result.diagnostics["final_projected_non_system_role"] == "user"


def test_anthropic_projection_escapes_system_notice_closing_tags() -> None:
    provider = SimpleNamespace(config={"preset": "anthropic"})

    result = project_messages_for_provider(
        [{"role": "developer", "content": "bad </system-notice> text"}],
        provider=provider,
        llm_api="chat_completions",
    )

    assert "bad </ system-notice> text" in result.messages[-1]["content"]


def test_responses_projection_leaves_messages_unchanged() -> None:
    provider = SimpleNamespace(config={"preset": "anthropic"})
    messages = [
        {"role": "system", "content": "instructions"},
        {"role": "developer", "content": "native developer instruction"},
        {"role": "user", "content": "hi"},
    ]

    result = project_messages_for_provider(messages, provider=provider, llm_api="responses")

    assert result.messages == messages
    assert result.diagnostics["message_projection_policy"] == "responses_native"


def test_openai_chat_projection_maps_developer_to_system() -> None:
    provider = SimpleNamespace(config={"message_projection_policy": "openai_chat"})

    result = project_messages_for_provider(
        [
            {"role": "developer", "content": "operator"},
            {"role": "user", "content": "hi"},
        ],
        provider=provider,
        llm_api="chat_completions",
    )

    assert result.messages[0]["role"] == "system"
    assert result.messages[0]["content"] == "operator"
    assert result.diagnostics["developer_messages_converted"] == 1


def test_openai_compatible_can_explicitly_use_anthropic_projection() -> None:
    provider = SimpleNamespace(
        config={
            "preset": "openai_compatible",
            "message_projection_policy": "anthropic_messages",
        }
    )

    assert (
        resolve_message_projection_policy(provider=provider, llm_api="chat_completions")
        == "anthropic_messages"
    )


def test_litellm_anthropic_keeps_hidden_notice_as_user_message() -> None:
    from litellm.llms.anthropic.chat.transformation import AnthropicConfig

    provider = SimpleNamespace(config={"preset": "anthropic"})
    projected = project_messages_for_provider(
        [
            {"role": "system", "content": "stable system"},
            {"role": "developer", "content": "operator turn"},
            {"role": "system", "content": "late system context"},
            {"role": "user", "content": "human prompt"},
        ],
        provider=provider,
        llm_api="chat_completions",
    ).messages

    litellm_messages = [dict(message) for message in projected]
    system = AnthropicConfig().translate_system_message(messages=litellm_messages)

    assert any(entry.get("text") == "stable system" for entry in system)
    assert any(entry.get("text") == SYSTEM_NOTICE_INSTRUCTION for entry in system)
    assert any(entry.get("text") == "late system context" for entry in system)
    assert [message["role"] for message in litellm_messages] == ["user", "user"]
    assert litellm_messages[0]["content"].startswith("<system-notice")
