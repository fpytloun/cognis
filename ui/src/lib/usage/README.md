# Usage frontend checkpoint

## Final live-adapter checkpoint

### Honest ranking refusal recovery

The real expansion fixture proves `mixed_cost_ranking` on provider/service cost
rankings (`tests/e2e/usage_checkpoint/test_expansion.py:101–106`). The backend
rejects mixed currency/basis before incomplete pricing; neither is a valid cost
ranking. Grouped trends are not ranked by the canonical QueryService.

The adapter recognizes only status-422 `mixed_cost_ranking` and
`incomplete_cost_ranking`. A primary refusal permits one shape-only trend retry,
with identical scope, protected/visible filters, precise interval, metric,
pricing basis, pending and forecast options. Costs, Performance and an explicitly
selected service use ungrouped recovery; all-service recovery keeps service
grouping so it cannot relabel overall traffic as a companion-only population.
An already-refused primary population does not issue another cost ranking.
Returned all-service groups are explicitly unranked observations, including null
allocation where supplied. No compatible band is combined or sorted by spend.

Optional ranking refusal preserves an already-successful trend and explains the
missing ranking without a third data request. Optional ordinary server failure
can omit that section, but authorization, invalid/expired/protected selectors,
scope mismatches and cancellation clear or abort the load rather than exposing
partial success. No metric/currency substitution, producer change, fanout or
filter-removing retry is introduced.

Frontend regression coverage lives in `ranking-refusal.test.ts`; it exercises
both real refusal codes across initial/secondary paths, identical request
populations, mixed bands, monetary/token nulls, null cohorts, empty results,
authorization/selector/scope fences and cancellation. The actual Python fixture
and its assertions remain untouched. The fixture owner's real QueryService
witness is source evidence, not a claim that this frontend check reran Python or
served the application.

Overview now loads one fresh catalogue, one `view=cost_breakdowns` operation and
one bounded ungrouped trend. The canonical total and all four fixed subject,
agent, provider and model marginal panels come exclusively from the breakdown
operation. Complete membership is validated; duplicate dimensions, duplicate
bands and incompatible marginal bands fail closed without a ranking fallback.
Shares reconcile exact decimals in the selected compatible band. Legends retain
the exact decimals. Scope-wide partial, not-fully-priced (including partial),
unallocatable and unknown-usage counts remain visible even without any money.
The selected band's fully/partially-priced counts are not summed across bands.

The timeline is an independently observed trend with its own labelled timestamp,
not an asserted copy of the four marginal charts' read snapshot. Its totals never
replace the marginal denominator or add selector bands. Scope/authorization
failure clears the active load; only an ordinary timeline server failure leaves
the successful canonical marginals visible with a missing-timeline explanation.
Controller generation, abort and authority invalidation remain the load owner.

Marginal drill predicates use authorized raw identities and catalogue-supported
detail filters (opaque subject keys are not emails). Missing/redacted identity
never acquires a drill; the backend continues to validate all protected scope and
retention bindings. Incoming marginal detail predicates intersect the existing
selection through the canonical `intersectFilters` helper; disjoint selections
show an explanation and dispatch nothing. Sequential provider → Overview → agent
drills retain both predicates and the original scope/precise interval.
A drill selects the cohort, not a fabricated price filter.

The narrow quota DTO handoff now maps `sample_id`, `limit`,
`maximum_utilization` and `first_observed_at`. The historical coalesced bucket
maximum is plotted separately from the nullable utilization field, with first/
last timestamps and sample count. It is never filled into utilization or labelled
latest/current. Reported historical limits and remaining values use separate
native-unit charts partitioned by account/window/reset/source; no configured
ceiling, conversion, interpolation or live provider refresh is inferred.

The phase notes below are historical checkpoint boundaries, not remaining raw
contract blockers. Final cumulative independent review follows the final wiring
commit. Composed-tree full checks, browser/screenshots and service qualification
remain parent-owned and are not established by these focused checks.

## Charts-first expansion: phase A (historical)

`OverviewCharts.svelte` leads Overview with an ungrouped known-priced-subtotal
trend and four simultaneous User, Agent, Provider and Model attribution panels.
One local currency/basis selection controls all five charts without a fetch.
Lifecycle diagnostics and exact chart tables are behind native disclosures.

`composition.ts` is a pure internal view-model owner, not a wire contract.
It reconciles complete marginal groups to their band denominator with decimal
arithmetic before producing approximate geometry. Partial pricing does not
invalidate a known-subtotal composition. Null amounts remain unavailable,
real zeroes remain visible, and signed or unreconciled amounts cannot form a pie.
Other is a decimal sum of complete returned groups only; all original rows remain
in the accessible table. Missing/redacted identity never acquires a drill filter.

Phase A deliberately does not call `view="cost_breakdowns"` or change raw DTOs.
The production adapter does not yet populate `UsageView.costBreakdowns`; its
panels explicitly show unavailable rather than substitute a ranking or fixture.
`composition.test-data.ts` is synthetic internal-model test input only.

### Phase B handoff

- Consume the backend owner's frozen raw DTOs. Map its four fixed marginal
  dimensions and compatible bands into the internal model; preserve decimal
  amounts and overlapping partial/unpriced/unallocatable counts.
- Supply `complete` only for certified complete group membership. Preserve
  redacted labels and supply a cohort `drill` only from a supported authorized
  identity/filter or protected selection. Never derive it from display text.
- Use one fresh catalogue and the approved single-operation/generation
  `cost_breakdowns` result; preserve the same scope, filters and exact interval
  for its trend companion. Controller cancellation and stale-response rejection
  continue to own the complete active load.
- Finish the remaining chart-led views and final diagnostics/content pass.
  Requalify request bounds, revocation and pricing through the real API.
- Obtain fresh integrated desktop/mobile light/dark/partial/unknown captures
  under parent-admitted fixtures, then one independent review of the final
  workstream. Phase-A mounted tests are not browser or API acceptance.

Phase-A validation uses task-local lockfile-installed dependencies, the normal
Make lock and a scoped configuration extending the real generated SvelteKit
config. No full Svelte check, app/viewer build, service or browser fixture is
part of this checkpoint. The historical full-check OOM and broader qualification
gaps below are not cleared by focused evidence. Historical commands below do not
authorize increasing current resource limits.

## Supported secondary-view chart increment (historical)

`view-charts.ts` owns presentation-only comparisons of the existing supported
responses. Provider/service means use their own sum/sample populations;
throughput is eligible output divided by eligible duration, never mean rates.
Failure rates require the explicit known-dispatched denominator. Unknown clocks,
zero denominators and absent metrics do not acquire geometry. Companion coverage
counts can overlap and are not a complete/incomplete pie.

Costs leads with the separate-band monetary trend, input/output trend and a
known input/output composition. Cache and reasoning subsets are never added to
that composition; unknown categories retain values without a pie. Small monetary
trends scale to their observed numeric range, with sub-cent tick labels.

The adapter retains existing request budgets and maps performance from the same
returned ranking metrics. A selected-service trend survives only an explicit
incomplete-cost-ranking refusal; unauthorized rankings still clear the whole
view. No substitute metric, broader population or per-service fetch is added.
Unallocated cohorts remain visible but never become broad service selectors.

Explorer charts are visible-cursor-page observations, not full-window totals or
deduplicated logical outcomes. Cumulative-index and retained-detail session rows
remain separate comparisons. Chart categories cannot broaden a query; protected
row-specific actions remain in the disclosed exact-data tables. Comparison plots
show at most 12 returned rows per measure, explicitly labelled when limited; the
accessible comparison table retains all returned rows without further fetching.

### Historical Limits observations and the remaining contract gap

The prior ceiling-only blocker was too broad: stored quota observations do not
require configured ceilings to be useful. `quota-charts.ts` now builds separate
historical percent-utilization and provider-native-remaining scatter plots.
Account generation, window, reset timestamp and source partition the cohorts;
missing identities do not establish compatibility. Nulls remain gaps, original
decimal strings remain authoritative and no lines connect sparse observations.
Native remaining units are not converted into percent or a configured ceiling.
The latest returned observation timestamp is explicit, not a claim of current
provider status. Choosing an observation is local component state, not a selector
URL, analytics request, provider refresh or request-list navigation.

Canonical evidence: `providers/usage_api/base.py` names normalized
`OAuthUsageWindow.used_percent`; `core/usage/quota.py:120-129` captures it into
`utilization`. `core/usage/query_models.py:619-637` exposes both a nullable
`utilization` field and `maximum_utilization`. The actual producer
`core/usage/query_quota.py:57-83` populates **maximum_utilization**, leaving the
frozen UI DTO's `utilization` null. This is a narrower, demonstrated contract
gap, not a reason to hide known remaining observations. The frontend does not
guess the missing raw field or derive a percent from remaining. Root/backend
must reconcile that public DTO before real maximum-utilization samples can be
plotted; no raw contract or normalization code is changed in this increment.

Only explicitly selected-provider history in **My usage** can be requested by
this UI. Admin/project history dispatch and mismatched-provider fallbacks are
refused before transport; the personal-scope route check requires provider ownership.
No all-provider defaults, fanout, capture, polling or extra endpoint is added.

The monetary headline already preserves `known_amount` independently of ranking
quality. `core/usage/query_populations.py:118-121` emits a known subtotal when
priced **or partial** components exist. The adapter maps that amount unchanged
and focused coverage proves a partial-valued trend remains populated without a
ranking request. The four marginal Overview charts still await their new raw
contract.

The partial-cost regression also exposed rounded-only chart data: cost-series
labels now retain the exact decimal amount and currency rather than only a
two-decimal currency formatter. Approximate geometry is unchanged.

Focused mounted/pure/adapter tests and usage-scoped Svelte diagnostics cover this
increment. Full-framework/build, rendered/API-backed acceptance, final DTO wiring
and one final independent review remain parent-gated; old captures do not qualify
these layouts. Parent owns regeneration of the combined changelog index.

Focused validation through ranking refusal recovery (160 tests, thirteen files; two workers):

```sh
make test-ui VITEST_ARGS='src/lib/usage/ranking-refusal.test.ts src/lib/usage/overview-api.test.ts src/lib/usage/quota-charts.test.ts src/lib/usage/SecondaryCharts.test.ts src/lib/usage/view-charts.test.ts src/lib/usage/adapter.test.ts src/lib/usage/UsageDashboard.test.ts src/lib/usage/UsageWorkspace.test.ts src/lib/usage/chart-state.test.ts src/lib/usage/performance.test.ts src/lib/usage/composition.test.ts src/lib/usage/CostComposition.test.ts src/lib/usage/decimal.test.ts --maxWorkers=2'
make validate COMMAND="bash -c 'cd ui && ./node_modules/.bin/svelte-check --tsconfig .svelte-kit/tsconfig.usage-secondary.json'"
```

The ignored scoped config extends `ui/tsconfig.json` and includes all
`src/lib/usage/**/*.ts` and `*.svelte` owners plus the normal generated ambient/env
and application declarations. It is not a whole-application check or a type stub.

## Public interface provenance

`wire.ts` and `query-contract.ts` consume the public contract frozen in
`ui/e2e/usage/contracts/`, not the mutable API implementation:

- `current.json` pins the current schema, documentation, and provenance hashes.
  Its source-commit-named directory is generated from the actual committed
  Pydantic models, including cost breakdowns and partial/unallocatable counts.
- The original top-level historical export is unchanged: JSON SHA-256
  `ebdc69a21ade97f5fbef04b3130b62c85988c21b2398249a0bc13d2b4de08be4`,
  documentation SHA-256
  `0a278f28368e9e36853f4afa3249b02dd3dfd101d08fd6275db2cc679a0fe9a4`.
  The validator selects it only with `--contract historical`.
- See `ui/e2e/usage/contracts/README.md` for deterministic regeneration and source
  provenance. The generator owns current hash pins; consumers do not hand-copy
  schema definitions or relax validation.

These exact public documents are interface evidence only. They do not establish
an approved immutable proxy commit, installed app dependency, or shipping backend.
No API implementation is copied, imported, executed, or changed by these tests.

## Owners and invariants

- The adapter obtains a fresh scope catalogue for each active bounded view and
  checks response scope. It preserves every meaningful filter, or refuses the
  query. It never retries after clearing a filter, selection, or cursor.
- Nested attempts use `POST /api/v1/usage/attempts/search`. Session and request
  pages use their distinct POST DTOs, cursors, and protected selection refs.
  Request pagination is explicitly attempt-indexed; duplicate logical IDs and
  unknown logical outcomes are not presented as deduplicated request totals.
- Session source is explicit Cognis/proxy metadata. Only verified Cognis IDs can
  populate `session_id`. Client proxy labels are unverified metadata; proxy
  summaries are labelled retained-detail totals with returned retention bounds.
- Session summaries require complete UTC days. An exact selected hour is
  preserved and refused for an unrepresentable summary, never widened silently.
  Exact-window request and physical-attempt drill remains available.
- `query-state.ts` owns URLs, breadcrumbs, and typed client history. Error
  categories and logical-request IDs round-trip as real filters. Opaque refs,
  opaque cursors, and private proxy search remain in request bodies and
  authority/query-bound history, never URL text or a shared cache. Missing or
  expired state requires explicit reselection/first-page/reset, not a broad fetch.
  Client history is not an authorization grant; the server revalidates every use.
- Returned `bucket.groups` supplies grouped series without per-group fanout.
  Missing groups and null measurements stay gaps; null group identities cannot
  become a broader drill. Every drill keeps exact interval and parent filters.
- Decimal strings, currency, historical/API-equivalent basis, known subtotals,
  nulls, capture/projection coverage, and server/provider/client provenance remain
  distinct. Numeric chart geometry is approximate, not accounting arithmetic.
  No missing price or client receipt is inferred.
- `UsageController` retains ownership of active reads, cancellation, stale
  response rejection, visibility, and coalesced event invalidation. There is no
  polling, hidden scan, per-row request, quota refresh, or transcript fetch.
  Sidebar row/observer authority and managed/private restrictions are unchanged.

## Validation and fixture acceptance

Use the shared locked Make targets with the existing pinned environment:

```sh
NODE_OPTIONS=--max-old-space-size=3072 make test-ui VITEST_ARGS='src/lib/usage/ --maxWorkers=2'
NODE_OPTIONS=--max-old-space-size=3072 make check-ui
NODE_OPTIONS=--max-old-space-size=3072 make build-standalone
NODE_OPTIONS=--max-old-space-size=3072 make build-ui
```

`contract.test.ts` validates synthetic requests/responses using the exact frozen
JSON schemas through the existing Python `jsonschema` installation. The validator
imports no Cognis application or API implementation. The opt-in loopback browser
harness is fixture-backed, not an API-backed screen or deployed Preview app.
Inspect every capture before claiming browser/visual acceptance.

## Integration and acceptance still required

- Approved immutable proxy commit and actual app dependency from the parent.
- Real API-backed global/personal/project flows, screenshots, pricing and
  authority/revocation acceptance. Global index184 is
  `0a25dd52501a4f58b67c0844103c507ca0c533e7`; missing-index503 is only a
  compatibility/failure fixture, not the intended admin experience.
- Actual app/query runtime `0203ae0f6` (parent `82312cba8`) and true Capture
  price injection. The parent owns backend/storage/app composition and changelog.
- Mermaid extraction review was accepted. Ordinary measured critical JS was
  **480,664 B**, against the unchanged **481,280 B** gate: **616 B headroom**,
  **324 B below** accepted ordinary upstream **480,988 B**. The **1 KiB margin
  target was not achieved**; the architect explicitly accepted 616 B. No further
  size optimization is authorized. Rerun the unchanged ordinary gate after this
  final wiring; diagnostic identical-seed measurements are not ordinary evidence.
- Preserve the five rich failures reproduced on untouched `c696dbaa`: two
  dashboard fullscreen/title expectations, action-menu sizing, and two viewer
  expectations. They are residual failures, not passing checks or weakened tests.
