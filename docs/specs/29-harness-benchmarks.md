# 29 — Harness benchmarks and Cognis gap analysis

Status: draft, research-only. No code changes.
Scope: prompt design, tool exposure, skill system, plan/todo, model-family
dispatch, search ranking, cap pressure handling. Behavior only — not storage.

## Why this spec exists

Cognis tasks still feel "dumb": the model skips the named skill, calls
`search_tools` with a wrong category, asks for tools that were supposed to be
default-visible, and generally does not act like a well-instructed agent even
when the right tools exist. The exposure work in specs 25–27 closed most of
the mechanical leaks, but behavioral quality is still well below what mature
open harnesses ship today.

This spec benchmarks Cognis against four reference harnesses and enumerates
the concrete gaps. It is explicitly scoped to **behavioral and architectural
gaps**, not performance or infra.

## Harnesses benchmarked

Clones used for inspection live under `~/src/opencode` and
`~/src/_harness_refs/{aider,codex,crush,goose}`.

| Harness | Source | Notes |
|---|---|---|
| **opencode** | TypeScript, GitHub `anomalyco/opencode` | Full agentic coding CLI, skill loader, per-provider system prompts, TodoWrite, plan mode |
| **crush** | Go, GitHub `charmbracelet/crush` | Implements the open Agent Skills standard (agentskills.io); very strong skill-usage prompt |
| **codex** | Rust, GitHub `openai/codex` | Ships with GPT-specific prompts, BM25-backed `tool_search`, `tool_suggest` for missing plugins, strict `update_plan` tool |
| **goose** | Rust, GitHub `block/goose` | Recipes (yaml/json) with sub-recipes and parameters; subagents with `max_turns`; planner-then-executor dispatch |
| **aider** | Python, GitHub `Aider-AI/aider` | Architect/editor pattern; minimal harness, multiple apply formats |

## Executive summary

The mature harnesses converge on a small number of patterns that Cognis
either is missing or implements weakly:

1. **One skill loader, not a skill toolkit.** Opencode and crush expose one
   action to "use a skill": load (or `view`) the SKILL.md. Cognis exposes 10+
   `skill_*` tools that all compete for the same cap budget and confuse model
   choice.
2. **Skill usage is a CRITICAL RULE, not a hint.** Crush puts "LOAD MATCHING
   SKILLS" as rule #14 inside `<critical_rules>` at the top of the system
   prompt. Cognis puts the guidance at the bottom inside `<skills_guidance>`
   and phrases it as advice.
3. **Per-model system prompts.** Opencode and codex ship distinct prompts
   per family (`anthropic.txt`, `gpt.txt`, `gemini.txt`, `beast.txt`,
   `kimi.txt`, `trinity.txt`, `codex.txt`, `copilot-gpt-5.txt`, `gpt_5_2_prompt.md`,
   `gpt-5.2-codex_prompt.md`, `gpt-5.1-codex-max_prompt.md`). Cognis has one
   universal prompt.
4. **`tool_search` uses BM25, not substring scoring.** Codex indexes tool
   search entries with `bm25::SearchEngine` and enforces per-server bucket
   limits (e.g. at most 8 matches per MCP server, with a special larger
   bucket for the `computer-use` server). Cognis uses a naive substring +
   per-term score and a single global limit.
5. **Tool suggestion for capabilities that are not installed.** Codex ships
   `tool_suggest` so that when a user clearly wants a capability that is not
   in the active tools, the model elicits an install/enable flow. Cognis has
   no equivalent; the model just gives up or hallucinates.
6. **Planning is a distinct, structured step.** Opencode has a plan agent
   with a `plan_exit` handoff; codex has an `update_plan` tool with a strict
   state machine; goose has a dedicated planner prompt that either writes a
   plan or asks clarifying questions. Cognis has step profiles but does not
   enforce a plan-before-act cadence for chat or general workflow.
7. **TodoWrite as the live progress artifact.** Opencode, crush, codex, and
   Claude Code all use a `todowrite`/`update_plan` tool with a strict single
   `in_progress` invariant. Cognis has workflows and tasks but not a
   lightweight, in-chat TodoWrite for multi-step reasoning.
8. **Extension/tool count warnings.** Goose warns the user when extensions
   exceed 5 or tool count exceeds 50 because tool-selection accuracy
   degrades. Cognis silently caps at 128 and lets the model drown.
9. **Skills as files, not DB entities.** Opencode and crush use filesystem
   `SKILL.md` with YAML frontmatter, following agentskills.io. Cognis stores
   skills in DB. That's fine as a source-of-truth, but it also means Cognis
   has to re-invent the loader UX.

The rest of this spec expands each gap, maps it to reference code, and lists
a prioritized remediation plan.

---

## Gap catalogue

Each gap lists: what Cognis does, what references do, the observed cost in
Cognis runs, and a remediation outline. No code changes are proposed here
beyond the high-level direction.

### G1. Skill toolkit bloat

**Cognis today**

`cognis/tools/builtin/skill_management.py:42` exposes 10+ tools:
`skill_list`, `skill_load`, `skill_get`, `skill_versions`, `skill_write`,
`skill_asset_write`, `skill_asset_delete`, `skill_delete`, `skill_import_url`,
`skill_restore_version`, `skill_export`.

**References**

- Opencode: a single tool named `skill` with a dynamic description that lists
  available skills in compact form (`~/src/opencode/packages/opencode/src/tool/skill.ts`).
- Crush: skills are just `SKILL.md` files on disk; the harness does not even
  need a dedicated load tool — the model uses `view` on `<location>`
  (`~/src/_harness_refs/crush/internal/skills/skills.go`, `agent/templates/coder.md.tpl`).

**Cost in Cognis**

- Eats cap budget. The captured turn (128 tools cap) had zero `skill_*`
  visible because other categories crowded them out.
- Creates false pathways. The model may pick `skill_get` or `skill_list`
  instead of `skill_load`.
- Increases default-visible surface needed for skill-authoring agents.

**Remediation direction**

- Collapse runtime skill access to a single `skill_load` tool whose
  description dynamically lists available skills (compact form).
- Keep mutation/import/export/versions out of the default tool surface.
  Expose them only when the step profile explicitly opts in (e.g. a
  skill-authoring profile).
- For primary-owner authoring paths, move skill mutation into command/UI
  flow rather than default model tools.

### G2. Weak skill-loading enforcement

**Cognis today**

`cognis/core/context.py:1637` adds a `<skills_guidance>` block near the end
of the immutable prefix with "Review the list above and use `skill_load`..."
and "If the task explicitly names one of the skills above..." guidance.

**References**

- Crush `coder.md.tpl` puts the rule at the top, as `<critical_rules>` item
  14: "LOAD MATCHING SKILLS ... MUST call `view` on its `<location>` before
  taking any other action for that task." It then has a second
  `<skills_usage>` section with a **"MANDATORY activation flow"** and a hard
  "Do NOT skip step 2" instruction.
- Opencode's `system.ts` renders verbose skill list in the system prompt and
  compact in the tool description because "agents seem to ingest the
  information about skills a bit better" that way.

**Cost in Cognis**

- The captured failure mode: task explicitly names `daily-brief`, the list is
  in `<available_skills>`, and the model still skips `skill_load`.

**Remediation direction**

- Promote skill-activation to a `<critical_rules>` block near the top of the
  prompt, worded as a hard rule, not advice.
- Add a second `<skills_usage>` block with explicit activation flow: scan →
  match → load → follow → only then execute.
- Keep the compact summary inside the `skill_load` tool description and the
  verbose list in the prefix.

### G3. One-size-fits-all system prompt

**Cognis today**

`cognis/core/prompts.py` renders one system prompt for all providers/models.

**References**

- Opencode ships distinct prompts: `anthropic.txt`, `beast.txt`, `codex.txt`,
  `copilot-gpt-5.txt`, `default.txt`, `gemini.txt`, `gpt.txt`, `kimi.txt`,
  `trinity.txt`. `session/system.ts` picks based on model ID substring.
- Codex ships `gpt_5_codex_prompt.md`, `gpt_5_1_prompt.md`, `gpt_5_2_prompt.md`,
  `gpt-5.1-codex-max_prompt.md`, `gpt-5.2-codex_prompt.md`,
  `prompt_with_apply_patch_instructions.md`.

**Cost in Cognis**

- GPT-5.x benefits from Responses-specific instructions (tool search, allowed
  tools, namespaces); Anthropic benefits from tool_search tool phrasing;
  Gemini wants structured JSON-oriented instructions.
- A single prompt either under- or over-instructs each family.

**Remediation direction**

- Introduce per-family prompt variants: `anthropic`, `openai_responses`,
  `openai_chat`, `gemini`, `generic`. Compose with the current step-profile
  and task-type prompts.
- Keep the non-family-specific parts centralized; only vary the
  tool-usage / formatting / structured-output instructions.

### G4. Naive `tool_search` ranking

**Cognis today**

`cognis/tools/builtin/tool_search.py:41` ranks by substring hits plus split
terms. Single global limit. No tokenization, no IDF.

**References**

- Codex `core/src/tools/handlers/tool_search.rs:22` uses the BM25 crate with
  `SearchEngineBuilder::with_documents(Language::English, ...)` and enforces
  per-server bucket limits with `limit_bucket`/`default_limit_for_bucket`.
  The `computer-use` MCP server gets a bucket of 20, everything else 8.

**Cost in Cognis**

- Poor precision on multi-term queries like "read saved output by call_id".
- One large MCP server (Todoist, Rohlik, github-copilot) can dominate
  results and starve other servers.

**Remediation direction**

- Switch `search_inventory` to BM25 (rank-bm25 in Python, or a comparable
  lightweight scorer).
- Add per-(server|profile-group) bucket limits with sensible defaults and a
  configurable override for high-cardinality servers.

### G5. No tool-suggest for "not installed"

**Cognis today**

`search_tools` only searches already-installed tools. There is no way for
the model to indicate "this user wants a capability I don't have".

**References**

- Codex ships `tool_suggest` with `DiscoverableTool::{Connector, Plugin}`
  and an install/enable action type
  (`codex-rs/tools/src/tool_suggest.rs`). The description in
  `core/templates/search_tool/tool_suggest_description.md` tightly scopes
  when to call it.

**Remediation direction**

- Add a controller-owned `tool_suggest` that surfaces MCP servers and
  skills that are known-but-disabled or known-but-not-installed and elicits
  a user confirmation.
- Emit a visible action card in UI so the user can install/enable with one
  click.

### G6. No plan-first dispatch

**Cognis today**

Direct chat and general workflow execute immediately. Step profiles exist
but don't enforce a plan-before-act cadence.

**References**

- Opencode has a plan agent with a `plan_exit` tool that hands off to the
  build agent (`packages/opencode/src/tool/plan.ts`).
- Codex has `update_plan` with a strict state machine (pending →
  in_progress → completed, exactly one in_progress at a time)
  (`codex-rs/tools/src/plan_tool.rs`, `gpt_5_2_prompt.md`).
- Goose has a separate planner AI (`prompts/plan.md`) that either writes a
  plan or asks clarifying questions; the plan is then handed to the
  executor in a fresh context.

**Remediation direction**

- Add a lightweight `update_plan`-style tool exposed for step profiles that
  opt in (general-task, implement, research). Enforce single in_progress
  invariant in the controller.
- For long-running workflows, optionally route through a plan step that
  writes a structured plan to the session before the first run step.

### G7. Missing TodoWrite

**Cognis today**

Has a full workflow engine and tasks, but no lightweight "in-turn" checklist
the model can freely update as it works. Users have to run a workflow or a
task for visible progress.

**References**

- Opencode and crush both ship a `todowrite` tool (identical contract,
  strict one-in-progress invariant).
- Claude Code ships the same.
- Crush adds `content` + `active_form` (imperative vs. present continuous)
  for better UI rendering.

**Remediation direction**

- Add a controller `todowrite` tool that persists per-session and emits
  events to the UI. Do not tie it to workflow/StepRun persistence.
- Render todos as a live checklist in chat and `/info`.

### G8. Extension/tool-count warnings

**Cognis today**

`max_tools=128` silently caps. User gets no signal that accuracy will
degrade.

**References**

- Goose `prompts/system.md` emits an explicit suggestion when
  `extension_count > 5` or `tool_count > 50`:
  "Consider asking if they'd like to disable some extensions to improve
  tool selection accuracy."

**Remediation direction**

- Emit a structured warning when inventory exceeds thresholds (configurable
  per step profile).
- Surface the warning in `/info` (we already surface counts as of 25).
- Offer a one-click "trim to step profile" action.

### G9. Plain prompt for agent ambition

Mature harnesses front-load tone/ambition rules. Cognis prompts are terse by
comparison.

**References**

- Codex `gpt_5_2_prompt.md`: "Persist until the task is fully handled
  end-to-end within the current turn whenever feasible: do not stop at
  analysis or partial fixes."
- Crush `coder.md.tpl`: "BE AUTONOMOUS: Don't ask questions — search, read,
  think, decide, act."

**Remediation direction**

- Import the "persistence" rules into the Cognis base prompt, especially
  for agentic/task steps. Keep direct chat gentler.

### G10. Filesystem skill standard

**Cognis today**

Skills are DB-only. Authoring and publishing require API/UI.

**References**

- agentskills.io open standard: `SKILL.md` with YAML frontmatter, optional
  `scripts/`, `reference/`, `assets/` subdirs.
- Opencode scans `.claude`, `.agents`, `skill/**/SKILL.md`,
  `skills/**/SKILL.md` in global and project dirs
  (`packages/opencode/src/skill/index.ts`).
- Crush: `SkillFileName = "SKILL.md"` and `fastwalk` discovery.

**Remediation direction**

- Add filesystem skill loader that complements the DB loader. Project and
  user SKILL.md files resolved on session start.
- Keep DB as source of truth for user-authored/shared skills; filesystem is
  additive and more portable.

### G11. Structural consistency for cache stability

**Cognis today**

We correctly use an immutable prefix, and we pay cache_control attention on
Anthropic. But the model-facing tool list order is not always stable when
cap pressure changes.

**References**

- Goose `prompt_manager.rs` explicitly notes "Stable tool ordering is
  important for multi session prompt caching" and sorts deterministically.

**Remediation direction**

- Ensure sorted output in `_build_inventory_schemas` and `_select_generic_visible_tools`
  across turns so providers with prefix caching benefit. We already sort
  inventory by category priority; extend to visible selection too.

### G12. `<system-reminder>` discipline

Claude Code and opencode use `<system-reminder>` tags to inject per-turn
reminders into tool outputs and user messages. Cognis has the machinery but
doesn't use it for:

- Skill-load reminder after the first user message that matches a skill
  name by substring.
- TodoWrite reminder when a long task is in progress.
- Plan reminder for general-task workflows.

**Remediation direction**

- Add controller-side reminder injection for those three cases, gated on
  step profile.

### G13. Per-family tool tailoring

**Cognis today**

The same tools go to Anthropic and OpenAI with minor schema differences.

**References**

- Opencode selectively enables `apply_patch` only for GPT-5 family and
  falls back to `edit`/`write` for others (`tool/registry.ts:273`).

**Remediation direction**

- Let the tool registry advertise preferred families per tool. For example:
  - `apply_patch` preferred on GPT-5+
  - `edit/multiedit/patch` preferred on Anthropic/Gemini
  - Disable `web_search` for models with native browsing (rare, but cleaner)

---

## Prioritized remediation plan

P0 = immediate, P1 = next, P2 = nice-to-have.

### P0

- **G1 Skill toolkit bloat**: collapse to a single `skill_load` tool for
  default use. Move the rest behind a dedicated "skill authoring" profile.
- **G2 Skill-loading enforcement**: promote the rule into
  `<critical_rules>` near the top of the prompt; add explicit
  `<skills_usage>` activation flow.
- **G7 TodoWrite**: add a minimal in-session TodoWrite tool with a strict
  one-in-progress invariant, and UI rendering.

### P1

- **G3 Per-family system prompts**: ship `openai_responses`, `anthropic`,
  `gemini`, `generic` variants; compose with existing step-profile prompts.
- **G4 BM25 tool search**: replace substring ranking with BM25; add
  per-bucket limits.
- **G6 Plan-first dispatch**: add controller `update_plan` tool with strict
  state machine, opt-in per step profile.

### P2

- **G5 tool_suggest for not-installed capabilities** with UI action cards.
- **G8 Tool-count warnings** surfaced in `/info` and inline.
- **G9 Agent ambition rules** promoted in general-task / task-step prompts.
- **G10 Filesystem skill loader** (`SKILL.md` discovery under `.cognis/skills/`
  and project `skill/`).
- **G11 Stable ordering** in visible selection.
- **G12 `<system-reminder>` discipline** for skill / todo / plan reminders.
- **G13 Per-family tool tailoring** (e.g. `apply_patch` only for GPT-5+).

## Non-goals

- Replacing Cognis DB-backed skills with pure filesystem skills. DB remains
  the authoritative store for shared/authored skills; filesystem loading is
  additive.
- Changing the workflow engine. This spec does not touch task/workflow
  persistence.
- Replacing the step-profile system. The changes here build on it.

## Open questions

1. Do we want `tool_suggest` to run before an Intaris evaluation, or does
   it need its own policy class? Intaris likely already covers it.
2. How do we want to reconcile workflow deliverables with TodoWrite? They
   should not duplicate.
3. Should per-family prompts live as files (opencode) or Python strings
   (current pattern)? Files make editing and diffing easier; Python strings
   integrate better with Python-native templating.

## Reference code pointers

- `~/src/opencode/packages/opencode/src/tool/skill.ts`
- `~/src/opencode/packages/opencode/src/skill/index.ts`
- `~/src/opencode/packages/opencode/src/tool/registry.ts`
- `~/src/opencode/packages/opencode/src/session/system.ts`
- `~/src/opencode/packages/opencode/src/session/prompt/*.txt`
- `~/src/opencode/packages/opencode/src/tool/todowrite.txt`
- `~/src/_harness_refs/crush/internal/skills/skills.go`
- `~/src/_harness_refs/crush/internal/agent/templates/coder.md.tpl`
- `~/src/_harness_refs/crush/internal/agent/tools/todos.md`
- `~/src/_harness_refs/codex/codex-rs/core/gpt_5_2_prompt.md`
- `~/src/_harness_refs/codex/codex-rs/core/gpt-5.2-codex_prompt.md`
- `~/src/_harness_refs/codex/codex-rs/core/src/tools/handlers/tool_search.rs`
- `~/src/_harness_refs/codex/codex-rs/core/templates/search_tool/tool_description.md`
- `~/src/_harness_refs/codex/codex-rs/core/templates/search_tool/tool_suggest_description.md`
- `~/src/_harness_refs/codex/codex-rs/tools/src/tool_suggest.rs`
- `~/src/_harness_refs/codex/codex-rs/tools/src/plan_tool.rs`
- `~/src/_harness_refs/goose/crates/goose/src/prompts/system.md`
- `~/src/_harness_refs/goose/crates/goose/src/prompts/plan.md`
- `~/src/_harness_refs/goose/crates/goose/src/prompts/subagent_system.md`
- `~/src/_harness_refs/goose/crates/goose/src/recipe/mod.rs`
- `~/src/_harness_refs/goose/crates/goose/src/agents/prompt_manager.rs`
- `~/src/_harness_refs/aider/aider/coders/base_prompts.py`
- `~/src/_harness_refs/aider/aider/coders/architect_prompts.py`
