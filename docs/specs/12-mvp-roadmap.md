# Cognis: MVP Roadmap

## MVP Goal

Deploy a usable system as fast as possible. A single user can chat with an AI
agent that has persistent memory (Mnemory), safety guardrails (Intaris), and
the ability to delegate work to sub-sessions while the main chat stays
responsive.

## Prerequisites (Before Cognis Development)

### Intaris Changes Required

| # | Change | Est. |
|---|--------|------|
| I1 | Extend `VALID_EVENT_TYPES`: add `user_message`, `assistant_message`, `delegation`, `compaction_summary` | 1 day |
| I2 | Review Intaris UI event formatting for new types | 1 day |
| I3 | Add `last_n` parameter + reverse read to `GET /session/{id}/events` (S3-aware) | 2-3 days |
| I4 | Expose `last_seq` in event read API | 0.5 days |
| I5 | JWT validation middleware (ES256, file + JWKS, backward-compatible with API keys) | 1-2 days |
| I6 | Event recording idempotency (optional `idempotency_key` on event append) | 1 day |

### Mnemory Changes Required

| # | Change | Est. |
|---|--------|------|
| M1 | JWT validation middleware (ES256, file + JWKS, backward-compatible with API keys) | 1-2 days |

### Contract Tests

Before writing Cognis provider code, write contract tests that call real
Mnemory/Intaris APIs with the expected request/response shapes. This
surfaces mismatches in Week 0, not Week 5.

## MVP Scope

### In Scope

- Single user authentication (JWT, email as user_id)
- First-start bootstrap (one-time setup URL, 15 min TTL)
- CLI admin commands (Typer: create-user, reset-password, api-key)
- Single primary agent (configurable via API/UI)
- Interactive chat with streaming responses (WebSocket)
- Non-blocking main chat (continue chatting during delegations)
- **Controller runs all agent loops** (brain)
- **In-process executor handles all tool calls** (hands)
- Three delegation modes: Agent, Worker, Fork
- Decision Engine (rules + LLM classifier)
- LLM delegation tools (delegate, spawn_worker, fork) as requests to system
- Mnemory integration (recall/remember, controller-mediated, JWT auth)
- Intaris integration (evaluate/reasoning/events, controller-mediated, JWT auth)
- Session content stored in Intaris event store (NOT in Cognis)
- Session cache (L1 in-memory for Intaris-derived state, no DB dual-write)
- LiteLLM for multi-provider LLM support
- MCP tool support (local + Intaris-managed remote)
- Built-in tools (orchestration + system)
- Non-bypassable tool safety for destructive operations
- Trust model for injected content (structural wrapping + size limits)
- Context window management with compaction (LLM + mechanical fallback)
- Token counting (tiktoken / fallback)
- Remember retry queue (bounded, rate-limited)
- Encrypted secrets store
- SQLite for Cognis metadata
- SvelteKit web UI: chat, agent CRUD, settings
- Delegation status cards and escalation UI
- WebSocket reconnection protocol
- Plugin hook system (basic events)
- Configuration via env vars (infra) + DB/API (app settings). No config file.
- Zero-config local deployment (`uvx cognis`, auto-generated keys, ~/.cognis)
- Health check, structured logging, Prometheus metrics
- JWKS endpoint for JWT key distribution
- Cross-service UI access via token exchange

### Out of Scope (Phase 2+)

- Multi-user
- Task queue / kanban
- Scheduler / cron
- Full agent creation wizard (character creator)
- Avatar generation
- Docker / Kubernetes executors
- Local LLM inference on executor
- Node groups and label selectors
- Chat platform integrations (Slack, Discord)
- A2A federation / Agent Cards serving
- DID / cryptographic identity
- Cost tracking dashboard
- PostgreSQL
- Agent export/import (YAML)
- Interactive CLI chat (`cognis chat`) — terminal rendering complexity
- Redis L2 cache for session state

## Implementation Plan

### Phase 0: Prerequisites (Week 0)

- Intaris changes I1-I6 (including JWT validation and event idempotency)
- Mnemory change M1 (JWT validation)
- Contract tests for Mnemory and Intaris APIs (including JWT auth)
- Validate every endpoint Cognis will call against real services

**Done when**: contract tests pass against running Mnemory and Intaris.

### Phase 1.0: Foundation (Week 1-2)

#### 1.0.1 Project Setup
- Python package structure (pyproject.toml, cognis/, entry points for `uvx`)
- FastAPI application factory
- Environment variable configuration (no config file)
- Auto-generated keys and data directory (~/.cognis)
- Database setup (SQLite + async, Alembic migrations)
- Settings table with default seed values
- Structured logging
- Health check endpoint

#### 1.0.2 Data Models
- Pydantic models: Agent, Conversation, Session, Turn, Message
- Pydantic models: ToolDefinition, ToolCall, ToolResult
- Pydantic models: DelegationInfo, ExecutorConfig
- Pydantic models: LLMProviderConfig, ModelInfo, ModelRoutingPolicy
- DB schema: users (email PK), agents, conversations, sessions, settings,
  llm_providers, model_routing, secrets, api_keys, audit_log

#### 1.0.3 Auth + Bootstrap
- JWT implementation (ES256, auto-generated keys)
- Email as user_id (sub claim in JWT, X-User-Id to services)
- Auth middleware (JWT + API key)
- JWKS endpoint (/.well-known/jwks.json)
- First-start bootstrap (one-time setup URL, 15 min TTL)
- Token exchange endpoint for cross-service UI access

#### 1.0.4 CLI Admin Commands (Typer)
- `cognis` / `cognis serve` — start server
- `cognis admin create-user` — direct DB user creation
- `cognis admin reset-password` — direct DB password reset
- `cognis admin api-key create/list` — API key management
- `cognis config init` — print env var template
- `cognis status` — health check via API

### Phase 1.1: Provider Layer (Week 2-3)

#### 1.1.1 Provider Registry + LLM Router
- Protocol definitions (all providers)
- Provider registry (loads config from DB settings + env vars)
- LLM Router: load LLM provider configs from DB, model catalog, routing policy
- LLM backend implementations: litellm (default), passthrough, executor
- Health checking for all providers + LLM providers

#### 1.1.2 Mnemory Provider
- HTTP client (httpx)
- JWT service auth (issue JWT with aud=mnemory, sub=user email)
- recall() and remember() matching verified contract
- Session ID management (first recall creates, subsequent recalls use)
- X-Agent-Id header management (user_id from JWT sub)
- Graceful degradation on failure
- Remember retry queue (bounded, rate-limited — see 05-integrations.md)

#### 1.1.3 Intaris Provider
- HTTP client (httpx)
- JWT service auth (issue JWT with aud=intaris, sub=user email)
- create_session(), evaluate(), report_reasoning(), checkpoint()
- Event recording: record_events(), read_events()
- get_session() for intention readback
- Escalation management via audit + decision endpoints
- Fail-closed on evaluate failure

#### 1.1.4 LLM Provider
- LiteLLM wrapper (loads provider config from DB)
- Streaming completion
- Fallback model support
- Token counting (tiktoken + fallback)

#### 1.1.5 Secrets Provider
- Encrypted DB (AES-256-GCM)
- CRUD + scope resolution
- resolve_for_execution()

### Phase 1.2: Executor (Week 3)

#### 1.2.1 Executor Protocol
- ExecutorProvider protocol
- ExecutorConfig model
- JSON-RPC message types (tool.execute, tool.result, etc.)

#### 1.2.2 InProcess Executor
- Same-process executor with JSON-RPC bridge
- MCP server initialization and tool discovery
- Tool execution dispatch
- Health reporting

### Phase 1.3: Orchestration Core (Week 3-4)

#### 1.3.1 Session Manager
- Conversation and session CRUD
- Mnemory session creation (first recall at session start)
- Intaris session creation (POST /intention)
- Session correlation tracking
- Session timeout and rotation

#### 1.3.2 Session Cache
- L1 in-memory cache for Intaris-derived state (events, seq, compaction, intention)
- Cache population: cold (full Intaris read) and warm (incremental after_seq) paths
- Cache invalidation: compaction triggered by controller, intention read-through
- No Intaris-derived state in Cognis DB (see 01-architecture.md Session Cache Architecture)

#### 1.3.3 Context Assembler
- Dynamic token budget (static + dynamic split)
- Token counting via LLMProvider.count_tokens() (tiktoken / fallback)
- Load compaction summary and recent events from session cache
- Memory injection from Mnemory recall
- Delegation status assembly
- Untrusted content wrapping for tool results and memory (see 06-tool-system.md Trust Model)

#### 1.3.4 Compaction
- LLM-based compaction via _system/compaction agent
- Mechanical fallback on LLM failure
- Compaction summary stored in Intaris events; session cache updated (no Cognis DB write)

#### 1.3.5 Decision Engine
- Rules engine (explicit commands, message length, continuations)
- LLM classifier (fast model, with fallback to "inline" on failure)
- Orchestration plan construction
- Delegation tool request handling (approve/modify/reject)

### Phase 1.4: Agent Loop + Tools (Week 4-5)

#### 1.4.1 Agent Loop Engine
- Main agent loop (assemble → LLM → process → finalize)
- Concurrent loop management (main + delegations)
- LLM streaming to client
- Tool call extraction and processing
- Per-session locking (single writer)

#### 1.4.2 Tool Router
- Categorize: orchestration / intaris-mcp / local
- Permission evaluation with non-bypassable check
- Dispatch to Intaris MCP proxy or executor
- Escalation handling with timeout

#### 1.4.3 Tool System
- Tool registry (merge local + intaris + builtin)
- MCP client for local servers
- Skill loading
- Permission matching (glob patterns)

#### 1.4.4 Delegation
- Create child session (Cognis DB + Intaris + Mnemory)
- Start concurrent agent loop
- Result delivery (Intaris event + WebSocket notification)
- Three modes: agent, worker, fork (effective_agent_id handling)

### Phase 1.5: API Layer (Week 5-6)

#### 1.5.1 REST Endpoints
- Auth, conversations, agents, sessions, tools, secrets, escalations, system

#### 1.5.2 WebSocket Handler
- Client connection management
- Message routing to agent loops
- Streaming response forwarding
- Delegation event forwarding
- Escalation notification + resolution
- Message queuing (max 5, batch on turn complete)
- Reconnection protocol (replay missed events via session cache)

#### 1.5.3 Event Bus
- Internal pub/sub
- Hook registration (before/after/async)

### Phase 1.6: UI (Week 6-8)

#### 1.6.1 SvelteKit Setup
- Project structure, API client, WebSocket client, auth flow

#### 1.6.2 Chat Page
- Message list with streaming
- Markdown + syntax highlighting
- Delegation status cards
- Tool call indicators
- Escalation prompts with countdown
- Message queue indicator

#### 1.6.3 Agent Management
- Agent list, create form, edit, detail

#### 1.6.4 Settings
- Secrets CRUD, connection status, LLM providers

### Phase 1.7: Testing + Polish (Week 8)

- Integration tests: full chat flow with Mnemory + Intaris
- Delegation flow: worker, agent, fork
- Escalation + timeout flow
- Context compaction test (long conversations)
- Error handling: service failures, graceful degradation
- Load test: concurrent conversations + delegations

## Dependency Graph

```
Phase 0: Intaris prereqs (I1-I5) + Mnemory (M1) + contract tests
  │
  ▼
Phase 1.0: Foundation (models, env config, auth, DB, CLI admin, bootstrap)
  │
  ▼
Phase 1.1: Providers ──────────────────────────┐
  │                                             │
  ▼                                             ▼
Phase 1.2: Executor         Phase 1.3: Orchestration Core
  │                                             │
  └─────────────────┬──────────────────────────┘
                    │
                    ▼
             Phase 1.4: Agent Loop + Tools
                    │
                    ▼
             Phase 1.5: API Layer
                    │
                    ▼
             Phase 1.6: UI
                    │
                    ▼
             Phase 1.7: Testing
```

## Success Criteria

The MVP is complete when a user can:

1. Run `uvx cognis` with zero config and complete first-start setup
2. Log in and configure an LLM provider via the Settings UI
3. Create an agent with name, personality, LLM config
4. Chat with streaming responses
5. See memory working (agent recalls past context via Mnemory)
6. See guardrails working (tool calls evaluated, escalations appear via Intaris)
7. Delegate work (system-driven or via LLM tools)
8. Continue chatting while delegation runs in background
9. See delegation results in conversation
10. Manage secrets (add API keys, used by MCP tools)
11. See context compaction work in long conversations
12. Access Intaris and Mnemory UIs via cross-service token exchange

## Risks and Mitigations

| Risk | Mitigation |
|------|-----------|
| Intaris event store read latency | Session cache (L1 in-memory) eliminates repeated reads; incremental after_seq fetches |
| Mnemory/Intaris API mismatches | Contract tests in Phase 0 catch this early |
| LLM classifier unreliability | Rules-only fallback; classifier_fallback: "inline" in config |
| MCP server instability | Timeout handling; disable problematic servers |
| Context compaction quality | Mechanical fallback; conservative thresholds |
| WebSocket reliability | Reconnection protocol with event replay (see 09-ui-ux.md); message dedup by ID |
| In-process executor not isolated | Acceptable for MVP; Docker executor in Phase 2 |
| Scope still large for 8 weeks | Ship chat page first; agent/settings can slip to week 9 |
