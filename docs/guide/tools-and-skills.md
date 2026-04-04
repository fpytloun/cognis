# Tools and Skills

The `Tools` workspace helps you understand what Cognis can execute and how those capabilities are exposed to agents.

## Tool registry

The tool registry lists available tools from multiple sources, including:

- built-in executor tools
- MCP tools
- orchestration-related capabilities
- system utilities

Use the filters to narrow by category or source when you want to understand what is available before enabling it on an executor or agent.

## Why the registry matters

The registry is a reference view, not the final permission model.

Whether an agent can actually use a tool depends on:

- which executor it resolves to
- which tools and MCP servers that executor exposes
- which Intaris MCP servers are allowed directly on the agent
- which restrictions the agent applies on top
- whether Intaris allows, escalates, or denies the runtime call

## Skills

Skills are reusable instruction bundles that can be loaded by the agent runtime. In the UI you can:

- inspect built-in or file-sourced skills
- create database-backed custom skills
- edit descriptions and instructions
- organize skills with tags

Use skills when guidance should be reusable across tasks instead of copied into each agent definition.

## MCP servers versus tools

MCP servers are configured in settings and then attached to executors. Once attached, they contribute tools to the executor's effective tool set.

This means:

- tools are what the agent can call
- MCP servers are one way those tools become available
- executors are the boundary where MCP tools are actually exposed
- Intaris-managed remote MCP servers are a separate per-agent path in the agent editor

## Practical advice

- Start with the smallest useful executor tool set.
- Attach MCP servers only where they are needed.
- Use the tool registry to verify naming, categories, and source before troubleshooting agent permissions.
- Use skills for reusable operating instructions, not as a replacement for clear agent configuration.
