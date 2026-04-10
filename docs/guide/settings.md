# Settings

The `Settings` workspace is where operators configure the Cognis environment after the first login. It is the main place for providers, routing, secrets, executors, MCP servers, diagnostics, and user administration.

## Main areas

Depending on your role, the settings workspace can include:

- providers
- routing
- web search
- secrets
- executors
- tools / MCP servers
- system diagnostics
- users
- account settings and API keys

## Providers and routing

Use the provider and routing tabs to decide:

- which model backends are available
- which model should handle each task type
- whether a provider should run on the controller or through an executor

For a first deployment, configure one provider, test it, then add routing rules only when you need different models for different jobs.

## Secrets

Secrets are stored encrypted and can be reused by tools, MCP servers, channels, and provider configurations.

Use the secrets section when you want credentials managed inside Cognis rather than only through environment variables.

## Web search

The web search section controls which search provider integrations are available to the workspace. Configure this early if your agents or tools need live web results.

## Executors

The executors section lets you inspect:

- configured executor instances
- connection and readiness state
- enabled tool groups and tools
- assigned MCP servers
- token generation for remote executors

This is also where you decide whether local executor modes should remain enabled in production.

## Tools and MCP servers

The tools section inside settings manages shared MCP server definitions. These are configured globally and then assigned to executors.

This split matters because agents inherit tools from their executor rather than talking to MCP servers directly.

This is separate from the top-level `Tools` workspace:

- `Settings -> Tools` manages MCP server definitions and attachment points
- `Tools` lets you inspect the tool registry and manage skills

## System diagnostics

The system tab is the best place to confirm that a new deployment is healthy. It shows:

- readiness checklist
- provider health
- database details
- JWT key fingerprint
- configuration summary
- system settings editor for advanced operators

If the workspace says setup is incomplete, the system tab usually explains why.

## Users and account

Admins can manage users from settings, including:

- create user
- edit name or role
- disable or re-enable access
- delete users

Individual users can also manage their own API keys and account details from the account area.

## Recommended operator flow

For a fresh environment, this order usually works best:

1. confirm diagnostics and readiness
2. configure provider and routing
3. add secrets if needed
4. configure executors and MCP servers
5. create agents
6. optionally add channels and pairing policies
7. optionally add schedules for recurring work
