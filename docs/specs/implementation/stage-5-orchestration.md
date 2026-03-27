# Stage 5: Orchestration Core

**Status**: NOT STARTED
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
  - Conversation archive / delete / purge with Intaris cascade

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
    - Token counting via LLMProvider.count_tokens()

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

- [ ] Session manager creates conversations + sessions with Intaris/Mnemory correlation
- [ ] Session cache cold path loads from Intaris correctly
- [ ] Session cache warm path fetches incrementally (no full re-read)
- [ ] Cache updates correctly on event recording and compaction
- [ ] Context assembler runs 3 external fetches in parallel
- [ ] Context assembly degrades gracefully on partial failure
- [ ] Token budget computed correctly (static + dynamic split)
- [ ] LLM-based compaction produces summary and updates Intaris + cache
- [ ] Mechanical compaction fallback works when LLM fails
- [ ] Decision Engine classifies obvious cases via rules
- [ ] Decision Engine uses LLM classifier for ambiguous cases
- [ ] Classifier fallback to foreground on timeout
- [ ] Session recovery scans stale sessions on startup
- [ ] Unit tests for cache, context assembly, compaction, decision engine
- [ ] `ruff check` and `mypy` clean

## Key References

- `docs/specs/01-architecture.md` — session cache architecture, concurrency model
- `docs/specs/03-session-model.md` — turn lifecycle, context assembly, compaction, recovery
- `docs/specs/13-nfr-operations.md` — latency targets for context assembly
