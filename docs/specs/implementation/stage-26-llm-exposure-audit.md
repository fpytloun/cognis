# Stage 26: LLM-Exposure Auditing in Intaris

## Status

DONE

## Goal

Ship the coordinated Intaris + Cognis change that audits every message the
LLM sees and anchors the immutable session prefix in Intaris as a
`context_snapshot` event. Delete `memory_stale` and the per-field TTL logic
that caused spurious degraded turns.

This stage implements [`../26-llm-exposure-audit.md`](../26-llm-exposure-audit.md).

Single landing. No feature flag. No phased rollout.

## Dependencies

- `docs/specs/05-integrations.md`
- `docs/specs/06-tool-system.md`
- `docs/specs/13-nfr-operations.md`
- `docs/specs/14-workflow-engine.md`
- `docs/specs/23-harness-stabilization.md`
- `docs/specs/24-provider-stabilization.md`
- `docs/specs/25-harness-polish.md`
- `docs/specs/26-llm-exposure-audit.md`
- Intaris-side schema change (coordinated; lands together with this stage)

## Scope

### In Scope

- Intaris event vocabulary extension:
  - `system_message`, `developer_message`
  - `context_snapshot` anchor
- Cognis producers and consumer changes:
  - per-turn audit of outgoing messages
  - prefix reconstruction from Intaris alone
  - bootstrap, repair, compaction, fork lifecycle emitting `context_snapshot`
- Removal of `memory_stale`:
  - delete gap-fill block
  - drop per-field TTL fields
  - force `include_instructions=False` on per-turn recall
- Hard-fail semantics on missing core memories
- Telemetry updates

### Out of Scope

- Cognis DB schema changes
- Mnemory API additions
- Admin audit UI inside Cognis
- Workflow deliverables and step profiles (deferred to Stage 31)

## Deliverables

### 1. Intaris event vocabulary

- Accept and persist `system_message`, `developer_message`, `context_snapshot`.
- Preserve payload metadata: `role`, `content`, `content_type`, `source`,
  `turn_id`, `position`, `hash`.
- Preserve ordering guarantees.
- Audit UI filters by type and `source`.
- Size caps consistent with existing message events.

### 2. Cognis per-turn audit

- `cognis/core/agent_loop.py` emits one batched `record_events` call per
  turn containing the new message events (minus dedup) before the LLM call.
- `cognis/core/context.py` produces the ordered `(role, content, source)`
  list used by the agent loop.

### 3. Prefix lifecycle via `context_snapshot`

- Bootstrap on new session — emit constituent messages and a
  `context_snapshot` with `source="bootstrap"`.
- Repair on missing anchor — call `load_session_identity(existing)`, emit
  constituents plus `context_snapshot` with `source="repair"`.
- Compaction — emit the new `compaction_summary` and a `context_snapshot`
  with `source="compaction"` on the new session.
- Fork — inherit from parent and emit `context_snapshot` with
  `source="fork"`.

### 4. Per-turn recall policy

- `include_instructions=False` everywhere on the per-turn path.
- `search_mode="search"`.
- The recall response's `instructions` and `core_memories` are ignored by
  contract.
- Search results injected as a `developer_message` with `source="memory_search"`.

### 5. Hard-fail on missing core memories

- New `ImmutablePrefixUnavailable` error in `cognis/core/errors.py`.
- `TurnScheduler` maps it to `TurnError(code="immutable_prefix_unavailable")`.
- Emit SYSTEM_NOTICE on EventBus and a `system_message` event with
  `source="system_notice"` in Intaris.

### 6. `memory_stale` removal

- Delete the gap-fill block in `ContextAssembler.assemble`.
- Drop `memory_instructions_cached_at` and `core_memories_cached_at`.
- Remove any emission of `memory_stale`.

### 7. Session cache rebuild from Intaris

- `_cold_load` identifies the latest `context_snapshot` event and applies the
  referenced constituents to rebuild the immutable prefix.
- Redis L2 is a cache only; loss is safe.

### 8. Provider protocol extension

- `MemoryProvider.load_session_identity(...)` in `cognis/providers/base.py`.
- Implementation in `cognis/providers/memory/mnemory.py`.

### 9. Telemetry

- Remove `memory_stale`.
- Add counters listed in the spec.

### 10. Tests

- Unit and integration coverage for:
  - correct per-turn audit emissions and dedup
  - prefix reconstruction from Intaris
  - per-turn recall ignoring `instructions`/`core_memories`
  - hard-fail when core memories are missing
  - grep-enforced absence of `memory_stale`

## Suggested Work Breakdown

### Workstream A: Intaris schema change

Coordinated with the Intaris repo. Lands before or with the Cognis change.

- add event type validation for `system_message`, `developer_message`,
  `context_snapshot`
- persistence and ordering
- audit UI filters

### Workstream B: Cognis producer path

Files likely touched:

- `cognis/core/context.py`
- `cognis/core/agent_loop.py`
- `cognis/core/session.py`
- `cognis/core/compaction.py`
- `cognis/core/workflow_engine.py`

Tasks:

1. Implement `_ensure_immutable_prefix` and prefix lifecycle.
2. Emit per-turn audit events.
3. Emit `context_snapshot` at each boundary.
4. Translate hard-fail errors to TurnError.

### Workstream C: Cognis consumer and cache

Files likely touched:

- `cognis/core/session_cache.py`
- `cognis/core/context.py`

Tasks:

1. Drop per-field TTL logic.
2. Apply the latest `context_snapshot` during cold load.
3. Remove `memory_stale` paths.

### Workstream D: Provider extension

Files likely touched:

- `cognis/providers/base.py`
- `cognis/providers/memory/mnemory.py`
- `cognis/providers/guardrails/intaris.py`

Tasks:

1. Add `load_session_identity`.
2. Accept the new event types on the record/read helpers.

### Workstream E: Tests and telemetry

Files likely touched:

- `tests/unit/test_context.py`
- `tests/unit/test_session_cache.py`
- `tests/unit/test_mnemory_provider.py`
- `tests/integration/test_session_context_snapshot.py`
- `tests/integration/test_llm_exposure_audit.py`

Tasks:

1. Regression tests for absence of `memory_stale`.
2. Prefix rebuild from Intaris alone.
3. Hard-fail path emits system notice and aborts turn cleanly.
4. Per-turn audit event count and ordering matches outgoing messages.

## Acceptance Criteria

- Intaris accepts and audits the new event types.
- Cognis records every message the LLM sees as a typed Intaris event.
- Immutable prefix is reconstructible from Intaris alone.
- Per-turn Mnemory recall does not modify the immutable prefix.
- Missing core memories hard-fail turns with visible system notice and
  audit trail.
- No code or tests reference `memory_stale` after this stage.
- Cognis chat UI does not display the new audit events.
