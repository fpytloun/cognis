# Auto Routing Implementation Plan

## Purpose

This document defines the implementation plan for runtime-supported auto
selection of agents and workflows in Cognis.

It turns the current prompt guidance around delegation, system specialists,
and `wait` behavior into deterministic controller behavior shared by:

- direct delegation via `delegate`
- task creation via `create_task`
- workflow agent selection when the workflow or caller does not force an agent

It depends on:

- `02-agent-model.md`
- `06-tool-system.md`
- `10-api-spec.md`
- `14-workflow-engine.md`
- `17-agent-runtimes.md`
- `18-runtime-contract.md`

## Scope

### In scope

- `agent_id="auto"` and `agent_id="self"` semantics for `delegate` and
  `create_task`
- shared routing helper for delegation and task creation
- deterministic routing pipeline with classifier fallback
- workflow preferred-agent and hard-required-agent precedence handling
- eligible candidate filtering for system and user agents
- execution-envelope intersection so child execution cannot widen capability
- runtime-led `wait` handling
- classifier prompt/input schema for mode + workflow + agent selection
- routing telemetry and regression tests

### Out of scope for the first implementation wave

- changes to user-facing agent creation UX beyond exposing new `auto` / `self`
  semantics where already relevant
- broader tool-surface redesign beyond current `bash`-based filesystem policy
- cost dashboards or operator routing controls beyond basic feature flags and
  metrics
- replacing workflow selection with a completely separate routing subsystem

## Goals

1. Preserve explicit user and tool intent.
2. Make auto-routing deterministic and testable.
3. Prevent delegated or task child execution from widening capabilities.
4. Allow system specialists and eligible user agents to participate in routing.
5. Keep direct conversations responsive by making `wait` runtime-led.
6. Keep classifier behavior advisory and bounded.

## Architectural Decisions

1. Routing policy is controller-owned. Model output is advisory only.
2. Omitted `agent_id`, empty `agent_id`, and `agent_id="auto"` are the same
   internal value.
3. `agent_id="self"` means force the current caller agent and fail closed if
   that choice is invalid in context.
4. Workflow hard constraints and explicit user choices must not be silently
   substituted.
5. Eligible candidates are filtered mechanically before the classifier sees
   them.
6. Child execution uses an execution envelope that must be a subset of the
   parent envelope across all relevant dimensions.
7. `wait` defaults to `false` and becomes `true` only when current-turn
   continuation depends on the delegated result.

## Routing Contract

### API semantics

For both `delegate` and `create_task`:

- `agent_id` omitted / empty / `"auto"` => system-selected agent
- `agent_id="self"` => current caller agent
- explicit agent ID => exact target agent

The API layer must normalize omitted, empty, and `"auto"` to one internal
representation before routing logic runs.

### Deterministic routing pipeline

Shared precedence order:

1. explicit user/tool choice
2. workflow hard constraint or preferred agent
3. candidate eligibility filter
4. deterministic heuristics
5. classifier suggestion
6. runtime normalization
7. execution-envelope materialization

This pipeline is used by:

- `delegate`
- `create_task`
- any future controller-owned auto-agent selection path

### Selection classes

- `inline` — no child execution
- `delegate` with `self`
- `delegate` with a specialist or eligible user agent
- `task` with selected workflow and execution agent

## Workflow precedence rules

Workflow agent constraints must be split into two classes.

### Hard-required agent

If a workflow requires a specific agent and that agent is ineligible after
filtering or execution-envelope materialization:

- fail closed, or
- reselect a different workflow when workflow selection is still automatic

Do not silently substitute a different agent under the same hard-constrained
workflow.

### Preferred agent

If a workflow prefers an agent and that agent is ineligible:

- fall back to the next valid routing stage
- record the reason the preferred agent was not used

### Explicit user choice precedence

Explicit user or tool choice wins over workflow preference.

Explicit invalid forced choice must return a deterministic error instead of
silently rerouting.

## Candidate filtering

Only eligible candidates are exposed to the classifier.

### Candidate set

- `self`
- system agents
- eligible user agents

### Minimum eligibility checks

- visible to the current user or tenant
- active and not archived
- bound or otherwise allowed in the current context
- compatible with required workflow/runtime constraints
- executor available and compatible
- not policy-disabled in the current conversation or session context
- feasible under precomputed execution-envelope subset checks

### Candidate ranking and cap

The candidate pool sent to the classifier must be bounded.

Rules:

1. Always include `self` when valid.
2. Always include obvious matching system specialists when valid.
3. Rank eligible user agents by deterministic signals such as:
   - exact workflow preference match
   - explicit secondary binding relevance
   - tool/policy compatibility closeness
   - purpose similarity score
   - recent successful use in the same conversation or workflow family
4. Cap the classifier candidate pool to a small fixed maximum.

The cap and ranking reasons must be logged via structured reason codes.

## Candidate metadata contract

The classifier must receive compact summaries, not full prompts.

Each candidate includes:

- `agent_id`
- `name`
- `description`
- `agent_type`
- `is_system`
- compact purpose summary
- compact skills summary
- compact tool/policy summary
- continuity-preserving flag

The summary must be regenerated or invalidated when the underlying agent
definition changes.

## Unified classifier

One classifier may choose mode, workflow, agent, and advisory `wait`, with the
controller retaining authority.

### Suggested response schema

```json
{
  "mode": "inline | delegate | task",
  "agent_selection": "auto | self | explicit",
  "selected_agent_id": "system:explore | system:research | system:code-review | system:architect | system:implement | <user-agent-id> | null",
  "selected_workflow_id": "<workflow-id> | null",
  "wait": false,
  "confidence": 0.82,
  "reason": "...",
  "alternatives_considered": ["self", "system:implement"]
}
```

### Failure handling

The controller must reject classifier output that is:

- invalid JSON
- low confidence
- timed out
- missing required fields
- references unknown agents or workflows
- references ineligible agents or workflows

Fallbacks:

- auto/advisory routing => deterministic safe fallback
- explicit forced selections => deterministic error
- hard-constrained workflow incompatibility => fail closed or reselect workflow

## Heuristics

Heuristics must remain narrow and high-confidence.

Initial intent classes:

- code review / audit / diff review => `system:code-review`
- external research / comparison / current information => `system:research`
- codebase exploration / trace / find-where => `system:explore`
- architecture critique / design review => `system:architect`

Heuristics must define:

- exact reason codes
- confidence threshold to skip classifier
- tie-break rules
- multilingual / ambiguous fallback behavior

Everything outside narrow confidence should fall through to classifier or
deterministic default behavior.

## Wait behavior

`wait` is runtime-led.

Rules:

- default `wait=false`
- use `wait=true` only when current-turn continuation depends on the delegated
  result

Examples of runtime triggers for `wait=true`:

- parent needs child output before replying usefully
- parent is joining multiple child outputs in the same turn
- current-turn control flow depends on the delegated result

The classifier may suggest `wait`, but the runtime owns the final value.

## Execution envelope

Capability narrowing is enforced through an execution envelope.

### Envelope dimensions

- tools
- read/write/shell/web capability
- file roots and filesystem scope
- secrets scope
- executor binding and location
- MCP server access
- channel/account reachability
- network and egress class
- runtime/context restrictions

### Two-phase handling

#### Phase 1: feasibility filtering

Precompute the parent envelope and discard candidates that can never produce a
valid child subset.

#### Phase 2: child envelope materialization

After final selection, materialize the exact child envelope and reject the
selection if the intersection is empty or violates a hard constraint.

### Empty intersection behavior

- explicit forced selection => deterministic error
- auto-routing => continue to the next valid candidate or deterministic fallback
- hard-constrained workflow => fail closed or reselect workflow

## Telemetry and observability

The first implementation wave must ship with routing telemetry.

### Required reason codes

- explicit forced selection accepted / rejected
- workflow preferred agent accepted / rejected
- workflow hard constraint accepted / rejected
- heuristic match accepted / rejected
- classifier accepted / rejected
- fallback class used
- candidate exclusion reasons
- envelope rejection reasons

### Required metrics

- routing decisions by source (`explicit`, `workflow`, `heuristic`,
  `classifier`, `fallback`)
- candidate count before and after filtering
- classifier latency and timeout rate
- classifier/runtime disagreement rate
- fallback counts by class
- invalid forced selection counts
- preferred-agent rejection counts
- envelope rejection counts by dimension

### Logging constraints

- do not log raw prompts or full candidate prompt text
- do not log user message content
- structured logs may include IDs, reason codes, confidence, latency, and
  bounded summaries

## Tests

### Unit tests

- API normalization of omitted / empty / `auto`
- `self` forced selection success and fail-closed behavior
- workflow preferred vs hard-required fallback semantics
- deterministic candidate filtering and ranking
- execution-envelope subset checks by dimension
- `wait` derivation from runtime dependency conditions
- classifier rejection fallback paths

### Integration tests

- direct delegation auto-selects a specialist when appropriate
- direct delegation preserves `self` when continuity matters
- task creation respects workflow preferred agent when eligible
- task creation fails closed or reselects workflow correctly on hard constraint
- user agent candidates are filtered by visibility/binding/executor readiness
- child execution cannot widen secrets, tools, runtime placement, or MCP access

### Telemetry tests

- reason codes emitted for each routing source
- fallback metrics increment correctly
- disagreement metrics record classifier overrides and rejections

## Workstreams

### Workstream 1: API and tool semantics

Deliverables:

- `delegate` and `create_task` accept `auto` / `self`
- API normalization helpers
- updated API and tool definitions

Likely files:

- `cognis/tools/builtin/orchestration.py`
- `cognis/core/agent_loop.py`
- `cognis/api/models.py`
- `docs/specs/10-api-spec.md`

Acceptance criteria:

- omitted, empty, and `auto` behave identically
- explicit invalid forced selection returns deterministic error

### Workstream 2: shared routing helper

Deliverables:

- central routing helper used by delegation and task creation
- deterministic precedence implementation
- reason-code model

Likely files:

- `cognis/core/decision.py`
- new shared routing module under `cognis/core/`
- `cognis/core/agent_loop.py`
- `cognis/core/turn_scheduler.py`

Acceptance criteria:

- routing is reproducible from the same inputs when classifier is skipped or
  rejected
- delegation and task creation share the same precedence logic

### Workstream 3: candidate filtering and ranking

Deliverables:

- eligibility filter for system and user agents
- deterministic ranking and candidate cap
- compact candidate-summary generation

Likely files:

- `cognis/core/agent_registry.py`
- `cognis/core/decision.py`
- new routing helper module

Acceptance criteria:

- ineligible agents never reach classifier input
- candidate pool is bounded and explainable via reason codes

### Workstream 4: execution envelope enforcement

Deliverables:

- parent envelope computation
- candidate feasibility filter
- child envelope materialization
- empty-intersection handling by fallback class

Likely files:

- `cognis/core/tool_exposure.py`
- `cognis/core/tool_router.py`
- `cognis/core/executor_resolution.py`
- `cognis/core/agent_loop.py`
- agent/executor permission models as needed

Acceptance criteria:

- child envelope is a strict subset of parent envelope across all supported
  dimensions
- no delegated child can widen secrets, network, MCP, or executor reachability

### Workstream 5: classifier integration

Deliverables:

- unified routing classifier prompt/schema
- controller-side validation and fallback logic
- timeout and confidence thresholds

Likely files:

- `cognis/core/decision.py`
- classifier prompt definitions in system-agent registry or dedicated module

Acceptance criteria:

- invalid classifier output never fails open
- classifier guidance is observable and bounded

### Workstream 6: telemetry and tests

Deliverables:

- metrics and structured logs
- unit and integration coverage for routing, fallback, and envelope rules

Likely files:

- `cognis/core/decision.py`
- `tests/unit/`
- `tests/integration/`

Acceptance criteria:

- production telemetry can explain why a route was chosen and why candidates
  were excluded
- routing regressions are caught by automated tests

### Workstream 7: model-family prompt blocks

Deliverables:

- GPT/Codex runtime guidance block
- Gemini runtime guidance block
- Claude/Anthropic runtime guidance block
- feature flag for rollout

Likely files:

- `cognis/core/prompts.py`
- provider/model resolution paths as needed

Acceptance criteria:

- blocks can be enabled per model family without affecting unrelated models
- prompt-behavior changes are separable from routing changes during rollout

## Proposed phase order

### Phase 1

- API normalization for `auto` / `self`
- shared routing helper skeleton
- telemetry schema and reason codes

### Phase 2

- candidate filtering and ranking
- deterministic workflow-preference and explicit-selection handling

### Phase 3

- execution-envelope enforcement
- fail-closed handling for explicit and hard-constrained cases

### Phase 4

- classifier integration with timeout/confidence thresholds
- routing behavior tests

### Phase 5

- model-family prompt blocks behind feature flags
- rollout tuning from telemetry

## Rollout guardrails

- ship telemetry before broad auto-routing enablement
- feature-flag classifier-backed auto-routing
- stage rollout for direct delegation before task/workflow auto-selection if
  needed
- keep deterministic fallback paths available at all times

## Acceptance criteria

- no explicit invalid forced agent selection is silently rerouted
- no hard-constrained workflow executes on a substituted incompatible agent
- no child execution envelope exceeds the parent envelope
- routing decisions are explainable from telemetry without content leakage
- classifier rejection, timeout, and disagreement cases are safe and visible
- auto-routing improves specialist selection without regressing direct chat
  responsiveness
