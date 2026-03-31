# Getting Started

## Prerequisites

- Python 3.12+
- One LLM option:
  - OpenAI API key
  - Anthropic API key
  - Local Ollama instance

## 1. Start companion services

```bash
uvx mnemory
uvx intaris
```

- **Mnemory** stores long-term memory.
- **Intaris** provides guardrails and event recording.

Point both services at Cognis's public key:

```bash
MNEMORY_JWT_PUBLIC_KEY=~/.cognis/keys/public.pem uvx mnemory
INTARIS_JWT_PUBLIC_KEY=~/.cognis/keys/public.pem uvx intaris
```

## 2. Start Cognis

```bash
OPENAI_API_KEY=sk-... uvx cognis
```

Cognis creates `~/.cognis/` on first start with:

- JWT signing keys
- Secrets encryption key
- SQLite database

When the bundled UI is available, Cognis prints a setup URL on `:8080`.

## 3. Complete setup

Open the printed `/setup?token=...` URL and create the first admin account.

If the token expires:

- restart Cognis to get a new token, or
- create the first user locally with the CLI:

```bash
cognis admin create-user admin@example.com --name "Admin"
```

## 4. Configure an LLM provider

Open **Settings → Providers** and choose a preset:

- OpenAI
- OpenAI Compatible
- Anthropic
- Ollama
- LiteLLM Proxy
- Custom

Use **Test provider** to verify model resolution and connectivity.

## 5. Create an agent

Open **Agents → New** and define:

- identity and description
- personality/system prompt
- tools and permissions
- model settings
- optional MCP servers

## 6. Start chatting

Open **Chat → New**, choose an agent, and begin the first conversation.

## Troubleshooting

- **Mnemory or Intaris unreachable**
  - Check the URLs shown in **Settings → System**.
  - Confirm both services are running and trust Cognis's JWT public key.
- **Provider test fails**
  - Verify the API key or Ollama base URL.
  - Confirm the selected default model exists.
- **Setup token expired**
  - Restart Cognis or use the CLI bootstrap command.
