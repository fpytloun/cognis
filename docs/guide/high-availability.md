# High availability

Cognis HA uses one controller StatefulSet/image, one logical external load
balancer, one public Service, and optionally one Ingress. Scheduler, workflow,
channel, and turn workers remain in every controller process and coordinate
through durable ownership rather than separate microservices.

## Mode matrix

| Capability | Simple | Production single replica | HA |
|---|---|---|---|
| Replicas | 1 | 1 | 2+ |
| Database | SQLite | SQLite or PostgreSQL | PostgreSQL (`asyncpg`) |
| Artifacts/tool outputs | Filesystem | Filesystem or S3 | S3-compatible |
| Crypto | May auto-generate | External recommended | External required |
| Schema | Startup `auto` | `auto` or migrate + `validate` | Migration Job + `validate` |
| Redis | Optional | Optional | Optional; recommended for full realtime multi-controller UX |

PostgreSQL owns durable orchestration, leases, and fencing. Intaris owns
canonical conversation/session content. Redis is disposable acceleration only:
it relays volatile Chat v2 runtime frames between controllers and caches raw,
authority-partitioned Intaris event reads. Redis loss must not affect readiness,
turn correctness, ownership, or canonical final state.

Use Redis for full realtime UX when a browser is attached to a controller other
than the turn owner and to reduce repeated Intaris reads. Without Redis, the
owner's local clients retain volatile token/thinking/tool detail; remote clients
see the PostgreSQL-backed active-turn spinner and recover canonical content via
REST sync after Intaris settlement. This Redis-free profile is supported and
correct, but performs more Intaris reads.

The relay uses one versioned Pub/Sub channel plus expiring latest-envelope keys
and tombstones for reconnect hydration. It is not Redis Streams, a consumer
group, or a durable queue. Every receiving controller reapplies local
authorization before fanout. Runtime frames remain volatile. The event cache
stores raw Intaris results partitioned by authority and invalidated with
generation-safe keys; it never serves stale canonical data after a Redis or
Intaris error.

The canonical event cache defaults to a one-hour sliding TTL. Reads refresh
expiration only after half the TTL has elapsed, avoiding a Redis write on every
local hit. Redis hits atomically validate the session generation while
refreshing expiration, so an append always wins a refresh race. Values of at
least 64 KiB are compressed by default; the 2 MiB admission limit applies to
stored compressed bytes while decompression is hard-bounded to 16 MiB.
Existing uncompressed entries remain readable until they expire. Configure the
policy with `COGNIS_EVENT_CACHE_TTL_SECONDS`,
`COGNIS_EVENT_CACHE_SLIDING_TTL`,
`COGNIS_EVENT_CACHE_COMPRESSION_ENABLED`,
`COGNIS_EVENT_CACHE_COMPRESSION_THRESHOLD_BYTES`, and
`COGNIS_EVENT_CACHE_MAX_VALUE_BYTES`.

### Redis security

Redis may contain sensitive runtime, session, assistant, thinking, tool, and
canonical event content. For any remote Redis deployment:

- require authentication with least-privilege ACLs;
- require TLS and verify the server identity;
- isolate Redis on a private network and restrict ingress to controller pods;
- configure bounded memory and an explicit eviction policy;
- monitor evictions, memory pressure, reconnects, relay drops, payload sizes,
  and cache errors.

Never place a credentialed Redis URL in documentation, logs, diagnostics, or
support output. Supply `COGNIS_REDIS_URL` through a Secret.

Authentication caches are process-local. User or API-key revocation can
therefore take up to the configured authentication cache TTL (45 seconds by
default) to become visible on every controller. Operators requiring faster
revocation should reduce that TTL and account for the additional database
load.

## Kubernetes topology and secrets

The chart in [`deploy/helm/cognis`](../../deploy/helm/cognis/) renders one
StatefulSet with stable controller ordinals, one public Service, one headless
discovery Service, probes, and a migration Job. The StatefulSet uses parallel
initial creation/scale (`podManagementPolicy: Parallel`) and ordered one-at-a-time
rolling updates; StatefulSets do not support Deployment `maxSurge` or
`maxUnavailable`. PDB,
topology spread, PVC, ServiceAccount, Ingress, and NetworkPolicy are optional.

Pod name becomes `COGNIS_CONTROLLER_ID`; HA ordinal names such as
`cognis-0` and `cognis-1` are stable. Pod DNS through the StatefulSet headless
Service becomes `COGNIS_CONTROLLER_INTERNAL_URL`. The headless Service is internal
discovery, not another public endpoint. Browser/API traffic and executor
outbound WebSockets use the single public Service/Ingress.

HA startup validation requires PostgreSQL, S3-compatible artifact and
tool-output stores, shared ES256 keys, shared secrets-encryption key, shared
artifact signing secret, controller identity/internal URL, and schema validation
mode. Helm HA values must reference existing Secrets. Create them before
installation because pre-install hooks cannot depend on normal resources from
the same release.

Migration Jobs require an external PostgreSQL `DATABASE_URL`. Simple SQLite
deployments retain startup auto-bootstrap. The chart rejects a SQLite migration
Job because it does not mount a pre-created shared PVC identically into both the
hook and controller Deployment.

## Controller bridge

Controllers forward calls to the owner of an executor connection through
`/api/internal/executor-bridge`. The endpoint validates a controller JWT,
requester liveness, target owner, and connection epoch.

The bridge shares port 8080 with public APIs. Catch-all Ingress rules can make
it technically reachable externally; controller JWT authentication is the
security boundary. The chart README provides an optional ingress-nginx deny
example. It is defense-in-depth, not a portable chart guarantee.

## Executor labels and failover

WebSocket executors connect outbound to the public URL. Their owning controller
records durable ownership; peers forward through the bridge.

- Explicit primary pins do not automatically move.
- Label-selector primaries may fail over at a later admission after reconnect
  grace expires.
- Additional executors remain explicit-only. An expired additional assignment
  may return to an eligible primary.
- Task pins persist across workflow steps.

Failover uses compare-and-swap and records a durable system notice naming the
old and new executors and the factual reason. It never runs in the middle of an
accepted operation.

Only an allowlist of read-only unary RPCs may retry with a stable call ID and
executor-local result cache. Tool execution, streaming inference, and mutations
are not replayed after `accepted_unknown` or partial delivery. Executor restart
loses that unary cache. An external side effect remains ambiguous when Cognis
did not observe a terminal result.

### Stateful executor pools

Do not scale one executor token. Every pod needs a distinct Cognis executor
record and token; Kubernetes does not provision them. Create one Secret and
one StatefulSet identity per executor, while giving all records common
application labels such as `pool=browser` for selector matching:

```yaml
apiVersion: v1
kind: Secret
metadata:
  name: cognis-executor-0
stringData:
  token: REPLACE_WITH_TOKEN_FOR_EXECUTOR_0
---
apiVersion: apps/v1
kind: StatefulSet
metadata:
  name: cognis-executor-0
spec:
  serviceName: cognis-executor-0
  replicas: 1
  selector:
    matchLabels:
      app.kubernetes.io/name: cognis-executor
      cognis.io/pool: browser
      cognis.io/executor-id: executor-0
  template:
    metadata:
      labels:
        app.kubernetes.io/name: cognis-executor
        cognis.io/pool: browser
        cognis.io/executor-id: executor-0
    spec:
      enableServiceLinks: false
      securityContext:
        runAsNonRoot: true
        runAsUser: 1000
        runAsGroup: 1000
        fsGroup: 1000
      containers:
        - name: executor
          image: ghcr.io/fpytloun/cognis-executor:latest
          env:
            - name: COGNIS_CONTROLLER_URL
              value: wss://cognis.example.com/api/executor/ws
            - name: COGNIS_EXECUTOR_TOKEN
              valueFrom:
                secretKeyRef:
                  name: cognis-executor-0
                  key: token
          volumeMounts:
            - name: home
              mountPath: /home/cognis
  volumeClaimTemplates:
    - metadata:
        name: home
      spec:
        accessModes: [ReadWriteOnce]
        resources:
          requests:
            storage: 20Gi
```

Create a separate `executor-1` record, token, Secret, and manifest for the
second pod.

## Rolling upgrades and rollback

Use `helm upgrade --install ... --wait --wait-for-jobs --atomic`. The
pre-install/pre-upgrade hook runs `cognis-controller db upgrade` from the new
image before new controllers roll. Old controllers continue serving while it
runs. A failed Job blocks rollout and remains inspectable until its configured
`ttlSecondsAfterFinished` expires or the next hook attempt replaces it.

Helm rollback does not reverse database changes. Use expand-contract:

1. Add backward-compatible schema.
2. Roll while old and new controllers both support it.
3. Verify no old controllers remain.
4. Remove obsolete schema only in a later release.

Rollback to code expecting schema removed by a contract migration is unsupported
without a tested database restore or forward-fix.

When upgrading an older HA chart release that used a Deployment, Helm replaces
the release-managed Deployment with the StatefulSet. Render validation ensures
only one controller workload kind exists per mode; operators should verify the
old Deployment is absent and the StatefulSet's ordinal pods are ready before
considering the upgrade complete.

## Draining and reconnect

On SIGTERM readiness changes to 503 before draining. The controller stops new
admissions, waits for active turns, requests cancellation after the drain
timeout, settles durable ownership, and closes WebSockets. Termination grace
must exceed preStop plus drain and cancellation timeouts.

Browser and executor WebSockets reconnect through the load balancer. Durable
turn, task, schedule, pause, channel, worker, and executor ownership prevents a
peer from treating active work as unowned. It cannot make arbitrary external
side effects transactional; `accepted_unknown` remains ambiguous.

Cluster reconciliation reads Intaris session watermarks under the authoritative
session user, agent, and agent-owner identity loaded from Cognis metadata. Signal
subscription ownership is routing metadata and is never used as authorization
context. This keeps reconciliation of shared agents and different users
isolated across controller replicas.

Durable direct turns are also cluster-authoritative. PostgreSQL records whether
each turn is queued, claimed, running, absorbing, or terminal; every controller
uses that record for Chat v2 queue and active-turn state. A browser may be
connected to a different controller from the one executing the turn without
losing its active indicator. The owning controller supplies optional live token
and tool-output detail only to its local connections; canonical timeline content
is recovered through the normal Intaris sync path after settlement.

After restart, status synchronization and stale direct-turn event reconciliation
recover the session user, agent, and agent owner from durable Cognis metadata
before contacting Intaris. A failed reconciliation is isolated to that request:
it cannot retain a conversation lease or prevent later FIFO requests from being
claimed.

Durable attachment payloads retain artifact identity only. Each fenced claim
re-authorizes the artifact and materializes a fresh, expiry-bounded URL before
execution; that trusted materialized reference bypasses the external attachment
dictionary parser. Unexpected controller failures retry at most three times and
then settle terminally, releasing the conversation FIFO head instead of spinning
indefinitely.

### Resolve a stale tool turn

Administrators can inspect stale durable turns with
`GET /api/v1/system/direct-turns/stale?limit=100`. The response contains only
operational identity, ownership, fencing, phase, timeout, and timestamp fields;
it never returns the persisted request payload, tool arguments, or results.

Use `POST /api/v1/system/direct-turns/{request_id}/resolve-ambiguous` only when
the listed turn is in `tool_in_flight` and its controller lease has expired.
Send a unique `client_transaction_id`, a non-secret operator reason, and the
exact `expected` snapshot returned by the listing. The server returns `409`
when the lease is live, the snapshot changed, or the transaction ID belongs to
another resolution. A same-actor retry with the same transaction ID is
idempotent.

Successful resolution advances the conversation fence, records a redacted
`direct_turn_operator_recovery` audit entry, marks the turn `ambiguous`, and
wakes the next queued turn. It does not replay or cancel the uncertain external
tool effect. Never put credentials, tool input, or other secrets in the reason.

## Dependency failures

| Failure | Behavior |
|---|---|
| PostgreSQL | Readiness fails; controllers cannot coordinate |
| Artifact S3 | Artifact operations fail; HA does not silently use local files |
| Tool-output S3 | Durable output operations fail; tool side effects may remain ambiguous |
| Redis | L2 cache degrades; PostgreSQL-backed correctness remains |
| Mnemory | Memory operations degrade according to provider policy |
| Intaris | Guardrail/audit operations fail closed where required |
| Controller | Ready peers serve; executors bridge through owner or reconnect |
| Executor | New selector admissions may fail over; in-flight operations are not replayed |

An Intaris event-stream `404` is accepted as an empty newly created stream only
after the same scoped identity successfully reads the session metadata. A
missing or inaccessible session remains an error. Deterministic client errors
do not open the shared event circuit breaker; retryable transport and server
failures still contribute to provider outage detection.

## Backup, restore, and key rotation

Back up PostgreSQL, both S3 buckets, Mnemory, Intaris, optional vector storage,
and stateful executor homes. Back up crypto separately. Losing the
secrets-encryption key makes stored secrets unrecoverable.

Restore database, object stores, and matching keys as one recovery point.
Validate schema heads and object access before admitting traffic.

Rotate JWT keys with an overlap while consumers trust both keys. Rotate artifact
signing secrets only after accounting for active signed URLs. Replacing the
secrets-encryption key without an application-supported re-encryption procedure
makes old ciphertext unreadable.

## Observability and qualification

- `/api/livez`: process/HTTP liveness only
- `/api/readyz`: lifecycle, database, and schema compatibility
- `/api/health`: dependency diagnostics, not a liveness probe

Redis is intentionally outside `/api/readyz`. Safe diagnostics expose only
configured/available/relay-connected booleans and aggregate counters; they must
not expose URLs, channels, keys, session/conversation/user/controller identity,
or cached payloads.

Monitor ownership claims/leases, bridge failures, database and schema health,
S3 errors, executor reconnect/failover, `accepted_unknown`, queue lag, drain
timeouts, relay enqueue/publish/receive/apply/drop/reconnect counts, relay queue
depth and payload histogram, event-cache hits/misses/errors/invalidations,
singleflight joins, cache size/bytes, and Intaris request amplification. Logs and
metrics must not contain credentials, prompts, tool arguments, results, or
identity labels.

Before production, qualify controller termination during active turns, executor
owner loss, WebSocket reconnect, PostgreSQL restart, S3 and optional Redis
outage, failed migration, mixed-version rollout, duplicate task/schedule
admission, and backup restore. The local harness is documented in
[HA E2E Compose](ha-e2e.md).

## Operator checklist

Before:

- [ ] PostgreSQL and S3 backup/restore tested
- [ ] required Secrets pre-created
- [ ] migration supports old/new overlap
- [ ] PDB/topology spread match cluster capacity
- [ ] termination grace covers preStop and drain
- [ ] Ingress supports long-lived WebSockets
- [ ] executor records/tokens are unique

After:

- [ ] migration Job succeeded
- [ ] all pods report ready
- [ ] exactly one public Service and Ingress exist
- [ ] controller directory contains expected replicas
- [ ] executor reconnect and bridge routing work
- [ ] no sustained ownership, bridge, schema, or S3 errors

## Guarantees and limits

Cognis provides durable coordination, compare-and-swap ownership transitions,
admission-time selector failover, authenticated controller forwarding, and
validation-only HA startup. It does not provide exactly-once arbitrary external
tool execution, transactional third-party APIs, automatic executor token
provisioning, path-level bridge isolation on the shared listener, database
rollback through Helm, or availability without PostgreSQL.
