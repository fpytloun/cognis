# Cognis: Agent Sharing

## Status

PLANNED. This spec defines agent sharing between users for trusted
collaboration (team, family, friends). Groups/teams as grantees and as
owners are deferred to later phases. Owner-shipped Intaris policies are a
later phase as well.

## Goals

1. The owner of an agent can share it with another user by email.
2. The grantee can **use** the agent (start conversations, create tasks,
   create schedules, read the agent's configuration) but **cannot** edit,
   delete, reshare, or trigger identity/personality re-sync.
3. The grantee's conversations, tasks, and schedules on a shared agent
   belong to the grantee. The owner never sees grantees' message content,
   tool arguments, or memory content.
4. The agent's identity — personality, core memories, system prompt — is
   authored once (by the owner) and is reused for every grantee's turn.
   Episodic memories written during a grantee's turn stay scoped to that
   grantee on that agent; they do not leak into the owner's personal
   memory namespace and they do not leak between grantees.
5. The owner decides per-share whether the agent runs on the owner's
   executor (isolated, owner-provisioned tools/secrets/MCP) or on the
   grantee's executor (grantee-provisioned tools/secrets).
6. Non-owner access is **never** overridden by a system-admin role. An
   admin user is not a super-peer with the authority to chat with,
   delete, or reshare another user's agent. Admin continues to hold
   infrastructure-level authority (settings, providers, users, audit),
   but not peer-level authority over user-owned resources.

## Non-goals (MVP)

- Groups/teams as grantees. The data model supports it; the code path
  is not wired.
- Groups/teams as owners of an agent.
- Per-action ACL beyond the single `use` permission.
- Cross-user single conversation (multi-participant chat) with a shared
  agent. Each user always has their own conversation.
- Owner-authored Intaris policies shipped with the agent.
- Public agent discovery / Agent Cards.

## Principles

- **Separation of `acting user` and `agent owner`.** Every runtime turn
  carries both. The acting user is the caller (grantee or owner).
  The agent owner is the authoritative identity for the agent's
  personality, tool set, and default executor.
- **Owner retains write authority.** The grant confers *use*, never
  *modify*. The grantee can read the config, chat, run tasks — not
  change the agent.
- **Grantee's personal workspace stays theirs.** Conversations, tasks,
  schedules, personal memories, and personal executors belong to the
  grantee. The owner cannot enumerate or read them.
- **Agent's identity stays owner-scoped.** Personality, core memories,
  and the pinned bootstrap identity live in a namespace keyed by
  `agent.owner_email`. Grantees draw from that namespace; they do not
  write into it.
- **No admin bypass for user-owned resources.** Admins are not implicit
  sharers of every agent, not implicit readers of every conversation.
  Admin privileges are limited to system-level concerns (settings,
  provider configuration, user administration, audit log). Read-only
  audit endpoints may be admin-scoped, but they surface metadata, not
  content.
- **Polymorphic grantee schema now, user-only wiring.** The grants
  table carries `grantee_type` + both `grantee_user_email` and
  `grantee_group_id` from day one. Only `user` is honored by the code
  until groups land.

## Design

### Data model

#### `agent_grants` (new table)

```sql
CREATE TABLE agent_grants (
    grant_id             VARCHAR PRIMARY KEY,
    agent_id             VARCHAR NOT NULL
        REFERENCES agents(agent_id) ON DELETE CASCADE,
    grantee_type         VARCHAR NOT NULL CHECK (grantee_type IN ('user', 'group')),
    grantee_user_email   VARCHAR NULL REFERENCES users(email),
    grantee_group_id     VARCHAR NULL,                           -- reserved
    permission           VARCHAR NOT NULL CHECK (permission IN ('use')),
    executor_scope       VARCHAR NOT NULL
        CHECK (executor_scope IN ('owner_executor', 'grantee_executor')),
    granted_by           VARCHAR NOT NULL REFERENCES users(email),
    granted_at           TIMESTAMP WITH TIME ZONE NOT NULL,
    revoked_at           TIMESTAMP WITH TIME ZONE NULL,
    note                 TEXT NULL,
    CHECK (
        (grantee_type = 'user'  AND grantee_user_email IS NOT NULL AND grantee_group_id IS NULL)
     OR (grantee_type = 'group' AND grantee_group_id   IS NOT NULL AND grantee_user_email IS NULL)
    ),
    UNIQUE (agent_id, grantee_type, grantee_user_email, grantee_group_id)
);

CREATE INDEX ix_agent_grants_grantee_user ON agent_grants (grantee_user_email)
    WHERE revoked_at IS NULL;
CREATE INDEX ix_agent_grants_agent ON agent_grants (agent_id)
    WHERE revoked_at IS NULL;
```

- **Revocation is soft** (`revoked_at` stamp) so grants are auditable
  and so we can attribute historical tasks/schedules to the grant that
  authorized them. Active grants are those with `revoked_at IS NULL`.
- `permission = 'use'` is the only MVP value. The column is an enum to
  avoid a migration if `manage` or a narrower role is added later.
- `executor_scope` is per-grant so the owner can decide isolation on a
  per-grantee basis.
- `note` is an owner-visible free-text field — useful for remembering
  why a grant exists.

Both an Alembic migration and an idempotent `_ensure_agent_grants_table`
bootstrap helper are required; this follows the AGENTS.md rule.

#### `agents` table

No new column. Ownership remains `owner_email` (single user). Sharing is
expressed through `agent_grants`, not a `visibility` enum. If and when
we need public discovery, a `visibility` field can be added
independently.

The informal `visibility` field shown in earlier drafts of
`02-agent-model.md` is withdrawn by this spec. Presence of at least one
active grant makes an agent "shared"; absence makes it "private".

#### Other tables

No new columns on `conversations`, `sessions`, `tasks`, `schedules`.
Their `user_email` / `created_by` fields already point to the acting
user. What changes is the **access check** that gates creation (see
Authorization below).

### Authorization model

A single resolver replaces `require_owner_or_admin` for agent-scoped
routes:

```python
async def check_agent_access(request, db, agent, *, required: str) -> AgentAccess
# required ∈ {"view", "use", "edit", "delete", "share"}
```

Decision matrix (MVP):

| Required | Owner | Admin | Active `use` grantee | Other |
|---|---|---|---|---|
| `view` | allow | **deny** | allow | deny |
| `use`  | allow | **deny** | allow | deny |
| `edit` | allow | **deny** | deny | deny |
| `delete` | allow | **deny** | deny | deny |
| `share` | allow | **deny** | deny | deny |

The admin role does not appear as `allow` anywhere in this table. This
is the deliberate "no admin bypass" rule. Admin retains authority over:

- `/settings/*`, `/providers`, `/executors` (global), `/model-routing`
- User administration (`/users/*`) and API keys of other users
- Audit log / reconciliation endpoints (metadata only)
- System agents

Admin does **not** gain access to another user's agents, conversations,
tasks, schedules, memories, or secrets through their role alone. If an
admin legitimately needs to operate on another user's agent, that user
must share it with them like any other peer. For break-glass
situations, operators have direct DB access (the CLI `admin` commands)
— which is visible, logged, and distinct from an in-app role.

`AgentAccess` carries the grant row when access was via a grant, so
downstream code (executor resolver, Mnemory header builder) can read
`executor_scope` without a second query.

### Listing

- `GET /agents` list returns: agents where `owner_email = caller`
  **UNION** agents where an active grant exists for the caller.
- `AgentResponse` gains:
  - `is_shared_with_me: bool`
  - `shared_by_email: str | None`
  - `granted_permission: str | null`
  - `executor_scope: str | null`
  - `is_readonly_for_caller: bool` — true iff caller is grantee, not
    owner.
- The tool-visible summary used by the `list_agents` tool
  (`list_active_agents_summary`) includes shared agents so the LLM can
  also target them from delegation.

### Runtime identity (two-headed context)

Today, `current_user_email` alone identifies the turn. This spec adds a
second ContextVar:

- `current_user_email` — the acting user (caller). Unchanged.
- `current_agent_owner_email` — the agent's owner. New.

Both are populated by `scoped_runtime_context(...)` alongside
`current_agent_id`. Every session manager / agent loop / task queue /
workflow engine / scheduler entry point that already sets
`current_agent_id` also sets `current_agent_owner_email =
agent.owner_email`.

When caller = owner, the two are equal (today's behavior).

### Mnemory: `(user, owner)` memory keying

Mnemory today implicitly treats the caller's JWT `sub` as the tenant.
For sharing to work correctly, Mnemory must distinguish:

- **user** — whose perspective / episode is this memory from
- **owner** — which agent's namespace does it belong to

The scoping is:

| Record | `user` | `owner` | Examples |
|---|---|---|---|
| Owner's identity / pinned personality | owner_email | owner_email | bootstrap writes |
| Owner's episodic on their own agent | owner_email | owner_email | today's default |
| Grantee's episodic on shared agent | grantee_email | owner_email | remembered during grantee's turn |
| Any personal memory outside an agent | user_email | user_email (= user) | personal notes |

#### Cognis-side wire contract

- `JWTAuthProvider.sign_service_jwt(...)` accepts `agent_owner_email`
  and includes an `"aow"` claim (agent-owner) when different from
  `sub`. When equal, the claim is omitted.
- `MnemoryProvider._headers(...)` emits `X-Agent-Owner` on every call
  that has a resolved agent, with value `current_agent_owner_email`.
  When equal to `sub`, Mnemory treats it the same as today (no
  sharing).

#### Mnemory-side semantic contract

Mnemory storage becomes keyed by `(user, owner, agent_id, memory_id)`.
Existing records have `user == owner` by construction. The migration is
a backfill that stamps `owner = user` on every pre-existing memory.

Recall for a turn with `(user=U, owner=O, agent_id=A)` returns the
union of:

- records with `(user=O, owner=O, agent_id=A)` — the agent's identity
  and the owner's episodic (shared personality);
- records with `(user=U, owner=O, agent_id=A)` — the caller's episodic
  on this particular agent.

Recall outside an agent context (personal memory) queries records with
`user=U` and the default owner=`U`; it never returns `owner≠U` records.

This preserves three invariants:

1. Owner's personal memory never leaks to grantees.
2. Grantees' personal memories outside the agent never leak to the
   owner.
3. Grantees' episodic memory on the shared agent never leaks between
   grantees.

#### Bootstrap writes

`bootstrap_agent` and `replace_bootstrap_identity` continue to write
with `user = agent.owner_email`. The JWT already contains that
subject; no code change in Cognis — just the Mnemory side honors the
owner dimension.

### Executor routing

`select_executor_for_agent` gains an argument:

```python
def select_executor_for_agent(
    executors, agent_execution, *, caller_email, agent_owner_email,
    executor_scope, policy=None,
) -> ExecutorRow | None
```

Branches:

- Caller is the owner → `filter_owner = caller_email` (today's
  behavior).
- Caller is a grantee, `executor_scope == "owner_executor"` →
  `filter_owner = agent_owner_email`. The agent runs on the owner's
  executor pool even though it was triggered by the grantee.
- Caller is a grantee, `executor_scope == "grantee_executor"` →
  `filter_owner = caller_email`. The agent runs on the grantee's
  executor pool.

`is_executor_row_usable`'s owner check is unchanged; we just pass the
right owner.

#### Subprocess executors

Subprocess executors mint a short-lived JWT. When `executor_scope ==
"owner_executor"`, the JWT subject is still the caller (grantee) but
the `X-Agent-Owner` / `aow` claim carries the owner. The
subprocess-provisioned secrets lookup (see below) uses the owner's
email when the executor is owner-scoped.

### Secrets

Secrets are `(user_email, name, scope, agent_id)` today.

- `executor_scope == "owner_executor"` → secrets are fetched under
  `user_email = agent.owner_email`. The agent uses the owner's
  credentials transparently; the grantee never sees the values.
- `executor_scope == "grantee_executor"` → secrets are fetched under
  `user_email = caller`. The grantee must have the relevant secrets
  in their own store. The UI warns about this at share time.

### Secondary agents

System secondaries (`system:*`) remain universally available. Any
primary agent — owned or shared — can delegate to them.

Non-system secondary agents are user-owned and bound to a primary
through `agent_secondary_bindings`. When a grantee runs a shared
primary that is bound to a non-system secondary:

- If the secondary is also shared with the same grantee (active `use`
  grant on the secondary) → delegation proceeds.
- Otherwise → the delegation tool call returns an `is_error=true`
  result: "Secondary agent `X` is required by this workflow but is
  not shared with you. Ask the owner to share it."

The share UI, at the moment of creating a grant for a primary,
enumerates the primary's non-system bound secondaries and warns:

> This agent delegates to `writer`, `editor`, `committer`. These are
> your private secondary agents. To get identical behavior, share them
> with the same grantee. You can share all with one click.

A `Share all dependent secondaries` shortcut creates grants for every
non-system secondary in the same transaction.

### Intaris

MVP change: Intaris receives `X-Agent-Owner` as a passthrough header on
every call for parity with Mnemory. No body/policy changes. Intaris
does not need to act on it in MVP, but shipping the header now means
Phase 3 (owner-authored policies) does not require a protocol change.

### Conversations, tasks, schedules

- `POST /conversations` with `agent_id=X` — requires caller to hold
  `use` on X. `conversation.user_email = caller`.
- `POST /tasks` with `agent_id=X` — same.
- `POST /schedules` with `agent_id=X` — same. The schedule fires under
  the grantee's identity (`current_user_email = grantee`,
  `current_agent_owner_email = agent.owner_email`) even when the
  grantee is offline.
- Listing remains per-user: `created_by = caller` or `user_email =
  caller`. Owner does not see grantee conversations/tasks/schedules.

### Revocation

`DELETE /agents/{id}/shares/{grant_id}`:

1. Stamp `revoked_at = now()`.
2. Scan `schedules` where `agent_id = grant.agent_id AND created_by =
   grant.grantee_user_email AND status = 'active'` → mark `paused`
   with reason `access_revoked`.
3. Scan `tasks` where the same predicate and `status ∈
   (queued, running, paused_*)` → move to `paused_access_revoked` (new
   terminal-pause reason). Do not delete.
4. Running turns complete or abort at the next boundary. New turns are
   rejected by `check_agent_access`.

The grantee sees the resources in their UI with an "access revoked"
marker. The owner sees them only in their audit section, not as part
of their own tasks/schedules.

### Ownership by team (Phase 3 sketch, not implemented)

- Introduce a `Principal` abstraction.
  `resolve_agent_owner_principal(agent) -> UserPrincipal | GroupPrincipal`.
- `agents` gets `owner_type`, `owner_user_email`, `owner_group_id`,
  with existing `owner_email` kept as a compatibility view.
- Mnemory's `owner` dimension becomes `(type, id)` instead of an
  email.

None of this is built in MVP. The shape of `agent_grants.grantee_type`
and the `Principal` naming is chosen to make Phase 3 a purely additive
change.

## Out-of-band channels and delivery

Channel adapter delivery is per-conversation. Sharing does not change
that. A grantee chatting via Signal/Slack on a shared agent gets
delivered their own conversation's events — never the owner's.

## Audit

System-level audit records the **grant lifecycle** (create, update,
revoke) with `granted_by`, `grantee_user_email`, `agent_id`, and
`permission`. These are IDs and are safe to log under the redaction
allowlist.

No audit entry is generated for individual messages or tool calls on a
shared agent — those flow through Intaris session recording as they
would for a private agent.

## Failure modes

| Condition | Behavior |
|---|---|
| Grant does not exist | 403 from `check_agent_access(required="view"\|"use")` |
| Grant exists but `revoked_at` set | 403; same shape |
| Agent deleted while grant active | grants cascade-deleted; grantees lose access, 403 on next call |
| `executor_scope=owner_executor` and owner's executor is offline | runtime raises the same `executor_unavailable` error as for the owner; surface the owner's email in the error to help the grantee contact them |
| `executor_scope=grantee_executor` and grantee's executor lacks a tool the agent needs | tool call returns `is_error=true` with a clear message; agent can decide how to proceed |
| Grantee tries to access a non-system bound secondary that is not shared | delegation tool returns `is_error=true` with instructions to ask the owner to share the secondary |
| Mnemory cold-start on grantee's first recall | same 1–2s latency as today; no functional difference |
| Mnemory returns owner's personal memory to grantee (bug) | treated as a P0 regression; contract tests enforce the invariant |

## Testing

- **Unit.** `check_agent_access` matrix including admin-as-caller
  (must not grant access). `select_executor_for_agent` in both
  `executor_scope` values. Mnemory header builder: `X-Agent-Owner`
  exactly when `current_agent_owner_email != current_user_email`. JWT
  `aow` claim present/absent correctly. API round-trip contracts for
  `AgentGrantCreate/Update/Response` and `SharedAgentSummary`.
- **Contract (Mnemory).** Identity writes with `(O, O)` visible to
  both O and G when talking to agent A. G's remember creates `(G, O)`
  not visible to O. O's personal memory outside A not visible to G.
  G's personal memory outside A not visible to O.
- **Integration.** End-to-end: create, share, chat, delegate,
  schedule, revoke. Non-system secondary gating. Executor scope
  toggle takes effect next turn. Admin user (without a grant) cannot
  list or invoke the agent.
- **Invariants** (`cognis/core/invariants.py`):
  - Every active `agent_grants.agent_id` resolves to an agent.
  - No active task/schedule on an agent whose grant to its
    `created_by` is revoked or missing (or the owner).
  - No agent grant with `grantee_user_email` referencing a missing
    user.

## Spec cross-references

- [02-agent-model.md](02-agent-model.md) — agent definition and
  ownership; this spec replaces the informal "Ownership and Sharing"
  section.
- [05-integrations.md](05-integrations.md) — Mnemory wire contract is
  extended with `X-Agent-Owner` / `aow` claim and the `(user, owner)`
  keying requirement.
- [07-security-identity.md](07-security-identity.md) — JWT claim
  additions and the "no admin bypass for user-owned resources" rule.
- [01-architecture.md](01-architecture.md) — new table
  `agent_grants`.
- [10-api-spec.md](10-api-spec.md) — new routes for grant CRUD and
  `shared-with-me`.

## Implementation plan

See [implementation/stage-29-agent-sharing.md](implementation/stage-29-agent-sharing.md).
This stage is scheduled **before** stages 30 (auto routing),
31 (workflow deliverables and step profiles), and 32 (workflow
composition) because it touches authorization, runtime identity, and
provider wire contracts that later stages build on top of.
