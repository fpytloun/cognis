# Configuring Providers

Providers tell Cognis which model backends are available for chat, routing, classification, compaction, and workflow execution.

## Where provider settings live

Open `Settings` and use the providers and routing sections.

Provider configuration is stored in the Cognis database. All provider settings are managed through structured UI forms.

## Supported presets

The UI includes guided forms for:

- **OpenAI** for direct OpenAI API access
- **OpenAI Compatible** for APIs that follow the OpenAI chat format
- **Anthropic** for direct Anthropic API access
- **Ollama** for local Ollama deployments
- **LiteLLM Proxy** for a LiteLLM proxy that performs routing upstream
- **ChatGPT Subscription (Codex)** for ChatGPT Pro/Max subscription access through LiteLLM's `chatgpt/` provider route

For provider-specific settings not covered by the structured fields (e.g., Azure `api_version`), use the collapsible **Advanced settings** section to add key-value pairs.

## Common provider choices

### OpenAI

Use this when Cognis should call OpenAI directly. Provide the API key and a default model.

### OpenAI Compatible

Use this for vLLM, TGI, local gateways, or other services that expose an OpenAI-style API.

### Anthropic

Use this when Cognis should call Anthropic directly for chat or routing tasks.

Anthropic-compatible endpoints can also use this preset with a custom base URL.
This keeps LiteLLM on its Anthropic transport, so Claude-specific options such
as extended thinking are sent using the Anthropic wire format instead of being
filtered as unsupported OpenAI-compatible parameters. For example, a Meridian
endpoint that exposes the Anthropic Messages API should be configured as an
Anthropic provider with its Meridian base URL, not as an OpenAI Compatible
provider.

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

### LiteLLM Proxy

Use this when a LiteLLM proxy is already aggregating your models. Cognis treats the proxy as the provider and lets the proxy perform the final upstream routing.

### ChatGPT Subscription (Codex)

Use this when Cognis should call LiteLLM's native ChatGPT subscription provider. This preset uses OAuth device-code authentication instead of an API key. Create the provider first, then start OAuth from the provider editor. The UI shows the verification URL and user code; after you approve in the browser, Cognis stores the resulting token cache in the encrypted secrets table.

The controller remains stateless: LiteLLM's `CHATGPT_TOKEN_DIR` file is only hydrated into a temporary directory for each model call, and refreshed tokens are written back to encrypted database storage. On PostgreSQL, Cognis serializes OAuth token hydration and refresh with a transaction-scoped advisory lock so multiple controller replicas do not refresh the same token concurrently.

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
| `direct` | DuckDuckGo (`ddgs`) | httpx + trafilatura | Free; no API key. The default. |
| `tavily` | tavily.com | tavily.com | Paid. Also unlocks Tavily-native `web_crawl` / `web_map` / `web_research`. |
| `brave` | api.search.brave.com | — | Paid. Search-only. |
| `searxng` | self-hosted SearXNG | — | Free; user runs the SearXNG instance. |
| `browser` | — | Playwright/Patchright headed when allowed, otherwise headless | Auto-fallback target for Cloudflare/JS-required pages. |

### Browser fallback

When the direct fetch backend hits a Cloudflare/5xx/connection error and
`web.fetch_fallback_browser` is enabled (default `true`), the request is
automatically retried through the executor's browser. If
`web.browser_fetch.headed_fallback_enabled` is enabled and the executor also sets
`browser.headed_allowed=true`, Cognis uses headed browser fetch first. Otherwise
it uses headless browser fetch. Browser fetch navigation defaults to
`wait_until=domcontentloaded`, a 60 second navigation timeout, and a short
best-effort `networkidle` soft wait. Hard cases (Cloudflare managed challenge,
Turnstile) can still fail; the agent should treat that as a real signal rather
than retrying mechanically.

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
