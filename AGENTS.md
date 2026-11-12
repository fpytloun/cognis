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
   For UI work, also read `ui/DESIGN.md` explicitly; it is not auto-loaded.
2. Inspect the current implementation and tests for the area that will change.
3. Use `docs/specs/README.md` to locate a specification when the change affects
   an architecture or contract documented there.
4. Read the affected user-facing guide when behavior, configuration, or
   contributor workflow changes.

Current code and user guides are product truth. Some specifications describe
future or partially shipped work. Do not implement an old plan because it
appears in a specification.

This file defines stable boundaries and repository rules. It is not an
exhaustive file inventory. Load documentation that informs the current
decision; do not read unrelated specifications or guides as a routine
prerequisite.

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

Task-owned delegation identities live in the existing attempt-fenced
`execution_recovery` metadata. Reserve child/session and turn IDs before external
provisioning; assert ownership inside reservation/finalization transactions,
locking the task before the child. Release DB transactions before external I/O.
The workflow/queue owns suspension and bounded deferral; only explicit task
cancellation or resolved human cancellation/denial may cancel its child. Reuse
canonical tool settlement and executor receipts, never blind-replay an uncertain
mutation. Automatic child continuations persist their active turn and typed
retry bookkeeping before dispatch; transcript and todo content stay in Intaris.
Child configuration/tool scope changes and legacy calls without reserved identity
require reconciliation.
These metadata-only compatibility checks add no content store or schema.

Delegation tool-call events retain exact recovery arguments in Intaris separately
from bounded model/UI previews. Legacy previews are usable only when removing
presentation fields reproduces the reserved argument binding exactly; truncated
or changed inputs remain blocked. This reuses the existing attempt fence,
settlement, transaction, and cancellation owners without new side effects.
Delegated policies inherit activated parent allowances in Intaris rather than
redeclaring ambient task grants; child deny clauses remain effective.

Unrecoverable executor mutations in agent run steps settle as canonical uncertain
results so the agent can inspect effects before continuing. Deterministic steps
still require explicit reconciliation. The turn-owned duplicate ledger retains
uncertainty, never success; Intaris remains its restart source. Result persistence
precedes task descriptor retirement and receipt acknowledgment. Existing task
fences, transactions, append idempotency and scheduler cancellation still own
recovery; no new lock, replay permission or persisted format is introduced.

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

### Session preamble lifetime

Executor eligibility is a read-only, turn-owned observation refreshed before
context preparation and executor tool/switch admission. Refresh revalidates the
effective shared-agent scope and current policy without expanding assignments,
rebuilding inventory, or changing pins. Database sessions end before dispatch;
failed observations are unknown and fail closed. Cancellation remains owned by
the turn scheduler, and refresh never retries a delivered tool. Changed executor
guidance uses the existing append-only context audit/delivery boundary.

`ContextAssembler` owns the rendered pre-history preamble and append-only
superseding context updates. Intaris `lifecycle/context_preamble` events are
the durable representation; `SessionCache` snapshots reconstruct them from
events, including after Redis loss. They are scoped to the Intaris session;
rotation starts a fresh freeze. Existing sessions without these events freeze
on their first assembly after upgrade (one cache miss is expected).

Turn admission serializes writers. Assembly persists before exposing a new
preamble/update, without a DB transaction over external I/O. Cancellation
remains owned by the turn scheduler; interrupted persistence invalidates the
canonical cache so retry reconciles with Intaris. Append idempotency includes
the captured event head and content digest. Read-only candidates never append.
Updates carry an explicit admitted-user sequence anchor when they must replay
before an already-recorded user message. Mid-turn project exposures are recorded
after tool batches, separately from project discovery events. Policy evidence
is historical context, not authorization, and refresh appends rather than edits.
No Cognis DB schema or provider API changes are involved.

Full-transcript `prompt_injections` snapshots are retired: do not write, decode,
or replay them. Existing events remain stored in Intaris but have no effect on
canonical history. The outgoing boundary preserves accepted tool bytes only in
the live turn; canonical replay uses the original per-message events.
`context_projection.py` retains the 75% trigger and 35% target, with critical/hard
budget handling and 85% session compaction as independent safety gates.
Cross-turn byte identity of transient notices and projected tool results is
maintained through canonical per-message Intaris events plus small
`context_projection` lifecycle events. These events contain only call/event
identifiers, projection operations, a rendering-policy version, and bounded
numeric parameters; they never store prompts, copied histories, or rendered
tool output. `SessionCache` reconstructs decisions from Intaris after cold-cache
recovery, and replay reapplies them to canonical events. Projection events use
the existing Intaris append, idempotency, retry, and turn-scheduler cancellation
behavior. No Cognis DB content storage, new lock, or new retry framework is
involved.

Live inference commits new notice events and projection deltas before exposing
the prompt. Unchanged cycles append no decision event. Writes are bounded by
encoded UTF-8 bytes and use deterministic idempotency keys; a complete parallel
tool-group omission is one operation. After interrupted writes the next assembly
reconciles the accepted events. Output-limit responses persist one canonical
assistant segment per model response rather than repeatedly copying accumulated
text. Chat v2 coalesces those segments and hydrates missing segment predecessors
when projecting a partial snapshot/sync/backfill window. Existing assistant events
remain authoritative full replacements unless explicitly marked as segments.
Executor assignment notices use that same canonical message boundary. Assembly
keeps the latest identical notice in its historical position, appends changed
assignments, and appends explicit revocation when the assigned pool disappears.
Existing audit-only records without replay metadata are not reinterpreted.

Routing notices also use canonical per-message events with explicit request-only
scope. Their content and turn identity prevent duplicate exposure on retry or
profile-switch reassembly; later requests retain them as historical context.
`switch_executor` schema exposure depends on assigned pool size, not transient
usability, and uses the canonical definition without a live executor-ID enum.
Runtime assignment/usability validation remains owned by executor switching.
These changes reuse serialized turn admission and the existing delivery commit;
they add no transaction, lock, external side effect, or cancellation owner.

Cached projection results retain their compact decision delta alongside their
rendering. Reusing a result after failed persistence must carry the same delta
through the existing delivery commit; durability filtering prevents repeated
writes after success. The metadata lives with the turn-owned projection result
and follows the existing offload/cancellation boundary; thresholds are unchanged.
Executor per-call schema overlays use observed, enabled tools on assigned targets,
including temporarily unavailable targets. Actual routing retains its usable-target
requirement. Native Responses message items are persisted per assistant event under
the existing native-envelope byte cap so cold/profile replay preserves their wire
shape. Oversized text envelopes must not evict otherwise valid reasoning/call items.
No session snapshots, content-table storage, or new external side effects are added.

Whole-group projection state records only fully compacted parallel batches;
partial pruning retains per-result markers and committed sibling preservation.
The below-threshold loop path can reuse that accepted mixed rendering after
pressure recovery, retaining pending decisions and its prefix-mutation guard.
Synthetic interruption evidence is protected when reconstructed. Existing
rendering-policy decisions remain authoritative, including for older markers.
This uses turn-owned projection/cache state and the existing offload, cancellation,
and delivery-commit boundaries, with no new writes, locks, or persisted format.

Tool-call event builders share sanitized native replay metadata for regular and
controller calls. Preserve original argument bytes only when sanitization leaves
their values unchanged; signed native envelopes and the existing byte caps remain
authoritative. These are per-event fields, not snapshots. Projection diagnostics
are content-free logs and add no durable content or inference. Bundled hook
startup tool hints use persisted child hook identity and the existing filtered
inventory; they add no permissions. Existing hook fences, turn cancellation,
event append idempotency and retry ownership are unchanged.

Canonical replay regroups tool calls owned by one native provider response into
that response's assistant message when only the batch's own results separate
them, matching the live request shape. A numeric marker on newly recorded native
assistant or tool-call events opts into regrouping; unmarked batches retain their old grouping
and omission scope without migration. Projection replaces a textual tool result
only when its placeholder at most halves it; persisted decisions stay
authoritative. Both are pure replay/projection rules with no new events, writes,
locks or new event types. Controller discovery and rejected-tool repair append
results and bridge guidance without promoting direct schemas; call_tool retains
the existing permission, validation and execution boundary.

Skill activation grants session-scoped eligibility, not direct schema exposure.
Skill-load-capable sessions select the stable controller discovery kernel before
their first inference, even on Anthropic; inventory names are not embedded in
schema descriptions. The shared exposure boundary applies this policy to fallback
candidates too. Loaded-skill instructions and resolved eligibility use the
existing canonical developer-message event and loaded-skill cache replay path.
Auxiliary tool notices record their originating call; attachment context renders
from sanitized canonical references through one shared formatter. New markers
are forward-only. UI-only skill notices are excluded from model replay. These
changes use the existing turn serialization, incremental event append, strict
delivery, cancellation and idempotency boundaries, with no new locks, schema,
snapshots or content stores.
New loaded-skill events preserve earlier instruction notices while updating latest
skill state, including slash-command reloads. Legacy events retain their original
supersession behavior. Live project-exposure notices carry their canonical audit
source and role so provider wrappers do not change on cold replay.
Skill tool results deduplicate unchanged instruction/template blocks against the
turn's actual trusted messages, not historical loaded-skill snapshots. Asset
preparation still finishes before result settlement; only duplicate instruction
injection is skipped. Changed blocks append full replacement guidance. This is
a pure result transformation using existing turn serialization, event persistence,
replay, cancellation and retry owners; it adds no state, locks or durable format.
Settlement transforms the router's complete raw skill payload, then reapplies
the same sanitization and output budget before recording or presenting it.
Never parse the wrapped or truncated presentation as skill JSON.

Attachment compaction policy 2 recovers canonical artifact references through
`artifact_read`; SessionCache accepts it only for that operation and retains
policy-1 replay. Fresh image protection is derived from canonical message order
through the next assistant response, not a durable delivery claim. Text pruning,
hard budgets, access checks, turn cancellation and projection commit ownership
are unchanged; no new side effect, lock, transaction or retry is introduced.

Within-turn soft-target projection can reuse the accepted rendering plus new
messages after exact counting passes the existing loop-pressure, hard, and
available-budget gates. A complete source-prefix hash (excluding mutable token
estimates) is turn-owned `ProjectionTurnState` metadata, copied through the
existing offload fork and published only after cancellation/fence checks.
Unknown or changed source hashes cannot authorize this optimization. Existing
cheap cached retries remain compatible; they do not establish a source hash
when source identity is unknown. Projection decisions still commit through the
existing delivery boundary. No new side effects, locks, retries, event formats,
durable snapshots, or cross-turn threshold changes are introduced.

Rotation copies content before rebasing event-scoped projection decisions to
Intaris-assigned destination sequences. Projection-only batches use deterministic
rotation-owned append keys; cancellation remains scheduler-owned and retries
reuse confirmed append identities. Call-scoped decisions retain stable IDs.
SessionCache resolves legacy copied projection references in their source
namespace, drops absent targets, and reloads pre-fix event-scoped L2 state once.
This compatibility path is required for existing Intaris histories and can be
removed only when such histories are no longer supported. No DB schema, lock,
transaction, content store, or provider API changes are involved. Recap cards
remain in their original sessions; old compaction-tail recap copies are hidden
at the shared Chat v2 normalization boundary.

### Controller tool contracts

Skill resolution batches version and asset reads only after owner-scoped
visibility resolution; ordering, missing-version and manifest fallbacks remain
unchanged. Executor pool reuse is limited to one runtime build, including
selection and inventory assembly; changed pin winners absent from that pool
require fresh resolution. Eligibility refresh remains the authorization owner
before context/tool admission. Forwarded catalog discovery uses the physical
connection's existing cache and invalidation rules after bridge ownership
checks. Its deadline includes lock waiting, and cached results never claim
physical delivery. No new cross-turn cache, transaction, retry, or durable state
is introduced.

Chat preparation reuses identical acknowledged Intaris configuration writes only
within a scheduler-owned turn. Every refresh still resolves current policy inputs;
changed inputs and failed writes cannot hit the cache. Cancellation/return closes
the cache, including inherited child contexts. This adds no durable policy state
or tool-authorization cache.

WebSocket tool catalogs belong to one physical executor connection. Reuse requires
the executor's explicit catalog-notification contract; configuration changes and
MCP recovery invalidate it, and reconnect/ownership replacement starts cold.
Legacy executors keep per-call discovery. Registry permission filtering remains
fresh. Context identity and Intaris status reads may overlap, but prefix persistence
and event-cache refresh remain serialized; gather/cancellation owns all read tasks.
Existing canonical write ordering, idempotency, fences, and retry ownership remain
unchanged. MCP OAuth I/O runs after metadata sessions close.

- Controller-injected tool schemas have one source of truth in
  `cognis/tools/builtin/workflow.py`.
- Do not hand-write a second LLM-facing or validator-facing schema.
- Validate controller tool arguments through
  `cognis.core.tool_arguments.validate_tool_arguments` before mutation.
- On invalid arguments, return a synthetic error result that lets the model
  correct the call.

### API and lifecycle contracts

Task lifecycle tools accept the owner's non-delegated primary chat as well as
the task's bound control chat. `core/task_control.py` revalidates current metadata
authority per call; a UI context marker or a prior inspection is not a grant.
DB reads finish before queue dispatch. The existing queue mutation transactions,
attempt CAS, execution leases, cancellation and retry owners remain unchanged;
no task-control token, content store or replay permission is introduced.

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

Before each commit, run focused tests and checks for the changed behavior.
Expand validation when the affected surface, integration risk, repository
policy, or release scope requires it. Report checks not run and why.

### Shared-host validation safety

Use locked Make targets for focused or full tests, type checks, and builds,
including isolated worktrees. They share one slot per host/Unix user at
`/tmp/cognis-validation-<uid>.lock`; direct `uv`/`npm`/`npx` bypass it.
Focused checks remain preferred for bounded changes:

```bash
make test PYTEST_ARGS='tests/unit/test_validation_lock.py -q -k deadline'
make test-ui VITEST_ARGS='src/lib/chat-v2/outbox-clone.test.ts --maxWorkers=2'
make validate COMMAND='uv run pytest tests/contract/ -v'
```

Use `make check-ui`, `make build-ui`, `make build-standalone`, or the E2E targets
for broader checks. Pytest and Vitest default to CPU/2 (rounded down, minimum one;
four on an eight-CPU host). Do not raise worker counts above this default.
Waiting reports the owner PID immediately and every five minutes. The default
wait is one hour; callers may override `VALIDATION_LOCK_TIMEOUT` in seconds.
Never delete a live lock or nest locked Make targets. Keep launchers alive and
stop task-owned orphan validators after cancellation before retrying. Separate
users/temporary directories and detached services are outside the lock's scope.
See `docs/development/release-validation.md` for lifecycle details.

After tests, clean up only resources you created: `docker compose down -v
--remove-orphans` (use `--rm` for one-off containers), disposable containers,
volumes and networks; stop your processes and browsers.
After merge or push, remove your worktree with `git worktree remove` (never
`--force` over uncommitted changes) and its merged local branch. Never remove
anything you did not create.

The commands below are the canonical repository check inventory, not a
mandatory checklist for every commit. Run the checks that own or cover the
changed boundary:

```bash
uv run ruff check cognis/ packages/common/src/ packages/executor/src/ tests/
uv run ruff format --check cognis/ packages/common/src/ packages/executor/src/ tests/
MYPYPATH=packages/common/src:packages/executor/src uv run mypy --explicit-package-bases cognis/
uv run --package cognis-common mypy packages/common/src/cognis
MYPYPATH=.:packages/common/src:packages/executor/src uv run --package cognis-executor mypy --explicit-package-bases packages/executor/src/cognis
make test
cd ui && npm test -- --run && npm run check && npm run build:standalone
```

Use path-scoped Ruff and focused pytest selections for bounded backend changes.
Run mypy for each affected Python package. Run the relevant frontend test,
check, and build commands for UI changes. Run `make test` and the broader
package matrix for cross-cutting changes, release candidates, or when focused
checks cannot establish the affected invariants.

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
| Release | Full unit/lint/type/build baseline; risk-based service, database, E2E, browser, and package qualification below |

### Release qualification and test maintenance

Prepare the qualification matrix from **all changes since the previous release**,
not only the release-preparation diff. Always run the full unit suites, lint,
package type checks, UI checks/builds, and changelog validation. Select relevant
contract/integration/E2E/browser scenarios using the table above; run the full
scenario suite for broad runtime/protocol changes. Schema changes require both
SQLite and PostgreSQL evidence; package-boundary changes require isolated installs.
Do not rerun unrelated exhaustive browser/device permutations solely because a
release is being prepared. Record commands, environment, skips, and blockers;
missing service credentials or infrastructure are **not passing checks**.

Tests should assert supported behavior and ownership, not incidental query
parameter order, private component placement, exact CSS spelling, or wall-clock
speed. Keep mocks aligned with the canonical runtime models and cover explicit
authorization failures when updating permission-aware fixtures. Correct an
obsolete expectation only after verifying the intended contract in code and
existing behavior tests. Never skip a regression, relax authorization, suppress
type errors, or raise timing limits merely to make a release green.

See `docs/development/release-validation.md` for the evidence format.

### Chat v2

The active chat protocol uses `chat_v2_frame`,
`conversation_runtime_snapshot`, and REST snapshot/sync/backfill under
`cognis/api/chat_v2/`. There is no legacy timeline transport.

For ordering, identity, reconnect, or projection changes:

```bash
uv run pytest tests/e2e/ -v
make e2e-events-replay
make e2e-browser
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

## Changelog Fragments

- Add `changelog.d/<unique-name>.<category>.md` with every user/operator-visible
  feature, fix, security change, deprecation/removal, or configuration change.
  Categories: `added`, `changed`, `fixed`, `security`, `deprecated`, `removed`,
  `upgrade`. Breaking changes also need an `upgrade` note with required action.
- Write concise English user-facing prose, no heading/bullet prefix, secrets,
  private URLs, or invented issue IDs. Skip pure refactors, tests, typos, and
  docs that only explain existing behavior. Group related work, not commits.
- Run `uv run python scripts/changelog.py` and commit the fragment plus generated
  index; verify with `--check`. Do not edit generated JSON or finalize/consume
  fragments during development. See `changelog.d/README.md` for release commands.

## Git and Delivery

- Follow the active coding skill for worktree, review, and commit discipline.
- Never push to `main` or `master` without explicit approval.
- Stage tracked changes with `git add -u`.
- Add new task-owned files by explicit path. Never use `git add -A`.
- Use Conventional Commits unless the user requests another repository
  convention.
- Commit only task-owned changes.
