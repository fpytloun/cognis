# Stage 22: Harness Prompt Cache and Operational Resilience

## Status

DONE

## Goal

Refine prompt composition, prompt caching, memory freshness, and operational
resilience so the stabilized harness remains efficient and safe under long
sessions, restarts, and multi-replica deployment.

This stage implements Track C from
[`../23-harness-stabilization.md`](../23-harness-stabilization.md).

## Dependencies

- `docs/specs/03-session-model.md`
- `docs/specs/05-integrations.md`
- `docs/specs/06-tool-system.md`
- `docs/specs/13-nfr-operations.md`
- `docs/specs/23-harness-stabilization.md`
- Stages 20 and 21 complete

## Scope

### In Scope

- immutable-prefix cleanup and prompt-cache stability
- deterministic skills metadata and cache breakpoint placement
- memory TTL/freshness correctness
- skill-load context protection
- durable remember queue
- multi-replica-safe follow-up dedupe
- Intaris endpoint isolation and local decision caching/batching groundwork

### Out of Scope

- typed deliverables
- step profiles
- broad runtime abstraction changes

## Deliverables

### 1. Prompt-cache cleanup

- move stable project instructions into the immutable prefix
- keep mutable date/environment data outside the cached region
- anchor cache breakpoints to stable tool-schema edges
- sort skills metadata deterministically

### 2. Skill and context handling

- treat loaded skill instructions as protected context rather than ordinary tool
  output
- reduce unnecessary cache busting from skill discovery

### 3. Memory freshness and tool hardening

- enforce correct memory instruction/core-memory TTL refresh behavior
- route memory tools through provider methods with retry and breaker handling
- make stale-memory behavior visible and deterministic

### 4. Durable recovery primitives

- persist remember queue items durably
- replay pending remember work after restart
- replace in-memory-only follow-up dedupe with a multi-replica-safe mechanism

### 5. Guardrails operational refinement

- split Intaris circuit breakers by endpoint
- add local short-lived decision caching or batch-evaluate groundwork where safe

### 6. Tests and telemetry

- prompt stability tests
- durable queue recovery tests
- follow-up dedupe tests under restart/multi-replica assumptions
- telemetry for cache stability, remember replay, and decision-cache hits

## Suggested Work Breakdown

### Workstream A: Prompt-cache stabilization

Files likely touched:

- `cognis/core/context.py`
- `cognis/core/tool_exposure.py`
- `cognis/tools/skills.py`
- `cognis/providers/llm/litellm.py`

Tasks:

1. Move stable project instructions into the immutable prefix.
2. Stabilize skill ordering and cache anchor placement.
3. Extend Anthropic prefix cache TTL where appropriate.

### Workstream B: Memory freshness and durability

Files likely touched:

- `cognis/core/context.py`
- `cognis/core/session_cache.py`
- `cognis/core/remember_queue.py`
- `cognis/providers/memory/mnemory.py`
- `cognis/tools/builtin/memory.py`

Tasks:

1. Fix TTL refresh semantics.
2. Move memory tool calls behind provider methods.
3. Persist remember queue work and replay on restart.

### Workstream C: Multi-replica and Intaris resilience

Files likely touched:

- `cognis/core/turn_scheduler.py`
- `cognis/providers/guardrails/intaris.py`
- `cognis/core/tool_router.py`

Tasks:

1. Replace in-memory follow-up dedupe.
2. Split circuit breakers by Intaris endpoint.
3. Add local decision-cache or batch-evaluate foundation.

## Acceptance Criteria

- stable project instructions contribute to prefix-cache reuse
- skill ordering and discovery do not cause unnecessary cache churn
- memory instructions refresh correctly after TTL expiry
- remember work survives restart and is replayed safely
- follow-up dedupe is no longer single-replica only
