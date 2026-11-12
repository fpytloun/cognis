# Troubleshooting

## Setup problems

- **Setup page says the token is invalid or expired**
  - Restart Cognis to print a new setup URL.
  - Or create the first user locally with `cognis-controller admin create-user ...`.

## UI problems

- **The PWA returns to login after resuming**
  - Startup tries the refresh cookie when the access cookie has expired.
  - A temporary browser storage-coordination failure revokes existing document
    access and shows a loading/recovery state, rather than declaring the session
    expired. Recovery rechecks coordination and the server session, including
    when the app comes online or becomes visible. Persistent storage failure
    remains fail-closed; use the workspace Retry action once storage is available.
  - Explicit logout is not undone by automatic recovery, even if storage fails.
  - A deployed service-worker update can also reload the installed PWA. That
    update reload is separate from authentication recovery.
- **The browser opens `:8080` but no UI is shown**
  - Confirm bundled UI assets are present.
  - If you intentionally split deployments, set `COGNIS_SERVE_UI=false` and serve the UI separately.
- **The UI loads but setup still looks incomplete**
  - Open the onboarding guide or system diagnostics and check which readiness step is still pending.
  - Confirm that a provider and at least one agent already exist.
- **Opening an older chat times out after 15 seconds**
  - The initial timeline request has a 15-second browser deadline. A snapshot
    cache hit does not bypass fresh attachment authorization or URL hydration.
  - Compare `cognis_chat_snapshot_request_latency_seconds` with
    `cognis_chat_snapshot_request_stage_latency_seconds` at `/api/metrics`.
    The stage labels are `context` (metadata and runtime context), `immutable`
    (cache lookup or projection build, including shared rebuild waits), and
    `attachments` (request-time attachment authorization and URL hydration).
    Context timings include successful context loads; immutable and attachment
    timings also include failed or cancelled operations.
  - Snapshot and history reads batch attachments within each source-session
    authority. Exact artifact reads resolve ownership and explicit grants before
    falling back to recursive work provenance; they do not cache access decisions.
  - PostgreSQL attachment hydration disables JIT only within its short database
    transaction. This avoids expensive compilation of overestimated recursive
    access plans without changing server defaults or leaving pooled connections
    modified. The transaction closes before URL generation.
  - Investigate the slow stage before extending deadlines or flushing caches.

## Slow chat replies

Runtime preparation batches visible skills' current versions and assets rather
than issuing one pair of queries per skill. Executor selection and inventory
assembly reuse one build-owned pool observation when it contains the selected
executor; a concurrent pin winner outside that observation triggers a fresh
resolution. Context and tool admission still refresh eligibility independently.
Forwarded catalog reads use the physical owner's catalog cache, with bridge
ownership checks and an overall deadline covering cache-lock waits. Forwarders
do not retain a separate catalog cache.

- Correlate `turn: stage completed` logs by `session_id` and `turn_id`.
  `runtime.resolve` covers runtime admission, while `runtime.build_attempt`
  covers each attempt, including an interrupted attempt before recovery.
- Runtime child stages distinguish `runtime.policy_load`, `runtime.pin_read`,
  `runtime.scope_resolve`, `runtime.executor_select`, `runtime.pool_resolve`,
  `runtime.web_config`, `runtime.image_tool_descriptions`, and
  `runtime.skills_resolve`. Remote admission additionally records
  `runtime.catalog_read`, `runtime.inventory_merge` (including program policy
  and MCP composition), and `runtime.registry_build`.
- `runtime.recovery_wait` measures the connection wait only, not durable
  recovery bookkeeping or retry backoff. Stages are emitted only for executed
  paths. They contain identifiers, duration, and outcome, not request content.
- Do not add parent and child durations: they overlap. Child stages are not
  exhaustive; compare them with the enclosing attempt to locate remaining work.
  An `interrupted` outcome means the operation raised or was cancelled, even
  when the caller subsequently recovered.
- Provider stream durations are not isolated model compute. Logged time to first
  token may include reasoning or tool deltas, not visible text. Measure browser
  submission, first visible text, and completion separately before claiming an
  end-to-end latency improvement.

## Provider problems

- **Provider test fails with auth error**
  - Check the environment variable for the provider API key.
- **Provider test fails with connection error**
  - Check proxy/base URL settings or Ollama availability.
- **Provider test fails with model not found**
  - Verify the configured default model.

## Executor problems

- **Tools are missing for an agent**
  - Confirm the selected executor has those tool groups enabled.
  - Confirm the agent did not disable the tool category or tool itself.
- **A provider or channel depends on a remote executor but does not work**
  - Confirm the executor is connected and healthy.
  - Confirm labels or explicit executor binding resolve to the expected executor.

## Companion service problems

- **Mnemory unreachable**
  - Verify `COGNIS_MNEMORY_URL`.
- **Intaris unreachable**
  - Verify `COGNIS_INTARIS_URL`.
- **JWT validation fails in companion services**
  - Confirm they are using Cognis's public key or JWKS endpoint.

## API key problems

- **API key stopped working**
  - Check whether it expired.
  - Revoke and create a new one from **Settings → Account** if needed.

## Docs and onboarding problems

- **The onboarding guide links out to the wrong place**
  - Use the in-app `Docs` section for bundled user guidance.
- **A docs page does not match the current UI**
  - Check whether the running instance is older than the repository docs and bundled UI assets.
