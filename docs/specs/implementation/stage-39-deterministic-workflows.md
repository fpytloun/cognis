# Stage 39: Deterministic Workflow Steps

## Status

PLANNED

## Goal

Implement the deterministic workflow-step architecture from
[`../34-deterministic-workflows.md`](../34-deterministic-workflows.md).

This stage makes workflow steps capable of mechanical controller-owned work
without invoking an LLM provider:

1. render safe typed workflow expressions;
2. execute one deterministic tool call through the existing tool router;
3. skip or branch with deterministic conditions;
4. complete a task silently or with existing delivery semantics.

The outcome should make workflows such as "check Slack, branch on whether there
is anything actionable, otherwise complete silently" possible without spending
an LLM turn on mechanical polling or no-op decisions.

## Non-Goals

- No arbitrary scripts, Python snippets, shell snippets, or broad expression
  language.
- No direct Slack/email/channel posting from deterministic steps.
- No migration of existing gate conditions to Jinja in this stage.
- No `notify` or `transform` step unless required by implementation findings.
- No semantic judgment in deterministic steps.

Because `notify` and `transform` are deferred, v1 workflows have only two
user-facing shapes after deterministic checks:

- route to a `run` step when interpretation, synthesis, response writing, or
  notification text is needed;
- end with deterministic `complete`, commonly `delivery_mode_override="silent"`,
  when there is no useful user-facing result.

## Architecture Review

Architect review result: **APPROVE WITH CHANGES**.

Required changes incorporated into this implementation slice:

- deterministic step restart/idempotency rules are explicit;
- side-effecting or unknown-metadata tools are default-denied for
  autonomous/scheduled deterministic tool calls unless explicitly allowed;
- `complete` v1 supports `completed` and `failed`, not `cancelled`;
- Jinja starts with a minimal sandboxed surface and no macros/includes/call
  blocks/custom unreviewed filters.

## Core Invariants

1. Existing `run` and `gate` workflows remain valid and serialize unchanged.
2. The controller, not the agent, advances deterministic steps.
3. Deterministic steps create inspectable `StepRun` records and durable
   `StepOutput`-compatible outputs.
4. Deterministic tool calls use the existing `ToolRouter`, executor pool,
   guardrails/audit, and runtime metadata path.
5. Deterministic steps do not create model prompts, call LLM providers, or inject
   memory context.
6. Side-effecting deterministic tool calls are never silently re-executed after
   restart.

## Workstreams

### A. Workflow rendering module

Add `cognis/core/workflow_rendering.py`.

Deliverables:

- Jinja2 dependency if not already present.
- Strict undefined handling.
- Sandboxed/native typed rendering adapter.
- Three render modes:
  - expression mode for `when` and `condition.if`;
  - native mode for args/outputs/metadata;
  - text mode for summaries/content.
- JSON-like context builder exposing only safe task/workflow/vars/steps data.
- Date helpers shared with existing datetime builtin tools.
- Size limits, truncation, and redaction helpers for audit/runtime info.

Tests:

- missing variables fail closed;
- whole-expression native type preservation works;
- embedded expressions stringify;
- date helpers match datetime tool behavior;
- unsafe constructs are rejected;
- credentials/session/executor/tool-router objects are not reachable.

### B. Model and validation updates

Update workflow models around `cognis/models/workflow.py`.

Deliverables:

- extend `StepDefinition.type` with `tool_call`, `condition`, and `complete`;
- add deterministic config models;
- add common `when`, `on_skip`, `on_error`, and optional `next` fields;
- validate named step targets;
- validate deterministic jump loop caps;
- preserve backward-compatible serialization for existing definitions;
- reject unsupported v1 statuses such as `complete.status="cancelled"`.

Tests:

- existing workflow fixtures still parse and serialize;
- deterministic workflow fixtures parse;
- invalid branch targets fail validation;
- invalid complete statuses fail validation;
- unknown deterministic fields fail clearly.

### C. Restart-safe deterministic step runner

Add deterministic-step execution paths to `cognis/core/workflow_engine.py`.

Deliverables:

- deterministic state machine:

  ```text
  pending -> rendering -> executing -> persisted -> completed|skipped|failed
  ```

- create `StepRun` before rendering;
- persist redacted render summaries before tool execution;
- persist output before advancing `WorkflowState`;
- resume logic that:
  - never re-executes terminal deterministic steps;
  - retries rendering safely;
  - retries read-only executing tool calls when safe;
  - pauses/fails side-effecting executing tool calls after restart unless a
    verified idempotency contract exists.

Tests:

- restart after rendered args but before execution;
- restart after completed deterministic step does not re-run it;
- side-effecting executing step is not silently retried;
- skipped step persists status and output.

### D. Tool execution helper extraction

Extract a reusable helper, for example `cognis/core/tool_execution.py`, from the
current agent-loop regular-tool execution path.

Deliverables:

- helper accepts the runtime/tool execution context needed by both agent loop and
  deterministic `tool_call`;
- preserves:
  - `target_executor` stripping and validation;
  - executor-pool lookup;
  - active executor refresh;
  - runtime metadata;
  - tool registry lookup;
  - guardrails/audit path through `ToolRouter`;
  - metrics/logging labels;
  - output chunk/artifact callbacks where applicable.
- `AgentLoop._execute_regular_tool` delegates to the helper.

Tests:

- existing agent-loop tool tests still pass;
- target executor behavior is unchanged;
- executor-routed fake tool still uses the selected executor;
- non-executor tool rejects `target_executor`.

### E. `tool_call` step

Deliverables:

- render args in native mode;
- validate rendered args are JSON-serializable and match the target tool schema;
- default-deny side-effecting or unknown-metadata tools in autonomous/scheduled
  workflows unless `allow_side_effects=true`;
- execute via extracted helper;
- normalize `ToolResult` into `StepOutput`;
- reference large outputs rather than copying them into downstream prompt
  context.

Tests:

- read-only builtin tool succeeds;
- tool error fails or continues according to config;
- missing metadata is blocked by default in autonomous/scheduled workflow;
- explicit `allow_side_effects=true` permits configured write-capable tool;
- large output records references/truncated summary.

### F. `when` and `condition`

Deliverables:

- evaluate `when` before all step types;
- record skipped `StepRun` and skipped output;
- implement `condition.if` branch selection;
- support named branch targets and default linear advance;
- enforce loop cap/global deterministic jump safety cap;
- persist branch decision in runtime info and output metadata.

Tests:

- `when=false` skips a `run` step without invoking LLM;
- `condition` true and false branches route correctly;
- missing branch target validation fails;
- branch loop cap prevents infinite deterministic loops;
- `on_error=continue` records failed StepRun/output and advances.

### G. `complete` step

Deliverables:

- render summary/content/outputs;
- write final task result summary/data;
- support `status=completed` and `status=failed`;
- support `delivery_mode_override="silent"` using existing delivery semantics;
- do not implement workflow-authored cancellation in v1.

Tests:

- silent completion does not inject a conversation event;
- non-silent completion uses existing task delivery path;
- failed completion marks task failed with deterministic result;
- invalid cancelled status is rejected.

### H. API, UI, tools, and skill guidance

Deliverables:

- update workflow CRUD/import/export API schemas;
- update generated/handwritten tool descriptions for:
  - `create_workflow`;
  - `update_workflow`;
  - `compose_and_run_workflow`;
- update Cognis Workflow Manager skill guidance so LLM authors know when to use:
  - deterministic `tool_call`;
  - `when`;
  - `condition`;
  - silent `complete`;
  - `run` for judgment/synthesis;
  - `gate` for human approval;
- update UI task/step-run display for deterministic statuses, branches, skipped
  steps, rendered-argument summaries, and output references.

Tests:

- API accepts deterministic workflows and rejects malformed ones;
- UI serialization/rendering handles new step types;
- tool schema examples produce valid workflow definitions.

## Suggested Implementation Order

1. Rendering module and tests.
2. Model/schema validation while keeping feature unused by system workflows.
3. Tool execution helper extraction with existing behavior preserved.
4. `tool_call` step using read-only builtin tool in tests.
5. `when` and `condition` branching.
6. `complete` silent no-op.
7. API/UI/tool-description/skill guidance updates.
8. Optional `notify`, `transform`, and gate-condition migration after v1 is
   stable.

## Acceptance Criteria

- All existing workflow tests pass unchanged.
- Deterministic steps never invoke an LLM provider or prompt projection.
- Deterministic `tool_call` uses the same tool routing/executor envelope as
  agent tool calls.
- Deterministic step transitions are persisted and restart-safe.
- Unknown or write-capable tools are blocked in autonomous/scheduled workflows
  unless explicitly allowed.
- Existing persisted workflow definitions remain valid.
- A workflow can fetch data, branch on empty results, and complete silently
  without any LLM step after the fetch.
- LLM workflow authors receive enough schema and skill guidance to construct
  valid deterministic workflows.
