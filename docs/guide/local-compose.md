# Local Compose Deployment

Local Compose is a supported single-instance deployment for local personal use,
demos, development, and implementation-test feedback loops. It runs the Cognis
controller/UI, Mnemory, Intaris, Qdrant, and a WebSocket executor on one Docker
Compose project.

This is not a production high-availability deployment. For shared or public
deployments, use the production guidance in [Deployment](deployment.md).

## What it starts

`compose.local.yml` starts:

- `qdrant` for local vector storage
- `mnemory` from the published `ghcr.io/fpytloun/mnemory:latest` image
- `intaris` from the published `ghcr.io/fpytloun/intaris:latest` image
- `cognis`, built from this repository by default
- `cognis-executor`, built from this repository by default

The dependency image tags can be overridden:

```bash
MNEMORY_IMAGE=ghcr.io/fpytloun/mnemory:<tag>
INTARIS_IMAGE=ghcr.io/fpytloun/intaris:<tag>
COGNIS_IMAGE=ghcr.io/fpytloun/cognis:<tag>
COGNIS_EXECUTOR_IMAGE=ghcr.io/fpytloun/cognis-executor:<tag>
```

Use `latest` or `general` for normal operation. The `minimal` tag omits
browser, document, Git, Node, and `uvx` system tooling. The `development` tag
adds preinstalled language servers.

The default compose path builds Cognis and the executor from the checkout. Use
prebuilt Cognis images only when you intentionally want to test a published
image instead of local source.

## Configure inference once in Cognis

Mnemory and Intaris use `http://cognis:8080/v1` with separate service JWTs.
Cognis provisions each token into a dedicated read-only companion volume.
The companions do not receive upstream API keys or Cognis's signing key.
Use companion images that support `COGNIS_INFERENCE_URL` and
`COGNIS_INFERENCE_TOKEN_FILE`; older images are not compatible with this managed
configuration. When testing unreleased changes, build both companion repositories
locally and set `MNEMORY_IMAGE` and `INTARIS_IMAGE` to those local image tags.

Start the stack, sign in as an administrator, add global providers in Settings,
then open **Routing → Companion services**. Select a **Lite** model for frequent
calls, a **Smart** model for analysis, and an **Embedding** model. Advanced
per-role overrides are optional. Lite and Smart can select the same model.
An embedding provider can differ from the chat provider.

Before configuration, the control plane remains available but companion
inference returns `503`. Tool safety remains fail-closed. Credentials are read
at request time, so starting companions before Cognis is supported.
Do not create reciprocal health-check dependencies.

See [Inference API](inference-api.md) for authentication, observability,
rotation, and embedding migration requirements.

### Optional developer seed provider

Local Compose does not resolve Cognis credential IDs. If another agent or
credential manager has access to credential ID `litellm`, it should inject these
environment variables before starting or seeding the stack:

```bash
export COGNIS_LOCAL_LLM_BASE_URL=https://api.groq.com/openai/v1
export COGNIS_LOCAL_LLM_API_KEY=...
```

Never put real API keys in committed files. For local use, copy the example file
and fill it outside version control:

```bash
cp .env.local.example .env.local
set -a
source .env.local
set +a
```

Recommended cheap defaults are based on the deployed Cognis/Mnemory/Intaris
manifests:

- Cognis chat/default routing: `openai/gpt-oss-120b`
- Mnemory: `openai/gpt-oss-120b`, `text-embedding-3-small`, dimensions `1536`
- Intaris: evaluator `typesafe/jev-1.13` or `openai/gpt-oss-20b`,
  analysis/judge `openai/gpt-oss-120b`,
  `text-embedding-3-small`, dimensions `1536`, sparse model `Qdrant/bm25`

These are recommendations only. Configure the actual companion models in
Settings; the seed environment configures the Cognis provider, not the
companions independently. A chat-only endpoint does not supply embeddings.
Jev is a typed-decision model for the classifier route or Intaris evaluation, not chat. Use a
current Intaris image supporting `EVALUATOR_BACKEND=auto` (the Compose default).
Select literal upstream IDs from discovery; OpenRouter embeddings use
`openai/text-embedding-3-small`.

## Start interactively (recommended)

Copy `.env.local.example` to `.env.local`. Leave both local admin credentials
blank to create your administrator using the one-time setup URL. Start only the
controller and companions; the guide will pair your executor later:

```bash
docker compose --env-file .env.local -f compose.local.yml up -d --build --wait cognis mnemory intaris qdrant
docker compose --env-file .env.local -f compose.local.yml logs cognis
```

Open the setup URL from the logs, create the administrator, and follow
[Getting Started](getting-started.md). This is the zero-agent, zero-provider
path. A single OpenRouter provider can cover companion chat, embeddings,
Jev evaluation, agent chat, and optional recommended avatar generation.

## Optional developer seed

The seed is an explicit demo shortcut, not required for onboarding. Populate
the provider API key in `.env.local`, and use a non-disposable admin password
if the instance is not throwaway. It creates its own development account
when the admin variables are blank:

```bash
docker compose --env-file .env.local -f compose.local.yml run --rm seed
```

The seed step is idempotent. It creates or updates:

- one local admin user
- one shared LiteLLM/OpenAI-compatible provider using env-based auth
- model routing for default, classifier, compaction, and evaluator routes
- one WebSocket executor record
- one local implementation/test agent assigned to that executor
- one sample conversation with seed messages

The seed writes executor connection files under
`.local/cognis-compose/executor-token/`. Those files contain an executor token;
do not commit or share them.

## Executor mode 1: Compose sidecar

The default local executor is a Docker Compose sidecar. Start it after seeding:

```bash
docker compose --env-file .env.local -f compose.local.yml up -d --build cognis-executor
```

The executor waits for `.local/cognis-compose/executor-token/executor.env`,
connects to `ws://cognis:8080/api/executor/ws`, and stores its persistent home,
workspace, browser profiles, and caches in the `cognis-executor-home` volume.
The seed file explicitly enables insecure `ws://` only for this trusted local
Compose network; remote executors should keep using `wss://`.

The Compose build selects the recommended `general` executor target. This
target preinstalls Patchright with Google Chrome. Configure the executor
browser runtime as `patchright` with channel `chrome`. Headed browser UX can
need additional host or display configuration.

## Executor mode 2: host executor

Use a host executor when tools need host filesystem access, host browser
profiles, local credentials, or easier debugging.

After running the seed service, run from this checkout:

```bash
set -a
source .local/cognis-compose/executor-token/host-executor.env
set +a
uv run cognis-executor
```

Or use the published CLI:

```bash
set -a
source .local/cognis-compose/executor-token/host-executor.env
set +a
uvx --from 'cognis-executor[full]' cognis-executor
```

The host executor connects to `ws://localhost:8080/api/executor/ws`. Plain
`ws://` is appropriate only for localhost/local development.

## Reset and cleanup

Stop without deleting data:

```bash
docker compose --env-file .env.local -f compose.local.yml down
```

Destroy all local state:

```bash
docker compose --env-file .env.local -f compose.local.yml down --volumes
```

This destructive command removes local users, stored secrets, conversations,
Mnemory data, Intaris data/events, Qdrant vectors, and executor browser profiles.
Generated connection files under `.local/cognis-compose/executor-token/` remain
on the host; they contain invalidated tokens after a reset. Do not reuse them.

## Manual verification

After the stack is running:

1. Open `http://localhost:8080`.
2. Create the first account from the setup URL, or log in with the explicit
   development account if you used the seed path.
3. Check Settings → Providers, Routing, and Executors.
4. Follow setup to create an XO, or open the seeded local agent and conversation.
5. Send a chat turn through the configured model.
6. Exercise browser tools through either the compose sidecar or host executor.

