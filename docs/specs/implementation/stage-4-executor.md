# Stage 4: Executor + Tools

**Status**: NOT STARTED
**Repo**: `cognis`
**Depends on**: Stage 3 (providers, especially Intaris for tool evaluation)
**Can run in parallel with**: Stage 5
**Estimated effort**: 3-4 days

## Objective

Implement the executor protocol, in-process executor with JSON-RPC bridge,
tool registry, tool router, and built-in tools. After this stage, the
controller can dispatch tool calls to the executor and receive results,
with proper Intaris evaluation on every call.

## Deliverables

### 1. Executor Protocol

- `cognis/providers/executor/protocol.py`
  - `spawn(config: ExecutorConfig) -> ExecutorHandle`
  - `cleanup(handle: ExecutorHandle)`
  - `ExecutorConfig`: tools, model (optional), secrets, controller_url
  - `ExecutorHandle`: send/receive interface

### 2. In-Process Executor

- `cognis/providers/executor/in_process.py`
  - Same-process executor using the JSON-RPC message protocol
  - Executes `tool.execute` commands and returns results
  - Processes `tool.list` to enumerate available tools
  - Secret injection: receives decrypted secrets in config
  - No knowledge of sessions, memory, or guardrails

### 3. JSON-RPC Protocol

- Message types matching `docs/specs/04-controller-executor.md`:
  - `tool.list` → `tool.list.result`
  - `tool.execute` → `tool.execute.result`
  - `tool.execute.progress` (streaming output)
  - `heartbeat` / `heartbeat.ack`
  - `cancel`

### 4. Tool Registry

- `cognis/tools/registry.py`
  - Merge tools from multiple sources:
    - Built-in tools (`tools/builtin/`)
    - MCP servers (via MCP client)
    - Intaris-managed MCP servers
  - Deduplicate by tool name with priority
  - Tool metadata: name, description, schema, risk_level, read_only,
    non_bypassable, timeout_seconds

### 5. Tool Router

- `cognis/core/tool_router.py`
  - Classify tool call → routing category:
    - Orchestration tool (delegate, spawn_worker, fork) → controller handles
    - Intaris-managed MCP tool → Intaris proxy
    - Local tool → Intaris evaluate → executor dispatch
  - Non-bypassable tools always go through Intaris (even if agent has
    `"*": "allow"`)
  - Tool result wrapping: XML tags with `trust="untrusted"`
  - Output size limits: truncate to max_result_size with notice

### 6. Built-In Tools

- `cognis/tools/builtin/orchestration.py`
  - `delegate` — request agent delegation (stub, wired in Stage 6)
  - `spawn_worker` — request worker delegation (stub)
  - `fork` — request fork delegation (stub)
- `cognis/tools/builtin/system.py`
  - `list_agents` — list available agents
  - `get_status` — current session and delegation status

### 7. MCP Client

- `cognis/tools/mcp.py`
  - Connect to configured MCP servers
  - Discover tools via `list_tools`
  - Execute tools via `call_tool`
  - Timeout handling per server

## Acceptance Criteria

- [ ] In-process executor receives and executes tool calls via JSON-RPC
- [ ] Tool registry merges built-in + MCP tools correctly
- [ ] Tool router classifies and dispatches tool calls
- [ ] Non-bypassable tools are always sent to Intaris evaluate
- [ ] Tool results are wrapped with untrusted content tags
- [ ] Large tool outputs are truncated with notice
- [ ] Built-in orchestration tools return proper tool call responses
- [ ] MCP client connects and discovers tools from an MCP server
- [ ] Unit tests for router classification, result wrapping, size limits
- [ ] `ruff check` and `mypy` clean

## Key References

- `docs/specs/04-controller-executor.md` — JSON-RPC protocol, executor model
- `docs/specs/06-tool-system.md` — tool routing, permissions, trust model
- `docs/specs/05-integrations.md` — Intaris evaluate contract
