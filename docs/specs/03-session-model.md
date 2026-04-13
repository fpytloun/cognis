# Cognis: Session Model

## Terminology

| Term | Definition |
|------|-----------|
| **Conversation** | Long-lived interaction between a user and an agent in a specific context. Persists across time. |
| **Session** | Bounded work context within a conversation. Has its own Intaris session with focused intention and its own Mnemory recall session. |
| **Turn** | One user message plus all agent work (LLM calls, tool calls, delegations) until final response. |
| **Message** | A single message (user, assistant, system). Stored in Intaris events. |
| **Context** | Environment where a conversation takes place: web UI, Slack channel, Discord, API, scheduled task. |
| **Runtime session** | Opaque durable session handle owned by the selected runtime. |
| **Execution** | One active run/attempt within a runtime session. |
| **Projection** | Normalized Cognis-visible session history derived from raw runtime trace plus Cognis overlay events. |

## Data Ownership

Session **metadata** (status, IDs, correlation refs) is in Cognis DB.
Session **content** (messages, tool calls, events) is in Intaris event store.
Intaris-derived state (event seq, compaction summary, intention) is in the
**session cache** (in-memory / Redis) — NOT in Cognis DB.

For external runtimes, see `18-runtime-contract.md` for the distinction
between raw runtime trace, runtime session identity, and normalized transcript
projection.

```
Cognis DB                  Session Cache (L1 memory)   Intaris Event Store
┌────────────────────┐    ┌──────────────────────┐    ┌──────────────────────┐
│ conversation_id    │    │ events (since compact)│    │ session events (ndjson)│
│ session_id         │    │ last_event_seq       │    │   user_message        │
│ agent_id           │    │ last_compaction_seq  │    │   assistant_message   │
│ status             │    │ last_compaction_      │    │   tool_call           │
│ intaris_session_id │    │   summary            │    │   tool_result         │
│ mnemory_session_id │    │ intention            │    │   delegation          │
│ delegation_mode    │    │                      │    │   compaction_summary  │
│ delegation_task    │    │ (ephemeral, rebuilt   │    │   evaluation          │
│ started_at         │    │  from Intaris on miss)│    │                      │
└────────────────────┘    └──────────────────────┘    └──────────────────────┘
```

## Conversation Model

```python
class Conversation(BaseModel):
    conversation_id: str
    user_email: str
    agent_id: str

    title: str | None = None
    context: ConversationContext

    root_session_id: str
    active_session_ids: list[str]

    status: ConversationStatus        # active, archived
    last_message_at: datetime | None
    created_at: datetime
    updated_at: datetime


class ConversationContext(BaseModel):
    type: str                         # "web", "signal", "slack", "discord", "api", "scheduled"
    ref: str | None = None            # normalized routing identity, e.g. signal:+4177666888
    platform_data: dict[str, Any] = {}
    memory_labels: dict[str, str] = {}  # For Mnemory recall scoping
    reply_capabilities: dict[str, Any] = {}  # buttons, threads, edits, etc.
    delivery_preferences: dict[str, Any] = {}  # preferred channel/session behavior
```

### Context identity and routing

`context.ref` is the canonical delivery identity for a conversation.
Examples:

- `signal:+4177666888`
- `slack:C1234567890`
- `slack:C1234567890:thread:1712345678.000100`
- `discord:channel:1234567890`
- `web:user:filip@pytloun.cz:default`
- `schedule:daily-review`

This lets Cognis route outbound messages and task results back to the
correct channel/thread through the appropriate connector.

For external transports, `context.platform_data` also stores routing metadata
such as `channel_type`, `account_id`, and `chat_id`. When a channel account
uses the default `pairing` policy, the remote sender must first be linked to a
verified `channel_contact` via a short-lived pairing code redeemed in the web
UI. Unpaired senders do not create turns or reach the agent loop.

### Multi-Context Agent Presence

An agent participates in multiple conversations simultaneously:

```
Agent "Aria"
  ├── Conversation: Web UI (general chat)
  ├── Conversation: Slack #engineering
  ├── Conversation: Slack #ops
  └── Conversation: Scheduled daily review
```

Each is independent: own session, own Intaris scope, own context window. They
share long-term memory in Mnemory (same agent_id). Mnemory recall uses
`memory_labels` to bias search toward the conversation's topic area.

Tasks route back into conversations, not directly to channels. A task result,
question, or failure is injected as a synthetic conversation event into the
target conversation. The conversation's channel connector then delivers the
result to the actual transport (Signal, Slack, web UI, etc.).

This means the main chat agent remains the human-facing narrator:
- the workflow/task system does the work
- the conversation agent receives task events and phrases the response
- this works for idle and active conversations alike

Synthetic task events include:
- `task_result`
- `task_question`
- `task_failed`
- `task_status`

Delivery behavior:
- **Idle conversation**: synthetic task event triggers a new agent turn immediately
- **Active conversation**: synthetic task event is queued like any inbound
  message and processed on the next turn after the current one completes

## Session Model

Runtime session hierarchy:

```text
conversation -> cognis_session -> runtime_session -> execution
```

For `native`, the runtime session is effectively the Cognis session itself.
For external runtimes such as `claude_code`, the runtime session is an opaque
durable handle and user-visible history is reconstructed from a normalized
projection.

```python
class Session(BaseModel):
    """Durable session metadata stored in Cognis DB."""

    session_id: str
    conversation_id: str
    parent_session_id: str | None     # NULL for root session
    previous_session_id: str | None   # Links to predecessor after rotation/compaction

    user_email: str
    agent_id: str                     # May differ for agent delegation

    delegation: DelegationInfo | None  # Set for child sessions

    # External session correlation
    intaris_session_id: str | None
    mnemory_session_id: str | None

    # State (Cognis-authoritative)
    status: SessionStatus
    started_at: datetime
    completed_at: datetime | None
    idle_since: datetime | None
    completion_reason: str | None     # Why session ended: "compacted", "user_reset", etc.
    result: SessionResult | None

    # NOTE: intention, last_event_seq, last_compaction_summary, and
    # last_compaction_seq are NOT stored in the DB. They live in the
    # session cache (in-memory / Redis). See 01-architecture.md
    # "Session Cache Architecture" for details.


class SessionStatus(str, Enum):
    ACTIVE = "active"
    IDLE = "idle"
    SUSPENDED = "suspended"
    TERMINATED = "terminated"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class DelegationInfo(BaseModel):
    mode: DelegationMode              # agent, worker, fork
    delegated_by_session: str
    delegated_by_agent: str
    effective_agent_id: str           # For Mnemory X-Agent-Id
    task_description: str
    expected_output: str | None
    constraints: TaskConstraints


class SessionResult(BaseModel):
    status: str                       # completed, failed, partial
    summary: str
    detailed_output: str | None
    artifacts: list[ArtifactRef] = []
    token_usage: TokenUsage
    duration_seconds: float
```

## Session Hierarchy

```
Conversation: "Help me refactor the auth system"
│
├── Root Session (agent: Aria)
│   Intaris intention: "General development assistance"
│   │
│   ├── Turn 1: User asks about approaches → inline response
│   │
│   ├── Turn 2: "research OAuth2 best practices"
│   │   Decision Engine → DELEGATE_WORKER
│   │   ├── Child Session (worker: _worker/research)
│   │   │   Intaris intention: "Research OAuth2 best practices"
│   │   │   effective_agent_id: "aria" (parent's, for memory)
│   │   │   → completes, result flows back
│   │   └── Main session presents research findings
│   │
│   ├── Turn 3: "implement token refresh" → DELEGATE_AGENT
│   │   ├── Child Session (worker: _worker/code)
│   │   │   Intaris intention: "Implement OAuth2 token refresh"
│   │   │   → runs in background
│   │   └── Main session: "Working on it, you can continue chatting"
│   │
│   └── Turn 4: User asks unrelated question → inline response
│       (while child session continues in background)
```

## Turn Lifecycle

```
1. User message received

2. Decision Engine classifies:
   → INLINE: continue to step 3
   → DELEGATE: create child session, respond with notice, start child loop

3. Context assembly (controller, parallelized where possible):
    a. Load session cache (L1 memory; cold start reads from Intaris)
    b. Concurrent fetch (asyncio.gather):
       - Fetch new events from Intaris since cached last_event_seq
       - Read intention from Intaris (read-through)
       - Recall from Mnemory (query, labels, context=cached intention)
    c. On partial failure: continue with available results, flag degraded
    d. Build messages: system prompt + memories + compacted + recent + user msg

4. LLM call (streaming):
   a. Route to cloud provider or executor (local model)
   b. Stream tokens to client

5. Process LLM response:
   a. Text → stream to client
   b. Orchestration tool → Decision Engine approves/modifies, handle as session op
   c. Intaris-managed MCP tool → Intaris proxy (evaluate + execute)
   d. Local tool → Intaris evaluate → executor tool.execute
   e. Feed result back to LLM → goto 4

6. Finalize turn:
    a. Record events to Intaris: user_message, assistant_message, tool calls
    b. Append same events to session cache (L1)
    c. Remember to Mnemory (async, via bounded retry queue)
    d. Check compaction threshold
    e. NO Cognis DB write for event seq or compaction — cache is
       the only local copy, Intaris is the durable source of truth
```

## Context Window Management

### Token Budget (Dynamic)

The budget is split into **static** (known at session start) and **dynamic**
(what's left for history):

```
Total budget: configurable (e.g., 128,000 tokens)

Static allocation (computed at session start + on skill activation):
  System prompt + personality        ~2,000
  Tool schemas (JSON Schema for N tools) ~5,000-15,000
  Active skill instructions          ~0-10,000
  Reserved for response              ~8,000
  ─────────────────────────────────
  Total static                       ~15,000-35,000

Dynamic allocation (total - static):
  Memory context (Mnemory recall)    ~3,000
  Compaction summary                 ~5,000
  Active delegation statuses         ~1,000
  Recent turns (uncompacted)         remaining (~55,000-85,000)
  Current user message               variable
```

Static budget is recomputed when tools or skills change. This prevents the
token budget overrun identified in the review.

### Compaction Strategy

When context approaches limit (>85% of total budget), compaction triggers
and creates a new Intaris session within the same conversation. The old
session is marked completed with ``completion_reason="compacted"``. The
compaction summary is stored as a ``compaction_summary`` event in the
old session's Intaris stream and injected as system context in the new
session.

Two compaction paths:

- **Manual** (``/compact`` slash command): Compaction runs immediately.
  Session creation is *deferred* until the next user message. The
  ``_load_conversation_runtime()`` function detects the completed/compacted
  root session and calls ``rotate_session()`` on the next turn.

- **Automatic** (post-turn in ``_execute_step()``): When
  ``context_result.recommend_compaction`` is ``True`` after context assembly,
  ``_auto_compact()`` runs after ``_finalize_step()`` records the turn's
  events. It compacts, rotates the session immediately, and emits a
  ``SESSION_COMPACTED`` event for client notification. Bounded to 15 seconds
  timeout. Only fires for direct chat (``ctx.is_direct``), not workflow steps.

Guard: automatic compaction only fires when ``_finalize_step()`` succeeded
(events recorded). This prevents data loss where the turn's events would be
lost if compaction rotated away from the session before events were saved.

```python
class CompactionStrategy:
    async def compact(self, session: Session, *, trigger: str = "manual") -> CompactionResult:
        """
        1. Preserve last N turns uncompacted (default 10)
        2. Summarize older turns via _system/compaction agent (LLM call)
        3. Store summary as compaction_summary event in Intaris
        4. Update session cache: compaction_summary, compaction_seq
        5. Trim pre-compaction events from cache buffer
        """
        ...

    async def compact_with_fallback(self, session: Session, *, trigger: str = "manual") -> CompactionResult:
        """
        Tiered fallback for compaction:
        1. Try LLM compaction (primary)
        2. Retry with fallback model
        3. Mechanical fallback: drop oldest turns beyond preserve window,
           keep only turn metadata (who/what/topic). Log COMPACTION_DEGRADED.
        """
        ...
```

### Session Rotation

When compaction (or user reset) triggers session rotation:

1. Old session marked ``COMPLETED`` with ``completion_reason``
2. New root session created with ``previous_session_id`` pointing to old session
3. New Intaris session created for the new root
4. Conversation ``root_session_id`` updated to new session
5. ``mnemory_session_id`` reset to ``None`` — the first recall in the new session
   creates a fresh Mnemory session and reconstructs the full immutable prefix
   (core memories + instructions) from scratch
6. Old session cache evicted; new session cache pre-populated with compaction summary

The ``previous_session_id`` chain enables "View previous session" navigation
in the web UI. Resetting ``mnemory_session_id`` ensures the new context window
gets a complete immutable prefix rebuild; the old Mnemory session's dedup set
is stale after compaction anyway.

### Context Assembly

```python
class ContextAssembler:
    async def assemble(self, session, user_message, agent):
        messages = []

        # 1. System prompt (local, fast)
        messages.append(self._build_system_prompt(agent))

        # 2. Parallel external fetches (the performance-critical step)
        #    These three calls are independent and can run concurrently.
        #    Use cached intention for Mnemory recall context — it may be
        #    slightly stale if intention just changed, but this is acceptable
        #    for the latency win (~2000ms → ~1200ms for warm sessions).
        cached_intention = self.session_cache.get_intention(session.session_id)

        recall_task = self.memory.recall(
            query=user_message.content,
            session_id=session.mnemory_session_id,
            labels=session.conversation.context.memory_labels,
            context=cached_intention,
        )
        events_task = self.session_cache.refresh_events(
            session.session_id,
            intaris=self.guardrails,
        )
        intention_task = self.guardrails.get_session(
            session_id=session.intaris_session_id,
        )

        recall, _, intaris_session = await asyncio.gather(
            recall_task, events_task, intention_task,
            return_exceptions=True,
        )

        # Handle partial failures gracefully
        if isinstance(recall, Exception):
            recall = None  # Continue without memory; degraded flag set
        if isinstance(intaris_session, Exception):
            pass  # Use cached intention; degraded
        else:
            self.session_cache.update_intention(
                session.session_id, intaris_session.intention)

        # 3. Memory context
        if recall and (recall.core_memories or recall.search_results):
            messages.append(self._format_memory_context(recall))

        # 4. Compaction summary (from session cache)
        compaction = self.session_cache.get_compaction_summary(
            session.session_id)
        if compaction:
            messages.append({"role": "system", "content": compaction})

        # 5. Recent turns (from session cache — refreshed in step 2)
        cached_events = self.session_cache.get_events_since_compaction(
            session.session_id,
            types=["user_message", "assistant_message",
                   "tool_call", "tool_result", "delegation"],
        )
        messages.extend(self._events_to_messages(cached_events))

        # 6. Active delegation statuses
        active = await self._get_active_delegations(session)
        if active:
            messages.append(self._format_delegation_status(active))

        # 7. User message
        messages.append({"role": "user", "content": user_message.content})

        return messages
```

## Session Correlation

Controller creates all external sessions. Both Mnemory and Intaris sessions
are pre-established before any agent loop or executor work begins.

```
Cognis Session ────── Mnemory Session ────── Intaris Session
     │                      │                      │
     │ session_id           │ mnemory_session_id    │ intaris_session_id
     │ (Cognis DB)          │ (auto from 1st recall)│ (Cognis-provided)
     │                      │                       │
     │ Created by:          │ Created by:           │ Created by:
     │ Session Manager      │ Controller 1st recall │ Controller POST /intention
```

### Creation Flow

1. **Conversation created** → conversation metadata row created in Cognis DB
   (no root session yet for web conversations)
2. **First turn** → controller creates the root Cognis session lazily,
   then creates the matching Intaris session (POST /intention)
3. **First turn** → controller calls Mnemory recall (session_id=null on the
   first recall) → gets `mnemory_session_id`, stores in session
4. **Delegation** → controller creates child session in Cognis DB →
   pre-creates both Mnemory session (first recall) and Intaris session
   (POST /intention with parent_session_id)

### Status Synchronization

Cognis session status transitions (idle, completed, failed, cancelled,
suspended, terminated) are synced to Intaris via
`PATCH /api/v1/session/{id}/status`.  The sync is best-effort:
Intaris unavailability does not block Cognis
transitions.

Cognis states that Intaris does not natively support are mapped:

| Cognis status | Intaris status | `status_reason` |
|---|---|---|
| `failed` | `terminated` | `source_status=failed` |
| `cancelled` | `terminated` | `source_status=cancelled` |

All other states map 1:1.  The mapping and sync live in
`SessionManager._sync_intaris_status()` — the single code path for
all status transitions.

### Conversation History Across Sessions

After compaction or session rotation, conversation history spans
multiple root sessions linked by `previous_session_id`.  The
`GET /conversations/{id}/messages` endpoint walks this lineage on
full loads (`after_seq=0`) and merges events from all root sessions
oldest-first.  Incremental fetches (`after_seq > 0`) read only the
active session.  Missing or truncated lineage segments are surfaced as
explicit `history_gap` events instead of being silently dropped.

## Workflow Session Mapping

Workflow execution creates sessions at the step level, not the workflow level.
See [14-workflow-engine.md](14-workflow-engine.md) for the full workflow spec.
For runtime-level step/session boundaries, see `18-runtime-contract.md`.

### Mapping to Intaris sessions

Intaris supports parent + child sessions, not deeper nesting. The mapping is:

```
Task / workflow state (Cognis metadata only — no Intaris session)
  ├── StepRun: plan        → Intaris parent session
  │     ├── sub-agent      → Intaris child session
  │     └── sub-agent      → Intaris child session
  ├── StepRun: implement   → Intaris parent session
  │     ├── sub-agent      → Intaris child session
  │     └── sub-agent      → Intaris child session
  └── StepRun: review      → Intaris parent session
```

- Each executable workflow step gets one Intaris parent session.
- Sub-agents spawned within the step are Intaris child sessions.
- Step re-attempts (after evaluation rejection) **continue the same
  Intaris session** — the evaluator feedback is appended as an
  ``evaluation_feedback`` event, and the agent resumes with its prior
  work context intact. No new session is created on retry.
- The workflow-level structure is tracked in Cognis metadata only
  (`tasks.workflow_state` and `step_runs` tables).

### Main chat as workflow

Main chat runs the `direct` workflow (single step, no evaluation). This
means all execution goes through the workflow engine — foreground chat is
just the simplest workflow. Background tasks use multi-step workflows.

## Sub-Session Execution

When an agent calls `spawn_worker`, `delegate`, or `fork` during a step:

1. Controller creates a child session in Cognis DB and a child Intaris
   session under the parent (via `create_child_session`)
2. Controller publishes `DELEGATION_STARTED` event → frontend shows a
   delegation card with the task description
3. Controller spawns a background `asyncio.Task` that runs a full agent
   loop on the child session using the task description as the user
   message. The child reuses the parent's tool registry and executor.
4. The parent agent loop continues immediately — the child runs
   concurrently in the background

This is distinct from the Decision Engine's "delegate" path, which
creates a **Task** in the task queue. Orchestration tools create
lightweight **sub-sessions** that run a single agent loop turn.

## Delegation Result Delivery

When a child session completes (or fails):

1. Controller updates the child session status in Cognis DB
2. Controller appends a `delegation_completed` (or `delegation_failed`)
   event to the **parent** session's Intaris event stream
   (data={child_session_id, result_summary, ...})
3. Controller publishes `DELEGATION_COMPLETED` / `DELEGATION_FAILED`
   event → frontend updates the delegation card
4. The next time ContextAssembler runs for the parent session, it picks
   up the delegation result from Intaris events — no special queue needed
5. If the parent is mid-turn, the result appears in the next context
   assembly

This avoids lock contention — Intaris event append is independent of the
parent's turn processing.

## Long-Lived Session Management

### Topic Evolution

Intaris handles intention evolution via `/reasoning`. Cognis reads updated
intention back (via `GET /session/{id}` or from `evaluate` responses) and
caches it in `session.intention`. Mnemory recall uses this cached intention as
the `context` parameter for relevance biasing.

### Session Timeout and Rotation

```python
class SessionTimeoutPolicy:
    idle_timeout: timedelta = timedelta(minutes=30)
    max_session_age: timedelta = timedelta(hours=24)
```

Session rotation happens for three reasons:

1. **Compaction** (automatic or manual ``/compact``): context exceeds 85%
   of model capacity. Uses ``rotate_session()`` with
   ``completion_reason="compacted"``.
2. **User reset** (``/new`` or ``/reset`` in channel-bound context): user
   explicitly starts fresh. Uses ``rotate_session()`` with
   ``completion_reason="user_reset"``.
3. **Max session age**: root session reaches ``max_session_age``.

In all cases ``rotate_session()`` is called:
1. Complete current session (``status=completed``, set ``completion_reason``)
2. Create new root session with ``previous_session_id`` pointing to old session
3. Create new Intaris session with inherited intention
4. Reset ``mnemory_session_id`` to ``None`` (fresh Mnemory session on first recall)
5. Update ``conversation.root_session_id``
6. Emit ``SESSION_COMPACTED`` event (for compaction) or push notification to client

A per-conversation ``asyncio.Lock`` in the WebSocket handler prevents
duplicate deferred session creation when multiple tabs send messages
simultaneously after ``/compact``.

### Conversation Archival

Old conversations: all events preserved in Intaris, Mnemory memories persist,
conversation marked `archived`.

## Message Queuing

If a user sends messages while a turn is processing:

1. Messages queued (max `max_queued_messages`, default 5)
2. Beyond limit → reject with error
3. Control commands (`/cancel`, `/stop`, `/status`) bypass queue, processed
   immediately on a separate channel
4. When current turn completes, all queued messages merged into a single user
    message and processed as one turn. Format:
    ```
    [Queued messages (N messages while previous turn was processing)]

    [1] message one content

    [2] message two content
    ```
    Each message includes its index. The LLM sees them as a batch from the
    same user. Individual messages are preserved in the Intaris event stream
    as separate `user_message` events for audit fidelity.
5. `queued_message_count` included in `message_complete` WebSocket event

### Escalation Timeout

When Intaris escalates a tool call and the user doesn't respond:

- `escalation_timeout_seconds` (default 300) in `ExecutionConstraints`
- On timeout: treat as denied, feed denial to LLM, continue
- Escalation timeout does NOT count against `max_duration_seconds`
- `escalation_expired` WebSocket event sent to client

## Concurrency

### Single Writer per Session

Each session has at most one active turn at a time (per-session async lock).
But delegations from that session run as independent concurrent agent loops.

### Parallel Delegations

Multiple delegations from one session can run in parallel:

```
Root Session (Turn 5)
  ├── Delegation A (running) → completes → result in Intaris events
  ├── Delegation B (running) → completes → result in Intaris events
  └── Turn 6 (user asks new question, while A and B continue)
```

Results appear in context on next assembly. WebSocket pushes immediate
notifications to the client.

## Session Recovery

### Problem

If the controller crashes mid-turn — after LLM response, mid-tool-call, or
before turn finalization (step 6) — the session is left in an undefined
state. Events are batch-recorded at turn finalization, so a crash loses the
entire turn's events.

### Recovery Procedure

On controller startup:

1. Scan `sessions` table for rows with `status = 'active'` and
   `updated_at < NOW() - threshold` (default: 5 minutes).
2. Mark these sessions as `status = 'idle'` with `idle_since = NOW()`.
3. For sessions with in-flight delegations (child sessions also active),
   mark child sessions as `failed` with `result_summary = 'controller
   restart; parent recovered'`.
4. Emit `SESSION_RECOVERED` event on the internal event bus.
5. On next WebSocket connect for the affected user, send a
   `session_recovered` message so the client can refresh state.

### Known Limitation (MVP)

A crash between LLM response and turn finalization loses the current turn's
events — the user saw the streamed response, but Intaris has no record of
it. This is acceptable for MVP because:

- The user can re-send their message.
- The session cache is rebuilt from Intaris on recovery (clean state).
- Incremental event recording (write events as they happen rather than
  batch at finalization) can be added later to close this gap.

### Graceful Shutdown

On SIGTERM / SIGINT:

1. Stop accepting new WebSocket connections.
2. Signal all active agent loops to finish current LLM call (do not start
   new tool calls).
3. Wait up to `shutdown_grace_seconds` (default: 15) for in-flight turns
   to finalize.
4. For turns that did not finalize in time: attempt a best-effort event
   flush to Intaris (partial turn recording).
5. Flush Mnemory remember retry queue (bounded timeout).
6. Mark remaining active sessions as `idle`.
7. Close executor connections.
8. Exit.

## Data Retention and Deletion

### Retention Classes

| Class | Behavior | Default |
|-------|----------|---------|
| **active** | Normal operation. Events in Intaris, memories in Mnemory, metadata in Cognis DB. | Indefinite while conversation is active |
| **archived** | Read-only. No new turns. Events preserved in Intaris. | Indefinite (user controls archival) |
| **purged** | Events deleted from Intaris. Cognis metadata deleted. Mnemory memories persist (user controls via Mnemory). | On explicit user request |

### Delete / Archive Semantics

**Archive** (`POST /api/conversations/:id/archive`):
- Set `conversations.status = 'archived'` in Cognis DB.
- Complete all active sessions for this conversation.
- Evict from session cache.
- Intaris events preserved (read-only).
- Mnemory memories persist.

**Delete** (`DELETE /api/conversations/:id`):
- Set `conversations.status = 'deleted'` in Cognis DB (soft delete).
- Complete all active sessions.
- Evict from session cache.
- Intaris events preserved for `retention_days` (default: 90).
- After retention period: cascade `purge` to Intaris.

**Purge** (`DELETE /api/conversations/:id/purge`):
- Hard delete conversation and session rows from Cognis DB.
- Delete all session events from Intaris event store
  (`delete_session` for each session).
- Delete audit log records for affected sessions from Intaris DB.
- Mnemory memories are NOT auto-deleted (user controls their own
  memory lifecycle via Mnemory API or UI).
- Emit `CONVERSATION_PURGED` audit event in Cognis audit log
  (metadata only — no content).

### Observability Redaction

See [13-nfr-operations.md](13-nfr-operations.md) for the content redaction
policy. Logs and metrics must never contain message content, tool arguments,
memory content, or secret values.

### MVP Scope

- Archive and soft-delete are MVP features.
- Purge cascade to Intaris is MVP (simple: call `delete_session` per
  session).
- Automated retention-based cleanup (purge after N days) is Phase 2.
- The retention policy is documented here so Phase 2 does not inherit
  undefined behavior.
