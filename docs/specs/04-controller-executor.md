# Cognis: Controller-Executor Architecture

## Overview

The controller runs all workflow orchestration, session management, tool
policy, task state, notifications, and delivery. Executors host two kinds of
capability:

1. **Tool execution** — the classic executor role
2. **Runtime hosting** — long-lived, stateful external agent runtimes such as
   Claude Code

For native Cognis agents, an executor behaves like a tool sandbox. For
external runtimes, the executor is a **runtime host** with bounded persistent
state for that runtime only. It still does not own workflows, tasks, or Cognis
conversation state.

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
│  Inference Router (for location="executor" providers)        │
│  ┌──────────────────────────────────────────────────────┐    │
│  │ Resolve provider → match executor by labels → proxy  │    │
│  └──────────────────────────────────────┬───────────────┘    │
└──────────────────────────────────────────┼────────────────────┘
                                           │
                            WebSocket + JSON-RPC 2.0
                            (permessage-deflate, JWT auth)
                                           │
              ┌────────────────────────────┼────────────────────┐
              │                            │                    │
       ┌──────▼──────┐  ┌─────────────────▼──┐  ┌──────────────▼──┐
       │ InProcess   │  │  WebSocket (Remote) │  │  Subprocess     │
       │ Executor    │  │  Executor           │  │  Executor       │
       │             │  │                     │  │                 │
       │ Native tools│  │  Native tools       │  │  Native tools   │
       │ + MCP       │  │  + MCP              │  │  + MCP          │
       │             │  │  + LLM proxy        │  │  + LLM proxy    │
       └─────────────┘  └─────────────────────┘  └─────────────────┘
                         (any machine, wss://)    (local subprocess)
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

    async def health(self) -> ProviderHealth:
        """Report provider health status."""
        ...


class ExecutorHandle(BaseModel):
    executor_id: str
    executor_type: str             # "in_process", "subprocess", "websocket"
    started_at: datetime
    capabilities: ExecutorCapabilities
    status: str = "ready"          # "pending", "ready", "disconnected"
    metadata: dict[str, Any] = {}


class ExecutorCapabilities(BaseModel):
    tools: list[str]               # Available tool names
    inference: bool = False        # Can proxy LLM inference
    inference_models: list[str] = []  # Available models (informational)
    inference_type: str | None = None  # "litellm_proxy"
```

## Executor Configuration

The executor is a **stateless remote hand** — it only needs a controller URL
and a JWT token to start.  All tool, MCP, and inference configuration is
managed on the controller side (DB + UI) and pushed to the executor via the
``executor.configure`` message after authentication.

```python
class ExecutorConfig(BaseModel):
    """Configuration for an executor (tool sandbox)."""

    executor_id: str

    # Tools (populated by controller via executor.configure)
    tools: list[ToolDefinition] = []
    mcp_servers: list[MCPServerConfig] = []
    secrets: dict[str, str] = {}

    # Connection to controller (only needed for remote/subprocess executors)
    controller_url: str | None = None    # WebSocket URL
    controller_token: str | None = None  # JWT auth token

    # Resource limits
    resource_limits: ResourceLimits | None = None

    # Internal routing metadata
    metadata: dict[str, Any] = {}        # e.g. {"executor_type": "websocket"}
```

Note: ``InferenceConfig`` is no longer used by the executor.  LLM inference
configuration lives on the ``LLMProviderConfig`` (see 05-integrations.md).
When a provider has ``location="executor"``, the controller sends the fully
resolved model string and LiteLLM kwargs in each ``llm.complete`` call.  The
executor runs the matching LiteLLM transport locally as a transparent proxy:

- ``litellm.acompletion()`` for canonical chat-completions models
- ``litellm.aresponses()`` for OpenAI Responses-capable models when the
  controller-side rollout flag enables the bridge

The executor normalizes all inference output back into Cognis' canonical
chat-like chunk/result shape before it crosses the WebSocket boundary, so the
controller and executor paths remain behaviorally aligned.

### Secret and Runtime State Lifecycle

Secrets are injected into the executor at spawn time via
`ExecutorConfig.secrets` (already decrypted by the controller from the
SecretsProvider). The executor never contacts the secrets store directly.

| Executor Type | Secret Delivery | Secret Lifetime | Cleanup |
|---------------|----------------|-----------------|---------|
| **In-process** | In-memory dict | Cleared on executor cleanup | Controller calls `cleanup()` |
| **Subprocess** | JWT token via stdin (never CLI args) | Process lifetime | Secrets die with the process on exit |
| **WebSocket (remote)** | Via encrypted WS after auth (executor.configure) | Connection lifetime | Cleared on disconnect |
| **Docker** (planned) | Environment variables | Container lifetime | Container destroyed after work |
| **Kubernetes** (planned) | K8s Secrets or environment | Pod lifetime | Pod terminated after work |

Rules:
- **No controller-injected secret persistence on executor side.** Executors
  must not write Cognis-injected secrets to disk, logs, or shared storage.
- **Executor reuse:** In-process executor may be reused across sessions
  (MVP acceptable — same process anyway). Subprocess/Docker/K8s executors
  are per-delegation: new process/container per delegated task.
- **Phase 2: Pull-based secrets.** For remote executors, consider
  Vault/KMS integration where the executor pulls secrets at runtime using
  a short-lived token, rather than receiving plaintext from the controller.

Exception for runtime-hosted auth:

- Executor-hosted runtimes such as `claude_code` may persist **runtime-native
  auth and session state** inside an isolated runtime root owned by the
  executor. This is not treated as Cognis secret persistence. It must remain
  scoped to the owning user/runtime, never be written to logs, and be covered
  by explicit retention and cleanup rules.

Note what is NOT in ExecutorConfig for normal tool execution: no workflow
graph, no task state authority, no delivery state, and no general-purpose
service ownership for Mnemory or Intaris.

Runtime-hosting exception:

- a runtime host may know the runtime run ID, Cognis agent ID, and runtime
  session metadata required to resume a managed external session
- this does **not** make the executor the owner of workflows, tasks, or
  conversation state

## JSON-RPC Protocol

Bidirectional JSON-RPC 2.0 over WebSocket between controller and executor.

### Connection Lifecycle

```
1. Admin creates executor in UI (name, type, labels, enabled tools)
2. Admin generates a JWT token (POST /api/v1/executors/{id}/token)
3. Executor process starts: cognis-executor --controller-url wss://... --token <jwt>
4. Executor connects to WS /api/executor/ws (permessage-deflate)
5. Executor sends executor.ready with JWT token + platform info + optional environment snapshot
6. Controller validates JWT (aud=cognis-executor, sub=executor_id)
7. Controller looks up executor config from DB
8. Controller increments desired config version and sends executor.configure with enabled tools/groups, executor-assigned MCP servers, scoped secrets, and executor runtime config (including LSP/browser settings)
9. Executor applies the config, starts/refreshes MCP clients, refreshes executor-local runtime helpers such as LSP managers, and ACKs with applied version + observed tool manifest + refreshed environment snapshot
10. Controller persists the observed manifest and marks executor as ready
11. Controller dispatches tool.execute / llm.complete as needed
12. Executor sends executor.heartbeat every 15 seconds
13. On shutdown: executor.cancel or graceful disconnect
```

For subprocess executors, steps 1-3 are automated: the controller spawns
``python -m cognis.executor`` with a short-lived JWT (5 min) piped via stdin.

### Controller → Executor

```python
# Push configuration after authentication (mandatory before tool.execute)
"executor.configure" → {
    "config_version": int,          # Strictly increasing per executor
    "enabled_tools": list[str],       # Tool names or ["*"]
    "enabled_tool_groups": list[str], # Tool categories
    "config": dict,                   # Type-specific config from DB
    "mcp_servers": list[dict],        # Resolved stdio MCP server configs
    "secrets": dict[str, str]         # Only secrets needed by those MCP servers
}
# → {"status": "configured", "applied_version": int, "ready": bool,
#    "observed_at": str, "observed_tools": [...], "capabilities": {...},
#    "config_keys": [...], "environment": {...}}

# List available tools on the executor
"tool.list" → {}
# → {"tools": [ToolDefinition, ...]}

# Execute a tool call
"tool.execute" → {
    "call_id": str,
    "tool_name": str,
    "arguments": dict,
    "timeout_seconds": int
}

# Inspect executor-local LSP status (best-effort, read-only)
"lsp.status" → {}
# → {
#   "supported": bool,
#   "enabled": bool,
#   "executor_id": str | null,
#   "executor_type": str | null,
#   "state": "ready" | "disabled" | "unsupported" | "unavailable",
#   "config": {...},
#   "active_servers": [...],
#   "broken_servers": [...],
#   "spawning_count": int,
#   "totals": {...},
#   "available_servers": [...],
#   "warnings": list[str]
# }

# Start a runtime-backed turn or step
"runtime.start" → {
    "runtime_run_id": str,
    "runtime_type": str,             # "claude_code"
    "run_kind": str,                 # "direct_turn" | "workflow_step"
    "agent_id": str,
    "acting_user_email": str,
    "conversation_id": str | null,
    "task_id": str | null,
    "step_run_id": str | null,
    "last_event_seq": int,
    "config": dict,
    "input": dict
}
# → {"status": "started", "runtime_run_id": str, "external_session_id": str,
#    "accepted_event_seq": int}

# Resume an existing runtime lease
"runtime.resume" → {
    "runtime_run_id": str,
    "runtime_type": str,
    "acting_user_email": str,
    "last_event_seq": int,
    "response": dict | null
}
# → {"status": "resumed", "runtime_run_id": str, "accepted_event_seq": int}

# Submit a response to a pending runtime question/approval
"runtime.respond" → {
    "runtime_run_id": str,
    "request_id": str,
    "response": dict
}
# → {"status": "accepted"}

# Cancel a runtime lease
"runtime.cancel" → {
    "runtime_run_id": str,
    "reason": str | null
}
# → {"status": "cancelled" | "already_completed"}

# Inspect runtime status
"runtime.status" → {
    "runtime_run_id": str
}
# → {"status": str, "external_session_id": str | null,
#    "last_event_seq": int, "pending_request_id": str | null,
#    "recoverable": bool}

# Collect buffered runtime events after seq
"runtime.collect_events" → {
    "runtime_run_id": str,
    "after_seq": int
}
# → {"events": [{"seq": int, "type": str, "payload": dict, "ts": str}],
#    "current_status": str, "has_more": bool}
# → {"call_id": str, "output": str|dict, "is_error": bool, "duration_ms": int}

# Cancel a running tool call
"tool.cancel" → {
    "call_id": str
}

# LLM completion — proxy through executor's local LiteLLM
# The controller resolves the provider, model prefix, and credentials,
# then sends the fully resolved call for the executor to proxy.
"llm.complete" → {
    "request_id": str,
    "messages": list[dict],
    "model": str,                     # Prefixed model string (e.g. "ollama/llama3.2")
    "request_kwargs": dict            # All LiteLLM kwargs (api_key, api_base, etc.)
}
# Executor acknowledges with {"status": "streaming"}, then sends
# llm.chunk notifications, then llm.done.

# Shut down executor
"executor.cancel" → {
    "reason": str
}
```

### Executor → Controller

```python
# Authentication on connect (first message, before any other exchange)
"executor.ready" → {
    "token": str,                  # JWT (aud=cognis-executor, sub=executor_id)
    "environment": {               # Optional for backward compatibility
        "user": str,
        "home": str,
        "cwd": str,
        "hostname": str,
        "source": str,
        "observed_at": str         # UTC ISO-8601 snapshot time
    },
    "platform": {
        "os": str,                 # e.g. "darwin", "linux"
        "arch": str,               # e.g. "arm64", "x86_64"
        "python": str              # e.g. "3.12.4"
    }
}
# Controller validates JWT, looks up executor in DB, then sends
# executor.configure.  After configuration, controller responds:
# → {"status": "registered", "executor_id": str}

# executor.configure result also includes the normalized environment
# snapshot when available so the controller can refresh prompt/runtime
# guidance after configuration or reconnect. The runtime metadata may
# advertise configure capabilities such as:
# - mcp_runtime_status_v1
# - lsp_status_v1
#
# The controller must only call lsp.status when lsp_status_v1 is advertised.
# If the capability is absent, the executor is treated as unsupported for
# LSP status reporting.

# Tool execution result (response to tool.execute)
# Returned as JSON-RPC response to the tool.execute request

# Tool progress (long-running tools)
"tool.progress" → {
    "call_id": str,
    "output_chunk": str
}

# LLM streaming chunk (proxied inference)
"llm.chunk" → {
    "request_id": str,
    "content": str | None,
    "tool_calls": list[dict] | None,
    "reasoning_content": str | None,  # Extended thinking / reasoning tokens
    "index": int
}

# LLM completion done (proxied inference)
"llm.done" → {
    "request_id": str,
    "usage": {"prompt_tokens": int, "completion_tokens": int},
    "finish_reason": str,
    "error": str | None            # Non-null on inference failure
}

# Heartbeat (every 15 seconds)
"executor.heartbeat" → {
    "uptime_seconds": int,
    "active_calls": int,
    "configured": bool             # False until executor.configure processed
}
```

### Planned: Channel Adapter Routing via Executor

For adapters that depend on software or network access local to the user's
machine (for example Signal via `signal-cli`, IRC connections, or homeserver-
local Matrix access), the executor is the natural remote hand. The controller
remains stateless and orchestration-only; the executor reuses the same
`cognis.channels.adapters.*` code and hosts the platform connection locally.

This is the target architecture and is documented here before implementation.

#### Location Model

- **`controller`** (default): adapter runs on the controller process. Best for
  webhook-driven or cloud-hosted APIs such as WhatsApp, Slack HTTP events,
  Google Chat, and simple Telegram polling.
- **`executor`**: adapter runs on a connected executor selected by the
  controller. Best for adapters that need user-local services or network
  reachability, such as Signal backed by a user-managed `signal-cli` instance.

The intended per-account configuration is:

```python
channel_account = {
    "adapter_location": "controller",  # or "executor"
    "executor_id": None,  # optional preferred executor ID
}
```

#### Planned JSON-RPC Methods

```python
# Controller → Executor
"channel.start" → {
    "account_id": str,
    "channel_type": str,
    "config": dict,
    "credentials": dict,
}

"channel.stop" → {
    "account_id": str,
}

"channel.send" → {
    "account_id": str,
    "message": dict,
}

"channel.typing" → {
    "account_id": str,
    "chat_id": str,
}

"channel.mark_read" → {
    "account_id": str,
    "chat_id": str,
    "message_id": str,
}

"channel.sync_profile" → {
    "account_id": str,
    "name": str,
    "avatar_b64": str | None,
    "avatar_content_type": str | None,
}

# Executor → Controller (notifications)
"channel.message" → {
    "account_id": str,
    "message": dict,
}

"channel.status" → {
    "account_id": str,
    "status": dict,
}
```

#### Signal Example

Signal supports two transport modes on the executor:

**REST API mode** — the executor talks to an external signal-cli REST API:

```text
1. User runs Cognis controller in the cloud.
2. User runs Cognis executor on a machine they control.
3. User runs signal-cli REST API on that executor machine.
4. Channel account is configured with adapter_location="executor".
5. Controller sends channel.start to the executor.
6. Executor starts the Signal adapter locally and talks to signal-cli REST API.
7. Inbound Signal messages flow back to controller via channel.message.
8. Controller orchestrates the turn and sends outbound replies via channel.send.
```

**Direct JSON-RPC mode** — the executor runs signal-cli directly:

```text
1. User runs Cognis controller in the cloud.
2. User runs Cognis executor on a machine with signal-cli installed.
3. Executor config has signal.direct_enabled=true and optionally signal.command.
4. Channel account is configured with transport="direct_jsonrpc" and adapter_location="executor".
5. Controller sends channel.start to the executor.
6. Executor spawns a signal-cli jsonRpc subprocess and communicates via stdio.
7. Inbound Signal messages flow back to controller via channel.message.
8. Controller orchestrates the turn and sends outbound replies via channel.send.
9. Typing, read receipts, and profile sync flow via channel.typing, channel.mark_read, channel.sync_profile.
```

This keeps the controller free of platform-side connection state while
allowing stateful platform-side services to live next to the executor
controlled by the user.

### Multi-Executor Agent Runtime

Agents resolve to an executor pool instead of a single executor when their
`execution` config includes additional executor bindings. The primary executor
binding remains `execution.executor_id` or `execution.executor_selector`; any
`execution.additional_executors` entries are secondary hands that the agent may
use explicitly.

```python
class ResolvedExecutorTarget(BaseModel):
    executor_id: str
    executor_type: str
    primary: bool
    description: str | None = None
    labels: dict[str, Any] = {}
    runtime_state: str
    connected: bool
    ready: bool
    usable: bool
    unavailable_reason: str | None = None
    observed_tools: list[ToolDefinition] = []
    environment: ExecutorEnvironmentSnapshot | None = None


class ResolvedExecutorPool(BaseModel):
    primary_executor_ids: list[str]
    active_executor_id: str
    executors: dict[str, ResolvedExecutorTarget]
```

The controller owns the pool and active executor pointer. Executors do not know
whether they are primary or additional; they only receive normal `tool.execute`
RPCs. Runtime context and tool routing metadata carry the selected executor ID.

#### Primary Executor Set

- Explicit primary binding resolves to one primary executor.
- Primary label selector may resolve to multiple primary executors; all usable
  matches are valid primary execution targets.
- If the current active executor is primary and usable, it remains active.
- Otherwise the controller selects a usable primary executor by deterministic
  ordering: `active` state before `degraded`, then `executor_id`.
- If no primary executor is usable, the turn fails before model execution with a
  clear user-visible error.

#### Additional Executors

- Additional bindings may be direct executor IDs or label selectors.
- Selector bindings expand to all eligible matching executors.
- Descriptions are included in context to explain why an executor exists, but
  tool routing uses real executor IDs only.
- Additional executors never become primary. If active execution is on an
  additional executor, Cognis injects a reminder to switch back when the work
  on that executor is complete.

#### Executor Availability

An executor is usable for switching and tool execution only when all of these
are true:

- DB `status == "active"`.
- Runtime state is `active` or `degraded`.
- Desired and applied config versions match.
- Remote executors have a live ready WebSocket connection.
- The requested tool is enabled and present in the executor's observed tool
  inventory.
- Deployment policy allows that executor type.

Unavailable executors remain visible in context and UI with their factual state
(`offline`, `stale`, `blocked`, `reconfiguring`, disconnected, policy-denied,
or missing tool), but the controller does not dispatch tool calls to them.

#### Active Executor Recovery

The controller checks the active executor before each model cycle and before
each executor-routed tool call. If the active executor is no longer usable:

1. Switch back to a usable primary executor if one exists.
2. Inject a hidden reminder explaining the forced switch and the previous
   executor state.
3. Continue only if the work can safely continue on the primary executor.
4. If the failed executor is required for the requested work, notify the user
   and cancel the turn instead of continuing on the wrong host.

Reminder shape:

```text
<executor_context_reminder>
The previously active executor "infra-runner" is no longer usable.
Observed state: offline.
The controller switched active execution back to primary executor "local-macbook".
Continue only if the remaining work can safely run on the primary executor.
If this work requires "infra-runner", notify the user and stop the turn.
</executor_context_reminder>
```

The reminder must be factual. It must not speculate about why the executor is
offline.

### Error Handling

| Scenario | Behavior |
|----------|----------|
| Executor fails to connect within 30s | Mark failed, retry spawn |
| WebSocket disconnects during tool execution | Fail the tool call with executor state; do not retry on a different executor unless the agent explicitly requested a different target |
| Tool exceeds timeout | Controller sends tool.cancel, waits 10s, force-kills |
| Executor exceeds resource limits | Executor self-enforces and reports error |
| Controller goes down (tool-only run) | Executor detects disconnect, exits cleanly |
| Controller goes down (runtime-hosted run) | Executor keeps the runtime lease for a grace period, buffers events locally, then marks the run orphaned if the controller does not reclaim it |
| Active additional executor goes offline before next model call | Switch to a usable primary executor, inject a factual reminder, and continue only if safe |
| Required executor is offline or lacks the requested tool | Notify the user and cancel the turn; do not run the work on another host |

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
    supported_runtimes  JSON,              -- ["native", "claude_code"]
    runtime_metadata    JSON,              -- runtime-specific capabilities
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

Agent `execution` config specifies primary and additional executor preferences:

```python
class AgentExecutorBinding(BaseModel):
    executor_id: str | None = None
    executor_selector: dict[str, str] | None = None
    description: str | None = None


class AgentExecutionConfig(BaseModel):
    executor_id: str | None = None          # Primary explicit executor
    executor_selector: dict[str, str] = {}  # Primary label matching
    additional_executors: list[AgentExecutorBinding] = []
    timeout_seconds: int = 300
```

Resolution rules:

1. If primary `executor_id` is set → add that executor to the primary set.
2. If primary `executor_selector` is set → add all eligible matching executors
   to the primary set.
3. Expand `additional_executors` into the additional set by direct ID or label
   selector.
4. Remove duplicates; primary membership wins over additional membership.
5. Mark every resolved executor with availability and observed tool inventory.
6. Select an active primary executor; if none is usable, fail the turn.
7. For each executor-routed tool call, route to `target_executor` when present,
   otherwise to the active executor.

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

Native tools include: `read`, `write`, `edit`, `apply_patch`, `multiedit`,
`list_directory`, `glob`, `grep`, `bash`, `web_fetch`. See
[06-tool-system.md](06-tool-system.md) for full definitions.

### Subprocess Executor

Runs as a separate Python process on the same machine.  The controller
generates a short-lived JWT (5 min) and pipes it via stdin — the token
never appears in CLI arguments or the process listing.

```python
class SubprocessExecutorProvider(ExecutorProvider):
    async def spawn(self, config: ExecutorConfig) -> ExecutorHandle:
        token = self._auth_provider.sign_executor_token(
            config.executor_id, ttl_seconds=300
        )
        process = await asyncio.create_subprocess_exec(
            sys.executable, "-m", "cognis.executor",
            "--controller-url", f"ws://localhost:{port}/api/executor/ws",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        # Token via stdin (never CLI args)
        process.stdin.write(token.encode())
        process.stdin.close()
        # Delegates to WebSocketExecutorProvider for connection lifecycle
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
edge case.  LLM providers are configured normally in the controller (same UI,
same DB table), just with ``location: "executor"`` to route inference through
a matching remote executor instead of calling the API directly from the
controller.

The executor is a **transparent LiteLLM proxy**.  It receives the fully
resolved model string (e.g. ``ollama/llama3.2``) and all LiteLLM kwargs
(``api_key``, ``api_base``, ``temperature``, etc.) in each ``llm.complete``
call and runs ``litellm.acompletion()`` locally.  This means the executor
supports **any provider LiteLLM supports** — not just OpenAI-compatible HTTP
endpoints.

Covers:
- **Local models**: ollama, vllm, llama.cpp (via LiteLLM's provider prefixes)
- **Cloud providers from a different network**: OpenAI, Anthropic, etc. routed
  through an executor for network locality or compliance
- **Self-hosted proxies**: LiteLLM proxy, OpenRouter on local network
- **Air-gapped**: no cloud access from controller, executor has the network path

```
Controller-side provider (default):
  LiteLLMProvider → litellm.acompletion() → Cloud Provider

Executor-side provider (location="executor"):
  LiteLLMProvider → InferenceRouter → executor (llm.complete via WS)
    → executor runs litellm.acompletion() locally → any provider
```

The controller still handles memory injection, guardrails evaluation, and
context assembly.  The executor just proxies the LLM call.  The agent loop
is identical regardless of where inference happens.

Executor-provided models are available to **all Cognis tasks**, not just the
agent that declared them.  Internal tasks (compaction, classification) can use
executor-hosted models via the model routing policy.

### LLM Provider Configuration for Executor Routing

An LLM provider with ``location: "executor"`` is configured identically to a
controller-side provider, plus ``executor_labels`` for matching:

```python
# Example: route OpenAI calls through a local executor
LLMProviderConfig(
    provider_id="local-openai",
    display_name="OpenAI via Local Executor",
    location="executor",              # Route through executor
    backend="litellm",
    litellm_provider="openai",
    api_key_secret="OPENAI_API_KEY",
    executor_labels={"location": "local"},  # Match executor by labels
    default_model="gpt-4o-mini",
    models=[ModelInfo(model_id="gpt-4o-mini"), ModelInfo(model_id="gpt-4o")],
)

# Example: local ollama on a Mac Studio
LLMProviderConfig(
    provider_id="local-ollama",
    display_name="Local Ollama",
    location="executor",
    backend="litellm",
    litellm_provider="ollama",
    executor_labels={"location": "local"},
    default_model="llama3.2",
    models=[ModelInfo(model_id="llama3.2")],
)
```

The ``InferenceRouter`` in the controller finds a connected executor whose
labels match ``executor_labels``, then sends ``llm.complete`` with the
resolved model prefix and all LiteLLM kwargs.  The executor calls
``litellm.acompletion(model="ollama/llama3.2", ...)`` and streams chunks
back.

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

### Executor-Hosted Runtimes

Executors host more than tools. They may also host full agent runtimes.

Examples:

- `native` runtime with Cognis tool execution
- `claude_code` runtime using native Claude Code on the executor
- future `opencode` runtime

The controller still owns workflow progression, task state, memory policy,
notifications, and guardrails orchestration. The executor hosts the runtime
process and local state.

Runtime protocol invariants:

- event `seq` is monotonic per `runtime_run_id`
- `runtime.start`, `runtime.resume`, `runtime.cancel`, and terminal event
  handling must be idempotent
- an executor must replay buffered events from `after_seq + 1`
- terminal events must be replayable until the controller ACKs them via stored
  `last_event_seq`

### Custom Executor Implementations

Anyone can build a custom executor that provides both tool execution and LLM
inference. Examples:

```python
class ClaudeCodeRuntimeHost:
    """Executor-side host for the claude_code agent runtime."""
    # Starts/resumes native Claude Code sessions
    # Uses user's Claude subscription auth on the executor
    # Streams runtime events back to the controller
    # Cognis controller still owns workflow/task orchestration
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

For external runtimes, the controller also expects a runtime RPC surface such
as `runtime.start`, `runtime.resume`, `runtime.respond`, and `runtime.cancel`.
Tool RPC remains separate from runtime RPC.

## Scalability Path

```
Phase 1 (MVP):
  Single controller, in-process executor
  + remote WebSocket executors, subprocess executors
  + executor-side LLM inference via LiteLLM proxy

Phase 2:
  Docker/K8s executors
  Multiple executor pools with label selectors
  Channel adapters may run on executors for user-local services

Phase 3:
  Multiple controller instances (sticky WebSocket sessions)
  Decompose: cognis-api, cognis-orchestrator, cognis-inference
  Each component scales independently
```

For channel adapters in multi-controller deployments, the intended model is:

- **Webhook channels** stay effectively stateless on the controller side.
- **Long-lived controller-hosted adapters** will require DB-based lease
  ownership so only one controller replica polls a given account.
- **Executor-hosted adapters** do not need controller-side leader election;
  the selected executor owns the live platform connection.
