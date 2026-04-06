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

Skills are versioned, reusable instruction and tool bundles stored in the database. Each skill can contain:

- **Instructions** -- markdown guidance injected into the agent's context when the skill is active
- **Tool definitions** -- first-class tools with execution recipes that appear in the agent's effective tool set
- **Prompt templates** -- reusable templates for common patterns
- **Assets** -- scripts, configuration files, and other resources stored in the Cognis artifact store
- **Secret placeholders** -- declared secrets the skill needs at runtime, bound separately by the agent owner

### Managing skills

In the UI you can:

- create database-backed custom skills with instructions, tools, and templates
- import skills from a URL (supports Claude Code / Agent Skills `SKILL.md` format, GitHub URLs, and Cognis YAML)
- export skills as `SKILL.md` or Cognis YAML for sharing and version control
- edit skill content (creates a new immutable version on each save)
- view version history, provenance, and asset manifests
- organize skills with tags
- set auto-load to make a skill active for all agents

### Skill versioning

Every content change creates a new immutable version with a content hash. The logical skill record points to the current published version. This means:

- runtime behavior is pinned to a specific version
- effective-tools preview and runtime use the same resolved version set
- version history is preserved for auditability
- imported skills record provenance (source URL, commit SHA, checksum)

### Agent skill selection

In the agent editor, you can select which skills an agent should use. Selected skills contribute their instructions to the agent's context and their tools to the agent's effective tool set.

Skills are resolved in order: agent-specified skills first, then auto-load skills alphabetically. This ordering is deterministic to preserve prompt caching stability.

### LLM skill management

Agents can manage skills through built-in tools:

- `skill_list` -- list available skills
- `skill_get` -- get skill details including version info
- `skill_write` -- create or update a skill (creates a new version)
- `skill_delete` -- delete a skill
- `skill_import_url` -- import a skill from a URL
- `skill_export` -- export a skill as SKILL.md or YAML

All mutation tools are non-bypassable and evaluated by Intaris guardrails.

### Importing skills

Skills can be imported from:

- **Raw SKILL.md URLs** -- any HTTPS URL pointing to a SKILL.md file
- **GitHub blob URLs** -- automatically resolved to raw content
- **GitHub folder URLs** -- `SKILL.md` is inferred inside the folder
- **Cognis YAML** -- the native portable format with full metadata

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
