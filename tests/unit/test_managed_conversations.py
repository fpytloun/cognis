from cognis.core.managed_conversations import (
    is_allowed_managed_conversation_target,
    managed_conversation_target_error,
)
from cognis.tools.builtin.orchestration import AGENT_CONVERSATION_CREATE_TOOL


def test_managed_conversation_allows_primary_agent_targets() -> None:
    assert is_allowed_managed_conversation_target("laforge")
    assert is_allowed_managed_conversation_target("lumi")


def test_managed_conversation_rejects_system_agent_targets() -> None:
    assert not is_allowed_managed_conversation_target("system:implement")
    assert not is_allowed_managed_conversation_target("system:explore")

    message = managed_conversation_target_error("system:implement")

    assert "primary/user agent" in message
    assert "delegate()" in message
    assert "system:*" in message


def test_managed_conversation_create_tool_guidance_rejects_system_agents() -> None:
    description = AGENT_CONVERSATION_CREATE_TOOL.description

    assert "primary/user agents" in description
    assert "delegate()" in description
    assert "system:*" in description
