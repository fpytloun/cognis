# Creating Agents

Open `Agents` to create or edit agents. An agent defines how Cognis should behave, what tools it can use, which model it prefers, and which workflows it may run.

## Key sections

### Identity

- agent ID
- name / display name
- description
- avatar URL

These fields make the agent recognizable in the UI and help you distinguish between general-purpose, specialist, and background agents.

### Personality

- tone
- temperament
- purpose
- behavioral rules
- system prompt

The runtime identity is composed in two layers:

- `tone`, `temperament`, `purpose`, and `behavioral rules` form the core identity
- `system prompt` adds free-form instructions on top of that core

The editor shows a **System prompt preview** so you can see the exact combined
system message the LLM will receive.

If you use **Sync personality**, Cognis re-bootstraps the structured
personality fields to Mnemory as the evolution seed. This can override evolved
identity in Mnemory, so the UI asks for confirmation first.

For a first agent, keep personality instructions short and practical. Add more constraints only when you know the agent needs them.

### Tools and permissions

- executor binding (specific executor or label selector)
- inherited tool categories from the selected executor
- category and per-tool disable switches
- per-tool permission policy (`allow`, `evaluate`, `deny`)
- allowed secrets
- delegation permission
- max delegation depth

Agents inherit the tool pool from their selected executor. The effective tool
set is:

1. tools enabled on the executor
2. minus categories or individual tools disabled on the agent
3. then filtered by per-tool permission policy for guardrails behavior

If you leave the executor empty, Cognis resolves it from the agent's label
selector or the system default executor.

This means agent safety is shaped by both executor capabilities and the agent's own restrictions.

### MCP servers

Global MCP servers are managed from **Settings → Tools**, then assigned to
executors from **Settings → Executors**. Agents do not own MCP server process
config anymore — they inherit MCP tools from the executor they run on.

Legacy inline MCP entries may still appear on older agents with:

- name
- command
- arguments
- environment variables
- timeout

Use **Settings → Tools** to create, edit, and test configured MCP servers.
Use **Settings → Executors** to attach those MCP servers to a specific
executor. Agent-side tool toggles then control whether the agent can use the
MCP tool categories provided by that executor.

### LLM configuration

- provider selection
- model override
- temperature
- max tokens

Use overrides only when the agent really needs different behavior from the default routing policy.

### Workflow settings

- available workflows
- default workflow
- selection mode
- step agent overrides

Workflow settings matter most when the agent should do structured background work instead of only direct chat responses.
