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
- supported browser engines
- headless support
- headed support
- max parallel browser sessions
- download support
- upload support
- max artifact size
- Playwright/runtime version

Production deployments should standardize a browser-capable executor image or
executor class to reduce drift.

## Browser Tool Set

Initial executor-native browser tools:

- `browser_open`
- `browser_snapshot`
- `browser_get_text`
- `browser_click`
- `browser_fill`
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

### `browser_snapshot`

- returns a size-bounded representation of the current page
- may include URL, title, visible text summary, and selector/locator hints
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
  `browser_get_text`, `browser_screenshot`
- write/non-bypassable: `browser_click`, `browser_fill`, `browser_press`,
  `browser_upload`, `browser_submit`-like actions

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
