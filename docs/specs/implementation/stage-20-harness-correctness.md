# Stage 20: Harness Correctness and Concurrency Stabilization

## Status

PLANNED

## Goal

Resolve the highest-severity correctness issues in the current harness before
adding new workflow contracts or runtime complexity.

This stage implements Track A from
[`../23-harness-stabilization.md`](../23-harness-stabilization.md):

- remove singleton transient state from live execution paths
- bound retry/wait loops so turns cannot hang forever
- make gate restart behavior deterministic
- eliminate latent cross-user memory identity fallback
- preserve valid tool-call/tool-result history through interruption paths

## Dependencies

- `docs/specs/03-session-model.md`
- `docs/specs/05-integrations.md`
- `docs/specs/13-nfr-operations.md`
- `docs/specs/14-workflow-engine.md`
- `docs/specs/23-harness-stabilization.md`

## Scope

### In Scope

- step-local pending-event state
- bounded Intaris recovery loops
- gate timeout semantics and restart recovery
- scheduler DST correctness
- malformed/interrupted tool-call transcript repair
- explicit Mnemory user context enforcement
- recall failure semantics cleanup
- regression coverage for concurrent sessions and restart recovery

### Out of Scope

- parallel tool execution
- ripgrep/fd shell/search improvements
- prompt-cache improvements
- typed deliverables and step profiles

## Deliverables

### 1. Step-local execution state

- move `_pending_events` and related flush context off `AgentLoop`
- keep all pending event buffers scoped to `StepContext` or equivalent
- remove singleton mutation from failure/retry paths

### 2. Bounded recovery and timeout behavior

- retry ceilings and wall-clock caps for Intaris recovery paths
- structured failure result after retry exhaustion
- gate timeout action defaults to `fail`, with explicit configuration

### 3. Restart-safe gate recovery

- reuse persisted `pause_id` for gate steps
- prevent duplicate notification/waiter divergence on restart
- test paused-task recovery through full restart flow

### 4. Scheduler timezone correctness

- preserve IANA timezone semantics across DST changes
- add DST regression tests

### 5. Memory identity and recall semantics

- remove implicit Mnemory default subject fallback
- fail closed when scoped user context is absent
- make recall failure semantics explicit and aligned with the documented policy

### 6. History integrity under interruption

- repair interrupted tool-call batches so replayed history remains valid for
  provider tool-call constraints
- avoid call-id collisions in malformed-tool-call recovery

### 7. Tests and telemetry

- unit tests for concurrent state isolation and bounded retries
- integration tests for gate restart and Intaris outage
- metrics for retry exhaustion and gate timeout action

## Suggested Work Breakdown

### Workstream A: Agent loop state isolation

Files likely touched:

- `cognis/core/agent_loop.py`
- `cognis/core/context.py`

Tasks:

1. Introduce step-local pending event state.
2. Update emergency flush paths to consume local state only.
3. Add concurrent session regression tests.

### Workstream B: Intaris and gate recovery

Files likely touched:

- `cognis/core/agent_loop.py`
- `cognis/core/workflow_engine.py`
- `cognis/core/task_queue.py`
- `cognis/core/notifications.py`

Tasks:

1. Cap Intaris retry loops.
2. Reuse persisted gate `pause_id` on restart.
3. Change gate timeout default and surface the action in telemetry.

### Workstream C: Memory and scheduler hardening

Files likely touched:

- `cognis/providers/memory/mnemory.py`
- `cognis/core/context.py`
- `cognis/core/scheduler.py`

Tasks:

1. Remove implicit Mnemory fallback user.
2. Align recall failure semantics with the chosen policy.
3. Fix DST handling and add regression coverage.

## Acceptance Criteria

- no cross-session pending-event corruption in concurrent tests
- Intaris outage returns a bounded structured failure
- a restarted gate resolves the active workflow run using the original pause id
- scheduler tests pass across DST boundaries
- memory calls without scoped user context fail closed
