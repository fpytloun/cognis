# HA E2E Compose

The HA overlay extends the existing Local Compose and deterministic E2E files.
It adds PostgreSQL, MinIO, a migration service, two controllers, and one nginx
load balancer. The default profile intentionally remains Redis-free and
unchanged.

```bash
make ha-e2e-prepare
make ha-e2e-up
docker compose --project-name cognis-ha-e2e \
  --env-file .local/cognis-ha-e2e/current/compose.env \
  -f compose.local.yml -f compose.e2e.yml -f compose.ha-e2e.yml \
  run --rm seed-e2e
```

Preparation writes random keys and credentials under
`.local/cognis-ha-e2e/`, which Git ignores. No fixed secret is committed.
Existing credentials are reused so stop/start preserves access to retained
PostgreSQL and encrypted data. Use
`uv run python scripts/prepare_ha_e2e.py --force` only together with deliberate
removal of retained HA volumes. Complete credential bundles are staged under a
restrictive directory and activated through one atomic `current` symlink switch;
an interrupted generation leaves the previous bundle active.
Forced regeneration rotates the database-encryption key as well as service
credentials, so retained encrypted rows are unreadable unless the previous key
is restored.

The only published endpoint is `http://localhost:18080` on `cognis-lb`.
Override it with `COGNIS_HA_E2E_PORT`; internal service ports remain unchanged.
The overlay resets inherited publications for Cognis, mock LLM, Mnemory,
Intaris, and Qdrant, so it can coexist with the ordinary E2E stack on
8080/8090. Controllers use their service DNS names as stable internal
addresses.

Run `make ha-e2e-qualify` for the assembled live qualification. In addition to
migration/readiness/object-store checks, it:

- routes two authenticated HTTP/WebSocket clients through the one published
  nginx endpoint to different controllers using the test-only
  `X-Cognis-HA-Controller` header;
- registers two same-selector WebSocket executors, physically connects them to
  different controllers, pins a conversation to executor 1, and checks durable
  connection ownership;
- kills the controller owning a durably admitted request at a pre-model
  checkpoint, restarts it, and requires exactly one canonical user item, one
  assistant completion, and exactly one recovery claim;
- restarts executor 1 with the same ID inside reconnect grace and requires the
  persisted pin and generation to remain unchanged;
- stops executor 1 past reconnect grace, admits later work, and requires one
  atomic selector-primary failover to executor 2 plus exactly one durable notice;
- then stops controller 1, proves controller 2 is directly ready, and requires
  20 consecutive public readiness, liveness, and JWKS API successes.

The verifier uses only production HTTP/WebSocket endpoints for user operations.
It uses read-only PostgreSQL queries to assert durable ownership, request
attempts, and notice cardinality, and Docker Compose only to inject the intended
process failures. The deterministic routing header and upstream diagnostic
response header exist only in `docker/ha-e2e/nginx.conf`; the Helm ingress does
not recognize them.

Current limitations: accepted-unknown tool dispatch is covered by focused
runtime tests rather than deliberately corrupting an executor operation in this
assembled run. Explicit-primary and additional-executor no-failover semantics
also remain component/integration coverage. Any intermittent dead-upstream
response fails qualification.

## Opt-in Redis profile

Append `compose.redis-ha-e2e.yml` to enable one private, health-checked local
Redis shared by both controllers:

```bash
make redis-ha-e2e-config
make redis-ha-e2e-up
docker compose --project-name cognis-ha-e2e \
  --env-file .local/cognis-ha-e2e/current/compose.env \
  -f compose.local.yml -f compose.e2e.yml -f compose.ha-e2e.yml \
  -f compose.redis-ha-e2e.yml run --rm seed-e2e
```

The overlay uses the existing `COGNIS_REDIS_URL` only. Its credential-free URL
is restricted to the private Compose network and is not a production example.
Browser WebSockets and REST can be pinned independently with the existing
test-only `X-Cognis-HA-Controller` routing header: attach the observer WebSocket
to controller 2 and admit REST work through controller 1.

After seeding, run `make redis-ha-e2e-qualify`. The command is destructive to
the Redis service during its outage scenario and therefore is not part of the
default test target.

The deterministic `redis-ha-remote-progress` mock scenario leaves thinking,
partial assistant text, and a tool call active long enough to exercise:

- remote runtime detail before canonical completion;
- reconnect hydration without duplicate item IDs;
- Redis stop mid-turn, where the turn continues and remote UI retains durable
  active-turn state before exactly one final canonical assistant item;
- Redis restart, after which a subsequent turn resumes remote streaming;
- owner kill/takeover, where fencing rejects late envelopes from the old owner.

Live fault-injection qualification is intentionally external and opt-in because
it stops Compose services. Focused structural assertions and report schema live
under `tests/e2e/`; environments without the assembled HA integration are
skipped rather than substituting fake production hooks.

For read amplification, run 20 clients across both controllers against one
conversation and count Intaris requests by normalized unique query. A warmed
Redis event cache must add zero Intaris reads for the repeated query. During
Redis outage, clients fall back to direct Intaris reads without changing the
canonical result. The counter must wrap the actual Intaris HTTP path; a
standalone synthetic counter is not acceptable.

The Redis event cache is demand-filled rather than a complete replica of the
Intaris event store. Exact cache hits do not read Intaris; cold, evicted, or
previously unseen query shapes read Intaris once and populate Redis. Mutable
watermarks, empty results, active tails, and historical pages default to a
one-hour sliding TTL. Cognis append notifications invalidate both tiers
immediately. Sliding refresh is generation-validated and thresholded, so hot
conversations remain warm without a Redis write on every local hit.

Canonical append invalidation is dispatched outside Intaris' listener deadline.
The app-local dispatcher retains at most 4,096 opaque session tokens and
coalesces each token to its newest watermark. PostgreSQL signaling and Redis
generation work retry independently with bounded backoff. Capacity eviction,
retry exhaustion, or shutdown timeout clears local L1 and bypasses canonical
caches process-wide for the configured cache TTL. PostgreSQL signaling
normally invalidates remote L1 immediately; the TTL fallback bounds freshness
when queued propagation cannot complete.

Capture, but do not enforce brittle CI timing thresholds for:

- owner-local first-frame time and remote frame delay;
- observer enqueue duration;
- maximum relay queue depth and payload-size histogram;
- Intaris request counts by unique query;
- ownership-validation success rate.

Unit tests enforce structural/nonblocking properties. E2E reports retain the
measured values for comparison.

Stop without deleting data using `make ha-e2e-down`. Every HA command uses the
dedicated `cognis-ha-e2e` Compose project, so cleanup cannot stop containers in
the ordinary E2E project. Add `--volumes` to that dedicated project's composed
`down` command only when PostgreSQL and MinIO qualification data should be
destroyed.

For the Redis-enabled overlay use `make redis-ha-e2e-down`; use
`make redis-ha-e2e-clean` only when Redis, PostgreSQL, and MinIO qualification
volumes should all be destroyed.
