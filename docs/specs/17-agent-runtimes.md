# Cognis: Agent Runtimes

## Purpose

This document defines how Cognis supports multiple agent runtimes while
keeping workflows, task orchestration, notifications, memory, and guardrails
controller-owned.

The first external runtime is **Claude Code**. Future runtimes such as
OpenCode should fit the same model.

The normative low-level runtime contract is defined in
`18-runtime-contract.md`. This document focuses on runtime strategy and the
Claude-specific direction.

## Design Goals

1. **Agent identity stays in Cognis**

   Agents remain Cognis objects with the same ID, permissions, delivery
   targets, bindings, memory policy, and workflow behavior regardless of
   runtime.

2. **Workflows stay above runtimes**

   A workflow still defines steps like `plan`, `implement`, `review`, and
   `deliver`. The runtime only changes how a step or direct turn executes.

3. **Executors host runtimes**

   The controller does not run native Claude Code or other external harnesses.
   An executor hosts the selected runtime and exposes a runtime RPC surface.

4. **Native experience where possible**

   For Claude Code, Cognis should preserve native session, auth, resume,
   configuration, and event semantics as much as possible instead of
   re-implementing Claude behavior in a wrapper LLM loop.

5. **Cognis remains the orchestrator**

   Cognis still owns workflow transitions, task state, notifications,
   cancellation, retries, delivery, audit linkage, and persistence of
   orchestration metadata.

6. **External runtime integrations must be reliable**

   Runtime sessions must be resumable, cancellable, observable, and detectable
   when stuck, disconnected, or orphaned.

## Core Model

### Trust boundary

The controller remains the source of truth for:

- workflow state
- task state
- conversation/session lineage
- notifications and user-visible pause reasons
- delivery and audit linkage

Executors may host runtime-native state for external runtimes, but they do not
become workflow owners.

For `claude_code`, the runtime host is an explicit exception to the historical
"pure tool sandbox" model. That exception is narrow:

- it may persist Claude-native auth/session state in an isolated runtime root
- it may maintain runtime-native event buffers and lease metadata long enough
  for recovery
- it must not become the system of record for Cognis tasks, conversations, or
  workflow transitions

For Claude-backed sessions, raw low-level session trace durability belongs to
Intaris. Cognis consumes a normalized projection rather than duplicating the
raw Claude session log as a second low-level store.

### Agent runtime is first-class

Each agent has a runtime configuration separate from executor placement.

```python
class AgentRuntimeConfig(BaseModel):
    type: Literal["native", "claude_code", "opencode"] = "native"
    config: dict[str, Any] = Field(default_factory=dict)
```

Updated agent model:

```python
class AgentDefinition(BaseModel):
    ...
    llm_config: AgentLLMConfig | None = None
    execution: dict[str, Any] | None = None   # executor placement
    runtime: AgentRuntimeConfig | None = None # how the agent runs
    ...
```

### Runtime and executor are different concerns

- **Runtime** answers: how does this agent execute a turn or workflow step?
- **Executor** answers: where does that runtime run, and which local resources
  does it use?

Examples:

- `native` runtime on in-process executor
- `native` runtime on WebSocket executor near local tools
- `claude_code` runtime on a user-owned remote executor

### Workflow semantics do not change

For a software development workflow:

```text
plan -> implement -> review -> revise -> deliver
```

Each step still has:

- an assigned agent
- completion criteria
- optional evaluation
- optional human questions or gates
- retry and iteration policy

If the step agent uses `runtime.type = "claude_code"`, that step executes via
Claude Code on the selected executor. If it uses `native`, it executes through
the existing Cognis agent loop.

## Runtime Resolution

Runtime resolution happens after agent selection and before step execution.

Resolution inputs:

- agent runtime config
- agent execution config
- workflow step metadata
- owner/user context
- available executors

Resolution outputs:

- selected runtime adapter
- selected executor placement
- effective runtime config

Resolution order:

1. resolve the agent for the turn or step
2. resolve the requested runtime from `agent.runtime`
3. co-resolve a compatible executor from `agent.execution`
4. validate runtime support and runtime-specific capability metadata
5. construct a runtime session handle for direct turn or workflow step use

An executor may advertise supported runtimes, for example:

```json
{
  "supported_runtimes": ["native", "claude_code"],
  "runtime_metadata": {
    "claude_code": {
      "cli_path": "/usr/local/bin/claude",
      "version": "2.1.77"
    }
  }
}
```

## Runtime Adapter Contract

The controller talks to runtimes through a normalized adapter contract.
See `18-runtime-contract.md` for the normative lifecycle, event, capability,
projection, and tool-contract details.

```python
class AgentRuntimeAdapter(Protocol):
    async def start_turn(self, request: RuntimeTurnRequest) -> RuntimeRunHandle: ...
    async def resume_turn(self, request: RuntimeResumeRequest) -> RuntimeRunHandle: ...
    async def cancel(self, request: RuntimeCancelRequest) -> None: ...
    async def submit_response(self, request: RuntimeResponseRequest) -> None: ...
```

Normalized runtime events:

- `message_delta`
- `message_completed`
- `progress`
- `tool_activity`
- `question`
- `permission_request`
- `completed`
- `failed`
- `cancelled`
- `heartbeat`

The controller projects these into:

- WebSocket chat events
- task progress updates
- notifications and `PauseWaiter` state
- workflow step completion / retry / failure transitions

## Executor Runtime RPC

Executors need a runtime RPC surface in addition to `tool.execute`.

Required methods:

- `runtime.start`
- `runtime.resume`
- `runtime.respond`
- `runtime.cancel`
- `runtime.status`
- `runtime.collect_events`

Runtime event collection must support:

- monotonic event sequence numbers per runtime run
- idempotent re-fetch after reconnect
- acknowledgement or last-seen cursor from the controller
- replay of buffered events after controller restart or reconnect

This is intentionally separate from tool execution because external runtimes
are long-lived, stateful, and interruptible. They are not modeled as a single
tool call.

Example request:

```json
{
  "method": "runtime.start",
  "params": {
    "runtime_type": "claude_code",
    "run_kind": "workflow_step",
    "agent_id": "backend-coder",
    "session_id": "sess_123",
    "step_run_id": "step_456",
    "config": {"permission_mode": "bypassPermissions"},
    "input": {...}
  }
}
```

## Claude Code Runtime

### Goals

Claude Code integration should behave like a first-class Cognis runtime, not a
best-effort subprocess wrapper.

The Claude Code runtime should preserve native Claude behavior for:

- subscription and OAuth authentication
- native Claude session persistence and resume
- native Claude event streaming
- native Claude model/tool/session semantics
- native Claude configuration and runtime state

while integrating into Cognis for:

- workflow orchestration
- task lifecycle
- notifications and questions
- Intaris guardrails
- Mnemory memory plumbing
- task result delivery and audit linkage

### Primary transport

Claude Code runtime should be **CLI-first**, not SDK-first.

Reasons:

- CLI exposes the most native Claude Code behavior
- CLI supports real session persistence and resume
- CLI supports stream JSON output for event translation
- CLI supports Claude subscription auth and long-lived setup tokens
- CLI keeps Cognis aligned with Anthropic's intended Claude Code surface

The SDK can still be used for tests, compatibility probes, or narrowly scoped
helpers, but it is not the primary runtime implementation.

### Authentication

Claude Code runtime uses Claude's native authentication model rather than
Cognis's generic LLM provider configuration.

Supported auth methods for Cognis-managed Claude runtime:

1. Claude subscription OAuth login
2. Console/API-key based auth only when explicitly configured

Auth is user-scoped and executor-local. Cognis does not proxy Claude Code
inference through LiteLLM for `claude_code` runtime sessions.

User-facing rules:

- Claude auth is attached to the user on a specific executor runtime host
- moving a Claude-backed agent to another executor requires separate Claude
  auth on that executor unless migration support is explicitly implemented
- the UI must show which executor currently holds Claude auth for the agent
- auth expiry, invalidation, or required re-login must surface as a first-class
  runtime status, not as a generic task failure
- retention, revocation, and cleanup actions for executor-stored Claude auth
  and transcripts must be explicit in the UI and auditable in Cognis

### Config and state isolation

Cognis should run Claude Code with an isolated, Cognis-owned config root.

Suggested layout on the executor:

```text
<executor-data>/runtimes/claude-code/
  <acting_user_email>/
    settings.json
    state/
    sessions/
    logs/
    auth/
    agents/
```

Goals of isolated config:

- no dependency on ambient `~/.claude` state
- deterministic Cognis-managed settings
- clean per-user separation
- explicit cleanup and retention policy
- no accidental pickup of unrelated user hooks/plugins/settings

### Cognis-managed Claude settings

For Cognis-managed runs, the executor should generate or maintain Claude Code
settings under the isolated config root.

Recommended defaults:

- disable hooks unless explicitly enabled by Cognis
- disable IDE auto-install and auto-connect
- disable deep link registration
- disable native git workflow instructions when Cognis provides workflow-level
  instructions
- load only the project/runtime configuration that Cognis explicitly chooses

### Intaris integration

The Claude Code runtime should use the existing Claude Code <-> Intaris
integration path where available instead of recreating permission prompts in a
side channel.

Design requirements:

- Intaris remains the primary policy and approval authority for Cognis-managed
  Claude sessions
- Claude native permission UX should be minimized or bypassed for managed runs
- approvals, denials, and escalations must still surface in Cognis
- Cognis notifications remain the user-facing pause/resume mechanism

This keeps one approval model across native and Claude-backed agents.

Claude v1 approved mode:

- use the existing Claude Code <-> Intaris integration where available
- Cognis still receives normalized approval/question events and remains the
  user-facing orchestration layer
- if native integration cannot preserve Cognis audit linkage or notification
  flow for a given action type, that action type is out of scope for v1
- native Intaris-side records must be attributable to `runtime_run_id`,
  `acting_user_email`, and Cognis `agent_id`
- approval/question replay after reconnect must not create duplicate Intaris
  decisions or duplicated Cognis notifications

### Mnemory integration

The Claude Code runtime should use the existing Claude Code <-> Mnemory
integration path where available.

Controller-owned responsibilities remain:

- which memories are in scope for the Cognis session
- when auto-recall and remember are enabled for the agent type
- how memory failures degrade the surrounding Cognis workflow

Claude v1 approved mode:

- use the existing Claude Code <-> Mnemory integration where available
- Cognis remains the owner of agent identity, agent memory policy, and whether
  a given agent class should auto-recall or auto-remember
- native Mnemory writes must be replay-safe and attributable to the Cognis
  `runtime_run_id`, `acting_user_email`, and `agent_id`

### Direct chat behavior

For direct chat, the conversation still belongs to a Cognis agent.

If that agent uses `runtime.type = "claude_code"`:

- turn execution runs through Claude Code on the executor
- Cognis streams normalized events to the UI
- Cognis persists orchestration metadata and session lineage
- Claude runtime session IDs are stored as runtime metadata, not as Cognis's
  primary session identity

Claude v1 scope restriction:

- direct chat is supported for active sessions
- controller-restart recovery for direct-chat Claude runs requires durable
  `runtime_runs` support; until that exists, direct chat should be behind a
  feature flag or restricted to non-durable mode

### Workflow step behavior

Workflow steps remain controller-owned.

For a step assigned to a Claude-backed agent:

- Cognis builds the step input and definition-of-done
- Cognis starts or resumes a Claude runtime session for the step
- Cognis receives progress and question events
- Cognis evaluates the final structured step result and decides whether to
  advance, retry, or fail the step

Claude Code does not own workflow transitions.

Claude v1 target scope:

- full support for background task/workflow steps
- direct chat support only once runtime recovery guarantees match workflow
  expectations

### Structured completion contract

Native Cognis steps require explicit `step_complete`. Claude Code runtime does
not need to literally call that tool.

Instead, Claude-backed workflow steps must return a normalized structured step
result:

```json
{
  "summary": "Implemented the API endpoint and tests.",
  "artifacts": [...],
  "changed_files": [...],
  "followups": [...],
  "status": "completed"
}
```

The runtime adapter converts this into the controller's `StepOutput` shape so
the existing evaluator and workflow engine continue to work.

## Reliability Requirements

Claude Code runtime integration must be reliable enough for long-running task
orchestration, not only direct interactive chat.

### Required guarantees

1. **Session resume**

   Cognis must be able to resume a paused or interrupted Claude session using
   runtime metadata stored in Cognis plus native Claude session state stored on
   the executor.

2. **Cancellation**

   User and system cancellation must terminate the active Claude run, update
   task/step status, and clean up runtime resources.

3. **Stuck session detection**

   The controller and executor must detect when Claude appears stuck, for
   example:

   - no event output for longer than heartbeat threshold
   - process still alive but no progress
   - waiting on a hidden prompt or blocked subprocess
   - executor disconnected mid-run

4. **Orphan cleanup**

   When a controller or executor dies, orphaned runtime processes and stale
   leases must be discovered and either resumed or terminated safely.

5. **Question durability**

   If Claude asks a question during planning or implementation, the pending
   question must survive restarts and reconnects.

### Runtime lease model

Each active external runtime run should have a lease with:

- `runtime_run_id`
- `runtime_type`
- `executor_id`
- `acting_user_email`
- `agent_id`
- `conversation_id | null`
- `task_id | null`
- `step_run_id | null`
- `external_session_id`
- `status`
- `last_event_at`
- `last_heartbeat_at`
- `pending_request_id | null`

This lease must be persisted in Cognis storage as a first-class metadata row,
for example:

```sql
CREATE TABLE runtime_runs (
    runtime_run_id TEXT PRIMARY KEY,
    runtime_type TEXT NOT NULL,
    acting_user_email TEXT NOT NULL REFERENCES users(email),
    agent_id TEXT NOT NULL,
    executor_id TEXT NOT NULL REFERENCES executors(executor_id),
    conversation_id TEXT,
    task_id TEXT,
    step_run_id TEXT,
    status TEXT NOT NULL,
    external_session_id TEXT,
    external_run_id TEXT,
    lease_owner TEXT,
    last_event_seq BIGINT NOT NULL DEFAULT 0,
    last_event_at TIMESTAMP WITH TIME ZONE,
    last_heartbeat_at TIMESTAMP WITH TIME ZONE,
    pending_request_id TEXT,
    pending_request_kind TEXT,
    recovery_policy JSON,
    metadata JSON,
    created_at TIMESTAMP WITH TIME ZONE NOT NULL,
    updated_at TIMESTAMP WITH TIME ZONE NOT NULL
);
```

Additional integrity requirements:

- `agent_id` may reference either a DB-backed agent or a `system:*` agent ID;
  do not enforce a DB foreign key that would reject system agents
- at most one non-terminal `runtime_run` may exist for the same `step_run_id`
- at most one non-terminal direct-turn `runtime_run` may exist for the same
  `(conversation_id, agent_id, acting_user_email)` key
- `lease_owner` changes must be compare-and-swap style to avoid split brain

Required semantics:

- one active lease owner at a time
- idempotent `runtime.start` and `runtime.resume`
- monotonic event sequence per `runtime_run_id`
- controller stores `last_event_seq` after successful projection
- replay after reconnect starts from `last_event_seq + 1`
- completion and cancellation handling must be idempotent

### Stuck detection policy

The controller should monitor:

- event heartbeat timeout
- wall-clock timeout for current step/turn
- repeated identical status with no forward progress
- executor disconnects while a runtime lease is active

Recovery actions:

1. probe executor via `runtime.status`
2. if recoverable, keep lease and continue waiting
3. if waiting on human input, persist as paused and surface notification
4. if unrecoverable, cancel and mark failed with structured reason
5. optionally offer retry/resume using the same Claude session when safe

Controller restart semantics:

- task/workflow runs backed by `runtime_runs` must be reclaimable after
  controller restart
- pending questions and approvals tied to a `runtime_run_id` must remain
  resolvable after restart
- direct-chat recovery may be feature-flagged until the same guarantees are
  proven for conversation turns

## Questions and Notifications

All runtime questions that require human input must be normalized into
Cognis notifications.

Question sources:

- planning clarification
- missing credentials or auth follow-up
- approval/escalation from Intaris-backed action
- runtime-specific question emitted by Claude Code integration

Flow:

1. runtime emits `question` or `permission_request`
2. Cognis persists a notification
3. `PauseWaiter` blocks the run without consuming active step capacity
4. user responds in UI/API/channel
5. Cognis calls `runtime.respond`
6. runtime resumes

User-visible states must distinguish at least:

- `running`
- `waiting_for_input`
- `waiting_for_approval`
- `recovering`
- `stuck`
- `failed`

Normalized conversation history and user-facing transcript views are derived by
projection. Raw runtime trace, approvals, and low-level tool events remain
available for support and audit but are not the product transcript directly.

## Data Ownership

### Cognis owns

- agent identity
- workflow definitions and step status
- task state and delivery
- runtime orchestration metadata
- notification persistence
- audit/event projection into Cognis conversations

### Claude Code owns

- native Claude runtime session state
- native Claude auth material inside the isolated runtime root
- native Claude local transcript/session data used for resume

### Intaris owns

- guardrail evaluation and approvals
- policy/audit artifacts tied to guarded actions

### Mnemory owns

- persistent memory records and runtime recall behavior outside transient
  executor state

## Initial Runtime Types

### `native`

Current Cognis agent loop. Default runtime.

### `claude_code`

Executor-hosted native Claude Code runtime using Cognis-managed isolated config,
native Claude auth, and existing Intaris/Mnemory integrations.

Additional v1 constraints:

- supported primarily on subprocess and remote WebSocket executors
- internal/system control-plane agents remain `native`-only by default
- background workflow/task execution is the primary supported path
- direct chat rollout should stay behind a feature flag until restart recovery,
  auth UX, and stuck detection are validated in production-like tests

### `opencode`

Future executor-hosted runtime following the same contract.

## Non-goals for Claude v1

- replacing Cognis workflows with Claude-owned process control
- making Claude Code the source of truth for task state
- requiring the controller to shell out to `claude`
- forcing all Cognis agents to use Claude Code
- full parity with every Claude Code remote-control or desktop feature on day 1

## Implementation Phases

1. add first-class runtime config to agents and API models
2. add runtime capability metadata to executors
3. add persisted `runtime_runs` schema and runtime event sequencing
4. add runtime RPC alongside tool RPC
5. wrap current behavior as `native` runtime adapter
6. implement executor-side Claude runtime host
7. implement controller-side Claude runtime adapter
8. integrate background workflow/task execution first
9. add stuck detection, heartbeat monitoring, restart recovery, and orphan cleanup
10. enable direct chat once durability and UX requirements are met

## Open Design Decisions Before Build-Out

1. exact approved trust boundary for Claude↔Intaris and Claude↔Mnemory native integrations beyond the v1 baseline defined here
2. runtime auth UX for login, expiry, revocation, and executor migration
3. feature-flag policy for direct chat vs workflow-only rollout
4. metrics and alerting required for production runtime hosting
