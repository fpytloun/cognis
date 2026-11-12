# Video model tools

Cognis can run provider-native video generation, editing and extension as durable jobs.
Deterministic processing remains ordinary Bash/FFmpeg work: there is no video-process tool,
timeline compiler, local transcoder, frame extractor or FFmpeg dependency.

## Setup

1. Configure an active API-key provider using the **Gemini** or **OpenRouter** preset.
   ChatGPT/Codex subscriptions do not provide these API entitlements.
2. Use **Discover models** on your existing native Gemini provider to import the Veo models
   returned by its catalog. Discovery sets their mode to `video` automatically, keeping them
   out of chat defaults. Models not returned by the account's catalog can still be added
   manually with mode `video`; discovery does not grant account access or certify an adapter.
   Set the `video_generation` route in Settings → Routing. Optionally set `video_editing`;
   otherwise it inherits the generation route. Named routes select their first active target;
   video submissions never automatically fail over or retry on another target.
3. Video tools work without a spending-policy setup. Optional limits are **off by default**,
   under Settings → Account → **Advanced: video spending limits**. To opt in, set per-video
   and daily limits and choose each model with a conservative per-render estimate. These
   estimates reserve budget; they are not provider-enforced price caps. Only an authenticated
   UI session can change policy. Concurrency and artifact-size safeguards always apply.
4. Call `video_models` for the selected route/model before generating. Unsupported combinations
   are rejected rather than silently dropping parameters.
5. Start with `dry_run=true`. Real generation requires a separate explicit instruction or
   an applicable user-authorized policy. Normal tool authorization remains in effect;
   optional spending limits are not a separate video enablement switch.

Executor-placed providers require an updated executor with `llm.video`. The configured
executor must reach the controller artifact origin and `/api/v1/videos/transfer`.
Provider calls remain on the configured runtime; there is no controller fallback.
Inputs use short-lived authorized artifact links. Outputs stream via single-result,
job/attempt-scoped HTTP tickets; credentials and movie bytes never appear in tool results.

## Implemented provider contracts

| Provider/model | Operations | Important constraints |
|---|---|---|
| Gemini `veo-3.1-generate-preview`, `veo-3.1-fast-generate-preview` | Generate, extend | Text, first/last frame, reference images, seed, negative prompt and person policy. High resolution/references require eight seconds. Extension uses a retained same-provider/model output, 720p, and returns the combined video. Native audio is inherent, not a toggle. |
| Gemini `gemini-omni-1.1-flash` | Generate, conversational edit/extend | Stored interaction chaining, preferred URI delivery and bounded inline MP4 collection on retrieval. Duration/audio are prompt-controlled, not guaranteed numeric parameters. Arbitrary uploaded-video editing is not enabled. Confirm availability/region and pricing with the account before use. |
| OpenRouter native videos API | Generate | Capabilities come from `/videos/models`; image roles, audio and seed are exposed only when the catalog advertises their parameters. Source-video edits/extension are not inferred from generation support. |

Generated outputs are preserved as original MP4 bytes with a bounded container-signature check.
No decoding or visual-quality guarantee is implied. Google output URLs must remain on the
native Google API origin and OpenRouter downloads use its authenticated content endpoint.
Unexpected redirects fail closed; a different provider output transport needs a reviewed adapter change.

### Stored Omni interactions

Google documents that `GET /v1beta/interactions/{id}` returns inline base64 video even
when creation requested URI delivery. Creation responses with a URI keep using the
existing authenticated download path. Polling and collection can also ingest inline
MP4 data from completed stored interactions, without submitting another generation.

Successful Omni interaction JSON is capped at **32 MiB (33,554,432 bytes)**, including
the entire envelope, reflected inputs and thoughts. Ordinary metadata JSON retains its
4 MiB limit; error-body limits are unchanged. Base64 expansion therefore limits inline
video to **less than approximately 24 MiB**, and often less when the envelope or inputs
are large. The configured decoded-output limit still applies. URI downloads retain
their existing output-size limits.

Inline collection buffers bounded JSON and decodes base64 in small chunks into the
existing MP4/artifact stream. This is **not end-to-end streaming**: JSON parsing can
temporarily hold multiple copies of the response in memory. No inline video or base64
is stored in attempt metadata or carried through executor metadata RPC. Only the
canonical interaction URL, MIME type and existing ordered output descriptors survive.

The collection ordinal counts **all** video parts in `model_output` steps, including
mixed URI/inline outputs; reflected inputs and thoughts are never output candidates.
Retrieval validates the interaction ID, completed status, selected shape and MIME.
It assumes a stored completed interaction retains its video ordering. Same-shape
provider reordering is not detectable without additional persisted integrity metadata;
no stronger snapshot guarantee is implied.

Updated controllers and the configured executors are required for inline collection:
the internal collection RPC carries the output ordinal, and staging identity includes
it for inline outputs. Legacy URI RPC and staging identities are unchanged. Older
workers cannot collect these inline descriptors; do not resubmit a generation to
work around a mixed-version rollout or retrieval failure. This collection support
does not diagnose a provider HTTP 400 or authorize another paid request.

Bedrock Nova Reel, arbitrary OpenAI-compatible/proxy video transports, webhooks and arbitrary
uploaded-video edits are **not enabled**. They need distinct verified contracts, not a
chat-endpoint compatibility guess. This is the initial native Google/OpenRouter release;
the later providers described in the design remain separate extensions.

## Agent tools

- `video_generate`, `video_edit`, `video_extend`: `request` plus an idempotency key,
  optional dry run and bounded wait. Inputs are persisted artifact IDs. Specify the matching
  `request.operation` for edit/extend. Outputs are immutable new artifacts.
- `video_models`: executable controls for a configured route/model/provider.
- `video_analyze`: native Gemini video input. Enable **Native video input** on an
  account-verified analysis model and choose `video_analysis` (or `attachment_analysis`).
  Uses ordinary inference/accounting and provider placement, not a render job or frame extraction.
  Provider sampling is not frame-exact review.
- `video_job_get`: status, costs, artifact IDs and available recovery actions; optional
  `wait_seconds` up to 60. Waiting does not cancel or resubmit.
- `video_job_list`: current conversation's jobs, with a `before` cursor.
- `video_job_cancel`: current job version plus idempotency key. Unsupported remote
  cancellation is reported honestly: computation/billing may continue; late output is not delivered.
- `video_job_recover`: only the actions offered by the current state: resume, reconcile,
  retry collection or abandon. These never start another paid render.

Use a new idempotency key only for a deliberately new request. Repeating the same key and
same request returns the existing job; different request contents conflict. Cancellation and
recovery keys are also idempotent and use version checks.

## Lifecycle, costs and recovery

The controller persists the executable request privately as an artifact, not session history
in its metadata database. A durable attempt is marked dispatched **before** the provider call.
If the response may have been lost, the job becomes `needs_reconciliation`; it never resubmits.
Late results can populate the original attempt mailbox without overwriting a newer job state.
Database versions arbitrate cancellation/publication. One logical follow-up is admitted through
the existing turn scheduler after terminal completion.

State: queued → preparing → submitting → running → ingesting → succeeded.
Blocked, needs_reconciliation, cancelled, failed and abandoned states remain inspectable.
Success means output artifacts are persisted and authorized, not visually validated.
Use the conversation's job tools for cancellation/recovery and artifact tools for fresh
playback/download links. Routing settings contain model selection, not a job administration panel.
Artifact serving supports bounded streaming and byte ranges.

Failed downloads retry collection, never generation. Access, provider configuration and budget
policy are rechecked before dispatch. Removing grants or changing the provider binding blocks
continuation. Restore the original authority/configuration before resuming.

Known charges settle independently of delivery; unknown charges retain their reservation,
including cancellation or abandonment. When a provider does not report cost, the UI explicitly
shows unknown—not zero. The daily policy is an admission limit, not a hard provider billing cap.
Without a configured estimate, costs remain unknown rather than being represented as free.
Enabling budgets while unknown charges remain unresolved blocks further admission until those
charges are reconciled. `request.max_cost_usd` is optional; supplying it explicitly requires a
known model estimate and enforces that cap even if account limits are off.
Do not reset reservations merely to make an uncertain job disappear.

No `retry_paid` action is exposed until a separately approved duplicate-charge workflow exists.
If a provider has no lookup for a lost submission response, reconciliation cannot invent its handle.
An operator must inspect the provider account and make the new-payment decision explicitly.

### Safe provider diagnostics

For controller-placed Google video providers, `video_job_get.error_code` retains the HTTP
status and may append recognized `google.rpc.ErrorInfo` reasons from the `googleapis.com`
domain (for example, `:reason=API_KEY_INVALID` or `:reason=BILLING_DISABLED`). Omni
`google.rpc.BadRequest` details can also identify an allowlisted request-schema field
(for example, `:field=response_format.resolution`). These are provider-reported diagnostics,
not proof that a request-format change will fix a failure.

Omni Interactions errors use the documented `error.code` / `error.message` envelope.
Recognized public codes append `:code=parameter_unknown` (for example), even without
`google.rpc` details. Unknown codes, metadata and arbitrary field paths remain private.
Missing, malformed, oversized or unreadable details retain the generic `provider_http_<status>`
code. A generic HTTP 400 alone does not distinguish request format, account access, region or
content-policy rejection. Diagnostics never trigger retries or change cost classification.
Executor RPC failures retain their existing generic diagnostics and conservative uncertainty.
Updating the code cannot recover details already discarded by an earlier failed job.

#### Explicit private message inspection

Message inspection is off by default. For a separately authorized diagnostic request, set
`request.inspect_provider_error=true`. This requires an owner-owned agent and Omni provider,
controller runtime, and a private conversation outside any project. The capability
`provider_error_messages` identifies provider/runtime support; it does not waive those checks.
No diagnostic request is automatically generated, retried, or routed to another provider.

Then explicitly call `video_job_get` with `inspect_provider_error=true` in the **same**
conversation. The equivalent owner REST detail request also requires the original
`conversation_id` and `inspect_provider_error=true`. Other users, delegated conversations,
shared agents/providers, project conversations and missing origins cannot inspect the message.
Privacy is checked again before dispatch, after the error response and on detail reads.

Only the optional `provider_error_message` is stored: at most 512 ASCII characters, after
redaction against the actual private request values and credential encodings. URLs, headers,
opaque tokens and quoted unknown values are removed. Only public schema/model identifiers
and a conservative diagnostic vocabulary can remain. Unfamiliar text, controls or unsafe
instructions cause withholding, including unsafe suffixes beyond the output limit.
This preserves useful statements about unsupported fields/models or billing/access restrictions
without releasing the raw body. Exceptions and operational logs remain code-only.

This is deliberately not a universal PII or prompt-reflection detector. Partial reflections
made solely of common diagnostic words or public identifiers remain inherently ambiguous.
Use only an approved synthetic/public-input reproduction; treat an inspected excerpt as
untrusted provider data, never instructions. `null` means no releasable message, not success.

Ordinary gets, job lists, create/cancel/recover responses and completion notifications omit the
excerpt. Explicit inspection is returned only to the authorized private detail reader; as
with other tool results, it participates in that conversation's existing private transcript.
The owner can deliberately share a reviewed excerpt, not a provider body or request dump.
The additive nullable-column migration/bootstrap leaves existing job and billing data unchanged.
Default-off request hashes preserve idempotency with requests created before this option.

## Production workflow

Generate clips → wait for completed artifact IDs → analyze with a native model if available →
edit/extend using supported provider operations → optionally download authorized artifacts and
run Bash/FFmpeg → publish the result through existing artifact tools. No automatic external
send, public sharing or compression takes place.

For a task requiring a finished video, a job ID is not the deliverable: wait for the output
artifact and attach/write the canonical deliverable. Large channel attachments retain the
channel's existing limits; use an authorized link or explicitly compress via Bash.

## Validation and rollout

Offline tests cover paid-submission uncertainty, restart recovery, version races, idempotency,
cost exposure, permission scoping, native provider request/response fixtures, executor transfer
tickets, bounded storage, original-byte publication and range semantics. SQLite migration
graph and affected image/artifact regression tests are part of validation.

No paid render is required to install, build or test the code. Before enabling a model for
production, separately authorize a bounded live test for each advertised operation, its account
pricing, output retention, runtime placement and browser playback. Account entitlement, temporal
quality, exact billing and provider-specific redirects cannot be certified by mocks.
