# Cognis: Integration Contracts

## Overview

Cognis integrates with external services through provider interfaces. This
document specifies exact API contracts **verified against the current Mnemory
and Intaris implementations**.

Important: the controller is the sole client for Mnemory and Intaris. Executors
never call these services directly — all communication is proxied through the
controller.

## Memory Provider (Mnemory)

### Protocol

```python
class MemoryProvider(Protocol):
    async def recall(
        self,
        query: str,
        session_id: str | None = None,
        labels: dict[str, Any] | None = None,
        context: str | None = None,
        search_mode: str = "find",
        include_instructions: bool = False,
    ) -> RecallResult:
        """
        Load relevant memories.

        First call (session_id=None): creates session, returns core_memories.
        Subsequent calls: searches for memories, skips already-known.

        NOTE: search_mode defaults to "find" (LLM-powered, slower) on every
        call. For follow-up recalls within a turn, consider search_mode="search"
        (fast vector search) to reduce latency.
        """
        ...

    async def remember(
        self,
        session_id: str,
        messages: list[dict],
        role: str | None = None,
        labels: dict[str, Any] | None = None,
        context: str | None = None,
    ) -> None:
        """
        Store conversation for memory extraction (async, fire-and-forget).
        Returns immediately. Mnemory processes in background.

        IMPORTANT: remember does NOT create sessions. Always call recall first
        to establish a session. role="assistant" requires X-Agent-Id header.
        """
        ...

    async def add_memory(
        self,
        content: str,
        memory_type: str | None = None,
        categories: list[str] | None = None,
        importance: str | None = None,
        role: str = "user",
        pinned: bool = False,
        labels: dict[str, Any] | None = None,
    ) -> str:
        """Add a memory directly (for agent personality sync, etc.)."""
        ...

    async def search(
        self,
        query: str,
        labels: dict[str, Any] | None = None,
        categories: list[str] | None = None,
        limit: int = 10,
    ) -> list[MemoryItem]:
        """Direct memory search (bypass session tracking)."""
        ...


class RecallResult(BaseModel):
    session_id: str
    instructions: str | None
    core_memories: str | None        # Only on first call
    search_results: list[MemorySearchResult]
    stats: RecallStats

class MemorySearchResult(BaseModel):
    id: str
    memory: str
    score: float
    metadata: dict
    has_artifacts: bool

class RecallStats(BaseModel):
    core_count: int
    search_count: int
    new_count: int
    known_skipped: int
    latency_ms: int
```

### Mnemory HTTP Endpoints (Verified)

#### POST /api/recall

Request:
```json
{
  "session_id": "string | null",
  "query": "string | null",
  "messages": [{"role": "user", "content": "..."}],
  "include_instructions": false,
  "search_mode": "find",
  "context": "string | null",
  "labels": {"key": "value"},
  "ttl": 86400
}
```

Response:
```json
{
  "session_id": "string",
  "instructions": "string | null",
  "core_memories": "string | null",
  "search_results": [{"id": "...", "memory": "...", "score": 0.0, "metadata": {}, "has_artifacts": false}],
  "stats": {"core_count": 0, "search_count": 0, "new_count": 0, "known_skipped": 0, "latency_ms": 0}
}
```

Semantics:
- No `session_id` or expired → creates new session, returns `core_memories`
- `query` wins; otherwise last user message from `messages`
- `search_mode` defaults to `"find"` (LLM-powered) on every call — NOT automatic fast mode
- `labels` filter recall results
- Sessions track known memory IDs for echo suppression

#### POST /api/remember

Request:
```json
{
  "session_id": "string | null",
  "messages": [{"role": "user", "content": "..."}, {"role": "assistant", "content": "..."}],
  "role": "user | assistant | null",
  "labels": {"key": "value"},
  "context": "string | null"
}
```

Response: `{"accepted": true}`

Semantics:
- Async/background processing — no result payload
- Only `user` and `assistant` messages are processed; `system`/`tool` ignored
- `role="assistant"` requires `X-Agent-Id` header
- Does NOT create sessions — always establish via recall first
- With retry: if remember fails, retry with exponential backoff (3 attempts)

### Mnemory Auth

Cognis authenticates to Mnemory using **JWT with audience claims**:
- `Authorization: Bearer <jwt>` — JWT issued by Cognis
- JWT `sub` = user email (maps to Mnemory `X-User-Id`)
- JWT `aud` includes `"mnemory"` — audience claim prevents token misuse
- `X-Agent-Id: <agent>` header for agent scoping

JWT validation in Mnemory is a Phase 0 prerequisite (M1). Mnemory accepts
both JWT and API key for backward compatibility with standalone usage.

### Integration Flow

```
Conversation Start:
  1. Controller calls recall(query=first_message, session_id=None)
     - X-Agent-Id: agent_id
     - Labels: conversation memory_labels
  2. Returns mnemory_session_id + core_memories
  3. Controller stores mnemory_session_id in Cognis sessions table

Each Turn:
  1. Controller calls recall(query=message, session_id=mnemory_session_id)
     - context=session.intention (for relevance bias)
     - search_mode="find" (first turn) or "search" (follow-up, cheaper)
  2. Results injected into LLM context

After Turn:
  1. Controller calls remember(session_id, [user_msg, assistant_msg])
     - role=None (auto-detect)
     - labels: {cognis_session_id, cognis_conversation_id}
     - Dispatched to bounded retry queue (see Remember Retry Queue below)

Delegation:
  1. Controller calls recall(session_id=None, query=task_description)
     - X-Agent-Id: effective_agent_id (parent for workers, own for agents)
  2. New mnemory_session_id stored in child session

Agent Personality Sync:
  1. POST /api/memories for each personality trait
     - role="assistant", pinned=true, X-Agent-Id: agent_id
```

## Guardrails Provider (Intaris)

### Protocol

```python
class GuardrailsProvider(Protocol):
    async def create_session(
        self,
        session_id: str,
        intention: str,
        agent_id: str,
        user_id: str | None = None,
        parent_session_id: str | None = None,
        policy: SessionPolicy | None = None,
        details: dict | None = None,
    ) -> None:
        """
        Create a guardrails session. Intaris returns {ok: true}.
        Duplicate session_id returns 409. X-Agent-Id header required.
        """
        ...

    async def evaluate(
        self,
        session_id: str,
        tool_name: str,
        arguments: dict,
        context: dict | None = None,
    ) -> EvaluationResult:
        """
        Evaluate a tool call for safety and alignment.
        NOTE: `context` is a dict, not a string.
        """
        ...

    async def report_reasoning(
        self,
        session_id: str,
        content: str,
        context: str | None = None,
    ) -> None:
        """
        Forward user/agent content for intention tracking.
        IMPORTANT: content must be prefixed with "User message: " for
        Intaris to trigger intention regeneration.
        """
        ...

    async def checkpoint(
        self,
        session_id: str,
        content: str,
    ) -> None:
        """Submit periodic agent state checkpoint."""
        ...

    async def get_session(
        self,
        session_id: str,
    ) -> IntarisSession:
        """
        Read back session state including updated intention.
        Intaris is authoritative for intention — Cognis caches it.
        """
        ...

    async def submit_decision(
        self,
        call_id: str,
        decision: str,
        note: str | None = None,
    ) -> None:
        """Submit user decision for escalated tool call."""
        ...

    async def list_pending_escalations(
        self,
        session_id: str | None = None,
    ) -> list[EscalationRecord]:
        """List unresolved escalations (via audit endpoint)."""
        ...

    # Session event recording (session content storage)

    async def record_events(
        self,
        session_id: str,
        events: list[SessionEvent],
        source: str = "cognis",
        idempotency_key: str | None = None,
    ) -> EventAppendResult:
        """
        Append events to session event store.
        This is the primary storage for session content.

        idempotency_key: Optional key for deduplication on retry.
        Format: "{session_id}:{turn_number}:{batch_index}".
        If Intaris receives a duplicate key, it returns success without
        re-appending. This prevents duplicate events in conversation
        history when a network timeout causes a controller retry.
        """
        ...

    async def read_events(
        self,
        session_id: str,
        after_seq: int = 0,
        limit: int = 0,
        types: list[str] | None = None,
        last_n: int | None = None,
    ) -> EventReadResult:
        """
        Read events from session event store.
        Used by controller for context assembly (conversation history).
        """
        ...

    async def get_last_seq(
        self,
        session_id: str,
    ) -> int:
        """Get the last event sequence number for a session."""
        ...

    # Intaris MCP proxy

    async def call_mcp_tool(
        self,
        session_id: str,
        server_name: str,
        tool_name: str,
        arguments: dict,
    ) -> ToolResult:
        """
        Call a remote MCP tool through Intaris proxy.
        Intaris evaluates safety AND executes in one call.
        """
        ...


class EvaluationResult(BaseModel):
    call_id: str
    decision: str                    # "approve", "deny", "escalate"
    reasoning: str | None
    risk: str | None                 # "low", "medium", "high", "critical"
    path: str | None                 # "fast", "critical", "llm", "alignment"
    latency_ms: int
    injection_detected: bool = False
    session_status: str | None
    status_reason: str | None
    # NOTE: intention is NOT in evaluate response. Use get_session() to read it.


class SessionEvent(BaseModel):
    type: str                        # See VALID_EVENT_TYPES below
    data: dict                       # Arbitrary JSON, max 1 MB


class EventAppendResult(BaseModel):
    ok: bool
    count: int
    first_seq: int
    last_seq: int


class EventReadResult(BaseModel):
    events: list[dict]               # [{seq, ts, type, data, source}, ...]
    last_seq: int
    has_more: bool


class IntarisSession(BaseModel):
    session_id: str
    user_id: str
    agent_id: str
    intention: str | None            # The authoritative intention
    details: dict | None
    policy: dict | None
    status: str
    total_calls: int
    approved_count: int
    denied_count: int
    escalated_count: int
    parent_session_id: str | None
    created_at: str
    updated_at: str
```

### Intaris Event Types

Intaris event types for session recording. These must be in
`VALID_EVENT_TYPES` (Intaris prerequisite to extend):

| Type | Content | Written By |
|------|---------|-----------|
| `user_message` | `{content, role: "user"}` | Cognis |
| `assistant_message` | `{content, role: "assistant", token_usage: {...}}` | Cognis |
| `tool_call` | `{tool_name, arguments, call_id}` | Cognis |
| `tool_result` | `{call_id, output, is_error, duration_ms}` | Cognis |
| `evaluation` | `{call_id, decision, risk, reasoning}` | Intaris (auto) |
| `delegation` | `{mode, child_session_id, task, status, result_summary}` | Cognis |
| `compaction_summary` | `{summary, tokens_before, tokens_after, turns_compacted}` | Cognis |
| `message` | Generic message (existing) | Any |
| `reasoning` | Intention tracking (existing) | Intaris (auto) |
| `checkpoint` | State checkpoint (existing) | Cognis |
| `lifecycle` | Session lifecycle events (existing) | Any |

### Intaris HTTP Endpoints (Verified)

#### POST /api/v1/intention (Create Session)

Request:
```json
{
  "session_id": "string",
  "intention": "string",
  "details": {},
  "policy": {"allow_tools": [], "deny_tools": [], ...},
  "parent_session_id": "string | null"
}
```
Headers: `X-Agent-Id` required.
Response: `{"ok": true}`
Note: Create-only. Duplicate → 409. Does NOT update existing sessions.

#### POST /api/v1/evaluate

Request:
```json
{
  "session_id": "string",
  "tool": "string",
  "args": {},
  "context": {}
}
```
Note: `context` is a **dict**, not string. Field is named `tool` (not `tool_name`),
`args` (not `arguments`).

Response:
```json
{
  "call_id": "string",
  "decision": "approve | deny | escalate",
  "reasoning": "string",
  "risk": "string",
  "path": "string",
  "latency_ms": 0,
  "injection_detected": false,
  "session_status": "string | null",
  "status_reason": "string | null"
}
```

#### POST /api/v1/reasoning

Request:
```json
{
  "session_id": "string",
  "content": "User message: actual user text here",
  "context": "string | null"
}
```
Note: Content MUST be prefixed with `"User message: "` to trigger intention
regeneration.

Response: `{"ok": true, "call_id": "string"}`

#### GET /api/v1/session/{session_id}

Returns full session state including `intention` (authoritative).

#### POST /api/v1/decision (Resolve Escalation)

Request: `{"call_id": "string", "decision": "approve | deny", "note": "..."}`
Response: `{"ok": true}`

#### GET /api/v1/audit (List Escalations)

Query: `?decision=escalate&resolved=false`
Returns list of pending escalation records.

#### POST /api/v1/session/{id}/events (Record Events)

Request: `[{"type": "user_message", "data": {"content": "..."}}]`
Headers: `X-Intaris-Source: cognis`
Query params: `idempotency_key` (optional, format `{session_id}:{turn_number}:{batch_index}`)
Response: `{"ok": true, "count": 1, "first_seq": 1, "last_seq": 1}`

#### GET /api/v1/session/{id}/events (Read Events)

Query params: `after_seq`, `limit`, `type` (comma-separated), `source`,
`exclude_source`, `after_ts`, `before_ts`, `last_n` (Intaris prerequisite).

`last_n` is mutually exclusive with `after_seq` and `limit`.

Response:
```json
{
  "events": [{"seq": 1, "ts": "...", "type": "...", "data": {}, "source": "..."}],
  "last_seq": 10,
  "has_more": false
}
```

### Intaris Auth

Cognis authenticates to Intaris using **JWT with audience claims**:
- `Authorization: Bearer <jwt>` — JWT issued by Cognis
- JWT `sub` = user email (maps to Intaris `user_id`)
- JWT `aud` includes `"intaris"` — audience claim prevents token misuse
- `X-Agent-Id: <agent>` header for agent identity

JWT validation in Intaris is a Phase 0 prerequisite (I5). Intaris accepts
both JWT and API key for backward compatibility with standalone usage.

### Integration Flow

```
Session Start:
  1. POST /api/v1/intention (create Intaris session)
  2. Store intaris_session_id in Cognis sessions table

Each User Message:
  1. POST /api/v1/reasoning (content="User message: {text}")
  2. Intention updates asynchronously in Intaris

Each Tool Call:
  1. POST /api/v1/evaluate (tool, args, context)
  2. If escalate:
     a. Notify user via WebSocket
     b. Wait (with escalation_timeout_seconds)
     c. User resolves → POST /api/v1/decision
     d. Or timeout → treat as denied

After Turn:
  1. POST /session/{id}/events (record user_message, assistant_message, etc.)
  2. GET /api/v1/session/{id} to read back updated intention (cache in session)

Context Assembly:
  1. GET /session/{id}/events (after_seq=last_compaction_seq, type filter)
  2. Build conversation history from events
```

### Failure Modes

| Integration | Failure | Behavior |
|-------------|---------|----------|
| Mnemory recall | Graceful degradation | Continue without memory; warn user |
| Mnemory remember | Bounded retry queue | Enqueued with backoff; see Remember Retry Queue |
| Intaris evaluate | **Fail-closed** | Block tool execution; inform user |
| Intaris reasoning | Continue | Intention tracking degraded |
| Intaris events record | Retry | Session content may have gap |
| Intaris events read | Retry then degrade | Use cached compaction summary only |
| LLM | Retry + fallback | Retry, fallback model, then fail |

### Remember Retry Queue

The `remember()` call is not on the hot path — memory extraction is
best-effort enrichment. But silent loss during Mnemory outages is
unacceptable. A bounded retry queue prevents both data loss and
Mnemory overload.

```python
class RememberRetryQueue:
    """Bounded async queue for failed remember() calls."""

    max_depth: int = 100              # Max pending items
    max_concurrent: int = 5           # Max parallel retries
    max_retries: int = 5              # Per item
    backoff_base: float = 2.0         # Exponential backoff base (seconds)
    backoff_max: float = 60.0         # Max backoff per retry
    drain_on_shutdown: bool = True    # Attempt flush on graceful shutdown
```

Behavior:

1. On `remember()` failure: enqueue with retry metadata (attempt count,
   next retry time).
2. Background async task drains the queue with rate limiting
   (`max_concurrent` parallel calls).
3. Exponential backoff per item: 2s, 4s, 8s, 16s, 32s (capped at 60s).
4. If queue is full (`max_depth`): drop the **oldest** entry (least
   likely to still be useful). Emit `cognis_remember_queue_dropped_total`
   metric.
5. After `max_retries` exhausted: drop the item. Emit
   `cognis_remember_queue_failed_total` metric and log a warning
   (session_id only — no content).
6. On graceful shutdown: attempt to flush remaining queue with a bounded
   timeout (10s). Items that fail are lost (acceptable — they would be
   lost on crash anyway).

MVP: in-memory queue. Phase 2+: Redis-backed queue (survives restarts,
shared across controller replicas).

## LLM Providers

### Design Principle

LiteLLM is the default, pragmatic choice for cloud providers. But the
architecture does not depend on it. Our `LLMProvider` protocol is the
contract. LiteLLM is one backend implementation.

Use LiteLLM where it adds value:
- Multi-provider abstraction (unified API across OpenAI, Anthropic, Google, etc.)
- Cost tracking / pricing database
- Fallback / retry logic
- Provider-specific quirks handling

Use direct provider SDKs or passthrough when:
- Provider-specific features needed (Anthropic prompt caching, extended thinking)
- LiteLLM doesn't support the feature yet
- Simple local endpoint (no abstraction needed)
- Custom executor handles LLM internally

### LLMProvider Protocol (Our Abstraction)

```python
class LLMProvider(Protocol):
    """Cognis's LLM abstraction. NOT tied to LiteLLM."""

    async def complete(
        self,
        messages: list[dict],
        model: str | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        tools: list[dict] | None = None,
        stream: bool = True,
        **kwargs,
    ) -> LLMResponse | AsyncIterator[LLMChunk]:
        ...

    async def get_cost(self, usage: TokenUsage, model: str) -> Cost:
        ...

    def list_models(self) -> list[ModelInfo]:
        ...

    def count_tokens(self, text: str, model: str) -> int:
        """
        Count tokens for the given text and model.

        Resolution:
        1. Use provider-native tokenizer when available:
           - tiktoken for OpenAI models
           - Anthropic tokenizer for Claude models
        2. Fallback: len(text) // 4 (conservative estimate)

        Used by: ContextAssembler for dynamic token budget management,
        CompactionStrategy for threshold detection.
        """
        ...
```

### Token Counting Strategy

Context assembly requires knowing token counts for system prompts, tool
schemas, memory context, compaction summaries, and recent events. The
`count_tokens()` method on `LLMProvider` is the single entry point.

Implementation priority:
- **tiktoken** for OpenAI-family models (accurate, fast, well-maintained).
- **Anthropic tokenizer** for Claude models when available.
- **Conservative fallback** (`len(text) // 4`) for unknown models or when
  no tokenizer is available.

Static budget (system prompt + tool schemas + skill instructions) is
recomputed when tools or skills change, cached for the session lifetime.
Dynamic budget (remaining space for history, memory, user message) is
computed on every context assembly call.

### LLM Provider Configuration (First-Class Entity)

```python
class LLMProviderConfig(BaseModel):
    """A configured LLM provider — stored in the llm_providers DB table."""

    provider_id: str                     # "openai", "local-ollama", "claude-code"
    display_name: str

    # Where inference runs
    location: str                        # "controller" | "executor"

    # Implementation backend
    backend: str = "litellm"
    # "litellm"     — LiteLLM library (cloud multi-provider, cost tracking)
    # "direct"      — Native provider SDK (for provider-specific features)
    # "passthrough"  — Simple HTTP to OpenAI-compatible endpoint
    # "executor"    — Custom executor handles LLM internally

    # For litellm backend:
    litellm_provider: str | None = None  # "openai", "anthropic", "azure", etc.

    # For direct backend:
    sdk: str | None = None               # "anthropic", "openai"

    # For passthrough/executor-side openai_compatible:
    api_base: str | None = None          # Endpoint URL

    # Auth
    api_key_secret: str | None = None    # Secret name for API key
    oauth: OAuthProviderConfig | None = None  # For OAuth-based (ChatGPT)

    # Executor linkage (for location="executor")
    executor_labels: dict[str, str] | None = None

    # Model catalog — what this provider offers
    models: list[ModelInfo] = []
    default_model: str | None = None

    status: str = "active"


class ModelInfo(BaseModel):
    """A model exposed by a provider, with capabilities."""

    model_id: str                        # "gpt-4.1-mini", "llama3.3"
    display_name: str | None = None
    context_window: int = 128000
    max_output_tokens: int = 16384

    # Capabilities
    supports_tools: bool = True
    supports_streaming: bool = True
    supports_vision: bool = False
    supports_reasoning: bool = False
    reasoning_efforts: list[str] = []    # ["low", "medium", "high"]

    # Provider-specific capabilities
    supports_prompt_caching: bool = False
    supports_extended_thinking: bool = False

    # Cost (from LiteLLM pricing DB or manual override)
    input_cost_per_mtok: float | None = None
    output_cost_per_mtok: float | None = None

    # Model tier for dynamic routing
    tier: str = "standard"               # "nano", "mini", "standard", "large", "reasoning"


class OAuthProviderConfig(BaseModel):
    """OAuth 2.0 configuration for providers like ChatGPT subscription."""

    flow: str = "authorization_code"
    auth_url: str
    token_url: str
    client_id_secret: str                # Secret name
    client_secret_secret: str            # Secret name
    token_secret: str                    # Where to store OAuth tokens
    scopes: list[str] = []
```

### LLM Router

The `LLMRouter` is the unified entry point for all LLM calls — agent loops,
system tasks (compaction, classification), and future ecosystem proxy:

```python
class LLMRouter:
    """Routes LLM calls to the appropriate backend."""

    async def complete(self, messages, model=None, task_type="default",
                       agent=None, session=None, **kwargs):
        # 1. Resolve model from: explicit → agent config → routing policy → default
        resolved_model = self._resolve_model(task_type, agent, model)

        # 2. Find provider hosting this model
        provider = self._find_provider(resolved_model)

        # 3. Route based on backend
        match provider.backend:
            case "litellm":
                return await self._call_litellm(provider, resolved_model, messages, **kwargs)
            case "direct":
                return await self._call_direct_sdk(provider, resolved_model, messages, **kwargs)
            case "passthrough":
                return await self._call_passthrough(provider, resolved_model, messages, **kwargs)
            case "executor":
                executor = self._get_executor_for_provider(provider)
                return await executor.llm_complete(messages, model=resolved_model, **kwargs)
```

### Model Routing Policy

System-wide dynamic model selection for different task types:

```python
class ModelRoutingPolicy(BaseModel):
    """Which models for which task types."""
    default: str | None = None       # General agent work
    classifier: str | None = None    # Decision Engine (fast, cheap)
    compaction: str | None = None    # Context compaction
    simple_inline: str | None = None # Short inline responses
```

Agents can override this per-agent in their `AgentLLMConfig.model_routing`.

### Backend Implementations

```python
# LiteLLM backend — cloud multi-provider abstraction
class LiteLLMBackend(LLMProvider):
    """Uses LiteLLM library. Default for cloud providers."""
    # Multi-provider, cost tracking, fallbacks, retry
    ...

# Direct SDK backend — for provider-specific features
class DirectBackend(LLMProvider):
    """Uses native provider SDK (anthropic, openai packages)."""
    # Anthropic prompt caching, extended thinking, etc.
    ...

# Passthrough backend — simple HTTP, no LiteLLM overhead
class PassthroughBackend(LLMProvider):
    """Simple HTTP POST to any OpenAI-compatible endpoint."""
    # For local ollama, vllm, self-hosted LiteLLM proxy
    ...

# Executor backend — routes to inference-capable executor
class ExecutorBackend(LLMProvider):
    """Routes llm.complete to executor via WebSocket JSON-RPC."""
    # For executor-side inference (local models, custom executors)
    ...
```

### Provider Configuration Examples

```yaml
# Example LLM provider configurations (stored in llm_providers DB table,
# managed via Settings > LLM Providers in the UI or POST /api/v1/llm-providers)
llm_providers:
  # OpenAI via LiteLLM — full cost tracking, multi-model
  - provider_id: "openai"
    display_name: "OpenAI"
    location: controller
    backend: litellm
    litellm_provider: openai
    api_key_secret: "OPENAI_API_KEY"
    default_model: "gpt-4.1-mini"
    models:
      - model_id: "gpt-4.1-nano"
        tier: nano
        context_window: 128000
      - model_id: "gpt-4.1-mini"
        tier: mini
        context_window: 1000000
        supports_reasoning: true
        reasoning_efforts: [low, medium, high]
      - model_id: "gpt-4.1"
        tier: standard
        context_window: 1000000
        supports_reasoning: true

  # Anthropic via direct SDK — for prompt caching, extended thinking
  - provider_id: "anthropic"
    display_name: "Anthropic"
    location: controller
    backend: direct
    sdk: anthropic
    api_key_secret: "ANTHROPIC_API_KEY"
    models:
      - model_id: "claude-sonnet-4-20250514"
        tier: standard
        supports_reasoning: true
        supports_prompt_caching: true
        supports_extended_thinking: true

  # Local ollama — passthrough, no LiteLLM overhead
  - provider_id: "local-ollama"
    display_name: "Local Ollama"
    location: executor
    backend: passthrough
    api_base: "http://localhost:11434/v1"
    executor_labels: {"cognis.io/inference": "true"}
    default_model: "llama3.3"
    models:
      - model_id: "llama3.3"
        tier: standard
        context_window: 128000
      - model_id: "qwen3"
        tier: mini

  # ChatGPT subscription via OAuth
  - provider_id: "chatgpt"
    display_name: "ChatGPT Subscription"
    location: controller
    backend: litellm
    litellm_provider: openai
    oauth:
      flow: authorization_code
      auth_url: "https://auth.openai.com/authorize"
      token_url: "https://auth.openai.com/token"
      client_id_secret: "CHATGPT_CLIENT_ID"
      client_secret_secret: "CHATGPT_CLIENT_SECRET"
      token_secret: "chatgpt_oauth_token"
    models:
      - model_id: "chatgpt-4o"
        tier: standard

  # Custom executor (Claude Code with user's subscription)
  - provider_id: "claude-code"
    display_name: "Claude Code"
    location: executor
    backend: executor
    executor_labels: {"type": "claude-code"}
    models:
      - model_id: "claude-sonnet-4"
        tier: standard

model_routing:
  classifier: "gpt-4.1-nano"
  compaction: "gpt-4.1-mini"
  simple_inline: null                  # Use agent's default
```

## Secrets Provider

```python
class SecretsProvider(Protocol):
    async def get_secret(self, name: str, user_id: str, agent_id: str | None = None) -> str:
        ...
    async def set_secret(self, name: str, value: str, user_id: str, scope: str = "user", ...) -> None:
        ...
    async def resolve_for_execution(self, agent: AgentDefinition, user_id: str) -> dict[str, str]:
        """Resolve all secrets an agent's executor needs (for MCP servers)."""
        ...
```

Secrets are resolved by the controller at executor spawn time and passed in
`ExecutorConfig.secrets`. Executors use them for MCP server authentication.

## Intaris Prerequisites (Phase 0)

Before Cognis development starts, Intaris needs:

| # | Change | Description | Est. |
|---|--------|-------------|------|
| I1 | Extend VALID_EVENT_TYPES | Add: user_message, assistant_message, delegation, compaction_summary | 1 day |
| I2 | UI formatting for new types | Review Intaris console/UI rendering for new event types | 1 day |
| I3 | Reverse read / last_n | Add `last_n` parameter to GET /session/{id}/events (S3-aware) | 2-3 days |
| I4 | last_seq endpoint | Expose last_seq via API (or include in empty read response) | 0.5 days |
| I5 | JWT validation | Add ES256 JWT middleware alongside existing API key auth. Accept public key file path or JWKS URL. Extract user_id from `sub` claim, agent_id from `agent_id` claim. | 1-2 days |
| I6 | Event recording idempotency | Support optional `idempotency_key` on event append. If a duplicate key is received, return success without re-appending. Prevents duplicate events on controller retry after timeout. | 1 day |

## Mnemory Prerequisites (Phase 0)

| # | Change | Description | Est. |
|---|--------|-------------|------|
| M1 | JWT validation | Add ES256 JWT middleware alongside existing API key auth. Accept public key file path or JWKS URL. Extract user_id from `sub` claim, agent_id from `agent_id` claim or `X-Agent-Id` header. | 1-2 days |
