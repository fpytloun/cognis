# Stage 6: Agent Loop + Delegation

**Status**: NOT STARTED
**Repo**: `cognis`
**Depends on**: Stage 4 (executor + tools) AND Stage 5 (orchestration core)
**Estimated effort**: 4-5 days

## Objective

Implement the agent loop engine that runs complete chat turns: context
assembly, LLM call, response processing, tool execution, and turn
finalization. Add delegation support so the main session can spawn
background sub-sessions for heavy work while staying responsive.

## Deliverables

### 1. Agent Loop Engine

- `cognis/core/agent_loop.py`
  - Main loop for a single session (main or delegated):
    1. Receive user message
    2. Decision Engine classifies (foreground or delegate)
    3. If delegate: create child session, acknowledge, start child loop
    4. Context assembly (parallel fetches via ContextAssembler)
    5. LLM call (streaming via LLMProvider)
    6. Process response:
       - Text → stream to client
       - Tool call → Tool Router dispatch → feed result back → loop
       - Orchestration tool → handle as controller operation
    7. Finalize turn:
       - Record events to Intaris (with idempotency_key)
       - Append events to session cache
       - Remember to Mnemory (via retry queue)
       - Check compaction threshold
  - Maximum tool calls per turn (from settings)
  - Turn timeout handling

### 2. LLM Streaming

- Stream tokens from LLMProvider to the caller
- Accumulate full response for event recording
- Handle tool call responses (function calling format)
- Track token usage per turn

### 3. Session Locking

- `SessionLock` — one active turn per session at a time
- Async lock keyed by session_id
- Prevents concurrent turns in the same session
- Different sessions run fully concurrently

### 4. Concurrent Loop Management

- Manager tracks all active agent loops
- Start/stop loops by session_id
- Enforce concurrency limits:
  - Max concurrent sessions (global)
  - Max concurrent delegations per session
  - Max delegation depth
- Clean shutdown: signal all loops, wait for finalization

### 5. Delegation

- Three modes:
  - **Agent**: delegate to a different agent (different persona, tools)
  - **Worker**: delegate to same agent (same tools, focused objective)
  - **Fork**: parallel exploration (same context, branched)
- Delegation flow:
  1. LLM requests delegation via orchestration tool
  2. Decision Engine validates (within depth limit, allowed by policy)
  3. Controller creates child session
  4. Controller starts child agent loop concurrently
  5. Main session receives acknowledgment + progress events
  6. Child loop completes → structured result
  7. Controller synthesizes result into main session context
- Result delivery: structured format (summary, detailed_output, artifacts,
  memory_refs, confidence, follow_up_suggestions)

### 6. Escalation Handling

- When Intaris returns `decision=escalate` for a tool call:
  1. Task enters waiting state
  2. Push `escalation` event to client
  3. Start countdown timer (from `escalation_timeout_seconds`)
  4. Wait for user decision (approve/deny via WebSocket or REST)
  5. On approve: continue tool execution
  6. On deny: inform LLM of denial
  7. On timeout: deny (configurable default)

### 7. Event Recording

- Batch events at turn finalization:
  - `user_message`
  - `assistant_message` (accumulated from stream)
  - `tool_call` + `tool_result` for each tool execution
  - `delegation` events for child sessions
- Include `idempotency_key` for retry safety
- Append same events to session cache after Intaris confirms

## Acceptance Criteria

- [ ] Agent loop runs a complete chat turn: context → LLM → response → finalize
- [ ] LLM streaming delivers tokens incrementally to caller
- [ ] Tool calls route through Intaris evaluate → executor → result → LLM
- [ ] Multiple tool calls in a single turn work correctly
- [ ] Session lock prevents concurrent turns in same session
- [ ] Delegation creates child session and runs concurrent loop
- [ ] Child loop result returns to parent session
- [ ] Delegation depth limit enforced
- [ ] Escalation pauses execution and waits for resolution
- [ ] Events recorded to Intaris with idempotency key
- [ ] Session cache updated after event recording
- [ ] Remember dispatched to retry queue after turn
- [ ] Compaction triggered when threshold exceeded
- [ ] Turn respects max_tool_calls_per_turn limit
- [ ] Unit tests for loop flow, delegation, escalation
- [ ] `ruff check` and `mypy` clean

## Key References

- `docs/specs/01-architecture.md` — agent loop, concurrency model
- `docs/specs/03-session-model.md` — turn lifecycle (steps 1-6), delegation
- `docs/specs/04-controller-executor.md` — controller/executor interaction
- `docs/specs/06-tool-system.md` — tool routing, trust model
