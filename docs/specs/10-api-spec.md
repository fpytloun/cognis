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
GET    /api/auth/me              → Current user info
POST   /api/v1/auth/exchange-token → Issue short-lived token for Intaris/Mnemory UI access
```

### Conversations

```
GET    /api/conversations                     → List conversations
POST   /api/conversations                     → Create conversation
GET    /api/conversations/:id                 → Get details
PATCH  /api/conversations/:id                 → Update (title, archive)
DELETE /api/conversations/:id                 → Delete
GET    /api/conversations/:id/messages        → Get history (from Intaris events)
GET    /api/conversations/:id/sessions        → List sessions
GET    /api/conversations/:id/delegations     → Active delegations
```

#### Get Messages (proxied from Intaris events)
```http
GET /api/conversations/conv_abc/messages?limit=50&after_seq=100

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

### Agents

```
GET    /api/agents                            → List agents
POST   /api/agents                            → Create agent
GET    /api/agents/:id                        → Get details
PUT    /api/agents/:id                        → Update
DELETE /api/agents/:id                        → Archive
POST   /api/agents/:id/activate              → Activate draft
POST   /api/agents/:id/suspend               → Suspend
POST   /api/agents/:id/sync-personality      → Sync to Mnemory
GET    /api/agents/:id/card                   → A2A Agent Card
```

### Sessions

```
GET    /api/sessions/:id                      → Session details
GET    /api/sessions/:id/events               → Events (proxied from Intaris)
POST   /api/sessions/:id/cancel               → Cancel
```

### Tools

```
GET    /api/tools                             → List all available tools
GET    /api/agents/:id/tools                  → Tools for agent
GET    /api/mcp/servers                       → List MCP servers
```

### Secrets

```
GET    /api/secrets                            → List (metadata only)
POST   /api/secrets                            → Create/update
DELETE /api/secrets/:name                      → Delete
```

### Escalations

Cognis proxies escalation management through Intaris:

```
GET    /api/escalations                       → List pending (via Intaris /audit)
POST   /api/escalations/:call_id/resolve      → Resolve (via Intaris /decision)
```

#### Resolve Escalation
```http
POST /api/escalations/call_abc/resolve
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

### LLM Providers

```
GET    /api/v1/llm-providers                  → List configured providers
POST   /api/v1/llm-providers                  → Add provider (admin only)
GET    /api/v1/llm-providers/:id              → Provider details + model catalog
PUT    /api/v1/llm-providers/:id              → Update provider
DELETE /api/v1/llm-providers/:id              → Remove provider
POST   /api/v1/llm-providers/:id/test        → Test provider connectivity
```

### Model Routing

```
GET    /api/v1/model-routing                  → Current routing policy
PUT    /api/v1/model-routing                  → Update routing policy (admin only)
```

### System

```
GET    /api/health                            → Health check
GET    /api/health/providers                  → Provider status
GET    /api/metrics                           → Prometheus metrics
GET    /.well-known/jwks.json                 → Public keys for JWT validation
GET    /.well-known/agent.json                → Default agent card (A2A)
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
{type: "reconnect", conversation_id, last_seq}     // Reconnect and replay missed events
{type: "ping"}
```

### Server → Client
```typescript
// Streaming
{type: "chunk", conversation_id, session_id, message_id, content, index}
{type: "message_complete", conversation_id, message_id, seq, token_usage, queued_count}

// Tool calls
{type: "tool_call", conversation_id, session_id, call_id, tool_name,
 arguments, status, result_preview?}

// Delegations
{type: "delegation_started", conversation_id, parent_session_id,
 child_session_id, mode, agent_id, task}
{type: "delegation_progress", conversation_id, child_session_id,
 step, progress, token_usage}
{type: "delegation_completed", conversation_id, child_session_id, result}

// Escalations
{type: "escalation", conversation_id, session_id, call_id, tool_name,
 arguments, risk, reasoning, timeout_seconds}
{type: "escalation_expired", call_id}

// Reconnection
{type: "reconnected", conversation_id, missed_events_count}

// Recovery (sent after controller restart if session was affected)
{type: "session_recovered", conversation_id, session_id, reason}

// Errors
{type: "error", conversation_id?, code, message, recoverable}

{type: "pong"}
```

Notes:
- `seq` in `message_complete` is the Intaris event sequence number. The
  client tracks this for reconnection.
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
GET /api/conversations?limit=20&cursor=conv_abc

→ {items: [...], cursor: "conv_next", has_more: true}
```

For events: `after_seq`-based (matches Intaris pagination).
