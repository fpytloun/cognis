# Stage 6: Agent Loop + Workflow Engine

**Status**: DONE

## Implementation Notes

- Agent loop engine in `core/agent_loop.py` (~1150 lines): streaming LLM
  response accumulation, tool call interception, controller tool handling,
  cancel event support, per-session async locks, compaction triggering.
- Workflow engine in `core/workflow_engine.py`: step sequencing with
  evaluation gates, review loops, max-attempt limits, pause/resume via
  `PauseWaiter`, task result delivery to conversations.
- Task queue with priority-based picking, DAG dependency resolution,
  capacity management, and stale task recovery on startup.
- Step evaluator: semantic completion evaluation via LLM with
  approve/revise/fail outcomes.
- Event bus with topic-based pub/sub and global subscribers.
- Workflow registry with bundled system workflows (direct, research,
  code-with-review) and user workflow CRUD.
- 102 unit tests added covering all domain models and core logic.

**Repo**: `cognis`
**Depends on**: Stage 4 (executor + tools) AND Stage 5 (orchestration core)
**Estimated effort**: 7-10 days (largest stage — core of the system)

## Objective

Implement the agent loop (step runner), workflow engine (step orchestration),
and step evaluator (completion verification). After this stage, the system
can run complete chat turns, execute multi-step workflows with evaluation
and review loops, handle gate/pause steps, and manage background tasks
with step-by-step progress.

This is the core of Cognis. See `docs/specs/14-workflow-engine.md` for the
full design.

## Deliverables

### 1. Agent Loop Engine (Step Runner)

- `cognis/core/agent_loop.py`
  - Runs a single step session as a full agentic loop:
    1. Receive step objective and assemble step input context
       (see Step Input Context Assembly below)
    2. Context assembly (parallel fetches via ContextAssembler)
    3. LLM call (streaming via LLMProvider)
    4. Process response:
       - Text → stream to client
       - Tool call → Tool Router dispatch → feed result back → loop
       - `step_complete` → signal completion to workflow engine
       - `step_request_input` → pause and return to caller (if allowed)
       - Orchestration tool (delegate, spawn_worker, fork) → controller operation
    5. Continue until `step_complete` is called or limits hit
    6. Finalize step:
       - Record events to Intaris (with idempotency_key)
       - Append events to session cache
       - Remember to Mnemory (via retry queue)
       - Check compaction threshold
  - If LLM stops without `step_complete`: re-prompt once, then treat as
    failed attempt
  - Maximum tool calls per step (from agent settings)
  - Step duration timeout
  - Runaway detection (repeated tool calls, no progress)

### 2. Controller-Injected Step Tools

- `step_complete(summary, outputs, claims)` — always injected into run steps
- `step_request_input(question, options, context)` — injected only when
  workflow interaction mode is `step_requests` AND the current step has
  `allow_questions=true`; resumes the same step session
- `step_todo_write(todos)` — step-scoped cognitive aid, survives compaction
- `step_todo_list()` — read current step todos

These are controller tools, not executor tools. The controller intercepts
them before they reach the executor.

### 3. Task Queue

- `cognis/core/task_queue.py`
  - Postgres-backed priority queue with `SELECT ... FOR UPDATE SKIP LOCKED`
  - `LISTEN/NOTIFY` for low-latency wakeups, polling fallback
  - Task lifecycle: draft → queued → ready → running → paused → completed/failed/cancelled
  - Dependency resolution: when a task completes, re-evaluate all dependents;
    if all required deps met → transition to `ready`
  - DAG validation on dependency creation (reject cycles)
  - Concurrency enforcement (single unified capacity model for MVP):
    - Max active steps globally (from settings)
    - Max active steps per agent (from agent config)
    - Paused tasks release capacity
  - Priority: higher value picked first, FIFO within same priority
  - Scheduled tasks: `scheduled_for` field, not picked until time arrives
  - Result delivery: route task result back to source (chat conversation,
    API, scheduler record)

### 4. Workflow Engine

- `cognis/core/workflow_engine.py`
  - Orchestrates step execution for a task:
    1. Resolve workflow from task
    2. For each step:
       a. Resolve agent (from step_agent_overrides or task's owning agent)
       b. If run step: spawn step session, run agent loop
       c. If gate step: pause, send gate event to caller, wait for response
       d. Collect step output
       e. Run step evaluator (if configured)
       f. Handle evaluation result (advance, revise, fail)
       g. Handle review loops (on_reject → target step with feedback)
       h. Enforce iteration limits (max_attempts, max_loop_iterations)
       i. Push progress events
    3. Collect final result from last step
    4. Return to task / notify caller

### 5. Step Evaluator

- `cognis/core/step_evaluator.py`
  - Semantic completion check via independent LLM call
  - Input: step objective, step inputs, step output + claims, task context
  - Output: {decision: approved|revise|failed, reasoning, feedback}
  - Uses cheap model via routing policy (e.g., `task_type: "evaluator"`)
  - Skeptical by default — tuned to catch premature completion
  - Custom evaluator prompts per step (from workflow definition)
  - Timeout protection (single LLM call, not a loop)

### 6. Workflow Registry

- `cognis/core/workflow_registry.py`
  - Load system workflows on startup (bundled YAML files)
  - CRUD for user workflows (DB-backed)
  - Workflow resolution: by ID, or by classifier match
  - Validation: step references, loop detection, required fields

### 7. LLM Streaming

- Stream tokens from LLMProvider to the caller
- Accumulate full response for event recording
- Handle tool call responses (function calling format)
- Track token usage per step and per workflow run

### 8. Session Locking

- `SessionLock` — one active turn per session at a time
- Async lock keyed by session_id
- Prevents concurrent turns in the same session
- Different sessions and steps run fully concurrently

### 9. Concurrent Run Management

- Manager tracks all active workflow runs and step sessions
- Start/stop by run_id or step_run_id
- Enforce concurrency limits:
  - Max concurrent workflow runs (global)
  - Max concurrent step sessions per run (currently 1, parallel steps later)
  - Max sub-agents per step session
- Clean shutdown: signal all runs, wait for step finalization

### 10. Delegation (Within Steps)

- Three modes:
  - **Agent**: delegate to a different agent (different persona, tools)
  - **Worker**: delegate to same agent (same tools, focused objective)
  - **Fork**: parallel exploration (same context, branched)
- Within a step, the agent can use delegation tools to spawn sub-agents
- Sub-agents are Intaris child sessions under the step's parent session
- Result delivery back to the step session

### 11. Escalation Handling

- When Intaris returns `decision=escalate` for a tool call:
  1. Step session enters waiting state
  2. Push `escalation` event to client
  3. Start countdown timer (from `escalation_timeout_seconds`)
  4. Wait for resolution (approve/deny via WebSocket or REST)
  5. On approve: continue tool execution
  6. On deny: inform LLM of denial
  7. On timeout: deny (configurable default)

### 12. Gate Handling

- When workflow reaches a gate step:
  1. Workflow run enters `paused` state
  2. Push `workflow_gate` event to caller with message, options, context
  3. Wait for gate response (via WebSocket, REST, or main chat agent)
  4. Process response: continue / revise(target) / cancel
  5. Resume workflow

### 13. Event Recording

- Batch events at step finalization:
  - `user_message` (from step prompt injection)
  - `assistant_message` (accumulated from stream)
  - `tool_call` + `tool_result` for each tool execution
  - `step_complete` event
  - `delegation` events for sub-agents
- Include `idempotency_key` for retry safety
- Append same events to session cache after Intaris confirms

### 14. Decision Engine Integration

- Update Decision Engine to also select workflows:
  - Match task description against available workflows' criteria fields
  - Small LLM call: "Which workflow fits this task?"
  - Respect agent's `workflow_selection_mode`
  - Return: workflow_id + confidence

## Acceptance Criteria

### Agent Loop
- [x] Agent loop runs a complete step: context → LLM → tools → step_complete → finalize
- [x] LLM streaming delivers tokens incrementally to caller
- [x] Tool calls route through Intaris evaluate → executor → result → LLM
- [x] Multiple tool calls in a single step work correctly
- [x] `step_complete` tool is intercepted by controller (not sent to executor)
- [x] LLM stop without `step_complete` triggers re-prompt
- [x] `step_request_input` pauses and resumes the SAME step session
- [x] Step-local todo tools work and survive compaction
- [x] Session lock prevents concurrent turns in same session

### Task Queue
- [x] Tasks can be created in draft and queued states
- [x] Draft → queued (submit) transition works
- [x] Batch submit works (multiple drafts at once)
- [x] Dependencies can be added/removed with DAG validation (cycles rejected)
- [x] Dependency resolution: completing a task transitions dependents to ready
- [x] Failed required dependency flags dependent task for user decision
- [x] Queue picks ready tasks by priority (FIFO within same priority)
- [x] Unified capacity limits enforced (global, per-agent)
- [x] Scheduled tasks wait until scheduled_for time
- [x] Result delivery back to source conversation works

### Workflow Engine
- [x] Direct workflow (single step, no evaluation) works for main chat
- [x] Multi-step workflow executes steps in sequence
- [x] Step outputs accumulate and feed into subsequent steps
- [x] Step evaluator runs after `step_complete` and returns approve/revise/failed
- [x] Evaluation rejection triggers step re-attempt with feedback
- [x] `max_attempts` enforced per step
- [x] Review loops between steps work (on_reject → target step)
- [x] `max_loop_iterations` enforced
- [x] `on_exhausted` actions work: continue, fail, gate
- [x] Gate steps pause workflow and send structured options to caller
- [x] Gate response resumes workflow correctly (continue/revise/cancel)
- [x] Workflow selection works (explicit, agent default, automatic classifier)

### Integration
- [x] Background workflow runs don't block main chat
- [x] Step progress events push to client in real-time
- [x] Delegation within steps creates Intaris child sessions correctly
- [x] Escalation pauses step execution and waits for resolution
- [x] Events recorded to Intaris with idempotency key per step
- [x] Session cache updated after step event recording
- [x] Remember dispatched to retry queue after step
- [x] Compaction works within long-running steps
- [x] System workflows (Direct, Research, Code with Review, Creative) functional

### Testing
- [x] Unit tests for workflow engine state machine
- [x] Unit tests for step evaluator
- [x] Unit tests for step completion protocol
- [x] Unit tests for gate handling
- [x] Unit tests for review loop iteration
- [x] `ruff check` and `mypy` clean

## Key References

- `docs/specs/14-workflow-engine.md` — full workflow engine design
- `docs/specs/01-architecture.md` — agent loop, concurrency model
- `docs/specs/03-session-model.md` — turn lifecycle, workflow session mapping
- `docs/specs/04-controller-executor.md` — controller/executor interaction
- `docs/specs/06-tool-system.md` — tool routing, trust model
