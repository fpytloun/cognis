# Stage 31: Workflow-First Composition and Ephemeral Workflows

## Status

PLANNED

## Goal

Implement the workflow-first authoring layer described in
[`../27-workflow-composer.md`](../27-workflow-composer.md): the main chat agent
decides when to answer inline, when to use `system:general-task`, and when to
compose a richer workflow via `compose_and_run_workflow`.

This stage does not replace the workflow engine. It makes the workflow engine
practical as the default harness primitive for non-trivial work.

## Dependencies

- `docs/specs/02-agent-model.md`
- `docs/specs/06-tool-system.md`
- `docs/specs/09-ui-ux.md`
- `docs/specs/10-api-spec.md`
- `docs/specs/14-workflow-engine.md`
- `docs/specs/21-workflow-deliverables.md`
- `docs/specs/22-step-profiles.md`
- `docs/specs/27-workflow-composer.md`
- Stage 30 complete (deliverables and step profiles)

## Scope

### In Scope

- `compose_and_run_workflow` controller tool for primary agents
- hidden `system:workflow_composer` and `system:skill_decomposer` agents
- `workflows.lifecycle` and `workflows.archived_at`
- ephemeral workflow creation, task binding, and auto-archive
- workflow promotion flow from task detail into the workflow editor
- skill `steps:` extension and decomposition suggestions
- new coding workflow family members (`system:bug-fix`, `system:code-research`)
- always-attached `cognis-orchestrator` system skill generated from spec text
- UI affordances for composed workflow preview and promotion
- unit and integration coverage for composer validation and fallback behavior

### Out of Scope

- controller-owned compose-vs-inline routing in `DecisionEngine`
- replacing `system:general-task`
- sub-workflows or parallel workflow branches
- a public REST endpoint that mirrors `compose_and_run_workflow`
- non-coding flagship workflow families beyond the composition substrate itself

## Deliverables

### 1. Schema and domain model

- Add `lifecycle` and `archived_at` to workflow persistence and domain models.
- Add skill-side support for optional `steps:` metadata.
- Add matching Alembic migrations and bootstrap `_ensure_*` helpers.

### 2. Hidden composition agents

- `system:workflow_composer` returns validated workflow JSON only.
- `system:skill_decomposer` converts instruction-only skills into step
  fragments when needed.
- Both use cheap structured-output paths and no tools.

### 3. Orchestration tool

- Add `compose_and_run_workflow` to `OrchestrationMode.FULL`.
- Implement one-retry validator feedback loop.
- Fall back to `system:general-task` after repeated invalid composition.

### 4. Skill system extension

- Parse and store optional Cognis `steps:` blocks while remaining compatible
  with official `SKILL.md`.
- Add API/UI support to preview a suggested decomposition before saving.

### 5. Coding workflow family

- Keep `system:software-development`.
- Add `system:bug-fix`.
- Add `system:code-research`.
- Update `cognis-coding` to reference this family as composition material.

### 6. UI and API polish

- Show composed workflow preview cards in chat/task views.
- Add workflow-library filtering for ephemeral vs persistent.
- Add promote-from-task flow that opens the workflow editor pre-populated from
  the ephemeral workflow.
- Add `include_ephemeral` support where workflow listings are used for
  introspection/debugging.

### 7. Tests and telemetry

- unit tests for validation, fallback, archive, and promotion serialization
- integration tests for bug-fix composition, general-task fallback, and
  promotion UX/API flow
- telemetry for composition attempts, validation retries, fallback rate, and
  ephemeral workflow promotions

## Suggested Work Breakdown

### Workstream A: schema and persistence

Files likely touched:

- `cognis/models/workflow.py`
- `cognis/models/skill.py`
- `cognis/store/models.py`
- `cognis/store/migrations/versions/<new>.py`
- `cognis/bootstrap.py`

Tasks:

1. Add workflow lifecycle fields.
2. Add skill `steps:` storage.
3. Add bootstrap helpers plus reversible migration.

### Workstream B: composition agents

Files likely touched:

- `cognis/core/agent_registry.py`
- `cognis/core/system_skills.py`
- new prompt helpers if needed

Tasks:

1. Register hidden composition agents.
2. Add generated `cognis-orchestrator` system skill.
3. Add coding-family workflow definitions.

### Workstream C: orchestration tool handler

Files likely touched:

- `cognis/tools/builtin/orchestration.py`
- `cognis/core/agent_loop.py`
- `cognis/core/workflow_management.py`
- `cognis/core/task_queue.py`

Tasks:

1. Define `compose_and_run_workflow` schema.
2. Resolve hints, run composer, validate output.
3. Persist ephemeral workflows and create task/schedule.
4. Implement fallback to `system:general-task`.

### Workstream D: skills and decomposition

Files likely touched:

- `cognis/tools/skill_parser.py`
- `cognis/tools/skills.py`
- `cognis/api/routes/skills.py`

Tasks:

1. Parse optional `steps:` extension.
2. Surface declared step fragments in skill detail.
3. Add decomposition preview endpoint and route.

### Workstream E: UI and promotion flow

Files likely touched:

- workflow editor routes/components under `ui/src/routes/(app)/workflows/`
- task detail route/components under `ui/src/routes/(app)/tasks/`
- chat timeline components

Tasks:

1. Show composed workflow preview card.
2. Add promote-from-task button.
3. Open workflow editor with pre-populated ephemeral values.
4. Add workflow list filters for lifecycle.

### Workstream F: tests and telemetry

Files likely touched:

- `tests/unit/`
- `tests/integration/`
- metrics/telemetry modules touched by composition flow

Tasks:

1. Add composer validation/fallback tests.
2. Add end-to-end bug-fix composition test.
3. Add telemetry assertions for fallback and promotion.

## Acceptance Criteria

- a primary agent can call `compose_and_run_workflow` and receive a task backed
  by a newly composed workflow
- malformed composer output is retried once and then falls back cleanly to
  `system:general-task`
- ephemeral workflows are hidden from the normal library and auto-archive after
  their task completes
- a completed ephemeral workflow can be opened in the workflow editor as a
  pre-populated draft for saving as a persistent workflow
- skills remain compatible with plain `SKILL.md` and may optionally expose
  reusable workflow step fragments
- coding requests can compose into proportional shapes (`software-development`,
  `bug-fix`, `code-research`) instead of one generic coding workflow
