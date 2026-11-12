# Native binary transfers

Status: native HTTP data plane implemented as the follow-up to the WebSocket
message-size fix. Local qualification is recorded below; production ingress and
real object-store performance still require deployment-specific qualification.

## Objective

Transfer arbitrary binary files efficiently in both directions between executors
and controller-managed storage, including files of 1 GiB and larger. Remove
product-wide artifact-size ceilings. Do not encode file bodies as base64 or JSON,
and do not materialize complete file bodies in process memory.

The existing negotiated JSON chunk transport remains a control-message safeguard.
It is not the bulk-data transport: reassembling a large base64 JSON response still
requires memory proportional to the file and can exceed controller forwarding
limits.

## Decision: separate control and data planes

- WebSocket JSON-RPC carries commands, small typed descriptors, transfer IDs,
  receipts, and progress metadata.
- HTTPS carries raw binary request/response bodies with ordinary HTTP streaming
  and backpressure. The executor initiates connections, so no executor ingress
  port is required.
- The same opaque-byte transport serves files, images, Office documents, archives,
  model assets, and future binary producers. MIME type does not select the
  transport.
- Uploaded bytes are staging data, not a published artifact. Existing artifact
  ownership, project authorization, revision, and result-acceptance rules decide
  publication.

This keeps bulk backpressure away from executor heartbeat/cancellation messages
and prevents file bytes from traversing the controller-to-controller RPC bridge.
Native binary WebSocket multiplexing is not the first implementation: it would
also need stream framing, credit windows, fair scheduling, and binary forwarding
without removing single-connection TCP head-of-line blocking.

## Owners

| Owner | Responsibility |
|---|---|
| Common contracts | Typed transfer descriptors, receipts, protocol capability |
| Controller transfer service | Scoped grants, admission, state transitions, receipt verification, cancellation, expiry, and cleanup |
| HTTP routes | Authenticate/adapt raw streaming requests; no tool or publication policy |
| Artifact store | Generic bounded-memory streaming writes/reads and atomic object completion |
| Executor transfer client | Stream file handles, calculate integrity metadata, verify receipts/downloads, publish local files atomically |
| Tool router | Issue grants only after normal tool authorization; consume receipts through the same direct/recovered-result boundary |
| Artifact revision service | Reuse existing preauthorization, revision-head locking, transaction, and failed-publication cleanup |

Tools receive a typed transfer client through `ToolExecutionContext`, not a
second HTTP implementation or an untyped private-field injection. Controller
composition constructs the transfer service explicitly. Common cannot import
controller or executor implementation code.

## Upload protocol

1. Following tool authorization, the controller issues a short-lived,
   upload-initiation grant for that call attempt. It binds user, agent, session,
   conversation, call/attempt, selected executor, executor instance, and
   direction. Grant credentials remain internal runtime data, never model
   arguments or visible tool output.
2. The executor requests an upload slot with filename, MIME type, and exact
   declared byte length obtained from an opened file. The controller chooses
   the transfer ID and immutable storage key; the executor cannot choose an
   arbitrary namespace or destination object.
3. The controller atomically admits the slot and returns a narrowly scoped
   upload descriptor. The executor streams the file as the raw request body.
   Empty files are valid.
4. Sender and receiver incrementally count bytes and calculate SHA-256. The
   receiver rejects truncation and excess bytes against the declared length.
   The sender checks that its source did not change while being read.
5. After storage completion and revalidation of the live grant/call attempt,
   the receiver records an uploaded receipt containing the observed byte count
   and SHA-256. The sender compares these with its transmitted stream.
6. The executor returns only the receipt and attachment metadata over RPC.
   Controller result acceptance verifies the receipt's scope and state and
   consumes it idempotently through the artifact revision owner. Uploaded data
   is not independently sufficient authority to publish or supersede content.

A lost upload response is reconciled by transfer-status lookup using the same
scoped descriptor. A completed upload is never blindly overwritten or
automatically resubmitted as a new publication.

## Download protocol

- After existing content authorization, the controller issues a descriptor for
  an exact immutable content revision, with length, digest, filename, MIME type,
  and a scoped download URL.
- Generic downloads stream arbitrary MIME types, not only `video/*`.
- The executor downloads to a private temporary sibling file, verifies the
  complete length and digest, then atomically replaces the intended destination.
  Cancellation or corruption never exposes a partial destination.
- Standard HTTP byte ranges are supported for readers and future safe resume.
  A range is bound to the same immutable revision; content cannot change beneath
  an offset.
- Destination origins are controller-selected and restricted. Redirects cannot
  leak credentials or turn the transfer client into an arbitrary authenticated
  HTTP fetcher.

`artifact_save`, Office inputs/outputs, browser uploads, document assets, and
other binary producers consume this common client. Explicit base64 content
references remain an opt-in format for small downstream APIs that require them;
they must not silently become the internal transport for an artifact reference.
Transfer support is negotiated. Unsupported peers fail clearly rather than
falling back to whole-file base64 for a large file.

## State and consistency

The transfer service owns durable metadata, not file bodies:

```text
issued -> receiving -> uploaded -> consumed
   |          |           |
   +----------+-----------+-> cancelled / expired / failed
```

- Persist scope, immutable object key, exact declared length, observed
  length/digest, attempt identity, admission expiry, progress time, and receipt
  consumption/publication identity. Store bearer credentials hashed.
- Use short compare-and-set transactions for admission and state transitions.
  Never retain a DB transaction while reading the HTTP body or writing storage.
- Claim upload slots atomically; concurrent requests cannot both become the
  receiver for one slot.
- Serialize receipt consumption against cancellation and obsolete attempts.
  Repeated acceptance returns the same artifact/revision, not a duplicate.
- Recheck session/project access and attempt validity at the publication
  boundary. A physical-socket ownership fence is relevant where that socket's
  authority is used; the HTTP receiver's controller identity is not the
  requesting controller or physical-socket owner.
- On interrupted/reconnected attempts, do not silently resume or publish late
  results. Reconcile the previous receipt first and issue a new grant only for
  an authorized retry.
- Expiry/cancellation cleanup removes only unconsumed objects. A cleanup worker
  cannot delete an object already committed to a document.
- Table changes require both Alembic and idempotent bootstrap coverage, with
  SQLite and PostgreSQL evidence.

## Storage, memory, and throughput

- There is no fixed maximum artifact size. Declared length is an integrity and
  resource-reservation fact, not a product-size cap.
- Bound concurrent transfers, application/SDK queues, in-flight bytes, and idle
  waits. Reject exhausted capacity explicitly rather than buffering without a
  limit or exhausting the controller's own disk.
- Use bounded chunks, initially 1 MiB at the executor/HTTP application boundary.
  Actual ASGI reads may be smaller and are not accumulated into a whole body.
- Filesystem storage streams into a private staging file on the final
  filesystem and atomically completes it; avoid a second full-file copy.
- S3 storage uses native multipart upload with bounded part buffers and bounded
  parallel part uploads. Start with 8 MiB parts and at most four in flight.
  Completion occurs only after length/integrity and authorization checks.
  Abort incomplete multipart uploads on failure.
- Keep the public upload API one streaming body initially. Do not introduce
  parallel HTTP chunk assembly or resumable manifests without throughput
  evidence that a single stream is insufficient.
- Hash incrementally. S3 multipart ETags are not SHA-256 digests.
- Never close or delete a file while an offloaded worker still uses it.
  Cancellation must join the active worker, prevent late completion, and
  remove or abort uncommitted storage state.
- Deadlines distinguish admission expiry and lack of progress from total
  duration. A continuously progressing large transfer must not fail merely
  because it exceeds a small control-RPC timeout.
- Logs contain IDs, lengths, timing, outcome, and throughput, never binary
  content, arguments/results, signed URLs, or bearer credentials.

## Multi-controller and deployment contract

Grants, state, receipts, and stored bytes must be reachable when issuance,
upload, and consumption land on different replicas. Use shared S3 or a shared
filesystem for multi-controller deployments. A controller-local filesystem is
valid only for a single controller unless an explicit streaming owner relay is
implemented. Load-balancer affinity is not a correctness mechanism.

Ingress must not buffer whole request bodies or impose an artifact-size ceiling.
Qualify body limits, upload/download idle timeouts, response streaming, and
executor reachability through the actual ingress path. Configuration/deployment
changes require their own authorization and cannot be inferred from a code push.

## Acceptance

1. Round-trip empty files, arbitrary non-text binary types, and at least 1 GiB
   of incompressible data with identical byte counts and SHA-256.
2. Prove that no base64 file body or file-sized JSON crosses either control
   link, including the controller forwarding bridge.
3. Measure peak RSS and application buffers with a slow sender, slow receiver,
   and the configured concurrent-transfer count. Memory must be independent
   of file size; report actual bounds rather than assuming SDK defaults.
4. Benchmark uploads and downloads against raw streamed HTTP through the same
   ingress/storage topology. Target at least 70% of that baseline initially;
   report elapsed time and MiB/s for 1 GiB before considering extra parallelism.
5. Heartbeats, cancellation, and unrelated tools remain responsive during the
   benchmark. Verify no artifact publication after cancellation and bounded
   cleanup of partial files/multipart uploads.
6. Test expired/wrong-context grants and receipts, concurrent slot claims,
   replayed consumption, source mutation, truncation, excess bytes, corruption,
   disk full, lost HTTP responses, and cancellation around storage completion
   and revision commit.
7. Exercise issuance, receiving, and consumption on different controllers.
   Ownership takeover or stale attempts cannot publish or access another
   context's bytes.
8. Preserve revision conflict, project authorization, direct/recovered result,
   and existing small-file behavior tests. Preview/parser limits remain
   separate from file storage and transfer capacity.

## Delivery sequence

1. Complete the reviewed WebSocket size-limit quick fix independently.
2. Introduce common descriptors, storage streaming primitives, and the
   controller transfer owner with lifecycle/auth/integrity tests.
3. Wire executor upload/download and artifact publish/save end to end, with
   transactional staged-object adoption by the revision owner.
4. Migrate other binary producers/consumers to the same client; remove internal
   whole-file base64 paths rather than leaving competing transports.
5. Run multi-controller, failure-mode, and production-equivalent 1 GiB
    qualification; document the measured limits and rollout requirements.

## Implementation and lifecycle notes

The controller's transfer service owns upload scopes and staged receipts. File
bodies live only in artifact storage. The tool router issues a grant after normal
authorization and verifies the receipt against the server-owned call context.
Receipt verification does not commit an intermediate claim: the publication
transaction locks the scope and receipt and commits `uploaded -> consumed`
together with the artifact record. SQLite takes a writer lock at this boundary;
PostgreSQL uses row locks. A recovered or concurrent acceptance returns the same
publication. Cancellation and cleanup cannot delete a committed artifact.

Document publications keep existing revision, project, and conflict semantics.
Other attachments keep their original conversation/agent scope rather than
acquiring an explicit project membership. Structured revision conflicts are
identical for native and inline publications. Artifact-backed document assets
use download descriptors, not the unrelated remote-URL fetch limit.

Admission capabilities last one hour and active receive progress renews the
lease. A receiver admits four streams, each using 1 MiB application chunks.
The executor queues at most two read-ahead chunks. S3 uses four concurrent
disk-backed multipart parts, initially 8 MiB; part sizes grow for larger objects
without growing in-memory part buffers. Idle reads time out independently of
total HTTP transfer duration; configured tool deadlines remain authoritative.
The maintenance worker reclaims expired, unconsumed storage after its quiet
period, including abandoned S3 multipart uploads after controller loss.

Mixed-version peers retain the existing inline format when a native grant is
absent. This supports rolling upgrades, not a large-file guarantee for old peers.
Remove that path when the minimum supported controller/executor versions require
native transfer capability. Explicit base64 value references remain supported
for external APIs that require that representation.

### Qualification boundaries

`scripts/benchmark_binary_transfers.py --size-mib 1024` exercises incompressible
data over real loopback TCP, filesystem storage, SHA-256 verification, publication,
atomic save, memory sampling, and concurrent control HTTP probes. Client and
controller share one process; this is not an ingress or real-S3 benchmark.

Focused tests cover SQLite and PostgreSQL schema bootstrap (twice), formal
upgrade/downgrade, lengths beyond the signed 32-bit range, stale attempts,
cross-controller storage, truncated/empty/invalid transfers, cancelled writes,
lost HTTP and database acknowledgements, concurrent/recovered publication,
sharing scope, revision conflicts, and large document assets. Multipart tests
include bounded SDK concurrency, failed completion, abandoned-upload cleanup,
and actual boto3 HTTP requests against a loopback S3 protocol simulator.

Still qualify the production ingress and real S3 backend, slow sender/receiver
behavior with four concurrent GiB transfers, deployment IAM, and measured
heartbeat/cancellation behavior on the deployed controller-forwarding topology.

### Recorded local evidence

Reviewed implementation: `41df65b28dcdc8e6bc17de47af463a82bf56977d`.
The 1 GiB benchmark verified publication and atomic save with matching SHA-256:

| Measurement | Result |
|---|---:|
| Native upload | 13.03 s / 78.57 MiB/s |
| Native download | 11.62 s / 88.14 MiB/s |
| Raw HTTP/storage upload baseline | 128.60 MiB/s |
| Raw HTTP/storage download baseline | 74.87 MiB/s |
| Metadata-only RPC result | 522 bytes |
| Combined client/controller RSS growth | 24.20 MiB |
| Maximum concurrent control HTTP probe latency | 87.3 ms |

Native upload reached 61.1% of the raw baseline in this run, below the proposed
70% target; native upload additionally hashes at the client. These are local
observations, not a claim that all throughput/slow-peer acceptance is complete.

The broad affected regression run passed 626 tests. After review corrections,
292 affected tests passed, followed by 41 final lifecycle/revision/S3 tests and
21 native PostgreSQL tests. Three optional PostgreSQL cases skipped in the local
revision suite were also exercised separately with PostgreSQL: that selection
passed four cases, including its SQLite schema variant.

The isolated-package matrix passed ten cases; its ownership-allowlist case fails
on pre-existing cache-policy/video module entries, reproduced using wheels built
from untouched base `ae9f504e1`. All three affected packages were type-checked;
comparison with that base found no new errors. Existing common/executor/controller
typing failures remain and are not suppressed by this change.
