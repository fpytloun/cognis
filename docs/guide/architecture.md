# Architecture

Cognis is the controller and orchestration layer of the Openclaw ecosystem. It works with Mnemory for persistent memory, Intaris for guardrails and session content, and executors for tool execution.

![Cognis ecosystem overview](../assets/images/cognis-ecosystem-overview.svg)

## The ecosystem at a glance

- **Cognis** owns users, agents, settings, conversations, and orchestration.
- **Mnemory** owns long-term memory, recall sessions, and durable artifacts.
- **Intaris** owns guardrails decisions, session recording, and behavioral analysis.
- **Executors** run tools and can optionally proxy local model inference.

This split keeps Cognis focused on coordination instead of embedding memory, safety, and tool runtimes in one process.

## Controller versus executor

![Controller and executor split](../assets/images/cognis-controller-executor-split.svg)

The most important architecture rule in Cognis is simple:

**the controller decides, the executor does**

That means:

- the controller runs agent loops and workflow logic
- the controller assembles context and calls Mnemory and Intaris
- the executor performs actual tool execution
- the executor never owns memory, sessions, or guardrails policy

Even when Cognis uses an in-process executor for local development, tool calls still go through the executor boundary conceptually.

## What Cognis stores itself

Cognis stores:

- users and API keys
- agent definitions
- workflow templates
- task metadata
- secrets and provider settings
- conversation and session metadata

Cognis does **not** duplicate the durable content owned by Mnemory or Intaris.

## Why this design matters

This architecture makes Cognis easier to reason about and easier to deploy:

- you can move tool execution to a user-local or remote machine
- you can keep memory and guardrails as separate services
- you can scale the controller without turning it into a giant stateful monolith
- you can route inference through executors when model access is local

## Where to learn more next

- Use `/docs/executors` for executor placement and tool routing
- Use `/docs/channels` for controller-hosted versus executor-hosted channel adapters
- Use `/docs/workflows` for task and workflow execution behavior
