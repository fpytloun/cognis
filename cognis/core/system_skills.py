"""Canonical built-in system skill definitions."""

from __future__ import annotations

from typing import Final

SYSTEM_SKILL_DEFAULTS: Final[dict[str, dict[str, object]]] = {
    "cognis-orchestrator": {
        "skill_id": "cognis-orchestrator",
        "content": """---
name: Cognis Orchestrator
description: Guidance for deciding when to answer inline, when to use general-task, and when to compose a workflow.
tags:
  - cognis
  - orchestration
  - workflows
linked_tool_ids:
  - builtin:create_task
  - builtin:manage_schedules
  - builtin:compose_and_run_workflow
  - builtin:list_workflows
  - builtin:get_workflow
---

# Purpose

Use this skill when deciding how to execute non-trivial work in Cognis.

# Routing Rules

- Keep clearly trivial work inline.
- Use `create_task` with `system:general-task` when the work is substantial but does not justify explicit step structure.
- Use `manage_schedules` when the user wants normal task work to run later, at a specific time, or on a recurrence. Prefer this for reminders, timed automations, and recurring general-task work.
- Use `compose_and_run_workflow` only when the work needs a custom persistent multi-step workflow, strict deliverables, or an adapted reusable workflow definition before it can run safely.

# Workflow Composition Rules

- Prefer reusing an existing workflow unchanged when it already fits.
- If an existing workflow is close but not exact, adapt it by deriving a new workflow instead of mutating the original.
- Treat skills as capability bundles first. If a skill has saved decomposition, it can also provide workflow structure.
- If a skill is useful but does not declare steps, composition may decompose it first.
- Most scheduled work should use `manage_schedules` with an existing workflow or general task shape. Only compose a workflow for a schedule when the user explicitly needs custom step structure.
- Schedules that use composed workflows require persistent workflows. One-shot compositions should normally stay ephemeral unless they are explicitly scheduled.

# Deliverables

- Deliverables are the canonical workflow artifacts.
- Synthesis, summary, reporting, and final-output steps should usually require deliverables.
- Gather, inspect, and fetch steps may omit required deliverables when a lightweight step output is enough.

# Do Not Do

- Do not mutate system workflows in place.
- Do not create a custom workflow when an existing one already fits unchanged.
- Do not use `compose_and_run_workflow` merely because the user gave a time. Use `manage_schedules` for ordinary timed tasks.
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
linked_tool_ids:
  - builtin:read
  - builtin:write
  - builtin:edit
  - builtin:apply_patch
  - builtin:multiedit
  - builtin:lsp
  - builtin:glob
  - builtin:grep
  - builtin:bash
---

# Purpose

Use this skill when the agent is doing software engineering work and should follow a careful, execution-first coding workflow.

Workflow step objectives and controller completion contracts override this skill. If a workflow step asks for a plan, review, summary, or decision, produce that step artifact only and do not jump ahead to implementation.

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
- Do not add backward-compatibility code unless there is a concrete need such as persisted data, shipped behavior, external consumers, or an explicit user requirement.
- Before introducing a dependency, verify the project already uses it or that adding it is explicitly required.
- Preserve user-facing prose in the target language, including correct diacritics. Do not force natural-language documents to ASCII.
- Keep code identifiers, code comments, and commit messages in English unless the user or project explicitly requires otherwise.
- Add comments sparingly, only when they explain non-obvious intent or constraints.

# Workspace Hygiene

- You may be in a dirty workspace. Never revert, overwrite, or clean up changes you did not make unless the user explicitly asks.
- If unexpected changes overlap with your intended edits, inspect them and preserve the user's work; ask one targeted question only if they directly conflict with the task.
- Do not run destructive commands such as `git reset --hard`, `git checkout --`, or broad deletes unless the user explicitly requests or approves them.
- Do not create, amend, or push git commits unless the user explicitly asks.
- Prefer non-interactive git commands when git is needed.
- Never commit secrets, credentials, or local environment files.

# Routing

- In direct chat, implement when the user asks for implementation and the next action is clear.
- In general tasks, follow the requested task scope and stop at the requested artifact.
- In coding workflows, complete only the current workflow step artifact; later workflow steps handle later lifecycle actions such as implementation, verification, commit, pull request, and final summary.
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
- Do not use `bash` with `rg`, `grep`, `find`, `ls`, `cat`, `head`, `tail`, `sed`, or `echo` separators for file/code inspection when structured tools such as `read`, `grep`, `glob`, or `list_directory` are visible.
- Do not chain file inspection commands with `&&`, `;`, or separator output. Use independent structured tool calls in parallel instead.
- Prefer the dedicated file editing tools exposed for the current model. Use `apply_patch` when that is the visible edit tool; otherwise use `edit`, `multiedit`, and `write` for file-content changes.
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

- Do not stop at a plan unless the user request or current workflow step explicitly asks for a plan, review, explanation, or decision.
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
linked_tool_ids:
  - builtin:create_task
  - builtin:list_tasks
  - builtin:get_task
  - builtin:respond_task_input
  - builtin:update_task
  - builtin:cancel_task
  - builtin:get_task_output
  - builtin:get_task_step_output
  - builtin:resolve_task_pause
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
linked_tool_ids:
  - builtin:list_workflows
  - builtin:get_workflow
  - builtin:create_workflow
  - builtin:update_workflow
  - builtin:delete_workflow
  - builtin:duplicate_workflow
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
    "cognis-agent-manager": {
        "skill_id": "cognis-agent-manager",
        "content": """---
name: Cognis Agent Manager
description: Safe operating procedure for managing user-owned Cognis agents from main chat.
tags:
  - cognis
  - management
  - agents
linked_tool_ids:
  - builtin:manage_agents
---

# Purpose

Use this skill when the user asks the current primary agent to inspect, create, update, archive, activate, suspend, sync, bind, or share other agents owned by the same user.

# Safety Rules

- Inspect before mutating. Use `manage_agents` with `action="list"` or `action="get"` before editing an existing agent.
- Never try to manage yourself. If the target agent is the current agent, explain that self-management is not allowed.
- Only manage agents owned by the current user. Shared agents are use-only and cannot be edited or reshared by grantees.
- Treat delete as archive-only. Use `action="archive"`; do not promise permanent deletion.
- Share management can grant access to another user. Confirm the target email and executor scope with the user before calling `share_create`, `share_update`, or `share_revoke`.
- Keep permission and tool changes minimal. Preserve existing unrelated settings.

# Tool Usage

- `list` and `get` for inspection.
- `create` for new agents. Include full profile fields when the user provided them.
- `update` for targeted edits to profile, tools, permissions, skills, LLM config, execution, and avatar fields.
- `bindings_get` and `bindings_set` for primary-to-secondary agent bindings.
- `shares_list`, `share_create`, `share_update`, and `share_revoke` for owner-only share management.
- `sync_personality` after intentional identity/personality changes if synchronization is needed.

# Do Not Do

- Do not invent grantee email addresses.
- Do not silently broaden permissions or tool access.
- Do not mutate shared agents unless the current user is the owner.
- Do not claim a share was created, updated, or revoked until the tool confirms it.
""",
    },
    "cognis-web-research": {
        "skill_id": "cognis-web-research",
        "content": """---
name: Cognis Web Research
description: Recipe for ad-hoc multi-source web research using web_search and web_fetch.
tags:
  - cognis
  - web
  - research
linked_tool_ids:
  - builtin:web_search
  - builtin:web_fetch
  - builtin:web_crawl
  - builtin:web_map
---

# Purpose

Use this skill when the user asks for research, fact-checking, deep dives,
multi-source summaries, or anything else where the answer should be
synthesised from multiple live web pages rather than from memory.

The free path for research in Cognis is the agent loop itself: call
`web_search` to discover sources, fetch the best ones via `web_fetch`,
synthesise the result with citations. The dedicated `web_research` tool
is only available when the Tavily backend is configured and is preferred
over this skill for paid users who want a turnkey multi-source report.

# Recipe

1. **Diversify queries.** Run `web_search` with two or three different
   phrasings of the question (e.g. one with the technical term and one
   with the colloquial one). Combine results before picking sources.
2. **Pick a small, varied set of sources.** Aim for 3-7. Prefer primary
   sources (project docs, official blogs, datasheets, regulators) over
   aggregators. Avoid stacking three results from the same domain.
3. **Fetch in parallel.** Issue multiple `web_fetch` calls in the same
   turn so the executor's concurrency controller can pipeline them.
   `web_fetch` also materializes PDFs, images, and other binary files as
   artifacts. PDFs return extracted page text and keep the original PDF
   attached for later use.
4. **Do not force a backend unless you mean to.** Normally omit the
   optional `backend` parameter on `web_search` and `web_fetch` so the
   configured defaults apply. For fetches, omitting `backend` also keeps
   the automatic browser fallback available.
5. **Cross-check.** When sources disagree, surface the disagreement;
   never paper over it. When they agree, you can compress.
6. **Cite.** Every non-trivial claim in the synthesis should reference
   the URL it came from. The user can audit. Use markdown links.
   When images matter, fetch the selected image URLs so they become
   artifacts, then use `artifact_read` to analyze them through the
   vision-capable model routing before embedding them in a document.
7. **Escalate carefully.** If `web_fetch` reports both a primary failure
   and a "headless browser fallback failed" message in the same error,
   the controller already exhausted the headless retry. Headed
   fallback (if enabled via `web.browser_fetch.headed_fallback_enabled`)
   runs automatically after that. Do not manually retry with
   `backend='browser'` to "force" a browser fetch — it will only repeat
   the same headless attempt. Pick a different source or escalate to
   the user instead.
8. **Stop at "enough".** Quit when adding another source would be
   redundant. Five high-quality citations beat fifteen low-quality ones.

# Output Style

- Lead with the direct answer.
- Follow with the supporting reasoning organised by source.
- End with a `## Sources` list of `[title](url)` markdown links.
- Flag confidence (`high` / `medium` / `low`) when the question allows
  for it.

# Do Not Do

- Do not synthesise without fetching real sources. The whole point is
  to ground the answer in live web content rather than in memory.
- Do not fan out hundreds of fetches. The executor caps concurrency
  for a reason; staying inside the cap keeps page loads fast.
- Do not call `web_research` when it isn't exposed in your tool list -
  it requires Tavily and is unavailable on direct/SearXNG-only setups.
- Do not silently ignore disagreements between sources.
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
        "linked_tool_ids": list(parsed.get("linked_tool_ids") or []),
        "tools": parsed["tools"],
        "prompt_templates": parsed["prompt_templates"],
        "steps": parsed.get("steps") or [],
        "auto_load": bool(default.get("auto_load", False)),
    }
