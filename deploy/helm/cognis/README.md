# Cognis Helm chart

This chart deploys one Cognis controller image. Simple and single-replica
production modes use a `Deployment`; HA uses a `StatefulSet`.

| Mode | Replicas | Database | Storage | Redis |
|---|---:|---|---|---|
| `simple` | 1 | SQLite by default | Filesystem | Optional |
| `production` | 1 | SQLite or PostgreSQL | Filesystem or S3 | Optional |
| `ha` | 2+ | PostgreSQL | S3 artifacts and tool outputs | Optional |

The chart creates one public Service and, when enabled, one Ingress. A separate
headless Service provides per-pod DNS for the authenticated controller bridge;
it is not an external entrypoint.

## Install

```bash
helm upgrade --install cognis deploy/helm/cognis \
  --namespace cognis --create-namespace \
  -f deploy/helm/cognis/examples/values-simple.yaml
```

HA requires pre-created Secrets because its pre-install migration hook runs
before ordinary chart resources exist:

```bash
kubectl create namespace cognis --dry-run=client -o yaml | kubectl apply -f -
kubectl -n cognis create secret generic cognis-database \
  --from-literal=database-url='postgresql+asyncpg://cognis:REDACTED@postgres/cognis'
kubectl -n cognis create secret generic cognis-crypto \
  --from-file=jwt-private.pem=private.pem \
  --from-file=jwt-public.pem=public.pem \
  --from-file=secrets.key=secrets.key
kubectl -n cognis create secret generic cognis-object-storage \
  --from-literal=access-key='REDACTED' \
  --from-literal=secret-key='REDACTED' \
  --from-literal=signing-secret='REDACTED'

helm upgrade --install cognis deploy/helm/cognis \
  --namespace cognis --create-namespace \
  --wait --wait-for-jobs --atomic \
  -f deploy/helm/cognis/examples/values-ha.yaml
```

The chart does not install PostgreSQL, Redis, S3/MinIO, Mnemory, Intaris, or
Qdrant.

For HA, Redis is optional but recommended for full realtime detail across
controllers and lower Intaris read amplification. PostgreSQL remains the owner
of durable orchestration/leases/fencing and Intaris remains canonical for
content. Redis is disposable acceleration and is not part of readiness.

The chart exposes no separate controller variable: set `redis.existingSecret`
and `redis.secretKey` so the pod receives the existing `COGNIS_REDIS_URL`.
Remote Redis must require authentication with least-privilege ACLs and TLS, be
network-isolated, and use bounded memory plus an explicit monitored eviction
policy. Do not put credentialed URLs in values files, commands, or support
output.

`redis.eventCache` configures canonical event-cache behavior. The defaults are
a one-hour sliding TTL, compression enabled above 64 KiB, and a 2 MiB maximum
stored value:

```yaml
redis:
  eventCache:
    ttlSeconds: 3600
    slidingTtl: true
    compressionEnabled: true
    compressionThresholdBytes: 65536
    maxValueBytes: 2097152
```

The size limit applies after compression. Cognis always enforces a 16 MiB raw
and decompressed safety limit.

Omit `redis.existingSecret` for the supported Redis-free profile. Remote
observers then receive PostgreSQL-backed active-turn state and canonical
Intaris recovery rather than owner-live token/thinking/tool detail.

## HA StatefulSet rollout, migration, and rollback

HA controller pods have stable identities (`<release>-cognis-0`,
`<release>-cognis-1`, …). The StatefulSet `serviceName` is the chart's
headless Service, so controller bridge URLs resolve as:

```text
http://<pod-name>.<release>-cognis-internal.<namespace>.svc:8080
```

Initial HA creation and scale-out use `podManagementPolicy: Parallel`.
StatefulSet rolling updates are one ordinal at a time; Kubernetes StatefulSets
do not support Deployment `maxSurge` or `maxUnavailable`. `minReadySeconds`,
readiness, preStop drain delay, termination grace, PDB, and topology spread
remain in effect. HA keeps its existing `emptyDir` data mount and does not
create per-controller PVC templates; PostgreSQL and S3-compatible stores are
the durable HA state.

`migration.enabled=true` creates a `pre-install,pre-upgrade` hook Job running
`cognis-controller db upgrade`. Controllers then use
`COGNIS_SCHEMA_MODE=validate`. A failed hook blocks rollout and leaves the
previous workload running.

The migration Job accepts only `database.type=postgresql` with a pre-created
`database.existingSecret`. SQLite remains supported in simple mode through
startup auto-bootstrap. The chart deliberately rejects SQLite migration Jobs:
the hook cannot safely share a local database unless the operator mounts the
same pre-created PVC into both Job and Deployment, which this minimal chart does
not implement.

Helm `--atomic` and `helm rollback` cannot reverse a completed database
migration. Releases must use expand-contract migrations compatible with old and
new controllers during the rolling upgrade.

Simple-mode Deployment rolling updates use a surge pod. A single-node filesystem
deployment therefore needs storage that permits the old and surge pod to mount
the claim concurrently (for example RWX, or RWO on the same node where the
driver permits it). Strict single-attach storage can stall an upgrade; use a
planned maintenance window to remove the old pod after it becomes unready.

### Upgrade from an older HA Deployment

Older chart releases rendered HA as a Deployment. Switching that release to
this chart replaces the release-managed `Deployment/<release>-cognis` with a
same-named StatefulSet. Helm renders only the StatefulSet in HA mode, so it
removes the obsolete Deployment and must not leave both workloads active.
Unlike an ArgoCD prune-last cutover, Helm does not guarantee the new StatefulSet
becomes healthy before deleting the obsolete Deployment. Treat this first
workload-kind transition as maintenance-capable even when using `--wait
--atomic`; atomic rollback is reactive and cannot guarantee uninterrupted
service during the replacement.

Before the first such upgrade, verify the release and namespace, then run:

```bash
helm upgrade cognis deploy/helm/cognis \
  --namespace cognis --wait --wait-for-jobs --atomic \
  -f deploy/helm/cognis/examples/values-ha.yaml
kubectl -n cognis get deployment,statefulset -l app.kubernetes.io/instance=cognis
kubectl -n cognis get pods -l app.kubernetes.io/instance=cognis
```

The first command should leave no controller Deployment and exactly one
controller StatefulSet. Verify stable pod names and ready ordinals before
proceeding. Helm rollback can restore the prior Deployment manifest but cannot
roll back completed database migrations; use it only while the old controller
code remains compatible with the migrated schema.

## Internal bridge exposure

The bridge shares the controller listener. A catch-all Ingress can therefore
make `/api/internal/executor-bridge` technically reachable publicly. Controller
JWT authentication is the security boundary.

As optional ingress-nginx defense-in-depth:

```yaml
ingress:
  annotations:
    nginx.ingress.kubernetes.io/server-snippet: |
      location = /api/internal/executor-bridge { return 404; }
```

This is controller-specific and may be disabled by cluster policy. The headless
Service alone does not provide path-level isolation.

Validate rendering and configuration with `make deployment-validate`. Run the
live two-controller migration, sustained failover/API, and object-storage check
with:

```bash
make ha-e2e-qualify
```
