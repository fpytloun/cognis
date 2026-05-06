# Stage 37: Separate Executor Package and Portable Lite Binary

## Status

PLANNED

## Goal

Split the executor from the controller into a proper smaller install surface and
add a tiny portable executor binary as an additional installation option. The
binary is intended for simple remote hands: a user downloads it, runs it with a
controller URL and token, and gives an agent access to a machine without
installing the full controller stack.

## Non-Goals

- No rewrite of the executor protocol in Go or Rust in this stage.
- No full browser/LSP/toolchain bundle in the tiny binary.
- No change to the existing Docker full executor path.
- No attempt to provide strong anti-reverse-engineering guarantees.

## Target Install Modes

### Controller

`cognis-controller` remains the full controller package with API, DB, UI,
workflow engine, providers, and admin CLI.

### Standard Executor Package

`cognis-executor` becomes a separate Python package or distribution target that
can still be installed normally:

```bash
uvx cognis-executor --controller-url wss://cognis.example.com/api/executor/ws --token <token>
```

Optional extras provide larger capabilities:

- `cognis-executor[browser]`
- `cognis-executor[lsp]`
- `cognis-executor[llm]`
- `cognis-executor[channels]`
- `cognis-executor[document]`
- `cognis-executor[full]`

### Full Executor Container

The existing Docker image remains the preferred full-runtime executor with
browser dependencies, Node/npm, language servers, `ripgrep`, shells, and other
toolchain dependencies preinstalled.

### Portable Lite Binary

The lite binary is an additional option:

```bash
./cognis-executor --controller-url wss://cognis.example.com/api/executor/ws --token <token>
```

It should include only the minimal executor runtime:

- WebSocket JSON-RPC executor protocol
- filesystem tools
- shell tools
- glob/grep with Python fallback and optional use of host `rg`/`fd`
- basic artifact materialization/publishing only if it does not pull controller
  storage dependencies into the binary
- basic HTTP client support required by the executor protocol and simple tools

It should not include by default:

- Playwright or Patchright
- browser binaries
- LSP manager or LSP auto-install
- LiteLLM executor-side inference
- WeasyPrint/PDF generation
- rich web extraction stack
- channel adapters
- controller API, DB, migrations, UI, Mnemory, or Intaris providers

## Package Boundary

Introduce three logical surfaces:

### `cognis-protocol`

Small shared package with stable models and protocol helpers:

- executor JSON-RPC payload models
- `ExecutorConfig`, `ExecutorHandle`, `ExecutorCapabilities`
- `ToolCall`, `ToolResult`, `ToolDefinition`, `ToolSource`
- minimal runtime/environment metadata models
- redaction-safe logging helpers if needed

It must not depend on FastAPI, SQLAlchemy, Alembic, LiteLLM, Playwright,
WeasyPrint, or controller providers.

### `cognis-executor`

Executor runtime package:

- `cognis.executor.__main__`
- `ExecutorRunner`
- executor-native tool handlers
- MCP client where enabled by extras
- optional browser/LSP/LLM/channel/document feature groups

### `cognis-controller`

Controller package:

- API, store, workflow engine, memory/guardrails integrations, UI, admin CLI
- depends on `cognis-protocol`
- may depend on `cognis-executor` for in-process and subprocess executor modes,
  but installing only `cognis-executor` must not pull the controller stack.

## Build Strategy

Use Nuitka for the first compiled binary target because it compiles Python into
a native executable and benefits from the executor package cleanup. Build
profiles:

- `lite`: compiled portable binary with minimal feature set
- `standard`: Python executor wheel with default optional dependencies kept
  modest
- `full`: Docker executor image with browsers, LSPs, and toolchains

The binary build should be per OS/architecture: Linux x64/arm64, macOS
x64/arm64, and Windows x64 when supported.

## Workstreams

### 37.1 Dependency Inventory

- Generate a dependency map for executor imports.
- Identify controller-only imports currently pulled by executor code.
- Define exact base and optional extras dependency sets.

### 37.2 Protocol Extraction

- Move shared models and protocol helpers to the small protocol surface.
- Keep import compatibility wrappers if needed during migration.
- Ensure protocol package has no controller dependencies.

### 37.3 Executor Package Split

- Split packaging metadata so executor can be installed without controller UI,
  DB, API, providers, migrations, and admin commands.
- Keep existing `cognis-executor` CLI behavior.
- Keep subprocess executor compatibility for the controller.

### 37.4 Lazy Capability Registries

- Replace eager executor tool imports with capability-group registries.
- Import browser, LSP, LiteLLM, channels, document generation, and rich web
  extraction only when configured and installed.
- Make missing optional extras produce clear configure/runtime warnings instead
  of import-time crashes.

### 37.5 Lite Feature Profile

- Add a lite executor feature profile used by normal package tests and binary
  builds.
- Ensure lite profile can import and start without optional dependencies.
- Ensure unsupported requested tools are reported as unavailable during
  configure/tool listing.

### 37.6 Nuitka Binary Build

- Add a repeatable build script for lite binaries.
- Configure hidden imports only for lite-supported modules.
- Produce checksums and version metadata.
- Validate startup, help output, connection, configure, and basic tool execution.

### 37.7 CI and Release

- Add CI matrix for supported OS/arch targets.
- Run binary smoke tests against a local controller/executor WebSocket endpoint.
- Publish binaries as release artifacts.
- Document install commands and security expectations.

### 37.8 Documentation

- Document three executor install paths: portable lite binary, Python package,
  and full Docker executor.
- Explain which tools are included in the lite binary.
- Explain that larger features can still be installed through the standard
  executor package or Docker image.

## Acceptance Criteria

- `cognis-executor` can be installed without installing the controller stack.
- Existing Python executor invocation still works.
- Full Docker executor still works.
- Lite binary starts on a clean host, connects to a controller, configures, and
  executes shell/filesystem/search tools.
- Lite binary does not bundle browser, LSP, LiteLLM, channel, PDF, API, DB, UI,
  or migration machinery.
- Controller clearly reports unsupported tools/capabilities for lite executors.
