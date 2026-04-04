# Cognis: Deployment

## Deployment Modes

- **Local deployment** — single machine, SQLite, in-process executor. For
  personal use or development. Legitimate single-user setup, not just "dev mode."
- **Docker Compose** — containerized, PostgreSQL, staging/small production
- **Kubernetes** — full production, executor pods, horizontal scaling

## Local Deployment

### Quick Start

```bash
uvx mnemory                              # starts on :8050
uvx intaris                              # starts on :8060
uvx cognis                               # starts on :8080
```

On first start, Cognis:
1. Creates `~/.cognis/` directory with auto-generated keys and SQLite DB.
2. Serves the bundled web UI on `:8080` when UI assets are present and `COGNIS_SERVE_UI=true`.
3. Prints a one-time setup URL (15 min TTL) to create the first admin user.
4. Probes Mnemory and Intaris and reports reachability in startup output.

After creating the admin user via the setup URL, open `http://localhost:8080`
and log in. Configure LLM providers through **Settings → Providers** in the
web UI (or via the API).

**Alternative: CLI bootstrap (headless)**
```bash
cognis admin create-user admin@example.com --name "Admin"
```

### What Happens On First Start

Cognis auto-generates everything needed:

| File | Path | Purpose |
|------|------|---------|
| JWT private key | `~/.cognis/keys/private.pem` | Sign JWTs |
| JWT public key | `~/.cognis/keys/public.pem` | Share with Mnemory/Intaris |
| Secrets encryption key | `~/.cognis/secrets.key` | AES-256-GCM for secrets store |
| SQLite database | `~/.cognis/cognis.db` | Metadata, settings, agents |
| Default system settings | Seeded into DB | Sensible defaults for all app config |

### Environment Variables

There is **no configuration file**. Infrastructure config uses environment
variables. App-level config (LLM providers, model routing, session settings,
security policies) is stored in the database and managed via the UI/API.

#### Core (all have sensible defaults)

```bash
COGNIS_DATA_DIR=~/.cognis              # Data directory (default)
COGNIS_HOST=0.0.0.0                    # Bind address (default)
COGNIS_PORT=8080                       # Port (default)
COGNIS_MNEMORY_URL=http://localhost:8050  # Mnemory URL (default)
COGNIS_INTARIS_URL=http://localhost:8060  # Intaris URL (default)
```

#### Keys (auto-generated if missing)

```bash
COGNIS_JWT_PRIVATE_KEY_PATH=~/.cognis/keys/private.pem
COGNIS_JWT_PUBLIC_KEY_PATH=~/.cognis/keys/public.pem
COGNIS_SECRETS_KEY_PATH=~/.cognis/secrets.key
COGNIS_REQUIRE_EXTERNAL_CRYPTO=false  # true = fail fast if keys missing
```

For production: provide your own keys and set
`COGNIS_REQUIRE_EXTERNAL_CRYPTO=true`. For local: auto-generation is fine.

#### Redis (session cache L2)

```bash
COGNIS_REDIS_URL=                      # empty = L1-only (default)
# COGNIS_REDIS_URL=redis://localhost:6379/0
```

When set, the session cache uses Redis as an L2 shared store. On Redis
failure, the cache degrades to L1 in-process + Intaris cold loads.

#### Tool Output Storage

```bash
COGNIS_TOOL_OUTPUT_BACKEND=filesystem  # "filesystem" or "s3" (default: filesystem)
COGNIS_TOOL_OUTPUT_S3_ENDPOINT=http://localhost:9000
COGNIS_TOOL_OUTPUT_S3_ACCESS_KEY=
COGNIS_TOOL_OUTPUT_S3_SECRET_KEY=
COGNIS_TOOL_OUTPUT_S3_BUCKET=cognis-tool-outputs
COGNIS_TOOL_OUTPUT_S3_REGION=
```

When `s3`, tool outputs are stored in MinIO/S3 instead of the local
filesystem. TTL cleanup uses object `LastModified` timestamps.

#### Database (optional override)

```bash
DATABASE_URL=sqlite+aiosqlite:///~/.cognis/cognis.db  # default
# Production:
DATABASE_URL=postgresql+asyncpg://cognis:pw@localhost:5432/cognis
```

#### Container / CI Bootstrap

```bash
COGNIS_INITIAL_ADMIN_EMAIL=admin@example.com
COGNIS_INITIAL_ADMIN_PASSWORD=...
```

Creates the admin user on startup if the users table is empty.

#### Optional

```bash
COGNIS_LOG_LEVEL=info                  # Logging level
COGNIS_LOG_FORMAT=json                 # json or text
COGNIS_SERVE_UI=true                   # Serve bundled UI assets
COGNIS_CORS_ORIGINS=http://localhost:5173  # CORS allowlist
```

### Mnemory/Intaris JWT Configuration

For local deployment, point Mnemory and Intaris at Cognis's public key:

```bash
# Mnemory
MNEMORY_JWT_PUBLIC_KEY=~/.cognis/keys/public.pem

# Intaris
INTARIS_JWT_PUBLIC_KEY=~/.cognis/keys/public.pem
```

For production, use the JWKS URL instead:
```bash
MNEMORY_JWKS_URL=https://cognis.example.com/.well-known/jwks.json
INTARIS_JWKS_URL=https://cognis.example.com/.well-known/jwks.json
```

## Docker Compose

```yaml
services:
  cognis:
    build: .
    ports:
      - "8080:8080"
    environment:
      DATABASE_URL: "postgresql+asyncpg://cognis:${POSTGRES_PASSWORD}@postgres:5432/cognis"
      MNEMORY_URL: "http://mnemory:8050"
      INTARIS_URL: "http://intaris:8060"
    env_file: .env
    volumes:
      - cognis-data:/data
    secrets:
      - jwt_private_key
      - jwt_public_key
      - secrets_key
    depends_on:
      postgres: {condition: service_healthy}
      mnemory: {condition: service_started}
      intaris: {condition: service_started}

  postgres:
    image: postgres:16
    environment:
      POSTGRES_DB: cognis
      POSTGRES_USER: cognis
      POSTGRES_PASSWORD_FILE: /run/secrets/postgres_password
    secrets: [postgres_password]
    volumes: [pgdata:/var/lib/postgresql/data]
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U cognis"]
      interval: 5s
      retries: 5

  # Internal services - NOT exposed to host
  mnemory:
    image: ghcr.io/fpytloun/mnemory:latest
    # No host ports - only accessible within Docker network

  intaris:
    image: ghcr.io/fpytloun/intaris:latest
    # No host ports - only accessible within Docker network

volumes:
  pgdata:
  cognis-data:

secrets:
  jwt_private_key: {file: ./keys/private.pem}
  jwt_public_key: {file: ./keys/public.pem}
  secrets_key: {file: ./keys/secrets.key}
  postgres_password: {file: ./keys/postgres.pw}
```

Note: Mnemory and Intaris are NOT exposed to the host. The bundled Cognis
image serves both the API and the UI on `:8080` by default. Set
`COGNIS_SERVE_UI=false` when running API-only pods behind a separate static
frontend deployment.

### Dockerfile

```dockerfile
FROM node:20-slim AS ui-build
WORKDIR /app
COPY ui/package*.json ./
RUN npm ci
COPY ui/ .
RUN npm run build

FROM python:3.12-slim AS runtime
WORKDIR /app
ENV COGNIS_SKIP_UI_BUILD=1
COPY pyproject.toml README.md build.py ./
COPY cognis/ ./cognis/
COPY docs/ ./docs/
COPY ui/ ./ui/
COPY --from=ui-build /app/build ./ui/build
RUN pip install --no-cache-dir .
EXPOSE 8080
CMD ["cognis", "serve"]
```

## Kubernetes

### Single-Replica Production (Current)

Cognis runs as a single replica alongside Mnemory and Intaris in a shared
namespace. The bundled image serves both the API and the UI on `:8080`.

```
Namespace: openwebui (shared with Mnemory, Intaris)
  Deployment: cognis (replicas: 1, strategy: Recreate)
  Service: cognis (ClusterIP, port 8080)
  Ingress: cognis.fpy.cz (TLS via cert-manager)
  Certificate: cognis.fpy.cz (letsencrypt-prod)
  Secret: cognis-secret (JWT keys, secrets key, DB URL, MinIO creds)
  PVC: cognis-data (1Gi, local-path, for COGNIS_DATA_DIR)
```

#### Dependencies

| Dependency | Service URL | Purpose |
|------------|-------------|---------|
| PostgreSQL | `postgresql.postgresql.svc.cluster.local:5432` | Cognis metadata DB |
| Redis | `redis.redis.svc.cluster.local:6379` | L2 session cache |
| MinIO | `http://minio.minio.svc.cluster.local:9000` | Artifacts + tool outputs |
| Mnemory | `http://mnemory.openwebui.svc.cluster.local:8050` | Memory provider |
| Intaris | `http://intaris.openwebui.svc.cluster.local:8060` | Guardrails provider |

#### JWT/JWKS Integration

Cognis issues ES256 JWTs. Mnemory and Intaris validate them via JWKS:

```bash
# Mnemory
MNEMORY_JWKS_URL=http://cognis.openwebui.svc.cluster.local:8080/.well-known/jwks.json

# Intaris
INTARIS_JWKS_URL=http://cognis.openwebui.svc.cluster.local:8080/.well-known/jwks.json
```

Existing API key auth on Mnemory/Intaris is preserved for standalone
access. Cognis uses JWT bearer tokens exclusively.

#### Production Crypto

JWT private/public keys, secrets encryption key, and artifact signing
secret are mounted from a Kubernetes Secret into `/keys/`. Set
`COGNIS_REQUIRE_EXTERNAL_CRYPTO=true` to fail fast on startup if any
key file is missing (prevents auto-generation of divergent keys).

```yaml
volumes:
  - name: keys
    secret:
      secretName: cognis-secret
      items:
        - key: jwt-private.pem
          path: private.pem
        - key: jwt-public.pem
          path: public.pem
        - key: secrets.key
          path: secrets.key
```

#### Executor Policy

For multi-user production, disable local executor modes via the UI
Settings page (`Settings → Executors`):

- `executors.allow_in_process` → `false`
- `executors.allow_subprocess` → `false`

Only WebSocket (remote) executors are permitted. These settings are
DB-backed and persist across restarts. Default settings are seeded only
when missing (non-destructive), so manual changes are never overwritten.

#### Storage Backends

| Data | Backend | Config |
|------|---------|--------|
| Metadata (users, agents, settings) | PostgreSQL | `DATABASE_URL` |
| Artifacts (images, uploads) | MinIO/S3 | `COGNIS_ARTIFACT_BACKEND=s3` |
| Tool outputs (ephemeral) | MinIO/S3 | `COGNIS_TOOL_OUTPUT_BACKEND=s3` |
| Session cache (L2 shared) | Redis | `COGNIS_REDIS_URL` |
| Session cache (L1 hot) | In-process memory | Always active |

Tool outputs use S3 with metadata stored alongside the object. TTL
cleanup uses S3 object `LastModified` timestamps. The `read_tool_output`
and `search_tool_output` built-in tools load the full object into memory
for line-based read/search — acceptable for ephemeral tool outputs.

#### Rollout Order

1. Deploy Cognis manifests. Wait for `/api/health` to return healthy.
2. Verify JWKS endpoint: `GET /cognis/.well-known/jwks.json`.
3. Bootstrap first admin via `COGNIS_INITIAL_ADMIN_*` env vars or setup URL.
4. Update Mnemory deployment with `MNEMORY_JWKS_URL`.
5. Update Intaris deployment with `INTARIS_JWKS_URL`.
6. Verify cross-service health at `/api/health/providers`.

Rollback: remove `*_JWKS_URL` env vars from Mnemory/Intaris to revert
to API-key-only auth. Cognis can be deleted independently.

### Multi-Replica Production (Phase 2+)

```
Namespace: cognis
  Deployment: cognis-controller (replicas: 2+)
  Deployment: cognis-ui (replicas: 2+)
  Service: cognis-controller (ClusterIP)
  Ingress: cognis.example.com
  ServiceAccount: cognis-executor (can create Jobs)

Namespace: cognis-executors
  Job: cognis-exec-{id} (on-demand, created by controller)
```

Multi-replica requires:
- Redis-backed shared session cache (implemented)
- Sticky WebSocket sessions via ingress affinity
- Shared tool output storage via S3 (implemented)
- Distributed turn scheduling / session locking (not yet implemented)

Network policies: executor pods can reach cognis-controller only. They
cannot reach Mnemory or Intaris directly.

## Configuration Model

There is **no configuration file**. Configuration is split between
environment variables and database-stored settings.

### Environment Variables (Infrastructure)

Set before the process starts. See "Local Deployment > Environment
Variables" above for the full reference.

### Database Settings (Application)

Managed via the UI Settings page or `GET/PUT /api/v1/settings`. Seeded
with sensible defaults on first start.

### UI Environment Variables

The SvelteKit UI reads browser-visible environment variables:

| Variable | Purpose |
|----------|---------|
| `PUBLIC_COGNIS_API_URL` | Cognis API origin used by the browser. Leave empty for same-origin/proxied deployments or set an explicit public backend URL. |
| `PUBLIC_INTARIS_UI_URL` | Base URL used by the Settings page cross-service link to Intaris. |
| `PUBLIC_MNEMORY_UI_URL` | Base URL used by the Settings page cross-service link to Mnemory. |

| Category | Key | Default | Description |
|----------|-----|---------|-------------|
| session | `session.max_context_tokens` | 128000 | Context window budget |
| session | `session.compaction_threshold` | 0.85 | Trigger compaction at this % |
| session | `session.compaction_preserve_turns` | 10 | Turns to keep uncompacted |
| session | `session.max_tool_calls_per_turn` | 50 | Max tool calls per turn |
| session | `session.idle_timeout_seconds` | 1800 | 30 min idle → mark idle |
| session | `session.max_session_age_seconds` | 86400 | 24h max session age |
| session | `session.max_delegation_depth` | 5 | Max delegation chain depth |
| session | `session.max_queued_messages` | 5 | Max queued messages per session |
| session | `session.escalation_timeout_seconds` | 300 | 5 min escalation timeout |
| decision_engine | `decision_engine.inline_max_length` | 200 | Short messages → inline |
| decision_engine | `decision_engine.classifier_timeout_ms` | 500 | Classifier timeout |
| decision_engine | `decision_engine.classifier_fallback` | "inline" | Fallback on classifier failure |
| security | `security.non_bypassable_tools` | ["shell","bash","write_file","delete_file"] | Always go through guardrails |
| security | `security.token_ttl_seconds` | 3600 | JWT TTL |
| security | `security.max_connections` | 100 | Max WebSocket connections |
| security | `security.ws_auth_timeout_seconds` | 10 | Close unauthenticated WS connections after this timeout |

LLM providers and model routing are managed via their own API endpoints
(see [10-api-spec.md](10-api-spec.md)).

### Production Deployment Patterns

#### Reverse Proxy for Cross-Service UI Access

For production, use a reverse proxy or ingress to serve all three UIs
under one domain:

```
cognis.example.com/              → Cognis UI + API
cognis.example.com/intaris/      → Intaris UI + API
cognis.example.com/mnemory/      → Mnemory UI + API
```

This is an infrastructure concern — nginx, Traefik, or Kubernetes Ingress
handles the routing. Cognis does not proxy Intaris/Mnemory itself. A shared
JWT cookie across the domain enables seamless cross-service access.

## Monitoring

### Health
```
GET /api/health → 200 healthy | 503 degraded
GET /api/health/providers → detailed provider status
```

### Metrics (Prometheus)
```
cognis_turns_total{agent_id, status}
cognis_tool_calls_total{tool_name, decision}
cognis_delegations_total{mode, status}
cognis_llm_calls_total{model, status}
cognis_turn_duration_seconds{agent_id}
cognis_active_sessions{agent_id}
cognis_active_delegations
cognis_active_executors{type}
```

### Logging
JSON structured logs with correlation IDs (conversation_id, session_id).

### Graceful Shutdown

On SIGTERM / SIGINT the controller follows this sequence:

1. Stop accepting new WebSocket connections and HTTP requests (return 503).
2. Signal all active agent loops to finish current LLM call (do not start
   new tool calls).
3. Wait up to `shutdown_grace_seconds` (default: 15) for in-flight turns
   to finalize (record events to Intaris, update session cache).
4. For turns that did not finalize in time: attempt best-effort event
   flush to Intaris.
5. Flush Mnemory remember retry queue (bounded timeout: 10s).
6. Mark remaining active sessions as `idle` in Cognis DB.
7. Close executor WebSocket connections.
8. Emit `cognis_shutdown_completed` log event.
9. Exit.

See [03-session-model.md](03-session-model.md) "Session Recovery" for
how the controller handles sessions left in an inconsistent state after
an ungraceful shutdown (crash).

### Production HTTPS
All inter-service communication should use TLS in production. Docker Compose
and K8s examples use internal HTTP for simplicity; add a reverse proxy
(Traefik, nginx) for TLS termination.
