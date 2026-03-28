# Stage 7: API + WebSocket

**Status**: DONE
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

### 4. Task Routes

- `cognis/api/routes/tasks.py`
  - `GET /api/v1/tasks` — list (filterable by status, agent, priority, queue)
  - `POST /api/v1/tasks` — create (draft by default, queued for chat delegation)
  - `GET /api/v1/tasks/:id` — detail + workflow progress + step runs + deps
  - `PATCH /api/v1/tasks/:id` — update (title, description, priority, agent, workflow)
  - `POST /api/v1/tasks/:id/submit` — draft → queued
  - `POST /api/v1/tasks/:id/pause` — pause running task
  - `POST /api/v1/tasks/:id/resume` — resume paused task
  - `POST /api/v1/tasks/:id/cancel` — cancel (any state)
  - `POST /api/v1/tasks/:id/gate-response` — respond to gate step
  - `POST /api/v1/tasks/batch-submit` — submit multiple drafts
  - `GET /api/v1/tasks/:id/steps` — list step runs
  - `POST /api/v1/tasks/:id/dependencies` — add dependency
  - `DELETE /api/v1/tasks/:id/dependencies/:dep_id` — remove dependency

### 5. Workflow Routes

- `cognis/api/routes/workflows.py`
  - `GET /api/v1/workflows` — list (system + user)
  - `POST /api/v1/workflows` — create user workflow
  - `GET /api/v1/workflows/:id` — detail + steps
  - `PUT /api/v1/workflows/:id` — update
  - `DELETE /api/v1/workflows/:id` — delete (user only)
  - `POST /api/v1/workflows/:id/duplicate` — duplicate

### 6. Other Routes

- `cognis/api/routes/secrets.py` — CRUD (values never in response)
- `cognis/api/routes/tools.py` — list tools, agent tools, MCP servers
- `cognis/api/routes/escalations.py` — list pending, resolve (proxied to Intaris)
- `cognis/api/routes/system.py` — health, metrics, JWKS (extend Stage 2)

### 7. API Models

- `cognis/api/models.py` — Pydantic request/response models for all endpoints
  - Consistent pagination: `{items, total, offset, limit}`
  - Error responses: `{error, message, detail}`
  - All models typed and documented

### 8. WebSocket Handler

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

### 9. Event Bus

- `cognis/core/events.py`
  - Internal pub/sub for system events
  - Event types: turn_started, turn_completed, delegation_started,
    delegation_completed, escalation_created, escalation_resolved,
    compaction_triggered, session_recovered, etc.
  - Hook registration for plugins
  - WebSocket handler subscribes to push events to clients

## Acceptance Criteria

- [x] REST route groups implemented for conversations, agents, sessions, tasks, workflows, settings, secrets, tools, and escalations
- [x] Conversation CRUD works end-to-end
- [x] Agent create/activate/bootstrap flows integrate with Mnemory bootstrap
- [x] Task CRUD works: create draft, edit, submit, pause, resume, cancel
- [x] Task dependencies: add, remove, DAG validation
- [x] Batch submit works with per-item results
- [x] Task detail shows workflow progress + step runs + pending pause metadata
- [x] Workflow CRUD works: list, create, duplicate, edit, delete
- [x] Settings CRUD reads/writes DB settings table
- [x] LLM provider management and model routing endpoints work
- [x] WebSocket chat authenticates, queues messages, streams responses, and supports reconnect replay
- [x] Workflow progress, gate prompts, and step-question prompts push to clients
- [x] Escalation resolution works through both REST and WebSocket messages
- [x] Gate response via WebSocket and REST both work
- [x] Reconnection replays missed persisted events and pending prompts
- [x] Message queuing works (max 5, merged on turn complete)
- [x] Event bus dispatches events to registered handlers
- [x] Pydantic API models validate inputs/outputs
- [x] Added Stage 7 unit/API/WebSocket tests
- [x] `ruff check` and `mypy` clean

## Notes / MVP Deviations

- All entity routes were normalized to `/api/v1/...` for consistency.
- `DELETE /api/v1/conversations/:id/purge` reports whether an Intaris cascade was possible via `intaris_cascade`; when the provider contract lacks a verified delete-session API, the purge is Cognis-metadata-only.
- `/api/v1/workflow-runs/:id/steps` remains deferred in MVP because workflow-run state is task-backed (`tasks.workflow_state`) rather than stored as a separate workflow-run entity.
- `GET /api/v1/agents/:id/card` and `GET /.well-known/agent.json` are explicitly deferred until the agent model carries sufficient public discovery metadata.

## Key References

- `docs/specs/10-api-spec.md` — full endpoint specification
- `docs/specs/14-workflow-engine.md` — workflow engine, task lifecycle
- `docs/specs/09-ui-ux.md` — WebSocket protocol, reconnection, kanban
- `docs/specs/03-session-model.md` — message queuing, retention/deletion
