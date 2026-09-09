# Deployment

Cognis is designed as a cloud-native controller with one or more executors. The controller can run in a stable server environment while executors run wherever tools, browsers, credentials, files, or network access should live.

## Deployment model

The controller owns:

- users, agents, projects, tasks, workflows, schedules, settings, and secrets metadata
- chat and task orchestration
- model routing and provider configuration
- Mnemory and Intaris integration
- the bundled web UI

Executors own runtime work:

- filesystem, shell, search, and LSP tools
- browser automation and persistent browser profiles
- MCP servers attached to the executor
- optional executor-routed LLM inference
- local workspace state and caches

For ephemeral jobs, a WebSocket executor can be mostly stateless. For browser profiles, local identity, code workspaces, and LSP caches, mount a persistent executor home directory.

## Local development

The simplest local run uses `uvx`:

```bash
uvx cognis-controller
MNEMORY_JWT_PUBLIC_KEY=~/.cognis/keys/public.pem uvx mnemory
INTARIS_JWT_PUBLIC_KEY=~/.cognis/keys/public.pem uvx intaris
```

For development from a checkout:

```bash
uv pip install -e ".[dev]"
uv run cognis-controller serve
```

The published `cognis-controller` package is remote-WebSocket-only. If the
controller must run local in-process or subprocess tools, install the executor
package as well:

```bash
pip install "cognis-controller" "cognis-executor[full]"
```

There is no `local-executors` extra. The official controller image intentionally
contains no local executor runtime, so it must connect an external or sidecar
executor. Persisted local executor rows are shown as unavailable until that
package/process is installed and connected.

## Docker controller

Run the remote-only controller with a persistent data volume:

```bash
docker run -d \
  --name cognis \
  --add-host=host.docker.internal:host-gateway \
  -p 8080:8080 \
  -v cognis-data:/data \
  -e COGNIS_DATA_DIR=/data \
  -e COGNIS_MNEMORY_URL=http://host.docker.internal:8050 \
  -e COGNIS_INTARIS_URL=http://host.docker.internal:8060 \
  ghcr.io/fpytloun/cognis:latest
```

On Linux, `--add-host=host.docker.internal:host-gateway` lets the container reach services running on the Docker host.

## Local Compose deployment

For local personal use, demos, development, and implementation-test feedback
loops, Cognis also ships a single-instance Local Compose deployment:

```bash
cp .env.local.example .env.local
# Fill COGNIS_LOCAL_LLM_BASE_URL and COGNIS_LOCAL_LLM_API_KEY.
set -a
source .env.local
set +a
make local-compose-build
make local-compose-up
make local-compose-wait
make local-compose-seed
make local-compose-executor-up
```

This starts local Qdrant, published Mnemory/Intaris images, a Cognis controller
built from the checkout, and a WebSocket executor built from the checkout. The
seed step configures a single-user Cognis instance with an env-authenticated
OpenAI-compatible provider and a dummy local agent.

Local Compose uses local SQLite/data volumes and is intentionally not a
production high-availability topology. Use `make local-compose-reset` only when
you intend to destroy all local users, secrets, conversations, memory, Intaris
events, Qdrant vectors, and executor browser profiles.

See [Local Compose Deployment](local-compose.md) for the full environment
contract and executor options.

## WebSocket executors

Create a WebSocket executor in `Settings -> Executors`, generate a token, and run the executor image:

```bash
docker run -d \
  --name cognis-executor \
  -v cognis-executor-home:/home/cognis \
  -e COGNIS_CONTROLLER_URL=wss://cognis.example.com/api/executor/ws \
  -e COGNIS_EXECUTOR_TOKEN=eyJ... \
  ghcr.io/fpytloun/cognis-executor:latest
```

Use `wss://` for remote executors. Plain `ws://` is only appropriate for localhost or trusted local development networks.

## Stateless versus stateful executors

Use a stateless executor when:

- the work is ephemeral
- no browser login state is needed
- files can be fetched or generated per task
- the executor can be replaced at any time

Use a persistent executor home when:

- browser profiles should survive restarts
- code workspaces or generated files should persist
- LSP, package, or build caches matter
- a channel adapter needs local identity or helper service state

The published executor image uses `/home/cognis` as its persistent home.

## Reverse proxy and TLS

Put Cognis behind a reverse proxy for public deployments. The proxy should support:

- HTTPS for the web UI and REST API
- WebSocket upgrade for chat and executor connections
- request body limits appropriate for artifacts and uploads
- long-lived WebSocket timeouts

Set `COGNIS_TRUSTED_PROXY_CIDRS` to the comma-separated CIDRs of the immediate
reverse proxies. Cognis uses this setting for forwarded schemes, Secure cookies,
and WebSocket origin checks. Trusted proxies must replace untrusted
`X-Forwarded-Proto` values and sanitize all forwarded headers.

Remote executors should connect to `wss://<host>/api/executor/ws`.

## Kubernetes and high availability

The Helm chart at [`deploy/helm/cognis`](../../deploy/helm/cognis/) supports the
same image in simple, production single-replica, and HA configurations. HA uses
two or more controllers behind one public Service and Ingress; a headless
Service exists only for controller discovery.

HA requires PostgreSQL, S3-compatible artifact and tool-output storage, shared
external crypto/signing material, and a successful migration Job. Redis remains
optional and is configured only through `COGNIS_REDIS_URL`. It is recommended
for full realtime multi-controller UX and lower Intaris read amplification, but
is not part of readiness or correctness. The supported Redis-free profile shows
remote durable progress and recovers final canonical content from Intaris; the
Redis-enabled profile additionally relays volatile runtime detail and shares the
raw event cache. See [High availability](high-availability.md).

Canonical event-cache retention and compression are configurable independently
of the Redis URL. Defaults keep hot conversations warm for one sliding hour and
compress JSON values of at least 64 KiB before applying the 2 MiB stored-value
limit:

```text
COGNIS_EVENT_CACHE_TTL_SECONDS=3600
COGNIS_EVENT_CACHE_SLIDING_TTL=true
COGNIS_EVENT_CACHE_COMPRESSION_ENABLED=true
COGNIS_EVENT_CACHE_COMPRESSION_THRESHOLD_BYTES=65536
COGNIS_EVENT_CACHE_MAX_VALUE_BYTES=2097152
```

Compression uses a bounded versioned wire format and a hard 16 MiB
decompressed-value limit. Disabling Redis retains the configured local L1
policy; Redis and codec failures remain cache misses rather than readiness or
request failures.

### Trusted Mnemory evidence rollout

Trusted evidence is disabled by default. Enable it only after Cognis and
Mnemory contract versions are deployed together:

```text
COGNIS_TRUSTED_EVIDENCE_ENABLED=false
COGNIS_TRUSTED_EVIDENCE_OWNER_ALLOWLIST=
COGNIS_TRUSTED_EVIDENCE_MAX_ATTEMPTS=8
COGNIS_TRUSTED_EVIDENCE_MAX_AGE_SECONDS=3600
```

Evidence requires both `COGNIS_TRUSTED_EVIDENCE_ENABLED=true` and an effective
agent owner in `COGNIS_TRUSTED_EVIDENCE_OWNER_ALLOWLIST`. The allowlist is a
comma-separated list of owner email identifiers. Cognis trims and lowercases
each entry; an empty list selects nobody, and an empty entry between commas is
invalid. This list selects agent owners, not authenticated users. For example:

```text
# Initial rollout: no evidence markers, rows, or JWTs.
COGNIS_TRUSTED_EVIDENCE_ENABLED=false
COGNIS_TRUSTED_EVIDENCE_OWNER_ALLOWLIST=

# Canary one owner after both service contracts are deployed.
COGNIS_TRUSTED_EVIDENCE_ENABLED=true
COGNIS_TRUSTED_EVIDENCE_OWNER_ALLOWLIST=owner@example.com
```

The feature flag and owner allowlist control new evidence admission only.
Cognis stores each admission decision with the durable direct turn. The first
request for an idempotency key owns this decision.

A positive decision is also stored in the Intaris event marker and evidence
queue row. A queue worker uses this frozen decision after an authoritative
event reread. Its current feature flag and allowlist do not revoke the row.
Missing, malformed, negative, or unsupported admission data never creates
evidence.

Removing an owner stops new evidence admission for that owner. New-code workers
can still complete previously admitted rows. They reread the exact event and
make sure that the session, conversation, agent, user, owner, marker, and hashes
still match. A policy-fingerprint difference creates a warning and metric. It
does not change readiness or reject a valid admitted row.

Use two rolling updates for the first activation:

1. Deploy the rolling-safe Cognis binary to all replicas with evidence disabled.
2. Make sure that all old binaries are gone.
3. Add the optional allowlist Secret reference and enable one canary owner.
4. Roll the two controller replicas one at a time.
5. Make sure that one ready Service endpoint remains during the complete update.
6. Make sure that both replicas report no active policy mismatch after the update.

Do not mix an old binary with active evidence admission. An old worker does not
understand the frozen admission decision. For configuration rollback, disable
new admission with another rolling update. Keep the rolling-safe binary until
all admitted rows are terminal.

For Helm deployments, configure the optional Secret reference without putting
owner identifiers in values files:

```yaml
trustedEvidence:
  enabled: true
  ownerAllowlistSecret: cognis-policy
  ownerAllowlistSecretKey: trusted-evidence-owner-allowlist
  maxAttempts: 8
  maxAgeSeconds: 3600
```

The referenced Secret must contain one comma-separated owner allowlist value.
The empty default selects no owners.

The controller writes only a versioned marker to the durable user event. The
remember queue rereads the exact Intaris event, derives a fresh 60-second
evidence JWT, and sends it only to `/api/evidence/remember/v1`. Queue payloads
contain assertions and hashes, not message content or tokens.

The copied Mnemory golden contract fixture is from
`mnemory/tests/contract/fixtures/evidence_remember_v1.json` at server commit
`3dda2c0494783b7e9a36e15094460513b150b343`; its SHA-256 is
`5e734a5597c40c0b7116b98253525774ef55d283c2cf9412896977ad3be8e407`.

Retry is bounded by both maximum attempts and maximum age. Accepted, replayed,
recovered, skipped, rejected, conflict, abandoned, and unavailable outcomes
are terminal for evidence. Ordinary user and assistant memory rows run only
after evidence reaches a terminal state, including abandoned or unavailable.
Evidence dispatch lease expiry returns to pending; ordinary ambiguous remember
behavior is unchanged.

The lifecycle is append → marker → deterministic evidence row → exact reread →
fresh JWT dispatch → retained terminal ledger → dependent ordinary rows. The
agent loop coordinates this sequence; marker, hash, reconstruction, and queue
policy live in focused core modules.

Roll out Mnemory first. Then roll out the rolling-safe Cognis binary with the
flag disabled. Enable the canary only after all Cognis replicas use this binary.

### External rolling-update watchdog

Run the watchdog on Maitrea. Do not run it in a Cognis controller pod. Start it
before the Argo sync that changes the controller configuration:

```bash
python3 scripts/watch_statefulset_rollout.py \
  --namespace "$NAMESPACE" \
  --statefulset "$STATEFULSET" \
  --service "$SERVICE" \
  --health-url "$HEALTH_URL" \
  --target-image "$TARGET_IMAGE" \
  --application "$ARGO_APPLICATION" \
  --rollback-revision "$ROLLBACK_REVISION"
```

Set each variable from the approved deployment revision. Set
`ROLLBACK_REVISION` to the immutable previous healthy Git revision. The
watchdog passes these values as separate `kubectl` arguments.

The watchdog rolls back after three consecutive unsafe samples. Unsafe samples
include more than one unavailable controller, no ready Service endpoint, HTTP
503, another non-success status, and read failures. It also rolls back on
timeout. After rollback, make sure that one endpoint stayed ready and both
controller replicas are ready.

The HA chart uses two replicas, a StatefulSet rolling update, and a PDB with
`minAvailable: 1`. The StatefulSet controller replaces one ordinal at a time.
The external watchdog verifies availability during the update because the PDB
does not control normal StatefulSet replacement order.

## Multi-user hardening

For shared deployments, prefer WebSocket executors and disable local executor modes:

```text
executors.allow_in_process=false
executors.allow_subprocess=false
```

This keeps tool execution outside the controller process and makes executor trust boundaries easier to reason about.

## Backups

Back up:

- `COGNIS_DATA_DIR`, including JWT keys, secrets key, SQLite database, and artifacts
- PostgreSQL database if using PostgreSQL instead of SQLite
- Knowledgebase vector storage, such as the configured Qdrant collection, if
  the optional Knowledgebase feature is enabled
- Mnemory storage and vector data
- Intaris database and event store
- persistent executor homes if browser profiles or workspaces matter

The secrets encryption key is required to decrypt stored secrets. Losing it makes encrypted secrets unrecoverable.

## Upgrades

Simple mode runs schema bootstrap on startup. Production can instead run
`cognis-controller db upgrade` before starting with
`COGNIS_SCHEMA_MODE=validate`. HA requires that migration/validation split.

Revision `110_conversation_lineage` is additive. It backfills only canonical
top-level fork/task provenance and adds owner-scoped lookup indexes. Apply it
before controllers aggregate transitive Work evidence.

Before upgrading production:

- back up Cognis, Mnemory, and Intaris data
- review release notes for schema or executor changes
- restart executors after controller upgrades when tool schemas or browser settings changed

Helm rollback does not reverse a completed database migration. Use
expand-contract migrations so old and new controllers can overlap safely.

## Systemd

Systemd unit templates for the controller and executors live in [`deploy/systemd/`](../../deploy/systemd/). Use these when you want long-running services without Docker.
