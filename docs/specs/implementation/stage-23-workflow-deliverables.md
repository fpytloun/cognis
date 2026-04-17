# Stage 23: Workflow Deliverables and Step Profiles (Deferred Structural Work)

## Status

PLANNED

## Goal

Land the structural half of the harness work introduced by the harness stabilization tracks. The stabilization stages fix the controller's correctness, capability, and operational gaps first; this stage then upgrades the workflow contract itself:

1. A workflow step's user-facing output is a typed, versioned **deliverable** authored through a new controller-intercepted tool, not free assistant text.
2. Channel delivery happens exactly once per workflow — gated on `step_complete` acceptance and evaluator approval.
3. Specialized workflows restrict the tool surface with **step profiles** (`research`, `coding`) expressed as rule sets over a tool classification taxonomy. The default profile is `unrestricted`; user-installed MCP tools continue to just work.

This stage turns specs 21 and 22 into code across backend, UI, and telemetry after the harness stabilization stages are complete.

## Dependencies

- `docs/specs/14-workflow-engine.md`
- `docs/specs/21-workflow-deliverables.md`
- `docs/specs/22-step-profiles.md`
- `docs/specs/06-tool-system.md`
- `docs/specs/09-ui-ux.md`
- `docs/specs/23-harness-stabilization.md`
- Stages 20-22 complete (correctness, capability parity, prompt cache and operational resilience).

## Scope

### In Scope

- `step_profile` and `require_deliverable` fields on `StepDefinition` + `tool_overrides`.
- `deliverables` table, `step_runs` deliverable columns, `mcp_tool_classifications` table.
- `side_effect` classification on `ToolDefinition`; MCP discovery classification pipeline.
- `write_deliverable` controller tool (workflow-only) and buffered delivery flow.
- `step_complete` gate extended with `deliverable_missing` reason code.
- Profile-based filtering inside `cognis/core/tool_exposure.py`.
- System workflow wiring per the table in spec 22.
- API routes and WebSocket events for step editor and per-step deliverable views.
- UI: workflow step editor (profile + deliverable + overrides), deliverable panel with versions and evaluator-feedback diff, MCP classification editor.
- Unit and integration tests for the new invariants.
- Telemetry counters for deliverables and profile filtering.

### Out of Scope

- `reporting` and `communication` profiles (no shipped system workflow needs them).
- Trust flag on MCP servers.
- Daily-brief workflow migration (user-level workflow; separate PR).
- A `read_deliverable` reader tool for cross-step content fetch.
- DB-editable profile rule sets.
- Federation/A2A changes to deliverable handling.

## Deliverables

### 1. Domain model and DB migrations

- `cognis/models/workflow.py` — extend `StepDefinition` with `step_profile`, `require_deliverable`, `tool_overrides`.
- `cognis/models/deliverable.py` (new) — `Deliverable` Pydantic model.
- Alembic migration creating `deliverables` and `mcp_tool_classifications`, plus new columns on `step_runs` (`deliverable_id`, `profile_applied`, `require_deliverable`).
- Matching `_ensure_*` bootstrap helpers registered in `cognis.bootstrap.run_schema_bootstrap()`.

### 2. Tool classification

- `cognis/tools/classification.py` (new) — pure helpers:
  - `classify_from_annotations(annotations) -> (category, side_effect)`
  - `classify_from_name(name, description) -> (category, side_effect)` with a narrow, reviewed keyword list
  - `resolve_profile_filter(tools, profile, overrides) -> list[ToolDefinition]`
- `cognis/models/tool.py` — optional `side_effect` field on `ToolDefinition`.
- `cognis/tools/mcp.py` — attach classification to `ToolDefinition` at discovery time using annotations, heuristic, and override table.
- Existing builtins (`cognis/tools/builtin/**`, `cognis/tools/executor/**`) — add explicit `side_effect` to every tool. Non-behavioral, one-line additions.

### 3. `write_deliverable` tool and buffered delivery

- `cognis/tools/builtin/deliverable.py` (new) — controller-intercepted tool handler; versioning, supersede-previous logic, compact tool result.
- `cognis/core/agent_loop.py` — route the tool through controller interception, restrict visibility to workflow contexts, add `deliverable_missing` branch to `step_complete` rejection, preserve Tier 1 todos-pending branch.
- `cognis/core/workflow_engine.py`:
  - evaluator runs against the buffered deliverable (revise creates a new version; approval transitions the version to `approved`);
  - at workflow termination, locate the final delivering step and perform channel delivery once (`approved → delivered`);
  - non-delivering steps (those with `require_deliverable=False`) are allowed to hold a buffered deliverable (visible in UI) but do not trigger channel delivery;
  - legacy non-deliverable path (workflows composed entirely of `require_deliverable=False` steps) remains via `_build_result_summary`.
- `cognis/core/prompts.py` — brief additions to the task-step prompt block that describe the two-step authoring contract (write_deliverable, then step_complete). No placeholder syntax.

### 4. Profile-aware tool exposure

- `cognis/core/tool_exposure.py` — accept `step_profile` and `tool_overrides`; apply filter before deferred-loading/tool-search layer; keep Anthropic/OpenAI strategy handling unchanged.
- `cognis/core/agent_loop.py` — pass `ctx.step_definition.step_profile` and `ctx.step_definition.tool_overrides` through.

### 5. System workflow wiring

- `cognis/core/workflow_registry.py` — set `step_profile` and `require_deliverable` on every shipped step per the mapping in spec 22, section "System Workflow Mapping". No changes to workflow IDs, names, or step sequences.

### 6. API and WebSocket

- `cognis/api/routes/workflows.py` — surface the new fields; reject `tool_overrides` when profile is `unrestricted`.
- `cognis/api/routes/tasks.py` — step-run detail includes deliverable summary plus version list; new endpoints:
  - `GET /api/v1/step-runs/{id}/deliverables`
  - `GET /api/v1/step-runs/{id}/deliverables/{version}`
- `cognis/api/routes/settings.py` (or equivalent MCP routes) — `GET/PUT /api/v1/mcp-servers/{server_id}/tool-classifications`.
- Event bus: new typed events — `DELIVERABLE_WRITTEN`, `DELIVERABLE_APPROVED`, `DELIVERABLE_DELIVERED`, `DELIVERABLE_REVISED`.

### 7. UI

- **Workflow editor** — add `step_profile` dropdown, `require_deliverable` toggle, and `tool_overrides` panel (restrictive profiles only) with live visible-tool-set preview and "Include unclassified" affordance.
- **Deliverable panel** — new component under `ui/src/lib/components/tasks/`; renders latest approved/delivered version, version list with status badges, inline text diff between consecutive versions with evaluator feedback surfaced.
- **Reasoning subpanel** — demote free-text assistant messages during workflow steps into a labelled subpanel; do not send them to channels.
- **MCP server detail** — per-tool classification editor table with columns: name, inferred category, side_effect, source (annotation/heuristic/override), override action.
- **Channel rendering** — route workflow deliverables through a single message render; leave direct chat unchanged.
- **Docs** — short "Step profiles" and "Deliverables" pages under `docs/guide/workflows.md`.

### 8. Telemetry and tests

- Counters listed in specs 21 and 22 (deliverables written/approved/rejected/delivered/superseded, profile filter hides, unclassified hidden).
- Histograms for deliverable size and versions per step_run.
- Unit tests for:
  - classification precedence and rule-set filtering;
  - `write_deliverable` semantics (versioning, supersede, preview truncation);
  - `step_complete` interaction with both todos gate and deliverable gate;
  - workflow engine revise → new version → single-delivery behavior;
  - system workflow mapping produces the expected visible tool surface per step.
- One integration test that replays a `system:general-task`-shaped flow end-to-end and verifies single delivery and no duplicate channel messages on revise.

## Suggested Work Breakdown (Commit Order)

Each commit is green on its own. Intermediate commits do not activate new gates until the enabling commit lands.

### Commit 1: Domain model fields

Files likely touched:

- `cognis/models/workflow.py`
- `cognis/models/deliverable.py` (new)
- `cognis/api/models.py`

Tasks:

1. Add `step_profile`, `require_deliverable`, `tool_overrides` to `StepDefinition`.
2. Introduce `Deliverable` domain model.
3. Extend `WorkflowStepDTO` request/response schemas with the new fields.
4. Default `step_profile="unrestricted"`, `require_deliverable=True`.

### Commit 2: DB migration and bootstrap helpers

Files likely touched:

- `cognis/store/migrations/versions/<new>.py`
- `cognis/store/models.py`
- `cognis/bootstrap.py`

Tasks:

1. Create `deliverables` table with unique `(step_run_id, version)`.
2. Add `deliverable_id`, `profile_applied`, `require_deliverable` columns to `step_runs`.
3. Create `mcp_tool_classifications` table.
4. Register idempotent `_ensure_*` bootstrap helpers for all three changes.
5. Verify against both SQLite and PostgreSQL dialects.

### Commit 3: Tool classification pipeline

Files likely touched:

- `cognis/models/tool.py`
- `cognis/tools/classification.py` (new)
- `cognis/tools/mcp.py`
- `cognis/tools/builtin/**`
- `cognis/tools/executor/**`

Tasks:

1. Add optional `side_effect` field to `ToolDefinition`.
2. Implement `classify_from_annotations` and `classify_from_name` helpers.
3. Apply classification at MCP discovery, honoring `mcp_tool_classifications` overrides.
4. Populate `side_effect` for every builtin and executor-native tool.

### Commit 4: `write_deliverable` tool and buffered delivery

Files likely touched:

- `cognis/tools/builtin/deliverable.py` (new)
- `cognis/core/agent_loop.py`
- `cognis/core/workflow_engine.py`
- `cognis/store/queries.py`

Tasks:

1. Implement controller-intercepted `write_deliverable` handler (versioning, supersede, compact tool result).
2. Expose tool only under `WORKFLOW_POLICY` / `SECONDARY_POLICY`.
3. Update `_deliver_task_result_*` to read from deliverables; preserve legacy path for non-delivering workflows.
4. Ensure evaluator revise produces a new version and does not trigger a second delivery.

### Commit 5: `step_complete` deliverable gate

Files likely touched:

- `cognis/core/agent_loop.py`
- `cognis/core/prompts.py`

Tasks:

1. Add `deliverable_missing` rejection branch with prescriptive teach-back and follow-up system reminder.
2. Preserve and compose with Tier 1 `todos_pending` branch.
3. Surface the new reason in `STEP_COMPLETE_REJECTIONS` metric.
4. Update the `TASK_STEP` prompt with a concise deliverable authoring instruction (no placeholder syntax).

### Commit 6: Profile-aware exposure

Files likely touched:

- `cognis/core/tool_exposure.py`
- `cognis/core/agent_loop.py`
- `cognis/tools/classification.py`

Tasks:

1. Pass `step_profile` and `tool_overrides` through to `prepare_tool_exposure`.
2. Apply `resolve_profile_filter` before deferred-loading/tool-search logic.
3. Ensure `write_deliverable` survives filtering in all profiles (deliverable category exemption).
4. Keep `unrestricted` as identity.

### Commit 7: System workflow wiring

Files likely touched:

- `cognis/core/workflow_registry.py`

Tasks:

1. Set `step_profile` and `require_deliverable` on every step of all five shipped workflows per the mapping in spec 22.
2. Verify `system:direct` keeps `require_deliverable=False` and stays `unrestricted`.
3. Verify `system:software-development.commit`/`remember` keep `require_deliverable=False`.

### Commit 8: API routes and WebSocket events

Files likely touched:

- `cognis/api/routes/workflows.py`
- `cognis/api/routes/tasks.py`
- `cognis/api/routes/settings.py` (or new MCP routes file)
- `cognis/api/models.py`
- `cognis/api/websocket.py`
- `cognis/core/events.py`

Tasks:

1. Surface the new step fields in workflow CRUD; validate `tool_overrides` gated on restrictive profile.
2. Add deliverable endpoints on step-run detail.
3. Add MCP tool-classification endpoints.
4. Emit `DELIVERABLE_*` events on state transitions.

### Commit 9: UI

Files likely touched:

- `ui/src/routes/(app)/workflows/+page.svelte` and step editor components
- `ui/src/lib/components/tasks/DeliverablePanel.svelte` (new)
- `ui/src/lib/components/tasks/ReasoningPanel.svelte` (new)
- `ui/src/routes/(app)/tasks/[taskId]/+page.svelte`
- `ui/src/routes/(app)/settings/+page.svelte` (MCP classification editor)
- `docs/guide/workflows.md` additions

Tasks:

1. Workflow editor: profile dropdown, deliverable toggle, overrides panel with live preview and include-anyway.
2. Deliverable panel with versions, status badges, and evaluator-feedback diff.
3. Reasoning subpanel for non-deliverable free text.
4. Channel rendering routes workflow-backed task messages through the deliverable content.
5. MCP classification editor with effective-source column.
6. Docs page "Step profiles and deliverables".

### Commit 10: Tests

Files likely touched:

- `tests/unit/test_step_profiles.py` (new)
- `tests/unit/test_write_deliverable.py` (new)
- `tests/unit/test_step_complete_deliverable_gate.py` (new)
- `tests/unit/test_workflow_engine_delivery.py` (new)
- `tests/unit/test_tool_exposure_profile.py` (new)
- `tests/integration/test_system_general_task_deliverable_flow.py` (new)

Tasks:

1. Classification precedence tests (annotation → heuristic → override).
2. Profile filter tests for research/coding rule sets and unclassified behavior.
3. `write_deliverable` semantics tests (versioning, supersede, compact result, replace-on-repeat).
4. `step_complete` gate composition tests (`todos_pending` and `deliverable_missing` both enforced).
5. Workflow engine delivery-once tests (revise loop, outbox fallback, no duplicate channel send).
6. System workflow mapping assertions (visible tool surfaces per step per profile).
7. Integration end-to-end deliverable flow.

### Commit 11: Telemetry and docs polish

Files likely touched:

- `cognis/core/workflow_engine.py`
- `cognis/tools/builtin/deliverable.py`
- `cognis/core/tool_exposure.py`
- `docs/guide/workflows.md`, `docs/guide/troubleshooting.md`
- `AGENTS.md`

Tasks:

1. Register all counters and histograms listed in specs 21 and 22.
2. Structured logs with stable reason codes, no content.
3. Update troubleshooting guide with the new rejection codes.
4. Update AGENTS.md with a brief deliverables + profiles section.

## Acceptance Criteria

- `system:direct` end-to-end chat flow unchanged for users; no deliverable required; `write_deliverable` not exposed.
- `system:general-task` `step_complete` is rejected with `deliverable_missing` when the model skips the tool; succeeds once the deliverable is buffered.
- `system:research` and `system:software-development` do not expose filesystem/shell tools in research steps and do not expose unrelated destructive MCP tools in coding steps; unclassified user MCP tools are hidden with a UI include-anyway affordance.
- A workflow with an evaluator `revise` loop produces the user-facing channel message exactly once.
- Every restrictive profile exposes `write_deliverable`; writing it never fails because of profile filtering.
- `mcp_tool_classifications` is empty by default; new MCP servers connect and work in `unrestricted` without any admin action.
- Deliverable content never appears in logs or Prometheus labels.
- Unit suite green; integration suite green; lint/type baseline unchanged.
- AGENTS.md and workflows guide reflect the new model.

## Rollout Notes

- No feature flags. Defaults (`unrestricted`, `require_deliverable=True`) mean behavior changes on merge for workflow steps that already expected a deliverable.
- System workflows are updated in Commit 7, so shipped workflows move coherently.
- User-authored workflows default to `require_deliverable=True`. Authors who need legacy behavior set it to `False` per step. A release-note pointer to spec 21 is sufficient.
- Bootstrap migrations apply on startup; Alembic migrations remain available for operators who run `alembic upgrade head` manually.

## Risks and Mitigations

- **Legacy UI paths render both reasoning and deliverable.** Mitigation: channel-rendering code path for workflow-backed messages reads the deliverable as the single source; reasoning subpanel is UI-only and not sent to channels.
- **Evaluator flakiness cascades into stuck deliverables.** Mitigation: `CompletionConfig.max_attempts` caps revise iterations; `on_exhausted` path surfaces the latest rejected version with a `needs_review` flag rather than looping forever.
- **Classification heuristic false positives.** Mitigation: heuristic is narrow and documented; admin overrides per tool; unclassified tools stay visible under `unrestricted`.
- **Large deliverables pressure storage.** Mitigation: compact tool-result preview; `deliverables.content` in DB only, not re-echoed; evaluator sees content once per revision.
- **Migration on large existing `step_runs` tables.** Mitigation: new columns are nullable; migration is a metadata-only ALTER.

## Non-Goals Reaffirmed

- `reporting` and `communication` profiles.
- Trust flag on MCP servers.
- `read_deliverable` reader tool for cross-step content fetch.
- DB-editable profile rule sets.
- Federation semantics for deliverables.
