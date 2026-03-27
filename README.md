# cognis

Decoupled control plane for AI agents. Cognis is the controller and orchestration layer of the Openclaw ecosystem -- it manages agent definitions, interactive chat, delegated sub-sessions, tool execution routing, and integrates with external memory and guardrails services.

**Non-blocking.** The main chat is always responsive. Heavy work -- research, coding, multi-step tool calls -- is delegated to background sub-sessions. The user sees real-time progress and can continue chatting.

**Decoupled.** Cognis does not embed memory, guardrails, or session recording. It orchestrates them through pluggable provider interfaces. Swap any component without changing the controller.

**Safe by default.** Every tool call flows through guardrails evaluation. Non-bypassable tools always require safety checks. All actions are audited with full lineage.

**Self-hosted.** Python async controller, SQLite or PostgreSQL, no external dependencies beyond an LLM API key and the companion services. Your agents, conversations, and data stay under your control.

Part of the [Openclaw](https://github.com/openclaw) ecosystem: Cognis controller, [Intaris](https://github.com/openclaw/intaris) guardrails, [Mnemory](https://github.com/openclaw/mnemory) memory.

## Features

- **Interactive chat with streaming** -- WebSocket-based chat with real-time token streaming, tool call indicators, and delegation status cards.
- **Agent identity** -- Create agents with name, personality, behavioral rules, and skills. Personality bootstrapped to Mnemory and evolves through interactions.
- **Sub-session delegation** -- Three modes: Agent (delegate to different agent), Worker (same agent, focused task), Fork (parallel exploration). Main chat stays responsive.
- **Controller-executor separation** -- The controller decides; executors do. In-process executor for local use, Docker and Kubernetes executors for production. Same JSON-RPC protocol everywhere.
- **Memory integration** -- Persistent recall and remember through [Mnemory](https://github.com/openclaw/mnemory). Agent identity, user facts, episodic memory, and artifacts.
- **Guardrails integration** -- Every tool call evaluated by [Intaris](https://github.com/openclaw/intaris). Escalation prompts with approve/deny. Session recording and behavioral analysis.
- **LLM provider abstraction** -- Multi-provider support via LiteLLM. Configure providers and model routing through the UI. Cost tracking per agent and task.
- **MCP tool support** -- Connect any MCP server. Tools discovered automatically, evaluated through guardrails, executed on the executor.
- **Decision Engine** -- Deterministic rules + lightweight LLM classifier decide whether a request runs inline or gets delegated to a background sub-session.
- **Context management** -- Parallel context assembly (Mnemory recall + Intaris events + intention read via `asyncio.gather`). LLM-based compaction with mechanical fallback for long conversations.
- **Web UI** -- SvelteKit application with chat, agent management, and settings. Delegation cards, escalation prompts, and real-time status.
- **CLI** -- Typer-based CLI for server management and administration. Interactive chat in Phase 2.
- **Zero-config local deployment** -- `uvx cognis` with auto-generated keys, SQLite, and sensible defaults. Legitimate single-user setup, not just dev mode.
- **JWT service auth** -- Cognis issues ES256 JWTs. Mnemory and Intaris validate them. No API keys between services.
- **Encrypted secrets** -- AES-256-GCM encrypted secret store for API keys and credentials. Injected into executors at runtime.

## Quick Start

Cognis needs [Mnemory](https://github.com/openclaw/mnemory) and [Intaris](https://github.com/openclaw/intaris) running. Each is a single command:

```bash
uvx mnemory                     # Memory layer on :8050
uvx intaris                     # Guardrails on :8060
uvx cognis                      # Controller on :8080
```

On first start, Cognis creates `~/.cognis/` with auto-generated JWT keys, a secrets encryption key, and a SQLite database. It prints a one-time setup URL to create the first admin user:

```
Cognis started on http://localhost:8080

No users found. Complete setup at:
  http://localhost:8080/setup?token=<random_token>
This link expires in 15 minutes.
```

After creating the admin, log in and configure an LLM provider through **Settings > LLM Providers**.

For headless setup, use the CLI:

```bash
cognis admin create-user admin@example.com --name "Admin"
```

Point Mnemory and Intaris at Cognis's public key for JWT validation:

```bash
# Mnemory
MNEMORY_JWT_PUBLIC_KEY=~/.cognis/keys/public.pem uvx mnemory

# Intaris
INTARIS_JWT_PUBLIC_KEY=~/.cognis/keys/public.pem uvx intaris
```

## Architecture

Cognis is a decoupled control plane. It orchestrates, but does not own, memory or guardrails:

```
                    ┌─────────────┐
                    │  Web UI     │
                    │  (SvelteKit)│
                    └──────┬──────┘
                           │ REST + WebSocket
                    ┌──────▼──────┐
                    │   Cognis    │
                    │ Controller  │
                    └──┬───┬───┬──┘
                       │   │   │
            ┌──────────┘   │   └──────────┐
            ▼              ▼              ▼
      ┌──────────┐  ┌──────────┐  ┌──────────┐
      │ Mnemory  │  │ Intaris  │  │ Executor │
      │ (memory) │  │ (guard)  │  │ (tools)  │
      └──────────┘  └──────────┘  └──────────┘
```

| Data | Owner | Storage |
|------|-------|---------|
| Users, agents, secrets, settings | **Cognis** | Cognis DB (SQLite / PostgreSQL) |
| Conversation & session metadata | **Cognis** | Cognis DB |
| Session content (messages, tool calls) | **Intaris** | Intaris event store |
| Safety decisions, behavioral analysis | **Intaris** | Intaris DB |
| Persistent memory (facts, personality) | **Mnemory** | Mnemory (Qdrant) |

Every major capability is a pluggable provider behind a Python `Protocol` interface:

- `MemoryProvider` -- default: Mnemory
- `GuardrailsProvider` -- default: Intaris
- `ExecutorProvider` -- default: in-process (Docker, K8s in Phase 2)
- `LLMProvider` -- default: LiteLLM
- `SecretsProvider` -- default: encrypted DB
- `AuthProvider` -- default: ES256 JWT

## Configuration

There is **no configuration file**. Infrastructure config uses environment variables. Application config (LLM providers, model routing, session settings) is stored in the database and managed through the UI or API.

### Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `COGNIS_DATA_DIR` | `~/.cognis` | Data directory (keys, DB, secrets) |
| `COGNIS_HOST` | `0.0.0.0` | Bind address |
| `COGNIS_PORT` | `8080` | Port |
| `COGNIS_MNEMORY_URL` | `http://localhost:8050` | Mnemory service URL |
| `COGNIS_INTARIS_URL` | `http://localhost:8060` | Intaris service URL |
| `DATABASE_URL` | `sqlite+aiosqlite:///~/.cognis/cognis.db` | Database URL |
| `COGNIS_LOG_LEVEL` | `info` | Log level |

Auto-generated on first start (override with env vars for production):
- `COGNIS_JWT_PRIVATE_KEY_PATH` -- ES256 private key
- `COGNIS_JWT_PUBLIC_KEY_PATH` -- ES256 public key (share with Mnemory/Intaris)
- `COGNIS_SECRETS_KEY_PATH` -- AES-256-GCM encryption key

## Development

```bash
# Install with dev dependencies
uv pip install -e ".[dev]"

# Run server
uv run cognis serve

# Run tests
uv run pytest tests/unit/ -v          # Unit tests (fast, no services needed)
uv run pytest tests/contract/ -v      # Contract tests (need Mnemory + Intaris)
uv run pytest tests/integration/ -v   # Integration tests (need full stack)

# Lint and type check
ruff check cognis/ tests/
ruff format cognis/ tests/
mypy cognis/
```

## CLI

```bash
cognis serve                            # Start the controller
cognis admin create-user <email>        # Create user (direct DB access)
cognis admin reset-password <email>     # Reset password
cognis admin api-key create <email>     # Create API key
cognis status                           # Health + provider status
cognis config init                      # Print env var template
```

## Roadmap

- **Phase 1 (MVP)** -- Interactive chat, sub-session delegation, Mnemory/Intaris integration, SvelteKit UI, in-process executor
- **Phase 2** -- Multi-agent, task queue + kanban, Docker/K8s executors, chat platform integrations, scheduler
- **Phase 3** -- A2A federation, cryptographic agent identity, multi-tenant production deployment

See [docs/specs/](docs/specs/) for the full specification set and [docs/specs/implementation/](docs/specs/implementation/) for the implementation stage tracker.

## License

Business Source License 1.1, same licensing model as Intaris.

- Free for your own internal business operations, including internal deployment
- Modifications and redistribution allowed when not used commercially
- Converts to Apache License 2.0 on 2030-03-15

See [`LICENSE`](LICENSE) for the full terms.
