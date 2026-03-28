# Cognis: Federation and Cross-Agent Communication

## Overview

Federation enables agents across Cognis instances or different platforms to
discover each other, establish trust, and delegate tasks. Phase 3 feature,
but architecture is designed for it from day 1.

Protocol stack: **MCP** (tools) + **A2A** (agents) + **DID/VC** (identity).

## Agent Cards (Phase 2+)

Public/shared agents generate A2A-compatible Agent Cards:

```json
{
  "name": "Aria",
  "description": "Full-stack development assistant",
  "url": "https://cognis.example.com/a2a/agents/aria",
  "provider": {"organization": "Example Corp"},
  "capabilities": {"streaming": true},
  "skills": [
    {"id": "code-review", "name": "Code Review", "tags": ["code"]}
  ],
  "authentication": {"schemes": ["bearer"]}
}
```

Served at `GET /.well-known/agent.json` and `GET /api/agents/{id}/card`.

## A2A Task Lifecycle (Phase 3)

Remote agents delegate via:

```
Remote Agent → POST /a2a/tasks → Cognis creates session → executor runs tools
Remote Agent → GET /a2a/tasks/{id}/stream → SSE progress
Remote Agent ← {status: "completed", artifacts: [...]}
```

Cognis agents can also delegate to remote A2A agents.

## Trust Evolution

### Phase 1: JWT (Internal)
Cognis issues JWTs, Mnemory/Intaris validate. Single trust root.

### Phase 2: Agent Cards
A2A Agent Cards for discovery. JWT + card attestation.

### Phase 3: DID/VC
```
did:web:cognis.example.com:agents:aria
```
Decentralized identity, verifiable credentials, signed messages.

## Collaboration Scopes (Future)

Federation and cross-user collaboration depend on keeping ownership,
assistant identity, and private user context separate.

### Agent ownership

An agent is always owned by a single user (`owner_email`), even when it is
shared or public.

Future sharing permissions:
- `use` — another user may use the agent and create tasks with it
- `edit` — another user may also modify the agent definition and settings

### Memory scopes

There are two durable memory categories:

- **Assistant memories** — define the agent's identity, learned knowledge,
  habits, and behavior. These are shared with every user who has access to
  the agent.
- **User memories** — private facts/preferences for a specific human user.
  These remain scoped to the conversation/task initiator only.

This means shared-agent collaboration works like this:
- shared assistant memory
- private initiator user memory

### External / world-facing tasks

Public agents are the beginning of agent-to-world collaboration.

For external tasks or public channels:
- assistant memories are available
- user memories are not injected
- the agent's public continuity comes entirely from its assistant memories

This prevents leaking private user context while still preserving the
agent's durable identity.

## Gateway Pattern (Phase 3)

For production federation:
```
Internal Agents → Gateway → Internet → Remote Gateway → Remote Agents
```

Gateway handles: rate limiting, audit, policy, credential translation,
content filtering.

## Architectural Preparation

The MVP architecture prepares for federation:
1. Agent Cards generated from agent definitions
2. Agent identity abstraction (string → DID later)
3. A2A-compatible delegation model
4. JWT auth extensible to federated validation
5. Session hierarchy supports cross-agent delegation
6. Provider pattern: federation as another provider
