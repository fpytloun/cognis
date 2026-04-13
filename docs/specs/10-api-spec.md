# Cognis: API Specification

## Overview

Cognis exposes a REST + WebSocket API. All clients use the same surface.
Auto-generated OpenAPI 3.1 spec from FastAPI.

Base URL: `http://localhost:8080/api/v1`

API versioning: all endpoints are prefixed with `/api/v1`. When breaking
changes are needed, a `/api/v2` prefix will be introduced alongside `v1`
with a deprecation period. Non-versioned paths (`/api/health`,
`/api/metrics`, `/api/ws`) remain unversioned.

## Authentication

- `Authorization: Bearer <jwt_token>` — user sessions
- `X-API-Key: <key>` — service accounts

JWT obtained via `POST /api/auth/login`.

WebSocket: authenticate via first message `{type: "auth", token: "..."}`.
Do NOT put tokens in query params.

## REST Endpoints

### Bootstrap (Unauthenticated)

```
GET    /api/bootstrap-status      → Report whether first-run setup is still available
POST   /api/setup                → Create first admin user (one-time, token-gated)
GET    /.well-known/jwks.json    → JWKS public keys for JWT validation
```

The setup endpoint is only available when no users exist. It requires a
one-time token printed to stdout on first start (15 min TTL).

### Auth

```
POST   /api/auth/login           → Authenticate, get JWT (email + password)
POST   /api/auth/refresh         → Refresh JWT
POST   /api/auth/logout          → Invalidate token
POST   /api/auth/change-password → Change current user's password
GET    /api/auth/me              → Current user info
PATCH  /api/auth/me              → Update current user's profile (name)
GET    /api/v1/auth/api-keys     → List current user's API keys
POST   /api/v1/auth/api-keys     → Create current user's API key
DELETE /api/v1/auth/api-keys/:id → Revoke current user's API key
POST   /api/v1/auth/exchange-token → Issue short-lived token for Intaris/Mnemory UI access
```

### Admin User Management

All endpoints require admin role.

```
GET    /api/v1/admin/users              → List users (?include_disabled=true)
POST   /api/v1/admin/users              → Create user (email, name, password, role)
GET    /api/v1/admin/users/:email       → Get user details
PATCH  /api/v1/admin/users/:email       → Update user (name, role)
DELETE /api/v1/admin/users/:email       → Hard delete user + cascade (?confirm=true)
POST   /api/v1/admin/users/:email/disable → Disable user (soft delete)
POST   /api/v1/admin/users/:email/enable  → Re-enable user
```

Safety guards:
- Cannot delete/disable yourself
- Cannot demote/disable/delete the last admin
- Hard delete requires `?confirm=true` query parameter

### Conversations

```
GET    /api/v1/conversations                              → List conversations
POST   /api/v1/conversations                              → Create conversation
POST   /api/v1/conversations/resolve                      → Find-or-create default conversation
GET    /api/v1/conversations/:id                          → Get details
PATCH  /api/v1/conversations/:id                          → Update (title, archive)
DELETE /api/v1/conversations/:id                          → Delete
DELETE /api/v1/conversations/:id/purge                    → Purge metadata (+ Intaris cascade)
POST   /api/v1/conversations/:id/messages                 → Send a chat message (SSE or 202)
GET    /api/v1/conversations/:id/messages                 → Get history (from Intaris events)
GET    /api/v1/conversations/:id/sessions                 → List sessions
GET    /api/v1/conversations/:id/delegations              → Active delegations
GET    /api/v1/conversations/:id/sessions/:sid/events     → Session event stream
```

#### Resolve Conversation (find-or-create)
```http
POST /api/v1/conversations/resolve
{ "agent_id": "aria", "context_type": "web" }

→ 200 OK  (existing or newly created)
{ "conversation_id": "conv_abc", "agent_id": "aria", "context": {...}, ... }
```

Used by the web UI to ensure a persistent default conversation exists for
each agent. If an active conversation matching the (user, agent, context_type)
triple exists, it is returned. Otherwise a new one is created with
`context_ref=web:user:<email>:default`.

For external runtimes, conversation history is a normalized projection over raw
runtime trace plus Cognis overlay events. The REST API still exposes a single
conversation history surface regardless of runtime.

For web conversations, `active_session_id` may be `null` until the first user
message is sent. Sending the first message lazily creates the root session.

#### Get Messages (proxied from Intaris events)
```http
GET /api/v1/conversations/conv_abc/messages?limit=50&after_seq=100

→ 200 OK
{
  "messages": [
    {
      "seq": 101,
      "type": "user_message",
      "content": "Can you research OAuth2?",
      "timestamp": "2026-03-27T10:30:00Z"
    },
    {
      "seq": 102,
      "type": "assistant_message",
      "content": "I'll research that for you...",
      "token_usage": {...},
      "timestamp": "2026-03-27T10:30:05Z"
    }
  ],
  "last_seq": 150,
  "has_more": true
}
```

The controller reads from Intaris event store and formats for the client.

### Channels

```
GET    /api/v1/channels/types                              → Supported channel metadata
GET    /api/v1/channels/accounts                           → List configured channel accounts
POST   /api/v1/channels/accounts                           → Create channel account (defaults to pairing)
GET    /api/v1/channels/accounts/:id                       → Get channel account
PATCH  /api/v1/channels/accounts/:id                       → Update channel account
DELETE /api/v1/channels/accounts/:id                       → Delete channel account
POST   /api/v1/channels/accounts/:id/start                 → Start adapter
POST   /api/v1/channels/accounts/:id/stop                  → Stop adapter
GET    /api/v1/channels/accounts/:id/status                → Runtime adapter status
POST   /api/v1/channels/webhook/:channel_type/:account_id  → Inbound webhook (platform-authenticated)
GET    /api/v1/channels/webhook/:channel_type/:account_id  → Webhook verification challenge
GET    /api/v1/channels/contacts                           → Verified external sender mappings
POST   /api/v1/channels/contacts                           → Manually create verified sender mapping
GET    /api/v1/channels/pairing-requests                   → List pending pairing requests
POST   /api/v1/channels/pair                               → Redeem a sender-initiated pairing code
POST   /api/v1/channels/pairing-requests/:id/reject        → Reject a pending pairing request
```

Channel accounts should default to `pairing` so unknown remote senders cannot
talk to an agent until the authenticated Cognis user redeems their short-lived
pairing code in the web UI.

#### Send Message (REST chat)
```http
POST /api/v1/conversations/conv_abc/messages
Content-Type: application/json
Accept: text/event-stream
{ "content": "What is the weather?" }

→ 200 OK (SSE stream)
event: token
data: {"conversation_id":"conv_abc","session_id":"ses_123","message_id":"msg_abc","delta":"The weather"}

event: tool_call
data: {"conversation_id":"conv_abc","session_id":"ses_123","call_id":"call_1","tool_name":"weather","status":"started"}

event: tool_result
data: {"conversation_id":"conv_abc","session_id":"ses_123","call_id":"call_1","tool_name":"weather","is_error":false,"duration_ms":150}

event: complete
data: {"conversation_id":"conv_abc","session_id":"ses_123","message_id":"msg_abc","last_seq":42,"delegated":false}
```

Supports two delivery modes via the `Accept` header:

- **`Accept: text/event-stream`** — SSE streaming response with real-time
  token deltas, tool calls, and turn completion events. Keepalive comments
  (`: keepalive`) are emitted every 15 seconds to prevent proxy idle
  disconnections.
- **`Accept: application/json`** (default) — fire-and-forget 202 Accepted.
  Poll `GET /conversations/:id/messages?after_seq=N` for the response.

```http
POST /api/v1/conversations/conv_abc/messages
Content-Type: application/json
{ "content": "Hello" }

→ 202 Accepted
{ "status": "accepted" }
```

Slash commands (`/compact`, `/new`, `/model`, etc.) are dispatched through
the `CommandDispatcher` and return their result directly as 200 OK:

```http
POST /api/v1/conversations/conv_abc/messages
{ "content": "/info" }

→ 200 OK
{ "status": "command_executed", "result": {"type": "system_message", "text": "Session: ses_123\n..."} }
```

Error codes: `not_found` (404), `forbidden` (403), `session_ended` /
`session_suspended` (409), `rate_limited` / `queue_full` (429).

### Agents

```
GET    /api/v1/agents                         → List agents
POST   /api/v1/agents                         → Create agent
GET    /api/v1/agents/:id                     → Get details
PUT    /api/v1/agents/:id                     → Update
DELETE /api/v1/agents/:id                     → Archive
POST   /api/v1/agents/:id/activate           → Activate draft
POST   /api/v1/agents/:id/suspend            → Suspend
POST   /api/v1/agents/:id/sync-personality   → Sync to Mnemory
GET    /api/v1/agents/:id/card                → A2A Agent Card (deferred unless public discovery metadata is available)
```

Agent responses also include read-only Mnemory bootstrap fields:
- `personality_synced`
- `personality_sync_error`
- `personality_sync_checked_at`

### Sessions

```
GET    /api/v1/sessions/:id                   → Session details
GET    /api/v1/sessions/:id/events            → Events (proxied from Intaris)
POST   /api/v1/sessions/:id/cancel            → Cancel
```


### Tools

```
GET    /api/v1/tools                          → List static tool catalog (built-in + executor-native + web)
GET    /api/v1/tools/local-mcp/observed       → List cached observed local MCP tools across the user's executors
GET    /api/v1/tools/executor                 → List executor-native tools with status
GET    /api/v1/agents/:id/tools               → Tools for agent (filtered by agent config)
POST   /api/v1/agents/:id/mcp/test           → Test local MCP server discovery for agent
GET    /api/v1/mcp/servers                    → List MCP servers (local + Intaris)
GET    /api/v1/intaris/mcp/servers            → Auto-discover Intaris MCP servers
GET    /api/v1/intaris/mcp/tools              → List all Intaris MCP tools
```

### Skills

```
GET    /api/v1/skills                         → List all skills
POST   /api/v1/skills                         → Create skill
GET    /api/v1/skills/:id                     → Get skill detail
PUT    /api/v1/skills/:id                     → Update skill
DELETE /api/v1/skills/:id                     → Delete skill (DB-managed only)
POST   /api/v1/skills/:id/export             → Export skill as YAML
POST   /api/v1/skills/import                  → Import skill from YAML
```

### Executors

```
GET    /api/v1/executors                       → List executor configurations
GET    /api/v1/executors/:id                   → Get executor configuration
POST   /api/v1/executors                       → Create executor configuration
PUT    /api/v1/executors/:id                   → Update executor configuration
DELETE /api/v1/executors/:id                   → Delete executor configuration
POST   /api/v1/executors/:id/default           → Set as default executor
POST   /api/v1/executors/:id/token             → Generate executor JWT (owner-scoped)
GET    /api/v1/executor/status                 → Executor status and capabilities
GET    /api/v1/tools/executor                  → List executor-native tool definitions
GET    /api/v1/mcp-servers                      → List current user's MCP server configs
GET    /api/v1/mcp-servers/:id                  → Get current user's MCP server config
POST   /api/v1/mcp-servers                      → Create MCP server config (user-scoped)
PUT    /api/v1/mcp-servers/:id                  → Update MCP server config (user-scoped)
DELETE /api/v1/mcp-servers/:id                  → Delete MCP server config (user-scoped, 409 if referenced)
GET    /api/v1/agents/:id/effective-tools       → Effective tool set for saved agent
POST   /api/v1/agents/effective-tools/preview   → Effective tool preview for unsaved agent draft
```

### Secrets

```
GET    /api/v1/secrets                         → List (metadata only)
POST   /api/v1/secrets                         → Create/update
DELETE /api/v1/secrets/:name                   → Delete
```

### Credentials

```
GET    /api/v1/credentials                     → List structured credential records (metadata only)
GET    /api/v1/credentials/:id                 → Get credential metadata
POST   /api/v1/credentials                     → Create/update a structured credential record
POST   /api/v1/credentials/:id/revoke         → Revoke a credential without deleting it
DELETE /api/v1/credentials/:id                → Delete a credential record
```

`secrets` remain the low-level encrypted storage primitive used for provider
keys, MCP env refs, and similar infrastructure concerns. `credentials` are the
agent-facing structured auth records used for browser automation, saved auth
state, and controller-mediated auth flows. Credential payload values are never
returned by the REST API.

### Tasks (Kanban / Work Queue)

```
GET    /api/v1/tasks                              → List tasks (filterable by status, agent, priority, queue)
POST   /api/v1/tasks                              → Create task (draft by default, or queued via source_type=chat)
GET    /api/v1/tasks/:id                          → Task detail + workflow progress + step runs + dependencies + delivery config
PATCH  /api/v1/tasks/:id                          → Update (title, description, priority, agent, workflow, delivery)
DELETE /api/v1/tasks/:id                          → Cancel and remove
POST   /api/v1/tasks/:id/submit                   → Move draft → queued (start execution)
POST   /api/v1/tasks/:id/pause                    → Pause running task
POST   /api/v1/tasks/:id/resume                   → Resume paused task
POST   /api/v1/tasks/:id/cancel                   → Cancel task (any state)
POST   /api/v1/tasks/:id/gate-response            → Respond to a gate step
POST   /api/v1/tasks/:id/step-response            → Respond to `step_request_input` for the current step
GET    /api/v1/notifications                      → List pending notifications (escalations, gates, step questions, credential requests, auth challenges)
POST   /api/v1/notifications/:id/resolve         → Resolve a notification directly
POST   /api/v1/tasks/batch-submit                 → Submit multiple draft tasks at once
GET    /api/v1/tasks/:id/steps                    → List step runs with status and output
GET    /api/v1/step-runs/:id                      → Step run detail (output, evaluation, attempts)
POST   /api/v1/tasks/:id/dependencies             → Add dependency (depends_on task_id, required bool)
DELETE /api/v1/tasks/:id/dependencies/:dep_id     → Remove dependency
```

Task create/update payloads also support:
- `delivery_mode`
- `delivery_target`

These control where task results/questions are routed back (same conversation,
specific conversation, latest active for agent, preferred channel, or silent).

Workflow pauses can now include:

- `credential_request` — request a durable structured credential from the user
- `auth_challenge` — request a live MFA / OTP / push-approval response

Notification resolution for these flows stores only safe metadata and returns
opaque credential IDs or ephemeral credential refs back into the workflow when
needed.

### Schedules (Phase 2, model in MVP)

```
GET    /api/v1/schedules                          → List schedules
POST   /api/v1/schedules                          → Create schedule
PUT    /api/v1/schedules/:id                      → Update schedule
DELETE /api/v1/schedules/:id                      → Delete schedule
POST   /api/v1/schedules/:id/trigger              → Fire schedule immediately (create task)
```

### Escalations

Cognis proxies escalation management through Intaris:

```
GET    /api/v1/escalations                    → List pending (via Intaris /audit)
POST   /api/v1/escalations/:call_id/resolve   → Resolve (via Intaris /decision)
```

#### Resolve Escalation
```http
POST /api/v1/escalations/call_abc/resolve
{"decision": "approve", "note": "Looks safe"}

→ 200 {"call_id": "call_abc", "decision": "approve"}
```

### Settings (App Configuration — No Config File)

All application-level configuration is stored in the database and managed
through these endpoints. Infrastructure config (URLs, keys) uses env vars.

```
GET    /api/v1/settings                       → List all settings (grouped by category)
GET    /api/v1/settings/:key                  → Get single setting
PUT    /api/v1/settings/:key                  → Update setting (admin only)
```

Unknown setting keys are rejected. Known settings validate value types against
the seeded application schema.

### LLM Providers

```
GET    /api/v1/llm-providers                  → List configured providers
POST   /api/v1/llm-providers                  → Add provider (admin only)
GET    /api/v1/llm-providers/:id              → Provider details + model catalog
PUT    /api/v1/llm-providers/:id              → Update provider
DELETE /api/v1/llm-providers/:id              → Remove provider
POST   /api/v1/llm-providers/:id/test        → Test provider connectivity (resolved model, latency, sanitized errors)
```

### Model Routing

```
GET    /api/v1/model-routing                  → Current routing policy
PUT    /api/v1/model-routing                  → Update routing policy (admin only)
```

### Workflows

```
GET    /api/v1/workflows                      → List workflows (system + user)
POST   /api/v1/workflows                      → Create user workflow
GET    /api/v1/workflows/:id                  → Workflow details + steps
PUT    /api/v1/workflows/:id                  → Update workflow
DELETE /api/v1/workflows/:id                  → Delete workflow (user only)
POST   /api/v1/workflows/:id/duplicate        → Duplicate workflow
```

### Workflow Runs

```
GET    /api/v1/tasks/:id/workflow-run         → Current workflow run status + step progress
POST   /api/v1/tasks/:id/gate-response        → Respond to a gate step (approve/revise/cancel)
GET    /api/v1/workflow-runs/:id/steps        → Deferred in MVP; task-backed workflow state uses /api/v1/tasks/:id/steps instead
GET    /api/v1/step-runs/:id                  → Step run detail (output, evaluation, attempts)
```

### System

```
GET    /api/health                            → Health check
GET    /api/health/providers                  → Provider status
GET    /api/v1/system/diagnostics             → Admin diagnostics and readiness summary
GET    /api/metrics                           → Prometheus metrics
GET    /.well-known/jwks.json                 → Public keys for JWT validation
GET    /.well-known/agent.json                → Default agent card (A2A; deferred unless public discovery metadata is configured)
```

## WebSocket API

### Connection
```
WS /api/ws
First message: {type: "auth", token: "<jwt>"}
Auth timeout: 10 seconds (configurable via security.ws_auth_timeout_seconds).
Connections that do not send a valid auth message within the timeout are closed.
```

### Client → Server
```typescript
{type: "message", conversation_id, content}       // User message
{type: "cancel", conversation_id, session_id?}     // Cancel
{type: "resolve_escalation", call_id, decision, note?}
{type: "gate_response", task_id, step_name, action, feedback?}  // Respond to workflow gate
{type: "step_response", task_id?, notification_id?, step_name?, response}  // Respond to step_request_input
{type: "reconnect", conversation_id, last_seq}     // Reconnect and replay missed events
{type: "ping"}
```

The web client sends a heartbeat `ping` roughly every 30 seconds after the
socket authenticates so stalled connections are detected proactively.

### Server → Client
```typescript
// Streaming
{type: "chunk", conversation_id, session_id, message_id, content, index}
{type: "message_complete", conversation_id, message_id, seq, token_usage, queued_count}
{type: "reasoning", conversation_id, session_id, message_id, content}

// Tool calls
{type: "tool_call", conversation_id, session_id, call_id, tool_name,
 arguments?, status}
{type: "tool_result", conversation_id, session_id, call_id, tool_name,
 result, is_error, duration_ms}

// Conversation metadata
{type: "conversation_updated", conversation_id, title?}

// Delegations (sub-session lifecycle)
{type: "delegation_started", conversation_id, parent_session_id,
 child_session_id, mode, agent_id, task}
{type: "delegation_progress", conversation_id, child_session_id,
 step, progress, token_usage}
{type: "delegation_completed", conversation_id, child_session_id, result}
{type: "delegation_failed", conversation_id, child_session_id, reason}

// Escalations
{type: "escalation", conversation_id, session_id, call_id, tool_name,
 arguments, risk, reasoning, timeout_seconds}
{type: "escalation_expired", call_id}

// Workflow progress
{type: "workflow_step_started", task_id, step_name, step_type, attempt}
{type: "workflow_step_completed", task_id, step_name, attempt, output_summary}
{type: "workflow_step_rejected", task_id, step_name, attempt, feedback}
{type: "workflow_step_failed", task_id, step_name, attempt, reason}
{type: "workflow_gate", task_id, step_name, message, options, context}
{type: "workflow_step_question", notification_id, task_id?, step_name, question, options, context}
{type: "workflow_completed", task_id, result}
{type: "workflow_failed", task_id, reason}

// Session lifecycle
{type: "session_compacted", conversation_id, session_id, previous_session_id,
 summary_preview, method, turns_compacted}
{type: "session_reset", conversation_id, session_id, previous_session_id}
{type: "conversation_created", conversation_id, old_conversation_id}
{type: "session_recovered", conversation_id, session_id, reason}

// System messages (slash command feedback, cancel notifications, etc.)
{type: "system_message", conversation_id, text}

// Queue status
{type: "queued", conversation_id, queued_count}

// Reconnection
{type: "reconnected", conversation_id, missed_events_count}

// Auth
{type: "authenticated", user_email, role}

// Escalation resolution
{type: "escalation_resolved", conversation_id, call_id, decision, reason}

// Streaming gap notification
{type: "chunk_gap", conversation_id, dropped_count}

// Workflow cancellation
{type: "workflow_cancelled", conversation_id, task_id, reason}

// Errors
{type: "error", conversation_id?, code, message, recoverable, error_detail?, detail?}

{type: "pong"}
```

Notes:
- `seq` in `message_complete` is the Intaris event sequence number. The
  client tracks this for reconnection.
- `tool_call` events now include `arguments` (optional) when status is `"started"`.
  A separate `tool_result` event delivers the result after execution completes.
  For direct chat, the controller also emits a second `tool_call` with
  `status: "completed"` alongside `tool_result`.
- `reasoning` events carry LLM thinking/reasoning tokens (Anthropic extended
  thinking, OpenAI reasoning content). Streamed incrementally like `chunk`.
- `conversation_updated` notifies clients of metadata changes such as
  auto-generated titles. Fires asynchronously after the first turn.
- Delegation events track sub-session lifecycle. When an agent calls
  `spawn_worker`/`delegate`/`fork`, the controller creates a child
  Intaris session under the parent and spawns a background agent loop.
  `delegation_started` fires immediately, `delegation_completed` or
  `delegation_failed` fires when the child session finishes. The child
  session's result is also recorded as an Intaris event in the parent
  session so the parent agent sees it on the next context assembly.
- On reconnect, the server replays missed events since `last_seq` and
  sends `reconnected` when replay is complete. See
  [09-ui-ux.md](09-ui-ux.md) for the full reconnection protocol.

## Executor WebSocket Protocol

Separate WebSocket endpoint for executor connections:
```
WS /api/executor/ws
```

See [04-controller-executor.md](04-controller-executor.md) for the full
JSON-RPC protocol specification.

## Error Format

```json
{
  "error": {
    "code": "not_found",
    "message": "Conversation not found",
    "details": {"conversation_id": "..."}
  }
}
```

| Code | HTTP | Description |
|------|------|-------------|
| `unauthorized` | 401 | Invalid/expired token |
| `forbidden` | 403 | Insufficient permissions |
| `not_found` | 404 | Resource not found |
| `conflict` | 409 | State conflict |
| `validation_error` | 422 | Invalid request |
| `rate_limited` | 429 | Too many requests |
| `provider_error` | 502 | Upstream provider error |
| `internal_error` | 500 | Server error |

## Pagination

Cursor-based:
```http
GET /api/v1/conversations?limit=20&cursor=conv_abc

→ {items: [...], cursor: "conv_next", has_more: true}
```

For events: `after_seq`-based (matches Intaris pagination).
