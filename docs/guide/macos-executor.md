# macOS executor distribution

The macOS executor uses a Homebrew formula and a pre-resolved payload. The
payload is architecture-specific (`arm64` or `x86_64`) and is built from the
repository's frozen `uv.lock`. Service startup does not download Python
dependencies. The executor uses the system Google Chrome; Cognis does not
bundle Chromium.

## Public Homebrew installation

Homebrew is the recommended installation method for macOS. The public tap is
available at [fpytloun/homebrew-tap](https://github.com/fpytloun/homebrew-tap).
The `cognis-executor` formula will become installable after the first release
publishes its immutable macOS assets and formula:

```bash
brew tap fpytloun/tap
brew install cognis-executor
cognis-executor configure
brew services start cognis-executor
```

Before that first formula release, use the private Gitea development path or
the local immutable-asset flow below.

## Configure and operate

Run the interactive configuration once:

```bash
cognis-executor configure
cognis-executor doctor --test-connection
cognis-executor status
cognis-executor logs --lines 100
brew services list
```

Configuration, workspace data, browser profiles, and logs remain outside the
Homebrew prefix. The service is a per-user launchd service. Use `brew services`
without `sudo`:

```bash
brew services start cognis-executor
brew services stop cognis-executor
brew services restart cognis-executor
cognis-executor logs --follow
```

`cognis-executor status` reports configuration and service state. `doctor`
checks the workspace, `uvx`, `npx`, system Chrome, and optionally the
authenticated controller probe.

`cognis-executor logs` shows the service stderr and stdout logs with labels.
Python logging uses stderr, so `cognis-executor.error.log` is usually the
primary application log. Use `-n N` as the short form of `--lines N`. Use `-f`
as the short form of `--follow`. The command derives both paths from the active
Homebrew prefix, including `/opt/homebrew` and `/usr/local`.

## Upgrade and uninstall

Upgrade after a new immutable release is available:

```bash
brew update
brew upgrade cognis-executor
brew services restart cognis-executor
```

To uninstall the package and keep executor configuration:

```bash
brew services stop cognis-executor
brew uninstall cognis-executor
```

Do not remove the Cognis executor configuration or data directories unless you
also intend to remove enrollment, workspaces, browser profiles, and logs.

## Private Gitea development and HEAD path

For development, clone the private authoritative repository without putting
credentials in commands or files:

```bash
git clone ssh://git.fpy.cz:2222/filip/cognis.git
cd cognis
uv sync --all-extras
uv run cognis-executor doctor
```

The generated formula can use `--head-url` for a private Gitea HEAD formula.
Public formulas must omit `--head-url`. A HEAD install may download Python
dependencies during formula installation; stable asset installs do not.

To test local immutable assets without a release, cross-resolution can build
both assets from one checkout. Runtime validation still needs matching
hardware for each architecture. Generate a development formula with explicit
`file://` URLs:

```bash
python3 packaging/homebrew/build_executor_asset.py \
  --architecture arm64 --output "$PWD/cognis-executor-0.14.1-macos-arm64.tar.gz"
python3 packaging/homebrew/build_executor_asset.py \
  --architecture x86_64 --output "$PWD/cognis-executor-0.14.1-macos-x86_64.tar.gz"
arm_sha="$(shasum -a 256 cognis-executor-0.14.1-macos-arm64.tar.gz | awk '{print $1}')"
intel_sha="$(shasum -a 256 cognis-executor-0.14.1-macos-x86_64.tar.gz | awk '{print $1}')"
python3 packaging/homebrew/generate_formula.py --development \
  --version 0.14.1 \
  --arm64-url "file://$PWD/cognis-executor-0.14.1-macos-arm64.tar.gz" \
  --arm64-sha256 "$arm_sha" \
  --x86-64-url "file://$PWD/cognis-executor-0.14.1-macos-x86_64.tar.gz" \
  --x86-64-sha256 "$intel_sha" \
  --output CognisExecutor.rb
brew install --build-from-source ./CognisExecutor.rb
```

For a private Gitea HEAD formula, use the same real local asset metadata and
add the private repository URL:

```bash
python3 packaging/homebrew/generate_formula.py --development \
  --head-url ssh://git.fpy.cz:2222/filip/cognis.git \
  --version 0.14.1 \
  --arm64-asset "$PWD/cognis-executor-0.14.1-macos-arm64.tar.gz" \
  --arm64-sha256 "$arm_sha" \
  --x86-64-asset "$PWD/cognis-executor-0.14.1-macos-x86_64.tar.gz" \
  --x86-64-sha256 "$intel_sha" \
  --output CognisExecutor.rb
brew install --build-from-source --HEAD ./CognisExecutor.rb
```

## Troubleshooting

- Run `cognis-executor doctor --test-connection` and check the controller URL,
  token, TLS certificate, and network access.
- Run `cognis-executor logs -n 200` and check `brew services list`.
- Confirm that `python@3.12`, `uv`, `node`, and the formula's native libraries
  are installed.
- Browser tools use system Google Chrome when configured with the `chrome`
  channel. Install Chrome separately from Google if it is not present.
- Stop the service before replacing a local formula or troubleshooting a
  corrupted payload.

## Final tap publication steps

After release assets and their SHA-256 files are verified:

1. Generate a public formula with immutable versioned GitHub release URLs and
   real SHA-256 values.
2. Review the formula for tokens, environment-token arguments, plist secrets,
   local source paths, and non-versioned URLs.
3. Commit the formula to the public tap repository.
4. Run `brew audit --new` and install/test it on both Apple Silicon and
   Intel macOS.
5. Commit and push `Formula/cognis-executor.rb` to
   `fpytloun/homebrew-tap`.
6. Confirm that `brew update && brew upgrade cognis-executor` installs the new
   version.
