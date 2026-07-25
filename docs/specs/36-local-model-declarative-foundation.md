# Declarative Local-Model Foundation

Status: desired-state foundation, executor-managed Ollama reconciliation, Local Models catalog, and
advisory capacity-planning UI.

## Scope and ownership

`LocalModelDeployment` is durable controller-owned desired state. Cognis currently has no workspace
entity, so deployments follow the existing owner scope:

- a private deployment uses the human owner's email;
- a shared deployment uses `system@cognis.local`;
- viewer accounts cannot mutate desired state;
- a private deployment is mutable only by its owner;
- a shared deployment is readable by authenticated users but mutable only by an admin.

Selectors are declarations, not authorization. Before persistence, Cognis resolves them to concrete,
active `executors` rows and creates one `LocalModelTargetStatus` per executor. Exact IDs and label
matches form a union. Regular users may resolve only their own executors. Admin-owned private
deployments may additionally use shared executors. Shared deployments may use only shared executors.
An inaccessible exact ID is rejected rather than silently broadened.

## Model references

The parser accepts:

- Ollama-native names such as `llama3.2`, `library/gemma3:4b`, and references under
  `registry.ollama.ai` or `ollama.com`;
- Hugging Face GGUF references in the explicit form `hf.co/org/repo:quant`.

Untagged Ollama names canonicalize to `:latest`. URLs, absolute or traversal paths, backslashes,
control characters, whitespace, unsupported punctuation, malformed Hugging Face references, and
unknown registries are rejected. The original request, canonical name, runtime name, source, and
revision are stored separately.

## Desired-state contract

A deployment stores:

- `runtime_type` (`ollama` in this slice);
- requested/canonical/runtime references, source, optional digest and revision;
- the declarative selector and its materialized target rows;
- desired state (`present` or `absent`);
- update policy (`if_changed`, `always`, or `manual`);
- prune policy (`retain` by default, or `delete`);
- reconciliation concurrency (`max_parallel`) and monotonically increasing `generation`;
- one required executor-backed Ollama provider link. The provider owns the maximum host scope and
  deployment targets must be a subset of the currently resolved provider hosts;
- capacity assessment generation and an explicit persisted override acknowledgement;
- create/update and reconciliation-request timestamps.

Capacity prediction is advisory. A future predictor may record an assessment generation, but it must
not silently override explicit desired state. A caller may acknowledge an override and that decision
remains part of the deployment generation.

Legacy provider-less rows remain readable as `lifecycle_state=needs_provider`, but are not
materialized or reconciled until a provider is attached. New deployment requests require
`provider_id`. Provider deletion is blocked while any deployment references it.

Deleting a deployment deletes controller desired state and its generated provider-model reference.
The provider model entry is removed only when no other deployment or manual provider configuration
references it. Physical Ollama data remains governed by desired state and prune policy. To express
physical deletion intent, set `desired_state=absent`; the default `retain` policy is non-destructive.

## Target status

Each target is unique by `(deployment_id, executor_id)` and stores the desired generation,
observed generation, target state, observed digest/size, current operation ID, sanitized error, and
reconciliation timestamps.

New or changed targets remain `pending`. A reconciliation request increments deployment generation,
sets request timestamps, and re-materializes the authorized selector. It does not contact an executor
or create an operation.

Target states:

- `pending`: desired state has not been observed for the current generation;
- `reconciling`: a real future executor operation is active;
- `ready`: desired presence is observed;
- `absent`: desired absence is observed;
- `blocked`: policy/capacity/authorization prevents execution;
- `error`: reconciliation failed and requires retry or desired-state change.

## Durable operation state machine

Operations are durable controller records for executor-local pull/delete work. The controller persists
every transition and monotonic progress update through CAS helpers before exposing the state.

Allowed transitions:

| From | To |
|---|---|
| `queued` | `running`, `cancel_requested`, `cancelled`, `interrupted` |
| `running` | `cancel_requested`, `succeeded`, `failed`, `interrupted` |
| `cancel_requested` | `succeeded`, `failed`, `cancelled`, `interrupted` |
| `interrupted` | `queued`, `cancel_requested`, `succeeded`, `failed`, `cancelled` |
| `succeeded`, `failed`, `cancelled` | terminal |

An operation records action, deployment/executor/generation, state, progress sequence, bytes, phase,
idempotency key, request hash, sanitized error, and lifecycle timestamps.

Idempotency is scoped to `(deployment_id, idempotency_key)`. Reuse with the same request hash returns
the existing operation; reuse with a different hash is rejected. Progress sequences and byte counts
are monotonic. Repeating an identical progress sequence is a no-op; conflicting data at the same
sequence is rejected.

## API surface

- `GET /api/v1/local-model-catalog`
- `GET /api/v1/local-model-catalog/resolve`
- `POST /api/v1/local-model-fit-plans`
- `GET/POST /api/v1/local-model-deployments`
- `POST /api/v1/local-model-deployments:managed`
- `GET/PATCH/DELETE /api/v1/local-model-deployments/{deployment_id}`
- `GET /api/v1/local-model-deployments/{deployment_id}/targets`
- `GET /api/v1/local-model-deployments/{deployment_id}/operations`
- `POST /api/v1/local-model-deployments/{deployment_id}/reconciliation-requests`
- `POST /api/v1/llm-providers/{provider_id}/local-models:upsert`
- `POST /api/v1/local-model-providers/recommendations`
- `POST /api/v1/local-model-providers:find-or-create`

Recommendation returns only authorized, active, executor-backed Ollama providers resolving at least
one eligible host and capable of containing the requested deployment selector. Ranking is stable:
providers already containing the model lead, followed by healthy host count, user ownership in user
scope, Cognis-managed local reuse, host count, and provider ID. Reason codes make the ordering
inspectable.

Find-or-create reuses the first eligible provider unless explicit creation is requested. Managed
providers are idempotently keyed by owner/scope, Ollama runtime, and normalized host selector, named
from the host or labels, and marked in provider metadata. `local-model-deployments:managed` performs
provider resolution/creation and deployment creation in one transaction, so validation failure cannot
leave an orphan provider.

## Managed Ollama runtime

WebSocket executors expose a managed Ollama capability after `executor.configure`. Configuration lives
under the executor's existing `config.ollama_runtime` block:

```json
{
  "ollama_runtime": {
    "port": 11434,
    "management_enabled": true,
    "max_concurrent_pulls": 1,
    "disk_headroom_bytes": 5368709120,
    "request_timeout_seconds": 1800,
    "model_store_path": "/var/lib/ollama/models"
  }
}
```

The port defaults to `11434`. Cognis derives the endpoint as
`http://127.0.0.1:<port>` and accepts no configurable host. The historical
`endpoint: "http://127.0.0.1:11434"` JSON shape is normalized to port `11434`;
other endpoint values are rejected. Model mutation requests cannot supply an
endpoint, credentials, headers, filesystem path, or query string. Redirects
are rejected and managed HTTP clients ignore proxy environment variables.
Existing executor-side `llm.discover_models` remains read-only and continues
to support its prior provider endpoint/auth compatibility.

Desired local-inference configuration is fail-closed. Routing and management
remain unavailable while desired and applied generations differ or until the
live capability advertises matching local-inference/management flags, port,
and effective loopback endpoint.

`model_store_path` is executor configuration, not mutation input. It must be the absolute path used by
the local Ollama service (or `OLLAMA_MODELS` must be set in the executor environment). Pulls are
refused when that filesystem cannot be identified; disk headroom is never estimated from the
executor user's home directory.

The executor provides typed probe/version, installed (`/api/tags`), show, running (`/api/ps`),
streaming pull, cancellation, and delete operations. Pulls are serialized by an executor-wide
semaphore (default one) and all mutations take a per-model lock. Disk headroom is checked off the
event loop before a pull. Operation IDs and request hashes make starts idempotent. Progress, payloads,
model lists, errors, and the local completed-operation registry are bounded.

Cancellation aborts the active HTTP stream and acknowledges the request. It does not promise rollback:
Ollama may retain already-downloaded data. Cognis does not prewarm models and does not issue implicit
load/unload calls.

## Reconciliation and runtime API

`LocalModelReconciler` re-resolves declarations against currently authorized database executors and
then intersects them with provider-resolved hosts, so neither label selectors nor provider routing
become implicit authorization. Provider host eligibility is isolated behind
`LocalModelHostEligibility` for later capability gating. Provider-less legacy deployments are skipped.
The reconciler runs after desired-state changes, executor connect/label changes, operation completion,
and a bounded periodic resync. It computes the union of present references before pruning:

- offline targets remain pending;
- missing present models are pulled;
- `/api/show` must succeed before a target becomes ready;
- wipe drift changes a previously ready target back into a pull;
- `retain` is non-destructive;
- delete requires both `desired_state=absent` and `prune_policy=delete`;
- a model referenced by any present deployment on the executor cannot be deleted;
- retries use bounded exponential backoff with jitter and deployment `max_parallel`;
- no reconciliation path prewarms a model.

Runtime endpoints are concrete-executor scoped:

- `GET /api/v1/executors/{executor_id}/local-model-runtime`
- `POST /api/v1/executors/{executor_id}/local-model-runtime/operations`
- `POST /api/v1/executors/{executor_id}/local-model-runtime/operations/{operation_id}/cancellation-requests`
- `GET /api/v1/local-model-deployments/{deployment_id}/status`

An operation request contains only a deployment ID, action, and idempotency key. The referenced
deployment supplies the canonical model name, exact authorized target, and automatic provider-upsert
intent. Shared executor/deployment mutations are admin-only and viewers cannot mutate. Delete
dependency conflicts return HTTP 409 with the blocking deployment IDs. The exact runtime operation API
remains available for admin/diagnostic use with legacy provider-less deployments.

For provider-owned deployments, successful pull completion and readiness observation automatically
upsert the model through the provider-row lock. The model is therefore selectable only after it is
ready. Generated references are tracked by deployment ID. A provider default is set only when no
default exists and is never overwritten.

Provider reassignment, deployment selector changes, and provider routing changes are rejected while
operations are active or when the resulting deployment targets would fall outside the provider host
scope.

Executor-routed Ollama inference becomes readiness-aware when a provider/model is linked to managed
deployments. Only ready targets at the current generation are eligible. If none are ready, the error
contains a rollout summary with target states instead of the generic selector failure. A persisted
capacity override acknowledgement explicitly opts that deployment into normal selector routing.
Providers/models without managed deployments retain their prior behavior.

## Catalog and capacity planner

The catalog is an adapter boundary rather than a runtime mutation path:

- the bundled Ollama list is curated in Cognis and does not scrape undocumented Ollama APIs;
- Hugging Face search is a fast, bounded `full=true` public model page. It does not eagerly hydrate
  repositories, so intentionally basic metadata is not a partial-source warning. Visible or selected
  cards use a separate cached/coalesced detail resolver with at most four concurrent requests, an
  eight-second deadline, bounded JSON/README bodies, strict `https://huggingface.co` endpoint and
  redirect validation, and per-item errors;
- the installed catalog source is not yet wired into runtime inventory; managed runtime status still
  exposes installed models through the exact-executor runtime endpoint and the Installed view shows
  deployment targets and operations honestly;
- direct references use the same canonical WS2A parser as deployment creation;
- upstream records normalize repository/model-card links, metadata revision SHA, a sanitized README
  excerpt, downloads, likes, modification time, license, pipeline/tags/base models, architecture,
  parameter/context metadata, quantizations, exact artifact sizes, metadata status/confidence,
  reference integrity, and diagnostics. Projector and `mmproj` GGUF files are excluded from both
  completeness and size calculations.

The pull reference remains `hf.co/repository:quantization`. Repository metadata may record the SHA it
was read from, but Cognis never describes the pull as SHA-pinned. The floating-revision explanation is
kept in selected-model advanced details and final confirmation instead of warning on every catalog
card. Split GGUF sizes are summed only when every shard in one complete set has a bounded size;
incomplete or independent artifacts remain unknown rather than using one shard or a partial sum.

Catalog filters provide parameter presets `≤4B`, `4–8B`, `8–14B`, `14–32B`, `32–70B`, and `70B+`;
selected-quant download presets `≤4`, `4–8`, `8–16`, `16–32`, and `32+ GiB`; plus quantization,
minimum-context, and include-unknown controls. Filters apply to the current bounded upstream page and
never trigger unbounded repository fanout. The upstream cursor is preserved, and the response says
that a filtered page may therefore be short. Search cache keys include all filters; detail cache and
coalescing keys are repository plus revision SHA.

Fit plans consume one exact artifact, any positive requested context, a normal deployment selector,
and the latest WS1 resource snapshot. Results remain independent per executor and include static and
current-admission assessments. Unified memory is a single pool; discrete accelerator and host memory
are kept separate so offload is reported explicitly. Missing snapshots, required memory fields, or
model metadata produce `UNKNOWN`, never an optimistic guess. Arithmetic is bounded to the resource
snapshot integer contract.

The estimator reports weights, a KV-cache range, runtime buffer, reserved OS headroom, confidence,
reason codes, and snapshot age. Standard context choices are 8k through 256k plus the model's
advertised maximum. The group recommendation is the highest context that is green on every selected
executor, capped at 128k. This recommendation is advisory: custom contexts above that cap or above the
advertised maximum are still assessed and submitted unchanged.

`assessment_generation` is a SHA-256-derived marker over the exact model metadata, requested context,
selected executor IDs, and resource snapshots, bounded to JavaScript's exact 53-bit integer range.
Persisting it on a deployment therefore ties a capacity override acknowledgement to the exact
advisory request without pretending that context is a runtime desired-state field.

## Product surface

`/local-models` is a first-class route with Catalog, Deployments, Installed & targets, and Operations
views. The deployment planner supports exact executor IDs or a label-selector preview, exact
quantization choice, a logarithmic context control, per-executor plain-language results, and expandable
technical assumptions. Red or unknown plans require an explicit confirmation; deployment creation
persists `capacity_override_acknowledged` and the assessment generation.

The UI creates desired-state deployments rather than sending arbitrary imperative model URLs.
Runtime operations and rollout status are handled through the managed Ollama reconciliation APIs.

## Database operations

Local-model byte counters use signed 64-bit storage and API/runtime bounds. Revision
`092_local_model_byte_bigint` widens `local_model_operations.progress_bytes` and
`local_model_target_statuses.observed_size_bytes`. PostgreSQL operators applying the equivalent
hotfix manually may run:

```sql
ALTER TABLE local_model_operations
  ALTER COLUMN progress_bytes TYPE BIGINT;
ALTER TABLE local_model_target_statuses
  ALTER COLUMN observed_size_bytes TYPE BIGINT;
```

Both changes preserve existing nullability, defaults, and constraints. Downgrade intentionally
refuses to narrow either column while a value exceeds `2147483647`.
