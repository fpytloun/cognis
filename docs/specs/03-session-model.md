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

Persisted session events carry a `turn_id` field in their event data.
For events that belong to a concrete turn, `turn_id` is that turn's stable
correlation key across persisted history, replay, and live transport frames.
Out-of-band events that are not part of a specific turn still include the field
with a `null` value so the event schema stays uniform.

Session events also carry a context-lane envelope in their event data. Historical
pre-lane events are interpreted as `lane="main"`.

```python
class EventContextLane(BaseModel):
    lane: Literal["main", "side"] = "main"
    side_thread_id: str | None = None
    anchor_turn_id: str | None = None
    prompt_visibility: Literal["main", "side_only", "excluded"] = "main"
    tool_policy: Literal["normal", "none", "read_only", "safe_tools"] = "normal"
```

Lane rules:

- `main` is the canonical conversation lane. Normal user turns, assistant
  responses, tool calls/results, delegation events, task delivery events, and
  compaction summaries are main-lane unless explicitly marked otherwise.
- `side` is for side-question interactions such as `/btw`. Side-lane messages
  are stored in the same Intaris session for auditability and UI replay, but are
  not model-visible to ordinary main turns by default.
- `side_thread_id` scopes side history. `/btw` uses one side thread per active or
  anchored main turn so mid-turn side questions can have follow-ups without
  creating a conversation-wide side transcript that leaks into unrelated side
  questions.
- `anchor_turn_id` points at the main turn the side thread is about. For
  mid-turn `/btw`, it is the currently active `turn_id`; after completion it may
  point at the most recent completed turn or at an explicitly selected side
  thread.
- `prompt_visibility="side_only"` means included in side-question prompts for
  the matching side thread, excluded from main-turn prompts. `excluded` is for
  operational/audit events that should never be projected unless explicitly
  requested by an inspector.
- `tool_policy="none"` forbids tool execution for the side turn. Future side
  lanes may allow `read_only` or `safe_tools`, but `/btw` starts with `none`.

All context assembly, compaction, memory extraction, title/intention updates,
undo/redo, and delivery logic must consume events through a lane-aware selector.
No code path may treat every `user_message` or `assistant_message` event as
main-context history solely because of its event type.

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
│ started_at         │    │  from Intaris on miss)│    │   assistant_thinking  │
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

System-initiated follow-up turns are classified before prompt assembly so the
agent knows how to treat the notification relative to the existing
conversation:

- **`integrate`** — the follow-up belongs to the same active work thread and
  should continue that thread naturally (for example a same-conversation task
  result that is still relevant, or a delegation result returning to its
  parent conversation)
- **`notify`** — the follow-up should be presented as a separate update and
  must not resume an older thread by default (for example scheduled briefs,
  gate pauses, and cross-conversation task delivery)

Prompt framing rules for follow-up turns:

- prior conversation turns stay in context as history
- a controller-injected boundary marks those turns as historical context, not
  pending requests
- a structured follow-up block becomes the active instruction for the turn
- follow-up-specific system instructions are static and mode-based so dynamic
  follow-up data stays in the mutable prompt suffix

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
    result_content: str | None        # Durable full completed sub-session output, bounded

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
    mode: DelegationMode              # delegate mode metadata (legacy worker/fork concepts deferred)
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
    d. Select events with a lane-aware context policy (`main` for normal turns;
       `main` plus matching side thread for `/btw` side turns)
    e. Build messages: system prompt + memories + compacted + selected recent +
       current user/side question

4. LLM call (streaming):
   a. Derive the projection policy for the current model/context budget
   b. Project the model-facing transcript, retrying `normal` → `pressure` →
      `critical` modes before any hard context-pressure stop
   c. Route to cloud provider or executor (local model)
   d. Stream tokens to client

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
    d. Log/cache projection diagnostics and prune-cache hints for future replay
    e. Check compaction threshold
    f. NO Cognis DB write for event seq or compaction — cache is
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

### Projection and Tool Output Strategy

Projection owns model-facing budget pressure from tool outputs and runtime
transcript shape. It is intentionally distinct from compaction:

- **Cross-turn projection** is conservative. Historical tool outputs are mostly
  bounded previews or recoverable placeholders with `call_id`, size, anchor, and
  recovery metadata.
- **Within-turn projection** is more generous. Active evidence can borrow a
  larger burst budget so the agent can finish the current reasoning path without
  repeatedly recovering the same output.
- Projection modes are `normal`, `pressure`, and `critical`. The agent loop
  retries stricter projection before treating context pressure as a hard stop.
- Projection is not user-visible during normal operation. It is logged and
  summarized in `/context` and `/info`; compaction remains the visible session
  lifecycle event.

The projection policy is derived from the model window and effective prompt
budget. Large context models are treated as safety margin first, not as a reason
to fill the entire prompt. Initial internal targets:

| Model window | Steady target | Within-turn burst | Cross-turn tool budget | Within-turn tool budget |
|--------------|---------------|-------------------|------------------------|-------------------------|
| ~128K | ~90K-95K | ~105K-115K | ~8K-17K tokens | ~25K-45K tokens |
| ~272K | ~180K | ~225K-245K | ~20K-35K tokens | ~70K-100K tokens |
| ~400K | ~250K | ~330K-360K | ~30K-45K tokens | ~110K-150K tokens |
| ~1M | ~300K-320K | ~450K-600K | ~40K-70K tokens | ~180K-250K tokens |

The cache may remember recent projection decisions and prune hints, but cold
rebuild from Intaris only needs to be semantically close and bounded. It does
not need to reproduce a prior warm-cache projection byte-for-byte.

### Compaction Strategy

Compaction owns durable user/assistant history growth and provider-overflow
recovery when projection cannot make the prompt safe. When context approaches
the compaction threshold, compaction creates a new Intaris session within the
same conversation. The old session is marked completed with
``completion_reason="compacted"``. The compaction summary is stored as a
``compaction_summary`` event in the old session's Intaris stream and injected as
system context in the new session.

Two compaction paths:

- **Manual** (``/compact`` slash command): Compaction and session rotation run
  immediately under the agent-loop per-session lock. A concurrent turn waits
  and then re-resolves the active session before recording its user message.
  The deferred rotation path is crash-recovery/legacy safety only: if Cognis
  observes a completed/compacted root session without an already-rotated active
  child, ``_load_conversation_runtime()`` calls ``rotate_session()`` on the next
  turn and re-fetches preserved tail events from the old session's Intaris
  stream via ``tail_start_seq`` stored in the ``compaction_summary`` event data.
  preserved tail is seeded from Intaris events, not from any model-facing
  projected transcript; controller-only provider metadata such as Anthropic
  signed thinking blocks is within-turn only and is not replayed across the
  manual rotation boundary.

- **Automatic**: When context assembly or provider-overflow recovery indicates
  durable context pressure, ``_auto_compact()`` compacts, rotates the session,
  and emits a ``SESSION_COMPACTED`` event for client notification. LLM
  compaction has a bounded timeout (300 s); on timeout the mechanical fallback
  is called directly. On non-timeout failure, ``compact()`` already attempted
  its own retry and fallback internally, so ``_auto_compact`` returns ``None``
  cleanly.

Guard: automatic compaction only fires when ``_finalize_step()`` succeeded
(events recorded). This prevents data loss where the turn's events would be
lost if compaction rotated away from the session before events were saved.

**Compaction input assembly** uses a three-band strategy:

- **Head band** (20% of token budget): oldest events — captures original goal
  and task framing.
- **Middle band** (dropped): events between head and tail, replaced with an
  explicit omission marker that includes the seq range and a note that tool
  outputs remain recoverable by ``call_id``. User messages from the dropped
  band are copied verbatim into the compaction input so intermediate user
  intents are not lost.
- **Tail band** (60% of token budget): newest events — highest signal for
  resumption.
- **Headroom** (20%): reserved for the previous-summary wrapper and the
  recoverable-handles trailer.

Token budget is derived from the compaction model's ``max_input_tokens`` with
15% headroom, or from the ``session.compaction_max_input_tokens`` setting. The
previous summary wrapper is reserved from the input budget before banding, and
the preserved uncompacted tail is capped by walking user-turn boundaries
backwards until it reaches roughly 30% of the active prompt budget, with
``session.compaction_preserve_turns`` as the maximum turn cap.

**LLM retry**: ``compact()`` retries transient errors (429, 5xx, timeout,
connection) up to ``session.compaction_llm_max_attempts`` before falling back
to the mechanical sliding-window summary. Non-retryable errors (other 4xx,
empty summary) skip the retry.

**Mechanical fallback** (``build_sliding_window_summary``): prepends the
previous anchored summary when present, keeps the first 1-2 original user
requests, the last 8 user messages, 4 assistant finals, and 4
``write_deliverable`` contents captured from tool-call arguments, followed by
event counts and the recoverable-handles block. A prominent warning header
signals irreversible information loss. This path is a last resort — the
``cognis_compaction_fallback_used_total`` counter is alert-worthy.

**Fallback toggle**: ``session.compaction_fallback_enabled`` (default ``True``).
When ``False``, LLM exhaustion returns ``CompactionResult(compacted=False,
method="llm_failed")`` and the user receives a classified failure notice
instead of a degraded mechanical summary.

**Recursion bound**: ``_execute_step`` tracks ``ctx.compaction_recursion_depth``
and caps it at ``session.compaction_max_recursion`` (default 2). Exceeding the
cap surfaces a ``compaction_recursion_exhausted`` classified failure with a
user-visible notice to try ``/new`` and sets a short session-cache cooldown so
threshold-based auto-compaction is suppressed for the next few turns.

**Recoverable-handles block**: capped at 50 entries (ranked by ``output_size``
desc). A trailer line lists how many additional handles were omitted.

```python
class CompactionStrategy:
    async def compact(self, session: Session, *, trigger: str = "manual") -> CompactionResult:
        """
        1. Preserve a token-budgeted tail, capped by last N user turns (default 10)
        2. Assemble three-band input (head/middle-drop/tail, token-budgeted)
        3. Call LLM (system:compaction agent); retry once on transient errors
        4. Append recoverable-handle block (capped at 50 entries)
        5. Store summary as compaction_summary event in Intaris
        6. Update session cache: compaction_summary, compaction_seq
        7. Trim pre-compaction events from cache buffer
        """
        ...

    async def compact_with_fallback(self, session: Session, *, trigger: str = "manual") -> CompactionResult:
        """
        Sliding-window mechanical fallback — called directly only on outer timeout.
        compact() handles its own retry and fallback for non-timeout failures.
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

### Side-question lanes and `/btw`

`/btw` is a side-question command that asks a quick question without interrupting
or queueing behind the active main turn. It uses the same Cognis session and
same Intaris stream, but persists its user/assistant records on `lane="side"`
with `prompt_visibility="side_only"` and a `side_thread_id` anchored to the
active or selected main turn.

The `/btw` model-facing context is:

1. the normal main-lane durable context, assembled through the standard cached
   context path so immutable-prefix prompt caching can still apply;
2. prior `lane="side"` user/assistant messages from the same `side_thread_id`;
3. volatile active-turn context when the side thread is anchored to a running
   turn: the current assistant stream snapshot and bounded active tool-output
   snapshots, inserted as plain controller context blocks rather than provider
   protocol-level assistant/tool messages;
4. the current side question wrapped in strict side-question instructions.

Side turns are one-shot and non-mutating in the initial implementation:

- they use `tool_policy="none"`;
- the LLM request exposes no executable tools unless a future cache-preserving
  provider path can set `tool_choice="none"` and hard-reject returned tool
  calls;
- any returned tool call is recorded as rejected diagnostics and never executed;
- side turns do not affect main title generation, intention updates, normal
  memory extraction, undo/redo branch calculations, queued-message processing,
  or automatic compaction input except through explicit promotion.

Main turns exclude side-lane messages by default. A later explicit promotion
flow may summarize a side thread into a main-visible event, but raw side
transcripts are not promoted automatically.

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
        selected_events = self.context_selector.select(
            cached_events,
            lane="main",
            side_thread_id=None,
        )
        messages.extend(self._events_to_messages(selected_events))

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

When an agent calls `delegate` during a step:

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
2. Controller stores durable full result content on the child session and
   appends a `delegation_completed` (or `delegation_failed`) event to the
   **parent** session's Intaris event stream
   (data={child_session_id, result_summary, result_content, result_source,
   result_truncated, result_anchors, ...})
3. Controller publishes `DELEGATION_COMPLETED` / `DELEGATION_FAILED`
   event → frontend updates the delegation card
4. The next time ContextAssembler runs for the parent session, it picks
   up the delegation result from Intaris events — no special queue needed
5. If the parent is mid-turn, the result appears in the next context
   assembly

This avoids lock contention — Intaris event append is independent of the
parent's turn processing.

Delegate result content prefers an explicit workflow deliverable when one is
available. Otherwise Cognis aggregates all child `assistant_message` contents in
chronological order with stable `[assistant_message:N]` section markers and
separators, so a short later housekeeping response cannot hide an earlier full
report. Large delegate results are bounded predictably and marked as truncated.
The same section markers are exposed as anchors for `list_tool_output_anchors`,
`read_tool_output_anchor`, and `get_subsession` result recovery.

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

1. **Compaction** (automatic hard-pressure recovery or manual ``/compact``):
   automatic rotation runs when durable prompt pressure reaches the configured
   hard-pressure band (currently about 92% of the selected model budget), while
   manual ``/compact`` rotates immediately under the agent-loop session lock.
   Uses ``rotate_session()`` with
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

The agent-loop session lock covers the full compact→rotate critical section,
preventing active model cycles, idle checkpoints, or multiple tabs from
interleaving duplicate deferred session creation after ``/compact``.

### Conversation Archival

Old conversations: all events preserved in Intaris, Mnemory memories persist,
conversation marked `archived`.

## Side Threads

Side threads are lightweight lanes inside a normal Intaris session. They are not
child sessions and do not create a new Mnemory session. Their durable records use
normal `user_message` and `assistant_message` event types with side-lane metadata
so the UI can render them with the same message primitives and future tool-aware
side turns can reuse normal tool-call sequencing.

Selection rules:

- A mid-turn `/btw` creates or resumes `side_thread_id=btw:<active_turn_id>`.
- A follow-up `/btw` from an open side panel resumes that panel's
  `side_thread_id`.
- A post-turn `/btw` without an explicit side-thread selection starts a new side
  thread anchored to the most recent completed main turn.
- `/btw` side turns see main-lane history plus only their own side-thread
  history. Other side threads are hidden unless explicitly inspected.
- Main turns see no side-thread history unless a side thread is explicitly
  promoted into a main-visible summary event.

Compaction must keep side and main lanes separate. Main compaction input excludes
side-lane raw messages. A future side-thread compaction may summarize long side
threads independently, and explicit promotion should prefer a compact summary
over raw transcript injection.

## Message Queuing

If a user sends messages while a turn is processing:

1. Messages queued with stable `queue_id` metadata and optional
   `client_message_id` correlation (max `max_queued_messages`, default 20)
2. Beyond limit → reject with error
3. Control commands (`/cancel`, `/stop`, `/status`) bypass queue, processed
   immediately on a separate channel
4. While pending, clients can list queued messages and may edit queued text or
   delete a queued item before the scheduler pops it for processing. Attachment
   changes require delete-and-recreate because uploaded attachment references
   are immutable once queued.
5. When the current turn completes, queued messages are popped in order and each
   one is processed exactly once as its own follow-up turn. Each processed
   queued input is preserved in the Intaris event stream as a `user_message`
   event for audit fidelity.
6. `queued_count` and `queued_messages_updated` WebSocket events keep clients in
   sync with the pending queue.

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
