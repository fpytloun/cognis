# Settings

The `Settings` workspace is where operators configure the Cognis environment after the first login. It is the main place for providers, routing, secrets, executors, MCP servers, diagnostics, and user administration.

## Chat preparation latency

Intaris intention generation is submitted before inference without waiting for
generation to finish. Intaris tool evaluation still owns the intention barrier;
this does not bypass tool authorization or canonical event persistence.

`session.optional_recall_timeout_seconds` defaults to **0.5 seconds** for optional
per-turn memory search. A timeout cancels that search and surfaces a notice that
the turn continues without recalled memories. Core identity/instruction loading
is not subject to this optional-search budget, and explicit memory tools retain
their own timeouts. Existing database overrides are preserved.

Content-free `turn: stage completed` logs distinguish runtime resolution, core
identity preparation, optional recall, event refresh, Intaris status reads,
reasoning submission, and context delivery. CPU preparation logs separately
report queue wait and execution duration. Correlate by `turn_id`/`session_id`;
stage durations can overlap and must not be blindly summed. These server-side
timings do not measure browser paint or replace provider TTFT measurements.

## Main areas

For speech-provider setup, the optional listening preference, the
`audio_narrative` route, and deliverable narration/podcasts, see
[Speech and audio](speech.md).

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
Only the selected section's data is loaded; unrelated diagnostics and registries
do not delay it. Navigation warnings apply to unsaved edits in that section,
not to values initialized when its form opens.

## Agent-operated instance management

QuickStart enables full instance-management tool access for the first agent by
default, independent of the selected agent template. Its available management
skills are attached and auto-loaded. Advanced options allow explicit opt-out.
Existing agents are not silently upgraded: enable individual tools in the agent
editor and attach the corresponding skills.
An owner can also use `manage_agents` with `tools_add` to assign an individual
management tool, for example `allow_tools: ["builtin:manage_settings"]`.
The owner's root primary chat lists unassigned management tools as assignment
candidates without enabling their discovery or execution. Changing the current
agent still requires explicit user review before persistence. New agents do not
inherit management tools merely because those tools are assignable.

| Tool | Scope | Skill |
| --- | --- | --- |
| `manage_agents` | User-owned agents, profiles, sharing and bindings | `cognis-agent-manager` |
| `manage_mcp` | MCP servers, assignments and runtime capability refresh | `cognis-mcp-manager` |
| `manage_providers` | LLM provider CRUD, discovery, defaults, health and OAuth | `cognis-provider-manager` |
| `manage_routes` | Custom named inference routes and system task bindings | `cognis-route-manager` |
| `manage_settings` | Typed settings, web settings and workflow step profiles | `cognis-settings-manager` |
| `manage_executors` | Executor CRUD, defaults and reconfiguration | `cognis-executor-manager` |

Management is an explicit agent opt-in, not an authorization bypass. Tools execute
only in the owner's root primary chat, not tasks, delegates, participant chats, or
project-scoped contexts. Administrator operations still require an administrator.
Another user's private providers and executors remain private even to an
administrator. Intaris, approval controls, and plan-mode restrictions remain active.

Use `search_tools` to discover a domain tool and `describe_tool` with the exact
operation name for its action-specific schema. Configuration mutations support
`preview=true`; previews validate without persisting or triggering runtime effects.
Read before writing and provide the returned `expected_updated_at` for providers,
executors, and settings, or `expected_revision` for named routes, bindings, and
workflow profiles. Conflicts require a fresh read and review, not a blind retry.

Omitted fields preserve current values. Provider/executor config objects and
collections replace rather than deep-merge. `bindings_update` patches only explicitly
supplied task types; `bindings_reset` clears only the specified task types.
Named routes use `cognis/<name>` references, distinct from their route IDs.
`resolve` uses the inference planner without generating a response.

Create providers and executors with explicit stable IDs. After an uncertain result,
read that exact ID before retrying; duplicate IDs do not overwrite resources.
Dependency conflicts never cascade or silently rewrite consumers.

Provider discovery and enrichment do not save model metadata. Provider tests may
incur inference charges and are cooldown-limited; OAuth status polling can complete
authorization. Inline credentials are rejected. Use `config.api_key_secret` to
reference stored provider credentials and secure user-facing credential flows.
Executor token provisioning/rotation remains in the authenticated UI so tokens
never enter agent context.

Saving settings or executor configuration does not prove remote initialization.
Read back durable values and runtime status separately. Web setting changes validate
backend readiness and trigger cancellation-safe executor reconfiguration; disabling
an active backend restores affected defaults to direct access. Typed settings expose
their application timing. Failed hot application attempts guarded recovery without
overwriting a newer durable value. These tools do not expose arbitrary database
keys, environment editing, service restarts, secret retrieval, or user administration.

## Providers and routing

For typed usage-retention defaults, bounded previews, coverage limitations and
explicit activation, see [Usage accounting storage and retention](usage-accounting.md).

Use the provider and routing tabs to decide:

- which model backends are available
- which model should handle each task type
- whether a provider should run on the controller or through an executor

For a first deployment, configure one provider, test it, then add routing rules only when you need different models for different jobs.

Image tools offer configured models from active providers visible to the caller,
using `provider_id/model_id` references to avoid ambiguity. Omit `model` to use
the image-generation route; its resolved provider/model is shown as the default.
Options are rebuilt when a new turn's runtime is created, including in an existing
conversation. An already-running turn retains its current tool schema.

For Gemini image generation and editing, `size` is a sizing hint rather than
exact output dimensions: `WxH` maps to the nearest supported aspect ratio and
1K/2K/4K resolution tier. Gemini 2.5 Flash receives only the aspect ratio because
it does not support selectable resolution. Missing, `auto`, or invalid sizes
leave provider defaults unchanged. This applies to controller and executor
inference; OpenAI image sizes are passed through unchanged.

### Model discovery metadata

**Discover** lists available wire model IDs and enriches them with hosting-provider
catalog metadata. The same resolver supplies runtime metadata and manual model
enrichment. It loads reported context/input/output limits, capabilities, supported
parameters and reasoning values, token and cache prices, model mode, and other
pricing units (for example transcription prices per second). Provider-reported
fields take precedence over catalog fields; saved model overrides take precedence
over both. Discovery never changes saved overrides.

For OpenAI-compatible endpoints, the protocol is not the hosting identity. Cognis
recognizes exact known API hosts, including Groq, and preserves publisher
namespaces such as `openai/gpt-oss-20b`. For an unrecognized compatible endpoint,
set the provider advanced setting **catalog_provider** to its LiteLLM provider
name only when that catalog describes the actual host (for example `groq` behind
a private gateway). This setting changes metadata lookup, not inference routing.
Unknown compatible endpoints do not inherit OpenAI pricing merely because their
model IDs resemble OpenAI model names.

Metadata can be partial. Missing/null fields remain unknown in discovery, and
catalog lookup status and field sources are returned in `provider_metadata`.
Runtime still uses existing safety defaults when limits are unknown; the editor
does not present or save those defaults as discovered facts. Catalog prices are
estimates, not account-specific billing quotes. Live provider endpoints need not
report prices or every capability. Existing richer proxy and Ollama adapters
continue to supply their own metadata. Saved discovery snapshots can enrich
runtime metadata for 60 seconds; saving selected fields preserves them as model
configuration, and changing provider configuration prevents snapshot reuse.

### Provider health and usage

Enabled providers appear in the dashboard's compact Providers strip. Notifications
is the default tab when issues exist. Use the chevron beside the tabs to collapse
or expand the panel; the tabs and counts remain visible even with a single tab.
Selecting a different tab opens its content; clicking the current tab toggles
the panel. This browser remembers the collapse choice across visits.
The activity sidebar and provider editor use the same cached observations.

Provider settings include **Automatic health and usage checks**, a check interval
(default **15 minutes**), and remaining-quota thresholds (warning **20%**, critical
**0%**). Critical must not exceed warning. Checks use subscription usage endpoints
or a read-only metadata endpoint; they never generate an LLM response. Connectivity
does not guarantee that generation will succeed. Unsupported checks show unknown.
Executor-hosted connectivity probes are currently unsupported.

Disabling automatic checks stops all automatic upstream refreshes, including when
opening a view. Cached usage stays visible and the refresh icon still works.
**Refresh now** explicitly refreshes the shared cache; ordinary loading reuses it
until its deadline. A successful earlier check postpones the scheduled refresh.
Database leases coordinate manual and scheduled checks across controller replicas.

Last-refreshed timestamps older than one hour are amber and marked stale. Failed
checks preserve the last successful observations and back off. Quota issues are
evaluated from fresh, unexpired windows and appear only inside Cognis, not through
push or external notification channels. Anthropic API-key limits come from previously
observed response headers, not a proactive usage API; refreshing cannot make those
observations fresh. Without observations, usage is unknown.

Controller-hosted ElevenLabs API keys report subscription credits, remaining
credits, plan, and the provider's actual reset date through the same monitor.
These are credit units, not necessarily input characters; model credit rates
can differ. Enabled usage-based billing and its reported cap are kept separate
from the included allowance. Missing counts or restricted subscription access
mean unknown quota, not exhausted credits or an inference outage. A quota
permission refusal clears the old meter instead of presenting it as current.
Executor-hosted ElevenLabs credentials do not currently support this quota read.

## Notification inbox

The bell opens `/notifications`: a persistent, personal inbox with an unread
count, history, an **Unread** filter, and **Mark displayed as read**.
New shares, access changes, and revoked access to agents, projects, and knowledge
bases appear here. Repeating an unchanged grant does not create another entry.
Knowledge-base grants currently have one read-only role, so note-only edits are
not access changes.

Unread inbox items also appear in the dashboard's **Notifications** tab alongside
live system and provider notices. Reading an inbox item does not resolve an
approval, dismiss a system issue, or change access. Approval prompts keep their
existing attention actions. Notification links check current access; history
does not restore a revoked grant.

New entries produce an in-app toast while the app is focused and use the existing
browser/PWA Web Push subscription when enabled for the recipient's device. Push
and realtime hints are best effort. The inbox survives restarts and refreshes on
reconnect, focus, and periodically while visible. Existing history is not replayed
as push after a restart. There is no email delivery in this feature.

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
