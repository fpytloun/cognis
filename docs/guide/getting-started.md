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
uvx cognis-controller
```

On first start, Cognis creates `~/.cognis/` with:

- JWT signing keys
- an encryption key for stored secrets
- a local SQLite database unless you configure another database

When bundled UI assets are available, Cognis serves the workspace on `http://localhost:8080` and prints a one-time setup URL if no users exist yet.

If you already know which model backend you want to use, you can also start Cognis with that credential in place, but it is not required just to bootstrap the installation.

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
cognis-controller admin create-user admin@example.com --name "Admin"
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
The conversation appears immediately, but the active session is created lazily from that first user message.

The chat view will show streaming responses, tool activity, and any approval prompts that need human input.

## 8. Explore the rest of the workspace

Once basic chat is working, the next useful areas are:

- `Tasks` for background or multi-step work
- `Schedules` for recurring task creation
- `Workflows` for reusable execution templates
- `Channels` for external messaging integrations
- `Docs` for embedded user guidance inside the app

## Docker Quick Start

Cognis publishes two container images:

- `ghcr.io/fpytloun/cognis` for the controller and bundled web UI
- `ghcr.io/fpytloun/cognis-executor` for a remote WebSocket executor with browser, coding, shell, search, and LSP tooling

Run the controller with a persistent data volume:

```bash
docker run -d \
  --name cognis \
  --add-host=host.docker.internal:host-gateway \
  -p 8080:8080 \
  -v cognis-data:/data \
  -e COGNIS_DATA_DIR=/data \
  -e COGNIS_MNEMORY_URL=http://host.docker.internal:8050 \
  -e COGNIS_INTARIS_URL=http://host.docker.internal:8060 \
  -e OPENAI_API_KEY=sk-... \
  ghcr.io/fpytloun/cognis:latest
```

`host.docker.internal` works by default on Docker Desktop. On Linux, the `--add-host=host.docker.internal:host-gateway` flag maps it to the Docker host so a containerized controller can reach Mnemory and Intaris running on the host.

Open the printed setup URL from the controller logs:

```bash
docker logs cognis
```

Create a WebSocket executor in `Settings -> Executors`, generate a token, then run the executor image. For a local non-TLS controller, use host networking so the executor URL is `ws://localhost:8080/...`, which the executor accepts as a local-only insecure connection:

```bash
docker run -d \
  --name cognis-executor \
  --network host \
  -v cognis-executor-home:/home/cognis \
  -e COGNIS_CONTROLLER_URL=ws://localhost:8080/api/executor/ws \
  -e COGNIS_EXECUTOR_TOKEN=eyJ... \
  ghcr.io/fpytloun/cognis-executor:latest
```

For remote controllers, always use TLS:

```bash
docker run -d \
  --name cognis-executor \
  -v cognis-executor-home:/home/cognis \
  -e COGNIS_CONTROLLER_URL=wss://cognis.example.com/api/executor/ws \
  -e COGNIS_EXECUTOR_TOKEN=eyJ... \
  ghcr.io/fpytloun/cognis-executor:latest
```

The executor image runs as the non-root `cognis` user by default. Its home directory is designed to be mounted as a persistent volume so browser profiles, LSP caches, shell history, and workspace files survive restarts.

## Local Compose deployment

For a complete single-instance local deployment, use
[`compose.local.yml`](../../compose.local.yml) instead of starting each service
manually. It runs Cognis, Mnemory, Intaris, Qdrant, and a WebSocket executor,
then seeds a local admin user, provider, model routing, executor record, agent,
and sample conversation.

```bash
cp .env.local.example .env.local
# Fill COGNIS_LOCAL_LLM_BASE_URL and COGNIS_LOCAL_LLM_API_KEY.
set -a
source .env.local
set +a
make local-compose-build
make local-compose-up
make local-compose-wait
make local-compose-seed
make local-compose-executor-up
```

Open `http://localhost:8080` and log in with the configured local admin
credentials. See [Local Compose Deployment](local-compose.md) for the full env
contract, reset workflow, and the host-executor alternative.

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
