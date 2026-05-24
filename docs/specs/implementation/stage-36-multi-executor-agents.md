# Stage 36: Multi-Executor Agents

## Status

DONE

## Goal

Allow an agent to use multiple assigned executors while keeping one primary
executor binding model. Agents can run one-off tool calls on a specific
assigned executor, switch active execution to another assigned executor, and
recover deterministically when an executor goes offline — without the
controller silently re-routing work.

## Non-Goals

- No executor aliases. Tool calls and switching use real `executor_id` values.
- No automatic migration of work to an arbitrary executor when a required
  executor is unavailable.
- No portable binary or package split work in this stage; that is Stage 37.
- No Docker/Kubernetes managed executor backend.

## Core Invariants

These are hard rules. The implementation MUST NOT violate them.

1. **Single binding rule.** A conversation is bound to exactly one
   `active_executor_id`. The controller picks one usable primary executor
   when the conversation first needs an executor and persists it to the
   conversation row. After that, the controller never changes it for any
   reason. The only mutators are:
   - the agent calling the `switch_executor` controller tool, or
   - the user issuing the `/executor <id>` slash command.

2. **Primaries are auto-eligible. Additional executors are not.** The
   controller picks the initial active executor only from the agent's
   primary set. Additional executors are reachable exclusively through
   explicit `target_executor=<id>` per-call routing or an explicit
   `switch_executor`.

3. **No silent re-routing.** When the active executor is offline,
   unassigned, disabled, or otherwise unusable, the controller does NOT
   pick a replacement and does NOT cancel the turn. Tool calls without
   `target_executor` return a factual `is_error=True` tool result naming
   the active executor and its state. The agent decides whether to
   retry, call `switch_executor`, or stop. A tool call with
   `target_executor=X` against an unavailable X returns the same kind
   of factual error.

4. **Active-executor immutability across the conversation.** The active
   executor binding persists across LLM turns, workflow steps, and
   restarts. It changes only via `switch_executor` or `/executor`. The
   immutability extends within a single LLM response: every dispatched
   tool call uses either the explicit `target_executor` of that call or
   the conversation's current `active_executor_id` snapshot, never a
   value computed mid-batch.

5. **Agent always knows where its tool will run.** Every tool dispatch
   resolves to either (a) an executor the LLM explicitly named via
   `target_executor` in that very tool call, or (b) the
   `active_executor_id` value advertised in the environment block of the
   request that produced the tool call. There is no third source.

6. **Workflow same-executor invariant.** All steps of a single task run
   on the same executor by default. Each step creates its own
   conversation, but the task-level pin (`tasks.active_executor_id`)
   carries forward, so the controller does not re-pick between steps.
   The agent or user can still call `switch_executor` / `/executor`
   mid-workflow; the new binding is propagated to the task pin and
   inherited by all subsequent step conversations.

## Design Summary

Agent `execution.executor_id` and `execution.executor_selector` remain the
primary executor binding. A primary selector may resolve to multiple
executors; all usable matches form the primary executor set.

Agents may also define `execution.additional_executors`, where each binding
is either an explicit `executor_id` or an `executor_selector`, with an
optional description. Additional executors are exposed to the agent as
assigned secondary hands. They are never auto-selected by the controller.

The controller resolves an executor pool for each turn or workflow step:

- primary executor IDs (one or many)
- additional executor IDs (zero or many)
- active executor ID (persisted on the conversation)
- availability and observed tool inventory for every resolved executor
- environment snapshot for connected executors

Executor-native tools include a `target_executor` parameter overlay when
more than one usable assigned executor offers them. The controller strips
this parameter before evaluating guardrails or sending the tool call to the
executor; the target identity is passed to guardrails as separate metadata.

The `switch_executor` controller tool changes the conversation's active
executor for all subsequent executor-routed calls (without `target_executor`)
in this and any future turn, until the next switch.

The `/executor` slash command lets the user inspect the pool and force a
switch. It uses the same shared backend helper as the `switch_executor`
tool.

## Availability Semantics

An executor is **usable** only when:

- DB `status == "active"` (not disabled)
- runtime state is `active` or `degraded`
- desired and applied config versions match
- remote executors have a live ready connection
- deployment policy allows the executor type

A specific tool routed to an executor additionally requires:

- the tool is in the executor's `enabled_tools`/`enabled_tool_groups`
- the tool is observed in the executor's runtime tool inventory

Unavailable executors remain visible in context with factual state. The
controller must not speculate about why an executor is offline.

If `switch_executor` (or `/executor <id>`) targets an unavailable executor
or an executor not assigned to the agent, the call fails and the active
executor is unchanged.

If a tool call (with or without `target_executor`) targets an unavailable
executor, the tool is not evaluated by Intaris and no RPC is sent. The
result is a factual `is_error=True` tool result naming the executor and
its state.

## Workstreams

### 36.1 Data Model and API

- Add typed Pydantic `ExecutorBinding` for additional-executor entries.
- Validate agent create/update: each binding has either `executor_id` or
  `executor_selector` (not both, never neither, selector must be non-empty).
  Reject `additional_executors` entries that collide with the primary
  `executor_id`.
- Add `active_executor_id` column on `conversations` (Alembic + bootstrap
  helper).
- Update effective-tools API response to a list shape with per-executor
  state and per-tool `available_on` membership.

### 36.2 Executor Pool Resolution

- Add `ExecutorPool`, `ResolvedExecutorTarget`, and `ExecutorAvailability`
  in a new module `cognis/core/executor_pool.py`.
- Resolve a primary set + additional set per agent. Primary selector
  matching N≥1 usable executors yields a primary set of size N.
- Deduplicate by `executor_id`; primary membership wins on overlap.
- Return both usable and unusable resolved targets with factual state for
  context and UI.

### 36.3 Active Executor State

- Persist `conversation.active_executor_id` AND `tasks.active_executor_id`.
  The conversation pin is authoritative for chat conversations; the task
  pin is authoritative for multi-step task workflows where each step
  creates its own conversation row.
- Read at runtime resolution time. The runtime factory checks the
  conversation pin first; if unset, it falls back to the task pin. The
  resolver passes the chosen value into the resolver to override
  `execution.get("executor_id")` for runtime selection.
- The controller initialises `active_executor_id` once, the first time the
  runtime is built for that conversation/task, choosing from the usable
  primary set (preferring `runtime_state == active`, then `degraded`,
  then sorted by `executor_id`). Both the conversation pin and the task
  pin are seeded atomically.
- New step conversations created by the workflow engine inherit the
  task pin at creation time so each step starts already pinned.
- `switch_executor` and `/executor` issued from a task-step conversation
  also update the task pin so the change carries forward to subsequent
  steps.
- The controller never re-picks; if the persisted active is offline or no
  longer assigned, factual errors flow through to tool results until the
  agent or user acts.

### 36.4 Tool Routing

- Add `target_executor` schema overlay to executor-routed tools when more
  than one usable assigned executor exposes them.
- Strip the controller-only `target_executor` parameter from tool
  arguments before guardrails evaluation and before RPC dispatch.
- Pass the target executor id to guardrails as separate metadata.
- Resolve the target connection via lazy lookup (the active connection is
  established eagerly at runtime build; additional connections are looked
  up on demand via `WebSocketExecutorProvider.get_connection(id)` and
  in-process equivalents).
- Return a factual `is_error=True` result when the target is unassigned,
  unusable, or does not expose the requested tool. Do not call Intaris and
  do not RPC the executor.

### 36.5 `switch_executor` Controller Tool

- Add controller-handled `switch_executor` tool.
- Validate target is in the agent's assigned set and currently usable.
- On success: update `conversations.active_executor_id` and return
  environment + available tools for the new active. The environment block
  reflects the change on the next LLM turn.
- On failure: leave active unchanged. Return factual error.

### 36.6 `/executor` Slash Command

- Bare `/executor` displays a system message in the chat: active executor
  details (id, type, primary/additional, state, environment summary, tools
  count) and the full assigned pool with their states.
- `/executor <executor_id>` performs a switch using the same shared
  backend helper as `switch_executor`. Same validation and error model.
- Updates `conversations.active_executor_id` on success. The agent sees
  the new active in the next turn's environment block.
- Listed in `/help`.

### 36.7 Context and Reminders

- Update environment context to list primary executors, additional
  executors (with descriptions), per-executor availability, and the active
  executor.
- Inject a non-primary-active reminder on every LLM turn while the active
  executor is in the additional set. No reminder is needed when the
  active is in the primary set, because primaries are by definition
  legitimate hosts for the conversation.
- No forced-switch reminder. The controller never forces a switch.

### 36.8 No Controller-Initiated Cancellation

The previous draft of this spec proposed cancelling a turn when an
"executor required for the work" went offline. That entire pathway is
removed. Tool calls return factual errors; the agent decides what to do.
The user-initiated `/stop` and UI cancel paths are unaffected.

### 36.9 UI

- Keep the existing primary direct selector and label selector on the
  agent form.
- Add a repeater for additional executors. Each row supports either a
  direct executor select or a label selector textarea, plus an optional
  description.
- Show resolved pool and per-executor state (active, degraded, offline,
  stale, blocked, reconfiguring, disconnected, policy-denied,
  missing-tool).
- Update effective-tools preview to expose per-executor state and per-tool
  availability.
- Show an active-executor badge on conversations when the active is in
  the additional set.

### 36.10 Tests

- Primary explicit executor still behaves as before.
- Primary selector may match multiple executors (no longer raises).
- Additional explicit and selector bindings resolve correctly.
- `target_executor` routes one call without changing active executor.
- `switch_executor` changes active executor only when target is assigned
  and usable; persists on the conversation.
- `/executor` slash command: bare form returns a status display; argument
  form delegates to the same helper as `switch_executor`.
- Offline target executor fails before guardrails/RPC.
- Active executor going offline does NOT auto-fallback and does NOT
  cancel — tool calls return factual errors.
- Reminder absent when active is primary, present when active is
  additional.
- Validation: binding with both/neither id and selector rejected.

## Acceptance Criteria

- Agents can assign primary and additional executors through API and UI.
- Model context accurately presents executor IDs, descriptions,
  availability, active executor, and primary membership.
- Executor-routed tools support one-off `target_executor` routing for
  every assigned executor that exposes the tool.
- `switch_executor` tool and `/executor` slash command both update the
  conversation's active executor through a single shared helper.
- Offline/unhealthy executors produce factual `is_error=True` tool
  results. The controller never silently re-routes work and never
  cancels turns due to executor state.
- Existing single-executor agents keep current behaviour.
