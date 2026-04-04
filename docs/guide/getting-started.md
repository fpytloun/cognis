# Getting Started

This guide walks through the shortest path to a working Cognis system with the web UI, one LLM provider, one agent, and your first conversation.

## Prerequisites

- Python 3.12+
- One LLM option:
  - OpenAI API key
  - Anthropic API key
  - local Ollama instance

## 1. Start Cognis once to generate local state

```bash
OPENAI_API_KEY=sk-... uvx cognis
```

On first start, Cognis creates `~/.cognis/` with:

- JWT signing keys
- an encryption key for stored secrets
- a local SQLite database unless you configure another database

When bundled UI assets are available, Cognis serves the workspace on `http://localhost:8080` and prints a one-time setup URL if no users exist yet.

## 2. Start the companion services

```bash
uvx mnemory
uvx intaris
```

- **Mnemory** stores long-term memory and recall context.
- **Intaris** evaluates tool calls and stores session content.

Point both services at Cognis's public key for JWT validation:

```bash
MNEMORY_JWT_PUBLIC_KEY=~/.cognis/keys/public.pem uvx mnemory
INTARIS_JWT_PUBLIC_KEY=~/.cognis/keys/public.pem uvx intaris
```

If Mnemory or Intaris were already running before Cognis generated the key, restart them with the JWT public key configured.

## 3. Create the first admin user

Open the printed `/setup?token=...` URL and create the first admin account.

If the token has expired, either restart Cognis or create the first user locally:

```bash
cognis admin create-user admin@example.com --name "Admin"
```

## 4. Confirm readiness

After login, open the onboarding guide or the system tab and confirm:

- Mnemory is reachable
- Intaris is reachable
- an LLM provider is configured
- executor tools are configured
- at least one agent exists

The `Getting started` flow in the UI is the fastest way to see what is still missing.

## 5. Configure an LLM provider

Open `Settings` and add a provider using one of the guided presets:

- OpenAI
- OpenAI Compatible
- Anthropic
- Ollama
- LiteLLM Proxy
- Custom

Use the provider test action to verify credentials, model resolution, and basic connectivity before moving on.

## 6. Create your first agent

Open `Agents` and create an agent with:

- a name and description
- personality and system prompt guidance
- a provider or model override if needed
- an executor/tool configuration that matches what the agent should be allowed to do

For a first setup, keep the agent simple and enable only the tools you expect to use.

## 7. Start a conversation

Open `Chat`, create a new conversation, choose your agent, and send a first message.

The chat view will show streaming responses, tool activity, and any approval prompts that need human input.

## 8. Explore the rest of the workspace

Once basic chat is working, the next useful areas are:

- `Tasks` for background or multi-step work
- `Workflows` for reusable execution templates
- `Channels` for external messaging integrations
- `Docs` for embedded user guidance inside the app

## Common setup problems

- **Mnemory or Intaris unreachable**
  - Check the configured URLs in the system view.
  - Confirm both services are running and use Cognis's public key.
- **Provider test fails**
  - Verify the API key or base URL.
  - Confirm the model exists and is available to the selected provider.
- **No setup link is available**
  - A user may already exist, or the token may have expired.
  - Use the CLI admin command if you need local bootstrap access.
