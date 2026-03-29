# Stage 14: Degraded Mode and Recovery UX

**Status**: DONE
**Repo**: `cognis`
**Depends on**: Stage 11 (real health/test semantics), Stage 12 (diagnostics page)
**Estimated effort**: 3-4 days

## Objective

When things break, users should understand what still works, what is
blocked, and what to do about it. Failures should produce specific,
actionable guidance — not generic "turn failed" messages.

## Context

The current error handling is technically correct but user-hostile:

- When Intaris is down, chat fails with "Turn execution failed" — no
  indication that guardrails are the cause or that the user should check
  Intaris.
- When Mnemory is down, chat works but without memory. There is no
  in-chat indication that recall is unavailable.
- When no LLM provider is configured, the user gets a raw ValueError
  ("No LLM model configured") wrapped in a generic error.
- The health endpoint reports degraded status, but this information does
  not surface in the chat or task UIs where users actually work.
- Agent creation silently swallows Mnemory bootstrap failures — the agent
  is created but personality is not synced, with no user-visible warning.

## Deliverables

### 1. Provider Outage Banners

Global awareness of service health in the main UI.

- **Health polling component**: polls `GET /api/health` every 30 seconds
  (pauses when tab is hidden). Mounted in the app layout.
- **Banner display**: when any provider is unhealthy, show a persistent
  banner below the header with per-provider status:
  - Memory (Mnemory): "Memory unavailable — chat works but without
    recall. [Check diagnostics]"
  - Guardrails (Intaris): "Guardrails unavailable — tool execution is
    blocked. [Check diagnostics]"
  - LLM: "LLM provider error — chat and tasks are unavailable.
    [Configure provider]"
- **Auto-recovery**: banner disappears automatically when the provider
  recovers (next health poll returns healthy).
- **Distinct from WebSocket badge**: the existing WebSocket status badge
  in the header shows connection state. Provider banners show service
  health. Both can be visible simultaneously.
- **Severity levels**: use amber for degraded (memory down — chat still
  works), red for blocking (guardrails or LLM down — core features
  broken).

### 2. Contextual Failure Messaging

Replace generic errors with specific, actionable messages in context.

- **Chat errors** (`websocket.py` error messages and UI error display):
  - Intaris unreachable: "Guardrails service is unreachable — tool calls
    are blocked until it recovers. Check that Intaris is running."
  - LLM provider error: "LLM provider returned an error: [detail].
    Check your provider configuration in Settings."
  - No LLM configured: "No LLM provider is configured. Go to Settings >
    Providers to add one."
  - Mnemory unreachable (non-blocking): inline note in context area:
    "Memory is currently unavailable — this conversation won't have
    access to past context."
  - Session creation failed: "Could not create a session — [specific
    cause]. Try again or check the diagnostics page."
- **Task board errors**:
  - Task stuck in "running" with no progress: show last error reason
    on the task card ("Waiting for LLM provider", "Guardrails check
    failed", "Step evaluation timed out").
  - Task failed: show failure reason in the task detail view with
    actionable guidance.
- **Agent creation warnings**:
  - Mnemory bootstrap failed: show amber warning on the agent detail
    page: "Personality was not synced to Mnemory. [Retry sync]"
    (currently silent with only a log entry).
  - Surface the existing `personality_synced` status from the agent
    creation response.

### 3. Setup-Incomplete States

Distinguish "not configured yet" from "configured but broken."

- **No LLM provider**: on chat and tasks pages, show a prominent card:
  "Configure an LLM provider to start chatting" with a direct link to
  Settings > Providers. Do not show the normal chat composer.
- **No agents**: on chat page, show: "Create an agent to start chatting"
  with a link to Agents > New. Do not show the conversation sidebar.
- **Provider configured but broken**: show the normal UI with an error
  banner (from deliverable 1), not the setup-incomplete state.
- **Detection**: check on page load via existing API calls (provider
  list, agent list, health). Cache in a Svelte store to avoid repeated
  calls.

### 4. Retry and Recovery Affordances

Make recovery actions visible where failures occur.

- **Provider health retry**: "Retry" button on the health status in
  diagnostics and in the outage banner. Triggers an immediate health
  poll instead of waiting for the next interval.
- **Personality sync retry**: "Retry sync" button on agent detail page
  when `personality_synced` is false. Calls the existing
  `POST /api/v1/agents/{id}/sync-personality` endpoint.
- **WebSocket reconnect**: make the existing "Reconnect" button more
  prominent when the connection is stalled. Add auto-reconnect attempt
  counter ("Reconnecting... attempt 3/10").
- **Failed turn retry**: after a turn fails, show a "Retry" button that
  re-sends the last user message. Only available for recoverable errors.
- **Escalation recovery**: if escalation polling fails, show a "Refresh
  escalations" button instead of silently stopping.

### 5. Backend Error Classification

Improve error responses so the UI can show specific messages.

- **Structured error codes**: extend the WebSocket error messages with
  specific codes that the UI can map to user-friendly text:
  - `provider_unreachable:guardrails` — Intaris down
  - `provider_unreachable:memory` — Mnemory down
  - `provider_unreachable:llm` — LLM provider down
  - `provider_not_configured:llm` — no LLM provider set up
  - `provider_error:llm` — LLM returned an error (with detail)
  - `session_creation_failed` — could not create Intaris session
  - `turn_cancelled` — user cancelled the turn
- **Error detail field**: include `error_detail` in the WebSocket error
  message for provider errors (e.g., the actual LLM error message,
  sanitized of any secrets).
- **Backward compatible**: existing error codes continue to work. New
  codes are additive.

### 6. Degraded Mode Integration Tests

Verify degraded behavior is correct and user-facing messages are accurate.

- **Test: Mnemory down during chat** — verify chat works, memory note
  appears, no crash.
- **Test: Intaris down during chat** — verify tool calls blocked, specific
  error message returned, chat composer still accessible.
- **Test: LLM provider misconfigured** — verify specific error message,
  not a raw ValueError.
- **Test: No LLM provider configured** — verify setup-incomplete state
  shown on chat page.
- **Test: Mnemory down during agent creation** — verify agent created,
  warning surfaced, retry works.
- **Test: Provider recovery** — verify banner disappears when provider
  comes back.

## Acceptance Criteria

- [x] Provider outage banners appear automatically when services are down
- [x] Banners disappear automatically when services recover
- [x] Banners distinguish degraded (amber) from blocking (red) states
- [x] Chat errors show specific cause and actionable guidance
- [x] Task failures show specific reason on the task card/detail
- [x] Agent creation shows warning when Mnemory sync fails
- [x] Setup-incomplete states are visually distinct from failures
- [x] "No LLM provider" shows setup guidance, not an error
- [x] Retry buttons are available for: health check, personality sync,
      WebSocket reconnect, failed turn, escalation polling
- [x] WebSocket error messages include specific provider error codes
- [x] Degraded mode behavior is covered by integration tests
- [x] Provider recovery is tested (down → up → banner clears)

## Key References

- `cognis/api/websocket.py` — WebSocket error message construction
- `cognis/api/routes/system.py` — health endpoint
- `cognis/api/routes/agents.py` — agent creation, personality sync
- `cognis/core/agent_loop.py` — turn execution, error handling
- `cognis/core/context.py` — context assembly failure handling
- `cognis/providers/registry.py` — circuit breaker, provider health
- `ui/src/routes/(app)/+layout.svelte` — app shell (banner mount point)
- `ui/src/routes/(app)/chat/[conversationId]/+page.svelte` — chat errors
- `ui/src/routes/(app)/tasks/+page.svelte` — task board
- `docs/specs/13-nfr-operations.md` — degraded mode specifications

## Implementation Notes

- Added persistent provider-health banners in the main app shell with retry
  actions and clear degraded-vs-blocking messaging.
- Chat now maps structured WebSocket error codes to user-facing guidance,
  shows memory-degraded notes, exposes retry for recoverable turn failures,
  and surfaces reconnect attempts more clearly.
- Chat and task surfaces now distinguish setup-incomplete states (no provider,
  no agent) from broken-but-configured provider failures.
- Agent responses now surface Mnemory bootstrap/sync failures through read-only
  `personality_synced` metadata and UI retry actions.
