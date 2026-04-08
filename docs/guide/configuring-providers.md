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

For provider-specific settings not covered by the structured fields (e.g., Azure `api_version`), use the collapsible **Advanced settings** section to add key-value pairs.

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

## Managing models

Each provider has a list of configured models with their properties (context window, output limits, capabilities, and costs).

### Discovering models

Click **Discover** to query the provider for available models. For LiteLLM Proxy providers, Cognis fetches enriched metadata from the proxy's `/model/info` endpoint, including accurate context window sizes, capability flags, and pricing. For other providers, models are enriched with litellm's built-in model database.

Discovered models appear in a selection modal where you can choose which to add. You can also add models manually by typing a model ID.

### Editing model properties

Each configured model shows its properties (context window, max output tokens, capabilities, costs). Click **Edit** to override any property. User-configured overrides always take precedence over auto-detected values.

This is particularly useful when:

- the auto-detected context window is incorrect or outdated
- you want to restrict capabilities for a specific model
- you need to set custom pricing for cost tracking

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
