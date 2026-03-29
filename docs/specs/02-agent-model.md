# Cognis: Agent Model

## Overview

An agent in Cognis is an entity with identity, personality, skills, and
capabilities. Agents are stored in the database and managed via the UI or API.
They can be created through a wizard-like experience (character creator) or
programmatically. Export/import as YAML is supported for GitOps workflows.

For the MVP skill loader shipped in stage 15, `agent.skills` is stored inline
as JSON with `items[*].tool_names` references to existing builtin/static tool
names. MCP tool references are ignored in MVP and reserved for future phases.

## Agent Definition Schema

```python
class AgentDefinition(BaseModel):
    """Complete agent definition stored in the database."""

    # Identity
    agent_id: str                          # Unique identifier (slug)
    name: str                              # Display name
    description: str                       # Short description
    avatar_url: str | None = None

    # Ownership
    owner_email: str                       # User who owns this agent
    visibility: AgentVisibility = "private"  # private | shared | public

    # Personality (bootstrapped to Mnemory on creation; Mnemory owns runtime)
    personality: AgentPersonality

    # Capabilities
    skills: list[SkillReference] = []
    tools: AgentToolConfig
    permissions: AgentPermissions

    # LLM Configuration
    llm: AgentLLMConfig

    # Execution
    execution: AgentExecutionConfig

    # Metadata
    status: AgentStatus = "active"         # draft | active | suspended | archived
    tags: list[str] = []
    created_at: datetime
    updated_at: datetime


class AgentPersonality(BaseModel):
    system_prompt: str
    traits: list[str] = []                # ["analytical", "patient"]
    communication_style: str | None = None
    expertise_areas: list[str] = []
    behavioral_rules: list[str] = []
    # Bootstrapped to Mnemory as pinned assistant memories on agent creation.
    # After creation, Mnemory owns the runtime personality — it can evolve
    # through interactions. This Cognis definition serves as the initial
    # template and factory-reset reference. No ongoing sync from Cognis to
    # Mnemory. Future: optional reverse-sync Mnemory -> Cognis for display.


class AgentToolConfig(BaseModel):
    builtin_tools: list[str] = ["*"]      # Built-in + executor tool allowlist
    mcp_servers: list[MCPServerRef] = []  # Local MCP servers for executor
    intaris_mcp_servers: list[str] = []   # Intaris-managed remote MCP servers (auto-discovered)
    delegation_tools: bool = True
    custom_tools: list[CustomToolDef] = []
    # Note: executor-native tools (read, write, edit, bash, glob, grep, etc.)
    # are available to all agents by default. Use builtin_tools to restrict
    # or permissions.tool_permissions to deny specific tools.


class AgentPermissions(BaseModel):
    tool_permissions: dict[str, Permission] = {"*": "evaluate"}
    # "allow" = auto-approve (but non_bypassable tools still go through Intaris)
    # "evaluate" = send through GuardrailsProvider
    # "deny" = block

    can_delegate_to: list[str] = ["*"]
    can_be_delegated_to: bool = True
    max_delegation_depth: int = 3
    max_tokens_per_turn: int = 100000
    max_tool_calls_per_turn: int = 50
    max_concurrent_delegations: int = 3
    allowed_secrets: list[str] = []
    allowed_contexts: list[str] = ["*"]


class AgentLLMConfig(BaseModel):
    """Agent's LLM preferences. References configured providers."""

    provider_id: str | None = None    # Which LLM provider (None = system default)
    model: str | None = None          # Override model (None = provider default)

    fallback_provider_id: str | None = None
    fallback_model: str | None = None

    temperature: float = 0.7
    max_output_tokens: int = 4096
    reasoning_effort: str | None = None  # If model supports it

    # Model routing overrides (per agent)
    model_routing: ModelRoutingPolicy | None = None


class ModelRoutingPolicy(BaseModel):
    """Override system-wide model routing for this agent."""
    classifier: str | None = None     # Decision Engine model
    compaction: str | None = None     # Compaction model
    simple_inline: str | None = None  # Simple response model


class AgentExecutionConfig(BaseModel):
    """Execution environment preferences."""
    executor_type: str | None = None      # Preferred: in_process, docker, k8s
    node_selector: dict[str, str] = {}    # Label selector for executor pools
    timeout_seconds: int = 300
    resource_limits: ResourceLimits | None = None


class AgentWorkflowConfig(BaseModel):
    """Workflow execution preferences. See 14-workflow-engine.md."""
    available_workflow_ids: list[str] = []   # Which workflows this agent can use
    default_workflow_id: str = "direct"      # Default workflow for delegated tasks
    workflow_selection_mode: str = "automatic"  # "automatic" | "always_ask" | "use_default"
    step_agent_overrides: dict[str, dict[str, str]] = {}
    # Per-workflow step→agent overrides.
    # e.g. {"code-with-review": {"code_review": "reviewer-agent-id"}}
    # Steps without overrides use this agent.


class Permission(str, Enum):
    ALLOW = "allow"
    EVALUATE = "evaluate"
    DENY = "deny"


class AgentShare(BaseModel):
    """Future collaboration model (Phase 2+)."""
    shared_with_email: str
    permission: str              # "use" | "edit"
```

## Ownership and Sharing

An agent is always owned by a single user (`owner_email`). Ownership stays
simple even when the agent is shared with others.

Future sharing model:

- `use` — another user can chat with the agent, create tasks with it, and
  inspect task progress
- `edit` — another user can also modify the agent definition, workflow
  settings, and sharing

This is resource sharing, not user-memory sharing.

### Memory visibility model for shared agents

When an agent is shared:

- **Assistant memories** are shared with every user who has `use` access to
  the agent. These memories define the agent's identity, learned knowledge,
  and behavior.
- **User memories** remain private to the user who initiated the current
  conversation or task. They are never shared across collaborators just
  because the agent is shared.

This means a shared agent has a shared assistant brain, but each user keeps
their own private user context.

### External / world-facing agent mode

Agents with `visibility: public` are the beginning of agent-to-world
collaboration. In this mode, the agent may be offered for external tasks or
public channels.

For public/external interactions:

- only **assistant memories** are available
- **user memories are not injected at all**
- the agent's durable public identity comes entirely from its assistant
  memories and definition

## Agent Types

### Primary Agent

The main agent a user interacts with. Full personality, long-term memory,
rich tool set.

```yaml
agent_id: "aria"
name: "Aria"
description: "Full-stack development assistant"
personality:
  system_prompt: "You are Aria, a senior developer..."
  traits: ["analytical", "thorough", "patient"]
  expertise_areas: ["Python", "TypeScript", "Kubernetes"]
tools:
  builtin_tools: ["*"]             # All executor-native + builtin tools
  delegation_tools: true
  intaris_mcp_servers: ["github"]  # Auto-discovered from Intaris
permissions:
  tool_permissions:
    "*": "evaluate"
    "read": "allow"                # Executor-native read
    "glob": "allow"                # Executor-native glob
    "grep": "allow"                # Executor-native grep
llm:
  provider_id: "anthropic"
  model: "claude-sonnet-4-20250514"
```

### Specialist Agent

Focused expertise for delegation. Own identity and personality.

```yaml
agent_id: "reviewer"
name: "Code Reviewer"
personality:
  system_prompt: "You are a meticulous code reviewer..."
tools:
  builtin_tools: ["read", "grep", "glob"]
  delegation_tools: false
permissions:
  can_delegate_to: []
llm:
  provider_id: "anthropic"
  model: "claude-sonnet-4-20250514"
  temperature: 0.1
```

### Worker Template

Lightweight, no personality overhead. For focused tasks.

```yaml
agent_id: "_worker/research"
name: "Research Worker"
personality:
  system_prompt: "Find relevant information efficiently. Return concise results."
tools:
  builtin_tools: ["grep", "read", "web_fetch"]
  delegation_tools: false
llm:
  model: "gpt-4.1-mini"
  temperature: 0.1
```

Workers are prefixed with `_worker/`. They don't get Mnemory personality
memories and have minimal system prompts.

### Executor-Side LLM Agent

An agent that uses a provider running on an executor. This is a general-
purpose capability covering: local models (ollama, vllm, llama.cpp),
self-hosted LiteLLM proxy, custom executor implementations (Claude Code
with user's subscription), and air-gapped environments.

```yaml
agent_id: "local-coder"
name: "Local Coder"
llm:
  provider_id: "local-ollama"      # References an executor-side provider
  model: "llama3.3"
execution:
  node_selector: {"cognis.io/inference": "true"}
```

The provider `local-ollama` is configured in the `llm_providers` DB table
with `location: executor` and linked to executor groups via labels. When the
controller runs this agent's loop, the LLM Router detects the provider's
location and routes `llm.complete` to a matching executor via WebSocket
JSON-RPC.

### Custom Executor Agent

For custom executor implementations (e.g., Claude Code with a Claude
subscription):

```yaml
agent_id: "claude-coder"
name: "Claude Coder"
llm:
  provider_id: "claude-code"       # Custom executor handles LLM internally
execution:
  node_selector: {"type": "claude-code"}
```

The custom executor implements both `tool.execute` and `llm.complete` in its
own way. Cognis doesn't care how — it just uses the JSON-RPC protocol.

## Agent Lifecycle

```
        create ──► Draft ──activate──► Active ◄── resume
                                        │
                                    suspend
                                        │
                                    Suspended ── resume ──► Active
                                        │
                                    archive
                                        │
                                    Archived
```

| State | Receives Delegations | Active Sessions |
|-------|---------------------|-----------------|
| `draft` | No | No |
| `active` | Yes | Yes |
| `suspended` | No | Paused |
| `archived` | No | No |

### Creation Flow

1. UI wizard or API creates agent in `draft` state
2. Definition validated
3. Personality synced to Mnemory as pinned assistant memories
4. Agent activated → `active`
5. Agent Card generated (for A2A discovery)

### Memory Integration

Personality is stored in two places:
1. **Cognis DB** — authoritative definition (the "template")
2. **Mnemory** — runtime personality (pinned memories, `role=assistant`)

On creation/update, Cognis syncs personality to Mnemory. At runtime, the
controller loads personality via Mnemory recall — the agent's identity is part
of the memory flow. The agent can evolve through interactions (Mnemory). The
Cognis definition is the "reset point."

## Delegation Contract

### Agent Delegation (agent-to-agent)

Full agent with own identity. Gets:
- Own Intaris session (child of parent)
- Own Mnemory recall context (agent_id scoped)
- Own personality and system prompt
- Task description from parent

### Worker Delegation

Lightweight, no personality. Gets:
- Own Intaris session (child, task-focused intention)
- Parent's Mnemory context (`effective_agent_id` = parent's agent_id)
- Minimal system prompt from worker template
- Task description from parent

### Fork Delegation

Same agent in isolated context. Gets:
- Own Intaris session (child, inherited intention)
- Same Mnemory scope as parent
- Same personality and system prompt
- Context summary relevant to the task

### effective_agent_id

For Mnemory recall scoping in delegations:

```python
class DelegationContext(BaseModel):
    effective_agent_id: str  # Used for X-Agent-Id on Mnemory calls

# Agent delegation: effective_agent_id = delegate's own agent_id
# Worker delegation: effective_agent_id = parent's agent_id
# Fork delegation:   effective_agent_id = parent's agent_id (same agent)
```

This ensures workers see the parent's rich memory context, not an empty
worker-scoped memory.

## Agent Cards (A2A-Compatible)

Active agents with `visibility: public` generate Agent Cards:

```json
{
  "name": "Aria",
  "description": "Full-stack development assistant",
  "url": "https://cognis.example.com/a2a/agents/aria",
  "capabilities": {"streaming": true},
  "skills": [
    {"id": "code-review", "name": "Code Review", "tags": ["code"]}
  ],
  "authentication": {"schemes": ["bearer"]}
}
```

Served at:
- `GET /api/agents/{agent_id}/card`
- `GET /.well-known/agent.json` (default agent)

## Skill System

Skills are DB-managed instruction + tool bundles with import/export support:

```python
class SkillReference(BaseModel):
    skill_id: str
    name: str
    description: str
    source: str         # "db", "file"
    auto_load: bool = False
```

Skills provide instructions (injected into LLM context when activated), tool
definitions, and prompt templates. Loaded on demand to keep context lean.
Skills are managed via the API (`/api/v1/skills`), the Tools & Skills UI
page, and optionally by agents via the `skill_write` built-in tool.
See [06-tool-system.md](06-tool-system.md) for the full skill system design.

## Default Agents

| Agent | Role | Model |
|-------|------|-------|
| `default` | Primary | User's choice |
| `_worker/research` | Worker | gpt-4.1-mini |
| `_worker/summarize` | Worker | gpt-4.1-mini |
| `_worker/code` | Worker | claude-sonnet-4 |
| `_system/compaction` | System (internal) | gpt-4.1-mini |
| `_system/classifier` | System (internal) | gpt-4.1-mini |

System agents (`_system/`) are internal. Workers (`_worker/`) are visible but
cannot be primary agents.
