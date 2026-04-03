# AGENTS.md — Coding Agent Instructions for cognis

## Project Overview

**cognis** is the controller and orchestration layer of the Openclaw ecosystem — a decoupled control plane for AI agents. It manages agent definitions, interactive chat, delegated sub-sessions, tool execution routing, and integrates with external memory (Mnemory) and guardrails/audit (Intaris) services.

- **Language**: Python 3.12+, typed, async-first
- **Framework**: FastAPI (Starlette) for HTTP/WebSocket, Typer for CLI
- **Core dependencies**: fastapi, uvicorn, httpx, pydantic v2, sqlalchemy 2.x, litellm, typer
- **Frontend**: SvelteKit (separate app in `ui/`)
- **License**: TBD
- **Repository**: https://github.com/fpytloun/cognis
- **Part of**: Openclaw ecosystem (Cognis controller, Intaris guardrails, Mnemory memory)

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
│   │   │   └── system.py           # Health, metrics, JWKS
│   │   ├── websocket.py            # WebSocket transport layer (thin adapter)
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
│   │   └── agent_registry.py      # System agent definitions + registry
│   │
│   ├── models/                     # Domain models (Pydantic)
│   │   ├── agent.py
│   │   ├── session.py
│   │   ├── tool.py
│   │   ├── delegation.py
│   │   └── config.py              # LLMProviderConfig, ModelRoutingPolicy, etc.
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
│   │   │   ├── protocol.py        # ExecutorProvider Protocol
│   │   │   ├── in_process.py      # MVP: same-process executor
│   │   │   ├── subprocess.py      # Local subprocess executor
│   │   │   ├── docker.py          # Phase 2
│   │   │   └── kubernetes.py      # Phase 2
│   │   ├── secrets/
│   │   │   ├── protocol.py
│   │   │   └── encrypted_db.py    # AES-256-GCM encrypted secrets
│   │   ├── llm/
│   │   │   ├── protocol.py        # LLMProvider Protocol
│   │   │   └── litellm.py         # LiteLLM wrapper (loads config from DB)
│   │   └── auth/
│   │       ├── protocol.py
│   │       └── jwt.py             # ES256 JWT (auto-generated keys)
│   │
│   ├── tools/                      # Tool system
│   │   ├── builtin/
│   │   │   ├── orchestration.py   # delegate, spawn_worker, fork
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
│   └── cli/                        # Typer CLI commands
│       ├── __init__.py
│       ├── admin.py               # create-user, reset-password, api-key (direct DB)
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
| **CLI** | `cli/` | Typer commands: `serve`, `admin create-user`, `admin reset-password`, `admin api-key`, `config init`, `status` |
| **API Gateway** | `api/` | FastAPI routes, WebSocket transport layer (thin adapter), auth middleware, request/response models |
| **Turn Scheduler** | `core/turn_scheduler.py` | Transport-agnostic turn orchestration: submission, serialization, decision dispatch, follow-up turns, cancellation, error classification |
| **Command Dispatcher** | `core/commands.py` | Transport-agnostic slash command handling: /compact, /new, /model, /thinking, /context, /info, /lsp, /help, /approve, /deny |
| **Orchestration Core** | `core/` | Agent loop, Decision Engine, Session Manager, context assembly, compaction, tool routing, event bus |
| **Session Cache** | `core/session_cache.py` | L1 in-memory cache for Intaris-derived state (events, seq, compaction, intention). No DB persistence. |
| **Remember Queue** | `core/remember_queue.py` | Bounded async retry queue for failed Mnemory remember() calls |
| **Notification Service** | `core/notifications.py` | Unified lifecycle for escalations, gates, and step questions. DB-persistent, PauseWaiter-backed. |
| **Agent Registry** | `core/agent_registry.py` | System agent definitions (Python constants) + registry merging system and DB agents |
| **Domain Models** | `models/` | Pydantic models for agents, sessions, tools, delegations, config |
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

13. **Prompt caching via immutable prefix**: Context is structured with an immutable prefix (system prompt → tool schemas → memory instructions + core memories → compaction summary) followed by a mutable suffix (history → recalled memories → delegations → user message). The immutable prefix benefits from LLM prompt caching (Anthropic `cache_control`, OpenAI automatic prefix caching). Memory instructions and core memories are cached in the session cache for the duration of the session with a 30-minute TTL refresh.

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
uvx cognis                      # Controller on :8080

# Or run directly
uv run python -m cognis serve
```

On first start, Cognis auto-creates `~/.cognis/` with ES256 keys, secrets
encryption key, and SQLite database. A one-time setup URL (15 min TTL) is
printed to stdout for creating the first admin user.

### CLI admin commands

```bash
cognis admin create-user admin@example.com --name "Admin"
cognis admin reset-password admin@example.com
cognis admin api-key create admin@example.com --name "dev-key"
cognis admin api-key list admin@example.com
cognis status                   # Health + provider status (via API)
cognis config init              # Print env var template
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

```bash
# Create a new migration
uv run alembic -c cognis/store/migrations/alembic.ini revision --autogenerate -m "description"

# Apply all migrations
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
| `COGNIS_INITIAL_ADMIN_EMAIL` | — | Container/CI: auto-create admin on first start |
| `COGNIS_INITIAL_ADMIN_PASSWORD` | — | Container/CI: admin password (cleared after use) |

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
| `conversations` | `conversation_id` | Conversation metadata |
| `sessions` | `session_id` | Session metadata (NO event seq/compaction fields) |
| `tasks` | `task_id` | Durable work items (kanban cards, queue items) |
| `task_dependencies` | `(task_id, depends_on)` | DAG edges between tasks |
| `step_runs` | `step_run_id` | Workflow step execution attempts |
| `workflows` | `workflow_id` | Portable workflow templates |
| `schedules` | `schedule_id` | Cron-like task factory |
| `settings` | `key` | System settings (replaces config file) |
| `llm_providers` | `provider_id` | LLM provider configurations |
| `model_routing` | `task_type` | Model routing policy |
| `secrets` | `secret_id` | Encrypted secrets (AES-256-GCM) |
| `notifications` | `notification_id` | Persistent notifications (escalations, gates, step questions) |
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

- Use Alembic for all schema changes
- Migrations live in `cognis/store/migrations/versions/`
- Every migration must be reversible (`upgrade()` and `downgrade()`)
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

**Read the relevant spec before making changes in that area.**

## Important Rules

- **Never execute tool calls in the controller.** All tool execution goes through an executor, even in-process.
- **Never store Intaris-derived state in Cognis DB.** Use the session cache.
- **Never log message content, tool args, memory content, or secrets.**
- **Never use sync I/O in the controller.** Everything is async.
- **Never bypass Intaris for non-bypassable tools.** Even `"*": "allow"` permissions don't skip guardrails for these.
- **Never push to main/master without explicit approval.**
- **Always use `git add -u`** (tracked files only), never `git add -A`.
- **Always follow Conventional Commits** for commit messages.
