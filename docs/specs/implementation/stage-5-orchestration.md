# Stage 5: Orchestration Core

**Status**: DONE

## Implementation Notes

- Session manager with full lifecycle: create, recover stale, archive,
  soft-delete, purge. Descendant failure cascade on recovery.
- Session cache (L1 in-memory) for Intaris-derived state with incremental
  fetch, warm-cache fallback, and compaction application.
- Context assembler with parallel `asyncio.gather()` for memory recall,
  event refresh, and intention read. Graceful degradation per source.
- Compaction strategy: LLM-based summarization with mechanical fallback,
  idempotent Intaris writes, configurable threshold and preserve_turns.
- Decision engine: deterministic rules + LLM classifier for inline vs
  delegation routing.
- Remember retry queue: bounded async queue for failed Mnemory writes.

**Repo**: `cognis`
**Depends on**: Stage 3 (providers)
**Can run in parallel with**: Stage 4
**Estimated effort**: 4-5 days

## Objective

Implement session management, the tiered session cache, parallel context
assembly, compaction, and the Decision Engine. After this stage, the
controller can create sessions, assemble LLM context from cached Intaris
events + Mnemory recall, classify turns as foreground or delegated, and
compact long conversation histories.

## Progress Notes

- Stage 5 orchestration-core implementation is complete and locally validated.
- Implemented: session lifecycle management, in-memory session cache,
  parallel context assembly, LLM/mechanical compaction, and the decision
  engine.
- Added a small session lifecycle migration for `sessions.updated_at` and
  `sessions.idle_since` so stale-session recovery is correct for long-lived
  sessions.
- LiteLLM routing now exposes Stage 5 helpers for task-type model resolution,
  model metadata lookup, and structured message token counting with a short
  in-memory cache.
- Session cache metrics follow the NFR names and the cache is bounded with
  least-recently-used eviction.
- Local validation passed with `uv run pytest tests/unit/ -v`,
  `uv run ruff check cognis/ tests/`, and `uv run mypy cognis/`.
- Limitation: conversation purge currently removes Cognis metadata and evicts
  cache state, but Intaris event-store purge remains deferred until a
  verified delete-session provider contract exists.

## Deliverables

### 1. Session Manager

- `cognis/core/session.py`
  - Create conversation + root session
  - Create child session (delegation)
  - Create Intaris session (via GuardrailsProvider)
  - Create Mnemory session (via first recall, lazy)
  - Store correlation IDs: `intaris_session_id`, `mnemory_session_id`
  - Session lifecycle: active → idle → completed / failed
  - Session recovery on startup (scan stale active sessions)
  - Conversation archive / delete / purge (local metadata purge implemented;
    Intaris cascade deferred until provider contract support exists)

### 2. Session Cache

- `cognis/core/session_cache.py`
  - L1 in-memory cache keyed by `session_id`
  - Cached state: events (since compaction), last_event_seq,
    last_compaction_seq, last_compaction_summary, intention
  - Cold path: full read from Intaris on first access
  - Warm path: incremental `after_seq` fetch (append-only, immutable events)
  - Intention: read-through at turn start (mutable, may change via
    Intaris reasoning)
  - Update on event recording: append to buffer, bump seq
  - Update on compaction: new summary, trim pre-compaction events
  - Eviction: LRU or on session completion/idle timeout
  - Cache metrics: hits, misses, size, evictions

### 3. Context Assembler

- `cognis/core/context.py`
  - Parallel external fetches via `asyncio.gather()`:
    1. Mnemory recall (query=user message, context=cached intention)
    2. Intaris event refresh (incremental after_seq)
    3. Intaris intention read (get_session)
  - Partial failure: continue with available results, flag degraded
  - Build message list:
    1. System prompt (from agent definition)
    2. Memory context (wrapped as untrusted)
    3. Compaction summary (from cache)
    4. Recent events (from cache)
    5. Active delegation statuses
    6. User message
  - Dynamic token budget:
    - Static budget: system prompt + tool schemas (cached per session)
    - Dynamic budget: remaining space for history + memory + message
    - Token counting via LLMProvider `count_tokens()` +
      `count_messages_tokens()`

### 4. Compaction

- `cognis/core/compaction.py`
  - Trigger: when context exceeds `compaction_threshold` (default 85%)
  - LLM-based compaction: summarize older turns via cheap model
  - Mechanical fallback: if LLM fails, drop oldest turns keeping metadata
  - Write compaction_summary event to Intaris
  - Update session cache (new summary, trim events)
  - Preserve last N turns uncompacted (default 10)
  - No Cognis DB write — cache + Intaris only

### 5. Decision Engine

- `cognis/core/decision.py`
  - Layer 1: Deterministic rules
    - Short messages → foreground
    - Explicit prefixes (/research, /implement, etc.) → delegate
    - Pure conversation → foreground
  - Layer 2: Lightweight LLM classifier
    - Cheap model via LLM provider
    - Returns: foreground / delegate / ask_user
    - Timeout (500ms) with fallback to foreground
  - Layer 3: User override
    - Explicit keywords ("just answer", "run in background")
    - UI controls (Reply now / Create task)
  - Classification output envelope: decision, reason, confidence,
    predicted_tool_intensity, override_source
  - Metrics: classifications by decision, latency

## Acceptance Criteria

- [x] Session manager creates conversations + sessions with Intaris/Mnemory correlation
- [x] Session cache cold path loads from Intaris correctly
- [x] Session cache warm path fetches incrementally (no full re-read)
- [x] Cache updates correctly on event recording and compaction
- [x] Context assembler runs 3 external fetches in parallel
- [x] Context assembly degrades gracefully on partial failure
- [x] Token budget computed correctly (static + dynamic split)
- [x] LLM-based compaction produces summary and updates Intaris + cache
- [x] Mechanical compaction fallback works when LLM fails
- [x] Decision Engine classifies obvious cases via rules
- [x] Decision Engine uses LLM classifier for ambiguous cases
- [x] Classifier fallback to foreground on timeout
- [x] Session recovery scans stale sessions on startup
- [x] Unit tests for cache, context assembly, compaction, decision engine
- [x] `ruff check` and `mypy` clean

## Key References

- `docs/specs/01-architecture.md` — session cache architecture, concurrency model
- `docs/specs/03-session-model.md` — turn lifecycle, context assembly, compaction, recovery
- `docs/specs/13-nfr-operations.md` — latency targets for context assembly
