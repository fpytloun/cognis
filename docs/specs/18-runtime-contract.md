# Cognis: Runtime Contract

## Purpose

This document defines the runtime contract that all Cognis agent runtimes must
satisfy. The current runtimes are:

- `native` — the existing Cognis controller-driven agent loop
- `claude_code` — a Claude Code + Intaris backed runtime hosted on an executor

The goal is to keep upper layers runtime-agnostic:

- conversations
- channels
- slash commands
- tasks
- workflows
- delegation
- notifications
- worktree policy
- delivery

Those layers must depend only on this contract, not on runtime-specific logic.

## Terminology

| Term | Meaning |
|---|---|
| **conversation** | User-visible long-lived chat/thread identity |
| **cognis_session** | Cognis orchestration epoch within a conversation |
| **runtime_session** | Opaque durable runtime-owned session handle |
| **execution** | One active runtime run/attempt within a session or step |
| **step_run** | Workflow-level execution wrapper tracked by Cognis |
| **projection** | Normalized Cognis-visible event stream derived from runtime trace + Cognis overlay events |

## Design Rules

1. Runtimes are low-level execution engines.
2. Cognis owns orchestration and user-facing product behavior.
3. Tool policy semantics are Cognis-owned and runtime-neutral.
4. Runtime-specific event traces are never exposed directly as the product transcript without normalization.
5. Runtimes may differ internally, but must satisfy the same upward contract.

## Runtime Boundary

### Cognis-owned responsibilities

- conversation identity and routing
- channel delivery
- task and workflow state
- notification persistence and `PauseWaiter`
- slash command handling
- worktree and branch policy
- executor placement
- runtime selection from agent definition
- normalized transcript/projection consumption

### Runtime-owned responsibilities

- executing a direct turn or workflow step
- maintaining runtime-native session state
- streaming low-level runtime events
- translating Cognis tool/policy semantics to native enforcement
- exposing structured terminal outcomes

## Runtime Kinds

### `native`

The existing Cognis controller-driven loop. The runtime session is the Cognis
session itself and tool execution flows through Cognis ToolRouter + executor.

### `claude_code`

Executor-hosted Claude Code using native Claude auth and native Intaris
integration. Claude runtime sessions are durable and opaque. Intaris stores the
raw low-level runtime trace. Cognis consumes a normalized projection.

## Capability Model

Capabilities are product/test oriented, not vendor oriented.

```python
class RuntimeCapabilities(BaseModel):
    durable_session: bool
    streaming_output: bool
    tool_loop: bool
    pause_resume: bool
    cancel: bool
    usage_reporting: bool
    structured_terminal_outcome: bool
    error_taxonomy: bool

    native_compaction: bool = False
    background_execution: bool = False
    reasoning_summary: bool = False
    structured_output: bool = False
```

Mandatory for first-class direct-chat parity:

- `durable_session`
- `streaming_output`
- `tool_loop`
- `pause_resume`
- `cancel`
- `usage_reporting`
- `structured_terminal_outcome`
- `error_taxonomy`

If a runtime does not satisfy these, it cannot claim first-class parity.

## Session Boundary Model

### Base hierarchy

```text
conversation
  -> cognis_session
     -> runtime_session
        -> execution(s)
```

Rules:

- `conversation` is the user-visible chat/thread identity
- `cognis_session` is the Cognis orchestration epoch used for reset/compaction boundaries and session lifecycle
- `runtime_session` is the durable low-level runtime handle
- `execution` is one active turn/attempt/resume within a runtime session

### MVP boundary rule

For MVP external runtimes:

- one active `cognis_session` maps to one active `runtime_session`
- a new `runtime_session` is created only on:
  - conversation reset/new session
  - compaction rollover
  - explicit isolation boundary (future fork-style runtimes remain deferred)
  - delegated child-session creation
  - workflow step isolation boundary

This preserves the current Cognis product model while keeping runtime session
continuation simple.

### Workflow mapping

For workflow steps:

- each executable step owns a `step_run`
- the `step_run` references exactly one active runtime session in MVP
- retries may create new `execution` attempts inside the same runtime session
- if the runtime session becomes unrecoverable, the adapter may rotate to a new runtime session and must record that rollover explicitly

## Lifecycle Contract

All runtimes must support the following lifecycle operations.

```python
class RuntimeAdapter(Protocol):
    async def create_or_resume_runtime_session(
        self,
        request: RuntimeSessionRequest,
    ) -> RuntimeSessionHandle: ...

    async def submit_input(
        self,
        request: RuntimeInputRequest,
    ) -> RuntimeExecutionHandle: ...

    async def stream_events(
        self,
        request: RuntimeEventStreamRequest,
    ) -> AsyncIterator[RuntimeEvent]: ...

    async def pause(
        self,
        request: RuntimePauseRequest,
    ) -> RuntimePauseResult: ...

    async def resume(
        self,
        request: RuntimeResumeRequest,
    ) -> RuntimeExecutionHandle: ...

    async def cancel(
        self,
        request: RuntimeCancelRequest,
    ) -> RuntimeCancelResult: ...

    async def close(
        self,
        request: RuntimeCloseRequest,
    ) -> RuntimeCloseResult: ...
```

### Required properties

- operations must be idempotent where retry is plausible
- each execution has a stable `execution_id`
- each runtime session has a stable `runtime_session_id`
- each event has a stable `event_id` or monotonic `seq`
- terminal outcomes must be explicit, not inferred from silence

## Event Model

Runtimes emit low-level events. Cognis consumes these and produces normalized
projected events for the rest of the system.

### Raw runtime event envelope

```python
class RuntimeEvent(BaseModel):
    event_id: str
    runtime_type: str
    runtime_session_id: str
    execution_id: str
    seq: int
    source: str                # runtime | intaris | cognis_overlay
    kind: str
    payload: dict[str, Any]
    timestamp: datetime
    caused_by: str | None = None
```

### Minimum raw event kinds

- `input_received`
- `message_delta`
- `message_completed`
- `tool_proposed`
- `tool_decision`
- `tool_started`
- `tool_completed`
- `question`
- `approval_required`
- `progress`
- `usage`
- `paused`
- `resumed`
- `completed`
- `failed`
- `cancelled`

### Projection rules

Upper layers do not consume raw runtime events directly. They consume a
normalized projection with deterministic ordering and provenance.

```python
class ProjectedConversationEvent(BaseModel):
    event_id: str
    source: str                # runtime | cognis_overlay
    kind: str
    conversation_id: str
    cognis_session_id: str
    runtime_session_id: str | None = None
    execution_id: str | None = None
    caused_by: str | None = None
    visible_to_user: bool = True
    ordering_key: str
    payload: dict[str, Any]
    timestamp: datetime
```

Projection requirements:

- deterministic reconstruction after restart
- idempotent replay
- stable causal linkage between overlay events and runtime events
- user-visible transcript separated from raw audit trace

## Source-of-Truth Rules

### Native runtime

- Cognis and Intaris together remain the existing source-of-truth model
- normalized conversation history continues to be read from Intaris events

### Claude runtime

- Intaris is the canonical durable store for the raw low-level runtime trace
- Cognis does not duplicate that raw Claude session log in a second low-level store
- Cognis may persist overlay metadata and derived projection checkpoints
- user-visible history is a normalized projection, not a raw replay

### Overlay events

Overlay events are Cognis-owned events that do not originate inside the raw
runtime trace, for example:

- task status updates
- workflow transitions
- delegation started/completed
- PR opened
- slash command effects
- delivery notices

For external runtimes, overlay events should ideally be durably recorded in
Intaris as a separate source stream so transcript reconstruction can happen
from one durable backend.

## Tool Contract

Tool calls are part of the runtime contract.

### Shared semantics owned by Cognis

- inventory selection
- allow / deny / evaluate permission model
- non-bypassable tool categories
- timeout policy
- cancellation policy
- credential/secret availability
- workflow-level question permission policy

### Runtime responsibilities

Each runtime translates those shared semantics into native enforcement.

#### Native runtime

- Cognis exposes tool definitions directly
- Cognis routes evaluation and execution

#### Claude runtime

- Cognis produces an effective runtime tool policy
- runtime translates that into Claude + Intaris configuration/session policy
- approvals and denials must remain replay-safe and fail-closed where the shared Cognis policy requires it

### Required tool-policy semantics

The following meanings must remain equivalent across runtimes:

- `allow`: tool may proceed without user approval if no stronger policy blocks it
- `deny`: tool must not execute
- `evaluate`: guardrails evaluation decides allow/deny/escalate
- `non_bypassable`: runtime may not skip guardrails even if general policy would otherwise allow
- `timeout`: runtime must surface timeout as explicit failure or cancellation
- `cancel`: runtime must stop in-flight execution if supported, otherwise return a structured non-cancellable error

## Terminal Outcomes

All executions end with exactly one terminal outcome:

- `completed`
- `failed`
- `cancelled`

Each terminal outcome must include:

- `execution_id`
- `runtime_session_id`
- `status`
- `summary`
- `usage` when available
- `structured_result` when available
- `error_code` and `error_category` for failures

## Error Taxonomy

Minimum categories:

- `runtime_unavailable`
- `runtime_protocol_error`
- `runtime_policy_blocked`
- `runtime_question_timeout`
- `runtime_tool_timeout`
- `runtime_cancelled`
- `runtime_unrecoverable_session`
- `runtime_projection_error`

These categories must be stable enough for task/workflow logic and user-facing
messaging.

## Slash Command Contract

Slash commands are Cognis-owned.

Runtime adapters must support the resulting operations, but slash command
parsing and permissioning stay above the runtime layer.

Required parity targets:

- `/approve`
- `/deny`
- `/new`
- `/reset`
- `/compact`

The runtime adapter must define what each command means for that runtime. For
example, `/compact` may require runtime-session rollover for an external
runtime while keeping the same user-facing conversation.

## Recovery and Replay

Minimum guarantees:

- deterministic event replay from durable state after controller restart
- resume from latest projected checkpoint
- no duplicate user-visible events after retry/reconnect
- runtime session recovery status is surfaced explicitly to users/operators

Projection engines must store checkpoints containing at least:

- last projected raw event `seq`
- last emitted projected `event_id`
- active `runtime_session_id`
- active `execution_id`

## Conformance

No runtime is considered first-class until it passes parity tests for:

- direct chat
- delegation
- task-backed workflow execution
- notifications/questions/approvals
- slash commands
- cancellation and replay
- normalized transcript reconstruction

See `19-runtime-implementation-plan.md` for the rollout and test plan.
