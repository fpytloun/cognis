# Stage 15: MVP Closure Sweep

**Status**: DONE
**Repo**: `cognis`
**Depends on**: Stages 12, 13, 14 (all polish stages complete)
**Estimated effort**: 3-4 days

## Objective

Close remaining backend TODOs, fill test coverage gaps, and align the
documentation with the polished product. No new features — only closure of
known debt and final stabilization.

## Context

The MVP build (Stages 0-9) and polish (Stages 10-14) leave behind a set
of known technical debt items: backend TODOs in the codebase, missing
implementation stubs referenced in the architecture, test coverage gaps
in critical modules, and tracker/documentation drift. This stage resolves
all of them so the product can be considered complete for its stated scope.

## Deliverables

### 1. Backend TODOs

Resolve all TODO comments that have user-visible or operational impact.

- **`core/workflow_engine.py:702`** — resolve `latest_active_for_agent`
  delivery mode. Currently a TODO; needs to query the most recent active
  conversation for the user+agent pair and deliver task results there.
- **`core/workflow_engine.py:766`** — trigger a new agent turn for idle
  conversations when task results are delivered. Currently task events are
  injected but do not wake up the agent loop for idle conversations.
- **`core/session.py:321`** — emit `SESSION_RECOVERED` event via the
  event bus. The event bus now exists (added in Stage 6); this TODO is
  stale and can be resolved by adding the `event_bus.publish()` call.
- **`providers/executor/in_process.py:173`** — emit Prometheus metrics
  for executor spawn timing and outcome (success/failure/timeout).
- **`core/tool_router.py:174`** — emit Prometheus metrics for tool route
  decisions (orchestration/intaris_mcp/local) and execution outcomes
  (success/failure/timeout/escalated).

### 2. Small Implementation Gaps

Items referenced in the architecture or specs but not yet implemented.

- **`tools/skills.py`** — implement the skill loader. Referenced in
  AGENTS.md architecture diagram and `01-architecture.md`. Skills are
  agent-attached capability bundles (tool sets + prompts). For MVP, this
  can be a simple loader that reads skill definitions from the agent's
  config and merges their tools into the agent's tool registry.
- **General API rate limiting** — the spec calls for per-user,
  per-endpoint rate limiting (`01-architecture.md` line 73). Currently
  only login has rate limiting. Add a middleware-level rate limiter using
  a token bucket or sliding window per user (identified by JWT `sub`).
  Conservative defaults: 60 req/min for reads, 20 req/min for writes.
- **Intaris event-store purge cascade** — `purge_conversation` in
  `core/session.py` currently only removes Cognis metadata. Add a
  `delete_session` call to the Intaris provider when purging, guarded
  by a provider capability check (if Intaris does not support session
  deletion, log a warning and continue).
- **WebSocket heartbeat** — the WS client (`ui/src/lib/ws/client.ts`)
  has a `ping()` method but never calls it on a schedule. Add a
  30-second ping interval to detect dead connections proactively.

### 3. Test Coverage Expansion

Fill the identified gaps in the test suite. Target: 30+ new test functions.

- **WebSocket handler** (`cognis/api/websocket.py`):
  - Connection lifecycle (auth timeout, invalid token, successful auth)
  - Message routing (send message, cancel turn, reconnect)
  - Error handling (malformed messages, rate limiting, turn failure)
  - Reconnection with event replay
  - Message queuing during active turn
- **Auth middleware** (`cognis/api/middleware.py`):
  - JWT validation (valid, expired, wrong audience, malformed)
  - API key validation (valid, invalid, revoked)
  - Public endpoint bypass (health, JWKS, setup, login)
  - Rate limiting (if added in this stage)
- **LiteLLM provider** (`cognis/providers/llm/litellm.py`):
  - Model resolution (explicit model, task type routing, default fallback,
    no config error)
  - Model info lookup (found in provider, not found → defaults)
  - Token counting (tiktoken path, fallback path)
  - Cache TTL behavior
- **Circuit breaker** (`cognis/providers/circuit_breaker.py`):
  - State transitions: CLOSED → OPEN → HALF_OPEN → CLOSED
  - Failure threshold triggering
  - Recovery after half-open success
  - Concurrent access safety
- **CLI commands** (`cognis/cli/`):
  - `admin create-user` (success, duplicate email)
  - `admin reset-password` (success, unknown user)
  - `admin api-key create` and `admin api-key list`
  - `config init` (prints template)
  - `status` (healthy, unreachable)
- **Workflow engine orchestration** (`cognis/core/workflow_engine.py`):
  - Step sequencing (advance through multi-step workflow)
  - Gate handling (pause, resume with response)
  - Review loop (reject → retry → approve)
  - Loop iteration limits (max attempts exhausted)
  - Step evaluation (approved, revise, timeout)
  - Task result delivery to conversation

### 4. Documentation Alignment

Update user-facing documentation to reflect the polished product.

- **Stage files**: update each completed stage file with implementation
  notes documenting any deviations, follow-ups, or decisions made during
  implementation.
- **AGENTS.md**: update if any architecture changed during polish stages
  (e.g., bundled UI serving, new API endpoints, new env vars).
- **README.md**: final pass to ensure all claims match the polished
  product. Version bump if appropriate.

### 5. Edge Case Hardening

Tighten validation and handling for known edge cases.

- **Settings API validation**: reject unknown setting keys, validate
  value types against expected schema.
- **Task API validation**: reject invalid status transitions (e.g.,
  submit a completed task), validate dependency cycles on batch submit.
- **Workflow API validation**: reject workflows with empty step lists,
  validate step input references against actual step names.
- **Concurrent WebSocket connections**: handle multiple connections to
  the same conversation gracefully (fan out events to all connections,
  serialize turn execution).
- **Conversation sidebar scaling**: if not addressed in Stage 13,
  ensure cursor-based pagination replaces `listAll()` for conversations.

## Acceptance Criteria

- [x] All 5 backend TODOs with user/operational impact are resolved
- [x] `tools/skills.py` exists and loads skill definitions from agent config
- [x] General API rate limiting is active with configurable limits
- [x] Intaris purge cascade is implemented (with fallback for unsupported
      provider)
- [x] WebSocket heartbeat ping runs on a 30-second interval
- [x] Test count increases by 30+ covering the identified gaps
- [x] WebSocket handler has dedicated unit tests
- [x] Auth middleware has dedicated unit tests
- [x] LiteLLM provider has dedicated unit tests
- [x] Circuit breaker has dedicated unit tests
- [x] CLI commands have dedicated tests
- [x] Workflow engine orchestration has unit tests (not just domain models)
- [x] AGENTS.md and README.md are up to date
- [x] No known paper-cut regressions in core flows
- [x] All stage files have implementation notes

## Key References

- `cognis/core/workflow_engine.py:702,766` — delivery mode TODOs
- `cognis/core/session.py:321` — SESSION_RECOVERED TODO
- `cognis/providers/executor/in_process.py:173` — executor metrics TODO
- `cognis/core/tool_router.py:174` — tool route metrics TODO
- `cognis/tools/` — skills.py target location
- `cognis/api/middleware.py` — rate limiting target
- `cognis/api/websocket.py` — heartbeat and test targets
- `ui/src/lib/ws/client.ts` — client-side ping target
- `AGENTS.md` — architecture reference to update

## Implementation Notes

- Resolved the workflow delivery TODOs by implementing latest-active
  conversation resolution, task-result context inclusion, and guarded
  follow-up-turn requests for connected idle conversations.
- Added general API rate limiting, structured client-safe error sanitization,
  executor/tool-router metrics, and agent `sync_metadata` for Mnemory
  bootstrap status.
- Implemented MVP `tools/skills.py` as inline skill-to-builtin-tool-name
  references stored under `agent.skills.items[*].tool_names`.
- Added broad new unit coverage for circuit breaker behavior, LiteLLM routing,
  CLI commands, middleware rate limiting, workflow delivery, sync metadata,
  purge cascade reporting, and error sanitization, plus UI heartbeat tests.
