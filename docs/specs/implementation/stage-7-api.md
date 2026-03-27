# Stage 7: API + WebSocket

**Status**: NOT STARTED
**Repo**: `cognis`
**Depends on**: Stage 6 (agent loop — the API wires HTTP/WS to the loop)
**Estimated effort**: 3-4 days

## Objective

Implement the full REST API surface and WebSocket handler. After this
stage, external clients (UI, CLI, integrations) can interact with Cognis
through a complete API: manage agents, chat via WebSocket, browse tasks,
resolve escalations, and configure settings.

## Deliverables

### 1. Conversation Routes

- `cognis/api/routes/conversations.py`
  - `GET /api/v1/conversations` — list (paginated, filtered)
  - `POST /api/v1/conversations` — create (with agent_id)
  - `GET /api/v1/conversations/:id` — details
  - `PATCH /api/v1/conversations/:id` — update title, archive
  - `DELETE /api/v1/conversations/:id` — soft delete
  - `DELETE /api/v1/conversations/:id/purge` — hard delete + Intaris cascade
  - `GET /api/v1/conversations/:id/messages` — history (proxied from Intaris events)
  - `GET /api/v1/conversations/:id/sessions` — list sessions
  - `GET /api/v1/conversations/:id/delegations` — active delegations

### 2. Agent Routes

- `cognis/api/routes/agents.py`
  - `GET /api/v1/agents` — list agents
  - `POST /api/v1/agents` — create agent (bootstrap personality to Mnemory)
  - `GET /api/v1/agents/:id` — details
  - `PUT /api/v1/agents/:id` — update
  - `DELETE /api/v1/agents/:id` — archive
  - `POST /api/v1/agents/:id/activate` — activate draft

### 3. Settings Routes

- `cognis/api/routes/settings.py`
  - `GET /api/v1/settings` — all settings grouped by category
  - `GET /api/v1/settings/:key` — single setting
  - `PUT /api/v1/settings/:key` — update (admin only)
  - `GET /api/v1/llm-providers` — list providers
  - `POST /api/v1/llm-providers` — add provider
  - `GET /api/v1/llm-providers/:id` — details + model catalog
  - `PUT /api/v1/llm-providers/:id` — update
  - `DELETE /api/v1/llm-providers/:id` — remove
  - `POST /api/v1/llm-providers/:id/test` — test connectivity
  - `GET /api/v1/model-routing` — current routing policy
  - `PUT /api/v1/model-routing` — update routing (admin only)

### 4. Other Routes

- `cognis/api/routes/secrets.py` — CRUD (values never in response)
- `cognis/api/routes/tools.py` — list tools, agent tools, MCP servers
- `cognis/api/routes/escalations.py` — list pending, resolve (proxied to Intaris)
- `cognis/api/routes/system.py` — health, metrics, JWKS (extend Stage 2)

### 5. API Models

- `cognis/api/models.py` — Pydantic request/response models for all endpoints
  - Consistent pagination: `{items, total, offset, limit}`
  - Error responses: `{error, message, detail}`
  - All models typed and documented

### 6. WebSocket Handler

- `cognis/api/websocket.py`
  - Connection lifecycle: auth → active → disconnect
  - Auth timeout: 10s (from `ws_auth_timeout_seconds`)
  - Client → Server messages:
    - `message` — user message to conversation
    - `cancel` — cancel current turn
    - `resolve_escalation` — approve/deny
    - `reconnect` — replay missed events
    - `ping`
  - Server → Client messages:
    - `chunk` — streaming LLM token
    - `tool_call` — tool execution status
    - `delegation_started/progress/completed`
    - `escalation` — approval needed
    - `escalation_expired`
    - `message_complete` — with seq, token_usage, queued_count
    - `session_recovered`
    - `reconnected` — after event replay
    - `error`
    - `pong`
  - Reconnection: client sends last_seq, server replays from cache/Intaris
  - Message queuing: max 5 queued messages per session, merged on turn complete

### 7. Event Bus

- `cognis/core/events.py`
  - Internal pub/sub for system events
  - Event types: turn_started, turn_completed, delegation_started,
    delegation_completed, escalation_created, escalation_resolved,
    compaction_triggered, session_recovered, etc.
  - Hook registration for plugins
  - WebSocket handler subscribes to push events to clients

## Acceptance Criteria

- [ ] All REST endpoints return correct responses with proper auth
- [ ] Conversation CRUD works end-to-end
- [ ] Agent create bootstraps personality to Mnemory
- [ ] Settings CRUD reads/writes DB settings table
- [ ] LLM provider management + test endpoint works
- [ ] WebSocket chat sends messages and receives streaming responses
- [ ] Delegation events push to client in real-time
- [ ] Escalation events push to client, resolution flows back
- [ ] Reconnection replays missed events correctly
- [ ] Message queuing works (max 5, merged on turn complete)
- [ ] Purge cascades to Intaris (delete session events)
- [ ] Event bus dispatches events to registered handlers
- [ ] Pydantic models validate all inputs/outputs
- [ ] API tests for each route group
- [ ] `ruff check` and `mypy` clean

## Key References

- `docs/specs/10-api-spec.md` — full endpoint specification
- `docs/specs/09-ui-ux.md` — WebSocket protocol, reconnection
- `docs/specs/03-session-model.md` — message queuing, retention/deletion
