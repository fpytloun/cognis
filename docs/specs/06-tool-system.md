# Cognis: Tool System

## Overview

Tools are how agents interact with the world. The controller's **Tool Router**
categorizes each tool call and routes it to the appropriate handler. The
executor handles all actual tool execution — the controller never runs tools.

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
  └─ Local (filesystem/, shell, local MCP, built-in)
       → Controller evaluates via Intaris
       → If approved: Controller sends tool.execute to Executor
       → Executor runs tool, returns result
       → Controller records result to Intaris events
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

class ToolSource(BaseModel):
    type: str                          # "builtin", "local_mcp", "intaris_mcp", "skill", "plugin"
    server_name: str | None = None
    skill_id: str | None = None
```

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

## MCP Integration

### Local MCP (Executor-Managed)

Local MCP servers run on the executor. The executor starts and manages these
processes.

```yaml
# In agent definition
tools:
  mcp_servers:
    - name: "filesystem"
      transport: "stdio"
      command: "npx"
      args: ["@modelcontextprotocol/server-filesystem", "/workspace"]
    - name: "postgres"
      transport: "stdio"
      command: "npx"
      args: ["@modelcontextprotocol/server-postgres"]
      env:
        DATABASE_URL: "${secret:postgres_url}"
```

Flow: Controller evaluates via Intaris → approved → tool.execute to Executor →
Executor calls local MCP server → result back to Controller.

### Intaris-Managed MCP (Remote)

Remote MCP servers registered in Intaris. Intaris acts as MCP proxy,
evaluating safety AND executing in one call.

```yaml
tools:
  intaris_mcp_servers: ["github", "slack"]
```

Flow: Controller calls Intaris `POST /api/v1/mcp/call` → Intaris evaluates +
proxies to remote MCP server → result back to Controller. Executor not
involved.

### Tool Discovery

At session setup, the controller merges tools from all sources:

```python
available_tools = (
    local_mcp_tools(from executor capabilities)
    + intaris_mcp_tools(from Intaris /mcp/tools)
    + builtin_orchestration_tools
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
["shell", "bash", "write_file", "delete_file", "*/create_*", "*/delete_*"]
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
    "filesystem/read_file": "allow"    # Exact match
    "filesystem/*": "allow"            # Server glob
    "shell": "evaluate"               # Always evaluate
```

## Tool Router

```python
class ToolRouter:
    ORCHESTRATION_TOOLS = {"delegate", "spawn_worker", "fork"}

    async def route(self, tool_call, session, agent, executor):
        # Orchestration → controller handles directly
        if tool_call.name in self.ORCHESTRATION_TOOLS:
            return await self.decision_engine.handle_orchestration(
                session, tool_call
            )

        # Intaris-managed MCP → Intaris proxy
        tool = self.registry.get(tool_call.name)
        if tool and tool.source.type == "intaris_mcp":
            return await self.guardrails.call_mcp_tool(
                session_id=session.intaris_session_id,
                server_name=tool.source.server_name,
                tool_name=tool_call.name.split("/", 1)[1],
                arguments=tool_call.arguments,
            )

        # Local tool → evaluate then execute on executor
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

Skills are lazy-loaded instruction + tool bundles:

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
```

Skills are loaded from `./skills/` or `~/.config/cognis/skills/`.

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
   c. Local tool → evaluate and dispatch (steps 3-6)

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

#### Layer 2: Output Size Limits (MVP)

Cap raw tool output before injection into context:

- Default max tool result size: 50,000 characters (configurable per tool).
- Truncate with notice: `"[truncated: {original_size} chars → {max_size}]"`.
- Large outputs should be stored as artifacts (Mnemory) with a reference
  injected into context instead of the full content.

#### Layer 3: Post-Execution Content Evaluation (Phase 2)

For high-risk tool categories, pass tool results through Intaris for
content evaluation before injecting into the LLM context:

```
Tool executes → result
  │
  ├─ Low-risk tool (read_only=true, known schema) → inject directly
  │
  └─ High-risk tool (shell, web_fetch, external API) →
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
