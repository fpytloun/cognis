# Configuring Providers

## Supported presets

The Settings page provides guided forms for:

- **OpenAI**
- **Anthropic**
- **Ollama**
- **Custom**

The UI stores provider settings as JSON in the database, but you do not need to edit raw JSON unless you choose the Custom preset.

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
