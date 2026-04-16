# Cognis: Step Profiles and Tool Classification

## Purpose

Step profiles give workflow authors a simple way to restrict the tool surface a step sees, so specialized workflows can stay focused without the author having to enumerate individual tools. This spec defines:

- the `step_profile` field on workflow steps
- the three profiles shipped in v1: `unrestricted`, `research`, `coding`
- the tool classification taxonomy (category + side effect)
- the classification pipeline for builtins, skills, and MCP tools
- per-step `tool_overrides` (include/exclude)
- the exposure resolution order and its relationship to deferred loading / tool search

Related specs: [`06-tool-system.md`](06-tool-system.md), [`14-workflow-engine.md`](14-workflow-engine.md), [`21-workflow-deliverables.md`](21-workflow-deliverables.md).

## Motivation

Profiles are **a focus hint, not a security boundary**. Cognis already has security primitives: non-bypassable guardrails, user-scoped executors, and the Intaris evaluate path. Profiles address a different problem — reducing cognitive load on the model for specialized workflows where a tighter surface measurably improves behavior.

The daily-brief regression made this concrete: the model had `edit`, `multiedit`, and `patch` available during a briefing task and produced `multiedit(file_path="/dev/null", edits=[])` no-ops. Narrowing the surface is the structural fix.

Two constraints shape the design:

- **User tools must "just work" by default.** Installing a new MCP server should surface its tools everywhere (direct chat, `generic-task`, user workflows) without any admin review step. Every restriction must be explicit, opt-in, and per-step.
- **Specialized system workflows need tight surfaces.** `system:research` and `system:software-development` benefit from filtering. The user did not ask for those restrictions; the system workflow author did.

## Design Principles

### 1. Permissive by default

The default profile is `unrestricted`. It applies no filter. All tools exposed by the agent's executor are visible. `unrestricted` is the baseline for direct chat, `system:direct`, `system:general-task`, `system:creative`, every user-authored workflow, and every ad-hoc delegation.

### 2. Restrictions are expressed as rule sets over classification

The three restrictive profiles (`research`, `coding`) are defined as rules over `(category, side_effect)`, not as hardcoded tool-name lists. This keeps profiles stable across MCP inventory changes: adding a new MCP tool automatically falls into the right profile bucket if its classification is present.

### 3. Classification is optional and heuristic-assisted

Builtins declare classification in code. Skills declare it in their manifest. MCP tools are classified at discovery time from the MCP tool annotations and narrow name heuristics. Admin overrides exist but are never required for tools to work.

### 4. Unclassified tools under restrictive profiles are hidden, not blocked

Under a restrictive profile, an unclassified MCP tool is hidden from the model but listed in the step editor with a one-click "include anyway" affordance. This respects the "user tools just work" promise — restrictions never silently drop the user's intent.

### 5. Per-step overrides, exclude wins

Each step can add explicit include and exclude lists. Exclude always wins. Overrides apply only when the profile is restrictive.

### 6. Profiles are code, not data

The three profile rule sets are defined in code, reviewed, and stable. Users do not author profiles; they author workflows and pick a profile.

## Profile Semantics

### `unrestricted` (default)

Applies no filter. The tool exposure pipeline returns the full inventory unchanged. `tool_overrides` is ignored under `unrestricted` (there is nothing to override).

### `research`

Intended for steps that gather, analyze, and synthesize information.

Allowed when **all** hold:

- `category ∈ {knowledge, web, memory, time, context, workflow, orchestration, system, deliverable}`
- `side_effect ∈ {readonly}`, OR `category ∈ {memory, deliverable}` and `side_effect ∈ {readonly, write}` (memory and deliverable writes are always allowed)
- tool is not `destructive`

Typical visible tools:

- web search, extract, crawl (readonly)
- `memory_*` (full)
- knowledge/context readers
- `time_*` helpers
- controller tools (`step_complete`, `step_todo_write`, `step_todo_list`, `write_deliverable`)

Typical hidden tools:

- `edit`, `multiedit`, `patch`, `write` (filesystem)
- `bash` / shell
- MCP tools classified as `destructive` or `write` outside `memory`

### `coding`

Intended for steps that modify code, run shells, or use language servers.

Allowed when any hold:

- anything allowed by `research`
- `category ∈ {filesystem, shell, lsp, code}` (all side effects allowed within these categories)

Typical visible tools:

- `research` set ∪
- `edit`, `multiedit`, `patch`, `write`, `read`, `glob`, `grep` (filesystem)
- `bash` (shell)
- LSP diagnostics and code tools

Typical hidden tools:

- `destructive` MCP tools outside the coding categories (for example, messaging `send_*` tools)

## Tool Classification Taxonomy

### Category (primary axis)

Existing `ToolDefinition.category` (string) is retained. Standard values:

`knowledge`, `web`, `memory`, `time`, `context`, `filesystem`, `shell`, `lsp`, `code`, `browser`, `image`, `schedule`, `skill`, `mcp`, `workflow`, `orchestration`, `system`, `deliverable`, `general`.

New categories added by this spec:

- `deliverable` — the `write_deliverable` controller tool.

No backward-incompatible changes to the category set.

### Side effect (new, optional axis)

```python
class ToolDefinition(BaseModel):
    # existing fields
    side_effect: Literal["readonly", "write", "destructive"] | None = None
```

Meaning:

- `readonly` — pure read; never changes external state.
- `write` — changes state but is reversible or limited to owned data.
- `destructive` — irreversibly removes or broadly affects state.

When `side_effect` is `None`, the tool is treated as unclassified: visible under `unrestricted`, hidden under restrictive profiles (subject to overrides).

### Classification for MCP tools

MCP tool metadata may include annotations (`readOnlyHint`, `destructiveHint`, `idempotentHint`, `openWorldHint`). The classification pipeline maps them:

- `readOnlyHint=true` → `side_effect="readonly"`
- `destructiveHint=true` → `side_effect="destructive"`
- `readOnlyHint=false` and no destructive hint → `side_effect="write"`
- no annotations → fall through to heuristic

### Name/description heuristic (narrow)

Applied only when annotations are absent and no admin override exists. Deliberately narrow to avoid false positives:

- name starts with `get_`, `list_`, `search_`, `fetch_`, `read_`, `find_`, `view_`, `describe_` → `readonly`
- name starts with `send_`, `post_`, `publish_`, `create_`, `update_`, `modify_`, `set_`, `add_`, `grant_`, `revoke_`, `assign_`, `upload_` → `write`
- name starts with `delete_`, `remove_`, `drop_`, `purge_`, `destroy_`, `cancel_all_`, `reset_`, `wipe_` → `destructive`

If none match, the tool remains unclassified.

### Admin overrides

Optional per-tool overrides live in a new table `mcp_tool_classifications(server_id, tool_name) PK, category, side_effect, tags JSONB`. Overrides win over annotations and heuristics. Overrides are **optional**; the UI surfaces them but nothing requires them.

## Per-Step `tool_overrides`

```python
class StepToolOverrides(BaseModel):
    include: list[ToolSelector] = []   # re-admit hidden tools under restrictive profile
    exclude: list[ToolSelector] = []   # remove specific tools from the step

class ToolSelector(BaseModel):
    tool_name: str | None = None       # exact tool name (match by display or stable id)
    server_id: str | None = None       # match all tools from an MCP server
```

Semantics:

- Only consulted when `step_profile != "unrestricted"`.
- `exclude` always wins over `include`.
- `include` can re-admit tools that the profile would have hidden. It cannot re-admit anything below the controller's non-bypassable category set (none currently; reserved for future).
- Unclassified tools hidden by a restrictive profile are the most common use of `include`; the editor surfaces them with a one-click affordance.

## Exposure Resolution

Order of operations in `cognis.core.tool_exposure.prepare_tool_exposure`:

1. Start with the agent's effective tool inventory (builtins + executor-provided + skill + MCP).
2. Attach classification to every tool (declared, annotation, heuristic, override).
3. If `step_profile == "unrestricted"` → skip filtering; proceed to step 6.
4. Apply profile rule set: drop tools not matching allowed `(category, side_effect)` cells, keeping in mind unclassified-under-restrictive → hidden.
5. Apply `tool_overrides.include` (re-admit) then `tool_overrides.exclude` (remove). Exclude wins.
6. Feed the resulting set to existing deferred-loading / tool-search logic (Anthropic `cache_control`, OpenAI `tool_search`, generic fallback). This spec does not change that layer.

Profiles are orthogonal to tool exposure strategy (deferred loading, tool search). They simply narrow the inventory input.

## Deliverable Category Exemption

The `deliverable` category (containing only `write_deliverable`) is always included in every restrictive profile's allowed set. Authors never need to override it. This guarantees deliverables work under `research` and `coding` as uniformly as under `unrestricted`.

## System Workflow Mapping

Shipped mapping for v1 (see [`21-workflow-deliverables.md`](21-workflow-deliverables.md) for `require_deliverable` column):

| Workflow | Step | step_profile |
|---|---|---|
| `system:direct` | `execute` | `unrestricted` |
| `system:general-task` | `execute` | `unrestricted` |
| `system:research` | all three | `research` |
| `system:software-development` | all seven | `coding` |
| `system:creative` | `generate` | `unrestricted` |

Rationale: `direct`/`general-task`/`creative` remain fully permissive so user MCPs work out of the box. `research` and `software-development` are the two cases where a tight surface is a clear win, and both are system-authored, so the restriction is scoped to shipped behavior.

## Storage

### `mcp_tool_classifications`

```
mcp_tool_classifications (
  server_id   TEXT NOT NULL,
  tool_name   TEXT NOT NULL,
  category    TEXT NULL,
  side_effect TEXT NULL,    -- one of: readonly, write, destructive
  tags        JSON NULL,
  updated_at  TIMESTAMP NOT NULL,
  PRIMARY KEY (server_id, tool_name)
)
```

Zero rows is the expected state; the table only fills when admins override classifications.

### `step_runs`

Snapshot columns added for audit (see [`21-workflow-deliverables.md`](21-workflow-deliverables.md)):

- `profile_applied` — the profile that actually ran this step.
- The snapshot is helpful when debugging "why didn't this tool show up" after the fact.

## Telemetry

Prometheus counters:

- `cognis_step_profile_total{profile}` — step executions by profile.
- `cognis_step_profile_filter_hides_total{profile, category}` — tools hidden by profile filter, labelled by category (for spotting over- or under-filtering). Does not label by tool name.
- `cognis_step_profile_unclassified_hidden_total{profile}` — unclassified tools hidden under restrictive profile.

Telemetry never logs tool arguments, contents, or server credentials — only counts, categories, and ids.

## UI Surface

- **Workflow step editor** exposes `step_profile` as a dropdown (three options). A help panel lists the allowed categories for the selected profile and shows the agent's visible tool set under those rules.
- When the profile is restrictive, a `tool_overrides` panel appears with two lists (include, exclude) and a "Include all unclassified" shortcut.
- **MCP server detail page** lists the server's tools with columns: tool name, inferred category, inferred side_effect, source (annotation | heuristic | override), and an "Override" action. No review gate, no trust flag.
- **Step-run view** shows the `profile_applied` snapshot in step metadata.

## Failure Modes

| Failure | Behavior |
|---|---|
| Admin overrides an MCP tool classification to something nonsense | The exposure filter still runs; worst case the tool is hidden or included contrary to intent. Overrides are admin-scoped; no runtime safety risk. |
| MCP server returns tool annotations with unexpected shapes | Classification falls through to heuristic, then to unclassified. Tool remains available under `unrestricted`. |
| A tool's category is a new string not in the profile rule set | Treated as unclassified for the profile — hidden under restrictive, visible under `unrestricted`. Adding a new category requires a code change to profile rules. |
| A user picks a restrictive profile on a workflow that depends on an unclassified tool | Step editor warns and offers "Include" for the specific tool. No silent drops. |
| Step runs with both `tool_overrides.include` and `tool_overrides.exclude` listing the same tool | `exclude` wins; the tool is hidden. |

## Security

Profiles do not enforce security. They reduce surface, which indirectly reduces opportunities for tool misuse, but the authoritative gates remain:

- Intaris evaluate on every non-bypassable call.
- User-scoped executor membership (a user's MCPs never leak to other users, regardless of profile).
- Existing circuit breaker and retry/backoff per provider.

Do not rely on a restrictive profile to stop a malicious tool from running. A classification override is not a security boundary.

## Migration and Backward Compatibility

- `side_effect` is additive on `ToolDefinition`; `None` is a valid state and maps to "unclassified" behavior.
- Existing builtins are classified at code-time in the same commit that introduces `side_effect`. No data migration required.
- Existing user-authored workflows deserialize with `step_profile="unrestricted"` and `tool_overrides=None`; behavior is unchanged.
- MCP tools get classification at next discovery; existing cached inventories update on reconnect.

## Non-Goals

- `reporting` and `communication` profiles. Not used by any shipped system workflow. Easy to add later as additive code changes.
- A trust flag on MCP servers. Not needed for profiles; may reappear in a future security spec.
- DB-editable profile rule sets. Profiles are code, not data.
- Heuristic extension via user-configurable regex lists. Out of scope; admin overrides cover the long tail.

## Open Questions (for follow-up specs)

- Whether to expose `side_effect` in the settings UI as a per-builtin read-only indicator. Likely yes, but out of scope for this PR.
- Whether federation/A2A surfaces preserve classification across hops. Tracked in [`08-federation.md`](08-federation.md) for a future revision.
