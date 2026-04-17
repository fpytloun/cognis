# Cognis: LLM-Exposure Auditing in Intaris

## Purpose

Intaris is the session auditing service in the Openclaw ecosystem. Today it
audits user and assistant messages and tool calls, but it does not audit the
system-role and developer-role content that actually drives the model.
Memory instructions, core memories, routing reminders, system notices,
compaction summaries, attachment notices and other in-context content flow
into the LLM without being recorded as first-class events.

This spec fixes that by extending Intaris with typed message and snapshot
events so that every message the LLM receives is persisted as an audit event,
in order, for every session.

It also removes an architectural regression introduced during Stage 22: the
immutable session prefix (agent identity, memory instructions, core memories,
compaction summary) was partially driven by a per-turn TTL cache that
produced spurious `memory_stale` degraded turns whenever Mnemory returned
instructions but not core memories. The proper fix is to stop reconstructing
the prefix from per-turn recall and to anchor it in Intaris as a
`context_snapshot` event referencing the constituent message events.

## Goals

- Audit every message that reaches the LLM, in delivery order, with precise
  event types and source tags.
- Make immutable prefix state rebuildable from Intaris alone.
- Eliminate the per-turn recall leakage into immutable prefix and remove the
  `memory_stale` degraded source entirely.
- Keep the new audit stream invisible in Cognis chat UI. Investigation happens
  in Intaris.
- Land Intaris and Cognis changes together in a single rollout with no
  feature flag.

## Non-goals

- No Cognis database schema changes.
- No Mnemory API additions.
- No admin UI inside Cognis for the new audit stream.
- No phased landing, no feature flag.
- Workflow deliverables and step profiles remain deferred.

## Event vocabulary

All four message event types share the same payload shape and are used by any
Intaris client, not just Cognis.

### Message events

- `system_message`
- `developer_message`
- `user_message` (already exists; unchanged)
- `assistant_message` (already exists; unchanged)

Shared payload fields:

- `role` — `system | developer | user | assistant`
- `content` — text payload, already sanitized by the sender
- `content_type` — defaults to `"text"`; reserved for future (`"json"`, etc.)
- `source` — client-defined short tag describing what produced the message;
  used for audit filtering. Cognis uses values such as:
  - `identity`
  - `project_instructions`
  - `memory_instructions`
  - `core_memories`
  - `compaction_summary`
  - `routing_reminder`
  - `follow_up_boundary`
  - `skill_load`
  - `attachment_notice`
  - `delegation_result`
  - `system_notice`
  - `workflow_step_context`
  - `workflow_gate`
  - `intention_banner`
  - `environment_info`
  - `user_input`
  - `assistant_reply`
  - `tool_reminder`
  - `memory_search`
- `turn_id` — opaque client-issued identifier for the turn this message belongs to
- `position` — integer position within the turn's outgoing message list
- `hash` — `sha256` of `{role, content, source}`; used for dedup and joins

### Context snapshot anchor

- `context_snapshot`

Payload:

- `source` — one of `bootstrap | repair | compaction | fork | manual`
- `entries` — ordered references to the constituent events in effect from
  this point forward. Each entry is `{role, source, seq}` where `seq` is the
  Intaris event seq already recorded for that message.
- `extras` — optional open dict for client-specific metadata. Cognis uses it
  for values such as `mnemory_session_id`, `personality_hash`,
  `tool_policy_hash`, `runtime_hash`.
- `captured_at` — ISO 8601 timestamp

Semantics:

- A `context_snapshot` represents "which stable context events apply to any
  turn recorded after this seq".
- A later `context_snapshot` supersedes earlier ones for prefix reconstruction.
- Compaction is expressed as a `context_snapshot` with `source="compaction"`.
  There is no separate compaction event type.

### Tool events

- `tool_call`
- `tool_result`

These remain unchanged by this spec.

## Turn audit contract (Cognis)

For every LLM call Cognis makes:

1. Cognis mints `turn_id` for the turn.
2. Cognis composes the outgoing message list as today.
3. Before issuing the LLM call, Cognis emits one batched `record_events`
   call containing, in order:
   - any new message events required for this turn (per dedup rules below)
   - if the immutable prefix was just established or changed (bootstrap,
     repair, compaction, fork, manual), a `context_snapshot` referencing the
     relevant constituent events
4. Cognis issues the LLM call.
5. Assistant output is recorded via the existing `assistant_message`,
   `tool_call`, and `tool_result` events.

### Dedup

- Immutable prefix constituents are recorded once per boundary; they are not
  re-recorded on every turn. A later `context_snapshot` points back at those
  earlier recorded events. This keeps the audit complete without bloating
  event volume.
- Transient per-turn content is always recorded. Routing reminders, intention
  banners, attachment notices, system notices, follow-up boundaries, gate
  notices, `environment_info` changes, etc.
- Idempotency keys ensure that replays of the same turn do not double-record.

## Immutable prefix reconstruction

Any Intaris client rebuilds the immutable prefix as follows:

1. Find the latest `context_snapshot` in the session.
2. Load the referenced message events by seq.
3. Compose the prefix in the client's usual order (Cognis: identity,
   project instructions, memory instructions, core memories, compaction
   summary).

Cognis caches this composition in the session cache. Redis L2 remains a cache
only. Loss of cache or Redis is safe: reconstruction uses Intaris alone.

## Per-turn recall policy

Per-turn Mnemory recall is a pure search operation:

- `include_instructions=False`
- `search_mode="search"`
- The `instructions` and `core_memories` fields of the recall response are
  ignored by contract.
- Search results become a `developer_message` event with
  `source="memory_search"` when injected into the outgoing message list.

Per-turn recall cannot change the immutable prefix.

## Prefix lifecycle

The immutable prefix is established or refreshed only at controlled boundaries:

- **Bootstrap** — new session. Cognis calls
  `MemoryProvider.load_session_identity(...)` which requests instructions and
  core memories from Mnemory. Constituent message events are recorded, then a
  `context_snapshot` with `source="bootstrap"` is appended.
- **Repair** — existing session missing a prefix anchor (for example, a
  session that predates this spec). Cognis calls `load_session_identity(...)`
  with the existing Mnemory session id. On success, writes the constituents
  plus a `context_snapshot` with `source="repair"`. A 5-minute backoff is
  enforced per session.
- **Compaction** — session rotation. Cognis records a new
  `compaction_summary` `developer_message`, carries identity/instructions/
  core_memories forward, then appends a `context_snapshot` with
  `source="compaction"` on the new session.
- **Fork** — child session inherits parent's constituents and writes its own
  `context_snapshot` with `source="fork"`.
- **Manual** — reserved for admin-triggered rebuilds.

## Hard-fail policy

Core memories are essential to agent behavior. If bootstrap or repair fails to
produce non-null `core_memories`:

- Cognis raises `ImmutablePrefixUnavailable`.
- `TurnScheduler` translates it to `TurnError` with code
  `immutable_prefix_unavailable`.
- A session-scoped `SYSTEM_NOTICE` event is published on the EventBus for
  live UI notification.
- A `system_message` event with `source="system_notice"` is recorded in
  Intaris for audit.
- No implicit `replace_bootstrap_identity` call is made. Recovery requires
  explicit operator action.

## Removal of `memory_stale`

As part of this stage:

- The `memory_stale` degraded source is removed entirely.
- The per-field TTL fields `memory_instructions_cached_at` and
  `core_memories_cached_at` on the session cache are removed.
- The gap-fill block in `ContextAssembler.assemble` that generates
  `memory_stale` is removed.
- Per-turn recall never sets `include_instructions=True`.

## Cognis-side changes

Producers:

- `cognis/core/context.py` — adds `_ensure_immutable_prefix` hook; supplies
  the ordered list of message audit events for the turn.
- `cognis/core/agent_loop.py` — calls `_audit_outgoing_messages(turn_id,
  messages)` before LLM dispatch; translates `ImmutablePrefixUnavailable` to
  TurnError.
- `cognis/core/session.py` — session creation triggers bootstrap and emits the
  initial `context_snapshot`.
- `cognis/core/compaction.py` — rotation emits the `compaction_summary` and a
  new `context_snapshot` with `source="compaction"`.
- `cognis/core/workflow_engine.py` — fork and delegation emit `context_snapshot`
  with the appropriate source.

Consumer:

- `cognis/core/session_cache.py` — rebuilds the immutable prefix from the
  latest `context_snapshot` plus referenced events; drops per-field TTL.

Provider:

- `cognis/providers/memory/mnemory.py` — adds `load_session_identity` used
  only by bootstrap and repair.
- `cognis/providers/guardrails/intaris.py` — accepts and passes through the
  new event types.

Errors:

- `cognis/core/errors.py` — adds `ImmutablePrefixUnavailable`.

UI:

- Cognis WebSocket does not translate `system_message`, `developer_message`,
  or `context_snapshot` events into chat payloads.
- Cognis chat transcript continues to omit these events.

## Intaris-side changes

Coordinated with Cognis and landing together:

- Accept and persist new event types: `system_message`, `developer_message`,
  `context_snapshot`.
- Preserve `source`, `turn_id`, `position`, `hash` metadata.
- Ordering guarantees identical to existing events.
- Audit UI exposes type and `source` filters.
- Size caps consistent with existing message events.
- Retention policy consistent with existing events.

No new Intaris endpoints are required.

## Telemetry

Remove:

- any references to `memory_stale`

Add:

- `cognis_audit_events_total{type, source}`
- `cognis_context_snapshot_state_total{state}` with `state ∈
  present|bootstrapped|repaired|missing`
- `cognis_memory_bootstrap_attempts_total{outcome}` with `outcome ∈
  ok|missing_core|error`
- `cognis_memory_repair_attempts_total{outcome}` with `outcome ∈
  ok|missing_core|cooldown|error`
- `cognis_memory_recall_total{outcome}` with `outcome ∈ ok|failed`
- `cognis_turn_aborted_total{reason}` includes `immutable_prefix_unavailable`

Logs and metrics remain redacted per existing allowlist rules.

## Acceptance criteria

- Intaris accepts and persists `system_message`, `developer_message`, and
  `context_snapshot` events with the shared payload shape.
- Cognis records every message in the outgoing LLM message list as a typed
  Intaris event, in order, with correct metadata.
- Cognis records prefix constituents once per boundary; a `context_snapshot`
  anchor pins them forward.
- A cold Cognis cache rebuilds the immutable prefix from Intaris alone.
- Per-turn Mnemory recall never sets `include_instructions=True` and cannot
  mutate the immutable prefix.
- Bootstrap or repair that fails to produce core memories hard-fails the
  turn, emits a live SYSTEM_NOTICE, and records an audit `system_message`.
- `memory_stale` no longer appears anywhere in code or tests.
- Neither `system_message`, `developer_message`, nor `context_snapshot`
  events reach Cognis chat transcripts.

## Related specs

- [`05-integrations.md`](05-integrations.md)
- [`06-tool-system.md`](06-tool-system.md)
- [`13-nfr-operations.md`](13-nfr-operations.md)
- [`14-workflow-engine.md`](14-workflow-engine.md)
- [`23-harness-stabilization.md`](23-harness-stabilization.md)
- [`24-provider-stabilization.md`](24-provider-stabilization.md)
- [`25-harness-polish.md`](25-harness-polish.md)
