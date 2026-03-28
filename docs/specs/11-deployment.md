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
2. Prints a one-time setup URL (15 min TTL) to create the first admin user.
3. Connects to Mnemory and Intaris at default localhost ports.

After creating the admin user via the setup URL, open `http://localhost:8080`
and log in. Configure LLM providers through Settings > LLM Providers in the
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
```

For production: provide your own keys. For local: auto-generation is fine.

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

  cognis-ui:
    build: {context: ./ui}
    ports:
      - "3000:3000"
    environment:
      PUBLIC_COGNIS_API_URL: "https://cognis.example.com"
      PUBLIC_INTARIS_UI_URL: "https://cognis.example.com/intaris"
      PUBLIC_MNEMORY_UI_URL: "https://cognis.example.com/mnemory"
    depends_on: [cognis]

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
    image: ghcr.io/openclaw/mnemory:latest
    # No host ports - only accessible within Docker network

  intaris:
    image: ghcr.io/openclaw/intaris:latest
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

Note: Mnemory and Intaris are NOT exposed to the host. Only the Cognis
controller and UI have host port bindings. The UI uses browser-facing
`PUBLIC_*` origins, so these values should point to externally reachable
URLs (typically the reverse-proxied Cognis/Intaris/Mnemory routes), not
container-internal service names.

### Dockerfile

```dockerfile
FROM python:3.12-slim AS backend
WORKDIR /app
COPY pyproject.toml .
RUN pip install --no-cache-dir .
COPY cognis/ cognis/
EXPOSE 8080
CMD ["python", "-m", "cognis", "serve", "--host", "0.0.0.0"]
```

```dockerfile
# UI
FROM node:20-slim AS ui-build
WORKDIR /app
COPY ui/package*.json ./
RUN npm ci
COPY ui/ .
RUN npm run build

FROM node:20-slim AS ui
WORKDIR /app
COPY --from=ui-build /app/build ./build
COPY --from=ui-build /app/package.json .
EXPOSE 3000
CMD ["node", "build"]
```

## Kubernetes (Phase 2)

```
Namespace: cognis
  Deployment: cognis-controller (replicas: 2+)
  Deployment: cognis-ui (replicas: 2+)
  Service: cognis-controller (ClusterIP)
  Ingress: cognis.example.com
  ServiceAccount: cognis-executor (can create Jobs)

Namespace: cognis-executors
  Job: cognis-exec-{id} (on-demand, created by controller)

Namespace: openclaw
  Deployment: mnemory
  Deployment: intaris
```

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
