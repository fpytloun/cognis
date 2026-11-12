# Controller allocation pressure

## Executor receiver frame lifetime

The executor connection's idle receive loop owns only transport lifetime.
Decoded payloads, per-frame callback references and response/inference closures
belong to a single-frame coroutine, which returns before the next socket wait.
Pending futures, inference queues and scheduled callback tasks retain their own
payloads until the existing consumers finish. No payload is cleared in place.

Ownership fences, response ordering, backpressure, error metadata and physical
connection shutdown remain at their existing boundaries. The outer receiver
still terminates pending work exactly once on exit. There is no new lock,
transaction, cache, GC operation or allocator change. Weak-reference regressions
cover completed RPC results, ignored notifications, consumed inference frames
and results deliberately retained by a live consumer. This corrects proven
object retention; production RSS savings must be measured separately.

## Owner-local diagnostic slices

Admin-requested captures can await the session cache's read-only owner provider.
It estimates fixed fields for up to 64 cached sessions, reacquiring entries on
their owner loop and releasing graph references before yielding between roots.
Each root has a cooperative 2 ms / 2,000-object traversal budget. One callback
stops after 100 ms accumulated measured work or five seconds wall time, and
reports selected/unselected coverage, truncated roots, opaque objects and its
largest observed synchronous slice. These are cooperative limits, not hard
real-time guarantees; setup and cleanup are included in slice measurements.

Numeric session/field cursors rotate across calls, including after cancellation,
so expensive early entries cannot permanently starve later entries. A sample
distinguishes selected, visited and fully measured sessions, expected/visited
roots, and complete versus truncated/opaque roots. Selection is capped at 64;
later samples continue beyond that window. Rotation does not resume a truncated
graph walk or establish complete coverage of a changing cache.
Observed population growth, removal or replacement invalidates complete coverage.

The session provider includes projection decisions and project metadata roots.
It also samples the active-thinking graph within the same shared work budget.
Per-field top-level container-item counts and largest observed roots help
identify large containers even when their descendants cannot all be visited.
Cache-level thinking/hydration/eviction counts, turn-owned stream/output/work
counts and LLM client/catalog/token-count cache counts are supplied by their
owners through explicit composition. These are **cardinalities, not byte sizes**;
unavailable/failed providers are reported rather than represented as zero.

No history is hydrated, copied, serialized, touched or evicted. The provider
does not collect garbage, enumerate the global heap, inspect referrers, tune
allocators or start allocation tracing. Output contains fixed field labels and
numeric counts only, never session IDs, keys, payloads or object representations.
Estimates are non-additive reachable bytes and may observe changes between
slices. Truncated roots are lower-bound observations, not whole-root estimates.
They cannot justify eviction or a claimed percentage of RSS savings.

The capture owns cancellation and awaits the provider on the existing owner
loop. No new locks, transactions, retries or external side effects are added;
there is no retained accounting graph after completion or cancellation. Existing
synchronous providers remain supported for startup diagnostics and fixtures.

## Idle allocator accounting

Requested resource captures report Python allocated-block counts and, when the
process uses supported glibc, public `mallinfo2` counters alongside RSS. This is
read-only accounting: no heap walk, garbage collection, memory trimming, tracing,
allocator tuning or live debugger injection. Disabled captures perform no work.

`allocator_arena_allocated_bytes` includes Python allocator pools and tcache-held
blocks, not just live Python objects. `allocator_arena_free_bytes` counts free
space inside arenas, not necessarily resident or immediately releasable pages.
`allocator_mmap_bytes` covers allocator-owned mappings, not every anonymous
mapping. Do not equate these counters with RSS, sum them with RSS, or claim that
all arena-free bytes can be returned to the OS. Compare warmed windows with
similar work and session ownership before selecting allocator or cache changes.
Unsupported libc versions omit the allocator fields. No diagnostic counter is
an authorization or runtime-decision input.

Light captures also include timestamped cumulative process CPU time, Python
thread counts and natural GC collection/collected/uncollectable deltas from the
capture baseline. They do not run GC or install GC callbacks. Resource report
schema 2 identifies the build from `COGNIS_BUILD_SHA`/`COGNIS_BUILD_ID` when
available, otherwise explicitly marks package-version fallback. The outer API
envelope remains version 1; new diagnostic fields are additive.

## Repeated tool registration audits

Per-tool classified lookups use a fresh read-only registry view rather than
re-registering all tools. The view reads current base registrations and the
current step classification map; positive/negative lookups and handlers are not
cached. A missing base tool stays unavailable. Changed contracts still pass
through the successful-content audit mechanism below.

Validation is deliberately scoped to use: a selected effective definition must
pass its native contract audit before it is returned; enumeration/export audits
every effective definition. An invalid unrelated tool no longer prevents use of
a different valid tool, but the invalid tool cannot be used or exported. Audit
failure never falls back to the raw definition. Classification-only overrides
retain name/source identity; remapping uses eager registration and its existing
collision checks. Later in-place identity changes fail closed.

Owned state is limited to one helper invocation's view over existing registry
and step-map references. No new external side effects, shared waits, locks,
transactions or cancellation owner are introduced. The agent loop constructs
the view afresh at its existing lookup/ledger boundaries; it must not retain it
across executor-registry or classification-map replacement. Argument validation,
authorization, retry policy, ledger identities and guardrail decisions retain
their existing owners.

Tool registration reuses only successful, content-identical native contract
audits in a process-local 512-entry LRU. The cache retains SHA-256 digests, not
definitions, schemas, validators or session data. Each lookup fingerprints the
complete current definition and native validator ID catalog; mutable definitions
are not trusted solely on their declared schema hash. JSON-coercible Python
values that do not round-trip exactly bypass reuse and receive a fresh audit.

Explicit descriptor audits, argument/domain validation, authorization and
registry duplicate/merge checks remain unchanged. Failed audits are never
cached. Concurrent misses may repeat the audit rather than serialize expensive
validation under a global lock. Cold registration adds fingerprinting work;
repeated registry construction avoids the same schema/example audit.

## Retention and database read ownership

The S3 maintenance worker owns a disposable SQLite inventory for one complete
listing. No tool payload is read or persisted in it. Its cache is 1 MiB; keys
and numeric timestamps/sizes spill to temporary disk. Existing distributed
maintenance leases and worker cancellation/join remain the owners of scheduling
and interruption. All listing and indexing must finish before the first S3
delete. TTL precedes size-cap eviction, text timestamps take precedence over
anchor timestamps, and failed pairs are not retried within the pass. Partial
successful deletion still reduces remaining-byte accounting.

Explicit executor pools restrict the existing owner/shared SQL query to the
configured IDs. Any label selector retains the existing discovery path.
Authorization, unavailable placeholders, ordering and the single database
snapshot remain unchanged. This reduces unnecessary deserialization but does
not prove ownership of every asyncpg allocation seen in profiling.

Database reads use `cognis.json_codec.decode`, the shared msgspec decoder with
stdlib compatibility fallback. Serialization is unchanged. Real SQLite and
PostgreSQL regression fixtures cover decoded values and unchanged stored text.

## Admin API: preferred operational interface

Use the admin API through Cognis tooling or `scripts/controller-profiling.py`,
with a **normal Cognis admin user's API key** injected from the credential store.
The header is `X-API-Key`, not an inference bearer token. Non-admin, disabled,
expired and inference-only credentials retain the existing middleware restrictions.
Do not mint impersonation tokens, bypass authorization with pod-local code, or
put keys in shell command arguments, source files, URLs or reports.

The helper uses `COGNIS_URL` (HTTPS origin) and `COGNIS_ADMIN_API_KEY` (secret
environment injection), refuses redirects, and supports:

```text
python scripts/controller-profiling.py discover
python scripts/controller-profiling.py start --capture-id "$CAPTURE_ID" --level full --duration 600
python scripts/controller-profiling.py status --capture-id "$CAPTURE_ID"
python scripts/controller-profiling.py report --capture-id "$CAPTURE_ID"
python scripts/controller-profiling.py stop --capture-id "$CAPTURE_ID"
```

Generate a fresh UUID for `CAPTURE_ID` before starting; keep and reuse it if a
request times out. Do not automatically generate a new ID on retry.

| Endpoint under `/api/v1/system/profiling` | Operation |
| --- | --- |
| `GET /` (without trailing slash) | Ready exact controller incarnations, local status, capability/coordination state |
| `POST /captures` | `{capture_id, options, targets?}`; accepted is not started |
| `GET /captures/{capture_id}` | Per-target observed states and staleness |
| `GET /captures/{capture_id}/report` | Structured bounded aggregate reports |
| `POST /captures/{capture_id}/stop` | Idempotent desired stop for this capture only |

Omitting targets snapshots all currently-ready controllers (maximum eight).
Explicit targets must be exact `ready_owners` values, not hostnames. Replacements
never inherit a predecessor's capture. Commands and reports use shared Redis,
so any API replica can handle retries and report retrieval. Redis unavailable
means HTTP 503, not silently disabled or local-only success.

### Levels and limits

* `light`: process RSS and bounded session-owner estimates, without stacks or allocation tracing.
* `cpu`: light plus Python code-stack observations and native thread CPU deltas.
* `allocations`: light plus tracemalloc counters and Python allocation stacks/growth.
* `full`: CPU and allocation modes together. Higher observer overhead; compare a
  separate CPU-only window before attributing production CPU regressions.

CPU mode reports event-loop scheduling-delay buckets, sampler-thread CPU cost,
maximum observed sampling interval, missing thread-stat samples and the residual
between process CPU time and observed thread ticks. Thread coverage is explicitly
Python threads only; native-only threads, GIL-starved sampling, short-lived
threads and interval endpoints limit attribution. Stack rows are observations
at CPU-interval endpoints, not exact per-function accounting. Residual CPU is
not automatically native CPU. Loop delay is not CPU time, and process CPU
includes profiling overhead. No native call-stack profiler is implied.

Options default to 120 seconds (10–600), 100 ms stack sampling (50–1000),
60-second allocation snapshots (30–300), traceback depth 5 (1–10).
`allocation_snapshots=false` keeps counters/tracing but disables expensive snapshot
grouping. No mode reads frame locals, emits object contents, or provides native
heap/request-distributed tracing. Allocation tracing started live cannot recover
the origins of objects allocated earlier. Snapshot grouping may transiently use
substantial CPU/memory and cannot be interrupted safely; stop remains `stopping`
until it finishes and tracing/worker cleanup is complete.

There is one active cluster capture. Same ID/options returns the existing capture;
changed options or overlapping captures return 409. Idempotency/report retention
is the latest four captures for at most one hour, not a permanent request journal.
Download reports promptly. Up to eight 128 KiB reports are retained per capture,
with explicit truncation if reduced. Reads do not extend TTL. Redis data loss can
lose evidence; it cannot make a local capture unbounded. A temporary outage retries
final publication before the local collector is replaced.

Size reduction reports omitted stack-row/allocation-sample/memory-sample counts
and preserves first/last memory samples where the size cap permits. Oversized
endpoint samples still produce an explicit size error; truncation never silently
becomes a complete report.

### Offline comparison

Save both API reports before their TTL expires, then compare without credentials
or network access:

```text
python scripts/controller-profiling.py compare --before before.json --after after.json
```

The helper bounds each input to two MiB. It checks process incarnations, build
and resource schema, timestamp order, capture options, report truncation, owner
coverage and observed cardinalities. It reports raw process-counter deltas and
separate CPU-window averages; partial owner estimates do not produce byte deltas.
Even complete per-field byte deltas are non-additive. It never subtracts owner
estimates from RSS to invent an unaccounted-memory total. Workload is always
marked uncontrolled: matching counts do not prove comparable work or causal
memory savings.

Start also accepts `--sample-interval-ms`, `--traceback-depth`,
`--allocation-interval-seconds` and
`--no-allocation-snapshots` to expose the existing bounded API controls. Disabling
snapshots does not disable allocation tracing or guarantee low observer overhead.

The control loop reads small command metadata every five seconds. No collectors
run when idle. Detailed reports are retrieved through the admin API, not dumped
into Loki; lifecycle logs contain capture IDs and status only. Confirm every
target is terminal, `owned_tracing=false`, and check `tracemalloc_active` before
declaring profiling disabled. An externally-owned tracer is never stopped here.

### Boot configuration and kill switch

`COGNIS_PROFILING_ALLOWED=false` prevents API and boot captures on that process.
Default is API allowed, **no boot capture**.
`COGNIS_PROFILING_BOOT_LEVEL=light|cpu|allocations|full` requests one bounded
capture after readiness; `COGNIS_PROFILING_BOOT_DELAY_SECONDS=300`,
`COGNIS_PROFILING_BOOT_DURATION_SECONDS=120`, and optional
`COGNIS_PROFILING_BOOT_POD` control delay, duration and selection. An unselected
boot request snapshots ready replicas; explicit pod selection targets only that
process. Boot requests share the same cluster admission/expiry rules as API
requests and can be stopped through the API. Discovery exposes boot errors.

Legacy `COGNIS_RESOURCE_PROFILING` and `COGNIS_MEMORY_PROFILING` flags now request
finite captures through this owner, never an indefinite second profiler.
Legacy pod selectors remain local-only, and memory snapshots still require
`COGNIS_MEMORY_PROFILE_SNAPSHOTS=true`. The legacy resource delay/duration/sample
interval and memory interval/depth settings are honored within the new bounds.
Concurrent legacy local boot requests serialize through cluster admission.
Use the new flags for combined captures. Runtime API use needs no restart.

The operator-mounted startup Memray wrapper is a **separate** diagnostic tool.
Keep legacy memory flags off when using API capture to avoid enabling that wrapper
as well. The admin API never enables Memray and does not claim native attribution.

## Bounded runtime attribution

The underlying collector accepts these legacy resource boot settings; prefer
the admin API and unified boot controls above:

| Variable | Default | Meaning |
| --- | --- | --- |
| `COGNIS_RESOURCE_PROFILING` | disabled | `true` or `1` enables one capture per process |
| `COGNIS_RESOURCE_PROFILE_POD` | empty | Exact hostname; empty selects all replicas |
| `COGNIS_RESOURCE_PROFILE_DELAY_SECONDS` | 300 | Wait after application startup; 0–3600 |
| `COGNIS_RESOURCE_PROFILE_DURATION_SECONDS` | 120 | Automatically stop after 1–600 seconds |
| `COGNIS_RESOURCE_PROFILE_SAMPLE_MS` | 100 | Thread-stack sample interval; 50–1000 ms |

Invalid numeric values use their defaults. Flags are read at construction, not
dynamically. Apply environment changes using the normal approved deployment.
The capture does not restart itself; explicit shutdown interrupts warm-up and
joins the sampling worker before application teardown. Disabled or unselected
processes create no sampling task or thread. This mode does not start tracemalloc,
Memray, or any other profiler and does not change existing allocation-tracing flags.

### Reading the output

`resource_cpu` logs elapsed wall time, process CPU seconds, observed Python-thread
CPU seconds, and up to 20 code stacks with observation counts and CPU deltas.
It retains at most 256 distinct stacks with 24 frames each; discarded samples
and their CPU deltas are reported. No frame locals, source lines, arguments,
thread names, task names, prompts, tool results or session IDs are recorded.

CPU deltas come from Linux `/proc/self/task/*/stat`; a delta is associated with
the stack seen at the **end** of that interval, not proven time inside that
function. Native-only threads, short-lived threads, and GIL starvation can leave
CPU unattributed. Process CPU includes diagnostic overhead. Stack counts are
wall observations, not CPU percentages. Idle selector stacks may be frequent
without consuming much CPU. On systems without Linux procfs the stack capture
still works but observed thread CPU is unavailable (reported as zero).

`resource_memory` logs RSS, anonymous/file-backed memory, and estimates for five
session-cache groups: events, prefixes, skills, inventory and runtime metadata.
It runs at capture start, every 30 seconds, and at capture end. Each group has
a 2 ms / 20,000-object / 32-level traversal bound, with explicit truncation and
shallow-only counts. Reported measurement time covers owner traversal. Procfs
reads run off the event loop. The five traversals are synchronous on the cache
owner loop, with a combined nominal 10 ms budget; scheduling and individual
object operations can exceed it.

These are selected reachable Python-object estimates, **not** total heap, native
allocations, serialized bytes, or exclusive retained sizes. Objects are deduplicated
within each group, not between groups; do not sum groups as unique process memory.
Large unsupported objects are shallow-only. Truncated estimates are incomplete
and favor earlier roots; do not extrapolate them or use them as eviction budgets.
The walker never creates a full cache snapshot, serializes payloads, runs garbage
collection, evicts entries, touches recency, or changes session ownership.

Start with resource attribution alone. Compare the same workload and pod lifetime
with diagnostics off to estimate its observer cost. Use the existing explicitly
enabled allocation tracer separately if owner estimates do not explain RSS; a
gap is not by itself proof of a leak or allocator retention.

The executor bridge encodes canonical result bytes directly, rather than
decoding an encoded buffer into a full-size string and encoding it again.
Sorted keys, compact separators, UTF-8 encoding, float spellings, result size
limits and checksums retain the existing protocol. The shared codec keeps its
stdlib fallback for values that require it. This reduces transient allocations;
it does not establish that controller CPU or long-term RSS growth is resolved.

## Optional startup capture

`deploy/scripts/startup-memory-profile.py` is an operator-mounted diagnostic
wrapper, not the normal image entrypoint. If a deployment uses it, publish that
file as the mounted `startup.py` in its diagnostic ConfigMap.

The wrapper requires `COGNIS_MEMORY_PROFILING=true` or `1` and honors
`COGNIS_MEMORY_PROFILE_POD`. When disabled or not selected, it executes the normal
controller entrypoint without importing Memray. An enabled capture requires
Memray to be installed and `/cognis-profile-data` to be writable. It records
allocation metadata for 90 seconds, closes the tracker once, and releases its
reference. Controller exit also closes an unfinished capture.

Changing a mounted wrapper does not remove instrumentation from an already
running interpreter. The next normal deployment starts with the new guard.
Do not initiate an additional restart without operational approval. Existing
capture files are retained; the wrapper neither deletes nor reads their contents.

## Evidence and remaining attribution

A synthetic 28,000,036-byte result with Unicode text produced byte-identical
output before and after the bridge change. One local tracemalloc run measured
74.39 MiB versus 40.05 MiB peak traced allocations and 84 ms versus 21 ms encode
time. These are microbenchmark results, not production RSS or CPU savings.

Session snapshots still deep-copy canonical state to preserve isolation.
Classified inventory already retains only the latest fingerprint per session,
and executor-directory polling already defers observed tool schemas. Neither
is changed speculatively. Sustained CPU and retained anonymous memory require
separate warmed-process attribution.
