# Stage 39: Workflow Task Cockpit

## Status

DONE — awaiting human visual review and integration; not merged, pushed,
deployed, or released.

## Invocation

The complete handoff is:

> Implement Stage 39 end-to-end.

The implementing architect owns decomposition, managed implementation
workstreams, integration, compatibility, review, and acceptance evidence.

**Working mode: coordinate.**

## Goal

Deliver the workflow/task redesign specified by:

- [`../14-workflow-engine.md`](../14-workflow-engine.md);
- [`../34-deterministic-workflows.md`](../34-deterministic-workflows.md);
- [`../37-workflow-task-cockpit.md`](../37-workflow-task-cockpit.md).

The target is a **big-bang workflow UI/UX replacement on top of a conservative
engine evolution**:

- preserve the current task/workflow runtime and useful `system:general-task`;
- add effective workflow-definition snapshots;
- add UI-facing phases over canonical linked steps;
- add restart-safe deterministic workflow steps;
- replace task detail with the Task Cockpit;
- replace workflow authoring with a phase/step builder;
- verify parity and remove obsolete presentation code.

## Required Outcome

On completion, a user can:

1. run existing `run`/`gate` workflows unchanged;
2. understand a task through phases and step cards rather than a flat technical
   diagram;
3. inspect attempts, outputs, evaluations, logs, deliverables, and task/step chat
   from the new Task Cockpit;
4. author workflows as phases containing `run`, `gate`, `tool_call`,
   `condition`, and `complete` steps;
5. run mechanical fetch/check/no-op paths without invoking an LLM;
6. rely on pinned workflow semantics across edits, pauses, retries, and
   controller restarts.

## Non-Goals

- No global Control Center/dashboard.
- No Chat v2 Work Info.
- No task Kanban or project-model replacement.
- No new `workflow_runs` or phase-run table.
- No arbitrary graph canvas or parallel DAG runtime.
- No rewrite of existing `run`, evaluator, gate, schedule, delivery, or queue
  semantics.
- No deterministic `notify` or `transform` unless an implementation blocker
  proves one is essential and the architect records the decision.
- No production push or deployment as part of this stage.

## Hard Invariants

1. Existing persisted `run`/`gate` definitions remain valid.
2. `system:general-task` keeps its current one-run-step plus evaluator behavior.
3. `Task.workflow_state` remains the workflow-run state; `StepRun` remains the
   attempt ledger.
4. Phases group contiguous canonical steps and never advance the engine.
5. Runtime and UI use the same pinned effective definition for new task
   attempts.
6. Deterministic steps use the existing tool policy, executor, `ToolRouter`,
   guardrail/audit, output, recovery, and task-finalization envelopes.
7. A missing Intaris session never proves a deterministic tool was not
   dispatched.
8. Read-only and idempotent recovery decisions are explicit; ambiguous
   side-effecting dispatch is never silently replayed.
9. Heavy outputs and session logs remain lazy.
10. The final UI has no permanent legacy toggle.

## Baseline Hotspots

The architect must verify current line locations before editing, but the
principal ownership boundaries are:

### Backend

- `cognis/models/workflow.py`
- `cognis/core/workflow_registry.py`
- `cognis/core/workflow_engine.py`
- `cognis/core/task_queue.py`
- `cognis/core/agent_loop.py`
- `cognis/core/tool_router.py`
- `cognis/store/models.py`
- `cognis/store/queries.py`
- `cognis/api/models.py`
- `cognis/api/routes/tasks.py`
- `cognis/api/routes/workflows.py`
- workflow composition and orchestration tool schemas

### Frontend

- `ui/src/routes/(app)/tasks/[taskId]/+page.svelte`
- `ui/src/routes/(app)/workflows/+page.svelte`
- `ui/src/lib/task-detail.ts`
- `ui/src/lib/workflows.ts`
- `ui/src/lib/types/api.ts`
- `ui/src/lib/api/client.ts`
- `ui/src/lib/components/workflows/WorkflowDiagram.svelte`

### Tests and guidance

- workflow model/registry/engine/recovery/API tests;
- task-route and task-projection tests;
- UI component, route, serialization, and E2E tests;
- workflow-manager skill and LLM-facing workflow tool descriptions/examples.

## Delivery Strategy

This is one integrated stage with sequential acceptance gates. The architect may
delegate independent work only after shared contracts are pinned.

Recommended worktree strategy:

1. verify the intended base and create one isolated integration worktree;
2. keep shared model/API contract work architect-owned or single-owner;
3. delegate backend runtime and frontend components only after those contracts
   are committed in the integration branch;
4. require task-owned commits from each workstream;
5. integrate and resolve conflicts centrally;
6. run independent review after integration, not only per worker;
7. leave the completed stage committed locally; do not push or deploy.

## Workstream 39.0 — Compatibility Baseline

### Objective

Characterize current behavior before extension.

### Deliverables

- golden parse/serialize/export fixtures for representative existing system and
  user workflows;
- explicit `system:general-task` behavior test;
- characterization tests around regular tool dispatch before extraction;
- task-summary and step-projection response fixtures;
- inventory of existing task actions and workflow-editor capabilities used as
  the parity checklist.

### Gate

No production behavior changes. Existing focused workflow/task suites pass.

## Workstream 39.1 — Presentation Model and Effective Snapshot

### Objective

Add phase metadata and pin one effective workflow definition per new task
attempt.

### Deliverables

- `WorkflowPhaseDefinition`, `WorkflowPresentation`, and optional
  `Workflow.presentation`;
- contiguous/complete/ordered phase validation;
- implicit-phase fallback without rewriting legacy definitions;
- optional effective version, digest, and definition fields in `WorkflowState`;
- optional `routing_skips` separate from existing exhaustion `skipped_steps`;
- canonical digest helper;
- pin effective definition before first execution;
- resume and rerun semantics from Spec 37;
- public API serializer redaction of the full effective definition;
- golden omission/round-trip tests.

### Important constraints

- no DB table/column migration;
- include effective system overrides in the snapshot;
- existing in-flight state without a snapshot follows documented legacy
  fallback;
- retry/revision/resume/recovery preserve the current top-level attempt snapshot;
- paused-task rerun keeps the existing in-place resume behavior and snapshot;
- terminal-task rerun keeps the existing cloned-task behavior, and the clone
  pins the current definition before its first execution;
- do not change task identity or `attempt_number` semantics for snapshotting;
- full snapshot definitions are never emitted through public `workflow_state`;
- do not add phase runtime state.

### Gate

Backend architecture review. Demonstrate that editing a workflow does not change
the resume behavior or cockpit projection of a newly pinned task.

## Workstream 39.2 — Backend Cockpit Projection

### Objective

Make the backend the authority for phase and lightweight step state.

### Deliverables

- typed workflow/phase/step projection models from Spec 37;
- extend the existing task-summary response;
- optional projection and explicit workflow-less task fallback;
- projection uses pinned definition when present;
- legacy fallback uses current workflow definition plus implicit phase;
- correct attempt/supersession/status/timing/skip/attention derivation;
- routing-skipped steps/phases never remain pending after terminal completion;
- bounded payload with no heavy raw outputs or session transcript;
- frontend API/types updated from the contract.

### Gate

API contract review and projection tests across running, waiting, revised,
skipped, failed, cancelled, and completed tasks.

## Workstream 39.3 — Deterministic Rendering and Models

### Objective

Land safe deterministic definition support without yet dispatching real tools.

### Deliverables

- constrained renderer in `cognis/core/workflow_rendering.py`;
- expression, native, and text modes;
- strict JSON-like context, redaction, and size bounds;
- `tool_call`, `condition`, and `complete` config models;
- common `when`, `on_skip`, `on_error`, and `next` fields where approved by
  Spec 34;
- named-target and loop validation;
- API/import/export/composer schema support;
- no system workflow switched to deterministic execution yet.

### Gate

Security-focused review of template reachability and fail-closed behavior.

## Workstream 39.4 — Shared Tool Dispatch and Recovery

### Objective

Add a deterministic dispatch path without changing agent tool behavior or
creating duplicate side effects.

### Deliverables

- extract the smallest reusable tool dispatch boundary from
  `AgentLoop._execute_regular_tool`;
- preserve executor selection, `target_executor` handling, runtime metadata,
  tool registry, policy, `ToolRouter`, guardrails/audit, metrics, and artifact
  behavior;
- deterministic substate in `StepRun.runtime_info`;
- stable call identity and rendered-argument digest;
- deterministic-aware crash classification;
- explicit pre-dispatch, dispatching, output-persisted, and terminal recovery;
- read-only retry;
- ambiguous write/unknown dispatch pause/failure;
- idempotency argument propagation only for tools with a declared compatible
  contract.

### Gate

Independent backend/code review. Existing agent-loop tool tests and new crash
matrix pass before a real deterministic step uses the helper.

## Workstream 39.5 — Deterministic Step Execution

### Objective

Complete the engine extension from Spec 34.

### Deliverables

- controller-owned `tool_call`;
- `when` skip for supported step types;
- `condition` branching and loop protection;
- persist forward-branch bypasses in `routing_skips`, not exhaustion
  `skipped_steps`, and reactivate them correctly on backward routing;
- `complete` through existing task finalization and delivery;
- `StepRun`, `StepOutput`, workflow-state, event, result, and recovery
  integration;
- read-only tools enabled first;
- side-effecting tools require explicit definition permission and runtime policy;
- tool/composer descriptions and Workflow Manager skill guidance updated.

### Gate

Backend acceptance scenario:

```text
read-only tool_call
  → condition false
  → complete silently
```

must complete with zero LLM calls after tool execution. The true branch must
route into an existing agent `run`.

## Workstream 39.6 — Task Cockpit UI

### Objective

Replace task-detail presentation with the Task Cockpit.

### Deliverables

- new focused components under a task-cockpit component boundary;
- header, objective, attention, phase rail/sections, step cards, lazy inspector,
  result, task context, and activity views;
- distinct visual language for agent, tool, condition, gate, and complete steps;
- retain task lifecycle actions;
- retain gate/question/credential/revision flows;
- retain task chat, step chat, logs, outputs, evaluations, deliverables,
  comments, dependencies, configuration, and delivery;
- responsive desktop/mobile and keyboard-accessible behavior;
- remove cockpit dependency on `WorkflowDiagram`.

### Gate

Frontend parity review against the 39.0 inventory, focused UI tests, and E2E
task scenarios pass.

## Workstream 39.7 — Workflow Authoring UI

### Objective

Replace the current editor presentation with the phase/step builder.

### Deliverables

- vertical phase builder;
- add/reorder/rename/remove phases;
- add/move/duplicate/reorder/remove steps;
- editors for all five step types;
- advanced inspector for evaluator, retries, routes, input, profiles, policies,
  completion, and revisions;
- target/template validation;
- preserve system overrides, duplication, composed drafts, YAML import/export,
  dirty-form protection, and mobile navigation;
- serialization round-trip through canonical API models.

### Gate

Author/import/export/reopen/run E2E scenario passes for both legacy and
deterministic phased workflows.

## Workstream 39.8 — Cutover, Cleanup, and Proof

### Objective

Integrate the full stage, remove obsolete presentation code, and produce
acceptance evidence.

### Deliverables

- task and workflow routes cut over to new UI;
- obsolete task-detail/workflow-editor presentation state removed;
- unused workflow-diagram code removed when no remaining consumer exists;
- no permanent feature flag or legacy toggle;
- frontend build/type/lint checks;
- focused backend suites;
- required E2E matrix;
- independent findings-first review with all blocking findings resolved;
- final stage document status changed to `DONE` with commit and verification
  evidence.

## Dependency Graph

```text
39.0 Compatibility baseline
  └─ 39.1 Presentation + snapshot
       ├─ 39.2 Cockpit projection
       │    └─ 39.6 Task Cockpit
       └─ 39.3 Rendering + deterministic models
            └─ 39.4 Shared dispatch + recovery
                 └─ 39.5 Deterministic execution
                      └─ 39.7 Workflow authoring parity

39.6 + 39.7 + all backend work
  └─ 39.8 Cutover, cleanup, proof
```

Limited parallelism:

- after 39.1, projection work and rendering/model work may proceed in parallel;
- Task Cockpit may begin after the projection contract is pinned;
- workflow authoring may begin against pinned model contracts, but final
  serialization parity waits for deterministic models;
- shared backend hotspots (`workflow.py`, `workflow_engine.py`, task API models)
  must have one integration owner.

## Required Test Matrix

### Compatibility

- existing workflow golden fixtures;
- `system:general-task`;
- existing evaluator retry/revision;
- gate pause/resume;
- schedule-created task;
- delivery modes;
- system overrides;
- workflow duplicate/import/export.

### Snapshot and phases

- pin before first execution;
- source edit during pause does not alter resume;
- retry/revision/resume/recovery preserve snapshot;
- paused rerun preserves snapshot; terminal rerun clone pins new definition;
- implicit phase;
- invalid/duplicate/non-contiguous phase membership;
- forward branch bypass and backward reactivation;
- revision to earlier phase;
- workflow-less task projection;
- full effective definition absent from public responses;
- bounded task summary.

### Deterministic runtime

- strict rendering and security rejection;
- read-only builtin and MCP tool calls;
- executor routing and `target_executor`;
- tool error and configured handling;
- false `when` without LLM invocation;
- both condition branches;
- deterministic loop cap;
- silent and non-silent complete;
- crash before dispatch;
- crash during read-only dispatch;
- crash during write/unknown dispatch;
- crash after output persistence before workflow advancement;
- terminal deterministic step never re-executes.

### UI/E2E

- legacy general task cockpit;
- phased `run`/`gate` workflow;
- active gate and in-step question;
- failure/retry/revision;
- lazy output/log/deliverable;
- deterministic false/no-op branch;
- deterministic true/agent branch;
- author/save/export/import/run;
- responsive desktop/mobile.

## Review Contract

The architect must obtain:

1. architecture review after 39.1;
2. security/recovery review after 39.4;
3. UI/UX parity review after 39.6 and 39.7;
4. independent integrated code review before 39.8 completion.

Reviewers receive this stage, the normative specs, exact diff/commits, and
verification evidence. They must not reconstruct scope from implementation.

High or critical findings block progression. Medium findings block only when
they represent concrete correctness, security, recovery, data-loss, or accepted
scope violations.

## Final Acceptance Criteria

- Existing workflows and `system:general-task` retain behavior.
- New task attempts pin one effective definition used by runtime and UI.
- Phases are validated presentation metadata over contiguous steps.
- Existing task summary exposes a bounded backend-owned cockpit projection.
- Deterministic steps are controller-owned, inspectable, and restart-safe.
- Ambiguous side effects are never silently replayed.
- Task detail is the new phase-oriented Task Cockpit.
- Workflow authoring is the new phase/step builder.
- Existing task actions and advanced workflow capabilities have verified parity.
- Obsolete presentation code is removed.
- Global Control Center and Chat v2 Work Info remain separate.
- All required checks and independent reviews pass.
- Stage documentation records final commits, commands, outcomes, and residual
  risks.

## Completion Record

- Integration branch/worktree:
  `feat/stage-39-workflow-task-cockpit` in
  `/home/riker/src/cognis-stage39`.
- Implementation stack:
  - `32ece3ab` — effective workflow snapshots and presentation phases;
  - `f4081df9` — deterministic definitions and secure rendering;
  - `5607ab10` — restart-safe deterministic runtime;
  - `aa104815` — phase builder and Task Cockpit frontend;
  - `5af194f9` — backend-owned cockpit projection;
  - `231fcfbe` — integrated cockpit cutover and obsolete diagram removal;
  - the correction commit containing this completion record — lossless
    deterministic editor controls, compatible fresh-step serialization, and
    runtime-bounded backward routing. Its immutable hash is recorded in the
    final implementation report because a commit cannot contain its own hash.
- Backend verification:
  focused workflow snapshot, presentation, model, rendering, registry, engine,
  and deterministic runtime suites passed. The final correction run was
  `uv run pytest tests/unit/test_workflow_deterministic_models.py
  tests/unit/test_workflow_deterministic_execution.py
  tests/unit/test_workflow_registry.py -q` (**50 passed**), followed by Ruff on
  every changed Python file.
- Frontend verification:
  the final shared-adapter run passed **919 tests** across 89 files;
  `npm run check` reported zero errors and warnings; `npm run build` completed
  successfully. Round-trip coverage includes `when`, `on_skip`, `on_error`,
  `next`, all deterministic subtype configuration, and
  `complete.notification`; fresh deterministic steps omit agent-only fields.
- E2E verification:
  integrated backend execution covers condition routing, silent completion,
  backward reactivation, and global deterministic jump-cap termination.
  A self-contained route fixture covers the task brief and expected output,
  attention and actionable pause controls, phases and skipped steps, lazy
  deterministic output, task todos and managed progress, canonical final
  deliverable, compact and expanded Work, persistent Task Control Chat reopen,
  mobile lifecycle actions, Chat v2 Work deep-linking, and the phased workflow
  builder. The final focused run passed **5 browser scenarios**.
- Independent reviews:
  backend projection and deterministic runtime received focused independent
  review before integration. The final integrated review identified lossy
  deterministic authoring, incompatible fresh-step serialization, and
  unreachable backward routing; all three findings were corrected and covered
  by regression tests.
- Residual risks:
  human visual review remains required before integration or deployment.
  Browser acceptance uses a deterministic API fixture rather than a deployed
  controller, and workflow builder browser coverage verifies phased
  deterministic reload/presentation but not a mutating save/import round-trip.
- Final integration correction:
  - reconciled the public task contract on `progress` and canonical
    `conversation_id` work links;
  - added compact task Work aggregation over authorized task-step links, with
    per-step expanded Work and lazy command output;
  - made `?view=work` an effective Chat v2 deep link;
  - expanded the self-contained Playwright fixture and browser matrix;
  - is recorded by the immutable commit hash in the final report because a
    commit cannot contain its own hash.
- Final validation:
  - focused backend task control, projection, Work API, scheduler, agent-loop,
    queue, registry, and deterministic execution suites: **683 passed**;
  - `npm test -- --run`: **929 passed** across 95 files;
  - `npm run check`: zero errors and warnings;
  - `npm run build`: success;
  - focused Playwright: **5 passed**.
- Human-review evidence was refreshed from a final **5 passed** Playwright run.
  The deterministic fixture reports complete first-run readiness and avoids
  setup/reconnect chrome in the targeted product captures. Stable screenshots:
  - `docs/screenshots/stage-39/desktop-paused-overview.png`
    (1263×1370; `7b1b190610bf42bd168ab63810d3d2f3e08ce9b71234880f4cb0bbdf08af4c94`);
  - `desktop-attention-controls.png`
    (827×440; `6b635b62823528cf45c32445734f8a1516fae7939804185447d76738a9ef73b7`);
  - `desktop-progress-phases.png`
    (1263×1370; `c902ce84b52059bdccc59c520e4338ea0fb21247779d682b40a3b91310873590`);
  - `desktop-completed-work.png`
    (1280×720; `15d52ca1f377629bc10cd136377d558747247c3f966313b5861ae199e5483878`);
  - `mobile-paused-overview.png`
    (390×844; `532f5c4491f6e085a25de00d64d8ca4bc1031b82a9245ec58f5f5abfc777a0bd`);
  - `desktop-control-chat.png`
    (1440×1000; `eecac1bcf38125c67d61c9c4ebb1242a9035d35d8017ea97431b27fff91a57a6`);
  - `mobile-control-chat-reopened.png`
    (390×844; `6f554374e72ee3c4f31dbe7d2304928c42c823ec1fe9bbdded990e01377beed0`);
  - `chat-v2-work.png`
    (941×784; `30d3fee69acc71f63916cd7a2fdde555419077faf0853e6aebd93c8721180cc2`);
  - `chat-v2-work-mutations.png`
    (941×63; `b95851110883f65f46e00be59d0f355df2d49d6583768e91cd811bd7db4f776e`);
  - `workflow-builder-phases.png`
    (725×620; `af9f660a0e4ec84f240b83d522dc6d65174500a88b02e5853ac167fd72e0f8a5`);
  - `workflow-builder-deterministic-step.png`
    (725×1030; `b1ea2f74d3768b145fe3ecd3139a06a35c53c48c84b930fac2d7d99f5b3c7cda`).
- Final Task Control Chat evidence exposed and corrected two real UI defects:
  local SvelteKit CSP blocked the same-origin Chat v2 iframe, and the underlying
  mobile bottom navigation obscured the embedded composer. The fixture now
  serves the real conversation open/detail, sessions, Intaris detail, Chat v2
  snapshot/runtime, queue, and persisted timeline paths. The dedicated
  open/close/reopen browser scenario passed twice with the same conversation,
  prior user/assistant content, and actionable composer visible.
- Persistent Task Control Chat now suppresses embedded Star, Archive, and Delete
  controls while retaining Search, Info, and Work. Backend archive, soft-delete,
  and purge routes reject task-linked control conversations with
  `task_control_conversation_persistent`; resource-owner authorization remains
  mandatory before this persistence guard. The refreshed desktop and mobile
  screenshots were published only after the dedicated browser scenario passed
  twice.
