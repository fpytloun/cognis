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

## Signal: end-to-end setup

Signal supports two transport modes:

- `rest_api` — Cognis talks to an already running `signal-cli-rest-api`
- `direct_jsonrpc` — Cognis starts `signal-cli` directly on the executor

Both modes require the Signal account to be linked or registered outside the Cognis UI first.

### Choose the right mode

Use `REST API` when:

- you already run `signal-cli-rest-api`
- you want a simpler, externally managed setup
- you may run the adapter on either the controller or an executor

Use `direct JSON-RPC` when:

- you want Cognis to manage the `signal-cli` process for you
- the Signal state should stay on the same machine as the executor
- you want executor-hosted Signal with typing, read receipts, profile sync, and attachments enabled by default

### Common prerequisites

Before creating the channel account in Cognis:

1. Install and prepare Signal on the target machine.
2. Link or register the Signal account with `signal-cli`.
3. Confirm you can send or receive messages with that account outside Cognis.
4. Decide whether Cognis should use `REST API` or `direct JSON-RPC`.

Important:

- Cognis does not yet provide Signal onboarding in the UI.
- The Signal account state stays on the machine that runs `signal-cli`.
- If you move a direct-mode account to another executor, you must migrate the `signal-cli` state directory yourself.

### REST API mode

REST mode uses an external [signal-cli-rest-api](https://github.com/bbernhard/signal-cli-rest-api) service.

#### 1. Prepare Signal

On the machine that will host Signal:

1. Install `signal-cli` and `signal-cli-rest-api`.
2. Link or register the Signal account.
3. Start the REST API service and make sure it can access the prepared `signal-cli` account state.
4. Verify the REST API is reachable, for example by checking its `about` endpoint.

#### 2. Create the Cognis account

In `Channels -> Accounts -> New account`:

1. Choose `Signal`.
2. Select the target agent.
3. Set `Transport` to `rest_api`.
4. Choose `Controller` or `Executor` placement.
5. If using executor placement, optionally choose a specific executor.
6. Fill in credentials:
   - `signal-cli REST API URL`
   - `Phone number` in E.164 format
7. Save the account.

#### 3. Start and verify

1. Start the account from the `Channels` page.
2. Send a test message to the Signal number.
3. Confirm the account reaches `connected` state and the agent replies.

### Direct JSON-RPC mode

Direct mode runs `signal-cli` as a managed subprocess on the executor using JSON-RPC over stdio.

#### 1. Prepare the executor machine

On the machine that runs the Cognis executor:

1. Install `signal-cli`.
2. Link or register the Signal account with `signal-cli` on that same machine.
3. Confirm the linked account works from the command line.
4. Keep the Signal state on that machine; direct mode expects local access to it.

#### 2. Configure the executor in Cognis

In `Settings -> Executors`, edit the target executor and set Signal support in the executor config.

Example config fragment:

```json
{
  "signal": {
    "direct_enabled": true,
    "command": "signal-cli"
  }
}
```

Notes:

- `direct_enabled` must be `true` or Cognis will reject direct-mode Signal accounts.
- `command` is optional. Use it when `signal-cli` is not on the default `PATH`.
- The command is executor-scoped and user-scoped through the executor config. It is not stored on individual channel accounts.

#### 3. Create the Cognis account

In `Channels -> Accounts -> New account`:

1. Choose `Signal`.
2. Select the target agent.
3. Set `Transport` to `direct_jsonrpc`.
4. Set `Adapter placement` to `Executor`.
5. Choose an explicit `executor_id`.
6. Fill in the Signal `Phone number` credential.
7. Save the account.

Cognis validates all of the following before saving:

- direct mode must use executor placement
- an explicit executor must be selected
- the executor must belong to the same user
- the executor config must enable direct Signal support

#### 4. Start and verify

1. Start the account from the `Channels` page.
2. Cognis asks the executor to spawn `signal-cli ... jsonRpc` for that account.
3. Verify the account reaches `connected` state.
4. Send a test Signal message and confirm the agent replies.

#### Default behavior in direct mode

Direct mode enables these features by default when supported by the installed `signal-cli` version:

- typing indicators
- read receipts
- agent profile sync
- inbound attachment download

If the local `signal-cli` does not support one of these operations, Cognis degrades that feature gracefully without breaking normal send/receive.

#### Operational notes for direct mode

- The adapter uses the normal reconnection loop with exponential backoff.
- If the managed `signal-cli` subprocess exits unexpectedly, the adapter will retry through that reconnect path.
- Direct mode does not currently provide UI-driven account linking or registration.
- Executor migration is manual because the Signal state is still local to the executor machine.

## Operational notes

- One bot or channel identity should map to one agent unless the platform explicitly supports a different pattern.
- Webhook-based platforms need a reachable public URL.
- Executor-hosted adapters depend on the selected executor being connected and healthy.
