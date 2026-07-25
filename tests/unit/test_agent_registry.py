from __future__ import annotations

from cognis.core.agent_registry import SYSTEM_AGENTS
from cognis.core.system_skills import get_system_skill_default


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
    skills = agent.skills or {}

    assert builtin_tools == [
        "read",
        "write",
        "edit",
        "multiedit",
        "apply_patch",
        "grep",
        "glob",
        "list_directory",
        "bash",
    ]
    assert skills == {"items": [{"skill_id": "cognis-coding", "enabled": True}]}
    assert "Make the smallest correct change" in agent.system_prompt
    assert "one bounded implementation scope" in agent.system_prompt
    assert "granular todos for implementation" in agent.system_prompt
    assert "fix\n  the issue and rerun" in agent.system_prompt
    assert "unrelated pre-existing reason" in agent.system_prompt
    assert "Do not delegate implementation work further" in agent.system_prompt


def test_system_research_agent_has_expanded_web_tools() -> None:
    agent = SYSTEM_AGENTS["system:research"]
    tools = agent.tools or {}

    assert tools.get("builtin_tools") == [
        "read",
        "grep",
        "glob",
        "web_search",
        "web_fetch",
        "web_crawl",
        "web_map",
        "web_research",
    ]
    assert "Separate repo-local findings from external findings" in agent.system_prompt
    assert "web_research" in agent.system_prompt
    assert "Adapt depth to the request" in agent.system_prompt
    assert "Relevant media, diagrams, or artifacts" in agent.system_prompt


def test_system_explore_agent_uses_real_listing_tool() -> None:
    agent = SYSTEM_AGENTS["system:explore"]
    tools = agent.tools or {}

    assert tools.get("builtin_tools") == ["read", "grep", "glob", "list_directory", "bash"]


def test_system_review_agents_use_pragmatic_prompts() -> None:
    architect = SYSTEM_AGENTS["system:architect"]
    review = SYSTEM_AGENTS["system:code-review"]

    assert architect.description == "Implementation plan review for architecture and risk"
    assert "security, reliability, testability" in architect.system_prompt
    assert "enterprise-style artifacts" in architect.system_prompt
    assert "observable workstreams and milestones" in architect.system_prompt
    assert "Do NOT invent requirements" in architect.system_prompt
    assert "OVERENGINEERING" in architect.system_prompt

    assert review.description == "Findings-first code review for defects and regressions"
    assert review.skills == {"items": [{"skill_id": "cognis-coding", "enabled": True}]}
    assert "Primary focus: real bugs, regressions, security issues" in review.system_prompt
    assert "Do not nitpick style or architecture" in review.system_prompt
    assert "locked to the approved review scope" in review.system_prompt
    assert "concrete bug, regression, security" in review.system_prompt
    assert "### Must Fix" in review.system_prompt


def test_system_committer_allows_explicit_publish_only() -> None:
    agent = SYSTEM_AGENTS["system:committer"]

    assert (
        "Push only when task or project instructions explicitly require publishing"
        in agent.system_prompt
    )
    assert (
        "Open a pull request only when task or project instructions explicitly require"
        in agent.system_prompt
    )
    assert "NEVER push" not in agent.system_prompt


def test_system_agents_seed_reasoning_and_override_capabilities() -> None:
    implement = SYSTEM_AGENTS["system:implement"]
    explore = SYSTEM_AGENTS["system:explore"]

    assert implement.llm_config is not None
    assert implement.llm_config.reasoning_effort == "medium"
    assert implement.allow_user_override is True
    assert implement.allow_user_disable is True
    assert "llm_config.reasoning_effort" in implement.editable_fields
    assert "skills" in implement.editable_fields

    assert explore.llm_config is not None
    assert explore.llm_config.reasoning_effort == "low"


def test_agent_manager_system_skill_is_seeded_as_guidance_only() -> None:
    skill = get_system_skill_default("cognis-agent-manager")

    assert skill is not None
    assert skill["auto_load"] is False
    assert skill["linked_tool_ids"] == ["builtin:manage_agents"]
    assert "Shared agents are use-only" in str(skill["instructions"])
