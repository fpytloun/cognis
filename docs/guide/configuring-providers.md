# Configuring Providers

Providers tell Cognis which model backends are available for chat, routing, classification, compaction, and workflow execution.

## Where provider settings live

Open `Settings` and use the providers and routing sections.

Provider configuration is stored in the Cognis database. All provider settings are managed through structured UI forms.

## OpenAI-compatible base URLs and model names

OpenAI-compatible, OpenAI API and LiteLLM-proxy providers accept either a service
root or an API base ending in `/v1`, with optional trailing slash. For example,
`https://api.groq.com/openai` and `https://api.groq.com/openai/v1` resolve to the
same API base. Discovery, provider checks, chat, embeddings and audio use that
same normalization; `/v1` is never appended twice. Proxy management metadata
(`/model/info`) stays at the service root. Native Anthropic, Ollama and
subscription-provider URL contracts are unchanged.

For compatible/proxy providers, model IDs are the literal names advertised by
the upstream service. Keep vendor namespaces such as `openai/gpt-oss-20b`.
Do not add a LiteLLM transport prefix yourself: Cognis adds it separately.

## Supported presets

To check publicly documented Claude and GPT model changes from a Cognis
checkout without provider credentials, see the
[model-support preflight](model-support-preflight.md). This is a repository
maintenance check, not a test of account-specific model access.

The UI includes guided forms for:

- **OpenAI** for direct OpenAI API access
- **OpenAI Compatible** for APIs that follow the OpenAI chat format
- **Anthropic** for direct Anthropic API access
- **Ollama** for local Ollama deployments
- **LiteLLM Proxy** for a LiteLLM proxy that performs routing upstream
- **Groq**, **OpenRouter**, and **Google Gemini** for hosted inference
- **System One (TypeSafe Jev)** for typed decisions, not chat
- **AWS Bedrock (IAM)** for AWS-managed inference with the SDK credential chain
- **ChatGPT Subscription (Codex)** for ChatGPT Pro/Max subscription access through LiteLLM's `chatgpt/` provider route

For provider-specific settings not covered by the structured fields (e.g., Azure `api_version`), use the collapsible **Advanced settings** section to add key-value pairs.

## Common provider choices

### Explicit untiered aggregate-token estimates

An omitted service tier is normally unknown, not an untiered tariff. Two audited
OpenAI SDK resources can additionally carry operation-scoped evidence that the
service-tier parameter is not applicable: `embeddings.create` and
`images.generate`. This does not qualify image edits, other embedding transports,
audio, video, or arbitrary compatible API extensions.

The initial configuration surface is the owner-authorized provider API:
`POST /api/v1/llm-providers` or `PUT /api/v1/llm-providers/{provider_id}`.
Set the declaration and explicit scalar rates together on the corresponding
`config.models[]` entry. Preserve the provider's other intended configuration
when updating it. There is no new tariff editor or automatic catalog conversion.

For example, this model entry declares an **illustrative configured estimate**,
not the model's published provider price:

```json
{
  "model_id": "text-embedding-3-small",
  "model_mode": "embedding",
  "input_cost_per_mtok": 2.0,
  "untiered_tariff": {
    "version": 1,
    "scope": {
      "version": 1,
      "resource": "openai.embeddings.create",
      "abi_revision": 1
    },
    "billing_basis": "aggregate_input_tokens"
  }
}
```

For `openai.images.generate`, use `aggregate_input_output_tokens` and explicitly
configure both `input_cost_per_mtok` and `output_cost_per_mtok`. This declaration
means the scalar rates cover the reported aggregate token totals. It does **not**
model heterogeneous text/image rates, per-image/size/quality charges, audio
seconds, or video billing. Do not declare an aggregate tariff unless that
configured estimate is the intended cost basis.

The runtime independently requires the exact audited resource/ABI and absence of
unproven construction overrides. User kwargs, `extra_body`, arbitrary extensions,
provider defaults, a compatible hostname, and `tier: "standard"` cannot supply
this evidence. Missing required native usage still prevents a complete estimate.
Old null tariffs and historical unknown tiers are not converted or backfilled
into this contract. A cold price cache remains unpriced without inference-time
SQL or provider calls.
An observed image count or other media unit with no applicable rate keeps the
result partial, even when its aggregate-token subtotal is known.

The new optional fields extend strict persisted and executor payloads. Upgrade
controller, common and executor packages together before capturing this evidence;
older binaries are not supported readers of newly extended payloads. Existing
payloads without the fields retain their unknown semantics and booked costs.

### Hosted providers and System One

Select Groq, OpenRouter, Gemini, or System One in the provider form. The form
fills the endpoint and environment credential name (`GROQ_API_KEY`,
`OPENROUTER_API_KEY`, `GEMINI_API_KEY`, or `TYPESAFE_API_KEY`). Saved encrypted
secrets are also supported. Discover models before choosing defaults, and keep
provider model IDs intact: for example `openai/gpt-oss-20b` on Groq and
`typesafe/jev-1.13` on OpenRouter.

Gemini discovery uses header authentication and follows catalog pagination.
Gemini 2.5 uses thinking budgets; Gemini 3 uses thinking levels and cannot
disable thinking. OpenRouter uses its own reasoning controls, not the
publisher's native request fields.

Discovery classifies models by their advertised output modalities or protocol
metadata, not by a product-name prefix. OpenRouter discovery requests the full
catalog (`output_modalities=all`), including decision and embedding models;
this also applies to compatible providers using the OpenRouter endpoint.
These compatible providers can also execute typed evaluations through that
endpoint, on either the controller or an executor.

System One models return typed Choice, Score, and Noul results, not generated
text. Cognis excludes decision models from agent/chat model options. Select
`typesafe/jev-1.13` (pinned version) or `~typesafe/jev-latest` (latest alias)
for the `classifier` route or `intaris_evaluation` companion override, or access it explicitly through
the [inference API](inference-api.md). OpenRouter and direct TypeSafe providers
use the same native contract. Both controller and executor locations work;
credentials are resolved by the controller before executor dispatch.

The `classifier` route answers typed questions for tool metadata and skill-tool
selection. It supports both native decision models and chat models, including
mixed native/chat fallback targets in a named inference route. Tool permissions
and candidate validation still apply; a classification never grants access.

The separate `auxiliary_generation` route generates conversation titles and recaps,
composes workflows, and decomposes skills into workflow steps. It requires a chat model
and inherits the default chat route when unset. If these jobs previously used
your classifier's chat model, assign that model to `auxiliary_generation`
before changing `classifier` to Jev.

`typesafe/jev-router` is a **chat router**, not a System One evaluator. It
remains available for chat. Rediscover models after upgrading to refresh the
catalog. Existing manually configured native Jev versions and aliases without
metadata remain supported; fresh protocol metadata takes precedence over this
narrow compatibility fallback. Explicit user capability restrictions remain
unchanged when imported provider metadata is refreshed.
For older imports, capability restrictions are preserved conservatively even
when the editor did not record their origin. Use the model editor's registry
reset to opt into corrected capability values.

### AWS Bedrock

Bedrock uses IAM via the AWS SDK credential chain on the selected runtime.
Set the region in the provider form, or supply the runtime's AWS region.
Optionally select a shared AWS profile. Environment credentials, shared
profiles (including previously authenticated SSO), and workload roles are
supported. Cognis does not provision IAM roles or perform SSO login.

This is the same credential mechanism as S3 object storage, but S3 permissions
do **not** authorize Bedrock. Grant:

- `bedrock:ListFoundationModels` and `bedrock:ListInferenceProfiles` for discovery;
- `bedrock:InvokeModel` and `bedrock:InvokeModelWithResponseStream` on the
  permitted model/profile ARNs for generation and streaming.

Cross-region inference profiles also need permission to invoke their underlying
foundation models in the destination regions. Scope invocation policies to the
models you actually permit. Discovery lists on-demand foundation models and
active inference profiles; provisioned-throughput ARNs can be added manually.
Catalog connectivity is read-only; the provider's generation test invokes the
configured model and may incur cost.

### OpenAI

Use this when Cognis should call OpenAI directly. Provide the API key and a default model.

### OpenAI Compatible

Use this for vLLM, TGI, local gateways, or other services that expose an OpenAI-style API.

### Anthropic

Use this when Cognis should call Anthropic directly for chat or routing tasks.

The `protocol` setting controls transport selection:

- `auto` uses Cognis' native Anthropic Messages transport for the official
  `api.anthropic.com` endpoint and LiteLLM for custom endpoints.
- `anthropic_messages` explicitly selects the native Messages transport,
  including for a compatible custom endpoint.
- `litellm` explicitly retains the LiteLLM transport.

Claude subscription OAuth always uses the native Messages transport on the
controller; OAuth credentials are never sent to executors. API-key providers
can use the native transport on either the controller or an executor. Custom
endpoints default to LiteLLM unless native Messages compatibility is explicitly
selected.

Native strict tool schemas are emitted only for models with the
`supports_strict_tools` capability. Native tool search and `defer_loading` are
enabled only when the model advertises tool search, native search,
`defer_loading`, and `pause_turn`; otherwise Cognis falls back to controller
tool discovery. Prompt-cache controls remain capability-gated.

#### Claude Sonnet 5.5

`claude-sonnet-5-5` is available in the bundled catalog alongside Sonnet 5.
It uses a 1M-token context window, a 128K-token output limit, and $2/$10 per
million input/output tokens. Cognis supports it through the native Messages
transport (API key on controller or executor, or subscription OAuth). The
LiteLLM compatibility transport is rejected for this model because it cannot
provide the native signed-history validation described below.

- **Thinking:** adaptive by default, with `low`, `medium`, `high`, `xhigh`,
  and `max` effort. `none`, native `disabled`, and manual budgets fail before
  sending. They are never mapped to `between_tools`. An explicitly supplied
  native `thinking: {"type": "between_tools"}` is accepted at `low`, `medium`,
  or `high` effort; it still allows thinking between tool calls.
- **Tools:** automatic or disabled tool selection only. Required or named
  tool choice fails before sending. Keep workflows that require a tool call
  on a compatible model; automatic selection is not an equivalent guarantee.
- **Continuation:** empty signed thinking and redacted blocks are persisted
  unchanged. Cognis stores local prefix fingerprints with native envelopes
  and verifies the provider, system prompt, tools, and preceding messages
  before replay. Cache annotations can change. `between_tools` also binds the
  effort level.
- **Model switching:** switching between Opus, Sonnet 5, and Sonnet 5.5
  preserves visible messages, tool calls, and tool results. Only incompatible
  private reasoning is removed from the outgoing request. Cognis starts fresh
  reasoning without failing the turn or resetting the visible conversation.
  Returning to a previous model does not reintroduce pre-switch reasoning.
- **Edited history:** after a prefix edit, compaction, or a change of provider
  or tools, unproven thinking is removed from the outgoing request. Valid
  reasoning generated against the new prefix remains reusable on subsequent
  turns. Legacy thinking without provenance is handled the same way.
  These request projections never rewrite durable history; reasoning resets
  are logged with model and block counts, not content. Anthropic still validates
  signatures and account ownership. Native-envelope corruption and incomplete
  tool-result groups remain errors, rather than being repaired by discarding
  tool history.

See Anthropic's [migration guide](https://platform.claude.com/docs/en/models/sonnet-5-5/migration-guide).

For a local Meridian endpoint, run Meridian on or near the executor, then create
an **Anthropic** provider with:

- **Location**: `Executor`
- **Backend**: `litellm` (the default)
- **Base URL**: the executor-reachable Meridian Anthropic-compatible URL
- **Model IDs**: add the Meridian-exposed Claude model IDs manually

Cognis still owns the agent loop, memory, Intaris guardrails, tool approval,
workflow/session state, and audit records. The executor only provides the
network-local LiteLLM inference call and tool execution boundary.

### Ollama

Use this for local models exposed through Ollama. Make sure the selected model is already installed and reachable from the Cognis host or executor.

For a WebSocket executor, configure Ollama in **Settings → Executors → Local
inference**. Cognis accepts only an integer port from 1 through 65535 and
derives `http://127.0.0.1:<port>` internally. Hostnames, arbitrary URLs,
credentials, paths, queries, and redirects are not supported. Start Ollama on
the executor with the matching loopback listener, for example:

```bash
OLLAMA_HOST=127.0.0.1:22434 ollama serve
```

After saving, the executor is unavailable for local routing and model
management until the requested generation is applied and the executor
advertises the same flags and effective endpoint. The executor card reports
**Applying**, **Executor confirmed**, detected reachability, and the Ollama
version.

After an Ollama generation, Conversation Info and `/info` show the latest
provider-reported prompt/generation rates and durations plus client-measured
streaming time to first token. Cognis keeps only the latest observation for the
active session. `/benchmark quick` and `/benchmark full` are reserved explicit
commands; they currently return a clear unavailable result rather than calling
the provider outside normal accounting. Enabling them requires a bounded local
runtime benchmark operation that reports usage through the standard generation
path.

### LiteLLM Proxy

Use this when a LiteLLM proxy is already aggregating your models. Cognis treats the proxy as the provider and lets the proxy perform the final upstream routing.

The Local Compose deployment seeds a shared LiteLLM/OpenAI-compatible provider
using environment-based auth:

- `COGNIS_LOCAL_LLM_BASE_URL`
- `COGNIS_LOCAL_LLM_API_KEY`
- `COGNIS_LOCAL_CHAT_MODEL`

This repository does not resolve Cognis credential IDs in scripts. If your
running Cognis instance stores a LiteLLM credential, inject those env vars from
that credential externally before starting or seeding Local Compose. The seeded
provider stores only the env-var reference, not the API key value.

### ChatGPT Subscription (Codex)

Use this when Cognis should call LiteLLM's native ChatGPT subscription provider. This preset uses OAuth device-code authentication instead of an API key. Create the provider first, then start OAuth from the provider editor. The UI shows the verification URL and user code; after you approve in the browser, Cognis stores the resulting token cache in the encrypted secrets table.

The controller remains stateless: LiteLLM's `CHATGPT_TOKEN_DIR` file is only hydrated into a temporary directory for each model call, and refreshed tokens are written back to encrypted database storage. On PostgreSQL, Cognis serializes OAuth token hydration and refresh with a transaction-scoped advisory lock so multiple controller replicas do not refresh the same token concurrently.

#### Image preparation

Direct Codex and Anthropic-preset chat requests normalize authorized Cognis
image artifacts before sending them, for both streaming and non-streaming
generation. Anthropic images are prepared on the controller before native,
proxy, or executor dispatch; the 2048-pixel ceiling also satisfies Anthropic's
3000-pixel limit for requests containing more than 20 images. External image
URLs without Cognis artifact metadata are not fetched or normalized.
Cognis keeps originals unchanged and limits the prepared image to 2048 pixels
on its longest edge, preserving aspect ratio and applying EXIF orientation.
Supported still-image formats are JPEG, PNG, and WebP; other decodable formats
are converted to PNG, using the first frame for animations.

These are Cognis safety and transport budgets, not model capability claims:
source files are limited to 20 MiB, full decodes to 16 Mi pixels, and the total
encoded image payload to 28 MiB (including repeated references). JPEG sources
may contain up to 64 Mi pixels because the codec can downsample before full
decode. Cognis checks the reduced dimensions before allocating the decoded
pixels; other formats retain the 16 Mi-pixel source limit.
JPEG-backed MPO phone photos use the same safe decoder downsampling and are
flattened to a single JPEG primary frame; auxiliary frames are not decoded or
sent to the model.

Normalized content is reused through a bounded 64 MiB, content-hash LRU cache
on each controller provider instance. Concurrent requests share normalization,
but authorization is checked for every request. Eviction, provider recreation,
or controller restart can require normalization again; the cache is not durable
or shared between replicas. Unavailable or unsafe images are replaced with a
model-facing notice identifying the artifact and the rejection reason.

## Provider ownership

Providers can be shared or user-owned. Shared providers use the system owner, are visible to all users, and are managed by admins. User-owned providers are visible and manageable only by their owner. Admin role alone does not grant access to another user's user-owned provider.

Default provider/model routing is scoped by owner: shared defaults apply to shared/system-owned providers, while user-owned defaults apply only to that user. Explicit model lookup follows the same visibility rules and caches provider matches per owner scope, so one user's user-owned provider cannot satisfy another user's model request.

## Provider location and executor routing

Some providers can run on the controller, while others can be routed through an executor. This is useful when:

- the model endpoint is only reachable from a remote executor
- inference should stay on a user-local machine
- you want tool execution and model access to share the same remote environment

Executor-routed text providers use the executor-side LiteLLM backend. The
controller resolves provider configuration, credentials, routing, memory, and
guardrails; the selected executor performs the outbound model call from its own
network location.

## Managing models

Each provider has a list of configured models with their properties, such as context window, output limits, capability flags, and pricing metadata.

### Discovering models

Click **Discover** to query the provider for available models. For LiteLLM Proxy providers, Cognis fetches enriched metadata from the proxy's `/model/info` endpoint, including context window sizes, capability flags, and pricing metadata. For other controller-side providers, models are enriched with LiteLLM's built-in model database. For executor-routed providers such as a local Meridian endpoint, add models manually because discovery runs from the controller.

Discovered models appear in a selection modal where you can choose which to add. You can also add models manually by typing a model ID.

### Editing model properties

Each configured model shows its properties, including context window, max output tokens, capabilities, and pricing metadata. Click **Edit** to override any property. User-configured overrides always take precedence over auto-detected values.

This is particularly useful when:

- the auto-detected context window is incorrect or outdated
- you want to restrict capabilities for a specific model
- you need to record custom pricing metadata for planning or documentation

### Default model

Select one configured model as the default. This is the model used when no explicit model is specified in routing or agent configuration.

## Testing a provider

Use the built-in test action before depending on a provider for chat or workflows.

The test confirms:

- model resolution
- basic credentials
- connectivity
- sanitized error details when something fails

## Model routing

Use model routing to choose which provider/model should handle different kinds of work, such as:

- default chat
- lightweight classification
- context compaction
- workflow evaluation
- speech to text
- image generation
- embeddings for optional Knowledgebase indexing and retrieval

Routing lets you use a cheaper or faster model for simple tasks and keep a stronger model for heavier work. Text routes can also define a default Thinking effort for the selected model. Routes are resolved in the acting user's owner scope first and then fall back to shared/system routes when no user-owned route is configured.

The optional Knowledgebase feature is hidden/unavailable until an `embedding`
route is configured and a supported vector backend is enabled. Use an
embedding-capable provider/model for this route; Cognis does not hard-code an
embedding provider.

## Credentials and secrets

There are two different credential layers in Cognis:

- provider credentials used for model calls
- encrypted secrets used by tools and executors

Provider credentials can come either from environment variables or from Cognis-managed encrypted secrets, depending on how you choose to operate the deployment.

For example:

- `OPENAI_API_KEY` may power provider traffic
- a stored secret may be injected into a GitHub MCP server or other tool runtime

## Web tool backends

The `Settings -> Web` page configures the search and fetch backends agents
use through the `web_search`, `web_fetch`, `web_crawl`, `web_map`, and
`web_research` tools. Search and fetch are independent: each agent's
`web_search` uses the configured **search backend** and `web_fetch` uses the
**fetch backend**.

| Backend | Search | Fetch | Notes |
| --- | --- | --- | --- |
| `direct` | DDGS metasearch | httpx + trafilatura | Free; no API key. The default. |
| `tavily` | tavily.com | tavily.com | Paid. Also unlocks Tavily-native `web_crawl` / `web_map` / `web_research`. |
| `brave` | api.search.brave.com | — | Paid. Search-only. |
| `searxng` | self-hosted SearXNG | — | Free; user runs the SearXNG instance. |
| `browser` | — | Playwright/Patchright headed when allowed, otherwise headless | Auto-fallback target for Cloudflare/JS-required pages. |

### Browser fallback

When the direct fetch backend hits a Cloudflare/5xx/connection error and
`web.fetch_fallback_browser` is enabled (default `true`), the request is
automatically retried through the executor's browser. If
`web.browser_fetch.headed_fallback_enabled` is enabled (default `true`) and the
executor also sets `browser.headed_allowed=true`, Cognis uses headed browser fetch.
Executors without headed-session support use headless browser fetch instead. Browser
fetch navigation defaults to
`wait_until=domcontentloaded`, a 60 second navigation timeout, and a short
best-effort `networkidle` soft wait. Hard cases (Cloudflare managed challenge,
Turnstile) can still fail; HTTP error documents and short provider-generated
error pages are reported as failures rather than successful extractions.
Upgraded installations retain an explicitly stored headed-fallback choice;
new installations default it to enabled.

### Direct search limitations

Direct search uses explicit free DDGS engines for each search type. General web
search excludes encyclopedia-first engines. The result metadata identifies the
engines that returned results and their underlying index families.

These engines scrape public search pages. They have no availability contract.
Cognis reports provider failures instead of treating them as an empty search.
Each search has one 12-second process deadline. Cognis terminates the process
when this deadline expires.

### `web_crawl` / `web_map` / `web_research` availability

`web_crawl` and `web_map` are available on every executor (W3): when the
fetch backend is `tavily` they route to Tavily's native engines;
otherwise an in-tree DIY crawler / sitemap discovery is used. The DIY
path inherits the auto-browser-fallback so JS-heavy sites still produce
usable results.

`web_research` is **only exposed when Tavily is configured**. The free
research path is the agent loop itself: the `cognis-web-research` skill
(`Settings > Skills`) gives the agent a recipe for diversified
searches, parallel fetches, and cited synthesis. Frontier models do
this well without a special tool.

### Running your own SearXNG

cognis does not run SearXNG itself. The simplest setup uses the upstream
Docker image:

```
docker run -d --name searxng -p 8888:8080 searxng/searxng
```

Then set `web.searxng_url` to `http://localhost:8888` (or wherever your
instance is reachable from the executor). For higher-quality results
configure a curated engine list in your SearXNG instance (defaults to
Google + Bing + DuckDuckGo + Mojeek + Qwant). Public community SearXNG
instances are not recommended for agent fleets — they get rate-limited
quickly.

### Concurrency

Parallel agent calls share a per-executor concurrency controller with
configurable global / per-backend / per-host caps and per-backend qps
limits. Defaults are tuned for free-tier Brave (1 qps), generous for
direct/tavily, and bounded by the browser session pool for the browser
backend. Knobs live under `web.concurrency.*` and `web.rate_limit.*` in
`Settings > Web`.

## Troubleshooting tips

- Confirm the default model name is valid for the selected provider.
- If using Ollama or a local compatible API, verify the base URL from the Cognis host or executor.
- If a provider is routed through an executor, confirm the executor is connected and healthy.
- For ChatGPT subscription providers, complete OAuth before testing the provider and keep the Cognis secrets encryption key stable across controller replicas.
