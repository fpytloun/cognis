from __future__ import annotations

from cognis.core.system_skills import get_system_skill_default


def _instructions(skill_id: str) -> str:
    skill = get_system_skill_default(skill_id)
    assert skill is not None
    return " ".join(str(skill["instructions"]).split())


def test_coding_skill_keeps_git_and_validation_boundaries() -> None:
    instructions = _instructions("cognis-coding")
    for contract in (
        "normally `origin`",
        "isolated worktree",
        "<type>[optional scope][!]: <short description>",
        "Never delete, skip, or weaken a test",
        "never claim verification you did not run",
        "never secrets, tokens, or payloads",
        "does not redelegate",
    ):
        assert contract in instructions


def test_frontend_skill_tools_exist_in_executor_inventory() -> None:
    from cognis.executor.tool_definitions_runtime import (
        executor_tool_definitions as runtime_executor_tool_definitions,
    )

    skill = get_system_skill_default("cognis-frontend-engineering")
    assert skill is not None
    available = {f"builtin:{tool.name}" for tool in runtime_executor_tool_definitions()}
    assert {"builtin:browser_screenshot", "builtin:bash"} <= set(skill["linked_tool_ids"])
    assert set(skill["linked_tool_ids"]) <= available


def test_rich_deliverable_skill_validates_before_writing() -> None:
    instructions = _instructions("cognis-rich-deliverable")
    assert instructions.index("`validate_tool_call`") < instructions.index(
        "exact unchanged validated arguments"
    )
    assert "`artifact_publish`" in instructions


def test_orchestrator_skill_links_its_routing_tools() -> None:
    skill = get_system_skill_default("cognis-orchestrator")
    assert skill is not None
    assert {
        "builtin:delegate",
        "builtin:follow_up_subsession",
        "builtin:fork_subsession",
        "builtin:agent_conversation_send",
        "builtin:agent_conversation_set_profile",
        "builtin:create_task",
        "builtin:manage_schedules",
        "builtin:compose_and_run_workflow",
    } <= set(skill["linked_tool_ids"])
