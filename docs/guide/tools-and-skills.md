# Tools and Skills

The `Tools` workspace helps you understand what Cognis can execute and how those capabilities are exposed to agents.

## Tool registry

The tool registry lists available tools from multiple sources, including:

- built-in executor tools
- MCP tools
- skill-defined tools
- orchestration-related capabilities
- system utilities

Use the filters to narrow by category or source when you want to understand what is available before enabling it on an executor or agent.

Tool categories also drive workflow step tool profiles. A step profile can reduce which categories and capabilities are exposed by default for a specific step without changing the agent's underlying capability boundary.

## Why the registry matters

The registry is a reference view, not the final permission model.

Whether an agent can actually use a tool depends on:

- which executor it resolves to
- which tools and MCP servers that executor exposes
- which skills are active on the agent
- which Intaris MCP servers are allowed directly on the agent
- which restrictions the agent applies on top
- whether Intaris allows, escalates, or denies the runtime call

## Skills

Skills are reusable instruction bundles stored in the database and exposed through the `Tools` workspace and skill APIs.

Today, the most visible skill workflow is:

- create or edit database-backed skills
- import skills from supported external formats such as `SKILL.md`
- export skills for sharing or version control
- organize skills with tags and global attachment behavior
- attach skills to agents so their instructions are available at runtime

Depending on the imported format and runtime configuration, a skill can also carry richer metadata such as templates, assets, or tool-related information. This guide focuses on the shipped workflow you can use directly from the current app.

In practice, a skill can expose capability in three distinct ways:

- **Instructions** for reasoning and operating procedure
- **Linked runtime tools** that point at existing builtin or MCP tools already present in the registry
- **Bundled executable skill tools** that ship inside the skill version itself

Skills may also store a **saved workflow decomposition**. That decomposition is treated as a skill-owned template, not as a persistent workflow definition by itself.

### Managing skills

In the UI you can:

- create and edit database-backed custom skills
- import skills from a URL, including `SKILL.md`-style sources
- export skills for sharing and version control
- organize skills with tags
- attach a skill to all agents by default
- bind linked runtime tools that should become available when the skill is attached or explicitly loaded
- review or save workflow decomposition on the skill itself

### How skills work at runtime

Skills use a hybrid lazy-loading model for token efficiency:

1. **Compact metadata in the system prompt** -- visible skills are announced in the immutable prompt prefix with compact summaries. Attached skills are marked so the agent knows which ones are preferred defaults.
2. **On-demand loading via `skill_load`** -- the agent uses the `skill_load` tool to read full instructions when a skill is relevant to the current task. Instructions are loaded into the mutable context, not the cached prefix.
3. **Deferred tool exposure** -- linked runtime tools and bundled executable skill tools are discoverable but treated as deferred. Attached skills start available by default; other skills expose their tools after the agent loads the skill.

This means:
- adding or removing skills does not invalidate the entire prompt cache
- the agent only pays token costs for skills it actually uses
- skill edits are visible on the next turn without restarting the session
- after `skill_load`, the loaded skill's linked runtime tools and bundled executable tools become eligible for subsequent model calls in the turn
- if a skill carries saved decomposition, tasks and schedules that use that skill materialize from the latest saved skill version

### Agent skill selection

In the agent editor, you can select which skills to attach to an agent. Attached skills are highlighted in prompt metadata and their tools are available immediately through the deferred tool-loading path.

Attached-skill defaults now include linked runtime tools as well as bundled skill tools. This lets a skill bring an existing builtin or MCP tool surface into scope without duplicating that tool definition inside the skill.

Some shipped system agents also come with default attached skills. For example,
`system:implement` and `system:code-review` attach the built-in Cognis coding
skill by default. When a system agent allows overrides, its attached skill list
can be replaced per user through the normal agent editor.

All visible skills remain discoverable via prompt metadata and `skill_load`. Resolution order is deterministic: agent-attached skills first, then skills attached to all agents, then other discoverable skills.

### LLM skill management

Agents can manage skills through built-in tools:

- `skill_list` -- list available skills
- `skill_load` -- load a skill's full instructions and related metadata
- `skill_get` -- get skill details for inspection or debugging
- `skill_versions` -- inspect version history
- `skill_write` -- create or update a skill (creates a new version)
- `skill_asset_write` -- add or replace a text/script asset on a skill
- `skill_asset_delete` -- remove an asset from a skill
- `skill_delete` -- delete a skill
- `skill_import_url` -- import a skill from a URL
- `skill_restore_version` -- restore a previous immutable version
- `skill_export` -- export a skill as SKILL.md, YAML, or a full Cognis package

All mutation tools are non-bypassable and evaluated by Intaris guardrails. When an agent creates or imports a skill, Cognis automatically attaches it to that agent for future runs.

When a skill already has saved decomposition and its decomposition-driving inputs change, Cognis refreshes that decomposition before publishing the new current version. If the refresh fails, the update fails rather than leaving the latest saved version stale.

### Built-in Cognis management skills

Cognis also ships global management skills for Cognis-native operations such as task and workflow management. These skills are intended for the main chat agent and are discoverable through the normal skill tools (`skill_list`, `skill_load`). They guide the agent to inspect state first, use the correct management tools, and avoid mutating protected resources such as system workflows.

### Importing skills

Skills can be imported from:

- **Raw SKILL.md URLs** -- any HTTPS URL pointing to a SKILL.md file
- **GitHub blob URLs** -- automatically resolved to raw content
- **GitHub folder URLs** -- `SKILL.md` is inferred inside the folder
- **Cognis YAML** -- the native portable format
- **Cognis package** -- the full-fidelity portable format for skills with attached assets

Import security:

- HTTPS required for non-localhost URLs
- Private/reserved IP addresses are blocked (SSRF protection)
- Response size limits (10MB default)
- Redirect validation on every hop
- Provenance recorded (source URL, resolved URL, commit SHA, checksum)

### Executable skill tools

Skills can define tools with execution recipes. These bundled executable tools appear as first-class tools in the agent's effective tool set. When called, the executor stages required assets into a temporary workspace, executes the recipe, and cleans up afterward.

### Linked runtime tools

Skills can also point at existing runtime tools through `linked_tool_ids`. These are references to already-registered builtin or MCP tools. They are useful when a skill should expose a known tool surface without re-declaring a bundled executor-side tool recipe.

Use linked runtime tools when:

- the tool already exists in the registry
- the skill should surface that existing tool when attached or loaded
- you do not need to ship new executable code with the skill

Use bundled executable skill tools when:

- the skill needs to carry its own reusable command or script
- the behavior should travel with the skill version and asset package

Skills can also carry reusable workflow step material, including step tool-profile data. That means a skill can ship both runtime tool bindings and the recommended tool surface for the steps it contributes to composed workflows.

When Cognis decomposes a skill into workflow steps, it automatically tries to align step profiles and per-step tool overrides with the skill's linked and bundled tools. The saved decomposition is editable through the workflow editor UI, but saving in that mode writes back to the skill version rather than creating a standalone workflow row.

Skill assets are stored in Cognis itself (database metadata plus artifact storage) and are never managed as editable controller filesystem state. Executors only receive temporary staged copies for execution.

Supported recipe modes:

- `script` -- execute a staged asset script with arguments
- `command` -- run a declared command with templated arguments

The controller never executes skill recipes directly -- all execution goes through the executor.

## MCP servers versus tools

MCP servers are configured in settings and then attached to executors. Once attached, they contribute tools to the executor's effective tool set.

This means:

- tools are what the agent can call
- MCP servers are one way those tools become available
- skills are another way tools become available
- executors are the boundary where tools are actually exposed
- Intaris-managed remote MCP servers are a separate per-agent path in the agent editor

## Practical advice

- Start with the smallest useful executor tool set.
- Attach MCP servers only where they are needed.
- Use skills for reusable operating instructions and tool bundles instead of copying them into each agent definition.
- Import existing Claude Code skills from GitHub to bootstrap agent capabilities.
- Use the tool registry to verify naming, categories, and source before troubleshooting agent permissions.
