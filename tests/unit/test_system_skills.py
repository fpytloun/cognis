from __future__ import annotations

from cognis.core.system_skills import get_system_skill_default


def _normalized_instructions(skill: dict[str, object]) -> str:
    return " ".join(str(skill["instructions"]).split())


def test_coding_skill_defines_complete_isolated_git_workflow() -> None:
    skill = get_system_skill_default("cognis-coding")

    assert skill is not None
    instructions = _normalized_instructions(skill)
    assert "fetch the relevant remote, normally `origin`" in instructions
    assert "compare the intended base with its upstream revision" in instructions
    assert "create or reuse an isolated worktree" in instructions
    assert "already isolated and based on that verified revision" in instructions
    assert "If an assigned worktree is stale but still clean" in instructions
    assert "An explicit implementation request" in instructions
    assert "unless the request says to leave changes uncommitted" in instructions
    assert "the worktree may remain dirty with task-owned changes" in instructions
    assert "Do not amend, rebase, merge into a user-owned branch" in instructions


def test_coding_skill_defaults_to_conventional_commits() -> None:
    skill = get_system_skill_default("cognis-coding")

    assert skill is not None
    instructions = _normalized_instructions(skill)
    assert "Use Conventional Commits v1.0.0 for task-owned commits" in instructions
    assert "unless the repository defines a different commit convention" in instructions
    assert "<type>[optional scope][!]: <short description>" in instructions
    assert "`feat`, `fix`, `refactor`, `test`, `docs`, `perf`, `build`, `ci`, or `chore`" in (
        instructions
    )
    assert "Do not invent scopes, issue IDs, or breaking markers" in instructions


def test_coding_skill_scopes_parallel_implementation_and_validation() -> None:
    skill = get_system_skill_default("cognis-coding")

    assert skill is not None
    instructions = _normalized_instructions(skill)
    assert "Parallel implementation is appropriate only when" in instructions
    assert "Do not reflexively run" in instructions
    assert "focused checks provide sufficient evidence" in instructions
    assert "Do not delete, skip, weaken, or rewrite tests" in instructions
    assert "Report the exact verification commands and outcomes" in instructions


def test_coding_skill_defines_bounded_delivery_contract() -> None:
    skill = get_system_skill_default("cognis-coding")

    assert skill is not None
    assert str(skill["description"]).startswith("Bounded software delivery")
    assert "bounded-delivery" in skill["tags"]
    assert {
        "builtin:read",
        "builtin:write",
        "builtin:edit",
        "builtin:apply_patch",
        "builtin:multiedit",
        "builtin:lsp",
        "builtin:glob",
        "builtin:grep",
        "builtin:bash",
    } <= set(skill["linked_tool_ids"])
    instructions = _normalized_instructions(skill)
    assert "proportional Todos for genuine multistep work" in instructions
    assert "Do not create todos for work that can be completed in a single response" in instructions
    assert "mandatory proportional Todo for all work" not in instructions
    assert "Created todos persist" in instructions
    assert "terminal completion" in instructions
    assert "multiple in_progress items are allowed only for genuinely parallel work" in instructions
    assert "coordinator retains end-to-end ownership" in instructions
    assert "one bounded scope" in instructions
    assert "Do not delegate that same implementation scope" in instructions
    assert "delegate implementation further" in instructions
    assert "one evidence-based correction" in instructions
    assert "replan or escalate to a more suitable" in instructions
    assert "acceptance evidence beyond tests written by the same" in instructions
    assert "Keep reviews scope-locked" in instructions
    assert "Reuse context generically, not only for review" in instructions
    assert "Before any fresh delegation" in instructions
    assert "specialist role, tool/authority scope" in instructions
    assert "follow-up and fork preserve the source" in instructions
    assert "does not default to a" in instructions


def test_orchestrator_skill_routes_all_bounded_execution_shapes() -> None:
    skill = get_system_skill_default("cognis-orchestrator")

    assert skill is not None
    instructions = _normalized_instructions(skill)
    assert "Work directly" in instructions
    assert "Use a delegate" in instructions
    assert "Before starting a fresh delegate" in instructions
    assert "`follow_up_subsession`" in instructions
    assert "`fork_subsession`" in instructions
    assert "Use a managed conversation" in instructions
    assert "Use a task" in instructions
    assert "Use a workflow only when an explicit durable step" in instructions
    assert "Reuse a relevant managed conversation" in instructions
    assert "`agent_conversation_fork`" in instructions
    assert "Architect Todos track durable workstreams or milestones" in instructions
    assert "update it when each child result changes" in instructions
    assert "Select each worker's profile explicitly" in instructions
    assert "agent_conversation_set_profile" in instructions
    assert "run exactly that turn" in instructions
    assert "restore the" in instructions
    assert "previous profile only after" in instructions
    assert "On synchronous/joined surfaces" in instructions
    assert "Where asynchronous managed turns are exposed" in instructions


def test_orchestrator_skill_advertises_managed_conversation_tools() -> None:
    skill = get_system_skill_default("cognis-orchestrator")

    assert skill is not None
    assert str(skill["description"]).startswith("Route bounded work")
    assert {"bounded-delivery", "managed-conversations", "tasks", "workflows"} <= set(skill["tags"])
    assert {
        "builtin:manage_agents",
        "builtin:delegate",
        "builtin:follow_up_subsession",
        "builtin:fork_subsession",
        "builtin:agent_conversation_create",
        "builtin:agent_conversation_send",
        "builtin:agent_conversation_fork",
        "builtin:agent_conversation_wait",
        "builtin:agent_conversation_get",
        "builtin:agent_conversation_list",
        "builtin:agent_conversation_set_profile",
        "builtin:create_task",
        "builtin:manage_schedules",
        "builtin:compose_and_run_workflow",
        "builtin:list_workflows",
        "builtin:get_workflow",
    } <= set(skill["linked_tool_ids"])
