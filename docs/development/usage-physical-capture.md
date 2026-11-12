# Physical inference capture

This slice extends the factory/attribution and pricing checkpoints. It does not
install application lifecycle wiring or claim universal provider/pricing coverage.
The application/storage owner still owns `app.py`, the collector lifecycle,
projection signals, price publication and the shared pricing fingerprint.

## Physical ownership

| Path | Physical owner | Non-physical layers |
| --- | --- | --- |
| Controller Chat / Responses | `usage_sdk.SDKScope` below HTTPX single-send and aiohttp innermost middleware | `LLMService`, provider routing, `ObservedLLMTransport` |
| Executor Chat / Responses / embeddings / SDK images | The same portable SDK scope inside executor backends | Controller RPC and executor runner; neither emits a second physical attempt |
| Native Anthropic | `AnthropicMessagesClient._request`, with native decoded SSE projection | Controller/executor backend facade and explicit retry owner |
| Native Codex | `DirectCodexTransport._submit` and its request-owned stream | Provider selection and recovery |
| System One | Native single-POST lifetime in `request_systemone`, without SDK retries | Controller/executor facade |
| Speech / transcription / ElevenLabs | `observed_http` around the actual native HTTP dispatch | Media routing |
| Video | `VideoHTTPAdapter._json` for POST submission, on controller or executor | Job routing, GET polling, metadata and result/image downloads |
| Gateway / Intaris / Mnemory | Resolved provider's physical owner above | Authenticated proxy attribution and client/server delivery spans |
| Auxiliary and generating provider probes | The selected provider's owner; direct Codex probe owns its early-closed request | Metadata, OAuth, quota and connectivity-only reads are excluded |

`test_usage_dispatch_inventory.py` parses the production source and enumerates
qualified SDK/HTTP/facade callsites with exact multiplicities. Async LiteLLM
primitives must be arguments to `observed_sdk`, including conditional image/edit
selection. Native direct HTTP lifetimes are legitimate owners, not missing SDK
wrappers. Auth/discovery/RPC/attribution calls are explicitly classified and are
not charged. This is a regression inventory, not a general security analyzer or
proof that every possible third-party SDK transport has been exercised.

## Positive SDK request purpose

An active SDK scope is attribution, not inference authority. Registered, audited
SDK resource operations supply the operation and request-construction evidence.
Only their matching request builder may mint an opaque, request-local capability.
The capability keeps weak scope/request identities and scalar operation metadata;
it contains no prompts, bodies, headers, credentials or response objects.

HTTPX single-send admission checks that exact capability, live owning scope and
operation. Copying extensions to another request, supplying an arbitrary string
marker, using another scope or continuing after scope closure does not authorize
capture. Legitimate HTTP redirects explicitly derive a new request association.
Client replacement must pass through a registered SDK dispatch/construction path,
not merely occur while a scope is open.

LiteLLM's HTTPX-to-aiohttp bridge can delegate only the matched inference request.
The request-local delegation is claimed once; innermost aiohttp middleware then
records actual configured retries without charging the enclosing HTTPX call
again. Auxiliary/callback and custom-authority token POSTs have no capability.
There is no credential-host denylist or body/header/URL-content classifier.
An unsupported purpose is a diagnosed gap, never guessed inference. A currently
supported inference resource without its grant remains an acceptance blocker.

The current compatibility registry is fenced to OpenAI 2.30.0, LiteLLM 1.82.6
and the relevant HTTPX 0.28.1 request-construction ABI. It includes the common
LiteLLM native resource methods, OpenAI-like/Groq chat, Bedrock Invoke/Converse,
Gemini/Vertex chat, and Gemini/Ollama embeddings. Deferred streaming `make_call`
functions are registered independently, including imported aliases and the
effective Gemini client override. Credential acquisition and embedding file
resolution do not acquire authority from their enclosing operation.

Legacy resources use their actual final URL parameter or the owning SDK's URL
resolver. Vertex's `(authentication, URL)` result is passed through unchanged;
only its scalar URL element is observed. Module-local SDK client factories and
declared SDK default clients supply ownership, not arbitrary replacement clients.
Library upgrades require renewed ABI and physical-admission qualification; these
compatibility hooks can be removed when upstream provides equivalent supported
resource/request observation interfaces.

## Decoded provider evidence

OpenAI **2.30.0** is the audited private decoder compatibility target.
`usage_sdk_openai.install_openai_decoder` checks the installed version and
`BaseClient._process_response_data` signature inherited by `AsyncAPIClient`.
The asynchronous override observes already-decoded data before SDK model
construction and LiteLLM synthesis. The original is invoked exactly once;
observer errors do not replace its return value or exception.

The current active scope must contain the exact response object in its bounded
weak-key association. No request-ID/latest-attempt guess authorizes decoded
provider usage. Values do not retain response keys. Submission finalization
removes associations; scope finalization clears them, rejects late callbacks
and cannot grow the map. Projection retains only typed numeric usage, identity
and meaningful-generation timing evidence, never bodies, events, prompts,
completions, arbitrary mappings/headers or raw exceptions.

Normalized SDK chunks establish delivery/completion only, never reported usage.
Physical generated TTFT requires a meaningful decoded provider event. A usage-only
frame does not establish TTFT. Existing HTTP response JSON decoders remain the
non-streaming provider evidence boundary; no generic/global JSON parser or extra
body buffering is introduced.

Unsupported versions/signatures/decoders leave usage and physical TTFT unknown.
Counters in SSE error events rejected before the private method remain unknown;
previously decoded partial usage survives error, cancellation and early closure.
Configured retries, client replacement and explicit stream resubmission remain
owned by existing inference/recovery code.

## Pricing decision: unknown without proof

The parent and Storage rejected inferring untiered applicability from
`ModelInfo.tier="standard"`: that value can be only a metadata default. Omitted
wire tiers, explicit null/unknown and `auto` therefore remain **unpriced**, even
if a generic/null/default-looking tariff exists. Null is not a wildcard for
priority/flex or another concrete tariff. No new applicability field, schema
extension or late pin mutation was introduced by the original capture slice.

The subsequent untiered correction adds a versioned three-state
`service_tier_applicability` envelope. Only the ABI-fenced OpenAI
`embeddings.create` and `images.generate` resource owner can attach
`not_applicable` evidence to its existing opaque request-local grant. Its safe
projection observes typed construction-option presence and bounded model
identity, not final-body keys, serialized payloads or arbitrary header contents.
Unproven overrides suppress semantic proof without suppressing physical capture.
The existing request identity, one-shot delegation, retry and cancellation
boundaries remain authoritative. Redirect derivation does not inherit semantic
proof.

The configured tariff declaration must independently match the exact operation
and ABI scope. A separate fingerprint domain and immutable price rule preserve
legacy identity and prevent nullable tariffs becoming wildcards. Aggregate
input-only or input/output rates are explicitly configured estimates, never
inferred heterogeneous image tariffs. Only reported required native counters
can complete those valuations. Existing conservative token normalization is
unchanged; absent optional counters are not fabricated as zero.
Unpriced media extras, including an observed output-image count, retain partial
status even when all declared aggregate-token rates and totals are known.

Executor dispatch freezes at most one typed untiered price candidate per RPC
and at most 32 candidate groups per logical request. It stays controller-owned,
is not sent as caller metadata, and is validated at ingestion against exact
provider/model/operation/scope/fingerprint. Executor-reported revision IDs cannot
select a price, and ingestion never consults a newer cache snapshot. Other
tiered executor pricing behavior is unchanged.
Semantic scope is frozen independently of the optional monetary pin. A cold
cache preserves validated applicability and native counts with absent price IDs;
subsequent current repricing can use a newly published matching declaration.

Legacy concrete-tier and proven-untiered tariffs are disjoint pricing contracts:
neither may price the other's facts. Publication may change cost basis only
across those disjoint contracts and within the same currency. A now-ineligible
tariff leaves current value unknown; it never relabels an existing booked amount
or archived stratum with the new estimate basis. Overlapping basis transitions
and currency changes remain rejected.
Compatibility covers all retained provider/model contracts, not just the current
pointer: a disjoint intermediate cannot reset an older overlapping basis.
The existing usage projector owns receipt/dimension derivation for their durable
lifetime. Domain changes use the existing price-state publication transaction;
rebuilds derive dimensions from actual valued amounts, not an ineligible tariff.
Publication streams incompatible contract metadata in bounded batches under that
serialized state fence, without scanning raw facts; rejection rolls back the
candidate revision, pointer/generation and work intent together.
No new external I/O, lock, transaction, retry or cancellation owner is introduced.
Definition immutability and the existing writer/projection idempotency boundaries
remain authoritative.

Contexts, price definitions and archive billing strata use existing JSON
payloads; no SQL column is added. Archives retain aggregate units, missing-unit
completeness and the same applicability scope, so current repricing and replay
cannot reinterpret legacy unknown strata. Historical booked costs remain pinned.
Strict older readers require a coordinated controller/common/executor upgrade.
See [configured estimates](../guide/configuring-providers.md#explicit-untiered-aggregate-token-estimates)
for the supported API configuration.

The integrated `test_usage_dispatch_inventory.py` dispositions remain explicit:
image edits and completion-based images do not inherit generation proof;
Gemini/Ollama/native LiteLLM embeddings do not inherit the OpenAI ABI; native
speech/transcription and ElevenLabs remain captured with unknown unsupported
billing units; video POSTs remain captured while polling/downloads remain
excluded. This correction adds no audio/video tariff or financial-coverage
claim.

An actual supported concrete dispatch tier can use the unchanged shared
fingerprint and exact committed in-memory lookup. A miss/failure stays unpriced.
No inference-side SQL, publication, capacity wait or writer-time price fallback
is allowed. The same `UsageCapture(..., price_lookup=...)` instance enters the
existing registry.

The canonical selection projects submitted effective parameters once. Its concrete
tier supplies both `selection.service_tier` and the unchanged shared fingerprint;
metadata defaults never fill a missing tier.

`tests/qualification/usage_priority_valuation.py` explicitly qualifies the real
factory, installed SDK, accounting writer, fresh persisted canonical DTO and
Storage-derived historical/current valuation. It uses explicit
priority/standard/null tariff rules. The omitted, null, unknown, auto and priority
cases assert that only actual priority is valued. With the fixture rates, the
expected known output subtotal is USD 0.000004 and its
status is **partial**. Unknown cache-write semantics remain unknown; the test
does not invent disjoint input billing or claim complete cost coverage. This
does not establish live production price-publication or external tariff coverage.

## Qualification and limits

The installed loopback tests retain the original LiteLLM positive provider
counts (2/3) and negative absent-usage assertions, with two physical requests and
four observations across the retry. Additional installed SDK tests cover
fragmentation, shared-client interleaved actors, partial/error events, task
cancellation, early close, observer failures and unsupported ABI/version behavior.
Weak-key collection and rejected late association are tested separately.

The real factory reaches the installed native/SDK path and the actual accounting
service. A stopped writer and injected database failure must not fail or replay
inference; queue rejection and persistence loss remain coverage gaps, not zero
usage. Executor observations retain frozen controller correlation and executor
clock domains; lost/unsupported observations never become fabricated RPC attempts.

Native units are retained only when actually reported. Unavailable image counts,
audio/video durations, characters or counters stay unknown, never token
equivalents. Binary responses without billing metadata and asynchronous video
submission acknowledgments may remain unpriced/usage-unknown. Polling and output
downloads are not additional inference.

Only the installed HTTPX/aiohttp paths and decoded event ABI described above have
direct SDK evidence. Other SDK transports/provider event shapes are not a claim
of complete observed usage. Application startup and delivery paths must install
the required capture at their composition root; a missing installation is a
coverage gap, not a silent fallback.

### SQLite and PostgreSQL valuation fixture

The valuation test uses `usage_storage`: it creates the accounting schema in a
temporary SQLite database or a unique PostgreSQL schema, starts the fixture
writer, and owns teardown. The test adds only the `llm_providers` table needed
by the actual factory. It retains the installed SDK request, accounting writer,
fresh-session canonical read, and historical/current valuation assertions.
PostgreSQL requires an explicitly supplied, isolated test database URL.

To rerun the valuation fixture for both dialects from a prepared task-local
Python 3.12 environment, set `COGNIS_TEST_POSTGRES_URL` to an authorized
isolated test database and use:

```sh
make validate COMMAND='env PYTHONPATH=.:packages/common/src:packages/executor/src .venv/bin/python -m pytest tests/qualification/usage_priority_valuation.py -q -n 0 -rs'
```
