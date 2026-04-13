# Stage 18: First-Class Agent Runtimes

## Status

PLANNED

## Goal

Introduce first-class runtime support so `native` and `claude_code` execute
under one Cognis orchestration layer with the same product-facing behavior for:

- direct chat
- channels (web, Signal, Slack, etc.)
- slash commands
- tasks and workflows
- delegation
- notifications and approvals
- worktree policy
- delivery and transcript/history UX

This stage turns the runtime design specs into implementation scaffolding and a
usable Claude-backed runtime path.

## Dependencies

- `docs/specs/17-agent-runtimes.md`
- `docs/specs/18-runtime-contract.md`
- `docs/specs/19-runtime-implementation-plan.md`
- `docs/specs/03-session-model.md`
- `docs/specs/06-tool-system.md`
- `docs/specs/14-workflow-engine.md`

## Scope

### In Scope

- first-class runtime config on agents
- `native` runtime adapter wrapping current behavior
- runtime persistence model (`runtime_runs`)
- executor runtime RPC surface
- projection model for normalized conversation/session history
- executor-hosted `claude_code` runtime foundation
- direct chat parity path for Claude-backed agents
- delegated child execution via runtime-backed agents
- workflow step execution via runtime-backed agents
- runtime-aware tool-policy translation
- runtime observability and recovery hooks

### Out of Scope

- OpenCode runtime
- exposing Cognis orchestration tools inside Claude runtime
- runtime migration across executors
- perfect feature parity for every Claude-specific internal feature

## Why This Stage Exists

The current architecture assumes the Cognis agent loop is the only low-level
execution engine. That works for native agents but does not cleanly support
native external harnesses like Claude Code while preserving Cognis-owned:

- workflows
- task state
- channel UX
- slash commands
- session history
- approvals and notifications

This stage makes runtime a first-class abstraction so an alternative runtime can
replace the low-level agent loop without forcing wide product rewiring.

## Deliverables

### 1. Runtime Abstraction in Cognis

Add a runtime abstraction layer that all direct chat, delegated child sessions,
and workflow steps execute through.

Minimum deliverables:

- `agent.runtime` config model
- runtime registry and resolution
- `native` runtime adapter over current behavior
- runtime capability model

### 2. Durable Runtime Run Metadata

Add `runtime_runs` and any required projection checkpoint storage so runtime
execution can be resumed, replayed, and inspected safely.

Minimum requirements:

- one active runtime run per logical execution boundary
- stable runtime/session/execution IDs
- replay checkpointing
- durable pending question/approval linkage

### 3. Executor Runtime RPC

Extend the controller/executor protocol with runtime RPC operations:

- `runtime.start`
- `runtime.resume`
- `runtime.respond`
- `runtime.cancel`
- `runtime.status`
- `runtime.collect_events`

The runtime RPC must remain separate from `tool.execute`.

### 4. Projection Layer

Implement normalized conversation/session history projection so product surfaces
do not depend on raw runtime trace formats.

For Claude-backed runs:

- Intaris is the canonical raw low-level runtime trace store
- Cognis consumes raw trace + overlay events
- product history uses normalized projected events

### 5. Claude Runtime Host Foundation

Add executor-hosted Claude runtime support with:

- isolated Claude config root
- executor-local, acting-user-scoped auth state
- Intaris hook integration bootstrap
- runtime session lifecycle management
- runtime event collection/replay bridge

### 6. Tool-Policy Translation

Implement shared Cognis tool semantics with runtime-specific enforcement:

- `allow`
- `deny`
- `evaluate`
- `non_bypassable`
- timeout
- cancel

For `claude_code`, this means translating Cognis policy into Claude + Intaris
session/config enforcement rather than reusing the native ToolRouter loop.

### 7. Direct Chat Parity

Support direct chat with Claude-backed agents through the same product surface
as native agents, including:

- web chat
- Signal-routed conversations
- Slack-routed conversations
- slash commands
- approvals/questions
- normalized history

### 8. Delegation and Workflow Parity

Support runtime-transparent orchestration:

- `delegate(agent_id=...)` resolves target runtime automatically
- parent agent does not need runtime knowledge
- Claude-backed workflow steps integrate with evaluator/retry/cancel semantics

### 9. Runtime Observability

Add metrics and support/debugging for:

- runtime run counts by type/state
- projection lag
- replay failures
- capability mismatches
- policy translation failures
- stuck runtime detection

## Suggested Work Breakdown

### Workstream A: Runtime Models and Resolution

Files likely touched:

- `cognis/models/agent.py`
- `cognis/api/models.py`
- `cognis/api/runtime_support.py`
- `cognis/core/executor_resolution.py`
- new runtime package under `cognis/core/` or `cognis/runtime/`

Tasks:

1. Add runtime config models
2. Add runtime registry and resolution
3. Wrap current behavior in `native` runtime adapter
4. Keep upper layers runtime-agnostic

### Workstream B: Runtime Persistence and Replay

Files likely touched:

- `cognis/store/models.py`
- `cognis/store/queries.py`
- `cognis/bootstrap.py`
- `cognis/store/migrations/versions/*`

Tasks:

1. Add `runtime_runs` schema
2. Add bootstrap helper and migration
3. Add projection checkpointing
4. Add idempotent recovery/replay semantics

### Workstream C: Executor Runtime RPC

Files likely touched:

- `cognis/providers/executor/protocol.py`
- `cognis/providers/executor/websocket.py`
- `cognis/executor/runner.py`
- `cognis/api/executor_ws.py`

Tasks:

1. Define runtime RPC payloads/types
2. Implement controller-side runtime connection interface
3. Implement executor-side runtime host dispatch
4. Add replay-safe event collection

### Workstream D: Projection and History

Files likely touched:

- conversation/session history readers
- runtime projection module(s)
- Intaris integration reader path(s)

Tasks:

1. Define normalized projected event schema in code
2. Build native projection path
3. Build Claude raw-trace + overlay projection path
4. Ensure deterministic reconstruction after restart

### Workstream E: Claude Runtime Host

Files likely touched:

- executor runtime host package
- Claude runtime config/bootstrap code
- executor configuration surfaces

Tasks:

1. Add isolated Claude config root handling
2. Add acting-user-scoped auth handling
3. Start/resume/cancel Claude runtime sessions
4. Bridge Intaris hook-generated raw trace back into runtime event collection

### Workstream F: Tool Policy Translation

Files likely touched:

- runtime policy translation module(s)
- tool routing/policy evaluation integration points

Tasks:

1. Define effective runtime tool policy model
2. Map native tool semantics to `native` runtime
3. Map shared policy semantics to Claude + Intaris runtime enforcement
4. Add equivalence tests for allow/deny/evaluate/non-bypassable/timeout/cancel

### Workstream G: Direct Chat and Orchestration Parity

Files likely touched:

- conversation execution path
- workflow engine
- notification service
- WebSocket/API chat handlers

Tasks:

1. Route direct conversation execution through runtime adapter
2. Preserve slash command behavior above runtime layer
3. Route delegated child sessions through runtime resolution
4. Route workflow step execution through runtime adapter

### Workstream H: Ops and Debuggability

Tasks:

1. Add metrics/logging for runtime runs and projection state
2. Add operator-visible runtime state surfaces
3. Add stuck detection and recovery hooks
4. Add feature flag / kill switch for runtime types

## Acceptance Criteria

This stage is complete when:

1. Agents can declare `runtime.type = native | claude_code`.
2. Direct chat, delegation, and workflow execution all run through the same
   runtime abstraction.
3. Claude-backed direct chat works through the normal Cognis chat/channel UX.
4. `delegate(agent_id=...)` remains runtime-transparent.
5. Task/workflow steps can target Claude-backed agents without changing the
   workflow model.
6. Conversation history for Claude-backed sessions is reconstructed from a
   normalized projection rather than raw trace replay.
7. Tool-policy semantics are contract-tested as equivalent across `native` and
   `claude_code` for the supported surface.
8. Restart/replay works without duplicate user-visible events.
9. Runtime health, replay failures, and stuck sessions are observable.

## Required Tests

### Contract Tests

- runtime contract conformance for `native`
- runtime contract conformance for `claude_code`
- tool-policy translation equivalence

### Integration Tests

- direct Claude-backed chat via web
- direct Claude-backed chat via channel-routed conversation
- delegated run to Claude-backed agent
- workflow step execution using Claude-backed agent
- question/approval mid-run
- restart and replay during active runtime session
- slash command behavior parity

### Failure Tests

- executor disconnect mid-run
- Intaris unavailable during Claude startup
- duplicate raw runtime events
- projection checkpoint corruption
- orphaned Claude runtime host process

## Start Here

Recommended first implementation sequence:

1. runtime models + `native` adapter
2. `runtime_runs` schema + bootstrap + migration
3. executor runtime RPC
4. runtime contract tests
5. route direct conversation execution through runtime abstraction
6. Claude runtime host scaffold
7. raw trace projection path
8. direct chat parity hardening
