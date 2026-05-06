# Stage 36: Multi-Executor Agents

## Status

PLANNED

## Goal

Allow an agent to use multiple assigned executors while keeping one primary
executor binding model. Agents can run one-off tool calls on a specific assigned
executor, switch active execution to another assigned executor, and recover
deterministically when a remote executor goes offline.

## Non-Goals

- No executor aliases. Tool calls and switching use real `executor_id` values.
- No automatic migration of work to an arbitrary executor when a required
  executor is unavailable.
- No portable binary or package split work in this stage; that is Stage 37.
- No Docker/Kubernetes managed executor backend.

## Design Summary

Agent `execution.executor_id` and `execution.executor_selector` remain the
primary executor binding. A primary selector may resolve to multiple executors;
all usable matches form the primary executor set.

Agents may also define `execution.additional_executors`, where each binding is
either an explicit `executor_id` or an `executor_selector`, with an optional
description. Additional executors are exposed to the agent as assigned secondary
hands, but they are not primary executors.

The controller resolves an executor pool for each turn or workflow step:

- primary executor IDs
- additional executor IDs
- active executor ID
- availability and observed tool inventory for every resolved executor
- environment snapshot for connected executors

Executor-native tools may include `target_executor` to route a single call to a
specific assigned executor. The controller strips this parameter before sending
the tool call to the executor.

The `switch_executor` controller tool changes the active executor for subsequent
executor-routed calls in the current turn or workflow step.

## Availability Semantics

An executor is usable only when:

- DB `status == "active"`
- runtime state is `active` or `degraded`
- desired and applied config versions match
- remote executors have a live ready connection
- the requested tool is enabled and observed on that executor
- deployment policy allows the executor type

Unavailable executors remain visible with factual state. The controller must not
speculate about why an executor is offline.

If `switch_executor` targets an unavailable executor, the tool fails and the
active executor is unchanged.

If a tool call targets an unavailable executor, the tool is not evaluated by
Intaris and no RPC is sent. The result says the target executor state and that
the tool was not executed.

If the active executor becomes unavailable, the controller switches back to a
usable primary executor and injects a hidden reminder. If the unavailable
executor is required for the current work, the controller notifies the user and
cancels the turn instead of continuing on a different host.

## Workstreams

### 36.1 Data Model and API

- Extend agent execution config parsing with `additional_executors`.
- Add Pydantic models for executor bindings and resolved executor targets.
- Update agent create/update validation: a binding may use `executor_id` or
  `executor_selector`, not both.
- Update effective-tools API response to include resolved executor targets,
  primary membership, availability, and tool availability per executor.

### 36.2 Executor Pool Resolution

- Replace single-executor resolution in runtime setup with executor pool
  resolution.
- Preserve current single-executor behavior as the degenerate case.
- Resolve primary selector matches into a primary set instead of requiring a
  single match.
- Expand additional selector matches into additional executor targets.
- Deduplicate executor IDs; primary membership wins.

### 36.3 Active Executor State

- Add `active_executor_id` to turn/step runtime context.
- Select a usable primary executor at turn/step start.
- Keep active primary executor when it remains usable.
- Prefer `active`, then `degraded`, then sorted `executor_id` when choosing a
  replacement primary executor.

### 36.4 Tool Routing

- Add `target_executor` schema overlay to executor-routed tools when more than
  one executor can run them.
- Strip controller-only `target_executor` before guardrails evaluation and RPC
  dispatch, while passing target metadata separately to guardrails.
- Route executor-native, local MCP, and executor-backed skill tools to the
  selected target executor.
- Return factual controller-side errors when the selected target is unavailable
  or lacks the requested tool.

### 36.5 `switch_executor` Tool

- Add controller-handled `switch_executor` tool.
- Validate target membership and availability.
- Return environment and available tool summary on success.
- Leave active executor unchanged on failure.

### 36.6 Context and Reminders

- Update environment context to list primary executors, additional executors,
  descriptions, availability, and active executor details.
- Inject a non-primary active executor reminder.
- Inject a forced-switch reminder when the previously active executor becomes
  unavailable and the controller returns to primary.
- Ensure reminders are factual and do not infer why an executor is unavailable.

### 36.7 User Notification and Cancellation

- Detect when requested work requires an unavailable executor.
- Notify the user with executor ID and factual state.
- Cancel the turn instead of running required work on a different executor.

### 36.8 UI

- Keep the existing primary direct selector and label selector on the agent
  page.
- Add secondary executor rows supporting direct executor or label selector plus
  description.
- Show resolved targets and live state: active, degraded, offline, stale,
  blocked, reconfiguring, disconnected, policy-denied, and missing-tool.
- Update effective tools preview to show which executors can run each tool.

### 36.9 Tests

- Primary explicit executor still behaves as before.
- Primary selector may match multiple executors.
- Additional explicit and selector bindings resolve correctly.
- `target_executor` routes one call without changing active executor.
- `switch_executor` changes active executor only when target is usable.
- Offline target executor fails before guardrails/RPC.
- Active additional executor going offline forces switch to primary.
- Required offline executor notifies user and cancels turn.
- Degraded executor is usable and represented as degraded in metadata.
- Stale, blocked, reconfiguring, disconnected, and policy-denied executors are
  rejected for tool execution.

## Acceptance Criteria

- Agents can assign primary and additional executors through API and UI.
- Model context accurately presents executor IDs, descriptions, availability,
  active executor, and primary membership.
- Executor-routed tools support one-off `target_executor` routing.
- `switch_executor` works and fails safely.
- Offline/unhealthy executors produce factual failures and never silently route
  required work elsewhere.
- Existing single-executor agents keep current behavior.
