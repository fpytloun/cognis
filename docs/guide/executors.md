# Executors

Executors are the part of Cognis that perform tool execution. The controller decides what should happen; executors are the hands that actually run tools.

![Controller and executor split](../assets/images/cognis-controller-executor-split.svg)

## Why executors exist

This separation lets Cognis:

- keep orchestration logic in one place
- isolate tool execution
- connect to remote machines when tools need local access
- optionally route model inference through a matching executor

## Executor modes

Depending on configuration, Cognis can use:

- in-process executors for simple local setups
- subprocess executors for local isolation
- remote WebSocket executors for user-local or multi-machine deployments

For multi-user production setups, remote executors are the preferred model.

## What executors control

Executors define the effective tool pool available to agents. Agents inherit tools from their executor and can then further limit categories, individual tools, and permission policy.

Executors can also be used to:

- attach MCP servers
- expose only selected tool groups
- host Playwright-backed browser automation
- host channel adapters that need local services
- route provider calls to executor-side inference when supported

## Where to manage executors

Open `Settings` and use the executors section to inspect:

- configured executors
- status and health
- desired vs applied config generation
- enabled tool groups
- individually enabled tools
- attached MCP servers

If browser automation is enabled on an executor, the same settings area also
lets you configure browser runtime behavior such as auto-install, engine,
headed-mode allowance, session limits, and idle timeout.

### Runtime states

WebSocket executors can report several runtime states:

- `active` - the current desired generation is fully applied
- `degraded` - the current desired generation is applied, but one or more assigned MCP servers failed and were omitted from the active tool set
- `reconfiguring` - Cognis is pushing a newer desired generation to the connected executor
- `stale` - a newer desired generation is stored in Cognis, but the executor has not applied it yet
- `blocked` - the executor is connected, but the last configure attempt failed in a non-recoverable way
- `offline` - no executor WebSocket connection is currently active

`desired` and `applied` are config generations, not simple reconnect counters. If they differ, the executor is behind the latest saved configuration.

## Choosing the right mode

- Use local executor modes for development and simple single-user setups.
- Use remote WebSocket executors when tools, channels, or model endpoints must stay on another machine.
- Disable local executor modes in shared production environments if the controller should not execute tools locally.

## Common troubleshooting checks

- verify the executor is connected
- if `desired != applied`, wait for reconfigure or reconnect the executor
- confirm the required tools are enabled on the executor
- confirm the agent is bound to the expected executor or label selector
- confirm any required secrets or MCP servers are assigned correctly
- confirm browser automation is enabled if browser tools are expected

## Browser automation

Browser automation is executor-native. The controller handles orchestration,
credentials, approvals, and retry policy, but the executor is the only
component that launches Playwright and performs page actions.

In `Settings -> Executors`, the browser section stores executor config like:

```json
{
  "browser": {
    "enabled": true,
    "auto_install": true,
    "headed_allowed": false,
    "engine": "chromium",
    "max_sessions": 4,
    "idle_timeout_seconds": 600
  }
}
```

Saved browser auth state is stored in Cognis as an encrypted credential record,
not as an unmanaged browser profile on the executor. This keeps the controller
as the durable source of truth while still allowing temporary executors to
rehydrate browser sessions when a site permits it.

## MCP stdio command format

For executor-hosted stdio MCP servers, `Command` is the executable only. Cognis does not run a shell.

Use:

- `Command`: `npx`
- `Arguments`:

```text
-y
@doist/todoist-ai
```

Do not paste the full shell command into `Command`. Values like `npx -y @doist/todoist-ai` are treated as a single executable path and will fail.

If an assigned MCP server fails or times out during `spawn`, `initialize`, or `tools/list`, Cognis keeps the executor connected and marks it as `degraded`. The failing MCP server is omitted from the active observed tool set until the configuration is fixed and reapplied.

## Running as a systemd service

Cognis ships with systemd unit templates in [`deploy/systemd/`](../../deploy/systemd/) for running the controller and executors as managed services on Linux.

### Environment variables

The executor CLI reads connection parameters from environment variables so that tokens never appear in `/proc/<pid>/cmdline`:

| Variable | Description |
|---|---|
| `COGNIS_CONTROLLER_URL` | WebSocket URL of the controller |
| `COGNIS_EXECUTOR_TOKEN` | JWT authentication token |

CLI flags (`--controller-url`, `--token`) still work and take precedence over environment variables.

When installed from PyPI, the recommended command is `uvx cognis-executor`.

### System-level executor (template unit)

The template unit `cognis-executor@.service` runs one executor per dedicated Unix user. The instance name (`%i`) is the user and group.

```bash
# Install the template
sudo cp deploy/systemd/cognis-executor@.service /etc/systemd/system/
sudo systemctl daemon-reload

# Create env file for user "alice"
sudo tee /etc/cognis/executor-alice.env <<'EOF'
COGNIS_CONTROLLER_URL=wss://cognis.example.com/api/executor/ws
COGNIS_EXECUTOR_TOKEN=eyJ...
EOF
sudo chmod 600 /etc/cognis/executor-alice.env
sudo chown alice:alice /etc/cognis/executor-alice.env

# Start
sudo systemctl enable --now cognis-executor@alice
```

Generate the token in **Settings > Executors > Generate token**.

### User-level executor (no root)

The user unit `cognis-executor.user.service` runs under your own systemd instance. No root access required.

```bash
mkdir -p ~/.config/systemd/user
cp deploy/systemd/cognis-executor.user.service \
   ~/.config/systemd/user/cognis-executor.service
systemctl --user daemon-reload

# Create env file
cat > ~/.cognis/executor.env <<'EOF'
COGNIS_CONTROLLER_URL=wss://cognis.example.com/api/executor/ws
COGNIS_EXECUTOR_TOKEN=eyJ...
EOF
chmod 600 ~/.cognis/executor.env

# Start
systemctl --user enable --now cognis-executor

# Keep running after logout
loginctl enable-linger $USER
```

### Controller

A system-level controller unit (`cognis-controller.service`) is also provided. See [`deploy/systemd/README.md`](../../deploy/systemd/README.md) for full setup instructions.

### Running from a git checkout

The default `ExecStart` uses `uvx cognis-executor` (PyPI). To run from a local git checkout, swap to `uv run cognis-executor` with a `WorkingDirectory`. Both variants are documented in the unit files as comments.

## Signal on executors

Signal direct mode is an example of why executor-hosted adapters exist.

When a Signal account uses `transport=direct_jsonrpc`, Cognis does not talk to an external REST bridge. Instead, the selected executor starts `signal-cli` directly as a subprocess.

### Minimum executor config for direct Signal

In `Settings -> Executors`, open the executor card and expand `Signal Direct Mode`. Cognis exposes a toggle for direct mode and a field for the `signal-cli` command/path. Under the hood it stores the following executor config:

```json
{
  "signal": {
    "direct_enabled": true,
    "command": "signal-cli"
  }
}
```

Use an absolute path for `command` when `signal-cli` is not on the default `PATH`, for example:

```json
{
  "signal": {
    "direct_enabled": true,
    "command": "/opt/homebrew/bin/signal-cli"
  }
}
```

### Signal executor checklist

Before assigning a Signal direct-mode account to an executor:

1. `signal-cli` is installed on the executor machine.
2. The Signal account is already linked or registered on that machine.
3. The executor is connected in Cognis.
4. The executor config has `signal.direct_enabled=true`.
5. If needed, the executor config points to the correct `signal-cli` binary path.

### Important behavior

- The `signal-cli` command is executor-scoped and therefore user-scoped through executor ownership.
- Channel accounts do not store the executable path.
- Signal account state remains local to the executor machine.
- Moving a direct-mode Signal account to another executor requires migrating the local `signal-cli` state first.
