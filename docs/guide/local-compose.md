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
- `mnemory` from the published `genunix/mnemory:latest` image
- `intaris` from the published `genunix/intaris:latest` image
- `cognis`, built from this repository by default
- `cognis-executor`, built from this repository by default

The dependency image tags can be overridden:

```bash
MNEMORY_IMAGE=genunix/mnemory:<tag>
INTARIS_IMAGE=genunix/intaris:<tag>
COGNIS_IMAGE=ghcr.io/fpytloun/cognis:<tag>
COGNIS_EXECUTOR_IMAGE=ghcr.io/fpytloun/cognis-executor:<tag>
```

The default compose path builds Cognis and the executor from the checkout. Use
prebuilt Cognis images only when you intentionally want to test a published
image instead of local source.

## Configure the OpenAI-compatible endpoint

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

- Cognis chat/default routing: `gpt-oss-120b`
- Mnemory: `gpt-oss-120b`, `text-embedding-3-small`, dimensions `1536`
- Intaris: primary `gpt-oss-20b`, analysis/judge `gpt-oss-120b`,
  `text-embedding-3-small`, dimensions `1536`, sparse model `Qdrant/bm25`

These are recommendations only. Any OpenAI-compatible endpoint and any model IDs
supported by that endpoint can be used through the corresponding environment
variables in `.env.local.example`.

## Start and seed

Build local Cognis images:

```bash
make local-compose-build
```

Start the controller and companion services:

```bash
make local-compose-up
make local-compose-wait
```

Seed the single-user local instance:

```bash
make local-compose-seed
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
make local-compose-executor-up
```

The executor waits for `.local/cognis-compose/executor-token/executor.env`,
connects to `ws://cognis:8080/api/executor/ws`, and stores its persistent home,
workspace, browser profiles, and caches in the `cognis-executor-home` volume.
The seed file explicitly enables insecure `ws://` only for this trusted local
Compose network; remote executors should keep using `wss://`.

The first phase is optimized for practical local validation. Browser automation
uses the executor-native Playwright runtime in the container; headed browser UX
may need additional host/display tuning.

## Executor mode 2: host executor

Use a host executor when tools need host filesystem access, host browser
profiles, local credentials, or easier debugging.

After `make local-compose-seed`, run from this checkout:

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
uvx cognis-executor
```

The host executor connects to `ws://localhost:8080/api/executor/ws`. Plain
`ws://` is appropriate only for localhost/local development.

## Reset and cleanup

Stop without deleting data:

```bash
make local-compose-down
```

Destroy all local state:

```bash
make local-compose-reset
```

Reset removes local users, stored secrets, conversations, Mnemory data, Intaris
data/events, Qdrant vectors, executor browser profiles, and generated local
executor token files.

## Manual verification

After the stack is running:

1. Open `http://localhost:8080`.
2. Log in with `COGNIS_LOCAL_ADMIN_EMAIL` and `COGNIS_LOCAL_ADMIN_PASSWORD`.
3. Check Settings → Providers, Routing, and Executors.
4. Open the seeded local agent and sample conversation.
5. Send a chat turn through the configured model.
6. Exercise browser tools through either the compose sidecar or host executor.

