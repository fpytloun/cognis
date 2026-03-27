# Cognis: UI and User Experience

## Overview

Cognis has two primary interfaces: a **web UI** (SvelteKit) and a **CLI**
(Typer). Both are API-driven — they consume the same REST + WebSocket API.
End-user friendly — creating agents, chatting, managing work should not
require technical expertise.

Key UX principles:
- **Non-blocking** — main chat always responsive, background work shows progress
- **Progressive disclosure** — simple by default, power features on demand
- **Agent-as-character** — creating an agent feels like a character creator
- **Real-time** — streaming responses, live delegation status

## Architecture

```
SvelteKit Application
  ├── Pages: /chat, /agents, /agents/new, /settings, /tasks (Phase 2)
  ├── Components: ChatMessage, DelegStatus, ToolCall, AgentWizard, etc.
  ├── Stores (Svelte): authStore, chatStore, agentStore, wsStore
  └── API Client: REST (fetch) + WebSocket
        │
        ├── REST API → Cognis Controller
        └── WebSocket → Cognis Controller (chat + events)
```

## Pages

| Route | Purpose | Phase |
|-------|---------|-------|
| `/` | Redirect to active conversation | MVP |
| `/chat/:id` | Chat conversation view | MVP |
| `/chat/new` | Start new conversation | MVP |
| `/agents` | Agent list / gallery | MVP |
| `/agents/new` | Agent creation wizard | MVP |
| `/agents/:id` | Agent detail / edit | MVP |
| `/settings` | Secrets, connections, config | MVP |
| `/tasks` | Kanban board | Phase 2 |

## Chat Interface

Key UI elements:

### Streaming Responses
Tokens stream in real-time via WebSocket. Markdown renders progressively.
Code blocks get syntax highlighting as they form.

### Delegation Status Cards
Inline cards showing background work:
```
[>] Research: OAuth2 best practices           [Expand]
    Agent: Research Worker | Status: Working (45s)
    Steps: 3/? | Tokens: 12,400
```

Expandable to show progress steps and tool calls.

### Tool Call Indicators
```
[wrench] read_file("src/auth.py")  Approved | 0.3s
```

### Escalation Prompts
When Intaris escalates:
```
[!] Approval Required
Agent wants: shell("rm -rf /tmp/build")
Risk: High | Reason: Destructive command
[Approve] [Deny] [Approve + Note]
```

Escalation timeout shown as countdown. Auto-denied on timeout.

### Message Queuing Indicator
When the user types while a turn processes:
```
[Processing...] Your message is queued (1 pending)
```

## Agent Creation Wizard

5-step wizard:
1. **Identity** — name, ID, description, avatar (generate/upload)
2. **Personality** — traits (checkboxes), style, expertise, system prompt
3. **Skills & Tools** — MCP servers, skills, delegation permissions
4. **LLM Config** — provider selection (shows available providers + model catalog
   with capabilities, tier, cost), model override, temperature, reasoning effort
5. **Review & Create** — summary, create button

System prompt auto-generated from personality selections, editable for power
users. Model selection shows provider catalog with model capabilities, tier
labels, and cost estimates.

## Settings Page

- **Profile**: name, email
- **Secrets**: CRUD for API keys and tokens (values masked)
- **Connections**: Mnemory/Intaris status, test connectivity
- **LLM Providers**: list of configured providers with status, model catalog,
  and test availability. For OAuth-based providers (ChatGPT): "Connect"
  button → OAuth redirect flow → "Connected" status with re-auth option
- **Model Routing**: system-wide routing policy (classifier, compaction models)

## Real-Time Communication

### WebSocket Protocol (Client ↔ Controller)

Client → Server:
```typescript
{type: "message", conversation_id: "...", content: "..."}
{type: "cancel", conversation_id: "...", session_id?: "..."}
{type: "resolve_escalation", call_id: "...", decision: "...", note?: "..."}
{type: "reconnect", conversation_id: "...", last_seq: number}
{type: "ping"}
```

Server → Client:
```typescript
{type: "chunk", conversation_id, session_id, message_id, content, index}
{type: "tool_call", conversation_id, session_id, call_id, tool_name, status}
{type: "delegation_started", conversation_id, child_session_id, mode, task}
{type: "delegation_progress", conversation_id, child_session_id, step, progress}
{type: "delegation_completed", conversation_id, child_session_id, result}
{type: "escalation", conversation_id, call_id, tool_name, risk, reasoning, timeout}
{type: "escalation_expired", call_id}
{type: "message_complete", conversation_id, message_id, token_usage, queued_count}
{type: "session_recovered", conversation_id, session_id, reason}
{type: "reconnected", conversation_id, missed_events_count}
{type: "error", conversation_id, code, message, recoverable}
{type: "pong"}
```

### WebSocket Reconnection Protocol

When the client loses its WebSocket connection (network hiccup, tab sleep,
etc.), it should reconnect and recover missed events:

1. Client reconnects WebSocket and re-authenticates (first-message auth).
2. Client sends `{type: "reconnect", conversation_id, last_seq}` where
   `last_seq` is the sequence number of the last event it received.
3. Server reads missed events from the session cache (or Intaris if cache
   is cold) using `after_seq=last_seq`.
4. Server replays missed events as normal WebSocket messages
   (delegation_completed, tool_call, chunk, etc.).
5. Server sends `{type: "reconnected", conversation_id,
   missed_events_count}` when replay is complete.
6. Normal real-time event streaming resumes.

Client responsibilities:
- Track `last_seq` from `message_complete` events.
- Reconnect with exponential backoff (1s, 2s, 4s, max 30s).
- Deduplicate events by `message_id` (in case of overlap).
- If `missed_events_count` is high, consider refreshing full state via
  REST API instead.

## Tech Stack

| Technology | Purpose |
|------------|---------|
| SvelteKit | App framework |
| TypeScript | Type safety |
| Tailwind CSS | Styling |
| shadcn-svelte | UI components |
| Marked/remark | Markdown rendering |
| Shiki | Syntax highlighting |

## Task Kanban (Phase 2)

Three columns: Queued, In Progress, Completed. Each card shows agent, cost,
duration, progress bar. Cards are draggable for reprioritization.

## Platform Integrations (Phase 2)

Slack/Discord bot setup page: workspace connection, channel-to-agent mapping,
thread behavior configuration.

## Development

```bash
cd ui && npm install && npm run dev    # http://localhost:5173
# Proxies API calls to cognis backend at http://localhost:8080
```

## CLI Interface

### Overview

The Cognis CLI is built with **Typer** (typed, auto-generated help, built
on Click). It provides both server management and API access from the
terminal.

### CLI Commands

#### Server and Admin (MVP)

```bash
cognis                              # Start the controller server
cognis serve                        # Explicit server start (same as above)
cognis admin create-user <email>    # Create user (direct DB, local only)
cognis admin reset-password <email> # Reset password (direct DB, local only)
cognis admin api-key create <email> # Create API key (direct DB)
cognis admin api-key list <email>   # List API keys
cognis config init                  # Print default env var template to stdout
```

#### API Commands (MVP)

These commands talk to the Cognis REST API (require JWT or API key):

```bash
cognis agent list                   # List agents
cognis agent create <name>          # Create agent (interactive prompts)
cognis agent show <agent_id>        # Show agent details
cognis secret set <name>            # Set a secret (prompts for value)
cognis secret list                  # List secrets (metadata only)
cognis status                       # Health + provider status
```

#### Interactive Chat (Phase 2)

```bash
cognis chat                         # Chat with default agent
cognis chat --agent <agent_id>      # Chat with specific agent
```

Terminal chat will support streaming responses, markdown rendering, tool
call indicators, and delegation status. Phase 2 because it adds significant
terminal rendering complexity.

### Design Principles

- `cognis admin` commands bypass the API and access the database directly.
  They require local filesystem access to `COGNIS_DATA_DIR`. These are for
  bootstrapping and emergency administration.
- All other commands go through the REST API with standard authentication.
- Authentication: CLI reads JWT from `~/.cognis/token` (saved on login) or
  uses `COGNIS_API_KEY` environment variable.

```bash
cognis login                        # Authenticate and save JWT
cognis logout                       # Remove saved JWT
```

## Delivery Priority

For MVP, ship in order: CLI admin commands → chat page → agent CRUD →
settings. If timeline slips, a working chat page with CLI admin bootstrap
is the minimum viable deliverable.
