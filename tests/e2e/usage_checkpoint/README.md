# Seeded real-app usage checkpoint

This qualification uses the existing `e2e_stack` fixture and Playwright login
helper. Cognis runs its ordinary factory, successful lifespan, authentication,
middleware, usage router and SQL read service. There is no alternate app,
authentication override, usage-response mock or provider inference.

The ledger is synthetic. `seed.py` uses the canonical writer/projector and
publishes a declared fixture tariff: priority input USD 2/million and output
USD 4/million, configured-estimate provenance. Fresh-session SQL assertions
require a USD 0.002400000000 historical valuation for each explicitly priced
attempt. Automatic tier and absent-price facts must remain unpriced. This is
not evidence of Capture producing these canonical fields from provider traffic.

## Run

These commands describe qualification, not permission to start it. Obtain
separate parent approval for dependency preparation, build and runtime admission.
Use a clean task-owned checkout containing the intended immutable application
revision and its own physical dependencies. Before any server starts, build through the test-only
provenance wrapper under the existing shared lock:

```sh
make validate VALIDATION_LOCK_TIMEOUT=30 COMMAND='uv run --no-sync python -m tests.e2e.usage_checkpoint.build'
```

This runs the unchanged `npm run build` command (including its normal budget
gate/precompression) and writes `ui/build/.usage-checkpoint-build.json` only after
success, with the build-time commit/tree, committed input hashes and every output
hash. Startup rejects missing/stale-source/modified-output manifests. Do not
retroactively stamp a previous build. An unchanged compiled UI can be reused
after test-only or backend commits because all compiled input hashes must still
match; the full-app source revision is separately attested at runtime. Unit HTML
in `test_usage_browser_build.py` tests only manifest validation and is never
served or reported as app/auth evidence. Never build while the server is running.

Create a new evidence directory for each run. Set these variables in a sanitized
environment (the qualification fails closed on inherited database/provider
credentials):

- `COGNIS_USAGE_CHECKPOINT=1`
- `COGNIS_USAGE_RUN_DIR`: unique task-owned absolute evidence directory
- `COGNIS_USAGE_BASE_REVISION`: full immutable application checkpoint SHA
- `COGNIS_E2E_CONTROLLER_PROJECT`: this checkout's absolute root
- `COGNIS_SERVE_UI=true`
- `UV_NO_SYNC=1`, `UV_OFFLINE=1`
- `UV_PROJECT_ENVIRONMENT`: the approved task-owned Python 3.12 environment
- `UV_CACHE_DIR`: the freshly qualified self-contained offline cache, including companions
- `UV_TOOL_DIR`: the fresh normally installed companion tool root, never a copied venv
- `HOME`: a new directory inside the evidence directory, not the operator's home
- `PYTHONPATH`: absolute `tests/e2e/usage_checkpoint`, repository root,
  `packages/common/src`, and `packages/executor/src`, in that order
- `PATH`: approved, version-attested Node, uv/uvx and system binaries
- `PLAYWRIGHT_BROWSERS_PATH`: existing Chromium installation

Do not set `DATABASE_URL`, `COGNIS_E2E_TEST_DATABASE_URL`,
`COGNIS_E2E_INTARIS_POLICY_MODEL`, production API keys or cloud credentials.
The original fixture owns temporary database/configuration/keys/ports and all
service start/stop. The opt-in `sitecustomize.py` fences Python subprocess
connections to loopback and attests actual controller module/build/DB roots.

Usage also requires the separately launch-bound [declared fixture inputs](INPUTS.md).
The admitted namespace uses `/work/data/new-cache` and `/work/data/tools`; per-service
HOMEs are fresh scratch, not the bootstrap HOME. Prepare and bind the canonical
spec before launch; pass its path/hash explicitly. Do not calculate a replacement
trusted hash inside a worker to accept whatever spec happens to be present.

Run serially through the repository lock (only after separate runtime admission):

```sh
make validate VALIDATION_LOCK_TIMEOUT=30 COMMAND='uv run pytest tests/e2e/test_usage_live_browser_acceptance.py --usage-input-spec "$COGNIS_USAGE_INPUT_SPEC" --usage-input-sha256 "$COGNIS_USAGE_INPUT_SHA256" -n 0 -v --tb=short'
```

Pass a new `--basetemp` inside the evidence directory and retain stdout/stderr
there. Never reuse an old pytest basetemp: pytest can clear it. Missing cached
dependencies are an environment blocker, not permission to install, contact a
provider, bypass the lock or reuse a running application.

## Evidence and limits

The bridge verifies module paths and byte-identical locally built HTML, seeds
only its existing temporary database, checks real authenticated scope endpoints,
and runs `ui/e2e/usage-live-qualification.spec.ts` with no retries.

The driver exercises global/personal/project views, grouped daily chart drills,
Cognis/proxy sessions, logical-request/physical-attempt boundaries, error filters,
Back/reload, missing history, cryptographically malformed selections and fresh
SQL membership revocation. Captures wait for loaded content and check document
overflow/floating-chrome overlap. Inspect every PNG manually before acceptance.

Malformed selections and missing browser history are **not actual expiry**.
The real unmodified server has a 3600-second TTL; this short run does not change
its clock or token codec and does not establish elapsed-TTL expiry. Chromium
mobile emulation is not a physical-device or WebKit qualification. Final all-path
Capture and any later parent revision require their own qualification.

The existing real fixture creates and activates its test agent before yielding.
Both operations await assistant-identity memory synchronization, which can submit
loopback Mnemory extraction calls. The bridge saves the complete mock history
immediately after that barrier, after seeded API preflights and after the browser.
All three histories must be identical: the analytics phase has zero inference
delta, not a falsely empty whole-stack history. No entries are reset or filtered;
a saturated bounded history is inconclusive and fails the gate.

Capture geometry retains raw and painted line boxes. Painted obstacles are
intersected with the viewport and ancestor scrollport clipping; fixed chrome must
still cover none of them, including visible message inputs. Dedicated browser
fixtures prove that actual painted overlap fails while clipped/offscreen boxes
do not. Per-test packets and geometry are saved even when a capture fails.

Retain source provenance, seed/price manifest, API packets, browser log, captures,
service logs, full phased inference histories and teardown checks. Do not publish the pytest
data tree or `runtime.json`: they contain disposable fixture credentials/keys.
Publish only explicitly selected safe evidence.

## Additive expansion phase

The same bridge admits `expansion.py` **only after the original five-case
Playwright process exits successfully**. Original seed bytes, 12 facts, ten
complete USD 0.002400000000 valuations, two unpriced valuations and every
original negative/control assertion remain unchanged. The new seed refuses a
second invocation. It appends ten facts under a separate project and writes
`expansion-seed.json`. It uses declared priority tariffs through the actual
writer/maintenance/projector, not inserted aggregate rows.

Fresh SQL verifies facts, both valuations, session activity/parent/previous
links, delegation discriminators, active-session pointer and the closed
`ManagedConversationLink`. The own conversation has a prior root and its
rotation, then a renewed active root. Its delegated child has a rotation,
grandchild and sibling; a managed child has its own conversation. Another
actor contributes a separate own conversation. Two USD provider/model tariffs
and one EUR tariff are independently published: no exchange rate or mixed-band
total is manufactured.

The real authorized Work graph producer resolves the child's two physical rows
to its representative rotated session, title and parent node key. The manifest
binds both focused summary requests to that actual navigation anchor; it does
not assume the rotation root is what the UI selects. The browser expands the
existing activity node normally and verifies the selected-session inspector.

Three second-phase synthetic `UsageProviderSample` observations go through the
canonical writer under a mapped, actor-owned **disabled, non-default** provider:
historical maxima 90 then 20, followed by a missing observation. They create no
inference facts, monitor or upstream request. Real owner-scoped GET history and
fresh SQL/query witnesses retain raw native units, unknown latest utilization
and the exact last timestamp. No fake coalescing or maximum-as-current inference
is used; a mutation witness rejects substituting that maximum as utilization.

| Population | Attempts | Known USD | Known EUR | Partial / unpriced |
|---|---:|---:|---:|---|
| Expansion project | 10 | 0.088000000000 | 0.019200000000 | 1 / 2 |
| Combined original + expansion | 22 | 0.112000000000 | 0.019200000000 | 1 / 4 |
| Conversation direct: both own roots and rotation | 4 | 0.013600000000 | none | 1 / 2 |
| Conversation inclusive: own history + descendants | 9 | 0.066400000000 | 0.019200000000 | 1 / 2 |
| Child execution direct: child and rotation | 2 | 0.021600000000 | none | 0 / 0 |
| Same execution inclusive: plus grandchild, not sibling/parent | 3 | 0.036000000000 | none | 0 / 0 |

The partial attempt has 2,000 known input tokens and **unknown output**, with
the measured USD 0.004000000000 input component. Its price/coverage is not zero
and is not promoted to complete. The missing tariff has no monetary allocation;
the partial contribution is also counted among unpriced attempts.

The bridge stores real-auth HTTP packets in `expansion-api-preflight.json`:
filtered and combined `cost_breakdowns`, then direct/inclusive conversation and
execution `session-summaries`, and stored provider history. Every complete marginal must reconcile exactly
to **that response's own** currency/basis totals. Per-fact persisted membership
also fixes each subject/agent/provider/model key, count, partial/unpriced coverage
and amount; exchanging labels between cohorts cannot pass on totals alone.
Original logical-request timers retain their genuinely unallocated null
provider/model cohorts with zero physical attempts and no invented money.
Browser tables retain the corresponding label-to-amount association. It then invokes
`ui/e2e/usage-expansion-live-qualification.spec.ts` with its private
`COGNIS_USAGE_EXPANSION=1` gate. Running that file standalone is not admission.
The second browser process receives only the remainder of the original
360-second browser-phase budget; this does not extend the outer attempt or
PID1 lease. A timeout is incomplete qualification, never permission to raise
budgets or skip original cases.

The unreset full mock history is checked before the original browser, after
expansion seeding/preflights, and in the existing finalizer over **both**
phases. Any added inference fails, including setup-like calls caused by an
analytics action. Page-local dispatch journals retain stable request identity,
method/path, body hash, selector/inclusion, response or failure; pending and
aborted requests still count for hidden-panel silence. Catalog dispatches are
recorded too. The silence baseline precedes leaving Usage for Overview, so a
request emitted by that transition cannot become an accepted baseline request.
The three expected partial/mixed-band ranking refusals (Costs, Performance and
Companion Services) must each be the actual 422 `mixed_cost_ranking` for the
two-band fixture. No other HTTP error is accepted; every query
retains the explicit admin/expansion-project population. Sidebar capture coverage
and observed-through labels must match their own response metadata, independently
of monetary pricing coverage. The default source excludes pending identities:
finalized/succeeded counts remain exact, but observed Total must display Unknown,
not a sum inferred from terminal contributions. The bridge's canonical run directory is the only
capture-output input; no undeclared browser-output environment variable is needed.

**Unresolved final product witness:** `query_service.py` refuses cost ranking
over USD/EUR with `mixed_cost_ranking` before incomplete-price checking. Final
`adapter.ts` only handles `incomplete_cost_ranking` as a section error, then
rethrows the mixed refusal and replaces known Costs/Performance trends with
the generic unavailable view. The no-service seed test proves the exact
producer refusal. The browser's populated-trend assertions are deliberately
retained: this product defect is not converted to a passing missing-data screen
or a single-currency fixture. Dashboard owner correction and final source
reconciliation are required before browser admission.

### Expansion capture matrix

The four serial expansion cases cover desktop Chromium 1440×1000 and mobile
Chromium emulation 390×844, one context at a time. Each device requests:

- USD and EUR overview captures; four additional scrolled USD attribution-panel
  captures, so lower mobile panels are not called visible merely because they
  exist in the document.
- Five additional view captures: Costs & Tokens, Performance, Limits,
  Companion Services, Sessions & Requests. Empty measurements and absent
  companion data require their actual unavailable explanations, not fake zero
  charts. Limits require explicit stored-history loading and never poll a
  provider. Stored sample fixtures use the real writer and producer; historical
  `maximum_utilization` is never asserted as current utilization or plotted as
  observed utilization. The returned original observations are keyboard-opened
  and inspected without an additional API dispatch.
- Conversation direct/inclusive and focused child execution direct/inclusive:
  four captures. The direct/inclusive pair retains the same execution selector.
  Both prior and active own roots must contribute to conversation direct.
- Real keyboard disclosure and legend activation, normal protected drill/Back,
  hidden drawer/non-Usage-tab dispatch checks, label target dimensions,
  viewport/scroll-width clipping and actual target hit tests. Dashboard captures
  retain the existing painted-text/floating-chrome detector; the chat modal is
  checked on its own bounds/hit targets, not forbidden from covering its inert
  background composer.

Each chat state also captures the actual scrolled bottom and checks the final
Refresh control's full viewport visibility, hit target and scroll-owner position.
That is 19 PNGs and corresponding geometry packets per device, 38 requested
new PNGs total. They are candidate evidence until manually inspected. A
60-second case or the shorter enclosing lease may leave the matrix incomplete;
report exact executed cases/captures, not an all-device pass. No old captures
are reused. Physical iPhone/Safari, WebKit, PostgreSQL, actual elapsed expiry,
paid-provider Capture and deployment are outside this matrix.

### Final-source and input gates

Source reconciliation uses dashboard `70d6a7f53f7dba5606cd4e01ce46fe0eccf9d096`
and sidebar `50e0d5c8e607d7cd55d5680e664bede4391cd209`, consumed via approved
composition `e37d0543790fc0601f18ae1e3f00ec91ff9db8a1`. Panel/cohort names,
raw adapters, full-chat Usage caller/scope, tree navigation, quota maxima and
freshness fields have been inspected against that source, not active worktrees.
Original lifecycle/session/request/physical-trail navigation now opens the
actual native disclosures while preserving all five cases and data/guard
assertions. This is static reconciliation plus no-service SQL evidence, NOT
rendered DOM/authenticated-browser acceptance. Parent still freezes and builds
the final combined source through the unchanged provenance owner. A missing
route/stream or final UI defect is blocked/product evidence, never permission
for state injection or an alternate test page.

Preparation needs a new approved private namespace/evidence root and final-lock
dependency closure: Python runtime wheels alone do not establish fresh frozen
uv sync, uvx companion startup, or package build gates. The historical tooling
image/receipt is an input candidate, not proof of Node version, lock-compatible
physical node_modules, Playwright/Chromium, final compiled UI, or service
readiness. BM25's pinned config/stopword data requires its verified manifest and
an approved declared input path, not a hidden cache copy. No export/traversal of
`/opt/usage-expiry/cache`, rejected third roots or retired launcher replay.

Runtime admission remains parent-owned: network none, private volumes, nonroot,
read-only root, capability drop and clean environment; no host binds/ports,
credentials or companion probes; ≤4 CPU/4 GiB/default workers capped at four;
normal Make lock admission 30 seconds, browser case 60 seconds, phase
360 seconds, attempt 400 seconds, independent PID1 lease ≤300 seconds.
Do not override resources, heap, TTL, authority or clocks.

### No-service development witness

`tests/e2e/usage_checkpoint/test_expansion.py` uses disposable SQLite and real
writer/projector/query owners without starting app, companions or a browser.
It executes original seed then revocation then expansion; verifies original
manifest preservation, both marginal populations and all four summary
expectations; rejects missing marginals, exchanged cohort identities, changed
totals and duplicate seeding. Original attempt and logical-request valuations
are fingerprinted using their actual `(source_kind, source_id)` identity.
It is development evidence, not auth/app/browser acceptance.

```sh
make validate VALIDATION_LOCK_TIMEOUT=30 COMMAND='env UV_OFFLINE=1 UV_NO_SYNC=1 PYTHONPATH=.:packages/common/src:packages/executor/src .venv/bin/python -m pytest tests/e2e/usage_checkpoint/test_expansion.py -q -o addopts='
```
