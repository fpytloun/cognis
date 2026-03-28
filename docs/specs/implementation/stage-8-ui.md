# Stage 8: UI

**Status**: NOT STARTED
**Repo**: `cognis` (under `ui/`)
**Depends on**: Stage 7 (API + WebSocket — the UI consumes them)
**Estimated effort**: 5-7 days

## Objective

Build the SvelteKit web application with chat, agent management, and
settings pages. After this stage, a user can interact with Cognis entirely
through the browser: create agents, chat with streaming responses, see
delegation progress, resolve escalations, and configure LLM providers.

## Deliverables

### 1. SvelteKit Project Setup

- `ui/` directory with SvelteKit + TypeScript + Tailwind CSS + shadcn-svelte
- API client module: typed fetch wrapper for all REST endpoints
- WebSocket client module: connection management, auto-reconnect,
  event dispatch
- Auth store: JWT management, login/logout, token refresh
- Router: `/chat`, `/agents`, `/agents/new`, `/agents/:id`, `/settings`

### 2. Chat Page (`/chat`)

- Conversation list sidebar (create, select, archive, delete)
- Chat message area:
  - Streaming markdown rendering (assistant responses)
  - User message input with send
  - Tool call indicators (name, status, duration)
  - Delegation status cards (started, progress, completed)
  - Escalation prompts with approve/deny buttons and countdown timer
  - Queued message indicator
- WebSocket connection:
  - Auto-connect on page load
  - Handle all server message types
  - Reconnection with event replay on disconnect
  - Connection status indicator
- Message history loading (paginated from REST API)

### 3. Agent Management

- `/agents` — agent list with status indicators
- `/agents/new` — agent creation form:
  - Name, handle, description
  - Avatar (upload or placeholder)
  - Personality fields (tone, temperament, behavioral rules)
  - Purpose/role selection
  - Tool permissions
  - LLM config (model selection from available providers)
  - Preview of what will be bootstrapped to Mnemory
- `/agents/:id` — agent detail and edit
- Agent activation/suspension controls

### 4. Task Kanban Board (`/tasks`)

- Kanban columns: Draft, Queued, Running, Paused, Done
- Task cards showing: title, agent, status, priority, workflow, dependency indicator
- Create draft tasks directly on the board
- Drag to reorder priority within a column
- Submit drafts individually or batch-submit
- Set dependencies between tasks (link UI or modal)
- Click task → detail view with workflow step progress
- Filter by agent, priority, workflow, status, or search
- `/tasks/:id` — task detail:
  - Workflow step progress (step list with status indicators)
  - Step output and evaluation results
  - Gate prompts with approve/reject/cancel buttons
  - Dependency graph (which tasks this depends on / depends on this)
  - Result summary when completed

### 5. Workflow Editor (`/workflows`)

- Workflow list (system + user)
- System workflows: read-only, can duplicate
- User workflows: full CRUD
- Form-based step editor:
  - Step list with drag-to-reorder
  - Per step: name, type (run/gate), prompt, inputs, completion config
  - Gate options editor
  - Review loop configuration (on_reject, max iterations)
- Visual pipeline preview
- Export/import as YAML
- Duplicate from existing workflow

### 6. Settings Page (`/settings`)

- Tabbed layout:
  - **LLM Providers**: add/edit/remove providers, test connectivity,
    model catalog display
  - **Model Routing**: configure which model for which task type
  - **Secrets**: add/edit/remove encrypted secrets (values masked)
  - **System**: connection status for Mnemory/Intaris/executors,
    session settings, security settings
  - **Account**: user profile, API keys, password change

### 7. Shared Components

- `ChatMessage` — renders assistant/user/system messages with markdown
- `ToolCallCard` — tool name, args summary, status, duration
- `DelegationCard` — delegation type, progress, result summary
- `EscalationPrompt` — tool details, risk, reasoning, approve/deny, timer
- `AgentAvatar` — avatar display with fallback
- `ProviderStatusBadge` — health indicator for each provider
- `LoadingState` — skeleton/spinner states for async operations

### 8. Cross-Service Links

- "View in Intaris" links on escalation cards and audit references
- "View in Mnemory" links on memory references
- Token exchange: request exchange token from Cognis, open target UI
  with token parameter

## Design Principles

- **Non-blocking**: chat input always available, background work shows
  progress without blocking interaction
- **Progressive disclosure**: simple by default, power features accessible
  but not overwhelming
- **Real-time**: streaming tokens appear as they arrive, delegation status
  updates live, escalation countdowns tick
- **Responsive**: works on desktop and tablet (mobile is not MVP-critical)

## Acceptance Criteria

- [ ] `cd ui && npm install && npm run dev` starts on :5173
- [ ] Login flow works (email + password → JWT → authenticated session)
- [ ] Chat page sends messages and renders streaming responses
- [ ] Tool calls display with status indicators
- [ ] Delegation cards show progress and completed results
- [ ] Escalation prompts appear with approve/deny and countdown
- [ ] Reconnection works (disconnect → reconnect → replay missed events)
- [ ] Agent list shows all agents with status
- [ ] Agent creation form creates agent and bootstraps to Mnemory
- [ ] Agent workflow settings (available workflows, default, step agent overrides)
- [ ] Task kanban board shows tasks across all states (draft through done)
- [ ] Create draft task, edit, set priority, set dependencies
- [ ] Configure delivery target for task results/questions
- [ ] Submit drafts (individual and batch)
- [ ] Task detail shows workflow step progress with status per step
- [ ] Gate prompts appear in task detail with action buttons
- [ ] In-step question prompts appear and resume the same step
- [ ] Workflow list shows system + user workflows
- [ ] Workflow editor: create, edit, duplicate, export/import
- [ ] Settings page manages LLM providers (add, test, remove)
- [ ] Settings page manages secrets (add, edit, remove)
- [ ] Model routing configuration works
- [ ] Cross-service links open Intaris/Mnemory UIs correctly
- [ ] UI handles error states gracefully (provider down, auth expired, etc.)
- [ ] Build produces production bundle: `npm run build`

## Key References

- `docs/specs/09-ui-ux.md` — UX design, WebSocket protocol, kanban, workflow editor
- `docs/specs/14-workflow-engine.md` — workflow engine, task lifecycle, gates
- `docs/specs/10-api-spec.md` — REST + WS message formats
- `docs/specs/07-security-identity.md` — cross-service token exchange
