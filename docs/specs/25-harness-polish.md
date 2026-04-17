# Cognis: Harness Polish and Remaining Gap Fill

## Purpose

After the harness stabilization sequence (Stages 20-23), a verification sweep
identified a set of smaller but real gaps that still affect harness
behavior in production:

- binary content from MCP tools is dropped on the floor
- skill-load instructions are pruned like ordinary tool output
- mid-stream recovery behavior is reversed from the intended contract
- non-OpenAI token counting still uses a `chars/4` heuristic
- session locks accumulate for sessions that were never cleanly closed
- event-bus subscribers that consistently raise are never evicted
- nonexistent-memory-tool prompt text is hardcoded and drifts from Mnemory
- workflow step `reasoning_effort` overrides are not validated against the
  allowed set

This spec captures the scope of a final harness-polish stage before the
deferred structural work (typed deliverables, step profiles, browser takeover,
agent runtimes, auto routing) is taken up.

## Related Specs

- [`23-harness-stabilization.md`](23-harness-stabilization.md)
- [`24-provider-stabilization.md`](24-provider-stabilization.md)
- [`05-integrations.md`](05-integrations.md)
- [`06-tool-system.md`](06-tool-system.md)
- [`13-nfr-operations.md`](13-nfr-operations.md)
- [`14-workflow-engine.md`](14-workflow-engine.md)

## Design Principles

### 1. Only fix real, verified gaps

This stage is not a refactor. Every item must be backed by concrete code
evidence and a demonstrable user-visible or operational impact.

### 2. Preserve compatibility

Spec 21 deliverables and spec 22 step profiles are still deferred. Nothing in
this stage may pre-empt or block that future work.

### 3. Token accuracy over heuristics

Token budgeting should prefer real provider tokenizers over character
approximations when the provider exposes a counting API. Fall back only when
the provider path is unavailable.

### 4. Fail loudly for misconfiguration

Workflow step configuration that silently drops invalid values must start to
refuse them at load time.

## Scope

### In Scope

- MCP binary content (image, resource) passthrough via the artifact store
- skill-load tool output protection during context pruning
- mid-stream recovery semantics reversal and partial-content handling
- provider-native token counting for Anthropic and Gemini with fallback
- periodic `SessionLock` sweeper for abandoned sessions
- event-bus dead subscriber auto-eviction
- dynamic MCP nonexistent-tool prompt text
- workflow step `reasoning_effort` field validation
- telemetry additions for each change

### Out of Scope

- `TokenUsage` schema extension with `reasoning_tokens`, `cache_creation_tokens`,
  `cache_read_tokens` (deferred to a future cost-observability stage)
- feeding `StreamAccumulator.usage` into `/context` display (same as above)
- `Cost` accuracy for reasoning tokens (same as above)
- Bedrock/Vertex reasoning-family detection improvements
- typed deliverables, `write_deliverable`, step profiles (deferred)
- browser takeover / noVNC / session recording (deferred)
- agent runtimes (deferred)
- auto routing (deferred)

## Required Changes

### 1. MCP image/resource passthrough

- Replace `"[image content omitted]"` and `"[resource content omitted]"`
  placeholders with artifact-backed references.
- Persist binary content via `cognis/artifacts/store.py`.
- Inject artifact references into the tool result payload so the downstream
  tool-exposure layer can present them as provider-native image or document
  blocks for vision-capable agents.
- Cap per-result binary size and total per-turn binary budget to avoid
  pathological MCP outputs.

### 2. Skill-load context protection

- Mark `skill_load` tool results as protected context in the event stream.
- Skip protected skill-load entries in `cognis/core/pruning.py` token-budget
  pruning.
- Cap cumulative protected skill-load content at a shared token budget (e.g.
  12k tokens) so repeated loads cannot balloon prompt size.
- On budget overflow, evict oldest protected skill-load with a user-visible
  warning event.

### 3. Mid-stream recovery reversal

- On transient retry, preserve the partial `tool_calls` accumulator entries
  from the previous attempt and reset only the text `content` buffer.
- On retries exhausted, do not record partial text content as an
  `assistant_message`. Emit a structured `stream_failed` event instead.
- Record an `assistant_message_aborted` event with partial length metadata for
  telemetry without exposing content.

### 4. Non-OpenAI tokenizer

- Use per-provider token counting:
  - OpenAI: keep `tiktoken` path.
  - Anthropic: use LiteLLM's Anthropic token counter (proxies to the Anthropic
    count-tokens endpoint) with `chars/4` fallback on error.
  - Gemini: use LiteLLM's Gemini token counter with `chars/4` fallback.
  - Unknown/local: `chars/4`.
- Cache the chosen backend per model to avoid O(N) provider calls per turn.
- Surface the chosen backend in telemetry.

### 5. Session lock periodic sweeper

- Start a background sweeper task alongside the other providers during
  `cognis/api/app.py` lifespan.
- Default sweep interval: 15 minutes, configurable via environment variable.
- For each lock entry, if the lock is unheld and the session is not in the
  live-session set, evict the lock entry.
- Record eviction reason in a counter.

### 6. EventBus dead subscriber eviction

- Track per-subscriber consecutive error count in `cognis/core/events.py`.
- On successful invocation, reset the counter.
- On exception, increment; at threshold (default 5 consecutive errors),
  auto-unsubscribe the subscriber and log at WARN level.
- Tag subscribers with a subscriber-type label for telemetry.

### 7. Dynamic MCP nonexistent-tool prompt

- Replace the hardcoded `_NONEXISTENT_MEMORY_TOOLS` tuple in
  `cognis/core/context.py` with a cached lookup against Mnemory's live tool
  inventory.
- Cache for 30 minutes or until an explicit invalidation.
- Fall back to the current hardcoded tuple if Mnemory is unreachable.

### 8. Workflow step `reasoning_effort` validator

- Add a Pydantic `field_validator` on
  `cognis/models/workflow.py:WorkflowStepDefinition.reasoning_effort`.
- Accept `None` or one of `NORMALIZED_REASONING_LEVELS`.
- Reject unknown values with a clear error message.
- Apply the same validator to any other step-override structure that carries
  `reasoning_effort`.

## Telemetry

- `cognis_mcp_binary_content_artifacts_total{mime_type}`
- `cognis_tokenizer_used_total{provider, backend}`
- `cognis_session_locks_evicted_total{reason}`
- `cognis_event_subscriber_errors_total{subscriber_type}`
- `cognis_event_subscribers_auto_removed_total{subscriber_type, reason}`

Content, arguments, and memory payloads remain excluded from logs and metrics.

## Acceptance Criteria

- MCP image/resource content reaches vision-capable agents via artifact
  references instead of placeholder strings.
- Skill-load instructions survive long tool-heavy turns without being pruned.
- Mid-stream retry preserves tool-call progress; retries exhausted do not
  pollute history with partial text.
- Token accounting for Anthropic and Gemini models is derived from provider
  tokenizers rather than `chars/4` on the default code path.
- Abandoned session locks are removed by the periodic sweeper.
- A misbehaving event-bus subscriber is auto-unsubscribed after five
  consecutive errors.
- MCP nonexistent-memory-tool prompt text is derived from the live tool
  inventory when available.
- Workflow-step `reasoning_effort` overrides with invalid values are rejected
  at load time.

## Implementation Order

This spec is implemented by
[`implementation/stage-25-harness-polish.md`](implementation/stage-25-harness-polish.md).

After this stage ships, the deferred structural stages follow:

- browser takeover and session recording
- first-class agent runtimes
- auto routing for agents and workflows
- workflow deliverables and step profiles

Tracker numbering is updated so these deferred stages come after the
harness-polish stage.
