"""Canonical built-in system skill definitions."""

from __future__ import annotations

from typing import Final

SYSTEM_SKILL_DEFAULTS: Final[dict[str, dict[str, object]]] = {
    "cognis-orchestrator": {
        "skill_id": "cognis-orchestrator",
        "content": """---
name: Cognis Orchestrator
description: Route bounded work across direct execution, delegates, managed conversations, tasks, and explicit durable workflows.
tags:
  - cognis
  - orchestration
  - bounded-delivery
  - managed-conversations
  - tasks
  - workflows
linked_tool_ids:
  - builtin:manage_agents
  - builtin:delegate
  - builtin:follow_up_subsession
  - builtin:fork_subsession
  - builtin:agent_conversation_create
  - builtin:agent_conversation_send
  - builtin:agent_conversation_fork
  - builtin:agent_conversation_wait
  - builtin:agent_conversation_get
  - builtin:agent_conversation_list
  - builtin:agent_conversation_set_profile
  - builtin:create_task
  - builtin:manage_schedules
  - builtin:compose_and_run_workflow
  - builtin:list_workflows
  - builtin:get_workflow
---

# Purpose

Use this skill when choosing and coordinating the least costly safe execution
shape for work in Cognis.

# Routing Rules

- Work directly when one agent can safely inspect, act, validate, and report
  without a durable background boundary.
- Use a delegate for one bounded terminal result such as independent
  exploration, specialist advice, or scope-locked review.
- Before starting a fresh delegate, inspect existing child sessions. Reuse or
  fork a child only when the same problem, specialist role, tool/authority
  scope, and expected output remain compatible. Follow-up and fork preserve the
  source child's agent identity and capabilities; they do not change specialist.
  For the same compatible line of work, use `follow_up_subsession`; use
  `fork_subsession` for an independent branch. Start fresh with the appropriate
  specialist when the next work needs a different role, tools, authority, or
  output contract.
- Use a managed conversation for visible, inspectable, iterative work that may
  need follow-up. Reuse a relevant managed conversation with
  `agent_conversation_get` and `agent_conversation_send` instead of creating a
  duplicate; use `agent_conversation_fork` when the new work should branch from
  that context.
- Every new managed-conversation contract must state `Working mode: execute` or
  `Working mode: coordinate`. Execute means the target completes the assigned
  core work itself and does not split or delegate it; bounded exploration,
  research, consultation, or independent review remains allowed. Coordinate
  permits independent workstreams while retaining integration and acceptance.
- Use a task when work needs durable background ownership, status, pause/resume,
  or later retrieval but not a custom workflow definition.
- Use a workflow only when an explicit durable step, deliverable, evaluation, or
  gate contract is needed. Tasks and workflows are options, not defaults for
  substantial work.
- Use `manage_schedules` for ordinary delayed or recurring work. Do not compose
  a workflow merely because the user supplied a time.

# Bounded Coordination

- The coordinator retains end-to-end ownership for decomposition, integration,
  acceptance evidence, and final delivery.
- Decompose large scope into proportional observable workstreams when that
  materially improves safety or elapsed time. Give each implementation worker
  one bounded scope with stable inputs and acceptance criteria; workers do not
  delegate implementation further.
- Architect Todos track durable workstreams or milestones. Keep the parent Todo
  current across turns, and update it when each child result changes the state
  of the parent workstream. Plain proportional names are sufficient.
- Select each worker's profile explicitly from discovered eligible profiles
  when profile choice matters. Do not guess profile IDs.

# Managed Conversations and Profiles

- Respect the current surface. On synchronous/joined surfaces, create, send, or
  retry joins before returning. Where asynchronous managed turns are exposed,
  use them only for truly independent or follow-up work, stop duplicating that
  scope in the parent, and rely on completion notification.
- Change an idle managed conversation's profile with
  `agent_conversation_set_profile` before sending the next turn. Never race a
  profile change with send/admission, and never change a profile while work is
  active or queued.
- For one critical consultation, switch an idle managed conversation to the
  needed profile, run exactly that turn, inspect the result, and restore the
  previous profile only after the conversation is idle again.

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

- Do not default substantial work to a task or workflow when direct, delegated,
  or managed execution provides the required contract.
- Do not duplicate a managed workstream or continue its same scope in parallel.
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
description: Bounded software delivery discipline for implementation, validation, review, and coordination in Cognis.
tags:
  - cognis
  - coding
  - implementation
  - bounded-delivery
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

- Create and maintain proportional Todos for genuine multistep work. Do not
  create todos for work that can be completed in a single response. Created
  todos persist across turns until terminal completion; complete or cancel every
  item before finishing. Plain names are sufficient, hierarchy is optional when
  useful, and multiple in_progress items are allowed only for genuinely parallel
  work.
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
- Use Conventional Commits v1.0.0 for task-owned commits unless the repository
  defines a different commit convention or the user explicitly requests one.
  Format the subject as `<type>[optional scope][!]: <short description>`.
  Choose the type from the actual change, for example `feat`, `fix`,
  `refactor`, `test`, `docs`, `perf`, `build`, `ci`, or `chore`. Keep the
  subject concise and in English. Add a body or `BREAKING CHANGE` footer only
  when it carries useful context. Do not invent scopes, issue IDs, or breaking
  markers.
- Add comments sparingly, only when they explain non-obvious intent or constraints.

# Workspace Hygiene

- You may be in a dirty workspace. Never revert, overwrite, or clean up changes you did not make unless the user explicitly asks.
- If unexpected changes overlap with your intended edits, inspect them and preserve the user's work; ask one targeted question only if they directly conflict with the task.
- Do not run destructive commands such as `git reset --hard`, `git checkout --`, or broad deletes unless the user explicitly requests or approves them.
- Before implementation, inspect the repository state and configured remotes. When the task should start from current upstream and network access is available, fetch the relevant remote, normally `origin`, then compare the intended base with its upstream revision before writing.
- Do not update, rebase, reset, or otherwise move the user's current branch merely to catch up. For substantial, parallel, risky, or workflow-owned implementation, create or reuse an isolated worktree from the verified target revision. Do not create another worktree when the current workspace is already isolated and based on that verified revision.
- If an assigned worktree is stale but still clean, create or recreate a separate worktree from the intended upstream revision rather than silently implementing on the stale base. If it already contains task work, preserve it and report the divergence before any integration; never discard work to catch up.
- Leave completed implementation reviewable and transferable. A committed result should have no task-owned uncommitted residue. When the request explicitly asks for uncommitted changes, the worktree may remain dirty with task-owned changes, but remove unrelated or generated residue you created.
- An explicit implementation request, or an implementation workflow step whose completion contract expects a finished change, authorizes local commits when the agent owns an isolated worktree, unless the request says to leave changes uncommitted. Commit only task-owned changes. For patch-only, review-only, exploratory, or explicitly uncommitted work, do not create a commit.
- Do not amend, rebase, merge into a user-owned branch, push, open or update a pull request, or deploy unless the user request or workflow contract authorizes that integration step.
- Prefer non-interactive git commands when git is needed.
- Never commit secrets, credentials, or local environment files.

# Execution Contract

- Follow the current role, user request, and workflow contract.
- A coordinator retains end-to-end ownership for decomposition, integration,
  acceptance evidence, correction decisions, and final delivery.
- If explicitly assigned as a coordinator, plan, split genuinely independent work, and integrate the results while retaining that ownership.
- Decompose large scope into proportional observable workstreams when useful.
  Architect Todos track those durable workstreams or milestones; developer
  Todos track granular implementation, test, and acceptance steps.
- If directly assigned as the implementer, own one bounded scope and inspect, implement, and test it yourself. Do not delegate that same implementation scope, delegate implementation further, or redelegate the same scope.
- When assigning a primary-agent workstream, state `Working mode: execute` or
  `Working mode: coordinate`. In execute mode, the target completes the core
  work itself and does not split or delegate it. Use coordinate mode only when
  the target is expected to decompose independent workstreams and retain
  integration and acceptance.
- Prefer delegation for bounded independent exploration or review. Parallel implementation is appropriate only when the plan and integration contracts are stable, workstreams have separate ownership, dependencies are not sequential, each worker has clear acceptance criteria and an isolated workspace, and one coordinator owns final integration and review.
- Keep work direct when workers would touch the same hotspots, interfaces are still evolving, one slice depends on decisions from another, or coordination costs more than the implementation.
- After a failed check or concrete review finding, make one evidence-based
  correction when the cause and fix are clear. If it still fails or uncertainty
  remains, stop repeating fixes and replan or escalate to a more suitable
  profile.
- Reuse context generically, not only for review. Before any fresh delegation,
  check whether an existing child context has the same problem, specialist role,
  tool/authority scope, and expected output. Continue or branch from it only
  when all remain compatible, because follow-up and fork preserve the source
  child's agent identity and capabilities. Start fresh with the appropriate
  specialist when any of those change.
- Keep implementation fixes with the original implementer. For review, start the
  first genuinely independent review fresh, then continue or fork that reviewer
  context after fixes instead of rebuilding review context from scratch.
- Keep reviews scope-locked. Reviewers block only for concrete bugs,
  regressions, security or data-loss risks, or approved acceptance violations.
- Use a workflow only when an explicit durable step, deliverable, evaluation, or
  gate contract is needed; substantial software work does not default to a
  software-development workflow.
- Use only execution mechanisms and tools visible in the current context.

# Tool Use

- Prefer `read`, `grep`, and `glob` for code inspection.
- Use `lsp` for semantic navigation such as definitions, references, hover, and symbols when available.
- Do not use `bash` with `rg`, `grep`, `find`, `ls`, `cat`, `head`, `tail`, `sed`, or `echo` separators for file/code inspection when structured tools such as `read`, `grep`, `glob`, or `list_directory` are visible.
- Do not chain file inspection commands with `&&`, `;`, or separator output. Use independent structured tool calls in parallel instead.
- Prefer the dedicated file editing tools exposed for the current model. Use `apply_patch` when that is the visible edit tool; otherwise use `edit`, `multiedit`, and `write` for file-content changes.
- Use `bash` for git, tests, builds, package managers, and atomic filesystem operations.
- Avoid shell or interpreter one-liners that rewrite files when dedicated edit tools fit the task.

# Verification

- Make validation proportional to scope and risk. Start with the narrowest checks that exercise the changed behavior, then expand to affected-module checks when interfaces or shared behavior changed.
- When feasible, obtain acceptance evidence beyond tests written by the same
  implementation author: an independent review, an existing regression suite,
  a build/type/lint check, or an external behavior check.
- Run the full suite only when required by repository instructions, release policy, broad cross-cutting impact, migration risk, or an explicit request. Do not reflexively run a 30–60 minute suite when focused checks provide sufficient evidence. Run intentionally long checks asynchronously when the execution context supports it.
- If the task affects tests, lint, typing, or build behavior, run the relevant command when feasible.
- Do not delete, skip, weaken, or rewrite tests merely to obtain a green result. Test changes are valid when the intended behavior or contract actually changed.
- Update directly affected docs when behavior, usage, or contributor workflow actually changed.
- If no documentation changes are needed, say so plainly.
- Report the exact verification commands and outcomes, checks not run and why, and any remaining risks.

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
4. Inspect presentation phases and preserve their complete, contiguous step coverage.
5. If a workflow is referenced by active tasks, treat it as protected and avoid destructive edits.

# Safe Mutation Rules

- Keep changes minimal and explicit.
- Preserve valid step names and references.
- Do not attempt to modify system workflows.
- Do not delete or overwrite a workflow referenced by active tasks.
- Prefer duplicating a workflow before heavy edits when the user wants to preserve the original.
- Author concise presentation phases for meaningful stages. Every step must appear
  exactly once, and phase membership must follow canonical contiguous step order.

# Deterministic Step Authoring

- Use `tool_call` only for one mechanical tool invocation, `condition` only for
  strict-boolean branching, and `complete` for terminal no-LLM completion.
- A deterministic step must contain exactly one matching config. Do not mix
  agent, input, completion, review, question, or outcome-route fields into it.
- `when` and `condition.if` are constrained Jinja expressions and must evaluate
  to a boolean. A single `{{ expression }}` preserves native argument/output
  types; summaries and content render as text.
- Branches and `next` reference existing steps. Never create self-jumps or
  deterministic jump cycles.
- Tool calls default to read-only behavior. Set `allow_side_effects=true` only
  for intentionally authorized mutations. Never render credentials or secrets.
- Prefer deterministic fetch/check/branch/no-op logic; keep judgment,
  synthesis, writing, and ambiguous intent interpretation in `run` steps.

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

- "Create a workflow with Plan, Build, Verify, and Deliver phases that plans,
  implements, reviews, and commits changes."
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
- For tool changes, prefer explicit CRUD over raw settings: inspect the current assignment with `tools_get`, use `search_tools`/`describe_tool` for authorized tool IDs and semantics, optionally check a proposed mutation with `validate_tool_call`, then use `tools_set`, `tools_add`, or `tools_remove`.
- Prefer curated `tool_groups` for normal access and use `allow_tools` / `deny_tools` only for granular exceptions. Do not invent tool or group IDs.
- Manage knowledgebase data access separately with `knowledgebases_get`, `knowledgebases_set`, `knowledgebases_add`, and `knowledgebases_remove`; tool assignment controls what the agent can do, knowledgebase assignment controls which KBs it can access.
- Do not confuse tool exposure (`tool_groups`, `allow_tools`, `deny_tools`) with guardrail permissions (`tool_permissions`).
- Never try to manage yourself. If the target agent is the current agent, explain that self-management is not allowed.
- Only manage agents owned by the current user. Shared agents are use-only and cannot be edited or reshared by grantees.
- Treat delete as archive-only. Use `action="archive"`; do not promise permanent deletion.
- Share management can grant access to another user. Confirm the target email and executor scope with the user before calling `share_create`, `share_update`, or `share_revoke`.
- Keep permission and tool changes minimal. Preserve existing unrelated settings.

# Tool Usage

- `list` and `get` for inspection.
- `create` for new agents. Include full profile fields when the user provided them.
- `update` for targeted edits to profile, tools, permissions, skills, LLM config, execution, and avatar fields.
- `tools_get`, `tools_set`, `tools_add`, and `tools_remove` for explicit tool assignment CRUD; `describe_tool` and `validate_tool_call` are the unified discovery and preflight path.
- `knowledgebases_get`, `knowledgebases_set`, `knowledgebases_add`, and `knowledgebases_remove` for explicit assigned knowledgebase CRUD.
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
4. **Use configured backend policy.** `web_search` and `web_fetch` select
   their backends from system settings. Fetches keep the configured automatic
   browser fallback available; do not bypass native web tools by calling
   browser tools directly after a failure.
5. **Cross-check.** When sources disagree, surface the disagreement;
   never paper over it. When they agree, you can compress.
6. **Cite.** Every non-trivial claim in the synthesis should reference
   the URL it came from. The user can audit. Use markdown links.
   When images matter, fetch the selected image URLs so they become
   artifacts, then use `artifact_read` to analyze them through the
   vision-capable model routing before embedding them in a document.
7. **Escalate carefully.** If `web_fetch` reports both a primary failure
   and a browser fallback failure in the same error, the controller already
   exhausted the configured browser retry. When
   `web.browser_fetch.headed_fallback_enabled` and `browser.headed_allowed`
   are both enabled, fallback prefers a headed browser; otherwise it uses
   headless. Pick a different source or escalate to the user instead of
   retrying mechanically.
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
    "cognis-pulse-deliverable": {
        "skill_id": "cognis-pulse-deliverable",
        "content": """---
name: Cognis Pulse Deliverable
description: Author validated decision-oriented Pulse Rich Deliverables using the server-owned composition contract.
tags:
  - cognis
  - deliverable
  - pulse
linked_tool_ids:
  - builtin:describe_tool
  - builtin:validate_tool_call
  - builtin:write_deliverable
---

# Purpose

Use this skill when the requested artifact is a Pulse presentation.

# Authoring Contract

1. Before composing content, call `describe_tool` for `write_deliverable`.
2. Select the registered `rich:pulse` operation from the returned descriptor. New writes must use `cognis.rich.pulse.v2` and `metadata.pulse_version=2`; persisted v1 payloads remain renderer-compatible but are not an authoring template.
3. Copy the returned v2 skeleton for the requested Pulse variant and set `action` to `rich:pulse`. Replace its sample values while preserving required block types, slot order, and bounds. Titles and prose are content-specific; do not copy user-specific language from another Pulse.
4. Compose the existing generic blocks into: hero; icon signal dashboard; compact agenda; editorial feature (`research_answer` or `card`) plus actions; cited News and AI accordions; visual monitoring; closing callout; numbered sources.
   - Pulse is visual-first: give every metric a relevant icon, use a strong hero image when it adds meaning, and use `card.variant="visual"` for one or two decision-relevant stories when appropriate media is available.
   - A visual editorial card is an image-led, rounded story tile with a readable overlay. It requires a relevant media reference, specific alt text, and provenance. Use generated images only when a real/authorized source image is unavailable and generation adds editorial value; otherwise use a normal `feature`/`editorial` card.
   - Do not add decorative images merely to fill space. Each visual must illuminate the story, status, place, or decision. Favor one strong image over a gallery of weak ones.
5. Keep collector and synthesis data renderer-neutral. Do not add a giant top-level markdown block, a table of contents, academic numbering, a source/sample/count chart, or an unavailable card for every failed source.
6. Pass the measurable quality gate: at least one non-agenda renderer-safe figure/artifact image, visual editorial card, or meaningful chart; line charts have at least three usable observations; every chart has source and an ISO-8601 timestamp with offset; every News/AI story is a leaf item linked to and citing a declared source; every image has alt text and provenance; multiple stories use progressive disclosure; at most one compact unavailable signal explicitly marked with `status: "unavailable"` or `degraded_data: true`.
7. Call `validate_tool_call` with the complete proposed `write_deliverable` arguments before writing. A `valid=true` result confirms the hard Pulse quality gate passed; the detailed authoritative quality counts are produced in render metadata when the deliverable is written and are then consumed by the evaluator.
8. Call `write_deliverable` only with the validated payload and a concise accessible fallback in `content`.
9. If the server rejects the payload, fix every JSON-path issue using the returned retry guidance and valid skeleton, then retry Pulse.
10. If Pulse remains unsuitable, author a new generic rich payload with `action` set to `write_deliverable` and neither `metadata.presentation`, `metadata.pulse_variant`, nor `metadata.pulse_version`; do not relabel or reuse the rejected Pulse payload, and never label an unvalidated payload as Pulse.

# Safety

- Never persist or claim success for a rejected Pulse payload.
- Never infer the grammar from a prior example when `describe_tool` is available.
- Keep this procedure portable. User-specific sources, titles, locations, preferences, and data belong in the calling task, not this skill.
""",
    },
    "cognis-rich-deliverable": {
        "skill_id": "cognis-rich-deliverable",
        "content": """---
name: Cognis Rich Deliverable
description: Compose excellent use-case-neutral Rich Deliverables (generic write_deliverable format='rich') for any archetype -- RCA, research, newsletters, comparisons, technical reports, and more.
tags:
  - cognis
  - deliverable
  - rich
linked_tool_ids:
  - builtin:describe_tool
  - builtin:write_deliverable
---

# Purpose

Use this skill when writing a `format='rich'` generic deliverable (not Pulse) and the archetype is not obvious, or when the composition needs review before writing. Rich Deliverables are use-case-neutral: there is no preset for most content. Compose the block vocabulary the way a human editor or designer would for the specific reader and content in front of you.

# Core Principle

Compose for a reader, not a form. One clear focal point per deliverable. Prose stays prose -- do not wrap plain narrative in card/status/metric blocks just to look "rich". Use hierarchy (headings, section grouping, a genuine focal block) instead of a wall of same-weight tiles.

# Block Families

- **Status at a glance**: `dashboard`, `metric`, `status`, `status_grid`, `card_grid` -- numeric/state summaries meant to be scanned in seconds. Not for narrative content.
- **Visual editorial stories**: `card` with `variant: "visual"` for an image-led story that benefits from a strong, relevant visual. Supply media with specific alt text and provenance; do not use it for filler or plain prose. Without suitable media, use `feature` or `editorial`.
- **Narrative with evidence**: `research_answer` (direct answer + key_points + citations), `evidence_report`/`claim_cards` (multiple weighed claims, each with its own evidence and confidence), `quote`.
- **Comparison and decision**: `comparison_matrix`, `decision_matrix`, `table` -- options weighed against shared criteria.
- **Sequence and process**: `timeline`, `steps`, `day_agenda`, `incident_timeline`, `checklist` -- anything with inherent order.
- **Prose and structure**: `markdown`, `section`, `stack`, `columns`, `grid`, `hero` -- real paragraphs, long-form reading, layout grouping. Prefer these over `card` for reflective or explanatory prose.
- **Visual evidence**: `chart` (only for genuinely multi-point quantitative series with `source` and `observed_at`; never a 1-2 point or purely categorical fact), `figure`/`gallery` (images with alt text and provenance), `mermaid` (diagrams).
- **Reference and code**: `code`, `kv`/`key_value`, `source_list`, `link`/`link_preview`.
- **Emphasis, sparingly**: `callout` for exactly one true highlight per deliverable (not every fact), `action` for a single explicit next step (not a menu), `divider` to separate real sections (not decoration).
- **Containers**: `tabs`, `accordion`, `modal` for progressive disclosure once there is genuinely more than one story or detail to browse.

# Archetype Recipes

- **RCA / incident dashboard**: hero/title -> `dashboard` or `status_grid` for current impact -> `incident_timeline` for chronology -> `evidence_report` or `research_answer` for root cause -> `table` for affected systems -> `checklist` for remediation actions.
- **Research answer / deep dive**: `research_answer` for the direct answer with key_points and citations -> `evidence_report`/`claim_cards` for supporting claims -> `comparison_matrix` if alternatives were weighed -> `source_list`.
- **Newsletter / digest**: hero -> `card_grid` or `accordion` of story cards (each cited) -> closing `callout` -> `source_list`. Use progressive disclosure (`accordion`/`tabs`) once there is more than a few stories.
- **Product / option comparison**: hero/markdown framing the decision -> a cited `comparison_matrix` or `decision_matrix` as the centerpiece, with exactly one `recommended: true` row when recommending an option -> `callout` for the recommendation -> `research_answer` for reasoning. When individual product imagery or detail is useful, follow the matrix immediately with a `card_grid` containing one `card` per compared product; each card must repeat the exact product name from its matrix row, include its verified media and source links, and summarize only the product-specific trade-offs. Do not emit a detached image-only gallery that forces the reader to map pictures back to rows.
- **Scientific / technical report**: `markdown`/`section` for abstract and prose -> `figure` for diagrams with captions -> `table` for data -> `evidence_report` for claims -> `source_list` for references. Favor real paragraphs over cards.
- **Architecture / design deck**: hero -> `section` per concern with markdown prose -> `mermaid` or `figure` per diagram -> `table` for tradeoffs -> `decision_matrix` if choosing between designs.
- **Notes / freeform visualization**: let the content shape the layout -- `markdown`/`section` for prose, `timeline`/`steps` only if there is a real sequence, `metric`/`dashboard` only if there are real numbers to scan. Do not force structure that is not in the content.
- **Daily pulse / briefing**: use the registered `rich:pulse` operation instead of generic rich for this specific archetype; it is an optional preset, not a quality requirement for anything else.

# Anti-Patterns

- **widget_salad**: many small unrelated card/metric/status tiles with no hierarchy or grouping, forcing the reader to scan everything equally.
- **nested_cards**: a card block containing another card block for no structural reason; prefer a single card or a section/grid of siblings.
- **two_point_chart**: a chart block with only one or two data points or a single category; use `metric`, `status`, or a sentence instead.
- **thesis_as_status_pill**: compressing a substantive claim or finding into a status/metric label instead of a paragraph or `research_answer`.
- **everything_is_a_card**: wrapping plain narrative prose in card/callout blocks purely to look "rich"; use `markdown`/`section` for prose.
- **chart_without_provenance**: a chart with no `source` or `observed_at`.

# Workflow

1. Identify the archetype (or the closest match) from the recipes above; if none fit, let the content shape the layout rather than forcing a recipe.
2. Call `describe_tool` for `write_deliverable` if you need the full block schema, the composition guide, or a worked example.
3. Compose blocks following the block-family guidance, keeping one clear focal point.
4. Write a concise, accessible fallback in `content` alongside the rich payload.
5. Call `write_deliverable` with `format='rich'`.

# Safety

- Presentation presets (like Pulse) are optional fill-in templates for their specific archetype, never a prerequisite for a good generic deliverable elsewhere.
- Do not invent block types outside the registered vocabulary; use `describe_tool` to confirm the current set.
- User-specific sources, titles, and data belong in the calling task, not this skill.
""",
    },
    "office-documents": {
        "skill_id": "office-documents",
        "content": """---
name: Office Documents
description: Create, inspect, validate, render, and safely modify DOCX/XLSX/PPTX files with Cognis Office tools backed by certified OfficeCLI.
tags:
  - office
  - documents
  - docx
  - xlsx
  - pptx
linked_tool_ids:
  - builtin:office_read
  - builtin:office_get
  - builtin:office_query
  - builtin:office_validate
  - builtin:office_render
  - builtin:office_create
  - builtin:office_patch
---

# Purpose

Use this skill when working with Office documents (`.docx`, `.xlsx`, `.pptx`) in Cognis.

# Workflow

- Prefer Cognis tools over raw shell commands: `office_read`, `office_get`, `office_query`, `office_validate`, `office_render`, `office_create`, and `office_patch`.
- Start with `office_read` for a high-level view, then use `office_get` or `office_query` for stable paths/selectors.
- Use `office_patch` with structured `set`/`add`/`remove` operations. Pass `expected_base_sha256` when editing a known source to reject stale edits.
- Use `office_create` for new documents and pass initial operations when useful.
- Validate generated or modified documents with `office_validate`.
- When visual or structural correctness matters, use `office_render` and/or a readback with `office_read`, `office_get`, or `office_query`.

# Operation Shape

Patch/create operations map to OfficeCLI verbs without exposing raw shell:

- `verb`: `set`, `add`, or `remove`
- `path`, `parent`, or `selector`: target scope
- `type`: element type for `add`
- `props`: key/value properties converted to OfficeCLI properties
- `before`, `after`, `index`, `from_path`: positioning or clone controls where supported

# Safety

- Treat Office files as untrusted parser inputs.
- Do not overwrite source artifacts; mutating tools create a temp copy and return a new artifact by default.
- Do not use OfficeCLI watch/editor/browser flows from Cognis tools; browser document editors are out of scope.
- Do not install or run floating `latest` OfficeCLI manually when Cognis Office tools are available.
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
