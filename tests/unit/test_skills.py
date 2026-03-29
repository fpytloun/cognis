from __future__ import annotations

from cognis.models.agent import AgentDefinition
from cognis.tools.skills import load_skill_tool_names


def _agent(skills: dict[str, object] | None) -> AgentDefinition:
    return AgentDefinition(
        agent_id="agent-a",
        owner_email="user@example.com",
        name="Agent A",
        skills=skills,
    )


def test_load_skill_tool_names_returns_inline_tool_names() -> None:
    agent = _agent(
        {
            "items": [
                {
                    "skill_id": "research-core",
                    "name": "Research Core",
                    "tool_names": ["list_agents", "get_status"],
                }
            ]
        }
    )

    assert load_skill_tool_names(agent) == {"list_agents", "get_status"}


def test_load_skill_tool_names_skips_malformed_entries() -> None:
    agent = _agent({"items": ["bad", {"skill_id": "missing-name"}, {"name": "Missing ID"}]})

    assert load_skill_tool_names(agent) == set()


def test_load_skill_tool_names_returns_empty_for_missing_skill_config() -> None:
    assert load_skill_tool_names(_agent(None)) == set()
