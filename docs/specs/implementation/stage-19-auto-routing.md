# Stage 19: Auto Routing for Agents and Workflows

## Status

PLANNED

## Goal

Implement controller-owned auto-routing for delegation and task creation so
Cognis can pick the right execution shape, workflow, and agent while
preserving explicit user intent, workflow constraints, and capability ceilings.

This stage turns the routing implementation plan into code with deterministic
fallbacks, execution-envelope enforcement, and routing telemetry.

## Dependencies

- `docs/specs/02-agent-model.md`
- `docs/specs/06-tool-system.md`
- `docs/specs/10-api-spec.md`
- `docs/specs/14-workflow-engine.md`
- `docs/specs/20-auto-routing-implementation-plan.md`

## Scope

### In Scope

- `agent_id="auto"` / `"self"` support in `delegate` and `create_task`
- shared routing helper for delegation and task creation
- workflow preferred-agent and hard-required-agent precedence
- eligible candidate filtering for system and user agents
- deterministic heuristics plus classifier fallback
- execution-envelope subset enforcement
- runtime-led `wait` derivation
- routing telemetry and regression coverage

### Out of Scope

- major redesign of the tool surface beyond current `bash` strategy
- advanced operator dashboards beyond required routing metrics and logs
- large UI redesigns unrelated to exposing the new routing semantics

## Deliverables

### 1. API normalization and semantics

- normalize omitted / empty / `auto`
- support `self`
- deterministic errors for invalid forced selections

### 2. Shared routing helper

- one shared pipeline for delegation and task creation
- deterministic precedence and reason codes
- workflow preference and hard-constraint handling

### 3. Candidate filtering and ranking

- eligibility filter for system and user agents
- bounded candidate set
- compact candidate summaries for classifier input

### 4. Execution-envelope enforcement

- parent envelope computation
- feasible-candidate prefilter
- child envelope materialization
- fail-closed behavior for empty intersections where required

### 5. Classifier fallback

- unified classifier contract for mode/workflow/agent selection
- timeout and confidence thresholds
- deterministic fallback behavior

### 6. Telemetry and tests

- routing metrics and structured logs
- unit and integration tests covering routing, fallback, and capability ceilings

### 7. Model-family prompt blocks

- feature-flagged GPT/Codex, Gemini, and Claude guidance blocks

## Suggested Work Breakdown

### Workstream A: Orchestration semantics

Files likely touched:

- `cognis/tools/builtin/orchestration.py`
- `cognis/core/agent_loop.py`
- `cognis/api/models.py`

Tasks:

1. Add and normalize `auto` / `self`
2. Preserve explicit forced semantics
3. Return deterministic errors for invalid forced choices

### Workstream B: Routing helper

Files likely touched:

- `cognis/core/decision.py`
- new routing helper module under `cognis/core/`
- `cognis/core/turn_scheduler.py`
- `cognis/core/agent_loop.py`

Tasks:

1. Implement shared precedence pipeline
2. Add reason-code model
3. Reuse helper from delegation and task creation

### Workstream C: Candidate filtering

Files likely touched:

- `cognis/core/agent_registry.py`
- routing helper module

Tasks:

1. Build eligibility predicates
2. Add deterministic ranking and candidate cap
3. Generate compact candidate summaries

### Workstream D: Execution envelope

Files likely touched:

- `cognis/core/tool_exposure.py`
- `cognis/core/tool_router.py`
- `cognis/core/executor_resolution.py`
- `cognis/core/agent_loop.py`

Tasks:

1. Compute parent envelope
2. Filter infeasible candidates
3. Materialize child envelope
4. Enforce subset guarantees

### Workstream E: Classifier

Files likely touched:

- `cognis/core/decision.py`
- classifier prompt/schema module(s)

Tasks:

1. Add unified routing classifier schema
2. Validate classifier output
3. Apply safe fallback rules

### Workstream F: Telemetry and tests

Files likely touched:

- routing helper module
- `tests/unit/`
- `tests/integration/`

Tasks:

1. Add metrics and structured logs
2. Add routing behavior tests
3. Add capability-ceiling tests

### Workstream G: Model-family prompts

Files likely touched:

- `cognis/core/prompts.py`

Tasks:

1. Add feature-flagged family-specific prompt blocks
2. Verify separation from routing changes

## Acceptance Criteria

- `delegate` and `create_task` support `auto` and `self` deterministically
- explicit invalid forced selections fail closed
- hard-constrained workflows are not silently executed on substitute agents
- child execution envelopes are subsets of parent envelopes
- invalid classifier output always falls back safely
- routing telemetry explains decisions, exclusions, and fallbacks
- regression tests cover routing precedence and capability ceilings
