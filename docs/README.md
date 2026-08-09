# Documentation

Cognis ships these guides in the repository and embeds the same user-facing docs in the web app under `/docs`.

The public guides explain how to run and use Cognis. The [specifications](specs/README.md) are internal design references and implementation history.

## Start Here

| Document | Description |
|---|---|
| [Getting Started](guide/getting-started.md) | First-run setup for Mnemory, Intaris, Cognis, a provider, an executor, and your first agent |
| [Local Compose Deployment](guide/local-compose.md) | Single-instance local deployment with Cognis, Mnemory, Intaris, Qdrant, seeding, and executor options |
| [Architecture](guide/architecture.md) | Understand the cloud-native controller/executor split and companion services |
| [Configuring Providers](guide/configuring-providers.md) | Add LLM providers, test them, and set model routing |
| [Creating Agents](guide/creating-agents.md) | Define identity, tools, skills, executor placement, and workflow behavior |
| [Security and Privacy](guide/security-and-privacy.md) | Understand secrets, value refs, inference-provider privacy, guardrails, and executor boundaries |

## Workspace

| Document | Description |
|---|---|
| [Using Chat](guide/using-chat.md) | Streaming chat, tool calls, escalations, delegation, todos, and recovery |
| [Projects](guide/projects.md) | Project sources, workflow bindings, grants, project-aware tasks, and revision flows |
| [Managing Tasks](guide/managing-tasks.md) | Task board, workflow runs, approvals, deliverables, revisions, and results delivery |
| [Schedules](guide/schedules.md) | Recurring task creation and scheduled workflow execution |
| [Workflows](guide/workflows.md) | Build reusable workflows and understand step behavior |
| [Tools and Skills](guide/tools-and-skills.md) | Inspect tools, MCP-backed capabilities, reusable skills, and skill-loaded tools |
| [Content and Temporary Sharing](guide/content-and-sharing.md) | Artifacts, documents, temporary signed links, HTML viewing, and recovery |
| [Rich Deliverables](guide/rich-deliverables.md) | Structured durable outputs and their renderer targets |
| [Rich Deliverable Composition](guide/rich-deliverable-composition.md) | Compose blocks, assets, evidence, and accessible fallbacks |
| [Rich Deliverable Layout Blocks](guide/rich-deliverable-blocks-layout.md) | Layout, narrative, status, and action block reference |
| [Rich Deliverable Data Blocks](guide/rich-deliverable-blocks-data.md) | Data, evidence, media, and utility block reference |

## Operations

| Document | Description |
|---|---|
| [Settings](guide/settings.md) | Configure providers, routing, secrets, executors, diagnostics, and users |
| [Executors](guide/executors.md) | Choose where tools run, configure browser automation, and route tools safely |
| [Channels](guide/channels.md) | Connect external messaging platforms and understand pairing |
| [Deployment](guide/deployment.md) | Docker, systemd, remote executors, TLS, backups, and production hardening |
| [High Availability](guide/high-availability.md) | Kubernetes topology, migration choreography, failover, recovery, and operational limits |
| [HA E2E Compose](guide/ha-e2e.md) | Deterministic two-controller qualification overlay |
| [Troubleshooting](guide/troubleshooting.md) | Common setup, provider, executor, browser, channel, and UI problems |

## In-App Docs

When Cognis serves the bundled UI, these guides are also available directly inside the app:

- `/docs`
- `/docs/getting-started`
- `/docs/local-compose`
- `/docs/architecture`
- `/docs/configuring-providers`
- `/docs/creating-agents`
- `/docs/security-and-privacy`
- `/docs/using-chat`
- `/docs/projects`
- `/docs/managing-tasks`
- `/docs/schedules`
- `/docs/workflows`
- `/docs/tools-and-skills`
- `/docs/content-and-sharing`
- `/docs/rich-deliverables`
- `/docs/rich-deliverable-composition`
- `/docs/rich-deliverable-blocks-layout`
- `/docs/rich-deliverable-blocks-data`
- `/docs/settings`
- `/docs/executors`
- `/docs/channels`
- `/docs/deployment`
- `/docs/troubleshooting`
