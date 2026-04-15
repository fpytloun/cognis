# Cognis: Agent Model

## Overview

An agent in Cognis is an entity with identity, capabilities, and a defined
role. Agents are stored in the database and managed via the UI or API. They
can be created through a wizard-like experience or programmatically.
Export/import as YAML is supported for GitOps workflows.

There are two agent types: **primary** agents (interactive, with personality
and memory) and **secondary** agents (lightweight task executors for focused
sub-tasks). Cognis ships pre-seeded system agents for common tasks like code
review, architecture review, and codebase exploration.

## Agent Types

### Primary Agents

Primary agents are the main assistants users interact with directly. They
have full personality, long-term memory via Mnemory, and can orchestrate
work by delegating to secondary agents or spawning tasks.

| Property | Value |
|---|---|
| Memory | Full Mnemory integration (auto recall + remember) |
| Personality | Core identity from structured fields, with Mnemory evolution layered on top |
| System prompt | Free text appended after the structured personality block |
| Can delegate | Yes (to other primary or bound secondary agents) |
| Can spawn tasks | Yes |
| Orchestration tools | Full (delegate, spawn_worker, tasks) |
| Mnemory bootstrap | Yes (structured personality fields synced as pinned memories; falls back to system prompt when personality is empty) |

### Secondary Agents

Secondary agents are lightweight task executors that primary agents invoke
for focused sub-tasks. They have no automatic memory machinery — just a
focused system prompt and a scoped tool set. Think of them as "hats" that
primary agents wear for specific work.

| Property | Value |
|---|---|
| Memory | No automatic recall/remember. Memory tools available only if explicitly bound. |
| Personality | None — task-focused system prompt only |
| System prompt | Focused task instructions |
| Can delegate | No |
| Can spawn tasks | No |
| Orchestration tools | None (`OrchestrationMode.NONE`) |
| Mnemory bootstrap | No |
| LLM config | Optional override; inherits caller's config at runtime |
| Runtime | Optional override; inherits caller's runtime at runtime |
| Executor | Optional override; inherits caller's executor at runtime |
| Hidden | Can be hidden from UI (invoked only by agents/workflows) |

Secondary agents are invoked in two ways:
1. **By primary agents** via the `delegate` tool during a conversation
2. **By the workflow engine** via `agent_override` on workflow steps

### Agent Type Comparison

| Capability | Primary | Secondary |
|---|---|---|
| Interactive chat with users | Yes | No (delegation/workflow only) |
| Mnemory auto recall per turn | Yes | No |
| Mnemory auto remember | Yes | No |
| Memory tools available | Yes | Only if explicitly in tool config |
| Memory instructions in context | Yes | No |
| Core memories in context | Yes | No |
| Orchestration tools | Yes | No |
| Can be delegated to | Yes (by other primary agents) | Yes (by bound primary agents) |
| Can delegate to others | Yes | No |
| Can spawn tasks | Yes | No |
| Compaction support | Yes (LLM-based) | Yes (same LLM-based compaction) |

## Agent Definition Schema

```python
class AgentDefinition(BaseModel):
    """Complete agent definition stored in the database."""

    # Identity
    agent_id: str                          # Unique identifier (slug)
    name: str                              # Display name
    description: str | None = None         # Short description
    avatar_url: str | None = None

    # Type
    agent_type: Literal["primary", "secondary"] = "primary"
    is_system: bool = False                # System agents are immutable
    hidden: bool = False                   # Hide from UI (secondary only)

    # Ownership
    owner_email: str                       # User who owns this agent
    visibility: str = "private"            # private | shared | public

    # System prompt (free text)
    system_prompt: str | None = None
    # For primary agents: bootstrapped to Mnemory as pinned assistant
    # memories on creation. After that, Mnemory owns runtime personality.
    # This definition is the "reset point."
    # For secondary agents: the only prompt. No Mnemory interaction.

    # Capabilities
    skills: dict[str, Any] | None = None   # Skill references (MVP: inline tool names)
    tools: dict[str, Any] | None = None    # Tool configuration
    permissions: AgentPermissions | None = None

    # LLM Configuration
    llm_config: AgentLLMConfig | None = None

    # Runtime + execution
    runtime: AgentRuntimeConfig | None = None # How the agent runs
    execution: dict[str, Any] | None = None   # Executor placement

    # Metadata
    status: str = "active"                 # draft | active | suspended | archived
    created_at: datetime | None = None
    updated_at: datetime | None = None
```

Note: Secondary agent bindings are stored in the `agent_secondary_bindings`
junction table, not on the agent definition itself. See the DB Schema
section below.

### Tool Configuration

```python
class AgentToolConfig(BaseModel):
    builtin_tools: list[str] | None = None  # Allowlist (None or ["*"] = all)
    mcp_servers: list[MCPServerConfig] | None = None
    intaris_mcp_servers: list[str] | None = None
    disabled_categories: list[str] | None = None
    disabled_tools: list[str] | None = None
    delegation_tools: bool = True           # Orchestration tools (primary only)
    memory_tools: bool = True               # Mnemory tools (primary: auto, secondary: explicit)
```

Notes:

- `mcp_servers` is **legacy inline configuration** kept for backward
  compatibility. New MCP servers are stored globally and assigned to executors.
- `runtime.type` controls how the agent executes (`native`, `claude_code`,
  future external runtimes).
- Agents inherit all tools exposed by their executor by default.
- Effective tool set = executor enabled tools/groups **minus** agent
  `disabled_categories` / `disabled_tools`, then `tool_permissions` controls
  allow/evaluate/deny behavior for guardrails.
- `execution.executor_id` binds an agent to a specific executor.
- `execution.executor_selector` binds an agent by label match (k8s-style).

### Permissions

```python
class AgentPermissions(BaseModel):
    tool_permissions: dict[str, Permission] | None = None
    # "allow" = auto-approve (non_bypassable tools still go through Intaris)
    # "evaluate" = send through GuardrailsProvider (default)
    # "deny" = block

    allowed_tools: list[str] | None = None   # Legacy allowlist
    denied_tools: list[str] | None = None    # Legacy denylist
    allowed_secrets: list[str] = []
    max_delegation_depth: int = 5
    can_delegate: bool = True
```

### LLM Configuration

```python
class AgentLLMConfig(BaseModel):
    provider_id: str | None = None
    model: str | None = None
    temperature: float | None = None
    max_tokens: int | None = None
    reasoning_effort: str | None = None
    model_routing: dict[str, str] | None = None
```

For secondary agents, LLM config is optional. At runtime, if not set,
the secondary agent inherits the calling primary agent's LLM config.
This allows cost optimization — a committer agent can use a cheap model
while a code review agent uses a capable one.

### Runtime Configuration

```python
class AgentRuntimeConfig(BaseModel):
    type: Literal["native", "claude_code", "opencode"] = "native"
    config: dict[str, Any] | None = None
```

Runtime and executor are separate:

- `runtime` selects how the agent executes a direct turn or workflow step
- `execution` selects where that runtime runs

For delegated secondary agents, runtime is inherited from the calling agent if
the secondary does not override it explicitly.

## System Agent Namespace

The `system:` prefix is reserved for system agents. Validation rules:

1. User-created agents **cannot** use the `system:` prefix
2. System agents have `is_system=True` and are immutable (403 on PUT/DELETE)
3. System agents are defined as Python constants, never stored in DB
4. They are merged with DB agents at query time (same pattern as system workflows)
5. System agents are always available — they cannot be deleted or disabled

```python
def validate_agent_id(agent_id: str, is_system: bool = False) -> None:
    if agent_id.startswith("system:") and not is_system:
        raise ValueError("The 'system:' prefix is reserved for system agents")
```

## Secondary Agent Binding

Primary agents declare which secondary agents they can use via the
`agent_secondary_bindings` junction table:

```sql
CREATE TABLE agent_secondary_bindings (
    primary_agent_id VARCHAR NOT NULL REFERENCES agents(agent_id) ON DELETE CASCADE,
    secondary_agent_id VARCHAR NOT NULL REFERENCES agents(agent_id) ON DELETE CASCADE,
    PRIMARY KEY (primary_agent_id, secondary_agent_id)
);
```

### Binding rules

1. When a primary agent delegates to a secondary agent, the secondary must
   be either:
   - A system secondary agent (`system:*`) — always allowed
   - Listed in the primary's bindings via the junction table
2. When a workflow step specifies `agent_override`, it is validated against
   the task's primary agent's bindings at task submission time
3. Secondary agents cannot delegate (enforced by `OrchestrationMode.NONE`)
4. Binding validation failure returns a clear error listing which agents
   are missing from the binding
5. When a secondary agent is deleted, FK cascade automatically removes it
   from all primary agents' bindings

### Runtime inheritance

When a primary agent delegates to a secondary agent:

- **LLM config**: secondary agent's own config if set, otherwise inherited
  from the calling primary agent
- **Runtime**: secondary agent's own runtime if set, otherwise inherited from
  the calling primary agent
- **Executor**: secondary agent's own executor config if set, otherwise
  inherited from the calling primary agent
- **Memory**: no automatic recall/remember. If the secondary agent has
  memory tools in its tool config, the LLM decides whether to use them.
  No memory system instructions are injected into context.

Runtime inheritance guardrails:

- system/control-plane agents such as compaction, classification, evaluation,
  and other Cognis-internal agents are `native`-only unless explicitly marked
  otherwise
- hidden secondary agents may inherit a caller's runtime only if that runtime
  is listed as compatible for the agent definition
- workflow-bound agents may further restrict runtime selection per step
- runtime-scoped auth and session state must always be keyed to the **acting
  user**, not only the agent owner, so shared/private agent future models do
  not leak runtime credentials across users

## Pre-seeded System Agents

Cognis ships with pre-seeded secondary agents for common tasks. These are
referenced by system workflows and available to all primary agents.

### Visible secondary agents

#### `system:explore`

Fast read-only agent for exploring codebases.

- **Tools**: read, grep, glob, list, bash (read-only shell)
- **LLM config**: inherits from caller (model routing: use `simple` task type for cost optimization)

```
You are a fast, read-only agent specialized for exploring codebases.

## Instructions

- You CANNOT modify files. You have read-only access.
- Find files by patterns, search code for keywords, and answer questions
  about the codebase structure and implementation.
- Be thorough in your exploration — check multiple locations, naming
  conventions, and related files.
- When asked to explore a topic, search broadly first (glob, grep) then
  read specific files for details.
- Report findings concisely with file paths and line numbers.
- If you find something unexpected or noteworthy, mention it.

## Output

Return a structured summary of your findings:
- What you found (with file:line references)
- Key patterns or conventions observed
- Anything notable or unexpected
```

#### `system:research`

Research agent for gathering information from web sources and local repo context.

- **Tools**: read, grep, glob, web_search, web_fetch, web_crawl, web_map, web_research
- **LLM config**: inherits from caller

```
You are a research agent for gathering and synthesizing information.

## Instructions

- Search the web for current information on the given topic.
- Read and analyze multiple sources to build a comprehensive picture.
- Cross-reference findings across sources for accuracy.
- When researching technical topics, prefer official documentation and
  authoritative sources.
- If the research involves code, also search the local codebase for
  relevant context.

## Output

Return structured research findings:
- Key facts and findings (with source URLs)
- Areas of consensus and disagreement across sources
- Recommendations or conclusions based on the evidence
- Gaps in available information
```

#### `system:code-review`

Findings-first code review agent for real defects and regressions.

- **Tools**: read, grep, glob, bash (read-only git commands only)
- **LLM config**: inherits from caller

```
You are an expert code reviewer. Your task is to review ONLY the changes
made. You are most likely being executed inside a git repository so you
can use git read-only commands to examine status and obtain diffs.

## Critical Instructions

- Review ONLY the modified code shown in the diff, NOT existing unchanged code
- Output ONLY the final review in the exact format specified below
- Do NOT write any files
- Do NOT execute any shell commands other than read-only, non-destructive
  git actions like git status, git diff, etc.
- Do NOT include thinking process, reasoning steps, or tool usage in output
- You can read files in the repository for further context (read-only)
- When referring to line numbers, provide actual file name and line number
  using full path from repository root (e.g., `src/myfile.py:123`)

## Review Requirements

Analyze the changes for:
- Code quality, readability, and maintainability
- Potential bugs, security issues, or performance problems
- Best practices and design patterns
- Test coverage and edge cases
- Documentation completeness

Adapt your review based on the project's nature and guidelines (AGENTS.md
or similar project conventions).

## Output Format (MUST follow exactly)

### Summary
[2-3 sentence overview of the changes and overall assessment]

---

### Strengths
- [Specific positive aspect with file/line reference if applicable]

---

### Issues Found

#### CRITICAL
- [Security vulnerabilities, data loss risks, breaking changes]

#### MAJOR
- [Bugs, logic errors, significant performance issues]

#### MINOR
- [Style issues, minor optimizations, suggestions]

---

### Recommendations
- [Actionable improvement with specific guidance]

---

## SCORE: [number]/100

## Scoring Guidelines

- 90-100: Excellent quality, minimal issues
- 80-89: Good quality, some minor improvements needed
- 70-79: Acceptable quality, several issues to address
- 60-69: Below standard, significant improvements required
- 0-59: Poor quality, major problems present

If no issues exist in a severity category, write "None identified".
Include file names and line numbers when referencing issues.
The SCORE line MUST always be present.
```

#### `system:architect`

Implementation plan reviewer focused on architecture and risk.

- **Tools**: read, grep, glob, bash (read-only git commands only)
- **LLM config**: inherits from caller (recommend capable model)

```
You are an expert Software Architect acting as an Architecture Review
Board (ARB) reviewer.

## Mission

1. Verify the architecture correlates with the stated intentions (goals,
   constraints, non-goals).
2. Identify weaknesses, missing requirements, risky assumptions, and
   likely failure modes.
3. Propose pragmatic improvements and alternatives with clear trade-offs.
4. Produce an actionable review that can be used to revise the plan.

## Critical Instructions

- Review the architectural plan provided (and only the referenced context).
- Do NOT invent requirements; if information is missing, explicitly call
  it out.
- Be constructive but rigorous. Do not rubber-stamp.
- Prefer specific, testable statements over vague advice.
- If a decision depends on unknowns: provide conditional guidance
  ("If X, do Y; otherwise do Z").
- If you propose patterns, justify them against the stated goals and list
  operational costs.

## Review Checklist (follow in order)

A) Extract Intent & Constraints
   - Goals, Non-Goals, Constraints, Assumptions
   - NFRs: availability/SLO, latency, throughput, consistency, RPO/RTO,
     compliance, data retention, privacy
   - If key items are missing, list "Blocking Questions" (max 5)

B) Architecture Summary (neutral)
   - Components & responsibilities, interfaces/APIs, data stores, key flows
   - Deployment/runtime topology, scaling model, integration points

C) Intent <-> Decision Traceability
   - Table: Intent/Constraint -> Decision -> Evidence -> Impact -> Gap/Risk

D) Quality Attributes Review
   - Reliability/Resilience, Scalability/Performance, Security/Privacy,
     Maintainability/Evolvability, Operability/Observability, Cost
     Efficiency, Data Integrity

E) Risk Register
   - Prefer >=8 risks when possible.
   - Each: Risk, Likelihood, Impact, Detection signal, Mitigation,
     Residual risk

F) Recommendations
   - (1) Minimal-change improvements
   - (2) Bolder alternative architecture
   - For each: benefit, downside, migration approach, what to measure

G) Verdict
   - APPROVE / APPROVE WITH CHANGES / REQUEST REWORK
   - Concrete next actions + acceptance criteria

## Output Format (MUST follow exactly)

### Summary
[2-4 sentence overview of the architecture and overall assessment,
explicitly stating whether it matches the intentions]

---

### Strengths
- [Specific strength tied to a goal/NFR]

---

### Issues Found

#### CRITICAL
- [High-impact misalignments, security/compliance gaps, data loss risks,
  unrecoverable failure modes]

#### MAJOR
- [Significant scalability/reliability/operability issues, unclear
  boundaries, weak data model, risky coupling]

#### MINOR
- [Clarity, documentation, naming, small optimizations, optional
  enhancements]

---

### Intent <-> Decision Traceability

| Intent / Constraint | Architectural Decision | Evidence in Plan | Impact | Gap / Risk |
|---|---|---|---|---|
| ... | ... | ... | ... | ... |

---

### Quality Attributes (1-5)
- Reliability/Resilience: [1-5] - [one-line justification]
- Scalability/Performance: [1-5] - [...]
- Security/Privacy: [1-5] - [...]
- Maintainability/Evolvability: [1-5] - [...]
- Operability/Observability: [1-5] - [...]
- Cost Efficiency: [1-5] - [...]
- Data Integrity: [1-5] - [...]

---

### Recommendations

#### Minimal-change
- [Actionable change + why + trade-off + measurement]

#### Bolder alternative
- [Alternative + when to choose it + migration notes]

---

### Verdict
**[APPROVE / APPROVE WITH CHANGES / REQUEST REWORK]**
- Next actions:
  - [ ]
- Acceptance criteria:
  - [ ]

---

## SCORE: [number]/100

## Scoring Rubric

Start from 100 and subtract penalties. Do not exceed 100.

1. Intent correlation (0-30)
   - Missing/unclear goals/non-goals/constraints: -5 to -15
   - Key decisions not traceable to intent: -5 to -20

2. Quality attributes & NFRs (0-30)
   - Missing explicit SLO/latency/throughput targets: -5 to -15
   - Missing RPO/RTO, backup/restore, DR: -5 to -15
   - Weak security/privacy posture: -5 to -20

3. Operability & delivery feasibility (0-20)
   - Missing observability: -5 to -15
   - Migration/rollout plan missing: -5 to -15
   - Unrealistic complexity: -5 to -15

4. Data & integration correctness (0-20)
   - Unclear data ownership, consistency, schema evolution: -5 to -15
   - Risky coupling, no failure handling: -5 to -15

Automatic floor rules:
- Any CRITICAL issues unmitigated: score <= 69
- Security/compliance critical gap: score <= 59
- Blocking Questions prevent validation: score <= 79
```

#### `system:committer`

Generates Git commit messages and creates commits following Conventional
Commits v1.0.0.

- **Tools**: read, bash
- **LLM config**: use model routing `simple` task type (cheap/fast model)

```
You are an expert in working with Git and creating meaningful Git commit
messages using Conventional Commits v1.0.0.

## Instructions

- Input is git diff of changes to be committed
- Understand the changes and output a commit message following conventions
- Stage all tracked changed files with git add -u
- Also explicitly git add any newly created files
- Create the commit with the generated message
- NEVER push. Do not run git push under any circumstances
- Use only plaintext and ASCII characters, no fancy visuals
- Use only dash or asterisk symbols for bullet points
- Avoid unnecessary empty lines between bullet points unless separating
  distinct lists
- Follow Git Conventional Commits v1.0.0 specification

## Commit message conventions

- Format: <type>[optional scope][!]: <short description>

  [optional body]

  [optional footer(s)]

- Types: fix (bug fix), feat (new feature), or accepted types (docs,
  chore, refactor, test, perf, ci, build, style, revert, etc.)
- Breaking changes: Indicate with ! after type/scope OR a
  BREAKING CHANGE: footer
- Scope (optional): Add context in parentheses (e.g., feat(parser): ...)
- Commit title must not be longer than 72 characters
- Description: Required, concise summary after colon
- Body (optional): Add details after a blank line. Use bulleted points
  for multiple changes
- Footer (optional): Add footers one blank line after body
- Only use allowed types
- Example messages:
  fix(auth): resolve login issue
  feat(ui)!: overhaul dashboard layout
  docs: clarify API documentation
  chore!: remove Node 6 support
```

### Hidden system agents (internal)

These agents are used internally by the controller for system-level LLM
calls. They are hidden from the UI agent list and API responses (unless
`?include_hidden=true` is passed for debugging).

#### `system:compaction`

Summarizes conversation history when context exceeds capacity. Used by
all agent types — both primary and secondary agents receive the same
LLM-based compaction quality.

- **Tools**: none
- **Hidden**: true

```
Summarize the older conversation history into a structured handoff
document for the same assistant to continue from. Use exactly these
sections (omit empty sections):

## Goal - What the user is trying to accomplish.
## Key Instructions - Constraints, preferences, and rules the user stated.
## Discoveries - Important findings, decisions, or conclusions reached.
## Relevant Files - Files read, created, or modified (with paths).
## Accomplished - Completed actions and their outcomes.
## Current Work - What was in progress when this history ended.

Be concise. Do not invent information not present in the history.
Prefer bullet points over prose.
```

#### `system:classifier`

Selects the best workflow for a background task.

- **Tools**: none
- **Hidden**: true

```
Select the best workflow for the given task. You MUST respond with a
single JSON object and nothing else. No markdown, no explanation, no
text before or after the JSON.

Example: {"workflow_id": "...", "confidence": 0.8, "reason": "..."}
```

#### `system:evaluator`

Evaluates whether a workflow step's output satisfies its definition of done.

- **Tools**: none
- **Hidden**: true

```
You are a workflow step evaluator. Assess whether the agent's work
satisfies the step's objective.

Be skeptical. Agents tend to declare victory prematurely. Verify the
agent's claims against the actual response content. If the step says
"implement with tests" and the response doesn't include tests, that
is a revise.

Respond with a single JSON object:
{
  "decision": "approved" | "revise" | "failed",
  "reasoning": "...",
  "feedback": "..." // actionable feedback for the agent if revise/failed
}
```

## Workflow Step Agent Override

Workflow steps can specify a secondary agent to handle that step instead
of the task's primary agent. This is defined on the `StepDefinition`:

```python
class StepDefinition(BaseModel):
    name: str
    type: str                              # "run" | "gate"
    agent_override: str | None = None      # Secondary agent ID for this step
    # ... existing fields ...
```

Steps without `agent_override` use the task's primary agent. This means
the primary agent handles steps that benefit from personality and memory
(planning, implementation, memory updates), while focused steps like
review and commit use lightweight secondary agents.

Within any step, the running agent can delegate to sub-sessions. For
example, the plan step runs as the primary agent, which can spawn
multiple `system:explore` sub-sessions in parallel to explore the
codebase before producing the plan.

### Validation at task submission

When a task is submitted with a workflow:

1. Resolve the task's primary agent
2. For each step with `agent_override`:
   a. Check that the override agent exists and is active
   b. Check that it is a secondary agent (`agent_type="secondary"`)
   c. If it is a `system:*` agent — always allowed
   d. If it is a user agent — must be in the primary agent's
      bindings via the `agent_secondary_bindings` table
3. Reject with clear error if any check fails:
   "Workflow 'system:software-development' step 'code_review' requires
   secondary agent 'my-reviewer' but it is not bound to primary agent
   'riker'. Add it to riker's secondary agent bindings."

### Runtime resolution in workflow engine

- Step has `agent_override` -> load that secondary agent's definition,
  use its prompt and tools
- Step has no `agent_override` -> use the task's primary agent
- LLM config: secondary agent's config if set, otherwise primary's config
- Executor: secondary agent's config if set, otherwise primary's executor
- The `_resolve_step_agent` method must check the `AgentRegistry` (not
  just DB) to resolve `system:*` agent references

## Updated System Workflows

System workflows use `agent_override` to route focused steps to
secondary agents. The primary agent handles steps that benefit from
personality and memory (planning, implementation, memory updates).

### `system:direct`

Single-step execution. No planning or evaluation. Unchanged.

### `system:software-development`

Full development pipeline with planning, architecture review, implementation,
documentation, code review, commit, and memory update. Replaces the former
`system:code-with-review`.

```python
Workflow(
    workflow_id="system:software-development",
    name="Software Development",
    description="Full development pipeline: plan, architect review, "
                "implement, docs, code review, commit, remember.",
    criteria="Implementation tasks, feature development, bug fixes "
             "requiring structured quality pipeline.",
    tags=["code", "development"],
    steps=[
        StepDefinition(
            name="plan",
            type="run",
            prompt=(
                "Explore the codebase to understand the relevant areas. "
                "Launch multiple explore sub-sessions in parallel to "
                "efficiently understand different aspects of the codebase. "
                "Then produce a detailed implementation plan covering: "
                "files to create/modify (with rationale), specific changes "
                "per file, edge cases and error handling, testing strategy, "
                "migration or compatibility concerns."
            ),
            input=StepInputConfig(type="null"),
            completion=CompletionConfig(evaluate=True, max_attempts=2),
            # Primary agent runs this — has memory, personality, project context
        ),
        StepDefinition(
            name="architect_review",
            type="run",
            agent_override="system:architect",
            prompt=(
                "Review this implementation plan as a proportional architecture "
                "and risk check. Catch important omissions and overengineering, "
                "but do not block on nitpicks."
            ),
            input=StepInputConfig(type="full", source="plan"),
            completion=CompletionConfig(evaluate=True, max_attempts=3),
            on_reject=OnRejectConfig(
                target="plan",
                max_loop_iterations=3,
                on_exhausted="gate",
            ),
        ),
        StepDefinition(
            name="implement",
            type="run",
            prompt=(
                "Implement the approved plan. Follow the plan step by step. "
                "After implementation, run relevant tests and linters."
            ),
            input=StepInputConfig(
                type="summary", source=["plan", "architect_review"]
            ),
            completion=CompletionConfig(evaluate=True, max_attempts=3),
            # Primary agent — needs memory and full tool access
        ),
        StepDefinition(
            name="update_docs",
            type="run",
            agent_override="system:implement",
            prompt=(
                "Review and update only the documentation directly affected by "
                "the changes: README, guides, specs, API docs, configuration "
                "examples, migration notes, or inline comments. If no "
                "documentation updates are needed, explicitly say so."
            ),
            input=StepInputConfig(type="summary", source="implement"),
            completion=CompletionConfig(evaluate=False),
            # Implementation specialist — closes docs if needed, or reports none needed
        ),
        StepDefinition(
            name="code_review",
            type="run",
            agent_override="system:code-review",
            prompt="Review all changes made during implementation for real defects, regressions, and meaningful gaps.",
            input=StepInputConfig(
                type="summary",
                source=["plan", "implement", "update_docs"],
            ),
            completion=CompletionConfig(evaluate=True, max_attempts=3),
            on_reject=OnRejectConfig(
                target="implement",
                max_loop_iterations=3,
                on_exhausted="gate",
            ),
        ),
        StepDefinition(
            name="commit",
            type="run",
            agent_override="system:committer",
            prompt="Create a conventional commit for all changes.",
            completion=CompletionConfig(evaluate=False),
        ),
        StepDefinition(
            name="remember",
            type="run",
            prompt=(
                "Store key findings, decisions, and implementation details "
                "as memories for future reference. Attach a detailed summary "
                "as an artifact."
            ),
            input=StepInputConfig(
                type="last",
                source=["plan", "implement", "code_review"],
            ),
            completion=CompletionConfig(evaluate=False),
            # Primary agent — has memory tools
        ),
    ],
    is_system=True,
)
```

### `system:research`

Plan, research, synthesize with evaluation.

```python
Workflow(
    workflow_id="system:research",
    name="Research",
    steps=[
        StepDefinition(
            name="plan",
            type="run",
            prompt="Create a research plan. Identify key questions, "
                   "sources, and methodology.",
            input=StepInputConfig(type="null"),
            completion=CompletionConfig(evaluate=True, max_attempts=2),
        ),
        StepDefinition(
            name="research",
            type="run",
            agent_override="system:research",
            prompt="Execute the research plan. Gather information from "
                   "available sources.",
            input=StepInputConfig(type="full", source="plan"),
            completion=CompletionConfig(evaluate=True, max_attempts=2),
        ),
        StepDefinition(
            name="synthesize",
            type="run",
            prompt="Synthesize findings into a coherent report with key "
                   "insights and recommendations.",
            input=StepInputConfig(
                type="summary", source=["plan", "research"]
            ),
            completion=CompletionConfig(evaluate=True),
        ),
    ],
    is_system=True,
)
```

### `system:creative`

Content generation with evaluation loop. Unchanged.

## Agent Registry

The `AgentRegistry` manages system and user agents, following the same
pattern as `WorkflowRegistry`:

```python
SYSTEM_AGENTS: dict[str, AgentDefinition] = {
    "system:explore": AgentDefinition(...),
    "system:research": AgentDefinition(...),
    "system:code-review": AgentDefinition(...),
    "system:architect": AgentDefinition(...),
    "system:committer": AgentDefinition(...),
    "system:compaction": AgentDefinition(..., hidden=True),
    "system:classifier": AgentDefinition(..., hidden=True),
    "system:evaluator": AgentDefinition(..., hidden=True),
}


class AgentRegistry:
    """Manages system and user agents."""

    async def get(self, agent_id: str) -> AgentDefinition | None:
        """Resolve agent — checks system agents first, then DB."""

    async def list_all(
        self,
        *,
        owner_email: str | None = None,
        agent_type: str | None = None,
        include_hidden: bool = False,
        include_system: bool = True,
    ) -> list[AgentDefinition]:
        """List all available agents (system + user)."""

    def get_system_agent(self, agent_id: str) -> AgentDefinition | None:
        """Get a system agent by ID (for internal use)."""
```

System agents are Python constants, never written to DB. The DB
`is_system` column exists as defense-in-depth (same as workflows).

### API protection

- `POST /api/v1/agents` — validates `agent_id` does not start with `system:`
- `PUT /api/v1/agents/{agent_id}` — returns 403 for system agents
- `DELETE /api/v1/agents/{agent_id}` — returns 403 for system agents
- `GET /api/v1/agents` — merges system + DB agents, respects `include_hidden`
  and `agent_type` filters

## Execution Policy for Secondary Agents

Secondary agents use a dedicated execution policy that skips memory
integration and orchestration tools while preserving full LLM-based
compaction:

```python
SECONDARY_POLICY = ExecutionPolicy(
    require_step_complete=True,
    step_complete_available=True,
    enable_auto_compaction=True,       # Same LLM compaction as primary
    event_flush_strategy="incremental",
    skip_memory=True,                  # No Mnemory recall/remember
    skip_orchestration=True,           # No delegate/spawn/fork tools
)
```

The `skip_memory` flag causes `ContextAssembler.assemble()` to skip
Mnemory recall, intention fetch, and memory instruction caching. The
`skip_orchestration` flag is enforced by `OrchestrationMode.NONE`.

All agents — primary and secondary — receive the same LLM-based
compaction quality via `system:compaction`. There is no mechanical
fallback.

## System Prompt Design

### Primary agents

Primary agents have a free-text `system_prompt` field. When creating a new
agent, the UI generates a structured default from the agent's metadata:

```
You are {name}{description_clause}.

## Capabilities
{generated from tools config}

## Delegation
{generated from secondary agent bindings}

## Rules
{generated from permissions}
```

This default is a UI convenience — users can completely rewrite it. The
`system_prompt` field is always free text, not a structured schema.

**Mnemory interaction**: The system prompt is bootstrapped to Mnemory as
pinned assistant memories on agent creation (existing behavior). After
that, Mnemory owns personality evolution. The system prompt in the DB is
the "reset point" for factory reset.

### Secondary agents

Secondary agents have a focused system prompt — just task instructions.
No personality, no delegation guidance. See the pre-seeded agent prompts
above for examples.

### Hidden system agents

Hidden system agents have minimal prompts optimized for their specific
internal function (compaction, classification, evaluation). These prompts
are maintained by the Cognis team and not user-editable.

## Context Assembly by Agent Type

The context assembler adjusts what it includes based on agent type:

| Context Block | Primary | Secondary |
|---|---|---|
| System prompt | Yes | Yes |
| Memory instructions from Mnemory | Yes | No |
| Core memories from Mnemory | Yes | No |
| Auto recall per turn | Yes | No |
| Compaction summary | Yes | Yes |
| History messages | Yes | Yes |
| Recalled memories | Yes | No |
| Delegation status | Yes | No |
| Orchestration tool schemas | Yes | No |
| Prior step context | Yes | Yes |

This is driven by the `ExecutionPolicy`. Secondary agents use
`SECONDARY_POLICY` with `skip_memory=True` and `skip_orchestration=True`.

## Delegation Contract

### Primary-to-Secondary Delegation

When a primary agent delegates to a secondary agent:

1. Controller creates a child Intaris session (linked to parent)
2. Secondary agent's definition drives the prompt and tools
3. No Mnemory auto-recall or auto-remember
4. LLM config inherited from primary unless secondary overrides it
5. Executor inherited from primary unless secondary overrides it
6. `OrchestrationMode.NONE` — secondary cannot delegate further
7. Same LLM-based compaction if context overflows

### Primary-to-Primary Delegation

When a primary agent delegates to another primary agent:

1. Controller creates a child Intaris session
2. Delegate agent uses its own identity, personality, and Mnemory scope
3. Full memory integration (own agent_id scope in Mnemory)
4. Own LLM config
5. Can delegate further (up to `max_delegation_depth`)

## DB Schema

```sql
CREATE TABLE agents (
    agent_id VARCHAR PRIMARY KEY,
    owner_email VARCHAR NOT NULL REFERENCES users(email),
    name VARCHAR NOT NULL,
    display_name VARCHAR,
    description TEXT,
    system_prompt TEXT,
    personality JSON,              -- Legacy/future structured personality
    skills JSON,
    tools JSON,
    permissions JSON,
    llm_config JSON,
    runtime JSON,
    execution JSON,
    sync_metadata JSON DEFAULT '{}',
    avatar_url VARCHAR,
    -- Type and system fields
    agent_type VARCHAR(20) NOT NULL DEFAULT 'primary'
        CHECK (agent_type IN ('primary', 'secondary')),
    is_system BOOLEAN NOT NULL DEFAULT FALSE,
    hidden BOOLEAN NOT NULL DEFAULT FALSE,
    --
    status VARCHAR NOT NULL DEFAULT 'active',
    created_at TIMESTAMP WITH TIME ZONE NOT NULL,
    updated_at TIMESTAMP WITH TIME ZONE NOT NULL
);

-- Junction table for secondary agent bindings
CREATE TABLE agent_secondary_bindings (
    primary_agent_id VARCHAR NOT NULL
        REFERENCES agents(agent_id) ON DELETE CASCADE,
    secondary_agent_id VARCHAR NOT NULL
        REFERENCES agents(agent_id) ON DELETE CASCADE,
    PRIMARY KEY (primary_agent_id, secondary_agent_id)
);
```

## Ownership and Sharing

An agent is always owned by a single user (`owner_email`). System agents
have a synthetic owner (the first admin user or a system email).

Future sharing model:

- `use` — another user can chat with the agent, create tasks with it
- `edit` — another user can also modify the agent definition

This is resource sharing, not user-memory sharing.

### Memory visibility model for shared agents

When an agent is shared:

- **Assistant memories** are shared with every user who has `use` access.
  These define the agent's identity, learned knowledge, and behavior.
- **User memories** remain private to the user who initiated the
  conversation or task.

A shared agent has a shared assistant brain, but each user keeps their
own private user context.

## Agent Lifecycle

```
        create --> Draft --activate--> Active <-- resume
                                        |
                                    suspend
                                        |
                                    Suspended -- resume --> Active
                                        |
                                    archive
                                        |
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
2. Definition validated (agent_id format, no `system:` prefix)
3. For primary agents: structured personality fields synced to Mnemory as pinned memories (fallback: system prompt only)
4. Agent activated -> `active`
5. For public agents: Agent Card generated (for A2A discovery)

### Memory Integration (Primary Agents Only)

Personality is stored in two places:
1. **Cognis DB** — authoritative core identity from structured personality fields plus system prompt
2. **Mnemory** — runtime personality evolution layer (pinned memories, `role=assistant`)

At runtime, Cognis always injects the structured personality block and system
prompt directly into the immutable system message. Cognis also bootstraps the
structured personality fields to Mnemory so recall can evolve and reinforce
that identity over time. When structured personality is empty, bootstrap falls
back to the raw system prompt so older agents still get a seed.
through interactions. The Cognis definition is the "reset point."

Secondary agents have no Mnemory integration. Their identity is entirely
defined by their system prompt.

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

Skills are DB-managed instruction + tool bundles with import/export support.
Skills provide instructions (injected into LLM context when activated), tool
definitions, and prompt templates. Loaded on demand to keep context lean.

For the MVP skill loader, `agent.skills` is stored inline as JSON with
`items[*].tool_names` references to existing builtin/static tool names.
MCP tool references are ignored in MVP and reserved for future phases.

See [06-tool-system.md](06-tool-system.md) for the full skill system design.
