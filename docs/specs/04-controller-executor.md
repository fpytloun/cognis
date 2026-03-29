# Cognis: Controller-Executor Architecture

## Overview

The controller runs all agent loops — LLM interaction, memory injection,
guardrails evaluation, session management. The executor is a **pure tool
execution sandbox**: it receives tool calls and returns results. It has no
knowledge of memory, guardrails, sessions, or the agent loop.

**Hard rule**: The controller NEVER executes tool calls. All tool execution
goes through an executor, even in the MVP in-process executor.

The executor can optionally provide **local LLM inference** for agents using
local models (e.g., ollama on a Mac Studio).

## Architecture

```
┌──────────────────────────────────────────────────────────────┐
│                     Cognis Controller                         │
│                                                              │
│  Agent Loop Engine         Tool Router                       │
│  ┌──────────────────┐     ┌──────────────────────────────┐  │
│  │ Assemble context  │     │ Orchestration → handle local │  │
│  │ Call LLM          │────►│ Intaris MCP   → Intaris proxy│  │
│  │ Process response  │     │ Local tool    → Executor     │  │
│  │ Record events     │     └────────────────────┬─────────┘  │
│  └──────────────────┘                           │            │
│                                                  │            │
└──────────────────────────────────────────────────┼────────────┘
                                                   │
                                    WebSocket + JSON-RPC
                                                   │
                              ┌─────────────────────┼──────────────┐
                              │                     │              │
                       ┌──────▼──────┐  ┌───────────▼──┐  ┌───────▼─────┐
                       │ InProcess   │  │   Docker     │  │ Kubernetes  │
                       │ Executor    │  │  Executor    │  │  Executor   │
                       │ (MVP)       │  │  (Phase 2)   │  │  (Phase 2)  │
                       │             │  │              │  │             │
                        │ Native tools│  │ Native tools │  │ Native tools│
                        │ + MCP       │  │ + MCP        │  │ + MCP       │
                        │ + opt. LLM  │  │ + opt. LLM   │  │ + opt. LLM  │
                       └─────────────┘  └──────────────┘  └─────────────┘
```

## Executor Provider Interface

```python
class ExecutorProvider(Protocol):
    """Interface for executor backends."""

    async def spawn(self, config: ExecutorConfig) -> ExecutorHandle:
        """Spawn a new executor. Returns handle for communication."""
        ...

    async def get_executor(self, handle: ExecutorHandle) -> ExecutorConnection:
        """Get connection to a running executor."""
        ...

    async def cancel(self, handle: ExecutorHandle) -> None:
        """Cancel a running executor."""
        ...

    async def list_active(self) -> list[ExecutorHandle]:
        """List active executors."""
        ...

    async def cleanup(self) -> None:
        """Clean up stale executors."""
        ...


class ExecutorHandle(BaseModel):
    executor_id: str
    executor_type: str             # "in_process", "subprocess", "docker", "kubernetes"
    started_at: datetime
    capabilities: ExecutorCapabilities
    metadata: dict[str, Any] = {}


class ExecutorCapabilities(BaseModel):
    tools: list[str]               # Available tool names
    inference: bool = False        # Can run LLM inference
    inference_models: list[str] = []  # Available models
    inference_type: str | None = None  # "openai_compatible" | "custom"
```

## Executor Configuration

Everything the executor needs:

```python
class ExecutorConfig(BaseModel):
    """Configuration for an executor (tool sandbox)."""

    executor_id: str

    # Tools
    tools: list[ToolDefinition]         # Available tool definitions
    mcp_servers: list[MCPServerConfig]  # Local MCP server configs
    secrets: dict[str, str]             # For MCP servers (already decrypted)

    # Optional: LLM inference capability
    inference: InferenceConfig | None

    # Connection to controller
    controller_url: str                  # WebSocket URL
    controller_token: str                # Auth token for controller

    # Resource limits
    resource_limits: ResourceLimits | None


class InferenceConfig(BaseModel):
    """Executor-side LLM inference. General-purpose, not just ollama."""

    type: str = "openai_compatible"
    # "openai_compatible" — any endpoint speaking OpenAI API format
    #   (ollama, vllm, llama.cpp, LiteLLM proxy, self-hosted, any cloud)
    # "custom" — executor handles LLM internally (Claude Code, Opencode, etc.)

    # For openai_compatible:
    endpoint: str | None = None       # e.g., "http://localhost:11434/v1"
    api_key_secret: str | None = None # Secret name (resolved from SecretsProvider)
    default_model: str | None = None
    models: list[str] = []
    provider_hint: str | None = None  # LiteLLM provider hint (e.g., "ollama")

    # For custom: no endpoint — the executor implementation handles LLM internally
```

### Secret Lifecycle

Secrets are injected into the executor at spawn time via
`ExecutorConfig.secrets` (already decrypted by the controller from the
SecretsProvider). The executor never contacts the secrets store directly.

| Executor Type | Secret Delivery | Secret Lifetime | Cleanup |
|---------------|----------------|-----------------|---------|
| **In-process** | In-memory dict | Cleared on executor cleanup | Controller calls `cleanup()` |
| **Subprocess** | Environment variables or stdin config | Process lifetime | Secrets die with the process on exit |
| **Docker** | Environment variables | Container lifetime | Container destroyed after work; secrets not persisted to image |
| **Kubernetes** | K8s Secrets or environment | Pod lifetime | Pod terminated after work |

Rules:
- **No secret persistence on executor side.** Executors must not write
  secrets to disk, logs, or shared storage.
- **Executor reuse:** In-process executor may be reused across sessions
  (MVP acceptable — same process anyway). Subprocess/Docker/K8s executors
  are per-delegation: new process/container per delegated task.
- **Phase 2: Pull-based secrets.** For remote executors, consider
  Vault/KMS integration where the executor pulls secrets at runtime using
  a short-lived token, rather than receiving plaintext from the controller.

Note what is NOT in ExecutorConfig: no agent definition, no delegation info,
no session IDs, no service tokens for Mnemory/Intaris. The executor knows
nothing about agents, sessions, or external services. It executes tools and
optionally provides LLM inference.

## JSON-RPC Protocol

Bidirectional JSON-RPC 2.0 over WebSocket between controller and executor.

### Connection Lifecycle

```
1. Controller spawns executor (process/container/pod)
2. Executor connects to controller via WebSocket
3. Executor sends executor.ready with capabilities
4. Controller dispatches tool calls as needed
5. Executor returns results
6. On shutdown: executor.cancel or graceful disconnect
```

### Controller → Executor

```python
# Execute a tool call
"tool.execute" → {
    "call_id": str,
    "tool_name": str,
    "arguments": dict,
    "timeout_seconds": int
}
# → {"call_id": str, "output": str|dict, "is_error": bool, "duration_ms": int}

# Cancel a running tool call
"tool.cancel" → {
    "call_id": str
}

# LLM completion (only for inference-capable executors)
"llm.complete" → {
    "request_id": str,
    "messages": list[dict],
    "model": str,
    "tools": list[dict] | None,
    "temperature": float,
    "max_tokens": int,
    "stream": bool
}
# If stream=true, executor sends llm.chunk notifications, then llm.done

# Shut down executor
"executor.cancel" → {
    "reason": str
}
```

### Executor → Controller

```python
# Registration on connect
"executor.ready" → {
    "executor_id": str,
    "capabilities": {
        "tools": list[str],        # Available tool names
        "inference": bool,
        "inference_models": list[str]
    }
}
# → {"status": "registered"}

# Tool execution result (response to tool.execute)
# Returned as JSON-RPC response to the tool.execute request

# Tool progress (long-running tools)
"tool.progress" → {
    "call_id": str,
    "output_chunk": str
}

# LLM streaming chunk (for local inference)
"llm.chunk" → {
    "request_id": str,
    "content": str | None,
    "tool_calls": list[dict] | None,
    "index": int
}

# LLM completion done (for local inference)
"llm.done" → {
    "request_id": str,
    "usage": {"prompt_tokens": int, "completion_tokens": int},
    "finish_reason": str
}

# Heartbeat
"executor.heartbeat" → {
    "uptime_seconds": float,
    "active_calls": int
}
```

### Error Handling

| Scenario | Behavior |
|----------|----------|
| Executor fails to connect within 30s | Mark failed, retry spawn |
| WebSocket disconnects during tool execution | Wait for reconnect (30s), then fail the tool call |
| Tool exceeds timeout | Controller sends tool.cancel, waits 10s, force-kills |
| Executor exceeds resource limits | Executor self-enforces and reports error |
| Controller goes down | Executor detects disconnect, exits cleanly |

## Executor Implementations

### InProcess Executor (MVP)

Runs tools as async tasks in the controller's process. Uses the same
JSON-RPC interface but communicates via direct async function calls instead
of a real WebSocket. This ensures the same code path as remote executors.

The in-process executor includes **native tool handlers** for common
developer operations (filesystem, search, shell, web). These execute
directly in the process without MCP overhead.

```python
class InProcessExecutor(ExecutorProvider):
    """In-process executor for MVP. Same interface, no network."""

    async def spawn(self, config: ExecutorConfig) -> ExecutorHandle:
        # Register native executor tools (read, write, edit, bash, etc.)
        native_handlers = build_executor_tool_handlers()

        # Initialize MCP servers
        tools = ToolRegistry(config.tools, config.mcp_servers, config.secrets)
        await tools.initialize()

        # Create in-process bridge (mimics WebSocket JSON-RPC)
        bridge = InProcessBridge(tools, native_handlers, config.inference)

        return ExecutorHandle(
            executor_id=f"inproc-{uuid4().hex[:8]}",
            executor_type="in_process",
            capabilities=bridge.capabilities,
        )
```

### Executor Configuration Table

Each executor has a DB-managed configuration that declares which tools
it supports:

```sql
CREATE TABLE executors (
    executor_id         TEXT PRIMARY KEY,
    name                TEXT NOT NULL,
    executor_type       TEXT NOT NULL DEFAULT 'in_process',
    labels              JSON,              -- k8s-style labels for agent matching
    enabled_tools       JSON,              -- ["read", "glob"] or ["*"]
    enabled_tool_groups JSON,              -- ["filesystem", "search"]
    config              JSON,              -- type-specific config
    status              TEXT NOT NULL DEFAULT 'active',
    is_default          INTEGER NOT NULL DEFAULT 0,
    owner_email         TEXT REFERENCES users(email),
    created_at          TIMESTAMP NOT NULL,
    updated_at          TIMESTAMP NOT NULL
);
```

A tool is enabled on an executor if:
- `"*"` is in `enabled_tools`, OR
- `tool.name` is in `enabled_tools`, OR
- `tool.category` is in `enabled_tool_groups`

The default in-process executor is created on first start with
`enabled_tools=[]` (no tools enabled). Users enable tools via the
Executors settings tab.

### Executor Selection

Agent `execution` config specifies executor preferences:

```python
class AgentExecutionConfig(BaseModel):
    executor_id: str | None = None         # Explicit executor
    executor_selector: dict[str, str] = {} # Label matching
    timeout_seconds: int = 300
```

Resolution order:
1. If `executor_id` is set → use that specific executor
2. If `executor_selector` is set → find executor matching all labels
3. Else → use the default executor (`is_default=True`)
4. Among matching executors, verify the required tool is enabled
5. If no match → error: "No executor available for tool X"

### Native Tool Dispatch

When the executor receives a `tool.execute` request, it checks native
handlers first, then falls back to MCP dispatch:

```python
async def tool_execute(self, tool_call):
    # 1. Check native executor tools
    native_handler = self.native_handlers.get(tool_call.name)
    if native_handler is not None:
        return await native_handler(tool_call.arguments, context)

    # 2. Fall back to MCP server dispatch
    registered_tool = self.registry.get(tool_call.name)
    if registered_tool and registered_tool.handler:
        return await registered_tool.handler(tool_call.arguments, context)

    return ToolResult(output="Tool not available on this executor.", is_error=True)
```

Native tools include: `read`, `write`, `edit`, `patch`, `multiedit`,
`list_directory`, `glob`, `grep`, `bash`, `web_fetch`. See
[06-tool-system.md](06-tool-system.md) for full definitions.

### Subprocess Executor

Runs as a separate Python process on the same machine:

```python
class SubprocessExecutor(ExecutorProvider):
    async def spawn(self, config: ExecutorConfig) -> ExecutorHandle:
        process = await asyncio.create_subprocess_exec(
            sys.executable, "-m", "cognis.executor",
            "--config-json", config.model_dump_json(),
            "--controller-url", config.controller_url,
        )
        ...
```

### Docker Executor (Phase 2)

```python
class DockerExecutor(ExecutorProvider):
    async def spawn(self, config: ExecutorConfig) -> ExecutorHandle:
        container = await self.docker.containers.run(
            image=self.config.image,
            command=["python", "-m", "cognis.executor"],
            environment=self._build_env(config),
            detach=True,
            network=self.config.network,
        )
        ...
```

### Kubernetes Executor (Phase 2)

Creates K8s Jobs with node selectors.

## Node Groups and Label Selectors (Phase 2)

Executor pools are configured via the database (API/UI managed):

```json
[
  {
    "name": "default",
    "type": "subprocess",
    "labels": {"tier": "standard"},
    "max_concurrent": 5
  },
  {
    "name": "gpu",
    "type": "kubernetes",
    "namespace": "cognis-gpu",
    "labels": {"gpu": "true"},
    "max_concurrent": 2
  }
]
```

Agents specify `execution.node_selector` to match pools.

## Executor-Side LLM Inference

Executor-side LLM is a **first-class, general-purpose capability** — not an
edge case. The LLM Router (in the controller) detects when an agent's
provider has `location: "executor"` and routes `llm.complete` calls to a
matching executor via WebSocket JSON-RPC.

Covers:
- **Local models**: ollama, vllm, llama.cpp
- **Self-hosted proxies**: LiteLLM proxy, OpenRouter on local network
- **Custom executors**: Claude Code (with user's Claude subscription),
  Opencode, or any custom implementation that handles LLM internally
- **Network-optimized**: executor co-located with LLM provider
- **Air-gapped**: no cloud access from controller

```
Controller-side provider (default):
  LLM Router → LiteLLM / direct SDK → Cloud Provider

Executor-side provider (openai_compatible):
  LLM Router → Executor (llm.complete via WS) → local endpoint (ollama, etc.)

Executor-side provider (custom):
  LLM Router → Executor (llm.complete via WS) → executor's internal LLM
```

The controller still handles memory injection and guardrails evaluation. The
executor just runs the LLM inference. The agent loop is identical regardless
of where inference happens.

Executor-provided models are available to **all Cognis tasks**, not just the
agent that declared them. Internal tasks (compaction, classification) can use
executor-hosted models via the LLM Router's model routing policy.

### Deployment: Cloud Controller + Local Executor

```
┌─────────────────────┐         ┌──────────────────────────┐
│  Cloud Controller   │         │  Mac Studio              │
│                     │         │                          │
│  Agent loops        │◄══WS══►│  Executor                │
│  Memory (Mnemory)   │         │  ├─ MCP servers          │
│  Guard (Intaris)    │         │  ├─ Filesystem access    │
│  API + Web UI       │         │  ├─ Shell                │
│  Cloud LLM (opt.)   │         │  └─ LLM (ollama/vllm/   │
│                     │         │       custom/Claude Code) │
└─────────────────────┘         └──────────────────────────┘
```

### Custom Executor Implementations

Anyone can build a custom executor that provides both tool execution and LLM
inference. Examples:

```python
class ClaudeCodeExecutor(ExecutorProvider):
    """Claude Code as executor. Uses user's Claude subscription."""
    # Implements tool.execute (via Claude Code's built-in tools)
    # Implements llm.complete (via Claude API with user's subscription)
    # Cognis controller handles memory, guardrails, sessions
    ...

class OpencodeExecutor(ExecutorProvider):
    """Opencode as executor."""
    # Bridges Opencode's tool execution to Cognis JSON-RPC
    # Optionally exposes Opencode's LLM calls via llm.complete
    ...
```

## Controller-Side Dispatch

```python
class ToolDispatcher:
    """Dispatches tool calls to the appropriate executor."""

    async def execute(
        self,
        tool_call: ToolCall,
        session: Session,
        executor: ExecutorConnection,
    ) -> ToolResult:
        """Execute a tool call on an executor."""
        result = await executor.rpc_call("tool.execute", {
            "call_id": tool_call.id,
            "tool_name": tool_call.name,
            "arguments": tool_call.arguments,
            "timeout_seconds": self.config.tool_timeout,
        })
        return ToolResult(
            tool_name=tool_call.name,
            output=result["output"],
            is_error=result["is_error"],
            duration_ms=result["duration_ms"],
        )
```

## Replaceability

The entire executor layer is replaceable. To use Opencode as an executor:

```python
class OpencodeExecutor(ExecutorProvider):
    """Use Opencode as the execution backend."""

    async def spawn(self, config: ExecutorConfig) -> ExecutorHandle:
        # Start Opencode session
        # Bridge Opencode's tool execution to Cognis JSON-RPC
        ...
```

The controller doesn't care how the executor works internally — only that it
implements the JSON-RPC protocol for `tool.execute` (and optionally
`llm.complete`).

## Scalability Path

```
Phase 1 (MVP):
  Single controller, in-process executor
  All in one Python process

Phase 2:
  Controller + subprocess/Docker/K8s executors
  Multiple executor pools with label selectors

Phase 3:
  Multiple controller instances (sticky WebSocket sessions)
  Decompose: cognis-api, cognis-orchestrator, cognis-inference
  Each component scales independently
```
