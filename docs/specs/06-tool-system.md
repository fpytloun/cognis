# Cognis: Tool System

## Overview

Tools are how agents interact with the world. The controller's **Tool Router**
categorizes each tool call and routes it to the appropriate handler. The
executor handles all actual tool execution — the controller never runs tools.

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
  ├─ Orchestration (delegate, spawn_worker, fork)
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
    server_name: str | None = None
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

# patch — Apply a unified diff patch
patch_tool = ToolDefinition(
    name="patch",
    description="Apply a unified diff patch to one or more files.",
    parameters={
        "type": "object",
        "properties": {
            "patch_text": {"type": "string", "description": "Unified diff patch text"},
        },
        "required": ["patch_text"],
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
            "include": {"type": "string", "description": "File pattern filter (e.g. '*.py')"},
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
        },
        "required": ["command"],
    },
    source=ToolSource(type="executor"),
    category="shell",
    read_only=False,
    non_bypassable=True,
    timeout_seconds=120,
)
```

### Web Tools

```python
# web_fetch — Fetch URL content
web_fetch_tool = ToolDefinition(
    name="web_fetch",
    description="Fetch content from a URL and return it as text or markdown.",
    parameters={
        "type": "object",
        "properties": {
            "url": {"type": "string", "description": "URL to fetch"},
            "format": {"type": "string", "enum": ["text", "markdown", "html"]},
            "timeout": {"type": "integer", "description": "Timeout in seconds (max 120)"},
        },
        "required": ["url"],
    },
    source=ToolSource(type="executor"),
    category="web",
    read_only=True,
    timeout_seconds=60,
)
```

### Safety Classification

| Tool | read_only | non_bypassable | Rationale |
|------|-----------|----------------|-----------|
| `read` | yes | no | Read-only filesystem access |
| `write` | no | **yes** | Creates/overwrites files |
| `edit` | no | **yes** | Modifies file content |
| `patch` | no | **yes** | Modifies files via diff |
| `multiedit` | no | **yes** | Modifies file content |
| `list_directory` | yes | no | Read-only directory listing |
| `glob` | yes | no | Read-only file search |
| `grep` | yes | no | Read-only content search |
| `bash` | no | **yes** | Arbitrary command execution |
| `web_fetch` | yes | no | Read-only URL fetching |

Write operations always go through Intaris evaluation regardless of agent
permissions. Read-only tools can be auto-approved by agent permission matrix.

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

# spawn_worker — Request lightweight worker for focused task
spawn_worker_tool = ToolDefinition(
    name="spawn_worker",
    description="Spawn a lightweight worker for focused tasks.",
    parameters={
        "type": "object",
        "properties": {
            "task": {"type": "string"},
            "worker_type": {"type": "string", "enum": ["research", "summarize", "code", "general"]},
        },
        "required": ["task"],
    },
    source=ToolSource(type="builtin"),
)

# fork — Request isolated child session (same agent)
fork_tool = ToolDefinition(
    name="fork",
    description="Fork into isolated child session for exploration.",
    parameters={
        "type": "object",
        "properties": {
            "reason": {"type": "string"},
            "context_summary": {"type": "string"},
        },
        "required": ["reason"],
    },
    source=ToolSource(type="builtin"),
)
```

These tools submit **requests** to the Decision Engine, which approves,
modifies, or rejects them. The LLM cannot force delegation.

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

### Local MCP (Executor-Managed)

Local MCP servers run on the executor. The executor starts and manages these
processes.

```yaml
# In agent definition
tools:
  mcp_servers:
    - name: "postgres"
      transport: "stdio"
      command: "npx"
      args: ["@modelcontextprotocol/server-postgres"]
      env:
        DATABASE_URL: "${secret:postgres_url}"
```

Flow: Controller evaluates via Intaris → approved → tool.execute to Executor →
Executor calls local MCP server → result back to Controller.

Note: Common developer tools (filesystem, shell, search) are now executor-
native and do not require MCP server configuration. Local MCP is for
specialized servers (databases, custom APIs, etc.).

### Intaris-Managed MCP (Remote)

Remote MCP servers registered in Intaris. Intaris acts as MCP proxy,
evaluating safety AND executing in one call.

```yaml
tools:
  intaris_mcp_servers: ["github", "slack"]
```

Available Intaris MCP servers are auto-discovered via `GET /api/v1/mcp/servers`
on Intaris. The agent form shows a multi-select dropdown of discovered servers.
Server configuration (credentials, endpoints) is managed on the Intaris side.

Flow: Controller calls Intaris `POST /api/v1/mcp/call` → Intaris evaluates +
proxies to remote MCP server → result back to Controller. Executor not
involved.

### Tool Discovery

At session setup, the controller merges tools from all sources:

```python
available_tools = (
    executor_native_tools           # Always available (opt-out per agent)
    + builtin_orchestration_tools   # delegate, spawn_worker, fork
    + builtin_system_tools          # list_agents, get_status
    + local_mcp_tools(from executor capabilities)
    + intaris_mcp_tools(from Intaris /mcp/tools)
    + skill_tools(from active skills)
)
```

The LLM sees a flat list. It doesn't know where tools execute.

## Tool Permission Evaluation

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
["bash", "write", "edit", "patch", "multiedit", "*/create_*", "*/delete_*"]
```

This prevents an agent with `"*": "allow"` from bypassing safety checks on
destructive operations.

### MCP Tool Naming

Local MCP tools are namespaced: `{server_name}/{tool_name}`. Permission
matching supports both exact names and glob patterns:

```yaml
permissions:
  tool_permissions:
    "*": "evaluate"                    # Default
    "read": "allow"                    # Executor-native read
    "glob": "allow"                    # Executor-native glob
    "grep": "allow"                    # Executor-native grep
    "postgres/query": "evaluate"       # MCP tool
    "bash": "evaluate"                 # Always evaluate
```

## Tool Router

```python
class ToolRouter:
    ORCHESTRATION_TOOLS = {"delegate", "spawn_worker", "fork"}
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
                tool_name=tool_call.name.split("/", 1)[1],
                arguments=tool_call.arguments,
            )

        # Executor-native and local MCP → evaluate then execute on executor
        decision = await self.evaluate_tool_call(tool_call, agent, session)
        if decision.decision == "approve":
            return await executor.tool_execute(tool_call)
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

## Skill System

Skills are DB-managed instruction + tool bundles with import/export support.
Agents can also manage skills via a built-in LLM tool.

```python
class Skill(BaseModel):
    skill_id: str
    name: str
    description: str
    instructions: str              # Markdown, injected into context
    tools: list[ToolDefinition] = []
    prompt_templates: dict[str, str] = {}
    source: str                    # "db", "file"
    auto_load: bool = False
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
    tags        JSON,
    auto_load   INTEGER NOT NULL DEFAULT 0,
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

### Filesystem Skills (read-only)

Skills can also be loaded from `./skills/` or `~/.config/cognis/skills/`
as read-only definitions. These are synced to the DB on startup with
`source="file"` and cannot be edited via the UI.

```
skills/
  git-release/
    SKILL.md          # Instructions + tool definitions in frontmatter
    templates/
      release-notes.md
```

## Complete Tool Call Flow

```
1. LLM generates tool_call(name, arguments)

2. Tool Router categorizes:
   a. Orchestration → handle as session op (step 2a)
   b. Intaris MCP → Intaris proxy (step 2b)
   c. Executor-native or Local MCP → evaluate and dispatch (steps 3-6)

   2a. Decision Engine approves/modifies → create session, start agent loop
   2b. Intaris evaluates + executes → return result to LLM

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
| ToolOutputStore | Full executor output | 24h (configurable) | LLM exploration via tools |
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
