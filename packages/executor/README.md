# cognis-executor

Standalone remote executor for Cognis.

The package provides the `cognis-executor` command. It connects an executor
process to a Cognis controller over WebSocket and runs the tools assigned to
that executor.

`cognis-controller` does not include this package. Install it on the machine
that owns local tools, browser profiles, credentials, or workspaces. A
controller-only installation and the official controller image are
remote-WebSocket-only.

## Install

Install the `full` extra for normal use:

```bash
pip install "cognis-executor[full]"
uvx --from 'cognis-executor[full]' cognis-executor
```

Configure a persistent native user configuration, inspect it, or run
read-only diagnostics:

```bash
cognis-executor configure
cognis-executor config show
cognis-executor doctor --test-connection
cognis-executor status
cognis-executor logs --lines 100
cognis-executor logs --follow
```

`configure --non-interactive` never prompts and accepts `--controller-url`,
`--token`, and `--workspace` (or the corresponding `COGNIS_*` environment
variables). Runtime precedence is explicit flags, environment, config file,
then defaults. The legacy no-subcommand invocation remains supported,
including a token supplied on standard input for headless subprocesses.
Lifecycle commands delegate to Homebrew/launchd or an existing Linux
user-systemd unit. Their service definition must invoke the bare
`cognis-executor` foreground command, not another lifecycle command.
`logs` reads both Homebrew service log files on macOS and the user journal on
Linux. Use `-n` for `--lines` and `-f` for `--follow`.
See the [macOS executor distribution guide](../../docs/guide/macos-executor.md)
for the Homebrew formula, per-user launchd service, and private development
installation path.

The bare package is a minimal executor. It includes core filesystem, shell,
search, project-context, WebSocket, MCP transport, OfficeCLI integration, and
LSP integration. Shared channel adapters and their dependencies are part of
`cognis-common`. The bare package does not install optional browser, web,
document, or inference dependencies.

Install a component extra only when the host needs a narrower capability set:

```bash
pip install "cognis-executor[browser]"
pip install "cognis-executor[web]"
pip install "cognis-executor[documents]"
pip install "cognis-executor[inference]"
```

The `mcp` and `channels` extras remain as compatibility aliases. Their shared
runtimes are part of `cognis-common` and are available in the bare package.

Missing optional components do not prevent executor startup. The executor omits
their tools and reports the runtime capability as installable or unavailable.

## Build

Build it from the repository root:

```bash
uv build --package cognis-executor
```
