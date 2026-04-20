"""Canonical built-in system skill definitions."""

from __future__ import annotations

from typing import Final

SYSTEM_SKILL_DEFAULTS: Final[dict[str, dict[str, object]]] = {
    "cognis-orchestrator": {
        "skill_id": "cognis-orchestrator",
        "auto_load": True,
        "content": """---
name: Cognis Orchestrator
description: Guidance for deciding when to answer inline, when to use general-task, and when to compose a workflow.
tags:
  - cognis
  - orchestration
  - workflows
---

# Purpose

Use this skill when deciding how to execute non-trivial work in Cognis.

# Routing Rules

- Keep clearly trivial work inline.
- Use `create_task` with `system:general-task` when the work is substantial but does not justify explicit step structure.
- Use `compose_and_run_workflow` when the work is multi-step, recurring, deliverable-sensitive, or should be scheduled.

# Workflow Composition Rules

- Prefer reusing an existing workflow unchanged when it already fits.
- If an existing workflow is close but not exact, adapt it by deriving a new workflow instead of mutating the original.
- Treat skills as capability bundles first. If a skill has saved decomposition, it can also provide workflow structure.
- If a skill is useful but does not declare steps, composition may decompose it first.
- Schedules force persistent workflows. One-shot compositions should normally stay ephemeral.

# Deliverables

- Deliverables are the canonical workflow artifacts.
- Synthesis, summary, reporting, and final-output steps should usually require deliverables.
- Gather, inspect, and fetch steps may omit required deliverables when a lightweight step output is enough.

# Do Not Do

- Do not mutate system workflows in place.
- Do not create a custom workflow when an existing one already fits unchanged.
- Do not schedule ephemeral workflows.
""",
    },
    "cognis-coding": {
        "skill_id": "cognis-coding",
        "content": """---
name: Cognis Coding
description: Coding discipline for careful implementation work inside Cognis conversations and tasks.
tags:
  - cognis
  - coding
  - implementation
---

# Purpose

Use this skill when the agent is doing software engineering work and should follow a careful, execution-first coding workflow.

# Working Style

- Inspect first. Read only the files, code paths, and repo guidance needed to act correctly.
- Start by understanding the project instructions and conventions already present in the repo.
- Prefer `AGENTS.md` first, then `README.md` or compatible instruction files when AGENTS is absent or insufficient.
- For non-trivial edits, form a short plan before writing code.
- Prefer the smallest correct change over broad rewrites.
- Preserve existing patterns unless there is a concrete reason to improve them.
- Keep new abstractions minimal unless reuse is clear.
- Stay within scope and avoid unrelated cleanup.
- When there is a simpler implementation that still satisfies the requirement, choose it.

# Routing

- Keep small, clear edits inline when you can finish them immediately.
- When the code path is unclear, use `system:explore` first to trace the implementation before editing.
- For larger implementation, refactor, or multi-step debugging work, prefer delegation or a task with the software-development workflow instead of forcing everything inline.
- For focused coding work that does not need the current agent identity, prefer `system:implement`.
- For findings-first review, prefer `system:code-review`.
- For a second set of eyes on an implementation plan, prefer `system:architect`, but do not turn small coding tasks into architecture theater.
- Use existing Cognis workflows for heavier engineering process instead of inventing a custom long-form process inside one chat turn.

# Tool Use

- Prefer `read`, `grep`, and `glob` for code inspection.
- Use `lsp` for semantic navigation such as definitions, references, hover, and symbols when available.
- Prefer `edit`, `multiedit`, `patch`, and `write` for file-content changes.
- Use `bash` for git, tests, builds, package managers, and atomic filesystem operations.
- Avoid shell or interpreter one-liners that rewrite files when dedicated edit tools fit the task.

# Verification

- Run the narrowest checks that prove the change works.
- If the task affects tests, lint, typing, or build behavior, run the relevant command when feasible.
- Update directly affected docs when behavior, usage, or contributor workflow actually changed.
- If no documentation changes are needed, say so plainly.
- If you delegate substantial coding work, explain that to the user and keep the main thread responsive when possible.
- Report verification performed and any remaining risks.

# Do Not Do

- Do not stop at a plan when the next concrete action is clear.
- Do not perform large opportunistic refactors without a concrete need.
- Do not overengineer, add speculative abstractions, or widen scope just because you see a cleaner architecture.
- Do not claim verification you did not run.
""",
    },
    "cognis-task-manager": {
        "skill_id": "cognis-task-manager",
        "content": """---
name: Cognis Task Manager
description: Safe operating procedure for inspecting and managing Cognis tasks from main chat.
tags:
  - cognis
  - management
  - tasks
---

# Purpose

Use this skill when the user wants the main chat agent to inspect, create, update, retry, continue, cancel, or otherwise manage Cognis tasks without leaving the conversation.

# When To Use

- Create a task from a conversational request.
- Inspect a running or paused task.
- Review task output or step output.
- Resolve a task gate.
- Answer a paused task question.
- Update task metadata such as title, description, or workflow assignment.

# Required Inspection Steps

1. Use `get_task` before mutating an existing task.
2. Read `pending_pause`, `workflow_run`, and recent `step_runs` before choosing an action.
3. If the task is paused on a question, use `respond_task_input` instead of gate tools.
4. If the task is paused on a gate, only choose actions that are explicitly offered.

# Safe Mutation Rules

- Preserve the user’s wording exactly when passing an operator instruction or note.
- Do not guess hidden workflow state; inspect first.
- Do not retry a gate unless a retry/revise action is actually offered.
- Do not treat escalation pauses as task gates; escalation approval still uses `/approve` or `/deny`.
- Prefer minimal updates to task metadata instead of rewriting unrelated fields.

# Tool Usage

- `create_task` to create a new task.
- `list_tasks` to inspect matching tasks.
- `get_task` for full task state, pause state, and workflow run metadata.
- `get_task_output` and `get_task_step_output` for outputs.
- `resolve_task_pause` to retry, continue, cancel, or resolve another offered gate action.
- `respond_task_input` to answer a paused step question.
- `update_task` and `cancel_task` for direct management.

# Do Not Do

- Do not mutate a task before inspecting it.
- Do not invent missing pause actions.
- Do not claim a paused task is unblocked until the tool confirms success.

# Examples

- "Inspect task `task_123` and tell me why it is paused."
- "Continue the paused task and tell it to incorporate the last review."
- "Answer the task’s pending question with the user’s reply."
""",
    },
    "cognis-workflow-manager": {
        "skill_id": "cognis-workflow-manager",
        "content": """---
name: Cognis Workflow Manager
description: Safe operating procedure for inspecting and managing Cognis workflow definitions from main chat.
tags:
  - cognis
  - management
  - workflows
---

# Purpose

Use this skill when the user wants the main chat agent to inspect or manage Cognis workflow definitions directly from conversation instead of the workflow editor.

# When To Use

- List available workflows.
- Inspect a workflow definition before creating tasks against it.
- Create a new user-owned workflow from a conversational specification.
- Update, duplicate, or delete an existing user-owned workflow.

# Required Inspection Steps

1. Use `list_workflows` or `get_workflow` before mutating a workflow.
2. Confirm whether the workflow is system-owned or user-owned.
3. Inspect current steps, references, loop targets, and outcome routes before editing.
4. If a workflow is referenced by active tasks, treat it as protected and avoid destructive edits.

# Safe Mutation Rules

- Keep changes minimal and explicit.
- Preserve valid step names and references.
- Do not attempt to modify system workflows.
- Do not delete or overwrite a workflow referenced by active tasks.
- Prefer duplicating a workflow before heavy edits when the user wants to preserve the original.

# Tool Usage

- `list_workflows` for discovery.
- `get_workflow` for full definitions.
- `create_workflow` for new definitions.
- `update_workflow` for targeted edits.
- `duplicate_workflow` to branch an existing workflow.
- `delete_workflow` only when the user clearly wants removal and it is safe.

# Do Not Do

- Do not invent step references or route targets.
- Do not mutate system workflows.
- Do not force workflow changes when active tasks still depend on the current definition.

# Examples

- "Create a workflow that plans, implements, reviews, and commits changes."
- "Duplicate the software-development workflow and tailor it for docs-only tasks."
- "Inspect this workflow and explain why it loops back to plan."
""",
    },
}


def get_system_skill_default(skill_id: str) -> dict[str, object] | None:
    """Return the canonical default definition for a system skill."""

    from cognis.tools.skill_parser import parse_skill_md

    default = SYSTEM_SKILL_DEFAULTS.get(skill_id)
    if default is None:
        return None
    parsed = parse_skill_md(str(default["content"]))
    return {
        "skill_id": default["skill_id"],
        "name": parsed["name"],
        "description": parsed["description"],
        "instructions": parsed["instructions"],
        "tags": list(parsed["tags"]),
        "tools": parsed["tools"],
        "prompt_templates": parsed["prompt_templates"],
        "steps": parsed.get("steps") or [],
        "auto_load": bool(default.get("auto_load", False)),
    }
