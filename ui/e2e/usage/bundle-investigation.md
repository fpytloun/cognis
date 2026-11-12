# Usage sidebar critical-chat investigation

Status: the authorized row-owned activation change is implemented and reviewed,
but the standard production gate still fails at 470.4 KiB versus 470.0 KiB.
The existing gate is unchanged. This is not a baseline exemption or release
approval. The accepted analytical views, DTOs and fixture screens are unchanged.
The first investigation below is retained history; the current ownership and
qualification are recorded under **Row-owned activation**.

## Measurement

Base: `2f7e9de8bd6255a18902f1447b56714d7fd2ffd9`, with task-local dependencies.
Both graph captures use the same production config plus a read-only
`generateBundle` observer, and the unmodified `check-bundle-budget.mjs` gate.
The observer records actual emitted chunk membership and static/dynamic module
edges. Analysis follows exactly the gate's manifest entry/layout/chat roots and
static imports; it does not count deferred imports.

| Measure | Before | After |
| --- | ---: | ---: |
| Critical JS Brotli bytes | 482,264 | 481,812 |
| Critical JS files | 193 | 193 |
| Gate bytes | 481,280 | 481,280 |
| Bytes over gate | 984 | 532 |
| Eager provider rendered bytes | 3,505 | 1,431 |
| Row component rendered bytes | 965 | 1,063 |

The fresh before measurement is 471.0 KiB, versus the earlier 470.8-KiB build.
The comparable graph captures save 452 Brotli bytes, not the uncompressed
module-size difference. No build or compression settings were relaxed.

## Proven dependency edge

Before, the critical `(app)/+layout.svelte` node statically included the
provider's auth subscription, visibility lifecycle and intersection observer.
Only its request runtime was behind a dynamic import. The analytics adapter,
charts, formatting/model helpers and request runtime were already outside the
critical static closure. The shared API client was not changed by this slice,
and `sidebar-context.ts` has no runtime imports; its types are erased.

After, the provider supplies only synchronous context/registration bridging:

```text
(app)/+layout.svelte -> UsageSummaryProvider.svelte -> sidebar-context.ts
                       --dynamic--> sidebar-observer.ts
                                    -> sidebar-runtime.ts
                                    --dynamic--> adapter.ts
```

The deferred observer now owns auth, document/intersection visibility and
disposal. Activation reads current authority after loading. Registrations removed
or unmounted during loading cannot activate. Hidden rows still issue no reads,
and visible rows still share a batch. The row action owns its text/title/ARIA
updates and releases/re-registers changed conversation IDs.

The observer and runtime are factored into the same non-critical chunk. Rendered
sizes are 2,196 and 1,678 bytes respectively. Neither enters the static chat
closure. The remaining provider bridge, row, context symbol and navigation remain
critical. The row action grew by 98 rendered bytes; the total compressed saving
above is the actual result, not an estimate from module lengths.

## Qualification and evidence

- Focused usage/sidebar tests: 50 passed, including batching/collapse abort,
  unmount before deferred observer resolution, latest-authority activation and
  revocation clearing.
- Narrow initialization review approved
  patch `d48e3adbdd64c6fbb27fa0044a358f3f236087a1`, without waiving the gate.
- `make check-ui`: passed, zero errors and the two existing unrelated warnings.
- `make build-standalone`: passed, including connected and offline variants.
- Exact standard `make build-ui`: production compilation passed, then the
  unchanged critical-chat gate failed at 470.7 KiB versus 470.0 KiB. Emitted
  filenames differ from the diagnostic build; the table above records the
  comparable graph-capture pair rather than substituting this rounded total.
- Full `client.json`, `ssr.json` and `critical.json` captures are preserved in
  `/tmp/cognis-usage-bundle-graph/before-2f7e9de8b/` and
  `/tmp/cognis-usage-bundle-graph/after-context-boundary/`.
- The read-only Vite capture config and analyzer are preserved alongside those
  directories, not installed into the shipping build.
- Archive: `/tmp/cognis-usage-bundle-graph-evidence.tar.gz`, SHA-256
  `7821657af5f4bf1d7c0bd5cb23aa99877fb8b3a85422afe4847e4bed24c344ce`.
  Artifact publication failed in the controller's execution-fence adapter; the
  local archive and committed measurements remain available.

All checks use the shared Make validation lock, task-local dependencies,
`NODE_OPTIONS=--max-old-space-size=3072`, and at most two Vitest workers.

To reproduce a graph capture, restore the archived config to
`ui/vite.usage-graph.config.ts` and analyzer to
`ui/scripts/usage-graph-analysis.mjs`, set `USAGE_GRAPH_DIR` to an empty evidence
directory, and use the shared lock:

```sh
make validate COMMAND="sh -c 'cd ui && npm exec -- vite build --config vite.usage-graph.config.ts && node scripts/usage-graph-analysis.mjs && node scripts/check-bundle-budget.mjs'"
```

Remove only those two restored diagnostic files afterward. The normal build
remains `make build-ui`, without diagnostic config or budget overrides.

## Prior decision (superseded by the row-owned plan)

The same material gate still fails. No second optimization is attempted.
The owner must decide whether to retain this reviewed partial improvement and
authorize a broader startup/sidebar ownership change. No analytics removal,
threshold increase, unrelated-baseline exemption or API fabrication is proposed.

Backend work remains gated on the final API handoff after checkpoint
`c64acfc5c` and proxy extension migration 182, with normative document
`660dd6e7c`. In particular, frontend integration still needs final proxy kind and
consumer-scoped correlation/retention semantics, paged session/explorer contracts,
bounded attribution series, supported error/performance/logical-request filters,
and real SQL/auth/retention acceptance. Existing unavailable states remain honest.

## Row-owned activation

Base: retained partial improvement
`322d7e7e06c3748063eb8e6725ec6dbcab2199ee`. The owner explicitly authorized
removing usage ownership of application startup, rather than another context
bridge optimization. No unrelated features, analytics or budget thresholds
were changed.

The provider, context symbol and separate Svelte badge component are deleted.
The app layout renders its children directly. The actual conversation row
reserves the same badge geometry and owns one small client DOM action:

```text
SidebarConversationRow.svelte -> sidebar-usage.ts
                                --dynamic--> sidebar-shared.ts
                                             -> sidebar-observer.ts
                                             -> sidebar-runtime.ts
                                                --dynamic--> adapter.ts
```

`sidebar-shared.ts` creates one client-only observer scope for concurrent row
registrations. The last unregister disposes the native observer, auth and
visibility subscriptions, WebSocket listeners, cache and active request.
Later rows create a fresh scope. Nothing registers authority or performs browser
work during SSR. The action reads its latest ID after loading, ignores destroyed
rows and keeps the reserved accessible unavailable state if loading fails.
Authority is subscribed only when the client scope is actually created.

### Measured boundary and size

The retained checkpoint's `after-context-boundary` graph is the before capture
for this change. Its reviewed source patch is exactly the source retained in
`322d7e7e0`. The same read-only capture config and gate-root analyzer produced
`after-row-boundary`; dependencies and compression settings are unchanged.

| Measure | Retained provider bridge | Row-owned activation |
| --- | ---: | ---: |
| Critical JS Brotli bytes | 481,812 | 481,691 |
| Critical JS files | 193 | 193 |
| Gate bytes | 481,280 | 481,280 |
| Bytes over gate | 532 | 411 |

The measured saving is **121 Brotli bytes**, not a sum of rendered module sizes.
In the new graph the only critical module under `src/lib/usage/` is
`sidebar-usage.ts`: 739 rendered bytes in `_app/immutable/nodes/11.CJF-U4XK.js`,
imported by the row. Its only runtime static dependency is Vite's preload
helper; `sidebar-shared.ts` is a dynamic edge. The shared registration, observer,
controller and request runtime are outside the static closure, in
`_app/immutable/chunks/DoeVSb05.js`. The adapter, formatting and charts remain
deferred. Shipping navigation remains critical and unchanged.

### Current qualification

All commands use task-local dependencies, the shared Make validation lock and
`NODE_OPTIONS=--max-old-space-size=3072`:

- `make test-ui VITEST_ARGS='src/lib/usage/ src/lib/components/chat/SidebarConversationRow.test.ts --maxWorkers=2'`:
  56 tests passed. Nine actual-row lifecycle cases mount without the old provider:
  shared visible batching, teardown/abort, all rows removed during loading,
  rapid remove/remount, latest-ID rebinding, fresh same-key scope, latest
  authority, revocation/logout, distinct stale results and chunk failure.
- `make validate COMMAND="sh -c 'cd ui && node e2e/usage/sidebar-ssr.mjs'"`:
  passed. Renders the actual row through the Vite SSR module loader with no
  browser globals; the badge is unavailable and client usage scope modules
  are not evaluated.
- `make check-ui`: passed, zero errors and the two existing unrelated warnings.
- `make build-standalone`: passed, including connected/offline variants.
- `make build-ui`: production compilation passed; the exact unchanged standard
  gate failed: 193 JS files, **470.4 KiB JS versus 470.0 KiB**, 33.0 KiB CSS.
  Its emitted filenames differ from the diagnostic capture. The table records
  the matched graph measurements, not an inferred exact standard byte total.
- No new Playwright captures were run: the accepted unchanged chart/explorer
  captures are retained, while this ownership change is exercised through
  provider-free actual-row DOM tests and actual-row SSR. Real backend runtime,
  SQL/authorization/retention and browser/API acceptance remain outstanding.

Narrow lifecycle review approved tree
`75bf320754bf9a64485bc66b4818439fbdb28f0a`. Compatible test-only continuation
approved the stronger same-key,
distinct-late-result and full-promise-drain assertions in final reviewed tree
`008e85b9fa114073a6c92357a140b004bfca4d7b`, patch
`07bf8ec76037d914b98ec570adfa458e9afb92f4`. Production sources are unchanged
from the first approval; this evidence document is the only later addition.
Neither approval waives the failed budget gate.

Full before/after captures and reproduction tools are preserved in
`/tmp/cognis-usage-row-boundary-evidence.tar.gz`, SHA-256
`ac8c469e806740362dc9b14f3ed6ac81bdca32f4e6871583c09b8392d2905d9f`.
Artifact publication still fails in the controller execution-fence adapter;
no successful upload is claimed. Temporary capture files have been removed
from the worktree. The reproduction command above remains applicable.

### Required decision

The requested owner/lifetime boundary is now present. The remaining critical
usage edge is the permitted row facade, not an eager provider or service
runtime. This single authorized ownership correction is complete, but the
material size gate remains blocked. The parent must decide the next justified
size-reduction scope before further implementation. No second optimization,
code-golf, feature removal, unrelated offset or gate change is attempted.
Backend/API adapter integration still waits for the published final contract;
real SQL/auth/retention acceptance is not claimed.
