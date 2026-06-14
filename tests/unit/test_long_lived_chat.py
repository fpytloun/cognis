from __future__ import annotations

from cognis.core.long_lived_chat import (
    is_agent_direct_context_ref,
    is_channel_context_type,
    is_long_lived_chat_context,
    is_web_main_chat_context,
    is_web_main_chat_context_ref,
)
from cognis.models.session import ConversationContext


def test_web_agent_direct_by_platform_data_is_long_lived() -> None:
    assert is_long_lived_chat_context(
        ConversationContext(
            type="web",
            ref="web:topic:abc",
            platform_data={"kind": "agent_direct"},
        )
    )


def test_web_agent_direct_by_context_ref_is_long_lived() -> None:
    assert is_agent_direct_context_ref("web:agent_direct:user@example.com:agent-1")
    assert is_long_lived_chat_context(
        ConversationContext(type="web", ref="web:agent_direct:user@example.com:agent-1")
    )


def test_web_main_chat_by_context_ref_is_long_lived() -> None:
    assert is_web_main_chat_context_ref("web:user:user@example.com:default")
    context = ConversationContext(type="web", ref="web:user:user@example.com:default")
    assert is_web_main_chat_context(context)
    assert is_long_lived_chat_context(context)


def test_external_channel_context_is_long_lived() -> None:
    assert is_channel_context_type("signal")
    assert is_long_lived_chat_context(
        ConversationContext(
            type="signal",
            ref="signal:chat-1",
            platform_data={"channel_type": "signal"},
        )
    )


def test_normal_web_topic_context_is_not_long_lived() -> None:
    assert not is_long_lived_chat_context(
        ConversationContext(type="web", ref="web:topic:abc", platform_data={"topic_id": "abc"})
    )


def test_non_ambient_contexts_are_not_long_lived() -> None:
    for context_type in ("task", "direct", "api", "chat", ""):
        assert not is_long_lived_chat_context(ConversationContext(type=context_type))
