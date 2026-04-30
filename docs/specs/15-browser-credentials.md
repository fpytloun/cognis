# Browser Automation and Credentials

## Goal

Add browser automation to Cognis in a way that preserves the controller/executor
separation, keeps plaintext credentials out of LLM context, and works on both
sticky local executors and temporary cloud executors such as Kubernetes pods.

This spec introduces:

- Playwright-based browser automation on executors
- first-class credential records for agent-facing authentication
- controller-mediated auth request flows that bypass the LLM for secret values
- controller-mediated MFA challenge flows for OTP, push approval, and manual continuation
- portable browser auth state persistence using Playwright `storageState`

## Non-Goals

Not part of the initial implementation:

- desktop or OS-level computer use outside the browser
- full persistent browser profile directories as the primary session model
- controller-side browser execution
- tools that return plaintext secret or credential values to the LLM
- exact in-memory browser/page resume after executor loss

## Design Principles

1. Controller owns durable truth.
2. Executor owns ephemeral browser runtime.
3. Browser sessions are rehydratable, not resumable.
4. Plaintext credentials never enter LLM context.
5. Browser automation remains executor-native only.
6. Production deployments must support temporary executors.

## Terminology

### Secret

A low-level encrypted sensitive value or encrypted payload managed by Cognis.
Secrets are the storage/security primitive.

Examples:

- API key
- password
- bearer token
- encrypted JSON payload

### Credential

A higher-level authentication record intended for agents, tools, and user-facing
auth flows. Credentials are structured, typed, and carry metadata describing
what they are for.

Examples:

- username/password for `github.com`
- token for a specific API endpoint
- TOTP seed for an authenticator-backed login flow
- saved Playwright browser auth state for `https://github.com`

Conceptually:

- `secret` = secure storage primitive
- `credential` = structured auth domain object built on top of secure storage

## Architecture Summary

### Controller Responsibilities

The controller owns all durable state related to authentication and browser
automation policy:

- credential records and metadata
- encrypted credential payloads
- agent permission checks for credential access
- auth request and re-auth notifications
- browser auth state persistence metadata
- artifact metadata for screenshots/downloads
- executor capability-aware scheduling
- audit events and policy enforcement

The controller must never execute browser actions directly.

### Executor Responsibilities

The executor owns ephemeral runtime state only:

- Playwright browser processes
- browser runtime leases
- browser contexts and pages
- local resolution of controller-approved credential refs
- temporary download/upload materialization
- runtime cleanup on cancellation, timeout, or disconnect

Executors must not persist decrypted credentials or browser auth state to disk
except temporary runtime files that are cleaned up when the runtime lease ends.

## Cloud-Native Model

This design is cloud-native when implemented with these rules:

1. Live browser state is executor-local and ephemeral.
2. Durable auth state is controller-owned and encrypted.
3. Temporary executors restore auth from controller-owned records.
4. Executor loss fails the active browser lease instead of pretending the page
   can resume exactly where it left off.
5. Workflows retry from a defined step boundary and may rehydrate auth state on
   another executor.

This model supports:

- executor groups
- short-lived Kubernetes pods
- remote WebSocket executors
- task-scoped browser work on a hardened browser executor pool

This model does not guarantee:

- exact DOM or navigation resume after executor loss
- portability of all authenticated sessions across IP/device changes
- headed/manual browser flows on generic cloud executors

## Credential Records

Credential records are first-class Cognis metadata objects.

Suggested fields:

- `credential_id: str`
- `user_email: str`
- `scope: str`
- `agent_id: str | None`
- `kind: str`
- `label: str`
- `description: str | None`
- `metadata: dict[str, Any]`
- `encrypted_payload: bytes`
- `version: int`
- `status: str`
- `created_at: datetime`
- `updated_at: datetime`
- `last_verified_at: datetime | None`
- `expires_at: datetime | None`
- `revoked_at: datetime | None`

Initial credential kinds:

- `text`
- `token`
- `username_password`
- `totp_seed`
- `recovery_codes`
- `browser_storage_state`

Suggested status values:

- `active`
- `expired`
- `revoked`
- `invalid`

## Credential Payloads

Credential payloads are encrypted JSON.

Examples:

`token`

```json
{
  "token": "..."
}
```

`username_password`

```json
{
  "username": "...",
  "password": "..."
}
```

`browser_storage_state`

```json
{
  "storage_state": {
    "cookies": [],
    "origins": []
  }
}
```

`totp_seed`

```json
{
  "issuer": "GitHub",
  "account_name": "user@example.com",
  "secret": "base32-secret"
}
```

`recovery_codes`

```json
{
  "codes": ["code-1", "code-2"]
}
```

## Credential Metadata

Credential metadata is non-secret, queryable, and safe for UI display, but it
must still be validated and size-limited.

Reserved/common keys:

- `url`
- `origin`
- `domain`
- `login_url`
- `provider`
- `auth_type`
- `tags`
- `notes`
- `username_hint`

Rules:

- metadata must not contain plaintext secrets
- reserved keys should have explicit type validation
- free-form metadata should be limited in size and count
- only allowlisted metadata keys should be indexed or used for filtering
- metadata is never treated as secret material, so the UI must discourage users
  from pasting passwords or tokens into metadata fields

## Permissions

Current `allowed_secrets` should evolve toward `allowed_credentials`.

Recommended rules:

- agents may only reference explicitly allowed credentials
- credentials may be scoped by user or by agent
- browser auth state records follow the same permission model
- agent-scoped credentials should only override user-scoped records when
  explicitly allowed
- list APIs should expose metadata only for credentials visible to the caller

Backward-compatible migrations may temporarily bridge `allowed_secrets` to
`allowed_credentials`, but the target model should be credential-based.

## Credential References

Tools must reference credentials by id, not by plaintext value.

MVP syntax:

- `$credential:<id>`
- `$credential:<id>.<field>`

Examples:

- `$credential:github_work.username`
- `$credential:github_work.password`
- `$credential:notion_token.token`
- `$credential:github_state.storage_state`

Rules:

- refs must fail closed if missing, unauthorized, expired, or revoked
- refs must never be expanded into LLM-visible content
- tool outputs must never echo resolved plaintext values

## Credential Decryption and Injection Contract

This contract is security-critical.

1. The LLM emits a tool call containing credential refs only.
2. The controller validates tool permission and credential permission.
3. The controller decrypts only the minimum credential fields required.
4. The controller injects only the minimum scoped plaintext needed for that
   executor runtime or tool call.
5. The executor resolves refs locally during execution.
6. Tool outputs, logs, metrics, and audit payloads never include plaintext
   credential values.

Rules:

- the controller is the only durable decrypting authority
- executors must not contact the credential store directly
- executors must not persist decrypted values beyond runtime memory or
  temporary files needed for immediate execution
- audit events may record credential ids and accessed fields, but never values

## Auth Request Flow

Missing credentials must be acquired through an orchestration primitive, not a
generic executor tool.

Recommended primitive: `request_credential`

Behavior:

1. The agent determines that a required credential is missing or invalid.
2. The controller records a persistent auth request notification.
3. The workflow/task waits according to existing pause/question mechanics.
4. The UI renders a typed auth form directly to the user.
5. The user submits the credential to Cognis without routing it through the
   LLM provider.
6. Cognis stores the encrypted credential record.
7. The paused workflow resumes with only metadata/success information visible
   to the model.

Supported request types in MVP:

- token
- username/password
- browser auth state renewal

This flow is for missing or stale durable authentication material. It is not
the same as a live MFA challenge that appears during an active browser session.

Suggested request fields:

- `kind`
- `label`
- `description`
- `metadata`
- `scope`
- `agent_id | null`
- `reason`
- `required_fields`

## Auth Challenge Flow (MFA and Step-Up Verification)

Live authentication challenges that occur during browser automation must use a
separate orchestration primitive from `request_credential`.

Recommended primitive: `request_auth_challenge`

This covers ephemeral or human-in-the-loop challenges such as:

- SMS codes
- email verification codes
- authenticator app TOTP codes
- mobile push approvals such as "tap yes on your phone"
- security key prompts
- manual "done/continue" checkpoints

Suggested challenge kinds:

- `otp_code`
- `totp_code`
- `sms_code`
- `email_code`
- `push_approval`
- `security_key`
- `manual_continue`

Suggested challenge fields:

- `challenge_id`
- `kind`
- `label`
- `message`
- `origin`
- `expires_at`
- `attempts`
- `input_schema`
- `browser_runtime_id | null`
- `browser_session_id | null`

Behavior:

1. The browser flow detects an MFA or step-up challenge.
2. The controller records a persistent auth challenge notification.
3. The active workflow pauses while preserving the browser lease if possible.
4. The UI prompts the user for the expected action or challenge response.
5. The response is delivered directly to Cognis without routing through the LLM
   provider.
6. The controller resumes the workflow with only challenge status visible to
   the model.

Design rules:

- ephemeral MFA responses are not durable credentials by default
- push approval and security key prompts are challenge events, not credentials
- OTP/TOTP values should not be echoed into normal tool results or logs
- if the browser lease is still alive, the challenge should resume in the same
  lease and session
- if the lease is lost, the step must fail or retry from a defined boundary

### MFA and Credentials

MFA spans both durable credentials and live challenges:

- durable auth material belongs in credential records
- ephemeral challenge responses belong in auth challenge orchestration

Examples:

- `totp_seed` is a credential kind
- SMS OTP is a live auth challenge response
- Google mobile push approval is a live auth challenge response
- browser `storageState` is a credential kind

Automated TOTP generation may be supported later by storing a `totp_seed`
credential and generating the current code without exposing the seed to the
LLM. The initial implementation may start with manual entry through the auth
challenge UI.

## Browser Runtime Lease Model

Browser execution must use an explicit lease abstraction owned by the executor.

Suggested fields:

- `browser_runtime_id`
- `owner_type`
- `owner_id`
- `executor_id`
- `mode`
- `browser_engine`
- `created_at`
- `last_used_at`
- `expires_at`
- `status`

Rules:

- a lease is scoped to a single active step/task execution
- a lease may contain multiple browser sessions in parallel
- a lease is destroyed on completion, timeout, cancellation, or executor loss
- a lease is not portable as a live object across executors
- a lease may remain alive while waiting on a bounded auth challenge timeout

## Browser Sessions Within a Lease

Each lease may manage multiple sessions.

Suggested per-session state:

- `browser_session_id`
- browser context handle
- active page handle
- auth state source reference
- temporary download directory
- artifact references produced during the session

Rules:

- session ids are explicit in tool calls
- sessions are isolated unless explicitly created to share state
- limits must exist for sessions per lease and per executor

## Executor Loss and Retry Semantics

Executor loss must be deterministic.

If the executor disconnects or a pod is evicted:

- the active browser lease is lost
- in-memory browser sessions are lost
- the step/run receives a structured failure reason
- the orchestrator may retry from the beginning of the step
- retry may restore `browser_storage_state` on another executor
- retry may need to re-run login and request MFA again
- retry must not claim exact page/DOM resume

This is a core cloud-native behavior: auth is portable, page execution is not.

MFA-heavy flows amplify this rule: they benefit from keeping the same live lease
while waiting for a user response, but they must still fail deterministically if
the executor disappears.

## Browser Executor Capability Contract

Browser-capable executors must advertise capabilities so the controller can
schedule work safely.

Required capability fields:

- `browser.playwright = true`
- `browser.runtime` (`"playwright"` or `"patchright"`)
- `browser.channel` (e.g. `"chrome"`, `"msedge"`, or `null` for the bundled
  engine build)
- `browser.stealth` (whether `playwright-stealth` evasions are applied to new
  contexts on this executor)
- `browser.auto_consent` (`"accept"` / `"reject"` / `"off"`) — runtime-
  agnostic CMP auto-dismiss behaviour
- `browser.humanize_input` + `browser.humanize_intensity` — Bezier mouse-path
  + jittered key-cadence humanization for `browser_click`/`browser_fill`/
  `browser_type`
- `browser.fingerprint_hardening` — AudioContext noise, Battery API stub,
  viewport jitter init scripts
- supported browser engines
- headless support
- headed support
- max parallel browser sessions
- download support
- upload support
- max artifact size
- Playwright/Patchright version

The runtime axis is orthogonal to the engine axis: `runtime` selects which
Python package drives Chromium (vanilla Playwright vs Patchright's patched
fork), while `engine` selects the browser family (`chromium`, `firefox`,
`webkit`). Patchright only ships Chromium in practice and is most effective
with `channel = "chrome"`.

Stealth defaults are runtime-aware: ON for `runtime = "playwright"`, OFF for
`runtime = "patchright"` (Patchright already covers the same JS-layer
evasions). Both defaults are user-overridable per executor.

Production deployments should standardize a browser-capable executor image or
executor class to reduce drift. The published executor image bundles Chromium
+ Chrome stable by default; Firefox and WebKit auto-install lazily on first
use or can be pre-bundled with the `COGNIS_EXECUTOR_BROWSERS` build arg. Use
`cognis-executor browser-install --all-defaults` (or
`cognis-controller executor browser-install --all-defaults`) to pre-warm a
host without waiting for the first session.

## Browser Tool Set

Initial executor-native browser tools:

- `browser_open`
- `browser_snapshot`
- `browser_get_text`
- `browser_get_focus`
- `browser_click`
- `browser_fill`
- `browser_type`
- `browser_press`
- `browser_wait_for`
- `browser_screenshot`
- `browser_close`
- `browser_save_auth_state`

Likely follow-ups:

- `browser_select`
- `browser_upload`
- `browser_download_wait`
- `browser_list_sessions`

## Browser Tool Semantics

### `browser_open`

- creates or reuses a browser session in a lease
- defaults to headless mode
- may accept `auth_state_ref`
- must fail if the selected executor does not support the requested mode

### `browser_fill`

- fills an input field or contenteditable target
- supports `value_ref` / credential refs
- must never return the resolved plaintext

### `browser_type`

- types into an input field or contenteditable target using key events
- supports literal `text` and secure `value_ref` / credential refs
- must never return the resolved plaintext
- supports per-key delay and executor-configured humanized key cadence

### `browser_press`

- presses a keyboard key in the current page, or types `text` / `value_ref`
  into the currently focused element
- useful after `browser_click` focuses a hosted payment iframe field where the
  exact editable element is not directly visible to the top-level page
- must never return typed text or resolved plaintext

### Frame-aware targeting

- `browser_snapshot` and `browser_query` discover actionable elements across
  the main document and reachable iframes using Playwright frame contexts
- returned refs include `frame_index`, `frame_url`, and `frame_name`
- action tools (`browser_click`, `browser_fill`, `browser_focus`,
  `browser_type`, and `browser_submit_form`) resolve refs back into the
  originating frame before creating a locator
- selector-mode actions search all frames and fail with frame metadata when
  multiple viable candidates match
- stale refs fail closed and require a fresh snapshot/query

### `browser_get_focus`

- returns the currently focused frame and active element metadata
- includes frame URL/name/index and non-secret element properties such as tag,
  type, name, placeholder, autocomplete, visibility, editability, and redacted
  value state
- must never return the active element's plaintext value

### `browser_snapshot`

- returns a size-bounded representation of the current page
- may include URL, title, visible text summary, and selector/locator hints
- includes frame metadata for every returned interactive element
- must avoid dumping raw page content without bounds

### `browser_screenshot`

- stores the image as an artifact
- returns an artifact reference only
- must not inline raw image bytes into prompt content

### `browser_save_auth_state`

- explicitly persists Playwright `storageState`
- creates or updates a `browser_storage_state` credential record
- returns only opaque metadata such as `credential_id` and `version`
- must never return raw `storageState` JSON

## Safety Classification

Browser tools need stricter treatment than filesystem or plain web fetch tools.

Guidance:

- mutating browser actions should be `non_bypassable`
- extraction from authenticated pages should be treated as sensitive even when
  nominally read-only
- policy context should include origin/domain and whether the page appears to
  be authenticated when that can be inferred safely

Suggested categories:

- read/navigation-sensitive: `browser_open`, `browser_snapshot`,
  `browser_get_text`, `browser_get_focus`, `browser_screenshot`
- write/non-bypassable: `browser_click`, `browser_fill`, `browser_press`,
  `browser_type`, `browser_upload`, `browser_submit`-like actions

The controller must not assume that screenshots or text extraction from a
logged-in page are low-risk.

## Browser Auth State Persistence

MVP persistence format is Playwright `storageState` only.

Rules:

- persisted auth state is stored as a credential record of kind
  `browser_storage_state`
- persistence must be explicit via `browser_save_auth_state`
- first save should require user or policy approval
- updates increment the credential version
- expired or revoked auth state fails closed on restore
- restore via `auth_state_ref` is best-effort and may fail due to site binding

Not in MVP:

- full browser profile directory persistence as the primary path
- assuming one browser state works across all executor environments

## Artifacts

Browser-generated outputs integrate with the Cognis artifact store.

Artifact-backed outputs in MVP:

- screenshots
- downloads

Rules:

- the executor uploads artifacts through the existing artifact path
- the controller stores artifact metadata and signed URL policy
- artifact size and retention limits must be enforced

Artifact-to-browser upload support may be added later.

## Session Recording

Browser and future desktop session recording should be modeled as an Intaris
feature, not as durable controller state in Cognis.

Ownership split:

- Cognis owns orchestration, browser session metadata, takeover state, and
  artifact references
- Intaris owns durable recording timelines, replay metadata, retention,
  review semantics, and the authoritative lifecycle for recording evidence
- the artifact backend remains the binary store for screenshots, video chunks,
  and related evidence blobs, but Intaris is the authority for retention,
  delete/legal-hold policy, replay availability, and signed-access brokering for
  recording evidence

Important boundary:

- generic browser tool artifacts (for example ordinary screenshots or downloads
  returned directly to the user during a task) may continue to use the normal
  Cognis artifact lifecycle
- recording evidence artifacts belong to the Intaris recording lifecycle, even
  if they share the same physical blob backend underneath

This recording model should apply to both:

- `browser` sessions
- future `desktop` / computer-use sessions

### Recording Modes

Suggested recording modes:

- `off` — no session recording
- `audit` — event timeline only
- `evidence` — event timeline plus key screenshots
- `full` — event timeline plus richer media capture such as periodic screenshots
  or video segments

Recommended defaults:

- browser-capable executors default to `audit`
- `evidence` and `full` remain explicit opt-in modes

### Recording Content

Minimum event timeline for browser recording:

- session opened / closed
- navigation and redirect outcomes
- click / fill / type / submit actions
- browser errors and failed requests
- auth challenge requested / resolved
- human takeover requested / granted / released
- agent resumed after takeover

Key evidence artifacts for `evidence` and `full` modes:

- page-open screenshots
- post-navigation screenshots
- auth challenge screenshots
- submit/error screenshots
- takeover start/end screenshots

The event payload should contain metadata and artifact references, not raw media
bytes.

### Recording Model

Suggested recording linkage fields:

- `recording_type`: `browser` | `desktop`
- `intaris_session_id`
- `user_email`
- `agent_id`
- `conversation_id`
- `task_id | null`
- `step_run_id | null`
- `browser_session_id | null`
- `runtime_run_id | null`
- `actor`: `agent` | `human` | `system`
- `control_mode`: `agent` | `human`

This keeps replay and auditing attributable even when control moves between the
agent and a human operator.

### Recording Flow

1. The executor captures browser/desktop runtime events.
2. Cognis enriches those events with stable session/task/user/agent lineage.
3. Cognis forwards the event stream to Intaris.
4. Intaris issues or governs the artifact linkage for recording evidence.
5. Media is stored in the artifact backend and referenced from Intaris events.
5. Intaris exposes replay/audit views over the resulting timeline.

Concrete recording-evidence lifecycle contract:

1. Cognis asks Intaris to reserve an evidence slot for a recording event.
2. Intaris returns an upload target or artifact reservation token.
3. Cognis or the executor uploads the media blob using that reservation.
4. Cognis finalizes the corresponding recording event with the Intaris-issued
   evidence reference and integrity metadata.
5. Intaris becomes authoritative for replay access, retention, delete/legal
   hold, and orphan cleanup for that evidence.

Required properties of this flow:

- idempotent reservation and finalize operations
- deterministic event-to-artifact linkage
- orphan cleanup for abandoned reservations/uploads
- no direct durable replay references created by Cognis alone

### Evidence Integrity

Every recording artifact reference should carry immutable integrity metadata:

- `artifact_id`
- `sha256`
- `size_bytes`
- `content_type`
- `captured_at`
- `executor_id`
- idempotent linkage back to the recording event

Recording event ingestion and recording artifact linkage must be idempotent.
Retries must not create duplicate replay entries or orphaned evidence blobs.

### Human Takeover and Recording

If browser takeover is enabled, the recording timeline must explicitly show:

- takeover requested
- takeover granted
- human control started
- human control ended
- agent resumed

This is required for mixed human/agent auditability.

Mandatory lineage fields for every browser/desktop recording event:

- `intaris_session_id`
- `conversation_id`
- `task_id | null`
- `step_run_id | null`
- `browser_session_id | desktop_session_id | null`
- `runtime_run_id | null`
- `executor_id`
- `actor`
- `actor_id`
- `control_mode`

Sanitization rules for recording payload metadata:

- URLs should exclude query strings and fragments by default
- known token- or code-shaped query parameters must be redacted if a full URL
  must be retained by policy
- page titles and metadata should be sanitized when they contain obvious secret,
  email, or tenant identifiers not required for audit replay
- profile identifiers included in recording events should be logical IDs only,
  never raw filesystem paths

### Privacy and Retention

Recording policy must support:

- secret redaction in event payloads
- deny-by-default screenshot/video capture on sensitive auth pages unless policy
  explicitly allows it
- retention TTL by recording mode
- delete/revoke behavior for sensitive evidence where allowed by policy

Hard rules:

- plaintext passwords, OTPs, and tokens must not appear in event payloads
- screenshots/video during password, OTP, token entry, or human takeover on
  sensitive auth pages are denied by default unless an explicit policy allows
  capture and defines the masking/redaction behavior
- recording mode must be visible and configurable per executor/runtime

### Browser Takeover

Browser takeover should be an optional executor capability, enabled per
executor. A headed Linux executor may expose a noVNC-backed remote view so the
user can complete login or MFA manually and then hand control back to the
agent.

Takeover access control requirements:

- explicit separation between `view` and `control` permission
- short-lived takeover/session tokens
- exactly one active controller for a live session at a time
- heartbeat or idle timeout for takeover sessions
- explicit reconnect / expiry behavior
- every takeover/view/control event must be auditable

Transport security requirements for noVNC/browser takeover:

- no direct public VNC/noVNC exposure from the executor host
- takeover transport must be brokered by Cognis or another authenticated control
  plane component
- TLS is required end-to-end for takeover traffic outside trusted localhost
  development paths
- viewer/control access must be authorized explicitly per session
- origin validation and short-lived access tokens are mandatory
- executor-local display ports must not be reachable by unintended network
  peers

Recommended feature split:

- Cognis control plane: takeover request, pause/resume, authorization, and
  audit linkage
- executor capability: headed browser + Xvfb/VNC/noVNC transport
- Intaris recording: durable takeover timeline and evidence

This should later extend naturally to desktop/computer-use sessions using the
same recording model.

### Takeover State Model

Takeover state held by Cognis is operational state only and must be restart-safe
enough to recover a consistent user experience:

- `requested`
- `granted`
- `human_active`
- `released`
- `resumed`
- `expired`

If the controller or executor restarts mid-takeover, the session must recover
to either:

- a clearly resumed agent state, or
- an explicit expired/orphaned takeover state requiring user confirmation

Multiple-viewer semantics:

- multiple read-only viewers may be allowed by policy
- only one active controller may interact with the live session at a time
- viewer joins/leaves and controller changes must be auditable

### Recording and Takeover NFRs

The implementation must define and enforce:

- max concurrent headed takeover sessions per executor
- storage/retention budget per recording mode
- p95 takeover handoff latency target
- replay availability target for retained evidence
- bounded orphan cleanup for failed uploads and abandoned takeover sessions

## Quotas and Cleanup

Browser workloads need explicit limits.

Recommended controls:

- max browser leases per executor
- max sessions per lease
- max sessions per user
- idle timeout per lease
- hard timeout per browser tool call
- artifact size limits
- temporary download directory cleanup on lease end
- browser process cleanup on cancellation/disconnect

## Security Requirements

Hard requirements:

- plaintext credentials never appear in LLM prompts
- plaintext credentials never appear in tool results
- plaintext credentials never appear in standard logs or metrics
- executors never own durable credential truth
- revoked or expired credentials fail closed
- credential usage is auditable by id and field, not by value

## Versioning and Revocation

Credential and auth-state lifecycle must support rotation.

Rules:

- every update increments version
- revoked credentials cannot be resolved
- expired credentials cannot be resolved without explicit re-authorization
- browser auth state may be invalidated independently of other credentials
- executor-side cached plaintext must not survive beyond the active runtime

## API and Model Direction

The product-facing abstraction should be `credentials`, even if the initial
implementation reuses current encrypted secret persistence internally.

Likely API additions:

- `GET /api/v1/credentials`
- `POST /api/v1/credentials`
- `DELETE /api/v1/credentials/{id}`

Existing notification/question APIs should be extended to support auth request
payloads for both `request_credential` and `request_auth_challenge`
orchestration.

## Migration Direction

Recommended implementation order:

1. introduce credential records and APIs
2. bridge current secret storage/provider internals if needed
3. add credential ref resolution for executor tools
4. add auth request orchestration flow
5. add auth challenge orchestration for MFA and step-up verification
6. add Playwright browser runtime leases and tools
7. add browser auth state persistence and restore

## Operational Caveats

This design is cloud-native enough for Cognis, but must be documented honestly.

Caveats:

- some sites bind sessions to IP, region, device fingerprint, or timing
- `storageState` restore is best-effort, not guaranteed portability
- exact page state is not durable across executor loss
- headed browser mode is primarily for compatible local executors
- MFA challenges often depend on keeping the same live browser session through
  the approval window

The supported contract for temporary executors is:

- durable auth state is portable
- live browser state is ephemeral
- re-auth is a first-class fallback, not an exceptional edge case
- MFA is a first-class human-interaction fallback, not an afterthought

## Observability

Recommended metrics and events:

- browser lease create/destroy/failure counts
- browser session counts and cleanup counts
- auth state restore success/failure rates
- re-auth request counts
- auth challenge counts by kind
- MFA resume success/failure rates
- credential resolution failures
- executor loss during browser activity
- artifact volume generated by browser tools

## Summary

This design keeps Cognis aligned with its core architecture:

- the controller remains the durable control plane
- the executor remains the disposable execution hand
- credentials become the structured auth abstraction for agents and browser use
- browser auth is portable enough for cloud-native executors without depending
  on durable local profiles

The central trade-off is intentional: Cognis supports rehydratable authenticated
browser work across temporary executors, but does not pretend that exact browser
process state is portable.
