# Cognis: Workflow Task Cockpit

## Status

APPROVED FOR IMPLEMENTATION

## Purpose

This spec defines the target workflow/task experience for Cognis while
preserving the workflow runtime that already works.

The redesign has two deliberately different scopes:

1. **Conservative engine evolution** — retain the current task, workflow,
   `WorkflowState`, `StepRun`, evaluator, gate, schedule, delivery, retry, and
   pause/resume contracts; add deterministic steps and immutable effective
   workflow snapshots.
2. **Big-bang UI/UX replacement** — replace the current task-detail and workflow
   authoring presentation with a phase-oriented Task Cockpit and workflow
   builder designed from scratch.

The implementation stage is
[`implementation/stage-39-workflow-task-cockpit.md`](implementation/stage-39-workflow-task-cockpit.md).

## Scope Boundaries

This redesign owns:

- workflow definition and execution compatibility;
- deterministic `tool_call`, `condition`, and `complete` steps from
  [`34-deterministic-workflows.md`](34-deterministic-workflows.md);
- UI-facing phases that group linked execution steps;
- workflow-definition pinning for stable execution and projection;
- task-detail workflow projection;
- the Task Cockpit runtime UI;
- the workflow authoring UI.

It does **not** own:

- the global Cognis Control Center/dashboard;
- Chat v2 Work Info and session execution-footprint views;
- replacement of the existing task Kanban or project model;
- a generic graph/DAG editor;
- a new workflow-runs table;
- arbitrary scripts or a general-purpose expression language;
- a rewrite of the existing `run`, evaluator, gate, schedule, delivery, or
  task-queue semantics.

## Current Baseline

The implementation must start from the current architecture rather than
inventing a parallel runtime:

- `Workflow.steps` is the canonical ordered execution list.
- Existing step types are `run` and `gate`.
- `Task.workflow_state` owns active run state; there is no separate
  `workflow_runs` table.
- `StepRun` is the durable execution ledger for one step attempt.
- `WorkflowEngine.execute_workflow()` is the controller-owned sequential state
  machine.
- task summary, step projections/history/detail, deliverables, gates,
  questions, comments, task chat, and step chat already have API contracts.
- `system:general-task` is a valid and useful one-step `run` workflow with
  semantic evaluation and must remain behaviorally unchanged.

## Core Invariants

1. **Execution remains step-based.** Phases never advance the engine and are not
   persisted as an independent runtime state machine.
2. **Phases are presentation metadata.** A phase groups a contiguous ordered
   range of existing workflow steps for authoring and runtime presentation.
3. **Existing workflows remain valid.** Stored `run`/`gate` definitions with no
   presentation metadata continue to parse, execute, export, and render.
4. **One execution definition per task attempt.** Once a new task attempt starts,
   runtime execution and UI projection use the same pinned effective workflow
   definition.
5. **No duplicate runtime model.** Keep `Task.workflow_state` and `StepRun`; do
   not add `workflow_runs` or phase-run tables.
6. **Deterministic work is controller-owned.** Deterministic steps do not create
   an LLM turn, but they use the same tool policy, executor, guardrail, audit,
   output, recovery, and task-finalization envelopes.
7. **Heavy data stays lazy.** Task summary returns a lightweight cockpit
   projection. Full outputs, evaluations, logs, sessions, and deliverables use
   existing detail endpoints.
8. **The UI may be replaced; behavior may not silently disappear.** Existing
   task actions and workflow capabilities need explicit parity or an approved
   removal decision.
9. **The three redesigns stay separate.** Task Cockpit must not depend on the
   future global Control Center or Chat v2 Work Info.

## Workflow Presentation Model

Add optional presentation metadata to `Workflow`:

```python
class WorkflowPhaseDefinition(BaseModel):
    id: str
    title: str
    description: str = ""
    step_names: list[str]


class WorkflowPresentation(BaseModel):
    phases: list[WorkflowPhaseDefinition]


class Workflow(BaseModel):
    ...
    presentation: WorkflowPresentation | None = None
```

Example:

```yaml
presentation:
  phases:
    - id: collect
      title: Collect evidence
      description: Fetch bounded source data before invoking an agent.
      step_names:
        - fetch_slack
        - fetch_alerts
        - has_actionable_alerts

    - id: investigate
      title: Investigate
      step_names:
        - investigate_alert

    - id: conclude
      title: Conclude
      step_names:
        - review_mitigation
        - finish
```

### Validation

When `presentation.phases` is present:

- phase IDs are non-empty and unique;
- titles are non-empty;
- every referenced step exists;
- every workflow step belongs to exactly one phase;
- a step cannot appear twice;
- `step_names` preserve canonical workflow-step order;
- each phase owns one contiguous range in that order;
- phase order follows the first owned step;
- empty phases are rejected.

When presentation metadata is absent, the UI derives one implicit phase. The
implicit phase is not written back unless the workflow is explicitly edited and
saved in the new authoring UI.

Phases are intentionally not arbitrary overlapping labels. Projects, tags,
owners, and agent assignments already cover other grouping needs.

## Effective Workflow Snapshot

### Problem

Tasks currently store a mutable `workflow_id` plus JSON runtime state. Editing a
workflow while a task is running can change step names, routing, deterministic
templates, or phases used by a resumed task. It can also make the cockpit render
a definition different from the definition that produced existing `StepRun`s.

### Model

Extend `WorkflowState` with optional, backward-compatible fields:

```python
class WorkflowState(BaseModel):
    ...
    effective_workflow_version: int | None = None
    effective_workflow_digest: str | None = None
    effective_workflow_definition: dict[str, Any] | None = None
    routing_skips: dict[str, str] = {}
```

The definition is the fully resolved effective definition used for execution,
including applicable system-workflow overrides. The digest is computed from a
canonical JSON representation.

### Rules

- The first top-level execution attempt pins the effective definition before its
  first step executes.
- Runtime resume, recovery, phase projection, and task result construction use
  the pinned definition.
- Step retry, evaluator revision, gate resume, operator resume, and controller
  recovery stay inside the same top-level attempt and preserve its snapshot.
- The existing rerun action remains status-dependent: rerunning a paused task is
  an in-place resume and preserves its snapshot; rerunning a terminal task
  creates a cloned task, whose first execution pins the then-current effective
  definition. Do not change task identity or `attempt_number` semantics merely
  to implement snapshots.
- Existing in-flight tasks without a snapshot preserve current legacy behavior;
  do not rewrite them during deployment.
- Existing persisted workflow rows are not migrated.
- Snapshot fields live in existing JSON state; no schema migration is required.
- The full `effective_workflow_definition` is persistence/runtime-only. Public
  task and workflow-run serializers must omit it and expose only bounded
  version/digest/projection fields. Existing APIs must not leak prompts,
  templates, tool arguments, or the full snapshot through `workflow_state`.
- API and YAML serialization must omit absent optional fields. Loading a legacy
  definition must not materialize empty `presentation` metadata on write unless
  the caller changes the definition.

`routing_skips` is separate from the existing `skipped_steps` exhaustion
mechanism. Forward deterministic routing records every bypassed step with a
bounded reason such as `condition:<step_name>:false`. Routing skips are normal
control flow and do not fail the task. The current `skipped_steps` field keeps
its existing exhausted-step/failure semantics.

## Step Types

The engine supports:

- `run` — existing full agent step;
- `gate` — existing user/caller pause;
- `tool_call` — one deterministic Cognis tool call;
- `condition` — deterministic branch selection;
- `complete` — deterministic task completion.

The deterministic contract, renderer, safety rules, and authoring guidance are
normative in [`34-deterministic-workflows.md`](34-deterministic-workflows.md).

Phases can contain any step type. A phase has no separate evaluator, retry
policy, or gate. Those remain step/workflow concerns.

## Phase Projection

Phase status is derived from the pinned workflow definition, current task
attempt, latest step-run projections, pending pause, and task status.

Suggested API values:

```text
pending
active
waiting
completed
failed
cancelled
```

Derivation rules:

- `active` — contains the current running/evaluating step;
- `waiting` — contains the step referenced by the active pause/gate/question;
- `failed` — contains the terminal failing step for a failed task attempt;
- `cancelled` — active phase when task cancellation occurred;
- `completed` — all owned steps are terminal-success or explicitly skipped;
- `pending` — none of the above.

An explicitly skipped step includes both a persisted skipped `StepRun` (for
example `when=false`) and a step named in `routing_skips` because a forward
branch bypassed it. A terminal task cannot leave a bypassed phase looking
pending. Backward routing clears or supersedes routing-skip entries when a
previously bypassed step becomes reachable in the active attempt.

Backward revision or deterministic routing may move `active` to an earlier
phase. Phase progress is therefore not monotonic.

The backend owns this projection. The frontend must not independently
reconstruct attempt supersession, terminal precedence, or legacy fallback
semantics.

## Task Summary Projection

Extend the existing lightweight task-summary response rather than adding a
parallel cockpit endpoint:

```python
class WorkflowStepProjection(BaseModel):
    name: str
    type: str
    status: str
    attempt_count: int = 0
    duration_seconds: float | None = None
    has_output: bool = False
    has_logs: bool = False
    has_deliverable: bool = False
    skip_reason: str | None = None


class WorkflowPhaseProjection(BaseModel):
    id: str
    title: str
    description: str = ""
    status: str
    steps: list[WorkflowStepProjection]


class TaskWorkflowProjection(BaseModel):
    workflow_id: str
    workflow_version: int | None = None
    workflow_digest: str | None = None
    current_phase_id: str | None = None
    current_step_name: str | None = None
    phases: list[WorkflowPhaseProjection]
```

The task response carries
`workflow_projection: TaskWorkflowProjection | None`. Draft or otherwise valid
workflow-less tasks return `null`; the cockpit shows objective, configuration,
dependencies, comments, and actions with an explicit "No workflow assigned"
state instead of inventing an implicit workflow.

The task summary also continues to expose pending attention and result metadata
through its existing contracts. Its public `workflow_state` representation must
exclude the full effective definition. Full step details remain lazy:

- step attempt history;
- output and evaluation payloads;
- deliverables;
- Intaris/session logs;
- task/step chat continuation.

No large raw tool result or session transcript belongs in the cockpit summary.

## Task Cockpit UX

The Task Cockpit replaces the current workflow-centric task-detail presentation.
It is a human control surface over one durable task.

### Information hierarchy

1. **Header**
   - title, task status, priority, project, workflow, agent;
   - primary task actions;
   - elapsed/completed timing.
2. **Objective**
   - task description;
   - expected output;
   - current result summary when terminal.
3. **Attention**
   - active gate;
   - pending step questions;
   - credentials/auth challenges;
   - failure or blocked-state action.
4. **Workflow**
   - phase rail/summary;
   - expandable phase sections;
   - step cards with type, state, duration, attempt count, and evidence badges.
5. **Step inspector**
   - selected attempt;
   - output, evaluation, deliverables, logs, and chat links loaded lazily;
   - retry/revise/respond actions where valid.
6. **Task context**
   - comments/revisions;
   - dependencies;
   - configuration and delivery;
   - activity/audit as a secondary view.

### Runtime example

```text
Collect evidence                                      Completed
  ✓ Fetch Slack messages            Tool call · 1.2 s
  ✓ Query Alertmanager              Tool call · 0.4 s
  ✓ Check whether actionable        Condition

Investigate                                           Running
  → Investigate station connectivity Agent · 4m 21s
    [Open chat] [Logs] [Output]

Conclude                                              Pending
  ○ Review mitigation                Gate
  ○ Finish                           Complete
```

### UX rules

- The default view explains current state without requiring the user to inspect
  raw logs.
- A phase is a readable section, not a graph node.
- The active/waiting state and required user action are visible above the fold.
- Tool calls and agent runs are visually distinct.
- Attempts are accessible but do not dominate the default layout.
- Mobile uses the same hierarchy with a full-screen step inspector.
- The existing horizontal SVG workflow diagram is not part of the target
  cockpit.

## Workflow Authoring UX

The workflow editor is also replaced at the presentation layer.

### Builder

The primary authoring surface is a vertical phase/step builder:

```text
Collect evidence
  Tool call   Fetch Slack
  Tool call   Query Alertmanager
  Condition   Anything actionable?

Investigate
  Agent run   Investigate alert

Conclude
  Gate        Approve mitigation
  Complete    Finish
```

Supported interactions:

- add, rename, describe, reorder, and remove phases;
- add, reorder, duplicate, and remove steps;
- move a step within or between contiguous phases;
- choose step type from `run`, `gate`, `tool_call`, `condition`, `complete`;
- configure the common fields appropriate to that step type;
- expose evaluator, retry, route, profile, input, policy, and completion settings
  in an advanced inspector;
- validate targets and templates before save;
- preserve YAML import/export;
- preserve system-workflow override behavior;
- preserve draft/composed workflow editing and duplication.

### Non-goals

- no free-position graph canvas;
- no arbitrary parallel DAG authoring;
- no hidden auto-generated steps;
- no UI abstraction that changes canonical step order;
- no removal of advanced workflow capabilities solely because the new simple
  editor does not surface them prominently.

## Frontend Transition

The visual replacement is big-bang at route cutover, not a parallel product.

Implementation may build new components beside the existing page while tests are
developed, but completion requires:

- task-detail route uses the new Task Cockpit;
- workflow route uses the new authoring surface;
- existing actions and supported configuration have parity;
- obsolete presentation components and dead state are removed;
- no permanent user-facing legacy toggle remains.

Retain existing API client methods and action/controller logic where practical.
Do not preserve a 3,000-line route merely to avoid extracting stable behavior.

## Compatibility Matrix

| Case | Required behavior |
|---|---|
| Existing `system:general-task` | Same one-run-step execution and evaluator behavior |
| Existing user `run`/`gate` workflow | Parses, exports, executes, and renders |
| Legacy task without snapshot | Current resume behavior; projection uses documented fallback |
| New task | Pins effective definition before execution |
| New workflow without explicit phases | Renders one implicit phase |
| Backward evaluator revision | Can reactivate an earlier phase |
| Existing system override | Included in pinned effective definition |
| Workflow-less task/draft | Cockpit renders configuration/actions with no workflow projection |
| Existing task actions | Available from Task Cockpit |
| Heavy step output/log | Loaded lazily through existing endpoint |

## Testing Requirements

### Backend

- golden parse/serialize/export fixtures for existing workflows;
- `system:general-task` characterization;
- effective-definition snapshot and digest tests;
- resume uses pinned definition after source workflow edit;
- step retry, evaluator revision, gate/operator resume, and recovery preserve the
  current snapshot;
- paused rerun preserves its snapshot; terminal rerun creates a new task that
  pins a fresh definition;
- phase validation and implicit-phase fallback;
- forward branch records routing skips and terminal phases do not remain pending;
- backward routing can reactivate a previously routing-skipped step;
- phase projection across running, waiting, skipped, revised, failed, completed,
  and cancelled states;
- workflow-less task summary and cockpit fallback;
- public workflow state never serializes the full effective definition;
- deterministic tests from Spec 34;
- task summary remains bounded and does not embed heavy payloads.

### Frontend

- task cockpit rendering for legacy and phased workflows;
- task action parity;
- gate/question/credential/failure attention states;
- lazy attempt/output/log loading;
- step attempt switching and revision flow;
- workflow builder serialization round-trip;
- all step-type editors;
- system override, duplicate, import/export, and unsaved-change protection;
- responsive desktop/mobile behavior;
- keyboard and screen-reader navigation for phases and step cards.

### E2E

At minimum:

1. create and run existing `system:general-task`;
2. create phased `run`/`gate` workflow and resolve the gate;
3. deterministic fetch → false condition → silent complete with no LLM step;
4. deterministic fetch → true condition → agent run → complete;
5. edit source workflow while a task is paused, then resume pinned execution;
6. evaluator revision to a step in an earlier phase;
7. controller restart during read-only deterministic execution;
8. ambiguous side-effecting deterministic execution pauses/fails without replay;
9. use Task Cockpit actions and inspect lazy step output/log;
10. author, export, import, and rerun a workflow in the new UI.

## Acceptance Criteria

- Existing `run`/`gate` workflows and `system:general-task` retain behavior.
- No new workflow-runs or phase-runs persistence model is introduced.
- New task attempts use one pinned effective definition for runtime and UI.
- Deterministic steps satisfy Spec 34 restart and policy guarantees.
- Phases group canonical contiguous steps and do not affect execution.
- Task summary exposes a bounded backend-owned workflow projection.
- Task detail is replaced by the phase-oriented Task Cockpit.
- Workflow authoring is replaced by the phase/step builder.
- Existing task actions, gates, questions, revisions, outputs, logs,
  deliverables, import/export, and overrides have verified parity.
- The old task-detail/workflow-editor presentation is removed after parity.
- Control Center and Chat v2 Work Info are not coupled into this implementation.
