# Cognis: Tool System

## Overview

Tools are how agents interact with the world. The controller's **Tool Router**
categorizes each tool call and routes it to the appropriate handler. The
executor handles all actual tool execution — the controller never runs tools.

For runtime-backed agents, tool handling is part of the runtime contract. See
`18-runtime-contract.md` for the shared policy model and runtime translation
rules.

## Tool Sources

Tools come from five sources, each with a priority for deduplication:

| Source | Priority | Description |
|--------|----------|-------------|
| `builtin` | 500 | Controller-side tools (orchestration, system, workflow directives) |
| `executor` | 400 | Executor-native tools (filesystem, search, shell, web). Always available. |
| `skill` | 300 | Skill-provided tools (DB-managed instruction + tool bundles) |
| `local_mcp` | 200 | Per-agent MCP servers running on the executor |
| `intaris_mcp` | 100 | Remote MCP servers proxied through Intaris |

When the same tool name appears from multiple sources, the higher-priority
source wins. Same-source duplicates are configuration errors.

## Tool Categories and Routing

```
LLM returns tool calls
  │
  ├─ Orchestration (delegate, task/workflow control)
  │    → Controller handles as session management operation
  │    → Not "tool execution" — these are orchestration directives
  │
  ├─ Intaris-managed MCP (github/, slack/, remote APIs)
  │    → Controller calls Intaris MCP proxy
  │    → Intaris evaluates + executes in one call
  │    → Executor NOT involved
  │
  ├─ Executor-native (read, write, edit, bash, glob, grep, etc.)
  │    → Controller evaluates via Intaris
  │    → If approved: Controller sends tool.execute to Executor
  │    → Executor runs native handler, returns result
  │
  └─ Local MCP (per-agent MCP servers)
       → Controller evaluates via Intaris
       → If approved: Controller sends tool.execute to Executor
       → Executor dispatches to MCP server, returns result
```

## Tool Definition

```python
class ToolDefinition(BaseModel):
    name: str                          # Unique identifier
    description: str                   # For LLM tool selection
    parameters: dict                   # JSON Schema
    source: ToolSource
    category: str = "general"
    read_only: bool = False
    requires_secrets: list[str] = []
    timeout_seconds: int = 30
    non_bypassable: bool = False       # If true, ALWAYS goes through guardrails
    max_result_size: int = 50_000

class ToolSource(BaseModel):
    type: str                          # "builtin", "executor", "local_mcp", "intaris_mcp", "skill"
    server_name: str | None = None     # MCP server display name
    server_id: str | None = None       # MCP server stable ID (for stable_tool_id)
    raw_tool_name: str | None = None   # Original MCP tool name (for dispatch)
    skill_id: str | None = None
```

## Executor-Native Tools

The executor ships with built-in tools that are always available without
MCP server configuration. These tools execute directly in the executor
process — no external process management or MCP protocol overhead.

Executor-native tools are **available to all agents by default** (opt-out
model). Agents can exclude specific tools via their permission matrix
(`"deny"`) or by removing them from `builtin_tools`.

### Filesystem Tools

```python
# read — Read file or directory contents
read_tool = ToolDefinition(
    name="read",
    description="Read a file or directory from the filesystem.",
    parameters={
        "type": "object",
        "properties": {
            "file_path": {"type": "string", "description": "Absolute path to file or directory"},
            "offset": {"type": "integer", "description": "Line number to start from (1-indexed)"},
            "limit": {"type": "integer", "description": "Max lines to read (default 2000)"},
        },
        "required": ["file_path"],
    },
    source=ToolSource(type="executor"),
    category="filesystem",
    read_only=True,
    timeout_seconds=30,
)

# write — Create or overwrite a file
write_tool = ToolDefinition(
    name="write",
    description="Write content to a file, creating it if it does not exist.",
    parameters={
        "type": "object",
        "properties": {
            "file_path": {"type": "string", "description": "Absolute path to the file"},
            "content": {"type": "string", "description": "Content to write"},
        },
        "required": ["file_path", "content"],
    },
    source=ToolSource(type="executor"),
    category="filesystem",
    read_only=False,
    non_bypassable=True,
    timeout_seconds=30,
)

# edit — Replace text in a file
edit_tool = ToolDefinition(
    name="edit",
    description="Edit a file by replacing exact text matches.",
    parameters={
        "type": "object",
        "properties": {
            "file_path": {"type": "string", "description": "Absolute path to the file"},
            "old_string": {"type": "string", "description": "Text to find and replace"},
            "new_string": {"type": "string", "description": "Replacement text"},
            "replace_all": {"type": "boolean", "description": "Replace all occurrences"},
        },
        "required": ["file_path", "old_string", "new_string"],
    },
    source=ToolSource(type="executor"),
    category="filesystem",
    read_only=False,
    non_bypassable=True,
    timeout_seconds=30,
)

# apply_patch — Apply a strict text patch
apply_patch_tool = ToolDefinition(
    name="apply_patch",
    description="Apply a strict apply_patch envelope or the supported unified diff update subset to one or more text files.",
    parameters={
        "type": "object",
        "properties": {
            "patchText": {
                "type": "string",
                "description": "Patch text in apply_patch envelope syntax or the supported unified diff update subset",
            },
            "operation": {
                "type": "object",
                "description": "Native apply_patch operation with type, path, and optional diff",
            },
        },
        "anyOf": [{"required": ["patchText"]}, {"required": ["operation"]}],
    },
    source=ToolSource(type="executor"),
    category="filesystem",
    read_only=False,
    non_bypassable=True,
    timeout_seconds=30,
)

# multiedit — Multiple edits on one file
multiedit_tool = ToolDefinition(
    name="multiedit",
    description="Apply multiple sequential text replacements to a single file.",
    parameters={
        "type": "object",
        "properties": {
            "file_path": {"type": "string", "description": "Absolute path to the file"},
            "edits": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "old_string": {"type": "string"},
                        "new_string": {"type": "string"},
                        "replace_all": {"type": "boolean"},
                    },
                    "required": ["old_string", "new_string"],
                },
            },
        },
        "required": ["file_path", "edits"],
    },
    source=ToolSource(type="executor"),
    category="filesystem",
    read_only=False,
    non_bypassable=True,
    timeout_seconds=30,
)

# list_directory — List directory contents
list_directory_tool = ToolDefinition(
    name="list_directory",
    description="List files and subdirectories in a directory.",
    parameters={
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Absolute path to the directory"},
            "ignore": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Glob patterns to ignore",
            },
        },
    },
    source=ToolSource(type="executor"),
    category="filesystem",
    read_only=True,
    timeout_seconds=30,
)
```

### Search Tools

```python
# glob — Find files by pattern
glob_tool = ToolDefinition(
    name="glob",
    description="Find files matching a glob pattern.",
    parameters={
        "type": "object",
        "properties": {
            "pattern": {"type": "string", "description": "Glob pattern (e.g. '**/*.py')"},
            "path": {"type": "string", "description": "Directory to search in"},
        },
        "required": ["pattern"],
    },
    source=ToolSource(type="executor"),
    category="search",
    read_only=True,
    timeout_seconds=30,
)

# grep — Search file contents
grep_tool = ToolDefinition(
    name="grep",
    description="Search file contents using regex patterns.",
    parameters={
        "type": "object",
        "properties": {
            "pattern": {"type": "string", "description": "Regex pattern to search for"},
            "path": {"type": "string", "description": "Directory to search in"},
            "include": {
                "type": "string",
                "description": "File pattern filter; use brace syntax or comma-separated globs for multiple patterns (e.g. '*.py', '*.{ts,tsx}', '*.ts,*.svelte')",
            },
        },
        "required": ["pattern"],
    },
    source=ToolSource(type="executor"),
    category="search",
    read_only=True,
    timeout_seconds=30,
)
```

### Shell Tools

```python
# bash — Execute shell commands
bash_tool = ToolDefinition(
    name="bash",
    description="Execute a shell command.",
    parameters={
        "type": "object",
        "properties": {
            "command": {"type": "string", "description": "Shell command to execute"},
            "description": {"type": "string", "description": "Brief description of what this does"},
            "timeout": {"type": "integer", "description": "Timeout in milliseconds"},
            "workdir": {"type": "string", "description": "Working directory"},
            "run_in_background": {"type": "boolean", "description": "Return a shell_id for managed polling"},
            "target_executor": {"type": "string", "description": "Optional assigned executor ID for this call"},
        },
        "required": ["command"],
    },
    source=ToolSource(type="executor"),
    category="shell",
    read_only=False,
    non_bypassable=True,
    timeout_seconds=3605,
)
```

Foreground `bash` commands default to a 120,000 ms timeout and may request up to
3,600,000 ms. When a foreground command times out or the tool call is cancelled,
the executor requests cleanup of the command process group where supported before
returning or propagating cancellation; Windows cleanup is limited to the shell
process unless the platform provides stronger process-tree support.

Use `run_in_background=true` for long-running builds, deployments, and watchers.
Background commands return a managed `shell_id`; use `bash_output` to poll output
and `bash_kill` to stop them. For background commands, `timeout` only controls
the initial preview wait and does not limit process lifetime. Prefer these
managed controls over process-name polling such as `pgrep`, which can match
wrappers or the polling command itself.

### Multi-Executor Targeting

Executor-routed tools may include an optional `target_executor` parameter. This
parameter is consumed by the controller and stripped before the tool request is
sent over JSON-RPC to the executor.

Rules:

- `target_executor` must be a real executor ID resolved from the agent's primary
  or additional executor bindings. Aliases are not supported.
- If `target_executor` is omitted, the call routes to the current active
  executor.
- If `target_executor` is present, only that call routes to the named executor;
  the active executor does not change.
- If the target executor is unavailable, the tool is not evaluated by Intaris
  and no RPC is sent. The controller returns a factual error, for example:
  `Target executor "infra-runner" is offline; tool was not executed.`
- If the target executor is available but does not expose the requested tool,
  the controller returns a tool error listing the executor state and available
  assigned executors for that tool.
- Guardrails evaluation includes the resolved target executor ID, primary vs.
  additional membership, labels, runtime state, and environment snapshot when
  available.

When an executor-routed tool is exposed to the model, the controller augments
the JSON schema with `target_executor` for tools that can run on more than one
assigned executor. The underlying `ToolDefinition` remains executor-neutral so
the same definition can be reused across in-process, subprocess, websocket, and
compiled-lite executors.

### Web Tools

Web tools support configurable backends. The default backend is set via the
`web.backend` setting (`"direct"`, `"tavily"`, or `"brave"`). Each tool call
can override the default with a `backend` parameter.

**Backends:**

| Backend | Fetch | Search | Crawl/Map/Research | API Key Required |
|---------|-------|--------|--------------------|------------------|
| `direct` | httpx + browser headers | DuckDuckGo | no | no |
| `tavily` | Tavily Extract | Tavily Search | yes | yes (`tavily_api_key`) |
| `brave` | falls back to direct | Brave Web Search | no | yes (`brave_api_key`) |

```python
# web_fetch — Fetch URL content (supports direct + tavily backends)
# web_search — Search the web (supports direct + tavily + brave backends)
# web_crawl — Crawl a website (requires tavily)
# web_map — Map site structure (requires tavily)
# web_research — Deep research (requires tavily)
```

The direct backend uses browser-like headers (Chrome User-Agent, Accept,
Sec-Fetch-* headers) to avoid bot detection. It retries on HTTP 429 with
exponential backoff and Retry-After header support. Cloudflare-protected
sites that require browser access return an actionable error suggesting
the Tavily backend.

### Safety Classification

| Tool | read_only | non_bypassable | Rationale |
|------|-----------|----------------|-----------|
| `read` | yes | no | Read-only filesystem access |
| `write` | no | **yes** | Creates/overwrites files |
| `edit` | no | **yes** | Modifies file content |
| `apply_patch` | no | **yes** | Modifies files via diff |
| `multiedit` | no | **yes** | Modifies file content |
| `list_directory` | yes | no | Read-only directory listing |
| `glob` | yes | no | Read-only file search |
| `grep` | yes | no | Read-only content search |
| `bash` | no | **yes** | Arbitrary command execution |
| `web_fetch` | yes | no | Read-only URL fetching |
| `web_search` | yes | no | Read-only web search |
| `web_crawl` | yes | no | Read-only website crawling |
| `web_map` | yes | no | Read-only site structure mapping |
| `web_research` | yes | no | Read-only research |

Write operations always go through Intaris evaluation regardless of agent
permissions. Read-only tools can be auto-approved by agent permission matrix.

Filesystem edit tools may append executor-local LSP diagnostics after a file is
changed. This is best-effort feedback from the selected executor runtime: the
edit still succeeds if LSP is disabled, unavailable, or times out during first
server startup. `read` may warm LSP in the background, but it does not block on
diagnostics.

The `apply_patch` tool accepts strict text-only patches in two forms:
- full apply_patch envelope syntax for `Add File`, `Update File`, `Delete File`, and `Move to`
- the supported unified diff subset for updates to existing files
- native OpenAI Responses operation objects (`create_file`, `update_file`, `delete_file`)

Existing-file `apply_patch` updates, deletes, and moves require a prior `read` of the
source file in the same execution scope. `Add File` does not. Unsupported apply_patch
forms fail deterministically instead of falling back to fuzzy matching. EOF
markers are supported: `*** End of File` is accepted inside apply_patch envelopes,
and `\ No newline at end of file` preserves final-newline semantics in apply_patch
envelopes and unified-diff hunks.

## Built-in Orchestration Tools

These are handled by the controller directly — they are not tool executions
but session management operations.

```python
# delegate — Request delegation to a specialized agent
delegate_tool = ToolDefinition(
    name="delegate",
    description="Delegate a task to a specialized agent.",
    parameters={
        "type": "object",
        "properties": {
            "agent_id": {"type": "string", "description": "Agent ID or 'auto'"},
            "task": {"type": "string", "description": "Task description"},
            "context": {"type": "string", "description": "Background context"},
            "expected_output": {"type": "string"},
        },
        "required": ["task"],
    },
    source=ToolSource(type="builtin"),
)

Worker/fork delegation modes remain deferred design concepts. The only
implemented sub-session orchestration tool today is `delegate`.
```

These tools submit **requests** to the Decision Engine, which approves,
modifies, or rejects them. The LLM cannot force delegation.

### Built-in Executor Routing Tools

Executor routing tools are controller-handled. They mutate turn-local runtime
state and never execute on an executor.

```python
# switch_executor — Change the active executor for subsequent executor-routed tools
switch_executor_tool = ToolDefinition(
    name="switch_executor",
    description=(
        "Switch the active executor for subsequent executor-routed tool calls. "
        "Use target_executor on individual tools for one-off execution instead."
    ),
    parameters={
        "type": "object",
        "properties": {
            "target_executor": {"type": "string", "description": "Assigned executor ID"},
            "reason": {"type": "string", "description": "Why this executor is needed"},
        },
        "required": ["target_executor"],
    },
    source=ToolSource(type="builtin"),
    category="orchestration",
    read_only=True,
)
```

`switch_executor` validation:

- The target must be in the resolved primary or additional executor set for the
  current agent.
- The target must be usable: active DB status, `active` or `degraded` runtime
  state, matching desired/applied config generation, ready connection for
  remote executors, and at least one observed tool.
- On success, the tool result includes the target executor's environment and
  available tool summary.
- On failure, the active executor is unchanged and the result states the target
  executor's factual state. The result must not speculate about why the
  executor is unavailable.
- If the agent is active on a non-primary executor, Cognis injects a reminder to
  switch back to a primary executor when the non-primary work is complete.
- If the active non-primary executor becomes unavailable and the work requires
  that executor, Cognis notifies the user and cancels the turn instead of
  continuing on a different host.

## Built-in Task Inspection Tools

These are controller-handled read/write tools for the durable task queue.
They let the main chat agent inspect and manage background work when the
user asks things like "what finished recently?" or "show me blocked tasks".

```python
# list_tasks — Query tasks visible to the current user/agent
list_tasks_tool = ToolDefinition(
    name="list_tasks",
    description="List tasks by status, agent, workflow, or recent completion.",
    parameters={
        "type": "object",
        "properties": {
            "status": {"type": "string"},
            "agent_id": {"type": "string"},
            "workflow_id": {"type": "string"},
            "recently_finished": {"type": "boolean"},
            "limit": {"type": "integer", "default": 20},
        },
    },
    source=ToolSource(type="builtin"),
    read_only=True,
)

# get_task_status — Detailed status/progress for one task
get_task_status_tool = ToolDefinition(
    name="get_task_status",
    description="Get current status, workflow step, gates, and result summary for a task.",
    parameters={
        "type": "object",
        "properties": {
            "task_id": {"type": "string"},
        },
        "required": ["task_id"],
    },
    source=ToolSource(type="builtin"),
    read_only=True,
)

# update_task — Pause/resume/cancel or reprioritize a task
update_task_tool = ToolDefinition(
    name="update_task",
    description="Pause, resume, cancel, or reprioritize a task.",
    parameters={
        "type": "object",
        "properties": {
            "task_id": {"type": "string"},
            "action": {"type": "string", "enum": ["pause", "resume", "cancel"]},
            "priority": {"type": "integer"},
        },
        "required": ["task_id"],
    },
    source=ToolSource(type="builtin"),
)
```

These tools are useful in the main chat because completed/paused/failed task
events may arrive while the conversation is active or idle. The chat agent can
query the queue and answer naturally, instead of relying only on pushed events.

## MCP Integration

### Global MCP Servers (DB-Managed)

MCP servers are configured globally in the ``mcp_servers`` DB table and
assigned to executors via ``config.mcp_server_ids``.  Agents inherit MCP
tools from their assigned executor.

```sql
CREATE TABLE mcp_servers (
    server_id         TEXT PRIMARY KEY,
    name              TEXT NOT NULL,
    transport         TEXT NOT NULL DEFAULT 'stdio',
    command           TEXT,              -- Required for stdio
    url               TEXT,              -- Required for sse/streamable_http
    args              JSON,
    env               JSON,              -- stdio only: {"KEY": "value"} or {"KEY": "$secret:name"}
    headers           JSON,              -- http only: {"Authorization": "value"} or "$secret:name"
    timeout_seconds   INTEGER NOT NULL DEFAULT 30,
    description       TEXT,
    owner_email       TEXT REFERENCES users(email),
    status            TEXT NOT NULL DEFAULT 'active',
    UNIQUE(name, owner_email)
);
```

MCP servers are **user-scoped** — each user manages their own servers.
Assignment to executors is validated: ``config.mcp_server_ids`` may only
reference servers owned by the same user as the executor.

### Local MCP (Executor-Hosted)

Local MCP servers run on the executor.  During ``executor.configure``, the
controller resolves assigned MCP servers from the DB, resolves secrets, and
sends the configs to the executor. The executor starts transport-specific MCP
clients (``stdio``, ``sse``, or ``streamable_http``), discovers tools, and
includes them in ``tool.list``.

Flow: Controller evaluates via Intaris → approved → tool.execute to Executor →
Executor calls local MCP server → result back to Controller.

**Supported transports for executor-hosted MCP:** ``stdio``, ``sse``, and
``streamable_http`` across websocket, subprocess, and in-process executor
modes. Transport-specific configuration is strict: ``stdio`` uses ``env`` and
HTTP transports use ``headers``.

Note: Common developer tools (filesystem, shell, search) are executor-native
and do not require MCP server configuration.  Local MCP is for specialized
servers (databases, custom APIs, etc.).

### Intaris-Managed MCP (Remote)

Remote MCP servers registered in Intaris.  Intaris acts as MCP proxy,
evaluating safety AND executing in one call.

```yaml
# In agent definition (agent-level, not executor-dependent)
tools:
  intaris_mcp_servers: ["github", "slack"]
```

Available Intaris MCP servers are auto-discovered via ``GET /api/v1/mcp/servers``
on Intaris.  The agent form shows a multi-select dropdown of discovered servers.
Server configuration (credentials, endpoints) is managed on the Intaris side.

Flow: Controller calls Intaris ``POST /api/v1/mcp/call`` → Intaris evaluates +
proxies to remote MCP server → result back to Controller.  Executor not
involved.

Intaris MCP tools are resolved at session setup and injected into the tool
registry alongside executor-provided tools.  They are included in the
effective-tools API response and the tool exposure layer.

### Tool Discovery and Inventory Assembly

At session setup, the controller assembles the **full effective inventory**
from all sources.  This is the internal source of truth — the LLM does NOT
see all of these directly.

```python
full_effective_inventory = (
    executor_native_tools           # Always available (opt-out per agent)
    + builtin_orchestration_tools   # delegate + task/workflow tools
    + builtin_system_tools          # list_agents, get_status, search_tools
    + builtin_memory_tools          # memory_search, memory_add, etc.
    + builtin_workflow_tools        # step_complete, step_todo_write, etc.
    + web_tools                     # web_fetch, web_search (from executor)
    + local_mcp_tools               # From executor's assigned MCP servers
    + intaris_mcp_tools             # From Intaris /mcp/tools (agent-assigned)
    + skill_tools                   # From active skills
)
```

The **tool exposure layer** then derives the model-facing tool set:

```python
model_facing_tools = tool_exposure.prepare(
    full_inventory=full_effective_inventory,
    provider=resolved_provider,
    model=resolved_model,
)
# Returns: core tools (always loaded) + deferred tools (provider-specific)
```

The LLM sees a model-facing tool set derived from the step profile and provider
capabilities. It can discover additional eligible tools via provider-native
tool search, the generic ``search_tools`` builtin, or ``skill_load`` when a
skill activates deferred tool ids. See the "Tool Exposure Architecture"
section for details.

## Tool Permission Evaluation

Shared tool semantics are runtime-neutral:

- `allow`
- `deny`
- `evaluate`
- `non_bypassable`
- timeout
- cancel

The `native` runtime enforces these through the existing controller-driven tool
loop. External runtimes must translate the same semantics to native runtime
enforcement without changing user-facing policy behavior.

```python
async def evaluate_tool_call(self, tool_call, agent, session):
    """
    Permission check flow:
    1. Non-bypassable check (system-level, always goes through guardrails)
    2. Agent permission matrix
    3. Guardrails evaluation (Intaris)
    """
    tool = self.registry.get(tool_call.name)

    # Step 1: Non-bypassable tools ALWAYS go through guardrails
    if tool and tool.non_bypassable:
        return await self._evaluate_via_guardrails(tool_call, session)

    # Step 2: Agent permission matrix
    permission = agent.permissions.resolve_permission(tool_call.name)
    if permission == Permission.DENY:
        return PermissionDecision(decision="deny", source="agent_permission")
    if permission == Permission.ALLOW:
        return PermissionDecision(decision="approve", source="agent_permission")

    # Step 3: EVALUATE → Intaris
    return await self._evaluate_via_guardrails(tool_call, session)
```

### Non-Bypassable Tools

Certain tool categories always go through guardrails regardless of agent
permissions. Configured system-wide:

Configured in the `settings` DB table (key: `security.non_bypassable_tools`),
managed via Settings UI or `PUT /api/v1/settings/security.non_bypassable_tools`:

```json
["bash", "write", "edit", "apply_patch", "multiedit", "*/create_*", "*/delete_*"]
```

This prevents an agent with `"*": "allow"` from bypassing safety checks on
destructive operations.

### MCP Tool Naming

MCP tool names must be safe for all LLM providers.  OpenAI requires tool
names to match ``^[a-zA-Z0-9_-]+$`` — no slashes, dots, or spaces.

**Model-visible name format:**

| Source | Internal routing | Model-visible name |
|--------|------------------|--------------------|
| Executor-native | ``ToolSource(type="executor")`` | ``read``, ``bash``, ``glob`` |
| Local MCP | ``ToolSource(type="local_mcp", server_name="postgres", raw_tool_name="query")`` | ``mcp_postgres__query`` |
| Intaris MCP | ``ToolSource(type="intaris_mcp", server_name="github", raw_tool_name="create_issue")`` | ``mcp_github__create_issue`` |

**Sanitization rules:**

- Replace ``/`` with ``__`` (double underscore)
- Replace any character not in ``[a-zA-Z0-9_-]`` with ``_``
- Prefix MCP tools with ``mcp_``
- If sanitization would create a collision, append a deterministic short suffix
  derived from the stable MCP identity so the visible tool names stay unique
- Store original server name and tool name in ``ToolSource.server_name``
  and ``ToolSource.raw_tool_name`` for dispatch

**Dispatch uses source metadata, not name parsing:**

```python
# Old (broken for OpenAI):
_, raw_tool_name = tool_call.name.split("/", 1)

# New (provider-safe):
server_name = registered_tool.definition.source.server_name
raw_tool_name = registered_tool.definition.source.raw_tool_name
```

**Stable tool IDs** for permission matching and UI:

- Builtin/native: ``builtin:<tool_name>``
- MCP: ``mcp:<server_id>:<raw_tool_name>``

Permission matching supports both stable IDs and plain names:

```yaml
permissions:
  tool_permissions:
    "*": "evaluate"                              # Default
    "read": "allow"                              # Executor-native read
    "builtin:glob": "allow"                      # Stable ID form
    "mcp_postgres__query": "evaluate"            # Model-visible name
    "mcp:mcp_postgres:query": "evaluate"         # Stable ID form
```

## Tool Router

```python
class ToolRouter:
    ORCHESTRATION_TOOLS = {"delegate", ...task/workflow tools...}
    CONTROLLER_TASK_TOOLS = {"list_tasks", "get_task_status", "update_task"}

    async def route(self, tool_call, session, agent, executor):
        # Orchestration → controller handles directly
        if tool_call.name in self.ORCHESTRATION_TOOLS:
            return await self.decision_engine.handle_orchestration(
                session, tool_call
            )

        # Task queue inspection/control → controller handles directly
        if tool_call.name in self.CONTROLLER_TASK_TOOLS:
            return await self.task_service.handle_tool_call(session, tool_call)

        # Intaris-managed MCP → Intaris proxy
        tool = self.registry.get(tool_call.name)
        if tool and tool.source.type == "intaris_mcp":
            return await self.guardrails.call_mcp_tool(
                session_id=session.intaris_session_id,
                server_name=tool.source.server_name,
                tool_name=tool.source.raw_tool_name,  # Use metadata, not name parsing
                arguments=tool_call.arguments,
            )

        # Executor-native and local MCP → resolve target, evaluate, execute
        target = await executor_pool.resolve_tool_target(tool_call, agent)
        if not target.usable:
            return ToolResult(
                output=f'Target executor "{target.executor_id}" is {target.runtime_state}; tool was not executed.',
                is_error=True,
            )

        decision = await self.evaluate_tool_call(
            tool_call.without_controller_args(), agent, session, target_executor=target
        )
        if decision.decision == "approve":
            return await target.connection.tool_execute(tool_call.without_controller_args())
        elif decision.decision == "deny":
            return ToolResult(denied=True, reasoning=decision.reasoning)
        elif decision.decision == "escalate":
            user_decision = await self.wait_for_escalation(
                session, tool_call, decision
            )
            if user_decision == "approve":
                return await executor.tool_execute(tool_call)
            return ToolResult(denied=True, reasoning="User denied")
```

## Tool Exposure Architecture

### Problem: Large Tool Inventories

Agents may have access to many tools from multiple sources (executor-native,
MCP, Intaris MCP, skills).  Sending all tool schemas to the LLM on every
request creates three problems:

1. **Provider limits** — OpenAI enforces a hard cap of 128 tools per request.
2. **Token cost** — 50+ tools can consume 10K-55K+ tokens before the
   conversation even starts.  Anthropic has measured 134K tokens for tool
   definitions alone in real deployments.
3. **Accuracy degradation** — models make worse tool selections with large,
   similar-looking tool sets.  Anthropic's testing showed accuracy improving
   from 49% to 74% (Opus 4) when using deferred loading instead of
   loading all tools upfront.

### Solution: Two-Tier Tool Exposure

The full effective tool inventory is the internal source of truth.  A
separate **tool exposure layer** derives the model-facing tool set before
each LLM call.

```
┌─────────────────────────────────────────────────────┐
│  Full Effective Inventory (internal)                  │
│  - All tools from all sources                        │
│  - Stable internal IDs                               │
│  - Used for: effective-tools API, agent editor,      │
│    permission resolution, runtime dispatch            │
│  - NOT sent to the model directly                    │
└──────────────────────┬──────────────────────────────┘
                       │
          ┌────────────┴────────────┐
          │                         │
┌─────────▼──────────┐  ┌──────────▼──────────────────┐
│  Core Tools         │  │  Deferred / Discoverable    │
│  (always loaded)    │  │  Tools                      │
│                     │  │                             │
│  - memory           │  │  - Executor MCP tools       │
│  - orchestration    │  │  - Intaris MCP tools        │
│  - step/task/wf     │  │  - Overflow tools           │
│  - filesystem       │  │  - Low-frequency tools      │
│  - shell            │  │                             │
│  - web              │  │  Loaded on demand via       │
│  - system/image     │  │  provider-specific mechanism │
│                     │  │  or generic tool search      │
│  Stable across      │  │                             │
│  turns → cached     │  │  Does NOT break cache       │
└─────────────────────┘  └─────────────────────────────┘
```

### Provider-Specific Mechanisms

Each LLM provider has a different optimal approach for handling deferred
tools while preserving prompt caching:

#### OpenAI

**For gpt-5.4+ models (Responses API):**

- Use the OpenAI Responses transport, but keep Cognis' internal transcript
  canonical.  A provider-boundary bridge translates canonical chat/tool
  messages into Responses ``input`` and normalizes Responses output back into
  the existing assistant/tool-call delta shape used by the agent loop.
- Responses-capable OpenAI models use a **stable full inventory** instead of
  falling back to generic slot-based trimming.  This keeps same-turn tool
  visibility aligned with the effective runtime.
- Tool exposure invariants remain unchanged:
  - stable ``stable_tool_id()`` values
  - deterministic ordering by category/source/stable ID
  - alias preservation from visible names back to internal names
  - cache-stable schema ordering/bytes across turns
- ``search_tools`` remains the fallback for non-Responses or unsupported
  models/providers.

**For older models (Chat Completions API):**

- Hard limit of **128 tools** per request.
- Use ``allowed_tools`` in ``tool_choice`` to restrict which tools the model
  can call **without changing the ``tools`` array**:

  ```python
  tool_choice = {
      "type": "allowed_tools",
      "mode": "auto",
      "tools": [
          {"type": "function", "name": "read"},
          {"type": "function", "name": "bash"},
          # ... only core tools listed here
      ]
  }
  ```

  The full toolkit stays in the cached prefix; per-turn restrictions live
  in request metadata only.  Cache is preserved.

- If total tools exceed 128, use a generic controller-side tool search
  fallback (see below).

#### Anthropic Claude

- Use ``defer_loading: true`` on MCP/overflow tools.
- Include a **Tool Search Tool** (regex or BM25 variant) in the tools array.
- Deferred tools are excluded from the system-prompt prefix entirely.
- When Claude discovers a deferred tool through search, the definition is
  appended inline as a ``tool_reference`` block in conversation history.
- **The prefix is untouched, so prompt caching is preserved.**
- Anthropic reports 85% token reduction and significant accuracy improvements.
- LiteLLM supports ``defer_loading`` for Anthropic via the
  ``tool-search-tool-2025-10-19`` beta header.

**Cache breakpoints for tools:**

Place ``cache_control: {"type": "ephemeral"}`` on the last tool in the
``tools`` array.  This caches the entire tool-definitions prefix.  Anthropic
supports up to 4 cache breakpoints; Cognis should use at least 2:

1. Last tool definition (caches all tools)
2. Last immutable system message (caches system prompt + memory instructions)

Cache hierarchy: ``tools → system → messages``.  A change at one level
invalidates that level and everything after it.

#### Google Gemini

- Supports **context caching** (explicit and implicit).
- Minimum cacheable content: 1024-4096 tokens depending on model.
- No equivalent of ``defer_loading`` or ``tool_search``.
- Use the generic controller-side tool search fallback for large inventories.

#### Other Providers

- Use the generic controller-side tool search fallback.
- Keep core tools in the ``tools`` array.
- Add a ``search_tools`` builtin tool for discovering overflow tools.

### Generic Tool Search Fallback

For providers without native tool search (or as a universal fallback), Cognis
provides a controller-side ``search_tools`` builtin tool:

```python
search_tools = ToolDefinition(
    name="search_tools",
    description=(
        "Search for additional tools available in this session. "
        "Use when you need a capability not in your current tool set. "
        "Returns matching tool names and descriptions."
    ),
    parameters={
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "Natural language search query"
            },
            "category": {
                "type": "string",
                "description": "Filter by tool category (e.g. 'mcp', 'filesystem')"
            },
        },
        "required": ["query"],
    },
    source=ToolSource(type="builtin"),
    category="system",
    read_only=True,
)
```

The handler searches the current step's hidden eligible inventory (including
deferred tools that survived profile filtering but excluding tools already
visible to the model) and returns matching tool definitions. The agent loop
then injects discovered tools into the next turn's ``tools`` array.

``search_tools`` is a controller-managed, read-only system builtin.  It does
not execute arbitrary tools itself; it only reveals the already effective,
permission-filtered inventory for the current step.

### Discovery Strategy Matrix

Step profiles narrow the effective inventory **before** Cognis chooses a
provider-specific discovery strategy.  The profile decides two things:

- which tools remain eligible for the current step
- which of those eligible tools are visible by default

Mode semantics:

- `soft` narrows the default-visible tool surface to the profile matrix plus
  explicit overrides, while keeping the searchable inventory broad for that step
- `hard` narrows the searchable inventory too, so discovery cannot escape the
  hard-approved subset
- agent/executor/runtime tool assignment remains the outer hard boundary in all
  cases

Deferred tools are tools Cognis may keep out of the current visible surface
until the model searches for them or explicitly loads a skill that exposes
them. Typical deferred candidates are MCP tools, Intaris MCP tools, and
skill-defined tools that are not attached by default.

### Skill Activation Discovery

`skill_load` is a controller-managed discovery path in its own right.

1. The model loads a skill summary into protected context with ``skill_load``.
2. If the skill version declares tool summaries that resolve to tool ids,
   Cognis activates those ids immediately.
3. If the skill has no declared tool ids, Cognis asks the configured
   ``classifier`` model to choose zero or more tool ids from the current hidden
   searchable inventory.
4. Activated tool ids are cached for the current session and included in later
   model-facing visibility calculations.

The classifier path is conservative by design. Empty results are valid and are
cached per session by ``(skill_id, content_hash)``.

| Provider / Setup | Strategy | Hidden tool discovery | Implications | Preferred |
|---|---|---|---|---|
| Anthropic with `supports_defer_loading=true` | `anthropic_defer_loading` | Deferred tools stay in the tool array with `defer_loading=true`; controller `search_tools` may still be available | Best Anthropic path; compact default surface and cache-friendly | Yes |
| OpenAI Responses with native namespaces + allowed-tools support | `openai_responses_tool_search` | Native `tool_search` plus namespace-deferred tools; core tools restricted with `allowed_tools` | Best intended OpenAI path; smallest advertised surface and strongest cache stability | Yes |
| OpenAI Responses without namespace tools but with allowed-tools support | `openai_responses_flat_tool_search` | Native `tool_search` plus flat deferred tools with `defer_loading=true` | Similar to the preferred OpenAI path, just less structured | Yes |
| OpenAI Responses downgraded from native tool search | `openai_responses_controller_search_fallback` | Controller `search_tools` discovers deferred tools from the step-filtered inventory | Keeps Responses transport but discovery becomes controller-managed; slightly worse cache stability and first-search UX | Fallback |
| OpenAI Responses with no deferred/search path left | `openai_responses_full_inventory` or `openai_responses_full_inventory_no_defer` | No discovery; everything is visible up front | Works, but increases tool-surface clutter and model confusion for large inventories | Avoid for large inventories |
| Generic chat-completions / Responses-disabled providers | `generic_search_tools` | Controller `search_tools` builtin only | Universal compatibility path; weaker than native deferred search but still respects step profiles | Fallback |
| Gemini and other providers without native deferred tools | `generic_search_tools` | Controller `search_tools` builtin only | Best available path; no provider-native defer/search support | Fallback |

#### Native OpenAI downgrade cache

When a provider/model rejects native OpenAI Responses `allowed_tools` /
`tool_search` parameters, Cognis marks that `(provider_id, model)` pair as
broken for native tool search and uses the controller fallback strategy for the
rest of the process lifetime.  The cache is cleared on restart or when the
provider configuration is updated.

### Tool Exposure Flow

```
1. Build full effective inventory (all sources, all tools)

2. Classify tools:
   - Core: builtin, executor-native, web → always loaded
   - Deferred: MCP, Intaris MCP, overflow → loaded on demand

3. Resolve the step profile:
   - Filter the eligible inventory (`hard` narrows search, `soft` keeps search broad)
   - Compute the default-visible subset for the step

4. Detect provider capabilities:
   - OpenAI gpt-5.4+: prefer Responses API native tool_search
   - Anthropic: use defer_loading + optional controller `search_tools`
   - OpenAI downgraded / unsupported: use Responses controller fallback
   - Other: use generic controller `search_tools` fallback

5. Build model-facing tool set:
   - Sanitize all names for provider compatibility
   - Apply provider-specific deferred loading flags
   - Build alias map (model-visible name → internal identity)
   - Apply cache hints (tool-level cache_control for Anthropic)

6. On tool call from model:
   - Reverse-map model-visible name to internal identity
   - Dispatch via ToolRouter using internal identity + source metadata

7. On search_tools call:
   - Search full effective permission-filtered inventory
   - Return matching tool schemas
   - Inject discovered tools into next turn
```

### Prompt Caching Strategy

Prompt caching is critical for cost efficiency.  The architecture must
preserve cache hits across turns within a session.

**Current provider caching behavior:**

| Provider | Mechanism | Granularity | Tool caching |
|----------|-----------|-------------|--------------|
| OpenAI | Automatic prefix matching | 128-token increments from 1024+ | Tools are part of cacheable prefix |
| Anthropic | Explicit ``cache_control`` breakpoints (up to 4) | Per-block | Tools cacheable with ``cache_control`` |
| Gemini | Implicit + explicit context caching | 1024-4096 token minimum | Part of cached content |

**Cache-preserving design rules:**

1. **Keep the ``tools`` array stable across turns.**  Do not add/remove tools
   between turns within a session.  Use ``allowed_tools`` (OpenAI) or
   ``defer_loading`` (Anthropic) to vary visibility without changing the array.

2. **Place static content first.**  Order: tools → one consolidated immutable
   system message (identity → runtime instructions → memory instructions →
   core memories → skills metadata/guidance → continuation summary) →
   environment → history → user message.

3. **Use multiple cache breakpoints for Anthropic.**  Mark the last tool
   definition and the last immutable system message with ``cache_control``.

4. **Use ``prompt_cache_key`` for OpenAI** when available, to improve routing
   stickiness across requests with shared prefixes.

5. **Deferred tools do not break cache.**  On both OpenAI (tool_search) and
   Anthropic (defer_loading), discovered tools are injected at the end of
   context or inline in conversation, not in the cached prefix.

6. **The generic ``search_tools`` fallback may break cache** if it injects
   new tools into the ``tools`` array on subsequent turns.  Mitigate by
   keeping injected tools in a separate "discovered" section at the end of
   the array, and using ``allowed_tools`` to restrict without array changes.

**Token budget accounting:**

Tool schemas consume tokens from the static budget.  The ``ContextAssembler``
must account for tool schema tokens when computing the dynamic budget for
messages and memory.  Core tools are a fixed cost; deferred tools are zero
cost until discovered.

## Skill System

Skills are DB-managed instruction + tool bundles with import/export support.
Agents can also manage skills via a built-in LLM tool.

Cognis remains compatible with the official `SKILL.md` style used by other
harnesses. A skill may therefore be:

- an instruction-only bundle that is loaded inline
- a bundle with tool recipes
- an instruction bundle that also declares reusable workflow material for the
  workflow composer

```python
class Skill(BaseModel):
    skill_id: str
    name: str
    description: str
    instructions: str              # Markdown, injected into context
    tools: list[ToolDefinition] = []
    prompt_templates: dict[str, str] = {}
    steps: list[dict[str, Any]] = []
    workflow_templates: list[dict[str, Any]] = []
    source: str                    # "db", "file"
    attach_to_all_agents: bool = False
    owner_email: str | None = None
    tags: list[str] = []
```

### DB Schema

```sql
CREATE TABLE skills (
    skill_id    TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    description TEXT,
    instructions TEXT NOT NULL,
    tools       JSON,              -- list of ToolDefinition dicts
    prompt_templates JSON,
    steps       JSON,
    workflow_templates JSON,
    tags        JSON,
    auto_load   INTEGER NOT NULL DEFAULT 0,  -- internal storage for attach_to_all_agents
    owner_email TEXT REFERENCES users(email),
    created_at  TIMESTAMP NOT NULL,
    updated_at  TIMESTAMP NOT NULL
);
```

### Skill Management

Skills are managed via:
1. **API** — CRUD endpoints at `/api/v1/skills`
2. **UI** — Tools & Skills page, Skills tab
3. **Import/Export** — YAML format for GitOps workflows
4. **LLM Tool** — `skill_write` built-in tool for agent self-management

The import/export surface preserves plain `SKILL.md` compatibility. Cognis-only
workflow extensions such as `steps:` and `workflow_templates:` are optional.

### Runtime Model

Skills have three distinct runtime states:

1. **Discoverable** — all visible skills for the user. These are announced in
   compact prompt metadata and can be loaded explicitly with `skill_load`.
2. **Attached** — skills selected on the agent plus skills attached to all
   agents. These are highlighted in the prompt as preferred defaults.
3. **Forced/Inherited** — workflow or delegation scoped attachments that can be
   injected into a sub-session explicitly.

Skills may also participate in three execution shapes:

1. **Inline guidance** — `instructions` loaded into a direct turn or
   `system:general-task` execution.
2. **Workflow fragment** — `steps:` consumed by `system:workflow_composer` as
   reusable step material.
3. **Workflow skeleton** — `workflow_templates:` consumed by the composer as a
   full-process starting point.

If a visible skill has no declared `steps:`, Cognis may still derive step
fragments on demand through the hidden `system:skill_decomposer` agent. That
derived structure is advisory to the composer and is never written back unless a
user or agent explicitly saves it.

Discoverability and attachment are intentionally separate. Agents should
usually choose from prompt-announced skills and call `skill_load` directly
rather than browsing with `skill_list`.

### Deferred Skill Tools

Skill-defined tools participate in the same deferred tool exposure model as MCP
tools:

- Attached skills start with their tool IDs marked as discovered.
- Discoverable but unattached skills expose their tools only after `skill_load`.
- Prompt metadata stays compact and cache-friendly because full skill content is
  loaded on demand.
- Provider-specific deferred loading (`defer_loading`, `search_tools`, future
  `allowed_tools` restrictions) controls visibility without changing the stable
  tool inventory.

This lets Cognis keep all visible skills discoverable while still encouraging
explicit skill loading and preserving prompt caching.

### Cloud-Native Skill Storage

Skills are managed as Cognis records backed by the database and artifact store.
System skills may be seeded from packaged application resources on bootstrap,
but skills are not discovered from editable controller filesystem directories.

At runtime, executors receive only temporary staged copies of the specific
assets required by the selected skill tool recipe. The controller remains the
source of truth for skill metadata, versions, and assets.

## Complete Tool Call Flow

```
0. Tool exposure layer prepares model-facing tool set:
   - Assemble full effective inventory
   - Classify into core (always loaded) and deferred (discoverable)
   - Apply provider-specific mechanisms (tool_search, defer_loading, allowed_tools)
   - Sanitize names for provider compatibility
   - Build alias map (model-visible name → internal identity)

1. LLM generates tool_call(name, arguments)

1a. Reverse-map model-visible name to internal identity via alias map

2. Tool Router categorizes:
   a. Orchestration → handle as session op (step 2a)
   b. search_tools → search full inventory, return matches (step 2b)
   c. Intaris MCP → Intaris proxy (step 2c)
   d. Executor-native or Local MCP → evaluate and dispatch (steps 3-6)

   2a. Decision Engine approves/modifies → create session, start agent loop
   2b. Controller searches effective inventory → returns tool schemas to LLM
       → LLM may call discovered tools on next turn
   2c. Intaris evaluates + executes → return result to LLM
       (uses source.server_name + source.raw_tool_name for dispatch)

3. Permission evaluation:
   a. Non-bypassable → always Intaris
   b. ALLOW → auto-approve (skip Intaris)
   c. DENY → block immediately
   d. EVALUATE → Intaris evaluation

4. Intaris evaluation:
   a. APPROVE → continue to step 5
   b. DENY → denial + reasoning back to LLM
   c. ESCALATE → pause, notify user, wait (with timeout)

5. Tool execution on executor:
   Controller sends tool.execute → Executor runs tool → returns result
   (Executor checks native handlers first, then MCP dispatch)

6. Record events to Intaris:
   tool_call event + tool_result event + evaluation event

7. Result sanitization (see Trust Model below)

8. Sanitized result returned to LLM for next iteration
```

## Trust Model for Injected Content

### Problem

Tool results, Mnemory recall content, and Intaris event content are
**untrusted data** entering the LLM context. A tool reading a web page,
file, or API response may return content crafted to manipulate the LLM
(prompt injection via tool output). Pre-execution guardrails (Intaris
evaluate) do not protect against this — they evaluate the *request*, not
the *result*.

### Defense Layers

#### Layer 1: Structural Isolation (MVP)

Mark untrusted content with clear boundaries in the context:

```python
def _wrap_tool_result(self, tool_name: str, result: str) -> str:
    return (
        f"<tool_result name=\"{tool_name}\" trust=\"untrusted\">\n"
        f"{result}\n"
        f"</tool_result>"
    )

def _wrap_memory_context(self, recall: RecallResult) -> str:
    return (
        "<memory_context trust=\"untrusted\">\n"
        f"{self._format_memories(recall)}\n"
        "</memory_context>"
    )
```

This is not a security boundary — the LLM can still be influenced — but it
makes the trust level explicit in the prompt and enables future filtering.

#### Layer 2: Output Size Limits and Context Management (MVP)

Three-layer context management for tool outputs:

**2a. Per-tool truncation** at execution time:
- Each executor tool applies its own output cap (shell: 50K, web_fetch: 500K,
  read: 2K lines × 2K chars, grep: 200 files × 500 matches).
- The tool router's `_sanitize_result()` applies **middle-truncation**
  to `max_result_size` (default 50,000 chars): the head and tail of the
  output are preserved, the middle is removed. The truncation marker
  includes the `call_id` so the LLM can recover the full output via
  `read_tool_output`.
- Full output (after executor truncation, before context truncation) is
  saved to the **ToolOutputStore** on the controller's local filesystem
  (`{COGNIS_DATA_DIR}/tool-outputs/{call_id}.txt`) with TTL-based cleanup.

**2b. Per-turn pruning** after each agent turn:
- Walks backwards through tool result messages in the LLM context.
- Protects the most recent ~40K tokens of tool outputs.
- Replaces older tool results with:
  `[Tool result cleared — use read_tool_output(call_id='...') to view]`
- Also clears large tool call arguments (>1K chars serialized) in the
  pruned zone.
- This is a view-layer operation — Intaris events and the ToolOutputStore
  are unaffected.

**2c. Exploration tools** for recovering cleared/truncated output:
- `read_tool_output(call_id, offset?, limit?)` — paginated line-by-line
  read from the ToolOutputStore, similar to the file read tool.
- `search_tool_output(call_id, pattern, context_lines?)` — regex search
  with context lines, similar to grep.
- These are controller-side built-in tools (read-only, no guardrails
  evaluation needed).

**Storage layers:**

| Layer | Content | TTL | Purpose |
|-------|---------|-----|---------|
| LLM context | Middle-truncated + pruned | Current turn | What the LLM reasons over |
| Intaris event | Middle-truncated preview (~50K) | Session lifetime | Compaction, audit, replay |
| ToolOutputStore | Full executor output | 24h (configurable) | LLM exploration via tools (filesystem or S3 backend) |
| WebSocket | Head-truncated (10KB) | Ephemeral | UI display |

#### Layer 3: Post-Execution Content Evaluation (Phase 2)

For high-risk tool categories, pass tool results through Intaris for
content evaluation before injecting into the LLM context:

```
Tool executes → result
  │
  ├─ Low-risk tool (read_only=true, known schema) → inject directly
  │
  └─ High-risk tool (bash, web_fetch, external API) →
       Intaris content_evaluate(result) →
         CLEAN → inject
         SUSPICIOUS → inject with warning prefix
         DANGEROUS → replace with "[content blocked by safety policy]"
```

This requires an Intaris enhancement (`POST /api/v1/content_evaluate` or
similar). Not MVP, but the architecture should accommodate it.

#### Layer 4: Structured Output Contracts (Ongoing)

Where possible, prefer tools that return structured data (JSON with a
known schema) over raw text. Structured outputs have a smaller injection
surface because the data follows a predictable format.

### MVP Requirements

- Structural wrapping of all tool results and memory context (Layer 1).
- Tool output size limits with truncation (Layer 2).
- Layers 3 and 4 are Phase 2+ improvements.
- The system prompt should include a note that tool results and memory
  content are untrusted external data.
