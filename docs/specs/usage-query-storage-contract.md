# Usage query and capture contract

This defines the shared storage contract for capture and query implementations.
Retention, projection and deletion remain owned by their lifecycle services.

## Capture vocabulary and bounds

Import from `cognis.models.usage`:

- `UsageModelName`: printable Unicode, 1–512 characters. Internal `UsageId`
  remains the independent ASCII-safe 160-character identifier type. Model
  selection, reported model, prices and quota model buckets use the model-name
  bound; database model-name columns match it.
- `UsageDisplayLabel`: bounded printable Unicode, including normal Czech/emoji
  hook names. Control/bidi override characters remain rejected.
- `UsageParameterName`: 16 safe scalars, including `reasoning_enabled`.
  `USAGE_PARAMETER_PROVENANCE_MAX_ITEMS = 16`.
- `UsageClientOperation`: the closed Intaris/Mnemory vocabulary defined in
  [`cognis-common`](../../packages/common/src/cognis/models/usage.py).
  Unknown external operation labels are ignored, not promoted to
  trusted authority and not allowed to discard a complete attempt.
- `UsageNumericExtra`: token units plus input/output audio/video duration in
  integer **milliseconds**, image counts, input/output/billed characters.
  Non-token extras require their declared unit. Names/units are a closed
  vocabulary, never arbitrary JSON or a text-token conversion.

`USAGE_OBSERVATION_MAX_BYTES = 8192`, measured using
`model_dump_json(exclude_none=True, exclude_defaults=True).encode()`.
The independent queue remains at most 1,024 items / 4 MiB, including the in-flight
batch; batch bounds remain 64 items / 128 KiB.

The full dense test retains verified resource references, Unicode hook metadata,
180-character requested/effective/reported model names, both complete 16-scalar
parameter snapshots, all numeric extras, timings and safe error metadata.
Its original object-per-field provenance form measured 8,692 bytes (5,869-byte
context), exceeding 8 KiB. The contract therefore supports **lossless** provenance
compaction:

```python
parameters.provenance_default  # source for all present scalars unless overridden
parameters.provenance         # explicit per-field exceptions
parameters.provenance_source(parameter_name)  # sole complete lookup
```

Complete input provenance lists are canonicalized to default plus exceptions
when this saves space. Scalars and their semantic sources are not dropped.
Do not infer unknown provenance by inspecting only the exception tuple.
Incomplete input snapshots do not acquire a fabricated default.

With lossless compaction the full event measures **6,878 bytes**, including
**4,055 bytes** of context and **2,823 bytes** of terminal metadata. With all
three model-name fields expanded to 512 characters, the same event remains
below 8 KiB. `USAGE_CONTEXT_TARGET_BYTES = 4864` and
`USAGE_TERMINAL_RESERVE_BYTES = 3328` replace the previous 2,300-byte heuristic.
They are fitting targets, not a guarantee that every combination of maximum
field lengths fits; the full-envelope ceiling remains authoritative.

Do not keep the old fixed 2,300-byte context threshold or clear all parameters.
Check the complete bounded envelope and use lossless compaction first. A genuinely
oversized optional-metadata case must retain verified attribution and record
`UsageMetadataReduction(reason="envelope_budget", fields=(...))` in the immutable
attempt context **before start**; final repeats that exact context. Request-stat
observations have the same optional reduction declaration. Permitted field names
exclude verified resources, subject/principal/payer identities, model and trace
identity. The service exposes/persists `metadata_reduced_events`; capture retains
its own `reduced_contexts` diagnostic. Those are event/context counts respectively,
not proof of lost physical attempts. No budget failure may fail inference.

## Dimension encoder and decoder

Import from `cognis.core.usage.dimensions`:

```python
UsageDimensions
UsageDimensionEncoding
encode_usage_dimensions(dimensions, *, grain) -> UsageDimensionEncoding
decode_usage_dimensions(mapping_or_json) -> UsageDimensions
USAGE_METRIC_POPULATIONS
UsageRequestMetrics
request_metric_values(*, server, client) -> UsageRequestMetrics
```

`grain` is `session`, `hourly`, `daily` or `archive`. Encoding uses sorted,
compact, UTF-8 JSON with explicit nulls/defaults, including `schema_version=1`;
`dimension_key` is SHA-256 of those exact bytes. Store
`encoding.dimensions.model_dump(mode="json")` in the dimensions column.
Do not hand-build a competing hash or parse opaque `owner_key`.

`usage_subject_key` comes from `UsageIdentity.usage_subject_key`, never the
provider payer, routing owner, resource owner, or a string split of `owner_key`.
Daily encoding nulls session/conversation/activity/task/step references;
hourly/session/archive retain them for current resource authorization.
Coarse daily summaries cannot authorize cross-user project-team reads.

`record_kind="attempt"` contains provider outcome, token, valuation and
provider/dispatch/generation timing measures. `record_kind="request"` contains
logical request/server/client E2E measures. Request dimensions have null
provider/model/effort/source/streaming allocation when genuinely unallocated:
association is not inferred from the last attempt or a fabricated identifier.
Only an actual verified request-to-attempt binding permits
`request_allocation_provenance="verified_attempt"` plus real provider/model
metadata. The flag is an observation, not an authorization grant.

Metric families are additive **within their declared population**, not by blindly
summing both record kinds:

- Attempt outcomes use `finalized_count`; each timing sum has its own known-sample
  count (`ttft_ms_sum / ttft_count`, etc.).
- Logical completed/error counters come only from an authoritative **server**
  terminal outcome.
- Server E2E uses server sums/counts; client-reported E2E uses client sums/counts.
  A client-only report increments neither logical completed nor logical errors.
- Output tokens/sec uses the paired `generation_output_tokens` and
  `generation_output_ms` population, with milliseconds divided by 1,000.

`USAGE_METRIC_POPULATIONS` freezes those exact names and denominators.
`request_metric_values` validates scope and request correlation, without
cross-clock subtraction. `usage_milliseconds_to_seconds`, `usage_outcome_to_api`
and `usage_cost_basis_to_api` explicitly adapt storage values to API units/enums:
`success/error` → `succeeded/failed`, `configured_estimate` → `api_estimate`,
`api_equivalent` → `configured_equivalent`. Estimates are never invoices.

## One shared token-semantic owner

Import `normalize_usage_tokens` from `cognis.models.usage_normalization`.
It returns frozen `UsageTokenNormalization` with inclusive `input`, `output`,
`total`, cache/reasoning evidence, and disjoint `billable` units.

Raw provider facts stay unchanged in `NormalizedUsage`. Anthropic input=10 plus
cache-read=90 with `additional_to_input` becomes analytics input=100, while
billable uncached input stays 10 and cache-read stays 90. Included cache subsets
are not added again; additional reasoning is included once in analytics output,
while included reasoning is not added again. Cache-write TTL components never
also add their aggregate parent. Provider total is informational, not an
additional measure.

Each result retains reported/partial/unknown/not-applicable coverage and source.
Unknown is not zero; known partial subtotals cannot be reported as complete.
Native durations/images/characters do not become token totals.

## Schema and query paths

Migration `181_usage_query_contract` follows `180_usage_accounting`; bootstrap
uses the same idempotent owner in `cognis/store/usage/schema.py`.
Previous-180 fixtures exercise preserved rows, wider names and nullable
unallocated request dimensions on SQLite and PostgreSQL.

New indexes are intentionally limited to:

| Table | Index / leading keys |
| --- | --- |
| attempts | `ix_usage_attempt_time(started_at_observed, row_id)` |
| attempts | `ix_usage_next_expiry(finalized_at, row_id)`; partial projected-terminal predicate |
| attempts | `ix_usage_owner_day_row(owner_key, bucket_day, row_id)` for bounded repair |
| projection state | `ix_usage_projection_day(bucket_day, owner_key)` |
| daily totals | `ix_usage_daily_day(bucket_day, owner_key, generation)` |
| hourly totals | `ix_usage_hourly_day(bucket_day, bucket_hour, owner_key, generation)` |

No parameter-facet/GIN indexes are added. The isolated PostgreSQL EXPLAIN tests
use actual populated tables, `ANALYZE`, default planner settings, and
`EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON)` to verify the global time, trusted
retention-age, state and summary date paths.

The small canonical `UsageHistoricalPriceBindingRow` table retains explicit
backfill authority independently of disposable valuation receipts. It does not
copy events or enable a maintenance job. Downgrade refuses to truncate retained
long model names, discard unallocated request summaries, or delete nonempty
canonical backfill bindings.

## Runtime composition

`UsageAccountingService(session_factory, controller_owner_id=...,
projection_waker=..., limits=None)` has stable public
`controller_owner_id` and `coverage_epoch_id` values. The application will pass
them to capture's `UsageCapture(sink=service, ...)`. `start`, `drain` and `aclose`
remain asynchronous lifecycle operations; `try_emit` and
`try_emit_provider_sample` remain synchronous nonblocking observations.
This checkpoint does not duplicate capture transport/middleware implementation.
