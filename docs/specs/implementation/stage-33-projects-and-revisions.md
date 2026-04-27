# Stage 33: Projects, Step Metadata Gating, and Human-as-Evaluator Revisions

## Status

PLANNED

## Goal

Ship the work described in
[`../30-projects-and-revisions.md`](../30-projects-and-revisions.md) in
ten reviewable slices:

1. First-class **Projects** with multiple source repositories, optional
   `local_path` hints, owner-only mutation, sharing through
   `project_grants`, and reuse of the existing image pipeline for
   avatars.
2. **Project ↔ workflow** bindings. `project_id` columns on tasks,
   schedules, and conversations. UI filters and assignment.
3. **Path-touch project context** injection that augments the existing
   repo-local instruction loader without replacing it.
4. **Project-aware workflow auto-selection** preferring project-bound
   candidates.
5. **Structured step-completion metadata** with per-step contracts.
6. **Conditional gate DSL** evaluated against prior step outputs and
   metadata.
7. **Step run history** (attempts + supersede chain) preserving prior
   attempts under human revisions.
8. **Task comments** with explicit intent and noop semantics, including
   active-step `context_only` injection at model boundaries.
9. **Human-as-evaluator revision flow** that selects a re-entry step
   (explicit, classifier-selected, or via gate) and rewinds workflow
   state.
10. **Coding workflow content upgrade** that uses the new metadata
    contract and conditional gate to short-circuit autonomous flow when
    the plan is uncertain.

This stage is scheduled after Stage 32 because it depends on workflow
deliverables, step profiles, and the workflow-first composition layer.

## Dependencies

- [`../30-projects-and-revisions.md`](../30-projects-and-revisions.md)
- [`../14-workflow-engine.md`](../14-workflow-engine.md)
- [`../21-workflow-deliverables.md`](../21-workflow-deliverables.md)
- [`../22-step-profiles.md`](../22-step-profiles.md)
- [`../27-workflow-composer.md`](../27-workflow-composer.md)
- [`../28-agent-sharing.md`](../28-agent-sharing.md) (sharing pattern,
  `check_*_access` resolver, no-admin-bypass rule)
- Stages 28, 29, 30, 31, 32 complete

## Scope

### In scope

- DB tables, migrations, and bootstrap helpers for projects, sources,
  workflow bindings, grants, and task comments.
- Project_id columns on tasks, schedules, conversations, with index.
- attempt_number and supersede columns on step_runs and deliverables.
- Pydantic domain models, API request/response models, serializers.
- New project routes and grant routes.
- Builtin tools for project CRUD and revision actions.
- Path-touch project context injection layered on top of the existing
  loader.
- Project-aware workflow registry filtering, decision-engine
  preference, and task-creation eligibility validation.
- Step metadata contract on `step_complete` (per-step schema injection
  and validation).
- Gate condition DSL parser, evaluator, and workflow validator.
- Step run history endpoint and supersede chain.
- Task comment persistence, intents, active-step injection, and
  next-step prompt rendering.
- Revision target classifier, gate fallback, and engine rewind.
- UI: Projects nav and pages, project filter and assignment on tasks,
  workflows, schedules; task comment composer and history; workflow
  editor support for `revision`, `metadata_contract`, and conditional
  gates.
- Tests: unit, contract round-trip, integration coverage.

### Out of scope

- Group/team grantees beyond schema reservation.
- Server-side validation that `local_path` exists.
- Any change to credential storage. `credential_ref` is a clue only.
- New federation, public discovery, or non-coding workflow families
  beyond what already exists.

## Phased rollout inside this stage

Each phase ships migration + bootstrap `_ensure_*` + tests + UI in the
same PR. Phases are merged in order, but later phases can be drafted in
parallel.

| Phase | Name | Notes |
|-------|------|-------|
| 33.1 | Project core | model, CRUD, sources, avatars, UI list/detail |
| 33.2 | Project links | `project_id` on tasks/schedules/conversations + filters/assignment UI |
| 33.3 | Project sharing | `project_grants`, grants UI, composition with agents |
| 33.4 | Path-touch project context | controller enrichment + agent project tools |
| 33.5 | Workflow auto-selection | eligibility filtering + classifier preference |
| 33.6 | Step metadata contract | `metadata_contract`, schema injection, validation |
| 33.7 | Gate condition DSL | parser, evaluator, workflow validator hook |
| 33.8 | Step run history | `attempt_number`, supersede chain, history endpoint |
| 33.9 | Task comments | persistence, intents, active-step injection, UI composer |
| 33.10 | Human revision flow | classifier, gate fallback, engine rewind |
| 33.11 | Coding workflow upgrade | plan metadata + low-confidence/high-risk gate |

## Deliverables

### 33.1 Project core

DB + bootstrap:

- `cognis/store/models.py` — `ProjectRow`, `ProjectSourceRow`.
- `cognis/store/migrations/versions/<rev>_projects.py` — create tables.
- `cognis/bootstrap.py` — `_ensure_projects_tables()` registered in
  `run_schema_bootstrap()`.

Domain + API:

- `cognis/models/project.py` — `Project`, `ProjectSource`, `ProjectStatus`.
- `cognis/api/models.py` — `ProjectCreateRequest`, `ProjectUpdateRequest`,
  `ProjectResponse`, `ProjectSourceCreateRequest`,
  `ProjectSourceUpdateRequest`, `ProjectSourceResponse`.
- `cognis/api/serializers.py` — `project_to_response`,
  `project_source_to_response`.
- `cognis/api/routes/projects.py` — CRUD endpoints listed in spec 30.
- `cognis/api/app.py` — register router.

Tools:

- `cognis/tools/builtin/projects.py` — `list_projects`, `get_project`,
  `create_project`, `update_project`, `delete_project`,
  `add_project_source`, `update_project_source`,
  `remove_project_source`.

Avatars:

- Reuse existing image pipeline. Add
  `POST /api/v1/projects/{project_id}/avatar/generate` convenience
  wrapper that calls the same generator used by agent avatars. Project
  responses emit `avatar_url = f"/api/v1/images/{avatar_image_id}"`
  when set.

UI:

- `ui/src/lib/types/api.ts` — `Project`, `ProjectSource`, `ProjectDetail`.
- `ui/src/lib/api/client.ts` — `api.projects` namespace.
- `ui/src/routes/(app)/projects/+page.svelte` — list/search/create.
- `ui/src/routes/(app)/projects/[projectId]/+page.svelte` — detail
  (sources, instructions, avatar, workflows placeholder for 33.2).
- Nav entries in `+layout.svelte` and `BottomTabBar.svelte`.
- Avatar editor reuses existing agent avatar component.

Tests:

- `tests/unit/test_projects_routes.py` — CRUD, ownership, source CRUD,
  avatar reference round-trip.
- `tests/unit/test_api_contracts.py` — `ProjectResponse` round-trip.
- `tests/unit/test_ui_contract_sync.py` — `Project` interface coverage.

### 33.2 Project links

DB + bootstrap:

- Migration: add `tasks.project_id` (index), `tasks.attempt_number`,
  `schedules.project_id`, `conversations.project_id`,
  `project_workflows` table.
- Bootstrap: `_ensure_project_id_links` and `_ensure_project_workflows_table`.

Domain + API:

- `Task`, `TaskModel`, `ScheduleModel`, `ConversationModel` += `project_id`.
- API request/response models extended.
- `cognis/store/queries.py` — query helpers for `project_workflows`.
- Project routes:
  `POST /api/v1/projects/{id}/workflows/{workflow_id}` and `DELETE`.
- Conversations create/update accept and persist `project_id`.
- Task creation/update accept `project_id`. When omitted and the source
  conversation has `project_id`, inherit it.

UI:

- `ui/src/lib/types/api.ts` — extend `Task`, `TaskDetail`, `Workflow`,
  `Schedule`, `Conversation`, `ConversationContext`.
- `ui/src/lib/tasks.ts::TaskFilterState` += `projectId`;
  `matchesTaskFilters` filters by `task.project_id`.
- Tasks board adds Project selector with URL-persisted filter.
- `CreateTaskModal` adds Project selector; Workflow picker filtered to
  project-bound + generic candidates.
- Task detail config modal adds Project field.
- Workflows page adds project-binding panel and Project filter.
- Schedules pages add Project field with eligibility-aware workflow
  picker.

Tests:

- `tests/unit/test_task_queue.py` — `project_id` propagation through
  `submit`/`create_draft`/`update_task_fields`.
- `tests/unit/test_projects_routes.py` — workflow attach/detach,
  inheritance from conversation.
- UI test for filter state and project-aware workflow picker.

### 33.3 Project sharing

DB + bootstrap:

- Migration: `project_grants` table mirroring `agent_grants` shape.
- Bootstrap: `_ensure_project_grants_table`.

API:

- `cognis/api/common.py::check_project_access(request, db, project, *, required)`
  mirroring `check_agent_access`. Returns owner allow, grantee allow on
  active `use` grant, never on admin role.
- Grants routes under `/api/v1/projects/{project_id}/grants`.
- Project list returns owned + shared.
- All project mutation routes use `check_project_access(required="manage")`
  and enforce owner-only mutation; grantees see read-only project, and
  may set the `project_id` on their own tasks/schedules/conversations.

UI:

- Sharing tab on project detail (owner view).
- Read-only banner on project detail (grantee view).
- "Shared with me" section on the project list.

Tests:

- `tests/unit/test_projects_routes.py` — admin-no-bypass matrix.
- `tests/unit/test_api_contracts.py` — `ProjectGrantResponse`.

### 33.4 Path-touch project context

- `cognis/core/project_runtime.py` — `resolve_project_for_path`,
  `resolve_project_for_task`, `build_project_context_message`.
- Extend `cognis/core/session_cache.py::CachedSessionState` with
  `project_metadata` plus `get/store_project_metadata`.
- Extend `agent_loop._ensure_known_project_context_loaded` to inject
  `<project_metadata>` once per session for project-bound tasks.
- Extend `agent_loop._maybe_load_project_context_before_tool` to match
  touched paths against `project_sources.local_path` prefixes and
  inject metadata even when the path does not exist.
- Sanitize: never serialize secret values; `credential_ref` exposed as
  label only.

Tests:

- `tests/unit/test_project_context_injection.py` — task with project,
  path-touch enrichment, missing local path still injects, cache hit
  on subsequent calls.

### 33.5 Workflow auto-selection

- `cognis/core/workflow_registry.py::list_all` and `get` accept
  `project_id`. Filter by eligibility = generic ∪ project-bound matching.
- `cognis/core/decision.py::select_workflow` runs a project-bound first
  pass over heuristic patterns; classifier candidates carry
  `project_match: bool` flag.
- `cognis/core/turn_scheduler.py::_select_workflow` plumbs project context.
- `task_queue.submit`, task API routes, and `update_task_fields`
  validate eligibility and return 400 on mismatch.
- Default workflow resolution prefers `project.default_workflow_id`
  before agent default.

Tests:

- `tests/unit/test_project_workflow_selection.py` — heuristic + classifier
  preference, fallback to default.

### 33.6 Step metadata contract

- `cognis/models/workflow.py` — add `StepRevisionConfig`,
  `StepCompletionMetadataField`, `StepCompletionContract`. Extend
  `StepDefinition` with `revision` and `metadata_contract`. Extend
  `StepOutput` with `metadata`.
- `cognis/tools/builtin/workflow.py::STEP_COMPLETE_TOOL` — accept
  optional `metadata` object.
- `cognis/core/agent_loop.py::_build_controller_tool_schemas` — inject
  per-step required metadata fields into the visible schema.
- `_validate_controller_tool_arguments` — enforce required fields and
  type/enum constraints; structured `is_error=true` on violation.
- Persist `metadata` in `step_runs.output`.

Tests:

- `tests/unit/test_step_metadata_contract.py` — schema injection, value
  validation, enum enforcement, missing required field produces
  structured error.

### 33.7 Gate condition DSL

- `cognis/core/gate_conditions.py` — tokenizer, parser, evaluator. No
  arbitrary Python `eval`. References to `metadata.<step>.<field>` and
  `outputs.<step>.<key>`. Operators per spec.
- `GateConfig` += `conditions: list[GateCondition]`.
- `workflow_engine._handle_gate_step` — when `conditions` non-empty,
  fire only on at least one true expression; else `return "continue"`.
- `workflow_registry._validate_workflow` — call
  `validate_gate_conditions(workflow)` to check references resolve.

Tests:

- `tests/unit/test_gate_conditions.py` — DSL parse/eval coverage,
  bad-reference detection at validation time, gate-skip when all false.

### 33.8 Step run history

- DB: `step_runs.attempt_number INT NOT NULL DEFAULT 1`,
  `step_runs.superseded_by_step_run_id VARCHAR NULL`,
  `deliverables.attempt_number INT NOT NULL DEFAULT 1`.
- Bootstrap: `_ensure_step_run_history_columns`.
- `StepRunStatus` += `superseded`.
- `workflow_engine._run_step` — reuse latest `step_run` only on
  matching `(task_id, step_name, attempt_number)`.
- `cognis/store/queries.py::list_step_run_history(task_id, step_name)`.
- New API: `GET /api/v1/tasks/{task_id}/steps/{step_name}/history`.
- UI: per-step attempts panel in task detail.

Tests:

- `tests/unit/test_step_run_history.py` — supersede chain creation,
  ordering, response shape.

### 33.9 Task comments

DB + bootstrap:

- `task_comments` table.
- Bootstrap `_ensure_task_comments_table`.

Domain + API:

- `cognis/models/comment.py` — `TaskComment`, `TaskCommentIntent`.
- `cognis/api/models.py` — `TaskCommentCreateRequest`,
  `TaskCommentUpdateRequest`, `TaskCommentResponse`.
- Routes: `GET/POST /api/v1/tasks/{task_id}/comments`,
  `PATCH /api/v1/tasks/{task_id}/comments/{comment_id}`.
- `record_only` is the default; never changes status (including
  terminal). UI forces explicit intent for any action.

Active-step injection:

- `agent_loop._execute_step` drains unapplied `context_only` and
  `request_revision` comments at the next model boundary, never
  mid-tool-batch.
- `_build_step_prompt` renders a `## User Comments` section for
  unapplied `context_only` comments on subsequent step prompts.

Tools:

- `list_task_comments`, `add_task_comment`, `apply_revision` registered
  in `tools/builtin/orchestration.py`.

UI:

- `ui/src/lib/components/tasks/TaskComments.svelte` with composer
  (body, `noop` checkbox default ON, four explicit intent buttons,
  optional target step picker for revisions).
- Comment history list with intent badges, applied/pending state, and
  attempt number.

Tests:

- `tests/unit/test_task_comments.py` — persistence across attempts,
  noop default, terminal-task `record_only` status invariance, active
  injection ordering.

### 33.10 Human revision flow

- `cognis/core/revision.py` — `RevisionTarget`,
  `select_revision_target`. Layered selection: explicit user choice,
  classifier path, gate fallback under threshold (default 0.65,
  setting `workflow.revision.min_confidence`).
- `workflow_engine.apply_human_revision(task_id, comment_id,
  target_step)` — implements the rewind described in spec 30:
  - increment `task.attempt_number`,
  - mark target+downstream `step_runs` and deliverables `superseded`,
  - drop `state.step_outputs` keys for those steps,
  - reset `state.loop_iterations` keys,
  - set `state.last_evaluation_feedback` and
    `state.last_revision_context`,
  - set `state.current_step_index = target_index`,
  - transition task back to `queued`/`running`,
  - mark comment `applied=True` with chosen target and confidence.
- `_build_step_prompt` renders `## Previous Attempt` and
  `## Human Evaluation` sections on the re-attempt target step.
- No cap on human-driven revisions.

UI:

- Revision composer surfaces classifier recommendation with confidence;
  user can override target.
- Gate fallback uses existing notification UI with per-step `revise(...)`
  options.

Tests:

- `tests/unit/test_revision_target_classifier.py` — explicit / classifier
  / gate-fallback paths.
- `tests/unit/test_human_revision_workflow.py` — invalidates target +
  downstream outputs, preserves history rows, populates revision
  context, increments attempt number, transitions task state.

### 33.11 Coding workflow upgrade (content)

- Edit `system:software-development` plan step to add a
  `metadata_contract` requiring `confidence`, `risk`, `decisions`,
  `open_questions`.
- Insert a `pre_implement_gate` with conditions:
  `metadata.plan.confidence < 0.6 or metadata.plan.risk == "high"`.
- Update workflow registry tests accordingly.

## Acceptance criteria

- All deliverables in 33.1–33.11 land with migrations + bootstrap +
  tests + UI in the same PR per phase.
- Owners can CRUD projects/sources/workflows/grants/avatars; grantees
  can read and use; admin has no bypass on owner-owned projects.
- Project source `local_path` is treated as a hint; missing checkouts
  still inject metadata; agents can plan to clone or set up.
- Tasks, schedules, conversations carry optional `project_id`
  end-to-end. Conversation-bound projects propagate to created tasks
  unless overridden.
- Project-bound workflows are ineligible for tasks outside their
  projects, and rank above generic workflows for project tasks.
- `step_complete` enforces step metadata contracts; conditional gates
  evaluate the DSL deterministically and skip on no-match.
- Comments persist; `record_only` never flips status, including on
  terminal tasks; `context_only` injects only at model boundaries.
- `request_revision` reopens the same task: target+downstream outputs
  invalidated, prior step runs preserved, fresh `step_runs` row for
  the target, classifier-or-explicit-or-gate-driven step selection.
- `workflow.revision.min_confidence` setting controls classifier
  threshold (default 0.65).
- Avatars on projects use the existing image pipeline.
- `tests/unit/test_api_contracts.py` and
  `tests/unit/test_ui_contract_sync.py` pass.

## Risks and mitigations

- **DSL scope creep.** Keep operator and reference whitelist strict.
  Reject unknown identifiers at workflow validation time. Cover with
  parser unit tests.
- **History bloat.** Step runs and deliverables grow per attempt. Mark
  superseded rows clearly; UI lazy-loads history. Future retention
  policy can prune by age or by terminal-state filter; out of scope
  here.
- **Classifier hallucination.** Guard with confidence threshold + gate
  fallback. Always allow user to override the picked target step.
- **Cross-feature regression.** Each phase is independently mergeable
  but the spec ships as one document. Run the existing harness and
  workflow integration tests in CI for every phase.
- **Sharing surface.** Reuse `agent_grants`-style code paths to keep
  the resolver behavior consistent. The "no admin bypass" invariant is
  asserted by an explicit unit test in 33.3.
- **Path-touch performance.** Project source path matching uses an
  in-memory cache keyed on `(user_email, project_id)`; eviction follows
  session cache TTL. Document the cost in `13-nfr-operations.md`
  alongside other context-assembly costs.

## Stage exit

Update the tracker in
[implementation/README.md](README.md): Stage 33 DONE. Add a follow-up
note that `check_project_access` is the canonical resolver going
forward and that workflow editors must surface step revision and
metadata fields from this stage onward.
