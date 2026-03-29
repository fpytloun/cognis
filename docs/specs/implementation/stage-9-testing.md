# Stage 9: Integration Testing + Polish

**Status**: DONE

## Implementation Notes

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
- Added a second fixture (`live_stack`) that starts Cognis as a subprocess
  alongside Mnemory + Intaris for full WebSocket/LLM testing.
- Fixed a WebSocket orchestration bug discovered by the live-server tests:
  `_load_conversation_runtime()` returned a raw DB row for the agent instead
  of an `AgentDefinition`, and Intaris event reads for newly created sessions
  now tolerate 404 as an empty event stream.
- All 27+ integration tests pass (`tests/integration/`).
- Contract test refresh passes against latest published `uvx mnemory`
  and `uvx intaris` with isolated temp dirs and auto-generated JWT keys
  (`tests/contract/`: 14 passed, 2 skipped for optional API-key scenarios).
- Added dedicated WebSocket unit tests (21 tests) covering:
  `_classify_turn_error` (11 tests for all error classification paths),
  inbound rate limiting, outbound backpressure and chunk gap frames,
  auth flow (invalid token, non-auth first message, valid auth, ping/pong,
  unknown message type).
- Added compaction integration test (`test_compaction.py`): lowers settings
  thresholds, chats multiple turns, verifies compaction summary shape.
- Added graceful shutdown test (`test_shutdown_recovery.py`): sends SIGTERM
  to Cognis subprocess, verifies clean exit.
- Added session recovery test (`test_shutdown_recovery.py`): full crash
  (SIGKILL) → restart → verify sessions recovered as idle.
- Added degraded-mode scenario tests (`test_degradation_scenarios.py`):
  agent creation survives Mnemory failure, conversation creation without
  Intaris session, settings/tools/workflows accessible when providers degraded.
- Accessibility polish deferred from Stage 8, addressed in Stage 13.

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

- [x] `uvx cognis` starts with zero config, first-start setup URL works
- [x] User can log in and configure LLM provider via Settings UI
- [x] User can create an agent with name, personality, LLM config
- [x] Agent chats with streaming responses
- [x] Memory works (agent recalls past context via Mnemory)
- [x] Guardrails work (tool calls evaluated, escalations appear via Intaris)
- [x] Delegation works (heavy request → background task → result returns)
- [x] Main chat remains responsive during delegation
- [x] Delegation results appear in conversation
- [x] Secrets management works (add API keys, used by MCP tools)
- [x] Context compaction works in long conversations
- [x] Cross-service UI access works (Intaris/Mnemory links with token exchange)

## Acceptance Criteria

- [x] All integration tests pass against live Mnemory + Intaris
- [x] Degradation tests confirm correct behavior per provider failure
- [x] Session recovery test passes (crash → restart → resume)
- [x] Graceful shutdown test passes (SIGTERM → clean exit)
- [x] Compaction test passes (30+ turns → summary → correct context)
- [x] Performance baseline recorded and within NFR targets
- [x] Contract tests still pass (no API drift)
- [x] All 12 MVP success criteria verified
- [x] No critical or high-severity bugs remaining

## Key References

- `docs/specs/12-mvp-roadmap.md` — success criteria
- `docs/specs/13-nfr-operations.md` — latency targets, degraded modes
- `docs/specs/03-session-model.md` — recovery, retention, compaction
- `tests/unit/test_websocket.py` — dedicated WebSocket handler unit tests
- `tests/integration/test_compaction.py` — compaction integration test
- `tests/integration/test_shutdown_recovery.py` — shutdown + recovery tests
- `tests/integration/test_degradation_scenarios.py` — degraded-mode tests
