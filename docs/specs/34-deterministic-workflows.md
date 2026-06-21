# Cognis: Deterministic Workflow Steps

## Purpose

Deterministic workflow steps let the workflow engine perform mechanical work
without calling an LLM provider. They run inside the same task, session,
executor, tool, audit, and delivery envelope as normal workflow steps, but the
controller computes their result directly.

The goal is to make common workflow shapes cheaper, faster, and more reliable:

```text
fetch data deterministically
  → branch/skip deterministically
  → call an agent only when judgment is needed
  → complete silently or notify deterministically
```

This spec extends [`14-workflow-engine.md`](14-workflow-engine.md) and
[`27-workflow-composer.md`](27-workflow-composer.md).

## Non-Goals

- No arbitrary scripts, Python execution, shell snippets, or unbounded workflow
  expression language.
- No direct channel posting from deterministic notification or completion steps.
  Delivery still goes through task/conversation delivery.
- No secret material rendered into templates. Credentials remain tool/runtime
  concerns and are never exposed in workflow rendering context.
- No semantic judgment in deterministic steps. Ambiguous triage, synthesis,
  writing, and decision-making remain `run` steps.
- No compatibility break for existing `run` and `gate` workflows.

## Core Invariants

1. **Controller-owned advancement.** Deterministic steps are completed by the
   `WorkflowEngine`, not by an agent. They do not call `step_complete`, but the
   controller translates their deterministic result into the same `StepRun`,
   `StepOutput`, and `WorkflowState.step_outputs` contracts before advancing.

2. **Same execution envelope, no model turn.** Deterministic steps reuse task,
   workflow, agent, executor, tool registry, tool policy, guardrails/audit, and
   delivery context. They skip model routing, projection, memory prompt
   injection, and semantic step evaluation unless explicitly introduced later.

3. **Durable observability.** Every deterministic step creates or updates a
   `StepRun`. The runtime info records the rendered expression/arguments
   summary, selected branch, skip reason, output references, and errors with
   size limits and redaction.

4. **Step output compatibility.** Every deterministic step emits a
   `StepOutput`-like record into `WorkflowState.step_outputs` unless explicitly
   marked internal-only. The minimum output is:

   ```json
   {
     "summary": "...",
     "content": "",
     "outputs": {},
     "metadata": {
       "deterministic_step": true,
       "step_type": "tool_call|condition|complete"
     },
     "claims": []
   }
   ```

5. **Existing `run` and `gate` semantics remain unchanged.** Existing workflow
   definitions serialize and execute unchanged. Existing gate conditions keep
   their current evaluator initially; Jinja-based deterministic expressions are
   introduced for new fields.

6. **No silent re-execution of side effects.** Restart recovery must detect
   partially executed deterministic steps and must not silently re-run
   side-effecting tool calls after the controller restarts.

## Step Types

### Existing types

- `run` — full agent step with `step_complete` and optional semantic
  evaluation.
- `gate` — human/caller pause step.

### New deterministic v1 types

- `tool_call` — render arguments and execute exactly one Cognis tool through the
  existing tool router.
- `condition` — evaluate a deterministic expression and route to a named step or
  continue.
- `complete` — finish the workflow/task without an LLM, including silent no-op
  completion.

### Deferred types

- `notify` — deterministic task/conversation notification through existing
  delivery machinery. Deferred until `complete` is proven.
- `transform` — compute structured outputs from expressions. Deferred until
  real workflows need it.

Deferring `notify` and `transform` is an intentional v1 boundary. A v1 workflow
that needs user-facing interpretation, synthesis, or notification should route
to a `run` step. A workflow that has no useful user-facing result should end via
`complete`, usually with `delivery_mode_override="silent"`.

## Common Step Fields

All step types may eventually support these controller-owned fields:

```python
class StepDefinition:
    name: str
    type: Literal["run", "gate", "tool_call", "condition", "complete"]

    # Optional deterministic precondition.
    when: str | None = None

    # Output recorded when `when` evaluates false.
    on_skip: DeterministicOutputConfig | None = None

    # Deterministic error behavior. Default is fail.
    on_error: Literal["fail", "continue", "skip", "gate"] | None = None

    # Optional explicit next step for deterministic jumps.
    next: str | None = None
```

`when` is evaluated by the workflow rendering engine before the step executes.
If it returns false:

1. create/update the `StepRun`;
2. record status `skipped`;
3. persist `on_skip` output if supplied, otherwise a default skipped output;
4. advance to the next step.

`on_error="continue"` still records a failed deterministic `StepRun` and a
`StepOutput` with `error` populated before advancing. It must never erase or
hide the failed execution.

## Deterministic Output Config

```python
class DeterministicOutputConfig:
    summary: str
    content: str | None = None
    outputs: dict[str, Any] = {}
    metadata: dict[str, Any] = {}
```

All fields are rendered through the workflow rendering engine. `summary` and
`content` use text rendering; `outputs` and `metadata` use native typed
rendering.

## `tool_call` Step

```python
class ToolCallStepConfig:
    tool: str
    args: dict[str, Any] = {}
    summary: str | None = None
    outputs: dict[str, Any] = {}
    fail_on_error: bool = True
    timeout_seconds: int | None = None
    allow_side_effects: bool = False
    redact_args: list[str] = []
```

Semantics:

1. Build the same runtime/executor context used by `run` steps.
2. Render `args` using native typed rendering.
3. Validate the rendered arguments are JSON-serializable and satisfy the target
   tool schema before dispatch.
4. Execute exactly one tool through the existing `ToolRouter`.
5. Convert `ToolResult` into a `StepOutput`.
6. Persist output references rather than copying huge raw outputs into future
   prompts.

`tool_call` must not fake an agent turn. The implementation should extract a
small reusable tool-execution helper from the current agent-loop tool dispatch
path so deterministic steps preserve:

- `target_executor` stripping and validation;
- executor-pool lookup and active-executor refresh;
- runtime metadata;
- tool registry and tool policy;
- guardrails/audit integration;
- metrics and structured logs;
- output chunk/artifact persistence where applicable.

### Side-effect policy

Autonomous and scheduled workflows must default to safe behavior:

- tools with `read_only=true` may run without extra approval;
- tools with `read_only=false`, missing metadata, or ambiguous metadata are
  blocked unless `allow_side_effects=true`;
- `allow_side_effects=true` must be explicit in the workflow definition and
  should be rejected by validation unless the workflow author, schedule, or
  previous gate makes the risk intentional;
- destructive tools should require a gate or a future stronger approval model.

The first implementation should prefer default-deny over best-effort warnings.

## `condition` Step

```python
class ConditionStepConfig:
    if_: str
    then: str | None = None
    else_: str | None = None
    output: DeterministicOutputConfig | None = None
```

Semantics:

1. Evaluate `if_` in expression mode.
2. If true, jump to `then` when supplied; otherwise advance to the next step.
3. If false, jump to `else_` when supplied; otherwise advance to the next step.
4. Record the expression result and selected branch in `StepRun.runtime_info` and
   `StepOutput.metadata`.

Workflow validation must reject unknown step names and must apply loop
protection to deterministic jumps. A global deterministic jump cap is acceptable
for v1; per-edge loop budgets can follow the existing review-loop pattern later.

## `complete` Step

```python
class CompleteStepConfig:
    status: Literal["completed", "failed"] = "completed"
    summary: str
    content: str | None = None
    outputs: dict[str, Any] = {}
    notification: StepCompletionNotification | None = None
    delivery_mode_override: Literal[
        "same_conversation",
        "preferred_channel",
        "latest_active_for_agent",
        "specific_conversation",
        "silent",
    ] | None = None
```

`complete` ends the workflow without an LLM step.

Important status rule:

- `status="completed"` is the normal success/no-op path.
- `status="failed"` marks a deterministic business failure with a rendered
  summary and result data.
- `cancelled` is intentionally not part of v1. User/system cancellation remains
  an operational action, not a workflow-authored deterministic outcome.

`delivery_mode_override="silent"` enables workflows such as alert polling to
finish without injecting a conversation event when no actionable work exists.
All non-silent delivery still uses the existing task delivery path.

Until deterministic `notify` exists, v1 workflows should not attempt rich
deterministic user-facing messages. They should either:

- route to a `run` step for judgment, synthesis, and response composition; or
- terminate with `complete`, often silently, when no user-facing response is
  needed.

## Workflow Rendering

Deterministic workflow fields use Jinja2 as a constrained workflow rendering
DSL. Cognis should not invent a custom language, but the Jinja surface must be
small and controller-owned.

### Environment

The renderer should live in `cognis/core/workflow_rendering.py`.

Required constraints:

- `StrictUndefined`;
- sandboxed environment;
- native typed rendering for values where feasible;
- no filesystem loaders;
- no imports;
- no raw Python objects in context;
- no macros, includes, call blocks, custom unreviewed filters, or environment
  mutation in v1;
- explicit maximum input, rendered output, metadata, and audit-record sizes.

If Jinja's sandbox and native rendering cannot be combined directly, Cognis
should wrap expression compilation and post-conversion in its own safe adapter
rather than relaxing the sandbox.

### Render modes

1. **Expression mode** — for `when` and `condition.if`. The result must be a
   boolean, or a documented strict boolean coercion must be applied. Invalid
   types fail closed.
2. **Native mode** — for `tool_call.args`, deterministic `outputs`, and
   `metadata`. Dicts/lists are rendered recursively. A string that consists of a
   single expression preserves the native evaluated type.
3. **Text mode** — for summaries, content, and messages.

### Context

Only JSON-like data and vetted helper functions are exposed:

```text
task.*
workflow.*
vars.*
thresholds.*
steps.<name>.summary
steps.<name>.content
steps.<name>.outputs.*
steps.<name>.metadata.*
steps.<name>.status
steps.<name>.error
steps.<name>.deliverable_id
steps.<name>.attachments
result.*        # current tool result, only while rendering tool summaries/outputs
```

The context must not expose:

- credentials or secret payloads;
- raw Intaris sessions;
- executor connection objects;
- database sessions;
- tool router or registry objects;
- Python objects with methods/properties outside the safe wrapper.

### Date helpers

Reuse the existing datetime tool logic for:

- `now(timezone="UTC")`;
- `date_add(datetime, ...)`;
- `date_sub(datetime, ...)`;
- `convert_timezone(datetime, from_timezone, to_timezone)`;
- `format_datetime(datetime, format="iso", timezone=None)`.

Use UTC by default, validate IANA timezone names, and keep calendar-aware month
and year arithmetic via `dateutil.relativedelta`.

### Failure behavior

Template/rendering errors fail closed by default. `on_error="gate"` may pause
the task with a bounded diagnostic, but the diagnostic must not contain secrets
or full unredacted template context.

## Restart and Idempotency

Deterministic step execution must be restart-safe.

State machine:

```text
pending
  → rendering
  → executing
  → persisted
  → completed | skipped | failed
```

Persistence order:

1. Create `StepRun` before rendering.
2. Persist redacted render inputs and rendered argument summary before tool
   execution.
3. For `tool_call`, persist a deterministic idempotency key derived from
   `task_id`, `step_name`, `attempt_number`, and `step_run_id`.
4. Execute the tool.
5. Persist raw output references and normalized `StepOutput`.
6. Mark the `StepRun` terminal only after output persistence succeeds.
7. Advance workflow state only after the terminal `StepRun` and
   `WorkflowState.step_outputs` are durable.

On resume:

- a terminal deterministic `StepRun` is never re-executed;
- a `rendering` step can be retried because no side effect happened;
- an `executing` read-only `tool_call` may retry if the tool is marked
  read-only;
- an `executing` side-effecting `tool_call` must pause/fail for operator review
  unless the tool exposes a verified idempotency contract and the same
  idempotency key can be safely reused.

## Authoring Guidance

Agents and workflow composers should prefer this pattern:

```yaml
- name: fetch_recent_messages
  type: tool_call
  tool_call:
    tool: mcp:slack-lumilens:conversations_history
    args:
      channel: "{{ vars.alerts_channel_id }}"
      limit: 30
      oldest: "{{ date_sub(now('UTC'), minutes=30) }}"
    summary: "Fetched recent Slack messages."

- name: has_actionable_messages
  type: condition
  condition:
    if: "{{ steps.fetch_recent_messages.outputs.messages | length > 0 }}"
    then: respond
    else: complete_silent

- name: respond
  type: run
  prompt: |
    Inspect the fetched messages and respond only to explicit unresolved
    allowlisted requests.

- name: complete_silent
  type: complete
  complete:
    status: completed
    summary: "No actionable Slack request found."
    delivery_mode_override: silent
```

Use deterministic steps for mechanical fetch/check/branch/no-op logic. Use
`run` for judgment, synthesis, natural-language response writing, and ambiguous
intent interpretation. Use `gate` for human approval, not mechanical branching.

## API, Tool, UI, and Skill Updates

Implementation must update:

- workflow Pydantic models and server-side validation;
- workflow CRUD/import/export schemas;
- `create_workflow`, `update_workflow`, and `compose_and_run_workflow` tool
  descriptions/examples so LLM agents can construct valid deterministic steps;
- the Cognis Workflow Manager skill guidance with the authoring rules above;
- task/step-run UI rendering for deterministic statuses, branches, skip reasons,
  rendered-argument summaries, and output references.

## Testing Requirements

Minimum test coverage:

- rendering: strict undefined handling, native type preservation, date helpers,
  unsafe construct rejection, size limits, truncation, redaction;
- model validation: valid/invalid deterministic configs, unknown branch targets,
  loop caps, backward-compatible existing workflows;
- workflow engine: `when` skip, true/false condition branches, deterministic
  output persistence, template error behavior, `on_error` handling;
- tool calls: read-only builtin tool, executor-routed fake tool,
  `target_executor` validation, missing/write-capable metadata rejection,
  explicit `allow_side_effects=true`, tool error behavior, large output refs;
- complete: silent completion does not inject a conversation event; non-silent
  completion uses existing delivery path;
- security: template context cannot access credentials, raw sessions, executor
  objects, DB sessions, or raw tool router state.

## Acceptance Criteria

- Existing workflows run unchanged.
- Deterministic steps never invoke the LLM provider or prompt projection.
- `tool_call` steps use the existing tool routing and executor envelope.
- Every transition is persisted, inspectable, and restart-safe.
- Side-effecting deterministic tool calls are not silently re-executed after
  restart.
- Unknown or write-capable tools are blocked in autonomous/scheduled workflows
  unless explicitly allowed.
- LLM workflow composers can create valid deterministic workflows from schema
  examples and skill guidance.
- Silent no-op workflow completion is possible without an LLM step after data
  fetch.
