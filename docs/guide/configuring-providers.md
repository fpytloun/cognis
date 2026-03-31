# Configuring Providers

## Supported presets

The Settings page provides guided forms for:

- **OpenAI** — direct OpenAI API
- **OpenAI Compatible** — any endpoint speaking the OpenAI API format (vLLM, local servers, etc.)
- **Anthropic** — direct Anthropic API
- **Ollama** — local Ollama instance
- **LiteLLM Proxy** — a running [LiteLLM proxy](https://docs.litellm.ai/docs/providers/litellm_proxy) that handles model routing internally
- **Custom** — raw JSON configuration

The UI stores provider settings as JSON in the database, but you do not need to edit raw JSON unless you choose the Custom preset.

### LiteLLM Proxy

If you run a LiteLLM proxy server that aggregates multiple LLM backends, use the **LiteLLM Proxy** preset. Cognis will use LiteLLM's dedicated `litellm_proxy/` model prefix so the proxy handles all routing internally. Enter the proxy's base URL (default `http://localhost:4000`) and the proxy API key.

### OpenAI Compatible

Use **OpenAI Compatible** for non-proxy endpoints that speak the OpenAI chat completions format (vLLM, text-generation-inference, local servers, etc.). Cognis prefixes model names with `openai/` so LiteLLM routes correctly even for non-standard model names.

## API keys vs encrypted secrets

- **LLM provider credentials** are typically read from environment variables before Cognis starts.
- **Encrypted secrets** are injected into executor sandboxes for tool use.

Examples:

- `OPENAI_API_KEY` for OpenAI provider traffic
- encrypted `github_token` for tools that access GitHub

## Testing providers

Use **Test provider** to run a small completion against the configured default model.

The test reports:

- resolved model name
- latency
- sanitized failure details

## Model routing

Use **Settings → Routing** to choose models for:

- `default`
- `classifier`
- `compaction`
- `simple_inline`

The routing UI shows models from all configured providers and warns when a route references a model that is not present in the configured catalog.
