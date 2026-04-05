# Channels

Channels connect Cognis agents to external messaging platforms such as Slack, Discord, Telegram, Signal, and similar adapters.

![Channel pairing and verified sender flow](../assets/images/cognis-channel-pairing-flow.svg)

## What a channel account does

A channel account binds:

- one platform connection
- one Cognis agent
- one adapter placement mode
- the credentials and settings needed for that platform

This lets a user talk to a Cognis agent through an external service while keeping the same conversation, session, and safety model.

## Creating a channel account

Open `Channels` and start in the `Accounts` view. The page is organized around three workflows:

- `Accounts` for platform setup and lifecycle management
- `Pairing inbox` for pending remote-sender approvals
- `Verified senders` for manual trust overrides

When you add or edit an account, Cognis shows:

- a short platform description
- manual setup guidance
- credential fields
- adapter settings
- whether a public webhook URL is required

After the account is saved, Cognis uses the configured adapter to receive and send messages for that platform.

The platform-specific setup steps stay next to the editor so you can finish vendor-side configuration while filling in the Cognis settings.

## Controller vs executor placement

Each channel account can run on:

- `Controller` for webhook-based or cloud-hosted integrations
- `Executor` for adapters that need user-local services or network reachability

Use executor placement when the integration depends on something that should stay near the user, such as a local helper service.

## Pairing and trust

Channel accounts can require sender verification before unknown remote users are allowed to interact with an agent.

This pairing flow helps prevent an unverified external sender from immediately gaining access to a user-owned agent. The UI stores verified mappings between external contacts and Cognis users.

In practice, the pairing inbox becomes the operational view for pending verifications, while verified senders is the advanced list of remote contacts that have already been trusted.

## Platform-specific setup

Different adapters have different requirements. Examples include:

- bot tokens
- webhook verification secrets
- service account credentials
- local API URLs for helper services

The `Channels` page includes platform-specific manual setup steps next to the form so operators can complete the vendor-side configuration without leaving the app flow.

## Signal: REST API vs direct JSON-RPC

Signal supports two transport modes:

### REST API (default)

Uses an external [signal-cli-rest-api](https://github.com/bbernhard/signal-cli-rest-api) service. You run the REST API yourself (e.g. via Docker), link your Signal number, and provide the API URL and phone number in the channel account credentials.

- Works with both controller and executor placement.
- Requires a running signal-cli REST API instance.

### Direct JSON-RPC (executor-only)

Runs `signal-cli` directly on the executor as a managed subprocess using JSON-RPC over stdio. No external REST API service is needed.

**Requirements:**

- `adapter_location` must be set to `executor` with an explicit `executor_id`.
- The executor must have `signal.direct_enabled: true` in its config.
- The executor must have `signal-cli` installed and accessible. The command path can be customized via `signal.command` in the executor config (default: `signal-cli`).
- The Signal account must already be linked/registered on the executor machine. Onboarding through the Cognis UI is not yet supported.

**Features:**

All Signal features are enabled by default in direct mode: typing indicators, read receipts, profile sync, and attachment handling. If a feature is unsupported by the installed `signal-cli` version, it degrades gracefully without breaking message send/receive.

**Limitations:**

- Signal account state lives on the executor machine. Moving an account to a different executor requires manually migrating the signal-cli data directory.
- The adapter uses the standard reconnection loop with exponential backoff. If the signal-cli subprocess crashes, the adapter will attempt to restart it automatically.
- No UI-driven onboarding (linking/registration) yet.

## Operational notes

- One bot or channel identity should map to one agent unless the platform explicitly supports a different pattern.
- Webhook-based platforms need a reachable public URL.
- Executor-hosted adapters depend on the selected executor being connected and healthy.
