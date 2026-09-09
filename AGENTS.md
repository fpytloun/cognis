# AGENTS.md — Cognis Repository Instructions

## Purpose

Cognis is a cloud-native agent operating system. The controller owns agent
identity, conversations, orchestration, workflows, memory context, guardrails,
routing, and the API. Executors run tools and optional inference near the
resources that they access.

The backend uses Python 3.12, FastAPI, Pydantic v2, SQLAlchemy 2.x, and
`asyncio`. The frontend is a separate SvelteKit application in `ui/`.

## Read Before Editing

1. Read this file.
2. Inspect the current implementation and tests for the area that will change.
3. Read the relevant specification from `docs/specs/README.md`.
4. Read user-facing guides when behavior or configuration changes.

Current code and user guides are product truth. Some specifications describe
future or partially shipped work. Do not implement an old plan because it
appears in a specification.

This file defines stable boundaries and repository rules. It is not an
exhaustive file inventory.

## Repository and Package Boundaries

| Path | Package or role | Responsibility |
|---|---|---|
| `cognis/` | `cognis-controller` | Controller, API, orchestration, persistence, providers |
| `packages/common/` | `cognis-common` | Pure contracts shared by controller and executor |
| `packages/executor/` | `cognis-executor` | Standalone executor, tools, browser, local runtime |
| `ui/` | SvelteKit application | Web and PWA user interface |
| `tests/` | Test suites | Unit, contract, integration, deployment, and E2E coverage |
| `docs/` | Documentation | User guides, architecture references, and implementation history |
| `deploy/`, `docker/` | Deployment assets | Kubernetes, systemd, Docker, and local-stack support |

Dependency direction is:

```text
controller ──> common <── executor
```

`common` must not import controller or executor implementation code. The
controller must not depend on executor implementation packages.

## Architecture and Ownership

These are target ownership boundaries. Some legacy hotspots contain mixed
responsibilities. Existing placement inside a hotspot is not a precedent for
new code.

| Area | Canonical owner |
|---|---|
| HTTP request and response adaptation | `cognis/api/routes/` |
| WebSocket protocol and connection adaptation | `cognis/api/websocket.py`, `cognis/api/chat_v2/` |
| Application construction and lifespan entry | `cognis/api/app.py` |
| Effective executor, tool, and skill runtime planning | `cognis/api/runtime_support.py` and focused core collaborators |
| Turn admission, serialization, cancellation, and recovery | `cognis/core/turn_scheduler.py` |
| LLM and tool turn coordination | `cognis/core/agent_loop.py` |
| Slash commands | `cognis/core/commands.py` |
| Workflow sequencing, gates, and step lifecycle | `cognis/core/workflow_engine.py` |
| Tool authorization, routing, and result trust boundary | `cognis/core/tool_router.py` |
| Context assembly and compaction | `cognis/core/context.py`, `cognis/core/compaction/` |
| Intaris-derived runtime cache | `cognis/core/session_cache.py` |
| Cognis metadata persistence | `cognis/store/` |
| External service integrations | `cognis/providers/` |
| External messaging adapters | `cognis/channels/` |

Transport modules adapt application operations. They must not own business
rules that another transport must reproduce.

## Non-Negotiable System Contracts

### Controller and executor

- The controller decides. Executors perform tool execution.
- Never execute executor tools directly in the controller, including local
  development. Route every tool call through an executor provider.
- An executor does not own conversations, memory, guardrail decisions, or
  workflow state.
- Channel adapters can run on the controller or an executor. Platform
  connection state belongs where the adapter runs. Reuse the same adapter
  implementation.
- Executor-routed inference remains transparent to controller policy. The
  controller resolves provider and model configuration before dispatch.
- Behavior needed by both controller and executor belongs in `cognis-common`
  only when it is portable and does not reverse package dependencies.

### Data ownership

| Data | Durable owner |
|---|---|
| Users, agents, projects, tasks, workflows, settings, and secrets | Cognis DB |
| Conversation and session metadata | Cognis DB |
| Session content, event sequence, compaction history, and tool-call audit | Intaris |
| Guardrail decisions and behavioral analysis | Intaris |
| Persistent memories and recall artifacts | Mnemory |
| Tool runtime state and browser profiles | Executor |

- Do not copy Intaris-derived state into Cognis DB.
- `SessionCache` can cache Intaris data in memory or Redis. It must remain
  reconstructable from the durable owner.
- Cognis DB stores metadata and references, not duplicate message or memory
  content.

### Security and access

- Intaris evaluation is fail-closed for non-bypassable tools.
- Secret resolution is fail-closed.
- Tool output is untrusted input. Apply the normal sanitization boundary to
  direct, recovered, and remotely reported results.
- The `admin` role does not grant access to another user's agents,
  conversations, tasks, schedules, memories, or secrets. Use ownership or an
  explicit grant.
- Email is the canonical Cognis user identifier and JWT subject. Service JWT
  audiences must remain scoped to their intended Cognis, Intaris, Mnemory, or
  executor recipient.
- Cognis, Intaris, and Mnemory use audience-scoped ES256 service JWTs. Do not
  add service API keys as a parallel authentication path.
- External channel senders must pass the configured verification policy.
- Never log message content, tool arguments or results, memory content, raw
  prompts or completions, or secret values.
- Logs and metrics can contain IDs, tool and model names, token counts,
  latencies, status codes, error categories, and decision outcomes.

### Execution and lifecycle

- All interactive and task execution uses the workflow execution model.
- A normal workflow step completes only through its explicit completion
  contract. LLM silence is not completion.
- Workflow deliverables are canonical step artifacts. Free-text step messages
  are progress unless the direct-step contract says otherwise.
- Task results, questions, and failures return through conversations. Tasks do
  not send directly to external channels.
- Compaction rotates to a new Intaris session within the same conversation.
  Preserve continuity, canonical history, and session ownership during
  compaction and recovery.
- Preserve cancellation, retry, idempotency, event-ordering, and ownership
  semantics across refactors.
- Keep database sessions around database work only. Release them before
  external I/O, event-store reads, or shared waits.
- Never use blocking I/O in the controller.

### Schema evolution

- Every schema change requires an Alembic migration and a matching idempotent
  bootstrap path.
- Test migrations and bootstrap behavior against SQLite and PostgreSQL.
- Do not add Intaris-owned event sequence, compaction, or intention fields to
  the Cognis session table.

## Architecture, Contract, and Invariant Hygiene

Treat the smallest correct change as the smallest change at the canonical
owner, not the change with the fewest edited lines.

- Before adding behavior, search for its existing owner and equivalent policy,
  schema, fallback, and implementation paths.
- `agent_loop.py` coordinates a turn. Put independently testable command,
  persistence, provider, and synchronization policy in an owned core module.
- `api/websocket.py` adapts the WebSocket protocol. Put operations shared with
  REST or channels in transport-neutral handlers.
- `api/app.py` is the composition root. Give new services explicit
  construction and lifecycle interfaces. Do not add private-field injection.
- Runtime execution and preview paths must consume the same effective tool,
  skill, executor, and access-policy plan.
- Do not copy implementations between controller and executor distributions.
- Give every state transition and side effect one owner. Avoid untyped state
  bags and hidden mutation across components.
- A compatibility path must identify its concrete consumer, supported
  contract, persisted format, or rollout need. It also needs coverage and a
  removal condition. Test-only aliases are not compatibility APIs.
- Keep mechanical relocation separate from behavior changes.
- Add invariant comments only for non-obvious ordering, ownership,
  transactional, or failure semantics.

For changes to `agent_loop.py`, `turn_scheduler.py`, `tool_router.py`,
`session_cache.py`, or persistence transactions, record the applicable:

- owned state and state lifetime;
- external side effects;
- lock or transaction boundary;
- cancellation owner;
- idempotency and retry contract;
- compatibility surface.

Keep this note proportional. Routine local changes do not need architecture
boilerplate.

### Controller tool contracts

- Controller-injected tool schemas have one source of truth in
  `cognis/tools/builtin/workflow.py`.
- Do not hand-write a second LLM-facing or validator-facing schema.
- Validate controller tool arguments through
  `cognis.core.tool_arguments.validate_tool_arguments` before mutation.
- On invalid arguments, return a synthetic error result that lets the model
  correct the call.

### API and lifecycle contracts

- API response models must represent every producer. Add contract coverage in
  `tests/unit/test_api_contracts.py`.
- Keep Pydantic and TypeScript API contracts synchronized.
- Put persistent-state lifecycle checkers and reconcilers in
  `cognis.core.invariants`.
- Do not clean up step sessions when a task pauses. Cleanup occurs only after a
  terminal task state.

## Python Conventions

- Use Python 3.12 syntax and `from __future__ import annotations`.
- Add type annotations to function signatures and return values.
- Add docstrings to public classes and public methods.
- Use `async def` for I/O-bound controller operations.
- Use `httpx.AsyncClient`, not `requests`.
- Use `asyncio` primitives, not threading primitives or polling loops.
- Use the `logging` module. Use `print()` only for intentional Typer CLI output.
- Keep code identifiers, comments, docstrings, and commit messages in English.
- Use comments sparingly. Explain constraints and intent, not syntax.

### Failure behavior

| Dependency or operation | Required behavior |
|---|---|
| Mnemory recall or remember | Degrade safely without inventing memory |
| Intaris evaluation | Fail closed |
| Intaris event recording | Preserve durability and retry semantics |
| LLM request | Use the configured retry and fallback policy |
| Executor delivery | Reconcile delivery state before a retry that can duplicate effects |
| Secret access | Fail closed |

Use shared provider retry and normalized error facilities. Do not add a broad
exception handler only to hide an unknown failure. Preserve intentional
failure isolation for metrics, cleanup, and optional caches.

## Configuration

- Infrastructure configuration uses environment variables.
- Application configuration uses the Cognis settings tables and API.
- LLM providers and model routing use their dedicated database models.
- Do not introduce a general application configuration file.
- `cognis/config.py` must remain safe to import without I/O or environment
  mutation.
- See `docs/guide/settings.md`, `docs/guide/deployment.md`, and
  `docs/specs/11-deployment.md` for the current configuration surface.

## Build and Validation

Use `uv` for Python dependency management and commands.

```bash
uv sync --all-packages --all-extras
uv run python -m cognis serve
```

Before each commit, run focused tests for the changed behavior and the required
repository checks:

```bash
uv run ruff check cognis/ packages/common/src/ packages/executor/src/ tests/
uv run ruff format --check cognis/ packages/common/src/ packages/executor/src/ tests/
MYPYPATH=packages/common/src:packages/executor/src uv run mypy --explicit-package-bases cognis/
uv run --package cognis-common mypy packages/common/src/cognis
MYPYPATH=.:packages/common/src:packages/executor/src uv run --package cognis-executor mypy --explicit-package-bases packages/executor/src/cognis
make test
cd ui && npm test -- --run && npm run check && npm run build:standalone
```

Do not weaken tests, add broad type suppressions, or reduce validation to make
a change pass.

### Additional validation by change type

| Change | Additional validation |
|---|---|
| Mnemory or Intaris client contract | `uv run pytest tests/contract/ -v` with the services running |
| Core orchestration, agent loop, context, or compaction | Relevant integration and E2E scenarios |
| Database schema, locking, leases, or CAS | SQLite and PostgreSQL migration or integration tests |
| Controller/common/executor package boundary | `COGNIS_RUN_ISOLATED_PACKAGE_TESTS=1 uv run pytest tests/unit/test_package_split.py -q` |
| Chat v2 backend frames or projection | Backend Chat v2 tests and canonical event replay |
| Frontend state or rendering | Unit checks, Svelte check, standalone build, and focused Playwright coverage |
| Release | Unit, contract, integration, E2E, browser, and package checks |

### Chat v2

The active chat protocol uses `chat_v2_frame`,
`conversation_runtime_snapshot`, and REST snapshot/sync/backfill under
`cognis/api/chat_v2/`. There is no legacy timeline transport.

For ordering, identity, reconnect, or projection changes:

```bash
uv run pytest tests/e2e/ -v
make e2e-events-replay
cd ui && npx playwright test e2e/
```

Canonical client invariants live in
`ui/src/lib/chat-v2/sync-engine.invariants.test.ts`. Backend runtime and
canonical IDs must remain byte-identical. See
`docs/specs/35-chat-v2-sync-architecture.md` and
`docs/specs/33-e2e-test-harness.md`.

## Database Changes

Cognis uses two schema evolution paths:

1. Idempotent bootstrap helpers in `cognis/bootstrap.py` run on startup.
2. Reversible Alembic migrations in `cognis/store/migrations/versions/`
   support formal upgrades and rollbacks.

For an existing-table schema change:

1. Add the Alembic migration.
2. Add and register the matching bootstrap helper.
3. Add a regression fixture with the previous schema.
4. Run bootstrap twice and verify columns, indexes, constraints, and backfills.
5. Test SQLite and PostgreSQL behavior.

Do not combine schema changes with query refactors.

## Common Extension Procedures

### Add a provider

1. Define or extend the provider protocol.
2. Implement the provider in its category package.
3. Register it through the provider registry.
4. Add configuration and health behavior.
5. For remote providers, use the shared retry and circuit-breaker facilities
   when the failure policy requires them.
6. Add unit and contract coverage.
7. Update the relevant specification.

### Add an API operation

1. Put transport-neutral behavior in its owning service.
2. Add a thin route or protocol adapter.
3. Add request and response models.
4. Apply authentication and ownership checks.
5. Add API contract and behavior tests.
6. Update the API specification and user guide when applicable.

### Add a tool

1. Define the tool in its owning controller or executor package.
2. Register it through the canonical registry.
3. Set read-only, bypass, timeout, and side-effect metadata correctly.
4. Preserve the untrusted-result boundary.
5. Add focused tests and update the tool-system specification.

## Specifications

Use `docs/specs/README.md` as the specification index. Important references
include:

- `01-architecture.md`
- `03-session-model.md`
- `04-controller-executor.md`
- `06-tool-system.md`
- `07-security-identity.md`
- `10-api-spec.md`
- `13-nfr-operations.md`
- `14-workflow-engine.md`
- `18-runtime-contract.md`
- `28-agent-sharing.md`
- `30-projects-and-revisions.md`
- `33-orchestration-routing.md`
- `34-deterministic-workflows.md`
- `35-chat-v2-sync-architecture.md`

Public diagrams must match the existing design language and retain editable
sources. Diagrams must clarify boundaries and ownership rather than decorate.

## Git and Delivery

- Follow the active coding skill for worktree, review, and commit discipline.
- Never push to `main` or `master` without explicit approval.
- Stage tracked changes with `git add -u`.
- Add new task-owned files by explicit path. Never use `git add -A`.
- Use Conventional Commits unless the user requests another repository
  convention.
- Commit only task-owned changes.
