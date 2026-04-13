# Cognis: Non-Functional Requirements and Operations

## Purpose

This document defines measurable targets, degraded-mode behavior, and
operational expectations for Cognis. It addresses gaps identified in
architecture reviews (Reviews 2026-03-27, scores 57 and 74) where
reliability, resilience, operability, and data integrity were rated 2-3/5
due to missing SLOs, recovery procedures, and retention policies.

## Latency Targets

### Context Assembly

Context assembly is the critical hot path — it runs on every turn before the
LLM call starts. It involves sequential external calls.

Context assembly runs external fetches **in parallel** (`asyncio.gather`):
Mnemory recall, Intaris event refresh, and Intaris intention read execute
concurrently. Total latency is bounded by the slowest single call, not
the sum.

| Phase | Target (P95) | Notes |
|-------|-------------|-------|
| Memory recall (Mnemory) | <= 1000ms | `search` mode for follow-ups; `find` mode (2 LLM calls) for first turn |
| Event read (Intaris) | <= 200ms warm, <= 1000ms cold | Warm = cache hit + incremental; cold = S3/filesystem |
| Intention read (Intaris) | <= 100ms | Simple DB read |
| **Total context assembly** | **<= 1200ms follow-up, <= 3000ms first turn** | Parallel fetch; bounded by slowest call |

### User-Facing Latency

| Metric | Target (P95) | Notes |
|--------|-------------|-------|
| Time-to-first-LLM-token (follow-up turn) | <= 2.5s | Parallel context assembly + LLM provider latency |
| Time-to-first-LLM-token (first turn) | <= 4.5s | Parallel assembly with Mnemory `find` mode + cold event read |
| Delegation acknowledgment in chat | <= 1s | After Decision Engine classifies as delegate |
| Task visibility in UI | <= 2s | After task creation |
| Classification (Decision Engine) | <= 500ms | Fast rules < 1ms; LLM classifier <= 500ms with fallback to inline |

### Internal Operations

| Metric | Target (P95) | Notes |
|--------|-------------|-------|
| Compaction (LLM-based) | <= 10s | Summarize N turns; not on hot path |
| Compaction (mechanical fallback) | <= 500ms | Drop oldest turns; emergency path |
| Mnemory remember | fire-and-forget | Async; retry queue absorbs failures |
| Intaris event recording | <= 500ms | Batch at turn finalization |

## Throughput and Concurrency

### MVP Targets (Single Controller)

| Resource | Limit | Notes |
|----------|-------|-------|
| Concurrent WebSocket connections | 100 | Configurable via `max_connections` |
| Concurrent active sessions (agent loops) | 50 | Main sessions + delegations |
| Concurrent delegations per session | 3 | Per-agent configurable |
| Max delegation depth | 5 | Prevents runaway chains |
| Queued messages per session | 5 | Beyond limit: reject |
| Max tool calls per turn | 50 | Per-agent configurable |

### Scaling Path

Phase 1 (MVP): single controller process handles all sessions.

Phase 1.5 (current): single controller replica with Redis-backed L2
session cache for restart resilience and future multi-replica readiness.
S3-backed tool output storage for shared/persistent tool outputs.

Phase 2+: multiple controller replicas with sticky WebSocket sessions,
distributed turn scheduling, and Redis-backed shared session state.

## Availability and Degradation

### Target Availability

MVP: no formal SLA. Target is "rarely down during working hours."

Production (Phase 2+): 99.5% uptime for the controller API. Excludes
planned maintenance windows.

### Degraded Modes

The system should not be fully down when a single dependency is unavailable.
Degradation should be explicit and visible to users.

| Dependency Down | Impact | Behavior |
|----------------|--------|----------|
| **Mnemory** | No memory recall or remember | Chat continues without memory context; responses carry internal `degraded_context` flag; remember queue buffers writes for retry; user sees "memory unavailable" indicator |
| **Intaris (evaluate)** | **No tool execution** | Fail-closed. Chat can continue for pure conversation (no tools). Tool calls blocked. User informed. |
| **Intaris (event read)** | No conversation history load | If cached compaction summary exists in memory, use it for degraded context assembly. If no cache: block turn with error. |
| **Intaris (event record)** | Turn events not persisted | Buffer events in memory; retry on recovery. If buffer exceeds limit, fail the turn. Never silently drop recorded events. |
| **LLM provider** | No model inference | Retry with fallback model per routing policy. If all providers down: inform user. |
| **Executor** | No tool execution | Retry spawn. On persistent failure: inform LLM, inform user. |
| **Redis (when used)** | No L2 cache | Fall back to L1 in-memory cache only. Higher cold-start cost on session load. Single-replica correctness preserved; multi-replica visibility lost. |
| **MinIO/S3 (tool outputs)** | No tool output persistence | Tool outputs stored in-memory only for current session. `read_tool_output`/`search_tool_output` return "not found" for outputs from before the outage. |
| **PostgreSQL** | **Full outage** | Controller cannot function. Return 503 on all requests. |

### Circuit Breaker

Provider calls use circuit breaker pattern:

```
CLOSED → (5 failures) → OPEN → (30s recovery) → HALF_OPEN → (success) → CLOSED
```

Circuit breaker state should be visible at `/api/health` per provider.

## Recovery Objectives

### RPO (Recovery Point Objective)

| Data | RPO | Notes |
|------|-----|-------|
| Session metadata (Cognis DB) | 0 (transactional) | PostgreSQL WAL; SQLite WAL mode |
| Session events (Intaris) | <= 1 turn | Events batch-recorded at turn finalization; a crash mid-turn loses at most the current turn's events |
| Memory writes (Mnemory) | best-effort | Retry queue reduces loss window; not transactional |
| In-memory session cache (L1) | lost on restart | Rebuilt from Redis L2 or Intaris on next session access |
| Redis session cache (L2) | best-effort | Survives controller restart; lost on Redis flush; rebuilt from Intaris |
| Tool outputs (S3) | 0 (written immediately) | S3 durability; TTL-based cleanup |
| Tool outputs (filesystem) | lost on pod restart | Ephemeral; acceptable for single-session tool exploration |

### RTO (Recovery Time Objective)

| Scenario | RTO | Notes |
|----------|-----|-------|
| Controller restart | <= 30s | FastAPI startup + provider health checks |
| Session cache warm-up (per session) | <= 2s | Cold read from Intaris on first access |
| Mnemory retry queue drain | <= 5 min | Bounded retry with backoff |
| PostgreSQL recovery | depends on infra | Standard PG recovery; outside Cognis scope |

## Observability

### Metrics (Prometheus)

Runtime observability requirements for first-class runtimes:

- runtime run count by runtime type/state
- projection lag
- replay failure count
- capability mismatch count
- policy translation failure count
- stuck runtime detection count

These are in addition to the existing provider/session metrics.

#### Request-Level

```
cognis_http_requests_total{method, path, status}
cognis_http_request_duration_seconds{method, path}
cognis_ws_connections_active
cognis_ws_connections_total
cognis_ws_reconnections_total
cognis_ws_missed_events_replayed
```

#### Turn and Session

```
cognis_turns_total{agent_id, classification}
cognis_turn_duration_seconds{agent_id, phase}
    # phase: total, context_assembly, llm_call, tool_execution, finalization
cognis_context_assembly_seconds{phase}
    # phase: memory_recall, event_read, reasoning, total
cognis_active_sessions
cognis_active_delegations
cognis_delegations_total{agent_id, mode, outcome}
cognis_compactions_total{method}
    # method: llm, fallback_model, mechanical
```

#### Provider Health

```
cognis_provider_requests_total{provider, operation, status}
cognis_provider_request_duration_seconds{provider, operation}
cognis_provider_circuit_breaker_state{provider}
    # state: closed, open, half_open
cognis_executor_spawns_total{outcome}
cognis_executor_spawn_duration_seconds{outcome}
cognis_tool_route_decisions_total{route}
cognis_tool_route_outcomes_total{route, outcome}
```

#### Cache

```
cognis_session_cache_hits_total
cognis_session_cache_misses_total
cognis_session_cache_size
cognis_session_cache_evictions_total
```

#### Task Queue (Phase 2)

```
cognis_task_queue_depth{queue}
cognis_task_lease_timeouts_total
cognis_executor_heartbeat_age_seconds{executor_id}
```

#### Browser Recording and Takeover

```
cognis_browser_takeovers_total{executor_id, outcome}
cognis_browser_takeover_duration_seconds{executor_id}
cognis_browser_recording_events_total{recording_type, mode, event_type}
cognis_browser_recording_artifacts_total{recording_type, mode}
cognis_browser_recording_orphan_artifacts_total
cognis_browser_recording_upload_failures_total
```

Recording/takeover operational targets must include:

- max concurrent headed takeover sessions per executor: default 2, configurable
- p95 browser takeover handoff latency: <= 3s after approval/grant in normal
  operation
- replay availability target for retained evidence: 99.5%
- orphan cleanup for failed recording uploads and abandoned takeover sessions:
  <= 15 minutes
- recording evidence storage budget: explicit per-mode quota with alerts at 80%
  consumption

#### Cost

```
cognis_llm_tokens_total{agent_id, model, direction}
    # direction: input, output, reasoning
cognis_llm_cost_total{agent_id, model, currency}
cognis_tool_calls_total{agent_id, tool, outcome}
```

#### Mnemory Retry Queue

```
cognis_remember_queue_depth
cognis_remember_queue_retries_total
cognis_remember_queue_dropped_total
cognis_remember_queue_success_total
```

### Health Endpoint

`GET /api/health` returns:

```json
{
  "status": "healthy | degraded | unhealthy",
  "providers": {
    "memory": {"status": "up", "latency_ms": 45},
    "guardrails": {"status": "up", "latency_ms": 12},
    "llm": {"status": "up"},
    "executor": {"status": "up", "active": 2},
    "database": {"status": "up"},
    "cache": {"status": "up", "type": "memory"}
  },
  "sessions": {"active": 5, "delegations": 2},
  "remember_queue": {"depth": 0, "oldest_age_seconds": null}
}
```

- 200: all providers healthy
- 503: any critical provider unhealthy (database, guardrails)

### Structured Logging

All log entries include correlation fields:

```json
{
  "timestamp": "...",
  "level": "info",
  "message": "turn_completed",
  "conversation_id": "...",
  "session_id": "...",
  "agent_id": "...",
  "user_email": "...",
  "duration_ms": 1234
}
```

#### Content Redaction Policy

Logs and metrics MUST NOT contain:

- message content (user or assistant)
- tool call arguments or results
- memory content (recall or remember payloads)
- secret values
- raw LLM prompts or completions

Logs MAY contain:

- IDs (session, conversation, agent, user, tool call)
- tool names (not arguments)
- model names
- token counts
- latencies and durations
- status codes and error categories
- decision outcomes (approve/deny/escalate)

This policy must be enforced by a logging allowlist, not by hoping
developers remember to redact.

### Alerting (Phase 2+)

Initial alert definitions:

| Alert | Condition | Severity |
|-------|-----------|----------|
| Intaris unavailable | Circuit breaker OPEN for > 60s | Critical |
| Mnemory unavailable | Circuit breaker OPEN for > 60s | Warning |
| High turn latency | P95 context assembly > 4s for 5 min | Warning |
| Remember queue backlog | Queue depth > 50 for > 5 min | Warning |
| LLM provider errors | Error rate > 10% for 5 min | Warning |
| Session cache miss rate | Miss rate > 50% for 10 min | Info |

## Backup and Restore

### Cognis DB

- PostgreSQL: standard pg_dump / pg_restore. Daily backups recommended.
- SQLite: file copy (with WAL checkpoint first).
- Cognis DB contains metadata only; session content is in Intaris.

### Intaris

- Event store (S3): S3 versioning or cross-region replication.
- Event store (filesystem): filesystem backup.
- Intaris DB: standard database backup.
- Intaris is the authoritative store for session content and audit data.

### Mnemory

- Qdrant: snapshot API.
- Artifacts: S3 backup or filesystem copy.
- Mnemory is the authoritative store for persistent memory.

### Recovery Priority

1. Cognis DB (restores system state, sessions, agents)
2. Intaris DB + event store (restores conversation content and audit)
3. Mnemory (restores memory; loss is degraded but not fatal)

## Database Migration Strategy

Use Alembic for SQLAlchemy-managed migrations:

- `cognis/store/migrations/` directory with Alembic configuration
- SQLite for local dev, PostgreSQL for production
- Migration compatibility tested against both backends
- All migrations must be reversible (downgrade support)
- Schema changes go through spec review before implementation
