# Systemd Service Templates

Systemd unit files for running Cognis as managed services on Linux.

## Files

| File | Type | Purpose |
|---|---|---|
| `cognis-controller.service` | System unit | Cognis controller (`cognis-controller serve`) |
| `cognis-executor@.service` | System template unit | Per-user executor (`cognis-executor`) |
| `cognis-executor.user.service` | User unit | Executor without root access |

## Controller (system-level)

Runs the Cognis controller as a dedicated `cognis` system user.

### Install

```bash
# Create system user
sudo useradd -r -m -s /bin/bash cognis

# Create env file
sudo mkdir -p /etc/cognis
sudo cp deploy/systemd/cognis-controller.service /etc/systemd/system/
sudo systemctl daemon-reload
```

### Environment file

Create `/etc/cognis/controller.env`:

```bash
COGNIS_DATA_DIR=/home/cognis/.cognis
COGNIS_HOST=0.0.0.0
COGNIS_PORT=8080
COGNIS_MNEMORY_URL=http://localhost:8050
COGNIS_INTARIS_URL=http://localhost:8060
COGNIS_LOG_LEVEL=info
COGNIS_LOG_FORMAT=json

# LLM provider keys
# OPENAI_API_KEY=sk-...
# ANTHROPIC_API_KEY=sk-ant-...
```

Restrict permissions since the file may contain API keys:

```bash
sudo chmod 600 /etc/cognis/controller.env
sudo chown cognis:cognis /etc/cognis/controller.env
```

### Start

```bash
sudo systemctl enable --now cognis-controller
```

## Executor (system-level template)

Each executor instance runs as a dedicated Unix user. The `%i` specifier
in the template unit is both the systemd instance name and the Unix
user/group.

### Install

```bash
# Create a dedicated user for the executor
sudo useradd -r -m -s /bin/bash alice

# Install the template
sudo cp deploy/systemd/cognis-executor@.service /etc/systemd/system/
sudo systemctl daemon-reload
```

### Environment file

Create `/etc/cognis/executor-alice.env`:

```bash
COGNIS_CONTROLLER_URL=wss://cognis.example.com/api/executor/ws
COGNIS_EXECUTOR_TOKEN=eyJ...
```

Generate the token in the Cognis UI under **Settings > Executors > Generate
token**, or via the API: `POST /api/v1/executors/{id}/token`.

Restrict permissions:

```bash
sudo chmod 600 /etc/cognis/executor-alice.env
sudo chown alice:alice /etc/cognis/executor-alice.env
```

### Start

```bash
sudo systemctl enable --now cognis-executor@alice
```

Multiple executors can run on the same host, each as a different user:

```bash
sudo systemctl enable --now cognis-executor@alice
sudo systemctl enable --now cognis-executor@bob
```

## Executor (user-level, no root)

Runs the executor under the current user's systemd instance. No root
access required.

### Install

```bash
mkdir -p ~/.config/systemd/user
cp deploy/systemd/cognis-executor.user.service \
   ~/.config/systemd/user/cognis-executor.service
systemctl --user daemon-reload
```

### Environment file

Create `~/.cognis/executor.env`:

```bash
COGNIS_CONTROLLER_URL=wss://cognis.example.com/api/executor/ws
COGNIS_EXECUTOR_TOKEN=eyJ...
```

Restrict permissions:

```bash
chmod 600 ~/.cognis/executor.env
```

### Start

```bash
systemctl --user enable --now cognis-executor
```

To keep the user service running after logout:

```bash
loginctl enable-linger $USER
```

## Running from a git checkout

The controller `ExecStart` uses `uvx cognis-controller` (PyPI install). To run
from a local git checkout instead, edit the service file and swap the
`ExecStart` line:

```ini
# Comment out the uvx line:
# ExecStart=/usr/bin/uvx cognis-controller serve

# Use uv run with a WorkingDirectory:
WorkingDirectory=/opt/cognis
ExecStart=/usr/bin/uv run cognis-controller serve
```

For executor units, use `uvx cognis-executor` for PyPI installs. From a local
git checkout, replace it with `uv run cognis-executor` and set
`WorkingDirectory`.

## Security notes

- Connection parameters (`COGNIS_CONTROLLER_URL`, `COGNIS_EXECUTOR_TOKEN`)
  are read from environment files, not CLI flags. This avoids exposing
  tokens in `/proc/<pid>/cmdline`.
- Environment files should be `chmod 600` and owned by the service user.
- The controller env file may contain LLM API keys — treat it as
  sensitive.
