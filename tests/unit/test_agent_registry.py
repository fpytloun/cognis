from __future__ import annotations

from cognis.core.agent_registry import SYSTEM_AGENTS
from cognis.core.orchestration_targets import OrchestrationTarget, OrchestrationTargetSnapshot
from cognis.core.system_skills import get_system_skill_default
from cognis.tools.builtin.orchestration import DELEGATE_TOOL, enrich_orchestration_target_catalog


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
        "lsp",
        "list_directory",
        "bash",
    ]
    assert skills == {
        "items": [{"skill_id": "cognis-coding", "enabled": True, "auto_load_instructions": True}]
    }
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
    assert "Include media or artifacts when" in agent.system_prompt
    assert "return usable artifact IDs\n  or source URLs and provenance" in agent.system_prompt


def test_system_explore_agent_uses_real_listing_tool() -> None:
    agent = SYSTEM_AGENTS["system:explore"]
    tools = agent.tools or {}

    assert tools.get("builtin_tools") == ["read", "grep", "glob", "lsp", "list_directory", "bash"]


def test_specialist_outputs_keep_role_evidence_without_generic_report_forms() -> None:
    explore = SYSTEM_AGENTS["system:explore"].system_prompt
    research = SYSTEM_AGENTS["system:research"].system_prompt
    implement = SYSTEM_AGENTS["system:implement"].system_prompt
    assert "Answer the caller's question with source references" in explore
    assert "only when they help explain the answer" in explore
    assert "Cite source URLs" in research
    assert "scale the structure to the request" in research
    assert "modified file references, checks and outcomes" in implement
    assert "Scale the structure to the assignment" in implement
    for prompt in (explore, research, implement):
        assert "Return a structured summary" not in prompt
        assert "Return structured research findings" not in prompt
        assert "Return:\n- What changed" not in prompt


def test_system_explore_agent_defines_general_read_only_evidence_role() -> None:
    agent = SYSTEM_AGENTS["system:explore"]
    prompt = agent.system_prompt

    assert "Lightweight read-only exploration" in agent.description
    assert "documents, files, configuration, and code" in agent.description
    assert "leaving final decisions to the caller" in agent.description
    assert "documents, files, configuration, and code" in prompt
    assert "commands that change system state" in prompt
    assert "evidence-backed conclusions" in prompt
    assert "trade-offs and final decisions to the parent agent" in prompt
    assert "Adapt depth to the caller's question" in prompt
    assert "source references" in prompt
    assert "file paths and line" in prompt


def test_explore_evidence_role_reaches_caller_delegate_catalog() -> None:
    target = OrchestrationTarget.from_agent(SYSTEM_AGENTS["system:explore"])
    tool = enrich_orchestration_target_catalog(
        DELEGATE_TOOL, OrchestrationTargetSnapshot(delegate=(target,), managed=())
    )
    description = tool.parameters["properties"]["agent_id"]["description"]

    assert target.description in description
    assert "documents, files, configuration, and code" in description
    assert "leaving final decisions to the caller" in description


def test_code_navigating_system_agents_expose_lsp() -> None:
    for agent_id in (
        "system:explore",
        "system:code-review",
        "system:architect",
        "system:implement",
    ):
        tools = SYSTEM_AGENTS[agent_id].tools or {}
        assert "lsp" in tools.get("builtin_tools", []), agent_id


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
    assert review.skills == {
        "items": [{"skill_id": "cognis-coding", "enabled": True, "auto_load_instructions": True}]
    }
    assert "Primary focus: real bugs, regressions, security issues" in review.system_prompt
    assert "Do not nitpick style or architecture" in review.system_prompt
    assert "locked to the approved review scope" in review.system_prompt
    assert "concrete bug, regression, security" in review.system_prompt
    assert "On repeated review, verify the previous findings" in review.system_prompt
    assert "Do not repeat a full review unless" in review.system_prompt
    assert "unresolved from the previous review" in review.system_prompt
    assert "### Must Fix" in review.system_prompt
    assert "When reviewing an escalated implementation" in architect.system_prompt
    assert "redo the whole plan" in architect.system_prompt


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
