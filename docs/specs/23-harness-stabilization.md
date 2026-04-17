# Cognis: Harness Stabilization and Capability Refinement

## Purpose

This spec captures the stabilization and refinement work required to turn the
current Cognis harness into a production-grade controller for coding,
research, and workflow-heavy agent execution.

The current architecture is directionally correct: controller-owned sessions,
executor-owned tool execution, deferred tool exposure, Intaris-enforced
guardrails, Mnemory-backed memory, and workflow-driven task execution.
The review also found several real issues that must be resolved before the
next structural layer such as typed deliverables and step profiles lands.

This spec defines three near-term tracks:

1. correctness and concurrency stabilization
2. capability parity for real coding/research harness use
3. prompt-cache, memory, and operational resilience refinement

Deliverables and step profiles remain valid future work, but they are
explicitly deferred until these tracks are complete.

## Related Specs

- [`03-session-model.md`](03-session-model.md)
- [`05-integrations.md`](05-integrations.md)
- [`06-tool-system.md`](06-tool-system.md)
- [`13-nfr-operations.md`](13-nfr-operations.md)
- [`14-workflow-engine.md`](14-workflow-engine.md)
- [`17-agent-runtimes.md`](17-agent-runtimes.md)
- [`18-runtime-contract.md`](18-runtime-contract.md)
- [`19-runtime-implementation-plan.md`](19-runtime-implementation-plan.md)
- [`20-auto-routing-implementation-plan.md`](20-auto-routing-implementation-plan.md)
- [`21-workflow-deliverables.md`](21-workflow-deliverables.md)
- [`22-step-profiles.md`](22-step-profiles.md)

## Why This Stage Comes First

The review found five classes of gaps that affect correctness and real-world
operator confidence more than missing product features:

1. shared mutable state and recovery paths that can corrupt session data
2. missing harness primitives expected from best-in-class coding agents
3. prompt and token accounting issues that waste cache and context budget
4. weak multi-replica and crash-recovery behavior
5. workflow/task semantics that are sound in design but fragile in failure

Adding typed deliverables or restrictive tool profiles on top of these issues
would increase complexity before the base harness is reliable enough.

## Review Findings Summary

### A. Correctness and Concurrency

- `AgentLoop` keeps `_pending_events` on a singleton instance instead of on
  step-local state, so concurrent turns can clobber one another.
- Intaris recovery loops can wait forever and hold the session lock.
- workflow gate restart currently risks waiter/notification mismatch.
- scheduler timezone handling is DST-incorrect.
- interruption and malformed-tool-call recovery paths can produce invalid
  tool-call / tool-result history.
- Mnemory calls can silently fall back to a shared default user identity if
  runtime context is absent.

### B. Capability Parity

- tool calls are always executed sequentially even when the model emits
  parallel-safe batches.
- grep/glob implementations are far behind `ripgrep`/`fd` quality and speed.
- bash lacks background processes, process-group cleanup, and stable polling.
- finish reasons, truncation, and step timeouts are not enforced end-to-end.
- compaction can fail to make progress on tool-heavy sessions.
- evaluator and passive-stop behavior are too brittle for lower-reliability
  models.

### C. Prompt Cache and Operational Resilience

- stable project instructions are excluded from the immutable prefix.
- tool-schema cache breakpoints are placed on unstable edges.
- memory instructions can remain stale far beyond the intended TTL.
- follow-up dedupe and session cache behavior are still single-replica biased.
- remember queue durability is in-memory only.
- Intaris evaluate overhead is paid call-by-call with no batching or local
  decision cache.

## Design Principles

### 1. Fix data integrity before adding new workflow contracts

Any state that is specific to a step, turn, pause, or child execution must be
owned by that unit of work, not by a long-lived singleton.

### 2. Match model capabilities with harness capabilities

If providers emit parallel tool calls, stop reasons, or usage metadata, the
harness must consume them correctly rather than degrade them into simpler
behavior.

### 3. Prefer token-aware budgeting everywhere

Context windows, truncation, compaction, and prompt-cache breakpoints should be
based on real or provider-specific token counts, not rough character heuristics.

### 4. Preserve determinism for caching

Any prompt content intended to benefit from prefix caching must be stable in
ordering, placement, and content boundaries across turns.

### 5. Multi-replica safety is an explicit target

Where the current implementation is intentionally single-process or
single-replica, the limitation must either be removed or clearly isolated so it
cannot silently corrupt behavior under horizontal deployment.

## Scope

### In Scope

- singleton-state removal and step-localization of transient execution state
- bounded retry and timeout behavior for Intaris, LLM streaming, and workflow
  execution
- gate and pause recovery correctness after restart
- parallel execution for parallel-safe read-only tools
- ripgrep/fd-backed search with graceful fallback
- background shell sessions with process-group cleanup
- token-aware truncation and improved finish-reason handling
- compaction progress guarantees on tool-heavy sessions
- prompt-cache stabilization and immutable-prefix cleanup
- durable remember queue and multi-replica follow-up dedupe groundwork
- memory provider hardening and explicit failure semantics
- evaluator and passive-stop robustness improvements

### Out of Scope

- typed deliverables and `write_deliverable`
- step profiles and tool classification filtering
- workflow composability primitives such as sub-workflows or parallel step
  groups
- full runtime abstraction changes from specs 17-19 (deferred Stages 27-29)
- product/UI redesign outside what is needed to surface stabilization behavior

## Track A: Correctness and Concurrency Stabilization

### Goals

- eliminate cross-session shared mutable state inside the harness
- ensure every waiting/retry loop has bounded exit conditions
- make restart recovery deterministic for gates and paused tasks
- remove latent cross-user memory leakage paths

### Required Changes

1. Move `_pending_events` and related flush state off `AgentLoop` and onto
   `StepContext` or another per-step structure.
2. Bound Intaris recovery loops with retry ceilings and total timeout.
3. Reuse persisted gate `pause_id` on restart instead of creating a new one.
4. Change gate timeout default from `continue` to `fail` and make it explicit.
5. Fix scheduler timezone handling to preserve IANA timezone semantics across
   DST.
6. Ensure interruption paths synthesize tool results for unexecuted queued
   tool calls when needed to preserve history validity.
7. Remove default Mnemory user fallback and fail closed when user context is
   absent.
8. Make memory recall failure semantics explicit instead of silently diverging
   from the "mandatory" intent.

### Acceptance Criteria

- concurrent turns for separate sessions cannot corrupt each other's pending
  event flush state
- Intaris outage eventually returns a structured failure instead of hanging a
  session forever
- gate approval after restart resolves the active workflow run, not an orphaned
  waiter
- cron schedules remain stable across DST boundaries in integration tests
- no memory write or search can execute under an implicit shared user identity

## Track B: Capability Parity for Coding and Research Harnesses

### Goals

- close the largest gaps versus Claude Code / Cursor class harnesses
- reduce latency on read-heavy tool batches
- improve shell and search ergonomics for real engineering workflows
- make loop termination and truncation semantics explicit

### Required Changes

1. Execute `read_only=True` tool batches in parallel when emitted in the same
   assistant message.
2. Replace Python grep/glob hot paths with `ripgrep`/`fd` when available,
   falling back cleanly when missing.
3. Add background shell sessions with `shell_id`, output polling, and kill
   support.
4. Launch shell commands in their own process group and terminate the whole
   group on timeout/cancel.
5. Carry finish reason / stop reason through the LLM stack and react to
   `length`, `content_filter`, and `tool_use` distinctly.
6. Add true step-level timeout enforcement.
7. Replace character-based truncation budgets with token-aware budgets.
8. Make compaction fall back to a progress-making strategy when user-turn based
   splitting is a no-op.
9. Improve passive-stop handling for workflow steps so omission of
   `step_complete` does not immediately consume a step attempt.
10. Tighten evaluator behavior so timeouts and malformed outputs do not silently
    approve work.

### Acceptance Criteria

- batches of read-only tool calls run concurrently and preserve deterministic
  result ordering in the transcript
- grep and glob are fast enough on large repositories without scanning `.venv`
  and `node_modules` by default
- long-running shell commands can be started, polled, and cancelled safely
- a `length`-truncated response is detected and continued rather than accepted
  as complete
- workflow steps time out predictably and surface a structured failure reason

## Track C: Prompt Cache, Memory, and Operational Resilience

### Goals

- improve prompt-cache hit rate and reduce unnecessary cache busting
- make memory freshness semantics match the documented intent
- make follow-up, remember, and session-cache behavior safer under restarts and
  multi-replica deployment

### Required Changes

1. Move stable project instructions into the immutable prefix.
2. Keep mutable environment/date content outside the cached prefix.
3. Anchor Anthropic/OpenAI tool-cache boundaries to stable schema edges.
4. Sort skills metadata deterministically.
5. Treat loaded skill instructions as protected context, not as ordinary tool
   output subject to pruning.
6. Make memory instruction/core-memory TTL refresh behavior explicit and
   correct.
7. Route memory tools through provider methods with retry/circuit-breaker
   protection.
8. Persist remember queue items durably instead of only in memory.
9. Replace in-memory-only follow-up dedupe with a multi-replica-safe mechanism.
10. Separate Intaris circuit breakers by endpoint and add local short-lived
    decision caching or batching where safe.

### Acceptance Criteria

- AGENTS/README project instructions benefit from prefix caching across turns
- skill discovery or ordering changes do not cause unnecessary prompt cache
  misses
- stale memory instructions are refreshed instead of persisting indefinitely
- a controller restart does not lose pending remember work
- follow-up suppression remains correct in a multi-replica deployment model

## Implementation Order

These tracks are intentionally sequenced before typed deliverables and step
profiles.

1. Track A: correctness and concurrency stabilization
2. Track B: capability parity for coding/research harnesses
3. Track C: prompt cache, memory, and operational resilience
4. deferred structural work:
   - typed workflow deliverables
   - step profiles and tool classification
   - workflow composability improvements

## Testing Requirements

Each track must extend automated coverage, not rely on manual verification.

### Unit Tests

- concurrent turn state isolation
- bounded retry / timeout behavior
- pause-id reuse and restart recovery
- parallel tool batch scheduling
- finish-reason handling
- token-budget truncation
- prompt-prefix stability and cache breakpoint placement
- memory TTL refresh and provider failure semantics

### Integration Tests

- Intaris outage and recovery during a live step
- controller restart with pending gate and paused task
- large-repo grep/glob behavior with and without `rg`/`fd`
- background shell lifecycle
- compaction on tool-heavy sessions
- durable remember queue recovery after restart

### Contract Tests

- Mnemory failure and header semantics
- Intaris evaluate/record-events behavior under retry and breaker conditions

## Telemetry

Add or tighten telemetry for the following cases:

- step timeout
- Intaris retry exhaustion
- gate timeout action
- finish-reason distribution (`stop`, `length`, `content_filter`, `tool_use`)
- parallel tool batch size and latency
- search backend used (`rg`, `fd`, python fallback)
- background shell lifecycle
- compaction no-op fallback activation
- remember queue persisted / retried / failed / replayed
- follow-up dedupe hits and suppressions

Content, prompts, tool arguments, and memory bodies remain excluded from logs.

## Deferred Structural Work

The following specs remain valid but are intentionally not part of this phase:

- [`21-workflow-deliverables.md`](21-workflow-deliverables.md)
- [`22-step-profiles.md`](22-step-profiles.md)

They should be implemented only after Tracks A-C are complete and the harness
is stable enough to support stricter workflow contracts without layering new
failure modes on top of old ones.
