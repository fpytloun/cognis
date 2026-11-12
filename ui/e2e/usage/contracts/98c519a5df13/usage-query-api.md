# Usage query API

Updated for asynchronous capture, parameter/timing provenance and retention.
This supersedes the initial durable-registration wording. No compatibility
alias preserves `prepared`, `prepared_at`, `registered_attempts` or
`meta.projection_state`: use `observed_start`, `started_at`,
`observed_attempts` and `meta.projection.state` respectively.

Canonical exports: `cognis.core.usage.query_models`. These controller-only query
contracts do not duplicate the storage-owned common observation model.

The additive lifetime conversation/direct-execution and known priced subtotal
contract is specified in [Conversation accounting and cost marginals](usage-conversation-breakdowns.md).

## Frontend handoff

`UsageFilters.conversation_tree` is nullable or
`{id: Identifier, inclusion: "direct" | "inclusive"}`. It is supported by
`UsageQuery` views `overview`, `trend` and `ranking` only. Membership is resolved
with the sidebar's bounded materialized membership and invalid-lineage proof,
under a single read snapshot; resource authorization still intersects every
selected session. Other filters intersect the population. Invalid membership
returns unavailable projection metadata, null total and empty groups/buckets.
Hourly/day query windows and retention remain unchanged; a lifetime sidebar
total reconciles only with a query window covering the same retained history.

Session group `label` now resolves to a current authorized conversation title,
with ` · <agent name>` for same-conversation delegates when the name is readable.
`UsageSessionItem.session_label` and `UsageRequestItem.session_label` (including
logical request log/detail models) expose the same nullable string. Unreadable,
missing or deleted titles are null even for accounting admins. Proxy labels are
not promoted to verified Cognis titles. Agent group labels use owned agent names.

Generate exact JSON Schema from this checkout:

```sh
PYTHONPATH=.:packages/common/src:packages/executor/src make validate \
  COMMAND='uv run python scripts/export_usage_query_contract.py --output .local/usage-query-contract.json'
```

Request schemas use validation mode; response schemas use serialization mode,
including derived means, rates and safe error summaries. Raw DTOs live in
`$lib/usage/wire`; summary aliases are exported from `$lib/types/api`.
UI workers own adapters, selection and components.

| HTTP operation | Request export | Response export |
|---|---|---|
| `POST /api/v1/usage/query` | `UsageQuery` | `UsageQueryResponse` |
| `GET /api/v1/usage/attempts` | `UsageAttemptQuery` (query parameters) | `UsageAttemptPage` |
| `POST /api/v1/usage/attempts/search` | `UsageAttemptQuery` | `UsageAttemptPage` |
| `POST /api/v1/usage/requests` | `UsageAttemptQuery` | `UsageRequestPage` |
| `POST /api/v1/usage/sessions` | `UsageSessionQuery` | `UsageSessionPage` |
| `POST /api/v1/usage/catalog` | `UsageCatalogRequest` | `UsageCatalog` |
| `POST /api/v1/usage/session-summaries` | `UsageSummaryRequest` | `UsageSummaryResponse` |
| `GET /api/v1/usage/providers/{provider_id}/history` | `ProviderQuotaHistoryQuery` plus provider path ID | `ProviderQuotaHistory` |
| `POST /api/v1/usage/facets` | `UsageFacetRequest` | `UsageFacetResponse` |

The application service and router implement these operations. Application
mounting and service lifecycle remain owned by the composition root.

`query_usage` consumes `UsageToolArguments` through one canonical schema; it cannot
receive actor identity or trusted invocation authority from arguments. The
controller handler and REST adapter must call the same service.

## Composition

Attempt `parameters` additionally exposes optional
`service_tier_applicability: {version: 1, state, scope}`. The three states are
`unknown`, `applicable` and `not_applicable`; missing/null means legacy unknown.
Only `not_applicable` carries the exact versioned resource/ABI scope. It does not
change the nullable requested/effective tier parameter, supply a wire tier or
assert complete financial coverage. The capture and configured-tariff contract
is documented in `docs/development/usage-physical-capture.md`.

This is an additive strict-payload extension, not a SQL schema change. Upgrade
controller/common/executor together before emitting it; old strict readers do
not gain support through a top-level usage-version alias. Existing pinned UI
qualification fixtures remain evidence for their recorded baseline, not a fresh
export of this extension.

Construct `await create_usage_query_service(session_factory, cursor_secret=secret)`
from `cognis.core.usage.query_service` once at application startup. Schema/index
discovery happens before serving requests. Use the same stable, private encryption
secret across controller replicas if cursors must survive routing or restarts.
Omitting it generates a process-local secret.

Mount `create_usage_router(service)` from `cognis.api.routes.usage` beneath
`/api/v1`. Pass this same service as `usage_query_service` to the runtime factory;
tool handlers derive authority from `RuntimeAccessContext`, not call arguments.
The read service has no background task or shutdown action and never constructs
an inference client. The immutable dimension encoder is built in; no population
configuration or JSON-key adapter is required.

This slice supplies the construction interface, not an `app.py` change.
The parent composition owner must mount the router and inject the shared service
before enabling the feature in a deployed application.
For stable paging across replicas and restarts, inject the same durable
`cursor_secret` into each service instance. Omitting it deliberately creates
process-local cursors; no secret value is returned or logged.

`UsageRequestPage.pagination_unit` is `attempt`. Its indexed physical rows remain
visible when optional logical statistics are absent. `started_at` is the physical
observation time; `logical_request_started_at`, logical outcome and logical timing
are nullable observations, never inferred from the final physical retry.
Several rows may share a request ID. Preserve `drill_filters` and pass the row's
`selection_ref` when fetching the bounded physical trail. Restricted team rows
return null request/attempt identifiers; the encrypted ref is their drill handle.

### Session explorer and protected detail selection

`UsageSessionQuery.session_type` explicitly selects `cognis` (default) or `proxy`;
the source is not inferred from an identifier. Both return `UsageSessionPage`.

| Item field | Cognis | Proxy |
|---|---|---|
| `session_type` | `cognis` | `proxy` |
| `session_id` | Verified Cognis ID | Always null |
| `totals_scope` | `cumulative` | `retained_detail` |
| `selection_ref` | Opaque detail selection | Opaque namespace-safe detail selection |
| `client_id`, `client_session_id` | Null | Private-scope trusted client/null and unverified label; redacted in team mode |
| `retained_start`, `retained_end` | Not populated | First/last actually retained matching start observations (inclusive edges, not a contiguous-coverage claim) |

Proxy namespaces are `(actual consumer, trusted client ID or explicit null,
unverified client session label)`. Verified Cognis session bindings win.
Trusted `intaris`/`mnemory` labels alone never create explorer sessions.
Labels and their hashes remain absent from canonical hourly/daily/archive dimensions.
Proxy list metrics use ready active session summaries and are retained-detail
totals, never lifetime spend. Projection/capture limitations remain in `meta`.

Session windows require complete UTC days and historical summary pricing.
Proxy pages cap `limit` at 100 (Cognis/detail retain their existing limits).
`search` is literal exact Cognis session ID or exact proxy label, not wildcard,
substring, transcript search or a new authority selector. Proxy-label search is
rejected in project-team mode to prevent identifier probing. Keyset pages are live
authorized reads, not a frozen accounting snapshot; preserve all query arguments
with `next_cursor`. A page may become empty after retention or visibility changes.

Pass an item's `selection_ref` to `UsageAttemptQuery.selection_ref` on
`POST /usage/requests` or `POST /usage/attempts/search`. Request rows provide
another ref for their indexed physical retry trail. Preserve the scope, time
window and filters; further narrowing is allowed, widening the original selection
window is rejected. Ref predicates intersect, never replace, fresh consumer and
current resource authorization. Proxy detail uses `ix_usage_client_session_time`
with explicit `IS NULL` client semantics; request trails use `ix_usage_request_attempt`.
Optional logical request statistics are left-joined and never gate physical rows.

Refs retain the original physical-row and registration cutoff through subsequent
drills. They cannot select later observations when a label is reused after detail
expiry, including a backdated start time or a recycled SQLite row ID. Policy age
alone does not hide genuinely retained pending/late observations. Empty expired
selections do not imply zero lifetime usage.

All cursors and refs are authenticated encrypted `v2.` tokens (AES-GCM), expire
after 3,600 seconds, and bind to actor/scope/trusted invocation and operation or
query. Do not decode them, derive resource IDs from them, or log them. Tokens from
the old signed-plaintext format are rejected; restart pagination. Refs never
grant transcript/resource/credential access. Catalog `selections` publishes the
typed `UsageSelectionContract`; the private payload is not a UI contract.

### Existing analytical drill contracts

Use `UsageQuery(view="trend", bucket="hour" | "day", group_by=...)` for grouped
timeseries: `buckets[].groups[]` contains each bucket's dimension population,
while top-level `groups[]` spans the selected window. Missing buckets are not
filled with invented zeroes. Catalog compatibility/retention restrictions apply.
`filters.error_categories` and `filters.states` select supported error/outcome
populations. `filters.logical_request_ids` is the existing bounded request-ID
filter for physical drill, not an additional chart API or a global lookup grant.
Team rows use encrypted refs instead of disclosing another consumer's IDs.

### Resource expense rankings

`UsageQuery(view="ranking", metric="cost")` accepts `group_by="task"`,
`"schedule"` and `"conversation"`. Task grouping includes all attributed tasks;
`filters.task_origin="one_off"` selects non-scheduled tasks and `"scheduled"`
selects tasks linked through schedule fires. `filters.schedule_ids` narrows to
unambiguous schedule-fire links. Schedule grouping excludes unlinked usage and
does not multiply a task's costs when multiple fires reference it.

`groups[].key` remains the stable resource ID. `groups[].label` contains an
authorized current task title, schedule name, conversation title or owned agent
name, or null when unavailable. Usage-admin authority does not grant title access.
Schedule names follow schedule owner-only access.

Ranking responses declare `ranking_measure`: `"known_priced_subtotal"` for cost,
otherwise `"attempts"`, `"input"` or `"output"`; non-ranking responses return null.
Partial pricing does not reject cost ranking. Compare known decimal subtotals,
sort unknown-cost groups last, and retain each group's unpriced/partially-priced
counts plus cost-band priced counts/quality. Incompatible currency/basis bands
still return `mixed_cost_ranking`; a known subtotal never claims complete spend.

Resource rankings use retained summaries and support day windows up to 730 days
(hour windows up to 90 days). Retention is disabled by default. If enabled
retention has expired part of the requested resource window, return HTTP 422,
code `resource_detail_expired`, message
`Task, schedule and conversation breakdowns are not available before <cutoff>`.
The cutoff is resolved only from currently authorized data. Do not return a
silently incomplete retained fraction or reconstruct archived resource rankings.

## Semantics

- Costs are exact nonnegative decimal strings, grouped by currency and
  `api_estimate`/`configured_equivalent`. No cash/invoice claim.
- Tokens are JSON-safe integers/null. Subtotals represent known observations;
  `token_known_attempts` exposes per-field coverage.
- `finalized_attempts` counts projected terminal contributions. `observed_attempts`,
  pending states and `pending_complete` remain null unless a bounded,
  authorized pending/outbox read establishes them. Observed starts are not proof
  of dispatch, durable admission or the complete number of executed attempts.
  A final-only observation remains countable if its start was dropped.
- Summaries use immutable trusted attempt-start UTC bucket attribution. A
  finalized unknown outcome is distinct from a still-pending observed start.
- Capture is asynchronous best-effort. Inference never waits for a DB write or
  queue capacity. The background writer commits raw facts and projection-outbox
  intent only; pricing and projections run separately. A bounded queue/hard crash
  may lose observations. `meta.capture` and each bucket's `capture` expose
  observed/gap/unknown coverage separately from `projection` readiness/lag.
  No detected loss is not a crash-proof completeness guarantee.
- Coverage-loss counts may be populated only when attributable to the authorized
  scope. Global drop counters must not leak other consumers through project or
  personal responses; unavailable scoped counts stay null.
- Performance means use sums/counts; generation and end-to-end throughput use
  their own matched token/time populations. Zero duration produces no rate.
  `end_to_end` in these provider-attempt fields means provider dispatch-to-finish,
  not client receipt. Nonstream TTFT is null unless measured by the provider.
  Keepalives are not generation. `provider_timing_source` identifies the local
  measurement clock without crossing host monotonic domains.
- Attempt error-rate denominator is known dispatched attempts, excluding
  observed-start-only unknowns. Logical failure rate is null without trusted logical
  completion evidence. Failed retries do not establish logical failure.
- Attempt `error_summary` is generated from fixed category templates. No input
  field accepts provider messages/bodies. Class/code fields are bounded codes,
  populated only from the capture owner's approved structured observations.
- Project rows use `UsageAttemptDetail`. Own-personal/admin rows may use
  `UsagePrivateAttemptDetail`, with safe key/client/provider correlation fields.
  Ordinary transcript/credential authorization is unchanged.
- No response cache is introduced. `authorize_usage` reads current user,
  role, project membership and applicable agent grant/assignment on every use.
  Its result is request-local, not a grant to skip resource predicates.
- Project SQL must require current project-visible canonical resource bindings,
  even for resource authors, and independently exclude private/managed child
  resources. A bare project/client header is not a resource binding.

## Forecasts

`forecast_usage` is pure arithmetic over buckets already read for the view.
It requires 24 closed contiguous hourly or seven daily buckets, at least three
nonempty buckets, ready projections, observed capture coverage without known
gaps, and certified observed-pending coverage for every bucket.
Capture gaps/unknowns and projection lag have distinct unavailable reasons.
Missing/unverified buckets are not zero. Late observations remain a limitation.
Archived daily accounting can support daily forecasts, never fabricated hourly
detail. `source_grain` must establish usable fidelity.

Rate is observed sample total divided by full sample seconds; prediction is rate
times horizon seconds (at most 30 days). Tokens round half-up to whole tokens.
Cost forecasts multiply exact sample totals before dividing and round the final
estimate half-up to 28 significant decimal digits; historical cost strings are
never rounded or repriced by forecast math.
Each token field has independent availability; missing pricing does not suppress
known token predictions. Cost requires one completely priced currency/basis.
Sample window, horizon and limitations are explicit. There is no confidence
claim, upstream sampling, forecast SQL, inference, timer or background job.
These are observed-rate forecasts, not proof of complete billing under lossy
asynchronous capture.

## Parameters and provider-to-client timing

`UsageRequestParameters` contains separate requested/effective
`UsageParameterSnapshot` envelopes. Every allowlisted setting has a typed nullable
value and explicit/configured-default/provider-default/adapter-normalized/
unsupported/unknown provenance. Unknown values never acquire invented defaults.
Safe response-format/tool-choice modes contain no schema, tool name/definition,
stop string, arbitrary request JSON or message content. Parameters are retained
detail only, not forever archive dimensions or arbitrary aggregate filters.

Provider-attempt fields retain their existing meaning. New
`UsageLogicalTiming.server` records logical controller-boundary elapsed and
first/last-send durations plus at most 64 typed local span offsets.
Routing, queue, provider/executor wait, accounting-hook, finalization and
delivery spans stay distinct. A server send is never client receipt.

`cognis_overhead_seconds` is available only from a complete, bounded,
non-overlapping controller-clock partition. It sums explicitly classified Cognis
spans instead of subtracting independently measured provider totals. An unresolved
executor interval may contain remote Cognis work, so total overhead remains
unavailable rather than guessing that breakdown.

`UsageLogicalTiming.client` is absent without authenticated correlated
instrumentation. It contains client-local first/last-chunk elapsed observations
and a source (`cognis_ui` or `authenticated_client_report`), never a difference
between host clocks. Values are bounded to 24 hours. The ingestion owner must
verify current authenticated ownership, correlation and timing bounds before
constructing this response; setting `correlation_verified` in a request cannot
grant that authority. No client timing ingestion endpoint is implemented here.

`logical_performance` reads fixed request-population sums/counts. Each logical
observation contributes once, never once per retry. Provider/model grouping
requires the encoder's explicit verified allocation; otherwise its key remains
null. Logical failure rates require authoritative captured server completions.
The current summary client cohort does not distinguish authenticated reports
from unverified reports, so authenticated-client aggregates remain unavailable.
Retained authenticated, correlated client detail remains independently usable.

## Retention and archive availability

`UsageAvailability` is present in response metadata and each sidebar item:
`retained`, `aggregate_only`, `detail_expired`, `mixed` or `unavailable`.
Expired/mixed results require an explicit `detail_retention_cutoff` and archive
grain. A detail-expired page has no attempts or detail cursor, not a fabricated
zero total. Unknown/unauthorized selectors remain indistinguishably unavailable.
The cutoff is policy metadata, not an existence test: retained pending or
late-finalized facts may have older start times. Only the authorized source can
establish a window's retained/mixed/aggregate-only resolution.

Configured detail age is reported only when known. Thirty days is a proposed
default, not live configuration or deletion approval. The separate proposed
seven-day payload audit is outside this implementation and introduces no logging.

Canonical UTC-day resource/session accounting survives detail deletion. Details
such as parameters, request/cycle IDs and client correlation do not live forever
in summaries. Archived queries require UTC-day-aligned windows, explicitly
supported dimensions and compatible billing strata. `check_query_availability`
rejects unsupported historic detail/filters/current repricing with actionable
codes; it never initiates a raw scan. Stored booked valuations are preserved.
New per-call pricing rules unsupported by retained units remain unavailable.

Fresh personal/project/admin/agent authority precedes every result, including
expired-detail and aggregate-only responses. Archive metadata is not a permission
grant. Current private/managed-resource exclusions and project moves/revocations
still apply to archived resource totals.

Storage owns the accepted archive/replay protocol: disjoint archived baseline
plus all retained facts (including unprojected observations), atomically fenced
application versions, exact eligible terminal projected-ID archive/delete in one
transaction without a second cumulative increment, and same-scope rebuild
exclusion. Trusted immutable attempt starts prevent expired replay resurrection;
expired missing finalizations/corrections are rejected in v1. This checkpoint
implements no archive mutation, retention job, purge or late-adjustment path.

## Bounds and unsupported sources

At most 100 filter values/selector items, 500 detail rows and 1,000 analytical
groups/buckets. Hourly requests span at most 90 days; daily at most 730 days.
Catalog compatibility, current resource joins, encrypted keyset cursors and
statement budgets are enforced by the service. PostgreSQL statements use a
1,000 ms timeout. Global queries fail closed when the qualified index is absent.
The catalog distinguishes missing reasoning-known/error-dispatch populations,
current-summary pricing qualification, and observed-pending qualification from known zero.
Retained current-price detail is independently supported: its valuation must match
the active owner/day and fact versions and the provider/model pricing generation
and revision. Currency and basis come from the selected immutable price revision.
Stale or missing valuations remain unknown without recalculation.

Projection metadata includes currently authorized captured-only owner/days even
when no active summary row exists. These indexed existence checks do not aggregate
raw facts. Unready contributions degrade the response rather than disappearing
behind another ready day.

Proxy explorer sessions consume the storage-owned 182 extension: session/day
kind and nullable Cognis session ID, plus retained correlation of authenticated
subject, trusted client ID (including explicit null), and client session label.
These disposable labels are not canonical hourly/daily/archive dimensions.
Verified Cognis binding takes precedence; companion-only labels cannot create
proxy sessions. This API does not fabricate Cognis resource authority from them.

Broad terminal analytics must use active compatible summary generations.
Observed counts add separately bounded authorized pending/outbox reads. No missing
summary can imply zero pending, and no foreground raw-history scan or rebuild
may substitute for unavailable projections. Expected normal response budget:
at most eight SQL statements including authorization and membership resolution.
