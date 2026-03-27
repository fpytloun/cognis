# Cognis: Project Vision

> Decoupled control plane for the agentic ecosystem.

## Overview

Cognis is the controller layer of the Openclaw ecosystem — a platform for
building, orchestrating, and managing autonomous AI agents. It is designed as a
**decoupled control plane** where every major concern is handled by a pluggable
provider behind a clear API contract.

The Openclaw ecosystem consists of three core services:

| Service | Role | Owns |
|---------|------|------|
| **Mnemory** | Memory layer | Persistent agent/user memory, recall sessions, artifact storage, semantic search |
| **Intaris** | Guardrails, audit & session recording | Safety evaluation, intention tracking, session event storage, behavioral analysis |
| **Cognis** | Controller | Agent orchestration, agent loops, LLM interaction, task delegation, UI, platform integrations |

Cognis does not embed memory, guardrails, or session recording — it
**orchestrates** them. This separation means each concern can evolve
independently, be replaced with alternative implementations, or be operated by
different teams.

### Data Ownership

| Domain | Owner | Storage |
|--------|-------|---------|
| Users, agents, secrets, system config | **Cognis** | Cognis DB (SQLite / PostgreSQL) |
| Conversation & session metadata | **Cognis** | Cognis DB |
| Session content (messages, tool calls, events) | **Intaris** | Intaris event store (S3 / filesystem) |
| Safety decisions, intention, behavioral analysis | **Intaris** | Intaris DB + event store |
| Persistent memory (facts, personality, recall) | **Mnemory** | Mnemory (Qdrant + artifacts) |

## Design Principles

### 1. Everything is a Plugin

Every major capability is a provider behind a Python `Protocol` interface:

- `MemoryProvider` — default: Mnemory
- `GuardrailsProvider` — default: Intaris (safety + session recording)
- `ExecutorProvider` — default: built-in (in-process, Docker, K8s)
- `SecretsProvider` — default: encrypted DB
- `LLMProvider` — default: LiteLLM
- `AuthProvider` — default: built-in JWT

If someone wants to replace the executor with Opencode, or swap memory for
their own system, they implement the provider interface. No other code changes.

### 2. Controller = Brain, Executor = Hands

The controller runs agent loops, manages LLM interaction, injects memory
context, evaluates tool calls through guardrails, and manages sessions. The
executor is a **tool execution sandbox** — it receives tool calls and returns
results. It does not know about memory, guardrails, or sessions.

**Hard rule: the controller NEVER executes tool calls.** All tool execution
goes through an executor, always, even in development (in-process executor).
This ensures a consistent trust boundary and code path across all environments.

The executor can optionally provide **local LLM inference** for agents using
local models (e.g., ollama on a Mac Studio). The controller routes LLM calls
to either a cloud provider (direct via LiteLLM) or to the executor (for local
models) based on agent configuration.

### 3. System Decides, LLM Requests

Orchestration decisions are made by **deterministic system logic**. The LLM
focuses on *what* to do; the system decides *how* to orchestrate it.

The LLM has delegation tools (`delegate`, `spawn_worker`, `fork`) that submit
**requests** to the Decision Engine. The system approves, modifies, or rejects
these. Predictable behavior regardless of model capability.

### 4. Non-Blocking by Default

The main chat is always available and responsive. Work requiring extended
thinking, research, or tool calls is delegated to a sub-session. The user sees
real-time progress and can continue chatting.

### 5. Standards Over Proprietary

- **MCP** for tool integration
- **A2A** for cross-agent federation
- **JSON-RPC** for controller-executor communication
- **JWT** for service-to-service authentication
- **OpenAPI** for REST API specification

### 6. Design for the Future

Designed for a world where LLMs are significantly more capable and autonomous:
stronger guardrails (Intaris), verifiable agent identity (DID-ready),
cryptographic signing for cross-agent interaction, and federation protocols for
agents across organizational boundaries.

## Use Cases

### Interactive Chat with Persistent Identity

A user chats with an agent that has a name, personality, behavioral patterns,
and long-term memory. The agent feels "alive" — it remembers past
conversations, develops its identity over time, and adapts to the user's
preferences. Identity and memory in Mnemory; all interactions audited through
Intaris.

### Task Delegation and Autonomous Work

A user describes a task. The system detects this requires delegation, spawns a
sub-session with a worker or specialized agent, and the main chat remains
available. The user sees progress updates. Results flow back to the main
conversation.

### Multi-Context Agent Presence

An agent can be present in multiple contexts simultaneously: a web UI
conversation, a Slack channel, a Discord server, a scheduled task. Each
context has its own conversation scope; the agent shares long-term memory and
identity across all contexts.

### Managed Work Queue (Phase 2)

Users submit work items to a task queue, visualized as a kanban board.
Multiple tasks execute in parallel on different executors.

### Scheduled and Hook-Driven Execution (Phase 2)

Agents triggered by schedules (cron) or hooks (webhooks, events).

### Cross-Agent Federation (Phase 3)

Agents from different platforms discover each other via Agent Cards, establish
trust via cryptographic identity, and delegate tasks across boundaries using
the A2A protocol.

## Phased Delivery

### Phase 1 — MVP: Interactive Chat + Sub-Sessions

- Single user, single primary agent
- Interactive chat with streaming responses (non-blocking)
- Sub-session delegation: Agent, Worker, Fork modes
- Controller runs all agent loops; executor handles all tool calls
- Mnemory integration (recall/remember)
- Intaris integration (evaluate/reasoning/session event recording)
- LiteLLM for multi-provider LLM support
- MCP tool support (local + Intaris-managed remote)
- Basic SvelteKit web UI (chat + agent configuration)
- JWT service-to-service auth with audience claims
- Encrypted secrets store
- In-process executor (MVP; same interface as remote)
- SQLite for Cognis metadata
- Decision Engine (rules + LLM classifier)

### Phase 2 — Multi-Agent, Tasks, Platform Integrations

- Multi-agent with agent registry and management
- Task queue with concurrency control + kanban UI
- Agent wizard (character creator with avatar generation)
- PostgreSQL for production
- Docker and Kubernetes executor support
- Chat platform integrations (Slack, Discord)
- Scheduler and cron execution
- Node groups with label selectors for executors
- Cost tracking and usage analytics

### Phase 3 — Federation and Scale

- A2A federation with Agent Cards
- DID-ready cryptographic agent identity
- Agent discovery and marketplace
- Multi-tenant production deployment
- Edge executor support
- Service decomposition (cognis-api, cognis-orchestrator, cognis-inference)

## Tech Stack

| Layer | Technology | Rationale |
|-------|------------|-----------|
| Backend language | Python 3.12+, typed, async | Consistent with ecosystem |
| Web framework | FastAPI (Starlette) | Async-native, OpenAPI |
| Frontend | SvelteKit (separate app) | Fast, light, excellent DX |
| LLM abstraction | LiteLLM | Multi-provider, cost tracking |
| DB (dev) | SQLite (WAL mode) | Zero-dependency local dev |
| DB (prod) | PostgreSQL | Multi-user, concurrent |
| Executor protocol | WebSocket + JSON-RPC | Bidirectional, real-time |
| Agent storage | Database with UI editor | Character creator UX, export/import |
| Configuration | Env vars (infra) + DB/API (app) | No config file; settings managed via UI/API |
| Tool protocol | MCP | Industry standard |
| Federation protocol | A2A (Phase 3) | Industry standard |
| Service auth | JWT with audience claims | Cognis issues, services validate |
| Secrets | Pluggable (encrypted DB default) | Swappable for Vault, AWS SM |
