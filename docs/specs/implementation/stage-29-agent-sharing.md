# Stage 29: Agent Sharing

## Status

PLANNED

## Goal

Ship user-to-user agent sharing per
[docs/specs/28-agent-sharing.md](../28-agent-sharing.md):

1. A new `agent_grants` table with polymorphic-grantee schema (user
   wired in MVP; group reserved).
2. Route-level authorization that recognizes "use" grants and that
   does **not** admit an admin-role bypass for user-owned resources.
3. A two-headed runtime identity (acting user + agent owner) threaded
   through session manager, agent loop, task queue, workflow engine,
   and scheduler.
4. Mnemory wire-contract extension: `X-Agent-Owner` header +
   optional `aow` JWT claim, with the companion Mnemory change to
   key storage by `(user, owner)`.
5. Owner-configurable executor scope per share (`owner_executor` or
   `grantee_executor`).
6. Grant lifecycle CRUD plus revocation side effects (pause the
   grantee's schedules/tasks that target the revoked agent).
7. Secondary agent gating: system secondaries remain universal; non-
   system bound secondaries require their own explicit share for the
   grantee.
8. UI: sharing panel for owners, read-only agent view for grantees,
   "shared with me" section in the agent list.

This stage is scheduled **before** stages 30 (auto routing),
31 (workflow deliverables and step profiles), and 32 (workflow-first
composition) because:

- It introduces `current_agent_owner_email` in the runtime context —
  later stages will need to plumb it through the routing helper,
  deliverable writes, and composed-workflow handoffs.
- It redefines `check_agent_access` — later stages should use that
  helper from the start, not retrofit away from
  `require_owner_or_admin`.
- It extends the Mnemory protocol, which is simpler to land before
  the workflow-deliverable code touches recall again.
- Auto routing needs to consider shared agents in its candidate set,
  so sharing must land first.

## Dependencies

- [docs/specs/28-agent-sharing.md](../28-agent-sharing.md)
- [docs/specs/02-agent-model.md](../02-agent-model.md) (Ownership and
  Sharing section — to be aligned)
- [docs/specs/05-integrations.md](../05-integrations.md) (Mnemory
  contract)
- [docs/specs/07-security-identity.md](../07-security-identity.md)
  (JWT claims, no-admin-bypass rule)
- [docs/specs/01-architecture.md](../01-architecture.md) (DB schema)
- Stages 20–28 complete (harness, provider, LLM-exposure audit,
  browser takeover, and agent runtimes baseline).
- Companion Mnemory work is ready behind a feature flag (see
  "Coordination with Mnemory" below). If Mnemory is not updated yet,
  this stage ships in two slices; see "Phased rollout".

## Scope

### In scope

- `agent_grants` table: SQLAlchemy model, Alembic migration, idempotent
  `_ensure_agent_grants_table()` in `cognis/bootstrap.py`.
- Pydantic models: `AgentGrant`, `AgentGrantCreate`, `AgentGrantUpdate`,
  `AgentGrantResponse`, `SharedAgentSummary`.
- Auth: `check_agent_access(required=...)` in `cognis/api/common.py`,
  replacing `require_owner_or_admin` on agent-scoped routes. The new
  resolver **must not** treat `role == "admin"` as implicit access
  for user-owned resources.
- Query helpers: `list_agents_for_user(email, include_shared=True)`,
  `list_active_grants_for_user(email)`, `get_active_grant(agent_id, grantee_email)`.
- Runtime ContextVar: `current_agent_owner_email` in
  `cognis/runtime_context.py`, threaded alongside every existing
  `current_agent_id` setter.
- Executor resolution: `executor_scope` branch in
  `cognis/core/executor_resolution.py:select_executor_for_agent`.
- Secrets lookup: owner-scope when `executor_scope == "owner_executor"`,
  caller-scope otherwise.
- Mnemory wire change: `X-Agent-Owner` header + `aow` JWT claim.
- Bootstrap personality flow unchanged (already writes under
  `agent.owner_email`).
- Secondary-agent gating in delegation path.
- Grant CRUD API routes + `shared-with-me` route.
- Route ACL swap on:
  - `GET/PUT/DELETE /agents/{id}` and sub-routes (bindings, sync,
    activate, suspend, sync-personality, duplicate)
  - `POST /conversations`, `POST /tasks`, `POST /schedules` (caller
    must hold `use` on the target agent)
- Revocation side effects: pause grantee's active tasks/schedules
  against the revoked agent.
- UI:
  - Sharing tab on agent detail (owner view).
  - Share dialog with executor-scope radio and non-system secondary
    warning + "share all dependents" shortcut.
  - Read-only agent detail page for grantees.
  - "Shared with me" list section.
  - Revocation confirmation with task/schedule impact summary.
- Invariants in `cognis/core/invariants.py`:
  - Orphan grant (agent gone) → reconciled (hard-delete).
  - Grant with missing grantee_user_email user → reconciled.
  - Active schedule/task under a revoked-or-missing grant (and not
    owner-initiated) → paused with `access_revoked` reason.
- Tests: unit, contract (flagged), integration.

### Out of scope

- Group/team grantees beyond schema.
- Group/team ownership of agents.
- Per-action ACL beyond `use`.
- Cross-user multi-participant conversations.
- Owner-authored Intaris policies shipped with an agent.
- Public agent discovery (Agent Card at `/agents/{id}/card` remains
  stubbed).
- A `manage` permission level.
- Backfill UI for legacy `visibility` field on agents (see "Migration"
  below).

## Deliverables

### 1. DB + models

- `cognis/store/models.py` — `AgentGrantRow` with the columns from
  spec 28.
- `cognis/store/migrations/versions/<rev>_agent_grants.py` — upgrade
  creates table + indexes; downgrade drops them.
- `cognis/bootstrap.py` — `_ensure_agent_grants_table()`, registered in
  `run_schema_bootstrap()`.
- `cognis/models/agent_grant.py` — Pydantic domain model.
- `cognis/store/queries.py` — query helpers listed in "Scope".

### 2. Auth resolver

- `cognis/api/common.py` — `check_agent_access(request, db, agent,
  *, required) -> AgentAccess`. The function:
  - returns owner allow for `owner_email == caller`;
  - for `required ∈ {view, use}` also allows an active `use` grant
    for the caller;
  - **never** allows on the sole basis of `caller.role == "admin"`;
  - returns 403 in all other cases;
  - carries the matching `AgentGrantRow` back when access was via a
    grant.
- `require_owner_or_admin` stays for non-agent resources (users,
  settings). Nothing in the agent-scoped routes calls it anymore.
- A unit test matrix covers: owner / admin-not-owner / grantee /
  revoked grantee / stranger × {view, use, edit, delete, share}.

### 3. Runtime context

- `cognis/runtime_context.py` — new ContextVar
  `current_agent_owner_email: ContextVar[str | None]`.
- `scoped_runtime_context(user_email, agent_id, agent_owner_email=None, ...)`
  sets all three. Callers that do not know the owner (e.g., pure
  personal memory calls with no agent) pass `None`.
- Update every site currently setting `current_agent_id`:
  `cognis/core/session.py`, `cognis/core/agent_loop.py`,
  `cognis/core/task_queue.py`, `cognis/core/workflow_engine.py`,
  `cognis/core/schedules.py` (or equivalent), channel inbound
  handlers, executor inference bridge, runtime_support.

### 4. Mnemory wire contract

- `cognis/providers/auth/jwt.py:sign_service_jwt` — accept
  `agent_owner_email` kw; include `"aow": <email>` when different
  from `sub`. Leave it out when equal (prevents unnecessary cache
  invalidation of existing JWTs).
- `cognis/providers/memory/mnemory.py:_headers` — emit
  `X-Agent-Owner: <email>` whenever `current_agent_id` is set; value
  is `current_agent_owner_email.get() or subject`. Pass the same
  owner into `sign_service_jwt`.
- `cognis/providers/guardrails/intaris.py:_headers` — same passthrough
  header (no behavior in MVP, ensures protocol parity).
- Docstrings and spec cross-links.
- Companion Mnemory change (separate repo) adds the `owner` dimension
  to its storage layer. See "Coordination with Mnemory".

### 5. Executor resolution

- `cognis/core/executor_resolution.py:select_executor_for_agent` —
  new kwargs `caller_email`, `agent_owner_email`, `executor_scope`.
  Branches per spec 28. Callers in `runtime_support.py` and
  `core/session.py` pass the resolved values.
- `cognis/core/executor_policy.py:is_executor_row_usable` unchanged
  in signature; callers pass the correct `owner_email`.
- Secrets fetcher in `cognis/core/tool_router.py` (or wherever
  it currently resolves) reads owner vs caller based on
  `executor_scope`.

### 6. Secondary-agent gating

- Delegation path in `cognis/core/agent_loop.py` (or registry lookup
  it calls) consults the active grant on a non-system secondary
  before dispatching.
- Tool-result on failure is structured `is_error=true` with a
  human-readable message that names the missing secondary and
  advises the grantee to ask the owner. No stack traces, no memory
  content leaked.

### 7. API routes

- `cognis/api/routes/agents.py`:
  - Replace `require_owner_or_admin` on all agent routes with
    `check_agent_access(..., required=<verb>)`.
  - `GET /agents` — list owned + shared-with-me.
  - Responses include `is_shared_with_me`, `shared_by_email`,
    `granted_permission`, `executor_scope`,
    `is_readonly_for_caller`.
- `cognis/api/routes/agent_shares.py` (new):
  - `GET /agents/{id}/shares`
  - `POST /agents/{id}/shares`
  - `PATCH /agents/{id}/shares/{grant_id}` (executor_scope + note)
  - `DELETE /agents/{id}/shares/{grant_id}`
- `cognis/api/routes/users.py` (or new `me.py`):
  - `GET /users/me/shared-with-me`
- `cognis/api/routes/conversations.py`, `.../tasks.py`,
  `.../schedules.py` — access check uses `check_agent_access`.
- `cognis/api/models.py` — add grant request/response models.
- `tests/unit/test_api_contracts.py` — round-trip coverage on the
  new models.
- `ui/src/lib/types/api.ts` — types in sync; enforced by
  `tests/unit/test_ui_contract_sync.py`.

### 8. Revocation side effects

- On `DELETE /agents/{id}/shares/{grant_id}` (and on cascade-delete
  of the agent):
  - Stamp `revoked_at`.
  - Pause grantee's active schedules and non-terminal tasks for that
    agent with a reason field (`access_revoked`) surfaced in the UI.
- Owner-view audit section lists these pauses.

### 9. UI

- Agent detail, owner view:
  - New "Sharing" tab.
  - Table of active grants (grantee email, permission, executor
    scope, granted date, note).
  - "Share" dialog: email picker, permission (disabled, value fixed
    at `use`), executor scope radio, list of bound non-system
    secondaries with per-row "include" checkboxes plus a "Share all"
    shortcut, free-text note.
- Agent detail, grantee view:
  - Banner: "Shared with you by {owner_email}".
  - Whole form read-only. Edit / Delete / Share / Sync Personality
    buttons hidden. Duplicate remains (creates the grantee's own
    editable copy).
  - LLM config, tools, executor, and personality shown, marked
    "read-only".
- Agent list:
  - Sections: "My agents", "System agents", "Shared with me".
- Revocation confirm dialog:
  - Shows count of the grantee's impacted schedules/tasks that will
    be paused.
- Mobile + keyboard-shortcut parity.

### 10. Invariants and reconcile

- `cognis/core/invariants.py`:
  - `check_agent_grants_integrity`:
    - every active `agent_grants.agent_id` → existing agent
    - every active grant with `grantee_type='user'` → existing user
  - `check_access_consistency`:
    - no active schedule with `agent_id=X, created_by=G` where G is
      not the owner of X and has no active `use` grant on X → pause
  - Reconciler stamps fixes at startup and via
    `/api/v1/system/reconcile`.

### 11. Tests

- Unit:
  - `check_agent_access` matrix.
  - Executor resolver with both scopes + admin-as-caller.
  - Mnemory header builder and JWT claim.
  - Grant CRUD serialization round-trip.
- Contract (gated on Mnemory feature flag):
  - `(user=O, owner=O)` bootstrap visible to grantee's recall.
  - Grantee remember creates `(G, O)`, invisible to owner.
  - Cross-grantee isolation of episodic.
  - Personal memory outside agent is still mutually invisible.
- Integration:
  - Full share → chat → delegate → schedule → revoke.
  - Admin (no grant) cannot list, read, or use another user's
    agent. This is an explicit regression test for "no admin
    bypass".
  - Non-system secondary gating with/without share.
  - `executor_scope` toggle takes effect on next turn.

## Coordination with Mnemory

The Mnemory-side change is a prerequisite for the episodic
isolation promise. The companion work in the Mnemory repo:

1. Add `owner` column to memories (backfill `owner = user`).
2. Accept `X-Agent-Owner` header and `aow` JWT claim; default to
   `sub` when absent.
3. Index and query on `(user, owner, agent_id)`.
4. Bootstrap writes still use `sub` as the writer identity.
5. Feature-flag the new behavior.

Until Mnemory ships this, the integration contract test for
`(G, O)` isolation remains skipped behind a feature flag
(`MNEMORY_OWNER_SCOPE_ENABLED`).

## Phased rollout inside this stage

**Slice 1 — Cognis-only sharing with identity-only memory.**
Everything except the `(user, owner)` episodic behavior. Grantees see
the owner's pinned identity memories but their episodic memories
cross-contaminate (current behavior). Sharing is usable for config +
inline prompts + tool isolation.

**Slice 2 — Mnemory `(user, owner)` behavior.** Flip the feature flag.
Run the contract tests. Ship.

Both slices are behind a single config flag
`FEATURE_AGENT_SHARING` gating the new routes and UI sections, so the
stage can be merged and rolled out independently.

## Migration notes

- There is a stale `visibility: str = "private"` reference in
  `docs/specs/02-agent-model.md`. That field is not in the DB and
  never was exposed via the API; this stage removes the reference
  from the spec and defines sharing solely through `agent_grants`.
- No backfill needed for agents — grants default to none.
- Mnemory-side migration: one-off script that sets `owner = user`
  on every pre-existing record. See Mnemory repo.

## Acceptance criteria

- Owner can share an agent with a second user by email; grantee can
  start a conversation with that agent; owner cannot see the
  grantee's messages, tool calls, or memories.
- Grantee cannot edit, delete, or reshare the shared agent. UI hides
  the controls; API returns 403.
- Admin user with no grant **cannot** read or invoke a user-owned
  agent through any route.
- Executor scope toggle behaves deterministically on the next turn
  after change.
- Revoking a grant immediately blocks new access and pauses the
  grantee's active schedules/tasks for that agent.
- Mnemory contract tests pass with the Mnemory feature flag on.
  Without the flag, identity-only sharing still works and does not
  leak the owner's personal memory (verified by a second integration
  test that plants a personal-memory record under the owner and
  confirms it is not returned to the grantee's recall on the shared
  agent).
- Existing `cognis-controller` acceptance tests continue to pass.
- `tests/unit/test_api_contracts.py` and
  `tests/unit/test_ui_contract_sync.py` pass after new models are
  added.

## Risks and mitigations

- **Mnemory dependency.** Sharing has weak privacy guarantees for
  episodic memory until Mnemory ships the companion change. Mitigated
  by the Slice-1/Slice-2 phased rollout and the feature flag.
- **Admin-role expectation change.** Admins may expect to "see
  everything" as in many SaaS tools. Mitigated by an explicit note
  in the settings UI and in `docs/guide/*` on what admin can and
  cannot do. Break-glass remains available via CLI and direct DB
  access.
- **Executor-scope confusion.** Grantee may wonder why a tool is
  missing. Mitigated by clear warning at share time and an
  executor-scope badge in the chat sidebar.
- **Secondary agent reshare fan-out.** A primary with many bound
  non-system secondaries may make sharing feel heavy. Mitigated by
  the "share all dependents" shortcut and the pre-share enumeration.

## Stage exit

Update the tracker in
[implementation/README.md](README.md): Stage 29 DONE. Flag
`FEATURE_AGENT_SHARING=on`. Add a follow-up note in later stages that
`check_agent_access` is the canonical resolver going forward.
