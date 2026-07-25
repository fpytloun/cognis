from __future__ import annotations

from cognis.core.runtime_metadata import assistant_message_runtime_metadata
from cognis.models.agent import AgentDefinition


def test_assistant_message_runtime_metadata_uses_resolved_turn_values() -> None:
    agent = AgentDefinition(
        agent_id="laforge",
        owner_email="owner@example.com",
        name="LaForge",
        display_name="LaForge",
    )

    metadata = assistant_message_runtime_metadata(
        agent,
        {
            "requested_agent_profile_id": "build",
            "resolved_agent_profile_id": "build",
            "agent_profile_source": "conversation",
            "agent_profile_synthetic": False,
            "resolved_provider_id": "openai",
            "resolved_model": "gpt-5.1",
            "reasoning_effort": "high",
            "reasoning_mode": "adaptive",
        },
    )

    assert metadata == {
        "agent_id": "laforge",
        "agent_name": "LaForge",
        "agent_display_name": "LaForge",
        "requested_agent_profile_id": "build",
        "agent_profile_id": "build",
        "agent_profile_source": "conversation",
        "agent_profile_synthetic": False,
        "provider_id": "openai",
        "model": "gpt-5.1",
        "reasoning_effort": "high",
        "reasoning_mode": "adaptive",
    }


def test_assistant_message_runtime_metadata_omits_empty_optional_values() -> None:
    agent = AgentDefinition(
        agent_id="laforge",
        owner_email="owner@example.com",
        name="LaForge",
    )

    metadata = assistant_message_runtime_metadata(agent, {"resolved_model": ""})

    assert metadata == {
        "agent_id": "laforge",
        "agent_name": "LaForge",
    }
