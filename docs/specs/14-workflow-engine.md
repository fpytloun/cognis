# Cognis: Workflow Engine

## Purpose

The workflow engine is the core orchestration layer of Cognis. It defines
how tasks are executed: which steps run, in what order, with what
verification, and how the controller manages transitions between them.

This document covers:
- workflow as a portable process template
- step types and execution semantics
- explicit step completion and evaluation
- gate/pause steps
- review loops and iteration limits
- Intaris session mapping
- interaction modes
- in-step cognition aids

## Design Principles

### 1. Controller enforces, agent executes

The workflow engine controls the process. The agent executes work within
each step. The agent cannot skip steps, bypass evaluation, or advance the
workflow on its own. This is the fundamental difference from systems where
the LLM decides its own process.

### 2. Workflow is above agents

A workflow is a portable process template. It does not reference specific
agents, tools, or models. Agent binding happens when a workflow is assigned
to a task. This keeps workflows reusable and shareable.

### 3. Explicit completion, not passive stop

A step is not done because the LLM stopped calling tools. It is done only
when the agent explicitly signals completion via a controller tool. This
prevents premature advancement and makes the completion contract machine-
verifiable.

For external runtimes such as `claude_code`, the runtime adapter may translate
native runtime completion into the controller's structured `StepOutput`
without requiring the external runtime to literally call the native
`step_complete` tool. The controller still decides whether the step is done.

The authoritative lifecycle and capability contract for runtime-backed step
execution is defined in `18-runtime-contract.md`.

### 4. Evaluation is semantic, not mechanical

Step evaluation checks whether the completion claim satisfies the step's
definition of done. It is a semantic check by an independent LLM call, not
a test runner or linter. Testing and review are separate workflow steps.

### 5. Human-in-the-loop is optional

Workflows can run fully autonomously. Caller interaction (gates, questions)
is controlled by workflow configuration. Some workflows are designed for
full autonomy; others include human checkpoints.

### 6. One execution capacity for MVP

For MVP, use one unified execution capacity model. If a step is running,
it consumes one active slot regardless of whether that time is spent in LLM
calls, tools, or helper sub-sessions. This keeps queueing and scheduling
simple while still respecting real system limits.

## Task Lifecycle

### Task as the top-level entity

A Task is the durable work item visible in the kanban board. Every piece of
background work — chat delegation, scheduler run, webhook trigger — creates
a Task. Main chat does NOT create tasks; it runs the `direct` workflow
inline.

### Task lifecycle

```
draft → queued → ready → running → completed
                                 → failed
                                 → cancelled
running → paused (gate step or user pause)
paused  → running (gate resolved or user resume)
         → cancelled

Any state → cancelled (user can cancel anytime)
```

| State | Meaning |
|-------|---------|
| `draft` | Defined but not submitted. Visible in kanban as a planned item. User can edit title, description, agent, workflow, dependencies, priority. |
| `queued` | Submitted for execution. Waiting for dependencies to resolve and/or capacity. |
| `ready` | All required dependencies met. Eligible for picking when capacity is available. |
| `running` | Picked from queue. Workflow is executing. |
| `paused` | Workflow hit a gate step, or user paused the task. |
| `completed` | Workflow finished successfully. Result available. |
| `failed` | Workflow failed after exhausting retries. |
| `cancelled` | User or system cancelled the task. |

### Task dependencies

Tasks can depend on other tasks, forming a DAG:

```
Task A: "Design API schema"         → no deps, runs when submitted
Task B: "Implement API endpoints"   → depends on A
Task C: "Write API tests"           → depends on B
Task D: "Write API docs"            → depends on A (not B)
```

B and D can both start after A completes. C waits for B.

Dependency rules:
- A task with unmet required dependencies stays `queued`, not `ready`
- When a dependency completes, controller re-evaluates all dependents
- If all required dependencies met → transition to `ready`
- Circular dependencies rejected at creation time (DAG validation)
- Failed/cancelled dependency with `required=true` → dependent task is
  flagged for user decision (pause with notification, not auto-cancelled)

Chat delegation creates tasks in `queued` state (immediate execution intent).
Users create tasks in `draft` state via UI/API (planning, kanban usage).
Submitting a draft moves it to `queued`. Batch submit is supported
("execute this plan" → submit all drafts).

### How tasks are created

| Source | Trigger | Result |
|--------|---------|--------|
| **Chat delegation** | Decision Engine classifies → "delegate" | Task with `source_type=chat`, `source_ref=conversation_id` |
| **API** | `POST /api/v1/tasks` | Task with `source_type=api` |
| **Scheduler** | Cron fires | Schedule creates Task with `source_type=scheduler`, `source_ref=schedule_id` |
| **Webhook** | External event received | Task with `source_type=webhook`, `source_ref=webhook_id` |

### Delivery target

Tasks also carry explicit delivery configuration, because the source of task
creation is not always the same place where results/questions should be sent.

```python
class TaskDelivery:
    mode: str                 # same_conversation | specific_conversation |
                              # latest_active_for_agent | preferred_channel | silent
    target: str | None        # conversation_id or context_ref depending on mode
```

Resolution rules:
- `same_conversation` — use `source_ref` when `source_type=chat`
- `specific_conversation` — use `delivery.target` as a conversation_id
- `latest_active_for_agent` — resolve latest active conversation for user+agent
- `preferred_channel` — use user/agent delivery preference
- `silent` — do not auto-inject into a conversation; result stays on the task

Default behavior:
- chat-created tasks default to `same_conversation`
- scheduler/webhook/API tasks should prefer explicit delivery settings

### Task queue

Tasks enter a priority queue and are picked when capacity is available.

Queue semantics:
- **Priority**: higher priority tasks are picked first. Within same priority, FIFO.
- **Capacity**: one unified execution-capacity model for MVP:
  - `max_active_steps_global`
  - `max_active_steps_per_agent`
  A task is only picked when a new step can start under both limits.
- **Scheduling**: `scheduled_for` field. NULL = immediate. Future timestamp = wait.
- **Queue implementation**: Postgres `SELECT ... FOR UPDATE SKIP LOCKED` for
  lease acquisition. `LISTEN/NOTIFY` for low-latency wakeups. Polling fallback.
- **Paused tasks**: do not consume active execution capacity while waiting
  at a gate or on caller input.

### Result delivery

When a task completes, the result must reach the configured delivery target:

| Delivery mode | Behavior |
|---------------|----------|
| `same_conversation` | Inject synthetic event into source conversation |
| `specific_conversation` | Inject into specified conversation |
| `latest_active_for_agent` | Resolve latest active conversation for user+agent and inject there |
| `preferred_channel` | Resolve preferred conversation/channel from user or agent settings |
| `silent` | Do not auto-inject; leave result on task/API only |

For conversational delivery modes, the controller:
1. Resolves a target conversation
2. Appends a synthetic task event to that conversation's Intaris event stream
   (`task_result`, `task_question`, `task_failed`, `task_status`)
3. Pushes a WebSocket event to any active UI clients
4. If the conversation is idle, trigger a new agent turn immediately
5. If the conversation is active, enqueue the synthetic event like any other
   inbound message; the agent picks it up on the next turn

Before that follow-up turn is submitted, Cognis classifies how the result
should relate to the target conversation:

- **`notify`** is the default for scheduled output, gate pauses, and task
  results delivered outside their source conversation
- **`integrate`** is used for delegation results returning to the parent
  conversation and for same-conversation task results when the bounded
  classifier determines the current thread is still about that work
- if the classifier times out, fails, or is unsure, Cognis falls back to
  **`notify`**

The follow-up contract is typed and semantic. Publishers provide facts such as
task identity, origin, status, and delivery mode; a centralized follow-up
policy selects the mode and emits validated metadata. Prompt rendering happens
later, inside context assembly, so producers do not generate follow-up prose.

For MVP, duplicate follow-up suppression is in-memory and scoped to a single
controller instance. Operators should run one controller replica when relying
on this dedupe behavior; restart or multi-replica replay guarantees are out of
scope for this phase.

This keeps task communication inside the normal conversation/session model.
Tasks do not speak directly to channels; they route back into conversations,
and the conversation's channel connector delivers to Signal/Slack/web/etc.

### Schedules (task factory)

A Schedule is a cron-like entity that creates tasks on a timer:

```python
class Schedule:
    schedule_id: str
    name: str
    cron_expr: str              # standard cron expression
    agent_id: str               # which agent runs the task
    workflow_id: str | None     # which workflow (or auto-select)
    task_template: dict         # title, description, priority, etc.
    enabled: bool
    last_fired_at: datetime | None
    next_fire_at: datetime | None
```

When the schedule fires:
1. Create a Task from the template
2. Set `source_type=scheduler`, `source_ref=schedule_id`
3. Task enters the queue like any other task

Schedules are a task factory, not a task type.

## Conceptual Model

### Three levels of orchestration

```
┌─────────────────────────────────────────────────┐
│ Workflow Engine (between-step orchestration)     │
│   step sequencing, gates, review loops,          │
│   pause/resume, attempt tracking                 │
├─────────────────────────────────────────────────┤
│ Step Runner (within-step execution)              │
│   agent loop, tool calls, sub-agents,            │
│   compaction, internal iteration                 │
├─────────────────────────────────────────────────┤
│ Step Evaluator (post-step verification)          │
│   semantic completion check, approve/revise      │
└─────────────────────────────────────────────────┘
```

The workflow engine manages transitions between steps. The step runner
executes each step as a full agentic session. The step evaluator verifies
completion before the workflow advances.

## Entities

### Workflow

A portable, agent-agnostic process template.

```python
class Workflow:
    workflow_id: str
    name: str
    description: str
    version: int
    criteria: str              # natural language: when to select this workflow
    tags: list[str]
    interaction: InteractionMode
    defaults: WorkflowDefaults
    steps: list[StepDefinition]
    is_system: bool            # bundled with Cognis, read-only
    owner_email: str | None    # NULL for system workflows
```

### StepDefinition

A single step within a workflow.

```python
class StepDefinition:
    name: str
    type: StepType             # "run" | "gate"
    description: str
    prompt: str                # objective for the step runner

    # Context from previous steps (see Step Input Context Model below)
    input: StepInputConfig | None = None
    # Default: type="last", source=<previous step>
    # First step default: type="null"

    # Completion and evaluation (run steps only)
    completion: CompletionConfig | None

    # Whether the step runner may ask the caller questions mid-step
    allow_questions: bool = False

    # Gate configuration (gate steps only)
    gate: GateConfig | None
```

### StepInputConfig

Controls what context flows from previous steps into this step.

```python
class StepInputConfig:
    type: str              # "null" | "full" | "summary" | "last"
    source: str | list[str] | None = None
    # Step name or list of step names.
    # For "full": single step only.
    # For "summary"/"last": single or list.
    # If None and type is "last": defaults to previous step.
```

| Type | What flows | Cost | Source |
|------|-----------|------|--------|
| `null` | Nothing from previous steps. Only step prompt + task description + agent memory. | Zero | None |
| `full` | Complete event history from source step's session, injected as conversation context into a **new** session. | Expensive — consumes context window | Single step only |
| `summary` | LLM-generated summary of source step session(s). More complete and objective than the agent's self-reported summary. | One cheap LLM call per source | Single or list |
| `last` (default) | `step_complete` output from source step(s) — the agent's own summary + claims + structured outputs. Already available, no extra LLM call. | Minimal | Single or list; if omitted, previous step |

Default behavior:
- If `input` is not specified: `type="last"`, `source=<previous step>`
- If first step and `input` not specified: `type="null"`
- If `input.type` is `"null"`: fresh context, no previous step data

### CompletionConfig

How a run step is verified as complete.

```python
class CompletionConfig:
    evaluate: bool = True      # run semantic evaluator after step_complete
    evaluator_prompt: str | None  # custom evaluator prompt (optional)
    max_attempts: int = 3      # max step attempts before exhaustion
    on_exhausted: str = "gate" # "continue" | "fail" | "gate"
```

### GateConfig

How a gate step interacts with the caller.

```python
class GateConfig:
    message: str               # displayed to caller
    context_from: list[str]    # step names whose outputs are included in gate context
    options: list[GateOption]
```

```python
class GateOption:
    label: str
    action: str                # "continue" | "revise(step_name)" | "cancel"
    prompt: bool = False       # ask caller for feedback text
```

### InteractionMode

Controls whether steps can dynamically request caller input.

```python
class InteractionMode:
    mode: str  # "none" | "explicit_gates" | "step_requests"
```

- `none` — fully autonomous. No gates, no questions. Steps run to completion
  without caller interaction. If `on_exhausted` is `gate`, it becomes `fail`.
- `explicit_gates` — only workflow-defined gate steps may pause execution.
  Dynamic caller requests from within steps are not available.
- `step_requests` — run steps with `allow_questions=true` may dynamically
  request caller input via `step_request_input`.

### WorkflowDefaults

Default values inherited by all steps unless overridden.

```python
class WorkflowDefaults:
    max_attempts: int = 3
    evaluate: bool = True
    on_exhausted: str = "gate"
```

## Runtime Entities

### Task (workflow execution state)

The Task entity owns workflow execution state directly. There is no
separate workflow-run entity in MVP.

Workflow state fields on Task:
```python
class Task:
    task_id: str
    title: str
    description: str
    status: TaskStatus          # draft, queued, ready, running, paused, completed, failed, cancelled
    priority: int
    created_by: str             # user email
    agent_id: str               # which agent runs this task
    source_type: str            # "chat", "api", "scheduler", "webhook"
    source_ref: str | None      # conversation_id, schedule_id, etc.
    delivery_mode: str          # same_conversation, specific_conversation, latest_active_for_agent, preferred_channel, silent
    delivery_target: str | None # conversation_id or context_ref
    workflow_id: str             # which workflow template
    workflow_state: WorkflowState  # current_step_index, step_outputs, iteration counts
    queue_name: str
    scheduled_for: datetime | None
    created_at: datetime
    started_at: datetime | None
    completed_at: datetime | None
    result_summary: str | None
    result_data: dict | None
```

### StepRun

A single step execution within a workflow run.

```python
class StepRun:
    step_run_id: str
    task_id: str
    step_name: str
    step_type: str              # "run" | "gate"
    status: StepRunStatus       # pending, running, evaluating, approved, rejected, paused, failed
    attempt: int                # current attempt number
    agent_id: str               # resolved agent for this step
    session_id: str | None      # Cognis session (maps to Intaris parent)
    intaris_session_id: str | None
    output: StepOutput | None
    evaluation: StepEvaluation | None
    started_at: datetime | None
    completed_at: datetime | None
```

### StepOutput

What a step produces when the agent calls `step_complete`.

```python
class StepOutput:
    summary: str
    outputs: dict               # structured outputs (plan, artifacts, etc.)
    claims: list[str]           # what the agent claims it accomplished
```

### StepEvaluation

Result of the controller's semantic evaluation.

```python
class StepEvaluation:
    decision: str               # "approved" | "revise" | "failed"
    reasoning: str
    feedback: str | None        # specific feedback for the agent on revise
    evaluated_at: datetime
```

## Step Completion Protocol

### The `step_complete` tool

Every executable (run) step has a controller-injected tool:

```json
{
  "name": "step_complete",
  "description": "Signal that this workflow step is complete. You MUST call this when you believe the step objective is satisfied. Do not simply stop — the workflow cannot advance without this signal.",
  "parameters": {
    "summary": "Brief summary of what was accomplished",
    "outputs": "Structured output data (plan, artifacts, results, etc.)",
    "claims": "List of specific claims about what was done"
  }
}
```

### Completion flow

```
Agent runs step (full agentic loop)
  │
  ├── Agent calls step_complete(summary, outputs, claims)
  │     │
  │     ▼
  │   Controller receives completion signal
  │     │
  │     ▼
  │   If evaluate=true: run step evaluator
  │     │
  │     ├── Evaluator: "approved"
  │     │     → step advances, workflow continues
  │     │
    │     ├── Evaluator: "revise"
    │     │     → increment attempt counter
    │     │     → if under max_attempts: append feedback to SAME session,
    │     │       resume agent loop (no context reset)
    │     │     → if exhausted: execute on_exhausted action
  │     │
  │     └── Evaluator: "failed"
  │           → step fails, on_exhausted action
  │
  │   If evaluate=false:
  │     → step advances immediately
  │
  └── Agent stops without calling step_complete
        │
        ▼
      Controller detects passive stop
        → re-prompt agent once: "You must call step_complete to finish this step."
        → if still no signal: treat as failed attempt
```

### The `step_request_input` tool (optional)

Available when BOTH conditions are met:
1. the current execution context uses interaction mode `step_requests`
2. the current step has `allow_questions=true`

```json
{
  "name": "step_request_input",
    "description": "Request input from the caller while staying within the same workflow step or direct-chat turn.",
  "parameters": {
    "question": "What you need to know",
    "options": "Optional list of structured options",
    "context": "Why you need this input",
    "timeout_action": "Optional: fail | cancel | continue_with_default"
  }
}
```

When called:
1. If task-backed, the current StepRun transitions to `paused`
2. Caller (main chat, user, API) receives the question
3. Execution does NOT advance — this is not a gate step
4. Caller responds
5. Response is injected into the SAME step session or direct-chat turn
6. Agent loop continues

For direct chat, the pause is transient: it blocks only new user turns in the
same conversation while the question is live. If the controller restarts, the
pending direct-chat question is marked orphaned rather than being resumed.

Exception for managed external runtimes:

- if the direct-chat turn is backed by a durable external runtime lease
  (`runtime_runs`), the pending question may be resumed after controller
  restart using the persisted runtime run state
- otherwise the question remains transient and is orphaned on restart

This is intended mainly for planning and research steps where ambiguity may
appear during execution. Formal approvals should still be modeled as gate
steps between workflow steps.

### Step-local cognition tools

Inside a step, the agent also has access to step-scoped task/todo tools:

```json
{
  "name": "step_todo_write",
  "description": "Track your progress within this step. Use todos for complex multi-step work, break substantial tasks into concrete actionable items instead of a few broad buckets, keep exactly one item in progress, and mark items completed or cancelled immediately as status changes. These todos help you stay organized, especially during long execution with compaction. They are advisory — the workflow advances based on step_complete, not todo status."
}
```

These survive compaction for task/delegation steps (stored in step metadata,
re-injected into context). In direct chat, todos are turn-local and should be
used only for concrete execution work that the agent is actively continuing in
the current turn.

The controller never infers workflow advancement from todo state alone. Only
`step_complete` may advance a run step, and task/delegation steps reject
`step_complete` while todos remain incomplete.

## Step Input Context Assembly

Each step always creates its own Intaris session. No session reuse across
steps. This keeps audit boundaries clean and allows review loops to go
back to any step without contamination.

### How context is assembled per input type

**`null`**:
```
New Intaris session for this step.
Context: step prompt + task description + agent memory (Mnemory recall).
No previous step data injected.
```

**`full`** (single source only):
```
New Intaris session for this step.
1. Read complete event history from source step's session (from cache).
2. Inject events as conversation context into the new session.
3. Append this step's prompt as the next user message.
The step sees the full source discussion but has its own session.
```

**`summary`** (single or multiple sources):
```
New Intaris session for this step.
1. For each source step: generate LLM summary of the step's session.
2. Inject summaries as labeled system context:
   <step_context source="plan" type="summary">
   Created 5-step implementation plan covering auth endpoints...
   </step_context>
3. Append this step's prompt.
```

**`last`** (single or multiple sources, default):
```
New Intaris session for this step.
1. For each source step: take the step_complete output (summary + claims + outputs).
2. Inject as labeled context:
   <step_output source="plan">
   Summary: Created 5-step implementation plan...
   Claims: covers auth endpoints, includes test strategy, ...
   Outputs: {plan: [...], criteria: [...]}
   </step_output>
3. Append this step's prompt.
```

### Iteration context (re-attempts)

On re-attempt after evaluation rejection or review loop rejection:
- the step's **own session continues** (same Intaris session)
- evaluator/reviewer feedback is appended as a message
- the agent sees all its own prior work plus specific feedback
- no input re-assembly — original context is already in the session
- this applies regardless of the step's `input.type`

This is the step's OWN session continuing, not another step's session.

## Step Evaluation

### What it is

Step evaluation is a **semantic completion check**. It answers:
"Does this step's output satisfy the step's definition of done?"

### What it is NOT

- Not a test runner (that is a separate workflow step)
- Not a linter (that is a separate workflow step)
- Not a code reviewer (that is a separate workflow step)
- Not an Intaris safety check (that is the guardrails layer)

### How it works

The evaluator is a single LLM call with a focused prompt:

```
You are evaluating whether a workflow step is complete.

Step objective:
{step.prompt}

Step inputs:
{formatted inputs from previous steps}

Agent's completion claim:
  Summary: {output.summary}
  Claims: {output.claims}
  Outputs: {output.outputs (summarized)}

Task context:
{task.objective}

Decide:
- "approved" if the step objective is satisfactorily met
- "revise" if the step is incomplete or the claims don't match the objective
- "failed" if the step fundamentally cannot succeed

Be skeptical. Agents tend to declare victory prematurely.
Check whether the claims are consistent with the objective.
If the step says "implement with tests" and the claims don't mention tests,
that is a revise.

Respond with JSON: {"decision": "...", "reasoning": "...", "feedback": "..."}
```

### Evaluator characteristics

- **Independent**: separate LLM call, not part of the builder's session
- **Skeptical**: tuned to catch premature completion
- **Cheap**: single LLM call with focused context (not a full agent loop)
- **Fast**: should complete in seconds, not minutes
- **Uses routing policy**: typically a cheaper model than the builder

### Custom evaluator prompts

Steps can override the default evaluator prompt:

```yaml
completion:
  evaluate: true
  evaluator_prompt: |
    This is a planning step. The plan should include:
    - numbered implementation steps
    - success criteria for each step
    - test strategy
    - documentation plan
    Reject if any of these are missing.
```

## Gate Steps

### Purpose

A gate step pauses workflow execution between steps and returns a structured
prompt to the caller. The caller processes the prompt and responds. The
workflow resumes based on the response.

### Gate flow

```
Workflow reaches gate step
  │
  ▼
Controller sends gate event to caller:
  {
    type: "workflow_gate",
    task_id: "...",
    step: "approve_plan",
    message: "Plan ready for review.",
    context: {plan output, review feedback},
    options: [
      {label: "Approve", action: "continue"},
      {label: "Request Changes", action: "revise(plan)", prompt: true},
      {label: "Cancel", action: "cancel"}
    ]
  }
  │
  ▼
Workflow pauses (status: paused)
  │
  ▼
Caller responds (via API, WebSocket, or main chat agent):
  {action: "continue"}
  or {action: "revise(plan)", feedback: "Add error handling for..."}
  or {action: "cancel"}
  │
  ▼
Controller resumes workflow:
  continue → advance to next step
  revise(X) → re-run step X with caller feedback
  cancel → abort workflow
```

### Who is the caller?

The caller depends on context:
- **Main chat agent**: receives gate events as messages, can present to user
  or decide autonomously based on agent configuration
- **User directly**: via task UI (approve/reject buttons)
- **API client**: programmatic gate resolution
- **Parent workflow**: if workflows are nested (future)

## Review Loops

### Between-step review loops

A step can route back to a previous step on rejection:

```yaml
- name: code_review
  type: run
  prompt: "Review code quality, tests, documentation..."
  input: {type: summary, source: [plan, implement]}
  completion:
    evaluate: true
    max_attempts: 2
    on_exhausted: continue
  # This step's OUTPUT may trigger re-running implement:
  on_reject:
    target: implement
    max_loop_iterations: 3
    on_exhausted: gate
```

Note the distinction:
- `completion.max_attempts` — how many times THIS step can be re-run
  (e.g., the review step itself gets re-evaluated after each attempt)
- `on_reject.max_loop_iterations` — how many times the LOOP between
  this step and the target can cycle (e.g., implement → review → implement → review)

### Within-step re-runs

When a step's evaluator returns "revise", the controller:
1. Increments the step attempt counter
2. Creates a new StepRun record for the new attempt
3. Injects: original step prompt + evaluator feedback + previous attempt summary
4. Agent runs the step again with the feedback

This is bounded by `completion.max_attempts`.

Runtime-specific attempt semantics:

- `native` runtime may either continue the same underlying session or start a
  fresh one depending on step/session policy
- `claude_code` runtime continues the same managed external runtime session for
  in-step revision attempts unless the runtime adapter explicitly declares the
  session unrecoverable and rotates to a new one

Clarification:

- creating a new `StepRun` record for a new attempt does **not** require a new
  underlying Intaris or external runtime session
- `StepRun` tracks Cognis attempt metadata; runtime/session reuse is governed
  by the runtime adapter and step retry policy

## Intaris Session Mapping

Intaris supports parent + child sessions, not deeper nesting.

### Mapping rule

```
Task / workflow state (Cognis metadata only — no Intaris session)
  │
  ├── StepRun: plan        → Intaris parent session
  │     ├── sub-agent      → Intaris child session
  │     └── sub-agent      → Intaris child session
  │
  ├── StepRun: implement   → Intaris parent session
  │     ├── sub-agent      → Intaris child session
  │     └── sub-agent      → Intaris child session
  │
  └── StepRun: review      → Intaris parent session
```

Each executable workflow step gets one Intaris parent session.
Sub-agents spawned within the step are Intaris child sessions.
No deeper nesting in Intaris — Cognis tracks the workflow-level
structure in its own metadata.

### Step attempt sessions

First attempt creates a new Intaris session. Subsequent attempts (after
evaluation rejection) **continue the same session** with evaluator feedback
appended. The agent keeps its own context and reasoning from prior attempts.

```
StepRun: implement (attempt 1) → Intaris session A (new)
StepRun: implement (attempt 2) → Intaris session A (continues, feedback appended)
StepRun: implement (attempt 3) → Intaris session A (continues, feedback appended)
```

This is critical: the agent should not lose its own work when iterating.
Only the first attempt assembles context from the step's `input` config.
Subsequent attempts just see the evaluator's feedback added to their
existing session.

Question policy precedence:

- workflow `interaction_mode` and `allow_questions` remain the primary policy
  for whether a step may ask the user for input
- runtime-emitted questions that arrive when questions are disallowed must be
  converted into a step failure or formal gate/escalation according to runtime
  policy; they must not silently bypass workflow interaction rules

## Main Chat As Workflow

Main chat is also a workflow execution — the `direct` workflow:

```yaml
name: Direct
description: Single-step execution. No planning or evaluation.
interaction:
  mode: step_requests
steps:
  - name: execute
    type: run
    prompt: "{user_message}"
    input: {type: "null"}
    completion:
      evaluate: false
```

This means:
- every user interaction goes through the workflow engine
- foreground chat is just a single-step workflow with no evaluation
- background tasks use multi-step workflows
- one execution model for everything

When the Decision Engine classifies a request as "delegate":
1. A task is created
2. A workflow is selected (per agent config or classifier)
3. The workflow runs in the background
4. The main chat (still running its own direct workflow) gets notified

## Workflow Selection

### How a workflow is selected

1. **User explicit**: "use Code with Review" in chat or UI dropdown
2. **Agent default**: agent has a `default_workflow_id`
3. **Automatic**: Decision Engine classifier matches task description against
   available workflows' `criteria` fields

### Agent workflow configuration

```python
class AgentWorkflowConfig:
    available_workflow_ids: list[str]
    default_workflow_id: str
    workflow_selection_mode: str  # "automatic" | "always_ask" | "use_default"
    step_agent_overrides: dict[str, dict[str, str]]
    # e.g. {"software-development": {"code_review": "reviewer-agent-id"}}
```

## Workflow Registry

### System workflows (bundled, read-only)

**Direct** — single step, no overhead:
```yaml
name: Direct
steps:
  - name: execute
    type: run
    input: {type: "null"}
    completion: {evaluate: false}
```

**Research** — plan, research, synthesize, evaluate:
```yaml
name: Research
steps:
  - name: plan
    type: run
    prompt: "Create a research plan..."
    input: {type: "null"}
    completion: {evaluate: true}
  - name: research
    type: run
    prompt: "Execute the research plan..."
    input: {type: last, source: plan}
    # Research consumes the finalized plan output and gathers evidence
    completion: {evaluate: true, max_attempts: 2}
  - name: synthesize
    type: run
    prompt: "Synthesize findings into a coherent report..."
    input: {type: summary, source: [plan, research]}
    completion: {evaluate: true}
```

**Code with Review** — the structured coding workflow:
```yaml
name: Code with Review
steps:
  - name: plan
    type: run
    prompt: "Break down this task into implementation steps..."
    input: {type: "null"}
    completion: {evaluate: true, max_attempts: 2}
  - name: architect_review
    type: run
    prompt: "Review this plan as a proportional architecture and risk check..."
    input: {type: full, source: plan}
    # Reviewer sees the full plan output, including detailed content
    completion: {evaluate: true}
    on_reject: {target: plan, max_loop_iterations: 3, on_exhausted: gate}
  - name: implement
    type: run
    prompt: "Implement the plan with tests and documentation..."
    input: {type: summary, source: [plan, architect_review]}
    # Summary saves context window for actual coding work
    completion: {evaluate: true, max_attempts: 3}
  - name: update_docs
    type: run
    prompt: "Update directly affected docs only when needed..."
    input: {type: summary, source: implement}
    completion: {evaluate: false}
  - name: code_review
    type: run
    prompt: "Review the change for real defects, regressions, and meaningful gaps..."
    input: {type: summary, source: [plan, implement, update_docs]}
    completion: {evaluate: true}
    on_reject: {target: implement, max_loop_iterations: 3, on_exhausted: gate}
  - name: commit
    type: run
    prompt: "Create a conventional commit..."
    # input not specified → default: type=last, source=code_review
    completion: {evaluate: false}
  - name: update_memory
    type: run
    prompt: "Store key findings and decisions as memories..."
    input: {type: last, source: [plan, implement, code_review]}
    completion: {evaluate: false}
```

**Creative** — generate with evaluation loop:
```yaml
name: Creative
steps:
  - name: generate
    type: run
    prompt: "Create the requested content..."
    input: {type: "null"}
    completion: {evaluate: true, max_attempts: 5, on_exhausted: continue}
```

### User workflows

Users can:
- Browse system workflows
- Duplicate → creates editable copy
- Create from scratch via UI form editor
- Export/import as YAML files

## Database Schema

See `01-architecture.md` for the full schema. Key tables:

- `tasks` — durable work items with queue semantics and workflow state
- `workflows` — portable workflow templates
- `step_runs` — individual step execution attempts, children of tasks
- `schedules` — cron-like task factory

Note: there is no `workflow_runs` table. The Task entity owns workflow
execution state directly (`workflow_state` JSONB field). StepRuns reference
`task_id` directly. This keeps the model simple for MVP while allowing
separation later if needed.

## State Machines

### Task states

```
draft → queued → ready → running → completed
                                 → failed
                                 → cancelled
running → paused → running (gate resolved / user resume)
                 → cancelled
Any state → cancelled
```

### StepRun states

```
pending → running → evaluating → approved → (next step)
                               → rejected → running (re-attempt)
                               → failed
running → paused (gate or step_request_input)
       → paused → running (response received)
```

## Safety and Limits

### Per-step limits

- `max_attempts` — max re-runs of a single step (default 3)
- `max_tool_calls` — max tool calls within a step session (from agent settings)
- `max_duration` — wall-clock timeout for a step (from agent settings)
- `question_timeout` — for `step_request_input` waiting period

### Per-workflow limits

- `max_loop_iterations` — max cycles of a review loop between steps
- Total workflow duration timeout (from task settings)

### Queue and capacity limits

- `max_active_steps_global` — total active step sessions across the system
- `max_active_steps_per_agent` — active step sessions for one agent
- dependency resolution moves tasks from `queued` → `ready`
- paused tasks release active capacity

### Runaway prevention

Within a step session:
- Repetition detection (same tool + same args)
- Progress detection (no changes after N tool calls)
- Compaction safety (preserve step todos across compaction)

### Evaluation limits

- Step evaluation uses a cheap model via routing policy
- Single LLM call, not a full agent loop
- Timeout on evaluation call (from settings)

## Relationship To Other Specs

| Spec | Relationship |
|------|-------------|
| `01-architecture.md` | Workflow engine is a new core component |
| `02-agent-model.md` | Agents have workflow config (available, default, step overrides) |
| `03-session-model.md` | Workflow runs create step sessions; Intaris mapping |
| `06-tool-system.md` | step_complete, step_request_input are controller-injected tools |
| `09-ui-ux.md` | Workflow progress in task cards, gate UI, workflow editor |
| `10-api-spec.md` | Workflow CRUD, gate response, step status |
| `13-nfr-operations.md` | Workflow metrics: step durations, evaluation rates, revision rates |
