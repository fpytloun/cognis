# Stage 9: Integration Testing + Polish

**Status**: IN PROGRESS

## Implementation Notes (Partial)

- Built integration test infrastructure under `tests/integration/` with
  auto-bootstrapped ES256 keys, subprocess Mnemory + Intaris via `uvx`,
  and Cognis in-process via `TestClient` — all using isolated temp dirs.
- Fixed 3 backend bugs discovered during integration testing:
  1. Auth middleware `except Exception` was catching route handler errors
     and returning 401 (moved JWT verification try/except before `call_next`).
  2. Agent creation failed hard when Mnemory personality bootstrap timed out
     (now gracefully degrades with a warning log).
  3. Mnemory httpx client timeout was 10s, insufficient for embedded Qdrant
     first-write initialization (increased to 30s).
- 16 integration tests pass without a live server (API surface, agent CRUD,
  health, degradation, settings, workflows, secrets, JWKS, tools, LLM
  providers, escalation endpoint, recovery, task dependencies, performance).
- 10 tests requiring WebSocket + LLM streaming are marked `live_server` and
  deferred to Phase 2 (Starlette TestClient cannot handle async agent loop
  + LLM streaming; needs Cognis as a subprocess too).
- Accessibility polish deferred note carried from Stage 8.
**Repo**: `cognis`
**Depends on**: Stage 8 (all functionality must be wired)
**Estimated effort**: 3-4 days

## Objective

Verify the full system works end-to-end with real Mnemory and Intaris
instances. Exercise all MVP flows, verify degraded-mode behavior, and
confirm the success criteria from the roadmap are met.

## Deliverables

### 1. Integration Test Suite

Tests in `tests/integration/` that run against a full Cognis stack with
live Mnemory and Intaris. Each test exercises a complete user flow.

#### Core Chat Flow
- Create agent → create conversation → send message → receive streaming
  response → verify events recorded in Intaris → verify remember dispatched
  to Mnemory

#### Memory Integration
- Chat multiple turns → verify Mnemory recall returns relevant context
- Create agent → verify personality bootstrapped to Mnemory
- Long conversation → verify compaction → verify recall still works

#### Guardrails Integration
- Tool call → Intaris evaluate → approved → executed → result in response
- Risky tool call → escalation → user approves → execution continues
- Risky tool call → escalation → timeout → denied
- Non-bypassable tool → always goes through Intaris regardless of permissions

#### Delegation Flow
- Request that triggers delegation → child session created → background
  execution → result returned to parent → synthesized in main chat
- Verify delegation depth limit enforced
- Verify concurrent delegations respect limits

#### Reconnection
- Chat → disconnect WebSocket → reconnect with last_seq → verify
  missed events replayed

#### Escalation Timeout
- Tool escalated → countdown starts → user resolves before timeout → OK
- Tool escalated → countdown expires → denied

### 2. Error Handling / Degradation Tests

- Mnemory unavailable → chat continues without memory → degraded flag set
- Intaris unavailable → tool calls blocked → user informed
- LLM provider unavailable → fallback model tried → if all fail, error
- Executor failure → retry → inform LLM on persistent failure

### 3. Session Recovery Test

- Start Cognis → create active session → kill Cognis process (simulate crash)
- Restart Cognis → verify stale sessions detected → SESSION_RECOVERED event
- Resume session → verify context rebuilt from Intaris cache

### 4. Graceful Shutdown Test

- Start turn → send SIGTERM → verify in-flight turn finalizes
- Verify events flushed to Intaris before exit
- Verify remember queue drained (best-effort)

### 5. Compaction Test

- Long conversation (30+ turns) → verify compaction triggers automatically
- Verify compaction summary appears in context assembly
- Verify pre-compaction turns are summarized, not lost
- Verify mechanical fallback works when compaction LLM fails

### 6. Performance Baseline

- Measure P95 time-to-first-token for follow-up turns
  (target: <= 2.5s from 13-nfr-operations.md)
- Measure P95 context assembly latency for warm cache
  (target: <= 1200ms)
- Measure concurrent session capacity (target: 50 concurrent)
- Record baseline for future regression tracking

### 7. Contract Test Refresh

- Re-run contract tests from Stage 0 against current Mnemory/Intaris
- Verify no API drift since initial contract tests were written

## MVP Success Criteria Verification

From `docs/specs/12-mvp-roadmap.md` — all must pass:

- [ ] `uvx cognis` starts with zero config, first-start setup URL works
- [ ] User can log in and configure LLM provider via Settings UI
- [ ] User can create an agent with name, personality, LLM config
- [ ] Agent chats with streaming responses
- [ ] Memory works (agent recalls past context via Mnemory)
- [ ] Guardrails work (tool calls evaluated, escalations appear via Intaris)
- [ ] Delegation works (heavy request → background task → result returns)
- [ ] Main chat remains responsive during delegation
- [ ] Delegation results appear in conversation
- [ ] Secrets management works (add API keys, used by MCP tools)
- [ ] Context compaction works in long conversations
- [ ] Cross-service UI access works (Intaris/Mnemory links with token exchange)

## Acceptance Criteria

- [ ] All integration tests pass against live Mnemory + Intaris
- [ ] Degradation tests confirm correct behavior per provider failure
- [ ] Session recovery test passes (crash → restart → resume)
- [ ] Graceful shutdown test passes (SIGTERM → clean exit)
- [ ] Compaction test passes (30+ turns → summary → correct context)
- [ ] Performance baseline recorded and within NFR targets
- [ ] Contract tests still pass (no API drift)
- [ ] All 12 MVP success criteria verified
- [ ] No critical or high-severity bugs remaining

## Key References

- `docs/specs/12-mvp-roadmap.md` — success criteria
- `docs/specs/13-nfr-operations.md` — latency targets, degraded modes
- `docs/specs/03-session-model.md` — recovery, retention, compaction
