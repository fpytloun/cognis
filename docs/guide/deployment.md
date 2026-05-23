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

## Docker controller

Run Cognis with a persistent data volume:

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

Remote executors should connect to `wss://<host>/api/executor/ws`.

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

Cognis runs schema bootstrap on startup for normal deployments. Alembic migrations are kept for formal migration history and rollback workflows.

Before upgrading production:

- back up Cognis, Mnemory, and Intaris data
- review release notes for schema or executor changes
- restart executors after controller upgrades when tool schemas or browser settings changed

## Systemd

Systemd unit templates for the controller and executors live in [`deploy/systemd/`](../../deploy/systemd/). Use these when you want long-running services without Docker.
