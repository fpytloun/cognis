# Stage 27: Browser Takeover and Session Recording

## Status

PLANNED

## Goal

Turn the current browser automation foundation into a human-assistable browser
runtime with:

- optional browser takeover from the Cognis web UI
- executor-scoped noVNC support for headed browser sessions
- Intaris-owned browser/desktop session recording timelines and evidence
- stricter browser observability and replay/audit linkage

This stage builds on the shipped browser foundation:

- Playwright executor runtime
- persistent local profiles
- browser session/profile discovery
- credential/auth challenge flows
- browser debug/control tools

## Why This Stage Exists

The browser toolset is now strong enough for many sites, but hard auth flows
such as Reddit MFA still benefit from a real human handoff path instead of more
automation heuristics.

At the same time, browser and future desktop sessions need audit-grade
recording/replay that fits Cognis data ownership rules:

- Cognis orchestrates
- Intaris owns durable session/audit content
- artifact blobs remain in the artifact backend but recording evidence lifecycle
  is governed by Intaris

## Scope

### In Scope

- optional executor capability for browser takeover
- noVNC-backed headed browser access for compatible executors
- controller-side browser takeover request / grant / resume flow
- UI for browser session listing, monitoring, and takeover
- Intaris recording event model for browser and future desktop sessions
- key evidence capture flow (audit timeline first, screenshots next)
- takeover access control and audit semantics
- numeric NFRs and quotas for recording/takeover

### Out of Scope

- full desktop/computer-use implementation
- complete video recording pipeline for all sessions
- cross-executor portable takeover sessions
- general remote desktop outside browser/desktop runtime scope

## Dependencies

- `docs/specs/15-browser-credentials.md`
- `docs/specs/05-integrations.md`
- `docs/specs/13-nfr-operations.md`
- current browser runtime/tooling already merged in Cognis
- prerequisite Intaris work for recording-evidence lifecycle APIs

## Deliverables

### 1. Takeover Control Plane in Cognis

Add controller-owned browser takeover flow with:

- `request_browser_takeover` primitive or equivalent workflow tool
- notification / pause / resume support
- explicit control states:
  - `requested`
  - `granted`
  - `human_active`
  - `released`
  - `resumed`
  - `expired`
- one active controller per live browser session
- auditable transitions

### 2. Executor noVNC Capability

Add optional executor browser takeover config, disabled by default:

- `browser_takeover_enabled`
- `browser_takeover_mode = off | novnc`
- `browser_takeover_idle_timeout_seconds`
- any required VNC/noVNC/Xvfb-related executor runtime settings

Expected behavior:

- only compatible executors expose takeover
- headed Linux browser sessions can be attached to a noVNC view
- no direct public VNC/noVNC exposure from executor hosts
- access is brokered by Cognis with short-lived authorization

### 3. Browser Session UI

Add browser session monitoring/takeover UI in Cognis web:

- list active browser sessions
- show URL/title/session metadata
- show current takeover state
- launch takeover view when allowed
- resume agent when human work is complete

Minimum viable UI:

- session list
- session detail panel
- takeover state controls
- embedded noVNC client for enabled executors

### 4. Intaris Recording Contract

Implement the Cognis side of the recording contract for:

- browser session events
- takeover events
- evidence artifact linkage

Expected event linkage fields:

- `intaris_session_id`
- `conversation_id`
- `task_id | null`
- `step_run_id | null`
- `browser_session_id | null`
- `runtime_run_id | null`
- `executor_id`
- `actor`
- `actor_id`
- `control_mode`

### 5. Evidence Capture Modes

Support mode-aware behavior:

- `off`
- `audit`
- `evidence`
- `full`

Recommended implementation order:

1. `audit`: event timeline only
2. `evidence`: key screenshots on important transitions
3. `full`: only after the earlier two are stable and useful

### 6. Security and Privacy Enforcement

Implement the browser takeover / recording constraints already defined in the
specs:

- deny-by-default media capture on sensitive auth pages
- no plaintext passwords/OTPs/tokens in payloads
- URL/title sanitization before durable recording
- short-lived takeover tokens
- explicit view vs control authorization
- audit of takeover joins/leaves/control transitions

## Suggested Work Breakdown

### Workstream A: Browser Takeover State Machine

Files likely touched:

- `cognis/core/notifications.py`
- `cognis/core/agent_loop.py`
- `cognis/api/routes/...` new browser session/takeover routes
- `cognis/api/websocket.py` if real-time browser session state events are added

Tasks:

1. Define browser takeover notification and resolution payloads
2. Add session-level control lock semantics
3. Add restart-safe expired/orphaned takeover handling
4. Add tests for takeover state transitions

### Workstream B: Executor noVNC Integration

Files likely touched:

- `cognis/tools/executor/browser/manager.py`
- executor runtime/config plumbing
- browser config UI/types

Tasks:

1. Add optional noVNC capability/config
2. Start/stop the noVNC/VNC sidecar/process only when enabled and needed
3. Bind browser sessions to takeover transport safely
4. Add cleanup for abandoned takeover sessions

### Workstream C: UI Session Monitor and Takeover Client

Files likely touched:

- `ui/src/routes/(app)/...`
- API client/types
- browser-specific components

Tasks:

1. Add browser session list/detail views
2. Add takeover request / resume controls
3. Add embedded noVNC client integration
4. Add browser session state refresh/reconnect UX

### Workstream D: Intaris Recording Emission

Files likely touched:

- `cognis/providers/guardrails/intaris.py`
- browser manager/handlers for event emission hooks
- artifact linkage flow

Tasks:

1. Emit browser/takeover recording events with full lineage
2. Integrate evidence reservation/upload/finalize flow once Intaris API exists
3. Add integrity metadata and idempotency handling
4. Add tests for duplicate/retry-safe emission

### Workstream E: Policy, Sanitization, and NFRs

Tasks:

1. Enforce URL/title/query sanitization rules
2. Add capture suppression/masking policy hooks for sensitive auth states
3. Expose recording mode and takeover limits in executor config
4. Implement metrics and alerts for takeover/recording paths

## Acceptance Criteria

This stage is complete when:

1. An agent can pause for browser takeover and the user can resume the same
   browser session safely.
2. noVNC/browser takeover is optional and enabled only on selected executors.
3. Takeover traffic is brokered/authenticated and not directly exposed from the
   executor host.
4. Browser/takeover events are emitted with stable lineage suitable for Intaris
   replay.
5. Sensitive auth pages do not capture secrets into durable event payloads, and
   media capture follows policy.
6. Session/takeover transitions are auditable and restart-safe.
7. Numeric operational limits are implemented for headed takeover sessions,
   replay availability, and orphan cleanup.

## Recommended Order for Next Session

If resuming this work in the next session, the best order is:

1. Define the concrete Cognis takeover state machine and API surface
2. Add executor config and runtime scaffolding for noVNC capability
3. Build browser session UI and takeover controls
4. Integrate Intaris recording event emission
5. Add evidence capture and sanitization policy hooks
6. Add end-to-end tests for takeover + resume

## Open Questions

These should be confirmed before implementation starts:

1. Should the first delivery include true interactive noVNC immediately, or a
   monitor-only browser session page first?
2. Are multiple read-only viewers allowed while one user holds control?
3. Should recording default to `audit` for browser-capable executors, or remain
   `off` until explicitly enabled?
4. How much screenshot capture is acceptable by default during takeover on auth
   pages?
