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
- organize skills with tags and auto-load behavior
- attach skills to agents so their instructions are available at runtime

Depending on the imported format and runtime configuration, a skill can also carry richer metadata such as templates, assets, or tool-related information. This guide focuses on the shipped workflow you can use directly from the current app.

### Managing skills

In the UI you can:

- create and edit database-backed custom skills
- import skills from a URL, including `SKILL.md`-style sources
- export skills for sharing and version control
- organize skills with tags
- set auto-load to make a skill active for all agents

### How skills work at runtime

Skills use a hybrid lazy-loading model for token efficiency:

1. **Compact metadata in the system prompt** -- only skill names, descriptions, and tool summaries are included in the immutable prompt prefix. This keeps the cached prefix stable and small.
2. **On-demand loading via `skill_load`** -- the agent uses the `skill_load` tool to read full instructions when a skill is relevant to the current task. Instructions are loaded into the mutable context, not the cached prefix.
3. **Executable skill tools** -- when a skill includes supported tool definitions and the runtime exposes them, those tools can be called directly by the agent.

This means:
- adding or removing skills does not invalidate the entire prompt cache
- the agent only pays token costs for skills it actually uses
- skill edits are visible on the next turn without restarting the session
- after `skill_write` or `skill_import_url`, the updated skill is available in the same turn

### Agent skill selection

In the agent editor, you can select which skills an agent should use. Selected skills contribute their tools to the agent's effective tool set and their metadata to the prompt.

Skills are resolved in order: agent-specified skills first, then auto-load skills alphabetically. This ordering is deterministic to preserve prompt caching stability.

### LLM skill management

Agents can manage skills through built-in tools:

- `skill_list` -- list available skills
- `skill_load` -- load a skill's full instructions and related metadata
- `skill_get` -- get skill details for inspection or debugging
- `skill_write` -- create or update a skill (creates a new version)
- `skill_delete` -- delete a skill
- `skill_import_url` -- import a skill from a URL
- `skill_export` -- export a skill as SKILL.md or YAML

All mutation tools are non-bypassable and evaluated by Intaris guardrails. After a successful mutation, the updated skill is immediately available for the rest of the turn.

### Built-in Cognis management skills

Cognis also ships global management skills for Cognis-native operations such as task and workflow management. These skills are intended for the main chat agent and are discoverable through the normal skill tools (`skill_list`, `skill_load`). They guide the agent to inspect state first, use the correct management tools, and avoid mutating protected resources such as system workflows.

### Importing skills

Skills can be imported from:

- **Raw SKILL.md URLs** -- any HTTPS URL pointing to a SKILL.md file
- **GitHub blob URLs** -- automatically resolved to raw content
- **GitHub folder URLs** -- `SKILL.md` is inferred inside the folder
- **Cognis YAML** -- the native portable format

Import security:

- HTTPS required for non-localhost URLs
- Private/reserved IP addresses are blocked (SSRF protection)
- Response size limits (10MB default)
- Redirect validation on every hop
- Provenance recorded (source URL, resolved URL, commit SHA, checksum)

### Executable skill tools

Skills can define tools with execution recipes. These tools appear as first-class tools in the agent's effective tool set. When called, the executor stages required assets into a temporary workspace, executes the recipe, and cleans up afterward.

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
