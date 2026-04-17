# Cognis: Workflow Deliverables

## Purpose

Workflow steps produce **deliverables** — typed, versioned artifacts that are the one and only user-facing output of a workflow step. Deliverables are authored via the controller-intercepted `write_deliverable` tool, stored durably, optionally evaluated, and delivered to the caller's channel exactly once per workflow.

This document defines:

- the deliverable domain model and lifecycle
- the `write_deliverable` tool contract
- the `step_complete` completion gate interaction
- the evaluator / revise loop integration
- the channel delivery contract (once-only semantics)
- data ownership and storage
- the relationship to free-text assistant output

Related specs: [`14-workflow-engine.md`](14-workflow-engine.md), [`22-step-profiles.md`](22-step-profiles.md), [`06-tool-system.md`](06-tool-system.md), [`09-ui-ux.md`](09-ui-ux.md).

## Motivation

Before this spec, a workflow step's user-facing output was whatever free text the assistant wrote before calling `step_complete`. That shape was responsible for an entire class of observed regressions:

- The final brief was delivered to the channel before `step_complete` was even accepted.
- Rejected `step_complete` calls left the brief as the most salient assistant content, so the model repeated it.
- Evaluator `revise` loops produced a second assistant message that was also delivered, so the user received the same brief twice.
- "Expected output" in the task description and "call step_complete" in the system prompt contradicted each other, forcing the model to choose.
- The UI rendered every assistant message as if it were the deliverable, including reasoning, progress updates, and retry artifacts.

Deliverables solve the class of problems by **decoupling authoring from delivery**:

1. Authoring is a single tool call, so the deliverable is typed and machine-verifiable.
2. Delivery is the controller's job, so it can be gated on acceptance and evaluator approval.
3. Only one delivery happens per workflow, regardless of how many revise iterations ran.
4. Assistant free text during a workflow step becomes reasoning/progress, never user-facing.

## Design Principles

### 1. Authoring is a tool call, not free text

Inside a workflow step, the model authors the user-facing output by calling `write_deliverable(content, format, ...)`. Free-text assistant messages during a workflow step are reasoning; they are never delivered. This makes the output a typed object and makes duplicate delivery impossible by construction.

### 2. One delivery per workflow

A workflow may have many steps, and every step may write deliverables. Exactly one deliverable — the final delivering step's latest approved version — is delivered to the channel. All other deliverables remain buffered in the database for the UI and for evaluator input.

### 3. Delivery is gated on acceptance and approval

Delivery does not happen when the deliverable is written. It happens after:
- `step_complete` is accepted for the final delivering step,
- and the evaluator (when configured) approves the step.

### 4. Revise is idempotent with respect to delivery

Evaluator `revise` creates a new deliverable version. The buffered previous version becomes `superseded`. Delivery fires only once, on the final approved version.

### 5. Tool result does not re-echo content

The `write_deliverable` tool result returns a short preview and version id only. The full content is never re-echoed to the model, so long deliverables cannot dominate context after rejection.

### 6. Workflow-only

`write_deliverable` is visible only inside workflow-execution contexts (workflow steps, including delegated sub-sessions running inside a workflow). It is not exposed in direct chat, where the assistant message is the reply.

## Domain Model

### Deliverable

```python
class Deliverable(BaseModel):
    deliverable_id: str            # "dlv_<hex>"
    step_run_id: str               # FK → step_runs
    version: int                   # monotonically increasing per step_run
    content: str                   # the user-facing output
    format: Literal["markdown", "plain", "html"]
    title: str | None
    target: Literal["channel", "none"] | None
    outputs: dict[str, Any]        # optional structured sidecar data
    status: Literal[
        "buffered",                # written, not yet approved
        "approved",                # approved; not yet delivered (or not a delivering step)
        "delivered",               # final delivering step; channel send succeeded
        "superseded",              # replaced by a newer version of the same step_run
        "rejected",                # evaluator rejected this version
    ]
    evaluator_feedback: str | None # populated when status transitions to "rejected"
    created_at: datetime
    updated_at: datetime
```

Invariants:

- `(step_run_id, version)` is unique.
- Monotonic `version`: the first deliverable on a step_run is version 1; subsequent writes increment by 1.
- On write, the previous version for the same step_run transitions from `buffered`/`approved` to `superseded`.
- A step_run has exactly one version in `{buffered, approved, delivered}`; others are `superseded` or `rejected`.

### StepDefinition additions

```python
class StepDefinition(BaseModel):
    # existing fields: name, type, prompt, agent_override, reasoning_effort,
    # input, completion, allow_questions, gate, on_reject, outcome_routes
    require_deliverable: bool = True
    step_profile: Literal["unrestricted", "research", "coding"] = "unrestricted"
    tool_overrides: StepToolOverrides | None = None
```

Defaults:

- `require_deliverable=True` — every new step-authored workflow requires a deliverable unless the author opts out (typical case: operational steps like `commit`/`remember`).
- `step_profile="unrestricted"` — see [`22-step-profiles.md`](22-step-profiles.md).

### StepRun additions

```python
class StepRun:
    # existing fields: step_run_id, task_id, step_name, step_type, status,
    # attempt, agent_id, conversation_id, session_id, intaris_session_id,
    # output, evaluation, todos, started_at, completed_at, ...
    deliverable_id: str | None          # latest non-superseded, non-rejected deliverable
    profile_applied: str | None         # snapshot of step_profile that ran
    require_deliverable: bool | None    # snapshot of require_deliverable that ran
```

The snapshot columns are audit metadata. Legacy rows read as `NULL` and render as "n/a" in the UI.

## `write_deliverable` Tool

### Tool shape

```json
{
  "name": "write_deliverable",
  "description": "Write the user-facing deliverable for this workflow step. Call this before step_complete when the step requires a deliverable. The content you pass is what the user will see; it replaces any assistant free text you have written this turn.",
  "parameters": {
    "type": "object",
    "required": ["content"],
    "properties": {
      "content":  {"type": "string", "description": "The final deliverable content."},
      "format":   {"type": "string", "enum": ["markdown", "plain", "html"], "default": "markdown"},
      "title":    {"type": "string", "description": "Optional title for the deliverable."},
      "target":   {"type": "string", "enum": ["channel", "none"], "description": "Only meaningful for the final delivering step; workflow policy overrides this."},
      "outputs":  {"type": "object", "description": "Optional structured sidecar data for the evaluator or downstream steps."}
    }
  }
}
```

### Exposure

- Exposed only when `ctx.policy in (WORKFLOW_POLICY, SECONDARY_POLICY)`. Never in direct chat.
- Available in **every** workflow step, including steps with `require_deliverable=false`. The step's deliverable is always stored; whether it becomes the channel-delivered one depends on workflow position and policy, not on the model's choice.
- Visible to the model under every `step_profile` (including restrictive ones) because the `deliverable` category is always in every restrictive profile's allowlist. See [`22-step-profiles.md`](22-step-profiles.md).

### Semantics

On invocation:

1. Validate arguments (non-empty `content`, valid `format`).
2. Supersede any existing non-terminal deliverable version for this step_run (set `status="superseded"`).
3. Insert a new `deliverables` row with `version = max_version + 1`, `status="buffered"`.
4. Update `step_runs.deliverable_id` to point at the new row.
5. Return to the model:
   ```json
   {
     "status": "buffered",
     "deliverable_id": "dlv_...",
     "version": 2,
     "length": 4231,
     "format": "markdown",
     "preview": "<first 240 chars of content>…"
   }
   ```
6. Emit controller event `DELIVERABLE_WRITTEN` to the event bus.

The tool result is intentionally small: the full content is **not** echoed back to the model. This keeps history compact and stops long deliverables from dominating context after rejection.

### Error shapes

Teach-back rejections follow the project-wide rejection shape (`{status:"rejected", reason, message, received}`):

- `reason="empty_content"` — `content` missing or whitespace-only.
- `reason="invalid_format"` — unsupported `format` value.
- `reason="not_in_workflow"` — tool was invoked outside a workflow context (should be unreachable because the tool is not exposed).

## `step_complete` Gate

A workflow step's `step_complete` is accepted only when **all** of the following hold:

1. Every todo is in a terminal state — `completed` or `cancelled`.
2. If `require_deliverable=True`: a deliverable has been buffered (or approved) for this step_run.
3. If the step is configured with evaluation: the evaluator approves the buffered deliverable (enforced by the workflow engine between `step_complete` acceptance and advancement).

Rejection reason codes (stable for telemetry):

- `todos_pending` — one or more todos still pending or in_progress.
- `deliverable_missing` — `require_deliverable=True` and no deliverable row exists.
- `invalid_step_complete_arguments` — Pydantic validation failure on step_complete payload.
- `invalid_step_complete_notification` — notification policy violation.
- `deliverable_rejected_by_evaluator` — evaluator returned `revise`; workflow engine re-runs the step; this is reported in structured logs/metrics, not as a tool result to the model.

When `deliverable_missing` triggers, the prescriptive teach-back tells the model exactly what to do:

> *"This step requires a deliverable. Call write_deliverable with your final user-facing output, then call step_complete. Do not restate the deliverable as free text; write_deliverable is the one delivery channel."*

The controller also appends a follow-up system reminder after the rejection, consistent with the `todos_pending` path defined in Tier 1 harness work.

## Workflow Engine Integration

### Per-step handling

After `_execute_run_step` returns a `StepOutput`:

1. If the step has a buffered deliverable and evaluation is configured:
   - The evaluator reviews the deliverable (see [Evaluator Input](#evaluator-input) below).
   - On `approve` → deliverable transitions `buffered → approved`.
   - On `revise` → deliverable transitions `buffered → rejected`, feedback stored in `evaluator_feedback`, state `last_evaluation_feedback` set, step is re-run; next `write_deliverable` creates version N+1.
   - On `failed` → evaluator malfunction path (existing behavior).
2. If the step has no evaluation:
   - The buffered deliverable transitions `buffered → approved` automatically on step acceptance.
3. If the step has `require_deliverable=False` and the model did not write one:
   - Accepted without a deliverable; `step_runs.deliverable_id` stays `NULL`.

### Final delivery

At workflow termination, the engine locates the **final delivering step** and, if it has an approved deliverable, performs channel delivery exactly once:

- Final delivering step is the last `type="run"` step with `require_deliverable=True` that has an `approved` deliverable.
- If no such step exists (e.g., `system:software-development` where `commit`/`remember` are the last steps with `require_deliverable=False`), the engine walks backward to find the last `require_deliverable=True` step with an `approved` deliverable and delivers that.
- If still no candidate (a workflow composed entirely of non-delivering steps), the engine uses the legacy `_build_result_summary` path (no channel deliverable).

On successful channel send, the chosen deliverable transitions `approved → delivered`. On failure, existing fallback/outbox logic applies; the deliverable stays `approved` until the outbox resolves.

### Evaluator input

The evaluator receives the buffered deliverable's `content` as the primary subject, plus:

- the step's prompt and expected output
- any prior step context already provided to step execution
- optional `outputs` sidecar from the deliverable
- the claim list from `step_complete`

This matches the existing evaluator contract, only the "subject under evaluation" moves from `step_output.content` (free text) to `deliverable.content`.

### Revise loop

The revise loop is unchanged mechanically; the only difference is that each loop creates a new deliverable version. Previous versions stay in the DB as `superseded` for the UI and for evaluator-feedback diffs.

`CompletionConfig.max_attempts` caps total revise iterations per step. On exhaustion, the `on_exhausted` action runs (`continue`, `fail`, `gate`). If `continue` on an exhausted-with-rejected-deliverable step, the latest rejected version is treated as approved for delivery purposes with a `needs_review` flag surfaced in the UI; this is a soft failure mode preferable to a stuck workflow.

## Channel Delivery

The existing `_deliver_task_result*` path is refactored to route through deliverables:

- `_deliver_task_result_direct` no longer reads `task.result_data.final_content`; it reads the chosen deliverable's `content` (when a deliverable exists).
- `_deliver_task_result_default` unchanged for non-delivering workflows.
- Once-only semantics: the deliverable status transition `approved → delivered` is the acknowledgement. A second delivery attempt on the same `deliverable_id` is a no-op with a warning log.
- Channel outbox (`channel_delivery_outbox`) keeps its existing retry semantics; a successful outbox send triggers the `approved → delivered` transition.

## Data Ownership

| Data | Owner | Storage |
|---|---|---|
| Deliverable content, version, status | **Cognis** | `deliverables` table in Cognis DB |
| Deliverable authored event | **Intaris** | Session event store (as a `tool_call`/`tool_result` pair for `write_deliverable`) |
| Evaluator decision on deliverable | **Cognis** | `step_runs.evaluation` column and `deliverables.evaluator_feedback` |
| Channel send attempts | **Cognis** | `channel_delivery_outbox` (existing) |

Deliverables are Cognis-owned because they are the controller's typed artifact. Intaris still sees the `write_deliverable` tool call in session events so session replay remains faithful.

## Assistant Free Text in Workflow Steps

Free-text assistant messages during a workflow step are reasoning/progress, never the user-facing deliverable. UI renders them as a demoted subpanel (`"Reasoning"`) distinct from the deliverable panel. Channel adapters never send them.

Exception: the existing direct-chat path (`system:direct`) is unchanged — the assistant message *is* the reply there. `write_deliverable` is not exposed in direct chat.

## Telemetry

Prometheus counters (labelled by `workflow_name` and `step_name` where useful):

- `cognis_deliverables_written_total{workflow, step}`
- `cognis_deliverables_approved_total{workflow, step}`
- `cognis_deliverables_rejected_total{workflow, step}`
- `cognis_deliverables_delivered_total{workflow, step}`
- `cognis_deliverables_superseded_total{workflow, step}`
- `cognis_step_complete_rejections_total{reason="deliverable_missing"}`

Histograms:

- `cognis_deliverable_size_bytes{workflow, step}` (on write)
- `cognis_deliverable_versions_per_step_run{workflow, step}` (on step finalization)

Content, snippets, and evaluator feedback text are **never** logged. Logs contain ids, counts, status codes, and reason codes only.

## UI Surface

See [`09-ui-ux.md`](09-ui-ux.md) for the general UX spec. Additions for deliverables:

- **Step-run view** includes a `DeliverablePanel`:
  - Primary: latest approved or delivered version, rendered per `format`.
  - Version list with status badges.
  - Per-version evaluator-feedback diff between consecutive versions.
  - Copy, download, "open in channel" actions.
- **Workflow editor** exposes `require_deliverable` toggle per step; defaults to true.
- **Channel rendering** for workflow-backed tasks shows the delivered deliverable as a single message; assistant free text is hidden from the channel and visible only in the step-run "Reasoning" subpanel.

## Migration and Backward Compatibility

- New tables/columns are additive; legacy rows read with `NULL` deliverable_id and `profile_applied=NULL`.
- Existing workflows without `step_profile`/`require_deliverable` deserialize with `unrestricted`/`True`; user-authored workflows that never called `write_deliverable` will hit `deliverable_missing` rejections on first run. This is the intended behavior — it surfaces the contract change. Existing **system** workflows are updated in the deferred [Stage 23 implementation](implementation/stage-23-workflow-deliverables.md) commit 7, so no manual migration is required for shipped workflows.
- For workflows that should keep legacy behavior during migration, authors can set `require_deliverable=False` on every step; this reverts the behavior to pre-Tier-3 (free text + step_complete).

## Non-Goals

- Streaming deliverables. A deliverable is authored in a single tool call; partial writes are not supported.
- Editing a delivered deliverable. Once a deliverable is `delivered`, it is immutable. A new workflow run produces a new deliverable.
- Binary deliverables. `format` is `markdown | plain | html`. Attach binary artifacts through existing attachment mechanisms.

## Open Questions (for follow-up specs)

- A future `read_deliverable` tool would let downstream steps read the full content of prior step deliverables (not just the truncated preview). The current model uses `StepInputConfig` for cross-step input; adding a direct reader is an ergonomic improvement rather than a structural need.
- Deliverables as inputs to future A2A/federation flows (see [`08-federation.md`](08-federation.md)).
