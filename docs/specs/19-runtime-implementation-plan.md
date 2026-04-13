# Runtime Implementation Plan

## Purpose

This document defines the implementation plan for first-class agent runtimes.
It depends on:

- `17-agent-runtimes.md`
- `18-runtime-contract.md`

It is intended to be implementation-ready enough to resume work in a later
session without re-deriving the architecture.

## Scope

### In scope

- first-class runtime abstraction in Cognis
- `native` runtime adapter wrapping current behavior
- `claude_code` runtime adapter and executor runtime host
- direct chat parity for Claude-backed agents
- delegated child execution using runtime-backed agents
- task/workflow step execution using runtime-backed agents
- normalized transcript projection from Intaris + Cognis overlay events
- runtime-aware tool-policy translation

### Out of scope for the first implementation wave

- OpenCode runtime
- Claude runtime exposing Cognis orchestration tools inside Claude
- advanced runtime migration across executors
- perfect parity for every vendor-specific runtime feature

## Architectural Decisions

1. Upper layers are runtime-agnostic.
2. `native` and `claude_code` are low-level runtime implementations.
3. Claude-backed raw low-level runtime trace is stored durably in Intaris.
4. User-visible session history is a normalized projection.
5. Tool policy semantics are Cognis-owned and translated by the runtime.

## MVP Parity Matrix

| Feature | Native | Claude v1 target |
|---|---|---|
| Direct chat | Yes | Yes |
| Signal direct chat | Yes | Yes |
| Slack direct chat | Yes | Yes |
| Slash commands | Yes | Yes |
| Streaming output | Yes | Yes |
| Tool indicators | Yes | Yes (projected) |
| Approvals/questions | Yes | Yes |
| Delegation target transparency | Yes | Yes |
| Task-backed workflows | Yes | Yes |
| Workflow retries/evaluation | Yes | Yes |
| Cancel/retry/restart recovery | Yes | Yes |
| Normalized session history | Yes | Yes |

Anything not meeting this matrix must be feature-flagged or explicitly deferred.

## Workstreams

### Workstream 1: Runtime abstraction

Deliverables:

- runtime models in `cognis/models/`
- controller runtime registry and resolution
- `native` runtime adapter over current loop
- runtime capability negotiation with executors

Files likely affected:

- `cognis/models/agent.py`
- `cognis/api/models.py`
- `cognis/core/agent_loop.py`
- `cognis/api/runtime_support.py`
- `cognis/core/executor_resolution.py`
- new runtime package under `cognis/core/` or `cognis/runtime/`

Acceptance criteria:

- agent runtime is resolved without upper-layer runtime branching
- current behavior works via `native` adapter unchanged

### Workstream 2: Runtime persistence and replay

Deliverables:

- `runtime_runs` schema
- bootstrap helper + Alembic migration
- replay/projection checkpoints
- runtime run status model

Suggested schema objects:

- `runtime_runs`
- optional projection checkpoint table if not embedded in `runtime_runs.metadata`

Acceptance criteria:

- one active runtime run per logical execution boundary
- restart-safe replay checkpointing
- durable pending question/approval state

### Workstream 3: Executor runtime RPC

Deliverables:

- runtime RPC message types
- controller-side runtime connection interface
- executor-side runtime host interface

Files likely affected:

- `cognis/providers/executor/protocol.py`
- `cognis/providers/executor/websocket.py`
- `cognis/executor/runner.py`
- `cognis/api/executor_ws.py`

Acceptance criteria:

- `runtime.start`
- `runtime.resume`
- `runtime.respond`
- `runtime.cancel`
- `runtime.status`
- `runtime.collect_events`
- replay is idempotent after reconnect

### Workstream 4: Transcript projection

Deliverables:

- normalized projected event schema
- projector for native runtime events
- projector for Claude raw Intaris events + Cognis overlay events
- conversation history reader updated to use projection where required

Design choice:

- do not show raw runtime trace directly as product transcript
- raw trace remains available for support/audit/debugging

Acceptance criteria:

- deterministic reconstruction of user-visible history after restart
- no duplicate visible events after replay

### Workstream 5: Claude runtime host

Deliverables:

- executor-side Claude runtime host
- isolated Claude config root
- native Claude auth handling
- Intaris hook integration bootstrap
- runtime session lifecycle management

Key requirements:

- one runtime session per active Cognis session in MVP
- direct chat and workflow step execution use the same runtime host
- Claude can run without Cognis orchestration tools inside the runtime

Acceptance criteria:

- executor can start/resume/cancel Claude sessions
- runtime state survives controller restart
- auth state is executor-local, acting-user scoped, and auditable

### Workstream 6: Tool-policy translation

Deliverables:

- shared effective runtime tool policy model
- native translation path
- Claude translation path to Claude + Intaris session policy/config

Acceptance criteria:

- `allow`, `deny`, `evaluate`, `non_bypassable`, timeout, and cancel semantics are equivalent by contract tests
- fail-closed behavior is preserved where required

### Workstream 7: Direct chat parity

Deliverables:

- direct conversation execution through runtime adapter
- slash command mapping for Claude runtime
- Signal/Slack/web direct chat support using same conversation model as native

Acceptance criteria:

- a user can chat directly with a Claude-backed agent through existing channels
- `/approve`, `/deny`, `/new`, `/reset`, `/compact` behave consistently
- direct chat survives restart via runtime replay/projection

### Workstream 8: Delegation and workflow parity

Deliverables:

- `delegate(agent_id=...)` runtime-transparent behavior
- runtime-backed child sessions
- workflow step execution through runtime adapter
- evaluator/retry/cancel integration

Acceptance criteria:

- parent agent does not need to know target runtime type
- workflow engine keeps step ownership and evaluator control
- Claude-backed steps can plan, ask questions, implement, review, and return structured outcomes

### Workstream 9: Observability and operations

Deliverables:

- metrics for runtime runs, projection lag, replay failures, capability mismatch
- structured logs for runtime state changes
- support/debug views for runtime trace vs projected transcript
- operator kill switch / feature flag for runtime types

Acceptance criteria:

- stuck sessions are detectable
- projection lag is visible
- runtime health is visible in settings/health endpoints

## Proposed Phase Order

### Phase 1

- runtime abstraction
- `runtime_runs`
- executor runtime RPC
- `native` adapter

### Phase 2

- projection model
- Claude runtime host bootstrap
- Intaris-backed raw trace ingestion

### Phase 3

- direct Claude chat parity
- slash commands
- approvals/questions/recovery

### Phase 4

- delegation parity
- workflow/task parity
- structured step outcomes and retries

### Phase 5

- hardening: replay, chaos testing, stuck detection, support tooling

## Required Tests

### Contract tests

- runtime contract conformance for `native`
- runtime contract conformance for `claude_code`
- tool-policy translation equivalence

### Integration tests

- direct chat in web
- direct chat in Signal/Slack-routed conversation model
- delegated child run to Claude-backed agent
- workflow `plan -> implement -> review -> deliver`
- question and approval mid-run
- restart during active run
- replay after reconnect
- cancel during tool execution
- compaction/reset rollover

### Failure tests

- Intaris unavailable during Claude startup
- executor disconnect during active Claude run
- projection checkpoint corruption
- duplicate raw events
- orphaned runtime host process

## Open Decisions To Resolve During Implementation

1. whether normalized projected events are materialized incrementally or rebuilt on read from raw trace + checkpoints
2. exact Intaris event shapes available from Claude integration and what adapter translation is still needed
3. exact worktree attachment model for direct chat vs task-backed workflows, following native Cognis behavior

## Start Here Next Session

Recommended first implementation steps:

1. add runtime model types and `native` runtime adapter
2. add `runtime_runs` DB schema + bootstrap + migration
3. extend executor protocol with runtime RPC types
4. write failing contract tests for runtime parity
5. wire direct conversation execution through runtime adapter without changing behavior for `native`

Only after that:

6. build Claude runtime host scaffold on executor
7. integrate Intaris-backed raw trace + projection
8. enable Claude runtime behind a feature flag
