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


def test_coding_skill_controls_change_placement_and_structural_debt() -> None:
    skill = get_system_skill_default("cognis-coding")

    assert skill is not None
    instructions = _normalized_instructions(skill)
    assert "fixes the cause at its canonical owner" in instructions
    assert "It is not necessarily the change with the fewest edited lines" in instructions
    assert "locate its owner and equivalent logic, policy, schema, and fallback paths" in (
        instructions
    )
    assert "Extend one source of truth" in instructions
    assert "identify the proposed owner before editing" in instructions
    assert "Do not add responsibility to a mixed hotspot" in instructions
    assert "Give each state transition and side effect an explicit owner" in instructions
    assert "Avoid hidden cross-component mutation, private-field injection" in instructions
    assert "Record why it exists and when it can be removed" in instructions
    assert "Separate mechanical relocation or renaming from behavior changes" in instructions
    assert "Prefer tests through the owning boundary" in instructions
    assert "add focused invariant tests when existing coverage is insufficient" in instructions


def test_coding_skill_assigns_conditional_review_and_evidence_ownership() -> None:
    skill = get_system_skill_default("cognis-coding")

    assert skill is not None
    instructions = _normalized_instructions(skill)
    assert "independent code review is required by the execution contract" in instructions
    assert "justified by concrete risk" in instructions
    assert "the implementing conversation owns it" in instructions
    assert "Continue compatible reviewer context after fixes only when" in instructions
    assert "do not duplicate qualifying review" in instructions
    assert "owns its independent code-review lifecycle" not in instructions
    assert "It starts the review, fixes findings" not in instructions
    assert (
        "coordinator accepts qualifying review evidence instead of repeating the review"
        in instructions
    )
    assert "reviewer session or conversation" in instructions
    assert "reviewed commit, tree, or diff" in instructions
    assert "verdict, each finding and its disposition" in instructions
    assert "final reviewed revision" in instructions
    assert "review evidence is missing or stale and the implementer is available" in instructions
    assert "return the review to the implementing conversation" in instructions
    assert "implementer is unavailable" in instructions
    assert "coordinator or integration changes include uncovered code" in instructions
    assert "explicit workflow gate" in instructions
    assert "After the first blocking review, make one evidence-based correction" in instructions
    assert "stop the fix loop" in instructions
    assert "required decision to the assignment owner" in instructions
    assert "replan in the same conversation" in instructions
    assert "Do not escalate for a new unrelated nit" in instructions
    assert "reviewer's role and criteria" in instructions
    assert "prior findings and dispositions" in instructions
    assert "materially useful" in instructions
    assert "implementation reasoning transcript" in instructions
    assert "architect-owned" not in instructions.lower()
    assert "architect in coordinate mode" not in instructions.lower()


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
    assert "Direct delivery is the default" in instructions
    assert "Do not redelegate that core scope" in instructions
    assert "Coordinated delivery can use a team" in instructions
    assert "do not redelegate implementation" in instructions
    assert "instead of replacing itself with another coordinator" in instructions
    assert "Advisory assistance returns analysis, plans, or review" in instructions
    assert "one evidence-based correction" in instructions
    assert "replan in the same conversation" in instructions
    assert "acceptance evidence beyond tests written by the same" in instructions
    assert "Keep reviews scope-locked" in instructions
    assert "Reuse context generically, not only for review" in instructions
    assert "Before any fresh delegation" in instructions
    assert "compatible role, responsibilities, tool/authority scope" in instructions
    assert "does not default to a" in instructions


def test_frontend_engineering_skill_is_cross_surface_and_restrained() -> None:
    skill = get_system_skill_default("cognis-frontend-engineering")

    assert skill is not None
    assert str(skill["description"]).startswith("Design and implement clear")
    assert {"frontend", "product-design", "ux", "accessibility"} <= set(skill["tags"])
    instructions = _normalized_instructions(skill)
    assert "Remove everything without a user-facing job" in instructions
    assert "Must know" in instructions
    assert "Must act" in instructions
    assert "every section a card" in instructions
    assert "Avoid Generic AI Design" in instructions
    assert "Product and marketing pages" in instructions
    assert "Task-focused applications" in instructions
    assert "Web applications" in instructions
    assert "Mobile applications" in instructions
    assert "Desktop applications" in instructions
    assert "Dashboards and control centers" in instructions
    assert "Model only the states required by the flow" in instructions
    assert "Choose a material for its semantic role" in instructions
    assert "Human Interface Guidelines as the source of truth" in instructions
    assert "intuitive, perceivable through more than one sensory cue" in instructions
    assert "Validate in Three Passes" in instructions


def test_frontend_engineering_skill_preserves_each_surface_contract() -> None:
    skill = get_system_skill_default("cognis-frontend-engineering")

    assert skill is not None
    instructions = str(skill["instructions"])
    product = instructions.split("## Product and marketing pages", 1)[1].split(
        "## Task-focused applications", 1
    )[0]
    web = instructions.split("## Web applications", 1)[1].split("## Mobile applications", 1)[0]
    mobile = instructions.split("## Mobile applications", 1)[1].split("## Desktop applications", 1)[
        0
    ]
    control_center = instructions.split("## Dashboards and control centers", 1)[1].split(
        "# 8. Implement as a Real Product", 1
    )[0]

    for phrase in ("product, audience, value", "evidence", "narrative hierarchy"):
        assert phrase in product
    for phrase in ("URLs", "history", "deep links", "keyboard", "reload"):
        assert phrase in web
    for phrase in ("touch", "safe areas", "Dynamic Type", "platform builds", "simulator"):
        assert phrase in mobile
    for phrase in ("anomaly detection", "density", "data freshness", "drill-down", "stale"):
        assert phrase in control_center


def test_frontend_engineering_skill_exposes_build_and_visual_validation_tools() -> None:
    skill = get_system_skill_default("cognis-frontend-engineering")

    assert skill is not None
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
        "builtin:browser_open",
        "builtin:browser_snapshot",
        "builtin:browser_query",
        "builtin:browser_click",
        "builtin:browser_press",
        "builtin:browser_eval",
        "builtin:browser_screenshot",
        "builtin:browser_close",
    } <= set(skill["linked_tool_ids"])
    from cognis.executor.tool_definitions_runtime import (
        executor_tool_definitions as runtime_executor_tool_definitions,
    )

    available_tool_ids = {f"builtin:{tool.name}" for tool in runtime_executor_tool_definitions()}
    assert set(skill["linked_tool_ids"]) <= available_tool_ids


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
    assert "Architect" not in instructions
    assert "update it when each child result changes" in instructions
    assert "Select each worker's profile explicitly" in instructions
    assert "agent_conversation_set_profile" in instructions
    assert "run exactly that turn" in instructions
    assert "restore the" in instructions
    assert "previous profile only after" in instructions
    assert "On synchronous/joined surfaces" in instructions
    assert "Where asynchronous managed turns are exposed" in instructions
    assert "Start an initial independent review fresh" in instructions
    assert "same bounded problem" in instructions
    assert "retained context is materially useful" in instructions
    assert "Send the context delta only" in instructions
    assert "fresh isolated child" in instructions
    assert "compact contract" in instructions
    assert "exact references" in instructions
    assert "never pass the parent transcript" in instructions
    assert "independent branch requiring inherited context" in instructions
    assert "status, results/findings, changed references" in instructions
    assert "detailed logs" in instructions


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
