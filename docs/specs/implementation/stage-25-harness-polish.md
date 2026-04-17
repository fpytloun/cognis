# Stage 25: Harness Polish and Remaining Gap Fill

## Status

DONE

## Goal

Finish the remaining verified harness gaps surfaced after Stages 20-23 landed,
so the stabilization sequence ends at a clean boundary before the deferred
structural work (browser takeover, agent runtimes, auto routing, workflow
deliverables and step profiles) is picked up.

This stage implements
[`../25-harness-polish.md`](../25-harness-polish.md).

## Dependencies

- `docs/specs/05-integrations.md`
- `docs/specs/06-tool-system.md`
- `docs/specs/13-nfr-operations.md`
- `docs/specs/14-workflow-engine.md`
- `docs/specs/23-harness-stabilization.md`
- `docs/specs/24-provider-stabilization.md`
- `docs/specs/25-harness-polish.md`
- Stages 20-23 complete

## Scope

### In Scope

- MCP binary content passthrough via the artifact store
- skill-load tool output protection during pruning
- mid-stream recovery semantics reversal and partial-content handling
- provider-native token counting for Anthropic and Gemini
- periodic `SessionLock` sweeper
- event-bus dead subscriber eviction
- dynamic MCP nonexistent-tool prompt text
- workflow step `reasoning_effort` field validation
- telemetry additions

### Out of Scope

- `TokenUsage` schema extension and cost observability
- Bedrock/Vertex reasoning-family detection
- typed deliverables / step profiles (deferred)
- browser takeover / agent runtimes / auto routing (deferred)

## Deliverables

### 1. MCP image/resource passthrough

- Replace placeholder strings in `cognis/tools/mcp.py` with artifact-backed
  references.
- Persist binary content via `cognis/artifacts/store.py`.
- Surface artifact references in tool-result payloads so `tool_exposure`
  presents them as provider-native image or document blocks for vision-capable
  agents.
- Enforce per-result binary size and per-turn binary budget limits.
- Metric: `cognis_mcp_binary_content_artifacts_total{mime_type}`.

Files likely touched:

- `cognis/tools/mcp.py`
- `cognis/core/tool_exposure.py`
- `cognis/core/tool_router.py`
- `cognis/artifacts/store.py`

### 2. Skill-load context protection

- Mark `skill_load` results with a protected-context flag when emitted.
- Update `cognis/core/pruning.py:prune_tool_outputs` to skip protected
  `skill_load` entries during budget pruning.
- Cap cumulative protected skill-load content at a configured token budget;
  on overflow, evict oldest protected entry with a user-visible warning event.

Files likely touched:

- `cognis/core/pruning.py`
- `cognis/core/skill_management.py` or `cognis/tools/builtin/skill_management.py`
- `cognis/core/events.py`

### 3. Mid-stream recovery semantics reversal

- On transient retry, preserve the partial `tool_calls` accumulator from the
  previous attempt.
- On retries exhausted, do not record partial `content` as an assistant
  message; emit a `stream_failed` event.
- Record an `assistant_message_aborted` event (length metadata only) for
  telemetry.

Files likely touched:

- `cognis/core/agent_loop.py`

### 4. Provider-native tokenizer for Anthropic and Gemini

- Use `litellm.token_counter` for Anthropic and Gemini with `chars/4` fallback.
- Keep `tiktoken` for OpenAI.
- Cache the chosen backend per model.
- Metric: `cognis_tokenizer_used_total{provider, backend}`.

Files likely touched:

- `cognis/providers/llm/litellm.py`

### 5. SessionLock periodic sweeper

- Background task on lifespan start; interval configurable via
  `COGNIS_SESSION_LOCK_SWEEP_INTERVAL_SECONDS` (default 15 minutes).
- For each entry in `SessionLock._locks`: if unheld and the session is not
  live, evict.
- Metric: `cognis_session_locks_evicted_total{reason}` with reasons
  `close_session` and `sweeper`.

Files likely touched:

- `cognis/core/session.py`
- `cognis/api/app.py`

### 6. EventBus dead subscriber eviction

- Track per-subscriber consecutive error count.
- Auto-unsubscribe at threshold (default 5).
- Metrics:
  - `cognis_event_subscriber_errors_total{subscriber_type}`
  - `cognis_event_subscribers_auto_removed_total{subscriber_type, reason}`

Files likely touched:

- `cognis/core/events.py`

### 7. Dynamic MCP nonexistent-tool prompt

- Replace the hardcoded `_NONEXISTENT_MEMORY_TOOLS` tuple with a cached
  Mnemory-sourced inventory lookup.
- Fall back to the current hardcoded list if Mnemory is unreachable.
- Cache TTL: 30 minutes or explicit invalidation.

Files likely touched:

- `cognis/core/context.py`
- `cognis/providers/memory/mnemory.py`

### 8. Workflow `reasoning_effort` validator

- Add a Pydantic `field_validator` to `WorkflowStepDefinition.reasoning_effort`
  in `cognis/models/workflow.py`.
- Accept `None` or one of `NORMALIZED_REASONING_LEVELS`.
- Apply the same validator to any other step-override structure that carries
  `reasoning_effort`.

Files likely touched:

- `cognis/models/workflow.py`
- `cognis/providers/llm/reasoning.py` (export of `NORMALIZED_REASONING_LEVELS`)

## Suggested Work Breakdown

### Workstream A: Harness-visible fixes

1. Workflow `reasoning_effort` validator.
2. Provider-native tokenizer.
3. Skill-load context protection.
4. MCP image/resource passthrough.
5. Mid-stream recovery reversal.

### Workstream B: Operational hygiene

1. Session lock sweeper.
2. EventBus dead subscriber eviction.
3. Dynamic MCP nonexistent-tool prompt.

### Workstream C: Telemetry and tests

1. Prometheus counters for the above.
2. Unit tests covering each deliverable.
3. Integration tests for:
   - multi-skill turn preserving skill-load instructions,
   - vision-capable agent receiving image content via MCP,
   - mid-stream retry preserving tool-call progress,
   - periodic sweeper evicting abandoned session locks.

## Acceptance Criteria

- MCP image/resource content reaches vision-capable agents as image blocks.
- Skill-load instructions survive long tool-heavy turns without being pruned.
- Mid-stream retry preserves tool-call progress; retries exhausted do not
  pollute history with partial text.
- Anthropic and Gemini token counting uses provider tokenizers on the default
  code path.
- Abandoned session locks are evicted by the sweeper.
- Faulty event subscribers are auto-unsubscribed after five consecutive
  errors.
- MCP nonexistent-tool prompt text is derived from live Mnemory inventory
  when available.
- Workflow step `reasoning_effort` overrides with invalid values raise a
  clear validation error at load time.
- All new telemetry counters emit values during unit/integration test runs.
