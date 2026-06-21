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

Each section is also reachable by a clean URL so you can bookmark or link to
a specific tab:

- `/settings/providers`
- `/settings/routing`
- `/settings/secrets`
- `/settings/web`
- `/settings/tools`
- `/settings/executors`
- `/settings/users`
- `/settings/system`
- `/settings/account`

These all land on the main settings page with the matching tab selected.

## Providers and routing

Use the provider and routing tabs to decide:

- which model backends are available
- which model should handle each task type
- whether a provider should run on the controller or through an executor

For a first deployment, configure one provider, test it, then add routing rules only when you need different models for different jobs.

## Secrets

Secrets are stored encrypted and can be reused by tools, MCP servers, channels, and provider configurations.

Use the secrets section when you want credentials managed inside Cognis rather than only through environment variables.

The same area now also exposes structured **credentials** for agent-facing
authentication flows. Use credentials when you need metadata and typed payloads,
for example:

- username/password login records
- tokens scoped to a target URL or domain
- saved browser authentication state
- MFA-related recovery material such as TOTP seeds or recovery codes

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

Executor cards also include **Browser Automation** settings for Playwright-based
browser tools. These control whether browser automation is enabled on the
executor, whether browser binaries may be auto-installed, whether headed mode is
allowed, whether persistent local profiles are enabled, and how many concurrent
browser sessions the executor should allow.

For a human-like local browser setup, the main browser settings to look at are:

- `Persistent local profiles`: keeps Playwright user data on that executor so cookies and local storage persist across runs
- `Default profile mode`: choose whether normal `browser_open` calls default to `persistent_local` or `ephemeral`
- `Realistic launch defaults`: uses a stable desktop-like viewport and reduced automation signals
- `Auto-start Xvfb`: on Linux, starts a virtual X server for headed browser launches when no real `DISPLAY` is available

Use `persistent_local` defaults on sticky local executors that should behave
more like a human browser. Use `ephemeral` defaults on temporary executors or
when you want a clean context for every run.

## Tools and MCP servers

The tools section inside settings manages shared MCP server definitions. These are configured globally and then assigned to executors.

This split matters because agents inherit tools from their executor rather than talking to MCP servers directly.

HTTP MCP servers can use controller-managed OAuth when their authentication mode
is set to `oauth2`. Cognis starts an authorization challenge, stores the
resulting tokens encrypted per user/server/provider/resource, refreshes them on
the controller, and sends only a bearer access token to the executor. OAuth is
available only for HTTP MCP transports; stdio servers still use command,
arguments, environment, and secrets.

MCP OAuth supports authorization-code + PKCE for providers that accept the
Cognis callback URL, and OAuth device-code flow for providers that advertise a
`device_authorization_endpoint`. Leave the flow on `auto` unless a provider
requires a specific mode. Device-code challenges show a verification URL and
user code; after the operator authorizes in the provider browser page, Cognis
polls the provider, stores the resulting token, and resumes MCP configuration
without requiring a public redirect URI.

Do not configure a static `Authorization` header on an OAuth MCP server. Non-auth
headers such as feature flags or tenant IDs may still be configured.

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
