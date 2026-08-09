# Cognis: System Architecture

## High-Level Architecture

```
┌────────────────────────────────────────────────────────────────────┐
│  Clients: SvelteKit Web UI │ Slack Bot │ Discord Bot │ API │ CLI  │
└──────────────────────────────┬─────────────────────────────────────┘
                               │  HTTP / WebSocket / SSE
┌──────────────────────────────▼─────────────────────────────────────┐
│                       Cognis Controller                             │
│                                                                     │
│  ┌────────────┐ ┌────────────┐ ┌────────────┐ ┌────────────────┐  │
│  │ API Gateway │ │ Agent Loop │ │  Decision  │ │    Session     │  │
│  │ HTTP/WS/SSE│ │  Engine    │ │  Engine    │ │    Manager     │  │
│  └────────────┘ └────────────┘ └────────────┘ └────────────────┘  │
│  ┌────────────┐ ┌────────────┐ ┌────────────┐ ┌────────────────┐  │
│  │   Agent    │ │    Tool    │ │   Event    │ │   Provider     │  │
│  │  Registry  │ │   Router   │ │    Bus     │ │   Registry     │  │
│  └────────────┘ └────────────┘ └────────────┘ └────────────────┘  │
└───────┬───────────────┬────────────────┬───────────────────────────┘
        │               │                │
   WS JSON-RPC     REST + JWT       REST + JWT
        │               │                │
 ┌──────▼──────┐  ┌─────▼─────┐   ┌─────▼─────┐
 │  Executors  │  │  Mnemory  │   │  Intaris   │
 │             │  │  (Memory) │   │  (Guard +  │
 │ Tool sandbox│  │           │   │  Session   │
 │ + optional  │  │           │   │  Recording)│
 │ local LLM   │  │           │   │           │
 └─────────────┘  └───────────┘   └───────────┘
                                        │
                                   ┌────▼────┐
                                   │  Remote │
                                   │   MCP   │
                                   │ Servers │
                                   └─────────┘
```

## Core Design: Controller = Brain, Executor = Hands

The controller runs **all agent loops**. It handles LLM interaction, memory
injection, guardrails evaluation, and session management. The executor is a
**pure tool execution sandbox** — it receives `tool.execute` commands and
returns results. It has no knowledge of memory, guardrails, or sessions.

```
Controller responsibilities:           Executor responsibilities:
  - Run agent loops (main + delegated)    - Execute tool calls (MCP, built-in)
  - Call LLM (or route to executor        - Manage local MCP server processes
    for local models)                     - Optionally run local LLM inference
  - Inject memory context (Mnemory)       - Return tool results
  - Evaluate tool calls (Intaris)         - Report health/heartbeat
  - Record session events (Intaris)
  - Manage sessions and delegations
  - Stream responses to clients
  - Handle escalations (user approval)
```

**Hard rule**: The controller NEVER executes tool calls directly. Even in the
MVP in-process executor, tool calls cross the executor boundary.

## Component Architecture

### API Gateway

Entry point for all client communication:
- **HTTP REST** for CRUD (agents, conversations, config)
- **WebSocket** for real-time bidirectional chat
- **SSE** for event broadcasting (delegation progress, notifications)
- **Authentication** via JWT tokens and API keys
- **Rate limiting** per user and per agent

All clients — web UI, Slack bot, Discord bot, API consumers — use the same
gateway.

### Channel Adapters

External messaging platforms integrate through the channel adapter subsystem.
Each configured channel account owns one adapter instance and routes inbound
messages through `TurnScheduler.submit_turn()`. Channel accounts should default
to the `pairing` policy so an unknown remote sender must first redeem a
short-lived verification code in the Cognis UI before a turn is submitted.

Webhook adapters are already naturally stateless on the controller side.

Deployment model:

- **Controller-hosted adapters** remain the default for cloud APIs and webhook
  platforms.
- **Executor-hosted adapters** are the target design for channels that need
  user-local services or reachability, such as Signal backed by `signal-cli`.
- **Long-lived controller-hosted adapters** will need DB-based lease ownership
  in multi-controller deployments so only one replica owns each live polling
  loop at a time.

Planned executor-hosted flow:

```text
external platform/service
        |
        v
executor-hosted channel adapter
        |
        v   channel.message
controller inbound pipeline -> TurnScheduler -> agent loop
        |
        v   channel.send
executor-hosted channel adapter -> external platform/service
```

The executor reuses the exact same adapter implementation as the controller.
There is no separate thin proxy channel runtime.

### Agent Loop Engine

Runs agent conversation loops. Can run N loops concurrently (main chat +
delegations). Each loop:

```
1. Assemble context:
   a. Agent system prompt + personality
   b. Recall memories from Mnemory
   c. Load conversation history from Intaris events
   d. Include active delegation statuses
   e. User message

2. Call LLM (via LLM Router):
   a. Route to appropriate provider (cloud, executor, direct SDK)
   b. Stream response tokens to client via WebSocket

3. Process response:
   a. Text → stream to client
   b. Orchestration tool (delegate / task-workflow control) → handle as controller op
   c. Other tool call → evaluate via Intaris, dispatch to executor
   d. Continue LLM loop until final response

4. Finalize turn:
   a. Record events to Intaris (messages, tool calls, results)
   b. Remember to Mnemory (async)
   c. Check compaction threshold
```

### Turn Scheduler

Transport-agnostic turn orchestration. Owns the full lifecycle of a chat
turn — from user message to response — without any dependency on WebSocket
or other transport layers.

```
User message (WS / REST / channel adapter)
  → TurnScheduler.submit_turn()
    → Authorization + session state checks
    → DecisionEngine.decide() (inline vs delegate)
    → [delegate] TaskQueue.submit()
    → [inline] WorkflowEngine.run_direct_turn()
    → EventBus.publish(TURN_COMPLETED)
  → TurnObserver callbacks (streaming to connected clients)
```

Responsibilities:
- Turn submission and per-conversation serialization (one active turn at a time)
- Decision engine dispatch (inline vs delegate)
- Follow-up turns (system-initiated, via EventBus FOLLOW_UP_TURN_REQUESTED)
- Turn cancellation (active turn + child sub-sessions)
- Error classification (provider health checks → structured TurnError)
- Post-turn housekeeping (last_message_at, session cache refresh, title change)
- Conversation runtime loading (including deferred session creation after compaction)
- Authorization enforcement (conversation ownership check)
- Escalation-pending blocking (queue messages when escalation is pending)
- Per-user concurrent turn limits

Follow-up turns use typed controller metadata rather than ad hoc prompt text.
The scheduler receives a semantic follow-up payload and submits a
system-initiated turn that keeps historical messages in context while marking
the new follow-up event as the active instruction. Durable follow-up intent and
turn ownership provide restart and multi-controller admission safety.

Streaming design: **Hybrid** — `TurnObserver` callbacks for real-time
streaming (no EventBus overhead per token), EventBus lifecycle events
(`TURN_STARTED`, `TURN_COMPLETED`, `TURN_ERROR`) for non-streaming
consumers (unread tracking, browser notifications).

### Command Dispatcher

Transport-agnostic slash command handling. Each command returns a
`CommandResult` that the transport layer renders into its native format.

Supported commands: `/compact`, `/new`, `/model`, `/thinking`, `/context`,
`/info`, `/lsp`, `/help`, `/approve`, `/deny`.

### Decision Engine

Determines how to handle each user message. Decisions are **deterministic**:

```
User message arrives
  │
  ├── Fast Rules Engine (< 1ms)
  │   1. Explicit commands (/research, /delegate) → DELEGATE
  │   2. Continuation of active delegation → INLINE
  │   3. Short conversational message → INLINE
  │   4. Complex request indicators → CLASSIFY
  │
  ├── LLM Classifier (if rules inconclusive, ~200ms)
  │   Fast/cheap model classifies into:
  │     inline | delegate | multi_task
  │   Output: task description, suggested agent, complexity
  │
  └── Orchestration Plan (deterministic from classification)
```

The LLM agent currently has one sub-session delegation tool (`delegate`) plus
task/workflow orchestration tools. These submit **requests** to controller
logic — the system approves, modifies, or rejects them. This ensures
predictable orchestration regardless of model capability.

### Session Manager

Manages conversation and session lifecycle:
- Conversation creation, retrieval, context loading
- Session hierarchy (root + child sessions for delegations)
- Mnemory session creation (first recall) and tracking
- Intaris session creation and correlation
- Compaction orchestration
- Session timeout and rotation

### LLM Router

Unified LLM call routing for both agent loops and internal system tasks
(compaction, classification). Routes based on provider configuration:

```python
class LLMRouter:
    async def complete(self, messages, model=None, task_type="default",
                       agent=None, session=None):
        """
        Route LLM call to the appropriate backend.
        
        Resolution:
        1. Resolve model from task_type routing policy + agent config
        2. Find provider that hosts this model
        3. Route based on provider backend:
           - litellm → LiteLLM library (multi-provider cloud)
           - direct → Native provider SDK (Anthropic, OpenAI)
           - passthrough → Simple HTTP to OpenAI-compatible endpoint
           - executor → Route to inference-capable executor via WS
        """
```

LiteLLM is the default backend for cloud providers but is not mandatory.
The `LLMProvider` protocol is our abstraction — LiteLLM is one implementation.
Direct provider SDKs can be used when provider-specific features are needed
(e.g., Anthropic prompt caching, extended thinking).

Executor-provided LLM is a **first-class, general-purpose capability** — not
an edge case. Covers: local models (ollama, vllm, llama.cpp), self-hosted
LiteLLM proxy, custom executor implementations (Claude Code with user's
subscription, Opencode), network-optimized inference, and air-gapped
environments.

**Dynamic model routing**: different task types use tier-appropriate models:

```yaml
model_routing:
  classifier:
    model: "gpt-4.1-nano"           # Decision Engine (fast, cheap)
    reasoning_effort: "low"
  compaction:
    model: "gpt-4.1-mini"           # Context compaction
    reasoning_effort: "low"
```

Internal system tasks (compaction, classification) can use any configured
provider, including executor-provided models if that's what's available.

**Phase 2**: Cognis can expose an OpenAI-compatible endpoint (`/api/llm/v1`)
to serve as an LLM proxy for Mnemory and Intaris, centralizing LLM
configuration and cost tracking across the entire ecosystem.

### Tool Router

Routes tool calls based on their type:

```
LLM returns tool calls
  │
  ├─ Orchestration tool (delegate / task-workflow control)
  │    → Controller handles as session management operation
  │
  ├─ Intaris-managed MCP tool (github/, slack/, remote APIs)
  │    → Controller calls Intaris MCP proxy
  │    → Intaris evaluates + executes in one call
  │    → Executor NOT involved
  │
  └─ Local tool (filesystem/, shell, local MCP, built-in)
       → Controller evaluates via Intaris
       → If approved: Controller sends tool.execute to Executor
       → Executor runs tool, returns result
```

### Event Bus

Internal pub/sub with hook support:

```python
class EventType(str, Enum):
    # Session lifecycle
    SESSION_CREATED = "session.created"
    SESSION_COMPLETED = "session.completed"
    SESSION_ROTATED = "session.rotated"

    # Turn lifecycle
    TURN_STARTED = "turn.started"
    TURN_COMPLETED = "turn.completed"

    # Delegation
    DELEGATION_STARTED = "delegation.started"
    DELEGATION_PROGRESS = "delegation.progress"
    DELEGATION_COMPLETED = "delegation.completed"

    # Tool calls
    TOOL_CALL_EVALUATED = "tool.evaluated"
    TOOL_CALL_EXECUTED = "tool.executed"
    TOOL_CALL_DENIED = "tool.denied"

    # Agent lifecycle
    AGENT_CREATED = "agent.created"
    AGENT_UPDATED = "agent.updated"

    # Intention
    SESSION_INTENTION_UPDATED = "session.intention_updated"
```

Hooks can be: before (can modify/block), after (observe), async (fire-and-forget).

### Agent Registry

Manages agent definitions in the database:
- CRUD operations
- Personality sync to Mnemory (pinned memories)
- Agent Card generation (A2A-compatible)
- Agent selection for delegation
- Default agents management

### Provider Registry

Central registry for pluggable providers:

```python
class ProviderRegistry:
    memory: MemoryProvider
    guardrails: GuardrailsProvider
    executor: ExecutorProvider
    secrets: SecretsProvider
    llm_router: LLMRouter           # Unified LLM routing (not a single provider)
    auth: AuthProvider
```

Provider infrastructure (service URLs, timeouts) is configured via
environment variables. Application-level configuration (LLM providers,
model routing, session settings, security policies) is stored in the
database and managed via the API/UI. There is no configuration file.
See [11-deployment.md](11-deployment.md) for environment variable
reference and [10-api-spec.md](10-api-spec.md) for settings API.

## Data Flow: Complete Request Lifecycle

### Inline Response (Simple Chat)

```
1. User sends message via WebSocket
2. API Gateway authenticates, routes to Session Manager
3. Session Manager loads conversation metadata from Cognis DB
4. Agent Loop Engine assembles context:
   a. Recall from Mnemory (query=message, session_id=mnemory_session)
   b. Read recent events from Intaris (conversation history)
   c. Forward user content to Intaris /reasoning
   d. Build LLM messages: system prompt + memories + history + user msg
5. LLM call (via LLM Router → appropriate backend):
   a. Stream tokens to client via WebSocket
   b. If tool call:
      i.   Tool Router categorizes (orchestration / intaris-mcp / local)
      ii.  Intaris evaluates (or Intaris MCP proxy for remote tools)
      iii. If APPROVE → send tool.execute to Executor, get result
      iv.  If DENY → feed denial to LLM, continue
      v.   If ESCALATE → pause, notify user, wait for decision
   c. Continue LLM loop until final response
6. Turn complete:
    a. Record all events to Intaris (user msg, assistant msg, tool calls)
    b. Append same events to session cache (L1)
    c. Remember to Mnemory (async, via retry queue)
    d. Check compaction threshold
```

### Delegated Sub-Session

```
1. Decision Engine (or LLM delegation tool) → delegate to worker
2. Controller creates:
   a. Child session in Cognis DB (parent_session_id set)
   b. Intaris child session (POST /intention with parent_session_id)
   c. Mnemory session (first recall with task description)
3. Main session responds: "Working on [task] in the background..."
4. Controller starts new concurrent agent loop for child session:
   a. Different agent/worker definition and system prompt
   b. Same or different executor (based on tool requirements)
   c. Runs independently — LLM calls, tool execution, memory/guardrails
5. User can continue chatting in main session
6. Child session completes:
   a. Result recorded as system message in main conversation (Intaris event)
   b. WebSocket push: delegation_completed
   c. Result available in next main session context assembly
```

## Database Schema (Cognis — Metadata Only)

Cognis DB stores only system state and session metadata. Session content
(messages, tool calls, events) is stored in Intaris. Intaris-derived state
(event sequences, compaction summaries, intention) is **not** stored in
Cognis DB — it lives in a tiered in-memory/Redis cache instead. See
[Session Cache Architecture](#session-cache-architecture) below.

```sql
-- Users
-- email is the primary identifier everywhere: Cognis DB, JWT sub claim,
-- X-User-Id to Mnemory/Intaris. Natural OAuth match.
CREATE TABLE users (
    email       TEXT PRIMARY KEY,       -- user_id everywhere
    name        TEXT,
    password_hash TEXT,                 -- argon2id; NULL for OAuth-only users
    role        TEXT NOT NULL DEFAULT 'user',
    created_at  TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

-- API keys (alternative to JWT for service/automation access)
CREATE TABLE api_keys (
    key_id      TEXT PRIMARY KEY,
    user_email  TEXT NOT NULL REFERENCES users(email),
    key_hash    TEXT NOT NULL,          -- argon2id hash of the key
    name        TEXT NOT NULL,
    scopes      JSONB,
    expires_at  TIMESTAMP,
    created_at  TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

-- Agent definitions
CREATE TABLE agents (
    agent_id    TEXT PRIMARY KEY,
    owner_email TEXT NOT NULL REFERENCES users(email),
    name        TEXT NOT NULL,
    display_name TEXT,
    description TEXT,
    system_prompt TEXT,
    personality JSONB,
    skills      JSONB,
    tools       JSONB,
    permissions JSONB,
    llm_config  JSONB,
    execution   JSONB,
    avatar_url  TEXT,
    status      TEXT NOT NULL DEFAULT 'active',
    created_at  TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at  TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

-- User-to-user agent sharing grants (see docs/specs/28-agent-sharing.md).
-- Grantee schema is polymorphic from day one; only `user` is wired in MVP.
CREATE TABLE agent_grants (
    grant_id             TEXT PRIMARY KEY,
    agent_id             TEXT NOT NULL REFERENCES agents(agent_id) ON DELETE CASCADE,
    grantee_type         TEXT NOT NULL CHECK (grantee_type IN ('user', 'group')),
    grantee_user_email   TEXT NULL REFERENCES users(email),
    grantee_group_id     TEXT NULL,                              -- reserved (Phase 2)
    permission           TEXT NOT NULL CHECK (permission IN ('use')),
    executor_scope       TEXT NOT NULL CHECK (executor_scope IN ('owner_executor', 'grantee_executor')),
    granted_by           TEXT NOT NULL REFERENCES users(email),
    granted_at           TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    revoked_at           TIMESTAMP NULL,
    note                 TEXT NULL,
    CHECK (
        (grantee_type = 'user'  AND grantee_user_email IS NOT NULL AND grantee_group_id IS NULL)
     OR (grantee_type = 'group' AND grantee_group_id   IS NOT NULL AND grantee_user_email IS NULL)
    ),
    UNIQUE (agent_id, grantee_type, grantee_user_email, grantee_group_id)
);
CREATE INDEX ix_agent_grants_grantee_user ON agent_grants (grantee_user_email)
    WHERE revoked_at IS NULL;
CREATE INDEX ix_agent_grants_agent ON agent_grants (agent_id)
    WHERE revoked_at IS NULL;

-- Conversation metadata (session content is in Intaris)
CREATE TABLE conversations (
    conversation_id TEXT PRIMARY KEY,
    user_email  TEXT NOT NULL REFERENCES users(email),
    agent_id    TEXT NOT NULL REFERENCES agents(agent_id),
    title       TEXT,
    context_type TEXT NOT NULL,
    context_ref TEXT,
    context_data JSONB,
    memory_labels JSONB,
    status      TEXT NOT NULL DEFAULT 'active',
    root_session_id TEXT,
    last_message_at TIMESTAMP,
    created_at  TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at  TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

-- Session metadata (session content is in Intaris)
-- NOTE: intention, last_event_seq, last_compaction_summary, and
-- last_compaction_seq are NOT stored here. They are Intaris-authoritative
-- and cached in the session cache layer (in-memory / Redis).
CREATE TABLE sessions (
    session_id  TEXT PRIMARY KEY,
    conversation_id TEXT NOT NULL REFERENCES conversations(conversation_id),
    parent_session_id TEXT REFERENCES sessions(session_id),
    user_email  TEXT NOT NULL,
    agent_id    TEXT NOT NULL,
    delegation_mode TEXT,
    delegation_task TEXT,
    status      TEXT NOT NULL DEFAULT 'active',
    intaris_session_id TEXT,
    mnemory_session_id TEXT,
    started_at  TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    completed_at TIMESTAMP,
    result_summary TEXT
);

-- System settings (replaces config file for app-level configuration)
-- Infrastructure config (URLs, keys) stays in env vars.
CREATE TABLE settings (
    key         TEXT PRIMARY KEY,       -- e.g. "session.idle_timeout_seconds"
    value       JSONB NOT NULL,
    category    TEXT NOT NULL,           -- session, security, decision_engine, etc.
    updated_by  TEXT REFERENCES users(email),
    updated_at  TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

-- LLM provider configurations (managed via UI/API, not config file)
CREATE TABLE llm_providers (
    provider_id  TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    location     TEXT NOT NULL,          -- "controller" | "executor"
    backend      TEXT NOT NULL,          -- "litellm" | "direct" | "passthrough" | "executor"
    config       JSONB NOT NULL,         -- backend-specific (model list, endpoint, etc.)
    status       TEXT NOT NULL DEFAULT 'active',
    created_at   TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at   TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

-- Model routing policy (which model for which task type)
CREATE TABLE model_routing (
    task_type    TEXT PRIMARY KEY,       -- "default", "classifier", "compaction", etc.
    provider_id  TEXT REFERENCES llm_providers(provider_id),
    model        TEXT NOT NULL,
    config       JSONB,                  -- temperature, max_tokens overrides
    updated_at   TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

-- Tasks (durable work items — the kanban card, the queue item)
-- Every background delegation, scheduler run, and webhook creates a Task.
-- Main chat does NOT create tasks — it runs the direct workflow inline.
-- Status lifecycle: draft → queued → ready → running → completed/failed/cancelled
CREATE TABLE tasks (
    task_id       TEXT PRIMARY KEY,
    title         TEXT NOT NULL,
    description   TEXT,
    status        TEXT NOT NULL DEFAULT 'draft',
        -- draft:     defined but not submitted for execution (kanban planning)
        -- queued:    submitted, waiting for dependencies and/or capacity
        -- ready:     all dependencies met, eligible for picking
        -- running:   picked from queue, workflow executing
        -- paused:    gate step or user-paused
        -- completed: workflow finished successfully
        -- failed:    workflow failed after exhausting retries
        -- cancelled: user or system cancelled
    priority      INTEGER NOT NULL DEFAULT 0,   -- higher = picked first

    -- Who
    created_by    TEXT NOT NULL REFERENCES users(email),
    agent_id      TEXT NOT NULL REFERENCES agents(agent_id),

    -- Source (how to deliver result back)
    source_type   TEXT NOT NULL,     -- "chat", "api", "scheduler", "webhook"
    source_ref    TEXT,              -- conversation_id, schedule_id, etc.

    -- Delivery (where results/questions are routed back)
    delivery_mode TEXT NOT NULL DEFAULT 'same_conversation',
        -- same_conversation: source conversation (default for chat)
        -- specific_conversation: use delivery_target
        -- latest_active_for_agent: resolve latest active conversation for user+agent
        -- preferred_channel: user/agent configured default context
        -- silent: no automatic conversation injection
    delivery_target TEXT,            -- conversation_id or context_ref depending on mode

    -- Workflow
    workflow_id   TEXT REFERENCES workflows(workflow_id),
    workflow_state JSONB,            -- current_step_index, step_outputs, iteration counts

    -- Queue
    queue_name    TEXT DEFAULT 'default',
    max_attempts  INTEGER DEFAULT 1,
    scheduled_for TIMESTAMP,         -- NULL = immediate

    -- Lifecycle
    created_at    TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    started_at    TIMESTAMP,
    completed_at  TIMESTAMP,

    -- Result
    result_summary TEXT,
    result_data   JSONB
);

-- Task dependencies (DAG edges between tasks)
-- A task with unmet required dependencies stays queued but is not eligible
-- for picking. When a dependency completes, controller re-evaluates dependents.
CREATE TABLE task_dependencies (
    task_id       TEXT NOT NULL REFERENCES tasks(task_id),
    depends_on    TEXT NOT NULL REFERENCES tasks(task_id),
    required      BOOLEAN NOT NULL DEFAULT TRUE,
        -- TRUE:  dependent task cannot start until this completes
        -- FALSE: advisory; dependency result available as input if completed
    PRIMARY KEY (task_id, depends_on),
    CHECK (task_id != depends_on)    -- no self-dependencies
);

-- Schedules (cron-like task factory — creates tasks on a schedule)
CREATE TABLE schedules (
    schedule_id   TEXT PRIMARY KEY,
    name          TEXT NOT NULL,
    cron_expr     TEXT NOT NULL,      -- standard cron expression
    agent_id      TEXT NOT NULL REFERENCES agents(agent_id),
    workflow_id   TEXT REFERENCES workflows(workflow_id),
    task_template JSONB NOT NULL,     -- title, description, priority, etc.
    enabled       BOOLEAN NOT NULL DEFAULT TRUE,
    last_fired_at TIMESTAMP,
    next_fire_at  TIMESTAMP,
    created_by    TEXT NOT NULL REFERENCES users(email),
    created_at    TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at    TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

-- Workflow templates (portable, agent-agnostic process definitions)
CREATE TABLE workflows (
    workflow_id   TEXT PRIMARY KEY,
    name          TEXT NOT NULL,
    description   TEXT,
    version       INTEGER NOT NULL DEFAULT 1,
    criteria      TEXT,
    tags          JSONB,
    steps         JSONB NOT NULL,
    interaction   JSONB,
    defaults      JSONB,
    is_system     BOOLEAN NOT NULL DEFAULT FALSE,
    owner_email   TEXT REFERENCES users(email),
    created_at    TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at    TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

-- Step run instances (one per step attempt, children of task)
CREATE TABLE step_runs (
    step_run_id   TEXT PRIMARY KEY,
    task_id       TEXT NOT NULL REFERENCES tasks(task_id),
    step_name     TEXT NOT NULL,
    step_type     TEXT NOT NULL,     -- "run" | "gate"
    attempt       INTEGER NOT NULL DEFAULT 1,
    status        TEXT NOT NULL DEFAULT 'pending',
    agent_id      TEXT,
    session_id    TEXT REFERENCES sessions(session_id),
    intaris_session_id TEXT,
    output        JSONB,
    evaluation    JSONB,
    started_at    TIMESTAMP,
    completed_at  TIMESTAMP
);

-- Encrypted secrets
CREATE TABLE secrets (
    secret_id   TEXT PRIMARY KEY,
    user_email  TEXT NOT NULL,
    name        TEXT NOT NULL,
    scope       TEXT NOT NULL DEFAULT 'user',
    agent_id    TEXT,
    encrypted_value BLOB NOT NULL,
    description TEXT,
    created_at  TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at  TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(user_email, name, scope, agent_id)
);

-- System audit log (Cognis-level events, NOT session content)
CREATE TABLE audit_log (
    log_id      TEXT PRIMARY KEY,
    event_type  TEXT NOT NULL,
    user_email  TEXT,
    agent_id    TEXT,
    details     JSONB,
    created_at  TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);
```

## Session Cache Architecture

Intaris-derived session state is cached in a tiered cache instead of being
persisted to Cognis DB. This eliminates the dual-write consistency problem
between Cognis DB and Intaris.

### Why: Intaris Mutability Model

Intaris uses two storage layers with different mutability guarantees:

| Data | Storage | Mutability |
|------|---------|------------|
| Session events (ndjson chunks) | **Object store** (S3/filesystem) | **Immutable** — append-only, never overwritten |
| Audit log core fields | Database | Immutable — INSERT only |
| Audit log resolution fields (overrides) | Database | Mutable — UPDATE on user/judge decision |
| Session metadata (intention, status, counters) | Database | Mutable — UPDATE on every evaluation |
| Compacted summaries | Database | Supersede — DELETE old + INSERT new |

The object store contents (event chunks) are the bulk of what context
assembly reads. They are safe to cache indefinitely because they never
change after being written.

### Cache Tiers

**L1 — In-process memory** (MVP):
- Python dict or LRU keyed by `session_id`
- Holds event buffer, seq counters, compaction summary, intention
- Zero-latency reads for active sessions
- Lost on controller restart — warm from Intaris on first access
- Evicted on session idle timeout or LRU pressure

**L2 — Redis** (optional):
- Survives controller restarts
- Reduces repeated Intaris cold loads across controller replicas
- Events as sorted set by seq; metadata as hash
- TTL matching session idle timeout
- MVP can skip Redis entirely

### Cached Session State

```python
class SessionCache:
    """In-memory cache for Intaris-derived session state."""

    session_id: str
    intaris_session_id: str

    # Event buffer: events since last compaction (append-only, immutable)
    events: list[IntarisEvent]
    last_event_seq: int

    # Compaction state (controller knows when it triggers compaction)
    last_compaction_seq: int
    last_compaction_summary: str | None

    # Intention (mutable — read-through at turn start)
    intention: str | None
```

### Cache Population Flow

```
First turn in session (cold cache):
  1. Read events from Intaris (full read from seq 0 or after last compaction)
  2. Read intention from Intaris (GET /session/{id})
  3. Populate L1 cache
  4. Context assembly reads from cache

Subsequent turns (warm cache):
  1. Fetch only new events from Intaris (after_seq=cached_last_seq)
  2. Append to L1 event buffer — no full re-read
  3. Read intention from Intaris (may have changed via /reasoning)
  4. Context assembly reads from cache

Turn finalization:
  1. Record events to Intaris (single write target)
  2. Append same events to L1 cache
  3. Update cached last_event_seq
  4. NO write to Cognis DB for these fields

After compaction:
  1. Write compaction summary event to Intaris
  2. Update L1 cache: new compaction summary, new compaction_seq
  3. Trim pre-compaction events from L1 buffer
  4. NO write to Cognis DB

Controller restart:
  1. L1 cache is empty (lost)
  2. Next access to any session triggers cold-cache path
  3. One-time Intaris read per session to warm cache
```

### Design Rationale

- **No dual-write**: Intaris is the single durable source of truth for
  event sequences, compaction state, and intention. Cognis DB never stores
  copies of these values.
- **No reconciliation needed**: The cache is ephemeral. If it is wrong or
  missing, it is rebuilt from Intaris. There is nothing to reconcile.
- **Append-only event caching is safe**: Intaris event chunks in the object
  store are immutable — once written, they never change. Cached events
  never need invalidation.
- **Controller-triggered compaction invalidation**: The controller runs
  compaction, so it knows exactly when the summary changes. No external
  invalidation signal needed.
- **Intention is read-through**: Intention can change via Intaris
  `/reasoning` (which the controller calls) or via the Intaris sweeper.
  Read at turn start with short-lived cache within the turn.

## Package Structure

```
cognis/
├── pyproject.toml
├── cognis/
│   ├── __init__.py
│   ├── main.py                     # Entry point
│   ├── config.py                   # Configuration
│   │
│   ├── api/                        # API Gateway
│   │   ├── app.py                  # FastAPI factory
│   │   ├── routes/
│   │   │   ├── conversations.py
│   │   │   ├── agents.py
│   │   │   ├── auth.py
│   │   │   ├── secrets.py
│   │   │   ├── settings.py         # System settings, LLM providers, model routing
│   │   │   ├── tasks.py            # Task queue, dependencies, gate/step responses
│   │   │   ├── tools.py
│   │   │   ├── workflows.py        # Workflow CRUD, gate response
│   │   │   ├── schedules.py        # Schedule CRUD (task factory)
│   │   │   ├── escalations.py
│   │   │   └── system.py           # Health, metrics, JWKS
│   │   ├── websocket.py            # WebSocket transport layer (thin adapter)
│   │   ├── middleware.py           # Auth, rate limiting
│   │   └── models.py              # API request/response models
│   │
│   ├── core/                       # Orchestration Core
│   │   ├── turn_scheduler.py      # Turn orchestration (transport-agnostic)
│   │   ├── commands.py            # Slash command dispatch (transport-agnostic)
│   │   ├── agent_loop.py          # Agent loop engine (step runner)
│   │   ├── task_queue.py          # Queue picking, capacity, dependency resolution
│   │   ├── workflow_engine.py     # Workflow orchestration (step sequencing, gates, loops)
│   │   ├── step_evaluator.py      # Semantic step completion evaluation
│   │   ├── decision.py            # Decision Engine (classify + workflow selection)
│   │   ├── session.py             # Session Manager
│   │   ├── session_cache.py       # L1 in-memory cache for Intaris-derived state
│   │   ├── tool_router.py         # Tool routing logic
│   │   ├── compaction.py          # Context compaction
│   │   ├── context.py             # Context assembly (parallel external fetches)
│   │   ├── events.py              # Event Bus + hooks
│   │   └── remember_queue.py      # Bounded retry queue for Mnemory remember
│   │
│   ├── models/                     # Domain models (Pydantic)
│   │   ├── agent.py
│   │   ├── session.py
│   │   ├── tool.py
│   │   ├── delegation.py
│   │   └── config.py
│   │
│   ├── providers/                  # Provider interfaces + implementations
│   │   ├── base.py                 # Protocol definitions
│   │   ├── registry.py
│   │   ├── memory/
│   │   │   ├── protocol.py
│   │   │   └── mnemory.py
│   │   ├── guardrails/
│   │   │   ├── protocol.py
│   │   │   └── intaris.py
│   │   ├── executor/
│   │   │   ├── protocol.py
│   │   │   ├── in_process.py      # MVP: same process
│   │   │   ├── subprocess.py      # Local subprocess
│   │   │   ├── docker.py          # Phase 2
│   │   │   └── kubernetes.py      # Phase 2
│   │   ├── secrets/
│   │   │   ├── protocol.py
│   │   │   └── encrypted_db.py
│   │   ├── llm/
│   │   │   ├── protocol.py
│   │   │   └── litellm.py
│   │   └── auth/
│   │       ├── protocol.py
│   │       └── jwt.py
│   │
│   ├── tools/                      # Tool system
│   │   ├── builtin/
│   │   │   ├── orchestration.py   # delegate, task/workflow orchestration
│   │   │   └── system.py          # list_agents, get_status
│   │   ├── mcp.py                  # MCP client
│   │   ├── skills.py
│   │   └── registry.py
│   │
│   ├── store/                      # Cognis DB (metadata only)
│   │   ├── database.py
│   │   ├── migrations/
│   │   └── queries.py
│   │
│   └── platforms/                  # Phase 2
│       ├── slack.py
│       └── discord.py
│
├── ui/                             # SvelteKit frontend
│   └── ...
│
└── tests/
    ├── unit/
    ├── integration/
    └── contract/                   # Mnemory/Intaris contract tests
```

## Concurrency Model

Fully async (Python `asyncio`):

- **WebSocket connections**: each client is an independent async task
- **Agent loops**: each active session (main + delegations) runs its own async loop
- **LLM streaming**: async iterators for token-by-token streaming
- **Tool execution**: async dispatch to executor, await result
- **Mnemory/Intaris calls**: async HTTP client (httpx)
- **Per-session ordering**: turns within a session are serialized via async lock

```python
class SessionLock:
    """One active turn per session at a time."""
    _locks: dict[str, asyncio.Lock]
```

Multiple conversations and delegations run concurrently. Within a single
session, turns are serialized but delegations from that session run in
parallel.

## Error Handling

### Provider Failures

| Provider | Failure Mode | Behavior |
|----------|-------------|----------|
| Memory (Mnemory) | Graceful degradation | Continue without memory context; warn user |
| Guardrails (Intaris) | **Fail-closed** | Block tool execution; inform user |
| Executor | Retry then fail | Retry tool call; on persistent failure, inform LLM |
| LLM | Retry with fallback | Retry, try fallback model, then fail |
| Secrets | Fail-closed | Cannot proceed without required secrets |

### Circuit Breaker

Provider calls use circuit breaker pattern:

```python
class CircuitBreaker:
    failure_threshold: int = 5
    recovery_timeout: float = 30.0
    # CLOSED → OPEN → HALF_OPEN → CLOSED
```

## Observability

- **Structured logging** (JSON) with correlation IDs
- **Prometheus metrics** at `/api/metrics`
- **Health check** at `/api/health` with provider status
- **OpenTelemetry tracing** (Phase 2) for request → LLM → tool → response spans
