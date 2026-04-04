# Configuring Providers

Providers tell Cognis which model backends are available for chat, routing, classification, compaction, and workflow execution.

## Where provider settings live

Open `Settings` and use the providers and routing sections.

Provider configuration is stored in the Cognis database. You usually do not need to edit raw JSON unless you are using the custom preset.

## Supported presets

The UI currently includes guided forms for:

- **OpenAI** for direct OpenAI API access
- **OpenAI Compatible** for APIs that follow the OpenAI chat format
- **Anthropic** for direct Anthropic API access
- **Ollama** for local Ollama deployments
- **LiteLLM Proxy** for a LiteLLM proxy that performs routing upstream
- **Custom** for manual configuration

## Common provider choices

### OpenAI

Use this when Cognis should call OpenAI directly. Provide the API key and a default model.

### OpenAI Compatible

Use this for vLLM, TGI, local gateways, or other services that expose an OpenAI-style API.

### Anthropic

Use this when Cognis should call Anthropic directly for chat or routing tasks.

### Ollama

Use this for local models exposed through Ollama. Make sure the selected model is already installed and reachable from the Cognis host or executor.

### LiteLLM Proxy

Use this when a LiteLLM proxy is already aggregating your models. Cognis treats the proxy as the provider and lets the proxy perform the final upstream routing.

## Provider location and executor routing

Some providers can run on the controller, while others can be routed through an executor. This is useful when:

- the model endpoint is only reachable from a remote executor
- inference should stay on a user-local machine
- you want tool execution and model access to share the same remote environment

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
- simple inline turns

Routing lets you use a cheaper or faster model for simple tasks and keep a stronger model for heavier work.

## Credentials and secrets

There are two different credential layers in Cognis:

- provider credentials used for model calls
- encrypted secrets used by tools and executors

Provider credentials can come either from environment variables or from Cognis-managed encrypted secrets, depending on how you choose to operate the deployment.

For example:

- `OPENAI_API_KEY` may power provider traffic
- a stored secret may be injected into a GitHub MCP server or other tool runtime

## Troubleshooting tips

- Confirm the default model name is valid for the selected provider.
- If using Ollama or a local compatible API, verify the base URL from the Cognis host or executor.
- If a provider is routed through an executor, confirm the executor is connected and healthy.
