# Cognis: API Specification

## Usage API client logs

`POST /api/v1/usage/requests` accepts the existing bounded detail query plus
`pagination_unit: "attempt" | "request"` (default `"attempt"`). Request mode
pages indexed unique logical headers, newest first. `POST
/api/v1/usage/requests/detail` accepts `scope`, `start`, `end`, `pricing_basis`,
required encrypted `selection_ref`, and optional attempt `limit`/`cursor`;
it returns `{meta, request, attempts, next_cursor}`.

Analytics add `traffic_origins`, `auth_types`, `client_ids`, `key_ids` filters
and `traffic_origin`, `auth_type`, `client`, `key` group/facet dimensions.
Request mode adds `http_status_codes`; provider/model filters select matching
requests without restricting totals to only their matching attempts.
Client/key selectors and private header metadata are unavailable in project
scope. References are not authorization grants.
See [Usage accounting](../guide/usage-accounting.md#api-clients-and-logical-request-logs)
for fields, retention, privacy, pagination and upgrade behavior.

## Streaming speech

Authenticated `GET /api/v1/tts/capabilities` adds `streaming_tts`,
`streaming_stt`, and `stt_sample_rate` to the existing readiness response.
These fields describe the selected providers/models, not a provider substitution.

`POST /api/v1/tts/stream` accepts the normal synthesis request and returns
`application/x-ndjson` with caching/proxy buffering disabled. Events are:
`audio_start`, ordered `audio` (base64 mono signed little-endian PCM, 24 kHz,
sample offsets), `audio_done`, completed `part` receipts with a `streamed` flag,
and a final `complete` containing the normal synthesis response. A cached part
can have a receipt without live frames. `audio_done` is not an artifact receipt;
only a complete manifest confirms the request finished. A transport/provider
failure emits a sanitized `error` and does not authorize automatic regeneration.
Viewer restrictions, agent access and speech preferences match normal synthesis.

The authenticated browser WebSocket accepts `speech_stt_start`,
`speech_stt_audio`, `speech_stt_commit`, and `speech_stt_cancel` for one
connection-owned `request_id`. Audio frames contain `audio_base64`, bounded to
32 KiB of mono PCM16. `speech_stt_event` carries normalized start/partial/
transcript/done events; `speech_stt_error` carries a safe code. Only final
transcript/done confirms a committed utterance. Viewer requests are refused.
Connections retain the existing authentication, revocation and rate limits;
there is no reconnect/replay protocol for live microphone input.

## Controller profiling administration

Admin session users and ordinary admin API keys can access
`/api/v1/system/profiling`; inference-only keys cannot. Discovery lists ready
controller incarnation IDs. `POST /captures` accepts a UUID `capture_id`,
`options` (light/cpu/allocations/full with bounded duration and sampling settings),
and optional exact `targets`; omission selects all currently-ready replicas.
It returns 202 admission, not proof of execution. `GET /captures/{id}` returns
per-controller states; `GET /captures/{id}/report` includes bounded aggregate
reports. `POST /captures/{id}/stop` requests an idempotent stop of only that capture.

Options and targets are immutable per retained ID. Conflicts return 409,
unavailable Redis coordination 503, and expired/unknown captures 404. Reports
are retained for the latest four captures for at most one hour. Replacements
do not inherit commands for old incarnations. API responses include `version=1`.
See [the operational guide](../development/controller-allocation-pressure.md)
for authentication through Cognis tooling, overhead, retention, boot flags and
limitations of Python rather than native/distributed tracing.

## Metadata command palette

`GET /api/v1/search/palette` is an authenticated, human-authorized global
navigation index, not a content-search endpoint. It searches accessible
conversation/task titles, schedule names, current live deliverable titles, and
current live artifact filenames, primary agent internal/display names, and
project names. Ownership and existing project grants apply
before candidate limits; administrator status does not bypass those checks.
Main-agent destinations require ownership or an active direct-use grant;
project assignment alone does not authorize a private main conversation.
App pages, actions and role-visible Settings destinations are searched locally
using the shell's shared navigation registry.

| Query parameter | Contract |
|---|---|
| `q` | Optional name query, at most 160 characters; blank returns recent metadata |
| `limit` | Page size, default 20, range 1–40 |
| `types` | Optional repeated entity kinds: `agent`, `project`, `conversation`, `task`, `schedule`, `deliverable`, `artifact`; applied before candidate limits |
| `cursor` | Opaque keyset continuation, bound to the user, query, selected kinds and ranking version |

The response is `{items, next_cursor}`. Each compact item contains `kind`, `id`,
`title`, nullable `project_id`, `agent_id`, `status`, and `format`, plus
`updated_at`, canonical `href`, and `size_bytes`. Authorized agent metadata adds
nullable `agent_name` and `agent_avatar_url`. Project destinations add
`project_avatar_url`, `project_icon`, and `project_color`, using the same avatar
precedence and validated icon/color defaults as project identity elsewhere.
Only these display fields are selected, not project definitions or full metadata.
Content items include `origin`
(`agent`, `user`, `other`) and a `producer_name` for user uploads. Attachment
context alone never establishes agent authorship. It contains no message,
document, artifact, memory, or tool-result content. Native SQLite FTS /
PostgreSQL GIN indexes generate prefix-anchored candidates, with bounded typo
refinement. Strong name matches precede typo candidates. Operational type
priority and two promoted rows per content kind diversify mixed searches
before seek and candidate limits; content-only searches keep unrestricted
content ordering. Remaining content is not dropped. Agent-produced file
priority, update time, kind and ID provide deterministic tie-breaks.
Main-agent links resolve the canonical `agent_direct` web conversation after
activation; project links explicitly select Overview.
Scheduled tasks retain only their latest readable
execution by creation time and ID, grouped by owner and scheduler source reference.
This selection precedes title matching, candidate limits and keyset pagination.
Continuation advances over scanned candidates, so a sparse or empty page can
still have `next_cursor`; clients must bound automatic continuation.

The process-local admission guard permits one active request per human and four
per controller, with per-second start limits of four per human and twelve
globally. Database work has a two-second deadline and the operation has a three-second
outer timeout. Admission rejects with `429 search_busy`; deadline expiry returns
`503 search_timeout`; invalid or mismatched cursors return `400 invalid_cursor`.
Responses use `Cache-Control: no-store`. The UI owns its bounded, short-lived
metadata cache and fences results after access-scope changes. Proven owner-only
chat metadata and account-sidebar reconciliation may carry `metadata_only: true`
on scope invalidation events; they dirty caches without replacing the visible
snapshot. Unmarked chat/sidebar invalidations remain fail-closed. Refresh and
access invalidations must not coalesce into the same outgoing slot.
Keystrokes never
request external search, embeddings, LLM inference, or content hydration.

## Library browsing

### Artifact authoring and document sharing

Human authoring uses existing immutable artifact lineage/storage and does not
require an agent runtime or conversation. Mutations are owner-only, reject
viewer-role callers, and preserve source provenance on new revisions.

- `POST /api/v1/library/artifacts`: durable UTF-8 text creation; filename,
  MIME type, content, optional project and selected agent IDs.
- `POST /api/v1/library/artifacts/upload`: durable multipart creation; file,
  optional display filename, JSON-encoded agent IDs, optional project.
- `GET /api/v1/library/artifacts/{ref}/edit`: bounded exact-revision source.
- `POST /api/v1/library/artifacts/{ref}/revisions`: create a successor of that
  exact current revision. Stale targets return 409. JSON syntax errors return 400.
- `GET /api/v1/library/agents`: selectable active owned primary agents.
- `GET/PUT /api/v1/library/items/{kind}/{ref}/sharing`: describe inherited versus
  explicit agent access, or replace explicit recipients across the lineage.
- `GET/PUT /api/v1/library/items/{kind}/{ref}/people`: owner-only direct human
  access for the whole lineage, including future revisions. Writes use
  `principal_email`, `expected_version`, `allow_comments` and `revoked`;
  a stale version returns 409. Revocation retains its versioned tombstone.
  Recipients must be active existing users when granting access. This grants no
  conversation/task access, agent runtime access, moderation or resharing rights.
  Managed outputs remain subject to their explicit-context visibility fence.
- `POST /api/v1/library/items/{kind}/{ref}/public-link`: signed exact-revision
  `view` or `download` link; expiry uses the existing deliverable sharing contract.
  Omitted mode selects View only for eligible formats.

Managed revision-pinned public links are independent of human lineage grants.
`GET /api/v1/feedback/public-shares/{share_id}/link` returns an owner's original
recoverable token with `Cache-Control: no-store`, without changing its expiration
or version. `ShareResponse.can_copy` distinguishes recoverable live links.
Legacy random tokens cannot be recovered or silently rotated. New managed shares
support `link_mode=view|download`; download links cannot enable commenting.
Human-only artifact readers receive authenticated `/api/v1/artifacts/{ref}/bytes`
URLs, not new public bearer capabilities.

Text authoring is limited to 1 MiB and uploaded files to 20 MiB. Object storage
writes and failed-write cleanup use the canonical revision service. Revision
creation and explicit sharing serialize on immutable lineage roots before locking
revision rows; grant revocation therefore includes concurrent successors.

### Browsing endpoints

Authenticated human readers can browse metadata independently of conversations:

- `GET /api/v1/library/items`: current eligible deliverable/file revisions with
  keyset pagination (`cursor`, `limit`, maximum 100).
- `GET /api/v1/library/groups`: lazy folders/facets with complete matching counts;
  `group_by=project|conversation|agent|format`.
- `GET /api/v1/library/items/{kind}/{ref}`: exact authorized metadata.
- `PUT /api/v1/library/items/{kind}/{ref}/favorite`: set personal lineage bookmark
  state with `{"favorite": true|false}` and return metadata including
  `is_favorite`. Any authenticated reader with current document access can
  bookmark it; inaccessible references return 404. Writes are idempotent and do
  not modify document timestamps, revisions, or grants.
- `GET /api/v1/library/deliverables/{ref}/content`: exact chat, task, or
  source-detached deliverable payload.

Shared filters: `kind=deliverable|artifact`, literal title/filename tokens `q`,
`project_id`, `conversation_id`, `agent_id`, `origin=all|user|agent|other`,
`ownership=all|mine|shared`, `format`, `after`, `before`, and
`sort=updated|created`, plus `favorites=true` to restrict to the caller's
bookmarked lineages. Empty project/conversation IDs select unassigned folders.
Authorization precedes pagination and aggregation. Cursors are bound to actor,
filters, and grouping; they never grant access.

Existing ownership, project membership, and revision metadata remain canonical.
Existing artifact preview/download and deliverable revision/export/share routes
serve the corresponding operations.

## Usage resource rankings

`POST /api/v1/usage/query` and the corresponding usage tool share the
`UsageQuery` / `UsageQueryResponse` contract.

- `view: "ranking"` accepts `group_by: "task" | "schedule" | "conversation"`
  alongside the other existing dimensions.
- Optional `filters.task_origin: "scheduled" | "one_off"` and
  `filters.schedule_ids: string[]` narrow the population before aggregation.
  Schedule grouping excludes usage without an unambiguous schedule-fire link.
- `groups[].key` remains an ID; `groups[].label` is an authorized current title
  or name, or null. Accounting-admin visibility is not content-read authority.
- `ranking_measure` is null outside ranking responses, otherwise `"attempts"`,
  `"input"`, `"output"` or `"known_priced_subtotal"` for `metric: "cost"`.
  Cost ranking accepts partial pricing and retains the existing pricing-coverage
  counts and cost-band quality. Mixed monetary currency/basis strata still fail.
- `bucket: "day"` permits up to 730 days; hourly windows permit up to 90 days.
  Resource grouping uses retained summaries, not archives. Windows crossing an
  authorized detail-expiry cutoff fail with HTTP 422, code
  `resource_detail_expired`, and a message containing that cutoff. Retention is
  disabled by default.

See [usage accounting](../guide/usage-accounting.md) for coverage and retention.

## Overview

Cognis exposes a REST + WebSocket API. All clients use the same surface.
Auto-generated OpenAPI 3.1 spec from FastAPI.

Base URL: `http://localhost:8080/api/v1`

API versioning: all endpoints are prefixed with `/api/v1`. When breaking
changes are needed, a `/api/v2` prefix will be introduced alongside `v1`
with a deprecation period. Non-versioned paths (`/api/health`,
`/api/metrics`, `/api/ws`) remain unversioned.

## Authentication

- `Authorization: Bearer <jwt_token>` — user sessions
- `X-API-Key: <key>` — service accounts

JWT obtained via `POST /api/auth/login`.

WebSocket: authenticate via first message `{type: "auth", token: "..."}`.
Do NOT put tokens in query params.

## REST Endpoints

### Navigation attention summary

`GET /api/v1/dashboard/navigation-summary` requires an authenticated user and
returns only that user's active conversation attention groups, complete dashboard
issue severity totals, unresolved schedule failure count, and paused task count.
The response follows `NavigationSummaryResponse` in OpenAPI. Groups contain
`count`, `has_unread`, `session_observed`, active-session status/completion reason,
and deduplicated pending notification types; they contain no IDs, titles, or
message content. The frontend applies the shared conversation color policy.
Totals are independent of page-size and dashboard candidate limits. Dashboard
inbox unread counts and companion-service notices are combined by the shell from
their existing stores. Schedule dismissal and recovery use the same issue
projection as `/api/v1/dashboard/issues`.

### Typed inference (outside the management prefix)

`POST /v1/systemone` is the native TypeSafe-compatible decision operation:
`model`, JSON `state`, and keyed Choice/Score/Noul `questions` produce validated
typed `answers` and provider usage. It shares inference authentication,
ownership, admission limits, sanitized errors, and metadata-only observability
with chat and embeddings. Intaris service JWTs may use
`routes/intaris_evaluation`; matching that alias to a Jev model does not make it
chat-compatible. The operation uses OpenRouter or direct System One transport,
on the controller or configured executor, without chat fallbacks.

### Bootstrap (Unauthenticated)

```
GET    /api/bootstrap-status      → Report whether first-run setup is still available
POST   /api/setup                → Create first admin user (one-time, token-gated)
GET    /.well-known/jwks.json    → JWKS public keys for JWT validation
GET    /.well-known/cognis-client.json → Discover the native-client server contract
```

The setup endpoint is only available when no users exist. It requires a
one-time token printed to stdout on first start (15 min TTL).

Native and other pluggable clients should discover a server through
`/.well-known/cognis-client.json` before authentication. The response uses
root-relative paths so the client retains the configured server origin and
derives `ws` or `wss` from its HTTP scheme:

```json
{
  "schema_version": 1,
  "product": {"id": "cognis", "display_name": "Cognis"},
  "protocol": {"id": "cognis-client", "version": 1},
  "server": {
    "id": "cognis:<jwt-public-key-fingerprint>",
    "version": "0.11.2",
    "build_id": "<public Cognis package version>"
  },
  "paths": {
    "api_v1": "/api/v1",
    "login": "/api/auth/login",
    "refresh": "/api/auth/refresh",
    "logout": "/api/auth/logout",
    "current_user": "/api/auth/me",
    "chat_v2": "/api/v1/chat/v2",
    "realtime": "/api/ws",
    "jwks": "/.well-known/jwks.json"
  },
  "capabilities": {"authentication": 1, "chat": 2, "realtime": 1}
}
```

Capability integers version each advertised contract independently.
`server.id` is a connection identity hint derived from the public JWT key,
not a permanent global identifier; reinstalling the server or rotating its JWT
key changes it. Clients should require confirmation before replacing a saved
connection when the identity hint changes. The endpoint returns
`Cache-Control: no-store` and does not expose deployment configuration, users,
provider URLs, or secrets.

### Auth

```
POST   /api/auth/login           → Authenticate, get JWT (email + password)
POST   /api/auth/refresh         → Refresh JWT
POST   /api/auth/logout          → Invalidate token
POST   /api/auth/change-password → Change current user's password
GET    /api/auth/me              → Current user info
PATCH  /api/auth/me              → Update current user's profile (name)
GET    /api/v1/auth/api-keys     → List current user's API keys
POST   /api/v1/auth/api-keys     → Create current user's API key
DELETE /api/v1/auth/api-keys/:id → Revoke current user's API key
POST   /api/v1/auth/exchange-token → Issue short-lived token for Intaris/Mnemory UI access
```

### Admin User Management

All endpoints require admin role.

```
GET    /api/v1/admin/users              → List users (?include_disabled=true)
POST   /api/v1/admin/users              → Create user (email, name, password, role)
GET    /api/v1/admin/users/:email       → Get user details
PATCH  /api/v1/admin/users/:email       → Update user (name, role)
DELETE /api/v1/admin/users/:email       → Hard delete user + cascade (?confirm=true)
POST   /api/v1/admin/users/:email/disable → Disable user (soft delete)
POST   /api/v1/admin/users/:email/enable  → Re-enable user
```

Safety guards:
- Cannot delete/disable yourself
- Cannot demote/disable/delete the last admin
- Hard delete requires `?confirm=true` query parameter

### Conversations

```
GET    /api/v1/conversations                              → List conversations
POST   /api/v1/conversations                              → Create conversation
POST   /api/v1/conversations/resolve                      → Find-or-create default conversation
GET    /api/v1/conversations/:id                          → Get details
PATCH  /api/v1/conversations/:id                          → Update (title, archive)
DELETE /api/v1/conversations/:id                          → Delete
DELETE /api/v1/conversations/:id/purge                    → Purge metadata (+ Intaris cascade)
GET    /api/v1/conversations/:id/queue                    → List pending queued user messages
PATCH  /api/v1/conversations/:id/queue/:queue_id          → Edit pending queued message content
DELETE /api/v1/conversations/:id/queue/:queue_id          → Cancel pending queued message
GET    /api/v1/conversations/:id/sessions                 → List sessions
GET    /api/v1/conversations/:id/delegations              → Active delegations
POST   /api/v1/conversations/:id/handoff-links           → Link a created recipient to a handoff
```

#### Handoff Recipient Link

`POST /api/v1/conversations/:id/handoff-links` accepts `source_session_id`,
`source_event_seq`, and `recipient_conversation_id`, returning `{ "ok": true }`.
It requires a mutable owned source, an existing owned recipient, and a source
session belonging to that conversation. The referenced Intaris event must be a
completed handoff. Content is read from Intaris, not supplied by the client.
The source lifecycle update is idempotent for the event/recipient pair and
preserves the original handoff identity. This operation creates no conversation
and sends no message; call it only after successful recipient creation.

Handoff lifecycle projection uses one card per request, from `running` through
`complete` or `failed`. Recipient-link updates preserve that card and its initial
timeline position. Legacy completed handoffs without request IDs remain linkable.

#### Resolve Conversation (find-or-create)
```http
POST /api/v1/conversations/resolve
{ "agent_id": "aria", "context_type": "web" }

→ 200 OK  (existing or newly created)
{ "conversation_id": "conv_abc", "agent_id": "aria", "context": {...}, ... }
```

Used by the web UI to ensure a persistent default conversation exists for
each agent. If an active conversation matching the (user, agent, context_type)
triple exists, it is returned. Otherwise a new one is created with
`context_ref=web:user:<email>:default`.

Conversation timeline reads and user-message sends use the Chat v2 API under
`/api/v1/chat/v2/conversations/:id`. The legacy REST/SSE chat message routes were
removed; clients should use Chat v2 snapshot/sync/timeline plus WebSocket
`chat_v2_frame` updates.

For web conversations, `active_session_id` may be `null` until the first user
message is sent. Sending the first message lazily creates the root session.

#### Chat v2 Timeline
```http
GET /api/v1/chat/v2/conversations/conv_abc/snapshot

→ 200 OK
{
  "schema_version": 2,
  "projection_version": "...",
  "conversation": {"conversation_id": "conv_abc", "agent_id": "aria", ...},
  "timeline": {"items": [{"kind": "message", "role": "user", ...}], ...},
  "state": {...},
  "queue": {"queued_count": 0, "messages": []},
  "runtime": {"has_active_turn": false, "volatile_items": []},
  "cursor": "opaque-cursor",
  "server_time": "2026-03-27T10:30:05Z"
}
```

Use `GET /api/v1/chat/v2/conversations/:id/sync?cursor=...` for incremental
updates and `GET /api/v1/chat/v2/conversations/:id/timeline?before=...` for
older scrollback pages. The controller reads Intaris event streams and projects
strict Chat v2 timeline items; clients must treat the cursor as opaque.

#### Canonical Work activity list

```http
GET /api/v1/work/activities?limit=20&cursor=<opaque>
```

The endpoint returns one item for each authorized `activity_scope_id`. It orders
items by `last_activity_at DESC, activity_scope_id DESC`. The signed cursor is
owner-bound. Clients must treat it as opaque. Invalid, modified, or cross-owner
cursors return `409 cursor_invalid`.

Each item includes a server-derived `scope`. Use this complete `TimelineScope`
to open the existing Work endpoints. Do not authorize or construct a Work view
from `activity_scope_id` alone. Reset conversation activities use a `session`
scope rooted at the canonical activity root. Task activities use the latest
authorized, non-superseded `task_step` scope.

The list reads only canonical Cognis metadata. It does not read Intaris, recover
the Work graph, admit materialization, or create Work cache rows. If an active
ready generation exists, `summary` contains immutable snapshot counters and
`materialization` is `ready`. Otherwise, `summary` is `null` and
`materialization` is `absent`. Opening the activity can start recovery.

Message records may include lane metadata:

```json
{
  "lane": "side",
  "side_thread_id": "btw:turn_01HV...",
  "anchor_turn_id": "turn_01HV...",
  "prompt_visibility": "side_only",
  "tool_policy": "none"
}
```

Clients must treat `lane="main"` as the default when metadata is absent.
Side-lane messages are part of the same Intaris session history for audit and UI
replay, but are visually separated and excluded from normal main-turn model
context by the controller.

### Channels

```
GET    /api/v1/channels/types                              → Supported channel metadata
GET    /api/v1/channels/accounts                           → List configured channel accounts
POST   /api/v1/channels/accounts                           → Create channel account (defaults to pairing)
GET    /api/v1/channels/accounts/:id                       → Get channel account
PATCH  /api/v1/channels/accounts/:id                       → Update channel account
DELETE /api/v1/channels/accounts/:id                       → Delete channel account
POST   /api/v1/channels/accounts/:id/start                 → Start adapter
POST   /api/v1/channels/accounts/:id/stop                  → Stop adapter
GET    /api/v1/channels/accounts/:id/status                → Runtime adapter status
POST   /api/v1/channels/webhook/:channel_type/:account_id  → Inbound webhook (platform-authenticated)
GET    /api/v1/channels/webhook/:channel_type/:account_id  → Webhook verification challenge
GET    /api/v1/channels/contacts                           → Verified external sender mappings
POST   /api/v1/channels/contacts                           → Manually create verified sender mapping
GET    /api/v1/channels/pairing-requests                   → List pending pairing requests
POST   /api/v1/channels/pair                               → Redeem a sender-initiated pairing code
POST   /api/v1/channels/pairing-requests/:id/reject        → Reject a pending pairing request
```

Channel accounts should default to `pairing` so unknown remote senders cannot
talk to an agent until the authenticated Cognis user redeems their short-lived
pairing code in the web UI.

#### Send Message (Chat v2)
```http
PUT /api/v1/chat/v2/conversations/conv_abc/messages/web_01HV...
Content-Type: application/json
{ "content": "What is the weather?", "client_message_id": "web_01HV..." }

→ 202 Accepted
{ "status": "accepted", "client_txn_id": "web_01HV...", ... }
```

The path transaction id gives durable idempotency. Realtime progress is delivered
over WebSocket as `chat_v2_frame`; clients recover missed frames with `/sync` or
`/snapshot`. Slash commands are not accepted by Chat v2 send and should use the
WebSocket command path.

Error codes: `not_found` (404), `forbidden` (403), `session_ended` /
`session_suspended` (409), `rate_limited` / `queue_full` (429).

#### Move a Running Tool Call to the Background (Chat v2)
```http
POST /api/v1/chat/v2/conversations/conv_abc/tool-calls/toolu_01.../background

→ 200 OK
{ "conversation_id": "conv_abc", "call_id": "toolu_01...", "tool_name": "bash",
  "status": "requested", "server_time": "..." }
```

Asks the executor to continue a running foreground `bash` call of the active
turn as a background shell. The call's own tool result reports the handoff
(`moved_to_background`, `shell_id`); `status` is `already_requested` for a
repeated request. Requires mutation access to the conversation.

Error codes: `unknown_call` (404), `no_active_turn` / `unsupported_tool` /
`not_backgroundable` / `executor_unsupported` (409), `executor_unavailable` (503).

#### Queued Messages

When a user sends another message while a turn is still processing, Cognis keeps
that input in the conversation's in-memory pending queue. The queue API exposes
the exact pending messages so clients can render, edit, or cancel them before the
scheduler starts processing them.

```http
GET /api/v1/conversations/conv_abc/queue

→ 200 OK
{
  "queued_count": 2,
  "messages": [
    {
      "queue_id": "qmsg_01HV...",
      "client_message_id": "web_01HV...",
      "content": "Follow-up question",
      "attachments": [],
      "created_at": "2026-03-27T10:30:03Z",
      "updated_at": "2026-03-27T10:30:03Z",
      "position": 1
    }
  ]
}
```

`queue_id` is the server-owned identifier for queue mutation. `client_message_id`
is an optional caller-provided correlation ID used by web clients to reconcile an
optimistic pending bubble with the persisted user-message event when the queued
turn starts.

```http
PATCH /api/v1/conversations/conv_abc/queue/qmsg_01HV...
{ "content": "Edited follow-up question" }

→ 200 OK
{ "queue_id": "qmsg_01HV...", "content": "Edited follow-up question", ... }
```

```http
DELETE /api/v1/conversations/conv_abc/queue/qmsg_01HV...

→ 204 No Content
```

Editing only changes message text. Queued attachments are immutable; clients
should offer delete-and-recreate when the user needs to change attachments.
Mutation endpoints return `404 not_found` if the queued message was already
removed or popped for processing. Queue mutation uses the same conversation
authorization as message sends: owners can edit or cancel pending queue entries,
while read-only viewers can inspect queue state but receive `403 forbidden` for
queue mutation attempts.

### Agents

```
GET    /api/v1/agents                         → List owned agents + agents shared with caller
GET    /api/v1/agents/memory-backends         → Authoritative memory backend/mode descriptors
POST   /api/v1/agents                         → Create agent
GET    /api/v1/agents/:id                     → Get details (owner: full; grantee: read-only)
PUT    /api/v1/agents/:id                     → Update (owner only)
DELETE /api/v1/agents/:id                     → Archive (owner only; cascades grants)
POST   /api/v1/agents/:id/activate           → Activate draft (owner only)
POST   /api/v1/agents/:id/suspend            → Suspend (owner only)
POST   /api/v1/agents/:id/sync-personality   → Sync to Mnemory (owner only)
GET    /api/v1/agents/:id/card                → A2A Agent Card (deferred unless public discovery metadata is available)
```

Agent responses also include read-only Mnemory bootstrap fields:
- `personality_synced`
- `personality_sync_error`
- `personality_sync_checked_at`

Agent responses additionally expose the sharing perspective of the
caller:
- `is_shared_with_me` — true when accessed via an active grant rather
  than ownership.
- `shared_by_email` — agent owner (when shared).
- `granted_permission` — currently always `"use"` when shared.
- `executor_scope` — `"owner_executor"` or `"grantee_executor"` when
  shared; null otherwise.
- `is_readonly_for_caller` — true for grantees, false for owners.

Access to agent-scoped routes uses `check_agent_access(required=...)`.
Ownership and active `use` grants are the only paths; the admin role
is **not** a third path for user-owned agents. See
[28-agent-sharing.md](28-agent-sharing.md).

#### Agent sharing

```
GET    /api/v1/agents/:id/shares                   → List active grants (owner only)
POST   /api/v1/agents/:id/shares                   → Create grant (owner only)
PATCH  /api/v1/agents/:id/shares/:grant_id         → Update grant (owner only)
DELETE /api/v1/agents/:id/shares/:grant_id         → Revoke grant (owner only)
GET    /api/v1/users/me/shared-with-me             → List agents shared with caller
```

Grant create body:

```json
{
  "grantee_email": "alice@example.com",
  "permission": "use",
  "executor_scope": "owner_executor",
  "include_bound_secondaries": ["secondary_id_1", "secondary_id_2"],
  "note": "Family assistant"
}
```

`include_bound_secondaries` is an owner convenience: for each listed
non-system secondary bound to this agent, the server opens an
identical grant to the same grantee in the same transaction.

Revocation (`DELETE`) soft-marks the grant with `revoked_at = now()`
and pauses the grantee's active schedules and non-terminal tasks
against the agent with reason `access_revoked`.

### Sessions

```
GET    /api/v1/sessions/:id                   → Session details
POST   /api/v1/sessions/:id/cancel            → Cancel
```


### Tools

```
GET    /api/v1/tools                          → List static tool catalog (built-in + executor-native + web)
GET    /api/v1/tools/local-mcp/observed       → List cached observed local MCP tools across the user's executors
GET    /api/v1/tools/executor                 → List executor-native tools with status
GET    /api/v1/agents/:id/tools               → Tools for agent (filtered by agent config)
POST   /api/v1/agents/:id/mcp/test           → Test local MCP server discovery for agent
GET    /api/v1/mcp/servers                    → List MCP servers (local + Intaris)
GET    /api/v1/intaris/mcp/servers            → Auto-discover Intaris MCP servers
GET    /api/v1/intaris/mcp/tools              → List all Intaris MCP tools
```

### Skills

```
GET    /api/v1/skills                         → List all skills
POST   /api/v1/skills                         → Create skill
GET    /api/v1/skills/:id                     → Get skill detail
PUT    /api/v1/skills/:id                     → Update skill
DELETE /api/v1/skills/:id                     → Delete skill (DB-managed only)
POST   /api/v1/skills/:id/suggest-steps       → Suggest workflow step fragments from instructions
POST   /api/v1/skills/:id/export             → Export skill as YAML
POST   /api/v1/skills/import                  → Import skill from YAML
```

### Executors

```
GET    /api/v1/executors                       → List executor configurations
GET    /api/v1/executors/:id                   → Get executor configuration
POST   /api/v1/executors                       → Create executor configuration
PUT    /api/v1/executors/:id                   → Update executor configuration
DELETE /api/v1/executors/:id                   → Delete executor configuration
POST   /api/v1/executors/:id/default           → Set as default executor
POST   /api/v1/executors/:id/token             → Generate executor JWT (owner-scoped)
POST   /api/v1/executors/:id/reconfigure       → Retry active WebSocket runtime initialization (202, ExecutorConfigResponse)
GET    /api/v1/executor/status                 → Executor status and capabilities
GET    /api/v1/tools/executor                  → List executor-native tool definitions
GET    /api/v1/mcp-servers                      → List current user's MCP server configs
GET    /api/v1/mcp-servers/:id                  → Get current user's MCP server config
POST   /api/v1/mcp-servers                      → Create MCP server config (user-scoped)
PUT    /api/v1/mcp-servers/:id                  → Update MCP server config (user-scoped)
DELETE /api/v1/mcp-servers/:id                  → Delete MCP server config (user-scoped, 409 if referenced)
POST   /api/v1/mcp-servers/:server_id/oauth/start       → Start OAuth authorization for an HTTP MCP server
GET    /api/v1/mcp/oauth/callback                       → Public OAuth redirect callback
GET    /api/v1/mcp-servers/:server_id/oauth/status      → Current user's MCP OAuth connection status
POST   /api/v1/mcp-servers/:server_id/oauth/disconnect  → Revoke local stored MCP OAuth tokens
GET    /api/v1/agents/:id/effective-tools       → Effective tool set for saved agent
POST   /api/v1/agents/effective-tools/preview   → Effective tool preview for unsaved agent draft
```

MCP OAuth start/status responses include the selected flow. Authorization-code
starts return a browser authorization URL. Device-code starts return only safe
operator fields such as the provider verification URI, optional complete
verification URI, user code, expiry, and polling interval; they must not expose
the device code, access tokens, refresh tokens, client secrets, or raw credential
headers.

The OAuth status response keeps the existing ``connected`` field but now defines
it as a currently usable access token. Additive fields distinguish
``authorized``/``authorization_required``, ``runtime_connected``, ``invalid``,
``outcome_unknown``, ``refresh_state``, bounded retry timing, sanitized last
refresh diagnostics, and per-executor desired/applied runtime convergence.

### Secrets

```
GET    /api/v1/secrets                         → List (metadata only)
POST   /api/v1/secrets                         → Create/update
DELETE /api/v1/secrets/:name                   → Delete
```

### Credentials

```
GET    /api/v1/credentials                     → List structured credential records (metadata only)
GET    /api/v1/credentials/:id                 → Get credential metadata
POST   /api/v1/credentials                     → Create/update a structured credential record
POST   /api/v1/credentials/:id/revoke         → Revoke a credential without deleting it
DELETE /api/v1/credentials/:id                → Delete a credential record
```

`secrets` remain the low-level encrypted storage primitive used for provider
keys, MCP env refs, and similar infrastructure concerns. `credentials` are the
agent-facing structured auth records used for browser automation, saved auth
state, and controller-mediated auth flows. Credential payload values are never
returned by the REST API.

### Tasks (Kanban / Work Queue)

```
GET    /api/v1/tasks                              → List tasks (filterable by status, agent, priority, queue)
POST   /api/v1/tasks                              → Create task (draft by default, or queued/ready with explicit start controls)
GET    /api/v1/tasks/:id                          → Task detail + workflow progress + step runs + dependencies + delivery config + workflow lifecycle metadata
PATCH  /api/v1/tasks/:id                          → Update (title, description, priority, agent, workflow, delivery)
DELETE /api/v1/tasks/:id                          → Cancel and remove
POST   /api/v1/tasks/:id/submit                   → Move draft → queued (start execution)
POST   /api/v1/tasks/:id/pause                    → Pause running task
POST   /api/v1/tasks/:id/resume                   → Resume paused task
POST   /api/v1/tasks/:id/retry                    → Recover failed task in place with saved step history (TaskActionResponse)
POST   /api/v1/tasks/:id/cancel                   → Cancel task (any state)
POST   /api/v1/tasks/:id/gate-response            → Respond to a gate step
POST   /api/v1/tasks/:id/step-response            → Respond to the current `step_request_questions` question set
GET    /api/v1/notifications                      → List pending notifications (escalations, gates, step questions, credential requests, auth challenges)
POST   /api/v1/notifications/:id/resolve         → Resolve a notification directly
POST   /api/v1/tasks/batch-submit                 → Submit multiple draft tasks at once
GET    /api/v1/tasks/:id/steps                    → List step runs with status and output
GET    /api/v1/step-runs/:id                      → Step run detail (output, evaluation, attempts)
POST   /api/v1/tasks/:id/dependencies             → Add dependency (depends_on task_id, required bool)
DELETE /api/v1/tasks/:id/dependencies/:dep_id     → Remove dependency
```

Task create/update payloads also support:
- `delivery_mode`
- `delivery_target`
- `draft`
- `start_immediately`

These control where task results/questions are routed back (same conversation,
specific conversation, latest active for agent, preferred channel, or silent).

Pending step-question pauses expose a canonical `questions` array. Rich clients
respond to `/step-response` with structured answers:

```json
{
  "step_name": "plan",
  "mode": "structured",
  "answers": [
    {
      "question_id": "strategy",
      "selected_option_ids": ["jwt"],
      "custom_answer": null
    }
  ]
}
```

Signal and other plain-text-like channels do not send per-question selections;
their full inbound message is recorded as `mode: "plain_text"` with one custom
answer for the first question so the agent can interpret it in context.

Task creation preserves draft behavior by default. `draft=true` or
`start_immediately=false` stores a task without enqueueing it. Use
`start_immediately=true` or create with a queued/ready status to submit work
through the normal queue path.

Step run timing fields distinguish latest-attempt and total time:

- `duration_seconds` / `latest_attempt_duration_seconds` — duration of the most
  recent attempt when available
- `accumulated_duration_seconds` — sum of completed attempt durations for the
  step in task diagrams and detail responses

Gate step metadata can include `gate_evaluation`, a structured object with the
condition expression, referenced values, expected or threshold values, operator,
actual result, pass/fail outcome, branch/action taken, and evaluation errors.

Workflow pauses can now include:

- `credential_request` — request a durable structured credential from the user
- `auth_challenge` — request a live MFA / OTP / push-approval response

Notification resolution for these flows stores only safe metadata and returns
opaque credential IDs or ephemeral credential refs back into the workflow when
needed.

### Schedules (Phase 2, model in MVP)

```
GET    /api/v1/schedules                          → List schedules
POST   /api/v1/schedules                          → Create schedule
PUT    /api/v1/schedules/:id                      → Update schedule
DELETE /api/v1/schedules/:id                      → Delete schedule
POST   /api/v1/schedules/:id/trigger              → Fire schedule immediately (create task)
```

### Escalations

Cognis proxies escalation management through Intaris:

```
GET    /api/v1/escalations                    → List pending (via Intaris /audit)
POST   /api/v1/escalations/:call_id/resolve   → Resolve (via Intaris /decision)
```

#### Resolve Escalation
```http
POST /api/v1/escalations/call_abc/resolve
{"decision": "approve", "note": "Looks safe"}

→ 200 {"call_id": "call_abc", "decision": "approve"}
```

### Settings (App Configuration — No Config File)

All application-level configuration is stored in the database and managed
through these endpoints. Infrastructure config (URLs, keys) uses env vars.

```
GET    /api/v1/settings                       → List all settings (grouped by category)
GET    /api/v1/settings/:key                  → Get single setting
PUT    /api/v1/settings/:key                  → Update setting (admin only)
DELETE /api/v1/settings/:key                  → Reset to the registry default (admin only)
```

Unknown setting keys are rejected. Known settings validate value types against
the typed application registry. List/detail/update/reset responses include
`default_value`, `is_overridden`, `label`, `description`, `section`,
`value_type`, options/bounds/unit, and `apply_scope`.
`is_overridden` is a value comparison against the current registry default;
it does not track historical administrator intent.

Exposed settings are restartless: updates are either hot-applied in the current
worker or read at the next safe operation/runtime boundary. Legacy stored keys
without sound runtime semantics remain bootstrapped for compatibility but are
not exposed by these endpoints.

### LLM Providers

```
GET    /api/v1/llm-providers                  → List configured providers
POST   /api/v1/llm-providers                  → Add provider (admin only)
GET    /api/v1/llm-providers/:id              → Provider details + model catalog
PUT    /api/v1/llm-providers/:id              → Update provider
DELETE /api/v1/llm-providers/:id              → Remove provider
POST   /api/v1/llm-providers/:id/test        → Test provider connectivity (resolved model, latency, sanitized errors)
```

### Local Models

```
GET    /api/v1/local-model-catalog                         → Search catalog with per-source availability
GET    /api/v1/local-model-catalog/detail                  → Resolve one bounded HF repository detail view
GET    /api/v1/local-model-catalog/resolve                 → Normalize one direct WS2A model reference
POST   /api/v1/local-model-fit-plans                       → Build an advisory per-executor capacity plan
GET    /api/v1/local-model-deployments                     → List visible desired-state deployments
POST   /api/v1/local-model-deployments                     → Create a desired-state deployment
GET    /api/v1/local-model-deployments/:id                 → Get one visible deployment
PATCH  /api/v1/local-model-deployments/:id                 → Update managed desired state
DELETE /api/v1/local-model-deployments/:id                 → Delete desired state when dependencies allow
GET    /api/v1/local-model-deployments/:id/targets         → List materialized executor targets
GET    /api/v1/local-model-deployments/:id/operations      → List durable runtime operations
GET    /api/v1/local-model-deployments/:id/status          → Generation-aware rollout summary
POST   /api/v1/local-model-deployments/:id/reconciliation-requests
                                                            → Request declarative reconciliation
GET    /api/v1/executors/:id/local-model-runtime           → Probe managed Ollama status
POST   /api/v1/executors/:id/local-model-runtime/operations
                                                            → Create exact-target pull/delete operation
POST   /api/v1/executors/:id/local-model-runtime/operations/:operation_id/cancellation-requests
                                                            → Request durable cancellation
POST   /api/v1/llm-providers/:id/local-models:upsert       → Atomically add/merge a configured model
```

Catalog and fit endpoints are authenticated and advisory only. Hugging Face access is public and
credential-free. Search is a fast bounded page and never fans out to repository APIs. Optional
`parameter_range`, `download_size_range`, `quantization`, `min_context`, and `include_unknown`
filters apply only to metadata in that upstream page; `next_cursor` still advances the upstream HF
page, so a filtered page may contain fewer than `limit` items. The search cache key includes source,
normalized query, upstream cursor, limit, and every filter.

`/detail` accepts only a validated `owner/repository` and optional 40-character revision SHA. It uses
only fixed `https://huggingface.co` model-info and revision-pinned README endpoints, refuses redirects,
and bounds concurrency, deadlines, response bodies, README bytes, cache entries, and normalized
fields. Detail work is coalesced and cached by repository plus SHA. A detail failure is diagnostic for
that item and does not retroactively mark a successful search source unavailable.

Fit plans accept any positive context without clamping and never mutate runtime state; deployment
creation remains the only write in the planning flow. See
`36-local-model-declarative-foundation.md` for selector authorization, reference integrity, capacity
math, and override semantics.

### Model Routing

```
GET    /api/v1/model-routing                  → Current routing policy
PUT    /api/v1/model-routing                  → Update routing policy (admin only)
```

### Workflows

```
GET    /api/v1/workflows                      → List workflows (system + user)
POST   /api/v1/workflows                      → Create user workflow
GET    /api/v1/workflows/:id                  → Workflow details + steps
PUT    /api/v1/workflows/:id                  → Update workflow
DELETE /api/v1/workflows/:id                  → Delete workflow (user only)
POST   /api/v1/workflows/:id/duplicate        → Duplicate workflow
POST   /api/v1/workflows/:id/promote          → Return pre-populated workflow draft from an ephemeral workflow
```

Workflow listing defaults to persistent, non-archived workflows. Debugging and
agent introspection can opt into ephemeral workflows with `?include_ephemeral=true`.

### Workflow Runs

```
GET    /api/v1/tasks/:id/workflow-run         → Current workflow run status + step progress
POST   /api/v1/tasks/:id/gate-response        → Respond to a gate step (approve/revise/cancel)
GET    /api/v1/workflow-runs/:id/steps        → Deferred in MVP; task-backed workflow state uses /api/v1/tasks/:id/steps instead
GET    /api/v1/step-runs/:id                  → Step run detail (output, evaluation, attempts)
```

### System

```
GET    /api/health                            → Health check
GET    /api/health/providers                  → Provider status
GET    /api/v1/system/diagnostics             → Admin diagnostics and readiness summary
GET    /api/metrics                           → Prometheus metrics
GET    /.well-known/jwks.json                 → Public keys for JWT validation
GET    /.well-known/cognis-client.json        → Public native-client discovery contract
GET    /.well-known/agent.json                → Default agent card (A2A; deferred unless public discovery metadata is configured)
```

## WebSocket API

### Connection
```
WS /api/ws
First message: {type: "auth", token: "<jwt>"}
Auth timeout: 10 seconds (configurable via security.ws_auth_timeout_seconds).
Connections that do not send a valid auth message within the timeout are closed.
```

### Client → Server
```typescript
{type: "message", conversation_id, content, client_message_id?}  // User message
{type: "cancel", conversation_id, session_id?}     // Cancel
{type: "cancel_queued_message", conversation_id, queue_id}  // Cancel pending queued message
{type: "update_queued_message", conversation_id, queue_id, content}  // Edit pending queued text
{type: "resolve_escalation", call_id, decision, note?}
{type: "gate_response", task_id, step_name, action, feedback?}  // Respond to workflow gate
{type: "step_response", task_id?, notification_id?, step_name?, mode, answers}  // Respond to step_request_questions question set
{type: "reconnect", conversation_id, last_seq}     // Reconnect and replay missed events
{type: "ping"}
```

The web client sends a heartbeat `ping` roughly every 30 seconds after the
socket authenticates so stalled connections are detected proactively.

Conversation-scoped frames are authorized before subscribing the socket or
delivering queue snapshots. Queue edit/cancel frames require mutation access;
read-only viewers receive `forbidden` and cannot change pending messages.

### Server → Client
```typescript
// Streaming
{type: "chunk", conversation_id, session_id, message_id, content, index}
{type: "message_complete", conversation_id, message_id, seq, token_usage, queued_count, messages?}
// Thinking (reasoning-capable models only)
{type: "assistant_thinking_chunk", conversation_id, session_id, message_id, block_id, delta, title?, complete}
{type: "assistant_thinking_block", conversation_id, session_id, message_id, block_id, title?, complete}

// Tool calls
{type: "tool_call", conversation_id, session_id, call_id, tool_name,
 arguments?, status}
{type: "tool_result_chunk", conversation_id, session_id, call_id, tool_name,
 delta, stream?, is_error, chunk_index?, content_offset?, live_output_available?}
{type: "tool_result", conversation_id, session_id, call_id, tool_name,
 result, is_error, duration_ms, output_size?, truncated?,
 agent_visible_truncated?, has_full_output?, recovery_call_id?,
 tool_output_artifact_id?, anchors_available?, anchor_count?,
 transport_truncated?}

// Conversation metadata
{type: "conversation_updated", conversation_id, title?}
{type: "conversation_state_snapshot", conversation_id, state}
{type: "conversation_state_delta", conversation_id, delta_id, state_version,
 snapshot_required, changed_paths, replace, source}
{type: "queued", conversation_id, queued_count}
{type: "queued_messages_updated", conversation_id, queued_count, messages}

// Delegations (sub-session lifecycle)
{type: "delegation_started", conversation_id, parent_session_id,
 child_session_id, mode, agent_id, task}
{type: "delegation_progress", conversation_id, child_session_id,
 step, progress, token_usage}
{type: "delegation_completed", conversation_id, child_session_id, result,
 result_content?, result_source?, result_truncated?, result_anchors?, result_sections?}
{type: "delegation_failed", conversation_id, child_session_id, reason}

// Escalations
{type: "escalation", conversation_id, session_id, call_id, tool_name,
 arguments, risk, reasoning, timeout_seconds}
{type: "escalation_expired", call_id}

// Workflow progress
{type: "workflow_composed", conversation_id, task_id, workflow_id, workflow_name,
 lifecycle, steps}
{type: "workflow_step_started", task_id, step_name, step_type, attempt}
{type: "workflow_step_completed", task_id, step_name, attempt, output_summary}
{type: "workflow_step_rejected", task_id, step_name, attempt, feedback}
{type: "workflow_step_failed", task_id, step_name, attempt, reason}
{type: "workflow_gate", task_id, step_name, message, options, context}
{type: "workflow_step_question", notification_id, task_id?, step_name, question, options, context}
{type: "workflow_completed", task_id, result}
{type: "workflow_failed", task_id, reason}

// Session lifecycle
{type: "session_compacted", conversation_id, session_id, previous_session_id,
 summary_preview, method, turns_compacted}
{type: "session_reset", conversation_id, session_id, previous_session_id}
{type: "history_rebased", conversation_id, session_id, previous_session_id,
 operation, undo_available, redo_available}
{type: "conversation_created", conversation_id, old_conversation_id}
{type: "session_recovered", conversation_id, session_id, reason}

// System messages (slash command feedback, cancel notifications, etc.).
// Visible /profile, /model, and /thinking command feedback is persisted as
// lifecycle event="system_notice" and echoed with notice_id; Chat v2 clients
// use item id system:{notice_id} for local/canonical reconciliation.
{type: "system_message", conversation_id, text, notice_id?, kind?, scope?, command_result?}

// Queue status
{type: "queued", conversation_id, queued_count}

// Reconnection
{type: "reconnected", conversation_id, missed_events_count}

// Auth
{type: "authenticated", user_email, role}

// Escalation resolution
{type: "escalation_resolved", conversation_id, call_id, decision, reason}

// Streaming gap notification
{type: "chunk_gap", conversation_id, dropped_count}

// Workflow cancellation
{type: "workflow_cancelled", conversation_id, task_id, reason}

// Errors
{type: "error", conversation_id?, code, message, recoverable, error_detail?, detail?}

{type: "pong"}
```

Notes:
- `seq` in `message_complete` is the Intaris event sequence number. The
  client tracks this for reconnection.
- Conversation detail responses include `conversation_state`, and message
  history responses include `state_snapshot`. These are authoritative
  backend-projected chat-facing state envelopes. For normal chats the envelope
  contains only active conversation/session state. For task-linked chats, the
  backend resolves authorized task or step links from persisted conversation
  records and projects task status, relevant/current step state, step todos,
  pending input/credential/auth/escalation summaries, capabilities, and offsets.
- `conversation_state_snapshot` is emitted after a successful reconnect/open
  subscription so stale client cache is not the source of truth.
- `conversation_state_delta` carries an idempotent replacement delta with
  delta fields flattened at the top level. In the first protocol version,
  clients should apply `replace.state` as the
  replacement authoritative state subtree for the given `conversation_id`.
  Deltas are live hints; if the client detects a gap or receives
  `snapshot_required`, it should reload history or reconnect for a fresh
  snapshot.
- Conversation state uses `state_version: 1`. Linked task and step references
  are informational projections; task and step backend models remain the
  authoritative owners of task state.
- `tool_call` events now include `arguments` (optional) when status is `"started"`.
  A separate `tool_result` event delivers the result after execution completes.
  For direct chat, the controller also emits a second `tool_call` with
  `status: "completed"` alongside `tool_result`.
- `tool_result.result` is the bounded canonical visible output. Optional
  truncation/recovery fields are additive metadata; `transport_truncated`
  means the WS/SSE payload is a smaller middle-truncated preview and clients
  should not replace a richer streamed buffer with that preview.
- `tool_result_chunk` carries incremental visible output for tools that stream.
  `chunk_index` and `content_offset` are optional ordering metadata that allow
  clients to ignore stale/replayed chunks during reconnect or multi-tab races.
  `content_offset` is monotonic across bounded active-snapshot truncation and
  uses UTF-16 code units, matching browser string indexing.
  `live_output_available` indicates that the generic paged output endpoint can
  read the running call's bounded live spool.
- `active_tool_outputs` in history responses contains bounded in-flight tool
  output snapshots keyed by `conversation_id`, `session_id`, and `call_id` so
  refresh/reconnect can hydrate running tool cards without loading raw output.

### Paged Tool Output

```http
GET /api/v1/conversations/{conversation_id}/tool-outputs/{call_id}?session_id={session_id}&offset=0&limit=200&latest=false
```

Returns a generic page of output for the chat full-output viewer. While a tool
is running, the endpoint reads the bounded live spool; after completion it reads
the ToolOutputStore when full output was persisted; otherwise it returns the
canonical bounded event preview with `recoverable=false`.

```typescript
{
  conversation_id, session_id?, call_id,
  status: "running" | "completed" | string,
  source: "live_spool" | "stored_output" | "event_preview",
  content,
  chunks?: [{index, offset, stream?, text}],
  offset, limit,
  next_offset?, prev_offset?,
  has_more_before, has_more_after,
  output_size, total_lines?,
  recoverable, truncated, spool_truncated?
}
```

For `live_spool`, offsets are stream content offsets and `latest=true` jumps to
the newest bounded page. For `stored_output`, offsets follow the existing
ToolOutputStore line-based paging semantics.
- `reasoning` events carry LLM thinking/reasoning tokens (Anthropic extended
  thinking, OpenAI reasoning content). Streamed incrementally like `chunk`.
- `conversation_updated` notifies clients of metadata changes such as
  auto-generated titles. Fires asynchronously after the first turn.
- Delegation events track sub-session lifecycle. When an agent calls
  `delegate`, the controller creates a child
  Intaris session under the parent and spawns a background agent loop.
  `delegation_started` fires immediately, `delegation_completed` or
  `delegation_failed` fires when the child session finishes. The child
  session's result is also recorded as an Intaris event in the parent
  session so the parent agent sees it on the next context assembly. Completion
  payloads include bounded durable `result_content` plus anchor metadata when
  recoverable output is available. Cognis prefers explicit deliverable content;
  otherwise it aggregates child assistant messages chronologically. The child
  sub-session detail API returns the same `result_content`, `result_anchors`,
  and bounded `result_sections`; list endpoints may omit those fields to stay compact.
- On reconnect, the server replays missed events since `last_seq` and
  sends `reconnected` when replay is complete. See
  [09-ui-ux.md](09-ui-ux.md) for the full reconnection protocol.

## Executor WebSocket Protocol

Separate WebSocket endpoint for executor connections:
```
WS /api/executor/ws
```

See [04-controller-executor.md](04-controller-executor.md) for the full
JSON-RPC protocol specification.

## Error Format

```json
{
  "error": {
    "code": "not_found",
    "message": "Conversation not found",
    "details": {"conversation_id": "..."}
  }
}
```

| Code | HTTP | Description |
|------|------|-------------|
| `unauthorized` | 401 | Invalid/expired token |
| `forbidden` | 403 | Insufficient permissions |
| `not_found` | 404 | Resource not found |
| `conflict` | 409 | State conflict |
| `validation_error` | 422 | Invalid request |
| `rate_limited` | 429 | Too many requests |
| `provider_error` | 502 | Upstream provider error |
| `internal_error` | 500 | Server error |

## Conversation Sidebar Projection

`GET /api/v1/conversations/waiting` pages unread/actionable candidates separately
from history. It accepts `cursor`, `limit` (1–100), channel/agent/project filters,
and title query `q`. It always excludes archived/deleted, task-execution,
task-control, and agent-direct conversations and enforces user ownership.
Without an explicit context filter (or with only `web` selected), its scope is
ordinary web topics: no channels, managed/workflow conversations, main/direct
chats, or legacy default/direct refs. Explicit channel filters can request channel
Waiting, but do not broaden the Chat navigation badge's fixed topic scope.
The response contains `conversations: CursorPage<ConversationResponse>` and
`active_conversation_ids`, resolved through the same direct/managed/background
activity owners as navigation. Clients exhaust these candidate pages and apply
the shared Waiting predicate before publishing a replacement snapshot.
`activity_complete=false` indicates that optional executor telemetry failed or
timed out; the endpoint still returns fresh database-backed candidates and known
active IDs with HTTP 200. Clients retain last-known active classifications for
returned candidates in the same owner/filter scope until a complete lookup
confirms activity again. They must not infer idle from missing activity on an
incomplete page. Activity knowledge is committed only after the full traversal
succeeds. Unread/attention data is not cached as a substitute for a failed query.
Waiting cursors cannot be reused with history or a different filter scope.
This endpoint does not read messages, mark conversations read, or dismiss
notifications.

`GET /api/v1/conversations/sidebar` supports full snapshots and
`changed_since` deltas. Full responses include `background_work` and set
`background_work_changed=true`. A delta may omit unchanged expensive sections:
when background work has not changed, it returns `background_work=null` with
`background_work_changed=false`; clients must preserve their cached projection
rather than clearing it. Changed background work is returned in full with the
flag set. Delta responses likewise omit unchanged agent/context catalogs and
contain only changed conversation/direct-chat rows plus tombstones.

## Pagination

Cursor-based:
```http
GET /api/v1/conversations?limit=20&cursor=conv_abc

→ {items: [...], cursor: "conv_next", has_more: true}
```

For events: `after_seq`-based (matches Intaris pagination).
