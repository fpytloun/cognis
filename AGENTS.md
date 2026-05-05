# AGENTS.md — Coding Agent Instructions for cognis

## Project Overview

**cognis** is a controller and orchestration layer for self-hosted AI agents — a decoupled control plane that manages agent definitions, interactive chat, delegated sub-sessions, tool execution routing, and integrations with external memory (Mnemory) and guardrails/audit (Intaris) services.

- **Language**: Python 3.12+, typed, async-first
- **Framework**: FastAPI (Starlette) for HTTP/WebSocket, Typer for CLI
- **Core dependencies**: fastapi, uvicorn, httpx, pydantic v2, sqlalchemy 2.x, litellm, typer
- **Frontend**: SvelteKit (separate app in `ui/`)
- **License**: TBD
- **Repository**: https://github.com/fpytloun/cognis
- **Companion services**: Intaris guardrails/audit and Mnemory memory

## Architecture

```
cognis/
├── pyproject.toml
├── cognis/
│   ├── __init__.py
│   ├── main.py                     # Entry point (Typer CLI + server start)
│   ├── config.py                   # Env var configuration (no config file)
│   │
│   ├── api/                        # API Gateway
│   │   ├── app.py                  # FastAPI factory
│   │   ├── routes/
│   │   │   ├── auth.py             # Login, refresh, logout, setup, exchange-token
│   │   │   ├── conversations.py
│   │   │   ├── agents.py
│   │   │   ├── secrets.py
│   │   │   ├── settings.py         # System settings, LLM providers, model routing
│   │   │   ├── tasks.py            # Task queue, dependencies, gate/step responses
│   │   │   ├── tools.py
│   │   │   ├── workflows.py        # Workflow CRUD, duplication, import/export
│   │   │   ├── schedules.py        # Schedule CRUD (task factory)
│   │   │   ├── escalations.py
│   │   │   ├── images.py            # Image serving, upload, generation
│   │   │   └── system.py           # Health, metrics, JWKS
│   │   ├── websocket.py            # WebSocket transport layer (thin adapter)
│   │   ├── executor_ws.py          # Executor WebSocket endpoint (auth + configure)
│   │   ├── runtime_support.py      # Step runtime factory (executor resolution)
│   │   ├── middleware.py           # Auth (JWT + API key), rate limiting
│   │   └── models.py              # API request/response Pydantic models
│   │
│   ├── core/                       # Orchestration Core
│   │   ├── turn_scheduler.py      # Turn orchestration (transport-agnostic)
│   │   ├── commands.py            # Slash command dispatch (transport-agnostic)
│   │   ├── agent_loop.py          # Agent loop engine (step runner)
│   │   ├── task_queue.py          # Queue picking, capacity, dependency resolution
│   │   ├── workflow_engine.py     # Workflow orchestration (step sequencing, gates, loops)
│   │   ├── step_evaluator.py      # Semantic step completion evaluation
│   │   ├── decision.py            # Decision Engine (rules + workflow selection)
│   │   ├── session.py             # Session Manager
│   │   ├── session_cache.py       # L1 in-memory cache for Intaris-derived state
│   │   ├── tool_router.py         # Tool routing logic
│   │   ├── compaction.py          # Context compaction (LLM + mechanical fallback)
│   │   ├── context.py             # Context assembly (parallel external fetches)
│   │   ├── events.py              # Event Bus + hooks
│   │   ├── remember_queue.py      # Bounded retry queue for Mnemory remember
│   │   ├── agent_registry.py      # System agent definitions + registry
│   │   └── executor_resolution.py # Executor selection (labels, defaults, tool enablement)
│   │
│   ├── models/                     # Domain models (Pydantic)
│   │   ├── agent.py
│   │   ├── channel.py
│   │   ├── session.py
│   │   ├── tool.py
│   │   ├── delegation.py
│   │   └── config.py              # LLMProviderConfig, ModelRoutingPolicy, ImageGenerationResult, etc.
│   │
│   ├── artifacts/                  # Binary artifact storage (images, etc.)
│   │   ├── __init__.py
│   │   └── store.py               # ArtifactBackend Protocol, FS/S3 backends, ArtifactStore
│   │
│   ├── channels/                   # External messaging adapters + pairing
│   │   ├── protocol.py            # ChannelAdapter protocol + base adapter
│   │   ├── manager.py             # Channel lifecycle + adapter orchestration
│   │   ├── inbound.py             # Channel -> TurnScheduler pipeline
│   │   ├── delivery.py            # EventBus -> channel delivery
│   │   ├── pairing.py             # Sender-initiated remote verification flow
│   │   ├── formatting.py          # Message splitting + markdown stripping
│   │   ├── registry.py            # Channel metadata for setup UI
│   │   └── adapters/              # Signal/Slack/etc. concrete adapters
│   │
│   ├── providers/                  # Provider interfaces + implementations
│   │   ├── base.py                 # Protocol definitions (all 6 providers)
│   │   ├── retry.py               # Shared retry utility (exponential backoff + jitter)
│   │   ├── registry.py
│   │   ├── memory/
│   │   │   ├── protocol.py        # MemoryProvider Protocol
│   │   │   └── mnemory.py         # Mnemory HTTP client (JWT auth)
│   │   ├── guardrails/
│   │   │   ├── protocol.py        # GuardrailsProvider Protocol
│   │   │   └── intaris.py         # Intaris HTTP client (JWT auth)
│   │   ├── executor/
│   │   │   ├── protocol.py        # ExecutorProvider Protocol + re-exports
│   │   │   ├── in_process.py      # In-process executor (same process)
│   │   │   ├── websocket.py       # WebSocket remote executor connection + provider
│   │   │   ├── subprocess.py      # Local subprocess executor (spawns process)
│   │   │   ├── composite.py       # Composite provider (routes by executor_type)
│   │   │   ├── docker.py          # Phase 2 (planned, not yet created)
│   │   │   └── kubernetes.py      # Phase 2 (planned, not yet created)
│   │   ├── secrets/
│   │   │   ├── protocol.py
│   │   │   └── encrypted_db.py    # AES-256-GCM encrypted secrets
│   │   ├── llm/
│   │   │   ├── protocol.py        # LLMProvider Protocol
│   │   │   ├── litellm.py         # LiteLLM wrapper (loads config from DB)
│   │   │   └── inference_router.py # Routes LLM inference to executor-side endpoints
│   │   └── auth/
│   │       ├── protocol.py
│   │       └── jwt.py             # ES256 JWT (auto-generated keys)
│   │
│   ├── tools/                      # Tool system
│   │   ├── builtin/
│   │   │   ├── orchestration.py   # delegate, task/workflow orchestration
│   │   │   └── system.py          # list_agents, get_status
│   │   ├── executor/
│   │   │   ├── lsp/               # LSP diagnostics integration
│   │   │   │   ├── client.py      # Async LSP client (JSON-RPC/stdio)
│   │   │   │   ├── diagnostics.py # Diagnostic formatting for LLM context
│   │   │   │   ├── install.py     # Auto-install strategies
│   │   │   │   ├── manager.py     # LSPManager (lazy spawn, routing)
│   │   │   │   ├── servers.py     # Language server definitions
│   │   │   │   └── types.py       # LSP type definitions
│   │   │   ├── filesystem.py      # read, write, edit, patch, multiedit
│   │   │   ├── search.py          # glob, grep
│   │   │   ├── shell.py           # bash
│   │   │   └── definitions.py     # Tool definitions + handler registry
│   │   ├── mcp.py                  # MCP client
│   │   ├── skills.py
│   │   └── registry.py
│   │
│   ├── store/                      # Cognis DB (metadata only)
│   │   ├── database.py            # SQLAlchemy async engine + session factory
│   │   ├── models.py              # SQLAlchemy ORM models
│   │   ├── queries.py             # Query helpers
│   │   └── migrations/            # Alembic migrations
│   │       ├── env.py
│   │       └── versions/
│   │
│   ├── executor/                    # Standalone executor runner (remote process)
│   │   ├── __init__.py
│   │   ├── __main__.py            # Entry point (python -m cognis.executor)
│   │   ├── runner.py              # ExecutorRunner (WS client, tool dispatch, heartbeat)
│   │   └── inference.py           # InferenceHandler (LiteLLM proxy for controller-routed calls)
│   │
│   └── cli/                        # Typer CLI commands
│       ├── __init__.py
│       ├── admin.py               # create-user, reset-password, api-key (direct DB)
│       ├── executor.py            # executor run (standalone executor process)
│       └── serve.py               # Server start
│
├── ui/                             # SvelteKit frontend (separate app)
│
└── tests/
    ├── unit/                       # Fast, no external services needed
    ├── integration/                # Require running Cognis + providers
    └── contract/                   # Validate Mnemory/Intaris API contracts
```

### Layer responsibilities

| Layer | Directory | Responsibility |
|---|---|---|
| **CLI** | `cli/` | Typer commands: `serve`, `admin create-user`, `admin reset-password`, `admin api-key`, `config init`, `status`, `executor run` |
| **API Gateway** | `api/` | FastAPI routes, WebSocket transport layer (thin adapter), executor WebSocket endpoint, auth middleware, request/response models |
| **Turn Scheduler** | `core/turn_scheduler.py` | Transport-agnostic turn orchestration: submission, serialization, decision dispatch, follow-up turns, cancellation, error classification |
| **Command Dispatcher** | `core/commands.py` | Transport-agnostic slash command handling: /compact, /new, /model, /thinking, /context, /info, /lsp, /help, /approve, /deny |
| **Orchestration Core** | `core/` | Agent loop, Decision Engine, Session Manager, context assembly, compaction, tool routing, event bus |
| **Session Cache** | `core/session_cache.py` | L1 in-memory cache for Intaris-derived state (events, seq, compaction, intention). No DB persistence. |
| **Remember Queue** | `core/remember_queue.py` | Bounded async retry queue for failed Mnemory remember() calls |
| **Notification Service** | `core/notifications.py` | Unified lifecycle for escalations, gates, and step questions. DB-persistent, PauseWaiter-backed. |
| **Agent Registry** | `core/agent_registry.py` | System agent definitions (Python constants) + registry merging system and DB agents |
| **Executor Resolution** | `core/executor_resolution.py` | Executor selection by ID, labels, and default; tool enablement checks |
| **Executor WS Endpoint** | `api/executor_ws.py` | Accepts remote executor WebSocket connections, handles JWT auth and executor.configure handshake |
| **Domain Models** | `models/` | Pydantic models for agents, sessions, tools, delegations, config |
| **Channels** | `channels/` | Channel adapter lifecycle, pairing, inbound routing, outbound delivery, and message formatting |
| **Providers** | `providers/` | Protocol definitions + implementations (memory, guardrails, executor, secrets, LLM, auth) |
| **Tools** | `tools/` | Built-in tools, MCP client, skill loader, tool registry |
| **Storage** | `store/` | SQLAlchemy async engine, ORM models, Alembic migrations, query helpers |
| **Frontend** | `ui/` | SvelteKit app (chat, agents, settings) |
| **Tests** | `tests/` | Unit, integration, and contract tests |

### Key design decisions

1. **Controller = Brain, Executor = Hands**: The controller runs all agent loops, manages LLM interaction, memory, guardrails, and sessions. The executor is a pure tool execution sandbox — it receives `tool.execute` commands via JSON-RPC and returns results. It knows nothing about memory, guardrails, or sessions. **Hard rule: the controller NEVER executes tool calls directly.**

2. **No config file**: Infrastructure config (URLs, ports, keys) uses environment variables. Application config (LLM providers, model routing, session settings, security policies) is stored in the database and managed via the API/UI. There is no `cognis.yaml` or any config file.

3. **Email as user_id**: The user's email is the primary key in the `users` table and flows as `sub` claim in JWTs and `X-User-Id` to Mnemory/Intaris. This is the universal user identifier across the ecosystem.

4. **JWT-only service auth**: Cognis issues ES256 JWTs. Mnemory and Intaris validate them. No API keys between services. JWT `aud` claim prevents confused-deputy attacks.

5. **Session cache, not DB cache**: Intaris-derived state (event sequences, compaction summaries, intention) is cached in-memory (L1) and optionally Redis (L2). It is NOT stored in Cognis DB. Intaris is the single durable source of truth for session content. This eliminates dual-write consistency problems.

6. **Parallel context assembly**: Mnemory recall, Intaris event refresh, and Intaris intention read run concurrently via `asyncio.gather()`. Partial failures degrade gracefully.

7. **Mnemory owns runtime personality**: Cognis bootstraps agent personality into Mnemory on creation. After that, Mnemory is the runtime authority — personality evolves through interactions. No ongoing sync from Cognis to Mnemory.

8. **Workflow-driven execution**: All execution goes through the workflow engine. Main chat is a single-step `direct` workflow. Background tasks use multi-step workflows with planning, evaluation, review loops, and gates. Workflows are portable templates above agents — they define process, not agent identity. See `docs/specs/14-workflow-engine.md`.

9. **Explicit step completion**: A workflow step is not done because the LLM stopped calling tools. The agent must call `step_complete` to signal completion. The controller then runs semantic evaluation before advancing the workflow. This is controller-driven, not model-driven.

10. **Tasks route back into conversations**: Task results, task questions, and task failures are injected as synthetic events into a target conversation. Tasks do NOT speak directly to channels. Channel adapters deliver conversation events to Signal/Slack/web/etc. This keeps all human-facing communication inside the normal conversation/session model.

11. **Follows mnemory/intaris conventions**: Same build tooling (hatchling/uv), config pattern (env vars, no config files), error handling, and code style. Compatible ecosystem.

12. **Compaction creates new sessions**: When context exceeds 85% capacity, compaction creates a new Intaris session within the same conversation. The compacted summary is injected as system context. Manual compaction (`/compact`) defers session creation until the next user message; automatic compaction creates it immediately since the user message is available. The old session is marked completed with `completion_reason="compacted"`.

13. **Prompt caching via immutable prefix**: Context is structured with an immutable prefix (tool schemas → one consolidated first system message containing tagged sections for identity, runtime instructions, memory instructions, core memories, available skills, and continuation summary) followed by a mutable suffix (environment → history → recalled memories → delegations → user message). The immutable prefix benefits from LLM prompt caching (Anthropic `cache_control`, OpenAI automatic prefix caching). Memory instructions and core memories are cached in the session cache for the duration of the session with a 30-minute TTL refresh. Tool schemas are kept stable across turns — the tool exposure layer uses provider-specific mechanisms (`allowed_tools`, `defer_loading`, `tool_search`) to vary tool visibility without changing the cached `tools` array. See `docs/specs/06-tool-system.md` for the full tool exposure architecture.

14. **External channel senders must be verifiable**: Channel accounts should default to `pairing` so unknown remote senders cannot talk to an agent until they redeem a short-lived verification code in the Cognis UI.

15. **Channel adapters may run on executors**: The default location is still the controller, but the target architecture allows a channel account to run on a connected executor when the platform needs user-local services or network reachability (for example Signal via `signal-cli`). The executor reuses the exact same adapter code; the controller does not own the platform-side connection state.

16. **LLM provider executor routing**: LLM providers are configured normally (same UI, same DB table). Setting `location="executor"` on a provider routes inference through a matching remote executor instead of calling the API from the controller. The executor is a transparent LiteLLM proxy — it receives the fully resolved model string and kwargs per-call. `executor_labels` on the provider config selects which executor to use. This means any LiteLLM-supported provider can run on any executor.

## Build / Run / Test

This project uses **uv** for dependency management. All tools (pytest, ruff,
mypy, alembic) run through `uv run` to use the project's `.venv`.

### Local development

```bash
# Install
uv pip install -e ".[dev]"

# Start ecosystem
uvx mnemory                     # Memory layer on :8050
uvx intaris                     # Guardrails on :8060
uvx cognis-controller           # Controller on :8080

# Or run directly
uv run python -m cognis serve
```

On first start, Cognis auto-creates `~/.cognis/` with ES256 keys, secrets
encryption key, and SQLite database. A one-time setup URL (15 min TTL) is
printed to stdout for creating the first admin user.

### CLI admin commands

```bash
cognis-controller admin create-user admin@example.com --name "Admin"
cognis-controller admin reset-password admin@example.com
cognis-controller admin api-key create admin@example.com --name "dev-key"
cognis-controller admin api-key list admin@example.com
cognis-controller status        # Health + provider status (via API)
cognis-controller config init   # Print env var template
```

`admin` commands access the database directly — no API auth needed, but
requires local filesystem access to `COGNIS_DATA_DIR`.

### Tests

```bash
# Unit tests (fast, no external services, default pytest run)
uv run pytest tests/unit/ -v

# Contract tests (require running Mnemory + Intaris with JWT auth)
uv run pytest tests/contract/ -v

# Integration tests (require full running Cognis stack)
uv run pytest tests/integration/ -v

# All tests
uv run pytest -v
```

### Contract tests

Contract tests in `tests/contract/` validate the exact API shapes Cognis
expects from Mnemory and Intaris. They run against real service instances
and catch integration mismatches early.

**When to run:**
- After changing any provider implementation (`providers/memory/`, `providers/guardrails/`)
- After updating Mnemory or Intaris
- Before releases
- In CI (requires running Mnemory + Intaris services)

**Requirements:**
- Mnemory running on `COGNIS_MNEMORY_URL` (default localhost:8050)
- Intaris running on `COGNIS_INTARIS_URL` (default localhost:8060)
- Both services configured with Cognis JWT public key for auth

### Integration tests

Integration tests exercise full user flows: chat → delegation → tool call →
Intaris evaluation → Mnemory recall → result. They require a running Cognis
instance with all providers connected.

**When to run:**
- After changing orchestration core (`core/`)
- After changing the agent loop or delegation logic
- After changing context assembly or compaction
- Before releases

### Linting

```bash
ruff check cognis/ tests/
ruff format cognis/ tests/
```

### Type checking

```bash
mypy cognis/
```

### Database migrations

Cognis uses **two complementary schema evolution mechanisms**:

1. **Bootstrap helpers** (`cognis/bootstrap.py`) — idempotent `_ensure_*` functions that run on every startup via `run_schema_bootstrap()`. These use `ALTER TABLE ADD COLUMN` with column-existence checks. This is the **primary mechanism** that keeps the schema up to date automatically.

2. **Alembic migrations** (`cognis/store/migrations/versions/`) — formal reversible migrations for CI, production rollbacks, and documentation. These are **not run automatically** on startup.

**IMPORTANT: When adding or modifying columns on an existing table, you MUST do BOTH:**
- Create an Alembic migration file in `cognis/store/migrations/versions/`
- Add a corresponding `_ensure_*` function in `cognis/bootstrap.py` and register it in `run_schema_bootstrap()`

If you only create the Alembic migration, the schema change will NOT be applied automatically on startup — the app will crash with `UndefinedColumnError` until someone manually runs `alembic upgrade head`.

```bash
# Create a new migration
uv run alembic -c cognis/store/migrations/alembic.ini revision --autogenerate -m "description"

# Apply all migrations (not needed for normal startup — bootstrap handles it)
uv run alembic -c cognis/store/migrations/alembic.ini upgrade head

# Rollback one migration
uv run alembic -c cognis/store/migrations/alembic.ini downgrade -1
```

## Code Conventions

### Style

- Python 3.12+ features (type unions with `|`, `type` statements where helpful)
- `from __future__ import annotations` in all files
- Type hints on all function signatures and return types
- Docstrings on all public classes and methods
- `logging` module for all output (never `print()`, except CLI user-facing output via Typer)
- f-strings for string formatting
- `async def` for all I/O-bound operations
- Comments and identifiers exclusively in English

### Async conventions

- Use `asyncio` everywhere. No sync I/O in the controller.
- Use `httpx.AsyncClient` for HTTP calls (not `requests`).
- Use `asyncio.gather()` for independent concurrent operations.
- Use `asyncio.Lock` for per-session serialization (not threading locks).
- Use `asyncio.Event` for signaling (not polling loops).
- Provider Protocol methods are all `async def`.

### Error handling

- API routes catch `ValueError` (→ 4xx) and `Exception` (→ 500)
- Provider failures handled per the failure mode table:
  - Mnemory: graceful degradation (continue without memory)
  - Intaris evaluate: **fail-closed** (block tool execution)
  - Intaris event recording: retry, then buffer
  - LLM: retry with fallback model
  - Executor: retry, then inform LLM
  - Secrets: fail-closed
- Internal errors logged with `logger.exception()` for stack traces
- Circuit breaker on all provider calls (5 failures → OPEN → 30s → HALF_OPEN)
- Exponential backoff retry on all provider HTTP calls via shared `providers/retry.py`
- Never let raw exceptions propagate to WebSocket clients — always send structured error messages

### Configuration

- All infrastructure config via environment variables (no config files)
- Application config stored in the `settings` DB table, managed via API/UI
- LLM providers stored in `llm_providers` DB table
- Model routing stored in `model_routing` DB table
- `config.py` reads env vars with sensible defaults for local development
- Auto-generated keys and DB in `~/.cognis/` (configurable via `COGNIS_DATA_DIR`)

### Environment variables

| Variable | Default | Description |
|---|---|---|
| `COGNIS_DATA_DIR` | `~/.cognis` | Data directory (keys, DB, secrets key) |
| `COGNIS_HOST` | `0.0.0.0` | Bind address |
| `COGNIS_PORT` | `8080` | Port |
| `COGNIS_MNEMORY_URL` | `http://localhost:8050` | Mnemory service URL |
| `COGNIS_INTARIS_URL` | `http://localhost:8060` | Intaris service URL |
| `DATABASE_URL` | `sqlite+aiosqlite:///~/.cognis/cognis.db` | Database URL |
| `COGNIS_JWT_PRIVATE_KEY_PATH` | `~/.cognis/keys/private.pem` | JWT private key (auto-generated) |
| `COGNIS_JWT_PUBLIC_KEY_PATH` | `~/.cognis/keys/public.pem` | JWT public key (auto-generated) |
| `COGNIS_SECRETS_KEY_PATH` | `~/.cognis/secrets.key` | AES-256-GCM key (auto-generated) |
| `COGNIS_LOG_LEVEL` | `info` | Log level |
| `COGNIS_LOG_FORMAT` | `json` | Log format (json or text) |
| `COGNIS_CORS_ORIGINS` | `http://localhost:5173` | CORS allowlist |
| `COGNIS_LSP_ENABLED` | `true` | Enable LSP diagnostics after file edits |
| `COGNIS_LSP_AUTO_INSTALL` | `true` | Auto-install missing language servers |
| `COGNIS_LSP_DIAGNOSTICS_TIMEOUT_MS` | `10000` | Max wait for diagnostics (ms) |
| `COGNIS_LSP_IDLE_TIMEOUT_SECONDS` | `600` | Kill idle LSP servers after (s) |
| `COGNIS_LSP_MAX_CONCURRENT_SERVERS` | `8` | Max concurrent LSP server processes |
| `COGNIS_ARTIFACT_BACKEND` | `filesystem` | Artifact store backend (`filesystem` or `s3`) |
| `COGNIS_ARTIFACT_PATH` | `~/.cognis/artifacts` | Filesystem artifact base path |
| `COGNIS_ARTIFACT_S3_ENDPOINT` | `http://localhost:9000` | S3/MinIO endpoint |
| `COGNIS_ARTIFACT_S3_ACCESS_KEY` | — | S3 access key (required for s3) |
| `COGNIS_ARTIFACT_S3_SECRET_KEY` | — | S3 secret key (required for s3) |
| `COGNIS_ARTIFACT_S3_BUCKET` | `cognis-artifacts` | S3 bucket name |
| `COGNIS_ARTIFACT_S3_REGION` | — | S3 region (optional) |
| `COGNIS_ARTIFACT_MAX_SIZE_MB` | `50` | Max artifact size in MB |
| `COGNIS_INITIAL_ADMIN_EMAIL` | — | Container/CI: auto-create admin on first start |
| `COGNIS_INITIAL_ADMIN_PASSWORD` | — | Container/CI: admin password (cleared after use) |
| `COGNIS_CONTROLLER_URL` | — | Executor: controller WebSocket URL (alternative to `--controller-url`) |
| `COGNIS_EXECUTOR_TOKEN` | — | Executor: JWT auth token (alternative to `--token`) |
| `COGNIS_EXECUTOR_WORKDIR` | `~` | Executor: default working directory for tool calls (alternative to `--workdir`) |

### Database

- SQLAlchemy 2.x with async engine (`aiosqlite` for SQLite, `asyncpg` for PostgreSQL)
- Pydantic models for domain logic, SQLAlchemy ORM models for persistence
- Alembic for schema migrations (reversible, tested against both SQLite and PostgreSQL)
- `JSONB` columns use dialect-aware handling: native JSONB on PostgreSQL, JSON (TEXT) on SQLite
- Email as primary key in `users` table — all FKs reference `users(email)`
- Intaris-derived state (event seq, compaction, intention) is NOT in the DB — it lives in session cache

### Database tables (metadata only)

| Table | Primary Key | Purpose |
|---|---|---|
| `users` | `email` | User accounts (email is user_id everywhere) |
| `api_keys` | `key_id` | API keys for programmatic access |
| `agents` | `agent_id` | Agent definitions (primary/secondary types, system flag) |
| `agent_secondary_bindings` | `(primary_agent_id, secondary_agent_id)` | Junction table for primary→secondary agent bindings |
| `agent_grants` | `grant_id` | User-to-user agent sharing grants (polymorphic grantee; user wired, group reserved) |
| `conversations` | `conversation_id` | Conversation metadata |
| `sessions` | `session_id` | Session metadata (NO event seq/compaction fields) |
| `tasks` | `task_id` | Durable work items (kanban cards, queue items) |
| `task_dependencies` | `(task_id, depends_on)` | DAG edges between tasks |
| `step_runs` | `step_run_id` | Current workflow step execution state + attempt counter |
| `deliverables` | `deliverable_id` | Typed step artifacts and final workflow outputs |
| `workflows` | `workflow_id` | Portable workflow templates |
| `schedules` | `schedule_id` | Cron-like task factory |
| `settings` | `key` | System settings (replaces config file) |
| `llm_providers` | `provider_id` | LLM provider configurations |
| `model_routing` | `task_type` | Model routing policy |
| `secrets` | `secret_id` | Encrypted secrets (AES-256-GCM) |
| `executors` | `executor_id` | Executor configurations (type, labels, enabled tools, config) |
| `notifications` | `notification_id` | Persistent notifications (escalations, gates, step questions) |
| `channel_accounts` | `account_id` | Configured external messaging connections |
| `channel_contacts` | `contact_id` | Verified external sender to Cognis user mappings |
| `channel_pairing_requests` | `request_id` | Short-lived remote verification challenges |
| `audit_log` | `log_id` | System-level audit events (NOT session content) |

### Session cache

Intaris-derived state is cached in-memory, NOT in the database:

```python
class SessionCache:
    """L1 in-memory cache for Intaris-derived session state."""
    events: list[IntarisEvent]       # Events since last compaction (append-only)
    last_event_seq: int              # Monotonically increasing
    last_compaction_seq: int         # Updated on compaction
    last_compaction_summary: str     # Updated on compaction
    intention: str | None            # Read-through at turn start
    memory_instructions: str | None  # Cached from first Mnemory recall (30 min TTL)
    core_memories: str | None        # Cached from first Mnemory recall (30 min TTL)
```

- **Events are immutable in Intaris object store** — safe to cache without invalidation
- **Incremental fetch**: only `after_seq=cached_last_seq` on warm cache
- **Controller-triggered invalidation**: controller knows when compaction happens
- **Lost on restart**: rebuilt from Intaris on first session access (cold-start penalty ~1-2s)

### JWT authentication

- Algorithm: ES256 (ECDSA P-256)
- Keys auto-generated in `COGNIS_DATA_DIR/keys/` on first start
- JWKS endpoint at `GET /.well-known/jwks.json`
- User JWT: `sub` = email, `aud` = `["cognis"]`, `role` = user role
- Service JWT (to Mnemory/Intaris): `sub` = user email, `aud` = `["mnemory", "intaris"]`
- Executor JWT: `sub` = executor_id, `aud` = `["cognis-executor"]`, `typ` = `"executor"`. Remote executor tokens do not expire and are revoked by bumping the executor's `token_version`. Short-lived (5 min) tokens are still used for subprocess executors.
- Password hashing: argon2id (`time_cost=3, memory_cost=65536, parallelism=4`)

### Content redaction

Logs and metrics MUST NOT contain:
- Message content (user or assistant)
- Tool call arguments or results
- Memory content (recall or remember payloads)
- Secret values
- Raw LLM prompts or completions

Logs MAY contain: IDs, tool names (not args), model names, token counts,
latencies, status codes, error categories, decision outcomes.

This is enforced by a logging allowlist, not by developer discipline.

### Adding a new provider

1. Define the Protocol in `providers/<category>/protocol.py`
2. Implement the provider in `providers/<category>/<name>.py`
3. Register in `providers/registry.py`
4. Add configuration fields (env vars for infrastructure, DB settings for app config)
5. Add health check method
6. Add circuit breaker wrapping
7. Write contract tests in `tests/contract/`
8. Update this AGENTS.md

### Adding a new API endpoint

1. Add the route in `api/routes/<resource>.py`
2. Add Pydantic request/response models in `api/models.py`
3. Add auth middleware requirements (JWT required, admin-only, etc.)
4. Add the endpoint to `docs/specs/10-api-spec.md`
5. Write unit tests
6. Ensure no content leaks into logs (use the redaction allowlist)

### Adding a new tool

1. Define the tool in `tools/builtin/<category>.py`
2. Register in `tools/registry.py`
3. Set `read_only`, `non_bypassable`, `timeout_seconds` appropriately
4. Tool results are wrapped as untrusted content (XML tags) before LLM injection
5. Large outputs are truncated to `max_result_size` with notice
6. Write unit tests for the tool logic
7. Update `docs/specs/06-tool-system.md`

### Database migrations

- **Every schema change requires BOTH an Alembic migration AND a bootstrap `_ensure_*` function** — see the "Database migrations" section under "Build / Run / Test" for details
- Alembic migrations live in `cognis/store/migrations/versions/`
- Bootstrap helpers live in `cognis/bootstrap.py` → `run_schema_bootstrap()`
- Every Alembic migration must be reversible (`upgrade()` and `downgrade()`)
- Bootstrap `_ensure_*` functions must be idempotent (check column existence before ALTER)
- Test against both SQLite and PostgreSQL
- Never add `last_event_seq`, `last_compaction_*`, or `intention` columns to the `sessions` table — these belong in the session cache

## Data Ownership

| Domain | Owner | Storage |
|---|---|---|
| Users, agents, secrets, system config | **Cognis** | Cognis DB (SQLite / PostgreSQL) |
| Conversation & session metadata | **Cognis** | Cognis DB |
| Session content (messages, tool calls, events) | **Intaris** | Intaris event store (S3 / filesystem) |
| Safety decisions, intention, behavioral analysis | **Intaris** | Intaris DB + event store |
| Persistent memory (facts, personality, recall) | **Mnemory** | Mnemory (Qdrant + artifacts) |

**Cognis DB is metadata only.** Session content lives in Intaris. Persistent
memory lives in Mnemory. Cognis never duplicates their data — it uses
references and caches.

## Specifications

Full architecture and design specifications are in `docs/specs/`:

| File | Content |
|---|---|
| `00-vision.md` | Project vision, design principles, phased delivery |
| `01-architecture.md` | System architecture, DB schema, session cache, package structure |
| `02-agent-model.md` | Agent definitions, personality, delegation, skills |
| `03-session-model.md` | Session model, turn lifecycle, context assembly, recovery, retention |
| `04-controller-executor.md` | Controller-executor separation, JSON-RPC protocol |
| `05-integrations.md` | Mnemory/Intaris/LLM/tool contracts with verified APIs |
| `06-tool-system.md` | Tool routing, permissions, MCP, trust model |
| `07-security-identity.md` | JWT auth, bootstrap, cross-service access, threat model |
| `08-federation.md` | Future federation design (A2A, DID) |
| `09-ui-ux.md` | SvelteKit UI, Typer CLI, WebSocket protocol |
| `10-api-spec.md` | REST + WebSocket API surface |
| `11-deployment.md` | Local/Docker/K8s deployment, env var reference |
| `12-mvp-roadmap.md` | 8-week implementation plan |
| `13-nfr-operations.md` | NFRs, SLOs, metrics, degraded modes, retention |
| `14-workflow-engine.md` | Workflow templates, step types, completion protocol, evaluation, gates |
| `15-browser-credentials.md` | Browser automation, credential records, auth request flows, and cloud-native executor behavior |
| `21-workflow-deliverables.md` | Typed deliverables, `write_deliverable` tool, step_complete gate, once-only channel delivery |
| `22-step-profiles.md` | Step profiles (`unrestricted`/`research`/`coding`), tool classification, per-step overrides |
| `28-agent-sharing.md` | User-to-user agent sharing, `agent_grants`, two-headed runtime identity, Mnemory `(user, owner)` keying, no admin bypass for user-owned resources |
| `30-projects-and-revisions.md` | Projects with multi-source repos and `project_grants`, project-aware tasks/schedules/conversations, path-touch project context injection, step-completion metadata contracts, conditional gate DSL, task comments with intent, and human-as-evaluator revision flow |
| `31-voice-mode.md` | Voice mode (TTS/STT/conversation): `LLMProvider.synthesize()`, `text_to_speech` routing slot, per-agent voice with system fallback, web microphone with iMessage-style record-preview-send, speaker button on assistant messages with cached playback, sentence-buffered TTS streaming, and bidirectional conversation-mode overlay |
| `32-conversation-search.md` | Conversation search: Intaris-owned hybrid index (lexical Tier 1 mandatory: PG `tsvector` + `pg_trgm` / SQLite FTS5 trigram; embedding Tier 2 optional via pgvector or Qdrant URL/local-mode), `INTARIS_SEARCH_ENABLED` feature flag, Cognis proxy + join with conversation metadata, Cmd+F in-conversation search with magnifier and client-first/server-fallback, sidebar promote-to-search with explicit submit, three LLM tools (`list_conversations`, `search_conversations`, `read_conversation_messages` with anchor-based pagination), `conversation_id` in runtime metadata, strict user scoping with no admin bypass and no agent-grant expansion |

## Diagram Style

Public architecture diagrams in `docs/assets/images/` use the Cognis launch dark style. Keep matching editable sources in `docs/assets/diagrams/`.

- Canvas: dark background `#020617`, rounded outer frame when useful.
- Main panels: `#0f172a` or `#111827` with slate strokes `#334155` / `#475569`.
- Primary/controller elements: cyan stroke `#38bdf8`, background `#082f49`.
- Memory elements: green stroke `#10b981`, background `#052e2b`.
- Guardrails/audit elements: purple stroke `#8b5cf6`, background `#22134d`.
- Executor/runtime elements: amber stroke `#f59e0b`, background `#3b2603`.
- Risk/denied/error elements: rose stroke `#fb7185`, background `#4c0519`.
- User/channel/neutral elements: slate stroke `#64748b`, background `#111827`.
- Text: primary `#f8fafc`, muted `#94a3b8`, monospace/details `#bae6fd`.
- Typography: Helvetica for labels, monospace only for protocol/tool names. Use clear hierarchy: 28px title, 20px section heading, 14px labels, 12-13px details.
- Shapes: rounded rectangles, clean SVG-like lines, `roughness: 0`; avoid hand-drawn styling.
- Arrows: cyan for primary flow, slate dashed for retry/revision/secondary flows. Always label non-obvious edges.
- Tone: diagrams should explain boundaries and ownership, not decorate. Prefer fewer boxes with stronger labels over dense exhaustive maps.

**Read the relevant spec before making changes in that area.**

## Important Rules

- **Never execute tool calls in the controller.** All tool execution goes through an executor, even in-process.
- **Never store Intaris-derived state in Cognis DB.** Use the session cache.
- **Workflow deliverables are the canonical step artifacts.** In workflow steps, `write_deliverable` is the authoritative user-facing artifact for evaluation, UI, and final workflow output. Free-text assistant messages in workflow steps are reasoning/progress only. Exception: `system:direct` keeps normal chat-message replies.
- **Never log message content, tool args, memory content, or secrets.**
- **Never use sync I/O in the controller.** Everything is async.
- **Never bypass Intaris for non-bypassable tools.** Even `"*": "allow"` permissions don't skip guardrails for these.
- **No admin bypass for user-owned resources.** The `admin` role grants authority over system settings, providers, users, and audit — not over other users' agents, conversations, tasks, schedules, memories, or secrets. Access to a user-owned agent is through ownership or an explicit `agent_grants` grant, never through role alone. See `docs/specs/28-agent-sharing.md`.
- **Never push to main/master without explicit approval.**
- **Always use `git add -u`** (tracked files only), never `git add -A`.
- **Always follow Conventional Commits** for commit messages.

## Contract and Invariant Hygiene

Stage 20+ refactors exposed several patterns that cost us debugging
cycles. When extending the controller, API, or workflow engine, follow
these rules:

- **One source of truth for controller tool schemas.** Controller-injected
  tools (``step_todo_write``, ``step_complete``, ``step_request_input``,
  etc.) must pull their JSON schema from
  ``cognis/tools/builtin/workflow.py``. Never hand-roll a second schema
  in the agent loop — the LLM-facing and validator-facing schemas must
  be byte-identical.
- **Validate controller tool arguments.** Every controller-intercepted
  tool handler must call ``validate_tool_arguments`` from
  ``cognis.core.tool_arguments`` before mutating state. On failure,
  emit a synthetic ``is_error=True`` tool result so the LLM can
  self-correct; never silently accept ``{"_raw": "..."}``.
- **API response shapes must round-trip every producer.** Add a
  coverage test in ``tests/unit/test_api_contracts.py`` for any new
  response model. The UI TypeScript interface in
  ``ui/src/lib/types/api.ts`` is enforced against the Pydantic model by
  ``tests/unit/test_ui_contract_sync.py``.
- **Lifecycle invariants belong in ``cognis.core.invariants``.** If a
  transition can leak persistent state when a failure path skips it,
  add a checker + reconciler there. The startup sequence and the
  ``/api/v1/system/reconcile`` admin endpoint will pick it up
  automatically.
- **Never clean up step sessions on pause.** ``_cleanup_step_sessions``
  must only run when ``task.status`` is terminal. Pausing the task
  leaves the step session in place so it can resume with its pending
  tool-call context.
