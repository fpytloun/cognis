# Cognis: Step Profiles and Tool Classification

## Purpose

Step profiles give workflow authors a simple way to restrict the tool surface a step sees, so specialized workflows can stay focused without the author having to enumerate individual tools. This spec defines:

- seeded step-profile presets plus inline step-level profile matrices
- the `step_profile_id`, `step_profile_mode`, and inline `step_profile` fields on workflow steps
- the tool classification taxonomy (category + capabilities)
- the classification pipeline for builtins, skills, MCP tools, and Intaris MCP tools
- per-step explicit include/exclude overrides
- the exposure resolution order and its relationship to deferred loading / tool search

Related specs: [`06-tool-system.md`](06-tool-system.md), [`14-workflow-engine.md`](14-workflow-engine.md), [`21-workflow-deliverables.md`](21-workflow-deliverables.md), [`27-workflow-composer.md`](27-workflow-composer.md).

## Motivation

Profiles are **a focus hint, not a security boundary**. Cognis already has security primitives: non-bypassable guardrails, user-scoped executors, and the Intaris evaluate path. Profiles address a different problem — reducing cognitive load on the model for specialized workflows where a tighter surface measurably improves behavior.

Coding workflows made this concrete: a research-only explanation step had
`edit`, `multiedit`, and `patch` available, so the model spent tokens exploring
write paths it never needed. Narrowing the surface is the structural fix.

Two constraints shape the design:

- **User tools must "just work" by default.** Installing a new MCP server should surface its tools everywhere (direct chat, `generic-task`, user workflows) without any admin review step. Every restriction must be explicit, opt-in, and per-step.
- **Specialized system workflows need tight surfaces.** `system:research` and `system:software-development` benefit from filtering. The user did not ask for those restrictions; the system workflow author did.

## Design Principles

### 1. Permissive by default

The default profile is `unrestricted`. It applies no filter. All tools exposed by the agent's executor are visible. `unrestricted` is the baseline for direct chat, `system:direct`, `system:general-task`, `system:creative`, every user-authored workflow, and every ad-hoc delegation.

### 2. Restrictions are expressed as a matrix over classification

Profiles are defined as a matrix over `(category, capability)`, not as hardcoded tool-name lists. This keeps profiles stable across MCP inventory changes: adding a new MCP tool automatically falls into the right profile bucket if its classification is present.

### 3. Classification is shared across all tool sources

Builtins and executor-native tools declare classification in code. Skills can declare it in their manifests. MCP tools and Intaris MCP tools are classified from metadata, narrow heuristics, and an LLM classifier fallback. Read-only tools are classified too.

### 4. Soft and hard modes are separate

`soft` mode narrows the default-visible tool surface to the profile matrix plus explicit includes/excludes, while keeping the searchable inventory broad for that step. `hard` mode narrows both the visible surface and the searchable inventory to the hard-approved subset.

### 5. Per-step overrides, exclude wins

Each step can add explicit include and exclude lists. Exclude always wins. Overrides apply only when the profile is restrictive.

### 6. Profiles are code, not data

The three profile rule sets are defined in code, reviewed, and stable. Users do not author profiles; they author workflows and pick a profile.

## Profile Semantics

### Presets and inline matrices

Shipped presets are seeded in code and can be referenced by `step_profile_id`. A step can also provide an inline `step_profile` object with a matrix plus include/exclude overrides. When both are present, the inline matrix augments or replaces rows from the preset.

### Capability columns

The capability columns are:

- `read`
- `write`
- `privileged`
- `destructive`

### Seeded presets in v1

- `system:direct-default`
- `system:general-task`
- `system:research`
- `system:coding`
- `system:review`

These presets are intentionally agent-oriented rather than classical automation-oriented. They describe the default tool surface for an agent working inside a workflow step.

Typical tools hidden by current presets:

- tools outside the profile matrix for the current preset
- tools explicitly excluded by `tool_overrides.exclude` such as `get_status` and `list_agents`
- tools filtered out by `hard` mode or not re-admitted by `tool_overrides.include`

### `system:direct-default`

The shipped direct-chat preset is intentionally soft. It exposes by default:

- the tools that match the preset matrix, including read-only `filesystem`, `web`, `datetime`, `system`, and `development` groups plus read/write `memory`
- explicit includes for `delegate` and `create_task`
- explicit excludes for noisy meta-tools such as `get_status` and `list_agents`

Because the preset runs in `soft` mode, tools outside that visible subset can still be discovered later when the model needs them.

### `system:research`

Research presets focus on read-heavy categories with memory writes still available.

### `system:coding`

Coding presets extend research-style access with `filesystem`, `shell`, `lsp`, and other implementation-oriented categories.

### `system:review`

Review presets are read-heavy like research, but include code-inspection categories such as `filesystem` and `lsp` without default write-heavy implementation access.

## Skill Activation

`skill_load` is a first-class tool-surface mutation point.

1. The model loads a skill with `skill_load`.
2. Cognis injects the skill instructions into protected context for the current turn.
3. Cognis resolves tool exposure for the skill using one of two paths:
   - declared path: if the skill manifest or version declares tool summaries that resolve to tool ids, those ids are activated directly
   - classified path: if the skill has no declared tool ids, Cognis retrieves BM25-ranked candidate tools from the current hidden searchable inventory and asks the configured `classifier` model to conservatively choose zero or more tool ids
4. The resolved tool ids are activated for the session and become part of subsequent model-facing visibility.

Classification is cached per session by `(skill_id, content_hash)`.

- Empty classifier results are cached and treated as a valid no-match outcome.
- The cache is intentionally session-local only. It is not persisted to Redis because tool inventories can change across runtime contexts.
- Out-of-profile tools are never activated even if the classifier tries to select them, because candidate generation only uses the current eligible inventory.

## Per-Turn Relevant Retrieval

Before the first model call of a turn, Cognis can run a classifier over the runtime skill summaries and inject a `<relevant_skills>` system block. This is a suggestion mechanism, not an automatic skill load.

Current behavior:

- the classifier evaluates the current task against the available runtime skill summaries
- empty results are allowed and preferred over low-confidence noise
- `retrieve_relevant_tools()` is still used as the first-stage candidate generator for the skill-tool classifier path described above

Current threshold in code:

- tool candidate retrieval: `8.0`

## Tool Classification Taxonomy

### Category (primary axis)

Existing `ToolDefinition.category` (string) is retained. Standard values:

`knowledge`, `web`, `memory`, `time`, `context`, `filesystem`, `shell`, `lsp`, `code`, `browser`, `image`, `schedule`, `skill`, `mcp`, `workflow`, `orchestration`, `system`, `deliverable`, `general`.

New categories added by this spec:

- `deliverable` — the `write_deliverable` controller tool.

No backward-incompatible changes to the category set.

### Capabilities (new axis)

```python
class ToolDefinition(BaseModel):
    # existing fields
    capabilities: list[Literal["read", "write", "privileged", "destructive"]] = []
```

Meaning:

- `read` — pure read or observation.
- `write` — mutates owned or local state.
- `privileged` — broad system, browser, shell, or sensitive capability.
- `destructive` — irreversible or broadly damaging action.

When `capabilities` is empty, Cognis derives a default from `read_only` and narrow heuristics. Dynamic tools can be refined further by the classifier pipeline.

### Classification for MCP tools

MCP tool metadata may include annotations (`readOnlyHint`, `destructiveHint`, `idempotentHint`, `openWorldHint`). The classification pipeline maps them into capabilities and can refine category from the tool metadata, name, description, and an LLM classifier fallback.

- `readOnlyHint=true` → `capabilities=["read"]`
- `destructiveHint=true` → include `"destructive"`
- `readOnlyHint=false` and no destructive hint → include `"write"`
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
4. Apply profile rule set:
   - `soft`: keep the searchable inventory broad, but make only tools that match the allowed `(category, side_effect)` cells default-visible
   - `hard`: drop tools outside the allowed cells so they are neither visible nor searchable
   - in both modes, unclassified-under-restrictive remains hidden unless re-admitted explicitly
5. Apply `tool_overrides.include` (re-admit) then `tool_overrides.exclude` (remove). Exclude wins.
6. Feed the resulting set to existing deferred-loading / tool-search logic (Anthropic `cache_control`, OpenAI `tool_search`, generic fallback). This spec does not change that layer.

Profiles are orthogonal to tool exposure strategy (deferred loading, tool search). They simply narrow the inventory input.

## Deliverable Category Exemption

The `deliverable` category (containing only `write_deliverable`) is always included in every restrictive profile's allowed set. Authors never need to override it. This guarantees deliverables work under `research` and `coding` as uniformly as under `unrestricted`.

Composed workflows use the same profile contract. The workflow composer should
reuse profile choices from referenced templates and step fragments instead of
defaulting every composed step to `unrestricted`.

## System Workflow Mapping

Shipped mapping for v1 (see [`21-workflow-deliverables.md`](21-workflow-deliverables.md) for `require_deliverable` column):

| Workflow | Step | step_profile |
|---|---|---|
| `system:direct` | `execute` | `system:direct-default` |
| `system:general-task` | `execute` | `system:general-task` |
| `system:research` | all three | `research` |
| `system:software-development` | `plan` | `research` |
| `system:software-development` | `architect_review`, `code_review` | `review` |
| `system:software-development` | `implement`, `update_docs`, `commit` | `coding` |
| `system:software-development` | `remember` | `system:direct-default` |
| `system:software-development` | `final_summary` | `research` |
| `system:bug-fix` | all four | `coding` |
| `system:code-research` | both steps | `coding` |
| `system:creative` | `generate` | `system:general-task` |

Rationale: direct and general-purpose system workflows now use named soft presets instead of `unrestricted`. This still keeps the default experience broad, but lets the controller hide noisy meta-tools and keep workflow behavior consistent across direct chat, general tasks, creative work, and memory-heavy follow-up steps.

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

Runtime logs should also record:

- skill suggestion classifier outcomes, including accepted skill ids
- BM25 tool-candidate retrieval outcomes for skill activation and `search_tools`
- skill activation resolution path (`declared` vs `classified`) and the activated tool ids
- skill-tool classifier cache hits, misses, and empty-result decisions

These logs must continue to avoid raw user message content, skill instructions, tool arguments, tool results, and secrets.

## Known Limitations

- Current BM25 tokenization is ASCII-oriented, so multilingual retrieval remains weaker than English retrieval.
- Skill suggestion and skill-tool classification deliberately prefer empty results over weak matches. This keeps false positives down, but some low-signal skills may require explicit `skill_load`.

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
