from __future__ import annotations

from cognis.core.agent_registry import SYSTEM_AGENTS


def test_system_implement_agent_is_registered() -> None:
    agent = SYSTEM_AGENTS["system:implement"]

    assert agent.name == "Implement"
    assert agent.description == "Focused implementation and targeted verification"
    assert agent.agent_type == "secondary"
    assert agent.is_system is True
    assert agent.hidden is False


def test_system_implement_agent_has_expected_tools_and_constraints() -> None:
    agent = SYSTEM_AGENTS["system:implement"]
    tools = agent.tools or {}
    builtin_tools = tools.get("builtin_tools")

    assert builtin_tools == [
        "read",
        "write",
        "edit",
        "multiedit",
        "patch",
        "grep",
        "glob",
        "list",
        "bash",
    ]
    assert "Make the smallest correct change" in agent.system_prompt
    assert "Do not delegate further" in agent.system_prompt
